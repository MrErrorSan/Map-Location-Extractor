"""
Merge all output/raw/<cell_id>.json files, deduplicate clinics by place_id or name+address,
write lahore_clinics.json.
"""
import hashlib
import json
from pathlib import Path


def load_config():
    config_path = Path(__file__).parent / "config.json"
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def clinic_key(c: dict) -> str:
    """Stable key for deduplication: place_id if present, else hash of name+address."""
    pid = c.get("place_id") or c.get("place_url")
    if pid:
        return f"id:{pid}"
    raw = f"{c.get('name','')}|{c.get('address','')}"
    return f"hash:{hashlib.sha256(raw.encode()).hexdigest()}"


def main():
    cfg = load_config()
    base = Path(__file__).parent
    raw_dir = base / cfg["raw_dir"]
    output_dir = base / cfg["output_dir"]
    output_dir.mkdir(parents=True, exist_ok=True)

    by_key = {}
    cell_ids_by_clinic = {}  # clinic_key -> set of cell_ids
    cells_count = 0

    for path in sorted(raw_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        clinics = data.get("clinics", [])
        cell_id = data.get("cell_id", path.stem)
        cells_count += 1
        for c in clinics:
            k = clinic_key(c)
            if k not in by_key:
                by_key[k] = {**c, "cell_ids": []}
                cell_ids_by_clinic[k] = set()
            if cell_id not in cell_ids_by_clinic[k]:
                by_key[k]["cell_ids"].append(cell_id)
                cell_ids_by_clinic[k].add(cell_id)

    clinics_list = []
    for c in by_key.values():
        clinics_list.append({
            "name": c.get("name"),
            "address": c.get("address"),
            "place_url": c.get("place_url"),
            "place_id": c.get("place_id"),
            "lat": c.get("lat"),
            "lon": c.get("lon"),
            "rating": c.get("rating"),
            "review_count": c.get("review_count"),
            "phone": c.get("phone"),
            "cell_ids": c.get("cell_ids", []),
        })

    out = {
        "boundary": cfg.get("boundary_name", "Lahore"),
        "source": cfg.get("boundary_file", "lahore.json"),
        "cells_count": cells_count,
        "clinics_count": len(clinics_list),
        "clinics": clinics_list,
    }
    out_path = output_dir / "lahore_clinics.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"Wrote {out_path}: {len(clinics_list)} unique clinics from {cells_count} cells.")


if __name__ == "__main__":
    main()
