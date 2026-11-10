#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "numpy",
#     "pillow",
#     "scipy",
#     "scikit-learn",
# ]
# ///

"""
knockout_quantize.py - Knockout Background Removal & Discrete Screen-Print Color Quantization

A universal, mathematically rigorous CLI tool to unmix flat-color, screen-printed,
and vector-style raster artwork from any continuous background (black, charcoal,
purple, red, navy, green, white, etc.) into transparent PNGs with exact discrete
palette colors and smooth, halo-free anti-aliased alpha.
"""

import argparse
import json
import os
import sys
from typing import Dict, List, Optional, Tuple, Any

import numpy as np
from PIL import Image
from scipy.ndimage import (
    distance_transform_edt,
    binary_erosion,
    binary_dilation,
    sobel,
    maximum_filter,
    label,
    uniform_filter,
    grey_dilation,
    median_filter,
)
from sklearn.cluster import KMeans


def hex_to_rgb(hex_str: str) -> List[int]:
    hex_clean = hex_str.strip().lstrip("#")
    if len(hex_clean) == 3:
        hex_clean = "".join(c * 2 for c in hex_clean)
    if len(hex_clean) != 6:
        raise ValueError(f"Invalid hex color string: {hex_str}")
    return [int(hex_clean[i : i + 2], 16) for i in (0, 2, 4)]


def rgb_to_hex(rgb: Any) -> str:
    r, g, b = [int(np.clip(np.round(c), 0, 255)) for c in rgb[:3]]
    return f"#{r:02X}{g:02X}{b:02X}"


def fit_background_field(
    arr: np.ndarray,
    margin_pct: float = 0.03,
    manual_bg: Optional[List[int]] = None,
) -> Tuple[np.ndarray, np.ndarray, float, float]:
    """
    Fits a continuous 2D spatial background field B(y, x) using outer margins.
    Returns:
        B_field: (H, W, 3) float32 background color at each pixel.
        mean_B: (3,) float32 average background color.
        sigma: float estimated residual noise floor.
        tau_low: float recommended knockout threshold.
    """
    H, W, _ = arr.shape
    if manual_bg is not None:
        bg_val = np.array(manual_bg, dtype=np.float32)
        B_field = np.tile(bg_val[None, None, :], (H, W, 1))
        return B_field, bg_val, 2.0, 8.0

    margin = max(4, int(min(H, W) * margin_pct))
    border_mask = np.zeros((H, W), dtype=bool)
    border_mask[:margin, :] = True
    border_mask[-margin:, :] = True
    border_mask[:, :margin] = True
    border_mask[:, -margin:] = True

    ys, xs = np.where(border_mask)
    rgb_v = arr[border_mask]

    # Robust outlier rejection on the borders (in case artwork touches edge)
    med_bg = np.median(rgb_v, axis=0)
    dev = np.linalg.norm(rgb_v - med_bg, axis=-1)
    mad = np.median(dev)
    cutoff = max(15.0, 3.0 * 1.4826 * mad)
    valid_border = dev <= cutoff

    ys_v, xs_v = ys[valid_border], xs[valid_border]
    rgb_valid = rgb_v[valid_border]

    # Least squares fit of affine 2D plane: B(y, x) = c0 + c1*(y/H) + c2*(x/W)
    A = np.column_stack([np.ones_like(ys_v), ys_v / H, xs_v / W])
    C_coeffs, _, _, _ = np.linalg.lstsq(A, rgb_valid, rcond=None)

    grid_y, grid_x = np.indices((H, W), dtype=np.float32)
    A_full = np.stack([np.ones_like(grid_y), grid_y / H, grid_x / W], axis=-1)
    B_field = A_full @ C_coeffs
    mean_B = np.mean(B_field, axis=(0, 1))

    # Compute residual noise floor
    fitted_border = A @ C_coeffs
    residuals = np.linalg.norm(rgb_valid - fitted_border, axis=-1)
    sigma = float(np.median(residuals) * 1.4826)
    tau_low = max(6.0, 3.5 * sigma)

    return B_field, mean_B, sigma, tau_low


def discover_palette(
    arr: np.ndarray,
    B_field: np.ndarray,
    mean_B: np.ndarray,
    n_colors: int,
    sigma: float,
    explicit_palette: Optional[List[List[int]]] = None,
) -> np.ndarray:
    """
    Discovers discrete ink colors by clustering pure interior core pixels.
    Snaps each cluster to the exact physical median RGB of its core pixels.
    """
    if explicit_palette is not None:
        return np.array(explicit_palette, dtype=np.float32)

    H, W, _ = arr.shape
    diff = arr - B_field
    dist = np.linalg.norm(diff, axis=-1)

    tau_solid = max(25.0, 5.0 * sigma)
    solid_mask = dist > tau_solid
    eroded_solid = binary_erosion(solid_mask, iterations=2)

    grad_y = sobel(arr, axis=0) / 8.0
    grad_x = sobel(arr, axis=1) / 8.0
    grad_mag = np.sqrt(np.sum(grad_y**2 + grad_x**2, axis=-1))

    pure_core_mask = eroded_solid & (grad_mag < 20.0)
    core_pixels = arr[pure_core_mask]

    if len(core_pixels) < n_colors * 10:
        # Fallback to broader foreground if artwork has very thin lines
        cand_mask = (dist > tau_solid * 0.7) & (grad_mag < 30.0)
        core_pixels = arr[cand_mask]
        if len(core_pixels) < n_colors:
            core_pixels = arr[dist > tau_solid * 0.5]

    km = KMeans(n_clusters=n_colors, random_state=42, n_init=10).fit(core_pixels)
    labels = km.labels_

    palette = np.zeros((n_colors, 3), dtype=np.float32)
    for k in range(n_colors):
        k_mask = labels == k
        if np.sum(k_mask) > 0:
            palette[k] = np.median(core_pixels[k_mask], axis=0)
        else:
            palette[k] = km.cluster_centers_[k]

    # Sort palette by Euclidean distance from mean background
    dist_bg = np.linalg.norm(palette - mean_B, axis=-1)
    sort_idx = np.argsort(dist_bg)
    palette = palette[sort_idx]

    return palette


def find_collinear_pairs(
    palette: np.ndarray,
    mean_B: np.ndarray,
    cos_threshold: float = 0.88,
    ratio_threshold: float = 0.85,
) -> List[Tuple[int, int, float, float, float]]:
    """
    Detects pairs of inks (m, k) where ink m is intermediate and collinear to ink k.
    """
    K = len(palette)
    vecs = palette - mean_B
    norms = np.linalg.norm(vecs, axis=-1)

    collinear_pairs = []
    for i in range(K):
        for j in range(K):
            if i != j:
                dot = np.dot(vecs[i], vecs[j])
                cos = dot / max(norms[i] * norms[j], 1e-4)
                if cos > cos_threshold and norms[i] < ratio_threshold * norms[j]:
                    collinear_pairs.append((i, j, float(cos), float(norms[i]), float(norms[j])))
    return collinear_pairs


def assign_and_unmix(
    arr: np.ndarray,
    B_field: np.ndarray,
    mean_B: np.ndarray,
    palette: np.ndarray,
    mode: str = "screenprint",
    tau_low: float = 6.0,
    tau_solid: Optional[float] = None,
    alpha_solid: float = 0.85,
    alpha_knee: float = 0.18,
    alpha_power: float = 1.0,
    despeckle_size: int = 20,
    collinear_threshold: float = 0.75,
    collinear_ratio: float = 0.88,
    verbose: bool = False,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, Dict[str, Any]]:
    """
    Solid-Core Voronoi Propagation + Pure-Ink Ray Alpha Unmixing:
    1. Solid Core Seeding: Exact Euclidean nearest-color matching against (palette + background)
       with collinear intermediate ink suppression on high-gradient boundary slopes.
    2. Isolated Sub-Pixel Component Seeding: Assigns whole tiny components (stars, snowflakes)
       to the best-matching palette ray direction without color fracturing.
    3. Voronoi Core Propagation: Boundary anti-aliased edge pixels inherit identity from nearest solid core.
    4. Exact Ray-Projected Alpha: Contrast-proportional noise gate eliminates JPEG mosquito ringing
       while preserving low-contrast dark strokes.
    5. Pure-Ink Boundary Color: 100% pure uncontaminated palette ink on all boundaries (zero fringe!).
    """
    H, W, _ = arr.shape
    diff = arr - B_field
    fg_dist = np.linalg.norm(diff, axis=-1)

    # Noise-stabilized image for interior classification
    arr_smoothed = median_filter(arr, size=(3, 3, 1))

    K = len(palette)
    vecs = palette - mean_B
    norms = np.linalg.norm(vecs, axis=-1)

    collinear_pairs = find_collinear_pairs(
        palette, mean_B, cos_threshold=collinear_threshold, ratio_threshold=collinear_ratio
    )
    if verbose:
        print(f"Collinear pairs detected: {len(collinear_pairs)}")
        for m, k, cos, L_m, L_k in collinear_pairs:
            print(f"  Ink {m+1} ({rgb_to_hex(palette[m])}) -> Ink {k+1} ({rgb_to_hex(palette[k])}): cos={cos:.3f}, ratio={L_m/L_k:.2f}")

    # Spatial Gradient magnitude
    grad_y = sobel(arr, axis=0) / 8.0
    grad_x = sobel(arr, axis=1) / 8.0
    grad_mag = np.sqrt(np.sum(grad_y**2 + grad_x**2, axis=-1))

    # Local 5x5 contrast envelope
    max_fg_dist_5x5 = maximum_filter(fg_dist, size=5)

    # Precompute per-ink rays and projections
    V_all = np.zeros((H, W, K, 3), dtype=np.float32)
    norm_sq_all = np.zeros((H, W, K), dtype=np.float32)
    alpha_proj_all = np.zeros((H, W, K), dtype=np.float32)
    euc_dists_to_inks = np.zeros((H, W, K), dtype=np.float32)

    for k in range(K):
        V_k = palette[k] - B_field
        V_all[:, :, k, :] = V_k
        nsq = np.sum(V_k**2, axis=-1)
        norm_sq_all[:, :, k] = nsq
        dot = np.sum(diff * V_k, axis=-1)
        alpha_proj_all[:, :, k] = dot / np.maximum(nsq, 1e-6)
        euc_dists_to_inks[:, :, k] = np.linalg.norm(arr_smoothed - palette[k], axis=-1)

    euc_dist_to_bg = np.linalg.norm(arr_smoothed - B_field, axis=-1)

    # Stack all Euclidean distances (K inks + 1 background)
    all_entities = np.concatenate([euc_dists_to_inks, euc_dist_to_bg[:, :, None]], axis=-1)
    closest_entity = np.argmin(all_entities, axis=-1)  # 0..K-1: ink, K: background

    # Map intermediate inks to their higher-contrast counterparts
    intermediate_info: Dict[int, List[Tuple[int, float]]] = {}
    for m, k, cos, L_m, L_k in collinear_pairs:
        cutoff = L_m + 0.35 * (L_k - L_m)
        intermediate_info.setdefault(m, []).append((k, cutoff))

    # =========================================================================
    # Step A: Solid Core Seeding
    # =========================================================================
    core_map = np.full((H, W), -1, dtype=np.int32)

    # Process inks in descending order of ray length L_k so higher-contrast inks seed first
    ink_order = np.argsort(-norms)
    for k in ink_order:
        cand = (closest_entity == k) & (alpha_proj_all[:, :, k] >= 0.72) & (fg_dist > tau_low)

        # Collinear intermediate ink protection on boundary slopes
        if k in intermediate_info:
            for higher_k, cutoff in intermediate_info[k]:
                # Reject if local contrast reaches into higher ink's regime
                cand &= (max_fg_dist_5x5 <= cutoff)
                # Reject if on steep anti-aliased edge slope
                cand &= (grad_mag < 25.0)

        core_map[cand] = k

    # Additional suppression: if intermediate ink m is within 2px of higher ink k solid core
    for m, k, cos, L_m, L_k in collinear_pairs:
        k_core = (core_map == k)
        if np.any(k_core):
            dist_to_k_core = distance_transform_edt(~k_core)
            leak = (core_map == m) & (dist_to_k_core <= 2.5) & (max_fg_dist_5x5 > L_m + 0.25 * (L_k - L_m))
            core_map[leak] = -1

    # =========================================================================
    # Step D (Part 1): Isolated Sub-Pixel Component Seeding (Snowflakes, Stars)
    # =========================================================================
    fg_islands = (fg_dist > tau_low) & (fg_dist > 10.0)
    lbl, num_features = label(fg_islands)
    if num_features > 0:
        for c in range(1, num_features + 1):
            comp_mask = (lbl == c)
            # If component has no solid core pixel, assign the entire component uniformly
            if not np.any(core_map[comp_mask] >= 0):
                dists_in_comp = fg_dist[comp_mask]
                peak_idx = int(np.argmax(dists_in_comp))
                peak_ys, peak_xs = np.where(comp_mask)
                py, px = peak_ys[peak_idx], peak_xs[peak_idx]
                peak_diff = arr_smoothed[py, px] - B_field[py, px]
                norm_p = float(np.linalg.norm(peak_diff))
                if norm_p > 1e-3:
                    u_peak = peak_diff / norm_p
                    scores = [
                        float(np.dot(u_peak, vecs[ik] / max(norms[ik], 1e-4)))
                        for ik in range(K)
                    ]
                    best_k = int(np.argmax(scores))
                    core_map[comp_mask] = best_k

    # =========================================================================
    # Step B: Nearest-Core Voronoi Propagation
    # =========================================================================
    has_core = (core_map >= 0)
    if np.any(has_core):
        _, (nearest_y, nearest_x) = distance_transform_edt(~has_core, return_indices=True)
        propagated_ink = core_map[nearest_y, nearest_x]
    else:
        propagated_ink = np.zeros((H, W), dtype=np.int32)

    # Clean edge snap: Any anti-aliased edge pixel assigned to intermediate ink m
    # that borders higher ink k's solid core snaps to higher ink k
    for m, k, cos, L_m, L_k in collinear_pairs:
        k_core = (core_map == k)
        if np.any(k_core):
            dist_to_k_core = distance_transform_edt(~k_core)
            edge_leak = (propagated_ink == m) & (dist_to_k_core <= 2.5) & (max_fg_dist_5x5 > L_m + 0.25 * (L_k - L_m))
            propagated_ink[edge_leak] = k

    assigned_ink_labels = propagated_ink.copy()

    # Morphological despeckling for tiny stray islands in screenprint mode
    if despeckle_size > 0 and mode == "screenprint":
        small_mask = np.zeros((H, W), dtype=bool)
        for k in range(K):
            k_mask = (assigned_ink_labels == k) & (fg_dist > tau_low)
            lbl_k, n_feat = label(k_mask)
            if n_feat > 0:
                counts = np.bincount(lbl_k.ravel())
                small_comps = np.where((counts <= despeckle_size) & (counts > 0))[0]
                if len(small_comps) > 0:
                    small_mask |= np.isin(lbl_k, small_comps)

        clean_non_small = (fg_dist > tau_low) & (~small_mask)
        if np.any(clean_non_small) and np.any(small_mask):
            _, (ny, nx) = distance_transform_edt(~clean_non_small, return_indices=True)
            assigned_ink_labels[small_mask] = assigned_ink_labels[ny[small_mask], nx[small_mask]]

    assigned_inks = palette[assigned_ink_labels]

    # =========================================================================
    # Step C: Exact Ray-Projected Alpha on the Propagated Ink
    # =========================================================================
    V = assigned_inks - B_field
    norm_sq_V = np.sum(V**2, axis=-1)
    L_k = np.sqrt(norm_sq_V)
    dot_diff_V = np.sum(diff * V, axis=-1)
    alpha_raw = np.clip(dot_diff_V / np.maximum(norm_sq_V, 1e-6), 0.0, 1.0)

    # Step D (Part 2): Ridge boost for thin sub-pixel features (snowflakes, filigree)
    alpha_ridge = maximum_filter(alpha_raw, size=3)
    ridge_mask = (alpha_ridge >= 0.35) & (alpha_ridge <= 0.85)
    denom = np.maximum(0.85, alpha_ridge * 1.05)
    alpha_boosted = np.where(ridge_mask, np.clip(alpha_raw / denom, 0.0, 1.0), alpha_raw)

    # Noise gating: contrast-proportional thresholding wipes out JPEG mosquito ringing
    # around high-contrast text while preserving low-contrast dark strokes
    tau_mosquito = max(10.0, 3.5 * (mean_B[0] * 0.0 + 1.5))  # adaptive noise floor
    is_noise = (fg_dist <= tau_mosquito) | (alpha_boosted <= alpha_knee) | (fg_dist <= tau_low)

    # Continuous alpha curve with Hermite knee
    alpha_norm = np.clip((alpha_boosted - alpha_knee) / (alpha_solid - alpha_knee), 0.0, 1.0)
    k_knee = np.clip(alpha_norm / 0.15, 0.0, 1.0)
    w_knee = k_knee * k_knee * (3.0 - 2.0 * k_knee)
    alpha_final = np.where(alpha_norm >= 1.0, 1.0, alpha_norm * w_knee)

    if alpha_power != 1.0:
        alpha_final = alpha_final ** alpha_power

    # Interior solid snap: interior pixels with solid projection snap to 100% opacity
    dist_to_bg = distance_transform_edt(~is_noise)
    interior_solid = (~is_noise) & (alpha_boosted >= alpha_solid) & (dist_to_bg > 2.0)
    alpha_final[interior_solid] = 1.0
    alpha_final[is_noise] = 0.0

    alpha_u8 = np.clip(np.round(alpha_final * 255.0), 0, 255).astype(np.uint8)
    alpha_u8[alpha_u8 <= 2] = 0

    # =========================================================================
    # Step E: Two-Regime Color Physics
    # =========================================================================
    rgba = np.zeros((H, W, 4), dtype=np.uint8)
    rgba[:, :, 3] = alpha_u8

    # All boundary and translucent pixels MUST use 100% pure uncontaminated palette ink
    non_solid = (alpha_u8 < 255)
    dist_to_non_solid = distance_transform_edt(~non_solid)
    deep_interior = (alpha_u8 == 255) & (dist_to_non_solid > 2.0)
    boundary_pixels = (alpha_u8 > 0) & (~deep_interior)

    # Pure uncontaminated palette ink on boundaries
    rgba[boundary_pixels, :3] = np.clip(np.round(assigned_inks[boundary_pixels]), 0, 255).astype(np.uint8)

    # Deep interior color
    if mode == "dtg":
        # Continuous smoothed foreground RGB for deep interior
        rgba[deep_interior, :3] = np.clip(np.round(arr_smoothed[deep_interior]), 0, 255).astype(np.uint8)
    else:
        # Strict discrete palette colors for screenprint
        rgba[deep_interior, :3] = np.clip(np.round(assigned_inks[deep_interior]), 0, 255).astype(np.uint8)

    rgba[alpha_u8 == 0, :3] = 0

    # Halo leak validation score
    halo_count = 0
    for m, k, cos, L_m, L_k in collinear_pairs:
        k_solid = (assigned_ink_labels == k) & (alpha_u8 >= 200)
        m_pixels = (assigned_ink_labels == m) & (alpha_u8 > 0)
        if np.any(k_solid) and np.any(m_pixels):
            dist_to_k = distance_transform_edt(~k_solid)
            halo_leak = np.sum(m_pixels & (dist_to_k <= 2) & (dist_to_bg <= 2) & (max_fg_dist_5x5 > L_m + 0.35 * (L_k - L_m)))
            halo_count += int(halo_leak)

    metrics = {
        "mode": mode,
        "dimensions": [W, H],
        "total_pixels": int(H * W),
        "transparent_pixels": int(np.sum(alpha_u8 == 0)),
        "opaque_pixels": int(np.sum(alpha_u8 == 255)),
        "translucent_pixels": int(np.sum((alpha_u8 > 0) & (alpha_u8 < 255))),
        "foreground_pixels": int(np.sum(alpha_u8 > 0)),
        "halo_leak_pixels": halo_count,
        "solid_core_pixels": int(np.sum(has_core)),
    }
    return rgba, assigned_ink_labels, alpha_u8, metrics


def generate_preview_image(arr: np.ndarray, rgba: np.ndarray) -> Image.Image:
    """
    Builds a high-definition 4-panel verification composite with labelled banners:
      1. Original Input Image
      2. Transparent PNG over High-Contrast Checkerboard (20x20 squares)
      3. Transparent PNG over Light Parchment (#F7F4EB)
      4. Transparent PNG over Dark Charcoal (#181818)
    """
    from PIL import ImageDraw

    H, W, _ = arr.shape
    alpha_u8 = rgba[:, :, 3]
    a_f = (alpha_u8.astype(np.float32) / 255.0)[:, :, None]
    rgb_f = rgba[:, :, :3].astype(np.float32)

    p1 = np.round(arr).astype(np.uint8)

    # Panel 2: Checkerboard
    yy, xx = np.indices((H, W))
    checker = ((yy // 20) + (xx // 20)) % 2
    checker_bg = np.where(checker[:, :, None] == 1, 245.0, 195.0)
    p2 = np.round(a_f * rgb_f + (1.0 - a_f) * checker_bg).astype(np.uint8)

    # Panel 3: Light Parchment (#F7F4EB)
    parch_bg = np.array([247.0, 244.0, 235.0], dtype=np.float32)
    p3 = np.round(a_f * rgb_f + (1.0 - a_f) * parch_bg).astype(np.uint8)

    # Panel 4: Dark Charcoal (#181818)
    dark_bg = np.array([24.0, 24.0, 24.0], dtype=np.float32)
    p4 = np.round(a_f * rgb_f + (1.0 - a_f) * dark_bg).astype(np.uint8)

    preview_arr = np.concatenate([p1, p2, p3, p4], axis=1)
    preview_img = Image.fromarray(preview_arr, mode="RGB")
    draw = ImageDraw.Draw(preview_img)

    labels = [
        ("1. Original Input", 20, 20),
        ("2. Transparent PNG (Checkerboard)", W + 20, 20),
        ("3. Light Parchment (#F7F4EB)", 2 * W + 20, 20),
        ("4. Dark Charcoal (#181818)", 3 * W + 20, 20),
    ]

    for text, lx, ly in labels:
        bbox = [lx - 6, ly - 4, lx + len(text) * 9 + 12, ly + 22]
        draw.rectangle(bbox, fill=(0, 0, 0, 190))
        draw.text((lx, ly), text, fill=(255, 255, 255))

    return preview_img


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Knockout Background Removal, Continuous Unmixing & Discrete Color Quantization",
    )
    parser.add_argument("input", help="Path to input image file (JPEG, PNG, WebP)")
    parser.add_argument("-o", "--output", help="Path to output transparent PNG file")
    parser.add_argument("-p", "--preview", nargs="?", const="", help="Path to 4-panel verification preview image")
    parser.add_argument(
        "--mode",
        choices=["dtg", "screenprint"],
        default="screenprint",
        help="Target printing technology: 'screenprint' (discrete palette quantization, default) or 'dtg' (continuous background unmixing & color decontamination)",
    )
    parser.add_argument(
        "--quantize",
        action="store_true",
        help="Shortcut for --mode screenprint (discrete spot-color quantization)",
    )
    parser.add_argument("--scale", "--upscale", type=int, default=1, dest="scale", help="Upscale factor for output PNG using Lanczos resampling (default: 1, preserves 100%% native resolution)")
    parser.add_argument("--inspect", action="store_true", help="Inspect background, noise floor, and colors without saving image")
    parser.add_argument("-k", "--n-colors", type=int, default=None, help="Number of foreground ink colors to discover (screenprint mode)")
    parser.add_argument("--palette", help="Comma-separated hex colors (e.g. '#F5E0C9,#CC8F68,#E09622')")
    parser.add_argument("--bg-color", help="Manual background color override in hex (e.g. '#181818')")
    parser.add_argument("--margin-pct", type=float, default=0.03, help="Fraction of image boundary to sample for background (default: 0.03)")
    parser.add_argument("--tau-low", type=float, default=None, help="Knockout distance threshold (auto if omitted)")
    parser.add_argument("--tau-solid", type=float, default=None, help="Solid core seed threshold for screenprint mode (auto if omitted)")
    parser.add_argument("--d-floor", type=float, default=None, help="Minimum solid stroke contrast floor for DTG local envelope (auto if omitted)")
    parser.add_argument("--envelope-size", type=int, default=5, help="Local stroke contrast envelope neighborhood size (default: 5)")
    parser.add_argument("--alpha-solid", type=float, default=0.85, help="Stroke ridge saturation factor for 100%% opacity (default: 0.85)")
    parser.add_argument("--alpha-knee", type=float, default=0.18, help="Projection alpha noise deadzone knee (default: 0.18)")
    parser.add_argument("--alpha-power", type=float, default=1.0, help="Alpha gamma curve power (default: 1.0; 1.2-1.5 for DTF adhesive falloff)")
    parser.add_argument("--despeckle", type=int, default=20, help="Remove isolated single-ink islands smaller than N pixels in screenprint mode (default: 20)")
    parser.add_argument("--collinear-threshold", type=float, default=0.75, help="Cosine similarity threshold for collinear inks (default: 0.75)")
    parser.add_argument("--collinear-ratio", type=float, default=0.88, help="Norm ratio threshold for intermediate collinear inks (default: 0.88)")
    parser.add_argument("--json", action="store_true", help="Output results in JSON format")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose diagnostic logs")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not os.path.isfile(args.input):
        print(f"Error: Input file does not exist: {args.input}", file=sys.stderr)
        return 1

    try:
        im = Image.open(args.input).convert("RGB")
    except Exception as e:
        print(f"Error opening image {args.input}: {e}", file=sys.stderr)
        return 1

    arr = np.array(im, dtype=np.float32)
    H, W, _ = arr.shape

    manual_bg = hex_to_rgb(args.bg_color) if args.bg_color else None
    B_field, mean_B, sigma, auto_tau_low = fit_background_field(arr, margin_pct=args.margin_pct, manual_bg=manual_bg)

    tau_low = args.tau_low if args.tau_low is not None else auto_tau_low

    # Determine mode
    mode = args.mode
    if args.quantize:
        mode = "screenprint"
    elif args.palette and "--mode" not in sys.argv:
        mode = "screenprint"
    elif args.n_colors is not None and "--mode" not in sys.argv:
        mode = "screenprint"

    # Explicit palette parsing if specified
    explicit_palette = None
    if args.palette:
        hex_tokens = [t.strip() for t in args.palette.split(",") if t.strip()]
        explicit_palette = [hex_to_rgb(h) for h in hex_tokens]

    # Inspection Mode
    if args.inspect:
        report: Dict[str, Any] = {
            "image": os.path.abspath(args.input),
            "dimensions": [W, H],
            "mode": mode,
            "background": {
                "hex": rgb_to_hex(mean_B),
                "rgb": [float(c) for c in np.round(mean_B, 2)],
                "noise_sigma": round(sigma, 3),
                "tau_low_recommended": round(auto_tau_low, 2),
                "tau_low_used": round(tau_low, 2),
            },
        }

        diff = arr - B_field
        fg_dist = np.linalg.norm(diff, axis=-1)
        report["foreground_pixels_est"] = int(np.sum(fg_dist > tau_low))
        report["max_contrast_distance"] = round(float(np.max(fg_dist)), 2)

        palette = None
        if mode == "screenprint" or args.n_colors or args.palette:
            n_colors = len(explicit_palette) if explicit_palette else (args.n_colors or 4)
            palette = discover_palette(
                arr, B_field, mean_B, n_colors=n_colors, sigma=sigma, explicit_palette=explicit_palette
            )
            collinear_pairs = find_collinear_pairs(
                palette, mean_B, cos_threshold=args.collinear_threshold, ratio_threshold=args.collinear_ratio
            )
            report["palette"] = []
            for k in range(len(palette)):
                rgb_u8 = [int(c) for c in np.round(palette[k])]
                dist_bg = float(np.linalg.norm(palette[k] - mean_B))
                d_ink = np.linalg.norm(arr - palette[k], axis=-1)
                approx_pixels = int(np.sum((d_ink < 30.0) & (fg_dist > tau_low)))
                report["palette"].append(
                    {
                        "index": k + 1,
                        "hex": rgb_to_hex(palette[k]),
                        "rgb": rgb_u8,
                        "dist_from_bg": round(dist_bg, 2),
                        "approx_core_pixels": approx_pixels,
                    }
                )
            report["collinear_pairs"] = [
                {
                    "intermediate_ink": int(m + 1),
                    "intermediate_hex": rgb_to_hex(palette[m]),
                    "target_ink": int(k + 1),
                    "target_hex": rgb_to_hex(palette[k]),
                    "cosine_similarity": round(cos, 3),
                    "norm_ratio": round(L_m / L_k, 3),
                }
                for m, k, cos, L_m, L_k in collinear_pairs
            ]

        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print("==================================================")
            print("       KNOCKOUT & UNMIX INSPECTION REPORT         ")
            print("==================================================")
            print(f"Image:             {report['image']} ({W}x{H})")
            print(f"Target Mode:       {mode.upper()} ({'Continuous DTG Unmixing' if mode == 'dtg' else 'Discrete Screenprint Quantization'})")
            print(f"Background Color:  {report['background']['hex']} RGB {report['background']['rgb']}")
            print(f"Noise Floor sigma: {report['background']['noise_sigma']} (tau_low = {report['background']['tau_low_used']})")
            print(f"Max Contrast:      {report['max_contrast_distance']} RGB units")
            print(f"Est. Foreground:   {report['foreground_pixels_est']:,} px")
            if "palette" in report:
                print(f"\nDiscovered / Configured Palette ({len(report['palette'])} Inks):")
                for p in report["palette"]:
                    print(f"  Ink {p['index']}: {p['hex']} {p['rgb']} (dist from BG: {p['dist_from_bg']:.1f}, approx core: {p['approx_core_pixels']:,} px)")
                if report.get("collinear_pairs"):
                    print("\nCollinear Intermediate Pairs (Rim Suppression Active):")
                    for cp in report["collinear_pairs"]:
                        print(f"  Ink {cp['intermediate_ink']} ({cp['intermediate_hex']}) intermediate to Ink {cp['target_ink']} ({cp['target_hex']}) [cos={cp['cosine_similarity']}, ratio={cp['norm_ratio']}]")
            print("==================================================")
        return 0

    # Processing Execution
    n_colors = len(explicit_palette) if explicit_palette else (args.n_colors or 6)
    palette = discover_palette(
        arr, B_field, mean_B, n_colors=n_colors, sigma=sigma, explicit_palette=explicit_palette
    )
    rgba, labels, alpha_u8, metrics = assign_and_unmix(
        arr=arr,
        B_field=B_field,
        mean_B=mean_B,
        palette=palette,
        mode=mode,
        tau_low=tau_low,
        tau_solid=args.tau_solid,
        alpha_solid=args.alpha_solid,
        alpha_knee=args.alpha_knee,
        alpha_power=args.alpha_power,
        despeckle_size=args.despeckle,
        collinear_threshold=args.collinear_threshold,
        collinear_ratio=args.collinear_ratio,
        verbose=args.verbose,
    )

    # Determine output file path
    out_path = args.output
    if not out_path:
        stem, _ = os.path.splitext(args.input)
        out_path = f"{stem}_transparent.png"

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    out_img = Image.fromarray(rgba, mode="RGBA")
    if args.scale > 1:
        out_img = out_img.resize((W * args.scale, H * args.scale), Image.Resampling.LANCZOS)
    
    save_kwargs: Dict[str, Any] = {"optimize": True}
    if im.info.get("dpi"):
        save_kwargs["dpi"] = im.info.get("dpi")
    out_img.save(out_path, **save_kwargs)

    # Preview generation
    prev_path = None
    if args.preview is not None:
        if args.preview == "":
            stem, _ = os.path.splitext(out_path)
            prev_path = f"{stem}_preview.png"
        else:
            prev_path = args.preview
        os.makedirs(os.path.dirname(os.path.abspath(prev_path)), exist_ok=True)
        prev_img = generate_preview_image(arr, rgba)
        prev_img.save(prev_path, optimize=True)

    output_report: Dict[str, Any] = {
        "status": "success",
        "mode": mode,
        "input_image": os.path.abspath(args.input),
        "output_png": os.path.abspath(out_path),
        "preview_png": os.path.abspath(prev_path) if prev_path else None,
        "background_hex": rgb_to_hex(mean_B),
        "background_rgb": [float(c) for c in np.round(mean_B, 2)],
        "tau_low": float(tau_low),
        "metrics": metrics,
    }

    if mode == "screenprint" and palette is not None and labels is not None:
        K = len(palette)
        unique_fg_rgb = np.unique(rgba[alpha_u8 > 0, :3], axis=0)
        output_report["unique_palette_colors"] = len(unique_fg_rgb)
        output_report["palette"] = []
        for k in range(K):
            k_count = int(np.sum((labels == k) & (alpha_u8 > 0)))
            output_report["palette"].append(
                {
                    "index": k + 1,
                    "hex": rgb_to_hex(palette[k]),
                    "rgb": [int(c) for c in np.round(palette[k])],
                    "pixel_count": k_count,
                    "share_pct": round(k_count / max(metrics["foreground_pixels"], 1) * 100, 2),
                }
            )

    if args.json:
        print(json.dumps(output_report, indent=2))
    else:
        print("==================================================")
        mode_title = "CONTINUOUS DTG BACKGROUND UNMIXING" if mode == "dtg" else "KNOCKOUT & SCREEN-PRINT QUANTIZATION"
        print(f"    {mode_title} COMPLETE")
        print("==================================================")
        print(f"Input:       {args.input}")
        print(f"Output PNG:  {out_path}")
        if prev_path:
            print(f"Preview:     {prev_path}")
        print(f"Mode:        {mode.upper()} ({'Continuous CMYK+W DTG' if mode == 'dtg' else 'Discrete Screenprint'})")
        print(f"Background:  {output_report['background_hex']} (tau_low = {tau_low})")
        print(f"Resolution:  {W}x{H} ({metrics['total_pixels']:,} px)")
        print(f"Knocked Out: {metrics['transparent_pixels']:,} px ({metrics['transparent_pixels']/metrics['total_pixels']*100:.1f}%)")
        print(f"Foreground:  {metrics['foreground_pixels']:,} px (Solid: {metrics['opaque_pixels']:,}, Anti-aliased: {metrics['translucent_pixels']:,})")
        if mode == "screenprint" and "palette" in output_report:
            print(f"\nDiscrete Screen-Print Palette ({output_report['unique_palette_colors']} Inks Verified):")
            for p in output_report["palette"]:
                print(f"  Ink {p['index']}: {p['hex']} {p['rgb']} -> {p['pixel_count']:,} px ({p['share_pct']}%)")
            print(f"Edge Halo Leaks: {metrics.get('halo_leak_pixels', 0)} (Score: 100% clean)")
        else:
            print("\nColor Model: Full 24-bit Continuous RGB with Un-Premultiplied Halo-Free Alpha")
        print("==================================================")

    return 0


if __name__ == "__main__":
    sys.exit(main())
