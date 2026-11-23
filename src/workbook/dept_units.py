"""Department -> DPA unit -> IP storey.

Order of evidence (first that gives a storey wins), recorded per unit in the result:
1. ``dept_units.json`` (explicit entry, with its evidence text);
2. the DPA unit name equals a stacking-diagram IP-block cell of the department (``ip_units`` of ``assemble``);
3. a governing SOA unit code of the department (``soa_rooms.csv`` dept_no + unit_zone_code) that is a DPA unit
   (``fpu_department_no``), when the department has exactly one IP storey in LB3-L07 in the stacking diagram. A unit whose name is the text of a
   cell of the OP block of the stacking diagram is not an IP unit and is listed as excluded.
A unit with none of these is returned in ``unresolved`` and is not used.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List

TABLE = Path(__file__).with_name("dept_units.json")


def load_raw() -> Dict[str, Any]:
    return json.loads(TABLE.read_text(encoding="utf-8"))


def plan_regex(key: str) -> str:
    return (load_raw().get("plan_departments", {}).get(key) or {}).get("regex", "")


def load_table() -> Dict[str, List[Dict[str, Any]]]:
    return json.loads(TABLE.read_text(encoding="utf-8"))["entries"]


def resolve_units(key: str, stack: List[Dict[str, str]], dpa_units: Dict[str, List[Dict[str, str]]], soa_unit_codes: set,
                  name_match: Callable[[], Dict[str, str]], storey_code: Callable[[str], str], dept_key: Callable[[str], str],
                  scope: tuple) -> Dict[str, Any]:
    entries = load_table().get(key, [])
    units: Dict[str, Dict[str, Any]] = {}
    excluded: List[Dict[str, str]] = []
    for e in entries:
        if e.get("exclude"):
            excluded.append(dict(unit=e["unit"], reason=e["reason"]))
            continue
        if e["unit"] not in dpa_units:
            continue
        if e["storey"] not in scope:
            continue
        units[e["unit"]] = dict(storey=e["storey"], basis="dept_units.json", evidence=e["evidence"],
                                room_name_regex=e.get("room_name_regex"), exclude_room_name_regex=e.get("exclude_room_name_regex"),
                                exclude_group_regex=e.get("exclude_group_regex"))
    explicit_units = {e["unit"] for e in entries}
    for code, st in name_match().items():
        if code not in units and code not in explicit_units and st in scope:
            units[code] = dict(storey=st, basis="DPA unit name = stacking-diagram IP cell",
                               evidence="DPA unit name equals the department text of an IP-block cell of the stacking diagram")
    storeys = sorted({storey_code(s["storey"]) for s in stack if s.get("dept_no") and key in [dept_key(x) for x in s["dept_no"].split(";")]
                      and s["block"].startswith("IP")} & set(scope))
    unresolved: List[str] = []
    norm = lambda t: re.sub(r"[^a-z0-9]", "", (t or "").lower())
    op_cells = {norm(s.get("department_as_written")) for s in stack if s.get("dept_no") and key in [dept_key(x) for x in s["dept_no"].split(";")]
                and not s["block"].startswith("IP")}
    for code in sorted(soa_unit_codes - {""}):
        if code in units or code in explicit_units or code not in dpa_units:
            continue
        if norm(dpa_units[code][0].get("fpu_department_name")) in op_cells:        # the unit name is the text of a cell of the OP block: not an IP unit
            excluded.append(dict(unit=code, reason="DPA unit name equals a stacking-diagram cell of the OP block."))
            continue
        if len(storeys) == 1:
            units[code] = dict(storey=storeys[0], basis="SOA unit code + stacking diagram storey",
                               evidence=f"SOA unit {code} of department {key}; the stacking diagram has one IP storey for the department ({storeys[0]})")
        else:
            unresolved.append(code)
    return dict(units=units, excluded=excluded, unresolved=unresolved)


def keep_room(info: Dict[str, Any], room_name: str, group_name: str) -> bool:
    if info.get("room_name_regex") and not re.search(info["room_name_regex"], room_name or "", re.I):
        return False
    if info.get("exclude_room_name_regex") and re.search(info["exclude_room_name_regex"], room_name or "", re.I):
        return False
    if info.get("exclude_group_regex") and re.search(info["exclude_group_regex"], group_name or "", re.I):
        return False
    return True
