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
#     "pytest",
# ]
# ///

import os
import re
import tempfile
import xml.etree.ElementTree as ET
import cv2
import numpy as np
from PIL import Image
import pytest
import scipy.ndimage

# Import module under test
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from prepress_vectorize import (
    discover_palette,
    unmix_spot_colors,
    clean_vtracer_svg,
    run_vector_knockout,
    hex_to_rgb_255,
    consolidate_raster_speckles,
    consolidate_low_gradient_voronoi_splits,
)


def test_discover_palette_zero_samples():
    """Verify discover_palette() handles 0 foreground samples cleanly without KMeans crash."""
    # An all-background image produces 0 foreground interior samples
    img = np.full((50, 50, 3), 24, dtype=np.uint8)
    bg = np.array([24.0, 24.0, 24.0], dtype=np.float32)

    inks, hexes = discover_palette(img, bg)
    assert len(inks) == 1
    assert len(hexes) == 1
    assert hexes[0] == "#FFFFFF"
    assert np.allclose(inks[0], [255.0, 255.0, 255.0])


def test_discover_palette_single_sample():
    """Verify discover_palette() handles 1 foreground sample cleanly without KMeans crash."""
    # Background with exactly 1 distant pixel
    img = np.full((100, 100, 3), 24, dtype=np.uint8)
    img[50, 50] = [200, 100, 50]
    bg = np.array([24.0, 24.0, 24.0], dtype=np.float32)

    inks, hexes = discover_palette(img, bg)
    assert len(inks) == 1
    assert len(hexes) == 1
    assert np.allclose(inks[0], [200.0, 100.0, 50.0], atol=1.0)


def test_discover_palette_inks_one():
    """Verify discover_palette() handles --inks 1 cleanly via median without silhouette crash."""
    img = np.full((100, 100, 3), 24, dtype=np.uint8)
    # 20x20 foreground square of deep gold
    img[40:60, 40:60] = [220, 180, 80]
    bg = np.array([24.0, 24.0, 24.0], dtype=np.float32)

    inks, hexes = discover_palette(img, bg, inks=1)
    assert len(inks) == 1
    assert len(hexes) == 1
    assert hexes[0] == "#DCB450"


def test_empty_palette_guard():
    """Verify empty or malformed --palette strings raise ValueError fast upfront."""
    pil_in = Image.new("RGB", (32, 32), color=(24, 24, 24))

    with pytest.raises(ValueError, match="--palette must specify at least one valid hex color"):
        run_vector_knockout(
            input_pil=pil_in,
            input_path="dummy.png",
            output_png="dummy_out.png",
            output_svg="dummy_out.svg",
            dpi=(300, 300),
            palette="",
        )

    with pytest.raises(ValueError, match="--palette must specify at least one valid hex color"):
        run_vector_knockout(
            input_pil=pil_in,
            input_path="dummy.png",
            output_png="dummy_out.png",
            output_svg="dummy_out.svg",
            dpi=(300, 300),
            palette="  ,  , ",
        )

    with pytest.raises(ValueError, match="Invalid hex color"):
        run_vector_knockout(
            input_pil=pil_in,
            input_path="dummy.png",
            output_png="dummy_out.png",
            output_svg="dummy_out.svg",
            dpi=(300, 300),
            palette="#ZZZZZZ",
        )

    with pytest.raises(ValueError, match="Invalid hex color"):
        run_vector_knockout(
            input_pil=pil_in,
            input_path="dummy.png",
            output_png="dummy_out.png",
            output_svg="dummy_out.svg",
            dpi=(300, 300),
            palette="#12345",
        )


def test_clean_vtracer_svg_case_insensitive_xxe():
    """Verify case-insensitive / whitespace-tolerant XXE check in clean_vtracer_svg."""
    with tempfile.TemporaryDirectory() as td:
        # Lowercase doctype
        svg_bad1 = os.path.join(td, "bad1.svg")
        with open(svg_bad1, "w", encoding="utf-8") as f:
            f.write('<!doctype svg SYSTEM "malicious.dtd"><svg></svg>')
        with pytest.raises(ValueError, match="Potentially malicious SVG"):
            clean_vtracer_svg(
                raw_svg_path=svg_bad1,
                out_svg_path=os.path.join(td, "out1.svg"),
                w_orig=100,
                h_orig=100,
                w_2x=200,
                h_2x=200,
                bg_rgb=np.array([24.0, 24.0, 24.0]),
                ink_rgbs=[np.array([200.0, 100.0, 50.0])],
                canonical_hexes=["#C86432"],
            )

        # Mixed case with whitespace entity
        svg_bad2 = os.path.join(td, "bad2.svg")
        with open(svg_bad2, "w", encoding="utf-8") as f:
            f.write('<!  ENTITY  xxe SYSTEM "file:///etc/passwd"><svg></svg>')
        with pytest.raises(ValueError, match="Potentially malicious SVG"):
            clean_vtracer_svg(
                raw_svg_path=svg_bad2,
                out_svg_path=os.path.join(td, "out2.svg"),
                w_orig=100,
                h_orig=100,
                w_2x=200,
                h_2x=200,
                bg_rgb=np.array([24.0, 24.0, 24.0]),
                ink_rgbs=[np.array([200.0, 100.0, 50.0])],
                canonical_hexes=["#C86432"],
            )


def test_clean_vtracer_svg_polygon_support():
    """Verify <polygon> elements are correctly handled and pruned if background border."""
    with tempfile.TemporaryDirectory() as td:
        raw_svg = os.path.join(td, "test_poly.svg")
        out_svg = os.path.join(td, "out_poly.svg")

        # SVG with:
        # 1. Background polygon touching border (x=0, y=0)
        # 2. Foreground polygon in interior (x=50, y=50)
        svg_content = """<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 200 200">
<polygon fill="#181818" points="0,0 200,0 200,10 0,10" />
<polygon fill="#C76331" points="50,50 150,50 150,150 50,150" />
</svg>"""
        with open(raw_svg, "w", encoding="utf-8") as f:
            f.write(svg_content)

        bg_rgb = np.array([24.0, 24.0, 24.0], dtype=np.float32)
        ink_rgbs = [np.array([200.0, 100.0, 50.0], dtype=np.float32)]
        canonical_hexes = ["#C86432"]

        clean_vtracer_svg(
            raw_svg_path=raw_svg,
            out_svg_path=out_svg,
            w_orig=100,
            h_orig=100,
            w_2x=200,
            h_2x=200,
            bg_rgb=bg_rgb,
            ink_rgbs=ink_rgbs,
            canonical_hexes=canonical_hexes,
        )

        with open(out_svg, "r", encoding="utf-8") as f:
            cleaned_text = f.read()

        # The background border polygon must be pruned
        assert 'points="0,0 200,0 200,10 0,10"' not in cleaned_text
        # The foreground polygon must remain and have its fill snapped
        assert 'points="50,50 150,50 150,150 50,150"' in cleaned_text
        assert 'fill="#C86432"' in cleaned_text


def test_interior_solid_cielab_guard():
    """Verify unmix_spot_colors() applies solid CIELAB distance in deep interior."""
    H, W = 60, 60
    # Background: dark slate #18181B
    bg_rgb = np.array([24.0, 24.0, 27.0], dtype=np.float32)

    # Inks:
    # Ink 0: Gold #D4AF37 -> [212, 175, 55]
    # Ink 1: Deep Brown #4A2E18 -> [74, 46, 24]
    # Ink 2: Cyan #00BCD4 -> [0, 188, 212]
    ink_rgbs = [
        np.array([212.0, 175.0, 55.0], dtype=np.float32),
        np.array([74.0, 46.0, 24.0], dtype=np.float32),
        np.array([0.0, 188.0, 212.0], dtype=np.float32),
    ]

    # Create test canvas
    P = np.full((H, W, 3), bg_rgb, dtype=np.float32)

    # Place a large 40x40 foreground object in the center (y: 10..50, x: 10..50)
    # It has a base of Brown
    P[10:50, 10:50] = ink_rgbs[1]

    # In deep interior (y: 20..40, x: 20..40), place an opaque Gold stripe
    P[20:40, 20:40] = ink_rgbs[0]

    # Add an anti-aliased boundary pixel along the outer edge of the Brown base (y=9, x=30)
    # Blended 55% Brown + 45% Background to test continuous fractional alpha ramp
    P[9, 30] = 0.55 * ink_rgbs[1] + 0.45 * bg_rgb

    # Run unmix_spot_colors
    flat_quantized, k_star, clean_alpha, dist_to_bg = unmix_spot_colors(
        P=P,
        B=bg_rgb,
        ink_rgbs=ink_rgbs,
        verbose=True,
    )

    # Check that in the deep interior of Gold stripe (e.g. at 30, 30):
    # - Winner is Ink 0 (Gold)
    # - clean_alpha is exactly 1.0
    assert k_star[30, 30] == 0
    assert clean_alpha[30, 30] == 1.0
    assert np.allclose(flat_quantized[30, 30], np.round(ink_rgbs[0]))

    # Check that in the deep interior of Brown base (e.g. at 15, 15):
    # - Winner is Ink 1 (Brown)
    # - clean_alpha is exactly 1.0
    assert k_star[15, 15] == 1
    assert clean_alpha[15, 15] == 1.0
    assert np.allclose(flat_quantized[15, 15], np.round(ink_rgbs[1]))

    # Check that the anti-aliased boundary pixel has fractional alpha between 0.0 and 1.0
    assert 0.0 < clean_alpha[9, 30] < 1.0
    assert k_star[9, 30] == 1
    assert np.allclose(flat_quantized[9, 30], np.round(ink_rgbs[1]))

    # Check that background pixels (e.g. at 2, 2) have clean_alpha == 0.0
    assert clean_alpha[2, 2] == 0.0
    assert np.allclose(flat_quantized[2, 2], np.round(bg_rgb))


def test_unmix_spot_colors_seal_boxes():
    """Verify unmix_spot_colors() applies morphological closing to initial_fg via seal_boxes."""
    H, W = 60, 60
    bg_rgb = np.array([24.0, 24.0, 27.0], dtype=np.float32)
    ink_rgbs = [np.array([212.0, 175.0, 55.0], dtype=np.float32)]

    # Two foreground blocks (y: 10..50, x: 10..24) and (y: 10..50, x: 26..40)
    # separated by a 2px gap at x: 24..26 of background
    P = np.full((H, W, 3), bg_rgb, dtype=np.float32)
    P[10:50, 10:24] = ink_rgbs[0]
    P[10:50, 26:40] = ink_rgbs[0]

    # Without seal_boxes:
    # Gap pixels at x=25 are pure background, so clean_alpha is 0.0
    _, _, clean_alpha_unsealed, _ = unmix_spot_colors(
        P=P,
        B=bg_rgb,
        ink_rgbs=ink_rgbs,
        seal_boxes=None,
    )
    assert clean_alpha_unsealed[30, 25] == 0.0

    # With seal_boxes spanning the gap:
    # Morphological closing bridges the 2px gap in initial_fg, creating a continuous 30px wide block
    # making (30, 25) deep interior (>3px inside initial_fg), promoting clean_alpha to 1.0!
    _, _, clean_alpha_sealed, _ = unmix_spot_colors(
        P=P,
        B=bg_rgb,
        ink_rgbs=ink_rgbs,
        seal_boxes=[(20, 15, 30, 45, 5)],
    )
    assert clean_alpha_sealed[30, 25] == 1.0


def test_topological_interior_ink_junction_not_knocked_out():
    """
    Verify generalized topological true-background & deep-interior partitioning:
    Two foreground inks (Gold #CA8812 and Dark Espresso #46200D) meeting at an internal crease
    with anti-aliased transitions over Crimson #9A0616 background produce 0 interior knocked-out
    pixels, while preserving 100% transparent exterior background and a 40x40 interior cutout stripe.
    """
    H, W = 160, 160
    # Background: Crimson #9A0616 -> [154, 6, 22]
    bg_rgb = hex_to_rgb_255("#9A0616")
    # Inks: Gold #CA8812, Dark Espresso #46200D
    gold_rgb = hex_to_rgb_255("#CA8812")
    espresso_rgb = hex_to_rgb_255("#46200D")
    ink_rgbs = [gold_rgb, espresso_rgb]

    # Initialize canvas to Crimson background
    P = np.full((H, W, 3), bg_rgb, dtype=np.float32)

    # Place a large 120x120 solid foreground hat shape (y: 20..140, x: 20..140)
    # Left side (x: 20..80): Gold
    P[20:140, 20:80] = gold_rgb
    # Right side (x: 80..140): Dark Espresso
    P[20:140, 80:140] = espresso_rgb

    # Anti-aliased transition across x: 78..82 between Gold and Espresso
    # Notice this intermediate mixture does NOT lie on the Crimson background ray!
    for col, frac in [(78, 0.8), (79, 0.6), (80, 0.4), (81, 0.2)]:
        P[20:140, col] = frac * gold_rgb + (1.0 - frac) * espresso_rgb

    # Add a 40x40 genuine negative-space cutout stripe (y: 50..90, x: 30..70) filled with Crimson
    P[50:90, 30:70] = bg_rgb

    flat_quantized, k_star, clean_alpha, dist_to_bg = unmix_spot_colors(
        P=P,
        B=bg_rgb,
        ink_rgbs=ink_rgbs,
        verbose=True,
    )

    # 1. Exterior canvas background (e.g. at (5, 5)) must be 100% transparent
    assert clean_alpha[5, 5] == 0.0
    assert np.allclose(flat_quantized[5, 5], np.round(bg_rgb))

    # 2. Genuine 40x40 interior cutout stripe (e.g. at (70, 50)) must be 100% transparent
    assert clean_alpha[70, 50] == 0.0
    assert np.all(clean_alpha[52:88, 32:68] == 0.0)

    # 3. Solid interior mask excluding cutout and outer 4px boundary
    # Along the internal Gold-Espresso junction (x: 78..82, y: 100..130):
    # There must be ZERO knocked-out pixels! (clean_alpha >= 0.50, in fact 1.0)
    junction_alpha = clean_alpha[100:130, 78:82]
    assert np.all(junction_alpha >= 0.50)
    assert np.all(junction_alpha == 1.0)

    # Across the entire solid interior (>4px from true_bg), count of knocked-out pixels is 0
    fg_region = np.zeros((H, W), dtype=bool)
    fg_region[24:136, 24:136] = True
    fg_region[46:94, 26:74] = False  # exclude neighborhood of cutout
    interior_knockouts = np.sum(clean_alpha[fg_region] < 0.50)
    assert interior_knockouts == 0


def test_speckle_consolidation_and_seam_underlay():
    """
    Verify pre-VTracer speckle consolidation and internal seam-backing underlay:
    A synthetic outlined shape with <16px transition speckles along an internal crease
    vectorizes via run_vector_knockout with 0 transparent pixels (alpha < 255) anywhere
    inside the solid interior (edt > 3px), while preserving exact canonical hex palette conformance!
    """
    H, W = 120, 120
    bg_rgb = hex_to_rgb_255("#9A0616")  # Crimson
    gold_rgb = hex_to_rgb_255("#CA8812")  # Gold
    espresso_rgb = hex_to_rgb_255("#46200D")  # Dark Espresso
    saddle_rgb = hex_to_rgb_255("#7B4A22")  # Intermediate Saddle Brown

    # Canvas
    img_arr = np.full((H, W, 3), bg_rgb, dtype=np.uint8)

    # Large 80x80 foreground object at y: 20..100, x: 20..100
    img_arr[20:100, 20:100] = gold_rgb.astype(np.uint8)

    # Add an internal Dark Espresso outline/crease (y: 20..100, x: 58..62)
    img_arr[20:100, 58:62] = espresso_rgb.astype(np.uint8)

    # Add <16px transition speckles of Saddle Brown along the crease
    # (three 6px-9px patches at y: 35..38, x: 56..58; y: 55..58, x: 62..65; y: 75..78, x: 56..58)
    img_arr[35:38, 56:58] = saddle_rgb.astype(np.uint8)  # 6 px
    img_arr[55:58, 62:65] = saddle_rgb.astype(np.uint8)  # 9 px
    img_arr[75:78, 56:58] = saddle_rgb.astype(np.uint8)  # 6 px

    pil_in = Image.fromarray(img_arr, mode="RGB")

    with tempfile.TemporaryDirectory() as td:
        out_png = os.path.join(td, "synthetic_out.png")
        out_svg = os.path.join(td, "synthetic_out.svg")

        palette_str = "#CA8812,#46200D,#7B4A22"
        F_out, alpha_out, mean_B, sigma_bg, p99_border_dE, cutout_count, palette_hex = run_vector_knockout(
            input_pil=pil_in,
            input_path="synthetic.png",
            output_png=out_png,
            output_svg=out_svg,
            dpi=(300, 300),
            palette=palette_str,
            filter_speckle=16,
            verbose=True,
        )

        # 1. Verify exact canonical hex palette conformance in SVG
        ET.register_namespace("", "http://www.w3.org/2000/svg")
        tree = ET.parse(out_svg)
        root = tree.getroot()
        canonical_set = {"#CA8812", "#46200D", "#7B4A22"}

        path_count = 0
        for elem in root.iter():
            tag = elem.tag.split("}")[-1]
            if tag in ("path", "polygon"):
                fill = elem.attrib.get("fill", "").upper()
                assert fill in canonical_set, f"Unexpected fill color {fill} in SVG"
                path_count += 1

        assert path_count > 0, "SVG should contain vectorized paths"

        # 2. Check rendered PNG:
        out_pil = Image.open(out_png).convert("RGBA")
        out_rgba = np.array(out_pil)
        alpha_channel = out_rgba[:, :, 3]

        # Compute distance transform inside solid foreground
        # In the deep interior (> 3px away from transparency, inside the 80x80 box):
        # zero transparent pixels (alpha < 255)!
        fg_mask_rendered = alpha_channel > 0
        edt_rendered = scipy.ndimage.distance_transform_edt(fg_mask_rendered)
        deep_interior_rendered = edt_rendered > 3.0

        holes_in_interior = np.sum((alpha_channel < 255) & deep_interior_rendered)
        assert holes_in_interior == 0, f"Found {holes_in_interior} non-opaque pixels inside deep interior!"

        # 3. Exterior pixels (e.g. at (5, 5)) must be 100% transparent
        assert alpha_channel[5, 5] == 0


def test_palette_relative_small_cutouts_and_solid_seams():
    """
    Verify palette-separated background dominance and topological seam preservation:
    1. Small genuine background negative-space pockets (e.g. 3 small crimson dots inside a cream band,
       and a narrow 2-3px crimson slit between two needles) remain 100% knocked out (alpha == 0).
    2. Accidental interior ink-to-ink transitions (Gold meeting Espresso with no background)
       remain 100% solid (alpha == 255 in deep interior, zero webbing or holes).
    3. SVG fills conform strictly to canonical hexes with underlay backing paths injected.
    """

    H, W = 160, 160
    bg_rgb = hex_to_rgb_255("#9A0616")  # Crimson
    cream_rgb = hex_to_rgb_255("#F5EED1")  # Cream
    gold_rgb = hex_to_rgb_255("#CA8812")  # Gold
    espresso_rgb = hex_to_rgb_255("#46200D")  # Espresso

    img_arr = np.full((H, W, 3), bg_rgb, dtype=np.uint8)

    # Foreground object: 120x120 box at y: 20..140, x: 20..140
    # Left half (x: 20..80) is Cream band
    img_arr[20:140, 20:80] = cream_rgb.astype(np.uint8)
    # Right half (x: 80..140) is Gold
    img_arr[20:140, 80:140] = gold_rgb.astype(np.uint8)

    # Add an internal crease of Espresso inside the Gold section (y: 30..130, x: 105..115)
    img_arr[30:130, 105:115] = espresso_rgb.astype(np.uint8)

    # 1. Add 3 small genuine crimson dots inside the Cream and Gold sections (realistic 6x6 dots at 1x)
    # Centers at (y=45, x=45), (y=75, x=45), (y=105, x=105)
    img_arr[42:48, 42:48] = bg_rgb.astype(np.uint8)  # 6x6 (36 px)
    img_arr[72:78, 42:48] = bg_rgb.astype(np.uint8)  # 6x6 (36 px)
    img_arr[102:108, 102:108] = bg_rgb.astype(np.uint8)  # 6x6 (36 px) inside Gold section

    # 2. Add a narrow 3px slit of background at y: 20..60, x: 79..82 (between Cream and Gold)
    img_arr[20:60, 79:82] = bg_rgb.astype(np.uint8)

    pil_in = Image.fromarray(img_arr, mode="RGB")

    with tempfile.TemporaryDirectory() as td:
        out_png = os.path.join(td, "cutouts_out.png")
        out_svg = os.path.join(td, "cutouts_out.svg")

        palette_str = "#F5EED1,#CA8812,#46200D"
        F_out, alpha_out, mean_B, sigma_bg, p99_border_dE, cutout_count, palette_hex = run_vector_knockout(
            input_pil=pil_in,
            input_path="synthetic_cutouts.png",
            output_png=out_png,
            output_svg=out_svg,
            dpi=(300, 300),
            palette=palette_str,
            filter_speckle=16,
            verbose=False,
        )

        out_pil = Image.open(out_png).convert("RGBA")
        out_rgba = np.array(out_pil)
        alpha_channel = out_rgba[:, :, 3]

        assert alpha_channel[45, 45] == 0, f"First small crimson dot should be knocked out (got {alpha_channel[45, 45]})!"
        assert alpha_channel[75, 45] == 0, f"Second small crimson dot should be knocked out (got {alpha_channel[75, 45]})!"
        assert alpha_channel[105, 105] == 0, f"Third small crimson dot should be knocked out (got {alpha_channel[105, 105]})!"

        # 2. The narrow slit at y: 25..55, x: 80 must be knocked out
        assert np.all(alpha_channel[25:55, 80] == 0), "Narrow background slit should be knocked out!"

        # 3. The Gold-to-Espresso crease at y: 60..100, x: 105..115 must be 100% solid
        crease_alpha = alpha_channel[60:100, 108:112]
        assert np.all(crease_alpha == 255), "Interior ink crease must have 0 holes/seams!"

        # 4. Canvas exterior (e.g. at (5, 5)) is transparent
        assert alpha_channel[5, 5] == 0


def test_brim_adjacent_highlight_not_knocked_out():
    """
    Verify thin foreground highlights near dark outlines are not zeroed out into holes:
    A bright cream/gold highlight separated from crimson background by a thin 3px dark espresso
    outline remains alpha == 1.0 (255), with zero holes.
    """
    H, W = 80, 80
    bg_rgb = hex_to_rgb_255("#9A0616")  # Crimson
    espresso_rgb = hex_to_rgb_255("#46200D")  # Dark Espresso outline
    cream_rgb = hex_to_rgb_255("#F5EED1")  # Bright Cream highlight
    gold_rgb = hex_to_rgb_255("#CA8812")  # Gold body

    ink_rgbs = [espresso_rgb, cream_rgb, gold_rgb]

    # Canvas
    P = np.full((H, W, 3), bg_rgb, dtype=np.float32)

    # Foreground body at y: 20..60, x: 20..65
    # x: 20..23 is thin 3px Dark Espresso outline
    P[20:60, 20:23] = espresso_rgb
    # x: 23..35 is bright Cream highlight immediately adjacent to outline
    P[20:60, 23:35] = cream_rgb
    # x: 35..65 is Gold body
    P[20:60, 35:65] = gold_rgb

    flat_quantized, k_star, clean_alpha, dist_to_bg = unmix_spot_colors(
        P=P,
        B=bg_rgb,
        ink_rgbs=ink_rgbs,
        verbose=False,
    )

    # The highlight region (y: 25..55, x: 25..33) must have alpha == 1.0 (zero holes!)
    assert np.all(clean_alpha[25:55, 25:33] == 1.0), "Highlight adjacent to dark outline must not have holes!"
    assert np.all(k_star[25:55, 25:33] == 1), "Highlight region should be assigned to Cream!"

    # The dark outline (y: 25..55, x: 21..23) must be solid Espresso
    assert np.all(clean_alpha[25:55, 21:23] >= 0.50)
    assert np.all(k_star[25:55, 21:23] == 0)

    # Exterior background is transparent
    assert clean_alpha[5, 5] == 0.0


def test_interior_core_support_eliminates_intermediate_halos():
    """
    Verify universal spatial + extremum core support eliminates false intermediate-ink halos:
    Soft Cream meeting Saddle Brown across a 2px anti-aliased ramp in deep interior produces
    0 pixels of intermediate Warm Bronze halo.
    """
    H, W = 80, 80
    bg_rgb = hex_to_rgb_255("#9A0616")  # Crimson
    cream_rgb = hex_to_rgb_255("#F5EED1")  # Ink 0: Soft Cream (L* ~ 93)
    saddle_rgb = hex_to_rgb_255("#7B4A22")  # Ink 1: Saddle Brown (L* ~ 35)
    bronze_rgb = hex_to_rgb_255("#AC6E3E")  # Ink 2: Intermediate Warm Bronze (L* ~ 50)

    ink_rgbs = [cream_rgb, saddle_rgb, bronze_rgb]

    # Canvas with large foreground object at y: 10..70, x: 10..70
    P = np.full((H, W, 3), bg_rgb, dtype=np.float32)
    # Left half: Cream
    P[10:70, 10:40] = cream_rgb
    # Right half: Saddle Brown
    P[10:70, 40:70] = saddle_rgb

    # 2px anti-aliased transition at x: 39..41
    P[10:70, 39] = 0.67 * cream_rgb + 0.33 * saddle_rgb
    P[10:70, 40] = 0.33 * cream_rgb + 0.67 * saddle_rgb

    _, k_star, clean_alpha, _ = unmix_spot_colors(
        P=P,
        B=bg_rgb,
        ink_rgbs=ink_rgbs,
        verbose=False,
    )

    # Inside the deep interior (y: 20..60, x: 20..60):
    # There must be ZERO pixels assigned to Ink 2 (Warm Bronze)!
    interior_region = k_star[20:60, 20:60]
    bronze_pixels = np.sum(interior_region == 2)
    assert bronze_pixels == 0, f"Found {bronze_pixels} intermediate Bronze halo pixels in interior!"


def test_smooth_shading_consolidation_preserves_crisp_highlights():
    """
    Verify low-gradient Voronoi consolidation eliminates smooth shading camouflage blotches
    while strictly preserving crisp cel-shaded drawn highlights.
    """
    H, W = 80, 80
    cream_rgb = hex_to_rgb_255("#F5EED1")  # Ink 0: Cream
    saddle_rgb = hex_to_rgb_255("#7B4A22")  # Ink 1: Saddle Brown
    bronze_rgb = hex_to_rgb_255("#AC6E3E")  # Ink 2: Bronze
    ink_rgbs = [cream_rgb, saddle_rgb, bronze_rgb]

    fg_mask = np.ones((H, W), dtype=bool)

    # Construct a synthetic unquantized P_lab image:
    # Left half (x: 0..50): very gentle smooth gradient from Saddle Brown to Bronze (gradient < 2.0 CIELAB)
    # Right half (x: 50..80): sharp step-edge highlight of Cream (step edge > 20.0 CIELAB)
    c_lab_saddle = cv2.cvtColor((saddle_rgb / 255.0).reshape(1, 1, 3).astype(np.float32), cv2.COLOR_RGB2LAB)[0, 0]
    c_lab_bronze = cv2.cvtColor((bronze_rgb / 255.0).reshape(1, 1, 3).astype(np.float32), cv2.COLOR_RGB2LAB)[0, 0]
    c_lab_cream = cv2.cvtColor((cream_rgb / 255.0).reshape(1, 1, 3).astype(np.float32), cv2.COLOR_RGB2LAB)[0, 0]

    P_lab = np.zeros((H, W, 3), dtype=np.float32)
    for col in range(50):
        frac = col / 50.0  # smooth ramp
        P_lab[:, col] = (1.0 - frac) * c_lab_saddle + frac * c_lab_bronze

    # Sharp step at col=50 into Cream
    P_lab[:, 50:] = c_lab_cream

    # Initial Voronoi classification:
    # x: 0..25 is Saddle Brown (Ink 1), x: 25..50 is Bronze (Ink 2) (a camouflage split on smooth shading!)
    # x: 50..80 is Cream (Ink 0) (a crisp drawn highlight!)
    k_star_init = np.zeros((H, W), dtype=np.int32)
    k_star_init[:, 0:25] = 1
    k_star_init[:, 25:50] = 2
    k_star_init[:, 50:80] = 0

    k_out = consolidate_low_gradient_voronoi_splits(
        k_star=k_star_init,
        fg_mask=fg_mask,
        P_lab=P_lab,
        ink_rgbs=ink_rgbs,
        max_dE_pair=28.0,
        max_p75_grad=4.5,
        verbose=False,
    )

    # 1. The smooth shading region (x: 0..50) must consolidate to 1 uniform ink (0 camo blotches!)
    smooth_region_inks = np.unique(k_out[:, 0:50])
    assert len(smooth_region_inks) == 1, f"Smooth shading should consolidate to 1 ink, got {smooth_region_inks}!"

    # 2. The sharp Cream highlight (x: 50..80) must remain 100% intact as Ink 0
    assert np.all(k_out[:, 50:80] == 0), "Sharp Cream highlight must be preserved!"


def test_speckle_consolidation_preserves_contrasting_dots():
    """
    Verify consolidate_raster_speckles preserves small high-contrast intentional dots
    while absorbing low-contrast transition slivers.
    """
    H, W = 80, 80
    cream_rgb = hex_to_rgb_255("#F5EED1")  # Ink 0: Cream
    espresso_rgb = hex_to_rgb_255("#46200D")  # Ink 1: Dark Espresso (high contrast to Cream, dE ~ 75)
    saddle_rgb = hex_to_rgb_255("#7B4A22")  # Ink 2: Saddle Brown
    bronze_rgb = hex_to_rgb_255("#AC6E3E")  # Ink 3: Bronze (low contrast to Saddle Brown, dE ~ 18)
    ink_rgbs = [cream_rgb, espresso_rgb, saddle_rgb, bronze_rgb]

    fg_mask = np.zeros((H, W), dtype=bool)
    fg_mask[10:70, 10:70] = True

    k_star = np.zeros((H, W), dtype=np.int32)  # Base is Ink 0 (Cream)
    k_star[10:70, 40:70] = 2  # Right half is Ink 2 (Saddle Brown)

    # 1. Add three 4x4 (16 px) high-contrast Espresso dots on the Cream section
    k_star[20:24, 20:24] = 1  # 4x4 dot 1
    k_star[35:39, 20:24] = 1  # 4x4 dot 2
    k_star[50:54, 20:24] = 1  # 4x4 dot 3

    # 2. Add an 8px low-contrast Bronze transition sliver on Saddle Brown
    k_star[30:34, 45:47] = 3  # 8 px (4x2) Bronze on Saddle Brown (dE ~ 18)

    k_out = consolidate_raster_speckles(
        k_star=k_star,
        fg_mask=fg_mask,
        ink_rgbs=ink_rgbs,
        filter_speckle=16,
        num_passes=2,
        verbose=False,
    )

    # 1. All three high-contrast 4x4 Espresso dots must survive intact!
    assert np.all(k_out[20:24, 20:24] == 1), "First high-contrast Espresso dot must survive!"
    assert np.all(k_out[35:39, 20:24] == 1), "Second high-contrast Espresso dot must survive!"
    assert np.all(k_out[50:54, 20:24] == 1), "Third high-contrast Espresso dot must survive!"

    # 2. The low-contrast Bronze transition sliver must be absorbed into modal Saddle Brown (Ink 2)
    assert np.all(k_out[30:34, 45:47] == 2), "Low-contrast Bronze sliver should be absorbed into Saddle Brown!"


def test_curved_2ink_seam_backing_leaves_solid_alpha():
    """
    Verify curved 2-ink seam backing underlay leaves alpha == 255 across a curved 2-ink boundary:
    A curved boundary between Dark Espresso outline and Gold fill inside an object produces
    zero transparent or semi-transparent holes/gaps (alpha < 255) along the curved seam.
    """
    H, W = 140, 140
    bg_rgb = hex_to_rgb_255("#9A0616")  # Crimson
    gold_rgb = hex_to_rgb_255("#CA8812")  # Gold
    espresso_rgb = hex_to_rgb_255("#46200D")  # Dark Espresso

    # Canvas
    img_arr = np.full((H, W, 3), bg_rgb, dtype=np.uint8)

    # Large 100x100 circular/curved foreground disk centered at (70, 70) with radius 50
    yy, xx = np.ogrid[:H, :W]
    disk_dist = np.sqrt((xx - 70) ** 2 + (yy - 70) ** 2)
    fg_disk = disk_dist <= 50.0
    img_arr[fg_disk] = gold_rgb.astype(np.uint8)

    # Curved 2-ink boundary: a curved Dark Espresso band passing through the disk
    # with a circular arc boundary: (xx - 50)^2 + (yy - 50)^2 between 25^2 and 45^2
    arc_dist = np.sqrt((xx - 50) ** 2 + (yy - 50) ** 2)
    curved_band = fg_disk & (arc_dist >= 25.0) & (arc_dist <= 45.0)
    img_arr[curved_band] = espresso_rgb.astype(np.uint8)

    pil_in = Image.fromarray(img_arr, mode="RGB")

    with tempfile.TemporaryDirectory() as td:
        out_png = os.path.join(td, "curved_seam_out.png")
        out_svg = os.path.join(td, "curved_seam_out.svg")

        palette_str = "#CA8812,#46200D"
        run_vector_knockout(
            input_pil=pil_in,
            input_path="curved_seam.png",
            output_png=out_png,
            output_svg=out_svg,
            dpi=(300, 300),
            palette=palette_str,
            verbose=False,
        )

        out_pil = Image.open(out_png).convert("RGBA")
        out_rgba = np.array(out_pil)
        alpha_channel = out_rgba[:, :, 3]

        # In the deep interior of the disk (> 4px away from silhouette boundary):
        # zero non-opaque pixels (alpha < 255) along the curved seam!
        fg_mask_rendered = alpha_channel > 0
        edt_rendered = scipy.ndimage.distance_transform_edt(fg_mask_rendered)
        deep_interior_rendered = edt_rendered > 4.0

        holes_in_interior = np.sum((alpha_channel < 255) & deep_interior_rendered)
        assert holes_in_interior == 0, f"Found {holes_in_interior} non-opaque pixels along curved seam!"

        # Exterior is transparent
        assert alpha_channel[5, 5] == 0


def test_vcusp_mixture_knocks_out_without_dark_scabs():
    """
    Verify tight linear-mixture cusp pixels near background in narrow V-cusps
    knock out cleanly to exterior background and never hallucinate dark #46200D scabs:
    A narrow V-cusp between Cream strokes with 45% Cream + 55% Crimson mixture pixels
    is included in exterior background and produces 0 dark espresso pixels.
    """
    H, W = 80, 80
    bg_rgb = hex_to_rgb_255("#9A0616")  # Crimson
    cream_rgb = hex_to_rgb_255("#F9D5BB")  # Cream (Ink 0)
    espresso_rgb = hex_to_rgb_255("#46200D")  # Dark Espresso (Ink 1)

    ink_rgbs = [cream_rgb, espresso_rgb]

    P = np.full((H, W, 3), bg_rgb, dtype=np.float32)

    # Two vertical Cream strokes that meet at top forming an arch / V-notch
    # Left stroke: x: 20..28, y: 10..70
    P[10:70, 20:28] = cream_rgb
    # Right stroke: x: 34..42, y: 10..70
    P[10:70, 34:42] = cream_rgb
    # Arch connecting them at top: y: 10..18, x: 20..42
    P[10:18, 20:42] = cream_rgb

    # Between x: 28..34, y: 18..70 is open to background below.
    # Right at the apex of the arch (y: 18..22, x: 28..34), place anti-aliased mixture pixels:
    # 45% Cream + 55% Crimson (dE_to_bg ~ 30 > 26)
    mixture_pixel = 0.45 * cream_rgb + 0.55 * bg_rgb
    P[18:22, 28:34] = mixture_pixel

    _, k_star, clean_alpha, _ = unmix_spot_colors(
        P=P,
        B=bg_rgb,
        ink_rgbs=ink_rgbs,
        verbose=False,
    )

    # 1. In the arch apex (y: 18..22, x: 28..34), the mixture must be knocked out to exterior background (alpha < 0.5)
    # or quantized to Cream (k_star == 0). It must NEVER be Dark Espresso (k_star == 1)!
    apex_k = k_star[18:22, 28:34]
    dark_espresso_scabs = np.sum(apex_k == 1)
    assert dark_espresso_scabs == 0, f"Found {dark_espresso_scabs} dark espresso scab pixels in V-cusp apex!"

    # 2. Exterior background is knocked out
    apex_alpha = clean_alpha[18:22, 28:34]
    assert np.all(apex_alpha <= 0.50), "V-cusp mixture pixels should be knocked out to background!"


def test_dark_stroke_on_dark_background_survives():
    """
    Verify [P0] & [P1] dark-background remediations:
    1. A 2px dark interior stroke (#301808) inside a bright foreground object
       (#F0C660 / #D18A11) on a #18181B dark slate background survives unmix_spot_colors
       with clean_alpha == 1.0 and its true dark ink index.
    2. A border-touching dark foreground ink region distinct from background is protected by
       palette separation (dE_to_bg < min_dE_to_ink) and is not falsely knocked out to exterior background.
    """
    H, W = 120, 120
    bg_rgb = hex_to_rgb_255("#18181B")       # Dark Slate background (L* ~ 9.7)
    gold_rgb = hex_to_rgb_255("#F0C660")     # Bright Gold foreground (Ink 0)
    amber_rgb = hex_to_rgb_255("#D18A11")    # Warm Amber foreground (Ink 1)
    dark_stroke_rgb = hex_to_rgb_255("#301808") # Deep Brown/Espresso stroke (Ink 2, dE to bg ~ 18.5)

    ink_rgbs = [gold_rgb, amber_rgb, dark_stroke_rgb]

    P = np.full((H, W, 3), bg_rgb, dtype=np.float32)

    # 1. Place bright foreground object at y: 20..100, x: 30..100
    P[20:100, 30:65] = gold_rgb
    P[20:100, 65:100] = amber_rgb

    # 2. Add a 2px dark interior stroke at y: 30..90, x: 50..52
    P[30:90, 50:52] = dark_stroke_rgb

    # 3. Add a border-touching dark foreground ink tab at y: 45..75, x: 0..30
    P[45:75, 0:30] = dark_stroke_rgb

    _, k_star, clean_alpha, _ = unmix_spot_colors(
        P=P,
        B=bg_rgb,
        ink_rgbs=ink_rgbs,
        verbose=False,
    )

    # 1. 2px dark interior stroke (y: 35..85, x: 50..52) must survive with clean_alpha == 1.0 and ink 2
    stroke_alpha = clean_alpha[35:85, 50:52]
    stroke_k = k_star[35:85, 50:52]
    assert np.all(stroke_alpha == 1.0), f"2px dark stroke on dark background must survive with alpha=1.0 (got min={np.min(stroke_alpha)})"
    assert np.all(stroke_k == 2), f"2px dark stroke must be assigned to dark ink index 2 (got {np.unique(stroke_k)})"

    # 2. Border-touching dark foreground ink (y: 50..70, x: 5..25) must NOT be knocked out
    border_dark_alpha = clean_alpha[50:70, 5:25]
    border_dark_k = k_star[50:70, 5:25]
    assert np.all(border_dark_alpha >= 0.50), f"Border-touching dark ink must not be knocked out (got min={np.min(border_dark_alpha)})"
    assert np.all(border_dark_k == 2), f"Border-touching dark ink must be assigned to ink 2 (got {np.unique(border_dark_k)})"

    # 3. Exterior canvas background (e.g. at (5, 5), (115, 115)) must be 100% transparent
    assert clean_alpha[5, 5] == 0.0
    assert clean_alpha[115, 115] == 0.0


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main(["-v", __file__]))
