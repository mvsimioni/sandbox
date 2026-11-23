"""Build a department input JSON from the project data tables (out/registers/*.csv).

Rooms come from the DPA (designed area), limited to the units that the stacking diagram puts in the IP block. The SOA area is shown
beside the DPA area. Measured area, AHU and class come from the plan rooms (room_polygons and zone_assignment) where a plan room matches
a DPA room by name or abbreviation, department and storey. What does not match, on either side, is listed on Flags and left blank:
nothing is forced. Criteria rows are matched to the room type by name; a room with no row in its own department takes the row of the same
space in the closest other department (D-026, presumed criteria). Occupancy follows the RDS (D-016, D-018) and, where the RDS
field is empty or the room has no RDS, the default density by room use (D-017). Plug load and lighting come from the ASHRAE values of the
room use where no project document gives one (D-025). Every room with a pressure target in Pa gets a Pressure row from its internal surface
(D-028): no door data is used.
"""

from __future__ import annotations

import csv
import difflib
import glob
import hashlib
import json
import math
import re
import threading
from functools import lru_cache
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import conf
from . import round5 as r5
from . import hfds as hf
from . import envelope as envm
from .fans import department_fans
from .linking import link_rds, link_rls, load_rls, plan_links, plan_system, rls_source_fields, split_code
from .reva import build_reva_input

REG = Path("out/registers")
UNCLASSIFIED = "UNCLASSIFIED"
AHU_COLUMNS = ("ahu", "ahu_tag", "ahu_or_system", "system_tag")
ZONE_COLUMNS = ("zone_tag", "zone", "zone_id")
SYSTYPE_COLUMNS = ("system_type", "systype")
SYSTEM_KINDS = {"AHU", "PAHU", "FCU", "DXFCU", "CRAC"}
SCOPE_STOREYS = ("LB3", "B3", "B2", "B1", "L01", "L02", "L03", "L04", "L05", "L06", "L07")

# Room use for the default occupant density (D-017): the first rule that matches the room name gives the use (a key of
# occupancy.density_default.per_100m2 in the project configuration). The mapping is an assumption and is stated on Basis of Design.
USE_RULES: List[Tuple[str, str, str]] = [
    (r"hand ?-?wash|scrub bay|scrub up", "corridor_circulation", "hand-wash bays (a recess of the corridor, not an occupied room)"),
    (r"change room|change cubicle", "toilet", "changing rooms (including the lockers that they hold)"),
    (r"^(?!.*(?:smart|self pick)).*locker", "", "locker rooms (the density table has no row for them)"),
    (r"operator room", "office", "operator rooms (staff workstations of a server room)"),
    (r"lecture|\blect\b", "lecture_classroom", "lecture rooms and lecture theatres"),
    (r"sub-?wait|waiting|\bwait\b|reception|counter|kiosk|dispensing|payment|self pick|service centre|registration", "reception_waiting", "reception, waiting and counters"),
    (r"toilet|\bwc\b|shower|ensuite|changing|change room|change cubicle|sanitary", "toilet", "toilets and changing rooms"),
    (r"plant|\bups\b|compressor|riser|shaft|\bahu\b|electrical|comms|server|\bpts\b|\bdb\b|substation|generator|switch ?room|battery|"
     r"transformer|rainwater|\btank\b|\bpump|water treatment|equipment room|automation system|filter room|fire suppression|gas cylinder|control panel|machine room", "plant_room", "plant, equipment, electrical and communication rooms"),
    (r"cleaner|cleaning|janitor|linen|\bstore\b|storage|stock|refrigerat|archive|trolley|bulk|warehouse|\bstr\b|refuse|disposal|waste|"
     r"loading bay|deboxing|equipment store|pigeon|utility|cold room|storeroom|inventory|manual library|cart staging|receiving|\bppe\b|recycl|outreach supplies|"
     r"equipment under repair|clean up room|chemical wate|detergent|\bdi water|supply station|boots|clogs|bin centre|pest control|freezer|chiller|fridge|cylinder|shelving|film management", "storage", "stores, cleaners rooms, utility rooms and waste rooms"),
    (r"corridor|circulation|lobby|airlock|air lock|sound lock|vestibule|lift|concourse|\bcorr\b|agv bay|^bay\s*-|(?:c-arm|amr|bed|stroller|wheelchair) park", "corridor_circulation",
     "corridors, lobbies, airlocks and bays that are recesses of a corridor"),
    (r"workroom|work room", "office", "clinical work rooms"),
    (r"anaesthe|anesthe", "medical_procedure", "anaesthesia preparation rooms (nearest listed use: procedure room)"),
    (r"recovery|\bicu\b|\bhdu\b|intensive", "recovery_icu", "recovery and intensive care"),
    (r"patient bay|\bbay\b.*(?:acute|edtu|resus|p2|holding)|isolation|acute|resus|ward|bed room|1-bed|single bed|patient room|\bholding\b", "patient_room", "patient rooms and patient bays"),
    (r"theatre|operating", "operating_room", "operating theatres"),
    (r"procedure|treatment|therapeutic|examination|exam room|\bexam\b|consult|endoscop|scan|clinic|triage|medication room|screening", "medical_procedure", "procedure, screening and consulting rooms"),
    (r"pantry|break|tea point|kitchenette|lounge|bev|staff accommodation|rest room|staff room|sleep pod|overnight bedroom|on-?call", "break_room_pantry", "pantries, break rooms, staff rooms and sleep pods"),
    (r"meeting|conference|focus|interview|huddle|roll call|multipurpose|multi-purpose", "conference_meeting", "meeting rooms and multipurpose rooms"),
    (r"pre-?function|foyer", "reception_waiting", "pre-function areas and foyers"),
    (r"office|\boff\b|workstation|write-up|writeup|admin|technician|workspace|desk|supervisor|control room|lactation|nursing room|nurse station|staff station|workshop|workroom|work room|"
     r"command centre|research room|reporting room|\blis room|projection room|\bpacs\b|\bris\b|digiti[sz]ation", "office", "offices, workstations and small private rooms"),
    (r"x-?ray|\bmri\b|ultrasound|\bct\b|fluoro|angio|mammo|cath lab|nuclear|\bpet\b|dexa|\bbmd\b|imaging|radiograph", "medical_procedure",
     "imaging rooms (nearest listed use: procedure room)"),
    (r"simulation|training|\btrg\b|classroom|seminar", "classroom_training", "training rooms"),
    (r"picking|repackag|automation|asrs|ipas|extemporaneous|compounding|sterile prep|pharmacy|controlled drug|drug room|clean room|"
     r"(?=.*(?:pharm|cytotox|\btpn\b|aseptic|hazardous))(?:gown|ante ?room|preparation|\bprep\b)", "pharmacy_prep", "pharmacy preparation, picking and automation rooms (pharmacy preparation area)"),
    (r"laborator|\blab\b|bench|haematology|hameotology|chemistry|histopatholog|specimen|blood bank|grossing|tissue processor|bone saw|mycology|\bpcr\b|processing room", "laboratory", "laboratories and laboratory benches"),
    (r"decontamination|washing|wash/dry|laundry|inspection|packing|steril|reprocess|endoscope|assembly|workstation zone",
     "process_area", "process rooms such as decontamination, washing, inspection and packing (light industrial, nearest listed use)"),
    (r"\brtl\b|retail|shop|store front", "retail_sales", "retail areas"),
    (r"kitchen|cooking|pot wash|cook chill|pastry|bakery|plating", "kitchen_cooking", "kitchens and food preparation rooms"),
    (r"cafeteria|dining|canteen", "cafeteria_dining", "dining areas"),
]

# Abbreviations printed on the Appendix D plans, expanded for the use mapping only (D-017). A name that no rule matches is tried again
# with each word expanded. Words that no document explains are not expanded: the room stays 'not set' and is listed on Flags.
APPD_ABBREVIATIONS: Dict[str, str] = {
    "elec": "electrical room", "batt": "battery room", "m&e": "plant room", "svc": "service plant room", "tfr": "transformer room",
    "lv": "low voltage switch room", "hv": "high voltage switch room", "lan": "comms room", "tel": "comms room", "elv": "comms room",
    "san": "sanitary", "chg": "change room", "mtg": "meeting room", "lact": "lactation room", "accom": "accommodation",
    "cln": "cleaner room", "cu": "clean utility", "du": "dirty utility", "eqpt": "equipment store", "ctrl": "control room",
    "proc": "procedure room", "int": "interview room", "nurs": "nurse station", "tech": "technician", "wkshp": "workshop",
    "agv": "agv bay", "ll": "lift lobby", "tll": "lift lobby", "iso": "isolation", "off": "office", "lect": "lecture", "rm": "room",
    "str": "store", "trg": "training room", "rtl": "retail", "refuse": "refuse store", "cln": "cleaner room", "lounge": "lounge",
    "multi": "multipurpose room",
}


def dept_label(key: str, root: Path = Path(".")) -> str:
    """Department name: src/booklet_departments.json department_label, else the DD Sign-OFF folder title kept in dept_units.json."""
    try:
        files = json.loads((Path(root) / "src" / "booklet_departments.json").read_text(encoding="utf-8")).get("files", {})
    except OSError:
        files = {}
    for v in files.values():
        if v.get("dept_no") == key and v.get("department_label"):
            return v["department_label"]
    from .dept_units import load_raw
    return load_raw().get("department_names", {}).get(key, "")


from .tag_notes import cath_tag_note, ucv_unit_note, unit_note  # noqa: E402


def _norm(s: Optional[str]) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _words(s: Optional[str]) -> List[str]:
    return re.findall(r"[a-z0-9]+", (s or "").lower())


def _f(v: Any) -> Optional[float]:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _read(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _regime(text: str) -> Optional[str]:
    from .presume import printed_regime
    return printed_regime(text)


def _pa(text: Optional[str]) -> Optional[float]:
    m = re.match(r"\s*([+\-−]?\s*\d+(?:\.\d+)?)\s*(?:pa)?\s*$", (text or "").replace("−", "-"), re.I)
    return float(m.group(1).replace(" ", "")) if m else None


def _storey_label(code: str) -> str:
    m = re.fullmatch(r"L0?(\d+)", code)
    if m:
        return f"Level {int(m.group(1))}"
    m = re.fullmatch(r"B(\d+)", code)
    if m:
        return f"Basement {m.group(1)}"
    if code == "LB3":
        return "Lower Basement 3"
    return f"Storey {code}" if code != "NS" else "Storey not set"


def _storey_code(raw: str) -> str:
    """Storey code of the stacking diagram ('3', 'B2') or of an identifier ('L03') as used by the plan tables."""
    raw = (raw or "").strip().upper()
    if re.fullmatch(r"\d+", raw):
        return f"L{int(raw):02d}"
    m = re.fullmatch(r"L0*(\d+)", raw)
    if m:
        return f"L{int(m.group(1)):02d}"
    return raw


def _dept_key(dept: str) -> str:
    m = re.match(r"\s*0*(\d+)", str(dept))
    if not m:
        raise ValueError(f"department '{dept}' has no number")
    return f"{int(m.group(1)):02d}"


def _doc(path: str) -> str:
    return Path(path or "").name


def load_polygons(root: Path) -> Dict[str, List[Dict[str, str]]]:
    out: Dict[str, List[Dict[str, str]]] = {}
    for f in sorted(glob.glob(str(root / REG / "room_polygons_*.csv"))):
        if f.endswith("_wkt.csv"):
            continue
        out[Path(f).stem.replace("room_polygons_", "")] = _read(Path(f))
    return out


_ZONE_PAIR = re.compile(r"^(.*-\d{2})([AB])(?:/B)?$")


def zone_key(tag: str) -> str:
    """A duty and standby pair ('-01A', '-01B' or '-01A/B') is one zone."""
    m = _ZONE_PAIR.match((tag or "").strip())
    return m.group(1) if m else (tag or "").strip()


def zone_totals(root: Path) -> Dict[str, Dict[str, Any]]:
    """Rooms and measured area of every unit on the zoning drawing (all storeys), from the zone assignment of each storey."""
    out: Dict[str, Dict[str, Any]] = {}
    for st, rows in load_zone_assignments(root).items():
        for r in rows.values():
            cls = (r.get("class") or "").strip()
            if not cls or cls.lower().startswith(("no information", "review", "mv", "nv")) or (r.get("class_kind") or "").upper() in ("MV", "NV"):
                continue
            t = out.setdefault(zone_key(cls), dict(rooms=0, area=0.0, storeys=[]))
            t["rooms"] += 1
            t["area"] += _f(r.get("area_m2")) or 0.0
            if st not in t["storeys"]:
                t["storeys"].append(st)
    return out


def load_zone_assignments(root: Path) -> Dict[str, Dict[str, Dict[str, str]]]:
    """storey -> room_id -> row, for every zone_assignment_<storey>.csv that exists."""
    out: Dict[str, Dict[str, Dict[str, str]]] = {}
    for f in sorted(glob.glob(str(root / REG / "zone_assignment_*.csv"))):
        rows = [{k: (re.sub(r"\bD-\d{3}\s*[:;,]?\s*", "", v) if isinstance(v, str) else v) for k, v in r.items()} for r in _read(Path(f))]   # decision numbers are internal
        out[Path(f).stem.replace("zone_assignment_", "")] = {r["room_id"]: r for r in rows if r.get("room_id")}
    return out


def basis_text(method: str) -> str:
    """Issued wording of the basis of a zoning class: the documents it rests on, not the steps of the work (D-015)."""
    t = (method or "").strip()
    t = re.sub(r"^adjudicated:\s*", "", t)
    t = re.sub(r";?\s*open conflict(?: CF-\d{4})?", "; the documents differ", t)
    t = re.sub(r"\bCF-\d{4}\b", "", t)
    t = t.replace("booklet picture", "booklet drawing").replace("graphic zone", "booklet drawing zone").replace("graphic tag", "booklet drawing tag")
    t = re.sub(r"\bgraphic\b", "booklet drawing", t)
    t = re.sub(r"^; ", "", t.strip(" ;"))
    return t or "not stated"


def _first(row: Dict[str, str], cols: Tuple[str, ...]) -> str:
    for c in cols:
        if row.get(c):
            return row[c].strip()
    return ""


from .common import ROOM_THERMAL_NOTE  # noqa: E402

OCCUPIED_USES = {"office", "conference_meeting", "break_room_pantry", "reception_waiting", "laboratory", "kitchen_cooking", "pharmacy_prep", "classroom_training",
                 "lecture_classroom", "medical_procedure", "patient_room", "retail_sales", "process_area", "recovery_icu", "operating_room"}

# Rooms whose load is led by equipment: the equipment gain is not known (the schedule of equipment is outside the scope), so the thermal load is not representative.
EQUIPMENT_LED = re.compile(r"data cent|server|\bcrac\b|\bups\b|batter|comms|\basrs\b|automation|chiller|freezer|cold room|kitchen|food prep|bakery|pastry|cook chill|"
                           r"\bmri\b|\bct\b|x-?ray|angio|cath|fluoro|ultrasound|mammo|imaging|laborator|\blab\b|haematolog|hameotolog|histo|microbiolog|chemistry|specimen", re.I)


def room_use(name: str) -> Tuple[Optional[str], str]:
    """(key of the density table, wording of the rule) for a room name; (None, '') when no rule matches."""
    low = (name or "").lower()
    for text in (low, _expand_abbreviations(low)):
        for rx, key, label in USE_RULES:
            if re.search(rx, text, re.I):
                return (key or None), label
    return None, ""


# D-025: the load use of a room (a key of internal_loads.plug_w_m2 and lighting_w_m2) from its name and the occupancy use. The mapping is an assumption and is
# stated on Basis of Design.
_IMAGING = re.compile(r"x-?ray|\bmri\b|ultrasound|\bct\b|fluoro|angio|mammo|cath lab|catheter|nuclear|\bpet\b|dexa|\bbmd\b|imaging|radiograph|scan", re.I)
_LOAD_FROM_USE = {"office": "office", "conference_meeting": "meeting", "break_room_pantry": "break_room_pantry", "reception_waiting": "reception_waiting",
                  "lecture_classroom": "training_classroom", "lecture_hall_fixed_seats": "training_classroom", "classroom_training": "training_classroom",
                  "computer_lab": "computer_lab", "laboratory": "laboratory", "process_area": "workshop", "pharmacy_prep": "pharmacy", "retail_sales": "reception_waiting",
                  "kitchen_cooking": "kitchen", "cafeteria_dining": "dining", "patient_room": "patient_room", "medical_procedure": "exam_treatment",
                  "operating_room": "operating_room", "corridor_circulation": "corridor_circulation", "storage": "store", "toilet": "toilet", "plant_room": "plant_room",
                  "main_entry_lobby": "lobby", "telephone_data_entry": "office", "physical_therapy": "exam_treatment"}


def load_use(name: str) -> Tuple[Optional[str], str]:
    """(key of the plug-load and lighting tables, room use in words) for a room name; (None, '') when no rule gives a room use."""
    low = (name or "").lower()
    use, _label = room_use(name)
    if re.search(r"nurse station|staff station|nursing station", low):
        return "nurse_station", "nurse station"
    if use == "recovery_icu":
        k = "icu_hdu_bed" if re.search(r"icu|hdu|intensive", low) else "recovery"
        return k, k.replace("_", " ")
    if use in ("medical_procedure", "operating_room") and _IMAGING.search(low) and use == "medical_procedure":
        return "procedure_imaging", "procedure and imaging"
    if use == "corridor_circulation" and re.search(r"lobby|concourse|foyer", low):
        return "lobby", "lobby"
    if use is None:
        if re.search(r"locker", low):
            return "toilet", "locker and changing room (nearest listed use: changing room)"
        return None, ""
    k = _LOAD_FROM_USE.get(use)
    return (k, k.replace("_", " ")) if k else (None, "")


def _expand_abbreviations(low: str) -> str:
    """Name with each Appendix D abbreviation spelled out (for the use mapping only)."""
    words = re.split(r"([^a-z0-9&]+)", low)
    return "".join(APPD_ABBREVIATIONS.get(w, w) for w in words)


_TIME_RANGE = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*(?:-|to|–)\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", re.I)
_TIME_ONE = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", re.I)
_QTY = re.compile(r"(?:up\s*to\s+|approx\.?\s+|about\s+)?(\d+(?:\.\d+)?)(?:\s*(?:-|–|to)\s*(\d+(?:\.\d+)?))?", re.I)


def parse_occupancy(text: str) -> Tuple[float, List[str]]:
    """Design occupancy of an RDS occupancy text, and the notes that go with it (D-016 reading rule).

    The count is the sum of the upper bounds of every quantity as printed (patients, staff, visitors, support persons, pax, FTE):
    '1 patient; 1-2 staff, 1-2 visitors' gives 5 and '1 patient with up to 1 support person; 2-3 staff' gives 5. A fraction is rounded up
    ('1.5 FTE' gives 2, '0.1 FTE' gives 1). Clock times ('7am-7pm') are not quantities. A text printed twice is read once.
    Notes: 'intermittent' (the word is kept in the text and flagged) and 'shifts' (a time range shows that the quantities are not all present at once)."""
    raw = (text or "").strip()
    notes: List[str] = []
    low = raw.lower()
    if re.search(r"intermit+i?ent|transient", low):
        notes.append("intermittent")
    if _TIME_RANGE.search(raw):
        notes.append("shifts")
    body = _TIME_RANGE.sub(" ", raw)
    body = _TIME_ONE.sub(" ", body)
    # a text printed twice without a separator ('1-6 staff1-6 staff') is read once
    segs = [x.strip() for x in re.split(r"[;,/]|(?<=[a-z])(?=\d)|\bwith\b|\band\b", body, flags=re.I) if x.strip()]
    seen: List[str] = []
    for x in segs:
        if x.lower() not in seen:
            seen.append(x.lower())
    total = 0.0
    for seg in seen:
        for m in _QTY.finditer(seg):
            hi = m.group(2) or m.group(1)
            # 'up to 6-8' and '2 to 3' use the upper bound; a 'support person' or 'staff assisting' still counts as a person
            total += float(hi)
    return float(math.ceil(round(total, 9))), notes


def occupancy_from_rds(row: Optional[Dict[str, str]]) -> Optional[Tuple[float, str]]:
    """(design occupancy, text as printed) from an RDS room row, or None when the RDS field is empty.
    The reading rule is parse_occupancy (D-016). 'n/a' or '0' is zero people and the text is kept as an RDS value."""
    if row is None:
        return None
    text = (row.get("occupancy") or "").strip()
    if not text:
        return None
    if text.lower() in ("n/a", "na", "nil", "none", "0"):
        return 0.0, text
    people, _ = parse_occupancy(text)
    return people, text


def occupancy_notes(text: str) -> List[str]:
    return parse_occupancy(text)[1] if text else []


# --------------------------------------------------------------------------- #
# Abbreviations of plan room tags that the abbreviation register does not list. 'Collab' is the first part of the word 'Collaboration' in the
# DPA room name 'Collaboration Huddle' (plan room B2-R022 'Collab' and DPA room PHAB 04.04.01). Each entry is applied to the name match only.
EXTRA_ABBREVIATIONS: Dict[str, List[str]] = {"collab": ["collaboration"]}


def _expansions(root: Path) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = defaultdict(list)
    for r in _read(root / REG / "zoning_abbreviations.csv"):
        out[r["abbreviation"].strip().lower()].append(r["expansion"].strip().lower())
    for k, v in EXTRA_ABBREVIATIONS.items():
        for x in v:
            if x not in out[k]:
                out[k].append(x)
    return out


def name_score(dpa_name: str, plan_name: str, abbr: Dict[str, List[str]]) -> float:
    """0 to 1: how well a plan name (often abbreviated) fits a DPA room name."""
    a, b = _norm(dpa_name), _norm(plan_name)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    pw = _words(plan_name)
    best = difflib.SequenceMatcher(None, a, b).ratio()
    variants = [plan_name.lower()]
    for i, w in enumerate(pw):
        for ex in abbr.get(w, []):
            variants.append(" ".join(pw[:i] + [ex] + pw[i + 1:]))
    dw = set(_words(dpa_name))
    for v in variants:
        best = max(best, difflib.SequenceMatcher(None, a, _norm(v)).ratio())
        vw = set(_words(v))
        if vw and vw <= dw and len(vw) >= max(1, len(dw) - 1):
            best = max(best, 0.9)
    return best


def zoning_name_score(dpa_name: str, plan_name: str, abbr: Dict[str, List[str]]) -> float:
    """Name fit for the zoning links (AHU only, never an area): `name_score`, and 0.86 where the plan name, or one of its abbreviation
    expansions, has two or more words and prints as a run of words inside the DPA name ('Manual Picking' in 'Manual Picking | Repackaging Area',
    'Inpatient Pharmacy' in 'Inpatient Pharmacy Automation System'). A single word is not enough ('Office' is not 'Office - Pharmacy Head').
    The link then still needs equal counts on both sides and one AHU (see `plan_links`)."""
    sc = name_score(dpa_name, plan_name, abbr)
    if sc >= 0.85:
        return sc
    dw = [w for w in _words(dpa_name) if len(w) > 1]
    pw = _words(plan_name)
    variants = [pw]
    for i, w in enumerate(pw):
        for ex in abbr.get(w, []):
            variants.append(pw[:i] + _words(ex) + pw[i + 1:])
    for v in variants:
        v = [w for w in v if len(w) > 1]
        if len(v) >= 2 and any(dw[i:i + len(v)] == v for i in range(len(dw) - len(v) + 1)):
            return 0.86
    return sc


MAX_AREA_RATIO = 2.0          # a plan room whose area is more than this factor above or below the design area is not the same room
GRID_PAD_MM = 25200           # three grid bays of 8,400 mm: a plan room further than this from the anchor rooms of the department is outside its span
MIN_ANCHORS = 3


def _ratio_ok(ref: float, area: float, max_ratio: float) -> bool:
    if not ref or not area:
        return True
    r = area / ref
    return 1.0 / max_ratio <= r <= max_ratio


def match_plan_rooms(dpa_rooms: List[Dict[str, Any]], plan_rooms: List[Dict[str, Any]], abbr: Dict[str, List[str]],
                     rejected: Optional[List[Dict[str, Any]]] = None, max_ratio: float = MAX_AREA_RATIO,
                     grid_pad_mm: float = GRID_PAD_MM) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """One-to-one match of DPA rooms to plan rooms of the same storey and department.

    A pair is accepted when the name score is at least 0.85, the area of the plan room is within a factor `max_ratio` of the design
    area of the DPA room (`ref_area`, else `dpa_area`), the plan room is inside the grid span of the department on its storey, the pair is
    the best for both sides (no second candidate within 0.05) and, if the name occurs more than once on either side, the areas of the
    pair agree within 15 %. A group of equal names with the same number of rooms on both sides is paired in order of area when every
    pair agrees within 15 %. Everything else stays unmatched on its side and is listed, never forced. A candidate that the area or the grid
    test removes is appended to `rejected` (dicts with the DPA room number, the plan room, the reason and the area ratio).

    The grid span of a storey is the box through the centroids (building coordinates, mm) of the anchor rooms, padded by `grid_pad_mm`.
    The anchors are the pairs whose names are equal and whose areas agree within 15 %; the test is used when a storey has at least
    three of them and the plan rooms carry centroids ('cx', 'cy')."""
    by_no = {d["number"]: d for d in dpa_rooms}
    ref_of = lambda d: (d.get("ref_area") or d.get("dpa_area") or 0)
    log: Dict[Tuple[str, int], Dict[str, Any]] = {}

    def run() -> Dict[str, Dict[str, Any]]:
        cands: Dict[str, List[Tuple[float, int]]] = defaultdict(list)
        rev: Dict[int, List[Tuple[float, str]]] = defaultdict(list)
        for d in dpa_rooms:
            for j, p in enumerate(plan_rooms):
                if p["storey"] != d["storey"]:
                    continue
                sc = name_score(d["name"], p["name"], abbr)
                if sc < 0.85:
                    continue
                cands[d["number"]].append((sc, j))
                rev[j].append((sc, d["number"]))
        pairs: Dict[str, Dict[str, Any]] = {}
        used_plan = set()
        for no, lst in cands.items():
            lst.sort(reverse=True)
            sc, j = lst[0]
            close = [x for x in lst if x[0] >= sc - 0.05]
            if len(close) > 1:
                pset = {x[1] for x in close}
                dset = [n for n, ll in cands.items() if {x[1] for x in ll if x[0] >= ll[0][0] - 0.05} == pset]
                if len(dset) != len(pset) or no in pairs:
                    continue
                ds = sorted(dset, key=lambda n: by_no[n]["dpa_area"] or 0)
                ps = sorted(pset, key=lambda jj: plan_rooms[jj]["area"] or 0)
                if all((ref_of(by_no[n])) and abs(ref_of(by_no[n]) - (plan_rooms[jj]["area"] or 0)) <= 0.15 * ref_of(by_no[n])
                       for n, jj in zip(ds, ps)):
                    for n, jj in zip(ds, ps):
                        if jj not in used_plan:
                            pairs[n] = plan_rooms[jj]
                            used_plan.add(jj)
                continue
            rl = sorted(rev[j], reverse=True)
            if (len(rl) > 1 and rl[1][0] >= rl[0][0] - 0.05) or rl[0][1] != no or j in used_plan:
                continue
            d = by_no[no]
            da, pa = ref_of(d), plan_rooms[j]["area"] or 0
            dup = sum(1 for x in dpa_rooms if _norm(x["name"]) == _norm(d["name"])) > 1 or \
                sum(1 for x in plan_rooms if _norm(x["name"]) == _norm(plan_rooms[j]["name"])) > 1
            if dup and not (da and abs(da - pa) <= 0.15 * da):
                continue
            pairs[no] = plan_rooms[j]
            used_plan.add(j)
        return pairs

    area_only = lambda d, p: "area" if not _ratio_ok(ref_of(d), p.get("area") or 0, max_ratio) else ""
    named = run()                                   # decided by name, storey and ties alone
    pairs: Dict[str, Dict[str, Any]] = {}
    for n, p in named.items():
        if area_only(by_no[n], p):
            log[(n, id(p))] = dict(dpa=n, plan=p, reason="area", ratio=(p["area"] / ref_of(by_no[n])) if ref_of(by_no[n]) and p.get("area") else None)
        else:
            pairs[n] = p
    # grid span of the department on each storey, from the anchor pairs
    span: Dict[str, Tuple[float, float, float, float]] = {}
    for st in {d["storey"] for d in dpa_rooms}:
        pts = []
        for n, p in pairs.items():
            d = by_no[n]
            if d["storey"] == st and _norm(d["name"]) == _norm(p["name"]) and ref_of(d) and abs(ref_of(d) - (p["area"] or 0)) <= 0.15 * ref_of(d) \
                    and p.get("cx") is not None and p.get("cy") is not None and not p.get("shared"):
                pts.append((p["cx"], p["cy"]))
        if len(pts) >= MIN_ANCHORS:
            xs, ys = sorted(x for x, _ in pts), sorted(y for _, y in pts)
            mx, my = xs[len(xs) // 2], ys[len(ys) // 2]
            kept = [(x, y) for x, y in pts if abs(x - mx) <= 4 * grid_pad_mm and abs(y - my) <= 4 * grid_pad_mm]   # a lone far anchor does not widen the span
            pts = kept if len(kept) >= MIN_ANCHORS else pts
            span[st] = (min(x for x, _ in pts) - grid_pad_mm, max(x for x, _ in pts) + grid_pad_mm,
                        min(y for _, y in pts) - grid_pad_mm, max(y for _, y in pts) + grid_pad_mm)
    for n, p in list(pairs.items()):
        sp = span.get(p["storey"])
        if sp and p.get("cx") is not None and p.get("cy") is not None and not (sp[0] <= p["cx"] <= sp[1] and sp[2] <= p["cy"] <= sp[3]):
            log[(n, id(p))] = dict(dpa=n, plan=p, reason="grid", ratio=None)
            del pairs[n]
    used = {id(p) for p in pairs.values()}
    unmatched_dpa = [d for d in dpa_rooms if d["number"] not in pairs]
    unmatched_plan = [p for p in plan_rooms if id(p) not in used]
    if rejected is not None:
        for (no, j), rec in log.items():
            if no not in pairs:
                rejected.append(rec)
    return pairs, unmatched_dpa, unmatched_plan


# --------------------------------------------------------------------------- #
_SEARCH_FLAG_WORDS = [("a join on the room name collides", "the room name is not unique"), ("the register keeps each row as printed", "each row is kept as printed"),
               ("the register keeps them as printed", "they are kept as printed")]


def criteria_text(row: Dict[str, str]) -> str:
    """Text of a booklet criteria row in document terms: the cells as printed, with no tool wording."""
    try:
        cells = json.loads(row.get("raw_cells") or "{}")
    except ValueError:
        cells = {}
    labels = [("min_oa", "minimum outdoor-air changes"), ("min_total", "minimum total air changes"), ("single_pass", "single pass"),
              ("afterhours", "after hours"), ("setback", "setback"), ("min_oa_rate_as_printed", "outdoor-air rate as printed")]
    parts = [f"{lab} {cells[k]}" for k, lab in labels if str(cells.get(k, "")).strip()]
    head = f"Room type {row.get('room_type_as_printed', '')}"
    out = head + (": " + "; ".join(parts) if parts else "")
    if cells.get("read_by"):
        out += ". The booklet page is an image; the cells are read from the printed page"
    flag = str(cells.get("source_flag") or "")
    for a, b in _SEARCH_FLAG_WORDS:
        flag = flag.replace(a, b)
    if flag:
        out += ". " + flag
    return out[:400]


def _criteria_alternatives(row: Dict[str, str]) -> List[str]:
    """Names listed inside one booklet row: the whole room type first, then each name of a multi-name row (separated by '/', '&' or ',')."""
    t = row.get("room_type_as_printed") or ""
    alts = [t] + [x for x in re.split(r"\s*/\s*|,\s*|\s+&\s+", t) if x.strip()]
    return [a.strip() for a in alts if a.strip()]


def _sentence_case(text: str) -> str:
    """Capital letter at the start of each sentence of a remark (the clauses are joined by full stops)."""
    return re.sub(r"(^|\. )([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)


def _criteria_desc(c: Dict[str, Any]) -> str:
    """Values of a booklet criteria row that decide the airflow, as they are printed."""
    parts = []
    if c.get("min_total_ach"):
        parts.append(f"{c['min_total_ach']} total ACH")
    if c.get("min_oa_ach"):
        parts.append(f"{c['min_oa_ach']} outdoor-air ACH")
    parts.append(f"pressure {c.get('pressure_regime_as_printed') or 'not printed'}")
    if c.get("air_class") not in (None, "", "NA"):
        parts.append(f"air class {c['air_class']}")
    return ", ".join(parts)


def _variant_text(first: Dict[str, Any], variants: List[Dict[str, Any]]) -> str:
    """Flag text for a room type that the booklet prints more than once with different values: each distinct set of values with its pages."""
    groups: Dict[str, List[str]] = {}
    for c in [first] + list(variants):
        pages = groups.setdefault(_criteria_desc(c), [])
        if str(c["booklet_page"]) not in pages:
            pages.append(str(c["booklet_page"]))
    parts = [f"{'Page' if len(pg) == 1 else 'Pages'} {' and '.join(pg) if len(pg) < 3 else ', '.join(pg[:-1]) + ' and ' + pg[-1]} {'gives' if len(pg) == 1 else 'give'} {d}."
             for d, pg in groups.items()]
    return ("The booklet prints this room type more than once with different values. " + " ".join(parts) +
            f" The table on page {first['booklet_page']} is used.")


from .styles import clean_text


def criteria_from_row(row: Dict[str, str], csid: str, how: str, code: str = "D", remark: Optional[str] = None) -> Dict[str, Any]:
    """Criteria block of the department input from one booklet criteria row."""
    reg_raw = row.get("pressure_regime_as_printed", "")
    pa_b = _f(row.get("pressure_pa"))
    crit = dict(criteria_row=row["room_type_as_printed"], page=f"page {row['booklet_page']}", min_oa_ach=_f(row["min_oa_ach"]),
                min_total_ach=_f(row["min_total_ach"]), temp_min_c=_f(row["temp_min_c"]), temp_max_c=_f(row["temp_max_c"]),
                rh_max_pct=_f(row["rh_max_pct"]), pressure_regime=_regime(reg_raw),
                pressure_pa_booklet=pa_b if pa_b is not None else _pa(reg_raw),
                air_class=(row["air_class"] if row["air_class"] not in ("", "NA") else None), filter=row["filter"] or None,
                single_pass=("Y" if row["single_pass"].upper().startswith("Y") else ("N" if row["single_pass"].upper() == "N" else None)),
                afterhours=row["afterhours"] or None, source=csid, code=code,
                remark=remark or f"Matched to room type '{row['room_type_as_printed']}' ({how}).")
    apply_exhaust_only(crit, row["room_type_as_printed"])
    _outdoor_air_rates(crit, row)
    return crit


def _outdoor_air_rates(crit: Dict[str, Any], row: Dict[str, str]) -> None:
    """The booklet prints the outdoor air of some rows per area ('0.6 L/s/m2') or per person ('10 L/s/person'), or both ('10 l/s/person or 0.6 l/s/m2, whichever
    higher'), or as air changes in the text ('6 ACH'). The register keeps the text, and the air-change column is blank. The rates go to their own cells; the outdoor
    air of the room is the highest of the values."""
    try:
        cells = json.loads(row.get("raw_cells") or "{}")
    except ValueError:
        cells = {}
    txt = str(cells.get("min_oa") or "")
    pp = re.search(r"([\d.]+)\s*l/s\s*/\s*(?:person|pax|p\b)", txt, re.I)
    pa = re.search(r"([\d.]+)\s*l/s\s*/\s*m(?:2|²)", txt, re.I)
    ach = re.fullmatch(r"\s*([\d.]+)\s*ACH\s*", txt, re.I)
    rates = []
    if pp:
        crit["oa_lps_per_person"] = float(pp.group(1))
        rates.append(f"{pp.group(1)} L/s per person")
    if pa:
        crit["oa_lps_per_m2"] = float(pa.group(1))
        rates.append(f"{pa.group(1)} L/s per m²")
    if ach and crit.get("min_oa_ach") is None:
        crit["min_oa_ach"] = float(ach.group(1))
        rates.append(f"{ach.group(1)} air changes")
    if not rates and re.search(r"SS\s*553", txt, re.I):
        # D-064 (6): a row that prints 'SS 553' for outdoor air takes the per-person and per-area rate of the booklet office rows (assumption, code A), not 0
        pp_, pa_ = _office_oa_rates()
        if pp_ or pa_:
            if pp_:
                crit["oa_lps_per_person"] = pp_
            if pa_:
                crit["oa_lps_per_m2"] = pa_
            crit["code"] = "A"
            crit["remark"] = ((crit.get("remark") or "").rstrip(". ") + f". The row prints SS 553 for outdoor air. The rate of the booklet office rows is used ({pp_:g} L/s per person or "
                              f"{pa_:g} L/s per m², whichever is higher). Assumption, to confirm.")
    if rates:
        crit["remark"] = (crit.get("remark") or "").rstrip(". ") + ". Outdoor air as printed: " + (" or ".join(rates)) + (", whichever is higher" if len(rates) > 1 else "") + "."


@lru_cache(maxsize=1)
def _office_oa_rates() -> Tuple[Optional[float], Optional[float]]:
    """Most frequent per-person and per-area outdoor-air rate of the office rows of the booklets (for the rows that print only 'SS 553')."""
    from collections import Counter
    pp_c, pa_c = Counter(), Counter()
    for row in _read(Path(".") / REG / "criteria.csv"):
        if "office" not in (row.get("room_type_as_printed") or "").lower():
            continue
        try:
            txt = str((json.loads(row.get("raw_cells") or "{}")).get("min_oa") or "")
        except ValueError:
            continue
        m1 = re.search(r"([\d.]+)\s*l/s\s*/\s*(?:person|pax|p\b)", txt, re.I)
        m2 = re.search(r"([\d.]+)\s*l/s\s*/\s*m(?:2|²)", txt, re.I)
        if m1:
            pp_c[float(m1.group(1))] += 1
        if m2:
            pa_c[float(m2.group(1))] += 1
    return (pp_c.most_common(1)[0][0] if pp_c else None, pa_c.most_common(1)[0][0] if pa_c else None)


EXHAUST_ONLY = re.compile(r"toilet|\bwc\b|shower|ensuite|\bbath", re.I)


def apply_exhaust_only(crit: Dict[str, Any], type_text: str) -> None:
    """A toilet, shower or bath type at negative pressure is an exhaust-only room: the booklet total air changes are the exhaust, and
    transfer air makes up (the Rev A example takes its toilet this way: 20 ACH on 4.3 m² gives 80 L/s of exhaust and no supply)."""
    if crit and EXHAUST_ONLY.search(type_text or "") and crit.get("pressure_regime") == "NEGATIVE" and crit.get("min_total_ach"):
        crit["exhaust_ach"] = crit["min_total_ach"]
        crit["min_total_ach"] = None
        crit["remark"] = (crit.get("remark") or "").rstrip(". ") + ". Exhaust-only room: the booklet total air changes are the exhaust. Transfer air makes up the supply."


def match_criteria(room_name: str, rows: List[Dict[str, str]], modal: bool = False) -> Tuple[Optional[Dict[str, str]], List[Dict[str, str]], str]:
    """Best criteria row of the department for a room name: (row, other variants of the same room type, how matched or why not)."""
    rn = _norm(room_name)
    rw = set(_words(room_name))
    best: List[Tuple[float, Dict[str, str], str]] = []
    for c in rows:
        top, how = 0.0, ""
        for k, alt in enumerate(_criteria_alternatives(c)):
            an = _norm(alt)
            if an and an == rn:                  # exact name only. A name that is part of a multi-name row is an assumption (code A). No word overlap, no similar name.
                sc, h = (3.0, "same name") if k == 0 else (2.5, "name listed in a multi-name booklet row")
                if sc > top:
                    top, how = sc, h
        if top > 0:
            best.append((top, c, how))
    if not best:
        return None, [], ""
    if modal:                       # rows from many booklets: a row whose whole room type is the name beats a composite type that contains it
        whole = [b for b in best if _norm(b[1]["room_type_as_printed"]) == rn]
        if whole:
            best = whole
    best.sort(key=lambda x: -x[0])
    top = best[0][0]
    tied = [b for b in best if b[0] >= top - 1e-9]
    types = {b[1]["room_type_norm"] for b in tied}
    if len(types) > 1:
        return None, [], "several room types fit equally: " + "; ".join(sorted({t["room_type_as_printed"] for _, t, _ in tied})[:4])
    group = sorted((b[1] for b in tied), key=lambda c: (_f(c.get("booklet_page")) or 0))
    first = group[0]
    sig = lambda c: (c.get("min_oa_ach"), c.get("min_total_ach"), c.get("pressure_pa"), c.get("pressure_regime_as_printed"), c.get("air_class"))
    if modal:                       # rows from many booklets: the most frequent set of values is the row used
        counts: Dict[Any, int] = defaultdict(int)
        for c in group:
            counts[sig(c)] += 1
        top_sig = max(counts, key=lambda k: counts[k])
        first = next(c for c in group if sig(c) == top_sig)
    variants = [c for c in group if sig(c) != sig(first)] if modal else [c for c in group[1:] if sig(c) != sig(first)]
    return first, variants, best[0][2]


# --------------------------------------------------------------------------- #
def _tag_candidates(tag: str) -> List[str]:
    """Tags to look up for a zone tag: 'AHU-X-01A/B' is the duty and standby pair '...-01A' and '...-01B'. A tag that ends in a number
    ('AHU-X-01') can be scheduled as '...-01A' and '...-01B'. After these, the same tags with the served level in place of the plant level
    ('AHU-B-07-04CATH-01' -> 'AHU-B-04-04CATH-01'): the schedule then prints another form of the tag, and the source text says so."""
    t = (tag or "").strip()
    out = [t]
    m = re.fullmatch(r"(.*?)([A-Z])((?:[/&][A-Z])+)", t)
    if m:
        letters = [m.group(2)] + re.findall(r"[A-Z]", m.group(3))
        out = [m.group(1) + x for x in letters] + [t]
    elif re.search(r"\d$", t):
        out = [t, t + "A", t + "B"]
    elif re.search(r"-\d{2}[AB]$", t):
        out = [t, t[:-1], t[:-1] + ("B" if t[-1] == "A" else "A")]
    return list(dict.fromkeys(out))


def _level_variant_tags(cands: List[str]) -> List[str]:
    out: List[str] = []
    for c in cands:
        m = re.match(r"^((?:AHU|PAHU|FCU)-B-)(\d\d)-(\d\d)(.*)$", c)
        if m and m.group(2) != m.group(3):
            out.append(f"{m.group(1)}{m.group(3)}-{m.group(3)}{m.group(4)}")
    return out


def _tender_for_tag(tag: str, sched: Dict[str, Dict[str, str]] = None, fcu: Dict[str, Dict[str, str]] = None, eq: List[Dict[str, str]] = None,
                    totals: List[Dict[str, str]] = None, add_src=None, mot: Optional[Dict[str, Dict[str, str]]] = None, schematic_only: bool = False,
                    root: Optional[Path] = None) -> Dict[str, Any]:
    """Tender airflow of one tag from the one lookup of every deliverable (``tender_lookup``): Equipment Schedule (Corr 5), else the MOT NSC Overall AC
    Equipment Schedule (Corr 2), else the tender air-side schematic. The tables of the arguments are not used: they remain for the callers."""
    from . import tender_lookup as tl
    t = tl.shared(None if root is None or Path(root) == Path(".") else root)
    out: Dict[str, Any] = {}
    if schematic_only:
        sv = t.schematic_value([tag])
        if sv is None:
            return out
        sid = f"TSCH-{_norm(sv['cand'])}"[:60]
        add_src(sid, document=f"Air-side schematic {sv['sheet']}", issue=sv.get("issue", "tender issue"), page=f"page {sv['page']}" if sv.get("page") else sv["sheet"],
                location=f"AHU {sv['cand']}", text_as_read=sv.get("text_as_read") or f"{sv['value']:g} L/s", method="Value read from the text printed on the air-side schematic",
                confidence="medium")
        out.update(sched_supply_lps=sv["value"], sched_source=sid, sched_code="D")
        return out
    r = t.lookup([tag])
    if r["value"] is None:
        out["tender_source_text"] = r["text"]
        out["tender_other_values"] = [(v, s) for v, s in r.get("others", [])]
        return out
    v, kind, c = r["value"], r["kind"], r["cand"]
    page = r.get("page", "")
    if kind == "eq5":
        sid = f"EQS5-{_norm(c)}"[:60]
        oa = r.get("oa")
        add_src(sid, document="Equipment Schedule, air handling unit schedule", issue="Corr 5", page=f"page {page}", location=f"sheet {r.get('sheet', '')}, tag {r['tag_as_written']}",
                text_as_read=f"{r['tag_as_written']}: supply airflow {v:g} L/s; outdoor airflow minimum {oa if oa is not None else 'not printed'}",
                method="Value read from the Equipment Schedule table", confidence="high")
        out.update(sched_oa_lps=oa)
    elif kind == "eq5fcu":
        sid = f"EQS5-{_norm(c)}"[:60]
        add_src(sid, document="Equipment Schedule, fan coil unit schedule", issue="Corr 5", page=f"page {page}", location=f"sheet {r.get('sheet', '')}, tag {r['tag_as_written']}",
                text_as_read=f"{r['tag_as_written']}: minimum airflow {v:g} L/s", method="Value read from the Equipment Schedule table", confidence="high")
    elif kind == "mot":
        sid = f"MOT2-{_norm(c)}"[:60]
        add_src(sid, document="MOT NSC Overall AC Equipment Schedule", issue=r.get("issue", "Corr 2"), page=f"page {page}", location=f"tag {c}",
                text_as_read=f"{c}: supply airflow {v:g} L/s; outdoor airflow minimum {r.get('oa') if r.get('oa') is not None else 'not printed'}",
                method="Value read from the MOT NSC equipment schedule table", confidence="high")
        out.update(sched_oa_lps=r.get("oa"))
    else:
        sid = f"TSCH-{_norm(c)}"[:60]
        add_src(sid, document=f"Air-side schematic {r.get('sheet', '')}", issue=r.get("issue", "tender issue"), page=f"page {page}" if page else r.get("sheet", ""), location=f"AHU {c}",
                text_as_read=r.get("text_as_read") or f"{v:g} L/s", method="Value read from the text printed on the air-side schematic", confidence="medium")
    out.update(sched_supply_lps=v, sched_source=sid, sched_code="D", tender_source_text=r["text"], tender_other_values=[(x, s) for x, s in r.get("others", [])])
    if r.get("type"):
        out["type"] = r["type"]
    return out


# --------------------------------------------------------------------------- #
def ip_units(key: str, stack: List[Dict[str, str]], dpa_units: Dict[str, List[Dict[str, str]]]) -> Dict[str, str]:
    """DPA unit codes of the department that the stacking diagram puts in the IP block, with their storey."""
    names = {code: _norm(rows[0]["fpu_department_name"]) for code, rows in dpa_units.items()}
    ip: Dict[str, str] = {}
    for s in stack:
        if not s.get("dept_no") or _dept_key(s["dept_no"]) != key or not s["block"].startswith("IP"):
            continue
        for code, nm in names.items():
            if nm and nm == _norm(s["department_as_written"]):
                ip.setdefault(code, _storey_code(s["storey"]))
    return ip


def height_conflict_text(c: Dict[str, Any]) -> str:
    """Words for a height conflict: different heights in the room layout sheets, or one sheet that prints several heights."""
    vals = ", ".join(f"{v:g} m" for v in c["values"])
    if len(c["values"]) > 1:
        return f"the room layout sheets give different ceiling heights for code {c['code']}: {vals}. None is chosen"
    return (f"a room layout sheet for code {c['code']} prints more than one ceiling height ({', '.join(c.get('flags') or [])}). "
            f"The value {vals} is only the governing value given for the code, not a clear height of this room. None is chosen")


def _area_txt(p: Dict[str, Any]) -> str:
    return f"{p['area']:.1f} m²" if p.get("area") is not None else "no measured area"


def _class_of(p: Dict[str, Any]) -> str:
    tag, _ = plan_system(p["zone"], AHU_COLUMNS, SYSTEM_KINDS)
    return tag or (p["zone"].get("class") or "")


def write_link_audit(data: Dict[str, Any], path: Path) -> None:
    """CSV of the links of one department: every DPA room with its RDS, height and plan basis, and the unlinked RDS and plan rooms."""
    fields = ["side", "dept", "ref", "name", "code", "storey", "rds_basis", "rds_code", "rds_note", "height_basis", "height_m", "plan_basis", "ahu", "occupancy_basis"]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in data["link_audit"]:
            w.writerow({k: r.get(k, "") for k in fields})


_UNIT_ROOMS: Dict[Tuple[str, str], Dict[str, int]] = {}
_UNIT_ROOMS_LOCK = threading.Lock()


def unit_rooms_of(dept: str, root: Path = Path(".")) -> Dict[str, int]:
    """Rooms of one department on each unit (by zone key): the unit of a room as the assembly of the department sets it."""
    ck = (str(Path(root).resolve()), str(dept))
    with _UNIT_ROOMS_LOCK:
        if ck in _UNIT_ROOMS:
            return _UNIT_ROOMS[ck]
    try:
        if str(dept) == "00":
            from .common_areas import assemble_common_areas
            rooms = assemble_common_areas(root, with_others=False)["rooms"]
        else:
            rooms = assemble_department(str(dept), None, root, with_others=False)["rooms"]
    except (IndexError, FileNotFoundError, ValueError, KeyError):         # a root with no registers for that department (a test root): it has no rooms to count
        rooms = []
    out: Dict[str, int] = defaultdict(int)
    for r in rooms:
        if r.get("ahu"):
            out[zone_key(r["ahu"])] += 1
    with _UNIT_ROOMS_LOCK:
        _UNIT_ROOMS[ck] = dict(out)
    return _UNIT_ROOMS[ck]


def other_unit_rooms(dept: str, root: Path = Path(".")) -> Dict[str, int]:
    """Rooms of the other departments (the common areas included) on each unit. A unit that several departments share is judged on the AHU master summary only."""
    mine = str(dept).lstrip("0").zfill(2) if str(dept).strip("0") else "00"
    nums = sorted({p.stem[1:] for p in (Path(root) / "out/loads/inputs").glob("D*.json")} | {"00"})
    out: Dict[str, int] = defaultdict(int)
    for n in nums:
        if n == mine:
            continue
        for k, v in unit_rooms_of(n, root).items():
            out[k] += v
    return dict(out)


def assemble_department(dept: str, out: Optional[Path] = None, root: Path = Path("."), with_others: bool = True) -> Dict[str, Any]:
    key = _dept_key(dept)
    root = Path(root)
    soa_all = [r for r in _read(root / REG / "soa_rooms.csv") if r["dept_no"] == key and r.get("governing", "").lower() == "yes"]
    stack = _read(root / REG / "stacking_diagram.csv")
    dpa_units: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for r in _read(root / REG / "dpa_rooms.csv"):
        if r.get("fpu_department_no"):
            dpa_units[r["fpu_department_no"]].append(r)
    soa_units = {r.get("unit_zone_code", "") for r in soa_all}
    cand_units = {c: rows for c, rows in dpa_units.items() if c in soa_units} if soa_units else dpa_units
    from .dept_units import keep_room, resolve_units
    res_units = resolve_units(key, stack, dpa_units, soa_units, lambda: ip_units(key, stack, cand_units), _storey_code, _dept_key, SCOPE_STOREYS)
    unit_info = res_units["units"]
    ip_map = {c: i["storey"] for c, i in unit_info.items()}
    if not ip_map:
        raise ValueError(f"department {key}: no DPA unit with a storey in LB3-L07 (no explicit entry, name match or SOA unit)"
                         + (f"; unresolved SOA units {res_units['unresolved']}" if res_units["unresolved"] else ""))
    dept_name = dept_label(key, root)
    if not dept_name:
        if soa_all:
            dept_name = re.sub(r"^\d+\s*-\s*", "", soa_all[0]["department_folder"])
        else:
            dept_name = next((r["department_as_written"] for r in stack if r.get("dept_no") and _dept_key(r["dept_no"]) == key and r["block"].startswith("IP")), key)
    base = build_reva_input(root)
    people = conf.people(root)
    densities = conf.occupancy_cfg(root).get("per_100m2") or {}
    all_criteria = _read(root / REG / "criteria.csv")
    criteria = [r for r in all_criteria if r["dept_no"].lstrip("0") == key.lstrip("0")]
    polygons = load_polygons(root)
    zones = load_zone_assignments(root)
    rds_all = [r for r in _read(root / REG / "rds_rooms.csv") if r["dept_no"].lstrip("0") == key.lstrip("0")]
    sched_rooms = [r for r in _read(root / REG / "schematic_rooms.csv") if r.get("issue_set") == "tender"]
    sched_equip = _read(root / REG / "schematic_equipment.csv")
    sched_totals = _read(root / REG / "schematic_ahu_totals.csv")
    ahu_sched = {_norm(r["tag_normalised"]): r for r in _read(root / REG / "ahu_schedule.csv")}
    fcu_sched = {_norm(r["tag_normalised"]): r for r in _read(root / REG / "equipment_schedule_air_handling.csv") if r.get("type") == "FCU"}
    from . import criteria_map as cm
    mot = cm.MotTheatres(root) if key.lstrip("0") == "22" else None
    hepa = r5.MotHepa(root) if key.lstrip("0") in ("22", "28") else None
    env_model = envm.Envelope(root, (conf.config(root).get("envelope_round5") or {}).get("coefficients"))
    mot_units: set = set()                       # unit tags of MOT rooms: no reduction is stated (D-057)
    hepa_specs: Dict[str, Dict[str, Any]] = {}   # unit tag -> theatre specs on it
    env_rooms = env_missing = 0
    dc_table = cm.datacentre_table(root) if key.lstrip("0") == "42" else {}
    unmapped_names: Dict[str, List[str]] = defaultdict(list)
    mot_sched = {_norm(r["tag"].replace("lSONP", "ISONP")): r for r in _read(root / REG / "mot_ahu_schedule.csv") if r.get("equipment_type") in ("AHU", "PAHU")}
    abbr = _expansions(root)

    sources: List[Dict[str, Any]] = [s for s in base["sources"] if s["id"] in ("RA-RZ-TBL", "RA-BOD-C19", "RA-BOD-C28", "RA-BOD-C30", "RA-BOD-C31",
                                                                              "RA-AHU-B29", "RA-OCC", "RA-DD21")]
    src_ids = {s["id"] for s in sources}
    flags: List[Dict[str, Any]] = []

    def add_src(sid: str, **kw: Any) -> str:
        if sid not in src_ids:
            sources.append(dict(id=sid, **kw))
            src_ids.add(sid)
        return sid

    add_src("ASM-DEFAULT", document="Workbook defaults", issue="", page="", location="space type UNCLASSIFIED",
            text_as_read="design room condition of the Rev A design basis (24 °C, 52.6 kJ/kg) applied where no space type is set",
            method="Assumption, to confirm", confidence="low")
    add_src("ASM-ACTIVITY", document="Workbook defaults", issue="", page="", location="activity of occupants",
            text_as_read="activity Seated (Rev A table) applied where occupancy is stated and no activity is given", method="Assumption, to confirm",
            confidence="low")
    ch_default = _f((conf.config(root).get("ceiling_height") or {}).get("flagged_default_m"))
    if ch_default:
        add_src("ASM-CEILING", document="Workbook defaults", issue="", page="", location="ceiling height",
                text_as_read=f"ceiling height {ch_default:g} m applied where the room layout sheets give none", method="Assumption, to confirm", confidence="low")
    add_src("OCC-DENSITY", document="ASHRAE Standard 62.1, Table 6.2.2.1: default occupant density", issue="edition to confirm (health-care rows: 62.1-2007)",
            page="Table 6.2.2.1", location="persons per 100 m² by occupancy category",
            text_as_read="default densities per 100 m², taken as listed in the project design basis", method="Value taken from the project design basis", confidence="medium")

    il = conf.internal_loads_cfg(root)
    plug_tab, light_tab = il.get("plug_w_m2") or {}, il.get("lighting_w_m2") or {}
    add_src("ASM-PLUG", document="ASHRAE Handbook, Fundamentals: nonresidential cooling and heating load calculations, plug-load densities by space type", issue="edition to confirm",
            page="plug-load tables", location="W/m² by space type", text_as_read="plug-load densities by room use, taken as listed in the project design basis",
            method="Value taken from the project design basis. No project document gives a plug load", confidence="medium")
    add_src("ASM-LIGHT", document="ASHRAE Standard 90.1: space-by-space lighting power density", issue="edition to confirm", page="space-by-space method table",
            location="W/m² by space type", text_as_read="lighting power densities by room use, taken as listed in the project design basis",
            method="Value taken from the project design basis. No project document gives a lighting load", confidence="medium")
    add_src("ASM-EQUIP", document="Workbook defaults", issue="", page="", location="equipment gain",
            text_as_read="equipment gain 0 W: no equipment list is in the scope", method="Assumption, to confirm", confidence="low")
    add_src("ASM-EQUIPPROV", document="Provisional equipment gain by room use (W/m²)", issue="22 Nov 2026", page="provisional equipment table", location="W/m² by room use",
            text_as_read="data centre 1,000; communications 200; UPS and battery 100; imaging 100; laboratory 40; sterile services 80; kitchen 200; automation 40 (W/m²)",
            method="Stated engineering allowance, order of magnitude of the ASHRAE health-care and laboratory guidance. No equipment list is in the scope. Vendor data needed",
            confidence="low")
    soa_by_code: Dict[str, Dict[str, str]] = {}
    for r in soa_all:
        soa_by_code.setdefault(r["room_code"].strip(), r)

    def soa_area_of(code: str) -> Optional[float]:
        s_ = soa_by_code.get(code)
        return (_f(s_.get("as_drawn_area_m2")) or _f(s_.get("designed_area_m2")) or _f(s_.get("unit_area_m2"))) if s_ else None

    # rooms of the IP units, from the DPA (designed area governs)
    dpa_rows: List[Dict[str, Any]] = []
    for code, storey_stack in ip_map.items():
        by_id: Dict[str, List[Dict[str, str]]] = defaultdict(list)
        for r in dpa_units[code]:
            by_id[r["unique_room_identifier"]].append(r)
        for ident, group in by_id.items():
            if not keep_room(unit_info[code], group[0]["room_name"], group[0].get("fpu_group_name", "")):
                continue
            pick = next((g for g in group if (_f(g.get("designed_area_m2")) or 0) > 0), group[0])
            ident_storey = _storey_code(pick.get("storey_from_identifier") or "")
            soa_q = soa_by_code.get(ident)
            dpa_rows.append(dict(number=ident, name=clean_text(pick["room_name"]), storey=ident_storey or storey_stack, dpa_area=_f(pick.get("designed_area_m2")),
                                 code=(pick.get("soa_std_room_code") or "").strip(), typical_count=int(_f(soa_q.get("qty")) or 1) if soa_q else 1,
                                 ref_area=_f(pick.get("designed_area_m2")) or soa_area_of(ident),
                                 row=pick, unit=code, differ=len({_f(g.get("designed_area_m2")) for g in group}) > 1,
                                 storey_basis="identifier" if ident_storey else "stacking diagram"))
    dpa_rows.sort(key=lambda d: (SCOPE_STOREYS.index(d["storey"]) if d["storey"] in SCOPE_STOREYS else 99, d["number"]))
    out_of_scope = [d for d in dpa_rows if d["storey"] not in SCOPE_STOREYS]
    dpa_rows = [d for d in dpa_rows if d["storey"] in SCOPE_STOREYS]

    # plan rooms of the department, per storey
    first_word = re.sub(r"[^A-Za-z ]", "", dept_name).split()[0] if re.sub(r"[^A-Za-z ]", "", dept_name).split() else dept_name
    from .dept_units import plan_regex
    dept_re = re.compile(plan_regex(key) or rf"\(0*{int(key)}\)|^{re.escape(first_word)}\b", re.I)
    plan: List[Dict[str, Any]] = []
    for st in sorted({d["storey"] for d in dpa_rows}):
        for p in polygons.get(st, []):
            za = zones.get(st, {}).get(p["room_id"])
            if not za or not dept_re.search(za.get("department", "")):
                continue
            n_tags = int(_f(p.get("n_tags_in_region")) or 1)
            plan.append(dict(storey=st, name=p.get("room_name") or p.get("room_tag", ""), area=_f(p.get("a_area_m2")), poly=p, zone=za,
                             shared=n_tags > 1 or (p.get("region_kind") or "").startswith("open plan"), n_tags=n_tags,
                             cx=_f(p.get("centroid_x_mm")), cy=_f(p.get("centroid_y_mm")), grid=p.get("grid_location", "")))
    rejected: List[Dict[str, Any]] = []
    # a plan room with no measured area (a tag of a leaked region, a tag with no closed region) still carries its adjudicated AHU: it is not
    # paired one to one (no area to test) but it takes part in the zoning links, and it is listed when no DPA room uses it
    plan_area = [p for p in plan if p["area"] is not None]
    plan_no_area = [p for p in plan if p["area"] is None]
    pairs, un_dpa, un_plan = match_plan_rooms(dpa_rows, plan_area, abbr, rejected)
    un_plan = un_plan + plan_no_area
    rejected_by = {rj["dpa"]: rj for rj in rejected}
    # rooms inside an open-plan region: the polygon file lists the tags that share the region; a DPA room that no plan room matches but
    # whose name matches exactly one of these tags is in that region, and takes its zoning (never its area)
    region_of: Dict[str, Tuple[Dict[str, Any], str]] = {}
    for d in un_dpa:
        best: Dict[int, Tuple[float, str]] = {}
        for j, pr in enumerate(plan):
            if not pr["shared"] or pr["area"] is None or pr["storey"] != d["storey"]:
                continue
            tags = [t.strip() for t in (pr["poly"].get("other_tags") or "").split("|") if t.strip()]
            for t in tags:
                nm = re.split(r"\s/\s", t)[0].strip()
                sc = name_score(d["name"], nm, abbr)
                if sc >= 0.85 and sc > best.get(j, (0.0, ""))[0]:
                    best[j] = (sc, nm)
        if len(best) == 1:
            j, (sc, nm) = next(iter(best.items()))
            region_of[d["number"]] = (plan[j], nm)
    # DPA rooms that the strict match and the open-plan regions leave without a plan room: link by name and count, then by the department rule
    link_rejected: List[Dict[str, Any]] = []
    zlinks = plan_links([d for d in dpa_rows if d["number"] not in pairs and d["number"] not in region_of], plan, set(pairs) | set(region_of),
                        lambda a, b: zoning_name_score(a, b, abbr), AHU_COLUMNS, SYSTEM_KINDS, max_area_ratio=MAX_AREA_RATIO, rejected=link_rejected)
    link_rej_by = {n_: rj for rj in link_rejected for n_ in rj["dpa"]}
    count_plan_ids = {id(p) for z in zlinks.values() if z["basis"] == "name and count" for p in z["plan"]}
    rule_users: Dict[int, List[str]] = defaultdict(list)
    for no, z in zlinks.items():
        if z["basis"] == "department rule":
            for p in z["plan"]:
                rule_users[id(p)].append(no)
    # RDS and RLS links of every DPA room
    link_in = [dict(number=d["number"], name=d["name"], code=d["code"]) for d in dpa_rows]
    rds_link = link_rds(link_in, rds_all)
    rls_rows = load_rls(root)
    rls_link = link_rls(link_in, rls_rows, key)
    tol_pct = ((conf.config(root).get("tolerances") or {}).get("area_vs_soa") or {}).get("agree_pct", 3) / 100.0
    tol_m2 = ((conf.config(root).get("tolerances") or {}).get("area_vs_soa") or {}).get("agree_m2", 0.5)
    crit_stats: Dict[str, List[str]] = defaultdict(list)
    unmapped_crit: List[str] = []
    ahu_kinds: Dict[str, str] = {}
    rooms: List[Dict[str, Any]] = []
    n_rds = n_rds_occ = n_default = n_nouse = 0
    rds_basis_count: Dict[str, int] = defaultdict(int)
    zl_rule_count, zl_count_count = [0], [0]
    n_rls = n_ch_default = 0
    n_loads = n_loads_missing = 0
    n_presumed = 0
    n_own_mapped = 0
    n_hfds, n_hfds_diff = [0], [0]
    perim_of: Dict[str, Tuple[float, str]] = {}
    presumed_how: Dict[str, int] = defaultdict(int)
    from .presume import Presumption
    pres = Presumption(root, room_use, split_code, _expand_abbreviations)

    for d in dpa_rows:
        pick = d["row"]
        number = d["number"]
        sid = add_src(f"DPA-{_norm(number)}", document=_doc(pick["source_file"]), issue=f"DPA, Revit synchronisation {pick.get('revit_sync_date', '')}",
                      page=f"sheet Database, row {pick['row']}", location=f"room {number}",
                      text_as_read=f"designed {pick.get('designed_area_m2')} m²; programmed {pick.get('programmed_area_m2')} m²; name {d['name']}",
                      method=f"Value read from the DPA workbook, sheet Database, row {pick['row']}", confidence="high")
        room: Dict[str, Any] = dict(
            number=number, name=d["name"], storey=d["storey"], ahu=None, zone_tag=None, system_type=None, terminal_tag=None, exhaust_fan=None,
            space_type=UNCLASSIFIED, room_condition="Comfort", dpa_area_m2=d["dpa_area"], soa_area_m2=None, measured_area_m2=None,
            area_selector=None, ceiling_height_m=None, occupancy_people=None, occupancy_per_100m2=None, iof=None, activity=None,
            operation_hours=None, lighting_w_m2=None, small_power_w_m2=None, gaps={},
            prov={"identity": ["D", sid], "zoning": ["D", sid], "space_type": ["A", "ASM-DEFAULT"], "dpa_area": ["D", sid]},
            criteria={}, envelope=[])
        remark = [f"DPA row {pick['row']}: designed {pick.get('designed_area_m2')} m²"]
        if d["storey_basis"] != "identifier":
            remark.append("storey from the stacking diagram")
        gaps: Dict[str, str] = room["gaps"]
        if d["differ"]:
            gaps["area"] = "The DPA gives more than one designed area for this room identifier. The first non-zero value is used."
        if not d["dpa_area"]:
            gaps["area"] = f"DPA designed area is 0 m² (programmed {pick.get('programmed_area_m2')} m²). The SOA or the measured area is used."
        s = soa_by_code.get(number)
        if s is not None:
            sa = _f(s.get("as_drawn_area_m2")) or _f(s.get("designed_area_m2")) or _f(s.get("unit_area_m2"))
            ssid = add_src(f"SOA-{key}-{_norm(number)}", document=_doc(s["source_file"]), issue=f"{s.get('soa_version', '')} {s.get('issue_date', '')}".strip(),
                           page=s.get("page_or_sheet_row", ""), location=s.get("bbox_pt", ""), text_as_read=f"{s['room_name']}: as drawn area {s.get('as_drawn_area_m2')} m²",
                           method="Value read from the SOA table", confidence=str(s.get("confidence", "")))
            room["soa_area_m2"] = sa
            room["prov"]["soa_area"] = ["D", ssid]
            remark.append(f"SOA {s.get('soa_version', '')} {sa:.1f} m²" if sa is not None else "SOA row without area")
            if (_f(s.get("qty")) or 1) > 1:
                gaps["area"] = f"SOA quantity {s.get('qty')}: the SOA area is the unit area."
        p = pairs.get(number)
        member = None
        if p is None and number in region_of:
            p, member = region_of[number]
        if p is not None:
            poly, za = p["poly"], p["zone"]
            psid = add_src(f"PLAN-{poly['room_id']}", document=_doc(poly.get("source_file", "")) or "Architectural tender drawings", issue="plan measurement",
                           page=f"page {poly.get('source_page', '')}, storey {p['storey']}", location=f"room {poly.get('room_tag', '')}, grid {poly.get('grid_location', '')}",
                           text_as_read=f"plan room {poly.get('room_tag', '')}; area {poly.get('a_area_m2')} m² (measure A), {poly.get('b_area_m2')} m² (measure B); {poly.get('method_agreement', '')}",
                           method="Area measured from the room outline on the drawing", confidence=str(poly.get("method_agreement", "")))
            base_area = d["dpa_area"] if d["dpa_area"] else soa_area_of(number)
            if p.get("shared"):
                gaps["area_diff"] = (f"Plan room {poly.get('room_tag', '')} is an open-plan region ({p['area']:.1f} m², grid {p['grid']}). "
                                     f"{p['n_tags']} room tags share it." + (f" This room is its tag '{member}'." if member else "") +
                                     " The area of the region is not the area of this room. The measured area is not used. The zoning of the region is used.")
                remark.append(f"plan room {poly.get('room_tag', '')} is open plan ({p['n_tags']} tags share it)")
                remark.append("measured area not used")
            else:
                room["measured_area_m2"] = p["area"]
                room["prov"]["measured_area"] = ["M", psid]
                per_ = _f(poly.get("a_perimeter_m"))
                if per_:
                    perim_of[number] = (per_, psid)
                if env_model.available and p["storey"] in envm.ENVELOPE_STOREYS and poly.get("polygon_building_mm_wkt"):
                    # D-042, D-049, D-066, D-071: envelope rows by the ETTV component method, simple facade method
                    erows = env_model.rows(p["storey"], number, poly["polygon_building_mm_wkt"], "ETTV-ENV")
                    if erows:
                        room["envelope"] = erows
                        env_rooms += 1
                        remark.append("external room: envelope rows by the simple facade method (ETTV ratio x facade length x storey height), to confirm")
                remark.append(f"plan room {poly.get('room_tag', '')} {p['area']:.1f} m², grid {p['grid']}")
                if base_area and abs(base_area - p["area"]) > max(tol_m2, tol_pct * base_area):
                    gaps["area_diff"] = (f"Plan room {poly.get('room_tag', '')} (grid {p['grid']}) measures {p['area']:.1f} m². "
                                         f"The design area is {base_area:.1f} m² ({(p['area'] - base_area) / base_area * 100:+.0f} %), outside the tolerance. "
                                         f"The AUTO selector uses the measured area when the two areas disagree. "
                                         f"Confirm which area is right, or set the selector on Room Information.")
            kind = (za.get("class_kind") or "").upper()
            tag = _first(za, AHU_COLUMNS) or (za.get("class", "").strip() if kind in SYSTEM_KINDS else "")
            if tag:
                room["ahu"] = tag
                room["zone_tag"] = _first(za, ZONE_COLUMNS) or None
                room["system_type"] = _first(za, SYSTYPE_COLUMNS) or ("FCU" if kind in ("FCU", "DXFCU") else None)
                zsid = add_src(f"ZONE-{p['storey']}-{poly['room_id']}", document=f"AHU zoning, storey {p['storey']}", issue="zoning assignment",
                               page=p["storey"], location=f"plan room {poly['room_id']}",
                               text_as_read=f"class {za.get('class', '')} ({za.get('class_kind', '')}); department {za.get('department', '')}; basis: {basis_text(za.get('method', ''))}",
                               method="Zoning of the storey, as printed on the room tag of the plan", confidence=str(za.get("confidence", "")))
                room["prov"]["zoning"] = ["D", zsid]
                ahu_kinds.setdefault(tag, kind or "AHU")
            else:
                gaps["ahu"] = f"The plan room shows class '{za.get('class', '')}' and no system tag."
                if (za.get("class") or "").strip():                 # a zoning class with no unit tag (MV, NV, no information) is the system class of the room
                    zsid = add_src(f"ZONE-{p['storey']}-{poly['room_id']}", document=f"AHU zoning, storey {p['storey']}", issue="zoning assignment",
                                   page=p["storey"], location=f"plan room {poly['room_id']}",
                                   text_as_read=f"class {za.get('class', '')} ({za.get('class_kind', '')}); department {za.get('department', '')}; basis: {basis_text(za.get('method', ''))}",
                                   method="Zoning of the storey, as printed on the room tag of the plan", confidence=str(za.get("confidence", "")))
                    room["system_type"] = za.get("class", "").strip().upper()
                    room["prov"]["zoning"] = ["D", zsid]
        else:
            rj = rejected_by.get(number)
            notes_plan: List[str] = []
            if rj is not None:
                pl = rj["plan"]
                if rj["reason"] == "area":
                    notes_plan.append(f"Plan room {pl['poly'].get('room_tag', '')} has a similar name ({pl['name']}, grid {pl['grid']}). It measures {pl['area']:.1f} m². "
                                      f"The design area is {d['ref_area']:.1f} m², more than a factor of {MAX_AREA_RATIO:g} apart. It is not accepted as this room.")
                else:
                    notes_plan.append(f"Plan room {pl['poly'].get('room_tag', '')} has the same name ({pl['name']}). It lies outside the grid span of the department "
                                      f"on this storey (grid {pl['grid']}). It is not accepted as this room.")
            zl = zlinks.get(number)
            if zl is not None:
                plans = zl["plan"]
                plan_names = "', '".join(dict.fromkeys(x["name"] for x in plans))
                ids = ", ".join(x["poly"]["room_id"] for x in plans)
                zsid = add_src((f"ZONE-{d['storey']}-{_norm(ids)}" if len(_norm(ids)) <= 36 else f"ZONE-{d['storey']}-{hashlib.sha1(ids.encode()).hexdigest()[:12]}"), document=f"AHU zoning, storey {d['storey']}", issue="zoning assignment", page=d["storey"],
                               location=f"plan rooms {ids}", text_as_read=f"{len(plans)} plan room(s) named {plan_names}; class {zl['value']}; department "
                               f"{plans[0]['zone'].get('department', '')}", method="Zoning of the storey, as printed on the room tags of the plan", confidence="medium")
                if zl["basis"] == "name and count":
                    notes_plan.append(f"AHU by name and count. Storey {d['storey']} has {zl['n_dpa']} DPA room(s) and {zl['n_plan']} plan room(s) named '{plan_names}'. "
                                      f"All of them carry {zl['value']}. The plan rooms are not paired one to one, so the measured area is not used.")
                    zcode = "D"
                else:
                    notes_plan.append(f"AHU by department rule: no plan room matches this room one to one. All {zl['n_plan']} plan room(s) named '{plan_names}' in the department "
                                      f"on storey {d['storey']} carry {zl['value']}. The room takes it. The measured area is not used. Confirm.")
                    zcode = "A"
                    zl_rule_count[0] += 1
                if zl["basis"] == "name and count":
                    zl_count_count[0] += 1
                    if env_model.available and d["storey"] in envm.ENVELOPE_STOREYS and zl.get("n_dpa"):
                        erows = env_model.typical_rows(d["storey"], number, [x["poly"].get("polygon_building_mm_wkt") for x in plans], int(zl["n_dpa"]), "ETTV-ENV")
                        if erows:
                            room["envelope"] = erows
                            env_rooms += 1
                            remark.append("external typical room: envelope rows from the plan rooms of the same name (simple facade method), to confirm")
                if zl["tag"]:
                    room["ahu"] = zl["tag"]
                    room["zone_tag"] = _first(plans[0]["zone"], ZONE_COLUMNS) or None
                    room["system_type"] = _first(plans[0]["zone"], SYSTYPE_COLUMNS) or ("FCU" if zl["kind"] in ("FCU", "DXFCU") else None)
                    room["prov"]["zoning"] = [zcode, zsid]
                    ahu_kinds.setdefault(zl["tag"], zl["kind"] or "AHU")
                else:
                    gaps["ahu"] = f"The plan rooms of the same name show class '{zl['value']}' and no system tag."
                    if str(zl["value"] or "").strip():
                        room["system_type"] = str(zl["value"]).strip().upper()
                        room["prov"]["zoning"] = [zcode, zsid]
            elif number in link_rej_by:
                lr = link_rej_by[number]
                notes_plan.append(f"No unit link: the plan room(s) named '{lr['plan_name']}' on storey {d['storey']} measure {lr['plan_area']:.1f} m², and the DPA room(s) that fit "
                                  f"that name have {lr['dpa_area']:.1f} m² ({'ratio ' + format(lr['dpa_area'] / lr['plan_area'], '.1f')}). The areas are more than a factor of "
                                  f"{MAX_AREA_RATIO:g} apart, so the plan room is not taken as this room. The measured area is not used.")
            elif rj is None:
                notes_plan.append("No plan room matches this room by name, department and storey.")
            gaps["plan"] = " ".join(notes_plan)
        _soa_a, _dpa_a, _meas_a = room.get("soa_area_m2"), d["dpa_area"], room.get("measured_area_m2")
        if _soa_a and _dpa_a and abs(_dpa_a - _soa_a) > max(tol_m2, tol_pct * _soa_a):
            # D-067: DPA and SOA differ beyond the tolerance. A third source (plan outline of a one-to-one linked room) decides; with none, the SOA as drawn; code A, VERIFY.
            third = None
            if _meas_a:
                near = [(abs(_meas_a - v), nm) for v, nm in ((_dpa_a, "DPA"), (_soa_a, "SOA")) if abs(_meas_a - v) <= max(tol_m2, tol_pct * v)]
                third = min(near)[1] if near else None
            pick = third or "SOA"
            room["area_selector"] = pick
            room["prov"]["area_selector"] = ["A", "GEN-BASIS"]
            ratio_ = max(_dpa_a, _soa_a) / max(min(_dpa_a, _soa_a), 1e-9)
            why_ = (f"the plan outline ({_meas_a:.1f} m²) supports the {third}" if third else "no third source supports either value, so the SOA area as drawn is used")
            gaps["area_diff"] = (f"The DPA designed area is {_dpa_a:.1f} m² and the SOA area is {_soa_a:.1f} m² (ratio {ratio_:.1f}). The {pick} area is used: {why_}. VERIFY. "
                                 "Confirm which area is right.")
            flags.append(dict(type="VERIFY", ref=number, text=f"Room {number} {d['name']}: DPA {_dpa_a:.1f} m² and SOA {_soa_a:.1f} m² differ beyond the tolerance"
                              + (" (ratio above 2: not settled by the rule, FAIL until the area is confirmed)" if ratio_ > 2 and not third else "") + f". Used: {pick}.", source=""))
            remark.append(f"area {pick} used where DPA and SOA differ (assumption, VERIFY)")
        hl = rls_link["heights"].get(number)
        hc = next((c for c in rls_link["conflicts"] if c["number"] == number), None)
        if hl is not None:
            src_f = rls_source_fields(hl["row"])
            hsid = add_src(f"RLS-{_norm(hl['code'])}"[:60], document=src_f["document"], issue="room layout sheet", page=f"page {src_f['page']}" if src_f["page"] else "",
                           location=f"room type {hl['code']}; linked to the DPA room by {hl['basis']}",
                           text_as_read=src_f["text"] or f"ceiling height {hl['height_m'] * 1000:.0f} mm",
                           method="Value read from the ceiling height note of the room layout sheet", confidence="high")
            room["ceiling_height_m"] = hl["height_m"]
            room["prov"]["ceiling_height"] = ["D", hsid]
            remark.append(f"ceiling height {hl['height_m']:.2f} m from the room layout sheet ({hl['basis']})"
                          + (f"; the sheet prints {', '.join(f'{v * 1000:.0f}' for v in hl['values'])} mm and the highest is used" if len(hl.get("values") or []) > 1 else ""))
            n_rls += 1
        elif ch_default:
            room["ceiling_height_m"] = ch_default
            room["prov"]["ceiling_height"] = ["A", "ASM-CEILING"]
            why = (height_conflict_text(hc).replace(". ", "; ") if hc
                   else ("no room layout sheet height is linked to this room" if rls_rows else "no room layout sheet height is available"))
            remark.append(f"ceiling height {ch_default:g} m assumed ({why})")
            if hc:
                gaps["height"] = height_conflict_text(hc)[0].upper() + height_conflict_text(hc)[1:] + "."
            n_ch_default += 1
        row, variants, how = None, [], ""
        crit_done = False
        if key.lstrip("0") == "22":                                  # operating theatres and anaesthesia preparation rooms: fixed by the MOT NSC specification
            spec = mot.theatre(number) or (mot.prep(number, d["name"]) if d["name"].startswith("Anaesthesia Prep") else None)
            if spec is not None:
                msid = add_src(f"MOT-{_norm(number)}"[:60], document="MOT NSC air-side schematic and schedule of modes", issue="Corr 2", page=spec["sheet"],
                               location=spec["what"], text_as_read=spec["text"], method="Value read from the room box of the MOT NSC air-side schematic", confidence="medium")
                booklet = [c for c in criteria if c["room_type_as_printed"].strip().lower() == ("operating theatre" if d["name"].startswith("Operating") else "anaesthetic prépara6on")
                           or (not d["name"].startswith("Operating") and c["room_type_as_printed"].lower().startswith("anaesthe") and cm.suite_of(number) == "DSOT" and 111 <= (_f(c["booklet_page"]) or 0) <= 112)]
                booklet = [c for c in booklet if (cm.suite_of(number) == "DSOT") == (110 <= (_f(c["booklet_page"]) or 0) <= 113)]
                comp = ""
                if booklet:
                    b = booklet[0]
                    comp = (f" Booklet row '{b['room_type_as_printed']}' (page {b['booklet_page']}), for comparison only: outdoor air {b['min_oa_ach']} and total {b['min_total_ach']} "
                            f"air changes, pressure {b['pressure_regime_as_printed']}.")
                room["criteria"] = dict(criteria_row=f"MOT NSC specified: {'operating theatre' if d['name'].startswith('Operating') else 'anaesthesia preparation room'}",
                                        page=spec["sheet"], supply_specified_lps=spec["supply"], oa_specified_lps=spec["oa"], exhaust_specified_lps=spec["exhaust"],
                                        temp_min_c=spec["tmin"], temp_max_c=spec["tmax"], rh_max_pct=spec["rh"],
                                        pressure_regime=("POSITIVE" if (spec["pa"] or 0) > 0 else "NEGATIVE" if (spec["pa"] or 0) < 0 else None),
                                        pressure_pa_tender=spec["pa"], air_class=spec["air_class"], filter=spec["filter"],
                                        single_pass="Y" if spec["mode"] == "single pass" else None, source=msid, code="S",
                                        remark=(f"Specified by the MOT NSC ({spec['what']}, unit {{UNIT}}), mode '{spec['mode']}': {spec['text']}. The specified airflows are not resized." + comp))
                crit_stats["MOT NSC specified"].append(number)
                crit_done = True
                if hepa is not None and d["name"].startswith("Operating"):
                    # D-039, D-057: supply of a laminar theatre = HEPA face area x design face velocity; capability at the upper velocity; no reduction on the unit
                    hs = hepa.for_tags(list(spec.get("sch_tags") or []) + [mot.theatre_ahu.get(number) or ""])
                    if hs is not None:
                        hsid = add_src(f"HEPA-{_norm(hs['terminal'])}"[:60], document="MOT NSC HEPA terminal schedule (SCH035) and Tech Specs for ACMV Services, clause 1.17.4", issue="Corr 3",
                                       page="schedule row of the terminal", location=hs["terminal"], text_as_read=hepa.check_text(hs),
                                       method="Face area from the printed module size times the Max Face Velocity of the schedule", confidence="high")
                        c_ = room["criteria"]
                        c_["supply_specified_lps"] = hs["design"]
                        if (c_.get("oa_specified_lps") or 0) > hs["design"]:
                            c_["oa_specified_lps"] = hs["design"]
                        c_["remark"] += " " + hepa.check_text(hs) + f" The room supply follows the HEPA terminal face area and velocity (Source ID {hsid}); the schematic airflow is a comparison."
                        c_["source"] = msid
                        room["prov"]["hepa"] = ["S", hsid]
                        room["_hepa"] = hs
                        remark.append(f"supply {hs['design']:,.0f} L/s from the HEPA terminal {hs['terminal']} (face area x {hepa.v_design:g} m/s)")
                if not room["ahu"]:                     # the unit of a theatre is the one that the schedule of modes names; the preparation room is on the unit of its theatre
                    utag = mot.theatre_ahu.get(number) if d["name"].startswith("Operating") else (spec.get("unit_tag") or (spec.get("sch_tags") or [None])[0])
                    if utag and re.match(r"^AHU-B-\d\d-\d\d", utag):
                        utag = utag.replace("lSONP", "ISONP")
                        if re.search(r"-\d{2}A$", utag):
                            utag = zone_key(utag) + "A/B"
                        room["ahu"] = utag
                        room["prov"]["zoning"] = ["D", msid]
                        remark.append(f"unit {utag} from the MOT NSC schedule of modes and air-side schematic")
                        gaps.pop("ahu", None)
                        ahu_kinds.setdefault(utag, "AHU")
                # R3-06: the unit named in the remark is the unit of the room. A different unit tag in the MOT NSC documents is a conflict: it is listed on Flags.
                unit_final = room["ahu"] or "not set"
                room["criteria"]["remark"] = room["criteria"]["remark"].replace("{UNIT}", unit_final)
                mot_unit = (mot.theatre_ahu.get(number) if d["name"].startswith("Operating") else (spec.get("unit_tag") or (spec.get("sch_tags") or [None])[0])) or ""
                mot_unit = mot_unit.replace("lSONP", "ISONP")
                if re.search(r"-\d{2}A$", mot_unit):
                    mot_unit = zone_key(mot_unit) + "A/B"
                conflict = []
                if spec.get("tag_conflict"):
                    conflict.append(spec["tag_conflict"][0].upper() + spec["tag_conflict"][1:])
                if room["ahu"] and mot_unit and zone_key(mot_unit) != zone_key(room["ahu"]):
                    conflict.append(f"The MOT NSC documents name the unit {mot_unit} for this room. The zoning of the plan gives {room['ahu']}")
                if conflict:
                    room["criteria"]["remark"] += " The documents name different units for this room: listed on Flags."
                    remark.append("the documents name different units for this room (listed on Flags)")
                    flags.append(dict(type="VERIFY", ref=number, text=f"Room {number} {d['name']}: " + ". ".join(conflict) + f". The unit used is {unit_final}. Confirm the unit.", source=msid))
        if not crit_done and key.lstrip("0") == "42":
            dc = cm.datacentre_criteria(d["name"], d["dpa_area"] or soa_area_of(number), dc_table, _regime)
            if dc is not None:
                csid = add_src(f"CRIT-42-{_norm(dc['criteria_row'])}-{dc['page'].split()[-1]}"[:60], document="DD Booklet, Data Centre (criteria table read from the page image)", issue="R2",
                               page=dc["page"], location="criteria table", text_as_read="; ".join(f"{k} {v}" for k, v in dc["_row"].items() if not k.startswith("_") and k != "Room"),
                               method="Value read from the page image of the DD Booklet criteria table", confidence="medium")
                note_ = dc.pop("_note"); dc.pop("_pdf_page"); dc.pop("_file"); dc.pop("_row")
                dc.update(source=csid, code="D", remark=(f"Booklet room type '{dc['criteria_row']}'. " + (note_ + "." if note_ else "")).strip())
                if d["name"] not in (dc["criteria_row"],):
                    dc["code"] = "A"
                    dc["remark"] += f" The room '{d['name']}' is mapped to this room type by name; confirm."
                room["criteria"] = dc
                crit_stats[f"{dc['criteria_row']} (Data Centre booklet)"].append(number)
                crit_done = True
        if not crit_done and key.lstrip("0") == "31" and d["storey"] == "L02" and d["unit"] == "ANWSIP2" and re.match(r"^Office\b", d["name"]):
            # the L02 admin neighbourhood area is future EDTU and is treated as office: the office row of the Admin Workspace booklet (dept 47), ED part
            orow = next((c for c in all_criteria if c["dept_no"].lstrip("0") == "47" and c["room_type_as_printed"].strip() == "ED Admin Workspace Office"), None)
            if orow is not None:
                csid = add_src(f"CRIT-31-{_norm(orow['room_type_as_printed'])}"[:60], document=_doc(orow["source_file"]), issue=orow.get("department_label", "booklet"),
                               page=f"page {orow['booklet_page']}", location=orow.get("bbox", ""), text_as_read=criteria_text(orow),
                               method="Value read from the DD Booklet criteria table", confidence=str(orow.get("confidence", "")))
                room["criteria"] = criteria_from_row(orow, csid, "room type mapped", "A",
                                                     f"Future EDTU, treated as office. Booklet room type '{orow['room_type_as_printed']}' (page {orow['booklet_page']}) of the Admin Workspace booklet. Confirm the mapping.")
                crit_stats[orow["room_type_as_printed"] + " (mapped)"].append(number)
                crit_done = True
        if not crit_done:
            mp = cm.mapped_row(key, d["name"], number, criteria, _regime)
            if mp is not None:
                mrow, why = mp
                csid = add_src(f"CRIT-{key}-{_norm(mrow['room_type_as_printed'])}"[:60], document=_doc(mrow["source_file"]), issue=mrow.get("department_label", "booklet"),
                               page=f"page {mrow['booklet_page']}", location=mrow.get("bbox", ""), text_as_read=criteria_text(mrow),
                               method="Value read from the DD Booklet criteria table", confidence=str(mrow.get("confidence", "")))
                room["criteria"] = criteria_from_row(mrow, csid, "room type mapped", "A",
                                                     f"Booklet room type '{mrow['room_type_as_printed']}' (page {mrow['booklet_page']}) mapped to the room type '{d['name']}': {why}. Confirm the mapping.")
                crit_stats[mrow["room_type_as_printed"] + " (mapped)"].append(number)
                _sig = lambda c: (c.get("min_oa_ach"), c.get("min_total_ach"), c.get("pressure_pa"), c.get("pressure_regime_as_printed"), c.get("air_class"))
                _var = [c for c in criteria if c["room_type_norm"] == mrow["room_type_norm"] and _sig(c) != _sig(mrow)]
                if _var:
                    gaps["criteria"] = _variant_text(mrow, _var)
                crit_done = True
        if not crit_done:
            row, variants, how = match_criteria(d["name"], criteria)
        own_hit = None
        if not crit_done and row is None:
            # D-026 applies only where the own department has no row for the space: the own booklet prints the same space with a level or department prefix, or a number
            own_hit = pres.find_own(key, d["name"], d["storey"], dept_name)
        if crit_done:
            pass
        elif own_hit is not None:
            orow = own_hit["row"]
            csid = add_src(f"CRIT-{key}-{_norm(orow['room_type_as_printed'])}"[:60], document=_doc(orow["source_file"]), issue=orow.get("department_label", "booklet"),
                           page=f"page {orow['booklet_page']}", location=orow.get("bbox", ""), text_as_read=criteria_text(orow),
                           method="Value read from the DD Booklet criteria table", confidence=str(orow.get("confidence", "")))
            room["criteria"] = criteria_from_row(orow, csid, own_hit["how"], "A",
                                                 f"Booklet room type '{orow['room_type_as_printed']}' (page {orow['booklet_page']}) of the booklet of this department, mapped to the room type "
                                                 f"'{d['name']}': {own_hit['reason']}. Confirm the mapping.")
            crit_stats[orow["room_type_as_printed"] + " (own booklet, mapped)"].append(number)
            n_own_mapped += 1
        elif row is not None:
            csid = add_src(f"CRIT-{key}-{_norm(row['room_type_as_printed'])}"[:60], document=_doc(row["source_file"]), issue=row.get("department_label", "booklet"),
                           page=f"page {row['booklet_page']}", location=row.get("bbox", ""), text_as_read=criteria_text(row),
                           method="Value read from the DD Booklet criteria table", confidence=str(row.get("confidence", "")))
            if how.startswith("name listed"):
                room["criteria"] = criteria_from_row(row, csid, how, "A", f"The name '{d['name']}' is listed inside the booklet row '{row['room_type_as_printed']}' (page {row['booklet_page']}). Confirm the mapping.")
            else:
                room["criteria"] = criteria_from_row(row, csid, how)
            crit_stats[row["room_type_as_printed"]].append(number)
            if variants:
                gaps["criteria"] = _variant_text(row, variants)
        else:
            # D-026: no row in the own department. The same space in the closest other department gives a presumed row (code A, italic green).
            rl0 = rds_link["links"].get(number)
            hit, why_p = None, ""
            for code_p in dict.fromkeys(c_ for c_ in (d["code"], (rl0 or {}).get("code")) if c_):
                hit, why_p = pres.find(key, d["name"], code_p)
                if hit:
                    break
            if not hit:
                hit, why_p = pres.find(key, d["name"], None)
            if hit:
                prow = hit["row"]
                dep_label = prow.get("department_label") or f"department {hit['department']}"
                csid = add_src(f"CRP-{key}-{hit['department']}-{_norm(prow['room_type_as_printed'])}"[:60], document=_doc(prow["source_file"]), issue=prow.get("department_label", "booklet"),
                               page=f"page {prow['booklet_page']}", location=prow.get("bbox", ""), text_as_read=criteria_text(prow),
                               method="Value read from the DD Booklet criteria table of another department", confidence=str(prow.get("confidence", "")))
                ptpl = str((conf.presumed_cfg(root).get("remarks_template")) or "Presumed criteria: taken from {department} booklet p{page}, {row}. No row in this department. To confirm.")
                ptxt = ptpl.format(department=dep_label, page=prow["booklet_page"], row=prow["room_type_as_printed"].rstrip(". "))
                room["criteria"] = criteria_from_row(prow, csid, hit["how"], "A", ptxt + f" Match: {hit['how']}.")
                room["criteria"]["presumed"] = True
                remark.append(ptxt)
                crit_stats[f"{prow['room_type_as_printed']} (presumed from {dep_label})"].append(number)
                n_presumed += 1
                presumed_how[hit["how"].split(" (")[0]] += 1
            else:
                unmapped_crit.append(number)
                unmapped_names[d["name"]].append(number)
                gaps["criteria"] = (f"No criteria row: {why_p}. The room has no criteria row in its own department" + (f" ({how})" if how else "") +
                                    ". Confirm the room type.")
        # D-036, D-037, D-064: rooms with no booklet row (and plant, comms, riser, IT, UPS rooms on a presumed row) take the HFDS row; a mapped or presumed row is compared with it
        hit_h = hf.find_row(root, d["name"])
        if hit_h is not None:
            hrow, hkind, hwhy = hit_h
            c_cur = room["criteria"]
            hsid_ = add_src(f"HFDS-{_norm(hrow['table_id'])}-{_norm(hrow['area_name'])}"[:60], **hf.source_fields(hrow))
            if not c_cur or (hkind in ("it", "plant", "riser") and c_cur.get("code") == "A"):
                room["criteria"] = hf.to_criteria(hrow, hsid_, hkind, hwhy)
                crit_stats[f"HFDS {hrow['area_name']} (no booklet row)"].append(number)
                if number in unmapped_crit:
                    unmapped_crit.remove(number)
                    if number in unmapped_names.get(d["name"], []):
                        unmapped_names[d["name"]].remove(number)
                        if not unmapped_names[d["name"]]:
                            del unmapped_names[d["name"]]
                gaps.pop("criteria", None)
                remark.append(f"criteria from the HFDS ({hrow['area_name']}, table {hrow['table_id']}, page {hrow['printed_page']})")
                n_hfds[0] += 1
                if hkind == "it" and not room["ahu"] and not room.get("system_type"):
                    room["system_type"] = "FCU"
                    room["prov"]["zoning"] = ["A", hsid_]
            elif c_cur.get("code") == "A":
                diffs = hf.compare(c_cur, hrow)
                tag_ = (f"HFDS cross-check ({hrow['area_name']}, table {hrow['table_id']}, page {hrow['printed_page']}): "
                        + ("agrees." if not diffs else "differs: " + "; ".join(diffs) + ". The row of the booklet governs. VERIFY."))
                c_cur["remark"] = (c_cur.get("remark") or "") + " " + tag_
                if diffs:
                    flags.append(dict(type="VERIFY", ref=number, text=f"Room {number} {d['name']}: {tag_}", source=hsid_))
                    n_hfds_diff[0] += 1
        rl = rds_link["links"].get(number)
        rds = rl["resolved"]["row"] if rl else None
        rds_conflict = rl["resolved"]["conflict"] if rl else []
        rds_unlinked = next((u for u in rds_link["unlinked_dpa"] if u["number"] == number), None)
        if rl:
            n_rds += 1
            rds_basis_count[rl["basis"]] += 1
            remark.append(f"RDS room code {rl['code']} linked by {rl['basis']}")
            if rl.get("code_note"):
                remark.append("RDS code disagreement: " + rl["code_note"])
                gaps["rds_code"] = (f"The room data sheet is linked by name only. {rl['code_note'][0].upper()}{rl['code_note'][1:]}. "
                                    "Confirm that this is the sheet of this room.")
        else:
            remark.append("no RDS linked" + (f" ({rds_unlinked['reason']})" if rds_unlinked else ""))
        occ = occupancy_from_rds(rds) if not rds_conflict else None
        if rds_conflict:
            n_nouse += 1
            room["occupancy_basis"] = "NOT SET"
            remark.append("occupancy not set (the RDS issues disagree)")
            gaps["occupancy"] = ("The RDS gives different occupancy texts for this room in the same issue: " + "; ".join(f"'{t}'" for t in rds_conflict) +
                                 ". None is chosen. Confirm the occupancy.")
        elif occ is not None:
            n_rds_occ += 1
            rsid = add_src(f"RDS-{key}-{_norm(rds['room_code'])}"[:60], document=_doc(rds["rel_path"]), issue=f"{rds.get('revision', '')} {rds.get('date', '')}".strip(),
                           page="room data sheet, occupancy field", location=f"room code {rds['room_code']}; linked to the DPA room by {rl['basis']}", text_as_read=f"Occupancy: {occ[1]}",
                           method=f"Value read from the room data sheet {_doc(rds['rel_path'])}, occupancy field", confidence="high")
            room["occupancy_people"] = occ[0]
            room["occupancy_basis"] = "RDS"
            room["prov"]["occupancy_people"] = ["D", rsid]
            remark.append(f"RDS occupancy '{occ[1]}' = {occ[0]:g} people" + (" (kept as an RDS value)" if occ[0] == 0 else " (sum of upper bounds, a fraction rounded up)"))
            onotes = occupancy_notes(occ[1])
            if "intermittent" in onotes:
                gaps["occupancy_note"] = (f"The RDS occupancy '{occ[1]}' says that the people are there intermittently. The text is kept as printed; "
                                          f"the design occupancy is the upper bound, {occ[0]:g} people. Confirm the design occupancy.")
            if "shifts" in onotes:
                gaps["occupancy_note"] = (f"The RDS occupancy '{occ[1]}' gives two shifts. The design occupancy is the sum of the upper bounds, {occ[0]:g} people, as the "
                                          "reading rule sets. The shifts are not present at the same time: the peak at one time is lower. Confirm the design occupancy.")
            if occ[0] == 0 and not gaps.get("occupancy_note"):
                use0, _lab0 = room_use(d["name"])
                if use0 in OCCUPIED_USES:
                    gaps["occupancy_note"] = (f"RDS occupancy '{occ[1]}' taken as 0 persons (RDS value). The room use is normally occupied. To confirm.")
            if occ[0] > 0:
                room["activity"] = "Seated"
                room["prov"]["activity"] = ["A", "ASM-ACTIVITY"]
        else:
            use, label = room_use(d["name"])
            if use is not None and use in densities:
                room["occupancy_per_100m2"] = float(densities[use])
                room["occupancy_basis"] = "DENSITY DEFAULT"
                room["occupancy_use"] = use.replace("_", " ")
                room["prov"]["occupancy_density"] = ["A", "OCC-DENSITY"]
                if densities[use]:
                    room["activity"] = "Seated"
                    room["prov"]["activity"] = ["A", "ASM-ACTIVITY"]
                n_default += 1
                remark.append(("no RDS for this room" if rds is None else "RDS occupancy field empty") + f"; room use {use.replace('_', ' ')}")
                if re.search(r"hand ?-?wash|scrub bay|scrub up", d["name"], re.I):
                    remark.append("Hand-wash bay: use taken as circulation, 0 persons. To confirm")
                if rl and rl["resolved"]["notes"]:
                    remark.append("RDS " + "; ".join(rl["resolved"]["notes"]))
            else:
                n_nouse += 1
                room["occupancy_basis"] = "NOT SET"
                remark.append("occupancy not set")
                gaps["occupancy"] = ("Occupancy is not set. " + ("The RDS field is empty." if rds is not None else "No RDS row matches.") +
                                     (f" The density table has no value for this room use ({label.split(' (')[0]}). " if label else f" The room name '{d['name']}' does not say what the room is used for, so no room use fits it for the default density. ") +
                                     "Give the number of persons.")
        if re.search(r"\.\.|\.$", number):
            gaps["number"] = (f"The room number '{number}' in the DPA is incomplete: it ends in full stops where the storey and the room digits are missing. "
                              "The number is kept as the DPA prints it. Confirm the room number.")
        _c0 = room.get("criteria") or {}
        if (_c0.get("oa_lps_per_person") or 0) > 0 and (room.get("occupancy_people") == 0 or room.get("occupancy_per_100m2") == 0):
            gaps["oa_person"] = (f"The criteria row prints {_c0['oa_lps_per_person']:g} L/s of outdoor air per person. The occupancy of this room is 0 persons, so this rate gives no outdoor air "
                                 "and the outdoor air is 0 unless another value sets it. Confirm the occupancy.")
        # D-025: plug load and lighting from the ASHRAE values of the room use (no project document gives them). Cells red, note in Remarks.
        lk, lwhy = load_use(d["name"])
        if lk and lk in plug_tab and lk in light_tab:
            room["lighting_w_m2"], room["small_power_w_m2"] = float(light_tab[lk]), float(plug_tab[lk])
            room["prov"]["lighting"], room["prov"]["small_power"] = ["A", "ASM-LIGHT"], ["A", "ASM-PLUG"]
            room["load_basis"], room["load_use"] = "ASHRAE", lwhy
            n_loads += 1
        else:
            gaps["loads"] = ("Lighting and small power are not set. " + ("The room name does not say what the room is used for, so no ASHRAE room use fits it." if not lk
                             else "The ASHRAE tables of the project have no value for this room use.") + " Give the room use or the values.")
            n_loads_missing += 1
        room["remark"] = ". ".join(_sentence_case(x.replace("; ", ". ")).rstrip(". ") for x in remark if x.strip(". ")) + "."
        room["remark"] += " " + ROOM_THERMAL_NOTE
        eq_prov = r5.equipment_provisional(d["name"], root)
        if eq_prov is not None:
            eq_use, eq_w = eq_prov
            eq_area = d["dpa_area"] or soa_area_of(number) or _f((room.get("measured_area_m2"))) or 0.0
            room["equipment_sensible_w"], room["equipment_latent_w"] = round(eq_w * eq_area, 0), 0.0
            room["prov"]["equipment_sensible"] = room["prov"]["equipment_latent"] = ["A", "ASM-EQUIPPROV"]
            room["equip_basis"], room["equip_w_m2"], room["equip_use"] = "PROVISIONAL", eq_w, eq_use
            room["remark"] += (f" Equipment gain is a provisional {eq_w:g} W/m² ({eq_use.replace('_', ' ')}) on {eq_area:.1f} m², no vendor data: stated allowance, to confirm.")
            gaps["equipment"] = (f"Equipment gain is a provisional {eq_w:g} W/m² for this equipment-led room ({eq_use.replace('_', ' ')}). No equipment list or vendor data is in the scope. "
                                 "VERIFY with vendor data.")
        elif EQUIPMENT_LED.search(d["name"]):
            gaps["equipment"] = "Equipment gain is 0 W: no equipment list is in the scope. VERIFY: the room is led by equipment, so the thermal load is not representative."
            room["equipment_sensible_w"], room["equipment_latent_w"] = 0.0, 0.0
            room["prov"]["equipment_sensible"] = room["prov"]["equipment_latent"] = ["A", "ASM-EQUIP"]
        if room["ahu"] and cath_tag_note(room["ahu"]):
            room["remark"] += " " + cath_tag_note(room["ahu"])
        if room["ahu"] and ucv_unit_note(room["ahu"]) and room.get("criteria") and "operating" in str(room["criteria"].get("criteria_row", "")).lower():
            room["remark"] += " " + ucv_unit_note(room["ahu"])
            room["criteria"]["remark"] = (room["criteria"].get("remark") or "") + " " + ucv_unit_note(room["ahu"])
            gaps["ucv"] = ucv_unit_note(room["ahu"])
        rooms.append(room)

    # rooms with no unit: the room number printed on an air-side schematic box (for example 'ICU BED 05.02.06') names the unit of the room. A link needs the
    # number and a word of the room name in common; a number that two units print stays unlinked and is listed.
    sch_num: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for sr in _read(root / REG / "schematic_rooms.csv"):
        mm = re.search(r"(\d\d\.\d\d\.\d\d)", sr.get("room_name_as_written", ""))
        if mm and sr.get("ahu_tag"):
            sch_num[mm.group(1)].append(sr)
    sch_linked, sch_conflict, sch_nomatch = 0, [], 0
    stop = {"room", "bed", "bay", "the", "and", "of", "for", "no", "area"}
    # D-051: the unit of each room comes from the link table when it exists (the only interface between zoning and the workbooks); the linking above stays as the fallback
    links_on = r5.links_available(root)
    link_counts = r5.apply_unit_links(rooms, key, root, add_src, ahu_kinds) if links_on else {}
    if links_on:
        flags.append(dict(type="VERIFY", ref="Room Information", text="The unit of each room is taken from the link table of the project: " +
                          (", ".join(f"{n} by {b}" for b, n in sorted(link_counts.items(), key=lambda kv: -kv[1])) or "no room of this department is in the table") + ".", source=""))
    for room in ([] if links_on else rooms):
        if room["ahu"]:
            continue
        mm = re.search(r"(\d\d\.\d\d\.\d\d)", room["number"])
        cands = sch_num.get(mm.group(1), []) if mm else []
        if not cands:
            continue
        rw = {w for w in re.findall(r"[a-z]{3,}", room["name"].lower())} - stop
        cands = [c for c in cands if c.get("storey") == room["storey"]
                 and (rw & ({w for w in re.findall(r"[a-z]{3,}", c["room_name_as_written"].lower())} - stop)
                      or any(w[:3] in c["room_name_as_written"].lower() for w in rw if len(w) > 4))]
        if not cands:
            sch_nomatch += 1
            continue
        tags_ = sorted({t.strip() for c in cands for t in c["ahu_tag"].split(";") if t.strip() and not re.search(r"/\d+[A-Z]$", t.strip())})   # a standby unit '-03/04C' is not a zone
        zkeys = {zone_key(t) for t in tags_}
        if len(zkeys) > 1:
            sch_conflict.append((room["number"], tags_))
            continue
        if not tags_:
            continue
        tag = tags_[0]
        if len(tags_) > 1 and all(re.search(r"-\d{2}[AB]$", t) for t in tags_):
            tag = zone_key(tags_[0]) + "A/B"                  # the duty and standby pair is one zone
        c0 = sorted(cands, key=lambda c: (c["issue_set"] != "tender", c["sheet"]))[0]
        zsid = add_src(f"SCHROOM-{_norm(c0['sheet'])}-{_norm(room['number'])}"[:60], document=f"Air-side schematic {c0['sheet']}", issue=c0.get("issue", ""), page=f"page {c0.get('page', '')}",
                       location=f"room box '{c0['room_name_as_written']}'", text_as_read=f"{c0['room_name_as_written']} on unit {c0['ahu_tag']}",
                       method="Unit of the room read from the room box of the air-side schematic, linked by the room number", confidence=c0.get("confidence", "") or "medium")
        room["ahu"] = tag
        room["prov"]["zoning"] = ["D", zsid]
        room["remark"] = room["remark"].rstrip(".") + f". Unit {tag} from the room number on schematic {c0['sheet']}."
        room["gaps"].pop("ahu", None)
        ahu_kinds.setdefault(tag, "AHU")
        sch_linked += 1
    if sch_linked or sch_conflict:
        flags.append(dict(type="VERIFY", ref="Room Information", text=f"{sch_linked} room(s) take their unit from the room number printed on an air-side schematic box. "
                          f"{len(sch_conflict)} room number(s) are printed on boxes of different units and stay unlinked"
                          + (": " + "; ".join(f"{n} ({', '.join(u)})" for n, u in sch_conflict[:6]) if sch_conflict else "") + ".", source=""))
    n_noahu = sum(1 for r in rooms if not r["ahu"])
    flags.append(dict(type="VERIFY", ref="Room Information", text=f"{n_noahu} of {len(rooms)} room(s) have no unit: no plan room of the zone schedule and no schematic room number links "
                      "them to one. Their supply is not on any unit. The airflow of a unit is the sum of its linked rooms only.", source=""))
    # tender schematic room (comparison) and a pressure target from it where the booklet gives a direction only.
    # Link by unit, room name and typical count (brief 7.3 step 4): (1) the room names of the unit's schematic boxes; where a name has several boxes with the same
    # figures they are one typical room; (2) for a room with no unit, a schematic name of the same storey whose typical counts add up to the number of rooms of
    # this workbook with that name. A name that fits more than one set of figures stays unlinked and is listed.
    def _typ(s_):
        try:
            return max(1, int(float(s_.get("typical_count") or 1)))
        except ValueError:
            return 1

    def _fig(s_):
        return (s_.get("supply_L_s"), s_.get("return_L_s"), s_.get("exhaust_L_s"), s_.get("pressure"))

    def _link_schematic(room, s, how):
        ssid = add_src(f"TSCH-{s['sheet']}-{_norm(s['room_name_as_written'])}"[:60], document=f"Air-side schematic {s['sheet']}", issue=s.get("issue", "tender issue"),
                       page=f"page {s.get('page', '')}", location=s.get("box_page_view_table", ""), text_as_read=s.get("airflow_as_printed", ""),
                       method="Value read from the room table on the air-side schematic", confidence=s.get("confidence", ""))
        room["schematic"] = dict(supply_lps=_f(s["supply_L_s"]), return_lps=_f(s["return_L_s"]), exhaust_lps=_f(s["exhaust_L_s"]),
                                 pressure_pa=_pa(s.get("pressure")), source=ssid, code="D" if how.startswith("unit") else "A",
                                 source_text=f"tender air-side schematic {s['sheet']}, {s['room_name_as_written']} (linked by {how})")
        used_sr.add(id(s))
        c = room["criteria"]
        if c and _pa(s.get("pressure")) is not None and c.get("pressure_pa_booklet") is None and c.get("pressure_regime") in ("POSITIVE", "NEGATIVE"):
            c["pressure_pa_tender"] = _pa(s.get("pressure"))

    used_sr: set = set()
    sr_ambiguous: List[str] = []
    n_by_name: Dict[Tuple[str, str], int] = defaultdict(int)
    for room in rooms:
        n_by_name[(_norm(room["name"]), room["storey"])] += 1
    for room in rooms:
        if not room["ahu"]:
            continue
        tag_c = _tag_candidates(room["ahu"])
        rws = [s for s in sched_rooms if any(t in (s["ahu_tag"] or "") for t in tag_c) and _norm(s["room_name_as_written"]) == _norm(room["name"])
               and (not room["storey"] or s["storey"] == room["storey"])]
        if len(rws) == 1 or (len(rws) > 1 and len({_fig(x) for x in rws}) == 1):
            _link_schematic(room, rws[0], "unit, name and typical count")
            for x in rws[1:]:
                used_sr.add(id(x))
        elif len(rws) > 1:
            sr_ambiguous.append(f"{room['number']} ({len(rws)} boxes with different figures)")
    for room in rooms:
        if room.get("schematic"):
            continue
        rws = [s for s in sched_rooms if _norm(s["room_name_as_written"]) == _norm(room["name"]) and s["storey"] == room["storey"]]
        if rws and len({_fig(x) for x in rws}) == 1 and sum(_typ(x) for x in rws) == n_by_name[(_norm(room["name"]), room["storey"])]:
            _link_schematic(room, rws[0], "name and typical count")
        elif rws:
            sr_ambiguous.append(f"{room['number']} ({len(rws)} boxes, typical count {sum(_typ(x) for x in rws)} against {n_by_name[(_norm(room['name']), room['storey'])]} rooms)")
    num_rows: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for s_ in sched_rooms:
        mm_ = re.search(r"(\d\d\.\d\d\.\d\d)", s_.get("room_name_as_written", ""))
        if mm_:
            num_rows[mm_.group(1)].append(s_)
    for room in rooms:                 # (3) the room number printed in the box name, on the same storey, with a word of the room name in common
        if room.get("schematic"):
            continue
        mm_ = re.search(r"(\d\d\.\d\d\.\d\d)", room["number"])
        cands_ = [x for x in (num_rows.get(mm_.group(1), []) if mm_ else []) if x["storey"] == room["storey"]]
        rw_ = {w for w in re.findall(r"[a-z]{3,}", room["name"].lower())} - {"room", "bed", "bay", "the", "and", "for", "area"}
        cands_ = [x for x in cands_ if rw_ & ({w for w in re.findall(r"[a-z]{3,}", x["room_name_as_written"].lower())} - {"room", "bed", "bay", "the", "and", "for", "area"})
                  or any(w[:3] in x["room_name_as_written"].lower() for w in rw_ if len(w) > 4)]
        if cands_ and len({_fig(x) for x in cands_}) == 1:
            _link_schematic(room, cands_[0], "room number printed in the box")
    n_sch = sum(1 for r in rooms if r.get("schematic"))
    unit_tags = {r["ahu"] for r in rooms if r["ahu"]}
    unmatched_sr = [s for s in sched_rooms if id(s) not in used_sr and any(t in (s["ahu_tag"] or "") for u in unit_tags for t in _tag_candidates(u))]
    flags.append(dict(type="VERIFY", ref="Airflow Comparison", text=f"{n_sch} of {len(rooms)} room(s) are linked to a room box of the tender air-side schematics (by unit and name, "
                      "or by name and typical count). " + (f"{len(unmatched_sr)} room box(es) of the schematics on the units of this workbook match no room: "
                      + "; ".join(f"{x['room_name_as_written']} ({x['sheet']})" for x in unmatched_sr[:8]) + (" and others" if len(unmatched_sr) > 8 else "") + ". " if unmatched_sr else "") +
                      (f"{len(sr_ambiguous)} room(s) fit more than one set of figures or a different count and stay unlinked: " + "; ".join(sr_ambiguous[:6]) + ("." if len(sr_ambiguous) <= 6 else " and others.") if sr_ambiguous else ""), source=""))
    # D-028: every room with a pressure target in Pa is calculated on Pressure from its internal surface (2 x area + perimeter x height) at the airtightness
    # limit. No door data. The perimeter is the perimeter of the measured plan room where the room is linked to one, else 4 x square root of the area, assumed.
    n_perim_plan = n_perim_assumed = 0
    for room in rooms:
        c = room["criteria"]
        pa = None
        if c:
            pa = next((c[k] for k in ("pressure_pa_tender", "pressure_pa_working", "pressure_pa_booklet") if c.get(k) is not None), None)
        if pa is None or pa == 0:           # a neutral room (0 Pa) has no offset to calculate
            continue
        got = perim_of.get(room["number"])
        if got:
            room["pressure"] = dict(mode="controlled", suite=room["number"], perimeter_m=round(got[0], 3), prov=["M", got[1]], doors=[])
            n_perim_plan += 1
        else:
            room["pressure"] = dict(mode="controlled", suite=room["number"], perimeter_m=None, perimeter_rule="factor", prov=["A", "GEN-PRESSURE"], doors=[])
            room["gaps"]["perimeter"] = ("The perimeter for the internal surface is 4 × the square root of the area, assumed: the room is not linked to a measured plan room. "
                                         "The offset follows the area and the ceiling height. Confirm the room outline.")
            n_perim_assumed += 1
    # D-065: a room with a pressure direction and no value on any drawing is not calculated. The room type is listed once on Flags: directional airflow only, no offset.
    # Exhaust-driven rooms (toilets, change rooms, cleaners' rooms, dirty utilities) need no pressure row.
    exhaust_driven = re.compile(r"toilet|\bwc\b|change|cleaner|dirty|soiled|sluice|shower|ensuite|disposal|decontam", re.I)
    dir_only: Dict[str, List[str]] = defaultdict(list)
    for room in rooms:
        c_ = room["criteria"] or {}
        if c_.get("pressure_regime") in ("POSITIVE", "NEGATIVE") and all(c_.get(k) is None for k in ("pressure_pa_tender", "pressure_pa_working", "pressure_pa_booklet")) \
                and not exhaust_driven.search(room["name"]):
            dir_only[room["name"]].append(room["number"])
    for nm_, nums_ in sorted(dir_only.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        flags.append(dict(type="VERIFY", ref=nm_, text=f"Directional airflow only, no offset calculated: {len(nums_)} room(s) named '{nm_}' have a pressure direction and no pressure value on any "
                          f"drawing or in the HFDS. The offset air of these rooms is not in the AHU balance. First rooms: {', '.join(nums_[:4])}.", source=""))
    ahus: List[Dict[str, Any]] = []
    for tag in dict.fromkeys(r["ahu"] for r in rooms if r["ahu"]):
        if tag.startswith("AHU group:"):
            # a group of units (the room is printed on several units of one pod): one unit row, tender capacity = the sum of the units of the group
            members_ = [m_.strip() for m_ in tag.split(":", 1)[1].split(";") if m_.strip()]
            vals_ = [_tender_for_tag(m_, ahu_sched, fcu_sched, sched_equip, sched_totals, add_src, mot_sched, root=root) for m_ in members_]
            if vals_ and all(v_.get("sched_supply_lps") is not None for v_ in vals_):
                t = dict(sched_supply_lps=sum(v_["sched_supply_lps"] for v_ in vals_), sched_code="D", sched_source=vals_[0]["sched_source"],
                         tender_source_text=f"sum of the units of the group ({', '.join(members_)}), Equipment Schedule Corr 5; the common standby unit is not taken out", type="AHU")
            else:
                t = dict(tender_source_text=f"not every unit of the group ({', '.join(members_)}) has a tender airflow", type="AHU")
        else:
            t = _tender_for_tag(tag, ahu_sched, fcu_sched, sched_equip, sched_totals, add_src, mot_sched, root=root)
        a = dict(tag=tag, type=t.pop("type", ahu_kinds.get(tag, "AHU")))
        a.update(t)
        if unit_note(tag):
            a["note"] = unit_note(tag)
            if ucv_unit_note(tag):
                flags.append(dict(type="VERIFY", ref=tag, text=f"Unit {tag}: " + ucv_unit_note(tag), source=""))
        sc = _tender_for_tag(tag, None, None, None, None, add_src, schematic_only=True, root=root)
        if sc.get("sched_supply_lps") is not None:
            a.update(schematic_supply_lps=sc["sched_supply_lps"], schematic_source=sc["sched_source"], schematic_code="D")
        # D-060: ducted or hi-wall fan coil unit (Equipment Schedule, column Type)
        for cand_ in _tag_candidates(tag):
            fr_ = fcu_sched.get(_norm(cand_))
            if fr_ is not None and (fr_.get("Type") or "").strip():
                a["ducted"] = "N" if "wall" in fr_["Type"].lower() else "Y"
                break
        # D-054: the second room list = the room table of the tender air-side schematic of the unit
        sl_ = r5.second_room_list(_tag_candidates(tag), sched_rooms)
        if sl_ is not None:
            a["second_rooms"] = sl_[0]
            a["second_source"] = add_src(f"SECOND-{_norm(tag)}"[:60], document="Tender air-side schematics, room tables", issue="tender issue", page=f"sheets {sl_[1]}",
                                         location=f"unit {tag}", text_as_read=f"{sl_[0]} rooms (sum of the typical counts of the room boxes that carry the unit)",
                                         method="Count of the room boxes of the tender air-side schematic with the unit tag", confidence="medium")
        # D-055 / D-057: tender base test and MOT units
        mot_here = [r for r in rooms if r["ahu"] == tag and (r.get("_hepa") or str((r["prov"].get("zoning") or ["", ""])[1]).startswith("MOT-"))]
        is_mot_unit = bool(mot_here) or _norm(tag) in mot_sched
        sched_v = a.get("sched_supply_lps") if not str(a.get("sched_source", "")).startswith("TSCH") else None
        reason_, note_ = r5.tender_base_rule(a, sched_v, a.get("schematic_supply_lps"), root, is_mot_unit)
        if reason_:
            a["no_reduction_reason"] = reason_
        if note_ or any(r.get("_hepa") for r in mot_here) or (hepa is not None and tag in hepa.future):
            leak_ = float(((base.get("constants") or {}).get("DuctLeakage") or {}).get("value", 0.10))
            txt_ = []
            for r_ in mot_here:
                hs_ = r_.get("_hepa")
                if hs_:
                    duty_ = hs_["capability"] * (1 + leak_)
                    sch_ = a.get("sched_supply_lps")
                    txt_.append(f"Theatre {r_['number']}: design supply {hs_['design']:,.0f} L/s, capability {hs_['capability']:,.0f} L/s at {hepa.v_cap:g} m/s; unit duty "
                                f"{duty_:,.0f} L/s with {leak_ * 100:.0f} % duct leakage" + (f" against {sch_:,.0f} L/s on the schedule" + (". VERIFY" if duty_ > sch_ + 0.5 else "") if sch_ else "") + ".")
            if hepa is not None and tag in hepa.future:
                fut_ = hepa.future[tag]
                sch_ = a.get("sched_supply_lps")
                day1_ = sum(r_["_hepa"]["design"] for r_ in mot_here if r_.get("_hepa"))
                a["ucv_add_lps"] = round(max(fut_ - day1_, 0.0), 1)   # D-058: the future duty governs the unit; the extra supply is added on the AHU tab
                txt_.append(f"Two duties (D-058). Day-1 duty: theatre supply {day1_:,.0f} L/s at {hepa.v_design:g} m/s. Future ultra-clean duty: {fut_:,.0f} L/s to the room "
                            f"(IP-BH-16057 note; Tech Spec clause 1.17.11) × (1 + {leak_ * 100:.0f} % leakage). The future duty governs the unit, the coil and the duct capability check, "
                            f"and is the calculated supply of this unit. It is not resized"
                            + (f" and no reduction against the {sch_:,.0f} L/s on the schedule is stated" if sch_ else "")
                            + ". The check of 5,320 × 1.2 = 6,384 L/s is an inference of the design team.")
            if note_:
                txt_.append(note_ + ".")
            a["note"] = ((a.get("note") + " ") if a.get("note") else "") + " ".join(txt_)
        ahus.append(a)
        if a.get("sched_supply_lps") is None and any(str((r["prov"].get("zoning") or ["", ""])[1]).startswith("MOT-") for r in rooms if r["ahu"] == tag):
            flags.append(dict(type="VERIFY", ref=tag, text=f"Unit {tag} is named by the MOT NSC schedule of modes (Appendix J), for the rooms that carry it here. The MOT NSC "
                              "Overall AC Equipment Schedule does not list this tag, and the zone schedule does not show it. No tender airflow is available. Confirm the tag.", source=""))
    for r_ in rooms:
        r_.pop("_hepa", None)
    if str(dept).lstrip("0") == "22":
        flags.append(dict(type="VERIFY", ref="AHU-B-07-05MOTHYB-02", text="Tag variant, listed and not used (D-059). The MOT NSC schedule of modes (Appendix J, theatre MSOT 05.02.12) prints "
                          "AHU-B-07-05MOTHYB-02. The cross-reference workbook (row 26) gives AHU-B-07-05MOTHYB-01 for this theatre and for its preparation room MSOT 05.01.12. "
                          "The cross-reference cell 'AHU-B-07-05MOT-01 / MOTHYB-01' (row 14) is a variant: theatre MSOT 03.02.04 and its preparation room MSOT 03.01.04 are on AHU-B-07-05MOT-01.", source=""))
    ztot = zone_totals(root)
    for a in ahus:
        z = ztot.get(zone_key(a["tag"]))
        if z is not None:
            sid_z = add_src(f"ZUNIT-{_norm(zone_key(a['tag']))}"[:60], document="AHU zone schedule, LB3 to L07", issue="Rev A", page="storeys " + ", ".join(z["storeys"]),
                            location=f"unit {a['tag']}", text_as_read=f"{z['rooms']} zoned room(s), {z['area']:.1f} m² measured on the tender plan",
                            method="Rooms and area of the unit in the zone schedule", confidence="medium")
            a.update(zone_rooms=z["rooms"], zone_area_m2=round(z["area"], 1), zone_code="D", zone_source=sid_z)
    if with_others:
        oth = other_unit_rooms(key, root)
        osid = add_src("UNIT-SHARE", document="Room lists of the other department workbooks (the same assembly of every department)", issue="Rev A", page="all departments",
                       location="rooms with the unit of the AHU tab", text_as_read="number of rooms of the other departments that carry the unit, by unit tag",
                       method="Count of the rooms of the other departments on the unit", confidence="high")
        for a in ahus:
            a.update(other_rooms=oth.get(zone_key(a["tag"]), 0), other_rooms_source=osid)
    dept_fans, fan_flags = department_fans(root, [a["tag"] for a in ahus], rooms, add_src)
    flags.extend(fan_flags)
    for a in ahus:
        n_rooms = sum(1 for r in rooms if r["ahu"] == a["tag"])
        flags.append(dict(type="VERIFY", ref=a["tag"], text=f"AHU {a['tag']}: {n_rooms} room(s) of this workbook carry this AHU. The calculated airflow covers only these rooms. "
                          "The tender airflow covers the whole unit. The reduction is a unit result only when every department of the unit is counted. "
                          "The master AHU summary counts each unit once.", source=""))
    for p in un_plan:
        if id(p) in count_plan_ids:
            continue
        users = rule_users.get(id(p))
        flags.append(dict(type="VERIFY", ref=p["poly"]["room_id"], text=f"Plan room {p['poly'].get('room_tag', '')} ({_area_txt(p)}, storey {p['storey']}, "
                          f"department {p['zone'].get('department', '')}) matches no DPA room of this department one to one. It is not in the workbook."
                          + (f" Its AHU is used by the department rule for room(s) {', '.join(users)}." if users else ""), source=""))
    for u in rds_link["unlinked_rds"]:
        flags.append(dict(type="VERIFY", ref=u["code"] or u["name"], text=f"Room data sheet {u['code']} ({u['name']}) is linked to no DPA room of this workbook: {u['reason']}. "
                          f"Files: {', '.join(u['files'][:3])}{' and others' if len(u['files']) > 3 else ''}.", source=""))
    if rds_link["unlinked_dpa"]:
        flags.append(dict(type="VERIFY", ref="Room Information", text=f"{len(rds_link['unlinked_dpa'])} of {len(rooms)} DPA room(s) have no room data sheet link. "
                          "Each takes its occupancy from the default density. The reason for each room is in its Remarks.", source=""))
    for c in rls_link["conflicts"]:
        flags.append(dict(type="VERIFY", ref=c["number"], text=(height_conflict_text(c)[0].upper() + height_conflict_text(c)[1:] + ". The assumed height applies."), source="ASM-CEILING"))
    if not rls_rows:
        flags.append(dict(type="CONFIRM", ref="Room Information", text="No ceiling heights from the room layout sheets are available. Every room has the assumed height "
                          f"of {ch_default:g} m. Confirm the heights.", source="ASM-CEILING"))
    for d in out_of_scope:
        flags.append(dict(type="VERIFY", ref=d["number"], text=f"DPA room {d['number']} {d['name']} is on storey {d['storey']}, outside the IP storeys. It is not in the workbook.", source=""))
    for code in sorted(soa_units - set(ip_map) - {""}):
        if code in dpa_units:
            flags.append(dict(type="CONFIRM", ref=code, text=f"DPA unit {code} ({dpa_units[code][0]['fpu_department_name']}, {len(dpa_units[code])} rooms) is not in the IP block "
                              "of the stacking diagram. It is not in this workbook.", source=""))
    if not env_model.available:
        flags.append(dict(type="CONFIRM", ref="Envelope", text="Envelope rows are not set. Every room is treated as internal, with no glazing, wall or roof gain.", source="ASM-DEFAULT"))
    flags.append(dict(type="CONFIRM", ref="Room Information", text="Lighting and small power are ASHRAE values for the room use (red cells): no project document gives them. "
                      "Equipment gain is 0 W (a provisional value on equipment-led rooms) and operation time is not set: no document gives them. Confirm the values.", source="ASM-PLUG"))
    if not all(people.values()):
        flags.append(dict(type="CONFIRM", ref="README", text="Author, checker and approver of the revision block are not given. They are left blank.", source="ASM-DEFAULT"))
    storeys = sorted({r["storey"] for r in rooms}, key=lambda s: SCOPE_STOREYS.index(s) if s in SCOPE_STOREYS else 99)
    sp = list(base["space_types"]) + [dict(name=UNCLASSIFIED, room_t_c=base["space_types"][0]["room_t_c"], room_h_kjkg=base["space_types"][0]["room_h_kjkg"],
                                           code="A", source="ASM-DEFAULT")]
    crit_lines = [f"Criteria row '{t}' applied to {len(v)} room(s): {', '.join(v[:6])}{' and others' if len(v) > 6 else ''}." for t, v in sorted(crit_stats.items())]
    crit_lines.append(f"{len(unmapped_crit)} room(s) have no criteria row. They are listed on Flags." if unmapped_crit
                      else "Every room has a criteria row.")
    if unmapped_names:
        crit_lines.append("Room types with no criteria row, by name and number of rooms: " +
                          "; ".join(f"{n} ({len(v)})" for n, v in sorted(unmapped_names.items(), key=lambda kv: (-len(kv[1]), kv[0]))) + ". Confirm the booklet room type of each.")
        for n, v in sorted(unmapped_names.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            flags.append(dict(type="CONFIRM", ref=n, text=f"{len(v)} room(s) named '{n}' have no criteria row: no booklet room type is mapped to this room name. "
                              f"Their air-change and outdoor airflows are 0 L/s until the room type is confirmed. First rooms: {', '.join(v[:4])}.", source=""))
    use_lines = ["Occupancy comes from the RDS occupancy field.",
                 "The default density applies only where that field is empty or the room has no RDS. It is in persons per 100 m², rounded up to a whole person.",
                 "The room use comes from the room name. Each rule below gives the room kind and its density per 100 m²."] + \
                [f"{label}: {key.replace('_', ' ')} ({densities.get(key, 'n/a')})." for _, key, label in USE_RULES if key in densities]
    load_lines = ["Lighting and small power (plug load) come from the ASHRAE value of the room use, because no project document gives them. The room use comes from the room name, "
                  "as for the occupancy. Plug load / lighting in W/m² by room use: " +
                  "; ".join(f"{k.replace('_', ' ')} {plug_tab[k]:g} / {light_tab[k]:g}" for k in plug_tab if k in light_tab) + ". "
                  "Retail areas take the reception and waiting use. Process rooms (decontamination, washing, inspection, packing) take the workshop use. Locker rooms take the changing-room use. "
                  "A room name that does not say what the room is used for has no value and is listed on Flags."]
    first = lambda f: next((f(r) for r in rooms if f(r)), "")
    docs = [dict(document="DPA (departmental planning), room database", role="Rooms and designed area", issue="as printed", taken="Room list, designed area, storey",
                 source=first(lambda r: r["prov"]["dpa_area"][1])),
            dict(document="SOA (schedule of accommodation), governing issue", role="Area shown beside the DPA area", issue="as printed", taken="Area as drawn",
                 source=first(lambda r: r["prov"].get("soa_area", [0, ""])[1] if r.get("soa_area_m2") is not None else "")),
            dict(document="Room data sheets (RDS)", role="Occupancy", issue="as printed", taken="Occupancy field",
                 source=first(lambda r: r["prov"]["occupancy_people"][1] if r.get("occupancy_basis") == "RDS" else "")),
            dict(document="DD Booklet, criteria tables", role="Room criteria", issue="as printed", taken="Air changes, pressure, temperature, humidity, class, filter",
                 source=first(lambda r: r["criteria"].get("source") if r["criteria"] else ""))]
    data = dict(base)
    data.update(
        department=dict(number=key, name=dept_name),
        project=dict(base["project"], revision=dict(rev="A", date=conf.revision_date(root), description="DRAFT, acceptance open", **people)),
        scope=(f"Thermal load, airflow, outdoor air and pressure workbook for {key} {dept_name}, IP block (DPA units {', '.join(ip_map)}). "
               "Rooms are the DPA rooms. Rooms and plan rooms that do not match are listed on Flags."),
        storeys=storeys, storey_labels={s: _storey_label(s) for s in storeys}, space_types=sp, sources=sources, rooms=rooms, ahus=ahus,
        flags=flags, references=[], fans=dept_fans, terminals=[], documents=[x for x in docs if x["source"]],
        key_flags=[f["text"] for f in flags[-3:]], criteria_origin=["Room criteria: the criteria tables of the DD Booklet for this department."] + crit_lines + use_lines + load_lines)
    if env_model.available:
        esid = add_src("ETTV-ENV", document="ETTV report (20260129_AIGH_ETTV_Report.pdf)", issue="29 Jan 2026", page="pages 3, 9, 27 and 34 (facade data, wall and glazing U-values, shading coefficient, correction factor)",
                       location="Appendix 1 sheets, one record per facade sector N to NW",
                       text_as_read="wall U-value, glazing U-value, area-weighted shading coefficient, correction factor and window-to-wall ratio of each facade",
                       method="Gain per m² of wall = 12 × Uw; gain per m² of glass = 3.4 × Uf + 211 × CF × SC (ETTV equivalents, an averaged basis); areas by the simple facade method",
                       confidence="medium")
        data["sources"] = sources
        data["orientations"] = env_model.orientation_table(esid)
        flags.append(dict(type="CONFIRM", ref="Envelope", text=(f"Envelope gain is assessed for {env_rooms} external room(s) by the simple facade method: the length of the room edges on the storey outline "
                          "× storey height × the window-to-wall ratio of the ETTV report for the facade, with the U-value and shading coefficient of the report. The areas of the openings are not "
                          "read one by one from the building elevations. The grid bearing is taken as 359.77 degrees (page-up grid axis against grid north) and is not yet confirmed. Confirm the method, "
                          "the bearing and the areas."), source=esid))
    data["constants"] = dict(base["constants"])
    data["assemble_stats"] = dict(
        rooms=len(rooms), plan_rooms_of_department=len(plan), matched_to_plan=len(pairs), in_open_plan_region=len(region_of), unmatched_dpa_rooms=len(un_dpa) - len(region_of) - len(zlinks), unmatched_plan_rooms=len(un_plan),
        with_ahu=sum(1 for r in rooms if r["ahu"]), with_criteria=sum(1 for r in rooms if r["criteria"]), with_soa=sum(1 for r in rooms if r.get("soa_area_m2") is not None),
        with_rds=n_rds, occupancy_from_rds=n_rds_occ, occupancy_default_density=n_default, occupancy_not_set=n_nouse, ahus=len(ahus), fans=len(dept_fans),
        ahus_with_tender=sum(1 for a in ahus if a.get("sched_supply_lps") is not None), pressure_rooms=n_perim_plan + n_perim_assumed, pressure_perimeter_from_plan=n_perim_plan, pressure_perimeter_assumed=n_perim_assumed,
        units=list(ip_map.items()), unit_basis={c: i["basis"] for c, i in unit_info.items()}, units_excluded=res_units["excluded"],
        units_unresolved=res_units["unresolved"],
        plan_linked=len(pairs) + len(region_of) + len(zlinks), plan_pairs=len(pairs), plan_by_name_and_count=zl_count_count[0], plan_by_department_rule=zl_rule_count[0],
        rds_links_by_basis=dict(rds_basis_count), rds_unlinked_dpa=len(rds_link['unlinked_dpa']), rds_unlinked_rds=len(rds_link['unlinked_rds']),
        loads_ashrae=n_loads, loads_missing=n_loads_missing, criteria_presumed=n_presumed, criteria_own_mapped=n_own_mapped, criteria_presumed_by=dict(presumed_how),
        criteria_residue=len(unmapped_crit), criteria_residue_names={n: len(v) for n, v in unmapped_names.items()}, height_from_rls=n_rls, height_default=n_ch_default, height_conflicts=len(rls_link['conflicts']), rls_rows=len(rls_rows))
    audit: List[Dict[str, Any]] = []
    for d, room in zip(dpa_rows, rooms):
        rl_ = rds_link["links"].get(d["number"])
        un = next((u for u in rds_link["unlinked_dpa"] if u["number"] == d["number"]), None)
        hl_ = rls_link["heights"].get(d["number"])
        zl_ = zlinks.get(d["number"])
        plan_basis = ("one to one" if d["number"] in pairs else "open-plan region" if d["number"] in region_of else zl_["basis"] if zl_ else "no link: area not plausible" if d["number"] in link_rej_by else "none")
        audit.append(dict(side="DPA", dept=key, ref=d["number"], name=d["name"], code=d["code"], storey=d["storey"],
                          rds_basis=rl_["basis"] if rl_ else "none", rds_code=rl_["code"] if rl_ else "", rds_note=(un or {}).get("reason", "") or (rl_ or {}).get("code_note", ""),
                          height_basis=hl_["basis"] if hl_ else "default (flagged)", height_m=room.get("ceiling_height_m") or "",
                          plan_basis=plan_basis, ahu=room.get("ahu") or "", occupancy_basis=room.get("occupancy_basis", "")))
    for u in rds_link["unlinked_rds"]:
        audit.append(dict(side="RDS", dept=key, ref=u["code"], name=u["name"], rds_note=u["reason"]))
    for u in sorted(rls_link["unlinked_rls"], key=lambda x: (x["code"], x["number"], x["variant"])):
        audit.append(dict(side="RLS", dept=key, ref=f"{u['number']}_{u['code']}" + (f"_{u['variant']}" if u["variant"] else ""), name=f"{u['rows']} row(s)",
                          height_basis="no DPA room of this workbook links to it", height_m=u["height_m"] or ""))
    for p_ in un_plan:
        if id(p_) not in count_plan_ids:
            audit.append(dict(side="PLAN", dept=key, ref=p_["poly"]["room_id"], name=p_["name"], storey=p_["storey"], ahu=_class_of(p_),
                              plan_basis="no DPA room one to one" + ("; used by the department rule" if rule_users.get(id(p_)) else "")))
    for n_, u_ in sch_conflict:
        audit.append(dict(side="SCHEM", dept=key, ref=n_, name="room number printed on the box of more than one unit", ahu="; ".join(u_)))
    data["link_audit"] = audit
    if out is not None:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return data
