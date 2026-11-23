"""Basis of Design, Sources, Flags, Checks and README."""

from __future__ import annotations

import re

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
    ("ListAreaSource", ["AUTO", "DPA", "SOA", "MEASURED", "LARGER"]),
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
DOOR_CONSTANTS = {"DoorCd", "GapSides", "GapBottom", "GapHinged", "GapSliding", "GapBedWidth", "GapShaft", "SecPerHour", "CM2perM2"}
PRESSURE_METHOD = (
    "Pressure method for every room with a pressure target in Pa other than 0 Pa (a neutral room has no offset). The whole envelope complies with the airtightness limit of 2.5 m³/h per m² of internal surface at 50 Pa. "
    "Internal surface = 2 × floor area + perimeter × ceiling height. Doors and windows are part of the surface. Door gaps are not modelled. "
    "Envelope leakage at the set pressure: Q = limit × internal surface × (ΔP ÷ 50)^n, with n = 0.65. The sensitivity cases use n = 0.50 and n = 0.75. "
    "The reference space is the corridor outside the room at 0 Pa. Air flows from the higher to the lower pressure. "
    "Offset needed for a negative room (extra exhaust) = envelope leakage in at the set pressure. Offset needed for a positive room (extra supply) = envelope leakage out at the set pressure. "
    "Design offset = 2.5 × internal surface (the limit at the reference pressure). "
    "Ratio used ÷ needed = the offset that the Airflow tab carries (supply minus extract) ÷ the offset needed at the set pressure. A ratio below 1.0 is a fail. "
    "Ratio design ÷ needed = design offset ÷ offset needed. A design ratio from 1.0 to 1.2 is VERIFY. "
    "The perimeter comes from the measured plan room where the room is linked to one. Otherwise the perimeter is 4 × √area, assumed (code A) and flagged. "
    "A cited document gives each stated value. The engineer sets the other values (code S).")


def _value_fmt(v) -> str:
    """Number format of one constant: whole numbers with thousands separator, otherwise the decimals the value needs (at most 4);
    true minus sign and a dash for zero."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return "General"
    if float(v) == int(v) and abs(v) < 1e12:
        d = 0
    else:
        txt = f"{float(v):.6f}".rstrip("0")
        d = min(max(len(txt.split(".")[1]), 1), 4)
    body = "#,##0" + ("." + "0" * d if d else "")
    return f'{body};"−"{body};"–"'


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
            if c is None or (nm in DOOR_CONSTANTS and not ctx.pl_links):       # door gaps are not modelled: their constants are defined only when a path table exists
                continue
            v = c.get("value")
            code, src = c.get("code", "S"), c.get("source", "")
            style = CODE_STYLES[code][0] if not blank(v) else "Assumption"
            put(ws, f"B{r}", c["label"], "Text")
            cell = put(ws, f"C{r}", v, style, _value_fmt(v))
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
                    ctx.assumptions.append(dict(sheet=ws.title, cell=f"{col}{r}", label=f"{title}: {row['vals'][0]}", room="", src=row.get("source"),
                                            value=", ".join(f"{h} {x}" for h, x in zip(heads[1:], row["vals"][1:]))))
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
             "Airflow rule: calculate both cases for every room. Case one is air changes × volume. Case two is the room sensible load at the "
             "supply-to-room temperature difference. Supply is the larger case, not less than exhaust and pressure require. Round once, at the end.",
             "AHU outdoor air = outdoor-air fraction of the highest-standard room on the AHU × AHU supply airflow. Add duct leakage to the supply first. "
             "The plain sum and the multiple-space value of the example are shown beside it.",
             "Outdoor air is capped at room supply. The outdoor air of a room is the highest of its air-change value, its value per person times the occupancy, its value per area times the area, and the specified value. It is a floor of the room supply. Transfer air from the adjacent spaces makes up any exhaust or pressure need beyond the supply. "
             "Areas are taken to 0.1 m² (named cell AreaDecimals). Airflows are calculated unrounded and rounded once at the end.",
             "Coil duty keeps the method of the Rev A AHU Summary. The outdoor-air load takes the room condition of the first supply room of the AHU, in room order. "
             "The example's own basis is shown beside the rule-based duty. That basis is the maximum of the sum and the multiple-space value. "
             "No change of method has been approved.",
             "Tender airflow (Equipment Schedule Corr 5; for the MOT NSC units the MOT NSC Overall AC Equipment Schedule Corr 2; else the tender air-side schematic) is information only. It is never a target or a limit. "
             "Reduction = tender − calculated. Reduction (%) = (tender − calculated) ÷ tender. A calculated airflow above tender is marked VERIFY. "
             "A unit states a reduction only where the calculated airflow is below tender and the room list of the unit is complete. The room list is complete when the counted rooms "
             "with a criteria row and a supply airflow are at least as many as the rooms that the zone schedule gives the unit, no counted room lacks a criteria row, and the counted area is "
             "at least the share ZoneCoverMin of the zoned area, and no other department has a room on the unit (a unit that several departments share is judged on the AHU master summary). The Room list column gives the count of rooms and, when the list is not complete, the reason. In every other case the Remarks cell says nothing about a reduction. "
             "A calculated airflow above tender, and a calculated outdoor air above the tender outdoor airflow, are marked VERIFY on every unit. The other departments on the unit are counted on the AHU master summary.",
             "Occupancy comes from the RDS. The default density by room use applies only where the RDS field is empty or the room has no RDS. "
             "It is rounded up to a whole person (red text, code A).",
             "Lighting and small power (plug load) come from a project document where one gives them. No project document does, so the ASHRAE value for the room use applies "
             "(plug load: ASHRAE Handbook, Fundamentals, plug-load densities; lighting: ASHRAE 90.1 space-by-space lighting power density). The cells are red text (code A) and the Remarks column "
             "gives the value and the room use. Equipment gain is a provisional value in W/m² for the rooms led by equipment (red cells, VERIFY flag, vendor data needed) and 0 W for other rooms. Envelope gain is assessed by the simple facade method only for the rooms that have a plan room link (see Envelope. Coverage); the other rooms are treated as internal and get no envelope gain.",
             "A room takes the row of its own department booklet where that booklet prints the same space (the same name, or the same name with a level or department prefix or a number in brackets; "
             "the Remarks say which). A room with no such row takes the row of the same space in the closest other department (same room type code, then same name, then same use class and the same words in the room name before its qualifier, so that one shared word is not enough; a row at negative pressure or on single pass is taken only for a room whose name says that it is a toilet, a dirty space or an isolation space; "
             "clinical rooms prefer clinical departments). The cells of such a presumed row are italic green (code A). The booklet and the page of the row stay as the source, and the Remarks column says 'Presumed criteria'. "
             "A room that no other department covers has no criteria row and is listed on Flags with the reason.",
             "Area used (selector AUTO, the default): the design area is the DPA designed area, else the SOA area. "
             "It is used where no area is measured, or where the measured area agrees with it within the tolerance (named cells AreaTolPct and AreaTolM2). "
             "The measured area is used where the two disagree. A measured area comes only from a plan room that matches the room by name, storey and grid span. "
             "Its area must be within a factor of two of the design area. Each room where the measured area replaces the design area has its own row on Flags. "
             "The selectors SOA, DPA, MEASURED and LARGER override this. Areas are rounded to 0.1 m².",
             "Rooms fixed by the MOT specification (code S on their criteria) keep their specified supply, outdoor air and exhaust. They are not resized. "
             "Airflow and Pressure show the offset needed beside the specified one.",
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


CALC_RULES = [
    "Calculation rules. Supply of a room: the largest of the thermal-load airflow, the air-change airflow (the outdoor air per person or per area is a floor on the supply) and, only where "
    "the room has a pressure offset, the exhaust and offset air. A room with no pressure offset has no supply floor from exhaust: the exhaust is made up by transfer air.",
    "Single-pass rooms. A room that needs 100 % outdoor air for clean or sterile supply (isolation, protective, theatre, clean rooms) sets the outdoor-air fraction of its unit. "
    "A dirty room (dirty utility, disposal, soiled, decontamination, toilets) takes recirculated supply from the unit and a dedicated exhaust with no return, and does not set the fraction. "
    "The class of each room is an assumption, to confirm. The dirty rooms of each unit are listed on the AHU tab.",
    "AHU balance. Supply = room supply × (1 + duct leakage). Outdoor air = the larger of (the highest room fraction × supply) and (the air that leaves the rooms: exhaust with no return, "
    "net offset air through the envelope, spill air to plant rooms). Recirculated air = supply − outdoor air. Spill or relief = outdoor air − exhaust − net envelope leakage.",
    "Duct leakage is 10 % on an AHU and on a ducted fan coil unit, and 0 % on a hi-wall fan coil unit (assumption, to confirm).",
    "Design room condition. The printed set point where the row has one. Otherwise 24 °C limited to the printed temperature range: 24 °C inside the range, the nearest limit outside it. "
    "Design humidity is the smaller of 60 % and the printed maximum. Rooms with no printed temperature keep the condition of their space type. The thermal airflow at the lower limit of "
    "the range is shown for information only.",
    "Ceiling height. Where a room layout sheet prints two or more heights, the highest is used.",
    "Operating theatres with a laminar-flow ceiling module: room supply = HEPA face area × 0.35 m/s (the Max Face Velocity of the HEPA schedule). The unit is checked at 0.40 m/s plus duct leakage. "
    "Cath lab and angiography rooms keep the airflow printed by the MOT NSC. No reduction against tender is stated for an MOT unit; MOT equipment is not resized.",
    "Envelope. ETTV component method, simple facade method. Gain per m² of wall = 12 × U. Gain per m² of glass = 3.4 × U + 211 × CF × SC. These are the ETTV equivalents, an averaged basis. "
    "Areas = length of the room edges on the storey outline × storey height, split by the window-to-wall ratio of the ETTV report for each facade. "
    "The areas are not read from the building elevations opening by opening. A room is external only where an edge of its outline lies on the storey outline. Flagged for human check. "
    "COVERAGE: envelope rows exist only for a room that has a one-to-one plan polygon (or a typical-room link by name and count) on a storey with a facade. Every other room, a perimeter ward or office with no such link included, gets no envelope gain in this issue: the gain is not assessed for it. The README shows the live count. "
    "Where the thermal case governs a room with an envelope, or is within 20 % of the air-change case, the room is marked VERIFY for a peak solar check.",
    "Equipment gain. Provisional W/m² by room use for equipment-led rooms (red cells and Remarks), until vendor data is available. Lighting and small power are ASHRAE values (red cells).",
    "Criteria with no booklet row take the HFDS Mechanical reverse-brief row (table and page are on the room). A mapped or presumed booklet row is compared with the HFDS row of the same room type. "
    "A difference raises VERIFY, with both values, and the booklet row governs.",
    "A reduction against tender is stated only where the room list of the unit is complete (counted area 0.9 to 1.1 of the zoned area), the room table of the air-side schematic agrees by count, "
    "and the tender base passes the schedule test (schedule and schematic within 10 %, no placeholder value).",
]


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
    for k in list(ctx.data.get("key_flags", [])) + CALC_RULES:
        put(ws, f"B{r}", k, "TextWrap")
        ws.merge_cells(f"B{r}:G{r}")
        ws.row_dimensions[r].height = 15 * (1 + len(k) // 150)
        r += 1
    r += 1
    put(ws, f"B{r}", "6  Assumptions on constants and tables (yellow cells)", "Section")
    r += 1
    put(ws, f"B{r}", "Assumptions about single rooms are on Flags, one row for each room.", "Note")
    r += 1
    for col, h in zip("BCDEF", ("Item", "Value", "Section", "Code", "Source ID")):
        put(ws, f"{col}{r}", h, "HeaderLeft")
    r += 1
    for a in ctx.assumptions:
        if a["sheet"] != "Basis of Design":
            continue
        put(ws, f"B{r}", str(a["label"]).replace("_", " "), "Text")
        v = a["value"]
        if v is None:
            put(ws, f"C{r}", None, "Text")
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            put(ws, f"C{r}", v, "Calc", _value_fmt(v))
        else:
            put(ws, f"C{r}", str(v), "Text")
            ws[f"C{r}"].alignment = Alignment(horizontal="left", vertical="center")
        c = put(ws, f"D{r}", "Design constants" if ":" not in str(a["label"]) else "Design tables", "Text")
        link_to(c, a["sheet"], a["cell"])
        put(ws, f"E{r}", "A", "Code")
        put(ws, f"F{r}", a.get("src"), "Tag")
        if a.get("src"):
            ctx.src_ranges.append(f"F{r}")
            ctx.src_link(ws, f"F{r}", a["src"])
        r += 1
    ctx.bod_last = r
    setup_sheet(ws, ws.title, rev=ctx.rev, date=ctx.date, project=ctx.project, print_rows="1:2", last_col=7, last_row=r,
                tab_colour="FF1F4E79")


# --------------------------------------------------------------------------- #
def write_sources(ctx: Ctx) -> None:
    ws = ctx.wb["Sources"]
    L = ctx.lay["SO"]
    title_block(ws, "Sources", "One row per source ID: document, issue, page or sheet, location, text as read, method and confidence. "
                "Every input in the workbook links here.", ctx)
    heads = [("id", "Source ID", "", 13), ("doc", "Document", "", 44), ("issue", "Issue", "", 22), ("page", "Page or sheet", "", 22),
             ("loc", "Location on the page", "", 30), ("text", "Text as read", "", 60), ("method", "Method", "", 34), ("conf", "Confidence", "", 13)]
    write_headers(ws, L, heads, 4, {"id": "SOURCE"})
    for i, s in enumerate(ctx.sources):
        g = lambda k: L.local(k, i)
        put(ws, g("id"), s["id"], "Tag")
        for k, f in (("doc", "document"), ("issue", "issue"), ("page", "page"), ("loc", "location"), ("text", "text_as_read"),
                     ("method", "method"), ("conf", "confidence")):
            put(ws, g(k), s.get(f), "TextWrap" if k in ("doc", "issue", "page", "loc", "text", "method") else "Text")
    band_rows(ws, f"A{L.first}:{L.col('conf')}{L.last}")
    _setup(ws, ctx, L, L.idx["conf"], L.last, "4:6", f"B{L.first}", "FF7F7F7F")


def build_flag_rows(ctx: Ctx) -> List[Dict[str, Any]]:
    """Flags of the workbook. One flag for each gap of each room, door, criteria row or AHU (never one per field); no duplicates."""
    rows: List[Dict[str, Any]] = []
    seen = set()

    def add(**kw) -> None:
        kw["desc"] = re.sub(r"\s*\(\s*\)", "", kw["desc"])          # no empty brackets
        d0 = kw["desc"]
        first = re.split(r"(?<=[.!?])\s", d0, maxsplit=1)[0]
        if len(first.split()) > 25 and ": " in d0 and d0.startswith("Room "):
            # a long room name makes the lead-in a long sentence: end the lead-in with a full stop
            head, tail = d0.split(": ", 1)
            kw["desc"] = f"{head}. {tail[:1].upper()}{tail[1:]}"
        key = (kw["type"], kw.get("ref", ""), kw["desc"])
        if key not in seen:
            seen.add(key)
            rows.append(kw)

    for f in ctx.data.get("flags", []):
        add(type=f["type"], ref=f.get("ref", ""), desc=f["text"], status=f.get("status", "OPEN"), owner=f.get("owner", "ACMV engineer"),
            src=f.get("source", ""), link=f.get("link"))
    RI, CR = ctx.lay["RI"], ctx.lay["CR"]
    for gi, r in enumerate(ctx.rooms):
        n = r["number"]
        who = f"Room {n} {r.get('name', '')}".strip()
        gaps = r.get("gaps") or {}

        def note(cat: str) -> str:
            return (" " + str(gaps[cat])) if gaps.get(cat) else ""
        plan_gap = gaps.get("plan")
        if blank(r.get("ahu")):
            if plan_gap:
                # one cause, one flag: with no plan room, neither the AHU or system nor the measured area can be set
                add(type="CONFIRM", ref=n, desc=f"{who}: no AHU or system is set and the measured area is blank. {plan_gap}", status="OPEN", owner="Zoning", src="",
                    link=("Room Information", RI.local("ahu", gi)))
            else:
                add(type="CONFIRM", ref=n, desc=f"{who}: no AHU or system is set.{note('ahu')}", status="OPEN", owner="Zoning", src="",
                    link=("Room Information", RI.local("ahu", gi)))
        elif plan_gap:
            add(type="VERIFY", ref=n, desc=f"{who}: {plan_gap}", status="OPEN", owner="Zoning", src="", link=("Room Information", RI.local("meas", gi)))
        if blank(r.get("terminal_tag")):
            add(type="VERIFY", ref=n, desc=f"{who}: no document shows its terminal type (VAV, CAV or venturi).{note('terminal')}",
                status="OPEN", owner="ACMV engineer", src="", link=("Room Information", RI.local("terminal", gi)))
        c = r.get("criteria") or {}
        if not c:
            add(type="VERIFY" if str(gaps.get("criteria", "")).startswith("Plant space") else "CONFIRM", ref=n,
                desc=f"{who}: no criteria row is set; the criteria cells are blank.{note('criteria')}",
                status="OPEN", owner="ACMV engineer", src="", link=("Criteria", CR.local("crit", gi)))
        elif gaps.get("criteria"):
            add(type="VERIFY", ref=n, desc=f"{who}: criteria row {c.get('criteria_row', '')}.{note('criteria')}", status="OPEN",
                owner="ACMV engineer", src=c.get("source", ""), link=("Criteria", CR.local("crit", gi)))
        pr = r.get("pressure")
        has_pa = any(not blank(c.get(k)) for k in ("pressure_pa_booklet", "pressure_pa_tender", "pressure_pa_working"))
        if c.get("pressure_regime") in ("POSITIVE", "NEGATIVE") and not has_pa and not pr:
            add(type="VERIFY", ref=n, desc=f"{who}: pressure is a direction only ({c['pressure_regime']}) and no drawing gives a value. "
                                           "Not calculated on Pressure.", status="OPEN", owner="ACMV engineer", src=c.get("source", ""),
                link=("Criteria", CR.local("regime", gi)))
        if gaps.get("door"):
            add(type="VERIFY", ref=n, desc=f"{who}: {gaps['door']}", status="OPEN", owner="ACMV engineer", src=c.get("source", ""),
                link=("Criteria", CR.local("regime", gi)))
        if blank(r.get("soa_area_m2")) and blank(r.get("dpa_area_m2")):
            add(type="VERIFY", ref=n, desc=(f"{who}: no area in the DPA or the SOA. The measured area is used.{note('area')}" if not blank(r.get("measured_area_m2"))
                                           else f"{who}: no area in the DPA, the SOA or a plan room. The area is 0 m², so every airflow that depends on it is 0.{note('area')}"),
                status="OPEN", owner="ACMV engineer", src="", link=("Room Information", RI.local("soa", gi)))
        elif gaps.get("area"):
            add(type="VERIFY", ref=n, desc=f"{who}: {gaps['area']}", status="OPEN", owner="ACMV engineer", src="",
                link=("Room Information", RI.local("meas", gi)))
        base_a = r.get("dpa_area_m2") if not blank(r.get("dpa_area_m2")) and r.get("dpa_area_m2") > 0 else r.get("soa_area_m2")
        if gaps.get("area_diff"):
            add(type="VERIFY", ref=n, desc=f"{who}: {gaps['area_diff']}", status="OPEN", owner="ACMV engineer", src="",
                link=("Room Information", RI.local("meas", gi)))
        elif (not blank(r.get("measured_area_m2")) and not blank(base_a) and base_a > 0 and r.get("area_selector") in (None, "", "AUTO")
              and abs(base_a - r["measured_area_m2"]) > max(ctx.constants["AreaTolM2"]["value"], ctx.constants["AreaTolPct"]["value"] * base_a)):
            add(type="VERIFY", ref=n, desc=(f"{who}: the measured area {r['measured_area_m2']:.1f} m² differs from the design area {base_a:.1f} m² "
                                           f"({(r['measured_area_m2'] - base_a) / base_a * 100:+.0f} %), outside the tolerance. The area used is the measured area "
                                           "(selector AUTO). Confirm which area is right."), status="OPEN", owner="ACMV engineer", src="",
                link=("Room Information", RI.local("meas", gi)))
        if gaps.get("rds_code"):
            add(type="VERIFY", ref=n, desc=f"{who}: {gaps['rds_code']}", status="OPEN", owner="ACMV engineer", src="",
                link=("Room Information", RI.local("pfix", gi)))
        if gaps.get("occupancy_note"):
            add(type="VERIFY", ref=n, desc=f"{who}: {gaps['occupancy_note']}", status="OPEN", owner="ACMV engineer", src="",
                link=("Room Information", RI.local("pfix", gi)))
        if gaps.get("number"):
            add(type="VERIFY", ref=n, desc=f"{who}: {gaps['number']}", status="OPEN", owner="ACMV engineer", src="", link=("Room Information", RI.local("pfix", gi)))
        if gaps.get("oa_person"):
            add(type="VERIFY", ref=n, desc=f"{who}: {gaps['oa_person']}", status="OPEN", owner="ACMV engineer", src=c.get("source", ""),
                link=("Room Information", RI.local("pfix", gi)))
        if blank(r.get("ceiling_height_m")):
            add(type="CONFIRM", ref=n, desc=f"{who}: ceiling height is not set. Volume and air-change airflow are zero until it is given.{note('height')}",
                status="OPEN", owner="ACMV engineer", src="", link=("Room Information", RI.local("h", gi)))
        if gaps.get("loads"):
            add(type="VERIFY", ref=n, desc=f"{who}: {gaps['loads']}", status="OPEN", owner="ACMV engineer", src="", link=("Room Information", RI.local("light", gi)))
        if gaps.get("perimeter"):
            add(type="ASSUMPTION", ref=n, desc=f"{who}: {gaps['perimeter']}", status="OPEN", owner="ACMV engineer", src="GEN-PRESSURE",
                link=("Pressure", ctx.lay["PS"].local("perim", ctx.ps_index[n]["index"]) if n in ctx.ps_index else "A1"))
        if gaps.get("equipment"):
            add(type="VERIFY", ref=n, desc=f"{who}: {gaps['equipment']}", status="OPEN", owner="ACMV engineer", src="", link=("Room Information", RI.local("eqs", gi)))
        if gaps.get("occupancy"):
            add(type="VERIFY", ref=n, desc=f"{who}: {gaps['occupancy']}", status="OPEN", owner="ACMV engineer", src="",
                link=("Room Information", RI.local("pfix", gi)))
    for a in ctx.ahus:
        if blank(a.get("sched_supply_lps")):
            add(type="VERIFY", ref=a["tag"], desc=f"AHU {a['tag']}: no tender airflow is set (no equipment schedule row and no tender schematic figure).",
                status="OPEN", owner="ACMV engineer", src="", link=("AHU", ctx.lay["AH"].local("sched", ctx.ahus.index(a))))
    # one flag per design constant or table row, and one per room (or per room and sheet) for the room-level inputs
    grouped: Dict[Any, List[Dict[str, Any]]] = {}
    for a in ctx.assumptions:
        if a["sheet"] == "Pressure" and a["label"].startswith("door "):
            grouped.setdefault((a["sheet"], a["label"].split()[1], a["room"]), []).append(a)
            continue
        if a["sheet"] == "Criteria":
            grouped.setdefault(("Criteria", a.get("src", "")), []).append(a)
            continue
        if a["sheet"] in ("Room Information", "Envelope") and a.get("room"):
            grouped.setdefault((a["sheet"], a["room"]), []).append(a)
            continue
        if blank(a.get("value")):
            desc = f"{a['label']}: not given by any document and left blank; set it before it is used."
            typ = "CONFIRM"
        else:
            desc = f"{a['label']} = {a['value']}: assumed, to confirm."
            typ = "ASSUMPTION"
        rows.append(dict(type=typ, ref=f"{a['sheet']}!{a['cell']}", desc=desc, status="OPEN", owner="ACMV engineer", src=a.get("src", ""),
                         link=(a["sheet"], a["cell"])))
    door_fields = {"kind": "type", "seal": "seal type", "gap_cm2": "gap area (cm²)", "leaf_w_m": "leaf width (m)", "leaf_h_m": "leaf height (m)",
                   "fixed_flow_m3h": "fixed transfer (m³/h)"}
    for key, items in grouped.items():
        if key[0] == "Pressure":
            _, door, room = key
            parts = [f"{door_fields[a['label'].split()[2]]} {str(a['value']).replace('_', ' ')}" for a in items if a["label"].split()[2] in door_fields]
            what = "; ".join(parts) if parts else "door row entered without a measured or scheduled value"
            rows.append(dict(type="ASSUMPTION", ref=f"Pressure!{door}", desc=f"Door {door} (room {room}): assumed inputs, to confirm: {what}.",
                             status="OPEN", owner="ACMV engineer", src=items[0].get("src", ""), link=("Pressure", items[0]["cell"])))
            continue
        if key[0] == "Criteria":
            rooms_n = len({a["room"] for a in items})
            rows.append(dict(type="ASSUMPTION", ref=f"Criteria!{key[1]}", desc=f"Criteria assumed for {rooms_n} room(s) from source {key[1]}: to confirm.",
                             status="OPEN", owner="ACMV engineer", src=key[1], link=("Criteria", items[0]["cell"])))
            continue
        sheet, room = key
        nice = {"lighting_w_m2": "lighting (W/m²)", "small_power_w_m2": "plug load (W/m²)", "occupancy_per_100m2": "occupant density (persons per 100 m²)",
                "equipment_sensible_w": "equipment, sensible (W)", "equipment_latent_w": "equipment, latent (W)"}
        what = ", ".join(f"{nice.get(a['label'].replace('room ', ''), a['label'].replace('room ', '').replace('_', ' '))} {a['value']}" for a in items)
        who = f"Envelope segment {room}" if sheet == "Envelope" else f"Room {room}"
        rows.append(dict(type="ASSUMPTION", ref=f"{sheet}!{room}", desc=f"{who}: assumed inputs, to confirm: {what}.",
                         status="OPEN", owner="ACMV engineer", src=items[0].get("src", ""), link=(sheet, items[0]["cell"])))
    return rows


def write_flags(ctx: Ctx, rows: List[Dict[str, Any]]) -> None:
    ws = ctx.wb["Flags"]
    L = ctx.lay["FL"]
    title_block(ws, "Flags", "Assumptions and open items, each CONFIRM, VERIFY or ASSUMPTION, with status. Change the status to CLOSED or "
                "ACCEPTED when resolved; the README counts the open ones.", ctx)
    heads = [("id", "Flag", "", 8), ("type", "Type", "", 12), ("ref", "Reference", "", 26), ("desc", "Description", "", 120),
             ("status", "Status", "", 11), ("owner", "Owner", "", 16), ("src", "Source ID", "", 12)]
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
    if ctx.n_flags:
        list_validation(ws, f"{L.col('type')}{L.first}:{L.col('type')}{L.last}", "ListFlagType")
        list_validation(ws, f"{L.col('status')}{L.first}:{L.col('status')}{L.last}", "ListFlagStatus")
        band_rows(ws, f"A{L.first}:{L.col('src')}{L.last}")
        cf_text(ws, f"{L.col('status')}{L.first}:{L.col('status')}{L.last}", f"{L.col('status')}{L.first}", "OPEN", bold=False, fill="FFFFF2CC")
    # live outdoor-air rule flags: one row per room the rule separates (CONFIRM) or lists for Marcus (VERIFY)
    AF, AH, AH2, FC = ctx.lay["AF"], ctx.lay["AH"], ctx.lay["AH2"], ctx.lay["FC"]
    for q in range(ctx.n_live):
        i = len(rows) + q
        g = lambda k: L.local(k, i)
        kx = f"ROWS($A${L.row(len(rows))}:$A{L.row(i)})"          # position of this live row, 1 for the first
        m = f"MATCH({kx},{AF.rng('flgrank')},0)"
        used = f"{kx}<=MAX({AF.rng('flgrank')})"
        pick = lambda k: f"INDEX({AF.rng(k)},{m})"
        ahu = pick("ahu")
        fm = f"MATCH({pick('no')},{FC.rng('no')},0)"
        put(ws, g("id"), f'=IF({used},"F-"&TEXT(ROWS($A${L.first}:$A{L.row(i)}),"000"),"")', "Tag")
        put(ws, g("type"), f'=IF({used},IF({pick("status")}="SEPARATE","CONFIRM","VERIFY"),"")', "Text")
        put(ws, g("ref"), f'=IF({used},{pick("no")},"")', "Tag")
        before = f"INDEX({AH.rng('oa_rule')},MATCH({ahu},{AH.rng('ahu')},0))"
        after = f"INDEX({AH2.rng('oa_rule')},MATCH({ahu},{AH2.rng('ahu')},0))"
        cb = f"INDEX({AH.rng('coil')},MATCH({ahu},{AH.rng('ahu')},0))"
        ca = f"INDEX({AH2.rng('coil')},MATCH({ahu},{AH2.rng('ahu')},0))"
        sb = f"INDEX({AH.rng('sa_ahu')},MATCH({ahu},{AH.rng('ahu')},0))"
        sa = f"INDEX({AH2.rng('sa_ahu')},MATCH({ahu},{AH2.rng('ahu')},0))"
        sep = (f'"Outdoor-air rule: separate room "&{pick("no")}&" ("&{pick("name")}&") from "&{ahu}&" onto a dedicated FCU. Its outdoor-air fraction "'
               f'&TEXT({pick("frac")},"0.0%")&" on "&TEXT({pick("supply")},"0")&" L/s of supply sets the AHU fraction. AHU supply "&TEXT({sb},"0")&" to "'
               f'&TEXT({sa},"0")&" L/s, outdoor air "&TEXT({before},"0")&" to "&TEXT({after},"0")&" L/s, coil duty "&TEXT({cb},"0.0")&" to "'
               f'&TEXT({ca},"0.0")&" kW. "&IF(INDEX({FC.rng("oasrc")},{fm})="not set","FCU outdoor-air source not set: choose it from the zoning",'
               f'"FCU outdoor air from "&INDEX({FC.rng("oasrc")},{fm})&" ("&INDEX({FC.rng("oaby")},{fm})&", "&INDEX({FC.rng("oacond")},{fm})&"); source capacity "&INDEX({FC.rng("cap")},{fm}))&". The change proposal is in the zoning change proposals file. '
               f'After the engineer approves the change, update the zone schedule and the zoning drawing. Add the FCU to the system schedule."')
        lst = (f'"Outdoor-air rule: room "&{pick("no")}&" ("&{pick("name")}&") sets the outdoor-air fraction of "&{ahu}&" with "'
               f'&TEXT({pick("frac")},"0.0%")&" on a small share of the supply. Its criteria (air class, filtration, pressure, or fixed by '
               f'specification) may not be met by an FCU. It is not separated. It is listed for review."')
        put(ws, g("desc"), f'=IF({used},IF({pick("status")}="SEPARATE",{sep},{lst}),"")', "TextWrap")
        put(ws, g("status"), f'=IF({used},"OPEN","")', "Text")
        put(ws, g("owner"), f'=IF({used},"ACMV engineer","")', "Text")
        put(ws, g("src"), None, "Tag")
    # R1-10 (decided): one live VERIFY row per AHU whose calculated outdoor air is above the tender outdoor airflow (D-012). The order comes from the AHU tab (oaflg).
    for q in range(ctx.n_live_oa):
        i = len(rows) + ctx.n_live + q
        g = lambda k: L.local(k, i)
        m = f"MATCH(ROWS($A${L.row(len(rows) + ctx.n_live)}:$A{L.row(i)}),{AH.rng('oaflg')},0)"
        used = f"ISNUMBER({m})"
        pk = lambda k: f"INDEX({AH.rng(k)},{m})"
        put(ws, g("id"), f'=IF({used},"F-"&TEXT(ROWS($A${L.first}:$A{L.row(i)}),"000"),"")', "Tag")
        put(ws, g("type"), f'=IF({used},"VERIFY","")', "Text")
        put(ws, g("ref"), f'=IF({used},{pk("ahu")},"")', "Tag")
        put(ws, g("desc"), (f'=IF({used},"AHU "&{pk("ahu")}&": outdoor air "&TEXT({pk("oa_rule")},"#,##0")&" L/s is above tender outdoor air "&TEXT({pk("sched_oa")},"#,##0")'
                            f'&" L/s ("&IF({pk("tsrc")}="","tender airflow",{pk("tsrc")})&"). VERIFY. The tender airflow is information only.","")'), "TextWrap")
        put(ws, g("status"), f'=IF({used},"OPEN","")', "Text")
        put(ws, g("owner"), f'=IF({used},"ACMV engineer","")', "Text")
        put(ws, g("src"), None, "Tag")
    r = L.last + 2
    put(ws, f"A{r}", "Live", "Tag")
    put(ws, f"D{r}", (f'="Outdoor-air rule: "&COUNTIF({AF.rng("status")},"SEPARATE")&" room(s) proposed for separation onto an FCU; "'
                      f'&COUNTIF({AF.rng("status")},"LIST FOR REVIEW")&" clinical room(s) listed for review (see Airflow, last columns and the FCU tab)."'),
        "Calc").alignment = Alignment(horizontal="left")
    _setup(ws, ctx, L, L.idx["src"], r, "4:6", f"B{L.first}", "FFC00000")


# --------------------------------------------------------------------------- #
def write_checks(ctx: Ctx) -> None:
    ws = ctx.wb["Checks"]
    L = Layout("Checks", ["no", "desc", "value", "limit", "status", "note"], 7, 0)
    RI, CR, AF, PS, PU, FB, FB2, AH, FL = (ctx.lay[k] for k in ("RI", "CR", "AF", "PS", "PU", "FB", "FB2", "AH", "FL"))
    title_block(ws, "Checks", "Live reconciliations. PASS or FAIL are hard tests; VERIFY marks a finding to review; INFO is a count.", ctx)
    heads = [("no", "No.", "", 6), ("desc", "Check", "", 80), ("value", "Value (unit in Note)", "", 14), ("limit", "Limit or expected (unit in Note)", "", 16),
             ("status", "Status", "", 10), ("note", "Note", "", 60)]
    write_headers(ws, L, heads, 4, {"no": "RECONCILIATION"}, unit_row=False)
    ws.row_dimensions[6].height = 4
    nr = len(ctx.rooms)
    err_ranges = []
    for st in ctx.storeys:
        E = ctx.eng_layouts[st]
        err_ranges.append(f"'{E.sheet}'!$A${E.first}:${E.col('frac')}${E.last + 1}")
    for key in ("RI", "CR", "EN", "AF", "FB", "CP"):
        L_ = ctx.lay[key]
        err_ranges.append(f"'{L_.sheet}'!$A${L_.first}:${get_column_letter(L_.ncols)}${L_.last}")
    ps = ctx.lay["PS"]
    err_ranges.append(f"'Pressure'!$A${ps.first}:${get_column_letter(ps.ncols)}${ps.last}")
    pl = ctx.lay["PL"]
    err_ranges.append(f"'Pressure'!$A${pl.first}:${get_column_letter(pl.ncols)}${pl.last}")
    ah, ah2 = ctx.lay["AH"], ctx.lay["AH2"]
    err_ranges.append(f"'AHU'!$A${ah.first}:${get_column_letter(ah.ncols)}${ah2.last + 1}")
    for sh in ("Fans", "VAV", "Venturi Valves", "Sources"):
        w_ = ctx.wb[sh]
        err_ranges.append(f"'{sh}'!$A$7:${get_column_letter(max(w_.max_column, 2))}${max(w_.max_row, 8) + 1}")
    err_ranges.append(f"'Flags'!$A${FL.first}:${get_column_letter(FL.ncols)}${FL.last + 2}")
    err_ranges.append("'Basis of Design'!$A$1:$G$400")
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
            if sh != "Basis of Design":
                runs = [(rws[0], rws[-1])]       # the whole table column: an ID added anywhere in it is tested
            else:
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
    # one cell may not hold more than 8192 characters of formula: the terms go into helper cells (column H of Checks), the check sums them
    src_chunks, cur, cur_len = [], [], 0
    for t in src_terms:
        if cur and cur_len + len(t) + 1 > 6000:
            src_chunks.append(cur)
            cur, cur_len = [], 0
        cur.append(t)
        cur_len += len(t) + 1
    if cur:
        src_chunks.append(cur)
    helper_first = L.first
    unresolved = f"SUM($H${helper_first}:$H${helper_first + len(src_chunks) - 1})" if src_chunks else "0"
    pa = ctx.lay["PS"]
    checks = [
        ("Total area used against the total design area (DPA, else SOA), within the tolerance", f"=SUM({RI.rng('area')})", f"=SUM({RI.rng('base')})",
         '=IF(ABS(C{r}-D{r})<=MAX(AreaTolM2,AreaTolPct*D{r}),"PASS","VERIFY")', "m²; the difference comes from the rooms listed on Flags"),
        ("Rooms whose design area (DPA, else SOA) and measured area disagree beyond tolerance", f'=COUNTIF({RI.rng("agree")},"NO")', 0, '=IF(C{r}=0,"PASS","VERIFY")', "list on Room Information"),
        ("Rooms whose final supply is below either airflow case or the pressure minimum", f'=COUNTIF({AF.rng("ok")},"FAIL")', 0, '=IF(C{r}=0,"PASS","FAIL")', ""),
        ("Rooms with an AHU whose outdoor air is below the air-change minimum", f"=SUMPRODUCT(--({AF.rng('oa')}<{AF.rng('oa_air')}),--({AF.rng('supply')}>0),--({AF.rng('ahu')}<>\"\"))", 0, '=IF(C{r}=0,"PASS","FAIL")', "rooms with no AHU are counted in the check on rooms with no AHU; an exhaust-only room has no supply and takes transfer air"),
        ("Rooms whose outdoor-air requirement is above their supply", f"=SUMPRODUCT(--({AF.rng('oa_air')}>{AF.rng('supply')}),--({AF.rng('supply')}>0))", 0, '=IF(C{r}=0,"PASS","VERIFY")', "outdoor air is capped at supply; transfer air makes up the rest"),
        ("Rooms with a pressure target in Pa and no row on Pressure", f'=SUMPRODUCT(--ISNUMBER({CR.rng("pa")}),--({CR.rng("pa")}<>0),--(COUNTIF({PS.rng("node")},{RI.rng("no")})=0))', 0,
         '=IF(C{r}=0,"PASS","FAIL")', "every room with a pressure target other than 0 Pa is calculated from its internal surface; a neutral room (0 Pa) has no offset"),
        ("Rooms fixed by the MOT specification", f"=SUM({AF.rng('fix')})", "information", '="INFO"', "rooms with specified airflows"),
        ("Specified (MOT) rooms where the calculation needs more supply than specified", f'=COUNTIF({AF.rng("ok")},"SPECIFIED")', 0,
         '=IF(C{r}=0,"PASS","VERIFY")', "specified airflows are not resized; listed for the MOT engineer"),
        ("Specified (MOT) rooms whose specified offset is below the offset needed", f'=COUNTIF({AF.rng("off_chk")},"SHORT")', 0,
         '=IF(C{r}=0,"PASS","VERIFY")', "offset needed beside the specified one (Pressure)"),
        ("Pressure-controlled rooms with a ratio below 1.0", f'=COUNTIF({PS.rng("status")},"FAIL")', 0, '=IF(C{r}=0,"PASS","FAIL")', ""),
        ("Pressure-controlled rooms with a ratio from 1.0 to 1.2 (VERIFY)", f'=COUNTIF({PS.rng("status")},"VERIFY")', 0, '=IF(C{r}=0,"PASS","VERIFY")', ""),
        ("Free-pressure spaces whose balance does not close", f'=COUNTIF({PS.rng("status")},"UNSOLVED")', 0, '=IF(C{r}=0,"PASS","FAIL")', ""),
        ("Suites with a ratio below 1.0", f'=COUNTIF({PU.rng("status")},"FAIL")', 0, '=IF(C{r}=0,"PASS","FAIL")', ""),
        ("Known-answer pressure cases that miss their expected value", f'=COUNTIF({ctx.ka_status_range},"FAIL")', 0, '=IF(C{r}=0,"PASS","FAIL")', "to 0.2 m³/h"),
        ("Room flow balances that do not close", f'=COUNTIF({FB.rng("status")},"FAIL")', 0, '=IF(C{r}=0,"PASS","FAIL")', ""),
        ("AHU balances that do not close", f'=COUNTIF({FB2.rng("status")},"FAIL")' if ctx.ahus else "=0", 0, '=IF(C{r}=0,"PASS","FAIL")', ""),
        ("AHU room supply equals the Airflow total of the rooms that have an AHU", f"=SUM({AH.rng('sa_room')})",
         f"=SUMPRODUCT(--(LEN({AF.rng('ahu')})>0),{AF.rng('supply')})", '=IF(ABS(C{r}-D{r})<=BalTolLps,"PASS","FAIL")', "L/s"),
        ("Room supply of rooms that have no AHU and no system class", f"=SUMPRODUCT(--(LEN({AF.rng('ahu')})=0),--(LEN({RI.rng('systype')})=0),{AF.rng('supply')})", 0,
         '=IF(C{r}<=BalTolLps,"PASS","FAIL")', "L/s; the rooms are listed on Flags"),
        ("Room supply of rooms that have no AHU and a system class MV, NV or no information", f"=SUMPRODUCT(--(LEN({AF.rng('ahu')})=0),--(LEN({RI.rng('systype')})>0),{AF.rng('supply')})", 0,
         '=IF(C{r}<=BalTolLps,"PASS","VERIFY")', "L/s; no unit carries this supply"),
        ("Formula errors in the calculation tabs", f"={err}", 0, '=IF(C{r}=0,"PASS","FAIL")', "error values are never masked"),
        ("Source IDs that do not resolve on Sources", f"={unresolved}", 0, '=IF(C{r}=0,"PASS","FAIL")', ""),
        ("Rooms with neither an AHU nor a system class", f'=SUMPRODUCT(({RI.rng("no")}<>"")*({RI.rng("ahu")}="")*({RI.rng("systype")}=""))', 0,
         '=IF(C{r}=0,"PASS","FAIL")', "FCU, MV, NV and no information are system classes; listed on Flags"),
        ("Rooms with a system class MV or NV and no unit", f'=SUMPRODUCT(({RI.rng("ahu")}="")*(({RI.rng("systype")}="MV")+({RI.rng("systype")}="NV")>0))', "information", '="INFO"',
         "ventilation without a unit"),
        ("Rooms with the system class no information and no unit", f'=SUMPRODUCT(({RI.rng("ahu")}="")*({RI.rng("systype")}="NO INFORMATION"))', 0,
         '=IF(C{r}=0,"PASS","VERIFY")', "the zoning gives no evidence for these rooms"),
        ("Rooms with no area", f'=SUMPRODUCT(({RI.rng("no")}<>"")*((({RI.rng("area")}="")+({RI.rng("area")}=0))>0))', 0, '=IF(C{r}=0,"PASS","FAIL")', "listed on Flags"),
        ("Rooms with no ceiling height", f'=SUMPRODUCT(({RI.rng("no")}<>"")*({RI.rng("h")}=""))', 0, '=IF(C{r}=0,"PASS","FAIL")',
         "a blank height is a fault: the default height is a flagged assumption"),
        ("Scrub-up, anteroom, airlock, plant and equipment rooms with no criteria row (a calculation fault)",
         f'=SUMPRODUCT(({CR.rng("no")}<>"")*({CR.rng("crit")}="")*((ISNUMBER(SEARCH("scrub",{RI.rng("name")}))+ISNUMBER(SEARCH("anteroom",{RI.rng("name")}))+ISNUMBER(SEARCH("ante room",{RI.rng("name")}))'
         f'+ISNUMBER(SEARCH("airlock",{RI.rng("name")}))+ISNUMBER(SEARCH("sound lock",{RI.rng("name")}))+ISNUMBER(SEARCH("plant",{RI.rng("name")}))+ISNUMBER(SEARCH("equipment room",{RI.rng("name")})))>0))', 0,
         '=IF(C{r}=0,"PASS","FAIL")', "the room would take 0 L/s: the criteria row comes from the booklet, another department or the HFDS"),
        ("Rooms with no criteria row", f'=SUMPRODUCT(({CR.rng("no")}<>"")*({CR.rng("crit")}=""))', 0, '=IF(C{r}=0,"PASS","FAIL")',
         "a room that takes the default for circulation counts as a room with a criteria row; listed on Flags"),
        ("Rooms with no occupancy value (no RDS value and no room use for the default)", f'=COUNTIF({RI.rng("obasis")},"NOT SET")', 0, '=IF(C{r}=0,"PASS","VERIFY")', "listed on Flags"),
        ("Rooms separated by the outdoor-air rule", f'=COUNTIF({AF.rng("status")},"SEPARATE")', "information", '="INFO"', "see FCU"),
        ("Clinical rooms listed for review under the outdoor-air rule", f'=COUNTIF({AF.rng("status")},"LIST FOR REVIEW")', "information", '="INFO"', ""),
        ("Rooms the rule would still separate after four rounds", f"=SUM({AF.rng('trg4')})-SUM({AF.rng('lst4')})", 0, '=IF(C{r}<=0,"PASS","FAIL")', "rounds converged"),
        ("AHUs whose scheduled supply is below the calculated AHU supply", f'=SUMPRODUCT(({AH.rng("sched")}<>"")*({AH.rng("sched")}<{AH.rng("sa_ahu")}))', 0,
         '=IF(C{r}=0,"PASS","VERIFY")', ""),
        ("AHUs whose outdoor air is above the tender outdoor airflow", (f'=SUMPRODUCT(--ISNUMBER({AH.rng("sched_oa")}),--({AH.rng("oa_rule")}>{AH.rng("sched_oa")}))' if ctx.ahus else "=0"), 0,
         '=IF(C{r}=0,"PASS","VERIFY")', "VERIFY, not FAIL: tender airflow is information only"),
        ("AHUs whose room list is not complete (not every zoned room is counted)", f'=COUNTIF({AH.rng("cov")},"Not complete*")' if ctx.ahus else "=0", "information", '="INFO"',
         "the zoned rooms and the rooms counted are on the AHU tab"),
        ("AHUs with a criteria gap (rooms with no criteria row)", f'=COUNTIF({AH.rng("cov")},"*no criteria row*")' if ctx.ahus else "=0", "information", '="INFO"',
         "units whose calculated airflow does not yet cover every room"),
        ("Rooms whose criteria are presumed from the same space in another department", f'=COUNTIF({CR.rng("remark")},"Presumed criteria*")', "information", '="INFO"',
         "assumption, code A: listed on Flags"),
        ("Rooms with lighting and small power assumed from ASHRAE", f'=COUNTIF({RI.rng("light_s")},"ASM-LIGHT")', "information", '="INFO"', "assumption, code A: red cells"),
        ("Rooms with no lighting or small power value", f'=SUMPRODUCT(({RI.rng("no")}<>"")*(({RI.rng("light")}="")+({RI.rng("power")}="")>0))', 0, '=IF(C{r}=0,"PASS","VERIFY")',
         "the room use is not mapped; listed on Flags"),
        ("Rooms linked to a room box of the tender air-side schematics", f'=COUNT({ctx.lay["CP"].rng("sch_s")})', "information", '="INFO"',
         "of the rooms on Room Information; the room boxes that match no room are on Flags"),
        ("Rooms whose design temperature is outside the printed criteria range", f'=COUNTIF({CR.rng("trange")},"FAIL")', 0, '=IF(C{r}=0,"PASS","FAIL")',
         "design temperature: the printed set point; else 24 °C limited to the printed range"),
        ("Rooms where the thermal airflow at the lower limit of the range is above the final supply", f'=COUNTIF({AF.rng("lo_chk")},"VERIFY")', 0, '=IF(C{r}=0,"PASS","VERIFY")',
         "information only, it does not change the supply (procedure, resuscitation, ICU and imaging rooms)"),
        ("Envelope rooms where the thermal case governs or is close to the air-change case (peak solar check)", f'=COUNTIF({AF.rng("env_chk")},"VERIFY")', 0, '=IF(C{r}=0,"PASS","VERIFY")',
         "marked for the peak solar check of the engineer"),
        ("Dirty single-pass rooms (recirculated supply, dedicated exhaust, no return)", f'=COUNTIF({AF.rng("spc")},"DIRTY")', "information", '="INFO"',
         "class by room use, assumption to confirm; listed on the AHU tab by unit"),
        ("AHUs whose make-up air is above the AHU supply", (f'=COUNTIF({AH.rng("oa_short")},">0")' if ctx.ahus else "=0"), 0, '=IF(C{r}=0,"PASS","VERIFY")',
         "the air that leaves the rooms is more than the AHU supply"),
        ("Open flags", f'=COUNTIF({FL.rng("status")},"OPEN")', "information", '="INFO"', "see Flags"),
    ]
    L.n = len(checks)
    # acceptance items of brief 8.6 (every room has an AHU or class, every criterion traces to the booklet, every pressure target is on Pressure or listed) and
    # the supply that no unit carries; the other checks test arithmetic and balance
    acceptance = ("Rooms with a pressure target", "Room supply of rooms that have no AHU", "Rooms with neither", "Rooms with the system class no information", "Rooms with no area",
                  "Rooms with no ceiling height", "Rooms with no criteria row", "Rooms with no occupancy value")
    ctx.chk_row = {}
    put(ws, f"G{L.first - 2}", "Kind", "HeaderLeft")
    ws.column_dimensions["G"].width = 14
    for i, (desc, val, lim, st, note) in enumerate(checks):
        r = L.row(i)
        kind = "ACCEPTANCE" if desc.startswith(acceptance) else "CALCULATION"
        put(ws, f"G{r}", kind, "Note")
        for key, pre in (("press", "Rooms with a pressure target"), ("sup_none", "Room supply of rooms that have no AHU and no"), ("sup_cls", "Room supply of rooms that have no AHU and a"),
                         ("sup_on", "AHU room supply equals"), ("no_unit", "Rooms with neither"), ("no_area", "Rooms with no area"), ("no_h", "Rooms with no ceiling height"),
                         ("no_crit", "Rooms with no criteria row"), ("no_occ", "Rooms with no occupancy value")):
            if desc.startswith(pre):
                ctx.chk_row[key] = r
        put(ws, f"A{r}", i + 1, "Text", "d0")
        put(ws, f"B{r}", desc, "Text")
        put(ws, f"C{r}", val, "Calc", "d1" if i == 0 else "d0")
        put(ws, f"D{r}", lim if isinstance(lim, str) and lim.startswith("=") else lim, "Calc", "d1" if (i == 0 or desc.startswith("AHU room supply")) else "d0")
        if isinstance(lim, str) and not lim.startswith("="):
            ws[f"D{r}"].alignment = Alignment(horizontal="right")
        put(ws, f"E{r}", st.replace("{r}", str(r)), "Check")
        put(ws, f"F{r}", note, "Note")
    if src_chunks:
        put(ws, f"H{L.first - 1}", "Helper: source IDs not found on Sources", "Note")
        ws.column_dimensions["H"].width = 14
        ws.column_dimensions["H"].hidden = True
        for j, chunk in enumerate(src_chunks):
            put(ws, f"H{helper_first + j}", "=" + "+".join(chunk), "Calc", "d0")
    # first check formula referenced E{r}="" harmlessly; overall
    last = L.last
    cf_text(ws, f"E{L.first}:E{last}", f"E{L.first}", "FAIL")
    cf_text(ws, f"E{L.first}:E{last}", f"E{L.first}", "VERIFY", fill="FFFFF2CC")
    r = last + 2
    # D-056: two status lines, never merged. 'Calculation' must be PASS for any issue. 'Inputs complete' counts the acceptance items that are open.
    kcol = f"$G${L.first}:$G${last}"
    ecol = f"$E${L.first}:$E${last}"
    put(ws, f"B{r}", "Calculation", "ResultText")
    put(ws, f"E{r}", f'=IF(COUNTIFS({kcol},"CALCULATION",{ecol},"FAIL")>0,"FAIL",IF(COUNTIFS({kcol},"CALCULATION",{ecol},"VERIFY")>0,"VERIFY","PASS"))', "ResultCheck")
    for col in "ACD":
        put(ws, f"{col}{r}", None, "ResultText")
    put(ws, f"F{r}", "Calculation checks only. FAIL if one fails; VERIFY if none fails and some need review. Must be PASS for any issue, a draft included", "ResultText")
    cf_text(ws, f"E{r}", f"E{r}", "FAIL")
    cf_text(ws, f"E{r}", f"E{r}", "VERIFY", fill="FFFFF2CC")
    ctx.checks_status = f"'Checks'!$E${r}"
    r2 = r + 1
    put(ws, f"B{r2}", "Inputs complete", "ResultText")
    put(ws, f"E{r2}", f'=IF(SUMPRODUCT(--({kcol}="ACCEPTANCE"),--({ecol}<>"PASS"))=0,"COMPLETE","NOT COMPLETE")', "ResultCheck")
    for col in "ACD":
        put(ws, f"{col}{r2}", None, "ResultText")
    put(ws, f"F{r2}", "Acceptance items (rooms with unit, criteria, area, height; area controls). Reads NOT COMPLETE while one is open. The workbook is a DRAFT, acceptance open", "ResultText")
    cf_text(ws, f"E{r2}", f"E{r2}", "NOT COMPLETE", fill="FFFFF2CC")
    r = r2
    ctx.checks_range = f"'Checks'!$E${L.first}:$E${last}"
    ctx.checks_layout = L
    ctx.checks_rows = (L.first, last)
    _setup(ws, ctx, L, 7, r, "4:6", f"C{L.first}", "FFC00000")


def write_readme(ctx: Ctx) -> None:
    from openpyxl.styles import Border, Font, PatternFill
    from openpyxl.worksheet.pagebreak import Break
    from .styles import FILL_FAIL, HAIR, LOAD_NAME, OCC_NAME, PRES_NAME
    ws = ctx.wb["README"]
    d = ctx.data
    for col, w in zip("ABCDEFG", (2, 34, 58, 44, 16, 14, 14)):
        ws.column_dimensions[col].width = w
    ws.column_dimensions["H"].width = 40
    put(ws, "B1", f"{d['department'].get('number', '')}  {d['department'].get('name', '')}", "Title")
    put(ws, "B2", f"{ctx.project}: thermal load, airflow and pressure workbook", "Note")
    ws.merge_cells("B2:H2")
    ws.row_dimensions[2].height = 14
    r = 4
    put(ws, f"B{r}", "Purpose and scope", "Section")
    r += 1
    put(ws, f"B{r}", d.get("purpose", "This workbook calculates room loads, supply and outdoor airflow, pressure offsets and AHU totals for the "
                                      "department. Every input has a source."), "TextWrap")
    ws.merge_cells(f"B{r}:H{r}")
    ws.row_dimensions[r].height = 30
    r += 2
    put(ws, f"B{r}", "Status (live)", "Section")
    r += 1
    FL = ctx.lay["FL"]
    ck = lambda k, col="C": f"'Checks'!${col}${ctx.chk_row[k]}"
    RI_, AF_, CR_ = ctx.lay["RI"], ctx.lay["AF"], ctx.lay["CR"]
    kind_rng = f"'Checks'!$G${ctx.checks_layout.first}:$G${ctx.checks_layout.last}"
    on_unit = f'SUMPRODUCT(--(LEN({AF_.rng("ahu")})>0),{AF_.rng("supply")})'
    tot_sup = f'SUM({AF_.rng("supply")})'
    n_rooms = f'COUNTA({RI_.rng("no")})'
    n_unit = f'SUMPRODUCT(--({RI_.rng("no")}<>""),--((LEN({RI_.rng("ahu")})+LEN({RI_.rng("systype")}))>0))'
    n_crit = f'SUMPRODUCT(--({CR_.rng("no")}<>""),--({CR_.rng("crit")}<>""))'
    acc_open = f'(SUMPRODUCT(--({kind_rng}="ACCEPTANCE"),--({ctx.checks_range}<>"PASS")))'
    pct = lambda a, b: f'TEXT(IF({b}=0,0,{a}/{b}),"0.0 %")'
    for label, f in (("Coverage of the AHU totals", f'=IF({tot_sup}=0,"No room supply",IF({on_unit}/{tot_sup}<0.99,"PARTIAL. Only "&{pct(on_unit, tot_sup)}&" of the room supply of this department is on a unit that the zoning gives. '
                                                         f'Read every AHU total as partial; the rooms with no unit are listed on Flags.","COMPLETE: all room supply is on a unit"))'),
                     ("Calculation checks", f'=COUNTIFS({kind_rng},"CALCULATION",{ctx.checks_range},"FAIL")&" FAIL, "&COUNTIFS({kind_rng},"CALCULATION",{ctx.checks_range},"VERIFY")&" TO VERIFY"'),
                     ("Inputs complete", f'=IF({acc_open}=0,"COMPLETE","NOT COMPLETE: "&{acc_open}&IF({acc_open}=1," item"," items")&" open")'),
                     ("Rooms with an AHU or a system class", f'={n_unit}&" of "&{n_rooms}&" ("&{pct(n_unit, n_rooms)}&")"'),
                     ("Room supply on an AHU", f'=TEXT({on_unit},"#,##0")&" of "&TEXT({tot_sup},"#,##0")&" L/s ("&{pct(on_unit, tot_sup)}&")"'),
                     ("Rooms with a criteria row", f'={n_crit}&" of "&{n_rooms}&" ("&{pct(n_crit, n_rooms)}&")"'),
                     ("Rooms with a pressure target and no Pressure row", f'={ck("press")}'),
                     ("Thermal load case", f'="PEOPLE, LIGHTING AND SMALL POWER (ASHRAE values where no project value). A provisional equipment gain is included for equipment-led rooms. Envelope gain by the simple facade method for "&IFERROR(SUMPRODUCT(({ctx.lay["EN"].rng("room")}<>"")/COUNTIF({ctx.lay["EN"].rng("room")},{ctx.lay["EN"].rng("room")}&"")),0)&" of "&{n_rooms}&" rooms only (rooms with a plan room link on a storey with a facade). The other rooms get no envelope gain: not assessed."'),
                     ("Open flags", f'=COUNTIF({FL.rng("status")},"OPEN")'),
                     ("Rooms", f"=COUNTA({ctx.lay['RI'].rng('no')})")):
        put(ws, f"B{r}", label, "Text")
        put(ws, f"C{r}", f, "Calc", "d0").alignment = Alignment(horizontal="left", vertical="center")
        r += 1
    r += 1
    put(ws, f"B{r}", "How to read this workbook", "Section")
    r += 1
    for t in ("Inputs are on Room Information, Criteria, Envelope and the system tabs. Each input has a code and a source ID that "
              "links to Sources. Results follow by formula. No value is pasted and no number is typed inside a formula.",
              "The Room Zones tab of each storey calculates the room loads. Airflow applies both cases to every room. Pressure "
              "calculates the offset of every room with a pressure target other than 0 Pa. AHU adds up the rooms of each AHU. Checks reconciles everything.",
              "Unknown values are blank and are listed on Flags. Yellow cells are assumptions to confirm. On Room Information the Code and Source ID "
              "columns and the system detail are grouped: use the outline buttons above the column letters to show them."):
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
        put(ws, f"D{r}", 123.4 if code not in ("K",) else "PASS", style, "d1")
        put(ws, f"E{r}", appearance[code], "Text")
        r += 1
        if code == "A":
            put(ws, f"B{r}", f"A   {OCC_NAME}", "Tag")
            put(ws, f"C{r}", "Occupancy from the default density: the RDS gives no value", "Text")
            put(ws, f"D{r}", 12, OCC_NAME, "d0")
            put(ws, f"E{r}", "red text on light yellow", "Text")
            r += 1
            put(ws, f"B{r}", f"A   {LOAD_NAME}", "Tag")
            put(ws, f"C{r}", "Plug load or lighting from the ASHRAE value of the room use: no project document gives it", "Text")
            put(ws, f"D{r}", 10, LOAD_NAME, "d1")
            put(ws, f"E{r}", "red text on light yellow", "Text")
            r += 1
            put(ws, f"B{r}", f"A   {PRES_NAME}", "Tag")
            put(ws, f"C{r}", "Criteria presumed from the same space in another department", "Text")
            put(ws, f"D{r}", "Office", PRES_NAME)
            put(ws, f"E{r}", "italic green text", "Text")
            r += 1
        if code == "K":
            put(ws, f"B{r}", "K   Check, failed", "Tag")
            put(ws, f"C{r}", "A test that fails", "Text")
            c = put(ws, f"D{r}", "FAIL", "Check")
            c.font = Font(name="Arial", size=10, bold=True)
            c.fill = PatternFill("solid", fgColor=FILL_FAIL)
            put(ws, f"E{r}", "bold on light red", "Text")
            r += 1
    put(ws, f"B{r}", "Notes are grey italic at 8 pt. Remarks are black at 10 pt.", "Note")
    ws.merge_cells(f"B{r}:H{r}")
    r += 2
    ws.row_breaks.append(Break(id=r - 1))
    put(ws, f"B{r}", "Tab map", "Section")
    r += 1
    tabs = [("Basis of Design", "Document hierarchy, design conditions and named constants, criteria origin, conventions, key flags"),
            ("Room Information", "One row per room: inputs with code, source ID and remark"),
            ("Criteria", "Criteria row applied to each room, with its source"),
            ("Envelope", "Façade segments per room, summed on the Room Zones tab")]
    tabs += [(ctx.sheet_for(s), f"Room loads, {ctx.label(s)}") for s in ctx.storeys]
    tabs += [("Airflow", "Both airflow cases, governing case, exhaust, return, outdoor air, outdoor-air rule"),
             ("Pressure", "Pressure offset of every room with a pressure target other than 0 Pa, sensitivities and the known-answer test"),
             ("AHU", "Airflow totals, outdoor air, coil duty"), ("FCU", "Rooms on fan coil units, including separated rooms"),
             ("Fans", "Return, exhaust and outdoor-air fan duties"), ("VAV", "Terminals: maximum, minimum, turndown"),
             ("Venturi Valves", "Valve ranges from Pressure"), ("Flow Balance", "Air in against air out"),
             ("Airflow Comparison", "Calculated airflows against the tender airflows, room by room and AHU by AHU"), ("Checks", "Live reconciliations"),
             ("Sources", "One row per source ID"), ("Flags", "Assumptions and open items")]
    for name, desc in tabs:
        c = put(ws, f"B{r}", name, "Text")
        link_to(c, name, "A1")
        put(ws, f"C{r}", desc, "Text")
        r += 1
    r += 1
    put(ws, f"B{r}", "Units and abbreviations", "Section")
    r += 1
    for t in ("Units. Airflow: L/s on every tab, except the leakage flows on Pressure, which are in m³/h (each column header says so). "
              "Pressure: Pa. Area: m². Volume: m³. Temperature: °C. Enthalpy: kJ/kg. Heat gain and room load: W. AHU, coil and fan duty: kW "
              "(named cell WperkW converts W to kW).",
              "Abbreviations. ACH: air changes per hour. AHU: air handling unit. PAHU: pre-cooled air handling unit (the term of the Contract documents, as the RFI-ME-0233 memo states). "
              "FCU: fan coil unit. VAV: variable air volume. OA: outdoor air. SOA: schedule of accommodation. "
              "DPA: as named in the source documents (no project document expands it). RDS: room data sheet. "
              "RLS: room layout sheet. DD: developed design. MOT: major operating theatre (department 22). "
              "MOT NSC: the modular operating theatres works package of the tender documents."):
        put(ws, f"B{r}", t, "TextWrap")
        ws.merge_cells(f"B{r}:H{r}")
        ws.row_dimensions[r].height = 42
        r += 1
    r += 1
    put(ws, f"B{r}", "Revision", "Section")
    r += 1
    for col, h in zip("BCDEFG", ("Rev", "Date", "Description", "By", "Chk'd", "App'd")):
        put(ws, f"{col}{r}", h, "HeaderLeft")
    r += 1
    rev = d.get("project", {}).get("revision", {})
    for rv in d.get("revisions", [rev]):
        for col, k in zip("BCDEFG", ("rev", "date", "description", "by", "checked", "approved")):
            put(ws, f"{col}{r}", rv.get(k), "TextWrap" if k == "description" else "Text")
        r += 1
    setup_sheet(ws, "README", rev=ctx.rev, date=ctx.date, project=ctx.project, print_rows="1:2", last_col=8, last_row=r, tab_colour="FF1F4E79")
