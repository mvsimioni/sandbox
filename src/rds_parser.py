"""Template-driven parser for Room Data Sheets (RDS) of the AIGH IP ACMV pipeline.

The parser reads the PDF text layer only (PyMuPDF characters with boxes, grouped
into lines and read as words) plus the ruling lines of the drawing layer.  It never
guesses: a value is read only from a region that is defined relative to a printed
label (and the rule lines or the next label), every field carries a confidence and
a method, and printed labels the template does not know are reported as
unmatched rather than interpreted.

Per-layout templates live in ``src/rds_layouts.json`` (label text variants and the
value-region rule of each field).  Rules supported by the engine:

``row_band``  label in a left column, value to its right.  The value region is the
              cell bounded by the vertical divider rule that sits to the right of
              the label (confidence 1.0); without a divider the region runs from
              the label to the next label above and below (confidence 0.7).
``below``     value region directly below the label, up to the next label or rule.
``inline``    "label value" pairs inside a ruled section, in two columns; the value
              is whatever follows the label on its row (labels and values can be
              glued in the text layer, so matching is done on glyphs, not words).
``grid``      a header row of flag names with the value in the cell below.
``table``     ruled tables: columns from the vertical rules in the header row,
              rows from the horizontal rules.

Confidence: 1.0 label and value region exact (rule bounded), 0.7 region
heuristics, 0.4 fallback (fuzzy label match).  Coordinates are PDF points with the
origin at the top left of the page (PyMuPDF convention).

Command line::

    python3 -m src.rds_parser run --manifest out/registers/manifest.csv [--dept 30]
    python3 -m src.rds_parser parse FILE.pdf        # one file, JSON to stdout
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import common

LAYOUTS_PATH = Path(__file__).with_name("rds_layouts.json")

CONF_EXACT = 1.0
CONF_HEURISTIC = 0.7
CONF_FALLBACK = 0.4

FIELD_COLUMNS = [
    "rel_path", "dept_no", "page", "layout_id", "field_label_as_printed", "field_key",
    "value_as_printed", "value_bbox", "label_bbox", "confidence", "method",
    "room_code", "section", "row", "sha256",
]
UNMATCHED_COLUMNS = ["rel_path", "dept_no", "page", "layout_id", "room_code", "kind", "label_as_printed", "label_bbox", "reason"]
ANNOT_COLUMNS = ["rel_path", "dept_no", "page", "room_code", "annot_type", "bbox", "content", "overlaps_field", "author", "subject", "created"]
SUMMARY_COLUMNS = [
    "rel_path", "dept_no", "status", "room_count", "room_code", "room_name", "department", "area_m2",
    "occupancy", "occupancy_staff", "occupancy_patients", "occupancy_visitors", "hours", "temperature",
    "humidity", "pressure", "ach", "acmv_notes", "equipment_count", "services_item_count", "revision", "date", "date_annotation",
    "print_date", "layout_id", "layout_pages", "fields_found", "fields_expected", "coverage_pct",
    "unmatched_labels", "missing_labels", "qty_summary_check", "annotation_count", "pages", "note",
]


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def snake(text: str) -> str:
    """Normalise a printed label to a snake_case key."""
    out = re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")
    return out


def compact(text: str) -> str:
    """Lower-case text with every whitespace character removed (matching key)."""
    return re.sub(r"\s+", "", str(text)).lower()


def fmt_bbox(bbox: Optional[Sequence[float]]) -> str:
    """Format a bbox as ``x0,y0,x1,y1`` in PDF points with one decimal."""
    if not bbox:
        return ""
    return ",".join("%.1f" % float(v) for v in bbox)


def union_bbox(boxes: Iterable[Sequence[float]]) -> Optional[Tuple[float, float, float, float]]:
    """Union of bounding boxes, or ``None`` for an empty input."""
    boxes = list(boxes)
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def load_layouts(path: Optional[Path | str] = None) -> Dict[str, Any]:
    """Load and validate the layout templates (data file, default ``src/rds_layouts.json``)."""
    target = Path(path) if path else LAYOUTS_PATH
    with target.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get("schema") != 1:
        raise ValueError("%s: unsupported schema %r" % (target, data.get("schema")))
    for key in ("layouts", "families"):
        if not isinstance(data.get(key), list) or not data[key]:
            raise ValueError("%s: missing %s" % (target, key))
    data.setdefault("normalise", {})
    data.setdefault("defaults", {})
    data.setdefault("column_sets", {})
    for name, default in (("x_max", 566.0), ("label_x_max", 150.0), ("fuzzy_min", 0.84),
                          ("section_rule_min_span", 330.0), ("footer_y", 784.0), ("row_tol", 4.0)):
        data["defaults"].setdefault(name, default)
    data["_by_id"] = {lay["id"]: lay for lay in data["layouts"]}
    data["_family_by_id"] = {fam["id"]: fam for fam in data["families"]}
    return data


# --------------------------------------------------------------------------- #
# Page model: glyphs, lines, rules
# --------------------------------------------------------------------------- #
@dataclass
class Glyph:
    """One character (or one letter of an expanded ligature) with its box."""

    c: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def ws(self) -> bool:
        return self.c.isspace()

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2.0


@dataclass
class Line:
    """A text line as delivered by the PDF text layer, with per-glyph boxes."""

    id: int
    glyphs: List[Glyph]
    bbox: Tuple[float, float, float, float]
    font: str
    size: float
    bold: bool
    compact: str = ""
    cmap: List[int] = field(default_factory=list)

    @property
    def cy(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2.0

    @property
    def text(self) -> str:
        return re.sub(r"\s+", " ", "".join(g.c for g in self.glyphs)).strip()


@dataclass
class Rule:
    """A horizontal (``kind`` h) or vertical (``kind`` v) ruling segment."""

    kind: str
    pos: float
    a: float
    b: float
    dashed: bool = False


@dataclass
class PageModel:
    """Everything the parser needs from one page."""

    number: int
    width: float
    height: float
    lines: List[Line]
    h: List[Rule]
    v: List[Rule]
    annots: List[Dict[str, Any]]
    claimed: set = field(default_factory=set)


def _expand(ch: str, normalise: Dict[str, str]) -> str:
    return normalise.get(ch, ch)


def open_clean(path: Path | str) -> Tuple[Any, Dict[int, List[Dict[str, Any]]]]:
    """Open a PDF from bytes, read its reviewer markup and return a copy without it.

    Annotations (FreeText comments, arrows, ...) are part of the text MuPDF extracts, so a
    corrected date typed over the printed one would be read as ``APR 2025AUG 2026``.  The
    markup is therefore recorded first (``annots`` by 0-based page) and removed from an
    in-memory copy; the source file is never written.
    """
    import pymupdf

    doc = pymupdf.open(stream=Path(path).read_bytes(), filetype="pdf")
    marks: Dict[int, List[Dict[str, Any]]] = {}
    touched = False
    for page in doc:
        found: List[Dict[str, Any]] = []
        for an in page.annots() or []:
            info = an.info or {}
            found.append({
                "type": an.type[1], "rect": tuple(float(v) for v in an.rect),
                "content": (info.get("content") or "").replace("\r", "\n").strip(), "author": info.get("title") or "",
                "subject": info.get("subject") or "", "created": info.get("creationDate") or "",
            })
        if found:
            marks[page.number] = found
            doc.xref_set_key(page.xref, "Annots", "null")
            touched = True
    if touched:
        clean = pymupdf.open(stream=doc.tobytes(), filetype="pdf")
        doc.close()
        doc = clean
    return doc, marks


def build_page(page: Any, layouts: Dict[str, Any], annots: Optional[List[Dict[str, Any]]] = None) -> PageModel:
    """Extract lines (with glyph boxes), ruling segments and annotations from a PyMuPDF page."""
    normalise = layouts.get("normalise", {})
    raw = page.get_text("rawdict")
    lines: List[Line] = []
    for block in raw.get("blocks", []):
        for ln in block.get("lines", []):
            glyphs: List[Glyph] = []
            font, size, bold = "", 0.0, False
            for span in ln.get("spans", []):
                if not font and any(not c["c"].isspace() for c in span.get("chars", [])):
                    font, size = span.get("font", ""), float(span.get("size", 0.0))
                    bold = "bold" in font.lower() or bool(span.get("flags", 0) & 16)
                for c in span.get("chars", []):
                    x0, y0, x1, y1 = c["bbox"]
                    for letter in _expand(c["c"], normalise):
                        glyphs.append(Glyph(letter, x0, y0, x1, y1))
            if not any(not g.ws for g in glyphs):
                continue
            bb = tuple(float(v) for v in ln["bbox"])
            line = Line(len(lines), glyphs, bb, font, size, bold)
            comp, cmap = [], []
            for idx, g in enumerate(glyphs):
                if not g.ws:
                    comp.append(g.c.lower())
                    cmap.append(idx)
            line.compact = "".join(comp)
            line.cmap = cmap
            lines.append(line)
    hs: List[Rule] = []
    vs: List[Rule] = []

    def add_seg(p1: Any, p2: Any, dashed: bool) -> None:
        if abs(p1.y - p2.y) <= 0.3 and abs(p1.x - p2.x) > 0.5:
            hs.append(Rule("h", (p1.y + p2.y) / 2.0, min(p1.x, p2.x), max(p1.x, p2.x), dashed))
        elif abs(p1.x - p2.x) <= 0.3 and abs(p1.y - p2.y) > 0.5:
            vs.append(Rule("v", (p1.x + p2.x) / 2.0, min(p1.y, p2.y), max(p1.y, p2.y), dashed))

    try:
        drawings = page.get_drawings()
    except Exception:  # noqa: BLE001 - a broken drawing layer must not stop text parsing
        drawings = []
    for dr in drawings:
        dashes = str(dr.get("dashes") or "")
        dashed = bool(dashes) and dashes.strip() not in ("[] 0", "[]  0", "")
        for item in dr.get("items", []):
            if item[0] == "l":
                add_seg(item[1], item[2], dashed)
            elif item[0] == "re":
                r = item[1]
                if r.height <= 1.5 and r.width > 1.5:
                    hs.append(Rule("h", (r.y0 + r.y1) / 2.0, r.x0, r.x1, dashed))
                elif r.width <= 1.5 and r.height > 1.5:
                    vs.append(Rule("v", (r.x0 + r.x1) / 2.0, r.y0, r.y1, dashed))
    return PageModel(page.number + 1, float(page.rect.width), float(page.rect.height), lines, hs, vs, list(annots or []))


# --------------------------------------------------------------------------- #
# Reading text out of regions
# --------------------------------------------------------------------------- #
def render(selection: Sequence[Tuple[Line, Sequence[Glyph]]]) -> Tuple[str, Optional[Tuple[float, float, float, float]]]:
    """Join selected glyphs into reading-order text and return ``(text, bbox)``.

    Glyphs stay with their line; lines on the same visual row are merged left to
    right, rows are read top to bottom and a row ending in a hyphen is joined to
    the next row without a space (so wrapped codes such as ``CW-FCB-002E-`` /
    ``SS`` read as one token).
    """
    segs = [(ln, [g for g in gl]) for ln, gl in selection if gl]
    if not segs:
        return "", None
    segs.sort(key=lambda s: (s[0].cy, s[0].bbox[0]))
    rows: List[List[Tuple[Line, List[Glyph]]]] = []
    for seg in segs:
        if rows and abs(seg[0].cy - rows[-1][0][0].cy) <= 3.5:
            rows[-1].append(seg)
        else:
            rows.append([seg])
    row_texts: List[str] = []
    boxes: List[Tuple[float, float, float, float]] = []
    for row in rows:
        row.sort(key=lambda s: s[1][0].x0)
        parts: List[str] = []
        prev_end: Optional[float] = None
        for _ln, gl in row:
            txt = "".join(g.c for g in gl)
            real = [g for g in gl if not g.ws]
            if real:
                boxes.extend((g.x0, g.y0, g.x1, g.y1) for g in real)
                if prev_end is not None and real[0].x0 - prev_end > 1.2 and parts and not parts[-1].endswith(" ") and not txt.startswith(" "):
                    parts.append(" ")
                prev_end = real[-1].x1
            parts.append(txt)
        row_texts.append(re.sub(r"\s+", " ", "".join(parts)).strip())
    out = ""
    for txt in row_texts:
        if not txt:
            continue
        if out and not out.endswith("-"):
            out += " "
        out += txt
    return out.strip(), union_bbox(boxes)


def glyphs_in(page: PageModel, region: Sequence[float], skip: Optional[set] = None) -> List[Tuple[Line, List[Glyph]]]:
    """Glyphs whose centre lies inside ``region`` (x0, y0, x1, y1), grouped by line."""
    x0, y0, x1, y1 = region
    sel: List[Tuple[Line, List[Glyph]]] = []
    for ln in page.lines:
        if skip and ln.id in skip:
            continue
        if ln.bbox[3] < y0 - 1 or ln.bbox[1] > y1 + 1 or ln.bbox[2] < x0 - 1 or ln.bbox[0] > x1 + 1:
            continue
        gl = [g for g in ln.glyphs if x0 <= g.cx <= x1 and y0 <= g.cy <= y1]
        if any(not g.ws for g in gl):
            sel.append((ln, gl))
    return sel


# --------------------------------------------------------------------------- #
# Label matching
# --------------------------------------------------------------------------- #
@dataclass
class LabelMatch:
    """A label located on a page."""

    bbox: Tuple[float, float, float, float]
    lines: List[Line]
    printed: str
    how: str            # exact | fuzzy
    variant: int
    end_index: int = 0  # compact index just after the label inside lines[0] (prefix mode)
    ratio: float = 1.0


def _variant_parts(variant: Any) -> List[str]:
    return [variant] if isinstance(variant, str) else list(variant)


def _in_region(ln: Line, region: Optional[Sequence[float]], by: str = "centre") -> bool:
    if region is None:
        return True
    x0, y0, x1, y1 = region
    cx = (ln.bbox[0] + ln.bbox[2]) / 2.0
    if by == "start":
        cx = ln.bbox[0]
    return x0 <= cx <= x1 and y0 <= ln.cy <= y1


def find_labels(page: PageModel, variants: Sequence[Any], *, mode: str = "line", region: Optional[Sequence[float]] = None,
                fuzzy_min: float = 0.84, fuzzy: bool = False, by: str = "centre") -> List[LabelMatch]:
    """Locate a label by its printed text (any of ``variants``).

    ``mode`` ``line`` needs the whole line to equal the label; ``prefix`` lets the
    label be followed by its value in the same line (glued text).  A variant may be
    a list of parts, the label then spanning consecutive lines.  ``fuzzy`` enables
    the fallback match (similarity at least ``fuzzy_min``), reported as ``fuzzy``.
    """
    found: List[LabelMatch] = []
    used: set = set()
    for vi, variant in enumerate(variants):
        parts = [compact(p) for p in _variant_parts(variant)]
        for ln in page.lines:
            if ln.id in page.claimed or ln.id in used or not _in_region(ln, region, by):
                continue
            first = parts[0]
            ok = ln.compact == first if mode == "line" else ln.compact.startswith(first)
            if not ok:
                continue
            chain = [ln]
            for part in parts[1:]:
                nxt = None
                for cand in page.lines:
                    if cand.id in page.claimed or cand.id in used or cand is chain[-1] or cand.compact != part:
                        continue
                    prev = chain[-1]
                    dy = cand.bbox[1] - prev.bbox[1]
                    overlap = min(cand.bbox[2], prev.bbox[2] + 25) - max(cand.bbox[0], prev.bbox[0] - 25)
                    if 0 <= dy <= 16 and overlap > 0:
                        nxt = cand
                        break
                if nxt is None:
                    chain = []
                    break
                chain.append(nxt)
            if not chain:
                continue
            if mode == "prefix":
                end_idx = len(first)
                gl = [ln.glyphs[i] for i in ln.cmap[:end_idx]]
                bb = union_bbox((g.x0, g.y0, g.x1, g.y1) for g in gl)
                printed = "".join(g.c for g in ln.glyphs[: ln.cmap[end_idx - 1] + 1]).strip()
                boxes = [bb] if bb else []
            else:
                end_idx = len(ln.compact)
                boxes = [c.bbox for c in chain]
                printed = " ".join(c.text for c in chain)
            bbox = union_bbox(boxes)
            if bbox is None:
                continue
            found.append(LabelMatch(bbox, chain, printed, "exact", vi, end_idx))
            used.update(c.id for c in chain)
    if not found and fuzzy:
        best: Optional[LabelMatch] = None
        for vi, variant in enumerate(variants):
            parts = [compact(p) for p in _variant_parts(variant)]
            if len(parts) != 1 or len(parts[0]) < 5:
                continue
            for ln in page.lines:
                if ln.id in page.claimed or not _in_region(ln, region, by):
                    continue
                probe = ln.compact if mode == "line" else ln.compact[: len(parts[0])]
                ratio = difflib.SequenceMatcher(None, parts[0], probe).ratio()
                if ratio >= fuzzy_min and (best is None or ratio > best.ratio):
                    end_idx = len(probe)
                    if mode == "prefix":
                        gl = [ln.glyphs[i] for i in ln.cmap[:end_idx]]
                        bb = union_bbox((g.x0, g.y0, g.x1, g.y1) for g in gl)
                        printed = "".join(g.c for g in ln.glyphs[: ln.cmap[end_idx - 1] + 1]).strip()
                    else:
                        bb, printed = ln.bbox, ln.text
                    best = LabelMatch(bb, [ln], printed, "fuzzy", vi, end_idx, ratio)
        if best:
            found.append(best)
    found.sort(key=lambda m: (m.bbox[1], m.bbox[0]))
    return found


def claim(page: PageModel, match: LabelMatch) -> None:
    """Mark the lines of a matched label as used."""
    page.claimed.update(ln.id for ln in match.lines)


# --------------------------------------------------------------------------- #
# Field records
# --------------------------------------------------------------------------- #
@dataclass
class FieldRec:
    """One extracted value with its provenance."""

    page: int
    layout_id: str
    label: str
    key: str
    value: str
    value_bbox: Optional[Tuple[float, float, float, float]]
    label_bbox: Optional[Tuple[float, float, float, float]]
    confidence: float
    method: str
    section: str = ""
    row: str = ""
    room_index: int = 0
    room_code: str = ""


@dataclass
class Unmatched:
    """A printed label (or template item) the template could not place."""

    page: int
    layout_id: str
    kind: str           # unmatched_label | missing_label | unknown_page | unparsed_table
    label: str
    bbox: Optional[Tuple[float, float, float, float]]
    reason: str
    room_index: int = 0
    room_code: str = ""


@dataclass
class PageResult:
    """The outcome of parsing one page."""

    page: int
    layout_id: str
    header: Dict[str, Tuple[str, Optional[Tuple[float, float, float, float]]]]
    fields: List[FieldRec] = field(default_factory=list)
    unmatched: List[Unmatched] = field(default_factory=list)
    expected: int = 0
    found: int = 0
    score: float = 0.0
    starts_room: bool = False
    tables_seen: List[str] = field(default_factory=list)
    layout_ids: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Layout detection and header
# --------------------------------------------------------------------------- #
def select_layouts(page: PageModel, layouts: Dict[str, Any], family: Dict[str, Any]) -> List[Tuple[Dict[str, Any], float]]:
    """Page templates whose anchor labels are present on the page, best first.

    A page can carry the tables of more than one template (for example the
    security systems schedule above the quantity summary), so every template whose
    anchor share reaches its ``min_fraction`` is applied, best score first; a line
    claimed by one template cannot be read again by another.
    """
    picked: List[Tuple[Dict[str, Any], float]] = []
    for lid in family["layouts"]:
        lay = layouts["_by_id"][lid]
        anchors = lay["match"]["anchors"]
        hit = sum(1 for a in anchors if any(ln.compact == compact(a) for ln in page.lines))
        score = hit / float(len(anchors))
        if hit and score >= lay["match"].get("min_fraction", 0.6) - 1e-9:
            picked.append((lay, score))
    picked.sort(key=lambda t: -t[1])
    return picked


def pick_family(page: PageModel, layouts: Dict[str, Any]) -> Dict[str, Any]:
    """Choose the family by page size (the only one in this release is DRF1)."""
    for fam in layouts["families"]:
        w, h = fam["page_size"]
        tol = fam.get("size_tol", 6.0)
        if abs(page.width - w) <= tol and abs(page.height - h) <= tol:
            return fam
    return layouts["families"][0]


def read_header(page: PageModel, family: Dict[str, Any]) -> Dict[str, Tuple[str, Optional[Tuple[float, float, float, float]]]]:
    """Read the unlabelled header band (tag, room name, room code) by region."""
    out = {}
    for spec in family.get("header", {}).get("fields", []):
        text, bb = render(glyphs_in(page, spec["region"]))
        out[spec["key"]] = (text, bb)
    return out


# --------------------------------------------------------------------------- #
# Rule helpers
# --------------------------------------------------------------------------- #
def full_width_rules(page: PageModel, x0: float, x1: float, min_span: float) -> List[float]:
    """Y positions of solid horizontal rules that cover at least ``min_span`` of [x0, x1]."""
    by_y: Dict[float, List[Tuple[float, float]]] = defaultdict(list)
    for r in page.h:
        if r.dashed:
            continue
        a, b = max(r.a, x0), min(r.b, x1)
        if b > a:
            by_y[round(r.pos * 2) / 2.0].append((a, b))
    ys = []
    for y, spans in by_y.items():
        spans.sort()
        cover, cur_a, cur_b = 0.0, spans[0][0], spans[0][1]
        for a, b in spans[1:]:
            if a <= cur_b + 0.5:
                cur_b = max(cur_b, b)
            else:
                cover += cur_b - cur_a
                cur_a, cur_b = a, b
        cover += cur_b - cur_a
        if cover >= min_span:
            ys.append(y)
    return sorted(ys)


def _text_row(page: PageModel, lm: LabelMatch, skip: set) -> List[Line]:
    return [ln for ln in page.lines if ln.id not in skip and abs(ln.cy - (lm.bbox[1] + lm.bbox[3]) / 2.0) <= 4.5]


# --------------------------------------------------------------------------- #
# Row fields (value to the right of, or below, a label)
# --------------------------------------------------------------------------- #
def parse_row_fields(page: PageModel, lay: Dict[str, Any], defaults: Dict[str, Any], res: PageResult) -> None:
    """``row_band`` and ``below`` fields of a layout (labels in a left column)."""
    specs = [f for f in lay.get("fields", [])]
    if not specs:
        return
    x_max = defaults["x_max"]
    label_region = (0.0, 0.0, defaults["label_x_max"], page.height)
    matches: Dict[str, LabelMatch] = {}
    # exact first, then fuzzy for what is still missing, so fuzzy never steals an exact label
    for fuzzy in (False, True):
        for spec in specs:
            if spec["key"] in matches:
                continue
            rule = spec.get("rule", "row_band")
            region = label_region if rule == "row_band" else spec.get("label_region")
            hits = find_labels(page, spec["labels"], mode="line", region=region, fuzzy=fuzzy,
                               fuzzy_min=defaults["fuzzy_min"], by="start" if rule == "row_band" else "centre")
            if hits:
                matches[spec["key"]] = hits[0]
                claim(page, hits[0])
    placed = sorted(matches.values(), key=lambda m: m.bbox[1])
    for spec in specs:
        lm = matches.get(spec["key"])
        rule = spec.get("rule", "row_band")
        if lm is None:
            if spec.get("expected", True):
                res.unmatched.append(Unmatched(page.number, lay["id"], "missing_label", " ".join(_variant_parts(spec["labels"][0])), None, "template label not found on the page"))
            continue
        if spec.get("ignore"):
            continue
        method, conf = "", CONF_EXACT
        if rule == "below":
            region, method, conf = _below_region(page, lm, placed, x_max)
        else:
            region, method, conf = _band_region(page, lm, placed, x_max)
        text, vbb = render(glyphs_in(page, region))
        if lm.how == "fuzzy":
            conf, method = CONF_FALLBACK, "fuzzy_label+" + method
        if spec.get("expected", True):
            res.found += 1
        res.fields.append(FieldRec(page.number, lay["id"], lm.printed, spec["key"], text, vbb, lm.bbox, conf, method))


def _band_region(page: PageModel, lm: LabelMatch, placed: List[LabelMatch], x_max: float) -> Tuple[Tuple[float, float, float, float], str, float]:
    cy = (lm.bbox[1] + lm.bbox[3]) / 2.0
    cands = [v for v in page.v if lm.bbox[2] - 3 <= v.pos <= lm.bbox[2] + 40 and v.a - 0.5 <= cy <= v.b + 0.5]
    if cands:
        v = min(cands, key=lambda r: (abs(r.pos - lm.bbox[2]), r.b - r.a))
        return (v.pos + 0.5, v.a, x_max, v.b), "label+row_band_rule", CONF_EXACT
    idx = placed.index(lm) if lm in placed else -1
    prev = placed[idx - 1] if idx > 0 else None
    nxt = placed[idx + 1] if 0 <= idx < len(placed) - 1 else None
    top = (prev.bbox[3] + lm.bbox[1]) / 2.0 if prev else lm.bbox[1] - 6.0
    bottom = (lm.bbox[3] + nxt.bbox[1]) / 2.0 if nxt else lm.bbox[3] + 40.0
    return (lm.bbox[2] + 3.0, top, x_max, bottom), "label+next_label_region", CONF_HEURISTIC


def _below_region(page: PageModel, lm: LabelMatch, placed: List[LabelMatch], x_max: float) -> Tuple[Tuple[float, float, float, float], str, float]:
    # next label below in an overlapping column, or the next horizontal rule below the label
    below = [m for m in placed if m is not lm and m.bbox[1] >= lm.bbox[3] - 1 and min(m.bbox[2], lm.bbox[2] + 150) > max(m.bbox[0], lm.bbox[0] - 5)]
    rules = [r.pos for r in page.h if r.pos > lm.bbox[3] + 2 and r.b > lm.bbox[0] and r.a < lm.bbox[2] + 150]
    lower = min([m.bbox[1] for m in below] + [min(rules) if rules else 10 ** 6])
    side = [m for m in placed if m is not lm and abs(m.bbox[1] - lm.bbox[1]) < 6 and m.bbox[0] > lm.bbox[2]]
    right = min([m.bbox[0] for m in side] + [x_max]) if side else x_max
    top = lm.bbox[3] + 0.5
    bottom = min(lower, page.height) - 0.5
    method = "label+below_region_rule" if rules and (not below or min(rules) <= min(m.bbox[1] for m in below)) else "label+below_next_label"
    conf = CONF_EXACT if method.endswith("rule") else CONF_HEURISTIC
    return (lm.bbox[0] - 2.0, top, right - 1.0, bottom), method, conf


# --------------------------------------------------------------------------- #
# Sections: inline label/value pairs and flag grids
# --------------------------------------------------------------------------- #
def section_band(page: PageModel, lm: LabelMatch, all_sections: List[LabelMatch], defaults: Dict[str, Any]) -> Tuple[float, float, str, float]:
    """Vertical extent of a section: between the solid full-width rules around its label."""
    cy = (lm.bbox[1] + lm.bbox[3]) / 2.0
    ys = full_width_rules(page, defaults["label_x_max"], defaults["x_max"], defaults["section_rule_min_span"])
    above = [y for y in ys if y <= cy - 1]
    below = [y for y in ys if y > cy + 1]
    if above and below:
        return max(above), min(below), "section_rule", CONF_EXACT
    order = sorted(all_sections, key=lambda m: m.bbox[1])
    idx = order.index(lm)
    top = max(above) if above else lm.bbox[1] - 6.0
    if below:
        bottom = min(below)
    elif idx + 1 < len(order):
        bottom = order[idx + 1].bbox[1] - 6.0
    else:
        bottom = defaults["footer_y"]
    return top, bottom, "section_heuristic", CONF_HEURISTIC


def _underlined(page: PageModel, vbb: Tuple[float, float, float, float], row_y1: float) -> bool:
    for r in page.h:
        if abs(r.pos - row_y1) <= 3.0 and min(r.b, vbb[2]) - max(r.a, vbb[0]) >= 0.5 * max(vbb[2] - vbb[0], 1.0):
            return True
    return False


def parse_sections(page: PageModel, lay: Dict[str, Any], defaults: Dict[str, Any], res: PageResult) -> None:
    """Sections with inline ``label value`` pairs and ``grid`` flag blocks."""
    secs = lay.get("sections", [])
    if not secs:
        return
    label_region = (0.0, 0.0, defaults["label_x_max"], page.height)
    smatch: Dict[str, LabelMatch] = {}
    # longest (multi-line) section labels first so "M&E Service Points / Future Proofing"
    # claims its lines before the plain "M&E Service Points"
    ordered = sorted(secs, key=lambda s: -max(len(_variant_parts(v)) for v in s["labels"]))
    for fuzzy in (False, True):
        for sec in ordered:
            if sec["key"] in smatch:
                continue
            hits = find_labels(page, sec["labels"], mode="line", region=label_region, fuzzy=fuzzy, fuzzy_min=defaults["fuzzy_min"], by="start")
            if hits:
                smatch[sec["key"]] = hits[0]
                claim(page, hits[0])
    plain = [m for k, m in smatch.items()]
    for sec in secs:
        lm = smatch.get(sec["key"])
        if lm is None:
            if sec.get("expected", True):
                lab = sec["labels"][0]
                res.unmatched.append(Unmatched(page.number, lay["id"], "missing_label", lab if isinstance(lab, str) else " ".join(lab), None, "section label not found"))
            continue
        if sec.get("expected", True):
            res.found += 1
        top, bottom, how, base_conf = section_band(page, lm, plain, defaults)
        sec_conf = CONF_FALLBACK if lm.how == "fuzzy" else base_conf
        method_prefix = ("fuzzy_label+" if lm.how == "fuzzy" else "label+") + how
        cols = sec.get("cols", [[defaults["label_x_max"], defaults["x_max"]]])
        band = (top, bottom)
        # --- flag grids first, so that their header and value lines are claimed before inline values are collected
        for grid in sec.get("grids", []):
            col = cols[grid.get("col", 0)]
            region = (col[0] - 2.0, band[0], col[1], band[1])
            hits = find_labels(page, grid["labels"], mode="line", region=region, by="start")
            if not hits:
                if grid.get("expected", True):
                    lab = grid["labels"][0]
                    res.unmatched.append(Unmatched(page.number, lay["id"], "missing_label", lab if isinstance(lab, str) else " ".join(lab), None, "grid label not found in section %s" % sec["key"]))
                continue
            gm = hits[0]
            claim(page, gm)
            if grid.get("expected", True):
                res.found += 1
            _parse_grid(page, lay, sec, grid, gm, region, band, base_conf, method_prefix, res)
        # --- inline pairs: locate every label of the section, then give each unclaimed text line to its label
        located: List[Tuple[Dict[str, Any], LabelMatch, bool]] = []
        for spec in sec.get("fields", []):
            col = cols[spec.get("col", 0)]
            region = (col[0] - 2.0, band[0], col[1], band[1])
            hits = find_labels(page, spec["labels"], mode="prefix", region=region, by="start")
            fz = False
            if not hits:
                hits = find_labels(page, spec["labels"], mode="prefix", region=region, by="start", fuzzy=True, fuzzy_min=defaults["fuzzy_min"])
                fz = bool(hits)
            if not hits:
                if spec.get("expected", True):
                    res.unmatched.append(Unmatched(page.number, lay["id"], "missing_label", spec["labels"][0], None, "label not found in section %s" % sec["key"]))
                continue
            claim(page, hits[0])
            located.append((spec, hits[0], fz))
        assigned: Dict[int, List[Line]] = {id(hm): [] for _sp, hm, _fz in located}
        cands = sorted((ln for ln in page.lines if ln.id not in page.claimed and band[0] <= ln.cy <= band[1]), key=lambda ln: (ln.bbox[1], ln.bbox[0]))
        for ln in cands:
            ci = next((i for i, c in enumerate(cols) if c[0] - 2.0 <= ln.bbox[0] < c[1]), None)
            if ci is None:
                continue
            pool = [(spec, hm) for spec, hm, _fz in located if spec.get("col", 0) == ci]
            # a wrapped value continues the line above it (same left edge, one text line lower)
            owner = None
            for spec, hm in pool:
                prev = assigned[id(hm)]
                if prev and abs(ln.bbox[0] - prev[-1].bbox[0]) <= 3.0 and 0 < ln.bbox[1] - prev[-1].bbox[1] <= 10.5:
                    owner = hm
                    break
            if owner is None:
                near = [(abs(ln.cy - (hm.bbox[1] + hm.bbox[3]) / 2.0), hm) for _sp, hm in pool if ln.bbox[0] >= hm.bbox[2] - 0.5]
                near = [t for t in near if t[0] <= 30.0]
                if near:
                    owner = min(near, key=lambda t: t[0])[1]
            if owner is not None:
                assigned[id(owner)].append(ln)
        for spec, hm, fz in located:
            ln0 = hm.lines[0]
            sel: List[Tuple[Line, List[Glyph]]] = []
            rest = [ln0.glyphs[i] for i in range(ln0.cmap[hm.end_index - 1] + 1, len(ln0.glyphs))] if hm.end_index <= len(ln0.cmap) else []
            if any(not g.ws for g in rest):
                sel.append((ln0, rest))
            for ln in assigned[id(hm)]:
                sel.append((ln, list(ln.glyphs)))
                page.claimed.add(ln.id)
            text, vbb = render(sel)
            conf = min(base_conf, sec_conf)
            method = method_prefix + "+inline"
            if vbb is not None:
                ul = _underlined(page, vbb, vbb[3])
            else:
                ul = any(abs(r.pos - hm.bbox[3]) <= 3.5 and r.b > hm.bbox[2] for r in page.h)
                method += "+empty"
            if ul:
                method += "+underline"
            else:
                conf = min(conf, CONF_HEURISTIC)
                method += "+no_underline"
            if len(assigned[id(hm)]) > 1:
                method += "+wrapped"
            if fz or hm.how == "fuzzy":
                conf, method = CONF_FALLBACK, "fuzzy_label+inline"
            if spec.get("expected", True):
                res.found += 1
            res.fields.append(FieldRec(page.number, lay["id"], hm.printed, "%s.%s" % (sec["key"], spec["key"]), text, vbb, hm.bbox, conf, method, section=sec["key"]))


def _parse_grid(page: PageModel, lay: Dict[str, Any], sec: Dict[str, Any], grid: Dict[str, Any], gm: LabelMatch,
                region: Sequence[float], band: Tuple[float, float], base_conf: float, method_prefix: str, res: PageResult) -> None:
    """Header row of flag names to the right of ``gm`` with one value below each."""
    heads: List[Tuple[str, Line]] = []
    row_y: Optional[float] = None
    prev_x = gm.bbox[2] - 1.0
    for name in grid["headers"]:
        comp = compact(name)
        cands = [ln for ln in page.lines if ln.compact == comp and ln.bbox[0] >= prev_x and region[0] <= ln.bbox[0] <= region[2]
                 and gm.bbox[1] - 14 <= ln.bbox[1] <= gm.bbox[1] + 22 and band[0] <= ln.cy <= band[1] and ln.id not in page.claimed]
        if row_y is not None:
            cands = [c for c in cands if abs(c.bbox[1] - row_y) <= 6.0]
        if not cands:
            heads = []
            break
        pick = min(cands, key=lambda c: (c.bbox[0], c.bbox[1]))
        if row_y is None:
            row_y = pick.bbox[1]
        heads.append((name, pick))
        prev_x = pick.bbox[2]
    if not heads:
        res.unmatched.append(Unmatched(page.number, lay["id"], "missing_label", " ".join(gm.printed.split()), gm.bbox, "flag headers not found beside grid label"))
        return
    head_ids = {h.id for _n, h in heads}
    for name, hl in heads:
        hcx = (hl.bbox[0] + hl.bbox[2]) / 2.0
        # the value sits below its header, centred on it, in body size (headers are 5-6 pt, values 8 pt)
        vals = [ln for ln in page.lines if ln.size >= 7.5 and ln.bbox[1] > hl.bbox[3] - 3 and ln.cy - hl.cy <= 40.0
                and abs((ln.bbox[0] + ln.bbox[2]) / 2.0 - hcx) <= 12.0 and ln.id != hl.id and ln.id not in page.claimed
                and ln.id not in head_ids]
        page.claimed.add(hl.id)
        key = "%s.%s.%s" % (sec["key"], grid["key"], snake(name))
        label = "%s / %s" % (" ".join(gm.printed.split()), hl.text)
        if vals:
            v = min(vals, key=lambda ln: ln.cy)
            page.claimed.add(v.id)
            res.fields.append(FieldRec(page.number, lay["id"], label, key, v.text, v.bbox, hl.bbox, base_conf, method_prefix + "+flag_grid", section=sec["key"]))
        else:
            res.fields.append(FieldRec(page.number, lay["id"], label, key, "", None, hl.bbox, CONF_HEURISTIC, method_prefix + "+flag_grid+no_value", section=sec["key"]))


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #
def _merge(values: Sequence[float], tol: float) -> List[float]:
    out: List[float] = []
    for v in sorted(values):
        if out and v - out[-1] <= tol:
            continue
        out.append(v)
    return out


def _table_rows(page: PageModel, pane: Tuple[float, float], y0: float, y1: float) -> Tuple[List[float], Optional[Tuple[float, float]]]:
    """Row boundaries (y of qualifying horizontal rules) of one pane in ``y0..y1``."""
    segs = [r for r in page.h if y0 - 1 <= r.pos <= y1 and r.b > pane[0] and r.a < pane[1] and not r.dashed]
    if not segs:
        return [], None
    xmin = max(min(r.a for r in segs), pane[0])
    xmax = min(max(r.b for r in segs), pane[1])
    extent = xmax - xmin
    by_y: Dict[float, List[Tuple[float, float]]] = defaultdict(list)
    for r in segs:
        by_y[round(r.pos * 2) / 2.0].append((max(r.a, pane[0]), min(r.b, pane[1])))
    good = []
    for y, spans in by_y.items():
        spans.sort()
        cover, ca, cb = 0.0, spans[0][0], spans[0][1]
        for a, b in spans[1:]:
            if a <= cb + 0.8:
                cb = max(cb, b)
            else:
                cover += cb - ca
                ca, cb = a, b
        cover += cb - ca
        if extent > 20 and cover >= 0.6 * extent:
            good.append(y)
    return _merge(good, 1.5), (xmin, xmax)


def table_title_ys(page: PageModel, layouts: Dict[str, Any], family: Dict[str, Any]) -> List[float]:
    """Top edge of every table title printed on the page, whichever template owns the table."""
    ys: List[float] = []
    for lid in family["layouts"]:
        for tbl in layouts["_by_id"][lid].get("tables", []):
            for hm in find_labels(page, tbl["title"], mode="line", region=(0.0, 0.0, 70.0, page.height), by="start"):
                if hm.lines[0].bold:
                    ys.append(hm.bbox[1])
    return sorted(set(round(y, 1) for y in ys))


def parse_tables(page: PageModel, lay: Dict[str, Any], layouts: Dict[str, Any], defaults: Dict[str, Any], res: PageResult,
                 row_counters: Dict[str, int], title_ys: Optional[List[float]] = None) -> None:
    """Ruled tables whose title is printed above them."""
    tables = lay.get("tables", [])
    if not tables:
        return
    found_titles: List[Tuple[Dict[str, Any], LabelMatch]] = []
    for fuzzy in (False, True):
        for tbl in tables:
            if any(t["key"] == tbl["key"] for t, _m in found_titles):
                continue
            hits = find_labels(page, tbl["title"], mode="line", region=(0.0, 0.0, 70.0, page.height), by="start", fuzzy=fuzzy, fuzzy_min=defaults["fuzzy_min"])
            hits = [h for h in hits if h.lines[0].bold or fuzzy]
            if hits:
                found_titles.append((tbl, hits[0]))
                claim(page, hits[0])
    found_titles.sort(key=lambda tm: tm[1].bbox[1])
    for i, (tbl, lm) in enumerate(found_titles):
        res.tables_seen.append(tbl["key"])
        y0 = lm.bbox[3]
        later = [y for y in (title_ys or []) if y > lm.bbox[1] + 2.0]
        y1 = (min(later) - 1.0) if later else defaults["footer_y"]
        title_conf = CONF_FALLBACK if lm.how == "fuzzy" else CONF_EXACT
        cols_spec = tbl.get("columns")
        if cols_spec is None:
            cols_spec = layouts["column_sets"][tbl["columns_ref"]]
        panes = [tuple(p) for p in tbl.get("panes", [[0.0, defaults["x_max"] + 10.0]])]
        # every line between title and next title belongs to this table region
        for ln in page.lines:
            if y0 - 1 <= ln.bbox[1] <= y1:
                page.claimed.add(ln.id)
        parsed_any = False
        for pane in panes:
            ys, extent = _table_rows(page, pane, y0, y1)
            if len(ys) < 2 or extent is None:
                continue
            parsed_any = True
            header_band = (ys[0], ys[1])
            hcy = (header_band[0] + header_band[1]) / 2.0
            vx = [v.pos for v in page.v if v.a - 0.5 <= hcy <= v.b + 0.5 and extent[0] - 1.5 <= v.pos <= extent[1] + 1.5]
            xs = _merge(vx, 1.5)
            if len(xs) < 2:
                res.unmatched.append(Unmatched(page.number, lay["id"], "unparsed_table", tbl["title"][0], lm.bbox, "no column rules in header row"))
                continue
            columns: List[Dict[str, Any]] = []
            for cx0, cx1 in zip(xs[:-1], xs[1:]):
                text, hbb = render(glyphs_in(page, (cx0, header_band[0], cx1, header_band[1])))
                columns.append({"x0": cx0, "x1": cx1, "header": text, "hbb": hbb, "key": None})
            exp_variants = {}
            for c in cols_spec:
                exp_variants[compact(c[0])] = (c[0], snake(c[0]))
            exact_cols = 0
            for col in columns:
                hit = exp_variants.get(compact(col["header"]))
                if hit:
                    col["key"], col["conf"] = hit[1], title_conf
                    exact_cols += 1
                elif col["header"]:
                    col["key"], col["conf"] = snake(col["header"]), CONF_HEURISTIC
                    res.unmatched.append(Unmatched(page.number, lay["id"], "unmatched_label", col["header"], col["hbb"], "table column header not in template (%s); values kept at 0.7" % tbl["key"]))
                else:
                    col["key"], col["conf"] = None, 0.0
            rk = tbl.get("row_key_col")
            bands = [(a, b, False) for a, b in zip(ys[1:-1], ys[2:])]
            # a table that runs off the foot of the page has no closing rule: its last row is bounded by the page body
            tail = glyphs_in(page, (extent[0], ys[-1] + 0.5, extent[1], y1))
            if tail and any(not g.ws for _l, gl in tail for g in gl):
                bands.append((ys[-1], y1, True))
            first_band = True
            for ry0, ry1, open_row in bands:
                cells = []
                for col in columns:
                    text, bb = render(glyphs_in(page, (col["x0"], ry0, col["x1"], ry1)))
                    cells.append((col, text, bb))
                if not any(t for _c, t, _b in cells):
                    continue
                counter_key = tbl["key"]
                # a row cut by a page break continues on the next page as a fragment with most cells empty
                filled = sum(1 for _c, t, _b in cells if t)
                continued = bool(first_band and row_counters.get("~open:" + counter_key) and filled <= 1 and tbl.get("row_key_col") is None)
                first_band = False
                if continued:
                    row_no = row_counters[counter_key]
                else:
                    row_counters[counter_key] = row_counters.get(counter_key, 0) + 1
                    row_no = row_counters[counter_key]
                row_counters["~open:" + counter_key] = 1 if open_row else 0
                if rk is not None:
                    rcol, rtext, rbb = cells[rk]
                    rowkey = snake(rtext) or "row%d" % row_no
                    others = [c for i, c in enumerate(cells) if i != rk and c[0]["key"]]
                    for col, text, bb in others:
                        if not text:
                            continue
                        key = "%s.%s" % (tbl["key"], rowkey) if len(others) == 1 else "%s.%s.%s" % (tbl["key"], rowkey, col["key"])
                        res.fields.append(FieldRec(page.number, lay["id"], rtext, key, text, bb, rbb, min(col["conf"], title_conf, CONF_HEURISTIC if open_row else 1.0),
                                                   "table_grid_rules+row_key" + ("+open_last_row" if open_row else ""), section=tbl["key"], row=str(row_no)))
                else:
                    for col, text, bb in cells:
                        if not col["key"] or not text:
                            continue
                        res.fields.append(FieldRec(page.number, lay["id"], col["header"], "%s.%s" % (tbl["key"], col["key"]), text, bb, col["hbb"],
                                                   min(col["conf"], title_conf, CONF_HEURISTIC if open_row else 1.0),
                                                   "table_grid_rules" + ("+open_last_row" if open_row else "") + ("+continues_previous_page" if continued else ""), section=tbl["key"], row=str(row_no)))
        if parsed_any:
            if tbl.get("expected", False):
                res.found += 1
        elif tbl.get("expected", False):
            res.unmatched.append(Unmatched(page.number, lay["id"], "unparsed_table", tbl["title"][0], lm.bbox, "table found by title but no ruled grid; not guessed"))
    for tbl in tables:
        if tbl.get("expected", False) and tbl["key"] not in res.tables_seen:
            res.unmatched.append(Unmatched(page.number, lay["id"], "missing_label", tbl["title"][0], None, "table title not found on the page"))


# --------------------------------------------------------------------------- #
# Page and file parsing
# --------------------------------------------------------------------------- #
def expected_count(lay: Dict[str, Any]) -> int:
    """Number of template items flagged ``expected`` for a layout."""
    total = 0
    total += sum(1 for f in lay.get("fields", []) if f.get("expected", True) and not f.get("ignore"))
    for sec in lay.get("sections", []):
        total += 1 if sec.get("expected", True) else 0
        total += sum(1 for f in sec.get("fields", []) if f.get("expected", True))
        total += sum(1 for g in sec.get("grids", []) if g.get("expected", True))
    total += sum(1 for t in lay.get("tables", []) if t.get("expected", False))
    return total


def parse_page(page: PageModel, layouts: Dict[str, Any], row_counters: Dict[str, int], family: Optional[Dict[str, Any]] = None,
               header: Optional[Dict[str, Any]] = None, selected: Optional[List[Tuple[Dict[str, Any], float]]] = None) -> PageResult:
    """Parse one page against every template that matches it."""
    defaults = layouts["defaults"]
    family = family or pick_family(page, layouts)
    header = header if header is not None else read_header(page, family)
    selected = selected if selected is not None else select_layouts(page, layouts, family)
    if not selected:
        body = [ln for ln in page.lines if 66 < ln.bbox[1] < defaults["footer_y"]]
        if not body:  # header and footer only: a continuation page left empty by the exporter
            return PageResult(page.number, "BLANK", header)
        res = PageResult(page.number, "UNKNOWN", header)
        bold = [ln for ln in page.lines if ln.bold and ln.bbox[0] < 150 and ln.bbox[1] > 60]
        for ln in bold[:60]:
            res.unmatched.append(Unmatched(page.number, "UNKNOWN", "unknown_page", ln.text, ln.bbox, "page matches no template; labels listed, no values read"))
        if not bold:
            res.unmatched.append(Unmatched(page.number, "UNKNOWN", "unknown_page", "", None, "page matches no template and has no bold labels"))
        return res
    title_ys = table_title_ys(page, layouts, family)
    res = PageResult(page.number, selected[0][0]["id"], header, score=selected[0][1])
    res.layout_ids = [lay["id"] for lay, _s in selected]
    for lay, _score in selected:
        res.starts_room = res.starts_room or bool(lay.get("starts_room"))
        res.expected += expected_count(lay)
        parse_row_fields(page, lay, defaults, res)
        parse_sections(page, lay, defaults, res)
        parse_tables(page, lay, layouts, defaults, res, row_counters, title_ys)
        if lay.get("starts_room"):
            for spec in family.get("footer", {}).get("fields", []):
                reg = spec["region"]
                hits = find_labels(page, spec["labels"], mode="prefix", region=(reg[0] - 2, reg[1], reg[2] + 2, reg[3]), by="start")
                if hits:
                    hm = hits[0]
                    ln = hm.lines[0]
                    rest = [ln.glyphs[i] for i in range(ln.cmap[hm.end_index - 1] + 1, len(ln.glyphs))]
                    text, vbb = render([(ln, rest)])
                    res.fields.append(FieldRec(page.number, lay["id"], hm.printed, spec["key"], text, vbb, hm.bbox, CONF_HEURISTIC, "label+footer_region"))
    # printed labels no template placed: reported, never interpreted
    any_rows = any(lay.get("fields") or lay.get("sections") for lay, _s in selected)
    any_tables = any(lay.get("tables") for lay, _s in selected)
    seen: set = set()
    if any_rows:
        for ln in page.lines:
            if ln.bold and ln.id not in page.claimed and ln.bbox[2] <= defaults["label_x_max"] and 66 < ln.bbox[1] < defaults["footer_y"]:
                res.unmatched.append(Unmatched(page.number, res.layout_id, "unmatched_label", ln.text, ln.bbox, "printed label not in the template; value not read"))
                seen.add(ln.id)
    if any_tables:
        for ln in page.lines:
            if ln.bold and ln.id not in page.claimed and ln.bbox[0] < 70 and 66 < ln.bbox[1] < defaults["footer_y"] and ln.id not in seen:
                res.unmatched.append(Unmatched(page.number, res.layout_id, "unmatched_label", ln.text, ln.bbox, "printed title not in the template; table not read"))
    return res


@dataclass
class Room:
    """One room data sheet inside a PDF (a PDF can hold several)."""

    index: int
    code: str = ""
    name: str = ""
    tag: str = ""
    pages: List[int] = field(default_factory=list)
    layout_ids: List[str] = field(default_factory=list)
    fields: List[FieldRec] = field(default_factory=list)
    unmatched: List[Unmatched] = field(default_factory=list)
    expected: int = 0
    found: int = 0
    has_summary: bool = False
    name_bbox: Optional[Tuple[float, float, float, float]] = None
    code_bbox: Optional[Tuple[float, float, float, float]] = None
    tag_bbox: Optional[Tuple[float, float, float, float]] = None


@dataclass
class FileResult:
    """Parse outcome of one PDF."""

    rel_path: str
    dept_no: str
    sha256: str
    rooms: List[Room]
    pages: List[PageResult]
    annotations: List[Dict[str, Any]]
    layout_by_page: Dict[int, str]
    status: str = "ok"
    note: str = ""
    page_count: int = 0


def parse_pdf(path: Path | str, layouts: Dict[str, Any], rel_path: str = "", dept_no: str = "", sha256: str = "") -> FileResult:
    """Parse every page of one PDF and group the pages into rooms."""
    path = Path(path)
    rooms: List[Room] = []
    pages: List[PageResult] = []
    annots: List[Dict[str, Any]] = []
    layout_by_page: Dict[int, str] = {}
    row_counters: Dict[str, int] = {}
    current: Optional[Room] = None
    a_layout = next((lay for lay in layouts["layouts"] if lay.get("starts_room")), None)
    doc, marks = open_clean(path)
    with doc:
        page_count = doc.page_count
        for pno in range(doc.page_count):
            pm = build_page(doc[pno], layouts, marks.get(pno))
            fam = pick_family(pm, layouts)
            hdr = read_header(pm, fam)
            code_hint = hdr.get("room_code", ("", None))[0]
            selected = select_layouts(pm, layouts, fam)
            starts = any(lay.get("starts_room") for lay, _s in selected)
            new_room = starts or current is None or code_hint != current.code
            if new_room:
                row_counters = {}
            pr = parse_page(pm, layouts, row_counters, fam, hdr, selected)
            if new_room:
                current = Room(index=len(rooms) + 1)
                current.code = code_hint
                current.name, current.name_bbox = hdr.get("room_name", ("", None))
                current.tag, current.tag_bbox = hdr.get("header_tag", ("", None))
                current.code_bbox = hdr.get("room_code", ("", None))[1]
                if not starts and a_layout is not None:
                    current.expected += expected_count(a_layout)
                    current.unmatched.append(Unmatched(pm.number, "UNKNOWN", "missing_label", "room summary page", None, "no summary page precedes this page of room %s" % code_hint, current.index, current.code))
                rooms.append(current)
            assert current is not None
            pages.append(pr)
            layout_by_page[pm.number] = pr.layout_id
            current.pages.append(pm.number)
            current.layout_ids.extend(pr.layout_ids or [pr.layout_id])
            current.expected += pr.expected
            current.found += pr.found
            if pr.starts_room:
                current.has_summary = True
                for key, (text, bb) in pr.header.items():
                    label = next((f["label_text"] for f in fam["header"]["fields"] if f["key"] == key), key)
                    current.fields.append(FieldRec(pm.number, pr.layout_id, label, key, text, bb, None, CONF_HEURISTIC, "header_region", room_index=current.index, room_code=current.code))
            for f in pr.fields:
                f.room_index, f.room_code = current.index, current.code
                current.fields.append(f)
            for u in pr.unmatched:
                u.room_index, u.room_code = current.index, current.code
                current.unmatched.append(u)
            for an in pm.annots:
                overlap_key, best = "", 0.0
                ax0, ay0, ax1, ay1 = an["rect"]
                for f in pr.fields:
                    if not f.value_bbox:
                        continue
                    ix = min(ax1, f.value_bbox[2]) - max(ax0, f.value_bbox[0])
                    iy = min(ay1, f.value_bbox[3]) - max(ay0, f.value_bbox[1])
                    if ix > 0 and iy > 0 and ix * iy > best:
                        best, overlap_key = ix * iy, f.key
                annots.append({**an, "page": pm.number, "room_code": current.code, "overlaps_field": overlap_key})
    return FileResult(rel_path, dept_no, sha256, rooms, pages, annots, layout_by_page, page_count=page_count)


# --------------------------------------------------------------------------- #
# Summary helpers
# --------------------------------------------------------------------------- #
_NUM = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


def _first_number(text: str) -> str:
    m = _NUM.search(text or "")
    return m.group(0).replace(",", ".") if m else ""


def _room_field(room: Room, key: str) -> Optional[FieldRec]:
    for f in room.fields:
        if f.key == key:
            return f
    return None


def _val(room: Room, key: str) -> str:
    f = _room_field(room, key)
    return f.value if f else ""


def _count_rows(room: Room, table: str, placeholders: Sequence[str]) -> int:
    rows: Dict[str, Dict[str, str]] = defaultdict(dict)
    for f in room.fields:
        if f.section == table and f.row:
            rows[f.row][f.key.split(".", 1)[1]] = f.value
    n = 0
    pats = [re.compile(p, re.IGNORECASE) for p in placeholders]
    for cells in rows.values():
        code = cells.get("code", "")
        desc = cells.get("description", "")
        if any(p.match(code.strip()) for p in pats) or re.match(r"^(no|nil)\b", desc.strip(), re.IGNORECASE):
            continue
        n += 1
    return n


_QTY_TABLES = ("engineering_services", "it", "security_systems", "medical_equipment", "standard_component_groups", "ffe_items")


def quantity_check(room: Room) -> Tuple[int, int]:
    """Cross-check of the table parse: ``(codes that reconcile, codes in the quantity summary)``.

    The "Quantity Summary - Engineering Services in Room" page must equal the quantities of
    the same code summed over every component table of the sheet; a dropped row, a wrong
    column or a mis-split pane shows up as a mismatch.
    """
    def table_rows(table: str) -> Dict[str, Dict[str, str]]:
        rows: Dict[str, Dict[str, str]] = defaultdict(dict)
        for f in room.fields:
            if f.section == table and f.row:
                rows[f.row][f.key.split(".", 1)[1]] = f.value
        return rows

    def qty(text: str) -> int:
        digits = re.sub(r"\D", "", text or "")
        return int(digits) if digits else 0

    listed: Dict[str, int] = defaultdict(int)
    for table in _QTY_TABLES:
        for cells in table_rows(table).values():
            code = cells.get("code", "").strip()
            if code:
                listed[code] += qty(cells.get("qty", ""))
    summary: Dict[str, int] = defaultdict(int)
    for cells in table_rows("quantity_summary").values():
        code = cells.get("code", "").strip()
        if code:
            summary[code] += qty(cells.get("total_room_qty", ""))
    return sum(1 for code, n in summary.items() if listed.get(code) == n), len(summary)


_OCC = {
    "staff": re.compile(r"(\d+)\s*(?:x\s*)?staff|staff\s*[:=]?\s*(\d+)", re.IGNORECASE),
    "patients": re.compile(r"(\d+)\s*(?:x\s*)?patients?|patients?\s*[:=]?\s*(\d+)", re.IGNORECASE),
    "visitors": re.compile(r"(\d+)\s*(?:x\s*)?visitors?|visitors?\s*[:=]?\s*(\d+)", re.IGNORECASE),
}


def room_summary(room: Room, layouts: Dict[str, Any], manifest_dept: str = "") -> Dict[str, Any]:
    """Key fields of one room as a flat dictionary (used for both summary tables)."""
    pats = layouts.get("placeholder_codes", [])
    occ = _val(room, "occupancy")
    exhaust = _val(room, "acmv.exhaust_requirements")
    ach = ""
    for text in (exhaust, _val(room, "acmv.room_air_recirculation"), _val(room, "description_functional_performance_specification")):
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:ACH|air changes)", text or "", re.IGNORECASE)
        if m:
            ach = m.group(1)
            break
    acmv_parts = []
    for key, lab in (("acmv.acmv_hours_of_op", "ACMV hours"), ("acmv.room_air_recirculation", "Room air re-circulation"), ("acmv.mode_of_control", "Mode of control"),
                     ("acmv.exhaust_requirements", "Exhaust requirements"), ("acmv.hepa_filtered", "HEPA filtered")):
        v = _val(room, key)
        if v:
            acmv_parts.append("%s: %s" % (lab, v))
    tag = room.tag.strip()
    dept = tag if tag and tag.upper() != "RDS" else manifest_dept
    out = {
        "room_code": room.code, "room_name": room.name, "department": dept,
        "area_m2": _first_number(_val(room, "area")), "occupancy": occ,
        "hours": _val(room, "hours_of_operation"), "temperature": _val(room, "acmv.room_temperature"),
        "humidity": _val(room, "acmv.room_humidity"), "pressure": _val(room, "acmv.room_pressure"), "ach": ach,
        "acmv_notes": "; ".join(acmv_parts),
        "equipment_count": _count_rows(room, "ffe_items", pats) + _count_rows(room, "medical_equipment", pats) + _count_rows(room, "standard_component_groups", pats),
        "services_item_count": _count_rows(room, "engineering_services", pats) + _count_rows(room, "it", pats) + _count_rows(room, "security_systems", pats),
        "revision": _val(room, "revision"), "date": _val(room, "issue_date"), "print_date": _val(room, "print_date"),
        "fields_found": room.found, "fields_expected": room.expected,
        "coverage_pct": ("%.1f" % (100.0 * room.found / room.expected)) if room.expected else "",
        "unmatched_labels": "; ".join(sorted({u.label for u in room.unmatched if u.kind in ("unmatched_label", "unknown_page") and u.label})),
        "missing_labels": "; ".join(sorted({u.label for u in room.unmatched if u.kind in ("missing_label", "unparsed_table") and u.label})),
    }
    good, listed = quantity_check(room)
    out["qty_summary_check"] = "%d/%d" % (good, listed)
    for who, pat in _OCC.items():
        m = pat.search(occ or "")
        out["occupancy_" + who] = (m.group(1) or m.group(2)) if m else ""
    return out


# --------------------------------------------------------------------------- #
# Batch run
# --------------------------------------------------------------------------- #
def pilot_dept_numbers() -> List[str]:
    """Pilot department numbers from ``config/project.yaml`` (leading digits of each name)."""
    out = []
    for name in common.load_config().get("pilot", {}).get("departments", []):
        m = re.match(r"\s*(\d{2})\b", str(name))
        if m:
            out.append(m.group(1))
    return out


def select_rows(manifest_rows: List[Dict[str, str]], depts: Optional[Sequence[str]]) -> List[Dict[str, str]]:
    """RDS rows of the manifest that are pulled, in the requested departments (default: the pilot)."""
    wanted = [str(d).zfill(2) for d in depts] if depts else pilot_dept_numbers()
    rows = [r for r in manifest_rows if r.get("doc_class") == "RDS" and r.get("pulled") == "y" and r.get("dept_no") in wanted]
    rows.sort(key=lambda r: (r.get("dept_no", ""), r.get("rel_path", "")))
    return rows


def run(manifest: Path | str, depts: Optional[Sequence[str]] = None, layouts_path: Optional[Path | str] = None,
        out_dir: Optional[Path | str] = None, source_root: Optional[Path | str] = None) -> Dict[str, Any]:
    """Parse all selected RDS files and write the registers; return counts."""
    paths = common.resolve_paths()
    layouts = load_layouts(layouts_path)
    rows = select_rows(common.read_csv_dicts(manifest), depts)
    src_root = Path(source_root) if source_root else paths["source"]
    reg = Path(out_dir) if out_dir else paths["registers"]
    field_rows: List[Dict[str, Any]] = []
    unmatched_rows: List[Dict[str, Any]] = []
    annot_rows: List[Dict[str, Any]] = []
    summary_rows: List[Dict[str, Any]] = []
    room_rows: List[Dict[str, Any]] = []
    layout_files: Dict[str, List[str]] = defaultdict(list)
    results: List[FileResult] = []
    for row in rows:
        rel, dept = row["rel_path"], row.get("dept_no", "")
        path = src_root / rel
        base = {"rel_path": rel, "dept_no": dept}
        if not rel.lower().endswith(".pdf"):
            summary_rows.append({**base, "status": "skipped_not_pdf", "note": "classified RDS by file name but the file is not a PDF (%s)" % Path(rel).suffix})
            continue
        if not path.is_file():
            summary_rows.append({**base, "status": "error", "note": "file not found under %s" % src_root})
            continue
        try:
            fr = parse_pdf(path, layouts, rel, dept, row.get("sha256", ""))
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
            summary_rows.append({**base, "status": "error", "note": "%s: %s" % (type(exc).__name__, exc)})
            continue
        results.append(fr)
        fam_ids = sorted({lid for pr in fr.pages for lid in (pr.layout_ids or [pr.layout_id])})
        for lid in fam_ids:
            layout_files[lid].append(rel)
        for room in fr.rooms:
            for f in room.fields:
                field_rows.append({
                    "rel_path": rel, "dept_no": dept, "page": f.page, "layout_id": f.layout_id,
                    "field_label_as_printed": f.label, "field_key": f.key, "value_as_printed": f.value,
                    "value_bbox": fmt_bbox(f.value_bbox), "label_bbox": fmt_bbox(f.label_bbox),
                    "confidence": "%.1f" % f.confidence, "method": f.method, "room_code": room.code,
                    "section": f.section, "row": f.row, "sha256": fr.sha256,
                })
            for u in room.unmatched:
                unmatched_rows.append({"rel_path": rel, "dept_no": dept, "page": u.page, "layout_id": u.layout_id, "room_code": room.code,
                                       "kind": u.kind, "label_as_printed": u.label, "label_bbox": fmt_bbox(u.bbox), "reason": u.reason})
        for an in fr.annotations:
            annot_rows.append({"rel_path": rel, "dept_no": dept, "page": an["page"], "room_code": an["room_code"], "annot_type": an["type"],
                               "bbox": fmt_bbox(an["rect"]), "content": an["content"], "overlaps_field": an["overlaps_field"], "author": an["author"], "subject": an.get("subject", ""), "created": an.get("created", "")})
        dept_name = row.get("dept_name", "")
        per_room = [room_summary(r, layouts, dept_name) for r in fr.rooms]
        for rs, room in zip(per_room, fr.rooms):
            # a reviewer's typed correction over the printed issue date is kept apart from the printed value
            rs["date_annotation"] = " | ".join(a["content"] for a in fr.annotations if a["page"] in room.pages and a["overlaps_field"] == "issue_date" and a["content"])
        for i, rs in enumerate(per_room, 1):
            room_rows.append({"room_index": i, "rel_path": rel, "dept_no": dept, "layout_id": "DRF1", "annotation_count": sum(1 for a in fr.annotations if a["room_code"] == rs["room_code"]), **{k: v for k, v in rs.items()}})
        agg: Dict[str, Any] = {}
        for col in ("room_code", "room_name", "department", "area_m2", "occupancy", "occupancy_staff", "occupancy_patients", "occupancy_visitors",
                    "hours", "temperature", "humidity", "pressure", "ach", "acmv_notes", "revision", "date", "date_annotation", "print_date"):
            vals = [rs[col] for rs in per_room]
            agg[col] = vals[0] if len(set(vals)) == 1 else " | ".join(vals)
        total_expected = sum(r.expected for r in fr.rooms)
        total_found = sum(r.found for r in fr.rooms)
        summary_rows.append({
            **base, "status": "ok", "room_count": len(fr.rooms), **agg,
            "equipment_count": sum(rs["equipment_count"] for rs in per_room), "services_item_count": sum(rs["services_item_count"] for rs in per_room),
            "layout_id": "DRF1", "layout_pages": ",".join(sorted({lid for pr in fr.pages for lid in (pr.layout_ids or [pr.layout_id])})),
            "fields_found": total_found, "fields_expected": total_expected,
            "coverage_pct": ("%.1f" % (100.0 * total_found / total_expected)) if total_expected else "",
            "unmatched_labels": "; ".join(sorted({u.label for r in fr.rooms for u in r.unmatched if u.kind in ("unmatched_label", "unknown_page") and u.label})),
            "missing_labels": "; ".join(sorted({u.label for r in fr.rooms for u in r.unmatched if u.kind in ("missing_label", "unparsed_table") and u.label})),
            "qty_summary_check": "%d/%d" % (sum(int(rs["qty_summary_check"].split("/")[0]) for rs in per_room), sum(int(rs["qty_summary_check"].split("/")[1]) for rs in per_room)),
            "annotation_count": len(fr.annotations), "pages": fr.page_count,
            "note": "" if len(fr.rooms) == 1 else "%d rooms in one PDF; per-room values joined with ' | ' in room order (see rds_rooms.csv)" % len(fr.rooms),
        })
    layout_rows = []
    label_lists = {lay["id"]: lay for lay in layouts["layouts"]}
    for lid, files in sorted(layout_files.items()):
        lay = label_lists.get(lid)
        labels = _layout_labels(lay) if lay else []
        layout_rows.append({"layout_id": lid, "page_type": lay.get("page_type", "") if lay else "", "label_list": " | ".join(labels), "file_count": len(files), "files": " | ".join(sorted(files))})
    fam_files = sorted({fr.rel_path for fr in results})
    for fam in layouts["families"]:
        layout_rows.insert(0, {"layout_id": fam["id"], "page_type": "family", "label_list": " | ".join(fam["layouts"]), "file_count": len(fam_files), "files": " | ".join(fam_files)})
    common.write_csv_dicts(reg / "rds_fields.csv", field_rows, FIELD_COLUMNS)
    common.write_csv_dicts(reg / "rds_summary.csv", summary_rows, SUMMARY_COLUMNS)
    common.write_csv_dicts(reg / "rds_rooms.csv", room_rows, _room_cols())
    common.write_csv_dicts(reg / "rds_layouts.csv", layout_rows, ["layout_id", "page_type", "label_list", "file_count", "files"])
    common.write_csv_dicts(reg / "rds_unmatched.csv", unmatched_rows, UNMATCHED_COLUMNS)
    common.write_csv_dicts(reg / "rds_annotations.csv", annot_rows, ANNOT_COLUMNS)
    return {"files": len(rows), "parsed": len(results), "fields": len(field_rows), "rooms": len(room_rows), "unmatched": len(unmatched_rows),
            "annotations": len(annot_rows), "out_dir": str(reg)}


def _room_cols() -> List[str]:
    cols = ["room_index", "rel_path", "dept_no", "layout_id", "room_code", "room_name", "department", "area_m2", "occupancy", "occupancy_staff",
            "occupancy_patients", "occupancy_visitors", "hours", "temperature", "humidity", "pressure", "ach", "acmv_notes", "equipment_count",
            "services_item_count", "revision", "date", "date_annotation", "print_date", "fields_found", "fields_expected", "coverage_pct", "unmatched_labels",
            "missing_labels", "qty_summary_check", "annotation_count"]
    return cols


def _layout_labels(lay: Dict[str, Any]) -> List[str]:
    labels: List[str] = []

    def lab(v: Any) -> str:
        return v if isinstance(v, str) else " ".join(v)

    for f in lay.get("fields", []):
        labels.append(lab(f["labels"][0]))
    for sec in lay.get("sections", []):
        labels.append(lab(sec["labels"][0]))
        labels.extend(lab(f["labels"][0]) for f in sec.get("fields", []))
        labels.extend(lab(g["labels"][0]) for g in sec.get("grids", []))
    for t in lay.get("tables", []):
        labels.append(t["title"][0])
    seen, out = set(), []
    for x in labels:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """Argument parser for ``python3 -m src.rds_parser``."""
    p = argparse.ArgumentParser(description="Template-driven Room Data Sheet parser (text layer, no model).")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="parse every pulled RDS PDF of the selected departments")
    r.add_argument("--manifest", default=str(common.PATHS["registers"] / "manifest.csv"))
    r.add_argument("--dept", action="append", help="department number (repeatable); default: the pilot departments")
    r.add_argument("--layouts", default=None, help="layout template JSON (default src/rds_layouts.json)")
    r.add_argument("--out-dir", default=None, help="register directory (default paths.registers)")
    one = sub.add_parser("parse", help="parse one PDF and print its fields as JSON")
    one.add_argument("pdf")
    one.add_argument("--layouts", default=None)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point; returns a process exit code."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            summary = run(args.manifest, args.dept, args.layouts, args.out_dir)
            print(json.dumps(summary))
            return 0
        if args.command == "parse":
            layouts = load_layouts(args.layouts)
            fr = parse_pdf(args.pdf, layouts, rel_path=args.pdf)
            payload = [{"room": r.code, "name": r.name, "found": r.found, "expected": r.expected,
                        "fields": [{"key": f.key, "label": f.label, "value": f.value, "bbox": fmt_bbox(f.value_bbox), "conf": f.confidence, "method": f.method, "page": f.page} for f in r.fields],
                        "unmatched": [{"kind": u.kind, "label": u.label, "reason": u.reason, "page": u.page} for u in r.unmatched]} for r in fr.rooms]
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 0
    except Exception as exc:  # noqa: BLE001
        print("error: %s" % exc, file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
