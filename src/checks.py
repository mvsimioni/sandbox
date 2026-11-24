"""Pass/fail checks per pipeline phase.

``python -m src.checks phase0`` runs the Phase 0 registry, writes
``out/qa/check_phase0.md`` (one table row per check with its evidence) and
exits 0 only when every non-SKIP check passes.  ``phase1`` checks the stage
1A conversion (``state/convert_ledger.jsonl`` and the ``checks.json`` files
the converter writes next to each sidecar); ``--scope converted`` limits it
to the files converted so far.  Phases 2 to 4 are in phase_checks.py and phase4_checks.py; phase 5 is not yet implemented and
exit 2.

Each check is a small function ``(ctx) -> (status, evidence)`` where status is
``PASS``, ``FAIL`` or ``SKIP`` and evidence is a list of short lines.  Checks
only read the project tree (progress.json is read without taking its lock);
the report is the only file written.  Nothing here touches the network.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

import yaml

try:  # package import (python -m src.checks, tests)
    from src import common, decisions
except ModuleNotFoundError:  # run as a plain script from inside src/
    import common  # type: ignore[no-redef]
    import decisions  # type: ignore[no-redef]

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
FOLDER_MIME = "application/vnd.google-apps.folder"

CheckOutcome = Tuple[str, List[str]]
CheckFunc = Callable[["Context"], CheckOutcome]

BOOTSTRAP_FILES: Tuple[str, ...] = (
    "README.md", "config/project.yaml", "requirements.lock",
    "src/common.py", "src/state.py", "src/drive_io.py", "src/manifest.py", "src/census.py",
    "src/checks.py", "src/pressure.py", "src/blocked_inputs.py",
    "out/qa/decisions_needed.md", "state/progress.json",
)
DRIVE_FOLDER_KEYS: Tuple[str, ...] = ("dd_signoff", "template", "basis", "markdown")
BLOCKED_INPUT_COLUMNS: Tuple[str, ...] = ("rel_path", "bytes", "reason", "needed_for")
BACKGROUND_PDF = "Basis/Arch Combined Tender .pdf"
TEMPLATE_QA_FILES: Tuple[str, ...] = (
    "zoning_sample_description.md", "template_spec_A_RevA.md", "template_spec_B_NDH.md", "template_spec_C_diff.md",
)
CENSUS_KINDS: Tuple[str, ...] = ("pdf", "pptx")
SELFTEST_TIMEOUT_S = 300
MIN_LISTING_GAP_MIN = 15
"""The spec's minimum gap between the two listings; the config may only widen it."""
BLOCKED_STATUS = "blocked_connector_limit"
RESOLVED_STATUSES: Tuple[str, ...] = ("uploaded", "not needed")
"""``status`` values of a blocked_inputs.csv row that no longer waits for the engineer."""
ASSEMBLED_TRANSPORT = "upload-assembled"
"""Ledger transport of a working copy stitched together from uploaded parts of one Drive file."""
TEMPLATE_PROGRESS_KEY = "template_workflow"
"""Progress item set by ``python -m src.template_workflow finish``."""
_LIMIT_REASON_RE = re.compile(r"connector|limit|10485760|10\s*mib|too\s+large|size", re.IGNORECASE)
"""Reason wording that claims a file was blocked by the connector transport limit."""
CONVERT_LEDGER = "convert_ledger.jsonl"
CONVERT_DONE_STATUSES: Tuple[str, ...] = ("converted", "duplicate")
GRADES: Tuple[str, ...] = ("A", "B", "C", "pending")
SCOPES: Tuple[str, ...] = ("all", "converted")
DEFAULT_RECALL = 0.995
MAX_MISSES_SHOWN = 8


class Check(NamedTuple):
    """A registered check: id, short title and the function that runs it."""

    check_id: str
    title: str
    func: CheckFunc


class CheckResult(NamedTuple):
    """The outcome of one check."""

    check_id: str
    title: str
    status: str
    evidence: List[str]


# --------------------------------------------------------------------------- #
# Context: lazily loaded, cached views of the project tree
# --------------------------------------------------------------------------- #
LEDGER_FILES: Tuple[str, ...] = ("pull_ledger.jsonl", "upload_ledger.jsonl")
"""Fetch ledgers read by the checks, in the same order as ``manifest.load_ledger``."""


class Context:
    """Paths and cached artefacts shared by the checks of one run."""

    def __init__(self, root: Optional[Path] = None, scope: str = "all") -> None:
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {SCOPES}, not {scope!r}")
        self.scope: str = scope
        self.root: Path = (root or common.project_root()).resolve()
        self.paths: Dict[str, Path] = common.resolve_paths()
        self.state: Path = self.paths.get("state", self.root / "state")
        self.registers: Path = self.paths.get("registers", self.root / "out" / "registers")
        self.qa: Path = self.paths.get("qa", self.root / "out" / "qa")
        self._cache: Dict[str, Any] = {}

    def _memo(self, key: str, loader: Callable[[], Any]) -> Any:
        """Return ``loader()`` once per key."""
        if key not in self._cache:
            self._cache[key] = loader()
        return self._cache[key]

    def config(self) -> Dict[str, Any]:
        """The parsed ``config/project.yaml``."""
        return self._memo("config", common.load_config)

    def _scan_listings(self) -> Tuple[List[Tuple[datetime, Path]], List[Tuple[Path, str]]]:
        """Every ``listing_*.json``: ``(readable sorted by time, unreadable with reason)``."""
        def scan() -> Tuple[List[Tuple[datetime, Path]], List[Tuple[Path, str]]]:
            good: List[Tuple[datetime, Path]] = []
            bad: List[Tuple[Path, str]] = []
            for path in sorted((self.state / "listings").glob("listing_*.json")):
                taken, problem = _listing_time(path)
                if taken is None:
                    bad.append((path, problem))
                else:
                    good.append((taken, path))
            good.sort(key=lambda item: (item[0], item[1].name))
            return good, bad
        return self._memo("listings", scan)

    def listings(self) -> List[Tuple[datetime, Path]]:
        """Readable consolidated listings as ``(taken_at, path)``, oldest first."""
        return self._scan_listings()[0]

    def unreadable_listings(self) -> List[Tuple[Path, str]]:
        """Files matching ``listing_*.json`` that are not consolidated listings, with the reason."""
        return self._scan_listings()[1]

    def listing_paths(self) -> List[Path]:
        """Paths of the readable consolidated listings, oldest first."""
        return [path for _, path in self.listings()]

    def latest_listing(self) -> Optional[Dict[str, Any]]:
        """The consolidated listing with the latest time (``files`` without folders), or ``None``."""
        def load() -> Optional[Dict[str, Any]]:
            readable = self.listings()
            if not readable:
                return None
            path = readable[-1][1]
            data = json.loads(path.read_text(encoding="utf-8"))
            files = [f for f in data.get("files", []) if isinstance(f, dict) and f.get("mimeType") != FOLDER_MIME]
            return {"path": path, "taken_at": data.get("taken_at", ""), "files": files,
                    "folders": list(data.get("folders", []))}
        return self._memo("latest_listing", load)

    def ledger_rows(self) -> List[Dict[str, Any]]:
        """Rows of ``state/pull_ledger.jsonl`` then ``state/upload_ledger.jsonl`` (dictionaries only).

        The manifest (``manifest.load_ledger``) treats both files as the fetch
        record, so the checks read the same pair; files uploaded by hand are
        otherwise reported as pulled without a ledger row.
        """
        def load() -> List[Dict[str, Any]]:
            rows: List[Dict[str, Any]] = []
            for name in LEDGER_FILES:
                rows.extend(r for r in common.read_jsonl(self.state / name) if isinstance(r, dict))
            return rows
        return self._memo("ledger", load)

    def ledger_index(self) -> Dict[str, Dict[str, Any]]:
        """Ledger rows keyed by ``id:<drive_id>`` and ``path:<rel_path>`` (later rows win)."""
        def build() -> Dict[str, Dict[str, Any]]:
            index: Dict[str, Dict[str, Any]] = {}
            for row in self.ledger_rows():
                if row.get("drive_id"):
                    index["id:" + str(row["drive_id"])] = row
                if row.get("rel_path"):
                    index["path:" + str(row["rel_path"])] = row
            return index
        return self._memo("ledger_index", build)

    def plan_rows(self) -> List[Dict[str, str]]:
        """Rows of ``state/pull_plan.csv``."""
        return self._memo("plan", lambda: common.read_csv_dicts(self.state / "pull_plan.csv"))

    def manifest_rows(self) -> List[Dict[str, str]]:
        """Rows of ``out/registers/manifest.csv``."""
        return self._memo("manifest", lambda: common.read_csv_dicts(self.registers / "manifest.csv"))

    def blocked_inputs(self) -> Optional[List[Dict[str, str]]]:
        """Rows of ``out/qa/blocked_inputs.csv`` (BOM tolerated) or ``None`` when the file is missing."""
        path = self.qa / "blocked_inputs.csv"
        return self._memo("blocked", lambda: _read_human_csv(path) if path.is_file() else None)

    def source_rows(self) -> List[Dict[str, Any]]:
        """Fetched files in scope for conversion: latest ledger row per rel_path, zips left out."""
        def load() -> List[Dict[str, Any]]:
            latest: Dict[str, Dict[str, Any]] = {}
            order: List[str] = []
            for row in self.ledger_rows():
                rel = str(row.get("rel_path") or "")
                if not rel or not row.get("sha256") or common.is_zip(rel):
                    continue
                if rel not in latest:
                    order.append(rel)
                latest[rel] = row
            return [latest[rel] for rel in order]
        return self._memo("source_rows", load)

    def convert_rows(self) -> Dict[str, Dict[str, Any]]:
        """Latest row of ``state/convert_ledger.jsonl`` per ``sha256|rel_path``."""
        def load() -> Dict[str, Dict[str, Any]]:
            latest: Dict[str, Dict[str, Any]] = {}
            for row in common.read_jsonl(self.state / CONVERT_LEDGER):
                if isinstance(row, dict) and row.get("sha256") and row.get("rel_path"):
                    latest[f"{row['sha256']}|{row['rel_path']}"] = row
            return latest
        return self._memo("convert_rows", load)

    def scoped_rows(self) -> List[Dict[str, Any]]:
        """Source rows to judge: all of them, or only those with a convert-ledger row (``--scope converted``)."""
        rows = self.source_rows()
        if self.scope == "converted":
            convert = self.convert_rows()
            rows = [r for r in rows if f"{r['sha256']}|{r['rel_path']}" in convert]
        return rows

    def converted_rows(self) -> List[Dict[str, Any]]:
        """Convert-ledger rows with status ``converted`` for the scoped source rows."""
        convert = self.convert_rows()
        out: List[Dict[str, Any]] = []
        for row in self.scoped_rows():
            record = convert.get(f"{row['sha256']}|{row['rel_path']}")
            if record and record.get("status") == "converted":
                out.append(record)
        return out

    def file_checks(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The ``checks.json`` written for a converted row, or ``None`` when missing or unreadable."""
        checks_path = row.get("checks_path")
        if not checks_path:
            return None
        path = Path(str(checks_path))
        path = path if path.is_absolute() else self.root / path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def recall_threshold(self) -> float:
        """``tolerances.numeric_recall`` from the configuration (default 0.995)."""
        try:
            value = (self.config().get("tolerances") or {}).get("numeric_recall")
            return float(value) if value is not None else DEFAULT_RECALL
        except Exception:  # noqa: BLE001 - a broken config falls back to the brief's value
            return DEFAULT_RECALL

    def progress_item(self, key: str) -> Optional[Dict[str, Any]]:
        """One item of ``state/progress.json`` read without taking the lock (checks only read)."""
        path = self.state / "progress.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError:
            return None
        item = (data.get("items") or {}).get(key) if isinstance(data, dict) else None
        return dict(item) if isinstance(item, dict) else None


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _parse_iso(stamp: str) -> Optional[datetime]:
    """Parse ``YYYY-MM-DDTHH:MM:SSZ`` (or any ISO form) into an aware UTC datetime."""
    text = str(stamp or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _listing_time(path: Path) -> Tuple[Optional[datetime], str]:
    """Time of a consolidated listing, or ``(None, reason)`` when the file is not one.

    The file must be a JSON object with a ``files`` list; its time is
    ``taken_at`` when parseable, else the ``YYYYmmddTHHMMSSZ`` stamp in the name.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"not JSON ({type(exc).__name__})"
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        return None, "no 'files' list"
    taken = _parse_iso(str(data.get("taken_at", "")))
    if taken:
        return taken, ""
    match = re.search(r"(\d{8}T\d{6})Z", path.name)
    if not match:
        return None, "no taken_at and no time stamp in the name"
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc), ""
    except ValueError:
        return None, "time stamp in the name is not a date"


def _read_human_csv(path: Path) -> List[Dict[str, str]]:
    """Rows of a human-maintained CSV, tolerating the UTF-8 BOM that Excel writes.

    The shared ``common.read_csv_dicts`` reads strict UTF-8 and would keep the
    BOM in the first column name; this variant is used only for files people
    edit by hand (``blocked_inputs.csv``).
    """
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _truthy(value: Any) -> bool:
    """Interpret JSON/CSV booleans (``True``, ``"true"``, ``"y"``, ``"1"``)."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "y", "yes", "1")


def _norm_path(rel_path: str) -> str:
    """Collapse whitespace and strip for forgiving path comparison."""
    return re.sub(r"\s+", " ", str(rel_path)).strip()


def _sample(items: Sequence[str], limit: int = 5) -> str:
    """Comma-joined first ``limit`` items, with a count of the rest."""
    shown = ", ".join(items[:limit])
    extra = len(items) - limit
    return shown + (f" (+{extra} more)" if extra > 0 else "")


# --------------------------------------------------------------------------- #
# Phase 0 checks
# --------------------------------------------------------------------------- #
def check_bootstrap_files(ctx: Context) -> CheckOutcome:
    """P0-01: every bootstrap file exists."""
    missing = [name for name in BOOTSTRAP_FILES if not (ctx.root / name).is_file()]
    if missing:
        return FAIL, [f"missing: {_sample(missing, 12)}"]
    return PASS, [f"all {len(BOOTSTRAP_FILES)} bootstrap files present"]


def check_config_folders(ctx: Context) -> CheckOutcome:
    """P0-02: the configuration loads and names the four Drive folders."""
    try:
        cfg = ctx.config()
    except Exception as exc:  # noqa: BLE001 - any load failure is the finding
        return FAIL, [f"config/project.yaml did not load: {exc}"]
    folders = ((cfg.get("drive") or {}).get("folders") or {})
    if not isinstance(folders, dict):
        return FAIL, ["drive.folders is not a mapping"]
    missing = [key for key in DRIVE_FOLDER_KEYS
               if not isinstance(folders.get(key), dict) or not str(folders[key].get("id", "")).strip()]
    if missing:
        return FAIL, [f"drive.folders lacks an id for: {', '.join(missing)}"]
    evidence = [f"{key}: {folders[key]['id']}" for key in DRIVE_FOLDER_KEYS]
    return PASS, evidence


def check_two_listings(ctx: Context) -> CheckOutcome:
    """P0-03: two consolidated listings >= 15 minutes apart and the unstable-departments file."""
    interval = max(MIN_LISTING_GAP_MIN, int(((ctx.config().get("drive") or {}).get("listing_interval_minutes")) or 0))
    readable = ctx.listings()
    evidence: List[str] = [f"{len(readable)} consolidated listing(s) under state/listings"]
    for path, reason in ctx.unreadable_listings():
        evidence.append(f"ignored {path.name}: {reason}")
    status = PASS
    if len(readable) < 2:
        status = FAIL
        evidence.append("second listing pending")
    else:
        span = readable[-1][0] - readable[0][0]
        evidence.append(f"span first to last: {span.total_seconds() / 60:.1f} min (need >= {interval})")
        if span < timedelta(minutes=interval):
            status = FAIL
            evidence.append("second listing pending")
    unstable = ctx.state / "unstable_departments.json"
    if unstable.is_file():
        evidence.append("state/unstable_departments.json present")
    else:
        status = FAIL
        evidence.append("state/unstable_departments.json missing")
    return status, evidence


def _assembled_parts(ctx: Context, record: Dict[str, Any]) -> int:
    """Number of uploaded parts behind an ``upload-assembled`` ledger row.

    The parts are ledgered with transport ``upload`` and a ``drive_id`` that
    names the parent file's id (``"<id> (parent file, N B)"``), so they are
    counted by that id; a ``parts`` field on the row itself takes precedence.
    """
    try:
        declared = int(record.get("parts") or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared > 0:
        return declared
    parent = str(record.get("drive_id") or "").strip()
    if not parent:
        return 0
    return sum(1 for r in ctx.ledger_rows()
               if r is not record and str(r.get("transport", "")) == "upload"
               and parent in str(r.get("drive_id", "")) and str(r.get("rel_path", "")) != str(record.get("rel_path", "")))


def _assembled_note(ctx: Context, rel_path: str, record: Dict[str, Any]) -> str:
    """Evidence line explaining why an assembled working copy cannot match Drive's byte size."""
    parts = _assembled_parts(ctx, record)
    count = f"{parts} uploaded parts" if parts else "uploaded parts"
    return (f"{rel_path}: assembled from {count}; page content to be confirmed against the Drive original "
            f"when a full transport exists")


def check_manifest_matches_listing(ctx: Context) -> CheckOutcome:
    """P0-04: manifest rows equal the listing's files; pulled rows carry sha256 and size_match.

    The ledger is the pull ledger plus the upload ledger (``Context.ledger_rows``).
    A row whose transport is ``upload-assembled`` is a working copy stitched
    from uploaded parts of one Drive file; it cannot be byte-identical to the
    original, so its ``size_match`` false is reported as an explained
    exception rather than a failure.  Every other ``size_match`` false fails.
    """
    listing = ctx.latest_listing()
    if listing is None:
        return FAIL, ["no consolidated listing"]
    if not (ctx.registers / "manifest.csv").is_file():
        return FAIL, ["out/registers/manifest.csv missing"]
    rows = ctx.manifest_rows()
    evidence = [f"manifest rows {len(rows)}, listing files {len(listing['files'])} ({listing['path'].name})"]
    status = PASS
    if len(rows) != len(listing["files"]):
        status = FAIL
        evidence.append("row count differs from the listing")
    ledger = ctx.ledger_index()
    no_sha: List[str] = []
    bad_size: List[str] = []
    assembled: List[str] = []
    pulled = 0
    for row in rows:
        if str(row.get("pulled", "")).strip().lower() != "y":
            continue
        pulled += 1
        rel_path = str(row.get("rel_path", "?"))
        if not str(row.get("sha256", "")).strip():
            no_sha.append(rel_path)
        record = ledger.get("id:" + str(row.get("drive_id", ""))) or ledger.get("path:" + str(row.get("rel_path", "")))
        if record is None or _truthy(record.get("size_match")):
            if record is None:
                bad_size.append(rel_path)
            continue
        if str(record.get("transport", "")) == ASSEMBLED_TRANSPORT:
            assembled.append(_assembled_note(ctx, rel_path, record))
        else:
            bad_size.append(rel_path)
    evidence.append(f"pulled rows {pulled} (ledgers: {', '.join(LEDGER_FILES)})")
    if no_sha:
        status = FAIL
        evidence.append(f"pulled without sha256: {_sample(no_sha)}")
    if bad_size:
        status = FAIL
        evidence.append(f"pulled without size_match true in the ledger: {_sample(bad_size)}")
    for line in assembled:
        evidence.append(f"explained exception ({ASSEMBLED_TRANSPORT}): {line}")
    return status, evidence


def check_skipped_zips(ctx: Context) -> CheckOutcome:
    """P0-05: skipped_zip.csv has one row per zip in the listing."""
    listing = ctx.latest_listing()
    if listing is None:
        return FAIL, ["no consolidated listing"]
    zips = [f.get("rel_path", "") for f in listing["files"] if common.is_zip(f.get("rel_path", ""))]
    path = ctx.registers / "skipped_zip.csv"
    if not path.is_file():
        return FAIL, [f"{path.relative_to(ctx.root)} missing ({len(zips)} zip(s) in the listing)"]
    rows = common.read_csv_dicts(path)
    evidence = [f"skipped_zip rows {len(rows)}, zips in listing {len(zips)}"]
    if len(rows) != len(zips):
        return FAIL, evidence + ["counts differ"]
    return PASS, evidence


def check_census_covers_ledger(ctx: Context) -> CheckOutcome:
    """P0-06: every fetched pdf/pptx appears in file_census.csv.

    Coverage is by ``rel_path`` over both ledgers (pull and upload): a file
    that is hash-identical to one already censused still has its own census
    row (noted ``duplicate of <rel_path>``), so every fetched path must appear.
    """
    wanted = sorted({str(r.get("rel_path", "")) for r in ctx.ledger_rows()
                     if common.file_ext(str(r.get("rel_path", ""))) in CENSUS_KINDS})
    if not wanted:
        return PASS, ["no pdf/pptx in the pull or upload ledger"]
    path = ctx.registers / "file_census.csv"
    if not path.is_file():
        return FAIL, [f"file_census.csv missing; {len(wanted)} pdf/pptx fetched"]
    census = {_norm_path(r.get("rel_path", "")) for r in common.read_csv_dicts(path)}
    missing = [p for p in wanted if _norm_path(p) not in census]
    evidence = [f"{len(wanted)} pdf/pptx in the ledgers ({', '.join(LEDGER_FILES)}), {len(census)} files in census"]
    if missing:
        return FAIL, evidence + [f"not in census: {_sample(missing)}"]
    return PASS, evidence


def check_decisions(ctx: Context) -> CheckOutcome:
    """P0-07: decisions_needed.md has entries, each with Status and Provisional value."""
    path = ctx.qa / decisions.FILE_NAME
    if not path.is_file():
        return FAIL, [f"{path.relative_to(ctx.root)} missing"]
    _, entries = decisions.parse_file(path)
    if not entries:
        return FAIL, ["no '### D-NNN' entries"]
    incomplete = [e.id for e in entries if e.get("Status") is None or e.get("Provisional value") is None]
    evidence = [f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'}: {_sample([e.id for e in entries], 8)}"]
    status = PASS
    if incomplete:
        status = FAIL
        evidence.append(f"missing Status or Provisional value: {', '.join(incomplete)}")
    duplicated = decisions.duplicate_ids(entries)
    if duplicated:
        status = FAIL
        evidence.append(f"duplicated id(s): {', '.join(duplicated)} (a register keyed by id must not repeat one)")
    return status, evidence


def check_source_lock(ctx: Context) -> CheckOutcome:
    """P0-08: state/source_lock.json records enforced true, or an honest false with a reason."""
    path = ctx.state / "source_lock.json"
    if not path.is_file():
        return FAIL, ["state/source_lock.json missing"]
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return FAIL, [f"source_lock.json unreadable: {exc}"]
    if not isinstance(state, dict) or not isinstance(state.get("enforced"), bool):
        return FAIL, ["source_lock.json has no boolean 'enforced'"]
    method = state.get("method") or "unknown"
    if state["enforced"]:
        return PASS, [f"enforced true (method {method})"]
    reason = str(state.get("reason") or state.get("note") or "").strip()
    if not reason:
        return FAIL, ["enforced false without a reason"]
    return PASS, [f"enforced false, reason: {reason}", f"method {method}"]


def check_pressure_selftest(ctx: Context) -> CheckOutcome:
    """P0-09: ``python -m src.pressure --selftest`` exits 0."""
    if not (ctx.root / "src" / "pressure.py").is_file():
        return FAIL, ["src/pressure.py missing"]
    env = dict(os.environ)
    env["AIGH_ROOT"] = str(ctx.root)
    try:
        proc = subprocess.run([sys.executable, "-m", "src.pressure", "--selftest"], cwd=str(ctx.root), env=env,
                              capture_output=True, text=True, timeout=SELFTEST_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        return FAIL, [f"selftest timed out after {SELFTEST_TIMEOUT_S}s"]
    tail = [line for line in (proc.stdout + proc.stderr).splitlines() if line.strip()][-3:]
    evidence = [f"exit code {proc.returncode}"] + tail
    return (PASS if proc.returncode == 0 else FAIL), evidence


def check_blocked_inputs(ctx: Context) -> CheckOutcome:
    """P0-10: blocked_inputs.csv exists, has the columns and matches the plan's blocked rows."""
    rows = ctx.blocked_inputs()
    if rows is None:
        return FAIL, ["out/qa/blocked_inputs.csv missing"]
    header = _csv_header(ctx.qa / "blocked_inputs.csv")
    missing_cols = [c for c in BLOCKED_INPUT_COLUMNS if c not in header]
    if missing_cols:
        return FAIL, [f"columns missing: {', '.join(missing_cols)}"]
    if not (ctx.state / "pull_plan.csv").is_file():
        return FAIL, [f"{len(rows)} blocked input(s)", "state/pull_plan.csv missing: nothing to reconcile against"]
    plan = {_norm_path(r.get("rel_path", "")): r for r in ctx.plan_rows()}
    plan_blocked = {p for p, r in plan.items() if r.get("status") == BLOCKED_STATUS}
    listed = {_norm_path(r.get("rel_path", "")): r for r in rows}
    evidence = [f"{len(rows)} blocked input(s), {len(plan_blocked)} plan row(s) {BLOCKED_STATUS}"]
    problems = _blocked_row_problems(listed, plan, plan_blocked)
    problems += _resolved_status_problems(ctx, listed, plan)
    not_listed = sorted(plan_blocked - set(listed))
    if not_listed:
        problems.insert(0, f"blocked in plan but not listed: {_sample(not_listed)}")
    outside = sorted(p for p in listed if p not in plan and not _claims_transport_limit(listed[p]))
    if outside:
        evidence.append(f"listed for reasons outside the plan (not reconciled): {_sample(outside)}")
    return (FAIL if problems else PASS), evidence + problems


def _resolved_status_problems(ctx: Context, listed: Dict[str, Dict[str, str]],
                              plan: Dict[str, Dict[str, str]]) -> List[str]:
    """Rows marked ``uploaded`` or ``not needed`` must be backed by the upload ledger or a recorded skip."""
    upload_ids = {str(r.get("drive_id", "")) for r in common.read_jsonl(ctx.state / "upload_ledger.jsonl")
                  if isinstance(r, dict)}
    upload_paths = {_norm_path(str(r.get("rel_path", ""))) for r in common.read_jsonl(ctx.state / "upload_ledger.jsonl")
                    if isinstance(r, dict)}
    skipped: set = set()
    skip_file = ctx.state / "pull_batches" / "basis_skipped.csv"
    if skip_file.is_file():
        skipped = {_norm_path(r.get("rel_path", "")) for r in common.read_csv_dicts(skip_file)}
    bad_up: List[str] = []
    bad_nn: List[str] = []
    for rel_path, row in listed.items():
        status = str(row.get("status", "")).strip().lower()
        if status == "uploaded":
            drive_id = str(row.get("drive_id", "")).strip()
            if rel_path not in upload_paths and not (drive_id and drive_id in upload_ids):
                bad_up.append(rel_path)
        elif status == "not needed":
            if rel_path not in skipped and plan.get(rel_path, {}).get("status") != "skipped_zip":
                bad_nn.append(rel_path)
    out: List[str] = []
    if bad_up:
        out.append(f"marked uploaded but not in the upload ledger: {_sample(bad_up)}")
    if bad_nn:
        out.append(f"marked not needed but not in basis_skipped.csv or a skipped_zip plan row: {_sample(bad_nn)}")
    return out


def _claims_transport_limit(row: Dict[str, str]) -> bool:
    """True when a blocked-input row says it was blocked by the connector size limit."""
    return bool(_LIMIT_REASON_RE.search(str(row.get("reason", ""))))


def _blocked_row_problems(listed: Dict[str, Dict[str, str]], plan: Dict[str, Dict[str, str]],
                          plan_blocked: set) -> List[str]:
    """Blocked-input rows that disagree with the pull plan, keyed on rel_path and bytes.

    A row whose path is in the plan must be a ``blocked_connector_limit`` row
    with the same byte count.  A row whose path is not in the plan is stale
    when its reason claims the transport limit (the plan is the record of
    that); rows blocked for other reasons are outside the plan's remit.
    """
    stale: List[str] = []
    wrong_status: List[str] = []
    wrong_bytes: List[str] = []
    for rel_path, row in listed.items():
        if rel_path in plan_blocked:
            plan_bytes = str(plan[rel_path].get("bytes", "")).strip()
            if plan_bytes and str(row.get("bytes", "")).strip() != plan_bytes:
                wrong_bytes.append(f"{rel_path} ({row.get('bytes', '')!s} vs plan {plan_bytes})")
        elif rel_path in plan:
            if str(row.get("status", "")).strip().lower() in RESOLVED_STATUSES:
                continue  # uploaded or not needed: the plan may show it fetched or pending
            if plan[rel_path].get("status") == "pending" and not _claims_transport_limit(row):
                continue  # not pulled for another recorded reason (outside the plan's remit)
            wrong_status.append(f"{rel_path} (plan status {plan[rel_path].get('status', '')!s})")
        elif _claims_transport_limit(row):
            stale.append(rel_path)
    problems: List[str] = []
    if stale:
        problems.append(f"listed as transport-limit-blocked but not in plan: {_sample(stale)}")
    if wrong_status:
        problems.append(f"listed but plan status is not {BLOCKED_STATUS}: {_sample(wrong_status)}")
    if wrong_bytes:
        problems.append(f"bytes differ from plan: {_sample(wrong_bytes)}")
    return problems


def _csv_header(path: Path) -> List[str]:
    """Column names of a CSV file (empty when missing or blank; a UTF-8 BOM is ignored)."""
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return next(csv.reader(handle), [])


def check_background_indexed(ctx: Context) -> CheckOutcome:
    """P0-11: sheet_index.csv exists, or the background PDF is listed as a blocked input."""
    sheet_index = ctx.registers / "sheet_index.csv"
    if sheet_index.is_file():
        return PASS, [f"sheet_index.csv present ({len(common.read_csv_dicts(sheet_index))} rows)"]
    rows = ctx.blocked_inputs() or []
    target = _norm_path(BACKGROUND_PDF)
    for row in rows:
        if _norm_path(row.get("rel_path", "")) == target:
            return PASS, ["sheet_index.csv absent", f"blocked_inputs.csv lists '{BACKGROUND_PDF}'"]
    return FAIL, ["sheet_index.csv absent", f"'{BACKGROUND_PDF}' not in blocked_inputs.csv"]


def check_template_outputs(ctx: Context) -> CheckOutcome:
    """P0-12: sample zones and the three template specifications (SKIP while the workflow runs)."""
    expected = [ctx.registers / "sample_zones.csv"] + [ctx.qa / name for name in TEMPLATE_QA_FILES]
    missing = [str(p.relative_to(ctx.root)) for p in expected if not p.is_file()]
    if not missing:
        return PASS, [f"all {len(expected)} template artefacts present"]
    item = ctx.progress_item(TEMPLATE_PROGRESS_KEY) or {}
    evidence = [f"missing: {_sample(missing, 6)}"]
    if item.get("status") == "done":
        reason = str(item.get("reason") or "").strip()
        return FAIL, evidence + [f"progress marks {TEMPLATE_PROGRESS_KEY} done at {item.get('updated_at', '?')}"
                                 + (f" (closed incomplete: {reason})" if reason else "")]
    return SKIP, evidence + [f"template workflow has not finished (progress item '{TEMPLATE_PROGRESS_KEY}' is "
                             f"{item.get('status') or 'not set'}; python -m src.template_workflow finish sets it)"]


# --------------------------------------------------------------------------- #
# Phase 1 checks (stage 1A conversion)
# --------------------------------------------------------------------------- #
def _front_matter(md_path: Path) -> Optional[Dict[str, Any]]:
    """The YAML front matter of a Markdown file, or ``None`` when absent or unreadable."""
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def check_p1_markdown_or_reason(ctx: Context) -> CheckOutcome:
    """P1-01: every in-scope file has a Markdown file or a recorded reason in the convert ledger."""
    rows = ctx.scoped_rows()
    if not rows:
        return SKIP, ["no fetched files in scope" + (" (convert ledger empty)" if ctx.scope == "converted" else "")]
    convert = ctx.convert_rows()
    missing: List[str] = []
    broken: List[str] = []
    with_reason = 0
    for row in rows:
        record = convert.get(f"{row['sha256']}|{row['rel_path']}")
        if record is None:
            missing.append(str(row["rel_path"]))
        elif record.get("status") in CONVERT_DONE_STATUSES:
            md_path = record.get("md_path")
            if not md_path or not (ctx.root / str(md_path)).is_file():
                broken.append(str(row["rel_path"]))
        elif record.get("error") or record.get("grade_reason"):
            with_reason += 1
        else:
            missing.append(f"{row['rel_path']} (status {record.get('status')} without a reason)")
    evidence = [f"{len(rows)} file(s) in scope ({ctx.scope}); {len(rows) - len(missing) - len(broken) - with_reason} "
                f"with Markdown, {with_reason} with a recorded reason"]
    if missing:
        evidence.append(f"no convert record: {_sample(missing)}")
    if broken:
        evidence.append(f"recorded as converted but Markdown missing: {_sample(broken)}")
    return (FAIL if missing or broken else PASS), evidence


def _per_file_check(ctx: Context, judge: Callable[[Dict[str, Any]], Tuple[bool, str]], label: str) -> CheckOutcome:
    """Run ``judge(checks.json)`` for every converted file; FAIL lists the files that do not pass."""
    rows = ctx.converted_rows()
    if not rows:
        return SKIP, ["no converted files in scope"]
    failures: List[str] = []
    unreadable: List[str] = []
    for row in rows:
        checks = ctx.file_checks(row)
        if checks is None:
            unreadable.append(str(row.get("rel_path")))
            continue
        ok, detail = judge(checks)
        if not ok:
            failures.append(f"{row.get('rel_path')}: {detail}")
    evidence = [f"{len(rows)} converted file(s) checked for {label}"]
    if unreadable:
        evidence.append(f"checks.json missing or unreadable: {_sample(unreadable)}")
    if failures:
        evidence.append(f"{len(failures)} failing: " + " | ".join(failures[:5]) +
                        (f" (+{len(failures) - 5} more)" if len(failures) > 5 else ""))
    return (FAIL if failures or unreadable else PASS), evidence


def check_p1_numeric_recall(ctx: Context) -> CheckOutcome:
    """P1-02: numeric recall >= the configured threshold per file (misses listed)."""
    threshold = ctx.recall_threshold()

    def judge(checks: Dict[str, Any]) -> Tuple[bool, str]:
        recall = float(checks.get("numeric_recall", 0.0))
        misses = [str(m) for m in checks.get("misses", [])][:MAX_MISSES_SHOWN]
        return recall >= threshold, f"recall {recall:.4f} < {threshold}; misses {', '.join(misses) or 'n/a'}"
    status, evidence = _per_file_check(ctx, judge, f"numeric recall >= {threshold}")
    return status, evidence


def check_p1_table_counts(ctx: Context) -> CheckOutcome:
    """P1-03: sidecar table count equals the Markdown table count per file."""
    def judge(checks: Dict[str, Any]) -> Tuple[bool, str]:
        tables = checks.get("tables") or {}
        return bool(tables.get("match")), f"sidecar {tables.get('sidecar')} vs Markdown {tables.get('markdown')}"
    return _per_file_check(ctx, judge, "table counts")


def check_p1_annotation_counts(ctx: Context) -> CheckOutcome:
    """P1-04: sidecar annotation count equals the Comments register rows per file."""
    def judge(checks: Dict[str, Any]) -> Tuple[bool, str]:
        annots = checks.get("annotations") or {}
        return bool(annots.get("match")), f"sidecar {annots.get('sidecar')} vs register {annots.get('register')}"
    return _per_file_check(ctx, judge, "annotation counts")


def check_p1_grades(ctx: Context) -> CheckOutcome:
    """P1-05: every converted or duplicate file carries a grade (A, B, C or pending) in ledger and front matter."""
    convert = ctx.convert_rows()
    records = [convert[f"{r['sha256']}|{r['rel_path']}"] for r in ctx.scoped_rows()
               if f"{r['sha256']}|{r['rel_path']}" in convert and
               convert[f"{r['sha256']}|{r['rel_path']}"].get("status") in CONVERT_DONE_STATUSES]
    if not records:
        return SKIP, ["no converted files in scope"]
    bad: List[str] = []
    tally: Dict[str, int] = {}
    for record in records:
        grade = record.get("grade")
        if grade not in GRADES:
            bad.append(f"{record.get('rel_path')}: ledger grade {grade!r}")
            continue
        tally[str(grade)] = tally.get(str(grade), 0) + 1
        front = _front_matter(ctx.root / str(record.get("md_path") or ""))
        if front is None:
            bad.append(f"{record.get('rel_path')}: no front matter")
        elif front.get("grade") != grade:
            bad.append(f"{record.get('rel_path')}: front matter grade {front.get('grade')!r} != ledger {grade!r}")
        elif not str(front.get("grade_reason") or "").strip():
            bad.append(f"{record.get('rel_path')}: empty grade_reason")
    evidence = [f"{len(records)} file(s): " + ", ".join(f"{g} {n}" for g, n in sorted(tally.items()))]
    if bad:
        evidence.append(f"problems: {_sample(bad)}")
    return (FAIL if bad else PASS), evidence


PHASE_CHECKS: Dict[int, List[Check]] = {
    1: [
        Check("P1-01", "Every in-scope file has Markdown or a recorded reason", check_p1_markdown_or_reason),
        Check("P1-02", "Numeric recall >= threshold per file", check_p1_numeric_recall),
        Check("P1-03", "Table counts match (sidecar vs Markdown)", check_p1_table_counts),
        Check("P1-04", "Annotation counts match (sidecar vs Comments register)", check_p1_annotation_counts),
        Check("P1-05", "Grades recorded in ledger and front matter", check_p1_grades),
    ],
    0: [
        Check("P0-01", "Bootstrap files exist", check_bootstrap_files),
        Check("P0-02", "Config loads with four Drive folder ids", check_config_folders),
        Check("P0-03", "Two listings >= 15 min apart; unstable departments recorded", check_two_listings),
        Check("P0-04", "Manifest matches listing; pulled rows hashed and size-matched", check_manifest_matches_listing),
        Check("P0-05", "Skipped zips equal zip count in listing", check_skipped_zips),
        Check("P0-06", "Every fetched pdf/pptx in file census", check_census_covers_ledger),
        Check("P0-07", "Decisions file has complete entries", check_decisions),
        Check("P0-08", "Source lock state recorded honestly", check_source_lock),
        Check("P0-09", "Pressure method self-test passes", check_pressure_selftest),
        Check("P0-10", "Blocked inputs consistent with pull plan", check_blocked_inputs),
        Check("P0-11", "Background PDF indexed or declared blocked", check_background_indexed),
        Check("P0-12", "Template workflow artefacts present", check_template_outputs),
    ],
}
"""Registry of checks per phase; phases absent here are not implemented."""


def _register_late_phases() -> None:
    """Phase 2 and phase 3 (D-056, D-069): thin wrappers in src/phase_checks.py."""
    from . import phase_checks as pc
    PHASE_CHECKS[2] = [
        Check("P2-01", "Status lines: Calculation, Inputs complete, DRAFT label", pc.check_p2_status),
        Check("P2-02", "Deliverable language scan: 0 hits", pc.check_p2_language),
        Check("P2-03", "MOT unit control total (links, D22; no variant tag)", pc.check_p2_unit_control),
    ]
    PHASE_CHECKS[3] = [
        Check("P3-01", "Decision trace: no FAIL, no NO TEST", pc.check_p3_trace),
        Check("P3-02", "Link coverage reported", pc.check_p3_link_coverage),
        Check("P3-03", "Seeded unit-tag errors are caught", pc.check_p3_seeded),
        Check("P3-04", "Structural text lint (reduced)", pc.check_p3_lint),
        Check("P3-05", "Outputs not older than inputs", pc.check_p3_fresh),
    ]


    from . import phase4_checks as p4
    from . import phase4_c4 as c4
    PHASE_CHECKS[4] = [
        Check("P4-01", "Every unit of the zone schedule has a register tab and a drawing or a stated reason", p4.check_p4_units),
        Check("P4-02", "App D tags appear exactly once (or are listed with a reason)", p4.check_p4_appd_tags),
        Check("P4-03", "Every highlighted App D space is in exactly one box", p4.check_p4_appd_spaces),
        Check("P4-04", "New-drawing values equal the register (DXF read-back)", p4.check_p4_values),
        Check("P4-05", "No placeholder change ID on a new drawing", p4.check_p4_no_placeholder),
        Check("P4-06", "No text overlaps text, a symbol or a duct", p4.check_p4_overlap),
        Check("P4-07", "PDF and DXF hold the same entities", p4.check_p4_pdf_dxf),
        Check("P4-08", "Every mark-up has a register row and the reverse", p4.check_p4_markup_register),
        Check("P4-09", "Change IDs: change list equals update workbook", p4.check_p4_workbook_ids),
        Check("P4-10", "Air in equals air out: room, AHU, system (1 % or 10 L/s)", p4.check_p4_balance),
        Check("P4-11", "Drawing register matches the files on disk", p4.check_p4_drawing_register),
        Check("P4-12", "Issue folder complete; README language", p4.check_p4_issue),
        Check("P4-13", "Drawn value anchored: the PDF text under the box holds the number", c4.check_p4_13_anchor),
        Check("P4-14", "Source read-back: the cited cell opens, lies in the room's row and column, equals 'required'", c4.check_p4_14_source),
        Check("P4-15", "Fans: ownership by tag core, pair once, required once, verdicts recomputed", c4.check_p4_15_fans),
        Check("P4-16", "No add, missing or no-schematic row where the tag or system is drawn", c4.check_p4_16_false_add),
        Check("P4-17", "Every drawn entity has one disposition; ledger agrees with PDF text", c4.check_p4_17_dispositions),
        Check("P4-18", "One home per unit over working sheets and new drawings", c4.check_p4_18_home),
        Check("P4-19", "Input gate: no instruction on a unit outside 0.90-1.10; name-only, reduction, D-076 rules", c4.check_p4_19_gate),
        Check("P4-20", "Value domain: pressure, typical count, airflow, unit of measure", c4.check_p4_20_domain),
        Check("P4-21", "MOT: no change ID or annotation for a list-only unit; MOT differences tab complete", c4.check_p4_21_mot),
        Check("P4-22", "Text: D-015 word list, structural lint, no duplicate rows", c4.check_p4_22_text),
        Check("P4-23", "One build: manifest hashes, issue equals staging, IDs equal, no ID reused", c4.check_p4_23_build),
    ]


_register_late_phases()


# --------------------------------------------------------------------------- #
# Runner and report
# --------------------------------------------------------------------------- #
def run_check(check: Check, ctx: Context) -> CheckResult:
    """Run one check, turning any exception into a FAIL with the error as evidence."""
    try:
        status, evidence = check.func(ctx)
    except Exception as exc:  # noqa: BLE001 - a crashing check is a failing check
        status, evidence = FAIL, [f"check raised {type(exc).__name__}: {exc}"]
    if status not in (PASS, FAIL, SKIP):
        status, evidence = FAIL, [f"check returned unknown status {status!r}"] + list(evidence)
    return CheckResult(check.check_id, check.title, status, [str(e) for e in evidence])


def run_phase(phase: int, ctx: Context) -> List[CheckResult]:
    """Run every registered check of ``phase`` in order."""
    return [run_check(check, ctx) for check in PHASE_CHECKS[phase]]


def counts(results: Sequence[CheckResult]) -> Dict[str, int]:
    """``{"PASS": n, "FAIL": n, "SKIP": n}``."""
    return {status: sum(1 for r in results if r.status == status) for status in (PASS, FAIL, SKIP)}


def all_passed(results: Sequence[CheckResult]) -> bool:
    """True when no check failed (SKIP does not count against the phase)."""
    return not any(r.status == FAIL for r in results)


def _cell(text: str) -> str:
    """Escape a value for a Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def render_report(phase: int, results: Sequence[CheckResult], ctx: Context) -> str:
    """The Markdown report: a summary and a table of check id, status and evidence."""
    tally = counts(results)
    verdict = PASS if all_passed(results) else FAIL
    lines = [
        f"# Phase {phase} checks",
        "",
        f"- Run at: {common.utc_now_iso()}",
        f"- Project root: {ctx.root}",
        f"- Scope: {ctx.scope}",
        f"- Result: **{verdict}** ({tally[PASS]} PASS, {tally[FAIL]} FAIL, {tally[SKIP]} SKIP)",
        "",
        "| Check | Title | Status | Evidence |",
        "|---|---|---|---|",
    ]
    for result in results:
        evidence = "<br>".join(_cell(line) for line in result.evidence) or "-"
        lines.append(f"| {result.check_id} | {_cell(result.title)} | {result.status} | {evidence} |")
    lines.append("")
    return "\n".join(lines)


def report_path(phase: int, ctx: Context) -> Path:
    """``<qa dir>/check_phase<N>.md``."""
    return ctx.qa / f"check_phase{phase}.md"


def print_results(results: Sequence[CheckResult]) -> None:
    """Console summary, one line per check plus the first evidence line."""
    for result in results:
        first = result.evidence[0] if result.evidence else ""
        print(f"{result.check_id} {result.status:4s} {result.title} -- {first}")
    tally = counts(results)
    print(f"{tally[PASS]} PASS, {tally[FAIL]} FAIL, {tally[SKIP]} SKIP")


def run_and_report(phase: int, ctx: Optional[Context] = None, out_path: Optional[Path] = None,
                   quiet: bool = False, scope: str = "all") -> int:
    """Run a phase, write its report and return the exit code (0 pass, 1 fail, 2 not implemented)."""
    if phase not in PHASE_CHECKS:
        print(f"phase {phase}: not implemented", file=sys.stderr)
        return 2
    ctx = ctx or Context(scope=scope)
    results = run_phase(phase, ctx)
    target = out_path or report_path(phase, ctx)
    common.atomic_write_text(target, render_report(phase, results, ctx))
    if not quiet:
        print_results(results)
        print(f"report: {target}")
    return 0 if all_passed(results) else 1


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """Argument parser: ``phase0`` .. ``phase5`` plus ``list``."""
    parser = argparse.ArgumentParser(description="Run the pass/fail checks of a pipeline phase.")
    parser.add_argument("phase", choices=[f"phase{n}" for n in range(6)] + ["list"],
                        help="phase to check, or 'list' to show the registered checks")
    parser.add_argument("--report", type=Path, default=None, help="report path (default: out/qa/check_phase<N>.md)")
    parser.add_argument("--quiet", action="store_true", help="write the report without console output")
    parser.add_argument("--scope", choices=SCOPES, default="all",
                        help="phase1: 'converted' judges only files already in the convert ledger (default all)")
    return parser


def _cmd_list() -> int:
    """Print the registry."""
    for phase, checks in sorted(PHASE_CHECKS.items()):
        for check in checks:
            print(f"phase{phase} {check.check_id} {check.title}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point; returns a process exit code."""
    args = build_parser().parse_args(argv)
    if args.phase == "list":
        return _cmd_list()
    phase = int(args.phase[len("phase"):])
    try:
        return run_and_report(phase, out_path=args.report, quiet=args.quiet, scope=args.scope)
    except Exception as exc:  # noqa: BLE001 - surface any failure as a non-zero exit
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
