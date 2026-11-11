# pixelpress

Production-grade graphic and illustration prepress tools for Antigravity (AGY), featuring Real-ESRGAN 2x super-resolution + Spot-Color Unmixing (Topological True-Background Flood-Fill + Interior Solid CIELAB Guard + Pre-VTracer Speckle Consolidation + Internal Seam-Backing Underlay + Boundary Fractional-Alpha Ray Unmixing with 9x9 local core support (4px radius at 2x resolution)) + $(K+1)$ Cutout Spline VTracer SVG vectorization & canonical hex snapping as the primary workflow, alongside continuous DTG/DTF alpha matting and raster screenprint separation.

---

## Architecture Overview

`pixelpress` provides two specialized prepress pipelines depending on artwork style and print destination:

### 1. Vector Prepress Pipeline (`--mode vector`, Primary Default)
Designed for graphic illustrations, vector-style artwork, cel-shaded art, anime, logos, and typography:
1. **Real-ESRGAN 2x Super-Resolution & DCT Denoising**: Removes DCT compression blockiness and doubles pixel resolution using `realesrgan-x4plus-anime` or `realesr-animevideov3`.
2. **Continuous Background Field $B(x, y)$**: Robust 2D degree-2 polynomial surface fitting ($1, x, y, x^2, xy, y^2$) across perimeter garment samples via Iteratively Reweighted Least Squares (IRLS) with Huber loss, passed as a continuous spatial field into spot-color unmixing.
3. **Spot-Color Palette Discovery or Explicit Lock (`--inks`, `--palette`, `--merge-hue-tol`)**: Samples solid foreground interior pixels in CIELAB space with automatic silhouette $K$ selection (default 2..6) and hue consolidation, or locks to an exact comma-separated list of canonical hex colors.
4. **Topological True-Background Flood-Fill & Two-Zone Spot-Color Unmixing (`unmix_spot_colors`)**:
   - *Topological True-Background Partitioning (`segment_topological_foreground`)*: Connects candidate background components to identify exterior canvas background and genuine negative-space cutouts ($\text{area} \ge 24$, inscribed radius $\ge 1.5$ px, 25th-percentile $\Delta E \le 20.0$ with palette-relative $\Delta E_{\text{bg}} < \min \Delta E_{\text{ink}}$ dominance).
   - *Deep Solid Interior (`edt_from_true_bg > 8.0` px at 2x / `4.0` px at 1x)*: Evaluates direct solid perceptual CIELAB distance ($\alpha = 1.0$) across all interior dark outlines, brow/mouth creases, and hat band junctions, guaranteeing zero interior knocked-out pixels.
   - *Garment Boundary Band (`<= 8.0` px at 2x / `4.0` px at 1x)*: Fractional-alpha ray unmixing along directional vectors $\vec{C}_k - \vec{B}$ gated by a $9\times 9$ local core support filter (`maximum_filter`, 4px radius at 2x resolution), eliminating Gibbs undershoot rims while preserving 1–2px filigree.
   - *Post-Alpha Topological Pinhole Seal*: Closes any accidental sub-threshold holes not connected to the exterior canvas or valid cutouts.
5. **Pre-VTracer Speckle Consolidation (`consolidate_raster_speckles`)**: Absorbs small multi-ink transition slivers ($<\text{filter\_speckle}$) into modal adjacent foreground inks before VTracer vectorization, eliminating Swiss-cheese transparent pinholes while keeping freestanding isolated dots intact.
6. **Flat $(K+1)$ Cutout Spline Vectorization & SVG Cleanup (`clean_vtracer_svg`)**: Traces the clean $(K+1)$ quantized composite via VTracer hierarchical cutout splines, strips background paths/polygons, enforces XXE/DTD security, snaps remaining paths strictly to canonical hex inks, and preserves protected interior regions.
7. **Internal Seam-Backing Underlay (`inject_seam_backing_underlay`)**: Identifies multi-ink junctions within the safe interior ($\ge 2$ px inset from all transparency), generates canonical darker-ink backing ribbons, traces them with VTracer, and prepends them as underlays in the SVG to eliminate hairline gaps at sharp Bézier boundaries.
8. **300 DPI Transparent PNG & Proofing Suite**: Rasterizes the clean SVG via `resvg` at native print dimensions and generates `2x2` verification composites (`-p`) plus `1:1` native-resolution diagnostic crop strips (`--verify-crops-dir`).

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
