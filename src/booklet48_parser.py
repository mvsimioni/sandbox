"""Parser for the short DD booklets, Section 4.8 Mechanical Ventilation Strategy.

The short booklets (``data/source/Short Version DD Booklet/<DEPT>_Section_4.8_*.pdf``)
are A3 landscape extracts with a text layer.  This module

1. classes every page from its heading (sensor schedule, criteria, zoning
   keyplan, standby summary, pressure regime, other);
2. parses each DESIGN CRITERIA "Room conditions" table by column x-ranges
   derived from the header words (never by token order), joins wrapped cells by
   row band and strips U+200B;
3. parses the EQUIPMENT STANDBY CONFIGURATION SUMMARY the same way, carrying
   the "Level NN <wing>" heading onto each row;
4. records the text labels of the zoning keyplans and of the pressure regime
   drawings with their boxes (colours are not interpreted);
5. classes the room data table page ("D_Template Room Code ...", the BME temperature-monitoring
   schedule) and writes it row by row to ``booklet_room_data.csv``;
6. strips reviewer comments (PDF annotations) out of the text layer of table pages and records them
   as notes, so they cannot be read as table cells; free-text NOTE blocks on pressure regime pages
   are recorded verbatim in ``criteria_notes.csv`` with a ``note_kind``;
7. writes registers under ``out/registers`` and a QA report under ``out/qa``.

Values come from the text layer only (words with their boxes).  Every value
carries provenance (file, booklet page as printed, PDF page, bbox, confidence)
and the figure as printed is kept beside every normalised value.

Usage::

    python3 -m src.booklet48_parser run [--paths FILE_OR_DIR ...]

Nothing here touches the network.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pymupdf

from . import common

ZWSP = "​"
BOOKLET_DIR_NAME = "Short Version DD Booklet"

PAGE_CLASSES = ("sensor schedule", "criteria", "zoning keyplan", "standby summary",
                "pressure regime", "room data table", "other")

CRITERIA_FIELDS = [
    "department", "source_file", "booklet_page", "pdf_page", "table_part",
    "room_type_as_printed", "room_type_norm",
    "pressure_regime_as_printed", "pressure_pa", "pressure_pa_min", "pressure_pa_max", "pressure_direction",
    "min_oa_ach", "min_total_ach", "single_pass", "air_class", "filter",
    "rh_as_printed", "rh_max_pct", "rh_min_pct",
    "temp_as_printed", "temp_min_c", "temp_max_c",
    "afterhours", "setback", "note_refs", "bbox", "confidence", "raw_cells",
    "reviewer_markup", "reviewer_comments",
]
NOTES_FIELDS = ["file", "page", "note_number", "text", "booklet_page", "pdf_page", "bbox",
                "department", "note_kind", "author", "date"]
# note_kind: table note (numbered NOTE block under a criteria table) | room data table note |
#            pressure regime note | pandemic mode note (un-numbered NOTE block of a pressure regime page) |
#            reviewer annotation (a PDF comment on a criteria, room data or pressure regime page) |
#            reviewer markup (a PDF Line / Square / Polygon annotation with no text, e.g. a strike-through)
STANDBY_FIELDS = ["department", "source_file", "booklet_page", "pdf_page", "level_wing_heading",
                  "tag_as_written", "tag_normalised", "area", "configuration", "note_refs", "bbox", "confidence"]
STANDBY_NOTES_FIELDS = NOTES_FIELDS      # note_kind there: table note | reviewer annotation
ROOM_DATA_FIELDS = ["department", "source_file", "booklet_page", "pdf_page", "row",
                    "d_template_room_code", "d_room_name", "d_unique_room_identifier", "item_description",
                    "qty03_v12", "type_of_tms", "d_template_room_name", "other_columns",
                    "columns_as_printed", "bbox", "confidence"]
ZONING_FIELDS = ["department", "source_file", "booklet_page", "pdf_page", "keyplan_title",
                 "label_kind", "text", "block_text", "bbox", "has_colour_legend", "legend_source", "legend_bbox"]
PRESSURE_FIELDS = ["department", "source_file", "booklet_page", "pdf_page", "page_heading",
                   "label_kind", "text", "bbox", "annotation_author", "annotation_date"]
# label_kind values: text_label | pressure_value (drawing text) | reviewer_annotation (a PDF comment, not drawing text) |
#                    reviewer_markup (a PDF Line / Square / Polygon annotation with no text)

# header keyword -> criteria field
HEADER_KEYS: List[Tuple[str, str]] = [
    ("room", r"^room\b"),
    ("pressure", r"pressure"),
    ("min_oa", r"outdoor"),
    ("min_total", r"total"),
    ("single_pass", r"single"),
    ("air_class", r"^air\b"),
    ("filter", r"filter"),
    ("rh", r"humidity"),
    ("temp", r"temperatu"),
    ("afterhours", r"afterhours"),
    ("setback", r"setback"),
]
HEADER_VOCAB = {
    "room", "pressure", "regime", "pa", "min.", "outdoor", "ach", "total", "single", "pass",
    "air", "class", "filter", "relative", "humidity", "design", "temperature", "c", "afterhours",
    "mode", "y/n", "setback", ",", "regime,", "temperature,", "mode,", "pa,",
    "temperatu", "re,",      # ISO AH wraps the heading as 'Temperatu' / 're, C'
}
TAG_RE = re.compile(r"^[A-Z]{2,6}(?:-[A-Z0-9]+){2,}$")
FURNITURE_LINE_RE = re.compile(
    r"^(4\s+DEPARTMENT DRAWINGS|4\.8\s+MECHANICAL VENTILATION STRATEGY.*|`|"
    r"ALEXANDRA HOSPITAL REDEVELOPMENT PROJECT.*|BINDING GUTER.*)$", re.I)
PRESSURE_VALUE_RE = re.compile(r"^[+\-−–]?\s*\d+(?:\.\d+)?\s*(?:Pa)?$", re.I)


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #
def clean(text: str) -> str:
    """Strip U+200B (and other zero-width marks), turn NBSP into a space, collapse spaces."""
    text = re.sub("[​‌‍⁠﻿]", "", text or "")
    text = text.replace(" ", " ")
    return re.sub(r"\s+", " ", text).strip()


class Word:
    """One text-layer word with its box and font size."""

    __slots__ = ("x0", "y0", "x1", "y1", "text", "size")

    def __init__(self, x0: float, y0: float, x1: float, y1: float, text: str, size: float = 0.0):
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1
        self.text, self.size = text, size

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2.0

    def box(self) -> Tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Word(%r, %.0f,%.0f)" % (self.text, self.x0, self.y0)


def union_box(boxes: Iterable[Tuple[float, float, float, float]]) -> Tuple[float, float, float, float]:
    boxes = list(boxes)
    if not boxes:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def fmt_bbox(box: Tuple[float, float, float, float]) -> str:
    return ",".join("%.1f" % v for v in box)


def md(text: Any) -> str:
    """Escape a value for a Markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def page_words(page: "pymupdf.Page") -> List[Word]:
    """Words of a page built from characters: U+200B is dropped from text and boxes.

    A zero-width space that sits across a visible gap (more than 1.5 points between the
    neighbouring glyphs) also ends the word, so ``Room<ZWSP>Neutral`` stays two words.
    """
    out: List[Word] = []
    zero_width = "\u200b\u200c\u200d\u2060\ufeff"
    for block in page.get_text("rawdict").get("blocks", []):
        for line in block.get("lines", []):
            cur: List[Tuple[str, Tuple[float, float, float, float], float]] = []
            state = {"pending": False, "last_x1": None}

            def flush() -> None:
                if cur:
                    text = "".join(c for c, _, _ in cur)
                    box = union_box(b for _, b, _ in cur)
                    out.append(Word(box[0], box[1], box[2], box[3], text, cur[0][2]))
                    cur.clear()

            for span in line.get("spans", []):
                size = float(span.get("size", 0.0))
                for ch in span.get("chars", []):
                    c = ch["c"]
                    bbox = tuple(ch["bbox"])
                    if c in zero_width:
                        state["pending"] = True
                        continue
                    if c.isspace():
                        flush()
                        state["pending"] = False
                        state["last_x1"] = None
                        continue
                    if state["pending"] and cur and state["last_x1"] is not None \
                            and bbox[0] - state["last_x1"] > 1.5:
                        flush()
                    state["pending"] = False
                    cur.append((c, bbox, size))
                    state["last_x1"] = bbox[2]
            flush()
    return [w for w in out if clean(w.text)]


def cluster_lines(words: Sequence[Word], tol: float = 4.0) -> List[List[Word]]:
    """Group words into text lines by y-centre (greedy, tolerance ``tol`` points)."""
    lines: List[List[Word]] = []
    centres: List[float] = []
    for word in sorted(words, key=lambda w: (w.cy, w.x0)):
        if lines and abs(word.cy - centres[-1]) <= tol:
            lines[-1].append(word)
            centres[-1] = sum(w.cy for w in lines[-1]) / len(lines[-1])
        else:
            lines.append([word])
            centres.append(word.cy)
    return [sorted(line, key=lambda w: w.x0) for line in lines]


def join_words(words: Sequence[Word]) -> str:
    """Join words reading order: line by line, left to right."""
    return clean(" ".join(w.text for line in cluster_lines(words) for w in line))


def is_gutter(word: Word) -> bool:
    """The rotated 'BINDING GUTER' strip along the left edge."""
    return word.x1 < 50.0


MONTHS = ("JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST", "SEPTEMBER", "OCTOBER",
          "NOVEMBER", "DECEMBER")


def _follows_month(word: Word, words: Sequence[Word]) -> bool:
    """True when the word is the year in a footer date stamp such as 'MAY 2025'."""
    return any(o.text.upper() in MONTHS and abs(o.cy - word.cy) < 4 and 0 <= word.x0 - o.x1 < 12 for o in words)


STAMP_WORDS = {"ALEXANDRA", "HOSPITAL", "(AH)", "REDEVELOPMENT", "PROJECT", "|", "DETAILED", "DESIGN", "BOOKLET"}


def footer_furniture(words: Sequence[Word], page_h: float, page_w: float) -> List[Word]:
    """The words of the footer stamp ('ALEXANDRA HOSPITAL REDEVELOPMENT PROJECT | DETAILED DESIGN BOOKLET | MAY 2025')
    and the printed page number.

    A table that runs to the bottom of the page can share its last line with the stamp (Palliative PDF p1: the
    'Cleaner Room' row), so the footer cannot be cut off at a fixed fraction of the page height: the stamp and the page
    number are taken out by token instead and everything else stays in the body.  The stamp is the run of
    stamp words, a month and a year on the line of the word ALEXANDRA, in the same type size as that word.
    """
    out: List[Word] = []
    lows = [w for w in words if w.text == "ALEXANDRA" and w.y0 > page_h * 0.85 and w.x0 < page_w * 0.2]
    for alex in lows:
        for w in words:
            if abs(w.size - alex.size) > 0.6 or abs(w.cy - alex.cy) > 5.0 or w.x0 < alex.x0 - 1.0:
                continue
            if w.text in STAMP_WORDS or w.text.upper() in MONTHS or re.fullmatch(r"\d{4}", w.text) and _follows_month(w, words):
                out.append(w)
    # the printed page number: digits only, at the bottom right (well clear of the table's last column)
    out += [w for w in words if w.y0 > page_h * 0.93 and re.fullmatch(r"\d{1,4}", w.text) and w.x0 > page_w * 0.9]
    return out


def strip_furniture(words: List[Word], page_h: float, page_w: float) -> List[Word]:
    gone = {id(w) for w in footer_furniture(words, page_h, page_w)}
    return [w for w in words if id(w) not in gone]


def printed_page_number(words: Sequence[Word], page_h: float, page_w: float) -> str:
    """Booklet page as printed: the digits-only word in the bottom-right corner."""
    cands = [w for w in words if w.y0 > page_h * 0.93 and re.fullmatch(r"\d{1,4}", w.text) and w.x0 > page_w * 0.6]
    if not cands:
        # anywhere along the footer, but never the year of the 'MAY 2025' date stamp
        cands = [w for w in words if w.y0 > page_h * 0.93 and re.fullmatch(r"\d{1,4}", w.text)
                 and not _follows_month(w, words)]
    if not cands:
        return ""
    best = max(cands, key=lambda w: w.x0)
    # two different numbers printed over one another (Psych Med PDF p24: '136' under '13'): ambiguous, not guessed
    if any(o is not best and o.text != best.text and o.x0 < best.x1 and best.x0 < o.x1 and abs(o.cy - best.cy) < 4
           for o in cands):
        return ""
    return best.text


# --------------------------------------------------------------------------- #
# Page classification
# --------------------------------------------------------------------------- #
def large_heading(words: Sequence[Word], min_size: float = 26.0) -> str:
    """Text of the words set in the big heading type (28 pt on the booklet pages), read line by line."""
    return join_words([w for w in words if w.size >= min_size and not is_gutter(w)])


def section_strip(words: Sequence[Word]) -> str:
    """Section title in the strip at the top right ('4.8 MECHANICAL VENTILATION STRATEGY | DESIGN CRITERIA',
    '4.1.10 AV STRATEGY'), 20 pt type; the 28 pt line above it is 'DEPARTMENT DRAWINGS'."""
    return join_words([w for w in words if w.x0 >= 735 and w.y0 < 90 and 14 <= w.size < 26 and not is_gutter(w)])


def classify_page(text: str, heading: str = "", section: str = "") -> str:
    """Class a page from its heading text (whitespace-collapsed).

    ``heading`` is the big-type heading (see ``large_heading``).  A page whose heading reads
    PRESSURE REGIME is a pressure regime page even when a keyplan title is printed on the drawing.
    ``section`` is the section strip (see ``section_strip``): a page of another section of the booklet
    (4.1.10 AV STRATEGY, 4.1.11 COMMUNICATION STRATEGY, 4.9 IT STRATEGY) is ``other`` whatever it prints, so an
    AV overview that carries a storey KEYPLAN title is not read as an AHU zoning keyplan.
    """
    flat = re.sub(r"\s+", " ", clean(text))
    if section and not re.search(r"MECHANICAL VENTILATION", section, re.I):
        return "other"
    if re.search(r"D_Template\s*Room\s*Code", flat) and re.search(r"D_Unique\s*Room\s*Identifier", flat):
        return "room data table"
    if heading and re.search(r"PRESSURE REGIME", heading) and not re.search(r"DESIGN CRITERIA", flat):
        return "pressure regime"
    if re.search(r"DESIGN CRITERIA", flat) or re.search(r"Room conditions\s*[–\-]\s*Part", flat, re.I):
        return "criteria"
    if re.search(r"EQUIPMENT STANDBY CONFIGURATION SUMMARY", flat, re.I):
        return "standby summary"
    if re.search(r"KEYPLAN", flat) or re.search(r"AHU ZONING", flat):
        return "zoning keyplan"
    if re.search(r"PRESSURE REGIME", flat):
        return "pressure regime"
    if re.search(r"Room Temperature\s*Sensor", flat, re.I):
        return "sensor schedule"
    return "other"


# --------------------------------------------------------------------------- #
# Cell parsers (as printed -> normalised)
# --------------------------------------------------------------------------- #
def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else ("%g" % value)


def room_norm(text: str) -> str:
    """Normalised room type: lower case, tidy dashes, slashes and ampersands; a trailing '*' footnote mark is dropped."""
    t = re.sub(r"\s*\*+$", "", clean(text)).lower()
    t = re.sub(r"[‐-―−]", "-", t)
    t = re.sub(r"\s*/\s*", " / ", t)
    t = re.sub(r"\s*&\s*", " & ", t)
    t = re.sub(r"\s*,\s*", ", ", t)
    t = re.sub(r"\s*\(\s*", " (", t)
    t = re.sub(r"\s*\)", ")", t)
    t = re.sub(r"\s+", " ", t).strip(" /")
    return t


def parse_pressure(cell: str) -> Tuple[str, str, bool]:
    """Return ``(pressure_pa, direction, ok)``; Pa only when the cell gives a number."""
    t = clean(cell)
    low = t.lower()
    if t in ("", "-", "NR", "nr", "N/A"):
        return "", "", True
    direction = ""
    if "neg" in low:
        direction = "negative"
    elif "pos" in low:
        direction = "positive"
    elif "neutral" in low:
        direction = "neutral"
    m = re.search(r"([+\-−–]?)\s*(\d+(?:\.\d+)?)\s*(?:Pa\b)?", t)
    if m:
        value = float(m.group(2))
        sign = m.group(1)
        if sign in ("-", "−", "–"):
            value, direction = -value, direction or "negative"
        elif sign == "+":
            direction = direction or "positive"
        elif direction == "negative":
            value = -value
        elif value == 0:
            direction = direction or "neutral"
        elif not direction:
            return "", "", False  # unsigned number: direction cannot be told
        if value == 0:
            direction = direction or "neutral"
        return _num(value), direction, True
    return "", direction, bool(direction)


_NUM = r"[+\-−–]?\s*\d+(?:\.\d+)?"
_PA_RANGE_RE = re.compile(
    r"^(?P<a>%s)\s*(?:Pa\b)?\s*(?:to|~|–|—|−|-|/)\s*(?P<b>%s)\s*(?:Pa\b)?$" % (_NUM, _NUM), re.I)
_PA_RANGE_SIGNED_RE = re.compile(r"^(?P<a>%s)\s*(?:Pa\b)?\s+(?P<b>[+\-−–]\s*\d+(?:\.\d+)?)\s*(?:Pa\b)?$" % _NUM, re.I)
_WORD_DIRECTION = (("neg", "negative"), ("pos", "positive"), ("neutral", "neutral"))


def _signed(text: str) -> Optional[float]:
    m = re.fullmatch(r"([+\-−–]?)\s*(\d+(?:\.\d+)?)", text.strip())
    if not m:
        return None
    value = float(m.group(2))
    return -value if m.group(1) in ("-", "−", "–") else value


def _direction_of(lo: float, hi: float) -> str:
    if lo < 0 and hi < 0:
        return "negative"
    if lo > 0 and hi > 0:
        return "positive"
    if lo == 0 and hi == 0:
        return "neutral"
    return "mixed"


def parse_pressure_full(cell: str) -> Dict[str, Any]:
    """Pressure cell -> ``pa`` (single signed value), ``pa_min`` / ``pa_max`` (equal for a single value,
    the two ends of a range such as '-10 to -15'), ``direction`` and ``ok``.

    * a range gives a blank ``pa`` and both ends; the direction is derived from the signs
      (``mixed`` when the range crosses zero);
    * an unsigned number with no direction word cannot be given a sign and is reported (``ok`` False);
    * an unsigned range with a direction word takes its sign from that word ('Negative 10 to 15').
    """
    t = clean(cell)
    out: Dict[str, Any] = {"pa": "", "pa_min": "", "pa_max": "", "direction": "", "ok": True}
    low = t.lower()
    word_dir = next((d for k, d in _WORD_DIRECTION if k in low), "")
    core = re.sub(r"[A-Za-z]+(?:-[A-Za-z]+)*", lambda m: m.group(0) if m.group(0).lower() in ("to", "pa") else " ", t)
    core = re.sub(r"\s+", " ", core).strip()
    has_sep_word = bool(re.search(r"\bto\b|~", core, re.I))
    m = (_PA_RANGE_RE.match(core) or _PA_RANGE_SIGNED_RE.match(core)) if has_sep_word \
        else (_PA_RANGE_SIGNED_RE.match(core) or _PA_RANGE_RE.match(core))
    if m:
        a_txt, b_txt = m.group("a"), m.group("b")
        a, b = _signed(a_txt), _signed(b_txt)
        if a is not None and b is not None:
            unsigned = not re.match(r"[+\-−–]", a_txt.strip()) and not re.match(r"[+\-−–]", b_txt.strip())
            if unsigned and word_dir == "negative":
                a, b = -a, -b
            elif unsigned and word_dir != "positive":
                out.update(ok=False, direction=word_dir)      # 'Pa' range with no sign anywhere
                return out
            lo, hi = min(a, b), max(a, b)
            out.update(pa_min=_num(lo), pa_max=_num(hi), direction=_direction_of(lo, hi))
            return out
    pa, direction, ok = parse_pressure(cell)
    out.update(pa=pa, direction=direction, ok=ok)
    if pa != "":
        out.update(pa_min=pa, pa_max=pa)
    return out


def parse_count(cell: str) -> Tuple[str, bool]:
    """Numeric ACH cell: number -> text; '-', 'NR' or blank -> blank (valid)."""
    t = clean(cell)
    if t in ("", "-", "NR", "nr", "N/A", "–"):
        return "", True
    m = re.fullmatch(r"(\d+(?:\.\d+)?)", t)
    return (_num(float(m.group(1))), True) if m else ("", False)


def parse_single_pass(cell: str) -> Tuple[str, bool]:
    t = clean(cell).lower()
    if t in ("yes", "y"):
        return "Y", True
    if t in ("no", "n"):
        return "N", True
    if t in ("nr", "", "-"):
        return "NR", True
    return "", False


def parse_yn(cell: str) -> Tuple[str, bool]:
    t = clean(cell).lower()
    if t in ("y", "yes"):
        return "Y", True
    if t in ("n", "no"):
        return "N", True
    if t in ("nr", ""):
        return t.upper(), True
    return "", False


def parse_rh(cell: str) -> Tuple[str, str, bool]:
    """Return ``(rh_max, rh_min, ok)`` from e.g. 'Max 60%', '40 - 60%', 'NR'."""
    t = clean(cell)
    if t.upper() in ("", "NR", "-"):
        return "", "", True
    rng = re.fullmatch(r"(\d+(?:\.\d+)?)\s*%?\s*[–—\-]\s*(\d+(?:\.\d+)?)\s*%?", t)
    if rng:
        a, b = float(rng.group(1)), float(rng.group(2))
        return _num(max(a, b)), _num(min(a, b)), True
    m = re.fullmatch(r"(max|min)\.?\s*(\d+(?:\.\d+)?)\s*%?", t, re.I)
    if m:
        v = _num(float(m.group(2)))
        return (v, "", True) if m.group(1).lower() == "max" else ("", v, True)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*%", t)
    if m:
        return _num(float(m.group(1))), "", True
    return "", "", False


def parse_temp(cell: str) -> Tuple[str, str, bool]:
    """Return ``(min_c, max_c, ok)`` from '21 - 24', '27', '24+-1', 'NR'."""
    t = clean(cell)
    if t.upper() in ("", "NR", "-"):
        return "", "", True
    rng = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:°?\s*C)?\s*[–—\-]\s*(\d+(?:\.\d+)?)\s*(?:°?\s*C)?", t)
    if rng:
        a, b = float(rng.group(1)), float(rng.group(2))
        return _num(min(a, b)), _num(max(a, b)), True
    pm = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:±|\+/?-)\s*(\d+(?:\.\d+)?)", t)
    if pm:
        c, d = float(pm.group(1)), float(pm.group(2))
        return _num(c - d), _num(c + d), True
    single = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:°?\s*C)?", t)
    if single:
        v = _num(float(single.group(1)))
        return v, v, True
    return "", "", False


# --------------------------------------------------------------------------- #
# Criteria tables
# --------------------------------------------------------------------------- #
class TableColumns:
    """Column x-ranges derived from the header words."""

    def __init__(self, clusters: Dict[str, Tuple[float, float]], header_bottom: float,
                 header_refs: Dict[str, List[str]], centre_bounds: bool = False):
        self.clusters = clusters
        self.header_bottom = header_bottom
        self.header_refs = header_refs
        order = sorted(clusters.items(), key=lambda kv: kv[1][0])
        self.order = [k for k, _ in order]
        self.bounds: Dict[str, Tuple[float, float]] = {}
        for i, (key, (x0, x1)) in enumerate(order):
            if centre_bounds:   # headers centred in their cells: cell edges lie midway between centres
                c = (x0 + x1) / 2.0
                lo = (c + sum(order[i - 1][1]) / 2.0) / 2.0 if i > 0 else float("-inf")
                hi = (c + sum(order[i + 1][1]) / 2.0) / 2.0 if i + 1 < len(order) else float("inf")
            else:
                lo = (order[i - 1][1][1] + x0) / 2.0 if i > 0 else float("-inf")
                hi = (x1 + order[i + 1][1][0]) / 2.0 if i + 1 < len(order) else float("inf")
            self.bounds[key] = (lo, hi)

    def column_of(self, word: Word, left_key: Optional[str] = "room") -> str:
        """Column of a word.  With ``left_key`` the first (left-aligned) column takes every
        word that starts left of the second column's header; the rest go by x-centre."""
        if left_key and left_key in self.clusters and len(self.order) > 1:
            second = self.order[1]
            if word.x0 < self.clusters[second][0] - 4.0:
                return left_key
        for key in self.order:
            lo, hi = self.bounds[key]
            if lo <= word.cx < hi:
                return key
        return self.order[-1]


def _cluster_x(words: Sequence[Word], gap: float = 8.0) -> List[List[Word]]:
    """Merge words into header-cell clusters: x-extents overlap, or the words are
    neighbours on one text line less than ``gap`` points apart (a wrapped or spaced title)."""
    clusters: List[List[Word]] = []
    extents: List[List[float]] = []
    for word in sorted(words, key=lambda w: w.x0):
        joined = False
        if extents:
            overlap = word.x0 <= extents[-1][1] - 0.5
            same_line = word.x0 <= extents[-1][1] + gap and any(abs(word.cy - o.cy) <= 4.0 for o in clusters[-1])
            joined = overlap or same_line
        if joined:
            clusters[-1].append(word)
            extents[-1][1] = max(extents[-1][1], word.x1)
        else:
            clusters.append([word])
            extents.append([word.x0, word.x1])
    return clusters


def find_header(words: Sequence[Word]) -> Optional[TableColumns]:
    """Locate the criteria header (starts at the 'Room' word) and derive columns."""
    room_candidates = [w for w in words if w.text.lower() == "room" and not is_gutter(w)]
    all_lines = cluster_lines([w for w in words if not is_gutter(w)])

    def is_header_line(line: Sequence[Word]) -> bool:
        return all(w.text.lower() in HEADER_VOCAB or re.fullmatch(r"\d", w.text) for w in line)

    for room in sorted(room_candidates, key=lambda w: w.y0):
        idx = next((i for i, ln in enumerate(all_lines) if room in ln), None)
        if idx is None or not is_header_line(all_lines[idx]):
            continue
        lo = hi = idx       # header cells are centred, so lines above and below 'Room' belong to it
        while lo > 0 and is_header_line(all_lines[lo - 1]) and \
                abs(all_lines[lo][0].cy - all_lines[lo - 1][0].cy) <= 25:
            lo -= 1
        while hi + 1 < len(all_lines) and is_header_line(all_lines[hi + 1]) and \
                abs(all_lines[hi + 1][0].cy - all_lines[hi][0].cy) <= 25:
            hi += 1
        header = [w for ln in all_lines[lo:hi + 1] for w in ln]
        texts = " ".join(w.text.lower() for w in header)
        if "pressure" not in texts or "humidity" not in texts:
            continue
        # footnote superscripts in the header: digit-only words smaller than the header text
        sizes = [w.size for w in header if not re.fullmatch(r"\d", w.text) and w.size]
        text_size = max(sizes) if sizes else 0.0
        refs = [w for w in header if re.fullmatch(r"\d", w.text) and w.size and text_size and w.size < text_size * 0.9]
        body = [w for w in header if w not in refs and w.text not in (",",)]
        clusters = _cluster_x(body)
        mapping: Dict[str, Tuple[float, float]] = {}
        refs_by_col: Dict[str, List[str]] = defaultdict(list)
        for cl in clusters:
            text = join_words(cl).lower()
            for key, pat in HEADER_KEYS:
                if key not in mapping and re.search(pat, text):
                    mapping[key] = (min(w.x0 for w in cl), max(w.x1 for w in cl))
                    break
        # attach the footnote refs to the column whose x-range contains them
        for ref in refs:        # nearest header cell, within 15 points of its text
            dist = {key: max(x0 - ref.cx, ref.cx - x1, 0.0) for key, (x0, x1) in mapping.items()}
            if dist:
                key = min(dist, key=dist.get)  # type: ignore[arg-type]
                if dist[key] <= 15.0:
                    refs_by_col[key].append(ref.text)
        return TableColumns(mapping, max(w.y1 for w in header), dict(refs_by_col))
    return None


def _is_pressure_cell(text: str) -> bool:
    """True when the text reads as a pressure cell (a Pa figure or Neutral / Negative / Positive): a table row."""
    if not text:
        return False
    r = parse_pressure_full(text)
    return bool(r["ok"] and (r["pa"] or r["pa_min"] or r["direction"]))


def parse_criteria_page(words: List[Word], page_h: float, page_w: float,
                        rects: Optional[Sequence[Tuple[float, float, float, float]]] = None,
                        marks: Optional[Sequence[Dict[str, Any]]] = None
                        ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str, List[str]]:
    """Parse one criteria page.

    ``rects`` (the filled rectangles of the page) are optional.  When the Room column is drawn as one filled
    rectangle per table row, a line that lies inside a row rectangle belongs to that row whatever its
    distance from the row's value line (a filter cell wrapped over four lines, a room name centred on a tall
    row), and a room name alone in a row rectangle that holds no value line is a row of its own with blank cells.
    Without row rectangles, or where a rectangle holds two value lines, the nearest value line is used.

    ``marks`` are the reviewer marks of the page (see ``_row_marks``): a strike-through line over the room text, or a
    comment whose callout arrow ends inside the row.  They are carried on the row as ``marks``.

    Returns ``(rows, notes, table_part, page_failures)``.  Each row is a dict with the raw
    cells (``cells``), the room text, the box, the note references and ``mode`` (the
    section heading such as 'Hybrid Cooling' that precedes it).
    """
    failures: List[str] = []
    flat = join_words(words)
    part_m = re.search(r"Room conditions\s*[–—\-]\s*Part\s*(\d+)", flat, re.I)
    table_part = "Part %s" % part_m.group(1) if part_m else ""
    if not table_part and re.search(r"Room conditions", flat, re.I):
        table_part = "Room conditions (unnumbered)"     # a single table printed without 'Part N' (Dialysis)
    if not table_part:
        failures.append("table caption ('Room conditions') not found")
    cols = find_header(words)
    if cols is None:
        failures.append("criteria header not found")
        return [], [], table_part, failures
    missing = [k for k, _ in HEADER_KEYS if k not in cols.clusters]
    if missing:
        failures.append("header columns not found: %s" % ", ".join(missing))
        if "room" not in cols.clusters or "pressure" not in cols.clusters:
            return [], [], table_part, failures

    notes_top = find_notes_top(words, cols.header_bottom, page_h)
    footer_top = page_h       # the footer stamp and page number are already out of ``words`` (see footer_furniture)
    body = [w for w in words if w.y0 >= cols.header_bottom - 0.5 and w.y0 < min(notes_top, footer_top) - 0.5
            and not is_gutter(w)]
    # keep the body free of header words and of the 'Room conditions - Part N' caption
    body = [w for w in body if w.y0 >= cols.header_bottom - 0.5]
    caption_lines = [ln for ln in cluster_lines(body) if re.match(r"Room conditions", join_words(ln), re.I)]
    caption_ids = {id(w) for ln in caption_lines for w in ln}
    body = [w for w in body if id(w) not in caption_ids]

    lines = cluster_lines(body)
    # a footnote printed under the last row ('* One out of six wards ...'): a line that starts with an asterisk,
    # plus the room-column-only lines that continue it.  It is a note, not a table row.
    foot_notes: List[Dict[str, Any]] = []
    kept: List[List[Word]] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if re.match(r"^\*", join_words(ln)):
            texts, boxes = [join_words(ln)], [union_box(w.box() for w in ln)]
            j = i + 1
            while j < len(lines) and lines[j][0].cy - lines[i if j == i + 1 else j - 1][0].cy < 20 \
                    and not _is_pressure_cell(join_words([w for w in lines[j] if cols.column_of(w) == "pressure"])):
                texts.append(join_words(lines[j]))
                boxes.append(union_box(w.box() for w in lines[j]))
                j += 1
            foot_notes.append({"number": "*", "text": re.sub(r"^\*\s*", "", clean(" ".join(texts))),
                               "box": union_box(boxes)})
            i = j
            continue
        kept.append(ln)
        i += 1
    lines = kept
    anchors: List[Dict[str, Any]] = []
    room_only: List[List[Word]] = []
    others: List[List[Word]] = []
    for ln in lines:
        by_col: Dict[str, List[Word]] = defaultdict(list)
        for w in ln:
            by_col[cols.column_of(w)].append(w)
        if "pressure" in by_col:
            anchors.append({"cy": sum(w.cy for w in ln) / len(ln), "by_col": by_col, "words": list(ln), "extra": []})
        elif set(by_col) == {"room"}:
            room_only.append(ln)
        else:
            others.append(ln)

    bands: List[Tuple[float, float, float, float]] = []
    room_ws = [w for ln in lines for w in ln if cols.column_of(w) == "room"]
    if rects and room_ws:
        bands = _row_bands(rects, min(w.x0 for w in room_ws) + 2.0, cols.header_bottom, min(notes_top, footer_top))

    def band_of(cy: float) -> Optional[int]:
        return next((i for i, bd in enumerate(bands) if bd[1] - 1.0 <= cy <= bd[3] + 1.0), None)

    anchor_in_band: Dict[int, Dict[str, Any]] = {}
    if bands:
        for a in anchors:
            bi = band_of(a["cy"])
            if bi is None or bi in anchor_in_band:
                anchor_in_band = {}
                bands = []          # a rectangle that holds two value lines, or a value line outside every rectangle
                break
            anchor_in_band[bi] = a

    headings: List[Tuple[float, str, List[Word]]] = []
    orphans: List[str] = []
    for ln in others:
        cy = sum(w.cy for w in ln) / len(ln)
        near = min(anchors, key=lambda a: abs(a["cy"] - cy), default=None)
        has_room = any(cols.column_of(w) == "room" for w in ln)
        cols_used = {cols.column_of(w) for w in ln}
        bi = band_of(cy) if bands else None
        if bi is not None and bi in anchor_in_band:
            anchor_in_band[bi]["extra"].append(ln)      # inside the row rectangle of that value line
        elif near is not None and abs(near["cy"] - cy) <= 14.0:
            near["extra"].append(ln)            # wrapped data cell of that row
        elif has_room or len(cols_used) >= 3:
            by_col = defaultdict(list)
            for w in ln:
                by_col[cols.column_of(w)].append(w)
            anchors.append({"cy": cy, "by_col": by_col, "words": list(ln), "extra": [], "no_pressure": True})
        else:
            headings.append((cy, join_words(ln), ln))
    anchors.sort(key=lambda a: a["cy"])
    blank_rows: Dict[int, Dict[str, Any]] = {}
    for ln in room_only:
        cy = sum(w.cy for w in ln) / len(ln)
        near = min(anchors, key=lambda a: abs(a["cy"] - cy), default=None)
        bi = band_of(cy) if bands else None
        if bi is not None and bi in anchor_in_band:
            anchor_in_band[bi]["extra"].append(ln)
            continue
        if bi is not None:      # a row rectangle with a room name and no value line: a row with blank cells
            if bi not in blank_rows:
                blank_rows[bi] = {"cy": cy, "by_col": defaultdict(list), "words": [], "extra": [],
                                  "no_pressure": True, "blank_row": True}
            blank_rows[bi]["by_col"]["room"].extend(ln)
            blank_rows[bi]["words"].extend(ln)
            continue
        if near is None or abs(near["cy"] - cy) > 30.0:
            orphans.append(join_words(ln))
            continue
        near["extra"].append(ln)
    if blank_rows:
        anchors.extend(blank_rows.values())
        anchors.sort(key=lambda a: a["cy"])
    for text in orphans:
        failures.append("room text with no data row nearby: %r" % text)

    rows: List[Dict[str, Any]] = []
    pending_mode = ""
    heading_iter = sorted(headings, key=lambda h: h[0])
    hi = 0
    for anchor in anchors:
        while hi < len(heading_iter) and heading_iter[hi][0] < anchor["cy"]:
            pending_mode = heading_iter[hi][1]   # a section row holds until the next one
            hi += 1
        cells_words: Dict[str, List[Word]] = defaultdict(list)
        for col, ws in anchor["by_col"].items():
            cells_words[col].extend(ws)
        all_words = list(anchor["words"])
        for ln in anchor["extra"]:
            for w in ln:
                cells_words[cols.column_of(w)].append(w)
                all_words.append(w)
        cells: Dict[str, str] = {}
        refs: List[str] = []
        for col, ws in cells_words.items():
            sizes = [w.size for w in ws if w.size]
            ref_ws = []
            if col == "room" and sizes:
                big = max(sizes)
                ref_ws = [w for w in ws if re.fullmatch(r"\d", w.text) and w.size and w.size < big * 0.8
                          and w is ws[-1]]
            refs.extend(w.text for w in ref_ws)
            cells[col] = join_words([w for w in ws if w not in ref_ws])
            if col == "room" and re.search(r"\*$", cells[col]):
                refs.append("*")          # '*' footnote reference printed after the room name
        for col, col_refs in cols.header_refs.items():
            refs.extend(col_refs)
        row_box = union_box(w.box() for w in all_words)
        rows.append({
            "cells": cells, "box": row_box, "marks": _row_marks(row_box, cells_words.get("room", []), marks or []),
            "note_refs": sorted(set(refs), key=lambda s: (len(s), s)),
            "mode": pending_mode, "no_pressure": bool(anchor.get("no_pressure")),
            "blank_row": bool(anchor.get("blank_row")),
            "pressure_words": [w for w in anchor["by_col"].get("pressure", [])],
        })
    notes = foot_notes + parse_notes(words, notes_top, footer_top)
    return rows, notes, table_part, failures


def _row_marks(box: Tuple[float, float, float, float], room_words: Sequence[Word],
               marks: Sequence[Dict[str, Any]], pad: float = 6.0) -> List[Dict[str, Any]]:
    """Reviewer marks that belong to one table row.

    * a ``strike`` (a thin horizontal Line or a StrikeOut with no text) belongs to the row whose room text it crosses;
    * a ``comment`` (a FreeText callout) belongs to the row when the tip of its callout arrow lies inside the row box
      (``pad`` points of slack); a comment without a callout arrow is not linked to any row.
    """
    out: List[Dict[str, Any]] = []
    for m in marks:
        if m["kind"] == "strike":
            if any(w.y0 - 1.0 <= m["y"] <= w.y1 + 1.0 and m["x0"] < w.x1 and w.x0 < m["x1"] for w in room_words):
                out.append(m)
        elif m.get("tip") and box[0] - pad <= m["tip"][0] <= box[2] + pad and box[1] - pad <= m["tip"][1] <= box[3] + pad:
            out.append(m)
    return out


def find_notes_top(words: Sequence[Word], below_y: float, page_h: float) -> float:
    """Top of the NOTE block: the first line below ``below_y`` that starts with the capitalised
    marker NOTE or NOTE:- (a data cell reading 'note' does not start the block)."""
    for line in cluster_lines([w for w in words if w.y0 > below_y and not is_gutter(w)]):
        if re.fullmatch(r"NOTES?:?-?", line[0].text) and len(line) <= 2:
            return min(w.y0 for w in line)
    return page_h


def parse_notes(words: Sequence[Word], notes_top: float, footer_top: float
                ) -> List[Dict[str, Any]]:
    """Notes block below a table: '1 - TEXT ...' with wrapped continuation lines."""
    region = [w for w in words if w.y0 >= notes_top - 0.5 and w.y0 < footer_top and not is_gutter(w)]
    notes: List[Dict[str, Any]] = []
    for ln in cluster_lines(region):
        text = join_words(ln)
        if re.fullmatch(r"NOTES?:?-?", text):
            continue
        m = re.match(r"^(\d+)\s*[–—\-.)]\s*(.*)$", text)
        if m:
            notes.append({"number": m.group(1), "text": m.group(2).strip(), "boxes": [union_box(w.box() for w in ln)]})
        elif notes:
            notes[-1]["text"] = (notes[-1]["text"] + " " + text).strip()
            notes[-1]["boxes"].append(union_box(w.box() for w in ln))
    for n in notes:
        n["box"] = union_box(n.pop("boxes"))
    return notes


def parse_block_note(words: Sequence[Word], notes_top: float, footer_top: float) -> List[Dict[str, Any]]:
    """NOTE block of a page whose note is not numbered ('NOTE:- AH INFECTION CONTROL TO CONFIRM ...'):
    one note holding every line below the marker, verbatim and joined in reading order.  A block whose lines
    are numbered is returned as numbered notes."""
    numbered = parse_notes(words, notes_top, footer_top)
    if numbered:
        return numbered
    region = [w for w in words if w.y0 >= notes_top - 0.5 and w.y0 < footer_top and not is_gutter(w)]
    lines = [ln for ln in cluster_lines(region) if not re.fullmatch(r"NOTES?:?-?", join_words(ln))]
    if not lines:
        return []
    text = join_words([w for ln in lines for w in ln])
    box = union_box(w.box() for ln in lines for w in ln)
    return [{"number": "", "text": re.sub(r"^NOTES?:?-?\s*", "", text), "box": box}]


def build_criteria_rows(rows: List[Dict[str, Any]], meta: Dict[str, Any], table_part: str,
                        failures: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Turn raw rows into criteria.csv rows, normalising each cell and recording failed fields."""
    out: List[Dict[str, Any]] = []
    for row in rows:
        c = row["cells"]
        failed: List[str] = []
        room = c.get("room", "")
        pr = parse_pressure_full(c.get("pressure", ""))
        pa, direction = pr["pa"], pr["direction"]
        if not pr["ok"]:
            failed.append("pressure")
        oa, ok = parse_count(c.get("min_oa", ""))
        if not ok:
            failed.append("min_oa")
        tot, ok = parse_count(c.get("min_total", ""))
        if not ok:
            failed.append("min_total")
        sp, ok = parse_single_pass(c.get("single_pass", ""))
        if not ok:
            failed.append("single_pass")
        rh_max, rh_min, ok = parse_rh(c.get("rh", ""))
        if not ok:
            failed.append("rh")
        t_min, t_max, ok = parse_temp(c.get("temp", ""))
        if not ok:
            failed.append("temp")
        ah, ok = parse_yn(c.get("afterhours", ""))
        if not ok:
            failed.append("afterhours")
        sb, ok = parse_yn(c.get("setback", ""))
        if not ok:
            failed.append("setback")
        if row.get("blank_row"):            # a room name in a row with every value cell empty: nothing to normalise
            sp = ah = sb = ""
        if not room:
            failed.append("room")
        if row.get("no_pressure"):
            failed.append("pressure (cell empty)")
        part = table_part + ("; %s" % row["mode"] if row["mode"] else "")
        confidence = max(0.1, 0.97 - 0.25 * len(failed) - (0.05 if row["mode"] else 0.0))
        strikes = [m for m in row.get("marks", []) if m["kind"] == "strike"]
        comments = [m for m in row.get("marks", []) if m["kind"] == "comment"]
        if strikes:       # the values are read as printed, but the reviewer has struck the row out: not current
            confidence = min(confidence, 0.50)
        for field in failed:
            failures.append({"file": meta["source_file"], "pdf_page": meta["pdf_page"],
                             "booklet_page": meta["booklet_page"], "row": room or "(no room text)",
                             "field": field, "cell": c.get(field.split(" ")[0], ""),
                             "bbox": fmt_bbox(row["box"])})
        out.append({
            "department": meta["department"], "source_file": meta["source_file"],
            "booklet_page": meta["booklet_page"], "pdf_page": meta["pdf_page"], "table_part": part,
            "room_type_as_printed": room, "room_type_norm": room_norm(room),
            "pressure_regime_as_printed": c.get("pressure", ""), "pressure_pa": pa,
            "pressure_pa_min": pr["pa_min"], "pressure_pa_max": pr["pa_max"],
            "pressure_direction": direction,
            "min_oa_ach": oa, "min_total_ach": tot, "single_pass": sp,
            "air_class": c.get("air_class", ""), "filter": c.get("filter", ""),
            "rh_as_printed": c.get("rh", ""), "rh_max_pct": rh_max, "rh_min_pct": rh_min,
            "temp_as_printed": c.get("temp", ""), "temp_min_c": t_min, "temp_max_c": t_max,
            "afterhours": ah, "setback": sb, "note_refs": ";".join(row["note_refs"]),
            "bbox": fmt_bbox(row["box"]), "confidence": "%.2f" % confidence,
            "reviewer_markup": "; ".join("struck through by a reviewer %s annotation%s" % (
                m["type"], " (%s)" % m["author"] if m["author"] else "") for m in strikes),
            "reviewer_comments": " || ".join(m["text"] for m in comments),
            "raw_cells": json.dumps({k: c.get(k, "") for k in
                                     ("min_oa", "min_total", "single_pass", "afterhours", "setback")},
                                    ensure_ascii=False),
        })
    return out


# --------------------------------------------------------------------------- #
# Room data table (the BME temperature-monitoring schedule: D_Template Room Code ...)
# --------------------------------------------------------------------------- #
ROOM_DATA_KEYS: List[Tuple[str, str]] = [
    ("d_template_room_code", r"template\s*room\s*code"),
    ("d_unique_room_identifier", r"unique\s*room\s*identifier"),
    ("d_template_room_name", r"template\s*room\s*name"),
    ("d_room_name", r"^d_?\s*room\s*name"),
    ("item_description", r"item\s*description"),
    ("qty03_v12", r"^qty"),
    ("type_of_tms", r"type\s*of\s*tms"),
]


def room_data_key(text: str) -> str:
    """Field name for a room data table heading as printed ('' when it is not a known heading)."""
    flat = clean(text).lower()
    for key, pat in ROOM_DATA_KEYS:
        if re.search(pat, flat):
            return key
    return ""


def parse_room_data_page(words: List[Word], page_h: float,
                         rects: Sequence[Tuple[float, float, float, float]],
                         rules: Sequence[Tuple[float, float, float]]
                         ) -> Tuple[List[Dict[str, Any]], Dict[str, str], List[str]]:
    """Parse the room data table: one row per room-and-item, every column as printed.

    ``rects`` are the filled rectangles of the page (the header cells are filled), ``rules`` the horizontal
    rule segments ``(y, x0, x1)`` that separate the rows.  Returns ``(rows, headings, failures)`` where each row is
    ``{"cells": {heading_key_or_text: cell}, "box": bbox}`` and ``headings`` maps each column key (or the heading
    text for an unknown column) to the heading as printed.
    """
    failures: List[str] = []
    anchors = [w for w in words if w.text.startswith("D_") and not is_gutter(w)]
    if not anchors:
        return [], {}, ["room data header (D_Template Room Code ...) not found"]
    first = min(anchors, key=lambda w: (round(w.cy), w.x0))
    # header cells are filled rectangles that contain the heading line
    hdr_rects = sorted([r for r in rects if r[0] > 50 and 12 <= r[3] - r[1] <= 70 and r[2] - r[0] >= 20
                        and r[1] - 1 <= first.cy <= r[3] + 1], key=lambda r: r[0])
    if len(hdr_rects) >= 3:
        header_bottom = max(r[3] for r in hdr_rects)
        spans = [(r[0], r[2]) for r in hdr_rects]
    else:
        lines = cluster_lines([w for w in words if first.y0 - 3 <= w.y0 <= first.y0 + 30 and not is_gutter(w)])
        header_bottom = max(w.y1 for ln in lines[:2] for w in ln)
        hw = [w for ln in lines[:2] for w in ln]
        spans = [(min(w.x0 for w in cl), max(w.x1 for w in cl)) for cl in _cluster_x(hw, gap=5.0)]
    header_words = [w for w in words if w.y1 <= header_bottom + 1.0 and w.y0 >= first.y0 - 3 and not is_gutter(w)]
    headings: Dict[str, str] = {}
    col_spans: Dict[str, Tuple[float, float]] = {}
    for x0, x1 in spans:
        cell = [w for w in header_words if x0 - 1 <= w.cx <= x1 + 1]
        text = join_words(cell)
        if not text:
            continue
        key = room_data_key(text) or re.sub(r"\W+", "_", text.lower()).strip("_")
        if key in headings:
            key = key + "_2"
        headings[key] = text
        col_spans[key] = (x0, x1)
    if not col_spans:
        return [], {}, ["room data header cells not derived"]
    order = sorted(col_spans, key=lambda k: col_spans[k][0])
    table_x0, table_x1 = col_spans[order[0]][0], col_spans[order[-1]][1]
    notes_top = find_notes_top(words, header_bottom, page_h)
    limit = min(notes_top, page_h)
    body = [w for w in words if w.y0 >= header_bottom - 0.5 and w.y0 < limit - 0.5 and not is_gutter(w)
            and table_x0 - 2 <= w.cx <= table_x1 + 2]
    edges = sorted({round(y, 1) for y, x0, x1 in rules
                    if header_bottom - 1 < y < limit and x1 - x0 >= 0.6 * (table_x1 - table_x0)})
    if len(edges) >= 1:
        bounds = [header_bottom] + edges
        if body and max(w.y0 for w in body) > bounds[-1] + 3:
            bounds.append(limit)
    else:
        # no ruled rows: anchor on the lines that carry a quantity and cut midway between them
        qty = col_spans.get("qty03_v12")
        anchor_y = sorted(w.cy for w in body if qty and qty[0] <= w.cx <= qty[1] and re.fullmatch(r"\d+", w.text))
        bounds = [header_bottom] + [(a + b) / 2.0 for a, b in zip(anchor_y, anchor_y[1:])] + [limit]
        failures.append("no row rules found; rows cut midway between the quantity cells")
    rows: List[Dict[str, Any]] = []
    for top, bottom in zip(bounds, bounds[1:]):
        ws = [w for w in body if top - 0.5 <= w.cy < bottom + 0.5]
        if not ws:
            continue
        by_key: Dict[str, List[Word]] = {k: [] for k in order}
        for w in ws:     # nearest header cell by x (0 when the word centre is inside it)
            key = min(order, key=lambda k: max(col_spans[k][0] - w.cx, w.cx - col_spans[k][1], 0.0))
            by_key[key].append(w)
        cells = {k: join_words(v) for k, v in by_key.items()}
        rows.append({"cells": cells, "box": union_box(w.box() for w in ws)})
    return rows, headings, failures


# --------------------------------------------------------------------------- #
# Standby summary
# --------------------------------------------------------------------------- #
def normalise_tag(tag: str) -> str:
    t = clean(tag).upper()
    t = re.sub(r"[‐-―−]", "-", t)
    t = re.sub(r"\s*-\s*", "-", t)
    return re.sub(r"\s+", "", t)


def standby_notes(words: List[Word], page_h: float) -> Tuple[str, List[Dict[str, Any]]]:
    """Note references printed on the Configuration header ('1,2') and the NOTE block of a standby page."""
    refs = ""
    conf = [w for w in words if w.text.startswith("Configuration") and not is_gutter(w)]
    if conf:
        head = min(conf, key=lambda w: w.y0)
        for w in words:
            if re.fullmatch(r"\d+(?:,\d+)*", w.text) and w.x0 >= head.x1 - 2 and abs(w.cy - head.cy) <= 14 \
                    and not is_gutter(w):
                refs = w.text
                break
        top = find_notes_top(words, head.y1, page_h)
    else:
        top = page_h
    return refs, parse_notes(words, top, page_h)


def _row_bands(rects: Sequence[Tuple[float, float, float, float]], cx: float, top: float, bottom: float
               ) -> List[Tuple[float, float, float, float]]:
    """Row rectangles of one column: filled rectangles holding x = ``cx``, between ``top`` and ``bottom``,
    taller than a text line, innermost only, in reading order."""
    cand = [r for r in rects if r[0] - 1 <= cx <= r[2] + 1 and r[3] - r[1] > 8 and r[2] - r[0] < 700
            and r[1] >= top - 1 and r[3] <= bottom + 1]
    keep = [r for r in cand if not any(o is not r and o[0] >= r[0] - 1 and o[2] <= r[2] + 1
                                       and o[1] >= r[1] - 1 and o[3] <= r[3] + 1
                                       and (o[2] - o[0]) * (o[3] - o[1]) < (r[2] - r[0]) * (r[3] - r[1])
                                       for o in cand)]
    out: List[Tuple[float, float, float, float]] = []
    for r in sorted(keep, key=lambda r: (r[1], r[0])):
        if not out or abs(out[-1][1] - r[1]) > 1.0:
            out.append(r)
    return out


def parse_standby_page(words: List[Word], page_h: float, carry_heading: str,
                       rects: Optional[Sequence[Tuple[float, float, float, float]]] = None
                       ) -> Tuple[List[Dict[str, Any]], str, List[str]]:
    """Parse one standby-summary page.

    The 'Level NN <wing>' heading is a merged cell in the left-hand column, vertically
    centred on its group of rows (not on the first row).  A row belongs to the heading
    whose merged-cell rectangle (``rects``, the filled rectangles of the page) contains
    it; without rectangles the nearest heading within 60 points is used, and a row with
    none takes ``carry_heading`` (the last heading of the previous page).

    Where the Configuration column is drawn as one filled rectangle per row, those rectangles are the
    rows: every word whose centre lies inside one belongs to that row, so an Area cell that is centred
    on its tag line (wrapped above and below it) or a Configuration cell set a few points off the tag
    line is read correctly.  A table with no Equipment Tag column (Palliative) is read the same way,
    with a blank tag.  Without row rectangles the earlier line-by-line reading is used.
    """
    failures: List[str] = []
    tag_w = [w for w in words if w.text == "Tag" and not is_gutter(w)]
    area_w = [w for w in words if w.text == "Area" and not is_gutter(w)]
    if not area_w:
        return [], carry_heading, ["standby header (Equipment Tag / Area) not found"]
    tagless = not tag_w
    tag_h = min(area_w if tagless else tag_w, key=lambda w: w.y0)
    near_header = [w for w in words if tag_h.y0 - 20 <= w.y0 < tag_h.y0 + 40 and not is_gutter(w)]
    conf_w = [w for w in near_header if w.text.lower().startswith("configuration")]
    header_bottom = max([tag_h.y1] + [w.y1 for w in conf_w])
    header_words = [w for w in near_header if w.y1 <= header_bottom + 1.0]
    sizes = [w.size for w in header_words if w.size and not re.fullmatch(r"[\d,]+", w.text)]
    text_size = max(sizes) if sizes else 0.0
    body_hdr = [w for w in header_words
                if not (re.fullmatch(r"[\d,]+", w.text) and w.size and text_size and w.size < text_size * 0.9)]
    mapping: Dict[str, Tuple[float, float]] = {}
    for cl in _cluster_x(body_hdr):
        text = join_words(cl).lower()
        ext = (min(w.x0 for w in cl), max(w.x1 for w in cl))
        if "tag" in text and "tag" not in mapping:
            mapping["tag"] = ext
        elif re.search(r"\barea\b", text) and "area" not in mapping:
            mapping["area"] = ext
        elif "standby" in text or "configuration" in text:
            mapping.setdefault("config", ext)
    need = {"area", "config"} if tagless else {"tag", "area", "config"}
    if set(mapping) != need:
        return [], carry_heading, ["standby header columns not derived (got %s)" % sorted(mapping)]
    cols = TableColumns(mapping, header_bottom, {}, centre_bounds=True)
    notes_top = find_notes_top(words, header_bottom, page_h)
    body_bottom = min(notes_top, page_h)
    body = [w for w in words if w.y0 > header_bottom - 0.5 and w.y0 < body_bottom - 0.5
            and not is_gutter(w)]
    config_cx = (mapping["config"][0] + mapping["config"][1]) / 2.0
    area_cx = (mapping["area"][0] + mapping["area"][1]) / 2.0
    bands = _row_bands(rects or [], config_cx, header_bottom, body_bottom)
    tag_like = [w for w in body if TAG_RE.match(w.text)]
    if tagless:
        if not bands:
            return [], carry_heading, ["standby table has no Equipment Tag column and no row rectangles"]
        area_cols = _row_bands(rects or [], area_cx, header_bottom, body_bottom)
        level_edge = (min(r[0] for r in area_cols) if area_cols else mapping["area"][0] - 10.0) - 2.0
    else:
        if not tag_like:
            return [], carry_heading, ["no equipment tags found below the standby header"]
        level_edge = min(w.x0 for w in tag_like) - 2.0
    level_words = [w for w in body if w.x1 <= level_edge]
    data_words = [w for w in body if w.x1 > level_edge]

    headings: List[Dict[str, Any]] = []
    for ln in cluster_lines(level_words, tol=5.0):
        text = join_words(ln)
        box = union_box(w.box() for w in ln)
        rect = None
        for r in (rects or []):
            if r[0] - 1 <= (box[0] + box[2]) / 2 <= r[2] + 1 and r[1] - 1 <= (box[1] + box[3]) / 2 <= r[3] + 1 \
                    and (r[3] - r[1]) > 0 and r[0] < level_edge:
                if rect is None or (r[2] - r[0]) * (r[3] - r[1]) < (rect[2] - rect[0]) * (rect[3] - rect[1]):
                    rect = r
        headings.append({"text": text, "cy": (box[1] + box[3]) / 2, "rect": rect})
    merged: List[Dict[str, Any]] = []
    for h in headings:      # a heading wrapped over two lines inside one merged cell ('Level 15' / 'NW wing')
        prev = merged[-1] if merged else None
        if prev is not None and h["rect"] is not None and prev["rect"] == h["rect"]:
            prev["text"] = clean(prev["text"] + " " + h["text"])
            prev["cy"] = (prev["cy"] + h["cy"]) / 2.0
        else:
            merged.append(h)
    headings = merged

    rows: List[Dict[str, Any]] = []
    banded = False
    if bands:
        built: List[Dict[str, Any]] = []
        ok = True
        for band in bands:
            ws = [w for w in data_words if band[1] - 1.0 <= w.cy <= band[3] + 1.0]
            if not ws:
                continue
            tags = [w for w in ws if TAG_RE.match(w.text) and w.cx < band[0] - 0.5]   # 'AHU-CONFIG-E' looks like a tag
            if len(tags) > 1 or (not tagless and not tags):
                ok = False
                break
            rest = [w for w in ws if not (tags and w is tags[0])]
            built.append({"cells": {"tag": tags[0].text if tags else "",
                                    "area": join_words([w for w in rest if w.cx < band[0] - 0.5]),
                                    "config": join_words([w for w in rest if w.cx >= band[0] - 0.5])},
                          "cy": (band[1] + band[3]) / 2.0, "boxes": [w.box() for w in ws]})
        used = sum(len(r["boxes"]) for r in built)
        n_tags = sum(1 for w in tag_like if w.cx < min(r[0] for r in bands) - 0.5)
        if ok and built and (tagless or len(built) == n_tags) and used == len(data_words):
            rows, banded = built, True
    if not banded:
        if tagless:
            return [], carry_heading, ["standby rows not matched to row rectangles"]
        for ln in cluster_lines(data_words):
            by_col: Dict[str, List[Word]] = defaultdict(list)
            for w in ln:
                by_col[cols.column_of(w, left_key=None)].append(w)
            cell = {k: join_words(v) for k, v in by_col.items()}
            cy = sum(w.cy for w in ln) / len(ln)
            if not cell.get("tag") and rows and (cell.get("area") or cell.get("config")):
                prev = rows[-1]      # wrapped cell continuing the previous row
                for k, v in cell.items():
                    prev["cells"][k] = clean(prev["cells"].get(k, "") + " " + v)
                prev["boxes"].extend(w.box() for w in ln)
                continue
            if not cell.get("tag"):
                failures.append("standby line without a tag: %r" % join_words(ln))
                continue
            rows.append({"cells": cell, "cy": cy, "boxes": [w.box() for w in ln]})

    last_heading = carry_heading
    for r in rows:
        chosen = None
        for h in headings:
            if h["rect"] is not None and h["rect"][1] - 1 <= r["cy"] <= h["rect"][3] + 1:
                chosen = h
                break
        if chosen is None:
            cand = [h for h in headings if h["rect"] is None]
            if cand:
                best = min(cand, key=lambda h: abs(h["cy"] - r["cy"]))
                if abs(best["cy"] - r["cy"]) <= 60.0:
                    chosen = best
        r["heading"] = chosen["text"] if chosen else carry_heading
        if chosen:
            last_heading = chosen["text"]
        r["box"] = union_box(r.pop("boxes"))
    if headings:
        last_heading = headings[-1]["text"]
    return rows, last_heading, failures


# --------------------------------------------------------------------------- #
# Zoning keyplans and pressure drawings
# --------------------------------------------------------------------------- #
def page_lines(page: "pymupdf.Page") -> List[Dict[str, Any]]:
    """Text lines (spans joined) with box and the text of their block, furniture removed."""
    out: List[Dict[str, Any]] = []
    footer_top = page.rect.height * 0.95
    for block in page.get_text("dict").get("blocks", []):
        lines = []
        for line in block.get("lines", []):
            text = clean("".join(s["text"] for s in line.get("spans", [])))
            if not text:
                continue
            x0, y0, x1, y1 = line["bbox"]
            if x1 < 50.0 or y0 > footer_top:     # gutter strip, footer and page number
                continue
            lines.append((text, (x0, y0, x1, y1)))
        block_text = clean(" ".join(t for t, _ in lines))
        for text, box in lines:
            out.append({"text": text, "box": box, "block_text": block_text})
    return out


def page_annotations(page: "pymupdf.Page") -> List[Dict[str, Any]]:
    """Reviewer comments (PDF annotations carrying text) of a page, text verbatim."""
    out: List[Dict[str, Any]] = []
    for annot in page.annots() or []:
        info = annot.info or {}
        text = clean(re.sub(r"[\r\n]+", " / ", info.get("content", "") or ""))
        if not text:
            continue
        verts = annot.vertices or []
        tip = tuple(verts[0]) if verts and annot.type[1] == "FreeText" else None    # callout arrow tip
        out.append({"text": text, "rect": tuple(annot.rect), "author": clean(info.get("title", "") or ""),
                    "date": clean(info.get("modDate", "") or info.get("creationDate", "") or ""),
                    "kind": annot.type[1], "tip": tip})
    return out


def page_markup(page: "pymupdf.Page") -> List[Dict[str, Any]]:
    """Reviewer markup that carries no text (a Line, Square, Polygon ... annotation with empty contents).

    ``strike`` is True for a thin horizontal Line or a StrikeOut: a reviewer's strike-through of a word or a row.
    ``y`` and ``x0`` / ``x1`` are the extent of the stroke.
    """
    out: List[Dict[str, Any]] = []
    for annot in page.annots() or []:
        kind = annot.type[1]
        info = annot.info or {}
        if kind in ("Popup", "Link", "Widget") or clean(info.get("content", "") or ""):
            continue
        rect = tuple(annot.rect)
        verts = annot.vertices or []
        horizontal = bool(kind == "Line" and len(verts) == 2 and abs(verts[0][1] - verts[1][1]) <= 3.0
                          and abs(verts[0][0] - verts[1][0]) >= 15.0)
        out.append({"kind": kind, "rect": rect, "author": clean(info.get("title", "") or ""),
                    "date": clean(info.get("modDate", "") or info.get("creationDate", "") or ""),
                    "strike": horizontal or kind == "StrikeOut",
                    "y": (rect[1] + rect[3]) / 2.0, "x0": rect[0], "x1": rect[2]})
    return out


def markup_text(m: Dict[str, Any], struck: str = "") -> str:
    """Text recorded for an annotation without contents."""
    return "[%s annotation, no text%s]" % (m["kind"], ("; strikes through: %s" % struck) if struck else "")


def _inside_any(box: Tuple[float, float, float, float], rects: Sequence[Tuple[float, float, float, float]],
                pad: float = 2.0) -> bool:
    cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
    return any(r[0] - pad <= cx <= r[2] + pad and r[1] - pad <= cy <= r[3] + pad for r in rects)


def has_colour_legend(page: "pymupdf.Page", words: Sequence[Word]) -> bool:
    """True when a coloured swatch has text right beside it, or the word LEGEND appears.

    Colours are not interpreted: only the presence of swatch plus text is recorded.
    """
    if any(re.fullmatch(r"LEGENDS?:?", w.text, re.I) for w in words):
        return True
    for d in page.get_drawings():
        fill = d.get("fill")
        rect = d["rect"]
        if fill is None or rect.width > 40 or rect.height > 40 or rect.width < 3 or rect.height < 3:
            continue
        if all(abs(c - 1.0) < 0.04 for c in fill) or all(abs(c) < 0.04 for c in fill):
            continue
        for w in words:
            if rect.x1 - 1 <= w.x0 <= rect.x1 + 80 and w.y0 - 2 <= rect.y1 and w.y1 + 2 >= rect.y0:
                return True
    return False


def raster_legend(page: "pymupdf.Page") -> str:
    """Bounding box of a raster colour legend (an image in the lower-right of the plan holding at least
    two clearly coloured fills), or ''.  Its text is not in the text layer and is not read here; the
    colours are not interpreted."""
    w, h = page.rect.width, page.rect.height
    for im in page.get_image_info():
        x0, y0, x1, y1 = im["bbox"]
        if not (x0 > w * 0.7 and y0 > h * 0.6 and y1 < h * 0.93 and 90 <= x1 - x0 <= 320 and 50 <= y1 - y0 <= 220):
            continue
        pix = page.get_pixmap(clip=pymupdf.Rect(x0, y0, x1, y1), dpi=40, alpha=False)
        counts: Dict[Tuple[int, int, int], int] = defaultdict(int)
        n = pix.width * pix.height
        for i in range(0, len(pix.samples) - 2, pix.n):
            r, g, b = pix.samples[i], pix.samples[i + 1], pix.samples[i + 2]
            if max(r, g, b) - min(r, g, b) >= 40:          # saturated, not white/grey/black
                counts[(r // 48, g // 48, b // 48)] += 1
        if sum(1 for c in counts.values() if c >= 0.01 * n) >= 2:
            return fmt_bbox((x0, y0, x1, y1))
    return ""


def zoning_records(page: "pymupdf.Page", words: List[Word]) -> Tuple[str, List[Dict[str, Any]], bool]:
    """Keyplan title, every tag and room label with bbox, and the legend ``(source, bbox)``:
    source is 'text' (words beside swatches), 'image' (raster legend, text not in the text layer) or ''."""
    annots = page_annotations(page)
    arects = [a["rect"] for a in annots]
    lines = [ln for ln in page_lines(page) if not FURNITURE_LINE_RE.match(ln["text"])
             and not _inside_any(ln["box"], arects)]
    title = ""
    for ln in lines:
        if "KEYPLAN" in ln["text"].upper():
            title = re.sub(r"\s+", " ", ln["text"])
            break
    records: List[Dict[str, Any]] = []
    for w in words:
        if is_gutter(w) or _inside_any(w.box(), arects):
            continue
        if TAG_RE.match(w.text):
            records.append({"label_kind": "equipment_tag", "text": w.text, "block_text": w.text,
                            "bbox": fmt_bbox(w.box())})
    for ln in lines:
        text = ln["text"]
        if TAG_RE.match(text) or all(TAG_RE.match(p) for p in text.split() if p):
            continue
        if "KEYPLAN" in text.upper():
            kind = "keyplan_title"
        elif re.match(r"^(AHU ZONING|DEPARTMENTAL BOUNDARY|Typical for|N|NTS)\b", text) or \
                re.match(r"^\d+(st|nd|rd|th)\b.*wing", text, re.I):
            kind = "other_text"
        else:
            kind = "room_label"
        records.append({"label_kind": kind, "text": text, "block_text": ln["block_text"],
                        "bbox": fmt_bbox(ln["box"])})
    for a in annots:
        records.append({"label_kind": "reviewer_annotation", "text": a["text"], "block_text": a["text"],
                        "bbox": fmt_bbox(a["rect"])})
    for m in page_markup(page):       # arrows, boxes and polygons drawn by a reviewer, with no text
        records.append({"label_kind": "reviewer_markup", "text": markup_text(m), "block_text": markup_text(m),
                        "bbox": fmt_bbox(m["rect"])})
    if has_colour_legend(page, words):
        legend = ("text", "")
    else:
        box = raster_legend(page)
        legend = ("image", box) if box else ("", "")
    return title, records, legend


def pressure_records(page: "pymupdf.Page", notes_top: Optional[float] = None
                     ) -> Tuple[str, List[Dict[str, Any]]]:
    """Drawing text labels (room names, +/- values) of a pressure regime page, plus reviewer annotations
    (PDF comments) kept apart as label_kind reviewer_annotation.  Lines at or below ``notes_top`` (the NOTE
    block) are not labels: they are recorded verbatim in criteria_notes.csv."""
    heading = ""
    records: List[Dict[str, Any]] = []
    annots = page_annotations(page)
    arects = [a["rect"] for a in annots]
    for ln in page_lines(page):
        text = ln["text"]
        if FURNITURE_LINE_RE.match(text) or _inside_any(ln["box"], arects):
            continue      # comment text is recorded once, below, as a reviewer_annotation
        if notes_top is not None and ln["box"][1] >= notes_top - 0.5:
            continue
        if re.match(r"^PRESSURE REGIME", text):
            heading = text
            continue
        kind = "pressure_value" if PRESSURE_VALUE_RE.match(text) or text in ("+", "-") else "text_label"
        records.append({"label_kind": kind, "text": text, "bbox": fmt_bbox(ln["box"]),
                        "annotation_author": "", "annotation_date": ""})
    for a in annots:
        records.append({"label_kind": "reviewer_annotation", "text": a["text"], "bbox": fmt_bbox(a["rect"]),
                        "annotation_author": a["author"], "annotation_date": a["date"]})
    for m in page_markup(page):       # boxes and arrows drawn by a reviewer, with no text
        records.append({"label_kind": "reviewer_markup", "text": markup_text(m), "bbox": fmt_bbox(m["rect"]),
                        "annotation_author": m["author"], "annotation_date": m["date"]})
    return heading, records


# --------------------------------------------------------------------------- #
# File-level driver
# --------------------------------------------------------------------------- #
def department_from_name(path: Path) -> str:
    """'IP_FAST_Section_4.8_...' -> 'IP FAST'."""
    stem = path.stem
    m = re.match(r"^(.*?)[_ ]Section[_ ]\d", stem, re.I)
    base = m.group(1) if m else stem
    return re.sub(r"[_\s]+", " ", base).strip()


def display_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return path.name


def _note_row(src: str, dept: str, booklet: str, pdf_page: int, number: str, text: str,
              box: Tuple[float, float, float, float], kind: str, author: str = "", date: str = "") -> Dict[str, Any]:
    return {"file": src, "page": booklet, "note_number": number, "text": text, "booklet_page": booklet,
            "pdf_page": pdf_page, "bbox": fmt_bbox(box), "department": dept, "note_kind": kind,
            "author": author, "date": date}


def _fills(page: "pymupdf.Page") -> List[Tuple[float, float, float, float]]:
    return [tuple(d["rect"]) for d in page.get_drawings()
            if d.get("fill") is not None and d["rect"].width > 20 and d["rect"].height > 10]


def _rules(page: "pymupdf.Page") -> List[Tuple[float, float, float]]:
    """Horizontal rule segments ``(y, x0, x1)``: stroked lines or hairline rectangles wider than 100 points."""
    out = []
    for d in page.get_drawings():
        r = d["rect"]
        if r.height < 2.5 and r.width > 100:
            out.append(((r.y0 + r.y1) / 2.0, r.x0, r.x1))
    return out


def drop_annotations(page: "pymupdf.Page") -> None:
    """Remove every annotation from the in-memory page so that reviewer comments (and what they cover) no
    longer sit in the text layer.  The PDF on disk is never written."""
    for annot in list(page.annots() or []):
        page.delete_annot(annot)


def other_heading(words: Sequence[Word]) -> str:
    """Title of a page that is not one of the table classes: the big-type words left of the section strip."""
    body = [w for w in words if not is_gutter(w) and not (w.x0 >= 735 and w.y0 < 90) and w.y0 < 300]
    for size in (26.0, 18.0):
        big = [w for w in body if w.size >= size]
        if big:
            return join_words(big)[:120]
    return ""


def parse_pdf(path: Path, root: Path) -> Dict[str, Any]:
    """Parse one booklet PDF into registers, page map and failures."""
    dept = department_from_name(path)
    src = display_path(path, root)
    result: Dict[str, Any] = {"department": dept, "source_file": src, "pages": [], "criteria": [],
                              "notes": [], "standby_notes": [], "standby": [], "zoning": [], "pressure": [],
                              "room_data": [], "failures": [], "page_failures": []}
    doc = pymupdf.open(str(path))
    carry = ""
    try:
        for index in range(len(doc)):
            page = doc[index]
            pdf_page = index + 1
            page_h, page_w = page.rect.height, page.rect.width
            all_words = page_words(page)
            booklet = printed_page_number(all_words, page_h, page_w)
            words = strip_furniture(all_words, page_h, page_w)     # footer stamp and page number are not body text
            heading_big = large_heading(words)
            strip = section_strip(words)
            cls = classify_page(page.get_text(), heading_big, strip)
            annots = page_annotations(page)
            markup = page_markup(page)
            if cls in ("criteria", "standby summary", "room data table") and list(page.annots() or []):
                drop_annotations(page)          # comment text must not be read as table cells
                words = strip_furniture(page_words(page), page_h, page_w)
            meta = {"department": dept, "source_file": src, "pdf_page": pdf_page, "booklet_page": booklet}
            entry = {"pdf_page": pdf_page, "booklet_page": booklet, "class": cls, "detail": "", "rows": 0,
                     "annotations": len(annots), "markup": len(markup)}
            if not booklet:
                result["page_failures"].append((pdf_page, "printed page number missing or ambiguous in the text layer"))

            def annotation_notes(target: List[Dict[str, Any]], kind: str,
                                 struck: Optional[Dict[int, str]] = None) -> None:
                for a in annots:
                    target.append(_note_row(src, dept, booklet, pdf_page, "", a["text"], a["rect"], kind,
                                            a["author"], a["date"]))
                for i, m in enumerate(markup):      # marks with no text (strike-through, box, arrow) are recorded too
                    target.append(_note_row(src, dept, booklet, pdf_page, "", markup_text(m, (struck or {}).get(i, "")),
                                            m["rect"], "reviewer markup", m["author"], m["date"]))

            if cls == "criteria":
                marks = [{"kind": "comment", "type": a["kind"], "text": a["text"], "author": a["author"],
                          "tip": a["tip"]} for a in annots if a["tip"]]
                marks += [{"kind": "strike", "type": m["kind"], "author": m["author"], "y": m["y"], "x0": m["x0"],
                           "x1": m["x1"], "idx": i} for i, m in enumerate(markup) if m["strike"]]
                rows, notes, part, pfail = parse_criteria_page(words, page_h, page_w, _fills(page), marks)
                built = build_criteria_rows(rows, meta, part, result["failures"])
                result["criteria"].extend(built)
                for n in notes:
                    result["notes"].append(_note_row(src, dept, booklet, pdf_page, n["number"], n["text"],
                                                     n["box"], "table note"))
                struck: Dict[int, str] = {}
                for r in rows:
                    for m in r["marks"]:
                        if m["kind"] == "strike":
                            struck[m["idx"]] = (struck.get(m["idx"], "") + "; " if m["idx"] in struck else "") + \
                                (r["cells"].get("room", "") or "(row without room text)")
                annotation_notes(result["notes"], "reviewer annotation", struck)
                entry.update(detail=part, rows=len(built), notes=len(notes))
                result["page_failures"].extend((pdf_page, f) for f in pfail)
            elif cls == "room data table":
                rows, headings, pfail = parse_room_data_page(words, page_h, _fills(page), _rules(page))
                for i, r in enumerate(rows, 1):
                    c = r["cells"]
                    known = {k: c.get(k, "") for k, _ in ROOM_DATA_KEYS}
                    other = {k: v for k, v in c.items() if k not in known}
                    result["room_data"].append({
                        "department": dept, "source_file": src, "booklet_page": booklet, "pdf_page": pdf_page,
                        "row": i, **known,
                        "other_columns": json.dumps(other, ensure_ascii=False) if other else "",
                        "columns_as_printed": json.dumps(headings, ensure_ascii=False),
                        "bbox": fmt_bbox(r["box"]),
                        "confidence": "0.97" if known["d_unique_room_identifier"] and known["qty03_v12"] else "0.60"})
                notes_top = find_notes_top(words, 0.0, page_h)
                nts = parse_notes(words, notes_top, page_h) if notes_top < page_h else []
                for n in nts:
                    result["notes"].append(_note_row(src, dept, booklet, pdf_page, n["number"], n["text"],
                                                     n["box"], "room data table note"))
                annotation_notes(result["notes"], "reviewer annotation")
                entry.update(rows=len(rows), detail="room data table; %d columns" % len(headings))
                result["page_failures"].extend((pdf_page, f) for f in pfail)
            elif cls == "standby summary":
                rows, carry, pfail = parse_standby_page(words, page_h, carry, _fills(page))
                has_tags = any(r["cells"].get("tag") for r in rows)
                refs, snotes = standby_notes(words, page_h)
                for n in snotes:
                    result["standby_notes"].append(_note_row(src, dept, booklet, pdf_page, n["number"], n["text"],
                                                             n["box"], "table note"))
                annotation_notes(result["standby_notes"], "reviewer annotation")
                for r in rows:
                    c = r["cells"]
                    result["standby"].append({
                        "department": dept, "source_file": src, "booklet_page": booklet, "pdf_page": pdf_page,
                        "level_wing_heading": r["heading"], "tag_as_written": c.get("tag", ""),
                        "tag_normalised": normalise_tag(c.get("tag", "")), "area": c.get("area", ""),
                        "configuration": c.get("config", ""), "note_refs": refs, "bbox": fmt_bbox(r["box"]),
                        "confidence": "0.97" if (c.get("area") and c.get("config") and r["heading"]
                                                 and (c.get("tag") or not has_tags)) else "0.50",
                    })
                    for field, key in (("area", "area"), ("configuration", "config"),
                                       ("level_wing_heading", None)):
                        val = r["heading"] if key is None else c.get(key, "")
                        if not val:
                            result["failures"].append({"file": src, "pdf_page": pdf_page, "booklet_page": booklet,
                                                       "row": c.get("tag", "") or c.get("area", ""),
                                                       "field": "standby " + field,
                                                       "cell": "", "bbox": fmt_bbox(r["box"])})
                entry.update(rows=len(rows), detail="standby rows" + ("" if has_tags or not rows
                                                                      else " (no equipment tag column)"))
                result["page_failures"].extend((pdf_page, f) for f in pfail)
            elif cls == "zoning keyplan":
                title, recs, legend = zoning_records(page, words)
                if not title:
                    result["page_failures"].append((pdf_page, "keyplan title not found"))
                for rec in recs:
                    result["zoning"].append({**meta, "keyplan_title": title, "has_colour_legend": "Y" if legend[0] else "N",
                                             "legend_source": legend[0], "legend_bbox": legend[1], **rec})
                tags = sum(1 for r in recs if r["label_kind"] == "equipment_tag")
                entry.update(rows=len(recs), detail="%s; %d tags; colour legend %s" % (title or "no title", tags, "yes (%s)" % legend[0] if legend[0] else "no"))
            elif cls == "pressure regime":
                notes_top = find_notes_top(words, 0.0, page_h)
                has_note = notes_top < page_h
                heading, recs = pressure_records(page, notes_top if has_note else None)
                for rec in recs:
                    result["pressure"].append({**meta, "page_heading": heading, **rec})
                kind = "pandemic mode note" if re.search(r"PANDEMIC", heading, re.I) else "pressure regime note"
                n_notes = 0
                if has_note:
                    for n in parse_block_note(words, notes_top, page_h):
                        result["notes"].append(_note_row(src, dept, booklet, pdf_page, n["number"], n["text"],
                                                         n["box"], kind))
                        n_notes += 1
                annotation_notes(result["notes"], "reviewer annotation")
                entry.update(rows=len(recs), detail=heading or "PRESSURE REGIME", notes=n_notes)
            else:
                if cls == "other":
                    entry["detail"] = other_heading(words)
                    if strip and not re.search(r"MECHANICAL VENTILATION", strip, re.I):
                        entry["detail"] = ("%s [%s: another section of the booklet]" % (entry["detail"], strip)).strip()
                    elif annots or markup:
                        # a 4.8 page whose table is not read (Healthy Aging OP pages): its reviewer comments are
                        # still recorded, so they are not lost with the table
                        annotation_notes(result["notes"], "reviewer annotation")
            result["pages"].append(entry)
    finally:
        doc.close()
    return result


def collect_inputs(paths: Optional[Sequence[str]], root: Path) -> List[Path]:
    """Files named by ``--paths`` (directories are searched for PDFs); default: the booklet folder."""
    if not paths:
        folder = root / common.load_config().get("paths", {}).get("source", "data/source") / BOOKLET_DIR_NAME
        return sorted(folder.glob("*.pdf"))
    found: List[Path] = []
    for item in paths:
        p = Path(item)
        if not p.is_absolute():
            p = (Path.cwd() / p) if (Path.cwd() / p).exists() else (root / p)
        if p.is_dir():
            found.extend(sorted(p.glob("*.pdf")))
        elif p.is_file():
            found.append(p)
        else:
            raise FileNotFoundError(str(item))
    return found


# --------------------------------------------------------------------------- #
# Cross-department comparison and report
# --------------------------------------------------------------------------- #
COMPARE_FIELDS = ["pressure", "min_oa_ach", "min_total_ach", "single_pass", "air_class",
                  "filter", "rh", "temp", "afterhours", "setback"]


def _mode_of(part: str) -> str:
    return part.split(";", 1)[1].strip() if ";" in part else ""


def _compare_values(r: Dict[str, Any]) -> Dict[str, Tuple[str, str]]:
    """Per compared field: ``(key, shown)``.  The key is what is compared (normalised, so '21-24' equals
    '21 – 24' and '- 15' equals '-15'); ``shown`` is the figure as printed."""
    pr = "%s|%s|%s|%s" % (r["pressure_direction"], r["pressure_pa"], r.get("pressure_pa_min", ""),
                          r.get("pressure_pa_max", ""))
    return {
        "pressure": (pr, clean(r["pressure_regime_as_printed"])),
        "min_oa_ach": (clean(r["min_oa_ach"]), json.loads(r["raw_cells"]).get("min_oa", "") or r["min_oa_ach"]),
        "min_total_ach": (clean(r["min_total_ach"]), json.loads(r["raw_cells"]).get("min_total", "") or r["min_total_ach"]),
        "single_pass": (clean(r["single_pass"]), clean(r["single_pass"])),
        "air_class": (clean(r["air_class"]), clean(r["air_class"])),
        "filter": (clean(r["filter"]).lower(), clean(r["filter"])),
        "rh": ("%s|%s" % (r["rh_max_pct"], r["rh_min_pct"]), clean(r["rh_as_printed"])),
        "temp": ("%s|%s" % (r["temp_min_c"], r["temp_max_c"]), clean(r["temp_as_printed"])),
        "afterhours": (clean(r["afterhours"]), clean(r["afterhours"])),
        "setback": (clean(r["setback"]), clean(r["setback"])),
    }


def _components(norm: str) -> set:
    return {c.strip() for c in re.split(r"/|\|", norm) if c.strip()}


def comparison_data(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Match criteria rows across departments by normalised room type and operating mode, and list the
    fields on which the departments differ.  Returns the groups, the near matches and the unmatched names."""
    depts = [r["department"] for r in results if r["criteria"]]
    table: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}
    label: Dict[Tuple[str, str], str] = {}
    for res in results:
        seen: Dict[Tuple[str, str], int] = defaultdict(int)
        for r in res["criteria"]:
            starred = "*" in r["note_refs"].split(";")      # a footnoted variant ('Anteroom*') is its own row type
            base = (r["room_type_norm"] + (" *" if starred else ""), _mode_of(r["table_part"]))
            n = seen[base]
            seen[base] += 1
            key = (base[0], base[1] + ("" if n == 0 else " (row %d)" % (n + 1)))
            table.setdefault(key, {})[res["department"]] = r
            label.setdefault(key, r["room_type_as_printed"])
    groups = {k: v for k, v in table.items() if len(v) >= 2}
    diffs: List[Dict[str, Any]] = []
    identical = 0
    for k, per_dept in groups.items():
        vals = {d: _compare_values(r) for d, r in per_dept.items()}
        differing = []
        for f in COMPARE_FIELDS:
            by_key: Dict[str, List[str]] = defaultdict(list)
            shown: Dict[str, str] = {}
            for d in per_dept:
                key_f, show = vals[d][f]
                by_key[key_f].append(d)
                shown.setdefault(key_f, show)
            if len(by_key) > 1:
                differing.append((f, [(shown[kf] or "(blank)", ds) for kf, ds in by_key.items()]))
        if differing:
            diffs.append({"key": k, "label": label[k], "fields": differing, "depts": list(per_dept)})
        else:
            identical += 1
    singles = {k: next(iter(v)) for k, v in table.items() if len(v) == 1}
    near: List[Dict[str, Any]] = []
    used: set = set()
    keys = list(singles)
    for i, k in enumerate(keys):
        for kk in keys[i + 1:]:
            if singles[k] == singles[kk] or k in used or kk in used:
                continue
            ca, cb = _components(k[0]), _components(kk[0])
            ratio = difflib.SequenceMatcher(None, k[0], kk[0]).ratio()
            if not ((ca & cb) or ratio >= 0.8) or k[1] != kk[1]:
                continue        # a different operating mode is not the same room type
            ra, rb = table[k][singles[k]], table[kk][singles[kk]]
            va, vb = _compare_values(ra), _compare_values(rb)
            d = ["%s: %s vs %s" % (f, va[f][1] or "(blank)", vb[f][1] or "(blank)")
                 for f in COMPARE_FIELDS if va[f][0] != vb[f][0]]
            near.append({"a": (singles[k], label[k], k[1]), "b": (singles[kk], label[kk], kk[1]), "diffs": d})
            used.update([k, kk])
    only = defaultdict(list)
    for k, d in singles.items():
        if k not in used:
            only[d].append(label[k] + (" [%s]" % k[1] if k[1] else ""))
    return {"departments": depts, "groups": len(groups), "identical": identical, "diffs": diffs,
            "near": near, "only": dict(only), "total_types": len(table)}


def compare_departments(results: List[Dict[str, Any]]) -> List[str]:
    """Markdown lines: criteria for the same room type compared across every department that has it."""
    data = comparison_data(results)
    if len(data["departments"]) < 2:
        return ["Fewer than two departments with criteria rows; nothing to compare."]
    L: List[str] = [
        "Rows are matched across all %d departments (%s) by normalised room type and operating mode "
        "(the mode is the section row, such as Hybrid Cooling). Compared fields: %s. Figures are compared in "
        "normalised form (so `21-24` equals `21 – 24`) and shown as printed." % (
            len(data["departments"]), ", ".join(data["departments"]), ", ".join(COMPARE_FIELDS)), ""]
    L.append("Room types present in two or more departments: %d of %d distinct room types (%d identical on all %d "
             "compared fields in every department that has them; %d with at least one difference)." % (
                 data["groups"], data["total_types"], data["identical"], len(COMPARE_FIELDS), len(data["diffs"])))
    L.append("")
    if data["diffs"]:
        L += ["### Differences for the same room type (flagged)", "",
              "| Room type | Field | Value as printed: departments |", "|---|---|---|"]
        for d in data["diffs"]:
            mode = " [%s]" % d["key"][1] if d["key"][1] else ""
            for f, variants in d["fields"]:
                L.append("| %s | %s | %s |" % (d["label"] + mode, f, "; ".join(
                    "**%s**: %s" % (v, ", ".join(ds)) for v, ds in variants)))
        L.append("")
    else:
        L += ["No value differences among the matched room types.", ""]
    if data["near"]:
        L += ["### Near matches (names differ, so not compared automatically; check by eye)", "",
              "| Department A | Name A | Department B | Name B | Differences (A vs B) |", "|---|---|---|---|---|"]
        for n in data["near"]:
            L.append("| %s | %s | %s | %s | %s |" % (n["a"][0], n["a"][1] + (" [%s]" % n["a"][2] if n["a"][2] else ""),
                                                  n["b"][0], n["b"][1] + (" [%s]" % n["b"][2] if n["b"][2] else ""),
                                                  "; ".join(n["diffs"]) or "identical on compared fields"))
        L.append("")
    if data["only"]:
        L += ["### Room types found in one department only", ""]
        for d, names in data["only"].items():
            L.append("- %s: %s." % (d, "; ".join(names)))
        L.append("")
    return L


def _corrections_section(results: List[Dict[str, Any]]) -> List[str]:
    """Corrections made after the independent verification (counts are taken from this run)."""
    L: List[str] = ["", "## Corrections after verification", "",
                    "Independent verification (`out/qa/verify/booklet48_parser_verify.md`) confirmed the criteria "
                    "of IP FAST and IP SURGICAL (41 rows, 533 cells), standby (50 rows) and page classes, and refuted one claim. "
                    "What changed:", ""]
    first_batch = ("IP FAST", "IP SURGICAL")
    ann = [(r["department"], p) for r in results if r["department"] in first_batch
           for p in r["pressure"] if p["label_kind"] == "reviewer_annotation"]
    drawing = {r["department"]: sum(1 for p in r["pressure"] if p["label_kind"] != "reviewer_annotation")
               for r in results if r["department"] in first_batch}
    L += ["### Pressure regime labels versus reviewer comments", "",
          "- `booklet_pressure_labels.csv` mixed drawing text with PDF FreeText annotations (reviewer comments), "
          "all typed `text_label`. Annotations are now recorded once each, with `label_kind = reviewer_annotation`, "
          "the annotation rectangle as `bbox` and `annotation_author` / `annotation_date` from the PDF; the same text is "
          "no longer repeated as drawing labels. The same separation applies to zoning keyplans.",
          "- Drawing labels now: %s. Reviewer annotations: %s." % (
              ", ".join("%s %d" % kv for kv in drawing.items()),
              ", ".join("%s p%s" % (d, p["booklet_page"]) for d, p in ann) or "none"), ""]
    if ann:
        L += ["| Department | Booklet p. | PDF p. | Author | Comment (verbatim, line breaks as ` / `) | bbox |",
              "|---|---|---|---|---|---|"]
        for d, p in ann:
            L.append("| %s | %s | %s | %s | %s | %s |" % (d, p["booklet_page"], p["pdf_page"], p["annotation_author"] or "-",
                                                          p["text"], p["bbox"]))
        L += ["", "**Reliance caveat.** One IP FAST comment reads that sign-off does not include endorsement for the "
              "ACMV / Pressure Regime portion, pending the consultant's consolidated inputs for a run-through with the "
              "infection-prevention and MOHH teams. The pressure regime drawings are therefore not signed-off design "
              "values. Other comments say some wards are negative and some positive, that the airlock is slightly positive "
              "to the staff corridor, and that NEP-capable wards convert to a single-pass BIBO exhaust in pandemic mode; "
              "one asks whether NEP-capable should be read against the adjacent external corridor (marked 'To verify'). "
              "No other page of IP FAST or IP SURGICAL carries annotations.", ""]
    L += ["### Zoning keyplan colour legend (earlier 'no legend' was wrong)", "",
          "- The earlier result rested on the absence of legend words. Swatch geometry shows no vector swatches (one white "
          "rectangle per page), but each keyplan page carries a raster legend image in the lower right whose text is not in "
          "the text layer. A render of IP FAST PDF p9 shows a heading 'Normal Mode Operation' and three coloured swatches "
          "labelled Recirculation, Single Pass and Recirculation with HEPA. The other keyplan pages carry an image of the same "
          "size and position class; they were detected by geometry and colour count, not read.",
          "- `booklet_zoning_labels.csv` now has `has_colour_legend = Y`, `legend_source` (`text` or `image`) and `legend_bbox`. "
          "Colours are not interpreted and the image text is not transcribed into the registers.", ""]
    zl = [z for res in results for z in res["zoning"]]
    kinds: Dict[Tuple[str, str], int] = defaultdict(int)
    for z in zl:
        kinds[(z["department"], z["label_kind"])] += 1
    L += ["### Zoning label split re-derived", "",
          "Counts by kind: " + "; ".join("%s %s = %d" % (d, k, n) for (d, k), n in sorted(kinds.items())) + ". "
          "`other_text` holds only 'AHU ZONING', 'DEPARTMENTAL BOUNDARY', 'N', 'Typical for :' and the 'Nth ... wing' "
          "storey notes; none of those strings appears under `room_label`.", ""]
    L += ["### Equipment standby note references", "",
          "- `booklet_equipment_standby.csv` has a `note_refs` column (`1,2`, printed beside the Configuration header) and the "
          "NOTE block of each standby page is written to `booklet_standby_notes.csv`.", ""]
    L += ["### Observation to cross-check (not a parser error)", "",
          "- IP FAST keyplan on PDF p9, titled 'IP 20TH STOREY', carries level-19 NW tags `AHU-B-19-19MEDSNWWG-1` and `-2`, but the "
          "standby summary lists level 19 NE and SE wings only. The storey title and the level in the tags also differ by one. "
          "Check against the zoning source.", ""]
    L += ["### Tests added", "",
          "- A value word whose plain box reaches into the Room column (the 'Interview Room' / 'Neutral' case).",
          "- Reviewer annotation on a pressure regime page kept apart from drawing labels.",
          "- Raster colour legend flagged as `image`.", "- Standby note references and NOTE block.", ""]
    return L


def _second_batch_section(results: List[Dict[str, Any]]) -> List[str]:
    """Diagnosis of the failures met when the five further extracts were first run, what was changed, and what is
    still not parsed.  The 'before' counts are those of the first run on the unmodified parser."""
    by = {r["department"]: r for r in results}
    present = [d for d in ("Dialysis", "Healthy Aging Geriatrics", "ISO AH", "Palliative", "Psych Med") if d in by]
    if not present:
        return []
    L: List[str] = ["## Second batch: Dialysis, Healthy Aging Geriatrics, ISO AH, Palliative, Psych Med", "",
                    "Short-version extracts added after the first two. The first run of the unchanged parser gave criteria rows "
                    "Dialysis 15, Healthy Aging Geriatrics 18, Palliative 16, Psych Med 16, ISO AH 0, and failed fields "
                    "Dialysis 6, Healthy Aging Geriatrics 2, ISO AH 2, Palliative 1, Psych Med 32 (4 criteria cells, 24 standby "
                    "cells, 4 room texts with no data row). Each was traced to the words and a render of the page. "
                    "The department name is taken from the file name, so the Healthy Ageing booklet is `Healthy Aging Geriatrics`.", "",
                    "| Failure | Cause found in the PDF | Change |", "|---|---|---|",
                    "| Dialysis p5, Healthy Aging p5: criteria header and table part not found | The page is not a criteria table. It is the "
                    "room data table (D_Template Room Code, D_Room Name, D_Unique Room Identifier, Item Description, Qty03 (v12), "
                    "Type of TMS, D_Template Room Name), a BME temperature-monitoring schedule. The page heading strip still reads DESIGN CRITERIA, so it was "
                    "classed `criteria`. ISO AH p5 is the same table and was classed `other`. | New class `room data table`, tested before `criteria`; rows "
                    "written to `booklet_room_data.csv`. |",
                    "| Dialysis p6: table part label not found | The single table has the caption `Room conditions` with no `Part N`. | `table_part` = "
                    "`Room conditions (unnumbered)`; not a failure. |",
                    "| ISO AH p6, p7: criteria header not found (0 rows) | The Temperature heading is wrapped as `Temperatu` / `re, C` and neither "
                    "fragment is a header word, so the header block stopped short. Reviewer callouts also sit across the heading. | `Temperatu` and `re,` added to the header "
                    "words and the temperature key matches `temperatu`; callouts are taken out of the text layer (next row). |",
                    "| Psych Med p6: 4 criteria cells and 4 orphan room texts | Four PDF FreeText comments (`should be negative pressured treatment room`, "
                    "`MDT to confirm and benchmark against other PHI on the Pa regime`, `Missing from this list: Iso ensuite toilet`, ...) lie over the table and were read "
                    "as room and value cells. The same happens on ISO AH p6 (`Isolation: min. -5pa`). | Annotations are removed from the in-memory page before words are "
                    "read on criteria, standby and room data pages; they are written to `criteria_notes.csv` / `booklet_standby_notes.csv` as `reviewer annotation`. |",
                    "| Psych Med p5: Laundry merged into Clinical Workroom; p6: `MERV 15` read as a section heading | The Room column is drawn as one rectangle per row. "
                    "`Laundry` is a row of its own with every value cell empty; the Low Stimulus Room filter cell is wrapped over four lines that sit outside the 14 point "
                    "window around its value line. | Where the Room column has row rectangles, a line belongs to the row rectangle that holds it; a room name alone in a "
                    "rectangle is a row with blank cells (reported as `pressure (cell empty)`). Without rectangles the old rule applies. |",
                    "| ISO AH p7: `* One out of six wards ...` read as a data row | An asterisk footnote under the last row. | Lines starting `*` are notes "
                    "(number `*`); the `*` after `Anteroom*` and `ISO Type A Bedroom*` is kept in the room text and in `note_refs`, and dropped from `room_type_norm`. |",
                    "| Dialysis p8, Psych Med p10-p11: standby area blank, tag taken from area text (Psych Med p10: 26 rows where 7 exist) | Area text is centred in a wide cell, so "
                    "words left of the column midpoint were read as tag; cells are vertically centred, so wrapped lines sit above and below the tag line; Configuration is a few "
                    "points off the tag line; `AHU-CONFIG-E` matches the tag pattern. A comment lies over the Psych Med table. | Where the Configuration column has one rectangle per row, "
                    "the rectangles are the rows. Only a tag-pattern word left of the Configuration column is a tag. A heading wrapped over two lines in one merged cell "
                    "(`Level 15` / `NW wing`) is joined. |",
                    "| Palliative p7: standby header not found | The table has no Equipment Tag column (Area and Configuration only; tags not yet assigned). | "
                    "Rows are read from the row rectangles with a blank tag; the page map notes `no equipment tag column`. |",
                    "| Printed page number `2025` | Where the page number is not in the text layer (keyplan pages and one pressure regime page) the fallback took the year of the "
                    "`MAY 2025` footer stamp. On Psych Med PDF p24 two numbers (`136` and `13`) are printed over one another. | The footer date is skipped and "
                    "overlapping numbers are not guessed; the booklet page is left blank and listed under the page map. |",
                    "| Healthy Aging p24 classed `zoning keyplan` | A page headed PRESSURE REGIME that also prints an `OP 8TH STOREY KEYPLAN` title. | A big-type heading that reads "
                    "PRESSURE REGIME decides the class. |", ""]
    L += ["### Pressure in Pa", "",
          "`pressure_pa` holds a single signed value; `pressure_pa_min` and `pressure_pa_max` hold the same value for a single figure and the two ends of a range "
          "(`-10 to -15` gives blank `pressure_pa`, min -15, max -10); `pressure_direction` is derived from the signs "
          "(`negative`, `positive`, `neutral`, or `mixed` for a range that crosses zero). A range of unsigned figures takes its sign from a direction word "
          "(`Negative 10 to 15`); an unsigned figure with no direction word is reported as a failed `pressure` field. "
          "Pa figures printed in the second batch are all single values: ",
          "; ".join("%s %s" % (r["room_type_as_printed"].replace("*", ""), r["pressure_regime_as_printed"] + " (" + r["pressure_pa"] + ")")
                    for res in results for r in res["criteria"] if r["pressure_pa"]) + ". "
          "No range is printed in these seven files; ranges are covered by a synthetic test only.", ""]
    L += ["### Still not parsed", ""]
    unparsed: List[str] = []
    for res in results:
        for p in res["pages"]:
            if p["class"] == "other":
                unparsed.append("%s PDF p%d (%s): %s" % (res["department"], p["pdf_page"], p["booklet_page"] or "no number",
                                                       p["detail"] or "no heading"))
    L += ["- Pages classed `other` (not tables this parser reads): " + "; ".join(unparsed) + ".",
          "- Healthy Aging Geriatrics PDF p23 is an outpatient (OP) Equipment Configuration Summary (AHU-A-10-01/04, Area Served, Configuration) and p25-p26 are "
          "an OP `Operation Mode` criteria table with a different layout (DEPARTMENT / AREA / ROOM columns, `+VE`, `24 hrs AC`) carrying reviewer comments; the "
          "file is the inpatient extract with these OP pages appended. They are left unparsed and are not in `criteria.csv`; their reviewer comments "
          "(p25: `This ciontradicts eearlier demarcation plan.` and `20251201 DPE: All the rooms update to be Neutral, Only DU, Disposal and Toilet to be -ve`) "
          "are recorded in `criteria_notes.csv`.",
          "- Palliative PDF p4-p6 (`EQUIPMENT STANDBY CONFIGURATION` Part 1 to 3) are diagram pages with no text beyond the heading; the standby summary on p7 is read.",
          "- Palliative PDF p11-p18 and Psych Med PDF p21-p29 are pages of other booklet sections appended to the extract (AV strategy 4.1.10, "
          "communication strategy 4.1.11, IT strategy 4.9). The section title in the strip at the top right decides: they are classed "
          "`other` and not read, including Palliative p12 and Psych Med p24 (AV overview with a storey KEYPLAN title).",
          "- The room data table headings are kept as printed in `columns_as_printed`; the Dialysis template code wraps over two lines "
          "(`093B_OSUB_P2` / `C1`) and is joined with a space, as printed.",
          "- Keyplan pages carry text labels only; colours are not interpreted. Sensor schedule pages are classed but not parsed.", ""]
    return L


def _corrections_section_2(results: List[Dict[str, Any]]) -> List[str]:
    """Corrections made after the second independent verification (figures are taken from this run)."""
    by = {r["department"]: r for r in results}
    if "Palliative" not in by and "Psych Med" not in by and "ISO AH" not in by:
        return []
    L: List[str] = ["", "## Corrections after verification (2)", "",
                    "The second independent verification (`out/qa/verify/booklet48_parser_verify_2.md`) checked 27 claims about the five "
                    "further extracts: 23 confirmed, 4 refuted. All four are corrected below, each with a test. The gaps it listed are "
                    "answered after the table.", "",
                    "| Refuted claim | Cause found | Change | Result in this run |", "|---|---|---|---|"]
    pal = [r for r in by.get("Palliative", {}).get("criteria", [])]
    low = [(r["room_type_as_printed"], r["bbox"].split(",")[1]) for r in pal
           if float(r["bbox"].split(",")[1]) > 783.0]
    notes1 = [(x["department"], x["pdf_page"], len(x["text"])) for res in results for x in res["notes"]
              if n_is_table_note1(x) and x["department"] in ("ISO AH", "Psych Med")]
    L.append("| Palliative has 16 criteria rows and its only flag is page numbers | 18 rows are printed (p1 16, p2 2). The body was cut at "
             "0.93 of the page height (783 pt); `Clinical Workroom` (y 784.0) and `Cleaner Room` (y 813.2, on the line of the footer "
             "stamp) lie below it. Nothing was reported. | The footer is no longer a fixed fraction of the page: the stamp "
             "(`ALEXANDRA HOSPITAL REDEVELOPMENT PROJECT | DETAILED DESIGN BOOKLET | MAY 2025`, found by its words and type size) and the printed "
             "page number are taken out by token (`footer_furniture`), and the whole page height is read. Applied to criteria, notes, room data, "
             "standby and pressure regime pages. | Palliative %d criteria rows; rows below 783 pt: %s. |" % (
                 len(pal), ", ".join("%s (y %s)" % kv for kv in low) or "none"))
    L.append("| Table notes are recorded verbatim | Note 1 stopped at `...BUT HAVE SPECIFIC ROOMS` (102 characters, 209 printed) on ISO AH p6/p7 "
             "and Psych Med p5/p6: the second line (`REQUIRING AIR CONDITIONING ... PUSH-BUTTON.`) is below 783 pt. | Same change. | "
             "Note 1 lengths: %s. |" % ("; ".join("%s PDF p%s %d characters" % t for t in notes1) or "n/a"))
    nopage = [(r["department"], p["pdf_page"]) for r in results for p in r["pages"] if not p["booklet_page"]]
    L.append("| Nine pages have a missing or ambiguous printed page number | There are %d (the earlier text said nine). | Wording corrected; the "
             "list in the page map was already right. | %s. |" % (
                 len(nopage), "; ".join("%s p%d" % (d, pg) for d, pg in nopage)))
    kp = {d: sum(1 for p in by[d]["pages"] if p["class"] == "zoning keyplan") for d in ("Palliative", "Psych Med") if d in by}
    L.append("| Palliative p11-p18 and Psych Med p21-p29 are classed `other` and not read | Palliative p12 and Psych Med p24 carry the "
             "strip title `4.1.10 AV STRATEGY` and a storey KEYPLAN title, so they were classed `zoning keyplan` and added 100 and 137 rows to "
             "`booklet_zoning_labels.csv`. | The section title in the strip at the top right (`section_strip`) is read first: a page whose "
             "strip is not `... MECHANICAL VENTILATION STRATEGY` is `other` (AV, communication, IT). | Keyplan pages now: %s. Zoning labels: %s. |" % (
                 ", ".join("%s %d" % kv for kv in kp.items()),
                 ", ".join("%s %d" % (d, len(by[d]["zoning"])) for d in ("Palliative", "Psych Med") if d in by)))
    L.append("")
    strikes = [(r["department"], r["pdf_page"], r["room_type_as_printed"], r["confidence"])
               for res in results for r in res["criteria"] if r["reviewer_markup"]]
    linked = [(r["department"], r["pdf_page"], r["room_type_as_printed"]) for res in results for r in res["criteria"]
              if r["reviewer_comments"]]
    markup = [(res["department"], n["pdf_page"], n["text"]) for res in results for n in res["notes"]
              if n["note_kind"] == "reviewer markup"]
    L += ["### Reviewer strike-through and marks with no text (finding F4)", "",
          "- `page_annotations` skipped every annotation without contents, so a struck-through row read as current. Annotations without text "
          "are now read by `page_markup`: a thin horizontal Line (or a StrikeOut) that crosses the room text of a row marks that row; "
          "a FreeText comment whose callout arrow ends inside a row is linked to it.",
          "- `criteria.csv` has two new columns at the end, `reviewer_markup` and `reviewer_comments` (blank on every IP FAST and IP SURGICAL row); "
          "a struck row keeps its values as printed and its confidence is capped at 0.50.",
          "- Struck rows: %s." % ("; ".join("%s PDF p%s %s (confidence %s)" % t for t in strikes) or "none"),
          "- Rows with a linked comment: %s." % ("; ".join("%s PDF p%s %s" % t for t in linked) or "none"),
          "- Marks with no text are also written to `criteria_notes.csv` (`reviewer markup`, a strike names the row) and, on pressure regime and "
          "keyplan pages, to `booklet_pressure_labels.csv` / `booklet_zoning_labels.csv` (`reviewer_markup`): %s." % (
              "; ".join("%s p%s %s" % t for t in markup[:12]) + (" and %d more" % (len(markup) - 12) if len(markup) > 12 else "")), ""]
    cw = sorted(r["department"] for res in results for r in res["criteria"] if r["room_type_norm"] in ("clinical workroom",))
    cl = sorted(r["department"] for res in results for r in res["criteria"] if r["room_type_norm"] in ("cleaner room",))
    L += ["### Gaps the verifier listed", "",
          "- Only 11 room data rows exist (Dialysis 1, Healthy Aging 2, ISO AH 8), so all were re-read by the verifier; no sample was possible.",
          "- A synthetic test now puts a table row on the footer stamp line and a note line below 0.93 of the page height (they fail on the earlier code).",
          "- Cross-department comparison: Palliative is now in the Clinical Workroom group (%s) and the Cleaner Room group (%s). "
          "Its values there equal the others, so the flagged differences did not change." % (", ".join(cw), ", ".join(cl)),
          "- Standby rows of Healthy Aging (8), ISO AH (12) and Psych Med (13) were re-derived by token: every word of the table body (below the header, "
          "above the NOTE block, comments removed) is in exactly one row's tag, area, configuration or heading, and no row holds a word that is not "
          "in the table. Dialysis (2) and Palliative (12) pass the same check. The level and wing coded in each tag agree with the heading of its row.",
          "- Zoning and pressure label pages of the five extracts were re-derived by token: no label holds a word that is not on its page, and every word "
          "missing from the registers is page furniture (the strip titles, the PRESSURE REGIME heading, the NOTE block that goes to `criteria_notes.csv`) "
          "except three drawing words on Palliative p10 (`1`, `A`, `A` of `1 BED: A CLASS`) that lie under an opaque reviewer comment box and are treated as "
          "comment text.",
          "- Healthy Aging p23 and p25-p26 (outpatient pages): checked. p23 is a two-row equipment standby table (`AHU-A-10-01/04`, `AHU CONFIG-D` and "
          "`AHU CONFIG-F`); p25-p26 are an OP `Operation Mode` criteria table (`+VE`, `-VE`, `YES`, `24 hrs AC`) in a different layout with the room names "
          "wrapped over several lines. They stay unparsed; the two p25 reviewer comments are now recorded. One of them (20251201 DPE: all rooms update to Neutral, "
          "only DU, Disposal and Toilet negative) disagrees with the `+VE` printed in the table, so the table values are under review and are not carried into any register.", "",
          "### Tests added", "",
          "- `test_row_on_the_footer_stamp_line_is_read`, `test_note_text_below_the_old_cut_off_is_kept_verbatim`, "
          "`test_footer_furniture_is_the_stamp_and_the_page_number_only`.",
          "- `test_page_of_another_section_is_other_even_with_a_keyplan_title`.",
          "- `test_strike_through_and_callout_are_linked_to_the_row`, `test_markup_without_text_on_a_pressure_page_is_recorded`, "
          "`test_page_markup_marks_only_thin_horizontal_lines_as_strikes`.", ""]
    return L


def n_is_table_note1(n: Dict[str, Any]) -> bool:
    return n["note_kind"] == "table note" and n["note_number"] == "1"


def _annot_cell(p: Dict[str, Any]) -> str:
    """Page map 'Annots' cell: reviewer comments, and marks with no text (strike-through, box, arrow) after a plus."""
    n, m = p.get("annotations") or 0, p.get("markup") or 0
    return ("%d" % n if n else "") + (("%s+%d no text" % (" " if n else "", m)) if m else "")


def build_report(results: List[Dict[str, Any]], notes_all: List[Dict[str, Any]]) -> str:
    """The QA report as Markdown."""
    L: List[str] = ["# Booklet Section 4.8 parser report", "",
                    "Generated by `python3 -m src.booklet48_parser run` on %s." % common.utc_now_iso(), "",
                    "Values come from the text layer (words with boxes); zero-width spaces are stripped and "
                    "every figure is kept as printed beside its normalised value in `criteria.csv`. "
                    "Bounding boxes are in PDF points (x0, y0, x1, y1; origin top left).", ""]
    L += ["## Page map", "",
          "Pages per department by class (a page is classed from its big-type heading and its text; "
          "`Annots` is the number of reviewer comments, i.e. PDF annotations with text, on the page, and after a plus the "
          "number of reviewer marks with no text):", "",
          "| Department | Pages | " + " | ".join(PAGE_CLASSES) + " |",
          "|---|---|" + "---|" * len(PAGE_CLASSES)]
    for res in results:
        counts = defaultdict(int)
        for p in res["pages"]:
            counts[p["class"]] += 1
        L.append("| %s | %d | %s |" % (res["department"], len(res["pages"]),
                                       " | ".join(str(counts[c]) if counts[c] else "" for c in PAGE_CLASSES)))
    L.append("")
    for res in results:
        L += ["### %s (%s)" % (res["department"], res["source_file"]), "",
              "| PDF page | Booklet page | Class | Rows | Annots | Detail |", "|---|---|---|---|---|---|"]
        for p in res["pages"]:
            L.append("| %d | %s | %s | %s | %s | %s |" % (p["pdf_page"], p["booklet_page"] or "not in text layer",
                                                        p["class"], p["rows"] if p["rows"] else "",
                                                        _annot_cell(p), md(p["detail"])))
        L.append("")
    L += ["## Row counts", "",
          "| Department | Criteria rows | Room data rows | Standby rows | Criteria notes (table / pressure and pandemic / "
          "room data / reviewer comment / reviewer mark) | Zoning labels (tags) | Pressure labels |",
          "|---|---|---|---|---|---|---|"]
    for res in results:
        ntags = sum(1 for z in res["zoning"] if z["label_kind"] == "equipment_tag")
        kinds = defaultdict(int)
        for n in notes_all:
            if n["file"] == res["source_file"]:
                kinds[n["note_kind"]] += 1
        split = "%d / %d / %d / %d / %d" % (kinds["table note"], kinds["pressure regime note"] + kinds["pandemic mode note"],
                                            kinds["room data table note"], kinds["reviewer annotation"],
                                            kinds["reviewer markup"])
        L.append("| %s | %d | %d | %d | %s | %d (%d) | %d |" % (
            res["department"], len(res["criteria"]), len(res["room_data"]), len(res["standby"]), split,
            len(res["zoning"]), ntags, len(res["pressure"])))
    L += ["", "Criteria rows per table part:", ""]
    for res in results:
        parts: Dict[str, int] = defaultdict(int)
        for r in res["criteria"]:
            parts[r["table_part"].split(";")[0]] += 1
        L.append("- %s: %s" % (res["department"], ", ".join("%s = %d" % kv for kv in sorted(parts.items())) or "none"))
    L += ["", "Zoning keyplan colour legend (text beside swatches, or a raster legend image whose text is not in the text layer): "
          + "; ".join("%s p%s %s" % (r["department"], p["booklet_page"] or "?", "yes" if "colour legend yes" in p["detail"] else "no")
                      for r in results for p in r["pages"] if p["class"] == "zoning keyplan"), ""]
    nopage = ["%s PDF p%d (%s)" % (r["department"], p["pdf_page"], p["class"])
              for r in results for p in r["pages"] if not p["booklet_page"]]
    if nopage:
        L += ["%d pages have a printed page number that is not in the text layer or is ambiguous (left blank; nothing is "
              "inferred from the sequence): %s." % (len(nopage), "; ".join(nopage)), ""]

    L += ["## Criteria notes (verbatim)", "",
          "`criteria_notes.csv` holds every free-text note that bears on the criteria: the numbered NOTE block under a "
          "criteria table (`table note`, an asterisk footnote has number `*`), the un-numbered NOTE block of a pressure regime page "
          "(`pressure regime note`, `pandemic mode note`), the notes of the room data table page, and the reviewer comments "
          "(`reviewer annotation`, PDF FreeText annotations) found on criteria, room data and pressure regime pages. "
          "Reviewer marks with no text (a strike-through Line, a Square, a Polygon) are recorded as `reviewer markup`, with the row struck "
          "through named in the text. Reviewer comments on pressure regime pages are also kept in `booklet_pressure_labels.csv`.", "",
          "| Department | Booklet p. | PDF p. | Kind | No. | Author | Text |", "|---|---|---|---|---|---|---|"]
    for n in notes_all:
        L.append("| %s | %s | %s | %s | %s | %s | %s |" % (n["department"], n["page"] or "?", n["pdf_page"],
                                                          n["note_kind"], n["note_number"], n["author"] or "", md(n["text"])))
    L.append("")

    fast = [r for r in results if r["department"].upper() == "IP FAST"]
    L += ["## IP FAST criteria, every row", ""]
    if not fast:
        L += ["IP FAST was not among the files parsed.", ""]
    for res in fast:
        L += ["Columns after the room type are as printed; the normalised values are in `out/registers/criteria.csv`. "
              "`Notes` are the footnote references that apply to the row (the header carries note 1 on the afterhours column).", "",
              "| Booklet p. | PDF p. | Part | Room type (as printed) | Pressure | OA ACH | Total ACH | Single pass | Air class | Filter | RH | Temp, C | After-hours | Setback | Notes | bbox | Conf. |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in res["criteria"]:
            raw = json.loads(r["raw_cells"])
            L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                r["booklet_page"], r["pdf_page"], r["table_part"], r["room_type_as_printed"],
                r["pressure_regime_as_printed"], raw["min_oa"], raw["min_total"], raw["single_pass"],
                r["air_class"], r["filter"], r["rh_as_printed"], r["temp_as_printed"], raw["afterhours"],
                raw["setback"], r["note_refs"], r["bbox"], r["confidence"]))
        L.append("")

    L += ["## Fields that failed", ""]
    any_fail = False
    for res in results:
        fails = res["failures"]
        pfails = res["page_failures"]
        if not fails and not pfails:
            continue
        any_fail = True
        L += ["### %s" % res["department"], ""]
        if fails:
            L += ["| PDF page | Booklet page | Row | Field | Cell as printed | bbox |", "|---|---|---|---|---|---|"]
            for f in fails:
                L.append("| %s | %s | %s | %s | %s | %s |" % (f["pdf_page"], f["booklet_page"], md(f["row"]), f["field"],
                                                           md(f["cell"] or "(blank)"), f["bbox"]))
            L.append("")
        for pg, msg in pfails:
            L.append("- PDF page %s: %s" % (pg, msg))
        L.append("")
    if not any_fail:
        L += ["No field failed to parse.", ""]
    L += ["Not failures, by design: `NR` (not required/recorded) and `-` cells are valid and give a blank "
          "normalised value (single pass `NR` is kept as `NR`); a pressure regime given only in words "
          "(Neutral, Negative, Positive) has a direction but a blank `pressure_pa` because no Pa value is printed; "
          "a single design temperature (for example `27`) is stored as min = max.", ""]
    L += ["## Parsing decisions to confirm", "",
          "- Section rows inside a criteria table (`Hybrid Cooling`, `Pandemic Mode`: a coloured band across the table, "
          "no room or pressure text) hold for every row below them until the next section row or the end of the table; "
          "the heading is appended to `table_part` (for example `Part 2; Hybrid Cooling`). The rendered page shows the "
          "`Corridor` row inside the Hybrid Cooling section, so it carries that mode.",
          "- Pressure direction is left blank for `NR` cells; `pressure_pa` is blank wherever only a word "
          "(Neutral, Negative, Positive) is printed.",
          "- Equipment standby notes (1 and 2) are written to `booklet_standby_notes.csv`; the references printed on the "
          "Configuration header (`1,2`) are carried on every standby row as `note_refs`.", ""]
    L += _second_batch_section(results)
    L += ["## Differences between departments for the same room type", ""]
    L += compare_departments(results)
    L += _corrections_section(results)
    L += _corrections_section_2(results)
    return "\n".join(L).rstrip() + "\n"


def run(paths: Optional[Sequence[str]] = None, quiet: bool = False) -> Dict[str, Any]:
    """Parse the booklets and write registers and the QA report."""
    root = common.project_root()
    pth = common.resolve_paths()
    registers = pth.get("registers", root / "out" / "registers")
    qa = pth.get("qa", root / "out" / "qa")
    inputs = collect_inputs(paths, root)
    results = [parse_pdf(p, root) for p in inputs]
    criteria = [r for res in results for r in res["criteria"]]
    notes = [n for res in results for n in res["notes"]]
    standby = [r for res in results for r in res["standby"]]
    standby_notes_rows = [n for res in results for n in res["standby_notes"]]
    zoning = [r for res in results for r in res["zoning"]]
    pressure = [r for res in results for r in res["pressure"]]
    room_data = [r for res in results for r in res["room_data"]]
    common.write_csv_dicts(registers / "criteria.csv", criteria, CRITERIA_FIELDS)
    common.write_csv_dicts(registers / "booklet_room_data.csv", room_data, ROOM_DATA_FIELDS)
    common.write_csv_dicts(registers / "criteria_notes.csv", notes, NOTES_FIELDS)
    common.write_csv_dicts(registers / "booklet_equipment_standby.csv", standby, STANDBY_FIELDS)
    common.write_csv_dicts(registers / "booklet_standby_notes.csv", standby_notes_rows, STANDBY_NOTES_FIELDS)
    common.write_csv_dicts(registers / "booklet_zoning_labels.csv", zoning, ZONING_FIELDS)
    common.write_csv_dicts(registers / "booklet_pressure_labels.csv", pressure, PRESSURE_FIELDS)
    common.atomic_write_text(qa / "booklet48_parser_report.md", build_report(results, notes))
    summary = {
        "files": [r["source_file"] for r in results],
        "criteria_rows": {r["department"]: len(r["criteria"]) for r in results},
        "notes": {r["department"]: len(r["notes"]) for r in results},
        "standby_rows": {r["department"]: len(r["standby"]) for r in results},
        "zoning_labels": {r["department"]: len(r["zoning"]) for r in results},
        "pressure_labels": {r["department"]: len(r["pressure"]) for r in results},
        "room_data_rows": {r["department"]: len(r["room_data"]) for r in results},
        "failed_fields": {r["department"]: len(r["failures"]) + len(r["page_failures"]) for r in results},
        "results": results,
    }
    if not quiet:
        printable = {k: v for k, v in summary.items() if k != "results"}
        print(json.dumps(printable, indent=2, ensure_ascii=False))
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Parse Section 4.8 short DD booklets.")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="parse booklets and write registers and the QA report")
    run_p.add_argument("--paths", nargs="*", default=None,
                       help="PDF files or folders (default: every PDF under data/source/%s/)" % BOOKLET_DIR_NAME)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            run(args.paths)
            return 0
    except Exception as exc:  # noqa: BLE001
        print("error: %s" % exc, file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
