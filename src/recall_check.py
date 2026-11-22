"""Numeric recall, table-count and annotation-count checks for one converted file.

``python -m src.recall_check run --kind pdf --sidecar <dir> --md <file>`` reads
the sidecar directory written by the matching ``sidecar_*`` module and the
final Markdown, computes

* **numeric recall**: the share of numeric tokens from the native text layer
  (PDF words), the shapes (PPTX), the cells (workbooks) or the body text
  (e-mail) that appear in the Markdown body, after normalising thousands
  separators, a leading ``+`` and trailing zeros (``1,200.0`` == ``1200``);
* **table count**: tables in the sidecar versus Markdown tables under the
  writer's ``### Tables`` sections;
* **annotation count**: annotations or comments in the sidecar versus rows in
  the ``## Comments register`` table,

writes ``checks.json`` into the sidecar directory and returns the grade per
the Markdown layout: ``A`` when the native layer exists and every check passes
on the MarkItDown text alone, ``B`` when the sidecar had to supply what
MarkItDown missed and the checks pass, ``C`` when gaps remain, ``pending``
for photos (no text layer; a caption comes later).

The tokenisers are the sidecar modules' own ``numeric_tokens`` so the recall
is comparable with the counts they record.  The writer marks its sections
with ``<!-- section: ... -->`` comments; the counts here follow those markers,
so a heading inside the verbatim MarkItDown text cannot be mistaken for one
of the writer's.  Nothing here touches the network.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

try:  # package import (python -m src.recall_check, tests)
    from src import sidecar_misc, sidecar_pdf, sidecar_pptx, sidecar_xlsx
    from src.common import atomic_write_json, load_config, read_csv_dicts, read_jsonl
except ModuleNotFoundError:  # run as a plain script from inside src/
    import sidecar_misc  # type: ignore[no-redef]
    import sidecar_pdf  # type: ignore[no-redef]
    import sidecar_pptx  # type: ignore[no-redef]
    import sidecar_xlsx  # type: ignore[no-redef]
    from common import atomic_write_json, load_config, read_csv_dicts, read_jsonl  # type: ignore[no-redef]

CHECKS_NAME = "checks.json"
DEFAULT_THRESHOLD = 0.995
MAX_MISSES_LISTED = 50
KINDS: Tuple[str, ...] = ("pdf", "pptx", "xlsx", "email", "image", "other")
GRADES: Tuple[str, ...] = ("A", "B", "C", "pending")

SECTION_MARK_RE = re.compile(r"^<!-- section: ([a-z-]+) -->\s*$")
FRONT_MATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
TABLE_SEPARATOR_RE = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")

SECTION_TABLES = "tables"
SECTION_ANNOTATIONS = "annotations"
SECTION_REGISTER = "comments-register"
SECTION_PROVENANCE = "provenance"


# --------------------------------------------------------------------------- #
# Token normalisation
# --------------------------------------------------------------------------- #
def normalise_number(token: str) -> Optional[str]:
    """Canonical form of a numeric token: no sign ``+``, no thousands separators, no trailing zeros.

    ``+1,200.50`` -> ``1200.5``; ``007`` -> ``7``; ``.5`` -> ``0.5``; ``-0`` -> ``0``.
    Returns ``None`` when the token holds no digit.
    """
    text = str(token).strip().replace(",", "").lstrip("+")
    negative = text.startswith("-")
    text = text.lstrip("-")
    if not text or not any(ch.isdigit() for ch in text):
        return None
    if "." in text:
        integer, fraction = text.split(".", 1)
        fraction = fraction.rstrip("0")
    else:
        integer, fraction = text, ""
    integer = integer.lstrip("0") or "0"
    out = integer + (f".{fraction}" if fraction else "")
    if out == "0":
        negative = False
    return ("-" if negative else "") + out


def normalised_set(tokens: Iterable[str]) -> Set[str]:
    """Apply :func:`normalise_number` to every token and drop the empties."""
    out: Set[str] = set()
    for token in tokens:
        value = normalise_number(token)
        if value is not None:
            out.add(value)
    return out


def numeric_tokens_for(kind: str, text: str) -> Set[str]:
    """Normalised numeric tokens of a free text, tokenised the way ``kind``'s sidecar does it."""
    if kind == "pdf":
        return normalised_set(sidecar_pdf.numeric_tokens(text.split()))
    if kind == "pptx":
        return normalised_set(sidecar_pptx.numeric_tokens(text))
    if kind == "xlsx":
        return normalised_set(sidecar_xlsx.numeric_tokens(text))
    return normalised_set(sidecar_misc.numeric_tokens(text))


def word_key(word: str) -> str:
    """Comparison key for a word: lower case, surrounding punctuation stripped, numbers normalised."""
    core = str(word).strip().strip("()[]{}<>,;:'\"*#=|").lower()
    number = normalise_number(core) if core and core[0] in "+-.0123456789" else None
    if number is not None and re.fullmatch(r"[+-]?[\d,]*\.?\d*", core):
        return number
    return core


def word_keys(text: str) -> Set[str]:
    """Set of :func:`word_key` values for every whitespace-separated word of ``text``."""
    return {key for key in (word_key(w) for w in str(text).split()) if key}


# --------------------------------------------------------------------------- #
# Markdown structure
# --------------------------------------------------------------------------- #
def strip_front_matter(md_text: str) -> str:
    """The Markdown without its leading YAML front matter."""
    return FRONT_MATTER_RE.sub("", md_text, count=1)


def body_text(md_text: str) -> str:
    """The checked part of a Markdown file: after the front matter, before the Provenance marker."""
    text = strip_front_matter(md_text)
    marker = f"<!-- section: {SECTION_PROVENANCE} -->"
    position = text.rfind(marker)
    return text[:position] if position >= 0 else text


def _section_lines(text: str, section: str) -> Iterable[List[str]]:
    """Yield the lines of every region that starts at ``<!-- section: <section> -->`` and ends at the next marker."""
    current: Optional[List[str]] = None
    for line in text.splitlines():
        match = SECTION_MARK_RE.match(line)
        if match:
            if current is not None:
                yield current
                current = None
            if match.group(1) == section:
                current = []
            continue
        if current is not None:
            current.append(line)
    if current is not None:
        yield current


def count_tables(lines: Sequence[str]) -> int:
    """Number of Markdown tables in ``lines`` (a table has exactly one separator row after a header row)."""
    count = 0
    for index, line in enumerate(lines):
        if index and TABLE_SEPARATOR_RE.match(line) and lines[index - 1].lstrip().startswith("|"):
            count += 1
    return count


def markdown_table_count(md_body: str) -> int:
    """Tables under the writer's ``### Tables`` sections."""
    return sum(count_tables(lines) for lines in _section_lines(md_body, SECTION_TABLES))


def register_row_count(md_body: str) -> int:
    """Data rows of the ``## Comments register`` table (header and separator excluded)."""
    total = 0
    for lines in _section_lines(md_body, SECTION_REGISTER):
        rows = [line for line in lines if line.lstrip().startswith("|")]
        total += max(0, len(rows) - 2)
    return total


# --------------------------------------------------------------------------- #
# Expected values from the sidecar directory
# --------------------------------------------------------------------------- #
def _json(path: Path, default: Any) -> Any:
    """Parse a JSON file, returning ``default`` when it is missing or unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _pdf_words(sidecar_dir: Path) -> List[Dict[str, Any]]:
    """Rows of ``words.jsonl``."""
    return [row for row in read_jsonl(sidecar_dir / "words.jsonl") if isinstance(row, dict)]


def _pptx_texts(sidecar_dir: Path) -> List[str]:
    """Every shape text of the deck plus the speaker notes."""
    texts: List[str] = []
    for slide in read_jsonl(sidecar_dir / sidecar_pptx.SLIDES_NAME):
        texts.extend(str(shape.get("text", "")) for shape in slide.get("shapes", []))
    notes = sidecar_dir / sidecar_pptx.NOTES_NAME
    if notes.is_file():
        texts.extend(line for line in notes.read_text(encoding="utf-8").splitlines()
                     if line and not line.startswith("#") and line != "(no notes)")
    return texts


def _xlsx_values(sidecar_dir: Path) -> List[str]:
    """Every cached cell value of every sheet (hidden included)."""
    workbook = _json(sidecar_dir / sidecar_xlsx.WORKBOOK_NAME, {})
    values: List[str] = []
    for sheet in workbook.get("sheets", []):
        name = (sheet.get("files") or {}).get("values")
        if name:
            values.extend(str(row.get("value", "")) for row in read_csv_dicts(sidecar_dir / name))
    return values


def _email_body(sidecar_dir: Path) -> str:
    """The body text of an e-mail from ``body.md`` (between the Body and Attachments headings)."""
    path = sidecar_dir / sidecar_misc.BODY_NAME
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8")
    start = text.find("## Body")
    end = text.find("## Attachments")
    if start < 0:
        return ""
    return text[start + len("## Body"): end if end > start else None]


def expected_tokens(kind: str, sidecar_dir: Path) -> Set[str]:
    """Normalised numeric tokens of the native layer recorded by ``kind``'s sidecar."""
    if kind == "pdf":
        return normalised_set(sidecar_pdf.numeric_tokens(_pdf_words(sidecar_dir)))
    if kind == "pptx":
        return normalised_set(t for text in _pptx_texts(sidecar_dir) for t in sidecar_pptx.numeric_tokens(text))
    if kind == "xlsx":
        return normalised_set(t for value in _xlsx_values(sidecar_dir) for t in sidecar_xlsx.numeric_tokens(value))
    if kind == "email":
        return normalised_set(sidecar_misc.numeric_tokens(_email_body(sidecar_dir)))
    return set()


def expected_table_count(kind: str, sidecar_dir: Path) -> int:
    """Tables the sidecar recorded: pdfplumber tables, slide tables, or one range per non-empty sheet."""
    if kind == "pdf":
        return len(_json(sidecar_dir / "tables.json", []))
    if kind == "pptx":
        return int(_json(sidecar_dir / sidecar_pptx.TABLES_NAME, {}).get("count", 0))
    if kind == "xlsx":
        sheets = _json(sidecar_dir / sidecar_xlsx.WORKBOOK_NAME, {}).get("sheets", [])
        return sum(1 for sheet in sheets if int(sheet.get("cells_non_empty", 0)) > 0)
    return 0


def expected_annotation_count(kind: str, sidecar_dir: Path) -> int:
    """Annotations (PDF) or comments (PPTX, workbook) in the sidecar."""
    if kind == "pdf":
        return len(read_jsonl(sidecar_dir / "annotations.jsonl"))
    if kind == "pptx":
        return len(read_jsonl(sidecar_dir / sidecar_pptx.COMMENTS_NAME))
    if kind == "xlsx":
        return len(read_jsonl(sidecar_dir / sidecar_xlsx.COMMENTS_NAME))
    return 0


def native_layer(kind: str, sidecar_dir: Path) -> Tuple[bool, str]:
    """``(present, evidence)``: does the file have a native text layer, cells or body text?"""
    if kind == "pdf":
        words = _json(sidecar_dir / "sidecar_manifest.json", {}).get("counts", {}).get("words")
        if words is None:
            words = len(_pdf_words(sidecar_dir))
        return int(words) > 0, f"{words} words in the text layer"
    if kind == "pptx":
        info = _json(sidecar_dir / sidecar_pptx.SLIDE_INFO_NAME, {})
        slides = int(info.get("slide_count", 0))
        return slides > 0, f"{slides} slides, {int(info.get('totals', {}).get('text_chars', 0))} text chars"
    if kind == "xlsx":
        sheets = _json(sidecar_dir / sidecar_xlsx.WORKBOOK_NAME, {}).get("sheets", [])
        cells = sum(int(s.get("cells_non_empty", 0)) for s in sheets)
        return cells > 0, f"{cells} non-empty cells in {len(sheets)} sheets"
    if kind == "email":
        chars = int(_json(sidecar_dir / sidecar_misc.MESSAGE_NAME, {}).get("body", {}).get("chars", 0))
        return chars > 0, f"{chars} body chars"
    if kind == "image":
        return False, "photo: no text layer"
    return False, "no converter for this type"


# --------------------------------------------------------------------------- #
# Checks and grade
# --------------------------------------------------------------------------- #
def recall(expected: Set[str], found: Set[str]) -> Tuple[float, List[str]]:
    """``(share of expected tokens present in found, sorted misses)``; an empty expectation is recall 1."""
    if not expected:
        return 1.0, []
    misses = sorted(expected - found, key=lambda t: (len(t), t))
    return (len(expected) - len(misses)) / len(expected), misses


def threshold_from_config() -> float:
    """``tolerances.numeric_recall`` from the project configuration, else the default."""
    try:
        value = (load_config().get("tolerances") or {}).get("numeric_recall")
        return float(value) if value is not None else DEFAULT_THRESHOLD
    except Exception:  # noqa: BLE001 - configuration problems fall back to the brief's value
        return DEFAULT_THRESHOLD


def _check(name: str, passed: bool, evidence: str, required: bool = True) -> Dict[str, Any]:
    """One row of the checks table."""
    return {"check": name, "pass": bool(passed), "evidence": evidence, "required": required}


def grade_for(kind: str, checks: Sequence[Dict[str, Any]], markitdown_ok: bool, markitdown_recall_ok: bool,
              sidecar_added: int, native_present: bool, native_evidence: str) -> Tuple[str, str]:
    """Apply the layout's grading rules to the check outcomes."""
    if kind == "image":
        return "pending", "photo: no text layer to check; caption by a model pending"
    if kind == "other":
        return "C", "no converter applies to this file type"
    failed = [c["check"] for c in checks if c["required"] and not c["pass"]]
    if not native_present:
        return "C", f"no native text layer or cells ({native_evidence})"
    if failed:
        return "C", "failed: " + "; ".join(failed)
    if markitdown_ok and markitdown_recall_ok and sidecar_added == 0:
        return "A", "native layer present; MarkItDown text complete; all checks pass"
    reasons = []
    if not markitdown_ok:
        reasons.append("MarkItDown failed")
    if not markitdown_recall_ok:
        reasons.append("MarkItDown text missed numeric tokens")
    if sidecar_added:
        reasons.append(f"sidecar supplied {sidecar_added} missing word(s)")
    return "B", "; ".join(reasons) + "; all checks pass with the sidecar"


def run_checks(kind: str, sidecar_dir: Path, md_body: str, markitdown_text: Optional[str],
               sidecar_added: int = 0, sidecar_ok: bool = True, markitdown_error: Optional[str] = None,
               threshold: Optional[float] = None) -> Dict[str, Any]:
    """Compute every check for one file and return the ``checks.json`` document (not yet written).

    ``md_body`` is the Markdown between the front matter and the Provenance
    section (see :func:`body_text`); ``markitdown_text`` is MarkItDown's raw
    output (``None`` when it failed); ``sidecar_added`` is the number of words
    or cells the writer had to take from the sidecar.
    """
    threshold = DEFAULT_THRESHOLD if threshold is None else float(threshold)
    sidecar_dir = Path(sidecar_dir)
    expected = expected_tokens(kind, sidecar_dir) if sidecar_ok else set()
    final_recall, misses = recall(expected, numeric_tokens_for(kind, md_body))
    md_recall, md_misses = recall(expected, numeric_tokens_for(kind, markitdown_text or ""))
    exp_tables, got_tables = expected_table_count(kind, sidecar_dir), markdown_table_count(md_body)
    exp_annots, got_annots = expected_annotation_count(kind, sidecar_dir), register_row_count(md_body)
    native_present, native_evidence = native_layer(kind, sidecar_dir) if sidecar_ok else (False, "sidecar failed")
    markitdown_ok = markitdown_error is None and markitdown_text is not None
    checks = [
        _check("sidecar extraction succeeded", sidecar_ok, "ok" if sidecar_ok else "sidecar module raised"),
        _check("native text layer or cells present", native_present, native_evidence),
        _check("numeric recall (final Markdown) >= threshold", final_recall >= threshold,
               f"{final_recall:.4f} ({len(expected) - len(misses)} of {len(expected)} tokens; threshold {threshold})"),
        _check("numeric recall (MarkItDown text alone) >= threshold", md_recall >= threshold,
               f"{md_recall:.4f} ({len(expected) - len(md_misses)} of {len(expected)} tokens)", required=False),
        _check("table count: sidecar equals Markdown", exp_tables == got_tables,
               f"sidecar {exp_tables}, Markdown {got_tables}"),
        _check("annotation count: sidecar equals Comments register rows", exp_annots == got_annots,
               f"sidecar {exp_annots}, register {got_annots}"),
        _check("MarkItDown conversion succeeded", markitdown_ok, markitdown_error or "ok", required=False),
    ]
    grade, reason = grade_for(kind, checks, markitdown_ok, md_recall >= threshold, sidecar_added,
                              native_present, native_evidence)
    return {
        "kind": kind,
        "sidecar_dir": str(sidecar_dir),
        "threshold": threshold,
        "numeric_recall": round(final_recall, 6),
        "numeric_recall_markitdown": round(md_recall, 6),
        "expected_tokens": len(expected),
        "misses": misses[:MAX_MISSES_LISTED],
        "misses_total": len(misses),
        "markitdown_misses_total": len(md_misses),
        "tables": {"sidecar": exp_tables, "markdown": got_tables, "match": exp_tables == got_tables},
        "annotations": {"sidecar": exp_annots, "register": got_annots, "match": exp_annots == got_annots},
        "native_layer": {"present": native_present, "evidence": native_evidence},
        "sidecar_added": int(sidecar_added),
        "markitdown_ok": markitdown_ok,
        "checks": checks,
        "grade": grade,
        "grade_reason": reason,
    }


def write_checks(sidecar_dir: Path, result: Dict[str, Any]) -> Path:
    """Write ``checks.json`` into the sidecar directory and return its path."""
    return atomic_write_json(Path(sidecar_dir) / CHECKS_NAME, result)


def kind_for_path(path: Path | str) -> str:
    """Conversion kind from a file name: pdf, pptx, xlsx (xlsx/xlsm/xls), email, image or other."""
    ext = Path(str(path)).suffix.lower().lstrip(".")
    if ext == "pdf":
        return "pdf"
    if ext == "pptx":
        return "pptx"
    if ext in ("xlsx", "xlsm", "xls"):
        return "xlsx"
    if ext in sidecar_misc.EMAIL_EXTS:
        return "email"
    if ext in sidecar_misc.IMAGE_EXTS:
        return "image"
    return "other"


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """Argument parser: ``run --kind K --sidecar DIR --md FILE [--threshold T] [--no-write]``."""
    parser = argparse.ArgumentParser(description="Numeric recall and count checks for one converted file.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="check one Markdown file against its sidecar directory")
    run.add_argument("--kind", choices=KINDS, default=None, help="file kind (default: from the Markdown file name)")
    run.add_argument("--sidecar", required=True, type=Path, help="sidecar directory of the file")
    run.add_argument("--md", required=True, type=Path, help="the final Markdown file")
    run.add_argument("--markitdown-text", type=Path, default=None,
                     help="file holding MarkItDown's raw text (default: the Markdown body is used for both)")
    run.add_argument("--threshold", type=float, default=None, help="recall threshold (default: config or 0.995)")
    run.add_argument("--no-write", action="store_true", help="print the result without writing checks.json")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point; returns a process exit code (1 on failure to run the checks)."""
    args = build_parser().parse_args(argv)
    try:
        md_text = args.md.read_text(encoding="utf-8")
        body = body_text(md_text)
        raw = args.markitdown_text.read_text(encoding="utf-8") if args.markitdown_text else body
        kind = args.kind or kind_for_path(re.sub(r"\.md$", "", args.md.name))
        result = run_checks(kind, args.sidecar, body, raw, threshold=args.threshold or threshold_from_config())
        if not args.no_write:
            write_checks(args.sidecar, result)
    except Exception as exc:  # noqa: BLE001 - surface any failure as a non-zero exit
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({k: result[k] for k in ("kind", "grade", "grade_reason", "numeric_recall", "tables", "annotations")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
