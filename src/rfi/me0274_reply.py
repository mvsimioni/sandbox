"""Reply to AH-C2-RCC-RFI-ME-0274 (27 Nov 2026), AHU-B-06-06STAFF-01, on the part-load scheme ruled by Marcus (D-126).

Values from src/rfi/me0274_scheme.py (scheme.json) and the R21 D47 workbook. Pages: 1 RFI form with the reply; 2 AHU zoning; 3 design basis,
AHU airflow and coils; 4 room airflow schedule; 5 part-load condition; 6 supply temperature and findings; 7-8 Equipment Schedule Part A / B.
``python3 -I src/rfi/me0274_reply.py``
"""
import json
import os

import openpyxl
import pymupdf

BASE = ("data/uploads/rfi_me0274_20261127/unz/ME-0274 - Clarification on the AHU serving on L6 Surgery Admin Area/Reply/"
        "AH-C2-RCC-RFI-ME-0274_Beca Reply.pdf")
WB = "out/issue/DRAFT_2026-11-27_R21/workbooks/AIGH_C2_D47_Admin_Workspace_Thermal_Load_Calc_RevA.xlsx"
EQS = "data/source/Basis/5. Equipment Schedules (Corr 5).pdf"
SCHEME = "out/issue/RFI_ME-0274_reply/scheme.json"
OUT = "out/issue/RFI_ME-0274_reply/AH-C2-RCC-RFI-ME-0274_Beca Reply.pdf"
ACC_TXT = "out/issue/RFI_ME-0274_reply/ME-0274_ACC_response.txt"
TAG = "AHU-B-06-06STAFF-01"
RED = (1, 0, 0)

S = json.load(open(SCHEME, encoding="utf-8"))
R = S["rooms"]
wb = openpyxl.load_workbook(WB, data_only=True)
ah = wb["AHU"]
col = {str(ah.cell(5, c).value): c for c in range(1, ah.max_column + 1) if ah.cell(5, c).value}
row = next(r for r in range(7, 12) if ah.cell(r, 1).value == TAG)
A = lambda k: ah.cell(row, col[k]).value
en = wb["Envelope"]
nums = {x["room"] for x in R}
glass = sum(r_[6] for r_ in en.iter_rows(min_row=7, values_only=True) if r_[0] in nums and isinstance(r_[6], (int, float)))
wall = sum(r_[7] for r_ in en.iter_rows(min_row=7, values_only=True) if r_[0] in nums and isinstance(r_[7], (int, float)))
sf = wb["Basis of Design"]["C16"].value
OA_H = A("Outdoor air of the HFDS / ASHRAE 170 rooms, straight sum (the ruling rule C4)")
OA_S = A("Outdoor air of the SS 553 rooms, sum of Table 1 rate × area (the ruling rules N1 to N3)")
assert len(R) == A("Rooms on this unit")

area = sum(x["area"] for x in R)
SUP, ROOMS = S["ahu_supply"], S["room_supply"]
S_SUP, S_COIL, S_RH, S_OAMIN, S_OAMAX = 7700, 240, 60, 950, 2600       # schedule values (rounded up)
CHW, HW = S_COIL / (4.186 * 8), S_RH / (4.19 * 10)
assert S_SUP >= SUP and S_COIL >= S["coil_design"] and S_RH >= S["central_peak"] and S_OAMAX >= S["oa_work_max"] and S_OAMIN >= S["oa_sum_rooms"]
FAN_KW = 1.21 * SUP * S["fan_rise"] / 1000
R13 = [x for x in R if x["vmin_basis"].startswith("28")]
F = S["final"]; R14 = S["ref14"]; R17 = S["ref176"]
pct = lambda a, b: f"{100 * a / b:.0f} %"
acc_names = "AGV Lift Lobbies (2), AGV Bays (2) and Clean Utility"

P1 = ("1. Serving AHU: AHU-B-06-06STAFF-01 (located at L6) serves the Surgery Admin Area at L6 and the rooms adjacent to the AGV Lift Lobbies (GL 13~14 / K~R). "
      "The tags AHU-B-07-06ADMIN-06 and AHU-B-07-06ADMIN-07 on the L6 and L7 ACMV layouts are superseded by AHU-B-06-06STAFF-01. Refer to the attached AHU Zoning.\n"
      "2. Operation mode: AHU-B-06-06STAFF-01 shall be Configuration D as stipulated in the contract specifications.\n"
      f"3. Airflow and coils: {len(R)} rooms, {area:,.0f} m2; sum of room supply air {ROOMS:,} L/s at 17.6 degC supply; AHU supply air {SUP:,} L/s incl. 10 % duct leakage "
      f"(schedule {S_SUP:,} L/s). System type VAV. Outdoor air reset between {S_OAMIN:,} and {S_OAMAX:,} L/s to the room that needs the most outdoor air. "
      f"Cooling coil air off {S['coil_leaving']:.1f} degC DB / {S['coil_leaving_wb']:.1f} degC WB, {S['coil_design']:.1f} kW (schedule {S_COIL} kW). Central reheat coil on the AHU "
      f"{S['central_peak']:.1f} kW (schedule {S_RH} kW); no terminal reheat.\n"
      "4. Part load: the AHU supply temperature, the reduced VAV minimum airflows of 13 rooms and the outdoor-air reset are explained on pages 5 and 6.\n"
      "Pages: 3 design basis; 4 room airflows; 5 part-load condition; 6 supply temperature and findings; 7-8 Equipment Schedule with the corrected values of this AHU "
      "(these replace the tender values for this AHU). Contractor to produce the air-side schematic shop drawing for the Engineer's review and to submit the ESP calculation.\n"
      f"5. Cost impact: there will be a cost omission from the Tender Basis. Against the tender Equipment Schedule for this AHU (supply air 9,000 L/s, cooling 600 kW, "
      f"reheat 600 kW), the AHU reduces to {S_SUP:,} L/s supply air, {S_COIL} kW cooling and {S_RH} kW reheat. The outdoor air increases to {S_OAMIN:,}-{S_OAMAX:,} L/s "
      "(tender 307 L/s), with an outdoor-airflow measuring station and reset control. Contractor to submit the net cost omission for the Engineer's review.")

BASIS = f"""AH-C2-RCC-RFI-ME-0274 reply (27 Nov 2026). Unit: AHU-B-06-06STAFF-01 (L6 ASW - Staff Area).

A. DESIGN BASIS
Outdoor design condition      32 degC DB / 26 degC WB (enthalpy 80.3 kJ/kg)
Room design condition         24 degC, 60 % RH; DD Booklet range of each room (23-25 degC,
                              RH max 65 % for the offices)
Supply air at the rooms       17.6 degC DB (page 6)
Room supply air               Larger of the air-change airflow of the room criteria and the
                              thermal-load airflow, rounded up to 10 L/s
Thermal-load airflow          Room sensible load / (1.21 x (room temperature - 17.6 degC))
VAV minimum airflow           Air-change airflow of the room criteria, except 13 rooms with a
                              reduced minimum (page 4, page 5)
Outdoor air of the rooms      HFDS / ASHRAE 170 rooms (column "OA scope" on page 4): outdoor
                              air change rate of the room criteria. Other rooms: SS 553:2026
                              Table 1 - offices and workspace 0.6 L/s per m2; meeting rooms
                              2.5 L/s per person + 0.3 L/s per m2; stores below 6 m2 exempt
                              (cl. 5.3.5). Toilets: exhaust only. No occupancy diversity.
Safety factor on room loads   {sf:.0%}
Envelope                      Glazing and wall of each facade room measured on the building
                              elevations; U-values and shading coefficients of the ETTV report.
                              Rooms of this unit: {glass:,.1f} m2 glazing, {wall:,.1f} m2 wall
Ceiling height                3.0 m (2.7 m in one room)
Pressure regime               Neutral. Cleaners Room and two Toilets: negative, single pass,
                              dedicated exhaust
Filter                        MERV 15

B. AHU AIRFLOW
Number of rooms               {len(R)}
Floor area                    {area:,.1f} m2
Sum of room supply air        {ROOMS:,} L/s (design)
Sum of VAV minimum airflow    {S['vmin_sum']:,} L/s
Duct leakage allowance        10 % of room supply air (allowance in the AHU supply air;
                              ductwork to be built and tested to the specification leakage class)
AHU supply air                {SUP:,} L/s   (schedule value {S_SUP:,} L/s)
Outdoor air of the rooms      HFDS / ASHRAE 170 rooms {OA_H:,.0f} L/s + SS 553 rooms {OA_S:,.0f} L/s
                              = {S['oa_sum_rooms']:,.0f} L/s
AHU outdoor-air intake        Reset each hour so that every room receives its outdoor air:
                              intake = largest of (room outdoor air x AHU supply / room supply).
                              Working hours {S['oa_work_min']:,}-{S['oa_work_max']:,} L/s (mean {S['oa_work_mean']:,}); after hours
                              (24 h rooms) up to {S['oa_after_max']:,} L/s; at design flow {S['oa_design']:,} L/s.
                              Never below {S['oa_sum_rooms']:,.0f} L/s. Schedule: min {S_OAMIN:,}, max {S_OAMAX:,} L/s.
Dedicated exhaust             {S['exhaust']:,} L/s (Cleaners Room 90 L/s, Toilets 2 x 70 L/s)
Spill / relief air            Outdoor-air intake less exhaust: up to {S_OAMAX - S['exhaust']:,} L/s

C. COILS
Room sensible load            {S['room_sensible']:.1f} kW (with the safety factor)
Room latent load              {S['room_latent']:.1f} kW
Supply fan heat               {FAN_KW:.1f} kW (1,700 Pa fan total pressure, 65 % fan and motor
                              efficiency); fan temperature rise {S['fan_rise']:.1f} K (draw-through)
Mixed air on cooling coil     {S['mix_db']:.1f} degC DB / {S['mix_wb']:.1f} degC WB ({S['mix_h']:.1f} kJ/kg), outdoor air {S['oa_design_intake']:,} L/s
Air off cooling coil          {S['coil_leaving']:.1f} degC DB / {S['coil_leaving_wb']:.1f} degC WB (dew point {S['coil_dew_point']:.1f} degC)
Cooling coil                  {S['coil_design']:.1f} kW total, {S['coil_design_sensible']:.1f} kW sensible (schedule value {S_COIL} kW), with
                              the return air at the room condition that results ({S['room_rh_design']:.0%} RH).
                              Return air at 24 degC / 60 % RH (pull-down): {S['coil_design_24_60']:.1f} kW
Chilled water                 8 degC entering / 16 degC leaving; {CHW:.2f} L/s
Central reheat coil (AHU)     Air from {S['coil_leaving']:.1f} degC to {S['reheat_to']:.1f} degC ({S['supply_at_rooms']:.1f} degC at the rooms after fan heat):
                              {S['central_base_design']:.1f} kW at design flow; with the part-load reset (page 5) peak
                              {S['central_peak']:.1f} kW (schedule value {S_RH} kW)
Reheat water                  35 degC entering / 25 degC leaving; {HW:.2f} L/s
Terminal reheat               None

D. NOTES
1. System type VAV. Room airflows on page 4: design (maximum) and VAV minimum.
2. External resistance in the equipment schedule (900 Pa) is provisional. Contractor to submit
   the ESP calculation; fan absorbed power and motor power to follow that calculation.
3. Room names and numbers of the Schedule of Accommodation. AGV Lift Lobbies and Multipurpose
   Room - Admin at their DPA areas; lift shafts excluded.
4. Outdoor-airflow measuring station on the AHU intake and reset control by the BMS (page 6).
"""

MIN_TAG = {"DD Booklet air change": "6 ACH", "fixed": "fixed"}
hdr = (f"{'Room number':<18}{'Room name':<25}{'Area':>6}{'Design':>7}{'Min':>6}{'Min':>6}{'Min of':>7}{'OA':>6}{'Exh':>5}{'Return':>7}  {'Governing':<13}OA scope\n"
       f"{'':<18}{'':<25}{'m2':>6}{'L/s':>7}{'L/s':>6}{'basis':>6}{'6 ACH':>7}{'L/s':>6}{'L/s':>5}{'L/s':>7}\n" + "-" * 110 + "\n")
body = ""
for x in sorted(R, key=lambda x: x["room"]):
    tag = MIN_TAG.get(x["vmin_basis"], "red.")
    p6 = f"{100 * x['vmin'] / x['ach6']:.0f}%" if tag == "red." else ""
    body += (f"{x['room']:<18}{x['name'][:24]:<25}{x['area']:>6.1f}{x['design']:>7.0f}{x['vmin']:>6.0f}{tag:>6}{p6:>7}{x['oa']:>6.1f}{x['exh']:>5.0f}{x['ret']:>7.0f}  "
             f"{x['gov']:<13}{x['scope']}\n")
foot = ("-" * 110 + f"\n{'TOTAL (' + str(len(R)) + ' rooms)':<43}{area:>6.1f}{ROOMS:>7,}{S['vmin_sum']:>6,}{'':>13}{sum(x['oa'] for x in R):>6.0f}"
        f"{S['exhaust']:>5}{sum(x['ret'] for x in R):>7,.0f}\n\n"
        f"Design = design (maximum) room supply air at 17.6 degC supply. AHU supply air = {ROOMS:,} x 1.10 = {SUP:,} L/s (schedule value {S_SUP:,} L/s).\n"
        "Min = VAV minimum airflow. Basis: 6 ACH = air-change airflow of the room criteria; red. = reduced minimum, the airflow that holds the DD\n"
        "Booklet minimum temperature (page 5), with its share of the air-change airflow; fixed = constant airflow (exhaust / single pass).\n"
        f"13 rooms with a reduced minimum: {S['r13_vmin']:,} L/s against {S['r13_ach6']:,} L/s air-change airflow ({pct(S['r13_vmin'], S['r13_ach6'])}). "
        "10 L/s is the smallest VAV box setting.\n"
        "OA = outdoor air the room must receive (HFDS / ASHRAE 170 air change rate, or SS 553 Table 1); the AHU intake is reset to deliver it.\n")

hot = ", ".join(f"{h['room']} {h['pct']:.0f} % (without reheat {h['pct_no_reheat']:.0f} %)" for h in S["hot_rooms"][:3])
PART = f"""A. PART-LOAD CONDITION
At part load the room loads fall well below the design loads (no safety factor; people, lighting and small power follow office
schedules; solar and outdoor temperature by the hour). A VAV room then reduces its airflow to its minimum. Where the minimum airflow
at the supply temperature cools more than the room load, the room falls below the minimum temperature of its DD Booklet range, and
the humidity of the room depends on the moisture of the supply air.
Test applied: hours in which a room is below its DD Booklet minimum temperature or above its RH limit, as a share of the hours it
operates. Below 0.4 % the arrangement is accepted.

B. METHOD
Hourly over one year (Changi typical meteorological year, 8,760 h; {S['hours_work']:,} working hours). Room loads without the safety
factor: glazing and wall by facade orientation with the hourly solar radiation, infiltration of facade rooms 0.5 ACH, people,
lighting and small power on office schedules. Offices operate Mon-Fri 08:00-18:00 and are off after hours; rooms noted 24 h
(AGV Lift Lobbies, AGV Bays, Clean Utility, DSOT 11.06.04 and 11.27.01) operate all hours. Supply fan rise {S['fan_rise']:.1f} K x (flow / design flow)^2.
Rooms with no DD Booklet criteria (Cleaners Room, two Toilets) are not tested.

C. RESULTS (working hours with at least one room out of range; average number of rooms below the minimum temperature)
                                                          Cold or humid    Rooms cold    RH alone
1 Supply 14 degC at rooms, VAV minimum 6 ACH ({R14['vmin_pct']} % of design)    {R14['hours_cold_or_humid']:6.1f} %       {R14['rooms_cold_mean']:6.1f}     {R14['hours_humid']:6.1f} %
  - with one central reheat temperature: cold/humid {R14['central_cold_or_humid']:.0f} %, but rooms above 25 degC in {R14['central_hot']:.0f} % of hours
2 Supply 17.6 degC, airflows re-sized, coil 15.4 degC         {R17['hours_cold_or_humid']:6.1f} %       {R17['rooms_cold_mean']:6.1f}     {R17['hours_humid']:6.1f} %
3 Supply 17.6 degC, coil {S['coil_leaving']:.0f} degC + central reheat, reduced min. {F['hours_cold_or_humid']:6.1f} %       {F['rooms_cold_mean']:6.1f}     {F['hours_humid']:6.1f} %
  in 13 rooms, outdoor-air reset, no reheat reset
  - with the central reheat reset (adopted):    cold or humid {F['central_cold_or_humid']:.1f} % of working hours

D. ADOPTED ARRANGEMENT
- Cooling coil leaves at {S['coil_leaving']:.1f} degC (dew point {S['coil_dew_point']:.1f} degC): the supply air is dry enough for every room RH limit (worst
  room above its limit in {F['hours_humid']:.1f} % of the hours).
- Central reheat on the AHU raises the air to {S['reheat_to']:.1f} degC ({S['supply_at_rooms']:.1f} degC at the rooms). In {S['reset_hours_pct']} % of the working hours the
  BMS resets the reheat higher (mean +{S['reset_mean']:.1f} K, up to +{S['reset_max']:.1f} K) when a room at its minimum airflow approaches its
  minimum temperature. Central reheat {S['central_base_design']:.1f} kW at design flow; peak of the year {S['central_peak']:.1f} kW; mean in working
  hours {S['central_mean_work']:.1f} kW. Schedule value {S_RH} kW.
- 13 rooms (open office ANWSIP6 Cl.06.01, offices WS.06.07, WS.06.11-16, WS.06.24, meeting rooms MR.06.01-03, storeroom AM.06.01)
  have a reduced VAV minimum: {S['r13_vmin']:,} L/s, {pct(S['r13_vmin'], S['r13_ach6'])} of their air-change airflow (20-33 % by room, page 4). This is the
  airflow that holds the DD Booklet minimum temperature in 99.6 % of their hours. The other rooms keep the air-change minimum.
- Outdoor-air intake reset each hour ({S['oa_work_min']:,}-{S['oa_work_max']:,} L/s in working hours) so that each room receives its outdoor air at
  its actual airflow (page 6, item 3).
- Remaining hours above the maximum temperature: {hot}. These occur at peak solar
  load with or without reheat (page 6, item 5).
- {acc_names}: central reheat only (page 6, item 4).
"""

acc = S["accepted"]
acc_txt = "; ".join(f"{a['name']} {k} mean {a['mean_T']:.0f} degC against {a['tmin']} degC" for k, a in acc.items())
short = ", ".join(f"{x['room']} {x['delivered']:.0f} of {x['required']:.0f} L/s" for x in S["oa_short_straight_sum"] if x["room"].startswith(("ANWSIP6 MR", "ANWSIP6 Cl", "LL", "DSOT 11.06")))
NOTES = f"""1. SUPPLY TEMPERATURE 17.6 degC AT THE ROOMS
- The DD Booklet air change (6 ACH) sets the VAV minimum of most rooms. With 14 degC supply the room airflows sized on the
  thermal load are hardly larger than the air-change airflow: the VAV minimum is {R14['vmin_pct']} % of the design airflow and the rooms
  cannot turn down. At part load {R14['rooms_cold_mean']:.0f} rooms on average are below 23 degC in every working hour (page 5, case 1).
- At 17.6 degC the thermal-load airflows are larger ({ROOMS:,} L/s against {R14['rooms']:,} L/s at 14 degC) and the air-change minimum
  becomes {S['vmin_pct_17']} % of the design airflow: the rooms have a turn-down range and follow their load.
- 17.6 degC is the supply temperature that the room sensible load of the unit requires at the room airflows set by the air-change
  rates and the 14 degC thermal sizing (24 degC - {S['room_sensible']:.1f} kW / (1.21 x {R14['rooms']:,} L/s)). The room design airflows on page 4
  are then re-sized on 17.6 degC, so every room holds 24 degC at full load.
2. DEHUMIDIFICATION SEPARATED FROM THE SUPPLY TEMPERATURE
- With the coil leaving at 15.4 degC (dew point 13.6 degC) the rooms are above their RH limit in {R17['hours_humid']:.0f} % of the working hours
  (open office above 65 % RH; the DSOT rooms above their 60 % limit).
- The coil therefore leaves at {S['coil_leaving']:.0f} degC (dew point {S['coil_dew_point']:.1f} degC) and the central reheat coil raises the air to {S['reheat_to']:.1f} degC.
  Cooling coil {S['coil_design']:.0f} kW at design (schedule {S_COIL} kW); central reheat {S['central_base_design']:.0f} kW at design flow (schedule {S_RH} kW).
3. OUTDOOR AIR OF EACH ROOM
- The AHU supplies one mixture to all rooms: each room receives the outdoor air in proportion to its supply airflow. With the
  intake equal to the sum of the room outdoor air ({S['oa_sum_rooms']:,.0f} L/s), rooms with a high outdoor-air share receive less than
  required, already at design flow: {short}.
- At part load, when rooms reduce their airflow, the shortfall grows. The intake is therefore reset each hour to the room that needs
  the most outdoor air: {S['oa_work_min']:,}-{S['oa_work_max']:,} L/s in working hours, {S['oa_design']:,} L/s at design flow (critical room {S['oa_design_critical']}).
- Requires an outdoor-airflow measuring station on the intake, a modulating outdoor-air damper and the reset in the BMS sequence;
  relief air up to {S_OAMAX - S['exhaust']:,} L/s.
4. 24 h ROOMS WITH NEGLIGIBLE INTERNAL LOAD
- {acc_names} have their air-change airflow as their design airflow and almost no internal load, so they take the supply
  temperature. One central supply temperature cannot hold them and the offices at the same time. They are on central reheat
  only and fall below their DD Booklet minimum temperature in most hours: {acc_txt}.
5. ROOMS ABOVE THE MAXIMUM TEMPERATURE AT PEAK SOLAR LOAD
- {', '.join(h['room'] for h in S['hot_rooms'][:3])} exceed 25 degC at peak solar load also without reheat (hourly solar radiation above the design-day
  envelope gains). The glazing of AMSC 12.01.01 is under review by the Engineer. Not a reheat item.
6. REDUCED VAV MINIMUM IN 13 ROOMS
- The reduced minimum (page 4) is below the DD Booklet air change during occupied hours. The room outdoor air is maintained by the
  intake reset (item 3). The Contractor shall set the VAV minimum of these boxes to the values on page 4.
7. BASIS OF THE PART-LOAD CHECK
- Working hours Mon-Fri 08:00-18:00 and the office schedules are assumptions; the DD Booklet gives no working hours. Offices
  without a 24 h note are off after hours and may shut down.
- Cooling coil duty with the return air at the room condition that results; at 24 degC / 60 % RH (pull-down) {S['coil_design_24_60']:.0f} kW.
"""

PART_A = [("9000", f"{S_SUP}"), ("600", f"{S_COIL}"), ("600.0", f"{S_RH}"), ("307", f"{S_OAMIN}"), ("N/A", f"{S_OAMAX}")]
PART_B = [("9000.0", f"{S_SUP}"), ("19.6", f"{HW:.2f}"), ("26.6", f"{S['mix_db']:.1f}"), ("23.3", f"{S['mix_wb']:.1f}"), ("26.2", f"{CHW:.2f}")]
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



import textwrap


def wrap(text, width=134):
    lines, bullet = [], False
    for line in text.splitlines():                       # join the continuation lines of a bullet into one paragraph
        if bullet and line.startswith("  ") and not line.lstrip().startswith(("-", "in 13")):
            lines[-1] += " " + line.strip()
            continue
        bullet = line.lstrip().startswith("- ")
        lines.append(line)
    out = []
    for line in lines:
        if len(line) <= width:
            out.append(line)
            continue
        ind = len(line) - len(line.lstrip(" "))
        sub = " " * (ind + 2 if line.lstrip().startswith("- ") else ind)
        out += textwrap.wrap(line, width=width, initial_indent="", subsequent_indent=sub, break_on_hyphens=False)
    return "\n".join(out) + "\n"


PART, NOTES = wrap(PART), wrap(NOTES)

d = pymupdf.open(BASE)
d.delete_pages(list(range(2, len(d))))
zoning_page(d, 1)
pg = d[0]
blk = [b for b in pg.get_text("blocks") if "Serving AHU" in b[4]]
assert len(blk) == 1, "reply text on page 1 not found"
rect = pymupdf.Rect(blk[0][:4])
pg.add_redact_annot(pymupdf.Rect(rect.x0 - 1, rect.y0 - 1, rect.x1 + 1, rect.y1 + 1))
pg.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
box(pg, pymupdf.Rect(rect.x0 - 4, rect.y0 - 8, rect.x1 + 6, rect.y1 + 8), P1, 5.0)
text_page(d, "ME-0274 Reply - Page 3: Design basis, AHU airflow and coils, AHU-B-06-06STAFF-01", BASIS, 6.3)
text_page(d, "ME-0274 Reply - Page 4: Room airflow schedule, AHU-B-06-06STAFF-01", hdr + body + foot, 5.9)
text_page(d, "ME-0274 Reply - Page 5: Part-load condition, AHU-B-06-06STAFF-01", PART, 6.2)
text_page(d, "ME-0274 Reply - Page 6: Supply temperature and findings, AHU-B-06-06STAFF-01", NOTES, 6.2)
src = pymupdf.open(EQS)
schedule_page(d, src, 24, PART_A, "ME-0274 Reply - Page 7: Air Handling Unit Schedule Part A. Corrected values of AHU-B-06-06STAFF-01 in red "
              "(outdoor airflow: min / max of the reset)")
schedule_page(d, src, 25, PART_B, "ME-0274 Reply - Page 8: Air Handling Unit Schedule Part B. Corrected values of AHU-B-06-06STAFF-01 in red "
              "(re-heating coil: central reheat)")
d.set_metadata({**d.metadata, "producer": "", "creator": ""})
short_acc = "; ".join(f"{x['room']} {x['delivered']:.0f} of {x['required']:.0f} L/s" for x in S["oa_short_straight_sum"]
                      if x["room"].startswith(("ANWSIP6 MR", "ANWSIP6 Cl", "LL", "DSOT 11.06")))
ACC = f"""AH-C2-RCC-RFI-ME-0274 - Clarification on the AHU serving on L6 Surgery Admin Area
CONSULTANT RESPONSE (27 Nov 2026)

1. SERVING AHU
AHU-B-06-06STAFF-01, located at L6, serves the Surgery Admin Area at L6 and the rooms adjacent to the AGV Lift Lobbies (GL 13~14 / K~R). The tags AHU-B-07-06ADMIN-06 and AHU-B-07-06ADMIN-07 on the L6 and L7 ACMV layouts are superseded by AHU-B-06-06STAFF-01. The served area is shown on the attached AHU Zoning (reply page 2); the lift shafts of the AGV Lift Lobbies are excluded.

2. OPERATION MODE
AHU-B-06-06STAFF-01 shall be Configuration D as stipulated in the contract specifications.

3. AIRFLOW SCHEMATIC AND EQUIPMENT SCHEDULE
Reply page 3: design basis, AHU airflow and coils. Page 4: room airflow schedule (design and VAV minimum airflow of each room). Page 5: part-load condition. Page 6: supply temperature and findings. Pages 7 and 8: Equipment Schedule (Corrigendum 5) Part A and Part B with the corrected values of this AHU in red; these replace the tender values for this AHU only.
The Contractor shall produce the air-side schematic shop drawing from these airflows for the Engineer's review and shall submit the external static pressure (ESP) calculation.

3.1 Design basis
- Outdoor design 32 degC DB / 26 degC WB. Room design 24 degC, 60 % RH, within the DD Booklet range of each room (offices 23-25 degC, RH max 65 %).
- Supply air at the rooms 17.6 degC. Room supply air = larger of the air-change airflow of the room criteria and the thermal-load airflow at 17.6 degC, rounded up to 10 L/s. Safety factor 20 % on room loads.
- Outdoor air of the rooms: HFDS / ASHRAE 170 rooms at the outdoor air change rate of their criteria; other rooms SS 553:2026 Table 1 (offices 0.6 L/s per m2; meeting rooms 2.5 L/s per person + 0.3 L/s per m2). Toilets exhaust only. No occupancy diversity.
- Envelope measured on the building elevations ({glass:,.1f} m2 glazing, {wall:,.1f} m2 wall); U-values and shading coefficients of the ETTV report.
- Pressure regime neutral; Cleaners Room and two Toilets negative, single pass. Filter MERV 15. System type VAV.

3.2 AHU airflow
- {len(R)} rooms, {area:,.1f} m2. Sum of room supply air {ROOMS:,} L/s; duct leakage allowance 10 %; AHU supply air {SUP:,} L/s (schedule {S_SUP:,} L/s).
- VAV minimum airflow: air-change airflow of the room criteria, except 13 rooms with a reduced minimum (page 4). Sum of minimum airflow {S['vmin_sum']:,} L/s.
- Outdoor air of the rooms {S['oa_sum_rooms']:,.0f} L/s. AHU outdoor-air intake reset each hour so that every room receives its outdoor air: {S['oa_work_min']:,}-{S['oa_work_max']:,} L/s in working hours, {S['oa_design']:,} L/s at design flow, never below {S['oa_sum_rooms']:,.0f} L/s. Schedule: min {S_OAMIN:,} L/s, max {S_OAMAX:,} L/s.
- Dedicated exhaust {S['exhaust']:,} L/s (Cleaners Room 90 L/s; Toilets 2 x 70 L/s). Relief air = intake less exhaust, up to {S_OAMAX - S['exhaust']:,} L/s.

3.3 Coils
- Room sensible load {S['room_sensible']:.1f} kW, latent {S['room_latent']:.1f} kW. Supply fan heat {FAN_KW:.1f} kW, fan temperature rise {S['fan_rise']:.1f} K.
- Mixed air on the cooling coil {S['mix_db']:.1f} degC DB / {S['mix_wb']:.1f} degC WB at outdoor air {S['oa_design_intake']:,} L/s. Air off the cooling coil {S['coil_leaving']:.1f} degC DB / {S['coil_leaving_wb']:.1f} degC WB (dew point {S['coil_dew_point']:.1f} degC).
- Cooling coil {S['coil_design']:.1f} kW ({S['coil_design_sensible']:.1f} kW sensible), schedule {S_COIL} kW; {S['coil_design_24_60']:.1f} kW with return air at 24 degC / 60 % RH (pull-down). Chilled water 8 / 16 degC, {CHW:.2f} L/s.
- Central reheat coil on the AHU: from {S['coil_leaving']:.1f} to {S['reheat_to']:.1f} degC ({S['supply_at_rooms']:.1f} degC at the rooms after fan heat), {S['central_base_design']:.1f} kW at design flow; with the part-load reset, peak {S['central_peak']:.1f} kW; schedule {S_RH} kW. Reheat water 35 / 25 degC, {HW:.2f} L/s. No terminal reheat.

3.4 Part-load condition (reply page 5)
- Test: hours in which a room is below the minimum temperature or above the RH limit of its DD Booklet range, as a share of the hours it operates; accepted below 0.4 %.
- Method: hourly over one year (Changi typical meteorological year). Room loads without the safety factor; envelope with hourly solar radiation; people, lighting and small power on office schedules. Offices Mon-Fri 08:00-18:00, off after hours; rooms noted 24 h operate all hours.
- With 14 degC supply and the air-change minimum (90 % of design airflow), rooms are below their minimum temperature in every working hour ({R14['rooms_cold_mean']:.0f} rooms on average).
- Adopted: supply 17.6 degC at the rooms with airflows sized on it; coil at {S['coil_leaving']:.0f} degC for dehumidification and central reheat; reduced VAV minimum in 13 rooms ({S['r13_vmin']:,} L/s, {pct(S['r13_vmin'], S['r13_ach6'])} of their air-change airflow, 20-33 % by room); outdoor-air reset. With the central reheat reset by the BMS ({S['reset_hours_pct']} % of working hours, mean +{S['reset_mean']:.1f} K), no room is cold or humid in any working hour (worst RH {F['hours_humid']:.1f} % of hours without the reset).

3.5 Supply temperature and findings (reply page 6)
- 17.6 degC: with 14 degC supply the VAV minimum is 90 % of the design airflow and the rooms cannot turn down. At 17.6 degC the minimum is {S['vmin_pct_17']} % of the design airflow and the rooms follow their load. 17.6 degC is the supply temperature that the room sensible load requires at the room airflows of the 14 degC sizing; the room airflows are re-sized on it.
- Dehumidification: with the coil leaving at 15.4 degC the rooms exceed their RH limit in {R17['hours_humid']:.0f} % of the working hours. The coil leaves at {S['coil_leaving']:.0f} degC and the central reheat restores the supply temperature.
- Outdoor air: each room receives outdoor air in proportion to its supply airflow. With the intake equal to the sum of the room outdoor air, rooms with a high outdoor-air share are short, already at design flow ({short_acc}). The intake is reset to the room that needs the most outdoor air; an outdoor-airflow measuring station, modulating outdoor-air damper and BMS reset are required.
- 24 h rooms with negligible load (AGV Lift Lobbies, AGV Bays, Clean Utility): on central reheat only; they take the supply temperature and are below their DD Booklet minimum temperature in most hours (about 17-18 degC).
- Rooms above 25 degC at peak solar load also without reheat: {', '.join(h['room'] for h in S['hot_rooms'][:3])}. The glazing of AMSC 12.01.01 is under review by the Engineer.
- The reduced minimum of the 13 rooms is below the DD Booklet air change during occupied hours; their outdoor air is maintained by the intake reset. The Contractor shall set these VAV minimums to the values on page 4.
- Working hours and office schedules are assumptions (the DD Booklet gives none).

3.6 Corrected Equipment Schedule values (AHU-B-06-06STAFF-01)
Part A: supply airflow {S_SUP:,} L/s (tender 9,000); minimum total cooling {S_COIL} kW (tender 600); minimum total reheating {S_RH} kW (tender 600); outdoor airflow min {S_OAMIN:,} L/s (tender 307), max {S_OAMAX:,} L/s (tender N/A). External resistance 900 Pa provisional, to be confirmed by the Contractor's ESP calculation; fan absorbed power and motor power to follow.
Part B: re-heating coil air flow {S_SUP:,} L/s, water flow {HW:.2f} L/s; cooling coil EAT {S['mix_db']:.1f} degC DB / {S['mix_wb']:.1f} degC WB, LAT 12.0 degC DB / 11.0 degC WB, chilled water flow {CHW:.2f} L/s. Other values unchanged.

4. COST IMPACT
There will be a cost omission from the Tender Basis. Against the tender Equipment Schedule for this AHU (supply air 9,000 L/s, cooling 600 kW, reheat 600 kW), the AHU reduces to {S_SUP:,} L/s supply air, {S_COIL} kW cooling and {S_RH} kW reheat. The outdoor air increases to {S_OAMIN:,}-{S_OAMAX:,} L/s (tender 307 L/s), with an outdoor-airflow measuring station and reset control. The Contractor shall submit the net cost omission for the Engineer's review.

Attachment: AH-C2-RCC-RFI-ME-0274_Beca Reply.pdf (8 pages: reply; AHU Zoning; design basis, AHU airflow and coils; room airflow schedule; part-load condition; supply temperature and findings; Equipment Schedule Part A and Part B with the corrected values of AHU-B-06-06STAFF-01).
"""
open(ACC_TXT, "w", encoding="utf-8").write(ACC)
print(len(ACC), "characters", ACC_TXT)
os.makedirs(os.path.dirname(OUT), exist_ok=True)
d.save(OUT, garbage=3, deflate=True)
print(len(pymupdf.open(OUT)), "pages", OUT)
