"""Open floor with no room outline (corridors open to the plan, bays without walls) joins the neighbouring air-conditioned zone (24 Nov 2026,
Marcus: the AHU serving the corridor has gaps).

On a storey: the floor plate (closed rooms joined across 3 m, holes filled) less every room outline is the open floor. Thin bands (walls, under
0.8 m wide) are removed by an opening of 0.4 m. Each open piece of 2 m2 or more is added to the outline of the neighbouring room whose class is an
air handling or fan coil unit and that shares the longest edge with it. The measured area of that room is not changed (the added open floor is
shown on the zoning only) and the room says so in its flags. A piece with no such neighbour stays open.

``python3 -m src.zoning.gap_fill --storeys L05,L06``  (run after open_region and booklet_pod; idempotent: the added parts are kept apart)
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Dict, List

import numpy as np
import shapely
import shapely.affinity
from shapely import wkt
from shapely.geometry import Polygon

csv.field_size_limit(1 << 30)
ROOT = Path(__file__).resolve().parent.parent.parent
REG = ROOT / "out/registers"
FLAG = "outline extended over open floor with no room outline (gap fill), measured area unchanged"


def _clean(g):
    g = shapely.make_valid(g)
    ps = [q for q in getattr(g, "geoms", [g]) if q.geom_type in ("Polygon", "MultiPolygon")]
    ps = [x for q in ps for x in getattr(q, "geoms", [q])]
    g = shapely.union_all([q.buffer(0) for q in ps]).simplify(0.2, preserve_topology=True).buffer(0)
    return g


def _rd(p: Path):
    with p.open(newline="", encoding="utf-8") as fh:
        r = csv.DictReader(fh)
        return list(r), r.fieldnames


def run(st: str) -> Dict[str, object]:
    from .open_region import _affine
    path = REG / f"room_polygons_{st}.csv"
    rows, cols = _rd(path)
    if "gap_fill_pv_wkt" not in cols:
        cols.append("gap_fill_pv_wkt")
    zr, _ = _rd(REG / f"zone_assignment_{st}.csv")
    kind = {z["room_id"]: z["class_kind"] for z in zr}
    geo = {}
    for r in rows:
        if not r["polygon_pv_wkt"]:
            continue
        g = shapely.make_valid(wkt.loads(r["polygon_pv_wkt"]))
        if r.get("gap_fill_pv_wkt"):                       # idempotent: take the earlier fill off again
            g = _clean(g.difference(wkt.loads(r["gap_fill_pv_wkt"]).buffer(0.01)))
            r["polygon_pv_wkt"] = shapely.to_wkt(g, rounding_precision=2)
            r["gap_fill_pv_wkt"] = ""
        geo[r["room_id"]] = g
    k2 = float(np.median([float(r["a_area_m2"]) / geo[r["room_id"]].area for r in rows
                          if r["room_id"] in geo and r["a_area_m2"] and "-T" not in r["room_id"] and geo[r["room_id"]].area > 0]))
    m = 1.0 / k2 ** 0.5
    closed = shapely.union_all([g for rid, g in geo.items() if "-T" not in rid])
    plate = closed.buffer(3 * m).buffer(-3 * m)
    # voids and courts stay holes (not floor); a piece larger than 250 m2 is not a corridor and stays open
    gap = plate.difference(shapely.union_all(list(geo.values())).buffer(0.3))
    gap = gap.buffer(-0.4 * m).buffer(0.4 * m)
    # lift shafts are not covered by any system: never filled, and cut out of every outline drawn on the storey (D-099)
    from .lift_shafts import write as _lifts
    lifts = [shapely.make_valid(wkt.loads(x["polygon_pv_wkt"])) for x in _lifts(st, k2)]
    shaft = shapely.union_all(lifts).buffer(0.2) if lifts else Polygon()
    pieces = [g for g in getattr(gap, "geoms", [gap]) if g.geom_type == "Polygon" and 2.0 <= g.area * k2 <= 250.0
              and not (not shaft.is_empty and g.intersects(shaft) and g.intersection(shaft).area > 0.3 * g.area)]
    pieces = [g.difference(shaft) if not shaft.is_empty else g for g in pieces]
    pieces = [q for g in pieces for q in getattr(g, "geoms", [g]) if q.geom_type == "Polygon" and q.area * k2 >= 2.0]
    tree_ids = [rid for rid in geo if kind.get(rid) in ("AHU", "FCU", "AHU spill", "CRAC")]
    added: Dict[str, List[Polygon]] = {}
    left = 0.0
    for pc in pieces:
        ring = pc.buffer(1.0 * m)
        best, bl = None, 0.0
        for rid in tree_ids:
            g = geo[rid]
            if not g.intersects(ring):
                continue
            try:
                l_ = g.boundary.intersection(ring).length
            except Exception:
                l_ = 0.0
            if l_ > bl:
                best, bl = rid, l_
        if best is None:
            left += pc.area * k2
            continue
        added.setdefault(best, []).append(pc)
    M = _affine(rows)
    n = 0
    for r in rows:
        rid = r["room_id"]
        if rid not in added:
            continue
        fill = shapely.union_all(added[rid])
        g = _clean(shapely.union_all([geo[rid], fill.buffer(0.35)]))
        r["polygon_pv_wkt"] = shapely.to_wkt(g, rounding_precision=2)
        r["gap_fill_pv_wkt"] = shapely.to_wkt(fill, rounding_precision=2)
        if FLAG not in (r.get("flags") or ""):
            r["flags"] = ((r.get("flags") or "") + " | " + FLAG + f" ({fill.area * k2:.1f} m2)").strip(" |")
        n += 1
    cut = 0
    if not shaft.is_empty:
        for r in rows:
            if r["polygon_pv_wkt"]:
                g0 = shapely.make_valid(wkt.loads(r["polygon_pv_wkt"]))
                if g0.intersects(shaft) and g0.intersection(shaft).area * k2 > 0.5:
                    g1 = _clean(g0.difference(shaft))
                    if not g1.is_empty:
                        r["polygon_pv_wkt"] = shapely.to_wkt(g1, rounding_precision=2)
                        if "lift shaft cut out" not in (r.get("flags") or ""):
                            r["flags"] = ((r.get("flags") or "") + " | lift shaft cut out of the drawn outline (D-099)").strip(" |")
                        cut += 1
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
    return {"storey": st, "pieces": len(pieces), "rooms_extended": n, "filled_m2": round(sum(g.area for v in added.values() for g in v) * k2, 1),
            "left_open_m2": round(left, 1), "lift_shafts": len(lifts), "outlines_cut_by_shafts": cut}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--storeys", default="LB3,B3,B2,B1,L01,L02,L03,L04,L05,L06,L07")
    a = ap.parse_args()
    for s in a.storeys.split(","):
        print(run(s))
