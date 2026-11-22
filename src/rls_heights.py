"""Ceiling heights from the Room Layout Sheets (RLS), decision D-019.

Only the ceiling height is read from each RLS PDF (printed on the room views
as ``CH=2700`` or a variant).  Everything else on the sheet stays unprocessed
(D-008).  Text layer only, no model reading, no network.

CLI::

    python3 -m src.rls_heights run [--limit N] [--dept 04,11] [--workers 3]
    python3 -m src.rls_heights build        # rebuild CSV and report from the cache

Two stages:

* ``extract`` reads each PDF once with PyMuPDF and stores, per file, every
  height candidate (text, page, bbox, font size) plus the title-block facts
  in a JSONL cache (``state/rls_heights_cache.jsonl``, keyed by sha256).
* ``build`` turns the cache into ``out/registers/rls_ceiling_heights.csv`` and
  ``out/qa/rls_heights_report.md``.  Nothing is guessed: a missing value stays
  empty and a conflict is kept and flagged, never resolved silently.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

LEDGER = "state/pull_ledger.jsonl"
CACHE = "state/rls_heights_cache.jsonl"
CSV_OUT = "out/registers/rls_ceiling_heights.csv"
REPORT_OUT = "out/qa/rls_heights_report.md"

# IP-block departments of storeys LB3-L07 are processed first (D-019).
PRIORITY_DEPTS = ["04", "11", "22", "23", "26", "28", "29", "30", "31", "34", "35",
                  "36", "37", "39", "40", "41", "42", "43", "44", "45", "46", "47", "50"]

FIELDS = [
    "rel_path", "dept_no", "rls_number", "sheet_no", "multi_room_flag", "rooms_on_sheet", "variant",
    "room_type_code", "room_name",
    "room_name_source", "heights_mm_as_printed", "height_count_by_value",
    "governing_height_mm", "lowest_height_mm", "governing_basis", "main_room_note", "conflict_flag",
    "page", "bbox", "height_text_as_printed", "issue", "revision", "issue_date",
    "revision_history", "governing_issue", "governing_issue_group", "governing_issue_note", "drawing_no_title_block",
    "method", "confidence", "source_sha256",
]

# ---------------------------------------------------------------- patterns --
_NUM = r"(\d{1,2}[.,]\d{1,3}|\d{4})"
# (kind, regex).  Applied to the upper-cased text of one text line.
HEIGHT_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("CH", re.compile(r"(?<![A-Z])C\s?\.?\s?H\s?\.?\s*[=:\-]?\s*(?P<v>[0-9][0-9.,\s/&\-–TOtoAND]*[0-9])\s*(?P<u>MM|M)?(?![A-Z0-9])")),
    ("CLG_HT", re.compile(r"(?:CLG|CEILING|CEIL)\.?\s*(?:HT|HEIGHT|H)\.?\s*[=:@\-]?\s*(?P<v>[0-9][0-9.,\s/&\-–]*[0-9])\s*(?P<u>MM|M)?")),
    ("FCL", re.compile(r"(?<![A-Z])F\.?C\.?L\.?\s*[=:@+\-]?\s*(?P<v>[0-9][0-9.,\s/&\-–]*[0-9])\s*(?P<u>MM|M)?")),
]
# Broad "mention" test, used to record lines that talk about a ceiling height
# without a parsable number (reported as unparsed variants, never guessed).
MENTION = re.compile(r"(?<![A-Z])(C\s?\.?\s?H\s?\.?\s*[=:]|CLG\.?\s*H|CEILING\s+H|F\.?C\.?L\b)", re.I)


_REAL_MENTION = re.compile(r"(?<![A-Z])(C\s?\.?\s?H\s?\.?\s*[=:]|CLG\.?\s*(?:HT|HEIGHT)\b|CEILING\s+(?:HT|HEIGHT)\b|F\.?C\.?L\b)", re.I)


def _to_mm(tok: str, unit: Optional[str]) -> Optional[int]:
    t = tok.strip().replace(",", ".")
    if "." in t:
        try:
            f = float(t)
        except ValueError:
            return None
        return int(round(f * 1000)) if f < 20 else None
    if not t.isdigit():
        return None
    n = int(t)
    if unit and unit.upper() == "M" and n < 20:
        n *= 1000
    return n if 1800 <= n <= 12000 else None


def parse_values(raw: str) -> List[int]:
    """Parse the number part of a match (single, range or list) to mm values."""
    parts = re.split(r"\s*(?:/|&|-|–|TO|AND|,\s)\s*", raw.strip(), flags=re.I)
    vals = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        m = re.fullmatch(r"\d{1,2}[.,]\d{1,3}|\d{3,5}", p)
        if not m:
            continue
        v = _to_mm(p, None)
        if v is not None:
            vals.append(v)
    return vals


def find_heights(line: str) -> List[Dict[str, Any]]:
    """Return height hits in one line of text: kind, text, values_mm, range."""
    up = line.upper()
    hits = []
    for kind, rx in HEIGHT_PATTERNS:
        for m in rx.finditer(up):
            raw = m.group("v")
            unit = m.group("u")
            if unit == "M" and re.fullmatch(r"\d{1,2}", raw.strip()):
                vals = [int(raw) * 1000]
            else:
                vals = parse_values(raw)
                if unit == "M":
                    vals = [v * 1000 if v < 20 else v for v in vals]
            if not vals:
                continue
            is_range = bool(re.search(r"\d\s*(?:-|–|TO)\s*\d", raw, re.I)) and len(vals) > 1
            hits.append({"kind": kind, "text": m.group(0).strip(), "values_mm": vals, "range": is_range})
    return hits


# ------------------------------------------------------------ file-name parse
_VAR = r"P\d+C\d+"
_MARK_SHEET = re.compile(r"(?P<pre>(?:AR|ID|MP)[-_ ]RLS[-_ ](?:32(?:\s*[-_]\s*|\s+))?)", re.I)
_MARK_PLAIN = re.compile(r"^[\d\s\-_.]*(?P<pre>RLS[-_ ](?:32[-_ ])?)", re.I)
_NUM_FIRST = re.compile(r"^(?P<num>\d{3}[A-Z]?)(?=[_\- ,])", re.I)
_NUM_MORE = re.compile(r"(?<![A-Za-z0-9\-])(?P<num>\d{3}[A-Z]?)(?=_(?:[A-Z]{2,}|%s))" % _VAR)
_CODE_AFTER = re.compile(r"^_(?P<code>(?!%s\b)[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*)" % _VAR, re.I)
_VAR_RX = re.compile(r"(?<![A-Za-z0-9])(?P<var>%s)(?:-\d{1,2}(?=-\d{3}))?(?![A-Za-z0-9])" % _VAR, re.I)
_SHEET_RX = re.compile(r"[-_ ](?P<seq>\d{3}[A-Z]?)(?=[\s.\-_]|$)", re.I)
_MP_CODE_SEQ = re.compile(r"^(?P<code>[A-Z0-9][A-Z0-9 \-]*?)(?:_(?P<var>(?:P\d|\d{2})C\d+)(?:-\d{1,2}(?=-\d{3}))?)?[-_ ](?P<seq>\d{3}[A-Z]?)(?=[\s.\-_]|$)", re.I)
_MP_CODE_ONLY = re.compile(r"^(?P<code>[A-Z0-9]+(?:-[A-Z0-9]+)*)(?=\s+-\s|\s*$)", re.I)


def parse_filename(name: str) -> Dict[str, Any]:
    """Identifiers printed in the file name.

    ``rls_number`` is only set when a 3-digit RLS number is printed in the name.
    The trailing ``-001`` of MP sheets (``MP-RLS-32-1BR-IC-B-001``) is a drawing
    sheet sequence, not an RLS number: it goes to ``sheet_no`` (RLS number blank).
    Sheets that name several rooms keep the first in ``rls_number`` and list all
    in ``rooms_on_sheet`` with ``multi_room`` set.
    """
    stem = re.sub(r"\.pdf$", "", name, flags=re.I)
    out: Dict[str, Any] = {"rls_number": "", "variant": "", "room_type_code": "", "name_from_file": "",
                           "sheet_no": "", "rooms_on_sheet": "", "multi_room": False}
    m = _MARK_SHEET.search(stem) or _MARK_PLAIN.match(stem)
    if not m:
        out["name_from_file"] = stem
        mp = re.match(r"^(?P<num>\d{3}[A-Z]?)_(?P<var>%s)_(?P<nm>.+)$" % _VAR, stem, re.I)
        if mp:  # "113_P1C6_Landscape Store_RLS (OP L12)": number first, RLS later
            out.update(rls_number=mp.group("num").upper(), variant=mp.group("var").upper(),
                       name_from_file=mp.group("nm").strip())
            return out
        mv = _VAR_RX.search(stem)
        if mv:
            out["variant"] = mv.group("var").upper()
        return out
    pre = m.group("pre").upper()
    is_mp = pre.startswith("MP")
    tail = stem[m.end("pre"):]
    first = _NUM_FIRST.match(tail)
    nums: List[Tuple[int, int, str]] = []   # (start, end, number)
    if first and (not is_mp or re.match(r"\d{3}[A-Z]?_", tail, re.I)):
        nums.append((first.start("num"), first.end("num"), first.group("num").upper()))
        for mm in _NUM_MORE.finditer(tail):
            if mm.start("num") > first.end("num"):
                nums.append((mm.start("num"), mm.end("num"), mm.group("num").upper()))
    elif is_mp:
        # MP-RLS-32-<ROOM-TYPE-CODE>-<sheet seq> - name: no RLS number is printed
        mc = _MP_CODE_SEQ.match(tail)
        if mc:
            out["room_type_code"] = re.sub(r"\s+", " ", mc.group("code")).upper()
            out["sheet_no"] = mc.group("seq").upper()
            out["name_from_file"] = re.sub(r"\s+", " ", tail[mc.end():]).strip(" _-")
            if mc.group("var"):
                out["variant"] = mc.group("var").upper()
        else:
            mo = _MP_CODE_ONLY.match(tail)
            if mo:
                out["room_type_code"] = mo.group("code").upper()
                out["name_from_file"] = re.sub(r"\s+", " ", tail[mo.end():]).strip(" _-")
            else:
                out["name_from_file"] = stem
        mv = _VAR_RX.search(tail)
        if mv and not out["variant"]:
            out["variant"] = mv.group("var").upper()
        return out
    # numbered forms (RLS_074_..., AR-RLS-32-037_..., MP-RLS-32-042_CODE_...)
    blank = list(tail)
    for s0, e0, _ in nums:
        for i in range(s0, e0):
            blank[i] = "#"
    sm = _SHEET_RX.search("".join(blank))
    cut = sm.start() if sm else len(tail)
    nums = [n for n in nums if n[0] < cut]
    if sm:
        out["sheet_no"] = sm.group("seq").upper()
    variants = list(dict.fromkeys(v.group("var").upper() for v in _VAR_RX.finditer(tail)))
    codes: List[str] = []
    for s0, e0, _ in nums:
        mc = _CODE_AFTER.match(tail[e0:])
        codes.append(mc.group("code").upper() if mc else "")
    seen: Dict[str, str] = {}
    for (s0, e0, n), c in zip(nums, codes):
        seen.setdefault(n, c)
    out["rls_number"] = nums[0][2] if nums else ""
    # AR/ID sheets carry the type code in the title block, not in the file name
    if codes and codes[0] and not pre.startswith(("AR", "ID")) and codes[0] != "S":
        out["room_type_code"] = codes[0]
    out["variant"] = variants[0] if variants else ""
    out["multi_room"] = len(seen) > 1 or len(variants) > 1
    if out["multi_room"]:
        out["rooms_on_sheet"] = "; ".join("%s%s" % (n, ("/" + c) if c else "") for n, c in seen.items()) + (
            " | variants " + " ".join(variants) if variants else "")
    end = 0
    if sm:
        end = sm.end("seq")
    else:
        for v in _VAR_RX.finditer(tail):
            end = max(end, v.end())
        if not end and nums:
            end = nums[0][1] + (len(codes[0]) + 1 if codes[0] else 0)
    nm = re.sub(r"\s+", " ", tail[end:]).strip(" _-.,")
    if not sm:
        nm = re.sub(r"^S[_\-]\s*", "", nm)   # "<code>_S_<name>": S = sheet marker, not part of the name
    out["name_from_file"] = nm
    return out


# ---------------------------------------------------------------- title block
_DATE_RX = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})$")
_DATE_TXT = re.compile(r"^(\d{1,2})\s*([A-Z]{3,9})\s*(\d{2,4})$", re.I)
_MONTHS = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
_ANY_DATE = re.compile(r"\b\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}\b|\b\d{1,2}\s*(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?\s*\d{2,4}\b", re.I)
_DWG_RX = re.compile(r"\b((?:MP|AR|ID)[-_]RLS[-_ ]?32[-_][A-Z0-9][A-Z0-9_\-]*)", re.I)


def parse_date(s: str) -> Optional[str]:
    s = s.strip()
    m = _DATE_RX.match(s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None
    m = _DATE_TXT.match(s)
    if m and m.group(2)[:3].upper() in _MONTHS:
        y = int(m.group(3))
        y = y + 2000 if y < 100 else y
        try:
            return date(y, _MONTHS[m.group(2)[:3].upper()], int(m.group(1))).isoformat()
        except ValueError:
            return None
    return None


def revision_rows(words: Sequence[Tuple], page_w: float) -> List[Dict[str, Any]]:
    """Read the DATE / REV / DESCRIPTION table from word boxes.

    ``words`` are PyMuPDF word tuples (x0,y0,x1,y1,text,...).  Every header
    triple found is read; rows are grouped by y below the header.
    """
    heads = []
    for w in words:
        if w[4] != "DATE":
            continue
        yc = (w[1] + w[3]) / 2
        rev = [v for v in words if v[4] == "REV" and abs((v[1] + v[3]) / 2 - yc) < 4 and v[0] > w[0]]
        desc = [v for v in words if v[4] == "DESCRIPTION" and abs((v[1] + v[3]) / 2 - yc) < 4 and v[0] > w[0]]
        if rev and desc:
            heads.append((w, min(rev, key=lambda v: v[0]), min(desc, key=lambda v: v[0])))
    rows_all: List[Dict[str, Any]] = []
    for dw, rw, sw in heads:
        x_date0 = dw[0] - 40
        x_rev0 = rw[0] - 8
        x_desc0 = sw[0] - 8
        x_end = min(page_w, sw[0] + 330)
        ymax = dw[3] + 140
        body = [w for w in words if (w[1] + w[3]) / 2 > (dw[1] + dw[3]) / 2 + 4 and w[1] < ymax and w[0] >= x_date0 and w[2] <= x_end + 1]
        if not body:
            continue
        body.sort(key=lambda w: ((w[1] + w[3]) / 2, w[0]))
        lines: List[List[Tuple]] = []
        for w in body:
            yc = (w[1] + w[3]) / 2
            if lines and abs((lines[-1][0][1] + lines[-1][0][3]) / 2 - yc) < 3.5:
                lines[-1].append(w)
            else:
                lines.append([w])
        for ln in lines:
            txt = " ".join(w[4] for w in sorted(ln, key=lambda w: w[0])).strip()
            m = _ANY_DATE.search(txt)
            iso = parse_date(m.group(0)) if m else None
            if not m or not iso:
                if rows_all and rows_all[-1].get("_table") == id(dw) and txt:
                    rows_all[-1]["description"] = (rows_all[-1]["description"] + " " + txt).strip()
                continue
            rest = (txt[:m.start()] + " " + txt[m.end():]).strip(" -:,")
            rest = re.sub(r"\s+", " ", rest)
            rest = re.split(r"\bNOS\.", rest)[0].strip()  # the quantity column sits to the right of the table
            rev = ""
            mr = re.match(r"^([A-Z]?\d{1,2}|[A-Z])\s+(.*)$", rest)
            if mr:
                rev, rest = mr.group(1), mr.group(2)
            rows_all.append({"_table": id(dw), "date_text": m.group(0), "date": iso, "rev": rev,
                             "description": rest, "y": round(ln[0][1], 1)})
    seen = set()
    uniq = []
    for r in rows_all:
        r.pop("_table", None)
        k = (r["date"], r["rev"], r["description"])
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return uniq


def title_block(page_text_lines: List[str]) -> Dict[str, str]:
    """Drawing number and room name from the title-block lines of a page."""
    out = {"drawing_no": "", "room_name": "", "code": "", "month": ""}
    idx = None
    for ln in page_text_lines:
        mm = re.match(r"^\s*(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?\s+(20\d\d)\s*$", ln, re.I)
        if mm:
            out["month"] = "%04d-%02d" % (int(mm.group(2)), _MONTHS[mm.group(1).upper()])
            break
    for ln in page_text_lines:
        mc = re.match(r"^\s*(\d{3})_([A-Z0-9\-]+)_(P\d+C\d+)\s*$", ln, re.I)
        if mc:
            out["code"] = mc.group(2).upper()
            break
    for i, ln in enumerate(page_text_lines):
        m = _DWG_RX.search(ln)
        if m and len(ln.strip()) < 80:
            out["drawing_no"] = m.group(1)
            idx = i
            break
    for i, ln in enumerate(page_text_lines):
        if ln.strip().upper() == "ROOM LAYOUT SHEET" and i + 1 < len(page_text_lines):
            nxt = page_text_lines[i + 1].strip()
            if nxt.upper().startswith("DEPT") and i + 2 < len(page_text_lines):
                nxt = page_text_lines[i + 2].strip()
            out["room_name"] = nxt
            return out
    if idx is not None and idx >= 2:
        # MP sheets: ... DEPT: x / <room name> / <code> / <drawing no>
        cand = page_text_lines[idx - 2].strip()
        if cand and not cand.upper().startswith(("DEPT", "RLS", "CHECKER", "APPROVER")):
            out["room_name"] = cand
    return out


# ------------------------------------------------------------------ extraction
def extract_pdf(path: str) -> Dict[str, Any]:
    """Read one PDF.  Returns a JSON-able record; never raises."""
    import fitz  # PyMuPDF

    fitz.TOOLS.mupdf_display_errors(False)
    rec: Dict[str, Any] = {"pages": 0, "text_chars": 0, "candidates": [], "mentions": [],
                           "title": {}, "revisions": [], "error": ""}
    try:
        doc = fitz.open(path)
    except Exception as exc:  # noqa: BLE001
        rec["error"] = "cannot open: %s" % exc
        return rec
    try:
        rec["pages"] = len(doc)
        if doc.needs_pass:
            rec["error"] = "encrypted"
            return rec
        for pno, page in enumerate(doc, start=1):
            d = page.get_text("dict")
            lines_txt: List[str] = []
            for blk in d.get("blocks", []):
                for ln in blk.get("lines", []):
                    spans = ln.get("spans", [])
                    txt = "".join(s["text"] for s in spans).strip()
                    if not txt:
                        continue
                    lines_txt.append(txt)
                    rec["text_chars"] += len(txt)
                    size = round(max(s["size"] for s in spans), 1)
                    bbox = [round(v, 1) for v in ln["bbox"]]
                    hits = find_heights(txt)
                    for h in hits:
                        h.update({"page": pno, "bbox": bbox, "size": size, "line": txt[:120]})
                        rec["candidates"].append(h)
                    if not hits and MENTION.search(txt):
                        rec["mentions"].append({"page": pno, "bbox": bbox, "line": txt[:120]})
            words = page.get_text("words")
            # "CH=" and its number sometimes sit in separate words
            for w in words:
                if re.fullmatch(r"C\.?H\.?\s?[=:]", w[4].upper()):
                    yc = (w[1] + w[3]) / 2
                    nxt = [v for v in words if v[0] >= w[2] - 1 and v[0] - w[2] < 40
                           and abs((v[1] + v[3]) / 2 - yc) < 5 and re.fullmatch(r"\d{4}", v[4])]
                    if nxt:
                        v = min(nxt, key=lambda v: v[0])
                        bb = [round(min(w[0], v[0]), 1), round(min(w[1], v[1]), 1),
                              round(max(w[2], v[2]), 1), round(max(w[3], v[3]), 1)]
                        if not any(abs(c["bbox"][0] - bb[0]) < 3 and abs(c["bbox"][1] - bb[1]) < 3
                                   for c in rec["candidates"] if c["page"] == pno):
                            rec["candidates"].append({"kind": "CH", "text": w[4] + v[4], "values_mm": [int(v[4])],
                                                      "range": False, "page": pno, "bbox": bb, "size": 0,
                                                      "line": w[4] + " " + v[4]})
            tb = title_block(lines_txt)
            if tb["drawing_no"] and not rec["title"].get("drawing_no"):
                rec["title"] = dict(tb, page=pno)
            elif rec["title"] and tb["month"] and not rec["title"].get("month"):
                rec["title"]["month"] = tb["month"]
            elif not rec["title"] and tb["room_name"]:
                rec["title"] = dict(tb, page=pno)
            for r in revision_rows(words, page.rect.width):
                r["page"] = pno
                rec["revisions"].append(r)
    except Exception as exc:  # noqa: BLE001
        rec["error"] = "read error: %s" % exc
    finally:
        doc.close()
    return rec


def _worker(args: Tuple[str, str]) -> Tuple[str, Dict[str, Any]]:
    key, path = args
    return key, extract_pdf(path)


# ------------------------------------------------------------------- ledger
def root() -> Path:
    here = Path(__file__).resolve().parent.parent
    return Path(os.environ.get("AIGH_ROOT", here))


def dept_of(rel_path: str) -> str:
    m = re.match(r"DD Sign-OFF/(?:__)?(\d{2})\s*-", rel_path)
    return m.group(1) if m else ""


def ledger_rls_rows() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(pdf rows, non-pdf rows) for DD Sign-OFF files with RLS in the name."""
    pdfs, other = [], []
    seen = set()
    for line in open(root() / LEDGER, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        rp = r["rel_path"]
        if not rp.startswith("DD Sign-OFF/") or "RLS" not in os.path.basename(rp):
            continue
        if rp in seen:
            continue
        seen.add(rp)
        (pdfs if rp.lower().endswith(".pdf") else other).append(r)
    return pdfs, other


def order_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    pri = {d: i for i, d in enumerate(PRIORITY_DEPTS)}
    return sorted(rows, key=lambda r: (pri.get(dept_of(r["rel_path"]), 999), dept_of(r["rel_path"]), r["rel_path"]))


def load_cache() -> Dict[str, Dict[str, Any]]:
    p = root() / CACHE
    out: Dict[str, Dict[str, Any]] = {}
    if p.exists():
        for line in open(p, encoding="utf-8"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            out[r["key"]] = r
    return out


def extract_all(rows: List[Dict[str, Any]], workers: int = 3, limit: Optional[int] = None,
                depts: Optional[Sequence[str]] = None, progress: bool = True) -> int:
    from multiprocessing import Pool

    cache = load_cache()
    todo = []
    for r in order_rows(rows):
        if depts and dept_of(r["rel_path"]) not in depts:
            continue
        key = r.get("sha256") or r["rel_path"]
        if key in cache or key in {k for k, _ in todo}:
            continue  # same content (sha256) is read once and shared by its copies
        todo.append((key, r))
    if limit:
        todo = todo[:limit]
    if not todo:
        return 0
    cache_path = root() / CACHE
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    by_key = {k: r for k, r in todo}
    n = 0
    jobs = [(k, str(root() / r["local_path"])) for k, r in todo]
    with open(cache_path, "a", encoding="utf-8") as fh, Pool(workers) as pool:
        for key, rec in pool.imap(_worker, jobs, chunksize=4):
            r = by_key[key]
            rec.update({"key": key, "rel_path": r["rel_path"], "sha256": r.get("sha256", ""),
                        "bytes": r.get("bytes"), "dept_no": dept_of(r["rel_path"])})
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            n += 1
            if progress and n % 100 == 0:
                print("extracted %d/%d" % (n, len(todo)), file=sys.stderr, flush=True)
    return n


# --------------------------------------------------------------------- build
def _rev_sort_key(r: Dict[str, Any]) -> Tuple:
    return (r.get("date") or "", r.get("page", 0), -r.get("y", 0))


def _issue_group(row: Dict[str, Any]) -> str:
    """Group key for governing_issue: room type code (or RLS number when no code) plus variant."""
    base = row["room_type_code"] or ("RLS-%s" % row["rls_number"] if row["rls_number"] else "")
    if not base:
        return ""
    return "%s|%s" % (base, row["variant"] or "(no variant)")


def build_rows(rows: List[Dict[str, Any]], cache: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for r in order_rows(rows):
        key = r.get("sha256") or r["rel_path"]
        rec = cache.get(key)
        if not rec:
            continue
        rp = r["rel_path"]
        fn = parse_filename(os.path.basename(rp))
        tb = rec.get("title") or {}
        code = fn["room_type_code"]
        num = fn["rls_number"]
        variant = fn["variant"]
        dwg = tb.get("drawing_no", "")
        method_bits = []
        # AR/ID sheets: the type code is in the title block ("037_MPR_P1C6")
        if not code and tb.get("code"):
            code = tb["code"]
        # room name: title block first, else file name
        if tb.get("room_name"):
            name, name_src = tb["room_name"], "title block"
        elif fn["name_from_file"]:
            name, name_src = fn["name_from_file"], "file name"
        else:
            name, name_src = "", ""
        cands = rec.get("candidates", [])
        printed = []
        counts: Counter = Counter()
        for c in cands:
            for v in c["values_mm"]:
                counts[v] += 1
            printed.append(c["text"])
        kinds = sorted({c["kind"] for c in cands})
        vals = sorted(counts)
        # revision / issue
        revs = [x for x in rec.get("revisions", [])]
        revs_sorted = sorted(revs, key=_rev_sort_key)
        latest = revs_sorted[-1] if revs_sorted else {}
        if not latest and tb.get("month"):
            latest = {"date": tb["month"], "rev": "", "description": "(title block month only; revision table empty)"}
            revs_sorted = [dict(latest, date_text=tb["month"])]
        hist = "; ".join("%s|%s|%s" % (x.get("date_text", ""), x.get("rev", ""), x.get("description", "")) for x in revs_sorted)
        row: Dict[str, Any] = {
            "rel_path": rp, "dept_no": dept_of(rp), "rls_number": num,
            "sheet_no": fn["sheet_no"],
            "multi_room_flag": ("several rooms/variants named on this sheet (%s); only the first is used for rls_number/variant/room_type_code"
                                % fn["rooms_on_sheet"]) if fn["multi_room"] else "",
            "rooms_on_sheet": fn["rooms_on_sheet"], "variant": variant, "room_type_code": code, "room_name": name,
            "room_name_source": name_src,
            "heights_mm_as_printed": "; ".join(printed),
            "height_count_by_value": "; ".join("%d x%d" % (v, counts[v]) for v in sorted(counts)),
            "governing_height_mm": "", "lowest_height_mm": "", "governing_basis": "", "main_room_note": "",
            "conflict_flag": "", "page": "", "bbox": "", "height_text_as_printed": "",
            "issue": latest.get("description", ""), "revision": latest.get("rev", ""),
            "issue_date": latest.get("date") or "", "revision_history": hist,
            "governing_issue": "", "governing_issue_group": "", "governing_issue_note": "", "drawing_no_title_block": dwg,
            "method": "", "confidence": "", "source_sha256": rec.get("sha256", ""),
        }
        if cands:
            top = max(counts.values())
            modal = sorted(v for v in counts if counts[v] == top)
            # governing = most frequent printed value; ties and several distinct values -> lowest, flagged
            if len(vals) == 1:
                gov, basis, flag, conf = vals[0], "single value printed", "", "high"
            elif len(modal) == 1:
                gov, basis, flag, conf = modal[0], "most frequent of %d distinct values" % len(vals), "several_heights", "medium"
            else:
                gov, basis, flag, conf = modal[0], "tie between %s; lowest taken" % "/".join(map(str, modal)), "several_heights_tie", "low"
            if len(vals) > 1:
                flag = flag or "several_heights"
            if any(c.get("range") for c in cands):
                flag = (flag + ";" if flag else "") + "range_printed"
                conf = "low" if conf != "low" else conf
            if kinds != ["CH"]:
                flag = (flag + ";" if flag else "") + "variant_label_" + "+".join(k for k in kinds if k != "CH")
                conf = "medium" if conf == "high" else conf
            row["governing_height_mm"] = gov
            row["lowest_height_mm"] = vals[0]
            row["governing_basis"] = basis
            row["conflict_flag"] = flag
            row["main_room_note"] = ("which height applies to the main room is not marked on the sheet; "
                                     "the governing value is the most frequent printed value" if len(vals) > 1 else
                                     "single height printed; assumed to be the room's")
            # provenance of the governing value: first occurrence of it
            first = next(c for c in cands if gov in c["values_mm"])
            row["page"] = first["page"]
            row["bbox"] = ",".join(str(v) for v in first["bbox"])
            row["height_text_as_printed"] = first["text"]
            row["method"] = "text layer (PyMuPDF get_text dict lines/words), pattern " + "+".join(kinds)
            row["confidence"] = conf
        else:
            if rec.get("error"):
                row["method"] = "no read: " + rec["error"]
            elif rec.get("text_chars", 0) < 50:
                row["method"] = "text layer empty (%d chars); not read, no model reading" % rec.get("text_chars", 0)
            elif [m for m in rec.get("mentions", []) if _REAL_MENTION.search(m["line"])]:
                ms = [m for m in rec["mentions"] if _REAL_MENTION.search(m["line"])]
                row["method"] = "text layer: ceiling-height wording without a parsable number"
                row["height_text_as_printed"] = "; ".join(m["line"] for m in ms[:3])
            else:
                row["method"] = "text layer read; no ceiling height printed"
            row["confidence"] = "n/a"
        out.append(row)
    # governing issue: latest per (room type code, variant) among files that have a height.
    # Interpretation of "latest per room type" (stated in the report): the group key is the room type
    # code plus the geometric variant (PnCn), across all departments.  Files with neither a room type
    # code nor an RLS number cannot be grouped and say so.
    by_code: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in out:
        if row["governing_height_mm"] == "":
            continue
        key = _issue_group(row)
        if not key:
            row["governing_issue_note"] = ("not grouped: no room type code and no RLS number readable from the file name or "
                                          "title block (cluster / key / non-standard sheet); latest issue not determinable")
            continue
        row["governing_issue_group"] = key
        by_code[key].append(row)
    for code, grp in by_code.items():
        dated = [g for g in grp if g["issue_date"]]
        if not dated:
            for g in grp:
                g["governing_issue_note"] = "no issue date readable; latest not determinable"
            continue
        month_level = any(len(g["issue_date"]) == 7 for g in dated)
        kfun = (lambda g: g["issue_date"][:7]) if month_level else (lambda g: g["issue_date"])
        latest_date = max(kfun(g) for g in dated)
        top = [g for g in dated if kfun(g) == latest_date]
        heights = {g["governing_height_mm"] for g in grp}
        for g in grp:
            if g in top:
                g["governing_issue"] = "Y"
                if len(top) > 1:
                    g["governing_issue_note"] = "%d files share the latest date %s" % (len(top), latest_date)
                    if len({t["governing_height_mm"] for t in top}) > 1:
                        g["governing_issue_note"] += "; heights differ between them (unresolved)"
            else:
                g["governing_issue"] = "N"
        for g in [g for g in grp if not g["issue_date"]]:
            g["governing_issue"] = ""
            g["governing_issue_note"] = "no issue date readable"
        if len(heights) > 1 and not any(g["governing_issue_note"].endswith("(unresolved)") for g in top):
            for g in top:
                g["governing_issue_note"] = (g["governing_issue_note"] + "; " if g["governing_issue_note"] else "") + \
                    "other files of this group carry different heights"
    return out


def write_csv(rows: List[Dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    os.replace(tmp, path)


def write_report(rows: List[Dict[str, Any]], all_pdf: List[Dict[str, Any]], other: List[Dict[str, Any]],
                 cache: Dict[str, Dict[str, Any]], path: Path) -> None:
    n_total = len(all_pdf)
    read = rows
    with_h = [r for r in read if r["governing_height_mm"] != ""]
    without = [r for r in read if r["governing_height_mm"] == ""]
    conflicts = [r for r in with_h if r["conflict_flag"]]
    by_dept: Dict[str, Counter] = defaultdict(Counter)
    for r in all_pdf:
        by_dept[dept_of(r["rel_path"])]["on_disk"] += 1
    for r in read:
        d = by_dept[r["dept_no"]]
        d["read"] += 1
        d["with_height"] += 1 if r["governing_height_mm"] != "" else 0
        d["without_height"] += 1 if r["governing_height_mm"] == "" else 0
        d["conflict"] += 1 if r["conflict_flag"] else 0
    reasons = Counter(r["method"].split(":")[0] if r["method"].startswith("no read") else r["method"] for r in without)
    L = ["# RLS ceiling heights report (D-019)", "",
         "Generated by `python3 -m src.rls_heights run`. Source: RLS PDFs under `DD Sign-OFF/` listed in `state/pull_ledger.jsonl`.",
         "Only the ceiling height is read (text layer, no model reading). Register: `out/registers/rls_ceiling_heights.csv`.", "",
         "## Counts", "",
         "- RLS PDFs on disk (ledger rows under DD Sign-OFF/ with RLS in the file name): %d" % n_total,
         "- Non-PDF RLS-named files, not read (emails, notes, workbooks): %d" % len(other),
         "- PDFs read so far: %d" % len(read),
         "- PDFs not yet read: %d" % (n_total - len(read)),
         "- Read with a height: %d" % len(with_h),
         "- Read without a height: %d" % len(without),
         "- With several heights or another flag (conflicting / range / variant label): %d" % len(conflicts), ""]
    L += ["## Counts per department", "", "| dept | on disk | read | with height | without height | flagged |", "|---|---|---|---|---|---|"]
    for d in sorted(set(by_dept) | set(PRIORITY_DEPTS), key=lambda d: (d not in PRIORITY_DEPTS, d)):
        c = by_dept[d]
        note = " (priority, 0 RLS PDFs in the ledger)" if d in PRIORITY_DEPTS and not c["on_disk"] else (" (priority)" if d in PRIORITY_DEPTS else "")
        L.append("| %s%s | %d | %d | %d | %d | %d |" % (d or "?", note, c["on_disk"], c["read"], c["with_height"], c["without_height"], c["conflict"]))
    empty_pri = [d for d in PRIORITY_DEPTS if not by_dept[d]["on_disk"]]
    L += ["", "Departments in the IP-block priority list: %s." % " ".join(PRIORITY_DEPTS),
          "Priority departments with 0 RLS PDFs in `state/pull_ledger.jsonl` (nothing to read, not a read failure): %s." % (" ".join(empty_pri) or "none"), ""]
    multi = [r for r in read if r.get("multi_room_flag")]
    no_num = [r for r in read if not r["rls_number"]]
    sheet_only = [r for r in read if r["sheet_no"] and not r["rls_number"]]
    L += ["## Identifiers (file name)", "",
          "- `rls_number` is filled only where a 3-digit RLS number is printed in the file name (%d of %d rows). %d rows have it blank (unknown)." % (len(read) - len(no_num), len(read), len(no_num)),
          "- MP-format sheets (`MP-RLS-32-<code>-001`) print a room type code and a trailing drawing-sheet sequence, no RLS number (the title blocks checked carry only the drawing number). "
          "That sequence is now in `sheet_no`, never in `rls_number`: %d rows have a `sheet_no` and a blank `rls_number`." % len(sheet_only),
          "- `variant` is the PnCn code in the file name (MP sheets that print `00C0` carry that as printed). Blank where the name has none.",
          "- Sheets naming several rooms or variants (e.g. `009_071`, `005 ... 280`, `025 ... 276`): %d rows. `rls_number`, `variant`, `room_type_code` hold the first only; "
          "every room/variant named is listed in `rooms_on_sheet` and the row carries `multi_room_flag`. The CH height on such a sheet is not attributed to a room; it is read from the sheet as a whole." % len(multi),
          "- Do not join the register to other data on `rls_number` for these rows without checking `multi_room_flag`.", ""]
    L += ["## Files without a height, by reason", ""]
    for k, v in reasons.most_common():
        L.append("- %d: %s" % (v, k))
    L += ["", "Sample (up to 25, spread over departments):", ""]
    seen_d: Counter = Counter()
    n = 0
    for r in without:
        if seen_d[r["dept_no"]] >= 2:
            continue
        seen_d[r["dept_no"]] += 1
        L.append("- `%s` (%s)" % (r["rel_path"], r["method"]))
        n += 1
        if n >= 25:
            break
    L += ["", "## Conflicting or flagged heights", "",
          "Where one file prints several different heights, all are kept in `heights_mm_as_printed`; the governing value is the most frequent one "
          "(lowest if tied) and the row is flagged. Which height belongs to the main room is not marked on the sheets, so every flagged row needs confirming.", ""]
    diff_rule = [r for r in with_h if r["lowest_height_mm"] != r["governing_height_mm"]]
    L += ["**Governing-height rule (interpretation, D-019, to confirm).** The brief says: governing = the most frequent main-room value; if several differ, the lowest, flagged. "
          "This register reads it as: one distinct value -> that value; several distinct values -> the most frequent one, and the lowest only when two or more are tied for most frequent; "
          "all such rows are flagged (`several_heights` / `several_heights_tie`) and the main room is not marked on the sheets. The other reading (always the lowest where values differ) "
          "is not applied but is kept in `lowest_height_mm`; the two readings differ on %d rows (`governing_height_mm` != `lowest_height_mm`). Needs a decision from the owner of D-019." % len(diff_rule), ""]
    for r in conflicts[:60]:
        L.append("- `%s`: %s [%s]" % (r["rel_path"], r["height_count_by_value"], r["conflict_flag"]))
    if len(conflicts) > 60:
        L.append("- ... %d more in the CSV (conflict_flag column)" % (len(conflicts) - 60))
    # same room type, different heights across files
    by_code: Dict[str, Dict[int, List[str]]] = defaultdict(lambda: defaultdict(list))
    for r in with_h:
        if r["governing_issue_group"]:
            by_code[r["governing_issue_group"]][int(r["governing_height_mm"])].append(r["rel_path"])
    multi = {c: h for c, h in by_code.items() if len(h) > 1}
    L += ["", "## Room types whose files disagree", "",
          "%d room-type codes carry different governing heights in different files (left as is, see `governing_issue` for the latest issue):" % len(multi), ""]
    ungrouped = [r for r in with_h if not r["governing_issue_group"]]
    for c in sorted(multi)[:60]:
        L.append("- %s: %s" % (c, ", ".join("%d mm (%d files)" % (h, len(f)) for h, f in sorted(multi[c].items()))))
    L += ["", "## Method and limits", "",
          "- Patterns: `CH=2700`, `CH 2700`, `C.H.=`, `CLG HT`/`CEILING HEIGHT`, `FCL`; ranges and lists keep every number.",
          "- Governing issue (interpretation of 'latest per room type'): the group is room type code + variant (PnCn), across all departments (`governing_issue_group`). "
          "Geometric variants that share a code (e.g. STEQ P1C117 vs P1C83) are separate groups. Where there is no code the group is `RLS-<number>` + variant. Latest = latest issue date in the title block "
          "(month level if any date in the group is month-only); equal dates are all marked Y and noted. To group by code alone, or within a department, is a different reading; confirm if wanted.",
          "- Files with a height that cannot be grouped (%d): no room type code and no RLS number could be read from the file name or title block "
          "(cluster / key-plan / non-standard names such as `AR-RLS-32-MOT_S-001-STORE BLOOD...`, `RLS_STORE BLOOD_FLUID_PHARM_L5`). Their `governing_issue` is blank on purpose and "
          "`governing_issue_note` says `not grouped: ...`; they are not silently skipped." % len(ungrouped),
          "- Many sheets carry bare elevation dimensions (2700, 3000) with no CH label; these are not taken as ceiling heights, so those files stay without a height.",
          "- A file with an empty text layer (scanned or outlined) is not read; no model reading is used.",
          "- Title block date, revision and issue come from the DATE/REV/DESCRIPTION table; empty tables leave the fields blank.",
          "- Unknown stays unknown: no default height is written here (the flagged default belongs to the workbook, D-019)."]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


def build() -> Dict[str, int]:
    pdfs, other = ledger_rls_rows()
    cache = load_cache()
    rows = build_rows(pdfs, cache)
    write_csv(rows, root() / CSV_OUT)
    write_report(rows, pdfs, other, cache, root() / REPORT_OUT)
    return {"pdfs": len(pdfs), "read": len(rows),
            "with_height": sum(1 for r in rows if r["governing_height_mm"] != "")}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m src.rls_heights", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("run", help="extract (cached) then build register and report")
    rp.add_argument("--limit", type=int, default=None)
    rp.add_argument("--dept", default="", help="comma list of department numbers")
    rp.add_argument("--workers", type=int, default=3)
    sub.add_parser("build", help="rebuild register and report from the cache only")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        pdfs, _ = ledger_rls_rows()
        depts = [d.strip() for d in a.dept.split(",") if d.strip()] or None
        n = extract_all(pdfs, workers=a.workers, limit=a.limit, depts=depts)
        print("extracted %d new files" % n, file=sys.stderr)
    print(json.dumps(build()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
