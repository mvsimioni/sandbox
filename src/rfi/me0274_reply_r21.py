"""Reply Rev 2 to AH-C2-RCC-RFI-ME-0274 (27 Nov 2026): Rev 1 (src/rfi/me0274_reply_r20.py) with Marcus's review: part-load reheat
analysis (Council C7, D-126): AHU reheat coil omitted, terminal reheat at the VAV boxes sized to the DD Booklet range, cooling coil on the
14 degC supply basis of the room airflows; one page dedicated to the part-load analysis (page 5).

Base: the reply as re-issued on 25 Nov 2026 (6 pages: RFI form with the reply, AHU zoning, design basis, room airflows, two Equipment Schedule
pages). Pages 1-2 are kept (the reply text box on page 1 is replaced); pages 3-6 are rebuilt from D47 R20 (outdoor air by D-110 / D-111,
no occupancy factor (D-118), safety factor 0.20 (D-114), envelope measured on the elevations (D-120, D-121)).
``python3 -I src/rfi/me0274_reply_r20.py``
"""
import openpyxl
import pymupdf

BASE = ("data/uploads/rfi_me0274_20261127/unz/ME-0274 - Clarification on the AHU serving on L6 Surgery Admin Area/Reply/"
        "AH-C2-RCC-RFI-ME-0274_Beca Reply.pdf")
WB = "out/issue/DRAFT_2026-11-27_R21/workbooks/AIGH_C2_D47_Admin_Workspace_Thermal_Load_Calc_RevA.xlsx"
EQS = "data/source/Basis/5. Equipment Schedules (Corr 5).pdf"
OUT = "out/issue/RFI_ME-0274_reply_R21/AH-C2-RCC-RFI-ME-0274_Beca Reply.pdf"
PL = "out/issue/DRAFT_2026-11-27_R21/partload/partload_results.json"
TAG = "AHU-B-06-06STAFF-01"
RED = (1, 0, 0)

wb = openpyxl.load_workbook(WB, data_only=True)
ws = wb["Airflow"]
rooms = []
for r in range(7, ws.max_row + 1):
    if ws.cell(r, 4).value == TAG:
        g = lambda c: ws.cell(r, c).value or 0
        rooms.append((g(1), g(2), g(6), g(20), g(26), g(23), g(24), g(14), ws.cell(r, 42).value or "exhaust only", g(28)))
rooms.sort(key=lambda x: x[0])
tot = lambda i: sum(x[i] for x in rooms)
ah = wb["AHU"]
col = {str(ah.cell(5, c).value): c for c in range(1, ah.max_column + 1) if ah.cell(5, c).value}
row = next(r for r in range(7, 12) if ah.cell(r, 1).value == TAG)
A = lambda k: ah.cell(row, col[k]).value
psy = {str(ah.cell(22, c).value): ah.cell(24, c).value for c in range(1, ah.max_column + 1) if ah.cell(22, c).value}
assert len(rooms) == A("Rooms on this unit") and round(tot(3)) == round(A("Room supply"))
en = wb["Envelope"]
nums = {x[0] for x in rooms}
glass = sum(r_[6] for r_ in en.iter_rows(min_row=7, values_only=True) if r_[0] in nums and isinstance(r_[6], (int, float)))
wall = sum(r_[7] for r_ in en.iter_rows(min_row=7, values_only=True) if r_[0] in nums and isinstance(r_[7], (int, float)))
sf = wb["Basis of Design"]["C16"].value

SUP, OA, EX = A("AHU supply airflow"), A("AHU outdoor air = larger of the minimum of the rule and the air that leaves the rooms"), A("Exhaust")
OA_H = A("Outdoor air of the HFDS / ASHRAE 170 rooms, straight sum (the ruling rule C4)")
OA_S = A("Outdoor air of the SS 553 rooms, sum of Table 1 rate × area (the ruling rules N1 to N3)")
import json
import math
# cooling coil on the 14 degC basis of the room airflows (C7 / D-126 provisional A): supply at the rooms 14 degC, less the fan rise = coil leaving;
# coil leaving RH 88.9 % (tender schedule); room at the steady state that results; GEN-PSY relations (Magnus saturation, 101.325 kPa)
P_ATM = 101.325
ps = lambda t: 0.61094 * math.exp(17.625 * t / (243.04 + t))
Wsat = lambda t: 0.622 * ps(t) / (P_ATM - ps(t))
W_ = lambda t, rh: 0.622 * rh * ps(t) / (P_ATM - rh * ps(t))
W_wb = lambda db, wb: ((2501 - 2.326 * wb) * Wsat(wb) - 1.006 * (db - wb)) / (2501 + 1.86 * db - 4.186 * wb)
h_ = lambda t, w: 1.006 * t + w * (2501 + 1.86 * t)
def wb_of(t, w):
    lo, hi = -10.0, t
    for _ in range(60):
        m = (lo + hi) / 2
        lo, hi = (m, hi) if W_wb(t, m) < w else (lo, m)
    return m
def dp_of(w):
    a = math.log(w * P_ATM / (0.622 + w) / 0.61094)
    return 243.04 * a / (17.625 - a)
FAN = psy["Supply fan temperature rise"]
T_SUP = 14.0
T_OFF = T_SUP - FAN
W_OFF = W_(T_OFF, 0.889)
W_OA = W_wb(32, 26)
W_ROOM = W_OFF + A("Room latent") * 1000 / (3010 * tot(3))
RH_ROOM = W_ROOM * P_ATM / (0.622 + W_ROOM) / ps(24)
T_MIX = (OA * 32 + (SUP - OA) * 24) / SUP
W_MIX = (OA * W_OA + (SUP - OA) * W_ROOM) / SUP
H_MIX = h_(T_MIX, W_MIX)
COIL = 1.2 * SUP / 1000 * (H_MIX - h_(T_OFF, W_OFF))
COIL_S = 1.2 * SUP / 1000 * 1.006 * (T_MIX - T_OFF)
COIL_L = COIL - COIL_S
W_MIX60 = (OA * W_OA + (SUP - OA) * W_(24, 0.60)) / SUP
COIL60 = 1.2 * SUP / 1000 * (h_(T_MIX, W_MIX60) - h_(T_OFF, W_OFF))
WB_MIX, WB_OFF, DP_OFF = wb_of(T_MIX, W_MIX), wb_of(T_OFF, W_OFF), dp_of(W_OFF)
# terminal reheat (part-load analysis of all workbooks, src/sim/partload_reheat.py, sizing basis)
PLR = next(x for x in json.load(open(PL, encoding="utf-8")) if x["unit"] == TAG)["cases"]["sizing"]
PLS, PLROOMS = PLR["res"], PLR["rooms"]
assert abs(PLS["t_coil_degC"] - T_OFF) < 0.05
TERM = PLS["B_installed_kW"]


def n_term():
    return sum(1 for p in PLROOMS if p["installed_kW"] > 0.05)


S_SUP, S_OA, S_COIL, S_TERM = 6600, 950, 160, 60    # schedule values (rounded up)
CHW, HW = S_COIL / (4.186 * 8), S_TERM / (4.19 * 10)
assert S_SUP >= SUP and S_OA >= OA and S_COIL >= COIL and S_TERM >= TERM

P1 = ("REPLY REV 2 (27 Nov 2026). This reply supersedes the replies of 25 Nov 2026 and Rev 1.\n"
      "1. Serving AHU: AHU-B-06-06STAFF-01 (located at L6) serves the Surgery Admin Area at L6 and the rooms adjacent to the AGV Lift Lobbies (GL 13~14 / K~R). "
      "The tags AHU-B-07-06ADMIN-06 and AHU-B-07-06ADMIN-07 on the L6 and L7 ACMV layouts are superseded by AHU-B-06-06STAFF-01. Refer to the attached AHU Zoning for the served area.\n"
      "2. Operation mode: AHU-B-06-06STAFF-01 shall be Configuration D as stipulated in the contract specifications.\n"
      f"3. Airflow: sum of room supply air {tot(3):,.0f} L/s ({len(rooms)} rooms, {tot(2):,.0f} m2). Duct leakage allowance 10 % of room supply. "
      f"AHU supply air {SUP:,.0f} L/s; schedule value {S_SUP:,} L/s. Outdoor air {OA:,.0f} L/s (sum of the room outdoor air: HFDS / ASHRAE 170 rooms "
      f"{OA_H:,.0f} L/s + SS 553 Table 1 rooms {OA_S:,.0f} L/s); schedule value {S_OA:,} L/s. Dedicated exhaust {EX:,.0f} L/s (Cleaners Room and two Toilets, "
      f"single pass). Cooling coil {COIL:.1f} kW (air off coil {T_OFF:.1f} degC DB / {WB_OFF:.1f} degC WB); schedule value {S_COIL} kW. System type: VAV.\n"
      f"4. Reheat: AHU reheat coil omitted. Part load (page 5): with no reheat the rooms are out of the DD Booklet range in more than 0.4 % of the working "
      f"hours, and one central reheat cannot hold every room. Reheat coils at the VAV terminals of {n_term()} rooms, {TERM:.1f} kW in total (schedule value "
      f"{S_TERM} kW), heating water 35/25 degC.\n"
      "Pages: 3 design basis; 4 room airflows; 5 part-load analysis and terminal reheat schedule; 6-7 Equipment Schedule with the corrected values of this AHU "
      "(these replace the tender values for this AHU).\n"
      "Contractor to produce the air-side schematic shop drawing for the Engineer's review and to submit the ESP calculation.\n"
      f"5. Cost impact: there will be a cost omission from the Tender Basis. Against the tender Equipment Schedule for this AHU (supply air 9,000 L/s, cooling "
      f"600 kW, reheat 600 kW), the AHU reduces to {S_SUP:,} L/s supply air and {S_COIL} kW cooling, and the AHU reheat coil is omitted (outdoor air {S_OA:,} L/s "
      f"against 307 L/s in the tender). Terminal reheat coils ({S_TERM} kW in total) and their heating water pipework are added. Contractor to submit the net "
      "cost omission for the Engineer's review.")

BASIS = f"""Reply Rev 2 (27 Nov 2026) to AH-C2-RCC-RFI-ME-0274. Unit: AHU-B-06-06STAFF-01 (L6 ASW - Staff Area).
Supersedes the values of the reply of 25 Nov 2026 and of Rev 1.

A. DESIGN BASIS
Outdoor design condition      32 degC DB / 26 degC WB (enthalpy 80.3 kJ/kg)
Room design condition         24 degC, 60 % RH (criteria range 23-25 degC, RH max 65 %)
Room supply air               Larger of the air-change airflow of the room criteria and the
                              thermal-load airflow, rounded up to 10 L/s
Thermal-load airflow          Room sensible load / (1.21 x (room temperature - 14 degC supply))
Outdoor air                   HFDS / ASHRAE 170 rooms (column "OA scope" on page 4): outdoor
                              air change rate of the room criteria, straight sum.
                              Other rooms: SS 553:2026 Table 1 - offices and workspace
                              0.6 L/s per m2; meeting rooms 2.5 L/s per person + 0.3 L/s per m2;
                              stores below 6 m2 exempt (cl. 5.3.5).
                              Toilets: exhaust only (no outdoor air of their own).
                              No occupancy diversity is applied to outdoor air.
                              AHU outdoor air = sum of the room outdoor air, and not less than
                              the exhaust.
Safety factor on room loads   {sf:.0%}
Envelope                      Glazing and wall of each facade room measured on the building
                              elevations; U-values and shading coefficients of the ETTV report.
                              Rooms of this unit: {glass:,.1f} m2 glazing, {wall:,.1f} m2 wall
Ceiling height                3.0 m (2.7 m in one room)
Pressure regime               Neutral. Cleaners Room and two Toilets: negative, single pass,
                              dedicated exhaust
Filter                        MERV 15

B. AHU AIRFLOW
Number of rooms               {len(rooms)}
Floor area                    {tot(2):,.1f} m2
Sum of room supply air        {tot(3):,.0f} L/s
Duct leakage allowance        10 % of room supply air (allowance in the AHU supply air;
                              ductwork to be built and tested to the specification leakage class)
AHU supply air                {SUP:,.0f} L/s   (schedule value {S_SUP:,} L/s)
Outdoor air                   HFDS / ASHRAE 170 rooms   {OA_H:,.0f} L/s
                              SS 553 Table 1 rooms      {OA_S:,.0f} L/s
                              AHU outdoor air           {OA:,.0f} L/s (schedule value {S_OA:,} L/s)
Recirculated air              {SUP - OA:,.0f} L/s
Dedicated exhaust             {EX:,.0f} L/s (Cleaners Room 90 L/s, Toilets 2 x 70 L/s)
Spill / relief air            {OA - EX:,.0f} L/s (outdoor air less exhaust)

C. COILS
Room sensible load            {A('Room sensible'):.1f} kW (with the safety factor)
Room latent load              {A('Room latent'):.1f} kW
Supply fan heat               {A('Fan heat'):.1f} kW (1,700 Pa fan total pressure, 65 % fan and motor
                              efficiency); fan temperature rise {psy['Supply fan temperature rise']:.1f} K (draw-through)
Supply air at the rooms       {T_SUP:.1f} degC DB (the supply temperature of the room airflows)
Mixed air on cooling coil     {T_MIX:.1f} degC DB / {WB_MIX:.1f} degC WB ({H_MIX:.1f} kJ/kg)
Air off cooling coil          {T_OFF:.1f} degC DB / {WB_OFF:.1f} degC WB (dew point {DP_OFF:.1f} degC)
Room humidity that results    {RH_ROOM:.0%} RH at 24 degC (steady state)
Cooling coil sensible         {COIL_S:.1f} kW
Cooling coil latent           {COIL_L:.1f} kW
Cooling coil total            {COIL:.1f} kW  (schedule value {S_COIL} kW), with the return air
                              at the room condition that results. With the return air at
                              24 degC / 60 % RH (pull-down): {COIL60:.1f} kW
Chilled water                 8 degC entering / 16 degC leaving; {COIL / (4.186 * 8):.2f} L/s (schedule value {CHW:.2f} L/s)
AHU reheat coil               Omitted (page 5: one central reheat cannot hold every room)
Terminal reheat coils         At the VAV terminals of {n_term()} rooms, {TERM:.1f} kW in total
                              (schedule value {S_TERM} kW); schedule on page 5
Reheat water                  35 degC entering / 25 degC leaving; {HW:.2f} L/s in total

D. NOTES
1. System type VAV. Each room airflow on page 4 is the design (maximum) supply air; "Min" is
   the VAV minimum (air-change rate of the room criteria).
2. The external resistance in the equipment schedule (900 Pa) is provisional. Contractor to submit
   the external static pressure calculation. Fan absorbed power and motor power to follow that
   calculation.
3. Room names and room numbers are those of the Schedule of Accommodation.
4. The served area is shown on the attached AHU Zoning (page 2).
5. Pages 6 and 7: Equipment Schedule (Corrigendum 5), Air Handling Unit Schedule Part A and
   Part B, with the corrected values of AHU-B-06-06STAFF-01 in red. Values not marked are unchanged.
6. Changes from the reply of 25 Nov 2026: {len(rooms)} rooms (44); outdoor air as the sum of the room
   outdoor air (was the highest room outdoor-air fraction x AHU supply, 2,225 L/s); exhaust of the
   two Toilets added; safety factor 20 % (10 %); envelope measured on the elevations; lift shafts
   excluded from the AGV Lift Lobbies and Multipurpose Room - Admin at its DPA area (8.85 m2).
7. Changes from Rev 1: part-load reheat analysis added (page 5); AHU reheat coil (70 kW) omitted;
   terminal reheat {S_TERM} kW added; cooling coil on the 14 degC supply of the room airflows:
   air off coil {T_OFF:.1f} degC (was 15.4), cooling {S_COIL} kW (was 130), chilled water {CHW:.2f} L/s (was 3.88).
"""

PART_A = [("9000", f"{S_SUP}"), ("600", f"{S_COIL}"), ("600.0", "NIL"), ("307", f"{S_OA}")]
PART_B = [("9000.0", "NIL"), ("12.0", "NIL"), ("24", "NIL"), ("60", "NIL"), ("35", "NIL"), ("25", "NIL"), ("19.6", "NIL"), ("30", "NIL"), ("26.6", f"{T_MIX:.1f}"), ("23.3", f"{WB_MIX:.1f}"),
          ("12.0", f"{T_OFF:.1f}"), ("11.0", f"{WB_OFF:.1f}"), ("26.2", f"{CHW:.2f}")]


def box(page, rect, text, size, font="helv", fill=None, align=0):
    a = page.add_freetext_annot(rect, text, fontsize=size, fontname=font, text_color=RED, fill_color=fill, align=align)
    a.set_info(title="Beca", subject="Text Box")
    a.update()
    return a


def text_page(d, title, text, size):
    q = d.new_page(width=595, height=842)
    box(q, pymupdf.Rect(40, 36, 555, 60), title, 10, "hebo")
    box(q, pymupdf.Rect(40, 66, 555, 815), text, size, "cour")


def schedule_page(d, src, pno, changes, title):
    """Only the column headings and the row of this AHU: the other rows and the sheet notes are removed (redacted) from a working copy,
    and the two bands are placed on a new page; the corrected values are text boxes over the printed cells."""
    tmp = pymupdf.open()
    tmp.insert_pdf(src, from_page=pno, to_page=pno)
    p = tmp[0]
    W, H = p.rect.width, p.rect.height
    hit = p.search_for(TAG)[0]
    ys = sorted({round(w[1], 1) for w in p.get_text("words") if w[4].startswith(("AHU-", "PAHU-", "FCU-"))})
    first = ys[0]
    hdr_b = first - 1.5
    row = pymupdf.Rect(0, hit.y0 - 2.2, W, hit.y1 + 2.2)
    for r in (pymupdf.Rect(0, hdr_b, W, row.y0), pymupdf.Rect(0, row.y1, W, H)):
        p.add_redact_annot(r, fill=(1, 1, 1))
    p.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_REMOVE, graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED)
    top = 40
    q = d.new_page(width=W, height=top + hdr_b + row.height + 30)
    q.show_pdf_page(pymupdf.Rect(0, top, W, top + hdr_b), tmp, 0, clip=pymupdf.Rect(0, 0, W, hdr_b))
    q.show_pdf_page(pymupdf.Rect(0, top + hdr_b, W, top + hdr_b + row.height), tmp, 0, clip=row)
    dy = top + hdr_b - row.y0
    yc = (hit.y0 + hit.y1) / 2
    words = sorted((w for w in p.get_text("words") if abs((w[1] + w[3]) / 2 - yc) < 3), key=lambda w: w[0])
    i = 0
    for old, new in changes:
        while words[i][4] != old:
            i += 1
        w = words[i]
        i += 1
        r = pymupdf.Rect(w[0] - 2.5, w[1] + dy, w[2] + 2.5, w[3] + 2 + dy)
        box(q, r, new, max(4, (w[3] - w[1]) * 0.82), fill=(1, 1, 1), align=1)
    sq = q.add_rect_annot(pymupdf.Rect(words[0][0] - 3, hit.y0 - 2 + dy, words[-1][2] + 3, hit.y1 + 2 + dy))
    sq.set_colors(stroke=RED); sq.set_border(width=0.6); sq.set_info(title="Beca"); sq.update()
    box(q, pymupdf.Rect(40, 10, W - 40, 32), title, 9, "hebo")


def partload_text():
    pct = lambda x: f"{100 * x:.1f} %"
    n_out = sum(1 for p in PLROOMS if p["criteria"] and p["frac_out_no_reheat"] >= 0.004)
    n_rh = sum(1 for p in PLROOMS if p["criteria"] and p["frac_rh_no_reheat"] >= 0.004)
    n_crit = sum(1 for p in PLROOMS if p["criteria"])
    hot = [p["room"] for p in PLROOMS if p["frac_hot_with_terminal"]]
    t = f"""A. QUESTION AND TEST
Is a reheat coil required at part load, and what size holds the DD Booklet range? Test: the hours in which a room is below the minimum
temperature or above the RH limit of its DD Booklet criteria with no reheat, as a fraction of the hours it operates (working hours;
all hours for 24 h rooms). Below 0.4 %: reheat omitted. Otherwise reheat is sized to hold the DD Booklet range.

B. METHOD
Hourly, one year (Changi typical meteorological year, 8,760 h). Room loads without the safety factor: glazing and wall by facade
orientation with the hourly solar radiation, infiltration of facade rooms 0.5 ACH, people, lighting and small power on office
schedules in the working hours. VAV rooms between the VAV minimum (page 4) and the design supply; fixed-airflow rooms at design.
Air off the cooling coil {T_OFF:.1f} degC at 88.9 % RH; supply fan rise {FAN:.1f} K x (flow / design flow)^2; outdoor air {OA:,.0f} L/s.
Assumptions: working hours Mon-Fri 08:00-18:00 (the DD Booklet gives none); occupancy, lighting and equipment profiles of an office;
AGV Lift Lobbies, Bays and DSOT rooms 24 h. Rooms with no DD Booklet criteria (Cleaners Room, two Toilets) are not tested.

C. RESULT WITHOUT REHEAT
VAV minimum airflow {PLS['vmin_sum']:,} L/s = {PLS['vmin_sum'] / PLS['vmax_sum']:.0%} of the design room supply ({PLS['vmax_sum']:,} L/s): the air-change rates of the room
criteria govern the minimum, so the rooms are overcooled at part load.
Hours with at least one room out of range: {pct(PLS['frac_out_of_range_no_reheat'])} of the operating hours (RH limit alone: {pct(PLS['frac_rh_only'])}).
Rooms out of range in 0.4 % or more of their hours: {n_out} of {n_crit} (RH limit alone: {n_rh} of {n_crit}).
Result: above the 0.4 % threshold. Reheat cannot be omitted.

D. ARRANGEMENT
Option A, central reheat on the AHU (one supply temperature each hour): peak {PLS['A_central_peak_kW']:.1f} kW, rooms still out of range in
{pct(PLS['A_frac_out_of_range'])} of the hours (the rooms have different loads at the same time). Not adopted; AHU reheat coil omitted.
Option B, reheat at each VAV terminal to the higher of the DD Booklet minimum and the temperature that holds the RH limit: rooms
cold or humid in {pct(PLS['B_frac_cold_or_humid'])} of the hours. Adopted.
Remaining hours above the maximum temperature ({', '.join(hot)})
occur at peak solar load with no reheat: a cooling airflow item, under review; it does not change the reheat.

E. TERMINAL REHEAT SIZE
Each terminal coil: VAV minimum airflow at zero internal load, warmed from the supply at the boxes at minimum flow ({PLS['ts_boxes_at_min_flow_degC']:.1f} degC) to the
reheat target (the higher of the DD Booklet minimum and the RH-limit temperature): q = 1.21 x Vmin x (target - {PLS['ts_boxes_at_min_flow_degC']:.1f}) / 1000 kW.
This size does not depend on the schedules. Coincident peak of the year: {PLS['B_terminal_peak_kW']:.1f} kW (heating water diversity).

{'Room number':<18}{'Room name':<27}{'Vmin':>6}{'Design':>7}{'Tmin':>6}{'RH max':>7}{'Target':>7}{'Reheat':>8}
{'':<18}{'':<27}{'L/s':>6}{'L/s':>7}{'degC':>6}{'%':>7}{'degC':>7}{'kW':>8}
""" + "-" * 86 + "\n"
    for p in sorted((p for p in PLROOMS if p["installed_kW"] > 0.05), key=lambda p: p["room"]):
        t += f"{p['room']:<18}{p['name'][:26]:<27}{p['vmin']:>6.0f}{p['vmax']:>7.0f}{p['tmin']:>6.0f}{100 * p['rhmax']:>7.0f}{p['target_degC']:>7.1f}{p['installed_kW']:>8.2f}\n"
    t += "-" * 86 + f"\n{'TOTAL (' + str(n_term()) + ' terminals)':<79}{TERM:>7.1f}\n"
    t += f"Schedule value {S_TERM} kW. Reheat water 35 / 25 degC, {HW:.2f} L/s in total. Each terminal coil to be selected for its VAV minimum airflow.\n"
    return t


ZONING = "out/issue/DRAFT_2026-11-27_R21/zoning/AIGH_IP_AHU_Zoning_L06.pdf"


def zoning_page(d, pno):
    """AHU zoning page: the 6th storey AHU zoning sheet (R21) with only the layer of AHU-B-06-06STAFF-01 on (the zones, labels and legend of the
    other units off), without the room notes, cropped to the served area (GL 13~18 / K~S) and placed as an image: nothing of the other areas
    is in the file. Replaces page pno."""
    z = pymupdf.open(ZONING)
    ocg = z.get_ocgs()
    on = [x for x, v in ocg.items() if v["name"] == TAG]
    z.set_layer(-1, on=on, off=[x for x in ocg if x not in on])
    pg = z[1]
    # the zones and their labels are annotations on the unit layers: keep only those of this AHU (the room notes go too)
    for a in list(pg.annots()):
        if a.get_oc() not in on:
            pg.delete_annot(a)
    keep = pymupdf.Rect(1276, 504, 2252, 1678)          # display coordinates (pt) of the served area with a margin
    pix = pg.get_pixmap(dpi=200, clip=keep, annots=True)       # the zones are annotations on the unit layers
    top = 50
    q = d.new_page(pno=pno + 1, width=keep.width, height=keep.height + top)
    q.insert_image(pymupdf.Rect(0, top, keep.width, keep.height + top), pixmap=pix)
    box(q, pymupdf.Rect(20, 12, keep.width - 20, 44),
        "AHU zoning, 6th storey: area served by AHU-B-06-06STAFF-01 (shaded blue): Surgery Admin Area and rooms adjacent to the AGV Lift Lobbies "
        "(lift shafts excluded)", 11, "hebo")
    d.delete_page(pno)


d = pymupdf.open(BASE)
d.delete_pages(list(range(2, len(d))))
zoning_page(d, 1)
pg = d[0]
# the base is flattened: the reply text is page text; it is removed (text only, the form lines stay) and the new reply is a text box
blk = [b for b in pg.get_text("blocks") if "Serving AHU" in b[4]]
assert len(blk) == 1, "reply text on page 1 not found"
rect = pymupdf.Rect(blk[0][:4])
pg.add_redact_annot(pymupdf.Rect(rect.x0 - 1, rect.y0 - 1, rect.x1 + 1, rect.y1 + 1))
pg.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
box(pg, pymupdf.Rect(rect.x0 - 4, rect.y0 - 8, rect.x1 + 6, rect.y1 + 8), P1, 5.0)
text_page(d, "ME-0274 Reply Rev 2 - Page 3: Design basis and AHU airflow, AHU-B-06-06STAFF-01", BASIS, 6.6)
hdr = (f"{'Room number':<18}{'Room name':<27}{'Area':>6}{'Supply':>7}{'Min':>6}{'OA':>6}{'Exh':>5}{'Return':>7}  {'Governing':<13}OA scope\n"
       f"{'':<18}{'':<27}{'m2':>6}{'L/s':>7}{'L/s':>6}{'L/s':>6}{'L/s':>5}{'L/s':>7}\n" + "-" * 105 + "\n")
body = "".join(f"{n:<18}{nm[:26]:<27}{a:>6.1f}{s:>7.0f}{vm:>6.0f}{oa:>6.1f}{ex:>5.0f}{rt:>7.0f}  {'air change' if gov == 'AIR CHANGE' else 'thermal load':<13}{sc}\n"
               for n, nm, a, s, oa, ex, rt, gov, sc, vm in rooms)
foot = ("-" * 105 + f"\n{'TOTAL (' + str(len(rooms)) + ' rooms)':<45}{tot(2):>6.1f}{tot(3):>7.0f}{tot(9):>6.0f}{tot(4):>6.0f}{tot(5):>5.0f}{tot(6):>7.0f}\n\n"
        f"Duct leakage allowance 10 %: AHU supply air = {tot(3):,.0f} x 1.10 = {SUP:,.0f} L/s (schedule value {S_SUP:,} L/s).\n"
        f"OA = outdoor air of the room (HFDS / ASHRAE 170 air change rate, or SS 553 Table 1). AHU outdoor air = {OA:,.0f} L/s (schedule value {S_OA:,} L/s).\n"
        "Supply = design (maximum) room supply air. Min = VAV minimum (air-change rate of the room criteria); fixed-airflow rooms: Min = Supply.\n")
text_page(d, "ME-0274 Reply Rev 2 - Page 4: Room airflow schedule, AHU-B-06-06STAFF-01", hdr + body + foot, 6.2)
text_page(d, "ME-0274 Reply Rev 2 - Page 5: Part-load analysis of the reheat requirement, AHU-B-06-06STAFF-01", partload_text(), 5.6)
src = pymupdf.open(EQS)
schedule_page(d, src, 24, PART_A, "ME-0274 Reply Rev 2 - Page 6: Air Handling Unit Schedule Part A. Corrected values of AHU-B-06-06STAFF-01 in red (reheating: AHU reheat coil omitted, terminal reheat on page 5)")
schedule_page(d, src, 25, PART_B, "ME-0274 Reply Rev 2 - Page 7: Air Handling Unit Schedule Part B. Corrected values of AHU-B-06-06STAFF-01 in red (re-heating coil omitted)")
d.set_metadata({**d.metadata, "producer": "", "creator": ""})
import os
os.makedirs(os.path.dirname(OUT), exist_ok=True)
d.save(OUT, garbage=3, deflate=True)
print(len(pymupdf.open(OUT)), "pages", OUT)
