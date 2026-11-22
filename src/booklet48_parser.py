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
5. writes registers under ``out/registers`` and a QA report under ``out/qa``.

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
                "pressure regime", "other")

CRITERIA_FIELDS = [
    "department", "source_file", "booklet_page", "pdf_page", "table_part",
    "room_type_as_printed", "room_type_norm",
    "pressure_regime_as_printed", "pressure_pa", "pressure_direction",
    "min_oa_ach", "min_total_ach", "single_pass", "air_class", "filter",
    "rh_as_printed", "rh_max_pct", "rh_min_pct",
    "temp_as_printed", "temp_min_c", "temp_max_c",
    "afterhours", "setback", "note_refs", "bbox", "confidence", "raw_cells",
]
NOTES_FIELDS = ["file", "page", "note_number", "text", "booklet_page", "pdf_page", "bbox"]
STANDBY_FIELDS = ["department", "source_file", "booklet_page", "pdf_page", "level_wing_heading",
                  "tag_as_written", "tag_normalised", "area", "configuration", "bbox", "confidence"]
ZONING_FIELDS = ["department", "source_file", "booklet_page", "pdf_page", "keyplan_title",
                 "label_kind", "text", "block_text", "bbox", "has_colour_legend"]
PRESSURE_FIELDS = ["department", "source_file", "booklet_page", "pdf_page", "page_heading",
                   "label_kind", "text", "bbox"]

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
    ("temp", r"temperature"),
    ("afterhours", r"afterhours"),
    ("setback", r"setback"),
]
HEADER_VOCAB = {
    "room", "pressure", "regime", "pa", "min.", "outdoor", "ach", "total", "single", "pass",
    "air", "class", "filter", "relative", "humidity", "design", "temperature", "c", "afterhours",
    "mode", "y/n", "setback", ",", "regime,", "temperature,", "mode,", "pa,",
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


def printed_page_number(words: Sequence[Word], page_h: float, page_w: float) -> str:
    """Booklet page as printed: the digits-only word in the bottom-right corner."""
    cands = [w for w in words if w.y0 > page_h * 0.93 and re.fullmatch(r"\d{1,4}", w.text) and w.x0 > page_w * 0.6]
    if not cands:
        cands = [w for w in words if w.y0 > page_h * 0.93 and re.fullmatch(r"\d{1,4}", w.text)]
    if not cands:
        return ""
    return max(cands, key=lambda w: w.x0).text


# --------------------------------------------------------------------------- #
# Page classification
# --------------------------------------------------------------------------- #
def classify_page(text: str) -> str:
    """Class a page from its heading text (whitespace-collapsed)."""
    flat = re.sub(r"\s+", " ", clean(text))
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
    """Normalised room type: lower case, tidy dashes, slashes and ampersands."""
    t = clean(text).lower()
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
    for room in sorted(room_candidates, key=lambda w: w.y0):
        below = [w for w in words if w.y0 >= room.y0 - 3.0 and not is_gutter(w)]
        header: List[Word] = []
        for line in cluster_lines(below):
            if all(w.text.lower() in HEADER_VOCAB or re.fullmatch(r"\d", w.text) for w in line):
                header.extend(line)
            else:
                break
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
        for ref in refs:
            for key, (x0, x1) in mapping.items():
                if x0 - 3 <= ref.cx <= x1 + 3:
                    refs_by_col[key].append(ref.text)
        return TableColumns(mapping, max(w.y1 for w in header), dict(refs_by_col))
    return None


def parse_criteria_page(words: List[Word], page_h: float, page_w: float
                        ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str, List[str]]:
    """Parse one criteria page.

    Returns ``(rows, notes, table_part, page_failures)``.  Each row is a dict with the raw
    cells (``cells``), the room text, the box, the note references and ``mode`` (the
    section heading such as 'Hybrid Cooling' that precedes it).
    """
    failures: List[str] = []
    flat = join_words(words)
    part_m = re.search(r"Room conditions\s*[–—\-]\s*Part\s*(\d+)", flat, re.I)
    table_part = "Part %s" % part_m.group(1) if part_m else ""
    if not table_part:
        failures.append("table part label ('Room conditions - Part N') not found")
    cols = find_header(words)
    if cols is None:
        failures.append("criteria header not found")
        return [], [], table_part, failures
    missing = [k for k, _ in HEADER_KEYS if k not in cols.clusters]
    if missing:
        failures.append("header columns not found: %s" % ", ".join(missing))
        if "room" not in cols.clusters or "pressure" not in cols.clusters:
            return [], [], table_part, failures

    note_words = [w for w in words if re.fullmatch(r"NOTES?:?-?", w.text, re.I) and w.y0 > cols.header_bottom]
    notes_top = min((w.y0 for w in note_words), default=page_h * 0.93)
    footer_top = page_h * 0.93
    body = [w for w in words if w.y0 >= cols.header_bottom - 0.5 and w.y0 < min(notes_top, footer_top) - 0.5
            and not is_gutter(w)]
    # keep the body free of header words and of the 'Room conditions - Part N' caption
    body = [w for w in body if w.y0 >= cols.header_bottom - 0.5]
    caption_lines = [ln for ln in cluster_lines(body) if re.match(r"Room conditions", join_words(ln), re.I)]
    caption_ids = {id(w) for ln in caption_lines for w in ln}
    body = [w for w in body if id(w) not in caption_ids]

    lines = cluster_lines(body)
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

    headings: List[Tuple[float, str, List[Word]]] = []
    orphans: List[str] = []
    for ln in others:
        cy = sum(w.cy for w in ln) / len(ln)
        near = min(anchors, key=lambda a: abs(a["cy"] - cy), default=None)
        has_room = any(cols.column_of(w) == "room" for w in ln)
        cols_used = {cols.column_of(w) for w in ln}
        if near is not None and abs(near["cy"] - cy) <= 14.0:
            near["extra"].append(ln)            # wrapped data cell of that row
        elif has_room or len(cols_used) >= 3:
            by_col = defaultdict(list)
            for w in ln:
                by_col[cols.column_of(w)].append(w)
            anchors.append({"cy": cy, "by_col": by_col, "words": list(ln), "extra": [], "no_pressure": True})
        else:
            headings.append((cy, join_words(ln), ln))
    anchors.sort(key=lambda a: a["cy"])
    for ln in room_only:
        cy = sum(w.cy for w in ln) / len(ln)
        near = min(anchors, key=lambda a: abs(a["cy"] - cy), default=None)
        if near is None or abs(near["cy"] - cy) > 30.0:
            orphans.append(join_words(ln))
            continue
        near["extra"].append(ln)
    for text in orphans:
        failures.append("room text with no data row nearby: %r" % text)

    rows: List[Dict[str, Any]] = []
    pending_mode = ""
    heading_iter = sorted(headings, key=lambda h: h[0])
    hi = 0
    for anchor in anchors:
        while hi < len(heading_iter) and heading_iter[hi][0] < anchor["cy"]:
            pending_mode = heading_iter[hi][1]
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
        for col, col_refs in cols.header_refs.items():
            refs.extend(col_refs)
        rows.append({
            "cells": cells, "box": union_box(w.box() for w in all_words),
            "note_refs": sorted(set(refs), key=lambda s: (len(s), s)),
            "mode": pending_mode, "no_pressure": bool(anchor.get("no_pressure")),
            "pressure_words": [w for w in anchor["by_col"].get("pressure", [])],
        })
        pending_mode = ""   # a section heading applies to the row that follows it
    notes = parse_notes(words, notes_top, footer_top)
    return rows, notes, table_part, failures


def parse_notes(words: Sequence[Word], notes_top: float, footer_top: float
                ) -> List[Dict[str, Any]]:
    """Notes block below a table: '1 - TEXT ...' with wrapped continuation lines."""
    region = [w for w in words if w.y0 >= notes_top - 0.5 and w.y0 < footer_top and not is_gutter(w)]
    notes: List[Dict[str, Any]] = []
    for ln in cluster_lines(region):
        text = join_words(ln)
        if re.fullmatch(r"NOTES?:?-?", text, re.I):
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


def build_criteria_rows(rows: List[Dict[str, Any]], meta: Dict[str, Any], table_part: str,
                        failures: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Turn raw rows into criteria.csv rows, normalising each cell and recording failed fields."""
    out: List[Dict[str, Any]] = []
    for row in rows:
        c = row["cells"]
        failed: List[str] = []
        room = c.get("room", "")
        pa, direction, ok = parse_pressure(c.get("pressure", ""))
        if not ok:
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
        if not room:
            failed.append("room")
        if row.get("no_pressure"):
            failed.append("pressure (cell empty)")
        part = table_part + ("; %s" % row["mode"] if row["mode"] else "")
        confidence = max(0.1, 0.97 - 0.25 * len(failed) - (0.05 if row["mode"] else 0.0))
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
            "pressure_direction": direction,
            "min_oa_ach": oa, "min_total_ach": tot, "single_pass": sp,
            "air_class": c.get("air_class", ""), "filter": c.get("filter", ""),
            "rh_as_printed": c.get("rh", ""), "rh_max_pct": rh_max, "rh_min_pct": rh_min,
            "temp_as_printed": c.get("temp", ""), "temp_min_c": t_min, "temp_max_c": t_max,
            "afterhours": ah, "setback": sb, "note_refs": ";".join(row["note_refs"]),
            "bbox": fmt_bbox(row["box"]), "confidence": "%.2f" % confidence,
            "raw_cells": json.dumps({k: c.get(k, "") for k in
                                     ("min_oa", "min_total", "single_pass", "afterhours", "setback")},
                                    ensure_ascii=False),
        })
    return out


# --------------------------------------------------------------------------- #
# Standby summary
# --------------------------------------------------------------------------- #
def normalise_tag(tag: str) -> str:
    t = clean(tag).upper()
    t = re.sub(r"[‐-―−]", "-", t)
    t = re.sub(r"\s*-\s*", "-", t)
    return re.sub(r"\s+", "", t)


def parse_standby_page(words: List[Word], page_h: float, carry_heading: str,
                       rects: Optional[Sequence[Tuple[float, float, float, float]]] = None
                       ) -> Tuple[List[Dict[str, Any]], str, List[str]]:
    """Parse one standby-summary page.

    The 'Level NN <wing>' heading is a merged cell in the left-hand column, vertically
    centred on its group of rows (not on the first row).  A row belongs to the heading
    whose merged-cell rectangle (``rects``, the filled rectangles of the page) contains
    it; without rectangles the nearest heading within 60 points is used, and a row with
    none takes ``carry_heading`` (the last heading of the previous page).
    """
    failures: List[str] = []
    tag_w = [w for w in words if w.text == "Tag" and not is_gutter(w)]
    area_w = [w for w in words if w.text == "Area" and not is_gutter(w)]
    if not tag_w or not area_w:
        return [], carry_heading, ["standby header (Equipment Tag / Area) not found"]
    tag_h = min(tag_w, key=lambda w: w.y0)
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
    if set(mapping) != {"tag", "area", "config"}:
        return [], carry_heading, ["standby header columns not derived (got %s)" % sorted(mapping)]
    cols = TableColumns(mapping, header_bottom, {}, centre_bounds=True)
    note_words = [w for w in words if re.fullmatch(r"NOTES?:?-?", w.text, re.I) and w.y0 > header_bottom]
    notes_top = min((w.y0 for w in note_words), default=page_h * 0.93)
    body = [w for w in words if w.y0 > header_bottom - 0.5 and w.y0 < min(notes_top, page_h * 0.93) - 0.5
            and not is_gutter(w)]
    tag_like = [w for w in body if TAG_RE.match(w.text)]
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

    rows: List[Dict[str, Any]] = []
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


def zoning_records(page: "pymupdf.Page", words: List[Word]) -> Tuple[str, List[Dict[str, Any]], bool]:
    """Keyplan title, every tag and room label with bbox, and the legend flag."""
    lines = [ln for ln in page_lines(page) if not FURNITURE_LINE_RE.match(ln["text"])]
    title = ""
    for ln in lines:
        if "KEYPLAN" in ln["text"].upper():
            title = re.sub(r"\s+", " ", ln["text"])
            break
    records: List[Dict[str, Any]] = []
    for w in words:
        if is_gutter(w):
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
    return title, records, has_colour_legend(page, words)


def pressure_records(page: "pymupdf.Page") -> Tuple[str, List[Dict[str, Any]]]:
    """Text labels (room names, +/- values, comments) of a pressure regime drawing."""
    heading = ""
    records: List[Dict[str, Any]] = []
    for ln in page_lines(page):
        text = ln["text"]
        if FURNITURE_LINE_RE.match(text):
            continue
        if re.match(r"^PRESSURE REGIME", text):
            heading = text
            continue
        kind = "pressure_value" if PRESSURE_VALUE_RE.match(text) or text in ("+", "-") else "text_label"
        records.append({"label_kind": kind, "text": text, "bbox": fmt_bbox(ln["box"])})
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


def parse_pdf(path: Path, root: Path) -> Dict[str, Any]:
    """Parse one booklet PDF into registers, page map and failures."""
    dept = department_from_name(path)
    src = display_path(path, root)
    result: Dict[str, Any] = {"department": dept, "source_file": src, "pages": [], "criteria": [],
                              "notes": [], "standby": [], "zoning": [], "pressure": [],
                              "failures": [], "page_failures": []}
    doc = pymupdf.open(str(path))
    carry = ""
    try:
        for index in range(len(doc)):
            page = doc[index]
            pdf_page = index + 1
            words = page_words(page)
            page_h, page_w = page.rect.height, page.rect.width
            booklet = printed_page_number(words, page_h, page_w)
            cls = classify_page(page.get_text())
            meta = {"department": dept, "source_file": src, "pdf_page": pdf_page, "booklet_page": booklet}
            entry = {"pdf_page": pdf_page, "booklet_page": booklet, "class": cls, "detail": "", "rows": 0}
            if not booklet:
                result["page_failures"].append((pdf_page, "printed page number not found"))
            if cls == "criteria":
                rows, notes, part, pfail = parse_criteria_page(words, page_h, page_w)
                built = build_criteria_rows(rows, meta, part, result["failures"])
                result["criteria"].extend(built)
                for n in notes:
                    result["notes"].append({"file": src, "page": booklet, "note_number": n["number"],
                                            "text": n["text"], "booklet_page": booklet,
                                            "pdf_page": pdf_page, "bbox": fmt_bbox(n["box"])})
                entry.update(detail=part, rows=len(built), notes=len(notes))
                result["page_failures"].extend((pdf_page, f) for f in pfail)
            elif cls == "standby summary":
                rects = [tuple(d["rect"]) for d in page.get_drawings()
                         if d.get("fill") is not None and d["rect"].width > 20 and d["rect"].height > 10]
                rows, carry, pfail = parse_standby_page(words, page_h, carry, rects)
                for r in rows:
                    c = r["cells"]
                    result["standby"].append({
                        "department": dept, "source_file": src, "booklet_page": booklet, "pdf_page": pdf_page,
                        "level_wing_heading": r["heading"], "tag_as_written": c.get("tag", ""),
                        "tag_normalised": normalise_tag(c.get("tag", "")), "area": c.get("area", ""),
                        "configuration": c.get("config", ""), "bbox": fmt_bbox(r["box"]),
                        "confidence": "0.97" if (c.get("area") and c.get("config") and r["heading"]) else "0.50",
                    })
                    for field, key in (("area", "area"), ("configuration", "config"), ("level_wing_heading", None)):
                        val = r["heading"] if key is None else c.get(key, "")
                        if not val:
                            result["failures"].append({"file": src, "pdf_page": pdf_page, "booklet_page": booklet,
                                                       "row": c.get("tag", ""), "field": "standby " + field,
                                                       "cell": "", "bbox": fmt_bbox(r["box"])})
                entry.update(rows=len(rows), detail="standby rows")
                result["page_failures"].extend((pdf_page, f) for f in pfail)
            elif cls == "zoning keyplan":
                title, recs, legend = zoning_records(page, words)
                if not title:
                    result["page_failures"].append((pdf_page, "keyplan title not found"))
                for rec in recs:
                    result["zoning"].append({**meta, "keyplan_title": title, "has_colour_legend": "Y" if legend else "N", **rec})
                tags = sum(1 for r in recs if r["label_kind"] == "equipment_tag")
                entry.update(rows=len(recs), detail="%s; %d tags; colour legend %s" % (title or "no title", tags, "yes" if legend else "no"))
            elif cls == "pressure regime":
                heading, recs = pressure_records(page)
                for rec in recs:
                    result["pressure"].append({**meta, "page_heading": heading, **rec})
                entry.update(rows=len(recs), detail=heading or "PRESSURE REGIME")
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
COMPARE_FIELDS = ["pressure_regime_as_printed", "min_oa_ach", "min_total_ach", "single_pass", "air_class",
                  "filter", "rh_as_printed", "temp_as_printed", "afterhours", "setback"]


def _mode_of(part: str) -> str:
    return part.split(";", 1)[1].strip() if ";" in part else ""


def compare_departments(results: List[Dict[str, Any]]) -> List[str]:
    """Markdown lines describing differences between departments for the same room type."""
    lines: List[str] = []
    depts = [r for r in results if r["criteria"]]
    if len(depts) < 2:
        return ["Fewer than two departments with criteria rows; nothing to compare."]
    for i in range(len(depts)):
        for j in range(i + 1, len(depts)):
            a, b = depts[i], depts[j]
            lines.append("### %s versus %s" % (a["department"], b["department"]))
            lines.append("")

            def keyed(res: Dict[str, Any]) -> Dict[Tuple[str, str, int], Dict[str, Any]]:
                seen: Dict[Tuple[str, str], int] = defaultdict(int)
                out: Dict[Tuple[str, str, int], Dict[str, Any]] = {}
                for r in res["criteria"]:
                    base = (r["room_type_norm"], _mode_of(r["table_part"]))
                    out[(base[0], base[1], seen[base])] = r
                    seen[base] += 1
                return out

            ka, kb = keyed(a), keyed(b)
            common_keys = [k for k in ka if k in kb]
            diff_rows: List[str] = []
            same = 0
            for k in common_keys:
                ra, rb = ka[k], kb[k]
                diffs = [(f, ra[f], rb[f]) for f in COMPARE_FIELDS if clean(ra[f]) != clean(rb[f])]
                label = ra["room_type_as_printed"] + (" [%s]" % k[1] if k[1] else "")
                if diffs:
                    for f, va, vb in diffs:
                        diff_rows.append("| %s | %s | %s | %s |" % (label, f, va or "(blank)", vb or "(blank)"))
                else:
                    same += 1
            lines.append("Room types matched by normalised name: %d (%d identical on all %d compared fields)."
                         % (len(common_keys), same, len(COMPARE_FIELDS)))
            lines.append("")
            if diff_rows:
                lines.append("| Room type | Field | %s | %s |" % (a["department"], b["department"]))
                lines.append("|---|---|---|---|")
                lines.extend(diff_rows)
            else:
                lines.append("No value differences among the matched room types.")
            lines.append("")
            only_a = [k for k in ka if k not in kb]
            only_b = [k for k in kb if k not in ka]
            matched_b: set = set()
            near: List[str] = []
            for k in list(only_a):
                best, best_ratio = None, 0.0
                for kk in only_b:
                    ratio = difflib.SequenceMatcher(None, k[0], kk[0]).ratio()
                    comps_a = {c.strip() for c in k[0].split("/")}
                    comps_b = {c.strip() for c in kk[0].split("/")}
                    if comps_a & comps_b:
                        ratio = max(ratio, 0.9)
                    if k[1] != kk[1]:
                        ratio -= 0.3        # a different operating mode is not the same room type
                    if ratio > best_ratio:
                        best, best_ratio = kk, ratio
                if best is not None and best_ratio >= 0.7:
                    matched_b.add(best)
                    only_a.remove(k)
                    ra, rb = ka[k], kb[best]
                    vals = ["%s: %s | %s" % (f, ra[f] or "(blank)", rb[f] or "(blank)")
                            for f in COMPARE_FIELDS if clean(ra[f]) != clean(rb[f])]
                    near.append("| %s | %s | %s |" % (ra["room_type_as_printed"] + (" [%s]" % k[1] if k[1] else ""),
                                                      rb["room_type_as_printed"] + (" [%s]" % best[1] if best[1] else ""),
                                                      "; ".join(vals) or "identical on compared fields"))
            only_b = [k for k in only_b if k not in matched_b]
            if near:
                lines.append("Near matches (names differ, so not compared automatically; check by eye):")
                lines.append("")
                lines.append("| %s | %s | Differences (%s | %s) |" % (a["department"], b["department"],
                                                                     a["department"], b["department"]))
                lines.append("|---|---|---|")
                lines.extend(near)
                lines.append("")
            if only_a:
                lines.append("Only in %s: %s." % (a["department"], "; ".join(
                    ka[k]["room_type_as_printed"] + (" [%s]" % k[1] if k[1] else "") for k in only_a)))
            if only_b:
                lines.append("Only in %s: %s." % (b["department"], "; ".join(
                    kb[k]["room_type_as_printed"] + (" [%s]" % k[1] if k[1] else "") for k in only_b)))
            lines.append("")
    return lines


def build_report(results: List[Dict[str, Any]], notes_all: List[Dict[str, Any]]) -> str:
    """The QA report as Markdown."""
    L: List[str] = ["# Booklet Section 4.8 parser report", "",
                    "Generated by `python3 -m src.booklet48_parser run` on %s." % common.utc_now_iso(), "",
                    "Values come from the text layer (words with boxes); zero-width spaces are stripped and "
                    "every figure is kept as printed beside its normalised value in `criteria.csv`. "
                    "Bounding boxes are in PDF points (x0, y0, x1, y1; origin top left).", ""]
    L += ["## Page map", ""]
    for res in results:
        L += ["### %s (%s)" % (res["department"], res["source_file"]), "",
              "| PDF page | Booklet page | Class | Rows | Detail |", "|---|---|---|---|---|"]
        for p in res["pages"]:
            L.append("| %d | %s | %s | %s | %s |" % (p["pdf_page"], p["booklet_page"] or "?", p["class"],
                                                  p["rows"] if p["rows"] else "", p["detail"]))
        L.append("")
    L += ["## Row counts", "",
          "| Department | Criteria rows | Criteria notes | Standby rows | Zoning labels (tags) | Pressure labels |",
          "|---|---|---|---|---|---|"]
    for res in results:
        ntags = sum(1 for z in res["zoning"] if z["label_kind"] == "equipment_tag")
        nn = sum(1 for n in notes_all if n["file"] == res["source_file"])
        L.append("| %s | %d | %d | %d | %d (%d) | %d |" % (res["department"], len(res["criteria"]), nn,
                                                           len(res["standby"]), len(res["zoning"]), ntags,
                                                           len(res["pressure"])))
    L += ["", "Criteria rows per table part:", ""]
    for res in results:
        parts: Dict[str, int] = defaultdict(int)
        for r in res["criteria"]:
            parts[r["table_part"].split(";")[0]] += 1
        L.append("- %s: %s" % (res["department"], ", ".join("%s = %d" % kv for kv in sorted(parts.items())) or "none"))
    L += ["", "Zoning keyplan colour legend (text beside coloured swatches): "
          + "; ".join("%s p%s %s" % (r["department"], p["booklet_page"], "yes" if "colour legend yes" in p["detail"] else "no")
                      for r in results for p in r["pages"] if p["class"] == "zoning keyplan"), ""]

    L += ["## Criteria notes (verbatim)", "", "| File | Page | No. | Text |", "|---|---|---|---|"]
    for n in notes_all:
        L.append("| %s | %s | %s | %s |" % (n["file"], n["page"], n["note_number"], n["text"]))
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
                L.append("| %s | %s | %s | %s | %s | %s |" % (f["pdf_page"], f["booklet_page"], f["row"], f["field"],
                                                           f["cell"] or "(blank)", f["bbox"]))
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
          "- Section rows inside a criteria table (`Hybrid Cooling`, `Pandemic Mode`, centred, no room or pressure text) "
          "are applied to the row that follows them only; the heading is appended to `table_part` "
          "(for example `Part 2; Hybrid Cooling`). The row after the Hybrid Cooling row (`Corridor`) therefore carries no mode; "
          "the printed layout does not say whether it belongs to that section.",
          "- Pressure direction is left blank for `NR` cells.",
          "- Equipment standby notes (1 and 2) are not written to `criteria_notes.csv`.", ""]
    L += ["## Differences between departments for the same room type", ""]
    L += compare_departments(results)
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
    zoning = [r for res in results for r in res["zoning"]]
    pressure = [r for res in results for r in res["pressure"]]
    common.write_csv_dicts(registers / "criteria.csv", criteria, CRITERIA_FIELDS)
    common.write_csv_dicts(registers / "criteria_notes.csv", notes, NOTES_FIELDS)
    common.write_csv_dicts(registers / "booklet_equipment_standby.csv", standby, STANDBY_FIELDS)
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
