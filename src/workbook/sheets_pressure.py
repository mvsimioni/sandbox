"""Pressure tab: the project pressure-offset method with live formulas."""

from __future__ import annotations

from typing import Any, Dict, List

from openpyxl.styles import Alignment

from .common import band_rows, cf_text, expand, group_cols, list_validation, title_block, write_headers
from .ctx import Ctx
from .layouts import NBIS, NPASS
from .model import blank
from .sheets_rooms import _prov_cells, _val, _setup
from .styles import CODE_STYLES, put

PS_HEAD = {
    "node": ("Node (room number or space)", "", 14), "kind": ("Type", "", 9), "name": ("Name", "", 28), "suite": ("Suite", "", 9),
    "set": ("Set pressure", "Pa", 8), "set_c": ("Code", "", 5), "set_s": ("Source ID", "", 11),
    "perim": ("Perimeter", "m", 8), "perim_c": ("Code", "", 5), "perim_s": ("Source ID", "", 11), "area": ("Floor area", "m²", 8),
    "h": ("Height", "m", 7), "ovr": ("Internal surface, if given", "m²", 9), "ovr_c": ("Code", "", 5),
    "ovr_s": ("Source ID", "", 11), "surf": ("Internal surface", "m²", 9), "limit": ("Leakage limit at reference", "m³/h per m²", 9),
    "n": ("Flow exponent n", "–", 7), "supply": ("Supply", "m³/h", 9), "exhaust": ("Extract", "m³/h", 9),
    "pfin": ("Pressure solved", "Pa", 9), "env_in": ("Envelope in", "m³/h", 9), "env_out": ("Envelope out", "m³/h", 9),
    "gap_in": ("Other paths in", "m³/h", 9), "gap_out": ("Other paths out", "m³/h", 9), "fix_in": ("Fixed transfer in", "m³/h", 9),
    "fix_out": ("Fixed transfer out", "m³/h", 9), "air_in": ("Air in", "m³/h", 9), "air_out": ("Air out", "m³/h", 9),
    "kindoff": ("Offset kind", "", 10), "needed": ("Offset needed", "m³/h", 9), "design": ("Design offset", "m³/h", 9),
    "tighter": ("Offset of a tighter room", "m³/h", 9), "ratio": ("Ratio design / needed", "–", 8), "status": ("Status", "", 9),
    "spec_off": ("Offset as specified (MOT, not resized)", "m³/h", 11), "spec_chk": ("Specified offset against needed", "", 11),
    "off_lps": ("Offset needed", "L/s", 9), "design_lps": ("Design offset", "L/s", 9),
    "needed_n50": ("Offset needed, n = 0.50", "m³/h", 9), "needed_n75": ("Offset needed, n = 0.75", "m³/h", 9),
    "ratio_n50": ("Ratio, n = 0.50", "–", 8), "ratio_n75": ("Ratio, n = 0.75", "–", 8),
    "flag_s": ("Fails only in a sensitivity case", "", 10), "upa": ("Pressure at design offset, no control", "Pa", 10),
    "resid": ("Balance residual", "m³/h", 9), "note": ("Note", "", 40),
}


def _fmt_cell(ws, ref, f, style, fmt=None, left=False):
    c = put(ws, ref, f, style, fmt)
    if left:
        c.alignment = Alignment(horizontal="left", vertical="center")
    return c


def write_pressure(ctx: Ctx) -> None:
    ws = ctx.wb["Pressure"]
    PS, PL, PU = ctx.lay["PS"], ctx.lay["PL"], ctx.lay["PU"]
    RI, CR, AF = ctx.lay["RI"], ctx.lay["CR"], ctx.lay["AF"]
    title_block(ws, "Pressure", "Every room with a pressure target in Pa. The envelope complies with the airtightness limit: Q = limit × internal surface × (ΔP ÷ 50 Pa)^n, "
                "with the limit 2.5 m³/h per m² at 50 Pa and n = 0.65. Internal surface = 2 × area + perimeter × height. Doors are not modelled. "
                "Offset needed for a negative room = envelope leakage in at the set pressure. Offset needed for a positive room = envelope leakage out. "
                "Design offset = 2.5 × internal surface. Ratio = design offset ÷ offset needed. Sensitivity: n = 0.50 and n = 0.75.", ctx)
    # ---- node table headers
    spec = [(k, *PS_HEAD[k]) for k in PS.keys if k in PS_HEAD]
    write_headers(ws, PS, spec, 4, {"node": "SPACE", "set": "SET PRESSURE", "perim": "GEOMETRY", "supply": "MECHANICAL AIR",
                                    "pfin": "LEAKAGE PATHS AT THE SOLVED PRESSURES", "kindoff": "OFFSET", "needed_n50": "SENSITIVITY"})
    for k in PS.keys:
        if k not in PS_HEAD:
            ws.column_dimensions[PS.col(k)].width = 9
    ps_rng = lambda k: PS.rng(k)
    for q, nd in enumerate(ctx.ps_nodes):
        g = lambda k: PS.local(k, q)
        isroom = nd["kind"] == "ROOM"
        gi = nd.get("gi")
        ctrl = nd["controlled"]
        free = isroom and not ctrl
        if isroom:
            _fmt_cell(ws, g("node"), expand(ctx, "={RI.no}", PS, q, {"RI": gi}), "LinkTag", left=True)
            _fmt_cell(ws, g("name"), expand(ctx, "={RI.name}", PS, q, {"RI": gi}), "Link", left=True)
        else:
            _val(ctx, ws, g("node"), nd["tag"], nd["code"], kind="tag", cref=g("set_c"), sref=g("set_s"), src=nd["source"],
                 label="pressure node", room=nd["tag"])
            _val(ctx, ws, g("name"), nd.get("description"), nd["code"], kind="text", cref=g("set_c"), sref=g("set_s"),
                 src=nd["source"])
        put(ws, g("kind"), nd["kind"], "Text")
        put(ws, g("suite"), nd.get("suite"), "Text")
        if isroom:
            room = nd["room"]
            put(ws, g("area"), expand(ctx, "={RI.area}", PS, q, {"RI": gi}), "Link", "d1")
            put(ws, g("h"), expand(ctx, "={RI.h}", PS, q, {"RI": gi}), "Link", "d2")
            pr = room.get("pressure") or {}
            pc, psr = (pr.get("prov") or ["M", ""])[:2]
            has_p = not blank(pr.get("perimeter_m"))
            if has_p and blank(psr):
                raise ValueError(f"room {room['number']}: pressure perimeter has no source id")
            if not has_p and pr.get("perimeter_rule") == "factor":
                # no measured outline: perimeter = PerimFactor x square root of the area used (assumed, code A, flagged on Flags)
                put(ws, g("perim"), f'=PerimFactor*SQRT({g("area")})', "Assumption", "d2")
                put(ws, g("perim_c"), "A", "Code")
                put(ws, g("perim_s"), "GEN-PRESSURE", "Tag")
                ctx.src_ranges.append(g("perim_s"))
                ctx.src_link(ws, g("perim_s"), "GEN-PRESSURE")
                ctx.coded.append(dict(sheet=ws.title, cell=g("perim"), code_cell=g("perim_c"), src_cell=g("perim_s"), code="A"))
            else:
                _val(ctx, ws, g("perim"), pr.get("perimeter_m"), pc, fmt="d2", cref=g("perim_c"), sref=g("perim_s"), src=psr,
                     label="perimeter", room=room["number"])
                _prov_cells(ctx, ws, g("perim_c"), g("perim_s"), pc, psr, has_p)
            ov = pr.get("surface_override_m2")
            oc, os_ = (pr.get("surface_prov") or ["S", "GEN-PRESSURE"])[:2]
            _val(ctx, ws, g("ovr"), ov, oc, fmt="d1", cref=g("ovr_c"), sref=g("ovr_s"), src=os_, label="surface", room=room["number"])
            _prov_cells(ctx, ws, g("ovr_c"), g("ovr_s"), oc, os_, not blank(ov))
            put(ws, g("surf"), f'=IF({g("ovr")}<>"",{g("ovr")},2*{g("area")}+{g("perim")}*{g("h")})', "Calc", "d1")
            put(ws, g("limit"), "=AirtightLimit", "Link", "d2")
            put(ws, g("n"), "=FlowN", "Link", "d2")
            if ctrl:
                put(ws, g("set"), expand(ctx, "={CR.pa}", PS, q, {"CR": gi}), "Link", "d1")
                put(ws, g("set_c"), "L", "Code")
                ctx.coded.append(dict(sheet=ws.title, cell=g("set"), code_cell=g("set_c"), src_cell=None, code="L"))
            put(ws, g("supply"), f"={ctx.lay['AF'].addr('supply', gi)}*ConvM3hPerLps", "Link", "d1")
            put(ws, g("exhaust"), f"={ctx.lay['AF'].addr('extract', gi)}*ConvM3hPerLps", "Link", "d1")
        else:
            _val(ctx, ws, g("set"), nd["pressure_pa"], nd["code"], fmt="d1", cref=g("set_c"), sref=g("set_s"), src=nd["source"],
                 label="external pressure", room=nd["tag"])
            _prov_cells(ctx, ws, g("set_c"), g("set_s"), nd["code"], nd["source"], True)
        # iteration columns
        for k in range(NPASS + 1):
            c = g(f"p{k}")
            if k == 0:
                f = "=StartPressure" if free else f"={g('set')}"
            elif free:
                dp = f"({g(f'p{k-1}')}-INDEX({ps_rng(f'p{k-1}')},{g('lk')}))"
                E = f"{g('limit')}*{g('surf')}*SIGN({dp})*(ABS({dp})/RefPressure)^{g('n')}"
                Gen = f"{g('n')}*{g('limit')}*{g('surf')}/RefPressure*(MAX(ABS({dp}),NewtonEps)/RefPressure)^({g('n')}-1)"
                R = (f"{g('supply')}-{g('exhaust')}+SUMIFS({PL.rng(f'f{k-1}')},{PL.rng('b')},{g('node')})"
                     f"-SUMIFS({PL.rng(f'f{k-1}')},{PL.rng('a')},{g('node')})-{E}")
                S = (f"SUMIFS({PL.rng(f'g{k-1}')},{PL.rng('a')},{g('node')})+SUMIFS({PL.rng(f'g{k-1}')},{PL.rng('b')},{g('node')})+{Gen}")
                f = f"={g(f'p{k-1}')}+({R})/({S})"
            else:
                f = f"={g(f'p{k-1}')}"
            put(ws, c, f, "Calc", "d3")
        put(ws, g("pfin"), f"={g(f'p{NPASS}')}", "Result", "d2")
        if not isroom:
            continue
        put(ws, g("lk"), f'=MATCH("REF",{ps_rng("node")},0)', "Calc", "d0")
        pref = f"INDEX({ps_rng('pfin')},{g('lk')})"
        dpf = f"({g('pfin')}-{pref})"
        put(ws, g("bsum"), f"={g('limit')}*{g('surf')}*SIGN({dpf})*(ABS({dpf})/RefPressure)^{g('n')}", "Calc", "d2")
        put(ws, g("env_in"), f"=MAX(-{g('bsum')},0)", "Calc", "d1")
        put(ws, g("env_out"), f"=MAX({g('bsum')},0)", "Calc", "d1")
        node = g("node")
        for key, col, sign in (("gap_in", "q", "in"), ("gap_out", "q", "out"), ("fix_in", "qf", "in"), ("fix_out", "qf", "out")):
            Q = PL.rng(col)
            if sign == "in":
                f = f'=SUMIFS({Q},{PL.rng("b")},{node},{Q},">0")-SUMIFS({Q},{PL.rng("a")},{node},{Q},"<0")'
            else:
                f = f'=SUMIFS({Q},{PL.rng("a")},{node},{Q},">0")-SUMIFS({Q},{PL.rng("b")},{node},{Q},"<0")'
            put(ws, g(key), f, "Calc", "d1")
        put(ws, g("air_in"), f"={g('env_in')}+{g('gap_in')}+{g('fix_in')}", "Calc", "d1")
        put(ws, g("air_out"), f"={g('env_out')}+{g('gap_out')}+{g('fix_out')}", "Calc", "d1")
        put(ws, g("resid"), f"={g('supply')}+{g('air_in')}-{g('exhaust')}-{g('air_out')}", "Calc", "d1")
        if free:
            put(ws, g("kindoff"), "FREE", "Calc")
            put(ws, g("status"), f'=IF(ABS({g("resid")})<=TolPressure,"FREE","UNSOLVED")', "Check")
            continue
        put(ws, g("negf"), f"=IF({g('pfin')}<{pref},1,0)", "Calc", "d0")
        raw = f"IF({g('negf')}=1,{g('air_in')}-{g('air_out')},{g('air_out')}-{g('air_in')})"
        put(ws, g("kindoff"), f'=IF({raw}>=-OffsetEps,IF({g("negf")}=1,"EXHAUST","SUPPLY"),IF({g("negf")}=1,"SUPPLY","EXHAUST"))', "Calc")
        put(ws, g("needed"), f"=ABS({raw})", "Result", "d1")
        put(ws, g("design"), f"={g('limit')}*{g('surf')}", "Calc", "d1")
        put(ws, g("tighter"), f"=TighterLimit*{g('surf')}", "Calc", "d1")
        put(ws, g("ratio"), f'=IF({g("needed")}>OffsetEps,{g("design")}/{g("needed")},"n/a")', "Calc", "d2")
        put(ws, g("status"), f'=IF(ISNUMBER({g("ratio")}),IF({g("ratio")}<RatioFail,"FAIL",IF(OR({g("ratio")}<RatioVerify,{raw}<-OffsetEps),'
                             f'"VERIFY","PASS")),"PASS")', "Check")
        fx = ctx.lay["AF"].addr("fix", gi)
        put(ws, g("spec_off"), f'=IF({fx}=1,IF({g("kindoff")}="EXHAUST",{g("exhaust")}-{g("supply")},{g("supply")}-{g("exhaust")}),"")', "Calc", "d1")
        put(ws, g("spec_chk"), f'=IF(ISNUMBER({g("spec_off")}),IF({g("spec_off")}>={g("needed")}-OffsetEps,"OK","SHORT"),"")', "Check")
        put(ws, g("off_lps"), f"={g('needed')}/ConvM3hPerLps", "Calc", "d1")
        put(ws, g("design_lps"), f"={g('design')}/ConvM3hPerLps", "Calc", "d1")
        gapnet = f"({g('gap_in')}+{g('fix_in')}-{g('gap_out')}-{g('fix_out')})"       # zero where no path other than the envelope exists
        for key, nexp in (("n50", "FlowNLow"), ("n75", "FlowNHigh")):
            e = f"{g('limit')}*{g('surf')}*SIGN({dpf})*(ABS({dpf})/RefPressure)^{nexp}"
            r2 = f"IF({g('negf')}=1,{gapnet}-{e},-({gapnet}-{e}))"
            put(ws, g(f"needed_{key}"), f"=ABS({r2})", "Calc", "d1")
        for key in ("n50", "n75"):
            put(ws, g(f"ratio_{key}"), f'=IF({g(f"needed_{key}")}>OffsetEps,{g("design")}/{g(f"needed_{key}")},"n/a")', "Calc", "d2")
        put(ws, g("flag_s"), f'=IF(AND({g("status")}<>"FAIL",OR(AND(ISNUMBER({g("ratio_n50")}),{g("ratio_n50")}<RatioFail),'
                             f'AND(ISNUMBER({g("ratio_n75")}),{g("ratio_n75")}<RatioFail))),"FLAG","")',
            "Check")
        put(ws, g("note"), f'=IF({raw}<-OffsetEps,"LEAKAGE RUNS THE WRONG WAY FOR THIS ROOM. ","")&IF(ABS({g("pfin")}-{pref})<ApplyFromPa,'
                           f'"SET PRESSURE BELOW THE LIMIT OF THE METHOD.","")', "Note")
        # uncontrolled pressure: bisection in fixed steps
        kk = lambda p: (f"SUMPRODUCT(({PL.rng('b')}={node})*{PL.rng('kc')}*SIGN({PL.rng('pa')}-{p})*SQRT(ABS({PL.rng('pa')}-{p})))"
                        f"+SUMPRODUCT(({PL.rng('a')}={node})*{PL.rng('kc')}*SIGN({PL.rng('pb')}-{p})*SQRT(ABS({PL.rng('pb')}-{p})))")
        for k in range(NBIS + 1):
            c = g(f"u{k}")
            if k == 0:
                f = f"={pref}"
            else:
                p = g(f"u{k-1}")
                E = f"{g('limit')}*{g('surf')}*SIGN({p}-{pref})*(ABS({p}-{pref})/RefPressure)^{g('n')}"
                Rr = (f"(-{E}+{kk(p)}+{g('fix_in')}-{g('fix_out')})-IF({g('negf')}=1,1,-1)*{g('design')}")
                f = f"={p}+SIGN({Rr})*PressureBracket/2^(COLUMN()-COLUMN(${PS.col('u0')}${PS.row(q)}))"
            put(ws, c, f, "Calc", "d3")
        put(ws, g("upa"), f"={g(f'u{NBIS}')}", "Calc", "d1")
    last = PS.last
    for k in ("status", "flag_s", "spec_chk"):
        cf_text(ws, f"{PS.col(k)}{PS.first}:{PS.col(k)}{last}", f"{PS.col(k)}{PS.first}", "FAIL")
        cf_text(ws, f"{PS.col(k)}{PS.first}:{PS.col(k)}{last}", f"{PS.col(k)}{PS.first}", "UNSOLVED")
        cf_text(ws, f"{PS.col(k)}{PS.first}:{PS.col(k)}{last}", f"{PS.col(k)}{PS.first}", "SHORT")
        cf_text(ws, f"{PS.col(k)}{PS.first}:{PS.col(k)}{last}", f"{PS.col(k)}{PS.first}", "VERIFY", fill="FFFFF2CC")
        cf_text(ws, f"{PS.col(k)}{PS.first}:{PS.col(k)}{last}", f"{PS.col(k)}{PS.first}", "FLAG", fill="FFFFF2CC")
    band_rows(ws, f"A{PS.first}:{PS.col('note')}{last}")
    group_cols(ws, PS, "p0", f"u{NBIS}", collapsed=True)   # Newton and bisection helper columns: expand to inspect, not printed
    # ---- link table
    _write_links(ctx, ws)
    _write_suites(ctx, ws)
    _write_known_answers(ctx, ws)
    end = ctx.ka_end
    _setup(ws, ctx, PS, PS.idx["note"], end, "4:6", f"{PS.col('name')}{PS.first}", "FFC00000")
    for key in ("pl_head",):
        pass


def _write_links(ctx: Ctx, ws) -> None:
    PS, PL = ctx.lay["PS"], ctx.lay["PL"]
    title_row = PL.first - 4
    if not ctx.pl_links:
        return
    put(ws, f"A{title_row}", "Air paths: fixed transfers and other paths, with the space on the other side", "Section")
    heads = {"id": ("Path ID", "", 14), "room": ("Room", "", 14), "a": ("Node A", "", 14), "b": ("Node B (other side)", "", 14),
             "kind": ("Door or gap", "", 24), "seal": ("Seal type", "", 10), "gap": ("Open gap area", "cm²", 8),
             "gap_c": ("Code", "", 5), "gap_s": ("Source ID", "", 11), "leaf_w": ("Leaf width", "m", 7), "leaf_h": ("Leaf height", "m", 7),
             "fixed": ("Fixed transfer, A to B", "m³/h", 9), "area": ("Gap area used", "m²", 9), "pa": ("Pressure A", "Pa", 8),
             "pb": ("Pressure B", "Pa", 8), "dp": ("ΔP", "Pa", 8), "q": ("Gap flow, A to B", "m³/h", 9),
             "qf": ("Fixed flow, A to B", "m³/h", 9)}
    spec = [(k, *heads[k]) for k in PL.keys if k in heads]
    write_headers(ws, PL, spec, PL.first - 3, {"id": "PATH"})
    for m, d in enumerate(ctx.pl_links):
        g = lambda k: PL.local(k, m)
        code, src = d.get("code", "D"), d.get("source", "")
        if blank(src):
            raise ValueError(f"door {d['id']} has no source id")
        for k, f, kind, fmt in (("id", "id", "tag", None), ("room", "room", "tag", None), ("a", "a", "tag", None), ("b", "b", "tag", None),
                                ("kind", "kind", "text", None), ("seal", "seal", "text", None), ("gap", "gap_cm2", "num", "d1"),
                                ("leaf_w", "leaf_w_m", "num", "d2"), ("leaf_h", "leaf_h_m", "num", "d2"),
                                ("fixed", "fixed_flow_m3h", "num", "d1")):
            _val(ctx, ws, g(k), d.get(f), code, fmt=fmt, kind=kind, cref=g("gap_c"), sref=g("gap_s"), src=src,
                 label=f"door {d['id']} {f}", room=d["room"])
        _prov_cells(ctx, ws, g("gap_c"), g("gap_s"), code, src, True)
        put(ws, g("area"), (f'=IF({g("fixed")}<>"",0,IF({g("gap")}<>"",{g("gap")}/CM2perM2,IF({g("seal")}="hinged",GapHinged/CM2perM2,'
                            f'IF({g("seal")}="sliding",GapSliding/CM2perM2,IF({g("seal")}="bed_width",GapBedWidth/CM2perM2,'
                            f'IF({g("seal")}="shaft",GapShaft/CM2perM2,IF(AND({g("leaf_w")}<>"",{g("leaf_h")}<>""),'
                            f'((2*{g("leaf_h")}+{g("leaf_w")})*GapSides+{g("leaf_w")}*GapBottom)/MMperM,0)))))))'), "Calc", "d3")
        put(ws, g("ia"), f"=MATCH({g('a')},{PS.rng('node')},0)", "Calc", "d0")
        put(ws, g("ib"), f"=MATCH({g('b')},{PS.rng('node')},0)", "Calc", "d0")
        put(ws, g("pa"), f"=INDEX({PS.rng('pfin')},{g('ia')})", "Link", "d2")
        put(ws, g("pb"), f"=INDEX({PS.rng('pfin')},{g('ib')})", "Link", "d2")
        put(ws, g("dp"), f"={g('pa')}-{g('pb')}", "Calc", "d2")
        put(ws, g("kc"), f'=IF({g("fixed")}<>"",0,DoorCd*{g("area")}*SecPerHour*SQRT(2/AirDensity))', "Calc", "d1")
        put(ws, g("q"), f"={g('kc')}*SIGN({g('dp')})*SQRT(ABS({g('dp')}))", "Calc", "d1")
        put(ws, g("qf"), f'=IF({g("fixed")}<>"",{g("fixed")},0)', "Calc", "d1")
        for k in range(NPASS):
            dpk = f"(INDEX({PS.rng(f'p{k}')},{g('ia')})-INDEX({PS.rng(f'p{k}')},{g('ib')}))"
            put(ws, g(f"f{k}"), f'=IF({g("fixed")}<>"",{g("fixed")},{g("kc")}*SIGN({dpk})*SQRT(ABS({dpk})))', "Calc", "d2")
            put(ws, g(f"g{k}"), f'=IF({g("fixed")}<>"",0,{g("kc")}/(2*SQRT(MAX(ABS({dpk}),NewtonEps))))', "Calc", "d3")
    if ctx.pl_links:
        list_validation(ws, f"{PL.col('seal')}{PL.first}:{PL.col('seal')}{PL.last}", "ListSeal")
        band_rows(ws, f"A{PL.first}:{PL.col('qf')}{PL.last}")
    group_cols(ws, PL, "ia", f"g{NPASS - 1}", collapsed=True)


def _write_suites(ctx: Ctx, ws) -> None:
    PS, PU = ctx.lay["PS"], ctx.lay["PU"]
    if not ctx.suites:
        return
    put(ws, f"A{PU.first - 4}", "Suites: rooms solved together, offsets by kind", "Section")
    heads = {"suite": ("Suite", "", 14), "n": ("Controlled rooms", "no.", 9), "ex_needed": ("Extra exhaust needed", "m³/h", 10),
             "sup_needed": ("Extra supply needed", "m³/h", 10), "ex_design": ("Design offset, exhaust rooms", "m³/h", 10),
             "sup_design": ("Design offset, supply rooms", "m³/h", 10), "needed": ("Offset needed", "m³/h", 10),
             "design": ("Design offset", "m³/h", 10), "ratio": ("Ratio design / needed", "–", 8), "status": ("Status", "", 9),
             "note": ("Note", "", 28)}
    write_headers(ws, PU, [(k, *heads[k]) for k in PU.keys], PU.first - 3, {"suite": "SUITE TOTALS"})
    for s, name in enumerate(ctx.suites):
        g = lambda k: PU.local(k, s)
        put(ws, g("suite"), name, "Tag")
        su, ko = PS.rng("suite"), PS.rng("kindoff")
        put(ws, g("n"), f'=COUNTIFS({su},{g("suite")},{ko},"EXHAUST")+COUNTIFS({su},{g("suite")},{ko},"SUPPLY")', "Calc", "d0")
        for key, kind, col in (("ex_needed", "EXHAUST", "needed"), ("sup_needed", "SUPPLY", "needed"),
                               ("ex_design", "EXHAUST", "design"), ("sup_design", "SUPPLY", "design")):
            put(ws, g(key), f'=SUMIFS({PS.rng(col)},{su},{g("suite")},{ko},"{kind}")', "Calc", "d1")
        put(ws, g("needed"), f"={g('ex_needed')}+{g('sup_needed')}", "Result", "d1")
        put(ws, g("design"), f"={g('ex_design')}+{g('sup_design')}", "Result", "d1")
        mixed = f"AND({g('ex_needed')}>0,{g('sup_needed')}>0)"
        put(ws, g("ratio"), f'=IF({mixed},"n/a",IF({g("needed")}>OffsetEps,{g("design")}/{g("needed")},"n/a"))', "Calc", "d2")
        put(ws, g("status"), f'=IF({mixed},"MIXED",IF(ISNUMBER({g("ratio")}),IF({g("ratio")}<RatioFail,"FAIL",IF({g("ratio")}<RatioVerify,"VERIFY","PASS")),"PASS"))',
            "Check")
        put(ws, g("note"), f'=IF({mixed},"no single margin: see the space rows","internal doors cancel")', "Note")
    if ctx.suites:
        cf_text(ws, f"{PU.col('status')}{PU.first}:{PU.col('status')}{PU.last}", f"{PU.col('status')}{PU.first}", "FAIL")
        cf_text(ws, f"{PU.col('status')}{PU.first}:{PU.col('status')}{PU.last}", f"{PU.col('status')}{PU.first}", "VERIFY", fill="FFFFF2CC")


def _write_known_answers(ctx: Ctx, ws) -> None:
    """Known-answer cases as live formulas, with the expected results beside them."""
    first = ctx.ka_first
    put(ws, f"A{first - 4}", "Known-answer test: the method reproduces these to within 0.2 m³/h", "Section")
    cols = {"A": ("Case", 14), "C": ("Check", 28), "E": ("Computed", 10), "F": ("Expected", 10), "G": ("Tolerance", 9), "H": ("Result", 9),
            "I": ("Input 1", 9), "J": ("Input 2", 9), "K": ("Input 3", 9), "L": ("Code", 5), "M": ("Source ID", 11)}
    for c, (h, w) in cols.items():
        put(ws, f"{c}{first - 2}", h, "Header")
    put(ws, f"A{first - 3}", "KNOWN ANSWERS", "HeaderLeft")
    SRC = "GEN-PRESSURE"
    rows: List[Dict[str, Any]] = []
    R = {}

    def add(key, case, label, params, formula, expected, tol, fmt="d1"):
        rows.append(dict(key=key, case=case, label=label, params=params, formula=formula, expected=expected, tol=tol, fmt=fmt))

    for pa, ex in ((5, 0.560), (15, 1.143), (25, 1.593), (50, 2.500), (70, 3.111)):
        add(f"lim{pa}", "1 limit conversion", f"limit at {pa} Pa (m³/h per m²)", [pa], "=AirtightLimit*({P1}/RefPressure)^FlowN", ex, 0.001, "d3")
    add("s1", "2 small room", "internal surface 1.88 × 2.94 × 3.0 m (m²)", [1.88, 2.94, 3.0], "=2*{P1}*{P2}+2*({P1}+{P2})*{P3}", 40.0, 0.05)
    add("e50", "2 small room", "envelope leakage at 50 Pa (m³/h)", [50], "=AirtightLimit*{s1}*({P1}/RefPressure)^FlowN", 99.9, 0.2)
    add("e15", "2 small room", "envelope leakage at 15 Pa (m³/h)", [15], "=AirtightLimit*{s1}*({P1}/RefPressure)^FlowN", 45.7, 0.2)
    add("n50", "3 sensitivity", "small room at 15 Pa, n = 0.50 (m³/h)", [15], "=AirtightLimit*{s1}*({P1}/RefPressure)^FlowNLow", 54.7, 0.2)
    add("n75", "3 sensitivity", "small room at 15 Pa, n = 0.75 (m³/h)", [15], "=AirtightLimit*{s1}*({P1}/RefPressure)^FlowNHigh", 40.5, 0.2)
    add("a_need", "4 room at -15 Pa", "offset needed = envelope leakage at 15 Pa (m³/h)", [], "={e15}", 45.7, 0.2)
    add("a_des", "4 room at -15 Pa", "design offset = 2.5 × surface (m³/h)", [], "=AirtightLimit*{s1}", 99.9, 0.2)
    add("a_rat", "4 room at -15 Pa", "ratio", [], "={a_des}/{a_need}", 2.19, 0.01, "d2")
    add("s2", "5 room at -30 Pa", "internal surface of room 3.97 × 4.83 × 3.0 m (m²)", [3.97, 4.83, 3.0], "=2*{P1}*{P2}+2*({P1}+{P2})*{P3}", 91.2, 0.1)
    add("er", "5 room at -30 Pa", "offset needed = envelope leakage at 30 Pa (m³/h)", [30], "=AirtightLimit*{s2}*({P1}/RefPressure)^FlowN", 163.5, 0.2)
    add("r_des", "5 room at -30 Pa", "design offset = 2.5 × surface (m³/h)", [], "=AirtightLimit*{s2}", 227.9, 0.2)
    add("r_rat", "5 room at -30 Pa", "ratio", [], "={r_des}/{er}", 1.39, 0.01, "d2")
    add("ee", "6 ensuite at -31 Pa", "offset needed = envelope leakage of 37.5 m² at 31 Pa (m³/h)", [31, 37.5], "=AirtightLimit*{P2}*({P1}/RefPressure)^FlowN", 68.7, 0.2)
    add("e_des", "6 ensuite at -31 Pa", "design offset = 2.5 × 37.5 m² (m³/h)", [37.5], "=AirtightLimit*{P1}", 93.8, 0.2)
    add("e_rat", "6 ensuite at -31 Pa", "ratio", [], "={e_des}/{ee}", 1.36, 0.01, "d2")
    add("f70", "7 room at -70 Pa", "offset needed = envelope leakage at 70 Pa (m³/h)", [70], "=AirtightLimit*{s1}*({P1}/RefPressure)^FlowN", 124.4, 0.2)
    add("f_rat", "7 room at -70 Pa", "ratio (below 1.0: a fail)", [], "={a_des}/{f70}", 0.80, 0.01, "d2")
    for i, r in enumerate(rows):
        R[r["key"]] = f"E{first + i}"
    ctx.ka_rows = {}
    for i, r in enumerate(rows):
        row = first + i
        put(ws, f"A{row}", r["case"], "Text")
        put(ws, f"C{row}", r["label"], "Text")
        f = r["formula"]
        for j, pv in enumerate(r["params"]):
            ref = f"{'IJK'[j]}{row}"
            put(ws, ref, pv, "Specified", "d2")
            ctx.coded.append(dict(sheet=ws.title, cell=ref, code_cell=f"L{row}", src_cell=f"M{row}", code="S"))
            f = f.replace("{P%d}" % (j + 1), ref)
        for key, ref in R.items():
            f = f.replace("{%s}" % key, ref)
        put(ws, f"E{row}", f, "Result", r["fmt"])
        put(ws, f"F{row}", r["expected"], "Specified", r["fmt"])
        put(ws, f"G{row}", r["tol"], "Specified", "d3")
        for c in ("F", "G"):
            ctx.coded.append(dict(sheet=ws.title, cell=f"{c}{row}", code_cell=f"L{row}", src_cell=f"M{row}", code="S"))
        put(ws, f"L{row}", "S", "Code")
        put(ws, f"M{row}", SRC, "Tag")
        ctx.src_ranges.append(f"M{row}")
        ctx.src_link(ws, f"M{row}", SRC)
        put(ws, f"H{row}", f'=IF(ABS(E{row}-F{row})<=G{row},"PASS","FAIL")', "Check")
    ctx.ka_end = first + len(rows)
    ctx.ka_status_range = f"'Pressure'!$H${first}:$H${first + len(rows) - 1}"
    ctx.ka_cells = {r["key"]: f"E{first + i}" for i, r in enumerate(rows)}
    ctx.ka_expected = {r["key"]: (r["expected"], r["tol"]) for r in rows}
    cf_text(ws, f"H{first}:H{first + len(rows) - 1}", f"H{first}", "FAIL")
