"""
Build a 500m grid from a 4-point boundary (e.g. lahore.json).
Outputs grid.json with cell id, center lat/lon, and optional bounds.
Uses point-in-polygon so only cells inside the boundary are kept.
"""
import json
import os
from pathlib import Path


def load_config():
    config_path = Path(__file__).parent / "config.json"
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def meters_to_degrees(meters: float, center_lat: float) -> tuple[float, float]:
    """Approximate meters to degrees (lat, lon) at given latitude."""
    # 1 degree lat ≈ 111 km; 1 degree lon ≈ 111 * cos(lat) km
    import math
    lat_deg = meters / (111_000)
    lon_deg = meters / (111_000 * math.cos(math.radians(center_lat)))
    return lat_deg, lon_deg


def point_in_polygon(plat: float, plon: float, polygon: list[list[float]]) -> bool:
    """Ray-casting: point (plat, plon) inside polygon (list of [lat, lon])? Uses lon=x, lat=y."""
    n = len(polygon)
    inside = False
    px, py = plon, plat  # x = lon, y = lat for ray in positive x
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][1], polygon[i][0]  # lon, lat
        xj, yj = polygon[j][1], polygon[j][0]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def build_grid(
    boundary_path: str | Path,
    cell_size_m: int,
    boundary_name: str,
    output_path: str | Path,
) -> list[dict]:
    with open(boundary_path, encoding="utf-8") as f:
        polygon = json.load(f)
    if len(polygon) < 3:
        raise ValueError("Boundary must have at least 3 points")

    lats = [p[0] for p in polygon]
    lons = [p[1] for p in polygon]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)
    center_lat = (min_lat + max_lat) / 2

    lat_deg, lon_deg = meters_to_degrees(cell_size_m, center_lat)

    cells = []
    cell_id = 0
    lat = min_lat + lat_deg / 2
    while lat <= max_lat:
        lon = min_lon + lon_deg / 2
        while lon <= max_lon:
            if point_in_polygon(lat, lon, polygon):
                cells.append({
                    "id": str(cell_id),
                    "center_lat": round(lat, 6),
                    "center_lon": round(lon, 6),
                    "boundary_name": boundary_name,
                })
                cell_id += 1
            lon += lon_deg
        lat += lat_deg

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"boundary_name": boundary_name, "cells": cells}, f, indent=2)
    return cells


def main():
    cfg = load_config()
    base = Path(__file__).parent
    boundary_file = base / cfg["boundary_file"]
    grid_file = base / cfg["grid_file"]
    build_grid(
        boundary_path=boundary_file,
        cell_size_m=cfg["cell_size_m"],
        boundary_name=cfg["boundary_name"],
        output_path=grid_file,
    )
    print(f"Wrote {grid_file}")


if __name__ == "__main__":
    main()
