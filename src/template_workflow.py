"""Record the state of the template workflow in ``state/progress.json``.

The template workflow (sample zones from the Template samples and the three
template specifications) is written by the engineering agents, not by a
script, so nothing else can tell ``src.checks`` whether it has finished.  This
module is the producer of the progress item ``template_workflow``:

* ``status`` prints which of the expected artefacts exist (exit 0 when all do,
  1 otherwise, never writes);
* ``finish`` marks the workflow ``done`` once every artefact exists, or with
  ``--incomplete --reason ...`` records an honest ``done`` with the missing
  artefacts listed, so P0-12 then FAILs instead of SKIPping;
* ``reopen`` sets the item back to ``running``.

Idempotent: ``finish`` on an already finished workflow changes only the
timestamp.  Nothing here touches the network or ``data/source``.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

try:  # package import (python -m src.template_workflow, tests)
    from src import common
    from src.state import Progress
except ModuleNotFoundError:  # run as a plain script from inside src/
    import common  # type: ignore[no-redef]
    from state import Progress  # type: ignore[no-redef,attr-defined]

PROGRESS_KEY = "template_workflow"
"""Progress item read by ``src.checks`` P0-12."""
REGISTER_FILES: Tuple[str, ...] = ("sample_zones.csv",)
QA_FILES: Tuple[str, ...] = (
    "zoning_sample_description.md", "template_spec_A_RevA.md", "template_spec_B_NDH.md", "template_spec_C_diff.md",
)


def expected_artefacts(paths: Optional[Dict[str, Path]] = None) -> List[Path]:
    """Absolute paths of every artefact the workflow must produce."""
    table = paths if paths is not None else common.resolve_paths()
    root = common.project_root()
    registers = table.get("registers", root / "out" / "registers")
    qa = table.get("qa", root / "out" / "qa")
    return [registers / name for name in REGISTER_FILES] + [qa / name for name in QA_FILES]


def missing_artefacts(artefacts: Sequence[Path]) -> List[str]:
    """Project-relative names of the artefacts that do not exist (or are empty)."""
    root = common.project_root()
    return [str(p.relative_to(root)) if p.is_relative_to(root) else str(p)
            for p in artefacts if not (p.is_file() and p.stat().st_size > 0)]


def artefact_digest(artefacts: Sequence[Path]) -> str:
    """SHA-256 over the hashes of the existing artefacts, in order (the item's ``hash``)."""
    digest = hashlib.sha256()
    for path in artefacts:
        digest.update(path.name.encode("utf-8"))
        digest.update((common.sha256_file(path) if path.is_file() else "missing").encode("ascii"))
    return digest.hexdigest()


def progress_file() -> Path:
    """``<state dir>/progress.json`` for the current project root."""
    return common.resolve_paths().get("state", common.project_root() / "state") / "progress.json"


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_status() -> int:
    """Print one line per artefact; exit 0 only when all exist."""
    artefacts = expected_artefacts()
    missing = set(missing_artefacts(artefacts))
    root = common.project_root()
    for path in artefacts:
        rel = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
        print(f"{'missing' if rel in missing else 'present':8s} {rel}")
    item = Progress(progress_file()).get(PROGRESS_KEY)
    print(f"progress item {PROGRESS_KEY}: {item.get('status') if item else 'not set'}")
    return 1 if missing else 0


def cmd_finish(incomplete: bool, reason: str) -> int:
    """Mark the workflow done; refuse when artefacts are missing unless ``incomplete`` is given with a reason."""
    artefacts = expected_artefacts()
    missing = missing_artefacts(artefacts)
    if missing and not incomplete:
        print(f"template workflow not finished; missing: {', '.join(missing)}", file=sys.stderr)
        print("use --incomplete --reason '...' to record an unfinished workflow as done", file=sys.stderr)
        return 1
    if missing and not reason.strip():
        print("--incomplete needs --reason", file=sys.stderr)
        return 1
    record = Progress(progress_file()).set(PROGRESS_KEY, "done", hash=artefact_digest(artefacts),
                                           missing=missing, reason=reason.strip() or None,
                                           artefacts=len(artefacts))
    print(f"{PROGRESS_KEY}: done ({len(artefacts) - len(missing)}/{len(artefacts)} artefacts) at {record['updated_at']}")
    return 0


def cmd_reopen() -> int:
    """Set the workflow back to ``running``."""
    record = Progress(progress_file()).set(PROGRESS_KEY, "running", missing=[], reason=None)
    print(f"{PROGRESS_KEY}: running at {record['updated_at']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Argument parser for ``status``, ``finish`` and ``reopen``."""
    parser = argparse.ArgumentParser(description="Record the template workflow's state in progress.json.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show which artefacts exist")
    finish = sub.add_parser("finish", help="mark the workflow done (requires every artefact)")
    finish.add_argument("--incomplete", action="store_true", help="mark done although artefacts are missing")
    finish.add_argument("--reason", default="", help="why the workflow is closed incomplete")
    sub.add_parser("reopen", help="mark the workflow running again")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point; returns a process exit code."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "status":
            return cmd_status()
        if args.command == "finish":
            return cmd_finish(args.incomplete, args.reason)
        if args.command == "reopen":
            return cmd_reopen()
    except Exception as exc:  # noqa: BLE001 - surface any failure as a non-zero exit
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
