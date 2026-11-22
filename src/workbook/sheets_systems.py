"""AHU, FCU, Fans, VAV, Venturi Valves, Flow Balance and Airflow Comparison."""

from __future__ import annotations

from typing import Any, Dict, List

from openpyxl.styles import Alignment

from .common import band_rows, cf_formula, cf_text, expand, group_cols, list_validation, title_block, write_headers
from .ctx import Ctx
from .model import blank
from .sheets_rooms import _prov_cells, _setup, _val
from .styles import link_to, put

U = "²"


def H(key, header, unit, width):
    return (key, header, unit, width)


def _hl(ws, ref, f, style, fmt=None, left=False):
    c = put(ws, ref, f, style, fmt)
    if left:
        c.alignment = Alignment(horizontal="left", vertical="center")
    return c


# --------------------------------------------------------------------------- #
def _ahu_table(ctx: Ctx, ws, L, after: bool) -> None:
    AF = ctx.lay["AF"]
    inc = f",{AF.rng('sepf')},0" if after else ""
    mask = f"*({AF.rng('sepf')}=0)" if after else ""
    cm = "cmark1" if after else "cmark0"
    for t, a in enumerate(ctx.ahus):
        g = lambda k: L.local(k, t)
        if not after:
            put(ws, g("ahu"), a["tag"], "Tag")
            first_room = next((i for i, r in enumerate(ctx.rooms) if r.get("ahu") == a["tag"]), None)
            if first_room is not None:
                link_to(ws[g("ahu")], "Airflow", f"{AF.col('no')}{AF.row(first_room)}")
        else:
            put(ws, g("ahu"), f"={ctx.lay['AH'].local('ahu', t)}", "LinkTag")
        key = g("ahu")
        crit = f"{AF.rng('ahu')},{key}"
        ws[g("ahu")].alignment = Alignment(horizontal="left")
        _hl(ws, g("type"), a.get("type") if not after else f"={ctx.lay['AH'].local('type', t)}", "Text" if not after else "Link", left=True)
        sup = f',{AF.rng("supply")},">0"'
        put(ws, g("n"), f"=COUNTIFS({crit}{inc}{sup})", "Calc", "d0")
        put(ws, g("area"), f"=SUMIFS({AF.rng('area')},{crit}{inc}{sup})", "Calc", "d1")
        put(ws, g("vol"), f"=SUMIFS({AF.rng('vol')},{crit}{inc}{sup})", "Calc", "d1")
        put(ws, g("sa_room"), f"=SUMIFS({AF.rng('supply')},{crit}{inc})", "Result", "d1")
        put(ws, g("leak"), "=DuctLeakage", "Link", "pct1")
        put(ws, g("sa_ahu"), f"={g('sa_room')}*(1+{g('leak')})", "Result", "d1")
        put(ws, g("oa_sum"), f"=SUMIFS({AF.rng('oa')},{crit}{inc})", "Calc", "d1")
        put(ws, g("frac"), f"=_xlfn.MAXIFS({AF.rng('frac')},{crit}{inc})", "Calc", "pct1")
        _hl(ws, g("crit"), f'=IF({g("sa_room")}>0,INDEX({AF.rng("name")},MATCH({key},{AF.rng(cm)},0)),"–")', "Calc", left=True)
        put(ws, g("oa_rule"), f"={g('frac')}*{g('sa_ahu')}", "Result", "d1")
        put(ws, g("vou"), f"={g('oa_sum')}", "Calc", "d1")
        put(ws, g("vps"), f"={g('sa_room')}", "Calc", "d1")
        put(ws, g("xs"), f"=IF({g('vps')}>0,{g('vou')}/{g('vps')},0)", "Calc", "pct1")
        put(ws, g("zd"), f"={g('frac')}", "Calc", "pct1")
        put(ws, g("ev"), f"=1+{g('xs')}-{g('zd')}", "Calc", "d3")
        put(ws, g("vot"), f"=IF({g('ev')}>0,{g('vou')}/{g('ev')},0)", "Calc", "d1")
        put(ws, g("oa_ms"), f"=MAX({g('oa_sum')},{g('vot')})", "Calc", "d1")
        put(ws, g("ret"), f"={g('sa_ahu')}-{g('oa_rule')}", "Calc", "d1")
        put(ws, g("exh"), f"=SUMIFS({AF.rng('exhaust')},{crit}{inc})", "Calc", "d1")
        put(ws, g("relief"), f"={g('sa_ahu')}-{g('ret')}-{g('exh')}", "Calc", "d1")
        put(ws, g("sens"), f"=SUMIFS({AF.rng('sens')},{crit}{inc})/WperkW", "Calc", "d2")
        put(ws, g("lat"), f"=SUMIFS({AF.rng('lat')},{crit}{inc})/WperkW", "Calc", "d2")
        ref = f"MATCH({key},{AF.rng('ref1' if after else 'ref0')},0)"
        put(ws, g("rt"), f'=IF({g("sa_room")}>0,INDEX({AF.rng("troom")},{ref}),0)', "Calc", "d1")
        put(ws, g("rh"), f'=IF({g("sa_room")}>0,INDEX({AF.rng("hroom")},{ref}),0)', "Calc", "d1")
        for suffix, oa in (("", g("oa_rule")), ("_ms", g("oa_ms"))):
            put(ws, g("oa_sens" + suffix), f"=AirSensibleFactor*{oa}/WperkW*(OutdoorDB-{g('rt')})", "Calc", "d2")
            put(ws, g("oa_lat" + suffix), f"=AirDensity*{oa}/WperkW*(OutdoorH-{g('rh')})-{g('oa_sens' + suffix)}", "Calc", "d2")
            put(ws, g("coil_s" + suffix), f"={g('sens')}+{g('oa_sens' + suffix)}+{g('fan')}", "Calc", "d2")
            put(ws, g("coil_l" + suffix), f"={g('lat')}+{g('oa_lat' + suffix)}", "Calc", "d2")
            put(ws, g("coil" + suffix), f"={g('coil_s' + suffix)}+{g('coil_l' + suffix)}", "Result", "d2")
        put(ws, g("fan"), f"={g('sa_ahu')}/LperM3*FanPressure/FanEfficiency/WperkW", "Calc", "d2")
        put(ws, g("wm2"), f"=IF({g('area')}>0,{g('coil')}*WperkW/{g('area')},0)", "Calc", "d1")
        put(ws, g("req_t"), f"=IF({g('sa_room')}>0,SUMPRODUCT(({AF.rng('ahu')}={key}){mask}*{AF.rng('supply')}*{AF.rng('troom')})/{g('sa_room')}-{g('sens')}*WperkW/(AirSensibleFactor*{g('sa_room')}),0)", "Calc", "d1")
        if not after:
            sc, ss = a.get("sched_code", "D"), a.get("sched_source", "")
            has = not blank(a.get("sched_supply_lps")) or not blank(a.get("sched_oa_lps"))
            if has and blank(ss):
                raise ValueError(f"AHU {a['tag']}: scheduled duty has no source id")
            _val(ctx, ws, g("sched"), a.get("sched_supply_lps"), sc, fmt="d0", cref=g("sched_c"), sref=g("sched_s"), src=ss,
                 label="scheduled supply", room=a["tag"])
            _val(ctx, ws, g("sched_oa"), a.get("sched_oa_lps"), sc, fmt="d0", cref=g("sched_c"), sref=g("sched_s"), src=ss,
                 label="scheduled OA", room=a["tag"])
            _prov_cells(ctx, ws, g("sched_c"), g("sched_s"), sc, ss, has)


def write_ahu(ctx: Ctx) -> None:
    ws = ctx.wb["AHU"]
    L, L2 = ctx.lay["AH"], ctx.lay["AH2"]
    title_block(ws, "AHU", "Airflow totalisation and coil duty per AHU. Supply = room supply + duct leakage (named cell). Outdoor air "
                "= the outdoor-air fraction of the highest-standard room × AHU supply (rule); the plain sum and the multiple-space "
                "value of the example are shown beside it. Table B repeats the AHU without the rooms the outdoor-air rule separates.", ctx)
    heads = [H("ahu", "AHU or system", "", 24), H("type", "Type", "", 9), H("n", "Rooms", "no.", 6), H("area", "Area", "m" + U, 9),
             H("vol", "Volume", "m³", 9), H("sa_room", "Room supply", "L/s", 10), H("leak", "Duct leakage", "%", 8),
             H("sa_ahu", "AHU supply airflow", "L/s", 10), H("oa_sum", "Sum of room outdoor air", "L/s", 10),
             H("crit", "Highest-standard room (sets the fraction)", "", 28), H("frac", "Highest room outdoor-air fraction", "–", 9),
             H("oa_rule", "AHU outdoor air = fraction × supply", "L/s", 11), H("vou", "Vou (sum of Voc)", "L/s", 9),
             H("vps", "Vps (sum of Vsc)", "L/s", 9), H("xs", "Xs = Vou / Vps", "–", 8), H("zd", "Zd, highest Voc / Vsc", "–", 8),
             H("ev", "Ev = 1 + Xs − Zd", "–", 8), H("vot", "Vot = Vou / Ev", "L/s", 9),
             H("oa_ms", "Outdoor air as the example: max (sum, Vot)", "L/s", 11), H("ret", "Return = AHU supply − outdoor air", "L/s", 10),
             H("exh", "Exhaust", "L/s", 9), H("relief", "Spill or relief", "L/s", 9), H("sens", "Room sensible", "kW", 9),
             H("lat", "Room latent", "kW", 9), H("rt", "Reference room temperature (first room served)", "°C", 10), H("rh", "Reference room enthalpy (first room served)", "kJ/kg", 10),
             H("oa_sens", "Outdoor air sensible", "kW", 9), H("oa_lat", "Outdoor air latent", "kW", 9), H("fan", "Fan heat", "kW", 8),
             H("coil_s", "Coil sensible", "kW", 9), H("coil_l", "Coil latent", "kW", 9), H("coil", "Coil duty (outdoor-air rule)", "kW", 10),
             H("wm2", "Coil duty per area", "W/m" + U, 9),
             H("oa_sens_ms", "Outdoor air sensible, example basis", "kW", 10), H("oa_lat_ms", "Outdoor air latent, example basis", "kW", 10),
             H("coil_s_ms", "Coil sensible, example basis", "kW", 10), H("coil_l_ms", "Coil latent, example basis", "kW", 10),
             H("coil_ms", "Coil duty, example basis", "kW", 10),
             H("req_t", "Supply temperature required by the sensible load", "°C", 10), H("sched", "Scheduled supply", "L/s", 9),
             H("sched_oa", "Scheduled outdoor air", "L/s", 9), H("sched_c", "Code", "", 5), H("sched_s", "Source ID", "", 11)]
    bands = {"ahu": "A. AS ZONED: AIRFLOW", "frac": "OUTDOOR AIR", "vou": "MULTIPLE SPACE (example)", "ret": "RETURN, EXHAUST, RELIEF",
             "sens": "COIL DUTY", "oa_sens_ms": "COIL DUTY ON THE EXAMPLE'S OUTDOOR-AIR BASIS", "req_t": "CHECKS AND SCHEDULE"}
    write_headers(ws, L, heads, 4, bands)
    _ahu_table(ctx, ws, L, False)
    # second table
    b = L2.first - 3
    for k, h, u, w in heads:
        _hl(ws, f"{L2.col(k)}{b + 1}", h, "Header")
        _hl(ws, f"{L2.col(k)}{b + 2}", u or None, "Unit")
    ws.row_dimensions[b + 1].height = 42
    put(ws, f"A{b}", "B. AFTER THE OUTDOOR-AIR RULE: rooms separated onto FCUs removed", "HeaderLeft")
    _ahu_table(ctx, ws, L2, True)
    # totals rows
    for LL in (L, L2):
        if not ctx.ahus:
            continue
        tr = LL.last + 1
        put(ws, f"A{tr}", "TOTAL", "ResultTag")
        for k in ("n", "area", "vol", "sa_room", "sa_ahu", "oa_sum", "oa_rule", "oa_ms", "ret", "exh", "relief", "sens", "lat",
                  "oa_sens", "oa_lat", "fan", "coil_s", "coil_l", "coil", "coil_ms", "coil_s_ms", "coil_l_ms",
                  "oa_sens_ms", "oa_lat_ms"):
            put(ws, f"{LL.col(k)}{tr}", f"=SUM({LL.col(k)}{LL.first}:{LL.col(k)}{LL.last})", "Total",
                "d0" if k == "n" else ("d1" if k in ("area", "vol", "sa_room", "sa_ahu", "oa_sum", "oa_rule", "oa_ms", "ret", "exh", "relief") else "d2"))
    ctx.ahu_total_row = L.last + 1
    ctx.ahu2_total_row = L2.last + 1
    if ctx.ahus:
        cf_formula(ws, f"{L.col('req_t')}{L.first}:{L.col('req_t')}{L.last}",
                   f"AND({L.col('req_t')}{L.first}<SupplyTemp,{L.col('sa_room')}{L.first}>0)")
        cf_formula(ws, f"{L.col('sched')}{L.first}:{L.col('sched')}{L.last}",
                   f'AND({L.col("sched")}{L.first}<>"",{L.col("sched")}{L.first}<{L.col("sa_ahu")}{L.first})')
    list_validation(ws, f"{L.col('type')}{L.first}:{L.col('type')}{L.last}", "ListSystemType")
    band_rows(ws, f"A{L.first}:{L.col('sched_s')}{L.last}")
    band_rows(ws, f"A{L2.first}:{L2.col('sched_s')}{L2.last}")
    group_cols(ws, L, "vou", "vot")
    group_cols(ws, L, "oa_sens_ms", "coil_ms")
    _setup(ws, ctx, L, L.idx["sched_s"], ctx.ahu2_total_row, "4:6", f"{L.col('crit')}{L.first}", "FF00B050")
    ws.freeze_panes = f"B{L.first}"


# --------------------------------------------------------------------------- #
def write_fcu(ctx: Ctx) -> None:
    ws = ctx.wb["FCU"]
    L = ctx.lay["FC"]
    AF, FN = ctx.lay["AF"], ctx.lay["FN"]
    title_block(ws, "FCU", "Rooms on fan coil units: rooms whose system type is FCU, and rooms separated by the outdoor-air rule. "
                "The coil covers the room load plus outdoor air as it arrives: untreated air from an outdoor-air fan, or treated air from "
                "a PAHU at its off-coil condition. The outdoor-air source is the room's own input, else the PAHU (or outdoor-air fan) that "
                "serves the room's AHU on Fans; the capacity columns test that unit with the added airflow. Blank rows are unused list positions.", ctx)
    heads = [H("k", "No.", "", 5), H("no", "Room number", "", 14), H("name", "Room name", "", 26), H("ahu", "Original AHU or system", "", 24),
             H("why", "Reason", "", 12), H("area", "Area", "m" + U, 8), H("sens_kw", "Room sensible", "kW", 9),
             H("lat_kw", "Room latent", "kW", 9), H("supply", "Room supply", "L/s", 9), H("oa", "Outdoor air", "L/s", 9),
             H("oasrc", "Outdoor-air source (PAHU or fan)", "", 22), H("oaby", "Source chosen by", "", 14),
             H("oacond", "Outdoor air arrives", "", 11),
             H("oa_t", "Arrival temperature", "°C", 9), H("oa_h", "Arrival enthalpy", "kJ/kg", 9),
             H("oa_sens", "Outdoor air sensible", "kW", 9), H("oa_lat", "Outdoor air latent", "kW", 9),
             H("coil_s", "Coil sensible", "kW", 9), H("coil_l", "Coil latent", "kW", 9), H("coil", "FCU coil duty", "kW", 10),
             H("cap_sched", "Source unit, scheduled", "L/s", 10), H("cap_drawn", "Source unit, drawn with the added air", "L/s", 11),
             H("cap", "Source capacity", "", 12), H("flag", "Flag", "", 44)]
    write_headers(ws, L, heads, 4, {"k": "ROOM", "area": "LOAD", "oasrc": "OUTDOOR AIR", "oa_sens": "COIL", "cap_sched": "CAPACITY OF THE SOURCE",
                                    "flag": "FLAG"})
    ri = ctx.lay["RI"]
    for t in range(ctx.n_fcu_rows):
        g = lambda k: L.local(k, t)
        put(ws, g("k"), f"=ROWS({L.col('k')}${L.first}:{L.col('k')}{L.row(t)})", "Calc", "d0")
        mx = f"MAX({AF.rng('fcurank')})"
        idx = f"MATCH({g('no')},{AF.rng('no')},0)"
        put(ws, g("no"), f'=IF({g("k")}>{mx},"",INDEX({AF.rng("no")},MATCH({g("k")},{AF.rng("fcurank")},0)))', "LinkTag").alignment = Alignment(horizontal="left")
        for k, src, fmt, left in (("name", AF.rng("name"), None, True), ("ahu", AF.rng("ahu"), None, True),
                                  ("area", AF.rng("area"), "d1", False), ("supply", AF.rng("supply"), "d1", False),
                                  ("oa", AF.rng("oa"), "d1", False)):
            _hl(ws, g(k), f'=IF({g("no")}="","",INDEX({src},{idx}))', "Link", fmt, left)
        _hl(ws, g("why"), f'=IF({g("no")}="","",IF(INDEX({AF.rng("systype")},{idx})="FCU","SYSTEM TYPE","OA RULE"))', "Calc", left=True)
        put(ws, g("sens_kw"), f'=IF({g("no")}="","",INDEX({AF.rng("sens")},{idx})/WperkW)', "Link", "d2")
        put(ws, g("lat_kw"), f'=IF({g("no")}="","",INDEX({AF.rng("lat")},{idx})/WperkW)', "Link", "d2")
        own = f"INDEX({ri.rng('oasrc')},{idx})"
        mp, mo = f"MATCH({g('ahu')},{FN.rng('pk_p')},0)", f"MATCH({g('ahu')},{FN.rng('pk_o')},0)"
        if ctx.fans:
            dflt = (f'IF(ISNUMBER({mp}),INDEX({FN.rng("tag")},{mp}),IF(ISNUMBER({mo}),INDEX({FN.rng("tag")},{mo}),"not set"))')
        else:
            dflt = '"not set"'
        _hl(ws, g("oasrc"), f'=IF({g("no")}="","",IF({own}<>"",{own},{dflt}))', "Link", left=True)
        _hl(ws, g("oaby"), f'=IF({g("no")}="","",IF({own}<>"","ROOM INPUT",IF({g("oasrc")}="not set","NOT SET","FANS DEFAULT")))', "Calc", left=True)
        if ctx.fans:
            ftype = f'IF(ISNUMBER(MATCH({g("oasrc")},{FN.rng("tag")},0)),INDEX({FN.rng("type")},MATCH({g("oasrc")},{FN.rng("tag")},0)),"")'
        else:
            ftype = '""'
        _hl(ws, g("oacond"), (f'=IF({g("no")}="","",IF(INDEX({ri.rng("oacond")},{idx})<>"",INDEX({ri.rng("oacond")},{idx}),'
                              f'IF({ftype}="PAHU","TREATED","UNTREATED")))'), "Link", left=True)
        put(ws, g("oa_t"), f'=IF({g("no")}="","",IF({g("oacond")}="TREATED",PAHUOffT,OutdoorDB))', "Calc", "d1")
        put(ws, g("oa_h"), f'=IF({g("no")}="","",IF({g("oacond")}="TREATED",PAHUOffH,OutdoorH))', "Calc", "d1")
        troom = f"INDEX({AF.rng('troom')},{idx})"
        hroom = f"INDEX({AF.rng('hroom')},{idx})"
        put(ws, g("oa_sens"), f'=IF({g("no")}="","",AirSensibleFactor*{g("oa")}/WperkW*({g("oa_t")}-{troom}))', "Calc", "d2")
        put(ws, g("oa_lat"), f'=IF({g("no")}="","",AirDensity*{g("oa")}/WperkW*({g("oa_h")}-{hroom})-{g("oa_sens")})', "Calc", "d2")
        put(ws, g("coil_s"), f'=IF({g("no")}="","",{g("sens_kw")}+{g("oa_sens")})', "Calc", "d2")
        put(ws, g("coil_l"), f'=IF({g("no")}="","",{g("lat_kw")}+{g("oa_lat")})', "Calc", "d2")
        put(ws, g("coil"), f'=IF({g("no")}="","",{g("coil_s")}+{g("coil_l")})', "Result", "d2")
        if ctx.fans:
            fm = f"MATCH({g('oasrc')},{FN.rng('tag')},0)"
            put(ws, g("cap_sched"), f'=IF({g("no")}="","",IF(ISNUMBER({fm}),INDEX({FN.rng("sched")},{fm}),""))', "Link", "d0")
            put(ws, g("cap_drawn"), f'=IF({g("no")}="","",IF(ISNUMBER({fm}),INDEX({FN.rng("drawn")},{fm}),""))', "Link", "d1")
            put(ws, g("cap"), f'=IF({g("no")}="","",IF(ISNUMBER({fm}),INDEX({FN.rng("status")},{fm}),"NOT SET"))', "Check")
        else:
            put(ws, g("cap_sched"), None, "Link", "d0")
            put(ws, g("cap_drawn"), None, "Link", "d1")
            put(ws, g("cap"), f'=IF({g("no")}="","","NOT SET")', "Check")
        _hl(ws, g("flag"), (f'=IF({g("no")}="","",IF({g("oasrc")}="not set","OUTDOOR-AIR SOURCE NOT SET: CHOOSE FROM ZONING; COIL ON UNTREATED AIR UNTIL THEN",'
                            f'IF(AND({g("oacond")}="TREATED",PAHUOffT=""),"PAHU OFF-COIL CONDITION NOT SET",'
                            f'IF({g("cap")}="FAIL","SOURCE UNIT TOO SMALL WITH THE ADDED AIR",IF({g("oaby")}="FANS DEFAULT","SOURCE TAKEN FROM FANS: CONFIRM","")))))'),
            "Calc", left=True)
    if ctx.n_fcu_rows:
        band_rows(ws, f"A{L.first}:{L.col('flag')}{L.last}")
        cf_formula(ws, f"{L.col('flag')}{L.first}:{L.col('flag')}{L.last}", f'{L.col("flag")}{L.first}<>""')
        cf_text(ws, f"{L.col('cap')}{L.first}:{L.col('cap')}{L.last}", f"{L.col('cap')}{L.first}", "FAIL")
    put(ws, f"A{L.last + 2}", "Source capacity: the PAHU or outdoor-air fan on Fans carries the separated room's outdoor air on top of its AHU's; "
        "its status there is the test. The FCU itself is a new system: its selection follows the approved zoning change (Gate 3a).", "Note")
    _setup(ws, ctx, L, L.idx["flag"], L.last + 2, "4:6", f"{L.col('name')}{L.first}", "FF00B050")


# --------------------------------------------------------------------------- #
def write_fans(ctx: Ctx) -> None:
    ws = ctx.wb["Fans"]
    L, AF, AH = ctx.lay["FN"], ctx.lay["AF"], ctx.lay["AH"]
    AH2, FC = ctx.lay["AH2"], ctx.lay["FC"]
    title_block(ws, "Fans", "Return, exhaust and isolation-exhaust fans, outdoor-air fans and PAHUs: required airflow from the balance, "
                "duct leakage, drawn duty and margin against the scheduled duty. Airflow only: fan pressure is not calculated here.", ctx)
    heads = [H("tag", "Fan tag", "", 24), H("type", "Type", "", 16), H("serves", "Serves (AHU tag, or fan tag for exhaust)", "", 26),
             H("req", "Required airflow", "L/s", 10), H("leak", "Duct leakage", "%", 8), H("drawn", "Drawn duty", "L/s", 10),
             H("sched", "Scheduled duty", "L/s", 10), H("margin", "Margin of schedule over drawn duty", "%", 10),
             H("code", "Code", "", 5), H("src", "Source ID", "", 11), H("remark", "Remark", "", 40), H("status", "Status", "", 11),
             H("pk_p", "First PAHU of the AHU (FCU source)", "", 16), H("pk_o", "First outdoor-air fan of the AHU (FCU source)", "", 16)]
    write_headers(ws, L, heads, 4, {"tag": "FAN", "req": "AIRFLOW", "sched": "SCHEDULE", "pk_p": "FCU OUTDOOR-AIR SOURCE"})
    for t, f in enumerate(ctx.fans):
        g = lambda k: L.local(k, t)
        code, src = f.get("code", "D"), f.get("source", "")
        if blank(src):
            raise ValueError(f"fan {f['tag']} has no source id")
        for k, fld, kind, fmt in (("tag", "tag", "tag", None), ("type", "type", "text", None), ("serves", "serves", "tag", None),
                                  ("sched", "scheduled_lps", "num", "d0")):
            _val(ctx, ws, g(k), f.get(fld), code, fmt=fmt, kind=kind, cref=g("code"), sref=g("src"), src=src, label=f"fan {fld}",
                 room=f["tag"])
        _prov_cells(ctx, ws, g("code"), g("src"), code, src, True)
        # outdoor air of the AHU after the outdoor-air rule, plus the outdoor air of FCU rooms that draw on this unit
        oa_ahu = (f"INDEX({AH2.rng('oa_rule')},MATCH({g('serves')},{AH2.rng('ahu')},0))"
                  f"+SUMIFS({FC.rng('oa')},{FC.rng('oasrc')},{g('tag')})")
        put(ws, g("req"), (f'=IF({g("type")}="RETURN",SUMIFS({AF.rng("ret")},{AF.rng("ahu")},{g("serves")}),'
                           f'IF(OR({g("type")}="OUTDOOR AIR",{g("type")}="PAHU"),{oa_ahu},'
                           f'IF(OR({g("type")}="EXHAUST",{g("type")}="ISOLATION EXHAUST"),SUMIFS({AF.rng("exhaust")},{AF.rng("exfan")},{g("tag")}),0)))'),
            "Calc", "d1")
        put(ws, g("leak"), "=DuctLeakage", "Link", "pct1")
        put(ws, g("drawn"), f"={g('req')}*(1+{g('leak')})", "Result", "d1")
        put(ws, g("margin"), f'=IF(AND({g("sched")}<>"",{g("drawn")}>0),{g("sched")}/{g("drawn")}-1,"")', "Calc", "pct1")
        put(ws, g("status"), f'=IF({g("sched")}="","NO SCHEDULE",IF({g("sched")}>={g("drawn")},"PASS","FAIL"))', "Check")
        put(ws, g("remark"), f.get("remark"), "Note")
        put(ws, g("pk_p"), f'=IF(AND({g("type")}="PAHU",COUNTIFS({L.upto("type", t)},"PAHU",{L.upto("serves", t)},{g("serves")})=1),{g("serves")},"")', "CalcTag")
        put(ws, g("pk_o"), f'=IF(AND({g("type")}="OUTDOOR AIR",COUNTIFS({L.upto("type", t)},"OUTDOOR AIR",{L.upto("serves", t)},{g("serves")})=1),{g("serves")},"")', "CalcTag")
    if ctx.fans:
        list_validation(ws, f"{L.col('type')}{L.first}:{L.col('type')}{L.last}", "ListFanType")
        band_rows(ws, f"A{L.first}:{L.col('status')}{L.last}")
        cf_text(ws, f"{L.col('status')}{L.first}:{L.col('status')}{L.last}", f"{L.col('status')}{L.first}", "FAIL")
    _setup(ws, ctx, L, L.idx["pk_o"], L.last, "4:6", f"{L.col('tag')}{L.first}", "FF00B050")


def write_vav(ctx: Ctx) -> None:
    ws = ctx.wb["VAV"]
    L, AF = ctx.lay["VV"], ctx.lay["AF"]
    title_block(ws, "VAV", "One row per terminal: rooms served, design maximum, minimum (the largest of the air-change, outdoor-air and "
                "pressure needs of each room) and turndown. A room whose terminal type no document shows is listed on Flags.", ctx)
    heads = [H("tag", "Terminal tag", "", 18), H("type", "Terminal type", "", 11), H("rooms", "Rooms served", "", 36), H("n", "Rooms", "no.", 6),
             H("max", "Design maximum", "L/s", 10), H("min", "Minimum", "L/s", 10), H("turn", "Turndown, min / max", "%", 10),
             H("code", "Code", "", 5), H("src", "Source ID", "", 11), H("remark", "Remark", "", 40)]
    write_headers(ws, L, heads, 4, {"tag": "TERMINAL", "max": "AIRFLOW"})
    for t, v in enumerate(ctx.terminals):
        g = lambda k: L.local(k, t)
        code, src = v.get("code", "D"), v.get("source", "")
        if blank(src):
            raise ValueError(f"terminal {v['tag']} has no source id")
        for k, fld, kind in (("tag", "tag", "tag"), ("type", "type", "text")):
            _val(ctx, ws, g(k), v.get(fld), code, kind=kind, cref=g("code"), sref=g("src"), src=src, label=f"terminal {fld}", room=v["tag"])
        _prov_cells(ctx, ws, g("code"), g("src"), code, src, True)
        put(ws, g("rooms"), ", ".join(v.get("rooms", [])), "Text")
        put(ws, g("n"), f"=COUNTIF({AF.rng('terminal')},{g('tag')})", "Calc", "d0")
        put(ws, g("max"), f"=SUMIFS({AF.rng('supply')},{AF.rng('terminal')},{g('tag')})", "Result", "d1")
        put(ws, g("min"), f"=SUMIFS({AF.rng('minflow')},{AF.rng('terminal')},{g('tag')})", "Result", "d1")
        put(ws, g("turn"), f"=IF({g('max')}>0,{g('min')}/{g('max')},0)", "Calc", "pct1")
        put(ws, g("remark"), v.get("remark"), "Note")
    if ctx.terminals:
        list_validation(ws, f"{L.col('type')}{L.first}:{L.col('type')}{L.last}", "ListTerminal")
        band_rows(ws, f"A{L.first}:{L.col('remark')}{L.last}")
    _setup(ws, ctx, L, L.idx["remark"], L.last, "4:6", f"{L.col('tag')}{L.first}", "FF00B050")


def write_venturi(ctx: Ctx) -> None:
    ws = ctx.wb["Venturi Valves"]
    L, AF, PS = ctx.lay["VN"], ctx.lay["AF"], ctx.lay["PS"]
    title_block(ws, "Venturi Valves", "One row per valve: design airflow, and from Pressure the offset needed, the design offset as the top "
                "of the valve's range and the offset of a tighter room (1.0 m³/h per m²), so that the range covers both ends.", ctx)
    heads = [H("tag", "Valve tag", "", 18), H("room", "Room number", "", 14), H("role", "Supply or exhaust", "", 10),
             H("d1", "Design airflow, normal mode", "L/s", 10), H("d2", "Design airflow, second mode", "L/s", 10),
             H("off", "Offset needed", "m³/h", 9), H("design", "Design offset: top of range", "m³/h", 10),
             H("tight", "Offset of a tighter room", "m³/h", 10), H("ratio", "Ratio", "–", 7), H("status", "Status", "", 9),
             H("code", "Code", "", 5), H("src", "Source ID", "", 11), H("remark", "Remark", "", 36)]
    write_headers(ws, L, heads, 4, {"tag": "VALVE", "d1": "AIRFLOW", "off": "FROM PRESSURE"})
    for t, v in enumerate(ctx.valves):
        g = lambda k: L.local(k, t)
        code, src = v.get("code", "D"), v.get("source", "")
        if blank(src):
            raise ValueError(f"valve {v['tag']} has no source id")
        for k, fld, kind, fmt in (("tag", "tag", "tag", None), ("room", "room", "tag", None), ("role", "role", "text", None),
                                  ("d2", "airflow_mode2_lps", "num", "d1")):
            _val(ctx, ws, g(k), v.get(fld), code, fmt=fmt, kind=kind, cref=g("code"), sref=g("src"), src=src, label=f"valve {fld}", room=v["tag"])
        _prov_cells(ctx, ws, g("code"), g("src"), code, src, True)
        col = f'IF({g("role")}="SUPPLY",{AF.rng("supply")},{AF.rng("exhaust")})'
        put(ws, g("d1"), f"=INDEX({col},MATCH({g('room')},{AF.rng('no')},0))", "Link", "d1")
        onp = f"COUNTIF({PS.rng('node')},{g('room')})=0"
        for k, key in (("off", "needed"), ("design", "design"), ("tight", "tighter")):
            put(ws, g(k), f'=IF({onp},"",INDEX({PS.rng(key)},MATCH({g("room")},{PS.rng("node")},0)))', "Link", "d1")
        put(ws, g("ratio"), f'=IF({onp},"",INDEX({PS.rng("ratio")},MATCH({g("room")},{PS.rng("node")},0)))', "Link", "d2")
        put(ws, g("status"), f'=IF({onp},"",INDEX({PS.rng("status")},MATCH({g("room")},{PS.rng("node")},0)))', "Check")
        put(ws, g("remark"), v.get("remark"), "Note")
    if ctx.valves:
        list_validation(ws, f"{L.col('role')}{L.first}:{L.col('role')}{L.last}", "ListValveRole")
        band_rows(ws, f"A{L.first}:{L.col('remark')}{L.last}")
        cf_text(ws, f"{L.col('status')}{L.first}:{L.col('status')}{L.last}", f"{L.col('status')}{L.first}", "FAIL")
    _setup(ws, ctx, L, L.idx["remark"], L.last, "4:6", f"{L.col('tag')}{L.first}", "FF00B050")


# --------------------------------------------------------------------------- #
def write_flow_balance(ctx: Ctx) -> None:
    ws = ctx.wb["Flow Balance"]
    L, L2, AF, PS, AH = ctx.lay["FB"], ctx.lay["FB2"], ctx.lay["AF"], ctx.lay["PS"], ctx.lay["AH"]
    title_block(ws, "Flow Balance", "Air in against air out per room, system and AHU. For rooms on Pressure the leakage paths close the "
                "balance; an exhaust-only room is fed by transfer air and shows TRANSFER AIR.", ctx)
    heads = [H("no", "Room number", "", 14), H("name", "Room name", "", 28), H("ahu", "AHU or system", "", 24), H("supply", "Supply", "L/s", 9),
             H("oa", "Outdoor air", "L/s", 9), H("exhaust", "Exhaust", "L/s", 9), H("ret", "Return", "L/s", 9),
             H("extract", "Total extract", "L/s", 9), H("leak_in", "Leakage in", "L/s", 9), H("leak_out", "Leakage out", "L/s", 9),
             H("inn", "Air in", "L/s", 9), H("out", "Air out", "L/s", 9), H("net", "Net, in − out", "L/s", 9),
             H("close", "Tolerance", "L/s", 9), H("status", "Closure", "", 14), H("ach", "Net as air changes", "ACH", 8)]
    write_headers(ws, L, heads, 4, {"no": "ROOM", "supply": "AIR", "leak_in": "PRESSURE LEAKAGE", "inn": "BALANCE"})
    for i, r in enumerate(ctx.rooms):
        g = lambda k: L.local(k, i)
        for k, src, st in (("no", "no", "LinkTag"), ("name", "name", "Link"), ("ahu", "ahu", "LinkTag"), ("supply", "supply", "Link"),
                           ("oa", "oa", "Link"), ("exhaust", "exhaust", "Link"), ("ret", "ret", "Link"), ("extract", "extract", "Link")):
            _hl(ws, g(k), f"={AF.addr(src, i)}", st, "d1" if st == "Link" and k not in ("name",) else None, st != "Link" or k == "name")
        node = ctx.ps_index.get(r["number"])
        if node is not None:
            q = node["index"]
            put(ws, g("leak_in"), f"={PS.addr('air_in', q)}/ConvM3hPerLps", "Link", "d1")
            put(ws, g("leak_out"), f"={PS.addr('air_out', q)}/ConvM3hPerLps", "Link", "d1")
        else:
            put(ws, g("leak_in"), None, "Link", "d1")
            put(ws, g("leak_out"), None, "Link", "d1")
        put(ws, g("inn"), f"={g('supply')}+{g('leak_in')}", "Calc", "d1")
        put(ws, g("out"), f"={g('extract')}+{g('leak_out')}", "Calc", "d1")
        put(ws, g("net"), f"={g('inn')}-{g('out')}", "Result", "d1")
        put(ws, g("close"), f"=MAX(BalTolLps,BalTolPct*{g('inn')})", "Calc", "d1")
        put(ws, g("status"), f'=IF(ABS({g("net")})<={g("close")},"PASS",IF({g("supply")}=0,"TRANSFER AIR",IF({AF.addr("fix", i)}=1,"SPECIFIED","FAIL")))', "Check")
        put(ws, g("ach"), f"=IF({AF.addr('vol', i)}>0,ConvM3hPerLps*{g('net')}/{AF.addr('vol', i)},0)", "Calc", "d2")
    band_rows(ws, f"A{L.first}:{L.col('ach')}{L.last}")
    cf_text(ws, f"{L.col('status')}{L.first}:{L.col('status')}{L.last}", f"{L.col('status')}{L.first}", "FAIL")
    # AHU summary
    put(ws, f"A{L2.first - 4}", "By AHU (Rev A 'Flow Balance' definitions)", "Section")
    heads2 = [H("ahu", "AHU or system", "", 24), H("supply", "Room supply", "L/s", 9), H("oa", "AHU outdoor air", "L/s", 9),
              H("exhaust", "Exhaust", "L/s", 9), H("ret", "Return", "L/s", 9), H("relief", "Net positive air to relieve", "L/s", 10),
              H("net_ach", "As air changes", "ACH", 8), H("area", "Area", "m" + U, 8), H("vol", "Volume", "m³", 9),
              H("sum_net", "Sum of room nets", "L/s", 9), H("close", "Tolerance", "L/s", 9), H("status", "Closure", "", 14), H("note", "Note", "", 46)]
    for k, h, u, w in heads2:
        _hl(ws, f"{L2.col(k)}{L2.first - 2}", h, "Header")
        _hl(ws, f"{L2.col(k)}{L2.first - 1}", u or None, "Unit")
    ws.row_dimensions[L2.first - 2].height = 42
    for t, a in enumerate(ctx.ahus):
        g = lambda k: L2.local(k, t)
        _hl(ws, g("ahu"), f"={AH.addr('ahu', t)}", "LinkTag", left=True)
        for k, src in (("supply", "sa_room"), ("oa", "oa_rule"), ("exhaust", "exh"), ("ret", "ret"), ("relief", "relief"),
                       ("area", "area"), ("vol", "vol")):
            put(ws, g(k), f"={AH.addr(src, t)}", "Link", "d1")
        put(ws, g("net_ach"), f"=IF({g('vol')}>0,ConvM3hPerLps*{g('relief')}/{g('vol')},0)", "Calc", "d2")
        put(ws, g("sum_net"), f"=SUMIFS({L.rng('net')},{L.rng('ahu')},{g('ahu')})", "Calc", "d1")
        put(ws, g("close"), f"=MAX(BalTolLps,BalTolPct*{g('supply')})", "Calc", "d1")
        put(ws, g("status"), f'=IF(AND(ABS({g("relief")}-{g("oa")}+{g("exhaust")})<={g("close")},{g("ret")}>=0,{g("oa")}<={AH.addr("sa_ahu", t)}+{g("close")}),"PASS","FAIL")', "Check")
        put(ws, g("note"), (f'=IF({g("relief")}<-{g("close")},"Negative relief: "&TEXT(-{g("relief")},"0")&" L/s of make-up air is drawn as transfer air from '
                            f'adjacent spaces; the corridor or adjacent system must supply it.",IF({g("relief")}>{g("close")},"Positive relief: "&TEXT({g("relief")},"0")&" L/s to spill or transfer out.",""))'), "Note")
    if ctx.ahus:
        cf_text(ws, f"{L2.col('status')}{L2.first}:{L2.col('status')}{L2.last}", f"{L2.col('status')}{L2.first}", "FAIL")
    _setup(ws, ctx, L, L.idx["ach"], L2.last, "4:6", f"{L.col('name')}{L.first}", "FF00B050")


# --------------------------------------------------------------------------- #
def write_comparison(ctx: Ctx) -> None:
    ws = ctx.wb["Airflow Comparison"]
    L, L2, L3, AF, AH, CR = ctx.lay["CP"], ctx.lay["CP2"], ctx.lay["CP3"], ctx.lay["AF"], ctx.lay["AH"], ctx.lay["CR"]
    title_block(ws, "Airflow Comparison", "Calculated airflows against the working-drawing and tender schematics, room by room and AHU by AHU, "
                "and the reference rows of the example. Update rule: where this workbook needs more air than the drawing by more than 5 % "
                "or 10 L/s the mark-up shows the increase; where it needs less the drawn value stays and the margin is recorded.", ctx)
    heads = [H("no", "Room number", "", 14), H("name", "Room name", "", 26), H("ahu", "AHU or system", "", 22),
             H("calc_s", "Supply, calculated", "L/s", 9), H("sch_s", "Supply, schematic", "L/s", 9), H("d_s", "Difference", "L/s", 8),
             H("dp_s", "Difference", "%", 8), H("calc_r", "Return, calculated", "L/s", 9), H("sch_r", "Return, schematic", "L/s", 9),
             H("d_r", "Difference", "L/s", 8), H("calc_e", "Exhaust, calculated", "L/s", 9), H("sch_e", "Exhaust, schematic", "L/s", 9),
             H("d_e", "Difference", "L/s", 8), H("pa_calc", "Pressure target used", "Pa", 8), H("pa_sch", "Pressure on schematic", "Pa", 8),
             H("flag", "Mark-up rule", "", 26), H("code", "Code", "", 5), H("src", "Source ID", "", 11), H("remark", "Remark", "", 34)]
    write_headers(ws, L, heads, 4, {"no": "ROOM", "calc_s": "SUPPLY", "calc_r": "RETURN", "calc_e": "EXHAUST", "pa_calc": "PRESSURE", "flag": "RULE"})
    for i, r in enumerate(ctx.rooms):
        g = lambda k: L.local(k, i)
        sc = r.get("schematic") or {}
        has = any(not blank(sc.get(k)) for k in ("supply_lps", "return_lps", "exhaust_lps", "pressure_pa"))
        code, src = (sc.get("code", "D"), sc.get("source", "")) if has else ("", "")
        if has and blank(src):
            raise ValueError(f"room {r['number']}: schematic values have no source id")
        _hl(ws, g("no"), f"={AF.addr('no', i)}", "LinkTag", left=True)
        _hl(ws, g("name"), f"={AF.addr('name', i)}", "Link", left=True)
        _hl(ws, g("ahu"), f"={AF.addr('ahu', i)}", "LinkTag", left=True)
        for k, s in (("calc_s", "supply"), ("calc_r", "ret"), ("calc_e", "exhaust")):
            put(ws, g(k), f"={AF.addr(s, i)}", "Link", "d1")
        for k, fld in (("sch_s", "supply_lps"), ("sch_r", "return_lps"), ("sch_e", "exhaust_lps"), ("pa_sch", "pressure_pa")):
            _val(ctx, ws, g(k), sc.get(fld), code, fmt="d1", cref=g("code"), sref=g("src"), src=src, label=f"schematic {fld}", room=r["number"])
        _prov_cells(ctx, ws, g("code"), g("src"), code, src, has)
        put(ws, g("d_s"), f'=IF({g("sch_s")}="","",{g("calc_s")}-{g("sch_s")})', "Calc", "d1")
        put(ws, g("dp_s"), f'=IF(OR({g("sch_s")}="",{g("sch_s")}=0),"",{g("d_s")}/{g("sch_s")})', "Calc", "pct1")
        put(ws, g("d_r"), f'=IF({g("sch_r")}="","",{g("calc_r")}-{g("sch_r")})', "Calc", "d1")
        put(ws, g("d_e"), f'=IF({g("sch_e")}="","",{g("calc_e")}-{g("sch_e")})', "Calc", "d1")
        put(ws, g("pa_calc"), f"={CR.addr('pa', i)}", "Link", "d1")
        _hl(ws, g("flag"), (f'=IF({g("sch_s")}="","NO SCHEMATIC VALUE",IF(OR({g("dp_s")}>SchedTolPct,{g("d_s")}>SchedTolLps),'
                            f'"INCREASE ON MARK-UP",IF({g("d_s")}<0,"DRAWN VALUE STAYS; MARGIN RECORDED","AGREES")))'), "Calc", left=True)
        put(ws, g("remark"), sc.get("remark"), "Note")
    band_rows(ws, f"A{L.first}:{L.col('remark')}{L.last}")
    cf_text(ws, f"{L.col('flag')}{L.first}:{L.col('flag')}{L.last}", f"{L.col('flag')}{L.first}", "INCREASE ON MARK-UP", fill="FFFFF2CC")
    # AHU table
    put(ws, f"A{L2.first - 4}", "By AHU", "Section")
    heads2 = [H("ahu", "AHU or system", "", 22), H("calc", "AHU supply, calculated", "L/s", 9), H("sch", "AHU duty, schematic or schedule", "L/s", 9),
              H("d", "Difference", "L/s", 8), H("dp", "Difference", "%", 8), H("sched", "Scheduled duty (AHU tab)", "L/s", 9),
              H("d_sched", "Calculated − scheduled", "L/s", 9), H("flag", "Mark-up rule", "", 26), H("code", "Code", "", 5),
              H("src", "Source ID", "", 11), H("remark", "Remark", "", 34)]
    for k, h, u, w in heads2:
        _hl(ws, f"{L2.col(k)}{L2.first - 2}", h, "Header")
        _hl(ws, f"{L2.col(k)}{L2.first - 1}", u or None, "Unit")
    ws.row_dimensions[L2.first - 2].height = 42
    for t, a in enumerate(ctx.ahus):
        g = lambda k: L2.local(k, t)
        _hl(ws, g("ahu"), f"={AH.addr('ahu', t)}", "LinkTag", left=True)
        put(ws, g("calc"), f"={AH.addr('sa_ahu', t)}", "Link", "d1")
        sc, ss = a.get("schematic_code", "D"), a.get("schematic_source", "")
        has = not blank(a.get("schematic_supply_lps"))
        if has and blank(ss):
            raise ValueError(f"AHU {a['tag']}: schematic duty has no source id")
        _val(ctx, ws, g("sch"), a.get("schematic_supply_lps"), sc, fmt="d0", cref=g("code"), sref=g("src"), src=ss, label="schematic duty", room=a["tag"])
        _prov_cells(ctx, ws, g("code"), g("src"), sc, ss, has)
        put(ws, g("d"), f'=IF({g("sch")}="","",{g("calc")}-{g("sch")})', "Calc", "d1")
        put(ws, g("dp"), f'=IF(OR({g("sch")}="",{g("sch")}=0),"",{g("d")}/{g("sch")})', "Calc", "pct1")
        put(ws, g("sched"), f'=IF({AH.addr("sched", t)}="","",{AH.addr("sched", t)})', "Link", "d0")
        put(ws, g("d_sched"), f'=IF({g("sched")}="","",{g("calc")}-{g("sched")})', "Calc", "d1")
        _hl(ws, g("flag"), (f'=IF({g("sch")}="","NO SCHEMATIC VALUE",IF(OR({g("dp")}>SchedTolPct,{g("d")}>SchedTolLps),'
                            f'"INCREASE ON MARK-UP",IF({g("d")}<0,"DRAWN VALUE STAYS; MARGIN RECORDED","AGREES")))'), "Calc", left=True)
        put(ws, g("remark"), a.get("schematic_remark"), "Note")
    # reference rows
    put(ws, f"A{L3.first - 4}", "Reference rows (information only; not adopted)", "Section")
    heads3 = [H("label", "Reference", "", 22), H("flow", "Airflow", "L/s", 9), H("m3h", "Airflow", "m³/h", 9), H("ach", "Air changes on this workbook's volume", "ACH", 10),
              H("oa", "Outdoor air", "L/s", 9), H("diff", "This workbook (AHU supply) − reference", "L/s", 12), H("pct", "This workbook / reference", "%", 9),
              H("code", "Code", "", 5), H("src", "Source ID", "", 11), H("remark", "Remark", "", 34)]
    for k, h, u, w in heads3:
        _hl(ws, f"{L3.col(k)}{L3.first - 2}", h, "Header")
        _hl(ws, f"{L3.col(k)}{L3.first - 1}", u or None, "Unit")
    ws.row_dimensions[L3.first - 2].height = 42
    tot_sa = f"SUM({AH.rng('sa_ahu')})"
    tot_vol = f"SUM({AF.rng('vol')})"
    for t, ref in enumerate(ctx.references):
        g = lambda k: L3.local(k, t)
        code, src = ref.get("code", "D"), ref.get("source", "")
        if blank(src):
            raise ValueError(f"reference {ref.get('label')} has no source id")
        _val(ctx, ws, g("label"), ref["label"], code, kind="text", cref=g("code"), sref=g("src"), src=src)
        _val(ctx, ws, g("flow"), ref.get("flow_lps"), code, fmt="d0", cref=g("code"), sref=g("src"), src=src, label="reference airflow")
        _val(ctx, ws, g("oa"), ref.get("oa_lps"), code, fmt="d0", cref=g("code"), sref=g("src"), src=src, label="reference OA")
        _prov_cells(ctx, ws, g("code"), g("src"), code, src, True)
        put(ws, g("m3h"), f"={g('flow')}*ConvM3hPerLps", "Calc", "d0")
        put(ws, g("ach"), f"=IF({tot_vol}>0,{g('m3h')}/{tot_vol},0)", "Calc", "d2")
        put(ws, g("diff"), f"={tot_sa}-{g('flow')}", "Calc", "d0")
        put(ws, g("pct"), f"=IF({g('flow')}>0,{tot_sa}/{g('flow')},0)", "Calc", "pct1")
        put(ws, g("remark"), ref.get("remark"), "Note")
    _setup(ws, ctx, L, L.idx["remark"], L3.last, "4:6", f"{L.col('name')}{L.first}", "FF7F7F7F")
