---
name: print-image-review
description: Comprehensive prepress and print quality review for knockout transparent PNGs and cutout SVGs, covering Vector, DTG, DTF, and screen-printing compliance across 2x2 verification composites and 1:1 native-resolution diagnostic crops.
---

# Prepress & Print Image Quality Review Protocol

`print-image-review` provides a disciplined, objective review protocol for validating transparent graphic artwork and cutout SVGs intended for physical apparel and digital printing (Vector Cutout Prepress, Direct-to-Garment, Direct-to-Film, and traditional silkscreen spot-color printing).

Automated script completion only verifies code execution without crashing. **Direct visual inspection of actual generated composite sheets and 1:1 native-resolution diagnostic crop strips via `view_file` is mandatory** to detect color fringing, Gibbs dark rings, broken 1–2px filigree, clogged cutouts/counters, or interior shadow punch-through.

---

## Prepress Verification Layouts

The quality review is conducted across two complementary visual artifacts:

### 1. Full-Canvas 2x2 Verification Sheet (`-p` / `--verify-composite`)

| Panel | Backdrop | Primary Inspection Purpose |
| :--- | :--- | :--- |
| **Top-Left** | Over Dark Charcoal (`#18181B`) | **Color Fringing & Halo Audit**: Exposes residual background color bleed (e.g. red, pink, green, cream halos) along light typography, fine strokes, and outer silhouettes. |
| **Top-Right** | Over Clean White (`#FFFFFF`) | **Dark Undershoot & Linework Audit**: Exposes broken dark outlines, eroded tips, and unwanted edge darkening. |
| **Bottom-Left** | Over Warm Cream (`#F4EFE6`) | **Intermediate Contrast Audit**: Exposes subtle chromatic shifts and color contamination against mid-tone backdrops. |
| **Bottom-Right** | Alpha Matte Mask ($[\alpha, \alpha, \alpha]$) | **Geometry & Cutout Audit**: Inspects interior negative space cutouts vs. solid interior opacity ($\alpha = 1.0$). |

### 2. Mandatory 1:1 Native-Resolution Detail Crop Gate (`--verify-crops-dir`)

Full-canvas composite images (often 3000–8000 px wide) are unavoidably downscaled during browser viewing and vision model tokenization. This downsampling conceals critical 1–2px defects: hairline breaks, single-pixel DCT ringing artifacts, subtle 1px halos, or eroded filigree tips.

The reviewer MUST directly inspect all 1:1 native-resolution diagnostic crop strips (`Original Input | Over Dark #18181B | Over White #FFFFFF | Over Cream #F4EFE6`) generated in `--verify-crops-dir`:
1. **Thin Filigree & Fine Ridges**: 1–2px hairlines, fine needle tips, delicate star points, isolated stippling, and fine hatching.
2. **Transition & Unknown-Band Perimeter**: Anti-aliased boundary transitions, edge smoothness, and color decontamination.
3. **Interior Cutouts & Counters**: Negative space inside typography loops (e.g. 'e', 'o', 'a'), filigree voids, and enclosed decorative stripes.
4. **Outer Silhouette Edge Contrast**: High-contrast silhouette boundaries against dark and light garments.

---

## Core Review Checkpoints & Pass/Fail Criteria

### 1. Halo & Color Bleed Audit (P0 Blocker)
- **What to Inspect**: Inspect outer boundaries of foreground strokes in Panel 1 (Over Dark `#18181B`) and Panel 3 (Over Warm Cream `#F4EFE6`).
- **Failure Mode**: A 1–2px tinted rim (e.g. reddish, greenish, or muddy halo) around light strokes caused by incomplete background unmixing.
- **Pass Criteria**:
  - Stroke color remains 100% pure and true to the foreground subject all the way to its anti-aliased edge.
  - Zero residual background bleed.

### 2. JPEG Gibbs Ringing & Dark Rim Audit (P1 Risk)
- **What to Inspect**: Inspect boundaries of bright, high-contrast foreground elements over white and cream backdrops.
- **Failure Mode**: Dark undershoot rims or dirty rings around bright strokes caused by DCT compression ringing.
- **Pass Criteria**:
  - Gibbs ringing undershoots are properly suppressed by Known-$B$ vector color-line projection; no dark halos encircle bright typography or highlights.

### 3. Interior Cutout vs. Dark Foreground Audit (P0 Blocker)
- **What to Inspect**:
  - **Negative Space Cutouts**: Inspect interior enclosed background spaces (e.g. loops in letters, gaps in decorative filigree, negative space between branches or stripes).
  - **Low-Contrast Dark Foreground**: Inspect dark interior painted shadows, creases, or outlines whose color is close to the original background.
- **Failure Mode**:
  - Dark interior creases turn translucent or punch through with holes.
  - Interior negative space loops remain clogged with original background color.
- **Pass Criteria**:
  - All genuine negative spaces are 100% transparent ($\alpha = 0$).
  - All dark interior foreground features remain 100% solid and opaque ($\alpha = 1.0$).

### 4. Thin 1–2px Filigree & Hairline Integrity (P1 Risk)
- **What to Inspect**:
  - Delicate 1–2px strokes, fine needle foliage, hanging threads, small stars, and isolated stippling dots in Crop #1.
- **Failure Mode**:
  - Broken stair-stepping, fragmented lines, or lost sub-pixel highlights.
- **Pass Criteria**:
  - 1–2px strokes are continuous, unbroken, and cleanly matted.

### 5. Enclosed Background-Colored Decorative Elements (P1 Risk)
- **What to Inspect**:
  - Enclosed objects featuring decorative patterns or stripes that match the canvas background color (e.g. striped banners, ribbons, or multi-tone garments).
- **Failure Mode**:
  - Decorative stripes inside the object get hollowed out into transparent cutouts when artistic intent was for them to remain solid opaque graphic elements.
- **Remediation**:
  - Advise applying `--protect-interior-box x0,y0,x1,y1` to keep interior stripes 100% solid while maintaining outer silhouette matting.

### 6. Print-Technology Compliance Audit (P1 Risk)
- **Vector Prepress (`--mode vector`, Primary Default)**:
  - Scalable Cutout SVG verified: native dimensions `width="{w_orig}"`, `height="{h_orig}"`, `viewBox="0 0 {w_2x} {h_2x}"`, full-canvas background rects deleted, exterior border paths removed, genuine interior cutouts cleared, and protected motifs (`--protect-interior-box`, `--protect-poly`) retained.
  - Rendered 300 DPI RGBA PNG verified: sharp anti-aliased edges, zero halo bleed, and uniform flat fills.
- **Direct-to-Garment (`--mode dtg`)**:
  - Continuous 24-bit shading preserved without posterization or artificial quantization banding.
- **Direct-to-Film (`--mode dtf`)**:
  - Alpha falloff ramp (`--dtf-alpha-floor 0.15`) verified to prevent adhesive powder scatter on faint vapor.
- **Screen Printing (`--mode screenprint`)**:
  - Verified discrete palette count ($K \le 8$ inks).
  - Zero continuous gradients or floating intermediate tones.
  - Halftone/binary alpha verified ($\alpha \in \{0, 255\}$).

### 7. Resolution & DPI Preservation Gate (P0 Blocker)
- **Pass Criteria**:
  - Output PNG dimensions $(W \times H)$ are **100% identical** to source image dimensions.
  - DPI metadata (300 DPI) is preserved.

---

## Actionable Remediation Feedback Guide

When defects are observed, provide exact CLI parameter adjustments:

| Observed Defect | Root Cause | Actionable Remediation |
| :--- | :--- | :--- |
| Enclosed decorative stripes hollowed out | Interior component core matches background | Add `--protect-interior-box 400,500,800,900` covering the element |
| Tiny interior counter/loop not knocked out | Heavy anti-aliasing prevented $\Delta E \le 6.5$ | Add `--cutout-box 400,500,800,900` covering the loop |
| Dark interior painted shadow became translucent | Shadow contrast too low against background | Add `--protect-box 400,500,800,900` covering the shadow |
| Non-rectangular interior solid hollowed out | Irregular geometry with background-matching tone | Add `--protect-poly 400,500,450,420,500,500` covering the polygon |
| Background leaks through narrow outline gap | Small gap between branches, tiers, or frame | Add `--seal-box 400,500,800,900:20` to close the gap |
| Light halo surviving on outer silhouette | Background threshold slightly too low | Increase `--bg-thresh` from `6.5` to `7.5` or `8.0` |
| Delicate 1px hairlines thinned or broken | Ridge threshold too high for low-contrast strokes | Decrease `--thin-thresh` from `32.0` to `20.0` or `24.0` |
| DTF adhesive powder scatter on faint edges | Low-opacity edge vapor | Increase `--dtf-alpha-floor` to `0.20` |

---

## Standard Review Output Format

Conclude every prepress image review with the standard triage report:

```markdown
### Prepress Image Quality Review Report

**File Reviewed**: `path/to/artwork_transparent.png`
**2x2 Verification Sheet**: `path/to/verify_composite.png`
**1:1 Diagnostic Crops**: `path/to/crops_dir/`
**Target Printing Tech**: Vector Prepress (Cutout SVG + 300 DPI PNG) | DTG (Continuous) | DTF | Screen Printing (Discrete K-Color)
**Native Dimensions**: [W] x [H] px (Preserved: YES / NO)
**1:1 Detail Crops Inspected**: YES / NO

#### 1. Visual Audit Findings
- **Halo & Color Bleed**: PASS / FAIL (Notes on Dark Charcoal `#18181B` & Warm Cream `#F4EFE6`)
- **Gibbs Ringing & Dark Rims**: PASS / FAIL (Notes on Clean White `#FFFFFF`)
- **Interior Cutouts & Dark Solids**: PASS / FAIL (Negative spaces clean, dark solids intact)
- **1–2px Filigree & Hairlines**: PASS / FAIL (Continuous, unbroken hairlines in Crop #1)
- **Enclosed Decorative Elements**: PASS / FAIL (Interior stripes intact or remediation noted)
- **Print Compliance**: PASS / FAIL (Continuous shading / discrete palette verified)
- **Resolution Preservation**: PASS / FAIL (Dimensions & DPI match source)

#### 2. Prioritized Issues & Remediation (if any)
- **[P0] Blocker**: Issue description, coordinate bounding box `[x0:x1, y0:y1]`, and exact CLI flag adjustment (e.g. `--protect-interior-box x0,y0,x1,y1`).
- **[P1] Print Risk**: Detail, potential on-press impact, recommended parameter tweak.
- **[P2] Polish**: Minor visual enhancement.

#### 3. Verdict
**PASS — Production Ready** | **REVISE — Parameters Adjustment Required**
```
