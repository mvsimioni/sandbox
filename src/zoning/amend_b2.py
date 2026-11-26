"""B2 zoning amendments from Marcus's mark-ups of 26 Nov 2026 (D-112). Runs on out/registers/room_polygons_B2.csv after the
storey pipeline and before gap_fill (gap fills are taken off first; gap_fill adds them again).

1. Inpatient Pharmacy hall (B2-R051, one open room split into 14 plan tags, all on AHU-B-B1-B2PHAB-01A/B): the tag split failed (tag-size
   pieces; 'Corr' and 'Trol Bay' took regions elsewhere on the plan). The hall is drawn as one outline, the wall-face region of B2-R051 less the
   other rooms and the voids, on the tag 'Manual Picking' (B2-R051-T08); the other tags of the hall are not drawn (their rooms stay on the
   unit in the zone schedule and the workbooks).
2. Bin Centre (B2-R132): its outline is open to the south row of lorry bays; the bays are cut off it.
3. Loading dock: the lorry bays (north and south rows) are open floor of the loading / unloading area (B2-R109, MV) and join it; the outdoor
   corner by the landscape feature (north-east of the dock) is not floor.
After gap_fill (``post``), from the mark-ups and answers of 26 Nov 2026:
4. Service corridor with the 1:25 ramp, the In-house Laundry (HOSP 01.B2.01, no room outline) and the open floor east of the toilets:
   one area on AHU-B-B1-B2COR-01 (drawn on B2-R063 SVC), no holes.
5. Ramp B2-R049: not zoned (vehicle ramp), not drawn.
6. Lobes over the walls removed: pharmacy hall (opening of 1.2 m); B2-R065 SVC and B2-R097 Pantry back to their wall-face regions.
7. The open strip between the FLL / IT closet and the pharmacy hall is the MV service corridor (B2-R111).
Order: run() -> python3 -m src.zoning.gap_fill -> post().
"""
from __future__ import annotations

import csv
import shutil
from pathlib import Path

import numpy as np
import shapely
from shapely import wkt
from shapely.geometry import Polygon, box

csv.field_size_limit(1 << 30)
ROOT = Path(__file__).resolve().parent.parent.parent
REG = ROOT / "out/registers"
FLAG = "B2 mark-up 26 Nov 2026 (D-112)"
BAY_ROW_S = (1762.0, 1074.0, 2190.0, 1172.0)       # pv box of the south lorry-bay row (between the dock and the Bin Centre)
DOCK_BOX = (1730.0, 785.0, 2405.0, 1176.0)         # pv box of the loading / unloading area with both bay rows
DOCK_OUT = (2200.0, 760.0, 2420.0, 843.0)          # outdoor corner by the landscape feature, north-east of the dock (not floor)
STRIP_BOX = (722.0, 515.0, 770.0, 790.0)           # open strip between the FLL / IT closet and the pharmacy hall: MV service corridor (B2-R111)
SVC_BOX = (1612.0, 488.0, 2062.0, 706.0)           # service corridor with the 1:25 ramp, In-house Laundry, open floor east of the toilets


def _g(s):
    return shapely.make_valid(wkt.loads(s)) if s else Polygon()


def _poly(g):
    ps = [q for q in getattr(g, "geoms", [g]) if q.geom_type == "Polygon" and q.area > 1.0]
    return shapely.union_all(ps).buffer(0) if ps else Polygon()


def run(dry: bool = False) -> dict:
    p = REG / "room_polygons_B2.csv"
    with p.open(newline="", encoding="utf-8") as fh:
        rd = csv.DictReader(fh)
        cols, rows = list(rd.fieldnames), list(rd)
    if not dry:
        bak = ROOT / "out/work/room_polygons_B2_before_D112.csv"
        bak.parent.mkdir(parents=True, exist_ok=True)
        if not bak.exists():
            shutil.copy(p, bak)
    by = {r["room_id"]: r for r in rows}
    for r in rows:                                  # take the gap fills off (gap_fill adds them again)
        if r.get("gap_fill_pv_wkt") and r.get("polygon_pv_wkt"):
            r["polygon_pv_wkt"] = shapely.to_wkt(_poly(_g(r["polygon_pv_wkt"]).difference(_g(r["gap_fill_pv_wkt"]).buffer(0.01))), rounding_precision=2)
            r["gap_fill_pv_wkt"] = ""
    k2 = float(np.median([float(r["a_area_m2"]) / _g(r["polygon_pv_wkt"]).area for r in rows
                          if r.get("a_area_m2") and r.get("polygon_pv_wkt") and "-T" not in r["room_id"] and _g(r["polygon_pv_wkt"]).area > 0]))
    log = {}
    # 1 pharmacy hall
    wk = {r["room_id"]: r for r in csv.DictReader((REG / "room_polygons_B2_wkt.csv").open(encoding="utf-8"))}
    hall = _g(wk["B2-R051"]["wall_face_wkt_pv"])
    others = shapely.union_all([_g(r["polygon_pv_wkt"]) for r in rows if r.get("polygon_pv_wkt") and not r["room_id"].startswith("B2-R051")
                                and "VOID" not in (r.get("room_name") or "").upper()])
    voids = shapely.union_all([_g(r["polygon_pv_wkt"]).buffer(1.0) for r in rows if "VOID" in (r.get("room_name") or "").upper() or
                               (r.get("room_name") or "").upper() == "RAMP"])
    hall = _poly(hall.difference(others.buffer(0.3)).difference(voids))
    for rid, r in by.items():
        if rid.startswith("B2-R051-T"):
            if rid == "B2-R051-T08":
                r["polygon_pv_wkt"] = shapely.to_wkt(hall, rounding_precision=2)
                r["a_area_m2"] = f"{hall.area * k2:.3f}"
                r["flags"] = ((r.get("flags") or "") + f" | {FLAG}: outline of the whole Inpatient Pharmacy hall (B2-R051 wall-face region)").strip(" |")
            else:
                r["polygon_pv_wkt"] = ""
                r["flags"] = ((r.get("flags") or "") + f" | {FLAG}: part of the Inpatient Pharmacy hall, drawn on B2-R051-T08").strip(" |")
    log["hall_m2"] = round(hall.area * k2, 1)
    # 2 bin centre
    b = by["B2-R132"]
    g0 = _g(b["polygon_pv_wkt"])
    g1 = _poly(g0.difference(box(*BAY_ROW_S)))
    b["polygon_pv_wkt"] = shapely.to_wkt(g1, rounding_precision=2)
    b["flags"] = ((b.get("flags") or "") + f" | {FLAG}: south lorry-bay row cut off (it is loading dock)").strip(" |")
    log["bin_m2"] = (round(g0.area * k2, 1), round(g1.area * k2, 1))
    # 3 loading dock: the open bays inside the dock box join B2-R109
    d = by["B2-R109"]
    gd = _g(d["polygon_pv_wkt"])
    rest = shapely.union_all([_g(r["polygon_pv_wkt"]) for r in rows if r.get("polygon_pv_wkt") and r["room_id"] != "B2-R109"])
    from .lift_shafts import write as _lifts
    lifts = [shapely.make_valid(wkt.loads(x["polygon_pv_wkt"])) for x in _lifts("B2", k2)]
    shaft = shapely.union_all(lifts).buffer(0.2) if lifts else Polygon()
    bays = box(*DOCK_BOX).difference(gd).difference(rest.buffer(0.3)).difference(shaft).difference(box(*DOCK_OUT))
    bays = bays.buffer(-0.4 / k2 ** 0.5).buffer(0.4 / k2 ** 0.5)
    pcs = [q for q in getattr(bays, "geoms", [bays]) if q.geom_type == "Polygon" and q.area * k2 >= 4.0]
    gd1 = _poly(shapely.union_all([gd] + [q.buffer(0.35, join_style="mitre") for q in pcs]))
    d["polygon_pv_wkt"] = shapely.to_wkt(gd1, rounding_precision=2)
    d["flags"] = ((d.get("flags") or "") + f" | {FLAG}: lorry bays (open floor of the dock) joined").strip(" |")
    log["dock_bays"] = (len(pcs), round(sum(q.area for q in pcs) * k2, 1))
    if not dry:
        with p.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
    return log


def post(dry: bool = False) -> dict:
    p = REG / "room_polygons_B2.csv"
    with p.open(newline="", encoding="utf-8") as fh:
        rd = csv.DictReader(fh)
        cols, rows = list(rd.fieldnames), list(rd)
    by = {r["room_id"]: r for r in rows}
    k2 = float(np.median([float(r["a_area_m2"]) / _g(r["polygon_pv_wkt"]).area for r in rows
                          if r.get("a_area_m2") and r.get("polygon_pv_wkt") and "-T" not in r["room_id"] and _g(r["polygon_pv_wkt"]).area > 0]))
    m = 1.0 / k2 ** 0.5
    wk = {r["room_id"]: r for r in csv.DictReader((REG / "room_polygons_B2_wkt.csv").open(encoding="utf-8"))}
    log = {}
    # 6 lobes over the walls
    for rid in ("B2-R065", "B2-R051-T08"):
        g = _g(by[rid]["polygon_pv_wkt"])
        if rid == "B2-R065":                       # narrow room: back to its wall-face region
            g1 = _poly(g.intersection(_g(wk[rid]["wall_face_wkt_pv"]).buffer(0.3 * m, join_style="mitre")))
        else:
            g1 = _poly(g.buffer(-1.2 * m, join_style="mitre").buffer(1.2 * m, join_style="mitre"))
        by[rid]["polygon_pv_wkt"] = shapely.to_wkt(g1, rounding_precision=2)
        by[rid]["flags"] = ((by[rid].get("flags") or "") + f" | {FLAG}: lobe over the wall removed").strip(" |")
        log[rid] = round((g.area - g1.area) * k2, 1)
    r97 = by["B2-R097"]
    g = _g(r97["polygon_pv_wkt"])
    g1 = _poly(g.intersection(_g(wk["B2-R097"]["wall_face_wkt_pv"]).buffer(0.5 * m, join_style="mitre")))
    r97["polygon_pv_wkt"], r97["gap_fill_pv_wkt"] = shapely.to_wkt(g1, rounding_precision=2), ""
    r97["flags"] = ((r97.get("flags") or "") + f" | {FLAG}: outline back to its walls (no corridor fill)").strip(" |")
    log["B2-R097"] = round((g.area - g1.area) * k2, 1)
    # 5 ramp
    by["B2-R049"]["polygon_pv_wkt"] = ""
    by["B2-R049"]["flags"] = ((by["B2-R049"].get("flags") or "") + f" | {FLAG}: vehicle ramp, not zoned").strip(" |")
    # 4 service corridor, laundry and open floor east of the toilets: one COR-01 area on B2-R063
    rest = shapely.union_all([_g(r["polygon_pv_wkt"]) for r in rows if r.get("polygon_pv_wkt") and r["room_id"] != "B2-R063"
                              and not (r.get("room_id") == "B2-R075")])
    from .lift_shafts import write as _lifts
    lifts = [shapely.make_valid(wkt.loads(x["polygon_pv_wkt"])) for x in _lifts("B2", k2)]
    shaft = shapely.union_all(lifts).buffer(0.2) if lifts else Polygon()
    r63 = by["B2-R063"]
    g63 = _g(r63["polygon_pv_wkt"])
    # the plate inside the corridor box: what the rooms around it do not cover (walls under 0.8 m stay out)
    opn = box(*SVC_BOX).difference(rest.buffer(0.3)).difference(shaft)
    opn = opn.buffer(-0.4 * m).buffer(0.4 * m)
    pcs = [q for q in getattr(opn, "geoms", [opn]) if q.geom_type == "Polygon" and q.area * k2 >= 2.0]
    g63b = _poly(shapely.union_all([g63] + [q.buffer(0.35, join_style="mitre") for q in pcs]))
    r63["polygon_pv_wkt"] = shapely.to_wkt(g63b, rounding_precision=2)
    r63["flags"] = ((r63.get("flags") or "") + f" | {FLAG}: service corridor with the 1:25 ramp, In-house Laundry (no outline) and the open floor "
                    "east of the toilets drawn on this room (AHU-B-B1-B2COR-01)").strip(" |")
    log["B2-R063_added_m2"] = round((g63b.area - g63.area) * k2, 1)
    # 7 strip to the MV service corridor
    r11 = by["B2-R111"]
    g11 = _g(r11["polygon_pv_wkt"])
    rest2 = shapely.union_all([_g(r["polygon_pv_wkt"]) for r in rows if r.get("polygon_pv_wkt") and r["room_id"] != "B2-R111"])
    st = box(*STRIP_BOX).difference(rest2.buffer(0.3)).difference(shaft).buffer(-0.4 * m).buffer(0.4 * m)
    sp = [q for q in getattr(st, "geoms", [st]) if q.geom_type == "Polygon" and q.area * k2 >= 2.0]
    g11b = _poly(shapely.union_all([g11] + [q.buffer(0.35, join_style="mitre") for q in sp]))
    r11["polygon_pv_wkt"] = shapely.to_wkt(g11b, rounding_precision=2)
    r11["flags"] = ((r11.get("flags") or "") + f" | {FLAG}: open strip between the FLL / IT closet and the pharmacy hall joined (MV service corridor)").strip(" |")
    log["B2-R111_added_m2"] = round((g11b.area - g11.area) * k2, 1)
    # building-mm outline of the changed rows (the envelope uses it)
    from .open_region import _affine
    M_ = _affine(rows)
    import shapely.affinity as _af
    for rid in ("B2-R065", "B2-R051-T08", "B2-R097", "B2-R049", "B2-R063", "B2-R111"):
        r = by[rid]
        r["zoning_building_mm_wkt"] = (shapely.to_wkt(_af.affine_transform(_g(r["polygon_pv_wkt"]), [M_[0, 0], M_[1, 0], M_[0, 1], M_[1, 1], M_[2, 0], M_[2, 1]]),
                                                     rounding_precision=0) if r["polygon_pv_wkt"] else "")
    if not dry:
        with p.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
    return log


if __name__ == "__main__":
    import sys
    print(post() if "--post" in sys.argv else run())
