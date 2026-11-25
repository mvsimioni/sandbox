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

NEWTON = 6
PS = "(PsyA*EXP(PsyB*{t}/(PsyC+{t})))"                     # saturation pressure, kPa
WS = "(PsyEps*" + PS + "/(PsyP-" + PS + "))"               # saturation humidity ratio
W_OF = "(({h})-PsyCpA*({t}))/(PsyHfg+PsyCpV*({t}))"        # humidity ratio from enthalpy and dry bulb
PW = "(({w})*PsyP/(PsyEps+({w})))"                         # vapour pressure from humidity ratio
DEW = "PsyC*LN(" + PW + "/PsyA)/(PsyB-LN(" + PW + "/PsyA))"


def _cols(src: int, x: int) -> List[tuple]:
    """(header, unit, formula, number format) for the row x of the block that reads the row src of the tab."""
    c = lambda k: f"{k}{x}"
    out = [
        ("AHU or system", None, f"=A{src}", None),
        ("Outdoor air dry bulb", "°C", "=OutdoorDB", "t"),
        ("Outdoor air enthalpy", "kJ/kg", "=OutdoorH", "t"),
        ("Outdoor air humidity ratio", "kg/kg", "=" + W_OF.format(h=c("C"), t=c("B")), "w"),
        ("Return air dry bulb (reference room)", "°C", f"=AB{src}", "t"),
        ("Return air enthalpy (reference room)", "kJ/kg", f"=AC{src}", "t"),
        ("Return air humidity ratio", "kg/kg", "=" + W_OF.format(h=c("F"), t=c("E")), "w"),
        ("Outdoor air", "L/s", f"=N{src}", "q"),
        ("Recirculated air", "L/s", f"=W{src}", "q"),
        ("AHU supply airflow", "L/s", f"=J{src}", "q"),
        ("Mixed air dry bulb", "°C", f"=IF({c('J')}>0,({c('H')}*{c('B')}+{c('I')}*{c('E')})/{c('J')},0)", "t"),
        ("Mixed air enthalpy", "kJ/kg", f"=IF({c('J')}>0,({c('H')}*{c('C')}+{c('I')}*{c('F')})/{c('J')},0)", "t"),
        ("Mixed air humidity ratio", "kg/kg", f"=IF({c('J')}>0,({c('H')}*{c('D')}+{c('I')}*{c('G')})/{c('J')},0)", "w"),
        ("Mixed air relative humidity", "%", f"=IF({c('J')}>0," + PW.format(w=c("M")) + "/" + PS.format(t=c("K")) + ",0)", "p"),
        ("Coil sensible", "kW", f"=AG{src}", "k"),
        ("Coil latent", "kW", f"=AH{src}", "k"),
        ("Coil duty (outdoor-air rule)", "kW", f"=AI{src}", "k"),
        ("Coil sensible heat ratio", "–", f"=IF({c('Q')}>0,{c('O')}/{c('Q')},0)", "r"),
        ("Off-coil dry bulb", "°C", f"=IF({c('J')}>0,{c('K')}-{c('O')}*WperkW/(AirSensibleFactor*{c('J')}),0)", "t"),
        ("Off-coil enthalpy", "kJ/kg", f"=IF({c('J')}>0,{c('L')}-{c('Q')}/(AirDensity*{c('J')}/LperM3),0)", "t"),
        ("Off-coil humidity ratio", "kg/kg", f"=IF({c('J')}>0," + W_OF.format(h=c("T"), t=c("S")) + ",0)", "w"),
        ("Saturation humidity ratio at the off-coil dry bulb", "kg/kg", "=" + WS.format(t=c("S")), "w"),
        ("Off-coil relative humidity", "%", f"=IF({c('J')}>0," + PW.format(w=c("U")) + "/" + PS.format(t=c("S")) + ",0)", "p"),
        ("Off-coil dew point", "°C", f"=IF({c('U')}>0," + DEW.format(w=c("U")) + ",0)", "t"),
        ("Supply fan temperature rise", "K", f"=IF({c('J')}>0,AF{src}*WperkW/(AirSensibleFactor*{c('J')}),0)", "t"),
        ("Supply air dry bulb at the AHU outlet", "°C", f"=IF({c('J')}>0,{c('S')}+{c('Y')},0)", "t"),
        ("Supply air humidity ratio", "kg/kg", f"={c('U')}", "w"),
        ("Supply air relative humidity", "%", f"=IF({c('J')}>0," + PW.format(w=c("AA")) + "/" + PS.format(t=c("Z")) + ",0)", "p"),
        ("SUPPLY AIR DEW POINT AT THE COIL DUTY", "°C", f"=IF({c('AA')}>0," + DEW.format(w=c("AA")) + ",0)", "t"),
        ("Apparatus dew point of the coil line", "°C",
         f"=IF({c('J')}=0,0,IFERROR(IF(ABS({WS.format(t=c(L(36 + NEWTON)))}-({c('M')}+({c(L(36 + NEWTON))}-{c('K')})*{c('AJ')}))<OffsetEps,"
         f"{c(L(36 + NEWTON))},\"none\"),\"none\"))", "t"),
        ("Coil contact factor", "–", f"=IF(ISNUMBER({c('AD')}),IF({c('K')}>{c('AD')},({c('K')}-{c('S')})/({c('K')}-{c('AD')}),0),\"–\")", "r"),
        ("Room humidity ratio that results (room latent on the room supply)", "kg/kg",
         f"=IF(H{src}>0,{c('AA')}+AA{src}/(AirDensity*H{src}/LperM3*(PsyHfg+PsyCpV*{c('E')})),0)", "w"),
        ("Room relative humidity that results", "%", f"=IF(H{src}>0," + PW.format(w=c("AF")) + "/" + PS.format(t=c("E")) + ",0)", "p"),
        ("Check", None,
         f"=IF({c('J')}=0,\"–\",IF({c('U')}>{c('V')},\"The off-coil state is past saturation: cooling alone cannot give this sensible and latent duty. VERIFY\","
         f"IF(NOT(ISNUMBER({c('AD')})),\"The coil line does not meet the saturation curve: no apparatus dew point. VERIFY\",\"OK\")))", None),
        ("Source ID", None, "GEN-PSY", None),
        ("Slope of the coil line (helper)", "kg/kg per K", f"=IF({c('K')}<>{c('S')},({c('M')}-{c('U')})/({c('K')}-{c('S')}),0)", "s"),
        ("Apparatus dew point, start (helper)", "°C", f"={c('X')}-1", "t"),
    ]
    for i in range(NEWTON):                                   # Newton steps on Ws(t) = W of the coil line at t
        t = c(L(37 + i))
        ps = PS.format(t=t)
        f = f"({WS.format(t=t)}-({c('M')}+({t}-{c('K')})*{c('AJ')}))"
        df = f"(PsyEps*PsyP*{ps}/(PsyP-{ps})^2*PsyB*PsyC/(PsyC+{t})^2-{c('AJ')})"
        out.append((f"Apparatus dew point, step {i + 1} (helper)", "°C", f"=IFERROR({t}-{f}/{df},{t})", "t"))
    out += _coil_cols(src, x, 38 + NEWTON)
    return out


HS = "(PsyCpA*{t}+" + WS + "*(PsyHfg+PsyCpV*{t}))"          # enthalpy of saturated air at t


def _coil_cols(src: int, x: int, c0: int) -> List[tuple]:
    """Reheat duty, chilled water and reheat water flow, coil rows (25 Nov 2026). c0 = first free column."""
    c = lambda k: f"{k}{x}"
    n = lambda i: c(L(c0 + i))
    hs_l, hs_e = HS.format(t="ChwLWT"), HS.format(t="ChwEWT")

    def ntu(h_in: str, h_out: str) -> str:                  # counterflow, enthalpy potential: air in against leaving water, air out against entering water
        d1, d2 = f"({h_in}-{hs_l})", f"({h_out}-{hs_e})"
        return (f"=IF(OR({c('J')}=0,{h_in}<={h_out}),0,IF(OR({d1}<=0,{d2}<=0),\"not possible\","
                f"({h_in}-{h_out})/IF(ABS({d1}-{d2})<OffsetEps,{d1},({d1}-{d2})/LN({d1}/{d2}))))")

    def rows(k: str) -> str:
        return f"=IF(ISNUMBER({k}),IF({k}>0,ROUNDUP({k}/CoilNTUPerRow,0),0),\"–\")"
    wwb = WS.format(t=n(5))
    return [
        ("Chilled water flow at the coil duty of this tab", "L/s", f"={c('Q')}/(WaterCp*(ChwLWT-ChwEWT)*WaterDensity)", "f"),
        ("Cooling coil transfer units at the coil duty of this tab", "–", ntu(c("L"), c("T")), "r"),
        ("Cooling coil rows at the coil duty of this tab (estimate)", "no.", rows(n(1)), "i"),
        ("Cooling coil is at the schedule leaving condition and the air is reheated (Y or N; N for a fan coil unit)", None, f"=IF(AND({c('J')}>0,{c('S')}>CoilLATDB,B{src}<>\"FCU\"),\"Y\",\"N\")", None),
        ("Coil leaving dry bulb, schedule", "°C", "=CoilLATDB", "t"),
        ("Coil leaving wet bulb, schedule", "°C", "=CoilLATWB", "t"),
        ("Coil leaving humidity ratio, schedule", "kg/kg",
         f"=((PsyHfg-(WaterCp-PsyCpV)*{n(5)})*{wwb}-PsyCpA*({n(4)}-{n(5)}))/(PsyHfg+PsyCpV*{n(4)}-WaterCp*{n(5)})", "w"),
        ("Coil leaving enthalpy, schedule", "kJ/kg", f"=PsyCpA*{n(4)}+{n(6)}*(PsyHfg+PsyCpV*{n(4)})", "t"),
        ("Supply air dew point with the coil at the schedule leaving condition", "°C", "=" + DEW.format(w=n(6)), "t"),
        ("REHEAT COIL DUTY", "kW", f"=IF({n(3)}=\"Y\",AirSensibleFactor*{c('J')}*({c('S')}-{n(4)})/WperkW,0)", "k"),
        ("Cooling coil duty with reheat (coil at the schedule leaving condition)", "kW",
         f"=IF({n(3)}=\"Y\",MAX({c('Q')},AirDensity*{c('J')}/LperM3*({c('L')}-{n(7)})),{c('Q')})", "k"),
        ("CHILLED WATER FLOW (cooling coil duty with reheat)", "L/s", f"={n(10)}/(WaterCp*(ChwLWT-ChwEWT)*WaterDensity)", "f"),
        ("REHEAT WATER FLOW", "L/s", f"={n(9)}/(WaterCp*(HwEWT-HwLWT)*WaterDensity)", "f"),
        ("Cooling coil transfer units with reheat", "–", ntu(c("L"), f"IF({n(3)}=\"Y\",MIN({c('T')},{n(7)}),{c('T')})"), "r"),
        ("COOLING COIL ROWS (estimate)", "no.", rows(n(13)), "i"),
        ("Reheat coil transfer units", "–",
         f"=IF({n(9)}<=0,0,IF(OR(HwEWT<={c('S')},HwLWT<={n(4)}),\"not possible\",({c('S')}-{n(4)})/IF(ABS((HwEWT-{c('S')})-(HwLWT-{n(4)}))<OffsetEps,"
         f"HwEWT-{c('S')},((HwEWT-{c('S')})-(HwLWT-{n(4)}))/LN((HwEWT-{c('S')})/(HwLWT-{n(4)})))))", "r"),
        ("REHEAT COIL ROWS (estimate)", "no.", rows(n(15)), "i"),
        ("Source ID", None, "EQS-COIL; GEN-COIL", None),
    ]


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
    note = ("Outdoor air and return air mix; the coil takes the sensible, latent and total duty of this tab from the mixed air; the supply fan "
            "(draw-through) warms the air after the coil. Dry bulb by the sensible air constant, enthalpy by the air density, both on the AHU "
            "supply airflow, so that the states close on the coil duty of this tab. Humidity ratio from enthalpy and dry bulb; dew point and "
            "saturation by the psychrometric constants of the Basis of Design (source GEN-PSY). The return air is at the reference room "
            "condition (no return fan heat or duct gain). The supply air dew point is the dew point of the off-coil air. Reheat: where the off-coil "
            "dry bulb that the sensible load needs is above the coil leaving dry bulb of the tender schedule, the coil cools to the schedule leaving "
            "condition and a reheat coil warms the air to that dry bulb. Water flow = duty ÷ (specific heat × temperature rise). Coil rows are an "
            "estimate from the transfer units of a counterflow coil (sources EQS-COIL, GEN-COIL); the coil selection governs.")
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
        ws.row_dimensions[r0 + 1].height = 88
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
                if head.isupper() or head.startswith(("SUPPLY AIR DEW POINT", "CHILLED WATER FLOW (", "COOLING COIL ROWS", "REHEAT")):
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
