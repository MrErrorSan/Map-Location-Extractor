"""
Scrape Google Maps for 'clinic' (or config search_query) per grid cell.
Saves one JSON per cell under output/raw/<cell_id>.json for resumability.
Supports parallel workers (num_workers > 1), each with its own browser and cell slice.
Uses Playwright; run: playwright install chromium
"""
import json
import logging
import multiprocessing
import random
import re
import signal
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# Graceful shutdown: set to True on SIGINT so main loop can exit cleanly
_stop_flag = False

def _signal_handler(sig, frame):
    global _stop_flag
    _stop_flag = True
    log.info("Shutdown requested. Finishing current cell then exiting.")

try:
    signal.signal(signal.SIGINT, _signal_handler)
except (ValueError, OSError):
    pass  # Not in main thread or unsupported

# Console logging for debugging
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
)
log = logging.getLogger(__name__)


def load_config():
    config_path = Path(__file__).parent / "config.json"
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def load_grid(grid_path: Path) -> list[dict]:
    with open(grid_path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("cells", data) if isinstance(data, dict) else data


def extract_place_id_from_url(url: str) -> str | None:
    """Extract place ID or stable part of place URL for deduplication."""
    if not url:
        return None
    # .../place/Name/data/!4m2!3m1!1s0x... or /place/.../@lat,lon
    m = re.search(r"!1s(0x[0-9a-f]+(?::[0-9a-f]+)?)", url)
    if m:
        return m.group(1)
    m = re.search(r"/place/[^/]+/([^/]+)", url)
    if m:
        return m.group(1)
    return url


def extract_lat_lon(url: str) -> tuple[float | None, float | None]:
    """Extract lat, lon from Maps place URL (@lat,lon)."""
    if not url:
        return None, None
    m = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", url)
    if m:
        try:
            return float(m.group(1)), float(m.group(2))
        except ValueError:
            pass
    return None, None


def detect_block(page) -> bool:
    """Detect CAPTCHA / consent / unusual traffic blocking."""
    try:
        url = page.url.lower()
        if "sorry" in url or "consent" in url or "blocked" in url:
            return True
        content = page.content()
        if content:
            c = content.lower()
            if "unusual traffic" in c or "captcha" in c or "not a robot" in c:
                return True
    except Exception:
        pass
    return False


def detect_network_failure(page) -> bool:
    """Detect no internet / connection timed out / network error."""
    try:
        content = (page.content() or "").lower()
        if "no internet" in content or "connection timed out" in content or "network error" in content:
            return True
    except Exception:
        pass
    return False


def humanize_page(page) -> None:
    """Human-like interaction simulation to reduce automation detection. Uses viewport-relative coordinates."""
    try:
        vp = page.viewport_size
        if vp and vp.get("width") and vp.get("height"):
            x = random.randint(50, max(100, vp["width"] - 50))
            y = random.randint(50, max(100, vp["height"] - 50))
        else:
            x, y = random.randint(200, 800), random.randint(200, 600)
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.2, 0.6))
        page.mouse.wheel(random.randint(-200, 200), random.randint(-200, 200))
        if random.random() < 0.3:
            if vp and vp.get("width") and vp.get("height"):
                page.mouse.move(random.randint(50, vp["width"] - 50), random.randint(50, vp["height"] - 50))
            else:
                page.mouse.move(random.randint(200, 800), random.randint(200, 600))
    except Exception:
        pass


def fetch_place_details_from_panel(page, cid: str) -> dict:
    """
    Extract address, phone, website, hours from the open place detail panel.
    Call after clicking a place link; returns dict with keys address, phone, website, hours.
    """
    out = {"address": "", "phone": "", "website": "", "hours": ""}
    try:
        page.wait_for_selector("h1", timeout=5000)
    except Exception:
        return out
    try:
        # data-item-id is used by Maps for detail section items
        for key, data_id in [("address", "address"), ("phone", "phone"), ("website", "authority"), ("hours", "oh")]:
            try:
                loc = page.locator(f'[data-item-id="{data_id}"]')
                if loc.count() > 0:
                    out[key] = (loc.first.inner_text() or "").strip()
            except Exception:
                pass
        # Fallback: common class patterns if data-item-id missing
        if not out["address"]:
            for sel in ['[data-tooltip="Copy address"]', 'button[aria-label*="Address"]', 'span[class*="address"]']:
                try:
                    el = page.locator(sel).first
                    if el.count() > 0:
                        out["address"] = (el.inner_text() or "").strip()[:500]
                        break
                except Exception:
                    pass
        if not out["phone"]:
            try:
                tel = page.locator('a[href^="tel:"]').first
                if tel.count() > 0:
                    out["phone"] = (tel.inner_text() or "").strip()
            except Exception:
                pass
        if not out["website"]:
            try:
                link = page.locator('a[data-item-id="authority"]').first
                if link.count() == 0:
                    link = page.locator('a[href^="http"]').filter(has_text=re.compile(r"Website|Site|Open", re.I)).first
                if link.count() > 0:
                    out["website"] = (link.get_attribute("href") or "").strip()[:500]
            except Exception:
                pass
    except Exception:
        pass
    return out


# Regex patterns to discover which element is rating / review count / phone (Maps can change classes)
RATING_PATTERN = re.compile(r"^\d\.\d$")  # e.g. 4.8
REVIEW_PATTERN = re.compile(r"^\(\d[\d,]*\)$")  # e.g. (1,166)
PHONE_PATTERN = re.compile(r"^[\d\s\-+()]{7,}$")  # phone-like
# From google_maps.html: ZkP5Je has aria-label e.g. "5.0 stars 66 Reviews" or "4.7 stars 1,504 Reviews"
STARS_ARIA_PATTERN = re.compile(r"([\d.]+)\s*stars?\s*([\d,]+)\s*Reviews?", re.I)

# Maps URL patterns for logging/analysis (e.g. /search/QUERY/@lat,lon,zoom or /@lat,lon,zoom)
_MAPS_URL_QUERY_AT = re.compile(r"/search/([^/]+)/@([^/]+)")
_MAPS_URL_AT_ONLY = re.compile(r"@(-?[\d.]+),(-?[\d.]+),(\d+)z")
_MAPS_URL_3D = re.compile(r"@(-?[\d.]+),(-?[\d.]+),(\d+)[az]")


def discover_selectors(feed_handle) -> dict:
    """
    Run once per cell: scan the first result card in the feed and infer class names
    for link, name, rating, review count, phone, and snippet (category/hours).
    Returns a dict of selectors we can use (e.g. link_class, name_class, rating_class, ...).
    Maps changes classes dynamically; this finds current ones on the fly.
    """
    result = {
        "link_class": "",
        "name_class": "",
        "rating_class": "",
        "review_class": "",
        "phone_class": "",
        "snippet_class": "",
        "stars_aria_class": "",  # ZkP5Je in google_maps.html: aria-label="5.0 stars 66 Reviews"
    }
    try:
        # feed_handle is passed as first arg; discover current class names from first card (google_maps.html patterns)
        discovered = feed_handle.evaluate(
            """
            (feed) => {
                const link = feed.querySelector('a[href*="/maps/place/"]');
                if (!link) return {};
                const article = link.closest('div[role="article"]') || link.parentElement;
                if (!article) return { linkClass: (link.className || '').trim().split(/\\s+/)[0] || '' };

                const out = { linkClass: (link.className || '').trim().split(/\\s+/)[0] || '' };
                const ratingRe = /^\\d\\.\\d$/;
                const reviewRe = /^\\(\\d[\\d,]*\\)$/;
                const phoneRe = /^[\\d\\s\\-+()]{7,}$/;
                const starsAriaRe = /[\\d.]+\\s*stars?\\s*[\\d,]+\\s*Reviews?/i;

                const walk = (el) => {
                    if (!el || el.nodeType !== 1) return;
                    const text = (el.textContent || '').trim();
                    const cls = (el.className || '').trim();
                    const aria = (el.getAttribute('aria-label') || '').trim();
                    if (cls && aria && starsAriaRe.test(aria) && !out.starsAriaClass) out.starsAriaClass = cls.split(/\\s+/)[0];
                    if (!cls) { for (const c of el.children) walk(c); return; }
                    if (text && cls && !out.ratingClass && ratingRe.test(text)) out.ratingClass = cls.split(/\\s+/)[0];
                    else if (text && cls && !out.reviewClass && reviewRe.test(text)) out.reviewClass = cls.split(/\\s+/)[0];
                    else if (text && cls && !out.phoneClass && phoneRe.test(text)) out.phoneClass = cls.split(/\\s+/)[0];
                    else if (text && text.length > 3 && text.length < 120 && cls && !out.nameClass && el.tagName !== 'A') {
                        if (cls.includes('Headline') || cls.includes('qBF1Pd') || cls.includes('fontHeadline')) out.nameClass = cls.split(/\\s+/)[0];
                    }
                    if (el.children.length === 0 && text && cls && text.length > 2 && text.length < 80) {
                        if (!out.snippetClass && (cls.includes('W4Efsd') || cls.includes('fontBody'))) out.snippetClass = cls.split(/\\s+/)[0];
                    }
                    for (const c of el.children) walk(c);
                };
                walk(article);
                return out;
            }
            """
        )
        if isinstance(discovered, dict):
            result["link_class"] = (discovered.get("linkClass") or "").strip()
            result["name_class"] = (discovered.get("nameClass") or "").strip()
            result["rating_class"] = (discovered.get("ratingClass") or "").strip()
            result["review_class"] = (discovered.get("reviewClass") or "").strip()
            result["phone_class"] = (discovered.get("phoneClass") or "").strip()
            result["snippet_class"] = (discovered.get("snippetClass") or "").strip()
            result["stars_aria_class"] = (discovered.get("starsAriaClass") or "").strip()
    except Exception as e:
        log.debug("Selector discovery failed: %s", e)
    return result


def log_maps_url(page, stage: str, cid: str = "") -> None:
    """
    Log current Maps URL and parsed parts (query, center, zoom) for analyzing URL patterns.
    Use logs to see: how URL changes after pan vs after 'Search this area', and whether
    area-scoped results use different URL params (e.g. viewport/bounds). Enable with
    config log_url_changes=true.
    """
    cfg = load_config()
    if not cfg.get("log_url_changes", True):
        return
    try:
        url = page.url
        prefix = f"[URL {stage}]" if not cid else f"[Cell {cid} URL {stage}]"
        log.info("%s %s", prefix, url[:120] + ("..." if len(url) > 120 else ""))
        # Parse and log structure for analysis
        query_at = _MAPS_URL_QUERY_AT.search(url)
        at_match = _MAPS_URL_AT_ONLY.search(url) or _MAPS_URL_3D.search(url)
        if query_at:
            log.info("%s parsed: query=%s @=%s", prefix, query_at.group(1)[:40], query_at.group(2)[:50])
        if at_match:
            log.info("%s parsed: lat=%s lon=%s zoom=%s", prefix, at_match.group(1), at_match.group(2), at_match.group(3))
    except Exception as e:
        log.debug("URL log failed: %s", e)


def _random_mouse_move(page):
    """Optional anti-detection: move mouse to random position in viewport."""
    try:
        page.mouse.move(random.randint(100, 800), random.randint(100, 600))
        time.sleep(random.uniform(0.1, 0.3))
    except Exception:
        pass


def pan_map_to_trigger_search_this_area(page, cid: str) -> bool:
    """
    Move the map slightly (pan left then right) so Maps shows the 'Search this area' button.
    From google_maps.html the map has role=application and aria-label containing 'Map'.
    """
    cfg = load_config()
    if not cfg.get("pan_map_before_search_this_area", True):
        return False
    if cfg.get("random_mouse_move", False):
        _random_mouse_move(page)
    pixels = int(cfg.get("pan_map_pixels", 60))
    wait_after = float(cfg.get("pan_map_wait_after_sec", 1.0))
    try:
        # Map: role=application, aria-label="Map · Use arrow keys to pan..." (google_maps.html)
        map_loc = page.locator('[role="application"][aria-label*="Map"]')
        if map_loc.count() == 0:
            map_loc = page.get_by_role("application", name=re.compile("Map", re.I))
        if map_loc.count() == 0:
            log.debug("[Cell %s] Map element not found for pan.", cid)
            return False
        box = map_loc.first.bounding_box()
        if not box or box.get("width", 0) < 100 or box.get("height", 0) < 100:
            log.debug("[Cell %s] Map box too small or missing.", cid)
            return False
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        page.mouse.move(cx, cy)
        time.sleep(0.2)
        page.mouse.down()
        time.sleep(0.1)
        page.mouse.move(cx - pixels, cy)
        time.sleep(0.1)
        page.mouse.up()
        time.sleep(wait_after)
        log.info("[Cell %s] Panned map %spx to trigger 'Search this area'. Waited %.1fs.", cid, pixels, wait_after)
        return True
    except Exception as e:
        log.debug("[Cell %s] Map pan failed: %s", cid, e)
    return False


# From google_maps.html: "Search this area" is a <button aria-label="Search this area" jsaction="search.refresh">
SEARCH_THIS_AREA_TEXTS = (
    "search this area",
    "search this Area",
    "redo search",
    "update results",
    "search as map moves",
    "search as i move",
)


def try_click_search_this_area(page, cid: str, wait_after_sec: float | None = None) -> bool:
    """
    If Maps shows a 'Search this area' button (exact pattern from google_maps.html),
    click it so results are scoped to the visible map. Returns True if clicked.
    """
    cfg = load_config()
    if wait_after_sec is None:
        wait_after_sec = cfg.get("search_this_area_wait_sec", 2.5)
    if not cfg.get("click_search_this_area", True):
        return False
    try:
        # Preferred: exact button from Maps HTML (button with aria-label="Search this area")
        btn = page.locator('button[aria-label="Search this area"]')
        if btn.count() > 0:
            btn.first.click(timeout=3000)
            log.info("[Cell %s] Clicked 'Search this area'. Waiting %.1fs ...", cid, wait_after_sec)
            time.sleep(wait_after_sec)
            return True
        for text in SEARCH_THIS_AREA_TEXTS:
            btn = page.get_by_role("button", name=re.compile(re.escape(text), re.I))
            if btn.count() > 0:
                btn.first.click(timeout=3000)
                log.info("[Cell %s] Clicked '%s'. Waiting %.1fs ...", cid, text, wait_after_sec)
                time.sleep(wait_after_sec)
                return True
        link = page.locator("a, button").filter(has_text=re.compile("search this area|redo search|update (results|search)", re.I)).first
        if link.count() > 0:
            link.click(timeout=3000)
            log.info("[Cell %s] Clicked 'Search this area' link. Waiting %.1fs ...", cid, wait_after_sec)
            time.sleep(wait_after_sec)
            return True
    except Exception as e:
        log.debug("[Cell %s] No 'Search this area' or click failed: %s", cid, e)
    return False


def get_selectors_for_item(discovered: dict) -> dict:
    """Build CSS selectors from discovered classes; use [class*='...'] so extra classes are ok."""
    def sel(cls: str) -> str:
        if not cls:
            return ""
        return f'[class*="{cls}"]'
    stars_aria = sel(discovered.get("stars_aria_class", "")) or "[class*='ZkP5Je']"
    return {
        "name": sel(discovered["name_class"]) or ".qBF1Pd, [class*='Headline']",
        "rating": sel(discovered["rating_class"]) or "[class*='MW4etd']",
        "review": sel(discovered["review_class"]) or "[class*='UY7F9']",
        "phone": sel(discovered["phone_class"]) or "[class*='UsdlK']",
        "snippet": sel(discovered["snippet_class"]) or "[class*='W4Efsd']",
        "stars_aria": stars_aria,
    }


def load_completed_cells(raw_dir: Path) -> set:
    """
    Load union of completed cell IDs from _completed_cells.json and any _completed_cells_w*.json
    (used for resumability with single or parallel workers).
    """
    completed = set()
    # Legacy single-worker file
    p = raw_dir / "_completed_cells.json"
    if p.exists():
        try:
            with open(p, encoding="utf-8") as f:
                completed.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    # Per-worker files
    for path in raw_dir.glob("_completed_cells_w*.json"):
        try:
            with open(path, encoding="utf-8") as f:
                completed.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    return completed


def load_global_seen_from_raw(raw_dir: Path) -> tuple[set, set]:
    """
    Load all place_ids and place_urls from existing output/raw/*.json.
    Also merges from _global_seen_ids.json if present (persisted mid-run snapshot).
    Returns (urls_set, ids_set). Use ids_set for duplicate check (more reliable than URL).
    """
    urls, ids = set(), set()
    for path in raw_dir.glob("*.json"):
        if path.name.startswith("_"):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            for c in data.get("clinics", []):
                u = c.get("place_url")
                pid = c.get("place_id") or (extract_place_id_from_url(u) if u else None)
                if u:
                    urls.add(u)
                if pid:
                    ids.add(pid)
        except (json.JSONDecodeError, OSError):
            continue
    # Merge persisted snapshot so we don't lose IDs if script crashed mid-run
    snapshot_path = raw_dir / "_global_seen_ids.json"
    if snapshot_path.exists():
        try:
            with open(snapshot_path, encoding="utf-8") as f:
                for pid in json.load(f):
                    ids.add(pid)
        except (json.JSONDecodeError, OSError):
            pass
    return urls, ids


def scrape_cell(
    page,
    cell: dict,
    search_query: str,
    scroll_pause_sec: float,
    cell_index: int,
    total_cells: int,
    global_seen_ids: set | None = None,
    cached_selectors: dict | None = None,
    raw_dir: Path | None = None,
) -> list[dict]:
    """
    Navigate to Maps search at cell center, scroll results, return list of clinic dicts.
    Updates global_seen_ids with new place_ids; exits early when a scroll round adds no new places.
    """
    if global_seen_ids is None:
        global_seen_ids = set()
    cid = cell["id"]
    lat = cell["center_lat"]
    lon = cell["center_lon"]

    log.info("[Cell %s] (%s/%s) Starting scrape at (%.4f, %.4f) ...", cid, cell_index + 1, total_cells, lat, lon)
    url = f"https://www.google.com/maps/search/{search_query.replace(' ', '+')}/@{lat},{lon},16z"
    cfg = load_config()
    wait_until = cfg.get("wait_until_goto", "domcontentloaded")
    page.goto(url, wait_until=wait_until, timeout=30000)
    time.sleep(random.uniform(1.2, 2.8))
    if load_config().get("humanize_page", True):
        humanize_page(page)
    if detect_block(page):
        log.error("[Cell %s] Blocked by Google (CAPTCHA/consent/unusual traffic). Waiting 5 min.", cid)
        time.sleep(300)
        return [], None
    if detect_network_failure(page):
        log.error("[Cell %s] Network failure detected. Skipping cell.", cid)
        return [], None
    log_maps_url(page, "after_load", cid)
    # Pan map slightly so "Search this area" button appears (user must move map for it to show)
    pan_map_to_trigger_search_this_area(page, cid)
    log_maps_url(page, "after_pan", cid)
    log.info("[Cell %s] Looking for 'Search this area' to scope results ...", cid)
    try_click_search_this_area(page, cid)
    log_maps_url(page, "after_search_this_area", cid)
    log.info("[Cell %s] Waiting for results feed ...", cid)

    clinics = []
    seen_urls = set()
    results_selector = 'div[role="feed"]'
    try:
        feed = page.wait_for_selector(results_selector, timeout=15000)
        log.info("[Cell %s] Results feed found.", cid)
    except PlaywrightTimeout:
        log.warning("[Cell %s] Results feed not found (timeout). Map list may be empty or DOM changed. Skipping cell.", cid)
        return clinics, None

    if detect_block(page):
        log.error("[Cell %s] Block detected after feed load. Skipping cell.", cid)
        return clinics, None

    # Reuse cached selectors from first cell (or discover and cache)
    if cached_selectors is not None:
        discovered = cached_selectors
        log.info("[Cell %s] Using cached selectors.", cid)
    else:
        discovered = discover_selectors(feed)
        log.info(
            "[Cell %s] Discovered: link=%s name=%s rating=%s review=%s phone=%s snippet=%s stars_aria=%s",
            cid,
            discovered["link_class"] or "(none)",
            discovered["name_class"] or "(none)",
            discovered["rating_class"] or "(none)",
            discovered["review_class"] or "(none)",
            discovered["phone_class"] or "(none)",
            discovered["snippet_class"] or "(none)",
            discovered.get("stars_aria_class") or "(none)",
        )
    sels = get_selectors_for_item(discovered)

    stable_count = 0
    max_stable = 3  # stop after this many scrolls with no new items
    scroll_round = 0
    cell_start = time.time()
    cell_timeout_sec = int(cfg.get("cell_timeout_sec", 300))

    while stable_count < max_stable:
        if time.time() - cell_start > cell_timeout_sec:
            log.warning("[Cell %s] Cell timeout (%ss) exceeded. Stopping cell.", cid, cell_timeout_sec)
            break
        scroll_round += 1
        prev_total = len(clinics)
        global_before_round = len(global_seen_ids)
        if cfg.get("humanize_page", True):
            humanize_page(page)

        # Class-agnostic: all place links inside the feed
        items = feed.query_selector_all('a[href*="/maps/place/"]')
        if not items:
            items = page.query_selector_all('a[href*="/maps/place/"]')
        for node in items:
            try:
                href = node.get_attribute("href") or ""
                if not href or "/maps/place/" not in href:
                    continue
                if href in seen_urls:
                    continue
                place_id = extract_place_id_from_url(href)
                if place_id and place_id in global_seen_ids:
                    seen_urls.add(href)
                    continue
                seen_urls.add(href)
                name = (node.get_attribute("aria-label") or "").strip() or "Unknown"
                rating = ""
                review_count = ""
                phone = ""
                category_hours = ""

                # Get extra fields from parent card (div[role="article"] from google_maps.html)
                article_handle = None
                try:
                    article_handle = node.evaluate_handle("el => el.closest('div[role=article]')")
                    art = article_handle.as_element() if article_handle and hasattr(article_handle, "as_element") else article_handle
                    if art is not None and hasattr(art, "query_selector"):
                        # Name: article has aria-label="Place Name" (reliable per google_maps.html)
                        art_label = (art.get_attribute("aria-label") or "").strip()
                        if art_label:
                            name = art_label
                        elif sels["name"]:
                            name_el = art.query_selector(sels["name"])
                            if name_el:
                                name = (name_el.inner_text() or "").strip().split("\n")[0] or name
                        # Rating + review: element with aria-label like "5.0 stars 66 Reviews" (discovered or ZkP5Je)
                        stars_el = art.query_selector(sels.get("stars_aria", "[class*='ZkP5Je']"))
                        if stars_el:
                            stars_aria = (stars_el.get_attribute("aria-label") or "").strip()
                            m = STARS_ARIA_PATTERN.search(stars_aria)
                            if m:
                                rating, review_count = m.group(1), m.group(2).replace(",", "")
                        if not rating and sels["rating"]:
                            re_el = art.query_selector(sels["rating"])
                            if re_el:
                                rating = (re_el.inner_text() or "").strip()
                        if not review_count and sels["review"]:
                            rc_el = art.query_selector(sels["review"])
                            if rc_el:
                                review_count = (rc_el.inner_text() or "").strip()
                        if sels["phone"]:
                            ph_el = art.query_selector(sels["phone"])
                            if ph_el:
                                phone = (ph_el.inner_text() or "").strip()
                        if sels["snippet"]:
                            snippet_els = art.query_selector_all(sels["snippet"])
                            parts = []
                            for w in snippet_els:
                                t = (w.inner_text() or "").strip()
                                if t and t not in parts:
                                    parts.append(t)
                            category_hours = " · ".join(parts) if parts else ""
                except Exception:
                    pass
                finally:
                    if article_handle:
                        try:
                            article_handle.dispose()
                        except Exception:
                            pass

                place_lat, place_lon = extract_lat_lon(href)
                clinics.append({
                    "name": name,
                    "address": category_hours,
                    "place_url": href,
                    "place_id": place_id,
                    "lat": place_lat,
                    "lon": place_lon,
                    "rating": rating,
                    "review_count": review_count,
                    "phone": phone,
                    "website": "",
                    "hours": "",
                })
                if place_id:
                    global_seen_ids.add(place_id)
            except Exception:
                continue

        new_total = len(clinics)
        new_this_round = new_total - prev_total
        new_to_global = len(global_seen_ids) - global_before_round

        # Early exit: if we've scrolled at least twice and this round added nothing new (all duplicates from other cells), stop
        if scroll_round >= 2 and new_to_global == 0:
            log.info("[Cell %s] Scroll %s: only duplicates (0 new to global). Skipping rest of cell.", cid, scroll_round)
            break

        if new_total == prev_total:
            stable_count += 1
            log.info("[Cell %s] Scroll %s: %s total places. No new results this scroll (%s/%s).", cid, scroll_round, new_total, stable_count, max_stable)
            if scroll_round == 1 and new_total == 0:
                log.warning("[Cell %s] No place links in feed. DOM or region may differ.", cid)
        else:
            stable_count = 0
            log.info("[Cell %s] Scroll %s: %s total places (+%s this scroll).", cid, scroll_round, new_total, new_this_round)

        try:
            feed.evaluate("el => el.scrollBy(0, el.clientHeight)")
        except Exception as e:
            log.warning("[Cell %s] Scroll failed: %s. Stopping.", cid, e)
            break
        time.sleep(random.uniform(max(0.5, scroll_pause_sec - 0.5), scroll_pause_sec + 0.8))

        if cfg.get("memory_cleanup_gc", False):
            try:
                page.evaluate("() => { window.gc && window.gc(); }")
            except Exception:
                pass

        # Save partial results every 10 clinics so crash doesn't lose all data
        if raw_dir and len(clinics) > 0 and len(clinics) % 10 == 0:
            try:
                partial_path = raw_dir / f"{cid}.partial.json"
                with open(partial_path, "w", encoding="utf-8") as f:
                    json.dump({"cell_id": cid, "cell": cell, "clinics": clinics}, f, indent=2)
            except Exception:
                pass

    log_maps_url(page, "after_scrape", cid)

    # Optional: fetch full address, phone, website, hours from each place's detail page
    if cfg.get("fetch_place_details", False) and clinics:
        log.info("[Cell %s] Fetching place details for %s places ...", cid, len(clinics))
        detail_partial_every = max(1, cfg.get("detail_partial_save_every", 10))
        for idx, c in enumerate(clinics):
            if _stop_flag:
                break
            try:
                page.goto(c["place_url"], wait_until="domcontentloaded", timeout=15000)
                time.sleep(random.uniform(0.4, 0.9))
                d = fetch_place_details_from_panel(page, cid)
                if d.get("address"):
                    c["address"] = d["address"]
                if d.get("phone"):
                    c["phone"] = d["phone"]
                if d.get("website"):
                    c["website"] = d["website"]
                if d.get("hours"):
                    c["hours"] = d["hours"]
            except Exception as e:
                log.debug("[Cell %s] Place details failed for %s: %s", cid, c.get("name", ""), e)
            time.sleep(random.uniform(0.3, 0.7))
            # Partial save during detail fetch so crash doesn't lose fetched details
            if raw_dir and (idx + 1) % detail_partial_every == 0:
                try:
                    partial_path = raw_dir / f"{cid}.partial.json"
                    with open(partial_path, "w", encoding="utf-8") as f:
                        json.dump({"cell_id": cid, "cell": cell, "clinics": clinics}, f, indent=2)
                except Exception:
                    pass
        log.info("[Cell %s] Place details done.", cid)

    log.info("[Cell %s] No more results (map list exhausted). Cell done. Moving to next.", cid)
    by_url = {c["place_url"]: c for c in clinics}
    # Return (results, discovered) so caller can cache selectors; discovered is set only when we ran discovery
    return list(by_url.values()), (discovered if cached_selectors is None else None)


def _worker_cfg_for(cfg: dict, worker_id: int, num_workers: int) -> dict:
    """Build config overrides for a worker: separate browser profile and optional per-worker proxy."""
    overrides = {}
    profile = cfg.get("browser_profile_dir", ".browser-profile")
    overrides["browser_profile_dir"] = f"{profile}-w{worker_id}" if num_workers > 1 else profile
    proxy = cfg.get("proxy")
    if isinstance(proxy, list) and proxy:
        overrides["proxy"] = proxy[worker_id % len(proxy)]
    elif proxy:
        overrides["proxy"] = proxy
    return overrides


def run_worker(
    worker_id: int,
    cells_subset: list[dict],
    base_path: str,
    raw_dir_path: str,
    cfg_overrides: dict,
) -> None:
    """
    Run the scrape loop for one worker (own browser, own slice of cells).
    Writes completed to _completed_cells_w{worker_id}.json. Uses same raw_dir for cell JSONs.
    """
    base = Path(base_path)
    raw_dir = Path(raw_dir_path)
    cfg = load_config()
    cfg.update(cfg_overrides)

    delay = cfg.get("delay_between_cells_sec", 3)
    scroll_pause = cfg.get("scroll_pause_sec", 1.5)
    search_query = cfg.get("search_query", "clinic")
    max_retries = cfg.get("max_retries_per_cell", 2)
    use_persistent_context = cfg.get("use_persistent_context", True)
    restart_every_n = int(cfg.get("restart_browser_every_n_cells", 0))
    save_global_seen_every = int(cfg.get("save_global_seen_every_n_ids", 100))
    completed_path = raw_dir / f"_completed_cells_w{worker_id}.json"
    global_seen_snapshot_path = raw_dir / "_global_seen_ids.json"

    completed = set()
    if completed_path.exists():
        try:
            with open(completed_path, encoding="utf-8") as f:
                completed = set(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    _urls, global_seen_ids = load_global_seen_from_raw(raw_dir)
    last_global_seen_saved = len(global_seen_ids)
    cached_selectors = None
    cells_since_restart = 0

    log.info("[Worker %s] Starting with %s cells.", worker_id, len(cells_subset))

    with sync_playwright() as p:
        context, page, browser = _create_context_and_page(p, cfg, base)
        try:
            if cfg.get("use_stealth", False):
                try:
                    from playwright_stealth import stealth_sync
                    stealth_sync(page)
                except ImportError:
                    pass
        except Exception:
            pass

        for i, cell in enumerate(cells_subset):
            if restart_every_n and cells_since_restart >= restart_every_n:
                log.info("[Worker %s] Restarting browser after %s cells.", worker_id, cells_since_restart)
                if use_persistent_context:
                    context.close()
                else:
                    browser.close()
                context, page, browser = _create_context_and_page(p, cfg, base)
                cells_since_restart = 0
                if cfg.get("use_stealth", False):
                    try:
                        from playwright_stealth import stealth_sync
                        stealth_sync(page)
                    except Exception:
                        pass

            cid = cell["id"]
            if cid in completed:
                continue
            for attempt in range(max_retries + 1):
                try:
                    results, discovered = scrape_cell(
                        page, cell, search_query, scroll_pause,
                        cell_index=i, total_cells=len(cells_subset),
                        global_seen_ids=global_seen_ids,
                        cached_selectors=cached_selectors,
                        raw_dir=raw_dir,
                    )
                    if cached_selectors is None and discovered is not None:
                        cached_selectors = discovered
                    if len(results) == 0 and cached_selectors is not None and cfg.get("rediscover_selectors_on_empty", True):
                        cached_selectors = None
                    out_file = raw_dir / f"{cid}.json"
                    with open(out_file, "w", encoding="utf-8") as f:
                        json.dump({"cell_id": cid, "cell": cell, "clinics": results}, f, indent=2)
                    partial_path = raw_dir / f"{cid}.partial.json"
                    if partial_path.exists():
                        try:
                            partial_path.unlink()
                        except OSError:
                            pass
                    completed.add(cid)
                    cells_since_restart += 1
                    with open(completed_path, "w", encoding="utf-8") as f:
                        json.dump(list(completed), f)
                    if save_global_seen_every > 0 and len(global_seen_ids) - last_global_seen_saved >= save_global_seen_every:
                        try:
                            with open(global_seen_snapshot_path, "w", encoding="utf-8") as f:
                                json.dump(list(global_seen_ids), f)
                            last_global_seen_saved = len(global_seen_ids)
                        except Exception:
                            pass
                    delay_actual = random.uniform(max(1, delay - 1), delay + 1.5) if cfg.get("randomize_delays", True) else delay
                    log.info("[Worker %s][Cell %s] Saved %s clinics. Waiting %.1fs ...", worker_id, cid, len(results), delay_actual)
                    break
                except Exception as e:
                    log.exception("[Worker %s] Cell %s attempt %s failed: %s", worker_id, cid, attempt + 1, e)
                    if cfg.get("screenshot_on_error", True):
                        try:
                            errors_dir = base / "errors"
                            errors_dir.mkdir(parents=True, exist_ok=True)
                            page.screenshot(path=str(errors_dir / f"{cid}.png"))
                        except Exception:
                            pass
                    if attempt == max_retries:
                        log.error("[Worker %s] Cell %s failed after %s attempts.", worker_id, cid, max_retries + 1)
                    else:
                        backoff = min(60, 5 * (2 ** attempt))
                        time.sleep(backoff)
            delay_actual = random.uniform(max(1, delay - 1), delay + 1.5) if cfg.get("randomize_delays", True) else delay
            time.sleep(delay_actual)

        if use_persistent_context:
            context.close()
        else:
            browser.close()

    log.info("[Worker %s] Finished. Completed %s cells.", worker_id, len(completed))


def _create_context_and_page(p, cfg, base: Path):
    """Create browser context and page. Used at start and after restart."""
    use_persistent_context = cfg.get("use_persistent_context", True)
    user_data_dir = str(base / cfg.get("browser_profile_dir", ".browser-profile"))
    proxy = cfg.get("proxy")
    locale = cfg.get("locale") or random.choice(["en-US", "en-GB"])
    timezone_id = cfg.get("timezone_id") or random.choice(["Asia/Karachi", "Asia/Dubai", "UTC"])
    viewport = {"width": random.randint(1200, 1400), "height": random.randint(800, 950)} if cfg.get("random_viewport", False) else {"width": 1280, "height": 900}
    user_agent = cfg.get("user_agent") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    kwargs = dict(
        headless=cfg.get("headless", False),
        viewport=viewport,
        user_agent=user_agent,
        accept_downloads=False,
        locale=locale,
        timezone_id=timezone_id,
    )
    if proxy:
        kwargs["proxy"] = proxy
    # Expose GC so memory_cleanup_gc can trigger it (Chromium doesn't expose it by default)
    if cfg.get("memory_cleanup_gc", False):
        kwargs["args"] = ["--js-flags=--expose-gc"]
    if use_persistent_context:
        context = p.chromium.launch_persistent_context(user_data_dir, **kwargs)
        page = context.pages[0] if context.pages else context.new_page()
        return context, page, None
    launch_opts = {"headless": cfg.get("headless", False)}
    if cfg.get("memory_cleanup_gc", False):
        launch_opts["args"] = ["--js-flags=--expose-gc"]
    browser = p.chromium.launch(**launch_opts)
    context = browser.new_context(**kwargs)
    page = context.new_page()
    return context, page, browser


def main():
    global _stop_flag
    cfg = load_config()
    base = Path(__file__).parent
    grid_path = base / cfg["grid_file"]
    raw_dir = base / cfg["raw_dir"]
    raw_dir.mkdir(parents=True, exist_ok=True)

    cells = load_grid(grid_path)
    num_workers = max(1, int(cfg.get("num_workers", 1)))
    completed = load_completed_cells(raw_dir)
    remaining = [c for c in cells if c["id"] not in completed]
    log.info("Resuming: %s cells already completed, %s remaining.", len(completed), len(remaining))

    if num_workers > 1 and remaining:
        # Parallel: partition remaining cells round-robin, spawn one process per worker
        worker_cells = [[] for _ in range(num_workers)]
        for j, cell in enumerate(remaining):
            worker_cells[j % num_workers].append(cell)
        log.info("Starting %s workers with %s cells each (round-robin).", num_workers, [len(w) for w in worker_cells])
        procs = []
        for w in range(num_workers):
            if not worker_cells[w]:
                continue
            overrides = _worker_cfg_for(cfg, w, num_workers)
            proc = multiprocessing.Process(
                target=run_worker,
                args=(w, worker_cells[w], str(base), str(raw_dir), overrides),
            )
            proc.start()
            procs.append(proc)
        for proc in procs:
            proc.join()
        # Merge worker completed files into legacy _completed_cells.json for next run
        all_done = load_completed_cells(raw_dir)
        try:
            with open(raw_dir / "_completed_cells.json", "w", encoding="utf-8") as f:
                json.dump(list(all_done), f)
        except Exception:
            pass
        log.info("All workers finished. Total cells completed: %s.", len(all_done))
        return

    # Single-worker path (original logic)
    delay = cfg.get("delay_between_cells_sec", 3)
    scroll_pause = cfg.get("scroll_pause_sec", 1.5)
    search_query = cfg.get("search_query", "clinic")
    max_retries = cfg.get("max_retries_per_cell", 2)
    use_persistent_context = cfg.get("use_persistent_context", True)
    restart_every_n = int(cfg.get("restart_browser_every_n_cells", 0))
    completed_path = raw_dir / "_completed_cells.json"
    _urls, global_seen_ids = load_global_seen_from_raw(raw_dir)
    log.info("Loaded %s known place IDs from existing raw data (early exit when only duplicates).", len(global_seen_ids))
    last_global_seen_saved = len(global_seen_ids)
    global_seen_snapshot_path = raw_dir / "_global_seen_ids.json"
    save_global_seen_every = int(cfg.get("save_global_seen_every_n_ids", 100))
    total = len(cells)
    cached_selectors = None
    cells_since_restart = 0

    with sync_playwright() as p:
        context, page, browser = _create_context_and_page(p, cfg, base)
        user_data_dir = str(base / cfg.get("browser_profile_dir", ".browser-profile"))
        log.info("Browser launched." if not restart_every_n else "Browser launched (restart every %s cells).", restart_every_n or "N/A")

        try:
            if cfg.get("use_stealth", False):
                try:
                    from playwright_stealth import stealth_sync
                    stealth_sync(page)
                    log.info("Stealth mode applied.")
                except ImportError:
                    log.debug("playwright-stealth not installed; run: pip install playwright-stealth")
        except Exception as e:
            log.debug("Stealth apply failed: %s", e)

        for i, cell in enumerate(cells):
            if restart_every_n and cells_since_restart >= restart_every_n:
                log.info("Restarting browser after %s cells (anti-fingerprint).", cells_since_restart)
                if use_persistent_context:
                    context.close()
                else:
                    browser.close()
                context, page, browser = _create_context_and_page(p, cfg, base)
                cells_since_restart = 0
                if cfg.get("use_stealth", False):
                    try:
                        from playwright_stealth import stealth_sync
                        stealth_sync(page)
                    except Exception:
                        pass
            if _stop_flag:
                log.info("Stop flag set. Exiting gracefully.")
                break
            cid = cell["id"]
            if cid in completed:
                continue
            for attempt in range(max_retries + 1):
                try:
                    results, discovered = scrape_cell(
                        page, cell, search_query, scroll_pause,
                        cell_index=i, total_cells=total,
                        global_seen_ids=global_seen_ids,
                        cached_selectors=cached_selectors,
                        raw_dir=raw_dir,
                    )
                    if cached_selectors is None and discovered is not None:
                        cached_selectors = discovered
                    if len(results) == 0 and cached_selectors is not None and cfg.get("rediscover_selectors_on_empty", True):
                        log.info("[Cell %s] No results; clearing cached selectors for rediscovery next cell.", cid)
                        cached_selectors = None
                    out_file = raw_dir / f"{cid}.json"
                    with open(out_file, "w", encoding="utf-8") as f:
                        json.dump({"cell_id": cid, "cell": cell, "clinics": results}, f, indent=2)
                    partial_path = raw_dir / f"{cid}.partial.json"
                    if partial_path.exists():
                        try:
                            partial_path.unlink()
                        except OSError:
                            pass
                    completed.add(cid)
                    cells_since_restart += 1
                    with open(completed_path, "w", encoding="utf-8") as f:
                        json.dump(list(completed), f)
                    # Persist global_seen_ids periodically so a crash doesn't lose duplicate-tracking progress
                    if save_global_seen_every > 0 and len(global_seen_ids) - last_global_seen_saved >= save_global_seen_every:
                        try:
                            with open(global_seen_snapshot_path, "w", encoding="utf-8") as f:
                                json.dump(list(global_seen_ids), f)
                            last_global_seen_saved = len(global_seen_ids)
                        except Exception:
                            pass
                    delay_actual = random.uniform(max(1, delay - 1), delay + 1.5) if cfg.get("randomize_delays", True) else delay
                    log.info("[Cell %s] Saved %s clinics to %s. Waiting %.1fs ...", cid, len(results), out_file.name, delay_actual)
                    break
                except Exception as e:
                    log.exception("Cell %s attempt %s failed: %s", cid, attempt + 1, e)
                    if cfg.get("screenshot_on_error", True):
                        try:
                            errors_dir = base / "errors"
                            errors_dir.mkdir(parents=True, exist_ok=True)
                            page.screenshot(path=str(errors_dir / f"{cid}.png"))
                            log.info("[Cell %s] Screenshot saved to errors/%s.png", cid, cid)
                        except Exception:
                            pass
                    if attempt == max_retries:
                        log.error("[Cell %s] Failed after %s attempts. Moving to next cell.", cid, max_retries + 1)
                    else:
                        backoff = min(60, 5 * (2 ** attempt))
                        log.info("[Cell %s] Retrying in %ss (exponential backoff) ...", cid, backoff)
                        time.sleep(backoff)
            if not _stop_flag:
                delay_actual = random.uniform(max(1, delay - 1), delay + 1.5) if cfg.get("randomize_delays", True) else delay
                time.sleep(delay_actual)

        log.info("Scrape run finished. Total cells completed: %s.", len(completed))
        if use_persistent_context:
            context.close()
        else:
            browser.close()


if __name__ == "__main__":
    main()
