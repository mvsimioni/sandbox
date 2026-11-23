"""Fill the Calculated airflow column of the AHU summary of ``out/zoning/zone_schedule.xlsx`` from the AHU master summary.

``python3 -m src.zoning.fill_calculated [--master out/loads/workbooks/AHU_master_summary.xlsx] [--schedule out/zoning/zone_schedule.xlsx]``

A live link cannot cross two files, so the value is written as a number and the column "Calculated airflow source" names the workbook, the
tab and the row it came from, and the department workbooks that add up to it. The Reduction columns and the Remarks column stay formulas.
A unit that is not in the master summary, or has no room supply there, keeps a blank Calculated airflow and the source column says why.
The tender airflow is not changed. A tender airflow in the master summary that differs from the schedule is listed in the source column.
"""

from __future__ import annotations

import argparse
import copy
import re
import sys
from pathlib import Path
from typing import Dict, List

import openpyxl
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, PatternFill
from openpyxl.utils import get_column_letter

SRC_HEAD = "Calculated airflow source ID"
README_OLD_ENTER = "Enter the calculated airflow in L/s in the Calculated airflow column. Cells with a blue font are inputs."
README_NEW_ENTER = ("The Calculated airflow column holds the supply airflow in L/s from AHU_master_summary.xlsx (tab AHU Master): the room supply of the "
                    "department workbooks that carry rooms on the unit, plus the duct leakage. The next column names the workbook rows. The values are numbers: "
                    "they do not update when a workbook changes.")
README_OLD_RED = ("Reduction (L/s) = Tender airflow minus Calculated airflow. Reduction (%) = Reduction divided by Tender airflow. "
                  "Both are formulas and stay blank until you enter the calculated airflow.")
README_NEW_RED = ("Reduction (L/s) = Tender airflow minus Calculated airflow. Reduction (%) = Reduction divided by Tender airflow. "
                  "Both are formulas and stay blank where the tender airflow or the calculated airflow is blank.")


def _level_tokens(tag: str) -> str:
    for tok in tag.split("-")[1:4]:
        if re.fullmatch(r"B[1-3]|\d{2}", tok):
            return {"B3": "B3", "B2": "B2", "B1": "B1"}.get(tok, "L" + tok)
    return "no information"


def main(argv=None) -> int:
    from ..workbook.styles import link_to
    from .deliver import plant_level
    ap = argparse.ArgumentParser()
    ap.add_argument("--master", default="out/loads/workbooks/AHU_master_summary.xlsx")
    ap.add_argument("--schedule", default="out/zoning/zone_schedule.xlsx")
    ap.add_argument("--workbooks", default="out/loads/workbooks")
    ns = ap.parse_args(argv)
    master = openpyxl.load_workbook(ns.master, data_only=True)
    mh = {str(c.value): i for i, c in enumerate(master["AHU Master"][5])}
    mrow: Dict[str, tuple] = {}
    for i, r in enumerate(master["AHU Master"].iter_rows(min_row=7, values_only=True), 7):
        if r[0] and r[0] != "TOTAL":
            mrow[str(r[0])] = (i, r)
    g = lambda r, h: r[mh[h]]
    files: Dict[str, str] = {}
    for p in sorted(Path(ns.workbooks).glob("AIGH_C2_D*_RevA.xlsx")):
        m = re.match(r"AIGH_C2_D(\d+)_", p.name)
        if m:
            files[m.group(1)] = p.name
    contrib: Dict[str, List[str]] = {}
    depts: Dict[str, List[str]] = {}
    for r in master["Department Rows"].iter_rows(min_row=7, values_only=True):
        if r[0] and r[1]:
            contrib.setdefault(str(r[0]), []).append(f"{files.get(str(r[1]), 'department ' + str(r[1]))} ({r[3]} rooms, {r[5]:g} L/s)")
            depts.setdefault(str(r[0]), []).append(str(r[2]))
    wb = openpyxl.load_workbook(ns.schedule)
    sm = wb["AHU summary"]
    heads = [c.value for c in sm[1]]
    col = {h: i + 1 for i, h in enumerate(heads)}
    col_k, col_i, col_src = col["Calculated airflow (L/s)"], col["Tender airflow (L/s)"], col[SRC_HEAD]
    col_ts, col_code = col["Tender airflow source"], col["Code"]
    col_toa, col_oa = col["Tender outdoor airflow (L/s)"], col["Calculated outdoor air (L/s)"]
    col_q, col_r, col_s = col["Rooms counted in the department workbooks"], col["Area counted in the department workbooks (m²)"], col["Room list of the unit"]
    coverage: List[dict] = []
    if "Sources" in wb.sheetnames:
        del wb["Sources"]
    so = wb.create_sheet("Sources")
    so["A1"].value = "Source ID"
    for j, h in enumerate(("Source ID", "Document", "Tab and row", "Departments and room supply", "Note"), 1):
        c = so.cell(row=1, column=j, value=h)
        c.style = "HeaderLeft"
    for j, w in enumerate((16, 36, 30, 90, 70), 1):
        so.column_dimensions[get_column_letter(j)].width = w
    n_fill = n_blank_in = n_blank_out = n_added = 0
    diffs: List[str] = []
    sid_n = 0
    seen = set()

    def source(tag: str, row_no: int, r, note: str) -> str:
        nonlocal sid_n
        sid_n += 1
        sid = f"MST-{sid_n:03d}"
        vals = [sid, "AHU_master_summary.xlsx", f"AHU Master, row {row_no}", "; ".join(contrib.get(tag, [])) or "no department workbook", note]
        rr = sid_n + 1
        for j, v in enumerate(vals, 1):
            c = so.cell(row=rr, column=j, value=v)
            c.style = "Tag" if j == 1 else "TextWrap"
            if j > 1:
                c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True, indent=1)
        return sid

    def style_row(i: int) -> None:
        for j in range(1, len(heads) + 1):
            sm.cell(row=i, column=j)._style = copy.copy(sm.cell(row=2, column=j)._style)
        sm.row_dimensions[i].height = 28

    for i in range(2, sm.max_row + 1):
        tag = sm.cell(row=i, column=1).value
        if not tag:
            continue
        seen.add(str(tag))
        hit = mrow.get(str(tag))
        k = sm.cell(row=i, column=col_k)
        sidc = sm.cell(row=i, column=col_src)
        if hit is None:
            sm.cell(row=i, column=col_s).value = "partial: no department workbook carries a room on this unit"
            coverage.append(dict(unit=str(tag), zone_rooms=sm.cell(row=i, column=6).value, zone_area_m2=sm.cell(row=i, column=7).value, counted_rooms=0, counted_area_m2=0,
                                 status="partial: no department workbook carries a room on this unit"))
            k.value = None
            note = "The unit is not in AHU_master_summary.xlsx: no department workbook carries a room on it."
            n_blank_out += 1
            sid = source(str(tag), 0, None, note)
            so.cell(row=sid_n + 1, column=3).value = "not in AHU Master"
        else:
            row_no, r = hit
            calc, tender_m = g(r, "Calculated airflow"), g(r, "Tender airflow")
            sm.cell(row=i, column=col_q).value = g(r, "Rooms")
            sm.cell(row=i, column=col_r).value = round(float(g(r, "Area") or 0), 1)
            cov_ = g(r, "Room list of the unit") or ""
            sm.cell(row=i, column=col_s).value = cov_ or None
            coverage.append(dict(unit=str(tag), zone_rooms=sm.cell(row=i, column=6).value, zone_area_m2=sm.cell(row=i, column=7).value, counted_rooms=g(r, "Rooms"),
                                 counted_area_m2=round(float(g(r, "Area") or 0), 1), status=cov_ or "complete"))
            toa_m, oa_m = g(r, "Tender outdoor airflow"), g(r, "AHU outdoor air = fraction × calculated airflow")
            sm.cell(row=i, column=col_toa).value = float(toa_m) if isinstance(toa_m, (int, float)) else None
            sm.cell(row=i, column=col_oa).value = round(float(oa_m), 1) if isinstance(calc, (int, float)) and calc > 0 and isinstance(oa_m, (int, float)) else None
            if isinstance(calc, (int, float)) and calc > 0:
                k.value = round(float(calc), 1)
                note = f"Room supply {g(r, 'Room supply'):g} L/s plus duct leakage {float(g(r, 'Duct leakage')) * 100:g} %."
                n_fill += 1
            else:
                k.value = None
                note = "The master summary gives 0 L/s: no room of the department workbooks has a supply airflow on this unit. The calculated airflow is not available."
                n_blank_in += 1
            tender_s = sm.cell(row=i, column=col_i).value
            if tender_m is not None and tender_s is None:
                sm.cell(row=i, column=col_i).value = tender_m
                sm.cell(row=i, column=col_ts).value = f"AHU_master_summary.xlsx, tender airflow: {g(r, 'Tender source') or 'see the master summary'}"
                sm.cell(row=i, column=col_code).value = "D"
                note += " Tender airflow taken from the master summary: the schedule has none."
            elif tender_m != tender_s:
                note += (f" Tender airflow differs: schedule {tender_s if tender_s is not None else 'blank'} L/s, master summary "
                         f"{tender_m if tender_m is not None else 'blank'} L/s. The tender column of this schedule is not changed.")
                diffs.append(f"{tag}: schedule {tender_s}, master {tender_m}")
            sid = source(str(tag), row_no, r, note)
        sidc.value = sid
        link_to(sidc, "Sources", f"A{sid_n + 1}")
    # units of the master summary that are not on the zoning drawing
    nxt = sm.max_row + 1
    for tag, (row_no, r) in mrow.items():
        if tag in seen:
            continue
        i = nxt
        nxt += 1
        style_row(i)
        calc, tender_m = g(r, "Calculated airflow"), g(r, "Tender airflow")
        vals = {1: tag, 2: g(r, "Type") or re.match(r"^([A-Z]+)", tag).group(1), 3: "; ".join(depts.get(tag, [])) or "no information", 4: _level_tokens(tag),
                5: plant_level(tag), 6: g(r, "Rooms"), 7: round(float(g(r, "Area") or 0), 1), 8: None, col_i: tender_m,
                col_ts: (f"AHU_master_summary.xlsx, tender airflow: {g(r, 'Tender source') or 'see the master summary'}" if tender_m is not None else
                         "No information: no tender airflow in the master summary."),
                col_k: round(float(calc), 1) if isinstance(calc, (int, float)) and calc > 0 else None,
                col_code: "D" if tender_m is not None else None}
        for j, v in vals.items():
            sm.cell(row=i, column=j).value = v
        cov_ = g(r, "Room list of the unit") or ""
        sm.cell(row=i, column=col_q).value = g(r, "Rooms")
        sm.cell(row=i, column=col_r).value = round(float(g(r, "Area") or 0), 1)
        sm.cell(row=i, column=col_s).value = cov_ or None
        coverage.append(dict(unit=tag, zone_rooms=None, zone_area_m2=None, counted_rooms=g(r, "Rooms"), counted_area_m2=round(float(g(r, "Area") or 0), 1),
                             status=cov_ or "complete (unit not on the zoning drawing)"))
        toa_m, oa_m = g(r, "Tender outdoor airflow"), g(r, "AHU outdoor air = fraction × calculated airflow")
        sm.cell(row=i, column=col_toa).value = float(toa_m) if isinstance(toa_m, (int, float)) else None
        sm.cell(row=i, column=col_oa).value = round(float(oa_m), 1) if isinstance(calc, (int, float)) and calc > 0 and isinstance(oa_m, (int, float)) else None
        from .deliver import red_formula, redp_formula, remarks_formula
        sm.cell(row=i, column=col["Reduction (L/s)"]).value = red_formula(i)
        sm.cell(row=i, column=col["Reduction (%)"]).value = redp_formula(i)
        sm.cell(row=i, column=col["Remarks"]).value = remarks_formula(i)
        note = ("Unit not on the zoning drawing. " + (f"Room supply {g(r, 'Room supply'):g} L/s plus duct leakage {float(g(r, 'Duct leakage')) * 100:g} %."
                                                     if isinstance(calc, (int, float)) and calc > 0 else "The master summary gives 0 L/s: the calculated airflow is not available."))
        sid = source(tag, row_no, r, note)
        sm.cell(row=i, column=col_src).value = sid
        link_to(sm.cell(row=i, column=col_src), "Sources", f"A{sid_n + 1}")
        n_added += 1
        if isinstance(calc, (int, float)) and calc > 0:
            n_fill += 1
    last = sm.max_row
    sm.auto_filter.ref = f"A1:{get_column_letter(len(heads))}{last}"
    sm.print_area = f"A1:{get_column_letter(len(heads))}{last}"
    sm.conditional_formatting.add(f"T2:T{last}", FormulaRule(formula=["AND(ISNUMBER($T2),ISNUMBER($U2),$U2>$T2)"], fill=PatternFill("solid", bgColor="FCE8B2", fgColor="FCE8B2")))
    sm.conditional_formatting.add(f"N2:N{last}", FormulaRule(formula=['LEFT($N2,6)="VERIFY"'], fill=PatternFill("solid", bgColor="FCE8B2", fgColor="FCE8B2")))
    from ..workbook.styles import setup_sheet
    so_last = sid_n + 1
    from ..workbook import conf
    from .deliver import _project
    setup_sheet(so, "Sources", rev="A", date=conf.revision_date(), project=_project(), print_rows="1:1", last_col=5, last_row=so_last, zoom=100)
    ws = wb["README"]
    for row in ws.iter_rows():
        for c in row:
            if c.value == README_OLD_ENTER:
                c.value = README_NEW_ENTER
            elif c.value == README_OLD_RED:
                c.value = README_NEW_RED
    wb.save(ns.schedule)
    # recalculate, so that the stored values of the formulas are present for viewers that do not calculate
    import shutil
    import tempfile
    from ..workbook.recalc import recalc
    with tempfile.TemporaryDirectory(prefix="zs_rc_") as t:
        shutil.copy(recalc(Path(ns.schedule), Path(t)), ns.schedule)
    # check: rooms and area per unit, workbooks against the zone schedule
    import csv
    qa = Path("out/qa/unit_room_coverage.csv")
    qa.parent.mkdir(parents=True, exist_ok=True)
    with qa.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["unit", "zone_rooms", "zone_area_m2", "counted_rooms", "counted_area_m2", "status"])
        w.writeheader()
        w.writerows(coverage)
    n_part = sum(1 for c in coverage if str(c["status"]).startswith("partial"))
    print(f"unit room coverage: {len(coverage) - n_part} complete, {n_part} partial of {len(coverage)} units ({qa})")
    print(f"{ns.schedule}: Calculated airflow filled for {n_fill} unit(s) ({n_added} added from the master summary); blank, unit in master but no room supply: {n_blank_in}; "
          f"blank, unit not in master: {n_blank_out}; tender differences listed: {len(diffs)}")
    for d in diffs:
        print("  tender difference", d)
    return 0


if __name__ == "__main__":
    sys.exit(main())
