"""Envelope areas of the facade rooms from the building elevations (D-120, forensic envelope review, 26 Nov 2026).

Replaces the simple facade method (D-071: ETTV window-to-wall ratio x facade length x storey height) for the rooms of storeys B1 to L07 that have
a plan room: for each facade sector the glass is the glass measured on the elevations for that plan room (src.arch.room_glazing, after the
corrections of the review) and the wall is the rest of the facade (facade length x storey height - glass). The U-values and shading coefficients
stay those of the ETTV report for the sector. Where several workbook rooms share one plan room, the facade is split by room area. A room whose
plan room has no facade carries no facade rows. Roof rows and rows of other building parts are kept.

Applied after consolidation, before the zone facade share (D-090). It also replaces the L07 facade rows of D47 typed by Winson Khoo (D-121).
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

REG = Path("out/registers")
STOREYS = ["B1", "L01", "L02", "L03", "L04", "L05", "L06", "L07"]
SECTORS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
SRC = "ARCH-ELEV"
# workbook rooms with no plan room that take the facade of a plan room: number -> (storey, plan room, glass factor, facade height m, reason)
ROOM_MAP = {"TRED 09.03.05": ("L02", "L02-R108", 0.0, 12.0,
                              "Lecture theatre stage (outline L02-R108, tag S.33): AC; the curtain wall in front of it is not a window, so the facade is wall only "
                              "(Marcus, 26 Nov 2026, D-122). Height: double-height volume, L02 FFL to L04 FFL (the curtain wall runs about 8.9 m above L02 FFL).")}


def _area(r: Dict[str, Any]) -> float:
    for k in ("dpa_area_m2", "soa_area_m2", "measured_area_m2"):
        v = r.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return 0.0


def measured(root: Path = Path(".")) -> Dict[tuple, Dict[str, Dict[str, Any]]]:
    out: Dict[tuple, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    for st in STOREYS:
        p = root / REG / f"room_glazing_{st}.csv"
        if not p.exists():
            continue
        for r in csv.DictReader(p.open(encoding="utf-8")):
            out[(st, r["room_id"])][r["sector"]] = dict(facade_m=float(r["facade_m"]), h=float(r["storey_h_m"]), glass=float(r["glass_m2"]),
                                                         hidden_m=float(r["hidden_m"]), court_m=float(r["court_m"]), elevations=r["elevations"])
    return out


def apply(datasets: Dict[str, Dict[str, Any]], root: Path = Path(".")) -> List[Dict[str, Any]]:
    from . import envelope as envm
    env = envm.Envelope(root)
    meas = measured(root)
    if not meas:
        return []
    groups: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for n, d in datasets.items():
        for r in d["rooms"]:
            pid = r.get("plan_room_id")
            if pid and r.get("storey") in STOREYS:
                groups[(r["storey"], pid)].append(r)
    for n, d in datasets.items():
        for r in d["rooms"]:
            if r.get("number") in ROOM_MAP:
                st, pid, gf, hh, why = ROOM_MAP[r["number"]]
                r["_d120_map"] = (gf, hh, why)
                groups[(st, pid)].append(r)
    log = []
    for (st, pid), rooms in groups.items():
        m = meas.get((st, pid), {})
        tot = sum(_area(r) for r in rooms) or float(len(rooms))
        for r in rooms:
            share = (_area(r) or 1.0) / tot if len(rooms) > 1 else 1.0
            keep = [e for e in (r.get("envelope") or []) if e.get("building_part") != "Facade"]
            old = [e for e in (r.get("envelope") or []) if e.get("building_part") == "Facade"]
            rows = []
            for sector in SECTORS:
                v = m.get(sector)
                if not v or v["facade_m"] * share < 0.5:
                    continue
                f = env.fac.get(sector)
                L, h = v["facade_m"] * share, v["h"]
                glass = v["glass"] * share
                mp = r.get("_d120_map")
                if mp:
                    glass, h = glass * mp[0], mp[1]
                area = L * h
                wall = max(0.0, area - glass)
                note = []
                if v["hidden_m"] * share >= 0.5:
                    note.append(f"{v['hidden_m'] * share:.1f} m of it is behind another part of the building on the elevation (no glass read there)")
                if v["court_m"] * share >= 0.5:
                    note.append(f"{v['court_m'] * share:.1f} m faces a court")
                rows.append(dict(
                    segment=f"{r['number']}-{sector}", building_part="Facade", face=sector, azimuth_deg=SECTORS.index(sector) * 45, orientation=sector,
                    glazing_m2=round(glass, 1), wall_m2=round(wall, 1), roof_m2=0,
                    shading="shading coefficient of the ETTV report, area weighted", method="Glass measured on the elevations; wall = facade length x storey height - glass",
                    source=SRC, code="A",
                    remark=(f"Facade {L:.1f} m on the {sector} facade of plan room {pid}" + (f" ({share:.0%} of it, split by area among {len(rooms)} rooms)" if len(rooms) > 1 else "")
                            + f", storey height {h:g} m; glass {glass:.1f} m² measured on {v['elevations']}" + ("; " + "; ".join(note) if note else "")
                            + (f". U-value and shading coefficient: ETTV report, {sector} facade." if f else ". No ETTV record for this facade.")
                            + " Forensic envelope review D-120." + (" " + r["_d120_map"][2] if r.get("_d120_map") else ""))))
            r["envelope"] = keep + rows
            r.pop("_d120_map", None)
            if old or rows:
                log.append(dict(storey=st, plan_room_id=pid, number=r["number"], name=r.get("name", ""),
                                glass_before=round(sum(float(e["glazing_m2"]) for e in old), 1), glass_after=round(sum(e["glazing_m2"] for e in rows), 1),
                                wall_before=round(sum(float(e["wall_m2"]) for e in old), 1), wall_after=round(sum(e["wall_m2"] for e in rows), 1),
                                method_before="; ".join(sorted({e.get("method", "") for e in old}))))
    for d in datasets.values():
        if any(e.get("source") == SRC for r in d["rooms"] for e in r.get("envelope") or []):
            ids = {s["id"] for s in d.get("sources", [])}
            if SRC not in ids:
                from .ctx import BUILTIN_SOURCES
                d.setdefault("sources", []).extend(s for s in BUILTIN_SOURCES if s["id"] == SRC)
            for fl in d.get("flags", []):
                if fl.get("ref") == "Envelope":
                    fl["text"] = ("Envelope gain of the facade rooms of B1 to L07: glass measured on the building elevations for each plan room and facade sector, "
                                  "wall = facade length x storey height - glass (forensic envelope review D-120), with the U-value and shading coefficient of the ETTV report "
                                  "for the sector. Rooms with no plan outline keep a share of the zone facade at the ETTV window-to-wall ratio (D-090). Open points of the "
                                  "review are listed in the review report. The grid bearing is taken as 359.77 degrees and is not yet confirmed.")
    return log
