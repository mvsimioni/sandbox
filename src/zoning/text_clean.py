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


_HISTORY = re.compile(r"\s*Earlier class\b.*?\b(?:is replaced|is withdrawn|was replaced|was withdrawn)\.", re.S)
_REGION_ID = re.compile(r"\b((?:open |closed )?(?:IPAS )?region) R\d{3}\b")
_REGION_PAREN = re.compile(r"\s*\(R\d{3}\)")
_REGION_BARE = re.compile(r"\bR\d{3}\b")
_WORDBOX = re.compile(r"\s*\(page view, word box x [\d.]+, y [\d.]+ pt\)")
CATH_DRAWING = re.compile(r"\bAHU-B-07-(04CATH-0[12])\b")


def clean_note(text: str, cls: str = "", grid: str = "") -> str:
    """Issued wording of a room note. ``cls`` is the class of the room (to name a tag variant), ``grid`` its plan grid position (to replace region numbers)."""
    t = (text or "").strip()
    if not t:
        return ""
    t = _HISTORY.sub("", t)
    t = re.sub(r"\bConflict logged\.", "The documents give different values. The values and documents are stated in this note and in the Sources column.", t)
    t = re.sub(r"the tags differ \(see tag variants\)", "the tags differ (tag of this room: %s)" % (cls or "see System / class"), t)
    where = ("at grid " + grid) if grid else ""
    t = _REGION_ID.sub(lambda m: (m.group(1) + " " + where).strip(), t)
    t = _REGION_PAREN.sub((" (grid %s)" % grid) if grid else "", t)
    t = _REGION_BARE.sub((" at grid " + grid) if grid else "the open region", t)
    t = _WORDBOX.sub((" (grid position %s)" % grid) if grid else "", t)
    t = t.replace("Graphic share", "Share of the room area in the booklet zone")
    t = t.replace("graphic share", "share of the room area in the booklet zone")
    t = re.sub(r"\s*(?:Tag form|Booklet tag box form|Conflict|See conflict[^.]*?)\s*:?\s*CF-\d{4}[^.]*\.", "", t)
    t = _CF_SENT.sub("", t) if "CF-" in t else t
    t = _CF_TAIL.sub("", t)
    t = _DEC.sub("", t)
    for rx, rep in _REPL:
        t = rx.sub(rep, t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t


_OWN_UNIT = re.compile(r"-\d\d(?:ASW|SLEEP)-")


def clean_dept(text: str, cls: str = "") -> str:
    t = _DEC.sub("", text or "").strip()
    t = re.sub(r"\s{2,}", " ", t)
    if cls and _OWN_UNIT.search(cls) and "DSOT" in t:      # ward and sleep laboratory rooms have their own unit; the theatre department is not theirs
        return "no information (ward or sleep laboratory room on its own unit)"
    return t


def split_citations(sources: str, grid: str = "") -> List[str]:
    out = []
    for part in re.split(r"\s*;\s+(?=[A-Z])", sources or ""):
        p = clean_note(part, grid=grid).strip(" ;|")
        if re.search(r"IP-BH-(?:07141|16041)", p):          # these drawings print the cath lab units with the plant level 04: quote the tag as printed
            p = CATH_DRAWING.sub(r"AHU-B-04-\1", p)
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
