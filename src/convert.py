"""Stage 1A orchestration: MarkItDown plus sidecars plus Markdown for every fetched file.

``python -m src.convert run`` walks ``state/pull_ledger.jsonl`` and
``state/upload_ledger.jsonl`` (or the subset chosen with ``--paths``,
``--roots``, ``--dept``, ``--only-class`` and ``--limit``) and, per file:

1. runs MarkItDown through its Python API with no model client and no
   plugins, catching exceptions per file and recording the version;
2. runs the matching sidecar module (``sidecar_pdf``, ``sidecar_pptx``,
   ``sidecar_xlsx`` or ``sidecar_misc``) into ``out/sidecar/<rel path>/``;
3. calls ``md_writer`` to build the Markdown per the layout;
4. runs ``recall_check`` (numeric recall, table and annotation counts) and
   sets the grade;
5. appends the result to ``state/convert_ledger.jsonl`` and updates
   ``state/progress.json``.

Hash-identical duplicates (the same sha256 as an earlier ledger row) get a
short Markdown pointing to the first copy and are recorded as duplicates.
Zips are never opened: they are recorded once as skipped.  Files run in a
multiprocessing pool of ``cpu_count() - 1`` workers with a per-file timeout
(900 s by default).  The run is resumable: a sha256 already in the convert
ledger with the same ``pipeline_version`` is skipped unless ``--force``.
The Markdown path mirrors the source tree under ``out/markdown`` with the
extension replaced by ``.md`` (the original extension is kept before ``.md``
only when two names in one folder would otherwise collide).  Nothing under
``data/source`` is written and nothing here touches the network.
"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import signal
import sys
import time
import traceback
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Deque, Dict, Iterator, List, Optional, Sequence, Set, Tuple

try:  # package import (python -m src.convert, tests)
    from src import md_writer, recall_check, sidecar_misc, sidecar_pdf, sidecar_pptx, sidecar_xlsx
    from src.common import (
        append_jsonl,
        atomic_write_text,
        classify_document,
        ensure_dir,
        is_zip,
        parse_department,
        project_root,
        read_jsonl,
        resolve_paths,
        sha256_file,
        utc_now_iso,
    )
    from src.state import Progress, file_lock
except ModuleNotFoundError:  # run as a plain script from inside src/
    import md_writer  # type: ignore[no-redef]
    import recall_check  # type: ignore[no-redef]
    import sidecar_misc  # type: ignore[no-redef]
    import sidecar_pdf  # type: ignore[no-redef]
    import sidecar_pptx  # type: ignore[no-redef]
    import sidecar_xlsx  # type: ignore[no-redef]
    from common import (  # type: ignore[no-redef]
        append_jsonl,
        atomic_write_text,
        classify_document,
        ensure_dir,
        is_zip,
        parse_department,
        project_root,
        read_jsonl,
        resolve_paths,
        sha256_file,
        utc_now_iso,
    )
    from state import Progress, file_lock  # type: ignore[no-redef]

PIPELINE_VERSION = "1.0.0"
LEDGER_NAME = "convert_ledger.jsonl"
LEDGER_FILES: Tuple[str, ...] = ("pull_ledger.jsonl", "upload_ledger.jsonl")
DEFAULT_TIMEOUT_S = 900
HARD_TIMEOUT_GRACE_S = 60
"""Extra seconds the parent waits beyond the worker's own alarm before it terminates the pool."""
POLL_INTERVAL_S = 0.2
POOL_CONTEXT = "forkserver"
"""Workers start from a clean server process: no fork of a possibly multi-threaded parent."""
ROOT_CHOICES: Tuple[str, ...] = ("Basis", "Template", "DD Sign-OFF")
STATUS_CONVERTED = "converted"
STATUS_DUPLICATE = "duplicate"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"
RESUMABLE_STATUSES: Tuple[str, ...] = (STATUS_CONVERTED, STATUS_DUPLICATE, STATUS_SKIPPED)


class ConversionTimeout(Exception):
    """Raised inside a worker when the per-file alarm fires."""


@dataclass
class Layout:
    """Absolute directories of one project tree."""

    root: Path
    source: Path
    markdown: Path
    sidecar: Path
    state: Path
    qa: Path

    @classmethod
    def from_root(cls, root: Optional[Path] = None) -> "Layout":
        """Resolve the layout from ``config/project.yaml`` (``root`` overrides ``AIGH_ROOT``)."""
        if root is not None:
            os.environ["AIGH_ROOT"] = str(Path(root).resolve())
        base = project_root()
        paths = resolve_paths()
        return cls(root=base, source=paths.get("source", base / "data" / "source"),
                   markdown=paths.get("markdown", base / "out" / "markdown"),
                   sidecar=paths.get("sidecar", base / "out" / "sidecar"), state=paths.get("state", base / "state"),
                   qa=paths.get("qa", base / "out" / "qa"))

    def ledger_path(self) -> Path:
        """``state/convert_ledger.jsonl``."""
        return self.state / LEDGER_NAME


@dataclass
class Task:
    """Everything a worker needs for one file (plain data, so it pickles)."""

    sha256: str
    rel_path: str
    local_path: str
    bytes: int
    drive_id: Optional[str]
    drive_modified: Optional[str]
    kind: str
    doc_class: str
    md_rel: str
    root: str
    markdown_dir: str
    sidecar_dir: str
    state_dir: str
    timeout_s: int
    force: bool
    threshold: float = recall_check.DEFAULT_THRESHOLD
    duplicate_of: Optional[str] = None
    duplicate_md: Optional[str] = None
    scratch_dir: Optional[str] = None


# --------------------------------------------------------------------------- #
# Planning: ledgers, filters, duplicates, output paths
# --------------------------------------------------------------------------- #
def load_source_rows(state_dir: Path) -> List[Dict[str, Any]]:
    """Rows of the pull ledger then the upload ledger, latest row per ``rel_path`` kept in first-seen order."""
    latest: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for name in LEDGER_FILES:
        for row in read_jsonl(state_dir / name):
            if not isinstance(row, dict) or not row.get("rel_path") or not row.get("sha256"):
                continue
            rel = str(row["rel_path"])
            if rel not in latest:
                order.append(rel)
            latest[rel] = row
    return [latest[rel] for rel in order]


def first_by_sha(rows: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """The first ledger row for every sha256 (the canonical copy)."""
    first: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        first.setdefault(str(row["sha256"]), row)
    return first


def colliding_stems(rows: Sequence[Dict[str, Any]]) -> Set[str]:
    """``folder/stem`` keys that more than one distinct file name maps to (they keep their extension)."""
    names_by_key: Dict[str, Set[str]] = {}
    for row in rows:
        rel = Path(str(row["rel_path"]))
        names_by_key.setdefault(f"{rel.parent.as_posix()}/{rel.stem}", set()).add(rel.name)
    return {key for key, names in names_by_key.items() if len(names) > 1}


def md_rel_path(rel_path: str, collisions: Set[str]) -> str:
    """``<rel path>.md`` with the extension replaced, or kept when the stem collides within its folder."""
    rel = Path(rel_path)
    key = f"{rel.parent.as_posix()}/{rel.stem}"
    name = f"{rel.name}.md" if key in collisions else f"{rel.stem}.md"
    return (rel.parent / name).as_posix()


def local_file(row: Dict[str, Any], layout: Layout) -> Path:
    """Absolute path of the fetched copy (``local_path`` relative to the root, else ``data/source/<rel_path>``)."""
    local = row.get("local_path")
    if local:
        path = Path(str(local))
        return path if path.is_absolute() else layout.root / path
    return layout.source / str(row["rel_path"])


def _path_selected(row: Dict[str, Any], wanted: Sequence[str], layout: Layout) -> bool:
    """True when ``--paths`` names this row by rel_path, by ledger local_path or by the file on disk."""
    local = local_file(row, layout)
    names = {str(row["rel_path"]), str(row.get("local_path") or ""), str(local)}
    if names & set(wanted):
        return True
    resolved = local.resolve() if local.exists() else None
    return resolved is not None and any(Path(p).exists() and Path(p).resolve() == resolved for p in wanted)


def _matches(row: Dict[str, Any], args: argparse.Namespace, layout: Layout) -> bool:
    """Apply ``--paths``, ``--roots``, ``--dept`` and ``--only-class`` to one ledger row."""
    rel = str(row["rel_path"])
    if args.paths and not _path_selected(row, args.paths, layout):
        return False
    dept = parse_department(rel)
    if args.roots and dept["root"] not in args.roots:
        return False
    if args.dept and dept["dept_no"] != args.dept:
        return False
    if args.only_class and classify_document(rel) != args.only_class:
        return False
    return True


def ledger_key(sha256: str, rel_path: str) -> str:
    """Key of a convert-ledger row: a duplicate shares its sha256 with the first copy, not its path."""
    return f"{sha256}|{rel_path}"


def load_convert_rows(ledger_path: Path) -> Dict[str, Dict[str, Any]]:
    """Latest convert-ledger row per ``(sha256, rel_path)`` (see :func:`ledger_key`)."""
    latest: Dict[str, Dict[str, Any]] = {}
    for row in read_jsonl(ledger_path):
        if isinstance(row, dict) and row.get("sha256") and row.get("rel_path"):
            latest[ledger_key(str(row["sha256"]), str(row["rel_path"]))] = row
    return latest


def is_done(row: Optional[Dict[str, Any]], layout: Layout) -> bool:
    """True when a convert-ledger row makes a re-run unnecessary (same version, output present)."""
    if not row or row.get("pipeline_version") != PIPELINE_VERSION or row.get("status") not in RESUMABLE_STATUSES:
        return False
    md_path = row.get("md_path")
    return not md_path or (layout.root / str(md_path)).is_file()


def plan_tasks(rows: Sequence[Dict[str, Any]], layout: Layout, args: argparse.Namespace) -> Tuple[List[Task], List[Dict[str, Any]]]:
    """Turn ledger rows into worker tasks; return ``(tasks, rows recorded without a worker)``.

    Zips are recorded as skipped (once) and never opened; rows already done
    for this pipeline version are left out unless ``--force``.
    """
    canonical = first_by_sha(rows)
    collisions = colliding_stems(rows)
    done = load_convert_rows(layout.ledger_path())
    threshold = recall_check.threshold_from_config()
    tasks: List[Task] = []
    recorded: List[Dict[str, Any]] = []
    for row in rows:
        if not _matches(row, layout=layout, args=args):
            continue
        sha, rel = str(row["sha256"]), str(row["rel_path"])
        key = ledger_key(sha, rel)
        if is_zip(rel):
            if not done.get(key):
                recorded.append(_zip_row(row))
            continue
        if not args.force and is_done(done.get(key), layout):
            continue
        first = canonical[sha]
        duplicate_of = str(first["rel_path"]) if str(first["rel_path"]) != rel else None
        tasks.append(Task(
            sha256=sha, rel_path=rel, local_path=str(local_file(row, layout)), bytes=int(row.get("bytes") or 0),
            drive_id=row.get("drive_id"), drive_modified=row.get("drive_modified"),
            kind=recall_check.kind_for_path(rel), doc_class=classify_document(rel),
            md_rel=md_rel_path(rel, collisions), root=str(layout.root), markdown_dir=str(layout.markdown),
            sidecar_dir=str(layout.sidecar / rel), state_dir=str(layout.state), timeout_s=int(args.timeout),
            force=bool(args.force), threshold=threshold, duplicate_of=duplicate_of,
            duplicate_md=md_rel_path(str(first["rel_path"]), collisions) if duplicate_of else None,
            scratch_dir=args.scratch,
        ))
        if args.limit and len(tasks) >= args.limit:
            break
    return tasks, recorded


def _zip_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """The convert-ledger row for a zip that is never opened."""
    return _result(str(row["sha256"]), str(row["rel_path"]), STATUS_SKIPPED, kind="zip", md_path=None,
                   grade=None, grade_reason="zip archives are never opened (brief rule)", seconds=0.0,
                   converters=[], error=None)


def _result(sha256: str, rel_path: str, status: str, **extra: Any) -> Dict[str, Any]:
    """A convert-ledger row with the common fields filled in."""
    row: Dict[str, Any] = {"sha256": sha256, "rel_path": rel_path, "status": status, "md_path": None,
                           "sidecar_dir": None, "checks_path": None, "kind": None, "doc_class": None, "grade": None,
                           "grade_reason": None, "seconds": 0.0, "converters": [], "error": None,
                           "duplicate_of": None, "pipeline_version": PIPELINE_VERSION, "generated_at": utc_now_iso()}
    row.update(extra)
    return row


# --------------------------------------------------------------------------- #
# Per-file work (runs in a worker)
# --------------------------------------------------------------------------- #
def run_markitdown(path: Path) -> md_writer.MarkItDownOutput:
    """Convert one file with MarkItDown (Python API, no model client, no plugins); never raises."""
    import markitdown

    version = str(getattr(markitdown, "__version__", "unknown"))
    started = time.monotonic()
    try:
        converter = markitdown.MarkItDown(enable_plugins=False)
        result = converter.convert(str(path))
        text = result.text_content if result is not None else ""
        return md_writer.MarkItDownOutput(text=text or "", version=version, seconds=time.monotonic() - started)
    except ConversionTimeout:
        raise
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
        return md_writer.MarkItDownOutput(text=None, version=version, error=f"{type(exc).__name__}: {str(exc)[:300]}",
                                          seconds=time.monotonic() - started)


def run_sidecar(kind: str, path: Path, out_dir: Path, state_dir: Path, force: bool,
                scratch_dir: Optional[Path]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Run the sidecar module for ``kind`` (idempotent by sha256); return ``(manifest, error)``."""
    try:
        if kind == "pdf":
            return sidecar_pdf.build_sidecars(path, out_dir, force=force, state_dir=state_dir), None
        sha = sha256_file(path)
        module = {"pptx": sidecar_pptx, "xlsx": sidecar_xlsx}.get(kind, sidecar_misc)
        if not force and module.already_done(out_dir, sha):
            return md_writer._json(out_dir / "sidecar_manifest.json", None), None  # noqa: SLF001
        if kind == "xlsx":
            return sidecar_xlsx.run_one(path, out_dir, state_dir, scratch_dir), None
        return module.run_one(path, out_dir, state_dir), None
    except ConversionTimeout:
        raise
    except Exception as exc:  # noqa: BLE001 - recorded, the Markdown still gets the MarkItDown text
        return None, f"{type(exc).__name__}: {str(exc)[:300]}"


def _sidecar_files(manifest: Optional[Dict[str, Any]]) -> List[str]:
    """File names of a manifest whatever its shape (dict for PDFs, list for the others)."""
    files = (manifest or {}).get("files") or []
    return list(files.keys()) if isinstance(files, dict) else list(files)


def write_duplicate(task: Task) -> Dict[str, Any]:
    """Write the short Markdown of a hash-identical duplicate and return its ledger row."""
    started = time.monotonic()
    source = md_writer.Source(path=Path(task.local_path), rel_path=task.rel_path, sha256=task.sha256,
                              bytes=task.bytes, kind=task.kind, drive_id=task.drive_id,
                              drive_modified=task.drive_modified)
    md = md_writer.MarkItDownOutput(text=None, version="not run", error="duplicate: not converted")
    body = md_writer.Body(text="", units=[], unit_count=0, tables_written=0, comment_rows=0, sidecar_added=0, notes=[])
    reason = f"hash-identical duplicate of {task.duplicate_of}; see {task.duplicate_md}"
    fields = md_writer.front_matter_fields(source, md, None, body, "pending", reason, PIPELINE_VERSION)
    fields["converters"] = []
    fields["methods_used"] = ["duplicate-of-first-copy"]
    md_path = Path(task.markdown_dir) / task.md_rel
    atomic_write_text(md_path, md_writer.render_duplicate(fields, task.duplicate_of or "", task.duplicate_md or ""))
    return _result(task.sha256, task.rel_path, STATUS_DUPLICATE, md_path=_relative(md_path, task.root),
                   kind=task.kind, doc_class=task.doc_class, grade="pending", grade_reason=reason,
                   seconds=round(time.monotonic() - started, 3), duplicate_of=task.duplicate_of)


def _relative(path: Path, root: str) -> str:
    """``path`` relative to ``root`` as POSIX when possible, else absolute."""
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return str(path)


def convert_file(task: Task) -> Dict[str, Any]:
    """MarkItDown, sidecar, Markdown and checks for one file; returns the ledger row."""
    started = time.monotonic()
    path = Path(task.local_path)
    if not path.is_file():
        return _result(task.sha256, task.rel_path, STATUS_ERROR, kind=task.kind, doc_class=task.doc_class,
                       error=f"local file missing: {path}")
    if task.duplicate_of:
        return write_duplicate(task)
    sidecar_dir = ensure_dir(task.sidecar_dir)
    md = run_markitdown(path)
    manifest, sidecar_error = run_sidecar(task.kind, path, sidecar_dir, Path(task.state_dir), task.force,
                                          Path(task.scratch_dir) if task.scratch_dir else None)
    source = md_writer.Source(path=path, rel_path=task.rel_path, sha256=task.sha256, bytes=task.bytes,
                              kind=task.kind, drive_id=task.drive_id, drive_modified=task.drive_modified)
    body = md_writer.build_body(source, md, sidecar_dir, manifest, sidecar_error)
    checks = recall_check.run_checks(task.kind, sidecar_dir, body.text, md.text, sidecar_added=body.sidecar_added,
                                     sidecar_ok=manifest is not None, markitdown_error=md.error,
                                     threshold=task.threshold)
    checks_path = recall_check.write_checks(sidecar_dir, checks)
    fields = md_writer.front_matter_fields(source, md, manifest, body, checks["grade"], checks["grade_reason"],
                                           PIPELINE_VERSION)
    provenance = md_writer.render_provenance(source, md, _relative(sidecar_dir, task.root), manifest, sidecar_error,
                                             body, checks)
    md_path = Path(task.markdown_dir) / task.md_rel
    atomic_write_text(md_path, md_writer.render_document(fields, body, provenance))
    return _result(task.sha256, task.rel_path, STATUS_CONVERTED, md_path=_relative(md_path, task.root),
                   sidecar_dir=_relative(sidecar_dir, task.root), checks_path=_relative(checks_path, task.root),
                   kind=task.kind, doc_class=task.doc_class, grade=checks["grade"], grade_reason=checks["grade_reason"],
                   seconds=round(time.monotonic() - started, 3), converters=fields["converters"],
                   error=None if sidecar_error is None and md.error is None else
                   "; ".join(e for e in (f"markitdown: {md.error}" if md.error else "",
                                         f"sidecar: {sidecar_error}" if sidecar_error else "") if e),
                   numeric_recall=checks["numeric_recall"], sidecar_files=len(_sidecar_files(manifest)))


def _alarm_handler(signum: int, frame: Any) -> None:  # noqa: ARG001 - signal handler signature
    """Turn the per-file alarm into an exception."""
    raise ConversionTimeout("per-file timeout")


def convert_task(task_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Worker entry point: run :func:`convert_file` under a SIGALRM per-file timeout; never raises."""
    task = Task(**task_dict)
    os.environ["AIGH_ROOT"] = task.root  # the sidecar modules resolve their paths from the same tree
    previous = signal.signal(signal.SIGALRM, _alarm_handler) if hasattr(signal, "SIGALRM") else None
    try:
        if previous is not None:
            signal.alarm(max(1, int(task.timeout_s)))
        return convert_file(task)
    except ConversionTimeout:
        return _result(task.sha256, task.rel_path, STATUS_ERROR, kind=task.kind, doc_class=task.doc_class,
                       error=f"timeout after {task.timeout_s} s")
    except Exception as exc:  # noqa: BLE001 - every failure becomes a ledger row
        return _result(task.sha256, task.rel_path, STATUS_ERROR, kind=task.kind, doc_class=task.doc_class,
                       error=f"{type(exc).__name__}: {str(exc)[:300]}",
                       traceback=traceback.format_exc()[-2000:])
    finally:
        if previous is not None:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)


# --------------------------------------------------------------------------- #
# Pool with a per-file deadline
# --------------------------------------------------------------------------- #
def default_workers() -> int:
    """``cpu_count() - 1`` with a floor of one."""
    return max(1, (os.cpu_count() or 2) - 1)


def run_tasks(tasks: Sequence[Task], workers: int) -> Iterator[Dict[str, Any]]:
    """Yield one ledger row per task, in completion order.

    With one worker the files run inline.  Otherwise a pool keeps ``workers``
    files in flight; when a worker's own alarm fails to end a file within the
    grace period the pool is terminated, the file is recorded as timed out,
    and a fresh pool takes the remaining files.
    """
    pending: Deque[Task] = deque(tasks)
    if workers <= 1:
        while pending:
            yield convert_task(asdict(pending.popleft()))
        return
    context = multiprocessing.get_context(POOL_CONTEXT)
    pool = context.Pool(processes=workers, maxtasksperchild=1)
    in_flight: Dict[str, Tuple[Task, Any, float]] = {}
    try:
        while pending or in_flight:
            while pending and len(in_flight) < workers:
                task = pending.popleft()
                in_flight[task.sha256 + task.rel_path] = (task, pool.apply_async(convert_task, (asdict(task),)), time.monotonic())
            finished = [key for key, (_t, result, _s) in in_flight.items() if result.ready()]
            for key in finished:
                task, result, _started = in_flight.pop(key)
                yield result.get()
            late = [key for key, (t, _r, started) in in_flight.items()
                    if time.monotonic() - started > t.timeout_s + HARD_TIMEOUT_GRACE_S]
            if late:
                key = late[0]
                task, _result_obj, _started = in_flight.pop(key)
                yield _result(task.sha256, task.rel_path, STATUS_ERROR, kind=task.kind, doc_class=task.doc_class,
                              error=f"hard timeout after {task.timeout_s + HARD_TIMEOUT_GRACE_S} s; pool restarted")
                pending.extendleft(t for t, _r, _s in reversed(list(in_flight.values())))
                in_flight.clear()
                pool.terminate()
                pool.join()
                pool = context.Pool(processes=workers, maxtasksperchild=1)
                continue
            if not finished:
                time.sleep(POLL_INTERVAL_S)
    finally:
        pool.terminate()
        pool.join()


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #
def record(row: Dict[str, Any], layout: Layout, progress: Optional[Progress]) -> None:
    """Append one row to the convert ledger and mirror it in progress.json."""
    ledger = layout.ledger_path()
    with file_lock(ledger):
        append_jsonl(ledger, row)
    if progress is not None:
        status = {STATUS_CONVERTED: "done", STATUS_DUPLICATE: "duplicate", STATUS_SKIPPED: "skipped"}.get(row["status"], "error")
        progress.set(progress_key(row), status, hash=row["sha256"], rel_path=row["rel_path"],
                     md_path=row.get("md_path"), grade=row.get("grade"), error=row.get("error"))


def progress_key(row: Dict[str, Any]) -> str:
    """``convert:<sha256>`` for the first copy; a duplicate adds its own path so it never overwrites the original."""
    if row.get("status") == STATUS_DUPLICATE:
        return f"convert:{row['sha256']}:duplicate:{row['rel_path']}"
    return f"convert:{row['sha256']}"


def summarise(rows: Sequence[Dict[str, Any]]) -> str:
    """One line with counts per status and grade."""
    statuses: Dict[str, int] = {}
    grades: Dict[str, int] = {}
    for row in rows:
        statuses[row["status"]] = statuses.get(row["status"], 0) + 1
        if row.get("grade"):
            grades[str(row["grade"])] = grades.get(str(row["grade"]), 0) + 1
    status_text = ", ".join(f"{k} {v}" for k, v in sorted(statuses.items())) or "nothing to do"
    grade_text = ", ".join(f"{k} {v}" for k, v in sorted(grades.items()))
    return f"convert: {status_text}" + (f"; grades {grade_text}" if grade_text else "")


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """Argument parser: ``run [filters] [--force] [--workers N] [--timeout S] [--dry-run]``."""
    parser = argparse.ArgumentParser(description="Stage 1A: MarkItDown, sidecars, Markdown and checks per file.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="convert the fetched files (resumable)")
    run.add_argument("--root", type=Path, default=None, help="project root (default: AIGH_ROOT or the checkout)")
    run.add_argument("--paths", nargs="+", default=None, help="only these files (ledger rel_path or local path)")
    run.add_argument("--roots", nargs="+", choices=ROOT_CHOICES, default=None, help="only these top-level folders")
    run.add_argument("--dept", default=None, help="only this department number (two digits)")
    run.add_argument("--only-class", default=None, help="only this document class (e.g. RDS)")
    run.add_argument("--limit", type=int, default=None, help="stop after N files")
    run.add_argument("--force", action="store_true", help="reconvert files already in the convert ledger")
    run.add_argument("--workers", type=int, default=None, help="pool size (default: cpu_count() - 1)")
    run.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S, help="per-file timeout in seconds (default 900)")
    run.add_argument("--scratch", default=None, help="scratch parent for .xls conversion copies")
    run.add_argument("--no-progress", action="store_true", help="do not touch progress.json")
    run.add_argument("--dry-run", action="store_true", help="list the files that would run and exit")
    return parser


def run_command(args: argparse.Namespace) -> int:
    """``run``: plan, convert, record; exit 1 when any file ended in error."""
    layout = Layout.from_root(args.root)
    rows = load_source_rows(layout.state)
    tasks, recorded = plan_tasks(rows, layout, args)
    if args.dry_run:
        for task in tasks:
            print(f"{task.kind:6s} {'dup ' if task.duplicate_of else '    '}{task.rel_path} -> {task.md_rel}")
        print(f"{len(tasks)} file(s) would run; {len(recorded)} zip(s) recorded as skipped; "
              f"{len(rows)} ledger row(s) in total")
        return 0
    progress = None if args.no_progress else Progress(layout.state / "progress.json")
    results: List[Dict[str, Any]] = []
    for row in recorded:
        record(row, layout, progress)
        results.append(row)
    workers = args.workers if args.workers else default_workers()
    for row in run_tasks(tasks, workers):
        record(row, layout, progress)
        results.append(row)
        flag = row.get("grade") or row["status"]
        print(f"{row['status']:9s} {flag:7s} {row['rel_path']}" + (f"  ({row['error']})" if row.get("error") else ""))
    print(summarise(results))
    return 1 if any(r["status"] == STATUS_ERROR for r in results) else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point; returns a process exit code."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return run_command(args)
    except Exception as exc:  # noqa: BLE001 - surface any failure as a non-zero exit
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
