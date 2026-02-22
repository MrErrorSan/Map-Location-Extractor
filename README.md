# Map Scraper — Google Maps boundary & place scraper

Python web automation project to define a city boundary, split it into a grid, and scrape Google Maps (e.g. for "clinic") per cell. Output is a merged, deduplicated JSON of places.

## Overview

1. **Boundary** — Define the city as a polygon (e.g. `lahore.json`). You can create or replace it with the **city boundary extractor**.
2. **Grid** — Build a grid of cells inside the boundary (`grid_builder.py` → `grid.json`). Cell size is configurable (e.g. 500 m or 2000 m).
3. **Scrape** — For each cell, open Google Maps, search for a query (e.g. "clinic"), scroll results, and save places to `output/raw/<cell_id>.json`. Resumable; supports multiple workers.
4. **Merge** — Merge all raw JSONs, deduplicate by place ID or name+address, write e.g. `output/lahore_clinics.json`.

## Requirements

- Python 3.10+
- Playwright (Chromium) for browser automation

```bash
pip install -r requirements.txt
playwright install chromium
```

## Config

Edit `config.json` for:

| Key | Description |
|-----|-------------|
| `boundary_name` | City name (used by extractor and grid) |
| `boundary_file` | Boundary polygon JSON (e.g. `lahore.json`) |
| `grid_file` | Output path for the grid (e.g. `grid.json`) |
| `cell_size_m` | Grid cell size in metres (e.g. 500 or 2000) |
| `search_query` | Google Maps search term (e.g. `"clinic"`) |
| `output_dir`, `raw_dir` | Where merged output and raw cell JSONs go |
| `num_workers` | Parallel scraper workers (each has its own browser) |

Scraper and boundary extractor have more options (delays, viewport, etc.) in the same file.

---

## 1. City boundary extractor (optional)

Use this when you want to **create or replace** the city boundary polygon (e.g. for a new city).

- Opens Google Maps and searches for the city (from `boundary_name` or `--city`).
- **You click on the map** to add boundary points; each click shows a point and the lat/long box at the bottom (same as in Google Maps).
- The script records each new coordinate from that box.
- **When you close the browser window**, it builds a polygon from all points in click order and saves it (e.g. `lahore.json`). That file is the same format `grid_builder.py` expects.

**Run:**

```bash
python city_boundary_extractor.py
```

Optional: `--city "City Name"`, `--output path/to/boundary.json`, `--width`, `--height`.

You need at least **3 points** for a valid polygon. Then run `grid_builder.py` (or the full pipeline) to build the grid from the new boundary.

---

## 2. Build grid from boundary

Builds `grid.json` from the boundary polygon: divides the bounding box into cells of size `cell_size_m` and keeps only cells whose center lies inside the polygon (point-in-polygon).

```bash
python grid_builder.py
```

Uses `config.json`: `boundary_file`, `cell_size_m`, `boundary_name`, `grid_file`.

---

## 3. Scrape Google Maps per cell

For each cell in `grid.json`, opens Maps, runs the search (e.g. "clinic"), pans to trigger "Search this area", scrolls the results list, and saves places to `output/raw/<cell_id>.json`. Skips cells that already have a file (resumable).

```bash
python scraper.py
```

Or run only this step via the pipeline:

```bash
python run.py --scrape-only
```

Uses `config.json` for search query, paths, delays, number of workers, etc.

---

## 4. Merge and deduplicate

Merges all `output/raw/*.json` (excluding `_*` files), deduplicates places by place ID or name+address, and writes e.g. `output/lahore_clinics.json`.

```bash
python merge_dedup.py
```

Or:

```bash
python run.py --merge-only
```

---

## Full pipeline

Run all steps in order: grid → scrape → merge.

```bash
python run.py
```

Run only specific steps:

- `python run.py --grid-only` — build `grid.json` from `lahore.json` (or current `boundary_file`)
- `python run.py --scrape-only` — scrape only (requires existing `grid.json`)
- `python run.py --merge-only` — merge only (requires existing `output/raw/*.json`)

---

## File layout

| File / folder | Purpose |
|---------------|---------|
| `config.json` | Boundary name/file, grid size, search query, paths, scraper and extractor options |
| `lahore.json` | Boundary polygon: `[[lat, lon], ...]` (create with city_boundary_extractor or by hand) |
| `grid.json` | Grid cells (id, center_lat, center_lon) — produced by `grid_builder.py` |
| `city_boundary_extractor.py` | Interactive boundary capture from Google Maps (click on map, close browser to save polygon) |
| `grid_builder.py` | Build grid from boundary polygon |
| `scraper.py` | Scrape Google Maps per grid cell; saves to `output/raw/` |
| `merge_dedup.py` | Merge raw JSONs and write final places JSON |
| `run.py` | Run full pipeline or a single step |
| `output/raw/` | One JSON per grid cell (resumable scrape) |
| `output/lahore_clinics.json` | Final merged, deduplicated list of places |

---

## Notes

- **Google Maps** may change its DOM; if the scraper or boundary extractor stops finding elements, selectors (e.g. `div[role="feed"]`, `button[jsaction="reveal.card.latLng"]`) may need updating.
- Automated use of Google Maps may violate its Terms of Service; use for personal or educational purposes only.
