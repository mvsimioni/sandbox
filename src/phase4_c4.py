"""Phase 4 checks P4-13 to P4-23 (council C4, build lines 29 to 40).

These checks read the files of the build, the PDF text layer of the working sheets (pymupdf), the DXF text of the new
drawings (ezdxf) and the workbooks (openpyxl).  They do not import ``review.py`` or ``model.py``: the checks for anchor
(P4-13), false add (P4-16), entity count (P4-17), one home (P4-18) and MOT (P4-21) must not share a reader with the build.
They only read the CSV files that the build writes (entity ledger, scope table, change list).

A check returns ``(PASS|FAIL, evidence)``.  A seeded-error run (``src/phase4_seeded.py``) points the module at a scratch copy
by changing ``SCH``, ``NEWD``, ``MARK`` and ``ISSUE`` and clearing ``_MEMO``.
"""

from __future__ import annotations

import csv
import glob
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
SCH = ROOT / "out" / "schematics"
NEWD = SCH / "new_drawings"
MARK = SCH / "markups"
STAGING = ROOT / "out" / "issue" / "_staging" / "SCHEMATICS_DRAFT"
ISSUE = ROOT / "out" / "issue" / "SCHEMATICS_DRAFT"
REG = ROOT / "out" / "registers"
QA = ROOT / "out" / "qa"
WD = ROOT / "data" / "source" / "Basis" / "Working Drawings"
WBDIR = ROOT / "out" / "loads" / "workbooks"
ZONE = ROOT / "out" / "zoning" / "zone_schedule.xlsx"
PASS, FAIL = "PASS", "FAIL"
ID_RE = re.compile(r"CH-[A-Z0-9]+-\d{4}")
csv.field_size_limit(10 ** 9)
_MEMO: Dict[str, Any] = {}
EVN = 12

INSTRUCTION_PREFIXES = ("Mark up", "New system", "New drawing")
FAN_QUANTITIES = ("Return fan airflow", "Exhaust fan airflow", "Toilet exhaust fan airflow", "Fan airflow", "Isolation exhaust airflow")


FAILS: Dict[str, List[str]] = {}


def ret(bad: List[str], ev: List[str]) -> Tuple[str, List[str]]:
    """Result of a check; the full list of failures is kept under the name of the check, for the README (known gaps)."""
    import sys
    FAILS[sys._getframe(1).f_code.co_name] = list(bad)
    return (FAIL if bad else PASS), ev + bad[:EVN]


def memo(key: str, fn):
    if key not in _MEMO:
        _MEMO[key] = fn()
    return _MEMO[key]


def reset() -> None:
    _MEMO.clear()


def rcsv(path: Path) -> List[Dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def sample(items: Iterable[Any], n: int = 6) -> str:
    items = list(items)
    return ", ".join(str(i) for i in items[:n]) + (f" and {len(items) - n} more" if len(items) > n else "")


def num(x: Any) -> Optional[float]:
    if x is None:
        return None
    s = str(x).strip().replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def changes() -> List[Dict[str, str]]:
    return memo("changes", lambda: rcsv(SCH / "change_register.csv"))


def ledger() -> List[Dict[str, str]]:
    return memo("ledger", lambda: rcsv(SCH / "entity_ledger.csv"))


def ledger_by_id() -> Dict[str, Dict[str, str]]:
    return memo("ledger_by_id", lambda: {r["entity_id"]: r for r in ledger()})


def scope_rows() -> List[Dict[str, str]]:
    return memo("scope", lambda: rcsv(SCH / "scope.csv"))


def dreg() -> List[Dict[str, str]]:
    return memo("dreg", lambda: rcsv(SCH / "drawing_register.csv"))


def bbox_of(s: str) -> Optional[List[float]]:
    try:
        b = json.loads(s)
        return [float(v) for v in b] if len(b) == 4 else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Tag keys: a reader of its own (not tag_keys of review.py)
# --------------------------------------------------------------------------- #
TAG_RE = re.compile(r"\b((?:AHU|PAHU|FCU|RAF|EAF|TEAF|IEAF|IEA|EF|SAF|OAF|BIBO|AMU|EQPT)-[A-Z0-9]{1,4}-[A-Z0-9]{2,}[A-Z0-9/\-]*)")
UNIT_PREFIX = ("AHU", "PAHU", "FCU")
FAN_PREFIX = ("RAF", "EAF", "TEAF", "IEAF", "IEA", "EF", "SAF", "OAF")


def tkey(tag: str) -> Optional[Tuple[str, str]]:
    """(function prefix, core) with the A/B/C duplex suffix of the last number removed: 'RAF-B-02-01X-01A/B' -> ('RAF', 'B-02-01X-01')."""
    t = (tag or "").upper().strip().strip(".,;:()")
    m = re.match(r"^([A-Z]+)-(.+)$", t)
    if not m:
        return None
    pre, core = m.groups()
    core = re.sub(r"(\d+)(?:[A-Z]|/\d+[A-Z]?)?(?:/[A-Z0-9]+)*$", r"\1", core)
    return pre, core


def core_of(tag: str) -> Optional[str]:
    k = tkey(tag)
    return k[1] if k else None


def tags_in(text: str) -> List[str]:
    """Tags with a full code (prefix and at least three more hyphen parts); a fragment such as 'AHU-B-08-08' is not a tag."""
    out = []
    for m in TAG_RE.finditer((text or "").upper()):
        t = m.group(1).rstrip("-/")
        if t.count("-") >= 3 and re.search(r"-\d{2}[A-Z]?(?:/[A-Z0-9]+)*$", t) and not re.search(r"-\d\d-\d\d$", t):
            out.append(t)
    return out


def unit_tags(text: str) -> List[str]:
    return [t for t in tags_in(text) if t.split("-")[0] in UNIT_PREFIX]


# --------------------------------------------------------------------------- #
# PDF text of the working sheets (pymupdf) and DXF text of the new drawings (ezdxf)
# --------------------------------------------------------------------------- #
def sheet_pdf(sheet: str) -> Optional[Path]:
    g = sorted(WD.glob(sheet + "_*.pdf"))
    return g[0] if g else None


def pdf_words(sheet: str, page: int = 1) -> List[Tuple[float, float, float, float, str]]:
    def load():
        import pymupdf
        p = sheet_pdf(sheet)
        if p is None:
            return []
        doc = pymupdf.open(p)
        try:
            pg = doc[min(page, len(doc)) - 1]
            return [(w[0], w[1], w[2], w[3], w[4]) for w in pg.get_text("words")]
        finally:
            doc.close()
    return memo(f"words:{sheet}:{page}", load)


def pdf_text(sheet: str) -> str:
    def load():
        import pymupdf
        p = sheet_pdf(sheet)
        if p is None:
            return ""
        doc = pymupdf.open(p)
        try:
            return "\n".join(pg.get_text() for pg in doc)
        finally:
            doc.close()
    return memo("text:" + sheet, load)


def text_under(sheet: str, bbox: List[float], page: int = 1, pad: float = 2.0) -> str:
    x0, y0, x1, y1 = bbox
    out = []
    for a, b, c, d, w in pdf_words(sheet, page):
        if c >= x0 - pad and a <= x1 + pad and d >= y0 - pad and b <= y1 + pad:
            out.append(w)
    return " ".join(out)


def numbers_in(text: str) -> List[float]:
    vals = []
    for m in re.finditer(r"[-+]?\d[\d,]*(?:\.\d+)?", text.replace("−", "-")):
        v = num(m.group(0))
        if v is not None:
            vals.append(v)
    return vals


def has_number(text: str, value: float, tol: float = 0.51, by_abs: bool = False) -> bool:
    for v in numbers_in(text):
        if abs(v - value) <= tol or (by_abs and abs(abs(v) - abs(value)) <= tol):
            return True
    return False


def dxf_texts(path: Path) -> List[str]:
    def load():
        import ezdxf
        doc = ezdxf.readfile(str(path))
        out: List[str] = []

        def walk(layout):
            for e in layout:
                t = e.dxftype()
                if t == "TEXT":
                    out.append(e.dxf.text)
                elif t == "MTEXT":
                    out.append(e.plain_text())
                elif t == "INSERT":
                    for a in getattr(e, "attribs", []):
                        out.append(a.dxf.text)
        walk(doc.modelspace())
        for blk in doc.blocks:
            walk(blk)
        return out
    return memo("dxf:" + str(path), load)


def new_dxf_files() -> List[Path]:
    return sorted(NEWD.glob("IP-BH-*_P1.dxf"))


def working_sheets() -> List[str]:
    """Every air-side working sheet of the list of drawings, with the overall schematics."""
    def load():
        s = {r["drawing_number"] for r in dreg() if r.get("kind") == "mark-up" or r.get("number_status") == "working issue"}
        return sorted(x for x in s if sheet_pdf(x))
    return memo("work_sheets", load)


# --------------------------------------------------------------------------- #
# Scope: list-only units recomputed from the MOT schedule (not read from scope.csv)
# --------------------------------------------------------------------------- #
def mot_keys() -> Set[Tuple[str, str]]:
    def load():
        ks = set()
        for r in rcsv(REG / "mot_ahu_schedule.csv"):
            k = tkey(r["tag"])
            if k:
                ks.add(k)
        return ks
    return memo("mot_keys", load)


def is_list_only_tag(tag: str) -> bool:
    k = tkey(tag)
    return bool(k and k in mot_keys())


def row_tags(r: Dict[str, str]) -> List[str]:
    return tags_in(" ".join([r.get("unit", ""), r.get("item", "")]))


# --------------------------------------------------------------------------- #
# Zone schedule
# --------------------------------------------------------------------------- #
def _d027_complete(t: str) -> bool:
    """D-027 reading used by the review gate: every zoned room counted, no counted room without a criteria row, and no
    second room list that states another room count than the one counted."""
    t = t.strip()
    m2 = re.search(r"second room list gives (\d+) rooms against (\d+) counted", t)
    if m2 and m2.group(1) != m2.group(2):
        return False
    if t.lower().startswith("complete"):
        return True
    if re.search(r"no criteria row|have no criteria", t) or t.startswith("Not complete: no department workbook") or "not in the zone schedule" in t:
        return False
    m = re.search(r"(\d+) of (\d+) zoned rooms counted", t)
    if m:
        return m.group(1) == m.group(2)
    return bool(re.search(r"all \d+ zoned rooms are counted|the zoned room is counted", t))


def zone_units() -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Unit key -> dict(tag, complete, cover, zoned_area, counted_area, depts), computed from the zone schedule cells."""
    def load():
        import openpyxl
        wb = openpyxl.load_workbook(ZONE, read_only=True, data_only=True)
        rows = list(wb["AHU summary"].iter_rows(values_only=True))
        wb.close()
        h = [str(c or "") for c in rows[0]]

        def col(name):
            for i, x in enumerate(h):
                if x.startswith(name):
                    return i
            return None
        ct, cd, ca, cc, cl, cp = col("AHU or FCU tag"), col("Departments"), col("Area (m"), col("Area counted"), col("Room list of the unit"), col("Rooms")
        out: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for r in rows[1:]:
            tag = r[ct]
            if not tag:
                continue
            tag = str(tag)
            zoned, counted = num(r[ca]), num(r[cc]) or 0.0
            cover = (counted / zoned) if zoned else None
            complete = _d027_complete(str(r[cl] or ""))
            for t in re.split(r"\s*;\s*", tag):
                k = tkey(t)
                if k:
                    out[k] = dict(tag=tag, complete=complete, cover=cover, zoned=zoned, counted=counted, depts=str(r[cd] or ""))
        return out
    return memo("zone_units", load)


def gate_ok(tag: str) -> Tuple[bool, str]:
    """The input gate of build line 11, recomputed from the zone schedule: in schedule, list complete, cover 0.90 to 1.10."""
    z = zone_units().get(tkey(tag) or ("", ""))
    if z is None:
        return False, "unit not in the zone schedule"
    if not z["complete"]:
        return False, "room list not complete"
    if z["cover"] is None or not (0.90 <= z["cover"] <= 1.10):
        return False, "cover " + ("n/a" if z["cover"] is None else f"{z['cover']:.2f}") + " outside 0.90 to 1.10"
    return True, ""


# --------------------------------------------------------------------------- #
# P4-13 drawn value anchored
# --------------------------------------------------------------------------- #
def check_p4_13_anchor(ctx) -> Tuple[str, List[str]]:
    rows = changes()
    led = ledger_by_id()
    bad: List[str] = []
    n = anch = sums = implied = 0
    for r in rows:
        sh = r["sheet"]
        if not sh.startswith("IP-BH-"):
            continue
        d = num(r["drawn"])
        if d is None:
            continue
        n += 1
        cid = r["change_id"] or f"(no ID, {r['sheet']} {r['item']} {r['quantity']})"
        bb = bbox_of(r["bbox"])
        if bb is None:
            bad.append(f"{cid}: numeric drawn value {r['drawn']} and no box")
            continue
        page = int(r["page"] or 1)
        txt = text_under(sh, bb, page)
        by_abs = r["quantity"] == "Pressure target"
        if has_number(txt, d, by_abs=by_abs):
            anch += 1
            continue
        if r["quantity"] == "Typical count" and d == 1:
            implied += 1                     # exempt: an implied typical count of 1 is not printed in the box
            continue
        comps = [x.strip() for x in re.split(r"[;,]", r.get("ledger_ids", "")) if x.strip()]
        if len(comps) >= 2:
            tot, ok = 0.0, True
            for c in comps:
                lr = led.get(c)
                cb = bbox_of(lr["bbox"]) if lr else None
                cv = num(lr["value"]) if lr else None
                if cv is None or cb is None or not has_number(text_under(lr["sheet"], cb, int(lr["page"] or 1)), cv):
                    ok = False
                    bad.append(f"{cid}: component {c} of the sum {r['drawn']} is not anchored in its box")
                    break
                tot += cv
            if ok:
                if abs(tot - d) <= 0.51:
                    sums += 1
                else:
                    bad.append(f"{cid}: drawn {r['drawn']} is not the sum {tot:g} of its component boxes {', '.join(comps)}")
            continue
        bad.append(f"{cid} {r['sheet']} {r['item']} {r['quantity']}: drawn value {r['drawn']} is not in the PDF text under the box {r['bbox']} (found: {txt[:50]!r})")
    ev = [f"{n} rows with a numeric drawn value and a box on a working sheet: {anch} anchored (PDF text under the box, 2 pt margin), {sums} sums with every component box anchored, "
          f"{implied} exempt (implied typical count 1, no number printed in the box)", f"{len(bad)} not anchored"]
    return ret(bad, ev)


# --------------------------------------------------------------------------- #
# P4-14 source read-back
# --------------------------------------------------------------------------- #
HEAD_OK = {
    "Supply airflow": ("final supply", "ahu supply airflow", "calculated airflow", "room supply", "supply"),
    "Return airflow": ("return", "recirculated"),
    "Exhaust airflow": ("exhaust",),
    "Outdoor air": ("ahu outdoor air", "outdoor air"),
    "Spill or relief air": ("spill",),
    "Pressure target": ("set pressure",),
    "Typical count": ("rooms",),
}
for _q in FAN_QUANTITIES + ("Exhaust system",):
    HEAD_OK[_q] = ("return", "recirculated", "spill", "exhaust", "total extract")

CELL_RE = re.compile(r"Workbook\s+(D\d\d|master summary),?\s+tab\s+([A-Za-z0-9 ]+?)\s*,?\s*(?:cells?\s+([A-Z]{1,2}\d+(?:\s*(?:and|,)\s*[A-Z]{1,2}\d+)*)|row\s+(\d+))")


def col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def wb_path(code: str) -> Optional[Path]:
    if code.lower().startswith("master"):
        return WBDIR / "AHU_master_summary.xlsx"
    g = sorted(WBDIR.glob(f"AIGH_C2_{code}_*.xlsx"))
    return g[0] if g else None


def wb_tab(code: str, tab: str) -> List[Tuple[Any, ...]]:
    def load():
        import openpyxl
        p = wb_path(code)
        if p is None:
            return []
        wb = openpyxl.load_workbook(p, data_only=True, read_only=True)
        try:
            if tab not in wb.sheetnames:
                return []
            return [tuple(r) for r in wb[tab].iter_rows(values_only=True)]
        finally:
            wb.close()
    return memo(f"wbtab:{code}:{tab}", load)


def header_row(rows: List[Tuple[Any, ...]]) -> int:
    """Index of the heading row: the row before the units row whose first cell is 'Room number', 'AHU or system', 'Node ...' or a first heading."""
    for i, r in enumerate(rows[:12]):
        c0 = str(r[0] or "")
        if c0.startswith(("Room number", "AHU or system", "Node (room", "AHU or FCU", "Fan")):
            return i
    return 3


def check_p4_14_source(ctx) -> Tuple[str, List[str]]:
    bad: List[str] = []
    n_cells = n_rows = ok = 0
    uncited: List[str] = []
    for r in changes():
        req = num(r["required"])
        is_instr = r["action"].startswith(INSTRUCTION_PREFIXES) and r["layer_type"] in ("airflows", "rooms")
        src = r.get("source", "") or ""
        cites = list(CELL_RE.finditer(src))
        cid = r["change_id"] or f"{r['sheet']} {r['item']}"
        if req is None or r["quantity"] in ("Typical count",):
            continue
        if not cites:
            if is_instr and r["quantity"] in HEAD_OK:
                uncited.append(cid)
            continue
        n_rows += 1
        vals: List[Optional[float]] = []
        row_cites = 0
        row_bad = len(bad)
        for m in cites:
            code, tab, cells, rowno = m.group(1), m.group(2).strip(), m.group(3), m.group(4)
            rows = wb_tab(code, tab)
            if not rows:
                bad.append(f"{cid}: cited tab {tab} of {code} cannot be opened")
                continue
            if cells:
                for letters, rn in re.findall(r"([A-Z]{1,2})(\d+)", cells):
                    n_cells += 1
                    row_cites += 1
                    ri, ci = int(rn) - 1, col_index(letters)
                    if ri >= len(rows) or ci >= len(rows[ri]):
                        bad.append(f"{cid}: cell {letters}{rn} of {code} {tab} is empty or out of range")
                        vals.append(None)
                        continue
                    hr = header_row(rows)
                    head = str(rows[hr][ci] or "").lower() if ci < len(rows[hr]) else ""
                    label = str(rows[ri][0] or "")
                    vals.append(num(rows[ri][ci]))
                    want = HEAD_OK.get(r["quantity"])
                    if want and not any(head.startswith(w) or w in head for w in want):
                        bad.append(f"{cid}: cell {letters}{rn} of {code} {tab} is under the heading {head!r}, not a {r['quantity']} column")
                    rno = r.get("room_no", "")
                    if rno and tab in ("Airflow", "Pressure") and label != rno and "linked" not in r["class"] and "average" not in r.get("note", ""):
                        bad.append(f"{cid}: cell {letters}{rn} is in the row of {label!r}, the row is for room {rno!r}")
                    elif not rno and tab in ("AHU", "AHU Master") and r["unit"] and label and not r["unit"].startswith("AHU group"):
                        ku = tkey(unit_tags(r["unit"])[0]) if unit_tags(r["unit"]) else None
                        kl = tkey(label)
                        if ku and kl and ku[1] != kl[1]:
                            bad.append(f"{cid}: cell {letters}{rn} is in the row of {label!r}, the unit is {r['unit']!r}")
            else:
                n_cells += 1
                row_cites += 1
                ri = int(rowno) - 1
                if ri >= len(rows):
                    bad.append(f"{cid}: row {rowno} of {code} {tab} does not exist")
                elif not any(num(c) is not None and abs(num(c) - req) <= max(0.51, 0.005 * abs(req)) for c in rows[ri]) \
                        and not ("relief path" in r["class"] and f"{round(req):,}" in r.get("note", "")):   # derived excess: stated in the note
                    bad.append(f"{cid}: required {req:g} is in no cell of row {rowno} of {code} {tab}")
        good = [v for v in vals if v is not None]
        if vals and len(good) == len(vals):
            pools = {good[0]} if len(good) == 1 else {sum(good), max(0.0, sum(good)), max(good), min(good), sum(good) / len(good)}
            stated = "ormula" in (src + " " + r.get("note", "")) or "average" in r.get("note", "").lower() or "linked" in r["class"]
            if len(good) > 1 and not stated:
                bad.append(f"{cid}: {len(good)} cells cited and neither a formula nor an average is stated")
            elif not any(abs(req - p) <= max(0.51, 0.005 * abs(p)) for p in pools):
                bad.append(f"{cid}: required {req:g} differs from the cited cell(s) {good} (sum {sum(good):g}, average {sum(good) / len(good):g})")
        if len(bad) == row_bad and row_cites:
            ok += 1
    if uncited:
        bad.append(f"{len(uncited)} instruction rows with a required value cite no workbook cell: {sample(uncited)}")
    ev = [f"{n_rows} rows cite {n_cells} workbook cells or rows; each is opened: the cell lies in the row of the room (room-number label) or unit, under the heading of its quantity, and equals 'required' "
          f"(several cells: the row states its formula or average, required equals the sum or the average); {ok} rows agree", f"{len(bad)} differences"]
    return ret(bad, ev)


# --------------------------------------------------------------------------- #
# P4-15 fans
# --------------------------------------------------------------------------- #
def workbook_fan_rows() -> List[Dict[str, str]]:
    def load():
        import openpyxl
        out = []
        wb = openpyxl.load_workbook(SCH / "schematic_update_register.xlsx", read_only=True)
        for ws in wb.worksheets:
            for row in ws.iter_rows(min_row=8, values_only=True):
                if row and len(row) > 12 and row[1] == "Fan":
                    out.append(dict(item=str(row[2] or ""), quantity=str(row[3] or ""), drawn=str(row[5] or ""), required=str(row[6] or ""),
                                    cls=str(row[9] or ""), action=str(row[10] or ""), id=str(row[11] or ""), sheet=str(row[12] or ""),
                                    **{"class": str(row[9] or "")}))
        wb.close()
        return out
    return memo("wb_fan_rows", load)


def check_p4_15_fans(ctx) -> Tuple[str, List[str]]:
    rows = [r for r in changes() if r["quantity"] in FAN_QUANTITIES]
    led = ledger_by_id()
    bad: List[str] = []
    n_own = n_pair = n_ver = 0
    req_by_tag: Dict[str, Set[float]] = defaultdict(set)
    for r in rows:
        cid = r["change_id"] or f"{r['sheet']} {r['item']}"
        is_query = r["action"].startswith("Query")
        ftags = [t for t in tags_in(r["item"]) if t.split("-")[0] in FAN_PREFIX]
        utags = unit_tags(r["unit"])
        # ownership by tag core: the fan core is the core of one of its AHUs
        if ftags and utags and not is_query:
            n_own += 1
            fc = {core_of(t) for t in ftags}
            uc = {core_of(t) for t in utags}
            if not (fc & uc):
                bad.append(f"{cid}: fan {ftags[0]} has no AHU of its tag core among {', '.join(utags)} and the row is not a query")
        if ftags and len({core_of(u) for u in utags}) >= 3 and not (is_query and ("link" in r["action"].lower() or "link" in r["class"].lower())):
            bad.append(f"{cid}: fan {ftags[0]} is linked to {len({core_of(u) for u in utags})} units ({', '.join(utags[:4])}); the row is not a query about the link")
        rq = num(r["required"])
        if rq is not None and not is_query:
            for t in ftags:
                req_by_tag[t.upper()].add(round(rq, 1))
        # pair counted once: a numeric drawn value over several ledger rows is one value per duty/standby pair
        comps = [x.strip() for x in re.split(r"[;,]", r.get("ledger_ids", "")) if x.strip()]
        d = num(r["drawn"])
        if d is not None and len(comps) >= 2:
            n_pair += 1
            seen: Dict[Tuple[Optional[str], float], int] = {}
            vals = []
            for c in comps:
                lr = led.get(c)
                v = num(lr["value"]) if lr else None
                if v is None:
                    continue
                key = (core_of(lr["text"]), v)
                if key in seen:
                    continue                  # same drawn duty on an A/B pair: duty/standby, counted once
                seen[key] = 1
                vals.append(v)
            if vals and abs(sum(vals) - d) > 0.51:
                bad.append(f"{cid}: drawn {d:g} is not the sum of the drawn duties with each duty/standby pair counted once ({sum(vals):g} from {', '.join(comps)})")
        # a verdict 'covers/adequate/keep' is recomputed with the drawn value
        note = (r.get("note", "") + " " + r.get("class", "")).lower()
        if d is not None and rq is not None and ("covers the requirement" in note or "adequate" in note) and not is_query:
            n_ver += 1
            if d + max(0.05 * rq, 10.0) < rq:
                bad.append(f"{cid}: verdict 'covers' flips: drawn {d:g} against required {rq:g}")
    # the Fan rows of the unit tabs of the update workbook: verdict recomputed, drawn value once per pair
    n_wb = 0
    for fr in workbook_fan_rows():
        n_wb += 1
        d, rq = num(fr["drawn"]), num(fr["required"])
        verdict = (fr["class"] + " " + fr["action"]).lower()
        tag = (tags_in(fr["item"]) or [""])[0]
        k = tkey(tag) if tag else None
        vals = [num(l["value"]) for l in ledger() if l["sheet"] == fr["sheet"] and l["kind"] == "fan tag" and k and tags_in(l["text"]) and tkey(tags_in(l["text"])[0]) == k
                and num(l["value"]) is not None]
        if d is not None and vals and not fr["action"].startswith("Query"):
            exp = vals[0] if len(set(vals)) == 1 else sum(sorted(set(vals)))
            if abs(exp - d) > 0.51:
                bad.append(f"{fr['id'] or fr['sheet'] + ' ' + fr['item']}: drawn {d:g} for {tag}, ledger duties {sorted(set(vals))} give {exp:g} (pair counted once)")
        if d is not None and rq is not None and not fr["action"].startswith("Query"):
            covered = d + max(0.05 * rq, 10.0) >= rq
            says_ok = any(w in verdict for w in ("keep", "adequate", "covers", "margin", "no change"))
            says_short = any(w in verdict for w in ("increase", "resize", "short"))
            if (says_ok and not covered) or (says_short and covered):
                n_ver += 1
                bad.append(f"{fr['id'] or fr['sheet'] + ' ' + fr['item']}: verdict {fr['class']!r} flips with drawn {d:g} against required {rq:g}")
            else:
                n_ver += 1
    for t, v in req_by_tag.items():
        if len(v) > 1:
            bad.append(f"{t}: {len(v)} required values {sorted(v)} on instruction rows (one tag, one requirement)")
    # ledger: a fan group written twice with different requirement is caught above; tag with two duties needs a query
    duties: Dict[str, Set[float]] = defaultdict(set)
    for lr in ledger():
        if lr["kind"] == "fan tag" and num(lr["value"]) is not None and tags_in(lr["text"]):
            duties[tags_in(lr["text"])[0]].add(num(lr["value"]))
    two = {t for t, v in duties.items() if len(v) > 1}
    qtags = {t.upper() for r in changes() if r["action"].startswith("Query") for t in tags_in(r["item"] + " " + r["note"] + " " + r["drawn"])}
    for t in sorted(two):
        if t not in qtags:
            bad.append(f"{t}: printed with {len(duties[t])} duties and no query row 'tag to correct'")
    ev = [f"{len(rows)} fan rows of the change list and {n_wb} Fan rows of the unit tabs; {n_own} instruction rows checked for ownership by tag core; {n_pair} sums over a pair checked (pair counted once); {n_ver} verdicts recomputed with the drawn value",
          f"{len(two)} tags printed with two duties; {len(bad)} failures"]
    return ret(bad, ev)


# --------------------------------------------------------------------------- #
# P4-16 no add where drawn
# --------------------------------------------------------------------------- #
def split_tags(sheet: str) -> List[str]:
    """Tags that the sheet prints over two text lines ('AHU-B-B1-B2' above 'MMDTSWWG-01A'): the second line is directly below the
    first.  Read from the PDF text layer (spans), not from the review."""
    def load():
        import pymupdf
        p = sheet_pdf(sheet)
        if p is None:
            return []
        doc = pymupdf.open(p)
        out: List[str] = []
        try:
            for pg in doc:
                spans = [(sp["text"].strip(), sp["bbox"]) for b in pg.get_text("dict")["blocks"] for ln in b.get("lines", []) for sp in ln["spans"] if sp["text"].strip()]
                for t, bb in spans:
                    if not re.match(r"^(?:P?AHU|RAF|EAF|TEAF|IEAF|EF)-[A-Z0-9]+-[A-Z0-9]+-(?:[A-Z0-9]{0,2})$", t):
                        continue
                    for t2, b2 in spans:
                        if -5 <= b2[1] - bb[3] < 6 and abs((b2[0] + b2[2]) / 2 - (bb[0] + bb[2]) / 2) < 30 and re.match(r"^[0-9A-Z]{2,}[A-Z0-9\- /]*$", t2) \
                                and t2 != t:
                            out.append(re.sub(r"\s+", "", t + t2))
                            break
        finally:
            doc.close()
        return out
    return memo("split:" + sheet, load)


def near_tag_printed(k: Tuple[str, str], idx: Dict[Tuple[str, str], Set[str]], sheets: Iterable[str]) -> Optional[Tuple[Tuple[str, str], str]]:
    """A tag printed on one of the sheets with the same level and a code that starts with the code of k, or the reverse."""
    def parts(key):
        m = re.match(r"^B-([A-Z0-9]{1,2})-([A-Z0-9]+?)(?:-\d+)?$", key[1])
        return (m.group(1), m.group(2)) if m else None
    a = parts(k)
    if not a or k[0] not in ("AHU",):
        return None
    for k2, shs in idx.items():
        if k2[0] != "AHU" or k2 == k:
            continue
        b = parts(k2)
        hit = [x for x in shs if x in set(sheets)]
        if b and hit and b[0] == a[0] and b[1] != a[1] and min(len(a[1]), len(b[1])) >= 5 and (b[1].startswith(a[1]) or a[1].startswith(b[1])):
            return k2, hit[0]
    return None


def sheet_tag_index() -> Dict[Tuple[str, str], Set[str]]:
    """(function, core) -> sheets whose PDF text prints the tag (working sheets only)."""
    def load():
        idx: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
        for sh in working_sheets():
            for t in tags_in(pdf_text(sh)) + split_tags(sh):
                k = tkey(t)
                if k:
                    idx[k].add(sh)
        return idx
    return memo("tag_index", load)


def spill_label_sheets() -> Set[str]:
    out = set()
    for sh in working_sheets():
        for line in pdf_text(sh).splitlines():
            u = line.upper()
            if ("SPILL AIR" in u or "RELIEF AIR" in u) and "SHALL" not in u and "BRANCH" not in u:
                out.add(sh)
                break
    return out


ADD_RE = re.compile(r"add an? (return fan|spill|relief)", re.I)


def is_add_row(r: Dict[str, str]) -> bool:
    a = r["action"]
    return (a.startswith("New system") or (a.startswith("Mark up: add") and not a.startswith("Mark up: add the room")) or r["class"] == "missing system"
            or r["quantity"] in ("Unit with no schematic", "System of a DD Booklet absent from the drawings", "App D system absent from the working drawings"))


def check_p4_16_false_add(ctx) -> Tuple[str, List[str]]:
    idx = sheet_tag_index()
    spill = spill_label_sheets()
    bad: List[str] = []
    n = 0
    fan_funcs = ("RAF", "EAF", "TEAF", "EF", "IEAF", "IEA")
    for r in changes():
        cid = r["change_id"] or f"{r['sheet']} {r['item']}"
        if ADD_RE.search(r["action"]):
            bad.append(f"{cid}: instruction '{r['action']}' (build line 20: never an instruction in this draft)")
            continue
        if not is_add_row(r):
            continue
        n += 1
        tags = tags_in(r["item"]) if r["quantity"].startswith("Separated room") else tags_in(r["item"] + " " + r["unit"])
        func_row = r["quantity"] in ("Exhaust system",) or "exhaust" in r["action"].lower() and r["quantity"] != "Unit with no schematic"
        overall_row = r["class"] == "missing on overall"
        for t in tags:
            k = tkey(t)
            if not k:
                continue
            if func_row:
                # a missing exhaust system: look for an exhaust element of the system on any working sheet
                for f in ("EAF", "TEAF", "IEAF", "IEA", "EF"):
                    for kk in (k, (f, k[1])):
                        if (f, kk[1]) in idx:
                            bad.append(f"{cid}: '{r['action'][:40]}' for the system {k[1]}, and {f}-{k[1]} is printed on {sample(sorted(idx[(f, kk[1])]), 3)}")
                            break
                continue
            hits = idx.get(k)
            if overall_row:
                hits = {h for h in (hits or set()) if h in ("IP-BH-00100", "IP-BH-00101")}
                nm = near_tag_printed(k, idx, ("IP-BH-00100", "IP-BH-00101")) if not hits else None
                if nm and r["action"].startswith("Mark up"):
                    bad.append(f"{cid}: '{r['action'][:40]}' for {t}, and the near-match {nm[0][0]}-{nm[0][1]} is printed on {nm[1]} (a near-match of a tag is a query)")
                    break
            if hits:
                bad.append(f"{cid}: '{r['action'][:40]}' for {t}, and the tag is printed on {sample(sorted(hits), 3)}")
                break
            if k[0] in UNIT_PREFIX and not overall_row:
                # the same system code with a fan function on a working sheet
                for f in fan_funcs:
                    if (f, k[1]) in idx and r["quantity"] in FAN_QUANTITIES:
                        bad.append(f"{cid}: add for the system {k[1]} and {f}-{k[1]} is printed on {sample(sorted(idx[(f, k[1])]), 3)}")
                        break
        if "spill" in (r["quantity"] + r["action"] + r["item"]).lower() and r["sheet"] in spill and r["action"].startswith("Mark up"):
            bad.append(f"{cid}: add of a spill or relief path on {r['sheet']}, and the sheet prints a SPILL AIR label")
    ev = [f"{n} rows add, miss or show 'no schematic' for a unit or fan; each tag is looked up in the PDF text of {len(working_sheets())} working sheets ({sum(len(v) for v in idx.values())} tag prints; general notes excluded)",
          f"0 rows 'add a return fan' or 'add a spill/relief path' required; {len(bad)} failures"]
    return ret(bad, ev)


# --------------------------------------------------------------------------- #
# P4-17 every drawn entity has one disposition
# --------------------------------------------------------------------------- #
DISP_HEADS = re.compile(r"(reviewed under unit|shared: units|list only \(MOT\)|query: |not an air-side entity|not reviewed)")


def check_p4_17_dispositions(ctx) -> Tuple[str, List[str]]:
    led = ledger()
    bad: List[str] = []
    ids = Counter(r["entity_id"] for r in led)
    dup = [i for i, c in ids.items() if c > 1]
    if dup:
        bad.append(f"{len(dup)} ledger entity IDs twice: {sample(dup)}")
    zero, two = [], []
    for r in led:
        n = len(DISP_HEADS.findall(r["disposition"] or ""))
        if n == 0:
            zero.append(r["entity_id"])
        elif n > 1:
            two.append(r["entity_id"])
    if zero:
        bad.append(f"{len(zero)} ledger rows with no disposition: {sample(zero)}")
    if two:
        bad.append(f"{len(two)} ledger rows with two dispositions: {sample(two)}")
    # room boxes of schematic_rooms.csv against the ledger room rows with a class, per working sheet
    work = set(working_sheets())
    boxes = Counter()
    seen_box = set()
    for r in rcsv(REG / "schematic_rooms.csv"):
        if r.get("issue_set") == "working" and r["sheet"] in work:
            kb = (r["sheet"], r["room_name_as_written"], r.get("box_page_view_table", ""), r.get("mode", ""))
            if kb in seen_box:
                continue
            seen_box.add(kb)
            boxes[r["sheet"]] += 1
    lrooms = Counter(r["sheet"] for r in led if r["kind"] == "room box" and r["disposition_class"])
    diff = [f"{s}: {boxes[s]} boxes, {lrooms.get(s, 0)} room rows with a class" for s in sorted(boxes) if boxes[s] != lrooms.get(s, 0)]
    if diff:
        bad.append(f"{len(diff)} sheets where the room boxes and the room rows with a class differ: {sample(diff, 4)}")
    # AHU and fan tags found by regex in the PDF text of each working sheet are all in the ledger
    keys_by_sheet: Dict[str, Set[Tuple[str, str]]] = defaultdict(set)
    for r in led:
        for t in tags_in(r["text"]):
            k = tkey(t)
            if k:
                keys_by_sheet[r["sheet"]].add(k)
        for t in re.split(r"[;|]", r.get("tag_keys", "")):
            m = re.match(r"^\s*([A-Z]+):(.+)$", t.strip())
            if m:
                keys_by_sheet[r["sheet"]].add((m.group(1), m.group(2)))
    missing, n_tags = [], 0
    zk = zone_units()
    qrows = {(r["sheet"], tkey(t)) for r in changes() if r["action"].startswith("Query") for t in tags_in(r["unit"] + " " + r["item"])}
    for r in led:                           # a ledger row 'query: drawn, not in the zone schedule' is the query row of the tag on its sheet
        if (r["disposition"] or "").startswith(("query", "list only")):
            for t in tags_in(r["text"]):
                qrows.add((r["sheet"], tkey(t)))
    zone_q = []
    for sh in sorted(work):
        seen = {tkey(t) for t in tags_in(pdf_text(sh))}
        for k in sorted(x for x in seen if x):
            n_tags += 1
            if k not in keys_by_sheet[sh] and not any(k[1] == c[1] for c in keys_by_sheet[sh] if c[0] == k[0]):
                missing.append(f"{sh} {k[0]}-{k[1]}")
            if k[0] == "AHU" and k not in zk and (sh, k) not in qrows and not is_list_only_tag(f"{k[0]}-{k[1]}"):
                zone_q.append(f"{sh} {k[0]}-{k[1]}")
    if missing:
        bad.append(f"{len(missing)} AHU or fan tags in the PDF text of a working sheet and not in the ledger for that sheet: {sample(missing)}")
    if zone_q:
        bad.append(f"{len(zone_q)} AHU tags on a working sheet, not in the zone schedule and with no query row: {sample(zone_q)}")
    ev = [f"{len(led)} ledger rows: {len(led) - len(zero) - len(two)} with one disposition; {len(work)} working sheets; {n_tags} tag keys read from the PDF text; {sum(boxes.values())} room boxes in schematic_rooms.csv"]
    return ret(bad, ev)


# --------------------------------------------------------------------------- #
# P4-18 one home per unit
# --------------------------------------------------------------------------- #
XREF_RE = re.compile(r"shown on\s+IP-BH-\d|original AHU|separated from", re.I)


def check_p4_18_home(ctx) -> Tuple[str, List[str]]:
    idx = sheet_tag_index()
    homes: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for p in new_dxf_files():
        num_ = p.name[:11]
        seen: Set[Tuple[str, str]] = set()
        for t in dxf_texts(p):
            if XREF_RE.search(t):
                continue                      # a value-free cross-reference line is not a home
            for tag in tags_in(t):
                k = tkey(tag)
                if k and k[0] in UNIT_PREFIX:
                    seen.add(k)
        for k in seen:
            homes[k].append(num_)
    bad = []
    for k, hs in sorted(homes.items()):
        w = sorted(idx.get(k, set()))
        if len(hs) > 1:
            bad.append(f"{k[0]}-{k[1]} is a home on {len(hs)} new drawings: {sample(hs)}")
        if w:
            bad.append(f"{k[0]}-{k[1]} is on the new drawing {hs[0]} and printed on the working sheet {w[0]}")
    multi = sum(1 for k, v in idx.items() if k[0] in UNIT_PREFIX and len(v) > 1)
    ev = [f"{len(new_dxf_files())} new drawings (DXF text) and {len(working_sheets())} working sheets (PDF text): {len(homes)} unit tags on new drawings, "
          f"{sum(1 for k in idx if k[0] in UNIT_PREFIX)} unit tags on working sheets ({multi} on several sheets of one system: sister sheets, not counted)", f"{len(bad)} failures"]
    return ret(bad, ev)


# --------------------------------------------------------------------------- #
# P4-19 gate
# --------------------------------------------------------------------------- #
UNIT_LEVEL_Q = ("Supply airflow", "Outdoor air", "Spill or relief air", "Return fan airflow", "Exhaust fan airflow", "Toilet exhaust fan airflow", "Fan airflow",
                "Isolation exhaust airflow", "Exhaust airflow", "Return airflow", "Coil duty")


def is_unit_level(r: Dict[str, str]) -> bool:
    return not r.get("room_no") and r["quantity"] in UNIT_LEVEL_Q and not (r.get("note", "").startswith("Per room"))


def dept_names() -> Dict[str, str]:
    out = {}
    for p in WBDIR.glob("AIGH_C2_D*_*.xlsx"):
        m = re.match(r"AIGH_C2_D(\d\d)_(.+?)_Thermal_Load_Calc", p.name)
        if m:
            out[m.group(1)] = re.sub(r"[^a-z0-9]+", " ", m.group(2).lower()).strip()
    return out


def check_p4_19_gate(ctx) -> Tuple[str, List[str]]:
    bad: List[str] = []
    n_unit = n_room = n_red = n_add = 0
    ledger_rooms = {re.sub(r"\W+", "", r["text"].lower()) for r in ledger() if r["kind"] == "room box" and r["sheet_role"] == "working"}
    links = memo("links", lambda: rcsv(REG / "room_unit_links.csv"))
    dn = dept_names()
    for r in changes():
        cid = r["change_id"] or f"{r['sheet']} {r['item']}"
        instr = r["action"].startswith(INSTRUCTION_PREFIXES)
        if not instr:
            continue
        # unit-level rows: the gate holds for every unit of the row
        if is_unit_level(r) and r["layer_type"] in ("airflows", "new systems", "rooms"):
            n_unit += 1
            for t in unit_tags(r["unit"]) or unit_tags(r["item"]):
                ok, why = gate_ok(t)
                if not ok:
                    bad.append(f"{cid}: instruction-grade unit-level row ({r['quantity']}) on {t}: {why}")
                    break
        # room increases from a name-only link
        if r["layer_type"] == "airflows" and r.get("room_no"):
            n_room += 1
            dr, rq = num(r["drawn"]), num(r["required"])
            lt = r.get("link_type", "")
            if lt != "room number" and dr and rq is not None and r["action"].startswith("Mark up"):
                ratio = rq / dr
                # a number-matched room above 2 times stays a mark-up; a link by name (name only or one-to-one name) does not
                if ratio > 2 or (ratio < 0.5 and lt.startswith("name only")):
                    bad.append(f"{cid}: room increase on a {lt or 'name'} link with ratio {ratio:.2f}")
            ud = [zone_units().get(tkey(t) or ("", ""), {}).get("depts", "") for t in unit_tags(r["unit"])]
            wd = dn.get(r.get("dept", ""), "")
            if wd and ud and all(u for u in ud):
                norm = [re.sub(r"[^a-z0-9]+", " ", u.lower()) for u in ud]
                words = {w for w in wd.split() if len(w) > 3}
                if r.get("link_type", "").startswith("name only") and not any(words & set(u.split()) for u in norm) and not any("common" in u for u in norm):
                    bad.append(f"{cid}: name-only room link across departments: workbook room of {wd!r}, unit departments {ud[0][:50]!r}")
        # an added FCU and path (D-076 separated room) and a 'show the value' row: gate, and the room is on no working sheet
        if r["quantity"].startswith("Separated room"):
            n_add += 1
            for t in unit_tags(r["unit"]):
                ok, why = gate_ok(t)
                if not ok:
                    bad.append(f"{cid}: 'New system: FCU and outdoor-air connection' on {t} fails the gate: {why}")
                    break
            nm_ = re.sub(r"^Room\s+\S+(?:\s+\d+(?:\.\d+)*)?\s+", "", r["item"])
            if re.sub(r"\W+", "", nm_.lower()) in ledger_rooms:
                bad.append(f"{cid}: room {nm_!r} is drawn on a working sheet and is also given an FCU")
        if r["action"] == "Mark up: show the required value":
            for t in unit_tags(r["unit"]):
                ok, why = gate_ok(t)
                if not ok:
                    bad.append(f"{cid}: 'show the required value' ({r['quantity']}) on {t} fails the gate: {why}")
                    break
        if r["sheet"] == "App D" and r["layer_type"] == "new systems" and r["quantity"].startswith("App D system"):
            for t in unit_tags(r["item"]):
                if t.startswith(("AHU", "PAHU")):
                    ok, why = gate_ok(t)
                    if not ok:
                        bad.append(f"{cid}: new App D system {t} fails the gate: {why}")
                    break
        # reductions of outdoor air or spill
        if r["action"].startswith("Mark up: reduction") and r["quantity"] in ("Outdoor air", "Spill or relief air"):
            n_red += 1
            bad.append(f"{cid}: reduction mark-up of {r['quantity']} (a query until Marcus confirms)")
        # D-076 boxes: absent from every working sheet, one-to-one link to this unit, unit passes the gate
        if r["action"].startswith("Mark up: add the room"):
            n_add += 1
            nm = re.sub(r"\W+", "", r["item"].lower())
            if nm in ledger_rooms:
                bad.append(f"{cid}: D-076 box {r['item']!r} is drawn on a working sheet")
            lk = [l for l in links if l["room_name"] == r["item"] or l.get("dpa_room_id") == r.get("room_no")]
            if not any(l.get("unit_class", "") and tkey(l["unit_class"].split(";")[0]) == tkey((unit_tags(r["unit"]) or [""])[0]) for l in lk):
                bad.append(f"{cid}: D-076 box {r['item']!r} has no one-to-one zoning link to {r['unit']} in room_unit_links.csv")
            for t in unit_tags(r["unit"]):
                ok, why = gate_ok(t)
                if not ok:
                    bad.append(f"{cid}: D-076 box on {t} fails the gate: {why}")
                    break
    ev = [f"{n_unit} unit-level instruction rows checked against the zone schedule (in schedule, room list complete, cover 0.90 to 1.10 recomputed from areas); {n_room} room airflow rows; "
          f"{n_red} OA or spill reductions; {n_add} D-076 boxes", f"{len(bad)} failures"]
    return ret(bad, ev)


# --------------------------------------------------------------------------- #
# P4-20 value domain
# --------------------------------------------------------------------------- #
UOM = {"Pa": ("Pressure target",), "no.": ("Typical count",)}
AIRFLOW_Q = {"Supply airflow", "Return airflow", "Exhaust airflow", "Outdoor air", "Spill or relief air"} | set(FAN_QUANTITIES)


def pressure_cell(room: str, code: str) -> Optional[float]:
    rows = wb_tab(code, "Pressure")
    for r in rows[5:]:
        if r and str(r[0] or "") == room:
            return num(r[4])
    return None


def check_p4_20_domain(ctx) -> Tuple[str, List[str]]:
    bad: List[str] = []
    n_p = n_t = n_a = 0
    for r in changes():
        cid = r["change_id"] or f"{r['sheet']} {r['item']}"
        q = r["quantity"]
        if q == "Pressure target":
            n_p += 1
            rq = num(r["required"])
            if rq is not None:
                if not (-60 <= rq <= 60):
                    bad.append(f"{cid}: pressure required {rq:g} Pa outside -60 to +60")
                code = f"D{r['dept']}" if r.get("dept") else None
                if code and r.get("room_no"):
                    own = pressure_cell(r["room_no"], code)
                    if own is None or abs(own - rq) > 0.01:
                        bad.append(f"{cid}: pressure required {rq:g} Pa is not the Set pressure of room {r['room_no']} in the Pressure tab ({own})")
            if r["layer_type"] == "airflows" or r["action"].startswith("Mark up"):
                bad.append(f"{cid}: pressure row carries a mark-up (cloud): {r['action'][:50]}")
            if r["unit_of_measure"] != "Pa":
                bad.append(f"{cid}: pressure row in {r['unit_of_measure']!r}")
        if q == "Typical count":
            for k in ("required", "drawn"):
                v = num(r[k])
                if v is not None:
                    n_t += 1
                    if v < 1 or v != int(v):
                        bad.append(f"{cid}: typical count {r[k]} is not a whole number of 1 or more")
            if r["unit_of_measure"] not in ("no.", ""):
                bad.append(f"{cid}: typical count in {r['unit_of_measure']!r}")
        if q in AIRFLOW_Q:
            for k in ("required", "drawn"):
                v = num(r[k])
                if v is not None:
                    n_a += 1
                    if v < 0:
                        bad.append(f"{cid}: negative airflow {r[k]}")
            if r["unit_of_measure"] != "L/s":
                bad.append(f"{cid}: airflow {q!r} in {r['unit_of_measure']!r}")
    ev = [f"{n_p} pressure rows (|Pa| <= 60, equal to the room's own Pressure-tab row, no mark-up); {n_t} typical-count values (whole, >= 1); {n_a} airflow values (>= 0); unit of measure agrees with quantity",
          f"{len(bad)} failures"]
    # clouds on pressure rows in the mark-up PDFs: the mark-up log of each sheet names the annotation and its change ID
    pids = {r["change_id"] for r in changes() if r["quantity"] == "Pressure target" and r["change_id"]}
    for p in sorted(MARK.glob("*_markup_log.csv")):
        for l in rcsv(p):
            if l["change_id"] in pids and "cloud" in (l.get("annotation", "") or "").lower():
                bad.append(f"{l['change_id']}: a cloud on a pressure row in {p.name}")
    return ret(bad, ev)


# --------------------------------------------------------------------------- #
# P4-21 MOT
# --------------------------------------------------------------------------- #
def markup_subjects() -> Dict[str, Set[str]]:
    def load():
        import pymupdf
        out: Dict[str, Set[str]] = {}
        for p in sorted(MARK.glob("*_markup.pdf")):
            doc = pymupdf.open(p)
            ids: Set[str] = set()
            for pg in doc:
                for a in pg.annots():
                    s = (a.info or {}).get("subject", "")
                    ids.add(s if ID_RE.fullmatch(s or "") else "(none)")
            doc.close()
            out[p.name[:11]] = ids
        return out
    return memo("markup_subjects", load)


def check_p4_21_mot(ctx) -> Tuple[str, List[str]]:
    import openpyxl
    bad: List[str] = []
    mk = mot_keys()
    rows = changes()
    by_id = {r["change_id"]: r for r in rows if r["change_id"]}
    lo_rows = [r for r in rows if r["change_id"] and any(is_list_only_tag(t) for t in row_tags(r))]
    # a row that names a list-only unit and a mixed group is allowed only when the row is on the overall tab for the other units: none expected
    for r in lo_rows:
        bad.append(f"{r['change_id']}: change ID on list-only unit {sample([t for t in row_tags(r) if is_list_only_tag(t)], 2)} ({r['sheet']}, {r['action'][:40]})")
    # annotations of the mark-up PDFs
    subj = markup_subjects()
    n_ann = 0
    for sh, ids in subj.items():
        for i in ids:
            n_ann += 1
            r = by_id.get(i)
            if r and any(is_list_only_tag(t) for t in row_tags(r)):
                bad.append(f"{i}: annotation in {sh}_markup.pdf on a list-only unit")
        # a sheet on which every AHU is list-only has no annotation at all
        ahus = {tkey(t) for t in unit_tags(pdf_text(sh))}
        if ahus and all(k in mk for k in ahus if k) and ids:
            bad.append(f"{sh}: every AHU on the sheet is list-only and the mark-up PDF holds {len(ids)} annotations")
    # no overall-schematic row for such a unit
    for r in rows:
        if r["sheet"] in ("IP-BH-00100", "IP-BH-00101") and any(is_list_only_tag(t) for t in row_tags(r)) and r["action"].startswith(INSTRUCTION_PREFIXES):
            bad.append(f"{r['change_id']}: overall-schematic instruction for a list-only unit")
    # scope.csv recomputed: the table must agree with the schedule
    for s in scope_rows():
        if s["kind"] == "unit" and s["unit"]:
            lo = s["list_only"] == "1"
            sched = any(is_list_only_tag(t) for t in unit_tags(s["unit"]) + tags_in(s["keys"]))
            if sched and not lo:
                bad.append(f"scope.csv: {s['unit']} is in the MOT schedule and not marked list-only")
    # every list-only unit on every sheet where it is drawn has a row on the MOT differences tab
    wb = openpyxl.load_workbook(SCH / "schematic_update_register.xlsx", read_only=True)
    tab = {(str(r[0]), tkey(str(r[1]))) for r in list(wb["MOT differences"].iter_rows(values_only=True))[5:] if r and r[0] and r[1]}
    wb.close()
    n_pairs = 0
    miss = []
    for sh in working_sheets():
        for t in {t for t in tags_in(pdf_text(sh)) if t.split("-")[0] in UNIT_PREFIX + FAN_PREFIX}:
            k = tkey(t)
            if k in mk:
                n_pairs += 1
                if (sh, k) not in tab and not any(s == sh and kk and kk[1] == k[1] for s, kk in tab):
                    miss.append(f"{sh} {t}")
    if miss:
        bad.append(f"{len(miss)} list-only units or fans drawn on a sheet with no row on the MOT differences tab: {sample(miss)}")
    ev = [f"{len(mk)} tag keys in the MOT schedule; {len(lo_rows)} change IDs on them; {n_ann} annotations read in {len(subj)} mark-up PDFs; {n_pairs} (sheet, unit) pairs drawn and checked against the MOT differences tab", f"{len(bad)} failures"]
    return ret(bad, ev)


# --------------------------------------------------------------------------- #
# P4-22 text
# --------------------------------------------------------------------------- #
def forbidden() -> List[str]:
    from src.workbook.conf import forbidden_terms
    return forbidden_terms(ROOT)


def word_hits(text: str, terms: List[str]) -> List[str]:
    out = []
    for t in terms:
        for m in re.finditer(r"\b" + re.escape(t) + r"(s|es)?\b", text, 0 if t == "AI" else re.I):
            if t == "workflow" and re.match(r"\s*TRAINING", text[m.end():], re.I):
                continue
            out.append(t)
    return out


def long_sentences(text: str, limit: int = 25) -> int:
    n = 0
    for s in re.split(r"(?<=[.!?])\s+", text):
        if len(s.split()) > limit and not re.search(r"\d{3,}|[A-Z]{2,}-", s[:0]):
            n += 1
    return n


def check_p4_22_text(ctx) -> Tuple[str, List[str]]:
    import openpyxl
    terms = forbidden()
    hits: Counter = Counter()
    first: Dict[str, str] = {}

    def scan(src: str, text: str) -> None:
        for t in word_hits(text, terms):
            hits[(src, t)] += 1
            first.setdefault(f"{src}/{t}", text[:90])
    n_str = 0
    for r in changes():
        for k in ("action", "note", "item", "class", "source"):
            v = r.get(k, "")
            if v:
                n_str += 1
                scan("change list", v)
    wb = openpyxl.load_workbook(SCH / "schematic_update_register.xlsx", read_only=True)
    for ws in wb:
        for row in ws.iter_rows(values_only=True):
            for v in row:
                if isinstance(v, str):
                    n_str += 1
                    scan("update workbook", v)
    wb.close()
    for p in new_dxf_files():
        for t in dxf_texts(p):
            n_str += 1
            scan("DXF " + p.name[:11], t)
    for f in ("missing_systems.csv", "drawing_register.csv"):
        if (SCH / f).exists():
            for r in rcsv(SCH / f):
                for v in r.values():
                    scan(f, v or "")
    bad = [f"{src}: banned word {t!r} {n} times, for example {first.get(f'{src}/{t}', '')!r}" for (src, t), n in sorted(hits.items())]
    # sentence length of the notes and actions (structural lint; the ASD script cannot be fetched without network)
    longs = sum(long_sentences(r.get("note", "")) + long_sentences(r.get("action", "")) for r in changes())
    # no duplicate (unit, room, action, quantity) rows
    cnt: Counter = Counter()
    for r in changes():
        if r["action"].startswith(INSTRUCTION_PREFIXES) and r["item"]:
            cnt[(r["unit"], r["sheet"], r.get("room_no") or r["item"], r["action"], r["quantity"])] += 1
    dups = [k for k, c in cnt.items() if c > 1]
    if dups:
        bad.append(f"{len(dups)} duplicate (unit, room, action) rows: {sample([d[2] + ' / ' + d[3][:30] for d in dups])}")
    ev = [f"{n_str} strings of the change list, workbook and {len(new_dxf_files())} DXF files scanned for the D-015 word list ({len(terms)} terms)",
          f"structural lint (25 words per sentence): {longs} long sentences in notes and actions (information: the ASD-STE100 script of the public skill needs the network and is not run; rules applied by script: sentence length)",
          f"{len(bad)} failures"]
    return ret(bad, ev)


# --------------------------------------------------------------------------- #
# P4-09 (widened) change IDs over list, workbook, mark-up subjects and DXF
# --------------------------------------------------------------------------- #
def workbook_ids() -> Set[str]:
    def load():
        import openpyxl
        wb = openpyxl.load_workbook(SCH / "schematic_update_register.xlsx", read_only=True)
        ids: Set[str] = set()
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                for c in row:
                    if isinstance(c, str):
                        ids.update(ID_RE.findall(c))
        wb.close()
        return ids
    return memo("wb_ids", load)


def dxf_ids() -> Set[str]:
    ids: Set[str] = set()
    for p in new_dxf_files():
        for t in dxf_texts(p):
            ids.update(ID_RE.findall(t))
    return ids


def check_p4_09_ids(ctx) -> Tuple[str, List[str]]:
    reg = {r["change_id"] for r in changes() if r["change_id"]}
    wbi = workbook_ids()
    subj = markup_subjects()
    mids = {i for v in subj.values() for i in v if i != "(none)"}
    dids = dxf_ids()
    bad = []
    a, b = sorted(reg - wbi), sorted(wbi - reg)
    if a:
        bad.append(f"in the change list and not in the workbook: {sample(a)}")
    if b:
        bad.append(f"in the workbook and not in the change list: {sample(b)}")
    extra = sorted(mids - reg)
    if extra:
        bad.append(f"mark-up subjects with no change-list row: {sample(extra)}")
    xd = sorted(dids - reg)
    if xd:
        bad.append(f"IDs on new drawings (DXF) with no change-list row: {sample(xd)}")
    unmarked = {r["change_id"] for r in changes() if r["change_id"] and r["layer_type"] in ("rooms", "airflows") and r["action"].startswith("Mark up") and r["sheet"] in subj}
    miss = sorted(unmarked - mids)
    if miss:
        bad.append(f"{len(miss)} 'Mark up' rows on a marked-up sheet with no annotation subject: {sample(miss)}")
    ev = [f"{len(reg)} change IDs in the change list; {len(wbi)} in the update workbook; {len(mids)} as mark-up subjects ({len(subj)} PDFs); {len(dids)} on {len(new_dxf_files())} new drawings (DXF)"]
    return ret(bad, ev)


# --------------------------------------------------------------------------- #
# P4-23 one build
# --------------------------------------------------------------------------- #
def sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def check_p4_23_build(ctx) -> Tuple[str, List[str]]:
    bad: List[str] = []
    man_p = QA / "run_manifest.json"
    if not man_p.exists():
        return FAIL, ["out/qa/run_manifest.json does not exist"]
    man = json.loads(man_p.read_text())
    p4 = man.get("phase4")
    if not p4:
        return FAIL, ["run_manifest.json has no phase4 section"]
    files = p4.get("files", {})
    # hash per file of the staging folder and of the issue folder
    n_iss = 0
    if not ISSUE.exists():
        bad.append(f"{ISSUE} does not exist")
    else:
        for p in sorted(ISSUE.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(ISSUE).as_posix()
            n_iss += 1
            h = sha(p)
            want = files.get(rel)
            if want is None:
                bad.append(f"issue file {rel} has no hash in the manifest")
            elif want != h:
                bad.append(f"issue file {rel} differs from its manifest hash")
            sp = STAGING / rel
            if STAGING.exists() and (not sp.exists() or sha(sp) != h):
                bad.append(f"issue file {rel} differs from its staging source")
        for rel in files:
            if not (ISSUE / rel).exists():
                bad.append(f"manifest file {rel} is not in the issue folder")
    # no output older than its input
    ins = [REG / "schematic_model.json", REG / "schematic_equipment.csv", REG / "room_unit_links.csv", ZONE]
    outs = [SCH / "entity_ledger.csv", SCH / "change_register.csv", SCH / "schematic_update_register.xlsx"]
    old = []
    for o in outs:
        for i in ins:
            if i.exists() and o.exists() and o.stat().st_mtime + 1 < i.stat().st_mtime:
                old.append(f"{o.name} older than {i.name}")
    if old:
        bad.append("output older than its input: " + sample(old))
    # IDs: change list = workbook = mark-up subjects = DXF
    reg = {r["change_id"] for r in changes() if r["change_id"]}
    if reg != workbook_ids():
        bad.append(f"change IDs of the change list and the workbook differ ({len(reg ^ workbook_ids())})")
    mids = {i for v in markup_subjects().values() for i in v if i != "(none)"}
    if mids - reg:
        bad.append(f"{len(mids - reg)} mark-up subjects are not in the change list")
    if dxf_ids() - reg:
        bad.append(f"{len(dxf_ids() - reg)} IDs on the DXF files are not in the change list")
    # the persisted ledger: every ID of the change list is in it, none reused
    lp = SCH / "change_id_ledger.csv"
    led = rcsv(lp)
    ids = Counter(r["change_id"] for r in led)
    dup = [i for i, c in ids.items() if c > 1]
    if dup:
        bad.append(f"{len(dup)} change IDs twice in change_id_ledger.csv: {sample(dup)}")
    notin = sorted(reg - set(ids))
    if notin:
        bad.append(f"{len(notin)} change-list IDs not in change_id_ledger.csv: {sample(notin)}")
    retired = {r["change_id"] for r in led if r.get("status") == "retired"}
    reused = sorted(reg & retired)
    if reused:
        bad.append(f"{len(reused)} retired IDs used in the change list: {sample(reused)}")
    keys = Counter((r["sheet"], r["entity"], r["quantity"], r["unit"]) for r in led if r.get("status") != "retired")
    dk = [k for k, c in keys.items() if c > 1]
    if dk:
        bad.append(f"{len(dk)} keys (sheet, entity, quantity, unit) with two active IDs: {sample(dk, 2)}")
    ev = [f"manifest phase4 holds {len(files)} file hashes; {n_iss} issue files compared with the manifest and with staging; {len(led)} IDs in change_id_ledger.csv, {len(retired)} retired"]
    return ret(bad, ev)
