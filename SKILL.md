---
name: baidu-map-distribution
description: >
  Baidu Maps toolkit for ICBC work: (A) customer distribution maps with
  numbered markers, and (B) optimal route planning with real driving times
  + trajectory maps + Word reports. Use when user asks about: plotting
  addresses on maps, customer/branch distribution visualization, visit
  route planning, geographic analysis from Excel/CSV address lists.
---

# Baidu Map Toolkit

Two integrated workflows for location-based business operations.

## Quick Start

```bash
# Workflow A: Distribution map from address list
python3 scripts/gen_distribution_map.py --input data.xlsx --ak YOUR_KEY --output map.png

# Workflow B: Route plan from geocoded data (recommended)
python3 scripts/gen_route_plan.py --geocoded geocoded.json --ak YOUR_KEY \
    --origin "起点地址"

# Workflow B: Route plan from raw Excel (geocodes first)
python3 scripts/gen_route_plan.py --input customers.xlsx --ak YOUR_KEY \
    --origin "广州市天河区中山大道西41号" --output_dir ./routes/
```

## API Key Configuration (priority order)

1. `--ak <KEY>` CLI argument
2. `BAIDU_MAP_AK` environment variable
3. `config.json` file (in skill root, **NOT committed to git**)

**⚠️ Security**: Never hardcode API keys in source code. Use `config.example.json`
as a template — it contains a placeholder only.

---

## Workflow A: Customer Distribution Map

Generate publication-ready PNG maps with numbered markers from address lists.

### Input Formats

| Format | Requirement |
|--------|------------|
| Excel (.xlsx) | Column "地址" or "address" |
| JSON | Array of objects with `address` field |
| CSV | Header row with address column |

### Output

PNG image: Baidu base map + orange native markers + red calibrated labels + title bar + district statistics.

### Pipeline

1. **Read input** → auto-detects header rows (handles 企查查/天眼查 disclaimers)
2. **Geocode** → batch geocoding with retry + caching (`.geocode_cache.json`)
3. **Calibrate** → image differencing for precise label alignment
4. **Render** → numbered labels at exact marker positions

### Key Parameters

| Param | Default | Description |
|-------|---------|-------------|
| `--zoom` | auto | Map zoom level (auto-calculated) |
| `--width` | 1024 | Image width (max 1024!) |
| `--height` | 768 | Image height |
| `--title` | auto | Custom title text |

### Troubleshooting (Workflow A)

| Problem | Fix |
|---------|-----|
| Labels not aligned to markers | Calibration handles this automatically |
| 401 errors in batch | Retry with backoff (built-in); 401 = rate limit, not bad address |
| Empty map / all centered | Check AK validity; zoom may be wrong |
| Excel "no header" error | Script auto-skips disclaimer rows |

---

## Workflow B: Route Planning + Word Report

Plan optimal multi-round visit routes using **real Baidu driving times** (with traffic).

### Input Options

| Option | Source | Speed |
|--------|--------|-------|
| `--geocoded JSON` | Output from Workflow A | ⚡ Fast (skips geocoding) |
| `--input xlsx/json/csv` | Raw address list | Slower (geocodes first) |

### Algorithm

1. **Merge nearby points** (≤300m default → cluster centroids)
2. **Smart bin-packing** → groups into ~3-hour rounds (greedy nearest-neighbor)
3. **TSP optimization** → reorder each round for minimum travel
4. **Baidu Direction API v2** → get real driving distance/time per leg
5. **Validate & split** → if any round exceeds limit, split in half
6. **Trajectory maps** → render route path with arrows on map tiles
7. **Word report** → embedded images + time tables

### Output Files

| File | Description |
|------|-------------|
| `route_round_1.png` ... | Trajectory map per round (red arrows + numbers) |
| `route_plan_report.docx` | Full report with tables + embedded maps |

### Key Parameters

| Param | Default | Description |
|-------|---------|-------------|
| `--origin` | required | Starting address (will be geocoded) |
| `--stop-min` | 5 | Minutes per stop/visit |
| `--max-round-min` | 180 | Max minutes per round (~3h) |
| `--merge-m` | 300 | Merge distance in meters |
| `--output-dir` | ./ | Output directory |
| `--no-word` | off | Skip Word report |

### Coordinate Order Warning

⚠️ **Baidu uses different coordinate orders for different APIs!**

| API | Coordinate Order | Example |
|-----|-----------------|---------|
| Geocoding (`/geocoding/v3/`) | `(lng, lat)` | `113.35,23.14` ✅ |
| **Direction (`/direction/v2/driving`)** | **`(lat, lng)`** | `23.14,113.35` ✅ |
| Static Map (`/staticimage/v2/`) | `(lng, lat)` | `113.35,23.14` ✅ |

Getting this wrong causes silent `status=2` errors!

---

## Shared Utilities (`scripts/_shared_utils.py`)

Both workflows use these common modules:

| Module | Function |
|--------|----------|
| `load_ak()` | Secure AK loading (CLI > env > config) |
| `batch_geocode()` | Batch geocoding with cache + retry |
| `api_direction()` | Direction API call (lat,lng order!) |
| `calibrate_pixel_scale()` | Image differencing calibration |
| `haversine()`, `merge_nearby()` | Coordinate math |
| `read_input_data()` | Multi-format input reader |

---

## Dependencies

```
Pillow, numpy, scipy, openpyxl, requests, python-docx (Workflow B only)
```

Install:
```bash
pip install Pillow numpy scipy openpyxl requests python-docx
```

---

## Project Structure

```
baidu-map-distribution/
├── SKILL.md                          # This file
├── .gitignore                        # Excludes caches, secrets, outputs
├── config.example.json               # Template config (placeholder AK)
├── config.json                       # Your actual config (git-ignored!)
│
├── scripts/
│   ├── _shared_utils.py              # Shared: API calls, calibration, math
│   ├── gen_distribution_map.py       # Workflow A: Distribution map
│   └── gen_route_plan.py             # Workflow B: Route plan + Word
│
└── references/
    └── baidu-api.md                  # Baidu API documentation
```

## License

MIT License — feel free to fork, modify, and redistribute.
