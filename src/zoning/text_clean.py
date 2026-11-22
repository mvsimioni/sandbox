"""Plain-language text for issued zoning deliverables (PDF annotations, legend, index, zone schedule).

Issued text cites documents by number, issue, sheet and page only.  Internal conflict ids, decision ids, file names and process words are removed here,
so that the working tables keep their full wording and the issued files do not carry it.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, List

_CF_SENT = re.compile(r"\s*(?:[A-Z][^.]*?)?\bCF-\d{4}\b[^.]*\.")           # a sentence that names a conflict id: "Conflict CF-0019." "Tag form: CF-0003."
_CF_TAIL = re.compile(r"\s*\(?\bCF-\d{4}\b\)?[.,;]?")
_DEC = re.compile(r"\bD-\d{3}\b:?\s*")
_REPL = [
    (re.compile(r"\bpolygons\b"), "areas"), (re.compile(r"\bPolygons\b"), "Areas"),
    (re.compile(r"\bpolygon\b"), "plan area"), (re.compile(r"\bPolygon\b"), "Plan area"),
    (re.compile(r"\bnormalised\b"), "standard"),
    (re.compile(r"\bregister(s)?\b", re.I), "list"),
]


def clean_note(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    t = re.sub(r"\s*(?:Tag form|Booklet tag box form|Conflict|See conflict[^.]*?)\s*:?\s*CF-\d{4}[^.]*\.", "", t)
    t = _CF_SENT.sub("", t) if "CF-" in t else t
    t = _CF_TAIL.sub("", t)
    t = _DEC.sub("", t)
    for rx, rep in _REPL:
        t = rx.sub(rep, t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t


def clean_dept(text: str) -> str:
    t = _DEC.sub("", text or "").strip()
    return re.sub(r"\s{2,}", " ", t)


def split_citations(sources: str) -> List[str]:
    out = []
    for part in re.split(r"\s*;\s+(?=[A-Z])", sources or ""):
        p = clean_note(part).strip(" ;|")
        if p:
            out.append(p)
    return out


def top_citations(source_strings: Iterable[str], n: int = 4, max_len: int = 170) -> List[str]:
    """Most frequent document citations over the rooms of a zone, each cut at a clause boundary when long."""
    c: Counter = Counter()
    for s in source_strings:
        for p in split_citations(s):
            if "tag text" in p or "room tag text" in p:      # plan tag text differs per room: cite the sheet only
                p = re.split(r",\s*(?:room )?tag text", p)[0]
            c[p[:max_len].rstrip(" ,(")] += 1
    return [k for k, _ in c.most_common(n)]
