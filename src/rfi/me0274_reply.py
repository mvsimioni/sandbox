"""Reply to AH-C2-RCC-RFI-ME-0274 written into the RFI form as editable text-box annotations (not flattened).

Pages 1-5: the RFI as received, reply text box on page 1. Page 6: design basis and AHU airflow. Page 7: room airflow schedule. Pages 8-9: the two
Equipment Schedule pages of the unit (Corr 5, pages 25 and 26) with the corrected values of AHU-B-06-06STAFF-01 marked over the printed cells.
Values: D47 workbook as uploaded on 25 Nov 2026 (airflows) and the coil method of D-104 (minimum reheat).
``python3 -I src/rfi/me0274_reply.py``
"""
import pymupdf, openpyxl

RFI = "data/uploads/rfi_me0274/x/ME-0274 - Clarification on the AHU serving on L6 Surgery Admin Area/Reply/AH-C2-RCC-RFI-ME-0274_Beca Reply.pdf"
WB = "out/issue/DRAFT_2026-11-25_D47_psychrometric/AIGH_C2_D47_Admin_Workspace_Thermal_Load_Calc_RevA.xlsx"
EQS = "data/source/Basis/5. Equipment Schedules (Corr 5).pdf"
OUT = "out/issue/RFI_ME-0274_reply/AH-C2-RCC-RFI-ME-0274_Beca Reply.pdf"
TAG = "AHU-B-06-06STAFF-01"
RED = (1, 0, 0)

ws = openpyxl.load_workbook(WB, data_only=True)["Airflow"]
rooms = []
for r in range(7, 78):
    if ws.cell(r, 4).value == TAG:
        g = lambda c: ws.cell(r, c).value or 0
        rooms.append((g(1), g(2), g(6), g(20), g(26), g(23), g(24), g(14)))
rooms.sort(key=lambda x: x[0])
tot = lambda i: sum(x[i] for x in rooms)
assert round(tot(3)) == 5780 and len(rooms) == 44

P1 = ("1. Serving AHU: AHU-B-06-06STAFF-01 (located at L6) serves the Surgery Admin Area at L6 and the rooms adjacent to the AGV Lift Lobbies (GL 13~14 / K~R). "
      "The tags AHU-B-07-06ADMIN-06 and AHU-B-07-06ADMIN-07 on the L6 and L7 ACMV layouts are superseded by AHU-B-06-06STAFF-01. Refer to the attached \"AHU Zoning - RFI 0274\" for the served area.\n"
      "2. Operation mode: AHU-B-06-06STAFF-01 shall be Configuration D as stipulated in the contract specifications.\n"
      "3. Airflow: sum of room supply air 5,780 L/s (44 rooms, 1,057 m2). Duct leakage allowance 10 % of room supply. AHU supply air 6,358 L/s; schedule value 6,400 L/s. "
      "Outdoor air 2,225 L/s (35 % of AHU supply); schedule value 2,250 L/s. Dedicated exhaust 90 L/s (Cleaners Room, single pass). Recirculated air 4,133 L/s. "
      "Cooling coil duty 161.4 kW; schedule value 165 kW. Reheat coil 65 kW. System type: VAV. Design basis: page 6. Room airflows: page 7. "
      "Equipment schedule with the corrected values of this AHU: pages 8 and 9 (these replace the values of the tender Equipment Schedule for this AHU).\n"
      "Contractor to produce the air-side schematic shop drawing from these airflows for the Engineer's review, and to submit the external static pressure (ESP) calculation to confirm the ESP.")

BASIS = f"""Reply to AH-C2-RCC-RFI-ME-0274. Unit: AHU-B-06-06STAFF-01 (L6 ASW - Staff Area).

A. DESIGN BASIS
Outdoor design condition      32 degC DB / 26 degC WB (enthalpy 80.3 kJ/kg)
Room design condition         24 degC, 60 % RH (criteria range 23-25 degC, RH max 65 %)
Minimum total supply          6 air changes per hour (offices, meeting rooms, open workspace,
                              stores, lift lobbies). Cleaners Room: 20 air changes per hour.
Outdoor air                   Offices, workstations, open workspace, meeting rooms: larger of
                              (10 L/s per person x occupancy x intermittent occupancy factor) and
                              (0.6 L/s per m2). Intermittent occupancy factor: 0.7 offices,
                              workstations and open workspace; 0.4 meeting rooms
                              Stores, lobbies, utility: 2 air changes per hour
Room supply air               Larger of the air-change airflow and the thermal-load airflow,
                              rounded up to 10 L/s
Thermal-load airflow          Room sensible load / (1.21 x (room temperature - 14 degC supply))
Safety factor on room loads   10 %
Ceiling height                3.0 m (2.7 m in two rooms)
Pressure regime               Neutral. Cleaners Room: negative, single pass, dedicated exhaust
Filter                        MERV 15

B. AHU AIRFLOW
Number of rooms               44
Floor area                    {tot(2):,.1f} m2
Sum of room supply air        {tot(3):,.0f} L/s
Duct leakage allowance        10 % of room supply air (allowance in the AHU supply air;
                              ductwork to be built and tested to the specification leakage class)
AHU supply air                6,358 L/s   (schedule value 6,400 L/s)
Sum of room outdoor air       {tot(4):,.0f} L/s
Outdoor air at the AHU        2,225 L/s = 35 % x AHU supply air. The fraction is set by the room
                              with the highest outdoor air fraction (Office - Typical, 14 L/s
                              outdoor air in 40 L/s supply). Schedule value 2,250 L/s
Recirculated air              4,133 L/s
Dedicated exhaust             90 L/s (Cleaners Room)
Spill / relief air            2,135 L/s (outdoor air less exhaust)

C. COILS
Room sensible load            42.0 kW
Room latent load              13.8 kW
Supply fan heat               16.6 kW (1,700 Pa fan total pressure, 65 % fan and motor efficiency);
                              fan temperature rise 2.2 K (draw-through)
Supply air required at rooms  18.0 degC DB
Mixed air on cooling coil     26.8 degC DB / 21.5 degC WB (62.3 kJ/kg)
Air off cooling coil          15.8 degC DB / 14.7 degC WB (dew point 14.0 degC)
Cooling coil sensible         84.4 kW
Cooling coil latent           77.0 kW
Cooling coil total            161.4 kW  (schedule value 165 kW)
Chilled water                 8 degC entering / 16 degC leaving; 4.82 L/s (schedule value 4.93 L/s)
Reheat coil                   No reheat is needed at the design condition. The reheat coil is sized
                              to warm the full supply air from the coil leaving air (15.8 degC) to the
                              maximum leaving air temperature of the schedule (24 degC): 63.3 kW
                              (schedule value 65 kW)
Reheat water                  35 degC entering / 25 degC leaving; 1.55 L/s

D. NOTES
1. System type VAV. Each room airflow on page 7 is the design (maximum) supply air.
2. The external resistance in the equipment schedule (900 Pa) is provisional. Contractor to submit
   the external static pressure calculation. Fan absorbed power and motor power to follow that
   calculation.
3. Room names and room numbers are those of the Schedule of Accommodation.
4. The served area is shown on the attached "AHU Zoning - RFI 0274".
5. Pages 8 and 9: Equipment Schedule (Corrigendum 5), Air Handling Unit Schedule Part A and
   Part B, with the corrected values of AHU-B-06-06STAFF-01 in red. Values not marked are unchanged.
"""

# old printed text -> new value, in the order printed on the row
PART_A = [("9000", "6400"), ("600", "165"), ("600.0", "65"), ("307", "2250")]
PART_B = [("9000.0", "6400"), ("12.0", "15.8"), ("19.6", "1.55"), ("26.6", "26.8"), ("23.3", "21.5"), ("12.0", "15.8"), ("11.0", "14.7"), ("26.2", "4.93")]


def box(page, rect, text, size, font="helv", fill=None, align=0):
    a = page.add_freetext_annot(rect, text, fontsize=size, fontname=font, text_color=RED, fill_color=fill, align=align)
    a.set_info(title="Beca", subject="Text Box")
    a.update()
    return a


def text_page(d, title, text, size):
    q = d.new_page(width=595, height=842)
    box(q, pymupdf.Rect(40, 36, 555, 60), title, 10, "hebo")
    box(q, pymupdf.Rect(40, 66, 555, 810), text, size, "cour")


def schedule_page(d, src, pno, changes, title):
    d.insert_pdf(src, from_page=pno, to_page=pno)
    p = d[-1]
    hit = p.search_for(TAG)[0]
    yc = (hit.y0 + hit.y1) / 2
    words = sorted((w for w in p.get_text("words") if abs((w[1] + w[3]) / 2 - yc) < 3), key=lambda w: w[0])
    i = 0
    for old, new in changes:
        while words[i][4] != old:
            i += 1
        w = words[i]
        i += 1
        r = pymupdf.Rect(w[0] - 2.5, w[1], w[2] + 2.5, w[3] + 2)
        box(p, r, new, max(4, (w[3] - w[1]) * 0.82), fill=(1, 1, 1), align=1)
    sq = p.add_rect_annot(pymupdf.Rect(words[0][0] - 3, hit.y0 - 2, words[-1][2] + 3, hit.y1 + 2))
    sq.set_colors(stroke=RED); sq.set_border(width=0.6); sq.set_info(title="Beca"); sq.update()
    box(p, pymupdf.Rect(40, 14, p.rect.width - 40, 34), title, 9, "hebo")


d = pymupdf.open(RFI)
pg = d[0]
for a in list(pg.annots()):
    if a.type[1] == "FreeText":
        pg.delete_annot(a)
box(pg, pymupdf.Rect(82, 600, 529, 690), P1, 6)
text_page(d, "ME-0274 Reply - Page 6: Design basis and AHU airflow, AHU-B-06-06STAFF-01", BASIS, 7.3)
hdr = (f"{'Room number':<18}{'Room name':<28}{'Area':>7}{'Supply':>8}{'OA':>7}{'Exhaust':>8}{'Return':>8}  Governing\n"
       f"{'':<18}{'':<28}{'m2':>7}{'L/s':>8}{'L/s':>7}{'L/s':>8}{'L/s':>8}\n" + "-" * 96 + "\n")
body = "".join(f"{n:<18}{nm[:27]:<28}{a:>7.1f}{s:>8.0f}{oa:>7.0f}{ex:>8.0f}{rt:>8.0f}  {'air change' if gov == 'AIR CHANGE' else 'thermal load'}\n"
               for n, nm, a, s, oa, ex, rt, gov in rooms)
foot = ("-" * 96 + f"\n{'TOTAL (44 rooms)':<46}{tot(2):>7.1f}{tot(3):>8.0f}{tot(4):>7.0f}{tot(5):>8.0f}{tot(6):>8.0f}\n\n"
        "Duct leakage allowance 10 %: AHU supply air = 5,780 x 1.10 = 6,358 L/s (schedule value 6,400 L/s).\n"
        "OA = outdoor air content of the room supply air. Outdoor air at the AHU = 2,225 L/s (schedule value 2,250 L/s).\n"
        "Supply = design (maximum) room supply air.\n")
text_page(d, "ME-0274 Reply - Page 7: Room airflow schedule, AHU-B-06-06STAFF-01", hdr + body + foot, 7)
src = pymupdf.open(EQS)
schedule_page(d, src, 24, PART_A, "ME-0274 Reply - Page 8: Air Handling Unit Schedule Part A. Corrected values of AHU-B-06-06STAFF-01 in red")
schedule_page(d, src, 25, PART_B, "ME-0274 Reply - Page 9: Air Handling Unit Schedule Part B. Corrected values of AHU-B-06-06STAFF-01 in red")
d.save(OUT, garbage=3, deflate=True)
print(len(pymupdf.open(OUT)), "pages")
