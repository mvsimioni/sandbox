"""Supporting calculation workbook for the reply to AH-C2-RCC-RFI-ME-0274 (AHU-B-06-06STAFF-01).

Inputs (blue) are taken from the R22 D47 workbook and the scheme ruled by Marcus (D-126, D-127); sizing, airflow, outdoor-air and coil
calculations are live formulas; the hourly part-load results (green) are values of the hourly model (src/rfi/me0274_scheme.py), and the
part-load statistics on sheet "Part load" are formulas over sheet "Hourly".
``python3 -I src/rfi/me0274_calc_xlsx.py`` writes out/issue/RFI_ME-0274_reply/AIGH_IP_ME-0274_AHU-B-06-06STAFF-01_Calculation.xlsx
"""
import csv
import json
import sys
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter as L_

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
DIR = ROOT / "out/issue/RFI_ME-0274_reply"
S = json.load(open(DIR / "scheme.json", encoding="utf-8"))
WB_SRC = ROOT / "out/issue/DRAFT_2026-11-27_R22/workbooks/AIGH_C2_D47_Admin_Workspace_Thermal_Load_Calc_RevA.xlsx"
OUT = DIR / "AIGH_IP_ME-0274_AHU-B-06-06STAFF-01_Calculation.xlsx"
TAG = S["unit"]

F_HEAD = Font(name="Arial", bold=True, color="FFFFFF", size=9)
FILL_HEAD = PatternFill("solid", fgColor="2F4F6F")
F_IN = Font(name="Arial", color="0000CC", size=9)
F_CALC = Font(name="Arial", color="000000", size=9)
F_RES = Font(name="Arial", color="006100", size=9)
FILL_RES = PatternFill("solid", fgColor="E2EFDA")
FILL_KEY = PatternFill("solid", fgColor="FFF2CC")
F_TITLE = Font(name="Arial", bold=True, size=13)
F_SUB = Font(name="Arial", bold=True, size=10)
F_TXT = Font(name="Arial", size=9)
THIN = Border(bottom=Side(style="thin", color="BFBFBF"))
WRAP = Alignment(wrap_text=True, vertical="top")


def head(ws, row, labels, widths=None):
    for j, t in enumerate(labels, 1):
        c = ws.cell(row, j, t)
        c.font, c.fill, c.alignment = F_HEAD, FILL_HEAD, Alignment(wrap_text=True, vertical="center", horizontal="center")
    ws.row_dimensions[row].height = 42
    for j, w in enumerate(widths or [], 1):
        ws.column_dimensions[L_(j)].width = w


def put(ws, row, col, val, kind="calc", fmt=None):
    c = ws.cell(row, col, val)
    c.font = {"in": F_IN, "calc": F_CALC, "res": F_RES}[kind]
    if kind == "res":
        c.fill = FILL_RES
    if fmt:
        c.number_format = fmt
    return c


def title(ws, text, sub=None):
    ws["A1"] = text
    ws["A1"].font = F_TITLE
    if sub:
        ws["A2"] = sub
        ws["A2"].font = F_TXT


src = openpyxl.load_workbook(WB_SRC, data_only=True)
af = src["Airflow"]
A = {}
for r in range(7, af.max_row + 1):
    if af.cell(r, 4).value == TAG:
        g = lambda c: af.cell(r, c).value
        A[g(1)] = dict(vol=g(7) or 0, qs=g(8) or 0, ql=g(9) or 0, troom=g(10) or 24, ach=g(12) or 0, th14=g(13) or 0, mins=g(19) or 0,
                       final14=g(20) or 0, fixed=bool(g(34)) or str(g(22) or "").upper().startswith("Y"), vmin_wb=g(28) or 0)
cr = src["Criteria"]
CRIT = {}
for r in range(7, cr.max_row + 1):
    n = cr.cell(r, 1).value
    if n in A:
        CRIT[n] = (cr.cell(r, 17).value, cr.cell(r, 18).value, cr.cell(r, 19).value)
STATS = {x["room"]: x for x in S["room_stats"]}
R = sorted(S["rooms"], key=lambda x: x["room"])
N = len(R)

wb = openpyxl.Workbook()
# ------------------------------------------------------------------ Cover
ws = wb.active
ws.title = "Cover"
title(ws, f"AH-C2-RCC-RFI-ME-0274 - {TAG}: supporting calculation", "AIGH Inpatient, L6 Surgery Admin Area. 27 Nov 2026.")
rows = [
    ("Purpose", "Calculation behind the consultant reply to RFI ME-0274: room airflows, AHU airflow, outdoor-air intake, coils and the part-load check."),
    ("Unit", f"{TAG}: {N} rooms, {sum(x['area'] for x in R):,.1f} m2."),
    ("Scheme", "Supply 17.6 degC at the rooms; cooling coil leaving 12.0 degC with central reheat to 15.4 degC and an hourly reset; VAV minimum = "
               "DD Booklet air change, reduced in 13 rooms; outdoor-air intake reset each hour to the room that needs the most outdoor air."),
    ("Sheets", "Inputs - design inputs and schedule values. Rooms - room airflows (formulas). AHU - airflow and outdoor air (formulas). "
               "Psychrometrics - state points and coils (formulas). Part load - summary of the hourly check (formulas over Hourly, and the "
               "reference cases). Room results - hourly check by room (model values). Hourly - 8,760 h of the hourly check (model values). "
               "Schedule - Equipment Schedule values. Findings - notes and assumptions."),
    ("Colour key", "Blue text: input. Black text: formula. Green cell: result of the hourly model (value)."),
    ("Sources", "R22 thermal-load workbook D47 (room loads, air-change airflow, outdoor air, criteria of the DD Booklet); tender air-side schematic "
                "IP-BH-06068; Equipment Schedule (Corrigendum 5); Changi typical meteorological year (hourly weather); Engineer's rulings D-126, D-127."),
    ("Relations", "Saturation pressure 0.61094 exp(17.625 t / (243.04 + t)) kPa; humidity ratio 0.622 pv / (101.325 - pv); enthalpy "
                  "1.006 t + W (2501 + 1.86 t) kJ/kg; sensible 1.21 x L/s x K W; latent 3010 x L/s x dW W; total 1.2 x L/s x dh W."),
]
for i, (k, v) in enumerate(rows, 4):
    ws.cell(i, 1, k).font = F_SUB
    c = ws.cell(i, 2, v); c.font = F_TXT; c.alignment = WRAP
    ws.row_dimensions[i].height = 15 * max(1, len(v) // 110 + 1)
ws.column_dimensions["A"].width = 14
ws.column_dimensions["B"].width = 120

# ------------------------------------------------------------------ Inputs
wi = wb.create_sheet("Inputs")
title(wi, "Design inputs")
IN = {}
inputs = [
    ("TS", "Supply air temperature at the rooms", S["supply_at_rooms"], "degC", "Engineer's ruling (D-126)"),
    ("TROOM", "Room design temperature", 24, "degC", "Basis of Design"),
    ("FAN", "Supply fan temperature rise", S["fan_rise"], "K", "D47 AHU sheet (1,700 Pa, 65 % fan and motor efficiency, draw-through)"),
    ("TCOIL", "Cooling coil leaving dry bulb", S["coil_leaving"], "degC", "Engineer's ruling (D-126): dehumidification"),
    ("RHCOIL", "Cooling coil leaving RH", 0.889, "-", "tender Equipment Schedule (12.0 / 11.0 degC)"),
    ("PATM", "Atmospheric pressure", 101.325, "kPa", "standard"),
    ("TOA", "Outdoor design dry bulb", 32, "degC", "Basis of Design"),
    ("WBOA", "Outdoor design wet bulb", 26, "degC", "Basis of Design"),
    ("LEAK", "Duct leakage allowance", 0.10, "-", "Basis of Design"),
    ("QS", "Room sensible load (with safety factor)", S["room_sensible"], "kW", "D47 AHU sheet"),
    ("QL", "Room latent load", S["room_latent"], "kW", "D47 AHU sheet"),
    ("BOXMIN", "Smallest VAV box setting", S["box_min"], "L/s", "ASSUMPTION"),
    ("DTCHW", "Chilled water temperature difference", 8, "K", "8 / 16 degC"),
    ("DTHW", "Reheat water temperature difference", 10, "K", "35 / 25 degC"),
    ("S_SUP", "Schedule value: supply airflow", 7700, "L/s", "rounded up"),
    ("S_COIL", "Schedule value: cooling", 240, "kW", "rounded up"),
    ("S_RH", "Schedule value: central reheat", 65, "kW", "rounded up"),
    ("S_OAMIN", "Schedule value: outdoor airflow min", 950, "L/s", "not below the sum of the room outdoor air"),
    ("S_OAMAX", "Schedule value: outdoor airflow max", 2600, "L/s", "rounded up from the reset maximum"),
]
head(wi, 3, ["Key", "Input", "Value", "Unit", "Source"], [10, 44, 12, 8, 70])
for i, (k, lab, v, u, srcs) in enumerate(inputs, 4):
    wi.cell(i, 1, k).font = F_TXT
    wi.cell(i, 2, lab).font = F_TXT
    put(wi, i, 3, v, "in", "0.000" if isinstance(v, float) and v < 1 else None)
    wi.cell(i, 4, u).font = F_TXT
    wi.cell(i, 5, srcs).font = F_TXT
    IN[k] = f"Inputs!$C${i}"
wi.freeze_panes = "A4"

# ------------------------------------------------------------------ Rooms
wr = wb.create_sheet("Rooms")
title(wr, "Room airflows", "Design airflow = larger of the air-change airflow, the thermal-load airflow at the supply temperature and the supply minimum "
      "from exhaust and pressure, rounded up to 10 L/s; fixed rooms keep their airflow. VAV minimum: air-change airflow, or the reduced minimum.")
cols = ["Room", "Name", "Area m2", "Volume m3", "Room temp degC", "Room sensible W", "Air-change airflow L/s", "ACH", "Supply min (exhaust / pressure) L/s",
        "Fixed airflow", "Thermal-load airflow at TS L/s", "Design airflow L/s", "Governing", "Design airflow at 14 degC (issued workbook) L/s",
        "Minimum basis", "Airflow holding the DD Booklet minimum temp. (0.4 % of hours) L/s", "VAV minimum L/s", "Minimum / air-change airflow",
        "Room outdoor air L/s", "Exhaust L/s", "Return L/s", "Outdoor-air share at design", "AHU intake needed at design flow L/s",
        "DD Booklet T min", "T max", "RH max %", "24 h", "Accepted out of range (24 h, negligible load)"]
head(wr, 4, cols, [17, 26, 8, 8, 8, 9, 9, 6, 10, 7, 10, 9, 12, 10, 12, 12, 9, 9, 9, 7, 8, 9, 10, 7, 6, 6, 5, 9])
r0 = 5
for i, x in enumerate(R):
    r = r0 + i
    a = A[x["room"]]
    tmn, tmx, rhm = CRIT.get(x["room"], (None, None, None))
    st = STATS[x["room"]]
    put(wr, r, 1, x["room"], "in"); put(wr, r, 2, x["name"], "in"); put(wr, r, 3, x["area"], "in", "0.0"); put(wr, r, 4, a["vol"], "in", "0.0")
    put(wr, r, 5, a["troom"], "in"); put(wr, r, 6, a["qs"], "in", "0"); put(wr, r, 7, a["ach"], "in", "0.0")
    put(wr, r, 8, f"=IF(D{r}>0,G{r}*3.6/D{r},0)", fmt="0.0")
    put(wr, r, 9, a["mins"], "in", "0"); put(wr, r, 10, "Y" if a["fixed"] else "N", "in")
    put(wr, r, 11, f"=IF(F{r}>0,F{r}/(1.21*(E{r}-{IN['TS']})),0)", fmt="0.0")
    put(wr, r, 12, f"=IF(J{r}=\"Y\",N{r},ROUNDUP(MAX(G{r},K{r},I{r})/10,0)*10)", fmt="0")
    put(wr, r, 13, f"=IF(J{r}=\"Y\",\"fixed\",IF(K{r}>G{r},\"thermal load\",\"air change\"))")
    put(wr, r, 14, a["final14"], "in", "0")
    basis = "reduced" if x["vmin_basis"].startswith("28") else ("fixed" if x["vmin_basis"] == "fixed" else "air change")
    put(wr, r, 15, basis, "in")
    if basis == "reduced":
        put(wr, r, 16, st["need_q04"], "res", "0.0")
        put(wr, r, 17, f"=MIN(L{r},MAX(S{r},{IN['BOXMIN']},FLOOR(P{r},5)))", fmt="0.0")
    else:
        put(wr, r, 17, f"=IF(O{r}=\"fixed\",L{r},{a['vmin_wb']})", fmt="0.0")
    put(wr, r, 18, f"=IF(G{r}>0,Q{r}/G{r},\"\")", fmt="0%")
    put(wr, r, 19, x["oa"], "in", "0.0"); put(wr, r, 20, x["exh"], "in", "0")
    put(wr, r, 21, f"=MAX(L{r}-T{r},0)", fmt="0")
    put(wr, r, 22, f"=IF(L{r}>0,S{r}/L{r},0)", fmt="0.0%")
    put(wr, r, 23, f"=S{r}*AHU!$C$6/L{r}", fmt="0")
    put(wr, r, 24, tmn, "in"); put(wr, r, 25, tmx, "in"); put(wr, r, 26, rhm, "in")
    put(wr, r, 27, "Y" if st["h24"] else "N", "in"); put(wr, r, 28, "Y" if st["accepted"] else "N", "in")
rl = r0 + N - 1
rt = rl + 1
wr.cell(rt, 1, f"TOTAL ({N} rooms)").font = F_SUB
for c in (3, 4, 6, 7, 12, 14, 17, 19, 20, 21):
    put(wr, rt, c, f"=SUM({L_(c)}{r0}:{L_(c)}{rl})", fmt="#,##0")
    wr.cell(rt, c).font = Font(name="Arial", bold=True, size=9)
red = rt + 2
wr.cell(red, 1, "13 rooms with a reduced minimum").font = F_SUB
put(wr, red, 7, f"=SUMIF(O{r0}:O{rl},\"reduced\",G{r0}:G{rl})", fmt="#,##0")
put(wr, red, 17, f"=SUMIF(O{r0}:O{rl},\"reduced\",Q{r0}:Q{rl})", fmt="#,##0")
put(wr, red, 18, f"=Q{red}/G{red}", fmt="0%")
wr.freeze_panes = "C5"

# ------------------------------------------------------------------ Hourly (values) - written first so AHU / Part load can refer to it
wh = wb.create_sheet("Hourly")
title(wh, "Hourly part-load check (model values)", "Changi typical meteorological year. Working = Mon-Fri 08:00-18:00 (ASSUMPTION). Rooms counted: all tested "
      "rooms except the accepted 24 h rooms with negligible load.")
hrows = list(csv.reader(open(DIR / "hourly.csv", encoding="utf-8")))
hdr = ["Month", "Day", "Hour", "Working (1)", "Outdoor DB degC", "Outdoor DP degC", "AHU supply L/s", "Outdoor-air intake L/s", "Supply at rooms degC",
       "Central reheat reset K", "Central reheat kW", "Cooling coil kW", "Rooms cold or humid, no reset", "Rooms cold or humid, with reset", "Rooms above T max"]
head(wh, 4, hdr, [6, 5, 5, 8, 8, 8, 9, 9, 9, 9, 9, 9, 10, 10, 9])
for k, row in enumerate(hrows[1:], 5):
    for j, v in enumerate(row, 1):
        c = wh.cell(k, j, float(v) if "." in v else int(v))
        c.font = F_RES
H0, H1 = 5, 4 + len(hrows) - 1
wh.freeze_panes = "A5"
rng = lambda col: f"Hourly!${col}${H0}:${col}${H1}"

# ------------------------------------------------------------------ AHU
wa = wb.create_sheet("AHU", 2)
title(wa, f"{TAG}: airflow and outdoor air")
head(wa, 4, ["Item", "", "Value", "Unit", "Formula / note"], [44, 2, 12, 8, 90])
roomref = lambda c: f"Rooms!${c}${rt}"
lines = [
    ("Sum of room design airflow", f"={roomref('L')}", "L/s", "sum of Rooms column L"),
    ("AHU supply airflow", f"=C5*(1+{IN['LEAK']})", "L/s", "room airflow x (1 + duct leakage)"),
    ("Schedule value", f"={IN['S_SUP']}", "L/s", "rounded up"),
    ("Sum of VAV minimum airflow", f"={roomref('Q')}", "L/s", "sum of Rooms column Q"),
    ("VAV minimum / design airflow", "=C8/C5", "-", ""),
    ("Sum of room outdoor air", f"={roomref('S')}", "L/s", "HFDS / ASHRAE 170 and SS 553 Table 1 rates of the rooms"),
    ("Outdoor-air intake needed at design flow", f"=MAX(Rooms!$W${r0}:$W${rl})", "L/s", "largest of room outdoor air x AHU supply / room supply (the room that needs the most)"),
    ("Room that sets the intake at design flow", f"=INDEX(Rooms!$A${r0}:$A${rl},MATCH(C11,Rooms!$W${r0}:$W${rl},0))", "", ""),
    ("Intake, working hours: minimum", f"=_xlfn.MINIFS({rng('H')},{rng('D')},1)", "L/s", "hourly reset (sheet Hourly)"),
    ("Intake, working hours: mean", f"=AVERAGEIFS({rng('H')},{rng('D')},1)", "L/s", ""),
    ("Intake, working hours: maximum", f"=_xlfn.MAXIFS({rng('H')},{rng('D')},1)", "L/s", ""),
    ("Intake, after hours (24 h rooms): maximum", f"=_xlfn.MAXIFS({rng('H')},{rng('D')},0)", "L/s", ""),
    ("Intake for the coil duty", "=MAX(C11,C15)", "L/s", "larger of design-flow need and part-load maximum"),
    ("Schedule: outdoor airflow min", f"={IN['S_OAMIN']}", "L/s", "not below the sum of the room outdoor air"),
    ("Schedule: outdoor airflow max", f"={IN['S_OAMAX']}", "L/s", ""),
    ("Dedicated exhaust", f"={roomref('T')}", "L/s", "Cleaners Room and Toilets, single pass"),
    ("Relief air at the maximum intake", "=C19-C20", "L/s", "intake less exhaust"),
    ("Check: schedule >= calculated", '=IF(AND(C7>=C6,C19>=C15,C18>=C10),"OK","CHECK")', "", ""),
]
for i, (lab, f, u, note) in enumerate(lines, 5):
    wa.cell(i, 1, lab).font = F_TXT
    put(wa, i, 3, f, fmt="0.00" if "/" in lab and "design airflow" in lab else "#,##0")
    wa.cell(i, 4, u).font = F_TXT
    wa.cell(i, 5, note).font = F_TXT
wa["C9"].number_format = "0%"

# ------------------------------------------------------------------ Psychrometrics
wp = wb.create_sheet("Psychrometrics", 3)
title(wp, "State points and coils (design condition)")
head(wp, 4, ["Item", "", "Value", "Unit", "Formula / note"], [44, 2, 12, 8, 90])
ps = lambda t: f"(0.61094*EXP(17.625*({t})/(243.04+({t}))))"
W = lambda t, rh: f"0.622*({rh})*{ps(t)}/({IN['PATM']}-({rh})*{ps(t)})"
h = lambda t, w: f"1.006*({t})+({w})*(2501+1.86*({t}))"
plines = [
    ("Coil leaving dry bulb", f"={IN['TCOIL']}", "degC", ""),                                                                    # 5
    ("Coil leaving humidity ratio", f"={W('C5', IN['RHCOIL'])}", "kg/kg", "at the coil leaving RH"),                              # 6
    ("Coil leaving dew point", f"=243.04*LN(C6*{IN['PATM']}/(0.622+C6)/0.61094)/(17.625-LN(C6*{IN['PATM']}/(0.622+C6)/0.61094))", "degC", ""),  # 7
    ("Coil leaving wet bulb", S["coil_leaving_wb"], "degC", "solved from the humidity ratio (value)"),                            # 8
    ("Coil leaving enthalpy", f"={h('C5', 'C6')}", "kJ/kg", ""),                                                                  # 9
    ("Central reheat leaving (supply less fan rise)", f"={IN['TS']}-{IN['FAN']}", "degC", ""),                                    # 10
    ("Supply at the rooms", f"=C10+{IN['FAN']}", "degC", ""),                                                                     # 11
    ("AHU supply airflow", "=AHU!C6", "L/s", ""),                                                                                 # 12
    ("Room humidity ratio (steady state)", f"=C6+{IN['QL']}*1000/(3010*AHU!C5)", "kg/kg", "supply + room latent / (3010 x room airflow)"),  # 13
    ("Room RH that results at 24 degC", f"=C13*{IN['PATM']}/(0.622+C13)/{ps(IN['TROOM'])}", "-", ""),                              # 14
    ("Outdoor humidity ratio (32 / 26 degC)", f"=((2501-2.326*{IN['WBOA']})*0.622*{ps(IN['WBOA'])}/({IN['PATM']}-{ps(IN['WBOA'])})-1.006*({IN['TOA']}-{IN['WBOA']}))/(2501+1.86*{IN['TOA']}-4.186*{IN['WBOA']})", "kg/kg", ""),  # 15
    ("Outdoor-air intake for the coil duty", "=AHU!C17", "L/s", ""),                                                              # 16
    ("Mixed air dry bulb", f"=(C16*{IN['TOA']}+(C12-C16)*{IN['TROOM']})/C12", "degC", ""),                                        # 17
    ("Mixed air humidity ratio", "=(C16*C15+(C12-C16)*C13)/C12", "kg/kg", "return at the room condition that results"),           # 18
    ("Mixed air enthalpy", f"={h('C17', 'C18')}", "kJ/kg", ""),                                                                   # 19
    ("Mixed air wet bulb", S["mix_wb"], "degC", "solved from the humidity ratio (value)"),                                       # 20
    ("Cooling coil total", "=1.2*C12/1000*(C19-C9)", "kW", "1.2 x L/s x dh"),                                                    # 21
    ("Cooling coil sensible", "=1.2*C12/1000*1.006*(C17-C5)", "kW", ""),                                                         # 22
    ("Mixed air humidity ratio, return 24 degC / 60 % (pull-down)", f"=(C16*C15+(C12-C16)*{W(IN['TROOM'], '0.6')})/C12", "kg/kg", ""),  # 23
    ("Cooling coil total, pull-down", f"=1.2*C12/1000*({h('C17', 'C23')}-C9)", "kW", ""),                                         # 24
    ("Cooling coil, hourly peak of the year", f"=MAX({rng('L')})", "kW", "sheet Hourly"),                                         # 25
    ("Schedule value: cooling", f"={IN['S_COIL']}", "kW", ""),                                                                    # 26
    ("Chilled water flow", f"=C26/(4.186*{IN['DTCHW']})", "L/s", ""),                                                             # 27
    ("Supply fan heat", f"=1.21*C12*{IN['FAN']}/1000", "kW", ""),                                                                 # 28
    ("Central reheat at design flow (coil leaving to C10)", "=1.21*C12*(C10-C5)/1000", "kW", ""),                                 # 29
    ("Central reheat, peak of the year (with reset)", f"=MAX({rng('K')})", "kW", "sheet Hourly"),                                 # 30
    ("Central reheat, mean in working hours", f"=AVERAGEIFS({rng('K')},{rng('D')},1)", "kW", ""),                                 # 31
    ("Schedule value: central reheat", f"={IN['S_RH']}", "kW", ""),                                                               # 32
    ("Reheat water flow", f"=C32/(4.19*{IN['DTHW']})", "L/s", ""),                                                                # 33
    ("Check: schedule >= calculated", '=IF(AND(C26>=C21,C32>=C30),"OK","CHECK")', "", ""),                                       # 34
]
for i, (lab, f, u, note) in enumerate(plines, 5):
    wp.cell(i, 1, lab).font = F_TXT
    kind = "res" if isinstance(f, float) else "calc"
    fmt = "0.00000" if u == "kg/kg" else ("0%" if u == "-" else "0.0")
    put(wp, i, 3, f, kind, fmt)
    wp.cell(i, 4, u).font = F_TXT
    wp.cell(i, 5, note).font = F_TXT

# ------------------------------------------------------------------ Part load
wl = wb.create_sheet("Part load", 4)
title(wl, "Part-load check", "Test: hours in which a room is below its DD Booklet minimum temperature or above its RH limit, as a share of the hours "
      "it operates; accepted below 0.4 %.")
head(wl, 4, ["Item", "", "Value", "Unit", "Formula / note"], [50, 2, 12, 8, 90])
work_n = f"COUNTIF({rng('D')},1)"
pl = [
    ("Working hours in the year", f"={work_n}", "h", ""),
    ("Working hours with a room cold or humid, no reset", f"=COUNTIFS({rng('D')},1,{rng('M')},\">0\")/{work_n}", "-", "adopted scheme without the reheat reset"),
    ("Working hours with a room cold or humid, with reset", f"=COUNTIFS({rng('D')},1,{rng('N')},\">0\")/{work_n}", "-", "adopted scheme"),
    ("Working hours with a room above T max", f"=COUNTIFS({rng('D')},1,{rng('O')},\">0\")/{work_n}", "-", "peak solar rooms (sheet Room results)"),
    ("Working hours with the reheat reset", f"=COUNTIFS({rng('D')},1,{rng('J')},\">0\")/{work_n}", "-", ""),
    ("Mean reset in working hours", f"=AVERAGEIFS({rng('J')},{rng('D')},1)", "K", ""),
    ("Maximum reset", f"=MAX({rng('J')})", "K", ""),
]
for i, (lab, f, u, note) in enumerate(pl, 5):
    wl.cell(i, 1, lab).font = F_TXT
    put(wl, i, 3, f, fmt="0.0%" if u == "-" else "0.0" if u == "K" else "#,##0")
    wl.cell(i, 4, u).font = F_TXT
    wl.cell(i, 5, note).font = F_TXT
r = 14
wl.cell(r, 1, "Reference cases (model values)").font = F_SUB
head(wl, r + 1, ["Case", "", "Working hours with a room cold or humid", "Mean rooms cold", "RH alone", "With one central temperature: cold / humid",
                 "With one central temperature: hot"])
cases = [("1 Supply 14 degC, VAV minimum 6 ACH (issued workbook airflows)", S["ref14"]),
         ("2 Supply 17.6 degC, airflows re-sized, coil 15.4 degC", S["ref176"]),
         ("3 Adopted: 17.6 degC, coil 12 degC + central reheat, reduced minimum in 13 rooms, outdoor-air reset", S["final"])]
for i, (lab, d) in enumerate(cases, r + 2):
    wl.cell(i, 1, lab).font = F_TXT
    for j, k in enumerate(("hours_cold_or_humid", "rooms_cold_mean", "hours_humid", "central_cold_or_humid", "central_hot"), 3):
        put(wl, i, j, d[k] / 100 if k != "rooms_cold_mean" else d[k], "res", "0.0%" if k != "rooms_cold_mean" else "0.0")
wl.column_dimensions["F"].width = 14
wl.column_dimensions["G"].width = 14

# ------------------------------------------------------------------ Room results
wq = wb.create_sheet("Room results", 5)
title(wq, "Hourly check by room (model values, adopted scheme)", "Shares of the hours each room operates. 'No reset': central reheat at its base setting only.")
cols = ["Room", "Name", "Hours", "24 h", "Accepted", "Cold, no reset", "Humid, no reset", "Hot, no reset", "Cold, with reset", "Humid, with reset",
        "Hot, with reset", "T mean degC", "T min degC", "T max degC", "RH max %", "Mean airflow L/s"]
head(wq, 4, cols, [17, 26, 7, 5, 8, 8, 8, 8, 8, 8, 8, 7, 7, 7, 7, 8])
for i, x in enumerate(R, 5):
    st = STATS[x["room"]]
    vals = [x["room"], x["name"], st["hours"], "Y" if st["h24"] else "N", "Y" if st["accepted"] else "N", st["cold"] / 100, st["humid"] / 100, st["hot"] / 100,
            st["cold_r"] / 100, st["humid_r"] / 100, st["hot_r"] / 100, st["T_mean"], st["T_min"], st["T_max"], st["RH_max"], st["V_mean"]]
    for j, v in enumerate(vals, 1):
        put(wq, i, j, v, "res", "0.0%" if 6 <= j <= 11 else None)
wq.freeze_panes = "C5"

# ------------------------------------------------------------------ Schedule
wsch = wb.create_sheet("Schedule", 6)
title(wsch, "Equipment Schedule (Corrigendum 5), Air Handling Unit Schedule: corrected values of " + TAG)
head(wsch, 4, ["Part", "Item", "Tender", "Corrected", "Unit", "Reference"], [6, 40, 10, 10, 8, 40])
sch = [("A", "Supply airflow", 9000, f"={IN['S_SUP']}", "L/s", "AHU!C6"),
       ("A", "Minimum total cooling", 600, f"={IN['S_COIL']}", "kW", "Psychrometrics!C21"),
       ("A", "Minimum total re-heating", 600, f"={IN['S_RH']}", "kW", "Psychrometrics!C30"),
       ("A", "Outdoor airflow, min", 307, f"={IN['S_OAMIN']}", "L/s", "AHU!C10"),
       ("A", "Outdoor airflow, max", "N/A", f"={IN['S_OAMAX']}", "L/s", "AHU!C15"),
       ("B", "Re-heating coil air flow", 9000, f"={IN['S_SUP']}", "L/s", ""),
       ("B", "Re-heating coil water flow", 19.6, "=Psychrometrics!C33", "L/s", ""),
       ("B", "Cooling coil EAT DB", 26.6, "=ROUND(Psychrometrics!C17,1)", "degC", ""),
       ("B", "Cooling coil EAT WB", 23.3, "=Psychrometrics!C20", "degC", ""),
       ("B", "Cooling coil LAT DB", 12.0, "=Psychrometrics!C5", "degC", "unchanged"),
       ("B", "Cooling coil LAT WB", 11.0, "=Psychrometrics!C8", "degC", "unchanged"),
       ("B", "Chilled water flow", 26.2, "=Psychrometrics!C27", "L/s", "")]
for i, (pt, it, t, c, u, ref) in enumerate(sch, 5):
    for j, v in enumerate((pt, it, t), 1):
        wsch.cell(i, j, v).font = F_TXT
    put(wsch, i, 4, c, fmt="0.00" if "flow" in it and "water" in it else "#,##0.0" if u == "degC" else "#,##0")
    wsch.cell(i, 4).fill = FILL_KEY
    wsch.cell(i, 5, u).font = F_TXT
    wsch.cell(i, 6, ref).font = F_TXT

# ------------------------------------------------------------------ Findings
wf = wb.create_sheet("Findings", 7)
title(wf, "Supply temperature, findings and assumptions")
acc = "; ".join(f"{a['name']} {k}: mean {a['mean_T']:.0f} degC against {a['tmin']} degC" for k, a in S["accepted"].items())
short = "; ".join(f"{x['room']} {x['delivered']:.0f} of {x['required']:.0f} L/s" for x in S["oa_short_straight_sum"])
notes = [
    ("1 Supply temperature 17.6 degC", f"With 14 degC supply the VAV minimum (DD Booklet air change) is {S['vmin_pct_14']} % of the design airflow and the rooms "
     f"cannot turn down: rooms below 23 degC in every working hour. At 17.6 degC the thermal-load airflows are larger and the minimum is "
     f"{S['vmin_pct_17']} % of the design airflow. 17.6 degC = 24 - room sensible load / (1.21 x room airflow of the 14 degC sizing)."),
    ("2 Dehumidification", f"With the coil leaving at 15.4 degC the rooms exceed their RH limit in {S['ref176']['hours_humid']:.0f} % of the working hours. "
     f"The coil leaves at {S['coil_leaving']:.0f} degC (dew point {S['coil_dew_point']:.1f} degC) and the central reheat restores the supply temperature."),
    ("3 Outdoor air of each room", "Each room receives outdoor air in proportion to its supply airflow. With the intake equal to the sum of the room "
     f"outdoor air, these rooms are short at design flow: {short}. The intake is reset each hour to the room that needs the most outdoor air."),
    ("4 24 h rooms with negligible load", f"On central reheat only (Engineer's ruling); below their DD Booklet minimum temperature in most hours: {acc}."),
    ("5 Rooms above T max at peak solar load", ", ".join(f"{h_['room']} {h_['pct']:.0f} % of hours (without reheat {h_['pct_no_reheat']:.0f} %)" for h_ in S["hot_rooms"])
     + ". Not a reheat item; the glazing of AMSC 12.01.01 is under review."),
    ("6 Reduced VAV minimum in 13 rooms", "Below the DD Booklet air change during occupied hours (Engineer's ruling); the room outdoor air is maintained by the "
     "intake reset. Minimum = larger of the room outdoor air, the smallest box setting and the airflow that holds the DD Booklet minimum temperature in "
     "99.6 % of the hours (rounded down to 5 L/s)."),
    ("7 Store - Equipment AMSC 10.19.03", "Served by this AHU as on the tender air-side schematic IP-BH-06068 (Engineer's ruling D-127)."),
    ("Assumptions", "Working hours Mon-Fri 08:00-18:00; office schedules of occupancy, lighting and equipment; offices without a 24 h note off after hours; "
     "infiltration 0.5 ACH in facade rooms; no safety factor at part load; smallest VAV box setting 10 L/s; coil leaving RH 88.9 %; cooling coil duty "
     "with the return air at the room condition that results (pull-down value also given)."),
]
for i, (k, v) in enumerate(notes, 3):
    wf.cell(i, 1, k).font = F_SUB
    c = wf.cell(i, 2, v); c.font = F_TXT; c.alignment = WRAP
    wf.row_dimensions[i].height = 13 * (len(v) // 120 + 1)
wf.column_dimensions["A"].width = 32
wf.column_dimensions["B"].width = 130

for w_ in wb.worksheets:
    w_.sheet_view.showGridLines = False
    w_.page_setup.orientation = "landscape"
    w_.page_setup.fitToWidth = 1
    w_.page_setup.fitToHeight = 0
    w_.sheet_properties.pageSetUpPr.fitToPage = True
wb.properties.creator = "AIGH ACMV"
wb.properties.lastModifiedBy = "AIGH ACMV"
wb.properties.title = f"ME-0274 {TAG} calculation"
order = ["Cover", "Inputs", "Rooms", "AHU", "Psychrometrics", "Part load", "Room results", "Hourly", "Schedule", "Findings"]
wb._sheets = [wb[n] for n in order]
wb.active = 0
wb.save(OUT)


def recalc_and_neutralise(path):
    """Recalculate with the local spreadsheet engine (headless, no network) so the file carries the values, then set neutral properties."""
    import re
    import shutil
    import subprocess
    import tempfile
    import zipfile
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["soffice", "--headless", "--calc", "--convert-to", "xlsx", "--outdir", td, str(path)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300, cwd=td)
        src_ = Path(td) / Path(path).name
        tmp = Path(str(path) + ".tmp")
        with zipfile.ZipFile(src_) as zi, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zo:
            for it in zi.infolist():
                d = zi.read(it.filename)
                if it.filename == "docProps/app.xml":
                    d = re.sub(rb"<Application>.*?</Application>", b"<Application>Microsoft Excel</Application>", d)
                    d = re.sub(rb"<AppVersion>.*?</AppVersion>", b"", d)
                if it.filename == "docProps/core.xml":
                    d = re.sub(rb"<dc:creator>.*?</dc:creator>", b"<dc:creator>AIGH ACMV</dc:creator>", d)
                    d = re.sub(rb"<cp:lastModifiedBy>.*?</cp:lastModifiedBy>", b"<cp:lastModifiedBy>AIGH ACMV</cp:lastModifiedBy>", d)
                zo.writestr(it, d)
        shutil.move(tmp, path)


recalc_and_neutralise(OUT)
chk = openpyxl.load_workbook(OUT, data_only=True)
errs = [(w.title, c.coordinate) for w in chk for row in w.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith(("#", "Err:"))]
assert not errs, errs
assert abs(chk["Psychrometrics"]["C21"].value - S["coil_design"]) < 0.1 and abs(chk["AHU"]["C6"].value - S["ahu_supply"]) < 0.5
print(OUT, chk.sheetnames)
