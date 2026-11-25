"""Psychrometric state at the coil duty, per AHU, on the AHU tab of a department workbook (25 Nov 2026, Marcus).

Adds two blocks under the AHU tab (one for section A, one for section B), all as live formulas on the cells of the tab and the psychrometric
constants of the Basis of Design (PsyA, PsyB, PsyC, PsyP, PsyEps, PsyCpA, PsyCpV, PsyHfg; source GEN-PSY):

  outdoor air and return air  ->  mixed air  ->  coil (sensible, latent and total duty of the tab)  ->  off-coil state  ->  supply fan
  ->  supply air state and its dew point; apparatus dew point and contact factor of the coil line; room humidity that results.

Basis (the same conventions as the coil duty of the tab, so that the states close on that duty):
  dry bulb   by the sensible air constant (AirSensibleFactor) on the AHU supply airflow;
  enthalpy   by the air density (AirDensity) on the AHU supply airflow, coil total duty;
  humidity   ratio from enthalpy and dry bulb, h = cpa t + W (hfg + cpv t); dew point by the Magnus form of the saturation pressure;
  draw-through fan: the fan heat of the tab warms the air after the coil; the humidity ratio does not change after the coil.

``python3 -m src.workbook.psychro_ahu <in.xlsx> <out.xlsx>``  (the input is not changed)
"""
from __future__ import annotations

import re
import sys
from copy import copy
from pathlib import Path
from typing import List

import openpyxl
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter as L

PS = "(PsyA*EXP(PsyB*{t}/(PsyC+{t})))"                     # saturation pressure, kPa
WS = "(PsyEps*" + PS + "/(PsyP-" + PS + "))"               # saturation humidity ratio
W_OF = "(({h})-PsyCpA*({t}))/(PsyHfg+PsyCpV*({t}))"        # humidity ratio from enthalpy and dry bulb
H_OF = "(PsyCpA*({t})+({w})*(PsyHfg+PsyCpV*({t})))"        # enthalpy from dry bulb and humidity ratio
PW = "(({w})*PsyP/(PsyEps+({w})))"                         # vapour pressure from humidity ratio
DEW = "PsyC*LN(" + PW + "/PsyA)/(PsyB-LN(" + PW + "/PsyA))"
HS = "(PsyCpA*{t}+" + WS + "*(PsyHfg+PsyCpV*{t}))"          # enthalpy of saturated air at t

# (key, header, unit, formula with {key} references to the same row and {src} = row of the tab, number format)
SPEC = [
    ("oa_t", "Outdoor air dry bulb", "°C", "OutdoorDB", "t"),
    ("oa_h", "Outdoor air enthalpy", "kJ/kg", "OutdoorH", "t"),
    ("oa_w", "Outdoor air humidity ratio", "kg/kg", W_OF.format(h="{oa_h}", t="{oa_t}"), "w"),
    ("ra_t", "Return air dry bulb (reference room)", "°C", "AB{src}", "t"),
    ("ra_h", "Return air enthalpy (reference room)", "kJ/kg", "AC{src}", "t"),
    ("ra_w", "Return air humidity ratio (room design)", "kg/kg", W_OF.format(h="{ra_h}", t="{ra_t}"), "w"),
    ("q_oa", "Outdoor air", "L/s", "N{src}", "q"),
    ("q_ra", "Recirculated air", "L/s", "W{src}", "q"),
    ("q_sa", "AHU supply airflow", "L/s", "J{src}", "q"),
    ("q_rm", "Room supply airflow", "L/s", "H{src}", "q"),
    ("mx_t", "Mixed air dry bulb", "°C", "({q_oa}*{oa_t}+{q_ra}*{ra_t})/{q_sa}", "t"),
    ("mx_h", "Mixed air enthalpy", "kJ/kg", "({q_oa}*{oa_h}+{q_ra}*{ra_h})/{q_sa}", "t"),
    ("mx_w", "Mixed air humidity ratio", "kg/kg", "({q_oa}*{oa_w}+{q_ra}*{ra_w})/{q_sa}", "w"),
    ("mx_rh", "Mixed air relative humidity", "%", PW.format(w="{mx_w}") + "/" + PS.format(t="{mx_t}"), "p"),
    ("tab_s", "Coil sensible of this tab", "kW", "AG{src}", "k"),
    ("tab_l", "Coil latent of this tab", "kW", "AH{src}", "k"),
    ("tab_q", "Coil duty of this tab (outdoor-air rule)", "kW", "AI{src}", "k"),
    ("fan", "Supply fan temperature rise", "K", "AF{src}*WperkW/(AirSensibleFactor*{q_sa})", "t"),
    ("sa_t", "Supply air dry bulb required by the sensible load", "°C", "AP{src}", "t"),
    ("off_t", "Off-coil dry bulb required (supply less fan rise)", "°C", "{sa_t}-{fan}", "t"),
    ("req_w", "Supply humidity ratio required by the room latent load", "kg/kg",
     "{ra_w}-AA{src}/(AirDensity*{q_rm}/LperM3*(PsyHfg+PsyCpV*{ra_t}))", "w"),
    ("req_dp", "Supply dew point required by the room latent load", "°C", DEW.format(w="{req_w}"), "t"),
    ("sch_w", "Coil leaving humidity ratio of the tender schedule", "kg/kg",
     "((PsyHfg-(WaterCp-PsyCpV)*CoilLATWB)*" + WS.format(t="CoilLATWB") + "-PsyCpA*(CoilLATDB-CoilLATWB))/(PsyHfg+PsyCpV*CoilLATDB-WaterCp*CoilLATWB)", "w"),
    ("coil_rh", "Coil leaving relative humidity (from the tender schedule leaving condition)", "%",
     PW.format(w="{sch_w}") + "/" + PS.format(t="CoilLATDB"), "p"),
    ("reheat_ok", "Unit with a reheat coil (Y or N; N for a fan coil unit or a CRAC unit)", None,
     'IF(OR(B{src}="FCU",LEFT(A{src},3)="FCU",LEFT(A{src},4)="CRAC"),"N","Y")', None),
    ("dh_t", "Dry bulb at which the coil leaves the required humidity ratio", "°C",
     "PsyC*LN(" + PW.format(w="{req_w}") + "/({coil_rh}*PsyA))/(PsyB-LN(" + PW.format(w="{req_w}") + "/({coil_rh}*PsyA)))", "t"),
    ("cl_t", "COIL LEAVING DRY BULB", "°C", 'IF({reheat_ok}="Y",MIN({off_t},{dh_t}),{off_t})', "t"),
    ("cl_w", "Coil leaving humidity ratio", "kg/kg",
     "MIN({req_w},PsyEps*{coil_rh}*" + PS.format(t="{cl_t}") + "/(PsyP-{coil_rh}*" + PS.format(t="{cl_t}") + "))", "w"),
    ("cl_h", "Coil leaving enthalpy", "kJ/kg", H_OF.format(t="{cl_t}", w="{cl_w}"), "t"),
    ("cl_rh", "Coil leaving relative humidity", "%", PW.format(w="{cl_w}") + "/" + PS.format(t="{cl_t}"), "p"),
    ("sa_dp", "SUPPLY AIR DEW POINT", "°C", DEW.format(w="{cl_w}"), "t"),
    ("sa_rh", "Supply air relative humidity at the AHU outlet", "%", PW.format(w="{cl_w}") + "/" + PS.format(t="{sa_t}"), "p"),
    ("rm_w", "Room humidity ratio that results", "kg/kg", "{cl_w}+AA{src}/(AirDensity*{q_rm}/LperM3*(PsyHfg+PsyCpV*{ra_t}))", "w"),
    ("rm_rh", "Room relative humidity that results", "%", PW.format(w="{rm_w}") + "/" + PS.format(t="{ra_t}"), "p"),
    ("cc_s", "Cooling coil sensible duty", "kW", "AirSensibleFactor*{q_sa}*({mx_t}-{cl_t})/WperkW", "k"),
    ("cc_q", "COOLING COIL DUTY", "kW", "MAX(0,AirDensity*{q_sa}/LperM3*({mx_h}-{cl_h}))", "k"),
    ("cc_l", "Cooling coil latent duty", "kW", "{cc_q}-{cc_s}", "k"),
    ("cc_d", "Cooling coil duty less the coil duty of this tab", "kW", "{cc_q}-{tab_q}", "k"),
    ("rh_q", "REHEAT COIL DUTY", "kW", "AirSensibleFactor*{q_sa}*MAX(0,{off_t}-{cl_t})/WperkW", "k"),
    ("chw", "CHILLED WATER FLOW", "L/s", "{cc_q}/(WaterCp*(ChwLWT-ChwEWT)*WaterDensity)", "f"),
    ("hw", "REHEAT WATER FLOW", "L/s", "{rh_q}/(WaterCp*(HwEWT-HwLWT)*WaterDensity)", "f"),
    ("cc_d1", "Enthalpy difference, air in to saturated air at the leaving water (helper)", "kJ/kg", "{mx_h}-" + HS.format(t="ChwLWT"), "t"),
    ("cc_d2", "Enthalpy difference, air out to saturated air at the entering water (helper)", "kJ/kg", "{cl_h}-" + HS.format(t="ChwEWT"), "t"),
    ("cc_ntu", "Cooling coil transfer units (counterflow, log-mean enthalpy difference)", "–",
     'IF({mx_h}<={cl_h},0,IF(OR({cc_d1}<=0,{cc_d2}<=0),"not possible",({mx_h}-{cl_h})/IF(ABS({cc_d1}-{cc_d2})<CoilTol,{cc_d1},({cc_d1}-{cc_d2})/LN({cc_d1}/{cc_d2}))))', "r"),
    ("cc_rows", "COOLING COIL ROWS (estimate)", "no.", 'IF(ISNUMBER({cc_ntu}),ROUNDUP({cc_ntu}/CoilNTUPerRow,0),"–")', "i"),
    ("rh_d1", "Temperature difference, entering water to air out (helper)", "K", "HwEWT-{off_t}", "t"),
    ("rh_d2", "Temperature difference, leaving water to air in (helper)", "K", "HwLWT-{cl_t}", "t"),
    ("rh_ntu", "Reheat coil transfer units (counterflow, log-mean temperature difference)", "–",
     'IF({rh_q}<=0,0,IF(OR({rh_d1}<=0,{rh_d2}<=0),"not possible",({off_t}-{cl_t})/IF(ABS({rh_d1}-{rh_d2})<CoilTol,{rh_d1},({rh_d1}-{rh_d2})/LN({rh_d1}/{rh_d2}))))', "r"),
    ("rh_rows", "REHEAT COIL ROWS (estimate)", "no.", 'IF(ISNUMBER({rh_ntu}),ROUNDUP({rh_ntu}/HeatCoilNTUPerRow,0),"–")', "i"),
    ("ts_q", "Tender schedule basis: cooling coil duty with the coil leaving at the schedule condition", "kW",
     "MAX(0,AirDensity*{q_sa}/LperM3*({mx_h}-" + H_OF.format(t="CoilLATDB", w="{sch_w}") + "))", "k"),
    ("ts_rh", "Tender schedule basis: reheat from the schedule leaving dry bulb", "kW",
     'IF({reheat_ok}="Y",AirSensibleFactor*{q_sa}*MAX(0,{off_t}-CoilLATDB)/WperkW,0)', "k"),
    ("check", "Check", None,
     'IF({cc_ntu}="not possible","The chilled water cannot give this coil leaving state. VERIFY",IF({off_t}<ChwEWT,"The off-coil dry bulb is below the chilled water. VERIFY",'
     'IF({rh_q}>0,"Reheat: the coil cools below the required dry bulb to remove moisture",IF({cl_w}<{req_w}-CoilTol/WperkW,'
     '"No reheat: the coil removes more moisture than the room latent load; the room is below the design humidity","OK"))))', None),
]


def _cols(src: int, x: int) -> List[tuple]:
    """(header, unit, formula, number format) for the row x of the block that reads the row src of the tab."""
    ref = {k: f"{L(i + 2)}{x}" for i, (k, *_) in enumerate(SPEC)}
    live = f"AND(H{src}>0,J{src}>0)"
    out = [("AHU or system", None, f"=A{src}", None)]
    for k, head, unit, f, fmt in SPEC:
        out.append((head, unit, f'=IF({live},{f.format(src=src, **ref)},"–")', fmt))
    out.append(("Source ID", None, "GEN-PSY; EQS-COIL; GEN-COIL", None))
    return out


FMT = {"t": "0.00;\\−0.00;\\–", "w": "0.00000", "q": "#,##0.0;\\−#,##0.0;\\–", "k": "#,##0.00;\\−#,##0.00;\\–", "p": "0.0%", "r": "0.000", "s": "0.000000", "f": "#,##0.00;\\−#,##0.00;\\–", "i": "0;\\−0;\\–"}


def _style(dst, src):
    dst.font, dst.fill, dst.border, dst.alignment = copy(src.font), copy(src.fill), copy(src.border), copy(src.alignment)


def add(path_in: Path, path_out: Path) -> dict:
    wb = openpyxl.load_workbook(path_in)
    ws = wb["AHU"]
    if any(str(ws.cell(r, 1).value or "").startswith("C. PSYCHROMETRIC") for r in range(1, ws.max_row + 1)):
        raise SystemExit("the AHU tab already has the psychrometric block")
    sec, cur = {}, None
    for r in range(1, ws.max_row + 1):
        v = str(ws.cell(r, 1).value or "")
        if v.startswith("A. "):
            cur = "A"
        elif v.startswith("B. "):
            cur = "B"
        elif cur and v and v not in ("AHU or system", "TOTAL") and ws.cell(r, 2).value != "Type" and ws.cell(r, 34).value:
            sec.setdefault(cur, []).append(r)
    titles = {"A": "C. PSYCHROMETRIC STATE AT THE COIL DUTY: AS ZONED (ROWS OF SECTION A)",
              "B": "D. PSYCHROMETRIC STATE AT THE COIL DUTY: AFTER THE OUTDOOR-AIR RULE (ROWS OF SECTION B)"}
    note = ("Outdoor air and return air mix. Off-coil dry bulb = supply dry bulb required by the sensible load (this tab) less the supply fan rise "
            "(draw-through). Supply humidity ratio required = room design humidity ratio less the room latent load on the room supply airflow. "
            "The coil leaves the air at the relative humidity of the tender schedule leaving condition. Where the required humidity ratio needs a "
            "coil leaving dry bulb below the off-coil dry bulb, the coil cools to that dry bulb and a reheat coil warms the air (not on a fan coil "
            "unit or a CRAC unit). Where the coil at the off-coil dry bulb removes more moisture than required, the room is below the design "
            "humidity and there is no reheat. Cooling coil duty = air density × supply airflow × enthalpy drop, mixed air to coil leaving air. "
            "Water flow = duty ÷ (specific heat × temperature rise). Coil rows are an estimate from the transfer units of a counterflow coil; the "
            "coil selection governs. Return air is at the reference room condition of this tab (no return fan heat or duct gain). "
            "Sources GEN-PSY, EQS-COIL, GEN-COIL.")
    r0 = ws.max_row + 3
    h4, h5, h6, d7 = ws["A4"], ws["A5"], ws["A6"], ws["H7"]
    done = {}
    for k in ("A", "B"):
        rows = sec.get(k, [])
        if not rows:
            continue
        cols = _cols(rows[0], r0 + 4)
        n = len(cols)
        ws.cell(r0, 1, titles[k])
        for c in range(1, n + 1):
            _style(ws.cell(r0, c), h4)
        ws.cell(r0 + 1, 1, note)
        _style(ws.cell(r0 + 1, 1), ws["A2"])
        ws.merge_cells(start_row=r0 + 1, start_column=1, end_row=r0 + 1, end_column=12)
        ws.row_dimensions[r0 + 1].height = 112
        for c, (head, unit, _, _) in enumerate(cols, 1):
            a, b = ws.cell(r0 + 2, c, head), ws.cell(r0 + 3, c, unit)
            _style(a, h5)
            _style(b, h6)
        ws.row_dimensions[r0 + 2].height = 66
        for i, src in enumerate(rows):
            x = r0 + 4 + i
            for c, (head, _, f, fmt) in enumerate(_cols(src, x), 1):
                cell = ws.cell(x, c, f)
                _style(cell, ws["A7"] if c == 1 else d7)
                if fmt:
                    cell.number_format = FMT[fmt]
                if head.isupper() or head.startswith(("COOLING COIL ROWS", "REHEAT COIL ROWS")):
                    cell.font = Font(name=d7.font.name, sz=d7.font.sz, b=True)
        done[k] = (r0 + 4, r0 + 3 + len(rows))
        r0 = r0 + 4 + len(rows) + 2
    if ws.print_area:
        ws.print_area = re.sub(r"\d+$", str(r0 - 3), str(ws.print_area).split("!")[-1].replace("$", ""))
    path_out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path_out)
    return done


if __name__ == "__main__":
    print(add(Path(sys.argv[1]), Path(sys.argv[2])))
