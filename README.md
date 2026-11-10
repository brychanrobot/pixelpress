# pixelpress

Production-grade graphic and illustration prepress tools for Antigravity (AGY), featuring knockout background removal, discrete spot-color screen-print quantization, and continuous color-decontaminating alpha unmixing.

---

## Skills

- **`knockout-quantize`** ([`skills/knockout-quantize/SKILL.md`](skills/knockout-quantize/SKILL.md)): Decomposes raster graphics (JPEG, PNG, WebP) into production-grade transparent PNGs. Supports both continuous DTG/DTF unmixing and discrete silkscreen spot-color separation using Solid-Core Voronoi Propagation and Pure-Ink Ray Alpha unmixing.
- **`print-image-review`** ([`skills/print-image-review/SKILL.md`](skills/print-image-review/SKILL.md)): Prepress visual and quantitative audit protocol for evaluating transparent PNGs against 4-panel verification composites across Light Parchment (`#F7F4EB`) and Dark Charcoal (`#181818`) backdrops.

---

## Subagents

- **`print-knockout-specialist`** ([`agents/print-knockout-specialist.md`](agents/print-knockout-specialist.md)): Autonomous prepress technician that determines print technology, executes `knockout_quantize.py`, generates 4-panel previews, and enforces resolution preservation.
- **`print-image-reviewer`** ([`agents/print-image-reviewer.md`](agents/print-image-reviewer.md)): Read-only prepress quality auditor that inspects 4-panel composites and 1:1 crops for edge halos, surviving mosquito noise, negative space cutouts, and sub-pixel highlight integrity.

---

## CLI Quickstart

```bash
# Silkscreen Spot-Color Quantization
uv run skills/knockout-quantize/scripts/knockout_quantize.py \
  artwork.jpg \
  --palette "#F5E0C9,#CC8F68,#E09622,#97572A,#5E2615" \
  --mode screenprint \
  -o artwork_screenprint.png \
  -p preview_composite.png

# Continuous DTG Digital Printing
uv run skills/knockout-quantize/scripts/knockout_quantize.py \
  artwork.jpg \
  --mode dtg \
  -o artwork_dtg.png \
  -p preview_dtg.png
```
