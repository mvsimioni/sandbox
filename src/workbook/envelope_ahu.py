"""Facade exposure of the rooms of one AHU, by a closed facade line, written to the Envelope tab of the issued workbooks.

Why: the first method (envelope.py) takes the outline of the storey as the union of the plan rooms. Gaps between rooms (columns, shafts, rooms
with no outline) move that outline inside the building, so most rooms on the real facade are missed. The common-area workbook also treats
App D spaces as internal. AHU-B-06-06STAFF-01 had no envelope row in any workbook.

Method (provisional, to confirm by the engineer):
* Facade line of the storey: union of the plan room outlines, closed by CLOSING_MM (buffer out, then in). The closing bridges gaps between
  rooms up to 2 x CLOSING_MM wide. Only the outer boundary of the main floor plate is a facade (courts and voids are not).
* A room is exposed where the facade line passes within TOL_MM of the room outline. The facade length of the room is the length of the
  facade line inside that band. The orientation is the outward normal of each part of the line, with the grid bearing of envelope.py.
* Gain: as envelope.py (ETTV ratio per facade x facade length x storey height; ETTV coefficients).
* Facade length of the AHU zone (plan rooms of the zone) that no workbook room carries is reported, not allocated.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import shutil
from pathlib import Path
from typing import Dict, List, Tuple

from . import envelope as envm

REG = Path("out/registers")
CLOSING_MM = 1500.0
TOL_MM = 300.0


def _rows(p: Path) -> List[Dict[str, str]]:
    with p.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class FacadeLine:
    def __init__(self, storey: str, root: Path = Path("."), closing: float = CLOSING_MM):
        from shapely import wkt
        from shapely.ops import unary_union
        polys = []
        for r in _rows(root / REG / f"room_polygons_{storey}.csv"):
            try:
                g = wkt.loads(r["polygon_building_mm_wkt"]).buffer(0)
            except Exception:
                continue
            if not g.is_empty:
                polys.append(g)
        u = unary_union([p.buffer(closing) for p in polys]).buffer(-closing)
        geoms = list(u.geoms) if hasattr(u, "geoms") else [u]
        self.plate = max(geoms, key=lambda g: g.area)
        self.ring = self.plate.exterior

    def lengths(self, outline, tol: float = TOL_MM) -> Dict[str, float]:
        from shapely.geometry import LineString, Point
        band = outline.buffer(tol)
        out: Dict[str, float] = {}
        pts = list(self.ring.coords)
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
            if self.plate.contains(Point(mx + nx * 200.0, my + ny * 200.0)):
                nx, ny = -nx, -ny
            az = (math.degrees(math.atan2(nx, ny)) + envm.GRID_BEARING_DEG) % 360.0
            s = envm.SECTORS[int(((az + 22.5) % 360.0) // 45.0)]
            out[s] = out.get(s, 0.0) + part / 1000.0
        return {k: v for k, v in out.items() if v >= 0.3}

    def allocate(self, outlines: Dict[str, object], tol: float = TOL_MM, step: float = 100.0) -> Dict[str, Dict[str, float]]:
        """Each step of the facade line goes to the nearest outline within tol, so a length is never counted in two rooms."""
        from shapely.geometry import Point
        from shapely.strtree import STRtree
        keys = list(outlines)
        geoms = [outlines[k] for k in keys]
        tree = STRtree(geoms)
        out: Dict[str, Dict[str, float]] = {k: {} for k in keys}
        pts = list(self.ring.coords)
        for a, b in zip(pts[:-1], pts[1:]):
            L = math.hypot(b[0] - a[0], b[1] - a[1])
            if L < 1.0:
                continue
            nx, ny = (b[1] - a[1]) / L, -(b[0] - a[0]) / L
            mx, my = (a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0
            if self.plate.contains(Point(mx + nx * 200.0, my + ny * 200.0)):
                nx, ny = -nx, -ny
            az = (math.degrees(math.atan2(nx, ny)) + envm.GRID_BEARING_DEG) % 360.0
            s = envm.SECTORS[int(((az + 22.5) % 360.0) // 45.0)]
            n = max(1, int(round(L / step)))
            for i in range(n):
                t = (i + 0.5) / n
                p = Point(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
                best, bd = None, tol
                for j in tree.query(p.buffer(tol)):
                    d = geoms[int(j)].distance(p)
                    if d <= bd:
                        best, bd = keys[int(j)], d
                if best is not None:
                    out[best][s] = out[best].get(s, 0.0) + L / n / 1000.0
        return {k: {s: v for s, v in d.items() if v >= 0.3} for k, d in out.items()}


def appd_outlines(storey: str, root: Path) -> Dict[int, object]:
    from shapely import wkt
    return {int(r["annotation_index"]): wkt.loads(r["polygon_building_mm_wkt"]).buffer(0)
            for r in _rows(root / REG / "appd_outlines_mm.csv") if r["storey"] == storey and r["polygon_building_mm_wkt"]}


def remark(length: float, sector: str, storey: str, h: float, wwr: float, page: str) -> str:
    return (f"Facade length {length:.1f} m on the {sector} facade of storey {storey}, storey height {h:g} m, window-to-wall ratio {wwr:.3f} "
            f"(ETTV report, page {page}). Facade length measured on the facade line of the storey: plan room outlines joined across gaps up to "
            f"{2 * CLOSING_MM / 1000:g} m; room edge within {TOL_MM:g} mm of the line. Openings are not read from the elevations one by one. Confirm.")


def write_rows(wb_path: Path, out_path: Path, new_rows: Dict[str, List[Dict]], last_row_limit: int) -> int:
    import openpyxl
    wb = openpyxl.load_workbook(wb_path)
    ws = wb["Envelope"]
    keep = []
    for r in range(7, ws.max_row + 1):
        if ws.cell(r, 1).value in new_rows:
            keep.append(r)
    if keep:
        raise SystemExit(f"{wb_path.name}: Envelope already has rows for {sorted({ws.cell(r, 1).value for r in keep})}; not replaced by this script")
    last = max(r for r in range(7, ws.max_row + 1) if ws.cell(r, 1).value)
    proto = last
    r = last
    for number, rows in new_rows.items():
        for e in rows:
            r += 1
            if r > last_row_limit:
                raise SystemExit(f"{wb_path.name}: Envelope range ends at row {last_row_limit}")
            vals = [number, e["segment"], e["building_part"], e["face"], e["azimuth_deg"], e["orientation"], e["glazing_m2"], e["wall_m2"], e["roof_m2"],
                    e["shading"], e["method"], e["code"], e["source"], f"=INDEX(OriWindow,MATCH(F{r},OriName,0))", f"=INDEX(OriWall,MATCH(F{r},OriName,0))",
                    f"=G{r}*N{r}", f"=H{r}*O{r}", f"=I{r}*RoofSolarGain", e["remark"]]
            for c, v in enumerate(vals, start=1):
                cell = ws.cell(r, c, v)
                src = ws.cell(proto, c)
                cell._style = copy.copy(src._style)
    wb.save(out_path)
    return r - last


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ahu", default="AHU-B-06-06STAFF-01")
    ap.add_argument("--storey", default="L06")
    ap.add_argument("--workbook", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--report", required=True)
    a = ap.parse_args()
    root = Path(".")
    import openpyxl
    from shapely.ops import unary_union
    from shapely import wkt

    env = envm.Envelope(root)
    h = env.storey_height(a.storey)
    line = FacadeLine(a.storey, root)
    outl = appd_outlines(a.storey, root)

    zone_rows = {r["room_id"] for r in _rows(root / REG / f"zone_assignment_{a.storey}.csv") if r["class"] == a.ahu}
    zone = unary_union([wkt.loads(r["polygon_building_mm_wkt"]).buffer(0) for r in _rows(root / REG / f"room_polygons_{a.storey}.csv")
                        if r["room_id"] in zone_rows and r["polygon_building_mm_wkt"]])

    wb = openpyxl.load_workbook(a.workbook, read_only=True)
    ri = wb["Room Information"]
    rooms: List[Tuple[str, str, str, bool]] = []
    for row in ri.iter_rows(min_row=7, values_only=True):
        if not row[0] or row[6] != a.storey:
            continue
        if row[9] == a.ahu:
            rooms.append((row[0], row[3], row[8], True))
        elif not row[9] and row[8] and str(row[8]).startswith("APPD-"):
            g = outl.get(int(str(row[8]).rsplit("-", 1)[1]))
            if g is not None and g.area > 0 and g.intersection(zone).area / g.area > 0.5:
                rooms.append((row[0], row[3], row[8], False))       # space with no unit link, inside the plan zone of the AHU
    wb.close()

    new: Dict[str, List[Dict]] = {}
    report = dict(ahu=a.ahu, storey=a.storey, storey_height_m=h, closing_mm=CLOSING_MM, tol_mm=TOL_MM, rooms=[], unallocated={})
    carried = []
    geo = {}
    for number, name, sid, linked in rooms:
        idx = int(sid.rsplit("-", 1)[1]) if sid and sid.startswith("APPD-") else None
        if idx is not None and outl.get(idx) is not None:
            geo[number] = outl[idx]
    alloc = line.allocate(geo)
    for number, name, sid, linked in rooms:
        g = geo.get(number)
        L = alloc.get(number, {})
        rows = env.rows_from_lengths(a.storey, number, L, "ETTV-ENV") if L else []
        for e in rows:
            f = env.fac[e["orientation"]]
            e["remark"] = remark(L[e["orientation"]], e["orientation"], a.storey, h, f["wwr"], f["page"])
            if not linked:
                e["remark"] += (f" The room has no AHU link in this workbook. It lies inside the plan zone of {a.ahu}: "
                                "its gain counts in the room, not in the AHU total, until the engineer confirms the unit.")
        if rows:
            new[number] = rows
            carried.append(g)
        report["rooms"].append(dict(room=number, name=name, outline=sid, linked_to_ahu=linked, facade_m={k: round(v, 2) for k, v in L.items()},
                                    glazing_m2=sum(e["glazing_m2"] for e in rows), wall_m2=sum(e["wall_m2"] for e in rows)))

    z_len = line.lengths(zone)
    c_len = line.lengths(unary_union(carried)) if carried else {}
    report["zone_facade_m"] = {k: round(v, 2) for k, v in z_len.items()}
    report["carried_facade_m"] = {k: round(v, 2) for k, v in c_len.items()}
    report["unallocated"] = {k: round(z_len.get(k, 0) - c_len.get(k, 0), 2) for k in z_len if z_len.get(k, 0) - c_len.get(k, 0) > 0.3}

    n = write_rows(Path(a.workbook), Path(a.out), new, last_row_limit=135)
    report["rows_added"] = n
    Path(a.report).write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "rooms"}, indent=1))
    for r in report["rooms"]:
        if r["facade_m"]:
            print(r)


if __name__ == "__main__":
    main()
