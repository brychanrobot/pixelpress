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
import numpy as np
import pytest

# Import module under test
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from prepress_vectorize import (
    discover_palette,
    unmix_spot_colors,
    clean_vtracer_svg,
    run_vector_knockout,
    hex_to_rgb_255,
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
    from PIL import Image
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


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main(["-v", __file__]))
