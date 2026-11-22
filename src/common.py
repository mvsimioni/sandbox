"""Shared helpers for the AIGH IP ACMV document pipeline.

Every other module imports from here rather than redefining paths, hashing,
atomic writes, JSONL/CSV helpers or the path-parsing and document
classification rules.  Nothing in this module touches the network.
"""

from __future__ import annotations

import argparse
import csv
import functools
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import yaml

CONNECTOR_LIMIT_BYTES: int = 10_485_760
"""Largest file (in bytes) the Drive connector transport will fetch."""

STAGES: tuple[str, ...] = ("Submitted", "AH sign off with comments", "DD Closure")
"""Canonical stage folder names, in the spelling used by the manifest."""

ROOT_DD = "DD Sign-OFF"
ROOT_BASIS = "Basis"
ROOT_TEMPLATE = "Template"

_DEPT_RE = re.compile(r"^(?:__)?(\d{2})\s*-\s*(.+)$")
_STRAY_PREFIX = "__"


# --------------------------------------------------------------------------- #
# Project root and configuration
# --------------------------------------------------------------------------- #
def project_root() -> Path:
    """Return the directory that contains ``config/project.yaml``.

    The search starts from this file and walks upwards, so the helpers work
    whether the pipeline is invoked from the project root, from ``src`` or
    from a test runner.
    """
    here = Path(__file__).resolve()
    for candidate in (here.parent, *here.parents):
        if (candidate / "config" / "project.yaml").is_file():
            return candidate
    raise FileNotFoundError("config/project.yaml not found above %s" % here)


@functools.lru_cache(maxsize=1)
def load_config() -> Dict[str, Any]:
    """Load and cache ``config/project.yaml`` as a dictionary."""
    cfg_path = project_root() / "config" / "project.yaml"
    with cfg_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError("config/project.yaml must contain a mapping at top level")
    return data


def _resolve_paths() -> Dict[str, Path]:
    """Build the absolute path table from ``config.paths``."""
    root = project_root()
    paths_cfg = load_config().get("paths", {}) or {}
    return {key: (root / str(value)).resolve() for key, value in paths_cfg.items()}


PATHS: Dict[str, Path] = _resolve_paths()
"""Absolute ``Path`` objects for every entry under ``paths`` in the config."""


# --------------------------------------------------------------------------- #
# Time, hashing, directories
# --------------------------------------------------------------------------- #
def utc_now_iso() -> str:
    """Return the current UTC time as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_file(path: Path | str, algorithm: str, chunk: int) -> str:
    """Stream ``path`` through ``hashlib.new(algorithm)`` and return the hex digest."""
    digest = hashlib.new(algorithm)
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def sha256_file(path: Path | str, chunk: int = 1 << 20) -> str:
    """Return the SHA-256 hex digest of a file, read in ``chunk``-byte blocks."""
    return _hash_file(path, "sha256", chunk)


def md5_file(path: Path | str, chunk: int = 1 << 20) -> str:
    """Return the MD5 hex digest of a file (for comparison with Drive's md5Checksum)."""
    return _hash_file(path, "md5", chunk)


def ensure_dir(path: Path | str) -> Path:
    """Create ``path`` (and parents) if missing and return it as a ``Path``."""
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


# --------------------------------------------------------------------------- #
# Atomic writes
# --------------------------------------------------------------------------- #
def atomic_write_text(path: Path | str, text: str, encoding: str = "utf-8") -> Path:
    """Write ``text`` to ``path`` atomically (temp file in the same directory, then ``os.replace``)."""
    target = Path(path)
    ensure_dir(target.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def atomic_write_json(path: Path | str, obj: Any, indent: int = 2) -> Path:
    """Serialise ``obj`` as UTF-8 JSON and write it atomically to ``path``."""
    text = json.dumps(obj, indent=indent, ensure_ascii=False, sort_keys=False) + "\n"
    return atomic_write_text(path, text)


# --------------------------------------------------------------------------- #
# JSONL and CSV
# --------------------------------------------------------------------------- #
def append_jsonl(path: Path | str, obj: Any) -> None:
    """Append one JSON object as a single line to ``path`` (created if missing)."""
    target = Path(path)
    ensure_dir(target.parent)
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with target.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()


def read_jsonl(path: Path | str) -> List[Any]:
    """Read every non-blank line of ``path`` as JSON; a missing file yields ``[]``."""
    target = Path(path)
    if not target.exists():
        return []
    rows: List[Any] = []
    with target.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                rows.append(json.loads(stripped))
    return rows


def read_csv_dicts(path: Path | str) -> List[Dict[str, str]]:
    """Read a UTF-8 CSV file into a list of dictionaries; a missing file yields ``[]``."""
    target = Path(path)
    if not target.exists():
        return []
    with target.open("r", encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv_dicts(path: Path | str, rows: Iterable[Dict[str, Any]], fieldnames: Sequence[str]) -> Path:
    """Write ``rows`` as a UTF-8 CSV with the given header, atomically.

    Missing keys are written blank; extra keys raise so column drift is caught early.
    """
    target = Path(path)
    ensure_dir(target.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fieldnames), restval="", extrasaction="raise")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


# --------------------------------------------------------------------------- #
# Path parsing
# --------------------------------------------------------------------------- #
def _segments(rel_path: str) -> List[str]:
    """Split a Drive-style relative path on forward slashes, dropping empties."""
    return [seg for seg in str(rel_path).replace("\\", "/").split("/") if seg]


def is_zip(rel_path: str) -> bool:
    """True when the final path segment ends in ``.zip`` (case-insensitive)."""
    segs = _segments(rel_path)
    return bool(segs) and segs[-1].lower().endswith(".zip")


def parse_department(rel_path: str) -> Dict[str, Any]:
    """Identify root, department number and name from a relative path.

    Returns ``{"root", "dept_no", "dept_name", "stray"}``.  The department is
    the top-level folder under ``DD Sign-OFF`` matching
    ``^(?:__)?(\\d{2})\\s*-\\s*(.+)$``; a leading ``__`` marks a stray folder.
    Outside ``DD Sign-OFF`` (or when the folder does not match) ``dept_no`` and
    ``dept_name`` are blank.
    """
    segs = _segments(rel_path)
    root = segs[0] if segs else ""
    result: Dict[str, Any] = {"root": root, "dept_no": "", "dept_name": "", "stray": False}
    if root != ROOT_DD or len(segs) < 2:
        return result
    folder = segs[1]
    # A zipped department sitting directly under the root is named like the folder.
    if len(segs) == 2 and is_zip(folder):
        folder = folder[: -len(".zip")]
    match = _DEPT_RE.match(folder.strip())
    if not match:
        return result
    result["dept_no"] = match.group(1)
    result["dept_name"] = match.group(2).strip()
    result["stray"] = folder.startswith(_STRAY_PREFIX)
    return result


def parse_stage(rel_path: str) -> str:
    """Return the canonical stage folder name directly under the department folder.

    Matching is case-insensitive and ignores surrounding whitespace.  Anything
    that is not ``Submitted``, ``AH sign off with comments`` or ``DD Closure``
    (including a file sitting directly in the department folder) is ``other``.
    """
    segs = _segments(rel_path)
    if len(segs) < 4 or segs[0] != ROOT_DD:
        return "other"
    candidate = re.sub(r"\s+", " ", segs[2].strip()).lower()
    for stage in STAGES:
        if candidate == stage.lower():
            return stage
    return "other"


# --------------------------------------------------------------------------- #
# Document classification
# --------------------------------------------------------------------------- #
def _token(word: str) -> re.Pattern[str]:
    """Regex that matches ``word`` as a token (not glued to other letters/digits)."""
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])", re.IGNORECASE)


_RE_RDS = _token("RDS")
_RE_RLS = _token("RLS")
_RE_SOA = _token("SOA")
_RE_SOE = _token("SoE")
_RE_DD = re.compile(r"(?<![A-Za-z])DD(?![A-Za-z])", re.IGNORECASE)
_RE_DRAWING_LIST = re.compile(r"(drawing|dwg)[\s_\-]*list", re.IGNORECASE)
_RE_LAYOUT_CODE = re.compile(r"AR-(?:IP|OP)-01-", re.IGNORECASE)
_RE_ELEVATION_CODE = re.compile(r"AR-(?:IP|OP)-31", re.IGNORECASE)
_RE_SCHEMATIC_CODE = re.compile(r"(?:IP|HS)-BH-", re.IGNORECASE)
_BOOKLET_PARTIES = ("reply", "jacobs", "dpa", "beca", "inh")


def file_ext(rel_path: str) -> str:
    """Lower-case extension of the final path segment without the dot ('' if none)."""
    segs = _segments(rel_path)
    if not segs:
        return ""
    return Path(segs[-1]).suffix.lower().lstrip(".")


def _is_booklet(name: str, lower: str, ext: str) -> Optional[str]:
    """Return a reason when the name follows the DD booklet conventions."""
    if "booklet" in lower:
        return "name contains 'booklet'"
    if ext in ("pptx", "pdf") and _RE_DD.search(name):
        for party in _BOOKLET_PARTIES:
            if party in lower:
                return f"{ext} with 'DD' and '{party}' in name"
    return None


def classify_with_reason(rel_path: str) -> tuple[str, str]:
    """Classify a document from its file name; return ``(doc_class, reason)``.

    Rules are applied in order (case-insensitive) and the first hit wins, so a
    'Drawing List' workbook is a drawing list rather than a workbook.
    """
    segs = _segments(rel_path)
    name = segs[-1] if segs else ""
    lower = name.lower()
    ext = file_ext(name)

    if ext == "zip":
        return "zip", "extension .zip"
    booklet_reason = _is_booklet(name, lower, ext)
    if booklet_reason:
        return "Booklet", booklet_reason
    if "_rds" in lower or _RE_RDS.search(name):
        return "RDS", "'RDS' token in name"
    if _RE_RLS.search(name):
        return "RLS", "'RLS' token in name"
    if _RE_SOA.search(name):
        return "SOA", "'SOA' in name"
    if _RE_SOE.search(name):
        return "SoE", "'SoE' in name"
    if _RE_DRAWING_LIST.search(name):
        return "drawing list", "'Drawing List' / 'DWG list' in name"
    if "comment" in lower:
        return "comment register", "'comment' in name"
    if _RE_LAYOUT_CODE.search(name) or "layout" in lower:
        return "layout plan", "AR-IP-01-/AR-OP-01- code or 'layout' in name"
    if _RE_ELEVATION_CODE.search(name):
        return "elevation", "AR-IP-31/AR-OP-31 code in name"
    if _RE_SCHEMATIC_CODE.search(name):
        return "schematic", "IP-BH-/HS-BH- code in name"
    if ext in ("msg", "eml"):
        return "correspondence", f"extension .{ext}"
    if ext in ("jpg", "jpeg", "png", "heic"):
        return "photo", f"extension .{ext}"
    if ext in ("xlsx", "xlsm", "xls"):
        return "workbook", f"extension .{ext}"
    if ext == "pdf":
        return "pdf document", "extension .pdf with no stronger rule"
    if ext == "pptx":
        return "presentation", "extension .pptx with no stronger rule"
    return "other", f"no rule matched (extension '{ext or 'none'}')"


def classify_document(rel_path: str) -> str:
    """Return only the ``doc_class`` for ``rel_path`` (see :func:`classify_with_reason`)."""
    return classify_with_reason(rel_path)[0]


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def _cmd_paths() -> int:
    """Print the resolved path table."""
    for key, value in sorted(PATHS.items()):
        print(f"{key:10s} {value}")
    return 0


def _cmd_classify(rel_paths: List[str], as_json: bool) -> int:
    """Print department, stage and classification for each path."""
    for rel_path in rel_paths:
        doc_class, reason = classify_with_reason(rel_path)
        record = {
            "rel_path": rel_path,
            **parse_department(rel_path),
            "stage": parse_stage(rel_path),
            "doc_class": doc_class,
            "reason": reason,
        }
        if as_json:
            print(json.dumps(record, ensure_ascii=False))
        else:
            print(f"{doc_class:16s} {record['dept_no'] or '--':2s} {record['stage']:26s} {rel_path}  ({reason})")
    return 0


def _cmd_hash(files: List[str]) -> int:
    """Print SHA-256 and MD5 for local files; non-zero if any is missing."""
    failures = 0
    for item in files:
        path = Path(item)
        if not path.is_file():
            print(f"missing: {path}", file=sys.stderr)
            failures += 1
            continue
        print(f"{sha256_file(path)}  {md5_file(path)}  {path}")
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    """Argument parser for the small diagnostic command line."""
    parser = argparse.ArgumentParser(description="Shared helpers for the AIGH pipeline (diagnostics).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("paths", help="print resolved project paths")
    classify = sub.add_parser("classify", help="classify one or more relative paths")
    classify.add_argument("rel_paths", nargs="+")
    classify.add_argument("--json", action="store_true", help="emit one JSON object per line")
    hash_cmd = sub.add_parser("hash", help="print SHA-256 and MD5 of local files")
    hash_cmd.add_argument("files", nargs="+")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point; returns a process exit code."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "paths":
            return _cmd_paths()
        if args.command == "classify":
            return _cmd_classify(args.rel_paths, args.json)
        if args.command == "hash":
            return _cmd_hash(args.files)
    except Exception as exc:  # noqa: BLE001 - surface any failure as a non-zero exit
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
