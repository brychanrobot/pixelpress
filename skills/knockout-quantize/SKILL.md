---
name: knockout-quantize
description: Knockout background removal, continuous color decontaminating unmixing (DTG/Web), and discrete spot-color screen-print quantization for illustrations and apparel graphics.
---

# Knockout Background Removal, Continuous Unmixing & Discrete Color Quantization

`knockout-quantize` is a mathematically rigorous CLI tool and workflow for decomposing raster graphic artwork (JPEG, PNG, WebP) into production-grade transparent PNGs with pristine, fringe-free edges. It supports two primary printing paradigms:

1. **Digital DTG (Direct-to-Garment) & DTF (Direct-to-Film)**: Continuous local-envelope background unmixing and edge color decontamination without hard quantization, preserving subtle artist shading and multi-tone highlights.
2. **Traditional Screen Printing (Spot-Color Separation)**: Solid-core Voronoi propagation and discrete $K$-color palette quantization for physical silkscreen separation.

---

## Step 0: Determine Target Printing Technology (DTG vs. DTF vs. Screen Printing)

**Before running any background removal or color unmixing, ALWAYS determine or confirm the target printing technology:**

### 1. DTG (Direct-to-Garment, e.g. Kornit, Brother, Epson) / Digital Sublimation / Web
- **Recommended Mode**: **Continuous Color Unmixing (`--mode dtg`)**
- **Characteristics**: Prints continuous 24-bit CMYK+White ink droplets over an opaque white underbase.
- **Unmixing Physics**:
  1. Fits continuous 2D spatial background field $\vec{B}(y, x)$ from outer margins.
  2. Computes continuous alpha matte $\alpha(y, x) \in [0, 1]$ scaled by local stroke contrast envelope.
  3. Decontaminates anti-aliased edge colors by subtracting background bleed:
     $$\vec{F}_{\text{unmixed}} = \text{clip}\left(\vec{B}(y, x) + \frac{\vec{C}(y, x) - \vec{B}(y, x)}{\max(\alpha, 0.02)},\; 0,\; 255\right)$$
  4. Preserves 100% of smooth artist gradients and multi-tone highlights within stroke interiors while ensuring edge pixels contain zero background bleed.

### 2. DTF (Direct-to-Film Transfers)
- **Recommended Mode**: **Continuous Color Unmixing with Steep Falloff (`--mode dtg --alpha-power 1.3`)**
- **Characteristics**: DTF hot-melt adhesive powder adheres to low-opacity pixels ($< 20\%$ opacity), creating faint glue halos around anti-aliased text.
- **Remedy**: Apply an exponential gamma curve `--alpha-power 1.2` to `1.5` to steepen edge dropoff while maintaining smooth interior shading.

### 3. Traditional Screen Printing (Silkscreen Spot-Color Separation) / Vinyl / Embroidery
- **Recommended Mode**: **Discrete Spot-Color Quantization (`--mode screenprint` / `--quantize`, default when palette is specified)**
- **Characteristics**: Each ink color corresponds to a dedicated physical mesh screen and squeegee pass.
- **Two-Regime Mathematical Physics**:
  - **Step A — Solid Core Seeding**: True interior pixels of each ink are seeded by comparing Euclidean distance against the full palette plus the local background $\vec{B}(y, x)$, requiring ray projection $\alpha_k \ge 0.72$ and local gradient $\|\nabla \vec{C}\| < 25.0$. Collinear intermediate inks (e.g. Tan/Amber intermediate to high-contrast Cream/Gold) are strictly rejected on boundary slopes if local contrast reaches into the higher ink's regime.
  - **Step B — Nearest-Core Voronoi Propagation**: Boundary anti-aliased edge pixels inherit their exact ink identity from their nearest solid core via `distance_transform_edt`. This guarantees that high-contrast strokes (e.g. light typography) maintain uniform ink identity all the way to their outer anti-aliased rim without intermediate ink hijacking.
  - **Step C — Exact Ray-Projected Alpha**: Alpha is computed by directional ray projection along the propagated ink's vector $\vec{V}_k = \vec{F}_k - \vec{B}$. An adaptive noise gate ($\alpha_{\text{raw}} \le 0.18$) automatically wipes out JPEG mosquito ringing proportional to stroke contrast $L_k$.
  - **Step D — Sub-Pixel Detail Preservation**: Fine 1–2px features (delicate stars, filigree, fine dots) that lack a wide solid core are seeded as whole connected components to their best-matching ink ray, and boosted via local ridge maximum filtering so thin strokes retain visibility.
  - **Step E — Pure-Ink Boundary Color**: Every boundary pixel ($\alpha < 255$) uses 100% pure uncontaminated palette ink $\vec{F}_k$, guaranteeing zero background color bleed.

---

## Resolution & DPI Preservation Gate

In prepress and screen printing production, downscaling or altering image resolution destroys fine mesh registration and causes blurred halftone dots:

> [!IMPORTANT]
> **Resolution & DPI Preservation Gate**:
> 1. The output transparent PNG dimensions ($W \times H$) MUST strictly match the source raster image dimensions ($100\%$ pixel-identical, zero unintended downscaling).
> 2. Original DPI metadata (`info.get('dpi')`, typically 72, 150, or 300 DPI) must be preserved in the output PNG header.
> 3. If upscaling is explicitly requested for oversized garment prints or wide banners, use the optional `--scale <factor>` flag (e.g. `--scale 2`), which applies high-order Lanczos resampling.

---

## CLI Tool Reference

The CLI tool is located at `skills/knockout-quantize/scripts/knockout_quantize.py`. It is a self-contained PEP 723 script executable via `uv run`:

```bash
uv run /home/bryant/fast/code/pixelpress/skills/knockout-quantize/scripts/knockout_quantize.py [OPTIONS] <input-image>
```

### Options & Arguments

| Argument | Flag | Default | Description |
| :--- | :--- | :--- | :--- |
| `input` | *(positional)* | *(required)* | Path to input image file (JPEG, PNG, WebP). |
| `--output` | `-o` | `<stem>_transparent.png` | Destination path for output transparent PNG. |
| `--preview` | `-p` | None (or `<stem>_preview.png`) | Path for 4-panel verification composite image. |
| `--mode` | | `screenprint` | `screenprint` (discrete palette quantization) or `dtg` (continuous unmixing). |
| `--quantize`| | `False` | Shortcut flag for `--mode screenprint`. |
| `--palette` | | None | Comma-separated hex list of spot colors (e.g. `'#F5E0C9,#CC8F68,#E09622'`). |
| `-k`, `--n-colors` | | None | Number of foreground ink colors to auto-discover if palette not provided. |
| `--scale` | `--upscale`| `1` | Upscale factor using Lanczos resampling (default: 1, preserves native resolution). |
| `--inspect` | | `False` | Inspect background plane, noise floor, and palette without writing files. |
| `--bg-color` | | Auto | Manual background hex override (e.g. `'#181818'`). |
| `--margin-pct`| | `0.03` (3%) | Outer border fraction used to fit the background plane. |
| `--tau-low` | | `max(6.0, 3.5*sigma)` | Distance threshold below which pixels are 100% knocked out. |
| `--alpha-solid`| | `0.85` | Ray projection factor for 100% solid interior opacity. |
| `--alpha-knee` | | `0.18` | Directional projection deadzone knee for JPEG noise elimination. |
| `--alpha-power`| | `1.0` | Alpha gamma exponent (1.2–1.5 for DTF adhesive falloff). |
| `--despeckle`| | `20` | Minimum pixel island size for morphological cleanup. |
| `--collinear-threshold` | | `0.75` | Cosine similarity threshold for detecting collinear inks. |
| `--collinear-ratio` | | `0.88` | Norm ratio threshold for intermediate collinear inks. |
| `--json` | | `False` | Output results in structured JSON format. |
| `-v`, `--verbose`| | `False` | Enable diagnostic logging. |

---

## Standard Workflows

### Workflow 1: Silkscreen Spot-Color Quantization (Screen Printing)

When separating flat-color illustration artwork into exact spot-color inks:

```bash
uv run /home/bryant/fast/code/pixelpress/skills/knockout-quantize/scripts/knockout_quantize.py \
  artwork.jpg \
  --palette "#F5E0C9,#CC8F68,#E09622,#97572A,#5E2615,#F3CF74" \
  --mode screenprint \
  -o artwork_transparent.png \
  -p preview_composite.png
```

### Workflow 2: Digital Direct-to-Garment (DTG) Unmixing

When preparing continuous-tone or painterly artwork for DTG digital printing:

```bash
uv run /home/bryant/fast/code/pixelpress/skills/knockout-quantize/scripts/knockout_quantize.py \
  artwork.jpg \
  --mode dtg \
  -o artwork_transparent.png \
  -p preview_dtg.png
```

### Workflow 3: Direct-to-Film (DTF) Transfer Preparation

For DTF heat transfers requiring steep adhesive falloff:

```bash
uv run /home/bryant/fast/code/pixelpress/skills/knockout-quantize/scripts/knockout_quantize.py \
  artwork.jpg \
  --mode dtg \
  --alpha-power 1.3 \
  -o artwork_dtf.png \
  -p preview_dtf.png
```

### Workflow 4: Prepress Image Inspection

Inspect background uniformity, noise floor, and palette candidates before running full unmixing:

```bash
uv run /home/bryant/fast/code/pixelpress/skills/knockout-quantize/scripts/knockout_quantize.py \
  artwork.jpg --inspect --palette "#F5E0C9,#CC8F68,#E09622"
```

---

## Verification & 4-Panel Composite Review

Every unmixing pass produces a 4-panel verification composite (`-p`) that MUST be inspected directly using `view_file`:
1. **Panel 1 (Far-Left)**: Original raster input image.
2. **Panel 2**: Transparent PNG composited over a high-contrast $20 \times 20$ checkerboard.
3. **Panel 3**: Transparent PNG composited over Light Parchment (`#F7F4EB`).
4. **Panel 4 (Far-Right)**: Transparent PNG composited over Dark Charcoal (`#181818`).

Always review with `skills/print-image-review/SKILL.md` before final approval.
