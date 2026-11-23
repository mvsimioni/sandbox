"""App D outlines in building millimetres, and the plan rooms that lie inside them (final verification R1-08, decided ruling 2).

``out/registers/appd_outlines_mm.csv`` holds one row per App D area outline (storeys L02 to L07): storey, annotation index and the polygon in building
millimetres (the frame of ``label_point_x_mm`` and ``label_point_y_mm`` of ``room_polygons_<storey>.csv``). It is written by ``build_outlines`` from the
App D page and the calibrations of the zoning (the same ones that give the plan rooms their millimetre coordinates).
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List, Tuple

REG = Path("out/registers")
OUTLINES = REG / "appd_outlines_mm.csv"
STOREYS = ("L02", "L03", "L04", "L05", "L06", "L07")


def build_outlines(root: Path = Path("."), storeys=STOREYS) -> List[Dict[str, Any]]:
    import os
    from shapely.geometry import Polygon
    from ..zoning import calibrate as cal  # noqa: F401
    from ..zoning import storeys as sv
    cwd = os.getcwd()
    os.chdir(root)
    try:
        rows: List[Dict[str, Any]] = []
        for key in storeys:
            info = sv.storey_info(key)
            if info["appd_page0"] is None:
                continue
            P = sv.storey_paths(key)
            C = sv.stage_calibrate({"P": P, "info": info})
            page = sv._open(P["root"], sv.APPD_PDF, info["appd_page0"])
            for a in sv.load_appd_polygons_checked(page, C["cal_appd"], C["cal_bg"]):
                pts = [C["cal_bg"].to_building(x, y) for x, y in a.polygon_pv_bg.exterior.coords]
                poly = Polygon([(float(x), float(y)) for x, y in pts]).buffer(0)
                rows.append(dict(storey=key, annotation_index=a.annotation_index, caption_m2=a.caption_m2, polygon_building_mm_wkt=poly.wkt))
    finally:
        os.chdir(cwd)
    path = Path(root) / OUTLINES
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["storey", "annotation_index", "caption_m2", "polygon_building_mm_wkt"])
        w.writeheader()
        w.writerows(rows)
    return rows


def load_outlines(root: Path = Path(".")) -> Dict[Tuple[str, str], Any]:
    from shapely import wkt
    path = Path(root) / OUTLINES
    if not path.exists():
        build_outlines(root)
    with path.open(encoding="utf-8") as fh:
        return {(r["storey"], r["annotation_index"]): wkt.loads(r["polygon_building_mm_wkt"]) for r in csv.DictReader(fh)}


def rooms_inside(root: Path, storey: str, outlines: Dict[Tuple[str, str], Any], rooms: List[Dict[str, str]]) -> Dict[str, List[Dict[str, str]]]:
    """Plan rooms of one storey by outline: a room lies in an outline when its label point is inside it (building mm)."""
    from shapely.geometry import Point
    res: Dict[str, List[Dict[str, str]]] = {}
    items = [(k[1], p) for k, p in outlines.items() if k[0] == storey]
    for r in rooms:
        try:
            pt = Point(float(r["label_point_x_mm"]), float(r["label_point_y_mm"]))
        except (KeyError, ValueError):
            continue
        for idx, poly in items:
            if poly.contains(pt):
                res.setdefault(idx, []).append(r)
    return res
