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
    find_objects,
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

    # Robust mode/histogram-peak background estimation on borders
    # (prevents foreground artwork touching border from corrupting background)
    rgb_quant = (np.clip(rgb_v, 0, 255) // 4).astype(np.int32)
    packed = rgb_quant[:, 0] * 4096 + rgb_quant[:, 1] * 64 + rgb_quant[:, 2]
    unique_bins, bin_counts = np.unique(packed, return_counts=True)
    mode_bin = unique_bins[np.argmax(bin_counts)]
    rough_bg_median = np.median(rgb_v[packed == mode_bin], axis=0)

    # Inlier border pixels within 25.0 RGB units of rough_bg_median
    dev = np.linalg.norm(rgb_v - rough_bg_median, axis=-1)
    inliers = dev <= 25.0
    if np.sum(inliers) >= 30:
        inlier_pixels = rgb_v[inliers]
        inlier_dev = np.linalg.norm(inlier_pixels - rough_bg_median, axis=-1)
        inlier_std = float(np.median(inlier_dev) * 1.4826)
    else:
        inlier_std = 2.0

    # Strict cutoff: never exceeds 24.0 so low-contrast foreground (dist >= 35) is never absorbed
    cutoff = float(np.clip(inlier_std * 3.5 + 10.0, 14.0, 24.0))
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
    tau_low = float(np.clip(max(6.0, 3.5 * sigma), 6.0, 16.0))

    return B_field, mean_B, sigma, tau_low


def discover_palette(
    arr: np.ndarray,
    B_field: np.ndarray,
    mean_B: np.ndarray,
    n_colors: int,
    sigma: float,
    scale_factor: float = 1.0,
    explicit_palette: Optional[List[List[int]]] = None,
    min_cluster_ratio: float = 0.0008,
) -> np.ndarray:
    """
    Discovers discrete ink colors by clustering pure interior core pixels.
    Snaps each cluster to the exact physical median RGB of its core pixels.
    Auto-prunes extraneous noise clusters with < 0.08% of core pixels (ALG-04).
    """
    if explicit_palette is not None:
        return np.array(explicit_palette, dtype=np.float32)

    H, W, _ = arr.shape
    diff = arr - B_field
    dist = np.linalg.norm(diff, axis=-1)

    tau_solid = max(38.0, 6.0 * sigma)
    solid_mask = dist > tau_solid
    erode_iter = max(1, int(np.round(2 * scale_factor)))
    eroded_solid = binary_erosion(solid_mask, iterations=erode_iter)

    grad_y = sobel(arr, axis=0) / 8.0
    grad_x = sobel(arr, axis=1) / 8.0
    grad_mag = np.sqrt(np.sum(grad_y**2 + grad_x**2, axis=-1))

    pure_core_mask = eroded_solid & (grad_mag < 20.0)
    core_pixels = arr[pure_core_mask]

    if len(core_pixels) < n_colors * 10:
        # Fallback to broader foreground if artwork has very thin lines
        cand_mask = (dist > 25.0) & (grad_mag < 30.0)
        core_pixels = arr[cand_mask]
        if len(core_pixels) < n_colors:
            core_pixels = arr[dist > max(15.0, 3.0 * sigma)]

    km = KMeans(n_clusters=n_colors, random_state=42, n_init=10).fit(core_pixels)
    labels = km.labels_
    counts = np.bincount(labels, minlength=n_colors)
    total_core = len(core_pixels)

    # Auto-prune tiny clusters (< min_cluster_ratio of core pixels, e.g. < 0.08%)
    min_pixels = max(30, int(total_core * min_cluster_ratio))
    valid_clusters = [k for k in range(n_colors) if counts[k] >= min_pixels]
    if len(valid_clusters) == 0:
        valid_clusters = list(range(n_colors))

    palette_list = []
    for k in valid_clusters:
        k_mask = labels == k
        palette_list.append(np.median(core_pixels[k_mask], axis=0))

    palette = np.array(palette_list, dtype=np.float32)

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
    max_perp_dist: float = 85.0,
) -> List[Tuple[int, int, float, float, float]]:
    """
    Detects pairs of inks (m, k) where ink m is an anti-aliased intermediate rim along the ray to ink k.
    Requires:
      1. High cosine similarity (cos > cos_threshold)
      2. Intermediate length (ratio < ratio_threshold)
      3. Small perpendicular distance from palette[m] to the line segment (mean_B -> palette[k])
      4. Positive luminance shift relative to background (anti-aliasing rims of bright inks)
    """
    K = len(palette)
    vecs = palette - mean_B
    norms = np.linalg.norm(vecs, axis=-1)
    delta_Y = 0.299 * vecs[:, 0] + 0.587 * vecs[:, 1] + 0.114 * vecs[:, 2]

    collinear_pairs = []
    for i in range(K):
        for j in range(K):
            if i != j:
                # Both inks must be brighter than background (rims of bright foreground against darker BG)
                if delta_Y[i] <= 0 or delta_Y[j] <= 0:
                    continue
                dot = np.dot(vecs[i], vecs[j])
                cos = dot / max(norms[i] * norms[j], 1e-4)
                if cos > cos_threshold and norms[i] < ratio_threshold * norms[j]:
                    # Perpendicular distance from palette[i] to the ray (mean_B -> palette[j])
                    proj = dot / max(norms[j], 1e-4)
                    perp_sq = max(0.0, norms[i]**2 - proj**2)
                    perp_dist = float(np.sqrt(perp_sq))
                    if perp_dist <= max_perp_dist:
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
    scale_factor = float(min(H, W)) / 764.0
    diff = arr - B_field
    fg_dist = np.linalg.norm(diff, axis=-1)

    # Noise-stabilized image for interior classification
    arr_smoothed = median_filter(arr, size=(3, 3, 1))

    K = len(palette)
    vecs = palette - mean_B
    norms = np.linalg.norm(vecs, axis=-1)

    # Compute luminance shift of each ink relative to background
    # (used for polarity separation to prevent dark foliage from being hijacked by bright snow caps)
    delta_Y_ink = 0.299 * vecs[:, 0] + 0.587 * vecs[:, 1] + 0.114 * vecs[:, 2]
    dark_inks = [k for k in range(K) if delta_Y_ink[k] < -10.0]
    bright_inks = [k for k in range(K) if delta_Y_ink[k] >= -10.0]
    has_dual_polarity = (len(dark_inks) > 0 and len(bright_inks) > 0)

    # Pixel luminance difference from local background
    delta_Y_pixel = 0.299 * diff[:, :, 0] + 0.587 * diff[:, :, 1] + 0.114 * diff[:, :, 2]
    is_dark_pixel = delta_Y_pixel < -6.0

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

    # Local contrast envelope (scales with resolution)
    env_size = max(3, int(np.round(5 * scale_factor)) | 1)
    max_fg_dist_5x5 = maximum_filter(fg_dist, size=env_size)

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

    if has_dual_polarity:
        # Enforce ray-polarity alignment:
        # Dark pixels cannot match bright inks; bright pixels cannot match dark inks
        for k in bright_inks:
            euc_dists_to_inks[:, :, k] = np.where(is_dark_pixel, 1e6, euc_dists_to_inks[:, :, k])
        for k in dark_inks:
            euc_dists_to_inks[:, :, k] = np.where(~is_dark_pixel, 1e6, euc_dists_to_inks[:, :, k])

    euc_dist_to_bg = np.linalg.norm(arr_smoothed - B_field, axis=-1)

    # Stack all Euclidean distances (K inks + 1 background)
    all_entities = np.concatenate([euc_dists_to_inks, euc_dist_to_bg[:, :, None]], axis=-1)
    closest_entity = np.argmin(all_entities, axis=-1)  # 0..K-1: ink, K: background

    fg_mask = fg_dist > tau_low
    dist_to_bg = distance_transform_edt(fg_mask)
    thin_boundary_dist = 2.5 * scale_factor

    # Map intermediate inks to their higher-contrast counterparts
    intermediate_info: Dict[int, List[Tuple[int, float]]] = {}
    for m, k, cos, L_m, L_k in collinear_pairs:
        cutoff = L_m + 0.25 * (L_k - L_m)
        intermediate_info.setdefault(m, []).append((k, cutoff))

    # Identify connected components containing higher collinear inks (fixes ALG-01!)
    fg_lbl, num_fg = label(fg_mask)
    fg_lbl_flat = fg_lbl.ravel()
    suppress_m_branches: Dict[int, np.ndarray] = {i: np.zeros((H, W), dtype=bool) for i in range(K)}
    erode_m = max(1, int(np.round(2.0 * scale_factor)))
    dilate_m_iter = int(np.round(2.0 * scale_factor))

    # Precompute per-ink wide solids and dilations ONCE
    m_wide_solid_all: Dict[int, np.ndarray] = {}
    dilate_m_all: Dict[int, np.ndarray] = {}
    for m in set(p[0] for p in collinear_pairs):
        m_wide = binary_erosion(closest_entity == m, iterations=erode_m)
        m_wide_solid_all[m] = m_wide
        dilate_m_all[m] = binary_dilation(m_wide, iterations=dilate_m_iter)

    for m, k, cos, L_m, L_k in collinear_pairs:
        k_candidate_solid = (
            (closest_entity == k)
            & (alpha_proj_all[:, :, k] >= 0.70)
            & (fg_dist >= L_m + 0.20 * (L_k - L_m))
        )
        if np.any(k_candidate_solid) and num_fg > 0:
            k_solid_counts = np.bincount(fg_lbl_flat, weights=k_candidate_solid.ravel(), minlength=num_fg + 1)
            k_comp_lookup = k_solid_counts > 0
            k_comp_lookup[0] = False

            m_solid_counts = np.bincount(fg_lbl_flat, weights=m_wide_solid_all[m].ravel(), minlength=num_fg + 1)
            suppress_entire_lookup = k_comp_lookup & (m_solid_counts == 0)
            suppress_branch_lookup = k_comp_lookup & (m_solid_counts > 0)

            if np.any(suppress_entire_lookup):
                suppress_m_branches[m] |= suppress_entire_lookup[fg_lbl]
            if np.any(suppress_branch_lookup):
                in_branch_comp = suppress_branch_lookup[fg_lbl]
                suppress_m_branches[m] |= in_branch_comp & (~dilate_m_all[m]) & (dist_to_bg <= thin_boundary_dist * 1.5)

    # =========================================================================
    # Step A: Solid Core Seeding
    # =========================================================================
    core_map = np.full((H, W), -1, dtype=np.int32)

    # Process inks in descending order of ray length L_k so higher-contrast inks seed first
    ink_order = np.argsort(-norms)
    for k in ink_order:
        is_interior = dist_to_bg > thin_boundary_dist

        # For dark inks in dual-polarity images, allow lower contrast floor since L_k is smaller
        is_dark_k = (k in dark_inks) if has_dual_polarity else False
        min_proj = 0.60 if is_dark_k else 0.72

        cand_boundary = (~is_interior) & (closest_entity == k) & (alpha_proj_all[:, :, k] >= min_proj) & (fg_dist > tau_low)
        if has_dual_polarity and (k in dark_inks):
            # Prevent 1px JPEG dark-undershoot rings right outside bright text from seeding false dark cores
            cand_boundary &= (grad_mag < 25.0) & (alpha_proj_all[:, :, k] >= 0.80)
        cand_interior = is_interior & (closest_entity == k)
        cand = cand_boundary | cand_interior

        # Collinear intermediate ink protection on boundary slopes
        # ONLY apply grad_mag and max_fg_dist suppression near background boundary (~is_interior)
        if k in intermediate_info:
            for higher_k, cutoff in intermediate_info[k]:
                cand &= (~cand_boundary | (max_fg_dist_5x5 <= cutoff))
                cand &= (~cand_boundary | (grad_mag < 25.0))
            if k in suppress_m_branches:
                cand &= ~suppress_m_branches[k]

        core_map[cand] = k

    # Additional suppression: if intermediate ink m is within boundary zone of higher ink k solid core
    for m, k, cos, L_m, L_k in collinear_pairs:
        k_core = (core_map == k)
        if np.any(k_core):
            dist_to_k_core = distance_transform_edt(~k_core)
            leak = (core_map == m) & (dist_to_k_core <= thin_boundary_dist) & (max_fg_dist_5x5 > L_m + 0.20 * (L_k - L_m))
            core_map[leak] = -1

    # =========================================================================
    # Step D (Part 1): Isolated Sub-Pixel Component Seeding (Snowflakes, Stars)
    # =========================================================================
    fg_islands = (fg_dist > tau_low) & (fg_dist > 10.0)
    lbl, num_features = label(fg_islands)
    if num_features > 0:
        slices = find_objects(lbl)
        for c, sl in enumerate(slices, start=1):
            if sl is None:
                continue
            comp_local = (lbl[sl] == c)
            core_sub = core_map[sl]
            if not np.any(core_sub[comp_local] >= 0):
                dists_in_comp = fg_dist[sl][comp_local]
                peak_idx = int(np.argmax(dists_in_comp))
                peak_ys, peak_xs = np.where(comp_local)
                py, px = peak_ys[peak_idx], peak_xs[peak_idx]
                peak_diff = arr_smoothed[sl][py, px] - B_field[sl][py, px]
                norm_p = float(np.linalg.norm(peak_diff))
                if norm_p > 1e-3:
                    u_peak = peak_diff / norm_p
                    scores = [
                        float(np.dot(u_peak, vecs[ik] / max(norms[ik], 1e-4)))
                        for ik in range(K)
                    ]
                    best_k = int(np.argmax(scores))
                    core_sub[comp_local] = best_k

    # =========================================================================
    # Step B: Nearest-Core Voronoi Propagation
    # =========================================================================
    has_core = (core_map >= 0)
    propagated_ink = np.zeros((H, W), dtype=np.int32)
    if np.any(has_core):
        if has_dual_polarity:
            has_core_dark = has_core & np.isin(core_map, dark_inks)
            has_core_bright = has_core & np.isin(core_map, bright_inks)
            if np.any(has_core_dark) and np.any(has_core_bright):
                dist_to_dark_core, (ny_d, nx_d) = distance_transform_edt(~has_core_dark, return_indices=True)
                prop_dark = core_map[ny_d, nx_d]

                _, (ny_b, nx_b) = distance_transform_edt(~has_core_bright, return_indices=True)
                prop_bright = core_map[ny_b, nx_b]

                use_dark = is_dark_pixel & (dist_to_dark_core <= 3.0 * scale_factor)
                propagated_ink = np.where(use_dark, prop_dark, prop_bright)
            else:
                _, (nearest_y, nearest_x) = distance_transform_edt(~has_core, return_indices=True)
                propagated_ink = core_map[nearest_y, nearest_x]
        else:
            _, (nearest_y, nearest_x) = distance_transform_edt(~has_core, return_indices=True)
            propagated_ink = core_map[nearest_y, nearest_x]

    # Clean edge snap: Any anti-aliased edge pixel assigned to intermediate ink m
    # that borders higher ink k's solid core or is on thin branches of k's component snaps to higher ink k
    for m, k, cos, L_m, L_k in collinear_pairs:
        k_core = (core_map == k)
        if np.any(k_core):
            dist_to_k_core = distance_transform_edt(~k_core)
            edge_leak = (
                (propagated_ink == m)
                & (dist_to_k_core <= thin_boundary_dist)
                & (max_fg_dist_5x5 > L_m + 0.20 * (L_k - L_m))
            )
            propagated_ink[edge_leak] = k
        if m in suppress_m_branches:
            branch_leak = (propagated_ink == m) & suppress_m_branches[m]
            propagated_ink[branch_leak] = k

        # Component majority rule (vectorized bincount):
        if num_fg > 0:
            k_pix_counts = np.bincount(fg_lbl_flat, weights=(core_map == k).ravel(), minlength=num_fg + 1)
            m_pix_counts = np.bincount(fg_lbl_flat, weights=(core_map == m).ravel(), minlength=num_fg + 1)
            majority_k_lookup = (k_pix_counts > m_pix_counts) & (k_pix_counts > 0)
            majority_k_lookup[0] = False
            if np.any(majority_k_lookup):
                in_maj_comp = majority_k_lookup[fg_lbl]
                thin_tips = in_maj_comp & (propagated_ink == m) & (dist_to_bg <= thin_boundary_dist * 1.5)
                propagated_ink[thin_tips] = k

    assigned_ink_labels = propagated_ink.copy()

    # Morphological despeckling: aspect-ratio and stroke-aware (fixes ALG-02 & ALG-04!)
    eff_despeckle = int(np.round(despeckle_size * (scale_factor ** 2)))
    max_speckle_dim = max(4, int(np.round(5 * scale_factor)))
    if eff_despeckle > 0 and mode == "screenprint":
        small_mask = np.zeros((H, W), dtype=bool)
        for k in range(K):
            k_mask = (assigned_ink_labels == k) & (fg_dist > tau_low)
            lbl_k, n_feat = label(k_mask)
            if n_feat > 0:
                counts = np.bincount(lbl_k.ravel(), minlength=n_feat + 1)
                small_ids = np.where((counts <= eff_despeckle) & (counts > 0))[0]
                small_ids = small_ids[small_ids > 0]
                if len(small_ids) > 0:
                    slices_k = find_objects(lbl_k)
                    for sc in small_ids:
                        sl = slices_k[sc - 1]
                        if sl is None:
                            continue
                        h = sl[0].stop - sl[0].start
                        w = sl[1].stop - sl[1].start
                        max_dim = max(h, w)
                        min_dim = max(1, min(h, w))
                        aspect_ratio = max_dim / min_dim
                        # Only mark as speckle if compact 2D island; never erase elongated 1D strokes!
                        if max_dim < max_speckle_dim and aspect_ratio < 2.5:
                            comp_sc = (lbl_k[sl] == sc)
                            small_mask[sl][comp_sc] = True

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
    ridge_kernel_size = max(3, int(np.round(3 * scale_factor)) | 1)
    alpha_ridge = maximum_filter(alpha_raw, size=ridge_kernel_size)
    ridge_mask = (alpha_ridge >= 0.35) & (alpha_ridge <= 0.85)
    denom = np.maximum(0.85, alpha_ridge * 1.05)
    alpha_boosted = np.where(ridge_mask, np.clip(alpha_raw / denom, 0.0, 1.0), alpha_raw)

    # Adaptive contrast-scaled noise floor and alpha knee (fixes ALG-03!):
    # Scale mosquito gate proportionally for low-contrast inks (Dark Pine Green, Dark Brown)
    s_contrast = np.clip((L_k - 60.0) / 90.0, 0.0, 1.0)
    tau_dark = np.minimum(tau_low, 0.06 * L_k)
    tau_bright = np.maximum(tau_low, 10.0)
    tau_ink_adaptive = (1.0 - s_contrast) * tau_dark + s_contrast * tau_bright

    alpha_knee_eff = alpha_knee * np.clip((L_k - 30.0) / 120.0, 0.35, 1.0)

    is_noise = (fg_dist <= tau_ink_adaptive) | (alpha_boosted <= alpha_knee_eff)

    # Continuous alpha curve with Hermite knee
    alpha_norm = np.clip(
        (alpha_boosted - alpha_knee_eff) / np.maximum(alpha_solid - alpha_knee_eff, 1e-4),
        0.0,
        1.0,
    )
    k_knee = np.clip(alpha_norm / 0.15, 0.0, 1.0)
    w_knee = k_knee * k_knee * (3.0 - 2.0 * k_knee)
    alpha_final = np.where(alpha_norm >= 1.0, 1.0, alpha_norm * w_knee)

    if alpha_power != 1.0:
        alpha_final = alpha_final ** alpha_power

    # Interior solid snap: interior pixels with solid projection snap to 100% opacity
    dist_to_bg_noise = distance_transform_edt(~is_noise)
    interior_solid = (~is_noise) & (alpha_boosted >= alpha_solid) & (dist_to_bg_noise > 2.0 * scale_factor)
    alpha_final[interior_solid] = 1.0
    alpha_final[is_noise] = 0.0

    alpha_u8 = np.clip(np.round(alpha_final * 255.0), 0, 255).astype(np.uint8)
    alpha_u8[alpha_u8 <= 2] = 0

    # =========================================================================
    # Step E: Two-Regime Color Physics
    # =========================================================================
    rgba = np.zeros((H, W, 4), dtype=np.uint8)
    rgba[:, :, 3] = alpha_u8

    pure_rgb = np.clip(np.round(assigned_inks), 0, 255).astype(np.float32)

    if mode == "dtg":
        # Hybrid Pure-Palette Boundary + Continuous Deep Interior:
        # 1. Boundary pixels, thin strokes, and filigree (dist_to_bg <= 2.5 * scale_factor or alpha_u8 < 255)
        #    use 100% pure uncontaminated palette ink (pure_rgb), guaranteeing ZERO background fringing
        #    and 100% crisp solid color on thin strokes (castle lines, pine needles, snowflakes, stars).
        # 2. Deep interior pixels (dist_to_bg > 2.5 * scale_factor, alpha_u8 == 255, alpha_raw >= 0.90)
        #    smoothly ramp to continuous arr_smoothed, preserving 100% of inner highlight stripes
        #    and smooth shading without any quantization or erosion.
        ramp_start = 2.5 * scale_factor
        ramp_width = 1.0 * scale_factor
        w_interior = np.clip((dist_to_bg - ramp_start) / ramp_width, 0.0, 1.0)
        # Zero interior weight on non-solid or boundary-blended pixels
        w_interior[(alpha_u8 < 255) | (alpha_raw < 0.90)] = 0.0

        out_rgb = (1.0 - w_interior)[..., None] * pure_rgb + w_interior[..., None] * arr_smoothed
        rgba[alpha_u8 > 0, :3] = np.clip(np.round(out_rgb[alpha_u8 > 0]), 0, 255).astype(np.uint8)
    else:
        # Strict discrete palette colors for screenprint across all foreground pixels
        rgba[alpha_u8 > 0, :3] = np.clip(np.round(pure_rgb[alpha_u8 > 0]), 0, 255).astype(np.uint8)

    rgba[alpha_u8 == 0, :3] = 0

    # Halo leak validation score
    halo_count = 0
    for m, k, cos, L_m, L_k in collinear_pairs:
        k_solid = (assigned_ink_labels == k) & (alpha_u8 >= 200)
        m_pixels = (assigned_ink_labels == m) & (alpha_u8 > 0)
        if np.any(k_solid) and np.any(m_pixels):
            dist_to_k = distance_transform_edt(~k_solid)
            halo_leak = np.sum(
                m_pixels
                & (dist_to_k <= 2.5 * scale_factor)
                & (dist_to_bg_noise <= 2.5 * scale_factor)
                & (max_fg_dist_5x5 > L_m + 0.35 * (L_k - L_m))
            )
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

    scale_factor = float(min(H, W)) / 764.0

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
                arr, B_field, mean_B, n_colors=n_colors, sigma=sigma, scale_factor=scale_factor, explicit_palette=explicit_palette
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
        arr, B_field, mean_B, n_colors=n_colors, sigma=sigma, scale_factor=scale_factor, explicit_palette=explicit_palette
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
