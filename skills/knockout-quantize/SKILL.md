---
name: knockout-quantize
description: Knockout background removal, directional ray-unmixing, and discrete screen-print color quantization for flat-color, illustration, and t-shirt graphics.
---

# Knockout Background Removal & Discrete Screen-Print Color Quantization

`knockout-quantize` is a mathematically rigorous CLI tool and workflow for decomposing raster graphics (JPEG, PNG, WebP) into production-grade transparent PNGs featuring **strictly discrete screen-print ink palettes** with **smooth, halo-free anti-aliased alpha**.

It replaces manual, ad-hoc image unmixing scripts with a universal, zero-bounding-box mathematical pipeline that works across arbitrary continuous backgrounds (black, charcoal, purple, red, navy, green, white) and arbitrary foreground ink counts.

---

## 1. The Core Problem: The Collinear Anti-Aliased Edge Trap

When flat-color or screen-printed artwork is rasterized or saved as JPEG, intermediate anti-aliased edge pixels are formed by optical blending between the foreground ink $F$ and the background color $B$:
$$C(p) = \alpha(p) F + (1 - \alpha(p)) B(p)$$

If an illustration uses multiple inks where one ink $F_m$ (e.g. Tan `#CC8F68` or Stone Beige `#CAAF96`) lies near the line segment between the background $B$ (e.g. Crimson Red `#A80B20` or Deep Purple `#4A2959`) and a brighter ink $F_k$ (e.g. Crisp Cream `#F5E0C9`), a critical ambiguity occurs:
- A 50% anti-aliased edge pixel of Cream over Red has RGB $\approx [206, 117, 116]$.
- This is within $\approx 25$ RGB units of solid Tan $[204, 143, 104]$!
- Naive color clustering or pixel-by-pixel nearest-neighbor classifiers will falsely misclassify the entire outer rim of the Cream text/stars as Tan, creating ugly false fringing halos.

### The Physics-Based Solution (Zero Bounding Boxes)

`knockout_quantize.py` solves this problem purely through **Background-Centered Ray Unmixing** and **Stroke Ridge Coherence**:

1. **Background-Centered Ray Coordinates**:
   $$\vec{\Delta}(p) = C(p) - B(p), \quad \vec{V}_k(p) = F_k - B(p)$$
   - Implied projection alpha:
     $$\alpha_k(p) = \text{clip}\left(\frac{\vec{\Delta}(p) \cdot \vec{V}_k(p)}{\|\vec{V}_k(p)\|_2^2},\; 0,\; 1\right)$$
   - Ray reconstruction residual (perpendicular distance to ray $[0, \vec{V}_k]$):
     $$r_k(p) = \|\vec{\Delta}(p) - \alpha_k(p) \vec{V}_k(p)\|_2$$

2. **Collinear Rim Suppression**:
   - Detects all collinear pairs where $\cos\theta(\vec{V}_m, \vec{V}_k) > 0.88$ and $\|\vec{V}_m\| < 0.85 \|\vec{V}_k\|$.
   - When inspecting candidate core pixels for intermediate ink $m$, looks at the local $5 \times 5$ neighborhood.
   - If the local maximum displacement $\|\vec{\Delta}\|$ jumps toward Ink $k$'s norm ($\max_{5 \times 5} D > L_m + 0.35(L_k - L_m)$), the pixel is on an uphill transition slope to Ink $k$ and is **suppressed from seeding into Ink $m$**.
   - If the local neighborhood forms a flat plateau at $L_m$, the pixel is part of a genuine stroke of Ink $m$ (such as a Tan tree, Tan star, or Stone Beige window arch) and is preserved.

3. **Geodesic Voronoi Propagation & Connected-Component Despeckling**:
   - High-confidence core seeds are propagated to all foreground pixels ($D > \tau_{\text{low}}$) via distance transform.
   - Small isolated noise islands ($\le 4$ px) embedded inside another ink are merged into the surrounding ink.

4. **Exact Directional Alpha Unmixing & Discrete Palette Quantization**:
   - For every pixel assigned to Ink $k$, alpha is computed via projection along $\vec{V}_k(p)$ and passed through a smooth Hermite saturation knee up to $\alpha_{\text{solid}} = 0.88 \to 1.0$.
   - Output RGB is set **strictly and exactly** to the assigned discrete palette ink $F_k$ (and `[0, 0, 0, 0]` where $\alpha = 0$).

---

## 2. CLI Tool Usage & Syntax

The CLI tool is located at `skills/knockout-quantize/scripts/knockout_quantize.py`. It is a self-contained PEP 723 executable that runs directly via `uv run`:

```bash
uv run /home/bryant/fast/code/pixelpress/skills/knockout-quantize/scripts/knockout_quantize.py [OPTIONS] <input-image>
```

### Options & Arguments

| Argument | Flag | Default | Description |
| :--- | :--- | :--- | :--- |
| `input` | *(positional)* | *(required)* | Path to input image file (JPEG, PNG, WebP). |
| `--output` | `-o` | `<stem>_transparent.png` | Destination path for output transparent PNG. |
| `--preview` | `-p` | None (or `<stem>_preview.png`) | Path for 4-panel verification composite image. |
| `--inspect` | | `False` | Run analysis and print palette report without saving output. |
| `--n-colors` | `-k` | `4` | Number of foreground ink colors to discover automatically. |
| `--palette` | | None | Comma-separated hex list (e.g. `'#F5E0C9,#CC8F68,#E09622'`). |
| `--bg-color` | | Auto | Manual background hex override (e.g. `'#181818'`). |
| `--margin-pct`| | `0.03` (3%) | Outer border fraction used to fit the background plane. |
| `--tau-low` | | `max(6.0, 3.5*sigma)` | Distance threshold below which pixels are 100% transparent. |
| `--tau-solid` | | Auto | Minimum distance from background for solid core seeding. |
| `--alpha-solid`| | `0.88` | Projection alpha saturation knee for 100% opacity. |
| `--despeckle`| | `4` | Maximum size of isolated single-ink islands to clean up. |
| `--json` | | `False` | Output inspection / completion metrics in JSON format. |
| `--verbose` | `-v` | `False` | Enable diagnostic logging of intermediate steps. |

---

## 3. Standard Workflows

### Workflow A: Auto-Inspection Mode
Always run `--inspect` first to examine the detected background, noise floor, and palette clusters:

```bash
uv run /home/bryant/fast/code/pixelpress/skills/knockout-quantize/scripts/knockout_quantize.py \
  artwork.jpg --inspect -k 4
```

Output:
```text
==================================================
       KNOCKOUT & QUANTIZE INSPECTION REPORT      
==================================================
Image:             /path/to/artwork.jpg (764x1024)
Background Color:  #A80B20 RGB [167.92, 10.64, 31.69]
Noise Floor sigma: 1.166 (tau_low = 6.0)

Discovered / Configured Palette (4 Inks):
  Ink 1: #F5E0C9 [245, 224, 201] (dist from BG: 283.1, approx core: 37,928 px)
  Ink 2: #E09622 [224, 150, 34] (dist from BG: 150.2, approx core: 82,163 px)
  ...
Collinear Intermediate Pairs (Rim Suppression Active):
  Ink 2 intermediate to Ink 1 [cos=0.986, ratio=0.548]
==================================================
```

### Workflow B: Production Quantization with Explicit Palette
When the target discrete screen-print ink hex codes are known or curated:

```bash
uv run /home/bryant/fast/code/pixelpress/skills/knockout-quantize/scripts/knockout_quantize.py \
  input.jpg \
  -o output_transparent.png \
  -p preview_check.png \
  --palette "#F5E0C9,#CC8F68,#E09622,#97572A,#5E2615,#F3CF74"
```

### Workflow C: Handling Faint Knockout Features (e.g. Castle/Sky Silhouettes)
If an illustration contains subtle low-contrast background features (such as faint sky silhouettes, shadow shapes, or background knockout textures) that should also be removed, raise `--tau-low` to the silhouette threshold:

```bash
uv run /home/bryant/fast/code/pixelpress/skills/knockout-quantize/scripts/knockout_quantize.py \
  gothic_window.jpg \
  -o window_transparent.png \
  -p window_preview.png \
  --palette "#F1E1C7,#DEA13A,#CAAF96,#F6D490" \
  --tau-low 50.0
```

---

## 4. Verification & Quality Assurance

Every execution produces a 4-panel verification composite (`-p`) that MUST be inspected directly using `view_file`:
1. **Panel 1 (Top-Left / Far-Left)**: Original raster input.
2. **Panel 2**: Transparent PNG composited over a $20 \times 20$ high-contrast checkerboard (`#F5F5F5` / `#C3C3C3`).
3. **Panel 3**: Transparent PNG composited over Light Parchment (`#F7F4EB`).
4. **Panel 4**: Transparent PNG composited over Dark Charcoal (`#181818`).

### Sanity Checks
- **Strict Palette Purity**: Verify that `unique_palette_colors == K`. Every non-zero alpha pixel must match one of the $K$ discrete palette colors.
- **No Edge Halos**: Inspect Panel 4 (Dark Charcoal) around bright text. The edges must transition smoothly to transparency without intermediate ink halos (e.g. no Tan halos around Cream letters).
- **No Straight Line Cuts**: Ensure strokes, branches, and ornaments are natural and continuous. Never use artificial rectangular spatial bounding boxes.
