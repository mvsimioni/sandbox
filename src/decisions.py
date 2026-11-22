"""Record and list engineering decisions in ``out/qa/decisions_needed.md``.

The file is a Markdown document.  Any text before the first entry is a
preamble that is preserved verbatim.  Each entry has the shared shape::

    ### D-001 Drive transport
    - Status: open
    - Context: why the question arises
    - Options: a; b; c
    - Provisional value: what the pipeline assumes meanwhile
    - Asked at: 2026-11-22T10:00:00Z

``add`` is idempotent on the id: an existing entry is left untouched unless
``--update`` is given.  ``list`` prints a table, ``next-id`` the next free id.
Nothing here touches the network.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

try:  # package import (python -m src.decisions, tests)
    from src.common import atomic_write_text, resolve_paths, utc_now_iso
except ModuleNotFoundError:  # run as a plain script from inside src/
    from common import atomic_write_text, resolve_paths, utc_now_iso  # type: ignore[no-redef]

FILE_NAME = "decisions_needed.md"
DEFAULT_PREAMBLE = (
    "# Decisions needed\n\n"
    "Questions the pipeline cannot settle on its own.  Each entry carries the value the\n"
    "pipeline uses provisionally; the engineer confirms or replaces it at the next gate.\n\n"
)
FIELDS: Tuple[str, ...] = ("Status", "Context", "Options", "Provisional value", "Asked at")
"""Field labels in the order they are written."""

_HEADING_RE = re.compile(r"^###\s+(D-\d{3,})\s*(.*?)\s*$")
_FIELD_RE = re.compile(
    r"^\s*(?:[-*]\s+)?\**(Status|Context|Options|Provisional value|Asked at)\**\s*:\**\s*(.*?)\s*$",
    re.IGNORECASE,
)
_ID_RE = re.compile(r"^D-(\d{3,})$")


@dataclass
class Decision:
    """One entry of the decisions file."""

    id: str
    title: str
    fields: Dict[str, str] = field(default_factory=dict)

    def get(self, label: str) -> Optional[str]:
        """Value of a field by its canonical label, or ``None`` when the line is absent."""
        return self.fields.get(label)

    def render(self) -> str:
        """The entry as Markdown, fields in canonical order followed by any extras."""
        lines = [f"### {self.id} {self.title}".rstrip()]
        for label in FIELDS:
            if label in self.fields:
                lines.append(f"- {label}: {self.fields[label]}")
        for label, value in self.fields.items():
            if label not in FIELDS:
                lines.append(f"- {label}: {value}")
        return "\n".join(lines) + "\n"


def _canonical_label(label: str) -> str:
    """Map a case-insensitive field label onto its canonical spelling."""
    lowered = label.strip().lower()
    for canonical in FIELDS:
        if canonical.lower() == lowered:
            return canonical
    return label.strip()


# --------------------------------------------------------------------------- #
# Location, parsing and rendering
# --------------------------------------------------------------------------- #
def decisions_path() -> Path:
    """``<qa dir>/decisions_needed.md`` for the current project root."""
    return resolve_paths()["qa"] / FILE_NAME


def parse_text(text: str) -> Tuple[str, List[Decision]]:
    """Split a decisions document into ``(preamble, entries)``.

    Lines of an entry that do not look like a field are appended to the last
    field seen (continuation) or ignored when no field has started yet.
    """
    preamble: List[str] = []
    entries: List[Decision] = []
    current: Optional[Decision] = None
    last_label: Optional[str] = None
    for line in text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            current = Decision(id=heading.group(1), title=heading.group(2))
            entries.append(current)
            last_label = None
            continue
        if current is None:
            preamble.append(line)
            continue
        match = _FIELD_RE.match(line)
        if match:
            last_label = _canonical_label(match.group(1))
            current.fields[last_label] = match.group(2)
        elif last_label is not None and line.strip():
            current.fields[last_label] = (current.fields[last_label] + " " + line.strip()).strip()
    return "\n".join(preamble).rstrip("\n") + ("\n" if preamble else ""), entries


def parse_file(path: Path) -> Tuple[str, List[Decision]]:
    """Parse ``path``; a missing file yields the default preamble and no entries."""
    if not path.is_file():
        return DEFAULT_PREAMBLE, []
    return parse_text(path.read_text(encoding="utf-8"))


def render_document(preamble: str, entries: Sequence[Decision]) -> str:
    """Assemble the whole document from its preamble and entries."""
    head = preamble if preamble.strip() else DEFAULT_PREAMBLE
    if not head.endswith("\n"):
        head += "\n"
    if not head.endswith("\n\n"):
        head += "\n"
    return head + "\n".join(entry.render() for entry in entries)


def write_file(path: Path, preamble: str, entries: Sequence[Decision]) -> Path:
    """Write the document atomically."""
    return atomic_write_text(path, render_document(preamble, entries))


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #
def next_id(entries: Sequence[Decision]) -> str:
    """The lowest unused ``D-NNN`` after the highest id present (``D-001`` when empty)."""
    highest = 0
    for entry in entries:
        match = _ID_RE.match(entry.id)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"D-{highest + 1:03d}"


def _normalise_options(options: Sequence[str]) -> str:
    """Join option strings with ``; ``, splitting any that already carry semicolons."""
    parts: List[str] = []
    for item in options:
        parts.extend(p.strip() for p in str(item).split(";") if p.strip())
    return "; ".join(parts)


def build_entry(decision_id: str, title: str, context: str, options: Sequence[str], provisional: str,
                status: str = "open", asked_at: Optional[str] = None) -> Decision:
    """Create a :class:`Decision` with every shared field populated."""
    if not _ID_RE.match(decision_id):
        raise ValueError(f"decision id must look like D-001, got {decision_id!r}")
    return Decision(
        id=decision_id,
        title=title.strip(),
        fields={
            "Status": status.strip() or "open",
            "Context": " ".join(context.split()),
            "Options": _normalise_options(options),
            "Provisional value": " ".join(provisional.split()),
            "Asked at": asked_at or utc_now_iso(),
        },
    )


def add_entry(path: Path, entry: Decision, update: bool = False) -> Tuple[str, Decision]:
    """Add ``entry`` to the file; return ``("added" | "unchanged" | "updated", entry_on_disk)``.

    Idempotent on the id: a second ``add`` with the same id changes nothing
    unless ``update`` is true, in which case the supplied fields replace the
    old ones (``Asked at`` is kept from the original entry).
    """
    preamble, entries = parse_file(path)
    for index, existing in enumerate(entries):
        if existing.id != entry.id:
            continue
        if not update:
            return "unchanged", existing
        merged = Decision(id=entry.id, title=entry.title or existing.title, fields=dict(existing.fields))
        merged.fields.update({k: v for k, v in entry.fields.items() if k != "Asked at"})
        entries[index] = merged
        write_file(path, preamble, entries)
        return "updated", merged
    entries.append(entry)
    write_file(path, preamble, entries)
    return "added", entry


def set_status(path: Path, decision_id: str, status: str) -> bool:
    """Change the Status line of one entry; return False when the id is unknown."""
    preamble, entries = parse_file(path)
    for entry in entries:
        if entry.id == decision_id:
            entry.fields["Status"] = status
            write_file(path, preamble, entries)
            return True
    return False


def table_lines(entries: Sequence[Decision], width: int = 48) -> List[str]:
    """Render the entries as a fixed-width text table."""
    def clip(value: Optional[str]) -> str:
        text = value if value is not None else "(missing)"
        return text if len(text) <= width else text[: width - 1] + "…"

    header = f"{'id':7s} {'status':10s} {'title':32s} provisional value"
    lines = [header, "-" * len(header)]
    for entry in entries:
        lines.append(f"{entry.id:7s} {clip(entry.get('Status'))[:10]:10s} {clip(entry.title)[:32]:32s} "
                     f"{clip(entry.get('Provisional value'))}")
    return lines


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """Argument parser for ``add``, ``list``, ``next-id`` and ``set-status``."""
    parser = argparse.ArgumentParser(description="Maintain out/qa/decisions_needed.md.")
    parser.add_argument("--path", type=Path, default=None, help="decisions file (default: <qa>/decisions_needed.md)")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="add an entry (no-op when the id already exists)")
    add.add_argument("--id", dest="decision_id", default=None, help="D-NNN; default: next free id")
    add.add_argument("--title", required=True)
    add.add_argument("--context", required=True)
    add.add_argument("--options", nargs="+", required=True, help="one or more options; ';' also separates")
    add.add_argument("--provisional", required=True, help="the value used until the decision is made")
    add.add_argument("--status", default="open")
    add.add_argument("--asked-at", default=None, help="ISO UTC timestamp; default: now")
    add.add_argument("--update", action="store_true", help="replace the fields of an existing id")

    sub.add_parser("list", help="print a table of entries")
    sub.add_parser("next-id", help="print the next free id")
    status = sub.add_parser("set-status", help="change the Status line of an entry")
    status.add_argument("decision_id")
    status.add_argument("status")
    return parser


def _cmd_add(path: Path, args: argparse.Namespace) -> int:
    """``add``."""
    _, entries = parse_file(path)
    decision_id = args.decision_id or next_id(entries)
    entry = build_entry(decision_id, args.title, args.context, args.options, args.provisional,
                        status=args.status, asked_at=args.asked_at)
    outcome, stored = add_entry(path, entry, update=args.update)
    print(f"{outcome}: {stored.id} {stored.title} -> {path}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point; returns a process exit code."""
    args = build_parser().parse_args(argv)
    try:
        path = args.path if args.path is not None else decisions_path()
        if args.command == "add":
            return _cmd_add(path, args)
        if args.command == "list":
            _, entries = parse_file(path)
            if not entries:
                print(f"no entries in {path}")
                return 0
            print("\n".join(table_lines(entries)))
            return 0
        if args.command == "next-id":
            _, entries = parse_file(path)
            print(next_id(entries))
            return 0
        if args.command == "set-status":
            if not set_status(path, args.decision_id, args.status):
                print(f"no such decision: {args.decision_id}", file=sys.stderr)
                return 1
            print(f"{args.decision_id}: Status = {args.status}")
            return 0
    except Exception as exc:  # noqa: BLE001 - surface any failure as a non-zero exit
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
