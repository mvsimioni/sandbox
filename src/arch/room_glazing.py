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
    steps_all = []
    for st in STOREYS:
        rooms = _rooms(st)
        if not rooms:
            continue
        geoms = [g.buffer(-250).buffer(250) or g for _, _, g in rooms]
        geoms = [g if not g.is_empty else rooms[i][2] for i, g in enumerate(geoms)]
        tree = shapely.STRtree(geoms)
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
                    best, bk = None, None
                    for tol in (TOL_MM, REACH_MM):
                        for j in tree.query(p.buffer(tol)):
                            d = geoms[int(j)].distance(p)
                            if d <= tol:
                                k = (round(d / 50.0), geoms[int(j)].area)
                                if bk is None or k < bk:
                                    best, bk = int(j), k
                        if best is not None:
                            break
                    axis_pos = px if fx == 0 else py
                    depth = -(px * fx + py * fy)            # smaller = nearer the viewer of that elevation
                    steps.append(dict(storey=st, kind=kind, elevation=ename, room=rooms[best][0] if best is not None else "",
                                      room_name=rooms[best][1] if best is not None else "", sector=sector, x=round(px), y=round(py),
                                      axis_mm=axis_pos, depth=depth, len_m=L / n / 1000.0, proj_m=L / n / 1000.0 * abs(cosf), cosf=cosf))
        # front step per elevation and axis bin
        front = {}
        for s in steps:
            k = (s["elevation"], int(s["axis_mm"] // 100))
            if k not in front or s["depth"] < front[k]:
                front[k] = s["depth"]
        for s in steps:
            prof = profiles.get((s["elevation"], st))
            hidden = s["depth"] > front[(s["elevation"], int(s["axis_mm"] // 100))] + FRONT_TOL_MM
            s["hidden"] = hidden
            s["glass_m2"] = 0.0 if (hidden or prof is None) else _glass(prof, s["axis_mm"]) * s["proj_m"]
            sp = slats.get((s["elevation"], st))
            s["slat_m2"] = 0.0 if (hidden or sp is None) else _glass(dict(bins=sp["bins"], glass_m2_per_m=sp["slat_m2_per_m"]), s["axis_mm"]) * s["proj_m"]
            s["on_elevation"] = prof is not None
        steps_all += steps
        agg = defaultdict(lambda: dict(facade_m=0.0, glass_m2=0.0, slat_m2=0.0, hidden_m=0.0, court_m=0.0, elevations=set()))
        for s in steps:
            if not s["room"]:
                continue
            a_ = agg[(s["room"], s["room_name"], s["sector"])]
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
                        "hidden_m", "court_m", "elevations"])
            for (rid, nm, sec), v in sorted(agg.items()):
                fa = v["facade_m"] * h
                w.writerow([st, rid, nm, sec, round(v["facade_m"], 2), h, round(fa, 1), round(v["glass_m2"], 2), round(v["slat_m2"], 2),
                            round(v["glass_m2"] / fa, 3) if fa else 0, round(v["hidden_m"], 2), round(v["court_m"], 2),
                            "; ".join(sorted(v["elevations"]))])
    with (REG / "room_glazing_steps.csv").open("w", newline="", encoding="utf-8") as fh:
        keys = ["storey", "kind", "elevation", "room", "room_name", "sector", "x", "y", "axis_mm", "len_m", "proj_m", "glass_m2", "slat_m2", "hidden"]
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(steps_all)
    return len(steps_all)


if __name__ == "__main__":
    print(run(), "steps")
