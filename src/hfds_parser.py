"""Parser for the HFDS Mechanical reverse-brief (Module 01: Mechanical System, 94 pages, text layer).

Source: ``data/source/Basis/HFDS (uploaded)/HFDS - Mechanical reverse-brief.pdf`` (D-036, D-037).

What it does
------------
1. Opens the PDF read-only and, *in memory only*, removes the reviewer FreeText comments (PDF annotations
   by the reviewer "MS75"): their appearance streams sit on top of the table cells and would otherwise be read as
   cell text.  Every reviewer comment and highlight is recorded verbatim in ``hfds_notes.csv`` (kind
   ``reviewer annotation``) and is *not* a design rule.
2. Finds every table with ``Page.find_tables`` and reads each cell from the words of the text layer inside the
   cell box (never by token order).  The main table T.1.1.1.1 (pages 5-13) and the later tables T.1.1.3.5 to
   T.1.1.3.15 and the MV table (page 59) have their columns mapped from the header text.  T.1.1.3.1 / .2 / .10 are
   read as two or three column tables.  Cells that are merged over several rows are inherited by the rows they
   span (flagged ``span_inherited``); empty cells stay empty.
3. Writes ``out/registers/hfds_criteria.csv`` (one row per area / room type, every column as printed beside the
   normalised value), ``out/registers/hfds_notes.csv`` (numbered clauses and items of the manual, the
   T.1.1.3.3 / T.1.1.3.4 AHU/FCU configuration columns, table footnotes, reviewer annotations) and
   ``out/qa/hfds_report.md``.

Every value carries document, PDF page, printed page, location (box), text as printed, method and confidence.
Nothing is read from an image: the PDF has a complete text layer for every table.  Figures (control schematics,
layouts) are not parsed; their pages are listed in the report.  An unknown value stays blank; contradictions
between tables are listed in the report, not resolved.

Usage::

    python3 -m src.hfds_parser run [--pdf FILE]

Nothing here touches the network.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pymupdf

from . import common

HFDS_REL = "data/source/Basis/HFDS (uploaded)/HFDS - Mechanical reverse-brief.pdf"
METHOD_TABLE = "text layer: PyMuPDF find_tables cell grid, words inside each cell box; header-text column map"
METHOD_NOTE = "text layer: PyMuPDF text blocks in column order; clause number and item marker from the first line"
METHOD_ANNOT = "PDF annotation (reviewer comment), content verbatim"
DOC_TITLE = "HFDS - Mechanical reverse-brief.pdf"

CRITERIA_FIELDS = [
    "document", "table_id", "table_title", "table_kind", "pdf_page", "printed_page", "row_index", "row_number",
    "sub_item", "section", "parent_area", "area_name_as_printed", "area_name",
    "temp_as_printed", "rh_as_printed", "pressure_as_printed", "oa_total_ach_as_printed", "oa_ach_as_printed",
    "total_ach_as_printed", "system_mode_as_printed", "exhaust_all_as_printed", "hours_as_printed",
    "filter_bank_as_printed", "terminal_filter_as_printed", "essential_power_as_printed",
    "standby_fan_motor_as_printed", "standby_unit_as_printed", "standby_cfg_column_as_printed",
    "remarks_as_printed", "other_columns_as_printed",
    "temp_setpoint_c", "temp_tol_c", "temp_min_c", "temp_max_c", "temp_note",
    "rh_min_pct", "rh_max_pct", "rh_note",
    "pressure_regime", "pressure_pa", "pressure_ref",
    "oa_ach", "oa_ach_note", "total_ach", "total_ach_qualifier", "total_ach_note",
    "exhaust_ach", "exhaust_ach_basis", "exhaust_all_to_outdoors",
    "hours_h", "hours_norm", "filter_norm", "essential_power", "standby_fan_motor", "standby_unit",
    "standby_config", "standby_config_source", "system_type", "system_type_source", "config_xref",
    "air_system_mode", "report_categories",
    "location", "text_as_printed", "method", "confidence", "confidence_reason", "flags",
]

NOTES_FIELDS = [
    "note_id", "document", "note_kind", "source_table", "section", "clause_id", "clause_title", "item", "pdf_page",
    "printed_page", "text_as_printed", "config", "system_type", "standby_rule", "areas_listed", "rule_tags",
    "report_categories", "location", "method", "confidence", "confidence_reason",
]

# --------------------------------------------------------------------------- #
# Small pure helpers (unit tested)
# --------------------------------------------------------------------------- #
ZWSP = "​"


def clean(text: Optional[str]) -> str:
    """Collapse whitespace, strip zero-width and soft hyphen marks."""
    if text is None:
        return ""
    text = text.replace(ZWSP, "").replace("­", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


_NUM = r"\d+(?:\.\d+)?"


def _f(x: str) -> float:
    return float(x)


def _num_out(v: Optional[float]) -> str:
    if v is None:
        return ""
    return str(int(v)) if float(v).is_integer() else str(v)


def norm_temperature(text: str) -> Dict[str, str]:
    """Normalise a design temperature cell: ``24 +/- 1``, ``21 to 24 +/- 1``, ``18-25``, ``Max. 24``, ``NR``."""
    t = clean(text).replace("–", "-").replace("−", "-").replace("±", "+/-")
    t = re.sub(r"\+\s*/\s*-", "+/-", t)
    out = {"temp_setpoint_c": "", "temp_tol_c": "", "temp_min_c": "", "temp_max_c": "", "temp_note": ""}
    if not t:
        return out
    if t.upper() in ("NR", "-", "NA", "N/A"):
        out["temp_note"] = t
        return out
    m = re.match(r"^Max\.?\s*(%s)\s*(?:deg\s*c)?$" % _NUM, t, re.I)
    if m:
        out["temp_max_c"] = _num_out(_f(m.group(1)))
        out["temp_note"] = "maximum"
        return out
    m = re.match(r"^(%s)\s*(?:to|-)\s*(%s)\s*(?:\+/-\s*(%s))?$" % (_NUM, _NUM, _NUM), t, re.I)
    if m:
        out["temp_min_c"] = _num_out(_f(m.group(1)))
        out["temp_max_c"] = _num_out(_f(m.group(2)))
        if m.group(3):
            out["temp_tol_c"] = _num_out(_f(m.group(3)))
        out["temp_note"] = "controllable range"
        return out
    m = re.match(r"^(%s)\s*\+/-\s*(%s)$" % (_NUM, _NUM), t)
    if m:
        out["temp_setpoint_c"] = _num_out(_f(m.group(1)))
        out["temp_tol_c"] = _num_out(_f(m.group(2)))
        return out
    m = re.match(r"^(%s)$" % _NUM, t)
    if m:
        out["temp_setpoint_c"] = _num_out(_f(m.group(1)))
        return out
    out["temp_note"] = "not normalised: " + t
    return out


def norm_rh(text: str) -> Dict[str, str]:
    """Normalise a relative-humidity cell: ``Max. 60%``, ``40- 60%``, ``NR``, ``No Control``."""
    t = clean(text).replace("–", "-").replace("−", "-")
    out = {"rh_min_pct": "", "rh_max_pct": "", "rh_note": ""}
    if not t:
        return out
    if t.upper() in ("NR", "-") or re.match(r"^no\s*control$", t, re.I):
        out["rh_note"] = t
        return out
    m = re.match(r"^Max\.?\s*(%s)\s*%%?$" % _NUM, t, re.I)
    if m:
        out["rh_max_pct"] = _num_out(_f(m.group(1)))
        return out
    m = re.match(r"^(%s)\s*-\s*(%s)\s*%%?$" % (_NUM, _NUM), t)
    if m:
        out["rh_min_pct"] = _num_out(_f(m.group(1)))
        out["rh_max_pct"] = _num_out(_f(m.group(2)))
        return out
    out["rh_note"] = "not normalised: " + t
    return out


def norm_pressure(text: str) -> Dict[str, str]:
    """Pressure regime and, only when printed, the Pascal figure (``Negative (- 5 Pa)`` gives -5)."""
    t = clean(text)
    out = {"pressure_regime": "", "pressure_pa": "", "pressure_ref": ""}
    if not t:
        return out
    low = t.lower()
    if low in ("nr", "-"):
        out["pressure_regime"] = t.upper() if low == "nr" else "-"
        return out
    pa = re.search(r"([+\-−–]?\s*\d+(?:\.\d+)?)\s*Pa\b", t)
    if pa:
        out["pressure_pa"] = _num_out(_f(re.sub(r"[\s]", "", pa.group(1)).replace("−", "-").replace("–", "-")))
    kinds = [k for k in ("positive", "negative", "neutral", "standard") if k in low]
    if "positive" in kinds and "neutral" in kinds:
        out["pressure_regime"] = "positive or neutral"
    elif "positive" in kinds and "negative" in kinds:
        out["pressure_regime"] = "positive / negative"
    elif kinds:
        out["pressure_regime"] = kinds[0]
    else:
        out["pressure_regime"] = "not normalised"
    ref = re.search(r"w\.?r\.?t\.?\s*(.+)$", t, re.I)
    if ref:
        out["pressure_ref"] = clean(ref.group(1))
    return out


def _ach_part(part: str) -> Tuple[str, str, str]:
    """(value, qualifier, note) of one ACH figure."""
    p = clean(part)
    if not p:
        return "", "", ""
    m = re.match(r"^([<>]=?|≥|≤)?\s*(%s)$" % _NUM, p)
    if m:
        return _num_out(_f(m.group(2))), (m.group(1) or ""), ""
    return "", "", p


def norm_ach_pair(text: str) -> Dict[str, str]:
    """``2 / 6``, ``5 / > 25``, ``NR / 20``, ``2 / Refer to SS553``, ``Refer to SS553``, ``12``.

    A cell with several modes (``2 / 6 (Normal) 2 / 12 (...)``) is not split: numbers stay blank and the note says so.
    """
    t = clean(text)
    out = {"oa_ach": "", "oa_ach_note": "", "total_ach": "", "total_ach_qualifier": "", "total_ach_note": ""}
    if not t:
        return out
    if re.search(r"\((?:normal|pandemic|haze)", t, re.I) or len(re.findall(r"\d\s*/\s*\d", t)) > 1:
        out["oa_ach_note"] = out["total_ach_note"] = "several modes in one cell, see as printed"
        return out
    if re.match(r"^refer to iso", t, re.I):
        out["oa_ach_note"] = out["total_ach_note"] = t
        return out
    if "/" in t:
        a, b = [x.strip() for x in t.split("/", 1)]
        v, q, n = _ach_part(a)
        out["oa_ach"], out["oa_ach_note"] = v, n or (q and "qualifier " + q) or ""
        v, q, n = _ach_part(b)
        out["total_ach"], out["total_ach_qualifier"], out["total_ach_note"] = v, q, n
        return out
    if re.match(r"^refer to ss ?553$", t, re.I):
        out["oa_ach_note"] = out["total_ach_note"] = "Refer to SS553 (one note printed for OA and total)"
        return out
    v, q, n = _ach_part(t)
    if v:
        out["oa_ach"] = v if q == "" else v
        out["oa_ach_note"] = "single figure printed in the OA/total column" + ((" (%s)" % q) if q else "")
    else:
        out["oa_ach_note"] = out["total_ach_note"] = n
    return out


def norm_hours(text: str) -> Dict[str, str]:
    t = clean(text)
    out = {"hours_h": "", "hours_norm": ""}
    if not t:
        return out
    m = re.match(r"^(\d+)\s*hrs?\.?$", t, re.I)
    if m:
        out["hours_h"] = m.group(1)
        out["hours_norm"] = "%sh" % m.group(1)
        return out
    low = t.lower()
    if low in ("off hrs", "off hrs."):
        out["hours_norm"] = "Off Hrs (as printed; may mean office hours, not resolved)"
    elif low in ("office hrs", "office hours", "office hrs."):
        out["hours_norm"] = "office hours"
    else:
        out["hours_norm"] = t
    return out


def norm_yn(text: str) -> str:
    t = clean(text)
    low = t.lower()
    if low in ("y", "yes"):
        return "Y"
    if low in ("n", "no"):
        return "N"
    if low == "nr":
        return "NR"
    if low in ("-", "–"):
        return "-"
    return t


STANDBY_PATTERNS = [
    (re.compile(r"N\s*Duty\s*\+\s*1\s*Stand\s*-?\s*by", re.I), "N Duty + 1 Stand-by"),
    (re.compile(r"1\s*Duty\s*\+\s*1\s*Hot\s*Stand\s*-?\s*by", re.I), "1 Duty + 1 Hot Stand-by"),
    (re.compile(r"1\s*Duty\s*\+\s*1\s*Stand\s*-?\s*by", re.I), "1 Duty + 1 Stand-by"),
    (re.compile(r"Duty\s*\+\s*Hot\s*Stand\s*-?\s*by", re.I), "Duty + Hot Stand-by"),
    (re.compile(r"Duty\s*Hot\s*Stand\s*-?\s*by\s*Fan", re.I), "Duty / Hot Stand-by fan and fan motor"),
    (re.compile(r"Duty\s*\+\s*Stand\s*-?\s*by", re.I), "Duty + Stand-by"),
    (re.compile(r"N\s*\+\s*1", re.I), "N + 1"),
]


def standby_from_text(*texts: str) -> Tuple[str, str]:
    """Return (configuration, which text it came from) for the first stated duty/standby arrangement."""
    for label, text in texts:  # type: ignore[misc]
        t = clean(text)
        for pat, name in STANDBY_PATTERNS:
            if pat.search(t):
                return name, label
    return "", ""


def system_type_from_text(text: str) -> str:
    t = clean(text)
    fcu = bool(re.search(r"\bFCUs?\b|fan coil", t, re.I))
    ahu = bool(re.search(r"\bAHUs?\b|air handling", t, re.I))
    if fcu and ahu:
        return "AHU+FCU"
    return "FCU" if fcu else ("AHU" if ahu else "")


CATEGORY_PATTERNS = [
    ("plant room", re.compile(r"plant\s*room|chiller plant|p-?tube|medical gas|lift motor|machine room|RO/DI", re.I)),
    ("AHU room", re.compile(r"\bAHU\s*room|air handling (unit )?room|\bCRAC\b", re.I)),
    ("electrical/transformer/switch", re.compile(r"transformer|switch\s*room|consumer switch|\bCSR\b|\bHT room|electrical (plant )?room|electrical plant|substation|\bFCC\b", re.I)),
    ("LAN/IT/server/comms", re.compile(r"\bLAN\b|server|comms|\bMDF\b|\bIDF\b|\bIT room|data cent", re.I)),
    ("UPS/battery", re.compile(r"\bUPS\b|batter", re.I)),
    ("riser", re.compile(r"\brisers?\b", re.I)),
    ("store", re.compile(r"\bstores?\b|storage|bulk", re.I)),
    ("lift lobby", re.compile(r"lift lobb", re.I)),
    ("laboratory/innovation", re.compile(r"\blab\b(?! office)|laborator|innovation|research lab|wet lab", re.I)),
]


def categories_for(*texts: str) -> str:
    blob = " | ".join(clean(t) for t in texts if t)
    return "; ".join(name for name, pat in CATEGORY_PATTERNS if pat.search(blob))


RULE_TAGS = [
    ("standby", re.compile(r"stand\s*-?\s*by|N\s*\+\s*1|duty", re.I)),
    ("FCU", re.compile(r"\bFCUs?\b|fan coil", re.I)),
    ("AHU", re.compile(r"\bAHUs?\b", re.I)),
    ("plant room", re.compile(r"plant\s*room|machine room|motor room", re.I)),
    ("electrical room", re.compile(r"transformer|switch room|\bCSR\b|\bHT room|electrical plant", re.I)),
    ("IT/LAN/server", re.compile(r"\bLAN\b|server|comms|\bMDF\b", re.I)),
    ("UPS/battery", re.compile(r"\bUPS\b|batter", re.I)),
    ("riser", re.compile(r"\brisers?\b", re.I)),
    ("store", re.compile(r"\bstores?\b|storage", re.I)),
    ("lift", re.compile(r"\blift\b", re.I)),
    ("laboratory", re.compile(r"\blab\b|laborator|innovation", re.I)),
    ("pressure", re.compile(r"pressur", re.I)),
    ("air change", re.compile(r"\bACH\b|air change", re.I)),
    ("filter/HEPA", re.compile(r"HEPA|MERV|filter", re.I)),
    ("exhaust", re.compile(r"exhaust", re.I)),
    ("single pass/recirculation", re.compile(r"single pass|recirculat", re.I)),
    ("power", re.compile(r"essential power|emergency power|E-Supply|N-Supply|generator", re.I)),
]


def rule_tags(text: str) -> str:
    return "; ".join(name for name, pat in RULE_TAGS if pat.search(text))


def split_area_items(cell: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Split a room description cell holding sub-items ``a) ... b) ...``.

    Returns (stem printed before the first marker, [(marker, text), ...]).  Markers must run a, b, c ... in order.
    A cell without markers returns ('', []).
    """
    t = clean(cell)
    found: List[Tuple[int, str]] = []
    expect = ""
    for m in re.finditer(r"(?:(?<=\s)|^)([a-h])\)\s*", t):
        if not expect and not found:
            expect = m.group(1)        # the first marker may be b), c) ... when the sibling is the row above
        if m.group(1) == expect:
            found.append((m.start(), m.group(1)))
            expect = chr(ord(expect) + 1)
    if not found:
        return "", []
    stem = t[: found[0][0]].strip()
    items: List[Tuple[str, str]] = []
    for i, (pos, mk) in enumerate(found):
        end = found[i + 1][0] if i + 1 < len(found) else len(t)
        body = re.sub(r"^[a-h]\)\s*", "", t[pos:end]).strip()
        items.append((mk, body))
    return stem, items


# --------------------------------------------------------------------------- #
# PDF access
# --------------------------------------------------------------------------- #
def open_pdf(path: Path) -> pymupdf.Document:
    doc = pymupdf.open(str(path))
    return doc


def collect_annotations(doc: pymupdf.Document) -> List[Dict[str, Any]]:
    """Reviewer annotations of every page (text verbatim); FreeText / Text ones are then removed in memory."""
    out: List[Dict[str, Any]] = []
    for pno, page in enumerate(doc, 1):
        for a in list(page.annots() or []):
            info = a.info or {}
            kind = a.type[1]
            out.append({
                "pdf_page": pno, "kind": kind, "rect": [round(x, 1) for x in a.rect],
                "content": clean(info.get("content", "")), "author": clean(info.get("title", "")),
            })
            if kind in ("FreeText", "Text"):
                page.delete_annot(a)
    return out


def printed_page_number(page: pymupdf.Page) -> str:
    """Page number printed at the foot ('VOL. 2' then the number at the right margin)."""
    best = ""
    for b in page.get_text("blocks"):
        if b[1] > 560:
            m = re.search(r"VOL\.\s*2\s+(\d{1,3})\b", b[4])
            if m:
                return m.group(1)
            if b[0] > 700 and re.match(r"^\s*\d{1,3}\s*$", b[4]):
                best = b[4].strip()
    return best


def cell_text(page: pymupdf.Page, bbox: Optional[Sequence[float]]) -> Optional[str]:
    if not bbox:
        return None
    rect = pymupdf.Rect(bbox) + (-0.4, -0.4, 0.4, 0.4)
    words = page.get_text("words", clip=rect)
    words = [w for w in words if rect.contains(pymupdf.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2))]
    words.sort(key=lambda w: (round(w[3] / 3.0), w[0]))
    return clean(" ".join(w[4] for w in words))


def find_captions(page: pymupdf.Page) -> List[Dict[str, Any]]:
    """Lines beginning ``Table T.x.y.z:`` with their title (a wrapped title line is joined)."""
    caps: List[Dict[str, Any]] = []
    lines: List[Tuple[float, float, float, str]] = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for ln in b["lines"]:
            txt = "".join(s["text"] for s in ln["spans"])
            lines.append((ln["bbox"][0], ln["bbox"][1], ln["bbox"][3], txt))
    for i, (x0, y0, y1, txt) in enumerate(lines):
        m = re.search(r"Table\s+(T\.\d+(?:\.\d+)+)\s*:\s*(.*)$", clean(txt))
        if not m:
            continue
        title = m.group(2)
        # wrapped title: the next line directly below at the same left edge
        for (nx0, ny0, ny1, ntxt) in lines:
            if abs(nx0 - x0) < 4 and 0 < ny0 - y0 < 16 and not re.match(r"^(Table|Note|\d|\*|VOL|Abbrev)", clean(ntxt)):
                title = clean(title + " " + ntxt)
                break
        title = re.sub(r"\s+\d{1,3}$", "", title)
        title = title.rstrip(":").strip()
        caps.append({"table_id": m.group(1), "title": title, "y": y0, "x": x0})
    return caps


HEADER_KEYS: List[Tuple[str, re.Pattern[str]]] = [
    ("sno", re.compile(r"^sno$")),
    ("area", re.compile(r"^(roomdescription|areadescription|area|typeofoperatingtheatre|isolationroom)$")),
    ("temp", re.compile(r"^(indoordesigncondition.*|tempdegc|roomtemp.*|roomtempdegc|tempdegc)$")),
    ("rh", re.compile(r"^(rh|relativehumidity.*)$")),
    ("pressure", re.compile(r"^pressurerelationship.*")),
    ("oa_total", re.compile(r"^minoutdoorach.*mintotalach")),
    ("oa", re.compile(r"^(minimumoutdoorairchanges|minoutdoorairchange.*)$")),
    ("total", re.compile(r"^(minimumtotalairchanges|mintotalairchange.*)$")),
    ("exhaust_all", re.compile(r"^allroomair.*")),
    ("hours", re.compile(r"^(operating.*hrs|acoperating.*hours?)$")),
    ("filter", re.compile(r"^(filterbank|ahufilter.*)$")),
    ("terminal", re.compile(r"^terminalfilter$")),
    ("essential", re.compile(r"^essentialpower.*")),
    ("standby_fan", re.compile(r"^standby.*fan.*motor$")),
    ("standby_unit", re.compile(r"^standby.*(ahu|fcu).*$")),
    ("system_mode", re.compile(r"^(acsystem)?recirculation.*|^acsystemrecirculation.*")),
    ("remarks", re.compile(r"^remarks$")),
]


def header_key(text: str) -> Optional[str]:
    k = re.sub(r"[^a-z]", "", (text or "").lower())
    k = k.replace("remarks", "remarks").replace("rema", "rema")
    if re.match(r"^rema\w*rks$", k):
        k = "remarks"
    for name, pat in HEADER_KEYS:
        if pat.search(k):
            return name
    return None


# Number of header rows per table id (default 1).
HEADER_ROWS = {"T.1.1.3.5": 3, "T.1.1.3.6": 3, "T.1.1.3.13": 2, "T.1.1.3.14": 2, "T.1.1.3.15": 2}
MV_TABLE_KEY = "T.1.1.3.14 (MV)"


def build_column_map(header_rows: List[List[Optional[str]]], ncols: int) -> Tuple[Dict[int, str], Dict[int, str]]:
    """Map column index -> canonical key from the merged header text; also returns raw header text per column."""
    raw: Dict[int, str] = {}
    for j in range(ncols):
        parts = [clean(r[j]) for r in header_rows if j < len(r) and r[j]]
        raw[j] = " ".join(p for p in parts if p)
    cmap: Dict[int, str] = {}
    for j in range(ncols):
        parts = [clean(r[j]) for r in header_rows if j < len(r) and r[j]]
        key = (header_key(parts[-1]) if parts else None) or header_key(raw[j])
        # 'Stand-by Fan & Fan Motor' and 'Stand-by AHU / FCU' share a stem: tell them apart by the words
        low = re.sub(r"[^a-z]", "", raw[j].lower())
        if "standby" in low or "stand" in low and "by" in low:
            if "ahu" in low or "fcu" in low:
                key = "standby_unit"
            elif "fan" in low:
                key = "standby_fan"
        if key and key not in cmap.values():
            cmap[j] = key
    # an unnamed column right after 'Indoor Design Condition' is the RH half of that merged header
    for j in range(1, ncols):
        if j not in cmap and cmap.get(j - 1) == "temp" and not raw[j] and "rh" not in cmap.values():
            cmap[j] = "rh"
    # 'Room Description' with an unnamed next column (SOC group tables): second column is the room
    if "area" in cmap.values():
        aj = [j for j, k in cmap.items() if k == "area"][0]
        if aj + 1 < ncols and (aj + 1) not in cmap and not raw[aj + 1]:
            cmap[aj + 1] = "subarea"
    # an unnamed last column right of Remarks holds the duty/standby arrangement
    last = ncols - 1
    if last not in cmap and not raw[last] and "remarks" in cmap.values() and "standby_cfg" not in cmap.values():
        cmap[last] = "standby_cfg"
    return cmap, raw


# --------------------------------------------------------------------------- #
# Table extraction
# --------------------------------------------------------------------------- #
class TableRaw:
    def __init__(self, page_no: int, table_id: str, title: str, bbox: Sequence[float], rows: List[List[Optional[str]]],
                 row_boxes: List[Tuple[float, float, float, float]], cell_boxes: List[List[Optional[Tuple[float, ...]]]],
                 printed: str, uncovered_words: List[str]):
        self.page_no = page_no
        self.table_id = table_id
        self.title = title
        self.bbox = bbox
        self.rows = rows
        self.row_boxes = row_boxes
        self.cell_boxes = cell_boxes
        self.printed = printed
        self.uncovered_words = uncovered_words


def extract_tables(doc: pymupdf.Document) -> Tuple[List[TableRaw], List[Dict[str, Any]]]:
    """Every table of the document as raw cell text, with its caption and the words inside it that no cell holds."""
    tables: List[TableRaw] = []
    issues: List[Dict[str, Any]] = []
    for pno, page in enumerate(doc, 1):
        found = page.find_tables().tables
        if not found:
            continue
        caps = find_captions(page)
        printed = printed_page_number(page)
        for t in found:
            if t.row_count < 2 or t.col_count < 2:
                issues.append({"pdf_page": pno, "what": "table-like grid ignored", "why": "fewer than 2 rows or 2 columns",
                               "bbox": [round(x) for x in t.bbox]})
                continue
            rows: List[List[Optional[str]]] = []
            rboxes: List[Tuple[float, float, float, float]] = []
            cboxes: List[List[Optional[Tuple[float, ...]]]] = []
            for r in t.rows:
                rows.append([cell_text(page, b) for b in r.cells])
                cboxes.append([tuple(b) if b else None for b in r.cells])
                rboxes.append(tuple(r.bbox))
            # caption: nearest caption (above or below) on the page
            cap = None
            if caps:
                cap = min(caps, key=lambda c: min(abs(c["y"] - t.bbox[1]), abs(c["y"] - t.bbox[3])))
            tid = cap["table_id"] if cap else ""
            title = cap["title"] if cap else ""
            # words inside the table box that no cell box holds
            tb = pymupdf.Rect(t.bbox)
            inside = [w for w in page.get_text("words", clip=tb)]
            held = [pymupdf.Rect(b) + (-1, -1, 1, 1) for cb in cboxes for b in cb if b]
            unc = [w[4] for w in inside
                   if not any(h.contains(pymupdf.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)) for h in held)]
            tables.append(TableRaw(pno, tid, title, tuple(t.bbox), rows, rboxes, cboxes, printed, unc))
    # the manual prints T.1.1.3.14 twice (SOC APN RSP table and MV table): keep both, key the MV one apart;
    # T.1.1.1.1 repeats on pages 5-13 because it is one table continued
    seen: Dict[str, TableRaw] = {}
    for tr in tables:
        first = seen.get(tr.table_id)
        if first is not None and first.title != tr.title:
            if re.search(r"\bMV\b", tr.title):
                tr.table_id = MV_TABLE_KEY
            issues.append({"pdf_page": tr.page_no, "what": "table id %s printed twice with different titles" % first.table_id,
                           "why": "pages %d ('%s') and %d ('%s'); both kept, the later one keyed '%s'"
                                  % (first.page_no, first.title[:50], tr.page_no, tr.title[:50], tr.table_id),
                           "bbox": []})
        seen.setdefault(tr.table_id, tr)
    return tables, issues


def table_kind(tid: str) -> str:
    if tid == "T.1.1.1.1":
        return "ACMV design criteria (main table)"
    if tid in ("T.1.1.3.1",):
        return "pressure relationship list"
    if tid == "T.1.1.3.2":
        return "isolation room types"
    if tid in ("T.1.1.3.3", "T.1.1.3.4"):
        return "AHU/FCU configuration"
    if tid == "T.1.1.3.10":
        return "operating theatre cleanliness (cfu)"
    if tid == MV_TABLE_KEY:
        return "MV design condition"
    return "AC design condition"


# --------------------------------------------------------------------------- #
# Configuration tables T.1.1.3.3 / T.1.1.3.4 -> notes, and area cross-reference
# --------------------------------------------------------------------------- #
def parse_config_table(tr: TableRaw) -> List[Dict[str, Any]]:
    """Columns of T.1.1.3.3 / T.1.1.3.4 as configuration records."""
    header = tr.rows[0]
    labels = {r[0]: r for r in tr.rows[1:] if r and r[0]}
    out = []
    for j in range(1, len(header)):
        label = clean(header[j])
        if not label:
            continue
        cfg_name = re.sub(r"\s*Pressure Regime\s*", "", label).strip()
        g = lambda key: clean(labels.get(key, [None] * (j + 1))[j]) if key in labels and j < len(labels[key]) else ""  # noqa: E731
        areas_raw = g("Areas")
        items = [clean(x) for x in re.split(r"(?:(?<=\s)|^)\d{1,2}\.\s*(?=[A-Za-z])", areas_raw) if clean(x)]
        control = g("Control Strategy")
        equip = g("Equipment Configuration")
        out.append({
            "config": cfg_name, "equipment": equip, "lcp": g("LCP"), "electrical": g("Electrical Power Supply"),
            "control": control, "areas": items, "areas_raw": areas_raw, "col": j,
            "system_type": system_type_from_text(control + " " + equip),
            "standby": standby_from_text(("config control strategy", control))[0] or control,
        })
    return out


def norm_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", clean(text).lower())


def config_xref_for(area: str, parent: str, configs: List[Tuple[str, Dict[str, Any]]]) -> List[Tuple[str, str, Dict[str, Any]]]:
    """Configurations (of T.1.1.3.3 / T.1.1.3.4) whose listed area name matches the row's area by name."""
    out = []
    a = norm_key(area)
    p = norm_key(parent)
    if not a:
        return out
    for tid, cfg in configs:
        for item in cfg["areas"]:
            k = norm_key(item)
            k2 = norm_key(re.sub(r"\s*\(.*?\)", "", item))
            for kk in {k, k2}:
                if len(kk) >= 6 and (kk == a or kk in a or (len(a) >= 8 and a in kk) or kk == p):
                    out.append((tid, item, cfg))
                    break
    return out


# --------------------------------------------------------------------------- #
# Row building
# --------------------------------------------------------------------------- #
def blank_row() -> Dict[str, Any]:
    return {k: "" for k in CRITERIA_FIELDS}


def fill_normalised(row: Dict[str, Any], c: Dict[str, str], tid: str) -> None:
    """Set the normalised columns of ``row`` from the as-printed values in ``c`` (keys as header map)."""
    row["temp_as_printed"] = c.get("temp", "")
    row["rh_as_printed"] = c.get("rh", "")
    row["pressure_as_printed"] = c.get("pressure", "")
    row["oa_total_ach_as_printed"] = c.get("oa_total", "")
    row["oa_ach_as_printed"] = c.get("oa", "")
    row["total_ach_as_printed"] = c.get("total", "")
    row["system_mode_as_printed"] = c.get("system_mode", "")
    row["exhaust_all_as_printed"] = c.get("exhaust_all", "")
    row["hours_as_printed"] = c.get("hours", "")
    row["filter_bank_as_printed"] = c.get("filter", "")
    row["terminal_filter_as_printed"] = c.get("terminal", "")
    row["essential_power_as_printed"] = c.get("essential", "")
    row["standby_fan_motor_as_printed"] = c.get("standby_fan", "")
    row["standby_unit_as_printed"] = c.get("standby_unit", "")
    row["standby_cfg_column_as_printed"] = c.get("standby_cfg", "")
    row["remarks_as_printed"] = c.get("remarks", "")
    row.update(norm_temperature(c.get("temp", "")))
    row.update(norm_rh(c.get("rh", "")))
    row.update(norm_pressure(c.get("pressure", "")))
    if c.get("oa_total"):
        row.update(norm_ach_pair(c["oa_total"]))
    else:
        if c.get("oa"):
            v, q, n = _ach_part(c["oa"])
            row["oa_ach"], row["oa_ach_note"] = v, n or (("qualifier " + q) if q else "")
        if c.get("total"):
            v, q, n = _ach_part(c["total"])
            row["total_ach"], row["total_ach_qualifier"], row["total_ach_note"] = v, q, n
    row.update(norm_hours(c.get("hours", "")))
    ex = norm_yn(c.get("exhaust_all", "")) if c.get("exhaust_all") else ""
    row["exhaust_all_to_outdoors"] = ex
    mode = c.get("system_mode", "")
    if re.match(r"^exhaust$", clean(mode), re.I) and row["total_ach"]:
        row["exhaust_ach"] = row["total_ach"]
        row["exhaust_ach_basis"] = "air system mode printed as Exhaust; figure from the total ACH column"
    elif ex == "Y" and row["total_ach"]:
        row["exhaust_ach"] = row["total_ach"]
        row["exhaust_ach_basis"] = "all room air exhausted directly to outdoors = Y, so exhaust equals total ACH (derived)"
    filt = [x for x in (c.get("filter", ""), ("terminal " + c["terminal"]) if c.get("terminal") and c["terminal"] not in ("-", "–") else "") if x]
    row["filter_norm"] = "; ".join(filt)
    row["essential_power"] = norm_yn(c.get("essential", "")) if c.get("essential") else ""
    row["standby_fan_motor"] = norm_yn(c.get("standby_fan", "")) if c.get("standby_fan") else ""
    row["standby_unit"] = norm_yn(c.get("standby_unit", "")) if c.get("standby_unit") else ""
    sb, src = standby_from_text(("standby column", c.get("standby_cfg", "")), ("remarks", c.get("remarks", "")))
    if sb:
        row["standby_config"] = sb
        row["standby_config_source"] = "stated in " + src
    elif row["standby_unit"] == "Y" or row["standby_fan_motor"] == "Y":
        parts = []
        if row["standby_unit"] == "Y":
            parts.append("standby AHU/FCU = Y")
        if row["standby_fan_motor"] == "Y":
            parts.append("standby fan & fan motor = Y")
        row["standby_config"] = "; ".join(parts) + " (arrangement not stated in the row)"
        row["standby_config_source"] = "Y flags of the table"
    elif row["standby_unit"] == "N" and row["standby_fan_motor"] == "N":
        row["standby_config"] = "no standby (N / N)"
        row["standby_config_source"] = "N flags of the table"
    row["air_system_mode"] = mode


def finish_row(row: Dict[str, Any], tr: TableRaw, area_stem_parent: str, configs: List[Tuple[str, Dict[str, Any]]]) -> None:
    """Add system type, configuration cross-reference and report categories."""
    stated = system_type_from_text(" ".join([row["area_name_as_printed"], row["remarks_as_printed"], row["standby_cfg_column_as_printed"], row["standby_config"] if "stated" in row["standby_config_source"] else ""]))
    xrefs = config_xref_for(row["area_name"], row["parent_area"], configs) if tr.table_id in ("T.1.1.1.1",) or True else []
    if xrefs:
        row["config_xref"] = "; ".join("%s Configuration %s (name match: '%s')" % (t, cfg["config"].replace("Configuration ", ""), item) for t, item, cfg in xrefs)
    if stated:
        row["system_type"] = stated
        row["system_type_source"] = "stated in the row text"
    elif xrefs:
        types = sorted({cfg["system_type"] for _, _, cfg in xrefs if cfg["system_type"]})
        if len(types) == 1:
            row["system_type"] = types[0]
            row["system_type_source"] = "T.1.1.3.3/T.1.1.3.4 configuration list (name match, medium confidence)"
        elif types:
            row["system_type"] = "/".join(types)
            row["system_type_source"] = "several configuration lists match by name (conflict, not resolved)"
    cats = categories_for(row["area_name"], row["parent_area"], row["area_name_as_printed"], " ".join(item for _, item, _ in xrefs))
    row["report_categories"] = cats


def rows_from_table(tr: TableRaw, configs: List[Tuple[str, Dict[str, Any]]], document: str,
                    state: Optional[Dict[str, Any]] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Criteria rows of one generic or main-style table.  Returns (rows, problems)."""
    problems: List[str] = []
    nh = HEADER_ROWS.get(tr.table_id, 1)
    header = tr.rows[:nh]
    ncols = max(len(r) for r in tr.rows)
    cmap, raw_header = build_column_map(header, ncols)
    if "area" not in cmap.values():
        problems.append("no room/area column found from the header text; table not parsed")
        return [], problems
    area_j = [j for j, k in cmap.items() if k == "area"][0]
    sno_j = [j for j, k in cmap.items() if k == "sno"]
    sub_j = [j for j, k in cmap.items() if k == "subarea"]
    unmapped = [j for j in range(ncols) if j not in cmap and raw_header[j]]
    rows_out: List[Dict[str, Any]] = []
    last_cell: Dict[int, Tuple[Tuple[float, ...], str]] = {}
    state = state if state is not None else {}
    section = state.get("section", "")
    cur_no = state.get("cur_no", "")
    cur_parent = state.get("cur_parent", "")
    group_area = state.get("group_area", "")
    data_idx = state.get("data_idx", 0)
    for ri in range(nh, len(tr.rows)):
        cells = tr.rows[ri]
        boxes = tr.cell_boxes[ri]
        rb = tr.row_boxes[ri]
        vals = {j: (cells[j] if j < len(cells) else None) for j in range(ncols)}
        own_nonempty = [j for j in range(ncols) if (vals.get(j) or "") != ""]
        if not own_nonempty:
            continue
        inherited: List[str] = []
        spanned_remarks = False
        for j in range(ncols):
            if vals[j] is None:
                lc = last_cell.get(j)
                if lc and lc[0][3] > rb[1] + 2.0 and lc[0][1] <= rb[1] + 1 and j in cmap and cmap[j] != "sno":
                    if cmap[j] == "remarks":
                        spanned_remarks = bool(lc[1])      # a remark spanning rows is not copied: it may apply to one of them
                        continue
                    vals[j] = lc[1]
                    inherited.append(cmap[j])
            elif boxes[j] is not None and vals[j] != "":
                last_cell[j] = (boxes[j], vals[j])
        nonempty = own_nonempty
        area_cell = vals.get(area_j) or ""
        sno = clean(vals[sno_j[0]]) if sno_j else ""
        c: Dict[str, str] = {cmap[j]: clean(vals[j]) for j in cmap if j in vals and vals[j] is not None and cmap[j] not in ("sno", "area")}
        c_print: Dict[str, str] = {cmap[j]: clean(cells[j]) for j in cmap if j < len(cells) and cells[j] is not None and cmap[j] not in ("sno", "area")}
        other_cells = [j for j in nonempty if j != area_j and (not sno_j or j != sno_j[0]) and not (sub_j and j == sub_j[0] and False)]
        # heading rows: a number and a name only, or a name only (section)
        if sno_j and not [j for j in other_cells] and area_cell:
            if sno:
                cur_no, cur_parent = sno, area_cell
                continue
            section = area_cell
            cur_parent = ""
            continue
        if not sno_j and sub_j and area_cell and not [j for j in other_cells if j != sub_j[0]] and not (vals.get(sub_j[0]) or ""):
            group_area = area_cell
            continue
        if sno:
            cur_no = sno
            cur_parent = ""
        sub_raw = clean(vals[sub_j[0]]) if sub_j and vals.get(sub_j[0]) else ""
        # table of groups (SOC ...): column 0 is the group, column 1 the room
        if sub_j:
            if area_cell and area_cell != group_area:
                group_area = area_cell
            area_printed = (group_area + " - " + sub_raw) if (group_area and sub_raw and area_cell == group_area) else (sub_raw or area_cell)
            if group_area and sub_raw:
                area_printed = group_area + " - " + sub_raw
            stem, items = "", []
        else:
            area_printed = clean(area_cell)
            stem, items = split_area_items(area_printed)
        data_idx += 1
        unit_flags: List[str] = []
        targets: List[Tuple[str, str, str]] = []
        if items:
            if len(items) > 1:
                unit_flags.append("shared_row_split(%d sub-items share one set of values)" % len(items))
            for mk, body in items:
                name = clean((stem + " " + body).strip()) if stem else body
                targets.append((mk, name, area_printed))
        else:
            name = area_printed
            targets.append(("", name, area_printed))
        for mk, name, printed in targets:
            row = blank_row()
            row.update({
                "document": document, "table_id": tr.table_id, "table_title": tr.title, "table_kind": table_kind(tr.table_id),
                "pdf_page": tr.page_no, "printed_page": tr.printed, "row_index": data_idx, "row_number": cur_no if sno_j else "",
                "sub_item": mk, "section": section, "parent_area": cur_parent or group_area if sub_j else (cur_parent or ""),
                "area_name_as_printed": printed, "area_name": name,
            })
            if sub_j:
                row["parent_area"] = group_area
            if mk and not cur_parent and not stem and sno_j and not sno:
                row["parent_area"] = cur_parent
            fill_normalised(row, c, tr.table_id)
            for key, field in (("temp", "temp_as_printed"), ("rh", "rh_as_printed"), ("pressure", "pressure_as_printed"),
                               ("oa_total", "oa_total_ach_as_printed"), ("oa", "oa_ach_as_printed"), ("total", "total_ach_as_printed"),
                               ("system_mode", "system_mode_as_printed"), ("exhaust_all", "exhaust_all_as_printed"),
                               ("hours", "hours_as_printed"), ("filter", "filter_bank_as_printed"),
                               ("terminal", "terminal_filter_as_printed"), ("essential", "essential_power_as_printed"),
                               ("standby_fan", "standby_fan_motor_as_printed"), ("standby_unit", "standby_unit_as_printed"),
                               ("standby_cfg", "standby_cfg_column_as_printed"), ("remarks", "remarks_as_printed")):
                row[field] = c_print.get(key, "")       # as printed = this row's own cell; inherited values only feed the normalised columns
            if unmapped:
                other = {raw_header[j]: clean(vals[j]) for j in unmapped if vals.get(j)}
                row["other_columns_as_printed"] = json.dumps(other, ensure_ascii=False) if other else ""
            row["location"] = "page %d table %s row %d box [%s]" % (tr.page_no, tr.table_id, ri + 1, ", ".join("%.0f" % x for x in rb))
            row["text_as_printed"] = " | ".join(clean(vals[j]) for j in range(ncols) if vals.get(j))
            row["method"] = METHOD_TABLE
            flags = list(unit_flags)
            if inherited:
                flags.append("span_inherited(%s)" % ",".join(sorted(set(inherited))))
            if spanned_remarks:
                flags.append("remarks_cell_spans_from_row_above(not copied)")
            if mk and sno_j and not sno:
                flags.append("row_number_inherited")
            if not any(c.get(k) for k in ("temp", "pressure", "oa_total", "oa", "total", "rh")):
                flags.append("no_numeric_columns")
            row["flags"] = "; ".join(flags)
            reasons = []
            conf = "high"
            if inherited:
                conf = "medium"
                reasons.append("value(s) read from a cell merged over several rows")
            if unit_flags:
                conf = "medium"
                reasons.append("one row of values printed for several sub-items")
            if "several modes" in (row["oa_ach_note"] + row["total_ach_note"]):
                conf = "medium"
                reasons.append("several modes in one ACH cell, not split")
            if "not normalised" in (row["temp_note"] + row["rh_note"] + row["pressure_regime"]):
                conf = "medium"
                reasons.append("a cell did not match the normalisation pattern; as printed is kept")
            row["confidence"] = conf
            row["confidence_reason"] = "; ".join(reasons) if reasons else "text layer, cell box, header-mapped column"
            finish_row(row, tr, cur_parent, configs)
            rows_out.append(row)
    state.update({"section": section, "cur_no": cur_no, "cur_parent": cur_parent, "group_area": group_area, "data_idx": data_idx})
    return rows_out, problems


def rows_from_mv_table(tr: TableRaw, configs: List[Tuple[str, Dict[str, Any]]], document: str) -> List[Dict[str, Any]]:
    """T.1.1.3.14 (MV): Area, Mode, Air changes provided (with sub-cases), Remarks, Air changes specified, Reference."""
    out: List[Dict[str, Any]] = []
    cur_area = ""
    cur_mode = ""
    idx = 0
    for ri in range(2, len(tr.rows)):
        r = [clean(x) if x is not None else "" for x in tr.rows[ri]]
        r += [""] * (7 - len(r))
        if not any(r) or r[0].lower().startswith("note"):
            if r[0].lower().startswith("note"):
                continue
            continue
        if r[0]:
            cur_area, cur_mode = r[0], r[1]
        area, mode = cur_area, cur_mode
        # sub-case rows: [label, value, remarks, specified, reference] or [value, None, remarks, specified, ref]
        if r[2] and re.match(r"^\d+$", r[2]):
            label, val, rem, spec, ref = "", r[2], r[4], r[5], r[6]
        else:
            label, val, rem, spec, ref = r[2], r[3], r[4], r[5], r[6]
        idx += 1
        row = blank_row()
        row.update({
            "document": document, "table_id": tr.table_id, "table_title": tr.title, "table_kind": table_kind(tr.table_id),
            "pdf_page": tr.page_no, "printed_page": tr.printed, "row_index": idx, "row_number": "",
            "sub_item": label, "parent_area": area if label else "",
            "area_name_as_printed": area, "area_name": (area + (" - " + label if label else "")),
            "system_mode_as_printed": mode, "air_system_mode": mode, "remarks_as_printed": rem,
            "total_ach_as_printed": val,
            "other_columns_as_printed": json.dumps({"air changes specified": spec, "reference": ref, "sub-case": label}, ensure_ascii=False),
        })
        if re.match(r"^\d+(\.\d+)?$", val):
            row["exhaust_ach"] = val
            row["exhaust_ach_basis"] = "air changes provided by an exhaust system (MV table)"
            row["total_ach"] = val
            row["total_ach_note"] = "air changes provided (MV table)"
        row["location"] = "page %d table MV row %d box [%s]" % (tr.page_no, ri + 1, ", ".join("%.0f" % x for x in tr.row_boxes[ri]))
        row["text_as_printed"] = " | ".join(x for x in r if x)
        row["method"] = METHOD_TABLE + "; fixed position parse of the MV table"
        row["confidence"] = "medium" if label else "high"
        row["confidence_reason"] = "sub-case row of a table with shifting columns; read by position" if label else "text layer, cell box"
        row["flags"] = ""
        finish_row(row, tr, "", configs)
        out.append(row)
    return out


def rows_from_pairs_table(tr: TableRaw, configs: List[Tuple[str, Dict[str, Any]]], document: str) -> List[Dict[str, Any]]:
    """T.1.1.3.1 (area, pressure) and T.1.1.3.2 (isolation room type, ACMV provisions)."""
    out = []
    for ri in range(1, len(tr.rows)):
        r = [clean(x) if x is not None else "" for x in tr.rows[ri]]
        if not any(r):
            continue
        row = blank_row()
        row.update({
            "document": document, "table_id": tr.table_id, "table_title": tr.title, "table_kind": table_kind(tr.table_id),
            "pdf_page": tr.page_no, "printed_page": tr.printed, "row_index": ri, "area_name_as_printed": r[0], "area_name": r[0],
        })
        if tr.table_id == "T.1.1.3.1":
            row["pressure_as_printed"] = r[1]
            row.update(norm_pressure(r[1]))
        else:
            row["remarks_as_printed"] = r[1]
            low = r[1].lower()
            if "negative" in low and "positive" in low:
                row["pressure_regime"] = "positive / negative (combination)"
            elif "negative" in low:
                row["pressure_regime"] = "negative"
            elif "positive" in low:
                row["pressure_regime"] = "positive"
            elif "standard" in low:
                row["pressure_regime"] = "standard"
            dp = re.findall(r"([≥>]\s*[+\-−]?\s*\d+(?:\.\d+)?)\s*Pa", r[1])
            if dp:
                row["pressure_ref"] = "differential pressures printed: " + "; ".join(clean(x) for x in dp)
            m = re.search(r"(\d+)\s*(?:air change|ACH)[^.]*patient room", r[1], re.I)
            if m:
                row["total_ach"] = m.group(1)
                row["total_ach_note"] = "patient room (minimum, as printed in the provisions text)"
            row["pressure_pa"] = ""
        row["location"] = "page %d table %s row %d" % (tr.page_no, tr.table_id, ri + 1)
        row["text_as_printed"] = " | ".join(x for x in r if x)
        row["method"] = METHOD_TABLE
        row["confidence"] = "high" if tr.table_id == "T.1.1.3.1" else "medium"
        row["confidence_reason"] = "text layer" if tr.table_id == "T.1.1.3.1" else "text layer; bullet text kept as printed, only pressure direction normalised"
        finish_row(row, tr, "", configs)
        out.append(row)
    return out


# --------------------------------------------------------------------------- #
# Notes: clauses and items, table footnotes, configurations, annotations
# --------------------------------------------------------------------------- #
CLAUSE_RE = re.compile(r"^(1\.\d+(?:\.\d+)*)\.?\s*(.*)$", re.S)
MARKER_RE = re.compile(r"^(\(?[a-z]\)|[ivx]{1,5}\.|\d{1,2}\.)$")


def _item_level(marker: str) -> int:
    if re.match(r"^[ivx]+\.$", marker):
        return 2
    return 1


def extract_clauses(doc: pymupdf.Document, tables: List[TableRaw]) -> Tuple[List[Dict[str, Any]], Dict[int, int]]:
    """Numbered clauses and their lettered / roman / numbered items, in column order per page.

    Returns (note rows, {pdf_page: number of text blocks not attached to a clause}).
    """
    table_boxes: Dict[int, List[pymupdf.Rect]] = defaultdict(list)
    for tr in tables:
        table_boxes[tr.page_no].append(pymupdf.Rect(tr.bbox))
    notes: List[Dict[str, Any]] = []
    unattached: Dict[int, int] = defaultdict(int)
    last_pos: Dict[int, Tuple[int, float, float, str]] = {}
    section = ""
    clause_id, clause_title = "", ""
    clause_x = 0.0
    for pno, page in enumerate(doc, 1):
        if pno < 2:
            continue
        printed = printed_page_number(page)
        blocks = [b for b in page.get_text("blocks") if b[6] == 0]
        def keyfn(b):
            col = 1 if (b[0] >= 430 and (b[2] - b[0]) < 420) else 0
            return (col, round(b[1]), b[0])
        blocks.sort(key=keyfn)
        letter_stack = roman_stack = ""
        for b in blocks:
            x0, y0, x1, y1 = b[:4]
            text = b[4]
            if y0 < 45 or y0 > 560:
                continue
            br = pymupdf.Rect(x0, y0, x1, y1)
            if any(tb.intersects(br) and (tb & br).get_area() > 0.5 * br.get_area() for tb in table_boxes.get(pno, [])):
                continue
            lines = [clean(x) for x in text.split("\n") if clean(x)]
            if not lines:
                continue
            records: List[Dict[str, Any]] = []        # one block can hold a heading, items and sub-items
            cur: Optional[Dict[str, Any]] = None
            for ln in lines:
                mclause = re.match(r"^(1\.\d+(?:\.\d+)+)\.?$", ln)
                mclause1 = re.match(r"^(1\.\d+(?:\.\d+)+)\.\s+(\S.*)$", ln)
                if mclause or mclause1:
                    cur = {"kind": "clause", "id": (mclause or mclause1).group(1), "text": mclause1.group(2) if mclause1 else ""}
                    records.append(cur)
                elif (re.match(r"^([a-z]\)|[ivx]{1,5}\.|\d{1,2}\.)$", ln) and cur is not None) or (re.match(r"^([a-z]\)|\d{1,2}\.)$", ln) and not records) or (re.match(r"^[ivx]{1,5}\.$", ln) and not records and clause_id):
                    cur = {"kind": "item", "id": ln, "text": ""}
                    records.append(cur)
                elif cur is None and not records and re.match(r"^(\d{1,2}\.|[a-z]\))\s+\S", ln) and clause_id == "":
                    mm = re.match(r"^(\d{1,2}\.|[a-z]\))\s+(\S.*)$", ln)
                    cur = {"kind": "item", "id": mm.group(1), "text": mm.group(2)}
                    records.append(cur)
                elif cur is not None:
                    cur["text"] = (cur["text"] + " " + ln).strip()
                else:
                    cur = {"kind": "text", "id": "", "text": ln}
                    records.append(cur)
            if not records:
                continue
            if records[0]["kind"] == "text":
                col = 1 if x0 >= 430 else 0
                prev = last_pos.get(pno)
                if (prev and prev[0] == col and 0 <= y0 - prev[1] <= 22 and abs(x0 - prev[2]) <= 16 and len(clean(text)) > 25
                        and not text.lower().startswith(("figure", "table"))):
                    # paragraph under the heading / item just read: same clause, same item
                    n = _note_row(len(notes) + 1, "design note (paragraph)", clause_id, clause_title, section, prev[3], pno, printed, clean(text), b)
                    notes.append(n)
                    last_pos[pno] = (col, y1, prev[2], prev[3])
                elif len(clean(text)) > 60 and clause_id and abs(x0 - clause_x) < 12 and not text.lower().startswith(("figure", "table")):
                    notes.append(_note_row(len(notes) + 1, "design note (clause text)", clause_id, clause_title, section, "", pno, printed, clean(text), b))
                    last_pos[pno] = (col, y1, x0, "")
                else:
                    unattached[pno] += 1
                continue
            for rec in records:
                if rec["kind"] == "clause":
                    cid = rec["id"]
                    if len(cid.split(".")) == 3:
                        section = cid + " " + rec["text"]
                        clause_id, clause_title = "", ""
                        letter_stack = roman_stack = ""
                        continue
                    clause_id, clause_title, clause_x = cid, rec["text"], x0
                    letter_stack = roman_stack = ""
                    notes.append(_note_row(len(notes) + 1, "design note (clause heading)", clause_id, clause_title, section, "", pno, printed, clause_title, b))
                    last_pos[pno] = (1 if x0 >= 430 else 0, y1, x0, "")
                elif rec["kind"] == "item" and rec["text"]:
                    marker = rec["id"]
                    if re.match(r"^[a-z]\)$", marker):
                        letter_stack, roman_stack, path = marker, "", marker
                    elif re.match(r"^[ivx]+\.$", marker):
                        roman_stack = marker
                        path = (letter_stack + " > " if letter_stack else "") + marker
                    else:
                        path = " > ".join(x for x in (letter_stack, roman_stack, marker) if x) if (letter_stack or roman_stack) else marker
                    notes.append(_note_row(len(notes) + 1, "design note (item)", clause_id, clause_title, section, path, pno, printed, rec["text"], b))
                    last_pos[pno] = (1 if x0 >= 430 else 0, y1, x0, path)
    return notes, dict(unattached)


def _note_row(n: int, kind: str, clause_id: str, clause_title: str, section: str, item: str, pno: int, printed: str,
              text: str, block: Sequence[Any]) -> Dict[str, Any]:
    row = {k: "" for k in NOTES_FIELDS}
    row.update({
        "note_id": "HFDS-N%04d" % n, "document": DOC_TITLE, "note_kind": kind, "section": section, "clause_id": clause_id,
        "clause_title": clause_title, "item": item, "pdf_page": pno, "printed_page": printed, "text_as_printed": text,
        "rule_tags": rule_tags(text + " " + clause_title),
        "report_categories": categories_for(text, clause_title),
        "location": "page %d block [%s]" % (pno, ", ".join("%.0f" % v for v in block[:4])),
        "method": METHOD_NOTE, "confidence": "high",
        "confidence_reason": "text layer block; order across columns by x position",
    })
    return row


def table_footnotes(doc: pymupdf.Document, tables: List[TableRaw]) -> List[Dict[str, Any]]:
    """Footnote blocks beside the tables: 'Notes:' lists, '*' lines and 'Abbreviation' lines."""
    out: List[Dict[str, Any]] = []
    seen_pages = set()
    for tr in tables:
        if tr.page_no in seen_pages:
            continue
        seen_pages.add(tr.page_no)
        page = doc[tr.page_no - 1]
        blocks = sorted([b for b in page.get_text("blocks") if b[6] == 0], key=lambda b: (round(b[1]), b[0]))
        i = 0
        while i < len(blocks):
            b = blocks[i]
            t = clean(b[4])
            if re.match(r"^(Notes?\s*:|Abbreviation|\*|Remarks\s*:)", t):
                text = t
                last = b
                j = i + 1
                if re.match(r"^Notes?\s*:?\s*$", t):
                    while j < len(blocks) and blocks[j][1] - last[3] < 14 and blocks[j][1] < 565 and not blocks[j][4].startswith("VOL"):
                        text += " " + clean(blocks[j][4])
                        last = blocks[j]
                        j += 1
                out.append({"pdf_page": tr.page_no, "printed": tr.printed, "table_id": tr.table_id, "text": text,
                            "block": last if j == i + 1 else b})
                i = j
                continue
            i += 1
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for f in out:
        key = (f["table_id"], f["text"])
        if key in merged:
            merged[key]["also_pages"].append(f["pdf_page"])
        else:
            f["also_pages"] = []
            merged[key] = f
    out = list(merged.values())
    rows = []
    for k, f in enumerate(out):
        row = {x: "" for x in NOTES_FIELDS}
        row.update({
            "document": DOC_TITLE, "note_kind": "table footnote", "source_table": f["table_id"], "pdf_page": f["pdf_page"],
            "printed_page": f["printed"], "text_as_printed": f["text"], "rule_tags": rule_tags(f["text"]),
            "report_categories": categories_for(f["text"]),
            "location": "page %d block [%s]%s" % (f["pdf_page"], ", ".join("%.0f" % v for v in f["block"][:4]),
                                                  ("; same text also on pages " + ", ".join(str(x) for x in f["also_pages"])) if f["also_pages"] else ""),
            "method": METHOD_NOTE, "confidence": "high", "confidence_reason": "text layer block below the table",
            "item": "",
        })
        rows.append(row)
    return rows


def config_notes(tables: List[TableRaw]) -> Tuple[List[Dict[str, Any]], List[Tuple[str, Dict[str, Any]]]]:
    rows: List[Dict[str, Any]] = []
    cfgs: List[Tuple[str, Dict[str, Any]]] = []
    for tr in tables:
        if tr.table_id not in ("T.1.1.3.3", "T.1.1.3.4"):
            continue
        for cfg in parse_config_table(tr):
            cfgs.append((tr.table_id, cfg))
            text = "Equipment: %s | LCP: %s | Electrical: %s | Control: %s | Areas: %s" % (
                cfg["equipment"], cfg["lcp"], cfg["electrical"], cfg["control"], cfg["areas_raw"])
            row = {x: "" for x in NOTES_FIELDS}
            row.update({
                "document": DOC_TITLE, "note_kind": "AHU/FCU configuration (%s)" % ("24-Hr areas" if tr.table_id.endswith(".3") else "12-Hr areas"),
                "source_table": tr.table_id, "clause_id": tr.table_id, "clause_title": tr.title,
                "item": cfg["config"], "pdf_page": tr.page_no, "printed_page": tr.printed, "text_as_printed": text,
                "config": cfg["config"], "system_type": cfg["system_type"], "standby_rule": cfg["standby"] or cfg["control"],
                "areas_listed": " | ".join(cfg["areas"]),
                "rule_tags": rule_tags(text), "report_categories": categories_for(cfg["areas_raw"]),
                "location": "page %d table %s column %d" % (tr.page_no, tr.table_id, cfg["col"] + 1),
                "method": METHOD_TABLE + " (configuration column read as one record)",
                "confidence": "high", "confidence_reason": "text layer, one column of the configuration table",
            })
            rows.append(row)
    return rows, cfgs


def annotation_notes(annots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for a in annots:
        if not a["content"]:
            kind = "reviewer markup (no text)"
        else:
            kind = "reviewer annotation"
        row = {x: "" for x in NOTES_FIELDS}
        row.update({
            "document": DOC_TITLE, "note_kind": kind, "item": a["kind"], "pdf_page": a["pdf_page"],
            "text_as_printed": a["content"], "rule_tags": "", "report_categories": categories_for(a["content"]),
            "location": "page %d annotation %s box %s author %s" % (a["pdf_page"], a["kind"], a["rect"], a["author"]),
            "method": METHOD_ANNOT, "confidence": "high",
            "confidence_reason": "reviewer comment/highlight; not a design rule; a Highlight carries the highlighted text",
        })
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def parse_pdf(pdf_path: Path, document: Optional[str] = None) -> Dict[str, Any]:
    doc = open_pdf(pdf_path)
    document = document or DOC_TITLE
    annots = collect_annotations(doc)
    tables, issues = extract_tables(doc)
    cfg_rows, configs = config_notes(tables)
    criteria: List[Dict[str, Any]] = []
    per_table: List[Dict[str, Any]] = []
    states: Dict[str, Dict[str, Any]] = {}
    for tr in tables:
        problems: List[str] = []
        if tr.table_id in ("T.1.1.3.3", "T.1.1.3.4"):
            rows: List[Dict[str, Any]] = []
            problems.append("configuration table: read as notes (hfds_notes.csv), not as area rows")
        elif tr.table_id == MV_TABLE_KEY:
            rows = rows_from_mv_table(tr, configs, document)
        elif tr.table_id in ("T.1.1.3.1", "T.1.1.3.2"):
            rows = rows_from_pairs_table(tr, configs, document)
        elif tr.table_id == "":
            rows, problems = [], ["table has no caption on its page; not parsed"]
        else:
            rows, p = rows_from_table(tr, configs, document, states.setdefault(tr.table_id, {}))
            problems += p
        criteria.extend(rows)
        per_table.append({"table_id": tr.table_id, "title": tr.title, "pdf_page": tr.page_no, "printed": tr.printed,
                          "rows": len(rows), "grid_rows": len(tr.rows), "problems": problems,
                          "uncovered_words": tr.uncovered_words})
    clause_rows, unattached = extract_clauses(doc, tables)
    foot_rows = table_footnotes(doc, tables)
    ann_rows = annotation_notes(annots)
    notes = clause_rows + foot_rows + cfg_rows + ann_rows
    for i, r in enumerate(notes, 1):
        r["note_id"] = "HFDS-N%04d" % i
    image_pages = {i + 1: len(p.get_images()) for i, p in enumerate(doc) if p.get_images()}
    texts = [p.get_text() for p in doc]
    hits = term_hits(texts, SEARCH_TERMS)
    return {"pages": len(doc), "criteria": criteria, "notes": notes, "per_table": per_table, "issues": issues,
            "unattached": unattached, "annots": annots, "image_pages": image_pages, "configs": configs, "term_hits": hits,
            "table_pages": sorted({t.page_no for t in tables})}


def cross_table_differences(criteria: List[Dict[str, Any]]) -> List[str]:
    """Same area name printed in two tables with different temperature, pressure, OA or total ACH (listed, not resolved)."""
    by: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in criteria:
        if r["table_kind"] in ("ACMV design criteria (main table)", "AC design condition") and r["area_name"]:
            by[norm_key(re.sub(r"\(.*?\)", "", r["area_name"]))].append(r)
    out = []
    for k, rows in sorted(by.items()):
        tabs = {r["table_id"] for r in rows}
        if len(tabs) < 2:
            continue
        sig = {(r["temp_as_printed"], r["pressure_regime"], r["oa_ach"], r["total_ach"]) for r in rows}
        if len(sig) > 1:
            desc = "; ".join("%s p%s: T=%s P=%s OA=%s tot=%s" % (r["table_id"], r["pdf_page"], r["temp_as_printed"] or "-",
                                                              r["pressure_regime"] or "-", r["oa_ach"] or "-", r["total_ach"] or "-") for r in rows)
            out.append("%s -> %s" % (rows[0]["area_name"], desc))
    return out


def select_rows(criteria: List[Dict[str, Any]], cat: str) -> List[Dict[str, Any]]:
    return [r for r in criteria if cat in r["report_categories"].split("; ")]


def fmt_row(r: Dict[str, Any]) -> str:
    bits = []
    if r["temp_as_printed"]:
        bits.append("T %s" % r["temp_as_printed"])
    if r["rh_as_printed"]:
        bits.append("RH %s" % r["rh_as_printed"])
    if r["pressure_as_printed"]:
        bits.append("P %s" % r["pressure_as_printed"])
    if r["oa_total_ach_as_printed"] or r["oa_ach_as_printed"] or r["total_ach_as_printed"]:
        bits.append("ACH %s" % (r["oa_total_ach_as_printed"] or (r["oa_ach_as_printed"] + " / " + r["total_ach_as_printed"])))
    if r["hours_as_printed"]:
        bits.append("hrs %s" % r["hours_as_printed"])
    if r["standby_config"]:
        bits.append("standby: %s" % r["standby_config"])
    if r["system_type"]:
        bits.append("type %s" % r["system_type"])
    if r["remarks_as_printed"]:
        bits.append("remarks: %s" % r["remarks_as_printed"][:80])
    return "%s %s p%s r%s `%s`: %s" % (r["table_id"], r["sub_item"] and (r["sub_item"] + ")") or "", r["pdf_page"], r["row_number"] or r["row_index"],
                                       r["area_name"][:70], "; ".join(bits))


CATEGORY_ORDER = [
    ("plant room", "Plant rooms"),
    ("AHU room", "AHU rooms"),
    ("electrical/transformer/switch", "Electrical, transformer and switch rooms"),
    ("LAN/IT/server/comms", "LAN, IT, server and comms rooms"),
    ("UPS/battery", "UPS and battery rooms"),
    ("riser", "Risers"),
    ("store", "Stores"),
    ("lift lobby", "Lift lobbies"),
    ("laboratory/innovation", "Laboratory and innovation-type rooms"),
]


SEARCH_TERMS = ["riser", "innovation", "lift lobb", "switch room", "battery", "UPS", "AHU room", "plant room", "LAN", "comms", "server",
                "transformer", "store", "laborator"]


def term_hits(doc_pages_text: List[str], terms: Sequence[str]) -> Dict[str, List[int]]:
    """Pages whose text layer contains each term (whole text, tables, figure labels and clauses alike)."""
    out: Dict[str, List[int]] = {}
    for t in terms:
        pat = re.compile(r"\b" + re.escape(t) if t.isupper() is False else r"\b" + re.escape(t) + r"\b", re.I if not t.isupper() else 0)
        out[t] = [i + 1 for i, tx in enumerate(doc_pages_text) if pat.search(tx)]
    return out


def build_report(res: Dict[str, Any], pdf_rel: str) -> str:
    crit, notes = res["criteria"], res["notes"]
    L: List[str] = []
    L.append("# HFDS Mechanical reverse-brief: parser report")
    L.append("")
    L.append("Source: `%s` (%d pages). Decisions D-036 and D-037. Method: text layer only (PyMuPDF `find_tables`, words in each cell box)."
             " No value was read from an image, so there is no image-read list for a second read." % (pdf_rel, res["pages"]))
    L.append("")
    L.append("## Counts")
    L.append("")
    kinds = Counter(r["table_id"] for r in crit)
    L.append("- criteria rows (`out/registers/hfds_criteria.csv`): **%d** from **%d** tables on **%d** pages" % (len(crit), len(res["per_table"]), len(res["table_pages"])))
    L.append("- notes rows (`out/registers/hfds_notes.csv`): **%d** (%s)" % (len(notes), ", ".join("%s %d" % (k, v) for k, v in sorted(Counter(n["note_kind"] if not n["note_kind"].startswith("AHU/FCU") else "AHU/FCU configuration" for n in notes).items()))))
    L.append("- criteria rows by confidence: %s" % ", ".join("%s %d" % (k, v) for k, v in sorted(Counter(r["confidence"] for r in crit).items())))
    L.append("- criteria rows by table: %s" % ", ".join("%s %d" % (k, v) for k, v in sorted(kinds.items())))
    L.append("- reviewer annotations recorded verbatim: %d (FreeText comments removed in memory before reading cells, because their text sits on the table cells)" % len(res["annots"]))
    L.append("")
    L.append("## Tables found, by page")
    L.append("")
    L.append("| PDF page | printed page | table | title | grid rows | criteria rows | words in table box not held by a cell |")
    L.append("|---|---|---|---|---|---|---|")
    for t in res["per_table"]:
        L.append("| %d | %s | %s | %s | %d | %d | %d |" % (t["pdf_page"], t["printed"], t["table_id"] or "(no caption)", t["title"][:70], t["grid_rows"], t["rows"], len(t["uncovered_words"])))
    L.append("")
    L.append("## Not parsed, and why")
    L.append("")
    for t in res["per_table"]:
        for p in t["problems"]:
            L.append("- page %d %s: %s" % (t["pdf_page"], t["table_id"], p))
        if t["uncovered_words"]:
            L.append("- page %d %s: %d word(s) inside the table box are held by no cell and are not in the register: `%s`" % (t["pdf_page"], t["table_id"], len(t["uncovered_words"]), " ".join(t["uncovered_words"])[:160]))
    for i in res["issues"]:
        L.append("- page %d: %s (%s)" % (i["pdf_page"], i["what"], i["why"]))
    notab = [p for p in range(1, res["pages"] + 1) if p not in res["table_pages"]]
    L.append("- pages with no table (clause text, figures, control schematics): read as clause text into the notes register where it is numbered; "
             "figure labels are not parsed. Pages with embedded images (figures, schematics, layouts): %s." % ", ".join(str(p) for p in sorted(res["image_pages"])))
    L.append("- text blocks on clause pages that are not a numbered clause or item (figure labels, call-outs, table-like text outside a grid): %d blocks on %d pages (not in the register)." % (sum(res["unattached"].values()), len(res["unattached"])))
    L.append("- 'Off Hrs' (printed in the Operating Hrs column of the main table) and 'Office Hrs' both occur; the abbreviation is kept as printed and is not resolved.")
    L.append("- the manual numbers two different tables T.1.1.3.14 (page 53 SOC APN RSP and page 59 MV design condition); both are kept, the MV one keyed `T.1.1.3.14 (MV)`.")
    L.append("- cells merged over several rows (Recirculation / Single pass, filter and standby columns of T.1.1.3.7 to T.1.1.3.11) are inherited by the rows they span and flagged `span_inherited`; the as-printed column of the spanned row stays as the table prints it.")
    L.append("- rows printed with a shared set of values for several sub-items (`a)` and `b)` in one row) are split into one row per sub-item and flagged `shared_row_split`.")
    L.append("")
    L.append("## Configuration tables T.1.1.3.3 and T.1.1.3.4 (AHU/FCU, 24-hour and 12-hour areas)")
    L.append("")
    for tid, cfg in res["configs"]:
        L.append("- %s %s: %s; standby rule: %s; areas: %s" % (tid, cfg["config"], cfg["system_type"] or "type not stated", (cfg["standby"] or cfg["control"])[:90], " | ".join(cfg["areas"])[:260]))
    L.append("")
    L.append("## Rows and notes that apply to the room types asked for")
    L.append("")
    for cat, title in CATEGORY_ORDER:
        rows = select_rows(crit, cat)
        nrows = [n for n in notes if cat in n["report_categories"].split("; ") and n["note_kind"] != "reviewer annotation"]
        L.append("### %s (%d criteria rows, %d notes)" % (title, len(rows), len(nrows)))
        L.append("")
        for tid, cfg in res["configs"]:
            for item in cfg["areas"]:
                if cat in categories_for(item).split("; "):
                    L.append("- listed in %s %s: `%s` -> %s; standby rule: %s" % (tid, cfg["config"], item, cfg["system_type"] or "type not stated", (cfg["standby"] or "")[:80]))
        for r in rows[:25]:
            L.append("- " + fmt_row(r))
        if len(rows) > 25:
            L.append("- ... %d more in the register (filter `report_categories`)" % (len(rows) - 25))
        for n in nrows[:12]:
            L.append("- note %s p%s %s %s: %s" % (n["note_id"], n["pdf_page"], n["clause_id"] or n["source_table"], n["item"], n["text_as_printed"][:170]))
        if len(nrows) > 12:
            L.append("- ... %d more in the notes register (filter `report_categories`)" % (len(nrows) - 12))
        L.append("")
    L.append("## Terms in the whole text layer (pages), for rooms the register has no row for")
    L.append("")
    for t, pages in res["term_hits"].items():
        L.append("- `%s`: %s" % (t, (", ".join(str(x) for x in pages) if pages else "not printed anywhere in the text layer")))
    L.append("")
    L.append("## Same room named in two tables with different figures (listed, not resolved)")
    L.append("")
    diffs = cross_table_differences(crit)
    for d in diffs[:60]:
        L.append("- " + d)
    if not diffs:
        L.append("- none")
    L.append("")
    L.append("## Reviewer annotations (not design rules)")
    L.append("")
    for a in res["annots"]:
        if a["content"] and a["kind"] in ("FreeText", "Text"):
            L.append("- page %d %s: %s" % (a["pdf_page"], a["kind"], a["content"][:230]))
    L.append("")
    return "\n".join(L)


def run(pdf: Optional[str] = None, quiet: bool = False) -> Dict[str, Any]:
    root = common.project_root()
    pth = common.resolve_paths()
    registers = pth.get("registers", root / "out" / "registers")
    qa = pth.get("qa", root / "out" / "qa")
    pdf_path = Path(pdf) if pdf else root / HFDS_REL
    if not pdf_path.is_absolute():
        pdf_path = root / pdf_path
    res = parse_pdf(pdf_path)
    common.write_csv_dicts(registers / "hfds_criteria.csv", res["criteria"], CRITERIA_FIELDS)
    common.write_csv_dicts(registers / "hfds_notes.csv", res["notes"], NOTES_FIELDS)
    rel = HFDS_REL if not pdf else str(pdf)
    common.atomic_write_text(qa / "hfds_report.md", build_report(res, rel))
    summary = {
        "pages": res["pages"], "tables": len(res["per_table"]), "criteria_rows": len(res["criteria"]),
        "notes_rows": len(res["notes"]), "annotations": len(res["annots"]),
        "criteria_by_table": dict(Counter(r["table_id"] for r in res["criteria"])),
    }
    if not quiet:
        print(json.dumps(summary, indent=2))
    return {**res, "summary": summary}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Parse the HFDS Mechanical reverse-brief (design criteria tables and notes).")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="write hfds_criteria.csv, hfds_notes.csv and the QA report")
    r.add_argument("--pdf", default=None, help="PDF path (default: %s)" % HFDS_REL)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            run(args.pdf)
            return 0
    except Exception as exc:  # noqa: BLE001
        print("error: %s" % exc, file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
