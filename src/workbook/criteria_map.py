"""Criteria rows that no name match gives, mapped room type by room type, and the specified rows of the MOT NSC theatres.

A criteria row is never taken on one word that the room name and the booklet row have in common. Where the booklet row of a room type is clear from the
documents, the mapping is listed here with its reason. Such a row has code A (assumption, to confirm) and a remark. Code S is for the operating theatres,
anaesthesia preparation rooms of the MOT NSC (specified airflows, conditions and pressure, from the MOT NSC air-side schematics and the schedule of modes),
and the booklet row is kept in the remark for comparison. The Data Centre booklet is a raster document: its criteria come from the cell register of
``out/registers/criteria_raster_datacentre.csv``.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REG = Path("out/registers")

ICU_BEDROOM = r"^\d Bed Room - (?:ICU|HDU)(?:, Bariatric)?$"

# department -> rules. pat: regular expression on the room name. row: room type as printed in the booklet. pages: booklet pages by suite prefix of the room number.
# regime: when the booklet prints one room type twice with opposite pressure regimes, the regime that fits.
RULES: Dict[str, List[Dict[str, Any]]] = {
    "04": [
        dict(pat=ICU_BEDROOM, row="ICU Bedrooms", reason="ICU and HDU bedroom: the booklet row 'ICU Bedrooms' (positive, 2 outdoor air changes, 10 total air changes)"),
        dict(pat=r"^1 Bed Room - ICU, Iso Type A$", row="ICU Bedroom (NEP ISO)",
             reason="isolation bedroom 'Type A' taken as the negative-pressure isolation bedroom; the positive-pressure variant is the room named 'Iso PP'"),
        dict(pat=r"^Anteroom - ICU, PP$", row="Anteroom (NEP ISO)", regime="POSITIVE",
             reason="anteroom of a positive-pressure isolation bedroom: the second printed row 'Anteroom' (positive)"),
        dict(pat=r"^(?:Medication Room|Pantry - ICU)$", row="Clean Utility/AGV Lift Lobby/Workstation/Corridor/Medication/Pantry",
             reason="the booklet row lists this room type in a group of room types with one set of values"),
    ],
    "22": [
        dict(pat=r"^Patient Bay - (?:Recovery|Holding)\b", row="PACU PACU-Recovery Bays/Reception/FM", pages={"DSOT": (111, 112), "MSOT": (132, 133)},
             alt_row="PACU PACU-Recovery Bays/MEDS/Staff Station/Patient Bay –ISO/CU",
             reason="recovery or holding bay: the booklet row of the PACU recovery bays (neutral, 2 outdoor air changes, 6 total air changes) of the suite"),
        dict(pat=r"^Patient Bay - Enclosed, Contact Isolation", row="PACU PACU-Recovery Bays/MEDS/Staff Station/Patient Bay –ISO/CU", pages={"MSOT": (132, 133)},
             reason="enclosed isolation bay: the booklet row of the PACU recovery bays, which lists the isolation patient bay (neutral)"),
    ],
}


def _f(v: Any) -> Optional[float]:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _read(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def suite_of(number: str) -> str:
    m = re.match(r"[A-Za-z]+", number or "")
    return m.group(0).upper() if m else ""


def _n(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


def mapped_row(dept: str, name: str, number: str, rows: List[Dict[str, str]], regime_of) -> Optional[Tuple[Dict[str, str], str]]:
    """(booklet row, reason) for a room that a department rule maps, or None.

    A rule has a name pattern ``pat``, the booklet room type ``row`` (exact) or ``rowp`` (start of the room type, ignoring punctuation), ``pages`` (page range per
    suite prefix of the room number) or ``pagerange`` (page range for every suite), and ``reason``."""
    dk = dept.zfill(2).lstrip("0").zfill(2)
    for rule in RULES.get(dk, []) + RULES_EXTRA.get(dk, []):
        if not re.search(rule["pat"], name, re.I if rule.get("ic") else 0):
            continue
        pages = rule.get("pages")
        wanted = ([rule["row"]] + ([rule["alt_row"]] if rule.get("alt_row") else [])) if rule.get("row") else []
        if rule.get("rowp"):
            cands = [r for r in rows if _n(r["room_type_as_printed"]).startswith(_n(rule["rowp"]))]
        else:
            cands = [r for r in rows if r["room_type_as_printed"].strip() in wanted]
        rng = rule.get("pagerange")
        if pages is not None:
            suite = suite_of(number)
            if suite not in pages:
                continue
            rng = pages[suite]
        if rng is not None:
            cands = [r for r in cands if rng[0] <= (_f(r["booklet_page"]) or 0) <= rng[1]]
        if rule.get("regime"):
            cands = [r for r in cands if regime_of(r.get("pressure_regime_as_printed", "")) == rule["regime"]]
        if cands:
            cands.sort(key=lambda r: (_f(r["booklet_page"]) or 0))
            return cands[0], rule["reason"]
    return None


# --------------------------------------------------------------------------- #
# MOT NSC theatres
# --------------------------------------------------------------------------- #
def _modes(text: str) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for m in re.finditer(r"(norm|set back|single pass)\s+([+-]?\d+(?:\.\d+)?)", text or ""):
        out[m.group(1)] = float(m.group(2))
    return out


def _tag_key(t: str) -> str:
    t = (t or "").split("|")[0].strip().replace("lSONP", "ISONP")
    return t


class MotTheatres:
    """Specified values of the operating theatres and anaesthesia preparation rooms of the MOT NSC, by DPA room number.

    A DPA room number gives the level and the pod (MSOT: level 5, suites 03 to 05 are pods 1 to 3; DSOT: level 6, suites 02 to 04 are pods 1 to 3).
    The air-side schematic box of the same pod, with the unit tag of the schedule of modes, gives the values. Where the two documents print different unit
    tags for a theatre, the box of the same pod with the same room kind is used and the remark names both tags."""

    POD = {"MSOT": {3: 1, 4: 2, 5: 3}, "DSOT": {2: 1, 3: 2, 4: 3}}
    LEVEL = {"MSOT": "L5", "DSOT": "L6"}

    def __init__(self, root: Path = Path(".")):
        rows = _read(root / REG / "mot_theatre_rooms.csv")
        self.theatre_ahu: Dict[str, str] = {}
        self.theatre_label: Dict[str, str] = {}
        for r in rows:
            m = re.search(r"\((MSOT|DSOT) (\d\d\.\d\d\.\d\d)\)", r["room_name_printed"])
            if m and r["source_kind"].startswith("Appendix J") and not re.search(r"\((?:B)\)\s*$", r["room_name_printed"]):
                key = f"{m.group(1)} {m.group(2)}"
                self.theatre_ahu.setdefault(key, _tag_key(r["ahu_pahu_tag"]))
                self.theatre_label.setdefault(key, r["room_name_printed"])
        self.sch = [r for r in rows if r["source_kind"] == "air-side schematic room box" and r["supply_air"]]
        self._used: set = set()

    @staticmethod
    def _tags(t: str) -> List[str]:
        return [x.strip().replace("lSONP", "ISONP") for x in (t or "").split("|") if x.strip()]

    def _pod_rows(self, suite: str, pod: int, kind: str) -> List[Dict[str, str]]:
        return [r for r in self.sch if r["level"] == self.LEVEL[suite] and r["pod_or_suite"].upper().startswith(f"POD{pod}") and r["room_type"] == kind]

    @staticmethod
    def _kind_word(label: str) -> str:
        m = re.match(r"\s*(?:OR|OPERATING ROOM|OPERATION ROOM)\s*-\s*([A-Za-z]+)", label or "", re.I)
        return m.group(1).upper() if m else ""

    def theatre(self, number: str) -> Optional[Dict[str, Any]]:
        m = re.fullmatch(r"(MSOT|DSOT) (\d\d)\.02\.(\d\d)", number)
        tag = self.theatre_ahu.get(number)
        if not m or not tag:
            return None
        suite, pod = m.group(1), self.POD[m.group(1)].get(int(m.group(2)))
        cands = self._pod_rows(suite, pod, "operating theatre")
        hits = [r for r in cands if tag in self._tags(r["ahu_pahu_tag"])]
        note = ""
        if not hits:
            word = self._kind_word(self.theatre_label.get(number, ""))
            hits = [r for r in cands if word and self._kind_word(r["room_name_printed"]) == word]
            if hits:
                note = (f"; the schedule of modes prints the unit tag {tag} and the schematic prints {', '.join(self._tags(hits[0]['ahu_pahu_tag'])) or 'no tag'}: "
                        "the schematic box of the same pod and room kind is used")
        if not hits:
            return None
        sp = self._spec(hits[0], f"operating theatre {number}, unit {tag}{note}")
        sp["sch_tags"] = self._tags(hits[0]["ahu_pahu_tag"])
        sp["pod"] = pod
        return sp

    def prep(self, number: str, name: str) -> Optional[Dict[str, Any]]:
        m = re.fullmatch(r"(MSOT|DSOT) (\d\d)\.01\.(\d\d)", number)
        if not m:
            return None
        suite, pod_no, n = m.group(1), m.group(2), m.group(3)
        pod = self.POD[suite].get(int(pod_no))
        rows = self._pod_rows(suite, pod, "anaesthetic room")
        if "NP" in name:
            hits = [r for r in rows if not self._tags(r["ahu_pahu_tag"]) and "single pass" in r["supply_air"]]
            return self._spec(hits[0], f"anaesthesia preparation room {number}, negative-pressure suite") if hits else None
        tnum = f"{suite} {pod_no}.02.{n}"
        th = self.theatre(tnum)
        if th is not None and any("ISONP" in t for t in th["sch_tags"]):      # the isolation theatres have the negative-pressure preparation rooms
            tnum = f"{suite} {pod_no}.02.{int(n) + 2:02d}"
            th = self.theatre(tnum)
        if th is None:
            return None
        hits = [r for r in rows if set(self._tags(r["ahu_pahu_tag"])) & set(th["sch_tags"])]
        if not hits:
            return None
        sp = self._spec(hits[0], f"anaesthesia preparation room {number}, unit {th['sch_tags'][0]}")
        sp["sch_tags"] = th["sch_tags"]
        sp["unit_tag"] = self.theatre_ahu.get(tnum)      # the unit pair of the theatre that the preparation room serves (the schematic header is shared by all the units of the pod)
        return sp

    @staticmethod
    def _spec(r: Dict[str, str], what: str) -> Dict[str, Any]:
        sup, oa, ex = _modes(r["supply_air"]), _modes(r["outdoor_air"]), _modes(r["exhaust_air"])
        pa = _modes(r["pressure"])
        t = re.search(r"(\d+)\s*-\s*(\d+)\s*°C", r["temperature"] or "")
        rh = re.search(r"(\d+)\s*%", r["relative_humidity"] or "")
        mode = "norm" if "norm" in sup else "single pass"
        supply, oa_v = sup.get(mode), oa.get(mode)
        cls = re.search(r"CLASS (\d)", r["air_class"] or "")
        return dict(what=what, mode=mode, supply=supply, oa=oa_v, exhaust=ex.get("single pass") if mode == "single pass" else None,
                    pa=pa.get(mode), tmin=float(t.group(1)) if t else None, tmax=float(t.group(2)) if t else None, rh=float(rh.group(1)) if rh else None,
                    air_class=cls.group(1) if cls else None, filter=("HEPA" if "HEPA" in (r["filter"] or "") else None),
                    sheet=r["page_or_sheet"], file=r["source_file"], text=f"supply {r['supply_air']}; outdoor air {r['outdoor_air']}; pressure {r['pressure']}",
                    modes=dict(supply=sup, outdoor_air=oa, exhaust=ex, pressure=pa))


# --------------------------------------------------------------------------- #
# Data Centre booklet (raster read)
# --------------------------------------------------------------------------- #
DC_NAME = [
    (r"^Primary Server Room$", ("86", "Primary Server Room")),
    (r"^Primary Server Room - CRAC$", ("86", "CRAC Room")),
    (r"^Primary Server Room - Operator Room$", ("86", "Operator Room")),
    (r"^Secondary Server Room$", ("87", "Secondary Server Room")),
    (r"^Secondary Server Room - CRAC$", ("87", "CRAC Room")),
    (r"^Battery Room$", ("86", "Battery Room 1")),
    (r"^UPS$", ("86", "UPS Room")),
    (r"^Fire Suppression Room$", ("86", "Gas Suppression Room")),
]


def datacentre_table(root: Path = Path(".")) -> Dict[Tuple[str, str], Dict[str, str]]:
    out: Dict[Tuple[str, str], Dict[str, str]] = {}
    for r in _read(root / REG / "criteria_raster_datacentre.csv"):
        if r["table_kind"] == "criteria":
            out.setdefault((r["booklet_page"], r["row_label"]), {"_pdf_page": r["pdf_page"], "_file": r["source_file"]})[r["column_as_printed"]] = r["value_as_printed"]
    return out


def datacentre_criteria(name: str, area_m2: Optional[float], table: Dict[Tuple[str, str], Dict[str, str]], regime_of) -> Optional[Dict[str, Any]]:
    """Criteria block (without source id and code) for a Data Centre room, from the booklet table read from the raster page."""
    for pat, key in DC_NAME:
        if re.search(pat, name):
            row = table.get(key)
            if row:
                break
    else:
        return None
    page, label = key
    c: Dict[str, Any] = dict(criteria_row=label, page=f"page {page}")
    oa = row.get("Min. Outdoor ACH", "")
    note: List[str] = []
    m = re.search(r"([\d.]+)\s*l/s/m", oa)
    if m and area_m2:
        c["oa_specified_lps"] = round(float(m.group(1)) * area_m2, 1)
        note.append(f"outdoor air {m.group(1)} L/s per m² on {area_m2:g} m²")
    m2 = re.search(r"([\d.]+)\s*l/s/person", oa)
    if m2:
        c["oa_lps_per_person"] = float(m2.group(1))
        note.append(f"outdoor air {m2.group(1)} L/s per person or the area value, whichever is higher")
    elif re.fullmatch(r"\d+(?:\.\d+)?", oa.strip()):
        c["min_oa_ach"] = float(oa)
    tot = row.get("Min. Total ACH", "")
    if re.fullmatch(r"\d+(?:\.\d+)?", tot.strip()):
        c["min_total_ach"] = float(tot)
    elif "smoke" in tot:
        note.append("no total air change in normal operation; 9 air changes only in smoke purging")
    t = re.search(r"(\d+)\s*-\s*(\d+)", row.get("Design Temperature, C", ""))
    if t:
        c["temp_min_c"], c["temp_max_c"] = float(t.group(1)), float(t.group(2))
    rh = re.findall(r"(\d+)\s*%", row.get("Relative Humidity", ""))
    if rh:
        c["rh_max_pct"] = float(rh[-1])
    reg = row.get("Pressure Regime, Pa", "")
    c["pressure_regime"] = regime_of(reg)
    cl = row.get("Air Class", "").strip()
    c["air_class"] = cl if cl.isdigit() else None
    flt = row.get("Filter", "").strip()
    c["filter"] = flt if flt not in ("", "NA", "NR") else None
    sp = row.get("Single Pass", "").strip()
    c["single_pass"] = "Y" if sp.upper().startswith("Y") else None
    if sp.lower().startswith("upon smoke"):
        note.append("single pass only on smoke purging")
    c["_note"] = "; ".join(note)
    c["_pdf_page"] = row.get("_pdf_page", "")
    c["_file"] = row.get("_file", "")
    c["_row"] = row
    return c


# --------------------------------------------------------------------------- #
# Room types that a booklet row names, mapped one by one (code A, to confirm). A room type that no row names stays without a criteria row and is listed.
# --------------------------------------------------------------------------- #
def _r(pat: str, rowp: str, why: str, **kw: Any) -> Dict[str, Any]:
    return dict(pat=pat, rowp=rowp, reason=why, **kw)


_OT_POD = "OT POD Clinical Support"
_LISTED = "the booklet row names this room type among the room types that share its values"
RULES_EXTRA: Dict[str, List[Dict[str, Any]]] = {
    "04": [
        _r(r"^(?:Clean Utility|Corridor|Workstation|Medication Room|Airlock)\b(?!.*Iso)", "Clean Utility/AGV Lift Lobby/Workstation/Corridor", _LISTED),
        _r(r"^Dirty Utility|^Disposal", "Disposal/Dirty Utility", _LISTED),
        _r(r"^Pod - Silent|^Ensuite|^Store\b", "Silent VC/ENS/Pantry Staff", _LISTED),
    ],
    "11": [
        _r(r"^Patient Bay - P2, Acute", "P2 North Patient Bay", "patient bay of the P2 zone; the booklet prints P2 North (negative 10 Pa) and P2 South (negative or neutral) rows, and the room list does not give the part of P2", pagerange=(132, 132)),
        _r(r"^Patient Bay - Acute Treatment, P3", "P3 Patient Bays", "acute patient bay of the P3 zone"),
        _r(r"^Patient Bay - EDTU", "EDTU Patient Bay", "patient bay of the EDTU zone"),
        _r(r"^Isolation - Fever Special", "FEVER Isolation Patient Bays", "isolation bay of the fever zone"),
        _r(r"^Patient Bay - Resuscitation, NP", "RESUS Dual (NP)", "resuscitation bay with negative-pressure isolation"),
        _r(r"^Triage Room", "P3 TRIAGE", "triage room of the P3 zone"),
        _r(r"^Screening Area", "P3 SCREENING", "screening area of the P3 zone"),
        _r(r"^Bay - Asthma", "P3 Asthma Bay", "asthma bay of the P3 zone"),
        _r(r"^Consult Room", "CAC Consultation", "consultation room of the caregiver area", pages={"OCAC": (133, 133)}),
        _r(r"^Consult Room", "P3 Consultation 1", "consultation room of the P3 zone", pages={"EMER": (130, 130)}),
    ],
    "22": [
        _r(r"^\d Bed Room - ASW", "ASW Ward 5 BED", "ward bedroom of the ASW suite", pages={"AMSC": (81, 81)}),
        _r(r"^(?:Staff Station|Clean Utility|Store - Equipment|Pantry)\b", "ASW Ward 5 BED", _LISTED, pages={"AMSC": (81, 81)}),
        _r(r"^1 Bed Room - Sleep Lab", "Sleep Lab", "sleep laboratory bedroom", pages={"AMSC": (81, 81)}),
        _r(r"^(?:Corridor|Meeting Room|Office|Workstation)\b", "Offices/Meeting Room/Central Reporting", _LISTED, pages={"AMSC": (81, 81)}),
        _r(r"^(?:Disposal Room|Dirty Utility|Cleaners Room)", "DU/Cleaner", _LISTED, pages={"AMSC": (81, 81)}),
        _r(r"^(?:Corridor|Meeting Room|Bay - Exit|Store - Equipment|Bay - Cart Holding|Bay - Mobile Equipment|Workstation|Control Room)\b", _OT_POD, _LISTED,
           pages={"DSOT": (112, 112), "MSOT": (133, 133)}),
        _r(r"^Sterile Hold and Preparation", "Sterile Hold & Preparation", "the booklet prints the same room type with '&'", pages={"DSOT": (111, 111), "MSOT": (132, 132)}),
        _r(r"^(?:Patient Bay - Transfer|Staff Station|Clean Utility|Workstation)\b", "Patient Bay-Transfer/Patient Bay-Holding", _LISTED, pages={"DSOT": (112, 112)}),
        _r(r"^(?:Staff Station|Workstation - Write Up)\b", "Pre-OP OPS Command Centre", _LISTED, pages={"MSOT": (133, 133)}),
        _r(r"^(?:Patient Bay - Transfer)\b", "Pre-OP OPS Command Centre", "the booklet row lists the patient bay 'Holding'; the room is a transfer bay of the same suite", pages={"MSOT": (133, 133)}),
        _r(r"^(?:Overnight Bedroom)\b", "Staff- Support/Lounge/Staff rest", _LISTED, pages={"MSOT": (133, 133)}),
    ],
    "23": [
        _r(r"^Patient Bay - Enclosed, Endo", "Enclosed (Endo)", "enclosed patient bay of the endoscopy suite"),
        _r(r"^Patient Bay - Enclosed, Iso Type B", "Enclosed - Iso Type B", "enclosed isolation patient bay"),
        _r(r"^Bay - ", "Bay - Kiosk (Registration", "the booklet row lists this bay among the bays that share its values"),
        _r(r"^Change Cubicle I Toilet - Patient", "Change Cubicle / Toilet (Patient Accessible)", "the room name prints 'I' for '/'"),
        _r(r"^Circulation$", "Circulation / Waiting Area (General)", "the booklet row names circulation"),
        _r(r"^Waiting - Sub", "Circulation / Waiting Area (Sub)", "the booklet row names the waiting area of the sub-suite"),
        _r(r"^Waiting$", "Circulation / Waiting Area (General)", "the booklet row names the waiting area"),
        _r(r"^(?:Pantry - Patient|Lounge, Pantry - Staff)", "Pantry / Lounge", "the booklet row names pantry and lounge"),
        _r(r"^Store - Equipment", "Store (Equipment)", "store of equipment"),
        _r(r"^Disposal Room", "Dirty Utility / Disposal Room", "the booklet row names the disposal room"),
        _r(r"^Water Treatment Room", "RO Water Room", "the booklet room type for reverse-osmosis water equipment"),
    ],
    "26": [
        _r(r"^General X-ray", "X-Ray", "X-ray room", pagerange=(202, 202)),
        _r(r"^Fluoroscopy - Procedure Room", "Fluoroscopy", "fluoroscopy room", pagerange=(203, 203)),
        _r(r"^Control Room - Fluoroscopy", "Fluoroscopy Control Room", "control room of fluoroscopy"),
        _r(r"^Toilet - Barium", "Fluoroscopy Toilet", "toilet of fluoroscopy"),
        _r(r"^CT Angiography$", "CT Angio", "CT angiography room", pagerange=(202, 202)),
        _r(r"^Control Room - CT Angiography", "CT Angio – Control", "control room of CT angiography"),
        _r(r"^Vendor Technical Room - CT Angiography", "CT Angio – Vendor Equipment", "vendor room of CT angiography"),
        _r(r"^Vendor Technical Room - Angiography", "Angio Procedure – Vendor Equipment", "vendor room of angiography"),
        _r(r"^Control Room - MRI", "MRI Control Rooms", "control room of MRI"),
        _r(r"^Patient Bay - MRI", "MRI Patient Bay", "patient bay of MRI"),
        _r(r"^Vendor Technical Room - MRI", "MRI Vendor Equipment", "vendor room of MRI"),
        _r(r"^CT Scanning - Procedure Room", "CT Procedure", "CT scanning room", pagerange=(201, 201)),
        _r(r"^Control Room - CT Scanning", "CT Control Room", "control room of CT scanning"),
        _r(r"^(?:Waiting|Sub-Wait)", "Wait", "waiting area", pagerange=(216, 216)),
        _r(r"^(?:Patient Bay - Recovery|Pantry - Patient Recovery|Staff Station)", "Patient Recovery Bay / Staff Station / Patient Pantry", _LISTED),
        _r(r"^Tech Work Room", "Tech Work", "technician work room", pagerange=(202, 202)),
        _r(r"^Central Reporting Room", "Central Reporting", "central reporting room"),
        _r(r"^Toilet - ", "Toilet", "toilet", pagerange=(201, 201)),
    ],
    "28": [
        _r(r"^Catheter Lab - Procedure Room", "Cath Lab", "catheter laboratory procedure room", pagerange=(86, 86)),
        _r(r"^(?:Catheter Lab Control / Reporting Room|Reporting Room)", "Control Room", "control room", pagerange=(86, 86)),
        _r(r"^Scrub Up Room", "Scrub Room", "scrub room", pagerange=(86, 86)),
        _r(r"^Sterile Prep$", "Sterile Prep Room", "sterile preparation room", pagerange=(86, 86)),
        _r(r"^Disposal Room", "Dirty Utility/Cleaner Store/Disposal", _LISTED),
        _r(r"^Patient Bay - Recovery", "Recovery Bay", "recovery bay"),
    ],
    "29": [
        _r(r"^Histopathologist", "Office – Histopathologist", "office of the histopathologist"),
        _r(r"^Hameotology Automation", "Haematology Automation", "the room name has a spelling difference"),
        _r(r"^Histo & Cyto Open Benches", "Main Histo & Cyto Lab Open Benches", "open benches of histology and cytology"),
        _r(r"^Meeting Room - Combined ISH", "Combined ISH, Multi-Header", "room of the combined in-situ hybridisation microscope"),
        _r(r"^Main/General Microbiology Room", "Main Microbiology", "main microbiology laboratory"),
        _r(r"^Molecular Diagnostic lab-1", "Molecular Diagnostic Lab – 1", "molecular diagnostic laboratory 1"),
        _r(r"^Molecular Diagnostic lab-2", "Molecular Diagnostic Lab - 2", "molecular diagnostic laboratory 2"),
        _r(r"^Disposal$", "Disposal room", "disposal room"),
        _r(r"^UPS$", "UPS Room", "UPS room"),
        _r(r"^Bottles Gas Room", "Bottle Gas Room", "bottle gas room"),
        _r(r"^Compressor Room", "Compressor", "compressor room"),
        _r(r"^Pantry - ", "Pantry – Staff", "staff pantry"),
        _r(r"^Evaluation Room", "Evaluation Room", "evaluation room"),
        _r(r"^POCT", "Office Lab / POCT Room / LIS Room", _LISTED),
        _r(r"^Office - (?:HOD|Head|Clinical Head)", "Office – Microbiologist / Clinical Head / Hod", _LISTED),
        _r(r"^Toilet I Shower", "Toilet | Showers", "the room name prints 'I' for '|'"),
    ],
    "30": [
        _r(r"^Manual Picking|^Pigeon Hole", "Manual Picking, Pigeon Hole", _LISTED),
        _r(r"^Payment I Registration Counter", "Payment, Registration Counter", "the room name prints 'I' for ','"),
        _r(r"^Waiting$", "Wait Area", "waiting area", pagerange=(148, 148)),
        _r(r"^(?:Cleaners Room|Disposal Room)", "Cleaner", "cleaner room", pagerange=(148, 148)),
        _r(r"^Toilet - Accessible", "Toilet", "toilet", pagerange=(148, 148)),
        _r(r"^Locker - Staff", "Locker", "locker room", pagerange=(136, 136)),
        _r(r"^(?:Meeting Room|Office - )", "Office/Meeting Room", _LISTED, pagerange=(136, 136)),
        _r(r"^(?:Compressor Room|IPAS UPS)", "UPS/Compressor Room", _LISTED),
    ],
    "37": [
        _r(r"Workshop", "Workshops (BME, FM)", "workshop", pagerange=(90, 90)),
        _r(r"Equipment Store", "Equipment Store", "equipment store", pagerange=(90, 90)),
    ],
    "41": [
        _r(r"^Decontamination area", "Decontamination", "decontamination area"),
        _r(r"^(?:Receiving Office|CSSD Office|Meeting Room)", "Office/Meeting Room/Technician Room", _LISTED),
        _r(r"^(?:Vendor Receiving|Store - Consumable|Bulk Store|Staff Pantry)", "Support Areas (Staff Facilities / Receiving / Other Store)", _LISTED),
        _r(r"^(?:Cleaners Room|DI Water)", "DI Water / Waste Cleaner Room / Change Room", _LISTED),
        _r(r"^AGV Dispatch \(OT\)$", "Clean Lift Lobby / AVG Dispatch (OT)", "the booklet prints the dispatch of the operating theatres"),
        _r(r"^Airlock \(To Clean Cart Staging\)", "Air Lock Inspection Packing - Clean Cart Staging", "air lock to the clean cart staging area"),
    ],
}
