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
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.pagebreak import Break
from openpyxl.workbook.defined_name import DefinedName

from . import conf
from .common import cf_formula, incomplete, tender_remark
from .sheets_systems import coverage_formula, small_zone, unit_extra
from .tag_notes import unit_note
from .layouts import AHU_KEYS
from .styles import CODE_STYLES, OCC_NAME, install_styles, link_to, put, setup_sheet

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
    project = str(wb["README"]["B2"].value or "").split(": thermal load")[0]
    fi = idx["frac"]
    num, _, name = title.partition("  ")
    out: List[Dict[str, Any]] = []
    for row in ah.iter_rows(min_row=FIRST, values_only=True):
        tag = row[idx["ahu"]]
        if not tag or tag == "TOTAL":
            break
        out.append(dict(tag=str(tag), type=row[idx["type"]], rooms=row[idx["n"]] or 0, area_m2=row[idx["area"]] or 0.0, rooms_no_height=row[idx["noh"]] or 0,
                        room_supply_lps=(row[idx["sa_room"]] or 0.0) + (row[idx["ucvadd"]] or 0.0),      # D-058: the future ultra-clean duty governs the unit
                         outdoor_air_lps=row[idx["oa_sum"]] or 0.0,
                        frac=row[fi] or 0.0, zone_rooms=row[idx["zr"]], zone_area_m2=row[idx["za"]],
                        tender_lps=row[idx["sched"]], tender_source=row[idx["tsrc"]], tender_source_id=row[idx["sched_s"]],
                        rooms_specified=row[idx["nfix"]] or 0, area_ok_m2=row[idx["area_ok"]] or 0.0,
                        tender_oa_lps=row[idx["sched_oa"]], rooms_ok=row[idx["nok"]] or 0, rooms_no_criteria=row[idx["nocrit"]] or 0, area_no_criteria_m2=row[idx["area_nocrit"]] or 0.0,
                        frac_not_single_pass=row[idx["fsp"]] or 0.0, single_pass_room=row[idx["spr"]] or "", single_pass_room_supply_lps=row[idx["spl"]] or 0.0,
                        frac_other_rooms=row[idx["spy"]] or 0.0, oa_balance_lps=row[idx["oa_bal"]] or 0.0, second_rooms=row[idx["second"]],
                        no_reduction=row[idx["norr_why"]] or "",
                        oa_clin_lps=row[idx["oa_clin"]] or 0.0, oa_nc_lps=row[idx["oa_nc"]] or 0.0))
    checks: Dict[str, Any] = {}
    if "Checks" in wb.sheetnames:
        for row in wb["Checks"].iter_rows(min_row=7, values_only=True):
            if row[1]:
                checks[str(row[1])] = row[2]
                if row[1] == "Calculation":
                    checks["Overall"] = row[4]
    rooms_total = sum(1 for r in wb["Room Information"].iter_rows(min_row=7, values_only=True) if r[0]) if "Room Information" in wb.sheetnames else 0
    dchk = dict(rooms=rooms_total, supply_on_unit=checks.get("AHU room supply equals the Airflow total of the rooms that have an AHU"),
                supply_no_unit=checks.get("Room supply of rooms that have no AHU and no system class"),
                supply_class=checks.get("Room supply of rooms that have no AHU and a system class MV, NV or no information"),
                rooms_no_unit=checks.get("Rooms with neither an AHU nor a system class"), overall=checks.get("Overall"))
    return dict(department=dict(number=num.strip(), name=name.strip()), ahus=out, file=path.name, project=project, checks=dchk)


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


_LEVEL = {"B3": -3, "B2": -2, "B1": -1}


def storey_order(tag: str):
    """Sort key of an AHU tag: storey (basement levels first), then tag."""
    import re
    for tok in tag.split("-")[1:4]:
        if re.fullmatch(r"B[1-3]|\d{2}", tok):
            return (_LEVEL.get(tok, int(tok) if tok.isdigit() else 99), tag)
    return (99, tag)


def merge_tag_variants(recs: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """One unit, one row. A schedule can print a unit with the plant level in the tag ('AHU-B-06-06SLEEP-01') where the zoning prints the level of the room
    ('AHU-B-07-06SLEEP-01'). When one form carries rooms and the other none, the second is renamed to the first and the tag is recorded as a variant. {tag: [variants]}."""
    import re
    from collections import defaultdict
    from .tag_notes import cath_tag_note
    with_rooms = {a["tag"] for r in recs for a in r["ahus"] if (a.get("rooms") or 0) > 0}
    by_rest: Dict[Any, set] = defaultdict(set)
    for r in recs:
        for a in r["ahus"]:
            m = re.match(r"^((?:AHU|PAHU|FCU)-B-)(\d\d)-(\d\d[A-Z0-9]*-\d+[A-Z/]*)$", a["tag"])
            if m:
                by_rest[(m.group(1), m.group(3))].add(a["tag"])
    mapping: Dict[str, str] = {}
    for tags in by_rest.values():
        roomed = [t for t in tags if t in with_rooms]
        if len(tags) > 1 and len(roomed) == 1:
            for t in tags:
                if t != roomed[0] and not cath_tag_note(t) and not cath_tag_note(roomed[0]):
                    mapping[t] = roomed[0]
    out: Dict[str, List[str]] = defaultdict(list)
    for r in recs:
        for a in r["ahus"]:
            if a["tag"] in mapping:
                out[mapping[a["tag"]]].append(a["tag"])
                a["tag"] = mapping[a["tag"]]
                a["tender_source"] = (str(a.get("tender_source") or "") + "; the schedule prints the tag as " + out[a["tag"]][-1]).lstrip("; ")
    return dict(out)


def build_master(inputs: Path, out: Path, root: Path = Path(".")) -> Dict[str, Any]:
    from . import styles as _styles
    from .polish import polish
    from types import SimpleNamespace
    recs = read_inputs(Path(inputs))
    variants = merge_tag_variants(recs)
    cfg = conf.config(Path(root))
    leak = float(((cfg.get("defaults_to_confirm") or {}).get("duct_leakage_pct", 10))) / 100.0
    date = conf.revision_date(Path(root))
    project = next((r["project"] for r in recs if r.get("project")), None) or str((cfg.get("project") or {}).get("name") or "Project")
    _styles.FOOTER_DEPT = "AHU master summary"
    wb = Workbook()
    wb.remove(wb.active)
    install_styles(wb)
    for n in ("README", "Basis", "Department Rows", "Department Checks", "AHU Master", "Checks", "Sources", "Flags"):
        wb.create_sheet(n)
    rd, ba, dr, dc, am, ck, so, fl = (wb[n] for n in ("README", "Basis", "Department Rows", "Department Checks", "AHU Master", "Checks", "Sources", "Flags"))

    tenders: Dict[str, Dict[str, Any]] = {}
    flags: List[Dict[str, str]] = []
    types: Dict[str, str] = {}
    tender_oa: Dict[str, float] = {}
    for r in recs:
        dn = r["department"]["number"]
        for a in r["ahus"]:
            types.setdefault(a["tag"], a.get("type") or "AHU")
            if a.get("tender_oa_lps") not in (None, "") and a["tag"] not in tender_oa:
                tender_oa[a["tag"]] = float(a["tender_oa_lps"])
            t = a.get("tender_lps")
            if t not in (None, ""):
                prev = tenders.get(a["tag"])
                if prev is None:
                    tenders[a["tag"]] = dict(lps=float(t), text=a.get("tender_source") or "", sid=a.get("tender_source_id") or "", dept=dn)
                elif abs(prev["lps"] - float(t)) > 1e-6:
                    flags.append(dict(type="VERIFY", ref=a["tag"], text=f"AHU {a['tag']}: department {prev['dept']} gives tender airflow {prev['lps']:g} L/s and "
                                      f"department {dn} gives {float(t):g} L/s. The first value is used."))

    for tag_, vs in variants.items():
        flags.append(dict(type="VERIFY", ref=tag_, text=f"Unit {tag_} is also printed as {', '.join(sorted(set(vs)))}. The tag forms are one unit row here. Confirm the tag."))
    from .tag_notes import cath_tag_note
    for tag_ in sorted({a["tag"] for r in recs for a in r["ahus"]}):
        if cath_tag_note(tag_):
            flags.append(dict(type="VERIFY", ref=tag_, text=f"Unit {tag_}: " + cath_tag_note(tag_)))
        if unit_note(tag_):
            flags.append(dict(type="VERIFY", ref=tag_, text=f"Unit {tag_}: " + unit_note(tag_)))

    # Basis: duct leakage and the tender leakage as named cells
    put(ba, "A1", "Basis", "Title")
    put(ba, "A2", "Constants of the master summary. Every constant is a named cell.", "Note")
    for c, h in zip("ABCDEF", ("Constant", "Value", "Unit (name)", "Code", "Source ID", "Remark")):
        put(ba, f"{c}4", h, "HeaderLeft")
    for rr, (label, val, nm, rem) in enumerate((("Duct leakage allowance on AHU supply airflow", leak, "DuctLeakage",
                                                 "10 % default confirmed for the project; any leakage class stated by a project document is a recorded difference"),
                                                ("Duct leakage stated on the tender schematics (comparison only)", 0.15, "TenderLeakage",
                                                 "used only in the Remarks wording; no calculation uses it"),
                                                ("Closure tolerance of the checks, absolute (L/s)", 10, "BalTolLps", "same value as the department workbooks"),
                                                ("Share of the zoned area that the counted rooms of a unit must reach before the room list of the unit is complete", 0.9, "ZoneCoverMin",
                                                 "same value as the department workbooks; to confirm"),
                                                ("Largest share of the zoned area that the counted rooms of a unit may reach", 1.1, "ZoneCoverMax",
                                                 "same value as the department workbooks; to confirm"),
                                                ("Smallest share of the tender airflow that the units stating a reduction must carry before the percentage of the total is shown", 0.5,
                                                 "MasterShareMin", "to confirm")), 5):
        put(ba, f"A{rr}", label, "Text")
        put(ba, f"B{rr}", val, "Specified" if nm not in ("ZoneCoverMin", "ZoneCoverMax", "MasterShareMin") else "Assumption", "d0" if nm == "BalTolLps" else "pct1")
        put(ba, f"C{rr}", f"{'L/s' if nm == 'BalTolLps' else 'fraction'}  ({nm})", "Text")
        put(ba, f"D{rr}", "S" if nm not in ("ZoneCoverMin", "ZoneCoverMax", "MasterShareMin") else "A", "Code")
        put(ba, f"E{rr}", "GEN-BASIS", "Tag")
        link_to(ba[f"E{rr}"], "Sources", "A7")
        put(ba, f"F{rr}", rem, "Remark")
        wb.defined_names[nm] = DefinedName(nm, attr_text=f"'Basis'!$B${rr}")
    for col, w in zip("ABCDEF", (60, 12, 28, 6, 14, 70)):
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
                     f"{len(r['ahus'])} AHU rows"))
    for tag_, te_ in tenders.items():
        sid_ = te_["sid"] or f"DEPT-{te_['dept']}"
        if sid_ not in {x[0] for x in srcs}:
            srcs.append((sid_, "Tender airflow as carried by the department workbook", "Corr 5 or tender schematic", te_["text"] or tag_,
                         f"department {te_['dept']}", f"tender airflow {te_['lps']:g} L/s"))
    for i, row in enumerate(srcs):
        for c, v in zip("ABCDEF", row):
            put(so, f"{c}{FIRST + i}", v, "Tag" if c == "A" else "TextWrap")
    for col, w in zip("ABCDEF", (24, 44, 22, 22, 40, 40)):
        so.column_dimensions[col].width = w
    src_row = {row[0]: FIRST + i for i, row in enumerate(srcs)}

    # Department Rows: one row per AHU and department
    put(dr, "A1", "Department Rows", "Title")
    put(dr, "A2", "Contribution of each department to each AHU, as read from the department workbook. Do not add these to the tender airflow.", "Note")
    dh = [("ahu", "AHU or system", "", 28), ("dept", "Department", "", 12), ("name", "Department name", "", 34), ("n", "Rooms", "no.", 8),
          ("area", "Area", "m²", 10), ("supply", "Room supply", "L/s", 11), ("oa", "Sum of room outdoor air", "L/s", 11),
          ("nh", "Rooms with no ceiling height", "no.", 11), ("frac", "Highest room outdoor-air fraction", "%", 12),
          ("code", "Code", "", 5), ("src", "Source ID", "", 12),
          ("zr", "Rooms of the unit in the zone schedule", "no.", 12), ("za", "Area of the unit in the zone schedule", "m²", 12),
          ("nfix", "Rooms specified by the MOT NSC", "no.", 12), ("aok", "Area of the rooms with a criteria row and a supply airflow", "m²", 14),
          ("nnc", "Rooms with no criteria row", "no.", 12), ("anc", "Area of the rooms with no criteria row", "m²", 14),
          ("fnsp", "Highest fraction of the rooms that are not single pass", "%", 14), ("spr", "Single-pass room that sets the fraction", "", 30),
          ("spl", "Supply of that room", "L/s", 11), ("spy", "Highest fraction of the other rooms", "%", 12),
          ("key", "Key (helper)", "", 12), ("idx", "Row (helper)", "", 8),
          ("nok", "Rooms with a criteria row and a supply airflow", "no.", 12),
          ("oab", "Outdoor air, balance basis (exhaust, offset air, spill air)", "L/s", 14), ("sl", "Rooms of the second room list", "no.", 12),
          ("nr", "Reason for no reduction against tender (tender base)", "", 40), ("nrk", "Unit with a reason (helper)", "", 12),
          ("oac", "Outdoor air of the HFDS / ASHRAE 170 rooms, straight sum (D-110)", "L/s", 14), ("oan", "Outdoor air of the SS 553 rooms (D-110)", "L/s", 14)]
    from openpyxl.utils import get_column_letter as _gcl
    for i, (k, h, u, w) in enumerate(dh):
        col = _gcl(i + 1)
        put(dr, f"{col}5", h, "Header" if k not in ("name",) else "HeaderLeft")
        put(dr, f"{col}6", u or None, "Unit")
        dr.column_dimensions[col].width = w
    row = FIRST
    for r in sorted(recs, key=lambda r: r["department"]["number"]):
        dn, nm = r["department"]["number"], r["department"]["name"]
        for a in sorted(r["ahus"], key=lambda a: storey_order(a["tag"])):
            put(dr, f"A{row}", a["tag"], "InputTag")
            put(dr, f"B{row}", dn, "InputTag")
            put(dr, f"C{row}", nm, "Input").alignment = Alignment(horizontal="left")
            put(dr, f"D{row}", a.get("rooms") or 0, "Input", "d0")
            put(dr, f"E{row}", a.get("area_m2") or 0, "Input", "d1")
            put(dr, f"F{row}", a.get("room_supply_lps") or 0, "Input", "d1")
            put(dr, f"G{row}", a.get("outdoor_air_lps") or 0, "Input", "d1")
            put(dr, f"H{row}", a.get("rooms_no_height") or 0, "Input", "d0")
            put(dr, f"I{row}", a.get("frac") or 0, "Input", "pct1")
            put(dr, f"L{row}", a.get("zone_rooms") or 0, "Input", "d0")
            put(dr, f"M{row}", a.get("zone_area_m2") or 0, "Input", "d1")
            put(dr, f"N{row}", a.get("rooms_specified") or 0, "Input", "d0")
            put(dr, f"O{row}", a.get("area_ok_m2") or 0, "Input", "d1")
            put(dr, f"P{row}", a.get("rooms_no_criteria") or 0, "Input", "d0")
            put(dr, f"Q{row}", a.get("area_no_criteria_m2") or 0, "Input", "d1")
            put(dr, f"R{row}", a.get("frac_not_single_pass") or 0, "Input", "pct1")
            put(dr, f"S{row}", a.get("single_pass_room") or None, "Input").alignment = Alignment(horizontal="left")
            put(dr, f"T{row}", a.get("single_pass_room_supply_lps") or 0, "Input", "d1")
            put(dr, f"U{row}", a.get("frac_other_rooms") or 0, "Input", "pct1")
            put(dr, f"V{row}", f'=IF(S{row}<>"",A{row}&"|"&TEXT(I{row},"0.0000"),"")', "Calc")
            put(dr, f"W{row}", f"=ROW()", "Calc", "d0")
            put(dr, f"X{row}", a.get("rooms_ok") or 0, "Input", "d0")
            put(dr, f"Y{row}", a.get("oa_balance_lps") or 0, "Input", "d1")
            put(dr, f"Z{row}", a.get("second_rooms") if a.get("second_rooms") not in ("", None) else None, "Input", "d0")
            put(dr, f"AA{row}", a.get("no_reduction") or None, "Input").alignment = Alignment(horizontal="left")
            put(dr, f"AB{row}", f'=IF(AA{row}<>"",A{row},"")', "Calc")
            put(dr, f"AC{row}", a.get("oa_clin_lps") or 0, "Input", "d1")
            put(dr, f"AD{row}", a.get("oa_nc_lps") or 0, "Input", "d1")
            put(dr, f"J{row}", "D", "Code")
            put(dr, f"K{row}", f"DEPT-{dn}", "Tag")
            link_to(dr[f"K{row}"], "Sources", f"A{src_row[f'DEPT-{dn}']}")
            row += 1
    dr_last = max(row - 1, FIRST)
    rng = lambda c: f"'Department Rows'!${c}${FIRST}:${c}${dr_last}"
    dr.freeze_panes = f"B{FIRST}"

    # Department Checks: the room supply and the rooms that the AHU rows cannot see (a room with no unit is on no AHU row)
    put(dc, "A1", "Department Checks", "Title")
    put(dc, "A2", "Values read from the Checks tab of each department workbook. The AHU rows hold only the rooms that have a unit: the supply and the rooms that have none are shown here.", "Note")
    dch = [("dept", "Department", "", 12), ("name", "Department name", "", 34), ("rooms", "Rooms", "no.", 9), ("on", "Room supply on a unit", "L/s", 14),
           ("off", "Room supply on no unit and no system class", "L/s", 16), ("cls", "Room supply on a room with a system class MV, NV or no information", "L/s", 18),
           ("norm", "Rooms with neither a unit nor a system class", "no.", 14), ("ov", "Overall status of the workbook", "", 14), ("file", "Workbook", "", 60)]
    for i, (k, h, u, w) in enumerate(dch):
        col_ = chr(65 + i)
        put(dc, f"{col_}5", h, "Header" if k not in ("name", "file") else "HeaderLeft")
        put(dc, f"{col_}6", u or None, "Unit")
        dc.column_dimensions[col_].width = w
    dc.row_dimensions[5].height = 54
    for i, r in enumerate(sorted(recs, key=lambda r: r["department"]["number"])):
        rr_ = FIRST + i
        ck_ = r.get("checks") or {}
        put(dc, f"A{rr_}", r["department"]["number"], "InputTag")
        put(dc, f"B{rr_}", r["department"]["name"], "Input").alignment = Alignment(horizontal="left")
        put(dc, f"C{rr_}", ck_.get("rooms"), "Input", "d0")
        put(dc, f"D{rr_}", ck_.get("supply_on_unit"), "Input", "d1")
        put(dc, f"E{rr_}", ck_.get("supply_no_unit"), "Input", "d1")
        put(dc, f"F{rr_}", ck_.get("supply_class"), "Input", "d1")
        put(dc, f"G{rr_}", ck_.get("rooms_no_unit"), "Input", "d0")
        put(dc, f"H{rr_}", ck_.get("overall"), "Input")
        put(dc, f"I{rr_}", r.get("file", ""), "Input").alignment = Alignment(horizontal="left")
    dc_last = FIRST + max(len(recs), 1) - 1
    dcr = lambda c: f"'Department Checks'!${c}${FIRST}:${c}${dc_last}"
    dc.freeze_panes = f"C{FIRST}"

    # AHU Master, sorted by storey then tag
    tags = sorted(dict.fromkeys(a["tag"] for r in recs for a in r["ahus"]), key=storey_order)
    put(am, "A1", "AHU Master", "Title")
    put(am, "A2", "Each AHU once, sorted by storey and tag. Room supply is the sum over the departments; calculated airflow adds the duct leakage. "
        "Outdoor air (D-110): on a unit with only HFDS / ASHRAE 170 rooms, the straight sum of the room outdoor air of all its departments; on SS 553 and mixed units, "
        "the highest room outdoor-air fraction of any department on the unit × the calculated airflow until the SS 553 Table 1 rates are set; never below the balance basis. Tender airflow is information only: "
        "it is never a target or a limit. A calculated airflow above tender is marked VERIFY. Outdoor air above the tender outdoor airflow is marked VERIFY and its reduction cells stay blank. "
        "A reduction is stated only where the calculated airflow is below tender and the room list of the unit is complete.", "Note")
    mh = [("ahu", "AHU or system", "", 28), ("type", "Type", "", 9), ("nd", "Departments", "no.", 13), ("n", "Rooms", "no.", 8), ("area", "Area", "m²", 10),
          ("nh", "Rooms with no ceiling height", "no.", 12),
          ("sa", "Room supply", "L/s", 11), ("leak", "Duct leakage", "%", 9), ("calc", "Calculated airflow", "L/s", 11),
          ("oa", "Sum of room outdoor air", "L/s", 11), ("frac", "Highest room outdoor-air fraction", "%", 12), ("oa_rule", "AHU outdoor air = larger of the minimum of the rule (D-110) and the balance basis", "L/s", 14),
          ("oa_mb", "Outdoor air, mass-balance basis (information only)", "L/s", 14),
          ("tender", "Tender airflow", "L/s", 11), ("toa", "Tender outdoor airflow", "L/s", 11), ("code", "Code", "", 5), ("sid", "Source ID", "", 24), ("tsrc", "Tender source", "", 40),
          ("zr", "Rooms of the unit in the zone schedule", "no.", 12), ("za", "Area of the unit in the zone schedule", "m²", 12), ("cov", "Room list of the unit (rooms counted)", "", 36),
          ("red", "Reduction = tender − calculated", "L/s", 12), ("redp", "Reduction = (tender − calculated) ÷ tender", "%", 12),
          ("flag", "Against tender", "", 28), ("oaflag", "Outdoor air against tender", "", 28), ("rem", "Remarks", "", 70),
          ("nfix", "Rooms specified by the MOT NSC", "no.", 12), ("aok", "Area of the rooms with a criteria row and a supply airflow", "m²", 14),
          ("nnc", "Rooms with no criteria row", "no.", 12), ("anc", "Area of the rooms with no criteria row", "m²", 14),
          ("nok", "Rooms with a criteria row and a supply airflow", "no.", 12),
          ("spnote", "Single-pass note (helper)", "", 20),
          ("oab", "Outdoor air, balance basis (exhaust, offset air, spill air)", "L/s", 14), ("sl", "Rooms of the second room list", "no.", 12),
          ("nr", "Reason for no reduction against tender (tender base)", "", 40),
          ("oac", "Outdoor air of the HFDS / ASHRAE 170 rooms, straight sum (D-110)", "L/s", 14), ("oan", "Outdoor air of the SS 553 rooms (D-110)", "L/s", 14),
          ("cls", "Unit class (HFDS/170, SS 553 or MIXED)", "", 12), ("oamin", "Minimum outdoor air of the rule (D-110)", "L/s", 14)]
    from openpyxl.utils import get_column_letter
    col = {k: get_column_letter(i + 1) for i, (k, *_r) in enumerate(mh)}
    for i, (k, h, u, w) in enumerate(mh):
        put(am, f"{col[k]}5", h, "HeaderLeft" if k in ("tsrc", "rem", "flag") else "Header")
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
        put(am, g("nfix"), f"=SUMIFS({rng('N')},{rng('A')},{g('ahu')})", "Calc", "d0")
        put(am, g("aok"), f"=SUMIFS({rng('O')},{rng('A')},{g('ahu')})", "Calc", "d1")
        put(am, g("nnc"), f"=SUMIFS({rng('P')},{rng('A')},{g('ahu')})", "Calc", "d0")
        put(am, g("anc"), f"=SUMIFS({rng('Q')},{rng('A')},{g('ahu')})", "Calc", "d1")
        put(am, g("nok"), f"=SUMIFS({rng('X')},{rng('A')},{g('ahu')})", "Calc", "d0")
        put(am, g("sa"), f"=SUMIFS({rng('F')},{rng('A')},{g('ahu')})", "Calc", "d1")
        put(am, g("leak"), "=DuctLeakage", "Link", "pct1")
        put(am, g("calc"), f"={g('sa')}*(1+{g('leak')})", "Result", "d1")
        put(am, g("oa"), f"=SUMIFS({rng('G')},{rng('A')},{g('ahu')})", "Calc", "d1")
        put(am, g("frac"), f"=_xlfn.MAXIFS({rng('I')},{rng('A')},{g('ahu')})", "Calc", "pct1")
        put(am, g("oab"), f"=SUMIFS({rng('Y')},{rng('A')},{g('ahu')})", "Calc", "d1")
        put(am, g("sl"), f'=IF(COUNTIFS({rng("A")},{g("ahu")},{rng("Z")},">0")=0,"",_xlfn.MAXIFS({rng("Z")},{rng("A")},{g("ahu")}))', "Calc", "d0")
        put(am, g("nr"), f'=IF(ISNUMBER(MATCH({g("ahu")},{rng("AB")},0)),INDEX({rng("AA")},MATCH({g("ahu")},{rng("AB")},0)),"")', "Calc")
        # D-110: minimum outdoor air = straight sum of the HFDS / ASHRAE 170 room outdoor air on an HFDS/170 unit; SS 553 and mixed units stay on the
        # fraction basis until the SS 553 Table 1 rates are set. D-032: the air that leaves the rooms of all the departments stays a floor.
        put(am, g("oac"), f"=SUMIFS({rng('AC')},{rng('A')},{g('ahu')})", "Calc", "d1")
        put(am, g("oan"), f"=SUMIFS({rng('AD')},{rng('A')},{g('ahu')})", "Calc", "d1")
        put(am, g("cls"), f'=IF(AND({g("oac")}>0,{g("oan")}>0),"MIXED",IF({g("oac")}>0,"HFDS/170",IF({g("oan")}>0,"SS 553","")))', "Calc")
        put(am, g("oamin"), f'=IF({g("cls")}="HFDS/170",{g("oac")},{g("frac")}*{g("calc")})', "Calc", "d1")
        put(am, g("oa_rule"), f"=MIN({g('calc')},MAX({g('oamin')},{g('oab')}))", "Calc", "d1")
        # R1-10 decided item 3 (information only, feeds nothing): larger of (sum of room outdoor air x (1 + DuctLeakage)) and (highest fraction of the rooms that are
        # not single pass x calculated airflow)
        put(am, g("oa_mb"), f"=MAX({g('oa')}*(1+{g('leak')}),_xlfn.MAXIFS({rng('R')},{rng('A')},{g('ahu')})*{g('calc')})", "Calc", "d1")
        # R1-10 decided item 2: the single-pass room that sets the fraction
        mt = f'MATCH({g("ahu")}&"|"&TEXT({g("frac")},"0.0000"),{rng("V")},0)'
        yy = f'MAX(INDEX({rng("U")},{mt}),_xlfn.MAXIFS({rng("I")},{rng("A")},{g("ahu")},{rng("W")},"<>"&INDEX({rng("W")},{mt})))'
        put(am, g("spnote"), (f'=IF(ISNUMBER({mt}),"The fraction "&TEXT({g("frac")},"0.0#")&" comes from "&INDEX({rng("S")},{mt})&" (single pass, "'
                              f'&TEXT(IF({g("sa")}>0,INDEX({rng("T")},{mt})/{g("sa")},0),"0.0 %")&" of the supply). The other rooms on this unit have a fraction of "'
                              f'&TEXT({yy},"0.0#")&" or less.","")'), "Calc")
        toa_ = tender_oa.get(tag)
        put(am, g("toa"), toa_, "Input", "d0")
        te = tenders.get(tag)
        if te:
            put(am, g("tender"), te["lps"], "Input", "d0")
            put(am, g("code"), "D", "Code")
            put(am, g("sid"), te["sid"] or f"DEPT-{te['dept']}", "Tag")
            sid_ = te["sid"] or f"DEPT-{te['dept']}"
            if sid_ in src_row:
                link_to(am[g("sid")], "Sources", f"A{src_row[sid_]}")
            put(am, g("tsrc"), te["text"], "TextWrap")
        else:
            put(am, g("tender"), None, "Input", "d0")
            note_ = next((a.get("tender_source") for r in recs for a in r["ahus"] if a["tag"] == tag and a.get("tender_source")), "")
            put(am, g("tsrc"), note_ or None, "TextWrap")
            flags.append(dict(type="VERIFY", ref=tag, text=f"AHU {tag}: no tender airflow is used. " + (str(note_) + "." if note_ else "No department workbook gives one.")))
        put(am, g("zr"), f"=_xlfn.MAXIFS({rng('L')},{rng('A')},{g('ahu')})", "Calc", "d0")
        put(am, g("za"), f"=_xlfn.MAXIFS({rng('M')},{rng('A')},{g('ahu')})", "Calc", "d1")
        put(am, g("cov"), coverage_formula(g("zr"), g("za"), g("nok"), g("aok"), "ZoneCoverMin", g("nnc"), zmax="ZoneCoverMax", sl=g("sl"), n=g("n"), norr=g("nr")), "Calc").alignment = Alignment(horizontal="left")
        oa_above = f'AND(ISNUMBER({g("toa")}),{g("oa_rule")}>{g("toa")})'
        put(am, g("oaflag"), f'=IF(NOT(ISNUMBER({g("toa")})),"not scheduled",IF({oa_above},"VERIFY: ABOVE TENDER OUTDOOR AIR","within tender outdoor air"))', "Calc").alignment = Alignment(horizontal="left")
        put(am, g("red"), f'=IF(OR({g("tender")}="",{g("calc")}=0,{incomplete(g("cov"))},{oa_above},{g("calc")}>={g("tender")}),"",{g("tender")}-{g("calc")})', "Calc", "d0")
        put(am, g("redp"), f'=IF(OR({g("red")}="",{g("tender")}=0),"",{g("red")}/{g("tender")})', "Calc", "pct1")
        # R3-03: above tender (supply or outdoor air) is VERIFY on every unit, before any test of the room list
        put(am, g("flag"), (f'=IF({g("tender")}="","NO TENDER VALUE",IF({g("calc")}=0,"NOT CALCULATED",IF({g("calc")}>{g("tender")},"VERIFY: ABOVE TENDER",'
                            f'IF({oa_above},"VERIFY: OUTDOOR AIR ABOVE TENDER",IF({incomplete(g("cov"))},"ROOM LIST NOT COMPLETE",IF({g("calc")}={g("tender")},"EQUALS TENDER",'
                            f'IF(AND({g("zr")}>0,{g("zr")}<=2),"REDUCTION AGAINST TENDER: NOT CONFIRMED","REDUCTION AGAINST TENDER")))))))'),
            "Calc").alignment = Alignment(horizontal="left")
        put(am, g("rem"), tender_remark(g("tender"), g("calc"), g("red"), g("redp"), f'IF({g("tsrc")}="","tender airflow",{g("tsrc")})',
                                        zero="No room of the department workbooks is on this unit, or no room of it has a supply airflow.",
                                        share_note=False, partial=g("cov"), note=g("tsrc"), all_spec=f'AND({g("n")}>0,{g("nfix")}={g("n")})',
                                        oa_above=oa_above, oa_calc=g("oa_rule"), oa_tender=g("toa"),
                                        extra=unit_extra(g("spnote"), unit_note(tag)), small=small_zone(g("zr"), g("za")),
                                        lead=(f'IF({g("nh")}>0,"Main cause: ceiling height is not set for "&{g("nh")}&" of "&{g("n")}&" rooms that this unit serves, '
                                              f'so their air-change airflow is 0 L/s.","")')), "Remark")
    last = FIRST + max(len(tags), 1) - 1
    # R3-10: the TOTAL row sums only the rows that state a reduction, so that tender − calculated = reduction in that row. The second row adds every row.
    tr1, tr2 = last + 1, last + 2
    red_rng = f"{col['red']}{FIRST}:{col['red']}{last}"
    put(am, f"A{tr1}", "TOTAL", "ResultTag")
    put(am, f"B{tr1}", "rows that state a reduction", "Note")
    put(am, f"A{tr2}", "TOTAL, all rows", "ResultTag")
    put(am, f"B{tr2}", "information", "Note")
    for k in ("n", "nh", "area", "sa", "calc", "oa", "oa_rule", "oa_mb", "tender", "red"):
        fmt = "d0" if k in ("n", "nh", "tender", "red") else "d1"
        rng_k = f"{col[k]}{FIRST}:{col[k]}{last}"
        put(am, f"{col[k]}{tr1}", (f"=SUM({rng_k})" if k == "red" else f"=SUMPRODUCT(--ISNUMBER({red_rng}),{rng_k})"), "Total", fmt)
        put(am, f"{col[k]}{tr2}", f"=SUM({rng_k})", "Total", fmt)
    # the percentage compares the reduction of the rows that state one with the tender of the same rows
    # D-054: the percentage of the total stays blank while the units that state a reduction carry less than MasterShareMin of the tender airflow of all units
    share = f'IF({col["tender"]}{tr2}=0,0,{col["tender"]}{tr1}/{col["tender"]}{tr2})'
    put(am, f"{col['redp']}{tr1}", f'=IF(OR({col["tender"]}{tr1}=0,{share}<MasterShareMin),"",{col["red"]}{tr1}/{col["tender"]}{tr1})', "Total", "pct1")
    put(am, f"{col['rem']}{tr1}", (f'="The TOTAL row adds only the rows that state a reduction: its tender − calculated = its reduction. These units carry "&TEXT({share},"0 %")&" of the tender airflow of all units. '
                                   f'The percentage stays blank while that share is below "&TEXT(MasterShareMin,"0 %")&". The row \'TOTAL, all rows\' adds every row."'), "Note")
    tr = tr2
    cf_formula(am, f"{col['flag']}{FIRST}:{col['flag']}{last}", f'LEFT({col["flag"]}{FIRST},6)="VERIFY"', bold=False, fill="FFFFF2CC")
    cf_formula(am, f"{col['oaflag']}{FIRST}:{col['oaflag']}{last}", f'LEFT({col["oaflag"]}{FIRST},6)="VERIFY"', bold=False, fill="FFFFF2CC")
    am.freeze_panes = f"B{FIRST}"
    ahu_rng = lambda c: f"'AHU Master'!${col[c]}${FIRST}:${col[c]}${last}"

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
    for c, w in zip("ABCDE", (10, 12, 30, 120, 10)):
        fl.column_dimensions[c].width = w
    fl_last = FIRST + max(len(flags), 1) - 2

    n_issued = len(list(Path(inputs).glob("AIGH_C2_D*_RevA.xlsx"))) if Path(inputs).is_dir() else len(recs)
    # Checks: the master tests itself against the department rows
    put(ck, "A1", "Checks", "Title")
    put(ck, "A2", "Live tests of the master summary against the department rows. PASS or FAIL are hard tests; VERIFY marks a finding to review.", "Note")
    for c, h in zip("ABCDEF", ("No.", "Check", "Value (unit in Note)", "Limit or expected (unit in Note)", "Status", "Note")):
        put(ck, f"{c}5", h, "HeaderLeft" if c in "BF" else "Header")
    ck.row_dimensions[6].height = 4
    tests = [
        ("Room supply of the department rows against the room supply of the AHU rows", f"=SUM({rng('F')})", f"=SUM({ahu_rng('sa')})",
         '=IF(ABS(C{r}-D{r})<=BalTolLps,"PASS","FAIL")', "L/s"),
        ("Rooms of the department rows against the rooms of the AHU rows", f"=SUM({rng('D')})", f"=SUM({ahu_rng('n')})", '=IF(C{r}=D{r},"PASS","FAIL")', "rooms"),
        ("Department rows whose AHU has no row on AHU Master", f"=SUMPRODUCT(--(COUNTIF({ahu_rng('ahu')},{rng('A')})=0))", 0, '=IF(C{r}=0,"PASS","FAIL")', "rows"),
        ("AHU rows that carry more than one row of the same tag", f"=SUMPRODUCT(--(COUNTIF({ahu_rng('ahu')},{ahu_rng('ahu')})>1))", 0, '=IF(C{r}=0,"PASS","FAIL")', "rows"),
        ("AHU outdoor air above the calculated airflow", f"=SUMPRODUCT(--({ahu_rng('oa_rule')}>{ahu_rng('calc')}+BalTolLps))", 0, '=IF(C{r}=0,"PASS","FAIL")', "rows"),
        ("AHU rows with no calculated airflow", f'=COUNTIF({ahu_rng("calc")},0)', 0, '=IF(C{r}=0,"PASS","VERIFY")', "rows; the reduction is not shown for them"),
        ("AHU rows with no tender airflow", f'=COUNTIF({ahu_rng("tender")},"")', 0, '=IF(C{r}=0,"PASS","VERIFY")', "rows"),
        ("AHU rows whose room list is not complete (not every zoned room is counted)", f'=COUNTIF({ahu_rng("cov")},"Not complete*")', "information", '="INFO"',
         "rows; see the Room list of the unit column"),
        ("AHU rows with a criteria gap (rooms with no criteria row)", f'=COUNTIF({ahu_rng("cov")},"*no criteria row*")', "information", '="INFO"',
         "rows; the calculated airflow of these units does not yet cover every room"),
        ("AHU rows that state a reduction and that the zone schedule gives 2 rooms or fewer ", f'=COUNTIF({ahu_rng("flag")},"REDUCTION AGAINST TENDER: NOT CONFIRMED*")', "information", '="INFO"',
         "rows; the reduction is kept as the room-list rule gives it and the Remarks column says that it is not confirmed"),
        ("AHU rows that state a reduction", f'=SUMPRODUCT(--ISNUMBER({ahu_rng("red")}))', "information", '="INFO"', "rows; the TOTAL row of AHU Master adds these rows only"),
        ("Zoned area that the department workbooks do not count on the AHU rows", f"=SUMPRODUCT(({ahu_rng('za')}>{ahu_rng('area')})*({ahu_rng('za')}-{ahu_rng('area')}))", 0,
         '=IF(C{r}<=0,"PASS","VERIFY")', "m² of the zone schedule"),
        ("AHU rows whose calculated airflow is above tender", f'=SUMPRODUCT(--ISNUMBER({ahu_rng("tender")}),--({ahu_rng("calc")}>{ahu_rng("tender")}))', 0, '=IF(C{r}=0,"PASS","VERIFY")',
         "rows; counted on every row, whatever the state of the room list"),
        ("AHUs with outdoor air above tender outdoor air", f'=SUMPRODUCT(--ISNUMBER({ahu_rng("toa")}),--({ahu_rng("oa_rule")}>{ahu_rng("toa")}))', 0, '=IF(C{r}=0,"PASS","VERIFY")',
         "rows; counted on every row. VERIFY, not FAIL: tender airflow is information only"),
        ("AHU rows that show VERIFY in the Against tender column", f'=COUNTIF({ahu_rng("flag")},"VERIFY*")', 0, '=IF(C{r}=0,"PASS","VERIFY")',
         "rows; above tender supply or above tender outdoor air. The two checks above count the same units from the numbers"),
        ("AHU rows with no tender outdoor airflow", f'=COUNTIF({ahu_rng("oaflag")},"not scheduled")', "information", '="INFO"', "rows; the outdoor air is not compared for them"),
        ("Rooms with no ceiling height", f"=SUM({ahu_rng('nh')})", 0, '=IF(C{r}=0,"PASS","VERIFY")', "rooms"),
        ("Source IDs that do not resolve on Sources",
         f"=SUMPRODUCT(({rng('K')}<>\"\")*(COUNTIF('Sources'!$A${FIRST}:$A${FIRST + len(srcs) - 1},{rng('K')})=0))", 0, '=IF(C{r}=0,"PASS","FAIL")', "IDs"),
        ("Formula errors", f"=SUMPRODUCT(--ISERROR('AHU Master'!$A${FIRST}:${col['rem']}${tr}))+SUMPRODUCT(--ISERROR({rng('A')}))", 0,
         '=IF(C{r}=0,"PASS","FAIL")', "cells"),
        ("Room supply on no unit, summed over the department workbooks", f"=SUM({dcr('E')})", 0, '=IF(C{r}<=BalTolLps,"PASS","FAIL")',
         "L/s; this supply is not in any AHU total"),
        ("Room supply on rooms with a system class MV, NV or no information, summed over the department workbooks", f"=SUM({dcr('F')})", 0, '=IF(C{r}<=BalTolLps,"PASS","VERIFY")',
         "L/s; no unit carries this supply"),
        ("Rooms on no unit and with no system class, summed over the department workbooks", f"=SUM({dcr('G')})", 0, '=IF(C{r}=0,"PASS","FAIL")', "rooms"),
        ("Department workbooks read against the department workbooks issued", f"=COUNTA({dcr('A')})", n_issued, '=IF(C{r}=D{r},"PASS","FAIL")', "workbooks"),
        ("Open flags", f'=COUNTIF(\'Flags\'!$E$5:$E${fl_last + 1},"OPEN")', "information", '="INFO"', "see Flags"),
    ]
    for i, (desc, val, lim, st, note) in enumerate(tests):
        rr = FIRST + i
        put(ck, f"A{rr}", i + 1, "Text", "d0")
        put(ck, f"B{rr}", desc, "Text")
        put(ck, f"C{rr}", val, "Calc", "d1" if i == 0 else "d0")
        put(ck, f"D{rr}", lim, "Calc", "d1" if i == 0 else "d0")
        put(ck, f"E{rr}", st.replace("{r}", str(rr)), "Check")
        put(ck, f"F{rr}", note, "Note")
    ck_last = FIRST + len(tests) - 1
    rr = ck_last + 2
    put(ck, f"B{rr}", "Calculation", "ResultText")
    # D-056: the two checks of the room supply and the rooms that no unit carries are input-completeness items, not calculation checks
    inc = f'(COUNTIFS(B{FIRST}:B{ck_last},"Room supply on no unit*",E{FIRST}:E{ck_last},"<>PASS")+COUNTIFS(B{FIRST}:B{ck_last},"Rooms on no unit*",E{FIRST}:E{ck_last},"<>PASS"))'
    nf = f'(COUNTIF(E{FIRST}:E{ck_last},"FAIL")-COUNTIFS(B{FIRST}:B{ck_last},"Room supply on no unit*",E{FIRST}:E{ck_last},"FAIL")-COUNTIFS(B{FIRST}:B{ck_last},"Rooms on no unit*",E{FIRST}:E{ck_last},"FAIL"))'
    put(ck, f"E{rr}", f'=IF({nf}>0,"FAIL",IF(COUNTIF(E{FIRST}:E{ck_last},"VERIFY")>0,"VERIFY","PASS"))', "ResultCheck")
    for c in "ACD":
        put(ck, f"{c}{rr}", None, "ResultText")
    put(ck, f"F{rr}", "Calculation checks. FAIL if any check fails; VERIFY if none fails and some need review", "ResultText")
    put(ck, f"B{rr + 1}", "Inputs complete", "ResultText")
    put(ck, f"E{rr + 1}", f'=IF({inc}=0,"COMPLETE","NOT COMPLETE")', "ResultCheck")
    for c in "ACD":
        put(ck, f"{c}{rr + 1}", None, "ResultText")
    put(ck, f"F{rr + 1}", "The AHU totals cover only the rooms that are linked to a unit. Read the coverage lines on the README before you use a total. DRAFT, acceptance open", "ResultText")
    cf_formula(ck, f"E{FIRST}:E{rr}", f'E{FIRST}="FAIL"')
    cf_formula(ck, f"E{FIRST}:E{rr}", f'E{FIRST}="VERIFY"', bold=False, fill="FFFFF2CC")
    for c, w in zip("ABCDEF", (6, 80, 16, 18, 10, 60)):
        ck.column_dimensions[c].width = w
    ck_rng = f"'Checks'!$E${FIRST}:$E${ck_last}"

    # README: same house style as the department workbooks
    put(rd, "B1", "AHU master summary", "Title")
    put(rd, "B2", project, "Note")
    for c, w in zip("ABCDEFG", (2, 28, 60, 40, 16, 14, 14)):
        rd.column_dimensions[c].width = w
    rd.column_dimensions["H"].width = 30
    r = 4
    put(rd, f"B{r}", "Read this first: coverage of the AHU totals", "Section")
    r += 1
    put(rd, f"C{r}", f'="Partial cover. The AHU totals add only the rooms that are linked to a unit: room supply on a unit is "&TEXT(SUM({dcr("D")}),"#,##0")&" L/s, against "'
                     f'&TEXT(SUM({dcr("D")})+SUM({dcr("E")})+SUM({dcr("F")}),"#,##0")&" L/s of room supply in the department workbooks, and "&TEXT(SUM({ahu_rng("tender")}),"#,##0")&" L/s of tender airflow on the units listed. '
                     f'Read every unit total as partial cover. A reduction against tender is stated only for a unit whose room list is complete."', "Calc").alignment = Alignment(wrap_text=True, vertical="top")
    put(rd, f"B{r}", "Coverage", "Text")
    rd.merge_cells(f"C{r}:H{r}")
    rd.row_dimensions[r].height = 58
    r += 2
    put(rd, f"B{r}", "Purpose and scope", "Section")
    r += 1
    for t in ("Each AHU is counted once across the department workbooks. The department workbooks give the room supply that each department takes from the AHU.",
              "Tender airflow comes from the Equipment Schedule (Corr 5), or from the tender air-side schematic where the schedule has no row. It is information only.",
              "Reduction = tender − calculated. Reduction (%) = (tender − calculated) ÷ tender. A calculated airflow above tender is marked VERIFY. Where the calculated outdoor air is above the tender outdoor airflow, the unit is marked VERIFY and its reduction cells stay blank."):
        put(rd, f"B{r}", t, "TextWrap")
        rd.merge_cells(f"B{r}:H{r}")
        rd.row_dimensions[r].height = 28
        r += 1
    r += 1
    put(rd, f"B{r}", "Status (live)", "Section")
    r += 1
    n_status = 7
    list_first = r + n_status + 2                  # first row of the list of department workbooks, below
    list_last = list_first + len(recs) - 1
    for label, f in (("Checks", f'=IF(COUNTIF({ck_rng},"FAIL")>0,COUNTIF({ck_rng},"FAIL")&" CHECK(S) FAIL",IF(COUNTIF({ck_rng},"VERIFY")>0,"NO CHECK FAILS; "&COUNTIF({ck_rng},"VERIFY")&" TO VERIFY","ALL CHECKS PASS"))'),
                     ("Room supply on an AHU", f'=TEXT(SUM({dcr("D")}),"#,##0")&" of "&TEXT(SUM({dcr("D")})+SUM({dcr("E")})+SUM({dcr("F")}),"#,##0")&" L/s ("&TEXT(IF(SUM({dcr("D")})+SUM({dcr("E")})+SUM({dcr("F")})=0,0,SUM({dcr("D")})/(SUM({dcr("D")})+SUM({dcr("E")})+SUM({dcr("F")}))),"0.0 %")&")"'),
                     ("Rooms on no unit and with no system class", f'=SUM({dcr("G")})&" of "&SUM({dcr("C")})'),
                     ("Thermal load case", '="PEOPLE, LIGHTING AND SMALL POWER (ASHRAE values where no project value). A provisional equipment gain is included for equipment-led rooms. Envelope gain is assessed by the simple facade method for external rooms."'),
                     ("Department workbooks listed below", f"=COUNTA($B${list_first}:$B${list_last})"),
                     ("AHU rows", f"=COUNTA({ahu_rng('ahu')})"),
                     ("Open flags", f"=COUNTIF('Flags'!$E$5:$E${fl_last + 1},\"OPEN\")")):
        put(rd, f"B{r}", label, "Text")
        put(rd, f"C{r}", f, "Calc", "d0").alignment = Alignment(horizontal="left", vertical="center")
        r += 1
    r += 1
    put(rd, f"B{r}", "Department workbooks", "Section")
    r += 1
    assert r == list_first, (r, list_first)
    for x in sorted(recs, key=lambda x: x['department']['number']):
        put(rd, f"B{r}", f"{x['department']['number']}", "Tag")
        put(rd, f"C{r}", x['department']['name'], "Text")
        put(rd, f"D{r}", x.get("file", ""), "Text")
        r += 1
    r += 1
    put(rd, f"B{r}", "Room list of a unit", "Section")
    r += 1
    put(rd, f"B{r}", "A unit states a reduction against tender only where the calculated airflow is below tender and the room list of the unit is complete. The room list is complete when "
        "the rooms counted in the department workbooks with a criteria row and a supply airflow are at least as many as the rooms that the zone schedule gives the unit, "
        "no counted room lacks a criteria row, and the area of the counted rooms is at least the share ZoneCoverMin of the zoned area. The Room list of the unit column gives "
        "'n of m zoned rooms counted'. For every other unit the Remarks column says nothing about a reduction. A calculated airflow above tender, and a calculated outdoor air above "
        "the tender outdoor airflow, are marked VERIFY on every unit. The TOTAL row adds only the rows that state a reduction. The row 'TOTAL, all rows' adds every row.", "TextWrap")
    rd.merge_cells(f"B{r}:H{r}")
    rd.row_dimensions[r].height = 56
    r += 2
    put(rd, f"B{r}", "Colour code and cell code", "Section")
    r += 1
    for c, h in zip("BCDE", ("Code and named style", "Meaning", "Sample", "Appearance")):
        put(rd, f"{c}{r}", h, "HeaderLeft")
    r += 1
    appearance = {"D": "blue text", "M": "blue text", "S": "blue text on light grey", "A": "blue text on light yellow", "C": "black text",
                  "L": "green text", "R": "black, bold, hairline rule above", "K": "pass plain; fail bold on light red"}
    for code, (style, meaning) in CODE_STYLES.items():
        put(rd, f"B{r}", f"{code}   {style}" + ("  /  Total" if code == "R" else ""), "Tag")
        put(rd, f"C{r}", meaning, "Text")
        put(rd, f"D{r}", 123.4 if code != "K" else "PASS", style, "d1")
        put(rd, f"E{r}", appearance[code], "Text")
        r += 1
        if code == "K":
            put(rd, f"B{r}", "K   Check, failed", "Tag")
            put(rd, f"C{r}", "A test that fails", "Text")
            c_ = put(rd, f"D{r}", "FAIL", "Check")
            c_.font = Font(name="Arial", size=10, bold=True)
            c_.fill = PatternFill("solid", fgColor="FFF4C7C3")
            put(rd, f"E{r}", "bold on light red", "Text")
            r += 1
    r += 1
    rd.row_breaks.append(Break(id=r - 1))
    put(rd, f"B{r}", "Tab map", "Section")
    r += 1
    for name, desc in (("Basis", "Duct leakage constants (named cells)"), ("Department Rows", "Contribution of each department to each AHU"),
                       ("AHU Master", "Each AHU once: calculated airflow, outdoor air, tender, reduction, remarks"),
                       ("Department Checks", "Room supply and rooms that no unit carries, from each department workbook"),
                       ("Checks", "Tests of the master against the department rows"), ("Sources", "One row per source ID"), ("Flags", "Open items")):
        c_ = put(rd, f"B{r}", name, "Text")
        link_to(c_, name, "A1")
        put(rd, f"C{r}", desc, "Text")
        r += 1
    r += 1
    put(rd, f"B{r}", "Units and abbreviations", "Section")
    r += 1
    put(rd, f"B{r}", "Units. Airflow: L/s. Area: m². AHU, coil and fan duty: kW. Percent: %. "
                     "Abbreviations. AHU: air handling unit. PAHU: pre-cooled air handling unit (the term of the Contract documents, RFI-ME-0233). FCU: fan coil unit. "
                     "CRAC: computer room air conditioner. OA: outdoor air. DD: developed design. MOT: major operating theatre (department 22). MOT NSC: the modular operating theatres works package of the tender documents.", "TextWrap")
    rd.merge_cells(f"B{r}:H{r}")
    rd.row_dimensions[r].height = 30
    r += 2
    put(rd, f"B{r}", "Revision", "Section")
    r += 1
    for c, h in zip("BCDEFG", ("Rev", "Date", "Description", "By", "Chk'd", "App'd")):
        put(rd, f"{c}{r}", h, "HeaderLeft")
    r += 1
    ppl = conf.people(Path(root))
    for c, v in zip("BCDEFG", ("A", date, "DRAFT, acceptance open", ppl["by"], ppl["checked"], ppl["approved"])):
        put(rd, f"{c}{r}", v, "Text")
    r += 1
    rd_last = r
    for ws, lc, lr, pr, fz in ((rd, 8, rd_last, "1:2", None), (ba, 6, 6, "1:2", None), (dr, len(dh), dr_last, "5:6", f"B{FIRST}"),
                               (dc, len(dch), dc_last, "5:6", f"C{FIRST}"),
                               (am, len(mh), tr, "5:6", f"B{FIRST}"), (ck, 6, rr, "5:6", None), (so, 6, FIRST + len(srcs), "6:6", None),
                               (fl, 5, FIRST + max(len(flags), 1), "5:5", None)):
        setup_sheet(ws, ws.title, rev="A", date=date, project=project, print_rows=pr, last_col=lc, last_row=lr, freeze=fz)
    # AHU Master: Code and Source ID are an outline group (collapsed for print); one page wide
    for k in ("code", "sid", "leak"):
        d = am.column_dimensions[col[k]]
        d.outline_level = 1
        d.hidden = True
    am.page_setup.fitToWidth = 1
    am.print_title_cols = None
    from .common import band_rows
    band_rows(am, f"A{FIRST}:{col['rem']}{last}")
    band_rows(dr, f"A{FIRST}:K{dr_last}")
    wide = SimpleNamespace(sources=[], rooms=[], ahus=[dict(tag=t) for t in tags], fans=[], terminals=[], valves=[])
    from .polish import id_widths
    idw = id_widths(wide)
    polish(wide, wb=wb, idw=idw, repage=False)
    wb.properties.creator = ppl["by"]
    wb.properties.title = f"{project}: AHU master summary"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    from .clean import clean_workbook
    clean_workbook(wb)
    wb.save(out)
    return dict(departments=len(recs), ahus=len(tags), flags=len(flags), out=str(out))
