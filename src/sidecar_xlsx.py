"""Sidecar extraction for one workbook (``.xlsx``, ``.xlsm`` or legacy ``.xls``).

``python -m src.sidecar_xlsx run --xlsx <path> [--out <dir>]`` reads the
workbook with openpyxl and straight from the package zip and writes, under the
output directory:

* ``<sheet>_values.csv`` (cell, value, type) with the cached values and
  ``<sheet>_formulas.csv`` (cell, formula) for every sheet, hidden ones
  included;
* ``workbook.json``: sheets with state (visible, hidden, veryHidden),
  dimensions, frozen panes, counts, defined names, external links, VBA
  presence, document properties and (for ``.xls``) the LibreOffice conversion;
* ``merged.json``, ``hidden.json`` (hidden rows and columns per sheet);
* ``comments.jsonl``: legacy notes from openpyxl plus threaded comments read
  from ``xl/threadedComments/*.xml`` with authors from ``xl/persons/*.xml``;
* ``formats.json``: number formats per column per sheet (first 50 non-empty
  cells), ``fills.json``: fill colours per sheet with counts and example cells;
* ``validations.json`` and ``conditional_formats.json``;
* for ``.xlsm``: ``vba/vbaProject.bin`` and, when oletools can parse it, the
  VBA source as ``vba/<module>.bas`` plus ``vba/vba.json`` (macros are never
  run; ``keep_vba=True`` is only used to read);
* ``sidecar_manifest.json``: source hash, files written, counts and checks.

A legacy ``.xls`` is first copied to a scratch directory under ``/tmp`` and
converted to ``.xlsx`` with ``soffice --headless``; the conversion is recorded
in ``workbook.json``.  The run is idempotent: when the manifest in the output
directory records the same source SHA-256 and module version and every listed
file exists, nothing is rewritten unless ``--force`` is given.  The source file
is only read.  Nothing here touches the network.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:  # package import (python -m src.sidecar_xlsx, tests)
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

MODULE_NAME = "sidecar_xlsx"
MODULE_VERSION = "1.0.0"
MANIFEST_NAME = "sidecar_manifest.json"
WORKBOOK_NAME = "workbook.json"
MERGED_NAME = "merged.json"
HIDDEN_NAME = "hidden.json"
COMMENTS_NAME = "comments.jsonl"
FORMATS_NAME = "formats.json"
FILLS_NAME = "fills.json"
VALIDATIONS_NAME = "validations.json"
CONDITIONAL_NAME = "conditional_formats.json"
VBA_DIR = "vba"
VBA_BIN = "xl/vbaProject.bin"
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
"""Compound-file signature; a vbaProject.bin that lacks it is not something olevba can read."""
FORMAT_SAMPLE = 50
FILL_EXAMPLES = 5
SOFFICE = "/usr/bin/soffice"
SOFFICE_TIMEOUT_S = 300
LEDGER_FILES: Tuple[str, ...] = ("pull_ledger.jsonl", "upload_ledger.jsonl")
SUPPORTED_SUFFIXES = (".xlsx", ".xlsm", ".xls")

_NUMERIC_RE = re.compile(r"(?<![A-Za-z0-9_])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|(?<![A-Za-z0-9_])[-+]?\.\d+")
_THREADED_RE = re.compile(r"^xl/threadedComments/[^/]+\.xml$", re.IGNORECASE)
_PERSONS_RE = re.compile(r"^xl/persons/[^/]+\.xml$", re.IGNORECASE)
_LEGACY_RE = re.compile(r"^xl/(?:comments\d*|comments/[^/]+)\.xml$", re.IGNORECASE)
"""Excel writes ``xl/comments1.xml``; openpyxl and LibreOffice write ``xl/comments/comment1.xml``."""
_SHEET_PART_RE = re.compile(r"^xl/worksheets/sheet\d+\.xml$", re.IGNORECASE)
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SHADOW_PREFIX = "[Threaded comment]"


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def numeric_tokens(text: Any) -> List[str]:
    """Return the numeric tokens of ``text`` in order, thousands separators removed.

    Numbers are formatted first (``12.0`` -> ``12``, ``True`` -> ``1``) so a
    cached numeric value and its printed form yield the same token.  The rule
    matches the PDF and PPTX sidecars: an optionally signed integer or decimal
    not glued to a letter, so ``AHU-07`` gives ``07`` and ``R2`` gives nothing.
    """
    return [m.group(0).replace(",", "") for m in _NUMERIC_RE.finditer(value_text(text))]


def value_text(value: Any) -> str:
    """Printable form of a cell value (``None`` -> '', dates in ISO form, ints without ``.0``)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    return str(value)


def value_type(value: Any) -> str:
    """Short type code for a cached value: n, s, b, d, e (error string) or '' when empty."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "b"
    if isinstance(value, (int, float)):
        return "n"
    if isinstance(value, (dt.datetime, dt.date, dt.time, dt.timedelta)):
        return "d"
    text = str(value)
    return "e" if text.startswith("#") and text.endswith(("!", "?", "/A")) else "s"


def formula_text(value: Any) -> Optional[str]:
    """The formula of a cell loaded without ``data_only`` ('' for plain values)."""
    if isinstance(value, str) and value.startswith("="):
        return value
    text = getattr(value, "text", None)  # ArrayFormula / DataTableFormula
    if text is not None:
        return str(text) if str(text).startswith("=") else "=" + str(text)
    return None


def safe_sheet_stem(name: str, taken: Dict[str, int]) -> str:
    """File-name stem for a sheet, unique across the workbook (case-insensitive)."""
    stem = _SAFE_NAME_RE.sub("_", name).strip("_") or "sheet"
    key = stem.lower()
    if key in taken:
        taken[key] += 1
        stem = f"{stem}_{taken[key]}"
    else:
        taken[key] = 1
    return stem


def _localname(element: Any) -> str:
    """Local (namespace-free) tag name of an lxml element, or '' for comments/PIs."""
    tag = element.tag
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _attr(element: Any, name: str) -> Optional[str]:
    """Attribute by local name (ignores any namespace prefix on the attribute)."""
    for key, value in element.attrib.items():
        if key.rsplit("}", 1)[-1] == name:
            return value
    return None


def _parse_xml(data: bytes) -> Optional[Any]:
    """Parse XML bytes with lxml, returning ``None`` when malformed."""
    from lxml import etree

    try:
        return etree.fromstring(data)
    except etree.XMLSyntaxError:
        return None


def _resolve_part(base_dir: str, target: str) -> str:
    """Resolve a relationship target against the directory of its source part."""
    target = target.replace("\\", "/")
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(base_dir, target))


def _rels(archive: zipfile.ZipFile, rels_name: str, base_dir: str) -> List[Tuple[str, str, str, bool]]:
    """``(rId, type, target, external)`` for every relationship in ``rels_name`` (empty when missing)."""
    if rels_name not in archive.namelist():
        return []
    root = _parse_xml(archive.read(rels_name))
    if root is None:
        return []
    out: List[Tuple[str, str, str, bool]] = []
    for rel in root.iter():
        if _localname(rel) != "Relationship":
            continue
        external = (rel.get("TargetMode") or "").lower() == "external"
        target = rel.get("Target") or ""
        out.append((rel.get("Id") or "", rel.get("Type") or "", target if external else _resolve_part(base_dir, target),
                    external))
    return out


def _colour(colour: Any) -> Optional[str]:
    """Stable key for an openpyxl ``Color`` (``rgb:FFFF0000``, ``theme:4:-0.25``, ``indexed:64``)."""
    if colour is None:
        return None
    kind = getattr(colour, "type", None)
    if kind == "rgb" and isinstance(colour.rgb, str):
        return f"rgb:{colour.rgb}"
    if kind == "theme":
        tint = round(float(colour.tint or 0.0), 4)
        return f"theme:{colour.theme}" + (f":{tint}" if tint else "")
    if kind == "indexed":
        return f"indexed:{colour.indexed}"
    return None


# --------------------------------------------------------------------------- #
# Legacy .xls conversion (scratch copy, LibreOffice headless)
# --------------------------------------------------------------------------- #
def soffice_version(binary: Optional[str] = None) -> Optional[str]:
    """``soffice --version`` first line, or ``None`` when LibreOffice is unavailable."""
    binary = binary or SOFFICE
    if not Path(binary).is_file():
        return None
    try:
        proc = subprocess.run([binary, "--headless", "--version"], capture_output=True, text=True, timeout=60,
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return (proc.stdout or proc.stderr).strip().splitlines()[0] if (proc.stdout or proc.stderr).strip() else None


def convert_xls(xls_path: Path, scratch_root: Optional[Path] = None, binary: Optional[str] = None) -> Dict[str, Any]:
    """Copy ``xls_path`` to a scratch directory and convert it to ``.xlsx`` with LibreOffice.

    Returns a record for ``workbook.json`` with ``ok``, the converted path
    (``None`` on failure), the command and the tool version.  Macros never run:
    the conversion is headless with a throwaway user profile.  ``binary``
    defaults to the module-level ``SOFFICE`` at call time.
    """
    binary = binary or SOFFICE
    scratch = Path(tempfile.mkdtemp(prefix="sidecar_xlsx_", dir=str(scratch_root) if scratch_root else None))
    copy = scratch / xls_path.name
    shutil.copyfile(xls_path, copy)
    profile = (scratch / "profile").resolve().as_uri()
    command = [binary, "--headless", "--norestore", "--nologo", f"-env:UserInstallation={profile}",
               "--convert-to", "xlsx", "--outdir", str(scratch), str(copy)]
    record: Dict[str, Any] = {"tool": "soffice", "version": soffice_version(binary), "command": command,
                              "scratch_dir": str(scratch), "source": str(xls_path), "ok": False,
                              "converted_path": None, "stderr": ""}
    if not Path(binary).is_file():
        record["stderr"] = f"{binary} not found"
        return record
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=SOFFICE_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        record["stderr"] = str(exc)[:500]
        return record
    converted = copy.with_suffix(".xlsx")
    record["stderr"] = (proc.stderr or "")[:500]
    record["returncode"] = proc.returncode
    if converted.is_file():
        record.update({"ok": True, "converted_path": str(converted), "converted_sha256": sha256_file(converted)})
    return record


# --------------------------------------------------------------------------- #
# Package-level reads (zip)
# --------------------------------------------------------------------------- #
def sheet_parts(archive: zipfile.ZipFile) -> List[Tuple[str, str]]:
    """``(sheet name, part name)`` in workbook order from ``xl/workbook.xml`` and its rels."""
    rels = {rid: target for rid, _t, target, ext in _rels(archive, "xl/_rels/workbook.xml.rels", "xl") if not ext}
    out: List[Tuple[str, str]] = []
    if "xl/workbook.xml" not in archive.namelist():
        return out
    root = _parse_xml(archive.read("xl/workbook.xml"))
    if root is None:
        return out
    for element in root.iter():
        if _localname(element) != "sheet":
            continue
        r_id = next((v for k, v in element.attrib.items() if k.endswith("}id")), None)
        out.append((_attr(element, "name") or "", rels.get(r_id or "", "")))
    return out


def threaded_parts_by_sheet(archive: zipfile.ZipFile) -> Dict[str, str]:
    """Map every threaded-comment part to the sheet name whose relationships point at it."""
    mapping: Dict[str, str] = {}
    for sheet_name, part in sheet_parts(archive):
        if not part:
            continue
        rels_name = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
        for _rid, _type, target, external in _rels(archive, rels_name, posixpath.dirname(part)):
            if not external and _THREADED_RE.match(target):
                mapping[target] = sheet_name
    return mapping


def read_persons(archive: zipfile.ZipFile) -> Dict[str, Dict[str, str]]:
    """Threaded-comment authors by GUID from ``xl/persons/*.xml``."""
    persons: Dict[str, Dict[str, str]] = {}
    for name in archive.namelist():
        if not _PERSONS_RE.match(name):
            continue
        root = _parse_xml(archive.read(name))
        if root is None:
            continue
        for element in root.iter():
            if _localname(element) == "person" and _attr(element, "id"):
                persons[str(_attr(element, "id"))] = {"name": _attr(element, "displayName") or "",
                                                      "user_id": _attr(element, "userId") or "",
                                                      "provider": _attr(element, "providerId") or ""}
    return persons


def _threaded_row(element: Any, part: str, sheet: Optional[str], persons: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    """One comments.jsonl row for a ``threadedComment`` element."""
    person_id = _attr(element, "personId") or ""
    text = ""
    mentions: List[str] = []
    for child in element:
        if _localname(child) == "text":
            text = "".join(child.itertext())
        elif _localname(child) == "mentions":
            mentions = [_attr(m, "mentionpersonId") or "" for m in child if _localname(m) == "mention"]
    return {
        "index": None, "kind": "threaded", "sheet": sheet, "cell": _attr(element, "ref") or "", "part": part,
        "comment_id": _attr(element, "id") or "", "parent_id": _attr(element, "parentId"),
        "author_id": person_id, "author": persons.get(person_id, {}).get("name", ""),
        "date": _attr(element, "dT") or "", "text": text,
        "done": (_attr(element, "done") or "0") in ("1", "true"),
        "mentions": [persons.get(m, {}).get("name", m) for m in mentions], "threaded_shadow": False,
    }


def read_threaded_comments(archive: zipfile.ZipFile) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """All threaded comments in the package and a summary of the parts read."""
    persons = read_persons(archive)
    by_sheet = threaded_parts_by_sheet(archive)
    parts = sorted(n for n in archive.namelist() if _THREADED_RE.match(n))
    rows: List[Dict[str, Any]] = []
    for part in parts:
        root = _parse_xml(archive.read(part))
        if root is None:
            continue
        for element in root.iter():
            if _localname(element) == "threadedComment":
                rows.append(_threaded_row(element, part, by_sheet.get(part), persons))
    summary = {"threaded_parts": parts, "persons": len(persons),
               "unlinked_parts": [p for p in parts if p not in by_sheet]}
    return rows, summary


def count_comment_elements(archive: zipfile.ZipFile) -> Dict[str, int]:
    """Independent recount of ``comment`` (legacy) and ``threadedComment`` elements in the package."""
    counts = {"legacy": 0, "threaded": 0, "merge_cells": 0}
    for name in archive.namelist():
        key = "legacy" if _LEGACY_RE.match(name) else "threaded" if _THREADED_RE.match(name) else \
            "merge_cells" if _SHEET_PART_RE.match(name) else None
        if key is None:
            continue
        root = _parse_xml(archive.read(name))
        if root is None:
            continue
        wanted = {"legacy": "comment", "threaded": "threadedComment", "merge_cells": "mergeCell"}[key]
        counts[key] += sum(1 for el in root.iter() if _localname(el) == wanted)
    return counts


def package_facts(xlsx_path: Path) -> Dict[str, Any]:
    """Facts read straight from the zip: VBA part, external link parts, element recounts."""
    with zipfile.ZipFile(xlsx_path) as archive:
        names = archive.namelist()
        ext_targets = [t for _r, _t, t, ext in _rels(archive, "xl/_rels/workbook.xml.rels", "xl") if ext]
        for name in names:
            if name.startswith("xl/externalLinks/_rels/"):
                ext_targets += [t for _r, _t, t, ext in _rels(archive, name, "xl/externalLinks") if ext]
        return {
            "has_vba": VBA_BIN in names,
            "external_link_parts": sorted(n for n in names if n.startswith("xl/externalLinks/") and n.endswith(".xml")),
            "external_targets": ext_targets,
            "element_counts": count_comment_elements(archive),
            "threaded": read_threaded_comments(archive),
        }


# --------------------------------------------------------------------------- #
# VBA (extract and dump source; never executed)
# --------------------------------------------------------------------------- #
def dump_vba(xlsx_path: Path, out_dir: Path) -> Dict[str, Any]:
    """Extract ``xl/vbaProject.bin`` to ``vba/`` and dump its modules with oletools when possible."""
    vba_dir = ensure_dir(out_dir / VBA_DIR)
    record: Dict[str, Any] = {"present": False, "bin_path": None, "read": False, "modules": [], "files": [],
                              "reason": None, "suspicious": []}
    with zipfile.ZipFile(xlsx_path) as archive:
        if VBA_BIN not in archive.namelist():
            record["reason"] = "no xl/vbaProject.bin in the package"
            return record
        blob = archive.read(VBA_BIN)
    bin_path = vba_dir / "vbaProject.bin"
    bin_path.write_bytes(blob)
    record.update({"present": True, "bin_path": f"{VBA_DIR}/vbaProject.bin", "bin_bytes": len(blob),
                   "files": [f"{VBA_DIR}/vbaProject.bin"]})
    try:
        from oletools import olevba
    except ImportError:
        record["reason"] = "oletools not installed; VBA source not read"
        return record
    record["oletools_version"] = _oletools_version()
    if not blob.startswith(OLE_MAGIC):
        record["reason"] = "vbaProject.bin is not an OLE container; VBA source not read"
        return record
    try:
        parser = olevba.VBA_Parser(str(bin_path), data=blob)
        for _fname, _stream, module_name, code in parser.extract_macros():
            stem = _SAFE_NAME_RE.sub("_", Path(str(module_name)).stem).strip("_") or "module"
            rel = f"{VBA_DIR}/{stem}.bas"
            atomic_write_text(out_dir / rel, code or "")
            record["modules"].append({"module": str(module_name), "path": rel, "lines": len((code or "").splitlines())})
            record["files"].append(rel)
        try:
            record["suspicious"] = [{"type": t, "keyword": k, "description": d} for t, k, d in parser.analyze_macros()]
        except Exception as exc:  # noqa: BLE001 - analysis is best effort
            record["analysis_error"] = str(exc)[:200]
        parser.close()
        record["read"] = True
        record["reason"] = None if record["modules"] else "oletools found no VBA modules"
    except Exception as exc:  # noqa: BLE001 - a damaged project must not fail the sidecar
        record["reason"] = f"oletools could not parse vbaProject.bin: {exc}"[:300]
    return record


def _oletools_version() -> Optional[str]:
    """Installed oletools version, or ``None``."""
    try:
        from oletools import olevba

        return getattr(olevba, "__version__", "unknown")
    except ImportError:
        return None


# --------------------------------------------------------------------------- #
# Sheet-level extraction (openpyxl)
# --------------------------------------------------------------------------- #
@dataclass
class SheetExtract:
    """Everything collected for one sheet before it is written."""

    index: int
    name: str
    stem: str
    state: str
    dimensions: str
    max_row: int
    max_col: int
    frozen_panes: Optional[str]
    values: List[Dict[str, Any]] = field(default_factory=list)
    formulas: List[Dict[str, Any]] = field(default_factory=list)
    cached_missing: int = 0
    numeric_tokens: int = 0
    merged: List[Dict[str, Any]] = field(default_factory=list)
    hidden_rows: List[int] = field(default_factory=list)
    hidden_cols: List[str] = field(default_factory=list)
    comments: List[Dict[str, Any]] = field(default_factory=list)
    formats: Dict[str, Any] = field(default_factory=dict)
    fills: List[Dict[str, Any]] = field(default_factory=list)
    validations: List[Dict[str, Any]] = field(default_factory=list)
    conditional: List[Dict[str, Any]] = field(default_factory=list)
    images: int = 0
    charts: int = 0


def extract_cells(ws_formula: Any, ws_value: Any, sheet: SheetExtract) -> None:
    """Fill values, formulas, cached-value gaps and the numeric-token count of a sheet."""
    for row in ws_formula.iter_rows():
        for cell in row:
            raw = cell.value
            if raw is None:
                continue
            formula = formula_text(raw)
            cached = ws_value[cell.coordinate].value if formula else raw
            if formula:
                sheet.formulas.append({"cell": cell.coordinate, "formula": formula})
                if cached is None:
                    sheet.cached_missing += 1
            sheet.values.append({"cell": cell.coordinate, "value": value_text(cached), "type": value_type(cached)})
            sheet.numeric_tokens += len(numeric_tokens(cached))


def extract_merged(ws: Any, sheet: SheetExtract) -> None:
    """Merged ranges of a sheet with the value of their origin cell."""
    for rng in ws.merged_cells.ranges:
        sheet.merged.append({"range": str(rng), "origin": rng.start_cell.coordinate,
                             "rows": rng.max_row - rng.min_row + 1, "cols": rng.max_col - rng.min_col + 1,
                             "value": value_text(ws.cell(rng.min_row, rng.min_col).value)})


def extract_hidden(ws: Any, sheet: SheetExtract) -> None:
    """Hidden rows and columns of a sheet (column dimensions may span a range)."""
    from openpyxl.utils import get_column_letter

    sheet.hidden_rows = sorted(int(i) for i, dim in ws.row_dimensions.items() if dim.hidden)
    cols: set[str] = set()
    for key, dim in ws.column_dimensions.items():
        if not dim.hidden:
            continue
        if dim.min and dim.max:
            cols.update(get_column_letter(i) for i in range(int(dim.min), int(dim.max) + 1))
        else:
            cols.add(str(key))
    sheet.hidden_cols = sorted(cols, key=lambda c: (len(c), c))


def extract_legacy_comments(ws: Any, sheet: SheetExtract) -> None:
    """Legacy notes (openpyxl ``cell.comment``), flagging those that mirror a threaded comment."""
    for row in ws.iter_rows():
        for cell in row:
            note = cell.comment
            if note is None:
                continue
            text = note.text or ""
            sheet.comments.append({
                "index": None, "kind": "legacy", "sheet": sheet.name, "cell": cell.coordinate, "part": None,
                "comment_id": None, "parent_id": None, "author_id": None, "author": note.author or "",
                "date": "", "text": text, "done": None, "mentions": [],
                "threaded_shadow": text.lstrip().startswith(_SHADOW_PREFIX),
            })


def extract_formats(ws: Any, sheet: SheetExtract) -> None:
    """Number formats per column from the first ``FORMAT_SAMPLE`` non-empty cells of each column."""
    from openpyxl.utils import get_column_letter

    per_col: Dict[str, Dict[str, int]] = {}
    seen: Dict[str, int] = {}
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is None:
                continue
            letter = get_column_letter(cell.column)
            if seen.get(letter, 0) >= FORMAT_SAMPLE:
                continue
            seen[letter] = seen.get(letter, 0) + 1
            bucket = per_col.setdefault(letter, {})
            bucket[cell.number_format] = bucket.get(cell.number_format, 0) + 1
    sheet.formats = {
        letter: {"sampled": seen[letter], "formats": counts,
                 "dominant": max(counts.items(), key=lambda kv: kv[1])[0]}
        for letter, counts in per_col.items()
    }


def extract_fills(ws: Any, sheet: SheetExtract) -> None:
    """Fill colours used on the sheet with counts and example cells (fill colour can carry status)."""
    fills: Dict[str, Dict[str, Any]] = {}
    for row in ws.iter_rows():
        for cell in row:
            fill = cell.fill
            if fill is None or not fill.fill_type:
                continue
            key = _colour(fill.fgColor) or _colour(fill.bgColor) or "unknown"
            entry = fills.setdefault(key, {"colour": key, "fill_type": fill.fill_type, "count": 0, "examples": [],
                                           "with_value": 0})
            entry["count"] += 1
            entry["with_value"] += int(cell.value is not None)
            if len(entry["examples"]) < FILL_EXAMPLES:
                entry["examples"].append({"cell": cell.coordinate, "value": value_text(cell.value)})
    sheet.fills = sorted(fills.values(), key=lambda e: (-e["count"], e["colour"]))


def extract_validations(ws: Any, sheet: SheetExtract) -> None:
    """Data validations of a sheet."""
    container = getattr(ws, "data_validations", None)
    for dv in (getattr(container, "dataValidation", None) or []):
        sheet.validations.append({
            "sqref": str(dv.sqref), "type": dv.type, "operator": dv.operator,
            "formula1": dv.formula1, "formula2": dv.formula2, "allow_blank": bool(dv.allow_blank),
            "show_dropdown": bool(dv.showDropDown), "error_title": dv.errorTitle, "error": dv.error,
            "prompt_title": dv.promptTitle, "prompt": dv.prompt,
        })


def _rule_record(rule: Any) -> Dict[str, Any]:
    """JSON-ready description of one conditional-formatting rule."""
    dxf = getattr(rule, "dxf", None)
    fill = font = None
    if dxf is not None:
        if dxf.fill is not None:
            fill = _colour(dxf.fill.fgColor) or _colour(dxf.fill.bgColor)
        if dxf.font is not None and dxf.font.color is not None:
            font = _colour(dxf.font.color)
    return {
        "type": rule.type, "operator": rule.operator, "formula": list(rule.formula or []),
        "priority": rule.priority, "stop_if_true": bool(rule.stopIfTrue), "text": rule.text,
        "fill": fill, "font_colour": font,
        "colour_scale": rule.colorScale is not None, "data_bar": rule.dataBar is not None,
        "icon_set": rule.iconSet is not None,
    }


def extract_conditional(ws: Any, sheet: SheetExtract) -> None:
    """Conditional formats of a sheet (one entry per range with its rules)."""
    for cf in ws.conditional_formatting:
        sheet.conditional.append({"sqref": str(cf.sqref), "rules": [_rule_record(r) for r in cf.rules]})


def extract_sheet(index: int, ws_formula: Any, ws_value: Any, stem: str) -> SheetExtract:
    """Collect everything for one sheet."""
    sheet = SheetExtract(index=index, name=ws_formula.title, stem=stem, state=ws_formula.sheet_state,
                         dimensions=ws_formula.dimensions, max_row=int(ws_formula.max_row),
                         max_col=int(ws_formula.max_column), frozen_panes=ws_formula.freeze_panes)
    extract_cells(ws_formula, ws_value, sheet)
    extract_merged(ws_formula, sheet)
    extract_hidden(ws_formula, sheet)
    extract_legacy_comments(ws_formula, sheet)
    extract_formats(ws_formula, sheet)
    extract_fills(ws_formula, sheet)
    extract_validations(ws_formula, sheet)
    extract_conditional(ws_formula, sheet)
    sheet.images = len(getattr(ws_formula, "_images", []) or [])
    sheet.charts = len(getattr(ws_formula, "_charts", []) or [])
    return sheet


# --------------------------------------------------------------------------- #
# Workbook-level extraction
# --------------------------------------------------------------------------- #
@dataclass
class WorkbookExtract:
    """Everything read from one workbook before it is written out."""

    sheets: List[SheetExtract]
    defined_names: List[Dict[str, Any]]
    external_links: Dict[str, Any]
    properties: Dict[str, Any]
    facts: Dict[str, Any]
    comments: List[Dict[str, Any]]


def _defined_names(wb: Any) -> List[Dict[str, Any]]:
    """Workbook- and sheet-scoped defined names."""
    out: List[Dict[str, Any]] = []
    names = wb.defined_names
    items = names.items() if hasattr(names, "items") else [(d.name, d) for d in getattr(names, "definedName", [])]
    for name, defn in items:
        out.append({"name": name, "scope": "workbook", "value": getattr(defn, "attr_text", None),
                    "hidden": bool(getattr(defn, "hidden", False))})
    for ws in wb.worksheets:
        local = getattr(ws, "defined_names", None) or {}
        for name, defn in (local.items() if hasattr(local, "items") else []):
            out.append({"name": name, "scope": ws.title, "value": getattr(defn, "attr_text", None),
                        "hidden": bool(getattr(defn, "hidden", False))})
    return out


def _properties(wb: Any) -> Dict[str, Any]:
    """Core document properties that help the title block of the Markdown."""
    props = wb.properties
    return {key: value_text(getattr(props, key, None)) or None
            for key in ("title", "subject", "creator", "lastModifiedBy", "created", "modified", "category")}


def _merge_comments(sheets: List[SheetExtract], threaded: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Legacy notes and threaded comments in one indexed list, ordered by sheet then cell."""
    order = {s.name: s.index for s in sheets}
    rows = [c for s in sheets for c in s.comments] + threaded
    rows.sort(key=lambda r: (order.get(r["sheet"] or "", 10**6), r["cell"], 0 if r["kind"] == "threaded" else 1,
                             "" if r["parent_id"] is None else r["parent_id"], r["date"]))
    for index, row in enumerate(rows, start=1):
        row["index"] = index
    return rows


def extract_workbook(xlsx_path: Path, keep_vba: bool) -> WorkbookExtract:
    """Load the workbook twice (formulas, cached values) and collect every sidecar input."""
    from openpyxl import load_workbook

    wb_f = load_workbook(str(xlsx_path), data_only=False, keep_vba=keep_vba)
    wb_v = load_workbook(str(xlsx_path), data_only=True, keep_vba=keep_vba)
    taken: Dict[str, int] = {}
    sheets: List[SheetExtract] = []
    for index, ws in enumerate(wb_f.worksheets, start=1):
        sheets.append(extract_sheet(index, ws, wb_v[ws.title], safe_sheet_stem(ws.title, taken)))
    facts = package_facts(xlsx_path)
    threaded_rows, threaded_summary = facts.pop("threaded")
    facts["threaded_summary"] = threaded_summary
    links = [getattr(getattr(link, "file_link", None), "Target", None) or str(link) for link in
             (getattr(wb_f, "_external_links", None) or [])]
    external = {"present": bool(links or facts["external_link_parts"] or facts["external_targets"]),
                "count": max(len(links), len(facts["external_link_parts"])),
                "targets": links or facts["external_targets"]}
    return WorkbookExtract(sheets, _defined_names(wb_f), external, _properties(wb_f), facts,
                           _merge_comments(sheets, threaded_rows))


# --------------------------------------------------------------------------- #
# Writers
# --------------------------------------------------------------------------- #
def _csv_text(rows: Iterable[Dict[str, Any]], fieldnames: Sequence[str]) -> str:
    """Render dictionaries as CSV text with a header."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(fieldnames), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def _jsonl(rows: List[Dict[str, Any]]) -> str:
    """Serialise rows as JSON Lines."""
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)


def _sheet_summary(sheet: SheetExtract) -> Dict[str, Any]:
    """The per-sheet block of ``workbook.json``."""
    return {
        "index": sheet.index, "name": sheet.name, "state": sheet.state, "dimensions": sheet.dimensions,
        "max_row": sheet.max_row, "max_col": sheet.max_col, "frozen_panes": sheet.frozen_panes,
        "files": {"values": f"{sheet.stem}_values.csv", "formulas": f"{sheet.stem}_formulas.csv"},
        "cells_non_empty": len(sheet.values), "formula_cells": len(sheet.formulas),
        "cached_values_missing": sheet.cached_missing, "numeric_tokens": sheet.numeric_tokens,
        "merged_ranges": len(sheet.merged), "hidden_rows": len(sheet.hidden_rows),
        "hidden_cols": len(sheet.hidden_cols), "legacy_comments": len(sheet.comments),
        "validations": len(sheet.validations), "conditional_formats": len(sheet.conditional),
        "fill_colours": len(sheet.fills), "images": sheet.images, "charts": sheet.charts,
    }


def write_sidecars(out_dir: Path, book: WorkbookExtract, source: Dict[str, Any], vba: Dict[str, Any],
                   conversion: Optional[Dict[str, Any]]) -> List[str]:
    """Write every sidecar file and return the list of relative paths written."""
    files: List[str] = []
    for sheet in book.sheets:
        values_name, formulas_name = f"{sheet.stem}_values.csv", f"{sheet.stem}_formulas.csv"
        atomic_write_text(out_dir / values_name, _csv_text(sheet.values, ("cell", "value", "type")))
        atomic_write_text(out_dir / formulas_name, _csv_text(sheet.formulas, ("cell", "formula")))
        files += [values_name, formulas_name]
    workbook = {
        "source": source, "converted_from": conversion, "sheet_count": len(book.sheets),
        "sheets": [_sheet_summary(s) for s in book.sheets], "defined_names": book.defined_names,
        "external_links": book.external_links, "has_vba": book.facts["has_vba"], "vba": vba,
        "properties": book.properties, "threaded_comment_parts": book.facts["threaded_summary"],
    }
    atomic_write_json(out_dir / WORKBOOK_NAME, workbook)
    atomic_write_json(out_dir / MERGED_NAME, {s.name: s.merged for s in book.sheets})
    atomic_write_json(out_dir / HIDDEN_NAME, {s.name: {"state": s.state, "rows": s.hidden_rows, "cols": s.hidden_cols}
                                              for s in book.sheets})
    atomic_write_text(out_dir / COMMENTS_NAME, _jsonl(book.comments))
    atomic_write_json(out_dir / FORMATS_NAME, {s.name: s.formats for s in book.sheets})
    atomic_write_json(out_dir / FILLS_NAME, {s.name: s.fills for s in book.sheets})
    atomic_write_json(out_dir / VALIDATIONS_NAME, {s.name: s.validations for s in book.sheets})
    atomic_write_json(out_dir / CONDITIONAL_NAME, {s.name: s.conditional for s in book.sheets})
    files += [WORKBOOK_NAME, MERGED_NAME, HIDDEN_NAME, COMMENTS_NAME, FORMATS_NAME, FILLS_NAME, VALIDATIONS_NAME,
              CONDITIONAL_NAME]
    if vba.get("present"):
        atomic_write_json(out_dir / VBA_DIR / "vba.json", vba)
        files += list(vba["files"]) + [f"{VBA_DIR}/vba.json"]
    return files


# --------------------------------------------------------------------------- #
# Checks, ledger, manifest, idempotency
# --------------------------------------------------------------------------- #
def _checks(book: WorkbookExtract, files: List[str], out_dir: Path, vba: Dict[str, Any],
            conversion: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Self-checks recorded in the manifest (the Markdown writer copies them into Provenance)."""
    counts = book.facts["element_counts"]
    legacy = sum(len(s.comments) for s in book.sheets)
    threaded = sum(1 for c in book.comments if c["kind"] == "threaded")
    merged = sum(len(s.merged) for s in book.sheets)
    missing = sum(s.cached_missing for s in book.sheets)
    formulas = sum(len(s.formulas) for s in book.sheets)
    checks = [
        {"check": "values and formulas CSV written for every sheet (hidden included)",
         "pass": all((out_dir / f).is_file() for f in files) and
         len([f for f in files if f.endswith("_values.csv")]) == len(book.sheets),
         "evidence": f"{len(book.sheets)} sheets, {len(files)} files"},
        {"check": "comment rows equal comment elements in the package",
         "pass": legacy == counts["legacy"] and threaded == counts["threaded"],
         "evidence": f"legacy {legacy}/{counts['legacy']}, threaded {threaded}/{counts['threaded']}"},
        {"check": "every threaded comment resolved to a sheet",
         "pass": all(c["sheet"] is not None for c in book.comments),
         "evidence": f"{sum(1 for c in book.comments if c['sheet'] is None)} unresolved"},
        {"check": "merged ranges equal mergeCell elements in the package", "pass": merged == counts["merge_cells"],
         "evidence": f"{merged} ranges, {counts['merge_cells']} elements"},
        {"check": "cached value present for every formula cell", "pass": missing == 0,
         "evidence": f"{missing} of {formulas} formula cells without a cached value"},
    ]
    if book.facts["has_vba"]:
        checks.append({"check": "vbaProject.bin extracted and VBA source read", "pass": bool(vba.get("read")),
                       "evidence": vba.get("reason") or f"{len(vba.get('modules', []))} modules"})
    if conversion is not None:
        checks.append({"check": "legacy .xls converted with LibreOffice", "pass": bool(conversion.get("ok")),
                       "evidence": conversion.get("stderr") or conversion.get("converted_path") or ""})
    return checks


def ledger_entry(sha256: str, path: Path, state_dir: Path) -> Optional[Dict[str, Any]]:
    """The pull or upload ledger row for this file, matched by sha256 (then by local path)."""
    rows: List[Dict[str, Any]] = []
    for name in LEDGER_FILES:
        rows.extend(r for r in read_jsonl(state_dir / name) if isinstance(r, dict))
    for row in rows:
        if row.get("sha256") == sha256:
            return row
    resolved = str(path.resolve())
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


def _converters(conversion: Optional[Dict[str, Any]], vba: Dict[str, Any]) -> List[Dict[str, str]]:
    """Converter names and versions for the manifest front matter."""
    import lxml
    import openpyxl

    out = [{"name": "openpyxl", "version": getattr(openpyxl, "__version__", "unknown")},
           {"name": "lxml", "version": getattr(lxml, "__version__", "unknown")}]
    if conversion is not None:
        out.append({"name": "soffice", "version": conversion.get("version") or "unknown"})
    if vba.get("oletools_version"):
        out.append({"name": "oletools", "version": str(vba["oletools_version"])})
    return out


def _totals(book: WorkbookExtract) -> Dict[str, int]:
    """Whole-workbook counts for the manifest."""
    keys = ("cells_non_empty", "formula_cells", "cached_values_missing", "numeric_tokens", "merged_ranges",
            "hidden_rows", "hidden_cols", "validations", "conditional_formats", "images", "charts")
    summaries = [_sheet_summary(s) for s in book.sheets]
    totals = {key: sum(s[key] for s in summaries) for key in keys}
    totals.update({
        "sheet_count": len(book.sheets),
        "hidden_sheets": sum(1 for s in book.sheets if s.state != "visible"),
        "comment_count": len(book.comments),
        "legacy_comments": sum(1 for c in book.comments if c["kind"] == "legacy"),
        "threaded_comments": sum(1 for c in book.comments if c["kind"] == "threaded"),
        "defined_names": len(book.defined_names),
    })
    return totals


def run_one(src_path: Path, out_dir: Path, state_dir: Path, scratch_root: Optional[Path] = None) -> Dict[str, Any]:
    """Extract every sidecar for ``src_path`` into ``out_dir`` and return the manifest."""
    sha256 = sha256_file(src_path)
    entry = ledger_entry(sha256, src_path, state_dir)
    ensure_dir(out_dir)
    suffix = src_path.suffix.lower()
    conversion: Optional[Dict[str, Any]] = None
    read_path = src_path
    if suffix == ".xls":
        conversion = convert_xls(src_path, scratch_root)
        if not conversion["ok"]:
            raise RuntimeError(f"LibreOffice conversion failed: {conversion['stderr'] or 'no output'}")
        read_path = Path(conversion["converted_path"])
    keep_vba = suffix == ".xlsm"
    book = extract_workbook(read_path, keep_vba=keep_vba)
    vba = dump_vba(read_path, out_dir) if book.facts["has_vba"] else {"present": False, "read": False, "files": [],
                                                                       "reason": "no VBA project in the package"}
    source = {"path": str(src_path), "sha256": sha256, "bytes": src_path.stat().st_size, "kind": suffix.lstrip(".")}
    files = write_sidecars(out_dir, book, source, vba, conversion)
    manifest = {
        "module": MODULE_NAME, "module_version": MODULE_VERSION, "generated_at": utc_now_iso(),
        "source_path": str(src_path), "sha256": sha256, "bytes": source["bytes"],
        "rel_path": (entry or {}).get("rel_path"), "drive_id": (entry or {}).get("drive_id"),
        "drive_modified": (entry or {}).get("drive_modified"), "ledger_transport": (entry or {}).get("transport"),
        "converters": _converters(conversion, vba),
        "methods_used": ["openpyxl", "package-zip-threaded-comments"] + (["soffice-xls-conversion"] if conversion else [])
        + (["oletools-olevba"] if vba.get("read") else []),
        "files": files, "counts": _totals(book), "checks": _checks(book, files, out_dir, vba, conversion),
    }
    atomic_write_json(out_dir / MANIFEST_NAME, manifest)
    return manifest


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def run(args: argparse.Namespace) -> int:
    """``run`` subcommand: sidecars for one workbook; skips when already done for this hash."""
    src_path = Path(args.xlsx)
    if not src_path.is_file():
        print(f"error: no such file: {src_path}", file=sys.stderr)
        return 1
    if src_path.suffix.lower() not in SUPPORTED_SUFFIXES:
        print(f"error: not an .xlsx/.xlsm/.xls file: {src_path}", file=sys.stderr)
        return 1
    state_dir = Path(args.state_dir) if args.state_dir else PATHS["state"]
    sha256 = sha256_file(src_path)
    out_dir = Path(args.out) if args.out else default_out_dir(ledger_entry(sha256, src_path, state_dir))
    if out_dir is None:
        print("error: --out is required when the file is not in a ledger", file=sys.stderr)
        return 1
    progress = None if args.no_progress else Progress(args.progress)
    key = f"{MODULE_NAME}:{sha256}"
    if not args.force and already_done(out_dir, sha256):
        print(f"skip (done): {src_path.name} -> {out_dir}")
        if progress is not None and not progress.is_done(key, sha256):
            progress.set(key, "done", hash=sha256, out_dir=str(out_dir))
        return 0
    if progress is not None:
        progress.set(key, "running", hash=sha256, out_dir=str(out_dir))
    try:
        manifest = run_one(src_path, out_dir, state_dir, Path(args.scratch) if args.scratch else None)
    except Exception as exc:  # noqa: BLE001 - surface any failure as a non-zero exit
        if progress is not None:
            progress.set(key, "error", hash=sha256, error=str(exc)[:500])
        print(f"error: {src_path.name}: {exc}", file=sys.stderr)
        return 1
    if progress is not None:
        progress.set(key, "done", hash=sha256, out_dir=str(out_dir))
    failed = [c["check"] for c in manifest["checks"] if not c["pass"]]
    counts = manifest["counts"]
    print(f"done: {src_path.name} -> {out_dir} (files={len(manifest['files'])}, sheets={counts['sheet_count']},"
          f" cells={counts['cells_non_empty']}, comments={counts['comment_count']}, failed checks={len(failed)})")
    for check in failed:
        print(f"  check failed: {check}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Argument parser."""
    parser = argparse.ArgumentParser(description="Sidecar extraction for one workbook (.xlsx, .xlsm, .xls).")
    sub = parser.add_subparsers(dest="command", required=True)
    run_cmd = sub.add_parser("run", help="extract values, formulas, structure, comments and formats for one workbook")
    run_cmd.add_argument("--xlsx", required=True, help="the .xlsx, .xlsm or .xls file to read")
    run_cmd.add_argument("--out", default=None, help="output directory (default: out/sidecar/<rel_path>/ from the ledger)")
    run_cmd.add_argument("--state-dir", default=None, help="directory holding the ledgers (default: state/)")
    run_cmd.add_argument("--progress", default=None, help="progress.json to update (default: state/progress.json)")
    run_cmd.add_argument("--no-progress", action="store_true", help="do not touch progress.json")
    run_cmd.add_argument("--scratch", default=None, help="parent directory for .xls conversion scratch copies (default: /tmp)")
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
