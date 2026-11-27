"""Build context: constants, defaults, provenance-aware input writer."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from openpyxl import Workbook
from openpyxl.styles import Alignment
from openpyxl.utils import get_column_letter

from .model import Layout, InputError, blank, validate
from .styles import CODE_STYLES, put, tag_variant, link_to

BUILTIN_SOURCES = [
    {"id": "GEN-METHOD", "document": "Beca NDH Inpatients HVAC Load Calc template and Rev A worked example (AIGH_C2_L1_OP_Pharmacy_AHU_Thermal_Load_Calc_RevA.xlsx)",
     "issue": "Rev A (draft for engineer review), 1 Oct 2026, as printed on the Basis of Design sheet of the template", "page": "sheet Level 1 Room Zones, AHU Summary",
     "location": "formula chain and conventions (10 % safety factor, 1.21 / 1.2 / 1.18 air constants)",
     "text_as_read": "template conventions", "method": "Formulas and conventions as set in the template", "confidence": "high"},
    {"id": "GEN-BASIS", "document": "Project design basis: default values for the department workbooks",
     "issue": "22 Nov 2026", "page": "defaults to confirm", "location": "duct leakage 10 %; outdoor-air rule 5 % and 5 pp",
     "text_as_read": "duct leakage 10 %; outdoor-air rule: share 5 %, least drop in fraction 5 percentage points", "method": "Value taken from the project design basis",
     "confidence": "high"},
    {"id": "GEN-PRESSURE", "document": "Project pressure-offset method",
     "issue": "project method", "page": "method", "location": "airtightness limit, leakage exponent, internal surface",
     "text_as_read": "limit 2.5 m3/h per m2 at 50 Pa; n 0.65; air density 1.2 kg/m3",
     "method": "Value set by the engineer", "confidence": "high"},
]

BUILTIN_SOURCES.append(
    {"id": "GEN-PSY", "document": "Psychrometric relations used for the room design condition (saturation pressure, humidity ratio, enthalpy of moist air)",
     "issue": "standard relations", "page": "psychrometrics", "location": "Magnus form of the saturation pressure; moist-air enthalpy",
     "text_as_read": "saturation pressure 0.61094 exp(17.625 t / (243.04 + t)) kPa; humidity ratio 0.622 pv / (101.325 - pv); enthalpy 1.006 t + w (2501 + 1.86 t)",
     "method": "Value set by the engineer", "confidence": "high"})

BUILTIN_SOURCES.append(
    {"id": "EQS-COIL", "document": "5. Equipment Schedules (Corr 5), Air Handling Unit Schedule Part A (cooling coil) and Part B (hot water heating)",
     "issue": "Corrigendum 5", "page": "AHU-A and AHU-B sheets (pages 13 to 28)", "location": "columns Cooling Coil / Air Side, Cooling Coil / Water Side, Re-Heating Coil / Air Side, Re-Heating Coil / Water Side; FCU and CRAC schedules, column Min External Static Pressure",
     "text_as_read": "cooling coil LATDB 12.0 °C, LATWB 11.0 °C, EWT 8.0 °C, LWT 16.0 °C; re-heating coil EAT 12.0 °C, max LAT 24 °C, EWT 35 °C, LWT 25 °C",
     "method": "Read from the schedule register (most printed value of each column)", "confidence": "high"})
BUILTIN_SOURCES.append(
    {"id": "HFDS-OT", "document": "HFDS - Mechanical reverse-brief.pdf (MOHH Healthcare Facility Design Standards, Vol 2 Module 01)",
     "issue": "uploaded 23 Nov 2026 (again 26 Nov 2026)", "page": "table T.1.1.1.1, page 39 (PDF page 8)", "location": "row 24 Operating Theatre",
     "text_as_read": "Operating Theatre 18-25 °C, 40-60 %, Positive, 5 / 25 ACH, 24 Hrs, system mode of operation a) Single pass b) Recirc",
     "method": "Read from the HFDS table", "confidence": "high"})
BUILTIN_SOURCES.append(
    {"id": "GEN-COIL", "document": "Coil estimate method: water flow from duty and water temperature rise; coil rows from the number of transfer units of a counterflow coil",
     "issue": "method of the engineer", "page": "method", "location": "cooling coil: log-mean enthalpy difference between the air and saturated air at the water temperature; reheat coil: log-mean temperature difference",
     "text_as_read": "rows = transfer units ÷ transfer units per row (0.45 per row assumed, to confirm at coil selection)",
     "method": "Estimate. The coil selection of the manufacturer governs", "confidence": "low"})

BUILTIN_SOURCES.append(
    {"id": "GEN-CLASS", "document": "Project rule for single-pass rooms (engineer's instruction of 23 Nov 2026)",
     "issue": "project rule", "page": "single-pass rooms", "location": "clean or sterile supply against dirty rooms",
     "text_as_read": "A single-pass room sets the outdoor-air fraction of the unit only where it needs 100 % outdoor air for clean or sterile supply. A dirty room that needs no sterile air "
                     "takes recirculated supply from the unit and a dedicated exhaust with no return.",
     "method": "Rule set by the engineer. The class of each room is an assumption, to confirm", "confidence": "medium"})

BUILTIN_SOURCES.append(
    {"id": "ARCH-ELEV", "document": "Building elevations AR-IP-31-ZZ-001 to -008 (north, south, east, west and the four internal elevations, IP), Rev --, 1:250",
     "issue": "uploaded 26 Nov 2026", "page": "one sheet per elevation", "location": "glazed panels (filled panes and halftone panes with glass ticks), storey bands between the drawn FFL level lines",
     "text_as_read": "glass m² per room and facade sector: glass per metre of each elevation at each storey x the length of the room's facade, checked against the AHU zoning plans "
                     "(forensic envelope review D-120, register src/arch/envelope_overrides_d120.json)",
     "method": "Measured from the elevation vectors; corrections of the review applied", "confidence": "medium"})

BUILTIN_SOURCES.append(
    {"id": "ENG-20261127", "document": "Engineer's instruction of 27 Nov 2026 (Marcus Simioni), mark-up of the L6 AHU zoning of AHU-B-06-06STAFF-01",
     "issue": "27 Nov 2026", "page": "mark-up", "location": "AGV lift lobbies LL ALLIP.06.01 / 06.02 and Multipurpose Room - Admin ANWSIP6 AM.06.03",
     "text_as_read": "Omit lift shafts and correct multipurpose rooms area",
     "method": "Rule set by the engineer: the room area is the DPA area (the measured outline took in the lift shafts, or is smaller than the room "
               "drawn on part plan AR-IP-02-06-011 Rev A, 06.IP.ANWSIP6.001 AREA = 9)", "confidence": "high"})

# name: (label, value, unit, code, source, remark)
DEFAULTS: Dict[str, Tuple] = {
    "SafetyFactor": ("Safety factor on room loads", 0.10, "fraction", "S", "GEN-METHOD", "template convention"),
    "DuctLeakage": ("Duct leakage allowance on AHU supply airflow", 0.10, "fraction", "S", "GEN-BASIS",
                    "10 % default confirmed; any leakage class stated by a project document is a recorded difference"),
    "TenderLeakage": ("Duct leakage stated on the tender schematics (comparison only)", 0.15, "fraction", "S", "GEN-BASIS",
                      "air-side schematics: stated value, used only in the Remarks wording; no calculation uses it"),
    "InfiltrationACH": ("Infiltration of a room with envelope, not pressurised", 0.5, "ACH", "S", "GEN-METHOD",
                        "template convention"),
    "AirSensibleFactor": ("Air constant, sensible (airflow from sensible load)", 1.21, "W per (L/s K)", "S",
                          "GEN-METHOD", "template"),
    "AirDensity": ("Air density", 1.2, "kg/m³", "S", "GEN-PRESSURE", "template convention"),
    "AirLatentFactor": ("Air constant, latent (infiltration)", 1.18, "W per (L/s kJ/kg)", "S", "GEN-METHOD", "template"),
    "ConvM3hPerLps": ("Conversion, m³/h per L/s", 3.6, "–", "S", "GEN-METHOD", "unit conversion"),
    "SecPerHour": ("Seconds per hour", 3600, "s", "S", "GEN-PRESSURE", "unit conversion"),
    "WperkW": ("Watts per kilowatt", 1000, "W", "S", "GEN-METHOD", "unit conversion"),
    "LperM3": ("Litres per cubic metre", 1000, "L", "S", "GEN-METHOD", "unit conversion: airflow L/s to m³/s"),
    "PercentBase": ("Occupancy density base (persons per 100 m²)", 100, "m²", "S", "GEN-METHOD", "unit conversion"),
    "RoundTo": ("Rounding of final supply (ROUNDUP digits; −1 = 10 L/s)", -1, "digits", "S", "GEN-METHOD",
                "as the example"),
    "AreaDecimals": ("Decimal places of the area used (areas are taken to 0.1 m²)", 1, "digits", "S", "GEN-BASIS",
                     "areas are taken to 0.1 m²; airflows are calculated unrounded"),
    "MMperM": ("Millimetres per metre", 1000, "mm", "S", "GEN-PRESSURE", "unit conversion"),
    "CM2perM2": ("Square centimetres per square metre", 10000, "cm²", "S", "GEN-PRESSURE", "unit conversion"),
    "OAShare": ("Outdoor-air rule: largest share of AHU supply a separated room may take", 0.05, "fraction", "S",
                "GEN-BASIS", "project design basis, to confirm"),
    "OADrop": ("Outdoor-air rule: least drop in AHU outdoor-air fraction", 0.05, "fraction", "S", "GEN-BASIS",
               "project design basis, to confirm"),
    "AirtightLimit": ("Envelope airtightness limit at the reference pressure", 2.5, "m³/h per m²", "S",
                      "GEN-PRESSURE", "doors included; general notes of the air-side schematics"),
    "TighterLimit": ("Tighter-room allowance at the reference pressure", 1.0, "m³/h per m²", "S",
                     "GEN-PRESSURE", "second end of the venturi range"),
    "FlowN": ("Flow exponent n", 0.65, "–", "S", "GEN-PRESSURE", "project method"),
    "FlowNLow": ("Flow exponent, sensitivity low", 0.50, "–", "S", "GEN-PRESSURE", "sensitivity"),
    "FlowNHigh": ("Flow exponent, sensitivity high", 0.75, "–", "S", "GEN-PRESSURE", "sensitivity"),
    "DoorCd": ("Door-gap discharge coefficient", 0.6, "–", "S", "GEN-PRESSURE", "project method"),
    "RefPressure": ("Reference pressure of the airtightness limit", 50, "Pa", "S", "GEN-PRESSURE", "project method"),
    "RatioFail": ("Ratio below which a room fails", 1.0, "–", "S", "GEN-PRESSURE", "offset used in Airflow over offset needed"),
    "RatioVerify": ("Ratio below which a room is flagged VERIFY", 1.2, "–", "S", "GEN-PRESSURE", ""),
    "ApplyFromPa": ("Pressure at or beyond which the airtightness rule applies", 5, "Pa", "S", "GEN-PRESSURE", "±"),
    "GapSides": ("Unsealed door: gap at the sides and top", 3, "mm", "A", "GEN-PRESSURE", "first value, to confirm"),
    "GapBottom": ("Unsealed door: gap at the bottom", 10, "mm", "A", "GEN-PRESSURE", "first value, to confirm"),
    "GapHinged": ("Provisional gap area, hinged sealed door", 15, "cm²", "A", "GEN-PRESSURE",
                  "until a test or door schedule replaces it"),
    "GapSliding": ("Provisional gap area, sliding sealed door", 10, "cm²", "A", "GEN-PRESSURE", ""),
    "GapBedWidth": ("Provisional gap area, bed-width sealed door", 20, "cm²", "A", "GEN-PRESSURE", ""),
    "GapShaft": ("Provisional gap area, shaft access door", 10, "cm²", "A", "GEN-PRESSURE", ""),
    "NewtonEps": ("Smallest pressure difference used in the iteration slope", 0.01, "Pa", "S", "GEN-PRESSURE",
                  "smallest pressure difference used in the slope of the iteration step"),
    "OffsetEps": ("Offset below which no offset is needed", 0.000001, "m³/h", "S", "GEN-PRESSURE", ""),
    "AreaTolPct": ("Area agreement tolerance, per cent", 0.03, "fraction", "S", "GEN-BASIS", "project design basis tolerances"),
    "AreaTolM2": ("Area agreement tolerance, absolute", 0.5, "m²", "S", "GEN-BASIS", "project design basis tolerances"),
    "BalTolPct": ("Flow balance closure tolerance", 0.01, "fraction", "S", "GEN-BASIS", "project design basis tolerances"),
    "BalTolLps": ("Flow balance closure tolerance, absolute", 10, "L/s", "S", "GEN-BASIS", "project design basis tolerances"),
    "ZoneCoverMin": ("Share of the zoned area that the counted rooms of a unit must reach before the room list of the unit is complete", 0.9, "fraction", "A",
                     "GEN-BASIS", "to confirm"),
    "PerimFactor": ("Perimeter factor of a room with no measured outline (perimeter = factor × square root of area)", 4, "–", "A", "GEN-PRESSURE",
                    "square room; assumed, to confirm"),
    "ZoneCoverMax": ("Largest share of the zoned area that the counted rooms of a unit may reach before the room list is not accepted", 1.1, "fraction", "A",
                     "GEN-BASIS", "to confirm: counted area between 0.9 and 1.1 of the zoned area"),
    "FcuLeakageDucted": ("Duct leakage allowance on a ducted fan coil unit", 0.10, "fraction", "A", "GEN-BASIS",
                         "no document gives the duct route of a fan coil unit; the AHU value is used, to confirm"),
    "FcuLeakageHiWall": ("Duct leakage allowance on a hi-wall (non-ducted) fan coil unit", 0.0, "fraction", "A", "GEN-BASIS",
                         "no ducts, so no duct leakage"),
    "DesignTRef": ("Reference design dry-bulb temperature of a room", 24, "°C", "S", "GEN-BASIS",
                   "used where the printed range of the room contains it; else the nearest limit of the range"),
    "DesignRHMax": ("Upper bound of the design humidity of a room", 0.60, "fraction", "S", "GEN-BASIS",
                    "the design humidity is the smaller of this value and the printed maximum"),
    "PsyA": ("Saturation pressure: coefficient", 0.61094, "kPa", "S", "GEN-PSY", "psychrometric relation"),
    "PsyB": ("Saturation pressure: exponent factor", 17.625, "–", "S", "GEN-PSY", "psychrometric relation"),
    "PsyC": ("Saturation pressure: temperature constant", 243.04, "°C", "S", "GEN-PSY", "psychrometric relation"),
    "PsyP": ("Atmospheric pressure", 101.325, "kPa", "S", "GEN-PSY", "standard atmosphere"),
    "PsyEps": ("Ratio of molar masses of water and dry air", 0.622, "–", "S", "GEN-PSY", "psychrometric relation"),
    "PsyCpA": ("Specific heat of dry air", 1.006, "kJ/(kg K)", "S", "GEN-PSY", "psychrometric relation"),
    "PsyCpV": ("Specific heat of water vapour", 1.86, "kJ/(kg K)", "S", "GEN-PSY", "psychrometric relation"),
    "PsyHfg": ("Latent heat of water at 0 °C", 2501, "kJ/kg", "S", "GEN-PSY", "psychrometric relation"),
    "PsyPct": ("Per cent base (humidity printed in per cent)", 100, "%", "S", "GEN-PSY", "unit conversion"),
    "FcuFanPressure": ("Fan pressure of a fan coil unit or a CRAC unit (external static of the tender schedule)", 200, "Pa", "D", "EQS-COIL",
                       "Equipment Schedule: fan coil units 150 or 200 Pa, CRAC units 150 Pa minimum external static; the larger value is used; the internal resistance of the unit is not in it, to confirm"),
    "OTNormalOAACH": ("Operating theatre, normal (recirculation) mode: outdoor air changes", 5, "ACH", "D", "HFDS-OT",
                      "HFDS T.1.1.1.1 row 24 Operating Theatre: 5 / 25 ACH, system mode a) single pass, b) recirculation (D-117)"),
    "CoilLATDB": ("Cooling coil leaving air dry bulb (tender schedule)", 12, "°C", "D", "EQS-COIL", "Equipment Schedule, AHU Part A"),
    "CoilLATWB": ("Cooling coil leaving air wet bulb (tender schedule)", 11, "°C", "D", "EQS-COIL", "Equipment Schedule, AHU Part A"),
    "ChwEWT": ("Chilled water entering the coil", 8, "°C", "D", "EQS-COIL", "Equipment Schedule, AHU Part A"),
    "ChwLWT": ("Chilled water leaving the coil", 16, "°C", "D", "EQS-COIL", "Equipment Schedule, AHU Part A"),
    "HwEWT": ("Reheat water entering the coil", 35, "°C", "D", "EQS-COIL", "Equipment Schedule, AHU Part B"),
    "HwLWT": ("Reheat water leaving the coil", 25, "°C", "D", "EQS-COIL", "Equipment Schedule, AHU Part B"),
    "WaterCp": ("Specific heat of water", 4.186, "kJ/(kg K)", "S", "GEN-COIL", "physical property"),
    "WaterDensity": ("Density of water", 1, "kg/L", "S", "GEN-COIL", "physical property"),
    "HeatCoilNTUPerRow": ("Transfer units of a reheat coil per row (coil row estimate)", 0.45, "–", "A", "GEN-COIL", "assumed, to confirm at coil selection"),
    "CoilTol": ("Smallest difference used in the coil estimate (numerical)", 0.001, "–", "S", "GEN-COIL", "numerical only"),
    "CoilNTUPerRow": ("Transfer units of a coil per row (coil row estimate)", 0.45, "–", "A", "GEN-COIL", "assumed, to confirm at coil selection"),
    "EnvCheckShare": ("Envelope rooms: share of the air-change airflow within which the thermal case is marked for a peak solar check", 0.20, "fraction", "A",
                      "GEN-BASIS", "to confirm"),
    "SchedTolPct": ("Tolerance on an airflow increase against a schematic", 0.05, "fraction", "S", "GEN-BASIS", "project design basis"),
    "SchedTolLps": ("Tolerance on an airflow increase against a schematic, absolute", 10, "L/s", "S", "GEN-BASIS", "project design basis"),
    "StartPressure": ("Iteration starting pressure of a free space", 0, "Pa", "S", "GEN-PRESSURE", "numerical start only"),
    "PressureBracket": ("Search bracket of the uncontrolled-pressure bisection", 1000, "Pa", "S", "GEN-PRESSURE", ""),
    "TolPressure": ("Pressure-iteration residual tolerance", 0.5, "m³/h", "S", "GEN-PRESSURE",
                    "free-space balance residual that passes"),
}
REQUIRED = ["SupplyTemp", "OutdoorDB", "OutdoorWB", "OutdoorH", "FanPressure", "FanEfficiency", "RoofSolarGain"]
OPTIONAL_BLANK = {
    "PAHUOffT": ("PAHU off-coil temperature (treated outdoor air to an FCU)", "°C"),
    "PAHUOffH": ("PAHU off-coil enthalpy (treated outdoor air to an FCU)", "kJ/kg"),
}
GROUPS = [
    ("Load method", ["SafetyFactor", "SupplyTemp", "InfiltrationACH", "RoofSolarGain", "OutdoorDB", "OutdoorWB", "OutdoorH",
                     "AirSensibleFactor", "AirDensity", "AirLatentFactor", "RoundTo", "AreaDecimals", "ConvM3hPerLps", "PercentBase",
                     "WperkW", "LperM3"]),
    ("AHU totalisation", ["DuctLeakage", "FcuLeakageDucted", "FcuLeakageHiWall", "TenderLeakage", "FanPressure", "FcuFanPressure", "FanEfficiency", "OAShare", "OADrop", "PAHUOffT", "PAHUOffH", "OTNormalOAACH"]),
    ("Pressure method", ["AirtightLimit", "TighterLimit", "FlowN", "FlowNLow", "FlowNHigh", "DoorCd", "RefPressure",
                         "RatioFail", "RatioVerify", "ApplyFromPa", "SecPerHour", "MMperM", "CM2perM2", "PerimFactor", "GapSides",
                         "GapBottom", "GapHinged", "GapSliding", "GapBedWidth", "GapShaft", "NewtonEps", "OffsetEps",
                         "StartPressure", "PressureBracket", "TolPressure"]),
    ("Check tolerances", ["AreaTolPct", "AreaTolM2", "BalTolPct", "BalTolLps", "SchedTolPct", "SchedTolLps", "ZoneCoverMin", "ZoneCoverMax", "EnvCheckShare"]),
    ("Coils and water", ["CoilLATDB", "CoilLATWB", "ChwEWT", "ChwLWT", "HwEWT", "HwLWT", "WaterCp", "WaterDensity", "CoilNTUPerRow", "HeatCoilNTUPerRow", "CoilTol"]),
    ("Room design condition", ["DesignTRef", "DesignRHMax", "PsyA", "PsyB", "PsyC", "PsyP", "PsyEps", "PsyCpA", "PsyCpV", "PsyHfg", "PsyPct"]),
]


class Ctx:
    """Everything the tab writers share."""

    def __init__(self, data: Dict[str, Any]):
        self.data = data
        self.wb: Optional[Workbook] = None
        self.lay: Dict[str, Layout] = {}
        self.names: Dict[str, str] = {}
        self.src_row: Dict[str, int] = {}
        self.coded: List[Dict[str, Any]] = []
        self.assumptions: List[Dict[str, Any]] = []
        self.auto_flags: List[Dict[str, Any]] = []
        self.src_ranges: List[str] = []
        self.sources: List[Dict[str, Any]] = []
        self.constants: Dict[str, Dict[str, Any]] = {}
        proj = data.get("project", {})
        self.project = proj.get("name", "Project")
        rev = proj.get("revision", {})
        self.rev = rev.get("rev", "A")
        self.date = rev.get("date", "")
        self.rooms: List[Dict[str, Any]] = []
        self.storeys: List[str] = []
        self.merge_inputs()

    # -- preparation ------------------------------------------------------- #
    def merge_inputs(self) -> None:
        data = self.data
        ids = {s["id"] for s in data.get("sources", [])}
        data["sources"] = list(data.get("sources", [])) + [s for s in BUILTIN_SOURCES if s["id"] not in ids]
        consts: Dict[str, Dict[str, Any]] = {}
        for name, (label, val, unit, code, src, rem) in DEFAULTS.items():
            consts[name] = dict(label=label, value=val, unit=unit, code=code, source=src, remark=rem)
        for name, c in (data.get("constants") or {}).items():
            base = consts.get(name, {})
            base.update({k: v for k, v in c.items()})
            consts[name] = base
        for name in REQUIRED:
            if name not in consts or blank(consts[name].get("value")):
                raise InputError(f"design constant '{name}' is required (with source id); unknown values are not invented")
        for name, (label, unit) in OPTIONAL_BLANK.items():
            if name not in consts:
                consts[name] = dict(label=label, value=None, unit=unit, code="A", source="GEN-METHOD",
                                    remark="blank: not given by any document, set before treated outdoor air is used")
        from .singlepass import classify_single_pass
        for r in data["rooms"]:
            crit = r.get("criteria") or {}
            if crit and str(crit.get("single_pass") or "").upper() == "Y" and not crit.get("sp_class"):
                crit["sp_class"] = classify_single_pass(str(r.get("name") or ""), crit)
        validate(data)
        self.constants = consts
        self.sources = data["sources"]
        for i, s in enumerate(self.sources):
            self.src_row[s["id"]] = 7 + i
        order = data.get("storeys") or []
        rooms = list(data["rooms"])
        seen = []
        for r in rooms:
            if r["storey"] not in seen:
                seen.append(r["storey"])
        self.storeys = [s for s in order if s in seen] + [s for s in seen if s not in order]
        self.rooms = sorted(rooms, key=lambda r: self.storeys.index(r["storey"]))
        self.level_label = data.get("storey_labels", {})

    def label(self, storey: str) -> str:
        return self.level_label.get(storey, storey)

    def sheet_for(self, storey: str) -> str:
        return f"{self.label(storey)} Room Zones"[:31]

    # -- writing ----------------------------------------------------------- #
    def src_link(self, ws, ref: str, src: str) -> None:
        c = ws[ref]
        if src in self.src_row:
            link_to(c, "Sources", f"A{self.src_row[src]}")

    def inp(self, ws, vref: str, cref: str, sref: str, value: Any, code: str, src: str, *, fmt: Optional[str] = None,
            kind: str = "num", label: str = "", room: str = "") -> None:
        """Write an input with its code and source id beside it (blank when the value is unknown)."""
        has = not blank(value)
        style = CODE_STYLES[code][0] if (has and code) else "Input"
        if kind in ("tag",):
            style = tag_variant(style)
        c = put(ws, vref, value if has else None, style, fmt)
        if kind in ("text", "tag"):
            c.alignment = Alignment(horizontal="left", vertical="center")
        put(ws, cref, code if has else None, "Code")
        put(ws, sref, src if has else None, "Tag")
        if has and src:
            self.src_ranges.append(sref)
            self.src_link(ws, sref, src)
        if has:
            self.coded.append(dict(sheet=ws.title, cell=vref, code_cell=cref, src_cell=sref, code=code))
            if code == "A":
                self.assumptions.append(dict(sheet=ws.title, cell=vref, label=label, value=value, room=room, src=src))

    def put_code_cell(self, ws, vref: str, cref: str, style: str, value: Any, code: str, fmt=None, kind="num") -> None:
        """Formula cell with a code letter beside it."""
        if kind == "tag":
            style = tag_variant(style)
        c = put(ws, vref, value, style, fmt)
        if kind in ("text", "tag"):
            c.alignment = Alignment(horizontal="left", vertical="center")
        put(ws, cref, code, "Code")
        self.coded.append(dict(sheet=ws.title, cell=vref, code_cell=cref, src_cell=None, code=code))

    def define(self, name: str, ref: str) -> None:
        from openpyxl.workbook.defined_name import DefinedName
        self.wb.defined_names[name] = DefinedName(name, attr_text=ref)
        self.names[name] = ref
