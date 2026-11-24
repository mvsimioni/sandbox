"""Outline for the L06 pod 3 open region (OI-R212-1, D-040, D-048, CF-0072).

``python3 -m src.zoning.open_region``    (run after the ``compare`` stage; idempotent)

The 21 room tags L06-R250-T01 to -T21 lie in one open region of 830.4 m2 (openings not closed): the plan gives no closed outline for the
three units AHU-B-07-06DSOT-11, AHU-B-07-06DSOT-12A/B and AHU-B-07-06STER-03A/B (and the other pod 3 units). Simpler documented method
(D-071, flagged for the human check): the region outline is the convex hull of the tag boxes, widened evenly until its area equals the
region area printed in room_polygons_L06.csv (830.4 m2); each tag takes the part of the region that is nearer to it than to any other tag
(nearest-tag partition). The zone of a unit is the union of the parts of its tags. This is an approximation of the booklet zone line and not
a measured outline: confidence 'low', and the polygon says so in the register.
"""
from __future__ import annotations

import csv
csv.field_size_limit(1 << 30)
import sys
from pathlib import Path
from typing import Dict, List

import numpy as np
import shapely
from shapely import wkt
from shapely.geometry import MultiPoint, Polygon, box
from shapely.ops import voronoi_diagram

ROOT = Path(__file__).resolve().parent.parent.parent
REG = ROOT / "out/registers"
STOREY, REGION = "L06", "L06-R250"
NOTE = ("approximate outline: nearest-tag partition of an open region of %.1f m2 (the plan has no closed outline; OI-R212-1, D-071). Check on the drawing")


def _rd(p: Path):
    with p.open(newline="", encoding="utf-8") as fh:
        r = csv.DictReader(fh)
        return list(r), r.fieldnames


def _affine(rows: List[Dict[str, str]]):
    """Affine page view -> building mm, fitted on the vertices of the rooms that have both polygons."""
    src, dst = [], []
    for r in rows:
        if r["polygon_pv_wkt"] and r["polygon_building_mm_wkt"]:
            a, b = wkt.loads(r["polygon_pv_wkt"]), wkt.loads(r["polygon_building_mm_wkt"])
            if a.geom_type != "Polygon" or b.geom_type != "Polygon":      # drawn or split outlines are not registration pairs
                continue
            if len(a.exterior.coords) == len(b.exterior.coords):
                src += list(a.exterior.coords)
                dst += list(b.exterior.coords)
        if len(src) > 400:
            break
    A = np.hstack([np.array(src), np.ones((len(src), 1))])
    M, *_ = np.linalg.lstsq(A, np.array(dst), rcond=None)
    return M


def _to_mm(poly: Polygon, M) -> Polygon:
    pts = np.hstack([np.array(poly.exterior.coords), np.ones((len(poly.exterior.coords), 1))]) @ M
    return Polygon(pts)


def run(STOREY: str = STOREY, REGION: str = REGION) -> Dict[str, object]:
    path = REG / f"room_polygons_{STOREY}.csv"
    rows, cols = _rd(path)
    tags = [r for r in rows if r["room_id"].startswith(REGION + "-T")]
    if not tags:
        return {"patched": 0}
    k2 = [float(r["a_area_m2"]) / wkt.loads(r["polygon_pv_wkt"]).area for r in rows if r["polygon_pv_wkt"] and r["a_area_m2"] and "-T" not in r["room_id"]]
    k2 = float(np.median(k2))
    region_m2 = float(tags[0]["region_area_m2"])
    boxes = [tuple(map(float, r["tag_box_pv"].split(","))) for r in tags]
    cen = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in boxes]
    hull = MultiPoint(cen).convex_hull
    # the widened region must not cover a room that has its own closed outline: the area is matched after the closed rooms are cut out
    # (a ring corridor round closed theatres keeps the area of the ring)
    others = shapely.union_all([shapely.make_valid(wkt.loads(r["polygon_pv_wkt"])).buffer(0) for r in rows if r["polygon_pv_wkt"] and "-T" not in r["room_id"]]).buffer(0.5)
    # the floor plate: every closed room outline of the storey, joined across gaps up to 3 m; the open region stays inside it
    d_ = 3.0 / k2 ** 0.5
    plate = shapely.union_all([shapely.make_valid(wkt.loads(r["polygon_pv_wkt"])).buffer(0) for r in rows if r["polygon_pv_wkt"] and "-T" not in r["room_id"]]).buffer(d_).buffer(-d_)
    plate = shapely.union_all([Polygon(g.exterior) for g in getattr(plate, "geoms", [plate]) if g.geom_type == "Polygon"])   # an open region is a hole of the joined rooms: fill it
    hull = hull.intersection(plate) if not hull.intersection(plate).is_empty else hull
    lo, hi = 0.0, 3000.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if hull.buffer(mid).intersection(plate).difference(others).area * k2 < region_m2:
            lo = mid
        else:
            hi = mid
    reg = hull.buffer(hi).intersection(plate).difference(others)
    cells = voronoi_diagram(MultiPoint(cen), envelope=reg.buffer(10).envelope)
    M = _affine(rows)
    patched = 0
    for r, c in zip(tags, cen):
        pt = shapely.Point(c)
        cell = next(g for g in cells.geoms if g.contains(pt))
        part = cell.intersection(reg)
        if part.is_empty or part.area * k2 < 0.5:        # the tag falls inside a closed room or outside the plate: its tag box only
            bx = boxes[tags.index(r)]
            part = box(*bx)
        if part.geom_type != "Polygon":
            part = max((g for g in part.geoms if g.geom_type == "Polygon"), key=lambda g: g.area)
        part = part.simplify(0.5, preserve_topology=True)       # 0.5 pt on the sheet: well below the drawing accuracy
        if part.geom_type != "Polygon":
            part = max((g for g in getattr(part, "geoms", [part]) if g.geom_type == "Polygon"), key=lambda g: g.area)
        r["polygon_pv_wkt"] = shapely.to_wkt(part, rounding_precision=2)
        r["polygon_building_mm_wkt"] = shapely.to_wkt(_to_mm(part, M), rounding_precision=0)
        r["a_area_m2"] = f"{part.area * k2:.3f}"
        r["method_agreement"] = "approximate: nearest-tag partition of an open region"
        r["measurement_confidence"] = "low: " + NOTE % region_m2
        r["flags"] = (r.get("flags") or "").split(" | " + NOTE[:30])[0] + " | " + NOTE % region_m2
        patched += 1
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    # the zone assignment carries the area of each tag
    zp = REG / f"zone_assignment_{STOREY}.csv"
    zr, zc = _rd(zp)
    by = {r["room_id"]: r for r in tags}
    for z in zr:
        if z["room_id"] in by:
            z["area_m2"] = by[z["room_id"]]["a_area_m2"]
            if "approximate outline" not in (z.get("notes") or ""):
                z["notes"] = ((z.get("notes") or "") + " Outline approximate: nearest-tag partition of the open region of %.1f m2 (OI-R212-1, D-071); check on the drawing." % region_m2).strip()
    with zp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=zc)
        w.writeheader()
        w.writerows(zr)
    tot = sum(float(r["a_area_m2"]) for r in tags if r["polygon_pv_wkt"])
    return {"patched": patched, "area_total_m2": round(tot, 1), "region_m2": region_m2}


def run_all() -> List[Dict[str, object]]:
    """Every open region of LB3 to L07 whose tags have no outline (D-071, extended 24 Nov 2026 for the MOT pods and the other open regions)."""
    out = []
    for st in ["LB3", "B3", "B2", "B1", "L01", "L02", "L03", "L04", "L05", "L06", "L07"]:
        rows, _ = _rd(REG / f"room_polygons_{st}.csv")
        regions = sorted({r["room_id"].rsplit("-T", 1)[0] for r in rows if "-T" in r["room_id"] and r["tag_box_pv"]
                          and (not r["polygon_pv_wkt"] or "nearest-tag partition" in (r.get("method_agreement") or ""))})
        for rg in regions:
            res = run(st, rg)
            out.append(dict(storey=st, region=rg, **res))
    return out


if __name__ == "__main__":
    for x in run_all():
        print(x)
    sys.exit(0)
