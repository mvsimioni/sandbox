"""Markdown writer for the stage 1A conversion (reconstructed Appendix C layout).

Builds one Markdown file per source from MarkItDown's text and the sidecar
directory written by ``sidecar_pdf``, ``sidecar_pptx``, ``sidecar_xlsx`` or
``sidecar_misc``:

* YAML front matter with the keys of :data:`FRONT_MATTER_KEYS` (grade filled
  in by the caller after ``recall_check`` has run);
* ``# <file name>``, ``## Document`` (title block, workbook or deck metadata
  as a table), then one section per unit: ``## Page N`` for PDFs, ``## Slide
  N`` for PPTX, ``## Sheet <name>`` for workbooks (``## Message``, ``## Image``
  and ``## File`` for e-mail, photos and the rest).  Inside each unit, in this
  order: the MarkItDown text for that unit verbatim under ``<!-- method:
  markitdown -->`` (split on MarkItDown's own page, slide or sheet markers;
  otherwise the whole text sits under the first unit and the Provenance says
  so), ``### Native text (sidecar)`` with the words or cells the sidecar adds
  that MarkItDown lacks (drawings: every text block with its page-view box
  and rotation), ``### Tables`` with a caption naming the source, ``###
  Annotations and comments`` and, for decks, ``### Pictures``;
* ``## Comments register`` (every annotation or comment of the file with its
  unit) and ``## Provenance`` (sidecar files, counts, checks and results).

The MarkItDown text is kept verbatim except that control characters (NUL
and the like) and carriage returns are dropped, so the file stays a text
file.  Every structural heading the writer owns is preceded by an HTML comment
``<!-- section: name -->`` so that ``recall_check`` can count tables and
register rows without mistaking a heading inside the verbatim MarkItDown text
for one of the writer's.  Markdown is for reading; calculations read the
sidecars.  Nothing here touches the network.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

try:  # package import (python -m src.md_writer, tests)
    from src import recall_check, sidecar_misc, sidecar_pptx, sidecar_xlsx
    from src.common import (
        atomic_write_text,
        classify_document,
        parse_department,
        parse_stage,
        read_csv_dicts,
        read_jsonl,
        sha256_file,
        utc_now_iso,
    )
except ModuleNotFoundError:  # run as a plain script from inside src/
    import recall_check  # type: ignore[no-redef]
    import sidecar_misc  # type: ignore[no-redef]
    import sidecar_pptx  # type: ignore[no-redef]
    import sidecar_xlsx  # type: ignore[no-redef]
    from common import (  # type: ignore[no-redef]
        atomic_write_text,
        classify_document,
        parse_department,
        parse_stage,
        read_csv_dicts,
        read_jsonl,
        sha256_file,
        utc_now_iso,
    )

FRONT_MATTER_KEYS: Tuple[str, ...] = (
    "source_path", "drive_id", "sha256", "bytes", "drive_modified", "root", "dept_no", "dept_name", "stage",
    "doc_class", "pages_or_slides_or_sheets", "converters", "methods_used", "grade", "grade_reason",
    "generated_at", "pipeline_version",
)
"""Front matter keys in the order they are written."""

MAX_TABLE_ROWS = 1000
"""Rows of one sheet range shown in the Markdown; the full range stays in the values CSV."""

NATIVE_METHOD: Dict[str, str] = {"pdf": "pymupdf-text", "pptx": "python-pptx", "xlsx": "openpyxl",
                                 "image": "pillow-exif", "other": "none"}
UNIT_HEADINGS: Dict[str, str] = {"email": "## Message", "image": "## Image", "other": "## File"}
REGISTER_COLUMNS: Tuple[str, ...] = ("index", "unit", "type", "author", "date", "subject", "text", "reply_to",
                                     "location", "colour", "layer")
UNIT_ANNOTATION_COLUMNS: Tuple[str, ...] = tuple(c for c in REGISTER_COLUMNS if c != "unit")
BOX_COLUMNS: Tuple[str, ...] = ("text", "x0 (pt)", "y0 (pt)", "x1 (pt)", "y1 (pt)", "rotation (deg)")

_SLIDE_MARK_RE = re.compile(r"<!-- Slide number: (\d+) -->\n?")
_CELL_RE = re.compile(r"^([A-Z]+)(\d+)$")
_FORMAT_LITERAL_RE = re.compile(r'"([^"]*)"')
_UNIT_LIKE_RE = re.compile(r"^[A-Za-z%°µ][A-Za-z0-9%°²³µ/.\- ]{0,11}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")
"""C0 control characters (NUL included) that have no place in a text file; tab, newline and form feed are kept."""


def clean_text(text: str) -> str:
    """Drop control characters and carriage returns, and turn a surviving form feed into a line break."""
    return _CONTROL_RE.sub("", str(text)).replace("\r", "").replace("\x0c", "\n")


# --------------------------------------------------------------------------- #
# Inputs and outputs
# --------------------------------------------------------------------------- #
@dataclass
class Source:
    """The file being converted and its ledger facts."""

    path: Path
    rel_path: str
    sha256: str
    bytes: int
    kind: str
    drive_id: Optional[str] = None
    drive_modified: Optional[str] = None


@dataclass
class MarkItDownOutput:
    """What MarkItDown returned for the file (``text`` is ``None`` when it failed)."""

    text: Optional[str]
    version: str
    error: Optional[str] = None
    seconds: float = 0.0


@dataclass
class Unit:
    """One page, slide, sheet or message with everything the writer renders for it."""

    heading: str
    label: str
    markitdown: str
    native_method: str
    native_lines: List[str] = field(default_factory=list)
    tables: List[str] = field(default_factory=list)
    annotations: List[Dict[str, Any]] = field(default_factory=list)
    pictures: Optional[List[str]] = None
    added: int = 0


@dataclass
class Body:
    """The rendered body (title to Comments register) and what went into it."""

    text: str
    units: List[str]
    unit_count: int
    tables_written: int
    comment_rows: int
    sidecar_added: int
    notes: List[str]


# --------------------------------------------------------------------------- #
# Markdown primitives
# --------------------------------------------------------------------------- #
def md_cell(value: Any) -> str:
    """Render a value for a table cell: pipes escaped, line breaks flattened, ``None`` blank."""
    if value is None:
        return ""
    if isinstance(value, float):
        value = f"{value:.1f}" if not value.is_integer() else str(int(value))
    return clean_text(value).replace("|", "\\|").replace("\n", " ").strip()


def md_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    """A Markdown table from a header row and row sequences (short rows are padded)."""
    width = len(headers)
    lines = ["| " + " | ".join(md_cell(h) or " " for h in headers) + " |", "|" + "|".join(" --- " for _ in headers) + "|"]
    for row in rows:
        cells = [md_cell(v) for v in row][:width]
        cells += [""] * (width - len(cells))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def kv_table(pairs: Iterable[Tuple[str, Any]]) -> str:
    """A two-column ``field | value`` table."""
    return md_table(("field", "value"), [(k, v) for k, v in pairs])


def section_mark(name: str) -> str:
    """The HTML comment that marks one of the writer's own sections."""
    return f"<!-- section: {name} -->"


def method_mark(name: str) -> str:
    """The HTML comment that names the method behind the following text."""
    return f"<!-- method: {name} -->"


def _json(path: Path, default: Any) -> Any:
    """Parse a JSON file, returning ``default`` when it is missing or unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _fmt(value: Any, digits: int = 1) -> str:
    """Format a coordinate-like value with ``digits`` decimals (strings are converted when numeric)."""
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "" if value is None else str(value)


def _csv_grid(path: Path) -> List[List[str]]:
    """Rows of a header-less CSV file (table CSVs written by the sidecars)."""
    import csv

    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [list(row) for row in csv.reader(handle)]


def grid_table(grid: Sequence[Sequence[str]]) -> str:
    """A Markdown table whose header is the grid's first row."""
    if not grid:
        return "(empty table)"
    width = max(len(row) for row in grid)
    header = list(grid[0]) + [""] * (width - len(grid[0]))
    return md_table([h or " " for h in header], grid[1:])


# --------------------------------------------------------------------------- #
# Splitting MarkItDown's text into units
# --------------------------------------------------------------------------- #
def split_pdf_text(text: Optional[str], page_count: int) -> Tuple[Dict[int, str], Optional[str]]:
    """Split on the form feeds pdfminer emits per page; otherwise everything goes under Page 1."""
    if text is None:
        return {}, None
    parts = text.split("\x0c")
    if len(parts) > 1 and not parts[-1].strip():
        parts = parts[:-1]
    if page_count > 0 and len(parts) == page_count:
        return {number: part for number, part in enumerate(parts, start=1)}, None
    note = (f"MarkItDown returned one text without usable page markers ({len(parts)} segment(s) for "
            f"{page_count} page(s)); the whole text is under Page 1")
    return {1: text}, note


def split_pptx_text(text: Optional[str], slide_count: int) -> Tuple[Dict[int, str], Optional[str]]:
    """Split on MarkItDown's ``<!-- Slide number: N -->`` markers (consumed by the split)."""
    if text is None:
        return {}, None
    pieces = _SLIDE_MARK_RE.split(text)
    if len(pieces) == 1:
        return {1: text}, f"MarkItDown emitted no slide markers for {slide_count} slide(s); the whole text is under Slide 1"
    segments: Dict[int, str] = {}
    for position in range(1, len(pieces) - 1, 2):
        number = int(pieces[position])
        segments[number] = segments.get(number, "") + pieces[position + 1]
    note = None
    if pieces[0].strip():
        segments[1] = pieces[0] + segments.get(1, "")
        note = "text before MarkItDown's first slide marker is under Slide 1"
    return segments, note


def split_xlsx_text(text: Optional[str], sheet_names: Sequence[str]) -> Tuple[Dict[str, str], Optional[str]]:
    """Split on MarkItDown's ``## <sheet>`` headings (consumed by the split)."""
    if text is None:
        return {}, None
    names = set(sheet_names)
    segments: Dict[str, str] = {}
    current: Optional[str] = None
    preamble: List[str] = []
    for line in text.splitlines():
        candidate = line[3:].strip() if line.startswith("## ") else None
        if candidate is not None and candidate in names:
            current = candidate
            segments.setdefault(current, "")
            continue
        if current is None:
            preamble.append(line)
        else:
            segments[current] += line + "\n"
    if not segments:
        first = sheet_names[0] if sheet_names else ""
        return {first: text}, f"MarkItDown emitted no sheet headings for {len(sheet_names)} sheet(s); the whole text is under the first sheet"
    note = None
    if any(line.strip() for line in preamble) and sheet_names:
        segments[sheet_names[0]] = "\n".join(preamble) + "\n" + segments.get(sheet_names[0], "")
        note = "text before MarkItDown's first sheet heading is under the first sheet"
    return {name: value.strip("\n") for name, value in segments.items()}, note


# --------------------------------------------------------------------------- #
# Annotations and comments
# --------------------------------------------------------------------------- #
def _pdf_annotation_rows(sidecar_dir: Path) -> List[Dict[str, Any]]:
    """Register rows for every PDF annotation (``annotations.jsonl``)."""
    annots = [a for a in read_jsonl(sidecar_dir / "annotations.jsonl") if isinstance(a, dict)]
    index_by_xref = {a.get("xref"): i for i, a in enumerate(annots, start=1)}
    rows: List[Dict[str, Any]] = []
    for index, a in enumerate(annots, start=1):
        text = a.get("contents") or a.get("error") or ""
        popup = a.get("popup_contents")
        if popup and popup != text:
            text = f"{text} [popup: {popup}]".strip()
        colours = [f"{k} {a[key]}" for k, key in (("stroke", "stroke_colour"), ("fill", "fill_colour")) if a.get(key)]
        rect = a.get("rect_view") or []
        location = f"rect {', '.join(_fmt(v) for v in rect)} (page view)" if len(rect) == 4 else ""
        if a.get("vertices_view"):
            location += f"; {len(a['vertices_view'])} vertices"
        rows.append({
            "index": index, "unit": f"page {a.get('page')}", "unit_key": a.get("page"), "type": a.get("type", ""),
            "author": a.get("title", ""), "date": a.get("creation_date_iso") or a.get("modification_date_iso") or "",
            "subject": a.get("subject", ""), "text": text, "reply_to": index_by_xref.get(a.get("irt_xref"), ""),
            "location": location, "colour": "; ".join(colours), "layer": a.get("oc_name") or "",
        })
    return rows


def _pptx_annotation_rows(sidecar_dir: Path) -> List[Dict[str, Any]]:
    """Register rows for every deck comment and reply (``comments.jsonl``)."""
    comments = [c for c in read_jsonl(sidecar_dir / sidecar_pptx.COMMENTS_NAME) if isinstance(c, dict)]
    index_by_id = {(c.get("part"), c.get("comment_id")): c.get("index") for c in comments if c.get("reply_to") is None}
    rows: List[Dict[str, Any]] = []
    for c in comments:
        kind = c.get("kind", "")
        reply = c.get("reply_to") is not None
        slide = c.get("slide")
        anchor = f"shape {c['anchor_shape_id']}" if c.get("anchor_shape_id") is not None else "slide"
        position = f" at {_fmt(c['pos_x_pt'])}, {_fmt(c['pos_y_pt'])} pt" if c.get("pos_x_pt") is not None else ""
        rows.append({
            "index": c.get("index"), "unit": f"slide {slide}" if slide is not None else "unresolved", "unit_key": slide,
            "type": f"{kind} {'reply' if reply else 'comment'}", "author": c.get("author", ""), "date": c.get("date", ""),
            "subject": c.get("status") or "", "text": c.get("text", ""),
            "reply_to": index_by_id.get((c.get("part"), c.get("reply_to")), "") if reply else "",
            "location": anchor + position, "colour": "", "layer": "",
        })
    return rows


def _xlsx_annotation_rows(sidecar_dir: Path) -> List[Dict[str, Any]]:
    """Register rows for every legacy note and threaded comment (``comments.jsonl``)."""
    comments = [c for c in read_jsonl(sidecar_dir / sidecar_xlsx.COMMENTS_NAME) if isinstance(c, dict)]
    index_by_id = {c.get("comment_id"): c.get("index") for c in comments if c.get("comment_id")}
    rows: List[Dict[str, Any]] = []
    for c in comments:
        kind = c.get("kind", "")
        if kind == "legacy":
            kind_label = "legacy note" + (" (threaded shadow)" if c.get("threaded_shadow") else "")
        else:
            kind_label = "threaded reply" if c.get("parent_id") else "threaded comment"
        sheet = c.get("sheet")
        rows.append({
            "index": c.get("index"), "unit": f"sheet {sheet}" if sheet is not None else "unresolved", "unit_key": sheet,
            "type": kind_label, "author": c.get("author", ""), "date": c.get("date", ""),
            "subject": "done" if c.get("done") else "", "text": c.get("text", ""),
            "reply_to": index_by_id.get(c.get("parent_id"), "") if c.get("parent_id") else "",
            "location": f"cell {c.get('cell', '')}", "colour": "", "layer": "",
        })
    return rows


def annotation_rows(kind: str, sidecar_dir: Path) -> List[Dict[str, Any]]:
    """Every annotation or comment of the file as a register row, with its unit."""
    if kind == "pdf":
        return _pdf_annotation_rows(sidecar_dir)
    if kind == "pptx":
        return _pptx_annotation_rows(sidecar_dir)
    if kind == "xlsx":
        return _xlsx_annotation_rows(sidecar_dir)
    return []


def _rows_for_unit(rows: Sequence[Dict[str, Any]], unit_key: Any) -> List[Dict[str, Any]]:
    """The annotation rows that belong to one unit."""
    return [row for row in rows if row.get("unit_key") == unit_key]


# --------------------------------------------------------------------------- #
# Missing-word logic shared by the unit builders
# --------------------------------------------------------------------------- #
def missing_keys(sidecar_text: str, markitdown_keys: set) -> set:
    """Word keys of ``sidecar_text`` that are absent from the MarkItDown text.

    A number is present when the MarkItDown text holds it or an equivalent
    shorter print of it (``113.30000000000001`` as ``113.3``, see
    :func:`recall_check.find_match`), so float repr noise is not a gap.
    """
    return {key for key in recall_check.word_keys(sidecar_text)
            if recall_check.find_match(key, markitdown_keys) is None}


def word_missing(word: str, markitdown_keys: set) -> bool:
    """True when one word of the native layer (with a usable key) is absent from the MarkItDown text."""
    key = recall_check.word_key(word)
    return bool(key) and recall_check.find_match(key, markitdown_keys) is None


def markitdown_block(text: str, failed: Optional[str]) -> str:
    """The verbatim MarkItDown text of a unit, or a short note when it produced nothing."""
    if failed is not None:
        return f"(MarkItDown produced no text: {failed})"
    stripped = clean_text(text).strip("\n")
    return stripped if stripped.strip() else "(MarkItDown returned no text for this unit)"


# --------------------------------------------------------------------------- #
# PDF units
# --------------------------------------------------------------------------- #
def _block_rotations(words: Sequence[Dict[str, Any]]) -> Dict[Tuple[int, int], int]:
    """Majority ``rotation_view`` of the words in each ``(page, block)``."""
    votes: Dict[Tuple[int, int], Counter] = defaultdict(Counter)
    for w in words:
        votes[(int(w["page"]), int(w["block"]))][int(w.get("rotation_view", 0))] += 1
    return {key: counter.most_common(1)[0][0] for key, counter in votes.items()}


def _pdf_native_lines(page: Dict[str, Any], words: List[Dict[str, Any]], blocks: List[Dict[str, Any]],
                      rotations: Dict[Tuple[int, int], int], md_keys: set, title_block: Optional[Dict[str, Any]],
                      ) -> Tuple[List[str], int]:
    """Native-text lines for one PDF page and the number of words the sidecar supplies."""
    number = int(page["page"])
    if not words:
        return [f"No native text layer on this page ({page.get('image_count', 0)} image(s), coverage "
                f"{page.get('image_coverage_pct', 0)} %, vector paths {page.get('vector_path_count', 0)}; "
                "a vision pass is needed)."], 0
    missing = [w for w in words if word_missing(w["text"], md_keys)]
    lines: List[str] = []
    if page.get("is_drawing_page"):
        lines.append(f"Drawing page: every text block with its page-view box and rotation; "
                     f"{len(missing)} word(s) of the native layer are absent from the MarkItDown text.")
        lines.append("")
        lines.append(md_table(BOX_COLUMNS, [
            (b["text"], _fmt(b["x0"]), _fmt(b["y0"]), _fmt(b["x1"]), _fmt(b["y1"]),
             rotations.get((number, int(b["block"])), 0)) for b in blocks]))
        if title_block and title_block.get("parsed"):
            parsed = title_block["parsed"]
            lines.append("")
            lines.append("Title block (parsed): " + "; ".join(f"{k} {v}" for k, v in parsed.items() if v))
    elif missing:
        lines.append(f"{len(missing)} word(s) of the native layer are absent from the MarkItDown text:")
        lines.append("")
        lines.append(md_table(BOX_COLUMNS, [
            (w["text"], *(_fmt(v) for v in w["bbox_view"]), w.get("rotation_view", 0)) for w in missing]))
    else:
        lines.append("The MarkItDown text covers every word of the native layer on this page.")
    return lines, len(missing)


def _pdf_tables(number: int, tables: Sequence[Dict[str, Any]], sidecar_dir: Path) -> List[str]:
    """Rendered pdfplumber tables of one page, each with its caption."""
    rendered: List[str] = []
    for k, t in enumerate(tables, start=1):
        bbox = ", ".join(_fmt(v) for v in t.get("bbox", []))
        caption = (f"Table P{number}-{k}: pdfplumber, page {number}, {t.get('strategy', '?')} strategy, "
                   f"{t.get('rows')} rows x {t.get('cols')} columns, bbox {bbox} (page view); file {t.get('file')}.")
        rendered.append(caption + "\n\n" + grid_table(_csv_grid(sidecar_dir / str(t.get("file", "")))))
    return rendered


def _pdf_units(md: MarkItDownOutput, sidecar_dir: Path, rows: List[Dict[str, Any]], notes: List[str]) -> List[Unit]:
    """Units for a PDF: one per processed page."""
    info = _json(sidecar_dir / "page_info.json", {})
    pages = info.get("pages", [])
    words_by_page: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    all_words = [w for w in read_jsonl(sidecar_dir / "words.jsonl") if isinstance(w, dict)]
    for w in all_words:
        words_by_page[int(w["page"])].append(w)
    blocks_by_page: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for b in read_csv_dicts(sidecar_dir / "blocks.csv"):
        blocks_by_page[int(b["page"])].append(b)
    tables_by_page: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for t in _json(sidecar_dir / "tables.json", []):
        tables_by_page[int(t["page"])].append(t)
    title_blocks = {int(t["page"]): t for t in _json(sidecar_dir / "titleblock.json", []) if isinstance(t, dict)}
    rotations = _block_rotations(all_words)
    segments, note = split_pdf_text(md.text, len(pages))
    if note:
        notes.append(note)
    whole_keys = recall_check.word_keys(md.text or "") if note else None
    units: List[Unit] = []
    for page in pages:
        number = int(page["page"])
        segment = segments.get(number, "")
        md_keys = whole_keys if whole_keys is not None else recall_check.word_keys(segment)
        native, added = _pdf_native_lines(page, words_by_page[number], blocks_by_page[number], rotations, md_keys,
                                          title_blocks.get(number))
        text = segment if (number == 1 or not note) else "(see Page 1: MarkItDown returned one text for the whole document)"
        units.append(Unit(heading=f"## Page {number}", label=f"page {number}",
                          markitdown=markitdown_block(text, md.error), native_method=NATIVE_METHOD["pdf"],
                          native_lines=native, tables=_pdf_tables(number, tables_by_page[number], sidecar_dir),
                          annotations=_rows_for_unit(rows, number), added=added))
    return units


def _pdf_document(sidecar_dir: Path, manifest: Dict[str, Any]) -> str:
    """The ``## Document`` table of a PDF."""
    info = _json(sidecar_dir / "page_info.json", {})
    pages = info.get("pages", [])
    sizes = Counter(f"{p.get('width_pt')} x {p.get('height_pt')} pt" for p in pages)
    title_blocks = _json(sidecar_dir / "titleblock.json", [])
    parsed = (title_blocks[0].get("parsed") or {}) if title_blocks else {}
    counts = manifest.get("counts", {})
    pairs: List[Tuple[str, Any]] = [
        ("pages", info.get("page_count")), ("pages processed", info.get("pages_processed")),
        ("page sizes (page view)", "; ".join(f"{size} ({n})" for size, n in sizes.most_common())),
        ("drawing pages", counts.get("drawing_pages")), ("text pages", counts.get("text_pages")),
        ("layers (OCG)", ", ".join(o.get("name", "") for o in info.get("ocgs", [])) or "none"),
        ("annotations", counts.get("annotations")), ("images", counts.get("images")), ("tables", counts.get("tables")),
    ]
    for key in ("drawing_number", "title", "revision", "date", "scale"):
        if parsed.get(key):
            pairs.append((f"title block {key.replace('_', ' ')}", parsed[key]))
    return kv_table(pairs)


# --------------------------------------------------------------------------- #
# PPTX units
# --------------------------------------------------------------------------- #
def _notes_by_slide(sidecar_dir: Path) -> Dict[int, str]:
    """Speaker notes per slide from ``notes.md``."""
    path = sidecar_dir / sidecar_pptx.NOTES_NAME
    if not path.is_file():
        return {}
    notes: Dict[int, str] = {}
    current: Optional[int] = None
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^## Slide (\d+)$", line)
        if match:
            current = int(match.group(1))
            notes[current] = ""
        elif current is not None and not line.startswith("# "):
            notes[current] += line + "\n"
    return {k: v.strip() for k, v in notes.items() if v.strip() and v.strip() != "(no notes)"}


def _pptx_native_lines(shapes: Sequence[Dict[str, Any]], notes: str, md_keys: set) -> Tuple[List[str], int]:
    """Native-text lines for one slide and the number of words the sidecar supplies."""
    missing_shapes = [(s, missing_keys(s.get("text", ""), md_keys)) for s in shapes if s.get("text")]
    missing_shapes = [(s, keys) for s, keys in missing_shapes if keys]
    notes_missing = missing_keys(notes, md_keys) if notes else set()
    added = sum(len(keys) for _, keys in missing_shapes) + len(notes_missing)
    if not missing_shapes and not notes_missing:
        return ["The MarkItDown text covers every shape text and the speaker notes of this slide."], 0
    lines: List[str] = []
    if missing_shapes:
        lines.append(f"{len(missing_shapes)} shape(s) carry words absent from the MarkItDown text:")
        lines.append("")
        lines.append(md_table(("shape id", "name", "type", "text", "left (pt)", "top (pt)", "width (pt)", "height (pt)",
                               "rotation (deg)"),
                              [(s["shape_id"], s["name"], s["type"], s["text"], *(_fmt((s.get("pt") or {}).get(k))
                                for k in ("left", "top", "width", "height")), s.get("rotation", 0))
                               for s, _ in missing_shapes]))
    if notes_missing:
        lines += ["", "Speaker notes (sidecar):", "", clean_text(notes)]
    return lines, added


def _pptx_tables(number: int, tables: Sequence[Dict[str, Any]], sidecar_dir: Path) -> List[str]:
    """Rendered slide tables of one slide, each with its caption."""
    rendered: List[str] = []
    for t in tables:
        caption = (f"Table S{number}-{t.get('index')}: slide {number} table shape '{t.get('shape_name')}' (python-pptx), "
                   f"{t.get('rows')} rows x {t.get('cols')} columns, merged cells repeated: {t.get('merged_cell_count', 0)}; "
                   f"file {t.get('path')}.")
        rendered.append(caption + "\n\n" + grid_table(_csv_grid(sidecar_dir / str(t.get("path", "")))))
    return rendered


def _pptx_pictures(items: Sequence[Dict[str, Any]]) -> List[str]:
    """Picture lines for one slide."""
    if not items:
        return ["(no pictures on this slide)"]
    rows = []
    for i in items:
        pt = i.get("shape_pt") or {}
        size = f"{i.get('pixel_width')} x {i.get('pixel_height')} px" if i.get("pixel_width") else "unknown"
        position = ", ".join(_fmt(pt.get(k)) for k in ("left", "top", "width", "height")) if pt else ""
        rows.append((i.get("path") or i.get("error", ""), size, i.get("sha256", ""), position + (" pt" if position else "")))
    return [md_table(("file", "pixel size", "sha256", "slide position (left, top, width, height)"), rows)]


def _pptx_units(md: MarkItDownOutput, sidecar_dir: Path, rows: List[Dict[str, Any]], notes: List[str]) -> List[Unit]:
    """Units for a deck: one per slide."""
    slides = [s for s in read_jsonl(sidecar_dir / sidecar_pptx.SLIDES_NAME) if isinstance(s, dict)]
    tables_by_slide: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for t in _json(sidecar_dir / sidecar_pptx.TABLES_NAME, {}).get("tables", []):
        tables_by_slide[int(t["slide"])].append(t)
    pictures_by_slide: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for p in _json(sidecar_dir / sidecar_pptx.PICTURES_NAME, {}).get("items", []):
        if p.get("slide") is not None:
            pictures_by_slide[int(p["slide"])].append(p)
    speaker_notes = _notes_by_slide(sidecar_dir)
    segments, note = split_pptx_text(md.text, len(slides))
    if note:
        notes.append(note)
    whole_keys = recall_check.word_keys(md.text or "") if note and len(slides) > 1 else None
    units: List[Unit] = []
    for slide in slides:
        number = int(slide["slide"])
        segment = segments.get(number, "")
        md_keys = whole_keys if whole_keys is not None else recall_check.word_keys(segment)
        native, added = _pptx_native_lines(slide.get("shapes", []), speaker_notes.get(number, ""), md_keys)
        units.append(Unit(heading=f"## Slide {number}", label=f"slide {number}",
                          markitdown=markitdown_block(segment, md.error), native_method=NATIVE_METHOD["pptx"],
                          native_lines=native, tables=_pptx_tables(number, tables_by_slide[number], sidecar_dir),
                          annotations=_rows_for_unit(rows, number), pictures=_pptx_pictures(pictures_by_slide[number]),
                          added=added))
    return units


def _pptx_document(sidecar_dir: Path, manifest: Dict[str, Any]) -> str:
    """The ``## Document`` table of a deck."""
    info = _json(sidecar_dir / sidecar_pptx.SLIDE_INFO_NAME, {})
    totals = info.get("totals", {})
    layouts = Counter(s.get("layout", "") for s in info.get("slides", []))
    return kv_table([
        ("slides", info.get("slide_count")), ("slide size", f"{info.get('slide_width_pt')} x {info.get('slide_height_pt')} pt"),
        ("layouts", "; ".join(f"{name} ({n})" for name, n in layouts.most_common())),
        ("shapes", totals.get("shape_count")), ("tables", totals.get("table_count")), ("charts", totals.get("chart_count")),
        ("pictures placed", totals.get("picture_count")), ("unique images", totals.get("unique_images")),
        ("comments", totals.get("comment_count")), ("comments without slide", totals.get("comments_without_slide")),
        ("legacy comment parts", len((manifest.get("comment_parts") or {}).get("legacy_parts", []))),
        ("modern comment parts", len((manifest.get("comment_parts") or {}).get("modern_parts", []))),
    ])


# --------------------------------------------------------------------------- #
# Workbook units
# --------------------------------------------------------------------------- #
def unit_from_number_format(number_format: Optional[str]) -> Optional[str]:
    """A unit named in an Excel number format (``0.0 "L/s"`` -> ``L/s``, ``0%`` -> ``%``), else ``None``."""
    if not number_format or number_format == "General":
        return None
    for literal in _FORMAT_LITERAL_RE.findall(number_format):
        candidate = literal.strip()
        if candidate and _UNIT_LIKE_RE.match(candidate):
            return candidate
    if "%" in number_format:
        return "%"
    return None


def _sheet_grid(values: Sequence[Dict[str, str]]) -> Tuple[Dict[Tuple[int, int], str], Tuple[int, int, int, int]]:
    """Cell values keyed by ``(row, col)`` and the bounding ``(min_row, max_row, min_col, max_col)``."""
    from openpyxl.utils import column_index_from_string

    grid: Dict[Tuple[int, int], str] = {}
    for row in values:
        match = _CELL_RE.match(str(row.get("cell", "")))
        if not match:
            continue
        grid[(int(match.group(2)), column_index_from_string(match.group(1)))] = str(row.get("value", ""))
    if not grid:
        return grid, (0, 0, 0, 0)
    rows_ = [r for r, _ in grid]
    cols_ = [c for _, c in grid]
    return grid, (min(rows_), max(rows_), min(cols_), max(cols_))


def sheet_range_table(values: Sequence[Dict[str, str]], formats: Dict[str, Any]) -> Tuple[str, int]:
    """The sheet range as a Markdown table with row numbers and lettered columns (units from number formats).

    Returns ``(table text, rows shown)``; rows beyond :data:`MAX_TABLE_ROWS` are
    left to the values CSV and said so under the table.
    """
    from openpyxl.utils import get_column_letter

    grid, (min_row, max_row, min_col, max_col) = _sheet_grid(values)
    if not grid:
        return "(empty sheet)", 0
    headers = ["row"]
    for c in range(min_col, max_col + 1):
        letter = get_column_letter(c)
        unit = unit_from_number_format((formats.get(letter) or {}).get("dominant"))
        headers.append(f"{letter} ({unit})" if unit else letter)
    shown = min(max_row, min_row + MAX_TABLE_ROWS - 1)
    rows = [[str(r)] + [grid.get((r, c), "") for c in range(min_col, max_col + 1)] for r in range(min_row, shown + 1)]
    text = md_table(headers, rows)
    if shown < max_row:
        text += f"\n\nRows {shown + 1} to {max_row} are not shown here; the full range is in the values CSV."
    return text, len(rows)


def _xlsx_native_lines(sheet: Dict[str, Any], values: Sequence[Dict[str, str]], md_keys: set) -> Tuple[List[str], int]:
    """Native-text lines for one sheet and the number of cells the sidecar supplies."""
    facts = (f"Sheet state {sheet.get('state')}; dimensions {sheet.get('dimensions')}; "
             f"{sheet.get('cells_non_empty', 0)} non-empty cells; {sheet.get('formula_cells', 0)} formula cells "
             f"({sheet.get('cached_values_missing', 0)} without a cached value; formulas in "
             f"{(sheet.get('files') or {}).get('formulas')}); {sheet.get('merged_ranges', 0)} merged ranges; "
             f"{sheet.get('hidden_rows', 0)} hidden rows; {sheet.get('hidden_cols', 0)} hidden columns.")
    if not values:
        return [facts, "", "(empty sheet)"], 0
    missing = [row for row in values if row.get("value") and missing_keys(row["value"], md_keys)]
    if not missing:
        return [facts, "", "The MarkItDown text covers every cached cell value of this sheet."], 0
    lines = [facts, "", f"{len(missing)} cell(s) carry values absent from the MarkItDown text:", "",
             md_table(("cell", "value", "type"), [(r["cell"], r["value"], r.get("type", "")) for r in missing])]
    return lines, len(missing)


def _xlsx_units(md: MarkItDownOutput, sidecar_dir: Path, rows: List[Dict[str, Any]], notes: List[str]) -> List[Unit]:
    """Units for a workbook: one per sheet (hidden sheets included)."""
    workbook = _json(sidecar_dir / sidecar_xlsx.WORKBOOK_NAME, {})
    sheets = workbook.get("sheets", [])
    formats_all = _json(sidecar_dir / sidecar_xlsx.FORMATS_NAME, {})
    names = [str(s.get("name", "")) for s in sheets]
    segments, note = split_xlsx_text(md.text, names)
    if note:
        notes.append(note)
    whole_keys = recall_check.word_keys(md.text or "") if note and len(names) > 1 else None
    units: List[Unit] = []
    for sheet in sheets:
        name = str(sheet.get("name", ""))
        segment = segments.get(name, "")
        md_keys = whole_keys if whole_keys is not None else recall_check.word_keys(segment)
        values = read_csv_dicts(sidecar_dir / str((sheet.get("files") or {}).get("values", "")))
        native, added = _xlsx_native_lines(sheet, values, md_keys)
        tables: List[str] = []
        if values:
            table, shown = sheet_range_table(values, formats_all.get(name) or {})
            caption = (f"Table {name}: sheet range {sheet.get('dimensions')} (openpyxl cached values, {shown} row(s) shown; "
                       f"file {(sheet.get('files') or {}).get('values')}).")
            tables.append(caption + "\n\n" + table)
        units.append(Unit(heading=f"## Sheet {name}", label=f"sheet {name}",
                          markitdown=markitdown_block(segment, md.error), native_method=NATIVE_METHOD["xlsx"],
                          native_lines=native, tables=tables, annotations=_rows_for_unit(rows, name), added=added))
    return units


def _xlsx_document(sidecar_dir: Path, manifest: Dict[str, Any]) -> str:
    """The ``## Document`` table of a workbook (metadata, then one row per sheet)."""
    workbook = _json(sidecar_dir / sidecar_xlsx.WORKBOOK_NAME, {})
    props = workbook.get("properties") or {}
    conversion = workbook.get("converted_from")
    external = workbook.get("external_links") or {}
    pairs: List[Tuple[str, Any]] = [
        ("sheets", workbook.get("sheet_count")), ("hidden sheets", manifest.get("counts", {}).get("hidden_sheets")),
        ("title", props.get("title")), ("creator", props.get("creator")), ("last modified by", props.get("lastModifiedBy")),
        ("modified", props.get("modified")), ("defined names", len(workbook.get("defined_names", []))),
        ("external links", "; ".join(str(t) for t in external.get("targets", [])) if external.get("present") else "none"),
        ("VBA project", "present" if workbook.get("has_vba") else "none"),
        ("converted from", f"{conversion.get('tool')} {conversion.get('version')}" if conversion else "not needed"),
        ("comments", manifest.get("counts", {}).get("comment_count")),
    ]
    sheet_rows = [(s.get("index"), s.get("name"), s.get("state"), s.get("dimensions"), s.get("cells_non_empty"),
                   s.get("formula_cells"), s.get("merged_ranges")) for s in workbook.get("sheets", [])]
    return kv_table(pairs) + "\n\n" + md_table(("index", "sheet", "state", "dimensions", "cells", "formulas", "merged"),
                                              sheet_rows)


# --------------------------------------------------------------------------- #
# E-mail, image and other units
# --------------------------------------------------------------------------- #
def _email_units(md: MarkItDownOutput, sidecar_dir: Path, manifest: Dict[str, Any]) -> List[Unit]:
    """A single ``## Message`` unit: headers, the body when MarkItDown misses words, attachments listed."""
    record = _json(sidecar_dir / sidecar_misc.MESSAGE_NAME, {})
    headers = record.get("headers") or {}
    body = recall_check.email_body(sidecar_dir).strip()
    md_keys = recall_check.word_keys(md.text or "")
    missing = missing_keys(body, md_keys)
    lines = [kv_table([(k, headers.get(k)) for k in ("from", "to", "cc", "date", "subject", "message_id", "in_reply_to")])]
    if missing:
        lines += ["", f"Body text (sidecar; {len(missing)} word(s) absent from the MarkItDown text):", "",
                  clean_text(body) or "(no body text)"]
    else:
        lines += ["", "The MarkItDown text covers the body text."]
    attachments = record.get("attachments") or []
    lines += ["", f"Attachments ({len(attachments)}; listed only, none opened):", ""]
    if attachments:
        lines.append(md_table(("index", "name", "size (bytes)", "content type", "zip", "note"),
                              [(a.get("index"), a.get("name"), a.get("size_bytes"), a.get("content_type"),
                                "yes" if a.get("is_zip") else "", a.get("note") or "") for a in attachments]))
    else:
        lines.append("(no attachments)")
    method = (manifest.get("methods_used") or ["email"])[0]
    return [Unit(heading=UNIT_HEADINGS["email"], label="message", markitdown=markitdown_block(md.text or "", md.error),
                 native_method=method, native_lines=lines, added=len(missing))]


def _image_units(md: MarkItDownOutput, sidecar_dir: Path) -> List[Unit]:
    """A single ``## Image`` unit with the safe EXIF fields (GPS excluded as personal data)."""
    record = _json(sidecar_dir / sidecar_misc.IMAGE_NAME, {})
    safe = sidecar_misc.markdown_fields(record) if record else {}
    exif = safe.pop("exif", {}) or {}
    preview = safe.pop("preview", None) or {}
    pairs = [(k, safe.get(k)) for k in ("format", "width", "height", "mode", "phash", "gps", "caption_note", "error")]
    pairs += [(f"exif {k}", v) for k, v in exif.items()]
    if preview:
        pairs.append(("preview", f"{preview.get('path')} ({preview.get('width')} x {preview.get('height')} px)"))
    return [Unit(heading=UNIT_HEADINGS["image"], label="image", markitdown=markitdown_block(md.text or "", md.error),
                 native_method=NATIVE_METHOD["image"], native_lines=[kv_table(pairs)])]


def _other_units(md: MarkItDownOutput, sidecar_dir: Path) -> List[Unit]:
    """A single ``## File`` unit for a type without a converter."""
    record = _json(sidecar_dir / sidecar_misc.OTHER_NAME, {})
    return [Unit(heading=UNIT_HEADINGS["other"], label="file", markitdown=markitdown_block(md.text or "", md.error),
                 native_method=NATIVE_METHOD["other"],
                 native_lines=[record.get("note") or "No converter applies to this file type."])]


def _misc_document(sidecar_dir: Path, manifest: Dict[str, Any]) -> str:
    """The ``## Document`` table for e-mail, images and other files."""
    pairs: List[Tuple[str, Any]] = [("kind", manifest.get("kind")), ("bytes", manifest.get("bytes"))]
    pairs += [(f"count {k}", v) for k, v in (manifest.get("counts") or {}).items()]
    return kv_table(pairs)


def _fallback_units(md: MarkItDownOutput, kind: str, sidecar_error: str) -> List[Unit]:
    """One unit holding the whole MarkItDown text when the sidecar module failed."""
    heading = {"pdf": "## Page 1", "pptx": "## Slide 1", "xlsx": "## Sheet (unknown)"}.get(kind, UNIT_HEADINGS.get(kind, "## File"))
    return [Unit(heading=heading, label=heading[3:].lower(), markitdown=markitdown_block(md.text or "", md.error),
                 native_method=NATIVE_METHOD.get(kind, "none"),
                 native_lines=[f"Sidecar extraction failed: {sidecar_error}"])]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render_unit(unit: Unit) -> str:
    """One unit in the prescribed order: MarkItDown text, native text, tables, annotations, pictures."""
    parts = [section_mark("unit"), unit.heading, "", method_mark("markitdown"), unit.markitdown, "",
             "### Native text (sidecar)", method_mark(unit.native_method)]
    parts += unit.native_lines or ["(nothing to add)"]
    parts += ["", section_mark("tables"), "### Tables", ""]
    parts.append("\n\n".join(unit.tables) if unit.tables else "(no tables)")
    parts += ["", section_mark("annotations"), "### Annotations and comments", ""]
    if unit.annotations:
        parts.append(md_table(UNIT_ANNOTATION_COLUMNS, [[row.get(c, "") for c in UNIT_ANNOTATION_COLUMNS]
                                                        for row in unit.annotations]))
    else:
        parts.append("(no annotations or comments)")
    if unit.pictures is not None:
        parts += ["", section_mark("pictures"), "### Pictures", ""] + unit.pictures
    return "\n".join(parts) + "\n"


def render_register(rows: Sequence[Dict[str, Any]]) -> str:
    """The ``## Comments register`` section."""
    parts = [section_mark("comments-register"), "## Comments register", ""]
    if rows:
        parts.append(md_table(REGISTER_COLUMNS, [[row.get(c, "") for c in REGISTER_COLUMNS] for row in rows]))
    else:
        parts.append("(no annotations or comments in this file)")
    return "\n".join(parts) + "\n"


def document_table(kind: str, sidecar_dir: Path, manifest: Optional[Dict[str, Any]]) -> str:
    """The ``## Document`` block for ``kind``."""
    if manifest is None:
        return "(sidecar failed; no document metadata)"
    if kind == "pdf":
        return _pdf_document(sidecar_dir, manifest)
    if kind == "pptx":
        return _pptx_document(sidecar_dir, manifest)
    if kind == "xlsx":
        return _xlsx_document(sidecar_dir, manifest)
    return _misc_document(sidecar_dir, manifest)


def build_units(kind: str, md: MarkItDownOutput, sidecar_dir: Path, manifest: Optional[Dict[str, Any]],
                sidecar_error: Optional[str], rows: List[Dict[str, Any]], notes: List[str]) -> List[Unit]:
    """The units of the file for its kind (a single fallback unit when the sidecar failed)."""
    if manifest is None:
        return _fallback_units(md, kind, sidecar_error or "unknown error")
    if kind == "pdf":
        return _pdf_units(md, sidecar_dir, rows, notes)
    if kind == "pptx":
        return _pptx_units(md, sidecar_dir, rows, notes)
    if kind == "xlsx":
        return _xlsx_units(md, sidecar_dir, rows, notes)
    if kind == "email":
        return _email_units(md, sidecar_dir, manifest)
    if kind == "image":
        return _image_units(md, sidecar_dir)
    return _other_units(md, sidecar_dir)


def build_body(source: Source, md: MarkItDownOutput, sidecar_dir: Path, manifest: Optional[Dict[str, Any]],
               sidecar_error: Optional[str] = None) -> Body:
    """Render everything from the title to the Comments register and report what went into it."""
    sidecar_dir = Path(sidecar_dir)
    notes: List[str] = []
    rows = annotation_rows(source.kind, sidecar_dir) if manifest is not None else []
    units = build_units(source.kind, md, sidecar_dir, manifest, sidecar_error, rows, notes)
    parts = [f"# {source.path.name}", "", section_mark("document"), "## Document", "",
             document_table(source.kind, sidecar_dir, manifest), ""]
    parts += [render_unit(unit) for unit in units]
    parts.append(render_register(rows))
    return Body(text="\n".join(parts), units=[u.heading for u in units], unit_count=len(units),
                tables_written=sum(len(u.tables) for u in units), comment_rows=len(rows),
                sidecar_added=sum(u.added for u in units), notes=notes)


def front_matter_fields(source: Source, md: MarkItDownOutput, manifest: Optional[Dict[str, Any]], body: Body,
                        grade: str, grade_reason: str, pipeline_version: str) -> Dict[str, Any]:
    """The front matter dictionary in :data:`FRONT_MATTER_KEYS` order."""
    dept = parse_department(source.rel_path)
    converters = [{"name": "markitdown", "version": md.version}]
    methods = ["markitdown"] if md.error is None else []
    if manifest is not None:
        converters += [dict(c) for c in manifest.get("converters", [])]
        methods += list(manifest.get("methods_used", []))
    count: Optional[int] = body.unit_count if manifest is not None else None
    fields: Dict[str, Any] = {
        "source_path": source.rel_path, "drive_id": source.drive_id, "sha256": source.sha256, "bytes": source.bytes,
        "drive_modified": source.drive_modified, "root": dept["root"], "dept_no": dept["dept_no"] or None,
        "dept_name": dept["dept_name"] or None, "stage": parse_stage(source.rel_path),
        "doc_class": classify_document(source.rel_path), "pages_or_slides_or_sheets": count,
        "converters": converters, "methods_used": methods, "grade": grade, "grade_reason": grade_reason,
        "generated_at": utc_now_iso(), "pipeline_version": pipeline_version,
    }
    return {key: fields.get(key) for key in FRONT_MATTER_KEYS}


def render_front_matter(fields: Dict[str, Any]) -> str:
    """YAML front matter between ``---`` lines, keys in layout order."""
    ordered = {key: fields.get(key) for key in FRONT_MATTER_KEYS}
    dumped = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True, default_flow_style=False, width=1000)
    return "---\n" + dumped + "---\n"


def _checks_table(checks: Sequence[Dict[str, Any]]) -> str:
    """Check / result / evidence table."""
    return md_table(("check", "result", "evidence"),
                    [(c.get("check"), "pass" if c.get("pass") else "FAIL", c.get("evidence", "")) for c in checks])


def render_provenance(source: Source, md: MarkItDownOutput, sidecar_dir_label: str, manifest: Optional[Dict[str, Any]],
                      sidecar_error: Optional[str], body: Body, checks: Dict[str, Any]) -> str:
    """The ``## Provenance`` section: sidecar files, counts, the checks run and their results."""
    files = list((manifest or {}).get("files") or [])
    lines = [section_mark("provenance"), "## Provenance", "",
             f"- Source: `{source.rel_path}` (sha256 {source.sha256}, {source.bytes} bytes)",
             f"- Sidecar directory: `{sidecar_dir_label}`"]
    if manifest is not None:
        version = manifest.get("module_version") or manifest.get("pipeline_version")
        lines.append(f"- Sidecar module: {manifest.get('module')} {version}, generated {manifest.get('generated_at')}; "
                     f"methods {', '.join(manifest.get('methods_used', []))}")
        lines.append(f"- Sidecar files written ({len(files)}): " + ", ".join(f"`{f}`" for f in files))
    else:
        lines.append(f"- Sidecar module failed: {sidecar_error}")
    status = "ok" if md.error is None else f"failed: {md.error}"
    lines.append(f"- MarkItDown {md.version}: {status} ({md.seconds:.1f} s), no model client, no plugins")
    for note in body.notes:
        lines.append(f"- Note: {note}")
    lines.append(f"- Units written: {body.unit_count}; tables: {body.tables_written}; comment rows: {body.comment_rows}; "
                 f"words or cells supplied by the sidecar: {body.sidecar_added}")
    lines += ["", "### Counts", ""]
    counts = dict((manifest or {}).get("counts") or {})
    lines.append(kv_table(sorted(counts.items())) if counts else "(no counts)")
    sidecar_checks = list((manifest or {}).get("checks") or [])
    if sidecar_checks:
        lines += ["", "### Sidecar self-checks", "", _checks_table(sidecar_checks)]
    lines += ["", "### Conversion checks", "",
              f"Numeric recall {checks.get('numeric_recall')} (MarkItDown alone {checks.get('numeric_recall_markitdown')}; "
              f"threshold {checks.get('threshold')}; {checks.get('expected_tokens')} native tokens, "
              f"{checks.get('misses_total')} missing).", ""]
    if checks.get("misses"):
        lines += ["Missing tokens: " + ", ".join(str(m) for m in checks["misses"]), ""]
    lines.append(_checks_table(checks.get("checks", [])))
    lines += ["", f"Grade **{checks.get('grade')}**: {checks.get('grade_reason')}", ""]
    return "\n".join(lines)


def render_document(fields: Dict[str, Any], body: Body, provenance: str) -> str:
    """Front matter, body and provenance as one Markdown text."""
    return render_front_matter(fields) + "\n" + body.text + "\n" + provenance


def render_duplicate(fields: Dict[str, Any], first_rel_path: str, first_md: str) -> str:
    """The short Markdown of a hash-identical duplicate: front matter plus one line."""
    name = Path(str(fields.get("source_path") or "")).name
    line = (f"Hash-identical duplicate of `{first_rel_path}` (sha256 {fields.get('sha256')}); "
            f"the full conversion is `{first_md}`.")
    return render_front_matter(fields) + f"\n# {name}\n\n{line}\n"


# --------------------------------------------------------------------------- #
# Command line (single-file render from an existing sidecar directory)
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """Argument parser: ``render --source FILE --rel-path REL --sidecar DIR --out MD [--markitdown-text FILE]``."""
    parser = argparse.ArgumentParser(description="Render the Markdown of one file from its sidecar directory.")
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render", help="write the Markdown for one source file")
    render.add_argument("--source", required=True, type=Path, help="the source file (read only)")
    render.add_argument("--rel-path", required=True, help="path relative to data/source (Drive layout)")
    render.add_argument("--sidecar", required=True, type=Path, help="sidecar directory already written for the file")
    render.add_argument("--out", required=True, type=Path, help="Markdown file to write")
    render.add_argument("--markitdown-text", type=Path, default=None, help="file holding MarkItDown's text")
    render.add_argument("--pipeline-version", default="manual", help="pipeline_version for the front matter")
    return parser


def render_from_sidecar(source_path: Path, rel_path: str, sidecar_dir: Path, out_path: Path,
                        markitdown_text: Optional[str], pipeline_version: str) -> Dict[str, Any]:
    """Build, check and write the Markdown for one file whose sidecar already exists; return the checks."""
    manifest = _json(sidecar_dir / "sidecar_manifest.json", None)
    kind = recall_check.kind_for_path(source_path)
    source = Source(path=source_path, rel_path=rel_path, sha256=sha256_file(source_path),
                    bytes=source_path.stat().st_size, kind=kind, drive_id=(manifest or {}).get("drive_id"),
                    drive_modified=(manifest or {}).get("drive_modified"))
    md = MarkItDownOutput(text=markitdown_text, version="not run",
                          error=None if markitdown_text is not None else "MarkItDown text not supplied")
    body = build_body(source, md, sidecar_dir, manifest, None if manifest else "no sidecar_manifest.json")
    checks = recall_check.run_checks(kind, sidecar_dir, body.text, md.text, sidecar_added=body.sidecar_added,
                                     sidecar_ok=manifest is not None, markitdown_error=md.error,
                                     threshold=recall_check.threshold_from_config())
    recall_check.write_checks(sidecar_dir, checks)
    fields = front_matter_fields(source, md, manifest, body, checks["grade"], checks["grade_reason"], pipeline_version)
    provenance = render_provenance(source, md, str(sidecar_dir), manifest, None, body, checks)
    atomic_write_text(out_path, render_document(fields, body, provenance))
    return checks


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point; returns a process exit code (1 on failure)."""
    args = build_parser().parse_args(argv)
    try:
        text = args.markitdown_text.read_text(encoding="utf-8") if args.markitdown_text else None
        checks = render_from_sidecar(args.source, args.rel_path, args.sidecar, args.out, text, args.pipeline_version)
    except Exception as exc:  # noqa: BLE001 - surface any failure as a non-zero exit
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"md_writer: {args.out} (grade {checks['grade']}: {checks['grade_reason']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
