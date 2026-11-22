"""Department input for the Rev A worked example, read from the Rev A workbook itself (with sources)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

import openpyxl

REVA_PATH = Path("data/source/Template/AIGH_C2_L1_OP_Pharmacy_AHU_Thermal_Load_Calc_RevA.xlsx")
REVA_NAME = REVA_PATH.name
ISSUE = "Rev A draft, 1 Oct 2026"
SS = "Level 1 Room Zones"


def _src(id_, doc, page, loc, text, method="openpyxl read of the cell (formulas kept)", conf="high"):
    return dict(id=id_, document=doc, issue=ISSUE, page=page, location=loc, text_as_read=text, method=method, confidence=conf)


def _density(formula: Any) -> Any:
    m = re.match(r"=M\d+\*(\d+(?:\.\d+)?)/100", str(formula))
    return float(m.group(1)) if m else None


def build_reva_input(root: Path = Path(".")) -> Dict[str, Any]:
    wb = openpyxl.load_workbook(root / REVA_PATH)  # formulas kept
    ws = wb[SS]
    bod = wb["Basis of Design"]
    ahu = wb["AHU Summary"]
    v = lambda c: ws[c].value
    doc = REVA_NAME
    sources: List[Dict[str, Any]] = []
    rows = [30, 31, 32, 33, 34, 37]
    for r in rows:
        txt = "; ".join(f"{c}{r}={ws[f'{c}{r}'].value}" for c in ("E", "G", "H", "J", "K", "L", "M", "O", "Q", "T", "U", "V", "Z", "AD", "AJ", "AN", "AP", "AQ", "BI")
                        if ws[f"{c}{r}"].value is not None)
        sources.append(_src(f"RA-RZ-{r}", doc, SS, f"row {r}, input columns", txt))
    sources.append(_src("RA-RZ-TBL", doc, SS, "design tables D4:F9 (orientation), D13:G21 (space types), Z5:AB11 (people), J5:J9, N5:N7",
                        f"North {v('E5')}/{v('F5')}; East {v('E6')}/{v('F6')}; South {v('E7')}/{v('F7')}; West {v('E8')}/{v('F8')} W/m2; roof solar gain {v('J9')}; "
                        f"outdoor {v('N5')} C DB / {v('N6')} C WB / {v('N7')} kJ/kg"))
    for cell, key, label in (("C19", "RA-BOD-C19", "outdoor design, summer"), ("C28", "RA-BOD-C28", "supply air temperature"),
                             ("C29", "RA-BOD-C29", "ceiling height"), ("C30", "RA-BOD-C30", "safety factor"),
                             ("C31", "RA-BOD-C31", "duct leakage allowance"), ("C25", "RA-BOD-C25", "pressure relationship"),
                             ("C23", "RA-BOD-C23", "minimum outdoor air"), ("C24", "RA-BOD-C24", "minimum total supply air")):
        sources.append(_src(key, doc, "Basis of Design", cell, f"{bod['B' + cell[1:]].value}: {bod[cell].value}"))
    sources.append(_src("RA-BOD-B42", doc, "Basis of Design", "B42", str(bod["B42"].value)))
    sources.append(_src("RA-BOD-B45", doc, "Basis of Design", "B45:B51", "key flags 1 to 7"))
    sources.append(_src("RA-ENV", doc, "Basis of Design", "B47, J35:J39", str(bod["B47"].value)))
    sources.append(_src("RA-EQ", doc, "Basis of Design", "B48, J36, J38", str(bod["B48"].value)))
    sources.append(_src("RA-OCC", doc, "Basis of Design", "B12, J37", f"{bod['B12'].value}: {bod['E12'].value}"))
    sources.append(_src("RA-DD21", "DD Booklet, Pharmacy (Batch 2), Aug 2025 (as cited by the Rev A Basis of Design)", "p.21",
                        "criteria table 'OP Level 1 Pharmacy'", str(bod["E10"].value),
                        method="read through the Rev A workbook: Basis of Design E10 and Level 1 Room Zones V30:Z34 (the booklet itself is not re-read here)",
                        conf="medium"))
    sources.append(_src("RA-AHU-B29", doc, "AHU Summary", "B29:B30, D29:D30", f"fan total pressure {ahu['B29'].value} Pa; efficiency {ahu['B30'].value}"))
    sources.append(_src("RA-AHU-REF", doc, "AHU Summary", "A44:F49", f"{ahu['A44'].value} {ahu['B44'].value}; {ahu['A45'].value} {ahu['B45'].value} / OA {ahu['E45'].value}; "
                                                                      f"{ahu['A46'].value} {ahu['B46'].value} / OA {ahu['E46'].value}"))
    sources.append(_src("RA-SS553", "SS 553:2016 Table 2 (as cited by the Rev A Basis of Design B12)", "Table 2", "reception / waiting; pharmacy",
                        "30 persons/100 m2 reception and waiting; 10 persons/100 m2 pharmacy (prep area)", method="read through the Rev A workbook", conf="medium"))
    sources.append(_src("RA-DD7", "DD Booklet, Pharmacy (as cited by the Rev A Basis of Design)", "p.7", "room areas and AHU zoning",
                        str(bod["B10"].value), method="read through the Rev A workbook", conf="medium"))

    rooms = []
    regime = {"G": "POSITIVE"}
    reg = {"Z1": "POSITIVE", "Z2": "POSITIVE", "Z3": "NEUTRAL", "Z4": "NEUTRAL", "Z5": "POSITIVE", "TEA": "NEGATIVE"}
    crit_row = {"Z1": "Wait Area", "Z2": "Dispensing / Manual Picking / Payment", "Z3": "Lockers", "Z4": "Pantry (Bay)", "Z5": "Wait Area",
                "TEA": "Toilet"}
    temps = {"Z1": (21, 24, 60), "Z2": (21, 24, 60), "Z3": (23, 25, 65), "Z4": (21, 24, 65), "Z5": (21, 24, 60), "TEA": (None, None, None)}
    for r in rows:
        zone = str(v(f"G{r}")).split(" ")[0]
        sup = r != 37
        name = str(v(f"E{r}")) if sup else "TOILET (DD p.7, 4.3 m2), not in the contractor list"
        pfix = None
        dens = None
        if sup:
            t = v(f"T{r}")
            dens = _density(t)
            pfix = float(t) if dens is None and t is not None else None
        prov = {k: ["D", f"RA-RZ-{r}"] for k in ("identity", "zoning", "space_type", "soa_area", "ceiling_height", "iof", "activity",
                                                   "operation_hours", "lighting", "small_power", "equipment_latent")}
        prov["identity"] = ["D", f"RA-RZ-{r}"]
        prov["ceiling_height"] = ["D", "RA-BOD-C29"]
        prov["area_selector"] = ["S", "RA-BOD-B42"]
        prov["occupancy_density"] = ["D", "RA-OCC"]
        prov["occupancy_people"] = ["A", "RA-OCC"]
        prov["equipment_sensible"] = ["A", "RA-EQ"]
        prov["equipment_latent"] = ["A", "RA-EQ"]
        if r in (32, 33):
            prov["iof"] = ["A", "RA-OCC"]
        env = []
        for ori_c, w_c, wall_c in (("J", "O", "P"), ("K", "Q", "R")):
            ori, w, wl = v(f"{ori_c}{r}"), v(f"{w_c}{r}"), v(f"{wall_c}{r}")
            if ori in (None, "-", "Internal"):
                continue
            if (w or 0) > 0 or (wl or 0) > 0:
                env.append(dict(segment=f"{zone}-{str(ori)[0]}", building_part=None, face=str(ori), azimuth_deg=None, orientation=str(ori),
                                glazing_m2=float(w or 0), wall_m2=float(wl or 0), roof_m2=0.0, shading="shaded by the L1 colonnade (as Rev A states)",
                                method="assumed", code="A", source="RA-ENV",
                                remark="Rev A flags this glazing as an ASSUMPTION to confirm against the architectural elevations"))
        if r in (30, 31, 34) or True:
            roof = v(f"S{r}")
            if (roof or 0) > 0:
                env.append(dict(segment=f"{zone}-R", face="Roof", orientation="Internal", glazing_m2=0.0, wall_m2=0.0, roof_m2=float(roof),
                                method="assumed", code="A", source="RA-ENV"))
        c = dict(criteria_row=crit_row[zone], page="p.21", min_oa_ach=v(f"V{r}") if sup else None, min_total_ach=v(f"Z{r}") if sup else None,
                 exhaust_ach=v(f"AD{r}"), temp_min_c=temps[zone][0], temp_max_c=temps[zone][1], rh_max_pct=temps[zone][2],
                 pressure_regime=reg[zone], air_class="3" if zone == "TEA" else "2", filter="MERV 15" if sup else None,
                 single_pass="N", afterhours="24 h AC" if zone == "Z2" else None, source="RA-DD21", code="D",
                 remark="values from Level 1 Room Zones V:Z and AD (the cells carry the DD p.21 values); regime from Basis of Design C25")
        rooms.append(dict(
            number=zone, name=name, storey="L01", ahu=str(v(f"B{r}")), zone_tag=zone, system_type=str(v(f"F{r}")) if sup else "EXHAUST",
            terminal_tag=None, exhaust_fan=None, space_type=str(v(f"H{r}")), room_condition=str(v(f"I{r}")) if sup else "Comfort",
            soa_area_m2=float(v(f"M{r}")), measured_area_m2=None, area_selector="SOA", ceiling_height_m=float(v(f"L{r}")),
            occupancy_people=pfix, occupancy_per_100m2=dens, iof=v(f"U{r}") if sup else None, activity=str(v(f"AJ{r}")) if sup else None,
            operation_hours=24 if zone in ("Z2",) else None,
            lighting_w_m2=v(f"AN{r}") if sup else None, small_power_w_m2=v(f"AQ{r}") if sup else None,
            equipment_sensible_w=v(f"AP{r}") if sup and (v(f"AP{r}") or 0) > 0 else None,
            equipment_latent_w=v(f"BI{r}") if sup and (v(f"BI{r}") or 0) > 0 else None,
            prov=prov, criteria=c, envelope=env,
            remark=f"Rev A {SS} row {r}: contractor area {v(f'M{r}')} m2 entered as the SOA-column area (no SOA row exists for the contractor's grouping); "
                   f"room number is the zone tag because Rev A gives room ID '-'."))
        if not sup:
            rooms[-1]["occupancy_per_100m2"] = None

    sp = []
    for rr in range(13, 18):
        sp.append(dict(name=str(ws[f"D{rr}"].value), room_t_c=ws[f"E{rr}"].value, room_h_kjkg=ws[f"G{rr}"].value, code="D", source="RA-RZ-TBL"))
    acts = [dict(name=str(ws[f"Z{rr}"].value), sensible_w=ws[f"AA{rr}"].value, latent_w=ws[f"AB{rr}"].value, code="D", source="RA-RZ-TBL")
            for rr in range(5, 12)]
    ori = [dict(name=str(ws[f"D{rr}"].value), window_w_m2=ws[f"E{rr}"].value, wall_w_m2=ws[f"F{rr}"].value, code="A", source="RA-RZ-TBL")
           for rr in range(5, 10)]
    defaults = []
    for rr in range(6, 14):
        nm = ws[f"AI{rr}"].value
        if nm:
            defaults.append(dict(name=str(nm), lighting_w_m2=ws[f"AK{rr}"].value, small_power_w_m2=ws[f"AL{rr}"].value, density_per_100m2=None,
                                 source="RA-RZ-TBL"))
    constants = {
        "SupplyTemp": dict(label="Supply air temperature at the room", value=float(ws["BV30"].value), unit="°C", code="A", source="RA-BOD-C28",
                           remark="12 °C off-coil + about 2 K fan rise: ASSUMPTION, confirm with AHU selection"),
        "SafetyFactor": dict(value=0.1, code="D", source="RA-BOD-C30", remark="10 % template convention (hard-coded 1.1 in Rev A BO and BQ)"),
        "DuctLeakage": dict(value=float(ahu["D8"].value), code="D", source="RA-BOD-C31", remark="10 % template convention (AHU Summary D8)"),
        "OutdoorDB": dict(label="Outdoor design dry bulb, summer", value=float(v("N5")), unit="°C", code="D", source="RA-BOD-C19", remark="SS 553:2016"),
        "OutdoorWB": dict(label="Outdoor design wet bulb, summer", value=float(v("N6")), unit="°C", code="D", source="RA-BOD-C19", remark="SS 553:2016"),
        "OutdoorH": dict(label="Outdoor design enthalpy, summer", value=float(v("N7")), unit="kJ/kg", code="D", source="RA-RZ-TBL", remark=""),
        "FanPressure": dict(label="Fan total pressure (supply)", value=float(ahu["B29"].value), unit="Pa", code="A", source="RA-AHU-B29",
                            remark="1000 Pa external + 700 Pa internal allowance: ADJUST to selection"),
        "FanEfficiency": dict(label="Fan and motor efficiency", value=float(ahu["B30"].value), unit="–", code="A", source="RA-AHU-B29",
                              remark="assumption, adjust to selection"),
        "RoofSolarGain": dict(label="Roof solar gain", value=float(v("J9")), unit="W/m²", code="D", source="RA-RZ-TBL", remark=""),
    }
    refs = [dict(label=str(ahu["A44"].value), flow_lps=ahu["B44"].value, oa_lps=None, source="RA-AHU-REF", code="D",
                 remark="contractor's stated tender figure; Rev A could not trace it"),
            dict(label="Equipment Schedule (Corr 5) sheet 7, AHU-B-03-01OPPHARM-01A/B", flow_lps=ahu["B45"].value, oa_lps=ahu["E45"].value,
                 source="RA-AHU-REF", code="D", remark="VERIFY: read from the PDF text layer in Rev A"),
            dict(label=str(ahu["A46"].value), flow_lps=ahu["B46"].value, oa_lps=ahu["E46"].value, source="RA-AHU-REF", code="D", remark="")]
    flags = [
        dict(type="VERIFY", ref="Airflow Comparison", text="Contractor's TENDER AHU airflow 5,530 L/s is not traceable; the Equipment Schedule (Corr 5) lists AHU-B-03-01OPPHARM-01A/B at 1,455 L/s / OA 523 L/s. VERIFY the tender air-side schematic before accepting any reduction narrative.", source="RA-BOD-B45"),
        dict(type="CONFIRM", ref="Envelope", text="Glazing on the north (grid U) and west frontages is ASSUMED (thin wall line on DD p.18, naturally ventilated colonnade outside). CONFIRM from the architectural elevations; up to about 6 kW of the room sensible load.", source="RA-ENV"),
        dict(type="CONFIRM", ref="Room Information", text="Equipment: fridge and freezer count from DD p.5 at 0.3 kW each and an OPAS allowance of 2.0 kW; CONFIRM with the pharmacy automation vendor.", source="RA-EQ"),
        dict(type="ASSUMPTION", ref="Basis of Design", text="Supply air temperature 14 °C (12 °C off-coil + 2 K fan rise). If rooms must be held at 21 °C the temperature difference falls to 7 K and airflow rises about 40 %.", source="RA-BOD-C28"),
        dict(type="CONFIRM", ref="AHU", text="Outdoor air 2 ACH far exceeds toilet exhaust plus pressurisation leakage; the surplus must be relieved (spill or relief, or transfer to lower-pressure areas).", source="RA-BOD-B45"),
        dict(type="CONFIRM", ref="AHU", text="Contractor tag AHU-B-02-01OPPPHARM-01A&B differs from the tender tag AHU-B-03-01OPPHARM-01A/B; confirm plant location and MCP allocation.", source="RA-BOD-B45"),
        dict(type="VERIFY", ref="Pressure", text="DD p.21 gives Positive for the pharmacy rooms without a pascal value and the contractor shows 0 Pa; the pressure method is not applied to this department.", source="RA-DD21"),
    ]
    documents = []
    for rr in range(8, 16):
        documents.append(dict(document=str(bod[f"B{rr}"].value), role=str(bod[f"C{rr}"].value), issue="", taken=str(bod[f"E{rr}"].value), source="RA-BOD-B45"))
    return {
        "schema_version": 1,
        "project": dict(name="AIGH redevelopment, Contract 2 (Inpatient), ACMV", code="AIGH_C2",
                        revision=dict(rev="A", date=__import__("datetime").date.today().isoformat(), description="Reproduction of the Rev A worked example", by="", checked="", approved="")),
        "department": dict(number="L1-OPP", name="Level 1 Outpatient Pharmacy AHU (Rev A example)"),
        "scope": "Reproduction of the Rev A worked example (451.5 m2, 2,760 L/s room supply) by the workbook generator, with inputs read from the Rev A workbook.",
        "storeys": ["L01"], "storey_labels": {"L01": "Level 1"},
        "documents": documents, "constants": constants, "space_types": sp, "activities": acts, "orientations": ori, "space_type_defaults": defaults,
        "criteria_origin": ["Room criteria: the DD Booklet, Pharmacy (Batch 2), p.21, as cited by the Rev A Basis of Design and carried in the Room Zones cells."],
        "sources": sources, "rooms": rooms, "ahus": [], "fans": [], "terminals": [], "valves": [], "references": refs, "flags": flags,
        "key_flags": [f["text"] for f in flags[:3]],
    }
