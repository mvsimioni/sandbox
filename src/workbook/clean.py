"""Deliverable wording (D-015, item 8 of the fix pass): internal names and decision numbers are taken out of the text of every cell before a workbook is saved.

One list of replacements, applied to text cells and to the text literals inside formulas. The list is the whole rule; the language scan of the self-test stays the check.
"""

from __future__ import annotations

import re
from typing import Any, List, Tuple

RULES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\s*\(?\bOI-R?\d+-\d+\b(?:,? open)?\)?"), ""),                                  # open-item numbers
    (re.compile(r"\bcouncil ruling\s+D-\d{3}(?:\s*[/,]\s*D-\d{3})*", re.I), "ruling of the engineer"),
    (re.compile(r"\bInterim, open for Marcus\b"), "Interim, to confirm"),
    (re.compile(r"\bis open for Marcus\b"), "is to confirm"),
    (re.compile(r"\bare open for Marcus\b"), "are to confirm"),
    (re.compile(r"\bopen for Marcus\b"), "to confirm"),
    (re.compile(r"\blisted for Marcus\b"), "listed for review"),
    (re.compile(r"\bfor Marcus\b"), "for the reviewer"),
    (re.compile(r"\bMarcus\b"), "the reviewer"),
    (re.compile(r"\s*\((?:D-\d{3}(?:\s*[,/]\s*|\s+to\s+)?)+\)"), ""),                           # (D-058) and (D-029, D-036)
    (re.compile(r"\bD-\d{3}\b"), "the ruling"),
    (re.compile(r"\bRound 5\b"), "Rev A"),
    (re.compile(r"\bby the ladder of the project\b"), "in the order of the project documents"),
    (re.compile(r"\bcouncil\b", re.I), "design team"),
]


def clean_text(s: str) -> str:
    for rx, new in RULES:
        s = rx.sub(new, s)
    return re.sub(r"  +", " ", s).replace(" ,", ",").replace(" .", ".")


def _clean_formula(f: str) -> str:
    """Clean the text literals ("...") of a formula; the rest of the formula is not changed."""
    return re.sub(r'"((?:[^"]|"")*)"', lambda m: '"' + clean_text(m.group(1)) + '"', f)


def clean_workbook(wb: Any) -> int:
    n = 0
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                v = c.value
                if not isinstance(v, str) or not v:
                    continue
                nv = _clean_formula(v) if v.startswith("=") else clean_text(v)
                if nv != v:
                    c.value = nv
                    n += 1
    return n
