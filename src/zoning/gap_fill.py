"""Open floor with no room outline (corridors open to the plan, bays without walls) joins the neighbouring air-conditioned zone (24 Nov 2026,
Marcus: the AHU serving the corridor has gaps).

On a storey: the floor plate (closed rooms joined across 3 m, holes filled) less every room outline is the open floor. Thin bands (walls, under
0.8 m wide) are removed by an opening of 0.4 m. Each open piece of 2 m2 or more is added to the outline of the neighbouring room whose class is an
air handling or fan coil unit and that shares the longest edge with it. AHU plant rooms (spill air) never take open floor: their outline is
the room with its plinths only (Marcus 24 Nov 2026: stair and lobby pockets were shown as plant room). The measured area of that room is not changed (the added open floor is
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
# open floor that is a space of its own (no room outline or tag in the plan): the piece holding the point becomes a zoning row of its own
# (storey, point pv, row id, name, class, basis)
OWN_SPACES = [("B2", (950.0, 1480.0), "B2-G001", "Trolley staging (open floor)", "AHU-B-B1-B2OFOO-02",
               "Equipment Schedule SCH/1 Corr 5: AHU-B-B1-B2OFOO-02 'Food Services (AGV, Staging)'; the open trolley bay west of the Plating Area "
               "has no room outline or tag on the plan; assigned by the schedule description, to confirm (Marcus mark-up 24 Nov 2026)")]
FLAG = "outline extended over open floor with no room outline (gap fill), measured area unchanged"


def _clean(g):
    g = shapely.make_valid(g)
    ps = [q for q in getattr(g, "geoms", [g]) if q.geom_type in ("Polygon", "MultiPolygon")]
    ps = [x for q in ps for x in getattr(q, "geoms", [q])]
    g = shapely.union_all([q.buffer(0) for q in ps]).simplify(0.6, preserve_topology=True).buffer(0)   # 0.6 pt = about 50 mm: drawing accuracy
    return g


def _void_points(st: str):
    """Plan text that marks a void (VOID, OPEN TO BELOW, ATRIUM, COURT, LIGHTWELL) on the storey sheet, in page view."""
    import re
    import pymupdf
    from .. import common
    from .storeys import BG_PDF, storey_info
    page = pymupdf.open(str(common.project_root() / BG_PDF))[storey_info(st)["page0"]]
    R = page.rotation_matrix
    out = []
    for w in page.get_text("words"):
        if re.search(r"VOID|ATRIUM|COURT|LIGHTWELL|OPEN\s*TO", w[4], re.I):
            c = pymupdf.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2) * R
            out.append(shapely.Point(c.x, c.y))
    return out


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
    # an enclosed opening of the joined rooms is floor (a bay or open area with no walls of its own), unless the plan labels it a void, court or
    # atrium, or it is larger than 600 m2: those stay holes (Marcus 24 Nov 2026: B2 kitchen bays were left open)
    voids = _void_points(st)
    parts = []
    for g in getattr(plate, "geoms", [plate]):
        if g.geom_type != "Polygon":
            continue
        keep = [h for h in g.interiors if Polygon(h).area * k2 > 600.0 or any(Polygon(h).contains(v) for v in voids)]
        parts.append(Polygon(g.exterior, keep))
    plate = shapely.union_all(parts)
    gap = plate.difference(shapely.union_all(list(geo.values())).buffer(0.3))
    gap = gap.buffer(-0.4 * m).buffer(0.4 * m)
    # lift shafts are not covered by any system: never filled, and cut out of every outline drawn on the storey (D-099)
    from .lift_shafts import write as _lifts
    lifts = [shapely.make_valid(wkt.loads(x["polygon_pv_wkt"])) for x in _lifts(st, k2)]
    shaft = shapely.union_all(lifts).buffer(0.2) if lifts else Polygon()
    pieces = [g for g in getattr(gap, "geoms", [gap]) if g.geom_type == "Polygon" and 0.8 <= g.area * k2 <= 400.0
              and not (not shaft.is_empty and g.intersects(shaft) and g.intersection(shaft).area > 0.3 * g.area)]
    pieces = [g.difference(shaft) if not shaft.is_empty else g for g in pieces]
    pieces = [q for g in pieces for q in getattr(g, "geoms", [g]) if q.geom_type == "Polygon" and q.area * k2 >= 0.8]
    tree_ids = [rid for rid in geo if kind.get(rid) in ("AHU", "FCU", "CRAC")]
    added: Dict[str, List[Polygon]] = {}
    left = 0.0
    tagpts = {}
    for r in rows:
        if r.get("tag_box_pv") and kind.get(r["room_id"]) in ("AHU", "FCU", "CRAC") and r["room_id"] in geo:
            b = list(map(float, r["tag_box_pv"].split(",")))
            tagpts[r["room_id"]] = shapely.Point((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
    own_rows = []
    for (s_, pt, rid_, name_, cls_, basis_) in OWN_SPACES:
        if s_ != st:
            continue
        P = shapely.Point(*pt)
        hit = next((pc for pc in pieces if pc.buffer(0.5 * m).contains(P)), None)
        if hit is not None:
            pieces = [pc for pc in pieces if pc is not hit]
            own_rows.append((rid_, name_, cls_, basis_, hit))
    # plinths: open floor wholly inside the outer boundary of an AHU plant room is part of that plant room (only there may a plant room grow)
    plant = {}
    for rid, g in geo.items():
        if kind.get(rid) == "AHU spill":
            plant[rid] = shapely.union_all([Polygon(q.exterior) for q in getattr(g, "geoms", [g]) if q.geom_type == "Polygon"]).buffer(0.5 * m)
    rest_pieces = []
    for pc in pieces:
        host = next((rid for rid, hull in plant.items() if hull.contains(pc)), None)
        if host is not None:
            added.setdefault(host, []).append(pc)
        else:
            rest_pieces.append(pc)
    pieces_left = rest_pieces
    for pc in pieces_left:
        # a piece that holds the plan tag of a room is that room's open floor (e.g. the Plating Area tag in the B2 kitchen bay)
        own = [rid for rid, tp in tagpts.items() if pc.buffer(0.5 * m).contains(tp)]
        if own:
            added.setdefault(min(own, key=lambda rid: geo[rid].area), []).append(pc)
            continue
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
        g = _clean(shapely.union_all([geo[rid], fill.buffer(0.35, quad_segs=1, join_style="mitre")]))
        r["polygon_pv_wkt"] = shapely.to_wkt(g, rounding_precision=2)
        r["gap_fill_pv_wkt"] = shapely.to_wkt(fill, rounding_precision=2)
        if FLAG not in (r.get("flags") or ""):
            r["flags"] = ((r.get("flags") or "") + " | " + FLAG + f" ({fill.area * k2:.1f} m2)").strip(" |")
        n += 1
    M_ = _affine(rows)
    for rid_, name_, cls_, basis_, g_ in own_rows:
        row = next((r for r in rows if r["room_id"] == rid_), None)
        if row is None:
            row = {c: "" for c in cols}
            row.update(room_id=rid_, storey=st, room_tag=name_, room_name=name_, mode="", region_kind="open floor")
            rows.append(row)
        row["polygon_pv_wkt"] = shapely.to_wkt(_clean(g_), rounding_precision=2)
        row["polygon_building_mm_wkt"] = shapely.to_wkt(shapely.affinity.affine_transform(g_, [M_[0, 0], M_[1, 0], M_[0, 1], M_[1, 1], M_[2, 0], M_[2, 1]]), rounding_precision=0)
        row["a_area_m2"] = f"{g_.area * k2:.3f}"
        row["flags"] = "open floor taken as a space of its own (D-101): " + basis_
        row["measurement_confidence"] = "low: open floor with no room outline on the plan"
        zp = REG / f"zone_assignment_{st}.csv"
        zrows, zcols = _rd(zp)
        z = next((x for x in zrows if x["room_id"] == rid_), None)
        if z is None:
            z = {c: "" for c in zcols}
            zrows.append(z)
        z.update(room_id=rid_, storey=st, room_tag=name_, room_name=name_, mode_label="", **{"class": cls_}, class_kind=(cls_ if cls_ in ("NV", "MV") else "AHU"), confidence="0.5",
                 area_m2=f"{g_.area * k2:.3f}", department=("Food Services" if "OFOO" in cls_ else "Common areas"), sources=basis_[:80], method="D-101 open floor",
                 notes=basis_, basis="Equipment Schedule description", ruling="D-101")
        with zp.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=zcols)
            w.writeheader()
            for x in zrows:
                w.writerow({c: x.get(c, "") for c in zcols})
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
    if "zoning_building_mm_wkt" not in cols:
        cols.append("zoning_building_mm_wkt")
    for r in rows:                      # the drawn outline in building mm: the envelope allocates the facade line on it (D-102)
        if r.get("polygon_pv_wkt"):
            try:
                gz = _clean(wkt.loads(r["polygon_pv_wkt"]))
                r["zoning_building_mm_wkt"] = shapely.to_wkt(shapely.affinity.affine_transform(gz, [M_[0, 0], M_[1, 0], M_[0, 1], M_[1, 1], M_[2, 0], M_[2, 1]]), rounding_precision=0)
            except Exception:
                r["zoning_building_mm_wkt"] = ""
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
