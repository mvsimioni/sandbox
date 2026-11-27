"""Engineer's instruction of 27 Nov 2026 (D-125): room areas of the L6 rooms of AHU-B-06-06STAFF-01 marked up by Marcus.

* AGV lift lobbies LL ALLIP.06.01 and LL ALLIP.06.02: the lift shafts are not part of the lobbies; the measured outline took them in, so the room
  area is the DPA area (the zoning outlines are cut by src.zoning.amend_d120.cut_shafts).
* Multipurpose Room - Admin ANWSIP6 AM.06.03: the DPA area (8.85 m2; part plan AR-IP-02-06-011 Rev A tags 06.IP.ANWSIP6.001 AREA = 9), not the
  smaller measured outline of the GA plan (8.07 m2).
"""
from __future__ import annotations

from typing import Any, Dict, List

ROOMS = ("LL ALLIP.06.01", "LL ALLIP.06.02", "ANWSIP6 AM.06.03")
SRC = "ENG-20261127"


def apply(datasets: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    from .ctx import BUILTIN_SOURCES
    log = []
    for n, d in datasets.items():
        hit = False
        for r in d["rooms"]:
            if r.get("number") in ROOMS and r.get("dpa_area_m2"):
                r["area_selector"] = "DPA"
                r.setdefault("prov", {})["area_selector"] = ["S", SRC]
                log.append(dict(workbook=f"D{n}", room=r["number"], dpa=r["dpa_area_m2"], measured=r.get("measured_area_m2")))
                hit = True
        if hit and not any(s_["id"] == SRC for s_ in d.get("sources", [])):
            d.setdefault("sources", []).extend(s_ for s_ in BUILTIN_SOURCES if s_["id"] == SRC)
    return log


# D-127 (Marcus, 27 Nov 2026): Store - Equipment AMSC 10.19.03 is served by AHU-B-06-06STAFF-01 (tender air-side schematic IP-BH-06068,
# STORE EQ 10.19.03; plan tag 'Str / AC (24HR)'). Applied before consolidation, which moves the room to the workbook of the unit (D-089).
UNIT_SET = {"AMSC 10.19.03": "AHU-B-06-06STAFF-01"}


def assign_units(datasets: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    from .ctx import BUILTIN_SOURCES
    log = []
    for n, d in datasets.items():
        for r in d["rooms"]:
            u = UNIT_SET.get(r.get("number"))
            if u and r.get("ahu") != u:
                log.append(dict(workbook=f"D{n}", room=r["number"], before=r.get("ahu"), system_before=r.get("system_type"), unit=u))
                r["ahu"] = u
                r["system_type"] = None
                r.setdefault("prov", {})
                if isinstance(r["prov"], dict):
                    r["prov"]["ahu"] = ["S", SRC]
                r["remark"] = (r.get("remark") or "").rstrip() + f" Served by {u} (Marcus 27 Nov 2026, D-127; tender air-side schematic IP-BH-06068)."
                if not any(s_["id"] == SRC for s_ in d.get("sources", [])):
                    d.setdefault("sources", []).extend(s_ for s_ in BUILTIN_SOURCES if s_["id"] == SRC)
    return log

