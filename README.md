# pixelpress

Production-grade graphic and illustration tools for Antigravity (AGY), featuring knockout background removal, discrete screen-print color quantization, and directional alpha unmixing.

## Skills

- **`knockout-quantize`**: Decomposes raster illustrations (JPEG, PNG, WebP) into production-grade transparent PNGs featuring strictly discrete screen-print ink palettes with smooth, halo-free anti-aliased alpha. Solves the collinear anti-aliased edge trap using zero-bounding-box background-centered ray unmixing and collinear rim suppression.

## CLI Usage

```bash
uv run skills/knockout-quantize/scripts/knockout_quantize.py --help
```

See [skills/knockout-quantize/SKILL.md](skills/knockout-quantize/SKILL.md) for full documentation.
