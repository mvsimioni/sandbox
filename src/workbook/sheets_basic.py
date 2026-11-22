"""Basis of Design, Sources, Flags, Checks and README."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, List

from openpyxl.styles import Alignment
from openpyxl.utils import get_column_letter

from .common import band_rows, cf_formula, cf_text, list_validation, title_block, write_headers
from .ctx import GROUPS, Ctx
from .model import Layout, blank
from .sheets_rooms import _prov_cells, _setup
from .styles import CODE_STYLES, FMT, put, link_to, setup_sheet

LISTS = OrderedDict([
    ("ListAreaSource", ["AUTO", "SOA", "MEASURED", "LARGER"]),
    ("ListSystemType", ["VAV", "CAV", "FCU", "MZ", "AHU", "PAHU", "FAN", "EXHAUST", "OTHER"]),
    ("ListRegime", ["POSITIVE", "NEGATIVE", "NEUTRAL"]),
    ("ListSeal", ["hinged", "sliding", "bed_width", "shaft"]),
    ("ListTerminal", ["VAV", "CAV", "VENTURI", "NONE"]),
    ("ListValveRole", ["SUPPLY", "EXHAUST"]),
    ("ListFanType", ["RETURN", "EXHAUST", "ISOLATION EXHAUST", "OUTDOOR AIR", "PAHU"]),
    ("ListFlagType", ["CONFIRM", "VERIFY", "ASSUMPTION"]),
    ("ListFlagStatus", ["OPEN", "CLOSED", "ACCEPTED"]),
    ("ListOACond", ["TREATED", "UNTREATED"]),
])
PRESSURE_METHOD = (
    "Project method for rooms with a pressure target (Marcus's method). Envelope leakage at a pressure difference: "
    "Q = limit × internal surface × (ΔP ÷ 50)^n, with the whole envelope leaking to the reference space (the corridor outside "
    "the suite at 0 Pa unless the documents name another). Door gaps: Q = 0.6 × A × √(2 ΔP ÷ 1.2) × 3600. Air flows from the higher "
    "to the lower pressure; for each space supply + air in = exhaust + air out. Offset needed: air in − air out for a negative room "
    "(extra exhaust), air out − air in for a positive room (extra supply). Design offset = limit × surface at the reference "
    "pressure (the top of the control valve's range). Ratio = design offset ÷ offset needed: below 1.0 fail; 1.0 to 1.2 VERIFY. "
    "Where a document in the corpus states a value it is cited; the other values are coded S, set by the engineer.")


def _tbl_name(ctx: Ctx, ws, name: str, col: str, r0: int, r1: int) -> None:
    ctx.define(name, f"'Basis of Design'!${col}${r0}:${col}${r1}")


def write_basis(ctx: Ctx) -> None:
    ws = ctx.wb["Basis of Design"]
    d = ctx.data
    for col, w in zip("ABCDEFGHIJ", (2, 50, 12, 34, 6, 15, 70, 3, 3, 3)):
        ws.column_dimensions[col].width = w
    put(ws, "B1", "Basis of Design", "Title")
    put(ws, "B2", f"{ctx.project}, {d['department'].get('number', '')} {d['department'].get('name', '')}. Document hierarchy, design conditions, constants, criteria origin, conventions, key flags", "Note")
    r = 4
    # 1 document hierarchy
    put(ws, f"B{r}", "1  Scope and document hierarchy", "Section")
    r += 1
    put(ws, f"B{r}", d.get("scope", "Independent check of the thermal load, supply airflow, outdoor air and pressure offset of the department."), "TextWrap")
    ws.merge_cells(f"B{r}:G{r}") if False else None
    r += 2
    for col, h in zip("BCDEFG", ("Document", "Issue", "Role", "Code", "Source ID", "What was taken from it")):
        put(ws, f"{col}{r}", h, "HeaderLeft")
    r += 1
    for doc in d.get("documents", []):
        put(ws, f"B{r}", doc["document"], "TextWrap")
        put(ws, f"C{r}", doc.get("issue"), "TextWrap")
        put(ws, f"D{r}", doc.get("role"), "TextWrap")
        put(ws, f"E{r}", "D", "Code")
        put(ws, f"F{r}", doc.get("source"), "Tag")
        if doc.get("source"):
            ctx.src_ranges.append(f"F{r}")
            ctx.src_link(ws, f"F{r}", doc["source"])
        put(ws, f"G{r}", doc.get("taken"), "TextWrap")
        r += 1
    r += 1
    # 2 constants
    put(ws, f"B{r}", "2  Design conditions and constants (named cells)", "Section")
    r += 1
    put(ws, f"B{r}", "Every constant below is a named cell; formulas use the name, never a typed number.", "Note")
    r += 1
    for col, h in zip("BCDEFG", ("Constant", "Value", "Unit (name)", "Code", "Source ID", "Remark")):
        put(ws, f"{col}{r}", h, "HeaderLeft")
    r += 1
    for gname, names in GROUPS:
        put(ws, f"B{r}", gname.upper(), "Unit")
        for c in "CDEFG":
            put(ws, f"{c}{r}", None, "Unit")
        r += 1
        for nm in names:
            c = ctx.constants.get(nm)
            if c is None:
                continue
            v = c.get("value")
            code, src = c.get("code", "S"), c.get("source", "")
            style = CODE_STYLES[code][0] if not blank(v) else "Assumption"
            put(ws, f"B{r}", c["label"], "Text")
            cell = put(ws, f"C{r}", v, style, "General")
            if isinstance(v, float) and 0.01 <= abs(v) < 1:
                cell.number_format = "0.00##"
            put(ws, f"D{r}", f"{c.get('unit', '')}  ({nm})", "Text")
            put(ws, f"E{r}", code, "Code")
            put(ws, f"F{r}", src, "Tag")
            ctx.src_ranges.append(f"F{r}")
            ctx.src_link(ws, f"F{r}", src)
            put(ws, f"G{r}", c.get("remark"), "Note")
            ctx.coded.append(dict(sheet=ws.title, cell=f"C{r}", code_cell=f"E{r}", src_cell=f"F{r}", code=code))
            if code == "A":
                ctx.assumptions.append(dict(sheet=ws.title, cell=f"C{r}", label=c["label"], value=v, room="", src=src))
            ctx.define(nm, f"'Basis of Design'!$C${r}")
            r += 1
    r += 1
    # 3 tables
    put(ws, f"B{r}", "3  Design tables", "Section")
    r += 2

    def table(title, heads, rows, names, fmts, code_key="code"):
        nonlocal r
        put(ws, f"B{r}", title, "Subhead")
        r += 1
        cols = "BCD"
        for col, h in zip("BCDEF", heads + ["Code", "Source ID"]):
            put(ws, f"{col}{r}", h, "HeaderLeft")
        r += 1
        r0 = r
        for row in rows:
            for j, (col, v) in enumerate(zip("BCD", row["vals"])):
                st = CODE_STYLES[row.get("code", "D")][0]
                c = put(ws, f"{col}{r}", v, st, fmts[j])
                if j == 0:
                    c.alignment = Alignment(horizontal="left", vertical="center")
                ctx.coded.append(dict(sheet=ws.title, cell=f"{col}{r}", code_cell=f"E{r}", src_cell=f"F{r}", code=row.get("code", "D")))
                if row.get("code") == "A" and j == 0:
                    ctx.assumptions.append(dict(sheet=ws.title, cell=f"{col}{r}", label=f"{title}: {row['vals'][0]}", value="row", room="", src=row.get("source")))
            put(ws, f"E{r}", row.get("code", "D"), "Code")
            put(ws, f"F{r}", row.get("source"), "Tag")
            ctx.src_ranges.append(f"F{r}")
            ctx.src_link(ws, f"F{r}", row.get("source"))
            r += 1
        r1 = max(r0, r - 1)
        for nm, col in zip(names, "BCD"):
            if nm:
                _tbl_name(ctx, ws, nm, col, r0, r1)
        r += 1

    d = ctx.data
    table("Space types: design room condition", ["Space type", "Room temperature (°C)", "Room enthalpy (kJ/kg)"],
          [dict(vals=[t["name"], t["room_t_c"], t["room_h_kjkg"]], code=t.get("code", "D"), source=t["source"]) for t in d["space_types"]],
          ["SpaceTypeName", "SpaceTypeT", "SpaceTypeH"], [None, "d1", "d1"])
    table("Activity: occupant heat gains", ["Activity", "Sensible (W per person)", "Latent (W per person)"],
          [dict(vals=[t["name"], t["sensible_w"], t["latent_w"]], code=t.get("code", "D"), source=t["source"]) for t in d["activities"]],
          ["ActName", "ActSensible", "ActLatent"], [None, "d0", "d0"])
    table("Orientation: indicative peak gains", ["Orientation", "Window (W/m²)", "Wall (W/m²)"],
          [dict(vals=[t["name"], t["window_w_m2"], t["wall_w_m2"]], code=t.get("code", "A"), source=t["source"]) for t in d["orientations"]],
          ["OriName", "OriWindow", "OriWall"], [None, "d0", "d0"])
    cond_rows = []
    for nm in ("Comfort", "Critical"):
        cond_rows.append(dict(vals=[nm, "=OutdoorDB", "=OutdoorH"], code="L", source=ctx.constants["OutdoorDB"]["source"]))
    table("Room condition: outdoor condition used", ["Room condition", "Outdoor dry bulb (°C)", "Outdoor enthalpy (kJ/kg)"], cond_rows,
          ["CondName", "CondDB", "CondH"], [None, "d1", "d1"])
    # space type defaults (reference)
    ctx.defaults_row = r
    put(ws, f"B{r}", "Space-type defaults (lighting, small power, density): used only where no document gives the value; all assumptions", "Text")
    r += 1
    for col, h in zip("BCDEFG", ("Space type", "Lighting (W/m²)", "Small power (W/m²)", "Code", "Source ID", "Occupancy density (persons / 100 m²)")):
        put(ws, f"{col}{r}", h, "HeaderLeft")
    r += 1
    for t in d.get("space_type_defaults", []):
        put(ws, f"B{r}", t["name"], "Assumption").alignment = Alignment(horizontal="left")
        put(ws, f"C{r}", t.get("lighting_w_m2"), "Assumption", "d1")
        put(ws, f"D{r}", t.get("small_power_w_m2"), "Assumption", "d1")
        put(ws, f"E{r}", "A", "Code")
        put(ws, f"F{r}", t["source"], "Tag")
        put(ws, f"G{r}", t.get("density_per_100m2"), "Assumption", "d1")
        ctx.src_ranges.append(f"F{r}")
        ctx.src_link(ws, f"F{r}", t["source"])
        for col in "BCDG":
            ctx.coded.append(dict(sheet=ws.title, cell=f"{col}{r}", code_cell=f"E{r}", src_cell=f"F{r}", code="A"))
        r += 1
    r += 1
    # 4 criteria origin and conventions
    put(ws, f"B{r}", "4  Where the criteria come from, and conventions", "Section")
    r += 1
    for text in d.get("criteria_origin", ["Room criteria: the criteria table of the department's DD Booklet; theatres in the MOT NSC: the MOT specification."]) + \
            [f"Safety factor on room loads: named cell SafetyFactor. Duct leakage on AHU supply airflow: named cell DuctLeakage.",
             "Airflow rule: both cases are calculated for every room (air changes × volume; room sensible load at the supply-to-room temperature "
             "difference); supply is the larger, not less than exhaust and pressure require; round once, at the end.",
             "AHU outdoor air = outdoor-air fraction of the highest-standard room on the AHU × AHU supply airflow, with duct leakage added to supply "
             "first. The plain sum and the multiple-space value of the example are shown beside it.",
             PRESSURE_METHOD]:
        c = put(ws, f"B{r}", text, "TextWrap")
        ws.merge_cells(f"B{r}:G{r}")
        ws.row_dimensions[r].height = 15 * (1 + len(text) // 150)
        r += 1
    r += 1
    ctx.bod_flags_row = r
    ctx.bod_next = r
    # lists, far right
    col0 = 12
    put(ws, f"{get_column_letter(col0)}3", "Lists for validation", "Section")
    for j, (nm, items) in enumerate(LISTS.items()):
        col = get_column_letter(col0 + j)
        put(ws, f"{col}4", nm, "Header")
        ws.column_dimensions[col].width = 18
        ws.column_dimensions[col].outline_level = 1   # validation lists: grouped and collapsed, outside the print area
        ws.column_dimensions[col].hidden = True
        for i, it in enumerate(items):
            put(ws, f"{col}{5 + i}", it, "Text")
        ctx.define(nm, f"'Basis of Design'!${col}$5:${col}${4 + len(items)}")
    for nm, key in (("ListSpaceType", "SpaceTypeName"), ("ListCondition", "CondName"), ("ListActivity", "ActName"),
                    ("ListOrientation", "OriName")):
        ctx.define(nm, ctx.names[key])


def write_basis_tail(ctx: Ctx) -> None:
    """Key flags and the assumptions register, after every other tab has registered its assumptions."""
    ws = ctx.wb["Basis of Design"]
    FL = ctx.lay["FL"]
    r = ctx.bod_next
    put(ws, f"B{r}", "5  Key flags", "Section")
    r += 1
    put(ws, f"B{r}", "Open flags by type (live from Flags)", "Text")
    r += 1
    for typ in ("CONFIRM", "VERIFY", "ASSUMPTION"):
        put(ws, f"B{r}", typ, "Tag")
        put(ws, f"C{r}", f'=COUNTIFS({FL.rng("type")},"{typ}",{FL.rng("status")},"OPEN")', "Link", "d0")
        r += 1
    for k in ctx.data.get("key_flags", []):
        put(ws, f"B{r}", k, "TextWrap")
        ws.merge_cells(f"B{r}:G{r}")
        ws.row_dimensions[r].height = 15 * (1 + len(k) // 150)
        r += 1
    r += 1
    put(ws, f"B{r}", "6  Assumptions to confirm (yellow cells)", "Section")
    r += 1
    for col, h in zip("BCDEFG", ("Item", "Value", "Where", "Code", "Source ID", "Room or tag")):
        put(ws, f"{col}{r}", h, "HeaderLeft")
    r += 1
    first = r
    for a in ctx.assumptions:
        put(ws, f"B{r}", a["label"], "Text")
        v = a["value"]
        put(ws, f"C{r}", v if isinstance(v, (int, float, str)) else str(v), "Text", "General").alignment = Alignment(horizontal="right")
        c = put(ws, f"D{r}", f"{a['sheet']}!{a['cell']}", "Tag")
        link_to(c, a["sheet"], a["cell"])
        put(ws, f"E{r}", "A", "Code")
        put(ws, f"F{r}", a.get("src"), "Tag")
        put(ws, f"G{r}", a.get("room"), "Tag")
        r += 1
    ctx.bod_last = r
    setup_sheet(ws, ws.title, rev=ctx.rev, date=ctx.date, project=ctx.project, print_rows="1:2", last_col=7, last_row=r,
                tab_colour="FF1F4E79", freeze="C4")


# --------------------------------------------------------------------------- #
def write_sources(ctx: Ctx) -> None:
    ws = ctx.wb["Sources"]
    L = ctx.lay["SO"]
    title_block(ws, "Sources", "One row per source ID: document, issue, page or sheet, location, text as read, method and confidence. "
                "Every input in the workbook links here.", ctx)
    heads = [("id", "Source ID", "", 13), ("doc", "Document", "", 44), ("issue", "Issue", "", 16), ("page", "Page or sheet", "", 14),
             ("loc", "Location on the page", "", 30), ("text", "Text as read", "", 60), ("method", "Method", "", 34), ("conf", "Confidence", "", 10)]
    write_headers(ws, L, heads, 4, {"id": "SOURCE"})
    for i, s in enumerate(ctx.sources):
        g = lambda k: L.local(k, i)
        put(ws, g("id"), s["id"], "Tag")
        for k, f in (("doc", "document"), ("issue", "issue"), ("page", "page"), ("loc", "location"), ("text", "text_as_read"),
                     ("method", "method"), ("conf", "confidence")):
            put(ws, g(k), s.get(f), "TextWrap" if k in ("doc", "loc", "text", "method") else "Text")
    band_rows(ws, f"A{L.first}:{L.col('conf')}{L.last}")
    _setup(ws, ctx, L, L.idx["conf"], L.last, "4:6", f"B{L.first}", "FF7F7F7F")


def build_flag_rows(ctx: Ctx) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for f in ctx.data.get("flags", []):
        rows.append(dict(type=f["type"], ref=f.get("ref", ""), desc=f["text"], status=f.get("status", "OPEN"), owner=f.get("owner", "Marcus"),
                         src=f.get("source", ""), link=f.get("link")))
    for gi, r in enumerate(ctx.rooms):
        n = r["number"]
        if blank(r.get("ahu")):
            rows.append(dict(type="CONFIRM", ref=n, desc=f"Room {n} {r.get('name', '')}: no AHU or system assigned (approved zoning not available).",
                             status="OPEN", owner="Zoning", src="", link=("Room Information", ctx.lay["RI"].local("ahu", gi))))
        if blank(r.get("terminal_tag")):
            rows.append(dict(type="VERIFY", ref=n, desc=f"Room {n} {r.get('name', '')}: no document seen shows its terminal type (VAV, CAV or venturi).",
                             status="OPEN", owner="Marcus", src="", link=("Room Information", ctx.lay["RI"].local("terminal", gi))))
        c = r.get("criteria") or {}
        if not c:
            rows.append(dict(type="CONFIRM", ref=n, desc=f"Room {n} {r.get('name', '')}: no criteria row mapped; criteria cells are blank.",
                             status="OPEN", owner="Marcus", src="", link=("Criteria", ctx.lay["CR"].local("crit", gi))))
        pr = r.get("pressure")
        has_pa = any(not blank(c.get(k)) for k in ("pressure_pa_booklet", "pressure_pa_tender", "pressure_pa_working"))
        if c.get("pressure_regime") in ("POSITIVE", "NEGATIVE") and not has_pa and not pr:
            rows.append(dict(type="VERIFY", ref=n, desc=f"Room {n} {r.get('name', '')}: pressure given as a direction only ({c['pressure_regime']}) and no "
                                                          "value on any drawing; not calculated on Pressure, listed for Marcus.",
                             status="OPEN", owner="Marcus", src=c.get("source", ""), link=("Criteria", ctx.lay["CR"].local("regime", gi))))
        for k, grp in (("soa_area_m2", "soa"), ("measured_area_m2", "meas")):
            if blank(r.get(k)) and k == "soa_area_m2":
                rows.append(dict(type="VERIFY", ref=n, desc=f"Room {n} {r.get('name', '')}: not found in the governing SOA; area from the measured layout only.",
                                 status="OPEN", owner="Marcus", src="", link=("Room Information", ctx.lay["RI"].local("soa", gi))))
        if blank(r.get("ceiling_height_m")):
            rows.append(dict(type="CONFIRM", ref=n, desc=f"Room {n} {r.get('name', '')}: ceiling height not found; volume and air-change airflow are zero until given.",
                             status="OPEN", owner="Marcus", src="", link=("Room Information", ctx.lay["RI"].local("h", gi))))
    for a in ctx.assumptions:
        if a["sheet"] == "Basis of Design" and a["room"] == "":
            pass
        rows.append(dict(type="ASSUMPTION", ref=f"{a['sheet']}!{a['cell']}", desc=f"{a['label']} = {a['value']}: assumed, to confirm.",
                         status="OPEN", owner="Marcus", src=a.get("src", ""), link=(a["sheet"], a["cell"])))
    return rows


def write_flags(ctx: Ctx, rows: List[Dict[str, Any]]) -> None:
    ws = ctx.wb["Flags"]
    L = ctx.lay["FL"]
    title_block(ws, "Flags", "Assumptions and open items, each CONFIRM, VERIFY or ASSUMPTION, with status. Change the status to CLOSED or "
                "ACCEPTED when resolved; the README counts the open ones.", ctx)
    heads = [("id", "Flag", "", 8), ("type", "Type", "", 12), ("ref", "Reference", "", 26), ("desc", "Description", "", 90),
             ("status", "Status", "", 11), ("owner", "Owner", "", 12), ("src", "Source ID", "", 12), ("note", "Note", "", 30)]
    write_headers(ws, L, heads, 4, {"id": "FLAG"})
    for i, f in enumerate(rows):
        g = lambda k: L.local(k, i)
        put(ws, g("id"), f"F-{i + 1:03d}", "Tag")
        put(ws, g("type"), f["type"], "Text")
        c = put(ws, g("ref"), f.get("ref"), "Tag")
        if f.get("link"):
            link_to(c, f["link"][0], f["link"][1])
        put(ws, g("desc"), f["desc"], "TextWrap")
        put(ws, g("status"), f.get("status", "OPEN"), "Text")
        put(ws, g("owner"), f.get("owner"), "Text")
        put(ws, g("src"), f.get("src"), "Tag")
        if f.get("src"):
            ctx.src_ranges.append(g("src"))
            ctx.src_link(ws, g("src"), f["src"])
        put(ws, g("note"), None, "Note")
    if ctx.n_flags:
        list_validation(ws, f"{L.col('type')}{L.first}:{L.col('type')}{L.last}", "ListFlagType")
        list_validation(ws, f"{L.col('status')}{L.first}:{L.col('status')}{L.last}", "ListFlagStatus")
        band_rows(ws, f"A{L.first}:{L.col('note')}{L.last}")
        cf_text(ws, f"{L.col('status')}{L.first}:{L.col('status')}{L.last}", f"{L.col('status')}{L.first}", "OPEN", bold=False, fill="FFFFF2CC")
    # live outdoor-air rule row
    AF = ctx.lay["AF"]
    r = L.last + 2
    put(ws, f"A{r}", "Live", "Tag")
    put(ws, f"D{r}", (f'="Outdoor-air rule: "&COUNTIF({AF.rng("status")},"SEPARATE")&" room(s) proposed for separation onto an FCU; "'
                      f'&COUNTIF({AF.rng("status")},"LIST FOR MARCUS")&" clinical room(s) listed for Marcus (see Airflow, last columns and the FCU tab)."'),
        "Calc").alignment = Alignment(horizontal="left")
    _setup(ws, ctx, L, L.idx["note"], r, "4:6", f"B{L.first}", "FFC00000")


# --------------------------------------------------------------------------- #
def write_checks(ctx: Ctx) -> None:
    ws = ctx.wb["Checks"]
    L = Layout("Checks", ["no", "desc", "value", "limit", "status", "note"], 7, 0)
    RI, CR, AF, PS, PU, FB, FB2, AH, FL = (ctx.lay[k] for k in ("RI", "CR", "AF", "PS", "PU", "FB", "FB2", "AH", "FL"))
    title_block(ws, "Checks", "Live reconciliations. PASS or FAIL are hard tests; VERIFY marks a finding to review; INFO is a count.", ctx)
    heads = [("no", "No.", "", 6), ("desc", "Check", "", 80), ("value", "Value", "", 12), ("limit", "Limit or expected", "", 14),
             ("status", "Status", "", 10), ("note", "Note", "", 60)]
    write_headers(ws, L, heads, 4, {"no": "RECONCILIATION"})
    nr = len(ctx.rooms)
    soa_sum = f"SUM({RI.rng('soa')})"
    err_ranges = []
    for st in ctx.storeys:
        E = ctx.eng_layouts[st]
        err_ranges.append(f"'{E.sheet}'!$A${E.first}:${E.col('frac')}${E.last}")
    for key in ("RI", "CR", "EN", "AF", "FB", "CP"):
        L_ = ctx.lay[key]
        err_ranges.append(f"'{L_.sheet}'!$A${L_.first}:${get_column_letter(L_.ncols)}${L_.last}")
    ps = ctx.lay["PS"]
    err_ranges.append(f"'Pressure'!$A${ps.first}:${get_column_letter(ps.ncols)}${ps.last}")
    pl = ctx.lay["PL"]
    err_ranges.append(f"'Pressure'!$A${pl.first}:${get_column_letter(pl.ncols)}${pl.last}")
    ah, ah2 = ctx.lay["AH"], ctx.lay["AH2"]
    err_ranges.append(f"'AHU'!$A${ah.first}:${get_column_letter(ah.ncols)}${ah2.last}")
    fc = ctx.lay["FC"]
    err_ranges.append(f"'FCU'!$A${fc.first}:${get_column_letter(fc.ncols)}${fc.last}")
    err = "+".join(f"SUMPRODUCT(--ISERROR({r}))" for r in err_ranges)
    # source resolution
    by_sheet: Dict[str, Dict[str, List[int]]] = {}
    from openpyxl.utils.cell import coordinate_from_string
    for ref in ctx.src_ranges:
        pass
    for c in ctx.coded:
        sc = c.get("src_cell")
        if sc:
            col, row = coordinate_from_string(sc)
            by_sheet.setdefault(c["sheet"], {}).setdefault(col, []).append(row)
    ids = ctx.lay["SO"].rng("id")
    src_terms = []
    for sh, cols in by_sheet.items():
        for col, rws in cols.items():
            rws = sorted(set(rws))
            runs, start, prev = [], rws[0], rws[0]
            for x in rws[1:]:
                if x != prev + 1:
                    runs.append((start, prev))
                    start = x
                prev = x
            runs.append((start, prev))
            for a, b in runs:
                rg = f"'{sh}'!${col}${a}:${col}${b}"
                src_terms.append(f"SUMPRODUCT(({rg}<>\"\")*(COUNTIF({ids},{rg})=0))")
    unresolved = "+".join(src_terms) if src_terms else "0"
    pa = ctx.lay["PS"]
    checks = [
        ("Total area used against the SOA or DPA total, within the tolerance", f"=SUM({RI.rng('area')})", f"={soa_sum}",
         '=IF(ABS(C{r}-D{r})<=MAX(AreaTolM2,AreaTolPct*D{r}),"PASS","VERIFY")', "m²"),
        ("Rooms whose SOA and measured areas disagree beyond tolerance", f'=COUNTIF({RI.rng("agree")},"NO")', 0, '=IF(C{r}=0,"PASS","VERIFY")', "list on Room Information"),
        ("Rooms whose final supply is below either airflow case or the pressure minimum", f'=COUNTIF({AF.rng("ok")},"FAIL")', 0, '=IF(C{r}=0,"PASS","FAIL")', ""),
        ("Rooms whose outdoor air is below the air-change minimum", f"=SUMPRODUCT(--({AF.rng('oa')}<{AF.rng('oa_air')}))", 0, '=IF(C{r}=0,"PASS","FAIL")', ""),
        ("Rooms whose outdoor air exceeds their supply", f"=SUMPRODUCT(--({AF.rng('oa')}>{AF.rng('supply')}))", 0, '=IF(C{r}=0,"PASS","FAIL")', "outdoor air is capped at supply; transfer air makes up the rest"),
        ("Pressure-controlled rooms with a ratio below 1.0", f'=COUNTIF({PS.rng("status")},"FAIL")', 0, '=IF(C{r}=0,"PASS","FAIL")', ""),
        ("Pressure-controlled rooms with a ratio from 1.0 to 1.2 (VERIFY)", f'=COUNTIF({PS.rng("status")},"VERIFY")', 0, '=IF(C{r}=0,"PASS","VERIFY")', ""),
        ("Free-pressure spaces whose balance does not close", f'=COUNTIF({PS.rng("status")},"UNSOLVED")', 0, '=IF(C{r}=0,"PASS","FAIL")', ""),
        ("Suites with a ratio below 1.0", f'=COUNTIF({PU.rng("status")},"FAIL")', 0, '=IF(C{r}=0,"PASS","FAIL")', ""),
        ("Known-answer pressure cases that miss their expected value", f'=COUNTIF({ctx.ka_status_range},"FAIL")', 0, '=IF(C{r}=0,"PASS","FAIL")', "Section 8.3, to 0.2 m³/h"),
        ("Room flow balances that do not close", f'=COUNTIF({FB.rng("status")},"FAIL")', 0, '=IF(C{r}=0,"PASS","FAIL")', ""),
        ("AHU balances that do not close", f'=COUNTIF({FB2.rng("status")},"FAIL")' if ctx.ahus else "=0", 0, '=IF(C{r}=0,"PASS","FAIL")', ""),
        ("AHU supply of rooms equals the Airflow total (no room with supply lacks an AHU)", f"=SUM({AH.rng('sa_room')})", f"=SUM({AF.rng('supply')})",
         '=IF(ABS(C{r}-D{r})<=BalTolLps,"PASS","FAIL")', "L/s"),
        ("Formula errors in the calculation tabs", f"={err}", 0, '=IF(C{r}=0,"PASS","FAIL")', "error values are never masked"),
        ("Source IDs that do not resolve on Sources", f"={unresolved}", 0, '=IF(C{r}=0,"PASS","FAIL")', ""),
        ("Rooms with a blank AHU, area or ceiling height", f'=SUMPRODUCT(({RI.rng("no")}<>"")*(({RI.rng("ahu")}="")+({RI.rng("area")}=0)+({RI.rng("h")}="")>0))', 0,
         '=IF(C{r}=0,"PASS","VERIFY")', "listed on Flags"),
        ("Rooms with no criteria row", f'=SUMPRODUCT(({CR.rng("no")}<>"")*({CR.rng("crit")}=""))', 0, '=IF(C{r}=0,"PASS","VERIFY")', "listed on Flags"),
        ("Rooms separated by the outdoor-air rule", f'=COUNTIF({AF.rng("status")},"SEPARATE")', "information", '="INFO"', "see FCU"),
        ("Clinical rooms listed for Marcus under the outdoor-air rule", f'=COUNTIF({AF.rng("status")},"LIST FOR MARCUS")', "information", '="INFO"', ""),
        ("Rooms the rule would still separate after four rounds", f"=SUM({AF.rng('trg4')})-SUM({AF.rng('lst4')})", 0, '=IF(C{r}<=0,"PASS","FAIL")', "rounds converged"),
        ("AHUs whose scheduled supply is below the calculated AHU supply", f'=SUMPRODUCT(({AH.rng("sched")}<>"")*({AH.rng("sched")}<{AH.rng("sa_ahu")}))', 0,
         '=IF(C{r}=0,"PASS","VERIFY")', ""),
        ("Open flags", f'=COUNTIF({FL.rng("status")},"OPEN")', "information", '="INFO"', "see Flags"),
    ]
    L.n = len(checks)
    for i, (desc, val, lim, st, note) in enumerate(checks):
        r = L.row(i)
        put(ws, f"A{r}", i + 1, "Text", "d0")
        put(ws, f"B{r}", desc, "Text")
        put(ws, f"C{r}", val, "Calc", "d1" if i == 0 else "d0")
        put(ws, f"D{r}", lim if isinstance(lim, str) and lim.startswith("=") else lim, "Calc", "d1" if i in (0, 12) else "d0")
        if isinstance(lim, str) and not lim.startswith("="):
            ws[f"D{r}"].alignment = Alignment(horizontal="right")
        put(ws, f"E{r}", st.replace("{r}", str(r)), "Check")
        put(ws, f"F{r}", note, "Note")
    # first check formula referenced E{r}="" harmlessly; overall
    last = L.last
    cf_text(ws, f"E{L.first}:E{last}", f"E{L.first}", "FAIL")
    cf_text(ws, f"E{L.first}:E{last}", f"E{L.first}", "VERIFY", fill="FFFFF2CC")
    r = last + 2
    put(ws, f"B{r}", "Overall", "Text")
    put(ws, f"E{r}", f'=IF(COUNTIF(E{L.first}:E{last},"FAIL")=0,"PASS","FAIL")', "Check")
    cf_text(ws, f"E{r}", f"E{r}", "FAIL")
    ctx.checks_status = f"'Checks'!$E${r}"
    ctx.checks_range = f"'Checks'!$E${L.first}:$E${last}"
    ctx.checks_layout = L
    ctx.checks_rows = (L.first, last)
    _setup(ws, ctx, L, 6, r, "4:6", f"C{L.first}", "FFC00000")


def write_readme(ctx: Ctx) -> None:
    ws = ctx.wb["README"]
    d = ctx.data
    for col, w in zip("ABCDEFG", (2, 24, 40, 14, 14, 12, 12)):
        ws.column_dimensions[col].width = w
    ws.column_dimensions["H"].width = 40
    put(ws, "B1", f"{d['department'].get('number', '')}  {d['department'].get('name', '')}", "Title")
    put(ws, "B2", f"{ctx.project}: thermal load, airflow and pressure workbook", "Note")
    r = 4
    put(ws, f"B{r}", "Purpose and scope", "Section")
    r += 1
    put(ws, f"B{r}", d.get("purpose", "Independent calculation of room loads, supply and outdoor airflow, pressure offsets and AHU totals for the "
                                      "department, on the NDH / Rev A engine, with every input traced to a source."), "TextWrap")
    ws.merge_cells(f"B{r}:H{r}")
    ws.row_dimensions[r].height = 42
    r += 2
    put(ws, f"B{r}", "Status (live)", "Section")
    r += 1
    FL = ctx.lay["FL"]
    for label, f in (("Checks", f'=IF(COUNTIF({ctx.checks_range},"FAIL")=0,"ALL CHECKS PASS",COUNTIF({ctx.checks_range},"FAIL")&" CHECK(S) FAIL")'),
                     ("Checks to verify", f'=COUNTIF({ctx.checks_range},"VERIFY")'),
                     ("Open flags", f'=COUNTIF({FL.rng("status")},"OPEN")'),
                     ("Rooms", f"=COUNTA({ctx.lay['RI'].rng('no')})")):
        put(ws, f"B{r}", label, "Text")
        put(ws, f"C{r}", f, "Link").alignment = Alignment(horizontal="left")
        r += 1
    r += 1
    put(ws, f"B{r}", "How to read this workbook", "Section")
    r += 1
    for t in ("Inputs are on Room Information, Criteria, Envelope, Pressure (doors) and the system tabs, each with a code and a source ID that "
              "links to Sources. Results follow by formula; no value is pasted and no number is typed inside a formula.",
              "The Room Zones tab of each storey is the load engine of the template. Airflow applies both cases to every room. Pressure "
              "calculates the offset of every room with a pressure target. AHU totalises per AHU. Checks reconciles everything.",
              "Unknown values are blank and listed on Flags. Yellow cells are assumptions to confirm."):
        put(ws, f"B{r}", t, "TextWrap")
        ws.merge_cells(f"B{r}:H{r}")
        ws.row_dimensions[r].height = 30
        r += 1
    r += 1
    put(ws, f"B{r}", "Colour code and cell code", "Section")
    r += 1
    for col, h in zip("BCDE", ("Code and named style", "Meaning", "Sample", "Appearance")):
        put(ws, f"{col}{r}", h, "HeaderLeft")
    r += 1
    appearance = {"D": "blue text", "M": "blue text", "S": "blue text on light grey", "A": "blue text on light yellow", "C": "black text",
                  "L": "green text", "R": "black, bold, hairline rule above", "K": "pass plain; fail bold on light red"}
    for code, (style, meaning) in CODE_STYLES.items():
        put(ws, f"B{r}", f"{code}   {style}" + ("  /  Total" if code == "R" else ""), "Tag")
        put(ws, f"C{r}", meaning, "Text")
        sample = put(ws, f"D{r}", 123.4 if code not in ("K",) else "PASS", style, "d1")
        put(ws, f"E{r}", appearance[code], "Text")
        r += 1
    put(ws, f"B{r}", "Notes are grey italic at 8 pt. Red is kept for failed checks and links to another file (none).", "Note")
    r += 2
    put(ws, f"B{r}", "Tab map", "Section")
    r += 1
    tabs = [("Basis of Design", "Document hierarchy, design conditions and named constants, criteria origin, conventions, key flags"),
            ("Room Information", "One row per room: inputs with code, source ID and remark"),
            ("Criteria", "Criteria row applied to each room, with its source"),
            ("Envelope", "Façade segments per room, summed by the engine")]
    tabs += [(ctx.sheet_for(s), f"Load engine of the template, {ctx.label(s)}") for s in ctx.storeys]
    tabs += [("Airflow", "Both airflow cases, governing case, exhaust, return, outdoor air, outdoor-air rule"),
             ("Pressure", "Pressure-offset method, suites, sensitivities and the known-answer test"),
             ("AHU", "Airflow totalisation, outdoor air, coil duty"), ("FCU", "Rooms on fan coil units, including separated rooms"),
             ("Fans", "Return, exhaust and outdoor-air fan duties"), ("VAV", "Terminals: maximum, minimum, turndown"),
             ("Venturi Valves", "Valve ranges from Pressure"), ("Flow Balance", "Air in against air out"),
             ("Airflow Comparison", "Calculated airflows against schematics and references"), ("Checks", "Live reconciliations"),
             ("Sources", "Source register"), ("Flags", "Assumptions and open items")]
    for name, desc in tabs:
        c = put(ws, f"B{r}", name, "Text")
        link_to(c, name, "A1")
        put(ws, f"C{r}", desc, "Text")
        r += 1
    r += 1
    put(ws, f"B{r}", "Units and abbreviations", "Section")
    r += 1
    for t in ("Airflow in L/s (m³/h on Pressure); pressure in Pa; power in kW (W in the engine); area m²; volume m³; temperature °C; "
              "enthalpy kJ/kg. ACH air changes per hour. AHU air handling unit; PAHU primary AHU; FCU fan coil unit; VAV variable air volume; "
              "OA outdoor air; SOA schedule of accommodation; DPA departmental planning.",):
        put(ws, f"B{r}", t, "TextWrap")
        ws.merge_cells(f"B{r}:H{r}")
        ws.row_dimensions[r].height = 42
    r += 2
    put(ws, f"B{r}", "Revision", "Section")
    r += 1
    for col, h in zip("BCDEFG", ("Rev", "Date", "Description", "By", "Chk'd", "App'd")):
        put(ws, f"{col}{r}", h, "HeaderLeft")
    r += 1
    rev = d.get("project", {}).get("revision", {})
    for rv in d.get("revisions", [rev]):
        for col, k in zip("BCDEFG", ("rev", "date", "description", "by", "checked", "approved")):
            put(ws, f"{col}{r}", rv.get(k), "Text")
        r += 1
    setup_sheet(ws, "README", rev=ctx.rev, date=ctx.date, project=ctx.project, print_rows="1:2", last_col=8, last_row=r, tab_colour="FF1F4E79")
