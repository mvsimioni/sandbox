"""Page census of every pulled PDF and PPTX, by script only (no model).

For each file in ``state/pull_ledger.jsonl`` with extension ``pdf`` or ``pptx``
(or any file named with ``--paths``) the census records one row per page or
slide in ``out/registers/page_census.csv`` and one row per file in
``out/registers/file_census.csv``.  The per-page measurements drive the later
choice of extraction route (text layer, vector drawing, scan) so they must be
cheap, deterministic and resumable:

* a file whose ``sha256`` already has a row in ``file_census.csv`` is skipped
  (rows with an error are skipped too unless ``--retry-errors`` is given, which
  drops those rows and censuses the files again),
* a ledger entry without a ``sha256`` whose local file is missing cannot be
  resumed by hash: it is attempted on every run and its error row (keyed by
  ``rel_path``) is replaced, not appended, so the registers stay idempotent,
* files run in a fixed-size pool of ``max(1, cpu_count() - 1)`` worker
  processes (one process per file) with a per-file time budget (default 600 s;
  a timeout is recorded as an error and the run continues); a worker that does
  not answer within the budget plus ``--kill-grace`` seconds is killed so it
  cannot hold a pool slot for the rest of the run,
* ``page.get_cdrawings()`` has its own per-page budget (default 20 s).  Because
  it is a single C call that no signal can interrupt, it runs in a forked
  helper process that is killed when the budget passes; ``vector_paths`` is
  then ``-1`` with the note ``path_count_timeout``,
* register appends are one ``write`` plus ``fsync`` per file, and every run
  starts by dropping torn (incomplete) rows and orphan page rows left by a
  crash, so a half-written row is never treated as done,
* one progress line per file goes to stderr and to ``logs/census.log``.

Image coverage on rotated pages: ``page.get_image_info()`` reports bboxes in
unrotated page space, so they are mapped through ``page.rotation_matrix`` before
being clipped to the (rotated) ``page.rect``.

Page classes (PDF)::

    vector_drawing  vector_paths >= 500 or vector_paths == -1
    raster_or_scan  image_coverage_pct >= 60 and text_chars < 50
    text            text_chars >= 200 and vector_paths < 500 and image_coverage_pct < 30
    mixed           otherwise

The note ``ocr_like_text`` marks a page with at least 50 text characters where
one image alone covers at least 90 % of the page (text over a scan).

Page classes (PPTX slides)::

    raster_or_scan  one picture covers >= 60 % of the slide and text_chars < 50
    text            text_chars >= 200 and picture coverage < 30 %
    mixed           otherwise

PPTX comments are counted from every ``ppt/comments/*.xml`` part (classic and
modern ``modernComment_*.xml``) read with ``zipfile``; parts that no slide's
relationships point at are added to slide 1 with the note
``unattributed_comments=N`` so the file total is complete.
"""

from __future__ import annotations

import argparse
import csv
import multiprocessing
import os
import re
import select
import signal
import struct
import sys
import time
import traceback
import zipfile
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from multiprocessing import connection as mp_connection
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

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
DEFAULT_KILL_GRACE_S: float = 30.0
EMU_PER_PT: float = 12700.0

VECTOR_THRESHOLD = 500
RASTER_COVERAGE_PCT = 60.0
RASTER_MAX_TEXT = 50
TEXT_MIN_CHARS = 200
TEXT_MAX_COVERAGE_PCT = 30.0
OCR_LIKE_SINGLE_IMAGE_PCT = 90.0

PATH_COUNT_TIMEOUT = "path_count_timeout"

_COMMENT_PART_RE = re.compile(r"^ppt/comments/[^/]+\.xml$", re.IGNORECASE)
_SLIDE_RELS_RE = re.compile(r"^ppt/slides/_rels/slide(\d+)\.xml\.rels$", re.IGNORECASE)

_PR_SET_PDEATHSIG = 1  # linux prctl option: deliver a signal when the parent dies


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
    main thread of a process, and only interrupts Python-level code: a single
    long C call is bounded with :func:`call_with_hard_budget` instead.
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
# Hard budget: run one integer-valued call in a forked helper that can be killed
# --------------------------------------------------------------------------- #
def _die_with_parent() -> None:
    """Best effort: ask the kernel to kill this process when its parent dies (Linux only)."""
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(_PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0)
    except Exception:  # noqa: BLE001 - optional safety net
        pass


def _reap(pid: int) -> None:
    """Kill ``pid`` if it is still running and collect its exit status."""
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def _read_exact(fd: int, size: int) -> bytes:
    """Read exactly ``size`` bytes from ``fd`` (fewer only at end of file)."""
    chunks: List[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _child_compute(fn: Callable[[], int], write_fd: int) -> None:
    """Child side of :func:`call_with_hard_budget`: compute, write 8 bytes, exit without cleanup."""
    status = 1
    try:
        _die_with_parent()
        os.write(write_fd, struct.pack("<q", int(fn())))
        status = 0
    except BaseException:  # noqa: BLE001 - never run the parent's handlers in the child
        status = 1
    finally:
        os._exit(status)


def call_with_hard_budget(fn: Callable[[], int], budget_s: float) -> Optional[int]:
    """Run ``fn`` (returning an int) in a forked child; ``None`` when ``budget_s`` passes first.

    A C-level call such as ``page.get_cdrawings()`` cannot be interrupted by a
    signal, so the only way to bound it is to run it in a process that can be
    killed.  The child inherits the open document by copy-on-write, writes the
    result to a pipe and exits with ``os._exit``.  A :class:`BudgetExceeded`
    raised by an enclosing :func:`time_budget` while waiting still kills the
    child before propagating.  Raises ``RuntimeError`` when the child fails.
    Without ``os.fork`` the call runs in-process, unbounded.
    """
    if not hasattr(os, "fork"):
        return int(fn())
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the child
        os.close(read_fd)
        _child_compute(fn, write_fd)
    os.close(write_fd)
    try:
        ready, _, _ = select.select([read_fd], [], [], max(float(budget_s), 0.0))
        if not ready:
            return None
        data = _read_exact(read_fd, 8)
    finally:
        os.close(read_fd)
        _reap(pid)
    if len(data) != 8:
        raise RuntimeError("helper process failed before returning a result")
    return struct.unpack("<q", data)[0]


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
    """``len(page.get_cdrawings())`` under a hard budget; ``(-1, 'path_count_timeout')`` when it passes.

    The count runs in a killable helper process (see :func:`call_with_hard_budget`).
    If that helper fails for any reason other than the budget, the call is made
    in-process so that a genuine pymupdf error is raised and recorded for the file.
    """
    try:
        count = call_with_hard_budget(lambda: len(page.get_cdrawings()), budget_s)
    except RuntimeError:
        count = len(page.get_cdrawings())
    if count is None:
        return -1, PATH_COUNT_TIMEOUT
    return count, None


def _image_boxes(page: Any) -> List[Tuple[float, float, float, float]]:
    """Bounding boxes of every image occurrence, in the rotated page space of ``page.rect``."""
    import pymupdf

    matrix = page.rotation_matrix
    boxes: List[Tuple[float, float, float, float]] = []
    for info in page.get_image_info(xrefs=False):
        bbox = info.get("bbox")
        if bbox and len(bbox) == 4:
            rect = pymupdf.Rect(*(float(v) for v in bbox)) * matrix
            boxes.append((rect.x0, rect.y0, rect.x1, rect.y1))
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
    """Per-page rows for a PDF file.

    The document is opened from memory rather than from a file descriptor so
    the forked path-counting helper never shares (and moves) a file offset with
    its parent.
    """
    import pymupdf

    rows: List[Dict[str, Any]] = []
    with pymupdf.open(stream=Path(path).read_bytes(), filetype="pdf") as doc:
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


def _resolve_part(target: str) -> str:
    """Resolve a slide relationship target (relative to ``ppt/slides``, or package-absolute) to a part name."""
    if target.startswith("/"):
        return target.lstrip("/")
    return os.path.normpath(os.path.join("ppt/slides", target)).replace("\\", "/")


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
            part = _resolve_part((rel.get("Target") or "").replace("\\", "/"))
            if _COMMENT_PART_RE.match(part):
                mapping[int(match.group(1))].append(part)
    return mapping


def pptx_comment_counts(path: Path) -> Tuple[Dict[int, int], int]:
    """Comment counts per slide (by slide number) and the total across the package.

    Reads every ``ppt/comments/*.xml`` part (classic ``comment*.xml`` and modern
    ``modernComment_*.xml``) straight from the zip.  Parts that no slide
    relationship points at are counted in the total only; :func:`census_pptx`
    attributes that remainder to slide 1 so the file total is complete.
    """
    per_slide: Dict[int, int] = defaultdict(int)
    with zipfile.ZipFile(path) as archive:
        comment_parts = [n for n in archive.namelist() if _COMMENT_PART_RE.match(n)]
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


def _is_picture(shape: Any) -> bool:
    """True for picture shapes, including filled picture placeholders."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER

    kind = shape.shape_type
    if kind in (MSO_SHAPE_TYPE.PICTURE, MSO_SHAPE_TYPE.LINKED_PICTURE):
        return True
    if kind != MSO_SHAPE_TYPE.PLACEHOLDER:
        return False
    fmt = getattr(shape, "placeholder_format", None)
    return fmt is not None and fmt.type == PP_PLACEHOLDER.PICTURE and hasattr(shape, "image")


@dataclass
class _SlideTally:
    """Running totals while walking a slide's shapes."""

    text_parts: List[str] = field(default_factory=list)
    picture_boxes: List[Tuple[float, float, float, float]] = field(default_factory=list)
    tables: int = 0
    charts: int = 0
    drawn: int = 0

    def add(self, shape: Any) -> None:
        """Account for one (non-group) shape."""
        from pptx.enum.shapes import MSO_SHAPE_TYPE

        self.text_parts.append(_shape_text(shape))
        if _is_picture(shape):
            bbox = _shape_box_pt(shape)
            if bbox:
                self.picture_boxes.append(bbox)
        elif getattr(shape, "has_table", False) and shape.has_table:
            self.tables += 1
        elif getattr(shape, "has_chart", False) and shape.has_chart:
            self.charts += 1
        elif shape.shape_type in (MSO_SHAPE_TYPE.AUTO_SHAPE, MSO_SHAPE_TYPE.FREEFORM, MSO_SHAPE_TYPE.LINE):
            self.drawn += 1


def _notes_chars(slide: Any) -> int:
    """Number of characters in the slide's speaker notes."""
    if not slide.has_notes_slide or slide.notes_slide.notes_text_frame is None:
        return 0
    return len(slide.notes_slide.notes_text_frame.text)


def census_slide(slide: Any, number: int, width: float, height: float, comments: int,
                 unattributed: int = 0) -> Dict[str, Any]:
    """Measure one slide and return a page-census row (without rel_path/sha256/kind).

    ``unattributed`` comments (from parts no slide references) are added to this
    slide's count and noted, so the file total stays complete.
    """
    tally = _SlideTally()
    for shape in _iter_shapes(slide.shapes):
        tally.add(shape)
    text = "\n".join(p for p in tally.text_parts if p)
    text_chars = len(text)
    picture_pct = coverage_pct(tally.picture_boxes, width, height)
    largest = max((single_box_pct(b, width, height) for b in tally.picture_boxes), default=0.0)
    total_comments = comments + unattributed
    notes = [f"tables={tally.tables}", f"charts={tally.charts}", f"notes_chars={_notes_chars(slide)}"]
    if unattributed:
        notes.append(f"unattributed_comments={unattributed}")
    return {
        "page": number,
        "width_pt": round(width, 2),
        "height_pt": round(height, 2),
        "rotation": 0,
        "text_chars": text_chars,
        "words": len(text.split()),
        "vector_paths": tally.drawn,
        "image_count": len(tally.picture_boxes),
        "image_coverage_pct": picture_pct,
        "annotation_count": total_comments,
        "annotation_types": f"comment={total_comments}" if total_comments else "",
        "layer_count": 0,
        "page_class": classify_slide(text_chars, picture_pct, largest),
        "notes": ";".join(notes),
    }


def census_pptx(path: Path) -> List[Dict[str, Any]]:
    """Per-slide rows for a PPTX file (comments no slide references are attributed to slide 1)."""
    from pptx import Presentation

    per_slide, total = pptx_comment_counts(path)
    unattributed = max(total - sum(per_slide.values()), 0)
    prs = Presentation(str(path))
    width = (prs.slide_width or 0) / EMU_PER_PT
    height = (prs.slide_height or 0) / EMU_PER_PT
    return [census_slide(slide, index, width, height, per_slide.get(index, 0), unattributed if index == 1 else 0)
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
    ``file_timeout_s`` and ``page_budget_s``.  Runs in a worker process.
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


def _worker_entry(task: Dict[str, Any], conn: Any) -> None:
    """Worker process body: census one file and send the result back over ``conn``."""
    _die_with_parent()
    try:
        conn.send(census_file(task))
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Work list, registers, logging
# --------------------------------------------------------------------------- #
def task_key(item: Dict[str, Any]) -> str:
    """Resume key of a task or register row: its sha256, or ``path:<rel_path>`` when there is none."""
    sha = str(item.get("sha256") or "")
    return sha if sha else f"path:{item.get('rel_path', '')}"


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
    """Keep the first task per resume key (hash-identical copies share one census)."""
    seen: set[str] = set()
    unique: List[Dict[str, Any]] = []
    for task in tasks:
        key = task_key(task)
        if key in seen:
            continue
        seen.add(key)
        unique.append(task)
    return unique


def _valid_rows(path: Path, columns: Sequence[str]) -> Tuple[List[Dict[str, str]], int]:
    """Rows of a register whose field count matches ``columns``; also the number of torn rows dropped.

    A crash during an append can leave an incomplete last line (or, in theory,
    any line of the block that was being written).  Such a line has too few
    fields or lacks its terminator, so it is dropped rather than trusted.
    """
    if not path.exists() or path.stat().st_size == 0:
        return [], 0
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    torn_tail = not raw.endswith(b"\n")
    records = list(csv.reader(text.splitlines(keepends=True)))
    if not records:
        return [], 0
    body = records[1:]
    if torn_tail and body:
        body = body[:-1]
    kept = [dict(zip(columns, rec)) for rec in body if len(rec) == len(columns)]
    return kept, len(records) - 1 - len(kept)


class Registers:
    """The two census CSVs plus the log; all appends go through a lock."""

    def __init__(self, registers_dir: Path, logs_dir: Path) -> None:
        self.page_path = registers_dir / "page_census.csv"
        self.file_path = registers_dir / "file_census.csv"
        self.log_path = logs_dir / "census.log"

    def file_rows(self) -> List[Dict[str, str]]:
        """Every file-census row (after :meth:`repair` these are all complete)."""
        return read_csv_dicts(self.file_path)

    def done_keys(self, include_errors: bool = True) -> set[str]:
        """Resume keys that already have a file-census row (optionally only error-free ones)."""
        return {task_key(row) for row in self.file_rows() if include_errors or not row.get("error")}

    def done_hashes(self) -> set[str]:
        """sha256 values that already have a file-census row (kept for callers that key by hash)."""
        return {row["sha256"] for row in self.file_rows() if row.get("sha256")}

    def repair(self) -> Dict[str, int]:
        """Drop torn rows from both registers and page rows whose file never got its file row.

        Returns ``{"torn": n, "orphans": m}``.  Runs under the register lock.
        """
        with file_lock(self.file_path):
            files, torn_files = _valid_rows(self.file_path, FILE_COLUMNS)
            if torn_files:
                write_csv_dicts(self.file_path, files, FILE_COLUMNS)
            pages, torn_pages = _valid_rows(self.page_path, PAGE_COLUMNS)
            done = {task_key(row) for row in files}
            kept = [row for row in pages if task_key(row) in done]
            orphans = len(pages) - len(kept)
            if torn_pages or orphans:
                write_csv_dicts(self.page_path, kept, PAGE_COLUMNS)
        return {"torn": torn_files + torn_pages, "orphans": orphans}

    def drop_rows(self, keys: set[str], errors_only: bool) -> int:
        """Remove the rows (file and page) whose resume key is in ``keys``; return how many file rows went.

        With ``errors_only`` only file rows that record an error are removed.
        """
        def goes(row: Dict[str, str]) -> bool:
            return task_key(row) in keys and (bool(row.get("error")) or not errors_only)

        with file_lock(self.file_path):
            rows = self.file_rows()
            kept = [row for row in rows if not goes(row)]
            if len(kept) != len(rows):
                write_csv_dicts(self.file_path, kept, FILE_COLUMNS)
                gone = {task_key(row) for row in rows} - {task_key(row) for row in kept}
                pages = read_csv_dicts(self.page_path)
                kept_pages = [row for row in pages if task_key(row) not in gone]
                if len(kept_pages) != len(pages):
                    write_csv_dicts(self.page_path, kept_pages, PAGE_COLUMNS)
        return len(rows) - len(kept)

    @staticmethod
    def _append(path: Path, rows: List[Dict[str, Any]], columns: Sequence[str]) -> None:
        """Append rows to a CSV as one ``write`` + ``fsync`` (header included when the file is new or empty).

        A single write of a prepared block cannot interleave with another
        writer and, should the process die mid-write, leaves at most one torn
        line, which :meth:`repair` drops at the next start.
        """
        import io

        ensure_dir(path.parent)
        new_file = not path.exists() or path.stat().st_size == 0
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(columns), restval="", extrasaction="raise")
        if new_file:
            writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
        block = buffer.getvalue().encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            view = memoryview(block)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)

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
# Run: a fixed-size pool of one-process-per-file workers that can be killed
# --------------------------------------------------------------------------- #
@dataclass
class _Job:
    """One running worker process."""

    index: int
    task: Dict[str, Any]
    process: Any
    conn: Any
    started: float


def _timeout_result(task: Dict[str, Any], timeout_s: float) -> Dict[str, Any]:
    """Result recorded when the parent gives up waiting on a worker."""
    row = _file_row(task["rel_path"], task["sha256"], task["kind"], [], timeout_s,
                    f"timeout after {timeout_s:g} s (worker unresponsive, killed)")
    return {"file": row, "pages": []}


def _failure_result(task: Dict[str, Any], detail: str) -> Dict[str, Any]:
    """Result recorded when a worker died without answering."""
    row = _file_row(task["rel_path"], task["sha256"], task["kind"], [], 0.0, f"worker failure: {detail}"[:500])
    return {"file": row, "pages": []}


def _mp_context() -> Any:
    """``fork`` where available (cheap, inherits monkeypatches), else the platform default."""
    if "fork" in multiprocessing.get_all_start_methods():
        return multiprocessing.get_context("fork")
    return multiprocessing.get_context()


def _start_job(context: Any, index: int, task: Dict[str, Any]) -> _Job:
    """Start one worker process for ``task``."""
    parent_conn, child_conn = context.Pipe(duplex=False)
    process = context.Process(target=_worker_entry, args=(task, child_conn), name=f"census-{index}")
    process.start()
    child_conn.close()
    return _Job(index=index, task=task, process=process, conn=parent_conn, started=time.monotonic())


def _kill_job(job: _Job) -> None:
    """Terminate a worker (SIGTERM, then SIGKILL), reap it and release its pipe."""
    if job.process.is_alive():
        job.process.terminate()
        job.process.join(2.0)
    if job.process.is_alive():
        job.process.kill()
    job.process.join()
    job.conn.close()


def _collect_job(job: _Job) -> Dict[str, Any]:
    """Read a finished worker's result, or a failure row when it died without one."""
    try:
        result = job.conn.recv()
    except (EOFError, OSError) as exc:
        job.process.join(5.0)
        result = _failure_result(job.task, f"exit code {job.process.exitcode} ({type(exc).__name__})")
    _kill_job(job)
    return result


def _poll_jobs(jobs: Dict[int, _Job], grace_s: float) -> Dict[int, Dict[str, Any]]:
    """Wait briefly for workers; return results for those that finished or overran ``grace_s``."""
    if not jobs:
        return {}
    now = time.monotonic()
    slack = min(grace_s - (now - job.started) for job in jobs.values())
    ready = set(mp_connection.wait([job.conn for job in jobs.values()], timeout=min(max(slack, 0.0), 1.0)))
    finished: Dict[int, Dict[str, Any]] = {}
    for index, job in list(jobs.items()):
        if job.conn in ready:
            finished[index] = _collect_job(job)
        elif time.monotonic() - job.started >= grace_s:
            _kill_job(job)
            finished[index] = _timeout_result(job.task, float(job.task.get("file_timeout_s", DEFAULT_FILE_TIMEOUT_S)))
        else:
            continue
        del jobs[index]
    return finished


def _record_result(task: Dict[str, Any], result: Dict[str, Any], registers: Registers, progress: Progress) -> bool:
    """Persist one result (registers, progress, log); return True when it is an error.

    A task without a sha256 (its local file is missing) cannot be resumed by
    hash, so it is attempted on every run and its previous row is replaced.
    """
    if not task["sha256"]:
        registers.drop_rows({task_key(task)}, errors_only=False)
    registers.record(result)
    error = bool(result["file"]["error"])
    progress.set(f"census:{task['rel_path']}", "error" if error else "done", hash=task["sha256"] or None,
                 pages=result["file"]["pages"], error=result["file"]["error"])
    registers.log(_describe(result["file"]))
    return error


def _run_pool(tasks: List[Dict[str, Any]], workers: int, file_timeout_s: float, registers: Registers,
              progress: Progress, kill_grace_s: float = DEFAULT_KILL_GRACE_S) -> int:
    """Census every task with at most ``workers`` concurrent processes; return the number of errors.

    Results are recorded in task order.  A worker that has not answered
    ``kill_grace_s`` after its own file budget is killed and recorded as a
    timeout, so it cannot keep a pool slot for the rest of the run.
    """
    context = _mp_context()
    grace = file_timeout_s + kill_grace_s
    jobs: Dict[int, _Job] = {}
    results: Dict[int, Dict[str, Any]] = {}
    errors = next_start = next_record = 0
    try:
        while next_record < len(tasks):
            while next_start < len(tasks) and len(jobs) < max(workers, 1):
                jobs[next_start] = _start_job(context, next_start, tasks[next_start])
                next_start += 1
            results.update(_poll_jobs(jobs, grace))
            while next_record in results:
                errors += _record_result(tasks[next_record], results.pop(next_record), registers, progress)
                next_record += 1
    finally:
        for job in jobs.values():
            _kill_job(job)
    return errors


def run(args: argparse.Namespace) -> int:
    """``run`` subcommand: census every pending pdf/pptx; non-zero when any file errored."""
    root_dir = args.project_root if args.project_root is not None else project_root()
    registers = Registers(args.registers_dir, args.logs_dir)
    progress = Progress(args.progress)
    repaired = registers.repair()
    if any(repaired.values()):
        registers.log(f"repaired registers: dropped {repaired['torn']} torn rows and {repaired['orphans']} "
                      f"orphan page rows")

    tasks = tasks_from_paths(args.paths, root_dir) if args.paths else tasks_from_ledger(args.ledger, root_dir)
    tasks = dedupe_tasks(tasks)
    done = registers.done_keys(include_errors=not args.retry_errors)
    pending = [t for t in tasks if not t["sha256"] or task_key(t) not in done]
    already = len(tasks) - len(pending)
    if args.limit is not None:
        pending = pending[: max(args.limit, 0)]
    if args.retry_errors and pending:
        dropped = registers.drop_rows({task_key(t) for t in pending}, errors_only=True)
        if dropped:
            registers.log(f"retry: dropped {dropped} error rows to census those files again")
    for task in pending:
        task["file_timeout_s"] = args.file_timeout
        task["page_budget_s"] = args.page_budget
    limit_note = f" (limit {args.limit})" if args.limit is not None else ""
    registers.log(f"census: {len(tasks)} candidate files, {already} already censused, "
                  f"{len(pending)} to do{limit_note}, {args.workers} workers")
    if not pending:
        return 0
    errors = _run_pool(pending, args.workers, args.file_timeout, registers, progress, args.kill_grace)
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
    run_cmd.add_argument("--paths", nargs="+", default=None,
                         help="ad hoc local pdf/pptx files instead of the ledger (rel_path is relative to the "
                              "project root, or absolute, so the summary buckets them by their first folder)")
    run_cmd.add_argument("--limit", type=int, default=None, help="census at most N pending files (pilots)")
    run_cmd.add_argument("--workers", type=int, default=default_workers(), help="pool size (default cpu_count-1)")
    run_cmd.add_argument("--file-timeout", type=float, default=DEFAULT_FILE_TIMEOUT_S, help="seconds per file")
    run_cmd.add_argument("--page-budget", type=float, default=DEFAULT_PAGE_BUDGET_S, help="seconds for path counting per page")
    run_cmd.add_argument("--kill-grace", type=float, default=DEFAULT_KILL_GRACE_S,
                         help="seconds after the file timeout before an unresponsive worker is killed")
    run_cmd.add_argument("--retry-errors", action="store_true",
                         help="census again the files whose file-census row records an error (their old rows are dropped)")
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
