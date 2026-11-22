"""Pass/fail checks per pipeline phase.

``python -m src.checks phase0`` runs the Phase 0 registry, writes
``out/qa/check_phase0.md`` (one table row per check with its evidence) and
exits 0 only when every non-SKIP check passes.  Phases 1 to 5 are not yet
implemented and exit 2.

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
    "src/checks.py", "src/pressure.py",
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
TEMPLATE_PROGRESS_KEY = "template_workflow"
"""Progress item set by ``python -m src.template_workflow finish``."""
_LIMIT_REASON_RE = re.compile(r"connector|limit|10485760|10\s*mib|too\s+large|size", re.IGNORECASE)
"""Reason wording that claims a file was blocked by the connector transport limit."""


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

    def __init__(self, root: Optional[Path] = None) -> None:
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


def check_manifest_matches_listing(ctx: Context) -> CheckOutcome:
    """P0-04: manifest rows equal the listing's files; pulled rows carry sha256 and size_match."""
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
    pulled = 0
    for row in rows:
        if str(row.get("pulled", "")).strip().lower() != "y":
            continue
        pulled += 1
        if not str(row.get("sha256", "")).strip():
            no_sha.append(row.get("rel_path", "?"))
        record = ledger.get("id:" + str(row.get("drive_id", ""))) or ledger.get("path:" + str(row.get("rel_path", "")))
        if record is None or not _truthy(record.get("size_match")):
            bad_size.append(row.get("rel_path", "?"))
    evidence.append(f"pulled rows {pulled}")
    if no_sha:
        status = FAIL
        evidence.append(f"pulled without sha256: {_sample(no_sha)}")
    if bad_size:
        status = FAIL
        evidence.append(f"pulled without size_match true in the ledger: {_sample(bad_size)}")
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
    """P0-06: every fetched pdf/pptx appears in file_census.csv."""
    wanted = [str(r.get("rel_path", "")) for r in ctx.ledger_rows()
              if common.file_ext(str(r.get("rel_path", ""))) in CENSUS_KINDS]
    if not wanted:
        return PASS, ["no pdf/pptx in the pull ledger"]
    path = ctx.registers / "file_census.csv"
    if not path.is_file():
        return FAIL, [f"file_census.csv missing; {len(wanted)} pdf/pptx fetched"]
    census = {_norm_path(r.get("rel_path", "")) for r in common.read_csv_dicts(path)}
    missing = [p for p in wanted if _norm_path(p) not in census]
    evidence = [f"{len(wanted)} pdf/pptx in ledger, {len(census)} files in census"]
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
    not_listed = sorted(plan_blocked - set(listed))
    if not_listed:
        problems.insert(0, f"blocked in plan but not listed: {_sample(not_listed)}")
    outside = sorted(p for p in listed if p not in plan and not _claims_transport_limit(listed[p]))
    if outside:
        evidence.append(f"listed for reasons outside the plan (not reconciled): {_sample(outside)}")
    return (FAIL if problems else PASS), evidence + problems


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


PHASE_CHECKS: Dict[int, List[Check]] = {
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
                   quiet: bool = False) -> int:
    """Run a phase, write its report and return the exit code (0 pass, 1 fail, 2 not implemented)."""
    if phase not in PHASE_CHECKS:
        print(f"phase {phase}: not implemented", file=sys.stderr)
        return 2
    ctx = ctx or Context()
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
        return run_and_report(phase, out_path=args.report, quiet=args.quiet)
    except Exception as exc:  # noqa: BLE001 - surface any failure as a non-zero exit
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
