"""Design-day report workbook of the thermal model (D-109): out/sim/AIGH_IP_thermal_model_design_days.xlsx.

Sheets: Summary (block peak per design day, matched-unit totals), Units (results_design_days.csv), Not matched (units in one source
only), Assumptions (every assumed or placeholder input of the model). Values only; sources named per sheet.
"""
from __future__ import annotations

import csv
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parent.parent.parent
SIM = ROOT / "out/sim"
SQL = SIM / "run_sizing_min/eplusout.sql"
ISSUE = "out/issue/DRAFT_2026-11-26_R19"

DAYS = [("SS553", "SS 553:2026 cl. 5.4.4 cooling day, 33 °C DB / 27 °C WB, 21 May (basis)"),
        ("DP", "ASHRAE 2025 HoF Changi 0.4 % dehumidification, DP 27.2 °C / MCDB 29.4 °C (comparison)"),
        ("WB", "ASHRAE 2025 HoF Changi 0.4 % evaporation, WB 28.0 °C / MCDB 30.8 °C (comparison)")]

ASSUMPTIONS = [
    ("Geometry", "One thermal zone per storey and unit; outline = union of the zoning room outlines of the unit, simplified 0.25 m", "Zoning Rev H (D-108, D-112)"),
    ("Geometry", "Floor-to-floor heights from the FFL of the building sections: LB3 0.00, B3 3.80, B2 8.30, B1 12.80, L01 17.00, L02 23.00, "
                 "then 6.00 m per storey to L08 59.00 (L01 mezzanine at 20.00 not modelled as a storey)", "Sections AR-SW-20-ZZ-001 to 003"),
    ("Geometry", "Slab 250 mm concrete (floors, ceilings and the roof slab); zone height = level to level", "Marcus, 26 Nov 2026 (D-119)"),
    ("Geometry", "External wall = wall within 0.8 m of the storey footprint edge; this also catches walls at shafts, voids and light wells "
                 "(over-counts envelope on internal zones)", "ASSUMPTION"),
    ("Geometry", "Sliver surfaces below 0.05 m² removed (44); 133 zones not fully enclosed (volume only)", "Model cleaning"),
    ("Envelope", "Glass of each zone and orientation measured on the building elevations (forensic envelope review D-120, Council C6), placed "
                 "as one window-to-wall ratio on the zone's exterior walls, at most 0.9 (3,190 of 3,330 m² placed; out/sim/glass_placement.csv)",
     "Elevations AR-IP-31-ZZ-001 to -008"),
    ("Envelope", "Wall U and SC per orientation from the ETTV report; glazing U 1.65 W/m²K; SHGC = 0.87 × SC", "ETTV report"),
    ("Geometry", "Terraces, covered areas and NV corridors are outdoors: the walls of the zones behind them are exterior walls", "D-120"),
    ("Envelope", "Roof U 0.5 W/m²K", "ASSUMPTION: no roof U in the project documents"),
    ("Envelope", "Basement external walls and ground floors adiabatic", "ASSUMPTION"),
    ("Envelope", "Walls to rooms not modelled (MV, NV, unconditioned) adiabatic", "ASSUMPTION"),
    ("Solar", "No mutual shading of building wings (MinimalShadowing); conservative on solar gain", "ASSUMPTION"),
    ("Loads", "People, lighting, small power, equipment sensible/latent and infiltration per room from the Room Zones sheets, summed per zone",
     "Workbooks R19"),
    ("Loads", "All internal loads at 100 % for 24 h (hospital), no diversity", "ASSUMPTION / placeholder for schedules"),
    ("Outdoor air", "Unit outdoor air from the AHU tab section B, shared over the unit's zones by room supply", "Workbooks R19"),
    ("HVAC", "Ideal loads air system per zone: no fan heat, no reheat, no coil leaving condition; minimum supply 12 °C", "Model method"),
    ("HVAC", "Cooling setpoint = supply-weighted design room temperature; dehumidifying humidistat 60 % RH; no humidification",
     "Workbooks R19 / ASSUMPTION for 60 %"),
    ("Design days", "Date 21 May, wind and clear-sky tau (0.463 / 2.322) from the Changi TMYx DDY; SS 553 gives no date", "Confirm the date"),
    ("Weather", "Changi TMYx EPW for the annual run; site from the EPW", "Weather set 26 Nov 2026"),
    ("Scope", "8 units with no zoning outline not modelled; 117 zones with no room rows carry envelope load only", "See Not matched"),
]


def _sheet(ws, header, rows, widths=None):
    ws.append(header)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="2F4F6F")
        c.alignment = Alignment(wrap_text=True, vertical="top")
    for r in rows:
        ws.append(r)
    ws.freeze_panes = "A2"
    for i, w in enumerate(widths or [], 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def block_peaks():
    c = sqlite3.connect(SQL)
    q = """select ep.EnvironmentName, t.Hour, d.Name, sum(r.Value)/1000 from ReportData r
           join ReportDataDictionary d using(ReportDataDictionaryIndex) join Time t using(TimeIndex)
           join EnvironmentPeriods ep using(EnvironmentPeriodIndex) where d.Name like 'Zone Ideal Loads%' group by 1,2,3"""
    D = defaultdict(dict)
    for env, h, n, v in c.execute(q):
        D[(env, h)][n] = v
    out = {}
    for env in {k[0] for k in D}:
        k = max((k for k in D if k[0] == env), key=lambda k: D[k]["Zone Ideal Loads Supply Air Total Cooling Rate"])
        tag = "SS553" if "SS553" in env else ("DP" if "DP=>MDB" in env else "WB")
        x = D[k]
        out[tag] = (k[1], x["Zone Ideal Loads Supply Air Total Cooling Rate"], x["Zone Ideal Loads Outdoor Air Total Cooling Rate"],
                    x["Zone Ideal Loads Supply Air Sensible Cooling Rate"], x["Zone Ideal Loads Supply Air Latent Cooling Rate"])
    return out


def _annual(ws, sql):
    """Monthly cooling energy (ideal loads, MWh thermal), annual peak hour and its weather, hours above the design enthalpies."""
    c = sqlite3.connect(sql)
    q = """select d.Name, t.Month, sum(r.Value) from ReportData r join ReportDataDictionary d using(ReportDataDictionaryIndex)
           join Time t using(TimeIndex) where d.Name in ('Zone Ideal Loads Supply Air Total Cooling Rate',
           'Zone Ideal Loads Outdoor Air Total Cooling Rate','Zone Ideal Loads Supply Air Latent Cooling Rate') group by 1,2"""
    m = defaultdict(dict)
    for n, mo, v in c.execute(q):
        m[mo][n] = v / 1e6
    names = ["Zone Ideal Loads Supply Air Total Cooling Rate", "Zone Ideal Loads Outdoor Air Total Cooling Rate",
             "Zone Ideal Loads Supply Air Latent Cooling Rate"]
    rows = [[mo] + [round(m[mo].get(n, 0)) for n in names] for mo in sorted(m)]
    rows.append(["Year"] + [round(sum(m[mo].get(n, 0) for mo in m)) for n in names])
    _sheet(ws, ["Month", "Coil cooling energy, MWh (thermal)", "of which outdoor air, MWh", "Latent, MWh"], rows, [10, 22, 22, 16])
    pk = c.execute("""select t.Month, t.Day, t.Hour, sum(r.Value) s from ReportData r join ReportDataDictionary d using(ReportDataDictionaryIndex)
                      join Time t using(TimeIndex) where d.Name='Zone Ideal Loads Supply Air Total Cooling Rate'
                      group by r.TimeIndex order by s desc limit 1""").fetchone()
    from CoolProp.HumidAirProp import HAPropsSI
    epw = [r for r in csv.reader(open(ROOT / "data/uploads/weather_20261126/SGP_SG_Singapore-Changi.Intl.AP.486980_TMYx.epw",
                                      encoding="latin-1"))][8:]
    hh = lambda r: HAPropsSI("H", "T", float(r[6]) + 273.15, "D", float(r[7]) + 273.15, "P", float(r[9])) / 1000
    w = next(r for r in epw if int(r[1]) == pk[0] and int(r[2]) == pk[1] and int(r[3]) == pk[2])
    ws.append([])
    ws.append([f"Annual peak hour: {pk[1]:02d}/{pk[0]:02d} hour {pk[2]}, coil load {pk[3] / 1e6:.1f} MW; weather file DB {w[6]} °C, "
               f"DP {w[7]} °C, h {hh(w):.1f} kJ/kg. This dew point is above the ASHRAE extreme annual WB (29.1 °C) and lasts one hour: "
               "a weather-file extreme, not a design condition."])
    ws.append([f"Weather-file hours above the SS 553 enthalpy (85.0 kJ/kg): {sum(1 for r in epw if hh(r) > 85.0)}; above the ASHRAE "
               f"0.4 % WB point (89.9 kJ/kg): {sum(1 for r in epw if hh(r) > 89.9)} of 8,760."])
    ws.append(["Annual run: Changi TMYx EPW, all internal loads 24 h at 100 %, ideal loads (no fan heat, no reheat, no plant efficiency): "
               "thermal energy at the coil, not electricity."])


def main():
    rows = list(csv.DictReader((SIM / "results_design_days.csv").open(encoding="utf-8")))
    f = lambda x, k: float(x[k]) if x.get(k) not in (None, "") else None
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    bp = block_peaks()
    m = [x for x in rows if f(x, "workbook coil kW (D)")]
    srows = []
    for tag, desc in DAYS:
        h, tot, oa, sen, lat = bp[tag]
        srows.append([desc, h, round(tot), round(oa), round(sen), round(lat),
                      round(sum(f(x, f"{tag} coil kW") or 0 for x in m)), round(sum(f(x, f"{tag} room kW") or 0 for x in m))])
    srows.append(["Workbooks R19, same units: COOLING COIL DUTY (AHU tab section D) / room sensible + latent (section B)", None, None, None,
                  None, None, round(sum(f(x, "workbook coil kW (D)") for x in m)), round(sum(f(x, "workbook room kW") or 0 for x in m))])
    _sheet(ws, ["Design day", "Block peak hour", "Block coil load, kW", "of which outdoor air, kW", "Block sensible, kW", "Block latent, kW",
                f"Coil load, {len(m)} matched units (sum of unit peaks), kW", "Room load, matched units, kW"], srows,
           [70, 10, 14, 14, 14, 14, 22, 18])
    ws.append([])
    ws.append(["Block peak = all 300 modelled units, coincident hour. Matched units = units with a section D duty in the workbooks."])
    ws.append(["Model coil load has no fan heat and no coil leaving condition; the workbook duty has both."])
    ws2 = wb.create_sheet("Units")
    keys = list(rows[0].keys())
    _sheet(ws2, keys, [[(float(x[k]) if k not in ("unit", "storeys", "workbooks") and x[k] not in ("",) else x[k]) for k in keys] for x in rows],
           [44] + [12] * (len(keys) - 1))
    ws3 = wb.create_sheet("Not matched")
    sys.path.insert(0, str(ROOT))
    from src.sim.results import workbook_duties
    wbd = workbook_duties(ROOT / ISSUE)
    mu = {x["unit"] for x in rows}
    nm = [["In the model, no workbook AHU-tab row", x["unit"], x["storeys"], f(x, "rooms_m2"), f(x, "SS553 coil kW")]
          for x in rows if not f(x, "workbook room kW")]
    nm += [["In the workbooks, not in the model zoning", u, "", None, round(v["coil_d"] or v["coil_b"], 1)] for u, v in wbd.items() if u not in mu]
    _sheet(ws3, ["Case", "Unit", "Storeys", "Room area in the workbooks, m²", "Coil load (model SS 553 or workbook), kW"], nm, [40, 60, 12, 16, 18])
    ws4 = wb.create_sheet("Assumptions")
    _sheet(ws4, ["Item", "Model input", "Source or status"], ASSUMPTIONS, [14, 110, 40])
    for w in (ws, ws4):
        for row in w.iter_rows():
            for c in row:
                c.alignment = Alignment(wrap_text=True, vertical="top")
    ann = SIM / "run_annual/eplusout.sql"
    if ann.exists():
        _annual(wb.create_sheet("Annual"), ann)
    out = SIM / "AIGH_IP_thermal_model_design_days.xlsx"
    wb.save(out)
    print(out, len(rows), len(nm))


if __name__ == "__main__":
    main()
