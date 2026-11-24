"""Room outlines drawn by inspection where the plan measurement misses the room (Marcus mark-ups, 24 Nov 2026).

B3-R058 Chiller: the plan face measured 99 m2 (one equipment plinth). The plant room is the whole hall between the basement wall (west), the
north wall, the east corridor (stepping round the WATER / lift pit room) and the DHW plant, down to the cross wall south of the DHW plant
(Marcus mark-up of the B3 zoning). Rooms with their own closed outline inside it are cut out. The measured area is that of the drawn outline.
"""
from __future__ import annotations

import csv
from pathlib import Path

import shapely
import shapely.affinity
from shapely import wkt
from shapely.geometry import Polygon

csv.field_size_limit(1 << 30)
REG = Path(__file__).resolve().parent.parent.parent / "out/registers"
DRAWN = {
    "B3-R058": [(501, 762), (795, 762), (795, 1043), (778, 1043), (778, 1131), (799, 1131), (799, 1195), (611, 1195), (611, 1334), (501, 1334)],
}
NOTE = "outline drawn by inspection from the Marcus mark-up of 24 Nov 2026 (whole plant room); measured area of the drawn outline"


def run():
    from .open_region import _affine
    out = []
    for rid, pts in DRAWN.items():
        st = rid.split("-")[0]
        path = REG / f"room_polygons_{st}.csv"
        with path.open(newline="", encoding="utf-8") as fh:
            rd = csv.DictReader(fh)
            rows, cols = list(rd), rd.fieldnames
        k2 = [float(r["a_area_m2"]) / wkt.loads(r["polygon_pv_wkt"]).area for r in rows
              if r["polygon_pv_wkt"] and r["a_area_m2"] and "-T" not in r["room_id"] and r["room_id"] != rid and wkt.loads(r["polygon_pv_wkt"]).area > 0]
        k2 = sorted(k2)[len(k2) // 2]
        others = shapely.union_all([shapely.make_valid(wkt.loads(r["polygon_pv_wkt"])).buffer(0) for r in rows
                                    if r["polygon_pv_wkt"] and r["room_id"] != rid and "-T" not in r["room_id"]
                                    and shapely.make_valid(wkt.loads(r["polygon_pv_wkt"])).intersects(Polygon(pts))])
        g = Polygon(pts).difference(others.buffer(0.2)) if not others.is_empty else Polygon(pts)
        g = shapely.make_valid(g).buffer(0)
        M = _affine(rows)
        for r in rows:
            if r["room_id"] == rid:
                r["polygon_pv_wkt"] = shapely.to_wkt(g, rounding_precision=2)
                r["polygon_building_mm_wkt"] = shapely.to_wkt(shapely.affinity.affine_transform(g, [M[0, 0], M[1, 0], M[0, 1], M[1, 1], M[2, 0], M[2, 1]]), rounding_precision=0)
                r["a_area_m2"] = f"{g.area * k2:.3f}"
                r["measurement_confidence"] = "medium: " + NOTE
                if NOTE not in (r.get("flags") or ""):
                    r["flags"] = ((r.get("flags") or "") + " | " + NOTE).strip(" |")
                out.append((rid, round(g.area * k2, 1)))
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        zp = REG / f"zone_assignment_{st}.csv"
        with zp.open(newline="", encoding="utf-8") as fh:
            rd = csv.DictReader(fh)
            zr, zc = list(rd), rd.fieldnames
        for z in zr:
            if z["room_id"] == rid:
                z["area_m2"] = f"{g.area * k2:.3f}"
        with zp.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=zc)
            w.writeheader()
            w.writerows(zr)
    return out


if __name__ == "__main__":
    print(run())
