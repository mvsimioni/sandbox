"""Zoning evidence per room, for every storey (LB3 to L07).

``python3 -m src.zoning.evidence run --storeys LB3,B3,B2,B1,L01,L02,L03,L04,L05,L06,L07``

For each storey the command reads ``out/registers/room_polygons_<KEY>.csv`` and writes

* ``out/registers/zoning_evidence_<KEY>.csv``       one row per room and candidate (long form);
* ``out/registers/zoning_evidence_rooms_<KEY>.csv`` one row per room (counts, status, notes);
* ``out/registers/zoning_evidence_summary.csv``     one row per storey;
* ``out/registers/zoning_abbreviations.csv``        the abbreviation dictionary built from the data.

Evidence sources (the ``source`` column)

``department``  department of the room: (1a) the stacking-diagram wing that holds the room centre, (1b) rooms of that name in the
                DPA room database, limited to the departments the stacking diagram shows on the storey;
``appd``        the App D polygon that covers the room: equipment tags drawn in it and its mode label;
``schematic``   air-side schematic room tables of the same storey (tender, working and MOT NSC issues) that list a room name
                matching the plan tag; the schematic department (from its title) is compared with the room departments;
``booklet``     AHU-zoning keyplan of a DD booklet: a room label that matches, and the AHU tags printed on that keyplan;
``schedule``    Equipment Schedule (Corr 5) rows whose served level and description match the department or the area;
``rfi_sample``  the RFI-ME-0233 sample polygons (L04 only);
``mode``        the mode label printed on the plan tag (MV, NV, AC).

Scores run from 0 to 1.  They rank candidates; they are not probabilities.  A candidate is *strong* from ``STRONG`` upward.
The text of ``evidence`` and ``source_ref`` is plain English and cites documents by number, issue, sheet and page only,
because it can reach the issued PDF and schedule (decision D-015).
"""

from __future__ import annotations

import argparse
import csv
import itertools
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

REG = Path("out/registers")
ALL_STOREYS = ["LB3", "B3", "B2", "B1", "L01", "L02", "L03", "L04", "L05", "L06", "L07"]
STRONG = 0.55       # a candidate at or above this score is strong
MIN_KEEP = 0.12     # weaker candidates are not written
SCHEMATIC_MIN_Q = 0.4

COLUMNS = ["room_id", "storey", "room_tag", "room_name", "source", "candidate_kind", "candidate", "zone_key", "score",
           "strong", "evidence", "source_ref", "flags"]
ROOM_COLUMNS = ["room_id", "storey", "room_tag", "room_name", "mode", "area_m2", "wing", "wing_departments",
                "name_departments", "n_ahu_candidates", "n_ahu_strong", "n_fcu_candidates", "best_ahu", "best_ahu_score",
                "best_other", "status", "conflict_detail", "sources_present", "note"]


# --------------------------------------------------------------------------------------------- generic helpers

def read_csv(path) -> List[dict]:
    p = Path(path)
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def fnum(v, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def storey_stack_key(key: str) -> Optional[str]:
    """Storey label used by the stacking diagram: ``L04`` -> ``4``, ``B2`` -> ``B2``; LB3 is not on the diagram."""
    if re.fullmatch(r"L\d\d", key):
        return str(int(key[1:]))
    if key in ("B1", "B2", "B3"):
        return key
    return None


def storey_from_keyplan(title: str) -> List[str]:
    """Storey keys named by a booklet keyplan title (IP keyplans only)."""
    t = title.upper()
    if not t.startswith("IP"):
        return []
    out = []
    m = re.search(r"BASEMENT\s*(\d)", t)
    if m:
        out.append("B" + m.group(1))
    for n in re.findall(r"(\d+)\s*(?:ST|ND|RD|TH)\b", t):
        out.append("L%02d" % int(n))
    return out


def zone_key(tag: str) -> str:
    """Duty/standby pair is one zone: ``-01A/B``, ``-01A&B``, ``-01A`` and ``-01B`` all become ``-01``."""
    t = re.sub(r"\s+", "", tag.upper())
    t = re.sub(r"(?<=-\d\d)(?:A[/&]B|A|B)$", "", t)
    return t


def tag_kind(tag: str) -> str:
    m = re.match(r"([A-Z]+)-", tag.upper())
    return m.group(1) if m else ""


TAG_PATTERN = re.compile(r"^(AHU|PAHU|FCU|EAF|OAF|TEAF|RAF|SAF)-B-([A-Z0-9]{1,2})-([A-Z0-9]{2})([A-Z0-9]*)-(\d{2}[AB]?(?:[/&]B|/\d{2}[A-Z]?)?)$")


def tag_levels(tag: str, lookup: Dict[str, str]) -> str:
    """Served level code of a tag (``4``, ``B2``...), from the schematic and schedule data first, then the tag pattern."""
    k = zone_key(tag)
    if k in lookup:
        return lookup[k]
    m = re.match(r"^[A-Z]+-B-([A-Z0-9]{1,2})-(B?\d{1,2})", k)
    return m.group(2).lstrip("0") if m else ""


# --------------------------------------------------------------------------------------------- departments

DEPT_PATTERNS: List[Tuple[str, str]] = [
    ("ED", r"\bED\b|EMERGENCY|EDTU|\bCAC\b|DROP OFF"),
    ("ENDO", r"ENDOSCOP"),
    ("DI", r"DIAG|IMAGING|\bDI\b|CT SCAN|\bMRI\b|RADIOL|\bRAD\b|ANGIO"),
    ("CATH", r"\bCATH"),
    ("ICU", r"\bICU\b|INTENSIVE"),
    ("HDU", r"\bHDU\b|HIGH DEPENDENCY"),
    ("ASW", r"\bASW\b|AMBULATORY SURGICAL WARD|SLEEP STUD"),
    ("OT", r"\bMOT\b|MSOT|DSOT|MAJOR|OPERATING|THEATRE|\bASC\b|DOSA|DAY SURGERY|AMBULATORY SURGICAL CENTRE|PRE-?OP|\bPACU\b"),
    ("CSSD", r"CSSD"),
    ("LAB", r"LABORATOR|\bLAB\b"),
    ("FOOD", r"FOOD SERVICES|HOSPITALITY|PWLS|WASTE MANAGEMENT|\bFS\b"),
    ("RETAIL", r"FOOD ?COURT|RETAIL|COMMUNITY BOULEVARD|QUEENSWAY"),
    ("MM", r"MAT\.? ?MGMT|MATERIALS|\bMMD\b|LOADING DOCK"),
    ("MORT", r"MORTUARY"),
    ("PHARM", r"PHARM"),
    ("SERVER", r"SERVER|BATTERY|\bUPS\b"),
    ("CORE", r"\bCORE\b|COMMON|LOBBY|GENERAL SPACES|PUDO|CORRIDOR|IGH LOBBY"),
    ("MEP", r"\bMEP\b|PLANT AREA|MEDICAL GAS|AIR PLANT|ELECTRICAL"),
    ("TRAIN", r"TRAIN|AUDITORIUM|LEARNING"),
    ("ADMIN", r"ADMIN|\bAN\b|STAFF AREA|OFFICE"),
    ("CARPARK", r"CAR ?PARK"),
    ("LINEN", r"LINEN"),
    ("HS", r"\bHS\b|\bSSR\b|FACILITY MANAGEMENT|\bMRO\b|\bBME\b|GENERAL SERVICES"),
    ("ITD", r"\bITD\b|INNOVATION"),
    ("BRIDGE", r"BRIDGE"),
]
DEPT_LABELS = {
    "ED": "Emergency", "ENDO": "Endoscopy", "DI": "Diagnostic imaging", "CATH": "Cath lab", "ICU": "ICU", "HDU": "HDU",
    "ASW": "Ambulatory surgical ward", "OT": "Operating theatres and day surgery", "CSSD": "CSSD", "LAB": "Laboratory",
    "FOOD": "Food services", "RETAIL": "Food court and retail", "MM": "Materials management", "MORT": "Mortuary",
    "PHARM": "Pharmacy", "SERVER": "Primary server", "CORE": "Core and common spaces", "MEP": "MEP plant",
    "TRAIN": "Training and auditorium", "ADMIN": "Admin workspace", "CARPARK": "Car park", "LINEN": "Clean linen",
    "HS": "Health services support", "ITD": "ITD and innovation", "BRIDGE": "Bridge",
}
_DEPT_RE = [(k, re.compile(p)) for k, p in DEPT_PATTERNS]


def canon_depts(text: str) -> Set[str]:
    t = (text or "").upper()
    return {k for k, rx in _DEPT_RE if rx.search(t)}


def dept_text_from_schematic_file(path: str) -> str:
    base = Path(path).name
    m = re.search(r"AIR-SIDE SCHEMATIC\s*-?\s*(.*?)(?:\.pdf)?$", base, re.I)
    return (m.group(1) if m else base).strip()


# --------------------------------------------------------------------------------------------- names and abbreviations

SEED_ABBREVIATIONS: Dict[str, List[str]] = {
    "CORR": ["CORRIDOR", "CIRCULATION"],
    "STR": ["STORE"],
    "TRMT": ["TREATMENT"],
    "FLL": ["FIRE LIFT LOBBY"],
    "PLL": ["PASSENGER LIFT LOBBY"],
    "SFL": ["SERVICE LIFT LOBBY", "STAFF LIFT LOBBY"],
    "BLL": ["BED LIFT LOBBY"],
    "SLL": ["SERVICE LIFT LOBBY"],
    "DLL": ["DUMBWAITER LIFT LOBBY", "DIRTY LIFT LOBBY"],
    "TLL": ["TROLLEY LIFT LOBBY"],
    "CHG": ["CHANGE", "CHANGE ROOM"],
    "PT": ["PATIENT"],
    "OFF": ["OFFICE"],
    "MTG": ["MEETING", "MEETING ROOM"],
    "TRG": ["TRAINING", "TRAINING ROOM"],
    "LACT": ["LACTATION", "LACTATION ROOM"],
    "RECEPT": ["RECEPTION"],
    "CLN": ["CLEAN", "CLEANER", "CLEANERS ROOM"],
    "DU": ["DIRTY UTILITY"],
    "CU": ["CLEAN UTILITY"],
    "PROC": ["PROCEDURE", "PROCEDURE ROOM"],
    "CTRL": ["CONTROL", "CONTROL ROOM"],
    "EQPT": ["EQUIPMENT", "EQUIPMENT STORE"],
    "VDR": ["VENDOR"],
    "SVC": ["SERVICE"],
    "NURS": ["NURSE", "NURSING"],
    "CONSULT": ["CONSULT ROOM", "CONSULTATION"],
    "INT": ["INTERVIEW ROOM", "INTERVIEW"],
    "RM": ["ROOM"],
    "SS": ["STAFF STATION", "SATELLITE STATION", "SUPPLY STATION"],
    "ELEC": ["ELECTRICAL ROOM", "ELECTRICAL"],
    "ISO": ["ISOLATION"],
    "WAIT": ["WAITING"],
    "WSHP": ["WORKSHOP"],
    "WKSHP": ["WORKSHOP"],
    "SCRUB": ["SCRUB UP ROOM", "SCRUB"],
    "OPER": ["OPERATING ROOM", "OPERATING"],
    "AR": ["ANAESTHESIA ROOM", "ANAESTHESIA PREP"],
    "HW": ["HAND WASH"],
    "TERR": ["TERRACE"],
    "MED": ["MEDICATION", "MEDICAL"],
    "SEC": ["SECURITY"],
}
STOP = {"THE", "OF", "AND", "ROOM", "RM"}


def norm_text(s: str) -> str:
    s = (s or "").upper().replace("&", " AND ")
    s = re.sub(r"\bFUT\.?(URE)?\b", "FUTURE", s)
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokens(s: str) -> List[str]:
    return [t for t in norm_text(s).split() if t != "AND"]


def initials(words: Sequence[str]) -> str:
    return "".join(w[0] for w in words if w)


def plan_parts(room_name: str) -> List[Tuple[str, str]]:
    """Split a plan tag name into (part, note).  ``Chg | Toilet`` is two parts; ``Str (Future Vdr)`` keeps its future note."""
    out = []
    for part in re.split(r"\s*\|\s*|\s*,\s*", room_name or ""):
        part = part.strip()
        if not part:
            continue
        note = ""
        m = re.search(r"\(([^)]*)\)", part)
        if m:
            note = m.group(1).strip()
            part = re.sub(r"\([^)]*\)", " ", part).strip()
        if part:
            out.append((part, note))
    return out


def _subseq(t: str, w: str) -> bool:
    it = iter(w)
    return all(ch in it for ch in t)


class Abbreviations:
    """Abbreviation dictionary: seed entries plus entries derived from the schematic room names and the DPA room names."""

    def __init__(self):
        self.entries: Dict[str, Dict[str, str]] = defaultdict(dict)      # token -> expansion -> basis
        self.count: Counter = Counter()

    def add(self, tok: str, expansion: str, basis: str):
        if expansion and expansion not in self.entries[tok]:
            self.entries[tok][expansion] = basis

    def build(self, plan_names: Iterable[str], reference_phrases: Counter):
        for tok, exps in SEED_ABBREVIATIONS.items():
            for e in exps:
                self.add(tok, norm_text(e), "seed (project abbreviation list)")
        plan_tokens: Counter = Counter()
        for n in plan_names:
            for part, _ in plan_parts(n):
                for t in tokens(part):
                    plan_tokens[t] += 1
        words: Counter = Counter()
        multi: Dict[str, List[str]] = defaultdict(list)       # initials -> phrases
        for ph, c in reference_phrases.items():
            ws = ph.split()
            for w in ws:
                words[w] += c
            if 3 <= len(ws) <= 4:
                multi[initials(ws)].append(ph)
        for t, c in plan_tokens.items():
            self.count[t] = c
            if len(t) < 2 or t.isdigit() or re.fullmatch(r"[A-Z]\d+", t):
                continue
            if t not in words and len(t) >= 3:
                for w, wc in words.items():
                    if wc >= 2 and len(w) > len(t) + 1 and w[0] == t[0] and (w.startswith(t) or (_subseq(t, w) and len(w) <= len(t) + 4)):
                        self.add(t, w, "derived: word in the schematic or DPA room names")
            for ph in multi.get(t, []):
                self.add(t, ph, "derived: initials of a phrase in the schematic or DPA room names")

    def expand(self, tok: str) -> List[str]:
        return [tok] + list(self.entries.get(tok, {}).keys())

    def rows(self) -> List[dict]:
        rows = []
        for tok in sorted(self.entries):
            for e, b in self.entries[tok].items():
                rows.append({"abbreviation": tok, "expansion": e, "basis": b, "plan_tag_count": self.count.get(tok, 0)})
        return rows


def variants(part: str, abb: Abbreviations, cap: int = 40) -> List[str]:
    toks = [t for t in tokens(part) if t not in STOP]
    if not toks:
        toks = tokens(part)
    opts = [abb.expand(t) for t in toks]
    out = [" ".join(c) for c in itertools.islice(itertools.product(*opts), cap)]
    return list(dict.fromkeys(out))


def segments(name: str) -> List[str]:
    """A room table name can hold several names (``CIRCULATION / CORRIDOR / STAFF STATION``)."""
    segs = [name]
    if re.search(r"[/,]|\s-\s", name):
        segs += [s for s in re.split(r"\s*[/,]\s*|\s+-\s+", name) if s.strip()]
    return segs


def sim(variant: str, other: str) -> float:
    a = set(variant.split()) - STOP
    b = set(norm_text(other).split()) - STOP
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    j = len(a & b) / len(a | b)
    return round(0.8 * j, 3) if j >= 0.5 else 0.0


def best_q(vars_: Sequence[str], name: str) -> Tuple[float, str]:
    best, bseg = 0.0, ""
    for i, seg in enumerate(segments(name)):
        for v in vars_:
            q = sim(v, seg) * (0.9 if i > 0 else 1.0)
            if q > best:
                best, bseg = q, seg
    return round(best, 3), bseg


# --------------------------------------------------------------------------------------------- wings

class WingModel:
    """Stacking-diagram wings placed on the plan.

    Method: the plan footprint of the storey is the 1st to 99th percentile box of the room centres.  The footprint is split
    at its centre into four quadrants (plan north at the top of the sheet, grid numbers increasing to the right): NW, NE, SW, SE.
    A cell wing (``SW-E cell``, ``NE-W cell``) is the strip, 18 % of the footprint width, next to the centre line on the east
    side of the south half or the west side of the north half.  ``West`` is the strip 8 % of the width at the west edge.
    The diagram is a schematic and gives no grid ranges, so this placement is approximate; the L04 sample zones are used to
    check it (see the summary file).
    """

    def __init__(self, rooms: List[dict], stack_rows: List[dict]):
        xs = sorted(v for v in (fnum(r.get("centroid_x_mm")) for r in rooms) if v is not None)
        ys = sorted(v for v in (fnum(r.get("centroid_y_mm")) for r in rooms) if v is not None)
        self.ok = bool(xs and ys and stack_rows)
        self.by_wing: Dict[str, List[dict]] = defaultdict(list)
        if not self.ok:
            return

        def pct(a, p):
            return a[min(len(a) - 1, max(0, int(round(p * (len(a) - 1)))))]

        self.x0, self.x1 = pct(xs, 0.01), pct(xs, 0.99)
        self.y0, self.y1 = pct(ys, 0.01), pct(ys, 0.99)
        self.cx, self.cy = (self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2
        self.w = self.x1 - self.x0
        for r in stack_rows:
            if r["block"].startswith("IP"):
                self.by_wing[r["wing"]].append(r)

    def wing(self, x: Optional[float], y: Optional[float]) -> str:
        if not self.ok or x is None or y is None:
            return ""
        north, east = y > self.cy, x > self.cx
        if "West" in self.by_wing and x < self.x0 + 0.08 * self.w:
            return "West"
        if not north and east and "SW-E cell" in self.by_wing and x < self.cx + 0.18 * self.w:
            return "SW-E cell"
        if north and not east and "NE-W cell" in self.by_wing and x > self.cx - 0.18 * self.w:
            return "NE-W cell"
        return ("N" if north else "S") + ("E" if east else "W")

    def departments(self, wing: str) -> List[Tuple[str, str]]:
        return [(r["department_as_written"], r["dept_no"]) for r in self.by_wing.get(wing, [])]

    def storey_departments(self) -> Set[str]:
        out: Set[str] = set()
        for rows in self.by_wing.values():
            for r in rows:
                out |= canon_depts(r["department_as_written"])
        return out


# --------------------------------------------------------------------------------------------- reference data

class Reference:
    def __init__(self):
        self.schem = read_csv(REG / "schematic_rooms.csv")
        self.schem_eq = read_csv(REG / "schematic_equipment.csv")
        self.appd_spaces = read_csv(REG / "appd_spaces.csv")
        self.appd_eq = read_csv(REG / "appd_equipment.csv")
        self.booklet = read_csv(REG / "booklet_zoning_labels.csv")
        self.schedule = read_csv(REG / "ahu_schedule.csv")
        self.stacking = read_csv(REG / "stacking_diagram.csv")
        self.dpa = read_csv(REG / "dpa_rooms.csv")
        self.crossref = read_csv(REG / "tag_crossref_rfi_me_0233.csv")
        self.mot = read_csv(REG / "mot_theatre_rooms.csv")
        self.level: Dict[str, str] = {}
        for r in self.schedule:
            t = r.get("tag_normalised") or r.get("tag_as_written")
            if t and r.get("served_level_from_tag"):
                self.level[zone_key(t)] = r["served_level_from_tag"].lstrip("0") or "0"
        for r in self.schem_eq:
            if r.get("tag") and r.get("tag_served_code"):
                self.level.setdefault(zone_key(r["tag"]), r["tag_served_code"].lstrip("0"))
        self.phrases: Counter = Counter()
        for r in self.schem:
            n = norm_text(r["room_name_as_written"])
            if n:
                self.phrases[n] += 1
        self.dpa_by_name: Dict[str, Set[str]] = defaultdict(set)
        for r in self.dpa:
            n = norm_text(r["room_name"])
            if n:
                self.dpa_by_name[n] |= canon_depts(r["fpu_department_name"])
                self.phrases[n] += 1

    def stacking_for(self, key: str) -> List[dict]:
        sk = storey_stack_key(key)
        return [r for r in self.stacking if sk is not None and r["storey"] == sk]


# --------------------------------------------------------------------------------------------- evidence builders

class Cand:
    __slots__ = ("source", "kind", "candidate", "zone", "score", "evidence", "ref", "flags")

    def __init__(self, source, kind, candidate, zone, score, evidence, ref, flags=""):
        self.source, self.kind, self.candidate, self.zone = source, kind, candidate, zone
        self.score, self.evidence, self.ref, self.flags = round(min(score, 0.98), 3), evidence, ref, flags


def cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def dept_factor(dkeys: Set[str], wing_d: Set[str], name_d: Set[str], storey_d: Set[str]) -> Tuple[float, str]:
    if not dkeys:
        return 0.5, "the department of the document is not known"
    if dkeys & wing_d:
        return 1.0, "the department is shown in the wing of the room"
    if "CORE" in dkeys:
        return 0.8, "the document covers core and common spaces"
    if dkeys & name_d:
        return 0.8, "the room name is used in this department in the DPA room database"
    if dkeys & storey_d:
        return 0.6, "the department is on this storey but the wing is different or not known"
    return 0.35, "the department is not shown on this storey in the stacking diagram"


def issue_label(issue_set: str, rev: str) -> str:
    base = {"tender": "tender issue", "working": "working issue", "mot_nsc": "MOT NSC issue"}.get(issue_set, issue_set)
    return base + (f" Rev {rev}" if rev else "")


def level_check(tag: str, key: str, ref: Reference) -> str:
    """A tag that serves another storey is flagged (plant level is not served level)."""
    lv = tag_levels(tag, ref.level)
    if not lv or "-" in lv:
        return ""
    want = str(int(key[1:])) if re.fullmatch(r"L\d\d", key) else key
    return f"the tag serves level {lv}, not {want}" if lv != want else ""


class StoreyData:
    """Per-storey indexes built once from the reference data."""

    def __init__(self, key: str, ref: Reference):
        self.key = key
        sk = storey_stack_key(key)
        self.stack = ref.stacking_for(key)
        self.schem = [r for r in ref.schem if r["storey"] == key and r["room_name_as_written"].strip()]
        for r in self.schem:
            r["_dept"] = canon_depts(dept_text_from_schematic_file(r["drive_rel_path"]))
            r["_zones"] = [t.strip() for t in r["ahu_tag"].split(";") if t.strip()]
        self.schem_names: Dict[str, List[dict]] = defaultdict(list)
        for r in self.schem:
            self.schem_names[r["room_name_as_written"]].append(r)
        self.sheet_zones: Dict[Tuple[str, str, str], Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
        for r in self.schem:
            self.sheet_zones[(r["sheet"], norm_text(r["room_name_as_written"]), r["typical_count"])][r["issue_set"]] |= {zone_key(t) for t in r["_zones"]}
        self.mot = []
        for r in ref.mot:
            lv = r["level"].replace("L", "")
            if lv.isdigit() and "L%02d" % int(lv) == key and r["ahu_pahu_tag"].strip():
                self.mot.append(r)
        sn = sk if sk and sk.isdigit() else None
        self.appd_eq: Dict[str, List[dict]] = defaultdict(list)
        self.appd_sp: Dict[str, dict] = {}
        if sn:
            for r in ref.appd_eq:
                if r["storey"] == sn:
                    self.appd_eq[r["space_annotation_index"]].append(r)
            for r in ref.appd_spaces:
                if r["storey"] == sn:
                    self.appd_sp[r["annotation_index"]] = r
        self.keyplans: Dict[Tuple[str, str, str], dict] = {}
        for r in ref.booklet:
            if key in storey_from_keyplan(r["keyplan_title"]):
                k = (r["dept_no"], r["department_label"], r["keyplan_title"])
                g = self.keyplans.setdefault(k, {"labels": [], "tags": [], "dept": r["department_label"], "title": r["keyplan_title"]})
                if r["label_kind"] == "room_label":
                    g["labels"].append(r)
                elif r["label_kind"] == "equipment_tag":
                    g["tags"].append(r)
        for g in self.keyplans.values():
            g["dkeys"] = canon_depts(g["dept"])
        self.schedule = []
        for r in ref.schedule:
            if self._level_matches((r.get("served_level_from_tag") or "").strip()):
                r["_dept"] = canon_depts(r["served_area [Description]"])
                self.schedule.append(r)

    def _level_matches(self, lv: str) -> bool:
        if not lv:
            return False
        if self.key.startswith("L") and self.key != "LB3":
            n = int(self.key[1:])
            if re.fullmatch(r"\d\d", lv):
                return int(lv) == n
            m = re.fullmatch(r"(\d\d)-(\d\d)", lv)
            return bool(m and int(m.group(1)) <= n <= int(m.group(2)))
        return lv == self.key

    def unit_count(self, dkey: str) -> int:
        return len({zone_key(r["tag_normalised"] or r["tag_as_written"]) for r in self.schedule if dkey in r["_dept"]})


def evidence_for_room(room: dict, sd: StoreyData, wm: WingModel, abb: Abbreviations, ref: Reference,
                      sample_row: Optional[dict]) -> Tuple[List[Cand], dict]:
    cands: List[Cand] = []
    key = sd.key
    x, y = fnum(room.get("centroid_x_mm")), fnum(room.get("centroid_y_mm"))
    wing = wm.wing(x, y)
    wing_rows = wm.departments(wing) if wm.ok else []
    wing_d: Set[str] = set()
    for dn, _ in wing_rows:
        wing_d |= canon_depts(dn)
    storey_d = wm.storey_departments() if wm.ok else set()
    part_vars = [(p, n, variants(p, abb)) for p, n in plan_parts(room["room_name"])]

    # (1a) department from the stacking diagram wing
    for dn, dno in wing_rows:
        dk = canon_depts(dn)
        cands.append(Cand("department", "department", dn + (f" (dept {dno})" if dno else ""), ";".join(sorted(dk)), 0.4,
                          f"The stacking diagram shows {dn} in the {wing} wing of storey {key}. The wing is placed on the plan from the centre of the room. This placement is approximate.",
                          f"Stacking diagram, storey {storey_stack_key(key)}", "wing placement approximate"))
    # (1b) department from the room name (DPA room database)
    name_d: Set[str] = set()
    for p, n, vs in part_vars:
        found: Set[str] = set()
        for v in vs:
            found |= ref.dpa_by_name.get(v, set())
        found &= storey_d
        if found and len(found) <= 3:
            name_d |= found
            lab = ", ".join(DEPT_LABELS.get(d, d) for d in sorted(found))
            cands.append(Cand("department", "department", lab, ";".join(sorted(found)), 0.3,
                              f'The room name "{p}" is used for {lab} in the DPA room database. These departments are on storey {key}.',
                              "DPA room database", ""))

    # (2) App D
    ai = room.get("appd_annotation_index", "")
    ov_room = fnum(room.get("appd_overlap_room"))
    if ai != "" and ov_room:
        eqs = sd.appd_eq.get(ai, [])
        sp = sd.appd_sp.get(ai, {})
        mode = sp.get("mode_label", "")
        ref_txt = f"App D {sp.get('drawing_number', '')}, page {sp.get('page', '')}".strip(", ")
        share = f"The App D area covers {round(ov_room * 100)} % of the room."
        conf = fnum(room.get("appd_class_confidence"), 1.0) or 1.0
        by_type: Dict[str, List[str]] = defaultdict(list)
        for e in eqs:
            by_type[e["type"] or tag_kind(e["tag_as_written"])].append(e["tag_as_written"])
        has_unit = bool(by_type.get("AHU") or by_type.get("PAHU") or by_type.get("FCU"))
        for typ in ("AHU", "PAHU", "FCU"):
            for t in by_type.get(typ, []):
                cands.append(Cand("appd", "FCU" if typ == "FCU" else "AHU", t, zone_key(t), min(0.95, ov_room * conf),
                                  f"Equipment tag {t} is drawn in the App D area. {share} Mode label in App D: {mode or 'none'}.", ref_txt))
        fans = sorted({t for typ, tags in by_type.items() if typ not in ("AHU", "PAHU", "FCU") for t in tags})
        if fans:
            cands.append(Cand("appd", "MV", "; ".join(fans), "", min(0.9, ov_room * conf) if not has_unit else 0.3,
                              f"Fan tags drawn in the App D area: {', '.join(fans)}. {share} Mode label in App D: {mode or 'none'}.",
                              ref_txt, "fans serve the unit" if has_unit else "fans only"))
        if not eqs and room.get("appd_class_tag"):
            k = room.get("appd_class_kind", "")
            cands.append(Cand("appd", k if k in ("AHU", "FCU") else "MV", room["appd_class_tag"], zone_key(room["appd_class_tag"]),
                              min(0.95, ov_room * conf), f"{room.get('appd_class_basis', '')}. {share}", ref_txt))

    # (3) air-side schematics
    sch: Dict[str, Cand] = {}
    for p, note, vs in part_vars:
        best_by_zone: Dict[str, Tuple[float, str, dict, str, float, str, str]] = {}
        for name, rows in sd.schem_names.items():
            q, seg = best_q(vs, name)
            if q < SCHEMATIC_MIN_Q:
                continue
            for r in rows:
                df, why = dept_factor(r["_dept"], wing_d, name_d, storey_d)
                for t in r["_zones"]:
                    zk = zone_key(t)
                    s = q * df
                    old = best_by_zone.get(zk)
                    if old is None or s > old[0]:
                        best_by_zone[zk] = (s, t, r, name, q, why, seg)
        n = len(best_by_zone)
        for zk, (s, t, r, name, q, why, seg) in best_by_zone.items():
            sc = s / max(1, n)
            iz = sd.sheet_zones.get((r["sheet"], norm_text(name), r["typical_count"]), {})
            flags = []
            if iz.get("tender") and iz.get("working") and iz["tender"] != iz["working"]:
                flags.append("tender and working issues list different units for this room name")
            if len(r["_zones"]) > 1:
                flags.append(f"table names {len(r['_zones'])} units; it does not say which unit serves the room")
            lf = level_check(t, key, ref)
            if lf:
                flags.append(lf)
                sc *= 0.5
            ev = (f'Plan name "{p}" matches "{name}"' + (f' (part "{seg}")' if seg != name else "") +
                  f" in the room table of air-side schematic {r['sheet']} ({issue_label(r['issue_set'], r['rev'])}, page {r['page'] or '1'}). "
                  f"Name match {q:.2f}. {cap(why)}. {n} unit(s) match this name on the storey.")
            c = Cand("schematic", tag_kind(t) or "AHU", t, zk, sc, ev,
                     f"{r['sheet']} {issue_label(r['issue_set'], r['rev'])}, page {r['page'] or '1'}", "; ".join(flags))
            if zk not in sch or c.score > sch[zk].score:
                sch[zk] = c
    cands += list(sch.values())
    # MOT theatre table
    for p, note, vs in part_vars:
        for r in sd.mot:
            q, seg = best_q(vs, r["room_name_printed"])
            if q < 0.6:
                continue
            df, why = dept_factor(canon_depts(r["pod_or_suite"]), wing_d, name_d, storey_d)
            for t in re.split(r"\s*[;,|]\s*", r["ahu_pahu_tag"]):
                if t.startswith(("AHU", "PAHU")):
                    cands.append(Cand("schematic", "AHU", t, zone_key(t), q * df * 0.8,
                                      f'Plan name "{p}" matches "{r["room_name_printed"]}" in the MOT NSC theatre table ({r["page_or_sheet"]}). Name match {q:.2f}. {cap(why)}.',
                                      r["page_or_sheet"], "assignment inferred" if "inferred" in r["ahu_trace_status"] else ""))

    # (4) booklet AHU zoning keyplans
    for g in sd.keyplans.values():
        if not g["tags"]:
            continue
        hit: Optional[Tuple[float, dict, str]] = None
        for p, note, vs in part_vars:
            for lab in g["labels"]:
                q, _ = best_q(vs, lab["block_text"] or lab["text"])
                if q >= 0.8 and (hit is None or q > hit[0]):
                    hit = (q, lab, p)
        if hit is None:
            continue
        q, lab, p = hit
        df, why = dept_factor(g["dkeys"], wing_d, name_d, storey_d)
        tagset = [t for t in g["tags"] if t["pdf_page"] == lab["pdf_page"]] or g["tags"]
        uniq = {zone_key(t["text"]): t for t in tagset}
        for zk, t in uniq.items():
            sc = 0.5 * q * df / math.sqrt(len(uniq))
            fl = []
            if not TAG_PATTERN.match(t["text"].strip()) and not re.match(r"^[A-Z]+-B-B\d-", t["text"].strip()):
                fl.append("tag does not follow the tag pattern; kept as written")
            lf = level_check(t["text"], key, ref)
            if lf:
                fl.append(lf)
                sc *= 0.5
            cands.append(Cand("booklet", tag_kind(t["text"]) or "AHU", t["text"], zk, sc,
                              f'Plan name "{p}" matches the label "{lab["block_text"] or lab["text"]}" on the AHU zoning keyplan of {g["dept"]} ({g["title"]}). '
                              f"The keyplan prints {len(uniq)} AHU tag(s). The booklet does not link the tag to the label. {cap(why)}.",
                              f"{g['dept']} booklet, {g['title']}, page {lab['booklet_page'] or lab['pdf_page']}", "; ".join(fl)))

    # (5) equipment schedule (Corr 5)
    seen: Dict[str, Cand] = {}
    room_d = wing_d | name_d
    for r in sd.schedule:
        if r["type"] not in ("AHU", "PAHU"):
            continue
        t = r["tag_as_written"]
        zk = zone_key(r["tag_normalised"] or t)
        desc = r["served_area [Description]"]
        dmatch = r["_dept"] & room_d
        area_q, area_txt = 0.0, ""
        for grp in re.findall(r"\(([^)]*)\)", desc):
            for piece in re.split(r"\s*[,&]\s*|\s+and\s+", grp):
                for p, note, vs in part_vars:
                    q, _ = best_q(vs, piece)
                    if q > area_q:
                        area_q, area_txt = q, piece
        sc, ev = 0.0, ""
        if area_q >= 0.6 and (dmatch or not wing_d):
            sc = 0.5 * area_q
            ev = f'Plan name matches the area "{area_txt}" in the served-area text of Equipment Schedule Corr 5, sheet {r["sheet_code"]}, page {r["page(s)"]}: "{desc}".'
        elif dmatch:
            sc = 0.25 / math.sqrt(max(1, min(sd.unit_count(d) for d in dmatch)))
            ev = f'The served-area text "{desc}" in Equipment Schedule Corr 5, sheet {r["sheet_code"]}, page {r["page(s)"]}, names a department of the wing. The text does not list rooms.'
        if sc >= MIN_KEEP and (zk not in seen or sc > seen[zk].score):
            seen[zk] = Cand("schedule", "AHU", t, zk, sc, ev, f"Equipment Schedule Corr 5, sheet {r['sheet_code']}, page {r['page(s)']}", "")
    cands += list(seen.values())

    # (6) RFI-ME-0233 sample (L04)
    if sample_row is not None:
        m = re.search(r"covers (\d+) %", sample_row["sources"])
        share = int(m.group(1)) / 100 if m else (fnum(sample_row["confidence"], 0.5) or 0.5)
        t = sample_row["class"]
        gov = [c for c in ref.crossref if t == c["governing_tag_airside_schematic"] or t in c["equipment_schedule_corr5_tag"]]
        note = ""
        if gov and gov[0]["equipment_schedule_corr5_tag"] != gov[0]["governing_tag_airside_schematic"]:
            note = (f" The memo RFI-ME-0233 gives the tag {gov[0]['governing_tag_airside_schematic']}; "
                    f"Equipment Schedule Corr 5 gives {gov[0]['equipment_schedule_corr5_tag']}.")
        cands.append(Cand("rfi_sample", "AHU", t, zone_key(t), min(0.97, share),
                          f"The RFI-ME-0233 sample zone {t} covers {round(share * 100)} % of the room.{note}",
                          "RFI-ME-0233 sample zoning, page 1", "" if gov else "tag as written on the sample"))

    # (7) mode label
    mode = room.get("mode", "").strip()
    if mode in ("MV", "NV"):
        cands.append(Cand("mode", mode, mode, mode, 0.6, f"The plan tag prints the mode label {mode}.", f"Plan {key}, tag text"))
    elif mode.startswith("AC"):
        cands.append(Cand("mode", "AC", mode, "AC", 0.3, f"The plan tag prints the mode label {mode}. The label does not name the system.", f"Plan {key}, tag text"))
    else:
        cands.append(Cand("mode", "none", "", "", 0.0, "The plan tag prints no mode label.", f"Plan {key}, tag text", "no mode label"))

    cands = [c for c in cands if c.score >= MIN_KEEP or c.source in ("mode", "department")]
    return cands, {"wing": wing, "name_d": name_d}


# --------------------------------------------------------------------------------------------- room status

def classify(room: dict, cands: List[Cand]) -> dict:
    ahu = [c for c in cands if c.kind in ("AHU", "PAHU") and c.source != "department"]
    zones: Dict[str, float] = {}
    for c in ahu:
        zones[c.zone] = max(zones.get(c.zone, 0.0), c.score)
    strong = {z: s for z, s in zones.items() if s >= STRONG}
    fcu = {c.zone for c in cands if c.kind == "FCU"}
    mode = room.get("mode", "").strip()
    detail: List[str] = []
    if len(strong) > 1:
        detail.append("strong candidates for different units: " + "; ".join(sorted(strong)))
    appd_ahu = {c.zone for c in cands if c.source == "appd" and c.kind == "AHU" and c.score >= STRONG}
    appd_fcu = {c.zone for c in cands if c.source == "appd" and c.kind == "FCU" and c.score >= STRONG}
    if appd_ahu and appd_fcu:
        detail.append("App D shows both an AHU and an FCU")
    elif appd_fcu and strong:
        detail.append("App D shows an FCU and another source shows an AHU")
    if strong and mode in ("MV", "NV"):
        detail.append(f"the plan mode label is {mode} but a strong AHU candidate exists")
    if detail:
        status = "conflict"
    elif strong:
        status = "one strong candidate"
    elif zones or fcu:
        status = "weak candidates only"
    elif mode in ("MV", "NV"):
        status = "class from mode label only"
    else:
        status = "none"
    best = max(zones.items(), key=lambda kv: kv[1]) if zones else ("", 0.0)
    other = [c for c in cands if c.kind in ("FCU", "MV", "NV") and c.source != "mode"]
    return {"n_ahu_candidates": len(zones), "n_ahu_strong": len(strong), "n_fcu_candidates": len(fcu),
            "best_ahu": best[0], "best_ahu_score": round(best[1], 3),
            "best_other": "; ".join(sorted({f"{c.kind} {c.candidate}" for c in other}))[:200],
            "status": status, "conflict_detail": " | ".join(detail),
            "sources_present": ";".join(sorted({c.source for c in cands if c.score > 0}))}


# --------------------------------------------------------------------------------------------- run

def write_csv(path: Path, cols: List[str], rows: List[dict]):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def wing_vs_sample(key: str, room_rows: List[dict], za: Dict[str, dict]) -> str:
    """L04 only: does the wing placement agree with the department of the RFI sample zone of the room?"""
    if key != "L04":
        return ""
    ok = tot = 0
    for rr in room_rows:
        z = za.get(rr["room_id"])
        if not z or "RFI-ME-0233" not in z.get("method", "") or z["class_kind"] != "AHU":
            continue
        zd = canon_depts(z["department"] + " " + z["class"])
        if zd & {"DI", "CATH"}:
            zd |= {"DI", "CATH"}
        tot += 1
        ok += bool(zd & canon_depts(rr["wing_departments"]))
    return f"{ok} of {tot} sample-zoned rooms are in a wing whose stacking department matches the sample zone department"


def run_storey(key: str, ref: Reference, abb: Abbreviations) -> dict:
    rooms = read_csv(REG / f"room_polygons_{key}.csv")
    if not rooms:
        raise FileNotFoundError(f"out/registers/room_polygons_{key}.csv is missing or empty")
    sd = StoreyData(key, ref)
    wm = WingModel(rooms, sd.stack)
    za = {r["room_id"]: r for r in read_csv(REG / f"zone_assignment_{key}.csv")}
    out_rows, room_rows = [], []
    for room in rooms:
        zr = za.get(room["room_id"])
        sample = zr if (zr and key == "L04" and "RFI-ME-0233" in zr.get("method", "") and zr["class_kind"] == "AHU") else None
        cands, info = evidence_for_room(room, sd, wm, abb, ref, sample)
        cl = classify(room, cands)
        note = []
        if not sd.stack:
            note.append("the stacking diagram has no row for this storey")
        if not sd.schem and not sd.mot:
            note.append("no air-side schematic room table for this storey")
        if not room["room_name"].strip():
            note.append("plan tag has no room name")
        room_rows.append({"room_id": room["room_id"], "storey": key, "room_tag": room["room_tag"], "room_name": room["room_name"],
                          "mode": room["mode"], "area_m2": room["a_area_m2"], "wing": info["wing"],
                          "wing_departments": "; ".join(a for a, _ in wm.departments(info["wing"])) if wm.ok else "",
                          "name_departments": ";".join(sorted(info["name_d"])), "note": "; ".join(note), **cl})
        for c in sorted(cands, key=lambda c: (-c.score, c.source)):
            out_rows.append({"room_id": room["room_id"], "storey": key, "room_tag": room["room_tag"], "room_name": room["room_name"],
                             "source": c.source, "candidate_kind": c.kind, "candidate": c.candidate, "zone_key": c.zone,
                             "score": c.score, "strong": "yes" if c.score >= STRONG else "no", "evidence": c.evidence,
                             "source_ref": c.ref, "flags": c.flags})
    write_csv(REG / f"zoning_evidence_{key}.csv", COLUMNS, out_rows)
    write_csv(REG / f"zoning_evidence_rooms_{key}.csv", ROOM_COLUMNS, room_rows)
    st = Counter(r["status"] for r in room_rows)
    return {"storey": key, "rooms": len(room_rows),
            "rooms_with_ahu_candidate": sum(1 for r in room_rows if r["n_ahu_candidates"]),
            "rooms_with_strong_ahu_candidate": sum(1 for r in room_rows if r["n_ahu_strong"]),
            "rooms_conflicting": st["conflict"],
            "rooms_weak_only": st["weak candidates only"],
            "rooms_mode_label_only": st["class from mode label only"],
            "rooms_none": st["none"],
            "rooms_without_any_ahu_candidate": sum(1 for r in room_rows if not r["n_ahu_candidates"]),
            "schematic_rows_on_storey": len(sd.schem), "stacking_rows": len(sd.stack),
            "wing_check_vs_sample": wing_vs_sample(key, room_rows, za)}


def write_summary(rows: List[dict]):
    path = REG / "zoning_evidence_summary.csv"
    existing = {r["storey"]: r for r in read_csv(path)}
    for r in rows:
        existing[r["storey"]] = {k: str(v) for k, v in r.items()}
    cols = list(rows[0].keys())
    write_csv(path, cols, [{c: existing[k].get(c, "") for c in cols} for k in ALL_STOREYS if k in existing])


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Zoning evidence per room")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--storeys", default=",".join(ALL_STOREYS))
    args = ap.parse_args(argv)
    keys = [k.strip() for k in args.storeys.split(",") if k.strip()]
    ref = Reference()
    names: List[str] = []
    for k in ALL_STOREYS:
        names += [x["room_name"] for x in read_csv(REG / f"room_polygons_{k}.csv")]
    abb = Abbreviations()
    abb.build(names, ref.phrases)
    write_csv(REG / "zoning_abbreviations.csv", ["abbreviation", "expansion", "basis", "plan_tag_count"], abb.rows())
    results = []
    for k in keys:
        res = run_storey(k, ref, abb)
        results.append(res)
        print({c: v for c, v in res.items() if c != "wing_check_vs_sample"}, res["wing_check_vs_sample"])
    if results:
        write_summary(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
