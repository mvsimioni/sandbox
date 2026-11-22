"""Room Information, Criteria, Envelope, the Room Zones engines and Airflow."""

from __future__ import annotations

import re
from typing import Any, Dict, List

from openpyxl.styles import Alignment

from .common import (band_rows, cf_formula, cf_text, expand, group_cols, list_validation, title_block, write_headers)
from .ctx import Ctx
from .layouts import (ENG, ENG_FIRST, FIRST, OA_ROUNDS, af_spec, cr_spec, en_spec, ri_spec)
from .model import blank, prov_of
from .styles import CODE_STYLES, put, setup_sheet, tag_variant

X = lambda v: None if blank(v) else v


def _setup(ws, ctx: Ctx, lay, ncols_print: int, last_row: int, print_rows: str, freeze: str, colour: str = None) -> None:
    setup_sheet(ws, ws.title, rev=ctx.rev, date=ctx.date, project=ctx.project, print_rows=print_rows,
                last_col=ncols_print, last_row=last_row, tab_colour=colour, freeze=freeze)


def _val(ctx: Ctx, ws, ref: str, v: Any, code: str, *, fmt=None, kind="num", cref: str = None, sref: str = None, src: str = "",
         label: str = "", room: str = "") -> None:
    """Input value with the group's shared code and source cells."""
    has = not blank(v)
    style = CODE_STYLES[code][0] if (has and code) else "Input"
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
                                   "soa": "AREA", "h": "HEIGHT", "pfix": "OCCUPANCY AND OPERATION", "light": "GAINS",
                                   "glz": "FENESTRATION (from Envelope)", "remark": "SOURCE REMARK"})
    for i, r in enumerate(ctx.rooms):
        row = L.row(i)
        n = r["number"]
        g = lambda k: f"{L.col(k)}{row}"
        # identity
        code, src = prov_of(r, "identity")
        for k, v in (("no", n), ("name", r.get("name")), ("storey", r.get("storey"))):
            _val(ctx, ws, g(k), v, code, kind="tag" if k == "no" else "text", cref=g("id_c"), sref=g("id_s"), src=src,
                 label=f"room {k}", room=n)
        _prov_cells(ctx, ws, g("id_c"), g("id_s"), code, src, True)
        # zoning
        zf = [("ahu", "ahu", "tag"), ("zone", "zone_tag", "tag"), ("systype", "system_type", "text"),
              ("terminal", "terminal_tag", "tag"), ("exfan", "exhaust_fan", "tag"), ("oasrc", "oa_source", "tag"),
              ("oacond", "oa_condition", "text")]
        anyv = any(not blank(r.get(f)) for _, f, _ in zf)
        zc, zs = prov_of(r, "zoning", anyv)
        for k, f, kind in zf:
            _val(ctx, ws, g(k), r.get(f), zc, kind=kind, cref=g("zon_c"), sref=g("zon_s"), src=zs, label=f"room {f}", room=n)
        _prov_cells(ctx, ws, g("zon_c"), g("zon_s"), zc, zs, anyv)
        # single-value groups
        simple = [("sptype", "space_type", "space_type", "text", None), ("cond", "room_condition", "space_type", "text", None),
                  ("soa", "soa_area_m2", "soa_area", "num", "d1"), ("meas", "measured_area_m2", "measured_area", "num", "d1"),
                  ("sel", "area_selector", "area_selector", "text", None), ("h", "ceiling_height_m", "ceiling_height", "num", "d2"),
                  ("pfix", "occupancy_people", "occupancy_people", "num", "d1"),
                  ("dens", "occupancy_per_100m2", "occupancy_density", "num", "d1"), ("iof", "iof", "iof", "num", "d2"),
                  ("act", "activity", "activity", "text", None), ("hours", "operation_hours", "operation_hours", "num", "d1"),
                  ("light", "lighting_w_m2", "lighting", "num", "d1"), ("power", "small_power_w_m2", "small_power", "num", "d1"),
                  ("eqs", "equipment_sensible_w", "equipment_sensible", "num", "d0"),
                  ("eql", "equipment_latent_w", "equipment_latent", "num", "d0")]
        for k, f, grp, kind, fmt in simple:
            v = r.get(f)
            if k == "sel" and blank(v):
                v = "AUTO"
                code_s, src_s = ("S", "GEN-CONFIG")
            elif k == "cond" and not blank(v):
                code_s, src_s = prov_of(r, "space_type", True)
            else:
                code_s, src_s = prov_of(r, grp, not blank(v))
            _val(ctx, ws, g(k), v, code_s, fmt=fmt, kind=kind, cref=g(k + "_c"), sref=g(k + "_s"), src=src_s,
                 label=f, room=n)
            _prov_cells(ctx, ws, g(k + "_c"), g(k + "_s"), code_s, src_s, not blank(v))
        # calculated
        soa, meas, sel = g("soa"), g("meas"), g("sel")
        agree = (f'=IF(OR({soa}="",{meas}=""),"",IF(OR(ABS({soa}-{meas})<=AreaTolM2,ABS({soa}-{meas})<=AreaTolPct*{soa}),'
                 f'"YES","NO"))')
        area = (f'=IF({sel}="SOA",{soa},IF({sel}="MEASURED",{meas},IF({sel}="LARGER",MAX({soa},{meas}),'
                f'IF({meas}="",{soa},IF({soa}="",{meas},IF({g("agree")}="YES",{soa},{meas}))))))')
        for k, f, st, fmt in (("area", area, "Calc", "d1"),
                              ("diff", f'=IF(AND({soa}<>"",{meas}<>"",{soa}<>0),({soa}-{meas})/{soa},"")', "Calc", "pct1"),
                              ("agree", agree, "Calc", None),
                              ("vol", f"={g('area')}*{g('h')}", "Calc", "d1"),
                              ("people", f'=IF({g("pfix")}<>"",{g("pfix")},{g("area")}*{g("dens")}/PercentBase)', "Calc", "d1"),
                              ("glz", f"=SUMIFS([EN.glz],[EN.room],{g('no')})", "Link", "d1"),
                              ("wall", f"=SUMIFS([EN.wall],[EN.room],{g('no')})", "Link", "d1"),
                              ("roof", f"=SUMIFS([EN.roof],[EN.room],{g('no')})", "Link", "d1"),
                              ("face", f'=IF(COUNTIF([EN.room],{g("no")})=0,"Internal",INDEX([EN.face],MATCH({g("no")},[EN.room],0))'
                                       f'&" "&INDEX([EN.az],MATCH({g("no")},[EN.room],0))&"°, "&INDEX([EN.shade],MATCH({g("no")},[EN.room],0))'
                                       f'&IF(COUNTIF([EN.room],{g("no")})>1," (+"&(COUNTIF([EN.room],{g("no")})-1)&" more)",""))',
                               "Link", None)):
            f = expand(ctx, f, L, i)
            c = put(ws, g(k), f, st, fmt)
            if k in ("agree", "face"):
                c.alignment = Alignment(horizontal="left" if k == "face" else "center", vertical="center")
        put(ws, g("remark"), r.get("remark"), "Note")
    last = L.last
    list_validation(ws, f"{L.col('sptype')}{L.first}:{L.col('sptype')}{last}", "ListSpaceType")
    list_validation(ws, f"{L.col('cond')}{L.first}:{L.col('cond')}{last}", "ListCondition")
    list_validation(ws, f"{L.col('sel')}{L.first}:{L.col('sel')}{last}", "ListAreaSource")
    list_validation(ws, f"{L.col('systype')}{L.first}:{L.col('systype')}{last}", "ListSystemType")
    list_validation(ws, f"{L.col('act')}{L.first}:{L.col('act')}{last}", "ListActivity")
    list_validation(ws, f"{L.col('oacond')}{L.first}:{L.col('oacond')}{last}", "ListOACond")
    band_rows(ws, f"A{L.first}:{L.col('remark')}{last}")
    cf_text(ws, f"{L.col('agree')}{L.first}:{L.col('agree')}{last}", f"{L.col('agree')}{L.first}", "NO")
    group_cols(ws, L, "id_c", "id_s")
    group_cols(ws, L, "zon_c", "zon_s")
    _setup(ws, ctx, L, L.idx["remark"], last, "4:6", f"{L.col('name')}{L.first}", "FF1F4E79")


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
        put(ws, g("name"), expand(ctx, "={RI.name}", L, i), "Link").alignment = Alignment(horizontal="left")
        c = r.get("criteria") or {}
        has = any(not blank(v) for k, v in c.items() if k not in ("source", "code"))
        code, src = (c.get("code", "D"), c.get("source", "")) if has else ("", "")
        if has and blank(src):
            raise ValueError(f"room {r['number']}: criteria have no source id")
        fields = [("crit", "criteria_row", "text", None), ("page", "page", "text", None), ("oa_ach", "min_oa_ach", "num", "d1"),
                  ("oa_lpp", "oa_lps_per_person", "num", "d1"), ("oa_spec", "oa_specified_lps", "num", "d1"),
                  ("sa_ach", "min_total_ach", "num", "d1"), ("sa_lpm2", "supply_lps_m2", "num", "d2"),
                  ("sa_spec", "supply_specified_lps", "num", "d1"), ("ex_ach", "exhaust_ach", "num", "d1"),
                  ("ex_lpm2", "exhaust_lps_m2", "num", "d2"), ("ex_spec", "exhaust_specified_lps", "num", "d1"),
                  ("tmin", "temp_min_c", "num", "d1"), ("tmax", "temp_max_c", "num", "d1"), ("rh", "rh_max_pct", "num", "d0"),
                  ("regime", "pressure_regime", "text", None), ("pa_bk", "pressure_pa_booklet", "num", "d1"),
                  ("pa_td", "pressure_pa_tender", "num", "d1"), ("pa_wd", "pressure_pa_working", "num", "d1"),
                  ("cls", "air_class", "text", None), ("filter", "filter", "text", None), ("single", "single_pass", "text", None),
                  ("after", "afterhours", "text", None)]
        for k, f, kind, fmt in fields:
            _val(ctx, ws, g(k), c.get(f), code, fmt=fmt, kind=kind, cref=g("code"), sref=g("src"), src=src,
                 label=f"criteria {f}", room=r["number"])
        _prov_cells(ctx, ws, g("code"), g("src"), code, src, has)
        calc = {
            "pa": f'=IF({g("pa_td")}<>"",{g("pa_td")},IF({g("pa_wd")}<>"",{g("pa_wd")},IF({g("pa_bk")}<>"",{g("pa_bk")},"")))',
            "pdiff": (f'=IF(AND(COUNT({g("pa_bk")},{g("pa_td")},{g("pa_wd")})>1,MAX({g("pa_bk")},{g("pa_td")},{g("pa_wd")})'
                      f'<>MIN({g("pa_bk")},{g("pa_td")},{g("pa_wd")})),"YES","")'),
            "press": f'=IF({g("regime")}="POSITIVE","P",IF({g("regime")}="NEGATIVE","N","-"))',
            "clin": (f'=IF(OR(ISNUMBER({g("pa")}),{g("regime")}="NEGATIVE",{g("single")}="Y",ISNUMBER(SEARCH("HEPA",{g("filter")})),'
                     f'{g("cls")}="1"),"YES","NO")'),
        }
        for k, f in calc.items():
            cc = put(ws, g(k), f, "Calc", "d1" if k == "pa" else None)
            if k != "pa":
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
                 label=f"envelope {f}", room=e["room"])
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
    _setup(ws, ctx, L, L.idx["remark"], last, "4:6", f"{L.col('seg')}{L.first}", "FF1F4E79")


# --------------------------------------------------------------------------- #
ENG_F: Dict[str, str] = {
    "lvl": "={RI.storey}", "ahu": "={RI.ahu}", "zone": "={RI.zone}", "no": "={RI.no}", "name": "={RI.name}",
    "sys": "={RI.systype}", "sptype": "={RI.sptype}", "cond": "={RI.cond}", "h": "={RI.h}", "area": "={RI.area}",
    "vol": "={area}*{h}", "glz": "={RI.glz}", "wall": "={RI.wall}", "roof": "={RI.roof}",
    "nenv": "=COUNTIF([EN.room],{no})",
    "occ": "={RI.people}", "iof": "={RI.iof}", "oa_ach": "={CR.oa_ach}", "oa_lpp": "={CR.oa_lpp}", "oa_spec": "={CR.oa_spec}",
    "oa": "=MAX({occ}*{oa_lpp}*{iof},{oa_ach}*{vol}/ConvM3hPerLps,{oa_spec})",
    "sa_ach": "={CR.sa_ach}", "sa_lpm2": "={CR.sa_lpm2}", "sa_spec": "={CR.sa_spec}",
    "sa_lps": "=MAX({sa_lpm2}*{area},{sa_ach}*{vol}/ConvM3hPerLps,{sa_spec})",
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
    "troom": "=INDEX(SpaceTypeT,MATCH({sptype},SpaceTypeName,0))", "hroom": "=INDEX(SpaceTypeH,MATCH({sptype},SpaceTypeName,0))",
    "tsup": "=SupplyTemp",
    "th_lps": "={sens}/(AirSensibleFactor*({troom}-{tsup}))",
    "pm": "={AF.pm}", "fin_unr": "=MAX({th_lps},{sa_lps},{pm})", "fin": "=ROUNDUP({fin_unr},RoundTo)",
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
        title_block(ws, E.sheet, f"Load engine of the NDH template / Rev A example for {ctx.label(st)}. Inputs arrive from "
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
                cell = put(ws, E.local(k, j), f, style, ENG_FMT.get(k, "d1"))
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
        group_cols(ws, E, "glz", "nenv")
        group_cols(ws, E, "oa_lpp", "oa_spec")
        group_cols(ws, E, "sa_lpm2", "sa_spec")
        group_cols(ws, E, "ex_ach", "ex_spec")
        group_cols(ws, E, "ppl", "inf_w")
        group_cols(ws, E, "lat_ppl_wp", "lat_inf_w")
        _setup(ws, ctx, E, E.idx["frac"], tr, "4:7", f"{E.col('name')}{E.first}", "FF0070C0")


# --------------------------------------------------------------------------- #
AF_F: Dict[str, str] = {
    "no": "={RI.no}", "name": "={RI.name}", "storey": "={RI.storey}", "ahu": "={RI.ahu}", "sys": "={RI.systype}",
    "area": "={ENG.area}", "vol": "={ENG.vol}", "sens": "={ENG.sens}", "lat": "={ENG.lat}", "troom": "={ENG.troom}",
    "hroom": "={ENG.hroom}", "ac": "={ENG.sa_lps}", "th": "={ENG.th_lps}",
    "gov": '=IF({supply}=0,"NONE",IF({pm}>MAX({th},{ac}),"PRESSURE",IF({th}>={ac},"THERMAL","AIR CHANGE")))',
    "margin": "=ABS({th}-{ac})", "ex_min": "={ENG.ex_fin}",
    "pm": '=IF({kind}="EXHAUST",MAX(0,{ex_min}-{off}),IF({kind}="SUPPLY",{ex_min}+{off},0))',
    "supply": "={ENG.fin}",
    "extract": '=IF({kind}="EXHAUST",{supply}+{off},IF({kind}="SUPPLY",{supply}-{off},MAX({supply},{ex_min})))',
    "single": "={CR.single}",
    "exhaust": '=IF({single}="Y",{extract},IF({kind}="EXHAUST",MAX({ex_min},{off}),{ex_min}))',
    "ret": "={extract}-{exhaust}", "oa_air": "={ENG.oa}",
    "oa": '=IF({supply}>0,MIN({supply},MAX({oa_air},{exhaust}+IF({kind}="SUPPLY",{off},0))),0)',
    "frac": "=IF({supply}>0,{oa}/{supply},0)",
    "minflow": "=MIN({supply},MAX({ac},{oa},{pm}))",
    "ok": '=IF({supply}>=MAX({th},{ac},{pm}),"PASS","FAIL")',
    "terminal": "={RI.terminal}", "exfan": "={RI.exfan}", "clin": "={CR.clin}", "systype": "={RI.systype}",
}
AF_STYLE = {"no": "LinkTag", "name": "Link", "storey": "Link", "ahu": "LinkTag", "sys": "Link", "terminal": "LinkTag",
            "exfan": "LinkTag", "clin": "Link", "systype": "Link", "single": "Link", "ok": "Check", "gov": "Calc",
            "supply": "Result", "oa": "Result"}
AF_LINK = {"area", "vol", "sens", "lat", "troom", "hroom", "ac", "th", "ex_min", "supply", "oa_air"}


def write_airflow(ctx: Ctx) -> None:
    ws = ctx.wb["Airflow"]
    L = ctx.lay["AF"]
    title_block(ws, "Airflow", "Both cases for every room: air-change airflow and thermal-load airflow; the larger governs, "
                "not less than exhaust and pressure require. Extract = supply + offset (negative room) or supply − offset "
                "(positive room). The last columns apply the outdoor-air rule for small rooms (Section 8.2) in rounds.", ctx)
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
            fmt = None if k in ("no", "name", "storey", "ahu", "sys", "gov", "single", "ok", "terminal", "exfan", "clin", "systype") \
                else ("pct1" if k == "frac" else ("d1"))
            c = put(ws, g(k), f, st, fmt)
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
                put(ws, inn, f"=IF({g('supply')}>0,1,0)", "Calc", "d0")
            else:
                put(ws, inn, f"={g(f'in{rr-1}')}-{g(f'sep{rr-1}')}", "Calc", "d0")
            put(ws, g(f"tot{rr}"), expand(ctx, f"=SUMIFS([AF.supply],[AF.ahu],{{ahu}},[AF.in{rr}],1)", L, i), "Calc", "d1")
            put(ws, g(f"max{rr}"), expand(ctx, f"=_xlfn.MAXIFS([AF.frac],[AF.ahu],{{ahu}},[AF.in{rr}],1)", L, i), "Calc", "pct1")
            put(ws, g(f"nxt{rr}"), expand(
                ctx, f"=SUMPRODUCT(MAX(([AF.ahu]={{ahu}})*([AF.in{rr}]=1)*(ROW([AF.frac])<>ROW())*[AF.frac]))", L, i), "Calc", "pct1")
            put(ws, g(f"trg{rr}"), f"=IF({inn}=1,IF(AND({g('frac')}>0,{g('frac')}={g(f'max{rr}')},"
                                   f"{g('supply')}/{g(f'tot{rr}')}<OAShare,{g(f'max{rr}')}-{g(f'nxt{rr}')}>OADrop),1,0),0)",
                "Calc", "d0")
            put(ws, g(f"sep{rr}"), f'=IF({g("clin")}="YES",0,{g(f"trg{rr}")})', "Calc", "d0")
            put(ws, g(f"lst{rr}"), f'=IF({g("clin")}="YES",{g(f"trg{rr}")},0)', "Calc", "d0")
        sums = lambda p: "+".join(g(f"{p}{x}") for x in range(1, OA_ROUNDS + 1))
        put(ws, g("status"), f'=IF({sums("sep")}>0,"SEPARATE",IF({sums("lst")}>0,"LIST FOR MARCUS",""))', "Calc").alignment = \
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
        put(ws, g("fcurank"), f'=IF({g("fcu")}=1,SUM({L.upto("fcu", i)}),0)', "Calc", "d0")
    last = L.last
    band_rows(ws, f"A{L.first}:{L.col('ok')}{last}")
    cf_text(ws, f"{L.col('ok')}{L.first}:{L.col('ok')}{last}", f"{L.col('ok')}{L.first}", "FAIL")
    for tx, fill in (("THERMAL", "FFDDEBF7"), ("AIR CHANGE", "FFE2EFDA"), ("PRESSURE", "FFFCE4D6")):
        cf_text(ws, f"{L.col('gov')}{L.first}:{L.col('gov')}{last}", f"{L.col('gov')}{L.first}", tx, bold=True, fill=fill)
    cf_formula(ws, f"{L.col('supply')}{L.first}:{L.col('supply')}{last}",
               f"{L.col('supply')}{L.first}<{L.col('ac')}{L.first}")
    cf_text(ws, f"{L.col('status')}{L.first}:{L.col('status')}{last}", f"{L.col('status')}{L.first}", "SEPARATE", fill="FFFFF2CC")
    group_cols(ws, L, "terminal", "systype")
    group_cols(ws, L, "in1", f"lst{OA_ROUNDS}")
    group_cols(ws, L, "cmark0", "fcurank")
    group_cols(ws, L, "single", "single")
    _setup(ws, ctx, L, L.idx["ok"], last, "4:6", f"{L.col('name')}{L.first}", "FF00B050")
