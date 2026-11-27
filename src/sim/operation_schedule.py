"""Schedule of operation of the rooms and units (Marcus, 27 Nov 2026).

Each room takes an operation profile: a 24 h grid for each day type (Mon-Fri, Saturday, Sunday / public holiday) with the codes
O = operating, R = reduced capacity (VAV minimum / set-back), S = shut down. The part-load check (src.sim.partload_reheat) reads the same
profiles, so the schedule in the workbook and the hourly results agree.

Assignment (in this order): an Engineer's override in out/registers/operation_profile_overrides.csv (room, profile, basis); DD Booklet
after-hours operation (criteria 'Y' or '24 hrs AC') -> 24H; plan tag AC (24HR) on the zoning -> 24H; otherwise OFFICE (ASSUMPTION: the
DD Booklets give no working hours; Mon-Fri 08:00-18:00).
"""
from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OVERRIDES = ROOT / "out/registers/operation_profile_overrides.csv"
DAYS = ("Mon-Fri", "Sat", "Sun/PH")


def _grid(wd, sat=None, sun=None):
    """wd / sat / sun: dict of code -> list of (start, end) hours; the rest of the day is S."""
    out = []
    for spec in (wd, sat if sat is not None else {}, sun if sun is not None else {}):
        day = ["S"] * 24
        for code, spans in spec.items():
            for a, b in spans:
                for h in range(a, b):
                    day[h] = code
        out.append(day)
    return out


ALL = {"O": [(0, 24)]}
PROFILES = {
    "24H": ("Operating 24 h, every day (wards, critical care, 24 h support rooms)", _grid(ALL, ALL, ALL)),
    "OFFICE": ("Mon-Fri 08:00-18:00; shut down after hours, Saturday, Sunday and public holidays", _grid({"O": [(8, 18)]})),
    "OFFICE+SAT": ("Mon-Fri 08:00-18:00 and Saturday 08:00-13:00", _grid({"O": [(8, 18)]}, {"O": [(8, 13)]})),
    "EXTENDED": ("Every day 07:00-21:00 (outpatient / clinic with extended hours)", _grid({"O": [(7, 21)]}, {"O": [(7, 21)]}, {"O": [(7, 21)]})),
    "24H-REDUCED-NIGHT": ("Operating 07:00-21:00, reduced capacity 21:00-07:00, every day",
                          _grid({"O": [(7, 21)], "R": [(0, 7), (21, 24)]}, {"O": [(7, 21)], "R": [(0, 7), (21, 24)]},
                                {"O": [(7, 21)], "R": [(0, 7), (21, 24)]})),
    "OFFICE-REDUCED": ("Mon-Fri 08:00-18:00; reduced capacity at other times (rooms that must hold conditions out of hours)",
                       _grid({"O": [(8, 18)], "R": [(0, 8), (18, 24)]}, {"R": [(0, 24)]}, {"R": [(0, 24)]})),
    "SHUT": ("Not operated", _grid({})),
}
SLOTS = 12          # rows reserved on the Profiles sheet (spare rows for new profiles)


def overrides():
    if not OVERRIDES.exists():
        return {}
    return {r["room"]: (r["profile"], r.get("basis", "Engineer")) for r in csv.DictReader(OVERRIDES.open(encoding="utf-8"))}


def assign(room: dict, plan24: set, ov: dict | None = None):
    """-> (profile, basis). room: no, after (DD Booklet after-hours), hours (operation time h/day)."""
    ov = overrides() if ov is None else ov
    if room["no"] in ov:
        return ov[room["no"]][0], f"Engineer: {ov[room['no']][1]}"
    a = str(room.get("after") or "").strip().upper()
    if a.startswith("Y") or "24" in a:
        return "24H", f"DD Booklet after-hours operation ('{room.get('after')}')"
    if isinstance(room.get("hours"), (int, float)) and room["hours"] >= 24:
        return "24H", "operation time 24 h"
    if room["no"] in plan24:
        return "24H", "plan tag AC (24HR)"
    return "OFFICE", "ASSUMPTION: working hours (no 24 h note)"


def codes(profile: str):
    return PROFILES.get(profile, PROFILES["OFFICE"])[1]


# ------------------------------------------------------------------------------------------------------------------ workbook sheets
def write_sheets(wb, rooms, title_note=""):
    """rooms: dicts workbook, no, name, storey, unit, design, vmin, profile, basis. Adds Profiles, Schedule of Operation, Unit Operation and
    Unit Airflow by Hour."""
    from openpyxl.formatting.rule import CellIsRule, ColorScaleRule
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter as L
    from openpyxl.worksheet.datavalidation import DataValidation

    HF, HFILL = Font(name="Arial", bold=True, color="FFFFFF", size=9), PatternFill("solid", fgColor="2F4F6F")
    DAYFILL = [PatternFill("solid", fgColor=c) for c in ("2F4F6F", "5B7083", "7D8E9E")]
    F9, FIN = Font(name="Arial", size=9), Font(name="Arial", size=9, color="0000CC")
    GREEN, AMBER, GREY = PatternFill("solid", fgColor="63BE7B"), PatternFill("solid", fgColor="FFC000"), PatternFill("solid", fgColor="E7E6E6")
    CENTER = Alignment(horizontal="center", vertical="center")
    SEP = Side(style="medium", color="2F4F6F")

    def hour_header(ws, row, c0):
        for d, day in enumerate(DAYS):
            a = c0 + 24 * d
            ws.merge_cells(start_row=row, start_column=a, end_row=row, end_column=a + 23)
            c = ws.cell(row, a, day)
            c.font, c.fill, c.alignment = HF, DAYFILL[d], CENTER
            for h in range(24):
                cc = ws.cell(row + 1, a + h, h)
                cc.font, cc.alignment = Font(name="Arial", size=7, bold=True), CENTER
                cc.fill = PatternFill("solid", fgColor="DCE3EA")
                ws.column_dimensions[L(a + h)].width = 2.6
            for rr in (row, row + 1):
                ws.cell(rr, a).border = Border(left=SEP)

    def code_format(ws, ref):
        ws.conditional_formatting.add(ref, CellIsRule(operator="equal", formula=['"O"'], fill=GREEN, font=Font(color="1E5E2E", size=7)))
        ws.conditional_formatting.add(ref, CellIsRule(operator="equal", formula=['"R"'], fill=AMBER, font=Font(color="7F5F00", size=7)))
        ws.conditional_formatting.add(ref, CellIsRule(operator="equal", formula=['"S"'], fill=GREY, font=Font(color="A6A6A6", size=7)))

    def head(ws, row, labels, widths):
        for j, (t, w) in enumerate(zip(labels, widths), 1):
            c = ws.cell(row, j, t)
            c.font, c.fill, c.alignment = HF, HFILL, Alignment(wrap_text=True, vertical="center", horizontal="center")
            ws.merge_cells(start_row=row, start_column=j, end_row=row + 1, end_column=j)
            ws.column_dimensions[L(j)].width = w

    # ---------------- Profiles
    wp = wb.create_sheet("Profiles")
    wp["A1"] = "Operation profiles"
    wp["A1"].font = Font(name="Arial", bold=True, size=13)
    wp["A2"] = ("Codes by hour (hour 8 = 08:00-09:00). O = operating, R = reduced capacity (VAV minimum / set-back), S = shut down. Edit a grid or add a "
                "profile in a spare row; every room that uses it follows (sheet Schedule of Operation). Public holidays follow Sun/PH.")
    wp["A2"].font = F9
    PC = 4                                   # first hour column
    head(wp, 4, ["Profile", "Description", "Weekly operating h"], [20, 60, 9])
    hour_header(wp, 4, PC)
    names = list(PROFILES)
    for i in range(SLOTS):
        r = 6 + i
        nm = names[i] if i < len(names) else None
        if nm:
            wp.cell(r, 1, nm).font = Font(name="Arial", size=9, bold=True, color="0000CC")
            wp.cell(r, 2, PROFILES[nm][0]).font = FIN
            for d in range(3):
                for h in range(24):
                    c = wp.cell(r, PC + 24 * d + h, PROFILES[nm][1][d][h])
                    c.font, c.alignment = FIN, CENTER
        a, b = L(PC), L(PC + 23)
        wp.cell(r, 3, f'=IF(A{r}="","",5*COUNTIF({a}{r}:{b}{r},"O")+COUNTIF({L(PC+24)}{r}:{L(PC+47)}{r},"O")+COUNTIF({L(PC+48)}{r}:{L(PC+71)}{r},"O"))').font = F9
    code_format(wp, f"{L(PC)}6:{L(PC + 71)}{5 + SLOTS}")
    dvc = DataValidation(type="list", formula1='"O,R,S"', allow_blank=True)
    wp.add_data_validation(dvc)
    dvc.add(f"{L(PC)}6:{L(PC + 71)}{5 + SLOTS}")
    wp.freeze_panes = wp.cell(6, PC)
    PROF_GRID = f"Profiles!${L(PC)}$6:${L(PC + 71)}${5 + SLOTS}"
    PROF_NAMES = f"Profiles!$A$6:$A${5 + SLOTS}"

    # ---------------- Schedule of Operation (rooms)
    ws = wb.create_sheet("Schedule of Operation")
    ws["A1"] = "Schedule of operation by room"
    ws["A1"].font = Font(name="Arial", bold=True, size=13)
    ws["A2"] = ("Choose the profile of each room (column J, list of sheet Profiles); the 24 h of each day type follow by formula. Blue = input. "
                "Basis: why the profile was assigned; rows marked ASSUMPTION have no 24 h note in the DD Booklet or on the plans. " + title_note)
    ws["A2"].font = F9
    labels = ["Workbook", "Room", "Name", "Storey", "Unit", "Design airflow L/s", "VAV minimum L/s", "Weekly operating h", "Weekly reduced h",
              "Profile", "Basis"]
    widths = [9, 17, 26, 6, 26, 8, 8, 8, 8, 18, 30]
    head(ws, 4, labels, widths)
    C0 = len(labels) + 1
    hour_header(ws, 4, C0)
    rooms = sorted(rooms, key=lambda x: (x["unit"] in ("", "none", None), x["unit"] or "", x["storey"] or "", x["no"]))
    first, last = 6, 5 + len(rooms)
    for i, x in enumerate(rooms):
        r = first + i
        for j, v in enumerate((x["workbook"], x["no"], x["name"], x["storey"], x["unit"] or "none", x["design"], x["vmin"]), 1):
            ws.cell(r, j, v).font = F9
        ws.cell(r, 10, x["profile"]).font = FIN
        ws.cell(r, 11, x["basis"]).font = Font(name="Arial", size=9, color="C00000" if x["basis"].startswith("ASSUMPTION") else "000000")
        for k in range(72):
            c = ws.cell(r, C0 + k, f"=INDEX({PROF_GRID},MATCH($J{r},{PROF_NAMES},0),{k + 1})")
            c.alignment = CENTER
        a, b = L(C0), L(C0 + 23)
        sa, sb, ua, ub = L(C0 + 24), L(C0 + 47), L(C0 + 48), L(C0 + 71)
        for col, code in ((8, "O"), (9, "R")):
            ws.cell(r, col, f'=5*COUNTIF({a}{r}:{b}{r},"{code}")+COUNTIF({sa}{r}:{sb}{r},"{code}")+COUNTIF({ua}{r}:{ub}{r},"{code}")').font = F9
    code_format(ws, f"{L(C0)}{first}:{L(C0 + 71)}{last}")
    dv = DataValidation(type="list", formula1=PROF_NAMES, allow_blank=False)
    ws.add_data_validation(dv)
    dv.add(f"J{first}:J{last}")
    ws.freeze_panes = ws.cell(first, 3)
    ws.auto_filter.ref = f"A5:K{last}"
    for d in range(3):
        for r in range(first, last + 1):
            ws.cell(r, C0 + 24 * d).border = Border(left=SEP)

    # ---------------- Unit Operation
    units = {}
    for x in rooms:
        if x["unit"] and x["unit"] != "none":
            u = units.setdefault(x["unit"], dict(n=0, design=0.0))
            u["n"] += 1
            u["design"] += float(x["design"] or 0)
    wu = wb.create_sheet("Unit Operation")
    wu["A1"] = "Operation of each unit by hour (from the rooms it serves)"
    wu["A1"].font = Font(name="Arial", bold=True, size=13)
    wu["A2"] = ("O = all rooms of the unit operating (full capacity). R = reduced capacity: only part of the rooms operating, or rooms at reduced "
                "capacity. S = all rooms shut down: the unit can be shut down. Formulas over sheet Schedule of Operation.")
    wu["A2"].font = F9
    head(wu, 4, ["Unit", "Rooms", "Design airflow L/s", "Hours O per week", "Hours R per week", "Hours S per week"], [28, 6, 9, 8, 8, 8])
    U0 = 7
    hour_header(wu, 4, U0)
    unitcol = f"'Schedule of Operation'!$E${first}:$E${last}"
    for i, (u, d) in enumerate(sorted(units.items())):
        r = 6 + i
        wu.cell(r, 1, u).font = F9
        wu.cell(r, 2, d["n"]).font = F9
        wu.cell(r, 3, round(d["design"])).font = F9
        for k in range(72):
            col = f"'Schedule of Operation'!{L(C0 + k)}${first}:{L(C0 + k)}${last}"
            c = wu.cell(r, U0 + k, f'=IF(COUNTIFS({unitcol},$A{r},{col},"S")=$B{r},"S",IF(COUNTIFS({unitcol},$A{r},{col},"O")=$B{r},"O","R"))')
            c.alignment = CENTER
        a, b = L(U0), L(U0 + 23)
        sa, sb, ua, ub = L(U0 + 24), L(U0 + 47), L(U0 + 48), L(U0 + 71)
        for col, code in ((4, "O"), (5, "R"), (6, "S")):
            wu.cell(r, col, f'=5*COUNTIF({a}{r}:{b}{r},"{code}")+COUNTIF({sa}{r}:{sb}{r},"{code}")+COUNTIF({ua}{r}:{ub}{r},"{code}")').font = F9
    ulast = 5 + len(units)
    code_format(wu, f"{L(U0)}6:{L(U0 + 71)}{ulast}")
    wu.freeze_panes = wu.cell(6, 2)
    wu.auto_filter.ref = f"A5:F{ulast}"

    # ---------------- Unit Airflow by Hour
    wa = wb.create_sheet("Unit Airflow by Hour")
    wa["A1"] = "Airflow of each unit by hour, share of the design airflow"
    wa["A1"].font = Font(name="Arial", bold=True, size=13)
    wa["A2"] = ("Rooms operating at design airflow (O) plus rooms at reduced capacity at their VAV minimum (R), divided by the design airflow of "
                "the unit. The share at O hours is the turn-down limit, not the load: the VAV boxes follow the room load between minimum and design.")
    wa["A2"].font = F9
    head(wa, 4, ["Unit", "Design airflow L/s"], [28, 9])
    A0 = 3
    hour_header(wa, 4, A0)
    des = f"'Schedule of Operation'!$F${first}:$F${last}"
    vmn = f"'Schedule of Operation'!$G${first}:$G${last}"
    for i, (u, d) in enumerate(sorted(units.items())):
        r = 6 + i
        wa.cell(r, 1, u).font = F9
        wa.cell(r, 2, round(d["design"])).font = F9
        for k in range(72):
            col = f"'Schedule of Operation'!{L(C0 + k)}${first}:{L(C0 + k)}${last}"
            c = wa.cell(r, A0 + k, f'=IF($B{r}=0,0,(SUMIFS({des},{unitcol},$A{r},{col},"O")+SUMIFS({vmn},{unitcol},$A{r},{col},"R"))/$B{r})')
            c.number_format = "0%"
            c.font = Font(name="Arial", size=6)
            c.alignment = CENTER
    wa.conditional_formatting.add(f"{L(A0)}6:{L(A0 + 71)}{ulast}",
                                  ColorScaleRule(start_type="num", start_value=0, start_color="F2F2F2", mid_type="num", mid_value=0.5,
                                                 mid_color="FFD966", end_type="num", end_value=1, end_color="63BE7B"))
    wa.freeze_panes = wa.cell(6, 2)
    for w_ in (wp, ws, wu, wa):
        w_.sheet_view.showGridLines = False
        w_.sheet_view.zoomScale = 85
    return dict(rooms=len(rooms), units=len(units))


def finalise(path, timeout=900):
    """Recalculate with the local spreadsheet engine (headless, no network) so the file carries values in any viewer; neutral properties."""
    import re
    import shutil
    import subprocess
    import tempfile
    import zipfile
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["soffice", "--headless", "--calc", "--convert-to", "xlsx", "--outdir", td, str(Path(path).resolve())], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout, cwd=td)
        src_ = Path(td) / Path(path).name
        tmp = Path(str(path) + ".tmp")
        with zipfile.ZipFile(src_) as zi, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zo:
            for it in zi.infolist():
                d = zi.read(it.filename)
                if it.filename == "docProps/app.xml":
                    d = re.sub(rb"<Application>.*?</Application>", b"<Application>Microsoft Excel</Application>", d)
                    d = re.sub(rb"<AppVersion>.*?</AppVersion>", b"", d)
                if it.filename == "docProps/core.xml":
                    d = re.sub(rb"<dc:creator>.*?</dc:creator>", b"<dc:creator>AIGH ACMV</dc:creator>", d)
                    d = re.sub(rb"<cp:lastModifiedBy>.*?</cp:lastModifiedBy>", b"<cp:lastModifiedBy>AIGH ACMV</cp:lastModifiedBy>", d)
                zo.writestr(it, d)
        shutil.move(tmp, path)
