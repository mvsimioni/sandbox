"""Envelope gain of an external room by the ETTV component method, simple facade method (D-042, D-049, D-066, D-071).

Method, as documented on Basis of Design:
* Properties come from the ETTV report (wall U-value, glazing U-value, shading coefficient, correction factor and window-to-wall ratio of each facade).
* A room is external only where an edge of its plan outline lies on the outline of the storey. The facade of an edge is the sector (N, NE, E, ...) of its outward normal,
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
EDGE_TOL_MM = 160.0
BUFFER_MM = 100.0


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
        if storey in self._storey_cache:
            return self._storey_cache[storey]
        from shapely import wkt
        from shapely.ops import unary_union
        rows = _read(self.root / REG / f"room_polygons_{storey}.csv")
        polys = []
        for r in rows:
            try:
                g = wkt.loads(r["polygon_building_mm_wkt"])
            except Exception:
                continue
            if not g.is_empty:
                polys.append(g.buffer(0))
        if not polys:
            self._storey_cache[storey] = None
            return None
        u = unary_union([p.buffer(BUFFER_MM) for p in polys])
        geoms = list(u.geoms) if hasattr(u, "geoms") else [u]
        big = max(geoms, key=lambda g: g.area)            # only the outer boundary of the main body of the storey; islands and courts are not facades
        ring = big.exterior
        self._storey_cache[storey] = ring
        return ring

    def facade_lengths(self, storey: str, room_wkt: str) -> Dict[str, float]:
        """Length in metres of the edges of the room outline that lie on the outline of the storey, by facade sector (cached by outline)."""
        key = (storey, hash(room_wkt))
        cache = self.__dict__.setdefault("_len_cache", {})
        if key not in cache:
            cache[key] = self._facade_lengths(storey, room_wkt)
        return dict(cache[key])

    def _facade_lengths(self, storey: str, room_wkt: str) -> Dict[str, float]:
        from shapely import wkt
        from shapely.geometry import Point
        ring = self._outline(storey)
        if ring is None:
            return {}
        try:
            g = wkt.loads(room_wkt).buffer(0)
        except Exception:
            return {}
        polys = list(g.geoms) if hasattr(g, "geoms") else [g]
        out: Dict[str, float] = {}
        for p in polys:
            for ring_ in [p.exterior]:
                pts = list(ring_.coords)
                for (x1, y1), (x2, y2) in zip(pts[:-1], pts[1:]):
                    L = math.hypot(x2 - x1, y2 - y1)
                    if L < 50:
                        continue
                    mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
                    if ring.distance(Point(mx, my)) > BUFFER_MM + EDGE_TOL_MM:
                        continue
                    nx, ny = (y2 - y1) / L, -(x2 - x1) / L
                    eps = 200.0
                    if p.contains(Point(mx + nx * eps, my + ny * eps)):
                        nx, ny = -nx, -ny
                    az = (math.degrees(math.atan2(nx, ny)) + GRID_BEARING_DEG) % 360.0
                    sector = SECTORS[int(((az + 22.5) % 360.0) // 45.0)]
                    out[sector] = out.get(sector, 0.0) + L / 1000.0
        return out

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
                        "Simple facade method: openings are not read from the elevations one by one. Confirm.")))
        return rows
