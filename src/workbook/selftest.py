"""Self test: Rev A reproduction, known-answer pressure cases, error scan, style proof, PDF export."""

from __future__ import annotations

import copy
import datetime
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import openpyxl

from .build import build_workbook
from .recalc import recalc, to_pdf
from .reva import REVA_NAME, REVA_PATH, build_reva_input
from .layouts import AHU_KEYS, CP_KEYS, ENG, FAN_KEYS
from .styles import CODE_ALLOWED

ERROR_VALUES = {"#N/A", "#NAME?", "#VALUE!", "#REF!", "#DIV/0!", "#NUM!", "#NULL!", "Err:502", "Err:504", "Err:508", "Err:511",
                "Err:522", "Err:523"}
ALLOWED_FUNCS = {"SUM", "SUMIFS", "SUMIF", "SUMPRODUCT", "COUNTIF", "COUNTIFS", "COUNT", "COUNTA", "IF", "AND", "OR", "MAX", "MIN", "ABS", "SIGN",
                 "SQRT", "ROUND", "ROUNDUP", "TEXT", "INDEX", "MATCH", "ISNUMBER", "ISTEXT", "NOT", "ISERROR", "SEARCH", "ROW", "ROWS", "COLUMN", "LEN", "_XLFN.MAXIFS"}
ALLOWED_LITERALS = {"0", "1", "2"}
TOL_PCT = 0.5


def scan_errors(path: Path) -> List[Tuple[str, str, Any]]:
    wb = openpyxl.load_workbook(path, data_only=True)
    bad = []
    for ws in wb:
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and (c.value in ERROR_VALUES or c.value.startswith("Err:")):
                    bad.append((ws.title, c.coordinate, c.value))
    return bad


def scan_formulas(path: Path) -> Dict[str, Any]:
    """Function whitelist, typed numbers, IFERROR, external links, pasted numbers in calc tabs."""
    wb = openpyxl.load_workbook(path)
    funcs: Dict[str, int] = {}
    bad_funcs, literals, iferror, external = [], [], [], []
    n_formula = 0
    for ws in wb:
        for row in ws.iter_rows():
            for c in row:
                v = c.value
                if not (isinstance(v, str) and v.startswith("=")):
                    continue
                n_formula += 1
                body = re.sub(r'"[^"]*"', '""', v)
                if "[" in body and ".xls" in body.lower():
                    external.append((ws.title, c.coordinate))
                for f in re.findall(r"([A-Za-z_][A-Za-z0-9_.]*)\(", body):
                    fu = f.upper()
                    funcs[fu] = funcs.get(fu, 0) + 1
                    if fu == "IFERROR":
                        iferror.append((ws.title, c.coordinate))
                    if fu not in ALLOWED_FUNCS:
                        bad_funcs.append((ws.title, c.coordinate, fu))
                stripped = re.sub(r"'[^']*'!", "", body)
                stripped = re.sub(r"\$?[A-Z]{1,3}\$?\d+", "", stripped)
                stripped = re.sub(r"[A-Za-z_][A-Za-z0-9_.]*", " ", stripped)
                for m in re.findall(r"(?<![\w.])\d+(?:\.\d+)?", stripped):
                    if m not in ALLOWED_LITERALS:
                        literals.append((ws.title, c.coordinate, m))
    return dict(n_formula=n_formula, funcs=funcs, bad_funcs=bad_funcs, literals=literals, iferror=iferror, external=external)


INTERNAL_PATTERNS = [r"config/", r"out/(qa|registers|zoning|loads|sidecar)", r"\.(csv|md|py|json|yaml)\b", r"\bSection \d+(\.\d+)?\b",
                     r"\bbrief\b", r"\bGate \w+", r"\bTask \d", r"template_spec", r"\bD-0\d\d\b",
                     # tool and process wording (D-015): a document says what was read and where, never which program read it
                     r"\bopenpyxl\b", r"\blibre ?office\b", r"\bpython\d?\b", r"\bpdfplumber\b", r"\bpymupdf\b", r"\bvision\b", r"\btext layer\b",
                     r"(?-i:\bconfig\b)(?!-)", r"\bcached\b", r"\bformulas? kept\b", r"\bre-?read\b", r"\bnot re-?read\b", r"\bjson\b", r"\bcsv\b",
                     r"\bparser\b", r"\b(workbook|document|file|report) generator\b", r"\bself-?test\b", r"\bsidecar\b", r"\bmarkdown\b", r"\bocr\b", r"\bregion growing\b",
                     r"\bscript(s|ed)?\b", r"\bhumani[sz]er\b", r"\bste-lint\b", r"\bworkflow\b", r"\bpipeline\b", r"\bregisters?\b",
                     r"\bdata/source\b", r"\bscratch\b", r"\bregex\b", r"\balgorithm\b"]


# Room type names printed in the DD Booklet criteria and the DPA that contain a scanned word. They are source data and are not changed.
SOURCE_NAME_PHRASES = ("Workflow Training",)


def language_scan(path: Path, root: Path = Path(".")) -> List[Tuple[str, str, str]]:
    """Forbidden terms of the project configuration (deliverable language) and references to internal files or processes, in the
    text of every cell (text cells, the literals of formulas and the recalculated values), the sheet names, the defined names and the
    document properties. Whole words, any case; 'AI' is matched in capitals only."""
    from .conf import forbidden_terms
    terms = forbidden_terms(root)
    pats = []
    for t in terms:
        rx = re.compile(r"\b" + re.escape(t) + r"(s|es)?\b", 0 if t == "AI" else re.I)
        pats.append((t, rx))
    internal = [re.compile(p, re.I) for p in INTERNAL_PATTERNS]
    hits: List[Tuple[str, str, str]] = []

    def check(where: str, cell: str, text: str) -> None:
        for ph in SOURCE_NAME_PHRASES:     # room type names as printed in the source documents are data, not wording
            text = re.sub(re.escape(ph), "", text, flags=re.I)
        for t, rx in pats:
            m = rx.search(text)
            if m:
                hits.append((where, cell, f"forbidden term '{t}': ...{text[max(0, m.start() - 25):m.end() + 25]}..."))
        for rx in internal:
            m = rx.search(text)
            if m:
                hits.append((where, cell, f"internal reference '{m.group(0)}': ...{text[max(0, m.start() - 25):m.end() + 25]}..."))

    wbf = openpyxl.load_workbook(path)
    wbv = openpyxl.load_workbook(path, data_only=True)
    for ws in wbf:
        check("sheet name", ws.title, ws.title)
        wv = wbv[ws.title]
        for row in ws.iter_rows():
            for c in row:
                v = c.value
                if isinstance(v, str):
                    if v.startswith("="):
                        for lit in re.findall(r'"((?:[^"]|"")*)"', v):
                            check(ws.title, c.coordinate, lit.replace('""', '"'))
                        cv = wv[c.coordinate].value
                        if isinstance(cv, str):
                            check(ws.title, c.coordinate, cv)
                    else:
                        check(ws.title, c.coordinate, v)
    for name in wbf.defined_names:
        check("defined name", name, name)
    p = wbf.properties
    for k in ("creator", "title", "subject", "description", "keywords", "lastModifiedBy"):
        v = getattr(p, k, None)
        if v:
            check("properties", k, str(v))
    return hits


TOOL_NAMES = re.compile(r"openpyxl|libre ?office|pymupdf|mupdf|pdfplumber|reportlab|\bpython\b|microsoft excel|\bclaude\b|anthropic|\bcairo\b|\bskia\b|chromium", re.I)


def template_text_scan(path: Path) -> List[Tuple[str, str, str]]:
    """Template text left in a cell (R2-05): a '{' or '}' in the text of any cell, or in the recalculated value of a formula. A placeholder such as
    'No tender {what} found.' is a fault. The path should be a recalculated file, so that the values of the formulas are present."""
    hits: List[Tuple[str, str, str]] = []
    wb = openpyxl.load_workbook(path, data_only=True)
    for ws in wb:
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and ("{" in c.value or "}" in c.value):
                    hits.append((ws.title, c.coordinate, c.value[:80]))
    return hits


def properties_scan(path: Path) -> List[Tuple[str, str]]:
    """Tool names in the file properties of a workbook (R2-13): every docProps part of the package."""
    import zipfile
    hits: List[Tuple[str, str]] = []
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if n.startswith("docProps/") and n.endswith(".xml"):
                txt = re.sub(r"<[^>]+>", " ", z.read(n).decode("utf-8", "replace"))
                m = TOOL_NAMES.search(txt)
                if m:
                    hits.append((n, m.group(0)))
    return hits


def _patterns(root: Path = Path(".")) -> List[Tuple[str, "re.Pattern[str]"]]:
    from .conf import forbidden_terms
    pats = [(t, re.compile(r"\b" + re.escape(t) + r"(s|es)?\b", 0 if t == "AI" else re.I)) for t in forbidden_terms(root)]
    pats += [(f"internal reference '{p}'", re.compile(p, re.I)) for p in INTERNAL_PATTERNS]
    return pats


def method_column_scan(path: Path, root: Path = Path(".")) -> Tuple[int, List[Tuple[str, str]]]:
    """The Method column of the Sources tab (D-015): how each value was obtained, in document terms. Returns the number of cells read
    and the hits. An empty method is a hit, and so is a method that names a tool or an internal process."""
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb["Sources"]
    col = None
    for row in ws.iter_rows(min_row=1, max_row=12):
        for c in row:
            if c.value == "Method":
                col, first = c.column, c.row + 1
                break
        if col:
            break
    if col is None:
        return 0, [("Sources", "no Method column")]
    pats = _patterns(root)
    n, hits = 0, []
    for r in range(first, ws.max_row + 1):
        if not ws.cell(r, 1).value:
            continue
        v = ws.cell(r, col).value
        n += 1
        if not isinstance(v, str) or not v.strip():
            hits.append((f"G{r}", "empty method"))
            continue
        for name, rx in pats:
            if rx.search(v):
                hits.append((f"{ws.cell(r, col).coordinate}", f"{name}: {v[:80]}"))
                break
    return n, hits


def scan_strings(obj: Any, root: Path = Path("."), where: str = "") -> List[Tuple[str, str]]:
    """Hits of the deliverable-language patterns in every string of a nested structure (the department input of the generator)."""
    pats = _patterns(root)
    hits: List[Tuple[str, str]] = []

    def walk(o: Any, path: str) -> None:
        if isinstance(o, str):
            for name, rx in pats:
                m = rx.search(o)
                if m:
                    hits.append((path, f"{name}: ...{o[max(0, m.start() - 25):m.end() + 25]}..."))
                    break
        elif isinstance(o, dict):
            for k, v in o.items():
                walk(v, f"{path}.{k}")
        elif isinstance(o, (list, tuple)):
            for i, v in enumerate(o):
                walk(v, f"{path}[{i}]")
    walk(obj, where)
    return hits


def generator_text_scan(root: Path = Path(".")) -> Tuple[int, List[Tuple[str, int, str]]]:
    """Static scan of every prose string literal in the generator modules (text that can reach a cell): three words or more, not a
    docstring, not a file name. The command-line module and this module are excluded (console text). Returns (strings scanned, hits)."""
    import ast
    pats = _patterns(root)
    n, hits = 0, []
    pkg = Path(__file__).resolve().parent
    for f in sorted(pkg.glob("*.py")):
        if f.name in ("__main__.py", "selftest.py"):
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        skip = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body and isinstance(node.body[0], ast.Expr) \
                    and isinstance(getattr(node.body[0], "value", None), ast.Constant):
                skip.add(id(node.body[0].value))
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id.startswith("_SEARCH_") for t in node.targets):
                for sub in ast.walk(node.value):
                    skip.add(id(sub))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
                v = node.value
                if len(v.split()) < 3 or re.fullmatch(r"[\w./\\*-]+", v):
                    continue
                n += 1
                for name, rx in pats:
                    if rx.search(v):
                        hits.append((f.name, node.lineno, f"{name}: {v[:80]}"))
                        break
    return n, hits


def style_proof(ctx, path: Path) -> Dict[str, Any]:
    """Every coded cell has a named style and a code letter; every input source ID resolves; fonts and merges."""
    wb = openpyxl.load_workbook(path)
    src_ids = {r[0] for r in wb["Sources"].iter_rows(min_row=7, values_only=True) if r[0]}
    named = set(wb.named_styles)
    problems: List[str] = []
    n = 0
    for c in ctx.coded:
        ws = wb[c["sheet"]]
        cell = ws[c["cell"]]
        n += 1
        if cell.value is None:
            continue
        if cell.style not in CODE_ALLOWED[c["code"]] or cell.style not in named:
            problems.append(f"{c['sheet']}!{c['cell']}: style {cell.style} not allowed for code {c['code']}")
        if c.get("code_cell"):
            letter = ws[c["code_cell"]].value
            if letter != c["code"]:
                problems.append(f"{c['sheet']}!{c['cell']}: code cell {c['code_cell']} holds {letter!r}, expected {c['code']}")
        if c["code"] in "DMSA" and c.get("src_cell"):
            sid = ws[c["src_cell"]].value
            if sid not in src_ids:
                problems.append(f"{c['sheet']}!{c['cell']}: source id {sid!r} does not resolve on Sources")
        elif c["code"] in "DMSA" and not c.get("src_cell"):
            problems.append(f"{c['sheet']}!{c['cell']}: input without a source cell")
    stray_font, unnamed, merged = [], [], []
    for ws in wb:
        for m in ws.merged_cells.ranges:
            merged.append((ws.title, str(m)))
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                if cell.font.name not in ("Arial", "Consolas"):
                    stray_font.append((ws.title, cell.coordinate, cell.font.name))
                if cell.style not in named or cell.style == "Normal":
                    unnamed.append((ws.title, cell.coordinate, cell.style))
    return dict(n_coded=n, problems=problems, stray_font=stray_font, unnamed=unnamed, merged=merged)


# --------------------------------------------------------------------------- #
def ka_input() -> Dict[str, Any]:
    """Department of three rooms carrying the known-answer rooms of the pressure method (envelope only) through the real Pressure tables."""
    base = build_reva_input()
    src = dict(id="KA-8.3", document="Project pressure-offset method, known-answer table", issue="project method", page="known answers", location="known-answer test",
               text_as_read="room 1.88 x 2.94 x 3.0 m; room 3.97 x 4.83 x 3.0 m; ensuite 37.5 m2 surface; envelope only",
               method="transcribed from the project method", confidence="high")
    d = copy.deepcopy(base)
    d["sources"].append(src)
    d["project"] = dict(d["project"], name="Known-answer pressure suites (synthetic test)",
                        revision=dict(rev="A", date=datetime.date.today().isoformat(), description="Synthetic known-answer test; not a Rev A reproduction",
                                      by="", checked="", approved=""))
    d["department"] = dict(number="KA", name="Known-answer pressure suites (synthetic test)")
    d["references"] = []
    d["flags"] = []

    def room(no, name, area, perim, suite, pa, doors, override=None):
        return dict(number=no, name=name, storey="L01", ahu="AHU-KA", zone_tag=no, system_type="VAV", terminal_tag=None,
                    space_type="Pharmacy (OP)", room_condition="Comfort", soa_area_m2=area, area_selector="SOA", ceiling_height_m=3.0,
                    prov={"identity": ["D", "KA-8.3"], "zoning": ["D", "KA-8.3"], "space_type": ["D", "KA-8.3"], "soa_area": ["D", "KA-8.3"],
                          "ceiling_height": ["D", "KA-8.3"], "area_selector": ["S", "GEN-BASIS"]},
                    criteria=dict(criteria_row="known-answer room", page="8.3", min_oa_ach=2, min_total_ach=6, pressure_regime="NEGATIVE",
                                  pressure_pa_tender=pa, air_class="1", single_pass="Y", source="KA-8.3", code="D"),
                    pressure=dict(mode="controlled", suite=suite, perimeter_m=perim, prov=["D", "KA-8.3"], surface_override_m2=override,
                                  surface_prov=["D", "KA-8.3"], doors=doors),
                    envelope=[])

    d["rooms"] = [
        room("ANTE", "Room at -15 Pa", 1.88 * 2.94, 2 * (1.88 + 2.94), "S4", -15, []),
        room("ROOM", "Room at -30 Pa", 3.97 * 4.83, 2 * (3.97 + 4.83), "S5", -30, []),
        room("ENSUITE", "Ensuite at -31 Pa", 4.0, 3.0, "S5", -31, [], override=37.5),
    ]
    d["pressure_nodes"] = [dict(tag="REF", description="Reference: corridor", pressure_pa=0, code="S", source="GEN-PRESSURE")]
    return d


def compare_recompute(data: Dict[str, Any], rc: Path, tol_pct: float = TOL_PCT) -> List[Dict[str, Any]]:
    """Independent recomputation (src.recompute.thermal, HIGHEST_FRACTION) against a recalculated workbook."""
    from ..recompute.thermal import recompute
    res = recompute(data, "HIGHEST_FRACTION")
    wb = openpyxl.load_workbook(rc, data_only=True)
    out: List[Dict[str, Any]] = []

    def add(label, mine, theirs):
        mine, theirs = float(mine or 0.0), float(theirs or 0.0)
        diff = abs(mine - theirs)
        pct = 100.0 * diff / abs(theirs) if theirs else (0.0 if diff < 1e-9 else 100.0)
        out.append(dict(item=label, workbook=mine, recomputed=theirs, diff_pct=pct, ok=pct <= tol_pct or diff <= 0.05))

    af = wb["Airflow"]
    hdr = {h: i for i, h in enumerate(c.value for c in af[5])}
    by_no = {r["number"]: r for r in res["rooms"]}
    for row in af.iter_rows(min_row=7, values_only=True):
        no = row[0]
        if no in by_no:
            r = by_no[no]
            for lab, col, key in (("supply", "Final supply", "supply_lps"), ("outdoor air", "Outdoor air", "oa_lps"),
                                  ("exhaust", "Exhaust", "exhaust_lps"), ("sensible W", "Room sensible", "sensible_w"),
                                  ("latent W", "Room latent", "latent_w")):
                add(f"room {no} {lab}", row[hdr[col]], r[key])
    ah = wb["AHU"]
    for t, a in enumerate(res["ahus"]):
        pick = lambda key: ah.cell(row=7 + t, column=AHU_KEYS.index(key) + 1).value
        add(f"AHU {a['tag']} supply", pick("sa_ahu"), a["supply_lps"])
        add(f"AHU {a['tag']} outdoor air", pick("oa_rule"), a["oa_lps"])
        add(f"AHU {a['tag']} coil kW", pick("coil"), a["coil_total_kw"])
        add(f"AHU {a['tag']} return", pick("ret"), a["return_lps"])
        add(f"AHU {a['tag']} exhaust", pick("exh"), a["exhaust_lps"])
    ps = wb["Pressure"]
    ph = {}
    for i, c in enumerate(ps[5]):
        if c.value and c.value not in ph:
            ph[c.value] = i
    for row in ps.iter_rows(min_row=7, values_only=True):
        no = row[0]
        if no in by_no and by_no[no].get("pressure") and by_no[no]["pressure"].get("mode") == "controlled" \
                and isinstance(row[ph["Offset needed"]], (int, float)):
            add(f"room {no} offset needed m3/h", row[ph["Offset needed"]], by_no[no]["pressure"]["offset_needed_m3h"])
    return out


def _cell(wb, sheet, row_pred, col):
    ws = wb[sheet]
    for r in ws.iter_rows(min_row=1):
        if row_pred(r):
            return r[col].value
    return None


def compare_reva(rc: Path, root: Path) -> Dict[str, Any]:
    """Compare the recalculated generator workbook with the Rev A example's own cached values, line by line."""
    gen = openpyxl.load_workbook(rc, data_only=True)
    ref = openpyxl.load_workbook(root / REVA_PATH, data_only=True)
    rz, hl, ahs = ref["Level 1 Room Zones"], ref["Heat load summary"], ref["AHU Summary"]
    rows: List[Dict[str, Any]] = []

    def add(label, mine, theirs, note=""):
        pct = None if not theirs else 100.0 * (mine - theirs) / theirs
        rows.append(dict(item=label, workbook=mine, reva=theirs, diff_pct=pct, ok=(pct is not None and abs(pct) <= TOL_PCT) or mine == theirs, note=note))

    ah = gen["AHU"]
    pick = lambda key: ah.cell(row=7, column=AHU_KEYS.index(key) + 1).value
    add("Area of the supply rooms (m2)", pick("area_sup"), hl["E3"].value)
    add("Room supply airflow (L/s)", pick("sa_room"), hl["H3"].value)
    add("Outdoor air, multiple-space value Vot (L/s)", pick("vot"), ahs["AA8"].value)
    add("Outdoor air on the example's basis, max(sum, Vot) (L/s)", pick("oa_ms"), ahs["I8"].value)
    add("Sum of room outdoor air (L/s)", pick("oa_sum"), ahs["T8"].value)
    add("Room sensible (kW)", pick("sens"), hl["L3"].value)
    add("Room latent (kW)", pick("lat"), hl["M3"].value)
    add("AHU supply with 10 % leakage (L/s)", pick("sa_ahu"), ahs["E8"].value)
    add("Fan heat (kW)", pick("fan"), ahs["B31"].value)
    add("Outdoor air sensible, example basis (kW)", pick("oa_sens_ms"), ahs["B26"].value)
    add("Coil duty on the example's outdoor-air basis (kW)", pick("coil_ms"), ahs["B35"].value,
        "room condition of the first space type (52.6 kJ/kg), as the example's AHU Summary B13/B14")
    # per room
    rz_rows = {"Z1": 30, "Z2": 31, "Z3": 32, "Z4": 33, "Z5": 34}
    eng = gen["Level 1 Room Zones"]
    ecol = lambda key: [e[0] for e in ENG].index(key)
    room_rows = []
    for r in range(8, 8 + 6):
        z = eng.cell(row=r, column=ecol("zone") + 1).value
        if z not in rz_rows:
            continue
        rr = rz_rows[z]
        vals = dict(zone=z, area=(eng.cell(row=r, column=ecol("area") + 1).value, rz[f"M{rr}"].value),
                    sens=(eng.cell(row=r, column=ecol("sens") + 1).value, rz[f"BO{rr}"].value),
                    lat=(eng.cell(row=r, column=ecol("lat") + 1).value, rz[f"BQ{rr}"].value),
                    th=(eng.cell(row=r, column=ecol("th_lps") + 1).value, rz[f"BW{rr}"].value),
                    oa=(eng.cell(row=r, column=ecol("oa") + 1).value, rz[f"X{rr}"].value),
                    sa=(eng.cell(row=r, column=ecol("fin") + 1).value, rz[f"BY{rr}"].value))
        room_rows.append(vals)
    # rule-based figures and their cause
    rule_oa = pick("oa_rule")
    rule_coil = pick("coil")
    return dict(lines=rows, rooms=room_rows, rule_oa=rule_oa, rule_coil=rule_coil, reva_oa=ahs["I8"].value, reva_coil=ahs["B35"].value,
                crit_room=pick("crit"), crit_frac=pick("frac"))


def compare_ka(rc: Path) -> List[Dict[str, Any]]:
    """Known-answer cases from the Pressure tab of a recalculated workbook (live block) and from the node tables."""
    wb = openpyxl.load_workbook(rc, data_only=True)
    ws = wb["Pressure"]
    out = []
    for r in ws.iter_rows(min_row=1, values_only=True):
        if r[7] in ("PASS", "FAIL") and isinstance(r[4], (int, float)) and isinstance(r[5], (int, float)) and r[0] and r[2]:
            out.append(dict(case=r[0], check=r[2], computed=r[4], expected=r[5], tol=r[6], ok=abs(r[4] - r[5]) <= r[6], where="known-answer block"))
    return out


def compare_ka_tables(rc: Path) -> List[Dict[str, Any]]:
    wb = openpyxl.load_workbook(rc, data_only=True)
    ws = wb["Pressure"]
    hdr = [c.value for c in ws[5]]
    idx = {}
    for i, h in enumerate(hdr):
        if h and h not in idx:
            idx[h] = i
    node = {}
    for r in ws.iter_rows(min_row=7, max_row=12, values_only=True):
        if r[0]:
            node[r[0]] = r
    g = lambda n, h: node[n][idx[h]]
    res = []
    def add(case, comp, exp, tol):
        res.append(dict(case=case, computed=comp, expected=exp, tol=tol, ok=abs(comp - exp) <= tol, where="node and path tables"))
    add("4 room at -15 Pa: envelope in (45.7)", g("ANTE", "Envelope in"), 45.7, 0.2)
    add("4 room at -15 Pa: offset needed", g("ANTE", "Offset needed"), 45.7, 0.2)
    add("4 room at -15 Pa: design offset", g("ANTE", "Design offset"), 99.9, 0.2)
    add("4 room at -15 Pa: ratio", g("ANTE", "Ratio design / needed"), 2.19, 0.01)
    add("5 room at -30 Pa: offset needed (envelope)", g("ROOM", "Offset needed"), 163.5, 0.2)
    add("5 room at -30 Pa: design offset", g("ROOM", "Design offset"), 227.9, 0.2)
    add("5 room at -30 Pa: ratio", g("ROOM", "Ratio design / needed"), 1.39, 0.01)
    add("6 ensuite at -31 Pa: offset needed (envelope)", g("ENSUITE", "Offset needed"), 68.7, 0.2)
    add("6 ensuite at -31 Pa: design offset", g("ENSUITE", "Design offset"), 93.8, 0.2)
    add("6 ensuite at -31 Pa: ratio", g("ENSUITE", "Ratio design / needed"), 1.36, 0.01)
    return res


def pdf_proof(rc: Path, out_dir: Path) -> Dict[str, Any]:
    pdf = to_pdf(rc, out_dir)
    pages = 0
    footer = False
    first_text = ""
    try:
        txt = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True, timeout=120).stdout
        pages = txt.count("\f")
        footer = bool(re.search(r"Page \d+ of \d+", txt))
        first_text = txt[:200]
    except FileNotFoundError:
        try:
            import pypdf
            r = pypdf.PdfReader(str(pdf))
            pages = len(r.pages)
            footer = any("Page" in (p.extract_text() or "") for p in r.pages[:3])
        except Exception:
            pages = -1
    return dict(pdf=str(pdf), pages=pages, footer_found=footer, size=pdf.stat().st_size)


def print_proof(xlsx: Path, pdf: Path) -> Dict[str, Any]:
    """No column cut and legible (brief 8.5): print areas cover every visible data column; the scale of each sheet is at
    least PRINT_MIN_SCALE; the header of the last printed column is found in the PDF text."""
    from openpyxl.utils import column_index_from_string as ci, get_column_letter as gl
    from .styles import A3_PRINTABLE_IN, PRINT_MIN_SCALE, _col_inches, col_dims
    wb = openpyxl.load_workbook(xlsx)
    try:
        txt = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True, timeout=120).stdout
    except FileNotFoundError:
        txt = ""
    flat = re.sub(r"\s+", " ", txt)
    problems: List[str] = []
    sheets: Dict[str, Any] = {}
    for ws in wb:
        pa = ws.print_area
        if not pa:
            problems.append(f"{ws.title}: no print area")
            continue
        last_col = ci(re.sub(r"[^A-Z]", "", pa.split(":")[1].replace("$", "")))
        cut = []
        dims = col_dims(ws)
        for row in ws.iter_rows():
            for c in row:
                if c.value is not None and c.column > last_col:
                    dim = dims.get(c.column)
                    if not (dim is not None and dim.hidden) and gl(c.column) not in cut:
                        cut.append(gl(c.column))
        if cut:
            problems.append(f"{ws.title}: visible data outside the print area in columns {cut[:8]}")
        pages = ws.page_setup.fitToWidth or 1
        widths = _col_inches(ws, last_col)
        total = sum(widths)
        rep_cols = 0
        if ws.print_title_cols:
            rep_cols = ci(re.sub(r"[^A-Z]", "", ws.print_title_cols.split(":")[1]))
        rep = sum(widths[:rep_cols])
        scale = min(1.0, pages * A3_PRINTABLE_IN / (total + (pages - 1) * rep)) if total else 1.0
        if scale < PRINT_MIN_SCALE - 1e-9:
            problems.append(f"{ws.title}: print scale {scale:.2f} below {PRINT_MIN_SCALE}")
        head = None
        for r in (5, 4):
            v = ws.cell(row=r, column=last_col).value
            if isinstance(v, str) and v.strip():
                head = v
                break
        if head and txt:
            word = re.split(r"[\s(]", head.strip())[0]
            if len(word) >= 4 and word not in flat:
                problems.append(f"{ws.title}: header {head!r} of the last printed column not found in the PDF")
        sheets[ws.title] = dict(pages_wide=pages, scale=round(scale, 2), last_print_col=gl(last_col), width_in=round(total, 1))
    return dict(problems=problems, sheets=sheets)


def d013_check(base: Dict[str, Any], work: Path) -> Dict[str, Any]:
    """Rev A input with tender airflows on its AHU, a fan, one room and one FCU unit; the recalculated reduction columns and
    remarks must equal the values worked by hand. Tender values never change a calculated value."""
    d = copy.deepcopy(base)
    d["sources"].append(dict(id="TND-1", document="Tender equipment schedule, test row", issue="Corr 5", page="p1", location="row 1",
                             text_as_read="supply 3500 L/s", method="test", confidence="high"))
    tag = d["rooms"][0]["ahu"]
    ahu = next((a for a in d.get("ahus", []) if a["tag"] == tag), None)
    if ahu is None:
        ahu = dict(tag=tag, type="AHU")
        d.setdefault("ahus", []).append(ahu)
    ahu.update(sched_supply_lps=3500.0, sched_code="D", sched_source="TND-1", tender_source_text="tender equipment schedule Corr 5, test row",
               zone_rooms=1, zone_area_m2=1.0, zone_code="D", zone_source="TND-1")     # a zone that the counted rooms cover: the room list of the unit is complete (D-027)
    d["fans"] = [dict(tag="RAF-T1", type="RETURN", serves=tag, scheduled_lps=3000.0, code="D", source="TND-1",
                      tender_source_text="tender equipment schedule Corr 5, test fan row")]
    r0 = d["rooms"][0]
    d["rooms"][0] = dict(r0, schematic=dict(supply_lps=1000.0, source="TND-1", code="D", source_text="tender schematic, test room"))
    out = work / "d013.xlsx"
    build_workbook(d, out)
    rc = recalc(out, work / "rc")
    wb = openpyxl.load_workbook(rc, data_only=True)
    fails: List[str] = []
    ah = wb["AHU"]
    idx = {k: i + 1 for i, k in enumerate(AHU_KEYS)}
    row = 7 + [a["tag"] for a in _ahus_in_order(d)].index(tag)
    sa, tn = ah.cell(row=row, column=idx["sa_ahu"]).value, ah.cell(row=row, column=idx["sched"]).value
    red, redp, rem = (ah.cell(row=row, column=idx[k]).value for k in ("red", "redp", "rem"))
    if abs(tn - 3500.0) > 1e-9 or abs(red - (3500.0 - sa)) > 1e-6 or abs(redp - (3500.0 - sa) / 3500.0) > 1e-9:
        fails.append(f"AHU reduction {tn} {sa} {red} {redp}")
    want = f"Reduced 3,500 to {sa:,.0f} L/s"
    if not (isinstance(rem, str) and (rem.startswith(want) or rem.startswith("VERIFY"))):
        fails.append(f"AHU remarks {rem!r}")
    if abs(sa - 3036.0) > 1e-6:
        fails.append(f"tender changed the calculated AHU supply: {sa}")
    cp = wb["Airflow Comparison"]
    if "tender" not in str(cp.cell(row=7, column=CP_KEYS.index("remark") + 1).value).lower():
        fails.append("room remarks on Airflow Comparison")
    fn = wb["Fans"]
    fr = [fn.cell(row=7, column=FAN_KEYS.index(k) + 1).value for k in ("sched", "drawn", "red", "redp", "rem")]
    if fr[0] < fr[1]:          # calculated above tender: no reduction is stated, the remark says VERIFY (D-027)
        fan_ok = fr[2] in (None, "") and str(fr[4]).startswith("VERIFY")
    else:                      # calculated below tender: the reduction is stated
        fan_ok = fr[2] is not None and abs(fr[2] - (fr[0] - fr[1])) <= 1e-6 and str(fr[4]).startswith("Reduced")
    if not fan_ok:
        fails.append(f"Fans reduction {fr}")
    # a unit whose room list is not complete against the zone schedule shows no reduction and says nothing about a reduction (D-027)
    dp = copy.deepcopy(d)
    for a in dp["ahus"]:
        if a["tag"] == tag:
            a.update(zone_rooms=500, zone_area_m2=500000.0)
    outp = work / "d013_partial.xlsx"
    build_workbook(dp, outp)
    wbp = openpyxl.load_workbook(recalc(outp, work / "rc_partial"), data_only=True)
    ahp = wbp["AHU"]
    redp_, remp, covp = (ahp.cell(row=row, column=idx[k]).value for k in ("red", "rem", "cov"))
    if redp_ not in (None, "") or "Reduced" in str(remp) or "partial" in str(remp).lower() or "No reduction" in str(remp) or not str(covp).startswith("Not complete"):
        fails.append(f"incomplete unit shows a reduction, or reduction wording, or no 'Not complete' text: {redp_!r} {remp!r} {covp!r}")
    return dict(ahu_remarks=rem, fan_row=fr, fails=fails, errors=len(scan_errors(rc)))


def _ahus_in_order(d: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in d["rooms"]:
        t = r.get("ahu")
        if t and t not in [a["tag"] for a in out]:
            out.append(dict(tag=t))
    return out


def occupancy_check(base: Dict[str, Any], work: Path) -> Dict[str, Any]:
    """Default density (D-017): one room with no RDS value gets area x density rounded up, red text in the named style, and the note
    in Remarks; a room with an RDS value of zero stays zero. The recomputation must agree with the workbook."""
    import math
    d = copy.deepcopy(base)
    d["sources"].append(dict(id="OCC-T", document="ASHRAE Standard 62.1, Table 6.2.2.1", issue="test", page="Table 6.2.2.1", location="test row",
                             text_as_read="test density", method="test", confidence="high"))
    d["sources"].append(dict(id="RDS-T", document="RDS, test room", issue="test", page="occupancy field", location="test", text_as_read="Occupancy: n/a",
                             method="test", confidence="high"))
    r0, r1 = d["rooms"][0], d["rooms"][1]
    r0.update(occupancy_people=None, occupancy_per_100m2=7.0, occupancy_basis="DENSITY DEFAULT", occupancy_use="office", remark="test room")
    r0["prov"] = dict(r0["prov"], occupancy_density=["A", "OCC-T"])
    r0.pop("occupancy_people", None)
    r1.update(occupancy_people=0.0, occupancy_basis="RDS", occupancy_per_100m2=None)
    r1["prov"] = dict(r1["prov"], occupancy_people=["D", "RDS-T"])
    out = work / "occ.xlsx"
    ctx = build_workbook(d, out)
    rc = recalc(out, work / "rc")
    wb = openpyxl.load_workbook(rc, data_only=True)
    ri = wb["Room Information"]
    hdr = {h: i for i, h in enumerate(c.value for c in ri[5]) if h}
    fails: List[str] = []
    row0 = next(r for r in ri.iter_rows(min_row=7, values_only=True) if r[0] == r0["number"])
    row1 = next(r for r in ri.iter_rows(min_row=7, values_only=True) if r[0] == r1["number"])
    area0 = row0[hdr["Area used"]]
    want = math.ceil(round(area0 * 7.0 / 100.0, 9))
    if row0[hdr["Occupancy used"]] != want:
        fails.append(f"occupancy {row0[hdr['Occupancy used']]} expected {want} (area {area0})")
    if row1[hdr["Occupancy used"]] != 0:
        fails.append(f"RDS zero became {row1[hdr['Occupancy used']]}")
    note = str(row0[hdr["Remarks"]])
    if not note.startswith("Occupancy assumed: 7 persons / 100 m² ×") or "To confirm." not in note:
        fails.append(f"remarks note {note[:80]!r}")
    wf = openpyxl.load_workbook(out)
    cell = wf["Room Information"].cell(row=7, column=hdr["Occupancy used"] + 1)
    from .styles import OCC_NAME, OCC_STYLE
    if cell.style != OCC_NAME or (cell.font.color is None or cell.font.color.rgb != OCC_STYLE["font_colour"]):
        fails.append(f"occupancy cell style {cell.style}")
    for x in compare_recompute(d, rc):
        if not x["ok"]:
            fails.append(f"recomputation {x['item']}: {x['workbook']} vs {x['recomputed']}")
    sp = style_proof(ctx, out)
    fails += sp["problems"][:3]
    return dict(people=row0[hdr["Occupancy used"]], expected=want, note=note, fails=fails)


def run(work: Optional[Path] = None, report: Optional[Path] = None) -> int:
    root = Path(".").resolve()
    work = work or Path(tempfile.mkdtemp(prefix="wbself_"))
    work.mkdir(parents=True, exist_ok=True)
    results: Dict[str, Any] = {}
    fails: List[str] = []
    # (1) Rev A reproduction
    data = build_reva_input(root)
    (work / "reva_input.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    ctx = build_workbook(data, work / "reva_rebuild.xlsx")
    rc = recalc(work / "reva_rebuild.xlsx", work / "rc")
    cmp_ = compare_reva(rc, root)
    results["reva"] = cmp_
    for ln in cmp_["lines"]:
        if not ln["ok"]:
            fails.append(f"Rev A line {ln['item']}: {ln['diff_pct']:.3f} %")
    for rm in cmp_["rooms"]:
        for k, (a, b) in rm.items():
            if k != "zone" and abs((a or 0) - (b or 0)) > 1e-6 * max(1, abs(b or 0)):
                fails.append(f"Rev A room {rm['zone']} {k}: {a} vs {b}")
    # (2) known answers: in every workbook (Rev A rebuild) and through the node tables (KA department)
    ka_data = ka_input()
    kctx = build_workbook(ka_data, work / "ka.xlsx")
    krc = recalc(work / "ka.xlsx", work / "rc")
    ka = compare_ka(rc) + compare_ka(krc) + compare_ka_tables(krc)
    results["known_answers"] = ka
    for k in ka:
        if not k["ok"]:
            fails.append(f"known answer {k.get('case')} {k.get('check', '')}: {k['computed']} vs {k['expected']}")
    # (2b) independent recomputation against both recalculated workbooks (0.5 %)
    rec = [dict(x, workbook_name="Rev A rebuild") for x in compare_recompute(data, rc)] + \
          [dict(x, workbook_name="known-answer department") for x in compare_recompute(ka_data, krc)]
    results["recompute"] = rec
    for x in rec:
        if not x["ok"]:
            fails.append(f"recomputation {x['workbook_name']} {x['item']}: {x['workbook']:.3f} vs {x['recomputed']:.3f} ({x['diff_pct']:.2f} %)")
    # (3) error scan
    err = {"reva_rebuild": scan_errors(rc), "known_answer_dept": scan_errors(krc)}
    results["errors"] = err
    for k, v in err.items():
        if v:
            fails.append(f"{k}: {len(v)} error values, first {v[:3]}")
    fs = scan_formulas(work / "reva_rebuild.xlsx")
    results["formulas"] = {k: (v if k in ("n_formula", "funcs") else v[:10]) for k, v in fs.items()}
    results["formulas"]["n_literals"] = len(fs["literals"])
    for k in ("bad_funcs", "literals", "iferror", "external"):
        if fs[k]:
            fails.append(f"formula scan {k}: {fs[k][:5]}")
    # (4) style proof
    sp = style_proof(ctx, work / "reva_rebuild.xlsx")
    sp2 = style_proof(kctx, work / "ka.xlsx")
    results["style"] = dict(n_coded=sp["n_coded"] + sp2["n_coded"], problems=(sp["problems"] + sp2["problems"])[:20],
                            stray_font=(sp["stray_font"] + sp2["stray_font"])[:10], unnamed=(sp["unnamed"] + sp2["unnamed"])[:10],
                            merged=sp["merged"])
    for k in ("problems", "stray_font", "unnamed"):
        if results["style"][k]:
            fails.append(f"style proof {k}: {results['style'][k][:3]}")
    # (4b) deliverable language: forbidden terms and internal references in every cell text
    lang = {"reva_rebuild": language_scan(rc, root), "known_answer_dept": language_scan(krc, root)}
    results["language"] = {k: v[:20] for k, v in lang.items()}
    for k, v in lang.items():
        if v:
            fails.append(f"language scan {k}: {len(v)} hits, first {v[:3]}")
    # (4b2) Sources Method column, every string the generator writes (D-015)
    mc = {}
    for nm, path_ in (("reva_rebuild", rc), ("known_answer_dept", krc)):
        mc[nm] = method_column_scan(path_, root)
    gen_n, gen_hits = generator_text_scan(root)
    asm_hits: List[Tuple[str, str]] = []
    asm_n = 0
    if (root / "out" / "registers" / "dpa_rooms.csv").exists():
        from .assemble import assemble_department
        for dept in ("30", "41"):
            try:
                dd = assemble_department(dept, None, root)
            except (OSError, ValueError, KeyError):
                continue
            asm_n += 1
            asm_hits += scan_strings(dd, root, f"D{dept}")
    results["language_method"] = {k: dict(cells=v[0], hits=v[1][:10]) for k, v in mc.items()}
    results["language_generator"] = dict(strings=gen_n, hits=gen_hits[:20], assembled_inputs=asm_n, assembled_hits=asm_hits[:20])
    for k, (n_, h_) in mc.items():
        if h_:
            fails.append(f"Sources Method column {k}: {len(h_)} hits, first {h_[:3]}")
    if gen_hits:
        fails.append(f"generator text: {len(gen_hits)} hits, first {gen_hits[:3]}")
    if asm_hits:
        fails.append(f"assembled department input text: {len(asm_hits)} hits, first {asm_hits[:3]}")
    # (4c) tender airflow columns (D-013): a build with tender values, recalculated
    d13 = d013_check(data, work)
    results["d013"] = d13
    for f_ in d13["fails"]:
        fails.append(f"D-013 columns: {f_}")
    occ = occupancy_check(data, work)
    results["occupancy"] = occ
    for f_ in occ["fails"]:
        fails.append(f"occupancy default density: {f_}")
    # Checks status of the Rev A rebuild
    wbv = openpyxl.load_workbook(rc, data_only=True)
    chk = [(r[1], r[2], r[4]) for r in wbv["Checks"].iter_rows(min_row=7, values_only=True) if r[1] and r[4]]
    results["checks"] = chk
    for d, v, st in chk:
        if st == "FAIL":
            fails.append(f"workbook check FAIL: {d} = {v}")
    # (5) PDF
    pdf = pdf_proof(rc, work / "pdf")
    results["pdf"] = pdf
    if pdf["pages"] < len(ctx.wb.sheetnames) or not pdf["footer_found"]:
        fails.append(f"pdf proof: {pdf}")
    pp = print_proof(rc, Path(pdf["pdf"]))
    ppk = print_proof(krc, to_pdf(krc, work / "pdf_ka"))
    results["print_proof"] = dict(reva=pp, ka=ppk)
    for nm, x in (("Rev A rebuild", pp), ("known-answer department", ppk)):
        for pr in x["problems"]:
            fails.append(f"print proof {nm}: {pr}")
    results["fails"] = fails
    results["work"] = str(work)
    if report:
        report.write_text(json.dumps(results, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
    # print summary
    print("== Rev A reproduction (workbook vs Rev A cached values) ==")
    for ln in cmp_["lines"]:
        d = "" if ln["diff_pct"] is None else f"{ln['diff_pct']:+.3f} %"
        print(f"  {ln['item']:<62s} {ln['workbook']:>12.4f} {ln['reva']:>12.4f} {d:>10s} {'ok' if ln['ok'] else 'MISS'}")
    print(f"  outdoor air under the 8.2 rule: {cmp_['rule_oa']:.1f} L/s ({cmp_['crit_room']} fraction {cmp_['crit_frac']:.4f} x AHU supply); coil {cmp_['rule_coil']:.2f} kW")
    print("== Known answers ==")
    print(f"  {sum(1 for k in ka if k['ok'])} of {len(ka)} pass")
    print(f"== Independent recomputation: {sum(1 for x in rec if x['ok'])} of {len(rec)} lines within {TOL_PCT} %")
    print(f"== Errors: {sum(len(v) for v in err.values())}; formulas {fs['n_formula']}; typed literals {len(fs['literals'])}; IFERROR {len(fs['iferror'])}")
    print(f"== Style proof: {results['style']['n_coded']} coded cells, {len(results['style']['problems'])} problems")
    print(f"== Deliverable language: {sum(len(v) for v in lang.values())} hits (forbidden terms and internal references)")
    print(f"== Sources Method column: {sum(v[0] for v in mc.values())} cells scanned, {sum(len(v[1]) for v in mc.values())} hits; generator text: {gen_n} strings scanned, {len(gen_hits)} hits; "
          f"assembled department inputs: {asm_n} scanned, {len(asm_hits)} hits")
    print(f"== D-013 tender columns: {'ok' if not results['d013']['fails'] else results['d013']['fails']}; AHU remark: {results['d013']['ahu_remarks']}")
    print(f"== Default occupancy density: {'ok' if not results['occupancy']['fails'] else results['occupancy']['fails']}; note: {results['occupancy']['note'][:110]}")
    print(f"== PDF: {pdf['pages']} pages, footer found {pdf['footer_found']}")
    print(f"== Print proof: {len(pp['problems']) + len(ppk['problems'])} problems; lowest scale {min(v['scale'] for v in pp['sheets'].values()):.2f}")
    print("SELFTEST", "PASS" if not fails else "FAIL")
    for f in fails:
        print("  -", f)
    return 0 if not fails else 1
