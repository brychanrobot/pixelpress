---
name: print-image-review
description: Comprehensive prepress and print quality review for knockout transparent PNGs, covering DTG, DTF, and screen-printing compliance across 4-panel verification composites.
---

# Prepress & Print Image Quality Review

`print-image-review` provides a disciplined, objective review protocol for validating transparent graphic artwork intended for apparel printing (Direct-to-Garment, Direct-to-Film, and traditional silkscreen spot-color printing).

Automated tests and command exit codes verify execution, but **visual inspection of actual generated composite output via `view_file` is mandatory** to detect color fringing, halo artifacts, broken negative space, or lost sub-pixel highlights.

---

## 4-Panel Verification Composite Layout

The review is centered around inspecting the high-definition 4-panel verification image (`-p preview.png`):

| Panel | Backdrop | Primary Inspection Purpose |
| :--- | :--- | :--- |
| **Panel 1 (Far-Left)** | Original Input Image | Ground truth comparison for geometry, artistic intent, and highlight placement. |
| **Panel 2** | High-Contrast Checkerboard (`#F5F5F5` / `#C3C3C3`) | Alpha transparency distribution, anti-aliased edge smoothness, and opacity levels. |
| **Panel 3** | Light Parchment (`#F7F4EB`) | **Dark Background Bleed & Fringe Audit**: Exposes residual dark/colored background halos (pink, green, purple, charcoal) on light typography and fine strokes. |
| **Panel 4 (Far-Right)** | Dark Charcoal (`#181818`) | **Light Background Bleed & Alpha Edge Audit**: Exposes residual light/white fringe, dusty edges, and surviving mosquito noise specs. |

---

## Core Review Audits & Pass/Fail Criteria

### 1. Halo & Edge Fringe Audit (P0 Blocker)
- **What to Inspect**: Inspect the outer boundaries of high-contrast foreground strokes (typography, flourishes, light elements) in Panel 3 (Light Parchment) and Panel 4 (Dark Charcoal).
- **Failure Mode**: A 1–2px discolored rim (e.g. pink/red, green, or muddy brown edge on cream or white letters) caused by incomplete background unmixing or collinear intermediate ink hijacking.
- **Pass Criteria**:
  - The stroke color must remain 100% uniform and true to its foreground ink all the way to its anti-aliased taper.
  - Zero halo leak pixels or residual background color contamination.

### 2. Background Speckle & Mosquito Noise Audit (P1 Risk)
- **What to Inspect**: Scan the knocked-out negative-space areas in Panel 3 and Panel 4, specifically within an $8 \times 8$ to $16 \times 16$ pixel radius of sharp high-contrast typography and flourishes.
- **Failure Mode**: Discrete floating specks, DCT compression ringing blocks, or single-pixel noise clusters that survived knockout.
- **Pass Criteria**:
  - Knocked-out areas must be completely transparent ($\alpha = 0$) with zero floating specks.
  - Background noise gate threshold ($\alpha_{\text{knee}} \ge 0.18$, $\tau_{\text{mosquito}} \ge \max(10.0, 3.5\sigma)$) successfully suppressed 100% of compression artifacts.

### 3. Interior Cutout vs. Dark Foreground Audit (P0 Blocker)
- **What to Inspect**:
  - **Negative Space Voids**: Inspect enclosed background cutouts (e.g. negative space inside window panes, garment show-through patterns, between tree branches or scarf stripes).
  - **Low-Contrast Dark Foreground**: Inspect dark foreground features whose RGB is close to the background (e.g. dark chocolate shading, dark pine trees, deep navy outlines, facial creases).
- **Failure Mode**:
  - Holes or missing geometry inside dark foreground elements (caused by over-aggressive global thresholding).
  - Incomplete knockout inside interior negative spaces (leaving background color trapped inside window frames or garment show-through cutouts).
- **Pass Criteria**:
  - All true negative spaces are 100% knocked out ($\alpha = 0$).
  - All dark foreground features remain 100% solid and opaque ($\alpha = 255$) without interior dropouts.

### 4. Sub-Pixel Detail & Internal Highlight Integrity (P1 Risk)
- **What to Inspect**:
  - Delicate 1–2px strokes (fine stars, snowflake arms, thin filigree, line shading).
  - Internal multi-tone highlight stripes (e.g. secondary light highlight bands inside letter strokes).
- **Failure Mode**:
  - Mottling, broken stair-stepping, or two-tone fracturing along thin arms.
  - Washed-out or missing internal highlight stripes.
- **Pass Criteria**:
  - Fine 1–2px features maintain uniform single-ink assignment without fracturing.
  - Internal highlight bands are sharp, continuous, and cleanly separated from primary stroke colors.

### 5. Print-Technology Compliance Audit (P1 Risk)
- **Screen Printing (`--mode screenprint`)**:
  - Verified discrete palette count matches physical separation specifications ($K \le 8$ inks).
  - No continuous gradients or floating intermediate tones.
  - Morphological despeckling has eliminated isolated islands smaller than minimum mesh tolerance ($\le 20$ px).
- **Direct-to-Film (DTF)**:
  - Alpha falloff is sufficiently steep (`--alpha-power 1.2`–`1.5`) to prevent hot-melt adhesive powder from clinging to faint low-opacity edges.
- **Direct-to-Garment (DTG, `--mode dtg`)**:
  - Interior colors retain smooth continuous transitions without quantization stair-stepping.
  - Edge boundaries remain 100% decontaminated of background color.

### 6. Resolution & DPI Preservation Gate (P0 Blocker)
- **What to Inspect**: Compare output PNG dimensions ($W \times H$) against input image dimensions.
- **Failure Mode**: Downscaling, resolution loss, or altered aspect ratio.
- **Pass Criteria**:
  - Output PNG dimensions $(W \times H)$ are **100% identical** to source image dimensions (unless explicit `--scale` was requested).
  - Embedded DPI metadata is preserved.

---

## Review Output Format & Reporting

When conducting a prepress review, format findings using the standard triage tiers:

```markdown
### Prepress Image Quality Review Report

**File Reviewed**: `path/to/artwork_transparent.png`
**Preview Composite**: `path/to/preview_composite.png`
**Target Printing Tech**: Screen Printing (Discrete 6-Color) | DTG | DTF
**Native Dimensions**: 764 x 1024 px (Preserved: YES / NO)

#### 1. Visual Audit Findings
- **Halo & Fringe**: PASS / FAIL (Notes on Light Parchment & Dark Charcoal inspection)
- **Background Knockout & Noise**: PASS / FAIL (Zero surviving mosquito noise)
- **Interior Cutouts & Dark Solids**: PASS / FAIL (Cutouts clean, dark solids intact)
- **Sub-Pixel & Highlight Integrity**: PASS / FAIL (Uniform stars, clean letter highlights)
- **Print Compliance**: PASS / FAIL (Discrete palette verified, no stray tones)

#### 2. Prioritized Issues (if any)
- **[P0] Blocker**: Issue description, affected coordinate region, recommended CLI flag adjustment.
- **[P1] Print Risk**: Detail, potential on-press impact.
- **[P2] Polish**: Minor visual enhancement.

#### 3. Verdict
**PASS — Production Ready** or **REVISE — Rerun with Adjusted Parameters**
```
