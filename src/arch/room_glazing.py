"""Glazing and facade of each plan room from the elevations (D-120, forensic envelope review, 26 Nov 2026).

For each storey: the floor plate = the zoning outlines of the storey (out/registers/room_polygons_<ST>.csv) joined across gaps of up to 3 m.
Its outer boundary is the external facade; holes of 200 m2 or more are courts, whose walls are on the internal elevations. Each boundary is
walked in 100 mm steps; a step goes to the nearest room outline within 300 mm (else within 3 m, as D-102). Its outward normal gives the sector
(N, NE, ...; grid bearing as envelope.py) and the elevation that shows it: the main elevation whose facing is nearest to the normal
(external boundary) or the internal elevation (court). The glass of the step = glass per metre of that elevation at that storey and that axis
position (src.arch.elev_glazing profiles) x the length of the step projected on the elevation. Where two steps of the same storey face the same
elevation at the same axis position, the front one (nearest the viewer) takes the glass; the step behind is hidden on that elevation and is
flagged (no glass read for it).

Output: out/registers/room_glazing_<ST>.csv per plan room and sector (facade length, glass m2 from the elevations, hidden length) and
out/registers/room_glazing_steps.csv (one row per step, for the review images).
"""
from __future__ import annotations

import csv
import math
import re
import pickle
from collections import defaultdict
from pathlib import Path

import numpy as np
import shapely
from shapely import wkt
from shapely.geometry import Point

ROOT = Path(__file__).resolve().parent.parent.parent
REG = ROOT / "out/registers"
csv.field_size_limit(1 << 30)
FFL = {"LB3": 0.0, "B3": 3.8, "B2": 8.3, "B1": 12.8, "L01": 17.0, "L02": 23.0, "L03": 29.0, "L04": 35.0, "L05": 41.0, "L06": 47.0,
       "L07": 53.0, "L08": 59.0}
STOREYS = ["B1", "L01", "L02", "L03", "L04", "L05", "L06", "L07"]
SECTORS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
GRID_BEARING_DEG = 359.77
CLOSE_MM, STEP_MM, TOL_MM, REACH_MM, COURT_M2 = 1500.0, 100.0, 300.0, 3000.0, 200.0
FRONT_TOL_MM = 1000.0
# facing (unit outward normal in building axes, +Y = north) of each elevation; internal elevations named as the external ones (to verify)
EXT = {"NORTH ELEVATION (IP)": (0, 1), "SOUTH ELEVATION (IP)": (0, -1), "EAST ELEVATION (IP)": (1, 0), "WEST ELEVATION (IP)": (-1, 0)}
INT = {"NORTH INTERNAL ELEVATION (IP)": (0, 1), "SOUTH INTERNAL ELEVATION (IP)": (0, -1), "EAST INTERNAL ELEVATION (IP)": (1, 0),
       "WEST INTERNAL ELEVATION (IP)": (-1, 0)}


PASS_NAMES = re.compile(r"\b(terr|terrace|bus stop|outdoor|covered|walkway|wc bay|rain garden|link ?bridge|spg|drop.?off|green roof|planter)\b", re.I)
PROBE_MM = (1500.0, 2000.0, 1000.0, 2500.0, 600.0, 3000.0)   # 1.5 m first: a wall-zone strip of a neighbour outline does not take the facade
PASS_REACH_MM = 30000.0


def _pass_through(cls, name):
    """Open or naturally ventilated space in front of the envelope (terrace, covered area, NV corridor): the glass seen on the elevation there
    is the wall of the conditioned room behind it (forensic review D-120, round 1)."""
    c = (cls or "").lower()
    return c == "nv" or "external" in c or (c in ("", "no information", "not conditioned") and bool(PASS_NAMES.search(name or "")))


def _classes(st):
    p = REG / f"zone_assignment_{st}.csv"
    return {z["room_id"]: z.get("class", "") for z in csv.DictReader(p.open(encoding="utf-8"))} if p.exists() else {}


OVERRIDES = Path(__file__).with_name("envelope_overrides_d120.json")


def _overrides(by_st):
    """Corrections of the forensic review (D-120) that no drawing rule can find: outline-only glazing, link-bridge glass, double-height
    spaces read in the upper band, panes mis-read as hatched. Each is applied to the facade steps of its elevation, storey and axis range."""
    import json
    if not OVERRIDES.exists():
        return []
    unplaced = []

    def rng(st, el, a, vis=True, only=None):
        return [s for s in by_st.get(st, []) if s["elevation"] == el and a[0] <= s["axis_mm"] <= a[1] and (not vis or not s["hidden"])
                and (only is None or s["room"] in only)]

    def spread(ss, area, tag):
        L = sum(s["len_m"] for s in ss)
        for s in ss:
            s["glass_m2"] += area * s["len_m"] / L
            s["override"] = (s.get("override", "") + " " + tag).strip()

    for it in json.loads(OVERRIDES.read_text(encoding="utf-8"))["items"]:
        el, st, a = it["elevation"], it["storey"], it["axis_mm"]
        ss = rng(st, el, a, only=set(it["only_rooms"]) if "only_rooms" in it else None)
        if it["action"] == "below_ground":
            for s in rng(st, el, a, vis=False):
                s["glass_m2"], s["slat_m2"], s["ground"] = 0.0, 0.0, True
                s["override"] = (s.get("override", "") + " " + it["id"]).strip()
        elif it["action"] == "exclude":
            for s in rng(st, el, a, vis=False, only=set(it["only_rooms"]) if "only_rooms" in it else None):
                s["glass_m2"] = 0.0
                s["override"] = (s.get("override", "") + " " + it["id"]).strip()
        elif it["action"] == "add":
            if ss:
                spread(ss, it["area_m2"], it["id"])
            else:
                unplaced.append(it["id"])
        elif it["action"] == "assign":
            rid = it["room_id"]
            nm = next((n for r_, n, _ in _rooms(st) if r_ == rid), "")
            for s in ss:
                s["room"], s["room_name"] = rid, nm or s["room_name"]
                s["override"] = (s.get("override", "") + " " + it["id"]).strip()
        elif it["action"] == "to_storey":
            g = sum(s["glass_m2"] for s in ss)
            to = set(it["to_rooms"]) if "to_rooms" in it else None
            tgt = rng(it["to_storey"], el, a, only=to) or rng(it["to_storey"], el, a, vis=False, only=to)
            if g and tgt:
                for s in ss:
                    s["glass_m2"] = 0.0
                    s["override"] = (s.get("override", "") + " " + it["id"]).strip()
                spread(tgt, g, it["id"])
            else:
                unplaced.append(it["id"])
        elif it["action"] == "add_room":
            # glass of a wall that is behind another part of the building on the elevation but seen on it (e.g. across an open forecourt)
            rid = it["room_id"]
            nm = next((n for r_, n, _ in _rooms(st) if r_ == rid), "")
            by_st.setdefault(st, []).append(dict(storey=st, kind="external", elevation=el, room=rid, room_name=nm, sector=it["sector"], x=0, y=0,
                                                 axis_mm=a[0], depth=0.0, len_m=0.0, proj_m=0.0, cosf=1.0, behind_open="", nx=0.0, ny=0.0,
                                                 hidden=False, glass_m2=it["area_m2"], slat_m2=0.0, on_elevation=True, override=it["id"]))
    return unplaced


def _rooms(st):
    out = []
    for r in csv.DictReader((REG / f"room_polygons_{st}.csv").open(encoding="utf-8")):
        s = r.get("zoning_building_mm_wkt") or r.get("polygon_building_mm_wkt")
        if not s:
            continue
        try:
            g = wkt.loads(s).buffer(0)
        except Exception:
            continue
        if not g.is_empty:
            out.append((r["room_id"], r.get("room_name", ""), g))
    return out


def _glass(prof, x_mm):
    b = prof["bins"]
    i = int((x_mm - b[0]) // (b[1] - b[0]))
    return float(prof["glass_m2_per_m"][i]) if 0 <= i < len(prof["glass_m2_per_m"]) else 0.0


def run():
    profiles = pickle.loads((REG / "elev_glazing_profiles.pkl").read_bytes())
    slats = pickle.loads((REG / "elev_slat_profiles.pkl").read_bytes())
    steps_all, by_st = [], {}
    for st in STOREYS:
        rooms = _rooms(st)
        cls = _classes(st)
        if not rooms:
            continue
        geoms = [g.buffer(-250).buffer(250) or g for _, _, g in rooms]
        geoms = [g if not g.is_empty else rooms[i][2] for i, g in enumerate(geoms)]
        tree = shapely.STRtree(geoms)
        tree0 = shapely.STRtree([g for _, _, g in rooms])
        plate = shapely.union_all([g.buffer(CLOSE_MM) for _, _, g in rooms]).buffer(-CLOSE_MM)
        rings = []
        for poly in getattr(plate, "geoms", [plate]):
            if poly.geom_type != "Polygon" or poly.area < 50e6:
                continue
            rings.append(("external", poly.exterior, poly))
            for h in poly.interiors:
                if shapely.Polygon(h).area >= COURT_M2 * 1e6:
                    rings.append(("court", h, poly))
        steps = []
        for kind, ring, poly in rings:
            pts = list(ring.coords)
            for a, b in zip(pts[:-1], pts[1:]):
                L = math.hypot(b[0] - a[0], b[1] - a[1])
                if L < 1.0:
                    continue
                ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
                nx, ny = uy, -ux
                mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
                if poly.contains(Point(mx + nx * 200, my + ny * 200)):
                    nx, ny = -nx, -ny
                az = (math.degrees(math.atan2(nx, ny)) + GRID_BEARING_DEG) % 360.0
                sector = SECTORS[int(((az + 22.5) % 360.0) // 45.0)]
                elevs = EXT if kind == "external" else INT
                ename, (fx, fy) = max(elevs.items(), key=lambda kv: nx * kv[1][0] + ny * kv[1][1])
                cosf = nx * fx + ny * fy
                n = max(1, int(round(L / STEP_MM)))
                for i in range(n):
                    t = (i + 0.5) / n
                    px, py = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
                    p = Point(px, py)
                    best, bk, behind = None, None, ""
                    # inward probe: the room that contains the point 0.6-2 m inside the facade (a thin wall-zone strip of a neighbour
                    # polygon does not take the facade); through open / NV spaces, on to the first conditioned room behind
                    for dd in PROBE_MM:
                        q = Point(px - nx * dd, py - ny * dd)
                        hit = [int(j) for j in tree0.query(q) if rooms[int(j)][2].contains(q)]
                        if hit:
                            best = min(hit, key=lambda j: rooms[j][2].area)
                            break
                    for tol in (() if best is not None else (TOL_MM, REACH_MM)):
                        for j in tree.query(p.buffer(tol)):
                            d = geoms[int(j)].distance(p)
                            if d <= tol:
                                k = (round(d / 50.0), geoms[int(j)].area)
                                if bk is None or k < bk:
                                    best, bk = int(j), k
                        if best is not None:
                            break
                    if best is not None and _pass_through(cls.get(rooms[best][0]), rooms[best][1]):
                        # through the open space along the line of sight of the elevation (not the normal of an oblique terrace edge)
                        dd = max(PROBE_MM)
                        while dd < PASS_REACH_MM:
                            dd += 500.0
                            q = Point(px - fx * dd, py - fy * dd)
                            hit = [int(j) for j in tree0.query(q) if rooms[int(j)][2].contains(q)]
                            hit = [j for j in hit if not _pass_through(cls.get(rooms[j][0]), rooms[j][1])]
                            if hit:
                                behind = rooms[best][0]
                                best = min(hit, key=lambda j: rooms[j][2].area)
                                break
                    axis_pos = px if fx == 0 else py
                    depth = -(px * fx + py * fy)            # smaller = nearer the viewer of that elevation
                    steps.append(dict(storey=st, kind=kind, elevation=ename, room=rooms[best][0] if best is not None else "",
                                      room_name=rooms[best][1] if best is not None else "", sector=sector, x=round(px), y=round(py),
                                      axis_mm=axis_pos, depth=depth, len_m=L / n / 1000.0, proj_m=L / n / 1000.0 * abs(cosf), cosf=cosf,
                                      behind_open=behind, nx=nx, ny=ny))
        # front step per elevation and axis bin
        # every wall of the storey that faces an elevation can hide what is behind it on that elevation, whichever elevation it is read on
        # (a 45 degree chamfer read on the East elevation still hides the notch behind it on the North one; D-120 round 2)
        front = {}
        for s in steps:
            for ename, (fx, fy) in (EXT if s["kind"] == "external" else INT).items():
                if s["nx"] * fx + s["ny"] * fy <= 0.2:
                    continue
                ax_ = s["x"] if fx == 0 else s["y"]
                dep = -(s["x"] * fx + s["y"] * fy)
                k = (ename, int(ax_ // 100))
                if k not in front or dep < front[k]:
                    front[k] = dep
        for s in steps:
            prof = profiles.get((s["elevation"], st))
            hidden = s["depth"] > front.get((s["elevation"], int(s["axis_mm"] // 100)), s["depth"]) + FRONT_TOL_MM
            s["hidden"] = hidden
            w_ = s["proj_m"] if s.get("behind_open") else s["len_m"]
            s["glass_m2"] = 0.0 if (hidden or prof is None) else _glass(prof, s["axis_mm"]) * w_
            sp = slats.get((s["elevation"], st))
            s["slat_m2"] = 0.0 if (hidden or sp is None) else _glass(dict(bins=sp["bins"], glass_m2_per_m=sp["slat_m2_per_m"]), s["axis_mm"]) * s["len_m"]
            s["on_elevation"] = prof is not None
        by_st[st] = steps
    unplaced = _overrides(by_st)
    for st, steps in by_st.items():
        steps_all += steps
        agg = defaultdict(lambda: dict(facade_m=0.0, glass_m2=0.0, slat_m2=0.0, hidden_m=0.0, court_m=0.0, ground_m=0.0, elevations=set()))
        for s in steps:
            if not s["room"]:
                continue
            a_ = agg[(s["room"], s["room_name"], s["sector"])]
            if s.get("ground"):
                a_["ground_m"] += s["len_m"]
                continue
            a_["facade_m"] += s["len_m"]
            a_["glass_m2"] += s["glass_m2"]
            a_["slat_m2"] += s["slat_m2"]
            a_["hidden_m"] += s["len_m"] if s["hidden"] else 0.0
            a_["court_m"] += s["len_m"] if s["kind"] == "court" else 0.0
            a_["elevations"].add(s["elevation"])
        h = FFL[STOREYS[STOREYS.index(st) + 1]] - FFL[st] if st != "L07" else FFL["L08"] - FFL["L07"]
        with (REG / f"room_glazing_{st}.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["storey", "room_id", "room_name", "sector", "facade_m", "storey_h_m", "facade_area_m2", "glass_m2", "slatted_m2", "glass_ratio",
                        "hidden_m", "court_m", "below_ground_m", "elevations"])
            for (rid, nm, sec), v in sorted(agg.items()):
                fa = v["facade_m"] * h
                w.writerow([st, rid, nm, sec, round(v["facade_m"], 2), h, round(fa, 1), round(v["glass_m2"], 2), round(v["slat_m2"], 2),
                            round(v["glass_m2"] / fa, 3) if fa else 0, round(v["hidden_m"], 2), round(v["court_m"], 2), round(v["ground_m"], 2),
                            "; ".join(sorted(v["elevations"]))])
    with (REG / "room_glazing_steps.csv").open("w", newline="", encoding="utf-8") as fh:
        keys = ["storey", "kind", "elevation", "room", "room_name", "sector", "x", "y", "axis_mm", "len_m", "proj_m", "glass_m2", "slat_m2", "hidden", "behind_open", "override", "ground"]
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(steps_all)
    if unplaced:
        print("overrides with no facade step in range:", unplaced)
    return len(steps_all)


if __name__ == "__main__":
    print(run(), "steps")
