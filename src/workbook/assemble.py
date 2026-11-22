"""Build a department input JSON from the project data tables (out/registers/*.csv).

Rooms come from the DPA (designed area), limited to the units that the stacking diagram puts in the IP block. The SOA area is shown
beside the DPA area. Measured area, AHU and class come from the plan rooms (room_polygons and zone_assignment) where a plan room matches
a DPA room by name or abbreviation, department and storey. What does not match, on either side, is listed on Flags and left blank:
nothing is forced. Criteria rows are matched to the room type by name. Occupancy follows the RDS (D-016, D-018) and, where the RDS
field is empty or the room has no RDS, the default density by room use (D-017). Door data is not available: a room with a pressure
target is flagged and not calculated on Pressure.
"""

from __future__ import annotations

import csv
import difflib
import glob
import hashlib
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import conf
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
    (r"toilet|\bwc\b|shower|changing|change room|change cubicle|sanitary", "toilet", "toilets and changing rooms"),
    (r"plant|\bups\b|compressor|riser|shaft|\bahu\b|electrical|comms|server|\bpts\b|\bdb\b|substation|generator|switch ?room|battery|"
     r"transformer|rainwater|\btank\b|\bpump", "plant_room", "plant, electrical and communication rooms"),
    (r"cleaner|cleaning|janitor|linen|\bstore\b|storage|stock|refrigerat|archive|trolley|bulk|warehouse|\bstr\b|refuse|disposal|waste|"
     r"loading bay|deboxing|equipment store|pigeon|utility|\bholding\b", "storage", "stores, cleaners rooms and utility rooms"),
    (r"corridor|circulation|lobby|airlock|air lock|vestibule|lift|concourse|\bcorr\b|agv bay", "corridor_circulation", "corridors, lobbies and airlocks"),
    (r"theatre|operating", "operating_room", "operating theatres"),
    (r"recovery|\bicu\b|\bhdu\b|intensive", "recovery_icu", "recovery and intensive care"),
    (r"ward|bed room|1-bed|single bed|patient room|isolation room", "patient_room", "patient rooms"),
    (r"procedure|treatment|examination|exam room|consult|endoscop|scan|clinic", "medical_procedure", "procedure and consulting rooms"),
    (r"pantry|break|tea point|kitchenette|lounge|bev|staff accommodation|(?<!smart )locker|rest room|staff room", "break_room_pantry", "pantries, break rooms and staff rooms"),
    (r"meeting|conference|focus|interview|huddle|roll call", "conference_meeting", "meeting rooms"),
    (r"waiting|reception|counter|kiosk|dispensing|payment|self pick|service centre|registration", "reception_waiting", "reception, waiting and counters"),
    (r"office|\boff\b|workstation|write-up|writeup|admin|technician|workspace|desk|supervisor|control room|lactation|nurse station|workshop",
     "office", "offices, workstations and small private rooms"),
    (r"simulation|training|\btrg\b|classroom|seminar", "classroom_training", "training rooms"),
    (r"picking|repackag|automation|asrs|ipas|gown|ante ?room|extemporaneous|compounding|sterile prep|pharmacy|controlled drug|drug room|"
     r"preparation|\bprep\b|clean room", "pharmacy_prep", "pharmacy preparation, picking and automation rooms (pharmacy preparation area)"),
    (r"laborator|\blab\b", "laboratory", "laboratories"),
    (r"decontamination|washing|inspection|packing|steril|reprocess|endoscope|assembly|workstation zone",
     "process_area", "process rooms such as decontamination, washing, inspection and packing (light industrial, nearest listed use)"),
    (r"lecture|\blect\b", "lecture_classroom", "lecture rooms"),
    (r"\brtl\b|retail|shop|store front", "retail_sales", "retail areas"),
    (r"kitchen|cooking|pot wash", "kitchen_cooking", "kitchens"),
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
}


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
    t = (text or "").upper()
    if not t or t == "NA":
        return None
    if "-VE" in t or "NEGATIVE" in t or re.match(r"\s*-\s*\d", t):
        return "NEGATIVE"
    if "+VE" in t or "POSITIVE" in t or re.match(r"\s*\+\s*\d", t):
        return "POSITIVE"
    if "NEUTRAL" in t or t.strip() in ("0", "0 PA"):
        return "NEUTRAL"
    return None


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


def load_zone_assignments(root: Path) -> Dict[str, Dict[str, Dict[str, str]]]:
    """storey -> room_id -> row, for every zone_assignment_<storey>.csv that exists."""
    out: Dict[str, Dict[str, Dict[str, str]]] = {}
    for f in sorted(glob.glob(str(root / REG / "zone_assignment_*.csv"))):
        out[Path(f).stem.replace("zone_assignment_", "")] = {r["room_id"]: r for r in _read(Path(f)) if r.get("room_id")}
    return out


def _first(row: Dict[str, str], cols: Tuple[str, ...]) -> str:
    for c in cols:
        if row.get(c):
            return row[c].strip()
    return ""


def room_use(name: str) -> Tuple[Optional[str], str]:
    """(key of the density table, wording of the rule) for a room name; (None, '') when no rule matches."""
    low = (name or "").lower()
    for text in (low, _expand_abbreviations(low)):
        for rx, key, label in USE_RULES:
            if re.search(rx, text, re.I):
                return key, label
    return None, ""


def _expand_abbreviations(low: str) -> str:
    """Name with each Appendix D abbreviation spelled out (for the use mapping only)."""
    words = re.split(r"(\W+)", low)
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
    t = row.get("room_type_as_printed") or ""
    alts = [t] + [x for x in re.split(r"\s*/\s*|\s+-\s+|,\s*|\s+&\s+|\(|\)", t) if x.strip()]
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
    return crit


EXHAUST_ONLY = re.compile(r"toilet|\bwc\b|shower|ensuite|\bbath", re.I)


def apply_exhaust_only(crit: Dict[str, Any], type_text: str) -> None:
    """A toilet, shower or bath type at negative pressure is an exhaust-only room: the booklet total air changes are the exhaust, and
    transfer air makes up (the Rev A example takes its toilet this way: 20 ACH on 4.3 m² gives 80 L/s of exhaust and no supply)."""
    if crit and EXHAUST_ONLY.search(type_text or "") and crit.get("pressure_regime") == "NEGATIVE" and crit.get("min_total_ach"):
        crit["exhaust_ach"] = crit["min_total_ach"]
        crit["min_total_ach"] = None
        crit["remark"] = (crit.get("remark") or "") + ". Exhaust-only room: the booklet total air changes are the exhaust. Transfer air makes up the supply."


def match_criteria(room_name: str, rows: List[Dict[str, str]], modal: bool = False) -> Tuple[Optional[Dict[str, str]], List[Dict[str, str]], str]:
    """Best criteria row of the department for a room name: (row, other variants of the same room type, how matched or why not)."""
    rn = _norm(room_name)
    rw = set(_words(room_name))
    best: List[Tuple[float, Dict[str, str], str]] = []
    for c in rows:
        top, how = 0.0, ""
        for alt in _criteria_alternatives(c):
            an = _norm(alt)
            aw = set(_words(alt))
            if not an:
                continue
            if an == rn:
                sc, h = 3.0, "same name"
            elif aw and aw <= rw and len(aw) >= 2:
                sc, h = 2.0 + len(aw) / 10, "room type words found in the room name"
            elif aw and rw and rw <= aw and len(rw) >= 2:
                sc, h = 2.0 + len(rw) / 10, "room name words found in the room type"
            elif len(aw) == 1 and aw <= rw and len(an) >= 5:
                sc, h = 1.5, "one key word in common"
            else:
                r = difflib.SequenceMatcher(None, an, rn).ratio()
                sc, h = (1.0 + r, "similar name") if r >= 0.88 else (0.0, "")
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
    """Tags to look up for a zone tag: 'AHU-X-01A/B' is the duty and standby pair '...-01A' and '...-01B'."""
    t = (tag or "").strip()
    m = re.fullmatch(r"(.*?)([A-Z])((?:[/&][A-Z])+)", t)
    if m:
        letters = [m.group(2)] + re.findall(r"[A-Z]", m.group(3))
        return [m.group(1) + x for x in letters] + [t]
    return [t]


def _tender_for_tag(tag: str, sched: Dict[str, Dict[str, str]], fcu: Dict[str, Dict[str, str]], eq: List[Dict[str, str]],
                    totals: List[Dict[str, str]], add_src) -> Dict[str, Any]:
    """Tender airflow of one tag: Equipment Schedule (Corr 5) row by normalised tag, else the tender air-side schematic figure."""
    out: Dict[str, Any] = {}
    cands = _tag_candidates(tag)
    for c in cands:
        row = sched.get(_norm(c))
        if row:
            sup = _f(row.get("supply_airflow_l_s [Supply Airflow (l/s)]"))
            oa = _f(row.get("outdoor_air_min_l_s [Outdoor Airflow / Min (l/s)]"))
            if sup is not None:
                sid = f"EQS5-{_norm(c)}"[:60]
                page = row.get("page(s)", "")
                add_src(sid, document="Equipment Schedule, air handling unit schedule", issue="Corr 5", page=f"page {page}",
                        location=f"sheet {row.get('sheet_code', '')}, tag {row.get('tag_as_written', c)}",
                        text_as_read=f"{row.get('tag_as_written', c)}: supply airflow {sup:g} L/s; outdoor airflow minimum {oa if oa is not None else 'not printed'}",
                        method="Value read from the Equipment Schedule table", confidence="high")
                out.update(sched_supply_lps=sup, sched_oa_lps=oa, sched_source=sid, sched_code="D", type=row.get("type") or "AHU",
                           tender_source_text=f"Equipment Schedule Corr 5, page {page}, {row.get('tag_as_written', c)}")
                return out
        frow = fcu.get(_norm(c))
        if frow:
            sup = _f(frow.get("Min Airflow (l/s)"))
            if sup is not None:
                sid = f"EQS5-{_norm(c)}"[:60]
                add_src(sid, document="Equipment Schedule, fan coil unit schedule", issue="Corr 5", page=f"page {frow.get('page(s)', '')}",
                        location=f"sheet {frow.get('sheet_code', '')}, tag {frow.get('tag_as_written', c)}",
                        text_as_read=f"{frow.get('tag_as_written', c)}: minimum airflow {sup:g} L/s", method="Value read from the Equipment Schedule table",
                        confidence="high")
                out.update(sched_supply_lps=sup, sched_source=sid, sched_code="D", type="FCU",
                           tender_source_text=f"Equipment Schedule Corr 5, page {frow.get('page(s)', '')}, {frow.get('tag_as_written', c)}")
                return out
    for c in cands:
        for t in totals:
            if t.get("issue_set") == "tender" and t.get("ahu_tag") == c and _f(t.get("ahu_supply_L_s")) is not None:
                sid = f"TSCH-{_norm(c)}"[:60]
                add_src(sid, document=f"Air-side schematic {t.get('sheet', '')}", issue=t.get("issue", "tender issue"), page=t.get("sheet", ""),
                        location=f"AHU {c}", text_as_read=t.get("ahu_supply_as_written", ""), method="Value read from the text printed on the air-side schematic", confidence="medium")
                out.update(sched_supply_lps=_f(t["ahu_supply_L_s"]), sched_source=sid, sched_code="D",
                           tender_source_text=f"tender air-side schematic {t.get('sheet', '')}, {c}")
                return out
        for e in eq:
            if e.get("issue_set") == "tender" and e.get("tag") == c:
                m = re.match(r"\s*([\d,]+)\s*L/s", e.get("duty_as_written") or "")
                if m:
                    sid = f"TSCH-{_norm(c)}"[:60]
                    add_src(sid, document=f"Air-side schematic {e.get('sheet', '')}", issue=e.get("issue", "tender issue"), page=f"page {e.get('page', '')}",
                            location=f"AHU {c}", text_as_read=e["duty_as_written"], method="Value read from the text printed on the air-side schematic", confidence=e.get("confidence", ""))
                    out.update(sched_supply_lps=float(m.group(1).replace(",", "")), sched_source=sid, sched_code="D",
                               tender_source_text=f"tender air-side schematic {e.get('sheet', '')}, {c}")
                    return out
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


def assemble_department(dept: str, out: Optional[Path] = None, root: Path = Path(".")) -> Dict[str, Any]:
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
    ip_map = ip_units(key, stack, cand_units)
    if not ip_map:
        raise ValueError(f"department {key}: no DPA unit matches an IP-block entry of the stacking diagram")
    if soa_all:
        dept_name = re.sub(r"^\d+\s*-\s*", "", soa_all[0]["department_folder"])
    else:
        dept_name = next((r["department_as_written"] for r in stack if r.get("dept_no") and _dept_key(r["dept_no"]) == key and r["block"].startswith("IP")), key)
    base = build_reva_input(root)
    people = conf.people(root)
    densities = conf.occupancy_cfg(root).get("per_100m2") or {}
    criteria = [r for r in _read(root / REG / "criteria.csv") if r["dept_no"].lstrip("0") == key.lstrip("0")]
    polygons = load_polygons(root)
    zones = load_zone_assignments(root)
    rds_all = [r for r in _read(root / REG / "rds_rooms.csv") if r["dept_no"].lstrip("0") == key.lstrip("0")]
    sched_rooms = [r for r in _read(root / REG / "schematic_rooms.csv") if r.get("issue_set") == "tender"]
    sched_equip = _read(root / REG / "schematic_equipment.csv")
    sched_totals = _read(root / REG / "schematic_ahu_totals.csv")
    ahu_sched = {_norm(r["tag_normalised"]): r for r in _read(root / REG / "ahu_schedule.csv")}
    fcu_sched = {_norm(r["tag_normalised"]): r for r in _read(root / REG / "equipment_schedule_air_handling.csv") if r.get("type") == "FCU"}
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
            pick = next((g for g in group if (_f(g.get("designed_area_m2")) or 0) > 0), group[0])
            ident_storey = _storey_code(pick.get("storey_from_identifier") or "")
            soa_q = soa_by_code.get(ident)
            dpa_rows.append(dict(number=ident, name=pick["room_name"], storey=ident_storey or storey_stack, dpa_area=_f(pick.get("designed_area_m2")),
                                 code=(pick.get("soa_std_room_code") or "").strip(), typical_count=int(_f(soa_q.get("qty")) or 1) if soa_q else 1,
                                 ref_area=_f(pick.get("designed_area_m2")) or soa_area_of(ident),
                                 row=pick, unit=code, differ=len({_f(g.get("designed_area_m2")) for g in group}) > 1,
                                 storey_basis="identifier" if ident_storey else "stacking diagram"))
    dpa_rows.sort(key=lambda d: (SCOPE_STOREYS.index(d["storey"]) if d["storey"] in SCOPE_STOREYS else 99, d["number"]))
    out_of_scope = [d for d in dpa_rows if d["storey"] not in SCOPE_STOREYS]
    dpa_rows = [d for d in dpa_rows if d["storey"] in SCOPE_STOREYS]

    # plan rooms of the department, per storey
    first_word = re.sub(r"[^A-Za-z ]", "", dept_name).split()[0] if re.sub(r"[^A-Za-z ]", "", dept_name).split() else dept_name
    dept_re = re.compile(rf"\(0*{int(key)}\)|^{re.escape(first_word)}\b", re.I)
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
    zlinks = plan_links([d for d in dpa_rows if d["number"] not in pairs and d["number"] not in region_of], plan, set(pairs) | set(region_of),
                        lambda a, b: zoning_name_score(a, b, abbr), AHU_COLUMNS, SYSTEM_KINDS)
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
                               text_as_read=f"class {za.get('class', '')} ({za.get('class_kind', '')}); department {za.get('department', '')}; basis {za.get('method', '')}",
                               method="Zoning of the storey, as printed on the room tag of the plan", confidence=str(za.get("confidence", "")))
                room["prov"]["zoning"] = ["D", zsid]
                ahu_kinds.setdefault(tag, kind or "AHU")
            else:
                gaps["ahu"] = f"The plan room shows class '{za.get('class', '')}' and no system tag."
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
                if zl["tag"]:
                    room["ahu"] = zl["tag"]
                    room["zone_tag"] = _first(plans[0]["zone"], ZONE_COLUMNS) or None
                    room["system_type"] = _first(plans[0]["zone"], SYSTYPE_COLUMNS) or ("FCU" if zl["kind"] in ("FCU", "DXFCU") else None)
                    room["prov"]["zoning"] = [zcode, zsid]
                    ahu_kinds.setdefault(zl["tag"], zl["kind"] or "AHU")
                else:
                    gaps["ahu"] = f"The plan rooms of the same name show class '{zl['value']}' and no system tag."
            elif rj is None:
                notes_plan.append("No plan room matches this room by name, department and storey.")
            gaps["plan"] = " ".join(notes_plan)
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
            remark.append(f"ceiling height {hl['height_m']:.2f} m from the room layout sheet ({hl['basis']})")
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
        row, variants, how = match_criteria(d["name"], criteria)
        if row is not None:
            csid = add_src(f"CRIT-{key}-{_norm(row['room_type_as_printed'])}"[:60], document=_doc(row["source_file"]), issue=row.get("department_label", "booklet"),
                           page=f"page {row['booklet_page']}", location=row.get("bbox", ""), text_as_read=criteria_text(row),
                           method="Value read from the DD Booklet criteria table", confidence=str(row.get("confidence", "")))
            room["criteria"] = criteria_from_row(row, csid, how)
            crit_stats[row["room_type_as_printed"]].append(number)
            if variants:
                gaps["criteria"] = _variant_text(row, variants)
        else:
            unmapped_crit.append(number)
            gaps["criteria"] = "No booklet room type fits this room name" + (f" ({how})" if how else "") + "."
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
            remark.append("occupancy not set (the RDS issues disagree; 0 people is a placeholder)")
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
                if rl and rl["resolved"]["notes"]:
                    remark.append("RDS " + "; ".join(rl["resolved"]["notes"]))
            else:
                n_nouse += 1
                room["occupancy_basis"] = "NOT SET"
                remark.append("occupancy not set (0 people is a placeholder)")
                gaps["occupancy"] = ("Occupancy is not set. " + ("The RDS field is empty." if rds is not None else "No RDS row matches.") +
                                     " No room use fits the room name for the default density.")
        room["remark"] = ". ".join(_sentence_case(x.replace("; ", ". ")) for x in remark) + "."
        rooms.append(room)

    # tender schematic room (comparison) and a pressure target from it where the booklet gives a direction only
    for room in rooms:
        if not room["ahu"]:
            continue
        tag_c = _tag_candidates(room["ahu"])
        rws = [s for s in sched_rooms if any(t in (s["ahu_tag"] or "") for t in tag_c) and _norm(s["room_name_as_written"]) == _norm(room["name"])
               and (not room["storey"] or s["storey"] == room["storey"])]
        if len(rws) == 1:
            s = rws[0]
            ssid = add_src(f"TSCH-{s['sheet']}-{_norm(s['room_name_as_written'])}"[:60], document=f"Air-side schematic {s['sheet']}", issue=s.get("issue", "tender issue"),
                           page=f"page {s.get('page', '')}", location=s.get("box_page_view_table", ""), text_as_read=s.get("airflow_as_printed", ""),
                           method="Value read from the room table on the air-side schematic", confidence=s.get("confidence", ""))
            room["schematic"] = dict(supply_lps=_f(s["supply_L_s"]), return_lps=_f(s["return_L_s"]), exhaust_lps=_f(s["exhaust_L_s"]),
                                     pressure_pa=_pa(s.get("pressure")), source=ssid, code="D",
                                     source_text=f"tender air-side schematic {s['sheet']}, {s['room_name_as_written']}")
            c = room["criteria"]
            if c and _pa(s.get("pressure")) is not None and c.get("pressure_pa_booklet") is None and c.get("pressure_regime") in ("POSITIVE", "NEGATIVE"):
                c["pressure_pa_tender"] = _pa(s.get("pressure"))
    # rooms with a pressure target: no door data, so no Pressure block; flagged
    for room in rooms:
        c = room["criteria"]
        pa = None
        if c:
            pa = next((c[k] for k in ("pressure_pa_tender", "pressure_pa_working", "pressure_pa_booklet") if c.get(k) is not None), None)
        if pa is not None:
            room["gaps"]["door"] = f"pressure target {pa:g} Pa, but no document gives door data. It is not calculated on Pressure."
    ahus: List[Dict[str, Any]] = []
    for tag in dict.fromkeys(r["ahu"] for r in rooms if r["ahu"]):
        t = _tender_for_tag(tag, ahu_sched, fcu_sched, sched_equip, sched_totals, add_src)
        a = dict(tag=tag, type=t.pop("type", ahu_kinds.get(tag, "AHU")))
        a.update(t)
        sc = _tender_for_tag(tag, {}, {}, sched_equip, sched_totals, add_src)
        if sc.get("sched_supply_lps") is not None:
            a.update(schematic_supply_lps=sc["sched_supply_lps"], schematic_source=sc["sched_source"], schematic_code="D")
        ahus.append(a)
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
    flags.append(dict(type="CONFIRM", ref="Envelope", text="Envelope rows are not set. Every room is treated as internal, with no glazing, wall or roof gain.", source="ASM-DEFAULT"))
    flags.append(dict(type="CONFIRM", ref="Room Information", text="Lighting, small power, equipment and operation time are not set: no document gives them.", source="ASM-DEFAULT"))
    if not all(people.values()):
        flags.append(dict(type="CONFIRM", ref="README", text="Author, checker and approver of the revision block are not given. They are left blank.", source="ASM-DEFAULT"))
    storeys = sorted({r["storey"] for r in rooms}, key=lambda s: SCOPE_STOREYS.index(s) if s in SCOPE_STOREYS else 99)
    sp = list(base["space_types"]) + [dict(name=UNCLASSIFIED, room_t_c=base["space_types"][0]["room_t_c"], room_h_kjkg=base["space_types"][0]["room_h_kjkg"],
                                           code="A", source="ASM-DEFAULT")]
    crit_lines = [f"Criteria row '{t}' applied to {len(v)} room(s): {', '.join(v[:6])}{' and others' if len(v) > 6 else ''}." for t, v in sorted(crit_stats.items())]
    crit_lines.append(f"{len(unmapped_crit)} room(s) have no matching booklet room type. They are listed on Flags." if unmapped_crit
                      else "Every room has a matching booklet room type.")
    use_lines = ["Occupancy comes from the RDS occupancy field.",
                 "The default density applies only where that field is empty or the room has no RDS. It is in persons per 100 m², rounded up to a whole person.",
                 "The room use comes from the room name. Each rule below gives the room kind and its density per 100 m²."] + \
                [f"{label}: {key.replace('_', ' ')} ({densities.get(key, 'n/a')})." for _, key, label in USE_RULES if key in densities]
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
        project=dict(base["project"], revision=dict(rev="A", date=conf.revision_date(root), description="Issued for review", **people)),
        scope=(f"Thermal load, airflow, outdoor air and pressure workbook for {key} {dept_name}, IP block (DPA units {', '.join(ip_map)}). "
               "Rooms are the DPA rooms. Rooms and plan rooms that do not match are listed on Flags."),
        storeys=storeys, storey_labels={s: _storey_label(s) for s in storeys}, space_types=sp, sources=sources, rooms=rooms, ahus=ahus,
        flags=flags, references=[], fans=dept_fans, terminals=[], documents=[x for x in docs if x["source"]],
        key_flags=[f["text"] for f in flags[-3:]], criteria_origin=["Room criteria: the criteria tables of the DD Booklet for this department."] + crit_lines + use_lines)
    data["constants"] = dict(base["constants"])
    data["assemble_stats"] = dict(
        rooms=len(rooms), plan_rooms_of_department=len(plan), matched_to_plan=len(pairs), in_open_plan_region=len(region_of), unmatched_dpa_rooms=len(un_dpa) - len(region_of) - len(zlinks), unmatched_plan_rooms=len(un_plan),
        with_ahu=sum(1 for r in rooms if r["ahu"]), with_criteria=sum(1 for r in rooms if r["criteria"]), with_soa=sum(1 for r in rooms if r.get("soa_area_m2") is not None),
        with_rds=n_rds, occupancy_from_rds=n_rds_occ, occupancy_default_density=n_default, occupancy_not_set=n_nouse, ahus=len(ahus), fans=len(dept_fans),
        ahus_with_tender=sum(1 for a in ahus if a.get("sched_supply_lps") is not None), pressure_rooms_not_calculated=sum(1 for r in rooms if r["gaps"].get("door")),
        units=list(ip_map.items()),
        plan_linked=len(pairs) + len(region_of) + len(zlinks), plan_pairs=len(pairs), plan_by_name_and_count=zl_count_count[0], plan_by_department_rule=zl_rule_count[0],
        rds_links_by_basis=dict(rds_basis_count), rds_unlinked_dpa=len(rds_link['unlinked_dpa']), rds_unlinked_rds=len(rds_link['unlinked_rds']),
        height_from_rls=n_rls, height_default=n_ch_default, height_conflicts=len(rls_link['conflicts']), rls_rows=len(rls_rows))
    audit: List[Dict[str, Any]] = []
    for d, room in zip(dpa_rows, rooms):
        rl_ = rds_link["links"].get(d["number"])
        un = next((u for u in rds_link["unlinked_dpa"] if u["number"] == d["number"]), None)
        hl_ = rls_link["heights"].get(d["number"])
        zl_ = zlinks.get(d["number"])
        plan_basis = ("one to one" if d["number"] in pairs else "open-plan region" if d["number"] in region_of else zl_["basis"] if zl_ else "none")
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
    data["link_audit"] = audit
    if out is not None:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return data
