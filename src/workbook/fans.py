"""Fans of a department (D-013): return and exhaust fans tied to the department by their tag, their schedule description and the
tender air-side schematic sheet that shows them next to the department's AHUs.

A fan is tied to a department when the area code of its tag (the part between the level and the number, for example CSSD in
RAF-B-03-CSSD-01A) belongs to the same area as the area code of one of the department's AHUs (CSSDDCN, CSSDINSP, B2PHAB, 01OPPHARM).
A fan that the same tender schematic sheet shows next to a department's AHU, but whose tag names another area (the core fans of a
storey), is not the department's. The sheet only tells a return fan which AHU it serves. The duty and standby pair is one row
('RAF-B-03-CSSD-01A/B') with the airflow of one unit. The tender airflow is the Equipment Schedule (Corr 5) value, else the figure of the
tender air-side schematic. A fan whose function the schedule does not state (HPF) is listed on Flags and not calculated.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

REG = Path("out/registers")
FAN_TYPES = {"RAF": "RETURN", "EAF": "EXHAUST", "TEAF": "EXHAUST", "TEF": "EXHAUST", "KEAF": "EXHAUST", "IEAF": "EXHAUST", "SEF": "EXHAUST",
             "OAF": "OUTDOOR AIR"}


def _norm(s: Optional[str]) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _f(v: Any) -> Optional[float]:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _read(path: Path) -> List[Dict[str, str]]:
    import csv
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def area_code(tag: str) -> str:
    parts = (tag or "").strip().split("-")
    return parts[3] if len(parts) >= 5 else ""


def area_match(a: str, b: str) -> bool:
    """Two area codes of tags belong to one area when they are equal, or when one is the start of the other (at least four characters, for example CSSD and
    CSSDDCN). A leading storey number must be the same: 02COR and 05COR are two areas. A shared word alone (COR, STAFF) is not a match."""
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return False
    da, db = re.match(r"^\d+", a), re.match(r"^\d+", b)
    if (da or db) and (da.group(0) if da else "") != (db.group(0) if db else ""):
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(re.sub(r"^\d+", "", short)) >= 4 and long_.startswith(short)


def clean_tag(tag: str) -> str:
    """'RAF-B-03-CSSD-01/A' is 'RAF-B-03-CSSD-01A'."""
    return re.sub(r"/([A-C])$", r"\1", (tag or "").strip())


def split_pair(tag: str) -> Tuple[str, str]:
    """(base, letter) of a duty or standby unit tag: 'RAF-B-03-CSSD-01A' -> ('RAF-B-03-CSSD-01', 'A'); no letter -> (tag, '')."""
    m = re.fullmatch(r"(.*\d)([A-C])", clean_tag(tag))
    return (m.group(1), m.group(2)) if m else (clean_tag(tag), "")


def ahu_units(tag: str) -> List[str]:
    """Unit tags inside a zone tag: 'AHU-X-01A/B' -> ['AHU-X-01A', 'AHU-X-01B']."""
    t = (tag or "").strip()
    m = re.fullmatch(r"(.*?)([A-Z])((?:[/&][A-Z])+)", t)
    if m:
        return [m.group(1) + x for x in [m.group(2)] + re.findall(r"[A-Z]", m.group(3))]
    return [t]


def department_fans(root: Path, ahu_tags: List[str], rooms: List[Dict[str, Any]], add_src: Callable[..., str]
                    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(fan rows, flags) for the fans that belong to the department's AHUs. Sets exhaust_fan on the rooms that an exhaust fan serves."""
    sched = _read(root / REG / "equipment_schedule_fans.csv")
    equip = [e for e in _read(root / REG / "schematic_equipment.csv") if e.get("issue_set") == "tender"]
    flags: List[Dict[str, Any]] = []
    dept_codes = {area_code(t) for t in ahu_tags if area_code(t)}
    if not dept_codes:
        return [], flags
    sheet_ahus: Dict[str, List[str]] = defaultdict(list)
    fan_sheets: Dict[str, List[str]] = defaultdict(list)
    for e in equip:
        tag = clean_tag(e["tag"])
        for t in ahu_tags:
            if any(_norm(tag) == _norm(u) for u in ahu_units(t)):
                if t not in sheet_ahus[e["sheet"]]:
                    sheet_ahus[e["sheet"]].append(t)
        if e["type"] in FAN_TYPES or e["type"] == "HPF":
            fan_sheets[_norm(tag)].append(e["sheet"])
    # candidate fans: schedule rows first, then schematic fans the schedule lacks
    cand: Dict[str, Dict[str, Any]] = {}
    for r in sched:
        tag = clean_tag(r["tag_normalised"])
        ft = r["type"]
        if not (ft in FAN_TYPES or ft == "HPF"):
            continue
        if any(area_match(area_code(tag), c) for c in dept_codes):
            cand[tag] = dict(tag=tag, ftype=ft, desc=r.get("Description", ""), loc=r.get("Location", ""), lps=_f(r.get("Min Airflow (l/s)")),
                             page=r.get("page", ""), origin="schedule", row=r)
    for e in equip:
        tag = clean_tag(e["tag"])
        if e["type"] not in FAN_TYPES or tag in cand or not re.match(r"[A-Z]+-[A-Z]-", tag):
            continue
        if any(area_match(area_code(tag), c) for c in dept_codes):
            m = re.match(r"\s*([\d,]+)\s*L/s", e.get("duty_as_written") or "")
            cand[tag] = dict(tag=tag, ftype=e["type"], desc="", loc=e.get("plant_level", ""), lps=float(m.group(1).replace(",", "")) if m else None,
                             page=e.get("page", ""), origin="schematic", sheet=e["sheet"], row=e)
    # one row per duty and standby pair
    groups: Dict[str, List[str]] = defaultdict(list)
    for tag in sorted(cand):
        base, letter = split_pair(tag)
        groups[base].append(letter)
    rows: List[Dict[str, Any]] = []
    for base, letters in groups.items():
        first = cand[base + sorted(letters)[0]] if letters != [""] else cand[base]
        tag = base + "/".join(sorted(x for x in letters if x)) if any(letters) else base
        ftype = first["ftype"]
        if ftype == "HPF":
            flags.append(dict(type="VERIFY", ref=tag, text=f"Fan {tag} ({first['desc'] or 'no description'}, {first['loc']}) is tied to this department by its tag. "
                              "Its function is not stated, so it is not in the Fans tab.", source=""))
            continue
        ctype = FAN_TYPES[ftype]
        lps = first["lps"]
        if first["origin"] == "schedule":
            r = first["row"]
            sid = add_src(f"EQS5F-{_norm(tag)}"[:60], document="Equipment Schedule, fan schedule (SCH010)", issue="Corr 5", page=f"page {first['page']}",
                          location=f"tag {r['tag_as_written']}; {first['loc']}", text_as_read=f"{r['tag_as_written']}: {first['desc']}; minimum airflow {lps:g} L/s"
                          if lps is not None else f"{r['tag_as_written']}: {first['desc']}; no airflow printed",
                          method="Value read from the Equipment Schedule table", confidence="high")
            src_text = f"Equipment Schedule Corr 5, page {first['page']}, {r['tag_as_written']}"
        else:
            sid = add_src(f"TSCH-{_norm(tag)}"[:60], document=f"Air-side schematic {first['sheet']}", issue="tender issue", page=f"page {first['page']}",
                          location=f"fan {tag}", text_as_read=f"{tag}: {lps:g} L/s" if lps is not None else tag, method="Value read from the text printed on the air-side schematic",
                          confidence="medium")
            src_text = f"tender air-side schematic {first['sheet']}, {tag}"
        serves, serves2, why = tag, None, ""
        if ctype in ("RETURN", "OUTDOOR AIR"):
            sheets = fan_sheets.get(_norm(base + sorted(letters)[0]), []) if letters != [""] else fan_sheets.get(_norm(base), [])
            near: List[str] = []
            for s in sheets:
                for a in sheet_ahus.get(s, []):
                    if a not in near:
                        near.append(a)
            if not near:
                near = [t for t in ahu_tags if area_match(area_code(base), area_code(t))]
            if near:
                serves = near[0]
                serves2 = near[1] if len(near) > 1 else None
                why = "serves " + " and ".join(near[:2]) + " (shown with them on the tender air-side schematic)"
            else:
                flags.append(dict(type="VERIFY", ref=tag, text=f"Fan {tag} is tied to this department by its tag, but no AHU of the department is shown with it. "
                                  "Its required airflow is not calculated.", source=sid))
        else:
            served = _exhaust_rooms(first, near=[a for s in fan_sheets.get(_norm(base + (sorted(letters)[0] if letters != [""] else "")), [])
                                                for a in sheet_ahus.get(s, [])], rooms=rooms, tag=tag)
            if served:
                for rm in served:
                    if not rm.get("exhaust_fan"):
                        rm["exhaust_fan"] = tag
                        rm["prov"]["exhaust_fan"] = ["D", sid]
                why = f"exhaust of {len(served)} room(s) of this workbook"
            else:
                flags.append(dict(type="VERIFY", ref=tag, text=f"Fan {tag} ({first['desc'] or 'no description'}) is tied to this department by its tag, but no room of this workbook "
                                  "is tied to it. Its required airflow is not calculated.", source=sid))
        row = dict(tag=tag, type=ctype, serves=serves, scheduled_lps=lps, code="D", source=sid, tender_source_text=src_text if lps is not None else None,
                   remark="; ".join(x for x in (first["desc"], first["loc"], why) if x))
        if serves2:
            row["serves2"] = serves2
        rows.append(row)
    return rows, flags


def _exhaust_rooms(fan: Dict[str, Any], near: List[str], rooms: List[Dict[str, Any]], tag: str) -> List[Dict[str, Any]]:
    """Rooms that an exhaust fan serves: toilet rooms of the same storey for a toilet exhaust fan, else the rooms of the AHU shown with it."""
    desc = (fan.get("desc") or "").lower()
    loc = (fan.get("loc") or "")
    if re.search(r"toilet|\bwc\b", desc):
        lvl = re.search(r"\b(B\d|L\d+|\d)\b", loc)
        out = [r for r in rooms if re.search(r"toilet|\bwc\b", (r.get("name") or "").lower())]
        if lvl:
            code = lvl.group(1)
            want = {code, f"L{int(code):02d}" if code.isdigit() else code}
            out = [r for r in out if r["storey"] in want or r["storey"].lstrip("L0") == code.lstrip("L0")] or out
        return out
    if near:
        return [r for r in rooms if r.get("ahu") in near]
    return []
