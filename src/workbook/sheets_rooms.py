"""Room Information, Criteria, Envelope, the Room Zones engines and Airflow."""

from __future__ import annotations

import re
from typing import Any, Dict, List

from openpyxl.styles import Alignment
from openpyxl.utils import get_column_letter

from .common import (band_rows, cf_formula, cf_text, expand, group_cols, list_validation, title_block, write_headers)
from .ctx import Ctx
from .layouts import (ENG, ENG_FIRST, FIRST, OA_ROUNDS, af_spec, cr_spec, en_spec, ri_spec)
from .model import blank, prov_of
from .styles import CODE_STYLES, LOAD_NAME, OCC_NAME, PRES_NAME, put, setup_sheet, tag_variant

X = lambda v: None if blank(v) else v


def _setup(ws, ctx: Ctx, lay, ncols_print: int, last_row: int, print_rows: str, freeze: str, colour: str = None) -> None:
    setup_sheet(ws, ws.title, rev=ctx.rev, date=ctx.date, project=ctx.project, print_rows=print_rows,
                last_col=ncols_print, last_row=last_row, tab_colour=colour, freeze=freeze)


def xl_str(text: str) -> str:
    """A text as an Excel string expression: quotes doubled, literals cut below the 255-character limit and joined."""
    text = str(text or "")
    parts = [text[i:i + 200] for i in range(0, len(text), 200)] or [""]
    return "&".join('"' + p.replace('"', '""') + '"' for p in parts)


def _template_pieces(tpl: str, values: Dict[str, str]) -> List[str]:
    """Excel string pieces of a Remarks template: the text between the {names} as literals and each {name} as the expression of `values`."""
    pieces, pos = [], 0
    for m in re.finditer(r"\{(\w+)\}", tpl):
        pieces.append(xl_str(tpl[pos:m.start()]))
        pieces.append(values.get(m.group(1), '""'))
        pos = m.end()
    pieces.append(xl_str(tpl[pos:]))
    return pieces


def _remark_formula(ctx: Ctx, r: Dict[str, Any], g, rem: Any) -> str:
    """Remarks cell of a room that carries an assumption of the project input: the notes of the project configuration (occupancy density D-017, plug load and
    lighting D-025), built from the cells of the same row, then the text of the room."""
    from .conf import internal_loads_cfg, occupancy_cfg
    pieces: List[str] = []
    if r.get("occupancy_basis") == "DENSITY DEFAULT":
        tpl = str(occupancy_cfg().get("remarks_template") or
                  "Occupancy assumed: {density} p/100 m2 x {area} m2 = {people} (ASHRAE 62.1 Table 6.2.2.1, {use}). No RDS value. To confirm.").replace("m2", "m²")
        pieces += _template_pieces(tpl, {"density": f'TEXT({g("dens")},"0")', "area": f'TEXT({g("area")},"0.0")', "people": f'TEXT({g("people")},"0")',
                                         "use": xl_str(str(r.get("occupancy_use") or "use not mapped"))})
        pieces.append(xl_str(" "))
    if r.get("load_basis") == "ASHRAE":
        tpl = str(internal_loads_cfg().get("remarks_template") or "{what} assumed: {value} W/m2 (ASHRAE, {use}). No project value. To confirm.").replace("m2", "m²")
        use = xl_str(str(r.get("load_use") or ""))
        for what, key in (("Plug load", "power"), ("Lighting", "light")):
            pieces += _template_pieces(tpl, {"what": xl_str(what), "value": f'TEXT({g(key)},"0.0")', "use": use})
            pieces.append(xl_str(" "))
    if not blank(rem):
        pieces.append(xl_str(str(rem)))
    elif pieces:
        pieces.pop()
    return "=" + "&".join(pieces)


def _val(ctx: Ctx, ws, ref: str, v: Any, code: str, *, fmt=None, kind="num", cref: str = None, sref: str = None, src: str = "",
         label: str = "", room: str = "", style: str = None) -> None:
    """Input value with the group's shared code and source cells."""
    has = not blank(v)
    style = style if (style and has) else (CODE_STYLES[code][0] if (has and code) else "Input")
    if kind == "tag":
        style = tag_variant(style)
    c = put(ws, ref, v if has else None, style, fmt)
    if kind in ("text", "tag"):
        c.alignment = Alignment(horizontal="left", vertical="center")
    if has:
        ctx.coded.append(dict(sheet=ws.title, cell=ref, code_cell=cref, src_cell=sref, code=code))
        if code == "A":
            ctx.assumptions.append(dict(sheet=ws.title, cell=ref, label=label, value=v, room=room, src=src))


def _prov_cells(ctx: Ctx, ws, cref: str, sref: str, code: str, src: str, any_value: bool) -> None:
    put(ws, cref, code if any_value else None, "Code")
    put(ws, sref, src if any_value else None, "Tag")
    if any_value and src:
        ctx.src_ranges.append(sref)
        ctx.src_link(ws, sref, src)


# --------------------------------------------------------------------------- #
def write_room_information(ctx: Ctx) -> None:
    ws = ctx.wb["Room Information"]
    L = ctx.lay["RI"]
    spec = ri_spec()
    title_block(ws, "Room Information", "One row per room. Inputs carry a code (D document, M measured, S specified, "
                "A assumption) and a source ID that links to Sources. Unknown values stay blank and are listed on Flags.", ctx)
    write_headers(ws, L, spec, 4, {"no": "ROOM", "ahu": "SYSTEM (approved zoning)", "sptype": "SPACE TYPE AND CONDITION",
                                   "dpa": "AREA", "h": "HEIGHT", "pfix": "OCCUPANCY AND OPERATION", "light": "GAINS",
                                   "glz": "FENESTRATION FROM ENVELOPE", "remark": "REMARKS"})
    for i, r in enumerate(ctx.rooms):
        row = L.row(i)
        n = r["number"]
        g = lambda k: f"{L.col(k)}{row}"
        # identity
        for k, v in (("no", n), ("name", r.get("name")), ("storey", r.get("storey"))):
            code, src = prov_of(r, "identity", not blank(v), k)
            _val(ctx, ws, g(k), v, code, kind="tag" if k == "no" else "text", cref=g(k + "_c"), sref=g(k + "_s"), src=src,
                 label=f"room {k}", room=n)
            _prov_cells(ctx, ws, g(k + "_c"), g(k + "_s"), code, src, not blank(v))
        # zoning
        zf = [("ahu", "ahu", "tag"), ("zone", "zone_tag", "tag"), ("systype", "system_type", "text"),
              ("terminal", "terminal_tag", "tag"), ("exfan", "exhaust_fan", "tag"), ("oasrc", "oa_source", "tag"),
              ("oacond", "oa_condition", "text")]
        for k, f, kind in zf:
            has_f = not blank(r.get(f))
            zc, zs = prov_of(r, "zoning", has_f, f)
            _val(ctx, ws, g(k), r.get(f), zc, kind=kind, cref=g(k + "_c"), sref=g(k + "_s"), src=zs, label=f"room {f}", room=n)
            _prov_cells(ctx, ws, g(k + "_c"), g(k + "_s"), zc, zs, has_f)
        # single-value groups
        simple = [("sptype", "space_type", "space_type", "text", None), ("cond", "room_condition", "space_type", "text", None),
                  ("dpa", "dpa_area_m2", "dpa_area", "num", "d1"),
                  ("soa", "soa_area_m2", "soa_area", "num", "d1"), ("meas", "measured_area_m2", "measured_area", "num", "d1"),
                  ("sel", "area_selector", "area_selector", "text", None), ("h", "ceiling_height_m", "ceiling_height", "num", "d2"),
                  ("pfix", "occupancy_people", "occupancy_people", "num", "d0"),
                  ("dens", "occupancy_per_100m2", "occupancy_density", "num", "d1"), ("iof", "iof", "iof", "num", "d2"),
                  ("act", "activity", "activity", "text", None), ("hours", "operation_hours", "operation_hours", "num", "d1"),
                  ("light", "lighting_w_m2", "lighting", "num", "d1"), ("power", "small_power_w_m2", "small_power", "num", "d1"),
                  ("eqs", "equipment_sensible_w", "equipment_sensible", "num", "d0"),
                  ("eql", "equipment_latent_w", "equipment_latent", "num", "d0")]
        density_default = r.get("occupancy_basis") == "DENSITY DEFAULT"
        load_ashrae = r.get("load_basis") == "ASHRAE"
        for k, f, grp, kind, fmt in simple:
            v = r.get(f)
            if k == "sel" and blank(v):
                v = "AUTO"
                code_s, src_s = ("S", "GEN-BASIS")
            elif k == "cond" and not blank(v):
                code_s, src_s = prov_of(r, "space_type", True)
            else:
                code_s, src_s = prov_of(r, grp, not blank(v))
            style = OCC_NAME if (k == "dens" and density_default and not blank(v)) else None
            if k in ("light", "power") and load_ashrae and not blank(v):
                style = LOAD_NAME
            if k in ("eqs", "eql") and r.get("equip_basis") == "PROVISIONAL" and not blank(v):
                style = LOAD_NAME                    # provisional equipment gain: red text, note in Remarks, vendor data to follow
            _val(ctx, ws, g(k), v, code_s, fmt=fmt, kind=kind, cref=g(k + "_c"), sref=g(k + "_s"), src=src_s,
                 label=f, room=n, style=style)
            _prov_cells(ctx, ws, g(k + "_c"), g(k + "_s"), code_s, src_s, not blank(v))
        # calculated
        dpa, soa, meas, sel, base = g("dpa"), g("soa"), g("meas"), g("sel"), g("base")
        agree = (f'=IF(OR({base}="",{meas}=""),"",IF(OR(ABS({base}-{meas})<=AreaTolM2,ABS({base}-{meas})<=AreaTolPct*{base}),'
                 f'"YES","NO"))')
        base_f = (f'=IF({dpa}="",IF({soa}="","",ROUND({soa},AreaDecimals)),IF({dpa}>0,ROUND({dpa},AreaDecimals),'
                  f'IF({soa}="","",ROUND({soa},AreaDecimals))))')
        area = (f'=ROUND(IF({sel}="SOA",{soa},IF({sel}="DPA",{dpa},IF({sel}="MEASURED",{meas},IF({sel}="LARGER",MAX({base},{meas}),'
                f'IF({meas}="",IF({base}="",0,{base}),IF({base}="",{meas},IF({g("agree")}="YES",{base},{meas}))))))),AreaDecimals)')
        people = (f'=IF({g("pfix")}<>"",IF({g("obasis")}="RDS",ROUNDUP({g("pfix")},0),{g("pfix")}),IF({g("obasis")}="DENSITY DEFAULT",ROUNDUP({g("area")}*{g("dens")}/PercentBase,0),'
                  f'{g("area")}*{g("dens")}/PercentBase))')
        for k, f, st, fmt in (("base", base_f, "Calc", "d1"), ("area", area, "Calc", "d1"),
                              ("diff", f'=IF(AND({base}<>"",{meas}<>"",{base}<>0),({base}-{meas})/{base},"")', "Calc", "pct1"),
                              ("agree", agree, "Calc", None),
                              ("vol", f"={g('area')}*{g('h')}", "Calc", "d1"),
                              ("people", people, OCC_NAME if density_default else "Calc", "d0"),
                              ("glz", f"=SUMIFS([EN.glz],[EN.room],{g('no')})", "Link", "d1"),
                              ("wall", f"=SUMIFS([EN.wall],[EN.room],{g('no')})", "Link", "d1"),
                              ("roof", f"=SUMIFS([EN.roof],[EN.room],{g('no')})", "Link", "d1"),
                              ("face", f'=IF(COUNTIF([EN.room],{g("no")})=0,"No envelope data",INDEX([EN.face],MATCH({g("no")},[EN.room],0))'
                                       f'&" "&INDEX([EN.az],MATCH({g("no")},[EN.room],0))&"°, "&INDEX([EN.shade],MATCH({g("no")},[EN.room],0))'
                                       f'&IF(COUNTIF([EN.room],{g("no")})>1," (+"&(COUNTIF([EN.room],{g("no")})-1)&" more)",""))',
                               "Link", None)):
            f = expand(ctx, f, L, i)
            c = put(ws, g(k), f, st, fmt)
            if k in ("agree", "face"):
                c.alignment = Alignment(horizontal="left" if k == "face" else "center", vertical="center")
        ob = r.get("occupancy_basis")
        put(ws, g("obasis"), ob, "Text")
        if density_default:
            ctx.coded.append(dict(sheet=ws.title, cell=g("people"), code_cell=g("dens_c"), src_cell=g("dens_s"), code="A"))
        rem = r.get("remark")
        if density_default or load_ashrae:
            put(ws, g("remark"), _remark_formula(ctx, r, g, rem), "Remark")
            nl = -(-(len(str(rem or "")) + (150 if density_default else 0) + (230 if load_ashrae else 0)) // 165)
        else:
            put(ws, g("remark"), rem, "Remark")
            nl = -(-len(str(rem or "")) // 165)
        ws.row_dimensions[row].height = 13.0 * max(1, nl) + 2
    last = L.last
    list_validation(ws, f"{L.col('sptype')}{L.first}:{L.col('sptype')}{last}", "ListSpaceType")
    list_validation(ws, f"{L.col('cond')}{L.first}:{L.col('cond')}{last}", "ListCondition")
    list_validation(ws, f"{L.col('sel')}{L.first}:{L.col('sel')}{last}", "ListAreaSource")
    list_validation(ws, f"{L.col('systype')}{L.first}:{L.col('systype')}{last}", "ListSystemType")
    list_validation(ws, f"{L.col('act')}{L.first}:{L.col('act')}{last}", "ListActivity")
    list_validation(ws, f"{L.col('oacond')}{L.first}:{L.col('oacond')}{last}", "ListOACond")
    band_rows(ws, f"A{L.first}:{L.col('remark')}{last}")
    cf_text(ws, f"{L.col('agree')}{L.first}:{L.col('agree')}{last}", f"{L.col('agree')}{L.first}", "NO")
    # occupancy that no RDS value and no room use gives: the 0 is a placeholder, shown on a yellow fill so it is not read as a value
    for k in ("obasis", "people"):
        cf_formula(ws, f"{L.col(k)}{L.first}:{L.col(k)}{last}", f'${L.col("obasis")}{L.first}="NOT SET"', bold=True, fill="FFFFF2CC")
    # provenance pairs and detail blocks: one outline level, collapsed for print; the label columns stay visible
    group_cols(ws, L, "zone", "oacond_s", collapsed=True)
    group_cols(ws, L, "glz", "face", collapsed=True)
    for k in L.idx:
        if k.endswith("_c") or k.endswith("_s"):
            dim = ws.column_dimensions[L.col(k)]
            dim.outline_level = 1
            dim.hidden = True
    ws.column_dimensions[L.col("remark")].width = 150
    _setup(ws, ctx, L, L.idx["remark"], last, "4:6", f"{get_column_letter(L.idx['name'] + 1)}{L.first}", "FF1F4E79")


# --------------------------------------------------------------------------- #
def write_criteria(ctx: Ctx) -> None:
    ws = ctx.wb["Criteria"]
    L = ctx.lay["CR"]
    title_block(ws, "Criteria", "The criteria row applied to each room, from the DD Booklet criteria table (or, for MOT NSC "
                "theatres, the MOT specification, coded S). The pressure target is taken from the tender drawings, then the "
                "working drawings, then the booklet.", ctx)
    write_headers(ws, L, cr_spec(), 4, {"no": "ROOM", "crit": "CRITERIA ROW AND SOURCE", "oa_ach": "AIR", "tmin": "CONDITIONS",
                                        "regime": "PRESSURE", "cls": "CLASS AND FILTRATION", "remark": "REMARK"})
    for i, r in enumerate(ctx.rooms):
        row = L.row(i)
        g = lambda k: f"{L.col(k)}{row}"
        put(ws, g("no"), expand(ctx, "={RI.no}", L, i), "LinkTag")
        put(ws, g("name"), expand(ctx, "={RI.name}", L, i), "Link", text=True).alignment = Alignment(horizontal="left")
        c = r.get("criteria") or {}
        has = any(not blank(v) for k, v in c.items() if k not in ("source", "code"))
        code, src = (c.get("code", "D"), c.get("source", "")) if has else ("", "")
        if has and blank(src):
            raise ValueError(f"room {r['number']}: criteria have no source id")
        fields = [("crit", "criteria_row", "text", None), ("page", "page", "text", None), ("oa_ach", "min_oa_ach", "num", "d1"),
                  ("oa_lpp", "oa_lps_per_person", "num", "d1"), ("oa_lpm2", "oa_lps_per_m2", "num", "d2"), ("oa_spec", "oa_specified_lps", "num", "d1"),
                  ("sa_ach", "min_total_ach", "num", "d1"), ("sa_lpm2", "supply_lps_m2", "num", "d2"),
                  ("sa_spec", "supply_specified_lps", "num", "d1"), ("ex_ach", "exhaust_ach", "num", "d1"),
                  ("ex_lpm2", "exhaust_lps_m2", "num", "d2"), ("ex_spec", "exhaust_specified_lps", "num", "d1"),
                  ("tmin", "temp_min_c", "num", "d1"), ("tmax", "temp_max_c", "num", "d1"), ("rh", "rh_max_pct", "num", "d0"),
                  ("regime", "pressure_regime", "text", None), ("pa_bk", "pressure_pa_booklet", "num", "d1"),
                  ("pa_td", "pressure_pa_tender", "num", "d1"), ("pa_wd", "pressure_pa_working", "num", "d1"),
                  ("cls", "air_class", "text", None), ("filter", "filter", "text", None), ("single", "single_pass", "text", None),
                  ("tset", "temp_setpoint_c", "num", "d1"),
                  ("after", "afterhours", "text", None)]
        for k, f, kind, fmt in fields:
            _val(ctx, ws, g(k), c.get(f), code, fmt=fmt, kind=kind, cref=g("code"), sref=g("src"), src=src,
                 label=f"criteria {f}", room=r["number"], style=PRES_NAME if c.get("presumed") else None)
        _prov_cells(ctx, ws, g("code"), g("src"), code, src, has)
        # D-110: the HFDS row prints 'Refer to SS553' for outdoor air (same source as the criteria row)
        put(ws, g("ss553"), "Y" if c.get("oa_ss553") else None, "Link").alignment = Alignment(horizontal="center")
        # D-111: SS 553 Table 1 type and rate (or 62.1 for a meeting room, rule N3), with its own source
        t1 = c.get("ss553_t1") or {}
        for k, f, kind, fmt in (("t1type", "kind", "text", None), ("t1_lpm2", "lpm2", "num", "d2"), ("t1_lpp", "lpp", "num", "d1")):
            _val(ctx, ws, g(k), t1.get(f), "D", fmt=fmt, kind=kind, cref=g("t1code"), sref=g("t1src"), src=t1.get("src", ""),
                 label=f"SS 553 {f}", room=r["number"])
        _prov_cells(ctx, ws, g("t1code"), g("t1src"), "D", t1.get("src", ""), bool(t1))
        spc = c.get("sp_class") or None
        _val(ctx, ws, g("spc"), spc, "A", kind="text", cref=g("spc_c"), sref=g("spc_s"), src="GEN-CLASS", label="single-pass class", room=r["number"])
        _prov_cells(ctx, ws, g("spc_c"), g("spc_s"), "A", "GEN-CLASS", spc is not None)
        calc = {
            "pa": f'=IF({g("pa_td")}<>"",{g("pa_td")},IF({g("pa_wd")}<>"",{g("pa_wd")},IF({g("pa_bk")}<>"",{g("pa_bk")},"")))',
            "pdiff": (f'=IF(AND(COUNT({g("pa_bk")},{g("pa_td")},{g("pa_wd")})>1,MAX({g("pa_bk")},{g("pa_td")},{g("pa_wd")})'
                      f'<>MIN({g("pa_bk")},{g("pa_td")},{g("pa_wd")})),"YES","")'),
            "press": f'=IF({g("regime")}="POSITIVE","P",IF({g("regime")}="NEGATIVE","N","-"))',
            # design condition of the room from the printed range (code C): the reference value limited to the range; a printed set point wins
            "td": (f'=IF({g("tset")}<>"",{g("tset")},IF(AND({g("tmin")}="",{g("tmax")}=""),"",'
                   f'MIN(MAX(DesignTRef,IF({g("tmin")}="",{g("tmax")},{g("tmin")})),IF({g("tmax")}="",{g("tmin")},{g("tmax")}))))'),
            "rhd": f'=IF({g("td")}="","",MIN(DesignRHMax,IF({g("rh")}="",DesignRHMax,{g("rh")}/PsyPct)))',
            "pv": f'=IF({g("td")}="","",{g("rhd")}*PsyA*EXP(PsyB*{g("td")}/(PsyC+{g("td")})))',
            "hd": f'=IF({g("td")}="","",PsyCpA*{g("td")}+PsyEps*{g("pv")}/(PsyP-{g("pv")})*(PsyHfg+PsyCpV*{g("td")}))',
            "trange": (f'=IF({g("td")}="","",IF(AND({g("tset")}="",OR({g("tmin")}<>"",{g("tmax")}<>""),'
                       f'OR({g("td")}<IF({g("tmin")}="",{g("tmax")},{g("tmin")}),{g("td")}>IF({g("tmax")}="",{g("tmin")},{g("tmax")}))),"FAIL","PASS"))'),
            "clin": (f'=IF(OR(ISNUMBER({g("pa")}),{g("regime")}="NEGATIVE",{g("single")}="Y",ISNUMBER(SEARCH("HEPA",{g("filter")})),'
                     f'{g("cls")}="1"),"YES","NO")'),
            # D-110: HFDS/170 scope = outdoor air set by air changes or a specified flow (HFDS T.1.1.1.1, ASHRAE 170, MOT NSC), with no per-person or
            # per-area rate and no 'Refer to SS553'. SS 553 scope = any other room with an outdoor-air rate. Blank = no outdoor-air requirement.
            "oascope": (f'=IF(N({g("oa_ach")})+N({g("oa_spec")})+N({g("oa_lpp")})+N({g("oa_lpm2")})=0,"",'
                        f'IF(AND({g("ss553")}<>"Y",N({g("oa_lpp")})=0,N({g("oa_lpm2")})=0),"HFDS/170","SS 553"))'),
        }
        for k, f in calc.items():
            cc = put(ws, g(k), f, "Calc", "d1" if k in ("pa", "td", "hd", "pv") else ("d2" if k == "rhd" else None))
            if k not in ("pa", "td", "hd", "pv", "rhd"):
                cc.alignment = Alignment(horizontal="center")
        put(ws, g("remark"), c.get("remark"), "Note")
    last = L.last
    list_validation(ws, f"{L.col('regime')}{L.first}:{L.col('regime')}{last}", "ListRegime")
    band_rows(ws, f"A{L.first}:{L.col('remark')}{last}")
    cf_text(ws, f"{L.col('pdiff')}{L.first}:{L.col('pdiff')}{last}", f"{L.col('pdiff')}{L.first}", "YES")
    _setup(ws, ctx, L, L.idx["remark"], last, "4:6", f"{L.col('name')}{L.first}", "FF1F4E79")


# --------------------------------------------------------------------------- #
def write_envelope(ctx: Ctx) -> None:
    ws = ctx.wb["Envelope"]
    L = ctx.lay["EN"]
    title_block(ws, "Envelope", "One row per room and façade segment; the engine sums these rows. Gains per m² come from the "
                "orientation table on Basis of Design. Anything assumed is yellow.", ctx)
    write_headers(ws, L, en_spec(), 4, {"room": "ROOM AND SEGMENT", "glz": "AREAS", "shade": "AS DRAWN", "win_wm2": "GAINS",
                                        "remark": "REMARK"})
    for i, e in enumerate(ctx.env_rows):
        row = L.row(i)
        g = lambda k: f"{L.col(k)}{row}"
        code, src = e.get("code", "D"), e.get("source", "")
        if blank(src):
            raise ValueError(f"envelope row {e.get('segment')} of {e['room']} has no source id")
        for k, f, kind, fmt in (("room", "room", "tag", None), ("seg", "segment", "tag", None), ("part", "building_part", "text", None),
                                ("face", "face", "text", None), ("az", "azimuth_deg", "num", "d0"), ("ori", "orientation", "text", None),
                                ("glz", "glazing_m2", "num", "d1"), ("wall", "wall_m2", "num", "d1"), ("roof", "roof_m2", "num", "d1"),
                                ("shade", "shading", "text", None), ("method", "method", "text", None)):
            _val(ctx, ws, g(k), e.get(f), code, fmt=fmt, kind=kind, cref=g("code"), sref=g("src"), src=src,
                 label=f, room=e.get("segment") or e["room"])
        _prov_cells(ctx, ws, g("code"), g("src"), code, src, True)
        calc = {"win_wm2": ("=INDEX(OriWindow,MATCH({ori},OriName,0))", "Link", "d1"),
                "wall_wm2": ("=INDEX(OriWall,MATCH({ori},OriName,0))", "Link", "d1"),
                "fen_w": ("={glz}*{win_wm2}", "Calc", "d0"), "wall_w": ("={wall}*{wall_wm2}", "Calc", "d0"),
                "roof_w": ("={roof}*RoofSolarGain", "Calc", "d0")}
        for k, (f, st, fmt) in calc.items():
            put(ws, g(k), expand(ctx, f, L, i), st, fmt)
        put(ws, g("remark"), e.get("remark"), "Note")
    last = L.last
    list_validation(ws, f"{L.col('ori')}{L.first}:{L.col('ori')}{last}", "ListOrientation")
    band_rows(ws, f"A{L.first}:{L.col('remark')}{last}")
    _setup(ws, ctx, L, L.idx["remark"], L.first + max(len(ctx.env_rows), 1) - 1, "4:6", f"{L.col('seg')}{L.first}", "FF1F4E79")


# --------------------------------------------------------------------------- #
ENG_F: Dict[str, str] = {
    "lvl": "={RI.storey}", "ahu": "={RI.ahu}", "zone": "={RI.zone}", "no": "={RI.no}", "name": "={RI.name}",
    "sys": "={RI.systype}", "sptype": "={RI.sptype}", "cond": "={RI.cond}", "h": "={RI.h}", "area": "={RI.area}",
    "vol": "={area}*{h}", "glz": "={RI.glz}", "wall": "={RI.wall}", "roof": "={RI.roof}",
    "nenv": "=COUNTIF([EN.room],{no})",
    "occ": "={RI.people}", "iof": '=IF({RI.iof}="",1,{RI.iof})', "oa_ach": "={CR.oa_ach}", "oa_lpp": "={CR.oa_lpp}", "oa_lpm2": "={CR.oa_lpm2}", "oa_spec": "={CR.oa_spec}",
    # D-110 (26 Nov 2026): the occupant diversity (IOF, D-106) no longer reduces outdoor air (HFDS/170 rule C4, C5; SS 553 per area)
    # D-111: SS 553 rooms: Table 1 rate x area, or 62.1 Rp x occupancy + Ra x area (meeting); cl 5.3.5 store or riser below 6 m2 and storage-only
    # cold room exempt (0); QP rate (cl 5.3.6) and a store of 6 m2 or more keep the booklet rate below
    "oa": ('=IF(AND({fix}=1,{oa_spec}>0),{oa_spec},IF(OR({CR.t1type}="COLD ROOM (cl 5.3.5)",AND({CR.t1type}="STORE OR RISER (cl 5.3.5)",{area}<6)),0,'
           'IF(N({CR.t1_lpm2})+N({CR.t1_lpp})>0,N({CR.t1_lpp})*{occ}+N({CR.t1_lpm2})*{area},'
           'MAX({occ}*{oa_lpp},{oa_lpm2}*{area},{oa_ach}*{vol}/ConvM3hPerLps,{oa_spec}))))'),
    "sa_ach": "={CR.sa_ach}", "sa_lpm2": "={CR.sa_lpm2}", "sa_spec": "={CR.sa_spec}",
    "sa_lps": "=MAX({sa_lpm2}*{area},{sa_ach}*{vol}/ConvM3hPerLps,{sa_spec},{oa})",       # the outdoor air is a floor of the supply
    "ex_ach": "={CR.ex_ach}", "ex_lpm2": "={CR.ex_lpm2}", "ex_spec": "={CR.ex_spec}",
    "ex_lps": "=MAX({area}*{ex_lpm2},{ex_ach}*{vol}/ConvM3hPerLps)",
    "ex_fin": "=IF({ex_spec}>0,{ex_spec},IF({ex_lps}>0,ROUNDUP({ex_lps},RoundTo),0))",
    "press": "={CR.press}",
    "ppl": "={occ}", "act": "={RI.act}",
    "ppl_sw": "=IF({ppl}>0,INDEX(ActSensible,MATCH({act},ActName,0)),0)", "ppl_w": "={ppl_sw}*{ppl}",
    "lgt_wm2": "={RI.light}", "lgt_w": "={lgt_wm2}*{area}", "pwr_wm2": "={RI.power}", "eq_w": "={RI.eqs}",
    "pwr_w": "={pwr_wm2}*{area}+{eq_w}",
    "fen_w": "=SUMIFS([EN.fen_w],[EN.room],{no})", "fen_wm2": "=IF({area}>0,{fen_w}/{area},0)",
    "trn_w": "=SUMIFS([EN.wall_w],[EN.room],{no})+SUMIFS([EN.roof_w],[EN.room],{no})",
    "trn_wm2": "=IF({area}>0,{trn_w}/{area},0)",
    "inf_ach": '=IF(OR({press}="P",{nenv}=0),0,InfiltrationACH)', "inf_lps": "={inf_ach}*{vol}/ConvM3hPerLps",
    "inf_w": "=(INDEX(CondDB,MATCH({cond},CondName,0))-{troom})*AirDensity*{inf_lps}",
    "lat_ppl_wp": "=IF({ppl}>0,INDEX(ActLatent,MATCH({act},ActName,0)),0)", "lat_ppl": "={lat_ppl_wp}*{ppl}",
    "lat_eq": "={RI.eql}",
    "lat_inf_w": "=(INDEX(CondH,MATCH({cond},CondName,0))-{hroom})*AirLatentFactor*{inf_lps}-{inf_w}",
    "sens": "=({ppl_w}+{lgt_w}+{pwr_w}+{fen_w}+{trn_w}+{inf_w})*(1+SafetyFactor)",
    "sens_wm2": "=IF({area}>0,{sens}/{area},0)",
    "lat": "=({lat_inf_w}+{lat_eq}+{lat_ppl})*(1+SafetyFactor)", "lat_wm2": "=IF({area}>0,{lat}/{area},0)",
    "tot": "={sens}+{lat}", "tot_wm2": "=IF({area}>0,{tot}/{area},0)",
    "troom": '=IF({CR.td}<>"",{CR.td},INDEX(SpaceTypeT,MATCH({sptype},SpaceTypeName,0)))',
    "hroom": '=IF({CR.td}<>"",{CR.hd},INDEX(SpaceTypeH,MATCH({sptype},SpaceTypeName,0)))',
    "tsup": "=SupplyTemp",
    "th_lps": "={sens}/(AirSensibleFactor*({troom}-{tsup}))",
    "fix": '=IF({CR.code}="S",1,0)', "pm": "={AF.pm}",
    "fin_unr": "=IF(AND({fix}=1,{sa_spec}>0),{sa_spec},MAX({th_lps},{sa_lps},{pm}))",
    "fin": "=IF(AND({fix}=1,{sa_spec}>0),{fin_unr},ROUNDUP({fin_unr},RoundTo))",
    "fin_m3h": "={fin}*ConvM3hPerLps", "ach": "=IF({vol}>0,{fin_m3h}/{vol},0)",
    "voc": "={oa}", "vsc": "={fin}", "frac": "=IF({vsc}>0,{voc}/{vsc},0)",
}
ENG_FMT = {"lvl": None, "ahu": None, "zone": None, "no": None, "name": None, "sys": None, "sptype": None, "cond": None,
           "act": None, "press": None, "iof": "d2", "nenv": "d0", "ppl": "d1", "oa_ach": "d1", "sa_ach": "d1", "ex_ach": "d1",
           "inf_ach": "d1", "ach": "d1", "frac": "pct1", "area": "d1", "vol": "d1", "h": "d1"}
TEXT_KEYS = {"lvl", "ahu", "zone", "no", "name", "sys", "sptype", "cond", "act", "press"}
TAG_KEYS = {"ahu", "zone", "no", "lvl"}
SUM_KEYS = {"area", "vol", "glz", "wall", "roof", "occ", "oa", "sa_lps", "ex_lps", "ex_fin", "ppl", "ppl_w", "lgt_w", "pwr_w", "fen_w",
            "trn_w", "inf_lps", "inf_w", "lat_ppl", "lat_eq", "lat_inf_w", "sens", "lat", "tot", "th_lps", "pm", "fin_unr", "fin",
            "fin_m3h", "voc", "vsc"}
STYLE_OF = {"L": "Link", "C": "Calc", "R": "Result"}


def write_engines(ctx: Ctx) -> None:
    for st in ctx.storeys:
        E = ctx.eng_layouts[st]
        ws = ctx.wb[E.sheet]
        title_block(ws, E.sheet, f"Room loads for {ctx.label(st)}. Inputs arrive from "
                    "Room Information, Criteria and Envelope (green); the code row under each header gives the cell code of the "
                    "whole column. Detail columns are grouped and can be collapsed.", ctx)
        spec = [(k, h, u, w) for k, h, u, w, c, grp in ENG]
        codes = {k: c for k, h, u, w, c, grp in ENG}
        bands = {}
        seen = set()
        for k, h, u, w, c, grp in ENG:
            if grp not in seen:
                bands[k] = grp
                seen.add(grp)
        write_headers(ws, E, spec, 4, bands, code_row=codes)
        put(ws, f"{E.col('lvl')}7", "Code", "Unit")
        rooms_st = [(gi, r) for gi, r in enumerate(ctx.rooms) if r["storey"] == st]
        for j, (gi, r) in enumerate(rooms_st):
            ctx.room_eng[gi] = (E, j)
            for k, h, u, w, c, grp in ENG:
                f = expand(ctx, ENG_F[k], E, j, {"RI": gi, "CR": gi, "AF": gi})
                style = STYLE_OF[c]
                if k in TAG_KEYS:
                    style = tag_variant(style)
                cell = put(ws, E.local(k, j), f, style, ENG_FMT.get(k, "d1"), text=k in TEXT_KEYS)
                if k in TEXT_KEYS:
                    cell.alignment = Alignment(horizontal="left", vertical="center")
        # totals
        tr = E.last + 1 if E.n else E.first
        put(ws, f"{E.col('no')}{tr}", "TOTAL", "ResultTag")
        put(ws, f"{E.col('name')}{tr}", f"Rooms on {ctx.label(st)}", "Total").alignment = Alignment(horizontal="left")
        for k in SUM_KEYS:
            if E.n:
                put(ws, f"{E.col(k)}{tr}", f"=SUM({E.col(k)}{E.first}:{E.col(k)}{E.last})", "Total",
                    ENG_FMT.get(k, "d1"))
        put(ws, f"{E.col('ach')}{tr}", f"=IF({E.col('vol')}{tr}>0,{E.col('fin_m3h')}{tr}/{E.col('vol')}{tr},0)", "Total", "d1")
        put(ws, f"{E.col('frac')}{tr}", f"=MAX({E.col('frac')}{E.first}:{E.col('frac')}{E.last})", "Total", "pct1")
        ctx.eng_total_row[st] = tr
        band_rows(ws, f"A{E.first}:{E.col('frac')}{E.last}")
        cf_formula(ws, f"{E.col('fin')}{E.first}:{E.col('fin')}{E.last}",
                   f"{E.col('fin')}{E.first}<{E.col('sa_lps')}{E.first}")
        group_cols(ws, E, "glz", "nenv", collapsed=True)
        group_cols(ws, E, "oa_lpp", "oa_spec", collapsed=True)
        group_cols(ws, E, "sa_lpm2", "sa_spec", collapsed=True)
        group_cols(ws, E, "ex_ach", "ex_spec", collapsed=True)
        group_cols(ws, E, "ppl", "inf_w", collapsed=True)
        group_cols(ws, E, "lat_ppl_wp", "lat_inf_w", collapsed=True)
        _setup(ws, ctx, E, E.idx["frac"], tr, "4:7", f"{E.col('name')}{E.first}", "FF0070C0")


# --------------------------------------------------------------------------- #
AF_F: Dict[str, str] = {
    "no": "={RI.no}", "name": "={RI.name}", "storey": "={RI.storey}", "ahu": "={RI.ahu}", "sys": "={RI.systype}",
    "area": "={ENG.area}", "vol": "={ENG.vol}", "sens": "={ENG.sens}", "lat": "={ENG.lat}", "troom": "={ENG.troom}",
    "hroom": "={ENG.hroom}", "ac": "={ENG.sa_lps}", "th": "={ENG.th_lps}",
    "gov": '=IF({supply}=0,"NONE",IF({fix}=1,"SPECIFIED",IF({pm}>MAX({th},{ac}),"PRESSURE",IF({th}>={ac},"THERMAL","AIR CHANGE"))))',
    "margin": "=ABS({th}-{ac})", "ex_min": "={ENG.ex_fin}",
    "pm": '=IF({kind}="EXHAUST",MAX(0,{ex_min}-{off}),IF({kind}="SUPPLY",{ex_min}+{off},0))',
    "supply": "={ENG.fin}",
    "extract": '=IF(AND({fix}=1,{ex_min}>0),{ex_min},IF({kind}="EXHAUST",{supply}+{off},IF({kind}="SUPPLY",{supply}-{off},MAX({supply},{ex_min}))))',
    "single": "={CR.single}",
    "exhaust": '=IF({single}="Y",{extract},IF({kind}="EXHAUST",MAX({ex_min},{off}),{ex_min}))',
    "ret": "={extract}-{exhaust}", "oa_air": "={ENG.oa}",
    "spc": "={CR.spc}",
    # a clean single-pass room is 100 % outdoor air; any other room carries its own minimum outdoor air. The exhaust and the offset air are made up at the AHU.
    "oa": '=IF({supply}>0,MIN({supply},IF(AND({fix}=1,{oa_spec}>0),{oa_spec},IF({spc}="CLEAN",{supply},{oa_air}))),0)',
    "th_lo": '=IF(AND({CR.td}<>"",{CR.tmin}<>""),IF({CR.tmin}>SupplyTemp,{sens}/(AirSensibleFactor*({CR.tmin}-SupplyTemp)),""),"")',
    "lo_chk": '=IF(ISNUMBER({th_lo}),IF({th_lo}>{supply}+OffsetEps,"VERIFY",""),"")',
    "env_chk": '=IF({ENG.nenv}>0,IF(OR({gov}="THERMAL",{th}>=(1-EnvCheckShare)*{ac}),"VERIFY",""),"")',
    "frac": "=IF({supply}>0,{oa}/{supply},0)",
    "minflow": "=MIN({supply},MAX({ac},{oa},{pm}))",
    "ok": '=IF({supply}>=MAX({th},{ac},{pm}),"PASS",IF({fix}=1,"SPECIFIED","FAIL"))',
    "fix": "={ENG.fix}", "oa_spec": "={ENG.oa_spec}",
    "spec_off": '=IF(AND({fix}=1,{kind}<>""),IF({kind}="EXHAUST",{extract}-{supply},{supply}-{extract}),"")',
    "off_chk": '=IF(ISNUMBER({spec_off}),IF({spec_off}>={off}-BalTolLps,"OK","SHORT"),"")',
    "terminal": "={RI.terminal}", "exfan": "={RI.exfan}", "clin": '=IF({fix}=1,"YES",{CR.clin})', "systype": "={RI.systype}",
    "oascope": '=IF(AND({CR.oascope}="",OR({fix}=1,{spc}="CLEAN"),{oa}>0),"HFDS/170",{CR.oascope})',
    # D-117 (Marcus, 26 Nov 2026): operating theatres (MOT-specified operating rooms and clean single-pass rooms) in both modes:
    # single pass (outdoor air = supply) and normal (HFDS outdoor air changes, 5 ACH for an operating theatre, T.1.1.1.1 row 24, capped at the
    # supply); the pressure targets hold at the AHU (balance basis). Other rooms keep their outdoor air in both modes.
    "isot": '=IF(OR({spc}="CLEAN",AND({fix}=1,ISNUMBER(SEARCH("operating",{name})))),1,0)',
    "oa_sp": '=IF({supply}>0,IF({isot}=1,{supply},{oa}),0)',
    "oa_norm": '=IF({supply}>0,IF({isot}=1,MIN({supply},IF(N({CR.oa_ach})>0,{CR.oa_ach},OTNormalOAACH)*{vol}/ConvM3hPerLps),{oa}),0)',
}
AF_STYLE = {"no": "LinkTag", "name": "Link", "storey": "Link", "ahu": "LinkTag", "sys": "Link", "terminal": "LinkTag",
            "exfan": "LinkTag", "clin": "Calc", "oascope": "Calc", "isot": "Calc", "oa_sp": "Calc", "oa_norm": "Calc", "systype": "Link", "single": "Link", "ok": "Check", "gov": "Calc",
            "supply": "Result", "oa": "Calc", "off_chk": "Check", "spc": "Link", "lo_chk": "Check", "env_chk": "Check"}
AF_LINK = {"fix", "oa_spec", "area", "vol", "sens", "lat", "troom", "hroom", "ac", "th", "ex_min", "supply", "oa_air", "spc"}


def write_airflow(ctx: Ctx) -> None:
    ws = ctx.wb["Airflow"]
    L = ctx.lay["AF"]
    title_block(ws, "Airflow", "Both cases for every room: air-change airflow and thermal-load airflow; the larger governs, "
                "not less than exhaust and pressure require. Extract = supply + offset (negative room) or supply − offset "
                "(positive room). The last columns apply the outdoor-air rule for small rooms in rounds.", ctx)
    spec = af_spec()
    bands = {"no": "ROOM", "ac": "AIRFLOW CASES", "ex_min": "EXHAUST AND PRESSURE", "supply": "RESULT", "oa_air": "OUTDOOR AIR",
             "terminal": "SYSTEM KEYS", "in1": "OUTDOOR-AIR RULE, ROUND BY ROUND", "status": "OUTCOME"}
    write_headers(ws, L, spec, 4, bands)
    for i, r in enumerate(ctx.rooms):
        E, j = ctx.room_eng[i]
        eng = lambda k: E.addr(k, j)
        row = L.row(i)
        g = lambda k: f"{L.col(k)}{row}"
        # pressure link (static knowledge of the node)
        node = ctx.ps_index.get(r["number"])
        ctrl = node is not None and node["controlled"]
        for k, f in AF_F.items():
            f = re.sub(r"\{ENG\.(\w+)\}", lambda m: eng(m.group(1)), f)
            f = expand(ctx, f, L, i, {"RI": i, "CR": i})
            st = AF_STYLE.get(k, "Link" if k in AF_LINK else "Calc")
            fmt = None if k in ("no", "name", "storey", "ahu", "sys", "gov", "single", "ok", "terminal", "exfan", "clin", "oascope", "systype", "off_chk", "spc", "lo_chk", "env_chk") \
                else ("pct1" if k == "frac" else ("d0" if k == "fix" else "d1"))
            c = put(ws, g(k), f, st, fmt, text=fmt is None and st != "Check")
            if fmt is None:
                c.alignment = Alignment(horizontal="left" if st != "Check" else "center", vertical="center")
        # kind / off
        if ctrl:
            n = ctx.lay["PS"]
            q = node["index"]
            put(ws, g("kind"), f"={n.addr('kindoff', q)}", "Link").alignment = Alignment(horizontal="left")
            put(ws, g("off"), f"={n.addr('off_lps', q)}", "Link", "d1")
        else:
            put(ws, g("kind"), None, "Link")
            put(ws, g("off"), None, "Link", "d1")
        # outdoor-air rule rounds
        for rr in range(1, OA_ROUNDS + 1):
            inn = g(f"in{rr}")
            if rr == 1:
                put(ws, inn, f'=IF(AND({g("supply")}>0,{g("ahu")}<>""),1,0)', "Calc", "d0")
            else:
                put(ws, inn, f"={g(f'in{rr-1}')}-{g(f'sep{rr-1}')}", "Calc", "d0")
            put(ws, g(f"tot{rr}"), expand(ctx, f"=SUMIFS([AF.supply],[AF.ahu],{{ahu}},[AF.in{rr}],1)", L, i), "Calc", "d1")
            put(ws, g(f"max{rr}"), expand(ctx, f"=_xlfn.MAXIFS([AF.frac],[AF.ahu],{{ahu}},[AF.in{rr}],1)", L, i), "Calc", "pct1")
            put(ws, g(f"nxt{rr}"), expand(
                ctx, f"=SUMPRODUCT(MAX(([AF.ahu]={{ahu}})*([AF.in{rr}]=1)*(ROW([AF.frac])<>ROW())*[AF.frac]))", L, i), "Calc", "pct1")
            # D-113 (26 Nov 2026, Marcus): no FCU separation on an HFDS/170 unit (straight sum: separating a room does not lower the outdoor air)
            ah_ = ctx.lay.get("AH")
            hf_ = "TRUE"   # D-116 (Marcus, 26 Nov 2026): the rule is withdrawn on every unit (outdoor air is a sum: separating a room lowers nothing)
            put(ws, g(f"trg{rr}"), f"=IF(AND({inn}=1,NOT({hf_})),IF(AND({g('frac')}>0,{g('frac')}={g(f'max{rr}')},"
                                   f"{g('supply')}/{g(f'tot{rr}')}<OAShare,{g(f'max{rr}')}-{g(f'nxt{rr}')}>OADrop),1,0),0)",
                "Calc", "d0")
            put(ws, g(f"sep{rr}"), f'=IF({g("clin")}="YES",0,{g(f"trg{rr}")})', "Calc", "d0")
            put(ws, g(f"lst{rr}"), f'=IF({g("clin")}="YES",{g(f"trg{rr}")},0)', "Calc", "d0")
        sums = lambda p: "+".join(g(f"{p}{x}") for x in range(1, OA_ROUNDS + 1))
        put(ws, g("status"), f'=IF({sums("sep")}>0,"SEPARATE",IF({sums("lst")}>0,"LIST FOR REVIEW",""))', "Calc").alignment = \
            Alignment(horizontal="left")
        put(ws, g("sepf"), f"=MIN(1,{sums('sep')})", "Calc", "d0")
        put(ws, g("cmark0"), f'=IF(AND({g("in1")}=1,{g("frac")}>0,{g("frac")}={g("max1")}),{g("ahu")},"")', "CalcTag")
        put(ws, g("cmark1"), expand(
            ctx, f'=IF(AND({g("sepf")}=0,{g("supply")}>0,{g("frac")}>0,{g("frac")}=_xlfn.MAXIFS([AF.frac],[AF.ahu],{g("ahu")},[AF.sepf],0)),{g("ahu")},"")',
            L, i), "CalcTag")
        put(ws, g("ref0"), expand(
            ctx, f'=IF(AND({g("supply")}>0,COUNTIFS({L.upto("ahu", i)},{g("ahu")},{L.upto("supply", i)},">0")=1),{g("ahu")},"")', L, i), "CalcTag")
        put(ws, g("ref1"), expand(
            ctx, f'=IF(AND({g("sepf")}=0,{g("supply")}>0,COUNTIFS({L.upto("ahu", i)},{g("ahu")},{L.upto("supply", i)},">0",{L.upto("sepf", i)},0)=1),{g("ahu")},"")',
            L, i), "CalcTag")
        put(ws, g("fcu"), f'=IF(OR({g("systype")}="FCU",{g("sepf")}=1),1,0)', "Calc", "d0")
        put(ws, g("flgrank"), f'=IF(OR({g("status")}="SEPARATE",{g("status")}="LIST FOR REVIEW"),'
                              f'COUNTIF({L.upto("status", i)},"SEPARATE")+COUNTIF({L.upto("status", i)},"LIST FOR REVIEW"),0)', "Calc", "d0")
        put(ws, g("fcurank"), f'=IF({g("fcu")}=1,SUM({L.upto("fcu", i)}),0)', "Calc", "d0")
    last = L.last
    band_rows(ws, f"A{L.first}:{L.col('fcurank')}{last}")
    cf_text(ws, f"{L.col('ok')}{L.first}:{L.col('ok')}{last}", f"{L.col('ok')}{L.first}", "FAIL")
    cf_text(ws, f"{L.col('ok')}{L.first}:{L.col('ok')}{last}", f"{L.col('ok')}{L.first}", "SPECIFIED", fill="FFFFF2CC")
    cf_text(ws, f"{L.col('off_chk')}{L.first}:{L.col('off_chk')}{last}", f"{L.col('off_chk')}{L.first}", "SHORT")
    for tx, fill in (("THERMAL", "FFDDEBF7"), ("AIR CHANGE", "FFE2EFDA"), ("PRESSURE", "FFFCE4D6")):
        cf_text(ws, f"{L.col('gov')}{L.first}:{L.col('gov')}{last}", f"{L.col('gov')}{L.first}", tx, bold=True, fill=fill)
    cf_formula(ws, f"{L.col('supply')}{L.first}:{L.col('supply')}{last}",
               f"{L.col('supply')}{L.first}<{L.col('ac')}{L.first}")
    cf_text(ws, f"{L.col('status')}{L.first}:{L.col('status')}{last}", f"{L.col('status')}{L.first}", "SEPARATE", fill="FFFFF2CC")
    group_cols(ws, L, "terminal", "fcurank", collapsed=True)      # one contiguous group: keys and the outdoor-air rule rounds
    _setup(ws, ctx, L, L.idx["fcurank"], last, "4:6", f"{L.col('name')}{L.first}", "FF00B050")
