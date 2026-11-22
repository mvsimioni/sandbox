"""Per-page PDF sidecars for the AIGH IP ACMV pipeline.

``python -m src.sidecar_pdf run --pdf <path> --out <dir>`` reads one PDF with
PyMuPDF (and pdfplumber for tables on text pages) and writes, into ``<dir>``:

* ``words.jsonl``        one row per word: page, block, line, word_no, text,
                         bbox raw and in page view, rotation of its line;
* ``blocks.csv``         one row per text block with its page-view bbox;
* ``page_info.json``     document OCG list and one record per page (size,
                         rotation, text chars, images and coverage, vector
                         path count, annotation count, page class, flags);
* ``annotations.jsonl``  every annotation with geometry raw and in page view,
                         colours as hex, optional content, popup, IRT reply
                         link and the FreeText default appearance string;
* ``images.json``        placed images with page-view bbox and pixel size;
* ``fills.json``         histogram of vector fill colours per page (top 20);
* ``tables_p<N>_<k>.csv`` and ``tables.json`` (pdfplumber, text pages only;
                         the page is handed over with the ``/Rotate`` that makes
                         its text horizontal and cells are filled from whole words);
* ``titleblock.json``    strings in the title-block zone of drawing pages and
                         a best-effort parse of number, title, revision, date,
                         scale;
* ``notes.md``           NOTES / GENERAL NOTES / LEGEND blocks verbatim;
* ``sidecar_manifest.json`` provenance: source sha256, files written with
                         their hashes, counts, converter versions.

The run is idempotent: when the manifest in ``<dir>`` already carries the
source sha256 (and every listed file exists) nothing is rewritten unless
``--force`` is given.  Time budgets use ``SIGALRM`` and are enforced on the main
thread only; off it the work still runs and the notes say the budget was not
applied.  All coordinates are PDF points; "page view" means the
page as displayed, i.e. raw coordinates transformed by ``page.rotation_matrix``.
Nothing here touches the network.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import sys
import tempfile
import warnings
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

try:  # package import (python -m src.sidecar_pdf, tests)
    from src import census
    from src.common import (
        PATHS,
        atomic_write_json,
        atomic_write_text,
        ensure_dir,
        project_root,
        read_jsonl,
        sha256_file,
        utc_now_iso,
        write_csv_dicts,
    )
    from src.state import Progress
except ModuleNotFoundError:  # run as a plain script from inside src/
    import census  # type: ignore[no-redef]
    from common import (  # type: ignore[no-redef]
        PATHS,
        atomic_write_json,
        atomic_write_text,
        ensure_dir,
        project_root,
        read_jsonl,
        sha256_file,
        utc_now_iso,
        write_csv_dicts,
    )
    from state import Progress  # type: ignore[no-redef,attr-defined]

PIPELINE_VERSION = "0.1.0"
MODULE_NAME = "sidecar_pdf"

DEFAULT_PATH_BUDGET_S: float = 20.0
DEFAULT_TABLE_BUDGET_S: float = 60.0
TEXT_PAGE_MIN_CHARS: int = census.TEXT_MIN_CHARS
FEW_VECTOR_PATHS: int = census.VECTOR_THRESHOLD
MANY_VECTOR_PATHS: int = census.VECTOR_THRESHOLD
A1_SHORT_PT: float = 1684.0
A1_LONG_PT: float = 2384.0
SIZE_TOLERANCE: float = 0.98
TITLE_BLOCK_RIGHT_SHARE: float = 0.18
TITLE_BLOCK_BOTTOM_SHARE: float = 0.12
FILL_TOP_N: int = 20
MIN_TABLE_ROWS: int = 2
MIN_TABLE_COLS: int = 2

BLOCK_COLUMNS: Tuple[str, ...] = ("page", "block", "text", "x0", "y0", "x1", "y1")
GEOMETRY_KINDS: Dict[str, str] = {
    "Line": "vertices", "Polygon": "vertices", "PolyLine": "vertices",
    "Highlight": "quadpoints", "Underline": "quadpoints", "StrikeOut": "quadpoints",
    "Squiggly": "quadpoints", "Redact": "quadpoints",
    "Ink": "inklist",
}
NOTES_RE = re.compile(r"^\s*(GENERAL\s+NOTES?|NOTES?|LEGEND)\b", re.IGNORECASE)
DRAWING_NUMBER_RES: Tuple[re.Pattern[str], ...] = (
    re.compile(r"\bIP-BH-\d{5}\b"),
    re.compile(r"\bHS-BH-\d+\b"),
    re.compile(r"\bAR-(?:IP|OP)-\d\d-\d\d-\d{3}\b"),
    re.compile(r"\bAR-(?:IP|OP)-31[-_]ZZ-\d{3}\b"),
)
REVISION_RE = re.compile(r"\bREV(?:ISION)?\.?\s*[:#.]?\s*([A-Z]{1,2}\d{0,2}|P\d{1,2}|\d{1,2})\b", re.IGNORECASE)
DATE_RES: Tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![\w\-/])\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}(?![\w\-/])"),
    re.compile(r"(?<![\w\-/])\d{4}-\d{2}-\d{2}(?![\w\-/])"),
    re.compile(r"\b\d{1,2}\s+(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC)[A-Z]*\.?\s+\d{2,4}\b", re.IGNORECASE),
    re.compile(r"\b(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC)[A-Z]*\.?\s+\d{4}\b", re.IGNORECASE),
)
SCALE_RE = re.compile(r"\b1\s*:\s*\d+(?:\s*@\s*A[0-4])?\b|\bN\.?T\.?S\.?\b|\bAS\s+SHOWN\b|\bNOT\s+TO\s+SCALE\b", re.IGNORECASE)
FIELD_LABEL_RE = re.compile(
    r"^\s*(DRAWING\s+(?:NO|NUMBER|TITLE)|DWG\.?\s*(?:NO|TITLE)|DRG\.?\s*(?:NO|TITLE)|SHEET(?:\s+NO)?|"
    r"REV(?:ISION)?|DATE|SCALE|DRAWN|CHECKED|APPROVED|DESIGNED|PROJECT(?:\s+TITLE)?|JOB\s*(?:NO|NUMBER)|"
    r"CLIENT|CONSULTANT|CONTRACTOR|STATUS|STAGE|CONTRACT|TITLE|SIZE|FILE|MARK|DESCRIPTION|KEY\s+PLAN)\b[\s:.]*$",
    re.IGNORECASE,
)
REVISION_TOKEN_RE = re.compile(r"[A-Z]{1,2}\d{0,2}|P\d{1,2}|\d{1,2}", re.IGNORECASE)
CROSS_REFERENCE_RE = re.compile(r"\b(?:REFER(?:\s+TO)?|REF\.?|SEE|AS\s+PER|CONT(?:INUED|D)?\.?\s+ON)\b", re.IGNORECASE)
LABEL_WINDOW: int = 8
TITLE_MAX_LINES: int = 3
ALIGN_TOLERANCE_PT: float = 40.0
NUMERIC_RE = re.compile(r"^[+-]?\d[\d,]*(?:\.\d+)?$")
UNIT_SUFFIX_RE = re.compile(r"(?:[A-Za-z%°²³µ]+(?:/[A-Za-z0-9²³]+)?|°C)$")

Box = Tuple[float, float, float, float]
Point = Tuple[float, float]


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
def view_point(point: Sequence[float], matrix: Any) -> Point:
    """Transform a raw point to page view with ``page.rotation_matrix``."""
    x, y = float(point[0]), float(point[1])
    return (round(x * matrix.a + y * matrix.c + matrix.e, 3), round(x * matrix.b + y * matrix.d + matrix.f, 3))


def view_box(box: Sequence[float], matrix: Any) -> Box:
    """Transform a raw bbox to page view (normalised so x0 <= x1 and y0 <= y1)."""
    x0, y0 = view_point((box[0], box[1]), matrix)
    x1, y1 = view_point((box[2], box[3]), matrix)
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def round_box(box: Sequence[float]) -> List[float]:
    """Round a bbox to three decimals as a plain list."""
    return [round(float(v), 3) for v in box[:4]]


def rotation_from_dir(direction: Sequence[float]) -> int:
    """Rotation in degrees (0, 90, 180, 270) of a text line from its ``dir`` vector.

    PyMuPDF's y axis points down, so ``(0, -1)`` is text running up the page,
    which reads as a 90 degree anticlockwise rotation.
    """
    dx, dy = float(direction[0]), float(direction[1])
    if dx == 0.0 and dy == 0.0:
        return 0
    degrees = math.degrees(math.atan2(-dy, dx))
    return int(round(degrees / 90.0) * 90) % 360


def is_a1_or_larger(width: float, height: float) -> bool:
    """True when the page view is at least A1 (594 x 841 mm) in either orientation."""
    short, long = sorted((float(width), float(height)))
    return short >= A1_SHORT_PT * SIZE_TOLERANCE and long >= A1_LONG_PT * SIZE_TOLERANCE


def colour_hex(components: Optional[Sequence[float]]) -> Optional[str]:
    """Grey, RGB or CMYK components (0..1) as ``#rrggbb``; ``None`` when absent."""
    if not components:
        return None
    values = [max(0.0, min(1.0, float(v))) for v in components]
    if len(values) == 1:
        rgb = (values[0], values[0], values[0])
    elif len(values) == 3:
        rgb = (values[0], values[1], values[2])
    elif len(values) == 4:
        c, m, y, k = values
        rgb = ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    else:
        return None
    return "#%02x%02x%02x" % tuple(int(round(v * 255)) for v in rgb)


def pdf_date_iso(value: Optional[str]) -> Optional[str]:
    """Convert a PDF date string (``D:YYYYMMDDHHmmSS...``) to ISO 8601; ``None`` when unparsable."""
    if not value:
        return None
    digits = re.sub(r"^D:", "", value.strip())
    match = re.match(r"(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?", digits)
    if not match:
        return None
    parts = [int(g) if g else default for g, default in zip(match.groups(), (1, 1, 1, 0, 0, 0))]
    try:
        stamp = datetime(*parts)
    except ValueError:
        return None
    zone = re.search(r"([Zz+\-])(\d{2})?'?(\d{2})?", digits[len(match.group(0)):])
    suffix = ""
    if zone:
        if zone.group(1).upper() == "Z":
            suffix = "Z"
        elif zone.group(2):
            suffix = f"{zone.group(1)}{zone.group(2)}:{zone.group(3) or '00'}"
    return stamp.strftime("%Y-%m-%dT%H:%M:%S") + suffix


# --------------------------------------------------------------------------- #
# Numeric tokens (recall check)
# --------------------------------------------------------------------------- #
def _word_text(word: Any) -> str:
    """Text of a word given as a string, a dict row or a PyMuPDF words tuple."""
    if isinstance(word, str):
        return word
    if isinstance(word, dict):
        return str(word.get("text", ""))
    if isinstance(word, (tuple, list)) and len(word) >= 5:
        return str(word[4])
    return str(word)


def _numeric_from_fragment(fragment: str) -> Optional[str]:
    """Normalise one fragment (units stripped) to a numeric string, or ``None``."""
    core = fragment.strip("()[]{}<>,;:'\"*#=+")
    core = UNIT_SUFFIX_RE.sub("", core).rstrip(".")
    core = core.lstrip("+")
    if core.endswith("-") or core.endswith("/"):
        core = core[:-1]
    if not NUMERIC_RE.match(core):
        return None
    core = core.replace(",", "")
    if core.startswith("-") and core[1:] == "":
        return None
    return core


def numeric_tokens(words: Iterable[Any]) -> Set[str]:
    """Return the set of numeric strings (integers and decimals, units stripped) in ``words``.

    ``words`` may hold plain strings, ``words.jsonl`` rows or PyMuPDF word
    tuples.  Duct sizes such as ``600x300`` and ratios such as ``1:100`` are
    split into their parts; thousands separators are removed; a trailing unit
    (``L/s``, ``Pa``, ``mm``, ``%``, ``m2``) is stripped.  The same function is
    applied to the Markdown and to the sidecar words so recall is comparable.
    """
    found: Set[str] = set()
    for word in words:
        text = _word_text(word).strip()
        if not text or not any(ch.isdigit() for ch in text):
            continue
        for fragment in re.split(r"[xX×:/]", text):
            value = _numeric_from_fragment(fragment)
            if value is not None:
                found.add(value)
    return found


# --------------------------------------------------------------------------- #
# Text: words, blocks, notes
# --------------------------------------------------------------------------- #
def _line_rotations(page: Any, flags: int) -> Dict[Tuple[int, int], int]:
    """Map ``(block, line)`` to the rotation of that line from the text dict."""
    rotations: Dict[Tuple[int, int], int] = {}
    for b_no, block in enumerate(page.get_text("dict", flags=flags).get("blocks", [])):
        for l_no, line in enumerate(block.get("lines", [])):
            rotations[(b_no, l_no)] = rotation_from_dir(line.get("dir", (1.0, 0.0)))
    return rotations


def extract_words(page: Any, number: int) -> List[Dict[str, Any]]:
    """Word rows for one page: raw bbox, page-view bbox and the rotation of the word's line.

    ``rotation`` is the line's own rotation in raw coordinates (from the text
    dict ``dir`` vector); ``rotation_view`` adds the page's ``/Rotate`` so it is
    the rotation as displayed.
    """
    import pymupdf

    flags = pymupdf.TEXTFLAGS_WORDS
    matrix = page.rotation_matrix
    rotations = _line_rotations(page, flags)
    page_rotation = int(page.rotation)
    rows: List[Dict[str, Any]] = []
    for x0, y0, x1, y1, text, block, line, word_no in page.get_text("words", flags=flags):
        rotation = rotations.get((int(block), int(line)), 0)
        rows.append({
            "page": number,
            "block": int(block),
            "line": int(line),
            "word_no": int(word_no),
            "text": text,
            "bbox_raw": round_box((x0, y0, x1, y1)),
            "bbox_view": round_box(view_box((x0, y0, x1, y1), matrix)),
            "rotation": rotation,
            "rotation_view": (rotation - page_rotation) % 360,
        })
    return rows


def extract_blocks(page: Any, number: int) -> List[Dict[str, Any]]:
    """Text blocks for one page with their page-view bbox."""
    matrix = page.rotation_matrix
    rows: List[Dict[str, Any]] = []
    for x0, y0, x1, y1, text, block_no, block_type in page.get_text("blocks"):
        if block_type != 0:
            continue
        vx0, vy0, vx1, vy1 = view_box((x0, y0, x1, y1), matrix)
        rows.append({
            "page": number,
            "block": int(block_no),
            "text": text.rstrip("\n"),
            "x0": round(vx0, 3), "y0": round(vy0, 3), "x1": round(vx1, 3), "y1": round(vy1, 3),
        })
    return rows


def notes_blocks(blocks: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Blocks whose first words are NOTES, GENERAL NOTES or LEGEND."""
    return [block for block in blocks if NOTES_RE.match(str(block.get("text", "")))]


def render_notes_md(notes: Sequence[Dict[str, Any]], source_name: str) -> str:
    """Markdown with every notes block verbatim under a page/bbox heading."""
    lines = [f"# Notes blocks: {source_name}", ""]
    if not notes:
        lines.append("No NOTES, GENERAL NOTES or LEGEND block found.")
        lines.append("")
    for block in notes:
        bbox = ", ".join(f"{block[k]:.1f}" for k in ("x0", "y0", "x1", "y1"))
        lines.append(f"## Page {block['page']}, block {block['block']} (page view bbox {bbox})")
        lines.append("")
        lines.append("```text")
        lines.append(str(block["text"]))
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Drawings, images, fills
# --------------------------------------------------------------------------- #
def budget_note(what: str) -> Optional[str]:
    """A page note when the SIGALRM budget cannot be armed (worker thread); ``None`` on the main thread."""
    if census.budget_enforceable():
        return None
    return f"{what}_budget_not_enforced:worker_thread"


def collect_drawings(page: Any, budget_s: float) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """``page.get_cdrawings()`` under a wall-clock budget; ``(None, note)`` on timeout or error.

    Off the main thread the budget cannot be enforced (``SIGALRM``); the paths
    are still collected and the note says that the budget was not applied.
    """
    note = budget_note("path_count")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            with census.time_budget(budget_s, "page_paths"):
                return list(page.get_cdrawings()), note
    except census.BudgetExceeded:
        return None, f"path_count_timeout>{budget_s:g}s"
    except Exception as exc:  # noqa: BLE001 - damaged content stream
        return None, f"path_count_error:{type(exc).__name__}"


def fill_histogram(drawings: Optional[Sequence[Dict[str, Any]]], number: int) -> Dict[str, Any]:
    """Top-N fill colours of a page's vector paths with counts and summed bbox area."""
    counts: Counter = Counter()
    areas: Dict[str, float] = defaultdict(float)
    filled = 0
    for item in drawings or []:
        fill = item.get("fill")
        if not fill or "f" not in str(item.get("type", "")):
            continue
        key = colour_hex(fill) or "unknown"
        filled += 1
        counts[key] += 1
        rect = item.get("rect")
        if rect:
            areas[key] += abs(float(rect[2]) - float(rect[0])) * abs(float(rect[3]) - float(rect[1]))
    top = [
        {"colour": colour, "count": count, "area_pt2": round(areas[colour], 1)}
        for colour, count in counts.most_common(FILL_TOP_N)
    ]
    return {
        "page": number,
        "paths_total": None if drawings is None else len(drawings),
        "filled_paths": filled,
        "distinct_fill_colours": len(counts),
        "top_fills": top,
    }


def extract_images(page: Any, number: int) -> List[Dict[str, Any]]:
    """Placed images on a page with page-view bbox, pixel size and colourspace."""
    matrix = page.rotation_matrix
    rows: List[Dict[str, Any]] = []
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:  # noqa: BLE001 - damaged resources
        return rows
    for info in infos:
        bbox = info.get("bbox") or (0, 0, 0, 0)
        rows.append({
            "page": number,
            "xref": int(info.get("xref") or 0),
            "bbox_raw": round_box(bbox),
            "bbox_view": round_box(view_box(bbox, matrix)),
            "width_px": int(info.get("width") or 0),
            "height_px": int(info.get("height") or 0),
            "colourspace": info.get("cs-name") or "",
            "bits_per_component": info.get("bpc"),
            "has_mask": bool(info.get("has-mask", False)),
        })
    return rows


# --------------------------------------------------------------------------- #
# Annotations
# --------------------------------------------------------------------------- #
def _xref_string(doc: Any, xref: int, key: str) -> Optional[str]:
    """Value of a string-like key on an xref, or ``None``."""
    if not xref:
        return None
    try:
        kind, value = doc.xref_get_key(xref, key)
    except Exception:  # noqa: BLE001 - dangling reference
        return None
    if kind in ("string", "name", "int", "real"):
        return str(value)
    return None


def _xref_ref(doc: Any, xref: int, key: str) -> Optional[int]:
    """Target xref of an indirect reference key (``/IRT 17 0 R`` -> 17), or ``None``."""
    if not xref:
        return None
    try:
        kind, value = doc.xref_get_key(xref, key)
    except Exception:  # noqa: BLE001
        return None
    if kind == "xref":
        match = re.match(r"(\d+)", str(value))
        return int(match.group(1)) if match else None
    return None


def _oc_name(doc: Any, oc_xref: int, ocgs: Dict[int, Dict[str, Any]]) -> Optional[str]:
    """Name of an optional-content group or membership dictionary by xref."""
    if not oc_xref:
        return None
    entry = ocgs.get(oc_xref)
    if entry:
        return str(entry.get("name", ""))
    name = _xref_string(doc, oc_xref, "Name")
    if name:
        return name
    member = _xref_ref(doc, oc_xref, "OCGs")
    if member and member in ocgs:
        return str(ocgs[member].get("name", ""))
    return None


def _points(values: Any) -> List[Point]:
    """Normalise a PyMuPDF point list to ``[(x, y), ...]``."""
    out: List[Point] = []
    for item in values or []:
        if hasattr(item, "x"):
            out.append((float(item.x), float(item.y)))
        elif isinstance(item, (tuple, list)) and len(item) >= 2 and not isinstance(item[0], (tuple, list)):
            out.append((float(item[0]), float(item[1])))
    return out


def _geometry(annot: Any, type_name: str, matrix: Any) -> Dict[str, Any]:
    """Vertices, quadpoints or inklist of an annotation, raw and in page view."""
    kind = GEOMETRY_KINDS.get(type_name)
    result: Dict[str, Any] = {
        "vertices_raw": None, "vertices_view": None,
        "quadpoints_raw": None, "quadpoints_view": None,
        "inklist_raw": None, "inklist_view": None,
    }
    if kind is None:
        return result
    try:
        vertices = annot.vertices
    except Exception:  # noqa: BLE001
        vertices = None
    if not vertices:
        return result
    if kind == "inklist":
        strokes = [_points(stroke) for stroke in vertices if isinstance(stroke, (list, tuple))]
        result["inklist_raw"] = [[list(p) for p in s] for s in strokes]
        result["inklist_view"] = [[list(view_point(p, matrix)) for p in s] for s in strokes]
        return result
    points = _points(vertices)
    if kind == "quadpoints":
        quads = [points[i:i + 4] for i in range(0, len(points), 4)]
        result["quadpoints_raw"] = [[list(p) for p in q] for q in quads]
        result["quadpoints_view"] = [[list(view_point(p, matrix)) for p in q] for q in quads]
        return result
    result["vertices_raw"] = [list(p) for p in points]
    result["vertices_view"] = [list(view_point(p, matrix)) for p in points]
    return result


def _popup(doc: Any, annot: Any) -> Tuple[Optional[int], Optional[str]]:
    """Popup xref and any contents stored on the popup object itself."""
    try:
        popup_xref = int(annot.popup_xref or 0)
    except Exception:  # noqa: BLE001
        popup_xref = 0
    if not popup_xref:
        return None, None
    return popup_xref, _xref_string(doc, popup_xref, "Contents")


def annotation_record(doc: Any, page: Any, annot: Any, number: int, index: int,
                      ocgs: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    """One ``annotations.jsonl`` row for ``annot``."""
    matrix = page.rotation_matrix
    kind = annot.type
    type_name = str(kind[1]) if isinstance(kind, (tuple, list)) and len(kind) > 1 else str(kind)
    info = annot.info or {}
    colours = annot.colors or {}
    border = annot.border or {}
    opacity = annot.opacity
    oc_xref = 0
    try:
        oc_xref = int(annot.get_oc() or 0)
    except Exception:  # noqa: BLE001
        oc_xref = 0
    irt = _xref_ref(doc, annot.xref, "IRT")
    popup_xref, popup_contents = _popup(doc, annot)
    rect = annot.rect
    record: Dict[str, Any] = {
        "page": number,
        "index": index,
        "xref": int(annot.xref),
        "type": type_name,
        "name": info.get("name") or None,
        "subject": info.get("subject") or "",
        "title": info.get("title") or "",
        "contents": info.get("content") or "",
        "creation_date": info.get("creationDate") or None,
        "creation_date_iso": pdf_date_iso(info.get("creationDate")),
        "modification_date": info.get("modDate") or None,
        "modification_date_iso": pdf_date_iso(info.get("modDate")),
        "stroke_colour": colour_hex(colours.get("stroke")),
        "fill_colour": colour_hex(colours.get("fill")),
        "opacity": None if opacity is None or opacity < 0 else round(float(opacity), 3),
        "border_width": border.get("width"),
        "rect_raw": round_box((rect.x0, rect.y0, rect.x1, rect.y1)),
        "rect_view": round_box(view_box((rect.x0, rect.y0, rect.x1, rect.y1), matrix)),
        **_geometry(annot, type_name, matrix),
        "oc_xref": oc_xref or None,
        "oc_name": _oc_name(doc, oc_xref, ocgs),
        "flags": int(annot.flags),
        "popup_xref": popup_xref,
        "popup_contents": popup_contents,
        "irt_xref": irt,
        "reply_type": _xref_string(doc, annot.xref, "RT"),
        "default_appearance": _xref_string(doc, annot.xref, "DA") if type_name == "FreeText" else None,
        "annot_id": info.get("id") or None,
    }
    return record


def extract_annotations(doc: Any, page: Any, number: int, ocgs: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """All annotation rows of one page, in page order."""
    rows: List[Dict[str, Any]] = []
    for index, annot in enumerate(page.annots()):
        try:
            rows.append(annotation_record(doc, page, annot, number, index, ocgs))
        except Exception as exc:  # noqa: BLE001 - one broken annotation must not lose the page
            rows.append({"page": number, "index": index, "xref": int(getattr(annot, "xref", 0) or 0),
                         "type": "error", "error": f"{type(exc).__name__}: {exc}"})
    return rows


# --------------------------------------------------------------------------- #
# Tables (pdfplumber, text pages)
# --------------------------------------------------------------------------- #
def is_text_page(text_chars: int, vector_paths: Optional[int]) -> bool:
    """A page with a real text layer and few vector paths (tables are worth extracting)."""
    paths = FEW_VECTOR_PATHS if vector_paths is None else vector_paths
    return text_chars >= TEXT_PAGE_MIN_CHARS and paths < FEW_VECTOR_PATHS


def is_drawing_page(width: float, height: float, vector_paths: Optional[int]) -> bool:
    """A1 or larger with many vector paths (or a path count that timed out)."""
    many = vector_paths is None or vector_paths >= MANY_VECTOR_PATHS
    return is_a1_or_larger(width, height) and many


def _table_csv_text(rows: Sequence[Sequence[Any]]) -> str:
    """Render a table (list of rows) as CSV text."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for row in rows:
        writer.writerow(["" if cell is None else str(cell).replace("\n", " ") for cell in row])
    return buffer.getvalue()


def _cell_of(word: Dict[str, Any], cells: Sequence[Optional[Sequence[float]]]) -> Optional[int]:
    """Index of the cell whose box holds the centre of ``word``, or ``None``."""
    cx = (float(word["x0"]) + float(word["x1"])) / 2.0
    cy = (float(word["top"]) + float(word["bottom"])) / 2.0
    for index, cell in enumerate(cells):
        if cell is not None and cell[0] <= cx < cell[2] and cell[1] <= cy < cell[3]:
            return index
    return None


def table_rows_from_words(table: Any, words: Sequence[Dict[str, Any]]) -> List[List[str]]:
    """Fill a pdfplumber table from whole words so a cell edge never splits a token.

    ``table.extract()`` assigns characters to cells, which cuts words such as
    ``R0C0`` or ``PH-303`` in two when the text strategy derives a column edge
    from another line.  Here each word goes to the cell holding its centre.
    Rows without text and columns empty in every row are dropped.
    """
    rows: List[List[str]] = []
    for row in table.rows:
        cells = list(row.cells)
        buckets: List[List[Dict[str, Any]]] = [[] for _ in cells]
        x0, top, x1, bottom = row.bbox
        for word in words:
            cy = (float(word["top"]) + float(word["bottom"])) / 2.0
            if not (top <= cy < bottom):
                continue
            index = _cell_of(word, cells)
            if index is not None:
                buckets[index].append(word)
        rows.append([" ".join(w["text"] for w in sorted(bucket, key=lambda w: (round(float(w["top"])), float(w["x0"]))))
                     for bucket in buckets])
    rows = [row for row in rows if any(cell for cell in row)]
    if not rows:
        return rows
    width = max(len(row) for row in rows)
    keep = [c for c in range(width) if any(c < len(row) and row[c] for row in rows)]
    return [[row[c] if c < len(row) else "" for c in keep] for row in rows]


def _plumber_tables(pl_page: Any, strategy: str) -> List[Tuple[Box, List[List[Any]]]]:
    """Tables found by pdfplumber on one page with one strategy: ``[(bbox, rows), ...]``."""
    settings = {"vertical_strategy": strategy, "horizontal_strategy": strategy}
    found: List[Tuple[Box, List[List[Any]]]] = []
    words: Optional[List[Dict[str, Any]]] = None
    for table in pl_page.find_tables(table_settings=settings):
        if words is None:
            words = list(pl_page.extract_words(keep_blank_chars=False, use_text_flow=False))
        rows = table_rows_from_words(table, words)
        if not rows or len(rows) < MIN_TABLE_ROWS or max(len(r) for r in rows) < MIN_TABLE_COLS:
            continue
        found.append((tuple(round(float(v), 3) for v in table.bbox), rows))
    return found


def dominant_rotation(words: Iterable[Dict[str, Any]]) -> int:
    """The most common raw line rotation of a page's words (0 when the page has none)."""
    counts: Counter = Counter(int(w.get("rotation", 0)) % 360 for w in words)
    return counts.most_common(1)[0][0] if counts else 0


def unrotate_box(box: Sequence[float], rotation: int, width: float, height: float) -> Box:
    """Map a box seen on a page displayed with ``/Rotate rotation`` back to the un-rotated frame.

    ``width`` and ``height`` are the un-rotated page size; both frames have a
    top-left origin, so this is the inverse of ``page.rotation_matrix``.
    """
    rotation = int(rotation) % 360
    points: List[Point] = []
    for vx, vy in ((float(box[0]), float(box[1])), (float(box[2]), float(box[3]))):
        if rotation == 90:
            points.append((vy, height - vx))
        elif rotation == 180:
            points.append((width - vx, height - vy))
        elif rotation == 270:
            points.append((width - vy, vx))
        else:
            points.append((vx, vy))
    (ax, ay), (bx, by) = points
    return (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))


class _TablePage:
    """How one text page was handed to pdfplumber and how its boxes map back to the page view."""

    def __init__(self, src_page: Any, number: int, copy_rotation: int) -> None:
        mediabox = src_page.mediabox
        cropbox = src_page.cropbox
        self.number = number
        self.copy_rotation = int(copy_rotation) % 360
        self.source_rotation = int(src_page.rotation) % 360
        self.media_width = float(mediabox.width)
        self.media_height = float(mediabox.height)
        # pdfplumber measures from the MediaBox corner, PyMuPDF from the CropBox corner
        self.offset = (float(cropbox.x0) - float(mediabox.x0), float(cropbox.y0))
        self.matrix = src_page.rotation_matrix

    def to_page_view(self, bbox: Sequence[float]) -> Box:
        """A pdfplumber bbox on the copy page -> page-view box of the source page."""
        raw = unrotate_box(bbox, self.copy_rotation, self.media_width, self.media_height)
        shifted = (raw[0] - self.offset[0], raw[1] - self.offset[1], raw[2] - self.offset[0], raw[3] - self.offset[1])
        return view_box(shifted, self.matrix)


def _write_table_copy(pdf_path: Path, page_rotations: Mapping[int, Optional[int]], copy_path: Path) -> List[_TablePage]:
    """Copy the text pages into ``copy_path`` with the /Rotate that makes their text run horizontally.

    pdfplumber lays words out in the displayed frame, so a page whose text is
    drawn at 90 degrees to its ``/Rotate`` yields one glyph per cell.  Each copy
    page is rotated by the dominant raw text rotation of the source page instead
    (``None`` keeps the source rotation), and the returned records map the boxes
    back to the source page view.
    """
    import pymupdf

    pages: List[_TablePage] = []
    with pymupdf.open(str(pdf_path)) as doc, pymupdf.open() as copy:
        for number in sorted(page_rotations):
            src_page = doc[number - 1]
            rotation = page_rotations[number]
            rotation = int(src_page.rotation) if rotation is None else int(rotation)
            copy.insert_pdf(doc, from_page=number - 1, to_page=number - 1, links=False, annots=False)
            copy[-1].set_rotation(rotation % 360)
            pages.append(_TablePage(src_page, number, rotation))
        copy.save(str(copy_path))
    return pages


def _tables_for_page(pl_page: Any, record: _TablePage, out_dir: Path, budget_s: float,
                     notes: List[str]) -> List[Dict[str, Any]]:
    """Run both pdfplumber strategies on one copy page and write its ``tables_p<N>_<k>.csv`` files."""
    entries: List[Dict[str, Any]] = []
    number = record.number
    k = 0
    for strategy in ("lines", "text"):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                with census.time_budget(budget_s, f"tables_p{number}_{strategy}"):
                    tables = _plumber_tables(pl_page, strategy)
        except census.BudgetExceeded:
            notes.append(f"page {number}: pdfplumber {strategy} strategy exceeded {budget_s:g}s")
            continue
        except Exception as exc:  # noqa: BLE001 - pdfplumber parse failure on one page
            notes.append(f"page {number}: pdfplumber {strategy} failed: {type(exc).__name__}")
            continue
        for bbox, rows in tables:
            k += 1
            name = f"tables_p{number}_{k}.csv"
            atomic_write_text(out_dir / name, _table_csv_text(rows))
            entries.append({
                "page": number, "file": name, "bbox": round_box(record.to_page_view(bbox)),
                "bbox_coordinates": "page view (top-left origin)",
                "rows": len(rows), "cols": max(len(r) for r in rows), "strategy": strategy,
                "text_rotation": record.copy_rotation, "page_rotation": record.source_rotation,
            })
    return entries


def extract_tables(pdf_path: Path, text_pages: Union[Mapping[int, Optional[int]], Sequence[int]], out_dir: Path,
                   budget_s: float = DEFAULT_TABLE_BUDGET_S) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Run pdfplumber on the given 1-based text pages and write ``tables_p<N>_<k>.csv`` files.

    ``text_pages`` maps each page number to the raw rotation of its text (see
    :func:`dominant_rotation`); a plain sequence of page numbers keeps each
    page's own ``/Rotate``.  Table boxes are returned in the source page view.
    """
    import pdfplumber

    rotations: Dict[int, Optional[int]] = (dict(text_pages) if isinstance(text_pages, Mapping)
                                           else {int(n): None for n in text_pages})
    entries: List[Dict[str, Any]] = []
    notes: List[str] = []
    if not rotations:
        return entries, notes
    note = budget_note("tables")
    if note:
        notes.append(note)
    with tempfile.TemporaryDirectory(prefix="sidecar_pdf_tables_") as tmp:
        copy_path = Path(tmp) / "text_pages.pdf"
        records = _write_table_copy(pdf_path, rotations, copy_path)
        with pdfplumber.open(str(copy_path)) as pdf:
            for index, record in enumerate(records):
                entries.extend(_tables_for_page(pdf.pages[index], record, out_dir, budget_s, notes))
    return entries, notes


# --------------------------------------------------------------------------- #
# Title block
# --------------------------------------------------------------------------- #
def title_block_zone(width: float, height: float) -> Dict[str, float]:
    """Right-hand 18 % and bottom 12 % of the page view."""
    return {"x_min": width * (1.0 - TITLE_BLOCK_RIGHT_SHARE), "y_min": height * (1.0 - TITLE_BLOCK_BOTTOM_SHARE)}


def _in_zone(box: Sequence[float], zone: Dict[str, float]) -> bool:
    """True when the centre of a page-view box lies in the title-block zone."""
    cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
    return cx >= zone["x_min"] or cy >= zone["y_min"]


def title_block_strings(words: Sequence[Dict[str, Any]], width: float, height: float) -> List[Dict[str, Any]]:
    """Group the page's words in the title-block zone into line strings with page-view boxes."""
    zone = title_block_zone(width, height)
    lines: Dict[Tuple[int, int], List[Dict[str, Any]]] = defaultdict(list)
    for word in words:
        if _in_zone(word["bbox_view"], zone):
            lines[(word["block"], word["line"])].append(word)
    strings: List[Dict[str, Any]] = []
    for (block, line), items in sorted(lines.items()):
        items.sort(key=lambda w: w["word_no"])
        boxes = [w["bbox_view"] for w in items]
        strings.append({
            "block": block, "line": line,
            "text": " ".join(w["text"] for w in items),
            "rotation": items[0]["rotation"],
            "bbox_view": [min(b[0] for b in boxes), min(b[1] for b in boxes),
                          max(b[2] for b in boxes), max(b[3] for b in boxes)],
        })
    strings.sort(key=lambda s: (round(s["bbox_view"][1]), s["bbox_view"][0]))
    return strings


def _first_match(patterns: Sequence[re.Pattern[str]], texts: Sequence[str]) -> Optional[str]:
    """First regex hit across ``texts`` in pattern order."""
    for pattern in patterns:
        for text in texts:
            match = pattern.search(text)
            if match:
                return match.group(0)
    return None


def _has_boxes(strings: Sequence[Dict[str, Any]]) -> bool:
    """True when every string carries a page-view box (real runs; unit tests may pass text only)."""
    return bool(strings) and all(isinstance(s.get("bbox_view"), (list, tuple)) and len(s["bbox_view"]) == 4
                                 for s in strings)


def _label_hits(strings: Sequence[Dict[str, Any]], label: str) -> List[Tuple[int, str]]:
    """``(index, remainder on the same line)`` for every string containing ``label``, last occurrence first.

    The title block proper sits at the bottom right of a sheet, so it is the
    last place a label such as DATE or REV appears; the revision history table
    above it carries the same labels and is tried only afterwards.
    """
    pattern = re.compile(r"\b" + label + r"\b[\s:.\-]*(.*)$", re.IGNORECASE)
    hits: List[Tuple[int, str]] = []
    for pos, string in enumerate(strings):
        match = pattern.search(str(string.get("text", "")))
        if match:
            hits.append((pos, match.group(1).strip()))
    hits.reverse()
    return hits


def _proximity(label_box: Sequence[float], box: Sequence[float]) -> Optional[float]:
    """Distance score of a value box from its label: below and left-aligned is best; above is rejected."""
    dy = float(box[1]) - float(label_box[1])
    dx = abs(float(box[0]) - float(label_box[0]))
    if dy < -2.0:
        return None
    return max(dy, 0.0) + 0.5 * dx


def _candidates_near(strings: Sequence[Dict[str, Any]], pos: int, window: int = LABEL_WINDOW) -> List[Dict[str, Any]]:
    """Value strings near ``strings[pos]``: nearest below/right by geometry when boxes exist, else the following ones."""
    if not _has_boxes(strings):
        return list(strings[pos + 1:pos + 1 + window])
    label_box = strings[pos]["bbox_view"]
    scored: List[Tuple[float, int, Dict[str, Any]]] = []
    for index in range(max(0, pos - window // 2), min(len(strings), pos + 1 + 2 * window)):
        if index == pos:
            continue
        score = _proximity(label_box, strings[index]["bbox_view"])
        if score is not None:
            scored.append((score, index, strings[index]))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in scored]


def _value_near_label(strings: Sequence[Dict[str, Any]], label: str,
                      validate: Callable[[str], Optional[str]]) -> Optional[str]:
    """The first value that ``validate`` accepts on the label's own line, else nearest to the label."""
    for pos, rest in _label_hits(strings, label):
        if rest:
            value = validate(rest)
            if value:
                return value
        for candidate in _candidates_near(strings, pos):
            text = str(candidate.get("text", "")).strip()
            if not text or FIELD_LABEL_RE.match(text):
                continue
            value = validate(text)
            if value:
                return value
    return None


def _match_drawing_number(text: str) -> Optional[str]:
    """The drawing number inside ``text`` (first of the known code patterns), or ``None``."""
    return _first_match(DRAWING_NUMBER_RES, [text])


def _match_revision(text: str) -> Optional[str]:
    """``text`` (its first token) as a revision code such as A, B1, P02 or 3, upper-cased."""
    token = text.split()[0].rstrip(".,;:") if text.split() else ""
    return token.upper() if token and REVISION_TOKEN_RE.fullmatch(token) else None


def _match_date(text: str) -> Optional[str]:
    """The first date-like token in ``text``."""
    return _first_match(DATE_RES, [text])


def _match_scale(text: str) -> Optional[str]:
    """The first scale-like token in ``text``."""
    match = SCALE_RE.search(text)
    return match.group(0) if match else None


def _first_in_reading_order(texts: Sequence[str]) -> Optional[str]:
    """First drawing number (any known pattern) in reading order."""
    for text in texts:
        value = _match_drawing_number(text)
        if value:
            return value
    return None


def _drawing_number(strings: Sequence[Dict[str, Any]]) -> Optional[str]:
    """The sheet's own drawing number.

    The value next to a DRAWING NO / DWG NO / SHEET NO label wins; otherwise the
    first match in reading order among the strings that are not cross-references
    ("REFER AR-IP-31-ZZ-002 FOR ELEVATION"), and finally among all of them.
    """
    labelled = _value_near_label(strings, r"(?:DRAWING|DWG\.?|DRG\.?|SHEET)\s*(?:NO|NUMBER)", _match_drawing_number)
    if labelled:
        return labelled
    texts = [str(s.get("text", "")) for s in strings]
    plain = [t for t in texts if not CROSS_REFERENCE_RE.search(t)]
    return _first_in_reading_order(plain) or _first_in_reading_order(texts)


def _revision(strings: Sequence[Dict[str, Any]]) -> Optional[str]:
    """Revision code: the value next to a REV / REVISION label, else an inline ``Rev C`` anywhere."""
    labelled = _value_near_label(strings, r"REV(?:ISION)?", _match_revision)
    if labelled:
        return labelled
    for string in strings:
        match = REVISION_RE.search(str(string.get("text", "")))
        if match:
            return match.group(1).upper()
    return None


def _is_title_line(text: str) -> bool:
    """A wordy line that is not a label, date, scale, revision or drawing number."""
    if not text or FIELD_LABEL_RE.match(text):
        return False
    if _match_date(text) or _match_scale(text) or REVISION_RE.search(text) or _match_drawing_number(text):
        return False
    return any(ch.isalpha() for ch in text)


def _lines_below(strings: Sequence[Dict[str, Any]], pos: int, window: int = LABEL_WINDOW) -> List[Dict[str, Any]]:
    """Strings left-aligned under ``strings[pos]`` in top-down order (reading order when boxes are unknown)."""
    if not _has_boxes(strings):
        return list(strings[pos + 1:pos + 1 + window])
    label_box = strings[pos]["bbox_view"]
    below: List[Tuple[float, int, Dict[str, Any]]] = []
    for index in range(max(0, pos - window // 2), min(len(strings), pos + 1 + 2 * window)):
        box = strings[index]["bbox_view"]
        dy = float(box[1]) - float(label_box[1])
        if index != pos and dy > 2.0 and abs(float(box[0]) - float(label_box[0])) <= ALIGN_TOLERANCE_PT:
            below.append((dy, index, strings[index]))
    below.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in below]


def _title_after_label(strings: Sequence[Dict[str, Any]]) -> Optional[str]:
    """Title lines under a TITLE / DRAWING TITLE label, joined (continuations need two or more words)."""
    for pos, rest in _label_hits(strings, r"(?:DRAWING\s+|DWG\.?\s*|DRG\.?\s*)?TITLE"):
        lines = [rest] if rest and _is_title_line(rest) else []
        for string in _lines_below(strings, pos)[:TITLE_MAX_LINES]:
            text = str(string.get("text", "")).strip()
            if not _is_title_line(text) or (lines and len(text.split()) < 2):
                break
            lines.append(text)
        if lines:
            return " ".join(lines)
    return None


def _guess_title(strings: Sequence[Dict[str, Any]], taken: Sequence[Optional[str]]) -> Optional[str]:
    """Best-effort title: the lines after a TITLE label, else the longest wordy line not used elsewhere."""
    labelled = _title_after_label(strings)
    if labelled:
        return labelled
    used = {t for t in taken if t}
    candidates = []
    for string in strings:
        text = str(string.get("text", "")).strip()
        if not _is_title_line(text) or any(u in text for u in used):
            continue
        words = [w for w in text.split() if any(ch.isalpha() for ch in w)]
        if len(words) >= 3:
            candidates.append(text)
    return max(candidates, key=len) if candidates else None


def parse_title_block(strings: Sequence[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    """Best-effort drawing number, title, revision, date and scale from title-block strings.

    Each field prefers the value next to its label (same line, else the nearest
    string below or to the right when boxes are known, else the following
    strings); the last occurrence of a label is tried first because the title
    block proper is the last thing in reading order.  Without a label the field
    falls back to the first pattern match in reading order.
    """
    texts = [str(s.get("text", "")) for s in strings]
    number = _drawing_number(strings)
    revision = _revision(strings)
    date = _value_near_label(strings, "DATE", _match_date) or _first_match(DATE_RES, texts)
    scale_match = _value_near_label(strings, "SCALE", _match_scale) or _first_match((SCALE_RE,), texts)
    scale = re.sub(r"\s+", "", scale_match) if scale_match and ":" in scale_match else scale_match
    title = _guess_title(strings, (number, date, scale_match))
    return {"drawing_number": number, "title": title, "revision": revision, "date": date, "scale": scale}


# --------------------------------------------------------------------------- #
# Page info
# --------------------------------------------------------------------------- #
def _ocg_list(doc: Any) -> Tuple[Dict[int, Dict[str, Any]], List[Dict[str, Any]]]:
    """Document OCGs as ``{xref: info}`` and as a JSON-ready list."""
    try:
        raw = doc.get_ocgs() or {}
    except Exception:  # noqa: BLE001 - damaged OC tree
        raw = {}
    ocgs = {int(k): dict(v) for k, v in raw.items()}
    listed = [
        {"xref": xref, "name": str(v.get("name", "")), "on": bool(v.get("on", True)),
         "intent": list(v.get("intent") or []), "usage": v.get("usage")}
        for xref, v in sorted(ocgs.items())
    ]
    return ocgs, listed


def page_info(page: Any, number: int, words: Sequence[Dict[str, Any]], images: Sequence[Dict[str, Any]],
              drawings: Optional[Sequence[Dict[str, Any]]], path_note: Optional[str],
              annotation_count: int) -> Dict[str, Any]:
    """One ``page_info.json`` page record."""
    rect = page.rect
    width, height = float(rect.width), float(rect.height)
    raw = page.mediabox
    text_chars = len(page.get_text("text"))
    boxes = [tuple(img["bbox_view"]) for img in images]
    coverage = census.coverage_pct(boxes, width, height)
    paths = None if drawings is None else len(drawings)
    notes = [path_note] if path_note else []
    return {
        "page": number,
        "width_pt": round(width, 2),
        "height_pt": round(height, 2),
        "raw_width_pt": round(float(raw.width), 2),
        "raw_height_pt": round(float(raw.height), 2),
        "rotation": int(page.rotation),
        "rotation_matrix": [round(float(v), 6) for v in tuple(page.rotation_matrix)],
        "text_chars": text_chars,
        "words": len(words),
        "dominant_text_rotation": dominant_rotation(words),
        "dominant_text_rotation_view": (dominant_rotation(words) - int(page.rotation)) % 360,
        "image_count": len(images),
        "image_coverage_pct": coverage,
        "vector_path_count": -1 if paths is None else paths,
        "annotation_count": annotation_count,
        "page_class": census.classify_pdf_page(-1 if paths is None else paths, text_chars, coverage),
        "is_text_page": is_text_page(text_chars, paths),
        "is_drawing_page": is_drawing_page(width, height, paths),
        "a1_or_larger": is_a1_or_larger(width, height),
        "notes": notes,
    }


# --------------------------------------------------------------------------- #
# Ledger lookup and idempotency
# --------------------------------------------------------------------------- #
def ledger_entry(pdf_path: Path, sha256: str, state_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Find the pull/upload ledger row for this file (by sha256, else by local path)."""
    state = Path(state_dir) if state_dir else PATHS["state"]
    rows: List[Dict[str, Any]] = []
    for name in ("pull_ledger.jsonl", "upload_ledger.jsonl"):
        rows.extend(r for r in read_jsonl(state / name) if isinstance(r, dict))
    for row in rows:
        if row.get("sha256") == sha256:
            return row
    try:
        rel = pdf_path.resolve().relative_to(project_root()).as_posix()
    except (ValueError, FileNotFoundError):
        rel = pdf_path.as_posix()
    for row in rows:
        if row.get("local_path") == rel or row.get("rel_path") == rel:
            return row
    return {}


def already_done(out_dir: Path, sha256: str, max_pages: Optional[int]) -> bool:
    """True when the manifest in ``out_dir`` was written for this sha256 and every listed file exists."""
    manifest_path = out_dir / "sidecar_manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if manifest.get("sha256") != sha256 or manifest.get("pipeline_version") != PIPELINE_VERSION:
        return False
    if manifest.get("max_pages") != max_pages:
        return False
    return all((out_dir / name).is_file() for name in manifest.get("files", {}))


def _converter_versions() -> List[Dict[str, str]]:
    """Name and version of every library used."""
    import pdfplumber
    import pymupdf

    return [
        {"name": "pymupdf", "version": str(pymupdf.VersionBind)},
        {"name": "mupdf", "version": str(pymupdf.VersionFitz)},
        {"name": "pdfplumber", "version": str(getattr(pdfplumber, "__version__", "unknown"))},
    ]


# --------------------------------------------------------------------------- #
# Main extraction
# --------------------------------------------------------------------------- #
class SidecarResult(dict):
    """The manifest dictionary returned by :func:`build_sidecars` (a plain dict with a type name)."""


def _write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    """Write all rows at once (atomically), replacing any earlier file."""
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    atomic_write_text(path, text)


def _file_entry(path: Path) -> Dict[str, Any]:
    """Size and sha256 of a written file for the manifest."""
    return {"bytes": path.stat().st_size, "sha256": sha256_file(path)}


def _clear_old_tables(out_dir: Path) -> None:
    """Remove table CSVs from an earlier run so a re-run never leaves stale files."""
    for old in out_dir.glob("tables_p*_*.csv"):
        old.unlink()


def _process_page(doc: Any, page: Any, number: int, ocgs: Dict[int, Dict[str, Any]],
                  path_budget_s: float) -> Dict[str, Any]:
    """Extract everything PyMuPDF gives for one page."""
    words = extract_words(page, number)
    blocks = extract_blocks(page, number)
    images = extract_images(page, number)
    drawings, path_note = collect_drawings(page, path_budget_s)
    annotations = extract_annotations(doc, page, number, ocgs)
    info = page_info(page, number, words, images, drawings, path_note, len(annotations))
    title_block = None
    if info["is_drawing_page"]:
        strings = title_block_strings(words, info["width_pt"], info["height_pt"])
        title_block = {"page": number, "zone": title_block_zone(info["width_pt"], info["height_pt"]),
                       "strings": strings, "parsed": parse_title_block(strings)}
    return {
        "words": words, "blocks": blocks, "images": images, "annotations": annotations,
        "fills": fill_histogram(drawings, number), "info": info, "notes": notes_blocks(blocks),
        "title_block": title_block,
    }


def build_sidecars(pdf_path: Path, out_dir: Path, max_pages: Optional[int] = None,
                   path_budget_s: float = DEFAULT_PATH_BUDGET_S, force: bool = False,
                   state_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Write every sidecar for ``pdf_path`` into ``out_dir`` and return the manifest.

    Returns the existing manifest (with ``"skipped": True``) when the output is
    already up to date for the file's sha256 and ``force`` is false.
    """
    import pymupdf

    pdf_path = Path(pdf_path)
    out_dir = ensure_dir(out_dir)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"no such PDF: {pdf_path}")
    sha256 = sha256_file(pdf_path)
    manifest_path = out_dir / "sidecar_manifest.json"
    if not force and already_done(out_dir, sha256, max_pages):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["skipped"] = True
        return manifest

    _clear_old_tables(out_dir)
    pages: List[Dict[str, Any]] = []
    with pymupdf.open(str(pdf_path)) as doc:
        if doc.needs_pass:
            raise RuntimeError(f"encrypted PDF (password required): {pdf_path}")
        ocgs, ocg_list = _ocg_list(doc)
        total = doc.page_count
        limit = total if max_pages is None else min(total, max(0, int(max_pages)))
        for index in range(limit):
            pages.append(_process_page(doc, doc[index], index + 1, ocgs, path_budget_s))

    text_pages = {p["info"]["page"]: p["info"]["dominant_text_rotation"] for p in pages if p["info"]["is_text_page"]}
    tables, table_notes = extract_tables(pdf_path, text_pages, out_dir)
    notes = [n for p in pages for n in p["notes"]]
    ledger = ledger_entry(pdf_path, sha256, state_dir)

    _write_jsonl(out_dir / "words.jsonl", (w for p in pages for w in p["words"]))
    write_csv_dicts(out_dir / "blocks.csv", (b for p in pages for b in p["blocks"]), BLOCK_COLUMNS)
    _write_jsonl(out_dir / "annotations.jsonl", (a for p in pages for a in p["annotations"]))
    atomic_write_json(out_dir / "images.json", [img for p in pages for img in p["images"]])
    atomic_write_json(out_dir / "fills.json", [p["fills"] for p in pages])
    atomic_write_json(out_dir / "tables.json", tables)
    atomic_write_json(out_dir / "titleblock.json", [p["title_block"] for p in pages if p["title_block"]])
    atomic_write_text(out_dir / "notes.md", render_notes_md(notes, pdf_path.name))
    atomic_write_json(out_dir / "page_info.json", {
        "source_path": str(pdf_path), "sha256": sha256, "page_count": total, "pages_processed": limit,
        "ocgs": ocg_list, "pages": [p["info"] for p in pages],
    })

    files = sorted(["words.jsonl", "blocks.csv", "annotations.jsonl", "images.json", "fills.json",
                    "tables.json", "titleblock.json", "notes.md", "page_info.json"] + [t["file"] for t in tables])
    manifest = SidecarResult({
        "module": MODULE_NAME,
        "pipeline_version": PIPELINE_VERSION,
        "generated_at": utc_now_iso(),
        "source_path": str(pdf_path),
        "rel_path": ledger.get("rel_path"),
        "drive_id": ledger.get("drive_id"),
        "sha256": sha256,
        "bytes": pdf_path.stat().st_size,
        "max_pages": max_pages,
        "page_count": total,
        "pages_processed": limit,
        "converters": _converter_versions(),
        "methods_used": ["pymupdf-text", "pymupdf-annots", "pymupdf-drawings", "pymupdf-images"]
                        + (["pdfplumber-tables"] if text_pages else []),
        "counts": {
            "words": sum(len(p["words"]) for p in pages),
            "blocks": sum(len(p["blocks"]) for p in pages),
            "annotations": sum(len(p["annotations"]) for p in pages),
            "images": sum(len(p["images"]) for p in pages),
            "tables": len(tables),
            "notes_blocks": len(notes),
            "drawing_pages": sum(1 for p in pages if p["info"]["is_drawing_page"]),
            "text_pages": len(text_pages),
            "ocgs": len(ocg_list),
            "numeric_tokens": len(numeric_tokens(w for p in pages for w in p["words"])),
        },
        "notes": table_notes + [f"page {p['info']['page']}: {n}" for p in pages for n in p["info"]["notes"]],
        "files": {name: _file_entry(out_dir / name) for name in files},
        "skipped": False,
    })
    atomic_write_json(manifest_path, manifest)
    return manifest


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """Argument parser: ``run --pdf <path> --out <dir> [--max-pages N] [--force]``."""
    parser = argparse.ArgumentParser(description="Write per-page PyMuPDF/pdfplumber sidecars for one PDF.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="extract sidecars for one PDF")
    run.add_argument("--pdf", required=True, type=Path, help="source PDF (read only)")
    run.add_argument("--out", required=True, type=Path, help="output directory for the sidecar files")
    run.add_argument("--max-pages", type=int, default=None, help="process only the first N pages")
    run.add_argument("--path-budget", type=float, default=DEFAULT_PATH_BUDGET_S,
                     help="seconds allowed for get_cdrawings per page (default 20)")
    run.add_argument("--force", action="store_true", help="rewrite even when the manifest is up to date")
    run.add_argument("--progress", type=Path, default=None,
                     help="progress.json to record the result in (default: state/progress.json)")
    run.add_argument("--no-progress", action="store_true", help="do not touch progress.json")
    return parser


def _record_progress(args: argparse.Namespace, manifest: Dict[str, Any]) -> None:
    """Mark ``sidecar_pdf:<sha256>`` done in progress.json unless disabled."""
    if args.no_progress:
        return
    progress = Progress(args.progress)
    progress.set(f"{MODULE_NAME}:{manifest['sha256']}", "done", hash=manifest["sha256"],
                 out_dir=str(args.out), rel_path=manifest.get("rel_path"))


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point; returns a process exit code (1 on failure)."""
    args = build_parser().parse_args(argv)
    try:
        manifest = build_sidecars(args.pdf, args.out, max_pages=args.max_pages,
                                  path_budget_s=args.path_budget, force=args.force)
        _record_progress(args, manifest)
    except Exception as exc:  # noqa: BLE001 - surface any failure as a non-zero exit
        print(f"error: {exc}", file=sys.stderr)
        return 1
    state = "up to date, skipped" if manifest.get("skipped") else "written"
    counts = manifest.get("counts", {})
    print(f"{MODULE_NAME}: {args.pdf.name} -> {args.out} ({state}; pages {manifest.get('pages_processed')}, "
          f"words {counts.get('words')}, annotations {counts.get('annotations')}, tables {counts.get('tables')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
