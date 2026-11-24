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
# spaces with no room outline in the plan, drawn by inspection: row id -> (name, class, class kind, points pv, basis)
NEW = {"B1-G001": ("AHU deck", "AHU spill (plant room)", "AHU spill",
                   [(728, 1271), (1293, 1271), (1293, 1795), (793, 1795), (775, 1718), (881, 1712), (864, 1357), (734, 1357)],
                   "Marcus mark-up 24 Nov 2026: AHU plant room (AHU deck) at B1; conditioned by spill air of its AHUs (D-036)")}
# a measured outline that leaks out of its room: split it (row id -> keep box, [(new row id, name, class, basis, box or None = the rest)])
SPLITS = {
    "L04-R009": ([(1203, 380, 1482, 615), (1203, 380, 1535, 498)], [
        ("L04-G002", "Facade zone, ICU side", "NV", "assumed natural ventilation like the DI facade zone (same facade construction), to confirm", (1535, 380, 2075, 640)),
        ("L04-G001", "Facade zone, DI (MRI delivery route, AC ledge)", "NV", "Marcus 24 Nov 2026: the facade of the DI department is natural ventilation", None)]),
}
CLASS = {"L04-R014": ("AHU-B-04-04CORE-01", "Marcus 24 Nov 2026: Lounge and Int room to be on the Core AHU (the Equipment Schedule names AHU-B-04-04DI-10 "
                                             "'L4 DI - Interviewer Lounge Caregiver' for this space; Marcus decision governs)")}
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


def _rows(st):
    path = REG / f"room_polygons_{st}.csv"
    with path.open(newline="", encoding="utf-8") as fh:
        rd = csv.DictReader(fh)
        return path, list(rd), list(rd.fieldnames)


def _write(path, rows, cols):
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def splits_and_plinths():
    """Split leaking outlines (SPLITS), set classes (CLASS), fill the plinth holes of every AHU plant room (D-101)."""
    from .open_region import _affine
    out = []
    for st in ["LB3", "B3", "B2", "B1", "L01", "L02", "L03", "L04", "L05", "L06", "L07"]:
        path, rows, cols = _rows(st)
        if "polygon_pv_orig_wkt" not in cols:
            cols.append("polygon_pv_orig_wkt")
        zp = REG / f"zone_assignment_{st}.csv"
        with zp.open(newline="", encoding="utf-8") as fh:
            rd = csv.DictReader(fh)
            zr, zc = list(rd), list(rd.fieldnames)
        zby = {z["room_id"]: z for z in zr}
        k2 = [float(r["a_area_m2"]) / wkt.loads(r["polygon_pv_wkt"]).area for r in rows
              if r["polygon_pv_wkt"] and r["a_area_m2"] and "-T" not in r["room_id"] and wkt.loads(r["polygon_pv_wkt"]).area > 0]
        k2 = sorted(k2)[len(k2) // 2]
        M = _affine(rows)
        mm = lambda g: shapely.to_wkt(shapely.affinity.affine_transform(g, [M[0, 0], M[1, 0], M[0, 1], M[1, 1], M[2, 0], M[2, 1]]), rounding_precision=0)
        by = {r["room_id"]: r for r in rows}
        for rid, (keep, parts) in SPLITS.items():
            if not rid.startswith(st) or rid not in by:
                continue
            r = by[rid]
            if not r.get("polygon_pv_orig_wkt"):
                g0 = shapely.make_valid(wkt.loads(r["polygon_pv_wkt"])).buffer(0)
                if r.get("gap_fill_pv_wkt"):
                    g0 = g0.difference(wkt.loads(r["gap_fill_pv_wkt"]).buffer(0.4)).buffer(0)
                r["polygon_pv_orig_wkt"] = shapely.to_wkt(g0, rounding_precision=2)
            g0 = wkt.loads(r["polygon_pv_orig_wkt"])
            kb = shapely.union_all([shapely.box(*b) for b in (keep if isinstance(keep, list) else [keep])])
            kept = g0.intersection(kb)
            rest = g0.difference(kb)
            r["polygon_pv_wkt"] = shapely.to_wkt(kept, rounding_precision=2)
            r["polygon_building_mm_wkt"] = mm(kept)
            r["a_area_m2"] = f"{kept.area * k2:.3f}"
            r["gap_fill_pv_wkt"] = ""
            r["flags"] = ((r.get("flags") or "").split(" | outline split")[0] + " | outline split: the measured face leaked along the open facade zone (D-101)").strip(" |")
            if rid in zby:
                zby[rid]["area_m2"] = r["a_area_m2"]
            for nid, name, cls, basis, bx in parts:
                g = rest.intersection(shapely.box(*bx)) if bx else rest
                rest = rest.difference(shapely.box(*bx)) if bx else Polygon()
                g = shapely.make_valid(g).buffer(0)
                if g.is_empty:
                    continue
                n = by.get(nid)
                if n is None:
                    n = {c: "" for c in cols}
                    n.update(room_id=nid, storey=st, room_tag=name, room_name=name, region_kind="facade zone")
                    rows.append(n)
                    by[nid] = n
                n["polygon_pv_wkt"] = shapely.to_wkt(g, rounding_precision=2)
                n["polygon_building_mm_wkt"] = mm(g)
                n["a_area_m2"] = f"{g.area * k2:.3f}"
                n["flags"] = f"split from {rid} (D-101): {basis}"
                z = zby.get(nid)
                if z is None:
                    z = {c: "" for c in zc}
                    zr.append(z)
                    zby[nid] = z
                z.update(room_id=nid, storey=st, room_tag=name, room_name=name, **{"class": cls}, class_kind=cls, confidence="0.6",
                         area_m2=n["a_area_m2"], department="Facade", method="D-101 split", notes=basis, basis="Marcus mark-up", ruling="D-101")
                out.append((nid, cls, round(g.area * k2, 1)))
        for rid, (cls, why) in CLASS.items():
            if rid in zby and rid.startswith(st):
                z = zby[rid]
                if not z.get("class_before_ruling"):
                    z["class_before_ruling"] = z["class"]
                z["class"], z["class_kind"], z["ruling"] = cls, "AHU", "D-101"
                z["notes"] = ((z.get("notes") or "") + " " + why).strip()
                out.append((rid, cls))
        for nid, (name, cls, kind, pts, basis) in NEW.items():
            if not nid.startswith(st):
                continue
            others = shapely.union_all([shapely.make_valid(wkt.loads(r["polygon_pv_wkt"])).buffer(0) for r in rows
                                        if r["polygon_pv_wkt"] and r["room_id"] != nid and "-T" not in r["room_id"] and "-G" not in r["room_id"]
                                        and "plinths filled" not in (r.get("flags") or "")
                                        and shapely.make_valid(wkt.loads(r["polygon_pv_wkt"])).intersects(Polygon(pts))])
            g = Polygon(pts).difference(others.buffer(0.2)) if not others.is_empty else Polygon(pts)
            g = shapely.make_valid(g).buffer(0)
            n = by.get(nid)
            if n is None:
                n = {c: "" for c in cols}
                n.update(room_id=nid, storey=st, room_tag=name, room_name=name, region_kind="drawn space")
                rows.append(n)
                by[nid] = n
            n["polygon_pv_wkt"] = shapely.to_wkt(g, rounding_precision=2)
            n["polygon_building_mm_wkt"] = mm(g)
            n["a_area_m2"] = f"{g.area * k2:.3f}"
            n["flags"] = "drawn by inspection (D-101): " + basis
            z = zby.get(nid)
            if z is None:
                z = {c: "" for c in zc}
                zr.append(z)
                zby[nid] = z
            z.update(room_id=nid, storey=st, room_tag=name, room_name=name, **{"class": cls}, class_kind=kind, confidence="0.6",
                     area_m2=n["a_area_m2"], department="MEP plant", method="D-101 drawn", notes=basis, basis="Marcus mark-up", ruling="D-101")
            out.append((nid, cls, round(g.area * k2, 1)))
        # plinths: an AHU plant room has no holes (equipment plinths are part of the room)
        nf = 0
        for r in rows:
            z = zby.get(r["room_id"])
            if z and z.get("class_kind") == "AHU spill" and r["polygon_pv_wkt"]:
                g = shapely.make_valid(wkt.loads(r["polygon_pv_wkt"])).buffer(0)
                parts_ = [q for q in getattr(g, "geoms", [g]) if q.geom_type == "Polygon"]
                if any(q.interiors for q in parts_):
                    g2 = shapely.union_all([Polygon(q.exterior) for q in parts_])
                    r["polygon_pv_wkt"] = shapely.to_wkt(g2, rounding_precision=2)
                    if "plinths filled" not in (r.get("flags") or ""):
                        r["flags"] = ((r.get("flags") or "") + " | plinths filled (part of the plant room, D-101)").strip(" |")
                    nf += 1
        if nf:
            out.append((st, "AHU plant rooms with plinths filled", nf))
        _write(path, rows, cols)
        with zp.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=zc)
            w.writeheader()
            for z in zr:
                w.writerow({c: z.get(c, "") for c in zc})
    return out


if __name__ == "__main__":
    print(run())
    print(splits_and_plinths())
