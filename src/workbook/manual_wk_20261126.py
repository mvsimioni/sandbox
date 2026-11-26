"""Manual changes of Winson Khoo to the workbooks of 25 Nov 2026 (D-114).

The engineer's Drive folder (1K0a_15NdEKs30UNm9jcKHnn38urPnpdb, 'Thermal Load checks') holds the department workbooks of issue R9 (25 Nov 2026
06:30) and the D47 psychrometric issue. The file properties name Winson Khoo as the last author of D00, D35, D43, D47 and the AHU master
summary (25 to 26 Nov 2026); the others are unchanged. Cell-by-cell differences against the base issue: out/qa/manual_20261126/diff_*.csv.
Marcus (26 Nov 2026): retain these changes, in particular the facade areas, where they do not contradict the rules of 26 Nov 2026; correct
the room IDs. Retained here:
  D47  L07 facade areas of the admin workspace (5 rows, S facade) replace the approximate split of the zone facade (D-090);
       safety factor 0.20.
  D43  ITEC 01.01.01 Tech Bar (Public) is an L01 room (its number and Winson): storey L01, no unit (D-103 links it on L01);
       the W facade of ITEC 01.07.03 is not facade; system type VAV on the rooms of AHU-B-07-07STAFF-02; Innovation Lab 50 persons
       per 100 m2; safety factor 0.15.
  D35  L07 core: FRC Satellite 1 is FACM 03.07.01 on L07 (07.IP.FACM.001, 37.67 m2) on AHU-B-07-07CORE-01; FACM 03.01.01 is an L01 room
       and FACM 03.18.01 an L18 room (no unit on this scope); passenger lift lobbies LL PLLIP.07.01 (07.IP.LL.007) and LL PLLIP.07.02
       (07.IP.LL.008) and the staff and service corridors 07.IP.CO.001, 07.IP.CO.002, 07.IP.CO.003 added on AHU-B-07-07CORE-01;
       safety factor 0.20.
  Room IDs: the DPA number where the DPA has the room (Winson typed the architectural number); the architectural number otherwise; the
  staff corridor that Winson numbered 07.IP.CO.006 is 07.IP.CO.003 on the part plans (760 m2, staff corridor).
Not retained (contradicts D-108, the mode of ventilation plans govern): the bed lift lobby LL BLLIP.07.01 (07.IP.LL.001) on
AHU-B-07-07CORE-01: the MOV plan prints FLL, MV; added as MV. Occupancy factors typed on the room-zone sheets: kept as information only,
they no longer reduce outdoor air (D-110).
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List

SRC = {n: dict(id=f"WK-{n}-20261126", document=f"AIGH_C2_{n}_..._Thermal_Load_Calc_RevA.xlsx as edited by Winson Khoo",
                issue="26 Nov 2026 (Drive folder 'Thermal Load checks')", page=pg, location=loc, text_as_read=txt,
                method="Value typed by the engineering team on the workbook; retained by Marcus (26 Nov 2026)", confidence="medium")
       for n, pg, loc, txt in (
           ("D47", "Envelope, rows 19 to 23", "L07 facade rows of the admin workspace",
            "ANWSIP7 Cl.07.01-S 79.2 / 65.4 m2; WS.07.05-S 9.6 / 7.8; WS.07.11-S 12.2 / 10; WS.07.16-S 12.2 / 10; WS.07.20-S 9.9 / 8.1 "
            "(glazing / wall); safety factor 0.2"),
           ("D43", "Room Information rows 7 to 15; Envelope", "storey, AHU, system type, occupancy density; facade rows",
            "ITEC 01.01.01 storey L01, no AHU; ITEC 01.07.03 W facade removed; system type VAV; ITEC 05.07.01 50 persons / 100 m2; "
            "safety factor 0.15"),
           ("D35", "Room Information rows 13 to 15 and 37 to 44", "L07 lift lobbies, corridors, FRC Satellite 1",
            "07.IP.LL.007 Passenger Lift Lobby 57.25 m2; 07.IP.LL.008 57.46 m2; 07.IP.LL.001 Bed Lift Lobby 109.64 m2; 07.IP.CO.001 44.54 m2; "
            "07.IP.CO.002 38.83 m2; 07.IP.CO.006 Staff Corridor 714.41 m2; 07.IP.FACM.001 FRC Satellite 1 37.67 m2 on AHU-B-07-07CORE-01; "
            "safety factor 0.2"))}

D47_FACADE = [("ANWSIP7 Cl.07.01", 79.2, 65.4, 24.1), ("ANWSIP7 WS.07.05", 9.6, 7.8, 2.9), ("ANWSIP7 WS.07.11", 12.2, 10.0, 3.7),
              ("ANWSIP7 WS.07.16", 12.2, 10.0, 3.7), ("ANWSIP7 WS.07.20", 9.9, 8.1, 3.0)]
D35_NEW = [  # number, arch number, name, DPA area, measured area, unit, system type, DPA source id
    ("LL PLLIP.07.01", "07.IP.LL.007", "Passenger Lift Lobby", 61.0, 57.25, "AHU-B-07-07CORE-01", None, "DPA-LLPLLIP0701"),
    ("LL PLLIP.07.02", "07.IP.LL.008", "Passenger Lift Lobby", 61.0, 57.46, "AHU-B-07-07CORE-01", None, "DPA-LLPLLIP0702"),
    ("LL BLLIP.07.01", "07.IP.LL.001", "Bed Lift Lobby", None, 109.64, None, "MV", "DPA-LLBLLIP0701"),
    ("07.IP.CO.001", "07.IP.CO.001", "Services & Goods Corridor", None, 44.54, "AHU-B-07-07CORE-01", None, None),
    ("07.IP.CO.002", "07.IP.CO.002", "Services & Goods Corridor", None, 38.83, "AHU-B-07-07CORE-01", None, None),
    ("07.IP.CO.003", "07.IP.CO.003", "Staff Corridor", None, 714.41, "AHU-B-07-07CORE-01", None, None)]
CIRC = {"criteria_row": "Circulation default (assumed)", "page": "circulation default", "min_oa_ach": 2.0, "min_total_ach": 6.0,
        "temp_min_c": 23, "temp_max_c": 25, "rh_max_pct": 60.0, "pressure_regime": "NEUTRAL", "air_class": "2", "filter": "MERV 15",
        "source": "CIRC-DEFAULT", "code": "A", "presumed": True,
        "remark": "Presumed criteria: the default row for circulation, corridor and lobby spaces (as typed by Winson Khoo, 26 Nov 2026). To confirm."}


def _note(r: Dict[str, Any], txt: str) -> None:
    r["remark"] = ((r.get("remark") or "") + " " + txt).strip()


def _sf(d: Dict[str, Any], v: float, sid: str) -> None:
    c = d["constants"].setdefault("SafetyFactor", {})
    c.update(value=v, code="D", source=sid, remark=f"{v:.0%} as typed by Winson Khoo on the workbook (26 Nov 2026), retained by Marcus (D-114)")


def _arch_src(arch: str) -> Dict[str, Any]:
    """Source row of an architectural room tag (MOV plans first, then part plans), as written by D-107 / D-108."""
    import csv
    from pathlib import Path
    hit = None
    for f in ("out/registers/arch_mov_tags.csv", "out/registers/arch_part_tags.csv"):
        p = Path(f)
        if p.exists() and hit is None:
            hit = next((r for r in csv.DictReader(p.open(encoding="utf-8")) if r.get("number") == arch), None)
    txt = (f"tag {arch} '{hit['name']}', {hit['area_m2']} m², {hit['mode']}" if hit else
           f"{arch}: number typed by Winson Khoo (26 Nov 2026); not found on the MOV plans or the part plans")
    return dict(id=f"ARCH-{arch}", document="Mode of Ventilation plans AR-IP-MOV-xx-001 and architectural part plans AR-IP-02 (room tags)",
                issue="26 Nov 2026", page=(hit or {}).get("sheet", "not found"), location=f"tag {arch}", text_as_read=txt,
                method="Room tag of the architectural drawings (D-107, D-108); room added from the workbook of Winson Khoo (D-114)",
                confidence="high" if hit else "low")


def d47_facade(datasets: Dict[str, Dict[str, Any]], log: List[Dict[str, Any]]) -> None:
    """L07 facade of the D47 admin workspace as typed by Winson Khoo: the five S rows, no facade on the other L07 rooms of D47. Applied
    again after the zone-facade share (D-102), which would otherwise split the zone facade over the rooms with no plan outline."""
    d = datasets.get("47")
    if not d:
        return
    fac = {n: (g, w, L) for n, g, w, L in D47_FACADE}
    for r in d["rooms"]:
        if r.get("storey") != "L07":
            continue
        old = sum(e.get("glazing_m2") or 0 for e in (r.get("envelope") or []))
        if r["number"] in fac:
            g, w, L = fac[r["number"]]
            r["envelope"] = [dict(segment=f"{r['number']}-S", building_part="Facade", face="S", azimuth_deg=180, orientation="S",
                                  glazing_m2=g, wall_m2=w, roof_m2=0, shading="shading coefficient of the ETTV report, area weighted",
                                  method="ETTV ratio x facade length x storey height", source=SRC["D47"]["id"], code="D",
                                  remark=f"Facade length {L} m on the S facade of storey L07, storey height 6 m (Winson Khoo, 26 Nov 2026; D-114)")]
        elif r.get("envelope"):
            r["envelope"] = []
        else:
            continue
        log.append(dict(workbook="D47", room=r["number"], change="L07 facade", before_glazing=old,
                        after_glazing=sum(e["glazing_m2"] for e in r["envelope"])))
    if not any(s_["id"] == SRC["D47"]["id"] for s_ in d["sources"]):
        d["sources"].append(dict(SRC["D47"]))


def d43_rooms(datasets: Dict[str, Dict[str, Any]], log: List[Dict[str, Any]]) -> None:
    """D43 rooms of AHU-B-07-07STAFF-02 typed by Winson Khoo (system type VAV; Innovation Lab 50 persons / 100 m2). Applied again after the
    consolidation, which places rooms that were not yet in D43 when apply() ran."""
    for n, d in datasets.items():
        for r in d["rooms"]:
            if r.get("storey") != "L07" or not str(r.get("number", "")).startswith("ITEC "):
                continue
            if r["number"] in ("ITEC 01.07.01", "ITEC 01.07.03", "ITEC 04.07.01", "ITEC 04.07.04", "ITEC 04.07.05", "ITEC 05.07.01") \
                    and r.get("system_type") != "VAV":
                r["system_type"] = "VAV"
                r.setdefault("prov", {})["system_type"] = ["D", SRC["D43"]["id"]]
                log.append(dict(workbook=f"D{n}", room=r["number"], change="system type VAV"))
            if r["number"] == "ITEC 05.07.01" and r.get("occupancy_per_100m2") != 50.0:
                r["occupancy_per_100m2"] = 50.0
                r.setdefault("prov", {})["occupancy_density"] = ["D", SRC["D43"]["id"]]
                log.append(dict(workbook=f"D{n}", room=r["number"], change="occupancy density 50 per 100 m2"))
            if not any(s_["id"] == SRC["D43"]["id"] for s_ in d["sources"]):
                d["sources"].append(dict(SRC["D43"]))


def apply(datasets: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    log: List[Dict[str, Any]] = []
    by = {n: {r["number"]: r for r in d["rooms"]} for n, d in datasets.items()}
    used = set()
    # D47
    if "47" in datasets:
        # D-121 (Marcus, 26 Nov 2026): the L07 facade measured on the elevations (D-120) replaces the facade rows typed by Winson Khoo;
        # d47_facade() is kept for reference only and is no longer applied
        _sf(datasets["47"], 0.20, SRC["D47"]["id"])
        used.add("D47")
    # D43
    if "43" in datasets:
        d, b = datasets["43"], by["43"]
        r = b.get("ITEC 01.01.01")
        if r:
            r["storey"], r["ahu"] = "L01", None
            _note(r, "Storey L01 (room number and Winson Khoo, 26 Nov 2026; D-114).")
            log.append(dict(workbook="D43", room=r["number"], change="storey L07 -> L01, AHU removed"))
        r = b.get("ITEC 01.07.03")
        if r and r.get("envelope"):
            r["envelope"] = [e for e in r["envelope"] if e.get("face") != "W"]
            log.append(dict(workbook="D43", room=r["number"], change="W facade removed"))
        for n in ("ITEC 01.07.01", "ITEC 01.07.03", "ITEC 04.07.01", "ITEC 04.07.04", "ITEC 04.07.05", "ITEC 05.07.01"):
            if n in b and b[n].get("storey") == "L07":
                b[n]["system_type"] = "VAV"
                b[n].setdefault("prov", {})["system_type"] = ["D", SRC["D43"]["id"]]
                log.append(dict(workbook="D43", room=n, change="system type VAV"))
        r = b.get("ITEC 05.07.01")
        if r:
            r["occupancy_per_100m2"] = 50.0
            r.setdefault("prov", {})["occupancy_density"] = ["D", SRC["D43"]["id"]]
            log.append(dict(workbook="D43", room=r["number"], change="occupancy density 50 per 100 m2"))
        _sf(d, 0.15, SRC["D43"]["id"])
        used.add("D43")
    # D35
    if "35" in datasets:
        d, b = datasets["35"], by["35"]
        for n, st, ahu in (("FACM 03.07.01", "L07", "AHU-B-07-07CORE-01"), ("FACM 03.01.01", "L01", None), ("FACM 03.18.01", "L18", None)):
            r = b.get(n)
            if not r:
                continue
            r["storey"], r["ahu"] = st, ahu
            if n == "FACM 03.07.01":
                r["measured_area_m2"] = 37.67
                r.setdefault("prov", {})["measured_area"] = ["D", SRC["D35"]["id"]]
                r.setdefault("soa_revit", {})["number"] = "07.IP.FACM.001"
            _note(r, f"Storey {st} from the room number (Winson Khoo, 26 Nov 2026; D-114).")
            log.append(dict(workbook="D35", room=n, change=f"storey {st}, unit {ahu}"))
        tmpl = next((r for r in d["rooms"] if r.get("criteria", {}).get("criteria_row", "").startswith("Circulation")), None) or d["rooms"][0]
        have = set(b)
        for num, arch, name, dpa, meas, ahu, sys_, sid in D35_NEW:
            if any(num == r_["number"] for x in datasets.values() for r_ in x["rooms"]):
                continue
            r = {k: copy.deepcopy(v) for k, v in tmpl.items() if k in ("space_type", "room_condition", "ceiling_height_m", "activity",
                                                                         "lighting_w_m2", "small_power_w_m2", "load_basis", "load_use")}
            r.update(number=num, name=name, storey="L07", ahu=ahu, zone_tag=None, system_type=sys_, terminal_tag=None, exhaust_fan=None,
                     dpa_area_m2=dpa, soa_area_m2=None, measured_area_m2=meas, area_selector=None, occupancy_people=None,
                     occupancy_per_100m2=0.0, iof=None, operation_hours=None, gaps={}, envelope=[], criteria=copy.deepcopy(CIRC),
                     soa_revit={"number": arch}, plan_room_id=None,
                     prov={"identity": ["D", sid or SRC["D35"]["id"]], "zoning": ["D", f"ARCH-{arch}"], "measured_area": ["D", SRC["D35"]["id"]],
                           "space_type": ["A", "ASM-DEFAULT"], "ceiling_height": ["A", "ASM-CEILING"], "occupancy_density": ["A", "OCC-DENSITY"],
                           "lighting": ["A", "ASM-LIGHT"], "small_power": ["A", "ASM-PLUG"]} | ({"dpa_area": ["D", sid]} if dpa else {}),
                     remark=(f"Added from the workbook of Winson Khoo (26 Nov 2026; D-114); architectural number {arch}. "
                             + ("MV on the mode of ventilation plan (FLL): not on an AHU (D-108)." if sys_ == "MV" else "")))
            if sid is None:
                r["prov"].pop("dpa_area", None)
            base_prov = {k: v for k, v in (tmpl.get("prov") or {}).items()
                         if k not in ("identity", "zoning", "dpa_area", "soa_area", "measured_area", "occupancy_people", "iof")}
            r["prov"] = {**base_prov, **r["prov"]}
            d["rooms"].append(r)
            aid = f"ARCH-{arch}"
            if not any(x["id"] == aid for x in d["sources"]):
                d["sources"].append(_arch_src(arch))
            if sid and not any(x["id"] == sid for x in d["sources"]):
                d["sources"].append(dict(id=sid, document="DPA_SOA.xlsx", issue="DPA", page="sheet Database",
                                         location=f"room {sid[4:6]} {sid[6:-4]}.{sid[-4:-2]}.{sid[-2:]}",
                                         text_as_read=f"{name}; unit LL Lift Lobby (as listed by Winson Khoo on the Sources sheet, 26 Nov 2026)",
                                         method="Value read from the DPA workbook, sheet Database", confidence="high"))
            log.append(dict(workbook="D35", room=num, change=f"added on {ahu or sys_}"))
        _sf(d, 0.20, SRC["D35"]["id"])
        used.add("D35")
    # every source the added or changed rooms cite must be on the Sources sheet: copy it from another workbook where missing
    pool = {}
    for x in datasets.values():
        for s_ in x.get("sources", []):
            pool.setdefault(s_["id"], s_)
    try:
        import json as _json
        from pathlib import Path as _P
        for s_ in _json.loads(_P("out/loads/inputs_common/D00.json").read_text(encoding="utf-8")).get("sources", []):
            pool.setdefault(s_["id"], s_)
    except (OSError, ValueError):
        pass
    for n in used:
        d = datasets[n[1:]]
        have = {s_["id"] for s_ in d["sources"]}
        need = set()
        for r in d["rooms"]:
            need |= {v[1] for v in (r.get("prov") or {}).values() if isinstance(v, list) and len(v) > 1}
            need.add((r.get("criteria") or {}).get("source"))
            need |= {e.get("source") for e in (r.get("envelope") or [])}
        for sid in sorted(x for x in need if x and x not in have and x in pool):
            d["sources"].append(dict(pool[sid]))
    for n in used:
        d = datasets[n[1:]]
        if not any(s["id"] == SRC[n]["id"] for s in d["sources"]):
            d["sources"].append(dict(SRC[n]))
    return log
