"""Progress state for the AIGH IP ACMV pipeline.

``state/progress.json`` has the shape::

    {"phase": int, "items": {"<key>": {"status": "...", "updated_at": "...", "hash": "..."}}}

Every write goes through an advisory ``fcntl`` lock on a sibling ``.lock``
file and an atomic replace, so parallel workers never clobber each other and a
crash mid-write never leaves a half-written file.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Sequence

try:  # package import (python -m src.state, tests)
    from src.common import PATHS, atomic_write_json, ensure_dir, utc_now_iso
except ModuleNotFoundError:  # run as a plain script from inside src/
    from common import PATHS, atomic_write_json, ensure_dir, utc_now_iso  # type: ignore[no-redef]

DEFAULT_PROGRESS_PATH: Path = PATHS["state"] / "progress.json"


@contextmanager
def file_lock(path: Path | str, exclusive: bool = True) -> Iterator[None]:
    """Hold an advisory ``fcntl`` lock on ``<path>.lock`` for the duration of the block.

    The lock file is created if missing and left in place; it is the lock,
    not the data, so its contents are irrelevant.
    """
    lock_path = Path(str(path) + ".lock")
    ensure_dir(lock_path.parent)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _empty_state() -> Dict[str, Any]:
    """A fresh progress document."""
    return {"phase": 0, "items": {}}


class Progress:
    """Read/modify/write access to ``progress.json`` with locking and atomic writes.

    Each mutating call re-reads the file under the lock, applies the change and
    writes the whole document back, so the class is safe to use from several
    processes at once and is naturally idempotent.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_PROGRESS_PATH

    # -- raw document ------------------------------------------------------- #
    def _read(self) -> Dict[str, Any]:
        """Load the document (without locking); a missing or empty file is an empty state."""
        if not self.path.exists():
            return _empty_state()
        text = self.path.read_text(encoding="utf-8").strip()
        if not text:
            return _empty_state()
        data = json.loads(text)
        data.setdefault("phase", 0)
        data.setdefault("items", {})
        return data

    def _write(self, data: Dict[str, Any]) -> None:
        """Atomically replace the document on disk."""
        atomic_write_json(self.path, data)

    def load(self) -> Dict[str, Any]:
        """Return the whole document, read under a shared lock."""
        with file_lock(self.path, exclusive=False):
            return self._read()

    # -- phase -------------------------------------------------------------- #
    @property
    def phase(self) -> int:
        """Current pipeline phase number."""
        return int(self.load().get("phase", 0))

    def set_phase(self, phase: int) -> None:
        """Record the current pipeline phase."""
        with file_lock(self.path):
            data = self._read()
            data["phase"] = int(phase)
            self._write(data)

    # -- items -------------------------------------------------------------- #
    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Return the item record for ``key`` or ``None`` when unknown."""
        item = self.load()["items"].get(key)
        return dict(item) if item is not None else None

    def status(self, key: str) -> Optional[str]:
        """Convenience: the status string for ``key`` or ``None``."""
        item = self.get(key)
        return item.get("status") if item else None

    def set(self, key: str, status: str, hash: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
        """Set ``key`` to ``status`` (and optional hash / extra fields) and return the record.

        Existing extra fields are kept unless overwritten; ``hash`` is only
        replaced when a new value is supplied.
        """
        with file_lock(self.path):
            data = self._read()
            record: Dict[str, Any] = dict(data["items"].get(key) or {})
            record.update(extra)
            record["status"] = status
            record["updated_at"] = utc_now_iso()
            if hash is not None:
                record["hash"] = hash
            record.setdefault("hash", None)
            data["items"][key] = record
            self._write(data)
            return dict(record)

    def remove(self, key: str) -> bool:
        """Delete ``key``; return True when it existed."""
        with file_lock(self.path):
            data = self._read()
            existed = data["items"].pop(key, None) is not None
            if existed:
                self._write(data)
            return existed

    def items_with_status(self, status: str) -> Dict[str, Dict[str, Any]]:
        """Return ``{key: record}`` for every item whose status equals ``status``."""
        items = self.load()["items"]
        return {k: dict(v) for k, v in items.items() if v.get("status") == status}

    def is_done(self, key: str, hash: Optional[str] = None) -> bool:
        """True when ``key`` is ``done`` and (if given) its stored hash matches ``hash``."""
        item = self.get(key)
        if not item or item.get("status") != "done":
            return False
        return hash is None or item.get("hash") == hash


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """Argument parser for inspecting or editing progress.json by hand."""
    parser = argparse.ArgumentParser(description="Inspect or update the pipeline progress file.")
    parser.add_argument("--path", type=Path, default=None, help="progress file (default: state/progress.json)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show", help="print the whole document")
    get_cmd = sub.add_parser("get", help="print one item")
    get_cmd.add_argument("key")
    set_cmd = sub.add_parser("set", help="set an item's status")
    set_cmd.add_argument("key")
    set_cmd.add_argument("status")
    set_cmd.add_argument("--hash", default=None)
    list_cmd = sub.add_parser("list", help="list keys with a given status")
    list_cmd.add_argument("status")
    phase_cmd = sub.add_parser("phase", help="show or set the phase")
    phase_cmd.add_argument("value", nargs="?", type=int)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point; returns a process exit code (1 on a missing key or error)."""
    args = build_parser().parse_args(argv)
    progress = Progress(args.path)
    try:
        if args.command == "show":
            print(json.dumps(progress.load(), indent=2, ensure_ascii=False))
        elif args.command == "get":
            item = progress.get(args.key)
            if item is None:
                print(f"no such key: {args.key}", file=sys.stderr)
                return 1
            print(json.dumps(item, indent=2, ensure_ascii=False))
        elif args.command == "set":
            print(json.dumps(progress.set(args.key, args.status, hash=args.hash), ensure_ascii=False))
        elif args.command == "list":
            for key in sorted(progress.items_with_status(args.status)):
                print(key)
        elif args.command == "phase":
            if args.value is not None:
                progress.set_phase(args.value)
            print(progress.phase)
    except Exception as exc:  # noqa: BLE001 - surface any failure as a non-zero exit
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
