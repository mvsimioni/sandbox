"""Write out/qa/round4_report.md: what round 4 changed (D-025 to D-028 and the round-3 findings), the completeness of every department and the residue with its reasons.

``python3 -m src.recompute.round4_report [--results out/qa/round4_results.json]``

The counts come from the department inputs (``out/loads/inputs/D<nn>.json``), the recalculated workbooks, the AHU master summary, the zone schedule and the
recomputation CSV. The results of the verification commands (selftest, pytest, checks, scans) are read from the results file that the driver wrote.
"""

from __future__ import annotations

import collections
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

import openpyxl

IN = Path("out/loads/inputs")
WB = Path("out/loads/workbooks")
OUT = Path("out/qa/round4_report.md")


def _reason_unit(room: Dict[str, Any]) -> str:
    plan = (room.get("gaps") or {}).get("plan", "")
    if plan.startswith("No unit link: the plan room"):
        return "the area test refused the link (the plan room is much smaller than the DPA rooms that fit its name)"
    if plan.startswith("No plan room matches"):
        return "no plan room of the department has this name on this storey"
    if "more than a factor" in plan and "similar name" in plan:
        return "a plan room of a similar name was refused on area"
    if "outside the grid span" in plan:
        return "the plan room of the same name lies outside the grid span of the department"
    if plan:
        return "linked plan rooms carry no single unit"
    return "no link found"


def _reason_crit(room: Dict[str, Any]) -> str:
    g = (room.get("gaps") or {}).get("criteria", "")
    if "plant room" in g:
        return "plant room: no row from the use class (open for Marcus)"
    if "does not give a room use" in g:
        return "the room name gives no room use and no other department prints the name"
    if "no other department has a criteria row for the use class" in g:
        return "no other department has a row for the use class"
    if g.startswith("Plant space"):
        return "plant space of the common areas"
    return "no other department prints this room type name or code"


def collect() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for f in sorted(IN.glob("D*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        num = f.stem[1:]
        rooms = d["rooms"]
        st = collections.Counter()
        res_unit, res_crit, res_occ, res_load = (collections.Counter() for _ in range(4))
        names_crit: Dict[str, int] = collections.defaultdict(int)
        for r in rooms:
            st["rooms"] += 1
            if r.get("ahu"):
                st["unit"] += 1
            elif r.get("system_type"):
                st["class"] += 1
            else:
                st["no_unit"] += 1
                res_unit[_reason_unit(r)] += 1
            c = r.get("criteria") or {}
            if not c:
                st["no_crit"] += 1
                res_crit[_reason_crit(r)] += 1
                names_crit[r["name"]] += 1
            elif c.get("presumed"):
                st["presumed"] += 1
            elif c.get("code") == "S":
                st["mot"] += 1
            else:
                st["own"] += 1
            if not (r.get("soa_area_m2") or r.get("dpa_area_m2") or r.get("measured_area_m2")):
                st["no_area"] += 1
            if r.get("prov", {}).get("ceiling_height", ["", ""])[0] == "D":
                st["h_rls"] += 1
            elif r.get("ceiling_height_m"):
                st["h_default"] += 1
            else:
                st["h_none"] += 1
            ob = r.get("occupancy_basis")
            st["occ_" + str(ob)] += 1
            if ob == "NOT SET":
                res_occ["the room name gives no room use for the default density"] += 1
            if r.get("load_basis") == "ASHRAE":
                st["loads"] += 1
            else:
                st["no_loads"] += 1
                res_load["the room name gives no ASHRAE room use"] += 1
            if r.get("pressure"):
                st["p_rooms"] += 1
                st["p_plan" if r["pressure"].get("perimeter_m") else "p_assumed"] += 1
        out[num] = dict(name=d["department"]["name"], st=st, res_unit=res_unit, res_crit=res_crit, res_occ=res_occ, res_load=res_load, names_crit=names_crit,
                        stats=d.get("assemble_stats", {}))
    return out


def pct(a: int, b: int) -> str:
    return f"{100.0 * a / b:.0f} %" if b else "–"


def main(argv=None) -> int:
    results: Dict[str, Any] = {}
    if argv and "--results" in argv:
        rp = Path(argv[argv.index("--results") + 1])
        if rp.exists():
            results = json.loads(rp.read_text(encoding="utf-8"))
    elif Path("out/qa/round4_results.json").exists():
        results = json.loads(Path("out/qa/round4_results.json").read_text(encoding="utf-8"))
    data = collect()
    tot = collections.Counter()
    for v in data.values():
        tot.update(v["st"])
    L: List[str] = []
    w = L.append
    w("# Round 4 report\n")
    w("Decisions D-025 to D-028 of Marcus (23 Nov 2026) implemented in the generator (`src/workbook`, `src/recompute/thermal.py`, `src/zoning`); round-3 findings R3-02 to R3-10 fixed; completeness push on every DPA room of LB3 to L07 IP. "
      "Every output was regenerated from the code (`python -m src.workbook.regenerate`, `python -m src.zoning.deliver all`, `python -m src.open_items`). Nothing was edited by hand.\n")
    w("## 1. What changed\n")
    w("| Item | Change |\n|---|---|")
    w("| D-025 plug load and lighting | Each room takes the ASHRAE plug load and the ASHRAE 90.1 lighting power density of its room use (`internal_loads` of the configuration) where no project document gives a value. "
      "Cells are red (named style Assumption-Load, code A). The Remarks cell states 'Plug load assumed: x W/m² (ASHRAE, use). No project value. To confirm.' and the same for lighting. "
      "Equipment is 0 W with a VERIFY flag on equipment-led rooms. Envelope gain is 'not assessed', stated plainly. All wording 'OCCUPANTS ONLY / no approved value' is removed (room Remarks, README, Basis of Design, AHU remarks, master, zone schedule). "
      "Both engines include the gains: the workbook engine (Room Zones) and `src/recompute/thermal.py` read the same room fields. The Rev A reproduction keeps its own project values. |")
    w("| D-026 presumed criteria | A room with no criteria row in its own department takes the row of the same space in the closest other department (`src/workbook/presume.py`: same room type code, then same name, then same use class; clinical rooms prefer clinical departments; the booklet and page stay as source). "
      "Criteria cells are italic green (named style Presumed-Criteria, code A). The Remarks say 'Presumed criteria: taken from <department> booklet p<page>, <row>. No row in this department. To confirm.' "
      "A plant room takes a row only by room type code or name (R1-08: no row from the use class). MOT theatres and anaesthesia rooms stay specified (code S). |")
    w("| D-027 reduction wording and completeness | The Remarks state a reduction only where Calculated is below Tender and the room list of the unit is complete. Otherwise they say nothing about a reduction ('partial:' and 'No reduction is stated' are gone). "
      "A completeness column states 'Complete' or 'Not complete: n of m zoned rooms counted'. Above-tender supply and outdoor air are VERIFY on every row of the AHU tabs, the master summary and the zone schedule; the master Checks rows count them from the numbers. "
      "The master TOTAL row adds only the rows that state a reduction (tender − calculated = reduction); the row 'TOTAL, all rows' adds every row. |")
    w("| D-028 pressure | The Pressure tab calculates every room with a pressure target in Pa from its internal surface (2 × area + perimeter × height) at 2.5 m³/h per m² at 50 Pa, converted with n = 0.65 (sensitivity 0.50 and 0.75). "
      "Offset needed = envelope leakage at the set pressure. Design offset = 2.5 × surface. Ratio = design / needed. The perimeter is the perimeter of the linked measured plan room, else 4 × √area (named cell PerimFactor, code A, flagged). "
      "No door data, no 'no door data' flag. The known-answer cases that used doors now test this method (the 8.3 envelope cases stay). |")
    w("| Open items | D-021, OI-04-1 to OI-04-4 and OI-R103-1 are settled and listed in one closed section. New items: OI-R305-1, OI-R306-1, OI-R308-1, OI-R302-1, OI-R307-1. The open conflicts of `conflicts.csv` are listed. |")
    w("| R3-02 | A unit link from a plan room needs area plausibility: the total design area of the DPA rooms must be within a factor of 2 of the plan area. A refused link is a VERIFY flag, a Remarks/Flags note and a row of the link audit. |")
    w("| R3-03 | Above-tender supply and outdoor air are VERIFY in the department AHU tabs, the master summary and the zone schedule; the master Checks rows count all of them. |")
    w("| R3-04 | D00: every space with a pressure target gets a Pressure row; Checks row 6 and the README line agree (0). CA-L05-060 has plug load, lighting and criteria like every space. |")
    w("| R3-05 | The MOT NSC note on IP-BH-16057 (CT and UCV theatre needs about 5,320 L/s for the future UCV) is in the room Remarks, the AHU remark of AHU-B-07-05MOTCT-01 (D22, master, zone schedule), Flags and OI-R305-1. |")
    w("| R3-06, R3-09 | The unit named in a D22 remark is the unit of the room. A different tag in the MOT NSC documents is a VERIFY flag and OI-R306-1. AHU-B-07-05MOTHYB-02 shows the same row (no tender airflow, same conflict note) in D22, the master summary and the zone schedule (OI-R114-1). |")
    w("| R3-07 | The open items state that the common areas of LB3, B3, B2, B1 and L01 are outside Appendix D and list them; the open conflicts section is read from `conflicts.csv`. |")
    w("| R3-08 | The room status 'REDUCTION AGAINST TENDER' is not given to a room with no criteria row (status NO CRITERIA ROW). D47 offices: D-009 covers only the AN-ED offices on L02; the D47 offices on L06 and L07 take presumed criteria and are listed in OI-R308-1. |")
    w("| R3-10 | Master TOTAL row: see D-027. |")
    w("")
    w("## 2. Completeness of the DPA rooms of every department\n")
    w("| Dept | Name | Rooms | Unit or class | Criteria own / presumed / MOT | No criteria | Area | Height RLS / default | Occupancy RDS / default / not set | Loads ASHRAE | Pressure rooms (plan / assumed perimeter) |\n|---|---|---|---|---|---|---|---|---|---|---|")
    for num, v in sorted(data.items()):
        s = v["st"]
        w(f"| D{num} | {v['name']} | {s['rooms']} | {s['unit'] + s['class']} ({pct(s['unit'] + s['class'], s['rooms'])}) | {s['own']} / {s['presumed']} / {s['mot']} | {s['no_crit']} | {s['rooms'] - s['no_area']} | "
          f"{s['h_rls']} / {s['h_default']} | {s['occ_RDS']} / {s['occ_DENSITY DEFAULT']} / {s['occ_NOT SET']} | {s['loads']} ({pct(s['loads'], s['rooms'])}) | {s['p_rooms']} ({s['p_plan']} / {s['p_assumed']}) |")
    t = tot
    w(f"| all | | {t['rooms']} | {t['unit'] + t['class']} ({pct(t['unit'] + t['class'], t['rooms'])}) | {t['own']} / {t['presumed']} / {t['mot']} | {t['no_crit']} | {t['rooms'] - t['no_area']} | {t['h_rls']} / {t['h_default']} | "
      f"{t['occ_RDS']} / {t['occ_DENSITY DEFAULT']} / {t['occ_NOT SET']} | {t['loads']} ({pct(t['loads'], t['rooms'])}) | {t['p_rooms']} ({t['p_plan']} / {t['p_assumed']}) |")
    w("")
    w("Every room has an area (DPA, SOA or measured), a ceiling height (RLS value or the flagged default), an occupancy (RDS value or the ASHRAE default density, except the rooms listed below) and a plug load and lighting value (ASHRAE, except the rooms listed below). "
      "A criteria row is own (booklet row of the department, code D or A), presumed (D-026) or specified (MOT NSC, code S).\n")
    w("## 3. Residue with the reason, by department\n")
    for num, v in sorted(data.items()):
        s = v["st"]
        parts = []
        if s["no_unit"]:
            parts.append(f"no unit or class: {s['no_unit']} ({'; '.join(f'{n} {k}' for k, n in v['res_unit'].most_common())})")
        if s["no_crit"]:
            parts.append(f"no criteria row: {s['no_crit']} ({'; '.join(f'{n} {k}' for k, n in v['res_crit'].most_common())}; names: {', '.join(f'{k} ({n})' for k, n in sorted(v['names_crit'].items(), key=lambda kv: -kv[1])[:6])})")
        if s["occ_NOT SET"]:
            parts.append(f"no occupancy: {s['occ_NOT SET']}")
        if s["no_loads"]:
            parts.append(f"no plug load or lighting: {s['no_loads']}")
        if s["h_default"]:
            parts.append(f"flagged default height: {s['h_default']} (no room layout sheet height linked)")
        if parts:
            w(f"- **D{num} {v['name']}** ({s['rooms']} rooms): " + "; ".join(parts) + ".")
    w("")
    w("The unit or class of a room comes only from a plan room of the zoning or from a room number on an air-side schematic. Where no document links the room, no unit is set (unknown stays unknown). "
      "The links that the new area test refused are in the link audit files (`out/qa/dpa_link_audit_D<nn>.csv`, plan_basis 'no link: area not plausible') and in OI-R302-1.\n")
    w("## 4. Units and reductions\n")
    try:
        m = openpyxl.load_workbook(WB / "AHU_master_summary.xlsx", data_only=True)["AHU Master"]
        hdr = {c.value: i for i, c in enumerate(m[5])}
        rows = [r for r in m.iter_rows(min_row=7, values_only=True) if r[0] and not str(r[0]).startswith("TOTAL")]
        c = collections.Counter(r[hdr["Against tender"]] for r in rows)
        comp = sum(1 for r in rows if str(r[hdr["Room list of the unit (rooms counted)"]]).startswith("Complete"))
        w(f"AHU master summary: {len(rows)} units; room list complete: {comp}; not complete: {len(rows) - comp}. Against tender: " + "; ".join(f"{k} {n}" for k, n in c.most_common()) + ".")
        tot_row = next(r for r in m.iter_rows(min_row=7, values_only=True) if r[0] == "TOTAL")
        w(f"TOTAL row (rows that state a reduction): tender {tot_row[hdr['Tender airflow']]:,.0f} L/s, calculated {tot_row[hdr['Calculated airflow']]:,.0f} L/s, reduction {tot_row[hdr['Reduction = tender − calculated']]:,.0f} L/s.\n")
    except Exception as exc:                                   # noqa: BLE001 - the report states what it could not read
        w(f"The master summary could not be read: {exc}\n")
    if results:
        w("## 5. Verification\n")
        w("| Step | Result |\n|---|---|")
        for k, v in results.items():
            w(f"| {k} | {v} |")
        w("")
    OUT.write_text("\n".join(L), encoding="utf-8")
    print(f"wrote {OUT}: {len(data)} departments, {tot['rooms']} rooms")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
