#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "numpy>=2.0",
#     "pillow>=10.0",
#     "scipy>=1.13",
#     "opencv-python-headless>=4.10",
#     "pymatting>=1.1.14",
#     "scikit-learn>=1.5",
#     "resvg-py>=0.2",
# ]
# ///

"""
prepress_vectorize.py - Spot-Color Ray Unmixing & Cutout Spline VTracer SVG + Decoupled Prepress Vectorization

Universal, mathematically rigorous prepress CLI tool for background removal, spot-color unmixing, and vectorization.
Decomposes illustration artwork into production-grade vector Cutout SVGs or transparent PNGs:
- Vector Mode (default): Real-ESRGAN (2x) + continuous background field B(x,y)
  + strict spot-color ray unmixing + 9x9 local core support constraint (4px radius at 2x resolution) (scipy.ndimage.maximum_filter)
  + strictly flat (K+1)-palette quantization + VTracer cutout splines + background path stripping
  + exact canonical hex snapping (<= K spot inks) + clean scalable SVG + resvg 300 DPI RGBA render.
- Raster Modes (dtg / dtf / screenprint): Edge-preserving chroma smoothing + continuous B(x,y) field
  + topological trimap + closed-form Laplacian matting + known-B color-line projection
  + decoupled post-processing (continuous DTG, firm alpha floor DTF, or spot-color silkscreen quantization).
- Prepress 2x2 verification sheet and 1:1 native-resolution diagnostic inspection crops.
"""

import argparse
import copy
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from typing import Dict, List, Optional, Tuple, Any

EXPECTED_REALESRGAN_BIN_SHA256 = os.environ.get(
    "PIXELPRESS_ESRGAN_BIN_SHA256",
    "22c2296a39b40cc43ac92615c827ab6d4b60b7142e8cee59f718e3f8e2cdbda9",
)

import cv2
import numpy as np
import pymatting
import resvg_py
import scipy.ndimage
from PIL import Image, ImageDraw, ImageFont
from scipy.ndimage import maximum_filter
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score



def get_system_font(size: int = 16) -> ImageFont.ImageFont:
    """Finds a crisp clean system truetype font with fallback to default."""
    candidates = [
        "/usr/share/fonts/google-droid-sans-fonts/DroidSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/liberation-sans/LiberationSans-Bold.ttf",
        "/usr/share/fonts/google-droid-sans-fonts/DroidSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def hex_to_rgb_float(hex_str: str) -> np.ndarray:
    """Converts hex color string to float64 RGB in [0, 1]."""
    clean = hex_str.strip().lstrip("#")
    if len(clean) == 3:
        clean = "".join(c * 2 for c in clean)
    if len(clean) != 6:
        raise ValueError(f"Invalid hex color: {hex_str}")
    return np.array([int(clean[i : i + 2], 16) for i in (0, 2, 4)], dtype=np.float64) / 255.0


def rgb_to_hex(rgb_float: np.ndarray) -> str:
    """Converts float64 RGB in [0, 1] to uppercase hex string."""
    r, g, b = (np.clip(rgb_float[:3], 0.0, 1.0) * 255.0).round().astype(int)
    return f"#{r:02X}{g:02X}{b:02X}"


def hex_to_rgb_255(hex_str: str) -> np.ndarray:
    """Converts hex color string to float32 RGB in [0, 255]."""
    clean = hex_str.strip().lstrip("#")
    if len(clean) == 3:
        clean = "".join(c * 2 for c in clean)
    if len(clean) != 6:
        raise ValueError(f"Invalid hex color: {hex_str}")
    try:
        return np.array([int(clean[i : i + 2], 16) for i in (0, 2, 4)], dtype=np.float32)
    except ValueError as e:
        raise ValueError(f"Invalid hex color: {hex_str}") from e


def normalize_hex(hex_str: str) -> str:
    """Normalizes hex color string to uppercase #RRGGBB."""
    clean = hex_str.strip().lstrip("#")
    if len(clean) == 3:
        clean = "".join(c * 2 for c in clean)
    if len(clean) != 6:
        raise ValueError(f"Invalid hex color: {hex_str}")
    try:
        _ = int(clean, 16)
    except ValueError as e:
        raise ValueError(f"Invalid hex color: {hex_str}") from e
    return f"#{clean.upper()}"



def parse_box(box_str: str) -> Tuple[int, int, int, int]:
    """Parses 'x0,y0,x1,y1' string into integer coordinates."""
    parts = [int(p.strip()) for p in box_str.replace(" ", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise ValueError(f"Invalid bounding box '{box_str}', expected 'x0,y0,x1,y1'")
    return parts[0], parts[1], parts[2], parts[3]


def parse_seal_box(seal_str: str) -> Tuple[int, int, int, int, int]:
    """Parses 'x0,y0,x1,y1[:radius]' string into integer coordinates and closing radius."""
    if ":" in seal_str:
        coords_str, radius_str = seal_str.split(":", 1)
        radius = int(radius_str.strip())
    else:
        coords_str = seal_str
        radius = 20
    parts = [int(p.strip()) for p in coords_str.replace(" ", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise ValueError(f"Invalid seal box '{seal_str}', expected 'x0,y0,x1,y1[:radius]'")
    return parts[0], parts[1], parts[2], parts[3], radius


def parse_poly(poly_str: str) -> np.ndarray:
    """Parses 'x1,y1,x2,y2,x3,y3,...' string into an (N, 1, 2) integer vertex array."""
    parts = [int(p.strip()) for p in poly_str.replace(" ", ",").split(",") if p.strip()]
    if len(parts) < 6 or len(parts) % 2 != 0:
        raise ValueError(f"Invalid polygon '{poly_str}', expected at least 3 vertex pairs 'x1,y1,x2,y2,x3,y3,...'")
    return np.array(parts, dtype=np.int32).reshape((-1, 1, 2))


def parse_crop_spec(crop_str: str) -> Tuple[int, int, int, int, str]:
    """Parses 'x,y,w,h:label' or 'x0,y0,x1,y1:label' string."""
    if ":" in crop_str:
        coords_str, label = crop_str.split(":", 1)
        label = label.strip()
    else:
        coords_str, label = crop_str, "Manual Inspection Crop"

    parts = [int(p.strip()) for p in coords_str.replace(" ", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise ValueError(f"Invalid crop coords '{coords_str}', expected 'x,y,w,h' or 'x0,y0,x1,y1'")

    # If 3rd/4th parameters look like width/height (w < 800 and h < 800 and x1 > x0)
    # Check if x1 > x0: if x1 <= x0, likely w, h
    x0, y0, p2, p3 = parts
    if p2 > x0 and p3 > y0:
        # Specified as x0, y0, x1, y1
        x1, y1 = p2, p3
    else:
        # Specified as x0, y0, width, height
        x1, y1 = x0 + p2, y0 + p3

    return x0, y0, x1, y1, label


def clamp_box(x0: int, y0: int, x1: int, y1: int, w: int, h: int) -> Tuple[int, int, int, int]:
    """Clamps bounding box within image boundaries."""
    x0_c = max(0, min(w, x0))
    x1_c = max(0, min(w, x1))
    y0_c = max(0, min(h, y0))
    y1_c = max(0, min(h, y1))
    if x0_c > x1_c:
        x0_c, x1_c = x1_c, x0_c
    if y0_c > y1_c:
        y0_c, y1_c = y1_c, y0_c
    return x0_c, y0_c, x1_c, y1_c


# =============================================================================
# STAGE 1: Edge-Preserving CIELAB Chroma Smoothing
# =============================================================================

def apply_chroma_smoothing(
    img_rgb: np.ndarray,
    sigma_color: float = 12.0,
    sigma_space: float = 5.0,
) -> np.ndarray:
    """
    Applies bilateral filter strictly on CIELAB chroma channels (a*, b*).
    L* is untouched (0% modification) to keep 1px edges and fine linework razor-sharp
    while gently smoothing DCT/AI chroma blockiness and noise.
    """
    img_f32 = img_rgb.astype(np.float32)
    lab = cv2.cvtColor(img_f32, cv2.COLOR_RGB2LAB)
    L, a, b = cv2.split(lab)

    a_smooth = cv2.bilateralFilter(a, d=7, sigmaColor=sigma_color, sigmaSpace=sigma_space)
    b_smooth = cv2.bilateralFilter(b, d=7, sigmaColor=sigma_color, sigmaSpace=sigma_space)

    lab_smooth = cv2.merge([L, a_smooth, b_smooth])
    rgb_smooth = cv2.cvtColor(lab_smooth, cv2.COLOR_LAB2RGB).astype(np.float64)
    return np.clip(rgb_smooth, 0.0, 1.0)


# =============================================================================
# STAGE 2: Spatially Varying Background Field B(x, y)
# =============================================================================

def fit_robust_poly(A: np.ndarray, vals: np.ndarray, max_iter: int = 5) -> np.ndarray:
    """Iteratively Reweighted Least Squares (IRLS) polynomial fit with Huber loss."""
    weights = np.ones_like(vals)
    c = np.zeros(A.shape[1], dtype=np.float64)
    for _ in range(max_iter):
        Aw = A * np.sqrt(weights[:, None])
        vw = vals * np.sqrt(weights)
        c, _, _, _ = np.linalg.lstsq(Aw, vw, rcond=None)
        residuals = np.abs(A @ c - vals)
        med_res = np.median(residuals)
        k = 1.345 * (med_res * 1.4826 if med_res > 1e-6 else 1.0) + 1e-6
        weights = np.where(residuals <= k, 1.0, k / (residuals + 1e-6))
    return c


def fit_background_field(
    arr_rgb: np.ndarray,
    border_pct: float = 0.025,
    manual_bg_rgb: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, np.ndarray, float, float]:
    """
    Fits a continuous 2D degree-2 polynomial background field B(x, y) from image borders.
    Returns:
        B_field: (H, W, 3) continuous background color field.
        mean_B: (3,) mean background RGB.
        sigma_bg: residual noise floor std dev (in [0, 1]).
        p99_border_dE: 99th percentile CIELAB delta_E on inlier border pixels.
    """
    H, W, _ = arr_rgb.shape
    if manual_bg_rgb is not None:
        B_field = np.tile(manual_bg_rgb[None, None, :], (H, W, 1))
        mean_B = manual_bg_rgb.copy()
        return B_field, mean_B, 2.0 / 255.0, 6.0

    border_w = max(4, int(min(H, W) * border_pct))
    border_mask = np.zeros((H, W), dtype=bool)
    border_mask[:border_w, :] = True
    border_mask[-border_w:, :] = True
    border_mask[:, :border_w] = True
    border_mask[:, -border_w:] = True

    yy, xx = np.mgrid[0:H, 0:W]
    xs_all = xx[border_mask] / float(W)
    ys_all = yy[border_mask] / float(H)
    pixels_all = arr_rgb[border_mask]

    # Convert border pixels to CIELAB to reject foreground border intrusions via MAD
    pixels_f32 = pixels_all.astype(np.float32)
    lab_border = cv2.cvtColor(pixels_f32.reshape(-1, 1, 3), cv2.COLOR_RGB2LAB).reshape(-1, 3)
    med_lab = np.median(lab_border, axis=0)
    dev_lab = np.sqrt(np.sum((lab_border - med_lab) ** 2, axis=-1))

    med_dev = np.median(dev_lab)
    mad = np.median(np.abs(dev_lab - med_dev))
    mad_sigma = mad * 1.4826
    cutoff = float(np.clip(med_dev + 3.0 * mad_sigma + 4.0, 10.0, 32.0))
    inliers = dev_lab <= cutoff

    if np.sum(inliers) < 50:
        inliers = np.ones(len(pixels_all), dtype=bool)

    xs = xs_all[inliers]
    ys = ys_all[inliers]
    pixels_inliers = pixels_all[inliers]

    # Degree-2 2D polynomial: 1, x, y, x^2, x*y, y^2
    A = np.column_stack([np.ones_like(xs), xs, ys, xs**2, xs * ys, ys**2])
    coeffs = [fit_robust_poly(A, pixels_inliers[:, c]) for c in range(3)]

    X_full = xx / float(W)
    Y_full = yy / float(H)
    A_full = np.stack(
        [np.ones_like(X_full), X_full, Y_full, X_full**2, X_full * Y_full, Y_full**2],
        axis=-1,
    )

    B_field = np.zeros((H, W, 3), dtype=np.float64)
    for c in range(3):
        B_field[:, :, c] = np.sum(A_full * coeffs[c], axis=-1)
    B_field = np.clip(B_field, 0.0, 1.0)
    mean_B = np.mean(B_field, axis=(0, 1))

    # Noise residual statistics on inliers
    border_res = np.abs(pixels_inliers - B_field[border_mask][inliers])
    sigma_bg = float(np.median(border_res) * 1.4826)

    # Compute border CIELAB delta_E
    inlier_B = B_field[border_mask][inliers].astype(np.float32)
    lab_inlier_B = cv2.cvtColor(inlier_B.reshape(-1, 1, 3), cv2.COLOR_RGB2LAB).reshape(-1, 3)
    border_dE = np.sqrt(np.sum((lab_border[inliers] - lab_inlier_B) ** 2, axis=-1))
    p99_border_dE = float(np.percentile(border_dE, 99))

    return B_field, mean_B, sigma_bg, p99_border_dE


def compute_lab_delta_e(img_rgb: np.ndarray, bg_rgb: np.ndarray) -> np.ndarray:
    """Computes perceptual Euclidean CIELAB delta_E between two RGB images."""
    lab_I = cv2.cvtColor(img_rgb.astype(np.float32), cv2.COLOR_RGB2LAB)
    lab_B = cv2.cvtColor(bg_rgb.astype(np.float32), cv2.COLOR_RGB2LAB)
    return np.sqrt(np.sum((lab_I - lab_B) ** 2, axis=-1))


# =============================================================================
# STAGE 3: Topological Two-Threshold Trimap with Interior Protection
# =============================================================================

def build_topological_trimap(
    img_proc: np.ndarray,
    B_field: np.ndarray,
    delta_E: np.ndarray,
    delta_E_raw: Optional[np.ndarray] = None,
    t_bg: float = 6.5,
    t_fg: float = 40.0,
    t_thin: float = 32.0,
    unknown_band_px: float = 6.0,
    min_cutout_area: int = 6,
    protect_interior_boxes: Optional[List[Tuple[int, int, int, int]]] = None,
    protect_boxes: Optional[List[Tuple[int, int, int, int]]] = None,
    cutout_boxes: Optional[List[Tuple[int, int, int, int]]] = None,
    seal_boxes: Optional[List[Tuple[int, int, int, int, int]]] = None,
    protect_polys: Optional[List[np.ndarray]] = None,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, int]:
    """
    Constructs a topological trimap (0.0: definite bg, 0.5: unknown, 1.0: definite fg).
    Employs true bg_core interior protection, thin-stroke ridge preservation without Gibbs ringing,
    and declarative ROI overrides.
    Returns:
        trimap: (H, W) float64 array with values in {0.0, 0.5, 1.0}.
        true_bg_regions: (H, W) boolean mask of verified exterior and interior background.
        valid_cutout_mask: (H, W) boolean mask of interior cutouts.
        thin_ridges: (H, W) boolean mask of preserved 1-2px high-contrast ridges.
        dist_from_bg: (H, W) float32 distance transform array from true background.
        valid_cutout_count: number of accepted interior cutout holes.
    """
    H, W = delta_E.shape
    if delta_E_raw is None:
        delta_E_raw = delta_E
    dE_min = np.minimum(delta_E, delta_E_raw)
    protect_interior_boxes = protect_interior_boxes or []
    protect_boxes = protect_boxes or []
    cutout_boxes = cutout_boxes or []
    seal_boxes = seal_boxes or []
    protect_polys = protect_polys or []

    # 1. Candidate background seed
    cutout_pocket_mask = np.zeros((H, W), dtype=bool)
    for cb in cutout_boxes:
        cx0, cy0, cx1, cy1 = clamp_box(*cb, W, H)
        dE_b = delta_E[cy0:cy1, cx0:cx1]
        dE_raw_b = delta_E_raw[cy0:cy1, cx0:cx1]
        box_seed = (
            ((dE_b <= max(t_bg + 18.0, 26.0)) | (dE_raw_b <= max(t_bg + 20.0, 28.0)))
            & (dE_raw_b <= 38.0)
        )
        cutout_pocket_mask[cy0:cy1, cx0:cx1] |= box_seed

    bg_seed = (dE_min <= (t_bg + 3.0)) | cutout_pocket_mask

    # Topological outline gap sealing (--seal-box)
    barrier = ~bg_seed
    barrier_sealed = barrier.copy()
    for sb in seal_boxes:
        sx0, sy0, sx1, sy1, r = sb
        sx0, sy0, sx1, sy1 = clamp_box(sx0, sy0, sx1, sy1, W, H)
        px0, py0 = max(0, sx0 - r), max(0, sy0 - r)
        px1, py1 = min(W, sx1 + r), min(H, sy1 + r)
        roi = barrier[py0:py1, px0:px1].astype(np.uint8)
        k_seal = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
        closed_roi = cv2.morphologyEx(roi, cv2.MORPH_CLOSE, k_seal)
        ox0, oy0 = sx0 - px0, sy0 - py0
        ox1, oy1 = ox0 + (sx1 - sx0), oy0 + (sy1 - sy0)
        barrier_sealed[sy0:sy1, sx0:sx1] |= (closed_roi[oy0:oy1, ox0:ox1] > 0)

    # 2. 8-connected components labeling
    candidate_bg = ~barrier_sealed
    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
        candidate_bg.astype(np.uint8), connectivity=8
    )

    # Identify exterior background components (touching canvas perimeter)
    border_pixels = np.concatenate(
        [labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]]
    )
    border_label_ids = set(np.unique(border_pixels))
    border_label_ids.discard(0)

    exterior_bg = np.isin(labels, list(border_label_ids))

    # Evaluate genuine interior cutouts/counters
    valid_cutout_mask = np.zeros((H, W), dtype=bool)
    valid_cutout_count = 0

    for lbl in range(1, num_labels):
        if lbl in border_label_ids:
            continue

        area = stats[lbl, cv2.CC_STAT_AREA]
        cx, cy = centroids[lbl]

        # Check if component centroid falls within any --cutout-box ROI or overlaps it
        in_cutout_roi = any(
            cb[0] <= cx < cb[2] and cb[1] <= cy < cb[3] for cb in cutout_boxes
        )
        if not in_cutout_roi and cutout_boxes:
            c_left = stats[lbl, cv2.CC_STAT_LEFT]
            c_top = stats[lbl, cv2.CC_STAT_TOP]
            c_w = stats[lbl, cv2.CC_STAT_WIDTH]
            c_h = stats[lbl, cv2.CC_STAT_HEIGHT]
            c_right = c_left + c_w
            c_bottom = c_top + c_h
            for cb in cutout_boxes:
                if not (c_right <= cb[0] or c_left >= cb[2] or c_bottom <= cb[1] or c_top >= cb[3]):
                    if np.any(labels[max(c_top, cb[1]):min(c_bottom, cb[3]), max(c_left, cb[0]):min(c_right, cb[2])] == lbl):
                        in_cutout_roi = True
                        break

        area_thresh = 3 if in_cutout_roi else min_cutout_area
        de_thresh = max(t_bg + 18.0, 26.0) if in_cutout_roi else t_bg

        if area >= area_thresh:
            comp_idx = labels == lbl
            p25_dE = np.percentile(delta_E[comp_idx], 25)
            min_dE = np.min(delta_E[comp_idx])
            min_dE_raw = np.min(delta_E_raw[comp_idx])
            if (p25_dE <= de_thresh) or (in_cutout_roi and (min_dE <= de_thresh or min_dE_raw <= max(t_bg + 20.0, 28.0))):
                valid_cutout_mask[comp_idx] = True
                valid_cutout_count += 1

    # Suppress interior cutout mask inside any --seal-box ROI
    for sb in seal_boxes:
        sx0, sy0, sx1, sy1, _ = sb
        sx0, sy0, sx1, sy1 = clamp_box(sx0, sy0, sx1, sy1, W, H)
        valid_cutout_mask[sy0:sy1, sx0:sx1] = False

    # Seed validated cutout pockets from explicit --cutout-box
    if np.any(cutout_pocket_mask):
        valid_cutout_mask |= cutout_pocket_mask
        num_cutouts, _ = cv2.connectedComponents(valid_cutout_mask.astype(np.uint8))
        valid_cutout_count = max(valid_cutout_count, num_cutouts - 1)

    # Combine exterior bg and validated interior cutouts
    true_bg_regions = exterior_bg | valid_cutout_mask

    # Apply --protect-interior-box overrides:
    # Suppresses interior cutout holes inside box while preserving outer silhouette matting!
    for pib in protect_interior_boxes:
        x0, y0, x1, y1 = clamp_box(*pib, W, H)
        true_bg_regions[y0:y1, x0:x1] = (
            (true_bg_regions[y0:y1, x0:x1] & exterior_bg[y0:y1, x0:x1])
            | cutout_pocket_mask[y0:y1, x0:x1]
        )

    # Euclidean distance from verified background
    dist_from_bg = cv2.distanceTransform(
        (~true_bg_regions).astype(np.uint8), cv2.DIST_L2, 5
    )

    # Build Trimap:
    k3 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    k7 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    k11 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))
    local_max_dE = cv2.dilate(delta_E.astype(np.float32), k11)

    # 1. Definite Background (0.0): eroded true_bg_regions + tiny exterior bg islands
    sure_bg = cv2.erode(true_bg_regions.astype(np.uint8), k3, iterations=1).astype(bool)

    # Ensure every valid cutout component has at least one pixel anchored in sure_bg
    for lbl in range(1, num_labels):
        comp_idx = (labels == lbl) & valid_cutout_mask
        if np.any(comp_idx) and not np.any(sure_bg & comp_idx):
            comp_dE = np.where(comp_idx, delta_E, 1e9)
            min_y, min_x = np.unravel_index(np.argmin(comp_dE), comp_dE.shape)
            sure_bg[min_y, min_x] = True

    # Tiny exterior background islands far from high-contrast foreground
    fg_high = (
        (delta_E >= t_fg)
        & (delta_E >= 0.72 * local_max_dE)
        & (~true_bg_regions)
    ).astype(np.uint8)
    dist_from_fg_high = cv2.distanceTransform((~fg_high).astype(np.uint8), cv2.DIST_L2, 5)
    exterior_pure_bg = true_bg_regions & (delta_E <= t_bg * 0.85) & (dist_from_fg_high >= 2.0)
    sure_bg |= exterior_pure_bg

    # Seed sure_bg directly from cutout pockets
    sure_bg |= cutout_pocket_mask

    # 2. Definite Foreground (1.0):
    # 2a. High-contrast eroded foreground with 7x7 elliptical kernel (radius 3px)
    fg_eroded = cv2.erode(fg_high, k7, iterations=1).astype(bool)

    # 2b. Deep topological interior via geodesically connected potential background
    pot_bg_thresh = min(22.0, max(t_bg + 2.0, 0.55 * t_fg))
    potential_bg_raw = (dE_min <= pot_bg_thresh) | true_bg_regions
    num_pot, pot_labels = cv2.connectedComponents(
        potential_bg_raw.astype(np.uint8), connectivity=8
    )
    overlapping_labels = np.unique(pot_labels[true_bg_regions])
    overlapping_labels = overlapping_labels[overlapping_labels > 0]
    connected_potential_bg = np.isin(pot_labels, overlapping_labels)

    dist_from_potential_bg = cv2.distanceTransform(
        (~connected_potential_bg).astype(np.uint8), cv2.DIST_L2, 5
    )
    deep_interior = (dist_from_potential_bg > unknown_band_px) & (~true_bg_regions)

    # 2c. Thin 1-2px stroke/filigree ridge preservation:
    # Strict 1D directional peak on x or y:
    dE_pad = np.pad(delta_E, 1, mode="edge")
    is_peak_x = (
        (delta_E >= dE_pad[1:-1, :-2])
        & (delta_E >= dE_pad[1:-1, 2:])
        & ((delta_E > dE_pad[1:-1, :-2]) | (delta_E > dE_pad[1:-1, 2:]))
    )
    is_peak_y = (
        (delta_E >= dE_pad[:-2, 1:-1])
        & (delta_E >= dE_pad[2:, 1:-1])
        & ((delta_E > dE_pad[:-2, 1:-1]) | (delta_E > dE_pad[2:, 1:-1]))
    )
    max_dE = maximum_filter(delta_E, size=3)
    is_ridge = (is_peak_x | is_peak_y) & (delta_E >= (max_dE - 1.0))

    # Polarity-invariant local peak vector alignment:
    lab_I = cv2.cvtColor(img_proc.astype(np.float32), cv2.COLOR_RGB2LAB)
    lab_B = cv2.cvtColor(B_field.astype(np.float32), cv2.COLOR_RGB2LAB)
    L_diff = np.abs(lab_I[:, :, 0] - lab_B[:, :, 0])
    chroma_diff = np.sqrt(
        (lab_I[:, :, 1] - lab_B[:, :, 1]) ** 2 + (lab_I[:, :, 2] - lab_B[:, :, 2]) ** 2
    )

    diff_IB = (img_proc - B_field).astype(np.float32)
    w_fg = (np.maximum(delta_E - t_bg, 0.0) ** 2)[:, :, np.newaxis].astype(np.float32)
    v_dom = cv2.GaussianBlur(w_fg * diff_IB, (9, 9), 2.5)
    dot_dom = np.sum(diff_IB * v_dom, axis=-1)

    valid_stroke_contrast = ((L_diff > 6.0) | (chroma_diff > 10.0)) & (dot_dom > 0.0)

    thin_ridges = (
        is_ridge
        & (delta_E > t_thin)
        & valid_stroke_contrast
        & (~true_bg_regions)
    )

    border_aa_guard = (
        (dist_from_bg <= max(2.5, 0.5 * unknown_band_px))
        & (delta_E < 0.85 * local_max_dE)
    )

    sure_fg = (fg_eroded | deep_interior) & (~sure_bg) & (~border_aa_guard)

    # Assemble trimap
    trimap = np.full((H, W), 0.5, dtype=np.float64)
    trimap[sure_bg] = 0.0
    trimap[sure_fg] = 1.0

    # Reachability-Bounded Interior Foreground Promotion:
    # Any enclosed interior fill inside --protect-interior-box or --seal-box that is blocked
    # from true background by fg_wall is promoted to 1.0 (Sure Foreground) while preserving outer anti-aliasing.
    wall_thresh = min(16.0, max(t_bg + 4.0, 0.35 * t_fg))
    fg_wall = ((delta_E >= wall_thresh) & (delta_E_raw > (t_bg + 2.5))).copy()

    for sb in seal_boxes:
        sx0, sy0, sx1, sy1, r = sb
        sx0, sy0, sx1, sy1 = clamp_box(sx0, sy0, sx1, sy1, W, H)
        px0, py0 = max(0, sx0 - r), max(0, sy0 - r)
        px1, py1 = min(W, sx1 + r), min(H, sy1 + r)
        roi = fg_wall[py0:py1, px0:px1].astype(np.uint8)
        k_seal = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
        closed_roi = cv2.morphologyEx(roi, cv2.MORPH_CLOSE, k_seal)
        ox0, oy0 = sx0 - px0, sy0 - py0
        ox1, oy1 = ox0 + (sx1 - sx0), oy0 + (sy1 - sy0)
        fg_wall[sy0:sy1, sx0:sx1] |= (closed_roi[oy0:oy1, ox0:ox1] > 0)

    for poly in protect_polys:
        poly_mask = np.zeros((H, W), dtype=np.uint8)
        cv2.fillPoly(poly_mask, [poly], 1)
        fg_wall[poly_mask > 0] = True

    open_or_bg = (~fg_wall) | true_bg_regions
    num_open, open_labels = cv2.connectedComponents(
        open_or_bg.astype(np.uint8), connectivity=8
    )
    bg_labels = np.unique(open_labels[true_bg_regions])
    bg_labels = bg_labels[bg_labels > 0]
    reachable_from_bg = np.isin(open_labels, bg_labels)

    in_protected_or_sealed = np.zeros((H, W), dtype=bool)
    for pib in protect_interior_boxes:
        x0, y0, x1, y1 = clamp_box(*pib, W, H)
        in_protected_or_sealed[y0:y1, x0:x1] = True
    for sb in seal_boxes:
        x0, y0, x1, y1, _ = sb
        x0, y0, x1, y1 = clamp_box(x0, y0, x1, y1, W, H)
        in_protected_or_sealed[y0:y1, x0:x1] = True

    enclosed_interior_fg = (
        (~reachable_from_bg)
        & (~true_bg_regions)
        & in_protected_or_sealed
        & (dist_from_bg > max(2.5, 0.5 * unknown_band_px))
        & (~border_aa_guard)
    )
    enclosed_interior_fg &= (~cutout_pocket_mask)

    trimap[enclosed_interior_fg] = 1.0

    # Thin stroke rescue: promote 0.0 -> 0.5 (never 1.0) so thin strokes unmix cleanly
    thin_mask = thin_ridges
    trimap[(thin_mask > 0) & (trimap == 0.0)] = 0.5

    # Ensure anti-aliased border pixels near background stay unknown (0.5)
    trimap[border_aa_guard & (trimap == 1.0)] = 0.5

    # 3. Apply explicit --protect-box overrides: force trimap = 1.0
    for pb in protect_boxes:
        x0, y0, x1, y1 = clamp_box(*pb, W, H)
        trimap[y0:y1, x0:x1] = 1.0
        true_bg_regions[y0:y1, x0:x1] = False

    # Apply explicit --protect-poly overrides: force trimap = 1.0
    for poly in protect_polys:
        poly_mask = np.zeros((H, W), dtype=np.uint8)
        cv2.fillPoly(poly_mask, [poly], 1)
        in_poly = poly_mask > 0
        trimap[in_poly] = 1.0
        true_bg_regions[in_poly] = False

    # 4. Explicit --cutout-box sure_bg seeding with anti-aliasing transition ring
    if np.any(cutout_pocket_mask):
        trimap[cutout_pocket_mask] = 0.0
        sure_bg[cutout_pocket_mask] = True
        true_bg_regions[cutout_pocket_mask] = True

        dist_to_pocket = cv2.distanceTransform(
            (~cutout_pocket_mask).astype(np.uint8), cv2.DIST_L2, 5
        )
        transition_ring = (dist_to_pocket <= 2.0) & (~cutout_pocket_mask)
        demote_mask = transition_ring & (trimap == 1.0)
        trimap[demote_mask] = 0.5

    return trimap, true_bg_regions, valid_cutout_mask, thin_ridges, dist_from_bg, valid_cutout_count


# =============================================================================
# STAGE 4: Closed-Form Laplacian Alpha Matting + Known-B Color-Line Unmixing
# =============================================================================

def run_pymatting_unmixing(
    img_proc: np.ndarray,
    trimap: np.ndarray,
    B_field: np.ndarray,
    delta_E: Optional[np.ndarray] = None,
    t_bg: float = 6.5,
    verbose: bool = False,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """
    Executes Two-Stage PyMatting + Known-B vector color-line unmixing.
    Returns:
        F_final: (H, W, 3) decontaminated foreground colors in [0, 1].
        alpha_final: (H, W) continuous alpha matte in [0, 1].
        F_init: (H, W, 3) initial ML foreground estimate.
        elapsed_sec: execution time in seconds.
    """
    t0 = time.perf_counter()

    # Numerical boundary guards [NUM-01]
    n_bg = np.sum(trimap == 0.0)
    n_fg = np.sum(trimap == 1.0)
    n_unk = np.sum(trimap == 0.5)

    if n_fg == 0:
        return np.zeros_like(img_proc), np.zeros(trimap.shape, dtype=np.float64), np.zeros_like(img_proc), 0.0
    if n_bg == 0:
        return img_proc.copy(), np.ones(trimap.shape, dtype=np.float64), img_proc.copy(), 0.0
    if n_unk == 0:
        alpha_final = trimap.copy()
        F_final = img_proc.copy()
        F_final[alpha_final == 0.0] = 0.0
        return F_final, alpha_final, img_proc.copy(), 0.0

    # Step 4a: Closed-form Laplacian alpha estimation
    if verbose:
        print("  [Stage 4a] Computing Closed-Form Laplacian alpha matte...")
    alpha_cf = pymatting.estimate_alpha_cf(img_proc, trimap)

    # Step 4b: Stage-1 continuous foreground color field
    if verbose:
        print("  [Stage 4b] Estimating initial foreground field F_init...")
    F_init = pymatting.estimate_foreground_ml(img_proc, alpha_cf)
    F_init = np.clip(F_init, 0.0, 1.0)

    # Step 4c: Known-B vector color-line projection on Unknown band
    if verbose:
        print("  [Stage 4c] Computing Known-B vector color-line projection...")
    unknown = trimap == 0.5
    v_fb = F_init - B_field
    v_ib = img_proc - B_field
    dot_num = np.sum(v_ib * v_fb, axis=-1)
    denom = np.sum(v_fb * v_fb, axis=-1) + 1e-4
    alpha_proj = np.clip(dot_num / denom, 0.0, 1.0)

    # F_init - B contrast norm
    norm_fb = np.sqrt(np.maximum(denom - 1e-4, 0.0))

    # [MEM-01] Immediately free projection intermediaries
    del v_fb, v_ib, dot_num, denom

    alpha_final = trimap.copy()
    alpha_final[unknown] = alpha_cf[unknown]

    # For unknown band where foreground-background contrast is distinct (||v_fb|| > 0.06):
    # take minimum(alpha_cf, alpha_proj) to eliminate Gibbs ringing and bleeding
    use_proj = unknown & (norm_fb > 0.06)
    alpha_final[use_proj] = np.minimum(alpha_cf[use_proj], alpha_proj[use_proj])

    # Soft-foot noise suppression on tiny background noise residuals [ALG-01]
    if delta_E is not None:
        feather_mask = unknown & (delta_E <= t_bg) & (alpha_final < 0.12)
        if np.any(feather_mask):
            t = alpha_final[feather_mask] / 0.12
            alpha_final[feather_mask] = t * t * (3.0 - 2.0 * t) * alpha_final[feather_mask]
            alpha_final = np.where(alpha_final < 0.01, 0.0, alpha_final)

    # Step 4d: Clean alpha floor / ceiling
    alpha_final[alpha_final < 0.02] = 0.0
    alpha_final[alpha_final > 0.985] = 1.0
    alpha_final[trimap == 0.0] = 0.0

    # Prune tiny isolated low-opacity exterior noise specks (area <= 8, max_alpha < 0.55)
    speck_mask = (alpha_final > 0.0).astype(np.uint8)
    num_specks, speck_labels, speck_stats, _ = cv2.connectedComponentsWithStats(
        speck_mask, connectivity=8
    )
    for s_idx in range(1, num_specks):
        if speck_stats[s_idx, cv2.CC_STAT_AREA] <= 8:
            s_pixels = speck_labels == s_idx
            if np.max(alpha_final[s_pixels]) < 0.55:
                alpha_final[s_pixels] = 0.0

    # Step 4e: Stage-2 final foreground decontamination
    if verbose:
        print("  [Stage 4e] Performing Stage-2 foreground decontamination F_final...")
    if np.max(alpha_final) == 0.0:
        F_final = np.zeros_like(img_proc)
    else:
        F_final = pymatting.estimate_foreground_ml(img_proc, alpha_final)
        F_final = np.clip(F_final, 0.0, 1.0)
        # Zero out RGB where alpha is zero for optimal PNG compression
        F_final[alpha_final == 0.0] = 0.0

    elapsed_sec = time.perf_counter() - t0
    return F_final, alpha_final, F_init, elapsed_sec


# =============================================================================
# STAGE 5: Strictly Decoupled Post-Processing
# =============================================================================

def postprocess_mode(
    F_final: np.ndarray,
    alpha_final: np.ndarray,
    mode: str = "dtg",
    dtf_alpha_floor: float = 0.15,
    colors: int = 8,
    explicit_palette: Optional[List[np.ndarray]] = None,
    verbose: bool = False,
) -> Tuple[np.ndarray, np.ndarray, Optional[List[str]]]:
    """
    Applies decoupled printing technology post-processing:
    - 'dtg': Continuous F_final and alpha_final (ZERO quantization).
    - 'dtf': Continuous F_final with soft Hermite alpha floor ramp (< 15%).
    - 'screenprint': Weighted K-means palette quantization + binary alpha (>= 0.5).
    """
    F_out = F_final.copy()
    alpha_out = alpha_final.copy()
    palette_hex_list: Optional[List[str]] = None

    if mode == "dtg":
        # Keep continuous unmixed output
        pass

    elif mode == "dtf":
        # Firm alpha floor with soft Hermite ramp to prevent powdery adhesive scatter
        floor = max(0.01, min(0.5, dtf_alpha_floor))
        low_vapor = (alpha_out > 0.0) & (alpha_out < floor)
        t = alpha_out[low_vapor] / floor
        alpha_out[low_vapor] = t * t * (3.0 - 2.0 * t) * alpha_out[low_vapor]
        alpha_out[alpha_out < 0.02] = 0.0
        F_out[alpha_out == 0.0] = 0.0

    elif mode == "screenprint":
        if verbose:
            print("  [Stage 5] Running decoupled spot-color quantization for screen printing...")

        fg_mask = alpha_out >= 0.5
        if np.sum(fg_mask) == 0:
            return np.zeros_like(F_out), np.zeros_like(alpha_out), []
        solid_mask = alpha_out >= 0.8
        if np.sum(solid_mask) < colors * 20:
            solid_mask = fg_mask

        solid_pixels = F_out[solid_mask]

        if explicit_palette is not None and len(explicit_palette) > 0:
            palette_rgb = np.array(explicit_palette, dtype=np.float64)
        else:
            n_clust = min(colors, len(solid_pixels), max(1, len(solid_pixels) // 50))
            km = KMeans(n_clusters=n_clust, random_state=42, n_init=10).fit(solid_pixels)
            # Refine cluster centers to median RGB of inliers
            palette_list = []
            for k in range(n_clust):
                k_pix = solid_pixels[km.labels_ == k]
                if len(k_pix) > 0:
                    palette_list.append(np.median(k_pix, axis=0))
                else:
                    palette_list.append(km.cluster_centers_[k])
            palette_rgb = np.array(palette_list, dtype=np.float64)

        # Snap foreground pixels (alpha >= 0.5) to nearest palette color in CIELAB
        fg_pixels = F_out[fg_mask].astype(np.float32)
        lab_fg = cv2.cvtColor(fg_pixels.reshape(-1, 1, 3), cv2.COLOR_RGB2LAB).reshape(-1, 3)
        lab_pal = cv2.cvtColor(palette_rgb.astype(np.float32).reshape(-1, 1, 3), cv2.COLOR_RGB2LAB).reshape(-1, 3)

        # Pairwise distance (N_fg, K)
        dists = np.sqrt(np.sum((lab_fg[:, None, :] - lab_pal[None, :, :]) ** 2, axis=-1))
        best_pal_idx = np.argmin(dists, axis=-1)

        F_out[fg_mask] = palette_rgb[best_pal_idx]

        # Screenprint binary/halftone-ready alpha
        alpha_out = np.where(fg_mask, 1.0, 0.0)
        F_out[alpha_out == 0.0] = 0.0

        palette_hex_list = [rgb_to_hex(c) for c in palette_rgb]

    return F_out, alpha_out, palette_hex_list


# =============================================================================
# VECTOR CUTOUT PREPRESS WORKFLOW (Real-ESRGAN + Edge-Snap + VTracer)
# =============================================================================

def resolve_realesrgan_bin(verbose: bool = False) -> str:
    """
    Auto-resolves path to realesrgan-ncnn-vulkan executable:
    1. System PATH
    2. ~/.cache/pixelpress/realesrgan-ncnn-vulkan/realesrgan-ncnn-vulkan
    3. Auto-downloads and extracts official v0.2.5.0 Linux binary release to cache dir.
    """
    bin_in_path = shutil.which("realesrgan-ncnn-vulkan")
    if bin_in_path and os.path.isfile(bin_in_path) and os.access(bin_in_path, os.X_OK):
        return bin_in_path

    cache_parent = os.path.expanduser("~/.cache/pixelpress")
    cache_dir = os.path.join(cache_parent, "realesrgan-ncnn-vulkan")
    lock_path = os.path.join(cache_parent, "realesrgan.lock")

    os.makedirs(cache_parent, exist_ok=True)
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            cached_bin = os.path.join(cache_dir, "realesrgan-ncnn-vulkan")
            cached_models = os.path.join(cache_dir, "models")
            if (
                os.path.isfile(cached_bin)
                and os.access(cached_bin, os.X_OK)
                and os.path.isdir(cached_models)
            ):
                return cached_bin

            if verbose:
                print(f"realesrgan-ncnn-vulkan binary not found. Downloading official v0.2.5.0 Linux release to {cache_dir}...")

            zip_url = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-ubuntu.zip"
            with tempfile.TemporaryDirectory(dir=cache_parent) as tmp_extract_dir:
                zip_path = os.path.join(tmp_extract_dir, "realesrgan.zip")
                urllib.request.urlretrieve(zip_url, zip_path)

                with zipfile.ZipFile(zip_path, "r") as zip_ref:
                    extract_root = os.path.abspath(tmp_extract_dir)
                    for member in zip_ref.infolist():
                        member_path = os.path.abspath(os.path.join(tmp_extract_dir, member.filename))
                        if not member_path.startswith(extract_root + os.sep) and member_path != extract_root:
                            raise RuntimeError(f"Zip-Slip vulnerability detected in member: {member.filename}")
                        zip_ref.extract(member, tmp_extract_dir)

                extracted_bin = os.path.join(tmp_extract_dir, "realesrgan-ncnn-vulkan")
                extracted_models = os.path.join(tmp_extract_dir, "models")

                if not (os.path.isfile(extracted_bin) and os.path.isdir(extracted_models)):
                    raise RuntimeError(
                        f"Expected realesrgan-ncnn-vulkan and models/ in extracted archive, but they were missing in {tmp_extract_dir}"
                    )

                h = hashlib.sha256()
                with open(extracted_bin, "rb") as f:
                    while chunk := f.read(65536):
                        h.update(chunk)
                computed_sha256 = h.hexdigest().lower()
                if computed_sha256 != EXPECTED_REALESRGAN_BIN_SHA256.lower():
                    raise RuntimeError(
                        f"SHA-256 checksum mismatch for realesrgan-ncnn-vulkan: expected {EXPECTED_REALESRGAN_BIN_SHA256}, got {computed_sha256}"
                    )

                os.chmod(extracted_bin, 0o755)

                staging_dir = os.path.join(tmp_extract_dir, "staged")
                os.makedirs(staging_dir, exist_ok=True)
                shutil.move(extracted_bin, os.path.join(staging_dir, "realesrgan-ncnn-vulkan"))
                shutil.move(extracted_models, os.path.join(staging_dir, "models"))

                if os.path.exists(cache_dir):
                    shutil.rmtree(cache_dir)
                shutil.move(staging_dir, cache_dir)

            if os.path.isfile(cached_bin) and os.access(cached_bin, os.X_OK) and os.path.isdir(cached_models):
                return cached_bin

            raise FileNotFoundError(
                f"Unable to resolve or auto-download realesrgan-ncnn-vulkan binary into {cache_dir}"
            )
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


def resolve_vtracer_bin(custom_path: Optional[str] = None) -> str:
    """
    Auto-resolves path to vtracer executable:
    1. Explicit custom_path if provided
    2. System PATH
    3. ~/.cargo/bin/vtracer
    """
    if custom_path:
        expanded = os.path.expanduser(custom_path)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
        raise FileNotFoundError(f"Specified vtracer binary not found or not executable: {custom_path}")

    bin_in_path = shutil.which("vtracer")
    if bin_in_path and os.path.isfile(bin_in_path) and os.access(bin_in_path, os.X_OK):
        return bin_in_path

    cargo_bin = os.path.expanduser("~/.cargo/bin/vtracer")
    if os.path.isfile(cargo_bin) and os.access(cargo_bin, os.X_OK):
        return cargo_bin

    raise FileNotFoundError(
        "vtracer binary not found on PATH or ~/.cargo/bin/vtracer. Please install via 'cargo install vtracer'."
    )


def run_esrgan_upscale_2x(
    pil_in: Image.Image,
    model_name: str,
    target_w: int,
    target_h: int,
    verbose: bool = False,
) -> Image.Image:
    """
    Upscales/denoises an image to (target_w, target_h) = (2*W, 2*H) using Real-ESRGAN Vulkan.
    - realesrgan-x4plus-anime: runs with -s 4, then downsamples 0.5x Lanczos to 2x.
    - realesr-animevideov3: runs with -s 2 directly to 2x.
    - none: performs Lanczos 2x upscale directly.
    """
    if model_name == "none":
        if verbose:
            print(f"Skipping Real-ESRGAN (model='none'). Performing 2x Lanczos upscale to {target_w}x{target_h}...")
        return pil_in.resize((target_w, target_h), Image.Resampling.LANCZOS)

    try:
        bin_path = resolve_realesrgan_bin(verbose=verbose)
        candidate_models = os.path.join(os.path.dirname(os.path.realpath(bin_path)), "models")
        fallback_models = os.path.expanduser("~/.cache/pixelpress/realesrgan-ncnn-vulkan/models")
        if os.path.isdir(candidate_models):
            models_dir = candidate_models
        elif os.path.isdir(fallback_models):
            models_dir = fallback_models
        else:
            models_dir = candidate_models

        with tempfile.TemporaryDirectory() as td:
            temp_in = os.path.join(td, "esrgan_input.png")
            temp_out = os.path.join(td, "esrgan_output.png")
            pil_in.save(temp_in)

            if model_name == "realesrgan-x4plus-anime":
                cmd = [
                    bin_path,
                    "-i", temp_in,
                    "-o", temp_out,
                    "-n", "realesrgan-x4plus-anime",
                    "-s", "4",
                    "-m", models_dir,
                ]
                if verbose:
                    print(f"Executing Real-ESRGAN: {' '.join(cmd)}")
                t0 = time.perf_counter()
                subprocess.run(cmd, check=True, timeout=120, capture_output=True, text=True)
                if verbose:
                    print(f"Real-ESRGAN 4x inference finished in {time.perf_counter() - t0:.2f}s. Downscaling 0.5x Lanczos to {target_w}x{target_h}...")
                img_4x = Image.open(temp_out).convert("RGB")
                img_2x = img_4x.resize((target_w, target_h), Image.Resampling.LANCZOS)
                return img_2x

            elif model_name == "realesr-animevideov3":
                cmd = [
                    bin_path,
                    "-i", temp_in,
                    "-o", temp_out,
                    "-n", "realesr-animevideov3",
                    "-s", "2",
                    "-m", models_dir,
                ]
                if verbose:
                    print(f"Executing Real-ESRGAN: {' '.join(cmd)}")
                t0 = time.perf_counter()
                subprocess.run(cmd, check=True, timeout=120, capture_output=True, text=True)
                if verbose:
                    print(f"Real-ESRGAN 2x inference finished in {time.perf_counter() - t0:.2f}s...")
                img_2x = Image.open(temp_out).convert("RGB")
                if img_2x.size != (target_w, target_h):
                    img_2x = img_2x.resize((target_w, target_h), Image.Resampling.LANCZOS)
                return img_2x
            else:
                raise ValueError(f"Unknown esrgan_model: {model_name}")

    except Exception as exc:
        err_msg = str(exc)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            err_msg = f"{exc}: {exc.stderr.strip()}"
        sys.stderr.write(
            f"Warning: Real-ESRGAN upscale failed ({err_msg}). Falling back gracefully to 2x Lanczos.\n"
        )
        return pil_in.resize((target_w, target_h), Image.Resampling.LANCZOS)


SVG_CMD_RE = re.compile(r"([MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?)")
SVG_TRANSLATE_RE = re.compile(
    r"translate\(\s*([-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?)(?:[\s,]+([-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?))?\s*\)"
)


def parse_svg_path_vertices(d_str: str, tx: float = 0.0, ty: float = 0.0) -> Tuple[List[float], List[float]]:
    """
    Tokenizes SVG command letters (MmLlHhVvCcSsQqTtAaZz) and numbers supporting scientific notation,
    tracks current pen (cx, cy) and subpath start (sx, sy), handles implicit repeated command tuples cleanly
    (including H/h 1-arg, V/v 1-arg, C/c 6-arg, S/s 4-arg, Q/q 4-arg, T/t 2-arg, A/a 7-arg where indices 5,6
    are target x,y), and returns absolute canvas (xs, ys) coordinates.
    """
    tokens = SVG_CMD_RE.findall(d_str)
    if not tokens:
        return [], []

    xs: List[float] = []
    ys: List[float] = []

    cx, cy = 0.0, 0.0
    sx, sy = 0.0, 0.0

    i = 0
    n = len(tokens)
    cmd = ""

    def is_cmd(tok: str) -> bool:
        return len(tok) == 1 and tok in "MmLlHhVvCcSsQqTtAaZz"

    while i < n:
        tok = tokens[i]
        if is_cmd(tok):
            cmd = tok
            i += 1
        elif not cmd:
            i += 1
            continue

        if cmd in ("Z", "z"):
            cx, cy = sx, sy
            xs.append(cx + tx)
            ys.append(cy + ty)
            continue

        if cmd in ("M", "m"):
            if i + 1 >= n or is_cmd(tokens[i]) or is_cmd(tokens[i + 1]):
                i += 1
                continue
            x_val, y_val = float(tokens[i]), float(tokens[i + 1])
            i += 2
            if cmd == "M":
                cx = x_val
                cy = y_val
            else:
                cx += x_val
                cy += y_val
            sx, sy = cx, cy
            xs.append(cx + tx)
            ys.append(cy + ty)
            cmd = "L" if cmd == "M" else "l"
            continue

        if cmd in ("L", "l"):
            if i + 1 >= n or is_cmd(tokens[i]) or is_cmd(tokens[i + 1]):
                i += 1
                continue
            x_val, y_val = float(tokens[i]), float(tokens[i + 1])
            i += 2
            if cmd == "L":
                cx = x_val
                cy = y_val
            else:
                cx += x_val
                cy += y_val
            xs.append(cx + tx)
            ys.append(cy + ty)
            continue

        if cmd in ("H", "h"):
            if i >= n or is_cmd(tokens[i]):
                i += 1
                continue
            x_val = float(tokens[i])
            i += 1
            if cmd == "H":
                cx = x_val
            else:
                cx += x_val
            xs.append(cx + tx)
            ys.append(cy + ty)
            continue

        if cmd in ("V", "v"):
            if i >= n or is_cmd(tokens[i]):
                i += 1
                continue
            y_val = float(tokens[i])
            i += 1
            if cmd == "V":
                cy = y_val
            else:
                cy += y_val
            xs.append(cx + tx)
            ys.append(cy + ty)
            continue

        if cmd in ("C", "c"):
            if i + 5 >= n or any(is_cmd(tokens[i + k]) for k in range(6)):
                i += 1
                continue
            x_val, y_val = float(tokens[i + 4]), float(tokens[i + 5])
            i += 6
            if cmd == "C":
                cx = x_val
                cy = y_val
            else:
                cx += x_val
                cy += y_val
            xs.append(cx + tx)
            ys.append(cy + ty)
            continue

        if cmd in ("S", "s"):
            if i + 3 >= n or any(is_cmd(tokens[i + k]) for k in range(4)):
                i += 1
                continue
            x_val, y_val = float(tokens[i + 2]), float(tokens[i + 3])
            i += 4
            if cmd == "S":
                cx = x_val
                cy = y_val
            else:
                cx += x_val
                cy += y_val
            xs.append(cx + tx)
            ys.append(cy + ty)
            continue

        if cmd in ("Q", "q"):
            if i + 3 >= n or any(is_cmd(tokens[i + k]) for k in range(4)):
                i += 1
                continue
            x_val, y_val = float(tokens[i + 2]), float(tokens[i + 3])
            i += 4
            if cmd == "Q":
                cx = x_val
                cy = y_val
            else:
                cx += x_val
                cy += y_val
            xs.append(cx + tx)
            ys.append(cy + ty)
            continue

        if cmd in ("T", "t"):
            if i + 1 >= n or is_cmd(tokens[i]) or is_cmd(tokens[i + 1]):
                i += 1
                continue
            x_val, y_val = float(tokens[i]), float(tokens[i + 1])
            i += 2
            if cmd == "T":
                cx = x_val
                cy = y_val
            else:
                cx += x_val
                cy += y_val
            xs.append(cx + tx)
            ys.append(cy + ty)
            continue

        if cmd in ("A", "a"):
            if i + 6 >= n or any(is_cmd(tokens[i + k]) for k in range(7)):
                i += 1
                continue
            x_val, y_val = float(tokens[i + 5]), float(tokens[i + 6])
            i += 7
            if cmd == "A":
                cx = x_val
                cy = y_val
            else:
                cx += x_val
                cy += y_val
            xs.append(cx + tx)
            ys.append(cy + ty)
            continue

        i += 1

    return xs, ys


def clean_vtracer_svg(
    raw_svg_path: str,
    out_svg_path: str,
    w_orig: int,
    h_orig: int,
    w_2x: int,
    h_2x: int,
    bg_rgb: np.ndarray,
    ink_rgbs: List[np.ndarray],
    canonical_hexes: List[str],
    protect_interior_boxes_2x: Optional[List[Tuple[int, int, int, int]]] = None,
    protect_boxes_2x: Optional[List[Tuple[int, int, int, int]]] = None,
    cutout_boxes_2x: Optional[List[Tuple[int, int, int, int]]] = None,
    protect_polys_2x: Optional[List[np.ndarray]] = None,
    verbose: bool = False,
) -> int:
    """
    Cleans VTracer SVG output:
    1. Sets native print dimensions width="{w_orig}", height="{h_orig}", and viewBox="0 0 {w_2x} {h_2x}".
    2. Strips every <rect> and <path> whose fill RGB is closer to background B than to any foreground ink C_k
       (unless explicitly protected within protect_boxes_2x or protect_interior_boxes_2x).
    3. Snaps every remaining foreground <path> fill attribute to the exact canonical uppercase hex of its
       nearest spot ink C_k, guaranteeing at most K unique spot-color hexes in the SVG (zero color fragmentation!).
    Returns total count of removed true interior negative-space cutout elements.
    """
    with open(raw_svg_path, "r", encoding="utf-8") as f:
        svg_text = f.read()

    if re.search(r"<!\s*(?:doctype|entity)\b", svg_text, re.IGNORECASE):
        raise ValueError("Potentially malicious SVG: DTD / Entity declarations are forbidden.")

    try:
        ET.register_namespace("", "http://www.w3.org/2000/svg")
        root = ET.fromstring(svg_text)
        tree = ET.ElementTree(root)
    except ET.ParseError as pe:
        raise ValueError(f"Failed to parse SVG XML from {raw_svg_path}: {pe}") from pe

    root.attrib["width"] = str(w_orig)
    root.attrib["height"] = str(h_orig)
    root.attrib["viewBox"] = f"0 0 {w_2x} {h_2x}"

    protect_boxes = (protect_interior_boxes_2x or []) + (protect_boxes_2x or [])
    cutout_boxes = cutout_boxes_2x or []
    protect_polygons = protect_polys_2x or []

    exterior_removed = 0
    interior_cutouts_removed = 0

    def get_fill_hex(elem: ET.Element) -> Optional[str]:
        fill = elem.attrib.get("fill", "")
        if not fill and "style" in elem.attrib:
            for part in elem.attrib["style"].split(";"):
                if part.strip().startswith("fill:"):
                    fill = part.strip().split(":")[1].strip()
                    break
        if fill and fill.startswith("#"):
            return fill
        return None

    def snap_elem_fill(elem: ET.Element, ink_idx: int) -> None:
        target_hex = canonical_hexes[ink_idx]
        elem.attrib["fill"] = target_hex
        if "style" in elem.attrib:
            parts = [p.strip() for p in elem.attrib["style"].split(";") if p.strip()]
            new_parts = [p for p in parts if not p.startswith("fill:")]
            if new_parts:
                elem.attrib["style"] = ";".join(new_parts)
            else:
                del elem.attrib["style"]

    for parent in root.iter():
        to_remove = []
        for child in list(parent):
            tag = child.tag.split("}")[-1]
            if tag not in ("path", "rect", "polygon"):
                continue

            fill_str = get_fill_hex(child)

            if tag == "rect":
                w_attr = child.attrib.get("width", "")
                h_attr = child.attrib.get("height", "")
                is_full_canvas = (
                    ("%" in w_attr or str(w_2x) in w_attr or str(w_orig) in w_attr)
                    and ("%" in h_attr or str(h_2x) in h_attr or str(h_orig) in h_attr)
                )
                if is_full_canvas:
                    to_remove.append(child)
                    exterior_removed += 1
                    continue
                if fill_str:
                    c = hex_to_rgb_255(fill_str)
                    dist_b = float(np.linalg.norm(c - bg_rgb))
                    min_ink_dist = min([float(np.linalg.norm(c - ink_rgb)) for ink_rgb in ink_rgbs])
                    if dist_b < min_ink_dist:
                        to_remove.append(child)
                        exterior_removed += 1
                        continue
                    else:
                        best_k = int(np.argmin([float(np.linalg.norm(c - ink_rgb)) for ink_rgb in ink_rgbs]))
                        snap_elem_fill(child, best_k)
                        continue

            if tag in ("path", "polygon"):
                if not fill_str:
                    continue

                c = hex_to_rgb_255(fill_str)
                dist_b = float(np.linalg.norm(c - bg_rgb))
                ink_dists = [float(np.linalg.norm(c - ink_rgb)) for ink_rgb in ink_rgbs]
                min_ink_dist = min(ink_dists)

                if dist_b < min_ink_dist:
                    # Background path or polygon!
                    transform = child.attrib.get("transform", "")
                    tx, ty = 0.0, 0.0
                    if transform:
                        m = SVG_TRANSLATE_RE.search(transform)
                        if m:
                            tx = float(m.group(1))
                            ty = float(m.group(2)) if m.group(2) is not None else 0.0

                    if tag == "polygon":
                        points_str = child.attrib.get("points", "")
                        nums = [float(v) for v in re.findall(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", points_str)]
                        xs = [nums[k] + tx for k in range(0, len(nums) - 1, 2)]
                        ys = [nums[k + 1] + ty for k in range(0, len(nums) - 1, 2)]
                    else:
                        d_str = child.attrib.get("d", "")
                        xs, ys = parse_svg_path_vertices(d_str, tx=tx, ty=ty)

                    if not xs or not ys:
                        to_remove.append(child)
                        exterior_removed += 1
                        continue

                    min_x, max_x = min(xs), max(xs)
                    min_y, max_y = min(ys), max(ys)
                    center_x = (min_x + max_x) / 2.0
                    center_y = (min_y + max_y) / 2.0

                    touches_border = (
                        min_x <= 1.5 or min_y <= 1.5 or max_x >= (w_2x - 1.5) or max_y >= (h_2x - 1.5)
                    )

                    if touches_border:
                        to_remove.append(child)
                        exterior_removed += 1
                        continue

                    # Interior background path (cutout)
                    test_points = [(center_x, center_y)]
                    stride = max(1, len(xs) // 16)
                    for k_idx in range(0, len(xs), stride):
                        test_points.append((xs[k_idx], ys[k_idx]))

                    in_protect = False
                    for px, py in test_points:
                        if any(px0 <= px <= px1 and py0 <= py <= py1 for (px0, py0, px1, py1) in protect_boxes):
                            in_protect = True
                            break
                        if protect_polygons:
                            pt = (float(px), float(py))
                            if any(cv2.pointPolygonTest(poly, pt, False) >= 0 for poly in protect_polygons):
                                in_protect = True
                                break

                    in_cutout = False
                    for px, py in test_points:
                        if any(cx0 <= px <= cx1 and cy0 <= py <= cy1 for (cx0, cy0, cx1, cy1) in cutout_boxes):
                            in_cutout = True
                            break

                    if in_protect and not in_cutout:
                        best_k = int(np.argmin(ink_dists))
                        snap_elem_fill(child, best_k)
                        continue
                    else:
                        to_remove.append(child)
                        interior_cutouts_removed += 1
                        continue
                else:
                    # Closer to foreground ink: snap to nearest canonical ink hex!
                    best_k = int(np.argmin(ink_dists))
                    snap_elem_fill(child, best_k)

        for child in to_remove:
            parent.remove(child)

    os.makedirs(os.path.dirname(os.path.abspath(out_svg_path)), exist_ok=True)
    tree.write(out_svg_path, encoding="utf-8", xml_declaration=True)
    if verbose:
        print(
            f"Cleaned Cutout SVG saved to {out_svg_path} "
            f"(removed {exterior_removed} exterior, {interior_cutouts_removed} interior cutouts, "
            f"snapped fills to {len(canonical_hexes)} spot inks: {', '.join(canonical_hexes)})"
        )

    return interior_cutouts_removed


def discover_palette(
    img_rgb: np.ndarray,
    bg_rgb: np.ndarray,
    inks: Optional[int] = 0,
    merge_hue_tol: float = 12.0,
    verbose: bool = False,
) -> Tuple[List[np.ndarray], List[str]]:
    """
    Auto-discovers foreground spot-color inks:
    1. Samples high-opacity interior foreground pixels (dist_to_bg >= 40.0 AND 2px erosion)
       to avoid anti-aliased boundary mixtures α C + (1-α) B or thin background fringes.
    2. Clusters colors in CIELAB space. If inks > 0, fits K=inks clusters.
       Otherwise, evaluates K in 2..6 and selects K maximizing silhouette score.
    3. Refines cluster centers to median RGB of inliers.
    4. Merges/consolidates hues within merge_hue_tol in CIELAB and discards background-bleed hues.
    """
    P = img_rgb.astype(np.float32)
    if bg_rgb.ndim == 3:
        B_arr = bg_rgb.astype(np.float32)
        mean_B = np.mean(B_arr.reshape(-1, 3), axis=0).astype(np.float32)
        dist_to_bg = np.linalg.norm(P - B_arr, axis=2)
    else:
        mean_B = bg_rgb.astype(np.float32)
        dist_to_bg = np.linalg.norm(P - mean_B.reshape(1, 1, 3), axis=2)

    fg_candidate = (dist_to_bg >= 28.0).astype(np.uint8)
    k_erode = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    fg_interior = cv2.erode(fg_candidate, k_erode, iterations=1) > 0

    if np.sum(fg_interior) < 500:
        fg_interior = dist_to_bg >= 25.0
    if np.sum(fg_interior) < 100:
        fg_interior = dist_to_bg >= 15.0

    sample_rgb = P[fg_interior]
    if len(sample_rgb) < 2:
        if len(sample_rgb) == 1:
            fallback = [np.mean(sample_rgb, axis=0).astype(np.float32)]
        else:
            fallback = [np.array([255.0, 255.0, 255.0], dtype=np.float32)]
        fallback_hexes = [f"#{int(round(c[0])):02X}{int(round(c[1])):02X}{int(round(c[2])):02X}" for c in fallback]
        return fallback, fallback_hexes

    sample_lab = cv2.cvtColor((sample_rgb / 255.0).reshape(-1, 1, 3).astype(np.float32), cv2.COLOR_RGB2LAB).reshape(-1, 3)

    if len(sample_lab) > 10000:
        rng = np.random.default_rng(42)
        idx = rng.choice(len(sample_lab), size=10000, replace=False)
        fit_lab = sample_lab[idx]
        fit_rgb = sample_rgb[idx]
    else:
        fit_lab = sample_lab
        fit_rgb = sample_rgb

    if inks is not None and inks >= 1:
        if inks == 1:
            best_k = 1
            med_rgb = np.median(sample_rgb, axis=0)
            cluster_rgbs = [np.clip(med_rgb, 0.0, 255.0).astype(np.float32)]
        else:
            best_k = min(inks, len(fit_lab))
            km = KMeans(n_clusters=best_k, random_state=42, n_init=10).fit(fit_lab)
            cluster_rgbs = []
            for k in range(best_k):
                mask = km.labels_ == k
                if np.sum(mask) > 0:
                    med_rgb = np.median(fit_rgb[mask], axis=0)
                else:
                    med_lab = km.cluster_centers_[k:k+1].reshape(1, 1, 3).astype(np.float32)
                    med_rgb = cv2.cvtColor(med_lab, cv2.COLOR_LAB2RGB).reshape(3) * 255.0
                cluster_rgbs.append(np.clip(med_rgb, 0.0, 255.0).astype(np.float32))
    else:
        max_k = min(6, max(2, len(fit_lab) // 50))
        best_score = -2.0
        best_k = 2
        best_km = None
        sil_sample_size = min(2000, len(fit_lab))
        for k_cand in range(2, max_k + 1):
            km_cand = KMeans(n_clusters=k_cand, random_state=42, n_init=10).fit(fit_lab)
            try:
                score = float(silhouette_score(fit_lab, km_cand.labels_, sample_size=sil_sample_size, random_state=42))
            except Exception:
                score = -1.0
            if verbose:
                print(f"    Auto-palette search K={k_cand}: silhouette={score:.4f}")
            if score > best_score:
                best_score = score
                best_k = k_cand
                best_km = km_cand
            elif score >= 0.65 and (best_score - score) <= 0.025 and k_cand > best_k:
                # Prefer higher K when clustering is strong (score >= 0.65) and within 0.025 of peak,
                # capturing distinct highlight and shadow tones without under-clustering
                best_k = k_cand
                best_km = km_cand
        km = best_km if best_km is not None else KMeans(n_clusters=2, random_state=42, n_init=10).fit(fit_lab)
        cluster_rgbs = []
        for k in range(best_k):
            mask = km.labels_ == k
            if np.sum(mask) > 0:
                med_rgb = np.median(fit_rgb[mask], axis=0)
            else:
                med_lab = km.cluster_centers_[k:k+1].reshape(1, 1, 3).astype(np.float32)
                med_rgb = cv2.cvtColor(med_lab, cv2.COLOR_LAB2RGB).reshape(3) * 255.0
            cluster_rgbs.append(np.clip(med_rgb, 0.0, 255.0).astype(np.float32))

    lab_B = cv2.cvtColor((mean_B / 255.0).reshape(1, 1, 3).astype(np.float32), cv2.COLOR_RGB2LAB)[0, 0]
    surviving_rgbs = []
    for c_rgb in cluster_rgbs:
        c_lab = cv2.cvtColor((c_rgb / 255.0).reshape(1, 1, 3).astype(np.float32), cv2.COLOR_RGB2LAB)[0, 0]
        dE_bg = float(np.linalg.norm(c_lab - lab_B))
        if dE_bg >= merge_hue_tol:
            surviving_rgbs.append(c_rgb)

    if not surviving_rgbs:
        surviving_rgbs = cluster_rgbs

    consolidated = []
    for c_rgb in surviving_rgbs:
        c_lab = cv2.cvtColor((c_rgb / 255.0).reshape(1, 1, 3).astype(np.float32), cv2.COLOR_RGB2LAB)[0, 0]
        merged = False
        for j, exist_rgb in enumerate(consolidated):
            exist_lab = cv2.cvtColor((exist_rgb / 255.0).reshape(1, 1, 3).astype(np.float32), cv2.COLOR_RGB2LAB)[0, 0]
            if float(np.linalg.norm(c_lab - exist_lab)) < merge_hue_tol:
                consolidated[j] = 0.5 * (exist_rgb + c_rgb)
                merged = True
                break
        if not merged:
            consolidated.append(c_rgb)

    ink_rgbs = [np.clip(c, 0.0, 255.0).astype(np.float32) for c in consolidated]
    canonical_hexes = [f"#{int(round(c[0])):02X}{int(round(c[1])):02X}{int(round(c[2])):02X}" for c in ink_rgbs]
    if verbose:
        print(f"Discovered {len(ink_rgbs)} spot inks: {', '.join(canonical_hexes)}")
    return ink_rgbs, canonical_hexes

def segment_topological_foreground(
    P: np.ndarray,
    B_arr: np.ndarray,
    dist_to_bg: np.ndarray,
    dE_to_bg: np.ndarray,
    min_dE_to_ink: Optional[np.ndarray] = None,
    protect_boxes_2x: Optional[List[Tuple[int, int, int, int]]] = None,
    cutout_boxes_2x: Optional[List[Tuple[int, int, int, int]]] = None,
    protect_polys_2x: Optional[List[np.ndarray]] = None,
    seal_boxes: Optional[List[Tuple[int, int, int, int, int]]] = None,
    protect_interior_boxes_2x: Optional[List[Tuple[int, int, int, int]]] = None,
    alphas: Optional[np.ndarray] = None,
    residuals: Optional[np.ndarray] = None,
    dE_solid: Optional[np.ndarray] = None,
    verbose: bool = False,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Generalized Topological True-Background & Deep-Interior Partitioning:
    1. Defines candidate background pixels via distance and CIELAB delta_E to B_arr,
       with palette-separated background proof (closer to B than to any foreground ink),
       and tight linear-mixture cusp pixels near background well-explained by local ink ray projection.
    2. Runs 8-connected component labeling to separate:
       - exterior_bg: touching any of the 4 outer canvas borders.
       - valid_cutouts: genuine negative-space interior holes (either large cutouts with area >= 24,
         max_edt >= 1.5px, p25_dE <= 20.0 with palette-relative dE_to_bg < min_dE_to_ink dominance,
         OR palette-separated background dominance with area >= 4, min_dE <= 20.0, and p25_dE + 3.0 < p25_min_ink_dE).
    3. Defines true_bg = exterior_bg | valid_cutouts.
    4. Computes deep_interior = distance_transform_edt(~true_bg) > 8.0 px at 2x (4.0 px at 1x).
    Returns (deep_interior, valid_cutouts, true_bg).
    """
    H, W = dist_to_bg.shape

    # 1. Candidate background pixels (palette-separated background proof)
    if min_dE_to_ink is not None:
        candidate_bg = (
            ((dE_to_bg <= 26.0) & (dE_to_bg < min_dE_to_ink))
            | ((dist_to_bg < 38.0) & (dE_to_bg < 30.0) & (dE_to_bg + 6.0 < min_dE_to_ink))
        )
    else:
        candidate_bg = (dE_to_bg <= 26.0) | (dist_to_bg < 38.0)

    # Tight linear-mixture cusp pixels near background (dist_to_bg < 55.0)
    # that are well-explained as a mixture of B and a locally present ink C_k
    # with alpha_k < 0.55 and orthogonal ray residual <= 20.0
    if alphas is not None and residuals is not None:
        K = alphas.shape[2]
        cusp_mixture = np.zeros((H, W), dtype=bool)
        for k in range(K):
            if dE_solid is not None:
                is_solid_k = (dE_solid[:, :, k] <= 18.0) | ((alphas[:, :, k] >= 0.70) & (residuals[:, :, k] <= 18.0))
            else:
                is_solid_k = (alphas[:, :, k] >= 0.70) & (residuals[:, :, k] <= 18.0)
            local_presence_k = scipy.ndimage.maximum_filter(is_solid_k, size=11)
            cusp_k = (dist_to_bg < 55.0) & (alphas[:, :, k] < 0.55) & (residuals[:, :, k] <= 20.0) & local_presence_k
            cusp_mixture |= cusp_k
        candidate_bg |= cusp_mixture

    # Looser threshold inside cutout boxes
    for cbox in (cutout_boxes_2x or []):
        cx0, cy0, cx1, cy1 = clamp_box(*cbox, W, H)
        candidate_bg[cy0:cy1, cx0:cx1] |= (dist_to_bg[cy0:cy1, cx0:cx1] < 38.0)

    # Apply morphological closing to foreground (~candidate_bg) inside seal_boxes
    for sb in (seal_boxes or []):
        sx0, sy0, sx1, sy1, r = sb
        sx0, sy0, sx1, sy1 = clamp_box(sx0, sy0, sx1, sy1, W, H)
        px0, py0 = max(0, sx0 - r), max(0, sy0 - r)
        px1, py1 = min(W, sx1 + r), min(H, sy1 + r)
        roi_fg = (~candidate_bg[py0:py1, px0:px1]).astype(np.uint8)
        k_seal = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
        closed_roi = cv2.morphologyEx(roi_fg, cv2.MORPH_CLOSE, k_seal)
        ox0, oy0 = sx0 - px0, sy0 - py0
        ox1, oy1 = ox0 + (sx1 - sx0), oy0 + (sy1 - sy0)
        candidate_bg[sy0:sy1, sx0:sx1] &= ~(closed_roi[oy0:oy1, ox0:ox1] > 0)

    # Force background to False inside protected areas
    for pbox in (protect_boxes_2x or []):
        px0, py0, px1, py1 = clamp_box(*pbox, W, H)
        candidate_bg[py0:py1, px0:px1] = False

    for poly in (protect_polys_2x or []):
        poly_mask = np.zeros((H, W), dtype=np.uint8)
        cv2.fillPoly(poly_mask, [poly], 1)
        candidate_bg[poly_mask > 0] = False

    # 2. 8-connected component labeling
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
        candidate_bg.astype(np.uint8), connectivity=8
    )

    exterior_bg = np.zeros((H, W), dtype=bool)
    valid_cutouts = np.zeros((H, W), dtype=bool)

    for lbl in range(1, num_labels):
        x = stats[lbl, cv2.CC_STAT_LEFT]
        y = stats[lbl, cv2.CC_STAT_TOP]
        w = stats[lbl, cv2.CC_STAT_WIDTH]
        h = stats[lbl, cv2.CC_STAT_HEIGHT]
        area = stats[lbl, cv2.CC_STAT_AREA]

        touches_border = (x == 0) or (y == 0) or (x + w >= W) or (y + h >= H)
        if touches_border:
            exterior_bg[y:y+h, x:x+w] |= (labels[y:y+h, x:x+w] == lbl)
        else:
            # Interior component NOT touching the border
            # Check if suppressed by protect_interior_boxes_2x
            in_protect_interior = False
            for pibox in (protect_interior_boxes_2x or []):
                bx0, by0, bx1, by1 = clamp_box(*pibox, W, H)
                if not (x + w <= bx0 or x >= bx1 or y + h <= by0 or y >= by1):
                    if np.any(labels[max(y, by0):min(y+h, by1), max(x, bx0):min(x+w, bx1)] == lbl):
                        in_protect_interior = True
                        break
            if in_protect_interior:
                continue

            # Check if suppressed by seal_boxes
            in_seal_box = False
            for sb in (seal_boxes or []):
                sx0, sy0, sx1, sy1, _ = sb
                bx0, by0, bx1, by1 = clamp_box(sx0, sy0, sx1, sy1, W, H)
                if not (x + w <= bx0 or x >= bx1 or y + h <= by0 or y >= by1):
                    if np.any(labels[max(y, by0):min(y+h, by1), max(x, bx0):min(x+w, bx1)] == lbl):
                        in_seal_box = True
                        break
            if in_seal_box:
                continue

            # Check if in cutout_boxes_2x
            is_in_cutout_box = False
            for cbox in (cutout_boxes_2x or []):
                cx0, cy0, cx1, cy1 = clamp_box(*cbox, W, H)
                if not (x + w <= cx0 or x >= cx1 or y + h <= cy0 or y >= cy1):
                    if np.any(labels[max(y, cy0):min(y+h, cy1), max(x, cx0):min(x+w, cx1)] == lbl):
                        is_in_cutout_box = True
                        break

            if area < 4:
                continue

            comp_slice = (labels[y:y+h, x:x+w] == lbl)
            comp_dE = dE_to_bg[y:y+h, x:x+w][comp_slice]
            min_dE = float(np.min(comp_dE))
            p25_dE = float(np.percentile(comp_dE, 25))

            comp_edt = scipy.ndimage.distance_transform_edt(comp_slice)
            max_edt = float(np.max(comp_edt))

            if min_dE_to_ink is not None:
                comp_ink_dE = min_dE_to_ink[y:y+h, x:x+w][comp_slice]
                p25_ink_dE = float(np.percentile(comp_ink_dE, 25))
            else:
                p25_ink_dE = 1e6

            # 1. Large cutout rule (must be closer to background than to any ink)
            min_area = 6 if is_in_cutout_box else 24
            max_p25 = 22.0 if is_in_cutout_box else 20.0
            is_large_cutout = (area >= min_area) and (max_edt >= 1.5) and (p25_dE <= max_p25) and (p25_dE < p25_ink_dE)

            # 2. High-confidence small cutout rule
            is_small_cutout = (area >= 12) and (p25_dE <= 20.0) and (p25_dE + 3.0 < p25_ink_dE)

            # 3. Palette-separated background dominance rule (small genuine negative-space pockets)
            is_bg_dominant = (area >= 4) and (min_dE <= 20.0) and (p25_dE + 3.0 < p25_ink_dE)

            if not (is_large_cutout or is_small_cutout or is_bg_dominant):
                continue

            valid_cutouts[y:y+h, x:x+w] |= comp_slice
            if verbose:
                print(f"        Cutout component {lbl}: at x={x}, y={y}, w={w}, h={h}, area={area}, p25_dE={p25_dE:.1f}")

    true_bg = exterior_bg | valid_cutouts
    edt_from_true_bg = scipy.ndimage.distance_transform_edt(~true_bg)
    deep_interior = edt_from_true_bg > 8.0

    if verbose:
        n_cutout_lbls = len(np.unique(labels[valid_cutouts]))
        print(f"    Topological Partitioning: exterior_bg={int(np.sum(exterior_bg))} px, "
              f"valid_cutouts={int(np.sum(valid_cutouts))} px ({n_cutout_lbls} components), "
              f"deep_interior={int(np.sum(deep_interior))} px")

    return deep_interior, valid_cutouts, true_bg


def consolidate_low_gradient_voronoi_splits(
    k_star: np.ndarray,
    fg_mask: np.ndarray,
    P_lab: np.ndarray,
    ink_rgbs: List[np.ndarray],
    max_dE_pair: float = 28.0,
    max_p75_grad: float = 4.5,
    verbose: bool = False,
) -> np.ndarray:
    """
    Generalized Low-Gradient Voronoi Split Consolidation:
    Eliminates two-tone camouflage blotching on smoothly shaded regions (e.g. spheres, cones, cheeks)
    while strictly preserving crisp cel-shaded drawn edges (hat cheek shadow, bevels, stripes, stars).

    When an ink component of k1 inside fg_mask shares a boundary with neighbor ink k2
    where dE(C_k1, C_k2) < max_dE_pair:
    Measures the 75th-percentile step-edge gradient in unquantized P_lab across their shared boundary.
    - Genuine cel-shaded art boundaries have a crisp drawn edge (p75 >= 6.0 CIELAB units).
    - False camouflage blotches cross a smooth gradient without a step edge (p75 < 4.5 CIELAB units).
    Any component meeting a neighbor across a low-gradient boundary merges into the dominant neighbor.
    """
    K = len(ink_rgbs)
    if K < 2 or not np.any(fg_mask):
        return k_star

    k_out = k_star.copy()
    H, W = k_star.shape

    # Precompute pairwise CIELAB distances between inks
    ink_labs = [
        cv2.cvtColor(
            np.clip(ink_rgbs[k] / 255.0, 0.0, 1.0).reshape(1, 1, 3).astype(np.float32),
            cv2.COLOR_RGB2LAB
        )[0, 0]
        for k in range(K)
    ]
    pair_dE = np.zeros((K, K), dtype=np.float32)
    for i in range(K):
        for j in range(K):
            pair_dE[i, j] = float(np.linalg.norm(ink_labs[i] - ink_labs[j]))

    # Step-edge difference magnitude in unquantized P_lab
    diff_x = np.zeros((H, W), dtype=np.float32)
    diff_y = np.zeros((H, W), dtype=np.float32)
    diff_x[:, :-1] = np.linalg.norm(P_lab[:, 1:, :] - P_lab[:, :-1, :], axis=2)
    diff_y[:-1, :] = np.linalg.norm(P_lab[1:, :, :] - P_lab[:-1, :, :], axis=2)
    grad_mag = np.maximum(diff_x, diff_y)

    k_dil = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))

    total_merged = 0
    for pass_idx in range(2):
        pass_merged = 0
        unique_inks = np.unique(k_out[fg_mask])
        for k1 in unique_inks:
            mask_k1 = fg_mask & (k_out == k1)
            if not np.any(mask_k1):
                continue

            num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
                mask_k1.astype(np.uint8), connectivity=8
            )

            for lbl in range(1, num_labels):
                area = stats[lbl, cv2.CC_STAT_AREA]
                x = stats[lbl, cv2.CC_STAT_LEFT]
                y = stats[lbl, cv2.CC_STAT_TOP]
                w = stats[lbl, cv2.CC_STAT_WIDTH]
                h = stats[lbl, cv2.CC_STAT_HEIGHT]

                px0, py0 = max(0, x - 1), max(0, y - 1)
                px1, py1 = min(W, x + w + 1), min(H, y + h + 1)

                comp_local = (labels[py0:py1, px0:px1] == lbl)
                dilated_local = cv2.dilate(comp_local.astype(np.uint8), k_dil) > 0
                boundary_band = dilated_local & (~comp_local) & fg_mask[py0:py1, px0:px1]

                if not np.any(boundary_band):
                    continue

                neighbor_inks = k_out[py0:py1, px0:px1][boundary_band]
                unique_neighbors = np.unique(neighbor_inks)

                best_target_k = None
                best_contact = 0

                for k2 in unique_neighbors:
                    if k2 == k1 or pair_dE[k1, k2] >= max_dE_pair:
                        continue

                    k2_boundary = boundary_band & (k_out[py0:py1, px0:px1] == k2)
                    contact_size = int(np.sum(k2_boundary))
                    if contact_size < 2:
                        continue

                    # Gradient across the shared boundary
                    grad_vals = grad_mag[py0:py1, px0:px1][k2_boundary]
                    p75_grad = float(np.percentile(grad_vals, 75))

                    if p75_grad < max_p75_grad:
                        if contact_size > best_contact:
                            best_contact = contact_size
                            best_target_k = k2

                if best_target_k is not None:
                    k_out[py0:py1, px0:px1][comp_local] = best_target_k
                    pass_merged += area

        total_merged += pass_merged
        if pass_merged == 0:
            break

    if verbose and total_merged > 0:
        print(f"    Low-Gradient Voronoi Consolidation: merged {total_merged} smooth shading pixels across low-gradient boundaries")

    return k_out


def unmix_spot_colors(
    P: np.ndarray,
    B: np.ndarray,
    ink_rgbs: List[np.ndarray],
    bg_flat_rgb: Optional[np.ndarray] = None,
    protect_interior_boxes_2x: Optional[List[Tuple[int, int, int, int]]] = None,
    protect_boxes_2x: Optional[List[Tuple[int, int, int, int]]] = None,
    cutout_boxes_2x: Optional[List[Tuple[int, int, int, int]]] = None,
    protect_polys_2x: Optional[List[np.ndarray]] = None,
    seal_boxes: Optional[List[Tuple[int, int, int, int, int]]] = None,
    verbose: bool = False,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Strict Spot-Color Ray Unmixing + Generalized Topological True-Background & Deep-Interior Guard + 9x9 Local Core Support (4px radius at 2x resolution):
    1. Supports spatially varying B field ((H, W, 3)) or uniform B vector ((3,)).
    2. Projects P - B onto each ink ray C_k - B for boundary anti-aliased mixture pixels.
    3. Partitions canvas topologically via segment_topological_foreground:
       - Exterior canvas background & valid negative-space cutouts form true_bg.
       - Pixels > 8.0px (4.0px at 1x) from true_bg form deep_interior: evaluated directly by perceptual solid CIELAB distance (alpha = 1.0).
    4. Enforces 9x9 local core support constraint (4px radius at 2x resolution) on transition boundaries to eliminate rogue color halos.
    5. Post-alpha topological pinhole seal guarantees zero accidental sub-threshold holes inside solid art.
    6. Builds the strictly flat (K+1)-color RGB raster for VTracer spline vectorization.
    """
    K = len(ink_rgbs)
    H, W, _ = P.shape

    if B.ndim == 1:
        B_arr = B.reshape(1, 1, 3).astype(np.float32)
        mean_B = B.astype(np.float32)
    elif B.ndim == 2:
        B_arr = B.reshape(1, -1, 3).astype(np.float32)
        mean_B = np.mean(B_arr.reshape(-1, 3), axis=0).astype(np.float32)
    else:
        B_arr = B.astype(np.float32)
        mean_B = np.mean(B_arr.reshape(-1, 3), axis=0).astype(np.float32)

    if bg_flat_rgb is None:
        bg_flat_rgb = mean_B

    P_minus_B = P - B_arr
    dist_to_bg = np.linalg.norm(P_minus_B, axis=2)

    alphas = []
    residuals = []
    dots = []
    for k in range(K):
        diff_k = ink_rgbs[k].reshape(1, 1, 3) - B_arr
        norm_sq_k = np.sum(diff_k * diff_k, axis=2) + 1e-6
        dot_k = np.sum(P_minus_B * diff_k, axis=2)
        dots.append(dot_k)
        a_k = np.clip(dot_k / norm_sq_k, 0.0, 1.0)
        P_hat = B_arr + a_k[:, :, None] * diff_k
        res = np.linalg.norm(P - P_hat, axis=2)
        alphas.append(a_k)
        residuals.append(res)

    alphas = np.stack(alphas, axis=2)
    residuals = np.stack(residuals, axis=2)
    dots = np.stack(dots, axis=2)
    raw_scores = residuals / (alphas + 0.15)

    # Convert P and B to CIELAB for perceptual color distance metrics
    P_u8 = np.clip(np.round(P), 0, 255).astype(np.uint8)
    P_smooth_u8 = cv2.bilateralFilter(P_u8, d=9, sigmaColor=18.0, sigmaSpace=7.0)
    P_smooth_norm = P_smooth_u8.astype(np.float32) / 255.0
    P_smooth_lab = cv2.cvtColor(P_smooth_norm, cv2.COLOR_RGB2LAB)

    P_norm = np.clip(P / 255.0, 0.0, 1.0).astype(np.float32)
    P_lab = cv2.cvtColor(P_norm, cv2.COLOR_RGB2LAB)
    if B_arr.shape[:2] == (1, 1):
        B_norm = np.clip(mean_B / 255.0, 0.0, 1.0).reshape(1, 1, 3).astype(np.float32)
        B_lab = cv2.cvtColor(B_norm, cv2.COLOR_RGB2LAB)[0, 0].reshape(1, 1, 3)
    else:
        B_norm = np.clip(B_arr / 255.0, 0.0, 1.0).astype(np.float32)
        B_lab = cv2.cvtColor(B_norm, cv2.COLOR_RGB2LAB)

    dE_to_bg = np.linalg.norm(P_lab - B_lab, axis=2)

    # Compute CIELAB distance from every pixel to every candidate ink C_k using edge-preserving smoothed lab
    dE_solid = np.zeros((H, W, K), dtype=np.float32)
    for k in range(K):
        Ck_norm = np.clip(ink_rgbs[k] / 255.0, 0.0, 1.0).reshape(1, 1, 3).astype(np.float32)
        Ck_lab = cv2.cvtColor(Ck_norm, cv2.COLOR_RGB2LAB)[0, 0]
        dE_solid[:, :, k] = np.linalg.norm(P_smooth_lab - Ck_lab.reshape(1, 1, 3), axis=2)
    min_dE_to_ink = np.min(dE_solid, axis=2)

    # Topological True-Background & Deep-Interior Partitioning
    deep_interior, valid_cutouts, true_bg = segment_topological_foreground(
        P=P,
        B_arr=B_arr,
        dist_to_bg=dist_to_bg,
        dE_to_bg=dE_to_bg,
        min_dE_to_ink=min_dE_to_ink,
        protect_boxes_2x=protect_boxes_2x,
        cutout_boxes_2x=cutout_boxes_2x,
        protect_polys_2x=protect_polys_2x,
        seal_boxes=seal_boxes,
        protect_interior_boxes_2x=protect_interior_boxes_2x,
        alphas=alphas,
        residuals=residuals,
        dE_solid=dE_solid,
        verbose=verbose,
    )
    edt_from_true_bg = scipy.ndimage.distance_transform_edt(~true_bg)

    # For deep interior pixels (> 8px away from true_bg), evaluate direct
    # solid CIELAB distance (alpha = 1.0) to each palette ink C_k instead of fractional
    # background-blended ray projection.
    if np.any(deep_interior):
        raw_scores[deep_interior] = dE_solid[deep_interior]
        alphas[deep_interior] = 1.0
        dots[deep_interior] = np.maximum(dots[deep_interior], 1.0)
        if verbose:
            print(f"    Interior Solid CIELAB Guard: {int(np.sum(deep_interior))} solid interior pixels evaluated directly")

    # Local Core Support Constraint (Universal Spatial + Extremum Core Support):
    # 1. Determine unconstrained winner
    k_raw = np.argmin(raw_scores, axis=2)
    best_raw_alpha = np.take_along_axis(alphas, k_raw[:, :, None], axis=2).squeeze(2)

    # 2. Local 3x3 luminance extrema in P_lab
    P_L = P_lab[:, :, 0]
    P_L_max3 = scipy.ndimage.maximum_filter(P_L, size=3)
    P_L_min3 = scipy.ndimage.minimum_filter(P_L, size=3)

    # 3. Define true physical core presence of ink k:
    # A pixel qualifies as a genuine core ONLY if (k_raw == k) & (alpha >= 0.78) & (dE_to_bg >= 22.0 | deep_interior)
    # AND EITHER:
    # a) It survives a 3x3 morphological erosion (genuine solid region >= 3x3), OR
    # b) It is a genuine 1D luminance ridge/valley stroke (NOT a monotonic slope between two inks or background!):
    #    - for a bright thin stroke (is_brightest): P_L >= maximum_filter(P_L, 3) - 1.2, dE_solid <= 8.0, area(k_raw == k) >= 10 px
    #    - for a dark thin stroke (is_darkest): P_L <= minimum_filter(P_L, 3) + 1.2, dE_solid <= 10.0, edt_from_true_bg > 4.0, area(k_raw == k) >= 10 px
    k_erode3 = np.ones((3, 3), np.uint8)
    is_core = np.zeros((H, W, K), dtype=bool)
    for k in range(K):
        base_core_k = (
            (k_raw == k)
            & (best_raw_alpha >= 0.78)
            & ((dE_to_bg >= 22.0) | deep_interior)
            & (dE_solid[:, :, k] <= 18.0)
        )
        survives_erode = cv2.erode(base_core_k.astype(np.uint8), k_erode3) > 0

        # Connected component area of (k_raw == k) >= 10 px
        mask_k = (k_raw == k)
        num_lbl_k, lbls_k, stats_k, _ = cv2.connectedComponentsWithStats(
            mask_k.astype(np.uint8), connectivity=8
        )
        if num_lbl_k > 1:
            comp_area_ge_10 = (lbls_k > 0) & (stats_k[lbls_k, cv2.CC_STAT_AREA] >= 10)
        else:
            comp_area_ge_10 = np.zeros((H, W), dtype=bool)

        is_brightest = (
            (P_L >= P_L_max3 - 1.2)
            & (dE_solid[:, :, k] <= 8.0)
            & comp_area_ge_10
        )
        is_darkest = (
            (P_L <= P_L_min3 + 1.2)
            & (dE_solid[:, :, k] <= 10.0)
            & (edt_from_true_bg > 4.0)
            & comp_area_ge_10
        )
        is_extremum = is_brightest | is_darkest
        is_core[:, :, k] = base_core_k & (survives_erode | is_extremum)

    # 4. Dilate is_core by 9x9 window (radius = 4px at 2x ESRGAN resolution)
    # Covering anti-aliased transition ramps without bleeding across neighboring features
    supported_mask = np.zeros((H, W, K), dtype=bool)
    for k in range(K):
        supported_mask[:, :, k] = scipy.ndimage.maximum_filter(is_core[:, :, k], size=9)

    # 4b. Single-Ink Component Core Propagation:
    # For isolated foreground strokes/components (e.g. pine fronds, trees, stars, filigree),
    # if a connected foreground component has genuine solid core support from EXACTLY ONE ink,
    # that ink's support propagates across the entire component, preventing thin tapered extremities
    # from being hijacked by unconstrained alien inks.
    fg_stroke_cand = (~true_bg) & ((dist_to_bg >= 22.0) | deep_interior)
    num_fg_cc, fg_cc_labels, fg_cc_stats, _ = cv2.connectedComponentsWithStats(
        fg_stroke_cand.astype(np.uint8), connectivity=8
    )
    for lbl in range(1, num_fg_cc):
        area = fg_cc_stats[lbl, cv2.CC_STAT_AREA]
        if area < 4:
            continue
        comp_mask = (fg_cc_labels == lbl)
        comp_core_inks = [k for k in range(K) if np.any(is_core[comp_mask, k])]
        if len(comp_core_inks) == 1:
            single_k = comp_core_inks[0]
            supported_mask[comp_mask, single_k] = True

    any_supported = np.any(supported_mask, axis=2)

    # 5. Universal core support penalty across BOTH boundary and deep interior
    scores = raw_scores.copy()
    mask_to_penalize = any_supported[:, :, None] & (~supported_mask)
    scores[mask_to_penalize] = 1e6

    # 6. Pick winning ink
    k_star = np.argmin(scores, axis=2)
    best_alpha = np.take_along_axis(alphas, k_star[:, :, None], axis=2).squeeze(2)
    best_dot = np.take_along_axis(dots, k_star[:, :, None], axis=2).squeeze(2)

    # 7. Continuous alpha ramp, tightened against Gibbs ringing
    clean_alpha = np.clip((best_alpha - 0.25) / (0.80 - 0.25), 0.0, 1.0)
    clean_alpha[dist_to_bg < 22.0] = 0.0
    bg_dominant_near_edge = (edt_from_true_bg <= 6.0) & (dist_to_bg < 38.0) & (dE_to_bg + 4.0 < min_dE_to_ink)
    clean_alpha[bg_dominant_near_edge] = 0.0

    # Ensure best_dot <= 0 ONLY applies in near_bg (edt <= 6.0 and dE_to_bg <= 22.0)
    # so thin foreground highlights near dark outlines (like hat brim edge) are NEVER zeroed out into holes
    near_bg_hole_guard = (edt_from_true_bg <= 6.0) & (dE_to_bg <= 22.0)
    clean_alpha[(best_dot <= 0) & near_bg_hole_guard] = 0.0

    clean_alpha[deep_interior] = 1.0
    clean_alpha[valid_cutouts] = 0.0

    # Apply ROI overrides
    for pbox in (protect_boxes_2x or []):
        px0, py0, px1, py1 = clamp_box(*pbox, W, H)
        clean_alpha[py0:py1, px0:px1] = 1.0

    for poly in (protect_polys_2x or []):
        poly_mask = np.zeros((H, W), dtype=np.uint8)
        cv2.fillPoly(poly_mask, [poly], 1)
        clean_alpha[poly_mask > 0] = 1.0

    for cbox in (cutout_boxes_2x or []):
        cx0, cy0, cx1, cy1 = clamp_box(*cbox, W, H)
        box_d = dist_to_bg[cy0:cy1, cx0:cx1]
        clean_alpha[cy0:cy1, cx0:cx1][box_d < 38.0] = 0.0

    # Post-Alpha Topological Pinhole Seal (Palette-Relative Background Proof)
    fg_mask = clean_alpha >= 0.50
    bg_mask = ~fg_mask
    n_bg_lbl, bg_lbls, bg_stats, _ = cv2.connectedComponentsWithStats(bg_mask.astype(np.uint8), connectivity=8)
    sealed_pinholes = 0
    for b_lbl in range(1, n_bg_lbl):
        bx = bg_stats[b_lbl, cv2.CC_STAT_LEFT]
        by = bg_stats[b_lbl, cv2.CC_STAT_TOP]
        bw = bg_stats[b_lbl, cv2.CC_STAT_WIDTH]
        bh = bg_stats[b_lbl, cv2.CC_STAT_HEIGHT]
        b_touches_border = (bx == 0) or (by == 0) or (bx + bw >= W) or (by + bh >= H)
        if b_touches_border:
            continue
        b_comp_mask = (bg_lbls[by:by+bh, bx:bx+bw] == b_lbl)
        overlaps_cutout = np.any(valid_cutouts[by:by+bh, bx:bx+bw] & b_comp_mask)
        if overlaps_cutout:
            continue

        comp_edt = edt_from_true_bg[by:by+bh, bx:bx+bw][b_comp_mask]
        min_comp_edt = float(np.min(comp_edt))
        # NEVER seal boundary anti-aliased ramp pixels into opaque brown!
        if min_comp_edt <= 6.0:
            continue

        comp_dE_bg = dE_to_bg[by:by+bh, bx:bx+bw][b_comp_mask]
        comp_min_dE_bg = float(np.min(comp_dE_bg))
        comp_p25_dE_bg = float(np.percentile(comp_dE_bg, 25))

        comp_min_ink = min_dE_to_ink[by:by+bh, bx:bx+bw][b_comp_mask]
        comp_p25_ink = float(np.percentile(comp_min_ink, 25))

        # If component is clearly genuine background rather than an ink-to-ink seam/pinhole,
        # NEVER seal it!
        if comp_min_dE_bg <= 24.0 or (comp_p25_dE_bg + 4.0 < comp_p25_ink):
            continue

        fg_mask[by:by+bh, bx:bx+bw][b_comp_mask] = True
        clean_alpha[by:by+bh, bx:bx+bw][b_comp_mask] = 1.0
        sealed_pinholes += int(np.sum(b_comp_mask))

    if verbose and sealed_pinholes > 0:
        print(f"    Post-Alpha Topological Pinhole Seal: sealed {sealed_pinholes} accidental sub-threshold pinhole pixels")

    # 8. Generalized Low-Gradient Voronoi Split Consolidation
    k_star = consolidate_low_gradient_voronoi_splits(
        k_star=k_star,
        fg_mask=fg_mask,
        P_lab=P_lab,
        ink_rgbs=ink_rgbs,
        max_dE_pair=28.0,
        max_p75_grad=4.5,
        verbose=verbose,
    )

    ink_colors_arr = np.array(ink_rgbs, dtype=np.float32)
    quantized_rgb = ink_colors_arr[k_star]

    flat_quantized = np.zeros((H, W, 3), dtype=np.uint8)
    flat_quantized[:] = np.clip(np.round(bg_flat_rgb), 0, 255).astype(np.uint8)
    flat_quantized[fg_mask] = np.clip(np.round(quantized_rgb[fg_mask]), 0, 255).astype(np.uint8)

    return flat_quantized, k_star, clean_alpha, dist_to_bg

def consolidate_raster_speckles(
    k_star: np.ndarray,
    fg_mask: np.ndarray,
    ink_rgbs: Optional[List[np.ndarray]] = None,
    filter_speckle: int = 16,
    num_passes: int = 2,
    verbose: bool = False,
) -> np.ndarray:
    """
    Consolidates small foreground ink speckles and transition slivers (< filter_speckle)
    into adjacent foreground inks before VTracer vectorization.
    Prevents VTracer from discarding tiny intermediate ink patches and cutting holes inside solid art.
    High-contrast intentional dots (dE >= 24.0 to modal neighbor) with area >= 6 px are strictly preserved.
    Freestanding isolated foreground dots surrounded by background are preserved.
    """
    if filter_speckle <= 0:
        return k_star

    k_out = k_star.copy()
    H, W = k_star.shape
    k_dil = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))

    for pass_idx in range(num_passes):
        total_reassigned = 0
        unique_inks = np.unique(k_out[fg_mask])
        for k in unique_inks:
            ink_mask = fg_mask & (k_out == k)
            if not np.any(ink_mask):
                continue

            num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
                ink_mask.astype(np.uint8), connectivity=8
            )

            for lbl in range(1, num_labels):
                area = stats[lbl, cv2.CC_STAT_AREA]
                if area >= filter_speckle:
                    continue

                x = stats[lbl, cv2.CC_STAT_LEFT]
                y = stats[lbl, cv2.CC_STAT_TOP]
                w = stats[lbl, cv2.CC_STAT_WIDTH]
                h = stats[lbl, cv2.CC_STAT_HEIGHT]

                px0 = max(0, x - 1)
                py0 = max(0, y - 1)
                px1 = min(W, x + w + 1)
                py1 = min(H, y + h + 1)

                comp_local = (labels[py0:py1, px0:px1] == lbl)
                dilated_local = cv2.dilate(comp_local.astype(np.uint8), k_dil) > 0
                fg_local = fg_mask[py0:py1, px0:px1]
                neighbor_fg = dilated_local & (~comp_local) & fg_local

                if np.any(neighbor_fg):
                    neighbor_inks = k_out[py0:py1, px0:px1][neighbor_fg]
                    counts = np.bincount(neighbor_inks)
                    modal_ink = int(np.argmax(counts))

                    if modal_ink == k:
                        continue

                    if ink_rgbs is not None:
                        Ck_lab = cv2.cvtColor(
                            np.clip(ink_rgbs[k] / 255.0, 0.0, 1.0).reshape(1, 1, 3).astype(np.float32),
                            cv2.COLOR_RGB2LAB
                        )[0, 0]
                        Cm_lab = cv2.cvtColor(
                            np.clip(ink_rgbs[modal_ink] / 255.0, 0.0, 1.0).reshape(1, 1, 3).astype(np.float32),
                            cv2.COLOR_RGB2LAB
                        )[0, 0]
                        dE_km = float(np.linalg.norm(Ck_lab - Cm_lab))
                        # High-contrast features (dE >= 24.0) with area >= 6 px are intentional dots/stars/accents,
                        # NEVER absorb them! Only absorb low-contrast transition slivers (dE < 24.0 or area < 6).
                        if dE_km >= 24.0 and area >= 6:
                            continue

                    k_out[py0:py1, px0:px1][comp_local] = modal_ink
                    total_reassigned += area

        if verbose and total_reassigned > 0:
            print(f"    Speckle consolidation pass {pass_idx + 1}: {total_reassigned} pixels absorbed into adjacent inks")
        if total_reassigned == 0:
            break

    return k_out


def inject_seam_backing_underlay(
    svg_path: str,
    k_star: np.ndarray,
    fg_mask: np.ndarray,
    ink_rgbs: List[np.ndarray],
    canonical_hexes: List[str],
    bg_flat_rgb: np.ndarray,
    vtracer_bin: Optional[str] = None,
    verbose: bool = False,
) -> int:
    """
    Generalized Internal Seam-Backing Underlay (Zero Exterior Bleed, Zero Cross-Color Contamination):
    1. Computes safe_interior via distance_transform_edt(fg_mask) > 2.5 (strictly > 2.5px inset from every outer edge & cutout hole).
    2. Identifies all internal ink-to-ink seams inside fg_mask (where 2+ different foreground inks meet within 3x3).
    3. Dilates seams into a compact ~4-5px seam_zone masked strictly by safe_interior.
    4. Across each internal seam, dilates the lower-luminance (darker) ink by ~2-3px across the seam (via cv2.erode
       on luminance-ranked index inside 5x5 ellipse), creating a single continuous solid vector ribbon spanning across
       the seam underneath both top paths (guaranteeing 0.0px spline separation gaps).
    5. Traces seam_zone with VTracer, strips background paths, snaps fills to canonical_hexes,
       and prepends them to svg_path as underlays.
    """
    K = len(ink_rgbs)
    if K < 2 or not np.any(fg_mask):
        return 0

    safe_interior = scipy.ndimage.distance_transform_edt(fg_mask) > 2.5
    if not np.any(safe_interior):
        return 0

    # Detect all internal ink-to-ink seams inside fg_mask
    is_seam = fg_mask & (
        scipy.ndimage.maximum_filter(k_star, size=3) != scipy.ndimage.minimum_filter(k_star, size=3)
    )
    if not np.any(is_seam & safe_interior):
        return 0

    k_zone = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    seam_zone = (cv2.dilate((is_seam & safe_interior).astype(np.uint8), k_zone) > 0) & safe_interior
    if not np.any(seam_zone):
        return 0

    # Rank inks by luminance (darkest to lightest)
    lum = [
        0.2126 * float(c[0]) + 0.7152 * float(c[1]) + 0.0722 * float(c[2])
        for c in ink_rgbs
    ]
    sorted_inks = sorted(range(K), key=lambda k: lum[k])
    rank_of_k = np.zeros(K, dtype=np.int32)
    for rank, k_idx in enumerate(sorted_inks):
        rank_of_k[k_idx] = rank
    k_of_rank = np.array(sorted_inks, dtype=np.int32)

    # Across each internal seam inside seam_zone, dilate the lower-luminance (darker) ink
    # by ~3-4px across the seam (cv2.erode on luminance-ranked index inside 9x9, mapped back to k_star)
    # so ONE single continuous vector ribbon spans right across the seam line underneath BOTH top paths.
    rank_map = np.where(fg_mask, rank_of_k[k_star], K)
    k_erode = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    min_rank_map = cv2.erode(
        rank_map.astype(np.uint8),
        k_erode
    )
    clamped_rank = np.minimum(min_rank_map, K - 1)
    seam_k = k_of_rank[clamped_rank]

    H, W = fg_mask.shape
    seam_raster = np.zeros((H, W, 3), dtype=np.uint8)
    seam_raster[:] = np.clip(np.round(bg_flat_rgb), 0, 255).astype(np.uint8)
    ink_colors_arr = np.array(ink_rgbs, dtype=np.float32)
    seam_raster[seam_zone] = np.clip(np.round(ink_colors_arr[seam_k[seam_zone]]), 0, 255).astype(np.uint8)

    vtracer_exe = resolve_vtracer_bin(vtracer_bin)
    with tempfile.TemporaryDirectory() as td:
        temp_seam_png = os.path.join(td, "seam_raster.png")
        temp_seam_svg = os.path.join(td, "seam_raw.svg")
        Image.fromarray(seam_raster).save(temp_seam_png)

        vtracer_cmd = [
            vtracer_exe,
            "--input", temp_seam_png,
            "--output", temp_seam_svg,
            "--colormode", "color",
            "--hierarchical", "cutout",
            "--mode", "spline",
            "--filter_speckle", "1",
            "--color_precision", "8",
            "--corner_threshold", "55",
        ]
        subprocess.run(vtracer_cmd, check=True, timeout=120, capture_output=True, text=True)

        ET.register_namespace("", "http://www.w3.org/2000/svg")
        seam_tree = ET.parse(temp_seam_svg)
        seam_root = seam_tree.getroot()

        backing_elements = []
        for elem in seam_root.iter():
            tag = elem.tag.split("}")[-1]
            if tag not in ("path", "polygon"):
                continue

            fill_str = elem.attrib.get("fill", "")
            if not fill_str and "style" in elem.attrib:
                for part in elem.attrib["style"].split(";"):
                    if part.strip().startswith("fill:"):
                        fill_str = part.strip().split(":")[1].strip()
                        break
            if not fill_str or not fill_str.startswith("#"):
                continue

            c = hex_to_rgb_255(fill_str)
            dist_b = float(np.linalg.norm(c - bg_flat_rgb))
            ink_dists = [float(np.linalg.norm(c - ink_rgb)) for ink_rgb in ink_rgbs]
            min_ink_dist = min(ink_dists)
            if dist_b < min_ink_dist:
                continue

            best_k = int(np.argmin(ink_dists))
            elem.attrib["fill"] = canonical_hexes[best_k]
            if "style" in elem.attrib:
                parts = [p.strip() for p in elem.attrib["style"].split(";") if p.strip()]
                new_parts = [p for p in parts if not p.startswith("fill:")]
                if new_parts:
                    elem.attrib["style"] = ";".join(new_parts)
                else:
                    del elem.attrib["style"]

            backing_elements.append(copy.deepcopy(elem))

        if not backing_elements:
            return 0

        main_tree = ET.parse(svg_path)
        main_root = main_tree.getroot()

        insert_idx = 0
        for i, child in enumerate(list(main_root)):
            ctag = child.tag.split("}")[-1]
            if ctag in ("path", "polygon", "rect"):
                insert_idx = i
                break

        for b_elem in reversed(backing_elements):
            main_root.insert(insert_idx, b_elem)

        main_tree.write(svg_path, encoding="utf-8", xml_declaration=True)
        if verbose:
            print(f"    Injected {len(backing_elements)} internal seam-backing underlay paths into {svg_path}")
        return len(backing_elements)


def run_vector_knockout(
    input_pil: Image.Image,
    input_path: str,
    output_png: str,
    output_svg: str,
    dpi: Tuple[int, int],
    palette: Optional[str] = None,
    inks: int = 0,
    filter_speckle: int = 16,
    merge_hue_tol: float = 12.0,
    esrgan_model: str = "realesrgan-x4plus-anime",
    vtracer_bin: Optional[str] = None,
    svg_bg_thresh: float = 22.0,
    fg_thresh: float = 25.0,
    border_pct: float = 0.025,
    manual_bg_rgb: Optional[np.ndarray] = None,
    protect_interior_boxes: Optional[List[Tuple[int, int, int, int]]] = None,
    protect_boxes: Optional[List[Tuple[int, int, int, int]]] = None,
    cutout_boxes: Optional[List[Tuple[int, int, int, int]]] = None,
    seal_boxes: Optional[List[Tuple[int, int, int, int, int]]] = None,
    protect_polys: Optional[List[np.ndarray]] = None,
    verbose: bool = False,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, float, float, int, List[str]]:
    """
    Executes Candidate B vector prepress knockout workflow:
    Real-ESRGAN (2x) + Continuous Background Field B(x,y) + Spot-Color Palette Determination +
    Strict Spot-Color Ray Unmixing & 9x9 Local Core Support (4px radius at 2x resolution) + Strictly Flat (K+1)-Color Raster +
    Cutout Spline VTracer + Background Path Stripping & Exact Canonical Hex Snapping +
    Clean Scalable Cutout SVG + resvg 300 DPI RGBA Render.
    """
    # Validate explicit spot-color palette upfront before expensive ESRGAN upscaling or background fitting
    explicit_ink_rgbs: Optional[List[np.ndarray]] = None
    explicit_canonical_hexes: Optional[List[str]] = None
    if palette is not None:
        hex_items = [h.strip() for h in palette.split(",") if h.strip()]
        if not hex_items:
            raise ValueError("--palette must specify at least one valid hex color")
        explicit_ink_rgbs = [hex_to_rgb_255(h) for h in hex_items]
        explicit_canonical_hexes = [normalize_hex(h) for h in hex_items]

    w_orig, h_orig = input_pil.size
    w_2x, h_2x = w_orig * 2, h_orig * 2

    # Step 1: Upscale & denoise via Real-ESRGAN to 2x (w_2x, h_2x)
    if verbose:
        print(f"Step 1: Real-ESRGAN upscaling/denoising input to 2x ({w_2x}x{h_2x}) [model={esrgan_model}]...")
    t_esrgan = time.perf_counter()
    pil_2x = run_esrgan_upscale_2x(
        pil_in=input_pil,
        model_name=esrgan_model,
        target_w=w_2x,
        target_h=h_2x,
        verbose=verbose,
    )
    img_2x_u8 = np.array(pil_2x)
    if verbose:
        print(f"Real-ESRGAN completed in {time.perf_counter() - t_esrgan:.2f}s")

    # Step 2: Fit continuous background field B(x, y) on 2x image
    if verbose:
        print("Fitting continuous background field B(x, y) on 2x image...")
    img_2x_f64 = img_2x_u8.astype(np.float64) / 255.0
    B_field, mean_B, sigma_bg, p99_border_dE = fit_background_field(
        img_2x_f64,
        border_pct=border_pct,
        manual_bg_rgb=manual_bg_rgb,
    )
    bg_rgb_255 = (mean_B * 255.0).astype(np.float32)
    B_field_255 = (B_field * 255.0).astype(np.float32)

    # Step 3: Determine Spot-Color Palette
    if explicit_ink_rgbs is not None and explicit_canonical_hexes is not None:
        ink_rgbs = explicit_ink_rgbs
        canonical_hexes = explicit_canonical_hexes
        if verbose:
            print(f"Using explicit spot-color palette ({len(canonical_hexes)} inks): {', '.join(canonical_hexes)}")
    else:
        if verbose:
            print(f"Auto-discovering spot-color palette (target inks={inks}, merge_hue_tol={merge_hue_tol})...")
        ink_rgbs, canonical_hexes = discover_palette(
            img_rgb=img_2x_u8,
            bg_rgb=B_field_255,
            inks=inks,
            merge_hue_tol=merge_hue_tol,
            verbose=verbose,
        )

    # Scale declarative ROI regions to 2x grid
    protect_interior_boxes_2x = [(x0 * 2, y0 * 2, x1 * 2, y1 * 2) for (x0, y0, x1, y1) in (protect_interior_boxes or [])]
    protect_boxes_2x = [(x0 * 2, y0 * 2, x1 * 2, y1 * 2) for (x0, y0, x1, y1) in (protect_boxes or [])]
    cutout_boxes_2x = [(x0 * 2, y0 * 2, x1 * 2, y1 * 2) for (x0, y0, x1, y1) in (cutout_boxes or [])]
    seal_boxes_2x = [(x0 * 2, y0 * 2, x1 * 2, y1 * 2, r * 2) for (x0, y0, x1, y1, r) in (seal_boxes or [])]
    protect_polys_2x = [p * 2 for p in (protect_polys or [])]

    # Step 4: Strict Spot-Color Ray Unmixing & 9x9 Local Core Support
    if verbose:
        print(f"Unmixing 2x raster with {len(ink_rgbs)} spot inks + 9x9 local core support...")
    t_unmix = time.perf_counter()
    P_f32 = img_2x_u8.astype(np.float32)
    flat_quantized, k_star, clean_alpha, dist_to_bg = unmix_spot_colors(
        P=P_f32,
        B=B_field_255,
        ink_rgbs=ink_rgbs,
        bg_flat_rgb=bg_rgb_255,
        protect_interior_boxes_2x=protect_interior_boxes_2x,
        protect_boxes_2x=protect_boxes_2x,
        cutout_boxes_2x=cutout_boxes_2x,
        protect_polys_2x=protect_polys_2x,
        seal_boxes=seal_boxes_2x,
        verbose=verbose,
    )
    if verbose:
        print(f"Ray unmixing & local core support finished in {time.perf_counter() - t_unmix:.2f}s")

    # Step 4b: Pre-VTracer Raster Speckle Consolidation
    fg_mask = clean_alpha >= 0.50
    if filter_speckle > 0:
        k_star = consolidate_raster_speckles(
            k_star=k_star,
            fg_mask=fg_mask,
            ink_rgbs=ink_rgbs,
            filter_speckle=filter_speckle,
            num_passes=2,
            verbose=verbose,
        )
        ink_colors_arr = np.array(ink_rgbs, dtype=np.float32)
        flat_quantized[fg_mask] = np.clip(np.round(ink_colors_arr[k_star[fg_mask]]), 0, 255).astype(np.uint8)

    # Step 5: Run VTracer on flat_quantized raster
    vtracer_exe = resolve_vtracer_bin(vtracer_bin)
    vt_filter_speckle = min(filter_speckle, 4) if filter_speckle > 0 else 0
    with tempfile.TemporaryDirectory() as td:
        temp_flat_png = os.path.join(td, "flat_quantized_2x.png")
        temp_raw_svg = os.path.join(td, "vtracer_raw.svg")
        Image.fromarray(flat_quantized).save(temp_flat_png)

        vtracer_cmd = [
            vtracer_exe,
            "--input", temp_flat_png,
            "--output", temp_raw_svg,
            "--colormode", "color",
            "--hierarchical", "cutout",
            "--mode", "spline",
            "--filter_speckle", str(vt_filter_speckle),
            "--color_precision", "8",
            "--corner_threshold", "55",
        ]
        if verbose:
            print(f"Executing VTracer: {' '.join(vtracer_cmd)}")
        t_vt = time.perf_counter()
        subprocess.run(vtracer_cmd, check=True, timeout=120, capture_output=True, text=True)
        if verbose:
            print(f"VTracer finished in {time.perf_counter() - t_vt:.2f}s (raw SVG size: {os.path.getsize(temp_raw_svg)} bytes)")

        # Step 6: Clean SVG, Strip Background Paths & Snap to Exact Canonical Hexes
        cutout_count = clean_vtracer_svg(
            raw_svg_path=temp_raw_svg,
            out_svg_path=output_svg,
            w_orig=w_orig,
            h_orig=h_orig,
            w_2x=w_2x,
            h_2x=h_2x,
            bg_rgb=bg_rgb_255,
            ink_rgbs=ink_rgbs,
            canonical_hexes=canonical_hexes,
            protect_interior_boxes_2x=protect_interior_boxes_2x,
            protect_boxes_2x=protect_boxes_2x,
            cutout_boxes_2x=cutout_boxes_2x,
            protect_polys_2x=protect_polys_2x,
            verbose=verbose,
        )

        # Step 6b: Inject Seam-Backing Underlay Paths
        backing_count = inject_seam_backing_underlay(
            svg_path=output_svg,
            k_star=k_star,
            fg_mask=fg_mask,
            ink_rgbs=ink_rgbs,
            canonical_hexes=canonical_hexes,
            bg_flat_rgb=bg_rgb_255,
            vtracer_bin=vtracer_bin,
            verbose=verbose,
        )
        if verbose:
            print(f"Injected {backing_count} seam-backing underlay paths")

    # Step 7: Render Clean Cutout SVG to W x H RGBA via resvg_py
    if verbose:
        print(f"Rendering Clean Cutout SVG via resvg_py at {w_orig}x{h_orig} px ({dpi[0]} DPI)...")
    t_render = time.perf_counter()
    png_bytes = resvg_py.svg_to_bytes(svg_path=output_svg, width=w_orig, height=h_orig)
    out_pil = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    os.makedirs(os.path.dirname(os.path.abspath(output_png)), exist_ok=True)
    out_pil.save(output_png, dpi=dpi)
    if verbose:
        print(f"Rendered PNG saved to {output_png} in {time.perf_counter() - t_render:.2f}s")

    rgba_u8 = np.array(out_pil)
    F_out = rgba_u8[:, :, :3].astype(np.float64) / 255.0
    alpha_out = rgba_u8[:, :, 3].astype(np.float64) / 255.0

    return F_out, alpha_out, mean_B, sigma_bg, p99_border_dE, cutout_count, canonical_hexes


# =============================================================================
# STAGE 6: Prepress Verification Sheet & 1:1 Native Resolution Crops
# =============================================================================

def composite_on_backdrop(F: np.ndarray, alpha: np.ndarray, bg_rgb: np.ndarray) -> np.ndarray:
    """Composites unmixed foreground and alpha over a constant background RGB."""
    comp = F * alpha[:, :, None] + bg_rgb[None, None, :] * (1.0 - alpha[:, :, None])
    return (np.clip(comp, 0.0, 1.0) * 255.0).round().astype(np.uint8)


def generate_verify_composite(
    F_final: np.ndarray,
    alpha_final: np.ndarray,
    output_path: str,
) -> None:
    """
    Generates a clean, labeled 2x2 prepress verification sheet:
    - Top-Left: Over Dark (#18181B)
    - Top-Right: Over White (#FFFFFF)
    - Bottom-Left: Over Warm Cream (#F4EFE6)
    - Bottom-Right: Alpha Mask ([alpha, alpha, alpha])
    """
    H, W, _ = F_final.shape

    dark_bg = np.array([24, 24, 27], dtype=np.float64) / 255.0
    white_bg = np.array([255, 255, 255], dtype=np.float64) / 255.0
    cream_bg = np.array([244, 239, 230], dtype=np.float64) / 255.0

    panel_dark = composite_on_backdrop(F_final, alpha_final, dark_bg)
    panel_white = composite_on_backdrop(F_final, alpha_final, white_bg)
    panel_cream = composite_on_backdrop(F_final, alpha_final, cream_bg)
    alpha_u8 = (np.clip(alpha_final, 0.0, 1.0) * 255.0).round().astype(np.uint8)
    panel_mask = np.stack([alpha_u8, alpha_u8, alpha_u8], axis=-1)

    header_h = max(36, int(H * 0.025))
    font_size = max(18, int(header_h * 0.55))
    font = get_system_font(font_size)

    def make_cell(panel_arr: np.ndarray, label: str) -> Image.Image:
        cell_img = Image.new("RGB", (W, H + header_h), color=(20, 20, 24))
        draw = ImageDraw.Draw(cell_img)
        bbox = draw.textbbox((0, 0), label, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        tx = (W - tw) // 2
        ty = (header_h - th) // 2
        draw.text((tx, ty), label, fill=(240, 240, 245), font=font)
        cell_img.paste(Image.fromarray(panel_arr), (0, header_h))
        return cell_img

    cell_dark = make_cell(panel_dark, "Over Dark (#18181B)")
    cell_white = make_cell(panel_white, "Over White (#FFFFFF)")
    cell_cream = make_cell(panel_cream, "Over Warm Cream (#F4EFE6)")
    cell_mask = make_cell(panel_mask, "Alpha Matte Mask")

    # Arrange 2x2
    sheet_w = 2 * W
    sheet_h = 2 * (H + header_h)
    sheet = Image.new("RGB", (sheet_w, sheet_h), color=(15, 15, 18))
    sheet.paste(cell_dark, (0, 0))
    sheet.paste(cell_white, (W, 0))
    sheet.paste(cell_cream, (0, H + header_h))
    sheet.paste(cell_mask, (W, H + header_h))

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    sheet.save(output_path, dpi=(300, 300))


def select_diverse_crops(
    H: int,
    W: int,
    thin_ridges: np.ndarray,
    unknown_mask: np.ndarray,
    valid_cutouts: np.ndarray,
    silhouette_mask: np.ndarray,
    crop_size: int = 400,
) -> List[Tuple[int, int, int, int, str]]:
    """
    Selects 4 diverse, non-overlapping crop windows scoring high on:
    1. Thin-stroke ridge density
    2. Transition/unknown-band perimeter complexity
    3. Interior cutouts/counters
    4. Outer silhouette edge contrast
    """
    cw = min(crop_size, W)
    ch = min(crop_size, H)

    # Compute fast 2D integral images
    int_ridge = cv2.integral(thin_ridges.astype(np.float32))
    int_unknown = cv2.integral(unknown_mask.astype(np.float32))
    int_cutout = cv2.integral(valid_cutouts.astype(np.float32))
    int_sil = cv2.integral(silhouette_mask.astype(np.float32))

    def get_sum(ii: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> float:
        return float(ii[y1, x1] - ii[y0, x1] - ii[y1, x0] + ii[y0, x0])

    step = max(30, crop_size // 8)
    cand_x = list(range(0, W - cw + 1, step))
    cand_y = list(range(0, H - ch + 1, step))
    if cand_x[-1] != (W - cw):
        cand_x.append(W - cw)
    if cand_y[-1] != (H - ch):
        cand_y.append(H - ch)

    candidates = [(x, y, x + cw, y + ch) for y in cand_y for x in cand_x]

    chosen_boxes: List[Tuple[int, int, int, int]] = []
    chosen_crops: List[Tuple[int, int, int, int, str]] = []

    def is_overlapping(box: Tuple[int, int, int, int]) -> bool:
        bx0, by0, bx1, by1 = box
        for cx0, cy0, cx1, cy1 in chosen_boxes:
            ix0 = max(bx0, cx0)
            iy0 = max(by0, cy0)
            ix1 = min(bx1, cx1)
            iy1 = min(by1, cy1)
            if ix1 > ix0 and iy1 > iy0:
                inter_area = (ix1 - ix0) * (iy1 - iy0)
                if inter_area > 0.15 * cw * ch:
                    return True
        return False

    categories = [
        ("Thin Filigree & Fine Ridges", int_ridge),
        ("Transition & Unknown-Band Perimeter", int_unknown),
        ("Interior Cutouts & Counters", int_cutout),
        ("Outer Silhouette Edge Contrast", int_sil),
    ]

    for cat_name, int_map in categories:
        best_cand = None
        best_score = -1.0
        for box in candidates:
            if is_overlapping(box):
                continue
            score = get_sum(int_map, *box)
            if score > best_score:
                best_score = score
                best_cand = box

        if best_cand is None or best_score <= 0.0:
            # Fallback: pick highest remaining score or center region
            for box in candidates:
                if not is_overlapping(box):
                    best_cand = box
                    break

        if best_cand is not None:
            chosen_boxes.append(best_cand)
            chosen_crops.append((*best_cand, cat_name))

    return chosen_crops


def generate_detail_crops(
    img_orig_u8: np.ndarray,
    F_final: np.ndarray,
    alpha_final: np.ndarray,
    crops_info: List[Tuple[int, int, int, int, str]],
    output_dir: str,
) -> List[str]:
    """
    Generates 4-column 1:1 native-resolution diagnostic strips for each crop region:
    Original Input | Over Dark (#18181B) | Over White (#FFFFFF) | Over Warm Cream (#F4EFE6)
    """
    os.makedirs(output_dir, exist_ok=True)
    saved_paths = []

    dark_bg = np.array([24, 24, 27], dtype=np.float64) / 255.0
    white_bg = np.array([255, 255, 255], dtype=np.float64) / 255.0
    cream_bg = np.array([244, 239, 230], dtype=np.float64) / 255.0

    col_titles = [
        "Original Input",
        "Over Dark #18181B",
        "Over White #FFFFFF",
        "Over Cream #F4EFE6",
    ]

    font_title = get_system_font(18)
    font_col = get_system_font(15)

    pad = 8
    header_h = 64

    H, W = img_orig_u8.shape[:2]
    for idx, (x0, y0, x1, y1, label) in enumerate(crops_info):
        x0, y0, x1, y1 = clamp_box(x0, y0, x1, y1, W, H)
        cw = x1 - x0
        ch = y1 - y0
        if cw <= 0 or ch <= 0:
            continue

        crop_orig = img_orig_u8[y0:y1, x0:x1]
        crop_F = F_final[y0:y1, x0:x1]
        crop_alpha = alpha_final[y0:y1, x0:x1]

        crop_dark = composite_on_backdrop(crop_F, crop_alpha, dark_bg)
        crop_white = composite_on_backdrop(crop_F, crop_alpha, white_bg)
        crop_cream = composite_on_backdrop(crop_F, crop_alpha, cream_bg)

        col_images = [crop_orig, crop_dark, crop_white, crop_cream]

        strip_w = 4 * cw + 5 * pad
        strip_h = ch + header_h + 2 * pad

        strip = Image.new("RGB", (strip_w, strip_h), color=(18, 18, 22))
        draw = ImageDraw.Draw(strip)

        # Main Strip Header
        title_text = f"1:1 Native Resolution Diagnostic Crop #{idx+1}: {label} [X:{x0}-{x1}, Y:{y0}-{y1}]"
        draw.text((pad + 4, 10), title_text, fill=(245, 245, 250), font=font_title)

        # Columns
        for c_idx, (col_img, col_name) in enumerate(zip(col_images, col_titles)):
            cx = pad + c_idx * (cw + pad)
            cy = header_h

            # Column title
            bbox = draw.textbbox((0, 0), col_name, font=font_col)
            tw = bbox[2] - bbox[0]
            tx = cx + (cw - tw) // 2
            draw.text((tx, 38), col_name, fill=(210, 215, 225), font=font_col)

            # Paste image
            strip.paste(Image.fromarray(col_img), (cx, cy))
            draw.rectangle([cx - 1, cy - 1, cx + cw, cy + ch], outline=(60, 60, 70), width=1)

        slug = "".join(c if c.isalnum() else "_" for c in label.lower()).strip("_")
        crop_filename = f"crop_{idx+1}_{slug}.png"
        out_crop_path = os.path.join(output_dir, crop_filename)
        strip.save(out_crop_path, dpi=(300, 300))
        saved_paths.append(out_crop_path)

    return saved_paths


# =============================================================================
# MAIN CLI ENTRYPOINT
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Universal Prepress Vectorize & Decontamination (Spot-Color Unmixing + Cutout Spline VTracer SVG)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input", help="Path to input image file (JPEG, PNG, WebP)")
    parser.add_argument("-o", "--output", help="Destination transparent PNG path")
    parser.add_argument(
        "--mode",
        choices=["vector", "dtg", "dtf", "screenprint"],
        default="vector",
        help="Target printing technology mode (vector: Spot-Color Ray Unmixing + Cutout Spline VTracer SVG, dtg: continuous unmixing, dtf: firm alpha floor, screenprint: spot-color quantization)",
    )
    parser.add_argument(
        "--svg-output",
        type=str,
        help="Optional destination path for output .svg file (defaults to <stem>.svg when --mode vector)",
    )
    parser.add_argument(
        "--esrgan-model",
        choices=["realesrgan-x4plus-anime", "realesr-animevideov3", "none"],
        default="realesrgan-x4plus-anime",
        help="Real-ESRGAN model for vector mode upscaling/denoising",
    )
    parser.add_argument(
        "--vtracer-bin",
        type=str,
        help="Path to vtracer binary (auto-resolved from PATH or ~/.cargo/bin/vtracer if not provided)",
    )
    parser.add_argument(
        "--svg-bg-thresh",
        type=float,
        default=22.0,
        help="CIELAB delta_E threshold for deleting background-matching SVG <path> elements",
    )
    parser.add_argument(
        "--scale",
        "--upscale",
        type=float,
        default=1.0,
        help="Optional upscale factor using Lanczos resampling (default: 1.0, preserves native resolution)",
    )
    parser.add_argument(
        "--quantize",
        action="store_true",
        help="Shortcut for --mode screenprint",
    )
    parser.add_argument(
        "--dtf-alpha-floor",
        type=float,
        default=0.15,
        help="DTF hot-melt adhesive alpha cutoff ramp floor",
    )
    parser.add_argument(
        "-k",
        "--colors",
        type=int,
        default=8,
        help="Number of spot-color inks for --mode screenprint",
    )
    parser.add_argument(
        "--palette",
        type=str,
        help="Explicit comma-separated hex colors (e.g. '#FFFFFF,#000000,#E5A125')",
    )
    parser.add_argument(
        "--inks",
        type=int,
        default=0,
        help="Number of foreground spot inks (K) for --mode vector (0 = auto-discover 2..6 in CIELAB)",
    )
    parser.add_argument(
        "--filter-speckle",
        type=int,
        default=16,
        help="Raster speckle consolidation threshold: absorbs low-contrast transition slivers < X px into adjacent inks before VTracer (default: 16; VTracer spline filter_speckle is clamped to min(X, 4))",
    )
    parser.add_argument(
        "--merge-hue-tol",
        type=float,
        default=12.0,
        help="CIELAB delta_E tolerance for merging similar hues during auto-palette discovery (default: 12.0)",
    )
    parser.add_argument(
        "--chroma-smooth",
        dest="chroma_smooth",
        action="store_true",
        default=True,
        help="Enable CIELAB bilateral chroma smoothing on a* and b*",
    )
    parser.add_argument(
        "--no-chroma-smooth",
        dest="chroma_smooth",
        action="store_false",
        help="Disable CIELAB bilateral chroma smoothing",
    )
    parser.add_argument(
        "--chroma-sigma-color",
        type=float,
        default=12.0,
        help="Bilateral filter sigmaColor for chroma channels",
    )
    parser.add_argument(
        "--chroma-sigma-space",
        type=float,
        default=5.0,
        help="Bilateral filter sigmaSpace for chroma channels",
    )
    parser.add_argument(
        "--border-pct",
        type=float,
        default=0.025,
        help="Outer border fraction used to fit continuous background field B(x,y)",
    )
    parser.add_argument(
        "--bg-color",
        type=str,
        help="Manual background color hex override (e.g. '#18181B')",
    )
    parser.add_argument(
        "--bg-thresh",
        type=float,
        default=6.5,
        help="CIELAB delta_E threshold for background seed and interior cutout validation",
    )
    parser.add_argument(
        "--fg-thresh",
        type=float,
        default=40.0,
        help="CIELAB delta_E threshold for high-contrast foreground core",
    )
    parser.add_argument(
        "--thin-thresh",
        type=float,
        default=32.0,
        help="CIELAB delta_E threshold for thin 1-2px ridge preservation",
    )
    parser.add_argument(
        "--unknown-band-px",
        type=float,
        default=6.0,
        help="Euclidean distance boundary from true background into object interior",
    )
    parser.add_argument(
        "--min-cutout-area",
        type=int,
        default=6,
        help="Minimum pixel area for accepting interior negative space cutouts",
    )
    parser.add_argument(
        "--protect-interior-box",
        action="append",
        default=[],
        help="Suppress interior cutout holes in x0,y0,x1,y1 while preserving outer silhouette matting (repeatable)",
    )
    parser.add_argument(
        "--protect-box",
        action="append",
        default=[],
        help="Force definite foreground (trimap = 1.0) in x0,y0,x1,y1 (repeatable)",
    )
    parser.add_argument(
        "--protect-poly",
        action="append",
        default=[],
        help="Force definite solid foreground inside polygon 'x1,y1,x2,y2,x3,y3,...' (repeatable)",
    )
    parser.add_argument(
        "--seal-box",
        action="append",
        default=[],
        help="Topological outline gap sealing 'x0,y0,x1,y1[:radius]' to bridge outline gaps (repeatable)",
    )
    parser.add_argument(
        "--cutout-box",
        action="append",
        default=[],
        help="Relax interior cutout threshold inside x0,y0,x1,y1 for tiny holes (repeatable)",
    )
    parser.add_argument(
        "-p",
        "--preview",
        "--verify-composite",
        dest="verify_composite",
        help="Output path for 2x2 prepress verification composite sheet",
    )
    parser.add_argument(
        "--verify-crops-dir",
        "--crops-dir",
        dest="verify_crops_dir",
        help="Directory to save 1:1 native-resolution diagnostic inspection crop strips",
    )
    parser.add_argument(
        "--crop",
        action="append",
        default=[],
        help="Manual 1:1 crop specification 'x,y,w,h:label' or 'x0,y0,x1,y1:label' (repeatable)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output summary statistics in structured JSON format",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print verbose diagnostic processing logs",
    )

    args = parser.parse_args()

    mode = "screenprint" if args.quantize else args.mode

    if not os.path.exists(args.input):
        sys.stderr.write(f"Error: Input file does not exist: {args.input}\n")
        sys.exit(1)

    t_start = time.perf_counter()

    # Determine output path
    stem, _ = os.path.splitext(args.input)
    output_path = args.output or f"{stem}_transparent.png"
    out_stem, _ = os.path.splitext(output_path)
    svg_output_path = args.svg_output or f"{out_stem}.svg"

    # Load input image
    input_pil = Image.open(args.input).convert("RGB")
    w_orig, h_orig = input_pil.size
    dpi = input_pil.info.get("dpi", (300, 300))
    if not isinstance(dpi, tuple) or len(dpi) != 2 or dpi[0] < 72:
        dpi = (300, 300)

    if args.scale > 1.0:
        new_w = int(round(w_orig * args.scale))
        new_h = int(round(h_orig * args.scale))
        if args.verbose:
            print(f"Upscaling input artwork from {w_orig}x{h_orig} to {new_w}x{new_h} (scale={args.scale}) via Lanczos...")
        input_pil = input_pil.resize((new_w, new_h), Image.Resampling.LANCZOS)
        w_orig, h_orig = input_pil.size

    img_u8 = np.array(input_pil)
    img_f64 = img_u8.astype(np.float64) / 255.0

    if args.verbose:
        print(f"Loaded input artwork: {args.input} ({w_orig}x{h_orig} px, DPI: {dpi})")

    # Parse ROI boxes and polygons
    protect_interior_boxes = [parse_box(b) for b in args.protect_interior_box]
    protect_boxes = [parse_box(b) for b in args.protect_box]
    cutout_boxes = [parse_box(b) for b in args.cutout_box]
    seal_boxes = [parse_seal_box(b) for b in args.seal_box]
    protect_polys = [parse_poly(p) for p in args.protect_poly]

    manual_bg = hex_to_rgb_float(args.bg_color) if args.bg_color else None

    if mode == "vector":
        vector_fg_thresh = 25.0 if args.fg_thresh == 40.0 else max(25.0, args.fg_thresh)

        F_out, alpha_out, mean_B, sigma_bg, p99_border_dE, cutout_count, palette_hex = run_vector_knockout(
            input_pil=input_pil,
            input_path=args.input,
            output_png=output_path,
            output_svg=svg_output_path,
            dpi=dpi,
            palette=args.palette,
            inks=args.inks,
            filter_speckle=args.filter_speckle,
            merge_hue_tol=args.merge_hue_tol,
            esrgan_model=args.esrgan_model,
            vtracer_bin=args.vtracer_bin,
            svg_bg_thresh=args.svg_bg_thresh,
            fg_thresh=vector_fg_thresh,
            border_pct=args.border_pct,
            manual_bg_rgb=manual_bg,
            protect_interior_boxes=protect_interior_boxes,
            protect_boxes=protect_boxes,
            cutout_boxes=cutout_boxes,
            seal_boxes=seal_boxes,
            protect_polys=protect_polys,
            verbose=args.verbose,
        )
        bg_hex = rgb_to_hex(mean_B)
        unmixing_time = 0.0
    else:
        # Stage 1: Edge-preserving CIELAB chroma smoothing
        if args.chroma_smooth:
            if args.verbose:
                print(f"Applying Stage 1 CIELAB bilateral chroma smoothing (sigmaColor={args.chroma_sigma_color}, sigmaSpace={args.chroma_sigma_space})...")
            img_proc = apply_chroma_smoothing(
                img_f64,
                sigma_color=args.chroma_sigma_color,
                sigma_space=args.chroma_sigma_space,
            )
        else:
            img_proc = img_f64.copy()

        # Stage 2: Fit continuous background field B(x, y)
        if args.verbose:
            print(f"Fitting Stage 2 continuous background field B(x, y) (border_pct={args.border_pct})...")
        B_field, mean_B, sigma_bg, p99_border_dE = fit_background_field(
            img_f64,
            border_pct=args.border_pct,
            manual_bg_rgb=manual_bg,
        )
        bg_hex = rgb_to_hex(mean_B)

        # Compute CIELAB delta_E
        delta_E = compute_lab_delta_e(img_proc, B_field)
        delta_E_raw = compute_lab_delta_e(img_f64, B_field)

        # Stage 3: Topological Trimap
        if args.verbose:
            print(f"Building Stage 3 topological trimap (t_bg={args.bg_thresh}, t_fg={args.fg_thresh}, t_thin={args.thin_thresh})...")
        trimap, true_bg, valid_cutout_mask, thin_ridges, dist_from_bg, cutout_count = build_topological_trimap(
            img_proc=img_proc,
            B_field=B_field,
            delta_E=delta_E,
            delta_E_raw=delta_E_raw,
            t_bg=args.bg_thresh,
            t_fg=args.fg_thresh,
            t_thin=args.thin_thresh,
            unknown_band_px=args.unknown_band_px,
            min_cutout_area=args.min_cutout_area,
            protect_interior_boxes=protect_interior_boxes,
            protect_boxes=protect_boxes,
            cutout_boxes=cutout_boxes,
            seal_boxes=seal_boxes,
            protect_polys=protect_polys,
        )

        # Stage 4: Closed-Form Matting + Known-B Unmixing
        if args.verbose:
            print("Executing Stage 4 Closed-Form Matting + Two-Stage Known-B Color-Line Unmixing...")
        F_final, alpha_final, F_init, unmixing_time = run_pymatting_unmixing(
            img_proc=img_proc,
            trimap=trimap,
            B_field=B_field,
            delta_E=delta_E,
            t_bg=args.bg_thresh,
            verbose=args.verbose,
        )

        # Stage 5: Decoupled Mode Post-Processing
        explicit_palette = None
        if args.palette is not None:
            explicit_palette = [hex_to_rgb_float(h) for h in args.palette.split(",") if h.strip()]
            if not explicit_palette:
                raise ValueError("--palette must specify at least one valid hex color")

        F_out, alpha_out, palette_hex = postprocess_mode(
            F_final=F_final,
            alpha_final=alpha_final,
            mode=mode,
            dtf_alpha_floor=args.dtf_alpha_floor,
            colors=args.colors,
            explicit_palette=explicit_palette,
            verbose=args.verbose,
        )

        # Save output transparent PNG
        out_rgba = np.zeros((h_orig, w_orig, 4), dtype=np.uint8)
        out_rgba[:, :, :3] = (np.clip(F_out, 0.0, 1.0) * 255.0).round().astype(np.uint8)
        out_rgba[:, :, 3] = (np.clip(alpha_out, 0.0, 1.0) * 255.0).round().astype(np.uint8)
        out_pil = Image.fromarray(out_rgba, mode="RGBA")

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        out_pil.save(output_path, dpi=dpi)

    # Stage 6: Prepress Verification Sheet
    if args.verify_composite:
        if args.verbose:
            print(f"Generating 2x2 prepress verification composite sheet: {args.verify_composite}...")
        generate_verify_composite(F_out, alpha_out, args.verify_composite)

    # Stage 6: 1:1 Native Resolution Diagnostic Crops
    saved_crops = []
    if args.verify_crops_dir:
        if args.verbose:
            print(f"Selecting and rendering 1:1 native resolution diagnostic crops into {args.verify_crops_dir}...")
        # Silhouette edge mask
        alpha_grad_x = cv2.Sobel(alpha_out.astype(np.float32), cv2.CV_32F, 1, 0, ksize=3)
        alpha_grad_y = cv2.Sobel(alpha_out.astype(np.float32), cv2.CV_32F, 0, 1, ksize=3)
        silhouette_mask = np.sqrt(alpha_grad_x**2 + alpha_grad_y**2)

        if mode == "vector":
            gray = cv2.cvtColor(img_u8, cv2.COLOR_RGB2GRAY).astype(np.float32)
            grad_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
            grad_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
            edge_mag = np.sqrt(grad_x**2 + grad_y**2)
            fine_ridges = (edge_mag > np.percentile(edge_mag, 85)) & (alpha_out > 0.1)
            unknown_mask = (alpha_out > 0.05) & (alpha_out < 0.95)
            if np.sum(unknown_mask) < 50:
                unknown_mask = silhouette_mask > 0.2

            bg_comp = (alpha_out == 0.0).astype(np.uint8)
            n_lbl, lbls, stats_c, _ = cv2.connectedComponentsWithStats(bg_comp, connectivity=8)
            valid_cutout_mask = np.zeros_like(alpha_out, dtype=bool)
            H_c, W_c = alpha_out.shape
            for l_i in range(1, n_lbl):
                x, y, w, h, _ = stats_c[l_i]
                if x > 0 and y > 0 and (x + w) < W_c and (y + h) < H_c:
                    valid_cutout_mask[lbls == l_i] = True
        else:
            fine_ridges = thin_ridges & (dist_from_bg <= (args.unknown_band_px + 2.0))
            unknown_mask = (trimap == 0.5)

        auto_crops = select_diverse_crops(
            H=h_orig,
            W=w_orig,
            thin_ridges=fine_ridges,
            unknown_mask=unknown_mask,
            valid_cutouts=valid_cutout_mask,
            silhouette_mask=silhouette_mask,
            crop_size=400,
        )

        manual_crops = [parse_crop_spec(c) for c in args.crop]
        all_crops = auto_crops + manual_crops

        saved_crops = generate_detail_crops(
            img_orig_u8=img_u8,
            F_final=F_out,
            alpha_final=alpha_out,
            crops_info=all_crops,
            output_dir=args.verify_crops_dir,
        )

    t_total = time.perf_counter() - t_start

    # Compute distribution statistics
    total_pixels = float(h_orig * w_orig)
    n_trans = float(np.sum(alpha_out == 0.0))
    n_solid = float(np.sum(alpha_out == 1.0))
    n_semi = total_pixels - n_trans - n_solid

    pct_trans = (n_trans / total_pixels) * 100.0
    pct_semi = (n_semi / total_pixels) * 100.0
    pct_solid = (n_solid / total_pixels) * 100.0

    summary = {
        "status": "success",
        "input": args.input,
        "output": output_path,
        "svg_output": svg_output_path if mode == "vector" else None,
        "mode": mode,
        "esrgan_model": args.esrgan_model if mode == "vector" else None,
        "dimensions": [w_orig, h_orig],
        "dpi": list(dpi),
        "runtime_seconds": round(t_total, 2),
        "unmixing_seconds": round(unmixing_time, 2),
        "background_rgb": [int(round(c * 255.0)) for c in mean_B],
        "background_hex": bg_hex,
        "sigma_bg_8bit": round(sigma_bg * 255.0, 2),
        "p99_border_delta_e": round(p99_border_dE, 2),
        "cutouts_detected": cutout_count,
        "alpha_distribution": {
            "transparent_pct": round(pct_trans, 1),
            "semi_transparent_pct": round(pct_semi, 1),
            "solid_opaque_pct": round(pct_solid, 1),
        },
        "palette": palette_hex,
        "verify_composite": args.verify_composite,
        "verify_crops": saved_crops,
    }

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"=== PREPRESS VECTORIZE COMPLETE ({t_total:.2f}s) ===")
        print(f"  Dimensions:        {w_orig}x{h_orig} px @ {dpi[0]} DPI (100% native preserved)")
        print(f"  Print Mode:        {mode.upper()}" + (" (Spot-Color Unmixing + Cutout Spline VTracer SVG)" if mode == "vector" else ""))
        if mode == "vector":
            print(f"  ESRGAN Model:      {args.esrgan_model}")
            print(f"  Output SVG:        {svg_output_path}")
        print(f"  Background Field:  {bg_hex} (border sigma: {sigma_bg * 255.0:.2f})")
        print(f"  Interior Cutouts:  {cutout_count} verified")
        print(f"  Alpha Matte:       {pct_trans:.1f}% transparent, {pct_semi:.1f}% anti-aliased edge, {pct_solid:.1f}% solid")
        if palette_hex:
            print(f"  Spot Palette:      {len(palette_hex)} colors ({', '.join(palette_hex)})")
        print(f"  Output RGBA:       {output_path}")
        if args.verify_composite:
            print(f"  2x2 Prepress Sheet:{args.verify_composite}")
        if saved_crops:
            print(f"  1:1 Detail Crops:  {len(saved_crops)} diagnostic strips in {args.verify_crops_dir}")



if __name__ == "__main__":
    main()
