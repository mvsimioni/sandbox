"""Record and list engineering decisions in ``out/qa/decisions_needed.md``.

The file is a Markdown document that engineers also edit by hand, so every
operation here works on the raw lines and changes only the lines it must.
Text before the first entry, free paragraphs under an entry, sub-bullets and
later ``##`` sections all survive an ``add`` or ``set-status`` untouched.
Each entry has the shared shape::

    ### D-001 Drive transport
    - Status: open
    - Context: why the question arises
    - Options: a; b; c
    - Provisional value: what the pipeline assumes meanwhile
    - Asked at: 2026-11-22T10:00:00Z

An entry runs from its ``### D-NNN`` heading to the next heading of level one
to three.  New entries are inserted after the last entry of the first run of
entries (so a trailing ``## Resolved`` section stays last).

``add`` is idempotent on the id: an existing entry is left untouched unless
``--update`` is given.  ``list`` prints a table, ``next-id`` the next free id.
Writes hold the shared advisory lock from :mod:`src.state` and replace the file
atomically, so several agents may add entries at once.  An id that appears
twice is a malformed register: ``add`` and ``set-status`` refuse to touch it.
Nothing here touches the network.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

try:  # package import (python -m src.decisions, tests)
    from src.common import atomic_write_text, resolve_paths, utc_now_iso
    from src.state import file_lock
except ModuleNotFoundError:  # run as a plain script from inside src/
    from common import atomic_write_text, resolve_paths, utc_now_iso  # type: ignore[no-redef]
    from state import file_lock  # type: ignore[no-redef,attr-defined]

FILE_NAME = "decisions_needed.md"
DEFAULT_PREAMBLE = (
    "# Decisions needed\n\n"
    "Questions the pipeline cannot settle on its own.  Each entry carries the value the\n"
    "pipeline uses provisionally; the engineer confirms or replaces it at the next gate.\n\n"
)
FIELDS: Tuple[str, ...] = ("Status", "Context", "Options", "Provisional value", "Asked at")
"""Field labels in the order they are written."""

_HEADING_RE = re.compile(r"^###\s+(D-\d{2,})\s*:?\s*(.*?)\s*$")
_ANY_HEADING_RE = re.compile(r"^#{1,3}\s")
_FIELD_RE = re.compile(
    r"^\s*(?:[-*]\s+)?\**(Status|Context|Options|Provisional value|Asked at)\**\s*:\**\s*(.*?)\s*$",
    re.IGNORECASE,
)
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*?)\s*$")
_ID_RE = re.compile(r"^D-(\d{2,})$")

Span = Tuple[int, int]
"""Half-open line range inside an entry (index 0 is the heading)."""


@dataclass
class Decision:
    """One entry of the decisions file.

    ``lines`` holds the raw lines of a parsed entry (heading first); a freshly
    built entry has no raw lines and renders in the canonical shape.
    """

    id: str
    title: str
    fields: Dict[str, str] = field(default_factory=dict)
    lines: List[str] = field(default_factory=list)
    spans: Dict[str, Span] = field(default_factory=dict)
    start: int = -1

    def get(self, label: str) -> Optional[str]:
        """Value of a field by its canonical label, or ``None`` when the line is absent."""
        return self.fields.get(label)

    def canonical_lines(self) -> List[str]:
        """Heading and field lines in the shared shape, canonical fields first."""
        lines = [f"### {self.id} {self.title}".rstrip()]
        lines += [f"- {label}: {self.fields[label]}" for label in FIELDS if label in self.fields]
        lines += [f"- {label}: {value}" for label, value in self.fields.items() if label not in FIELDS]
        return lines

    def render(self) -> str:
        """The entry as Markdown: raw lines when parsed, canonical lines when new."""
        return "\n".join(self.lines or self.canonical_lines()) + "\n"


def _canonical_label(label: str) -> str:
    """Map a case-insensitive field label onto its canonical spelling."""
    lowered = label.strip().lower()
    for canonical in FIELDS:
        if canonical.lower() == lowered:
            return canonical
    return label.strip()


# --------------------------------------------------------------------------- #
# Document model: raw lines, entries located by heading
# --------------------------------------------------------------------------- #
def _is_continuation(line: str) -> bool:
    """An indented, non-blank line that is not itself a field line continues the field above."""
    return bool(line.strip()) and line[:1].isspace() and not _FIELD_RE.match(line)


def _parse_fields(lines: Sequence[str]) -> Tuple[Dict[str, str], Dict[str, Span]]:
    """Fields of one entry (``lines[0]`` is the heading) with the span each one occupies.

    A field value continues over indented lines; indented bullets are joined
    with ``; `` and plain continuation lines with a space.  The first
    occurrence of a label wins.
    """
    fields: Dict[str, str] = {}
    spans: Dict[str, Span] = {}
    index = 1
    while index < len(lines):
        match = _FIELD_RE.match(lines[index])
        if not match:
            index += 1
            continue
        label = _canonical_label(match.group(1))
        value = match.group(2)
        end = index + 1
        while end < len(lines) and _is_continuation(lines[end]):
            bullet = _BULLET_RE.match(lines[end])
            text = bullet.group(1) if bullet else lines[end].strip()
            value = (value + ("; " if bullet else " ") + text) if value else text
            end += 1
        if label not in fields:
            fields[label] = value
            spans[label] = (index, end)
        index = end
    return fields, spans


@dataclass
class Document:
    """The decisions file as a list of lines plus the entries found in them."""

    lines: List[str] = field(default_factory=list)

    @property
    def entries(self) -> List[Decision]:
        """Entries in file order, each with its raw lines and field spans."""
        starts = [i for i, line in enumerate(self.lines) if _HEADING_RE.match(line)]
        entries: List[Decision] = []
        for start in starts:
            end = next((k for k in range(start + 1, len(self.lines)) if _ANY_HEADING_RE.match(self.lines[k])),
                       len(self.lines))
            heading = _HEADING_RE.match(self.lines[start])
            assert heading is not None  # starts were selected by this regex
            raw = list(self.lines[start:end])
            fields, spans = _parse_fields(raw)
            entries.append(Decision(id=heading.group(1), title=heading.group(2), fields=fields, lines=raw,
                                    spans=spans, start=start))
        return entries

    def preamble(self) -> str:
        """Text before the first entry (``""`` when the file starts with an entry)."""
        first = next((e.start for e in self.entries), len(self.lines))
        head = self.lines[:first]
        return "\n".join(head).rstrip("\n") + ("\n" if head else "")

    def find(self, decision_id: str) -> List[Decision]:
        """Every entry carrying ``decision_id`` (more than one means the file is malformed)."""
        return [e for e in self.entries if e.id == decision_id]

    def insert_index(self) -> int:
        """Where a new entry goes: after the last entry of the first run of entries."""
        entries = self.entries
        if not entries:
            return len(self.lines)
        for k in range(entries[0].start + 1, len(self.lines)):
            if _ANY_HEADING_RE.match(self.lines[k]) and not _HEADING_RE.match(self.lines[k]):
                return k
        return len(self.lines)

    def render(self) -> str:
        """The whole document with a single trailing newline."""
        return ("\n".join(self.lines) + "\n") if self.lines else ""


def decisions_path() -> Path:
    """``<qa dir>/decisions_needed.md`` for the current project root."""
    return resolve_paths()["qa"] / FILE_NAME


def load_document(path: Path) -> Document:
    """Read ``path`` into a :class:`Document`; a missing or blank file starts from the default preamble."""
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if not text.strip():
        text = DEFAULT_PREAMBLE
    return Document(text.splitlines())


def parse_text(text: str) -> Tuple[str, List[Decision]]:
    """Split a decisions document into ``(preamble, entries)``."""
    doc = Document(text.splitlines())
    return doc.preamble(), doc.entries


def parse_file(path: Path) -> Tuple[str, List[Decision]]:
    """Parse ``path``; a missing file yields the default preamble and no entries."""
    if not path.is_file():
        return DEFAULT_PREAMBLE, []
    return parse_text(path.read_text(encoding="utf-8"))


def duplicate_ids(entries: Sequence[Decision]) -> List[str]:
    """Ids that appear more than once, in order of first appearance."""
    seen: Dict[str, int] = {}
    for entry in entries:
        seen[entry.id] = seen.get(entry.id, 0) + 1
    return [decision_id for decision_id, count in seen.items() if count > 1]


# --------------------------------------------------------------------------- #
# Building entries
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


def build_entry(decision_id: Optional[str], title: str, context: str, options: Sequence[str], provisional: str,
                status: str = "open", asked_at: Optional[str] = None) -> Decision:
    """Create a :class:`Decision` with every shared field populated.

    ``decision_id`` may be ``None``: :func:`add_entry` then allocates the next
    free id under the file lock.
    """
    if decision_id is not None and not _ID_RE.match(decision_id):
        raise ValueError(f"decision id must look like D-001, got {decision_id!r}")
    return Decision(
        id=decision_id or "",
        title=" ".join(title.split()),
        fields={
            "Status": status.strip() or "open",
            "Context": " ".join(context.split()),
            "Options": _normalise_options(options),
            "Provisional value": " ".join(provisional.split()),
            "Asked at": asked_at or utc_now_iso(),
        },
    )


# --------------------------------------------------------------------------- #
# Line-level edits
# --------------------------------------------------------------------------- #
def _set_field(doc: Document, decision_id: str, label: str, value: str) -> bool:
    """Replace (or insert) one field line of an entry; return True when the text changed."""
    entry = doc.find(decision_id)[0]
    line = f"- {label}: {value}"
    if label in entry.spans:
        start, end = entry.spans[label]
        if entry.lines[start:end] == [line]:
            return False
        doc.lines[entry.start + start:entry.start + end] = [line]
        return True
    after = max((end for _, end in entry.spans.values()), default=1)
    doc.lines.insert(entry.start + after, line)
    return True


def _set_title(doc: Document, decision_id: str, title: str) -> bool:
    """Rewrite the heading line of an entry; return True when it changed."""
    entry = doc.find(decision_id)[0]
    heading = f"### {decision_id} {title}".rstrip()
    if doc.lines[entry.start] == heading:
        return False
    doc.lines[entry.start] = heading
    return True


def _insert_entry(doc: Document, entry: Decision) -> None:
    """Insert a new entry at :meth:`Document.insert_index` with blank-line separation."""
    index = doc.insert_index()
    block = entry.canonical_lines()
    if index > 0 and doc.lines[index - 1].strip():
        block.insert(0, "")
    if index < len(doc.lines):
        block.append("")
    doc.lines[index:index] = block


def _single(doc: Document, decision_id: str, path: Path) -> Optional[Decision]:
    """The one entry with ``decision_id`` (``None`` when absent); raise when the id is duplicated."""
    matches = doc.find(decision_id)
    if len(matches) > 1:
        raise ValueError(f"{decision_id} appears {len(matches)} times in {path}; fix the file by hand")
    return matches[0] if matches else None


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #
def add_entry(path: Path, entry: Decision, update: bool = False) -> Tuple[str, Decision]:
    """Add ``entry`` to the file; return ``("added" | "unchanged" | "updated", entry_on_disk)``.

    Idempotent on the id: a second ``add`` with the same id changes nothing
    unless ``update`` is true, in which case the supplied fields replace the
    old ones line by line (``Asked at`` is kept from the original entry).  An
    entry without an id receives the next free one.  The whole read, modify
    and write happens under the file lock.
    """
    with file_lock(path):
        doc = load_document(path)
        if not entry.id:
            entry = replace(entry, id=next_id(doc.entries))
        existing = _single(doc, entry.id, path)
        if existing is None:
            _insert_entry(doc, entry)
            atomic_write_text(path, doc.render())
            return "added", doc.find(entry.id)[0]
        if not update:
            return "unchanged", existing
        changed = bool(entry.title) and _set_title(doc, entry.id, entry.title)
        for label, value in entry.fields.items():
            if label != "Asked at":
                changed = _set_field(doc, entry.id, label, value) or changed
        if not changed:
            return "unchanged", existing
        atomic_write_text(path, doc.render())
        return "updated", doc.find(entry.id)[0]


def set_status(path: Path, decision_id: str, status: str) -> bool:
    """Change the Status line of one entry; return False when the id is unknown."""
    with file_lock(path):
        doc = load_document(path)
        if _single(doc, decision_id, path) is None:
            return False
        if _set_field(doc, decision_id, "Status", status):
            atomic_write_text(path, doc.render())
        return True


def table_lines(entries: Sequence[Decision], width: int = 48) -> List[str]:
    """Render the entries as a fixed-width text table (ASCII only)."""
    def clip(value: Optional[str]) -> str:
        text = value if value is not None else "(missing)"
        return text if len(text) <= width else text[: width - 3] + "..."

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
    add.add_argument("--id", dest="decision_id", default=None,
                     help="D-NNN; default: next free id (only the --id form is idempotent)")
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
    entry = build_entry(args.decision_id, args.title, args.context, args.options, args.provisional,
                        status=args.status, asked_at=args.asked_at)
    outcome, stored = add_entry(path, entry, update=args.update)
    print(f"{outcome}: {stored.id} {stored.title} -> {path}")
    return 0


def _cmd_list(path: Path) -> int:
    """``list``: a table, with a warning line for duplicated ids."""
    _, entries = parse_file(path)
    if not entries:
        print(f"no entries in {path}")
        return 0
    print("\n".join(table_lines(entries)))
    dupes = duplicate_ids(entries)
    if dupes:
        print(f"warning: duplicated id(s): {', '.join(dupes)}", file=sys.stderr)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point; returns a process exit code."""
    args = build_parser().parse_args(argv)
    try:
        path = args.path if args.path is not None else decisions_path()
        if args.command == "add":
            return _cmd_add(path, args)
        if args.command == "list":
            return _cmd_list(path)
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
