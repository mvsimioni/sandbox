"""Zone schedule (xlsx) and checks for the issued zoning PDF.

``python3 -m src.zoning.deliver schedule``  writes ``out/zoning/zone_schedule.xlsx`` from ``out/registers/zone_assignment_<KEY>.csv``.
``python3 -m src.zoning.deliver check``     checks the PDF (annotations, layers, legend, index, colours) and the room coverage; writes ``out/qa/zoning_deliverable_checks.json``.

Text that goes into the schedule follows ASD-STE100 and cites documents by number, issue, sheet and page only (see ``text_clean``).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from openpyxl import Workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties

from .. import common
from . import text_clean as tc
from .storeys import ALL_STOREYS, AHU_RE, display_tag, out_pdf_path, schedule_plant_level, storey_info, zone_key

NON_UNIT = {"MV", "NV", "no information"}
INK, PAPER, RULE, BAND = "1F2A33", "FFFFFF", "B8C0C6", "EEF1F3"
AMBER = "FCE8B2"
SANS, MONO = "Arial", "Consolas"


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
def _num(x: str) -> Optional[float]:
    try:
        return float(str(x).replace(",", "").strip())
    except ValueError:
        return None


def load_rooms(storeys: List[str]) -> Dict[str, List[dict]]:
    P = common.resolve_paths()
    out = {}
    for k in storeys:
        rows = common.read_csv_dicts(P["registers"] / ("zone_assignment_%s.csv" % k))
        out[k] = rows
    return out


from ..workbook.common import THERMAL_NOTE, THERMAL_NOTE_SHORT  # noqa: E402
from ..workbook.tender_lookup import Tender  # noqa: E402,F401   (one tender lookup for every deliverable)


def build_units(rooms: Dict[str, List[dict]]) -> Dict[str, dict]:
    units: Dict[str, dict] = {}
    for k, rows in rooms.items():
        for r in rows:
            cls = r["class"].strip()
            if cls in NON_UNIT or cls.lower().startswith("review"):
                continue
            zk, disp = zone_key(cls)
            u = units.setdefault(zk, {"tag": disp, "tags": set(), "depts": set(), "storeys": [], "n": 0, "area": 0.0, "conf": 1.0})
            u["tags"].add(cls)
            if r["department"]:
                u["depts"].add(tc.clean_dept(r["department"], cls))
            if k not in u["storeys"]:
                u["storeys"].append(k)
            u["n"] += 1
            u["area"] += _num(r["area_m2"]) or 0.0
            u["conf"] = min(u["conf"], _num(r["confidence"]) or 0.0)
    for zk, u in units.items():
        t = sorted(u["tags"])
        u["tag"] = display_tag(t)
    return units


from ..workbook.tag_notes import cath_tag_note, unit_note  # noqa: E402


def plant_level(tag: str) -> str:
    """Plant level from the tag pattern AHU-B-<plant level>-<served level><name>-<no.>, cross-checked with Equipment Schedule (Corr 5).
    Basement tags and tags that do not follow the pattern take the schedule value."""
    from .zones import parse_tag_levels
    from_tag = parse_tag_levels(tag.split("/")[0])[0]
    sched = schedule_plant_level(tag)
    if from_tag:
        lvl = "L" + from_tag
        if sched and sched != lvl:
            return "%s (tag); Equipment Schedule (Corr 5) gives %s" % (lvl, sched)
        return lvl
    return sched or "no information (the tag does not give it and the unit is not in Equipment Schedule (Corr 5))"


# --------------------------------------------------------------------------- #
# workbook
# --------------------------------------------------------------------------- #
_FMT_INT = '#,##0;"−"#,##0;"–"'
_FMT_D1 = '#,##0.0;"−"#,##0.0;"–"'
_FMT_D2 = '0.00;"−"0.00;"–"'
_FMT_PCT = '0.0%;"−"0.0%;"–"'
NUM_HEADS = {"Rooms", "Area (m²)", "Rooms counted in the department workbooks", "Area counted in the department workbooks (m²)", "Lowest room confidence", "Tender airflow (L/s)", "Calculated airflow (L/s)", "Reduction (L/s)", "Reduction (%)", "Confidence"}
LEFT_HEADS = {"Remarks", "Sources", "Tender airflow source", "Departments", "Room name", "Department"}


def _project() -> str:
    from ..workbook import conf
    return str((conf.config().get("project") or {}).get("name") or "Project")


def _head(ws, row: int, labels: List[str], widths: List[float]) -> None:
    """Header row in the house style: hairline rule under it, numeric headers right-aligned, text headers left-aligned."""
    for i, (lab, w) in enumerate(zip(labels, widths), 1):
        c = ws.cell(row=row, column=i, value=lab)
        c.style = "HeaderLeft" if lab not in NUM_HEADS else "Header"
        if lab in NUM_HEADS:
            c.alignment = Alignment(horizontal="right", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.row_dimensions[row].height = 42


def _print_setup(ws, last_col: int, last_row: int, title_row: int = 1, head: str = "", date: str = "") -> None:
    from ..workbook.styles import setup_sheet
    setup_sheet(ws, ws.title, rev="A", date=date, project=_project(), print_rows="%d:%d" % (title_row, title_row), last_col=last_col, last_row=max(last_row, 2),
                zoom=100)
    ws.page_setup.fitToWidth = 1
    ws.page_setup.pageOrder = "overThenDown"


README = [
    ("AHU zone schedule, LB3 to L07", "t"),
    ("This workbook lists each room and the air system that serves it. It goes with the AHU zoning drawing AIGH_IP_AHU_Zoning_LB3-L07.pdf.", ""),
    ("Sheets", "h"),
    ("AHU summary: one row for each air handling unit or fan coil unit, and for each duty and standby pair.", ""),
    ("LB3, B3, B2, B1, L01 to L07: one row for each room on that storey.", ""),
    ("Room sheets", "h"),
    ("System / class shows the AHU or FCU tag that serves the room. MV means mechanical ventilation only. NV means natural ventilation.", ""),
    ("No information means the documents do not show the system for the room. The Remarks column gives the reason. No AHU is assumed.", ""),
    ("A duty and standby pair is one zone. The tag ends in A/B.", ""),
    ("Area is the measured area of the room on the tender plan. It is blank where the plan shows no closed room area.", ""),
    ("Confidence is a number from 0 to 1. A value below 0.9 has an amber fill. Check these rooms first.", ""),
    ("Sources lists the documents with number, issue, sheet and page. Remarks give the basis of the class and any difference between documents.", ""),
    ("Basis gives the source that sets the class, in this order: RFI-ME-0233 zoning, MOT NSC, DD Booklet zoning, working air-side schematic, App D, tender air-side schematic, plan mode label. No unit evidence means that no document gives the system for the room.", ""),
    ("AHU summary", "h"),
    ("Departments, Storeys served and Plant level come from the room sheets and from the unit tag.", ""),
    ("Tender airflow is the supply airflow in L/s. The first source is Equipment Schedule SCH/1 (Corr 5). For the units of the MOT NSC (operating theatres, day surgery theatres, sterile services, cath lab and angiography), the source is the MOT NSC Overall AC Equipment Schedule (Corr 2); the tender air-side schematics IP-BH-16xxx show the same units. If no schedule has a figure, the source is the tender air-side schematic. The next column names the source.", ""),
    ("A unit is looked up by its tag with and without the suffix A or B. Where a schedule prints another form of the tag, the source column states the tag as the schedule prints it.", ""),
    ("Plant level comes from the tag (AHU-B-<plant level>-<served level><name>-<no.>). It is checked against Equipment Schedule (Corr 5). Basement tags take the level from the schedule.", ""),
    ("If neither source has a figure, the cell is blank and the source column gives the reason.", ""),
    ("The Calculated airflow column holds the supply airflow in L/s from AHU_master_summary.xlsx (tab AHU Master): the room supply of the department workbooks that carry rooms on the unit, plus the duct leakage. The Calculated airflow source ID column points to the Sources sheet, which names the master row and the department workbooks. The values are numbers: they do not update when a workbook changes.", ""),
    ("Reduction (L/s) = Tender airflow minus Calculated airflow. Reduction (%) = Reduction divided by Tender airflow. Both are formulas. They show a value only where the calculated airflow is below the tender airflow and the room list of the unit is complete. Otherwise they are blank and the Remarks cell says nothing about a reduction.", ""),
    ("Room list of the unit: the department workbooks add up only the rooms that they hold. The cell reads 'Complete' or 'Not complete' and states 'n of m zoned rooms counted'. A unit is complete when the rooms counted with a criteria row and a supply airflow are at least as many as the rooms that this schedule gives the unit, no counted room lacks a criteria row, and the counted area is at least the share 0.9 of the zoned area of the unit. A unit with a zoned area of 0 m² is not complete. A unit that the zone schedule gives 2 rooms or fewer and that shows a reduction is marked in its Remarks cell as not confirmed (OI-R206-1, open for Marcus); the reduction is kept as the rule gives it.", ""),
    ("Tender outdoor airflow is the outdoor airflow of the same schedule row (Outdoor Airflow / Min), or of the tender air-side schematic where the schedule has none. Calculated outdoor air is the outdoor air of the unit in AHU_master_summary.xlsx. Where the calculated outdoor air is above the tender outdoor air, the Remarks cell says VERIFY and the reduction cells of the unit stay blank. A calculated airflow above the tender airflow also gets VERIFY, whatever the state of the room list. Both columns are blank where no figure exists.", ""),
    ("The calculated airflow includes people, lighting and small power. Lighting and small power are ASHRAE values where no project document gives them. Equipment gain is not included and envelope gain is not assessed. A reduction that is stated is therefore an upper limit.", ""),
    ("The Remarks cell is a formula. It states the tender airflow, the calculated airflow and the change. If the calculated airflow is more than the tender airflow, the Remarks cell starts with VERIFY.", ""),
    ("Schematic and schedule airflows are for comparison only. They do not set the zone.", ""),
    ("Units that are in AHU_master_summary.xlsx and not on the zoning drawing are listed after the zoned units. Their Departments, Storeys and Rooms come from the master summary.", ""),
    ("Documents", "h"),
    ("Room tags and mode labels: Arch Combined Tender, sheets AR-IP-01-LB3-001 to AR-IP-01-07-001, issue as shown in the Sources column.", ""),
    ("AHU tags and served rooms: air-side department schematics, Equipment Schedule SCH/1 (Corr 5), Appendix D ACMV for Common Areas, and the DD Booklets, as cited for each room.", ""),
    ("Diagnostic Imaging on L04: RFI-ME-0233 governs. Other departments: the later dated document governs and the difference is shown in the Remarks column.", ""),
]


def oa_above_f(i: int) -> str:
    """Excel boolean: calculated outdoor air (column U) above the tender outdoor airflow (column T) of row i."""
    return f"AND(ISNUMBER(T{i}),ISNUMBER(U{i}),U{i}>T{i})"


def incomplete_f(i: int) -> str:
    """Excel boolean: the room list of the unit (column S) is not complete, or not known (D-027)."""
    return f'OR(S{i}="",ISNUMBER(SEARCH("Not complete",S{i})))'


def red_formula(i: int, zoned: bool = False) -> str:
    """Reduction (L/s): stated only where the calculated airflow is below tender, the room list of the unit is complete and the outdoor air is not above the tender outdoor
    airflow. A unit with a zoned area of 0 m² is not complete (open for Marcus)."""
    parts = ['I%d=""' % i, 'K%d=""' % i, incomplete_f(i)] + (['G%d=0' % i] if zoned else []) + [oa_above_f(i), 'K%d>=I%d' % (i, i)]
    return '=IF(OR(%s),"",I%d-K%d)' % (",".join(parts), i, i)


def redp_formula(i: int) -> str:
    return '=IF(OR(L%d="",I%d=0),"",L%d/I%d)' % (i, i, i, i)


NO_POLYGON_NOTE = ("No zone polygon: the room tags of this unit lie in an open region that has no closed outline, so the zoned area is 0 m² and the unit is not drawn "
                   "on the zoning PDF. ")


def remarks_formula(i: int, zoned: bool = False, on_drawing: bool = True, note: str = None) -> str:
    """Remarks of the AHU summary (D-027). The text states VERIFY where the calculated airflow is above tender and where the calculated outdoor air is above the tender outdoor
    airflow (every unit, whatever the state of the room list). It states a reduction only where the calculated airflow is below tender and the room list of the unit is
    complete. In every other case it says nothing about a reduction. 'The reduction is an upper limit.' stands on a text that states a reduction and on no other.
    A unit that the zone schedule gives 2 rooms or fewer is marked as not confirmed (open for Marcus). `note` is a fixed note that the documents print for the unit."""
    oa = ('IF({oa},"Outdoor air "&TEXT(U{r},"#,##0")&" L/s is above tender outdoor air "&TEXT(T{r},"#,##0")&" L/s ("&J{r}&"). VERIFY.","")').format(oa=oa_above_f(i), r=i)
    small = ('IF(AND(ISNUMBER(F{r}),F{r}>0,F{r}<=2)," Interim, open for Marcus (OI-R206-1): the zone schedule gives this unit "&F{r}&IF(F{r}=1," room"," rooms")&" ("&TEXT(G{r},"#,##0")&" m²). '
             'The reduction may come from an incomplete room list. It is not a confirmed result.","")').format(r=i)
    if not on_drawing:
        small = '""'
    f = ('IF(I{r}="",J{r}&IF(ISNUMBER(SEARCH(".",J{r},LEN(J{r}))),"","."),IF(K{r}="","Tender "&TEXT(I{r},"#,##0")&" L/s ("&J{r}&"). Calculated airflow not available.",'
         'IF(K{r}>I{r},"VERIFY: increase from "&TEXT(I{r},"#,##0")&" to "&TEXT(K{r},"#,##0")&" L/s (+"&TEXT((K{r}-I{r})/I{r},"0.0 %")&") against tender ("&J{r}&"). "&' + oa + ','
         'IF(' + oa_above_f(i) + ',"Supply "&TEXT(K{r},"#,##0")&" L/s against tender "&TEXT(I{r},"#,##0")&" L/s ("&J{r}&"). "&' + oa + ','
         'IF(AND(ISNUMBER(L{r}),NOT(' + incomplete_f(i) + ')),"Reduced "&TEXT(I{r},"#,##0")&" to "&TEXT(K{r},"#,##0")&" L/s (−"&TEXT((I{r}-K{r})/I{r},"0.0 %")&") against tender ("&J{r}&")."&' + small + '&" ' + THERMAL_NOTE + '","")))))').format(r=i)
    if zoned:
        f = 'IF(G%d=0,"%s","")&' % (i, NO_POLYGON_NOTE) + f
    if note:
        f = 'IF(LEN(%s)=0,"%s",%s&" %s")' % (f, note.replace('"', '""'), f, note.replace('"', '""'))
    return "=" + f


def build_xlsx(storeys: List[str], out: Path) -> dict:
    from ..workbook import conf, styles as wstyles
    from ..workbook.styles import install_styles
    wstyles.FOOTER_DEPT = "AHU zone schedule"
    date = conf.revision_date()
    rooms = load_rooms(storeys)
    units = build_units(rooms)
    tender = Tender()
    wb = Workbook()
    install_styles(wb)
    ws = wb.active
    ws.title = "README"
    ws.column_dimensions["A"].width = 2
    ws.column_dimensions["B"].width = 150
    r = 1
    for text, kind in README:
        if kind == "t":
            c = ws.cell(row=r, column=2, value=text)
            c.style = "Title"
            ws.row_dimensions[r].height = 28
        elif kind == "h":
            r += 1
            c = ws.cell(row=r, column=2, value=text)
            c.style = "Section"
        else:
            c = ws.cell(row=r, column=2, value=text)
            c.style = "TextWrap"
            ws.row_dimensions[r].height = 15.0 * max(1, -(-len(text) // 165)) + 2
        r += 1
    from ..workbook.styles import setup_sheet
    setup_sheet(ws, "README", rev="A", date=date, project=_project(), print_rows="1:1", last_col=2, last_row=r, zoom=100)
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 1

    # ---- AHU summary -----------------------------------------------------------------------------
    sm = wb.create_sheet("AHU summary")
    heads = ["AHU or FCU tag", "Type", "Departments", "Storeys served", "Plant level", "Rooms", "Area (m²)", "Lowest room confidence",
             "Tender airflow (L/s)", "Tender airflow source", "Calculated airflow (L/s)", "Reduction (L/s)", "Reduction (%)", "Remarks", "Code",
             "Calculated airflow source ID", "Rooms counted in the department workbooks", "Area counted in the department workbooks (m²)", "Room list of the unit",
             "Tender outdoor airflow (L/s)", "Calculated outdoor air (L/s)"]
    widths = [26, 7, 30, 12, 24, 8, 10, 11, 12, 40, 12, 12, 11, 62, 6, 16, 12, 12, 40, 12, 12]
    _head(sm, 1, heads, widths)
    n_no_tender = 0
    tender_conf: List[dict] = []
    order = sorted(units, key=lambda z: (min(ALL_STOREYS.index(s) for s in units[z]["storeys"]), z))
    for i, zk in enumerate(order, 2):
        u = units[zk]
        tags = sorted(u["tags"])
        lk = tender.lookup(tags)
        val, src = lk["value"], lk["text"]
        if lk.get("others"):
            tender_conf.append(dict(lk, tag=u["tag"]))
        if val is None:
            n_no_tender += 1
        typ = re.match(r"^([A-Z]+)", tags[0]).group(1)
        row = [u["tag"], typ, "; ".join(sorted(u["depts"])) or "no information", ", ".join(u["storeys"]), plant_level(tags[0]), u["n"], round(u["area"], 1), u["conf"],
               val, src, None,
               red_formula(i, True), redp_formula(i), remarks_formula(i, True, note=next((unit_note(t) for t in tags if unit_note(t)), None)),
               "D" if val is not None else None, None, None, None, None, None, None]
        styles_ = ["Tag", "Text", "TextWrap", "Text", "TextWrap", "Calc", "Calc", "Calc", "Input", "TextWrap", "Input", "Calc", "Calc", "Remark", "Code", "Tag", "Calc", "Calc", "TextWrap", "Input", "Calc"]
        for j, v in enumerate(row, 1):
            c = sm.cell(row=i, column=j, value=v)
            c.style = styles_[j - 1]
            if styles_[j - 1] in ("TextWrap", "Remark"):
                c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True, indent=1)
            elif styles_[j - 1] in ("Text", "Tag"):
                c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        sm.cell(row=i, column=6).number_format = _FMT_INT
        sm.cell(row=i, column=7).number_format = _FMT_D1
        sm.cell(row=i, column=8).number_format = _FMT_D2
        sm.cell(row=i, column=9).number_format = _FMT_INT
        sm.cell(row=i, column=11).number_format = _FMT_INT
        sm.cell(row=i, column=12).number_format = _FMT_INT
        sm.cell(row=i, column=13).number_format = _FMT_PCT
        sm.cell(row=i, column=20).number_format = _FMT_INT
        sm.cell(row=i, column=21).number_format = _FMT_INT
        sm.row_dimensions[i].height = 28
    last = len(order) + 1
    sm.freeze_panes = "B2"
    sm.auto_filter.ref = "A1:%s%d" % (get_column_letter(len(heads)), last)
    sm.conditional_formatting.add("N2:N%d" % last, FormulaRule(formula=['LEFT($N2,6)="VERIFY"'], fill=PatternFill("solid", bgColor=AMBER, fgColor=AMBER)))
    sm.conditional_formatting.add("H2:H%d" % last, FormulaRule(formula=["$H2<0.9"], fill=PatternFill("solid", bgColor=AMBER, fgColor=AMBER)))
    sm.conditional_formatting.add("T2:T%d" % last, FormulaRule(formula=["AND(ISNUMBER($T2),ISNUMBER($U2),$U2>$T2)"], fill=PatternFill("solid", bgColor=AMBER, fgColor=AMBER)))
    sm.conditional_formatting.add("A2:U%d" % last, FormulaRule(formula=["MOD(ROW(),2)=0"], fill=PatternFill("solid", bgColor="FFF4F4F4", fgColor="FFF4F4F4")))
    _print_setup(sm, len(heads), last, 1, "AHU summary", date)

    # ---- storey sheets ---------------------------------------------------------------------------
    rheads = ["Room ID", "Room tag", "Room name", "Storey", "Department", "System / class", "Area (m²)", "Confidence", "Sources", "Remarks", "Code", "Basis"]
    rwidths = [12, 24, 26, 8, 22, 28, 10, 11, 50, 60, 6, 24]
    counts = {}
    for k in storeys:
        w = wb.create_sheet(k)
        _head(w, 1, rheads, rwidths)
        rows = rooms[k]
        grids = {r["room_id"]: (r.get("grid_location") or r.get("grid_location_label_point") or "") for r in common.read_csv_dicts(common.resolve_paths()["registers"] / ("room_polygons_%s.csv" % k))}
        for i, a in enumerate(rows, 2):
            cls = a["class"].strip() or "no information"
            g_ = grids.get(a["room_id"], "")
            note = tc.clean_note(a["notes"], cls, g_)
            if "CATH-0" in cls:
                note = (note + " " + cath_tag_note(cls)).strip()
            vals = [a["room_id"], a["room_tag"], a["room_name"], k, tc.clean_dept(a["department"], cls) or "no information", cls, (_num(a["area_m2"]) or None), _num(a["confidence"]),
                    "; ".join(tc.split_citations(a["sources"], g_)), note, "D", a.get("basis", "")]
            sty = ["Tag", "Tag", "TextWrap", "Text", "TextWrap", "Tag", "Calc", "Calc", "TextWrap", "Remark", "Code", "Tag"]
            for j, v in enumerate(vals, 1):
                c = w.cell(row=i, column=j, value=v)
                c.style = sty[j - 1]
                if sty[j - 1] in ("TextWrap", "Remark"):
                    c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True, indent=1)
                elif sty[j - 1] in ("Text", "Tag"):
                    c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
            w.cell(row=i, column=7).number_format = _FMT_D1
            w.cell(row=i, column=8).number_format = _FMT_D2
        lr = len(rows) + 1
        w.freeze_panes = "C2"
        w.auto_filter.ref = "A1:%s%d" % (get_column_letter(len(rheads)), lr)
        w.conditional_formatting.add("H2:H%d" % lr, FormulaRule(formula=["$H2<0.9"], fill=PatternFill("solid", bgColor=AMBER, fgColor=AMBER)))
        w.conditional_formatting.add("F2:F%d" % lr, FormulaRule(formula=['$F2="no information"'], font=Font(name=SANS, italic=True, color="6B7780")))
        w.conditional_formatting.add("A2:L%d" % lr, FormulaRule(formula=["MOD(ROW(),2)=0"], fill=PatternFill("solid", bgColor="FFF4F4F4", fgColor="FFF4F4F4")))
        _print_setup(w, len(rheads), lr, 1, "%s  %s" % (k, storey_info(k)["storey_label"]), date)
        counts[k] = len(rows)
    from ..workbook.tender_lookup import record_conflicts
    n_conf = record_conflicts(tender_conf)
    wb.properties.creator = "AIGH IP ACMV design team"
    wb.properties.title = "AHU zone schedule, LB3 to L07"
    out.parent.mkdir(parents=True, exist_ok=True)
    from ..workbook.clean import clean_workbook
    clean_workbook(wb)
    wb.save(str(out))
    return {"rooms_per_storey": counts, "units": len(units), "units_without_tender_airflow": n_no_tender, "tender_conflicts_listed": n_conf}


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #
def check_coverage(storeys: List[str]) -> dict:
    P = common.resolve_paths()
    res = {"ok": True, "storeys": {}}
    for k in storeys:
        poly = common.read_csv_dicts(P["registers"] / ("room_polygons_%s.csv" % k))
        asg = common.read_csv_dicts(P["registers"] / ("zone_assignment_%s.csv" % k))
        pid = [r["room_id"] for r in poly]
        aid = [r["room_id"] for r in asg]
        dup = [x for x, n in Counter(aid).items() if n > 1]
        empty = [r["room_id"] for r in asg if not r["class"].strip()]
        pairs = defaultdict(set)         # a duty and standby pair must be one zone on a storey
        for r in asg:
            zk, _ = zone_key(r["class"].strip())
            pairs[zk].add(r["class"].strip())
        low = [r for r in asg if (_num(r["confidence"]) or 0) < 0.9]
        item = {"rooms_polygon_table": len(pid), "rooms_assigned": len(aid), "not_assigned": sorted(set(pid) - set(aid)), "assigned_not_in_polygon_table": sorted(set(aid) - set(pid)),
                "duplicates": dup, "empty_class": empty, "rooms_below_0.9": len(low), "area_below_0.9_m2": round(sum(_num(r["area_m2"]) or 0 for r in low), 1),
                "classes": len({r["class"] for r in asg})}
        item["ok"] = not (item["not_assigned"] or item["assigned_not_in_polygon_table"] or dup or empty)
        res["storeys"][k] = item
        res["ok"] &= item["ok"]
    res["rooms_total"] = sum(v["rooms_assigned"] for v in res["storeys"].values())
    return res


def undrawn_tags(k: str) -> set:
    """Display tags of the units of a storey whose rooms all have no outline on the plan: the PDF lists them as not drawn."""
    P = common.resolve_paths()
    polys = {r["room_id"] for r in common.read_csv_dicts(P["registers"] / ("room_polygons_%s.csv" % k)) if r["polygon_pv_wkt"]}
    drawn, allk = set(), defaultdict(set)
    for r in common.read_csv_dicts(P["registers"] / ("zone_assignment_%s.csv" % k)):
        c = r["class"].strip()
        if not c or c == "no information" or c.lower().startswith("review"):
            continue
        zk, _ = zone_key(c)
        allk[zk].add(c)
        if r["room_id"] in polys:
            drawn.add(zk)
    return {display_tag(allk[z]) for z in allk if z not in drawn}


def check_pdf(storeys: List[str]) -> dict:
    import pymupdf
    pdf = out_pdf_path(storeys)
    doc = pymupdf.open(str(pdf))
    P = common.resolve_paths()
    res: dict = {"pdf": str(pdf), "pages": len(doc), "problems": []}
    prob = res["problems"]
    cat = doc.pdf_catalog()
    ocp = doc.xref_get_key(cat, "OCProperties")
    off_names = []
    rooms_xref = next((x for x, g in doc.get_ocgs().items() if g["name"] == "Rooms (measured)"), -1)
    d_off = doc.xref_get_key(cat, "OCProperties/D/OFF")[1]
    res["rooms_layer_off"] = ("%d 0 R" % rooms_xref) in d_off and not doc.get_ocgs()[rooms_xref]["on"]
    if not res["rooms_layer_off"]:
        prob.append("Rooms (measured) layer is not off")
    ocg_names = {x: g["name"] for x, g in doc.get_ocgs().items()}
    fills: Dict[str, set] = defaultdict(set)
    zone_by_page: Dict[int, set] = {}
    n_zone = 0
    red = []
    for pn in range(len(doc)):
        page = doc[pn]
        zs, ls = set(), set()
        for a in page.annots() or []:
            nm = a.info.get("id", "")
            subj = a.info.get("subject", "")
            if nm.startswith(("zone-", "legend-swatch")):
                ic = doc.xref_get_key(a.xref, "IC")[1]
                ca = doc.xref_get_key(a.xref, "CA")[1]
                fo = doc.xref_get_key(a.xref, "FillOpacity")[1] if "FillOpacity" in doc.xref_object(a.xref) else ""
                oc = doc.xref_get_key(a.xref, "OC")[1]
                if not ic or ic == "null" or not subj or not oc or oc == "null" or abs(float(ca) - 0.35) > 1e-6 or (fo and abs(float(fo) - 0.35) > 1e-6):
                    prob.append("p%d %s incomplete (IC %s, CA %s, FillOpacity %s, subject %r, OC %s)" % (pn + 1, nm, ic, ca, fo, subj, oc))
                col = a.colors.get("fill") or ()
                if col and len(col) == 3:
                    fills[subj].add(tuple(round(v, 3) for v in col))
                    if col[0] > 0.9 and col[1] < 0.15 and col[2] < 0.15:
                        red.append((pn + 1, nm, col))
                # layer name equals the subject
                ocname = ocg_names.get(int(oc.split()[0]), "") if oc.endswith("R") else ""
                if nm.startswith("zone-") and ocname != subj:
                    prob.append("p%d %s layer %r differs from subject %r" % (pn + 1, nm, ocname, subj))
                (zs if nm.startswith("zone-") else ls).add(subj)
                if nm.startswith("zone-"):
                    n_zone += 1
        if zs or ls:
            zone_by_page[pn + 1] = zs
            if zs != ls:
                prob.append("p%d legend and zones differ: zones only %s; legend only %s" % (pn + 1, sorted(zs - ls), sorted(ls - zs)))
    res["zone_annotations"] = n_zone
    res["colour_conflicts"] = {k: sorted(v) for k, v in fills.items() if len(v) > 1}
    res["pure_red_fills"] = red
    if res["colour_conflicts"]:
        prob.append("colour differs between pages for %d zones" % len(res["colour_conflicts"]))
    if red:
        prob.append("pure red fill found: %s" % red[:3])
    # index: each zone once per storey served
    idx_pages = [p for p in range(len(doc)) if doc[p].rect.width < 900 and doc[p].rect.height < 1300]       # index pages are A3 portrait; the plans are far larger
    text = "\n".join(doc[p].get_text() for p in idx_pages)
    cur = None
    idx: Dict[str, List[str]] = defaultdict(list)
    keys = set(storeys)
    page_of = {}
    for pn in idx_pages:                  # a storey heading is one span; a zone tag is a span in the monospaced face
        for blk in doc[pn].get_text("dict")["blocks"]:
            for ln in blk.get("lines", []):
                for sp in ln["spans"]:
                    t = sp["text"].strip()
                    m = re.match(r"^(\w+)\s{2}.*PDF page (\d+)$", t)
                    if m and m.group(1) in keys:
                        cur = m.group(1)
                        page_of[cur] = int(m.group(2))
                    elif "Mono" in sp["font"] and cur and t:
                        idx[cur].append(t)
    res["index_pages"] = [p + 1 for p in idx_pages]
    ok_idx = True
    for k in storeys:
        got = idx.get(k, [])
        want = zone_by_page.get(page_of.get(k, -1), set()) | (undrawn_tags(k) & set(got))
        if sorted(got) != sorted(set(got)):
            prob.append("index %s lists a zone more than once: %s" % (k, [x for x, n in Counter(got).items() if n > 1]))
            ok_idx = False
        if set(got) != want:
            prob.append("index %s differs from the zones on its page: index only %s; page only %s" % (k, sorted(set(got) - want), sorted(want - set(got))))
            ok_idx = False
    res["index_ok"] = ok_idx
    res["index_zone_rows"] = {k: len(idx.get(k, [])) for k in storeys}
    # zones agree with the assignment tables
    for k in storeys:
        asg = common.read_csv_dicts(P["registers"] / ("zone_assignment_%s.csv" % k))
        want = set()
        grp = defaultdict(set)
        for r in asg:
            c = r["class"].strip()
            if c and c != "no information" and not c.lower().startswith("review"):
                grp[zone_key(c)[0]].add(c)
        disp_of = {zk: display_tag(v) for zk, v in grp.items()}
        want = set(disp_of.values())
        got = set(idx.get(k, []))
        undrawn_expected = undrawn_tags(k)
        res.setdefault("not_drawn_units_listed_in_index", {})[k] = sorted(got & undrawn_expected)
        got = got - undrawn_expected
        # rooms without a polygon do not draw a zone; a zone may be missing only when every room of the class has no polygon
        missing = want - got
        if missing:
            polys = {r["room_id"] for r in common.read_csv_dicts(P["registers"] / ("room_polygons_%s.csv" % k)) if r["polygon_pv_wkt"]}
            only_nopoly = [m for m in missing if not any(r["room_id"] in polys and disp_of.get(zone_key(r["class"].strip())[0]) == m for r in asg)]
            real = sorted(set(missing) - set(only_nopoly))
            if real:
                prob.append("%s: classes in the assignment without a zone: %s" % (k, real))
            res.setdefault("classes_without_drawn_zone_no_polygon", {})[k] = sorted(only_nopoly)
        extra = got - want
        if extra:
            prob.append("%s: zones not in the assignment: %s" % (k, sorted(extra)))
    # forbidden words in annotation and index text
    bad = re.compile(r"\b(stub|pipeline|method [AB]|register|Claude|python|script|CF-\d{4}|D-0\d\d|csv|workflow|agent|council|gate)\b", re.I)
    hits = []
    for pn in range(len(doc)):
        page = doc[pn]
        if pn in idx_pages:
            hits += [(pn + 1, m.group(0)) for m in bad.finditer(page.get_text())]
        for a in page.annots() or []:
            t = (a.info.get("content", "") + " " + a.info.get("title", "") + " " + a.info.get("subject", ""))
            hits += [(pn + 1, m.group(0)) for m in bad.finditer(t)]
    res["internal_terms_found"] = hits[:20]
    if hits:
        prob.append("internal words in the PDF text: %s" % hits[:5])
    # background pixel check from the stage result
    qa = P["qa"] / ("zoning_storeys_%s-%s.json" % (storeys[0], storeys[-1]))
    if qa.exists():
        v = json.loads(qa.read_text()).get("pdf_verify", {})
        pages = v.get("pages", [])
        res["stage_check_ok"] = v.get("ok")
        res["background_pixel_identical_all_pages"] = all(p.get("pixels_identical_annots_hidden") for p in pages) and len(pages) == len(storeys)
        res["content_stream_identical_all_pages"] = all(p.get("content_stream_identical") for p in pages)
        res["stage_problems"] = v.get("problems", [])
        if not res["background_pixel_identical_all_pages"]:
            prob.append("background is not pixel identical on every page")
    res["ok"] = not prob and bool(res.get("stage_check_ok"))
    return res


def check_fill(schedule: Path = None, master: Path = None) -> dict:
    """The Calculated airflow of the AHU summary is filled for every tag that the master summary holds with a room supply, and the Sources sheet exists.
    Fails when the filled count is 0 or differs from the number of tags common to both files that have a calculated airflow in the master summary."""
    import openpyxl
    P = common.resolve_paths()
    schedule = schedule or P["zoning"] / "zone_schedule.xlsx"
    master = master or Path("out/loads/workbooks/AHU_master_summary.xlsx")
    res: dict = {"ok": False, "problems": []}
    if not master.exists():
        res["problems"].append("the master summary does not exist: the calculated airflow cannot be filled")
        return res
    mw = openpyxl.load_workbook(master, data_only=True)["AHU Master"]
    mh = {str(c.value): i for i, c in enumerate(mw[5])}
    m_calc = {str(r[0]): r[mh["Calculated airflow"]] for r in mw.iter_rows(min_row=7, values_only=True) if r[0] and not str(r[0]).startswith("TOTAL")}
    wb = openpyxl.load_workbook(schedule, data_only=True)
    sm = wb["AHU summary"]
    heads = [c.value for c in sm[1]]
    k = heads.index("Calculated airflow (L/s)")
    rows = [r for r in sm.iter_rows(min_row=2, values_only=True) if r[0]]
    common_tags = {str(r[0]) for r in rows if str(r[0]) in m_calc and isinstance(m_calc[str(r[0])], (int, float)) and m_calc[str(r[0])] > 0}
    filled = {str(r[0]) for r in rows if r[k] is not None}
    res.update(units=len(rows), filled=len(filled), expected=len(common_tags), sources_sheet="Sources" in wb.sheetnames)
    if not filled:
        res["problems"].append("the Calculated airflow is filled for 0 units")
    if filled != common_tags and filled < common_tags:
        res["problems"].append("units with a calculated airflow in the master summary and none in the schedule: " + ", ".join(sorted(common_tags - filled)[:6]))
    if "Sources" not in wb.sheetnames:
        res["problems"].append("the Sources sheet is missing")
    ti = heads.index("Tender airflow (L/s)")
    m_ten = {str(r[0]): r[mh["Tender airflow"]] for r in mw.iter_rows(min_row=7, values_only=True) if r[0] and not str(r[0]).startswith("TOTAL")}
    diff = [str(r[0]) for r in rows if str(r[0]) in m_ten and (r[ti] if r[ti] is not None else None) != (m_ten[str(r[0])] if m_ten[str(r[0])] is not None else None)]
    res["tender_differences"] = diff
    if diff:
        res["problems"].append(f"the tender airflow differs between the master summary and the schedule for {len(diff)} unit(s): " + ", ".join(diff[:6]))
    res["ok"] = not res["problems"]
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("what", choices=["schedule", "check", "all"])
    ap.add_argument("--storeys", default=",".join(ALL_STOREYS))
    a = ap.parse_args(argv)
    storeys = a.storeys.split(",")
    P = common.resolve_paths()
    if a.what in ("schedule", "all"):
        r = build_xlsx(storeys, P["zoning"] / "zone_schedule.xlsx")
        print(json.dumps(r))
        from .fill_calculated import main as fill_main        # the fill is the last step of the schedule: a rebuilt schedule is never left empty
        fill_main([])
    if a.what in ("check", "all"):
        r = {"coverage": check_coverage(storeys), "pdf": check_pdf(storeys), "fill": check_fill()}
        (P["qa"] / "zoning_deliverable_checks.json").write_text(json.dumps(r, indent=1, default=str))
        print(json.dumps({"coverage_ok": r["coverage"]["ok"], "pdf_ok": r["pdf"]["ok"], "fill_ok": r["fill"]["ok"], "problems": r["pdf"]["problems"][:20] + r["fill"]["problems"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
