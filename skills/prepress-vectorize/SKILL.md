---
name: prepress-vectorize
description: Production-grade vector prepress, spot-color unmixing, cutout SVG vectorization, and continuous alpha unmixing (DTG/DTF) for illustrations and apparel graphics.
---

# Prepress Vectorization, Spot-Color Unmixing & Cutout SVG Extraction

`prepress-vectorize` is a mathematically rigorous CLI tool and workflow for decomposing graphic artwork (JPEG, PNG, WebP) into production-grade transparent PNGs and scalable cut-ready SVGs with pristine, fringe-free edges. Its primary, default workflow is **`--mode vector`** (Candidate B: Strict Spot-Color Ray Unmixing & `(K+1)`-Palette Quantization -> Cutout Spline VTracer SVG) designed specifically for vector-style artwork, graphic illustrations, anime, cel-shaded art, logos, and typography, alongside continuous raster unmixing modes (`dtg`, `dtf`, `screenprint`).

---

## Architecture & Mathematical Stages

The pipeline provides two distinct processing workflows depending on target printing technology:

```
[Graphic Artwork] ──> MODE: VECTOR (Primary Default)
                           │
                           ├──> Step 1: Real-ESRGAN 2x Upscaling/Denoising
                           ├──> Step 2: Continuous 2D Background Field B(x, y) Fitting
                           ├──> Step 3: Spot-Color Palette Determination (Explicit --palette or Auto-Discovery in CIELAB)
                           ├──> Step 4: Strict Spot-Color Ray Unmixing & 9x9 Local Core Support (scipy.ndimage.maximum_filter)
                           ├──> Step 5: Strictly Flat (K+1)-Color Raster Generation (Exact C_k or B)
                           ├──> Step 6: Cutout Spline VTracer Vectorization (--hierarchical cutout --mode spline --filter_speckle 16)
                           └──> Step 7: Background Path Removal & Canonical Hex Snapping (<= K Spot Inks) + resvg 300 DPI Render

[Raster Artwork]  ──> MODES: DTG / DTF / SCREENPRINT
                           │
                           ├──> Stage 1: CIELAB Chroma Smoothing
                           ├──> Stage 2: Spatially Varying Background Field B(x, y)
                           ├──> Stage 3: Topological Trimap (True bg_core + ROI Overrides)
                           ├──> Stage 4: Closed-Form Matting + Known-B Color-Line Unmixing
                           └──> Stage 5: Decoupled Print Mode Post-Processing (DTG / DTF / Screenprint)

[Both Pipelines]  ──> Stage 6: Prepress Verification (2x2 Sheet & 1:1 Native Resolution Crops)
```

### Stage 1: Edge-Preserving CIELAB Chroma Smoothing
- Converts sRGB $[0, 1]$ into CIELAB.
- Applies an edge-preserving bilateral filter strictly on chroma channels $a^*$ and $b^*$ ($d=7$, $\sigma_{\text{color}}=12.0$, $\sigma_{\text{space}}=5.0$).
- Luminance $L^*$ remains 100% untouched, ensuring 1px hairlines and high-contrast edges remain razor-sharp while AI/JPEG DCT chroma blockiness is gently smoothed.
- Enabled by default in `--mode dtg` and `--mode dtf`; can be toggled via `--no-chroma-smooth`.

### Stage 2: Spatially Varying Background Field $B(x, y)$
- Samples outer margin strips (`--border-pct 0.025`, default 2.5% of canvas).
- Rejects foreground border intrusions via Median Absolute Deviation (MAD) in CIELAB space.
- Fits a 2D degree-2 polynomial surface ($1, x, y, x^2, xy, y^2$) via Iteratively Reweighted Least Squares (IRLS) with Huber loss to model non-uniform vignette and lighting gradients.

### Stage 3: Topological Two-Threshold Trimap with True Interior Protection
- Computes perceptual CIELAB $\Delta E$ between chroma-smoothed artwork and $B(x, y)$.
- **Candidate Background Seed**: $\Delta E \le t_{\text{bg}} + 3.0$ (default $t_{\text{bg}} = 6.5$).
- **8-Connected Component Analysis**:
  - **Exterior Background**: Components touching canvas perimeter are marked as exterior background.
  - **Genuine Interior Cutouts & Counters**: Interior components with $\text{area} \ge 6$ px whose **25th-percentile $\Delta E \le t_{\text{bg}}$** are preserved as true background voids (e.g. inner loops of letters like 'o', 'e', 'a', gaps between foliage needles, or filigree cutouts). Dark interior painted shadows and creases whose $\Delta E$ merely dips to $10\text{–}18$ are rejected and stay solid.
- **Declarative ROI Overrides**:
  - `--protect-interior-box x0,y0,x1,y1`: Suppresses interior cutouts inside the box while still smoothly matting outer silhouettes against exterior background (ideal for enclosed objects with background-colored decorative stripes).
  - `--protect-box x0,y0,x1,y1`: Forces definite foreground ($\text{trimap} = 1.0$) for low-contrast interior details.
  - `--protect-poly x1,y1,x2,y2,x3,y3,...`: Forces definite solid foreground ($\text{trimap} = 1.0$) inside non-rectangular polygonal regions.
  - `--seal-box x0,y0,x1,y1[:radius]`: Bridges narrow outline gaps (default radius 20px) to prevent exterior background flood leakage into subject interiors.
  - `--cutout-box x0,y0,x1,y1`: Relaxes interior cutout acceptance ($\text{area} \ge 3$ px, 25th-percentile $\Delta E \le t_{\text{bg}} + 5.0$) for tiny, heavily anti-aliased negative space holes.
- **Trimap Partitioning**:
  - **Definite Background (`0.0`)**: 1px eroded true background plus pure exterior background islands ($\Delta E \le 0.85 t_{\text{bg}}$ at least 2px from foreground).
  - **Definite Foreground (`1.0`)**: High-contrast eroded core ($\Delta E > t_{\text{fg}}$, default 40.0, 7x7 elliptical kernel radius 3px), deep topological interior ($\text{dist\_from\_potential\_bg} > 6.0$ px via geodesically connected potential background $\Delta E \le 22.0$), and 1px thin-stroke ridges ($\Delta E > t_{\text{thin}}$, default 32.0) validated against JPEG Gibbs ringing ripples via polarity-invariant local peak vector alignment ($((|L_{\text{img}} - L_{\text{bg}}| > 6.0) \lor (\Delta C > 10.0)) \land (\vec{v}_{\text{curr}} \cdot \vec{v}_{\text{dom}} > 0)$).
  - **Unknown Band (`0.5`)**: Anti-aliased boundary transitions with sub-threshold soft-foot noise suppression.

### Stage 4: Closed-Form Laplacian Matting + Two-Stage Known-$B$ Unmixing
- Solves global matting Laplacian via `pymatting.estimate_alpha_cf` on the topological trimap.
- Derives initial continuous foreground color field $\vec{F}_{\text{init}}$ via `pymatting.estimate_foreground_ml`.
- In the unknown transition band ($\text{trimap} = 0.5$), computes exact directional vector projection onto the local color line $[\vec{B}, \vec{F}_{\text{init}}]$:
  $$\alpha_{\text{proj}} = \text{clip}\left(\frac{(\vec{I} - \vec{B}) \cdot (\vec{F}_{\text{init}} - \vec{B})}{\|\vec{F}_{\text{init}} - \vec{B}\|^2 + \epsilon},\; 0.0,\; 1.0\right)$$
- Projects out JPEG Gibbs undershoots and ringing by taking $\min(\alpha_{\text{cf}}, \alpha_{\text{proj}})$ where contrast $\|\vec{F}_{\text{init}} - \vec{B}\| > 0.06$.
- Cleans alpha floor ($\alpha < 0.02 \to 0.0$) and ceiling ($\alpha > 0.985 \to 1.0$), and prunes isolated noise specks ($\text{area} \le 8$ px, $\alpha_{\max} < 0.55$).
- Re-runs Stage-2 foreground decontamination $\vec{F}_{\text{final}} = \text{estimate\_foreground\_ml}(\vec{I}, \alpha_{\text{final}})$ and zeroes out RGB where $\alpha = 0$ for maximum PNG compression.

### Stage 5: Strictly Decoupled Print Technology Post-Processing
- **`--mode vector` (Candidate B: Strict Spot-Color Ray Unmixing & `(K+1)`-Palette Quantization -> Cutout Spline VTracer SVG, Primary Default)**:
  Decomposes artwork into discrete spot-color ink channels with exact canonical hexes and zero color fragmentation:
  1. Real-ESRGAN 2x neural denoising and upscaling.
  2. Spatially varying polynomial background field $B(x, y)$ fitting, passed as continuous 2D field into spot-color unmixing.
  3. Spot-color palette determination: explicit physical inks via `--palette "#RRGGBB,..."` or automatic K-means discovery in CIELAB space (`--inks K`, default 2..6 via silhouette scoring) sampled strictly from high-opacity interior pixels ($\text{dist\_to\_bg} \ge 40.0$ with 2px erosion) and consolidated via `--merge-hue-tol`.
  4. Generalized topological true-background flood-fill (`segment_topological_foreground`): partitions canvas into exterior background and genuine negative-space cutouts ($\text{area} \ge 24$, inscribed radius $\ge 1.5$ px, 25th-percentile $\Delta E \le 20.0$ with palette-relative $\Delta E_{\text{bg}} < \min \Delta E_{\text{ink}}$ dominance). Deep solid interior pixels (`edt_from_true_bg > 8.0` px at 2x / `4.0` px at 1x) evaluate direct solid CIELAB distance ($\alpha = 1.0$), while boundary pixels evaluate directional ray unmixing with $9\times 9$ local core support dilation (`scipy.ndimage.maximum_filter`, 4px radius at 2x resolution). Post-alpha topological pinhole seal closes any uninvited interior holes.
  5. Pre-VTracer raster speckle consolidation (`consolidate_raster_speckles`): absorbs small multi-ink transition slivers ($<\text{filter\_speckle}$) into modal adjacent foreground inks before VTracer vectorization, eliminating Swiss-cheese transparent pinholes while keeping freestanding isolated dots intact.
  6. Cutout spline vectorization via VTracer (`--colormode color --hierarchical cutout --mode spline --filter_speckle 4 --color_precision 8 --corner_threshold 55`).
  7. Background path removal (paths closer to $B$ than any ink) and exact canonical hex snapping on every remaining path so the output SVG contains at most $K$ exact spot inks.
  8. Internal seam-backing underlay (`inject_seam_backing_underlay`): identifies multi-ink junctions within the safe interior ($\ge 2$ px inset from all transparency), generates canonical darker-ink backing ribbons, traces them with VTracer, and prepends them as underlays in the SVG to eliminate hairline gaps at sharp Bézier boundaries.
  9. Native 1x 300 DPI transparent RGBA render via `resvg_py`.
- **`--mode dtg` (Direct-to-Garment Raster)**:
  Outputs continuous $\vec{F}_{\text{final}}$ and continuous $\alpha_{\text{final}}$ directly with ZERO palette quantization, preserving full 24-bit shading and smooth anti-aliased edges for photographic or continuous-tone artwork.
- **`--mode dtf` (Direct-to-Film Transfers)**:
  Maintains continuous foreground colors with a soft Hermite ramp on low-opacity pixels (`--dtf-alpha-floor 0.15`) to prevent hot-melt adhesive powder scatter on faint vapor while keeping edges crisp.
- **`--mode screenprint` / `--quantize` (Spot-Color Silkscreen Separation)**:
  Applies weighted K-means clustering (or maps to explicit `--palette`) on solid foreground pixels ($\alpha \ge 0.8$), snaps foreground pixels to the nearest spot ink in CIELAB space, and enforces binary/halftone-ready alpha ($\alpha \ge 0.5 \to 1.0$, else $0.0$).

### Stage 6: Prepress Verification & 1:1 Native Resolution Crops
- **`--verify-composite <path.png>` / `-p <path.png>`**:
  Renders a 2x2 prepress inspection sheet:
  1. Top-Left: Over Dark Charcoal (`#18181B`)
  2. Top-Right: Over Clean White (`#FFFFFF`)
  3. Bottom-Left: Over Warm Cream (`#F4EFE6`)
  4. Bottom-Right: Alpha Matte Mask ($[\alpha, \alpha, \alpha]$)
- **`--verify-crops-dir <dir>`**:
  Automatically scores and extracts 4 diverse, non-overlapping 1:1 native-resolution diagnostic crops:
  1. Thin Filigree & Fine Ridges
  2. Transition & Unknown-Band Perimeter
  3. Interior Cutouts & Counters
  4. Outer Silhouette Edge Contrast
  Saves each crop as a 4-column 1:1 strip (`Original | Over Dark #18181B | Over White #FFFFFF | Over Cream #F4EFE6`) with zero downscaling.

---

## Resolution & DPI Preservation Gate

In prepress and apparel screen printing, downscaling or altering image resolution destroys mesh registration and blurs fine details:

> [!IMPORTANT]
> **Resolution & DPI Preservation Gate**:
> 1. Output transparent PNG dimensions ($W \times H$) MUST strictly match source raster image dimensions ($100\%$ pixel-identical, zero unintended downscaling).
> 2. Embedded DPI metadata (`info.get('dpi')`, default 300 DPI) must be preserved in the output PNG header.
> 3. If enlargement is explicitly requested, use `--scale <factor>` for high-order Lanczos resampling.

---

## CLI Tool Reference

The CLI tool is located at `skills/prepress-vectorize/scripts/prepress_vectorize.py`. It is a self-contained PEP 723 script executable via `uv run`:

```bash
uv run /home/bryant/fast/code/pixelpress/skills/prepress-vectorize/scripts/prepress_vectorize.py [OPTIONS] <input-image>
```

### Options & Arguments

| Argument | Flag | Default | Description |
| :--- | :--- | :--- | :--- |
| `input` | *(positional)* | *(required)* | Path to input image file (JPEG, PNG, WebP). |
| `-o`, `--output` | | `<stem>_transparent.png` | Destination path for output transparent PNG. |
| `--mode` | | `vector` | `vector` (Candidate B: Spot-Color Ray Unmixing + Cutout Spline VTracer SVG), `dtg` (continuous), `dtf` (firm alpha floor), or `screenprint` (spot colors). |
| `--svg-output` | | `<stem>.svg` | Destination path for output SVG file in `--mode vector`. |
| `--esrgan-model` | | `realesrgan-x4plus-anime` | Real-ESRGAN model (`realesrgan-x4plus-anime`, `realesr-animevideov3`, or `none`). |
| `--vtracer-bin` | | Auto | Path to `vtracer` binary (auto-discovered from `PATH` or `~/.cargo/bin`). |
| `--palette` | | None | Explicit comma-separated hex list of spot colors (e.g. `'#DDA032,#CBB39B,#EFE0CE'`). |
| `--inks` | | `0` | Number of foreground spot inks ($K$) for `--mode vector` (default `0`: auto-discover 2..6 in CIELAB). |
| `--filter-speckle` | | `16` | Discard patches smaller than $X$ px in VTracer cutout splines (default: 16). |
| `--merge-hue-tol` | | `12.0` | CIELAB $\Delta E$ tolerance for merging similar hues during auto-palette discovery. |
| `--svg-bg-thresh` | | `22.0` | CIELAB $\Delta E$ threshold for deleting background-matching SVG elements. |
| `--scale` | `--upscale` | `1.0` | Optional upscale factor using Lanczos resampling. |
| `--quantize` | | `False` | Shortcut for `--mode screenprint`. |
| `-p`, `--verify-composite` | `--preview` | None | Path for 2x2 prepress verification composite sheet. |
| `--verify-crops-dir` | | None | Directory to save 1:1 native-resolution diagnostic crop strips. |
| `--crop` | | `[]` | Manual 1:1 crop specification `'x,y,w,h:label'` (repeatable). |
| `-k`, `--colors` | | `8` | Number of spot-color inks for `--mode screenprint`. |
| `--dtf-alpha-floor` | | `0.15` | Alpha floor ramp cutoff for DTF adhesive protection. |
| `--chroma-smooth` | | `True` | Enable CIELAB bilateral chroma smoothing on $a^*$ and $b^*$. |
| `--no-chroma-smooth` | | | Disable CIELAB bilateral chroma smoothing. |
| `--chroma-sigma-color` | | `12.0` | Bilateral filter $\sigma_{\text{color}}$ for chroma channels. |
| `--chroma-sigma-space` | | `5.0` | Bilateral filter $\sigma_{\text{space}}$ for chroma channels. |
| `--border-pct` | | `0.025` (2.5%) | Outer border fraction used to fit background field $B(x, y)$. |
| `--bg-color` | | Auto | Manual background hex override (e.g. `'#18181B'`). |
| `--bg-thresh` | | `6.5` | $\Delta E$ threshold for background seed and interior cutout core validation. |
| `--fg-thresh` | | `40.0` (25.0 in vector) | $\Delta E$ threshold for high-contrast foreground core. |
| `--thin-thresh` | | `32.0` | $\Delta E$ threshold for thin 1–2px filigree ridge preservation. |
| `--unknown-band-px` | | `6.0` | Euclidean distance boundary from true background into object interior. |
| `--min-cutout-area` | | `6` | Minimum pixel area for accepting interior negative space cutouts. |
| `--protect-interior-box`| | `[]` | Suppress interior cutout holes in `x0,y0,x1,y1` while preserving outer silhouette matting (repeatable). |
| `--protect-box` | | `[]` | Force definite solid foreground (`trimap = 1.0`) in `x0,y0,x1,y1` (repeatable). |
| `--protect-poly` | | `[]` | Force definite solid foreground (`trimap = 1.0`) inside polygon `'x1,y1,x2,y2,x3,y3,...'` (repeatable). |
| `--seal-box` | | `[]` | Bridge outline gaps via morphological closing in `x0,y0,x1,y1[:radius]` (repeatable). |
| `--cutout-box` | | `[]` | Relax interior cutout threshold in `x0,y0,x1,y1` for tiny holes (repeatable). |
| `--json` | | `False` | Output summary statistics in structured JSON format. |
| `-v`, `--verbose` | | `False` | Print diagnostic processing logs. |

---

## Standard Workflows

### Workflow 1: Vector Prepress Vectorization & Cutout SVG (Primary Default)

For vector-style artwork, graphic illustrations, anime, cel-shaded art, logos, and typography:

```bash
# Auto-discover 2..6 spot inks via CIELAB silhouette scoring:
uv run /home/bryant/fast/code/pixelpress/skills/prepress-vectorize/scripts/prepress_vectorize.py \
  artwork.jpg \
  --mode vector \
  -o artwork_transparent.png \
  --svg-output artwork.svg \
  -p preview_composite.png \
  --verify-crops-dir ./crops

# Or with an explicit physical spot-color palette:
uv run /home/bryant/fast/code/pixelpress/skills/prepress-vectorize/scripts/prepress_vectorize.py \
  artwork.jpg \
  --mode vector \
  --palette "#DDA032,#CBB39B,#EFE0CE" \
  -o artwork_transparent.png \
  --svg-output artwork.svg \
  -p preview_composite.png \
  --verify-crops-dir ./crops
```

### Workflow 2: Continuous DTG Digital Printing

For digital direct-to-garment printing where artist gradients and continuous shading must be preserved without color banding:

```bash
uv run /home/bryant/fast/code/pixelpress/skills/prepress-vectorize/scripts/prepress_vectorize.py \
  artwork.jpg \
  --mode dtg \
  -o artwork_transparent.png \
  -p preview_composite.png \
  --verify-crops-dir ./crops
```

### Workflow 3: Direct-to-Film (DTF) Transfers

For DTF heat transfers requiring a steeper alpha dropoff to prevent hot-melt adhesive powder from clinging to faint edge mist:

```bash
uv run /home/bryant/fast/code/pixelpress/skills/prepress-vectorize/scripts/prepress_vectorize.py \
  artwork.jpg \
  --mode dtf \
  --dtf-alpha-floor 0.15 \
  -o artwork_dtf.png \
  -p preview_dtf.png \
  --verify-crops-dir ./crops
```

### Workflow 4: Silkscreen Spot-Color Separation (Screen Printing)

When separating illustration artwork into discrete spot-color ink channels with binary mesh stencil boundaries:

```bash
uv run /home/bryant/fast/code/pixelpress/skills/prepress-vectorize/scripts/prepress_vectorize.py \
  artwork.jpg \
  --mode screenprint \
  --colors 6 \
  -o artwork_screenprint.png \
  -p preview_screenprint.png \
  --verify-crops-dir ./crops
```

Or with an explicit physical spot-color palette:

```bash
uv run /home/bryant/fast/code/pixelpress/skills/prepress-vectorize/scripts/prepress_vectorize.py \
  artwork.jpg \
  --mode screenprint \
  --palette "#552308,#D08915,#F3D5BA,#824B21,#BE7A4F,#EDBB5F" \
  -o artwork_screenprint.png \
  -p preview_screenprint.png
```

---

## Iterative ROI Refinement Workflow

When inspecting 1:1 detail crops or 2x2 verification sheets, apply targeted ROI overrides to resolve edge cases without altering global thresholds:

### Case A: Enclosed Object with Background-Colored Decorative Stripes
- **Symptom**: Decorative stripes inside an enclosed graphic (e.g. ribbon or banner stripes matching canvas background color) get hollowed out into negative space cutouts.
- **Remedy**: Apply `--protect-interior-box 400,500,800,900`.
- **Result**: Interior cutout holes inside the bounding box are suppressed, keeping the stripes 100% solid while outer boundaries continue to cleanly matte against the exterior background.

### Case B: Tiny, Heavily Anti-Aliased Interior Cutouts
- **Symptom**: Very small interior loops in typography or tight gaps between thin lines fail to knock out because edge anti-aliasing prevents their cores from dipping below the strict $t_{\text{bg}} = 6.5$ threshold.
- **Remedy**: Apply `--cutout-box 400,500,800,900`.
- **Result**: Core acceptance is relaxed inside the bounding box ($\Delta E \le t_{\text{bg}} + 5.0$, $\text{area} \ge 3$ px), cleanly opening the tight negative space.

### Case C: Deep Interior Shadow Punch-Through
- **Symptom**: A dark painted shadow or artistic fold in the interior of a foreground subject becomes translucent.
- **Remedy**: Apply `--protect-box 400,500,800,900`.
- **Result**: Forces definite foreground ($\text{trimap} = 1.0$) within the box, guaranteeing 100% solid opacity ($\alpha = 1.0$).

### Case D: Broken Outline / Open Silhouette Gap Leakage
- **Symptom**: Exterior background leaks through narrow gaps between foreground branches, tiers, or broken frame lines into the interior.
- **Remedy**: Apply `--seal-box 400,500,800,900:20`.
- **Result**: Bridges narrow outline gaps via morphological closing during flood-fill, preserving outer silhouette matting while preventing exterior background from flooding the interior.

### Case E: Non-Rectangular Solid Interior Protection
- **Symptom**: A triangular, arched, or polygonal interior motif requires 100% solid opacity without boxing out surrounding exterior background.
- **Remedy**: Apply `--protect-poly 400,500,450,420,500,500`.
- **Result**: Forces definite foreground ($\text{trimap} = 1.0$) strictly within the polygon vertices.
