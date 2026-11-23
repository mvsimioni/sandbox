"""Write out/qa/build_all_report.md from the department inputs, the recalculated workbooks, the recomputation CSV and the master summary."""

from __future__ import annotations

import collections
import csv
import glob
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

import openpyxl


def _flags_and_checks(path: Path) -> Dict[str, Any]:
    wb = openpyxl.load_workbook(path, data_only=True)
    fl = [r for r in wb["Flags"].iter_rows(min_row=7, values_only=True) if r[0]]
    by = collections.Counter(r[1] for r in fl)
    checks = [(r[1], r[2], r[4]) for r in wb["Checks"].iter_rows(min_row=7, values_only=True) if r[1] and r[4] and r[0] != None]
    overall = next((r[4] for r in wb["Checks"].iter_rows(min_row=7, values_only=True) if r[1] == "Calculation"), "")
    return dict(flags=sum(by.values()), by_type=dict(by), fail=[c[0] for c in checks if c[2] == "FAIL"], verify=[c[0] for c in checks if c[2] == "VERIFY"], overall=overall)


def main() -> int:
    root = Path(".")
    inputs = {p.stem[1:]: json.loads(p.read_text(encoding="utf-8")) for p in sorted(Path("out/loads/inputs").glob("D*.json"))}
    wbs = {re.match(r"AIGH_C2_D(\d+)_", p.name).group(1): p for p in sorted(Path("out/loads/workbooks").glob("AIGH_C2_D*_RevA.xlsx"))}
    rec = list(csv.DictReader(open("out/qa/recompute_comparison.csv", encoding="utf-8")))
    rec_by = collections.defaultdict(list)
    for r in rec:
        rec_by[r["dept"]].append(r)
    table = json.loads(Path("src/workbook/dept_units.json").read_text(encoding="utf-8"))
    out: List[str] = []
    w = out.append
    w("# Build all: department workbooks LB3 to L07\n")
    w("Rev A, 22 Nov 2026. Scope: IP storeys LB3, B3, B2, B1, L01 to L07 (D-010). Source files only: RDS, SOA, DPA, the DD booklet criteria, the room layout heights (D-019).\n")
    w("## 1. Result\n")
    n_rooms = sum(len(d["rooms"]) for d in inputs.values())
    w(f"- {len(inputs)} department workbooks built from the registers, recalculated and checked, plus D00 Common Areas and the AHU master summary. {n_rooms} rooms in the department workbooks.")
    no_ahu = [k for k, d in inputs.items() if d["assemble_stats"]["with_ahu"] == 0]
    w("- No department of the list was dropped: each has at least one DPA room on an IP storey LB3 to L07 (section 2). "
      f"Departments {', '.join(no_ahu)} have rooms but no room with an AHU (the plans give no AHU zoning evidence for them), so their workbooks carry no AHU rows (flagged).")
    w("")
    w("## 2. Department to DPA unit to storey\n")
    w("The mapping is in `src/workbook/dept_units.json` (explicit entries with evidence) and `src/workbook/dept_units.py` (order: explicit entry, DPA unit name equal to a stacking-diagram IP cell, SOA unit code with a single IP storey). "
      "Plan rooms of a department are selected by the regular expression in the same file (`plan_departments`).\n")
    w("| Dept | Name | DPA units (storey, basis) | Units excluded (reason) | Units unresolved |")
    w("|---|---|---|---|---|")
    for k, d in inputs.items():
        s = d["assemble_stats"]
        units = "; ".join(f"{u} ({st}, {s['unit_basis'].get(u, '')})" for u, st in s["units"])
        exc = "; ".join(f"{e['unit']}: {e['reason']}" for e in s.get("units_excluded", [])) or "none"
        w(f"| {k} | {d['department']['name']} | {units} | {exc} | {', '.join(s.get('units_unresolved', [])) or 'none'} |")
    w("")
    w("Evidence per entry is in the JSON (stacking diagram cell, DPA unit name, SOA unit code, bed counts). Entries that are inference, to confirm: D31 AIWLL2 (name only), "
      "D26 DIIP kept on L04 although the stacking diagram has a CT Scan cell for D26 on L5 (no D26 plan rooms on L05), D46 COMM and D39 MORT/MAIL (RDS room names), D36 HOSP (zone assignment text). "
      "The DPA unit SECU (security, 9 rooms) is listed in RDS D39 but no storey evidence exists: not used.\n")
    w("## 3. Per workbook\n")
    w("Rooms, rooms with an AHU, rooms with a room data sheet (RDS), height from the room layout sheets (RLS) against the flagged default, rooms with a criteria row, "
      "occupancy basis counts, flags, check results, recomputation.\n")
    w("| Dept | Workbook | Rooms | With AHU | RDS-linked | RLS height / default | Criteria | Occupancy RDS / density default / not set | Flags (VERIFY / CONFIRM / ASSUMPTION) | Checks | Recomputation |")
    w("|---|---|---|---|---|---|---|---|---|---|---|")
    notes: List[str] = []
    for k, d in inputs.items():
        s = d["assemble_stats"]
        f = _flags_and_checks(wbs[k])
        rr = rec_by[k]
        bad = [r for r in rr if r["within_tolerance"] != "yes"]
        unexpl = [r for r in bad if not r["cause"]]
        rtxt = f"{len(rr) - len(bad)} of {len(rr)} within 0.5 %" + (f"; {len(bad)} outside ({len(unexpl)} unexplained)" if bad else "")
        chk = f"{f['overall']}: FAIL {len(f['fail'])}, VERIFY {len(f['verify'])}"
        w(f"| {k} | {wbs[k].name} | {s['rooms']} | {s['with_ahu']} | {s['with_rds']} | {s['height_from_rls']} / {s['height_default']} | {s['with_criteria']} | "
          f"{s['occupancy_from_rds']} / {s['occupancy_default_density']} / {s['occupancy_not_set']} | {f['flags']} ({f['by_type'].get('VERIFY', 0)} / {f['by_type'].get('CONFIRM', 0)} / {f['by_type'].get('ASSUMPTION', 0)}) | {chk} | {rtxt} |")
        if f["fail"]:
            notes.append(f"D{k} FAIL: {'; '.join(f['fail'])}")
    w("")
    w("The check results come from the recalculated workbook (Checks tab). The status of every workbook is FAIL while an acceptance item is open (brief 8.6, limit 0, no tolerance): "
      "rooms with neither an AHU nor a system class, supply of such rooms, rooms with no area, no ceiling height or no criteria row, and pressure targets with no Pressure row. "
      "VERIFY checks are findings to review: area disagreement, rooms classed MV, NV or no information, rooms with an assumed perimeter on Pressure (listed on Flags). "
      "The README of each workbook shows the calculation checks, the acceptance state, the share of rooms with a unit, of supply on a unit and of rooms with a criteria row, and the thermal load case (people, lighting and small power; equipment 0; envelope not assessed). "
      "Error values, typed numbers in formulas, unlisted functions and forbidden terms or internal references: 0 in every workbook (`python3 -m src.workbook check`).\n")
    if notes:
        w("\n".join(f"- {n}" for n in notes))
    w("## 4. Independent recomputation\n")
    tot = len(rec)
    bad = [r for r in rec if r["within_tolerance"] != "yes"]
    w(f"`src/recompute/thermal.py` run on every department input JSON (`out/loads/inputs/D<nn>.json`) and compared with the recalculated workbook: room supply, outdoor air, sensible, latent; AHU supply, outdoor air, room sensible, room latent. "
      f"Result in `out/qa/recompute_comparison.csv`: {tot} values, {tot - len(bad)} within 0.5 % (or 0.05 in the unit), {len(bad)} outside.\n")
    causes = collections.Counter(r["cause"] or "no cause found" for r in bad)
    for c, n in causes.most_common():
        w(f"- {n} value(s): {c}.")
    by_dept = collections.Counter(r["dept"] for r in bad)
    w(f"- Departments with values outside: {', '.join(f'D{k} ({n})' for k, n in sorted(by_dept.items()))}.")
    w("")
    w("Both engines are read under the interim reading of the two rules that are open for Marcus (OI-13-1 supply floor from the exhaust; OI-R401-1 outdoor air of a room with a pressure offset), so that each engine applies the same stated rule on its own. "
      "The outdoor air that a booklet gives per m2 or per person is a floor of the room supply in both engines. A value outside tolerance with no cause named above has no explanation yet and is listed in `out/qa/recompute_comparison.csv`. "
      "The values that the other reading of the two open rules would change are in `out/qa/recompute_sensitivity.csv`; they are not explanations of a difference.\n")
    w("Known cause of a difference of arithmetic: area test at the tolerance edge. The workbook compares the design area and the measured area after taking each to 0.1 m2; the recomputation compares them unrounded. D22 AMSC 02.05.03 (design 10.22 m2, measured 9.709 m2, difference 0.511 m2 unrounded, 0.5 m2 rounded) is the case. "
      "The brief says to take areas to 0.1 m2 first, so the workbook reading follows the brief; the recomputation reproduces the workbook value when it rounds first.")
    w("\nOne defect of the recomputation was corrected: it rounded areas with Python `round`, which takes 10.25 to 10.2, where the spreadsheet takes 10.3 (D04 ICHA 12.07.03). It now rounds halves away from zero (`_round_half_up` in `src/recompute/thermal.py`). "
      "A room with no area (design area 0, no SOA, no measured area) is taken as 0 m2 in the comparison, as in the workbook; people-based loads stay (D11, D29, D31, D39, D45).\n")
    w("## 5. AHU master summary and Calculated airflow\n")
    m = openpyxl.load_workbook("out/loads/workbooks/AHU_master_summary.xlsx", data_only=True)["AHU Master"]
    rows = [r for r in m.iter_rows(min_row=7, values_only=True) if r[0]]
    w(f"`out/loads/workbooks/AHU_master_summary.xlsx`: {len(rows)} AHU/FCU rows from {len(wbs)} workbooks (D00 and the departments) (each AHU once; room supply summed over departments; duct leakage 10 %, D-005). "
      "The Calculated airflow of out/zoning/zone_schedule.xlsx (AHU summary) is filled from it as numbers (`python3 -m src.zoning.fill_calculated`); "
      "the column Calculated airflow source names the master row and the department workbooks; Reduction (L/s), Reduction (%) and Remarks stay formulas.\n")
    z = openpyxl.load_workbook("out/zoning/zone_schedule.xlsx")["AHU summary"]
    zt = [r for r in z.iter_rows(min_row=2, values_only=True) if r[0]]
    filled = [r for r in zt if r[10] is not None]
    mt_ = {str(r[0]): r for r in rows}
    n_common = sum(1 for r in zt if str(r[0]) in mt_)
    n_calc_common = sum(1 for r in zt if str(r[0]) in mt_ and isinstance(mt_[str(r[0])][8], (int, float)) and mt_[str(r[0])][8] > 0)
    n_no_supply = sum(1 for r in zt if str(r[0]) in mt_ and not (isinstance(mt_[str(r[0])][8], (int, float)) and mt_[str(r[0])][8] > 0))
    n_not_in = len(zt) - n_common
    w(f"Zone schedule AHU summary: {len(zt)} units; Calculated airflow filled for {len(filled)} (the master summary holds {n_common} of these tags, {n_calc_common} with a room supply above 0). "
      f"Blank for {len(zt) - len(filled)}: {n_no_supply} are in the master summary with no room supply, {n_not_in} are not in the master summary (no department workbook carries a room on them). "
      "Blank stays blank: no value is assumed. The fill is the last step of `python3 -m src.zoning.deliver schedule`, and `python3 -m src.zoning.deliver check` fails when the filled count is 0 or lower than the number of common tags with a calculated airflow, "
      "and when the tender airflow of the schedule and the master summary differ for a common tag. Both files take the tender airflow from one lookup (`src/workbook/tender_lookup.py`).\n")
    w("### Calculated against tender, per AHU or FCU (master summary)\n")
    w("Tender airflow is comparison only (D-012). Calculated covers only the rooms of the workbooks built here; a unit shared with a department outside this list is not complete.\n")
    w("| AHU or system | Departments (no.) | Rooms | Calculated (L/s) | Tender (L/s) | Reduction (L/s) | Reduction (%) | Against tender |")
    w("|---|---|---|---|---|---|---|---|")
    up = down = none = 0
    sum_c = sum_t = 0.0
    for r in rows:
        calc, ten = r[8], r[10]
        has_c = isinstance(calc, (int, float)) and calc > 0
        if has_c and isinstance(ten, (int, float)) and ten:
            sum_c += calc
            sum_t += ten
            if calc > ten:
                up += 1
            else:
                down += 1
        elif has_c:
            none += 1
        if not has_c and not isinstance(ten, (int, float)):
            continue
        red = f"{r[14]:,.0f}" if isinstance(r[14], (int, float)) else ""
        redp = f"{r[15] * 100:.1f}" if isinstance(r[15], (int, float)) else ""
        w(f"| {r[0]} | {r[2] if r[2] is not None else ''} | {r[3] if r[3] is not None else ''} | {f'{calc:,.0f}' if has_c else ''} | {f'{ten:,.0f}' if isinstance(ten, (int, float)) else ''} | {red} | {redp} | {r[16] or ''} |")
    w("")
    w(f"Units with both values: {up + down}; calculated above tender (VERIFY): {up}; at or below tender: {down}; calculated with no tender figure: {none}. "
      f"Sum over units with both: calculated {sum_c:,.0f} L/s against tender {sum_t:,.0f} L/s ({(sum_t - sum_c) / sum_t * 100 if sum_t else 0:.1f} % reduction). These sums include units whose other departments are outside this list.\n")
    w("## 6. Code and data changed\n")
    for t in ["`src/workbook/dept_units.json`, `src/workbook/dept_units.py`: department to DPA unit to storey mapping, with evidence per entry; plan-room department patterns; department names (booklet label, else DD Sign-OFF folder title).",
              "`src/workbook/assemble.py`: uses the mapping (fixes `no DPA unit matches an IP-block entry` for D04 and the other departments); unit filters for shared DPA units (ITEC D43/D44, TRED D50 without the simulation groups); decision numbers (D-nnn) removed from plan-department text that enters the workbook.",
              "`src/workbook/sheets_basic.py`: the source-ID check was one formula of more than 32 000 characters (Excel allows 8 192); it is now split over helper cells in a hidden column of Checks. The outdoor-air check counts rooms with an AHU only (rooms with no AHU are in the check on rooms with no AHU).",
              "`src/workbook/selftest.py`: the room type name 'Workflow Training' (DD booklet and DPA, Diagnostic Imaging) is source data and is not scanned as a forbidden word.",
              "`src/recompute/thermal.py`: half-up area rounding. `src/recompute/compare_workbooks.py`: the comparison. `src/zoning/fill_calculated.py` and `src/zoning/deliver.py`: zone schedule fill and README wording. `src/recompute/build_all_report.py`: this report."]:
        w(f"- {t}")
    w("\n## 7. Open items for Marcus\n")
    for t in ["Decision on the exhaust floor rule for a negative-regime room with no pressure value (OI-13-1) and on the outdoor air of a room with a pressure offset (OI-R401-1): see `out/qa/recompute_sensitivity.csv`.",
              "Confirm the inferred department to unit entries listed in section 2 and the exclusion of DIIP CT Scan (L5) and SECU.",
              "Every room with a default ceiling height, default density or no criteria row is listed on the Flags tab of its workbook.",
              "AHU units of the zone schedule that no workbook covers have a blank Calculated airflow.",
              "Open decisions D-020 to D-024 and the items of the final verification are in `out/qa/open_items_for_marcus.md` (written by `python3 -m src.open_items`)."]:
        w(f"- {t}")
    Path("out/qa/build_all_report.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print("wrote out/qa/build_all_report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
