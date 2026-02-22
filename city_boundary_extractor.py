"""
City boundary extractor for Google Maps.

Flow:
1. Opens Google Maps and searches for the city.
2. You click on the map — each click shows a point and the lat/long box at the bottom
   (same as in Google Maps / city_boundary_map.html).
3. The script records each new coordinate from that box.
4. When you close the browser window, the script builds a polygon from all points
   and saves the boundary JSON (same format as lahore.json for grid_builder).

Run: python city_boundary_extractor.py
Uses config.json (boundary_name, boundary_file). Optional: --city "Name" --output path
"""

import argparse
import json
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

# Selectors for the coordinates button at the bottom (aria-label is stable)
COORD_BUTTON_SELECTORS = [
    'button[jsaction="reveal.card.latLng"]',
    'button[aria-label*=","][aria-label*="."]',
    'button.ZqLNQd[aria-label*=","]',
]
LATLON_PATTERN = re.compile(r"(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)")


def load_config():
    config_path = Path(__file__).parent / "config.json"
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def get_coord_from_page(page, timeout_ms: int = 1500) -> tuple[float, float] | None:
    """Read (lat, lon) from the coordinates button at the bottom of the map."""
    for sel in COORD_BUTTON_SELECTORS:
        try:
            btn = page.locator(sel).first
            if btn.count() == 0:
                continue
            btn.wait_for(state="visible", timeout=timeout_ms)
            aria = btn.get_attribute("aria-label") or ""
            if not aria:
                aria = (btn.inner_text() or "").strip()
            m = LATLON_PATTERN.search(aria)
            if m:
                return float(m.group(1)), float(m.group(2))
        except Exception:
            continue
    return None


def _dismiss_consent_if_present(page, timeout_ms: int = 3000) -> None:
    """Click common consent/cookie buttons so they don't block the search box."""
    buttons = [
        'button[aria-label*="Accept"]',
        'button:has-text("Accept all")',
        'button:has-text("I agree")',
        '[aria-label*="Accept all"]',
    ]
    for sel in buttons:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=timeout_ms)
                time.sleep(0.8)
                return
        except Exception:
            continue


def extract_boundary(
    city_name: str,
    output_path: Path,
    viewport_width: int = 1920,
    viewport_height: int = 1080,
) -> list[list[float]]:
    """
    Open Maps, search for city. User clicks on the map; we record coords from the
    bottom bar. When the browser is closed, return collected points as a polygon.
    """
    points: list[tuple[float, float]] = []
    last_key: tuple[float, float] | None = None
    warmup_until = time.time() + 3  # don't add the initial map center as first point

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": viewport_width, "height": viewport_height},
            locale="en-US",
        )
        page = context.new_page()
        page.goto("https://www.google.com/maps", wait_until="domcontentloaded", timeout=30000)
        time.sleep(1.8)
        _dismiss_consent_if_present(page)

        # Search for the city
        search_sel = 'input[aria-label="Search Google Maps"], input#searchboxinput, input[name="q"]'
        try:
            search = page.locator(search_sel).first
            search.wait_for(state="visible", timeout=12000)
            search.fill(city_name)
            time.sleep(0.5)
            search.press("Enter")
        except Exception as e:
            print(f"Search failed: {e}")
            browser.close()
            return []

        time.sleep(4)

        print()
        print("Click on the map to add boundary points (the lat/long box at the bottom updates).")
        print("Close the browser window when you are done.")
        print()

        # Poll for coord changes (user clicks update the bottom bar)
        poll_interval = 0.7
        while True:
            time.sleep(poll_interval)
            try:
                coord = get_coord_from_page(page)
            except Exception:
                # Page closed or detached
                break
            if not coord:
                continue
            key = (round(coord[0], 5), round(coord[1], 5))
            # After warmup, add when coord changes (new click)
            if time.time() < warmup_until:
                last_key = key
                continue
            if key != last_key:
                last_key = key
                points.append(coord)
                print(f"  Point {len(points)}: {coord[0]:.6f}, {coord[1]:.6f}")

        # Browser was closed; don't call browser.close()

    # Polygon = points in the order they were clicked (same format as grid_builder expects)
    return [[round(p[0], 6), round(p[1], 6)] for p in points]


def main():
    parser = argparse.ArgumentParser(
        description="Extract city boundary: open map, you click points, close browser to save polygon."
    )
    parser.add_argument("--city", type=str, help="City name to search (default: from config boundary_name)")
    parser.add_argument("--output", type=str, help="Output JSON path (default: config boundary_file)")
    parser.add_argument("--width", type=int, help="Viewport width (default from config or 1920)")
    parser.add_argument("--height", type=int, help="Viewport height (default from config or 1080)")
    args = parser.parse_args()

    cfg = load_config()
    base = Path(__file__).parent
    city_name = args.city or cfg.get("boundary_name", "Lahore")
    output_path = base / (args.output or cfg.get("boundary_file", "lahore.json"))
    width = args.width if args.width is not None else cfg.get("boundary_viewport_width", 1920)
    height = args.height if args.height is not None else cfg.get("boundary_viewport_height", 1080)

    print(f"City: {city_name}")
    print(f"Output: {output_path}")

    points = extract_boundary(
        city_name=city_name,
        output_path=output_path,
        viewport_width=width,
        viewport_height=height,
    )

    if len(points) >= 3:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(points, f, indent=2)
        print(f"Saved polygon with {len(points)} points to {output_path}")
        print("Run python grid_builder.py to generate grid.json from this boundary.")
    elif points:
        print(f"Only {len(points)} point(s) collected; need at least 3 for a polygon. Not saved.")
    else:
        print("No points collected. Close the browser when you have finished clicking.")


if __name__ == "__main__":
    main()
