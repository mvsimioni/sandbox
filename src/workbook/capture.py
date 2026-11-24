"""Room capture (D-092): every plan room that the zoning puts on a unit is represented by a workbook room on that unit.

Step 1, link by zoning: a workbook room with no unit takes the unit of a plan room of the same storey (and of its department where the zoning names
the department number) when the plan name, with its abbreviations expanded, is fully contained in the room name. One room for one plan room. The link
is assumed (code A) and flagged.

Step 2, rooms from the drawing: a zoned plan room that is still not represented is added as a room of the workbook that holds its unit (else the
workbook of its department, else D00). It takes the plan outline and area, and the criteria and loads of the closest room of the same name on the
same unit where one exists. It is flagged: it may duplicate a room of the brief under another name.
"""

from __future__ import annotations

import copy
import csv
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

REG = Path("out/registers")

CANON = {"ward": "room", "rooms": "room", "storeroom": "store", "storage": "store", "stores": "store", "cleaners": "cleaner", "circulation": "corridor",
         "handwash": "handwashing", "iso": "isolation", "offices": "office", "consultation": "consult", "consulting": "consult", "proc": "procedure",
         "ante": "anteroom", "waiting": "wait", "preparation": "prep", "medicine": "medication", "meds": "medication", "x": "xray", "ray": "xray",
         "lobbies": "lobby", "bays": "bay", "utilities": "utility", "off": "office", "str": "store", "corr": "corridor", "mtg": "meeting",
         "cln": "cleaner"}
PLAN = {"1b": [("1", "bed")], "2b": [("2", "bed")], "4b": [("4", "bed")], "5b": [("5", "bed")], "6b": [("6", "bed")],
        "pt": [("patient",)], "hw": [("handwashing",)], "me": [("mobile", "equipment")], "ar": [("anteroom",), ("anaesthesia",)], "al": [("airlock",)],
        "ss": [("staff", "station"), ("satellite",), ("supply", "station")], "str": [("store",)], "off": [("office",)], "corr": [("corridor",)],
        "cln": [("cleaner",), ("clean",)], "mtg": [("meeting",)], "cu": [("clean", "utility")], "du": [("dirty", "utility")], "ctrl": [("control",)],
        "chg": [("change",)], "vdr": [("vendor",)], "ptube": [("pneumatic", "tube")], "med": [("medication",)], "ll": [("lift", "lobby")],
        "pll": [("lift", "lobby")], "boh": [("back", "house")], "tech": [("technician",), ("technical",)], "multi": [("multipurpose",), ("multi",)],
        "hc": [("accessible",)], "san": [("sanitary",), ("toilet",)], "chng": [("change",)], "recov": [("recovery",)], "resus": [("resuscitation",)],
        "consult": [("consult",)], "xray": [("xray",)], "agv": [("agv",)], "wait": [("wait",)], "prep": [("prep",)]}
SKIP = {"i", "the", "and", "of", "a", "b", "type"}


def _tok(name: str) -> List[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+", (name or "").lower().replace("x-ray", "xray")):
        w = CANON.get(w, w)
        if w not in SKIP:
            out.append(w)
    return out


def _covers(plan_name: str, room_name: str) -> float:
    """1.0 when every word of the plan name (abbreviations expanded) is in the room name; else 0. Ties are broken by the share of room words used."""
    rt = set(_tok(room_name))
    if not rt:
        return 0.0
    used = set()
    pw = [w for w in re.findall(r"[a-z0-9]+", (plan_name or "").lower().replace("x-ray", "xray")) if w not in SKIP]
    if not pw:
        return 0.0
    for w in pw:
        alts = PLAN.get(w, [(CANON.get(w, w),)])
        hit = next((a for a in alts if set(a) <= rt), None)
        if hit is None:
            return 0.0
        used |= set(hit)
    return 1.0 + len(used) / max(len(rt), 1) * 0.01


def _dept_of(zone_dept: str) -> str:
    m = re.search(r"\((\d{2})\)", zone_dept or "")
    return m.group(1) if m else ""


def _src(d: Dict[str, Any], sid: str, **kw) -> str:
    if not any(s["id"] == sid for s in d["sources"]):
        d["sources"].append(dict(id=sid, **kw))
    return sid


def capture(datasets: Dict[str, Dict[str, Any]], root: Path = Path(".")) -> Dict[str, Any]:
    from .room_coverage import run as coverage
    from . import envelope as envm
    rows = coverage(root, datasets, write=False)
    todo = [r for r in rows if not r["represented_by"]]
    linked, added = [], []
    # step 1: link rooms with no unit by name, same storey (and department where the zoning names it)
    free = defaultdict(list)
    for n, d in datasets.items():
        for r in d["rooms"]:
            if not r.get("ahu"):
                free[r["storey"]].append((n, r))
    taken = set()
    rest = []
    for p in sorted(todo, key=lambda x: (x["storey"], x["plan_room"])):
        dn = _dept_of(p["department"])
        best, bs = None, 0.0
        for n, r in free[p["storey"]]:
            if id(r) in taken or (dn and n != dn and n != "00"):
                continue
            s = _covers(p["plan_name"], r.get("name") or "")
            if s > bs:
                best, bs = (n, r), s
        if best is None:
            rest.append(p)
            continue
        n, r = best
        taken.add(id(r))
        d = datasets[n]
        sid = _src(d, f"ZLINK-{p['storey']}-{p['plan_room']}", document=f"AHU zoning, storey {p['storey']}", issue="zoning assignment", page=p["storey"],
                   location=f"plan room {p['plan_room']}", text_as_read=f"'{p['plan_name']}', class {p['unit']}, department {p['department']}",
                   method="Room linked to the plan room by name on the same storey (abbreviations expanded); unit from the zoning (D-092)", confidence="low")
        r["ahu"] = p["unit"]
        r["system_type"] = r.get("system_type") or ("FCU" if p["unit"].startswith("FCU") else None)
        r.setdefault("prov", {})["zoning"] = ["A", sid]
        r["plan_room_id"] = p["plan_room"]
        r.setdefault("gaps", {})["ahu"] = (f"Unit {p['unit']} from plan room {p['plan_room']} ('{p['plan_name']}') by name on storey {p['storey']}. "
                                           "The plan room is not linked to this room by any other document. Confirm (D-092).")
        r["remark"] = (r.get("remark") or "").rstrip() + f" Unit {p['unit']} from the zoning of plan room {p['plan_room']} by name, to confirm (D-092)."
        linked.append(dict(workbook=f"D{n}", room=r["number"], name=r.get("name"), plan_room=p["plan_room"], plan_name=p["plan_name"], unit=p["unit"]))
    # step 2: rooms from the drawing
    env = envm.Envelope(root)
    polys = {}
    for st in {p["storey"] for p in rest}:
        f = root / REG / f"room_polygons_{st}.csv"
        if f.exists():
            with f.open(newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    polys[r["room_id"]] = r
    holder = defaultdict(lambda: defaultdict(int))
    for n, d in datasets.items():
        for r in d["rooms"]:
            if r.get("ahu"):
                holder[r["ahu"]][n] += 1
    ch_default = None
    try:
        from . import conf as _c
        ch_default = float((_c.config(root).get("ceiling_height") or {}).get("flagged_default_m") or 0) or None
    except Exception:
        pass
    for p in rest:
        h = holder.get(p["unit"])
        dn = _dept_of(p["department"])
        n = max(h, key=lambda k: (h[k], k != "00")) if h else (dn if dn in datasets else "00")
        d = datasets[n]
        pr = polys.get(p["plan_room"]) or {}
        tag_level = bool(re.search(r"-T\d+$", p["plan_room"]))       # a tag inside an open-plan region: the region outline is not this room
        area = None
        try:
            area = float(p["area_m2"] or 0) or None
        except ValueError:
            area = None
        if area is None and not tag_level:
            try:
                area = float(pr.get("a_area_m2") or 0) or None
            except ValueError:
                area = None
        number = f"PLAN-{p['plan_room']}"
        sid = _src(d, f"PLAN-{p['plan_room']}", document=f"Architectural plan, storey {p['storey']} (room outline and tag)", issue="as issued", page=p["storey"],
                   location=f"plan room {p['plan_room']}", text_as_read=f"tag '{p['plan_name']}', {area or '?'} m², class {p['unit']} in the zoning",
                   method="Room outline and tag read from the plan; unit from the zoning (D-092)", confidence="medium")
        tmpl = None
        best = 0.0
        for r in d["rooms"]:
            if r.get("ahu") == p["unit"] and not str(r["number"]).startswith("PLAN-"):
                s = _covers(p["plan_name"], r.get("name") or "")
                if s > best:
                    tmpl, best = r, s
        room = dict(number=number, name=p["plan_name"], storey=p["storey"], ahu=p["unit"], zone_tag=None,
                    system_type="FCU" if p["unit"].startswith("FCU") else None, terminal_tag=None, exhaust_fan=None, space_type="UNCLASSIFIED",
                    room_condition="Comfort", dpa_area_m2=None, soa_area_m2=None, measured_area_m2=area, area_selector=None,
                    ceiling_height_m=(tmpl or {}).get("ceiling_height_m") or ch_default, occupancy_people=None, occupancy_per_100m2=None, iof=None,
                    activity=None, operation_hours=None, lighting_w_m2=None, small_power_w_m2=None, gaps={}, criteria={}, envelope=[],
                    prov={"identity": ["D", sid], "zoning": ["D", sid], "measured_area": ["M", sid], "space_type": ["A", "ASM-DEFAULT"]},
                    plan_room_id=p["plan_room"],
                    remark=(f"Room from the drawing (D-092): plan room {p['plan_room']} '{p['plan_name']}' on {p['unit']} in the zoning. No room of the brief "
                            "is linked to it. It may duplicate a room of the brief under another name. Confirm."))
        if tmpl is not None:
            for k in ("space_type", "room_condition", "criteria", "occupancy_per_100m2", "occupancy_basis", "activity", "lighting_w_m2", "small_power_w_m2",
                      "load_basis", "load_use", "exhaust_fan", "system_type", "zone_tag", "terminal_tag"):
                if tmpl.get(k) is not None:
                    room[k] = copy.deepcopy(tmpl[k])
            for k in ("criteria", "space_type", "activity", "lighting", "small_power", "occupancy_density", "exhaust_fan"):
                if k in (tmpl.get("prov") or {}):
                    room["prov"][k] = ["A", tmpl["prov"][k][1]]
            room["remark"] += f" Criteria and loads as room {tmpl['number']} ('{tmpl.get('name')}') on the same unit, assumed."
        else:
            room["gaps"]["criteria"] = "No room of the same name on this unit gives criteria or loads. Give the room type."
        if ch_default and not (tmpl or {}).get("ceiling_height_m"):
            room["prov"]["ceiling_height"] = ["A", "ASM-CEILING"]
        if tag_level:
            room["gaps"]["area"] = "The room is a tag inside an open-plan region of the plan: its own area and outline are not measured. Give the area."
        elif pr.get("polygon_building_mm_wkt") and p["storey"] in envm.ENVELOPE_STOREYS:
            room["envelope"] = env.rows(p["storey"], number, pr["polygon_building_mm_wkt"], "ETTV-ENV")
            room["_geom"] = True
        d["rooms"].append(room)
        if p["storey"] not in d["storeys"]:
            from .consolidate import STOREY_ORDER
            d["storeys"].append(p["storey"])
            d["storeys"].sort(key=lambda x: STOREY_ORDER.index(x) if x in STOREY_ORDER else 99)
            d.setdefault("storey_labels", {})[p["storey"]] = p["storey"]
        d.setdefault("flags", []).append(dict(type="VERIFY", ref=number, source=sid,
                                              text=f"Room {number} ('{p['plan_name']}', {area or '?'} m²) is added from the plan: it is on {p['unit']} in the zoning and no room of the brief is linked to it. Confirm it is not a duplicate."))
        added.append(dict(workbook=f"D{n}", room=number, name=p["plan_name"], unit=p["unit"], area_m2=area, template=(tmpl or {}).get("number")))
    return dict(linked=linked, added=added)


def ensure_unit_rows(datasets: Dict[str, Dict[str, Any]]) -> List[str]:
    """Every unit on a room of a workbook has a unit row there: copied from another workbook, else a row with no tender airflow (flagged)."""
    rows_any = {}
    for d in datasets.values():
        for a in d.get("ahus") or []:
            rows_any.setdefault(a["tag"], a)
    made = []
    for n, d in datasets.items():
        have = {a["tag"] for a in d.get("ahus") or []}
        for t in sorted({r.get("ahu") for r in d["rooms"] if r.get("ahu")} - have):
            if not re.match(r"^(P?AHU|FCU)-", t):
                continue
            if t in rows_any:
                a = copy.deepcopy(rows_any[t])
                a["other_rooms"] = 0
            else:
                a = dict(tag=t, type="FCU" if t.startswith("FCU") else ("PAHU" if t.startswith("PAHU") else "AHU"), sched_oa_lps=None, sched_supply_lps=None,
                         note="The unit is on rooms linked by the zoning (D-092). No tender airflow is set for it in this workbook. VERIFY.")
                made.append(f"D{n} {t}")
            d.setdefault("ahus", []).append(a)
    return made
