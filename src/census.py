"""Page census of every pulled PDF and PPTX, by script only (no model).

For each file in ``state/pull_ledger.jsonl`` with extension ``pdf`` or ``pptx``
(or any file named with ``--paths``) the census records one row per page or
slide in ``out/registers/page_census.csv`` and one row per file in
``out/registers/file_census.csv``.  The per-page measurements drive the later
choice of extraction route (text layer, vector drawing, scan) so they must be
cheap, deterministic and resumable:

* a file whose ``sha256`` already has a row in ``file_census.csv`` is skipped,
* files are processed in a ``multiprocessing.Pool`` with a per-file time budget
  (default 600 s; a timeout is recorded as an error and the run continues),
* ``page.get_cdrawings()`` has its own per-page budget (default 20 s); when it
  is exceeded ``vector_paths`` is ``-1`` and the note ``path_count_timeout`` is
  added,
* one progress line per file goes to stderr and to ``logs/census.log``.

Page classes (PDF)::

    vector_drawing  vector_paths >= 500 or vector_paths == -1
    raster_or_scan  image_coverage_pct >= 60 and text_chars < 50
    text            text_chars >= 200 and vector_paths < 500 and image_coverage_pct < 30
    mixed           otherwise

Page classes (PPTX slides)::

    raster_or_scan  one picture covers >= 60 % of the slide and text_chars < 50
    text            text_chars >= 200 and picture coverage < 30 %
    mixed           otherwise
"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import re
import signal
import sys
import time
import traceback
import zipfile
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

try:  # package import (python -m src.census, tests)
    from src.common import (
        PATHS,
        ensure_dir,
        file_ext,
        parse_department,
        project_root,
        read_csv_dicts,
        read_jsonl,
        sha256_file,
        utc_now_iso,
        write_csv_dicts,
    )
    from src.state import Progress, file_lock
except ModuleNotFoundError:  # run as a plain script from inside src/
    from common import (  # type: ignore[no-redef]
        PATHS,
        ensure_dir,
        file_ext,
        parse_department,
        project_root,
        read_csv_dicts,
        read_jsonl,
        sha256_file,
        utc_now_iso,
        write_csv_dicts,
    )
    from state import Progress, file_lock  # type: ignore[no-redef]

PAGE_COLUMNS: Tuple[str, ...] = (
    "rel_path", "sha256", "kind", "page", "width_pt", "height_pt", "rotation", "text_chars", "words",
    "vector_paths", "image_count", "image_coverage_pct", "annotation_count", "annotation_types",
    "layer_count", "page_class", "notes",
)
FILE_COLUMNS: Tuple[str, ...] = (
    "rel_path", "sha256", "kind", "pages", "text_pages", "vector_pages", "raster_pages", "mixed_pages",
    "total_annotations", "seconds", "error",
)
SUMMARY_COLUMNS: Tuple[str, ...] = (
    "root", "files", "errors", "pages", "text", "vector_drawing", "raster_or_scan", "mixed", "total_annotations",
)
CENSUS_KINDS: Tuple[str, ...] = ("pdf", "pptx")

DEFAULT_FILE_TIMEOUT_S: float = 600.0
DEFAULT_PAGE_BUDGET_S: float = 20.0
EMU_PER_PT: float = 12700.0

VECTOR_THRESHOLD = 500
RASTER_COVERAGE_PCT = 60.0
RASTER_MAX_TEXT = 50
TEXT_MIN_CHARS = 200
TEXT_MAX_COVERAGE_PCT = 30.0
OCR_LIKE_SINGLE_IMAGE_PCT = 90.0

_MODERN_COMMENT_RE = re.compile(r"^ppt/comments/modernComment_.*\.xml$", re.IGNORECASE)
_CLASSIC_COMMENT_RE = re.compile(r"^ppt/comments/comment\d*\.xml$", re.IGNORECASE)
_SLIDE_RELS_RE = re.compile(r"^ppt/slides/_rels/slide(\d+)\.xml\.rels$", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Time budgets (SIGALRM based, nestable, main thread only)
# --------------------------------------------------------------------------- #
class BudgetExceeded(Exception):
    """Raised inside a :func:`time_budget` block when its deadline passes."""

    def __init__(self, deadline: float, label: str = "") -> None:
        super().__init__(f"time budget exceeded ({label or 'unnamed'})")
        self.deadline = deadline
        self.label = label


_DEADLINES: List[Tuple[float, str]] = []


def _earliest_deadline() -> Optional[Tuple[float, str]]:
    """The soonest active deadline, or ``None`` when no budget is active."""
    return min(_DEADLINES, key=lambda item: item[0]) if _DEADLINES else None


def _on_alarm(signum: int, frame: Any) -> None:  # noqa: ARG001 - signal handler signature
    """Signal handler: raise for the earliest active deadline."""
    earliest = _earliest_deadline()
    if earliest is not None:
        raise BudgetExceeded(earliest[0], earliest[1])


def _arm_alarm() -> None:
    """(Re)programme the interval timer for the earliest active deadline."""
    earliest = _earliest_deadline()
    if earliest is None:
        signal.setitimer(signal.ITIMER_REAL, 0)
        return
    remaining = max(earliest[0] - time.monotonic(), 0.001)
    signal.signal(signal.SIGALRM, _on_alarm)
    signal.setitimer(signal.ITIMER_REAL, remaining)


@contextmanager
def time_budget(seconds: float, label: str = "") -> Iterator[None]:
    """Run the block under a wall-clock budget; raise :class:`BudgetExceeded` when it passes.

    Budgets nest: the alarm always fires for the earliest active deadline and
    the exception carries that deadline, so an inner block re-raises an outer
    block's timeout untouched.  Uses ``SIGALRM`` and therefore only works in the
    main thread of a process (which is where pool workers run their tasks).
    """
    deadline = time.monotonic() + max(float(seconds), 0.001)
    entry = (deadline, label)
    _DEADLINES.append(entry)
    _arm_alarm()
    try:
        yield
    finally:
        _DEADLINES.remove(entry)
        _arm_alarm()


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #
def coverage_pct(boxes: Iterable[Tuple[float, float, float, float]], width: float, height: float) -> float:
    """Percentage of a ``width`` x ``height`` page covered by the union of ``boxes`` (clipped to the page)."""
    from shapely.geometry import box
    from shapely.ops import unary_union

    page_area = float(width) * float(height)
    if page_area <= 0:
        return 0.0
    page = box(0.0, 0.0, float(width), float(height))
    shapes = []
    for x0, y0, x1, y1 in boxes:
        lo_x, hi_x = sorted((float(x0), float(x1)))
        lo_y, hi_y = sorted((float(y0), float(y1)))
        if hi_x > lo_x and hi_y > lo_y:
            shapes.append(box(lo_x, lo_y, hi_x, hi_y))
    if not shapes:
        return 0.0
    covered = unary_union(shapes).intersection(page)
    return round(100.0 * covered.area / page_area, 2)


def single_box_pct(bbox: Tuple[float, float, float, float], width: float, height: float) -> float:
    """Coverage of one box alone (see :func:`coverage_pct`)."""
    return coverage_pct([bbox], width, height)


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def classify_pdf_page(vector_paths: int, text_chars: int, image_coverage_pct: float) -> str:
    """Apply the PDF page-class rules in order."""
    if vector_paths == -1 or vector_paths >= VECTOR_THRESHOLD:
        return "vector_drawing"
    if image_coverage_pct >= RASTER_COVERAGE_PCT and text_chars < RASTER_MAX_TEXT:
        return "raster_or_scan"
    if text_chars >= TEXT_MIN_CHARS and vector_paths < VECTOR_THRESHOLD and image_coverage_pct < TEXT_MAX_COVERAGE_PCT:
        return "text"
    return "mixed"


def classify_slide(text_chars: int, picture_coverage_pct: float, largest_picture_pct: float) -> str:
    """Apply the PPTX slide-class rules in order."""
    if largest_picture_pct >= RASTER_COVERAGE_PCT and text_chars < RASTER_MAX_TEXT:
        return "raster_or_scan"
    if text_chars >= TEXT_MIN_CHARS and picture_coverage_pct < TEXT_MAX_COVERAGE_PCT:
        return "text"
    return "mixed"


def _format_types(counter: Counter) -> str:
    """``type=count`` pairs joined by semicolons, sorted by type name."""
    return ";".join(f"{name}={count}" for name, count in sorted(counter.items()))


# --------------------------------------------------------------------------- #
# PDF census
# --------------------------------------------------------------------------- #
def _count_paths(page: Any, budget_s: float) -> Tuple[int, Optional[str]]:
    """``len(page.get_cdrawings())`` under ``budget_s``; ``(-1, 'path_count_timeout')`` on timeout."""
    try:
        with time_budget(budget_s, "page_paths"):
            return len(page.get_cdrawings()), None
    except BudgetExceeded as exc:
        if exc.label != "page_paths":
            raise
        return -1, "path_count_timeout"


def _image_boxes(page: Any) -> List[Tuple[float, float, float, float]]:
    """Bounding boxes of every image occurrence on the page."""
    boxes: List[Tuple[float, float, float, float]] = []
    for info in page.get_image_info(xrefs=False):
        bbox = info.get("bbox")
        if bbox and len(bbox) == 4:
            boxes.append(tuple(float(v) for v in bbox))  # type: ignore[arg-type]
    return boxes


def _annotations(page: Any) -> Tuple[int, Counter]:
    """Count the page's annotations by type name."""
    counter: Counter = Counter()
    for annot in page.annots():
        kind = annot.type
        name = kind[1] if isinstance(kind, (tuple, list)) and len(kind) > 1 else str(kind)
        counter[str(name)] += 1
    return sum(counter.values()), counter


def census_pdf_page(page: Any, number: int, layer_count: int, page_budget_s: float) -> Dict[str, Any]:
    """Measure one PDF page and return a page-census row (without rel_path/sha256/kind)."""
    rect = page.rect
    width, height = float(rect.width), float(rect.height)
    text = page.get_text("text")
    text_chars = len(text)
    words = len(page.get_text("words"))
    vector_paths, path_note = _count_paths(page, page_budget_s)
    boxes = _image_boxes(page)
    image_pct = coverage_pct(boxes, width, height)
    largest = max((single_box_pct(b, width, height) for b in boxes), default=0.0)
    annot_count, annot_types = _annotations(page)
    notes: List[str] = []
    if path_note:
        notes.append(path_note)
    if text_chars >= RASTER_MAX_TEXT and largest >= OCR_LIKE_SINGLE_IMAGE_PCT:
        notes.append("ocr_like_text")
    if not text.strip() and not boxes and vector_paths == 0:
        notes.append("blank_page")
    return {
        "page": number,
        "width_pt": round(width, 2),
        "height_pt": round(height, 2),
        "rotation": int(page.rotation),
        "text_chars": text_chars,
        "words": words,
        "vector_paths": vector_paths,
        "image_count": len(boxes),
        "image_coverage_pct": image_pct,
        "annotation_count": annot_count,
        "annotation_types": _format_types(annot_types),
        "layer_count": layer_count,
        "page_class": classify_pdf_page(vector_paths, text_chars, image_pct),
        "notes": ";".join(notes),
    }


def census_pdf(path: Path, page_budget_s: float = DEFAULT_PAGE_BUDGET_S) -> List[Dict[str, Any]]:
    """Per-page rows for a PDF file."""
    import pymupdf

    rows: List[Dict[str, Any]] = []
    with pymupdf.open(str(path)) as doc:
        if doc.needs_pass:
            raise RuntimeError("encrypted PDF (password required)")
        try:
            layer_count = len(doc.get_ocgs())
        except Exception:  # noqa: BLE001 - non-PDF or damaged OC tree
            layer_count = 0
        for index, page in enumerate(doc, start=1):
            rows.append(census_pdf_page(page, index, layer_count, page_budget_s))
    return rows


# --------------------------------------------------------------------------- #
# PPTX census
# --------------------------------------------------------------------------- #
def _count_comment_elements(xml_bytes: bytes) -> int:
    """Number of comment elements (``cm``) in a classic or modern comments part."""
    from lxml import etree

    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError:
        return 0
    return sum(1 for el in root.iter() if isinstance(el.tag, str) and etree.QName(el).localname == "cm")


def _slide_comment_parts(archive: zipfile.ZipFile) -> Dict[int, List[str]]:
    """Map 1-based slide number -> comment part names, read from each slide's relationships."""
    from lxml import etree

    mapping: Dict[int, List[str]] = defaultdict(list)
    for name in archive.namelist():
        match = _SLIDE_RELS_RE.match(name)
        if not match:
            continue
        try:
            root = etree.fromstring(archive.read(name))
        except etree.XMLSyntaxError:
            continue
        for rel in root.iter():
            target = (rel.get("Target") or "").replace("\\", "/")
            if "comments/" in target:
                mapping[int(match.group(1))].append(_resolve_part(target))
    return mapping


def _resolve_part(target: str) -> str:
    """Resolve a slide relationship target (relative to ``ppt/slides``, or package-absolute) to a part name."""
    if target.startswith("/"):
        return target.lstrip("/")
    return os.path.normpath(os.path.join("ppt/slides", target)).replace("\\", "/")


def pptx_comment_counts(path: Path) -> Tuple[Dict[int, int], int]:
    """Comment counts per slide (by slide number) and the total across the package.

    Reads ``ppt/comments/comment*.xml`` and ``ppt/comments/modernComment_*.xml``
    straight from the zip.  Parts that no slide relationship points at are
    counted in the total only.
    """
    per_slide: Dict[int, int] = defaultdict(int)
    total = 0
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        comment_parts = [n for n in names if _CLASSIC_COMMENT_RE.match(n) or _MODERN_COMMENT_RE.match(n)]
        counts = {n: _count_comment_elements(archive.read(n)) for n in comment_parts}
        total = sum(counts.values())
        for slide_no, parts in _slide_comment_parts(archive).items():
            for part in parts:
                if part in counts:
                    per_slide[slide_no] += counts[part]
    return dict(per_slide), total


def _iter_shapes(shapes: Any) -> Iterator[Any]:
    """Yield every shape, descending into groups."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _iter_shapes(shape.shapes)
        else:
            yield shape


def _shape_text(shape: Any) -> str:
    """All text in a shape: text frame and, for tables, every cell."""
    parts: List[str] = []
    if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
        parts.append(shape.text_frame.text)
    if getattr(shape, "has_table", False) and shape.has_table:
        for row in shape.table.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


def _shape_box_pt(shape: Any) -> Optional[Tuple[float, float, float, float]]:
    """Bounding box of a shape in points, or ``None`` when it has no position."""
    if shape.left is None or shape.top is None or shape.width is None or shape.height is None:
        return None
    x0, y0 = shape.left / EMU_PER_PT, shape.top / EMU_PER_PT
    return (x0, y0, x0 + shape.width / EMU_PER_PT, y0 + shape.height / EMU_PER_PT)


def census_slide(slide: Any, number: int, width: float, height: float, comments: int) -> Dict[str, Any]:
    """Measure one slide and return a page-census row (without rel_path/sha256/kind)."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER

    text_parts: List[str] = []
    picture_boxes: List[Tuple[float, float, float, float]] = []
    tables = charts = drawn = 0
    for shape in _iter_shapes(slide.shapes):
        text_parts.append(_shape_text(shape))
        kind = shape.shape_type
        is_picture = kind in (MSO_SHAPE_TYPE.PICTURE, MSO_SHAPE_TYPE.LINKED_PICTURE)
        if not is_picture and kind == MSO_SHAPE_TYPE.PLACEHOLDER:
            is_picture = getattr(shape, "placeholder_format", None) is not None and \
                shape.placeholder_format.type == PP_PLACEHOLDER.PICTURE and hasattr(shape, "image")
        if is_picture:
            bbox = _shape_box_pt(shape)
            if bbox:
                picture_boxes.append(bbox)
        elif getattr(shape, "has_table", False) and shape.has_table:
            tables += 1
        elif getattr(shape, "has_chart", False) and shape.has_chart:
            charts += 1
        elif kind in (MSO_SHAPE_TYPE.AUTO_SHAPE, MSO_SHAPE_TYPE.FREEFORM, MSO_SHAPE_TYPE.LINE):
            drawn += 1
    text = "\n".join(p for p in text_parts if p)
    text_chars = len(text)
    words = len(text.split())
    picture_pct = coverage_pct(picture_boxes, width, height)
    largest = max((single_box_pct(b, width, height) for b in picture_boxes), default=0.0)
    notes_chars = len(slide.notes_slide.notes_text_frame.text) if slide.has_notes_slide and \
        slide.notes_slide.notes_text_frame is not None else 0
    return {
        "page": number,
        "width_pt": round(width, 2),
        "height_pt": round(height, 2),
        "rotation": 0,
        "text_chars": text_chars,
        "words": words,
        "vector_paths": drawn,
        "image_count": len(picture_boxes),
        "image_coverage_pct": picture_pct,
        "annotation_count": comments,
        "annotation_types": f"comment={comments}" if comments else "",
        "layer_count": 0,
        "page_class": classify_slide(text_chars, picture_pct, largest),
        "notes": f"tables={tables};charts={charts};notes_chars={notes_chars}",
    }


def census_pptx(path: Path) -> List[Dict[str, Any]]:
    """Per-slide rows for a PPTX file."""
    from pptx import Presentation

    per_slide, _total = pptx_comment_counts(path)
    prs = Presentation(str(path))
    width = (prs.slide_width or 0) / EMU_PER_PT
    height = (prs.slide_height or 0) / EMU_PER_PT
    return [census_slide(slide, index, width, height, per_slide.get(index, 0))
            for index, slide in enumerate(prs.slides, start=1)]


# --------------------------------------------------------------------------- #
# Per-file worker
# --------------------------------------------------------------------------- #
def _file_row(rel_path: str, sha256: str, kind: str, pages: List[Dict[str, Any]], seconds: float,
              error: str) -> Dict[str, Any]:
    """Aggregate page rows into one file-census row."""
    classes = Counter(row["page_class"] for row in pages)
    return {
        "rel_path": rel_path,
        "sha256": sha256,
        "kind": kind,
        "pages": len(pages),
        "text_pages": classes.get("text", 0),
        "vector_pages": classes.get("vector_drawing", 0),
        "raster_pages": classes.get("raster_or_scan", 0),
        "mixed_pages": classes.get("mixed", 0),
        "total_annotations": sum(int(row["annotation_count"]) for row in pages),
        "seconds": round(seconds, 2),
        "error": error,
    }


def census_file(task: Dict[str, Any]) -> Dict[str, Any]:
    """Census one file; always returns ``{"file": row, "pages": rows}`` (errors go in the row).

    ``task`` holds ``rel_path``, ``sha256``, ``kind``, ``local_path``,
    ``file_timeout_s`` and ``page_budget_s``.  Runs in a pool worker.
    """
    started = time.monotonic()
    rel_path, sha256, kind = task["rel_path"], task["sha256"], task["kind"]
    pages: List[Dict[str, Any]] = []
    error = ""
    try:
        with time_budget(float(task.get("file_timeout_s", DEFAULT_FILE_TIMEOUT_S)), "file"):
            if kind == "pdf":
                pages = census_pdf(Path(task["local_path"]), float(task.get("page_budget_s", DEFAULT_PAGE_BUDGET_S)))
            elif kind == "pptx":
                pages = census_pptx(Path(task["local_path"]))
            else:
                raise ValueError(f"unsupported kind '{kind}'")
    except BudgetExceeded:
        pages = []
        error = f"timeout after {task.get('file_timeout_s', DEFAULT_FILE_TIMEOUT_S)} s"
    except Exception as exc:  # noqa: BLE001 - every failure is recorded, never raised
        pages = []
        error = f"{type(exc).__name__}: {exc}"[:500]
    for row in pages:
        row.update({"rel_path": rel_path, "sha256": sha256, "kind": kind})
    seconds = time.monotonic() - started
    return {"file": _file_row(rel_path, sha256, kind, pages, seconds, error), "pages": pages}


# --------------------------------------------------------------------------- #
# Work list, registers, logging
# --------------------------------------------------------------------------- #
def _hash_or_fail(path: Path) -> str:
    """SHA-256 of a local file, raising a clear error when it is missing."""
    if not path.is_file():
        raise FileNotFoundError(f"not a file: {path}")
    return sha256_file(path)


def tasks_from_ledger(ledger_path: Path, root_dir: Path) -> List[Dict[str, Any]]:
    """Census tasks for every pdf/pptx ledger entry (missing local files become error tasks)."""
    tasks: List[Dict[str, Any]] = []
    for record in read_jsonl(ledger_path):
        if not isinstance(record, dict):
            continue
        rel_path = str(record.get("rel_path", ""))
        kind = file_ext(rel_path)
        if kind not in CENSUS_KINDS:
            continue
        local = root_dir / str(record.get("local_path", ""))
        sha = str(record.get("sha256") or "")
        if not sha and local.is_file():
            sha = sha256_file(local)
        tasks.append({"rel_path": rel_path, "sha256": sha, "kind": kind, "local_path": str(local)})
    return tasks


def tasks_from_paths(paths: Sequence[str], root_dir: Path) -> List[Dict[str, Any]]:
    """Census tasks for ad hoc local files; ``rel_path`` is relative to ``root_dir`` when possible."""
    tasks: List[Dict[str, Any]] = []
    for item in paths:
        local = Path(item).expanduser().resolve()
        kind = file_ext(local.name)
        if kind not in CENSUS_KINDS:
            raise ValueError(f"--paths only accepts pdf/pptx files: {local}")
        try:
            rel_path = local.relative_to(root_dir.resolve()).as_posix()
        except ValueError:
            rel_path = local.as_posix()
        tasks.append({"rel_path": rel_path, "sha256": _hash_or_fail(local), "kind": kind, "local_path": str(local)})
    return tasks


def dedupe_tasks(tasks: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep the first task per sha256 (hash-identical copies share one census)."""
    seen: set[str] = set()
    unique: List[Dict[str, Any]] = []
    for task in tasks:
        key = task["sha256"] or task["local_path"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(task)
    return unique


class Registers:
    """The two census CSVs plus the log; all appends go through a lock."""

    def __init__(self, registers_dir: Path, logs_dir: Path) -> None:
        self.page_path = registers_dir / "page_census.csv"
        self.file_path = registers_dir / "file_census.csv"
        self.log_path = logs_dir / "census.log"

    def done_hashes(self) -> set[str]:
        """sha256 values that already have a file-census row."""
        return {row["sha256"] for row in read_csv_dicts(self.file_path) if row.get("sha256")}

    def repair(self) -> int:
        """Drop page rows whose file never got its file-census row (interrupted run); return the count."""
        pages = read_csv_dicts(self.page_path)
        if not pages:
            return 0
        done = self.done_hashes()
        kept = [row for row in pages if row.get("sha256") in done]
        dropped = len(pages) - len(kept)
        if dropped:
            write_csv_dicts(self.page_path, kept, PAGE_COLUMNS)
        return dropped

    @staticmethod
    def _append(path: Path, rows: List[Dict[str, Any]], columns: Sequence[str]) -> None:
        """Append rows to a CSV, writing the header when the file is new or empty."""
        import csv

        ensure_dir(path.parent)
        new_file = not path.exists() or path.stat().st_size == 0
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(columns), restval="", extrasaction="raise")
            if new_file:
                writer.writeheader()
            for row in rows:
                writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
            handle.flush()
            os.fsync(handle.fileno())

    def record(self, result: Dict[str, Any]) -> None:
        """Persist one file's result: page rows first, then the file row that marks it complete."""
        with file_lock(self.file_path):
            if result["pages"]:
                self._append(self.page_path, result["pages"], PAGE_COLUMNS)
            self._append(self.file_path, [result["file"]], FILE_COLUMNS)

    def log(self, message: str) -> None:
        """One progress line to stderr and to ``logs/census.log``."""
        line = f"{utc_now_iso()} {message}"
        print(line, file=sys.stderr, flush=True)
        ensure_dir(self.log_path.parent)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _describe(row: Dict[str, Any]) -> str:
    """Short progress text for a file row."""
    if row["error"]:
        return f"ERROR {row['rel_path']} ({row['error']})"
    return (f"done {row['rel_path']} pages={row['pages']} text={row['text_pages']} vector={row['vector_pages']} "
            f"raster={row['raster_pages']} mixed={row['mixed_pages']} annots={row['total_annotations']} "
            f"{row['seconds']}s")


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #
def _timeout_result(task: Dict[str, Any], timeout_s: float) -> Dict[str, Any]:
    """Result recorded when the parent gives up waiting on a worker."""
    row = _file_row(task["rel_path"], task["sha256"], task["kind"], [], timeout_s,
                    f"timeout after {timeout_s:g} s (worker unresponsive)")
    return {"file": row, "pages": []}


def _run_pool(tasks: List[Dict[str, Any]], workers: int, file_timeout_s: float,
              registers: Registers, progress: Progress) -> int:
    """Census every task in a pool; return the number of files that ended in error."""
    errors = 0
    grace = file_timeout_s + 30.0
    context = multiprocessing.get_context("fork") if "fork" in multiprocessing.get_all_start_methods() \
        else multiprocessing.get_context()
    pool = context.Pool(processes=workers)
    try:
        pending = [(task, pool.apply_async(census_file, (task,))) for task in tasks]
        for task, handle in pending:
            try:
                result = handle.get(timeout=grace)
            except multiprocessing.TimeoutError:
                result = _timeout_result(task, file_timeout_s)
            except Exception as exc:  # noqa: BLE001 - a crashed worker must not stop the run
                row = _file_row(task["rel_path"], task["sha256"], task["kind"], [], 0.0,
                                f"worker failure: {type(exc).__name__}: {exc}"[:500])
                result = {"file": row, "pages": []}
            registers.record(result)
            status = "error" if result["file"]["error"] else "done"
            errors += status == "error"
            progress.set(f"census:{task['rel_path']}", status, hash=task["sha256"] or None,
                         pages=result["file"]["pages"], error=result["file"]["error"])
            registers.log(_describe(result["file"]))
    finally:
        pool.terminate()
        pool.join()
    return errors


def run(args: argparse.Namespace) -> int:
    """``run`` subcommand: census every pending pdf/pptx; non-zero when any file errored."""
    root_dir = args.project_root if args.project_root is not None else project_root()
    registers = Registers(args.registers_dir, args.logs_dir)
    progress = Progress(args.progress)
    dropped = registers.repair()
    if dropped:
        registers.log(f"repaired page_census.csv: dropped {dropped} orphan page rows")

    tasks = tasks_from_paths(args.paths, root_dir) if args.paths else tasks_from_ledger(args.ledger, root_dir)
    tasks = dedupe_tasks(tasks)
    done = registers.done_hashes()
    pending = [t for t in tasks if t["sha256"] not in done]
    if args.limit is not None:
        pending = pending[: max(args.limit, 0)]
    for task in pending:
        task["file_timeout_s"] = args.file_timeout
        task["page_budget_s"] = args.page_budget
    registers.log(f"census: {len(tasks)} candidate files, {len(tasks) - len(pending)} already censused, "
                  f"{len(pending)} to do, {args.workers} workers")
    if not pending:
        return 0
    errors = _run_pool(pending, args.workers, args.file_timeout, registers, progress)
    registers.log(f"census finished: {len(pending) - errors} ok, {errors} errors")
    return 1 if errors else 0


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
def _root_of(rel_path: str) -> str:
    """Top-level root of a rel_path ('DD Sign-OFF', 'Basis', 'Template' or whatever it starts with)."""
    return parse_department(rel_path)["root"] or "(none)"


def build_summary(file_rows: List[Dict[str, str]], page_rows: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Per-root counts of files, errors, pages and pages per class, plus a TOTAL row."""
    by_root: Dict[str, Dict[str, int]] = defaultdict(lambda: {k: 0 for k in SUMMARY_COLUMNS[1:]})
    for row in file_rows:
        bucket = by_root[_root_of(row["rel_path"])]
        bucket["files"] += 1
        bucket["errors"] += 1 if row.get("error") else 0
        bucket["total_annotations"] += int(row.get("total_annotations") or 0)
    for row in page_rows:
        bucket = by_root[_root_of(row["rel_path"])]
        bucket["pages"] += 1
        cls = row.get("page_class", "")
        if cls in bucket:
            bucket[cls] += 1
    rows = [{"root": root, **counts} for root, counts in sorted(by_root.items())]
    if rows:
        total = {k: sum(r[k] for r in rows) for k in SUMMARY_COLUMNS[1:]}
        rows.append({"root": "TOTAL", **total})
    return rows


def summary(args: argparse.Namespace) -> int:
    """``summary`` subcommand: print and write ``census_summary.csv``; non-zero when there is no census."""
    registers = Registers(args.registers_dir, args.logs_dir)
    file_rows = read_csv_dicts(registers.file_path)
    page_rows = read_csv_dicts(registers.page_path)
    if not file_rows:
        print("no census yet: run `census run` first", file=sys.stderr)
        return 1
    rows = build_summary(file_rows, page_rows)
    out_path = args.registers_dir / "census_summary.csv"
    write_csv_dicts(out_path, rows, SUMMARY_COLUMNS)
    header = f"{'root':14s}" + "".join(f"{col:>18s}" for col in SUMMARY_COLUMNS[1:])
    print(header)
    for row in rows:
        print(f"{row['root']:14s}" + "".join(f"{row[col]:>18d}" for col in SUMMARY_COLUMNS[1:]))
    print(f"written {out_path}")
    return 0


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def default_workers() -> int:
    """``max(1, cpu_count() - 1)``."""
    return max(1, (os.cpu_count() or 1) - 1)


def _add_dir_options(parser: argparse.ArgumentParser, suppress: bool) -> None:
    """Add ``--registers-dir`` / ``--logs-dir``; accepted before or after the subcommand."""
    reg_default: Any = argparse.SUPPRESS if suppress else PATHS["registers"]
    log_default: Any = argparse.SUPPRESS if suppress else PATHS["logs"]
    parser.add_argument("--registers-dir", type=Path, default=reg_default, help="out/registers directory")
    parser.add_argument("--logs-dir", type=Path, default=log_default, help="logs directory")


def build_parser() -> argparse.ArgumentParser:
    """Argument parser with the ``run`` and ``summary`` subcommands (directory options go on either side)."""
    parser = argparse.ArgumentParser(description="Page census of pulled PDF and PPTX files (script only, no model).")
    _add_dir_options(parser, suppress=False)
    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser("run", help="census every pending pdf/pptx in the pull ledger")
    _add_dir_options(run_cmd, suppress=True)
    run_cmd.add_argument("--ledger", type=Path, default=PATHS["state"] / "pull_ledger.jsonl", help="pull ledger")
    run_cmd.add_argument("--progress", type=Path, default=PATHS["state"] / "progress.json", help="progress file")
    run_cmd.add_argument("--project-root", type=Path, default=None, help="root that ledger local_path values are relative to")
    run_cmd.add_argument("--paths", nargs="+", default=None, help="ad hoc local pdf/pptx files instead of the ledger")
    run_cmd.add_argument("--limit", type=int, default=None, help="census at most N pending files (pilots)")
    run_cmd.add_argument("--workers", type=int, default=default_workers(), help="pool size (default cpu_count-1)")
    run_cmd.add_argument("--file-timeout", type=float, default=DEFAULT_FILE_TIMEOUT_S, help="seconds per file")
    run_cmd.add_argument("--page-budget", type=float, default=DEFAULT_PAGE_BUDGET_S, help="seconds for path counting per page")
    run_cmd.set_defaults(func=run)

    sum_cmd = sub.add_parser("summary", help="page counts per root and page class")
    _add_dir_options(sum_cmd, suppress=True)
    sum_cmd.set_defaults(func=summary)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point; returns a process exit code."""
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:  # noqa: BLE001 - surface any failure as a non-zero exit
        print(f"error: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
