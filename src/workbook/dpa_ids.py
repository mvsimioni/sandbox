"""Room IDs from the brief only (D-093): every room of a workbook carries a DPA (or SOA) unique room identifier.

Appendix D and the plans do not give room IDs. A space read from them (App D space 'CA-...', plan room 'L06-R...', room added from the drawing
'PLAN-...') is matched to the brief, in this order:

1. same storey, same unit, a brief room already in a workbook whose name fits: the space is the same room. It is merged into the brief room, which
   keeps its ID and takes the unit, outline, facade and measured area of the space where it has none;
2. same storey, a DPA room not yet in a workbook whose name fits: the space becomes that DPA room (ID, name and design area from the DPA);
3. as 1 and 2, by area (within 10 %) and at least one word in common.

The storey of a DPA room is the storey in its ID, else the storey of its unit (the storey of the rooms of that unit in the workbooks, else the
department units table). A space with no match is taken out of the workbooks and listed in out/qa/spaces_without_brief_room.csv.
"""

from __future__ import annotations

import copy
import csv
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .capture import _covers, _tok

REG = Path("out/registers")
SCOPE = {"LB3", "B3", "B2", "B1", "L01", "L02", "L03", "L04", "L05", "L06", "L07"}


def _names(r: Dict[str, Any]) -> List[str]:
    return [n.strip() for n in re.split(r"\s/\s|\|", r.get("name") or "") if n.strip()] or [r.get("name") or ""]


def _area(r: Dict[str, Any]) -> float:
    for k in ("dpa_area_m2", "soa_area_m2", "measured_area_m2"):
        v = r.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return 0.0


def _size_ok(a: float, b: float) -> bool:
    """A name match also needs a comparable size: within a factor of 2 when both areas are known (an open-plan region is not one room)."""
    return not (a > 0 and b > 0) or max(a, b) <= 2.0 * min(a, b)


def _shares_word(names: List[str], other: str) -> bool:
    ow = set(_tok(other))
    return any(set(_tok(n)) & ow for n in names)


def _dpa_src(d: Dict[str, Any], x: Dict[str, str]) -> str:
    sid = f"DPA-{re.sub(r'[^A-Za-z0-9]', '', x['unique_room_identifier'])}"[:60]
    if not any(s["id"] == sid for s in d["sources"]):
        d["sources"].append(dict(id=sid, document=Path(x["source_file"]).name, issue="DPA", page=f"row {x['row']}", location=f"room {x['unique_room_identifier']}",
                                 text_as_read=f"{x['room_name']}; designed {x['designed_area_m2']} m²; unit {x['fpu_department_no']} {x['fpu_department_name']}",
                                 method="Value read from the DPA workbook, sheet Database", confidence="high"))
    return sid


def _renumber(d: Dict[str, Any], room: Dict[str, Any], new: str) -> None:
    old = room["number"]
    room["number"] = new
    for e in room.get("envelope") or []:
        e["segment"] = str(e.get("segment", "")).replace(old, new, 1)
    p = room.get("pressure")
    if isinstance(p, dict) and p.get("suite") == old:
        p["suite"] = new
    for f in d.get("flags") or []:
        if f.get("ref") == old:
            f["ref"] = new


def rebase_ids(datasets: Dict[str, Dict[str, Any]], root: Path = Path(".")) -> Dict[str, Any]:
    with (root / REG / "dpa_rooms.csv").open(newline="", encoding="utf-8") as fh:
        dpa = {r["unique_room_identifier"]: r for r in csv.DictReader(fh)}
    in_wb = {r["number"]: (n, r) for n, d in datasets.items() for r in d["rooms"]}
    # storey of each DPA unit, from the brief rooms already in the workbooks
    us: Dict[str, Counter] = defaultdict(Counter)
    for num, (n, r) in in_wb.items():
        x = dpa.get(num)
        if x:
            us[x["fpu_department_no"]][r["storey"]] += 1
    try:
        from .dept_units import load_table
        for key, entries in load_table().items():
            for e in entries:
                if not e.get("exclude") and e.get("storey"):
                    us[e["unit"]][e["storey"]] += 1
    except Exception:
        pass
    unit_storey = {u: c.most_common(1)[0][0] for u, c in us.items() if c}

    def storey_of(x):
        return x.get("storey_from_identifier") or unit_storey.get(x["fpu_department_no"], "")

    free: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for x in dpa.values():
        st = storey_of(x)
        if st in SCOPE and x["unique_room_identifier"] not in in_wb:
            free[st].append(x)
    brief: Dict[Tuple[str, str], List[Tuple[str, Dict[str, Any]]]] = defaultdict(list)
    for n, d in datasets.items():
        for r in d["rooms"]:
            if r["number"] in dpa:
                brief[(r["storey"], r.get("ahu") or "")].append((n, r))
    spaces = [(n, r) for n, d in datasets.items() for r in d["rooms"] if r["number"] not in dpa]
    taken_free, taken_brief = set(), set()
    plan: Dict[int, Tuple[str, Any]] = {}

    def pass_dup(by_area: bool):
        for n, r in spaces:
            if id(r) in plan or not r.get("ahu"):
                continue
            nm, a = _names(r), _area(r)
            best, bd = None, None
            for bn, b in brief[(r["storey"], r["ahu"])] + [z for z in brief[(r["storey"], "")] if not z[1].get("ahu")]:   # same unit, or a brief room with no unit yet
                if id(b) in taken_brief:
                    continue
                if not by_area:
                    ok = any(_covers(x, b.get("name") or "") > 0 for x in nm) and _size_ok(a, _area(b))
                else:
                    ba = _area(b)
                    ok = a > 0 and ba > 0 and abs(ba - a) <= 0.1 * max(a, ba) and _shares_word(nm, b.get("name") or "")
                if ok:
                    dist = abs(_area(b) - a)
                    if bd is None or dist < bd:
                        best, bd = (bn, b), dist
            if best:
                taken_brief.add(id(best[1]))
                plan[id(r)] = ("merge", best, "name" if not by_area else "area")

    def pass_free(by_area: bool):
        for n, r in spaces:
            if id(r) in plan:
                continue
            nm, a = _names(r), _area(r)
            best, bd = None, None
            for x in free.get(r["storey"], []):
                if x["unique_room_identifier"] in taken_free:
                    continue
                try:
                    xa = float(x["designed_area_m2"] or 0)
                except ValueError:
                    xa = 0.0
                if not by_area:
                    ok = any(_covers(y, x["room_name"]) > 0 for y in nm) and _size_ok(a, xa)
                else:
                    ok = a > 0 and xa > 0 and abs(xa - a) <= 0.1 * max(a, xa) and _shares_word(nm, x["room_name"])
                if ok:
                    dist = abs(xa - a) if a and xa else 1e9
                    if bd is None or dist < bd:
                        best, bd = x, dist
            if best:
                taken_free.add(best["unique_room_identifier"])
                plan[id(r)] = ("become", best, "name" if not by_area else "area")

    pass_dup(False)
    pass_free(False)
    pass_dup(True)
    pass_free(True)

    merged, became, removed = [], [], []
    for n, r in spaces:
        d = datasets[n]
        act = plan.get(id(r))
        old, oname = r["number"], r.get("name")
        m_ = re.match(r"CA-(\w+)-(\d+)$", old)
        if m_:
            r["appd_space"] = [m_.group(1), str(int(m_.group(2)))]
        if act and act[0] == "become":
            x, how = act[1], act[2]
            sid = _dpa_src(d, x)
            _renumber(d, r, x["unique_room_identifier"])
            r["name"] = x["room_name"]
            try:
                r["dpa_area_m2"] = float(x["designed_area_m2"] or 0) or None
            except ValueError:
                r["dpa_area_m2"] = None
            r["soa_area_m2"] = None
            r["area_selector"] = None
            r["prov"]["identity"] = ["D", sid]
            if r["dpa_area_m2"]:
                r["prov"]["dpa_area"] = ["D", sid]
            r["prov"].pop("soa_area", None)
            r["prov"].pop("area_selector", None)
            r["remark"] = (f"DPA room {x['unique_room_identifier']} '{x['room_name']}' ({x['fpu_department_name']}). Drawing space {old} '{oname}' matched by {how} "
                           f"on storey {r['storey']} (D-093). " + (r.get("remark") or ""))
            r.setdefault("gaps", {})["identity"] = f"Room matched to the brief by {how} from drawing space {old} '{oname}'. Confirm (D-093)."
            became.append(dict(workbook=f"D{n}", space=old, space_name=oname, dpa_id=x["unique_room_identifier"], dpa_name=x["room_name"], how=how))
        elif act and act[0] == "merge":
            (bn, b), how = act[1], act[2]
            bd = datasets[bn]
            if not b.get("ahu") and r.get("ahu"):
                b["ahu"] = r["ahu"]
                b["prov"]["zoning"] = r["prov"].get("zoning") or b["prov"].get("zoning")
            if not b.get("envelope") and r.get("envelope"):
                rows = copy.deepcopy(r["envelope"])
                for e in rows:
                    e["segment"] = str(e.get("segment", "")).replace(old, b["number"], 1)
                b["envelope"] = rows
                b["_geom"] = True
            elif r.get("_geom"):
                b["_geom"] = True
            if b.get("measured_area_m2") is None and r.get("measured_area_m2") and not str(old).startswith("PLAN-"):
                b["measured_area_m2"] = r["measured_area_m2"]
                if "measured_area" in r["prov"]:
                    b["prov"]["measured_area"] = r["prov"]["measured_area"]
            if r.get("plan_room_id") and not b.get("plan_room_id"):
                b["plan_room_id"] = r["plan_room_id"]
            if r.get("appd_space"):
                b.setdefault("appd_spaces", []).append(r["appd_space"])
            for k, v in (r.get("prov") or {}).items():           # sources cited by what the brief room takes over
                if isinstance(v, (list, tuple)) and len(v) == 2 and bn != n:
                    from .capture import copy_sources
                    copy_sources(d, bd, v[1])
            if bn != n:
                from .capture import copy_sources
                copy_sources(d, bd, b.get("envelope"))
            b["remark"] = (b.get("remark") or "").rstrip() + f" Same room as drawing space {old} '{oname}' (matched by {how}, D-093): its unit, outline and facade are used here."
            d["rooms"] = [z for z in d["rooms"] if z is not r]
            d["flags"] = [f for f in d.get("flags") or [] if f.get("ref") != old]
            merged.append(dict(workbook=f"D{n}", space=old, space_name=oname, brief_room=b["number"], brief_workbook=f"D{bn}", brief_name=b.get("name"), how=how))
        else:
            d["rooms"] = [z for z in d["rooms"] if z is not r]
            d["flags"] = [f for f in d.get("flags") or [] if f.get("ref") != old]
            removed.append(dict(workbook=f"D{n}", space=old, name=oname, storey=r["storey"], unit=r.get("ahu") or r.get("system_type") or "",
                                area_m2=round(_area(r), 1), plan_room=r.get("plan_room_id") or "",
                                envelope_m2=round(sum(float(e.get("glazing_m2") or 0) + float(e.get("wall_m2") or 0) for e in r.get("envelope") or []), 1)))
    out = root / "out/qa/spaces_without_brief_room.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    if removed:
        with out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(removed[0]))
            w.writeheader()
            w.writerows(removed)
    return dict(became=became, merged=merged, removed=removed)
