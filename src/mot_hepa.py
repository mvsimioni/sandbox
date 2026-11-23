"""MOT NSC HEPA terminals, face-velocity requirements and operating-theatre supply airflow (D-038, D-039).

Reads, by script and from the PDF text layer only (no image reads):

* the MOT NSC HEPA Terminal Schedule SCH035 (latest corrigendum: Tender Corr 3, else Corr 2),
* the face-velocity clauses of "3. AH MOT NSC - Tech Specs for ACMV Services (Corri 3).pdf",

and writes

* out/registers/mot_hepa_terminals.csv   one row per scheduled terminal tag,
* out/registers/mot_face_velocity.csv    spec clauses and schedule velocities,
* out/registers/mot_ot_airflow.csv       per theatre: sum(face area x face velocity) in L/s, with the
                                         schematic and AHU-schedule airflows beside it.

Nothing is resolved here.  Where the schedule, the Tech Spec and the schematics disagree the row carries
both values and a ``flags`` text.  Unknown stays blank.

CLI:  python3 -m src.mot_hepa run
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:  # PyMuPDF
    import pymupdf as fitz  # type: ignore
except Exception:  # pragma: no cover
    import fitz  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = ROOT / "data" / "source"
MOT_DIR = SRC_ROOT / "Basis" / "MOT NSC"
REG = ROOT / "out" / "registers"

HEPA_SCHEDULE_NAME = "HEPA Terminal Schedule (MOT NSC)"
SPEC_NAME = "Tech Specs for ACMV Services"
# the Tech Spec copy is also expected in the Short Version DD Booklet folder; it is not there in this checkout
SPEC_BOOKLET_COPY = SRC_ROOT / "Short Version DD Booklet" / "3. AH MOT NSC - Tech Specs for ACMV Services (Corri 3).pdf"

TERMINAL_COLUMNS = [
    "terminal_tag", "location_as_printed", "level", "room_kind", "quantity", "quantity_basis",
    "module_size_as_printed", "width_mm", "height_mm", "face_area_m2", "face_area_basis",
    "filter_type_as_printed", "max_air_flow_l_s_as_printed", "max_face_velocity_m_s_as_printed",
    "dp_allowance_pa_as_printed", "duct_size_per_module_as_printed", "remarks_as_printed",
    "area_x_velocity_l_s", "area_x_velocity_vs_scheduled_pct",
    "source_doc", "source_page", "source_bbox_pt", "text_as_printed", "method", "confidence", "flags",
]

VELOCITY_COLUMNS = [
    "clause", "requirement_type", "text_as_printed", "velocity_min_m_s", "velocity_max_m_s", "velocity_m_s",
    "where_measured", "tolerance", "conditions", "applies_to", "airflow_l_s_printed",
    "source_doc", "pdf_page", "printed_page", "method", "confidence", "notes",
]

AIRFLOW_COLUMNS = [
    "theatre", "level", "pod", "room_kind", "terminals_used", "terminal_count", "module_sizes_as_printed",
    "face_area_m2_total", "face_velocity_used_m_s", "face_velocity_source",
    "supply_l_s_face_area_x_velocity", "ahu_l_s_with_leakage", "duct_leakage_pct",
    "supply_l_s_at_spec_0p25", "supply_l_s_at_spec_0p40", "supply_l_s_at_ashrae_0p127", "supply_l_s_at_ashrae_0p178",
    "scheduled_terminal_air_flow_l_s", "schematic_supply_norm_l_s", "schematic_supply_setback_l_s",
    "schematic_source", "ahu_tags_schematic", "ahu_schedule_supply_l_s", "ahu_schedule_source",
    "calc_vs_schematic_pct", "ahu_with_leakage_vs_ahu_schedule_pct",
    "other_suite_terminals", "status", "flags",
]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(SRC_ROOT.resolve()))
    except ValueError:
        return str(path)


def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.is_file():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, columns: List[str], rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="raise")
        w.writeheader()
        for r in rows:
            w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in columns})


def fnum(text: str) -> Optional[float]:
    try:
        return float(str(text).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def fmt(x: Optional[float], nd: int = 3) -> str:
    if x is None:
        return ""
    s = ("%.*f" % (nd, x)).rstrip("0").rstrip(".")
    return s or "0"


def corr_rank(path: str) -> int:
    m = re.search(r"Corr(?:i)?\s*(\d+)", path, re.I)
    return int(m.group(1)) if m else -1


def find_latest(name_fragment: str, mot_dir: Path = MOT_DIR, register: Optional[Path] = None) -> Optional[Path]:
    """Latest-corrigendum PDF whose file name contains ``name_fragment``: Corr 3 if present, else Corr 2, ...

    The document register (out/registers/mot_nsc_documents.csv) is consulted first (latest_issue == 'y'); the
    folder is scanned as a fallback so the module also works on a bare checkout.
    """
    register = register or REG / "mot_nsc_documents.csv"
    cands: List[Path] = []
    for r in read_csv(register):
        if name_fragment.lower() in r["rel_path"].lower() and r["rel_path"].lower().endswith(".pdf") and r.get("latest_issue") == "y":
            p = SRC_ROOT / r["rel_path"]
            if p.is_file():
                cands.append(p)
    if not cands:
        for p in mot_dir.rglob("*.pdf"):
            if name_fragment.lower() in p.name.lower():
                cands.append(p)
    if not cands:
        return None
    cands.sort(key=lambda p: corr_rank(str(p)))
    return cands[-1]


# --------------------------------------------------------------------------- #
# HEPA schedule
# --------------------------------------------------------------------------- #
# column bands (x centre, points) measured on the Corr 3 sheet header row
COLS = [
    ("ref", 0, 200), ("location", 200, 520), ("description", 520, 700), ("filter_type", 700, 780),
    ("max_air_flow", 780, 880), ("max_face_velocity", 880, 960), ("nominal_depth", 960, 1085),
    ("max_dp_initial", 1085, 1190), ("dp_allowance", 1190, 1280), ("duct_size", 1280, 1560), ("remarks", 1560, 99999),
]
SIZE_RE = re.compile(r"^(\d{2,4})\s*[xX×]\s*(\d{2,4})$")
TAG_RE = re.compile(r"^HEPA-[A-Za-z0-9]+-[A-Za-z0-9-]+$")


def _col(xc: float) -> str:
    for name, lo, hi in COLS:
        if lo <= xc < hi:
            return name
    return "remarks"


def parse_hepa_schedule(pdf_path: Path) -> List[Dict[str, Any]]:
    """One dict per terminal row on the schedule, from the text layer (word boxes), page and bbox kept."""
    out: List[Dict[str, Any]] = []
    doc = fitz.open(str(pdf_path))
    try:
        for pno in range(len(doc)):
            page = doc[pno]
            words = page.get_text("words")
            tags = [w for w in words if TAG_RE.match(w[4]) and w[0] < 600]
            for tw in sorted(tags, key=lambda w: w[1]):
                yc = (tw[1] + tw[3]) / 2.0
                row = [w for w in words if abs((w[1] + w[3]) / 2.0 - yc) < 6.0]
                row.sort(key=lambda w: w[0])
                cells: Dict[str, List[str]] = {}
                for w in row:
                    cells.setdefault(_col((w[0] + w[2]) / 2.0), []).append(w[4])
                j = {k: " ".join(v) for k, v in cells.items()}
                x0 = min(w[0] for w in row)
                y0 = min(w[1] for w in row)
                x1 = max(w[2] for w in row)
                y1 = max(w[3] for w in row)
                out.append({
                    "tag": j.get("ref", tw[4]), "location": j.get("location", ""), "size": j.get("description", ""),
                    "filter_type": j.get("filter_type", ""), "max_air_flow": j.get("max_air_flow", ""),
                    "max_face_velocity": j.get("max_face_velocity", ""), "nominal_depth": j.get("nominal_depth", ""),
                    "max_dp_initial": j.get("max_dp_initial", ""), "dp_allowance": j.get("dp_allowance", ""),
                    "duct_size": j.get("duct_size", ""), "remarks": j.get("remarks", ""),
                    "page": pno + 1, "bbox": [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)],
                    "text": " | ".join(" ".join(w[4] for w in row).split("  ")),
                })
    finally:
        doc.close()
    return out


def level_of(tag: str) -> str:
    m = re.match(r"HEPA-(L\d)-", tag)
    return m.group(1) if m else ""


def room_kind_of(tag: str, location: str) -> str:
    t, loc = tag.upper(), location.lower()
    if "operating room" in loc or "operation room" in loc:
        return "operating theatre"
    if "-CATH-STER" in t or "-ANGIO-STER" in t or "STER" in t or "sterile" in loc:
        return "sterile hold/prep"
    if "ante" in loc:
        return "ante room"
    if "cath lab" in loc or "angiography" in loc:
        return "procedure room (cath/angio)"
    return ""


def terminal_rows(parsed: List[Dict[str, Any]], source_rel: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    for p in parsed:
        seen[p["tag"]] = seen.get(p["tag"], 0) + 1
    for p in parsed:
        flags: List[str] = []
        m = SIZE_RE.match(p["size"].replace(" ", ""))
        w = float(m.group(1)) if m else None
        h = float(m.group(2)) if m else None
        area = (w * h / 1e6) if (w and h) else None
        vel = fnum(p["max_face_velocity"])
        flow = fnum(p["max_air_flow"])
        av = area * vel * 1000.0 if (area is not None and vel is not None) else None
        pct = ((av - flow) / flow * 100.0) if (av is not None and flow) else None
        if not m:
            flags.append("module size not read")
        if pct is not None and abs(pct) > 5.0:
            flags.append("face area x printed velocity = %s L/s differs from printed max air flow %s L/s by %s%%; "
                         "module count or size on the drawings decides" % (fmt(av, 0), fmt(flow, 0), fmt(pct, 0)))
        if "multiple" in p["remarks"].lower():
            flags.append("remark: apply multiple HEPA where schematic airflow exceeds schedule airflow; "
                         "drawing count not read, quantity shown is the schedule row")
        if "future ucv" in p["location"].lower() or "uvc" in p["location"].lower():
            flags.append("location text '%s' (UCV/UVC wording); Tech Spec 1.17.11 says UCV modules are not part of Day-1 works" % p["location"])
        if seen[p["tag"]] > 1:
            flags.append("tag appears %d times on the schedule" % seen[p["tag"]])
        rows.append({
            "terminal_tag": p["tag"], "location_as_printed": p["location"], "level": level_of(p["tag"]),
            "room_kind": room_kind_of(p["tag"], p["location"]),
            "quantity": 1, "quantity_basis": "one schedule row per tag; the schedule prints no quantity column",
            "module_size_as_printed": p["size"], "width_mm": fmt(w, 0), "height_mm": fmt(h, 0),
            "face_area_m2": fmt(area, 4), "face_area_basis": "calculated: printed module size width x height; the schedule prints no face area",
            "filter_type_as_printed": p["filter_type"], "max_air_flow_l_s_as_printed": p["max_air_flow"],
            "max_face_velocity_m_s_as_printed": p["max_face_velocity"], "dp_allowance_pa_as_printed": p["dp_allowance"],
            "duct_size_per_module_as_printed": p["duct_size"], "remarks_as_printed": p["remarks"],
            "area_x_velocity_l_s": fmt(av, 1), "area_x_velocity_vs_scheduled_pct": fmt(pct, 1),
            "source_doc": source_rel, "source_page": p["page"], "source_bbox_pt": str(p["bbox"]),
            "text_as_printed": p["text"], "method": "text layer (PyMuPDF words, row by y, column by x)",
            "confidence": "high" if m and vel is not None and flow is not None else "medium", "flags": "; ".join(flags),
        })
    return rows


# --------------------------------------------------------------------------- #
# Tech Spec clauses
# --------------------------------------------------------------------------- #
# (clause, requirement_type, start anchor, end anchor, vmin, vmax, v, where, tolerance, conditions, applies_to, airflow, notes)
CLAUSES: List[Dict[str, Any]] = [
    {"clause": "1.17.4 e) i", "type": "module face velocity, operating condition i",
     "start": r"i\.\s+capable to achieve design condition", "end": r"ii\.\s+As per ASHRAE",
     "vmin": 0.25, "vmax": 0.4, "where": "average velocity at module face", "tol": "",
     "cond": "Operating condition i of two; non-UCV and UCV are not distinguished in this clause",
     "applies": "OT HEPA modules (1.17.1: operating theatres, sterile hold and prep, cath labs, angiography)", "air": "",
     "notes": "Range, not a single value; the HEPA schedule prints one 'Max Face Velocity' per tag (0.35 for OT modules)."},
    {"clause": "1.17.4 e) ii", "type": "module face velocity, operating condition ii",
     "start": r"ii\.\s+As per ASHRAE standard 170", "end": r"iii\.\s+The Employer reserves",
     "vmin": 0.127, "vmax": 0.178, "where": "average velocity at module face (printed 'range of average velocity')", "tol": "",
     "cond": "Operating condition ii of two (ASHRAE 170 basis); the printed sub-clause numbering 'ii.' follows 'i.'", "applies": "OT HEPA modules",
     "air": "", "notes": "0.127-0.178 m/s is far below the schedule 0.35 m/s; selection is the Employer's (iii)."},
    {"clause": "1.17.4 e) iii", "type": "selection of operating condition", "start": r"iii\.\s+The Employer reserves", "end": r"1\.17\.5",
     "vmin": None, "vmax": None, "where": "", "tol": "", "cond": "Employer may select either condition or a combination across OT suites",
     "applies": "OT suites", "air": "", "notes": "Which condition governs which theatre is not stated."},
    {"clause": "1.17.3 a)", "type": "module size", "start": r"a\)\s+Standard OR \(L5 Pod1/Pod2", "end": r"b\)\s+TKR",
     "vmin": None, "vmax": None, "where": "", "tol": "", "cond": "standard OR", "applies": "L5 Pod1/Pod2, L6 all pods", "air": "",
     "notes": "Module 2800 x 2800 mm. Compare HEPA-L6-DSOT-09..12 (3200 x 3200 on the schedule, L6 Pod3)."},
    {"clause": "1.17.3 b)", "type": "module size", "start": r"b\)\s+TKR / Robotic / Hybrid OR", "end": r"c\)\s+Sterile Hold",
     "vmin": None, "vmax": None, "where": "", "tol": "", "cond": "TKR / Robotic / Hybrid", "applies": "L5 Pod3", "air": "",
     "notes": "Module 3200 x 3200 mm."},
    {"clause": "1.17.9 b)", "type": "uniformity of face velocity", "start": r"b\)\s+Airflow uniformity test across module face", "end": r"c\)\s+Particle count",
     "vmin": None, "vmax": None, "where": "across module face", "tol": "+/-15 % of mean velocity", "cond": "commissioning test, all HEPA modules",
     "applies": "OT HEPA modules", "air": "", "notes": ""},
    {"clause": "1.17.11 (Mode 2)", "type": "UCV future-provision airflow", "start": r"b\.\s+Mode 2 T&C", "end": r"i\.\s+Pressure cascade",
     "vmin": None, "vmax": None, "where": "", "tol": "", "cond": "UCV future-provision OTs (L5 MOT POD3 OR-CT/UCV, L6 DSOT POD3 OR-UCV): Day-1 conventional OT SAV; UCV terminal modules are not in the installed works",
     "applies": "L5 OR-CT/UCV, L6 DSOT POD3 OR-UCV", "air": "3,600",
     "notes": "No numeric UCV face velocity or UCV SAV is printed in 1.17.11; '3,600 L/s' is 'typically approximately' the conventional SAV per the airside schematic."},
    {"clause": "1.31.12 b. i (T&C)", "type": "velocity, specialist theatre entry", "start": r"Specialist Theatre: Average velocity", "end": r"Proprietary Unidirectional",
     "vmin": None, "vmax": None, "v": 0.4, "where": "average velocity of unidirectional airflow at entry to room", "tol": "+/-15 %",
     "cond": "Specialist Theatre (type not defined in the clause)", "applies": "specialist theatres", "air": "",
     "notes": "The line above it, 'Minimum Supply Air Volumes - All theatres:', prints no value."},
    {"clause": "1.31.12 b. i (T&C)", "type": "maximum air velocity at table", "start": r"Maximum Air Velocity: Measure downward air velocities", "end": r"control setpoint to this value",
     "vmin": None, "vmax": 0.25, "where": "downward velocity 100 mm above the operating table, under cooling conditions", "tol": "", "cond": "limit, not a supply design value",
     "applies": "all operating theatres", "air": "", "notes": "Sentence continues on the next printed page ('control setpoint to this value')."},
    {"clause": "1.13.4 (Filter Types table)", "type": "filter max face velocity", "start": r"Max Face Velocity", "end": r"Max Final",
     "vmin": None, "vmax": 2.5, "where": "filter media", "tol": "", "cond": "Types 1-7 print 2.5 m/s; the Type 8 (HEPA) column prints no value",
     "applies": "filter types 1-7", "air": "", "notes": "Seven values are printed against eight filter types; HEPA (type 8) is blank."},
    {"clause": "1.13.7 c) (BIBO)", "type": "filter max face velocity", "start": r"c\)\s+The face velocity across the HEPA filter media", "end": r"d\)\s+HEPA filter modules shall be of nominal",
     "vmin": None, "vmax": 2.0, "where": "HEPA filter media, design (clean-filter) airflow", "tol": "", "cond": "bag-in/bag-out containment housings only; not ceiling terminals",
     "applies": "BIBO housings", "air": "", "notes": "Not the OT ceiling-module face velocity."},
]


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def spec_pages(pdf_path: Path) -> List[Tuple[int, str, str]]:
    """(pdf page, text, printed page 'TS/ACMV/n (Corr 3)' or '') for every page."""
    out = []
    doc = fitz.open(str(pdf_path))
    try:
        for i in range(len(doc)):
            t = doc[i].get_text("text")
            m = re.search(r"TS/ACMV/(\d+)\s*\(Corr\s*\d+\)", t)
            out.append((i + 1, t, m.group(0) if m else ""))
    finally:
        doc.close()
    return out


def clause_rows(pdf_path: Path, source_rel: str) -> List[Dict[str, Any]]:
    pages = spec_pages(pdf_path)
    # skip the table of contents (first pages): clause anchors are searched from page 20
    rows: List[Dict[str, Any]] = []
    for c in CLAUSES:
        hit = None
        for pno, text, printed in pages:
            if pno < 20:
                continue
            flat = _squash(text)
            m = re.search(c["start"], flat)
            if not m:
                continue
            if c["clause"].startswith("1.13.4") and "Filter Types (MERV)" not in flat and "Max Face Velocity" not in flat:
                continue
            end = re.search(c["end"], flat[m.end():])
            seg = flat[m.start(): m.end() + (end.start() if end else 400)]
            hit = (pno, printed, seg[:700])
            break
        v = c.get("v")
        rows.append({
            "clause": c["clause"], "requirement_type": c["type"],
            "text_as_printed": hit[2] if hit else "", "velocity_min_m_s": fmt(c["vmin"]), "velocity_max_m_s": fmt(c["vmax"]),
            "velocity_m_s": fmt(v), "where_measured": c["where"], "tolerance": c["tol"], "conditions": c["cond"],
            "applies_to": c["applies"], "airflow_l_s_printed": c["air"], "source_doc": source_rel,
            "pdf_page": hit[0] if hit else "", "printed_page": hit[1] if hit else "",
            "method": "text layer (PyMuPDF page text, anchored on clause wording); numbers keyed from the printed text",
            "confidence": "high" if hit else "not found", "notes": c["notes"] if hit else "anchor text not found in the PDF; " + c["notes"],
        })
    return rows


def schedule_velocity_rows(terms: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for t in terms:
        v = t["max_face_velocity_m_s_as_printed"]
        out.append({
            "clause": "SCH035 " + t["terminal_tag"], "requirement_type": "scheduled max face velocity (per tag)",
            "text_as_printed": t["text_as_printed"], "velocity_min_m_s": "", "velocity_max_m_s": v, "velocity_m_s": "",
            "where_measured": "column 'Max Face Velocity (m/s)'; measured location not stated",
            "tolerance": "", "conditions": ("UCV wording in location text" if re.search(r"ucv|uvc", t["location_as_printed"], re.I) else "non-UCV wording"),
            "applies_to": t["location_as_printed"], "airflow_l_s_printed": t["max_air_flow_l_s_as_printed"],
            "source_doc": t["source_doc"], "pdf_page": t["source_page"], "printed_page": "",
            "method": t["method"], "confidence": t["confidence"], "notes": "",
        })
    return out


# --------------------------------------------------------------------------- #
# Theatre airflow
# --------------------------------------------------------------------------- #
def first_int(text: str) -> Optional[float]:
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*L/s", text or "")
    return fnum(m.group(1)) if m else None


def split_tags(s: str) -> List[str]:
    return [t.strip() for t in (s or "").split(",") if t.strip()]


def load_duct_leakage_pct() -> float:
    try:
        import yaml
        cfg = yaml.safe_load(open(ROOT / "config" / "project.yaml", encoding="utf-8"))
        return float(cfg["defaults_to_confirm"]["duct_leakage_pct"])
    except Exception:
        return 10.0


def pod_of(room: Dict[str, str]) -> str:
    return room.get("pod_or_suite", "")


def airflow_rows(terms: List[Dict[str, Any]], rooms: List[Dict[str, str]], ahu_sched: List[Dict[str, str]],
                 leak_pct: float) -> List[Dict[str, Any]]:
    by_tag = {t["terminal_tag"]: t for t in terms}
    sched_by_tag = {a["tag"]: a for a in ahu_sched}
    # schematic OT rows (room boxes) per HEPA tag
    sch_rows = [r for r in rooms if r["source_kind"] == "air-side schematic room box" and r["is_operating_theatre"] == "y"]
    sch_by_hepa: Dict[str, List[Dict[str, str]]] = {}
    for r in sch_rows:
        for t in split_tags(r["hepa_terminal_tags"]):
            sch_by_hepa.setdefault(t, []).append(r)
    rows: List[Dict[str, Any]] = []
    done: set = set()
    # one row per OT / procedure-room terminal on the schedule
    targets = [t for t in terms if t["room_kind"] in ("operating theatre", "procedure room (cath/angio)")]
    for t in targets:
        tag = t["terminal_tag"]
        done.add(tag)
        flags: List[str] = []
        area = fnum(t["face_area_m2"])
        vel = fnum(t["max_face_velocity_m_s_as_printed"])
        sup = area * vel * 1000.0 if area is not None and vel is not None else None
        ahu = sup * (1 + leak_pct / 100.0) if sup is not None else None
        sch = sch_by_hepa.get(tag, [])
        core = re.sub(r"^HEPA-L\d-", "", tag)
        narrowed = [r for r in sch if core in r["ahu_pahu_tag"]]
        sch = narrowed or sch
        norm = [first_int(r["sa_norm_l_s"]) for r in sch if first_int(r["sa_norm_l_s"]) is not None]
        setb = [first_int(r["sa_setback_l_s"]) for r in sch if first_int(r["sa_setback_l_s"]) is not None]
        sch_norm = max(norm) if norm else None
        sch_set = max(setb) if setb else None
        if len(set(norm)) > 1:
            flags.append("schematic room boxes give different supply values %s" % sorted(set(norm)))
        sch_tags: List[str] = []
        for r in sch:
            for k in (r.get("ahu_pahu_tag", ""),):
                for x in (y.strip() for y in re.split(r"[,;|]", k)):
                    if x and core in x and x not in sch_tags:
                        sch_tags.append(x)
        sched_vals = []
        for a in sch_tags:
            s = sched_by_tag.get(a)
            if s and fnum(s["supply_airflow_l_s"]) is not None:
                sched_vals.append((a, fnum(s["supply_airflow_l_s"])))
        ahu_sched = sum(v for _, v in sched_vals) if sched_vals else None
        sched_src = "; ".join("%s %s L/s" % (a, fmt(v, 0)) for a, v in sched_vals)
        if sched_vals:
            sched_src += " (out/registers/mot_ahu_schedule.csv, MOT AC schedule Corr 2)"
        if not sch:
            flags.append("no schematic OT room box carries this terminal tag; schematic airflow blank")
        if sup is None:
            flags.append("face area or velocity not read; supply not calculated")
        pct = ((sup - sch_norm) / sch_norm * 100.0) if (sup is not None and sch_norm) else None
        apct = ((ahu - ahu_sched) / ahu_sched * 100.0) if (ahu is not None and ahu_sched) else None
        sched_flow = fnum(t["max_air_flow_l_s_as_printed"])
        if t["flags"]:
            flags.append(t["flags"])
        if re.search(r"ucv|uvc", t["location_as_printed"], re.I):
            flags.append("UCV/UVC wording: Tech Spec 1.17.11 says the UCV terminal is not in the Day-1 works; supply shown is the scheduled module, UCV/non-UCV basis undecided")
        rows.append({
            "theatre": t["location_as_printed"], "level": t["level"], "pod": _pod(t, sch), "room_kind": t["room_kind"],
            "terminals_used": tag, "terminal_count": 1, "module_sizes_as_printed": t["module_size_as_printed"],
            "face_area_m2_total": fmt(area, 4), "face_velocity_used_m_s": fmt(vel), "face_velocity_source": "SCH035 'Max Face Velocity' for the tag (inside Tech Spec 1.17.4 e) i range 0.25-0.4 for OT modules)" if t["room_kind"] == "operating theatre" else "SCH035 'Max Face Velocity' for the tag",
            "supply_l_s_face_area_x_velocity": fmt(sup, 0), "ahu_l_s_with_leakage": fmt(ahu, 0), "duct_leakage_pct": fmt(leak_pct, 1),
            "supply_l_s_at_spec_0p25": fmt(area * 250.0 if area is not None else None, 0),
            "supply_l_s_at_spec_0p40": fmt(area * 400.0 if area is not None else None, 0),
            "supply_l_s_at_ashrae_0p127": fmt(area * 127.0 if area is not None else None, 0),
            "supply_l_s_at_ashrae_0p178": fmt(area * 178.0 if area is not None else None, 0),
            "scheduled_terminal_air_flow_l_s": fmt(sched_flow, 0),
            "schematic_supply_norm_l_s": fmt(sch_norm, 0), "schematic_supply_setback_l_s": fmt(sch_set, 0),
            "schematic_source": "; ".join(sorted({"%s %s (%s)" % (r["source_file"].split("/")[-1][:70], r["page_or_sheet"], r["box_or_row"][:60]) for r in sch})) if sch else "",
            "ahu_tags_schematic": "; ".join(sch_tags), "ahu_schedule_supply_l_s": fmt(ahu_sched, 0), "ahu_schedule_source": sched_src,
            "calc_vs_schematic_pct": fmt(pct, 1), "ahu_with_leakage_vs_ahu_schedule_pct": fmt(apct, 1),
            "other_suite_terminals": _suite_others(t, terms), "status": "calculated" if sup is not None else "terminals or velocity not found",
            "flags": "; ".join(f for f in flags if f),
        })
    # schematic OT rooms whose HEPA tag is not on the schedule, or that have no tag at all
    seen_rooms: set = set()
    for r in sch_rows:
        tags = split_tags(r["hepa_terminal_tags"])
        key = (r["room_name_printed"], r["level"], r["pod_or_suite"], r["ahu_pahu_tag"])
        if key in seen_rooms:
            continue
        seen_rooms.add(key)
        if not tags:
            rows.append(_missing_row(r, "no HEPA terminal tag on the schematic room box"))
        else:
            miss = [t for t in tags if t not in by_tag]
            if miss:
                rows.append(_missing_row(r, "HEPA tag(s) %s not on the HEPA schedule (latest corrigendum)" % ", ".join(miss)))
    # OT-type theatres in the Appendix J rows without HEPA tag
    for r in rooms:
        if r["source_kind"] == "Appendix J schedule of modes row" and r["is_operating_theatre"] == "y" and not split_tags(r["hepa_terminal_tags"]):
            rows.append(_missing_row(r, "Appendix J row for an OT carries no HEPA terminal tag (tag as printed: %s)" % r["ahu_pahu_tag"]))
    return rows


def _pod(t: Dict[str, Any], sch: List[Dict[str, str]]) -> str:
    for r in sch:
        if r.get("pod_or_suite"):
            return r["pod_or_suite"]
    m = re.search(r"\((L\d) (Pod\d)\)", t["location_as_printed"])
    return m.group(2) if m else ""


def _suite_others(t: Dict[str, Any], terms: List[Dict[str, Any]]) -> str:
    """Other HEPA terminals of the same suite (ISO NP: ante, staff, sterile), not counted in the theatre supply."""
    tag = t["terminal_tag"]
    m = re.match(r"(HEPA-L\d-MOTlSONP)-(\d\d)$", tag)
    if not m:
        return ""
    others = [x["terminal_tag"] for x in terms if x["terminal_tag"].startswith(m.group(1)) and x["terminal_tag"].endswith("-" + m.group(2)) and x["terminal_tag"] != tag]
    return "; ".join(others)


def _missing_row(r: Dict[str, str], why: str) -> Dict[str, Any]:
    row = {c: "" for c in AIRFLOW_COLUMNS}
    row.update({
        "theatre": r["room_name_printed"], "level": r["level"], "pod": r["pod_or_suite"], "room_kind": "operating theatre",
        "ahu_tags_schematic": r["ahu_pahu_tag"], "schematic_supply_norm_l_s": fmt(first_int(r["sa_norm_l_s"]), 0),
        "schematic_source": "%s %s" % (r["source_file"].split("/")[-1][:70], r["page_or_sheet"]),
        "status": "terminals or velocity not found", "flags": why,
    })
    return row


def run(out_dir: Optional[Path] = None) -> Dict[str, Any]:
    reg = out_dir or REG
    sched_pdf = find_latest(HEPA_SCHEDULE_NAME)
    spec_pdf = find_latest(SPEC_NAME)
    if sched_pdf is None:
        raise SystemExit("MOT HEPA terminal schedule not found under %s" % MOT_DIR)
    parsed = parse_hepa_schedule(sched_pdf)
    terms = terminal_rows(parsed, rel(sched_pdf))
    write_csv(reg / "mot_hepa_terminals.csv", TERMINAL_COLUMNS, terms)
    vel: List[Dict[str, Any]] = []
    if spec_pdf is not None:
        vel.extend(clause_rows(spec_pdf, rel(spec_pdf)))
    vel.extend(schedule_velocity_rows(terms))
    write_csv(reg / "mot_face_velocity.csv", VELOCITY_COLUMNS, vel)
    rooms = read_csv(REG / "mot_theatre_rooms.csv")
    ahu = read_csv(REG / "mot_ahu_schedule.csv")
    leak = load_duct_leakage_pct()
    air = airflow_rows(terms, rooms, ahu, leak)
    write_csv(reg / "mot_ot_airflow.csv", AIRFLOW_COLUMNS, air)
    return {"schedule": rel(sched_pdf), "spec": rel(spec_pdf) if spec_pdf else None, "terminals": len(terms),
            "velocity_rows": len(vel), "airflow_rows": len(air),
            "not_found": [r["theatre"] for r in air if r["status"] != "calculated"],
            "booklet_copy_of_spec_present": SPEC_BOOKLET_COPY.is_file()}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="parse the HEPA schedule and Tech Spec, write the three registers")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        import json
        print(json.dumps(run(), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
