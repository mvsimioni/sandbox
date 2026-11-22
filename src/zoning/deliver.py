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
from .storeys import ALL_STOREYS, AHU_RE, out_pdf_path, schedule_plant_level, storey_info, zone_key

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


class Tender:
    """Tender airflow of a unit: Equipment Schedule (Corr 5) first, then the tender air-side schematic; blank (no information) otherwise."""

    def __init__(self) -> None:
        P = common.resolve_paths()
        self.sched: Dict[str, dict] = {}
        for r in common.read_csv_dicts(P["registers"] / "ahu_schedule.csv"):
            v = _num(r.get("supply_airflow_l_s [Supply Airflow (l/s)]", ""))
            if v is not None:
                self.sched.setdefault(r["tag_normalised"].strip(), {"v": v, "sheet": r["sheet_code"], "pages": r["page(s)"], "title": r["schedule_title"]})
        for r in common.read_csv_dicts(P["registers"] / "equipment_schedule_air_handling.csv"):     # fan coil units: same schedule document
            if r.get("type") != "FCU":
                continue
            v = _num(r.get("Min Airflow (l/s)", ""))
            if v is not None:
                self.sched.setdefault(r["tag_normalised"].strip(), {"v": v, "sheet": r["sheet_code"], "pages": r["page(s)"], "title": r["schedule_title"]})
        self.schem: Dict[str, List[dict]] = defaultdict(list)
        for r in common.read_csv_dicts(P["registers"] / "schematic_ahu_totals.csv"):
            if r["issue_set"] != "tender" or r["scope"] != "sheet":
                continue
            v = _num(r["ahu_supply_L_s"])
            if v is not None:
                self.schem[r["ahu_tag"].strip()].append({"v": v, "sheet": r["sheet"], "issue": r["issue"]})

    @staticmethod
    def candidates(tags: List[str]) -> List[str]:
        out: List[str] = []
        for t in tags:
            base = t[:-3] if t.endswith("A/B") else (t[:-1] if t[-1:] in "AB" and AHU_RE.match(t) else None)
            out.append(t)
            if base is not None:
                out += [base + "A", base + "B", base]
        return list(dict.fromkeys(out))

    def find(self, tags: List[str]) -> Tuple[Optional[float], str]:
        cands = self.candidates(tags)
        hits = [(c, self.sched[c]) for c in cands if c in self.sched]
        if hits:
            vals = {h[1]["v"] for h in hits}
            if len(vals) == 1:
                h = hits[0][1]
                return h["v"], "Equipment Schedule SCH/1 (Corr 5), %s, page %s" % (h["sheet"], h["pages"])
            return None, "No information: the duty and standby units show different airflows in Equipment Schedule (Corr 5) (%s L/s)" % ", ".join("%s %g" % (c, h["v"]) for c, h in hits)
        hits2 = [(c, x) for c in cands for x in self.schem.get(c, [])]
        if hits2:
            vals = {h[1]["v"] for h in hits2}
            if len(vals) == 1:
                h = hits2[0][1]
                return h["v"], "Tender air-side schematic %s (%s)" % (h["sheet"], h["issue"])
            return None, "No information: tender air-side schematics show different airflows (%s L/s)" % ", ".join(sorted("%g" % v for v in vals))
        return None, "No information: the unit is not in Equipment Schedule (Corr 5) or in the tender air-side schematics"


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
                u["depts"].add(tc.clean_dept(r["department"]))
            if k not in u["storeys"]:
                u["storeys"].append(k)
            u["n"] += 1
            u["area"] += _num(r["area_m2"]) or 0.0
            u["conf"] = min(u["conf"], _num(r["confidence"]) or 0.0)
    for zk, u in units.items():
        t = sorted(u["tags"])
        u["tag"] = zone_key(t[0])[1] if (len(t) > 1 or AHU_RE.match(t[0])) else t[0]
    return units


def plant_level(tag: str) -> str:
    return schedule_plant_level(tag) or "no information (tag does not give it and the unit is not scheduled)"


# --------------------------------------------------------------------------- #
# workbook
# --------------------------------------------------------------------------- #
def _font(bold=False, mono=False, size=9, colour=INK, italic=False) -> Font:
    return Font(name=MONO if mono else SANS, size=size, bold=bold, color=colour, italic=italic)


def _head(ws, row: int, labels: List[str], widths: List[float]) -> None:
    thin = Side(style="thin", color=RULE)
    for i, (lab, w) in enumerate(zip(labels, widths), 1):
        c = ws.cell(row=row, column=i, value=lab)
        c.font = Font(name=SANS, size=9, bold=True, color=PAPER)
        c.fill = PatternFill("solid", fgColor=INK)
        c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
        c.border = Border(bottom=thin)
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.row_dimensions[row].height = 30


def _print_setup(ws, last_col: int, last_row: int, title_row: int = 1, head: str = "") -> None:
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A3
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.print_title_rows = "%d:%d" % (title_row, title_row)
    ws.print_area = "A1:%s%d" % (get_column_letter(last_col), max(last_row, 2))
    ws.page_margins.left = ws.page_margins.right = 0.4
    ws.page_margins.top = 0.6
    ws.page_margins.bottom = 0.6
    ws.oddHeader.left.text = "AIGH IP, AHU zone schedule"
    ws.oddHeader.left.font = "Arial,Bold"
    ws.oddHeader.left.size = 9
    ws.oddHeader.right.text = head or ws.title
    ws.oddHeader.right.size = 9
    ws.oddFooter.center.text = "Page &P of &N"
    ws.oddFooter.center.size = 8
    ws.sheet_view.showGridLines = False


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
    ("AHU summary", "h"),
    ("Departments, Storeys served and Plant level come from the room sheets and from the unit tag.", ""),
    ("Tender airflow is the supply airflow in L/s. The first source is Equipment Schedule SCH/1 (Corr 5). If the schedule has no figure, the source is the tender air-side schematic. The next column names the source.", ""),
    ("If neither source has a figure, the cell is blank and the source column gives the reason.", ""),
    ("Enter the calculated airflow in L/s in the Calculated airflow column. Cells with a blue font are inputs.", ""),
    ("Reduction (L/s) = Tender airflow minus Calculated airflow. Reduction (%) = Reduction divided by Tender airflow. Both are formulas and stay blank until you enter the calculated airflow.", ""),
    ("The Remarks cell is a formula. It states the tender airflow, the calculated airflow and the change. If the calculated airflow is more than the tender airflow, the Remarks cell starts with VERIFY.", ""),
    ("Schematic and schedule airflows are for comparison only. They do not set the zone.", ""),
    ("Documents", "h"),
    ("Room tags and mode labels: Arch Combined Tender, sheets AR-IP-01-LB3-001 to AR-IP-01-07-001, issue as shown in the Sources column.", ""),
    ("AHU tags and served rooms: air-side department schematics, Equipment Schedule SCH/1 (Corr 5), Appendix D ACMV for Common Areas, and the DD Booklets, as cited for each room.", ""),
    ("Diagnostic Imaging on L04: RFI-ME-0233 governs. Other departments: the later dated document governs and the difference is shown in the Remarks column.", ""),
]


def build_xlsx(storeys: List[str], out: Path) -> dict:
    rooms = load_rooms(storeys)
    units = build_units(rooms)
    tender = Tender()
    wb = Workbook()
    ws = wb.active
    ws.title = "README"
    ws.column_dimensions["A"].width = 120
    r = 1
    for text, kind in README:
        c = ws.cell(row=r, column=1, value=text)
        c.alignment = Alignment(wrap_text=True, vertical="top")
        if kind == "t":
            c.font = Font(name=SANS, size=16, bold=True, color=INK)
            ws.row_dimensions[r].height = 28
        elif kind == "h":
            r += 1
            c = ws.cell(row=r, column=1, value=text)
            c.font = Font(name=SANS, size=11, bold=True, color=INK)
            c.border = Border(bottom=Side(style="thin", color=RULE))
        else:
            c.font = _font(size=10)
        r += 1
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A3
    ws.page_setup.fitToHeight = 0
    ws.sheet_view.showGridLines = False
    ws.freeze_panes = "A2"

    # ---- AHU summary -----------------------------------------------------------------------------
    sm = wb.create_sheet("AHU summary")
    heads = ["AHU or FCU tag", "Type", "Departments", "Storeys served", "Plant level", "Rooms", "Area (m2)", "Lowest room confidence",
             "Tender airflow (L/s)", "Tender airflow source", "Calculated airflow (L/s)", "Reduction (L/s)", "Reduction (%)", "Remarks"]
    widths = [30, 7, 38, 14, 22, 8, 11, 11, 12, 46, 13, 12, 11, 70]
    _head(sm, 1, heads, widths)
    n_no_tender = 0
    order = sorted(units, key=lambda z: (min(ALL_STOREYS.index(s) for s in units[z]["storeys"]), z))
    for i, zk in enumerate(order, 2):
        u = units[zk]
        tags = sorted(u["tags"])
        val, src = tender.find(tags)
        if val is None:
            n_no_tender += 1
        typ = re.match(r"^([A-Z]+)", tags[0]).group(1)
        row = [u["tag"], typ, "; ".join(sorted(u["depts"])) or "no information", ", ".join(u["storeys"]), plant_level(tags[0]), u["n"], round(u["area"], 1), u["conf"],
               val, src, None,
               '=IF(OR(I%d="",K%d=""),"",I%d-K%d)' % (i, i, i, i),
               '=IF(OR(I%d="",K%d="",I%d=0),"",(I%d-K%d)/I%d)' % (i, i, i, i, i, i),
               ('=IF(I{r}="",J{r}&".",IF(K{r}="","Tender "&TEXT(I{r},"#,##0")&" L/s ("&J{r}&"). Calculated airflow not yet entered.",'
                'IF(K{r}>I{r},"VERIFY: increase from "&TEXT(I{r},"#,##0")&" to "&TEXT(K{r},"#,##0")&" L/s (+"&TEXT((K{r}-I{r})/I{r}*100,"0.0")&" %) against tender ("&J{r}&").",'
                '"Reduced "&TEXT(I{r},"#,##0")&" -> "&TEXT(K{r},"#,##0")&" L/s (-"&TEXT((I{r}-K{r})/I{r}*100,"0.0")&" %) against tender ("&J{r}&").")))').format(r=i)]
        for j, v in enumerate(row, 1):
            c = sm.cell(row=i, column=j, value=v)
            c.font = _font(mono=(j == 1))
            c.alignment = Alignment(vertical="top", wrap_text=j in (3, 10, 14))
        sm.cell(row=i, column=9).number_format = "#,##0"
        sm.cell(row=i, column=11).number_format = "#,##0"
        sm.cell(row=i, column=11).font = Font(name=SANS, size=9, color="1F4FBF")
        sm.cell(row=i, column=12).number_format = "#,##0;-#,##0"
        sm.cell(row=i, column=13).number_format = "0.0%"
        sm.cell(row=i, column=7).number_format = "#,##0.0"
        sm.cell(row=i, column=8).number_format = "0.00"
        if i % 2:
            for j in range(1, len(heads) + 1):
                sm.cell(row=i, column=j).fill = PatternFill("solid", fgColor=BAND)
    last = len(order) + 1
    sm.freeze_panes = "B2"
    sm.auto_filter.ref = "A1:%s%d" % (get_column_letter(len(heads)), last)
    sm.conditional_formatting.add("N2:N%d" % last, FormulaRule(formula=['LEFT($N2,6)="VERIFY"'], fill=PatternFill("solid", bgColor=AMBER, fgColor=AMBER)))
    sm.conditional_formatting.add("H2:H%d" % last, FormulaRule(formula=["$H2<0.9"], fill=PatternFill("solid", bgColor=AMBER, fgColor=AMBER)))
    _print_setup(sm, len(heads), last, 1, "AHU summary")

    # ---- storey sheets ---------------------------------------------------------------------------
    rheads = ["Room ID", "Room tag", "Room name", "Storey", "Department", "System / class", "Area (m2)", "Confidence", "Sources", "Remarks"]
    rwidths = [11, 24, 28, 7, 28, 30, 10, 11, 70, 80]
    counts = {}
    for k in storeys:
        w = wb.create_sheet(k)
        _head(w, 1, rheads, rwidths)
        rows = rooms[k]
        for i, a in enumerate(rows, 2):
            cls = a["class"].strip() or "no information"
            vals = [a["room_id"], a["room_tag"], a["room_name"], k, tc.clean_dept(a["department"]) or "no information", cls, _num(a["area_m2"]), _num(a["confidence"]),
                    "; ".join(tc.split_citations(a["sources"])), tc.clean_note(a["notes"])]
            for j, v in enumerate(vals, 1):
                c = w.cell(row=i, column=j, value=v)
                c.font = _font(mono=j in (1, 6))
                c.alignment = Alignment(vertical="top", wrap_text=j in (2, 3, 5, 9, 10))
            w.cell(row=i, column=7).number_format = "#,##0.0"
            w.cell(row=i, column=8).number_format = "0.00"
            if i % 2:
                for j in range(1, len(rheads) + 1):
                    w.cell(row=i, column=j).fill = PatternFill("solid", fgColor=BAND)
        lr = len(rows) + 1
        w.freeze_panes = "C2"
        w.auto_filter.ref = "A1:%s%d" % (get_column_letter(len(rheads)), lr)
        w.conditional_formatting.add("H2:H%d" % lr, FormulaRule(formula=["$H2<0.9"], fill=PatternFill("solid", bgColor=AMBER, fgColor=AMBER)))
        w.conditional_formatting.add("F2:F%d" % lr, FormulaRule(formula=['$F2="no information"'], font=Font(name=SANS, italic=True, color="6B7780")))
        _print_setup(w, len(rheads), lr, 1, "%s  %s" % (k, storey_info(k)["storey_label"]))
        counts[k] = len(rows)
    wb.properties.creator = "AIGH IP ACMV design team"
    wb.properties.title = "AHU zone schedule, LB3 to L07"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out))
    return {"rooms_per_storey": counts, "units": len(units), "units_without_tender_airflow": n_no_tender}


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
    for line in text.splitlines():
        m = re.match(r"^(\w+)\s{2}.*PDF page (\d+)$", line.strip())
        if m and m.group(1) in keys:
            cur = m.group(1)
            continue
        m = re.match(r"^(\S+)\s+(AHU|FCU|PAHU|MV|NV|CRAC)\s+rooms\s+\d+", line.strip())
        if m and cur:
            idx[cur].append(m.group(1))
    page_of = {}
    for line in text.splitlines():
        m = re.match(r"^(\w+)\s{2}.*PDF page (\d+)$", line.strip())
        if m and m.group(1) in keys:
            page_of[m.group(1)] = int(m.group(2))
    res["index_pages"] = [p + 1 for p in idx_pages]
    ok_idx = True
    for k in storeys:
        got = idx.get(k, [])
        want = zone_by_page.get(page_of.get(k, -1), set())
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
        for r in asg:
            c = r["class"].strip()
            if c and c != "no information" and not c.lower().startswith("review"):
                want.add(zone_key(c)[1] if AHU_RE.match(c) else c)
        got = set(idx.get(k, []))
        # rooms without a polygon do not draw a zone; a zone may be missing only when every room of the class has no polygon
        missing = want - got
        if missing:
            polys = {r["room_id"] for r in common.read_csv_dicts(P["registers"] / ("room_polygons_%s.csv" % k)) if r["polygon_pv_wkt"]}
            only_nopoly = [m for m in missing if not any(r["room_id"] in polys and (zone_key(r["class"].strip())[1] if AHU_RE.match(r["class"].strip()) else r["class"].strip()) == m for r in asg)]
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
    if a.what in ("check", "all"):
        r = {"coverage": check_coverage(storeys), "pdf": check_pdf(storeys)}
        (P["qa"] / "zoning_deliverable_checks.json").write_text(json.dumps(r, indent=1, default=str))
        print(json.dumps({"coverage_ok": r["coverage"]["ok"], "pdf_ok": r["pdf"]["ok"], "problems": r["pdf"]["problems"][:20]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
