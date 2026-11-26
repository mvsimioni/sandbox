"""AHU, FCU, Fans, VAV, Venturi Valves, Flow Balance and Airflow Comparison."""

from __future__ import annotations

from typing import Any, Dict, List

from openpyxl.styles import Alignment
from openpyxl.utils import get_column_letter

from .common import band_rows, cf_formula, cf_text, expand, group_cols, incomplete, list_validation, tender_remark, title_block, write_headers
from .ctx import Ctx
from .model import blank
from .sheets_rooms import _prov_cells, _setup, _val
from .styles import link_to, put

U = "²"


def H(key, header, unit, width):
    return (key, header, unit, width)


def _hl(ws, ref, f, style, fmt=None, left=False):
    c = put(ws, ref, f, style, fmt, text=left)
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
            put(ws, g("ahu"), f"={ctx.lay['AH'].local('ahu', t)}", "Tag")
        key = g("ahu")
        crit = f"{AF.rng('ahu')},{key}"
        ws[g("ahu")].alignment = Alignment(horizontal="left")
        _hl(ws, g("type"), a.get("type") if not after else f"={ctx.lay['AH'].local('type', t)}", "Text" if not after else "Link", left=True)
        sup = f',{AF.rng("supply")},">0"'
        put(ws, g("n"), f"=COUNTIFS({crit}{inc})", "Calc", "d0")
        put(ws, g("area"), f"=SUMIFS({AF.rng('area')},{crit}{inc})", "Calc", "d1")
        put(ws, g("area_sup"), f"=SUMIFS({AF.rng('area')},{crit}{inc}{sup})", "Calc", "d1")
        put(ws, g("vol"), f"=SUMIFS({AF.rng('vol')},{crit}{inc}{sup})", "Calc", "d1")
        put(ws, g("noh"), f'=COUNTIFS({crit}{inc},{ctx.lay["RI"].rng("h")},"")', "Calc", "d0")
        put(ws, g("nfix"), f"=COUNTIFS({crit}{inc},{AF.rng('fix')},1)", "Calc", "d0")
        put(ws, g("area_ok"), f'=SUMPRODUCT(--({AF.rng("ahu")}={key}){mask},--({AF.rng("supply")}>0),--({ctx.lay["CR"].rng("crit")}<>""),{AF.rng("area")})', "Calc", "d1")
        put(ws, g("nok"), f'=SUMPRODUCT(--({AF.rng("ahu")}={key}){mask},--({AF.rng("supply")}>0),--({ctx.lay["CR"].rng("crit")}<>""))', "Calc", "d0")
        put(ws, g("nocrit"), f'=SUMPRODUCT(--({AF.rng("ahu")}={key}){mask},--({ctx.lay["CR"].rng("crit")}=""))', "Calc", "d0")
        put(ws, g("area_nocrit"), f'=SUMPRODUCT(--({AF.rng("ahu")}={key}){mask},--({ctx.lay["CR"].rng("crit")}=""),{AF.rng("area")})', "Calc", "d1")
        put(ws, g("sa_room"), f"=SUMIFS({AF.rng('supply')},{crit}{inc})", "Calc", "d1")
        if not after:
            _hl(ws, g("ducted"), a.get("ducted"), "Text", left=True)
        else:
            _hl(ws, g("ducted"), f'=IF({ctx.lay["AH"].local("ducted", t)}="","",{ctx.lay["AH"].local("ducted", t)})', "Link", left=True)
        # duct leakage: 10 % on an AHU or a ducted fan coil unit, 0 % on a hi-wall fan coil unit
        put(ws, g("leak"), f'=IF({g("type")}="FCU",IF({g("ducted")}="N",FcuLeakageHiWall,FcuLeakageDucted),DuctLeakage)', "Link", "pct1")
        if not after:
            put(ws, g("spillreq"), a.get("spill_req_lps"), "Assumption" if a.get("spill_req_lps") else "Input", "d1")
        else:
            put(ws, g("spillreq"), f'=IF({ctx.lay["AH"].local("spillreq", t)}="",0,{ctx.lay["AH"].local("spillreq", t)})', "Link", "d1")
        # D-058: the future ultra-clean duty of a theatre governs the unit; the extra supply (future duty minus day-1 supply of the theatre) is added here
        if not after:
            put(ws, g("ucvadd"), a.get("ucv_add_lps"), "Assumption" if a.get("ucv_add_lps") else "Input", "d1")
        else:
            put(ws, g("ucvadd"), f'=IF({ctx.lay["AH"].local("ucvadd", t)}="",0,{ctx.lay["AH"].local("ucvadd", t)})', "Link", "d1")
        put(ws, g("sa_ahu"), f"=({g('sa_room')}+{g('spillreq')}+{g('ucvadd')})*(1+{g('leak')})", "Result", "d1")
        put(ws, g("oa_sum"), f"=SUMIFS({AF.rng('oa')},{crit}{inc})", "Calc", "d1")
        put(ws, g("frac"), f"=_xlfn.MAXIFS({AF.rng('frac')},{crit}{inc})", "Calc", "pct1")
        _hl(ws, g("crit"), f'=IF({g("frac")}>0,INDEX({AF.rng("name")},MATCH({key},{AF.rng(cm)},0)),"–")'
                                  f'&IF({nocrit_count(ctx, key)}>0," [incomplete: "&{nocrit_count(ctx, key)}&" rooms lack criteria]","")', "Calc", left=True)
        # AHU outdoor air: the larger of (a) the highest room fraction x AHU supply (clean single-pass and other rooms; dirty single-pass rooms carry only their own
        # minimum) and (b) the air that leaves the zone: exhaust with no return, net offset air leaving through the envelope, spill air to plant rooms.
        put(ws, g("oa_frac"), f"={g('frac')}*{g('sa_ahu')}", "Calc", "d1")
        put(ws, g("offnet"), f'=SUMIFS({AF.rng("off")},{crit}{inc},{AF.rng("kind")},"SUPPLY")-SUMIFS({AF.rng("off")},{crit}{inc},{AF.rng("kind")},"EXHAUST")', "Calc", "d1")
        put(ws, g("oa_bal"), f"={g('exh')}+MAX({g('offnet')},0)+{g('spillreq')}", "Calc", "d1")
        # D-110 (26 Nov 2026): minimum outdoor air of the multispace AHU. HFDS / ASHRAE 170 rooms: straight sum of the room outdoor air (no diversity,
        # no Ez, no Ev; 170 cl 7.1(a)(6)(i), IC 170-2013-14, IC 170-2017-4). SS 553 rooms: SS 553 Table 1 rate x floor area summed (pending Table 1:
        # the fraction basis stays until the rates are set). MIXED: both, pending. The balance basis (air that leaves the rooms) stays a floor.
        put(ws, g("oa_clin"), f'=SUMIFS({AF.rng("oa")},{crit}{inc},{AF.rng("oascope")},"HFDS/170")', "Calc", "d1")
        put(ws, g("oa_nc"), f'=SUMIFS({AF.rng("oa")},{crit}{inc},{AF.rng("oascope")},"SS 553")', "Calc", "d1")
        _hl(ws, g("ahucls"), (f'=IF(AND({g("oa_clin")}>0,{g("oa_nc")}>0),"MIXED",IF({g("oa_clin")}>0,"HFDS/170",'
                              f'IF({g("oa_nc")}>0,"SS 553","")))'), "Calc", left=True)
        # D-111: SS 553 part = sum of Table 1 rate x area (N2); MIXED = HFDS/170 straight sum + SS 553 sum (MS rules section 5)
        put(ws, g("oa_min"), f'={g("oa_clin")}+{g("oa_nc")}', "Calc", "d1")
        _hl(ws, g("oa_how"), (f'=IF({g("ahucls")}="HFDS/170","Straight sum of the room outdoor air (HFDS / ASHRAE 170 rooms)",'
                              f'IF({g("ahucls")}="SS 553","Sum of SS 553 Table 1 rate × area (and QP rates, cl 5.3.6)",'
                              f'IF({g("ahucls")}="MIXED","HFDS/170 straight sum + SS 553 sum (mixed unit, to confirm with the team)","No room with an outdoor-air rate")))'
                              f'&IF({g("oa_bal")}>{g("oa_min")},"; the air that leaves the rooms governs","")'), "Calc", left=True)
        put(ws, g("oa_short"), f"=MAX(MAX({g('oa_min')},{g('oa_bal')})-{g('sa_ahu')},0)", "Calc", "d1")
        put(ws, g("oa_rule"), f"=MIN({g('sa_ahu')},MAX({g('oa_min')},{g('oa_bal')}))", "Calc", "d1")
        put(ws, g("dn"), f'=COUNTIFS({crit}{inc},{AF.rng("spc")},"DIRTY"{sup})', "Calc", "d0")
        put(ws, g("dsup"), f'=SUMIFS({AF.rng("supply")},{crit}{inc},{AF.rng("spc")},"DIRTY")', "Calc", "d1")
        put(ws, g("dexh"), f'=SUMIFS({AF.rng("exhaust")},{crit}{inc},{AF.rng("spc")},"DIRTY")', "Calc", "d1")
        CRl = ctx.lay["CR"]
        put(ws, g("dirn"), (f'=SUMPRODUCT(--({AF.rng("ahu")}={key}){mask},--({AF.rng("supply")}>0),--(({CRl.rng("regime")}="POSITIVE")+({CRl.rng("regime")}="NEGATIVE")>0),'
                            f'--({CRl.rng("pa")}=""),--({AF.rng("spc")}<>"DIRTY"))'), "Calc", "d0")
        put(ws, g("dirsup"), (f'=SUMPRODUCT(--({AF.rng("ahu")}={key}){mask},--({AF.rng("supply")}>0),--(({CRl.rng("regime")}="POSITIVE")+({CRl.rng("regime")}="NEGATIVE")>0),'
                              f'--({CRl.rng("pa")}=""),--({AF.rng("spc")}<>"DIRTY"),{AF.rng("supply")})'), "Calc", "d1")
        put(ws, g("vou"), f"={g('oa_sum')}", "Calc", "d1")
        put(ws, g("vps"), f"={g('sa_room')}", "Calc", "d1")
        put(ws, g("xs"), f"=IF({g('vps')}>0,{g('vou')}/{g('vps')},0)", "Calc", "pct1")
        put(ws, g("zd"), f"={g('frac')}", "Calc", "pct1")
        put(ws, g("ev"), f"=1+{g('xs')}-{g('zd')}", "Calc", "d3")
        put(ws, g("vot"), f"=IF({g('ev')}>0,{g('vou')}/{g('ev')},0)", "Calc", "d1")
        put(ws, g("oa_ms"), f"=MAX({g('oa_sum')},{g('vot')})", "Calc", "d1")
        # R1-10 decided item 3: information only. Larger of (sum of room outdoor air x (1 + DuctLeakage)) and (highest fraction of the rooms that are not
        # single pass x AHU supply). It feeds neither the coil duty nor the Remarks. It shows the effect of option (b) of OI-R110-1.
        put(ws, g("fsp"), f'=_xlfn.MAXIFS({AF.rng("frac")},{crit}{inc},{AF.rng("single")},"<>Y")', "Calc", "pct1")
        put(ws, g("oa_mb"), f"=MAX({g('oa_sum')}*(1+{g('leak')}),{g('fsp')}*{g('sa_ahu')})", "Calc", "d1")
        # R1-10 decided item 2: the room that sets the highest fraction, when it is single pass
        mt = f"MATCH({key},{AF.rng(cm)},0)"
        put(ws, g("spr"), f'=IF(AND({g("frac")}>0,ISNUMBER({mt})),IF(INDEX({AF.rng("single")},{mt})="Y",INDEX({AF.rng("no")},{mt})&" "&INDEX({AF.rng("name")},{mt}),""),"")', "Calc")
        put(ws, g("spl"), f'=IF({g("spr")}<>"",INDEX({AF.rng("supply")},{mt}),0)', "Calc", "d1")
        put(ws, g("spy"), f'=IF({g("spr")}<>"",_xlfn.MAXIFS({AF.rng("frac")},{crit}{inc},{AF.rng("no")},"<>"&INDEX({AF.rng("no")},{mt})),0)', "Calc", "pct1")
        put(ws, g("spnote"), (f'=IF({g("spr")}="","","The fraction "&TEXT({g("frac")},"0.0#")&" comes from "&{g("spr")}&" (single pass, "'
                              f'&TEXT(IF({g("sa_room")}>0,{g("spl")}/{g("sa_room")},0),"0.0 %")&" of the supply). The other rooms on this unit have a fraction of "'
                              f'&TEXT({g("spy")},"0.0#")&" or less.")'), "Calc")
        put(ws, g("balnote"), (f'=IF({g("ahucls")}="HFDS/170","Outdoor air = straight sum of the room outdoor air (D-110). ",'
                               f'IF({g("ahucls")}="SS 553","Outdoor air = sum of the SS 553 room rates (D-111). ",IF({g("ahucls")}="MIXED","Outdoor air = HFDS/170 straight sum + SS 553 sum (D-111). ","")))&'
                               f'IF({g("spnote")}="","",{g("spnote")}&" ")'
                               f'&IF({g("dn")}>0,{g("dn")}&IF({g("dn")}=1," single-pass room is"," single-pass rooms are")&" dirty (supply "&TEXT({g("dsup")},"#,##0")&" L/s, exhaust "&TEXT({g("dexh")},"#,##0")&" L/s). '
                               f'"&IF({g("dn")}=1,"It takes","Each takes")&" recirculated supply from this unit and a dedicated exhaust with no return. It does not set the outdoor-air fraction. ","")'
                               f'&IF({g("oa_bal")}>{g("oa_min")},"The air that leaves the rooms sets the outdoor air ("&TEXT({g("oa_bal")},"#,##0")&" L/s): exhaust, offset air and spill air. ","")'
                               f'&IF({g("oa_short")}>0,"The air that leaves the rooms is "&TEXT({g("oa_short")},"#,##0")&" L/s more than the AHU supply. The make-up air must come from another source. ","")'
                               f'&IF({g("dirn")}>0,{g("dirn")}&IF({g("dirn")}=1," room has"," rooms have")&" a pressure direction and no pressure value (supply "&TEXT({g("dirsup")},"#,##0")&" L/s): directional airflow only, no offset is calculated, so the offset air is not in this balance. ","")'), "Calc")
        if not after:
            prev = f"COUNT({L.col('oaflg')}${L.first}:{L.col('oaflg')}{L.row(t - 1)})+1" if t else "1"
        else:
            prev = "0"
        put(ws, g("oaflg"), (f'=IF(AND(ISNUMBER({g("sched_oa")}),{g("oa_rule")}>{g("sched_oa")}),{prev},"")' if not after else '=""'), "Calc", "d0")
        put(ws, g("ret"), f"={g('sa_ahu')}-{g('oa_rule')}", "Calc", "d1")
        put(ws, g("exh"), f"=SUMIFS({AF.rng('exhaust')},{crit}{inc})", "Calc", "d1")
        put(ws, g("relief"), f"={g('oa_rule')}-{g('exh')}-{g('offnet')}", "Calc", "d1")
        put(ws, g("sens"), f"=SUMIFS({AF.rng('sens')},{crit}{inc})/WperkW", "Calc", "d2")
        put(ws, g("lat"), f"=SUMIFS({AF.rng('lat')},{crit}{inc})/WperkW", "Calc", "d2")
        ref = f"MATCH({key},{AF.rng('ref1' if after else 'ref0')},0)"
        # 25 Nov 2026 (Marcus): the return air is the supply-weighted mean of the rooms of the unit, not the first room served
        wsum = f"SUMPRODUCT(({AF.rng('ahu')}={key}){mask}*{AF.rng('supply')}*" + "{x})/" + g("sa_room")
        put(ws, g("rt"), f'=IF({g("sa_room")}>0,{wsum.format(x=AF.rng("troom"))},0)', "Calc", "d1")
        put(ws, g("rh"), f'=IF({g("sa_room")}>0,{wsum.format(x=AF.rng("hroom"))},0)', "Calc", "d1")
        for suffix, oa in (("", g("oa_rule")), ("_ms", g("oa_ms"))):
            put(ws, g("oa_sens" + suffix), f"=AirSensibleFactor*{oa}/WperkW*(OutdoorDB-{g('rt')})", "Calc", "d2")
            put(ws, g("oa_lat" + suffix), f"=AirDensity*{oa}/WperkW*(OutdoorH-{g('rh')})-{g('oa_sens' + suffix)}", "Calc", "d2")
            put(ws, g("coil_s" + suffix), f"={g('sens')}+{g('oa_sens' + suffix)}+{g('fan')}", "Calc", "d2")
            put(ws, g("coil_l" + suffix), f"={g('lat')}+{g('oa_lat' + suffix)}", "Calc", "d2")
            put(ws, g("coil" + suffix), f"={g('coil_s' + suffix)}+{g('coil_l' + suffix)}", "Calc", "d2")
        put(ws, g("fan"), f"={g('sa_ahu')}/LperM3*IF(OR({g('type')}=\"FCU\",LEFT({key},3)=\"FCU\",LEFT({key},4)=\"CRAC\"),FcuFanPressure,FanPressure)/FanEfficiency/WperkW", "Calc", "d2")
        put(ws, g("wm2"), f"=IF({g('area_sup')}>0,{g('coil')}*WperkW/{g('area_sup')},0)", "Calc", "d1")
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
            put(ws, g("tsrc"), a.get("tender_source_text"), "Text")
            _val(ctx, ws, g("second"), a.get("second_rooms"), "D", fmt="d0", cref=None, sref=g("second_s"), src=a.get("second_source", ""),
                 label="rooms of the second room list", room=a["tag"])
            put(ws, g("second_s"), a.get("second_source") if not blank(a.get("second_rooms")) else None, "Tag")
            put(ws, g("norr_why"), a.get("no_reduction_reason"), "Text")
            zc, zsid = a.get("zone_code", "D"), a.get("zone_source", "")
            zhas = not blank(a.get("zone_rooms"))
            _val(ctx, ws, g("zr"), a.get("zone_rooms"), zc, fmt="d0", cref=g("zc"), sref=g("zs"), src=zsid, label="rooms of the unit in the zone schedule", room=a["tag"])
            _val(ctx, ws, g("za"), a.get("zone_area_m2"), zc, fmt="d1", cref=g("zc"), sref=g("zs"), src=zsid, label="area of the unit in the zone schedule", room=a["tag"])
            _prov_cells(ctx, ws, g("zc"), g("zs"), zc, zsid, zhas)
            osrc = a.get("other_rooms_source", "")
            _val(ctx, ws, g("oth"), a.get("other_rooms"), "D", fmt="d0", cref=g("othc"), sref=g("oths"), src=osrc, label="rooms of other departments on the unit", room=a["tag"])
            _prov_cells(ctx, ws, g("othc"), g("oths"), "D", osrc, not blank(a.get("other_rooms")))
        else:
            A = ctx.lay["AH"]
            put(ws, g("sched"), f'=IF({A.local("sched", t)}="","",{A.local("sched", t)})', "Link", "d0")
            put(ws, g("sched_oa"), f'=IF({A.local("sched_oa", t)}="","",{A.local("sched_oa", t)})', "Link", "d0")
            put(ws, g("tsrc"), f'=IF({A.local("tsrc", t)}="","",{A.local("tsrc", t)})', "Link")
            put(ws, g("second"), f'=IF({A.local("second", t)}="","",{A.local("second", t)})', "Link", "d0")
            put(ws, g("second_s"), f'=IF({A.local("second_s", t)}="","",{A.local("second_s", t)})', "Link")
            put(ws, g("norr_why"), f'=IF({A.local("norr_why", t)}="","",{A.local("norr_why", t)})', "Link")
            put(ws, g("zr"), f'=IF({A.local("zr", t)}="","",{A.local("zr", t)})', "Link", "d0")
            put(ws, g("za"), f'=IF({A.local("za", t)}="","",{A.local("za", t)})', "Link", "d1")
            put(ws, g("oth"), f'=IF({A.local("oth", t)}="","",{A.local("oth", t)})', "Link", "d0")
            put(ws, g("othc"), f'=IF({A.local("othc", t)}="","",{A.local("othc", t)})', "Link")
            put(ws, g("oths"), f'=IF({A.local("oths", t)}="","",{A.local("oths", t)})', "Link")
        if not after:
            # the room list of the unit is complete when the rooms counted here reach ZoneCoverMin of the zoned area (or, where the zone has no
            # measured area, when every zoned room is counted). Until then no reduction is stated.
            put(ws, g("cov"), coverage_formula(g("zr"), g("za"), g("nok"), g("area_ok"), "ZoneCoverMin", g("nocrit"), g("oth"), zmax="ZoneCoverMax",
                                                    sl=g("second"), n=g("n"), norr=g("norr_why")), "Calc").alignment = Alignment(horizontal="left")
        else:
            put(ws, g("cov"), f'={ctx.lay["AH"].local("cov", t)}', "Link").alignment = Alignment(horizontal="left")
        oa_above = f'AND(ISNUMBER({g("sched_oa")}),{g("oa_rule")}>{g("sched_oa")})'
        put(ws, g("red"), f'=IF(OR({g("sched")}="",{g("n")}=0,{g("sa_ahu")}=0,{incomplete(g("cov"))},{oa_above},{g("sa_ahu")}>={g("sched")}),"",{g("sched")}-{g("sa_ahu")})', "Calc", "d0")
        put(ws, g("redp"), f'=IF(OR({g("red")}="",{g("sched")}=0),"",{g("red")}/{g("sched")})', "Calc", "pct1")
        put(ws, g("leadtxt"), "=" + height_lead(ctx, ahu=g("ahu"), n=g("n"), noh=g("noh")), "Calc")
        shared = (f'IF(IF(ISNUMBER({g("oth")}),{g("oth")},0)>0,"This unit is shared with other departments ("&{g("oth")}&IF({g("oth")}=1," room"," rooms")&"). '
                  f'Its result against tender is on the AHU master summary.","")')
        xx = unit_extra(g("balnote"), a.get("note"))
        inner = tender_remark(g("sched"), g("sa_ahu"), g("red"), g("redp"), f'IF({g("tsrc")}="","tender airflow",{g("tsrc")})',
                              zero="No room of this unit has a supply airflow.",
                              lead=g("leadtxt"), partial=g("cov"), note=g("tsrc"),
                              all_spec=f'AND({g("n")}>0,{g("nfix")}={g("n")})', oa_above=oa_above, oa_calc=g("oa_rule"), oa_tender=g("sched_oa"),
                              extra=f'({shared}&IF(AND({shared}<>"",{xx}<>"")," ","")&{xx})', small=small_zone(g("zr"), g("za")))[1:]
        put(ws, g("rem"), f'=IF({g("n")}=0,"No room on Room Information is served by this unit: no calculation.",{inner})', "Remark")


def unit_extra(spnote: str, note: str = None) -> str:
    """Excel text expression for the text that follows the result of a unit: the single-pass note of the cell and a fixed note that the documents print for the unit."""
    if not note:
        return spnote
    from .sheets_rooms import xl_str
    return f'IF({spnote}="","",TRIM({spnote})&" ")&{xl_str(note[:1].upper() + note[1:])}'


def small_zone(zr: str, za: str) -> tuple:
    """Interim state: a stated reduction for a unit that the zone schedule gives 2 rooms or fewer is not a confirmed result. The text says so."""
    return (f'AND(ISNUMBER({zr}),{zr}>0,{zr}<=2)',
            f'"Interim, to confirm: the zone schedule gives this unit "&{zr}&IF({zr}=1," room"," rooms")&" ("&TEXT({za},"#,##0")&" m²). The reduction may come from an incomplete room list. It is not a confirmed result."')


def coverage_formula(zr: str, za: str, nok: str, area_ok: str, z: str, nc: str = None, oth: str = None, zmax: str = None,
                     sl: str = None, n: str = None, norr: str = None) -> str:
    """Excel text: the completeness of the room list of a unit (D-027, D-054). It starts with 'Complete' when every zoned room of the unit is counted and with
    'Not complete' otherwise, and it gives the reason. A unit is complete when
    1. the zone schedule gives the unit at least one room (zr),
    2. the rooms counted with a criteria row and a supply airflow (nok) are at least as many as the zoned rooms,
    3. no counted room lacks a criteria row (nc),
    4. the zone schedule gives the unit an area, and the area of the counted rooms is between the share Z and the share ZMAX of it,
    5. no other department has a room on the unit (oth): a unit that several departments share is judged on the AHU master summary, which adds up all of them,
    6. a second room list agrees with the count (sl: the room table of the air-side schematic, the MOT NSC schedule or Appendix D), and
    7. the tender base of the unit passes the schedule test (norr is empty).
    A reduction against tender is stated only for a complete unit."""
    nct = f'IF({nc}>0,"; "&{nc}&IF({nc}=1," room has"," rooms have")&" no criteria row","")' if nc else '""'
    zt = f'IF(AND(ISNUMBER({za}),{za}>0),"","; the zone schedule gives the unit no area")'
    at = (f'IF(AND(ISNUMBER({za}),{za}>0,{area_ok}<{z}*{za}),"; the rooms counted cover "&TEXT({area_ok}/{za},"0 %")&" of the zoned area of "&TEXT({za},"#,##0.0")&" m², '
          f'less than the "&TEXT({z},"0 %")&" needed","")')
    if zmax:
        at += (f'&IF(AND(ISNUMBER({za}),{za}>0,{area_ok}>{zmax}*{za}),"; the rooms counted cover "&TEXT({area_ok}/{za},"0 %")&" of the zoned area of "&TEXT({za},"#,##0.0")&" m², '
               f'more than the "&TEXT({zmax},"0 %")&" allowed","")')
    ot = f'IF(IF(ISNUMBER({oth}),{oth},0)>0,"; "&{oth}&IF({oth}=1," room of another department is"," rooms of other departments are")&" on this unit: the result of the unit is on the AHU master summary","")' if oth else '""'
    ok = f'AND({nok}>={zr},{nc}=0,ISNUMBER({za}),{za}>0,{area_ok}>={z}*{za}' if nc else f'AND({nok}>={zr},ISNUMBER({za}),{za}>0,{area_ok}>={z}*{za}'
    if zmax:
        ok += f',{area_ok}<={zmax}*{za}'
    ok += (f',IF(ISNUMBER({oth}),{oth},0)=0' if oth else '')
    st = ""
    if sl and n:
        ok += f',ISNUMBER({sl}),{n}={sl}'
        st = (f'IF(ISNUMBER({sl}),IF({n}={sl},"","; the second room list gives "&{sl}&" rooms against "&{n}&" counted"),'
              f'"; no second room list (room table of the air-side schematic, MOT NSC schedule or Appendix D) is available for this unit")')
    nr = ""
    if norr:
        ok += f',{norr}=""'
        nr = f'IF({norr}="","","; "&{norr})'
    ok += ")"
    cnt = (f'IF({nok}>={zr},IF({zr}=1,"the zoned room is counted","all "&{zr}&" zoned rooms are counted")&IF({nok}>{zr}," ("&{nok}&" rooms counted)",""),'
           f'{nok}&" of "&{zr}&" zoned rooms counted")')
    tail = "&".join(x for x in (nct, zt, at, ot, st, nr) if x)
    return (f'=IF(OR({zr}="",{zr}=0),"Not complete: the unit is not in the zone schedule",'
            f'IF({ok},"Complete: ","Not complete: ")&{cnt}&IF({ok},"",{tail}))')


def height_lead(ctx: Ctx, *, ahu: str = None, room: str = None, fan_tag: str = None, n: str = None, noh: str = None) -> str:
    """Excel text expression naming the causes that the cells prove for a reduction: a missing ceiling height (volume, and with it the
    air-change airflow, is 0) and a missing criteria row (air-change and outdoor airflows are 0). Empty text when neither applies."""
    RI, CR = ctx.lay["RI"], ctx.lay["CR"]
    if room is not None:
        miss = f'COUNTIFS({RI.rng("no")},{room},{RI.rng("h")},"")'
        nocrit = f'COUNTIFS({CR.rng("no")},{room},{CR.rng("crit")},"")'
        h = f'IF({miss}>0,"Main cause: ceiling height is not set for this room, so its air-change airflow is 0 L/s.","")'
        c = f'IF({nocrit}>0,IF({miss}>0," ","")&"No criteria row is set for this room, so its air-change and outdoor airflows are 0 L/s.","")'
        return f"({h}&{c})"
    if n is not None:
        miss, every = noh, n
    elif fan_tag is not None:
        miss = f'(COUNTIFS({RI.rng("ahu")},{ahu},{RI.rng("h")},"")+COUNTIFS({RI.rng("exfan")},{fan_tag},{RI.rng("h")},""))'
        every = f'(COUNTIFS({RI.rng("ahu")},{ahu})+COUNTIFS({RI.rng("exfan")},{fan_tag}))'
    else:
        miss = f'COUNTIFS({RI.rng("ahu")},{ahu},{RI.rng("h")},"")'
        every = f'COUNTIFS({RI.rng("ahu")},{ahu})'
    h = (f'IF({miss}>0,"Main cause: ceiling height is not set for "&{miss}&" of "&{every}&" rooms that this unit serves, '
         f'so their air-change airflow is 0 L/s.","")')
    if ahu is None:
        return h
    nocrit = nocrit_count(ctx, ahu)
    c = (f'IF({nocrit}>0,IF({miss}>0," ","")&"No criteria row is set for "&{nocrit}&" of "&{every}&" rooms that this unit serves, '
         f'so their air-change and outdoor airflows are 0 L/s.","")')
    return f"({h}&{c})"


def nocrit_count(ctx: Ctx, ahu: str) -> str:
    """Excel expression: rooms on the AHU that have no criteria row."""
    RI, CR = ctx.lay["RI"], ctx.lay["CR"]
    return f'SUMPRODUCT(--({RI.rng("ahu")}={ahu}),--({CR.rng("crit")}=""))'


def write_ahu(ctx: Ctx) -> None:
    ws = ctx.wb["AHU"]
    L, L2 = ctx.lay["AH"], ctx.lay["AH2"]
    title_block(ws, "AHU", "Airflow totalisation and coil duty per AHU. Supply = room supply + duct leakage (named cell). Outdoor air "
                "= minimum of the rule (D-110, D-111): straight sum of the room outdoor air of the HFDS / ASHRAE 170 rooms plus the sum of the SS 553 "
                "Table 1 rate × area of the other rooms. The fraction of the highest-standard room × AHU supply is shown as a comparison. The plain sum and the multiple-space "
                "value of the example are shown beside it. Rooms and Area count every room on the unit. Table B repeats the AHU without "
                "the rooms the outdoor-air rule separates. Outdoor air is the larger of the fraction basis and the balance basis (the exhaust with no return, "
                "the net offset air that leaves through the envelope and the spill air). Only clean or sterile single-pass rooms (100 % outdoor air) set the fraction. "
                "A dirty single-pass room takes recirculated supply and a dedicated exhaust, and is listed in the Remarks. The column 'Outdoor air, mass-balance basis' "
                "is information only. Spill or relief = outdoor air − exhaust − net envelope leakage.", ctx)
    heads = [H("ahu", "AHU or system", "", 24), H("type", "Type", "", 9), H("n", "Rooms on this unit", "no.", 8), H("area", "Area of these rooms", "m" + U, 9),
             H("area_sup", "Area of rooms with supply", "m" + U, 9), H("vol", "Volume of rooms with supply", "m³", 9),
             H("noh", "Rooms with no ceiling height", "no.", 9), H("sa_room", "Room supply", "L/s", 10), H("leak", "Duct leakage", "%", 8),
             H("sa_ahu", "AHU supply airflow", "L/s", 10), H("oa_sum", "Sum of room outdoor air", "L/s", 10),
             H("crit", "Highest-standard room (sets the fraction)", "", 46), H("frac", "Highest room outdoor-air fraction", "–", 9),
             H("oa_rule", "AHU outdoor air = larger of the minimum of the rule (D-110) and the air that leaves the rooms", "L/s", 13), H("vou", "Vou (sum of Voc)", "L/s", 9),
             H("vps", "Vps (sum of Vsc)", "L/s", 9), H("xs", "Xs = Vou / Vps", "–", 8), H("zd", "Zd, highest Voc / Vsc", "–", 8),
             H("ev", "Ev = 1 + Xs − Zd", "–", 8), H("vot", "Vot = Vou / Ev", "L/s", 9),
             H("oa_ms", "Outdoor air as the example: max (sum, Vot)", "L/s", 11),
             H("oa_mb", "Outdoor air, mass-balance basis (information only)", "L/s", 12), H("ret", "Recirculated air = AHU supply − outdoor air", "L/s", 11),
             H("exh", "Exhaust", "L/s", 9), H("relief", "Spill or relief = outdoor air − exhaust − net envelope leakage", "L/s", 13), H("sens", "Room sensible", "kW", 9),
             H("lat", "Room latent", "kW", 9), H("rt", "Return air temperature (supply-weighted mean of the rooms)", "°C", 10), H("rh", "Return air enthalpy (supply-weighted mean of the rooms)", "kJ/kg", 10),
             H("oa_sens", "Outdoor air sensible", "kW", 9), H("oa_lat", "Outdoor air latent", "kW", 9), H("fan", "Fan heat", "kW", 8),
             H("coil_s", "Coil sensible", "kW", 9), H("coil_l", "Coil latent", "kW", 9), H("coil", "Coil duty (outdoor-air rule)", "kW", 10),
             H("wm2", "Coil duty per area", "W/m" + U, 9),
             H("oa_sens_ms", "Outdoor air sensible, example basis", "kW", 10), H("oa_lat_ms", "Outdoor air latent, example basis", "kW", 10),
             H("coil_s_ms", "Coil sensible, example basis", "kW", 10), H("coil_l_ms", "Coil latent, example basis", "kW", 10),
             H("coil_ms", "Coil duty, example basis", "kW", 10),
             H("req_t", "Supply temperature required by the sensible load", "°C", 10), H("sched", "Tender airflow (supply)", "L/s", 10),
             H("sched_oa", "Tender outdoor airflow", "L/s", 10), H("sched_c", "Code", "", 5), H("sched_s", "Source ID", "", 11),
             H("tsrc", "Tender source", "", 30),
             H("zr", "Rooms of the unit in the zone schedule", "no.", 10), H("za", "Area of the unit in the zone schedule", "m" + U, 10),
             H("zc", "Code", "", 5), H("zs", "Source ID", "", 11), H("oth", "Rooms of other departments on this unit", "no.", 12), H("othc", "Code", "", 5), H("oths", "Source ID", "", 11), H("cov", "Room list of the unit (rooms counted)", "", 40),
             H("red", "Reduction = tender − calculated supply", "L/s", 11),
             H("redp", "Reduction = (tender − calculated) ÷ tender", "%", 11), H("rem", "Remarks", "", 140),
             H("nfix", "Rooms specified by the MOT NSC", "no.", 10), H("area_ok", "Area of the rooms with a criteria row and a supply airflow", "m" + U, 12),
             H("leadtxt", "Causes proved by the cells (helper)", "", 12), H("nocrit", "Rooms with no criteria row", "no.", 10), H("area_nocrit", "Area of the rooms with no criteria row", "m" + U, 12),
             H("fsp", "Highest fraction of the rooms that are not single pass", "–", 12), H("spr", "Single-pass room that sets the fraction", "", 24),
             H("spl", "Supply of that room", "L/s", 10), H("spy", "Highest fraction of the other rooms", "–", 12), H("spnote", "Single-pass note (helper)", "", 20),
             H("oaflg", "Order of the units with outdoor air above tender (helper)", "no.", 12),
             H("nok", "Rooms with a criteria row and a supply airflow", "no.", 12),
             H("ducted", "Fan coil unit ducted (Y or N)", "", 9), H("oa_frac", "Outdoor air, fraction basis = highest fraction × AHU supply (critical-zone method; comparison only, D-110 rule C6)", "L/s", 12),
             H("oa_bal", "Outdoor air, balance basis = exhaust + net offset air + spill air", "L/s", 12),
             H("offnet", "Net envelope leakage out of the rooms (offset air)", "L/s", 12), H("oa_short", "Make-up air above the AHU supply", "L/s", 11),
             H("dn", "Dirty single-pass rooms", "no.", 9), H("dsup", "Supply of dirty single-pass rooms", "L/s", 10), H("dexh", "Exhaust of dirty single-pass rooms", "L/s", 10),
             H("balnote", "Balance note (helper)", "", 20), H("second", "Rooms of the second room list", "no.", 10), H("second_s", "Source ID", "", 11),
             H("norr_why", "Reason for no reduction against tender (tender base)", "", 30),
             H("dirsup", "Supply of rooms with a pressure direction and no value", "L/s", 12), H("dirn", "Rooms with a pressure direction and no value", "no.", 12),
             H("spillreq", "Spill air to plant rooms served by this unit", "L/s", 11),
             H("ucvadd", "Extra supply for the future ultra-clean duty", "L/s", 11),
             H("oa_clin", "Outdoor air of the HFDS / ASHRAE 170 rooms, straight sum (D-110 rule C4)", "L/s", 13),
             H("oa_nc", "Outdoor air of the SS 553 rooms, sum of Table 1 rate × area (D-111 rules N1 to N3)", "L/s", 13),
             H("ahucls", "Unit class (HFDS/170, SS 553 or MIXED)", "", 11),
             H("oa_min", "Minimum outdoor air of the rule (D-110)", "L/s", 12),
             H("oa_how", "Basis of the minimum outdoor air (D-110)", "", 44)]
    bands = {"ahu": "A. AS ZONED: AIRFLOW", "frac": "OUTDOOR AIR", "vou": "MULTIPLE SPACE (example)", "ret": "RETURN, EXHAUST, RELIEF",
             "sens": "COIL DUTY", "oa_sens_ms": "COIL DUTY ON THE EXAMPLE'S OUTDOOR-AIR BASIS", "req_t": "CHECKS", "sched": "TENDER AIRFLOW AND REDUCTION",
             "zr": "ROOM LIST OF THE UNIT", "oa_clin": "MINIMUM OUTDOOR AIR (D-110)"}
    write_headers(ws, L, heads, 4, bands)
    _ahu_table(ctx, ws, L, False)
    # second table
    b = L2.first - 3
    for k, h, u, w in heads:
        _hl(ws, f"{L2.col(k)}{b + 1}", h, "Header")
        _hl(ws, f"{L2.col(k)}{b + 2}", u or None, "Unit")
    ws.row_dimensions[b + 1].height = 42
    put(ws, f"A{b}", "B. AFTER THE OUTDOOR-AIR RULE: ROOMS SEPARATED ONTO FCUS REMOVED", "BandLeft")
    for ix in range(2, 12):
        put(ws, f"{get_column_letter(ix)}{b}", None, "BandLeft")
    _ahu_table(ctx, ws, L2, True)
    # totals rows
    for LL in (L, L2):
        if not ctx.ahus:
            continue
        tr = LL.last + 1
        put(ws, f"A{tr}", "TOTAL", "ResultTag")
        for k in ("n", "noh", "area", "area_sup", "vol", "sa_room", "sa_ahu", "oa_sum", "oa_rule", "oa_ms", "ret", "exh", "relief", "sens", "lat",
                  "oa_sens", "oa_lat", "fan", "coil_s", "coil_l", "coil", "coil_ms", "coil_s_ms", "coil_l_ms",
                  "oa_sens_ms", "oa_lat_ms", "oa_mb", "oa_frac", "oa_bal", "oa_clin", "oa_nc", "oa_min", "offnet", "oa_short", "dsup", "dexh", "dn", "dirsup", "dirn", "spillreq", "ucvadd"):
            put(ws, f"{LL.col(k)}{tr}", f"=SUM({LL.col(k)}{LL.first}:{LL.col(k)}{LL.last})", "Total",
                "d0" if k in ("n", "noh", "dn", "dirn") else ("d1" if k in ("area", "area_sup", "vol", "sa_room", "sa_ahu", "oa_sum", "oa_rule", "oa_ms", "oa_mb", "ret", "exh", "relief") else "d2"))
    ctx.ahu_total_row = L.last + 1
    ctx.ahu2_total_row = L2.last + 1
    if ctx.ahus:
        cf_formula(ws, f"{L.col('req_t')}{L.first}:{L.col('req_t')}{L.last}",
                   f"AND({L.col('req_t')}{L.first}<SupplyTemp,{L.col('sa_room')}{L.first}>0)")
        cf_formula(ws, f"{L.col('sched_oa')}{L.first}:{L.col('sched_oa')}{L.last}",
                   f'AND(ISNUMBER({L.col("sched_oa")}{L.first}),{L.col("oa_rule")}{L.first}>{L.col("sched_oa")}{L.first})', bold=False, fill="FFFFF2CC")
        cf_formula(ws, f"{L.col('sched')}{L.first}:{L.col('sched')}{L.last}",
                   f'AND({L.col("sched")}{L.first}<>"",{L.col("sched")}{L.first}<{L.col("sa_ahu")}{L.first})', bold=False, fill="FFFFF2CC")
    list_validation(ws, f"{L.col('type')}{L.first}:{L.col('type')}{L.last}", "ListSystemType")
    band_rows(ws, f"A{L.first}:{L.col('rem')}{L.last}")
    band_rows(ws, f"A{L2.first}:{L2.col('rem')}{L2.last}")
    if ctx.ahus:
        cf_formula(ws, f"{L.col('red')}{L.first}:{L.col('red')}{L.last}", f'AND({L.col("red")}{L.first}<>"",{L.col("red")}{L.first}<0)', bold=False, fill="FFFFF2CC")
        cf_formula(ws, f"{L2.col('red')}{L2.first}:{L2.col('red')}{L2.last}", f'AND({L2.col("red")}{L2.first}<>"",{L2.col("red")}{L2.first}<0)', bold=False, fill="FFFFF2CC")
    group_cols(ws, L, "vou", "vot")
    group_cols(ws, L, "oa_sens_ms", "coil_ms")
    group_cols(ws, L, "nfix", "ucvadd", collapsed=True)
    _setup(ws, ctx, L, L.idx["rem"], ctx.ahu2_total_row, "4:6", f"{L.col('crit')}{L.first}", "FF00B050")
    ws.freeze_panes = f"B{L.first}"


# --------------------------------------------------------------------------- #
def write_fcu(ctx: Ctx) -> None:
    ws = ctx.wb["FCU"]
    L = ctx.lay["FC"]
    AF, FN = ctx.lay["AF"], ctx.lay["FN"]
    title_block(ws, "FCU", "Rooms on fan coil units: rooms whose system type is FCU, and rooms separated by the outdoor-air rule. "
                "The coil covers the room load plus outdoor air as it arrives. That air is untreated from an outdoor-air fan, "
                "or treated from a PAHU at its off-coil condition. The outdoor-air source is the room's own input. Otherwise it is the PAHU "
                "(or outdoor-air fan) that serves the room's AHU on Fans. The capacity columns test that unit with the added airflow. "
                "Blank rows are unused list positions.", ctx)
    heads = [H("k", "No.", "", 5), H("no", "Room number", "", 14), H("name", "Room name", "", 26), H("ahu", "Original AHU or system", "", 24),
             H("why", "Reason", "", 12), H("area", "Area", "m" + U, 8), H("sens_kw", "Room sensible", "kW", 9),
             H("lat_kw", "Room latent", "kW", 9), H("supply", "Room supply", "L/s", 9), H("oa", "Outdoor air", "L/s", 9),
             H("oasrc", "Outdoor-air source (PAHU or fan)", "", 22), H("oaby", "Source chosen by", "", 14),
             H("oacond", "Outdoor air arrives", "", 11),
             H("oa_t", "Arrival temperature", "°C", 9), H("oa_h", "Arrival enthalpy", "kJ/kg", 9),
             H("oa_sens", "Outdoor air sensible", "kW", 9), H("oa_lat", "Outdoor air latent", "kW", 9),
             H("coil_s", "Coil sensible", "kW", 9), H("coil_l", "Coil latent", "kW", 9), H("coil", "FCU coil duty", "kW", 10),
             H("cap_sched", "Source unit, scheduled", "L/s", 10), H("cap_drawn", "Source unit, drawn with the added air", "L/s", 11),
             H("cap", "Source capacity", "", 12), H("flag", "Flag", "", 44),
             H("tender", "Tender airflow of the unit", "L/s", 10), H("tsrc", "Tender source", "", 28),
             H("calc", "Calculated airflow of the unit", "L/s", 10), H("red", "Reduction = tender − calculated", "L/s", 10),
             H("redp", "Reduction = (tender − calculated) ÷ tender", "%", 11), H("rem", "Remarks", "", 140)]
    write_headers(ws, L, heads, 4, {"k": "ROOM", "area": "LOAD", "oasrc": "OUTDOOR AIR", "oa_sens": "COIL", "cap_sched": "CAPACITY OF THE SOURCE",
                                    "flag": "FLAG", "tender": "TENDER AIRFLOW AND REDUCTION"})
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
        put(ws, g("coil"), f'=IF({g("no")}="","",{g("coil_s")}+{g("coil_l")})', "Calc", "d2")
        if ctx.fans:
            fm = f"MATCH({g('oasrc')},{FN.rng('tag')},0)"
            put(ws, g("cap_sched"), f'=IF({g("no")}="","",IF(ISNUMBER({fm}),INDEX({FN.rng("sched")},{fm}),""))', "Link", "d0")
            put(ws, g("cap_drawn"), f'=IF({g("no")}="","",IF(ISNUMBER({fm}),INDEX({FN.rng("drawn")},{fm}),""))', "Link", "d1")
            put(ws, g("cap"), f'=IF({g("no")}="","",IF(ISNUMBER({fm}),INDEX({FN.rng("status")},{fm}),"NOT SET"))', "Check")
        else:
            put(ws, g("cap_sched"), None, "Link", "d0")
            put(ws, g("cap_drawn"), None, "Link", "d1")
            put(ws, g("cap"), f'=IF({g("no")}="","","NOT SET")', "Check")
        _hl(ws, g("flag"), (f'=IF({g("no")}="","",IF({g("oasrc")}="not set","OUTDOOR-AIR SOURCE NOT SET; COIL ON UNTREATED AIR UNTIL THE SOURCE IS GIVEN",'
                            f'IF(AND({g("oacond")}="TREATED",PAHUOffT=""),"PAHU OFF-COIL CONDITION NOT SET",'
                            f'IF({g("cap")}="VERIFY","SOURCE UNIT MAY BE TOO SMALL WITH THE ADDED AIR",IF({g("oaby")}="FANS DEFAULT","SOURCE TAKEN FROM FANS: CONFIRM","")))))'),
            "Calc", left=True)
        AH = ctx.lay["AH"]
        mt = f"MATCH({g('ahu')},{AH.rng('ahu')},0)"
        sysrow = f'{g("why")}="SYSTEM TYPE"'
        put(ws, g("tender"), (f'=IF({g("no")}="","",IF({sysrow},IF(ISNUMBER({mt}),IF(INDEX({AH.rng("sched")},{mt})>0,'
                              f'INDEX({AH.rng("sched")},{mt}),""),""),""))'), "Link", "d0")
        _hl(ws, g("tsrc"), (f'=IF({g("tender")}="","",INDEX({AH.rng("tsrc")},{mt}))'), "Link", left=True)
        put(ws, g("calc"), (f'=IF({g("no")}="","",IF({sysrow},IF(ISNUMBER({mt}),INDEX({AH.rng("sa_ahu")},{mt}),{g("supply")}),'
                            f'{g("supply")}*(1+FcuLeakageDucted)))'), "Link", "d0")
        part = f'IF(ISNUMBER({mt}),INDEX({AH.rng("cov")},{mt}),"Not complete: the unit is not on the AHU tab")'
        put(ws, g("red"), f'=IF(OR({g("tender")}="",{g("calc")}=0,{incomplete(part)},{g("calc")}>={g("tender")}),"",{g("tender")}-{g("calc")})', "Calc", "d0")
        put(ws, g("redp"), f'=IF(OR({g("tender")}="",{g("red")}=""),"",IF({g("tender")}=0,"",{g("red")}/{g("tender")}))', "Calc", "pct1")
        put(ws, g("rem"), tender_remark(g("tender"), g("calc"), g("red"), g("redp"), f'IF({g("tsrc")}="","tender airflow",{g("tsrc")})',
                                        "airflow", "No room served by this unit needs it under its criteria.",
                                        lead=height_lead(ctx, room=g("no")), partial=part, note=g("tsrc")), "Remark")
    if ctx.n_fcu_rows:
        band_rows(ws, f"A{L.first}:{L.col('rem')}{L.last}")
        cf_formula(ws, f"{L.col('red')}{L.first}:{L.col('red')}{L.last}", f'AND({L.col("red")}{L.first}<>"",{L.col("red")}{L.first}<0)', bold=False, fill="FFFFF2CC")
        cf_formula(ws, f"{L.col('flag')}{L.first}:{L.col('flag')}{L.last}", f'{L.col("flag")}{L.first}<>""')
        cf_text(ws, f"{L.col('cap')}{L.first}:{L.col('cap')}{L.last}", f"{L.col('cap')}{L.first}", "VERIFY", fill="FFFFF2CC")
    put(ws, f"A{L.last + 2}", "Source capacity: the PAHU or outdoor-air fan on Fans carries the outdoor air of the separated room on top of its AHU's. "
        "Its status there is the test. The FCU itself is a new unit. Select it after the zoning change is approved. "
        "Unit values repeat on each room of the same unit. Do not add them.", "Note")
    _setup(ws, ctx, L, L.idx["rem"], L.last + 2, "4:6", f"{L.col('name')}{L.first}", "FF00B050")


# --------------------------------------------------------------------------- #
def write_fans(ctx: Ctx) -> None:
    ws = ctx.wb["Fans"]
    L, AF, AH = ctx.lay["FN"], ctx.lay["AF"], ctx.lay["AH"]
    AH2, FC = ctx.lay["AH2"], ctx.lay["FC"]
    title_block(ws, "Fans", "Return, exhaust and isolation-exhaust fans, outdoor-air fans and PAHUs: required airflow from the balance, "
                "duct leakage and drawn duty against the tender airflow. Airflow only: fan pressure is not calculated here.", ctx)
    heads = [H("tag", "Fan tag", "", 24), H("type", "Type", "", 16), H("serves", "Serves (AHU tag, or fan tag for exhaust)", "", 26), H("serves2", "Also serves (second AHU)", "", 22),
             H("req", "Required airflow", "L/s", 10), H("leak", "Duct leakage", "%", 8), H("drawn", "Calculated airflow, drawn duty", "L/s", 11),
             H("sched", "Tender airflow", "L/s", 10), H("tsrc", "Tender source", "", 28),
             H("red", "Reduction = tender − calculated", "L/s", 11), H("redp", "Reduction = (tender − calculated) ÷ tender", "%", 11),
             H("code", "Code", "", 5), H("src", "Source ID", "", 11), H("remark", "Remark", "", 30), H("status", "Status", "", 11),
             H("rem", "Remarks", "", 140),
             H("pk_p", "First PAHU of the AHU (FCU source)", "", 16), H("pk_o", "First outdoor-air fan of the AHU (FCU source)", "", 16)]
    write_headers(ws, L, heads, 4, {"tag": "FAN", "req": "AIRFLOW", "sched": "TENDER AIRFLOW AND REDUCTION", "pk_p": "FCU OUTDOOR-AIR SOURCE"})
    for t, f in enumerate(ctx.fans):
        g = lambda k: L.local(k, t)
        code, src = f.get("code", "D"), f.get("source", "")
        if blank(src):
            raise ValueError(f"fan {f['tag']} has no source id")
        for k, fld, kind, fmt in (("tag", "tag", "tag", None), ("type", "type", "text", None), ("serves", "serves", "tag", None),
                                  ("serves2", "serves2", "tag", None), ("sched", "scheduled_lps", "num", "d0")):
            _val(ctx, ws, g(k), f.get(fld), code, fmt=fmt, kind=kind, cref=g("code"), sref=g("src"), src=src, label=f"fan {fld}",
                 room=f["tag"])
        _prov_cells(ctx, ws, g("code"), g("src"), code, src, True)
        # outdoor air of the AHU after the outdoor-air rule, plus the outdoor air of FCU rooms that draw on this unit
        def oa_of(tagref: str) -> str:
            m = f"MATCH({tagref},{AH2.rng('ahu')},0)"
            return f"IF(ISNUMBER({m}),INDEX({AH2.rng('oa_rule')},{m}),0)"
        oa_ahu = (f"{oa_of(g('serves'))}+IF({g('serves2')}<>\"\",{oa_of(g('serves2'))},0)"
                  f"+SUMIFS({FC.rng('oa')},{FC.rng('oasrc')},{g('tag')})")
        ret_of = lambda tagref: f"SUMIFS({AF.rng('ret')},{AF.rng('ahu')},{tagref})"
        put(ws, g("req"), (f'=IF({g("type")}="RETURN",{ret_of(g("serves"))}+IF({g("serves2")}<>"",{ret_of(g("serves2"))},0),'
                           f'IF(OR({g("type")}="OUTDOOR AIR",{g("type")}="PAHU"),{oa_ahu},'
                           f'IF(OR({g("type")}="EXHAUST",{g("type")}="ISOLATION EXHAUST"),SUMIFS({AF.rng("exhaust")},{AF.rng("exfan")},{g("tag")}),0)))'),
            "Calc", "d1")
        put(ws, g("leak"), "=DuctLeakage", "Link", "pct1")
        put(ws, g("drawn"), f"={g('req')}*(1+{g('leak')})", "Calc", "d1")
        put(ws, g("tsrc"), f.get("tender_source_text") if not blank(f.get("scheduled_lps")) else None, "Text")
        fm_ = f"MATCH({g('serves')},{AH.rng('ahu')},0)"
        part = (f'IF(ISNUMBER({fm_}),INDEX({AH.rng("cov")},{fm_}),'
                f'IF(OR({g("type")}="EXHAUST",{g("type")}="ISOLATION EXHAUST"),"Not complete: the rooms tied to this fan are not checked against the zone schedule","Not complete: the unit is not on the AHU tab"))')
        put(ws, g("red"), f'=IF(OR({g("sched")}="",{g("req")}=0,{incomplete(part)},{g("drawn")}>={g("sched")}),"",{g("sched")}-{g("drawn")})', "Calc", "d0")
        put(ws, g("redp"), f'=IF(OR({g("sched")}="",{g("red")}="",{g("sched")}=0),"",{g("red")}/{g("sched")})', "Calc", "pct1")
        put(ws, g("status"), f'=IF({g("sched")}="","NO TENDER VALUE",IF({g("req")}=0,"NOT CALCULATED",IF({g("sched")}>={g("drawn")},"PASS","VERIFY")))', "Check")
        put(ws, g("rem"), tender_remark(g("sched"), g("drawn"), g("red"), g("redp"), f'IF({g("tsrc")}="","tender airflow",{g("tsrc")})',
                                        "airflow", "No room of this workbook is tied to this fan, or no room tied to it needs the airflow. It is not calculated.",
                                        lead=height_lead(ctx, ahu=g("serves"), fan_tag=g("tag")), partial=part, note=g("tsrc")), "Remark")
        put(ws, g("remark"), f.get("remark"), "Note")
        put(ws, g("pk_p"), f'=IF(AND({g("type")}="PAHU",COUNTIFS({L.upto("type", t)},"PAHU",{L.upto("serves", t)},{g("serves")})=1),{g("serves")},"")', "CalcTag")
        put(ws, g("pk_o"), f'=IF(AND({g("type")}="OUTDOOR AIR",COUNTIFS({L.upto("type", t)},"OUTDOOR AIR",{L.upto("serves", t)},{g("serves")})=1),{g("serves")},"")', "CalcTag")
    if ctx.fans:
        list_validation(ws, f"{L.col('type')}{L.first}:{L.col('type')}{L.last}", "ListFanType")
        band_rows(ws, f"A{L.first}:{L.col('status')}{L.last}")
        cf_text(ws, f"{L.col('status')}{L.first}:{L.col('status')}{L.last}", f"{L.col('status')}{L.first}", "VERIFY", fill="FFFFF2CC")
    _setup(ws, ctx, L, L.idx["pk_o"], L.last, "4:6", f"B{L.first}", "FF00B050")


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
        put(ws, g("max"), f"=SUMIFS({AF.rng('supply')},{AF.rng('terminal')},{g('tag')})", "Calc", "d1")
        put(ws, g("min"), f"=SUMIFS({AF.rng('minflow')},{AF.rng('terminal')},{g('tag')})", "Calc", "d1")
        put(ws, g("turn"), f"=IF({g('max')}>0,{g('min')}/{g('max')},0)", "Calc", "pct1")
        put(ws, g("remark"), v.get("remark"), "Note")
    if ctx.terminals:
        list_validation(ws, f"{L.col('type')}{L.first}:{L.col('type')}{L.last}", "ListTerminal")
        band_rows(ws, f"A{L.first}:{L.col('remark')}{L.last}")
    _setup(ws, ctx, L, L.idx["remark"], L.last, "4:6", f"B{L.first}", "FF00B050")


def write_venturi(ctx: Ctx) -> None:
    ws = ctx.wb["Venturi Valves"]
    L, AF, PS = ctx.lay["VN"], ctx.lay["AF"], ctx.lay["PS"]
    title_block(ws, "Venturi Valves", "One row per valve. It gives the design airflow. From Pressure it gives the offset needed. "
                "It gives the design offset as the top of the valve's range. It also gives the offset of a tighter room (1.0 m³/h per m²). "
                "The range covers both ends.", ctx)
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
    _setup(ws, ctx, L, L.idx["remark"], L.last, "4:6", f"B{L.first}", "FF00B050")


# --------------------------------------------------------------------------- #
def write_flow_balance(ctx: Ctx) -> None:
    ws = ctx.wb["Flow Balance"]
    L, L2, AF, PS, AH = ctx.lay["FB"], ctx.lay["FB2"], ctx.lay["AF"], ctx.lay["PS"], ctx.lay["AH"]
    title_block(ws, "Flow Balance", "Air in against air out per room, system and AHU. For rooms on Pressure the leakage paths close the "
                "balance; an exhaust-only room is fed by transfer air and shows TRANSFER AIR. The AHU table shows the balance of each unit: "
                "supply = outdoor air + recirculated air; outdoor air = the larger of the fraction basis and the air that leaves the rooms; "
                "spill or relief = outdoor air − exhaust − net envelope leakage.", ctx)
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
        put(ws, g("net"), f"={g('inn')}-{g('out')}", "Calc", "d1")
        put(ws, g("close"), f"=MAX(BalTolLps,BalTolPct*{g('inn')})", "Calc", "d1")
        # a room with no pressure offset whose exhaust is more than its supply is made up by transfer air (no supply floor from exhaust)
        ta = f'IF(OR({g("supply")}=0,{g("net")}<0),"TRANSFER AIR",' if node is None else f'IF({g("supply")}=0,"TRANSFER AIR",'
        put(ws, g("status"), f'=IF(ABS({g("net")})<={g("close")},"PASS",{ta}IF({AF.addr("fix", i)}=1,"SPECIFIED","FAIL")))', "Check")
        put(ws, g("ach"), f"=IF({AF.addr('vol', i)}>0,ConvM3hPerLps*{g('net')}/{AF.addr('vol', i)},0)", "Calc", "d2")
    band_rows(ws, f"A{L.first}:{L.col('ach')}{L.last}")
    cf_text(ws, f"{L.col('status')}{L.first}:{L.col('status')}{L.last}", f"{L.col('status')}{L.first}", "FAIL")
    # AHU summary
    put(ws, f"A{L2.first - 4}", "By AHU (Rev A 'Flow Balance' definitions)", "Section")
    heads2 = [H("ahu", "AHU or system", "", 24), H("supply", "Room supply", "L/s", 9), H("oa", "AHU outdoor air", "L/s", 9),
              H("exhaust", "Exhaust (no return)", "L/s", 9), H("ret", "Recirculated air", "L/s", 10),
              H("offnet", "Net envelope leakage out (offset air)", "L/s", 11), H("relief", "Spill or relief = outdoor air − exhaust − net leakage", "L/s", 13),
              H("net_ach", "As air changes", "ACH", 8), H("area", "Area", "m" + U, 8), H("vol", "Volume", "m³", 9),
              H("sum_net", "Sum of room nets", "L/s", 9), H("close", "Tolerance", "L/s", 9), H("status", "Closure", "", 14), H("note", "Note", "", 46)]
    for k, h, u, w in heads2:
        _hl(ws, f"{L2.col(k)}{L2.first - 2}", h, "Header")
        _hl(ws, f"{L2.col(k)}{L2.first - 1}", u or None, "Unit")
    ws.row_dimensions[L2.first - 2].height = 42
    for t, a in enumerate(ctx.ahus):
        g = lambda k: L2.local(k, t)
        _hl(ws, g("ahu"), f"={AH.addr('ahu', t)}", "LinkTag", left=True)
        for k, src in (("supply", "sa_room"), ("oa", "oa_rule"), ("exhaust", "exh"), ("ret", "ret"), ("offnet", "offnet"), ("relief", "relief"),
                       ("area", "area"), ("vol", "vol")):
            put(ws, g(k), f"={AH.addr(src, t)}", "Link", "d1")
        put(ws, g("net_ach"), f"=IF({g('vol')}>0,ConvM3hPerLps*{g('relief')}/{g('vol')},0)", "Calc", "d2")
        put(ws, g("sum_net"), f"=SUMIFS({L.rng('net')},{L.rng('ahu')},{g('ahu')})", "Calc", "d1")
        eng = "+".join(f"SUMIFS({ctx.eng_layouts[st].rng('fin')},{ctx.eng_layouts[st].rng('ahu')},{g('ahu')})" for st in ctx.storeys)
        put(ws, g("close"), f"=MAX(BalTolLps,BalTolPct*{g('supply')})", "Calc", "d1")
        put(ws, g("status"), f'=IF(AND(ABS({g("relief")}-{g("oa")}+{g("exhaust")}+{g("offnet")})<={g("close")},{g("ret")}>=0,{g("oa")}<={AH.addr("sa_ahu", t)}+{g("close")},'
                             f'ABS({g("supply")}-({eng}))<={g("close")}),"PASS","FAIL")', "Check")
        put(ws, g("note"), (f'=IF({g("relief")}<-{g("close")},"Negative relief: "&TEXT(-{g("relief")},"0")&" L/s of make-up air is drawn as transfer air from '
                            f'adjacent spaces; the corridor or adjacent system must supply it.",IF({g("relief")}>{g("close")},"Positive relief: "&TEXT({g("relief")},"0")&" L/s to spill or transfer out.",""))'), "Note")
    if ctx.ahus:
        cf_text(ws, f"{L2.col('status')}{L2.first}:{L2.col('status')}{L2.last}", f"{L2.col('status')}{L2.first}", "FAIL")
    _setup(ws, ctx, L, L.idx["ach"], L2.last, "4:6", f"{L.col('name')}{L.first}", "FF00B050")


# --------------------------------------------------------------------------- #
def write_comparison(ctx: Ctx) -> None:
    ws = ctx.wb["Airflow Comparison"]
    L, L2, L3, AF, AH, CR = ctx.lay["CP"], ctx.lay["CP2"], ctx.lay["CP3"], ctx.lay["AF"], ctx.lay["AH"], ctx.lay["CR"]
    title_block(ws, "Airflow Comparison", "Calculated airflows against the tender documents, room by room and AHU by AHU, and the reference rows. "
                "Tender airflows are information only: they are never a target or a limit. A calculated value below tender is a reduction, "
                "not a failure. A calculated value above tender is marked VERIFY.", ctx)
    heads = [H("no", "Room number", "", 14), H("name", "Room name", "", 26), H("ahu", "AHU or system", "", 22),
             H("sch_s", "Tender supply (schematic)", "L/s", 10), H("calc_s", "Calculated supply", "L/s", 10),
             H("d_s", "Reduction = tender − calculated", "L/s", 11),
             H("dp_s", "Reduction = (tender − calculated) ÷ tender", "%", 11),
             H("sch_r", "Return, tender schematic", "L/s", 10), H("calc_r", "Return, calculated", "L/s", 10),
             H("d_r", "Return: tender − calculated", "L/s", 10),
             H("sch_e", "Exhaust, tender schematic", "L/s", 10), H("calc_e", "Exhaust, calculated", "L/s", 10),
             H("d_e", "Exhaust: tender − calculated", "L/s", 10), H("pa_calc", "Pressure target used", "Pa", 8), H("pa_sch", "Pressure on tender schematic", "Pa", 9),
             H("flag", "Against tender", "", 26), H("code", "Code", "", 5), H("src", "Source ID", "", 11),
             H("tsrc", "Tender source", "", 28), H("remark", "Remarks", "", 140)]
    write_headers(ws, L, heads, 4, {"no": "ROOM", "sch_s": "SUPPLY", "sch_r": "RETURN", "sch_e": "EXHAUST", "pa_calc": "PRESSURE", "flag": "RESULT"})
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
        for k, s_ in (("calc_s", "supply"), ("calc_r", "ret"), ("calc_e", "exhaust")):
            put(ws, g(k), f"={AF.addr(s_, i)}", "Link", "d1")
        for k, fld in (("sch_s", "supply_lps"), ("sch_r", "return_lps"), ("sch_e", "exhaust_lps"), ("pa_sch", "pressure_pa")):
            _val(ctx, ws, g(k), sc.get(fld), code, fmt="d1", cref=g("code"), sref=g("src"), src=src, label=f"schematic {fld}", room=r["number"])
        _prov_cells(ctx, ws, g("code"), g("src"), code, src, has)
        put(ws, g("tsrc"), sc.get("source_text") if has else None, "Text")
        crit_ok = f'COUNTIFS({CR.rng("no")},{g("no")},{CR.rng("crit")},"")=0'
        put(ws, g("d_s"), f'=IF({g("sch_s")}="","",IF(AND(NOT({crit_ok}),{g("sch_s")}-{g("calc_s")}>=0),"",{g("sch_s")}-{g("calc_s")}))', "Calc", "d1")
        put(ws, g("dp_s"), f'=IF(OR({g("sch_s")}="",{g("sch_s")}=0,{g("d_s")}=""),"",{g("d_s")}/{g("sch_s")})', "Calc", "pct1")
        put(ws, g("d_r"), f'=IF({g("sch_r")}="","",{g("sch_r")}-{g("calc_r")})', "Calc", "d1")
        put(ws, g("d_e"), f'=IF({g("sch_e")}="","",{g("sch_e")}-{g("calc_e")})', "Calc", "d1")
        put(ws, g("pa_calc"), f"={CR.addr('pa', i)}", "Link", "d1")
        _hl(ws, g("flag"), (f'=IF({g("sch_s")}="","NO TENDER VALUE",IF({g("d_s")}<0,"VERIFY: ABOVE TENDER",IF(NOT({crit_ok}),"NO CRITERIA ROW",IF({g("d_s")}=0,"EQUALS TENDER",'
                            f'"REDUCTION AGAINST TENDER"))))'), "Calc", left=True)
        put(ws, g("remark"), tender_remark(g("sch_s"), g("calc_s"), g("d_s"), g("dp_s"),
                                           f'IF({g("tsrc")}="","tender schematic",{g("tsrc")})', "schematic supply",
                                           lead=height_lead(ctx, room=g("no")), share_note=False, thermal=False,
                                           partial=f'IF({crit_ok},"Complete","Not complete: no criteria row")'), "Remark")
    band_rows(ws, f"A{L.first}:{L.col('remark')}{L.last}")
    cf_formula(ws, f"{L.col('flag')}{L.first}:{L.col('flag')}{L.last}", f'LEFT({L.col("flag")}{L.first},6)="VERIFY"', bold=False, fill="FFFFF2CC")
    _setup(ws, ctx, L, L.idx["remark"], L.last, "4:6", f"{L.col('name')}{L.first}", "FF7F7F7F")
    # AHU table: its own tab, its own header
    ws = ctx.wb["Comparison by AHU"]
    title_block(ws, "Comparison by AHU", "Calculated airflow of each AHU against the tender airflow of the AHU tab and the tender schematic. "
                "Tender airflows are information only. This workbook adds up only its own rooms on a unit: the reduction of the whole unit is on the AHU master summary.", ctx)
    heads2 = [H("ahu", "AHU or system", "", 26), H("sched", "Tender airflow (AHU tab)", "L/s", 11), H("calc", "Calculated airflow, AHU supply", "L/s", 11),
              H("d_sched", "Reduction = tender − calculated", "L/s", 11), H("dp_sched", "Reduction = (tender − calculated) ÷ tender", "%", 11),
              H("tsrc", "Tender source", "", 40),
              H("sch", "Tender schematic AHU airflow", "L/s", 11), H("d", "Reduction = tender schematic − calculated", "L/s", 11),
              H("dp", "Reduction = (tender schematic − calculated) ÷ tender schematic", "%", 12), H("flag", "Against tender", "", 28), H("code", "Code", "", 5),
              H("src", "Source ID", "", 11), H("remark", "Remarks", "", 110)]
    write_headers(ws, L2, heads2, 4, {"ahu": "AHU", "sched": "TENDER, AHU TAB", "sch": "TENDER SCHEMATIC", "flag": "RESULT"})
    for t, a in enumerate(ctx.ahus):
        g = lambda k: L2.local(k, t)
        _hl(ws, g("ahu"), f"={AH.addr('ahu', t)}", "LinkTag", left=True)
        put(ws, g("calc"), f"={AH.addr('sa_ahu', t)}", "Link", "d0")
        sc, ss = a.get("schematic_code", "D"), a.get("schematic_source", "")
        has = not blank(a.get("schematic_supply_lps"))
        if has and blank(ss):
            raise ValueError(f"AHU {a['tag']}: schematic duty has no source id")
        _val(ctx, ws, g("sch"), a.get("schematic_supply_lps"), sc, fmt="d0", cref=g("code"), sref=g("src"), src=ss, label="schematic duty", room=a["tag"])
        _prov_cells(ctx, ws, g("code"), g("src"), sc, ss, has)
        put(ws, g("sched"), f'=IF({AH.addr("sched", t)}="","",{AH.addr("sched", t)})', "Link", "d0")
        put(ws, g("tsrc"), f'=IF({AH.addr("tsrc", t)}="","",{AH.addr("tsrc", t)})', "Link")
        part = AH.addr("cov", t)
        put(ws, g("d_sched"), f'=IF(OR({g("sched")}="",{g("calc")}=0,{incomplete(part)},{g("calc")}>={g("sched")}),"",{g("sched")}-{g("calc")})', "Calc", "d0")
        put(ws, g("dp_sched"), f'=IF(OR({g("d_sched")}="",{g("sched")}=0),"",{g("d_sched")}/{g("sched")})', "Calc", "pct1")
        put(ws, g("d"), f'=IF(OR({g("sch")}="",{incomplete(part)},{g("calc")}>={g("sch")}),"",{g("sch")}-{g("calc")})', "Calc", "d0")
        put(ws, g("dp"), f'=IF(OR({g("d")}="",{g("sch")}=0),"",{g("d")}/{g("sch")})', "Calc", "pct1")
        _hl(ws, g("flag"), (f'=IF({g("sched")}="","NO TENDER VALUE",IF({g("calc")}=0,"NOT CALCULATED",IF({g("calc")}>{g("sched")},"VERIFY: ABOVE TENDER",'
                            f'IF({incomplete(part)},"ROOM LIST NOT COMPLETE",IF({g("calc")}={g("sched")},"EQUALS TENDER","REDUCTION AGAINST TENDER")))))'), "Calc", left=True)
        put(ws, g("remark"), tender_remark(g("sched"), g("calc"), g("d_sched"), g("dp_sched"),
                                           f'IF({g("tsrc")}="","tender airflow",{g("tsrc")})',
                                           zero="Calculated airflow is 0 L/s: no room of this unit has a supply airflow.",
                                           lead=height_lead(ctx, ahu=g("ahu")), partial=part, note=g("tsrc")), "Remark")
    if ctx.ahus:
        cf_formula(ws, f"{L2.col('flag')}{L2.first}:{L2.col('flag')}{L2.last}", f'LEFT({L2.col("flag")}{L2.first},6)="VERIFY"', bold=False, fill="FFFFF2CC")
    band_rows(ws, f"A{L2.first}:{L2.col('remark')}{L2.last}")
    _setup(ws, ctx, L2, L2.idx["remark"], L2.last, "4:6", f"B{L2.first}", "FF7F7F7F")
    # reference rows: own tab, own header
    ws = ctx.wb["Reference Rows"]
    title_block(ws, "Reference Rows", "Airflows quoted by other documents, for information only. They are not adopted. The difference is the reference minus this workbook.", ctx)
    heads3 = [H("label", "Reference", "", 40), H("flow", "Reference airflow", "L/s", 11), H("m3h", "Reference airflow", "m³/h", 11),
              H("ach", "Air changes on this workbook's volume", "ACH", 12),
              H("oa", "Reference outdoor air", "L/s", 11), H("diff", "Difference = reference − this workbook (AHU supply)", "L/s", 14),
              H("pct", "Difference ÷ reference", "%", 11),
              H("code", "Code", "", 5), H("src", "Source ID", "", 11), H("remark", "Remarks", "", 140)]
    write_headers(ws, L3, heads3, 4, {"label": "REFERENCE"})
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
        put(ws, g("diff"), f"={g('flow')}-{tot_sa}", "Calc", "d0")
        put(ws, g("pct"), f"=IF({g('flow')}>0,{g('diff')}/{g('flow')},0)", "Calc", "pct1")
        put(ws, g("remark"), ref.get("remark"), "Remark")
    band_rows(ws, f"A{L3.first}:{L3.col('remark')}{L3.last}")
    _setup(ws, ctx, L3, L3.idx["remark"], L3.last, "4:6", f"B{L3.first}", "FF7F7F7F")
