# pixelpress

Production-grade graphic and illustration prepress tools for Antigravity (AGY), featuring Real-ESRGAN + Edge-Snap + VTracer Cutout SVG vectorization as its primary workflow, alongside decoupled Chroma-Smoothed Closed-Form Alpha Matting, Known-$B$ Vector Color-Line Unmixing, and discrete silkscreen spot-color separation.

---

## Architecture Overview

`pixelpress` provides two specialized prepress pipelines depending on the artwork style and print destination:

### 1. Vector Prepress Pipeline (`--mode vector`, Primary Default)
Designed for graphic illustrations, vector-style artwork, cel-shaded art, anime, logos, and typography:
1. **Real-ESRGAN 2x Upscaling & Denoising**: Removes DCT compression blockiness and doubles pixel resolution using `realesrgan-x4plus-anime` or `realesr-animevideov3`.
2. **CIELAB Bilateral Smoothing**: Light filtering strictly on $a^*$ and $b^*$ with $L^*$ untouched for flat color fills without bleeding.
3. **Continuous Background Field $B(x, y)$**: Robust 2D degree-2 polynomial surface fitting via IRLS Huber loss.
4. **Tight-Crotch Core Masking & Euclidean Color Propagation**: Propagates core foreground colors ($F_{\text{nearest}}$) into boundary transitions, preventing dark color fringing in tight crotches.
5. **Ridge Rescue & Boundary Edge Snapping**: Preserves thin 1–3px filigree lines while snapping transition pixels to $F_{\text{nearest}}$ and exterior background to exact flat background color.
6. **VTracer Cutout Splines & SVG Cleanup**: Generates hierarchical cut-ready SVGs with native print dimensions, removes outer border rects/paths, and preserves protected interior motifs (`--protect-interior-box`, `--protect-poly`).
7. **Native 300 DPI Rendering**: Direct rendering via `resvg` to a 300 DPI transparent RGBA PNG.

### 2. Continuous Raster Prepress Pipeline (`--mode dtg / dtf / screenprint`)
Designed for continuous-tone or photographic raster artwork:
- **`--mode dtg`**: Continuous 24-bit Closed-Form Laplacian alpha unmixing (zero quantization).
- **`--mode dtf`**: Continuous unmixing with firm alpha floor ramp (`--dtf-alpha-floor 0.15`).
- **`--mode screenprint`**: Discrete spot-color quantization ($K \le 8$ inks) with binary mesh alpha.

Both pipelines automatically generate 2x2 verification inspection sheets (`-p`) and 1:1 native-resolution diagnostic crop strips (`--verify-crops-dir`).

---

## Skills

- **`knockout-quantize`** ([`skills/knockout-quantize/SKILL.md`](skills/knockout-quantize/SKILL.md)): Standalone PEP 723 CLI script (`knockout_quantize.py`) executing the two-stage PyMatting + Known-$B$ unmixing pipeline and ROI refinement workflows.
- **`print-image-review`** ([`skills/print-image-review/SKILL.md`](skills/print-image-review/SKILL.md)): Prepress visual and quantitative audit protocol evaluating 2x2 verification sheets and 1:1 native-resolution diagnostic crop strips across Dark Charcoal (`#18181B`), Clean White (`#FFFFFF`), and Warm Cream (`#F4EFE6`) backdrops.

---

## Subagents

- **`print-knockout-specialist`** ([`agents/print-knockout-specialist.md`](agents/print-knockout-specialist.md)): Autonomous prepress technician that determines print technology, executes `knockout_quantize.py`, generates verification artifacts, and applies declarative ROI overrides.
- **`print-image-reviewer`** ([`agents/print-image-reviewer.md`](agents/print-image-reviewer.md)): Read-only prepress quality auditor that inspects 2x2 verification sheets and 1:1 diagnostic crops for edge fringing, dark rims, broken filigree, and cutout integrity.

---

## CLI Quickstart

The tool is packaged as a self-contained PEP 723 Python script executable anywhere via `uv run`:

```bash
# Vector Prepress Knockout & Cutout SVG (Primary Default)
uv run skills/knockout-quantize/scripts/knockout_quantize.py \
  artwork.jpg \
  --mode vector \
  -o artwork_transparent.png \
  --svg-output artwork.svg \
  -p preview_composite.png \
  --verify-crops-dir ./crops

# Continuous DTG Digital Printing
uv run skills/knockout-quantize/scripts/knockout_quantize.py \
  artwork.jpg \
  --mode dtg \
  -o artwork_dtg.png \
  -p preview_composite.png \
  --verify-crops-dir ./crops

# Direct-to-Film (DTF) Transfers
uv run skills/knockout-quantize/scripts/knockout_quantize.py \
  artwork.jpg \
  --mode dtf \
  --dtf-alpha-floor 0.15 \
  -o artwork_dtf.png \
  -p preview_dtf.png \
  --verify-crops-dir ./crops

# Traditional Screen Printing (Spot-Color Separation)
uv run skills/knockout-quantize/scripts/knockout_quantize.py \
  artwork.jpg \
  --mode screenprint \
  --colors 6 \
  -o artwork_screenprint.png \
  -p preview_screenprint.png \
  --verify-crops-dir ./crops

# Declarative ROI Override (Preserve Enclosed Decorative Stripes)
uv run skills/knockout-quantize/scripts/knockout_quantize.py \
  artwork.jpg \
  --mode dtg \
  --protect-interior-box 400,500,800,900 \
  -o artwork_protected.png \
  -p preview_protected.png

# Topological Outline Gap Sealing & Non-Rectangular Solid Protection
uv run skills/knockout-quantize/scripts/knockout_quantize.py \
  artwork.jpg \
  --mode dtg \
  --seal-box 400,500,800,900:20 \
  --protect-poly 400,500,450,420,500,500 \
  -o artwork_sealed.png \
  -p preview_sealed.png
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.
