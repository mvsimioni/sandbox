"""Sidecar extraction for one PPTX file.

``python -m src.sidecar_pptx run --pptx <path> [--out <dir>]`` reads a deck
with python-pptx and straight from the package zip and writes, under the
output directory:

* ``slides.jsonl``: one record per slide (number, layout name, every shape
  including the children of groups with id, name, type, text with paragraph
  and run structure, position and size in EMU and in pt, z-order and flags);
* ``tables_s<N>_<k>.csv`` for every table shape plus ``tables.json`` (merged
  cells are repeated in the CSV and flagged in the JSON);
* ``notes.md``: speaker notes per slide;
* ``comments.jsonl``: comments from the legacy ``ppt/comments/comment*.xml``
  parts (authors from ``ppt/commentAuthors.xml``) and from the modern
  ``ppt/comments/modernComment_*.xml`` parts (authors from ``ppt/authors.xml``,
  replies with their parent, anchor shape or slide);
* ``pictures/<slide>_<shape>_<sha8>.<ext>`` for every embedded picture at
  native resolution, deduplicated by SHA-256, with ``pictures.json``;
* ``slide_info.json``: slide size and per-slide counts;
* ``sidecar_manifest.json``: source hash, files written, counts and checks.

Coordinate frames.  Every shape record carries ``emu`` (the box exactly as
stored on the shape: slide space for top-level shapes, the parent group's
child space inside a group, named by ``emu_frame``), ``emu_slide`` and ``pt``
(the same box in slide space after every enclosing group's scale, offset,
rotation and flip have been applied; ``pt`` is EMU / 12700), ``rotation``
(the shape's own value) and ``rotation_slide`` (composed with the groups).

The run is idempotent: when the manifest in the output directory records the
same source SHA-256 and module version and every listed file exists, nothing
is rewritten unless ``--force`` is given.  The source file is only read.
Nothing here touches the network.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import posixpath
import re
import sys
import math
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

try:  # package import (python -m src.sidecar_pptx, tests)
    from src.census import coverage_pct
    from src.common import (
        PATHS,
        atomic_write_json,
        atomic_write_text,
        ensure_dir,
        read_jsonl,
        sha256_file,
        utc_now_iso,
    )
    from src.state import Progress
except ModuleNotFoundError:  # run as a plain script from inside src/
    from census import coverage_pct  # type: ignore[no-redef]
    from common import (  # type: ignore[no-redef]
        PATHS,
        atomic_write_json,
        atomic_write_text,
        ensure_dir,
        read_jsonl,
        sha256_file,
        utc_now_iso,
    )
    from state import Progress  # type: ignore[no-redef]

MODULE_NAME = "sidecar_pptx"
MODULE_VERSION = "1.1.0"
EMU_PER_PT: float = 12700.0
LEGACY_POS_PER_PT: float = 8.0
"""PowerPoint writes legacy comment positions in eighths of a point."""

MANIFEST_NAME = "sidecar_manifest.json"
SLIDES_NAME = "slides.jsonl"
TABLES_NAME = "tables.json"
NOTES_NAME = "notes.md"
COMMENTS_NAME = "comments.jsonl"
PICTURES_NAME = "pictures.json"
PICTURES_DIR = "pictures"
SLIDE_INFO_NAME = "slide_info.json"
LEDGER_FILES: Tuple[str, ...] = ("pull_ledger.jsonl", "upload_ledger.jsonl")

_MODERN_COMMENT_RE = re.compile(r"^ppt/comments/modernComment_.*\.xml$", re.IGNORECASE)
_LEGACY_COMMENT_RE = re.compile(r"^ppt/comments/comment\d*\.xml$", re.IGNORECASE)
_SLIDE_PART_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$", re.IGNORECASE)
_SLIDE_RELS_RE = re.compile(r"^ppt/slides/_rels/slide(\d+)\.xml\.rels$", re.IGNORECASE)
_MEDIA_RE = re.compile(r"^ppt/media/[^/]+$", re.IGNORECASE)
_IMAGE_EXTS = frozenset({"png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "emf", "wmf", "svg", "webp", "wdp", "ico"})
_CONTENT_TYPE_EXT: Dict[str, str] = {
    "image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/gif": "gif", "image/bmp": "bmp",
    "image/tiff": "tiff", "image/x-emf": "emf", "image/emf": "emf", "image/x-wmf": "wmf", "image/wmf": "wmf",
    "image/svg+xml": "svg", "image/webp": "webp", "image/vnd.ms-photo": "wdp", "image/x-icon": "ico",
}
"""Extension per OPC content type of an image part (the package metadata, not a Pillow probe)."""
ParseErrors = List[Dict[str, str]]
"""Where package parts that failed to parse are recorded: ``{"part", "error"}`` rows."""
_NUMERIC_RE = re.compile(r"(?<![A-Za-z0-9_])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|(?<![A-Za-z0-9_])[-+]?\.\d+")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def numeric_tokens(text: str) -> List[str]:
    """Return the numeric tokens of ``text`` in order, thousands separators removed.

    A token is an optionally signed integer or decimal (``1,250.5`` -> ``1250.5``,
    ``.5`` -> ``.5``).  A sign is taken only when it is not glued to a letter
    or digit, so ``AHU-07`` yields ``07`` (not ``-07``) and ``2024-05-01``
    yields ``2024``, ``05`` and ``01``; digits glued to a preceding letter
    (``R2``) are skipped.  The same
    function serves the numeric-recall check of the Markdown writer, so its
    exact behaviour matters more than linguistic perfection.
    """
    return [m.group(0).replace(",", "") for m in _NUMERIC_RE.finditer(text or "")]


def emu_to_pt(value: Optional[float]) -> Optional[float]:
    """Convert EMU to points (rounded to 2 dp); ``None`` stays ``None``."""
    return None if value is None else round(float(value) / EMU_PER_PT, 2)


def sha256_bytes(blob: bytes) -> str:
    """SHA-256 hex digest of a byte string."""
    return hashlib.sha256(blob).hexdigest()


def _localname(element: Any) -> str:
    """Local (namespace-free) tag name of an lxml element, or '' for comments/PIs."""
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _parse_xml(data: bytes, part: str = "", errors: Optional[ParseErrors] = None) -> Optional[Any]:
    """Parse XML bytes with lxml; ``None`` when malformed, recording the failure in ``errors`` when given.

    Nothing downstream may treat a part that failed to parse as empty without
    leaving a trace, so callers that read package parts pass their error list.
    """
    from lxml import etree

    try:
        return etree.fromstring(data)
    except (etree.XMLSyntaxError, ValueError) as exc:
        if errors is not None:
            errors.append({"part": part, "error": str(exc)[:200]})
        return None


def _read_part_xml(archive: zipfile.ZipFile, part: str, errors: Optional[ParseErrors] = None) -> Optional[Any]:
    """Read and parse one package part; missing or unreadable parts are recorded in ``errors``."""
    try:
        data = archive.read(part)
    except (KeyError, zipfile.BadZipFile, OSError) as exc:
        if errors is not None:
            errors.append({"part": part, "error": f"cannot read part: {exc}"[:200]})
        return None
    return _parse_xml(data, part, errors)


def _resolve_part(base_dir: str, target: str) -> str:
    """Resolve a relationship target against the directory of its source part."""
    target = target.replace("\\", "/")
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(base_dir, target))


def _rels_targets(archive: zipfile.ZipFile, rels_name: str, base_dir: str,
                  errors: Optional[ParseErrors] = None) -> List[Tuple[str, str, str]]:
    """``(rId, type, resolved target)`` for every relationship in ``rels_name`` (empty when missing)."""
    if rels_name not in archive.namelist():
        return []
    root = _read_part_xml(archive, rels_name, errors)
    if root is None:
        return []
    out: List[Tuple[str, str, str]] = []
    for rel in root.iter():
        if _localname(rel) != "Relationship":
            continue
        target = rel.get("Target") or ""
        if (rel.get("TargetMode") or "").lower() == "external":
            continue
        out.append((rel.get("Id") or "", rel.get("Type") or "", _resolve_part(base_dir, target)))
    return out


# --------------------------------------------------------------------------- #
# Geometry: absolute placement of shapes inside groups
# --------------------------------------------------------------------------- #
Matrix = Tuple[float, float, float, float]
"""Row-major 2 x 2 matrix ``(a, b, c, d)`` = ``[[a, b], [c, d]]``."""


def _mat_mul(m: Matrix, n: Matrix) -> Matrix:
    """Product ``m @ n`` of two 2 x 2 matrices."""
    return (m[0] * n[0] + m[1] * n[2], m[0] * n[1] + m[1] * n[3],
            m[2] * n[0] + m[3] * n[2], m[2] * n[1] + m[3] * n[3])


def _mat_vec(m: Matrix, v: Tuple[float, float]) -> Tuple[float, float]:
    """Product ``m @ v``."""
    return (m[0] * v[0] + m[1] * v[1], m[2] * v[0] + m[3] * v[1])


@dataclass(frozen=True)
class Transform:
    """Affine map from a (group) child coordinate space to slide EMU: ``abs = M @ c + t``.

    ``matrix`` and ``offset`` carry the exact map (scale, offset, rotation and
    flips of every enclosing group); ``scale_x``/``scale_y`` are the accumulated
    size factors, ``rotation`` the accumulated group rotation in degrees and
    ``flip_h``/``flip_v`` the parity of the accumulated flips, so a child's own
    rotation can be expressed in slide terms (``rotation_of``).
    """

    matrix: Matrix = (1.0, 0.0, 0.0, 1.0)
    offset: Tuple[float, float] = (0.0, 0.0)
    scale_x: float = 1.0
    scale_y: float = 1.0
    rotation: float = 0.0
    flip_h: bool = False
    flip_v: bool = False

    def point(self, x: float, y: float) -> Tuple[float, float]:
        """Map a point of the child space to slide coordinates."""
        px, py = _mat_vec(self.matrix, (x, y))
        return (px + self.offset[0], py + self.offset[1])

    def size(self, cx: float, cy: float) -> Tuple[float, float]:
        """Map an extent of the child space to slide coordinates (magnitudes only)."""
        return (abs(self.scale_x) * cx, abs(self.scale_y) * cy)

    def box(self, left: float, top: float, width: float, height: float) -> Tuple[float, float, float, float]:
        """Slide-space ``(left, top, width, height)`` of a child box.

        The box centre goes through the full affine map and the extent through
        the accumulated scale, which is exact for unrotated groups and keeps the
        centre exact for rotated ones (the rotation itself is reported through
        :meth:`rotation_of`, as PowerPoint does for the shape's own rotation).
        """
        cx, cy = self.point(left + width / 2.0, top + height / 2.0)
        w, h = self.size(width, height)
        return (cx - w / 2.0, cy - h / 2.0, w, h)

    @property
    def mirrored(self) -> bool:
        """True when an odd number of single-axis flips is accumulated (rotation sense reverses)."""
        return self.flip_h != self.flip_v

    def rotation_of(self, own_rotation: float) -> float:
        """Rotation in slide terms of a child that is rotated by ``own_rotation`` in this space."""
        signed = -own_rotation if self.mirrored else own_rotation
        return round((self.rotation + signed) % 360.0, 2)

    def child(self, off: Tuple[float, float], ext: Tuple[float, float],
              ch_off: Tuple[float, float], ch_ext: Tuple[float, float],
              rotation: float = 0.0, flip_h: bool = False, flip_v: bool = False) -> "Transform":
        """Transform for the children of a group placed at ``off``/``ext`` in this space.

        A child point ``p`` maps to ``off + S (p - chOff)`` in the group's unrotated
        frame, is then flipped and rotated about the group centre, and finally
        goes through this (parent) transform.
        """
        gx = ext[0] / ch_ext[0] if ch_ext[0] else 1.0
        gy = ext[1] / ch_ext[1] if ch_ext[1] else 1.0
        local_m: Matrix = (gx, 0.0, 0.0, gy)
        local_t = (off[0] - gx * ch_off[0], off[1] - gy * ch_off[1])
        theta = math.radians(rotation)
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        rot_flip = _mat_mul((cos_t, -sin_t, sin_t, cos_t), (-1.0 if flip_h else 1.0, 0.0, 0.0, -1.0 if flip_v else 1.0))
        centre = (off[0] + ext[0] / 2.0, off[1] + ext[1] / 2.0)
        k = _mat_mul(rot_flip, local_m)
        rel = _mat_vec(rot_flip, (local_t[0] - centre[0], local_t[1] - centre[1]))
        k_t = (rel[0] + centre[0], rel[1] + centre[1])
        matrix = _mat_mul(self.matrix, k)
        moved = _mat_vec(self.matrix, k_t)
        offset = (moved[0] + self.offset[0], moved[1] + self.offset[1])
        group_rotation = (-rotation if self.mirrored else rotation) + (180.0 if flip_h and flip_v else 0.0)
        return Transform(matrix, offset, self.scale_x * gx, self.scale_y * gy,
                         (self.rotation + group_rotation) % 360.0,
                         self.flip_h != flip_h, self.flip_v != flip_v)


def _xfrm_flips(xfrm: Any) -> Tuple[float, bool, bool]:
    """``(rotation degrees, flipH, flipV)`` of an ``a:xfrm``/``p:xfrm`` element (zeros when absent)."""
    if xfrm is None:
        return 0.0, False, False
    try:
        rotation = float(getattr(xfrm, "rot", 0.0) or 0.0)
    except (TypeError, ValueError):
        rotation = 0.0
    return rotation, bool(getattr(xfrm, "flipH", False)), bool(getattr(xfrm, "flipV", False))


def _group_transform(group: Any, parent: Transform) -> Transform:
    """Transform for the children of a python-pptx group shape (identity when it has no xfrm)."""
    xfrm = getattr(getattr(group._element, "grpSpPr", None), "xfrm", None)  # noqa: SLF001
    if xfrm is None or xfrm.off is None or xfrm.ext is None:
        return parent
    off = (float(xfrm.off.x), float(xfrm.off.y))
    ext = (float(xfrm.ext.cx), float(xfrm.ext.cy))
    ch_off = (float(xfrm.chOff.x), float(xfrm.chOff.y)) if xfrm.chOff is not None else off
    ch_ext = (float(xfrm.chExt.cx), float(xfrm.chExt.cy)) if xfrm.chExt is not None else ext
    rotation, flip_h, flip_v = _xfrm_flips(xfrm)
    return parent.child(off, ext, ch_off, ch_ext, rotation, flip_h, flip_v)


def _box_emu(shape: Any) -> Optional[Tuple[float, float, float, float]]:
    """``(left, top, width, height)`` in EMU as stored on the shape, or ``None`` when absent."""
    values = (shape.left, shape.top, shape.width, shape.height)
    if any(v is None for v in values):
        return None
    return tuple(float(v) for v in values)  # type: ignore[return-value]


def _own_flips(shape: Any) -> Tuple[bool, bool]:
    """``(flipH, flipV)`` stored on the shape's own transform."""
    try:
        xfrm = shape._element.xfrm  # noqa: SLF001
    except Exception:  # noqa: BLE001 - graphic frames expose xfrm differently
        xfrm = None
    _rotation, flip_h, flip_v = _xfrm_flips(xfrm)
    return flip_h, flip_v


def _slide_boxes(box: Optional[Tuple[float, float, float, float]], transform: Transform,
                 ) -> Tuple[Optional[Dict[str, float]], Optional[Dict[str, float]]]:
    """``(emu_slide, pt)``: the stored box in slide coordinates after group transforms."""
    if box is None:
        return None, None
    left, top, width, height = transform.box(*box)
    emu_slide = {"left": round(left, 1), "top": round(top, 1), "width": round(width, 1), "height": round(height, 1)}
    pt = {"left": emu_to_pt(left), "top": emu_to_pt(top), "width": emu_to_pt(width), "height": emu_to_pt(height)}
    return emu_slide, pt


# --------------------------------------------------------------------------- #
# Shapes
# --------------------------------------------------------------------------- #
def _shape_type_name(shape: Any) -> str:
    """Name of the MSO shape type, or ``UNKNOWN``."""
    try:
        kind = shape.shape_type
    except Exception:  # noqa: BLE001 - python-pptx raises on exotic graphic frames
        return "UNKNOWN"
    return getattr(kind, "name", None) or "UNKNOWN"


def _placeholder_type(shape: Any) -> Optional[str]:
    """Placeholder type name when the shape is a placeholder."""
    if not getattr(shape, "is_placeholder", False):
        return None
    try:
        return shape.placeholder_format.type.name
    except Exception:  # noqa: BLE001
        return "PLACEHOLDER"


def _element_text(element: Any) -> str:
    """Concatenated ``a:t`` text under a run or field element."""
    return "".join(t.text or "" for t in element.iter() if _localname(t) == "t")


def _paragraph_text(p_element: Any) -> Tuple[List[str], str, int, int]:
    """``(runs, text, line breaks, fields)`` of one ``a:p``, walking its children in document order.

    Runs (``a:r``) and fields (``a:fld``: slide number, date) both contribute
    visible text; a line break (``a:br``) becomes a newline so the words either
    side stay separate tokens.
    """
    runs: List[str] = []
    pieces: List[str] = []
    breaks = fields = 0
    for child in p_element:
        name = _localname(child)
        if name in ("r", "fld"):
            text = _element_text(child)
            runs.append(text)
            pieces.append(text)
            fields += name == "fld"
        elif name == "br":
            pieces.append("\n")
            breaks += 1
    return runs, "".join(pieces), breaks, fields


def _paragraphs(text_frame: Any) -> List[Dict[str, Any]]:
    """Paragraphs of a text frame with their runs and fields flattened to text."""
    out: List[Dict[str, Any]] = []
    for para in text_frame.paragraphs:
        runs, text, breaks, fields = _paragraph_text(para._p)  # noqa: SLF001
        if not runs and not breaks:
            text = para.text
        out.append({"level": int(para.level or 0), "runs": runs, "text": text,
                    "line_breaks": breaks, "fields": fields})
    return out


def _is_picture(shape: Any) -> bool:
    """True when the shape carries an embedded image (picture or filled picture placeholder)."""
    if _shape_type_name(shape) in ("PICTURE", "LINKED_PICTURE"):
        return True
    try:
        return getattr(shape, "image", None) is not None
    except Exception:  # noqa: BLE001 - empty picture placeholder
        return False


def _chart_summary(shape: Any) -> Tuple[str, str]:
    """``(chart type name, title text)`` for a chart graphic frame."""
    try:
        chart = shape.chart
        kind = getattr(chart.chart_type, "name", str(chart.chart_type))
        title = chart.chart_title.text_frame.text if chart.has_title else ""
        return kind, title
    except Exception:  # noqa: BLE001
        return "UNKNOWN", ""


def _table_paragraphs(table: Any) -> Tuple[str, List[Dict[str, Any]]]:
    """Flattened text and per-row paragraphs for a table shape."""
    paragraphs: List[Dict[str, Any]] = []
    lines: List[str] = []
    for row in table.rows:
        cells = [cell.text for cell in row.cells]
        line = " | ".join(cells)
        lines.append(line)
        paragraphs.append({"level": 0, "runs": cells, "text": line})
    return "\n".join(lines), paragraphs


def shape_record(shape: Any, slide_no: int, z_order: int, parent_id: Optional[int], depth: int,
                 transform: Transform) -> Dict[str, Any]:
    """Describe one shape (text, flags, geometry) as a JSON-ready dictionary."""
    kind = _shape_type_name(shape)
    is_table = bool(getattr(shape, "has_table", False) and shape.has_table)
    is_chart = bool(getattr(shape, "has_chart", False) and shape.has_chart)
    is_group = kind == "GROUP"
    is_picture = _is_picture(shape)
    text, paragraphs = "", []
    if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
        paragraphs = _paragraphs(shape.text_frame)
        text = "\n".join(p["text"] for p in paragraphs)
    elif is_table:
        text, paragraphs = _table_paragraphs(shape.table)
    chart_type, chart_title = _chart_summary(shape) if is_chart else ("", "")
    if is_chart and chart_title and not text:
        text = chart_title
    box = _box_emu(shape)
    try:
        rotation = float(shape.rotation)
    except Exception:  # noqa: BLE001
        rotation = 0.0
    flip_h, flip_v = _own_flips(shape)
    emu_slide, pt = _slide_boxes(box, transform)
    return {
        "slide": slide_no,
        "shape_id": int(shape.shape_id),
        "name": shape.name,
        "type": kind,
        "placeholder_type": _placeholder_type(shape),
        "parent_id": parent_id,
        "depth": depth,
        "z_order": z_order,
        "is_picture": is_picture,
        "is_table": is_table,
        "is_chart": is_chart,
        "is_group": is_group,
        "chart_type": chart_type or None,
        "text": text,
        "text_chars": len(text),
        "paragraphs": paragraphs,
        "emu": None if box is None else {"left": box[0], "top": box[1], "width": box[2], "height": box[3]},
        "emu_frame": "slide" if depth == 0 else "group",
        "emu_slide": emu_slide,
        "pt": pt,
        "rotation": rotation,
        "rotation_slide": transform.rotation_of(rotation),
        "flip_h": flip_h,
        "flip_v": flip_v,
        "flip_h_slide": transform.flip_h != flip_h,
        "flip_v_slide": transform.flip_v != flip_v,
    }


def iter_shape_records(shapes: Any, slide_no: int, parent_id: Optional[int] = None, depth: int = 0,
                       transform: Transform = Transform(), counter: Optional[List[int]] = None,
                       ) -> Iterator[Tuple[Dict[str, Any], Any]]:
    """Yield ``(record, shape)`` for every shape in paint order, descending into groups."""
    counter = counter if counter is not None else [0]
    for shape in shapes:
        record = shape_record(shape, slide_no, counter[0], parent_id, depth, transform)
        counter[0] += 1
        yield record, shape
        if record["is_group"]:
            child_transform = _group_transform(shape, transform)
            yield from iter_shape_records(shape.shapes, slide_no, record["shape_id"], depth + 1,
                                          child_transform, counter)


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #
def table_grid(table: Any) -> Tuple[List[List[str]], List[Dict[str, Any]]]:
    """Cell texts as a full grid (merged cells repeated) plus the list of merged cells.

    Each merged entry names the covered cell, its origin and the span, so a
    reader can tell a repeated value from a genuine duplicate.
    """
    rows = list(table.rows)
    grid = [[cell.text for cell in row.cells] for row in rows]
    merged: List[Dict[str, Any]] = []
    for r, row in enumerate(rows):
        for c, cell in enumerate(row.cells):
            if not cell.is_merge_origin:
                continue
            span_r, span_c = int(cell.span_height), int(cell.span_width)
            for i in range(span_r):
                for j in range(span_c):
                    if i == 0 and j == 0:
                        continue
                    rr, cc = r + i, c + j
                    if rr < len(grid) and cc < len(grid[rr]):
                        grid[rr][cc] = cell.text
                        merged.append({"row": rr, "col": cc, "origin_row": r, "origin_col": c,
                                       "span_rows": span_r, "span_cols": span_c, "repeated": True})
    return grid, merged


def _csv_text(grid: List[List[str]]) -> str:
    """Render a grid as CSV text (no synthetic header; row 0 is the table's first row)."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    for row in grid:
        writer.writerow(row)
    return buffer.getvalue()


def write_tables(out_dir: Path, table_shapes: List[Tuple[Dict[str, Any], Any]]) -> List[Dict[str, Any]]:
    """Write ``tables_s<N>_<k>.csv`` per table shape and return the entries for ``tables.json``.

    ``table_shapes`` pairs each table record with its shape object in paint
    order, so two tables that share a shape id (which happens in copied decks)
    each keep their own content.
    """
    entries: List[Dict[str, Any]] = []
    per_slide: Dict[int, int] = {}
    for record, shape in table_shapes:
        slide_no = record["slide"]
        per_slide[slide_no] = per_slide.get(slide_no, 0) + 1
        k = per_slide[slide_no]
        grid, merged = table_grid(shape.table)
        name = f"tables_s{slide_no}_{k}.csv"
        atomic_write_text(out_dir / name, _csv_text(grid))
        entries.append({
            "slide": slide_no, "index": k, "shape_id": record["shape_id"], "shape_name": record["name"],
            "path": name, "rows": len(grid), "cols": max((len(r) for r in grid), default=0),
            "merged_cell_count": len(merged), "merged_cells": merged,
            "numeric_tokens": sum(len(numeric_tokens(cell)) for row in grid for cell in row),
            "shape_pt": record["pt"],
        })
    return entries


# --------------------------------------------------------------------------- #
# Notes
# --------------------------------------------------------------------------- #
def slide_notes(slide: Any) -> str:
    """Speaker notes text of a slide ('' when none)."""
    if not slide.has_notes_slide:
        return ""
    frame = slide.notes_slide.notes_text_frame
    return frame.text if frame is not None else ""


def notes_markdown(file_name: str, notes: Dict[int, str]) -> str:
    """Render the speaker notes of every slide as Markdown."""
    lines = [f"# Speaker notes: {file_name}", ""]
    for slide_no in sorted(notes):
        lines += [f"## Slide {slide_no}", "", notes[slide_no].strip() or "(no notes)", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Comments (read from the package zip)
# --------------------------------------------------------------------------- #
def _attr(element: Any, name: str) -> Optional[str]:
    """Attribute by name: the un-namespaced attribute first, then one with any namespace prefix.

    On ``<p:sldId r:id="rId2" id="256"/>`` the answer for ``id`` is ``256``
    whatever the attribute order.
    """
    value = element.get(name)
    if value is not None:
        return value
    for key, value in element.attrib.items():
        if key.rsplit("}", 1)[-1] == name:
            return value
    return None


def read_comment_authors(archive: zipfile.ZipFile, errors: Optional[ParseErrors] = None,
                         ) -> Tuple[Dict[str, Dict[str, str]], Dict[str, Dict[str, str]]]:
    """``(legacy authors by id, modern authors by GUID)`` from the two author parts."""
    legacy: Dict[str, Dict[str, str]] = {}
    modern: Dict[str, Dict[str, str]] = {}
    names = set(archive.namelist())
    for part, target, tag in (("ppt/commentAuthors.xml", legacy, "cmAuthor"), ("ppt/authors.xml", modern, "author")):
        if part not in names:
            continue
        root = _read_part_xml(archive, part, errors)
        if root is None:
            continue
        for element in root.iter():
            if _localname(element) == tag and _attr(element, "id") is not None:
                target[str(_attr(element, "id"))] = {
                    "name": _attr(element, "name") or "", "initials": _attr(element, "initials") or "",
                    "user_id": _attr(element, "userId") or "",
                }
    return legacy, modern


def slide_numbers(archive: zipfile.ZipFile, errors: Optional[ParseErrors] = None) -> Tuple[Dict[str, int], Dict[str, int]]:
    """``(slide part name -> slide number, sldId -> slide number)`` from presentation.xml.

    Falls back to the number in the part name when the presentation part is
    unreadable.
    """
    by_part: Dict[str, int] = {}
    by_sld_id: Dict[str, int] = {}
    rels = {rid: target for rid, _type, target in
            _rels_targets(archive, "ppt/_rels/presentation.xml.rels", "ppt", errors)}
    root = _read_part_xml(archive, "ppt/presentation.xml", errors) if "ppt/presentation.xml" in archive.namelist() else None
    if root is not None:
        number = 0
        for element in root.iter():
            if _localname(element) != "sldId":
                continue
            number += 1
            # The plain ``id`` is the sldId; ``r:id`` lives in the relationships namespace.
            rid = element.get("id")
            r_id = next((v for k, v in element.attrib.items() if k.endswith("}id")), None)
            if r_id and r_id in rels:
                by_part[rels[r_id]] = number
            if rid is not None:
                by_sld_id[str(rid)] = number
    for name in archive.namelist():
        match = _SLIDE_PART_RE.match(name)
        if match and name not in by_part:
            by_part[name] = int(match.group(1))
    return by_part, by_sld_id


def comment_parts_by_slide(archive: zipfile.ZipFile, by_part: Dict[str, int],
                           errors: Optional[ParseErrors] = None) -> Dict[str, int]:
    """Map each comment part to the slide number whose relationships point at it."""
    mapping: Dict[str, int] = {}
    for name in archive.namelist():
        match = _SLIDE_RELS_RE.match(name)
        if not match:
            continue
        slide_part = f"ppt/slides/slide{match.group(1)}.xml"
        slide_no = by_part.get(slide_part, int(match.group(1)))
        for _rid, _type, target in _rels_targets(archive, name, "ppt/slides", errors):
            if _LEGACY_COMMENT_RE.match(target) or _MODERN_COMMENT_RE.match(target):
                mapping[target] = slide_no
    return mapping


def _text_of(element: Any) -> str:
    """Concatenate every ``a:t`` (or plain text) under ``element``, paragraphs separated by newlines."""
    paragraphs: List[str] = []
    found_p = False
    for para in element.iter():
        if _localname(para) == "p":
            found_p = True
            paragraphs.append("".join(t.text or "" for t in para.iter() if _localname(t) in ("t",)))
    if found_p:
        return "\n".join(paragraphs)
    return "".join(element.itertext())


def _body_text(cm: Any) -> str:
    """Text of a comment or reply, excluding nested replies."""
    for child in cm:
        name = _localname(child)
        if name == "text":
            return "".join(child.itertext())
        if name == "txBody":
            return _text_of(child)
    return ""


def _anchor(cm: Any) -> Tuple[str, Optional[int], Optional[str]]:
    """``(anchor type, shape id, sldId)`` of a modern comment from its marker list."""
    shape_id: Optional[int] = None
    sld_id: Optional[str] = None
    for element in cm.iter():
        name = _localname(element)
        if name == "spMk" and shape_id is None:
            try:
                shape_id = int(_attr(element, "id") or "")
            except ValueError:
                shape_id = None
        elif name == "sldMk" and sld_id is None:
            sld_id = _attr(element, "sldId")
    return ("shape" if shape_id is not None else "slide"), shape_id, sld_id


def _position(cm: Any, per_pt: float) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Raw ``pos`` x/y of a comment and their value in points."""
    for child in cm:
        if _localname(child) == "pos":
            try:
                x, y = float(_attr(child, "x") or 0), float(_attr(child, "y") or 0)
            except ValueError:
                return None, None, None, None
            return x, y, round(x / per_pt, 2), round(y / per_pt, 2)
    return None, None, None, None


def _comment_row(kind: str, part: str, slide_no: Optional[int], cm: Any, author: Dict[str, str],
                 reply_to: Optional[str], per_pt: float) -> Dict[str, Any]:
    """One comments.jsonl row (index is filled in later)."""
    anchor_type, shape_id, sld_id = _anchor(cm)
    x, y, x_pt, y_pt = _position(cm, per_pt)
    author_id = _attr(cm, "authorId") or ""
    return {
        "index": None, "kind": kind, "slide": slide_no, "part": part,
        "comment_id": _attr(cm, "id") or _attr(cm, "idx") or "",
        "author_id": author_id, "author": author.get("name", ""), "initials": author.get("initials", ""),
        "date": _attr(cm, "created") or _attr(cm, "dt") or "",
        "text": _body_text(cm), "reply_to": reply_to,
        "anchor_type": anchor_type if kind == "modern" else "slide",
        "anchor_shape_id": shape_id, "anchor_sld_id": sld_id,
        "pos_x": x, "pos_y": y, "pos_x_pt": x_pt, "pos_y_pt": y_pt,
        "pos_units": "eighth-pt" if kind == "legacy" else "EMU",
        "status": _attr(cm, "status") or ("done" if (_attr(cm, "complete") or "0") in ("1", "true") else ""),
    }


def parse_comment_part(archive: zipfile.ZipFile, part: str, slide_no: Optional[int],
                       authors: Tuple[Dict[str, Dict[str, str]], Dict[str, Dict[str, str]]],
                       by_sld_id: Dict[str, int], errors: Optional[ParseErrors] = None) -> List[Dict[str, Any]]:
    """Rows for every comment (and reply) in one legacy or modern comment part.

    A part that does not parse yields no rows and is recorded in ``errors``.
    """
    root = _read_part_xml(archive, part, errors)
    if root is None:
        return []
    legacy = bool(_LEGACY_COMMENT_RE.match(part))
    kind = "legacy" if legacy else "modern"
    author_map = authors[0] if legacy else authors[1]
    per_pt = LEGACY_POS_PER_PT if legacy else EMU_PER_PT
    rows: List[Dict[str, Any]] = []
    for cm in root.iter():
        if _localname(cm) != "cm":
            continue
        author = author_map.get(_attr(cm, "authorId") or "", {})
        row = _comment_row(kind, part, slide_no, cm, author, None, per_pt)
        if row["slide"] is None and row["anchor_sld_id"] in by_sld_id:
            row["slide"] = by_sld_id[row["anchor_sld_id"]]
        rows.append(row)
        for reply in cm.iter():
            if _localname(reply) != "reply":
                continue
            reply_author = author_map.get(_attr(reply, "authorId") or "", {})
            reply_row = _comment_row(kind, part, row["slide"], reply, reply_author, row["comment_id"], per_pt)
            reply_row.update({"anchor_type": row["anchor_type"], "anchor_shape_id": row["anchor_shape_id"],
                              "anchor_sld_id": row["anchor_sld_id"]})
            rows.append(reply_row)
    return rows


def read_comments(pptx_path: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """All comments of the package (legacy and modern) and a small summary of the parts read.

    When any comment, author, presentation or relationship part fails to
    parse, the summary carries ``unreadable_parts`` (part and error rows); the
    key is absent on a clean deck.
    """
    rows: List[Dict[str, Any]] = []
    errors: ParseErrors = []
    with zipfile.ZipFile(pptx_path) as archive:
        names = archive.namelist()
        authors = read_comment_authors(archive, errors)
        by_part, by_sld_id = slide_numbers(archive, errors)
        part_slide = comment_parts_by_slide(archive, by_part, errors)
        parts = sorted(n for n in names if _LEGACY_COMMENT_RE.match(n) or _MODERN_COMMENT_RE.match(n))
        for part in parts:
            rows.extend(parse_comment_part(archive, part, part_slide.get(part), authors, by_sld_id, errors))
    rows.sort(key=lambda r: (r["slide"] if r["slide"] is not None else 10**9, r["part"], 0 if r["reply_to"] is None else 1))
    for index, row in enumerate(rows, start=1):
        row["index"] = index
    summary = {
        "legacy_parts": [p for p in parts if _LEGACY_COMMENT_RE.match(p)],
        "modern_parts": [p for p in parts if _MODERN_COMMENT_RE.match(p)],
        "legacy_authors": len(authors[0]), "modern_authors": len(authors[1]),
        "unlinked_parts": [p for p in parts if p not in part_slide],
    }
    if errors:  # present only when a part failed, so a clean deck's summary stays minimal
        summary["unreadable_parts"] = errors
    return rows, summary


# --------------------------------------------------------------------------- #
# Pictures
# --------------------------------------------------------------------------- #
def _pixel_size(blob: bytes) -> Optional[Tuple[int, int]]:
    """Pixel size of an image blob via Pillow, or ``None`` for formats it cannot open (EMF, WMF, SVG)."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(blob)) as img:
            return int(img.width), int(img.height)
    except Exception:  # noqa: BLE001
        return None


def _crop(shape: Any) -> Optional[Dict[str, float]]:
    """Crop fractions of a picture shape, or ``None`` when it shows the whole image."""
    try:
        crop = {side: float(getattr(shape, f"crop_{side}") or 0.0) for side in ("left", "top", "right", "bottom")}
    except Exception:  # noqa: BLE001
        return None
    return crop if any(abs(v) > 1e-9 for v in crop.values()) else None


def _image_part(shape: Any) -> Tuple[bytes, str, str]:
    """``(blob, content type, part name)`` of a picture's embedded image part.

    Reads the OPC part directly, so the blob and its declared content type come
    back even when Pillow cannot identify the image (python-pptx's ``Image.ext``
    and ``Image.content_type`` both probe the bytes with Pillow and raise on
    EMF/WMF variants, HD Photo, SVG-only or corrupt blobs).  Raises when the
    shape has no embedded image (linked picture, empty placeholder).
    """
    rid = shape._element.blip_rId  # noqa: SLF001
    if rid is None:
        raise ValueError("no embedded image")
    part = shape.part.related_part(rid)
    return bytes(part.blob), str(getattr(part, "content_type", "") or ""), str(getattr(part, "partname", "") or "")


def _image_ext(content_type: str, part_name: str) -> str:
    """File extension for an image part from its content type, then its part name, else ``bin``."""
    ext = _CONTENT_TYPE_EXT.get(content_type.strip().lower())
    if ext:
        return ext
    tail = part_name.rsplit(".", 1)[-1].lower() if "." in part_name else ""
    return tail if tail in _IMAGE_EXTS else "bin"


class PictureStore:
    """Writes each distinct image blob once under ``pictures/`` and records every placement."""

    def __init__(self, out_dir: Path) -> None:
        self.dir = ensure_dir(out_dir / PICTURES_DIR)
        self.by_sha: Dict[str, str] = {}
        self.items: List[Dict[str, Any]] = []

    def _store(self, blob: bytes, ext: str, stem: str) -> Tuple[str, str, bool]:
        """Write ``blob`` unless its hash is known; return ``(sha256, relative path, newly written)``."""
        digest = sha256_bytes(blob)
        if digest in self.by_sha:
            return digest, self.by_sha[digest], False
        rel = f"{PICTURES_DIR}/{stem}_{digest[:8]}.{ext.lower().lstrip('.') or 'bin'}"
        target = self.dir.parent / rel
        if not (target.exists() and sha256_file(target) == digest):
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_bytes(blob)
            os.replace(tmp, target)
        self.by_sha[digest] = rel
        return digest, rel, True

    def add_shape(self, shape: Any, record: Dict[str, Any]) -> None:
        """Record a picture shape (image blob, placement, crop).

        A blob Pillow cannot identify is still exported and hashed; only its
        pixel size is ``None`` (with ``pixel_note`` saying why).  A shape without
        an embedded image is recorded with an ``error`` and never aborts the run.
        """
        try:
            blob, content_type, part_name = _image_part(shape)
        except Exception as exc:  # noqa: BLE001 - linked or empty picture, broken relationship
            self.items.append({"slide": record["slide"], "shape_id": record["shape_id"], "shape_name": record["name"],
                               "path": None, "error": f"no embedded image: {exc}", "shape_pt": record["pt"]})
            return
        ext = _image_ext(content_type, part_name)
        digest, rel, new = self._store(blob, ext, f"{record['slide']}_{record['shape_id']}")
        size = _pixel_size(blob)
        item = {
            "slide": record["slide"], "shape_id": record["shape_id"], "shape_name": record["name"], "path": rel,
            "content_type": content_type or None, "part_name": part_name or None, "bytes": len(blob),
            "sha256": digest, "pixel_width": size[0] if size else None, "pixel_height": size[1] if size else None,
            "crop": _crop(shape), "shape_pt": record["pt"], "rotation": record["rotation"],
            "rotation_slide": record["rotation_slide"],
            "duplicate_of_first_placement": not new, "source": "slide shape",
        }
        if size is None:
            item["pixel_note"] = f"Pillow cannot identify the image ({content_type or ext}); exported as stored"
        self.items.append(item)

    def add_unplaced_media(self, pptx_path: Path) -> None:
        """Export image parts under ``ppt/media`` that no slide shape placed (layouts, masters, backgrounds)."""
        with zipfile.ZipFile(pptx_path) as archive:
            for name in sorted(archive.namelist()):
                ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                if not _MEDIA_RE.match(name) or ext not in _IMAGE_EXTS:
                    continue
                blob = archive.read(name)
                if sha256_bytes(blob) in self.by_sha:
                    continue
                digest, rel, _new = self._store(blob, ext, "media")
                size = _pixel_size(blob)
                self.items.append({
                    "slide": None, "shape_id": None, "shape_name": None, "path": rel, "content_type": None,
                    "bytes": len(blob), "sha256": digest, "pixel_width": size[0] if size else None,
                    "pixel_height": size[1] if size else None, "crop": None, "shape_pt": None, "rotation": None,
                    "duplicate_of_first_placement": False, "source": f"package part {name} (not placed on a slide)",
                })

    def manifest(self) -> Dict[str, Any]:
        """Contents of ``pictures.json``."""
        return {"placements": len([i for i in self.items if i.get("slide") is not None]),
                "unique_images": len(self.by_sha), "items": self.items}


# --------------------------------------------------------------------------- #
# Whole-deck extraction
# --------------------------------------------------------------------------- #
@dataclass
class DeckExtract:
    """Everything read from one deck before it is written out."""

    slide_width_emu: int
    slide_height_emu: int
    slides: List[Dict[str, Any]]
    notes: Dict[int, str]
    table_shapes: List[Tuple[Dict[str, Any], Any]]
    picture_shapes: List[Tuple[Dict[str, Any], Any]]
    duplicate_shape_ids: List[Dict[str, int]]


def _duplicate_shape_ids(slide_no: int, records: List[Dict[str, Any]]) -> List[Dict[str, int]]:
    """Shape ids used by more than one shape on a slide (ids are not unique in copied decks)."""
    seen: Dict[int, int] = {}
    for record in records:
        seen[record["shape_id"]] = seen.get(record["shape_id"], 0) + 1
    return [{"slide": slide_no, "shape_id": sid, "count": n} for sid, n in sorted(seen.items()) if n > 1]


def extract_deck(pptx_path: Path) -> DeckExtract:
    """Walk the deck with python-pptx and collect slide, shape, notes, table and picture data.

    Table and picture shapes travel with their record (paint order), never
    looked up again by shape id, because shape ids can repeat.
    """
    from pptx import Presentation

    prs = Presentation(str(pptx_path))
    slides: List[Dict[str, Any]] = []
    notes: Dict[int, str] = {}
    table_shapes: List[Tuple[Dict[str, Any], Any]] = []
    picture_shapes: List[Tuple[Dict[str, Any], Any]] = []
    duplicates: List[Dict[str, int]] = []
    for slide_no, slide in enumerate(prs.slides, start=1):
        records: List[Dict[str, Any]] = []
        for record, shape in iter_shape_records(slide.shapes, slide_no):
            records.append(record)
            if record["is_table"]:
                table_shapes.append((record, shape))
            if record["is_picture"]:
                picture_shapes.append((record, shape))
        duplicates.extend(_duplicate_shape_ids(slide_no, records))
        try:
            layout = slide.slide_layout.name
        except Exception:  # noqa: BLE001
            layout = ""
        slides.append({"slide": slide_no, "slide_id": int(slide.slide_id), "layout": layout,
                       "shape_count": len(records), "shapes": records})
        notes[slide_no] = slide_notes(slide)
    return DeckExtract(int(prs.slide_width or 0), int(prs.slide_height or 0), slides, notes, table_shapes,
                       picture_shapes, duplicates)


PICTURE_COVERAGE_BASIS = "union of the unrotated picture boxes (pt, slide space) clipped to the slide"


def _picture_coverage(records: List[Dict[str, Any]], width_pt: float, height_pt: float) -> float:
    """Union coverage (%) of the picture shapes of one slide (see ``PICTURE_COVERAGE_BASIS``)."""
    boxes = []
    for record in records:
        if record["is_picture"] and record["pt"]:
            p = record["pt"]
            boxes.append((p["left"], p["top"], p["left"] + p["width"], p["top"] + p["height"]))
    return coverage_pct(boxes, width_pt, height_pt)


def slide_info(deck: DeckExtract, comments: List[Dict[str, Any]], tables: List[Dict[str, Any]],
               pictures: Dict[str, Any]) -> Dict[str, Any]:
    """Contents of ``slide_info.json``: slide size plus per-slide counts and totals."""
    width_pt, height_pt = deck.slide_width_emu / EMU_PER_PT, deck.slide_height_emu / EMU_PER_PT
    comment_counts: Dict[int, int] = {}
    for row in comments:
        if row["slide"] is not None:
            comment_counts[row["slide"]] = comment_counts.get(row["slide"], 0) + 1
    rows: List[Dict[str, Any]] = []
    for slide in deck.slides:
        records = slide["shapes"]
        text = "\n".join(r["text"] for r in records if r["text"])
        rows.append({
            "slide": slide["slide"], "layout": slide["layout"], "shape_count": len(records),
            "text_chars": len(text), "words": len(text.split()), "numeric_tokens": len(numeric_tokens(text)),
            "picture_count": sum(1 for r in records if r["is_picture"]),
            "picture_coverage_pct": _picture_coverage(records, width_pt, height_pt),
            "table_count": sum(1 for r in records if r["is_table"]),
            "chart_count": sum(1 for r in records if r["is_chart"]),
            "group_count": sum(1 for r in records if r["is_group"]),
            "comment_count": comment_counts.get(slide["slide"], 0),
            "notes_chars": len(deck.notes.get(slide["slide"], "").strip()),
        })
    totals = {key: sum(r[key] for r in rows) for key in
              ("shape_count", "text_chars", "words", "numeric_tokens", "picture_count", "table_count",
               "chart_count", "group_count", "comment_count", "notes_chars")}
    totals["comments_without_slide"] = sum(1 for row in comments if row["slide"] is None)
    totals["unique_images"] = pictures["unique_images"]
    totals["table_csv_files"] = len(tables)
    totals["duplicate_shape_ids"] = len(deck.duplicate_shape_ids)
    return {"slide_width_emu": deck.slide_width_emu, "slide_height_emu": deck.slide_height_emu,
            "slide_width_pt": round(width_pt, 2), "slide_height_pt": round(height_pt, 2),
            "picture_coverage_basis": PICTURE_COVERAGE_BASIS,
            "slide_count": len(deck.slides), "slides": rows, "totals": totals}


# --------------------------------------------------------------------------- #
# Ledger, manifest, idempotency
# --------------------------------------------------------------------------- #
def ledger_entry(sha256: str, pptx_path: Path, state_dir: Path) -> Optional[Dict[str, Any]]:
    """The pull or upload ledger row for this file, matched by sha256 (then by local path)."""
    rows: List[Dict[str, Any]] = []
    for name in LEDGER_FILES:
        rows.extend(r for r in read_jsonl(state_dir / name) if isinstance(r, dict))
    for row in rows:
        if row.get("sha256") == sha256:
            return row
    resolved = str(pptx_path.resolve())
    for row in rows:
        local = row.get("local_path")
        if local and str(Path(local).resolve()) == resolved:
            return row
    return None


def default_out_dir(entry: Optional[Dict[str, Any]]) -> Optional[Path]:
    """``out/sidecar/<rel_path>/`` for a ledger entry, or ``None`` when the file is not in a ledger."""
    if not entry or not entry.get("rel_path"):
        return None
    return PATHS["sidecar"] / str(entry["rel_path"])


def already_done(out_dir: Path, sha256: str) -> bool:
    """True when the manifest records this source hash and module version and every file exists."""
    manifest_path = out_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if manifest.get("sha256") != sha256 or manifest.get("module_version") != MODULE_VERSION:
        return False
    return all((out_dir / f).is_file() for f in manifest.get("files", []))


def count_comment_elements(pptx_path: Path, parts: Sequence[str]) -> Tuple[int, int]:
    """``(cm and reply elements, unreadable parts)`` across the given comment parts (independent recount)."""
    total = 0
    errors: ParseErrors = []
    with zipfile.ZipFile(pptx_path) as archive:
        for part in parts:
            root = _read_part_xml(archive, part, errors)
            if root is not None:
                total += sum(1 for el in root.iter() if _localname(el) in ("cm", "reply"))
    return total, len(errors)


def _checks(pptx_path: Path, deck: DeckExtract, comments: List[Dict[str, Any]], summary: Dict[str, Any],
            tables: List[Dict[str, Any]], pictures: Dict[str, Any], out_dir: Path) -> List[Dict[str, Any]]:
    """Self-checks recorded in the manifest (the Markdown writer copies them into Provenance)."""
    table_shapes = sum(1 for s in deck.slides for r in s["shapes"] if r["is_table"])
    csv_ok = all((out_dir / t["path"]).is_file() for t in tables)
    pics_ok = all(i.get("path") is None or (out_dir / i["path"]).is_file() for i in pictures["items"])
    cm_elements, unreadable = count_comment_elements(pptx_path, summary["legacy_parts"] + summary["modern_parts"])
    bad_parts = summary.get("unreadable_parts") or []
    return [
        {"check": "table CSV count equals table shape count", "pass": len(tables) == table_shapes and csv_ok,
         "evidence": f"{len(tables)} csv, {table_shapes} table shapes"},
        {"check": "every comment, author, presentation and relationship part parsed", "pass": not bad_parts,
         "evidence": "; ".join(f"{e['part']}: {e['error']}" for e in bad_parts) or "all parts parsed"},
        {"check": "comment rows equal comment and reply elements in the package",
         "pass": len(comments) == cm_elements and unreadable == 0,
         "evidence": f"{len(comments)} rows, {cm_elements} elements, {unreadable} unreadable parts"},
        {"check": "every comment resolved to a slide", "pass": all(r["slide"] is not None for r in comments),
         "evidence": f"{sum(1 for r in comments if r['slide'] is None)} unresolved"},
        {"check": "every exported picture file exists", "pass": pics_ok,
         "evidence": f"{pictures['unique_images']} unique images, {pictures['placements']} placements"},
        {"check": "every picture placement has an embedded image", "pass": all("error" not in i for i in pictures["items"]),
         "evidence": f"{sum(1 for i in pictures['items'] if 'error' in i)} without image"},
    ]


def _jsonl(rows: List[Dict[str, Any]]) -> str:
    """Serialise rows as JSON Lines."""
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)


def run_one(pptx_path: Path, out_dir: Path, state_dir: Path) -> Dict[str, Any]:
    """Extract every sidecar for ``pptx_path`` into ``out_dir`` and return the manifest."""
    sha256 = sha256_file(pptx_path)
    entry = ledger_entry(sha256, pptx_path, state_dir)
    ensure_dir(out_dir)
    deck = extract_deck(pptx_path)
    comments, comment_summary = read_comments(pptx_path)
    tables = write_tables(out_dir, deck.table_shapes)
    store = PictureStore(out_dir)
    for record, shape in deck.picture_shapes:
        store.add_shape(shape, record)
    store.add_unplaced_media(pptx_path)
    pictures = store.manifest()
    info = slide_info(deck, comments, tables, pictures)
    files: List[str] = [SLIDES_NAME, TABLES_NAME, NOTES_NAME, COMMENTS_NAME, PICTURES_NAME, SLIDE_INFO_NAME]
    atomic_write_text(out_dir / SLIDES_NAME, _jsonl(deck.slides))
    atomic_write_json(out_dir / TABLES_NAME, {"count": len(tables), "tables": tables})
    atomic_write_text(out_dir / NOTES_NAME, notes_markdown(pptx_path.name, deck.notes))
    atomic_write_text(out_dir / COMMENTS_NAME, _jsonl(comments))
    atomic_write_json(out_dir / PICTURES_NAME, pictures)
    atomic_write_json(out_dir / SLIDE_INFO_NAME, info)
    files += [t["path"] for t in tables] + sorted(set(store.by_sha.values()))
    manifest = {
        "module": MODULE_NAME, "module_version": MODULE_VERSION, "generated_at": utc_now_iso(),
        "source_path": str(pptx_path), "sha256": sha256, "bytes": pptx_path.stat().st_size,
        "rel_path": (entry or {}).get("rel_path"), "drive_id": (entry or {}).get("drive_id"),
        "drive_modified": (entry or {}).get("drive_modified"), "ledger_transport": (entry or {}).get("transport"),
        "converters": [{"name": "python-pptx", "version": _pptx_version()}, {"name": "lxml", "version": _lxml_version()}],
        "methods_used": ["python-pptx", "package-zip-comments"],
        "files": files, "counts": info["totals"], "comment_parts": comment_summary,
        "duplicate_shape_ids": deck.duplicate_shape_ids,
        "checks": _checks(pptx_path, deck, comments, comment_summary, tables, pictures, out_dir),
    }
    atomic_write_json(out_dir / MANIFEST_NAME, manifest)
    return manifest


def _pptx_version() -> str:
    """Installed python-pptx version."""
    import pptx

    return getattr(pptx, "__version__", "unknown")


def _lxml_version() -> str:
    """Installed lxml version."""
    import lxml

    return getattr(lxml, "__version__", "unknown")


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def run(args: argparse.Namespace) -> int:
    """``run`` subcommand: sidecars for one PPTX; skips when already done for this hash."""
    pptx_path = Path(args.pptx)
    if not pptx_path.is_file():
        print(f"error: no such file: {pptx_path}", file=sys.stderr)
        return 1
    if pptx_path.suffix.lower() != ".pptx":
        print(f"error: not a .pptx file: {pptx_path}", file=sys.stderr)
        return 1
    state_dir = Path(args.state_dir) if args.state_dir else PATHS["state"]
    sha256 = sha256_file(pptx_path)
    out_dir = Path(args.out) if args.out else default_out_dir(ledger_entry(sha256, pptx_path, state_dir))
    if out_dir is None:
        print("error: --out is required when the file is not in a ledger", file=sys.stderr)
        return 1
    progress = None if args.no_progress else Progress(args.progress)
    key = f"{MODULE_NAME}:{sha256}"
    if not args.force and already_done(out_dir, sha256):
        print(f"skip (done): {pptx_path.name} -> {out_dir}")
        if progress is not None and not progress.is_done(key, sha256):
            progress.set(key, "done", hash=sha256, out_dir=str(out_dir))
        return 0
    if progress is not None:
        progress.set(key, "running", hash=sha256, out_dir=str(out_dir))
    try:
        manifest = run_one(pptx_path, out_dir, state_dir)
    except Exception as exc:  # noqa: BLE001 - surface any failure as a non-zero exit
        if progress is not None:
            progress.set(key, "error", hash=sha256, error=str(exc)[:500])
        print(f"error: {pptx_path.name}: {exc}", file=sys.stderr)
        return 1
    if progress is not None:
        progress.set(key, "done", hash=sha256, out_dir=str(out_dir))
    failed = [c["check"] for c in manifest["checks"] if not c["pass"]]
    counts = manifest["counts"]
    print(f"done: {pptx_path.name} -> {out_dir} (files={len(manifest['files'])},"
          f" tables={counts['table_count']}, comments={counts['comment_count']},"
          f" images={counts['unique_images']}, failed checks={len(failed)})")
    for check in failed:
        print(f"  check failed: {check}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Argument parser."""
    parser = argparse.ArgumentParser(description="Sidecar extraction for one PPTX file.")
    sub = parser.add_subparsers(dest="command", required=True)
    run_cmd = sub.add_parser("run", help="extract slides, tables, notes, comments and pictures for one deck")
    run_cmd.add_argument("--pptx", required=True, help="the .pptx file to read")
    run_cmd.add_argument("--out", default=None, help="output directory (default: out/sidecar/<rel_path>/ from the ledger)")
    run_cmd.add_argument("--state-dir", default=None, help="directory holding the ledgers (default: state/)")
    run_cmd.add_argument("--progress", default=None, help="progress.json to update (default: state/progress.json)")
    run_cmd.add_argument("--no-progress", action="store_true", help="do not touch progress.json")
    run_cmd.add_argument("--force", action="store_true", help="rewrite even when already done for this hash")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point; returns a process exit code."""
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return run(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
