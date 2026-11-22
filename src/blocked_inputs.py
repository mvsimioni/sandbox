"""Inputs the pipeline could not bring in, listed for the engineer in ``out/qa/blocked_inputs.csv``.

``python -m src.blocked_inputs build`` reads, without touching Drive or ``data/source``:

* ``state/pull_plan.csv`` (written by ``python -m src.drive_io pull``),
* the latest consolidated listing under ``state/listings`` (built from ``state/drive_listing``),
* the pull and upload ledgers, and
* the skip records in ``state/pull_batches/*_skipped.csv``,

and writes one row per input that needs the engineer's attention:

1. every pull-plan row with status ``blocked_connector_limit`` that the upload ledger does not hold
   (reason ``over connector 10 MB limit; upload or other transport needed``);
2. every in-scope file of the listing that was not pulled for another recorded reason.  The files of
   the MOT NSC package outside the ACMV discipline are out of scope, not blocked, and are left out;
   a file that is already on disk without a ledger row is a ledger gap, reported on stdout only.

``status`` is ``pending`` (still needed), ``uploaded`` (the upload ledger holds the same ``rel_path``) or
``not needed`` (a recorded decision that it is outside the ACMV scope).  ``python -m src.checks phase0``
(P0-10) reconciles the file with the plan.  The command is idempotent: rows are sorted, the file is
rewritten only when its content changes, and a second run changes nothing.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:  # package import (python -m src.blocked_inputs, tests)
    from src import common
    from src.state import Progress
except ModuleNotFoundError:  # run as a plain script from inside src/
    import common  # type: ignore[no-redef]
    from state import Progress  # type: ignore[no-redef]

COLUMNS: Tuple[str, ...] = ("rel_path", "bytes", "reason", "needed_for", "drive_id", "dept_no", "doc_class", "status")
FILE_NAME = "blocked_inputs.csv"
PROGRESS_KEY = "blocked_inputs"
LEDGER_NAMES: Tuple[str, ...] = ("pull_ledger.jsonl", "upload_ledger.jsonl")
BLOCKED_STATUS = "blocked_connector_limit"
LIMIT_REASON = "over connector 10 MB limit; upload or other transport needed"
UNRECORDED_REASON = "in scope but not pulled; no fetch record"
CONNECTOR_LIMIT_BYTES = 10 * 1024 * 1024
ASSEMBLED_REASON = ("over connector 10 MB limit; uploaded in 5 parts and assembled; "
                    "page content to be confirmed against the Drive original")
PENDING, UPLOADED, NOT_NEEDED = "pending", "uploaded", "not needed"
OUT_OF_SCOPE_MARK = "outside ACMV scope"
MOT_FOLDER = "MOT NSC"
NEEDED_BOOKLET = "design criteria table, zoning pictures, comments (Task 1, 2, 3)"
NEEDED_COMMENTS = "comment register (Task 1)"
NEEDED_MOT = "MOT theatre criteria (Task 3)"
NEEDED_SCHEMATIC = "schematic model (Task 4)"
NEEDED_CONTEXT = "context"
NEEDED_CORPUS = "Task 1 corpus"


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
def _dirs() -> Dict[str, Path]:
    """State, QA and source directories for the current project root."""
    paths = common.resolve_paths()
    root = common.project_root()
    return {"root": root, "state": paths.get("state", root / "state"), "qa": paths.get("qa", root / "out" / "qa"),
            "source": paths.get("source", root / "data" / "source")}


def latest_listing_files(state: Path) -> List[Dict[str, Any]]:
    """Non-folder entries of the latest consolidated ``state/listings/listing_*.json`` (empty when none)."""
    best: Tuple[str, Optional[Path]] = ("", None)
    for path in sorted((state / "listings").glob("listing_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("files"), list):
            stamp = str(data.get("taken_at") or path.name)
            if stamp >= best[0]:
                best = (stamp, path)
    if best[1] is None:
        return []
    data = json.loads(best[1].read_text(encoding="utf-8"))
    folder_mime = "application/vnd.google-apps.folder"
    return [f for f in data["files"] if isinstance(f, dict) and f.get("mimeType") != folder_mime]


def ledger_index(state: Path, name: str) -> Dict[str, Dict[str, Any]]:
    """One ledger's rows keyed by ``id:<drive_id>`` and ``path:<rel_path>`` (later rows win)."""
    index: Dict[str, Dict[str, Any]] = {}
    for row in common.read_jsonl(state / name):
        if not isinstance(row, dict):
            continue
        if row.get("drive_id"):
            index["id:" + str(row["drive_id"])] = row
        if row.get("rel_path"):
            index["path:" + str(row["rel_path"])] = row
    return index


def recorded_skips(state: Path) -> Dict[str, str]:
    """``{rel_path: reason}`` from ``state/pull_batches/*_skipped.csv`` (files listed but deliberately not pulled)."""
    skips: Dict[str, str] = {}
    for path in sorted((state / "pull_batches").glob("*_skipped.csv")):
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("rel_path"):
                    skips[str(row["rel_path"])] = str(row.get("reason") or "")
    return skips


def pilot_departments() -> List[str]:
    """Two-digit department numbers of ``pilot.departments`` in the configuration."""
    numbers: List[str] = []
    for item in (common.load_config().get("pilot") or {}).get("departments") or []:
        head = str(item).split("-")[0].strip()
        if head.isdigit():
            numbers.append(head.zfill(2))
    return numbers


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def in_scope(rel_path: str, pilots: Sequence[str]) -> bool:
    """True for ``Basis``, ``Template`` and the pilot departments' folders (other departments are zips only)."""
    info = common.parse_department(rel_path)
    if info["root"] in (common.ROOT_BASIS, common.ROOT_TEMPLATE):
        return True
    return info["root"] == common.ROOT_DD and info["dept_no"] in pilots


def is_mot(rel_path: str) -> bool:
    """True when the file sits in the MOT NSC package."""
    return MOT_FOLDER in str(rel_path).replace("\\", "/").split("/")


def needed_for(rel_path: str, doc_class: str) -> str:
    """What the file is needed for, from its document class and path."""
    name = str(rel_path).replace("\\", "/").rsplit("/", 1)[-1].lower()
    if doc_class == "Booklet":
        return NEEDED_BOOKLET
    if doc_class == "comment register":
        return NEEDED_COMMENTS
    if is_mot(rel_path) or "specification" in name:
        return NEEDED_MOT
    if doc_class == "schematic":
        return NEEDED_SCHEMATIC
    if doc_class == "correspondence":
        return NEEDED_CONTEXT
    return NEEDED_CORPUS


def _as_int(value: Any) -> int:
    """Integer value of a listing or plan size (blank or malformed becomes 0)."""
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _row(rel_path: str, size: Any, reason: str, drive_id: str, status: str) -> Dict[str, Any]:
    """One output row with the derived columns filled in."""
    doc_class = common.classify_document(rel_path)
    return {"rel_path": rel_path, "bytes": _as_int(size), "reason": reason, "needed_for": needed_for(rel_path, doc_class),
            "drive_id": drive_id, "dept_no": common.parse_department(rel_path)["dept_no"], "doc_class": doc_class,
            "status": status}


def build_rows(plan: Sequence[Dict[str, str]], listing: Sequence[Dict[str, Any]], pull: Dict[str, Dict[str, Any]],
               upload: Dict[str, Dict[str, Any]], skips: Dict[str, str], pilots: Sequence[str],
               source: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The blocked-input rows (sorted by ``rel_path``) and the in-scope files on disk without a ledger row."""
    plan_by_id = {str(r.get("drive_id", "")): r for r in plan}
    rows: Dict[str, Dict[str, Any]] = {}
    gap: List[str] = []

    def uploaded(rel_path: str, drive_id: str) -> bool:
        return ("path:" + rel_path) in upload or (bool(drive_id) and ("id:" + drive_id) in upload)

    def recorded_out_of_scope(rel_path: str) -> bool:
        return OUT_OF_SCOPE_MARK in skips.get(rel_path, "")

    for entry in plan:  # 1. the plan's connector-limit rows that the upload ledger does not hold
        rel_path, drive_id = str(entry.get("rel_path", "")), str(entry.get("drive_id", ""))
        if entry.get("status") != BLOCKED_STATUS or uploaded(rel_path, drive_id):
            continue
        if recorded_out_of_scope(rel_path):
            reason, status = f"over connector 10 MB limit; {skips[rel_path]}", NOT_NEEDED
        else:
            reason, status = LIMIT_REASON, PENDING
        rows[rel_path] = _row(rel_path, entry.get("bytes"), reason, drive_id, status)

    for entry in listing:  # 1b. over-limit listed files that the upload ledger holds stay visible as uploaded
        rel_path, drive_id = str(entry.get("rel_path", "")), str(entry.get("id", ""))
        if common.is_zip(rel_path) or _as_int(entry.get("fileSize")) <= CONNECTOR_LIMIT_BYTES:
            continue
        held = upload.get("id:" + drive_id) or upload.get("path:" + rel_path)
        if held is None:
            continue
        if held.get("transport") == "upload-assembled" and held.get("size_match") is False:
            reason = ASSEMBLED_REASON
        else:
            reason = "over connector 10 MB limit; supplied by upload"
        rows[rel_path] = _row(rel_path, entry.get("fileSize"), reason, drive_id, UPLOADED)

    for entry in listing:  # 2. in-scope listed files not pulled for another recorded reason
        rel_path, drive_id = str(entry.get("rel_path", "")), str(entry.get("id", ""))
        planned = plan_by_id.get(drive_id)
        plan_status = str(planned.get("status", "")) if planned else ""
        if (rel_path in rows or common.is_zip(rel_path) or plan_status in ("fetched", "skipped_zip", BLOCKED_STATUS)
                or ("id:" + drive_id) in pull or not in_scope(rel_path, pilots)):
            continue
        if uploaded(rel_path, drive_id):
            rows[rel_path] = _row(rel_path, entry.get("fileSize"), "supplied by upload", drive_id, UPLOADED)
        elif is_mot(rel_path) and rel_path in skips:  # the MOT NSC disciplines outside ACMV are not inputs
            continue
        elif recorded_out_of_scope(rel_path):
            rows[rel_path] = _row(rel_path, entry.get("fileSize"), skips[rel_path], drive_id, NOT_NEEDED)
        elif rel_path in skips:
            rows[rel_path] = _row(rel_path, entry.get("fileSize"), skips[rel_path], drive_id, PENDING)
        elif (source / rel_path).is_file():
            gap.append(rel_path)  # on disk, just never ledgered: not a blocked input
        else:
            rows[rel_path] = _row(rel_path, entry.get("fileSize"), UNRECORDED_REASON, drive_id, PENDING)
    return [rows[key] for key in sorted(rows)], sorted(gap)


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def render(rows: Iterable[Dict[str, Any]]) -> str:
    """The CSV text for ``rows`` (the format ``write_csv_dicts`` writes)."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(COLUMNS))
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
    return buffer.getvalue()


def build(out_path: Optional[Path] = None) -> int:
    """``build``: write ``out/qa/blocked_inputs.csv``; 1 when the pull plan is missing."""
    d = _dirs()
    plan = common.read_csv_dicts(d["state"] / "pull_plan.csv")
    if not plan:
        print("error: state/pull_plan.csv missing or empty; run `python -m src.drive_io pull` first", file=sys.stderr)
        return 1
    pull = ledger_index(d["state"], LEDGER_NAMES[0])
    upload = ledger_index(d["state"], LEDGER_NAMES[1])
    rows, gap = build_rows(plan, latest_listing_files(d["state"]), pull, upload, recorded_skips(d["state"]),
                           pilot_departments(), d["source"])
    target = out_path or d["qa"] / FILE_NAME
    text = render(rows)
    changed = not target.is_file() or target.read_bytes() != text.encode("utf-8")
    if changed:
        common.atomic_write_text(target, text)
    counts: Dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    print(f"{'wrote' if changed else 'unchanged'} {target} ({len(rows)} rows: "
          + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) + ")")
    if gap:
        print(f"note: {len(gap)} listed file(s) are on disk without a ledger row (not blocked): {gap[0]}"
              + (f" (+{len(gap) - 1} more)" if len(gap) > 1 else ""))
    progress = Progress(d["state"] / "progress.json")
    digest = common.sha256_file(target)
    if not progress.is_done(PROGRESS_KEY, digest):
        progress.set(PROGRESS_KEY, "done", hash=digest, rows=len(rows))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Argument parser: ``build`` (the default and only command)."""
    parser = argparse.ArgumentParser(description="Write out/qa/blocked_inputs.csv from the pull plan and ledgers.")
    sub = parser.add_subparsers(dest="command")
    build_cmd = sub.add_parser("build", help="write out/qa/blocked_inputs.csv (idempotent)")
    build_cmd.add_argument("--out", type=Path, default=None, help="output path (default: out/qa/blocked_inputs.csv)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point; returns a process exit code."""
    args = build_parser().parse_args(argv)
    try:
        return build(getattr(args, "out", None))
    except Exception as exc:  # noqa: BLE001 - surface any failure as a non-zero exit
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
