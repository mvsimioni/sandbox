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
    "department", "dept_no", "department_label", "source_file", "booklet_page", "pdf_page", "table_part", "zone",
    "room_type_as_printed", "room_type_norm",
    "pressure_regime_as_printed", "pressure_pa", "pressure_pa_min", "pressure_pa_max", "pressure_direction",
    "min_oa_ach", "min_total_ach", "single_pass", "air_class", "filter",
    "rh_as_printed", "rh_max_pct", "rh_min_pct",
    "temp_as_printed", "temp_min_c", "temp_max_c",
    "afterhours", "setback", "note_refs", "bbox", "confidence", "raw_cells",
    "reviewer_markup", "reviewer_comments",
]
NOTES_FIELDS = ["file", "page", "note_number", "text", "booklet_page", "pdf_page", "bbox",
                "department", "dept_no", "department_label", "note_kind", "author", "date"]
# note_kind: table note (numbered NOTE block under a criteria table) | room data table note |
#            pressure regime note | pandemic mode note (un-numbered NOTE block of a pressure regime page) |
#            reviewer annotation (a PDF comment on a criteria, room data or pressure regime page) |
#            reviewer markup (a PDF Line / Square / Polygon annotation with no text, e.g. a strike-through)
STANDBY_FIELDS = ["department", "dept_no", "department_label", "source_file", "booklet_page", "pdf_page", "level_wing_heading",
                  "tag_as_written", "tag_normalised", "area", "configuration", "note_refs", "bbox", "confidence"]
STANDBY_NOTES_FIELDS = NOTES_FIELDS      # note_kind there: table note | reviewer annotation
ROOM_DATA_FIELDS = ["department", "dept_no", "department_label", "source_file", "booklet_page", "pdf_page", "row",
                    "d_template_room_code", "d_room_name", "d_unique_room_identifier", "item_description",
                    "qty03_v12", "type_of_tms", "d_template_room_name", "other_columns",
                    "columns_as_printed", "bbox", "confidence"]
ZONING_FIELDS = ["department", "dept_no", "department_label", "source_file", "booklet_page", "pdf_page", "keyplan_title",
                 "label_kind", "text", "block_text", "bbox", "has_colour_legend", "legend_source", "legend_bbox"]
PRESSURE_FIELDS = ["department", "dept_no", "department_label", "source_file", "booklet_page", "pdf_page", "page_heading",
                   "label_kind", "text", "bbox", "annotation_author", "annotation_date"]
# label_kind values: text_label | pressure_value (drawing text) | reviewer_annotation (a PDF comment, not drawing text) |
#                    reviewer_markup (a PDF Line / Square / Polygon annotation with no text)

# header keyword -> criteria field
HEADER_KEYS: List[Tuple[str, str]] = [
    ("room", r"^(?:area\s*/\s*)?room\b"),
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
# Emergency (section 4.1.8) criteria tables: the heading 'Design Temperature, C' wraps mid-word ('Temperatur' / 'e, C')
ZONED_HEADER_VOCAB = HEADER_VOCAB | {"rooms", "temperatur", "e,"}
# ICU HDU (4.8): the table prints no caption and every heading wraps mid-word in its cell ('Pressure Re' / 'gime, Pa',
# 'Min. Outdo' / 'or ACH', 'Relative Hu' / 'midity', 'Design Tem' / 'perature, C', 'Setback mo' / 'de, Y/N'); used only
# when the standard header is not found, so no other file reads differently
SPLIT_HEADER_VOCAB = HEADER_VOCAB | {"re", "gime,", "outdo", "or", "hu", "midity", "tem", "perature,", "mo", "de,"}
# I-CARE (4.8): the first heading is 'AREA / ROOM' and the table sits under a 28 pt 'ACMV' heading with the operating mode
# ('I-Care - Operation Mode') as its subtitle; used only when neither earlier header is found
AREA_HEADER_VOCAB = HEADER_VOCAB | {"area", "/", "area/room", "department", "temperatur", "e"}
# The six short DD booklets (A4, see ``page_xform``) wrap the headings mid-word in their cells ('Afterhour' / 's',
# 'Design Te' / 'mperatur' / 'e, C', 'Relative H' / 'umidity', 'Min. Outd' / 'oor ACH'), may head the first column
# 'Theatre', 'Department' or 'AREA / ROOM', and LabMed adds a 'Min. Total ACH (Purging)' column; used only after
# the three header vocabularies above found nothing
SHORT_HEADER_VOCAB = SPLIT_HEADER_VOCAB | AREA_HEADER_VOCAB | {
    "theatre", "afterhour", "s", "te", "mperatur", "mperature", "h", "umidity", "m", "ode,", "outd", "oor",
    "(purging)", ",c", "rooms", "re", "outdoo", "r", "e,"}
SHORT_EXTRA_KEYS: List[Tuple[str, str]] = [("min_total_purging", r"purging")]
# CSSD (4.8, A3): a clean text layer whose headings wrap mid-word ('Temperat' / 'ure, C', 'Afterhour' / 's mode,') and whose
# Afterhours heading carries the footnote reference '1,2'; used only after the vocabularies above found nothing
WRAP_HEADER_VOCAB = HEADER_VOCAB | {"temperat", "ure,", "afterhour", "s", "1,2"}
FOOTNOTE_REF_RE = re.compile(r"\d(?:,\d)*")
NO_CAPTION_PART = "(no caption printed)"
ZONED_CAPTION_RE = re.compile(r"^Rooms?\s+Part\s*(\d+)(?:\s*[–—\-]\s*(.+))?$", re.I)
# Zones of the Emergency criteria table.  The table prints no zone column: the zone is the leading words of the room
# name ('P2 South NP Toilet' is in zone 'P2 South NP').  Longest prefix first; the pattern is matched against the
# room name as printed, the label is the zone as written in the booklet (the pressure regime pages use the same names:
# P3, FEVER / ISOL FEVER, RESUS, P2 North / South, EDTU, CAC, Staff Support).
ZONE_PREFIXES: Tuple[Tuple[str, str], ...] = (
    ("P2 South Neutral", r"P2\s+South\s+Neutra(?:l)?\b"),      # 'P2 South Neutra Staff Station / Linen' is printed with the 'l' missing
    ("P2 South NP", r"P2\s+South\s+NP\b"),
    ("P2 North", r"P2\s+North\b"),
    ("P2", r"P2\b"),
    ("P3", r"P3\b"),
    ("CAC FEVER", r"CAC\s+FEVER\b"),
    ("CAC", r"CAC\b"),
    ("FEVER", r"FEVER\b"),
    ("RESUS", r"RESUS\b"),
    ("EDTU", r"EDTU\b"),
    ("Staff Support", r"Staff\s+Support\b"),
)
TAG_RE = re.compile(r"^[A-Z]{2,6}(?:-[A-Z0-9]+){2,}$")
FURNITURE_LINE_RE = re.compile(
    r"^(4\s+DEPARTMENT DRAWINGS|4\.8\s+MECHANICAL VENTILATION STRATEGY.*|`|"
    r"ALEXANDRA HOSPITAL REDEVELOPMENT PROJECT.*|BINDING GUTER.*)$", re.I)
# keyplan labels of a duty / standby pair read 'AHU-B-02-01EDP3WAIT-01A/B' (Emergency); the standby table lists '-01A' and '-01B'
PAIR_TAG_RE = re.compile(r"^[A-Z]{2,6}(?:-[A-Z0-9]+){2,}(?:/[A-Z0-9]+)?$")
# a tag that serves two units is printed 'AHU-B-07-04ICU-01/02C' (ICU HDU): the standby reader takes the '/' suffix as part of it
STANDBY_TAG_RE = re.compile(r"^[A-Z]{2,6}(?:-[A-Z0-9]+){2,}(?:/[A-Z0-9]+)*$")
SLIGHT_RE = re.compile(r"^SLIGHT(?:LY)?$", re.I)
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


A3_WIDTH = 1191.0       # every long booklet is A3 landscape; the geometry thresholds below are in that page space


def page_xform(page: "pymupdf.Page") -> Optional["pymupdf.Matrix"]:
    """Matrix from the stored page space to the page space the parser works in, or ``None`` (the page is used as is).

    The six short DD booklets (AMSC, CATH, DSOT, Endo, LabMed, MSOT) are A4 pages stored portrait with /Rotate 90, so
    the text layer comes out turned on its side; they are turned upright and enlarged to the A3 width of the other
    booklets, so that the one set of geometry thresholds reads every file.  Registers carry the boxes back in A4
    points (see ``unscale_boxes``)."""
    if not page.rotation:
        return None
    m = pymupdf.Matrix(page.rotation_matrix)
    if page.rect.width < 900.0:
        k = A3_WIDTH / page.rect.width
        m = m * pymupdf.Matrix(k, k)
    return m


def page_scale(page: "pymupdf.Page") -> float:
    """Enlargement ``page_xform`` applies (1.0 for the A3 booklets)."""
    return A3_WIDTH / page.rect.width if page.rotation and page.rect.width < 900.0 else 1.0


def page_words(page: "pymupdf.Page") -> List[Word]:
    """Words of a page built from characters: U+200B is dropped from text and boxes.

    A zero-width space that sits across a visible gap (more than 1.5 points between the
    neighbouring glyphs) also ends the word, so ``Room<ZWSP>Neutral`` stays two words.
    """
    out: List[Word] = []
    zero_width = "\u200b\u200c\u200d\u2060\ufeff"
    rot = page_xform(page)      # /Rotate pages (the six short DD booklets): text is stored unrotated
    k = page_scale(page)
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
                    if rot is not None:
                        bbox = tuple(pymupdf.Rect(bbox) * rot)
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
                    cur.append((c, bbox, size * k))
                    state["last_x1"] = bbox[2]
            flush()
    out = [w for w in out if clean(w.text)]
    if rot is not None:
        out = _drop_double_print(out)
    return out


def _drop_double_print(words: List[Word]) -> List[Word]:
    """Keep only the visible layer of a page whose text is laid down twice (LabMed p6: the second layer, 12 points right
    and 2.4 points down, is painted over the first, and the two differ here and there: Air Class '3' under '4').

    Evidence first: at least eight same-text words must have a twin to the right (up to 16 points) on nearly the same
    line, and they must agree on one offset (within 1.5 points), else the page is left as it is.  Every word that has any
    word at that offset from it is in the covered layer and is dropped; the layer on top stays, as printed."""
    by_text: Dict[str, List[Word]] = defaultdict(list)
    for w in words:
        by_text[w.text].append(w)
    offsets: List[Tuple[float, float]] = []
    for ws in by_text.values():
        for a in ws:
            for b in ws:
                if 4.0 <= b.x0 - a.x0 <= 16.0 and abs(b.cy - a.cy) < 4.0:
                    offsets.append((b.x0 - a.x0, b.cy - a.cy))
    if len(offsets) < 8:
        return words
    dx = sorted(o[0] for o in offsets)[len(offsets) // 2]
    dy = sorted(o[1] for o in offsets)[len(offsets) // 2]
    if sum(1 for o in offsets if abs(o[0] - dx) <= 1.5 and abs(o[1] - dy) <= 1.5) < 8:
        return words
    grid: Dict[Tuple[int, int], List[Word]] = defaultdict(list)
    for w in words:
        grid[(int(w.x0 // 4), int(w.cy // 4))].append(w)

    def covered(a: Word) -> bool:
        gx, gy = int((a.x0 + dx) // 4), int((a.cy + dy) // 4)
        return any(abs(b.x0 - (a.x0 + dx)) <= 1.5 and abs(b.cy - (a.cy + dy)) <= 1.5 and b is not a
                   for ix in (gx - 1, gx, gx + 1) for iy in (gy - 1, gy, gy + 1) for b in grid.get((ix, iy), []))

    return [w for w in words if not covered(w)]


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


def join_room_words(words: Sequence[Word]) -> str:
    """Join a wrapped room name.  Where the cell breaks a line inside a word the pieces are closed up: after a hyphen
    (ICU HDU ``Multi-`` / ``Purpose Room`` reads ``Multi-Purpose Room``) and before a stranded lower-case letter
    (Pharmacy ``Preparation/Inspectio`` / ``n Room`` reads ``Preparation/Inspection Room``).  Words on one line are joined
    with a space as before."""
    out = ""
    prev: Optional[Word] = None
    for line in cluster_lines(words):
        for i, w in enumerate(line):
            if i == 0 and prev is not None:
                if re.search(r"[A-Za-z]-$", prev.text) and re.match(r"[A-Za-z]", w.text):
                    out += w.text
                    prev = w
                    continue
                if re.fullmatch(r"[b-z]", w.text) and len(line) > 1 and re.search(r"[A-Za-z]{4}$", prev.text):
                    out += w.text
                    prev = w
                    continue
            out += (" " if out else "") + w.text
            prev = w
    return clean(out)


def is_gutter(word: Word) -> bool:
    """The rotated 'BINDING GUTER' strip along the left edge."""
    # the A4 short booklets, enlarged to A3 width, set the 24 pt gutter note a little wider (x1 about 57)
    return word.x1 < 50.0 or (word.x0 < 40.0 and word.x1 < 62.0 and 23.0 <= word.size < 25.0)


MONTHS = ("JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST", "SEPTEMBER", "OCTOBER",
          "NOVEMBER", "DECEMBER",
          "JAN", "FEB", "MAR", "APR", "JUN", "JUL", "AUG", "SEP", "SEPT", "OCT", "NOV", "DEC")   # 'APR 2025', 'JUL 2025', 'AUG 2025' stamps


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


def printed_page_box(words: Sequence[Word], page_h: float, page_w: float) -> Optional[Tuple[float, float, float, float]]:
    """Box of the printed page number (the digit word that ``printed_page_number`` returns), or ``None``."""
    number = printed_page_number(words, page_h, page_w)
    if not number:
        return None
    cands = [w for w in words if w.text == number and w.y0 > page_h * 0.93 and not _follows_month(w, words)]
    return max(cands, key=lambda w: w.x0).box() if cands else None


def page_number_absent(page: "pymupdf.Page", ref_box: Tuple[float, float, float, float]) -> bool:
    """True when the place where this booklet prints its page number (``ref_box``, taken from a page that has one)
    is empty on the rendered page: no ink at all, so the number is not printed (neither as text nor as a picture)."""
    pad = 3.0
    clip = pymupdf.Rect(ref_box[0] - pad, ref_box[1] - pad, ref_box[2] + pad, ref_box[3] + pad) & page.rect
    if clip.is_empty:
        return False
    pix = page.get_pixmap(clip=clip, dpi=144, alpha=False, colorspace=pymupdf.csGRAY)
    dark = sum(1 for v in pix.samples if v < 170)
    return dark <= 2


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


def plan_heading_words(words: Sequence[Word]) -> List[Word]:
    """The page heading of a plan page set in 24 pt at the top left ('PRESSURE REGIME - BAU (LEVEL 4)', ICU HDU), where
    the other booklets set it in the 28 pt type of ``large_heading``.  The section strip (x >= 735) and the 24 pt binding
    gutter note are not part of it."""
    return [w for w in words if 20.0 <= w.size < 26.0 and w.x0 < 735 and w.y0 < 140 and not is_gutter(w)]


def section_strip(words: Sequence[Word]) -> str:
    """Section title in the strip at the top right ('4.8 MECHANICAL VENTILATION STRATEGY | DESIGN CRITERIA',
    '4.1.10 AV STRATEGY'), 20 pt type; the 28 pt line above it is 'DEPARTMENT DRAWINGS'."""
    return join_words([w for w in words if w.x0 >= 650 and w.y0 < 90 and 14 <= w.size < 26 and not is_gutter(w)])


# Files the newer page recognitions do not touch, so that their registers stay as released: the standby table titled
# 'Equipment Configuration Summary' or untitled, and the criteria table recognised by its header alone (no 'DESIGN
# CRITERIA' strip or caption).  Healthy Aging p23 (two standby rows) and p25-p26 (an OP operation-mode table in another
# layout) stay unparsed; remove the file name here to read them.
FROZEN_LAYOUT_FILES = ("Healthy_Aging_Geriatrics_Section_4.8_Mechanical_Ventilation_Strategy.pdf",)


def classify_page(text: str, heading: str = "", section: str = "", extended: bool = True) -> str:
    """Class a page from its heading text (whitespace-collapsed).

    ``heading`` is the big-type heading (see ``large_heading``).  A page whose heading reads
    PRESSURE REGIME is a pressure regime page even when a keyplan title is printed on the drawing.
    ``section`` is the section strip (see ``section_strip``): a page of another section of the booklet
    (4.1.10 AV STRATEGY, 4.1.11 COMMUNICATION STRATEGY, 4.9 IT STRATEGY) is ``other`` whatever it prints, so an
    AV overview that carries a storey KEYPLAN title is not read as an AHU zoning keyplan.
    """
    flat = re.sub(r"\s+", " ", clean(text))
    if section and not re.search(r"MECHANICAL VENTILATION", section, re.I) \
            and not re.match(r"(PRESSURE REGIME|AHU ZONING)", heading):     # a plan headed so under a stale strip (DI: '4.1 DEPARTMENT LAYOUT PLAN')
        return "other"
    if re.search(r"D_Template\s*Room\s*Code", flat) and re.search(r"D_Unique\s*Room\s*Identifier", flat):
        return "room data table"
    if heading and re.search(r"PRESSURE REGIME", heading) and not re.search(r"DESIGN CRITERIA", flat):
        return "pressure regime"
    if section and re.search(r"\|?\s*PRESSURE REGIME", section) and not re.search(r"DESIGN CRITERIA", flat):
        return "pressure regime"            # the title is part of the section strip (MDU OCP: '... STRATEGY | PRESSURE REGIME')
    if re.search(r"DESIGN CRITERIA", flat) or re.search(r"Room conditions\s*[–\-]\s*Part", flat, re.I):
        return "criteria"
    if extended and re.search(r"Pressure\s*Regime", flat) and re.search(r"Single\s*Pass", flat) \
            and re.search(r"Min\.?\s*Total", flat) and (re.search(r"AREA\s*/\s*ROOM", flat, re.I) or re.search(r"Afterhours", flat, re.I)):
        return "criteria"       # I-CARE, Pharmacy: the table has no 'DESIGN CRITERIA' strip or 'Room conditions' caption
    if re.search(r"EQUIPMENT STANDBY CONFIGURATION SUMMARY", flat, re.I) \
            or (extended and (re.search(r"EQUIPMENT CONFIGURATION SUMMARY", flat, re.I)
                              or re.search(r"Equipment Tag\s+Area Served\s+Equipment Standby\s+Configuration", flat))):
        return "standby summary"
    if extended and re.search(r"KEYPLAN", flat) and not re.search(r"AHU ZONING", flat) \
            and len(re.findall(r"[+\-−–]?\d+(?:\.\d+)?\s*Pa\b", flat)) >= 3 and len(re.findall(r"\d+\s*ACH\b", flat)) >= 3:
        return "pressure regime"        # CSSD p6: a keyplan headed by no title whose labels are 'N Pa' / 'N ACH' pressure and air-change values
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
    sign_only = re.fullmatch(r"(?:slight(?:ly)?\s+)?([+\-−–])\s*ve", low)   # '-ve' / '+ve' / 'SLIGHT +VE': a direction with no figure
    if sign_only:
        return "", ("negative" if sign_only.group(1) != "+" else "positive"), True
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
    if re.fullmatch(r"SS\s*553", t, re.I):      # DI: the figure is 'as per SS 553' (the Singapore Standard); no number is printed
        return "", True
    m = re.fullmatch(r"(\d+(?:\.\d+)?)", t)
    return (_num(float(m.group(1))), True) if m else ("", False)


def parse_single_pass(cell: str) -> Tuple[str, bool]:
    t = re.sub(r"\s*-\s*", "-", clean(cell).lower())
    if t in ("yes", "y"):
        return "Y", True
    if t == "non-recirculating":      # Emergency 4.1.8 prints the word for air that is not recirculated: single pass
        return "Y", True
    if t in ("no", "n") or re.fullmatch(r"no \(c/w air purging\)", t):   # DI: 'No (c/w Air Purging)' is no single pass; the words stay in raw_cells
        return "N", True
    if t in ("nr", "n/r", "", "-"):         # 'N/R' (ICU HDU) is 'not required', as 'NR'
        return "NR", True
    if t in ("na", "n/a"):                  # CSSD: 'NA', not applicable (as parse_yn)
        return "NA", True
    return "", False


def parse_yn(cell: str) -> Tuple[str, bool]:
    t = clean(cell).lower()
    if t in ("y", "yes"):
        return "Y", True
    if t in ("n", "no"):
        return "N", True
    if t in ("nr", ""):
        return t.upper(), True
    if t in ("na", "n/a"):                          # DI: 'NA', not applicable
        return "NA", True
    if re.fullmatch(r"24\s*hrs?\s*ac", t):       # I-CARE: the room is air-conditioned for 24 hours; not a Y/N flag, kept as printed
        return "24 hrs AC", True
    return "", False


def parse_rh(cell: str) -> Tuple[str, str, bool]:
    """Return ``(rh_max, rh_min, ok)`` from e.g. 'Max 60%', '40 - 60%', 'NR'."""
    t = clean(cell)
    if t.upper() in ("", "NR", "N/R", "-", "N", "NA", "N/A"):       # 'N': no humidity control (Emergency 4.1.8 toilets and dirty utilities)
        return "", "", True
    rng = re.fullmatch(r"(\d+(?:\.\d+)?)\s*%?\s*[–—\-]\s*(\d+(?:\.\d+)?)\s*%?", t)
    if rng:
        a, b = float(rng.group(1)), float(rng.group(2))
        return _num(max(a, b)), _num(min(a, b)), True
    m = re.fullmatch(r"(max|min|≤|<=|≥|>=)\.?\s*(\d+(?:\.\d+)?)\s*%?", t, re.I)
    if m:
        v = _num(float(m.group(2)))
        return (v, "", True) if m.group(1).lower() in ("max", "≤", "<=") else ("", v, True)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*%", t)
    if m:
        return _num(float(m.group(1))), "", True
    return "", "", False


def parse_temp(cell: str) -> Tuple[str, str, bool]:
    """Return ``(min_c, max_c, ok)`` from '21 - 24', '27', '24+-1', 'NR'."""
    t = clean(cell)
    if t.upper() in ("", "NR", "N/R", "-", "N", "NA", "N/A"):       # 'N': no temperature control (Emergency 4.1.8 toilets and dirty utilities)
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
def zone_of(room: str) -> Tuple[str, str]:
    """``(zone, prefix_as_printed)`` of an Emergency room name, or ``("", "")`` when it opens with no known zone."""
    t = clean(room)
    for zone, pat in ZONE_PREFIXES:
        m = re.match(pat, t, re.I)
        if m:
            return zone, t[:m.end()]
    return "", ""


def zoned_caption(words: Sequence[Word]) -> Optional[Dict[str, Any]]:
    """Caption of an Emergency 4.1.8 criteria page: 'Rooms Part 1', or 'Rooms Part 6 - PANDEMIC SURGE AFFECTED ROOMS'
    (20 pt, left of the section strip).  This is the header signature of the zoned layout: the other booklets caption
    the same table 'Room conditions - Part N', so the section strip alone (4.1.8 is also the strip of Paeds, Palliative,
    Healthy Aging and part of Psych Med) does not tell the layouts apart.  Returns ``part`` ('Part 6'), ``mode`` (the
    title after the dash, as printed) and ``caption``; ``None`` when the page is not captioned like that."""
    cap = join_words([w for w in words if 18.0 <= w.size < 26.0 and w.x0 < 735 and w.y0 < 90 and not is_gutter(w)])
    m = ZONED_CAPTION_RE.match(cap)
    if not m:
        return None
    return {"part": "Part %s" % m.group(1), "mode": clean(m.group(2) or ""), "caption": cap}


class TableColumns:
    """Column x-ranges derived from the header words."""

    def __init__(self, clusters: Dict[str, Tuple[float, float]], header_bottom: float,
                 header_refs: Dict[str, List[str]], centre_bounds: bool = False,
                 header_text: Optional[Dict[str, str]] = None):
        self.clusters = clusters
        self.wrapped = False                      # True for the CSSD heading layout (``WRAP_HEADER_VOCAB``)
        self.header_bottom = header_bottom
        self.header_refs = header_refs
        self.header_text = header_text or {}      # heading of each column as printed (text-layer words, read in order)
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
                # Pharmacy p4: the pressure cell 'SLIGHTLY +VE' is centred under its heading, so its first word starts
                # left of the heading's text and is not part of the room name; the word goes by its centre
                if not (SLIGHT_RE.match(word.text) and second == "pressure" and self.bounds[second][0] <= word.cx):
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


def find_header(words: Sequence[Word], vocab: Optional[set] = None) -> Optional[TableColumns]:
    """Locate the criteria header (starts at the 'Room' word) and derive columns.

    ``vocab`` is the set of words a header line may hold (default ``HEADER_VOCAB``; the Emergency 4.1.8 handler
    passes ``ZONED_HEADER_VOCAB``, which also takes the 'Temperatur' / 'e,' wrap of the temperature heading)."""
    vocab = HEADER_VOCAB if vocab is None else vocab
    room_candidates = [w for w in words if w.text.lower() in ("room", "area/room") and not is_gutter(w)]
    all_lines = cluster_lines([w for w in words if not is_gutter(w)])

    def is_header_line(line: Sequence[Word]) -> bool:
        return all(w.text.lower() in vocab or re.fullmatch(r"\d", w.text) for w in line)

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
        short = vocab is SHORT_HEADER_VOCAB
        split = vocab is SPLIT_HEADER_VOCAB or vocab is WRAP_HEADER_VOCAB or short                # headings wrapped mid-word: match on the text with the breaks closed
        if "pressure" not in texts or "midity" not in texts:     # 'midity' also closes the wrapped 'Hu' / 'midity'
            continue
        # footnote superscripts in the header: digit-only words smaller than the header text
        sizes = [w.size for w in header if not re.fullmatch(r"\d", w.text) and w.size]
        text_size = max(sizes) if sizes else 0.0
        ref_re = FOOTNOTE_REF_RE if vocab is WRAP_HEADER_VOCAB else re.compile(r"\d")
        refs = [w for w in header if ref_re.fullmatch(w.text) and w.size and text_size and w.size < text_size * 0.9]
        body = [w for w in header if w not in refs and w.text not in (",",)]
        clusters = _cluster_x(body)
        mapping: Dict[str, Tuple[float, float]] = {}
        texts: Dict[str, str] = {}
        refs_by_col: Dict[str, List[str]] = defaultdict(list)
        for cl in clusters:
            text = join_words(cl).lower()
            closed = text.replace(" ", "")
            for key, pat in (SHORT_EXTRA_KEYS + HEADER_KEYS if short else HEADER_KEYS):
                if key not in mapping and (re.search(pat, text) or (split and re.search(pat, closed))):
                    mapping[key] = (min(w.x0 for w in cl), max(w.x1 for w in cl))
                    texts[key] = join_words(cl)
                    break
        # attach the footnote refs to the column whose x-range contains them
        for ref in refs:        # nearest header cell, within 15 points of its text
            dist = {key: max(x0 - ref.cx, ref.cx - x1, 0.0) for key, (x0, x1) in mapping.items()}
            if dist:
                key = min(dist, key=dist.get)  # type: ignore[arg-type]
                if dist[key] <= 15.0:
                    refs_by_col[key].extend(ref.text.split(","))
        tc = TableColumns(mapping, max(w.y1 for w in header), dict(refs_by_col), header_text=texts)
        tc.wrapped = vocab is WRAP_HEADER_VOCAB
        for key in tc.order:        # the heading of each column as printed: every header word that sits over it
            lo, hi = tc.bounds[key]
            texts[key] = join_words([w for w in body if lo <= w.cx < hi])
        return tc
    return None


def _is_pressure_cell(text: str) -> bool:
    """True when the text reads as a pressure cell (a Pa figure or Neutral / Negative / Positive): a table row."""
    if not text:
        return False
    r = parse_pressure_full(text)
    return bool(r["ok"] and (r["pa"] or r["pa_min"] or r["direction"]))


VISION_ROWS_PATH = Path(__file__).with_name("booklet48_vision_rows.json")


def vision_rows(file_name: str, pdf_page: int, path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Rows keyed in by eye for a criteria page that is an image (see ``booklet48_vision_rows.json``); ``None`` when none."""
    try:
        data = json.loads((path or VISION_ROWS_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = (data.get(file_name) or {}).get(str(pdf_page))
    return entry if isinstance(entry, dict) and entry.get("rows") else None


def vision_page_rows(entry: Dict[str, Any], box: Tuple[float, float, float, float]) -> List[Dict[str, Any]]:
    """Rows of ``parse_criteria_page`` shape from a ``vision_rows`` entry; ``box`` is the page image."""
    cols = entry["cols"]
    return [{"cells": dict(zip(cols, cells)), "box": box, "marks": [], "note_refs": [], "mode": "",
             "mode_from_caption": False, "no_pressure": False, "blank_row": False, "pressure_words": [], "vision": True,
             "zone": entry.get("department_column", "")}
            for cells in entry["rows"]]


def department_cells(dept_words: Sequence[Word], rects: Sequence[Tuple[float, float, float, float]],
                     edge: float, room_x0: float) -> List[Tuple[float, float, str]]:
    """Merged DEPARTMENT cells of a table (Pharmacy p4: ``LEVEL B2 - PHARM`` over nine rows, ``B 1`` over the last one).

    The text is rotated (reads bottom to top, wrapped lines run left to right).  Each filled rectangle left of the room
    column that holds such words is one cell; ``(y0, y1, text)`` is its extent and its text as printed (a wrap of one
    or two characters per line is closed up: ``B`` / ``1`` reads ``B1``).  No rectangle, no zone."""
    out: List[Tuple[float, float, str]] = []
    cells = [r for r in rects if r[2] <= room_x0 + 1.0 and 50.0 <= r[0] < edge and r[3] - r[1] > 20.0]
    for r in sorted(cells, key=lambda r: r[1]):
        ws = [w for w in dept_words if r[0] <= w.cx <= r[2] and r[1] <= w.cy <= r[3]]
        if not ws:
            continue
        lines: List[List[Word]] = []
        for w in sorted(ws, key=lambda w: w.x0):
            if lines and abs(w.cx - lines[-1][0].cx) <= 0.5 * max(w.x1 - w.x0, 1.0):
                lines[-1].append(w)
            else:
                lines.append([w])
        texts = [" ".join(w.text for w in sorted(ln, key=lambda w: -w.y1)) for ln in lines]
        sep = "" if all(len(t) <= 2 for t in texts) else " "
        out.append((r[1], r[3], clean(sep.join(texts))))
    return out


def group_cells(words: Sequence[Word], rects: Sequence[Tuple[float, float, float, float]], cols: "TableColumns",
                top: float, bottom: float) -> Tuple[List[Tuple[float, float, str]], set]:
    """Group-label cells of a short DD booklet table: filled cells at the left edge whose right neighbour is the room
    cell (which runs on to the value columns).  Returns ``[(y0, y1, label)]`` (label as printed, lines joined with a
    space) and the ids of the words the labels are made of.  No such cell, no zone; a room name that spans the group and
    room cells (CATH 'Sterile Prep Room') is not a group label and stays in the table."""
    pressure_x0 = cols.clusters["pressure"][0]
    body = [r for r in rects if r[1] >= top - 1.0 and r[3] <= bottom + 1.0 and r[3] - r[1] > 20.0]
    cells = []
    for r in body:
        if not (50.0 <= r[0] < 100.0 and r[2] < pressure_x0 - 20.0):
            continue
        if any(abs(o[0] - r[2]) <= 1.5 and o[2] > r[2] + 40.0 and o[2] <= pressure_x0 + 15.0
               and o[1] < r[3] - 1.0 and o[3] > r[1] + 1.0 for o in body):
            cells.append(r)
    out: List[Tuple[float, float, str]] = []
    taken: set = set()
    for r in sorted(cells, key=lambda r: r[1]):
        ws = [w for w in words if r[0] <= w.cx <= r[2] and r[1] <= w.cy <= r[3] and not is_gutter(w)]
        if not ws:
            continue
        if sum(w.y1 - w.y0 for w in ws) > 1.5 * sum(w.x1 - w.x0 for w in ws):
            # turned text (LabMed 'PATHOLOGY'): reads bottom to top, a wrapped line starts to the right of the one before
            cols_: List[List[Word]] = []
            for w in sorted(ws, key=lambda w: w.cx):
                if cols_ and abs(w.cx - cols_[-1][0].cx) <= 0.5 * max(w.x1 - w.x0, 1.0):
                    cols_[-1].append(w)
                else:
                    cols_.append([w])
            label = clean(" ".join(" ".join(w.text for w in sorted(c, key=lambda w: -w.y1)) for c in cols_))
        else:
            label = clean(" ".join(join_words(ln) for ln in cluster_lines(ws)))
        out.append((r[1], r[3], label))
        taken.update(id(w) for w in ws)
    return out, taken


def parse_criteria_page(words: List[Word], page_h: float, page_w: float,
                        rects: Optional[Sequence[Tuple[float, float, float, float]]] = None,
                        marks: Optional[Sequence[Dict[str, Any]]] = None,
                        zoned: Optional[Dict[str, Any]] = None, legacy: bool = False, short: bool = False
                        ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str, List[str]]:
    """Parse one criteria page.

    ``rects`` (the filled rectangles of the page) are optional.  When the Room column is drawn as one filled
    rectangle per table row, a line that lies inside a row rectangle belongs to that row whatever its
    distance from the row's value line (a filter cell wrapped over four lines, a room name centred on a tall
    row), and a room name alone in a row rectangle that holds no value line is a row of its own with blank cells.
    Without row rectangles, or where a rectangle holds two value lines, the nearest value line is used.

    ``marks`` are the reviewer marks of the page (see ``_row_marks``): a strike-through line over the room text, or a
    comment whose callout arrow ends inside the row.  They are carried on the row as ``marks``.

    ``zoned`` is the caption of an Emergency 4.1.8 page (see ``zoned_caption``): the table is the same eleven-column
    table, but it is captioned 'Rooms Part N' (not 'Room conditions - Part N'), its temperature heading wraps
    mid-word and a title after the part number ('PANDEMIC SURGE AFFECTED ROOMS') is the operating mode of every row.

    ``legacy`` (the released departments, ``RELEASED_DEPARTMENTS``): the marks that circle or highlight cells are not attached
    to rows (``link_marks``) and a wrapped room name is joined as before (``join_room_words``), so that their registers stay as
    released.

    ``short`` (the six A4 short DD booklets, see ``page_xform``): the left-hand group label of the table ('Standard
    Procedure Room', 'NEP Operating Theatre', 'PATHOLOGY') is horizontal text in its own filled cell and is the ``zone``
    of the rows beside it (``group_cells``); the headings are read with ``SHORT_HEADER_VOCAB``.

    Returns ``(rows, notes, table_part, page_failures)``.  Each row is a dict with the raw
    cells (``cells``), the room text, the box, the note references and ``mode`` (the
    section heading such as 'Hybrid Cooling' that precedes it).
    """
    failures: List[str] = []
    flat = join_words(words)
    part_m = re.search(r"Room conditions\s*[–—\-]\s*Part\s*(\d+)", flat, re.I)
    table_part = "Part %s" % part_m.group(1) if part_m else ""
    if zoned:
        table_part = zoned["part"]
    elif not table_part and re.search(r"Room conditions", flat, re.I):
        table_part = "Room conditions (unnumbered)"     # a single table printed without 'Part N' (Dialysis)
    cols = find_header(words, ZONED_HEADER_VOCAB if zoned else None)
    fallback_header = False
    if cols is None and not zoned:
        fallback_header = True
        cols = find_header(words, SPLIT_HEADER_VOCAB)       # headings wrapped mid-word (ICU HDU)
        if cols is None:
            cols = find_header(words, AREA_HEADER_VOCAB)    # 'AREA / ROOM' (I-CARE)
        if cols is None and short:
            cols = find_header(words, SHORT_HEADER_VOCAB)   # the A4 short DD booklets
        if cols is None and not short:
            cols = find_header(words, WRAP_HEADER_VOCAB)    # CSSD
    if cols is not None and not table_part and not zoned:
        # no 'Room conditions - Part N' caption: the title above the table names the mode ('I-Care - Operation Mode',
        # 'Basement 2 Pharmacy - Compounding Lab - Operation Mode', 'OP Level 1 Pharmacy'); none printed: a source gap
        above = [w for w in words if w.y1 <= cols.header_bottom and w.x0 < 650 and not is_gutter(w)
                 and w.size >= 18.0 and w.size < 30.0]
        title_lines = cluster_lines(above)
        if title_lines:
            last = title_lines[-1]
            # a long title runs on past x = 650 on a line below the section strip ('... Compounding Lab - Setback Mode 1',
            # Pharmacy p7-p10): the words are the same line's, so the mode and its number are not cut off
            tail = sorted((w for w in words if w.x0 >= 650 and w.y1 <= cols.header_bottom and w.y0 >= 78.0
                           and 18.0 <= w.size < 30.0 and not is_gutter(w)
                           and abs(w.cy - sum(x.cy for x in last) / len(last)) <= 4.0), key=lambda w: w.x0)
            prev = last[-1]
            for w in tail:
                if w.x0 - prev.x1 <= 20.0:
                    last = last + [w]
                    prev = w
            table_part = clean(re.sub(r"^[•\u2022\s]+", "", join_words(last)))
        if re.fullmatch(r"ACMV", table_part, re.I):
            table_part = ""
        if not table_part and fallback_header:
            table_part = NO_CAPTION_PART             # a layout that prints no caption at all: a source gap, not a failure
    if not table_part:
        failures.append("table caption ('Room conditions') not found")
    if cols is None:
        failures.append("criteria header not found")
        return [], [], table_part, failures
    missing = [k for k, _ in HEADER_KEYS if k not in cols.clusters]
    if missing:
        failures.append("header columns not found: %s" % ", ".join(missing))
        if "room" not in cols.clusters or "pressure" not in cols.clusters:
            return [], [], table_part, failures

    room_x0 = cols.clusters.get("room", (0.0, 0.0))[0]
    dept_zones: List[Tuple[float, float, str]] = []
    dept_hdr = [] if short else [w for w in words if w.text.upper() == "DEPARTMENT" and w.y1 <= cols.header_bottom + 1.0
                and w.x0 < 650 and w.cx < room_x0]
    if dept_hdr:     # a DEPARTMENT column of vertical merged text ('LEVEL B2 - PHARM') left of the room column is not row text
        edge = (dept_hdr[0].cx + room_x0) / 2.0
        note_y = find_notes_top(words, cols.header_bottom, page_h)         # the note under the table is not department text
        dept_words = [w for w in words if w.cx < edge and cols.header_bottom - 0.5 <= w.y0 < note_y - 0.5]
        dept_zones = department_cells([w for w in dept_words if not is_gutter(w)], rects or [], edge, room_x0)
        words = [w for w in words if w not in dept_words]
    if short:
        note_y = find_notes_top(words, cols.header_bottom, page_h)
        dept_zones, taken = group_cells(words, rects or [], cols, cols.header_bottom, note_y)
        words = [w for w in words if id(w) not in taken]
    notes_top = find_notes_top(words, cols.header_bottom, page_h)
    if notes_top >= page_h and cols.wrapped:
        # CSSD: the footnotes ('1 - REFER TO APPENDIX 4.D ...') are numbered lines under the table with no NOTE marker
        notes_top = next((min(w.y0 for w in ln) for ln in cluster_lines([w for w in words if w.y0 > cols.header_bottom
                          and not is_gutter(w)]) if re.fullmatch(r"\d", ln[0].text) and len(ln) > 2
                          and ln[1].text in ("–", "-", "—")), page_h)
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
    pending_mode = zoned["mode"] if zoned else ""
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
            cells[col] = (join_room_words if col == "room" and not legacy else join_words)([w for w in ws if w not in ref_ws])
            if col == "room" and re.search(r"\*$", cells[col]):
                refs.append("*")          # '*' footnote reference printed after the room name
        for col, col_refs in cols.header_refs.items():
            refs.extend(col_refs)
        row_box = union_box(w.box() for w in all_words)
        rows.append({
            "cells": cells, "box": row_box, "marks": _row_marks(row_box, cells_words.get("room", []), marks or []),
            "note_refs": sorted(set(refs), key=lambda s: (len(s), s)),
            "mode": pending_mode, "mode_from_caption": bool(zoned and zoned["mode"]),
            "no_pressure": bool(anchor.get("no_pressure")),
            "blank_row": bool(anchor.get("blank_row")),
            "pressure_words": [w for w in anchor["by_col"].get("pressure", [])],
            "zone": next((z for y0, y1, z in dept_zones if y0 - 1.0 <= anchor["cy"] <= y1 + 1.0), ""),
        })
    if marks and not legacy:
        link_marks(rows, marks, words, cols)
    notes = foot_notes + (parse_block_note(words, notes_top, footer_top) if notes_top < footer_top else [])
    if zoned is not None:
        zoned["header_text"] = dict(cols.header_text)       # the column headings as printed, for the report
        zoned["header_order"] = list(cols.order)
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


REGION_VERBS = {"Circle": "circled", "Square": "boxed", "Polygon": "outlined", "PolyLine": "outlined",
                "Highlight": "highlighted", "Underline": "underlined", "Squiggly": "underlined"}
REGION_SHAPES = ("Circle", "Square", "Polygon", "PolyLine")          # marks that enclose cells (a highlight marks text)
COLUMN_LABELS = {"room": "Room", "pressure": "Pressure", "min_oa": "Min. Outdoor ACH", "min_total": "Min. Total ACH",
                 "single_pass": "Single Pass", "air_class": "Air Class", "filter": "Filter", "rh": "Relative Humidity",
                 "temp": "Design Temperature", "afterhours": "Afterhours", "setback": "Setback"}
CIRCLE_AFTER_COMMENT_S = 30.0       # a circle drawn within this many seconds after a comment was last saved belongs to it


def _overlap_rows(rows: Sequence[Dict[str, Any]], rect: Sequence[float]) -> List[int]:
    """Rows a reviewer mark covers: the vertical overlap is at least half of the smaller of the row and the mark."""
    out = []
    for i, r in enumerate(rows):
        b = r["box"]
        ov = min(b[3], rect[3]) - max(b[1], rect[1])
        if ov > 0 and ov >= 0.5 * min(b[3] - b[1], rect[3] - rect[1]) and min(b[2], rect[2]) > max(b[0], rect[0]):
            out.append(i)
    return out


def _tip_rows(rows: Sequence[Dict[str, Any]], pt: Sequence[float], pad: float = 6.0) -> List[int]:
    return [i for i, r in enumerate(rows) if r["box"][0] - pad <= pt[0] <= r["box"][2] + pad
            and r["box"][1] - pad <= pt[1] <= r["box"][3] + pad]


def link_marks(rows: List[Dict[str, Any]], marks: Sequence[Dict[str, Any]], words: Sequence[Word],
               cols: "TableColumns") -> None:
    """Attach the reviewer marks that cover cells, and settle which rows each comment is about.

    * A circle, box, outline or highlight (``region``) belongs to every row it covers (``_overlap_rows``); the row records
      the mark, the cells (columns) inside it and, for a highlight, the words under it.
    * A comment keeps the row its callout arrow ends in.  It is also attached to (a) the row where a second arrow (a Line
      that starts inside the comment box) ends; (b) the rows of the circle or outline that the callout tip lies inside, and
      of every other circle around the same cells (DI p2: the arrow lands in the circle round 'No (c/w Air Purging)' and
      the MRI and CT Procedure 'No' cells carry the same circle); (c) when the tip is in no circle, the rows of a circle
      drawn within 30 s after the comment was last saved (DI p2 'Why is Ultrasound and U/S Procedure +ve?': the two +ve
      circles were drawn 9 and 12 s later).  When circled rows are found that do not include the row at the arrow tip, the tip
      row is dropped (the arrow ends in the gap between Toilet and Ultrasound).  A tip that lies below the last row (Pharmacy
      p4 'compressor room' comment) is taken to point at that row."""
    regions = [m for m in marks if m["kind"] == "region"]
    comments = [m for m in marks if m["kind"] == "comment"]
    arrows = [m for m in marks if m["kind"] == "arrow"]

    def cols_in(rect: Sequence[float]) -> Tuple[str, ...]:
        inside = tuple(k for k in cols.order if rect[0] <= sum(cols.clusters[k]) / 2.0 <= rect[2])
        if inside:
            return inside
        for k in cols.order:        # a mark that holds no header centre (a highlight of one word): the column it sits in
            lo, hi = cols.bounds[k]
            if lo <= (rect[0] + rect[2]) / 2.0 < hi:
                return (k,)
        return ()

    reg_rows = {id(m): _overlap_rows(rows, m["rect"]) for m in regions}
    for m in regions:
        names = cols_in(m["rect"])
        for i in reg_rows[id(m)]:
            under = join_words([w for w in words if m["rect"][0] <= w.cx <= m["rect"][2]
                                and rows[i]["box"][1] - 1.0 <= w.cy <= rows[i]["box"][3] + 1.0
                                and m["rect"][1] <= w.cy <= m["rect"][3]])
            labels = [COLUMN_LABELS.get(k, k) for k in names]
            where = (" around its %s cell%s" % ((", ".join(labels[:-1]) + " and " + labels[-1]) if len(labels) > 1 else labels[0],
                                                "s" if len(labels) > 1 else "")
                     if 0 < len(names) < 5 else " around the whole row" if names else "")
            text = "%s by a reviewer %s annotation%s%s%s" % (
                REGION_VERBS[m["type"]], m["type"], " (%s)" % m["author"] if m["author"] else "", where,
                " ('%s')" % under if m["type"] == "Highlight" and under else "")
            rows[i]["marks"].append({"kind": "region", "type": m["type"], "text": text})
    held = {id(c): {i for i, r in enumerate(rows) if any(x is c for x in r["marks"])} for c in comments}
    for r in rows:
        r["marks"] = [x for x in r["marks"] if x["kind"] != "comment"]
    for c in comments:
        final = set(held[id(c)])
        circ: set = set()
        tip_regs = [m for m in regions if m["type"] in REGION_SHAPES and c.get("tip")
                    and m["rect"][0] - 2.0 <= c["tip"][0] <= m["rect"][2] + 2.0
                    and m["rect"][1] - 2.0 <= c["tip"][1] <= m["rect"][3] + 2.0]
        for m in tip_regs:
            circ.update(reg_rows[id(m)])
        circles_tip = [m for m in tip_regs if m["type"] == "Circle"]
        for m in circles_tip:       # the same circle round the same cells elsewhere on the page: the same query
            same = cols_in(m["rect"])
            if 0 < len(same) < 5:
                for o in regions:
                    if o["type"] == "Circle" and cols_in(o["rect"]) == same:
                        circ.update(reg_rows[id(o)])
        if not tip_regs and c.get("mod") is not None:
            for o in regions:
                if o["type"] == "Circle" and o.get("created") is not None \
                        and 0.0 <= o["created"] - c["mod"] <= CIRCLE_AFTER_COMMENT_S:
                    circ.update(reg_rows[id(o)])
        for a in arrows:
            p, q = a["verts"]
            box = c["rect"]
            for here, there in ((p, q), (q, p)):
                if box[0] - 3.0 <= here[0] <= box[2] + 3.0 and box[1] - 3.0 <= here[1] <= box[3] + 3.0:
                    final.update(_tip_rows(rows, there))
        if circ and not (final & circ):
            final -= held[id(c)]
        final |= circ
        if not final and c.get("tip") and rows:
            last = max(range(len(rows)), key=lambda i: rows[i]["box"][3])
            b = rows[last]["box"]
            if b[0] <= c["tip"][0] <= b[2] and 0.0 < c["tip"][1] - b[3] <= 30.0:
                final.add(last)
        for i in final:
            rows[i]["marks"].append(c)
    for r in rows:      # strikes first, then comments in the order of the page's annotations, then the other marks
        r["marks"].sort(key=lambda m: (0 if m["kind"] == "strike" else 1 if m["kind"] == "comment" else 2,
                                       m.get("order", 0)))


def find_notes_top(words: Sequence[Word], below_y: float, page_h: float) -> float:
    """Top of the NOTE block: the first line below ``below_y`` that starts with the capitalised
    marker NOTE or NOTE:- (a data cell reading 'note' does not start the block)."""
    for line in cluster_lines([w for w in words if w.y0 > below_y and not is_gutter(w)]):
        if (re.fullmatch(r"NOTES?:?-?", line[0].text) and len(line) <= 2) or re.fullmatch(r"Notes?:", line[0].text):
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
                        failures: List[Dict[str, Any]], zoned: bool = False) -> List[Dict[str, Any]]:
    """Turn raw rows into criteria.csv rows, normalising each cell and recording failed fields.

    ``zoned`` (Emergency 4.1.8): the zone is read from the room name (``zone_of``) into the ``zone`` column, the prefix
    as printed is kept in ``raw_cells`` and a room name that opens with no known zone is a failed field."""
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
        zone, zone_prefix = zone_of(room) if zoned else (row.get("zone", ""), "")      # Pharmacy p4 / MDU: the DEPARTMENT cell
        if zoned and room and not zone:
            failed.append("zone")
        if row.get("no_pressure"):
            failed.append("pressure (cell empty)")
        part = table_part + ("; %s" % row["mode"] if row["mode"] else "")
        confidence = max(0.1, 0.97 - 0.25 * len(failed)
                         - (0.05 if row["mode"] and not row.get("mode_from_caption") else 0.0))
        strikes = [m for m in row.get("marks", []) if m["kind"] == "strike"]
        comments = [m for m in row.get("marks", []) if m["kind"] == "comment"]
        if strikes:       # the values are read as printed, but the reviewer has struck the row out: not current
            confidence = min(confidence, 0.50)
        if row.get("vision"):                 # keyed in from the page image, not read from a text layer
            confidence = min(confidence, 0.80)
        flag = ""
        if "single_pass" in failed and re.fullmatch(r"\d+(?:\.\d+)?", clean(c.get("single_pass", ""))):
            # a number under Single Pass: the cells of the row to the right of Min. Total ACH look shifted one column in the
            # source (DI p13 Toilet: Single Pass '20', Air Class 'NR', Filter '3').  Read as printed; the flag says so.
            flag = ("values look shifted one column in the source: Single Pass %r, Air Class %r, Filter %r are printed "
                    "under the wrong headings; the register keeps them as printed"
                    % (c.get("single_pass", ""), c.get("air_class", ""), c.get("filter", "")))
        for field in failed:
            failures.append({"file": meta["source_file"], "pdf_page": meta["pdf_page"],
                             "booklet_page": meta["booklet_page"], "row": room or "(no room text)",
                             "field": field, "cell": c.get(field.split(" ")[0], ""),
                             "bbox": fmt_bbox(row["box"])})
        out.append({
            "department": meta["department"], "source_file": meta["source_file"],
            "booklet_page": meta["booklet_page"], "pdf_page": meta["pdf_page"], "table_part": part, "zone": zone,
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
            "reviewer_markup": "; ".join(["struck through by a reviewer %s annotation%s" % (
                m["type"], " (%s)" % m["author"] if m["author"] else "") for m in strikes]
                + [m["text"] for m in row.get("marks", []) if m["kind"] == "region"]),
            "reviewer_comments": " || ".join(m["text"] for m in comments),
            "raw_cells": json.dumps({**{k: c.get(k, "") for k in
                                        ("min_oa", "min_total", "single_pass", "afterhours", "setback")},
                                     **({"zone_prefix": zone_prefix} if zoned else {}),
                                     **({"source_flag": flag} if flag else {}),
                                     **({"read_by": "vision: page is an image, cells keyed in from the render"}
                                        if row.get("vision") else {})},
                                    ensure_ascii=False),
        })
    return out


def design_parameter_notes(words: Sequence[Word]) -> List[Dict[str, Any]]:
    """Text of a 'Design Parameter' page (Pharmacy Compounding Lab p5) as notes: the numbered items ('1. Grade C:' with its
    bullets), the rows of the 'Ramp-Down Strategies' table (``number`` is the mode: 'Operation Mode', 'Setback Mode 1' ...;
    the table has a mode column on the left and a description column from x = 410) and the 'Recommend Setback Mode' paragraph
    with its two reasons (right column).  The title, the section strip and the footer are not part of it; the caller removes
    reviewer comments from the page first, so that their text is not read."""
    body = [w for w in words if not is_gutter(w) and w.y0 >= 105.0 and not (w.x0 >= 735 and w.y0 < 90)]
    notes: List[Dict[str, Any]] = []
    in_ramp = False
    left: Optional[Dict[str, Any]] = None
    right: Optional[Dict[str, Any]] = None

    def add(number: str, text: str, box: Tuple[float, float, float, float]) -> Dict[str, Any]:
        n = {"number": number, "text": text, "box": box}
        notes.append(n)
        return n

    def extend(n: Dict[str, Any], text: str, box: Tuple[float, float, float, float]) -> None:
        n["text"] = clean(n["text"] + " " + text)
        n["box"] = union_box([n["box"], box])

    for ln in cluster_lines(body):
        text = join_words(ln)
        box = union_box(w.box() for w in ln)
        if re.match(r"^Ramp-Down Strategies:?$", text):
            in_ramp = True
            continue
        if not in_ramp:
            m = re.match(r"^(\d+)\.\s+(.*)$", text)
            if m:
                left = add(m.group(1), m.group(2), box)
            elif left is not None:
                extend(left, text, box)
            continue
        lt = join_words([w for w in ln if w.x0 < 410.0])
        rt = join_words([w for w in ln if w.x0 >= 410.0])
        lbox = union_box(w.box() for w in ln if w.x0 < 410.0)
        rbox = union_box(w.box() for w in ln if w.x0 >= 410.0)
        if lt == "Mode" and rt == "Description":
            continue
        m_mode = re.fullmatch(r"(Operation Mode|Setback Mode \d)", lt)
        m_rec = re.match(r"^(Recommend Setback Mode)\s*:\s*(.*)$", lt)
        m_why = re.match(r"^(Greater Energy Savings|Easier Control)\s*:\s*(.*)$", rt)
        if m_mode:
            left = right = add(m_mode.group(1), rt, box)
            continue
        if m_rec:
            left = add(m_rec.group(1), m_rec.group(2), lbox)
        elif lt and left is not None:
            extend(left, lt, lbox)
        if m_why:
            right = add(m_why.group(1), m_why.group(2), rbox)
        elif rt and right is not None:
            extend(right, rt, rbox)
    return notes


def flag_repeated_rooms(built: List[Dict[str, Any]]) -> None:
    """A room name printed on two rows of one page with different values (ICU HDU p6: 'Anteroom (NEP ISO)' with -15 Pa and
    again with 15 Pa and HEPA) would collide in any join on the room name.  Each such row gets a ``source_flag`` in
    ``raw_cells``; the values are not changed."""
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in built:
        groups[r["room_type_norm"]].append(r)
    for norm, grp in groups.items():
        if len(grp) < 2 or len({(g["pressure_regime_as_printed"], g["min_total_ach"], g["filter"]) for g in grp}) < 2:
            continue
        for g in grp:
            raw = json.loads(g["raw_cells"])
            note = ("the room name %r is printed on %d rows of this page with different values (pressure %s): a join on the "
                    "room name collides; the register keeps each row as printed"
                    % (g["room_type_as_printed"], len(grp), " / ".join(x["pressure_regime_as_printed"] for x in grp)))
            raw["source_flag"] = (raw["source_flag"] + "; " + note) if raw.get("source_flag") else note
            g["raw_cells"] = json.dumps(raw, ensure_ascii=False)


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
    area = [w for w in words if w.text == "Area" and not is_gutter(w)]
    if conf and area:        # a page title that holds the word ('Equipment Configuration Summary Level 4') is not the header
        near = [w for w in conf if abs(w.cy - area[0].cy) <= 40.0]
        conf = near or conf
    if conf:
        head = min(conf, key=lambda w: w.y0)
        for w in words:
            if re.fullmatch(r"\d+(?:,\d+)*", w.text) and w.x0 >= head.x1 - 2 and abs(w.cy - head.cy) <= 14 \
                    and not is_gutter(w):
                refs = w.text
                break
        top = find_notes_top(words, head.y1, page_h)
        if top >= page_h:
            top = _unmarked_notes_top(words, head.y1, page_h)
    else:
        top = page_h
    return refs, parse_notes(words, top, page_h)


def _unmarked_notes_top(words: Sequence[Word], below_y: float, page_h: float) -> float:
    """Top of a numbered note block printed with no NOTE marker (Emergency standby page: '1 - REFER TO APPENDIX 4.B ...'):
    the first line below the last equipment tag or configuration cell that opens with a number and a dash.
    ``page_h`` when there is none."""
    cells = [w for w in words if not is_gutter(w) and w.y0 > below_y and
             (TAG_RE.match(w.text) or re.match(r"^(?:AHU-)?CONFIG-", w.text, re.I))]
    if not cells:
        return page_h
    table_bottom = max(w.y1 for w in cells)
    for line in cluster_lines([w for w in words if w.y0 > table_bottom + 2.0 and not is_gutter(w)]):
        if re.match(r"^\d+\s*[–—\-]\s*\S", join_words(line)):
            return min(w.y0 for w in line)
    return page_h


def _row_bands(rects: Sequence[Tuple[float, float, float, float]], cx: float, top: float, bottom: float,
               max_width: float = 700.0) -> List[Tuple[float, float, float, float]]:
    """Row rectangles of one column: filled rectangles holding x = ``cx``, between ``top`` and ``bottom``,
    taller than a text line and narrower than ``max_width`` (a page-wide fill is not a cell), innermost only, in
    reading order."""
    cand = [r for r in rects if r[0] - 1 <= cx <= r[2] + 1 and r[3] - r[1] > 8 and r[2] - r[0] < max_width
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
                       rects: Optional[Sequence[Tuple[float, float, float, float]]] = None,
                       page_w: float = 1190.52
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
    first_x0 = min(x0 for x0, _ in mapping.values())
    # a table whose first heading is the Equipment Tag column prints no 'Level NN wing' column (Emergency 4.1.8)
    no_level_column = not any(w.x1 < first_x0 - 2.0 for w in header_words if not is_gutter(w))
    notes_top = find_notes_top(words, header_bottom, page_h)
    if notes_top >= page_h:
        notes_top = _unmarked_notes_top(words, header_bottom, page_h)
    body_bottom = min(notes_top, page_h)
    body = [w for w in words if w.y0 > header_bottom - 0.5 and w.y0 < body_bottom - 0.5
            and not is_gutter(w)]
    config_cx = (mapping["config"][0] + mapping["config"][1]) / 2.0
    area_cx = (mapping["area"][0] + mapping["area"][1]) / 2.0
    bands = _row_bands(rects or [], config_cx, header_bottom, body_bottom)
    tag_like = [w for w in body if STANDBY_TAG_RE.match(w.text)]
    if tagless:
        if not bands:
            return [], carry_heading, ["standby table has no Equipment Tag column and no row rectangles"]
        # the Area cell can be wider than 700 points (Paeds: 704): only a fill as wide as the page is not a cell
        area_cols = _row_bands(rects or [], area_cx, header_bottom, body_bottom, max_width=page_w * 0.9)
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
        used = 0
        for band in bands:
            ws = [w for w in data_words if band[1] - 1.0 <= w.cy <= band[3] + 1.0]
            if not ws:
                continue
            tags = [w for w in ws if STANDBY_TAG_RE.match(w.text) and w.cx < band[0] - 0.5]   # 'AHU-CONFIG-E' looks like a tag
            # several tags stacked in one cell (a duty and standby pair, '...-01A' over '...-01B'): one row per tag
            stacked = len(tags) <= 1 or (max(t.x0 for t in tags) - min(t.x0 for t in tags) < 3.0
                                          and len({round(t.cy) for t in tags}) == len(tags))
            if not stacked or (not tagless and not tags):
                ok = False
                break
            rest = [w for w in ws if not any(w is t for t in tags)]
            area = join_words([w for w in rest if w.cx < band[0] - 0.5])
            config = join_words([w for w in rest if w.cx >= band[0] - 0.5])
            used += len(ws)
            for tag in (sorted(tags, key=lambda t: t.cy) or [None]):
                built.append({"cells": {"tag": tag.text if tag else "", "area": area, "config": config},
                              "cy": (band[1] + band[3]) / 2.0 if tag is None or len(tags) < 2 else tag.cy,
                              "boxes": [w.box() for w in rest] + ([tag.box()] if tag else [])})
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
        r["no_level_column"] = no_level_column
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


def pdf_seconds(date: Any) -> Optional[float]:
    """Seconds since the start of the year 2000 of a PDF date string ``D:20250916065758+08'00'`` (the time zone is
    ignored: only differences between annotations of one page are used); ``None`` when it does not parse."""
    m = re.match(r"D?:?(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})", str(date or ""))
    if not m:
        return None
    import datetime
    y, mo, d, h, mi, sec = (int(g) for g in m.groups())
    try:
        return (datetime.datetime(y, mo, d, h, mi, sec) - datetime.datetime(2000, 1, 1)).total_seconds()
    except ValueError:
        return None


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
                    "mod": pdf_seconds(info.get("modDate", "") or info.get("creationDate", "")),
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
                    "created": pdf_seconds(info.get("creationDate", "") or info.get("modDate", "")),
                    "verts": [tuple(v) for v in verts[:2]] if kind == "Line" else [],
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


def raster_legend(page: "pymupdf.Page", min_share: float = 0.01) -> str:
    """Bounding box of a raster colour legend (an image in the lower-right of the plan holding at least
    two clearly coloured fills), or ''.  Its text is not in the text layer and is not read here; the
    colours are not interpreted."""
    w, h = page.rect.width, page.rect.height
    for im in page.get_image_info():
        x0, y0, x1, y1 = im["bbox"]
        if not (x0 > w * 0.7 and y0 > h * 0.5 and y1 < h * 0.93 and 90 <= x1 - x0 <= 320 and 50 <= y1 - y0 <= 220):
            continue
        pix = page.get_pixmap(clip=pymupdf.Rect(x0, y0, x1, y1), dpi=40, alpha=False)
        counts: Dict[Tuple[int, int, int], int] = defaultdict(int)
        n = pix.width * pix.height
        for i in range(0, len(pix.samples) - 2, pix.n):
            r, g, b = pix.samples[i], pix.samples[i + 1], pix.samples[i + 2]
            if max(r, g, b) - min(r, g, b) >= 40:          # saturated, not white/grey/black
                counts[(r // 48, g // 48, b // 48)] += 1
        if sum(1 for c in counts.values() if c >= min_share * n) >= 2:
            return fmt_bbox((x0, y0, x1, y1))
    return ""


def zoning_records(page: "pymupdf.Page", words: List[Word], pair_tags: bool = False
                   ) -> Tuple[str, List[Dict[str, Any]], bool]:
    """Keyplan title, every tag and room label with bbox, and the legend ``(source, bbox)``:
    source is 'text' (words beside swatches), 'image' (raster legend, text not in the text layer) or ''.

    ``pair_tags`` (Emergency): a label such as 'AHU-B-02-01EDP3WAIT-01A/B', the duty and standby pair, is an equipment tag."""
    tag_re = PAIR_TAG_RE if pair_tags else TAG_RE
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
        if tag_re.match(w.text):
            records.append({"label_kind": "equipment_tag", "text": w.text, "block_text": w.text,
                            "bbox": fmt_bbox(w.box())})
    for ln in lines:
        text = ln["text"]
        if tag_re.match(text) or all(tag_re.match(p) for p in text.split() if p):
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
        box = raster_legend(page, min_share=0.001 if pair_tags else 0.01)    # Emergency: small swatches in a wide image
        legend = ("image", box) if box else ("", "")
    return title, records, legend


def pressure_records(page: "pymupdf.Page", notes_top: Optional[float] = None,
                     heading_words: Optional[Sequence[Word]] = None
                     ) -> Tuple[str, List[Dict[str, Any]]]:
    """Drawing text labels (room names, +/- values) of a pressure regime page, plus reviewer annotations
    (PDF comments) kept apart as label_kind reviewer_annotation.  Lines at or below ``notes_top`` (the NOTE
    block) are not labels: they are recorded verbatim in criteria_notes.csv.

    ``heading_words`` are the words of the big-type page heading.  When it opens with PRESSURE REGIME, every line of it
    is the page heading ('PRESSURE REGIME P3 WAITING & CONSULT BUSINESS AS USUAL'), so a second line that names the
    operating mode is not lost as a drawing label."""
    heading = ""
    head_text = join_words(heading_words) if heading_words else ""
    head_box = union_box(w.box() for w in heading_words) \
        if heading_words and head_text.startswith("PRESSURE REGIME") else None
    records: List[Dict[str, Any]] = []
    annots = page_annotations(page)
    arects = [a["rect"] for a in annots]
    for ln in page_lines(page):
        text = ln["text"]
        if FURNITURE_LINE_RE.match(text) or _inside_any(ln["box"], arects):
            continue      # comment text is recorded once, below, as a reviewer_annotation
        if notes_top is not None and ln["box"][1] >= notes_top - 0.5:
            continue
        if head_box is not None and _inside_any(ln["box"], [head_box], pad=1.0):
            heading = head_text
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
    """'IP_FAST_Section_4.8_...' -> 'IP FAST'; 'IGH_DD Booklet_MDU_OCP' -> 'IGH MDU OCP';
    'Rehab_Med_Combined_Mechanical_Ventilation_Strategy' -> 'Rehab Med Combined'.  The file name is kept in ``source_file``."""
    stem = path.stem
    m = re.match(r"^(.*?)[_ ]Section[_ ]\d", stem, re.I)
    base = m.group(1) if m else stem
    base = re.sub(r"[_\s]+Mechanical[_\s]+Ventilation[_\s]+Strategy$", "", base, flags=re.I)
    base = re.sub(r"^\d{8}[_\s]+", "", base)                  # a leading date stamp ('20250819_IGH_DD Booklet_...')
    base = re.sub(r"^(IGH)[_\s]+DD[_\s]+Booklet[_\s]+", r"\1 ", base, flags=re.I)
    base = re.sub(r"[_\s]+R$", "", base)                 # 'IGH_DD Booklet_I-CARE_BATCH 1_R': the trailing revision letter
    return re.sub(r"[_\s]+", " ", base).strip()


# --------------------------------------------------------------------------- #
# Department mapping (src/booklet_departments.json)
# --------------------------------------------------------------------------- #
DEPARTMENTS_PATH = Path(__file__).with_name("booklet_departments.json")


def load_departments(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """File name -> ``{booklet, dept_no, dept_title, department_label}`` (or ``{booklet: False, reason}`` for a file that is
    not a booklet).  An unreadable or missing mapping gives an empty dict, so a file with no entry is read by its name."""
    try:
        data = json.loads((path or DEPARTMENTS_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    return {k: v for k, v in files.items() if isinstance(v, dict)} if isinstance(files, dict) else {}


def department_info(path: Path, mapping: Optional[Dict[str, Dict[str, Any]]] = None) -> Tuple[str, str]:
    """``(dept_no, department_label)`` of a file from the mapping; both blank when the file has no entry
    (a synthetic or unlisted file), so a row is never given a department number by guesswork."""
    entry = (load_departments() if mapping is None else mapping).get(Path(path).name)
    if not entry or entry.get("booklet") is False:
        return "", ""
    return str(entry.get("dept_no", "")), str(entry.get("department_label", ""))


def stamp_department(result: Dict[str, Any], dept_no: str, label: str) -> None:
    """Write ``dept_no`` and ``department_label`` into every register row of a parsed file."""
    result["dept_no"], result["department_label"] = dept_no, label
    for key in ("criteria", "notes", "standby_notes", "standby", "zoning", "pressure", "room_data"):
        for row in result[key]:
            row["dept_no"], row["department_label"] = dept_no, label


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


def _drawing_rects(page: "pymupdf.Page") -> List[Tuple[Dict[str, Any], "pymupdf.Rect"]]:
    """Drawings with their rectangles in the displayed (rotated) page space."""
    rot = page_xform(page)
    return [(d, (pymupdf.Rect(d["rect"]) * rot) if rot is not None else d["rect"]) for d in page.get_drawings()]


def _fills(page: "pymupdf.Page") -> List[Tuple[float, float, float, float]]:
    return [tuple(r) for d, r in _drawing_rects(page)
            if d.get("fill") is not None and r.width > 20 and r.height > 10]


def _rules(page: "pymupdf.Page") -> List[Tuple[float, float, float]]:
    """Horizontal rule segments ``(y, x0, x1)``: stroked lines or hairline rectangles wider than 100 points."""
    out = []
    for d, r in _drawing_rects(page):
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


BOXED_REGISTERS = ("criteria", "notes", "standby_notes", "standby", "zoning", "pressure", "room_data", "failures")


def unscale_boxes(result: Dict[str, Any], start: Dict[str, int], entry: Dict[str, Any], k: float) -> None:
    """Bring the boxes of the rows a short-booklet page just added back to PDF points (the page was read at ``k`` times
    its size, see ``page_xform``)."""
    def back(text: Any) -> Any:
        if not isinstance(text, str) or not text:
            return text
        return ",".join("%.1f" % (float(v) / k) for v in text.split(","))

    for key, n0 in start.items():
        for row in result[key][n0:]:
            for field in ("bbox", "legend_bbox"):
                if field in row:
                    row[field] = back(row[field])
    if entry.get("number_box"):
        entry["number_box"] = tuple(v / k for v in entry["number_box"])


def parse_pdf(path: Path, root: Path) -> Dict[str, Any]:
    """Parse one booklet PDF into registers, page map and failures."""
    dept = department_from_name(path)
    src = display_path(path, root)
    result: Dict[str, Any] = {"department": dept, "source_file": src, "pages": [], "criteria": [],
                              "notes": [], "standby_notes": [], "standby": [], "zoning": [], "pressure": [],
                              "room_data": [], "failures": [], "page_failures": [], "zoned_pages": []}
    doc = pymupdf.open(str(path))
    carry = ""
    try:
        for index in range(len(doc)):
            page = doc[index]
            pdf_page = index + 1
            marks_at = {key: len(result[key]) for key in BOXED_REGISTERS}
            page_h, page_w = page.rect.height * page_scale(page), page.rect.width * page_scale(page)
            all_words = page_words(page)
            booklet = printed_page_number(all_words, page_h, page_w)
            words = strip_furniture(all_words, page_h, page_w)     # footer stamp and page number are not body text
            heading_big = large_heading(words)
            plan_head = plan_heading_words(words)
            if not re.search(r"PRESSURE REGIME", heading_big) and plan_head \
                    and re.match(r"PRESSURE REGIME|AHU ZONING", join_words(plan_head)):
                heading_big = join_words(plan_head)         # 24 pt plan heading (ICU HDU, DI)
            else:
                plan_head = []
            strip = section_strip(words)
            cls = classify_page(page.get_text(), heading_big, strip, path.name not in FROZEN_LAYOUT_FILES)
            vision = vision_rows(path.name, pdf_page) if cls == "other" else None
            image_box = None
            if vision:        # a criteria table that is a page-sized image: only the strip is text
                big_imgs = [i["bbox"] for i in page.get_image_info()
                            if (i["bbox"][2] - i["bbox"][0]) > 0.6 * page_w and (i["bbox"][3] - i["bbox"][1]) > 0.3 * page_h]
                if big_imgs:
                    cls, image_box = "criteria", tuple(max(big_imgs, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])))
            annots = page_annotations(page)
            markup = page_markup(page)
            if cls in ("criteria", "standby summary", "room data table") and list(page.annots() or []):
                drop_annotations(page)          # comment text must not be read as table cells
                words = strip_furniture(page_words(page), page_h, page_w)
            meta = {"department": dept, "source_file": src, "pdf_page": pdf_page, "booklet_page": booklet}
            sec_m = re.match(r"^(\d+(?:\.\d+)*)\s+MECHANICAL VENTILATION", strip)
            entry = {"pdf_page": pdf_page, "booklet_page": booklet, "class": cls, "detail": "", "rows": 0,
                     "section": sec_m.group(1) if sec_m else "",
                     "annotations": len(annots), "markup": len(markup),
                     "number_box": printed_page_box(all_words, page_h, page_w) if booklet else None}
            if not booklet:
                result["page_failures"].append((pdf_page, "printed page number missing or ambiguous in the text layer"
                                                if all_words else "the page has no text layer (an image): nothing was read"))

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
                          "tip": a["tip"], "rect": a["rect"], "mod": a["mod"], "order": n}
                         for n, a in enumerate(annots) if a["tip"]]
                marks += [{"kind": "strike", "type": m["kind"], "author": m["author"], "y": m["y"], "x0": m["x0"],
                           "x1": m["x1"], "idx": i} for i, m in enumerate(markup) if m["strike"]]
                # marks that circle, box, outline or highlight cells, and the second arrow of a comment (a Line that is not
                # a strike-through); parse_criteria_page attaches them to the rows they cover (``link_marks``)
                marks += [{"kind": "region", "type": m["kind"], "author": m["author"], "rect": m["rect"],
                           "created": m["created"]} for m in markup if m["kind"] in REGION_VERBS and not m["strike"]]
                marks += [{"kind": "arrow", "type": "Line", "verts": m["verts"]}
                          for m in markup if m["kind"] == "Line" and not m["strike"] and len(m["verts"]) == 2]
                short = page_scale(page) != 1.0
                # Emergency 4.1.8 layout: 'Rooms Part N', rows grouped by zone (the short booklets print 'Rooms Part N' too, with no zones)
                zoned = None if short else zoned_caption(words)
                rows, notes, part, pfail = parse_criteria_page(words, page_h, page_w, _fills(page), marks, zoned,
                                                               legacy=dept in RELEASED_DEPARTMENTS, short=short)
                if image_box is not None and not rows:      # no text-layer table: the rows keyed in by eye
                    rows = vision_page_rows(vision, image_box)
                    part, pfail, notes = vision.get("table_part", ""), [], []
                built = build_criteria_rows(rows, meta, part, result["failures"], zoned=zoned is not None)
                if dept not in RELEASED_DEPARTMENTS:
                    flag_repeated_rooms(built)
                if zoned is not None:
                    result["zoned_pages"].append({"pdf_page": pdf_page, "booklet_page": booklet, "strip": strip,
                                                  "caption": zoned["caption"], "part": zoned["part"],
                                                  "mode": zoned["mode"], "header": zoned.get("header_text", {}),
                                                  "order": zoned.get("header_order", []), "rows": len(built)})
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
                entry.update(detail=part + ("; %s" % zoned["mode"] if zoned and zoned["mode"] else "")
                             + ("; zoned layout" if zoned else "")
                             + ("; image page, cells keyed in by eye (confidence 0.80)" if image_box is not None else ""),
                             rows=len(built), notes=len(notes))
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
                rows, carry, pfail = parse_standby_page(words, page_h, carry, _fills(page), page_w)
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
                        "confidence": "0.97" if (c.get("area") and c.get("config")
                                                 and (r["heading"] or r.get("no_level_column"))
                                                 and (c.get("tag") or not has_tags)) else "0.50",
                    })
                    for field, key in (("area", "area"), ("configuration", "config"),
                                       ("level_wing_heading", None)):
                        val = r["heading"] if key is None else c.get(key, "")
                        if key is None and r.get("no_level_column"):
                            continue        # the table prints no Level / wing column: nothing to read
                        if not val:
                            result["failures"].append({"file": src, "pdf_page": pdf_page, "booklet_page": booklet,
                                                       "row": c.get("tag", "") or c.get("area", ""),
                                                       "field": "standby " + field,
                                                       "cell": "", "bbox": fmt_bbox(r["box"])})
                entry.update(rows=len(rows), detail="standby rows" + ("" if has_tags or not rows
                                                                      else " (no equipment tag column)"))
                result["page_failures"].extend((pdf_page, f) for f in pfail)
            elif cls == "zoning keyplan":
                title, recs, legend = zoning_records(page, words, pair_tags=bool(result["zoned_pages"]))
                if not title:
                    result["page_failures"].append((pdf_page, "keyplan title not found" + (
                        "; the plan is an image with no text layer, so no room or tag labels were read"
                        if len(recs) < 5 else "")))
                for rec in recs:
                    result["zoning"].append({**meta, "keyplan_title": title, "has_colour_legend": "Y" if legend[0] else "N",
                                             "legend_source": legend[0], "legend_bbox": legend[1], **rec})
                tags = sum(1 for r in recs if r["label_kind"] == "equipment_tag")
                entry.update(rows=len(recs), detail="%s; %d tags; colour legend %s" % (title or "no title", tags, "yes (%s)" % legend[0] if legend[0] else "no"))
            elif cls == "pressure regime":
                notes_top = find_notes_top(words, 0.0, page_h)
                has_note = notes_top < page_h
                head_ws = plan_head or [w for w in words if w.size >= 26.0 and w.x0 < 735 and w.y0 < 140
                                        and not is_gutter(w)]
                heading, recs = pressure_records(page, notes_top if has_note else None, head_ws)
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
                    if re.search(r"Design Parameter\s*$", entry["detail"], re.I):
                        # Pharmacy p5: grade C / D design parameters and the table of ramp-down modes (what 'Setback Mode
                        # 1 / 2 / 3' of the criteria pages mean) are text, not a criteria table: recorded as notes
                        drop_annotations(page)
                        for n in design_parameter_notes(strip_furniture(page_words(page), page_h, page_w)):
                            result["notes"].append(_note_row(src, dept, booklet, pdf_page, n["number"], n["text"],
                                                             n["box"], "design parameter text"))
                    if strip and not re.search(r"MECHANICAL VENTILATION", strip, re.I):
                        entry["detail"] = ("%s [%s: another section of the booklet]" % (entry["detail"], strip)).strip()
                    elif annots or markup:
                        # a 4.8 page whose table is not read (Healthy Aging OP pages): its reviewer comments are
                        # still recorded, so they are not lost with the table
                        annotation_notes(result["notes"], "reviewer annotation")
            result["pages"].append(entry)
            if page_scale(page) != 1.0:
                unscale_boxes(result, marks_at, entry, page_scale(page))
        _infer_missing_page_numbers(doc, result)
        sections = sorted({e["section"] for e in result["pages"] if e["section"]})
        if len(sections) > 1:           # a combined file (Rehab Med): the section strip tells the parts apart
            for e in result["pages"]:
                if e["section"] and e["class"] in ("criteria", "standby summary", "pressure regime", "zoning keyplan"):
                    e["detail"] = (e["detail"] + "; " if e["detail"] else "") + "section %s" % e["section"]
    finally:
        doc.close()
    stamp_department(result, *department_info(path))
    return result


def _infer_missing_page_numbers(doc: "pymupdf.Document", result: Dict[str, Any]) -> None:
    """A page whose printed number is not in the text layer.

    When the place where the booklet prints its number (taken from the pages that have one) is empty on the rendered
    page too, the number is simply not printed: that is a gap in the source, not a parsing failure.  The page map then
    says which number the sequence implies (the printed numbers either side are two apart, so exactly one number is
    missing) and the page leaves ``page_failures`` for ``page_gaps``.  ``booklet_page`` in the registers stays blank:
    only what is printed is recorded as printed."""
    pages = result["pages"]
    boxes = [e["number_box"] for e in pages if e.get("number_box")]
    if not boxes:
        return
    ref = (sorted(b[0] for b in boxes)[len(boxes) // 2], sorted(b[1] for b in boxes)[len(boxes) // 2],
           sorted(b[2] for b in boxes)[len(boxes) // 2], sorted(b[3] for b in boxes)[len(boxes) // 2])
    result.setdefault("page_gaps", [])
    for i, entry in enumerate(pages):
        if entry["booklet_page"]:
            continue
        msg = "printed page number missing or ambiguous in the text layer"
        if (entry["pdf_page"], msg) not in result["page_failures"]:
            continue
        if not page_number_absent(doc[entry["pdf_page"] - 1], ref):
            continue
        prev = next((e["booklet_page"] for e in reversed(pages[:i]) if e["booklet_page"]), "")
        nxt = next((e["booklet_page"] for e in pages[i + 1:] if e["booklet_page"]), "")
        inferred = ""
        if prev.isdigit() and nxt.isdigit() and entry["pdf_page"] > 1:
            before = pages[i - 1] if i else None
            after = pages[i + 1] if i + 1 < len(pages) else None
            if before and after and before["booklet_page"] == prev and after["booklet_page"] == nxt \
                    and int(nxt) - int(prev) == 2:
                inferred = str(int(prev) + 1)
        entry["booklet_inferred"] = inferred
        result["page_failures"].remove((entry["pdf_page"], msg))
        result["page_gaps"].append((entry["pdf_page"], "no page number is printed on this page (the place where the booklet prints "
                                    "it is blank on the render)" + (
                                        "; by sequence it is %s (%s and %s are printed either side)" % (inferred, prev, nxt)
                                        if inferred else "; the neighbouring numbers do not fix it")))


BOOKLET_NAME_RE = re.compile(r"Booklet|Mechanical[_ ]Ventilation|Section[_ ]\d", re.I)


def is_booklet_name(path: Path) -> bool:
    """The folder also holds other documents ('5. Equipment Schedules (Corr 5).pdf', a register .xlsx); a default run reads
    only the files named as booklets or as a mechanical ventilation strategy.  A file named with ``--paths`` is always read."""
    entry = load_departments().get(Path(path).name)
    if entry is not None:                      # src/booklet_departments.json decides for every file of the booklet folder
        return entry.get("booklet") is not False
    return bool(BOOKLET_NAME_RE.search(path.stem))


def skipped_inputs(root: Path) -> List[str]:
    """Files in the booklet folder that a default run does not read as booklets (name only)."""
    folder = root / common.load_config().get("paths", {}).get("source", "data/source") / BOOKLET_DIR_NAME
    return sorted(p.name for p in folder.iterdir() if p.is_file() and not (p.suffix.lower() == ".pdf" and is_booklet_name(p)))


def collect_inputs(paths: Optional[Sequence[str]], root: Path) -> List[Path]:
    """Files named by ``--paths`` (directories are searched for PDFs); default: the booklet folder."""
    if not paths:
        folder = root / common.load_config().get("paths", {}).get("source", "data/source") / BOOKLET_DIR_NAME
        return [p for p in sorted(folder.glob("*.pdf")) if is_booklet_name(p)]
    found: List[Path] = []
    for item in paths:
        p = Path(item)
        if not p.is_absolute():
            p = (Path.cwd() / p) if (Path.cwd() / p).exists() else (root / p)
        if p.is_dir():
            listed = load_departments()
            found.extend(q for q in sorted(p.glob("*.pdf")) if listed.get(q.name, {}).get("booklet") is not False)   # a file the mapping marks as not a booklet is skipped
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


def _split_zones(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A department whose rows carry a zone (Emergency) is compared zone by zone: each zone becomes its own entry
    named 'Emergency / P3', and the zone prefix is taken off the room type ('P3 TOILET' is a 'toilet' of zone P3), so
    that the same room type of two zones, or of Emergency and another department, can be compared."""
    out: List[Dict[str, Any]] = []
    for res in results:
        if not any(r.get("zone") for r in res["criteria"]):
            out.append(res)
            continue
        by_zone: Dict[str, List[Dict[str, Any]]] = {}
        for r in res["criteria"]:
            zone = r.get("zone", "")
            prefix = json.loads(r["raw_cells"]).get("zone_prefix", "") if zone else ""
            rest = clean(r["room_type_as_printed"][len(prefix):]).strip(" /") if prefix else r["room_type_as_printed"]
            by_zone.setdefault(zone, []).append({**r, "room_type_norm": room_norm(rest) or r["room_type_norm"],
                                                 "room_type_as_printed": rest or r["room_type_as_printed"]})
        for zone, rows in by_zone.items():
            out.append({**res, "department": "%s / %s" % (res["department"], zone or "no zone"), "criteria": rows})
    return out


def comparison_data(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Match criteria rows across departments by normalised room type and operating mode, and list the
    fields on which the departments differ.  Returns the groups, the near matches and the unmatched names."""
    results = _split_zones(results)
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


RELEASED_DEPARTMENTS = ("Dialysis", "Emergency", "Healthy Aging Geriatrics", "IP FAST", "IP SURGICAL", "ISO AH", "Paeds",
                        "Palliative", "Psych Med")


def _folder_update_section(results: List[Dict[str, Any]]) -> List[str]:
    """The extracts added to the folder after the first nine: what each holds, what is read and what is not.  Counts are
    taken from this run; the descriptions of the layouts were read from the words and confirmed on the rendered pages."""
    new = [r for r in results if r["department"] not in RELEASED_DEPARTMENTS]
    if not new:
        return []
    by = {r["source_file"].rsplit("/", 1)[-1]: r for r in new}
    L: List[str] = ["## Folder update: extracts added after the first nine", ""]
    try:
        skipped = skipped_inputs(common.project_root())
    except Exception:  # noqa: BLE001 - the report must not fail on a folder listing
        skipped = []
    L += ["A plain `python3 -m src.booklet48_parser run` reads every PDF in the booklet folder whose name marks it as a booklet "
          "(`is_booklet_name`: `Booklet`, `Mechanical Ventilation` or `Section N`); this report and the registers come from such a "
          "run over %d files (nine released extracts and %d added)." % (len(results), len(new)), ""]
    if skipped:
        L += ["Files in the folder that a default run does not read as booklets: %s. The first is an equipment schedule (read by "
              "the equipment schedule parsers, not here) and the second is a spreadsheet; neither is a section 4.8 booklet." % (
                  "; ".join("`%s`" % n for n in skipped)), ""]
    L += ["| Department | Pages | Criteria rows | Failed fields | Standby rows | Zoning labels | Pressure labels | Room data rows | Reviewer notes |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in new:
        L.append("| %s | %d | %d | %d | %d | %d | %d | %d | %d |" % (
            r["department"], len(r["pages"]), len(r["criteria"]), len(r["failures"]) + len(r["page_failures"]),
            len(r["standby"]), len(r["zoning"]), len(r["pressure"]), len(r["room_data"]), len(r["notes"])))
    L.append("")
    L += ["The nine released extracts are unchanged: every register row of each of them is identical to the previous run "
          "(compared field by field, all seven registers).", ""]

    def part(name: str):
        return by.get(name)

    icu = part("ICU_HDU_Section_4.8_Mechanical_Ventilation_Strategy.pdf")
    if icu:
        L += ["### ICU HDU", "",
              "- Criteria (PDF p6, booklet p. 139): the table prints **no caption** and every heading wraps mid-word in its cell (`Pressure Re` / "
              "`gime, Pa`, `Min. Outdo` / `or ACH`, `Relative Hu` / `midity`, `Design Tem` / `perature, C`, `Setback mo` / `de, Y/N`). "
              "The header is found by a second vocabulary (`SPLIT_HEADER_VOCAB`) used only when the usual one fails, so no other file "
              "reads differently; `table_part` is `%s` (a source gap, not a failure). %d rows, as on the render. Single Pass `N/R` is stored as `NR`; "
              "`≤60` and `≤65` give the maximum." % (NO_CAPTION_PART, len(icu["criteria"])),
              "- Two rows print an **unsigned** pressure: `ICU Bedroom (PP ISO)` `30` and the second `Anteroom (NEP ISO)` `15` (the room name is "
              "printed `NEP ISO` twice, as in the source). The cell carries no sign and the parser does not take one from the room name, so the "
              "figure is a failed field (`pressure_pa` blank, confidence 0.72). The names suggest positive-pressure isolation, the rows above "
              "print -30 and -15 for `NEP ISO`; the owner should confirm +30 and +15.",
              "- Standby (p7 to p9): the pages are titled `Equipment Configuration Summary Level N (Part K)`, which the standby class did not know, "
              "and two tags are printed with a `/` suffix (`AHU-B-07-04ICU-01/02C`), which the tag pattern did not accept, so the reader "
              "fell back to a column split that put the first words of the Area cell into the tag. `STANDBY_TAG_RE` takes the suffix. %d rows, "
              "every tag whole. The level is in the page title, not in a column, so `level_wing_heading` is blank (as for Emergency)."
              % len(icu["standby"]),
              "- Plans: p10 to p13 AHU zoning keyplans, p14 to p19 pressure regime plans (BAU level 4, HOT level 4, BAU/HOT level 5; the heading is "
              "24 pt at the top left, not the 28 pt of the other booklets, so these six pages were read as zoning keyplans until `plan_heading_words` "
              "was added), p20 to p23 MODE OF VENTILATION keyplans. The keyplan labels `AHU-B-07-05ICUISO-02A/B`, `-03A/B` and `AHU-B-07-04ICUISO-01 A/B` "
              "are recorded as room labels, like the ISO AH pair labels.", ""]
    ic = part("IGH_DD Booklet_I-CARE_BATCH 1_R.pdf")
    if ic:
        L += ["### IGH I-CARE BATCH 1", "",
              "- Criteria (p5, p6): the first heading is `AREA / ROOM`, the temperature heading has no unit, and the table sits under a 28 pt `ACMV` "
              "heading with an orange subtitle `I-Care – Operation Mode`, which is the mode and is carried as `table_part`. There is no `DESIGN CRITERIA` "
              "strip, so the page is recognised by its header (`AREA / ROOM`, `Pressure Regime`, `Single Pass`, `Min. Total`). %d rows (11 + 11)." % len(ic["criteria"]),
              "- Afterhours reads `24 hrs AC` on four rows (Clean Utility, Med Room, Procedure Room, Treatment Room - Large). It is not a Y/N flag; the "
              "words are kept as printed in `afterhours` (and in `raw_cells`). A decision for the owner (below).",
              "- Standby (p7): %d rows. Eight `FCU-A-7-NN/20` tags are stacked in one cell over one Area and one configuration, so each tag is a row with "
              "the same area, as for the duty and standby pairs of Emergency. Plans: OP 5th storey keyplans p8 to p11 and p14 to p15, pressure regime p12 and p13." % len(ic["standby"]), ""]
    md_ = part("IGH_DD Booklet_MDU_OCP.pdf")
    if md_:
        L += ["### IGH MDU OCP", "",
              "- Most pages of this file are **images**: p3 and p4 (the criteria, `Medical Day Unit – Operation Mode`, with a merged vertical `MDU` "
              "department cell) have only the section strip and the footer as text, so the text layer holds no table. There is no OCR in the project, "
              "so the 17 rows (11 on p3, 6 on p4) were **keyed in by eye** from the page render at 1568 px and checked against a zoomed crop of the lower half "
              "of p3; they sit in `src/booklet48_vision_rows.json`, are read only for a page with no text-layer table, go through the same "
              "normalisers as every other cell, and carry confidence 0.80 and `read_by` in `raw_cells`. The page map says `image page, cells keyed in by eye`.",
              "- p1 is a reference page (MOH guide table and GreenGov.Sg text, image); p5 to p7 are the standby configuration types A to I (diagrams); "
              "p9 and p10 are AHU zoning plans that are images, so `keyplan title not found` is reported with the reason (no room or tag labels can be read; the "
              "render of p9 shows tag `AHU-A-10-03/04` and the title `OP 8TH STOREY - KEYPLAN`, which are not in the registers). p8 is the standby table "
              "with no title (%d rows: `AHU-A-10-03/04` twice, CONFIG-D and CONFIG-F). "
              "p11 is a pressure regime plan whose title is in the section strip (`... | PRESSURE REGIME`); p15 a room data table (%d row)." % (
                  len(md_["standby"]), len(md_["room_data"])), ""]
    rm = part("Rehab_Med_Combined_Mechanical_Ventilation_Strategy.pdf")
    if rm:
        secs = sorted({p["section"] for p in rm["pages"] if p.get("section")})
        L += ["### Rehab Med Combined", "",
              "- One file holds **two sections** (strip `%s`: PDF p1 to p26 and p27 to p43, booklet p. 115 to 140 and 205 to 221), each with the same Part 1 and Part 2 "
              "criteria tables (14 + 6 rows), standby, pressure regime and keyplan pages; the reviewer comments differ between the two. The rows of both parts are in "
              "`criteria.csv` (%d rows); `pdf_page` and the page map (`section 4.1.8` / `section 4.2.8`) tell them apart. The standard handlers read these pages; the only addition is the section in the page map." % (
                  "`, `".join(secs), len(rm["criteria"])),
              "- p11 to p13 and p38 to p40 (`EQUIPMENT STANDBY CONFIGURATION PART 1 to 3`) are diagrams of the configuration types, not a table (class `other`). "
              "Standby rows (p14, p41): %d, with no tag column and a `Level N wing` heading." % len(rm["standby"]), ""]
    di = part("20250819_IGH_DD Booklet_DI_Signed_All_AH.pdf")
    if di:
        L += ["### IGH DI Signed All AH (diagnostic imaging)", "",
              "- %d criteria rows: IP DI p2 to p4 (63) and OP DI p11 to p13 (30). The IP table prints `NA` under Afterhours and Setback (`NA` is accepted), `SS 553` under Min. "
              "Outdoor ACH (the figure is by reference to the Singapore Standard; `min_oa_ach` is blank and the words stay in `raw_cells`) and `No (c/w Air Purging)` under "
              "Single Pass (stored as `N`, words kept). `Non-Recirculating` is stored as `Y`, as for Emergency. The pages are laid out with the section strip about 40 pt further "
              "left than in the other booklets; the strip is now read from x = 650 pt instead of 735 pt." % len(di["criteria"]),
              "- **Left as a failed field**: OP DI p13 `Toilet` prints `20` under Single Pass, `NR` under Air Class and `3` under Filter (the values of the cells to the right appear "
              "shifted by one column in the source; Min. Total ACH is `20`). It is read as printed and Single Pass `20` is the one failed field of the file.",
              "- Three OP DI rows are struck through by a reviewer and keep their values at confidence 0.50: p12 `Treatment Room`, p12 `Nurse Assessment` (comment: `Not DI but Breast Clinic`) and "
              "p13 `Patient Education`. Reviewer comments are linked to the row where their arrow ends and, since the fourth verification, also to the rows "
              "of the circles they refer to (the p2 comment `Why is Ultrasound and U/S Procedure +ve?` is on `Ultrasound` and `Ultrasound Procedure`, no longer on `Toilet`; see Corrections after verification (4)).",
              "- p5 to p8 carry the headings `AHU ZONING IP DIAGNOSTIC IMAGING` and `PRESSURE REGIME` under a stale strip (`4.1 DEPARTMENT LAYOUT PLAN`); a plan headed so is now read as such "
              "(`classify_page`), while a page of another section without such a heading stays `other`.", ""]
    ot = part("IGH_DD Booklet_ORTHO_BATCH 1.pdf")
    if ot:
        L += ["### IGH ORTHO BATCH 1", "",
              "- Same layout as I-CARE (`Ortho – Operation Mode`): %d rows (10 + 9), %d standby rows, keyplans p4 and p5, pressure regime p6. No failed field." % (
                  len(ot["criteria"]), len(ot["standby"])), ""]
    for fname, label in (("IGH_DD Booklet_Surgical GS_OCP_DPE comments.pdf", "General Surgery (`General Surgery – Operation Mode`, with reviewer comments)"),
                         ("IGH_DD Booklet_Surgical OPCC_BATCH 1.pdf", "Surgical OPCC (`Surgical OPCC – Operation Mode`)")):
        sg = part(fname)
        if sg:
            L += ["### %s" % sg["department"], "",
                  "- %s: same layout as I-CARE and Ortho (`AREA / ROOM`, subtitle as the mode). %d criteria rows over %d pages, %d standby rows, no failed field. Pressure `+VE` / `-VE`, Afterhours `24 hrs AC` as above. "
                  "The first table of the General Surgery file (12 rows) was compared cell by cell with its render; the other pages were not." % (
                      label, len(sg["criteria"]), sum(1 for pg in sg["pages"] if pg["class"] == "criteria"), len(sg["standby"])), ""]
    ph = part("IGH_DD Booklet_Pharmacy_BATCH 2_New signed.pdf")
    if ph:
        L += ["### IGH Pharmacy BATCH 2 (New signed)", "",
              "- %d criteria rows: p4 (Basement 2 Pharmacy, 10), p7 to p10 (Compounding Lab: operation mode and setback modes 1 to 3, 9 rows each; the table has "
              "**no Afterhours and no Setback columns**, which is reported per page as `header columns not found: afterhours, setback` and is not a missing value), p16 (OP Level 1, 10) and p17 (1). "
              "Pressure is printed `SLIGHT +VE`, `SLIGHTLY +VE`, `SLIGHTLY -VE` and `NEUTRAL` (a direction, no figure) and Single Pass, Relative Humidity and Temperature `N/R` (all accepted as not required). "
              "p4 has a `DEPARTMENT` column of vertical merged text (`LEVEL B2 – PHARM`, `B 1`) that is not read as row text; its text is carried as `zone` (see Corrections after verification (4)); "
              "p9 and p10 end with an unnumbered paragraph that starts `Note:`, recorded as a note, not as a row." % len(ph["criteria"]),
              "- **Not read as tables** (class `other`): p1 and p2 (`Overall AHU Zoning & Redundancy`), p3 and p6 (`Pressure Regime Overview`), p5 (`Compounding Lab – Design Parameter`: grade C and D text and a "
              "`Ramp-Down Strategies` table of modes, now recorded as notes of kind `design parameter text`) and p11 (`Cleanroom Data`). They hold plans, text and tables of another kind. Their reviewer comments are in `criteria_notes.csv`.", ""]
    dc = part("20250804_IGH_DD Booklet_Data Centre R2_Markup.pdf")
    if dc:
        L += ["### IGH Data Centre R2 Markup", "",
              "- **Nothing is read from the text layer**: all %d pages are images with no text (the section strip, the footer and the page number are in the picture), so the page numbers "
              "are not available either and each page is listed as `the page has no text layer`. Two pages were viewed on the render: PDF p1 (booklet p. 85, `MODE OF VENTILATION`, "
              "Primary and Secondary Server Room, colour legend AC / AC (24HR) / HC / MV / N.A / NV) and PDF p6 (booklet p. 90, `IP BASEMENT 2 - KEYPLAN`, tags `FCU-B-B2-PSR-01A/B` to `-04A/B` and "
              "`CRAC-B-B2-01A/B`). The %d reviewer annotations are recorded in `criteria_notes.csv`." % (
                  len(dc["pages"]), len(dc["notes"])), ""]
    return L


def _corrections_section_3(results: List[Dict[str, Any]]) -> List[str]:
    """Corrections made after the third independent verification (figures are taken from this run)."""
    by = {r["department"]: r for r in results}
    if "Emergency" not in by:
        return []
    ssc = [(r["pdf_page"], r["booklet_page"]) for r in by["Emergency"]["criteria"] if r["room_type_as_printed"] == "Staff Support Corridor"]
    four = ("ICU HDU", "IGH I-CARE BATCH 1", "IGH MDU OCP", "Rehab Med Combined")
    later_names = [r["department"] for r in results if r["department"] not in RELEASED_DEPARTMENTS and r["department"] not in four]
    later = later_names
    iso = [z["text"] for z in by.get("ISO AH", {}).get("zoning", []) if z["label_kind"] == "room_label" and z["text"].endswith("-1A/B")]
    ha = [z["text"] for z in by.get("Healthy Aging Geriatrics", {}).get("zoning", []) if z["label_kind"] == "room_label"
          and re.match(r"^AHU-A-10-01/04$", z["text"])]
    L: List[str] = ["", "## Corrections after verification (3)", "",
                    "The third independent verification (`out/qa/verify/booklet48_parser_verify_3.md`) checked 24 claims about the Emergency 4.1.8 handler, the `zone` column and "
                    "the Paeds fixes: all 24 confirmed in substance, with one wording refuted. The values were right. What changed:", "",
                    "| Finding | Change | Result in this run |", "|---|---|---|",
                    "| The ISO AH and Healthy Aging keyplan pair labels were described as `...-01A/B` | Wording corrected in the Emergency section above: the ISO AH labels read `-1A/B` and the Healthy Aging label is a range. "
                    "Behaviour is unchanged (they stay room labels). | ISO AH: %d label rows (%s); Healthy Aging: %d label row (`AHU-A-10-01/04`). |" % (
                        len(iso), ", ".join(sorted(set("`%s`" % t for t in iso))), len(ha)),
                    "| The four new PDFs (ICU_HDU, IGH I-CARE BATCH 1, IGH MDU_OCP, Rehab_Med_Combined) were not parsed, and the header named a plain `run` that would now take more files | They are parsed with the existing handlers plus the layout "
                    "additions listed in the folder update above; %d more extracts arrived while this was being done (%s) and are in the same run. "
                    "`python3 -m src.booklet48_parser run` is the command that produced this report; it now skips files that are not booklets. | %d files, %d departments; see the folder update. |" % (
                        len(later), ", ".join(later_names), len(results), len(results)),
                    "| `Staff Support Corridor` is printed on both p4 and p5 with the same values and appears twice in `criteria.csv` | Noted; it is kept twice because it is printed twice. | PDF pages %s: both `Neutral`, 6 ACH, single pass `N`, MERV 15. |" % (
                        " and ".join("%s (booklet p. %s)" % t for t in ssc) or "n/a"),
                    "| `test_zone_column_is_blank_for_other_departments_and_signed_pressure_is_kept` checked only the column order | The test now also parses a standard table and checks that every zone is blank and that `-15 Pa` keeps its sign; "
                    "a second test (`test_zone_is_blank_and_pa_is_signed_on_a_standard_table`) covers a split sign. No assertion was removed. | 2 tests. |",
                    "| Paeds cross-check: a reader of the cell rectangles matched 240 of 242 cells | The two other cells are not parser errors: the text-layer word `Neutral` on the `Interview Room` row spans x 161 to 318, so its centre falls in the Room column for a reader "
                    "that assigns words by centre; the parser reads it by the value line and the render shows `Neutral` under Pressure, as `criteria.csv` has it. | No change. |", ""]
    L += ["### Owner decisions still open", "",
          "These are interpretations the booklets do not settle; each is implemented as stated and each keeps the printed text beside the normalised value.", "",
          "- Emergency: `Non-Recirculating` is stored as single pass `Y`; `FEVER / RESUS NP` is read as zone `FEVER`; P2 rooms with no sub-zone are read as zone `P2`; the green cells on part 6 are not interpreted. "
          "The same `Non-Recirculating` reading is used for the DI extract.",
          "- I-CARE, Ortho, Pharmacy: Afterhours `24 hrs AC` is kept as printed in the Afterhours field (it is neither Y nor N).",
          "- DI: Min. Outdoor ACH `SS 553` is stored as blank with the words in `raw_cells`; Single Pass `No (c/w Air Purging)` is stored as `N`; Afterhours and Setback `NA` are stored as `NA`.",
          "- ICU HDU: the unsigned `30` and `15` on the PP ISO rows are left as failed fields (the sign is not printed).",
          "- MDU OCP p3 and p4: 17 rows keyed in by eye from images (confidence 0.80); they should be checked against the source before use.",
          "- Healthy Aging p23 (two standby rows) and p25 to p26 (an OP operation-mode table) stay unparsed so that the department's registers are unchanged; `FROZEN_LAYOUT_FILES` names the file, "
          "and removing it from that list reads them.", ""]
    return L


def _corrections_section_4(results: List[Dict[str, Any]]) -> List[str]:
    """Corrections made after the fourth verification, which checked the criteria registers cell by cell (figures from this run)."""
    by = {r["department"]: r for r in results}
    if "IGH DI Signed All AH" not in by or "IGH Pharmacy BATCH 2 New signed" not in by:
        return []

    def n(dept: str) -> int:
        return len(by[dept]["criteria"]) if dept in by else 0

    def with_(dept: str, field: str, pdf_page: Optional[int] = None) -> int:
        return sum(1 for r in by[dept]["criteria"] if r[field] and (pdf_page is None or r["pdf_page"] == pdf_page)) if dept in by else 0

    di, ph, rh = by["IGH DI Signed All AH"], by["IGH Pharmacy BATCH 2 New signed"], by.get("Rehab Med Combined", {"criteria": []})
    zoned_p4 = sorted({r["zone"] for r in ph["criteria"] if r["pdf_page"] == 4 and r["zone"]})
    parts = sorted({r["table_part"] for r in ph["criteria"] if r["pdf_page"] in (7, 8, 9, 10)})
    L: List[str] = ["", "## Corrections after verification (4)", "",
                    "Nine independent verifiers read the criteria tables of nine departments against the PDFs (`out/qa/verify/criteria_*.md`). Seven found no value that differs; Pharmacy (60 cells) and "
                    "Rehab Med (5 mark-up cells) did. The values of the other departments were right, but several findings concerned what the registers leave out. No row was added or lost: the row counts "
                    "before and after are equal for every department. Departments in `RELEASED_DEPARTMENTS` are unchanged: the new mark and room-name handling is switched off for them (`legacy`), and a "
                    "cell-by-cell comparison of the registers before and after shows only the departments named below.", "",
                    "| Department (rows before / after) | Finding | Change | Result in this run |", "|---|---|---|---|",
                    "| IGH Pharmacy BATCH 2 (%d / %d) | p4: `SLIGHTLY` started left of the Pressure heading, so it went into the room name (6 rooms ended `slightly`) and the pressure was stored as `+VE` / `-VE` | A `SLIGHT` / `SLIGHTLY` word that lies over the Pressure column is a pressure word (`TableColumns.column_of`) | the 6 rooms read as printed, pressure `SLIGHTLY +VE` / `SLIGHTLY -VE` (direction unchanged) |" % (n("IGH Pharmacy BATCH 2 New signed"), n("IGH Pharmacy BATCH 2 New signed")),
                    "| IGH Pharmacy BATCH 2 | p4: the DEPARTMENT column (`LEVEL B2 – PHARM`, `B1`) was dropped, so the B1 / B2 level was lost | The merged rotated cell is read (`department_cells`): the words inside each filled rectangle left of the room column, bottom to top, are the `zone` of the rows that rectangle spans | zone %s on p4 (9 rows `LEVEL B2 – PHARM`, `UPS/Compressor Room` `B1`) |" % (", ".join("`%s`" % z for z in zoned_p4) or "n/a"),
                    "| IGH Pharmacy BATCH 2 | p7 to p10: the table caption stopped at x = 650, so the mode digit (and on p7 the word `Mode`) was cut off and Setback Modes 1, 2 and 3 all read `... Setback Mode` | The title line is read to its end when the words past x = 650 sit on the title's own line below the section strip | %s |" % "; ".join("`%s`" % t for t in parts),
                    "| IGH Pharmacy BATCH 2 | `Preparation/Inspectio n Room` (4 rows): the cell breaks inside a word | A stranded lower-case letter at the start of a wrapped line is closed up to the word before it (`join_room_words`) | `Preparation/Inspection Room` on 4 rows |",
                    "| IGH Pharmacy BATCH 2 | the p4 comment about the compressor room (arrow tip 23 pt below the last row) was not attached | A tip that lies within 30 pt below the last row is taken to point at that row (`link_marks`) | on `UPS/Compressor Room` (%d row with a comment on p4) |" % with_("IGH Pharmacy BATCH 2 New signed", "reviewer_comments", 4),
                    "| IGH Pharmacy BATCH 2 | p5 `Compounding Lab – Design Parameter` (grade C / D text and the table of ramp-down modes) was not captured | Recorded in `criteria_notes.csv` as notes of kind `design parameter text` (`design_parameter_notes`): items 1 and 2, `Operation Mode`, `Setback Mode 1` to `3`, `Recommend Setback Mode`, `Greater Energy Savings`, `Easier Control` | %d notes |" % sum(1 for x in ph["notes"] if x["note_kind"] == "design parameter text"),
                    "| IGH Pharmacy BATCH 2 | Min. Outdoor ACH `NR` on `Change Room/Toilet` and `Locker` reads blank | Not changed: every department stores a `NR`, `-` or empty Min. Outdoor ACH as blank and keeps the printed word in `raw_cells` (`min_oa`: `NR`) | the 2 rows carry `\"min_oa\": \"NR\"` |",
                    "| IGH DI Signed All AH (%d / %d) | p2: the comment `Why is Ultrasound and U/S Procedure +ve?` was on `Toilet` | A comment is also attached to the rows of a circle drawn within 30 s after the comment was last saved when its arrow tip is in no circle (`link_marks`); when circled rows are found and the tip row is not among them, the tip row is dropped | on `Ultrasound` and `Ultrasound Procedure` (the circles were drawn 9 s and 12 s later); `Toilet` has none |" % (n("IGH DI Signed All AH"), n("IGH DI Signed All AH")),
                    "| IGH DI Signed All AH | p2: the air-purging comment was only on `CT Control Room`; the circled `No` cells of `MRI` and `CT Procedure` carried nothing | A comment whose arrow tip lies in a circle is also attached to every other circle round the same cells (same columns) on the page | the comment is on `MRI`, `CT Procedure` and `CT Control Room` |",
                    "| IGH DI Signed All AH | circles with no text were not attached to any row (`reviewer_markup` empty) | Circles, boxes, outlines and highlights are attached to the rows they cover (at least half of the smaller of the row and the mark, `_overlap_rows`) as `circled by a reviewer Circle annotation (author) around its Pressure cell` | %d rows with `reviewer_markup` on the DI pages, 3 of them the struck-through rows as before (new: p2 MRI, CT Procedure, CT Control Room, Ultrasound, Ultrasound Procedure; p3 X-Ray; p12 Tech Work, and the circles round the names of the two struck rows) |" % with_("IGH DI Signed All AH", "reviewer_markup"),
                    "| IGH DI Signed All AH | p12: the comment `Not DI but Breast Clinic` is now also on `Treatment Room` | Follows from the rule above: the circle round the `Nurse Assessment` name the arrow lands in has a twin round the `Treatment Room` name, and both rows are struck through. **Owner to confirm** that the comment covers both rows. | 1 row |",
                    "| IGH DI Signed All AH | p13 `Toilet`: the source row is shifted one column (Single Pass `20`, Air Class `NR`, Filter `3`) and the CSV had no sign of it | A number under Single Pass adds a `source_flag` to `raw_cells` that names the three cells; the values stay as printed and Single Pass `20` stays the one failed field | 1 row flagged |",
                    "| IGH DI Signed All AH | `SS 553` under Min. OA (8 rows) and the qualifiers (`No (c/w Air Purging)`, `Non-Recirculating`, `24 hrs AC`) survive only in `raw_cells` | Not changed (owner decisions listed below): the printed words are in `raw_cells`; the normalised fields stay blank / `N` / `Y` / as printed | unchanged |",
                    "| Rehab Med Combined (%d / %d) | p5: the second arrow (a Line annotation) of `inconsistent with other departments - these are usually +ve` ends in `Treatment Room`; the comment was only on `Clean Utility / Medication Room` | A Line that starts inside a comment's box is its second arrow; the row where it ends gets the comment | on `Treatment Room` and `Clean Utility / Medication Room` (%d rows with a comment on p5, the other 3 by their own arrows) |" % (n("Rehab Med Combined"), n("Rehab Med Combined"), with_("Rehab Med Combined", "reviewer_comments", 5)),
                    "| Rehab Med Combined | the clouds (p5 `Clean Utility / Medication Room`, `Treatment Room`; p36 `P&O Workshop`) and the highlights on `Transitional Living` were not on any row | Attached as `reviewer_markup` (see above) | %d rows with `reviewer_markup` |" % with_("Rehab Med Combined", "reviewer_markup"),
                    "| IGH MDU OCP (%d / %d) | the merged `MDU` department cell of the image pages was not kept | `department_column` of `src/booklet48_vision_rows.json` (already in the file) is now read as `zone` for the keyed-in rows | zone `MDU` on %d rows |" % (n("IGH MDU OCP"), n("IGH MDU OCP"), with_("IGH MDU OCP", "zone")),
                    "| ICU HDU (%d / %d) | `Multi- Purpose Room` (a stray space after the hyphen stopped an exact match of `room_type_norm`) | A line that ends in a hyphen is joined to the next without a space (`join_room_words`) | `Multi-Purpose Room` |" % (n("ICU HDU"), n("ICU HDU")),
                    "| ICU HDU | `Anteroom (NEP ISO)` is printed twice with different values (-15 Pa / 15 Pa, HEPA), so a join on the room type collides | Each such row gets a `source_flag` (`flag_repeated_rooms`, not for released departments); the values are not changed, because the booklet prints the name twice (the second is probably the PP ISO anteroom: **a query for the designer**) | 2 rows flagged |",
                    "| ICU HDU | the unsigned `30` and `15` on the PP ISO rows leave Pa and direction blank | Not changed: the sign is not printed and is not guessed. Evidence for the owner: on p18 (booklet p. 151) the 1BED ISO PP room is filled green (positive in the legend) with an outward arrow, which supports +30 | unchanged (2 failed fields) |",
                    "| IGH I-CARE, IGH ORTHO, IGH Surgical GS OCP, IGH Surgical OPCC | no value or reviewer defect found | None. The page-map remarks (the sheets titled `MODE OF VENTILATION` / `NORMAL MODE ... AIR RECIRCULATION` are classed `zoning keyplan`; the raster legend at the top right of I-CARE p15 is not found by `raster_legend`; the comments on the General Surgery plan pages, such as the ESWL 24HR AC comment, are not linked to criteria rows) concern the plan pages, not the criteria registers, and are left as they are | unchanged |", ""]
    L += ["Tests: 13 new tests in `tests/test_booklet48_parser.py` (`test_slightly_pressure_word_stays_in_the_pressure_column_and_long_title_keeps_its_mode`, `test_title_words_past_x650_below_the_strip_are_part_of_the_table_part`, "
          "`test_merged_department_cell_gives_the_zone_of_the_rows_it_spans`, `test_wrapped_room_names_are_closed_up_after_a_hyphen_and_before_a_stranded_letter`, `test_wrapped_room_name_is_closed_up_on_a_page_and_left_as_before_for_a_released_department`, "
          "`test_circles_polygons_and_highlights_are_attached_to_the_rows_they_cover`, `test_comment_is_linked_to_circled_rows_a_second_arrow_and_a_circle_drawn_just_after_it`, `test_second_arrow_and_a_tip_below_the_last_row_attach_the_comment`, "
          "`test_pdf_seconds_reads_annotation_dates`, `test_marks_round_cells_end_to_end_and_released_departments_stay_as_released`, `test_number_under_single_pass_is_flagged_as_a_shifted_row_and_repeated_room_names_are_flagged`, "
          "`test_vision_rows_carry_the_department_column_as_zone`, `test_design_parameter_page_is_recorded_as_notes`). No assertion of an earlier test was changed.", ""]
    L += ["### Owner decisions still open after the fourth verification", "",
          "- A comment is attached to circled rows by three rules (arrow tip in a circle; the same circle round the same cells elsewhere; a circle drawn within 30 s after the comment was saved). They were written for DI p2 and apply to every non-released department; "
          "the rows they add are listed above and can be removed by editing `link_marks`.",
          "- The pantry-smell comment on Pharmacy p4 (arrow tip above the table header) and the table-level comments on Pharmacy p16 and p17 stay in `criteria_notes.csv` only: no row is under their arrows.",
          "- Rehab Med: `table_part` reads `Part 1` / `Part 2` for both the inpatient (4.1.8, booklet p. 119-120) and outpatient (4.2.8, booklet p. 213-214) tables; `pdf_page` and `booklet_page` tell them apart (the section is in the page map).",
          "- MDU OCP: the 17 rows are keyed in from images with the bbox of the whole table; they are checked against the source by the verifier (323 cells, no difference).",
          "- Pending outside this task: the folder holds two extracts (`IGH_DD Booklet_Dental_BATCH 1.pdf`, `IGH_DD Booklet_ENT_DPE comments.pdf`) that are not yet in `src/booklet_departments.json`; they are parsed (Dental 17 criteria rows) with an empty department number, and `test_department_mapping_covers_the_booklet_folder_and_skips_non_booklets` fails for that reason, not because of this change.", ""]
    return L


def n_is_table_note1(n: Dict[str, Any]) -> bool:
    return n["note_kind"] == "table note" and n["note_number"] == "1"


def _unnumbered_label(p: Dict[str, Any]) -> str:
    """Page-map cell for a page with no printed number in the text layer."""
    if "booklet_inferred" in p:
        return "not printed" + (" (%s by sequence)" % p["booklet_inferred"] if p["booklet_inferred"] else "")
    return "not in text layer"


def _emergency_section(results: List[Dict[str, Any]]) -> List[str]:
    """Emergency (section 4.1.8): the zoned criteria layout, its other pages, and the Paeds fixes.  Everything is taken
    from this run; the statements about the other departments were checked against the registers of the previous run."""
    emer = [r for r in results if r.get("zoned_pages")]
    L: List[str] = ["## Emergency (section 4.1.8) and Paeds", ""]
    if not emer:
        return L + ["No file with the zoned criteria layout (a criteria page captioned 'Rooms Part N') was parsed.", ""]
    for res in emer:
        L += ["### %s: criteria layout" % res["department"], "",
              "The criteria tables of this file are the eleven-column table of the other booklets, with these differences "
              "(read from the words and confirmed on the rendered pages):", "",
              "- The caption is `Rooms Part N` (20 pt, top left), not `Room conditions - Part N`; part 6 is captioned "
              "`Rooms Part 6 - PANDEMIC SURGE AFFECTED ROOMS`, and that title is the operating mode of its rows "
              "(`table_part` = `Part 6; PANDEMIC SURGE AFFECTED ROOMS`). Parts 1 to 5 carry no mode.",
              "- There is **no zone column and no merged or vertical text**: the zone is the opening words of the room name "
              "(`P3 RECEPTION / WAITING`, `FEVER Gown Down`, `P2 South NP Toilet`, `CAC FEVER Triage`). The parser reads the zone from "
              "that prefix into the new `zone` column (`ZONE_PREFIXES`, longest prefix first) and keeps the prefix as printed in "
              "`raw_cells.zone_prefix`; `room_type_as_printed` is the whole name as printed. There is no mode, HEPA or other extra "
              "column: `HEPA` is a value of Filter.",
              "- Single Pass holds `Y`, `N` or the words `Non-Recirculating` (wrapped over two lines in the cell). The words are "
              "stored as `Y` (air that is not recirculated is single-pass air) and the words as printed are kept in `raw_cells`; "
              "this is an interpretation to confirm (below).",
              "- Pressure is `+5 Pa`, `-5 Pa`, `-10 Pa`, `Neutral`, or `-ve` (a direction with no figure). `-ve` gives direction "
              "`negative` and a blank `pressure_pa`; `pressure_pa` is signed wherever a figure is printed.",
              "- The toilet, DU (dirty utility), disposal, cleaner and `CAC FEVER Store` rows (" + str(sum(1 for r in res["criteria"] if r["filter"] == "N")) + " rows) print `N` under **Filter, Relative "
              "Humidity and Design Temperature** as well as under Afterhours and Setback. The earlier reading that these were shifted columns was "
              "wrong: the rendered pages show the five `N` in five different columns, so they are as printed (no filter, no humidity "
              "control, no temperature control). `N` in Relative Humidity and Temperature gives blank normalised values, with the `N` kept "
              "as printed; Filter is free text and keeps `N`.",
              "- Relative humidity is printed `≤65` (read as maximum 65 %, `rh_max_pct` = 65).",
              "- The heading `Design Temperature, C` wraps mid-word on parts 1 to 5 (`Temperatur` / `e, C`, header at y = 123 to 156) and does "
              "not on part 6 (header at y = 92 to 112, columns narrower); the header rows are found by words, not by position, so both "
              "read the same eleven columns.",
              "- Recognition: the strip `4.1.8 MECHANICAL VENTILATION STRATEGY | DESIGN CRITERIA` cannot tell the layouts apart, because "
              "Paeds, Palliative, Healthy Aging and part of Psych Med print the same `4.1.8` strip over the standard `Room conditions` tables. "
              "The zoned handler is therefore chosen by the header signature: the caption `Rooms Part N` together with the "
              "`Pressure Regime, Pa` ... `Setback mode, Y/N` header (`zoned_caption`, `ZONED_HEADER_VOCAB`).", ""]
        nfail = len(res["failures"]) + len(res["page_failures"])
        L += ["Failed fields in this file: %s. Rows per table row as printed: %d criteria rows from the six pages "
              "(26 + 24 + 22 + 27 + 19 + 27 when the rows of each rendered page are counted by eye)." % (
                  "none" if not nfail else str(nfail), len(res["criteria"])), ""]
        L += ["Header as printed (text-layer words over each column, read in order):", "",
              "| PDF page | Booklet p. | Caption | " + " | ".join(
                  h for h in ("Room", "Pressure", "Min. OA", "Min. total", "Single pass", "Air class", "Filter", "RH", "Temp", "After", "Setback"))
              + " |", "|---|---|---|" + "---|" * 11]
        keys = ["room", "pressure", "min_oa", "min_total", "single_pass", "air_class", "filter", "rh", "temp", "afterhours", "setback"]
        for z in res["zoned_pages"]:
            L.append("| %s | %s | %s | %s |" % (z["pdf_page"], z["booklet_page"], md(z["caption"]),
                                               " | ".join(md(z["header"].get(k, "(not found)")) for k in keys)))
        L += ["", "Section strip of those pages: `%s`." % res["zoned_pages"][0]["strip"], ""]
        zones: List[str] = []
        for r in res["criteria"]:
            if r["zone"] not in zones:
                zones.append(r["zone"])
        parts = sorted({r["table_part"] for r in res["criteria"]})
        L += ["Rows per zone and table part:", "",
              "| Zone | " + " | ".join(md(pt) for pt in parts) + " | Total | Pressures as printed |",
              "|---|" + "---|" * (len(parts) + 2)]
        for zone in zones:
            zr = [r for r in res["criteria"] if r["zone"] == zone]
            counts = ["%d" % sum(1 for r in zr if r["table_part"] == pt) or "" for pt in parts]
            pr: Dict[str, int] = defaultdict(int)
            for r in zr:
                pr[clean(r["pressure_regime_as_printed"])] += 1
            L.append("| %s | %s | %d | %s |" % (zone or "(no zone read)", " | ".join(c if c != "0" else "" for c in counts), len(zr),
                                                 ", ".join("%s x%d" % kv for kv in sorted(pr.items()))))
        L.append("| **All** | %s | %d | |" % (" | ".join(str(sum(1 for r in res["criteria"] if r["table_part"] == pt)) for pt in parts),
                                           len(res["criteria"])))
        L.append("")
        pa = defaultdict(int)
        for r in res["criteria"]:
            pa[(clean(r["pressure_regime_as_printed"]), r["pressure_pa"], r["pressure_direction"])] += 1
        L += ["Pressure cells as printed, with what they were read as:", "",
              "| As printed | `pressure_pa` | `pressure_direction` | Rows |", "|---|---|---|---|"]
        for (asp, pa_v, dr), n in sorted(pa.items()):
            L.append("| %s | %s | %s | %d |" % (md(asp), pa_v or "(blank)", dr or "(blank)", n))
        sp = defaultdict(int)
        for r in res["criteria"]:
            sp[(json.loads(r["raw_cells"]).get("single_pass", ""), r["single_pass"])] += 1
        L += ["", "Single Pass as printed, with what it was read as: " + "; ".join(
            "`%s` -> `%s` (%d rows)" % (a or "(blank)", b or "(blank)", n) for (a, b), n in sorted(sp.items())) + ".", ""]
        L += ["### %s: the other pages" % res["department"], "",
              "| PDF page | Booklet p. | Class | Rows | Detail |", "|---|---|---|---|---|"]
        for p in res["pages"]:
            if p["class"] != "criteria":
                L.append("| %d | %s | %s | %s | %s |" % (p["pdf_page"], p["booklet_page"] or _unnumbered_label(p), p["class"],
                                                         p["rows"] or "", md(p["detail"])))
        n_tags = sum(1 for z in res["zoning"] if z["label_kind"] == "equipment_tag")
        L += ["", "- **Standby summary** (booklet p. %s): the table has three columns, Equipment Tag, **Area Served** and Equipment Standby "
              "Configuration (note references `1,2`), and no Level or wing column, so `level_wing_heading` is blank on purpose and "
              "is not a failure. Each row names a duty and a standby tag stacked in one cell (`...-01A` over `...-01B`); the handler "
              "writes one register row per tag, both with the same area and configuration (%d rows from %d table rows). The two "
              "numbered notes under the table have no `NOTE` marker and are recorded in `booklet_standby_notes.csv`. The tag of the "
              "`P2 Acute (N)` standby pair is printed `...EDP2ACUTEN-01A` over `...EDP3ACUTEN-01B` (a `3` where the pair has a `2`); it is "
              "kept as printed." % (next((p["booklet_page"] for p in res["pages"] if p["class"] == "standby summary"), "?"),
                                    len(res["standby"]), len(res["standby"]) // 2),
              "- **Zoning keyplans**: three AHU ZONING plans of the 1st storey (booklet p. 136 to 138) carry the labels "
              "`AHU-B-02-01EDP3WAIT-01A/B` ... (a duty and standby pair per label; the reader of the standby table finds `-01A` and "
              "`-01B`). Those labels did not match the tag pattern, so the keyplans showed 0 tags; they now give %d tags (the pair "
              "pattern is switched on only for a file whose criteria use the zoned layout, so the other departments' zoning rows are unchanged). "
              "The two detail keyplans (p. 150 and 151) carry a raster `MODE OF VENTILATION` legend (AC, AC (24HR), HC, MV, N.A, NV) "
              "whose swatches are small; it is now found (the legend image sits higher on the page and its swatches are under 1 %% of "
              "its area), the text is in the image and is not read, and colours are not interpreted." % n_tags,
              "- **Pressure regime pages** (10): drawings with the text labels of rooms over a raster plan; the pressure figures of the "
              "plan (`0 Pa`, `-ve Pa`, `12 ACH`, `Non-Recirculating`) are inside the image and are not in the text layer, so the register "
              "holds the room labels only. The page heading has two lines (`PRESSURE REGIME P3 WAITING & CONSULT` / `BUSINESS AS USUAL`, "
              "`... P2 ACCUTE NORTH` / `PANDEMIC SURGE`); the whole heading is now `page_heading` (before, the second line, which names "
              "the operating mode, was lost as a drawing label). Spelling `ACCUTE` is as printed.", ""]
        L += ["Colours: on part 6 many cells are set in green type (Single Pass, Air Class and, for the CAC rows, Pressure). Colour is not "
              "interpreted by the parser; the green cells are probably the values that the pandemic surge changes, which should be "
              "confirmed from the booklet legend or with the author.", ""]
    L += ["### Paeds: the two failures", "",
          "Paeds parses with 22 criteria rows as before (standard `Room conditions` layout). The two failed fields were:", "",
          "- **Standby area blank** (PDF p17, booklet p117): the Area column of that table is 704 points wide, and the row-rectangle finder "
          "ignored rectangles 700 points or wider (meant for page-wide fills). With the Area cells not found, the Level / wing column edge "
          "fell in the middle of the Area text, and the seven standby rows were mis-split (the first words of each area read as the "
          "level heading, `Level 9 West Core wing Caregiver Accommodation` as one heading with a blank area) while still carrying "
          "confidence 0.97. The limit is now the page width, and the seven rows read as printed (`Level 8 NE wing`: two areas; "
          "`Level 9 NW wing`: two; `Level 9 NE wing`: two; `Level 9 West Core wing`: `Caregiver Accommodation`; configuration "
          "`AHU-CONFIG-E` throughout).",
          "- **Printed page number missing** (PDF p13): the page prints no number. The place where the booklet prints it (taken from the "
          "other pages) is blank on the render, so it is a gap in the source and not a parsing failure; the page map says "
          "`not printed (113 by sequence)` (pages 112 and 114 are printed either side) and `booklet_page` stays blank. The same test "
          "is applied to every file, so pages of other departments that also print no number moved from failures to source gaps "
          "(listed under 'Source gaps'); their rows are unchanged.", ""]
    L += ["### Checked against the previous run", "",
          "The registers of the previous run were kept and compared department by department after this change. Rows of Dialysis, "
          "Healthy Aging Geriatrics, IP FAST, IP SURGICAL, ISO AH, Palliative and Psych Med are identical in all seven registers "
          "(`criteria.csv` with the new `zone` column left out, `criteria_notes.csv`, `booklet_equipment_standby.csv`, "
          "`booklet_standby_notes.csv`, `booklet_zoning_labels.csv`, `booklet_pressure_labels.csv`, `booklet_room_data.csv`). "
          "Paeds differs only in the seven standby rows described above. Emergency is the file that changes: criteria 27 -> %d rows "
          "(failed fields 145 -> %d), standby 29 -> %d, standby notes 0 -> 2, zoning labels 1308 (9 tags instead of 0; the "
          "legend of p. 150 and 151), pressure labels 286 -> %d (the heading's second line is no longer a label)." % (
              sum(len(r["criteria"]) for r in emer), sum(len(r["failures"]) + len(r["page_failures"]) for r in emer),
              sum(len(r["standby"]) for r in emer), sum(len(r["pressure"]) for r in emer)), "",
          "Three parsers are shared by all files and changed: `-ve` / `+ve` give a direction, a relative humidity or temperature of `N` "
          "gives blank values, and `≤65` gives a maximum; no cell of another file reads differently (the comparison above found no change). Pages of other files whose page number is "
          "genuinely absent are source gaps (above). The `ISO AH` and `Healthy Aging` keyplans also carry pair labels that are "
          "recorded as room labels, not tags, because the pair pattern is limited to the Emergency file: the ISO AH labels read "
          "`AHU-B-18-19ISOLNWWG-1A/B` and `AHU-B-18-19ISOLSWWG-1A/B` (`-1A/B`, 4 label rows over four pages), and the Healthy Aging "
          "label is `AHU-A-10-01/04` (a range, not an A/B pair; 1 label row). That is left unchanged here and is worth a decision.", ""]
    L += ["### Parsing decisions to confirm (Emergency)", "",
          "- `Non-Recirculating` is stored as single pass `Y`. The same page prints `Y` for P2 North patient bays in pandemic mode and "
          "`Non-Recirculating` for P2 South NP and CAC rows, which suggests the same meaning, but the booklet does not say so.",
          "- A room name that opens with two zones (`FEVER / RESUS NP`, `FEVER / RESUS NP ANTE MMD LINEN`) is read as zone `FEVER` "
          "(the first word printed). `P2 Waiting / Reception`, `P2 Interview` and `P2 Store` are zone `P2` with no sub-zone printed; "
          "`P2 South Neutra Staff Station / Linen` is zone `P2 South Neutral` (the `l` is missing in the print).",
          "- The zone is derived, not printed, so `zone` is blank for every other department and the room name is never altered. "
          "The pressure regime page titles (P3, ISOL FEVER, P2 ACCUTE NORTH / SOUTH, EDTU, STAFF SUPPORT & COMMON SPACES) use the same "
          "names, except that the criteria table has a `RESUS` and a `CAC` zone for which no pressure regime page is titled.",
          "- In the cross-department comparison below, Emergency is compared zone by zone (`Emergency / P3`, ...) with the zone prefix "
          "taken off the room type, so that a toilet of zone P3 can be compared with a toilet of Dialysis; `room_type_norm` in "
          "`criteria.csv` is unchanged (it keeps the prefix).", ""]
    return L


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
            L.append("| %d | %s | %s | %s | %s | %s |" % (p["pdf_page"], p["booklet_page"] or _unnumbered_label(p),
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
              for r in results for p in r["pages"] if not p["booklet_page"] and "booklet_inferred" not in p]
    if nopage:
        L += ["%d pages have a printed page number that is not in the text layer or is ambiguous (left blank; nothing is "
              "inferred from the sequence): %s." % (len(nopage), "; ".join(nopage)), ""]
    unprinted = ["%s PDF p%d%s" % (r["department"], p["pdf_page"],
                                   " (%s by sequence)" % p["booklet_inferred"] if p["booklet_inferred"] else "")
                 for r in results for p in r["pages"] if "booklet_inferred" in p]
    if unprinted:
        L += ["%d pages print no page number at all (the place where the booklet prints it is blank on the render, so this is a "
              "gap in the source, listed under 'Source gaps' below and not counted as a failed field); `booklet_page` stays blank "
              "in the registers, and the number the sequence implies is given only here, in the page map: %s." % (
                  len(unprinted), "; ".join(unprinted)), ""]

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
    gaps = [(r["department"], pg, msg) for r in results for pg, msg in r.get("page_gaps", [])]
    if gaps:
        L += ["### Source gaps (not failures)", "",
              "| Department | PDF page | What is missing from the source |", "|---|---|---|"]
        for d, pg, msg in gaps:
            L.append("| %s | %s | %s |" % (d, pg, md(msg)))
        L.append("")
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
    L += _emergency_section(results)
    L += ["## Differences between departments for the same room type", ""]
    L += compare_departments(results)
    L += _corrections_section(results)
    L += _corrections_section_2(results)
    L += _folder_update_section(results)
    L += _corrections_section_3(results)
    L += _corrections_section_4(results)
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
