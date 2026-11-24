"""Envelope gain of an external room by the ETTV component method, simple facade method (D-042, D-049, D-066, D-071).

Method, as documented on Basis of Design:
* Properties come from the ETTV report (wall U-value, glazing U-value, shading coefficient, correction factor and window-to-wall ratio of each facade).
* Facade line (D-089): plan room outlines joined across gaps up to 3 m. A room is external where the line passes within 300 mm of it; each part of the line
  goes to the nearest room of the same outline set. The facade of a part of the line is the sector (N, NE, E, ...) of its outward normal,
  taken from the grid axes with the grid bearing (assumed, to confirm).
* Area of the facade of the room = length of its edges on the facade x storey height. Glass = area x window-to-wall ratio of the ETTV report for that facade. Wall = the rest
  (spandrel and mullion count as wall, as the report counts them).
* Gain per m2 of wall = 12 x Uw. Gain per m2 of glass = 3.4 x Uf + 211 x CF x SC. These are the equivalent coefficients of the ETTV formula, an averaged basis.
Simplification (D-071): the areas of openings are not read opening by opening from the elevations. Flagged for the human check. The check of the elevation window-to-wall ratio
against the ETTV ratio (D-049) is not made.
"""

from __future__ import annotations

import csv
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REG = Path("out/registers")
SECTORS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
GRID_BEARING_DEG = 359.77          # azimuth of the page-up grid axis relative to grid north (grid model, fit of the call-outs on the sample sheet); to confirm
ENVELOPE_STOREYS = {"L01": 17.0, "L02": 23.0, "L03": 29.0, "L04": 35.0, "L05": 41.0, "L06": 47.0, "L07": 53.0, "L08": 59.0}
DEFAULT_STOREY_H = 4.5
CLOSING_MM = 1500.0        # D-089: gaps up to 3 m between plan rooms are closed (provisional)
TOL_MM = 300.0             # D-089: room outline within 300 mm of the facade line (provisional)
STEP_MM = 100.0
OPEN_MM = 250.0            # D-089: parts of an outline narrower than 500 mm are not used to take the facade line


def _opened(g):
    h = g.buffer(-OPEN_MM).buffer(OPEN_MM)
    return h if not h.is_empty else g


def _f(v: Any) -> Optional[float]:
    try:
        return float(v) if str(v).strip() != "" else None
    except ValueError:
        return None


def _read(p: Path) -> List[Dict[str, str]]:
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class Envelope:
    def __init__(self, root: Path = Path("."), coeff: Optional[Dict[str, float]] = None):
        self.root = Path(root)
        coeff = coeff or {}
        self.k_wall = float(coeff.get("wall_w_m2_k", 12))
        self.k_cond = float(coeff.get("glass_conduction_w_m2_k", 3.4))
        self.k_sol = float(coeff.get("solar_w_m2", 211))
        self.fac: Dict[str, Dict[str, Any]] = {}
        for r in _read(self.root / REG / "ettv_facades.csv"):
            if r["row_type"] == "facade":
                self.fac[r["orientation"]] = dict(
                    wwr=_f(r["wwr"]), uw=_f(r["wall_u_area_weighted"]), uf=_f(r["glazing_u_w_m2k"]), sc=_f(r["sc_area_weighted"]),
                    cf=_f(r["correction_factor_cf"]), page=r["page"], record=r["record_id"], text=r["text_as_printed"], ettv=_f(r["ettv_w_m2"]))
        self._storey_cache: Dict[str, Any] = {}
        self.heights = self._storey_heights()

    @property
    def available(self) -> bool:
        return bool(self.fac)

    # -- properties --------------------------------------------------------------- #
    def orientation_table(self, source: str) -> List[Dict[str, Any]]:
        """Rows of the orientation table of the workbook: gain per m2 of glass and of wall for each facade."""
        out = []
        for o in SECTORS:
            f = self.fac.get(o)
            if not f or None in (f["uw"], f["uf"], f["sc"], f["cf"]):
                continue
            out.append(dict(name=o, window_w_m2=round(self.k_cond * f["uf"] + self.k_sol * f["cf"] * f["sc"], 2),
                            wall_w_m2=round(self.k_wall * f["uw"], 2), code="A", source=source))
        return out

    # -- geometry ------------------------------------------------------------------ #
    def _storey_heights(self) -> Dict[str, float]:
        ffl = dict(ENVELOPE_STOREYS)
        rows = _read(self.root / REG / "elevation_levels_consolidated.csv")
        got = {r["plan_key"]: _f(r["plan_ffl_m"]) for r in rows if r.get("block") == "IP" and r.get("plan_key") and _f(r.get("plan_ffl_m")) is not None}
        for k, v in got.items():
            if re.fullmatch(r"L\d\d", k):
                ffl[k] = v
        order = sorted((v, k) for k, v in ffl.items())
        h = {}
        for i, (v, k) in enumerate(order):
            h[k] = (order[i + 1][0] - v) if i + 1 < len(order) else DEFAULT_STOREY_H
        return h

    def storey_height(self, storey: str) -> Optional[float]:
        return self.heights.get(storey)

    def _outline(self, storey: str):
        """Facade line of the storey (D-089): the plan room outlines joined across gaps up to 2 x CLOSING_MM (columns, shafts, rooms with no
        outline), the outer boundary of the main floor plate only (courts and voids are not facades). Returns (plate, ring) or None."""
        if storey in self._storey_cache:
            return self._storey_cache[storey]
        from shapely.ops import unary_union
        polys = [g for g in self._set_geoms(storey, "plan")]
        if not polys:
            self._storey_cache[storey] = None
            return None
        u = unary_union([p.buffer(CLOSING_MM) for p in polys]).buffer(-CLOSING_MM)
        geoms = list(u.geoms) if hasattr(u, "geoms") else [u]
        big = max(geoms, key=lambda g: g.area)
        self._storey_cache[storey] = (big, big.exterior)
        return self._storey_cache[storey]

    def _set_geoms(self, storey: str, kind: str):
        """Outlines of one set on the storey: 'plan' = plan room outlines, 'appd' = App D area outlines."""
        key = (storey, kind)
        cache = self.__dict__.setdefault("_sets", {})
        if key not in cache:
            from shapely import wkt
            src = (_read(self.root / REG / f"room_polygons_{storey}.csv") if kind == "plan"
                   else [r for r in _read(self.root / REG / "appd_outlines_mm.csv") if r.get("storey") == storey])
            out = []
            for r in src:
                try:
                    g = wkt.loads(r["polygon_building_mm_wkt"]).buffer(0)
                except Exception:
                    continue
                if not g.is_empty:
                    out.append(g)
            cache[key] = out
        return cache[key]

    def _allocation(self, storey: str, kind: str):
        """Each step of the facade line goes to the nearest outline of the set within TOL_MM. Returns a list (per outline) of {sector: m}."""
        key = (storey, kind)
        cache = self.__dict__.setdefault("_alloc", {})
        if key in cache:
            return cache[key]
        from shapely.geometry import Point
        from shapely.strtree import STRtree
        o = self._outline(storey)
        geoms = self._set_geoms(storey, kind)
        res: List[Dict[str, float]] = [dict() for _ in geoms]
        if o is None or not geoms:
            cache[key] = res
            return res
        plate, ring = o
        geoms = [_opened(g) for g in geoms]       # slivers under 2 x OPEN_MM wide (gaps read as part of a room) are not the room
        tree = STRtree(geoms)
        pts = list(ring.coords)
        for a, b in zip(pts[:-1], pts[1:]):
            L = math.hypot(b[0] - a[0], b[1] - a[1])
            if L < 1.0:
                continue
            nx, ny = (b[1] - a[1]) / L, -(b[0] - a[0]) / L
            mx, my = (a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0
            if plate.contains(Point(mx + nx * 200.0, my + ny * 200.0)):
                nx, ny = -nx, -ny
            az = (math.degrees(math.atan2(nx, ny)) + GRID_BEARING_DEG) % 360.0
            sector = SECTORS[int(((az + 22.5) % 360.0) // 45.0)]
            n = max(1, int(round(L / STEP_MM)))
            for i in range(n):
                t = (i + 0.5) / n
                p = Point(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
                best, bk = None, None
                for j in tree.query(p.buffer(TOL_MM)):
                    d = geoms[int(j)].distance(p)
                    if d > TOL_MM:
                        continue
                    k = (round(d / 50.0), geoms[int(j)].area)      # nearest (50 mm steps); on a tie the smaller outline (a room before an open region)
                    if bk is None or k < bk:
                        best, bk = int(j), k
                if best is not None:
                    res[best][sector] = res[best].get(sector, 0.0) + L / n / 1000.0
        cache[key] = res
        return res

    def plan_room_facade(self, storey: str) -> Dict[str, Dict[str, float]]:
        """Facade of each plan room of the storey, keyed by room_id (plan set, D-089)."""
        key = ("_prf", storey)
        cache = self.__dict__.setdefault("_prf", {})
        if storey in cache:
            return cache[storey]
        from shapely import wkt
        ids = []
        for r in _read(self.root / REG / f"room_polygons_{storey}.csv"):
            try:
                g = wkt.loads(r["polygon_building_mm_wkt"]).buffer(0)
            except Exception:
                continue
            if not g.is_empty:
                ids.append(r["room_id"])
        al = self._allocation(storey, "plan")
        cache[storey] = {rid: al[i] for i, rid in enumerate(ids) if i < len(al) and al[i]}
        return cache[storey]

    def facade_lengths(self, storey: str, room_wkt: str) -> Dict[str, float]:
        """Length in metres of facade line given to this room outline, by facade sector (D-089). The outline is found in the plan set or in the
        App D set of the storey (same outline, or overlap of 90 % or more); the facade line is shared among the outlines of that set only."""
        key = (storey, hash(room_wkt))
        cache = self.__dict__.setdefault("_len_cache", {})
        if key not in cache:
            cache[key] = self._facade_lengths(storey, room_wkt)
        return dict(cache[key])

    def _facade_lengths(self, storey: str, room_wkt: str) -> Dict[str, float]:
        from shapely import wkt
        try:
            g = wkt.loads(room_wkt).buffer(0)
        except Exception:
            return {}
        if g.is_empty or self._outline(storey) is None:
            return {}
        for kind in ("plan", "appd"):
            geoms = self._set_geoms(storey, kind)
            best, score = None, 0.0
            for j, h in enumerate(geoms):
                if not h.intersects(g):
                    continue
                inter = h.intersection(g).area
                s = inter / max(h.area, g.area) if max(h.area, g.area) > 0 else 0.0
                if s > score:
                    best, score = j, s
            if best is not None and score >= 0.9:
                return {k: v for k, v in self._allocation(storey, kind)[best].items() if v >= 0.3}
        # an outline of neither set: the facade line within TOL_MM of the outline
        from shapely.geometry import LineString
        plate, ring = self._outline(storey)
        band = g.buffer(TOL_MM)
        out: Dict[str, float] = {}
        pts = list(ring.coords)
        from shapely.geometry import Point
        for a, b in zip(pts[:-1], pts[1:]):
            seg = LineString([a, b])
            L = seg.length
            if L < 1.0:
                continue
            part = seg.intersection(band).length
            if part < 1.0:
                continue
            nx, ny = (b[1] - a[1]) / L, -(b[0] - a[0]) / L
            mx, my = (a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0
            if plate.contains(Point(mx + nx * 200.0, my + ny * 200.0)):
                nx, ny = -nx, -ny
            az = (math.degrees(math.atan2(nx, ny)) + GRID_BEARING_DEG) % 360.0
            sector = SECTORS[int(((az + 22.5) % 360.0) // 45.0)]
            out[sector] = out.get(sector, 0.0) + part / 1000.0
        return {k: v for k, v in out.items() if v >= 0.3}

    def rows(self, storey: str, number: str, room_wkt: str, source: str) -> List[Dict[str, Any]]:
        """Envelope rows of the workbook schema for one room (one per facade sector). Empty for an internal room."""
        if not room_wkt:
            return []
        return self.rows_from_lengths(storey, number, self.facade_lengths(storey, room_wkt), source)

    def typical_rows(self, storey: str, number: str, plan_wkts: List[str], n_rooms: int, source: str) -> List[Dict[str, Any]]:
        """Envelope rows of a typical room that is linked by name and count to several plan rooms: the facade lengths of all those plan rooms divided by the number of rooms."""
        tot: Dict[str, float] = {}
        for w in plan_wkts:
            if w:
                for k, v in self.facade_lengths(storey, w).items():
                    tot[k] = tot.get(k, 0.0) + v
        if not tot or n_rooms <= 0:
            return []
        rows = self.rows_from_lengths(storey, number, {k: v / n_rooms for k, v in tot.items()}, source)
        for r in rows:
            r["remark"] = (f"Typical room: the facade of the {len(plan_wkts)} plan rooms of the same name, shared by {n_rooms} rooms. " + r["remark"])
        return rows

    def rows_from_lengths(self, storey: str, number: str, lengths: Dict[str, float], source: str) -> List[Dict[str, Any]]:
        h = self.storey_height(storey)
        if h is None:
            return []
        rows = []
        for sector, length in sorted(lengths.items()):
            f = self.fac.get(sector)
            if f is None or f["wwr"] is None or length < 0.5:
                continue
            area = length * h
            glass = area * f["wwr"]
            wall = area - glass
            az = SECTORS.index(sector) * 45
            rows.append(dict(
                segment=f"{number}-{sector}", building_part="Facade", face=sector, azimuth_deg=az, orientation=sector, glazing_m2=round(glass, 1), wall_m2=round(wall, 1), roof_m2=0,
                shading="shading coefficient of the ETTV report, area weighted", method="ETTV ratio x facade length x storey height", source=source, code="A",
                remark=(f"Facade length {length:.1f} m on the {sector} facade of storey {storey}, storey height {h:g} m, window-to-wall ratio {f['wwr']:.3f} (ETTV report, page {f['page']}). "
                        "Facade line: plan room outlines joined across gaps up to 3 m; room within 300 mm of the line; each part of the line to the nearest room. "
                        "Simple facade method: openings are not read from the elevations one by one. Confirm.")))
        return rows
