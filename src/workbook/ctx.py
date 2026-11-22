"""Build context: constants, defaults, provenance-aware input writer."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from openpyxl import Workbook
from openpyxl.styles import Alignment
from openpyxl.utils import get_column_letter

from .model import Layout, InputError, blank, validate
from .styles import CODE_STYLES, put, tag_variant, link_to

BUILTIN_SOURCES = [
    {"id": "GEN-ENGINE", "document": "Beca NDH Inpatients HVAC Load Calc template and Rev A worked example",
     "issue": "Rev A, 1 Oct 2026", "page": "sheet Level 1 Room Zones, AHU Summary",
     "location": "formula chain and conventions (10 % safety factor, 1.21 / 1.2 / 1.18 air constants)",
     "text_as_read": "template conventions", "method": "Formulas and conventions as set in the template", "confidence": "high"},
    {"id": "GEN-BASIS", "document": "Project design basis: default values for the department workbooks",
     "issue": "22 Nov 2026", "page": "defaults to confirm", "location": "duct leakage 10 %; outdoor-air rule 5 % and 5 pp",
     "text_as_read": "duct leakage 10 %; outdoor-air rule: share 5 %, least drop in fraction 5 percentage points", "method": "Value taken from the project design basis",
     "confidence": "high"},
    {"id": "GEN-PRESSURE", "document": "Project pressure-offset method",
     "issue": "project method", "page": "method", "location": "equations 1 to 6; provisional gap areas",
     "text_as_read": "limit 2.5 m3/h per m2 at 50 Pa; n 0.65; Cd 0.6; air density 1.2 kg/m3",
     "method": "Value set by the engineer", "confidence": "high"},
]

# name: (label, value, unit, code, source, remark)
DEFAULTS: Dict[str, Tuple] = {
    "SafetyFactor": ("Safety factor on room loads", 0.10, "fraction", "S", "GEN-ENGINE", "template convention"),
    "DuctLeakage": ("Duct leakage allowance on AHU supply airflow", 0.10, "fraction", "S", "GEN-BASIS",
                    "10 % default confirmed; any leakage class stated by a project document is a recorded difference"),
    "InfiltrationACH": ("Infiltration of a room with envelope, not pressurised", 0.5, "ACH", "S", "GEN-ENGINE",
                        "template convention"),
    "AirSensibleFactor": ("Air constant, sensible (airflow from sensible load)", 1.21, "W per (L/s K)", "S",
                          "GEN-ENGINE", "template"),
    "AirDensity": ("Air density", 1.2, "kg/m³", "S", "GEN-PRESSURE", "also the density of the door-gap equation"),
    "AirLatentFactor": ("Air constant, latent (infiltration)", 1.18, "W per (L/s kJ/kg)", "S", "GEN-ENGINE", "template"),
    "ConvM3hPerLps": ("Conversion, m³/h per L/s", 3.6, "–", "S", "GEN-ENGINE", "unit conversion"),
    "SecPerHour": ("Seconds per hour", 3600, "s", "S", "GEN-PRESSURE", "door-gap equation"),
    "WperkW": ("Watts per kilowatt", 1000, "W", "S", "GEN-ENGINE", "unit conversion"),
    "LperM3": ("Litres per cubic metre", 1000, "L", "S", "GEN-ENGINE", "unit conversion: airflow L/s to m³/s"),
    "PercentBase": ("Occupancy density base (persons per 100 m²)", 100, "m²", "S", "GEN-ENGINE", "unit conversion"),
    "RoundTo": ("Rounding of final supply (ROUNDUP digits; −1 = 10 L/s)", -1, "digits", "S", "GEN-ENGINE",
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
    "RatioFail": ("Ratio below which a room fails", 1.0, "–", "S", "GEN-PRESSURE", "design offset over offset needed"),
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
                  "numerical guard of the fixed-step solution"),
    "OffsetEps": ("Offset below which no offset is needed", 0.000001, "m³/h", "S", "GEN-PRESSURE", ""),
    "AreaTolPct": ("Area agreement tolerance, per cent", 0.03, "fraction", "S", "GEN-BASIS", "project design basis tolerances"),
    "AreaTolM2": ("Area agreement tolerance, absolute", 0.5, "m²", "S", "GEN-BASIS", "project design basis tolerances"),
    "BalTolPct": ("Flow balance closure tolerance", 0.01, "fraction", "S", "GEN-BASIS", "project design basis tolerances"),
    "BalTolLps": ("Flow balance closure tolerance, absolute", 10, "L/s", "S", "GEN-BASIS", "project design basis tolerances"),
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
    ("AHU totalisation", ["DuctLeakage", "FanPressure", "FanEfficiency", "OAShare", "OADrop", "PAHUOffT", "PAHUOffH"]),
    ("Pressure method", ["AirtightLimit", "TighterLimit", "FlowN", "FlowNLow", "FlowNHigh", "DoorCd", "RefPressure",
                         "RatioFail", "RatioVerify", "ApplyFromPa", "SecPerHour", "MMperM", "CM2perM2", "GapSides",
                         "GapBottom", "GapHinged", "GapSliding", "GapBedWidth", "GapShaft", "NewtonEps", "OffsetEps",
                         "StartPressure", "PressureBracket", "TolPressure"]),
    ("Check tolerances", ["AreaTolPct", "AreaTolM2", "BalTolPct", "BalTolLps", "SchedTolPct", "SchedTolLps"]),
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
                consts[name] = dict(label=label, value=None, unit=unit, code="A", source="GEN-ENGINE",
                                    remark="blank: not given by any document, set before treated outdoor air is used")
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
