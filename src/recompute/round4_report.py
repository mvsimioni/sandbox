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
    if "negative-pressure or single-pass rows" in g:
        return "the only rows with the main word of the name are negative-pressure or single-pass rows, and the name does not say toilet, dirty or isolation"
    if "with the main word" in g:
        return "no other department prints a row of the same use class with the main word of the name"
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
        names_oa: Dict[str, int] = collections.defaultdict(int)
        names_area: Dict[str, int] = collections.defaultdict(int)
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
                if "main word" in (c.get("remark") or ""):
                    st["presumed_by_use"] += 1
            elif c.get("code") == "S":
                st["mot"] += 1
            elif "of the booklet of this department, mapped to the room type" in (c.get("remark") or ""):
                st["own_mapped"] += 1
            else:
                st["own"] += 1
            if c and not any(c.get(k) not in (None, "", 0) for k in ("min_oa_ach", "oa_lps_per_person", "oa_lps_per_m2", "oa_specified_lps")):
                st["no_oa"] += 1
                names_oa[c.get("criteria_row") or ""] += 1
            if not (r.get("soa_area_m2") or r.get("dpa_area_m2") or r.get("measured_area_m2")):
                st["no_area"] += 1
                names_area[r["name"]] += 1
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
                        names_oa=names_oa, names_area=names_area, stats=d.get("assemble_stats", {}))
    return out


def process_text() -> str:
    """What the repository shows about commits, read from git."""
    import subprocess
    try:
        log = subprocess.run(["git", "log", "--format=%h %s (%cd)", "--date=format:%H:%M", "-4"], capture_output=True, text=True, check=True).stdout.strip().splitlines()
        dirty = subprocess.run(["git", "status", "--short"], capture_output=True, text=True, check=True).stdout.strip().splitlines()
    except Exception as exc:                                   # noqa: BLE001
        return f"The git state could not be read: {exc}\n"
    wip = [x for x in log if "WIP" in x]
    t = ("The brief says not to commit. " + (f"The repository has {len(wip)} commit(s) of the earlier pass of this round that the brief did not allow, in the history on top of the D-029 configuration commit: "
         + "; ".join(f"`{x}`" for x in wip) + ". They were not rewritten or removed here. " if wip else "")
         + f"This pass made no commit. The changes of this pass are in the working tree, not committed ({len(dirty)} changed paths under version control; `out/` is not under version control).\n")
    return t


def d00_section() -> str:
    """Common areas (D00): the criteria of every space and the spaces with no criteria row, with the reason."""
    from ..workbook.common_areas import assemble_common_areas
    d = assemble_common_areas(Path("."), with_others=False)
    rooms = d["rooms"]
    pres = [r for r in rooms if (r.get("criteria") or {}).get("presumed")]
    none = [r for r in rooms if not r.get("criteria")]
    by_kind = collections.Counter()
    for r in pres:
        c = r["criteria"]
        by_kind["circulation default" if c.get("criteria_row", "").startswith("Circulation default") else "booklet row of the same name (most frequent)" if "most frequent row" in (c.get("remark") or "") else "use class and main word"] += 1
    L = ["## 3a. Common areas (D00): criteria\n",
         f"{len(rooms)} spaces. {len(pres)} take presumed criteria (italic green, Remarks 'Presumed criteria: taken from ...'): " + "; ".join(f"{n} {k}" for k, n in by_kind.most_common()) + f". {len(none)} have no criteria row. "
         "D00 has no booklet of its own, so every row of a booklet is a presumed row.\n"]
    groups: Dict[str, List[str]] = collections.defaultdict(list)
    for r in none:
        g = (r.get("gaps") or {}).get("criteria", "")
        if g.startswith("Plant space"):
            k = "plant or electrical space: no criteria row (the plant-room criteria are open for Marcus)"
        elif "several names" in g:
            k = "outline with several names that do not give one room use"
        elif "does not give a room use" in g or "gives no room use" in g or g.startswith("No booklet room type fits"):
            k = "the name does not give a room use (abbreviation or word that no document explains)"
        else:
            k = "other"
        groups[k].append(r["name"])
    for k, names in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        cnt = collections.Counter(names)
        L.append(f"- {len(names)} spaces, {k}: " + ", ".join(f"{n} ({c})" for n, c in cnt.most_common(40)) + ("; and others" if len(cnt) > 40 else "") + ".")
    L.append("")
    L.append("A name such as 'Toilet / SAN' (toilet and sanitary) is one room use and takes the row of a toilet; 'Hold', 'Wash', 'HR', 'Svc' and 'Rtl' have no room use that a document gives, so they stay without a criteria row and are listed on Flags of D00.\n")
    return "\n".join(L)


def outdoor_air_section(data: Dict[str, Any]) -> str:
    """Rooms with a criteria row and no outdoor-air value, with the printed text that the value would come from."""
    t = collections.Counter()
    rows = collections.Counter()
    for v in data.values():
        t.update(v["st"])
        rows.update(v["names_oa"])
    L = ["## 3b. Outdoor air of the criteria rows\n",
         f"{t['no_oa']} of the rooms with a criteria row have no outdoor-air value (no air-change value, no per-person or per-area rate, no specified value). "
         "The printed text of the row is a dash, NA, a standard reference (SS 553) or blank. The most frequent rows: " +
         ", ".join(f"{k} ({n})" for k, n in rows.most_common(12)) + ". For these rooms the outdoor air stays 0 and the Remarks of the criteria row show the printed text; no value is assumed.\n"]
    return "\n".join(L)


def comparison_section() -> str:
    """Independent recomputation: read from the comparison CSV and the sensitivity CSV, with no explanation added here."""
    cp, sp = Path("out/qa/recompute_comparison.csv"), Path("out/qa/recompute_sensitivity.csv")
    if not cp.exists():
        return "## 4a. Independent recomputation\n\nThe comparison file is not present.\n"
    with cp.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    bad = [r for r in rows if r["within_tolerance"] != "yes"]
    causes = collections.Counter(r["cause"] or "no cause" for r in bad)
    L = ["## 4a. Independent recomputation\n",
         f"`src/recompute/thermal.py` reads the same inputs and applies the rules on its own. Both engines are read under the interim reading of the two rules that are open for Marcus "
         f"(OI-13-1 supply floor from the exhaust; OI-R401-1 outdoor air of a room with a pressure offset), so each engine applies the same stated rule independently. "
         f"{len(rows)} values compared; {len(rows) - len(bad)} within 0.5 % (or 0.05 in the unit); {len(bad)} outside.\n"]
    for c, n in causes.most_common():
        L.append(f"- {n} value(s) outside: {c}.")
    L.append("")
    if sp.exists():
        with sp.open(newline="", encoding="utf-8") as fh:
            s = list(csv.DictReader(fh))
        rooms = {(r["dept"], r["ref"]) for r in s}
        by = collections.Counter(r["metric"] for r in s)
        L.append(f"Sensitivity to the open rules (not an explanation of any difference above): under the other reading of OI-13-1 and OI-R401-1 the recomputation gives different values for {len(rooms)} rooms "
                 f"({', '.join(f'{n} {k}' for k, n in by.most_common())}). They are listed in `out/qa/recompute_sensitivity.csv`. They stay open until Marcus rules; then both engines change together.\n")
    unexpl = [r for r in bad if not r["cause"]]
    L.append(f"Values outside the tolerance with no explanation: {len(unexpl)}. " + ("" if not unexpl else "They are listed in the comparison file and are not explained here."))
    L.append("")
    return "\n".join(L)


def links_section() -> str:
    n = 0
    per = collections.Counter()
    import glob
    for f in sorted(glob.glob("out/qa/dpa_link_audit_D*.csv")):
        d = re.search(r"D(\d+)\.csv", f).group(1)
        with open(f, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("plan_basis") == "no link: area not plausible":
                    n += 1
                    per[d] += 1
    return (f"## 3c. Area test of the unit links\n\n{n} unit links were refused by the area test: " + ", ".join(f"D{k} ({v})" for k, v in sorted(per.items())) +
            ". The link audit files (`out/qa/dpa_link_audit_D<nn>.csv`) and OI-R302-1 list the same rooms.\n")


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
    w("# Round 4 report (pass 1 fixes)\n")
    w("Decisions D-025 to D-028 of Marcus (23 Nov 2026) implemented in the generator (`src/workbook`, `src/recompute/thermal.py`, `src/zoning`); round-3 findings R3-02 to R3-10 fixed; completeness push on every DPA room of LB3 to L07 IP. "
      "Every output was regenerated from the code (`python -m src.workbook.regenerate`, `python -m src.zoning.deliver all`, `python -m src.open_items`, `python -m src.recompute.compare_workbooks`). Nothing was edited by hand.\n")
    w("## 0. Process\n")
    w(process_text())
    w("## 1. What changed\n")
    w("| Item | Change |\n|---|---|")
    w("| D-025 plug load and lighting | Each room takes the ASHRAE plug load and the ASHRAE 90.1 lighting power density of its room use (`internal_loads` of the configuration) where no project document gives a value. "
      "Cells are red (named style Assumption-Load, code A). The Remarks cell states 'Plug load assumed: x W/m² (ASHRAE, use). No project value. To confirm.' and the same for lighting. "
      "Equipment is 0 W with a VERIFY flag on equipment-led rooms. Envelope gain is 'not assessed', stated plainly. All wording 'OCCUPANTS ONLY / no approved value' is removed (room Remarks, README, Basis of Design, AHU remarks, master, zone schedule). "
      "Both engines include the gains: the workbook engine (Room Zones) and `src/recompute/thermal.py` read the same room fields. The Rev A reproduction keeps its own project values. |")
    w("| D-026 presumed criteria | D-026 applies only where the own department has no row for the space. Step 0: the booklet of the room's own department is read first. A row of the own booklet that prints the same space with a level or department prefix or a number in brackets is the row of the room (for example `L6 Admin Workspace Office` for 'Office - Typical' in D47; `Lecture Room (64)` for 'Lecture Room' in D50). It is a mapped own row (code A, plain Assumption style, Remarks say so), not a presumed row. "
      "Then, for a room with no own row: the same room type code, then the same name, then the same use class AND the same main word in the room type name (`src/workbook/presume.py`). A row that has only the use class in common is not the same space and is not taken. A row at negative pressure or on single pass is taken from the use class only where the room name says toilet, dirty or isolation. Clinical rooms prefer clinical departments (01 to 29, Laboratory Medicine included). "
      "Criteria cells of a presumed row are italic green (named style Presumed-Criteria, code A); the Remarks say 'Presumed criteria: taken from <department> booklet p<page>, <row>. No row in this department. To confirm.' and name the match. In D00 every row taken from a booklet (same room type name, circulation default, use class) is a presumed row and has the italic green style and the Remarks. "
      "A plant room takes a row only by room type code or name. MOT theatres and anaesthesia rooms stay specified (code S). |")
    w("| D-027 reduction wording and completeness | The Remarks state a reduction only where Calculated is below Tender and the room list of the unit is complete. Otherwise they say nothing about a reduction ('partial:' and 'No reduction is stated' are gone). "
      "A completeness column states 'Complete' or 'Not complete' with the reason (rooms counted against zoned rooms, rooms with no criteria row, area share, other departments on the unit). A unit that another department also serves is judged on the master summary only: the department tab states no reduction and says so (column 'Rooms of other departments on this unit'), so the department tabs and the master cannot disagree. Above-tender supply and outdoor air are VERIFY on every row of the AHU tabs, the master summary and the zone schedule; the master Checks rows count them from the numbers. "
      "The master TOTAL row adds only the rows that state a reduction (tender − calculated = reduction); the row 'TOTAL, all rows' adds every row. |")
    w("| D-028 pressure | The Pressure tab calculates every room with a pressure target in Pa from its internal surface (2 × area + perimeter × height) at 2.5 m³/h per m² at 50 Pa, converted with n = 0.65 (sensitivity 0.50 and 0.75). "
      "Offset needed = envelope leakage at the set pressure. Design offset = 2.5 × surface. Ratio = design / needed. The perimeter is the perimeter of the linked measured plan room, else 4 × √area (named cell PerimFactor, code A, flagged). "
      "No door data, no 'no door data' flag. A neutral room (target 0 Pa) has no offset to calculate and has no Pressure row. The known-answer cases that used doors now test this method (the 8.3 envelope cases stay). |")
    w("| Outdoor air per area and per person | The booklet prints the outdoor air of some rows as L/s per m² or L/s per person (or the higher of the two, or as air changes in the text). The register keeps the text and leaves the air-change column blank. The criteria of the workbook now have a column 'Outdoor air per area' beside 'Outdoor air per person'; both are read from the printed text. The outdoor air of a room is the highest of the per-person, per-area, air-change and specified values, and it is a floor of the supply (both engines). |")
    w("| Wording | No double full stop, no double space and no zero-width character in any cell (text of a cell is cleaned when it is written; sentences are joined without a doubled stop). Completeness text: 'the zoned room is counted' for one room, and a reason for every 'Not complete'. |")
    w("| Open items | D-021, OI-04-1 to OI-04-4 and OI-R103-1 are settled and listed in one closed section. OI-R308-1 is settled by D-026 (the own rows of D47 are used) and is no longer an open question. New items: OI-R305-1, OI-R306-1, OI-R302-1, OI-R307-1, OI-R401-1 (the two engines read the exhaust of a room with a pressure offset differently; no engine is changed before Marcus rules; the comparison reads both engines under the interim reading and lists the other reading apart). The open conflicts of `conflicts.csv` are listed. |")
    w("| R3-02 | A unit link from a plan room needs area plausibility: the total design area of the DPA rooms must be within a factor of 2 of the plan area. A refused link is a VERIFY flag, a Remarks/Flags note and a row of the link audit. |")
    w("| R3-03 | Above-tender supply and outdoor air are VERIFY in the department AHU tabs, the master summary and the zone schedule; the master Checks rows count all of them. |")
    w("| R3-04 | D00: every space with a pressure target gets a Pressure row; Checks row 6 and the README line agree (0). CA-L05-060 has plug load, lighting and criteria like every space. |")
    w("| R3-05 | The MOT NSC note on IP-BH-16057 (CT and UCV theatre needs about 5,320 L/s for the future UCV) is in the room Remarks, the AHU remark of AHU-B-07-05MOTCT-01 (D22, master, zone schedule), Flags and OI-R305-1. |")
    w("| R3-06, R3-09 | The unit named in a D22 remark is the unit of the room. A different tag in the MOT NSC documents is a VERIFY flag and OI-R306-1. AHU-B-07-05MOTHYB-02 shows the same row (no tender airflow, same conflict note) in D22, the master summary and the zone schedule (OI-R114-1). In the zone schedule a unit that is not on the zoning drawing has 0 rooms, 0 m² and 'no information' for the storeys; the rooms of the department workbooks stand in the columns beside. |")
    w("| R3-07 | The open items state that the common areas of LB3, B3, B2, B1 and L01 are outside Appendix D and list them; the open conflicts section is read from `conflicts.csv`. |")
    w("| R3-08 | The room status 'REDUCTION AGAINST TENDER' is not given to a room with no criteria row (status NO CRITERIA ROW). D47 offices: D-009 covers only the AN-ED offices on L02; the D47 rooms on L06 and L07 take the own rows of the D47 booklet (p293). |")
    w("| R3-10 | Master TOTAL row: see D-027. |")
    w("")
    w("## 2. Completeness of the DPA rooms of every department\n")
    w("| Dept | Name | Rooms | Unit or class | Criteria own / own mapped / presumed / MOT | No criteria | Area | Height RLS / default | Occupancy RDS / default / not set | Loads ASHRAE | Pressure rooms (plan / assumed perimeter) |\n|---|---|---|---|---|---|---|---|---|---|---|")
    for num, v in sorted(data.items()):
        s = v["st"]
        w(f"| D{num} | {v['name']} | {s['rooms']} | {s['unit'] + s['class']} ({pct(s['unit'] + s['class'], s['rooms'])}) | {s['own']} / {s['own_mapped']} / {s['presumed']} / {s['mot']} | {s['no_crit']} | {s['rooms'] - s['no_area']} | "
          f"{s['h_rls']} / {s['h_default']} | {s['occ_RDS']} / {s['occ_DENSITY DEFAULT']} / {s['occ_NOT SET']} | {s['loads']} ({pct(s['loads'], s['rooms'])}) | {s['p_rooms']} ({s['p_plan']} / {s['p_assumed']}) |")
    t = tot
    w(f"| all | | {t['rooms']} | {t['unit'] + t['class']} ({pct(t['unit'] + t['class'], t['rooms'])}) | {t['own']} / {t['own_mapped']} / {t['presumed']} / {t['mot']} | {t['no_crit']} | {t['rooms'] - t['no_area']} | {t['h_rls']} / {t['h_default']} | "
      f"{t['occ_RDS']} / {t['occ_DENSITY DEFAULT']} / {t['occ_NOT SET']} | {t['loads']} ({pct(t['loads'], t['rooms'])}) | {t['p_rooms']} ({t['p_plan']} / {t['p_assumed']}) |")
    w("")
    w(f"{t['rooms'] - t['no_area']} of {t['rooms']} rooms have an area (DPA, SOA or measured); {t['no_area']} have none (listed by name in section 3). Every room has a ceiling height (RLS value or the flagged default). "
      f"{t['occ_RDS'] + t['occ_DENSITY DEFAULT']} of {t['rooms']} rooms have an occupancy (RDS value or the ASHRAE default density) and {t['loads']} have a plug load and lighting value (ASHRAE); the others are listed below. "
      "A criteria row is own (booklet row of the department with the same name, code D or A), own mapped (the own booklet prints the same space with a level or department prefix or a number in brackets, code A), presumed (D-026, another department) or specified (MOT NSC, code S).\n")
    w(f"Presumed rows: {t['presumed']}; of these {t['presumed_by_use']} by use class with the same main word in the room type name (were 867 of 1,429 by use class alone before this pass). Rooms with no criteria row: {t['no_crit']}.\n")
    w("## 3. Residue with the reason, by department\n")
    for num, v in sorted(data.items()):
        s = v["st"]
        parts = []
        if s["no_unit"]:
            parts.append(f"no unit or class: {s['no_unit']} ({'; '.join(f'{n} {k}' for k, n in v['res_unit'].most_common())})")
        if s["no_crit"]:
            parts.append(f"no criteria row: {s['no_crit']} ({'; '.join(f'{n} {k}' for k, n in v['res_crit'].most_common())}; names: {', '.join(f'{k} ({n})' for k, n in sorted(v['names_crit'].items(), key=lambda kv: -kv[1])[:6])})")
        if s["no_area"]:
            parts.append(f"no area: {s['no_area']} ({', '.join(f'{k} ({n})' for k, n in sorted(v['names_area'].items(), key=lambda kv: -kv[1])[:6])}; no DPA, SOA or measured area)")
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
    w(d00_section())
    w(outdoor_air_section(data))
    w(links_section())
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
    w(comparison_section())
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
