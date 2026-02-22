"""
Run the full pipeline: grid -> scrape -> merge.
Optional: --grid-only | --scrape-only | --merge-only to run a single step.
"""
import argparse
import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main():
    parser = argparse.ArgumentParser(description="Lahore clinics scraping pipeline")
    parser.add_argument(
        "--grid-only",
        action="store_true",
        help="Only build grid from boundary",
    )
    parser.add_argument(
        "--scrape-only",
        action="store_true",
        help="Only run Google Maps scraper (requires grid.json)",
    )
    parser.add_argument(
        "--merge-only",
        action="store_true",
        help="Only merge and deduplicate raw cell JSONs",
    )
    args = parser.parse_args()

    if args.grid_only:
        import grid_builder
        grid_builder.main()
        return
    if args.scrape_only:
        import scraper
        scraper.main()
        return
    if args.merge_only:
        import merge_dedup
        merge_dedup.main()
        return

    # Full pipeline
    import grid_builder
    import scraper
    import merge_dedup
    grid_builder.main()
    scraper.main()
    merge_dedup.main()


if __name__ == "__main__":
    main()
