# pixelpress

Production-grade graphic and illustration prepress tools for Antigravity (AGY), featuring Real-ESRGAN 2x super-resolution + Spot-Color Unmixing (Interior Solid CIELAB Guard + Boundary Fractional-Alpha Ray Unmixing with 7x7 Core Support) + $(K+1)$ Cutout Spline VTracer SVG vectorization & canonical hex snapping as the primary workflow, alongside continuous DTG/DTF alpha matting and raster screenprint separation.

---

## Architecture Overview

`pixelpress` provides two specialized prepress pipelines depending on artwork style and print destination:

### 1. Vector Prepress Pipeline (`--mode vector`, Primary Default)
Designed for graphic illustrations, vector-style artwork, cel-shaded art, anime, logos, and typography:
1. **Real-ESRGAN 2x Super-Resolution & DCT Denoising**: Removes DCT compression blockiness and doubles pixel resolution using `realesrgan-x4plus-anime` or `realesr-animevideov3`.
2. **Continuous Background Field $B(x, y)$**: Robust 2D degree-2 polynomial surface fitting ($1, x, y, x^2, xy, y^2$) across perimeter garment samples via Iteratively Reweighted Least Squares (IRLS) with Huber loss.
3. **Spot-Color Palette Discovery or Explicit Lock (`--inks`, `--palette`, `--merge-hue-tol`)**: Samples solid foreground interior pixels in CIELAB space with automatic silhouette $K$ selection (default 2..6) and hue consolidation, or locks to an exact comma-separated list of canonical hex colors.
4. **Two-Zone Spot-Color Unmixing (`unmix_spot_colors`)**:
   - *Deep Solid Interior (`edt_inside_fg > 3px`)*: Enforces solid opacity ($\alpha = 1.0$) and direct perceptual CIELAB $\Delta E$ matching so interior highlights and stripes never hallucinate background-blended rays.
   - *Garment Boundary Band (`<= 3px`)*: Fractional-alpha ray unmixing along the directional vector $B \to C_k$ gated by a $7\times 7$ local core support filter (`maximum_filter`), eliminating Gibbs undershoot rims and intermediate transition scabs while preserving delicate 1–2px filigree.
5. **Flat $(K+1)$ Cutout Spline Vectorization & SVG Cleanup (`clean_vtracer_svg`)**: Traces the clean $(K+1)$ quantized composite via VTracer hierarchical cutout splines (`--colormode color --hierarchical cutout --mode spline --filter_speckle 16`), strips background border `<path>`, `<polygon>`, and `<rect>` elements, enforces XXE/DTD security, snaps every foreground path strictly to its canonical uppercase hex ink, and preserves protected interior regions (`--protect-interior-box`, `--protect-poly`, `--seal-box`).
6. **300 DPI Transparent PNG & Proofing Suite**: Rasterizes the clean SVG via `resvg` at native print dimensions and generates `2x2` verification composites (`-p`) plus `1:1` native-resolution diagnostic crop strips (`--verify-crops-dir`).

### 2. Continuous Raster Prepress Pipeline (`--mode dtg / dtf / screenprint`)
Designed for continuous-tone or photographic raster artwork:
- **`--mode dtg`**: Continuous 24-bit Closed-Form Laplacian alpha unmixing (zero quantization) preserving artist gradients and soft shading.
- **`--mode dtf`**: Continuous unmixing with firm alpha floor ramp (`--dtf-alpha-floor 0.15`) to prevent hot-melt adhesive powder scatter on faint edge mist.
- **`--mode screenprint`**: Discrete spot-color quantization ($K \le 8$ inks) with binary/halftone-ready stencil alpha.

Both pipelines automatically generate 2x2 verification inspection sheets (`-p`) and 1:1 native-resolution diagnostic crop strips (`--verify-crops-dir`).

---

## Skills

- **`prepress-vectorize`** ([`skills/prepress-vectorize/SKILL.md`](skills/prepress-vectorize/SKILL.md)): Standalone PEP 723 CLI script (`prepress_vectorize.py`) executing the Spot-Color Unmixing & Cutout Spline VTracer SVG vectorization pipeline, continuous Closed-Form alpha matting (DTG/DTF), silkscreen spot-color separation, and declarative ROI refinement workflows.
- **`print-image-review`** ([`skills/print-image-review/SKILL.md`](skills/print-image-review/SKILL.md)): Prepress visual and quantitative audit protocol evaluating 2x2 verification sheets and 1:1 native-resolution diagnostic crop strips across Dark Charcoal (`#18181B`), Clean White (`#FFFFFF`), and Warm Cream (`#F4EFE6`) backdrops.

---

## Subagents

- **`prepress-specialist`** ([`agents/prepress-specialist.md`](agents/prepress-specialist.md)): Autonomous prepress technician that determines print technology, executes `prepress_vectorize.py`, generates verification artifacts, and applies declarative ROI overrides.
- **`print-image-reviewer`** ([`agents/print-image-reviewer.md`](agents/print-image-reviewer.md)): Read-only prepress quality auditor that inspects 2x2 verification sheets and 1:1 diagnostic crops for edge fringing, dark rims, broken filigree, and cutout integrity.

---

## CLI Quickstart

The tool is packaged as a self-contained PEP 723 Python script executable anywhere via `uv run`:

```bash
# Auto-Palette Vector Extraction (Auto-discovers 2..6 spot inks in CIELAB)
uv run skills/prepress-vectorize/scripts/prepress_vectorize.py \
  artwork.jpg \
  --mode vector \
  --inks 0 \
  --merge-hue-tol 12.0 \
  --filter-speckle 16 \
  -o artwork_transparent.png \
  --svg-output artwork.svg \
  -p preview_composite.png \
  --verify-crops-dir ./crops

# Explicit Canonical Spot-Palette Lock
uv run skills/prepress-vectorize/scripts/prepress_vectorize.py \
  artwork.jpg \
  --mode vector \
  --palette "#DDA032,#CBB39B,#EFE0CE" \
  -o artwork_transparent.png \
  --svg-output artwork.svg \
  -p preview_composite.png \
  --verify-crops-dir ./crops

# Declarative ROI Overrides on Vector Mode (Bridge Gaps & Protect Interior Stripes/Polygons)
uv run skills/prepress-vectorize/scripts/prepress_vectorize.py \
  artwork.jpg \
  --mode vector \
  --palette "#DDA032,#CBB39B,#EFE0CE" \
  --seal-box 400,500,800,900:20 \
  --protect-interior-box 400,500,800,900 \
  --protect-poly 400,500,450,420,500,500 \
  -o artwork_vector.png \
  --svg-output artwork.svg \
  -p preview_vector.png

# Continuous DTG Digital Printing (Continuous 24-bit shading)
uv run skills/prepress-vectorize/scripts/prepress_vectorize.py \
  artwork.jpg \
  --mode dtg \
  -o artwork_dtg.png \
  -p preview_dtg.png \
  --verify-crops-dir ./crops

# Direct-to-Film (DTF) Transfers (Firm adhesive alpha floor)
uv run skills/prepress-vectorize/scripts/prepress_vectorize.py \
  artwork.jpg \
  --mode dtf \
  --dtf-alpha-floor 0.15 \
  -o artwork_dtf.png \
  -p preview_dtf.png \
  --verify-crops-dir ./crops

# Traditional Screen Printing (Discrete spot-color separation)
uv run skills/prepress-vectorize/scripts/prepress_vectorize.py \
  artwork.jpg \
  --mode screenprint \
  --colors 6 \
  -o artwork_screenprint.png \
  -p preview_screenprint.png \
  --verify-crops-dir ./crops
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.
