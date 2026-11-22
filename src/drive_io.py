"""Google Drive input/output for the AIGH IP ACMV pipeline.

This is the ONLY module that touches Drive.  Two transports exist:

``rclone``
    Chosen automatically when ``rclone listremotes`` shows both ``aigh_src:``
    and ``aigh_md:``.  Exactly the commands from the brief are run (lsjson,
    copy, check); nothing else.

``connector``
    Otherwise.  The Google Drive connector of the Claude session lists
    metadata and saves downloads of at most 10 MiB to disk.  It cannot be
    called from Python, so here the Python side *plans* (``pull`` writes
    ``state/pull_plan.csv``), *ingests* saved blobs (``ingest`` /
    ``ingest-dir``) and *verifies* (``verify``) while agents do the fetching.

Verbs: ``doctor``, ``list``, ``pull``, ``push-markdown`` plus the helpers
``ingest``, ``ingest-dir``, ``verify``, ``record-push``, ``lock``, ``unlock``.

Everything is idempotent and resumable; all state lives in files under
``state/``.  Nothing under ``data/source`` is ever deleted except a
just-ingested copy whose size did not match Drive's ``fileSize`` (and the
blob's size is checked before anything is written, so that case only arises
when the blob changes under us).
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:  # package import (python -m src.drive_io, tests)
    from src import common
    from src.state import Progress, file_lock
except ModuleNotFoundError:  # run as a plain script from inside src/
    import common  # type: ignore[no-redef]
    from state import Progress, file_lock  # type: ignore[no-redef,attr-defined]

FOLDER_MIME = "application/vnd.google-apps.folder"
CONNECTOR_LIMIT = common.CONNECTOR_LIMIT_BYTES
SRC_REMOTE = "aigh_src"
MD_REMOTE = "aigh_md"
MARKDOWN_DRIVE_ROOT = "MarkDown"

PLAN_COLUMNS = ("drive_id", "rel_path", "bytes", "mime_type", "transport_eligible", "status")
PUSH_PLAN_COLUMNS = ("rel_path", "sha256", "bytes", "drive_path", "status")
LEDGER_FIELDS = (
    "drive_id", "rel_path", "local_path", "bytes", "sha256", "md5", "drive_md5",
    "size_match", "md5_match", "fetched_at", "transport",
)
REQUIRED_PACKAGES: Dict[str, str] = {
    # requirements.lock name -> import name
    "markitdown": "markitdown",
    "pymupdf": "fitz",
    "pdfplumber": "pdfplumber",
    "openpyxl": "openpyxl",
    "python-pptx": "pptx",
    "pandas": "pandas",
    "ezdxf": "ezdxf",
    "shapely": "shapely",
    "pyyaml": "yaml",
}
# config key -> (root folder name on disk / in rel_path, exclude zips?)
SOURCE_ROOTS: Tuple[Tuple[str, str, bool], ...] = (
    ("dd_signoff", common.ROOT_DD, True),
    ("template", common.ROOT_TEMPLATE, False),
    ("basis", common.ROOT_BASIS, False),
)
SUPERSEDED_RE = re.compile(r"\.superseded\.[0-9a-f]{8}$")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_PROBE_DIRS = 256


# --------------------------------------------------------------------------- #
# Paths and small utilities
# --------------------------------------------------------------------------- #
def paths() -> Dict[str, Path]:
    """Project paths, resolved now so that ``AIGH_ROOT`` is honoured."""
    return common.resolve_paths()


def state_dir() -> Path:
    """``state/`` for the current project root."""
    return paths()["state"]


def source_dir() -> Path:
    """``data/source`` for the current project root."""
    return paths()["source"]


def markdown_dir() -> Path:
    """``out/markdown`` for the current project root."""
    return paths()["markdown"]


def pull_plan_path() -> Path:
    """``state/pull_plan.csv``."""
    return state_dir() / "pull_plan.csv"


def pull_ledger_path() -> Path:
    """``state/pull_ledger.jsonl``."""
    return state_dir() / "pull_ledger.jsonl"


def upload_ledger_path() -> Path:
    """``state/upload_ledger.jsonl``: files the engineer uploaded by hand (over the connector limit)."""
    return state_dir() / "upload_ledger.jsonl"


def push_plan_path() -> Path:
    """``state/push_plan.csv``."""
    return state_dir() / "push_plan.csv"


def push_ledger_path() -> Path:
    """``state/push_ledger.jsonl``."""
    return state_dir() / "push_ledger.jsonl"


def lock_state_path() -> Path:
    """``state/source_lock.json``."""
    return state_dir() / "source_lock.json"


def progress() -> Progress:
    """Progress tracker bound to the current project root."""
    return Progress(state_dir() / "progress.json")


def _log(line: str) -> None:
    """Append one timestamped line to ``logs/drive_io.log`` (best effort)."""
    try:
        log_dir = paths().get("logs", common.project_root() / "logs")
        common.ensure_dir(log_dir)
        with (log_dir / "drive_io.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{common.utc_now_iso()}\t{line}\n")
    except OSError:
        pass


def _run(argv: Sequence[str], cwd: Optional[Path] = None, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    """Run an external command, log it, and return the completed process (never raises on exit code)."""
    _log("run: " + " ".join(argv))
    try:
        return subprocess.run(list(argv), cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        _log(f"run failed: {exc}")
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce Drive's string sizes (and None) to ``int``."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_iso(stamp: str) -> datetime:
    """Parse an ISO-8601 UTC stamp with or without fractional seconds."""
    text = stamp.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _listed_at(entry: Dict[str, Any]) -> datetime:
    """``listed_at`` of a listing entry as an aware datetime (the epoch when missing or unparsable)."""
    try:
        return _parse_iso(str(entry.get("listed_at") or ""))
    except ValueError:
        return _EPOCH


def _read_json_safe(path: Path) -> Dict[str, Any]:
    """Read a small JSON state file; a missing, empty or corrupt file is ``{}`` (never raises)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError) as exc:
        _log(f"unreadable state file {path}: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def rel_path_is_safe(rel_path: str) -> bool:
    """True when ``rel_path`` is a plain relative path: no empty, ``.`` or ``..`` segments and not absolute."""
    text = str(rel_path)
    if not text or text.startswith("/") or "\0" in text:
        return False
    return all(seg not in ("", ".", "..") for seg in text.split("/"))


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
def rclone_remotes() -> List[str]:
    """Remote names reported by ``rclone listremotes`` (empty when rclone is absent)."""
    proc = _run(["rclone", "listremotes"])
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip().endswith(":")]


def detect_transport() -> str:
    """``rclone`` when both project remotes exist, otherwise ``connector``."""
    remotes = set(rclone_remotes())
    return "rclone" if {f"{SRC_REMOTE}:", f"{MD_REMOTE}:"} <= remotes else "connector"


def drive_folder_ids() -> Dict[str, str]:
    """``{config key: Drive folder id}`` from ``config.drive.folders``."""
    folders = common.load_config().get("drive", {}).get("folders", {}) or {}
    return {key: str(value.get("id", "")) for key, value in folders.items() if isinstance(value, dict)}


def rclone_source(folder_id: str) -> str:
    """The rclone remote string for a source folder, exactly as the brief writes it."""
    return f"{SRC_REMOTE},root_folder_id={folder_id}:"


# --------------------------------------------------------------------------- #
# Listings
# --------------------------------------------------------------------------- #
def listings_dir() -> Path:
    """``state/listings``."""
    return state_dir() / "listings"


def listing_files() -> List[Path]:
    """Consolidated listings, oldest first (names sort chronologically)."""
    if not listings_dir().is_dir():
        return []
    return sorted(listings_dir().glob("listing_*.json"))


def latest_listing() -> Optional[Dict[str, Any]]:
    """The most recent consolidated listing, or ``None``."""
    files = listing_files()
    if not files:
        return None
    return json.loads(files[-1].read_text(encoding="utf-8"))


def load_listing(path: Path) -> Dict[str, Any]:
    """Read one consolidated listing file."""
    return json.loads(path.read_text(encoding="utf-8"))


def capture_scope(entry: Dict[str, Any]) -> str:
    """The folder a listing entry is captured under: ``<root>/<top-level folder>`` or the root itself.

    Captures are reconciled per scope so that a department re-listed later
    replaces its earlier rows (a file that vanished is dropped, not merged),
    while a partial second capture of one folder leaves the others alone.
    """
    segs = [s for s in str(entry.get("rel_path", "")).split("/") if s]
    if not segs:
        return ""
    is_folder = entry.get("mimeType") == FOLDER_MIME
    if len(segs) == 1 or (len(segs) == 2 and not is_folder):
        return segs[0]
    return f"{segs[0]}/{segs[1]}"


def capture_window_minutes() -> float:
    """Rows of one scope listed within this many minutes of its newest row belong to the same capture.

    Half the configured listing interval: two deliberate captures are at least
    the interval apart, while the pages of one capture are seconds apart.
    """
    interval = common.load_config().get("drive", {}).get("listing_interval_minutes", 15)
    return max(float(interval), 1.0) / 2.0


def current_capture_rows(rows_by_file: Dict[str, List[Dict[str, Any]]], window: timedelta) -> List[Dict[str, Any]]:
    """Rows of one scope from the JSONL files whose newest ``listed_at`` is within ``window`` of the latest."""
    newest = {name: max((_listed_at(r) for r in rows), default=_EPOCH) for name, rows in rows_by_file.items()}
    latest = max(newest.values(), default=_EPOCH)
    kept: List[Dict[str, Any]] = []
    for name, rows in rows_by_file.items():
        if latest - newest[name] <= window:
            kept.extend(rows)
    return kept


def read_raw_jsonl_listing(window_minutes: Optional[float] = None,
                           ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any], Dict[str, int]]:
    """Consolidate ``state/drive_listing/*.jsonl``: ``(files, folders, inputs, stats)`` deduplicated by id.

    Rows are grouped by capture scope (see :func:`capture_scope`) and JSONL
    file; within a scope only the latest capture survives, so a file absent
    from a department's second capture is dropped rather than carried over.
    When an id still appears more than once the entry with the latest
    ``listed_at`` wins.  ``inputs`` records the name, size and SHA-256 of each
    JSONL so a rerun on unchanged inputs can be recognised; ``stats`` counts
    stale rows dropped, rows without an id and unsafe ``rel_path`` values.
    """
    raw_dir = state_dir() / "drive_listing"
    window = timedelta(minutes=window_minutes if window_minutes is not None else capture_window_minutes())
    inputs: Dict[str, Any] = {}
    scopes: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    stats = {"rows": 0, "stale_dropped": 0, "no_id": 0, "unsafe_rel_path": 0}
    for jsonl in sorted(raw_dir.glob("*.jsonl")) if raw_dir.is_dir() else []:
        inputs[jsonl.name] = {"bytes": jsonl.stat().st_size, "sha256": common.sha256_file(jsonl)}
        for entry in common.read_jsonl(jsonl):
            if not isinstance(entry, dict) or not entry.get("id"):
                stats["no_id"] += 1
                continue
            stats["rows"] += 1
            scopes.setdefault(capture_scope(entry), {}).setdefault(jsonl.name, []).append(entry)
    by_id: Dict[str, Dict[str, Any]] = {}
    for rows_by_file in scopes.values():
        current = current_capture_rows(rows_by_file, window)
        stats["stale_dropped"] += sum(len(r) for r in rows_by_file.values()) - len(current)
        for entry in current:
            previous = by_id.get(entry["id"])
            if previous is None or _listed_at(entry) >= _listed_at(previous):
                by_id[entry["id"]] = entry
    stats["unsafe_rel_path"] = sum(1 for e in by_id.values() if not rel_path_is_safe(str(e.get("rel_path", ""))))
    files = [e for e in by_id.values() if e.get("mimeType") != FOLDER_MIME]
    folders = [e for e in by_id.values() if e.get("mimeType") == FOLDER_MIME]
    key = lambda e: str(e.get("rel_path", ""))  # noqa: E731
    return sorted(files, key=key), sorted(folders, key=key), inputs, stats


def _rclone_entry(root_name: str, item: Dict[str, Any], listed_at: str) -> Dict[str, Any]:
    """Map one ``rclone lsjson`` item to the shared listing shape."""
    rel = f"{root_name}/{item.get('Path', '')}"
    name = item.get("Name") or rel.rsplit("/", 1)[-1]
    entry: Dict[str, Any] = {
        "id": item.get("ID"),
        "title": name,
        "mimeType": item.get("MimeType") or "application/octet-stream",
        "fileSize": str(_as_int(item.get("Size"))),
        "fileExtension": common.file_ext(name),
        "modifiedTime": item.get("ModTime"),
        "rel_path": rel,
        "listed_at": listed_at,
    }
    hashes = {str(k).lower(): v for k, v in (item.get("Hashes") or {}).items()}
    md5 = hashes.get("md5")  # rclone emits ``Hashes.MD5``; older notes wrote ``md5``
    if md5:
        entry["md5Checksum"] = str(md5)
    return entry


def read_rclone_listing() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """Run ``rclone lsjson -R --files-only --hash`` per source folder and map to the listing shape."""
    listed_at = common.utc_now_iso()
    files: List[Dict[str, Any]] = []
    folder_paths: set[str] = set()
    ids = drive_folder_ids()
    for key, root_name, _exclude in SOURCE_ROOTS:
        proc = _run(["rclone", "lsjson", "-R", "--files-only", "--hash", rclone_source(ids[key])])
        if proc.returncode != 0:
            raise RuntimeError(f"rclone lsjson failed for {root_name}: {proc.stderr.strip()}")
        for item in json.loads(proc.stdout or "[]"):
            entry = _rclone_entry(root_name, item, listed_at)
            files.append(entry)
            parts = entry["rel_path"].split("/")
            folder_paths.update("/".join(parts[:n]) for n in range(1, len(parts)))
    folders = [{"id": None, "title": p.rsplit("/", 1)[-1], "mimeType": FOLDER_MIME, "rel_path": p, "listed_at": listed_at}
               for p in sorted(folder_paths)]
    return sorted(files, key=lambda e: e["rel_path"]), folders, {"transport": "rclone"}


def _listing_stamp(path: Path) -> Optional[datetime]:
    """The timestamp encoded in a ``listing_<stamp>.json`` name, or ``None``."""
    try:
        return datetime.strptime(path.stem[len("listing_"):], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _new_listing_path(capture: datetime) -> Path:
    """Pick ``listing_<stamp>.json`` from the capture time, kept strictly after every existing name.

    Names must sort chronologically (``latest_listing`` relies on it), so a
    capture older than the newest listing on disk is bumped past it, and the
    stamp is bumped by a second until the name is free.
    """
    stamps = [s for s in (_listing_stamp(p) for p in listing_files()) if s is not None]
    candidate = capture.replace(microsecond=0)
    if stamps and candidate <= max(stamps):
        candidate = max(stamps) + timedelta(seconds=1)
    while True:
        path = listings_dir() / f"listing_{candidate.strftime('%Y%m%dT%H%M%SZ')}.json"
        if not path.exists():
            return path
        candidate += timedelta(seconds=1)


def capture_time(files: List[Dict[str, Any]], folders: List[Dict[str, Any]]) -> datetime:
    """When the listing was captured on Drive: the newest ``listed_at`` (the wall clock if none)."""
    stamps = [_listed_at(e) for e in [*files, *folders]]
    latest = max(stamps, default=_EPOCH)
    return latest if latest > _EPOCH else datetime.now(timezone.utc)


def write_listing(files: List[Dict[str, Any]], folders: List[Dict[str, Any]], inputs: Dict[str, Any],
                  force: bool = False) -> Tuple[Path, bool]:
    """Write a consolidated listing unless the inputs are unchanged since the latest one.

    ``taken_at`` is the capture time (newest ``listed_at``), so ``minutes_apart``
    measures the interval between Drive captures, not between ``list`` runs;
    ``written_at`` is the wall clock.  Returns ``(path, written)``; when not
    written ``path`` is the existing latest listing.
    """
    existing = listing_files()
    if existing and not force:
        last = load_listing(existing[-1])
        if last.get("inputs") == inputs and inputs:
            return existing[-1], False
    taken = capture_time(files, folders)
    path = _new_listing_path(taken)
    doc = {"taken_at": taken.strftime("%Y-%m-%dT%H:%M:%SZ"), "written_at": common.utc_now_iso(),
           "inputs": inputs, "files": files, "folders": folders}
    common.atomic_write_json(path, doc)
    return path, True


def department_key(rel_path: str) -> Optional[str]:
    """Top-level folder name under ``DD Sign-OFF`` (``None`` outside that root)."""
    segs = [s for s in rel_path.split("/") if s]
    if len(segs) < 2 or segs[0] != common.ROOT_DD:
        return None
    folder = segs[1]
    if len(segs) == 2 and common.is_zip(folder):
        folder = folder[: -len(".zip")]
    return folder


def department_signatures(listing: Dict[str, Any]) -> Dict[str, set]:
    """``{department: {(id, modifiedTime, fileSize), ...}}`` over the files of a listing."""
    sigs: Dict[str, set] = {}
    for entry in listing.get("files", []):
        key = department_key(str(entry.get("rel_path", "")))
        if key is None:
            continue
        sigs.setdefault(key, set()).add((entry.get("id"), entry.get("modifiedTime"), str(entry.get("fileSize", ""))))
    for entry in listing.get("folders", []):
        key = department_key(str(entry.get("rel_path", "")))
        if key is not None:
            sigs.setdefault(key, set())
    return sigs


def compare_listings(previous: Dict[str, Any], current: Dict[str, Any], names: Tuple[str, str]) -> Dict[str, Any]:
    """Stable/unstable departments between two listings (a changed signature set is unstable)."""
    before, after = department_signatures(previous), department_signatures(current)
    departments = sorted(set(before) | set(after))
    unstable = [d for d in departments if before.get(d) != after.get(d)]
    stable = [d for d in departments if d not in unstable]
    try:
        minutes = round((_parse_iso(current["taken_at"]) - _parse_iso(previous["taken_at"])).total_seconds() / 60.0, 2)
    except (KeyError, ValueError):
        minutes = None
    return {"compared": [names[0], names[1]], "minutes_apart": minutes, "unstable": unstable, "stable": stable}


def cmd_list(args: argparse.Namespace) -> int:
    """``list``: consolidate the listing, then compare with the previous one for stability."""
    transport = args.transport or detect_transport()
    stats: Dict[str, int] = {}
    if transport == "rclone":
        files, folders, inputs = read_rclone_listing()
        inputs["taken"] = common.utc_now_iso()  # every rclone run is a fresh snapshot
    else:
        files, folders, inputs, stats = read_raw_jsonl_listing(getattr(args, "capture_window", None))
    if not files and not folders:
        where = "rclone lsjson returned nothing" if transport == "rclone" else "state/drive_listing/*.jsonl is empty"
        print(f"error: no listing data found ({where})", file=sys.stderr)
        return 1
    path, written = write_listing(files, folders, inputs, force=args.force)
    verb = "wrote" if written else "unchanged, kept"
    print(f"{verb} {path.relative_to(common.project_root())}: {len(files)} files, {len(folders)} folders ({transport})")
    if stats.get("stale_dropped"):
        print(f"  {stats['stale_dropped']} rows from earlier captures superseded by a later capture of the same folder")
    if stats.get("no_id"):
        print(f"  warning: {stats['no_id']} rows without an id were ignored")
    if stats.get("unsafe_rel_path"):
        print(f"  warning: {stats['unsafe_rel_path']} entries have an unsafe rel_path (empty, '.' or '..' segment); "
              f"ingest will refuse them")
    all_listings = listing_files()
    if len(all_listings) >= 2:
        prev_path, cur_path = all_listings[-2], all_listings[-1]
        report = compare_listings(load_listing(prev_path), load_listing(cur_path), (prev_path.name, cur_path.name))
        common.atomic_write_json(state_dir() / "unstable_departments.json", report)
        print(f"compared {prev_path.name} with {cur_path.name} ({report['minutes_apart']} minutes apart): "
              f"{len(report['stable'])} stable, {len(report['unstable'])} unstable")
        for dept in report["unstable"]:
            print(f"  unstable: {dept}")
    else:
        print("only one consolidated listing so far; run list again after the listing interval to test stability")
    progress().set("drive_io:list", "done", hash=path.name, transport=transport)
    return 0


# --------------------------------------------------------------------------- #
# Ledger helpers
# --------------------------------------------------------------------------- #
def ledger_rows(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """All pull-ledger rows (append-only; the last row per drive_id is current)."""
    return [row for row in common.read_jsonl(path or pull_ledger_path()) if isinstance(row, dict)]


def ledger_by_id(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """``{drive_id: latest ledger row}``."""
    latest: Dict[str, Dict[str, Any]] = {}
    for row in ledger_rows(path):
        if row.get("drive_id"):
            latest[str(row["drive_id"])] = row
    return latest


def upload_index(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Upload-ledger rows keyed by ``id:<drive_id>`` and ``path:<rel_path>`` (later rows win).

    A file in the upload ledger was supplied by hand because the connector
    could not fetch it, so the pull plan treats it as fetched (transport
    ``upload``) even when, as for a working copy assembled from uploaded parts,
    its bytes cannot match Drive's size.
    """
    index: Dict[str, Dict[str, Any]] = {}
    for row in ledger_rows(path or upload_ledger_path()):
        if row.get("drive_id"):
            index["id:" + str(row["drive_id"])] = row
        if row.get("rel_path"):
            index["path:" + str(row["rel_path"])] = row
    return index


def uploaded_row(entry: Dict[str, Any], uploads: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The upload-ledger row for a listing entry (by Drive id, else by ``rel_path``), or ``None``."""
    return uploads.get("id:" + str(entry.get("id"))) or uploads.get("path:" + str(entry.get("rel_path", "")))


def append_ledger(row: Dict[str, Any]) -> None:
    """Append one row to the pull ledger under the shared lock."""
    with file_lock(pull_ledger_path()):
        common.append_jsonl(pull_ledger_path(), {k: row.get(k) for k in LEDGER_FIELDS})


def listing_index(listing: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """``{id: file entry}`` for the files of a listing."""
    return {str(e["id"]): e for e in listing.get("files", []) if e.get("id")}


def local_rel_path(entry: Dict[str, Any], listing: Dict[str, Any]) -> str:
    """Where a listing entry lives under ``data/source``.

    Drive allows two files with the same path; the first id (sorted) keeps the
    plain path and later ones get ``<stem>~<id8><suffix>`` so neither is lost.
    """
    rel = str(entry["rel_path"])
    twins = sorted(str(e["id"]) for e in listing.get("files", []) if e.get("rel_path") == rel and e.get("id"))
    if len(twins) <= 1 or twins[0] == str(entry["id"]):
        return rel
    head, _, name = rel.rpartition("/")
    stem, dot, ext = name.rpartition(".")
    tagged = f"{stem}~{str(entry['id'])[:8]}.{ext}" if dot and stem else f"{name}~{str(entry['id'])[:8]}"
    return f"{head}/{tagged}" if head else tagged


# --------------------------------------------------------------------------- #
# Pull plan
# --------------------------------------------------------------------------- #
def ledger_row_current(row: Optional[Dict[str, Any]], entry: Dict[str, Any]) -> bool:
    """True when the ledger row is a good fetch of the file as the listing now describes it.

    A row whose size or md5 disagreed with Drive at ingest, or whose bytes/md5
    no longer match the latest listing (the file changed on Drive since the
    fetch), does not count as fetched, so ``pull`` re-plans the file.
    """
    if not row:
        return False
    if row.get("size_match") is False or row.get("md5_match") is False:
        return False
    drive_size = entry.get("fileSize")
    if drive_size is not None and _as_int(drive_size, -1) != _as_int(row.get("bytes"), -2):
        return False
    drive_md5 = entry.get("md5Checksum")
    if drive_md5 and row.get("md5") and str(drive_md5) != str(row.get("md5")):
        return False
    return True


def plan_status(entry: Dict[str, Any], ledger: Dict[str, Dict[str, Any]], transport: str = "connector",
                uploads: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    """Status of one listing file for the pull plan: zip, then fetched (pull or upload ledger), then the size rule.

    ``uploads`` is :func:`upload_index`; a file it holds is ``fetched`` whatever
    its size, since the upload is the transport the connector limit called for.
    """
    if common.is_zip(str(entry.get("rel_path", ""))):
        return "skipped_zip"
    if ledger_row_current(ledger.get(str(entry.get("id"))), entry):
        return "fetched"
    if uploads and uploaded_row(entry, uploads) is not None:
        return "fetched"
    if transport == "rclone" or _as_int(entry.get("fileSize")) <= CONNECTOR_LIMIT:
        return "pending"
    return "blocked_connector_limit"


def build_pull_plan(listing: Dict[str, Any], transport: str) -> List[Dict[str, Any]]:
    """Pull-plan rows for every non-folder entry of ``listing`` (pull and upload ledgers count as fetched)."""
    ledger = ledger_by_id()
    uploads = upload_index()
    rows: List[Dict[str, Any]] = []
    for entry in listing.get("files", []):
        size = _as_int(entry.get("fileSize"))
        eligible = transport == "rclone" or size <= CONNECTOR_LIMIT
        rows.append({
            "drive_id": entry.get("id"),
            "rel_path": entry.get("rel_path"),
            "bytes": size,
            "mime_type": entry.get("mimeType", ""),
            "transport_eligible": "y" if eligible else "n",
            "status": plan_status(entry, ledger, transport, uploads),
        })
    return rows


def write_pull_plan(rows: Iterable[Dict[str, Any]]) -> Path:
    """Write ``state/pull_plan.csv`` under the shared lock."""
    with file_lock(pull_plan_path()):
        return common.write_csv_dicts(pull_plan_path(), rows, PLAN_COLUMNS)


def mark_plan_fetched(drive_id: str) -> None:
    """Set the plan row for ``drive_id`` to ``fetched`` (no-op when the plan or row is absent)."""
    with file_lock(pull_plan_path()):
        rows = common.read_csv_dicts(pull_plan_path())
        changed = False
        for row in rows:
            if row.get("drive_id") == drive_id and row.get("status") != "fetched":
                row["status"] = "fetched"
                changed = True
        if changed:
            common.write_csv_dicts(pull_plan_path(), rows, PLAN_COLUMNS)


def _count_by(rows: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    """Counts of ``rows`` grouped by ``row[key]``."""
    counts: Dict[str, int] = {}
    for row in rows:
        value = str(row.get(key, ""))
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def print_plan_summary(rows: List[Dict[str, Any]]) -> None:
    """Human summary of the pull plan: by status and by root."""
    print("pull plan by status:")
    for status, count in _count_by(rows, "status").items():
        print(f"  {status:24s} {count:6d}")
    print("pull plan by root:")
    by_root = _count_by([{"root": common.parse_department(str(r["rel_path"]))["root"]} for r in rows], "root")
    for root, count in by_root.items():
        print(f"  {root:24s} {count:6d}")
    pending = [r for r in rows if r["status"] == "pending"]
    blocked = [r for r in rows if r["status"] == "blocked_connector_limit"]
    print(f"{len(pending)} pending, {len(blocked)} blocked by the connector limit ({CONNECTOR_LIMIT} bytes)")
    ledger = ledger_by_id()
    refetch = [r for r in pending if str(r.get("drive_id")) in ledger]
    if refetch:
        print(f"{len(refetch)} pending rows are re-fetches (changed on Drive since the fetch, or mismatched at "
              f"ingest); ingest them with --force")


CONNECTOR_INSTRUCTION = """\
INSTRUCTION (connector mode): an agent must fetch every row of state/pull_plan.csv whose status is
'pending' through the Google Drive connector (download by drive_id, save the blob to a scratch directory),
then run for each saved file:
    python -m src.drive_io ingest --blob <saved file> --drive-id <drive_id> --move
or for a batch:
    python -m src.drive_io ingest-dir --dir <scratch dir> --map <blob-name-to-drive_id json or csv> --move
Rows with status 'blocked_connector_limit' exceed the connector's 10485760-byte limit and need the
transport chosen in decisions_needed D-001 (upload or rclone). A pending row whose file already exists under
data/source is a re-fetch (changed on Drive, or mismatched at ingest): ingest it with --force.
Finish with: python -m src.drive_io verify
"""


def rclone_pull_commands(root_dir: Path) -> List[Tuple[str, List[str], List[str]]]:
    """``(root name, copy argv, check argv)`` per source root, exactly as the brief specifies."""
    ids = drive_folder_ids()
    commands: List[Tuple[str, List[str], List[str]]] = []
    for key, root_name, exclude in SOURCE_ROOTS:
        source, dest = rclone_source(ids[key]), str(root_dir / root_name)
        flags = ["--exclude", "*.zip", "--ignore-case"] if exclude else []
        commands.append((root_name, ["rclone", "copy", source, dest, *flags],
                         ["rclone", "check", source, dest, "--one-way", *flags]))
    return commands


def ingest_rclone_tree(listing: Dict[str, Any]) -> int:
    """After an rclone copy, append a ledger row (with hashes) for every copied file not yet in the ledger."""
    by_rel = {str(e.get("rel_path")): e for e in listing.get("files", [])}
    known = ledger_by_id()
    root, added = source_dir(), 0
    for path in sorted(p for p in root.rglob("*")
                       if p.is_file() and not p.name.startswith(".") and not SUPERSEDED_RE.search(p.name)):
        rel = path.relative_to(root).as_posix()
        entry = by_rel.get(rel)
        drive_id = str(entry.get("id")) if entry else f"path:{rel}"
        if drive_id in known:
            continue
        append_ledger(ledger_row(drive_id, rel, path, entry or {}, "rclone"))
        added += 1
    return added


def cmd_pull(args: argparse.Namespace) -> int:
    """``pull``: unlock, plan (connector) or copy+check+ledger (rclone), then lock again."""
    listing = latest_listing()
    if listing is None:
        print("error: no consolidated listing; run `list` first", file=sys.stderr)
        return 1
    transport = args.transport or detect_transport()
    unlock_source()
    try:
        if transport == "rclone":
            return _pull_rclone(listing)
        rows = build_pull_plan(listing, transport)
        write_pull_plan(rows)
        print(f"wrote {pull_plan_path().relative_to(common.project_root())} ({len(rows)} rows)")
        print_plan_summary(rows)
        print(CONNECTOR_INSTRUCTION)
        progress().set("drive_io:pull_plan", "done", hash=common.sha256_file(pull_plan_path()), transport=transport)
        return 0
    finally:
        lock_source()


def _pull_rclone(listing: Dict[str, Any]) -> int:
    """Run the rclone copy and check commands and ledger every copied file."""
    failures = 0
    for root_name, copy_argv, check_argv in rclone_pull_commands(source_dir()):
        for argv in (copy_argv, check_argv):
            proc = _run(argv)
            print(f"{root_name}: {argv[1]} exit {proc.returncode}")
            if proc.returncode != 0:
                failures += 1
                print(proc.stderr.strip(), file=sys.stderr)
    added = ingest_rclone_tree(listing)
    print(f"ledger: {added} new rows, {len(ledger_by_id())} total")
    write_pull_plan(build_pull_plan(listing, "rclone"))
    progress().set("drive_io:pull", "done" if not failures else "failed", transport="rclone")
    return 1 if failures else 0


# --------------------------------------------------------------------------- #
# Ingest (connector mode)
# --------------------------------------------------------------------------- #
def ledger_row(drive_id: str, rel_path: str, local: Path, entry: Dict[str, Any], transport: str) -> Dict[str, Any]:
    """Hash ``local`` and build a ledger row compared against the Drive metadata in ``entry``."""
    size = local.stat().st_size
    drive_size = entry.get("fileSize")
    drive_md5 = entry.get("md5Checksum") or None
    md5 = common.md5_file(local)
    return {
        "drive_id": drive_id,
        "rel_path": rel_path,
        "local_path": local.relative_to(common.project_root()).as_posix(),
        "bytes": size,
        "sha256": common.sha256_file(local),
        "md5": md5,
        "drive_md5": drive_md5,
        "size_match": (_as_int(drive_size, -1) == size) if drive_size is not None else True,
        "md5_match": (md5 == drive_md5) if drive_md5 else None,
        "fetched_at": common.utc_now_iso(),
        "transport": transport,
    }


def safe_dest(rel_path: str) -> Optional[Path]:
    """``data/source/<rel_path>`` when the path stays inside ``data/source``; ``None`` otherwise.

    The listing is agent-written, so ``rel_path`` is checked segment by segment
    and again after resolution (symlinks included) before anything is written.
    """
    if not rel_path_is_safe(rel_path):
        return None
    root = source_dir()
    dest = root / rel_path
    try:
        resolved = dest.resolve()
    except (OSError, RuntimeError):
        return None
    return dest if resolved.is_relative_to(root.resolve()) else None


def _inside_source(path: Path) -> bool:
    """True when ``path`` resolves to somewhere under ``data/source``."""
    try:
        return path.resolve().is_relative_to(source_dir().resolve())
    except (OSError, RuntimeError):
        return False


def _same_file(a: Path, b: Path) -> bool:
    """True when both paths name one and the same existing file."""
    try:
        return a.samefile(b)
    except OSError:
        return False


def _chain_to(dest: Path) -> List[Path]:
    """``data/source`` and every directory from there down to ``dest.parent`` (existing or not)."""
    root = source_dir()
    chain = [root]
    current = root
    for part in dest.parent.relative_to(root).parts:
        current = current / part
        chain.append(current)
    return chain


def _writable_chain(dest: Path) -> List[Path]:
    """Make every existing directory from ``data/source`` down to ``dest.parent`` writable, create the rest.

    ``lock`` may have made the whole tree immutable; a connector ingest only
    needs the one branch it writes to, and :func:`relock_branch` restores the
    lock on exactly that branch afterwards.  Returns the directories touched.
    """
    common.ensure_dir(source_dir())
    chain = _chain_to(dest)
    _make_writable([d for d in chain if d.exists()])
    for directory in chain:
        if not directory.exists():
            directory.mkdir()
    return chain


def _place_blob(blob: Path, dest: Path, move: bool) -> None:
    """Copy or move ``blob`` to ``dest`` (parents created and made writable for the write)."""
    _writable_chain(dest)
    if move:
        shutil.move(str(blob), str(dest))
    else:
        shutil.copy2(str(blob), str(dest))


def _supersede(dest: Path) -> Path:
    """Rename an existing file to ``<name>.superseded.<sha8>`` and return the new path."""
    sha8 = common.sha256_file(dest)[:8]
    kept = dest.with_name(f"{dest.name}.superseded.{sha8}")
    if not kept.exists():
        _writable_chain(dest)
        _make_writable([dest])
        dest.rename(kept)
    return kept


def _restore_superseded(kept: Optional[Path], dest: Path) -> None:
    """Put a superseded original back at ``dest`` after a failed replacement."""
    if kept is not None and kept.exists() and not dest.exists():
        kept.rename(dest)
        print(f"restored the previous copy of {dest.name}", file=sys.stderr)


def _size_matches(blob: Path, entry: Dict[str, Any]) -> bool:
    """True when the blob's size equals Drive's ``fileSize`` (or Drive gave none)."""
    drive_size = entry.get("fileSize")
    return drive_size is None or _as_int(drive_size, -1) == blob.stat().st_size


def _finish_ingest(drive_id: str, rel: str, dest: Path, entry: Dict[str, Any],
                   kept: Optional[Path]) -> Tuple[int, Dict[str, Any]]:
    """Hash the placed copy, ledger it and decide the exit code (a size mismatch deletes the copy)."""
    row = ledger_row(drive_id, rel, dest, entry, "connector")
    if not row["size_match"]:
        dest.unlink()  # the only deletion under data/source: a just-ingested copy of the wrong size
        _restore_superseded(kept, dest)
        print(f"error: size mismatch for {rel}: Drive {entry.get('fileSize')} bytes, blob {row['bytes']} bytes; "
              f"copy deleted", file=sys.stderr)
        return 2, row
    append_ledger(row)
    progress().set(f"pull:{drive_id}", "fetched" if row["md5_match"] is not False else "md5_mismatch",
                   hash=row["sha256"], rel_path=rel)
    if row["md5_match"] is False:
        print(f"warning: md5 mismatch for {rel}: Drive {row['drive_md5']}, local {row['md5']}; file kept, "
              f"plan row left pending, re-fetch with --force", file=sys.stderr)
        return 3, row
    mark_plan_fetched(drive_id)
    return 0, row


def _ingest_identical(blob: Path, dest: Path, drive_id: str, rel: str, entry: Dict[str, Any], move: bool) -> int:
    """``dest`` already holds the blob's bytes: ledger it if needed, mark fetched, tidy the blob."""
    print(f"already present and identical: {rel}")
    if drive_id not in ledger_by_id():
        append_ledger(ledger_row(drive_id, rel, dest, entry, "connector"))
    mark_plan_fetched(drive_id)
    if move and not _same_file(blob, dest) and not _inside_source(blob):
        blob.unlink()  # the scratch download only; never a file under data/source
    return 0


def ingest_blob(blob: Path, drive_id: str, move: bool = False, force: bool = False,
                listing: Optional[Dict[str, Any]] = None, touched: Optional[List[Path]] = None) -> int:
    """Ingest one saved download into ``data/source``; returns a process exit code.

    0 success (or already present and identical), 1 usage/refusal, 2 size
    mismatch (nothing left behind), 3 md5 mismatch (file kept, not marked
    fetched).  Paths written under ``data/source`` are appended to ``touched``
    so the caller can re-lock exactly that branch.
    """
    listing = listing or latest_listing()
    if listing is None:
        print("error: no consolidated listing; run `list` first", file=sys.stderr)
        return 1
    entry = listing_index(listing).get(drive_id)
    if entry is None:
        print(f"error: drive id {drive_id} not in the latest listing", file=sys.stderr)
        return 1
    if not blob.is_file():
        print(f"error: blob not found: {blob}", file=sys.stderr)
        return 1
    rel = local_rel_path(entry, listing)
    dest = safe_dest(rel)
    if dest is None:
        print(f"refused: rel_path {rel!r} would leave data/source", file=sys.stderr)
        return 1
    if move and _inside_source(blob):
        print("note: the blob is already under data/source; copying instead of moving")
        move = False
    if not _size_matches(blob, entry):
        print(f"error: size mismatch for {rel}: Drive {entry.get('fileSize')} bytes, blob {blob.stat().st_size} "
              f"bytes; nothing written", file=sys.stderr)
        return 2
    kept: Optional[Path] = None
    if dest.exists():
        if common.sha256_file(dest) == common.sha256_file(blob):
            return _ingest_identical(blob, dest, drive_id, rel, entry, move)
        if not force:
            print(f"refused: {rel} exists with a different sha256; use --force to supersede it", file=sys.stderr)
            return 1
        kept = _supersede(dest)
        print(f"superseded: previous copy kept as {kept.name}")
    _place_blob(blob, dest, move)
    if touched is not None:
        touched.extend([*_chain_to(dest), dest, *([kept] if kept else [])])
    code, row = _finish_ingest(drive_id, rel, dest, entry, kept)
    if code == 0:
        print(f"ingested {rel} ({row['bytes']} bytes, sha256 {row['sha256'][:12]}...)")
    return code


def cmd_ingest(args: argparse.Namespace) -> int:
    """``ingest --blob <file> --drive-id <id> [--move] [--force]``; the source lock is restored on the branch written."""
    touched: List[Path] = []
    try:
        code = ingest_blob(Path(args.blob), args.drive_id, move=args.move, force=args.force, touched=touched)
    finally:
        relocked = relock_branch(touched)
    return code if code or relocked else 1


def read_blob_map(path: Path) -> Dict[str, str]:
    """``{blob file name: drive_id}`` from a JSON object/list or a CSV with ``blob``/``drive_id`` columns."""
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
        return {str(item.get("blob") or item.get("file") or item.get("name")): str(item.get("drive_id"))
                for item in data}
    mapping: Dict[str, str] = {}
    for row in common.read_csv_dicts(path):
        name = row.get("blob") or row.get("file") or row.get("name")
        if name and row.get("drive_id"):
            mapping[name] = row["drive_id"]
    return mapping


def cmd_ingest_dir(args: argparse.Namespace) -> int:
    """``ingest-dir --dir <dir> --map <json|csv> [--move] [--force]``: ingest a batch; non-zero if any failed."""
    listing = latest_listing()
    mapping = read_blob_map(Path(args.map))
    if not mapping:
        print(f"error: empty mapping {args.map}", file=sys.stderr)
        return 1
    outcomes: Dict[str, int] = {}
    touched: List[Path] = []
    try:
        for name, drive_id in sorted(mapping.items()):
            code = ingest_blob(Path(args.dir) / name, drive_id, move=args.move, force=args.force, listing=listing,
                               touched=touched)
            outcomes[name] = code
    finally:
        relocked = relock_branch(touched)
    failed = {n: c for n, c in outcomes.items() if c != 0}
    print(f"ingest-dir: {len(outcomes) - len(failed)} ok, {len(failed)} failed")
    for name, code in failed.items():
        print(f"  failed ({code}): {name}")
    return 1 if failed or not relocked else 0


# --------------------------------------------------------------------------- #
# Verify
# --------------------------------------------------------------------------- #
def verify_row(row: Dict[str, Any], entry: Optional[Dict[str, Any]]) -> List[str]:
    """Problems with one ledger row: missing file, hash/size drift, disagreement with the listing."""
    problems: List[str] = []
    local = common.project_root() / str(row.get("local_path", ""))
    if not local.is_file():
        return [f"missing: {row.get('local_path')}"]
    size, sha = local.stat().st_size, common.sha256_file(local)
    if size != _as_int(row.get("bytes"), -1):
        problems.append(f"size differs from ledger: {size} != {row.get('bytes')}")
    if sha != row.get("sha256"):
        problems.append("sha256 differs from ledger")
    if entry is not None:
        if entry.get("fileSize") is not None and _as_int(entry.get("fileSize"), -1) != size:
            problems.append(f"size differs from Drive: {size} != {entry.get('fileSize')}")
        drive_md5 = entry.get("md5Checksum")
        if drive_md5 and common.md5_file(local) != drive_md5:
            problems.append("md5 differs from Drive")
    return problems


def cmd_verify(args: argparse.Namespace) -> int:
    """``verify``: re-hash every ledger file against the ledger and the latest listing; exit 1 on any mismatch."""
    listing = latest_listing() or {"files": []}
    index = listing_index(listing)
    rows = ledger_by_id()
    if not rows:
        print("ledger is empty; nothing to verify")
        return 0
    mismatches = 0
    for drive_id, row in sorted(rows.items(), key=lambda kv: str(kv[1].get("rel_path"))):
        problems = verify_row(row, index.get(drive_id))
        if problems:
            mismatches += 1
            print(f"MISMATCH {row.get('rel_path')}: " + "; ".join(problems))
    print(f"verified {len(rows)} ledger files: {len(rows) - mismatches} ok, {mismatches} mismatched")
    progress().set("drive_io:verify", "done" if not mismatches else "failed", mismatches=mismatches)
    return 1 if mismatches else 0


# --------------------------------------------------------------------------- #
# Push markdown
# --------------------------------------------------------------------------- #
def push_ledger_by_path() -> Dict[str, Dict[str, Any]]:
    """``{rel_path: latest push-ledger row}``."""
    latest: Dict[str, Dict[str, Any]] = {}
    for row in common.read_jsonl(push_ledger_path()):
        if isinstance(row, dict) and row.get("rel_path"):
            latest[str(row["rel_path"])] = row
    return latest


def markdown_files() -> List[Path]:
    """Every regular file under ``out/markdown`` (sorted)."""
    root = markdown_dir()
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file() and not p.name.startswith("."))


def build_push_plan() -> List[Dict[str, Any]]:
    """Push-plan rows: every markdown file, ``pushed`` when the ledger has it with the same sha256."""
    pushed = push_ledger_by_path()
    rows: List[Dict[str, Any]] = []
    for path in markdown_files():
        rel = path.relative_to(markdown_dir()).as_posix()
        sha = common.sha256_file(path)
        done = pushed.get(rel, {}).get("sha256") == sha
        rows.append({"rel_path": rel, "sha256": sha, "bytes": path.stat().st_size,
                     "drive_path": f"{MARKDOWN_DRIVE_ROOT}/{rel}", "status": "pushed" if done else "pending"})
    return rows


def rclone_push_commands() -> List[List[str]]:
    """The two push commands, exactly as the brief specifies (run from the project root)."""
    return [["rclone", "copy", "out/markdown", f"{MD_REMOTE}:"],
            ["rclone", "check", "out/markdown", f"{MD_REMOTE}:", "--one-way"]]


def record_push(rel_path: str, drive_id: Optional[str], transport: str) -> Dict[str, Any]:
    """Append a push-ledger row for ``rel_path`` (sha256 of the local file at this moment)."""
    local = markdown_dir() / rel_path
    if not local.is_file():
        raise FileNotFoundError(f"not under out/markdown: {rel_path}")
    row = {"rel_path": rel_path, "drive_path": f"{MARKDOWN_DRIVE_ROOT}/{rel_path}", "drive_id": drive_id,
           "sha256": common.sha256_file(local), "bytes": local.stat().st_size,
           "pushed_at": common.utc_now_iso(), "transport": transport}
    with file_lock(push_ledger_path()):
        common.append_jsonl(push_ledger_path(), row)
    return row


def cmd_push_markdown(args: argparse.Namespace) -> int:
    """``push-markdown``: rclone copy+check, or a connector push plan."""
    transport = args.transport or detect_transport()
    if transport == "rclone":
        for argv in rclone_push_commands():
            proc = _run(argv, cwd=common.project_root())
            print(f"{argv[1]} exit {proc.returncode}")
            if proc.returncode != 0:
                print(proc.stderr.strip(), file=sys.stderr)
                return 1
        pending = [r for r in build_push_plan() if r["status"] == "pending"]
        for row in pending:
            record_push(row["rel_path"], None, "rclone")
        print(f"push ledger: {len(pending)} new rows")
        return 0
    rows = build_push_plan()
    common.write_csv_dicts(push_plan_path(), rows, PUSH_PLAN_COLUMNS)
    pending_count = sum(1 for r in rows if r["status"] == "pending")
    print(f"wrote {push_plan_path().relative_to(common.project_root())}: {len(rows)} files, {pending_count} pending")
    print("INSTRUCTION (connector mode): an agent copies each pending file to Drive folder MarkDown at drive_path,\n"
          "then runs: python -m src.drive_io record-push --path <rel_path> --drive-id <new file id>")
    return 0


def cmd_record_push(args: argparse.Namespace) -> int:
    """``record-push --path <rel> --drive-id <id>``."""
    row = record_push(args.path, args.drive_id, "connector")
    print(json.dumps(row, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------- #
# Lock / unlock data/source
# --------------------------------------------------------------------------- #
_WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH


def _chmod_one(path: Path, writable: bool) -> None:
    """Add the owner write bit or clear every write bit on one path (symlinks are left alone)."""
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        return
    path.chmod((mode | stat.S_IWUSR) if writable else (mode & ~_WRITE_BITS))


def _chmod_tree(root: Path, writable: bool) -> List[str]:
    """Add or remove write bits on every directory and file under ``root``; return the paths that failed."""
    failures: List[str] = []
    for path in [root, *root.rglob("*")]:
        try:
            _chmod_one(path, writable)
        except OSError as exc:
            failures.append(f"{path}: {exc.strerror or exc}")
    return failures


def _make_writable(paths: Sequence[Path]) -> List[str]:
    """Clear the immutable flag (one ``chattr`` call) and add the owner write bit on each path; return failures."""
    targets = [p for p in paths if p.exists()]
    if not targets:
        return []
    failures: List[str] = []
    proc = _run(["chattr", "-i", *map(str, targets)])
    if proc.returncode != 0:
        _log(f"chattr -i failed: {proc.stderr.strip()}")
    for path in targets:
        try:
            _chmod_one(path, writable=True)
        except OSError as exc:
            failures.append(f"{path}: {exc.strerror or exc}")
    return failures


def _probe_dir(directory: Path) -> bool:
    """True when creating ``.lock_probe`` in ``directory`` fails (the lock bites there, even for root)."""
    probe = directory / ".lock_probe"
    try:
        with probe.open("x"):
            pass
    except OSError:
        return True
    try:
        probe.unlink()  # our own probe, created a moment ago; never a record document
    except OSError:
        pass
    return False


def _probe_enforced(root: Path) -> Dict[str, Any]:
    """Probe the root and (a sample of) its sub-directories: ``{enforced, probed_dirs, writable_dirs}``."""
    directories = [root, *sorted(p for p in root.rglob("*") if p.is_dir() and not p.is_symlink())]
    if len(directories) > _MAX_PROBE_DIRS:
        step = len(directories) / _MAX_PROBE_DIRS
        directories = [directories[int(i * step)] for i in range(_MAX_PROBE_DIRS)]
    writable = [str(d.relative_to(root)) or "." for d in directories if not _probe_dir(d)]
    return {"enforced": not writable, "probed_dirs": len(directories), "writable_dirs": writable[:10]}


def read_lock_state() -> Dict[str, Any]:
    """``state/source_lock.json`` as a dictionary (``{}`` when absent or unreadable)."""
    return _read_json_safe(lock_state_path())


def _all_immutable(root: Path) -> bool:
    """Return True when ``lsattr`` shows the immutable flag on every path under ``root`` (and on ``root``)."""
    proc = _run(["lsattr", "-R", "-a", "-d", str(root)])
    top = _run(["lsattr", "-d", str(root)])
    rec = _run(["lsattr", "-R", "-a", str(root)])
    lines = [l for l in (top.stdout + rec.stdout).splitlines() if l.strip() and not l.endswith(":")]
    entries = [l.split(None, 1) for l in lines]
    entries = [e for e in entries if len(e) == 2 and not e[1].rstrip().endswith(("/.", "/.."))]
    return bool(entries) and all("i" in e[0] for e in entries)


def lock_source() -> Dict[str, Any]:
    """Lock ``data/source``: ``chattr -R +i`` first, else ``chmod -R a-w``; record and verify honestly.

    ``enforced`` is true only when every probed directory refused a new file
    and no path failed to change mode; failures are counted and sampled in the
    state file rather than swallowed.
    """
    root = common.ensure_dir(source_dir())
    proc = _run(["chattr", "-R", "+i", str(root)])
    failures: List[str] = []
    if proc.returncode != 0 and _all_immutable(root):
        # chattr reports EPERM on directories with the filesystem-managed 'I' (indexed)
        # flag even when they are already immutable; read the flags back instead.
        _log("chattr returned non-zero but every path carries the immutable flag: " + proc.stderr.strip())
        proc = subprocess.CompletedProcess(proc.args, 0, proc.stdout, "")
    if proc.returncode == 0:
        method = "chattr"
    else:
        failures = _chmod_tree(root, writable=False)
        method = "chmod"
    probe = _probe_enforced(root)
    notes: List[str] = []
    if method == "chmod":
        notes.append("chattr +i refused: " + proc.stderr.strip())
        if not probe["enforced"]:
            notes.append("root ignores chmod bits, so the lock is advisory for this user")
    if failures:
        notes.append(f"{len(failures)} paths could not be changed")
    state: Dict[str, Any] = {"locked": True, "method": method, "enforced": probe["enforced"] and not failures,
                             "probed_dirs": probe["probed_dirs"], "writable_dirs": probe["writable_dirs"],
                             "failures": len(failures), "failure_sample": failures[:5], "uid": os.getuid(),
                             "note": "; ".join(notes), "at": common.utc_now_iso()}
    common.atomic_write_json(lock_state_path(), state)
    for line in failures[:5]:
        print(f"warning: lock: {line}", file=sys.stderr)
    return state


def unlock_source() -> Dict[str, Any]:
    """Undo :func:`lock_source` (both methods, whichever was recorded) and record the state."""
    root = common.ensure_dir(source_dir())
    previous = read_lock_state()
    notes: List[str] = []
    if previous.get("method") == "chattr" or not previous:
        proc = _run(["chattr", "-R", "-i", str(root)])
        if proc.returncode != 0:
            notes.append("chattr -i failed: " + proc.stderr.strip())
    failures = _chmod_tree(root, writable=True)
    if failures:
        notes.append(f"{len(failures)} paths could not be made writable")
    state = {"locked": False, "method": previous.get("method"), "enforced": False, "failures": len(failures),
             "failure_sample": failures[:5], "note": "; ".join(notes), "at": common.utc_now_iso()}
    common.atomic_write_json(lock_state_path(), state)
    for line in failures[:5]:
        print(f"warning: unlock: {line}", file=sys.stderr)
    return state


def relock_branch(paths: Sequence[Path]) -> bool:
    """Re-apply the recorded lock to just the given paths (plus their ancestors under ``data/source``).

    A single-file ingest opens one branch of a locked tree, so re-locking only
    that branch keeps each ingest O(depth) instead of O(whole tree).  Does
    nothing when the tree is not recorded as locked.  Returns ``False`` (after
    printing a warning) when a path could not be locked again; never raises.
    """
    state = read_lock_state()
    if not state.get("locked") or not paths:
        return True
    root = source_dir()
    targets: List[Path] = []
    for path in paths:
        candidates = [path, *path.parents]
        for candidate in candidates:
            if candidate.exists() and _inside_source(candidate) and candidate not in targets:
                targets.append(candidate)
            if candidate == root:
                break
    failures: List[str] = []
    if state.get("method") == "chattr":
        proc = _run(["chattr", "+i", *map(str, targets)])
        if proc.returncode != 0:
            failures.append("chattr +i: " + proc.stderr.strip())
    else:
        for target in targets:
            try:
                _chmod_one(target, writable=False)
            except OSError as exc:
                failures.append(f"{target}: {exc.strerror or exc}")
    for line in failures:
        print(f"warning: could not re-lock: {line}", file=sys.stderr)
        _log(f"relock failure: {line}")
    return not failures


def cmd_lock(_args: argparse.Namespace) -> int:
    """``lock``."""
    state = lock_source()
    print(json.dumps(state, ensure_ascii=False))
    return 0


def cmd_unlock(_args: argparse.Namespace) -> int:
    """``unlock``."""
    state = unlock_source()
    print(json.dumps(state, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------- #
# Doctor
# --------------------------------------------------------------------------- #
def _package_report() -> Dict[str, Optional[str]]:
    """``{lock name: version or None}`` for the required packages."""
    report: Dict[str, Optional[str]] = {}
    for lock_name, import_name in REQUIRED_PACKAGES.items():
        if importlib.util.find_spec(import_name) is None:
            report[lock_name] = None
            continue
        try:
            report[lock_name] = importlib.metadata.version(lock_name)
        except importlib.metadata.PackageNotFoundError:
            report[lock_name] = "present"
    return report


def _soffice_version() -> Optional[str]:
    """First line of ``soffice --version`` or ``None``."""
    proc = _run(["soffice", "--version"], timeout=90)
    return proc.stdout.strip().splitlines()[0] if proc.returncode == 0 and proc.stdout.strip() else None


def _rclone_version() -> Optional[str]:
    """First line of ``rclone version`` or ``None``."""
    proc = _run(["rclone", "version"])
    return proc.stdout.strip().splitlines()[0] if proc.returncode == 0 and proc.stdout.strip() else None


def _memory_total_bytes() -> Optional[int]:
    """MemTotal from ``/proc/meminfo`` in bytes (``None`` off Linux)."""
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def doctor_report() -> Dict[str, Any]:
    """Collect the environment report as a JSON-serialisable dictionary."""
    root = common.project_root()
    listing = latest_listing()
    lock_state = read_lock_state()
    usage = shutil.disk_usage(root)
    return {
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "packages": _package_report(),
        "soffice": _soffice_version(),
        "rclone": _rclone_version(),
        "rclone_remotes": rclone_remotes(),
        "transport": detect_transport(),
        "disk_free_bytes": usage.free,
        "cpu_count": os.cpu_count(),
        "memory_total_bytes": _memory_total_bytes(),
        "source_locked": bool(lock_state.get("locked")),
        "source_lock": lock_state,
        "listing": {"files": len(listing.get("files", [])), "folders": len(listing.get("folders", [])),
                    "taken_at": listing.get("taken_at")} if listing else None,
        "ledger_files": len(ledger_by_id()),
        "project_root": str(root),
    }


def doctor_problems(report: Dict[str, Any]) -> List[str]:
    """The conditions that make ``doctor`` exit 1."""
    problems: List[str] = []
    if not report["soffice"]:
        problems.append("soffice not found")
    if not report["packages"].get("markitdown"):
        problems.append("markitdown not importable")
    if sys.version_info < (3, 11):
        problems.append(f"python {report['python']} is older than 3.11")
    return problems


def print_doctor_table(report: Dict[str, Any]) -> None:
    """Human-readable table of the doctor report."""
    def gib(value: Optional[int]) -> str:
        return f"{value / 2**30:.1f} GiB" if value else "n/a"
    pkgs = ", ".join(f"{k}={v or 'MISSING'}" for k, v in report["packages"].items())
    rows = [
        ("python", report["python"]), ("packages", pkgs), ("soffice", report["soffice"] or "MISSING"),
        ("rclone", report["rclone"] or "MISSING"), ("remotes", ", ".join(report["rclone_remotes"]) or "none"),
        ("transport", report["transport"]), ("disk free", gib(report["disk_free_bytes"])),
        ("cpus", str(report["cpu_count"])), ("memory", gib(report["memory_total_bytes"])),
        ("source locked", str(report["source_locked"]) + (
            f" (method {report['source_lock'].get('method')}, enforced {report['source_lock'].get('enforced')})"
            if report["source_lock"] else "")),
        ("listing", json.dumps(report["listing"]) if report["listing"] else "none"),
        ("ledger files", str(report["ledger_files"])),
    ]
    for key, value in rows:
        print(f"{key:14s} {value}")


def cmd_doctor(_args: argparse.Namespace) -> int:
    """``doctor``: JSON report, human table, exit 1 when python/soffice/markitdown are missing."""
    report = doctor_report()
    problems = doctor_problems(report)
    report["problems"] = problems
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print()
    print_doctor_table(report)
    for problem in problems:
        print(f"PROBLEM: {problem}", file=sys.stderr)
    return 1 if problems else 0


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """Argument parser with one subcommand per verb."""
    parser = argparse.ArgumentParser(description="Drive input/output for the AIGH pipeline (the only module that touches Drive).")
    parser.add_argument("--transport", choices=("connector", "rclone"), default=None,
                        help="override the automatic transport choice (tests and dry runs)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="environment report; exit 1 when python, soffice or markitdown are missing")
    list_cmd = sub.add_parser("list", help="consolidate the Drive listing and detect unstable departments")
    list_cmd.add_argument("--force", action="store_true", help="write a new snapshot even if the inputs are unchanged")
    list_cmd.add_argument("--capture-window", type=float, default=None, metavar="MINUTES",
                          help="rows of one folder listed within this window count as one capture "
                               "(default: half of drive.listing_interval_minutes)")
    sub.add_parser("pull", help="plan (connector) or copy and check (rclone) the source folders")
    ingest = sub.add_parser("ingest", help="connector mode: ingest one saved download into data/source")
    ingest.add_argument("--blob", required=True, help="the saved download")
    ingest.add_argument("--drive-id", required=True, help="Drive file id of the blob")
    ingest.add_argument("--move", action="store_true", help="move instead of copy")
    ingest.add_argument("--force", action="store_true", help="supersede an existing different copy")
    batch = sub.add_parser("ingest-dir", help="connector mode: ingest a directory of saved downloads")
    batch.add_argument("--dir", required=True)
    batch.add_argument("--map", required=True, help="json or csv mapping blob file name to drive_id")
    batch.add_argument("--move", action="store_true")
    batch.add_argument("--force", action="store_true")
    sub.add_parser("verify", help="re-hash every ledger file against ledger and listing")
    sub.add_parser("push-markdown", help="upload out/markdown (rclone) or write the push plan (connector)")
    rec = sub.add_parser("record-push", help="connector mode: record one pushed markdown file")
    rec.add_argument("--path", required=True, help="path relative to out/markdown")
    rec.add_argument("--drive-id", required=True)
    sub.add_parser("lock", help="make data/source read-only")
    sub.add_parser("unlock", help="make data/source writable again")
    return parser


COMMANDS = {
    "doctor": cmd_doctor, "list": cmd_list, "pull": cmd_pull, "ingest": cmd_ingest, "ingest-dir": cmd_ingest_dir,
    "verify": cmd_verify, "push-markdown": cmd_push_markdown, "record-push": cmd_record_push,
    "lock": cmd_lock, "unlock": cmd_unlock,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point; returns a process exit code (non-zero on any failure)."""
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except Exception as exc:  # noqa: BLE001 - surface any failure as a non-zero exit
        print(f"error: {exc}", file=sys.stderr)
        _log(f"error: {exc!r}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
