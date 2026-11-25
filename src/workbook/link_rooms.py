"""Rooms with no unit linked to the latest zoning (25 Nov 2026, Marcus: "Based on latest zoning, update all workbooks to establish links between
rooms still unassigned to AHUs"). D-103.

A workbook room is free when it has no unit (no AHU or FCU tag), no terminal and no system type (or 'NO INFORMATION'). The zoning is the zone
assignment register of each storey (out/registers/zone_assignment_<STOREY>.csv, after D-095 to D-102). A free room is linked in this order; the
first rule that gives a result is used:

1. Own plan room. A zoned plan room of the same storey that no workbook room cites, with the plan tag equal to the SOA Revit tag of the room (else
   every word of the plan tag in the room name), in a compatible department, and an area within a factor 2 of the brief area where both are known.
   One plan room for one room; the best score first (tag over name, then area agreement, then the same department).
2. Same plan tag. Plan rooms of the same storey and a compatible department with the same tag (cited or not): the room takes their class. Where
   these plan rooms are on more than one class, the class with the best sum of area agreement (nearest plan room of the class) and share of the
   rooms of the DPA group of the room (rule 3) on that class, plus 0.5 where the unit tag holds a word of the room name (ACUTE, RESUS).
3. DPA group. The rooms of the same workbook, storey and DPA group (department code and group number of the room ID) that are on a unit: the unit
   that serves most of them. A room that the SOA gives as mechanically ventilated (temperature 'Not applicable (MV)', no recirculation) is MV.
4. Department on the storey. As 3, with every room of the workbook on that storey.

Plan class to room: a unit tag (AHU, PAHU, FCU, CRAC, or an AHU group) is the unit of the room; MV and NV give that system type; 'not conditioned'
gives OTHER with a remark; 'no information' gives nothing (the next rule is tried). Every link is an assumption (code A), flagged on the room,
and listed in out/qa/room_links_zoning.csv with the rule, the evidence and the alternatives.
"""
from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .capture import _covers, _src
from .room_coverage import cited_ids

REG = Path("out/registers")
QA = Path("out/qa/room_links_zoning.csv")
SCOPE = {"LB3", "B3", "B2", "B1", "L01", "L02", "L03", "L04", "L05", "L06", "L07"}
TAG = re.compile(r"(?:P?AHU|FCU|CRAC|DXFCU)-[A-Z0-9][\w/-]*")
DEPT = [("icu", "04"), ("hdu", "04"), ("emerg", "11"), ("mot", "22"), ("dsot", "22"), ("asw", "22"), ("day surgery", "22"), ("endo", "23"),
        ("diagnostic", "26"), ("cath", "28"), ("lab", "29"), ("pharm", "30"), ("cssd", "41"), ("food", "45"), ("hospitality", "36"), ("mmd", "40"),
        ("training", "50"), ("server", "42"), ("itd", "43"), ("bme", "37"), ("mro", "34"), ("admin", "47"), ("admin", "31"), ("an ", "31")]


def _depts(text: str) -> set:
    t = (text or "").lower()
    out = {v for k, v in DEPT if k in t}
    m = re.search(r"\((\d{2})\)", t)
    if m:
        out.add(m.group(1))
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _area(r: Dict[str, Any]) -> float:
    for k in ("dpa_area_m2", "soa_area_m2", "measured_area_m2"):
        try:
            v = float(r.get(k) or 0)
        except (TypeError, ValueError):
            v = 0.0
        if v > 0:
            return v
    return 0.0


def _parea(p: Dict[str, str]) -> float:
    try:
        return float(p.get("area_m2") or 0)
    except ValueError:
        return 0.0


def _group(number: str) -> str:
    m = re.match(r"(\S+)\s+(\w+)\.", number or "")
    return f"{m.group(1)} {m.group(2)}" if m else (number or "")


def _target(cls: str, kind: str) -> Optional[Tuple[Optional[str], Optional[str]]]:
    """(unit, system type) for a plan class; None where the class gives nothing."""
    c = (cls or "").strip()
    k = (kind or "").strip().lower()
    if TAG.search(c):
        return c, ("FCU" if c.startswith(("FCU", "DXFCU")) else None)
    if k in ("mv", "nv") or c.upper() in ("MV", "NV"):
        return None, c.upper() if c.upper() in ("MV", "NV") else k.upper()
    if k == "not conditioned":
        return None, "OTHER"
    return None


def _soa_mv(r: Dict[str, Any]) -> bool:
    s = r.get("soa_revit") or {}
    t, rc = str(s.get("temperature") or ""), str(s.get("recirculation") or "")
    return "(MV)" in t and not rc.upper().startswith("Y")


def _zoning(root: Path) -> Dict[str, List[Dict[str, str]]]:
    csv.field_size_limit(1 << 30)
    out = defaultdict(list)
    for f in sorted((root / REG).glob("zone_assignment_*.csv")):
        with f.open(newline="", encoding="utf-8") as fh:
            for p in csv.DictReader(fh):
                out[p["storey"]].append(p)
    return out


def link(datasets: Dict[str, Dict[str, Any]], root: Path = Path(".")) -> List[Dict[str, Any]]:
    zone = _zoning(root)
    cited = set()
    for d in datasets.values():
        src = {s["id"]: s for s in d["sources"]}
        for r in d["rooms"]:
            cited |= cited_ids(r, src)
    free = [(n, r) for n, d in sorted(datasets.items()) for r in d["rooms"]
            if not r.get("ahu") and not r.get("terminal_tag") and r.get("system_type") in (None, "", "NO INFORMATION") and r.get("storey") in SCOPE]
    done: Dict[int, Dict[str, Any]] = {}

    def tagmatch(r, p) -> int:
        tag = _norm((r.get("soa_revit") or {}).get("tag"))
        if tag and _norm(p["room_name"]) == tag:
            return 2
        return 1 if _covers(p["room_name"], r.get("name") or "") > 0 else 0

    def dept_ok(n, p) -> Tuple[bool, bool]:
        dm = _depts(p["department"])
        return (not dm or n in dm or n == "00"), n in dm

    # rule 1: own plan room, one to one
    cands = []
    for i, (n, r) in enumerate(free):
        a = _area(r)
        for p in zone.get(r["storey"], []):
            if p["room_id"] in cited or _target(p["class"], p["class_kind"]) is None:
                continue
            s = tagmatch(r, p)
            ok, same = dept_ok(n, p)
            if not s or not ok:
                continue
            pa = _parea(p)
            ar = min(a, pa) / max(a, pa) if a and pa else 0.0
            if a and pa and ar < 0.5:
                continue
            cands.append((s + ar + (0.5 if same else 0.0), i, p))
    cands.sort(key=lambda x: (-x[0], x[1], x[2]["room_id"]))
    used = set()
    for s, i, p in cands:
        if i in done or p["room_id"] in used:
            continue
        used.add(p["room_id"])
        done[i] = dict(rule="1 own plan room", plan=p, cls=p["class"], kind=p["class_kind"],
                       basis=f"plan room {p['room_id']} '{p['room_name']}' ({_parea(p):.1f} m², {p['department'] or 'no department'}), not cited by any other room",
                       alt="")
    idx = {id(r): i for i, (_, r) in enumerate(free)}

    def unit_of(i):
        x = done.get(i)
        if not x:
            return None
        t = _target(x["cls"], x["kind"])
        return t[0] if t and t[0] else None

    def pools():
        grp, dep = defaultdict(Counter), defaultdict(Counter)
        for n, d in datasets.items():
            for r in d["rooms"]:
                u = r.get("ahu") if id(r) not in idx else unit_of(idx[id(r)])
                if u and TAG.search(u):
                    grp[(n, r.get("storey"), _group(r["number"]))][u] += 1
                    dep[(n, r.get("storey"))][u] += 1
        return grp, dep

    # rule 2: same plan tag on the storey; among several classes, the one the DPA group of the room uses most, else the nearest in area
    grp, dep = pools()
    for i, (n, r) in enumerate(free):
        if i in done:
            continue
        a = _area(r)
        hits = [p for p in zone.get(r["storey"], []) if _target(p["class"], p["class_kind"]) is not None and tagmatch(r, p) and dept_ok(n, p)[0]]
        if not hits:
            continue
        cls = Counter(p["class"] for p in hits)
        g = grp[(n, r["storey"], _group(r["number"]))]
        gt = sum(g.values())

        def near(c):
            return min((p for p in hits if p["class"] == c), key=lambda p: (abs(_parea(p) - a) if a else 0.0, p["room_id"]))

        words = [w for w in re.findall(r"[a-z]{4,}", (r.get("name") or "").lower()) if w not in ("room", "patient", "staff", "store", "with")]

        def named(c):
            return 0.5 if any(w[:5].upper() in c.upper() for w in words) else 0.0

        def score(c):
            pa = _parea(near(c))
            return ((min(a, pa) / max(a, pa)) if a and pa else 0.0) + (g.get(c, 0) / gt if gt else 0.0) + named(c)
        c = max(cls, key=lambda c: (score(c), cls[c], c))
        best = near(c)
        how = (f"; class chosen by area agreement ({best['room_id']}, {_parea(best):.1f} m²) plus the share of DPA group {_group(r['number'])} "
               f"on it ({g.get(c, 0)} of {gt})" + (", and a word of the room name in the tag" if named(c) else "")) if len(cls) > 1 else ""
        done[i] = dict(rule="2 same plan tag", plan=best, cls=best["class"], kind=best["class_kind"],
                       basis=f"{len(hits)} plan room(s) tagged '{best['room_name']}' on {r['storey']}, on {len(cls)} class(es){how}",
                       alt="; ".join(f"{c} ({k})" for c, k in cls.most_common() if c != best["class"]))
    # rules 3 and 4: units of the neighbours in the brief
    grp, dep = pools()
    for i, (n, r) in enumerate(free):
        if i in done:
            continue
        if _soa_mv(r):
            s = r.get("soa_revit") or {}
            done[i] = dict(rule="3 SOA mechanical ventilation", plan=None, cls="MV", kind="MV",
                           basis=f"SOA Revit {s.get('number')}: temperature '{s.get('temperature')}', recirculation '{s.get('recirculation')}'", alt="")
            continue
        for rule, pool, what in (("3 DPA group", grp[(n, r["storey"], _group(r["number"]))], f"DPA group {_group(r['number'])}"),
                                 ("4 department on the storey", dep[(n, r["storey"])], f"workbook D{n} on {r['storey']}")):
            if pool:
                u, k = pool.most_common(1)[0]
                done[i] = dict(rule=rule, plan=None, cls=u, kind="AHU",
                               basis=f"{k} of {sum(pool.values())} room(s) of {what} on a unit are on {u}",
                               alt="; ".join(f"{x} ({c})" for x, c in pool.most_common() if x != u))
                break
    # write the links
    log = []
    for i, (n, r) in enumerate(free):
        x = done.get(i)
        if not x:
            log.append(dict(workbook=f"D{n}", room=r["number"], name=r.get("name"), storey=r["storey"], rule="none", unit="", system_type="",
                            plan_room="", basis="no plan room, plan tag, DPA group or department room on a unit", alternatives=""))
            continue
        unit, st = _target(x["cls"], x["kind"]) or (None, None)
        d = datasets[n]
        p = x["plan"]
        sid = _src(d, f"ZLINK3-{r['number'].replace(' ', '')}", document=f"AHU zoning, storey {r['storey']} (zone assignment register after D-095 to D-102)",
                   issue="zoning of 25 Nov 2026", page=r["storey"], location=(f"plan room {p['room_id']}" if p else x["rule"]),
                   text_as_read=(f"'{p['room_name']}', class {p['class']}, department {p['department'] or '-'}" if p else x["basis"]),
                   method=f"Room with no unit linked to the zoning, rule {x['rule']} (D-103): {x['basis']}", confidence="medium" if x["rule"].startswith("1") else "low")
        if unit:
            r["ahu"] = unit
        if st:
            r["system_type"] = st
        if p and x["rule"].startswith("1"):
            r["plan_room_id"] = p["room_id"]
            (r.get("gaps") or {}).pop("plan", None)
        r.setdefault("prov", {})["zoning"] = ["A", sid]
        what = unit or st
        note = f"{what} from the zoning, rule {x['rule']}: {x['basis']}." + (f" Other candidates: {x['alt']}." if x["alt"] else "") + " Confirm (D-103)."
        if st == "OTHER":
            note = f"Not conditioned in the zoning ({x['basis']}). Confirm (D-103)."
        r.setdefault("gaps", {})["ahu"] = note
        r["remark"] = (r.get("remark") or "").rstrip() + " " + note
        log.append(dict(workbook=f"D{n}", room=r["number"], name=r.get("name"), storey=r["storey"], rule=x["rule"], unit=unit or "", system_type=st or "",
                        plan_room=p["room_id"] if p else "", basis=x["basis"], alternatives=x["alt"]))
    out = root / QA
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(log[0]) if log else ["workbook"])
        w.writeheader()
        w.writerows(log)
    return log


if __name__ == "__main__":                 # dry run on the inputs of the last build (nothing is written but the QA list)
    from .room_coverage import _load_files
    lg = link(_load_files(Path(".")))
    print(Counter(x["rule"] for x in lg), Counter((x["rule"][:1], bool(x["unit"]), x["system_type"]) for x in lg))
