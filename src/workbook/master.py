"""AHU master summary: every AHU counted once across the departments, with the tender columns of D-013.

Input: a directory with department workbooks (.xlsx, read from their AHU tab: recalculated values are used, and a workbook with no stored
values is recalculated first) and/or department summaries (.json, format below). Output: one workbook in which the room supply of each AHU
is the sum of the department contributions (live SUMIFS), the duct leakage is one named cell, and the tender airflow is entered once.

JSON summary of one department::

    {"department": {"number": "30", "name": "Pharmacy"},
     "ahus": [{"tag": "AHU-...", "type": "AHU", "rooms": 12, "area_m2": 300.5, "room_supply_lps": 1450.0, "outdoor_air_lps": 300.0,
               "tender_lps": 3500.0, "tender_source": "Equipment Schedule Corr 5, page 13, AHU-...", "tender_source_id": "EQS5-..."}]}
"""

from __future__ import annotations

import datetime
import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import openpyxl
from openpyxl import Workbook
from openpyxl.styles import Alignment
from openpyxl.workbook.defined_name import DefinedName

from . import conf
from .common import cf_formula, tender_remark
from .layouts import AHU_KEYS
from .styles import install_styles, link_to, put, setup_sheet

FIRST = 7


def read_department_workbook(path: Path) -> Dict[str, Any]:
    """Department record from the AHU tab of a department workbook."""
    wb = openpyxl.load_workbook(path, data_only=True)
    ah = wb["AHU"]
    idx = {k: i for i, k in enumerate(AHU_KEYS)}
    probe = [r for r in ah.iter_rows(min_row=FIRST, max_row=FIRST + 1, values_only=True)]
    if probe and probe[0][idx["ahu"]] and probe[0][idx["sa_room"]] is None:       # no stored values: recalculate
        from .recalc import recalc
        with tempfile.TemporaryDirectory(prefix="wb_master_") as tmp:
            wb = openpyxl.load_workbook(recalc(path, Path(tmp)), data_only=True)
            ah = wb["AHU"]
    title = str(wb["README"]["B1"].value or "")
    num, _, name = title.partition("  ")
    out: List[Dict[str, Any]] = []
    for row in ah.iter_rows(min_row=FIRST, values_only=True):
        tag = row[idx["ahu"]]
        if not tag or tag == "TOTAL":
            break
        out.append(dict(tag=str(tag), type=row[idx["type"]], rooms=row[idx["n"]] or 0, area_m2=row[idx["area"]] or 0.0, rooms_no_height=row[idx["noh"]] or 0,
                        room_supply_lps=row[idx["sa_room"]] or 0.0, outdoor_air_lps=row[idx["oa_sum"]] or 0.0,
                        tender_lps=row[idx["sched"]], tender_source=row[idx["tsrc"]], tender_source_id=row[idx["sched_s"]]))
    return dict(department=dict(number=num.strip(), name=name.strip()), ahus=out, file=path.name)


def read_inputs(inputs: Path) -> List[Dict[str, Any]]:
    recs: List[Dict[str, Any]] = []
    paths = [inputs] if inputs.is_file() else sorted(inputs.iterdir())
    for p in paths:
        if p.name.startswith("~") or p.name.startswith("."):
            continue
        if p.suffix.lower() == ".xlsx" and "Master" not in p.name and "master" not in p.name:
            recs.append(read_department_workbook(p))
        elif p.suffix.lower() == ".json":
            d = json.loads(p.read_text(encoding="utf-8"))
            if "ahus" in d and "department" in d:
                d.setdefault("file", f"Department summary, {d['department'].get('number', '')} {d['department'].get('name', '')}".strip())
                recs.append(d)
    if not recs:
        raise ValueError(f"no department workbook or summary found in {inputs}")
    return recs


def build_master(inputs: Path, out: Path, root: Path = Path(".")) -> Dict[str, Any]:
    recs = read_inputs(Path(inputs))
    cfg = conf.config(Path(root))
    leak = float(((cfg.get("defaults_to_confirm") or {}).get("duct_leakage_pct", 10))) / 100.0
    date = conf.revision_date(Path(root))
    project = str((cfg.get("project") or {}).get("name") or "Project")
    wb = Workbook()
    wb.remove(wb.active)
    install_styles(wb)
    for n in ("README", "Basis", "Department Rows", "AHU Master", "Sources", "Flags"):
        wb.create_sheet(n)
    rd, ba, dr, am, so, fl = (wb[n] for n in ("README", "Basis", "Department Rows", "AHU Master", "Sources", "Flags"))

    tenders: Dict[str, Dict[str, Any]] = {}
    flags: List[Dict[str, str]] = []
    types: Dict[str, str] = {}
    for r in recs:
        dn = r["department"]["number"]
        for a in r["ahus"]:
            types.setdefault(a["tag"], a.get("type") or "AHU")
            t = a.get("tender_lps")
            if t not in (None, ""):
                prev = tenders.get(a["tag"])
                if prev is None:
                    tenders[a["tag"]] = dict(lps=float(t), text=a.get("tender_source") or "", sid=a.get("tender_source_id") or "", dept=dn)
                elif abs(prev["lps"] - float(t)) > 1e-6:
                    flags.append(dict(type="VERIFY", ref=a["tag"], text=f"AHU {a['tag']}: department {prev['dept']} gives tender airflow {prev['lps']:g} L/s and "
                                      f"department {dn} gives {float(t):g} L/s. The first value is used."))

    # Basis: duct leakage as one named cell
    put(ba, "A1", "Basis", "Title")
    put(ba, "A2", "Constants of the master summary. Every constant is a named cell.", "Note")
    for c, h in zip("ABCDEF", ("Constant", "Value", "Unit (name)", "Code", "Source ID", "Remark")):
        put(ba, f"{c}4", h, "HeaderLeft")
    put(ba, "A5", "Duct leakage allowance on AHU supply airflow", "Text")
    put(ba, "B5", leak, "Specified", "0.0%")
    put(ba, "C5", "fraction  (DuctLeakage)", "Text")
    put(ba, "D5", "S", "Code")
    put(ba, "E5", "GEN-BASIS", "Tag")
    link_to(ba["E5"], "Sources", "A7")
    put(ba, "F5", "10 % default confirmed for the project; the tender schematics state 15 %", "Note")
    wb.defined_names["DuctLeakage"] = DefinedName("DuctLeakage", attr_text="'Basis'!$B$5")
    for col, w in zip("ABCDEF", (46, 12, 24, 6, 14, 60)):
        ba.column_dimensions[col].width = w

    # Sources: one row per department workbook (+ the constant source)
    heads = ("Source ID", "Document", "Issue", "Page or sheet", "Location", "Text as read")
    for c, h in zip("ABCDEF", heads):
        put(so, f"{c}6", h, "HeaderLeft")
    put(so, "A1", "Sources", "Title")
    put(so, "A2", "One row per source ID.", "Note")
    srcs = [("GEN-BASIS", "Project design basis: default values", "22 Nov 2026", "duct leakage", "10 %", "duct leakage 10 %")]
    for r in recs:
        dn = r["department"]["number"]
        srcs.append((f"DEPT-{dn}", r.get("file", f"department {dn}"), f"{date}", "AHU tab, table A", f"department {dn} {r['department']['name']}",
                     f"{len(r['ahus'])} AHU row(s)"))
    for tag_, te_ in tenders.items():
        sid_ = te_["sid"] or f"DEPT-{te_['dept']}"
        if sid_ not in {x[0] for x in srcs}:
            srcs.append((sid_, "Tender airflow as carried by the department workbook", "Corr 5 or tender schematic", te_["text"] or tag_,
                         f"department {te_['dept']}", f"tender airflow {te_['lps']:g} L/s"))
    for i, row in enumerate(srcs):
        for c, v in zip("ABCDEF", row):
            put(so, f"{c}{FIRST + i}", v, "Tag" if c == "A" else "TextWrap")
    for col, w in zip("ABCDEF", (14, 44, 14, 22, 36, 30)):
        so.column_dimensions[col].width = w
    src_row = {row[0]: FIRST + i for i, row in enumerate(srcs)}

    # Department Rows: one row per AHU and department
    put(dr, "A1", "Department Rows", "Title")
    put(dr, "A2", "Contribution of each department to each AHU, as read from the department workbook. Do not add these to the tender airflow.", "Note")
    dh = [("ahu", "AHU or system", "", 26), ("dept", "Department", "", 10), ("name", "Department name", "", 28), ("n", "Rooms", "no.", 8),
          ("area", "Area", "m²", 10), ("supply", "Room supply", "L/s", 11), ("oa", "Sum of room outdoor air", "L/s", 11),
          ("nh", "Rooms with no ceiling height", "no.", 11), ("code", "Code", "", 5), ("src", "Source ID", "", 12)]
    for i, (k, h, u, w) in enumerate(dh):
        col = chr(65 + i)
        put(dr, f"{col}5", h, "Header")
        put(dr, f"{col}6", u or None, "Unit")
        dr.column_dimensions[col].width = w
    row = FIRST
    for r in recs:
        dn, nm = r["department"]["number"], r["department"]["name"]
        for a in r["ahus"]:
            put(dr, f"A{row}", a["tag"], "InputTag")
            put(dr, f"B{row}", dn, "InputTag")
            put(dr, f"C{row}", nm, "Input").alignment = Alignment(horizontal="left")
            put(dr, f"D{row}", a.get("rooms") or 0, "Input", "d0")
            put(dr, f"E{row}", a.get("area_m2") or 0, "Input", "d1")
            put(dr, f"F{row}", a.get("room_supply_lps") or 0, "Input", "d1")
            put(dr, f"G{row}", a.get("outdoor_air_lps") or 0, "Input", "d1")
            put(dr, f"H{row}", a.get("rooms_no_height") or 0, "Input", "d0")
            put(dr, f"I{row}", "D", "Code")
            put(dr, f"J{row}", f"DEPT-{dn}", "Tag")
            link_to(dr[f"J{row}"], "Sources", f"A{src_row[f'DEPT-{dn}']}")
            row += 1
    dr_last = max(row - 1, FIRST)
    rng = lambda c: f"'Department Rows'!${c}${FIRST}:${c}${dr_last}"
    dr.freeze_panes = f"B{FIRST}"

    # AHU Master
    tags = list(dict.fromkeys(a["tag"] for r in recs for a in r["ahus"]))
    put(am, "A1", "AHU Master", "Title")
    put(am, "A2", "Each AHU once. Room supply is the sum over the departments; calculated airflow adds the duct leakage. Tender airflow is information only: "
        "it is never a target or a limit. A calculated airflow above tender is marked VERIFY.", "Note")
    mh = [("ahu", "AHU or system", "", 26), ("type", "Type", "", 8), ("nd", "Departments", "no.", 11), ("n", "Rooms", "no.", 8), ("area", "Area", "m²", 10), ("nh", "Rooms with no ceiling height", "no.", 11),
          ("sa", "Room supply", "L/s", 11), ("leak", "Duct leakage", "%", 8), ("calc", "Calculated airflow", "L/s", 11), ("oa", "Sum of room outdoor air", "L/s", 11),
          ("tender", "Tender airflow", "L/s", 11), ("code", "Code", "", 5), ("sid", "Source ID", "", 16), ("tsrc", "Tender source", "", 36),
          ("red", "Reduction = tender − calculated", "L/s", 12), ("redp", "Reduction = (tender − calculated) ÷ tender", "%", 12),
          ("flag", "Against tender", "", 24), ("rem", "Remarks", "", 80)]
    col = {k: chr(65 + i) for i, (k, *_r) in enumerate(mh)}
    for i, (k, h, u, w) in enumerate(mh):
        put(am, f"{col[k]}5", h, "Header")
        put(am, f"{col[k]}6", u or None, "Unit")
        am.column_dimensions[col[k]].width = w
    am.row_dimensions[5].height = 42
    for t, tag in enumerate(tags):
        r = FIRST + t
        g = lambda k: f"{col[k]}{r}"
        put(am, g("ahu"), tag, "Tag")
        put(am, g("type"), types.get(tag), "Text")
        put(am, g("nd"), f"=COUNTIFS({rng('A')},{g('ahu')})", "Calc", "d0")
        put(am, g("n"), f"=SUMIFS({rng('D')},{rng('A')},{g('ahu')})", "Calc", "d0")
        put(am, g("area"), f"=SUMIFS({rng('E')},{rng('A')},{g('ahu')})", "Calc", "d1")
        put(am, g("nh"), f"=SUMIFS({rng('H')},{rng('A')},{g('ahu')})", "Calc", "d0")
        put(am, g("sa"), f"=SUMIFS({rng('F')},{rng('A')},{g('ahu')})", "Result", "d1")
        put(am, g("leak"), "=DuctLeakage", "Link", "pct1")
        put(am, g("calc"), f"={g('sa')}*(1+{g('leak')})", "Result", "d1")
        put(am, g("oa"), f"=SUMIFS({rng('G')},{rng('A')},{g('ahu')})", "Calc", "d1")
        te = tenders.get(tag)
        if te:
            put(am, g("tender"), te["lps"], "Input", "d0")
            put(am, g("code"), "D", "Code")
            put(am, g("sid"), te["sid"] or f"DEPT-{te['dept']}", "Tag")
            sid_ = te["sid"] or f"DEPT-{te['dept']}"
            if sid_ in src_row:
                link_to(am[g("sid")], "Sources", f"A{src_row[sid_]}")
            put(am, g("tsrc"), te["text"], "Text")
        else:
            put(am, g("tender"), None, "Input", "d0")
            flags.append(dict(type="VERIFY", ref=tag, text=f"AHU {tag}: no tender airflow is set in any department workbook."))
        put(am, g("red"), f'=IF({g("tender")}="","",{g("tender")}-{g("calc")})', "Calc", "d0")
        put(am, g("redp"), f'=IF(OR({g("tender")}="",{g("tender")}=0),"",{g("red")}/{g("tender")})', "Calc", "pct1")
        put(am, g("flag"), (f'=IF({g("tender")}="","NO TENDER VALUE",IF({g("red")}<0,"VERIFY: ABOVE TENDER",IF({g("red")}=0,"EQUALS TENDER",'
                            f'"REDUCTION AGAINST TENDER")))'), "Calc").alignment = Alignment(horizontal="left")
        put(am, g("rem"), tender_remark(g("tender"), g("calc"), g("red"), g("redp"), f'IF({g("tsrc")}="","tender airflow",{g("tsrc")})',
                                        lead=(f'IF({g("nh")}>0,"Main cause: ceiling height is not set for "&{g("nh")}&" of "&{g("n")}&" room(s) that this unit serves, '
                                              f'so their air-change airflow is 0 L/s.","")')), "Note")
    last = FIRST + max(len(tags), 1) - 1
    tr = last + 1
    put(am, f"A{tr}", "TOTAL", "ResultTag")
    for k in ("n", "nh", "area", "sa", "calc", "oa", "tender", "red"):
        put(am, f"{col[k]}{tr}", f"=SUM({col[k]}{FIRST}:{col[k]}{last})", "Total", "d0" if k in ("n", "nh", "tender", "red") else "d1")
    put(am, f"{col['redp']}{tr}", f'=IF({col["tender"]}{tr}=0,"",{col["red"]}{tr}/{col["tender"]}{tr})', "Total", "pct1")
    cf_formula(am, f"{col['flag']}{FIRST}:{col['flag']}{last}", f'LEFT({col["flag"]}{FIRST},6)="VERIFY"', bold=False, fill="FFFFF2CC")
    am.freeze_panes = f"B{FIRST}"

    # Flags
    put(fl, "A1", "Flags", "Title")
    put(fl, "A2", "Open items of the master summary.", "Note")
    for c, h in zip("ABCDE", ("Flag", "Type", "Reference", "Description", "Status")):
        put(fl, f"{c}5", h, "HeaderLeft")
    for i, f in enumerate(flags):
        put(fl, f"A{FIRST + i - 1}", f"F-{i + 1:03d}", "Tag")
        put(fl, f"B{FIRST + i - 1}", f["type"], "Text")
        put(fl, f"C{FIRST + i - 1}", f["ref"], "Tag")
        put(fl, f"D{FIRST + i - 1}", f["text"], "TextWrap")
        put(fl, f"E{FIRST + i - 1}", "OPEN", "Text")
    for c, w in zip("ABCDE", (8, 12, 28, 110, 10)):
        fl.column_dimensions[c].width = w

    # README
    put(rd, "B1", "AHU master summary", "Title")
    put(rd, "B2", project, "Note")
    lines = [("Purpose", "Each AHU is counted once across the departments. The department workbooks give the room supply that each department takes from the AHU."),
             ("Tender", "Tender airflow comes from the Equipment Schedule (Corr 5), or from the tender air-side schematic where the schedule has no row. It is information only."),
             ("Reduction", "Reduction = tender − calculated. Reduction (%) = (tender − calculated) ÷ tender. A calculated airflow above tender is marked VERIFY."),
             ("Departments", f"{len(recs)} department workbook(s): " + "; ".join(f"{r['department']['number']} {r['department']['name']}" for r in recs)),
             ("AHUs", f"{len(tags)} AHU row(s). Open flags: {len(flags)}."),
             ("Revision", f"Rev A, {date}. Author, checker and approver: not given.")]
    for i, (k, v) in enumerate(lines):
        put(rd, f"B{4 + i}", k, "Subhead")
        put(rd, f"C{4 + i}", v, "TextWrap")
    rd.column_dimensions["A"].width = 2
    rd.column_dimensions["B"].width = 16
    rd.column_dimensions["C"].width = 130
    for ws, lc, lr, pr, fz in ((rd, 3, 12, "1:2", None), (ba, 6, 6, "1:2", None), (dr, len(dh), dr_last, "5:6", f"B{FIRST}"),
                               (am, len(mh), tr, "5:6", f"B{FIRST}"), (so, 6, FIRST + len(srcs), "6:6", None), (fl, 5, FIRST + max(len(flags), 1), "5:5", None)):
        setup_sheet(ws, ws.title, rev="A", date=date, project=project, print_rows=pr, last_col=lc, last_row=lr, freeze=fz)
    wb.properties.creator = conf.people(Path(root))["by"]
    wb.properties.title = f"{project}: AHU master summary"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return dict(departments=len(recs), ahus=len(tags), flags=len(flags), out=str(out))
