"""Self test: Rev A reproduction, known-answer pressure cases, error scan, style proof, PDF export."""

from __future__ import annotations

import copy
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
from .layouts import AHU_KEYS, ENG
from .styles import CODE_ALLOWED

ERROR_VALUES = {"#N/A", "#NAME?", "#VALUE!", "#REF!", "#DIV/0!", "#NUM!", "#NULL!", "Err:502", "Err:504", "Err:508", "Err:511",
                "Err:522", "Err:523"}
ALLOWED_FUNCS = {"SUM", "SUMIFS", "SUMIF", "SUMPRODUCT", "COUNTIF", "COUNTIFS", "COUNT", "COUNTA", "IF", "AND", "OR", "MAX", "MIN", "ABS", "SIGN",
                 "SQRT", "ROUND", "ROUNDUP", "INDEX", "MATCH", "ISNUMBER", "ISERROR", "SEARCH", "ROW", "ROWS", "COLUMN", "_XLFN.MAXIFS"}
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
    """Department of three rooms carrying the known-answer suites of Section 8.3 through the real Pressure tables."""
    base = build_reva_input()
    src = dict(id="KA-8.3", document="Brief Section 8.3, known-answer table", issue="Rev D", page="8.3", location="known-answer test",
               text_as_read="room 1.88 x 2.94 x 3.0 m; room 3.97 x 4.83 x 3.0 m; ensuite 37.5 m2 surface; doors 15, 10, 20 cm2; shaft door 10.2 m3/h",
               method="transcribed from the brief", confidence="high")
    d = copy.deepcopy(base)
    d["sources"].append(src)
    d["department"] = dict(number="KA", name="Known-answer pressure suites (synthetic test)")
    d["references"] = []
    d["flags"] = []

    def room(no, name, area, perim, suite, pa, doors, override=None):
        return dict(number=no, name=name, storey="L01", ahu="AHU-KA", zone_tag=no, system_type="VAV", terminal_tag=None,
                    space_type="Pharmacy (OP)", room_condition="Comfort", soa_area_m2=area, area_selector="SOA", ceiling_height_m=3.0,
                    prov={"identity": ["D", "KA-8.3"], "zoning": ["D", "KA-8.3"], "space_type": ["D", "KA-8.3"], "soa_area": ["D", "KA-8.3"],
                          "ceiling_height": ["D", "KA-8.3"], "area_selector": ["S", "GEN-CONFIG"]},
                    criteria=dict(criteria_row="known-answer room", page="8.3", min_oa_ach=2, min_total_ach=6, pressure_regime="NEGATIVE",
                                  pressure_pa_tender=pa, air_class="1", single_pass="Y", source="KA-8.3", code="D"),
                    pressure=dict(mode="controlled", suite=suite, perimeter_m=perim, prov=["D", "KA-8.3"], surface_override_m2=override,
                                  surface_prov=["D", "KA-8.3"], doors=doors),
                    envelope=[])

    d["rooms"] = [
        room("ANTE", "Anteroom at -15 Pa", 1.88 * 2.94, 2 * (1.88 + 2.94), "S4", -15, [
            dict(id="D1", to="CORRIDOR", kind="hinged sealed door", seal="hinged", code="A", source="GEN-PRESSURE"),
            dict(id="D2", to="SHAFT", kind="sealed shaft door (fixed transfer into the room)", fixed_flow_m3h=-10.2, code="D", source="KA-8.3"),
            dict(id="D3", to="ROOM", kind="sliding sealed door", seal="sliding", code="A", source="GEN-PRESSURE")]),
        room("ROOM", "Isolation room at -30 Pa", 3.97 * 4.83, 2 * (3.97 + 4.83), "S5", -30, [
            dict(id="D4", to="CORRIDOR", kind="bed-width sealed door", seal="bed_width", code="A", source="GEN-PRESSURE"),
            dict(id="D5", to="ENSUITE", kind="sliding sealed door (internal)", seal="sliding", code="A", source="GEN-PRESSURE")]),
        room("ENSUITE", "Ensuite at -31 Pa", 4.0, 3.0, "S5", -31, [], override=37.5),
    ]
    d["pressure_nodes"] = [dict(tag="REF", description="Reference: corridor", pressure_pa=0, code="S", source="GEN-PRESSURE"),
                           dict(tag="CORRIDOR", description="Corridor outside the suite", pressure_pa=0, code="S", source="GEN-PRESSURE"),
                           dict(tag="SHAFT", description="Shaft", pressure_pa=0, code="D", source="KA-8.3")]
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
    add("Area of the supply rooms (m2)", pick("area"), hl["E3"].value)
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
        "return enthalpy is the supply-weighted mean of the room conditions (52.81 kJ/kg) where the example takes the first space type (52.6)")
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
    add("4 anteroom: envelope in (45.7)", g("ANTE", "Envelope in"), 45.7, 0.2)
    add("4 anteroom: offset needed", g("ANTE", "Offset needed"), 61.3, 0.2)
    add("4 anteroom: design offset", g("ANTE", "Design offset"), 99.9, 0.2)
    add("4 anteroom: ratio", g("ANTE", "Ratio design / needed"), 1.63, 0.01)
    add("5 suite: room envelope", g("ROOM", "Envelope in"), 163.5, 0.2)
    add("5 suite: ensuite envelope", g("ENSUITE", "Envelope in"), 68.7, 0.2)
    needed = g("ROOM", "Offset needed") + g("ENSUITE", "Offset needed")
    design = g("ROOM", "Design offset") + g("ENSUITE", "Design offset")
    add("5 suite: offset needed (room + ensuite)", needed, 273.5, 0.2)
    add("5 suite: design offset", design, 321.6, 0.2)
    add("5 suite: ratio", design / needed, 1.18, 0.01)
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
    print(f"== PDF: {pdf['pages']} pages, footer found {pdf['footer_found']}")
    print("SELFTEST", "PASS" if not fails else "FAIL")
    for f in fails:
        print("  -", f)
    return 0 if not fails else 1
