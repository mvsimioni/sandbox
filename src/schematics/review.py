"""Schematic review, system by system (brief Section 9.3, items 1 to 7).

Command line::

    python -m src.schematics.review run [--no-xlsx]

The review reads the schematic model, the zone schedule, the room links, the
department workbooks (cached values) and the equipment schedule.  It writes

* out/schematics/schematic_update_register.xlsx
* out/schematics/missing_systems.csv
* out/schematics/change_register.csv

No source drawing is read for writing and none is changed.  Every row keeps
the sheet, page and box of the drawn value.  Text in the outputs follows
ASD-STE100 (short sentences, no names of tools or internal processes).

Update rule for the Action column (Section 11 item 16, decisions D-054 and
D-072):

* the workbook needs MORE air than the drawing shows by more than 5 % of the
  drawn value or by more than 10 L/s: mark up the increase;
* the workbook needs LESS air: mark up a reduction only where the room list
  of the unit is complete (zone cover 0.9 to 1.1, second room list agrees,
  tender base consistent).  Otherwise the drawn value stays and the margin is
  recorded;
* sheets of the MOT NSC are reviewed and listed, never marked up.

Council C4 (D-077 to D-088) adds, in this file:

* the entity ledger (out/schematics/entity_ledger.csv): one row for every drawn entity of every
  working air-side sheet, each with exactly one disposition written by the review;
* one scope table (out/schematics/scope.csv): list-only units, fans and sheets;
* persistent change IDs (out/schematics/change_id_ledger.csv, change_id_map.csv);
* the input gate for unit-level rows (D-083), pressure rows as queries (D-081), fan duties by
  ownership with a duty/standby pair counted once (D-080), no add or missing row without a
  negative ledger lookup (D-078), one home per unit (D-079).
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import re
import shutil
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "out" / "schematics"

INC_PCT = 0.05          # increase threshold, share of the drawn value
INC_LPS = 10.0          # increase threshold, L/s
MIN_INC_LPS = 5.0       # an increase of this size or less is not marked up (readable draft, D-071)
RENAME_RATIO = 0.80     # name similarity that counts as "renamed"
BAL_PCT = 0.01          # balance tolerance, share
BAL_LPS = 10.0          # balance tolerance, L/s
LEAK_PCT = 0.10         # project duct leakage (the sheets print 15 %)
REVISION = "P1"

LAYERS = ("rooms", "airflows", "fans", "new systems", "queries")

# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
TAG_RE = re.compile(
    r"(?:PAHU|AHU|FCU|CRAC|OAF|EAF|TEAF|IEAF|RAF|TEF|HPF|KEAF|KMAF)-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?:/[A-Z0-9]+)*")
LEDGER_TAG_RE = re.compile(
    r"\b(?:PAHU|AHU|FCU|CRAC|OAF|EAF|TEAF|IEAF|RAF|TEF|HPF|KEAF|KMAF|BIBO)-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?:/[A-Z0-9]+)*")
FAN_PREFIXES = ("RAF", "EAF", "TEAF", "IEAF", "OAF", "TEF", "HPF", "KEAF", "KMAF")
GATE_LO, GATE_HI = 0.90, 1.10        # counted area / zoned area (D-054 band, D-083)
PRESSURE_MAX = 60.0                  # |Pa| above this is queried as a possible misread (D-081)
BUILD_ID = "C4"
SUPPRESSED_CLASS = "increase of 5 L/s or less, not marked up"
RATIO_MAX = 2.0                      # protective: a unit-level or fan increase above this ratio is a query, not an instruction


def num(x: Any) -> Optional[float]:
    if x is None or x == "":
        return None
    if isinstance(x, (int, float)):
        return float(x)
    m = re.search(r"-?\d+(?:\.\d+)?", str(x).replace(",", ""))
    return float(m.group(0)) if m else None


def fmt(v: Any, nd: int = 0) -> str:
    if v is None or v == "":
        return "not drawn"
    if isinstance(v, (int, float)):
        return f"{v:,.{nd}f}"
    return str(v)


def tag_keys(text: str) -> frozenset:
    """Set of 'TYPE:key' for every unit tag in a string (A/B letters removed).

    'AHU-B-05-05CORE-01A/B' gives {'AHU:B-05-05CORE-01'}; 'AHU-B-07-04ICU-05/06'
    gives two keys.  Types: AHU, PAHU, FCU, CRAC; fans keep their own prefix.
    """
    out = set()
    for t in TAG_RE.findall(text or ""):
        pre, _, rest = t.partition("-")
        pair = re.search(r"^(.*-)(\d+(?:/\d+)+)[A-Z]?$", rest)
        if pair:
            for n in pair.group(2).split("/"):
                out.add(f"{pre}:{pair.group(1)}{n}")
            continue
        rest = re.sub(r"(?<=\d)(?:/[A-Z])+$|(?<=\d)[A-Z](?:/[A-Z])*$", "", rest)
        out.add(f"{pre}:{rest}")
    return frozenset(out)


def key_only(k: str) -> str:
    return k.split(":", 1)[1]


def nname(s: Any) -> str:
    t = str(s or "").upper().replace("&", " AND ")
    t = re.sub(r"[^A-Z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_CLEAN = [
    (re.compile(r"council ruling D-\d+(?:\s*/\s*D-\d+)*"), "zoning rule"),
    (re.compile(r"zone_assignment\w*(?:\.csv)?"), "zone list"),
    (re.compile(r"ruling D-\d{3}"), "zoning rule"),
    (re.compile(r"[;,]?\s*earlier method: [^;)]*"), ""),
    (re.compile(r"[;,]?\s*link by [a-z0-9 ,\-]+?(?=;|,\s*boxes|\.|$)"), ""),
    (re.compile(r"[;,]?\s*boxes of units that [^;.]*"), ""),
    (re.compile(r"[;,]?\s*zone list class\b"), ""),
    (re.compile(r"\bLINK-[A-Z0-9]+\b"), ""),
    (re.compile(r"\s*Similarity\s+\d(?:\.\d+)?\.?"), ""),
    (re.compile(r"\(tender\)"), "(tender issue)"),
    (re.compile(r"\(working\)"), "(working drawing)"),
    (re.compile(r"\(mot_nsc\)"), "(MOT NSC drawing)"),
    (re.compile(r"\bcomposite box\b"), "box with several room names"),
    (re.compile(r"\b\d{8}_[A-Za-z0-9_ \-]+?\.pdf\b[^;]*?zone [A-Za-z0-9\-]+-p(\d+)-Z\d+"), r"DD Booklet, page \1"),
    (re.compile(r"\b\d{8}_[A-Za-z0-9_ \-]+?\.pdf"), "DD Booklet"),
    (re.compile(r"\bzone [A-Z0-9]{1,3}-\d{8}[A-Za-z0-9]*-p(\d+)-Z\d+"), r"booklet zone, page \1"),
    (re.compile(r"\bD-\d{3}\b"), ""),
    (re.compile(r"\bregister\b", re.I), "list"),
    (re.compile(r"\bmethod\b"), "basis"),
    (re.compile(r"\bheuristic\b"), "rule"),
    (re.compile(r"\bparser\b"), "reader"),
]


def clean(text: Any) -> Any:
    """Remove names of internal processes from text copied out of the inputs."""
    if not isinstance(text, str):
        return text
    for rx, rep in _CLEAN:
        text = rx.sub(rep, text)
    text = re.sub(r"\s+([;,.])", r"\1", text)
    text = re.sub(r"([;,])\s*([;,.])", r"\2", text)
    return re.sub(r"\s{2,}", " ", text).strip(" ;,")


_PREFIX_TOK = re.compile(r"^[PL]\d$")
_ABBR = {"CORR": "CIRCULATION", "CORRIDOR": "CIRCULATION", "REPORT": "REPORTING", "RECEP": "RECEPTION",
         "WAIT": "WAITING", "STOR": "STORE", "CONS": "CONSULT", "OFF": "OFFICE"}


def canon(s: Any) -> str:
    """Name key: upper case, no prefix such as P3, no one-word bracket such as (CATH), '&' as AND."""
    t = str(s or "").upper().replace("&", " AND ")
    t = re.sub(r"\(\s*[A-Z0-9]{1,6}\s*\)", " ", t)
    t = re.sub(r"[^A-Z0-9 ]+", " ", t)
    toks = [w for w in t.split() if not _PREFIX_TOK.match(w)]
    return " ".join(toks)


def _tok_match(a: str, b: str) -> bool:
    """Plan label token against a drawn word: same word, a prefix, or an abbreviation (STR for STORE)."""
    if a == b or b.startswith(a):
        return True
    if len(a) >= 3 and a[0] == b[0]:
        it = iter(b)
        return all(c in it for c in a)
    return False


def plan_matches(plan_name: str, drawn_name: str) -> bool:
    """Every word of the short plan label is a word, a prefix or an abbreviation of a word of the drawn name, in order."""
    pw = [w for w in canon(plan_name).split() if w not in ("AND", "THE", "OF")]
    dw = [_ABBR.get(w, w) for w in canon(drawn_name).split()]
    if not pw or not dw:
        return False
    pw = [_ABBR.get(w, w) for w in pw]
    i = 0
    for w in pw:
        while i < len(dw) and not _tok_match(w, dw[i]):
            i += 1
        if i == len(dw):
            return False
        i += 1
    return True


def is_composite(s: Any) -> bool:
    t = str(s or "")
    return "/" in t or " , " in t or t.count(",") >= 1


def name_tokens(s: Any) -> frozenset:
    return frozenset(_ABBR.get(w, w) for w in canon(s).split()) - {"AND", "THE", "OF", "ROOM"}


def short(text: str, n: int) -> str:
    """Cut at a word end, never inside a word."""
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0].rstrip(" ,;.")
    return cut + " ..."


def box_s(b: Any) -> str:
    if isinstance(b, str):
        b = jbox(b)
    if not b:
        return ""
    return "[" + ", ".join(f"{float(x):.1f}" for x in b) + "]"


def cell_addr(col0: int, row: int) -> str:
    from openpyxl.utils import get_column_letter
    return f"{get_column_letter(col0 + 1)}{row}"


def judge(drawn: Optional[float], req: Optional[float], d054: bool) -> Tuple[str, str, bool]:
    """Class, action and 'needs change ID' for one airflow value (update rule).  The input gate (D-083) is
    applied by the caller before this function."""
    if req is None:
        return "not established", "Query: required value not established", True
    if drawn is None:
        if req > 0.5:
            return "not drawn", "Mark up: show the required value", True
        return "no change", "No change", False
    diff = req - drawn
    big = abs(diff) > INC_PCT * abs(drawn) or abs(diff) > INC_LPS
    if diff > 0 and big:
        if diff <= MIN_INC_LPS:
            return SUPPRESSED_CLASS, "No mark-up: increase of 5 L/s or less; drawn value kept", False
        return "increase", "Mark up: increase", True
    if diff < 0 and big:
        if d054:
            return "reduction", "Mark up: reduction", True
        return "margin", "Keep drawn value; margin recorded", False
    return "no change", "No change", False


def read_csv(path: Path) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def jbox(text: Any) -> Optional[List[float]]:
    if not text:
        return None
    try:
        v = json.loads(text) if isinstance(text, str) else list(text)
        return [round(float(x), 1) for x in v] if len(v) == 4 else None
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Text of a working sheet (page-view boxes), read once
# --------------------------------------------------------------------------- #
class SheetText:
    """Text lines of the working sheets.  The model holds the label that lists the outdoor air of each AHU;
    the drawn value is a separate line.  This reader finds the drawn lines (outdoor air, return fans, spill
    labels, exhaust links) so that a mark-up sits on the value it changes."""

    def __init__(self, root: Path):
        self.dir = root / "data" / "source" / "Basis" / "Working Drawings"
        self._c: Dict[str, List[Tuple[str, List[float]]]] = {}

    def lines(self, sheet: str) -> List[Tuple[str, List[float]]]:
        if sheet in self._c:
            return self._c[sheet]
        out: List[Tuple[str, List[float]]] = []
        hits = sorted(self.dir.glob(sheet + "_*.pdf"))
        if hits:
            import pymupdf
            doc = pymupdf.open(hits[0])
            try:
                for b in doc[0].get_text("dict")["blocks"]:
                    for ln in b.get("lines", []):
                        t = " ".join(sp["text"].strip() for sp in ln["spans"] if sp["text"].strip())
                        if t:
                            out.append((t, [round(float(x), 1) for x in ln["bbox"]]))
            finally:
                doc.close()
        self._c[sheet] = out
        return out

    def words(self, sheet: str) -> List[Tuple[str, List[float]]]:
        """Words of the page with their boxes (for a value inside a longer line)."""
        key = "w:" + sheet
        if key in self._c:
            return self._c[key]
        out: List[Tuple[str, List[float]]] = []
        hits = sorted(self.dir.glob(sheet + "_*.pdf"))
        if hits:
            import pymupdf
            doc = pymupdf.open(hits[0])
            try:
                for w in doc[0].get_text("words"):
                    out.append((w[4], [round(float(x), 1) for x in w[:4]]))
            finally:
                doc.close()
        self._c[key] = out
        return out

    def oa_value_boxes(self, sheet: str, value: float) -> List[Tuple[List[float], str]]:
        """Boxes of the drawn outdoor-air value 'OA nnnn L/s': a line of its own (duct label) or the value inside a
        longer line (note).  Returns (box, kind) with kind 'duct label' or 'note line'."""
        out: List[Tuple[List[float], str]] = []
        seen = set()
        for t, b in self.lines(sheet):
            m = re.match(r"^OA\s*([\d,]+(?:\.\d+)?)\s*(?:L/S)?$", t, re.I)
            if m and abs(float(m.group(1).replace(",", "")) - value) < 0.5:
                out.append((b, "duct label"))
                seen.add(tuple(b))
        ws = self.words(sheet)
        for i, (w, b) in enumerate(ws):
            if w.upper() != "OA" or i + 1 >= len(ws):
                continue
            nxt, nb = ws[i + 1]
            if re.fullmatch(r"[\d,]+(?:\.\d+)?", nxt) and abs(float(nxt.replace(",", "")) - value) < 0.5:
                x0, y0 = min(b[0], nb[0]), min(b[1], nb[1])
                x1, y1 = max(b[2], nb[2]), max(b[3], nb[3])
                if i + 2 < len(ws) and ws[i + 2][0].upper().startswith("L/S"):
                    x1, y1 = max(x1, ws[i + 2][1][2]), max(y1, ws[i + 2][1][3])
                box = [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)]
                if tuple(box) in seen or any(_overlap(box, list(k)) > 0.6 for k in seen):
                    continue
                seen.add(tuple(box))
                out.append((box, "note line"))
        return out

    @staticmethod
    def _dist(a: List[float], b: Optional[List[float]]) -> float:
        if not b:
            return 0.0
        ax, ay, bx, by = (a[0] + a[2]) / 2, (a[1] + a[3]) / 2, (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5

    def oa_box(self, sheet: str, value: Optional[float], near: Optional[List[float]]) -> Optional[List[float]]:
        """Box of the drawn line 'OA nnn' with this value, the one nearest to the AHU tag."""
        if value is None:
            return None
        c = []
        for t, b in self.lines(sheet):
            m = re.match(r"^OA\s*([\d,]+(?:\.\d+)?)\s*(?:L/S)?$", t, re.I)
            if m and abs(float(m.group(1).replace(",", "")) - value) < 0.5:
                c.append(b)
        if not c:
            return None
        return min(c, key=lambda b: self._dist(b, near))

    def raf_lines(self, sheet: str) -> List[Tuple[str, List[float]]]:
        return [(t, b) for t, b in self.lines(sheet) if re.match(r"^RAF-[A-Z0-9]", t)]

    def duty_near(self, sheet: str, box: List[float]) -> Optional[Tuple[float, List[float]]]:
        """Value 'nnnn L/s' on a line directly under or beside a tag box."""
        best = None
        for t, b in self.lines(sheet):
            m = re.match(r"^(?:[A-Z]{2,4}\s*)?([\d,]{2,7})\s*L/S(?:\s*\(.*\))?$", t, re.I)
            if not m:
                continue
            dx = abs((b[0] + b[2]) / 2 - (box[0] + box[2]) / 2)
            dy = b[1] - box[3]
            if -3 <= dy <= 40 and dx <= 70:
                d = dx + abs(dy)
                if best is None or d < best[0]:
                    best = (d, float(m.group(1).replace(",", "")), b)
        return (best[1], best[2]) if best else None

    def spill_labels(self, sheet: str) -> List[Tuple[Optional[float], List[float]]]:
        out = []
        for t, b in self.lines(sheet):
            m = re.match(r"^SPILL AIR\s*([\d,]+(?:\.\d+)?|-)\s*L/S$", t, re.I)
            if m:
                v = m.group(1)
                out.append((None if v == "-" else float(v.replace(",", "")), b))
        return out

    def exhaust_links(self, sheet: str) -> List[Tuple[str, List[float]]]:
        """'CONNECTED TO ... EA nnnn' and similar lines that draw an exhaust connection."""
        return [(t, b) for t, b in self.lines(sheet) if re.search(r"CONNECTED TO", t, re.I) and re.search(r"\bEA\b|EAF|HEPA", t, re.I)]

    def has_tag(self, sheet: str, key_text: str) -> Optional[List[float]]:
        for t, b in self.lines(sheet):
            if key_text in t.replace(" ", ""):
                return b
        return None


# --------------------------------------------------------------------------- #
# Entity ledger (D-077): one row for every drawn entity of every working air-side sheet
# --------------------------------------------------------------------------- #
LEDGER_COLS = ["entity_id", "sheet", "sheet_role", "page", "bbox", "kind", "function", "text", "value", "uom",
               "tag_keys", "system", "sister_sheets", "parent_id", "quantity", "unit", "disposition",
               "disposition_class", "disposition_unit", "note"]
KIND_ORDER = {"AHU tag": 0, "PAHU tag": 1, "FCU tag": 2, "fan tag": 3, "BIBO tag": 4, "duty": 5, "OA value": 6,
              "spill or relief label": 7, "exhaust link": 8, "room box": 9, "room value": 10, "note": 11,
              "overall tag": 12}


def _jb(b: Optional[Sequence[float]]) -> str:
    return json.dumps([round(float(x), 1) for x in b]) if b else ""


def _overlap(a: Sequence[float], b: Sequence[float]) -> float:
    """Share of the smaller box that lies inside the other."""
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    sm = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return inter / sm if sm > 0 else 0.0


def tag_function(tag: str) -> str:
    pre = tag.split("-")[0]
    return pre if pre in ("RAF", "EAF", "TEAF", "IEAF") else "other"


def tag_kind(tag: str) -> str:
    pre = tag.split("-")[0]
    if pre == "PAHU":
        return "PAHU tag"
    if pre in ("AHU", "CRAC"):
        return "AHU tag"
    if pre == "FCU":
        return "FCU tag"
    if pre == "BIBO":
        return "BIBO tag"
    return "fan tag"


class EntityLedger:
    """The entity ledger.  Built from the schematic model, schematic_equipment.csv (working issue) and the text of
    the sheets.  The review writes exactly one disposition on every row (D-077)."""

    def __init__(self, inp: "Inputs"):
        self.i = inp
        self.rows: List[Dict[str, Any]] = []
        self.by_id: Dict[str, Dict[str, Any]] = {}
        self._build()

    # -- helpers -------------------------------------------------------------- #
    def _add(self, sheet: str, kind: str, bbox: Optional[Sequence[float]], text: str, **kw: Any) -> Dict[str, Any]:
        r = {c: "" for c in LEDGER_COLS}
        r.update(sheet=sheet, sheet_role=kw.pop("sheet_role", "working"), page=kw.pop("page", 1), bbox=_jb(bbox),
                 kind=kind, text=text)
        for k, v in kw.items():
            r[k] = v
        r["_box"] = [round(float(x), 1) for x in bbox] if bbox else None
        self.rows.append(r)
        return r

    def _build(self) -> None:
        i = self.i
        model_sheets = sorted(i.model["sheets"])
        eq: Dict[Tuple[str, str], List[Dict[str, str]]] = defaultdict(list)
        for e in i.equip:
            if e.get("issue_set") == "working":
                eq[(e["sheet"], re.sub(r"\s+", "", e["tag"]))].append(e)
        sys_by_sheet: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for s in sorted((x for x in i.systems if x.get("source_set") == "working"),
                        key=lambda x: x["kind"] == "working_unassigned_rooms"):
            sys_by_sheet[s["sheet"]].append(s)
        for sh in model_sheets:
            lines = i.st.lines(sh)
            row0 = len(self.rows)
            # 1. tags
            for t, b in lines:
                for m in LEDGER_TAG_RE.finditer(t):
                    tg = re.sub(r"\s+", "", m.group(0))
                    kind = tag_kind(tg)
                    r = self._add(sh, kind, b, t if len(t) < 160 else m.group(0),
                                  function=tag_function(tg) if kind == "fan tag" else "",
                                  tag_keys="; ".join(sorted(tag_keys(tg))), quantity="")
                    r["_tag"] = tg
                    # duty from the equipment list, where the line is the tag box of that row
                    for e in eq.get((sh, tg), []):
                        eb = jbox(e["box_page_view_tag"])
                        if eb and _overlap(b, eb) >= 0.5 and kind != "PAHU tag" or (eb and _overlap(b, eb) >= 0.5):
                            r["_equip"] = e
                            r["value"] = num(e["duty_as_written"]) if e["duty_as_written"] else ""
                            r["uom"] = "L/s" if r["value"] != "" else ""
                            dbx = jbox(e["box_page_view_duty"])
                            if dbx and r["value"] != "":
                                r["_dutybox"] = dbx
                            break
            # 2. outdoor-air values, spill labels, exhaust links, notes
            seen_oa = set()
            for t, b in lines:
                tu = t.upper()
                m = re.match(r"^OA\s*([\d,]+(?:\.\d+)?)\s*(?:L/S)?$", t, re.I)
                if m:
                    v = float(m.group(1).replace(",", ""))
                    self._add(sh, "OA value", b, t, value=v, uom="L/s", function="duct label")
                    seen_oa.add(tuple(b))
                    continue
                m = re.match(r"^SPILL AIR\s*([\d,]+(?:\.\d+)?|-)\s*L/S$", t, re.I)
                if m:
                    v = m.group(1)
                    self._add(sh, "spill or relief label", b, t, value="" if v == "-" else float(v.replace(",", "")),
                              uom="L/s", note="drawn path, value missing" if v == "-" else "")
                    continue
                if "SPILL AIR" in tu or ("RELIEF" in tu and "SHALL" in tu):
                    self._add(sh, "note", b, t, note="general note, not a path")
                    continue
                if re.search(r"CONNECTED TO", t, re.I) and re.search(r"\bEA\b|EAF|HEPA|BIBO", t, re.I):
                    self._add(sh, "exhaust link", b, t)
                    continue
                m = re.match(r"^IEA\s*([\d,]+(?:\.\d+)?)\s*L/S$", t, re.I)
                if m:
                    self._add(sh, "exhaust link", b, t, value=float(m.group(1).replace(",", "")), uom="L/s",
                              function="IEA label")
            # OA values inside a longer line: word level
            ws = i.st.words(sh)
            for k, (w, wb_) in enumerate(ws):
                if w.upper() != "OA" or k + 1 >= len(ws):
                    continue
                nxt, nb = ws[k + 1]
                if not re.fullmatch(r"[\d,]+(?:\.\d+)?", nxt):
                    continue
                x1 = max(wb_[2], nb[2])
                y1 = max(wb_[3], nb[3])
                if k + 2 < len(ws) and ws[k + 2][0].upper().startswith("L/S"):
                    x1, y1 = max(x1, ws[k + 2][1][2]), max(y1, ws[k + 2][1][3])
                box = [min(wb_[0], nb[0]), min(wb_[1], nb[1]), x1, y1]
                if any(_overlap(box, list(o)) > 0.6 for o in seen_oa):
                    continue
                seen_oa.add(tuple(box))
                self._add(sh, "OA value", box, "OA " + nxt + " L/s", value=float(nxt.replace(",", "")), uom="L/s",
                          function="note line")
            # 3. rooms and room values
            for s in sys_by_sheet.get(sh, []):
                for rm in s["rooms"]:
                    nb = rm["name"].get("box")
                    key = (sh, tuple(nb or ()))
                    if key in getattr(self, "_roomseen", set()):
                        continue
                    self._roomseen = getattr(self, "_roomseen", set()) | {key}
                    nm = rm["name"]["value"]
                    md = next((m_ for m_ in rm["modes"] if m_["mode"] == "NORM"), rm["modes"][0] if rm["modes"] else None)
                    rr = self._add(sh, "room box", nb, nm, value=int(rm.get("typical_count") or 1), uom="no.",
                                   system=s["system_id"], note="typical count")
                    rr["_rm"] = rm
                    rr["_sys"] = s
                    for q, key_, uom in (("supply", "supply_L_s", "L/s"), ("return", "return_L_s", "L/s"),
                                         ("exhaust", "exhaust_L_s", "L/s")):
                        dv = (md or {}).get(key_) or {}
                        if dv.get("value") is not None and dv.get("box"):
                            v_ = self._add(sh, "room value", dv["box"], dv.get("as_written") or str(dv["value"]),
                                           value=dv["value"], uom=uom, quantity=q, parent_id="", system=s["system_id"])
                            v_["_parent"] = rr
                    pr = rm.get("pressure") or {}
                    if pr.get("value") not in (None, "") and pr.get("box"):
                        pv = parse_pressure(pr["value"])
                        v_ = self._add(sh, "room value", pr["box"], pr.get("as_written") or str(pr["value"]),
                                       value=pv if pv is not None else "", uom="Pa", quantity="pressure", system=s["system_id"])
                        v_["_parent"] = rr
            # an outdoor-air line inside a room table is the room's own value, not the AHU outdoor air
            tbls = [rm["box_table"] for s_ in sys_by_sheet.get(sh, []) for rm in s_["rooms"] if rm.get("box_table")]
            for r_ in self.rows[row0:]:
                if r_["kind"] == "OA value" and r_["_box"] and any(_overlap(r_["_box"], t) > 0.8 for t in tbls):
                    r_["function"] = "room (inside the room table)"
        # ids, sorted per sheet
        self.rows.sort(key=lambda r: (r["sheet"], KIND_ORDER.get(r["kind"], 20), (r["_box"] or [0, 0])[1], (r["_box"] or [0, 0])[0],
                                      r["text"]))
        counters: Counter = Counter()
        for r in self.rows:
            code = re.sub(r"^IP-BH-", "", r["sheet"])
            counters[code] += 1
            r["entity_id"] = f"E-{code}-{counters[code]:04d}"
            self.by_id[r["entity_id"]] = r
        for r in self.rows:
            if "_parent" in r:
                r["parent_id"] = r["_parent"]["entity_id"]
        # duty rows
        extra = []
        for r in self.rows:
            if r.get("_dutybox"):
                extra.append((r, r["_dutybox"]))
        for r, db in extra:
            d = self._add(r["sheet"], "duty", db, (r["_equip"]["duty_as_written"] or ""), value=r["value"], uom="L/s",
                          parent_id=r["entity_id"], tag_keys=r["tag_keys"], function=r["function"])
            d["_parent"] = r
        self.rows.sort(key=lambda r: (r["sheet"], KIND_ORDER.get(r["kind"], 20), (r["_box"] or [0, 0])[1], (r["_box"] or [0, 0])[0],
                                      r["text"]))
        counters.clear()
        for r in self.rows:
            code = re.sub(r"^IP-BH-", "", r["sheet"])
            counters[code] += 1
            r["entity_id"] = f"E-{code}-{counters[code]:04d}"
        self.by_id = {r["entity_id"]: r for r in self.rows}
        for r in self.rows:
            if "_parent" in r:
                r["parent_id"] = r["_parent"]["entity_id"]
        self._index()

    def _index(self) -> None:
        self.by_sheet: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.by_key: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for r in self.rows:
            self.by_sheet[r["sheet"]].append(r)
            for k in filter(None, [x.strip() for x in r["tag_keys"].split(";")]):
                self.by_key[k].append(r)

    def add_overall(self, tags: Dict[str, List[Dict[str, Any]]]) -> None:
        for sh, lst in tags.items():
            for t in lst:
                self._add(sh, "overall tag", t["box"], t["tag"], sheet_role="overall",
                          tag_keys="; ".join(sorted(tag_keys(t["tag"]))))
        self.rows.sort(key=lambda r: (r["sheet"], KIND_ORDER.get(r["kind"], 20), (r["_box"] or [0, 0])[1], (r["_box"] or [0, 0])[0],
                                      r["text"]))
        counters: Counter = Counter()
        for r in self.rows:
            if r["entity_id"] and r["sheet_role"] == "working":
                pass
        # overall rows get their own id series; working ids are not renumbered
        n_over: Counter = Counter()
        for r in self.rows:
            if not r["entity_id"]:
                code = re.sub(r"^IP-BH-", "", r["sheet"])
                n_over[code] += 1
                r["entity_id"] = f"E-{code}-{n_over[code]:04d}"
        self.by_id = {r["entity_id"]: r for r in self.rows}
        self._index()

    # -- lookups ---------------------------------------------------------------- #
    def tag_rows(self, keys: Iterable[str], kinds: Sequence[str] = ("AHU tag", "PAHU tag", "FCU tag", "fan tag", "BIBO tag"),
                 roles: Sequence[str] = ("working",)) -> List[Dict[str, Any]]:
        out, seen = [], set()
        for k in keys:
            for r in self.by_key.get(k, []):
                if r["kind"] in kinds and r["sheet_role"] in roles and r["entity_id"] not in seen:
                    seen.add(r["entity_id"])
                    out.append(r)
        return out

    def sheets_of_keys(self, keys: Iterable[str]) -> List[str]:
        return sorted({r["sheet"] for r in self.tag_rows(keys)})

    def dispose(self, r: Dict[str, Any], text: str, unit: str = "", cls: str = "") -> bool:
        """Write the one disposition of a row.  Returns False if the row already had a different one."""
        if r["disposition"]:
            return r["disposition"] == text and r["disposition_unit"] == unit
        r["disposition"], r["disposition_unit"], r["disposition_class"] = text, unit, cls
        return True

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(LEDGER_COLS)
            for r in self.rows:
                w.writerow([r.get(c, "") if not isinstance(r.get(c, ""), float) else round(r[c], 3) for c in LEDGER_COLS])


# --------------------------------------------------------------------------- #
# Row builder
# --------------------------------------------------------------------------- #
class Row(dict):
    """One review row.  Keys are the columns of the change and unit sheets."""


EXTRA_COLS = ["ledger_id", "ledger_ids", "indicative", "cover", "rooms_counted", "rooms_zoned", "oa_room", "oa_fraction",
              "oa_room_sum", "room_no", "dept", "link_type", "supply", "return", "exhaust", "pressure", "mode", "terminal",
              "group_id"]


def mk(unit: str, layer: str, rtype: str, item: str, qty: str, uom: str,
       drawn: Any, req: Any, cls: str, action: str, needs_id: bool,
       sheet: str = "", page: Any = "", box: Any = None, source: str = "",
       note: str = "", **extra: Any) -> Row:
    d = num(drawn) if not isinstance(drawn, str) else None
    r = num(req) if not isinstance(req, str) else None
    diff = (r - d) if (d is not None and r is not None) else None
    pct = (diff / d) if (diff is not None and d) else None
    row = Row(unit=unit, layer=layer, row_type=rtype, item=item, quantity=qty, uom=uom,
              drawn=drawn, required=req, diff=diff, diff_pct=pct, cls=cls, action=action,
              needs_id=needs_id, change_id="", sheet=sheet, page=page, box=box_s(box) if box else "",
              source=clean(source), note=clean(note))
    for c in EXTRA_COLS:
        row[c] = extra.pop(c, "")
    if extra:
        raise TypeError("unknown row field " + ", ".join(extra))
    return row


# --------------------------------------------------------------------------- #
# Loading the inputs
# --------------------------------------------------------------------------- #
class Inputs:
    """Everything the review reads, loaded once."""

    def __init__(self, root: Path = ROOT):
        self.root = root
        reg = root / "out" / "registers"
        self.reg = reg
        model = json.load(open(reg / "schematic_model.json"))
        self.model = model
        self.systems = model["systems"]
        self.working = [s for s in self.systems if s["kind"] == "working_ahu_system"]
        self.unassigned = [s for s in self.systems if s["kind"] == "working_unassigned_rooms"]
        self.appd = [s for s in self.systems if s["source_set"] == "appd"]
        self.units = self._load_units()
        self.links = self._load_links()
        self.master = self._load_master()
        self.wb = self._load_workbooks()
        self.schedule = self._load_schedule()
        self.equip = read_csv(reg / "schematic_equipment.csv")
        self.plan_rooms = self._load_plan_rooms()
        self.mot_keys = self._load_mot_keys()
        self.st = SheetText(root)
        # sheets titled MOT ACMV: their units are MOT units (D-074, provisional: listed, not marked up)
        self.mot_sheets = frozenset(sh for sh, v in model.get("sheets", {}).items()
                                    if "MOT ACMV" in str(v.get("drive_rel_path") or ""))
        self.tender_summary = {r["sheet"]: r for r in read_csv(root / "out/qa/schematic_tender_vs_working_summary.csv")} \
            if (root / "out/qa/schematic_tender_vs_working_summary.csv").exists() else {}

    def _load_plan_rooms(self) -> Dict[str, List[Dict[str, str]]]:
        """Plan rooms of the zone assignment (one list per class): short names such as 'Report' or 'Corr'."""
        out: Dict[str, List[Dict[str, str]]] = defaultdict(list)
        for p in sorted(self.reg.glob("zone_assignment_*.csv")):
            for r in read_csv(p):
                cl = (r.get("class") or "").strip()
                if cl and r.get("class_kind") in ("AHU", "AHU group", "FCU"):
                    out[cl].append(r)
        return out

    def _load_mot_keys(self) -> frozenset:
        ks = set()
        for r in read_csv(self.reg / "mot_ahu_schedule.csv"):
            ks |= set(tag_keys(r["tag"]))
        for e in self.equip:
            if e.get("issue_set") == "mot_nsc":
                ks |= set(tag_keys(e["tag"]))
        return frozenset(ks)

    # -- zone schedule ------------------------------------------------------ #
    def _load_units(self) -> List[Dict[str, Any]]:
        import openpyxl
        wb = openpyxl.load_workbook(self.root / "out/zoning/zone_schedule.xlsx", data_only=True, read_only=True)
        ws = wb["AHU summary"]
        rows = list(ws.iter_rows(values_only=True))
        head = rows[0]
        units = []
        for i, r in enumerate(rows[1:], start=2):
            if not r[0]:
                continue
            d = dict(zip(head, r))
            tag = str(d["AHU or FCU tag"])
            units.append({
                "tag": tag, "type": d["Type"], "depts": d["Departments"], "storeys": d["Storeys served"],
                "plant": d["Plant level"], "rooms": d["Rooms"], "area": d["Area (m²)"],
                "tender": d["Tender airflow (L/s)"], "tender_src": d["Tender airflow source"],
                "calc": d["Calculated airflow (L/s)"], "reduction": d["Reduction (L/s)"],
                "area_counted": num(d.get("Area counted in the department workbooks (m²)")),
                "room_list": str(d["Room list of the unit"] or ""),
                "complete": str(d["Room list of the unit"] or "").startswith("Complete"),
                "keys": tag_keys(tag), "row": i,
                "has_tag": bool(TAG_RE.search(tag)),
                "source": f"Zone schedule, tab AHU summary, row {i}",
            })
        return units

    # -- room links ----------------------------------------------------------#
    def _load_links(self) -> List[Dict[str, str]]:
        return read_csv(self.reg / "room_unit_links.csv")

    # -- master summary ------------------------------------------------------#
    def _load_master(self) -> Dict[str, Dict[str, Any]]:
        import openpyxl
        wb = openpyxl.load_workbook(self.root / "out/loads/workbooks/AHU_master_summary.xlsx",
                                    data_only=True, read_only=True)
        ws = wb["AHU Master"]
        rows = list(ws.iter_rows(values_only=True))
        head = rows[4]
        out = {}
        for i, r in enumerate(rows[6:], start=7):
            if not r[0]:
                continue
            d = dict(zip(head, r))
            d["_row"] = i
            hc: Dict[str, str] = {}
            for j, h in enumerate(head):
                if h and h not in hc:
                    hc[h] = cell_addr(j, i)
            d["_cells"] = hc
            out[str(r[0])] = d
        return out

    # -- department workbooks ------------------------------------------------#
    @staticmethod
    def wb_table(rows: List[Tuple[Any, ...]], what: str, head_idx: int = 4, first: int = 6,
                 key_col: int = 0) -> Tuple[Dict[str, int], List[Tuple[int, Tuple[Any, ...]]]]:
        """One workbook table, read by its heading row.  The table ends at the first blank key or at the next
        table heading (a row with a text in the first cell and nothing else, such as 'SUITE TOTALS').
        The first heading with a given text keeps its column (a later column of the same name is ignored).
        A duplicate key is an error: nothing is overwritten (D-081)."""
        head: Dict[str, int] = {}
        for i, h in enumerate(rows[head_idx]):
            if h and h not in head:
                head[h] = i
        out: List[Tuple[int, Tuple[Any, ...]]] = []
        seen: Dict[str, int] = {}
        for n, r in enumerate(rows[first:], start=first + 1):
            k = r[key_col] if len(r) > key_col else None
            if k is None or str(k).strip() == "":
                break
            if isinstance(k, str) and all(x is None for j, x in enumerate(r) if j != key_col):
                break
            ks = str(k)
            if ks in seen:
                raise ValueError(f"{what}: duplicate key {ks!r} in rows {seen[ks]} and {n}")
            seen[ks] = n
            out.append((n, r))
        return head, out

    def _load_workbooks(self) -> Dict[str, Dict[str, Any]]:
        import openpyxl
        res: Dict[str, Dict[str, Any]] = {}
        self.wb_problems: List[str] = []
        for p in sorted((self.root / "out/loads/workbooks").glob("AIGH_C2_D*.xlsx")):
            m = re.search(r"_D(\d\d)_", p.name)
            if not m:
                continue
            dept = m.group(1)
            wb = openpyxl.load_workbook(p, data_only=True, read_only=True)
            d: Dict[str, Any] = {"file": p.name, "airflow": {}, "ahu": [], "fans": [], "fcu": [], "pressure": {}}
            # Airflow: one row per room
            rows = list(wb["Airflow"].iter_rows(values_only=True))
            head, body = self.wb_table(rows, f"D{dept} Airflow")
            for n, r in body:
                g = lambda k: r[head[k]] if k in head else None  # noqa: E731
                d["airflow"][str(r[0])] = {
                    "row": n, "name": g("Room name"), "storey": g("Storey"), "ahu": g("AHU or system"),
                    "supply": num(g("Final supply")), "extract": num(g("Total extract")),
                    "exhaust": num(g("Exhaust")), "return": num(g("Return")), "oa": num(g("Outdoor air")),
                    "oa_fraction": num(g("Outdoor-air fraction")),
                    "offset": num(g("Offset needed")), "offset_kind": g("Pressure offset kind"),
                    "exh_fan": g("Exhaust fan tag"), "terminal": g("Terminal tag"),
                    "clinical": g("Clinical criteria"), "area": num(g("Area")),
                    "cols": {k: cell_addr(head[k], n) for k in ("Final supply", "Return", "Exhaust",
                                                                "Total extract", "Offset needed") if k in head},
                }
            # AHU table A (stops at its blank row; table B, rooms moved to FCUs, is not read)
            rows = list(wb["AHU"].iter_rows(values_only=True))
            head, body = self.wb_table(rows, f"D{dept} AHU")
            for n, r in body:
                g = lambda k: num(r[head[k]]) if k in head else None  # noqa: E731
                d["ahu"].append({
                    "tag": str(r[0]), "row": n, "rooms": g("Rooms on this unit"),
                    "supply": g("AHU supply airflow"), "oa": g("AHU outdoor air = larger of fraction × supply and the air that leaves the rooms"),
                    "recirc": g("Recirculated air = AHU supply − outdoor air"), "exhaust": g("Exhaust"),
                    "spill": g("Spill or relief = outdoor air − exhaust − net envelope leakage"),
                    "coil": g("Coil duty (outdoor-air rule)"), "leak": g("Duct leakage"),
                    "oa_sum_rooms": g("Sum of room outdoor air"),
                    "oa_fraction": g("Highest room outdoor-air fraction"),
                    "oa_room": r[head["Highest-standard room (sets the fraction)"]] if "Highest-standard room (sets the fraction)" in head else None,
                    "cols": {"supply": cell_addr(head["AHU supply airflow"], n),
                             "spill": cell_addr(head["Spill or relief = outdoor air − exhaust − net envelope leakage"], n),
                             "coil": cell_addr(head["Coil duty (outdoor-air rule)"], n),
                             "oa": cell_addr(head["AHU outdoor air = larger of fraction × supply and the air that leaves the rooms"], n),
                             "recirc": cell_addr(head["Recirculated air = AHU supply − outdoor air"], n)},
                })
            # Fans
            rows = list(wb["Fans"].iter_rows(values_only=True))
            head, body = self.wb_table(rows, f"D{dept} Fans")
            for n, r in body:
                d["fans"].append({
                    "tag": str(r[0]), "type": r[1], "serves": r[2], "also": r[3], "row": n,
                    "dept": dept,
                    "required": num(r[head["Required airflow"]]),
                    "calc": num(r[head["Calculated airflow, drawn duty"]]),
                    "tender": num(r[head["Tender airflow"]]), "status": r[head["Status"]],
                    "cell": cell_addr(head["Calculated airflow, drawn duty"], n),
                })
            # FCU tab (rooms separated by the outdoor-air rule and FCU rooms); the key is the row number
            rows = list(wb["FCU"].iter_rows(values_only=True))
            head, body = self.wb_table(rows, f"D{dept} FCU")
            for n, r in body:
                if r[1] is None:
                    continue
                d["fcu"].append({
                    "room": str(r[head["Room number"]]), "name": r[head["Room name"]],
                    "ahu": r[head["Original AHU or system"]], "reason": r[head["Reason"]],
                    "supply": num(r[head["Room supply"]]), "oa": num(r[head["Outdoor air"]]),
                    "oa_src": r[head["Outdoor-air source (PAHU or fan)"]], "row": n,
                    "area": num(r[head["Area"]]),
                })
            # Pressure: the room table only; a key counts only if it is a room number of this workbook's Airflow tab
            rows = list(wb["Pressure"].iter_rows(values_only=True))
            head, body = self.wb_table(rows, f"D{dept} Pressure")
            for n, r in body:
                key = str(r[0])
                if key not in d["airflow"]:
                    continue
                d["pressure"][key] = {"set": num(r[head["Set pressure"]]), "row": n,
                                      "cell": cell_addr(head["Set pressure"], n),
                                      "offset": num(r[head["Offset needed"]]) if "Offset needed" in head else None}
            res[dept] = d
        return res

    # -- equipment schedule -----------------------------------------------------#
    def _load_schedule(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for r in read_csv(self.reg / "ahu_schedule.csv"):
            t = r.get("tag_normalised") or r.get("tag_as_written")
            for k in sorted(tag_keys(t)):
                out.setdefault(k, {
                    "tag": t, "supply": num(r.get("supply_airflow_l_s [Supply Airflow (l/s)]")),
                    "oa_min": num(r.get("outdoor_air_min_l_s [Outdoor Airflow / Min (l/s)]")),
                    "cool": num(r.get("total_cooling_kW [Min Total Cooling (kW)]")),
                    "pages": r.get("page(s)"), "sheet": r.get("sheet_code"),
                })
        return out


# --------------------------------------------------------------------------- #
# Room classification (item 1), pure function
# --------------------------------------------------------------------------- #
def _holder(x: Any) -> Tuple[str, bool]:
    return (x, True) if isinstance(x, str) else (x[0], bool(x[1]))


def classify_rooms(drawn: Dict[str, Dict[str, Any]], zone: Dict[str, Dict[str, Any]], complete: bool,
                   zone_other: Dict[str, List[Any]], drawn_other: Dict[str, List[Any]],
                   plan: Optional[Dict[str, Dict[str, Any]]] = None,
                   sheet_names: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """Class each room name of a unit.

    ``drawn`` and ``zone`` map a name key to ``{'count': n, 'name': text, ...}``; a drawn entry may carry
    ``composite`` (the box names several rooms).  ``complete`` is True when the zone room list of the unit is
    complete (D-054).  ``zone_other`` maps a name to the other units that hold it, each a tag or
    ``(tag, list_is_complete)``; ``drawn_other`` the same for boxes drawn on other units.  ``plan`` holds the
    plan rooms (zone assignment) that the unit has; ``sheet_names`` the names drawn on the sheet without the
    tag of this unit.  Where the zone link is unknown the class is "not established", never add or remove.
    A room is "moved to another unit" only when the other unit holds it in a complete list and this list is
    complete too.
    """
    res: List[Dict[str, Any]] = []
    plan = plan or {}
    sheet_names = sheet_names or {}
    left_d = dict(drawn)
    left_z = dict(zone)

    def count_row(dn: str, zn: str, extra: Optional[Dict[str, Any]] = None) -> None:
        dc, zc = drawn[dn]["count"], zone[zn]["count"]
        row: Dict[str, Any] = dict(dname=dn, zname=zn, dc=dc, zc=zc)
        if dc == zc:
            row["cls"] = "unchanged" if dn == zn else "name differs"
        elif zc < dc and not complete:
            row.update(cls="not established", why="the zone list may be incomplete: fewer rooms than drawn")
        else:
            row["cls"] = "typical count changed"
        if extra:
            row.update(extra)
        res.append(row)
        left_d.pop(dn, None)
        left_z.pop(zn, None)

    # 1. same name
    for n in sorted(set(drawn) & set(zone)):
        count_row(n, n)
    # 2. a box that names several rooms already shows the zone rooms it names
    for dn in sorted(list(left_d)):
        d = drawn[dn]
        if not d.get("composite"):
            continue
        hit = [zn for zn in sorted(left_z) if zone[zn].get("tokens") and zone[zn]["tokens"] <= d.get("tokens", frozenset())]
        for zn in hit:
            res.append(dict(cls="named in box", dname=dn, zname=zn, dc=d["count"], zc=zone[zn]["count"]))
            left_z.pop(zn, None)
        if hit:
            left_d.pop(dn, None)
    # 3. wording differs: best similar pair among the leftovers; the count is still compared
    pairs = []
    for dn in left_d:
        for zn in left_z:
            r = difflib.SequenceMatcher(None, dn, zn).ratio()
            a_, b_ = drawn[dn].get("tokens", frozenset()), zone[zn].get("tokens", frozenset())
            if r >= RENAME_RATIO or (a_ and a_ == b_):
                pairs.append((r, dn, zn))
    for r, dn, zn in sorted(pairs, reverse=True):
        if dn in left_d and zn in left_z:
            count_row(dn, zn, dict(ratio=round(r, 2)))
    # 4b. plan rooms of the unit (zone assignment) with the same name key
    for dn in sorted(list(left_d)):
        d = drawn[dn]
        pm = next((pn for pn, pv in plan.items() if plan_matches(pv["name"], d.get("name") or dn)), None)
        if pm:
            res.append(dict(cls="unchanged", dname=dn, zname=None, dc=d["count"], zc=None, plan=plan[pm]["name"]))
            left_d.pop(dn, None)
    # 5. drawn rooms the zone list does not have
    for dn, v in sorted(left_d.items()):
        oth = [_holder(x) for x in zone_other.get(dn, [])]
        if oth and complete and oth[0][1]:
            res.append(dict(cls="moved to another unit", dname=dn, zname=None, dc=v["count"], zc=0, other=oth[0][0]))
        elif oth:
            res.append(dict(cls="not established", dname=dn, zname=None, dc=v["count"], zc=0,
                            why=f"the zone list gives this room to {oth[0][0]}, and a list is not complete"))
        elif complete:
            res.append(dict(cls="to remove", dname=dn, zname=None, dc=v["count"], zc=0))
        else:
            res.append(dict(cls="not established", dname=dn, zname=None, dc=v["count"], zc=0,
                            why="the zone list of this unit is not complete"))
    # 6. zone rooms the sheet does not show for this unit
    for zn, v in sorted(left_z.items()):
        if zn in sheet_names or any(v.get("tokens") and v["tokens"] <= t for t in sheet_names.values() if t):
            res.append(dict(cls="drawn without tag", dname=None, zname=zn, dc=0, zc=v["count"]))
        elif drawn_other.get(zn):
            res.append(dict(cls="moved from another unit", dname=None, zname=zn, dc=0, zc=v["count"],
                            other=_holder(drawn_other[zn][0])[0]))
        else:
            res.append(dict(cls="to add", dname=None, zname=zn, dc=0, zc=v["count"]))
    return res


ROOM_ACTION = {
    "unchanged": ("No change", False),
    "named in box": ("No change: the box names this room", False),
    "name differs": ("No mark-up: wording of the name differs; drawn name kept", False),
    "typical count changed": ("Mark up: change the typical count", True),
    "renamed": ("No mark-up: wording of the name differs; drawn name kept", False),
    "to add": ("Mark up: add the room in the tabulated note", True),
    "to remove": ("Mark up: strike through the room", True),
    "moved to another unit": ("Mark up: strike through here; add on the other unit", True),
    "moved from another unit": ("Mark up: add the room in the tabulated note", True),
    "drawn without tag": ("Query: box drawn on this sheet without the tag of this unit", True),
    "drawn on another sheet": ("No change: the room is drawn on another sheet", False),
    "not established": ("Query: zone link not established", True),
}


def parse_pressure(text: Any) -> Optional[float]:
    t = str(text or "").strip().upper()
    if not t:
        return None
    if t.startswith("NEUTRAL"):
        return 0.0
    m = re.search(r"([+\-−]?)\s*(\d+(?:\.\d+)?)\s*PA\b", t)
    if not m and re.fullmatch(r"[+\-−]?\s*\d+(?:\.\d+)?", t):
        m = re.search(r"([+\-−]?)\s*(\d+(?:\.\d+)?)", t)
    if not m:
        return None
    v = float(m.group(2))
    return -v if m.group(1) in ("-", "−") else v


def pressure_sign(text: Any) -> Optional[int]:
    """-1 or +1 where the sheet gives the regime in words ('-VE', 'NEGATIVE', 'POSITIVE') and no value."""
    t = str(text or "").strip().upper()
    if re.match(r"^(-VE|NEGATIVE|NEG)\b", t):
        return -1
    if re.match(r"^(\+VE|POSITIVE|POS)\b", t):
        return 1
    return None


ABBR = {"Off": "Office", "Mtg": "Meeting", "Str": "Store", "Cln": "Cleaner", "Tfr": "Transfer", "Mp": "Multi-purpose"}
ABBR_RE = re.compile(r"\b(%s)\b" % "|".join(ABBR))


def dedupe_words(name: Any) -> str:
    """'WASHER WASHER WASHER DISINFECTOR DISINFECTOR' gives 'WASHER DISINFECTOR' (a box name read once for each line)."""
    t = str(name or "")
    for _ in range(3):
        t2 = re.sub(r"\b((?:\S+ ){0,3}\S+)(?: \1\b)+", r"\1", t)
        if t2 == t:
            break
        t = t2
    return t


def short_wb(dept: str, wb: Dict[str, Any]) -> str:
    return f"Workbook D{dept}"


# --------------------------------------------------------------------------- #
# The review
# --------------------------------------------------------------------------- #
class Review:
    def __init__(self, inp: Inputs, ledger: Optional["EntityLedger"] = None):
        self.i = inp
        self.rows: List[Row] = []
        self.unit_info: Dict[str, Dict[str, Any]] = {}
        self.missing: List[Row] = []
        self.consist: List[Row] = []
        self.appd_rows: List[Row] = []
        self.mot_rows: List[Dict[str, Any]] = []
        self.untagged: List[Dict[str, Any]] = []
        self.zoning_rows: List[Row] = []
        self.fan_rows: List[Row] = []
        self.not_reviewed_sheets: Dict[str, str] = {}
        self.one_test_rows = 0
        self._prep()
        self.led = ledger or EntityLedger(inp)
        if not any(r["sheet_role"] == "overall" for r in self.led.rows):
            self.led.add_overall({sh: self.read_overall(sh) for sh in self.OVERALL})
        self.build_scope(self.led)
        self.fill_systems()

    # -- preparation ----------------------------------------------------------#
    def _prep(self) -> None:
        i = self.i
        self.unit_by_tag = {u["tag"]: u for u in i.units}
        # same unit under two tags: the schedule prints one tag for the other
        for u in i.units:
            u["alias"] = None
            m = re.search(r"prints the tag as ([^;]+)", str(u["tender_src"] or ""))
            if m and m.group(1).strip() in self.unit_by_tag and m.group(1).strip() != u["tag"]:
                u["alias"] = m.group(1).strip()
        for u in i.units:
            if u["alias"]:
                self.unit_by_tag[u["alias"]].setdefault("alias", None)
                if not self.unit_by_tag[u["alias"]]["alias"]:
                    self.unit_by_tag[u["alias"]]["alias"] = u["tag"]
            u["akeys"] = frozenset(u["keys"] | (tag_keys(u["alias"]) if u["alias"] else frozenset()))
        self.units_by_key: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for u in i.units:
            u["links"] = []
            u["systems"] = []
            for k in u["akeys"]:
                self.units_by_key[k].append(u)
        # links to units
        self.link_unmatched = 0
        for lk in i.links:
            c = lk["unit_class"]
            u = self.unit_by_tag.get(c)
            if u is None:
                ks = tag_keys(c)
                cands = Counter()
                for k in ks:
                    for cu in self.units_by_key.get(k, []):
                        cands[cu["tag"]] += 1
                if cands:
                    u = self.unit_by_tag[cands.most_common(1)[0][0]]
            if u is None:
                self.link_unmatched += 1
                continue
            lk["_unit"] = u["tag"]
            u["links"].append(lk)
        # groups and their members
        groups = [u for u in i.units if u["tag"].startswith("AHU group")]
        for u in i.units:
            u["is_group"] = u in groups
            u["groups"] = []
            u["members"] = []
        for g in groups:
            gk = tag_keys(g["tag"])
            for u in i.units:
                if u["is_group"] or not u["has_tag"]:
                    continue
                if u["akeys"] & gk:
                    g["members"].append(u)
                    u["groups"].append(g)
        self._synthesise_systems()
        # working systems to units
        self.sys_keys: Dict[str, frozenset] = {}
        for s in i.working:
            ks = frozenset().union(*[tag_keys(a["tag"]["value"]) for a in s["ahus"]]) if s["ahus"] else frozenset()
            self.sys_keys[s["system_id"]] = ks
            for k in ks:
                for u in self.units_by_key.get(k, []):
                    if s not in u["systems"]:
                        u["systems"].append(s)
        # list-only units: one predicate, one table (D-082, build line 7)
        self._unit_scope()
        # holders of a room name on a storey: (unit tag, list complete)
        self.zone_names: Dict[str, Dict[str, List[Tuple[str, bool]]]] = defaultdict(lambda: defaultdict(list))
        for u in i.units:
            for lk in u["links"]:
                self.zone_names[lk["storey"]][canon(lk["room_name"])].append((u["tag"], bool(u["complete"])))
        self.drawn_names: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
        self.sheet_boxes: Dict[str, List[Tuple[str, frozenset, frozenset]]] = defaultdict(list)
        for s in i.systems:
            if s.get("source_set") != "working":
                continue
            for rm in s["rooms"]:
                tg = frozenset().union(*[tag_keys(a["value"] if isinstance(a, dict) else a)
                                         for a in rm["ahu_tags_as_written"]]) if rm["ahu_tags_as_written"] else frozenset()
                self.sheet_boxes[s["sheet"]].append((canon(rm["name"]["value"]), name_tokens(rm["name"]["value"]), tg))
                if s["kind"] == "working_ahu_system":
                    for u in self.units_by_key_for_system(s):
                        self.drawn_names[s["storey"]][canon(rm["name"]["value"])].append(u["tag"])
        # workbook room lookup by (dept, room number)
        self.wb_room = {}
        for dept, d in i.wb.items():
            for rn, v in d["airflow"].items():
                self.wb_room[(dept, rn)] = v
        # drawn tag keys on any working sheet and on tender sheets
        self.work_tag_keys: Dict[str, List[Dict[str, str]]] = defaultdict(list)
        self.tender_tag_keys: Dict[str, List[Dict[str, str]]] = defaultdict(list)
        self.mot_tag_keys: Dict[str, List[Dict[str, str]]] = defaultdict(list)
        for e in i.equip:
            for k in sorted(tag_keys(e["tag"])):
                if e.get("issue_set") == "working":
                    self.work_tag_keys[k].append(e)
                elif e.get("issue_set") == "tender":
                    self.tender_tag_keys[k].append(e)
                elif e.get("issue_set") == "mot_nsc":
                    self.mot_tag_keys[k].append(e)
        # model-level drawn tags (AHUs and fans of working systems)
        for s in i.working:
            for a in s["ahus"]:
                for k in sorted(tag_keys(a["tag"]["value"])):
                    self.work_tag_keys[k].append({"sheet": s["sheet"], "tag": a["tag"]["value"]})

    MOT_DEPT_RE = re.compile(r"MOT|Operating Theatre")
    GUARD_MOT_SHEETS = ("IP-BH-07050", "IP-BH-07051", "IP-BH-07052")     # the earlier guard (missing_global)

    def _unit_scope(self) -> None:
        """A unit is list-only when its tag (or a tag_keys variant) is in the MOT NSC AC schedule, or it is a MOT NSC
        theatre unit of the earlier guard (keys on a MOT NSC sheet and a theatre department), or its department in the
        zone schedule is the MOT NSC theatre department.  Fans of the schedule are list-only too."""
        i = self.i
        sched = read_csv(i.reg / "mot_ahu_schedule.csv")
        self.mot_sched_rows = sched
        self.mot_sched_keys = frozenset(k for r in sched for k in tag_keys(r["tag"]))
        self.mot_sched_fan_keys = frozenset(k for r in sched if r["tag"].split("-")[0] in FAN_PREFIXES for k in tag_keys(r["tag"]))
        for u in i.units:
            reason = ""
            if u["akeys"] & self.mot_sched_keys:
                reason = "tag in the MOT NSC AC equipment schedule (mot_ahu_schedule.csv)"
            elif (u["akeys"] & i.mot_keys) and (self.MOT_DEPT_RE.search(str(u["depts"])) or
                                                any(x["sheet"] in i.mot_sheets for x in u["systems"])):
                reason = "MOT NSC theatre unit of the earlier guard (tag on a MOT NSC list and a theatre department)"
            elif self.MOT_DEPT_RE.search(str(u["depts"])):
                reason = "department in the zone schedule is the MOT NSC theatre department"
            u["list_only"] = bool(reason)
            u["list_reason"] = reason

    def unit_list_only(self, text: str) -> bool:
        """List-only test of a unit tag or a fan tag (all its keys)."""
        ks = tag_keys(text)
        if not ks:
            return False
        for u in self.i.units:
            if u["has_tag"] and ks & u["akeys"] and u["list_only"]:
                return True
        return False

    def drawn_only_list_only(self, tag: str) -> bool:
        v = getattr(self, "drawn_only_units", {}).get(tag)
        return bool(v and v["list_only"])

    def near_units(self, tag: str) -> List[Dict[str, Any]]:
        """Units whose code is a near match of the fan tag code (B2COR02 against B2COR-01): a query, never a merge."""
        out = []
        for u in self.i.units:
            if u["has_tag"] and u["type"] == "AHU" and not u.get("is_group") and any(
                    re.sub(r"[^A-Z0-9]", "", self.core(k)) == re.sub(r"[^A-Z0-9]", "", self.core(kk)) or
                    difflib.SequenceMatcher(None, self.core(k), self.core(kk)).ratio() >= 0.85
                    for k in tag_keys(tag) for kk in u["akeys"]):
                out.append(u)
        return out

    def fan_list_only(self, tag: str) -> Tuple[bool, str]:
        ks = tag_keys(tag)
        if ks & self.mot_sched_fan_keys:
            return True, "fan in the MOT NSC AC equipment schedule"
        owners = self.fan_owners(tag)
        if owners and all(o["list_only"] for o in owners):
            return True, "fan owned by list-only unit " + "; ".join(o["tag"] for o in owners)
        if not owners:
            near = self.near_units(tag)
            if near and all(o["list_only"] for o in near):
                return True, "fan tag near to list-only unit " + "; ".join(o["tag"] for o in near)
            shs = sorted({r["sheet"] for r in getattr(self, "led", None).by_key.get(next(iter(sorted(ks)), ""), []) if r["kind"] == "fan tag"}) \
                if getattr(self, "led", None) is not None and ks else []
            if shs and all(self.sheet_list_only(x) for x in shs):
                return True, "fan with no owner on a sheet where every AHU is list-only"
        return False, ""

    def build_scope(self, ledger: "EntityLedger") -> List[Dict[str, Any]]:
        """scope.csv: one row for every unit, every fan tag of the working sheets and of the MOT schedule, and every
        working sheet (build line 7)."""
        rows: List[Dict[str, Any]] = []
        self.sheet_scope: Dict[str, Dict[str, Any]] = {}
        self.drawn_only_units: Dict[str, Dict[str, Any]] = {}
        sheet_rows: List[Dict[str, Any]] = []
        # sheets first (a fan with no owner follows the sheet it is drawn on)
        for sh in sorted(ledger.by_sheet):
            if ledger.by_sheet[sh][0]["sheet_role"] != "working":
                continue
            ahus: Dict[str, List[Dict[str, Any]]] = {}
            for r in ledger.by_sheet[sh]:
                if r["kind"] == "AHU tag":
                    for k in r["tag_keys"].split("; "):
                        us = [u for u in self.units_by_key.get(k, []) if not u.get("is_group")] or self.units_by_key.get(k, [])
                        ahus.setdefault(r["_tag"], us)

            def _lo(tg: str, us: List[Dict[str, Any]], sh: str = sh) -> bool:
                if us:
                    return all(u["list_only"] for u in us)
                # drawn AHU that is in no zone unit: list-only where its tag is in the MOT schedule or the sheet is one
                # of the MOT theatre block sheets of the earlier guard
                return bool(tag_keys(tg) & self.mot_sched_keys) or sh in self.GUARD_MOT_SHEETS or sh in self.i.mot_sheets

            for tg, us in ahus.items():
                if not us:
                    v = self.drawn_only_units.setdefault(tg, {"list_only": _lo(tg, us), "sheets": set()})
                    v["sheets"].add(sh)
            for r in ledger.by_sheet[sh]:
                if r["kind"] == "FCU tag" and not any(self.units_by_key.get(k) for k in r["tag_keys"].split("; ")):
                    v = self.drawn_only_units.setdefault(r["_tag"], {"list_only": _lo(r["_tag"], []), "sheets": set()})
                    v["sheets"].add(sh)
            flags = [_lo(tg, us) for tg, us in ahus.items()]
            lo = bool(flags) and all(flags)
            self.sheet_scope[sh] = {"list_only": lo, "n_ahu": len(ahus)}
            sheet_rows.append({"kind": "sheet", "unit": "", "sheet": sh, "list_only": int(lo),
                               "reason": ("every AHU on the sheet is list-only" if lo else
                                          "mixed sheet: list-only units are listed, the others are marked up" if any(flags) else ""),
                               "department": "", "keys": "", "sheets": "; ".join(sorted(ahus)),
                               "status": "REVIEWED, listed, no mark-up" if lo else ""})
        for u in self.i.units:
            if not u["has_tag"]:
                continue
            shs = ledger.sheets_of_keys(u["akeys"])
            rows.append({"kind": "unit", "unit": u["tag"], "sheet": "", "list_only": int(u["list_only"]),
                         "reason": u["list_reason"], "department": u["depts"],
                         "keys": "; ".join(sorted(u["akeys"])), "sheets": "; ".join(shs), "status": ""})
        for tg, v in sorted(self.drawn_only_units.items()):
            rows.append({"kind": "unit", "unit": tg, "sheet": "", "list_only": int(v["list_only"]),
                         "reason": ("drawn on a MOT NSC theatre sheet and in no zone unit (earlier guard)" if v["list_only"]
                                    else "drawn, not in the zone schedule"),
                         "department": "", "keys": "; ".join(sorted(tag_keys(tg))), "sheets": "; ".join(sorted(v["sheets"])),
                         "status": ""})
        seen = set()
        fan_sheets: Dict[str, set] = defaultdict(set)
        for x in ledger.rows:
            if x["kind"] == "fan tag":
                fan_sheets[x["_tag"]].add(x["sheet"])
        for r in sorted((x for x in ledger.rows if x["kind"] == "fan tag"), key=lambda x: (x["_tag"], x["sheet"])):
            if r["_tag"] in seen:
                continue
            seen.add(r["_tag"])
            lo, why = self.fan_list_only(r["_tag"])
            rows.append({"kind": "fan", "unit": r["_tag"], "sheet": "", "list_only": int(lo), "reason": why,
                         "department": "", "keys": r["tag_keys"], "sheets": "; ".join(sorted(fan_sheets[r["_tag"]])), "status": ""})
        for rr in self.mot_sched_rows:
            t = rr["tag"]
            if t.split("-")[0] in FAN_PREFIXES and t not in seen:
                seen.add(t)
                rows.append({"kind": "fan", "unit": t, "sheet": "", "list_only": 1, "reason": "fan in the MOT NSC AC equipment schedule",
                             "department": "", "keys": "; ".join(sorted(tag_keys(t))), "sheets": "", "status": ""})
        rows.extend(sheet_rows)
        self.scope_rows = rows
        return rows

    SCOPE_COLS = ["kind", "unit", "sheet", "list_only", "reason", "department", "keys", "sheets", "status"]

    def write_scope(self, path: Path) -> None:
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(self.SCOPE_COLS)
            for r in self.scope_rows:
                w.writerow([r.get(c, "") for c in self.SCOPE_COLS])

    def fill_systems(self) -> None:
        """Ledger columns 'system' and 'sister_sheets': the system of the entity and the other sheets that draw the same
        unit(s) of that system (a unit drawn over several sheets is reviewed once, over the union, build line 6)."""
        L = self.led
        by_sheet: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for s in self.i.working:
            by_sheet[s["sheet"]].append(s)
        tags_by_sheet: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for r in L.rows:
            if r["sheet_role"] == "working" and r["kind"] in ("AHU tag", "FCU tag"):
                tags_by_sheet[r["sheet"]].append(r)
        for r in L.rows:
            if r["sheet_role"] != "working" or r["system"]:
                continue
            ks = {k for k in r["tag_keys"].split("; ") if k}
            if r["kind"] in ("AHU tag", "FCU tag", "PAHU tag", "fan tag", "BIBO tag") and ks:
                cores = {self.core(k) for k in ks}
                for s in by_sheet.get(r["sheet"], []):
                    sk = self.sys_keys.get(s["system_id"], frozenset())
                    if sk & ks or (r["kind"] == "fan tag" and cores & {self.core(k) for k in sk}):
                        r["system"] = s["system_id"]
                        break
        for r in L.rows:
            if r["sheet_role"] != "working" or r["system"] or not r["_box"]:
                continue
            cands = [a for a in tags_by_sheet.get(r["sheet"], []) if a["system"]]
            if cands:
                r["system"] = min(cands, key=lambda a: SheetText._dist(a["_box"], r["_box"]))["system"]
        for r in L.rows:
            if r["sheet_role"] != "working" or not r["system"]:
                continue
            sk = self.sys_keys.get(r["system"], frozenset())
            r["sister_sheets"] = "; ".join(x for x in L.sheets_of_keys(sk) if x != r["sheet"])

    def sheet_list_only(self, sheet: str) -> bool:
        return bool(getattr(self, "sheet_scope", {}).get(sheet, {}).get("list_only"))

    def fan_owners(self, tag: str) -> List[Dict[str, Any]]:
        """Units whose name core is in the fan tag (RAF-...-04MRI-01A belongs to AHU-...-04MRI-01A/B)."""
        cores = {self.core(k) for k in tag_keys(tag)}
        out = []
        for u in self.i.units:
            if not u["has_tag"] or u.get("is_group") or u["type"] not in ("AHU", "CRAC"):
                continue
            if cores & {self.core(k) for k in u["akeys"]}:
                out.append(u)
        return out

    def _sheet_units(self, sheet: str) -> Dict[str, Tuple[Dict[str, Any], List[float], str]]:
        """Units of the zone schedule whose AHU, FCU or CRAC tag the sheet prints (unit tag -> unit, box, text)."""
        out: Dict[str, Tuple[Dict[str, Any], List[float], str]] = {}
        for t, b in self.i.st.lines(sheet):
            m = re.match(r"^((?:AHU|FCU|CRAC)-[A-Z0-9][A-Za-z0-9\-]*(?:/[A-Z0-9]+)?(?:\s[A-Z])?)", t)
            if not m:
                continue
            for k in sorted(tag_keys(m.group(1))):
                for u in self.units_by_key.get(k, []):
                    if u["tag"] not in out:
                        out[u["tag"]] = (u, b, m.group(1).strip())
        return out

    def _synthesise_systems(self) -> None:
        """Room boxes on a sheet with no AHU system in the model (the model did not link them to a unit): give
        each box to the unit whose tag the sheet prints, so that the box is reviewed.  A sheet that prints no unit
        tag continues the sheet before it in the series."""
        i = self.i
        have = {s["sheet"] for s in i.working}
        synth: List[Dict[str, Any]] = []
        keep: List[Dict[str, Any]] = []
        self.synth_sheets: Dict[str, List[str]] = {}
        for us in i.unassigned:
            sheet = us["sheet"]
            if sheet in have or not us["rooms"]:
                keep.append(us)
                continue
            cands = self._sheet_units(sheet)
            if not cands:
                prev = sheet[:-1] + str(int(sheet[-1]) - 1) if sheet[-1].isdigit() and sheet[-1] != "0" else ""
                for w in i.working:
                    if w["sheet"] == prev:
                        for k in w["ahus"]:
                            for kk in sorted(tag_keys(k["tag"]["value"])):
                                for u in self.units_by_key.get(kk, []):
                                    cands.setdefault(u["tag"], (u, k["tag"].get("box") or [0, 0, 0, 0], k["tag"]["value"]))
            if not cands:
                keep.append(us)
                continue
            groups = [t for t, (u, _, _) in cands.items() if u.get("is_group")]
            assign: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            rest: List[Dict[str, Any]] = []
            for rm in us["rooms"]:
                n = canon(rm["name"]["value"])
                if groups:
                    assign[groups[0]].append(rm)
                    continue
                if len(cands) == 1:
                    assign[next(iter(cands))].append(rm)
                    continue
                best, bt = 0.0, None
                for tg, (u, _, _) in sorted(cands.items()):
                    for zn, v in list(self.zone_rooms(u).items()) + list(self.plan_for(u).items()):
                        r = difflib.SequenceMatcher(None, n, zn).ratio()
                        if plan_matches(v["name"], rm["name"]["value"]):
                            r = max(r, 0.9)
                        if r > best:
                            best, bt = r, tg
                if bt and best >= 0.6:
                    assign[bt].append(rm)
                else:
                    rest.append(rm)
            for tg, rms in assign.items():
                u, b, txt = cands[tg]
                dn = i.st.duty_near(sheet, b)
                tagd = {"value": txt, "as_written": txt, "box": b, "box_level": "tag", "sheet": sheet, "page": us["page"]}
                duty = {"value": dn[0] if dn else None, "as_written": "", "box": dn[1] if dn else b, "box_level": "duty",
                        "sheet": sheet, "page": us["page"]}
                synth.append({
                    "system_id": f"synthetic:{sheet}:{tg}", "kind": "working_ahu_system", "source_set": "working",
                    "sheet": sheet, "page": us["page"], "storey": us["storey"], "title": us.get("title"),
                    "ahus": [{"tag": tagd, "duty_supply_L_s": duty}], "duty_standby": None, "plant_level": None,
                    "served_level": None, "outdoor_air": {"source": "", "lines": [], "pahu_rows": []},
                    "return_air": {"fans": []}, "spill_relief": {"lines": [], "louvres": [], "note": ""},
                    "exhaust_fans": [], "isolation_exhaust": {"header_labels": [], "line_rows": []},
                    "bibo_units": [], "toilet_exhaust_louvres": [], "filters_on_room_boxes": [], "totals_register": None,
                    "rooms": rms, "requirement_note_numbers": []})
                self.synth_sheets.setdefault(sheet, []).append(tg)
            if rest:
                keep.append(dict(us, rooms=rest))
        # untagged boxes on a sheet that has systems: give a box to a unit of the sheet only where one unit, and one
        # only, holds a room of this name (zone list or plan rooms); the others stay unassigned (not reviewed)
        self.box_assigned_by_name = 0
        keep2: List[Dict[str, Any]] = []
        for us in keep:
            sheet = us["sheet"]
            if sheet not in have or not us["rooms"]:
                keep2.append(us)
                continue
            systems = [w for w in i.working if w["sheet"] == sheet and w["kind"] == "working_ahu_system"]
            cand: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
            for w in systems:
                ks = frozenset().union(*[tag_keys(a["tag"]["value"]) for a in w["ahus"]]) if w["ahus"] else frozenset()
                for k in ks:
                    for u in self.units_by_key.get(k, []):
                        if not any(u is c[0] and w is c[1] for c in cand) and not u.get("is_group"):
                            cand.append((u, w))
            rest2: List[Dict[str, Any]] = []
            for rm in us["rooms"]:
                hits = []
                for u, w in cand:
                    for zn, v in list(self.zone_rooms(u).items()) + list(self.plan_for(u).items()):
                        if canon(rm["name"]["value"]) == zn or plan_matches(v["name"], rm["name"]["value"]):
                            if (u, w) not in hits:
                                hits.append((u, w))
                            break
                utags = {u["tag"] for u, _ in hits}
                if len(utags) == 1:
                    u, w = hits[0]
                    w["rooms"].append(dict(rm, assigned_by_name=True))
                    self.box_assigned_by_name += 1
                else:
                    rest2.append(rm)
            if rest2:
                keep2.append(dict(us, rooms=rest2))
        keep = keep2
        i.working = i.working + synth
        i.systems = i.systems + synth
        i.unassigned = keep

    def units_by_key_for_system(self, s: Dict[str, Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for k in self.sys_keys[s["system_id"]]:
            for u in self.units_by_key.get(k, []):
                if u not in out:
                    out.append(u)
        return out

    def family(self, u: Dict[str, Any], box_keys: Iterable[str] = ()) -> set:
        """Tags of the units that count as 'this unit' for a drawn box: the unit, its alias, its groups,
        its members and every unit whose tag is written on the box."""
        fam = {u["tag"]}
        if u.get("alias"):
            fam.add(u["alias"])
        fam |= {g["tag"] for g in u["groups"]}
        fam |= {m["tag"] for m in u["members"]}
        for k in box_keys:
            for x in self.units_by_key.get(k, []):
                fam.add(x["tag"])
                fam |= {g["tag"] for g in x["groups"]}
        return fam

    # -- drawn values of a unit ---------------------------------------------- #
    @staticmethod
    def _per_key_sum(items: Iterable[Tuple[str, float, Dict[str, Any]]], restrict: Optional[Iterable[str]] = None
                     ) -> Tuple[Optional[float], List[Dict[str, Any]]]:
        """Sum over distinct keys of the largest value (an A/B pair counts once).  With ``restrict``, only the keys of the
        unit count: a common AHU that carries the keys of two units adds to each unit once."""
        best: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        rs = set(restrict) if restrict is not None else None
        for tag, v, ref in items:
            if v is None:
                continue
            for k in sorted(tag_keys(tag) or {tag}):
                if rs is not None and k not in rs:
                    continue
                if k not in best or v > best[k][0]:
                    best[k] = (v, ref)
        if not best:
            return None, []
        return sum(v for v, _ in best.values()), [r for _, r in best.values()]

    def drawn_rooms(self, u: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """Drawn room boxes of the unit by name key.  A box that two systems of the unit both list counts once."""
        out: Dict[str, Dict[str, Any]] = {}
        seen_box = set()
        for s in u["systems"]:
            for rm in s["rooms"]:
                tags = frozenset().union(*[tag_keys(a["value"] if isinstance(a, dict) else a)
                                           for a in rm["ahu_tags_as_written"]]) if rm["ahu_tags_as_written"] else frozenset()
                if tags and not (tags & u["akeys"]):
                    continue
                bk = (s["sheet"], tuple(rm["name"].get("box") or ()), rm["name"]["value"])
                if bk in seen_box:
                    continue
                seen_box.add(bk)
                n = canon(rm["name"]["value"])
                modes = rm["modes"]
                md = next((m for m in modes if m["mode"] == "NORM"), modes[0] if modes else None)
                e = out.setdefault(n, {"count": 0, "name": rm["name"]["value"], "rm": rm, "mode": md,
                                       "sheet": s["sheet"], "page": s["page"], "storey": s["storey"],
                                       "other_modes": [m["mode"] for m in modes if m is not md],
                                       "composite": is_composite(rm["name"]["value"]),
                                       "tokens": name_tokens(rm["name"]["value"]), "tagkeys": set()})
                e["count"] += int(rm.get("typical_count") or 1)
                e["tagkeys"] |= set(tags)
                e.setdefault("boxes", []).append((s["sheet"], tuple(round(float(x), 1) for x in (rm["name"].get("box") or ()))))
        return out

    def long_name(self, z: Dict[str, Any]) -> str:
        """Name of a zoned room for a note: the workbook name where the plan name is an abbreviation."""
        nm = str(z["name"])
        if re.search(r"\b(?:%s|CU|SAN|HC|Multi)\b" % "|".join(ABBR), nm):
            for lk in z.get("links", []):
                dept = f"{int(lk['dept']):02d}" if str(lk["dept"]).isdigit() else str(lk["dept"])
                w = self.wb_room.get((dept, lk["dpa_room_id"]))
                if w and w.get("name") and not re.fullmatch(r"[A-Za-z]{1,4}", str(w["name"])) and not ABBR_RE.search(str(w["name"])):
                    return str(w["name"])
            nm = ABBR_RE.sub(lambda m: ABBR[m.group(1)], nm)
            if re.search(r"\b(?:CU|SAN|HC|Multi)\b", nm):
                nm += " (plan label)"
        return nm

    def zone_rooms(self, u: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """Zone rooms of the unit: its own links, those of its alias, of its groups and, for a group, of its members."""
        src: List[Dict[str, str]] = list(u["links"])
        if u.get("alias") and u["alias"] in self.unit_by_tag:
            src += self.unit_by_tag[u["alias"]]["links"]
        for g in u["groups"]:
            src += g["links"]
        for m in u["members"]:
            src += m["links"]
        out: Dict[str, Dict[str, Any]] = {}
        seen = set()
        for lk in src:
            key = (lk["dept"], lk["dpa_room_id"])
            if key in seen:
                continue
            seen.add(key)
            n = canon(lk["room_name"])
            e = out.setdefault(n, {"count": 0, "name": lk["room_name"], "links": [],
                                   "tokens": name_tokens(lk["room_name"])})
            e["count"] += 1
            e["links"].append(lk)
        return out

    def plan_for(self, u: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """Plan rooms (zone assignment) of the unit, its alias, its groups and members, by name key."""
        tags = {u["tag"]} | ({u["alias"]} if u.get("alias") else set()) | {g["tag"] for g in u["groups"]} \
            | {m["tag"] for m in u["members"]}
        out: Dict[str, Dict[str, Any]] = {}
        for t in tags:
            for r in self.i.plan_rooms.get(t, []):
                n = canon(r["room_name"])
                out.setdefault(n, {"name": r["room_name"], "tokens": name_tokens(r["room_name"]), "id": r["room_id"],
                                   "storey": r["storey"], "area": r["area_m2"]})
        return out

    def master_for(self, u: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
        """Master summary row of the unit: its own, else that of its alias."""
        m = self.i.master.get(u["tag"])
        if m and m.get("Calculated airflow"):
            return m, ""
        if u.get("alias"):
            a = self.i.master.get(u["alias"])
            if a and a.get("Calculated airflow"):
                return a, f"The workbook gives this unit under the tag {u['alias']}."
        return m, ""

    def sheet_of(self, u: Dict[str, Any]) -> Tuple[str, Any, Optional[List[float]]]:
        if u["systems"]:
            s = u["systems"][0]
            a = s["ahus"][0]["tag"] if s["ahus"] else None
            return s["sheet"], s["page"], (a or {}).get("box")
        return "", "", None

    # -- gate for unit-level rows (D-083, build line 11) ---------------------------- #
    def gate(self, u: Dict[str, Any]) -> Dict[str, Any]:
        """One gate ahead of judge() for every unit-level row.  A row is instruction-grade only when (a) the unit is in
        the zone schedule, (b) its room list is complete (D-027) and (c) counted area / zoned area is 0.90 to 1.10,
        computed from the zone schedule."""
        if "_gate" in u:
            return u["_gate"]
        in_sched = bool(u.get("has_tag")) and u in self.i.units
        complete = self.d027_complete(u)
        counted, zoned = u.get("area_counted"), u.get("area")
        cover = (counted / zoned) if (counted and zoned) else None
        m = re.search(r"(\d+) of (\d+) zoned rooms counted", str(u["room_list"]))
        if m:
            n, tot = int(m.group(1)), int(m.group(2))
        elif re.search(r"all (\d+) zoned rooms", str(u["room_list"])):
            n = tot = int(re.search(r"all (\d+) zoned rooms", str(u["room_list"])).group(1))
        elif "the zoned room is counted" in str(u["room_list"]):
            n = tot = 1
        else:
            n, tot = None, u.get("rooms")
        reasons = []
        if not in_sched:
            reasons.append("the unit is not in the zone schedule")
        if not complete:
            reasons.append("the room list of the unit is not complete")
        if cover is None:
            reasons.append("no counted area to compare with the zoned area")
        elif not (GATE_LO <= cover <= GATE_HI):
            reasons.append(f"the rooms counted cover {cover:.0%} of the zoned area (limits {GATE_LO:.0%} to {GATE_HI:.0%})")
        g = {"ok": not reasons, "reasons": reasons, "cover": cover, "n": n, "m": tot,
             "text": ("indicative, from %s of %s rooms" % (n if n is not None else "some", tot if tot is not None else "all")) +
                     ((", cover %.0f %%" % (cover * 100)) if cover is not None else "")}
        u["_gate"] = g
        return g

    @staticmethod
    def d027_complete(u: Dict[str, Any]) -> bool:
        """Room list complete under D-027: every zoned room is counted and no counted room lacks a criteria row.  The
        later conditions of the reduction rule (second room list, tender base, D-054) are not part of the gate."""
        t = str(u["room_list"])
        # a second room list that states another room count than the one counted: the list is not accepted as complete
        # (conservative reading for instructions; D-088: a wrong query is acceptable, a wrong instruction is not)
        m2 = re.search(r"second room list gives (\d+) rooms against (\d+) counted", t)
        if m2 and m2.group(1) != m2.group(2):
            return False
        if t.startswith("Complete"):
            return True
        if re.search(r"no criteria row|have no criteria", t):
            return False
        if t.startswith("Not complete: no department workbook") or "not in the zone schedule" in t:
            return False
        m = re.search(r"(\d+) of (\d+) zoned rooms counted", t)
        if m:
            return m.group(1) == m.group(2)
        return bool(re.search(r"all \d+ zoned rooms are counted|the zoned room is counted", t))

    GATE_ACTION = "Query: required value not established - room list"

    def gated(self, g: Dict[str, Any], gates: Optional[Sequence[Dict[str, Any]]] = None) -> bool:
        return all(x["ok"] for x in (gates if gates is not None else [g]))

    def gate_note(self, req: Optional[float], uom: str, gates: Sequence[Dict[str, Any]]) -> str:
        why = "; ".join(sorted({r for g in gates for r in g["reasons"]}))
        ind = f" Calculated value {req:,.0f} {uom} is {gates[0]['text']}." if req is not None else ""
        return f"The room list of the unit does not establish a required value ({why}).{ind} The drawn value stays."

    def gate_extra(self, gates: Sequence[Dict[str, Any]], req: Optional[float]) -> Dict[str, Any]:
        g = gates[0]
        return dict(indicative=round(req, 1) if req is not None else "",
                    cover=round(g["cover"], 3) if g["cover"] is not None else "",
                    rooms_counted=g["n"] if g["n"] is not None else "", rooms_zoned=g["m"] if g["m"] is not None else "")

    # -- unit review ------------------------------------------------------------#
    def detect_zoning_conflicts(self) -> None:
        """Where the service area that a document prints for a unit contradicts the approved zoning (build line 21): one
        zoning-conflict row for the unit that names both readings and their sources.  Both stay as they are.  All room and
        total mark-ups of the unit are held as queries.  Test: the area named in the equipment schedule shares no word
        with any room that the zoning gives to the unit, and no working sheet draws the unit."""
        GEN = {"AREA", "AND", "THE", "OF", "ROOM", "ROOMS", "COMMON", "AHU", "FCU", "FOR", "LEVEL", "SUPPORT", "SPACE"}

        def toks(t: Any) -> set:
            out = set()
            for w in re.sub(r"[^A-Z0-9 ]", " ", str(t).upper()).split():
                w = re.sub(r"S$", "", w)
                if w in GEN or len(w) <= 2 or re.fullmatch(r"L\d+|\d+", w):
                    continue
                out.add(w)
            return out

        desc: Dict[str, Tuple[str, str]] = {}
        for r in read_csv(self.i.reg / "ahu_schedule.csv"):
            for k in sorted(tag_keys(r["tag_normalised"] or r["tag_as_written"])):
                desc.setdefault(k, (r.get("served_area [Description]") or "", r.get("page(s)") or ""))
        self.zoning_conflicts: List[Dict[str, Any]] = []
        for u in self.i.units:
            if not (u["has_tag"] and u["type"] == "AHU" and not u["list_only"] and not u.get("is_group") and not u["systems"]):
                continue
            d = next((desc[k] for k in u["akeys"] if k in desc), None)
            zr = self.zone_rooms(u)
            if not d or not d[0] or not zr:
                continue
            rn = set()
            for v in zr.values():
                rn |= toks(v["name"])
            if toks(d[0]) and not (toks(d[0]) & rn) and len(zr) <= 2:
                u["zoning_hold"] = True
                lk = u["links"][0] if u["links"] else {}
                self.zoning_conflicts.append({"unit": u["tag"], "schedule": d[0], "page": d[1], "rooms": [v["name"] for v in zr.values()],
                                              "record": (lk.get("source_ref") or "")[:120], "unit_row": u["row"]})

    def review_all(self) -> None:
        self.detect_zoning_conflicts()
        self.build_fan_groups()
        for u in self.i.units:
            if not u["has_tag"]:
                self.untagged.append(u)
                continue
            self.review_unit(u)
        self.review_fans()
        self.review_untagged()

    def review_unit(self, u: Dict[str, Any]) -> None:
        tag = u["tag"]
        rows: List[Row] = []
        sheet, page, tagbox = self.sheet_of(u)
        info = {"tag": tag, "type": u["type"], "sheets": sorted({s["sheet"] for s in u["systems"]}),
                "complete": u["complete"], "counts": Counter(), "list_only": u["list_only"]}
        self.unit_info[tag] = info
        g = self.gate(u)
        d054 = bool(u["complete"] and u["reduction"] not in (None, "") and g["ok"])
        info["d054"] = d054
        if u["list_only"]:
            self.list_only_unit(u, rows, info)
        elif not u["systems"]:
            self.no_drawn_unit(u, rows, info)
        else:
            self.rooms_block(u, rows, d054, info)
            if u["type"] in ("AHU", "CRAC"):
                self.totals_block(u, rows, d054, info)
            else:
                rows.append(mk(tag, "queries", "AHU total", tag, "Unit", "", "drawn", "n/a", "not reviewed",
                               "No mark-up: unit type is " + str(u["type"]) + "; its rooms are reviewed above", False,
                               sheet, page, tagbox, u["source"]))
        for r in rows:
            r["unit"] = tag
        self.rows.extend(rows)
        info["rows"] = rows

    def list_only_unit(self, u: Dict[str, Any], rows: List[Row], info: Dict[str, Any]) -> None:
        """A list-only unit (build line 23): no change ID, no annotation, no overall-schematic addition."""
        tag = u["tag"]
        ents = self.led.tag_rows(u["akeys"], kinds=("AHU tag",))
        sh = sorted({e["sheet"] for e in ents})
        e0 = ents[0] if ents else None
        rows.append(mk(tag, "queries", "AHU total", tag, "Unit listed (MOT)", "L/s",
                       (e0["value"] if e0 and e0["value"] != "" else "not drawn"), "n/a", "listed (MOT)",
                       "List only (MOT): see the MOT differences tab; no mark-up", False,
                       sh[0] if sh else "", 1, e0["_box"] if e0 else None, u["source"],
                       "The unit is a MOT NSC unit (" + u["list_reason"] + "). It is reviewed and listed. It is not marked up.",
                       ledger_id=e0["entity_id"] if e0 else ""))
        info["counts"]["listed (MOT)"] += 1

    # -- the one drawn home of a unit that the model did not link to a system -------------------- #
    def drawn_unit_row(self, u: Dict[str, Any], rows: List[Row], info: Dict[str, Any]) -> bool:
        """A unit that the model did not link to a system but that a working sheet prints by its tag: review the
        drawn duty against the workbook instead of declaring the unit missing (D-078: 'drawn on IP-BH-nnnnn')."""
        hits = self.led.tag_rows(u["akeys"] | u["keys"], kinds=("AHU tag", "FCU tag"))
        hits = [h for h in hits if h["_tag"].split("-")[0] == str(u["tag"]).split("-")[0].split(" ")[0]]
        if not hits:
            return False
        h0 = hits[0]
        sh, box, txt = h0["sheet"], h0["_box"], h0["_tag"]
        tag = u["tag"]
        pg = 1
        m = self.i.master.get(tag)
        req = (m or {}).get("Calculated airflow")
        g = self.gate(u)
        dn = h0.get("_equip")
        dval = num(dn["duty_as_written"]) if dn else None
        dbox = jbox(dn["box_page_view_duty"]) if dn else None
        src = f"Working drawing {sh} p{pg}, tag {txt}" + ("; Workbook master summary, tab AHU Master, row %s" % m["_row"] if m else "")
        sheets = ", ".join(sorted({x["sheet"] for x in hits}))
        # the duty entity of the tag, if the ledger has one
        dent = next((x for x in self.led.rows if x["kind"] == "duty" and x["parent_id"] == h0["entity_id"]), None)
        if dval and req:
            if not g["ok"]:
                rows.append(mk(tag, "queries", "AHU total", tag, "Supply airflow", "L/s", dval, None, "not established",
                               self.GATE_ACTION, True, sh, pg, dbox or box, src,
                               f"The unit is drawn on {sheets}. " + self.gate_note(req, "L/s", [g]),
                               ledger_id=(dent or h0)["entity_id"], **self.gate_extra([g], req)))
                info["counts"]["not established"] += 1
            else:
                cls, act, need = judge(dval, req, False)
                if cls in ("no change", "margin", "reduction"):
                    act, need = "No change", False
                lay = "airflows" if need else "queries"
                rows.append(mk(tag, lay, "AHU total", tag, "Supply airflow", "L/s", dval, req, cls, act, need, sh, pg, dbox or box,
                               src, f"The unit is drawn on {sheets}. The sheet draws it and no system of the sheet is linked to it. "
                                    "Drawn duty compared with the workbook.", ledger_id=(dent or h0)["entity_id"]))
                info["counts"][cls] += 1
        else:
            rows.append(mk(tag, "queries", "AHU total", tag, "Unit drawn on a working sheet", "L/s", "drawn on " + sheets,
                           "not established", "drawn, not compared",
                           "Query: unit is drawn on this sheet; confirm its duty against the workbook", True, sh, pg, box, src,
                           "The sheet prints the tag. It prints no duty line under the tag, or the workbook gives no value.",
                           ledger_id=h0["entity_id"]))
            info["counts"]["drawn, not compared"] += 1
        info["sheets_found"] = sorted({x["sheet"] for x in hits})
        return True

    def no_drawn_unit(self, u: Dict[str, Any], rows: List[Row], info: Dict[str, Any]) -> None:
        """No 'missing system' or 'no schematic' row unless a ledger lookup of the tag (all variants) returns nothing on
        every working sheet, and the room-box tags and the equipment list of the working issue agree (D-078)."""
        tag = u["tag"]
        keys = u["keys"] | u["akeys"]
        if self.drawn_unit_row(u, rows, info):
            return
        # room-box tags and work_tag_keys (build line 19)
        box_hits = sorted({sh for sh, lst in self.sheet_boxes.items() for _, _, tg in lst if tg & keys})
        work_hits = sorted({e.get("sheet") for k in keys for e in self.work_tag_keys.get(k, []) if e.get("sheet")})
        if box_hits or work_hits:
            sh = sorted(set(box_hits) | set(work_hits))
            rows.append(mk(tag, "queries", "AHU total", tag, "Unit drawn on a working sheet", "L/s", "drawn on " + ", ".join(sh),
                           "not established", "drawn, not compared",
                           "Query: unit is drawn on " + ", ".join(sh) + "; the link to the zone unit is not read", True, sh[0], 1, None,
                           u["source"], "The tag is on the sheet in a room box or in the equipment list. The duty is not read. "
                                        "No missing-system row is raised."))
            info["counts"]["drawn, not compared"] += 1
            return
        # correction pass (Opus check item 2, build lines 17 and 19): a fan of this unit (tag core) drawn on a working sheet
        # means the unit is drawn there; the row is a query on the fan, never a missing system.
        def _core(t: str) -> str:
            t = re.sub(r"^(?:P?AHU|RAF|EAF|TEAF|IEAF|EF)-", "", t.strip())
            return re.sub(r"(\d+)(?:/?[AB])?(?:/[AB])?$", r"\1", t)
        ucore = _core(tag)
        fan_hits = [r for r in self.led.rows if r["kind"] == "fan tag" and r["sheet_role"] == "working" and _core(str(r["_tag"])) == ucore]
        if fan_hits and not u["tag"].startswith("FCU"):
            shs = sorted({r["sheet"] for r in fan_hits})
            fans = ", ".join(sorted({str(r["_tag"]) for r in fan_hits}))
            rows.append(mk(tag, "queries", "AHU total", tag, "Unit drawn on a working sheet", "L/s", "fans drawn on " + ", ".join(shs),
                           "not established", "drawn, not compared",
                           "Query: fan drawn, link to the unit not read; fans of this unit are drawn on " + ", ".join(shs), True, shs[0], 1, None,
                           u["source"], f"The fans {fans} are drawn on {', '.join(shs)}. The AHU tag is not read on a working sheet. "
                                        "No missing-system row is raised."))
            info["counts"]["drawn, not compared"] += 1
            return
        # Fix after verification (critical 4, D-079): a unit that an App D new drawing of this set already draws as equipment has
        # its home there.  Only the rows that were instructions ('New system: draw on a sheet') change; a unit held by the gate
        # stays the query it was.  The row refers to that drawing; no second drawing is asked for, and no room is added.
        appd_home = self.appd_new_drawing_home(keys) if (self.gate(u)["ok"] and not u.get("zoning_hold")) else []
        if appd_home:
            rows.append(mk(tag, "queries", "AHU total", tag, "Unit drawn on a new drawing", "L/s", "drawn on " + ", ".join(appd_home),
                           "not established", "drawn on new drawing",
                           "Query: unit is drawn on the App D new drawing " + ", ".join(appd_home) + "; no second drawing", True, "NEW", 1, None,
                           u["source"], "The new drawing " + ", ".join(appd_home) + " draws this unit as equipment. "
                                        "The unit has one home there. No sheet is added and no change is marked up for this unit."))
            info["counts"]["drawn, not compared"] += 1
            return
        tender = [e for k in keys for e in self.tender_tag_keys.get(k, [])]
        nsheets = sorted({e["sheet"] for e in tender})
        zs = set()
        for lk in u["links"]:
            zs.update(re.findall(r"IP-BH-\d{5}", lk.get("source_ref") or ""))
        belongs = nsheets or sorted(zs)
        if tender:
            where = "The unit is on tender sheet " + ", ".join(nsheets) + " and on no working sheet."
        elif u["type"] == "FCU":
            where = "No sheet shows this FCU."
        else:
            where = "No working sheet shows this unit."
        needed = ("Draw the unit with its rooms, outdoor-air source, return and exhaust. "
                  "Belongs on " + (("sheet " + ", ".join(belongs)) if belongs else
                                   "a new sheet of the " + str(u["storeys"]) + " air-side series") + ".")
        m = self.i.master.get(tag)
        req = m["Calculated airflow"] if m else None
        g = self.gate(u)
        srcm = u["source"] + ("; Workbook master summary, tab AHU Master, row %s" % m["_row"] if m else "")
        if g["ok"] and not u.get("zoning_hold"):
            mr = mk(tag, "new systems", "Missing system", tag, "Unit with no schematic", "L/s", "not drawn", req, "missing system",
                    "New system: draw on a sheet", True, belongs[0] if belongs else "NEW", 1, None, srcm, where + " " + needed)
        else:
            mr = mk(tag, "queries", "Missing system", tag, "Unit with no schematic", "L/s", "not drawn", None, "missing system",
                    ("Query: unit has no schematic; held for a zoning decision" if u.get("zoning_hold") else
                     "Query: unit has no schematic; required value not established - room list"), True,
                    belongs[0] if belongs else "NEW", 1, None, srcm,
                    where + " " + needed + " " + (self.gate_note(req, "L/s", [g]) if not g["ok"] else "The unit is held for a zoning decision."),
                    **self.gate_extra([g], req))
        rows.append(mr)
        self.missing.append(mr)
        info["counts"]["missing system"] += 1
        zr = self.zone_rooms(u)
        for n, v in sorted(zr.items()):
            rows.append(mk(tag, "new systems", "Room", v["name"], "Rooms in the zone list", "no.", "not drawn", v["count"],
                           "to add", "Add with the new system", False, "", "", None,
                           "Zone schedule room list; " + (v["links"][0].get("source_ref") or "")[:160]))
            info["counts"]["to add"] += 1

    def appd_new_drawing_home(self, keys: Any) -> List[str]:
        """New drawings of this set (App D layouts) that draw a unit tag as equipment, read from the coverage files of the
        new drawings (build line 24: the working sheet wins; a new drawing is the home of an App D unit)."""
        if not hasattr(self, "_appd_home"):
            import glob as _glob
            self._appd_home: Dict[str, set] = defaultdict(set)
            for f in sorted(_glob.glob(str(ROOT / "out" / "schematics" / "new_drawings" / "appd_*_coverage.csv"))):
                for r in read_csv(Path(f)):
                    if r.get("kind") == "App D layout tag" and str(r.get("reason", "")).startswith("drawn once"):
                        for form in [r["item"]] + re.split(r"\s*\|\s*", r.get("schedule_form") or ""):
                            for k in tag_keys(form):
                                self._appd_home[k].add(r["drawing"])
        return sorted({d for k in keys for d in self._appd_home.get(k, ())})

    def pahu_raw(self, k: str) -> str:
        """The PAHU tag as the source writes it (for example PAHU-B-B1-B1AHU-01A), not the key of the tag."""
        if not hasattr(self, "_praw"):
            self._praw: Dict[str, str] = {}
            for s in self.i.working:
                for t in TAG_RE.findall(s["outdoor_air"].get("source") or ""):
                    for kk in sorted(tag_keys(t)):
                        self._praw.setdefault(kk, t)
                for ln in s["outdoor_air"].get("lines", []):
                    for t in TAG_RE.findall(ln.get("from") or ""):
                        for kk in sorted(tag_keys(t)):
                            self._praw.setdefault(kk, t)
            for sh, tl in self.tags_on_sheets().items():
                if sh.startswith("PAHU:"):
                    self._praw.setdefault(sh, tl[0][2])
        return self._praw.get(k, "PAHU-" + key_only(k))

    def tags_on_sheets(self) -> Dict[str, List[Tuple[str, List[float], str]]]:
        """Every unit tag that a working sheet prints: tag key -> (sheet, box, text), read from the text of the sheets."""
        if not hasattr(self, "_tos"):
            self._tos: Dict[str, List[Tuple[str, List[float], str]]] = defaultdict(list)
            for sh, v in sorted(self.i.model["sheets"].items()):
                if v.get("issue") != "working drawing":
                    continue
                for t, b in self.i.st.lines(sh):
                    for m in re.finditer(r"(?:PAHU|AHU|FCU|CRAC)-[A-Z0-9][A-Za-z0-9\-]*(?:/[A-Z0-9]+)?(?:\s[A-D](?![A-Za-z0-9]))?", t):
                        for k in sorted(tag_keys(m.group(0))):
                            self._tos[k].append((sh, b, m.group(0)))
        return self._tos

    # item 1 and 2 -----------------------------------------------------------------
    def room_entity(self, sheet: str, box: Optional[Sequence[float]]) -> Optional[Dict[str, Any]]:
        if not hasattr(self, "_room_ent"):
            self._room_ent = {(r["sheet"], tuple(r["_box"] or ())): r for r in self.led.rows if r["kind"] == "room box"}
            self._room_val = {(r["parent_id"], r["quantity"]): r for r in self.led.rows if r["kind"] == "room value"}
        return self._room_ent.get((sheet, tuple(round(float(x), 1) for x in (box or ()))))

    def room_value_entity(self, ent: Optional[Dict[str, Any]], q: str) -> Optional[Dict[str, Any]]:
        if ent is None:
            return None
        self.room_entity("", None)
        return self._room_val.get((ent["entity_id"], q))

    def one_to_one_link(self, z: Dict[str, Any]) -> bool:
        """The zoned room has exactly one row in room_unit_links.csv for this unit and nothing else shares the room."""
        if z["count"] != 1 or len(z["links"]) != 1:
            return False
        lk = z["links"][0]
        if not hasattr(self, "_link_count"):
            self._link_count = Counter((l["dept"], l["dpa_room_id"]) for l in self.i.links)
        return self._link_count[(lk["dept"], lk["dpa_room_id"])] == 1

    def room_on_working_sheet(self, zname: str, zdisp: str, storey: str) -> bool:
        """Ledger lookup on the union of the working sheets of the storey: a room box with this name (D-076, step 15)."""
        if not hasattr(self, "_rooms_by_storey"):
            self._rooms_by_storey = defaultdict(list)
            for r in self.led.rows:
                if r["kind"] == "room box":
                    st = self.i.model["sheets"].get(r["sheet"], {}).get("storey")
                    self._rooms_by_storey[st].append(r["text"])
        for t in self._rooms_by_storey.get(storey, []):
            if canon(t) == zname or plan_matches(zdisp, t) or plan_matches(t, zdisp):
                return True
        return False

    def rooms_block(self, u: Dict[str, Any], rows: List[Row], d054: bool, info: Dict[str, Any]) -> None:
        tag = u["tag"]
        drawn = self.drawn_rooms(u)
        zone = self.zone_rooms(u)
        plan = self.plan_for(u)
        sheet, page, tagbox = self.sheet_of(u)
        complete = u["complete"]
        g = self.gate(u)
        storey = u["systems"][0]["storey"] if u["systems"] else ""
        fam0 = self.family(u)
        zother: Dict[str, List[Any]] = {}
        for dn, d in drawn.items():
            fam = self.family(u, d["tagkeys"])
            hold = [h for h in self.zone_names.get(storey, {}).get(dn, []) if h[0] not in fam]
            if hold:
                zother[dn] = hold
        dother = {n: [t for t in ts if t not in fam0] for n, ts in self.drawn_names.get(storey, {}).items()}
        sheet_names: Dict[str, Any] = {}
        for sh in {x["sheet"] for x in u["systems"]}:
            for cn, tk, tg in self.sheet_boxes.get(sh, []):
                if not (tg & u["akeys"]):
                    sheet_names[cn] = tk
        if not zone:
            res = [dict(cls="not established", dname=n, zname=None, dc=v["count"], zc=None,
                        why="the zone schedule gives no room to this unit") for n, v in sorted(drawn.items())]
        else:
            res = classify_rooms(drawn, zone, complete, zother, dother, plan, sheet_names)
        info["zone_keys"] = set(zone)
        info["classed_keys"] = {r["zname"] for r in res if r["zname"] is not None}
        hold = bool(u.get("zoning_hold"))
        for r in res:
            d = drawn.get(r["dname"]) if r["dname"] is not None else None
            z = zone.get(r["zname"]) if r["zname"] is not None else None
            cls = r["cls"]
            rm = d["rm"] if d else None
            bx = rm["name"]["box"] if rm else tagbox
            sh = d["sheet"] if d else sheet
            pg = d["page"] if d else page
            dtext = dedupe_words(d["name"] or "no name read on the box") if d else "not drawn"
            ztext = self.long_name(z) if z else "not in zone list"
            item = dtext if d else ztext
            note = ""
            if cls in ("name differs", "renamed"):
                note = f"Drawn name: {dtext}. Zone name: {ztext}."
            elif cls == "named in box":
                note = f"The box shows several room names: {dtext}. It names this room: {ztext}."
            elif cls == "moved to another unit":
                note = f"The zone list gives this room to {r['other']}."
            elif cls == "moved from another unit":
                note = f"The room is drawn on {r['other']}."
            elif cls == "drawn without tag":
                note = "A box with this name is on the sheet. It does not carry the tag of this unit."
            elif r.get("why"):
                note = r["why"][0].upper() + r["why"][1:] + "."
            if r.get("plan"):
                note = f"The zone assignment gives the plan room {r['plan']} to this unit."
            if cls == "typical count changed" and r.get("ratio") is not None and r["dname"] != r["zname"]:
                note = f"Drawn name: {dtext}. Zone name: {ztext}. " + note
            if cls in ("to add", "moved from another unit") and z:
                cited = set()
                for lk in z["links"]:
                    cited |= set(re.findall(r"IP-BH-[0-9A-Z]{5}", lk.get("source_ref") or ""))
                mine = {x["sheet"] for x in u["systems"]}
                if cited and not (cited & mine):
                    cls = "drawn on another sheet"
                    note = "The zone list takes this room from the sheet " + ", ".join(sorted(cited)) + ". It is not drawn on this sheet."
            act, need = ROOM_ACTION[cls]
            lay = "queries" if cls in ("not established", "drawn without tag") else "rooms"
            extra: Dict[str, Any] = {}
            # D-076 / D-084: an added room is drawn only for a room absent from every working sheet, with a one-to-one
            # zoning link to this unit, and a unit that passes the gate; a room on another unit is one 'moved' query
            if cls == "to add":
                un_box_sheets = {us["sheet"] for us in self.i.unassigned if us["rooms"]}
                ok_add = (z is not None and self.one_to_one_link(z) and g["ok"] and
                          not self.room_on_working_sheet(r["zname"], (z or {}).get("name", ""), storey) and not hold)
                if ok_add and sheet in un_box_sheets:
                    # correction pass (Opus check item 1): the sheet draws boxes that no unit owns (the tag sits above a box column,
                    # not on the box).  The zoned room may be one of them; the add is a link to confirm, not an instruction.
                    ok_add = False
                    cls, act, need, lay = "link to confirm", "Query: link to confirm", True, "queries"
                    note = ("The zone list gives the room to this unit. The sheet draws room boxes that carry no unit tag, and the box of this "
                            "unit may already be one of them (the unit tag is printed above a column of boxes). No box is added.")
                elif not ok_add:
                    why = []
                    if z is not None and not self.one_to_one_link(z):
                        why.append("the zoning link of the room to this unit is not one-to-one")
                    if not g["ok"]:
                        why.append("the room list of the unit does not establish the unit (" + "; ".join(g["reasons"]) + ")")
                    if z is not None and self.room_on_working_sheet(r["zname"], z.get("name", ""), storey):
                        why.append("a room box with this name is on a working sheet of the storey")
                    if hold:
                        why.append("the unit is held for a zoning decision")
                    cls = "to add: not established"
                    act, need, lay = "Query: room in the zone list is not drawn; add not established", True, "queries"
                    note = ("The zone list gives the room to this unit and no box shows it here. No box is added: " +
                            "; ".join(why or ["the conditions for an added room are not met"]) + ".")
                else:
                    lk0 = z["links"][0]
                    wr = self.wb_room.get((self.dept2(lk0), lk0["dpa_room_id"]))
                    extra = dict(room_no=lk0["dpa_room_id"], dept=self.dept2(lk0))
                    if wr:
                        ps = self.i.wb[self.dept2(lk0)]["pressure"].get(lk0["dpa_room_id"])
                        extra.update(supply=wr["supply"] if wr["supply"] is not None else "",
                                     **{"return": wr["return"] if wr["return"] is not None else ""},
                                     exhaust=wr["exhaust"] if wr["exhaust"] is not None else "",
                                     pressure=(ps or {}).get("set", "") if ps and ps.get("set") is not None else "",
                                     terminal=wr.get("terminal") or "", mode="normal")
            elif cls == "to remove":
                if not (g["ok"] and not hold):
                    cls = "not established"
                    act, need, lay = "Query: room is drawn and not in the zone list; room list not established", True, "queries"
                    note = "The box is on the sheet and the zone list does not hold it. The room list of the unit is not established. The box stays."
            elif cls == "moved to another unit":
                act, need, lay = f"Query: room drawn on this unit; the zoning gives it to {r.get('other')}", True, "queries"
            elif cls == "moved from another unit":
                act, need, lay = "No separate row: see the 'moved' query on the tab of the unit that draws the room", False, "rooms"
            elif cls == "typical count changed" and hold:
                act, need, lay = "Query: held for a zoning decision; typical count not changed", True, "queries"
            elif cls == "typical count changed":
                # Fix after verification (critical 2): the count comes from a shortened-name match that the sheet does not
                # confirm (sister boxes drawn separately, boxes drawn twice with 'TYPICAL').  Held as a query; drawn count stays.
                cls = "typical count to confirm"
                act, need, lay = "Query: typical count to confirm; drawn count kept", True, "queries"
                note = ((note + " ") if note else "") + (
                    f"The zone list gives {r['zc']} rooms for this name; the box shows {r['dc']}. The match uses a shortened room name, "
                    "and the sheet may draw sister boxes or the same box twice. Count the boxes on the sheet. The drawn count stays.")
            src = (f"Working drawing {sh} p{pg}, room box" if d else "") + \
                  ("; " if d and z else "") + \
                  (short(clean(z["links"][0].get("source_ref") or "Zone schedule room list"), 140) if z else "")
            zreq = r["zc"] if (r["zc"] is not None and not (cls == "not established" and not z)) else "not established"
            if r.get("plan"):
                zreq = r["dc"]
            ent = self.room_entity(sh, bx) if d else None
            rows.append(mk(tag, lay, "Room", item, "Typical count", "no.", r["dc"] if d else "not drawn",
                           zreq, cls, act, need, sh, pg, bx, src, note, ledger_id=ent["entity_id"] if ent else "", **extra))
            rows[-1]["_ent"] = ent
            rows[-1]["_ents"] = [x for x in (self.room_entity(b_[0], b_[1]) for b_ in (d.get("boxes") or [])) if x] if d else []
            info["counts"][cls] += 1
            if d and z and cls in ("unchanged", "typical count changed", "typical count to confirm", "name differs", "not established") \
                    and not d.get("composite"):
                self.airflow_rows(u, d, z, rows, d054, info, ent, hold)

    @staticmethod
    def dept2(lk: Dict[str, str]) -> str:
        return f"{int(lk['dept']):02d}" if str(lk["dept"]).isdigit() else str(lk["dept"])

    _DEPT_WORDS: Dict[str, set] = {}

    def dept_differs(self, u: Dict[str, Any], depts: set) -> bool:
        """True when the department of the workbook room shares no word (over 3 letters) with the departments of the unit
        and the unit is not a common-area unit (the same word test as check P4-19)."""
        if not self._DEPT_WORDS:
            from pathlib import Path
            for p in (Path(__file__).resolve().parents[2] / "out" / "loads" / "workbooks").glob("AIGH_C2_D*_*.xlsx"):
                m = re.match(r"AIGH_C2_D(\d\d)_(.+?)_Thermal_Load_Calc", p.name)
                if m:
                    self._DEPT_WORDS[m.group(1)] = set(w for w in re.sub(r"[^a-z0-9]+", " ", m.group(2).lower()).split() if len(w) > 3)
        ud = re.sub(r"[^a-z0-9]+", " ", str(u.get("depts", "")).lower())
        if not ud.strip() or "common" in ud:
            return False
        uw = set(ud.split())
        return any(self._DEPT_WORDS.get(d_) and not (self._DEPT_WORDS[d_] & uw) for d_ in depts)

    def airflow_rows(self, u: Dict[str, Any], d: Dict[str, Any], z: Dict[str, Any], rows: List[Row],
                     d054: bool, info: Dict[str, Any], ent: Optional[Dict[str, Any]], hold: bool) -> None:
        tag = u["tag"]
        md = d["mode"]
        if not md:
            return
        wbr = []
        for lk in z["links"]:
            dept = self.dept2(lk)
            w = self.wb_room.get((dept, lk["dpa_room_id"]))
            if w:
                wbr.append((dept, lk["dpa_room_id"], w))
        sheet, page = d["sheet"], d["page"]
        name = d["name"]
        depts = {dp for dp, _, _ in wbr}
        # link of the box to the workbook rooms (D-084): by room number, or one-to-one by name on this unit, or by name only
        by_number = any(re.search(re.escape(str(rn)), str(name)) for _, rn, _ in wbr)
        one_to_one = d["count"] == 1 and len(z["links"]) == 1 and len(wbr) == 1 and canon(name) == canon(z["name"]) \
            and self.one_to_one_link(z)
        link_type = "room number" if by_number else ("one-to-one name" if one_to_one else "name only")
        ded_exh = [w for _, _, w in wbr if (w["exhaust"] or 0) > 0.5 and not (w["return"] or 0) > 0.5]
        dedicated = bool(wbr) and len(ded_exh) == len(wbr)
        quantities = [("Supply airflow", "supply_L_s", "supply", "Final supply"),
                      ("Return airflow", "return_L_s", "return", "Return"),
                      ("Exhaust airflow", "exhaust_L_s", "exhaust", "Exhaust")]
        # Fix after verification (critical 5, D-081): a room that the sheet prints with a slight pressure regime ('SLIGHT -VE',
        # 'SLIGHT +VE') keeps its drawn supply and exhaust offset.  A mark-up that sets supply equal to exhaust removes it.
        _pr = str((d["rm"].get("pressure") or {}).get("value") or "").upper()
        slight = bool(re.search(r"SLIGHT", _pr) and re.search(r"[-+\u2212]\s*VE", _pr))
        _sv = [w["supply"] for _, _, w in wbr if w["supply"] is not None]
        _ev = [w["exhaust"] for _, _, w in wbr if w["exhaust"] is not None]
        _ds, _de = (md.get("supply_L_s") or {}).get("value"), (md.get("exhaust_L_s") or {}).get("value")
        slight_equal = (slight and _sv and _ev and abs(sum(_sv) / len(_sv) - sum(_ev) / len(_ev)) < 0.5
                        and _ds is not None and _de is not None and abs(_ds - _de) > 0.5)
        for qn, mk_key, wk, col in quantities:
            dv = md.get(mk_key) or {}
            drawn_v = dv.get("value")
            bx = dv.get("box") or d["rm"]["name"]["box"]
            vals = [w[wk] for _, _, w in wbr if w[wk] is not None]
            req = (sum(vals) / len(vals)) if vals else None
            if req is not None:
                req = round(req, 1)
            qkey = wk
            vent = self.room_value_entity(ent, qkey)
            if dedicated and wk == "return":
                # a room with dedicated exhaust states exhaust, not return (D-031)
                if drawn_v is not None and drawn_v > 0.5:
                    ex = sum(w["exhaust"] or 0 for w in ded_exh) / max(len(ded_exh), 1)
                    rows.append(mk(tag, "queries", "Room airflow", name, qn, "L/s", drawn_v, None, "dedicated exhaust",
                                   "Query: the sheet draws return air; the workbook gives dedicated exhaust of this room", True,
                                   sheet, page, bx,
                                   f"Working drawing {sheet} p{page}; Workbook D{wbr[0][0]} tab Airflow cell {wbr[0][2]['cols'].get('Exhaust', '')} (room {wbr[0][1]})",
                                   f"The workbook gives exhaust of {ex:,.0f} L/s and no return for this room. No return value is compared.",
                                   ledger_id=vent["entity_id"] if vent else "", link_type=link_type))
                    info["counts"]["dedicated exhaust"] += 1
                continue
            if dedicated and wk == "exhaust" and drawn_v is None:
                continue
            if drawn_v is None and (req is None or req <= 0.5):
                continue
            if drawn_v is None and not vent:
                # nothing drawn to anchor a value on: the row stays in the list, not on the drawing
                pass
            cls, act, need = judge(drawn_v, req, d054)
            note = f"Per room. Rooms drawn: {d['count']}; rooms in workbook: {len(wbr)}."
            lo = hi = None
            if len(vals) > 1:
                lo, hi = min(vals), max(vals)
                note += f" The required value is the average of {len(vals)} workbook rooms ({lo:,.0f} to {hi:,.0f} L/s); check by a person."
                if req and (hi - lo) > 0.25 * req and drawn_v is not None:
                    kinds = {judge(drawn_v, v, d054)[0] for v in (lo, req, hi)}
                    if len(kinds) > 1:
                        cls, act, need = ("linked rooms differ", "Query: linked workbook rooms differ; check which rooms the box stands for", True)
            ratio = (req / drawn_v) if (req is not None and drawn_v) else None
            if cls in ("increase", "reduction", "not drawn") and need:
                if len(depts) > 1:
                    cls, act, need = "link to confirm", "Query: link to confirm", True
                    note += " The workbook rooms linked to this box are from different departments."
                elif link_type == "name only" and self.dept_differs(u, depts):
                    cls, act, need = "link to confirm", "Query: link to confirm", True
                    note += " The workbook room is of another department than the unit (link by name only)."
                elif link_type == "name only" and ratio is not None and (ratio > 2 or ratio < 0.5):
                    cls, act, need = "link to confirm", "Query: link to confirm", True
                    note += (f" The box links to the workbook room by name only. Workbook {req:,.0f} L/s against drawn "
                             f"{drawn_v:,.0f} L/s is {ratio:.1f} times.")
                elif link_type == "one-to-one name" and cls == "increase" and ratio is not None and ratio > 2:
                    # correction pass (Opus check item 3): the boxes print no room number, so the link is by name.  Only a
                    # number-matched room above 2 times stays a mark-up (D-084, build line 14).
                    cls, act, need = "link to confirm", "Query: link to confirm", True
                    note += (f" The box links to the workbook room by name; the box prints no room number. Workbook {req:,.0f} L/s "
                             f"against drawn {drawn_v:,.0f} L/s is {ratio:.1f} times.")
                elif link_type == "room number" and ratio is not None and ratio > 2:
                    note += f" The workbook value is {ratio:.1f} times the drawn value (room matched by number)."
                elif cls == "not drawn" and link_type == "name only":
                    cls, act, need = "link to confirm", "Query: link to confirm", True
            if cls == "not drawn" and act.startswith("Mark up: show") and need:
                # correction pass (Opus check items 5 and 8; build line 19): 'show the required value' is an instruction only for a
                # valueless label that the sheet draws, on a unit that passes the gate, and not against a drawn exhaust.
                gg = self.gate(u)
                drawn_ea = (md.get("exhaust_L_s") or {}).get("value")
                why_q = []
                if not dv.get("box"):
                    why_q.append(f"the sheet draws no {qn.split()[0].lower()} label for this room, so there is no valueless label to show")
                elif not gg["ok"]:
                    why_q.append("the room list of the unit does not establish the unit (" + "; ".join(gg["reasons"]) + ")")
                if wk == "return" and drawn_ea is not None and drawn_ea > 0.5:
                    why_q.append(f"the sheet draws exhaust of {drawn_ea:,.0f} L/s for this room and the workbook gives return (an exhaust room states exhaust, not return)")
                if why_q:
                    cls, act, need = "required value not shown", "Query: the sheet and the workbook differ for this room; no value is added", True
                    note += " No value is added: " + "; ".join(why_q) + ". The drawn value stays."
            if slight_equal and wk in ("supply", "exhaust") and need and act.startswith("Mark up"):
                cls, act, need = "offset to check", "Query: workbook sets supply equal to exhaust; the sheet draws a slight pressure regime", True
                note += (f" The sheet prints '{_pr}' for this room and draws supply {_ds:,.0f} and exhaust {_de:,.0f} L/s. The workbook "
                         f"gives {req:,.0f} L/s for supply and for exhaust, which removes the drawn pressure offset. The drawn value stays. "
                         "Workbook input to check (D-081).")
            if hold and need and act.startswith("Mark up"):
                cls, act, need = "held for zoning decision", "Query: held for a zoning decision; value not changed", True
            if req is None:
                src = f"Working drawing {sheet} p{page}; no workbook value for this room"
            else:
                src = f"Working drawing {sheet} p{page}; " + "; ".join(
                    f"Workbook D{dp} tab Airflow cell {w['cols'].get(col, '')} (room {rn})" for dp, rn, w in
                    [x for x in wbr if x[2][wk] is not None][:12]) + \
                    (". Formula: average of the cells listed" if len(vals) > 1 else "")
            if d["other_modes"]:
                note += " Other modes drawn (" + ", ".join(d["other_modes"]) + ") are not compared."
            if cls == "margin" and req is not None and drawn_v:
                note += f" Drawn value exceeds the requirement by {drawn_v - req:,.0f} L/s."
            if cls == "increase" and drawn_v:
                dif = req - drawn_v
                p5, p10 = dif > INC_PCT * abs(drawn_v), dif > INC_LPS
                if p5 != p10:
                    note += (" The increase passes the 5 % test only." if p5 else " The increase passes the 10 L/s test only.") + \
                            " The reading of the update rule is to be confirmed."
                    self.one_test_rows += 1
            lay = "airflows" if cls not in ("not established", "linked rooms differ", "link to confirm", "held for zoning decision", "required value not shown", "offset to check") else "queries"
            rows.append(mk(tag, lay, "Room airflow", name, qn, "L/s", drawn_v, req, cls, act, need, sheet, page, bx, src, note,
                           ledger_id=vent["entity_id"] if vent else "", link_type=link_type,
                           room_no=wbr[0][1] if len(wbr) == 1 else "", dept=wbr[0][0] if len(wbr) == 1 else ""))
            info["counts"][cls] += 1
        # pressure (D-081): Pa against Pa, the room's own row; always a query to the workbook; offset air in the note only
        prs = []
        for dp, rn, _ in wbr:
            p = self.i.wb[dp]["pressure"].get(rn)
            if p and p["set"] is not None:
                prs.append((dp, rn, p))
        prraw = (d["rm"].get("pressure") or {}).get("value")
        pdrawn = parse_pressure(prraw)
        psign = pressure_sign(prraw) if pdrawn is None else None
        if prs and (pdrawn is not None or psign is not None):
            sets = sorted({p["set"] for _, _, p in prs})
            dp0, rn0, p0 = prs[0]
            setp = p0["set"]
            pb = (d["rm"].get("pressure") or {}).get("box")
            pent = self.room_value_entity(ent, "pressure")
            woff = [w["offset"] for _, _, w in wbr if w["offset"] is not None]
            cls, act, need = "no change", "No change", False
            note = ""
            if len(sets) > 1:
                cls, act, need = "linked rooms differ", "Query: pressure of the linked workbook rooms differs; drawn value stays", True
                note = "The workbook rooms linked to this box have different set pressures: " + ", ".join(f"{x:+,.0f} Pa" for x in sets) + ". "
                setp = None
            elif pdrawn is not None and abs(pdrawn) > PRESSURE_MAX:
                cls, act, need = "drawn value to check", "Query: drawn value to check (possible misread)", True
                note = f"The drawn value {pdrawn:+,.0f} Pa is above {PRESSURE_MAX:.0f} Pa. It is not corrected. "
            elif pdrawn is not None and pdrawn != 0 and setp != 0 and pdrawn * setp < 0:
                cls, act, need = "pressure direction conflict", "Query: pressure direction conflict; drawn value stays", True
                note = "The drawn regime and the workbook set pressure have opposite signs. "
            elif pdrawn is not None and abs(pdrawn - setp) > 0.5:
                cls, act, need = "differs", "Query: drawn pressure differs from the workbook; drawn value stays", True
            elif psign is not None:
                if setp != 0 and psign * setp > 0:
                    pass
                elif setp != 0:
                    cls, act, need = "pressure direction conflict", "Query: pressure direction conflict; drawn value stays", True
                    note = f"The sheet gives the regime in words ({'negative' if psign < 0 else 'positive'}). The workbook sets {setp:+,.0f} Pa. "
                else:
                    cls, act, need = "differs", "Query: drawn pressure differs from the workbook; drawn value stays", True
            note += "Offset air needed: " + (f"{woff[0]:,.0f} L/s" if woff else "not given") + \
                    " (Pressure tab). It is listed here only. No airflow is put on the room box."
            if need and len(sets) == 1 and cls not in ("drawn value to check",):
                pass
            rows.append(mk(tag, "queries" if need else "airflows", "Room airflow", name, "Pressure target", "Pa",
                           pdrawn if (pdrawn is not None and re.search(r"\d", str(prraw))) else prraw,
                           setp if setp is not None else "not established", cls, act, need,
                           sheet, page, pb or d["rm"]["name"]["box"],
                           f"Working drawing {sheet} p{page}; Workbook D{dp0} tab Pressure cell {p0['cell']} (room {rn0}, row {p0['row']})",
                           note, ledger_id=pent["entity_id"] if pent else "", link_type=link_type, room_no=rn0, dept=dp0,
                           pressure=setp if setp is not None else ""))
            info["counts"][cls] += 1

    # item 3 ---------------------------------------------------------------------
    def duty_entity(self, sheet: str, box: Optional[Sequence[float]]) -> Optional[Dict[str, Any]]:
        if not hasattr(self, "_duty_ent"):
            self._duty_ent = {(r["sheet"], tuple(r["_box"] or ())): r for r in self.led.rows
                              if r["kind"] in ("duty", "spill or relief label", "OA value")}
        return self._duty_ent.get((sheet, tuple(round(float(x), 1) for x in (box or ()))))

    def room_table_boxes(self, sheet: str) -> List[List[float]]:
        if not hasattr(self, "_tbl"):
            self._tbl = defaultdict(list)
            for r in self.led.rows:
                if r["kind"] == "room box" and r.get("_rm") and r["_rm"].get("box_table"):
                    self._tbl[r["sheet"]].append(r["_rm"]["box_table"])
        return self._tbl.get(sheet, [])

    def oa_anchor(self, sheet: str, value: Optional[float], near: Optional[Sequence[float]]) -> Optional[Dict[str, Any]]:
        """The drawn outdoor-air value box (build line 16): the value inside the note line, or the duct label.  A line
        in a room table is a room value and is not an anchor.  Never the PAHU tag or the note as a whole."""
        if value is None:
            return None
        cands = []
        for box, kind in self.i.st.oa_value_boxes(sheet, value):
            if any(_overlap(box, t) > 0.8 for t in self.room_table_boxes(sheet)):
                continue
            cands.append((box, kind))
        if not cands:
            return None
        box, kind = min(cands, key=lambda c: SheetText._dist(c[0], list(near) if near else None))
        ent = self.duty_entity(sheet, box)
        return {"box": box, "kind": kind, "entity": ent}

    def oa_extras(self, wrows: List[Tuple[str, Dict[str, Any]]], mast: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """The room that sets the outdoor-air fraction, the fraction and the sum of room outdoor air (build line 16)."""
        best = None
        for dp, r in wrows:
            if r.get("oa_fraction") is not None and (best is None or r["oa_fraction"] > best[1]["oa_fraction"]):
                best = (dp, r)
        if not best:
            return {}
        sm = sum(r["oa_sum_rooms"] for _, r in wrows if r.get("oa_sum_rooms") is not None)
        return {"oa_room": str(best[1].get("oa_room") or ""), "oa_fraction": round(best[1]["oa_fraction"], 3),
                "oa_room_sum": round(sm, 1)}

    def owned_groups(self, u: Dict[str, Any], fn: Optional[str] = None) -> List[Dict[str, Any]]:
        return [g for g in self.groups_by_unit.get(u["tag"], []) if fn is None or g["function"] == fn]

    def totals_block(self, u: Dict[str, Any], rows: List[Row], d054: bool, info: Dict[str, Any]) -> None:
        tag = u["tag"]
        sheet, page, tagbox = self.sheet_of(u)
        g = self.gate(u)
        hold = bool(u.get("zoning_hold"))
        wrows = self.wb_ahu_rows(u)
        mast, alias_note = self.master_for(u)
        keys = u["akeys"]
        gmast = None
        if not (mast and mast.get("Calculated airflow")):
            gmast = next((x for x in u["groups"] if self.i.master.get(x["tag"], {}).get("Calculated airflow")), None)
            if gmast is not None:
                mast = None

        def sums(attr: str) -> Optional[float]:
            v = [r[attr] for _, r in wrows if r[attr] is not None]
            return sum(v) if v else None

        sup_req = (mast or {}).get("Calculated airflow") if mast else sums("supply")
        sup_req = sup_req or None
        oa_req = (mast or {}).get("AHU outdoor air = fraction × calculated airflow") if mast else sums("oa")
        have_wb = bool(sup_req)
        recirc, spill_w = sums("recirc"), sums("spill")
        exh_w = sums("exhaust")
        ret_req = (recirc or 0) + max(spill_w or 0, 0) if have_wb and recirc is not None else None
        spill_req = max(spill_w, 0.0) if (have_wb and spill_w is not None) else None
        coil_req = sums("coil") if have_wb else None
        info["req"] = {"supply": sup_req, "oa": oa_req, "return": ret_req, "spill": spill_req, "coil": coil_req,
                       "exhaust": exh_w, "recirc": recirc}
        src_wb = "; ".join(f"Workbook D{dp} tab AHU row {r['row']}" for dp, r in wrows[:3]) or "no workbook row"
        src_m = f"Workbook master summary tab AHU Master row {mast['_row']}" if mast else ""

        def cells(kind: str, master_key: str = "") -> str:
            """The cells that hold the required value, and the formula where there are several (D-081, P4-14)."""
            if mast and master_key and master_key in mast.get("_cells", {}):
                return f"Workbook master summary, tab AHU Master, cell {mast['_cells'][master_key]}"
            if not wrows:
                return "no workbook row"
            if kind == "ret":
                txt = "; ".join(f"Workbook D{dp} tab AHU cells {r['cols']['recirc']} and {r['cols']['spill']}" for dp, r in wrows[:8])
                return txt + ("" if len(wrows) == 1 else "") + ". Formula: recirculated air + spill air (a spill value below 0 counts as 0)"
            col = r_col = {"supply": "supply", "oa": "oa", "spill": "spill"}[kind]
            txt = "; ".join(f"Workbook D{dp} tab AHU cell {r['cols'][r_col]}" for dp, r in wrows[:8])
            f_ = ""
            if len(wrows) > 1:
                f_ = ". Formula: sum of the cells"
            if kind == "spill":
                f_ += (". " if f_ else ". ") + "Formula: a value below 0 counts as 0"
            return txt + f_
        sup_items, oa_items, spill_vals = [], [], []
        for s in u["systems"]:
            for a in s["ahus"]:
                if tag_keys(a["tag"]["value"]) & keys:
                    sup_items.append((a["tag"]["value"], a["duty_supply_L_s"]["value"], a["duty_supply_L_s"]))
            for ln in s["outdoor_air"]["lines"]:
                if tag_keys(ln["to"]) & keys:
                    atag = next((a["tag"].get("box") for a in s["ahus"] if tag_keys(a["tag"]["value"]) & tag_keys(ln["to"])), None)
                    anc = self.oa_anchor(s["sheet"], ln["value_L_s"], atag)
                    ref = dict(ln)
                    ref["_anchor"] = anc
                    ref["_sheet"] = s["sheet"]
                    ref["_page"] = s["page"]
                    oa_items.append((ln["to"], ln["value_L_s"], ref))
            for ln in s["spill_relief"]["lines"]:
                spill_vals.append((ln["value_L_s"], ln))
        spill_path_drawn = bool(spill_vals)
        valueless_spill = None
        if u["systems"]:
            best = None
            for s in u["systems"]:
                for v, b in self.i.st.spill_labels(s["sheet"]):
                    d = SheetText._dist(b, tagbox)
                    if best is None or d < best[0]:
                        best = (d, v, b, s["sheet"])
            if best:
                if not spill_vals:
                    spill_path_drawn = True
                    if best[1] is not None:
                        spill_vals.append((best[1], {"box": best[2]}))
                    else:
                        valueless_spill = best
        sup_d, sup_refs = self._per_key_sum(sup_items, keys)
        oa_d, oa_refs = self._per_key_sum(oa_items, keys)
        spill_d = sum(v for v, _ in spill_vals if v is not None) if any(v is not None for v, _ in spill_vals) else None
        ret_groups = self.owned_groups(u, "RAF")
        ret_d = self.group_drawn_sum(ret_groups)
        info["drawn"] = {"supply": sup_d, "oa": oa_d, "return": ret_d, "spill": spill_d}
        once_through = bool(oa_d and sup_d and abs(sup_d - oa_d) <= INC_PCT * sup_d)
        info["once_through"] = once_through
        gates = [g]

        def bx(refs):
            return (refs[0].get("box") if refs else None) or tagbox

        def put(qty: str, uom: str, drawn: Any, req: Optional[float], box: Any, lid: str, src: str, extra: str,
                kind: str = "", ex: Optional[Dict[str, Any]] = None) -> None:
            ex = ex or {}
            if req is None and drawn is None:
                return
            if req is None and gmast is not None:
                rows.append(mk(tag, "airflows", "AHU total", tag, qty, uom, drawn if drawn is not None else "not drawn", "held for the group",
                               "held at group level", "No separate mark-up: the requirement is held for the group; see the tab of the group",
                               False, sheet, page, box or tagbox,
                               f"Working drawing {sheet} p{page}; Workbook master summary row of {gmast['tag']}", extra.strip(),
                               ledger_id=lid))
                info["counts"]["held at group level"] += 1
                return
            if not self.gated(g):
                rows.append(mk(tag, "queries", "AHU total", tag, qty, uom, drawn if drawn is not None else "not drawn", None,
                               "not established", self.GATE_ACTION, True, sheet, page, box or tagbox,
                               f"Working drawing {sheet} p{page}; {src}", (extra + " " + self.gate_note(req, uom, gates)).strip(),
                               ledger_id=lid, **{**self.gate_extra(gates, req), **ex}))
                info["counts"]["not established"] += 1
                return
            cls, act, need = judge(drawn, req, d054)
            if cls == "increase" and drawn and req / drawn > RATIO_MAX:
                cls, act, need = "increase above 2 times", "Query: required value is more than 2 times the drawn value; check the drawn value and the link of the unit", True
            if kind in ("oa", "spill") and cls == "reduction":
                cls, act, need = "reduction held as query", "Query: reduction not marked up; drawn value kept, margin recorded", True
            if cls == "not drawn" and act.startswith("Mark up: show") and need:
                # correction pass (Opus check items 5 and 8; build line 19): 'show the required value' is an instruction only for a
                # valueless label that the sheet draws, on a unit that passes the gate, and not against a drawn exhaust.
                gg = self.gate(u)
                drawn_ea = (md.get("exhaust_L_s") or {}).get("value")
                why_q = []
                if not dv.get("box"):
                    why_q.append(f"the sheet draws no {qn.split()[0].lower()} label for this room, so there is no valueless label to show")
                elif not gg["ok"]:
                    why_q.append("the room list of the unit does not establish the unit (" + "; ".join(gg["reasons"]) + ")")
                if wk == "return" and drawn_ea is not None and drawn_ea > 0.5:
                    why_q.append(f"the sheet draws exhaust of {drawn_ea:,.0f} L/s for this room and the workbook gives return (an exhaust room states exhaust, not return)")
                if why_q:
                    cls, act, need = "required value not shown", "Query: the sheet and the workbook differ for this room; no value is added", True
                    note += " No value is added: " + "; ".join(why_q) + ". The drawn value stays."
            if hold and need and act.startswith("Mark up"):
                cls, act, need = "held for zoning decision", "Query: held for a zoning decision; value not changed", True
            note = extra
            if drawn is not None and req is not None and isinstance(drawn, (int, float)):
                m = drawn - req
                pc = (lambda x: f" ({x / req:.1%})" if req else "")
                note = (f"Drawn duty covers the requirement. Margin {m:,.0f} {uom}{pc(m)}. " if m >= 0 else
                        f"Drawn duty does not cover the requirement. Short by {-m:,.0f} {uom}{pc(-m)}. ") + extra
            elif drawn is None and req is not None:
                note = "Not drawn on the sheet. " + extra
            if cls == "increase" and drawn:
                dif = req - drawn
                p5, p10 = dif > INC_PCT * abs(drawn), dif > INC_LPS
                if p5 != p10:
                    note += (" The increase passes the 5 % test only." if p5 else " The increase passes the 10 L/s test only.") + \
                            " The reading of the update rule is to be confirmed."
            lay = "queries" if (cls in ("not established", "held for zoning decision", "reduction held as query", "increase above 2 times")) else "airflows"
            rows.append(mk(tag, lay, "AHU total", tag, qty, uom, drawn, req, cls, act, need, sheet, page, box or tagbox,
                           f"Working drawing {sheet} p{page}; {src}", note.strip(), ledger_id=lid, **ex))
            info["counts"][cls] += 1

        part = alias_note + " " if alias_note else ""
        cover = g["cover"]
        info["cover"] = cover
        if cover is not None and cover > GATE_HI:
            part += (f"Room list not confirmed: the workbooks count {u['area_counted']:,.0f} m² for this unit and the zoning "
                     f"gives {u['area']:,.0f} m² ({cover:.0%}). ")
        if not u["complete"]:
            part += (f"The room list of the unit is not complete: the workbook value covers {len(u['links'])} of "
                     f"{u['rooms']} zoned rooms. ")
        leak = part + f"Workbook uses {LEAK_PCT:.0%} duct leakage; the sheets print 15 %. "
        # supply: the duty box of the AHU
        sref = sup_refs[0] if sup_refs else None
        sent = self.duty_entity(sheet, sref.get("box")) if sref else None
        comp = [self.duty_entity(r_["sheet"] if r_.get("sheet") else sheet, r_.get("box")) for r_ in sup_refs]
        comp = [c_["entity_id"] for c_ in comp if c_]
        sx = {"ledger_ids": "; ".join(comp)} if len(sup_refs) > 1 else {}
        put("Supply airflow", "L/s", sup_d, sup_req, (sref or {}).get("box"), sent["entity_id"] if sent else "",
            cells("supply", "Calculated airflow"), leak + (f"The drawn value is the sum of {len(sup_refs)} boxes (one for each AHU): " +
                                           ", ".join(f"{r_['value']:,.0f}" for r_ in sup_refs if r_.get("value") is not None) + " L/s. "
                                           if len(sup_refs) > 1 else ""), "supply", sx)
        # outdoor air: anchored on the drawn OA value box, one call-out for each A/B pair (build line 16)
        oa_ex = self.oa_extras(wrows, mast)
        if oa_items:
            best: Dict[str, Tuple[float, Dict[str, Any]]] = {}
            for t, v, ref in oa_items:
                for k in sorted(tag_keys(t) or {t}):
                    if v is not None and (k not in best or v > best[k][0]):
                        best[k] = (v, ref)
            pairs_anchor = [r for _, r in best.values()]
            anc = next((r["_anchor"] for r in pairs_anchor if r.get("_anchor")), None)
            if anc:
                ent = anc["entity"]
                ancs = [r["_anchor"] for r in pairs_anchor if r.get("_anchor")]
                cid = [a_["entity"]["entity_id"] for a_ in ancs if a_.get("entity")]
                exo = dict(oa_ex)
                if len(pairs_anchor) > 1:
                    exo["ledger_ids"] = "; ".join(cid)
                put("Outdoor air", "L/s", oa_d, oa_req, anc["box"], ent["entity_id"] if ent else "", cells("oa", "AHU outdoor air = fraction × calculated airflow"),
                    part + (f"Drawn value is on the {anc['kind']}. " if anc["kind"] else "") +
                    (f"The drawn value is the sum of {len(pairs_anchor)} boxes (one for each AHU). " if len(pairs_anchor) > 1 else ""), "oa", exo)
            else:
                rows.append(mk(tag, "queries", "AHU total", tag, "Outdoor air", "L/s", "not found on the sheet",
                               oa_req if (g["ok"] and oa_req) else None, "not established",
                               "Query: outdoor-air value not found on the sheet", True, sheet, page, tagbox,
                               f"Working drawing {sheet} p{page}; {src_m or src_wb}",
                               (f"The outdoor-air note names {oa_d:,.0f} L/s for this unit. No box with this value is on the sheet. " if oa_d is not None else
                                "The outdoor-air line of this unit gives no value on the sheet. ")
                               + (self.gate_note(oa_req, "L/s", gates) if not g["ok"] else ""),
                               **({**self.gate_extra(gates, oa_req), **oa_ex} if not g["ok"] else oa_ex)))
                info["counts"]["not established"] += 1
        elif oa_req:
            rows.append(mk(tag, "queries", "AHU total", tag, "Outdoor air", "L/s", "not found on the sheet",
                           oa_req if g["ok"] else None, "not established", "Query: outdoor-air value not found on the sheet", True,
                           sheet, page, tagbox, f"Working drawing {sheet} p{page}; {src_m or src_wb}",
                           "No outdoor-air line for this unit is on the sheet. " + (self.gate_note(oa_req, "L/s", gates) if not g["ok"] else ""),
                           **({**self.gate_extra(gates, oa_req), **oa_ex} if not g["ok"] else oa_ex)))
            info["counts"]["not established"] += 1
        # return air: the fan rows carry the instruction; this row records the comparison (no change ID)
        if ret_req is not None and ret_req > BAL_LPS:
            if ret_groups:
                j = judge(ret_d, ret_req, d054)[0] if ret_d is not None else "not established"
                rows.append(mk(tag, "airflows", "AHU total", tag, "Return air", "L/s", ret_d if ret_d is not None else "not drawn",
                               ret_req if g["ok"] else None, j if g["ok"] else "not established",
                               "No separate mark-up: see the fan rows" if g["ok"] else "No separate mark-up: required value not established - room list",
                               False, sheet, page, tagbox, f"Working drawing {sheet} p{page}; {src_wb}",
                               "Return = recirculated air + spill air. Drawn value: return fans owned by this unit; a duty/standby pair counts once. "
                               + part, ledger_id=""))
                info["counts"]["fan rows carry the return air"] += 1
        # spill or relief air
        sp_ent = None
        if spill_vals:
            sbox = spill_vals[0][1].get("box")
            sp_ent = self.duty_entity(sheet, sbox)
            numeric = [(v_, l_) for v_, l_ in spill_vals if v_ is not None]
            cids = [self.duty_entity(sheet, l_.get("box")) for _, l_ in numeric]
            cids = [c_["entity_id"] for c_ in cids if c_]
            put("Spill or relief air", "L/s", spill_d, spill_req, sbox, sp_ent["entity_id"] if sp_ent else "", cells("spill"),
                "The sheet value is for the whole system. " + part +
                (f"The drawn value is the sum of {len(numeric)} boxes. " if len(numeric) > 1 else ""), "spill",
                {"ledger_ids": "; ".join(cids)} if len(numeric) > 1 else None)
        elif valueless_spill and spill_req and spill_req > BAL_LPS:
            sp_ent = self.duty_entity(valueless_spill[3], valueless_spill[2])
            put("Spill or relief air", "L/s", None, spill_req, valueless_spill[2], sp_ent["entity_id"] if sp_ent else "", cells("spill"),
                "The sheet draws the spill path with the value missing. " + part, "spill")
        # coil duty: listed only
        sch = self.i.schedule.get(next(iter(sorted(u["keys"])), ""), None) if u["keys"] else None
        if coil_req is not None:
            cdraw = sch["cool"] if sch else None
            rows.append(mk(tag, "queries", "AHU total", tag, "Coil duty", "kW", cdraw if cdraw is not None else "not drawn",
                           round(coil_req, 1) if g["ok"] else None, "not drawn on sheet",
                           "List only: coil duty is not drawn on the sheet", False, sheet, page, tagbox,
                           f"Equipment schedule (Corr 5) {sch['pages'] if sch else 'no row'}; {src_wb}",
                           "Drawn value is the scheduled cooling duty, not a value printed on the sheet."))
            info["counts"]["not drawn on sheet"] += 1
        # nothing drawn: relief path or return air path (build line 20, never an instruction)
        info["relief_needed"] = False
        if spill_req and spill_req > BAL_LPS and not spill_path_drawn and have_wb:
            self.relief_query(u, rows, info, oa_req, exh_w, spill_w, spill_req, ret_req, sup_d, oa_d, once_through, g,
                              sheet, page, tagbox, src_wb, nothing="relief")
        elif spill_req and spill_req > BAL_LPS and ret_req and ret_req > BAL_LPS and not ret_groups and not once_through:
            self.relief_query(u, rows, info, oa_req, exh_w, spill_w, spill_req, ret_req, sup_d, oa_d, once_through, g,
                              sheet, page, tagbox, src_wb, nothing="return")

    def relief_query(self, u, rows, info, oa_req, exh_w, spill_w, spill_req, ret_req, sup_d, oa_d, once_through, g, sheet, page,
                     tagbox, src_wb, nothing: str) -> None:
        """'Query: relief path / return air path to confirm' with OA, exhaust, offset and the excess (D-078)."""
        tag = u["tag"]
        iso = bool([r for r in self.led.rows if r["kind"] == "exhaust link" and r["sheet"] in info["sheets"]])
        offs = (oa_req - (exh_w or 0) - (spill_w or 0)) if (oa_req is not None and spill_w is not None) else None
        txt = (f"The workbook gives outdoor air {oa_req:,.0f} L/s, exhaust {(exh_w or 0):,.0f} L/s, offset air "
               f"{(offs if offs is not None else 0):,.0f} L/s and an excess of {spill_req:,.0f} L/s. " if g["ok"] else
               f"The workbook indicates outdoor air {(oa_req or 0):,.0f} L/s, exhaust {(exh_w or 0):,.0f} L/s and an excess of "
               f"{spill_req:,.0f} L/s ({g['text']}). ")
        txt += ("The sheet draws no spill or relief path. " if nothing == "relief" else "The sheet draws no return fan. ")
        if once_through or iso:
            txt += ("The system is drawn once-through" if once_through else "The system has isolation exhaust") + \
                   "; the excess is taken as offset air; confirm. "
        txt += "Confirm the relief path or the return air path. Nothing is added to the sheet."
        mr = mk(tag, "queries", "Missing system", tag, "Relief path / return air path to confirm", "L/s", "not drawn",
                round(spill_req, 1) if g["ok"] else None, "relief path to confirm",
                "Query: relief path / return air path to confirm", True, sheet, page, tagbox, f"Working drawing {sheet} p{page}; {src_wb}",
                txt, **({} if g["ok"] else self.gate_extra([g], spill_req)))
        rows.append(mr)
        self.missing.append(mr)
        info["counts"]["relief path to confirm"] += 1

    def wb_ahu_rows(self, u: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
        out = []
        tags = {u["tag"]} | ({u["alias"]} if u.get("alias") else set())
        for dept, d in self.i.wb.items():
            exact = [r for r in d["ahu"] if r["tag"] in tags]
            use = exact or [r for r in d["ahu"] if tag_keys(r["tag"]) & u["akeys"] and "group" not in u["tag"].lower()
                            and "group" not in r["tag"].lower()]
            out.extend((dept, r) for r in use)
        return out

    @staticmethod
    def core(key: str) -> str:
        """Unit name of a tag key without prefix, level and A/B letter: 'RAF:B-B2-B2PHAB-02' gives 'B2PHAB-02'."""
        parts = key_only(key).split("-")
        return (parts[-2] + "-" + re.sub(r"(/[A-Z]|[A-Z])$", "", parts[-1])) if len(parts) >= 2 else key_only(key)

    # item 4: fans, by ownership, once for each drawn fan entity (D-080, build lines 17 and 18) --------------- #
    def build_fan_groups(self) -> None:
        """One group for each drawn fan (an A/B pair on one sheet is one group).  Ownership: the units whose name core
        is in the fan tag.  The group keeps every drawn duty so that a pair counts once."""
        self.fan_groups: List[Dict[str, Any]] = []
        self.groups_by_unit: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        by: Dict[Tuple[str, Tuple[str, ...]], List[Dict[str, Any]]] = defaultdict(list)
        mention: List[Dict[str, Any]] = []
        for r in self.led.rows:
            if r["kind"] != "fan tag" or r["sheet_role"] != "working":
                continue
            if not r.get("_equip"):
                mention.append(r)
                continue
            by[(r["sheet"], tuple(sorted(tag_keys(r["_tag"]))))].append(r)
        keyvals: Dict[str, Dict[str, set]] = defaultdict(lambda: defaultdict(set))
        for (sh, ks), ents in by.items():
            for k in ks:
                keyvals[k][sh] |= {e["value"] for e in ents if e["value"] != ""}
        for (sh, ks), ents in sorted(by.items()):
            ents = sorted(ents, key=lambda e: (e["_tag"], (e["_box"] or [0, 0])[1], (e["_box"] or [0, 0])[0]))
            tag0 = ents[0]["_tag"]
            fn = ents[0]["function"]
            texts = [e["_tag"] for e in ents]
            vals = [e["value"] for e in ents]
            lo, why = self.fan_list_only(tag0)
            owners = self.fan_owners(tag0)
            label = tag0
            if len(set(texts)) == 2 and all(re.search(r"\d[AB]$", t) for t in texts):
                label = re.sub(r"[AB]$", "", texts[0]) + "A/B"
            if len(ents) == 1:
                status = "single" if vals[0] != "" else "no duty"
                drawn = vals[0] if vals[0] != "" else None
            elif len(set(texts)) < len(texts):
                status, drawn = "duplicate tag", None
            elif len(ents) == 2 and all(v != "" for v in vals) and vals[0] == vals[1]:
                status, drawn = "pair", vals[0]
            elif len(ents) == 2:
                status, drawn = "unequal", None
            else:
                status, drawn = "group", None
            multi = {s_: v for s_, v in ((s_, set().union(*[keyvals[k][s_] for k in ks])) for s_ in
                                          set().union(*[set(keyvals[k]) for k in ks])) if v}
            two_duties = len(multi) > 1 and len({frozenset(v) for v in multi.values()}) > 1
            g = {"gid": f"FG-{len(self.fan_groups) + 1:04d}", "sheet": sh, "keys": ks, "tag": tag0, "label": label, "function": fn,
                 "ents": ents, "owners": owners, "list_only": lo, "list_why": why, "vals": vals, "status": status,
                 "drawn": drawn, "two_duties": two_duties, "other_sheets": sorted(s_ for s_ in multi if s_ != sh)}
            self.fan_groups.append(g)
            if not lo:
                for o in owners:
                    self.groups_by_unit[o["tag"]].append(g)
        # tag mentions that are not a drawn fan box: they follow the group of the same keys on the sheet
        self.fan_mentions = mention

    def group_drawn_sum(self, groups: Sequence[Dict[str, Any]]) -> Optional[float]:
        """Drawn duty of a set of fan groups, one value for each group (a duty/standby pair counts once).  None when
        a group has no stated duty or an arrangement that the sheet does not state."""
        if not groups:
            return None
        tot = 0.0
        for g in groups:
            if g["status"] not in ("single", "pair") or g["drawn"] is None or g["two_duties"]:
                return None
            tot += g["drawn"]
        return tot

    def exhaust_requirement(self, g: Dict[str, Any]) -> Dict[str, Any]:
        """Exhaust requirement of a drawn exhaust fan: the Fans-tab total for its tag and the exhaust of the workbook rooms
        that the Airflow tab ties to it, split into the rooms of its own AHUs and of other AHUs."""
        fk = set(g["keys"])
        own_keys = set()
        for o in g["owners"]:
            own_keys |= set(o["akeys"])
        own, other = 0.0, []
        for (dept, rn), w in self.wb_room.items():
            if not (tag_keys(str(w.get("exh_fan") or "")) & fk):
                continue
            if (w["exhaust"] or 0) <= 0:
                continue
            if tag_keys(str(w["ahu"] or "")) & own_keys:
                own += w["exhaust"]
            else:
                other.append((w["name"], w["exhaust"], str(w["ahu"])))
        wf, seen = [], set()
        for k in fk:
            for dept, x in self._wb_fans().get(k, []):
                if (dept, x["row"]) not in seen:
                    seen.add((dept, x["row"]))
                    wf.append((dept, x))
        total = sum(x["calc"] or 0 for _, x in wf) if wf else None
        return {"own": own, "other": other, "total": total, "wf": wf, "wfrows": frozenset((d_, x["row"]) for d_, x in wf)}

    def _wb_fans(self) -> Dict[str, List[Tuple[str, Dict[str, Any]]]]:
        if not hasattr(self, "_wbf"):
            self._wbf = defaultdict(list)
            for dept, d in self.i.wb.items():
                for f in d["fans"]:
                    for k in sorted(tag_keys(f["tag"])):
                        self._wbf[k].append((dept, f))
        return self._wbf

    def review_fans(self) -> None:
        keep = "Fan pressure cannot be taken from a schematic: the drawn value is kept."
        cls_map = {"increase": "to resize", "reduction": "to resize", "no change": "adequate", "margin": "adequate",
                   SUPPRESSED_CLASS: SUPPRESSED_CLASS, "not drawn": "duty not drawn", "not established": "not established"}
        pending: List[Dict[str, Any]] = []
        shared: Dict[frozenset, List[Dict[str, Any]]] = defaultdict(list)
        for g in self.fan_groups:
            if g["list_only"]:
                continue
            info = self.exhaust_info(g)
            g["req_info"] = info
            if g["function"] != "RAF" and info["wfrows"]:
                shared[info["wfrows"]].append(g)
        for g in self.fan_groups:
            if g["list_only"]:
                continue
            self.fan_group_row(g, keep, cls_map, shared)
        # units: isolation exhaust and a missing exhaust system
        for u in self.i.units:
            if u["has_tag"] and u["type"] in ("AHU", "CRAC") and not u["list_only"] and u["systems"] and u["tag"] in self.unit_info:
                self.unit_exhaust_rows(u, keep, cls_map)

    def exhaust_info(self, g: Dict[str, Any]) -> Dict[str, Any]:
        return self.exhaust_requirement(g) if g["function"] != "RAF" else {"wfrows": frozenset()}

    def unit_for_group(self, g: Dict[str, Any]) -> str:
        if g["owners"]:
            prim = [o for o in g["owners"] if o["systems"]] or g["owners"]
            return prim[0]["tag"]
        su = [u for u in self.i.units if u["has_tag"] and any(x["sheet"] == g["sheet"] for x in u["systems"]) and not u.get("is_group")
              and not u["list_only"]]
        return su[0]["tag"] if su else g["label"]

    def fan_group_row(self, g: Dict[str, Any], keep: str, cls_map: Dict[str, str], shared: Dict[frozenset, List[Dict[str, Any]]]) -> None:
        e0 = g["ents"][0]
        dent = next((x for x in self.led.rows if x["kind"] == "duty" and x["parent_id"] == e0["entity_id"]), None)
        ids = []
        for e in g["ents"]:
            d_ = next((x for x in self.led.rows if x["kind"] == "duty" and x["parent_id"] == e["entity_id"]), None)
            ids.append((d_ or e)["entity_id"])
        box = (dent or e0)["_box"]
        sheet = g["sheet"]
        unit = self.unit_for_group(g)
        fn = g["function"]
        qty = {"RAF": "Return fan airflow", "EAF": "Exhaust fan airflow", "TEAF": "Toilet exhaust fan airflow",
               "IEAF": "Isolation exhaust fan airflow"}.get(fn, "Fan airflow")
        item = g["label"]
        lay = "fans"
        owners = g["owners"]
        dtext = "; ".join(f"{e['_tag']} {e['value']:,.0f}" if e["value"] != "" else f"{e['_tag']} no duty" for e in g["ents"])
        common = dict(ledger_id=ids[0], ledger_ids="; ".join(ids), group_id=g["gid"])
        src0 = f"Working drawing {sheet} p1"
        tail = keep

        def emit(cls, act, need, drawn, req, note, srcx="", layer=None, **kw):
            r = mk(unit, layer or ("queries" if act.startswith("Query") else lay), "Fan", item, qty, "L/s", drawn, req, cls, act, need,
                   sheet, 1, box, src0 + (("; " + srcx) if srcx else ""), note, **{**common, **kw})
            self.fan_rows.append(r)
            self.rows.append(r)
            ui = self.unit_info.get(unit)
            if ui is not None:
                ui["rows"].append(r)
                ui["counts"]["fan: " + cls] += 1
            g["row"] = r
            return r

        self.fan_rows = getattr(self, "fan_rows", [])
        # ownership
        if not owners:
            near = self.near_units(g["tag"])
            if near:
                emit("tag code differs", "Query: tag code of the fan differs from the code of the unit", True, dtext, None,
                     f"The fan tag {g['tag']} does not match the code of the unit {near[0]['tag']}. The tags are not merged. " + tail)
            else:
                emit("fan: link not read", "Query: fan drawn, link to the unit not read", True, dtext, None,
                     "The sheet draws this fan. Its tag does not name a unit of the zone schedule. The link to the unit rests on the "
                     "position on the sheet only. " + tail)
            return
        if g["two_duties"]:
            emit("tag to correct", "Query: tag to correct; the same tag has two duties", True, dtext, None,
                 f"The tag {item} is drawn with different duties on {sheet} and on {', '.join(g['other_sheets'])}. The fan is not resized. " + tail)
            return
        if g["status"] in ("duplicate tag", "unequal", "group", "no duty"):
            req_t = None
            gates = [self.gate(o) for o in owners]
            if all(x["ok"] for x in gates) and fn == "RAF":
                rr = [self.unit_info.get(o["tag"], {}).get("req", {}).get("return") for o in owners]
                req_t = sum(rr) if rr and all(x is not None for x in rr) else None
            elif all(x["ok"] for x in gates):
                req_t = g["req_info"].get("total")
            why = {"duplicate tag": "the same tag is printed more than once on the sheet",
                   "unequal": "the two fans of the pair have different duties",
                   "group": "the sheet draws more than two fans under one tag",
                   "no duty": "the sheet prints no duty for this fan"}[g["status"]]
            emit("fan group: arrangement not stated", "Query: fan group; arrangement or duty not stated on the sheet", True, dtext, req_t,
                 f"Drawn fans: {dtext}. Reason: {why}. No value is given for a single fan. " + tail)
            return
        gates = [self.gate(o) for o in owners]
        shares = [o["tag"] for o in owners]
        basis = "ownership by the unit name in the tag"
        pair_note = " The two fans of the pair have the same drawn duty. They are read as duty and standby (to confirm). The pair counts once." \
            if g["status"] == "pair" else ""
        if fn == "RAF":
            rr = [self.unit_info.get(o["tag"], {}).get("req", {}).get("return") for o in owners]
            req = sum(rr) if rr and all(x is not None for x in rr) else None
            srcw = "; ".join(f"Workbook D{dp} tab AHU cells {r['cols']['recirc']} and {r['cols']['spill']}"
                             for o in owners for dp, r in self.wb_ahu_rows(o)[:8]) + \
                ". Formula: recirculated air + spill air (a spill value below 0 counts as 0), summed over the AHU rows listed"
            extra_note = "Required = return air (recirculated air + spill air) of the unit that the tag names. " + pair_note
        else:
            inf = g["req_info"]
            if inf["other"]:
                oth = inf["other"]
                emit("not established", "Query: workbook fan assignment to confirm", True, g["drawn"], None,
                     f"The workbook ties {len(oth)} rooms of other AHUs to this fan ({sum(x[1] for x in oth):,.0f} L/s), for example "
                     f"{oth[0][0]} on {oth[0][2]}. The fan assignment of the workbook is to be confirmed before the fan is resized. " + tail, **common)
                return
            req = inf["total"] if inf["total"] is not None else (inf["own"] if inf["own"] > 0 else None)
            srcw = "; ".join(f"Workbook D{dp} tab Fans cell {x['cell']}" for dp, x in inf["wf"][:2]) or "no row on the Fans tab"
            extra_note = pair_note
            if inf["total"] is not None and inf["own"] > 0 and abs(inf["total"] - inf["own"]) > max(INC_PCT * inf["own"], INC_LPS):
                extra_note += (f" The Fans tab gives {inf['total']:,.0f} L/s. The exhaust of the rooms tied to the fan is "
                               f"{inf['own']:,.0f} L/s. The Fans tab value is used. ")
            if req is not None and req <= 0.5:
                emit("not established", "Query: no room of the workbook is tied to this fan", True, g["drawn"], None,
                     "No room of the workbooks is tied to this fan. The drawn value stays. " + tail)
                return
        # shared Fans-tab total for several drawn fans: compare with the sum of the drawn duties, one for each pair
        grp = shared.get(g["req_info"].get("wfrows", frozenset()), []) if fn != "RAF" else []
        grp = [x for x in grp if not x["list_only"] and x["owners"]]
        if len(grp) > 1:
            if g is not grp[0]:
                g["row"] = grp[0].get("row")
                g["shared_with"] = grp[0]["gid"]
                return
            dsum = self.group_drawn_sum(grp)
            allowners = sorted({o["tag"] for x in grp for o in x["owners"]})
            allg = [self.gate(o) for x in grp for o in x["owners"]]
            labels = "; ".join(x["label"] for x in grp)
            if dsum is None or req is None:
                emit("fan group: arrangement not stated", "Query: fan group; arrangement or duty not stated on the sheet", True, dtext, req,
                     f"The workbook gives one total for the fans {labels}. The sheet does not give a duty for each. " + tail, srcx=srcw)
                return
            if not all(x["ok"] for x in allg):
                emit("not established", self.GATE_ACTION, True, dsum, None,
                     f"The workbook gives one total for the fans {labels}. " + self.gate_note(req, "L/s", allg), srcx=srcw,
                     **self.gate_extra(allg, req))
                return
            j = judge(dsum, round(req, 1), False)
            if j[0] in ("no change", "margin", SUPPRESSED_CLASS):
                emit("adequate", "No change", False, dsum, round(req, 1),
                     f"One total of {req:,.0f} L/s for the fans {labels}. The sum of the drawn duties, one for each pair, is {dsum:,.0f} L/s. "
                     f"It covers the total. No mark-up. " + tail, srcx=srcw)
            else:
                emit("fan group: total not covered", "Query: fan group; the drawn duties do not cover the workbook total", True, dsum,
                     round(req, 1), f"One total of {req:,.0f} L/s for the fans {labels}. The sum of the drawn duties, one for each pair, is "
                     f"{dsum:,.0f} L/s. The total is not divided between the fans. " + tail, srcx=srcw)
            return
        if not all(x["ok"] for x in gates):
            emit("not established", self.GATE_ACTION, True, g["drawn"], None,
                 self.gate_note(req, "L/s", gates) + " " + tail, srcx=srcw, **self.gate_extra(gates, req))
            return
        if req is None:
            emit("not established", "Query: required fan airflow not established", True, g["drawn"], None,
                 "The workbook gives no required value for this fan. " + tail, srcx=srcw)
            return
        d054 = all(self.unit_info.get(o["tag"], {}).get("d054") for o in owners)
        j = judge(g["drawn"], round(req, 1), d054)
        cls = cls_map[j[0]]
        act, need = j[1], j[2]
        if j[0] == "increase" and g["drawn"] and req / g["drawn"] > RATIO_MAX:
            cls, act, need = "increase above 2 times", "Query: required value is more than 2 times the drawn value; check the drawn value and the owner of the fan", True
        if cls == "to resize":
            act = "Mark up: resize the fan" + (" pair (A and B)" if g["status"] == "pair" else "")
        elif cls == "adequate":
            act, need = "No change", False
        elif cls == SUPPRESSED_CLASS:
            act, need = "No mark-up: increase of 5 L/s or less; drawn value kept", False
        note = extra_note
        if g["drawn"] is not None and j[0] in ("margin", "no change"):
            note = f"Margin {g['drawn'] - req:,.0f} L/s. " + note
        if len(owners) > 1:
            note += " Shared by the units " + "; ".join(shares) + "."
        emit(cls, act, need, g["drawn"], round(req, 1), (note + " " + tail).strip(), srcx=srcw)

    def unit_exhaust_rows(self, u: Dict[str, Any], keep: str, cls_map: Dict[str, str]) -> None:
        """Isolation exhaust and a missing exhaust system of a unit (build line 20: exhaust is compared with the drawn
        EAF, TEAF or IEA of the system; 'add an exhaust fan' stands only where nothing exhaust is drawn)."""
        tag = u["tag"]
        info = self.unit_info[tag]
        sheet, page, tagbox = self.sheet_of(u)
        g = self.gate(u)
        d054 = info.get("d054")
        rows = info["rows"]
        sheets = set(info["sheets"])
        iso_labels = []
        iso_present = False
        for s in u["systems"]:
            iso_labels += list(s["isolation_exhaust"]["header_labels"])
            if s["isolation_exhaust"]["header_labels"] or any(lb["duty"]["value"] is not None for lb in s["isolation_exhaust"]["line_rows"]):
                iso_present = True
        iso_rooms = [(n, d) for n, d in self.drawn_rooms(u).items()
                     if "IEA" in str(d["rm"].get("services_drawn") or "") or d["rm"].get("isolation_exhaust_target")]
        zr = self.zone_rooms(u)
        if iso_present or iso_rooms:
            vals = []
            for n, d in iso_rooms:
                for lk in zr.get(n, {}).get("links", []):
                    w = self.wb_room.get((self.dept2(lk), lk["dpa_room_id"]))
                    if w and w["exhaust"] is not None:
                        vals.append(w["exhaust"])
            reqv = round(sum(vals), 1) if vals else None
            lbl = sorted({lb["value_L_s"] for lb in iso_labels if lb["value_L_s"] is not None})
            lb0 = iso_labels[0] if iso_labels else None
            ent = self.duty_entity(sheet, (lb0 or {}).get("box")) if lb0 else None
            ex = {"ledger_id": ent["entity_id"] if ent else ""}
            if len(lbl) == 1 and g["ok"]:
                j = judge(lbl[0], reqv, d054)
                cls = cls_map.get(j[0], j[0])
                act, need = j[1], j[2]
                if cls == "to resize":
                    act = "Mark up: resize the fan"
                elif cls == "adequate":
                    act, need = "No change", False
                note = keep
                dv = lbl[0]
            elif len(lbl) == 1 and not g["ok"]:
                cls, act, need = "not established", self.GATE_ACTION, True
                note = self.gate_note(reqv, "L/s", [g]) + " " + keep
                dv = lbl[0]
                ex.update(self.gate_extra([g], reqv))
                reqv = None
            else:
                dv = None
                cls, act, need = "not established", "Query: isolation exhaust duty to confirm", True
                note = ("Header labels on the sheet: " + (", ".join(f"{x:,.0f}" for x in lbl) or "none") +
                        " L/s. The sheet does not give one total for these rooms. " + keep)
            r = mk(tag, "fans" if cls not in ("not established",) else "queries", "Fan", "Isolation exhaust (IEA)",
                   "Isolation exhaust airflow", "L/s", dv if dv is not None else "see note", reqv, cls, act, need,
                   sheet, page, (lb0 or {}).get("box") or tagbox,
                   f"Working drawing {sheet} p{page}; sum of room exhaust of isolation rooms, workbook tab Airflow", note, **ex)
            r["unit"] = tag
            rows.append(r)
            self.rows.append(r)
            info["counts"]["fan: " + cls] += 1
        # missing exhaust: nothing exhaust drawn on any sheet of the system (ledger lookup of tag variants and of the function)
        cores = {self.core(k) for k in u["akeys"]}
        exh_present = iso_present
        for r_ in self.led.rows:
            if r_["sheet"] not in sheets:
                continue
            if r_["kind"] == "fan tag" and r_["function"] != "RAF" and r_["_tag"].split("-")[0] in ("EAF", "TEAF", "IEAF", "TEF", "KEAF", "HPF") \
                    and any(self.core(k) in cores for k in tag_keys(r_["_tag"])):
                exh_present = True
            elif r_["kind"] in ("exhaust link", "BIBO tag"):
                exh_present = True
        if not exh_present:
            for gr in self.groups_by_unit.get(tag, []):
                if gr["function"] != "RAF":
                    exh_present = True
        exh_need, exh_rooms = 0.0, []
        for n, v in zr.items():
            for lk in v["links"]:
                w = self.wb_room.get((self.dept2(lk), lk["dpa_room_id"]))
                if w and w["exhaust"] and w["exhaust"] > 0.5:
                    exh_need += w["exhaust"]
                    exh_rooms.append(w["name"])
        if exh_need > BAL_LPS and not exh_present:
            if g["ok"]:
                mr = mk(tag, "new systems", "Missing system", tag, "Exhaust system", "L/s", "not drawn", round(exh_need, 1),
                        "missing system", "Mark up: add an exhaust fan and its ducts", True, sheet, page, tagbox,
                        f"Working drawing {sheet} p{page}; Workbook tab Airflow, room exhaust",
                        f"{len(exh_rooms)} rooms need exhaust. No exhaust fan, exhaust link or isolation exhaust is drawn on any sheet of the system.")
            else:
                mr = mk(tag, "queries", "Missing system", tag, "Exhaust system", "L/s", "not drawn", None, "missing system",
                        "Query: no exhaust element is drawn; required value not established - room list", True, sheet, page, tagbox,
                        f"Working drawing {sheet} p{page}; Workbook tab Airflow, room exhaust",
                        f"{len(exh_rooms)} rooms need exhaust. No exhaust element is drawn on any sheet of the system. " +
                        self.gate_note(exh_need, "L/s", [g]), **self.gate_extra([g], exh_need))
            mr["unit"] = tag
            rows.append(mr)
            self.rows.append(mr)
            self.missing.append(mr)
            info["counts"]["missing system"] += 1

    # -- one drawn room box, one result --------------------------------------------------------------- #
    RANK = {"unchanged": 0, "named in box": 0, "name differs": 0, "no change": 0, "margin": 0, "adequate": 0,
            "drawn on another sheet": 0, "typical count changed": 1, "typical count to confirm": 1, "increase": 1, "reduction": 1, "not drawn": 1, "to add": 1,
            "differs": 2, "drawn without tag": 2, "linked rooms differ": 2, "link to confirm": 2, "not established": 3, "offset to check": 2,
            "to add: not established": 3, "moved to another unit": 4, "moved from another unit": 4, "to remove": 5}

    def dedupe_boxes(self) -> None:
        """A drawn room box can be reviewed under several units (an A/B pair, its group, an alias).  Keep one result for
        each (sheet, box, item, quantity): the one where a room list holds the room (unchanged first, removal last).  The
        other tabs show a pointer row with no change ID of its own.  Fans are not touched here: a fan is reviewed once for
        each drawn fan (review_fans)."""
        groups: Dict[Tuple[Any, ...], List[Tuple[str, Row]]] = defaultdict(list)
        for tg, info in self.unit_info.items():
            for r in info.get("rows", []):
                if r["row_type"] in ("Room", "Room airflow") and r["box"] and r["quantity"] != "":
                    groups[(r["sheet"], r["box"], r["item"], r["quantity"])].append((tg, r))
        n_moved = 0

        def pick(cands: List[Tuple[str, Row]]) -> Tuple[str, Row]:
            return min(cands, key=lambda c: (self.RANK.get(c[1]["cls"], 3), self.unit_by_tag[c[0]]["is_group"] is False, c[0]))

        for key, lst in groups.items():
            if len({id(r) for _, r in lst}) < 2:
                continue
            win = pick(lst)
            for tg, r in lst:
                if r is win[1]:
                    continue
                if r["needs_id"] or r["cls"] != win[1]["cls"]:
                    r["ref"] = win[1]
                    r["ref_unit"] = win[0]
                    r["note"] = ((r["note"] + " ") if r["note"] else "") + \
                        f"The same box is reviewed on the tab of {win[0]}; this row says: {r['cls']}."
                    r["cls"] = "reviewed on another tab"
                    r["action"] = f"No separate mark-up: see the tab of {win[0]}"
                    r["needs_id"] = False
                    n_moved += 1
        self.n_dedup = n_moved

    def review_untagged(self) -> None:
        for u in self.untagged:
            zr = self.zone_rooms(u)
            first = ""
            if u["tag"].startswith("FCU"):
                cat = "FCU, tag to confirm"
                need = "Draw the FCU with its outdoor-air and exhaust connection. Give it a tag."
            elif u["tag"].startswith("AHU spill"):
                cat = "Spill air to plant rooms"
                need = "Show the spill air path to the plant rooms on the sheet of the unit that serves them."
            else:
                cat = "Rooms with no unit"
                need = "Name a unit for these rooms. Show it on a sheet."
            storey = str(u["storeys"])
            mr = mk(u["tag"], "new systems", "Missing system", u["tag"], cat, "rooms", "not drawn", u["rooms"],
                    "missing system", "New system: draw on a new sheet", u["type"] in ("FCU",), "NEW", 1, None,
                    u["source"], f"{need} Storey {storey}. Rooms in the zone list: {u['rooms']} ({u['area']} m²). {first}")
            if u["tag"].startswith("FCU"):
                self.missing.append(mr)
            else:
                mr["action"] = "Query: unit not named in the documents"
                mr["layer"] = "queries"
                mr["needs_id"] = True
            self.untag_rows = getattr(self, "untag_rows", [])
            self.untag_rows.append(mr)
            self.rows.append(mr)

    # -- global missing systems (item 5) ----------------------------------------#
    def appd_system_held(self, s: Dict[str, Any], ks: set) -> Optional[Tuple[str, str, str]]:
        """(action, reason, class) when an App D AHU system may not be drawn as a new system, else None."""
        tags = sorted({e["tag_as_written"] for e in s["equipment"] if str(e["tag_as_written"]).upper().startswith(("AHU-", "PAHU-"))})
        if not tags:
            return None
        t0 = tags[0]
        units = [u for u in self.i.units if u["has_tag"] and (u["akeys"] | u["keys"]) & ks]
        lv, code = self._code_of(t0)
        near = []
        if code:
            for k in list(self.work_tag_keys) + [k for u in self.i.units for k in u["akeys"]]:
                if not k.startswith("AHU:"):
                    continue
                lv2, c2 = self._code_of("AHU-" + k.split(":", 1)[1])
                if c2 == code and lv2 != lv:
                    near.append(k.split(":", 1)[1])
        if not units:
            if near:
                sh = sorted({h["sheet"] for k in near for h in self.work_tag_keys.get("AHU:" + k, []) if h.get("sheet")})
                return ("Query: tag to correct; a near-match of this tag is drawn" + (" on " + ", ".join(sh) if sh else ""),
                        f"The tag {t0} is not in the zone schedule. The near-match {', '.join(sorted(set(near))[:3])} is "
                        f"{'drawn on ' + ', '.join(sh) if sh else 'in the schedule'}. A near-match of a tag is a query, not a redraw.", "tag to correct")
            return ("Query: unit of the layout is not in the zone schedule",
                    f"The tag {t0} is not in the zone schedule and no working sheet draws it.", "not in zone schedule")
        gg = self.gate(units[0])
        if not gg["ok"]:
            return ("Query: required value not established - room list",
                    "The room list of the unit does not establish the unit (" + "; ".join(gg["reasons"]) + ").", "not established")
        return None

    def room_name_on_sheets(self, name: str) -> List[str]:
        """Working sheets that draw a room box with this name (words of the name equal, abbreviations of the plan allowed)."""
        if not hasattr(self, "_box_names"):
            self._box_names = defaultdict(set)
            for r in self.led.rows:
                if r["kind"] == "room box" and r["sheet_role"] == "working":
                    self._box_names[r["sheet"]].add(r["text"])
        want = name_tokens(re.sub(r"\(.*?\)", lambda m: " " + m.group(0)[1:-1] + " ", str(name)))
        out = []
        for sh, names in sorted(self._box_names.items()):
            for t in names:
                if want and name_tokens(t) == want:
                    out.append(sh)
                    break
        return out

    def missing_global(self) -> None:
        i = self.i
        # rooms separated onto an FCU under the outdoor-air rule
        for dept, d in sorted(i.wb.items()):
            for f in d["fcu"]:
                if f["reason"] != "OA RULE":
                    continue
                src_unit = next((u for u in i.units if u["tag"] == f["ahu"]), None)
                if src_unit is not None and src_unit["list_only"]:
                    continue
                sheet = ""
                page = ""
                bx = None
                if src_unit and src_unit["systems"]:
                    sheet, page, bx = self.sheet_of(src_unit)
                # correction pass (Opus check items 9 to 13; build lines 11 and 15): an added FCU, path and D-076 box needs the gate of
                # step 11 and a room that no working sheet draws.  Otherwise the row is a query and nothing is added.
                gg = self.gate(src_unit) if src_unit is not None else {"ok": False, "reasons": ["the unit is not in the zone schedule"],
                                                                        "cover": None, "n": None, "m": None, "text": "indicative"}
                on_sheets = self.room_name_on_sheets(str(f["name"]))
                base_note = (f"The room takes {f['oa']:,.1f} L/s of outdoor air from the AHU. Separate it onto its own FCU. "
                             f"Outdoor-air source in the workbook: {f['oa_src']}. Draw the FCU, its outdoor-air duct and its source.")
                if gg["ok"] and not on_sheets:
                    mr = mk(str(f["ahu"]), "new systems", "Missing system", f"Room {f['room']} {f['name']}",
                            "Separated room: FCU and outdoor-air connection", "L/s", "not drawn", f["supply"],
                            "missing system", "New system: FCU and outdoor-air connection on a new sheet", True,
                            sheet or "NEW", page or 1, bx,
                            f"Workbook D{dept} tab FCU row {f['row']}", base_note)
                elif not gg["ok"]:
                    mr = mk(str(f["ahu"]), "queries", "Missing system", f"Room {f['room']} {f['name']}",
                            "Separated room: FCU and outdoor-air connection", "L/s", "not drawn", None,
                            "missing system", "Query: required value not established - room list", True,
                            sheet or "NEW", page or 1, bx, f"Workbook D{dept} tab FCU row {f['row']}",
                            "No FCU, no path and no box are added. " + self.gate_note(f["supply"], "L/s", [gg]) +
                            (f" A room box with this name is also on {', '.join(on_sheets)}." if on_sheets else "") + " " + base_note,
                            **self.gate_extra([gg], f["supply"]))
                else:
                    mr = mk(str(f["ahu"]), "queries", "Missing system", f"Room {f['room']} {f['name']}",
                            "Separated room: FCU and outdoor-air connection", "L/s", "not drawn", None,
                            "missing system", "Query: room is drawn on " + ", ".join(on_sheets) + "; separation onto an FCU to confirm", True,
                            sheet or "NEW", page or 1, bx, f"Workbook D{dept} tab FCU row {f['row']}",
                            f"A room box with this name is on {', '.join(on_sheets)}. The room stays where it is drawn. No FCU, no path and no box are added. "
                            f"The workbook asks for an FCU: {base_note}")
                self.missing.append(mr)
                if src_unit and src_unit["tag"] in self.unit_info:
                    self.unit_info[src_unit["tag"]]["rows"].append(mr)
                    self.unit_info[src_unit["tag"]]["counts"]["missing system"] += 1
                    mr["unit"] = src_unit["tag"]
                self.rows.append(mr)
        # outdoor-air units referred to and not drawn
        named: Dict[str, Dict[str, Any]] = {}
        for s in i.working:
            src = s["outdoor_air"].get("source") or ""
            for k in sorted(tag_keys(src)):
                if k.startswith("PAHU:"):
                    named.setdefault(k, {"sheets": set(), "src": src, "box": None})["sheets"].add(s["sheet"])
                    ln = s["outdoor_air"]["lines"]
                    if ln and not named[k]["box"]:
                        named[k]["box"] = ln[0].get("box")
        eq_pahu = {k for e in i.equip if e["issue_set"] == "working" and e["type"] == "PAHU" for k in tag_keys(e["tag"])} | \
            {k for r_ in self.led.rows if r_["kind"] == "PAHU tag" and r_["sheet_role"] == "working" for k in r_["tag_keys"].split("; ") if k}
        done_raw = set()
        for k, v in sorted(named.items()):
            if k in eq_pahu or self.pahu_raw(k) in done_raw:
                continue
            done_raw.add(self.pahu_raw(k))
            sh = sorted(v["sheets"])
            mr = mk(self.pahu_raw(k), "new systems", "Missing system", self.pahu_raw(k), "Outdoor-air unit referred to, not drawn",
                    "L/s", "not drawn", "to confirm", "missing system", "New system: draw the outdoor-air unit", True,
                    "NEW", 1, v["box"], f"Working drawing {', '.join(sh)}; outdoor-air note",
                    f"The sheets {', '.join(sh)} name this unit as the outdoor-air source. No sheet draws it with its duty.")
            self.missing.append(mr)
            self.rows.append(mr)
        # systems of App D that no working sheet shows
        for s in i.appd:
            if s["kind"] in ("appd_space_without_equipment",):
                continue
            tags = [e["tag_as_written"] for e in s["equipment"]]
            ks = set()
            for t in tags:
                ks |= set(tag_keys(t))
            if ks & set(self.work_tag_keys) or any(self.led.tag_rows([k]) for k in ks):
                continue                       # drawn on a working sheet: the working sheet is the home (D-079)
            st = (s.get("storey") or [""])[0]
            e0 = s["equipment"][0] if s["equipment"] else {}
            kind = {"appd_ahu_system": "AHU", "appd_fcu_group": "FCU group", "appd_mv_fan_system": "Fan-only (MV) system"}[s["kind"]]
            held = self.appd_system_held(s, ks) if s["kind"] == "appd_ahu_system" else None
            if held:
                # correction pass (Opus check item 6; D-079, D-083, D-085): a system whose unit is not in the zone schedule, whose tag is a
                # near-match of a drawn tag, or whose room list fails the gate is a query.  It is not drawn as a new system.
                act, why, cls_ = held
                mr = mk(s["system_id"], "queries", "Missing system", ", ".join(sorted(set(tags)))[:200],
                        "App D system absent from the working drawings", "no.", "not drawn", None, cls_, act, True,
                        "App D", (s.get("page") or [1])[0], e0.get("box"),
                        f"Appendix D, drawing {', '.join(s.get('drawing_numbers') or [])}, p{(s.get('page') or [''])[0]}",
                        f"{kind} on storey {st}. {why} No system is drawn for it on a new sheet.")
                self.missing.append(mr)
                self.rows.append(mr)
                continue
            mr = mk(s["system_id"], "new systems", "Missing system", ", ".join(sorted(set(tags)))[:200],
                    "App D system absent from the working drawings", "no.", "not drawn", len(s["equipment"]),
                    "missing system", "New system: App D common-area sheet", True, "App D", (s.get("page") or [1])[0],
                    e0.get("box"), f"Appendix D, drawing {', '.join(s.get('drawing_numbers') or [])}, p{(s.get('page') or [''])[0]}",
                    f"{kind} on storey {st}. Outdoor-air and exhaust path: {s.get('outdoor_air_exhaust_path')}. "
                    f"Draw it on the App D air-side sheet of storey {st}.")
            self.missing.append(mr)
            self.rows.append(mr)
        # systems named in the DD Booklets and absent from every drawing
        uk = {k for u in i.units for k in u["keys"]}
        ak = {k for s in i.appd for e in s["equipment"] for k in tag_keys(e["tag_as_written"])}
        seen = set()
        ov_keys = {k for r_ in self.led.rows if r_["kind"] == "overall tag" for k in r_["tag_keys"].split("; ") if k}
        for r in read_csv(self.i.reg / "booklet_equipment_standby.csv"):
            for k in sorted(tag_keys(r["tag_normalised"])):
                if k in ov_keys:
                    continue                      # printed on an overall schematic: not 'shown on no sheet'
                if k in seen or k in self.work_tag_keys or k in self.tender_tag_keys or k in uk or k in ak or self.led.tag_rows([k]):
                    continue
                seen.add(k)
                mr = mk(key_only(k), "new systems", "Missing system", r["tag_normalised"],
                        "System of a DD Booklet absent from the drawings", "no.", "not drawn", 1, "missing system",
                        "Query: system named in a DD Booklet, shown on no sheet", True, "NEW", 1, jbox(r["bbox"]),
                        f"DD Booklet {r['department_label']}, booklet page {r['booklet_page']} (file page {r['pdf_page']})",
                        f"The booklet gives {r['tag_as_written']} for: {(r['area'] or '')[:70]}. "
                        "No drawing and no zone schedule row has this tag.")
                mr["layer"] = "queries"
                self.missing.append(mr)
                self.rows.append(mr)

    # -- item 6: consistency across sheets --------------------------------------- #
    def consistency(self) -> None:
        i = self.i
        # same tag, same duty
        byt: Dict[str, List[Dict[str, str]]] = defaultdict(list)
        for e in i.equip:
            if e["issue_set"] == "working" and e["type"] in ("AHU", "PAHU"):
                byt[e["tag"]].append(e)          # fans are reviewed once for each drawn fan (review_fans)
        for t, es in sorted(byt.items()):
            if self.unit_list_only(t) or self.drawn_only_list_only(t):
                continue
            duties = {(e["duty_as_written"] or "").strip() for e in es if (e["duty_as_written"] or "").strip()}
            if len(duties) > 1:
                e0 = es[0]
                vals = "; ".join(f"{e['sheet']}: {e['duty_as_written'] or 'no duty'}" for e in es)
                self.consist.append(mk(t, "queries", "Consistency", t, "Duty of one tag on several sheets", "L/s",
                                       es[0]["duty_as_written"], es[-1]["duty_as_written"], "differs",
                                       "Query: same tag with different duties", True, e0["sheet"], e0["page"],
                                       jbox(e0["box_page_view_duty"]) or jbox(e0["box_page_view_tag"]),
                                       "Working drawings " + ", ".join(sorted({e['sheet'] for e in es})), vals))
        # drawn AHU duty against the equipment schedule
        for t, es in sorted(byt.items()):
            if not t.startswith("AHU-") or self.unit_list_only(t) or self.drawn_only_list_only(t):
                continue
            d = num(es[0]["duty_as_written"])
            sch = None
            for k in sorted(tag_keys(t)):
                sch = i.schedule.get(k) or sch
            if d is None or not sch or sch["supply"] is None:
                continue
            if abs(d - sch["supply"]) > max(INC_PCT * sch["supply"], INC_LPS):
                e0 = es[0]
                self.consist.append(mk(t, "queries", "Consistency", t, "Sheet duty against the equipment schedule", "L/s",
                                       d, sch["supply"], "differs", "Query: sheet and schedule differ", True,
                                       e0["sheet"], e0["page"], jbox(e0["box_page_view_duty"]),
                                       f"Working drawing {e0['sheet']}; Equipment schedule (Corr 5) {sch['pages']}",
                                       f"Schedule row for {sch['tag']}."))
        # outdoor-air unit airflow equals the sum of what its AHUs take; each duty/standby pair counts once (build line 16)
        taken: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"units": {}, "sheet": None, "box": None})
        for s in i.working:
            ks = [k for k in sorted(tag_keys(s["outdoor_air"].get("source") or "")) if k.startswith("PAHU:")]
            for ln in s["outdoor_air"]["lines"]:
                for k in ks:
                    t = taken[k]
                    for ak in sorted(tag_keys(ln["to"])):
                        v = ln["value_L_s"] or 0
                        if v > t["units"].get(ak, 0):
                            t["units"][ak] = v
                    t["sheet"] = t["sheet"] or s["sheet"]
                    t["box"] = t["box"] or ln.get("box")
        seen_raw = set()
        for k, t in sorted(taken.items()):
            if self.pahu_raw(k) in seen_raw:
                continue
            seen_raw.add(self.pahu_raw(k))
            ues = []
            for ak in t["units"]:
                us = [x for x in self.units_by_key.get(ak, []) if not x.get("is_group")] or self.units_by_key.get(ak, [])
                if us:
                    ues.append(us[0])
            if ues and all(x["list_only"] for x in ues):
                continue
            ues = [x for x in ues if not x["list_only"]]
            eqs = [e for e in i.equip if e["issue_set"] == "working" and e["type"] == "PAHU" and k in tag_keys(e["tag"])]
            duty = [num(e["duty_as_written"]) for e in eqs]
            duty = [d for d in duty if d]
            if len(duty) == 1 or (len(duty) == 2 and duty[0] == duty[1]):
                drawn_duty = duty[0]
            else:
                drawn_duty = None            # more than two units, or unequal: the arrangement is not stated
            drawn_sum = sum(t["units"].values())
            wbsum, gates = 0.0, []
            for x in ues:
                m = self.i.master.get(x["tag"]) or {}
                if m.get("AHU outdoor air = fraction × calculated airflow"):
                    wbsum += m["AHU outdoor air = fraction × calculated airflow"]
                gates.append(self.gate(x))
            e0 = eqs[0] if eqs else None
            dbox = jbox(e0["box_page_view_duty"]) if e0 else None
            dent = self.duty_entity(e0["sheet"], dbox) if e0 and dbox else None
            lid = dent["entity_id"] if dent else ""
            note = (f"Sum of the outdoor air drawn on the AHU lines, one value for each AHU pair: {drawn_sum:,.0f} L/s. "
                    f"Sum of outdoor air in the workbooks, one value for each AHU: {wbsum:,.0f} L/s. ")
            if drawn_duty is None:
                self.consist.append(mk(self.pahu_raw(k), "queries", "Consistency", self.pahu_raw(k),
                                       "Duty of the outdoor-air unit against the sum of the AHU outdoor air", "L/s", "not stated",
                                       None, "not established", "Query: duty of the outdoor-air unit is not stated for the whole group",
                                       True, (e0 or {}).get("sheet") or t["sheet"] or "", 1, dbox or t["box"],
                                       "Working drawings; outdoor-air lines of " + str(len(t["units"])) + " AHUs",
                                       note + "The units of the group are more than two, or their duties differ. The arrangement is not stated.",
                                       ledger_id=lid))
                continue
            if not gates or not all(x["ok"] for x in gates):
                self.consist.append(mk(self.pahu_raw(k), "queries", "Consistency", self.pahu_raw(k),
                                       "Duty of the outdoor-air unit against the sum of the AHU outdoor air", "L/s", drawn_duty, None,
                                       "not established", self.GATE_ACTION, True, e0["sheet"] if e0 else (t["sheet"] or ""), 1,
                                       dbox or t["box"], "Working drawings; outdoor-air lines of " + str(len(t["units"])) + " AHUs",
                                       note + (self.gate_note(wbsum, "L/s", gates) if gates else "No AHU of the zone schedule is linked."),
                                       ledger_id=lid, **(self.gate_extra(gates, wbsum) if gates else {})))
                continue
            cls, act, need = judge(drawn_duty, round(wbsum, 1), False)
            if cls in ("no change", "margin"):
                act, need = "No change", False
            if cls == "reduction":
                cls, act, need = "margin", "Keep drawn value; margin recorded", False
            self.consist.append(mk(self.pahu_raw(k), "queries" if need and act.startswith("Query") else ("airflows" if need else "airflows"),
                                   "Consistency", self.pahu_raw(k), "Duty of the outdoor-air unit against the sum of the AHU outdoor air",
                                   "L/s", drawn_duty, round(wbsum, 1), cls, act, need, e0["sheet"] if e0 else (t["sheet"] or ""), 1,
                                   dbox or t["box"], "Working drawings; outdoor-air lines of " + str(len(t["units"])) + " AHUs", note,
                                   ledger_id=lid))
        # no room on two AHUs
        seen_rb = set()
        group_keys = [tag_keys(g["tag"]) for g in self.unit_by_tag.values() if g["is_group"]]
        for s in i.working:
            for rm in s["rooms"]:
                tg = frozenset().union(*[tag_keys(a["value"] if isinstance(a, dict) else a)
                                         for a in rm["ahu_tags_as_written"]]) if rm["ahu_tags_as_written"] else frozenset()
                shared = any(tg and tg <= gk for gk in group_keys)      # a declared AHU group shares its rooms
                mot = bool(tg & i.mot_keys) and any(u["list_only"] for k in tg for u in self.units_by_key.get(k, []))
                if shared or mot:
                    continue
                if rm.get("system_count", 1) > 1 and rm["room_id"] not in seen_rb:
                    seen_rb.add(rm["room_id"])
                    self.consist.append(mk(s["system_id"].split(":")[1], "queries", "Consistency", rm["name"]["value"],
                                           "Room box on more than one system", "no.", f"{rm['system_count']} systems", 1, "differs",
                                           "Query: room box names AHUs of several systems", True, s["sheet"], s["page"],
                                           rm["name"]["box"], f"Working drawing {s['sheet']} p{s['page']}",
                                           "AHU tags on the box: " + ", ".join(a["value"] if isinstance(a, dict) else a for a in rm["ahu_tags_as_written"])))
        seen = Counter((lk["dept"], lk["dpa_room_id"]) for u in i.units for lk in u["links"])
        dup = [k for k, n in seen.items() if n > 1]
        self.consist.append(mk("zone list", "queries", "Consistency", "Zoned room on two units", "Rooms on two units", "no.",
                               len(dup), 0, "no change" if not dup else "differs",
                               "No change" if not dup else "Query: room on two units", bool(dup), "", "", None,
                               "Zone schedule room list", "Every zoned room has one unit." if not dup else str(dup[:10])))
        self.overall_review()
        self.rows.extend(self.consist)

    # -- overall ACMV schematics (IP-BH-00100, IP-BH-00101) --------------------------- #
    OVERALL = ("IP-BH-00100", "IP-BH-00101")

    def read_overall(self, sheet: str) -> List[Dict[str, Any]]:
        """AHU and PAHU tags of an overall sheet from its text layer.  A tag is written in two lines: the
        first ends with a dash.  Boxes are in page-view points."""
        import pymupdf
        pdf = sorted((self.i.root / "data/source/Basis/Working Drawings").glob(sheet + "_*.pdf"))
        if not pdf:
            return []
        doc = pymupdf.open(pdf[0])
        try:
            pg = doc[0]
            spans = []
            for b in pg.get_text("dict")["blocks"]:
                for ln in b.get("lines", []):
                    for sp in ln["spans"]:
                        t = sp["text"].strip()
                        if t:
                            spans.append((t, sp["bbox"]))
            used = set()
            out = []
            for n, (t, bb) in enumerate(spans):
                if not re.match(r"^(?:PAHU|AHU)-B-[A-Z0-9]+-(?:[A-Z0-9]{2})?$", t):
                    continue
                best = None
                for m, (t2, b2) in enumerate(spans):
                    if m == n or m in used:
                        continue
                    if -5 <= b2[1] - bb[3] < 6 and abs((b2[0] + b2[2]) / 2 - (bb[0] + bb[2]) / 2) < 30 and \
                            re.match(r"^[0-9A-Z]{2,}[A-Z0-9\- /]*$", t2):
                        best = (m, t2, b2)
                        break
                if not best:
                    continue
                used.add(best[0])
                tag = re.sub(r"\s+", "", t + best[1])
                tag = re.sub(r"(\d)/AB$", r"\1A/B", tag)
                tag = re.sub(r"(\d)/([A-Z])$", r"\1\2", tag)
                x0, y0, x1, y1 = (min(bb[0], best[2][0]), min(bb[1], best[2][1]), max(bb[2], best[2][2]), max(bb[3], best[2][3]))
                out.append({"tag": tag, "box": [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)]})
            return out
        finally:
            doc.close()

    @staticmethod
    def _code_of(tag: str) -> Tuple[str, str]:
        m = re.match(r"^(?:PAHU|AHU)-B-([A-Z0-9]{1,2})-([A-Z0-9]+?)-?(\d*)[A-Z]?(?:/[A-Z])?$", tag)
        return (m.group(1), m.group(2)) if m else ("", "")

    def overall_near_tag(self, u: Dict[str, Any], on_sheet: Dict[str, List[Dict[str, Any]]]) -> Optional[Tuple[str, Dict[str, Any]]]:
        """A tag on an overall sheet with the same level and a code that starts with the code of the unit or the reverse."""
        lv, code = self._code_of(u["tag"])
        if not code:
            return None
        for sh, tags in on_sheet.items():
            for t in tags:
                lv2, c2 = self._code_of(t["tag"])
                if lv2 == lv and c2 and c2 != code and len(min(c2, code, key=len)) >= 5 and (c2.startswith(code) or code.startswith(c2)) \
                        and not t["tag"].startswith("PAHU"):
                    return sh, t
        return None

    def overall_review(self) -> None:
        """Tags of the overall schematics against the AHU list of the zone schedule.  The overall sheets
        give tags and no duties, so the duty comparison is made on the air-side sheets."""
        i = self.i
        on_sheet: Dict[str, List[Dict[str, Any]]] = {sh: self.read_overall(sh) for sh in self.OVERALL}
        found_keys: Dict[str, str] = {}
        unit_keys = {k for u in i.units if u["has_tag"] for k in u["akeys"]}
        n_tags = 0
        n_out = 0
        for sh, tags in on_sheet.items():
            for t in tags:
                n_tags += 1
                ks = tag_keys(t["tag"])
                for k in ks:
                    found_keys.setdefault(k, sh)
                srv = re.search(r"-B-[A-Z0-9]{1,2}-(B\d|\d\d)", t["tag"])
                lv = srv.group(1) if srv else ""
                in_scope = lv.startswith("B") or (lv.isdigit() and 1 <= int(lv) <= 7)
                if not in_scope:
                    n_out += 1
                    continue
                known = unit_keys | set(i.schedule) | set(self.work_tag_keys) | set(self.tender_tag_keys)
                if not (ks & known):
                    self.consist.append(mk(t["tag"], "queries", "Consistency", t["tag"], "Tag on the overall schematic", "no.", "1 tag", 0,
                                           "not in zone schedule", "Query: tag on the overall schematic is in no unit of the zone schedule",
                                           True, sh, 1, t["box"], f"Overall schematic {sh} p1",
                                           "The tag is in the schedule, the air-side sheets and the zone schedule nowhere. Check the tag form. The overall schematic gives no duty."))
        # units that no overall sheet shows
        missing: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for u in i.units:
            if not u["has_tag"] or u["is_group"] or u["type"] != "AHU" or u["list_only"]:
                continue
            if u["akeys"] & set(found_keys):
                continue
            near = self.overall_near_tag(u, on_sheet)
            if near is not None:
                # correction pass (Opus check items 2 and 7; build lines 19 and 24): a tag of the same family is drawn on the
                # overall schematic.  A near-match of the tag code is a query, not an addition.
                nsh, nt = near
                self.consist.append(mk(u["tag"], "queries", "Consistency", u["tag"], "AHU of the zone schedule not on the overall schematic",
                                       "L/s", "not drawn", None, "tag to correct",
                                       "Query: tag to correct; a near-match of this tag is drawn on the overall schematic", True,
                                       nsh, 1, nt["box"], u["source"],
                                       f"The overall schematic {nsh} prints '{nt['tag']}'. The zone schedule unit is '{u['tag']}'. The tag codes differ. "
                                       "No unit is added. Confirm which tag is right."))
                continue
            lvl = re.search(r"-B-(\d\d)-", u["tag"])
            sh = self.OVERALL[0] if (lvl and int(lvl.group(1)) >= 19) else self.OVERALL[1]
            missing[sh].append(u)
        for sh, us in sorted(missing.items()):
            for u in sorted(us, key=lambda x: x["tag"]):
                m = i.master.get(u["tag"]) or {}
                req = m.get("Calculated airflow")
                g = self.gate(u)
                if g["ok"] and req:
                    self.consist.append(mk(u["tag"], "new systems", "Consistency", u["tag"], "AHU of the zone schedule not on the overall schematic",
                                           "L/s", "not drawn", req, "missing on overall",
                                           "Mark up: add the AHU to the overall schematic (tabulated note)", True, sh, 1, None, u["source"],
                                           "The overall schematic gives tags and no duties. Draw the AHU at its served level."))
                else:
                    self.consist.append(mk(u["tag"], "queries", "Consistency", u["tag"], "AHU of the zone schedule not on the overall schematic",
                                           "L/s", "not drawn", None, "missing on overall",
                                           "Query: AHU of the zone schedule is not on the overall schematic; required value not established - room list",
                                           True, sh, 1, None, u["source"],
                                           "The overall schematic gives tags and no duties. " + self.gate_note(req, "L/s", [g]),
                                           **self.gate_extra([g], req)))
        self.overall_info = {"tags_read": n_tags, "tags_above_L07": n_out, "per_sheet": {sh: len(t) for sh, t in on_sheet.items()},
                             "units_missing": {sh: len(v) for sh, v in missing.items()}}

    # -- item 7: Appendix D reconciliation ------------------------------------------ #
    def appd_review(self) -> None:
        i = self.i
        rec = read_csv(i.reg / "appd_schedule_reconciliation.csv")
        eq = read_csv(i.reg / "appd_equipment.csv")
        spaces = read_csv(i.reg / "appd_spaces.csv")
        pos: Dict[str, Tuple[Any, Any]] = {}
        spbox: Dict[Tuple[str, str], Any] = {}
        for s in i.appd:
            for e in s["equipment"]:
                pos.setdefault(e["tag_as_written"], (e.get("page"), e.get("box")))
            for sp in s["spaces"]:
                spbox[(str(sp["page"]), str(sp["annotation_index"]))] = sp.get("box")
        out: List[Row] = []
        appd_keys = set()
        for r in rec:
            t = r["appd_tag_as_written"]
            ks = tag_keys(t)
            appd_keys |= set(ks)
            pg, bx = pos.get(t, (r["appd_pages"], None))
            src = f"Appendix D, drawing {r['appd_drawing_numbers']}, p{r['appd_pages']}"
            st = r["appd_storeys"]
            # 1 layout tag not in the schedule
            if r["found_in_schedule"] != "y":
                sib = self.sibling_tags(t, rec)
                txt = (r["possible_counterpart"] or "").strip()
                if sib:
                    txt = (txt + " " if txt else "") + "The same tag form is on other storeys: " + ", ".join(sib) + "."
                if not txt:
                    txt = "No similar tag found."
                out.append(mk("App D", "queries", "Layout tag not in the schedule", t, f"Storey {st}", "L/s",
                              "no schedule row", None, "not in schedule", "Query: tag on the layout, not in the schedule", True,
                              "App D", pg, bx, src, short(txt, 400)))
            # 3 variants and duplicates (the form on the layout differs from the form in the schedule)
            vreason = r["appd_variant_reason"] or ""
            if not r["appd_variant_flag"] and r["match_method"] == "variant-explained":
                vreason = r["match_explanation"] or "the form in the schedule differs"
            if r["appd_variant_flag"] or vreason:
                out.append(mk("App D", "queries", "Variant tag", t, f"Storey {st}", "L/s", r["schedule_tag_as_written"] or "",
                              None, "variant", "Query: confirm the tag form", True, "App D", pg, bx, src,
                              vreason + (" Schedule form: " + r["schedule_tag_as_written"] if r["schedule_tag_as_written"] else "")))
            dwg = [x for x in re.split(r"[;|,]\s*", r["appd_drawing_numbers"] or "") if x]
            if len(set(dwg)) > 1:
                out.append(mk("App D", "queries", "Tag on several drawings", t, f"Storeys {st}", "no.", len(set(dwg)), 1, "duplicate",
                              "Query: the same tag is on several layouts", True, "App D", pg, bx, src,
                              "Drawings: " + ", ".join(sorted(set(dwg)))))
            # 6 duty against the workbook D00
            sched = num(r["airflow"])
            wv, wsrc = self.d00_value(t)
            if sched is not None or wv is not None:
                if sched is None or wv is None:
                    cls, act, need = "not established", "Query: duty not compared", True
                    note = "The schedule or the workbook gives no value for this tag."
                elif wv <= 0.5 and sched > 0.5:
                    cls, act, need = "not established", "Query: no room is tied to this fan", True
                    note = "The workbook ties no room to this tag, so it gives 0 L/s. The scheduled duty is kept."
                else:
                    cls, act, need = judge(sched, round(wv, 1), False)
                    note = ""
                    if cls == "margin":
                        act, need = "Keep scheduled duty; margin recorded", False
                    elif cls == "increase":
                        act = "New drawing: show the larger workbook value"
                        # Fix after verification (critical 3, D-083): the input gate applies to every unit-level increase,
                        # also on an App D unit.  Unit not in the zone schedule, room list incomplete or cover outside 0.90-1.10:
                        # a query; both values stay on the drawing.
                        ks_ = tag_keys(t)
                        u_ = next((x for x in i.units if tag_keys(x["tag"]) & ks_), None)
                        g_ = self.gate(u_) if u_ is not None else None
                        if g_ is None or not g_["ok"]:
                            why_ = ["the unit is not in the zone schedule"] if g_ is None else g_["reasons"]
                            cls, act, need = "not established", self.GATE_ACTION, True
                            note = ("The room list of the unit does not establish a required value (" + "; ".join(why_) + "). "
                                    + (f"Calculated value {wv:,.0f} L/s is {g_['text']}. " if g_ is not None else
                                       f"The workbook value {wv:,.0f} L/s is for a unit that is not in the zone schedule. ")
                                    + "The scheduled duty stays.")
                    elif cls == "reduction":
                        act = "New drawing: show the scheduled duty; margin recorded"
                        need = False
                out.append(mk("App D", "airflows" if cls != "not established" else "queries", "Duty against workbook", t,
                              f"Storey {st}", "L/s", sched, round(wv, 1) if wv is not None else None, cls, act, need,
                              "App D", pg, bx, f"{src}; Equipment schedule (Corr 5) p{r['schedule_pages'] or '-'}; {wsrc}", note))
        # 1b systems of an App D layout that a working sheet draws as well: one sheet holds the system (the working
        # sheet, where it is marked up); the App D sheet refers to it
        done_w = set()
        for sy in i.appd:
            if sy["kind"] == "appd_space_without_equipment":
                continue
            for e in sy["equipment"]:
                t = e["tag_as_written"]
                hits = [h for k in tag_keys(t) for h in self.work_tag_keys.get(k, []) if h.get("sheet")]
                if not hits or t in done_w:
                    continue
                done_w.add(t)
                shs = sorted({h["sheet"] for h in hits})
                st_ = (sy.get("storey") or [""])[0]
                out.append(mk("App D", "queries", "App D tag on a working sheet", t, f"Level {int(st_):02d}", "", "drawn on " + ", ".join(shs), None,
                              "on working sheet", "Query: system is drawn on a working sheet; the App D sheet refers to it", True,
                              "App D", e.get("page") or 1, e.get("box"),
                              f"Appendix D, drawing {', '.join(sy.get('drawing_numbers') or [])}, p{e.get('page') or ''}; working drawing {', '.join(shs)}",
                              f"The working sheet {', '.join(shs)} draws this unit and the changes to it are marked up there. "
                              "The App D sheet does not draw it again."))
        # 2 schedule tags for common areas that no layout shows
        for r in read_csv(i.reg / "ahu_schedule.csv"):
            if "common" not in (r.get("served_area [Description]") or "").lower():
                continue
            if tag_keys(r["tag_normalised"]) & appd_keys:
                continue
            out.append(mk("App D", "queries", "Schedule tag not on a layout", r["tag_as_written"],
                          f"Level {r.get('served_level_from_tag')}", "L/s", num(r.get("supply_airflow_l_s [Supply Airflow (l/s)]")), None,
                          "not on layout", "Query: scheduled for common areas, on no App D layout", True, "App D", 1, None,
                          f"Equipment schedule (Corr 5) p{r['page(s)']}", (r.get("served_area [Description]") or "")[:200]))
        emitted = {x["item"] for x in out if x["row_type"] == "Schedule tag not on a layout"}
        for r in read_csv(i.reg / "schedule_common_area_items_not_in_appd.csv"):
            kw = r.get("keywords_matched") or ""
            if r.get("within_appd_storeys_2_to_7") != "y" or r["tag_as_written"] in emitted:
                continue
            if not re.search(r"corridor|common|toilet|caregiver|staff area|plant room", kw):
                continue
            if tag_keys(r["tag_as_written"]) & appd_keys:
                continue
            emitted.add(r["tag_as_written"])
            out.append(mk("App D", "queries", "Schedule tag not on a layout", r["tag_as_written"],
                          f"Level {r.get('served_level_from_tag')}", "L/s", num(r.get("airflow")), None,
                          "not on layout", "Query: scheduled for common areas, on no App D layout", True, "App D", 1, jbox(r.get("tag_bbox_pt")),
                          f"Equipment schedule (Corr 5) p{r['pages']}",
                          (r.get('description') or r.get('location') or '') + f". Words in the schedule text: {kw}."))
        for r in read_csv(i.reg / "equipment_schedule_fans.csv"):
            if "common" not in (r.get("Description") or "").lower():
                continue
            if tag_keys(r["tag_normalised"]) & appd_keys or not r["tag_normalised"]:
                continue
            out.append(mk("App D", "queries", "Schedule tag not on a layout", r["tag_as_written"],
                          f"Location {r.get('Location')}", "L/s", num(r.get("Min Airflow (l/s)")), None, "not on layout",
                          "Query: scheduled for common areas, on no App D layout", True, "App D", 1, None,
                          f"Equipment schedule (Corr 5) p{r['page']}", (r.get("Description") or "")[:200]))
        # 4 highlighted spaces without equipment; 5 equipment not inside a space
        for s in i.appd:
            if s["kind"] != "appd_space_without_equipment":
                continue
            sp = s["spaces"][0]
            mode = sp.get("mode_label_as_written") or ""
            nat = mode.strip().upper() == "NV"
            out.append(mk("App D", "queries", "Highlighted space with no equipment", sp["name_as_written"][:80],
                          f"Storey {sp['storey']}", "m²", sp["area_m2"], None, "no equipment",
                          "No change: natural ventilation" if nat else "Query: space has no fan, FCU or AHU on the layout",
                          not nat, "App D", sp["page"], sp.get("box"),
                          f"Appendix D, drawing {sp['drawing_number']}, p{sp['page']}, space {sp['space_ref']}",
                          f"Mode on the layout: {mode or 'none'}. Area in column Scheduled duty is in m²."))
        for e in eq:
            if e["space_relation"] != "inside":
                key = e["tag_as_written"]
                pg, bx = pos.get(key, (e["page"], None))
                out.append(mk("App D", "queries", "Equipment not inside a space", key, f"Storey {e['storey']}", "pt",
                              e["space_distance_pt"], 0, e["space_relation"], "Query: confirm the space this equipment serves", True,
                              "App D", e["page"], bx, f"Appendix D, drawing {e['drawing_number']}, p{e['page']}",
                              f"Nearest space: {e['space_name'][:100]}. Distance to the polygon in column Scheduled duty is in points."))
        # 7 outdoor-air and exhaust path
        for s in i.appd:
            if s["kind"] == "appd_space_without_equipment":
                continue
            e0 = s["equipment"][0] if s["equipment"] else {}
            out.append(mk("App D", "airflows", "Outdoor-air and exhaust path", s["system_id"],
                          f"Storey {(s.get('storey') or [''])[0]}", "", None, None, "no change", "No change", False,
                          "App D", (s.get("page") or [1])[0], e0.get("box"),
                          f"Appendix D, drawing {', '.join(s.get('drawing_numbers') or [])}",
                          (s.get("outdoor_air_exhaust_path") or "") +
                          (" Rates beside fans: " + "; ".join(map(str, s["rates_beside_fans_as_written"])) if s.get("rates_beside_fans_as_written") else "")))
        # 8 space on another AHU in the workbook than the one the layout shows
        d00 = self.i.wb.get("00", {}).get("airflow", {})
        done = set()
        for sy in i.appd:
            if sy["kind"] != "appd_ahu_system":
                continue
            sk = set()
            for e in sy["equipment"]:
                sk |= set(tag_keys(e["tag_as_written"]))
            for sp in sy["spaces"]:
                st_ = int(sp["storey"])
                key = f"CA-L{st_:02d}-{int(sp['annotation_index']):03d}"
                w = d00.get(key)
                if not w or not w["ahu"] or key in done:
                    continue
                if tag_keys(str(w["ahu"])) & sk or str(w["ahu"]).strip() in ("AHU", ""):
                    continue
                done.add(key)
                out.append(mk("App D", "queries", "Space on another AHU", f"Space {sp['annotation_index']}: {w['name']}", f"Storey {st_}",
                              "", sy["equipment"][0]["tag_as_written"], str(w["ahu"]), "differs",
                              "Query: workbook puts the space on another AHU", True, "App D", sp["page"], sp.get("box"),
                              f"Appendix D, drawing {sp['drawing_number']}, p{sp['page']}; Workbook D00 tab Airflow row {w['row']}",
                              f"The layout shows the space under {sy['equipment'][0]['tag_as_written']}. The workbook gives {w['ahu']}."))
        self.appd_rows = out
        self.rows.extend(out)

    @staticmethod
    def sibling_tags(t: str, rec: List[Dict[str, str]]) -> List[str]:
        """Same tag form on other storeys, for example OAF-B-03-03DISP-01 for OAF-B-04-04DISP-01."""
        m = re.match(r"^([A-Z]+)-B-\d\d-\d\d(.+)$", t)
        if not m:
            return []
        rx = re.compile(rf"^{m.group(1)}-B-\d\d-\d\d{re.escape(m.group(2))}$")
        return sorted({r["appd_tag_as_written"] for r in rec if rx.match(r["appd_tag_as_written"]) and r["appd_tag_as_written"] != t
                       and r["found_in_schedule"] == "y"})

    def d00_value(self, tag: str) -> Tuple[Optional[float], str]:
        d = self.i.wb.get("00")
        if not d:
            return None, "no D00 workbook"
        ks = tag_keys(tag)
        pre = tag.split("-")[0]
        if pre in ("AHU", "FCU", "PAHU"):
            for r in d["ahu"]:
                if tag_keys(r["tag"]) & ks and r["tag"].split("-")[0] == pre:
                    return r["supply"], f"Workbook D00 tab AHU cell {r['cols']['supply']}"
        else:
            for r in d["fans"]:
                if tag_keys(r["tag"]) & ks and r["tag"].split("-")[0] == pre:
                    return r["calc"], f"Workbook D00 tab Fans cell {r['cell']}"
        return None, "no row in the D00 workbook"

    # -- MOT sheets: reviewed and listed, not marked up --------------------------------- #
    # -- MOT: list-only units and fans, reviewed and listed, never marked up (D-082, build line 23) ------------------- #
    def mot_review(self) -> None:
        i = self.i
        sched: Dict[str, Dict[str, Any]] = {}
        for r in self.mot_sched_rows:
            for k in sorted(tag_keys(r["tag"])):
                cur = sched.get(k)
                if cur is None or str(r["issue"]) > str(cur["issue"]):
                    sched[k] = {"tag": r["tag"], "issue": r["issue"], "supply": num(r["supply_airflow_l_s"]), "page": r["page"]}
        rows = self.mot_rows
        done_keys = set()
        sheets_listed: Dict[str, List[str]] = defaultdict(list)

        def src_of(r, sc):
            return (f"Working drawing {r['sheet']} p1" + (f", box {box_s(r.get('_dutybox') or r['_box'])}" if r["_box"] else "") +
                    (f"; MOT AC equipment schedule {sc['issue']} p{sc['page']}" if sc else "; no MOT schedule row"))

        def add(sheet, tag, drawn, scheduled, result, issue, page, box, source, note, ent=None):
            rows.append({"sheet": sheet, "tag": tag, "drawn": drawn, "scheduled": scheduled,
                         "diff": (scheduled - drawn) if (drawn is not None and scheduled is not None) else None,
                         "result": result, "issue": issue, "page": page, "box": box_s(box) if box else "", "source": source,
                         "note": note, "ledger_id": ent["entity_id"] if ent else ""})

        for r in self.led.rows:
            if r["sheet_role"] != "working" or r["kind"] not in ("AHU tag", "fan tag", "PAHU tag"):
                continue
            tg = r["_tag"]
            if r["kind"] == "AHU tag":
                us = [u for k in tag_keys(tg) for u in self.units_by_key.get(k, [])]
                lo = (bool(us) and all(u["list_only"] for u in us)) or (not us and self.drawn_only_list_only(tg))
            elif r["kind"] == "fan tag":
                lo = self.fan_list_only(tg)[0]
            else:
                lo = "XXX" in tg.upper() and self.sheet_list_only(r["sheet"])
            if not lo:
                continue
            if r["kind"] == "PAHU tag":
                add(r["sheet"], tg, None, None, "PAHU tag printed XXX", "", 1, r["_box"], f"Working drawing {r['sheet']} p1",
                    "The sheet prints the outdoor-air unit tag as XXX. The tag is listed and not corrected.", r)
                sheets_listed[r["sheet"]].append(tg)
                continue
            if not r.get("_equip"):
                continue               # a mention of the tag in a note or label: the drawn box is the row
            sc = next((sched[k] for k in sorted(tag_keys(tg)) if k in sched), None)
            d = r["value"] if r["value"] != "" else None
            sv = sc["supply"] if sc else None
            if sc is None:
                res = "no scheduled value found"
            elif d is None:
                res = "duty not drawn"
            elif sv is None:
                res = "no scheduled value found"
            elif d == sv:
                res = "same"
            elif abs(d - sv) <= max(INC_PCT * sv, INC_LPS):
                res = "differs, within 5 % or 10 L/s"
            else:
                res = "differs"
            dent = next((x for x in self.led.rows if x["kind"] == "duty" and x["parent_id"] == r["entity_id"]), None)
            add(r["sheet"], tg, d, sv, res, sc["issue"] if sc else "", 1, (dent or r)["_box"], src_of(dict(r, _dutybox=(dent or {}).get("_box")), sc),
                "Reviewed and listed. Not marked up. Operating theatres keep their specified values." if r["kind"] == "AHU tag" else
                "Fan of a list-only unit. Reviewed and listed. Not marked up.", dent or r)
            done_keys |= set(tag_keys(tg))
            sheets_listed[r["sheet"]].append(tg)
        # scheduled units and fans that no working sheet draws
        for k, sc in sorted(sched.items()):
            if k in done_keys:
                continue
            pre = sc["tag"].split("-")[0]
            if pre not in ("AHU", "PAHU") + FAN_PREFIXES:
                continue
            seen_on = [x for x in self.led.by_key.get(k, []) if x["sheet_role"] == "working" and x["kind"] in ("AHU tag", "fan tag", "PAHU tag")]
            if seen_on:
                # printed on a sheet as a label only (no duty box read): one row on that sheet, with the scheduled duty
                for sh_ in sorted({x["sheet"] for x in seen_on}):
                    add(sh_, sc["tag"], None, sc["supply"], "tag printed; no duty box found", sc["issue"], 1,
                        next((x["_box"] for x in seen_on if x["sheet"] == sh_), None),
                        f"Working drawing {sh_} p1; MOT AC equipment schedule {sc['issue']} p{sc['page']}",
                        "The tag is printed on the sheet and no duty is drawn with it. The scheduled duty is listed. Not marked up.")
                done_keys.add(k)
                continue
            if pre in FAN_PREFIXES:
                add("", sc["tag"], None, sc["supply"], "scheduled fan not found on the sheets", sc["issue"], "", None,
                    f"MOT AC equipment schedule {sc['issue']} p{sc['page']}",
                    "The schedule lists this fan. No working sheet draws a fan with this tag. Listed, not marked up.")
            else:
                add("", sc["tag"], None, sc["supply"], "scheduled, on no working sheet", sc["issue"], "", None,
                    f"MOT AC equipment schedule {sc['issue']} p{sc['page']}", "Reviewed and listed. Not marked up.")
            done_keys.add(k)
        # list-only units of the zone schedule: drawn nowhere, or no schedule row, or not on the overall schematic
        ov_keys = {k for r in self.led.rows if r["kind"] == "overall tag" for k in r["tag_keys"].split("; ") if k}
        for u in i.units:
            if not (u["has_tag"] and u["list_only"] and u["type"] == "AHU" and not u.get("is_group")):
                continue
            sc = next((sched[k] for k in u["akeys"] if k in sched), None)
            drawn_on = self.led.sheets_of_keys(u["akeys"])
            if not drawn_on:
                add("", u["tag"], None, sc["supply"] if sc else None,
                    "no scheduled value found" if not sc else "not drawn on a working sheet", sc["issue"] if sc else "", "", None,
                    u["source"] + (f"; MOT AC equipment schedule {sc['issue']} p{sc['page']}" if sc else ""),
                    "The unit is in the zone schedule (" + u["list_reason"] + "). " +
                    ("The MOT schedule has no row for it. " if not sc else "") + "Listed, not marked up.")
            elif not sc:
                add(drawn_on[0], u["tag"], None, None, "no scheduled value found", "", 1, None, u["source"],
                    "The unit is a MOT department unit. The MOT schedule has no row for it. Listed, not marked up.")
            if not (u["akeys"] & ov_keys):
                lvl_ = re.search(r"-B-(\d\d)-", u["tag"])
                add(self.OVERALL[0] if (lvl_ and int(lvl_.group(1)) >= 19) else self.OVERALL[1], u["tag"], None, sc["supply"] if sc else None, "not on the overall schematic",
                    sc["issue"] if sc else "", 1, None, u["source"],
                    "The overall schematic does not show this list-only unit. It is listed here with its scheduled duty. Nothing is added to the overall schematic.")
        # tag differences between the MOT schematics and the schedules (existing cross-reference)
        for r in read_csv(self.i.root / "out/qa/mot_tag_differences.csv"):
            rows.append({"sheet": "", "tag": r["governing_tag"], "drawn": num(r["airflow_schematic_l_s"]),
                         "scheduled": num(r["airflow_ac_schedule_l_s"]), "diff": None,
                         "result": "tag difference: " + (r["difference_type"] or "")[:80], "issue": "", "page": "", "box": "",
                         "source": "MOT tag cross-reference, row " + str(r["crossref_row"]),
                         "note": (r["detail"] or "")[:300] + " Listed, not marked up.", "ledger_id": ""})
        # sheet status rows
        for sh in sorted(self.sheet_scope):
            sc_ = self.sheet_scope[sh]
            has = any(u["list_only"] for u in i.units if u["has_tag"] and self.led.sheets_of_keys(u["akeys"]) and sh in self.led.sheets_of_keys(u["akeys"])) \
                or sc_["list_only"]
            if not has:
                continue
            rows.append({"sheet": sh, "tag": "(sheet)", "drawn": None, "scheduled": None, "diff": None,
                         "result": "REVIEWED, listed, no mark-up" if sc_["list_only"] else "REVIEWED, mixed sheet: list-only units listed",
                         "issue": "", "page": 1, "box": "", "source": f"Working drawing {sh}",
                         "note": ("Every AHU on the sheet is list-only. No mark-up file is written." if sc_["list_only"] else
                                  "Units that are not list-only are marked up. The list-only units and fans are on this tab."), "ledger_id": ""})
        for m_ in rows:
            for k_ in ("result", "note", "source", "tag"):
                if isinstance(m_.get(k_), str):
                    m_[k_] = clean(m_[k_])
        rows.sort(key=lambda m: (m["sheet"] == "", m["sheet"], m["tag"] != "(sheet)", m["tag"]))

    # -- zoning against sheets (D-085, build lines 21 and 22) -------------------------------------------------------- #
    def zoning_queries(self) -> None:
        i = self.i
        self.zoning_box_ids: set = set()
        seen_groups = set()
        # drawn AHU absent from the zone schedule: one row with the drawn duty and the list of its boxes
        for tg, v in sorted(self.drawn_only_units.items()):
            if v["list_only"]:
                continue
            ents = [r for r in self.led.rows if r["kind"] in ("AHU tag", "FCU tag") and r["_tag"] == tg and r["sheet_role"] == "working"]
            e0 = next((e for e in ents if e.get("_equip")), ents[0])
            ks = tag_keys(tg)
            names, boxes = [], []
            for r in self.led.rows:
                rm = r.get("_rm")
                if r["kind"] == "room box" and rm and r["sheet"] in v["sheets"]:
                    tk = frozenset().union(*[tag_keys(a["value"] if isinstance(a, dict) else a) for a in rm["ahu_tags_as_written"]]) \
                        if rm["ahu_tags_as_written"] else frozenset()
                    if tk & ks:
                        names.append(r["text"])
                        boxes.append(r)
            for r in boxes:
                self.zoning_box_ids.add(r["entity_id"])
            seen_groups.add(ks)
            dv = e0["value"] if e0["value"] != "" else "drawn"
            mr = mk(tg, "queries", "Zoning", tg, "Unit drawn on the sheet, not in the zoning", "L/s", dv, "no unit in the zone schedule",
                    "not in zone schedule", "Query: drawn unit not in the approved zoning", True, e0["sheet"], 1, e0["_box"],
                    f"Working drawing {e0['sheet']} p1, tag box; zone schedule: no unit with this tag",
                    (f"The sheet draws this AHU with {len(set(names))} room boxes: {short(', '.join(sorted(set(names))), 200)}. "
                     if names else "The sheet draws this AHU with no room box of its own. ") +
                    "No room and no airflow is marked up for it. The zoning and the sheet are not changed.", ledger_id=e0["entity_id"])
            self.zoning_rows.append(mr)
        # room boxes whose printed tag is in no zone unit
        grp: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
        for r in self.led.rows:
            rm = r.get("_rm")
            if r["kind"] != "room box" or not rm or not rm["ahu_tags_as_written"] or r["entity_id"] in self.zoning_box_ids:
                continue
            texts = [a["value"] if isinstance(a, dict) else a for a in rm["ahu_tags_as_written"]]
            tk = frozenset().union(*[tag_keys(t) for t in texts])
            if not tk:
                continue
            if any(self.units_by_key.get(k) for k in tk):
                continue
            if tk & self.mot_sched_keys or (r["sheet"] in self.GUARD_MOT_SHEETS) or self.drawn_only_list_only(texts[0]):
                continue
            grp[(r["sheet"], "; ".join(sorted(set(texts))))].append(r)
        for (sh, tgs), lst in sorted(grp.items()):
            for r in lst:
                self.zoning_box_ids.add(r["entity_id"])
            names = sorted({r["text"] for r in lst})
            mr = mk(tgs, "queries", "Zoning", tgs, "Room boxes with a printed tag in no zone", "no.", f"{len(lst)} boxes", "no unit in the zone schedule",
                    "not in zone schedule", "Query: drawn unit not in the approved zoning", True, sh, 1, lst[0]["_box"],
                    f"Working drawing {sh} p1, room boxes; zone schedule: no unit with this tag",
                    f"{len(lst)} room boxes print the tag {tgs}. The zone schedule has no unit with this tag. Boxes: "
                    f"{short(', '.join(names), 200)}. No room or airflow is marked up for these boxes.", ledger_id=lst[0]["entity_id"])
            self.zoning_rows.append(mr)
        for c in self.zoning_conflicts:
            mr = mk(c["unit"], "queries", "Zoning", c["unit"], "Service area on the schedule contradicts the zoning", "", c["schedule"],
                    "; ".join(c["rooms"][:6]), "zoning conflict", "Query: zoning conflict; the service area of the unit has two readings", True,
                    "NEW", 1, None, f"Equipment schedule (Corr 5) p{c['page']}; zone schedule, tab AHU summary, row {c['unit_row']}",
                    f"Reading 1, equipment schedule p{c['page']}: the unit serves '{c['schedule']}'. Reading 2, approved zoning "
                    f"(record {c['record'] or 'in the zone schedule'}): the unit serves {short(', '.join(c['rooms']), 120)}. "
                    "The zoning and the schedule are not changed. Room and total mark-ups of this unit are held as queries.")
            self.zoning_rows.append(mr)
        # sheets whose unit cannot be established (build line 22): one query for each sheet, every box not reviewed
        un_sheets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for us in i.unassigned:
            if us["rooms"]:
                un_sheets[us["sheet"]] += us["rooms"]
        have_sys = {w["sheet"] for w in i.working if w["kind"] == "working_ahu_system"}
        self.partial_not_reviewed: Dict[str, int] = {}
        for sh, rms in sorted(un_sheets.items()):
            if sh in have_sys:
                self.partial_not_reviewed[sh] = len(rms)
            else:
                self.not_reviewed_sheets[sh] = "unit not established"
            ent = next((r for r in self.led.rows if r["kind"] == "room box" and r["sheet"] == sh), None)
            names = [rm["name"]["value"] for rm in rms]
            mr = mk(sh, "queries", "Zoning", sh, "Sheet: unit not established", "no.", f"{len(rms)} box(es)", "not established", "not reviewed",
                    ("Query: AHU of the sheet not established; room boxes not reviewed" if sh not in have_sys else
                     "Query: room boxes without an AHU tag on a sheet with several systems; boxes not reviewed"), True, sh, 1, ent["_box"] if ent else None,
                    f"Working drawing {sh} p1",
                    (f"The tag of the AHU was looked for in the sheet text, the title and the room links. It is not established. "
                     f"Every box ({len(rms)}) is not reviewed: unit not established. " if sh not in have_sys else
                     f"{len(rms)} box(es) carry no AHU tag and the sheet has several systems. No unit holds a room of this name alone. "
                     f"These boxes are not reviewed: unit not established. ") + f"Boxes: {short(', '.join(names), 200)}.",
                    ledger_id=ent["entity_id"] if ent else "")
            self.zoning_rows.append(mr)
        for r in self.zoning_rows:
            self.rows.append(r)
        self.missing.extend([r for r in self.zoning_rows if r["quantity"].startswith("Sheet:")])

    # -- the one disposition of every ledger row (D-077, build line 5) -------------------------------------------- #
    def assign_dispositions(self) -> Dict[str, Any]:
        L = self.led
        D = L.dispose
        stats: Counter = Counter()
        by_ent: Dict[str, List[Row]] = defaultdict(list)
        by_lid: Dict[str, List[Row]] = defaultdict(list)
        for r in {id(x): x for x in self.rows}.values():
            for e in [r.get("_ent")] + list(r.get("_ents") or []):
                if e is not None and r not in by_ent[e["entity_id"]]:
                    by_ent[e["entity_id"]].append(r)
            for lid in [r.get("ledger_id")] + [x for x in str(r.get("ledger_ids") or "").split("; ")]:
                if lid:
                    by_lid[lid].append(r)

        def home(u: Dict[str, Any]) -> str:
            return self.sheet_of(u)[0]

        def unit_text(r: Dict[str, Any], u: Optional[Dict[str, Any]], cls: str) -> Tuple[str, str, str]:
            if u is not None and u["list_only"]:
                return "list only (MOT)", u["tag"], "listed (MOT)"
            h = home(u) if u else ""
            if u is not None and h and r["sheet"] != h and r["kind"] in ("AHU tag", "FCU tag", "OA value", "spill or relief label",
                                                                          "exhaust link", "BIBO tag", "duty"):
                return f"reviewed under unit {u['tag']} on sister sheet {h}", u["tag"], cls
            return f"reviewed under unit {u['tag'] if u else ''}", (u["tag"] if u else ""), cls

        def best_unit(keys: Iterable[str]) -> Optional[Dict[str, Any]]:
            cands = []
            for k in keys:
                for u in self.units_by_key.get(k, []):
                    if u not in cands:
                        cands.append(u)
            if not cands:
                return None
            pref = [u for u in cands if not u.get("is_group") and u["systems"]] or [u for u in cands if not u.get("is_group")] or cands
            return pref[0]

        # 1. room boxes
        for r in L.rows:
            if r["kind"] != "room box":
                continue
            rows_ = by_ent.get(r["entity_id"], [])
            win = None
            for x in rows_:
                w = x
                while w.get("ref") is not None:
                    w = w["ref"]
                if w["row_type"] == "Room" and (win is None or self.RANK.get(w["cls"], 3) < self.RANK.get(win["cls"], 3)):
                    win = w
            sysid = r["system"]
            if win is not None:
                u = self.unit_by_tag.get(win["unit"])
                if u is not None and u["list_only"]:
                    D(r, "list only (MOT)", u["tag"], "listed (MOT)")
                else:
                    D(r, f"reviewed under unit {win['unit']}", win["unit"], win["cls"])
            elif r["entity_id"] in self.zoning_box_ids:
                D(r, "query: drawn, not in the zone schedule", "", "zoning query")
            elif self._sys_unassigned(r) and (r["sheet"] in self.not_reviewed_sheets or r["sheet"] in self.partial_not_reviewed):
                D(r, "not reviewed: unit not established", "", "not reviewed")
            else:
                us = [u for u in self.units_by_key_for_system(r["_sys"])] if r.get("_sys") and r["_sys"]["system_id"] in self.sys_keys else []
                if us and all(u["list_only"] for u in us):
                    D(r, "list only (MOT)", us[0]["tag"], "listed (MOT)")
                elif r["_rm"] and r["_rm"].get("ahu_tags_as_written") and self._box_tags_list_only(r["_rm"]):
                    D(r, "list only (MOT)", "", "listed (MOT)")
                elif self._sys_unassigned(r):
                    self.not_reviewed_sheets.setdefault(r["sheet"], "unit not established")
                    D(r, "not reviewed: unit not established", "", "not reviewed")
                else:
                    stats["room box without row"] += 1
        # 2. room values follow their room box
        for r in L.rows:
            if r["kind"] == "room value":
                p = L.by_id.get(r["parent_id"])
                if p and p["disposition"]:
                    vr = by_lid.get(r["entity_id"], [])
                    D(r, p["disposition"], p["disposition_unit"], (vr[0]["cls"] if vr else p["disposition_class"]))
                else:
                    stats["room value without room"] += 1
        # 3. AHU and FCU tags
        for r in L.rows:
            if r["kind"] not in ("AHU tag", "FCU tag") or r["sheet_role"] != "working":
                continue
            tg = r["_tag"]
            u = best_unit(r["tag_keys"].split("; "))
            if u is None:
                if self.drawn_only_list_only(tg):
                    D(r, "list only (MOT)", tg, "listed (MOT)")
                else:
                    D(r, "query: drawn, not in the zone schedule", "", "zoning query")
                continue
            cls = "no change"
            for x in self.unit_info.get(u["tag"], {}).get("rows", []):
                if x["quantity"] == "Supply airflow":
                    cls = x["cls"]
                    break
            D(r, *unit_text(r, u, cls))
        # 4. overall schematic tags
        for r in L.rows:
            if r["kind"] != "overall tag":
                continue
            u = best_unit(r["tag_keys"].split("; "))
            if u is None:
                D(r, "query: drawn, not in the zone schedule", "", "overall schematic query")
            elif u["list_only"]:
                D(r, "list only (MOT)", u["tag"], "listed (MOT)")
            else:
                D(r, f"reviewed under unit {u['tag']} (overall schematic)", u["tag"], "no change")
        # 5. fans
        gby: Dict[str, Dict[str, Any]] = {}
        for g in self.fan_groups:
            for e in g["ents"]:
                gby[e["entity_id"]] = g
        for r in L.rows:
            if r["kind"] != "fan tag":
                continue
            g = gby.get(r["entity_id"])
            if g is None:
                g = next((x for x in self.fan_groups if x["sheet"] == r["sheet"] and set(x["keys"]) & set(tag_keys(r["_tag"]))), None) \
                    or next((x for x in self.fan_groups if set(x["keys"]) & set(tag_keys(r["_tag"]))), None)
            if g is not None:
                D(r, *self._fan_disp(g, mention=g is not gby.get(r["entity_id"])))
            else:
                lo, _ = self.fan_list_only(r["_tag"])
                own = self.fan_owners(r["_tag"])
                if lo:
                    D(r, "list only (MOT)", "", "listed (MOT)")
                elif own:
                    D(r, f"reviewed under unit {own[0]['tag']}" + (" (and others)" if len(own) > 1 else ""), own[0]["tag"], "tag named in a label")
                else:
                    D(r, "query: fan drawn, link to the unit not read", "", "fan: link not read")
        # 7. PAHU tags: shared among the units that take outdoor air from them
        pahu_units: Dict[str, set] = defaultdict(set)
        for s in self.i.working:
            pks = [k for k in sorted(tag_keys(s["outdoor_air"].get("source") or "")) if k.startswith("PAHU:")]
            for ln in s["outdoor_air"]["lines"]:
                u = best_unit(tag_keys(ln["to"]))
                for k in pks:
                    if u is not None:
                        pahu_units[k].add(u["tag"])
        sheet_units: Dict[str, List[str]] = defaultdict(list)
        for r in L.rows:
            if r["kind"] == "AHU tag" and r["disposition_unit"] and r["sheet_role"] == "working":
                if r["disposition_unit"] not in sheet_units[r["sheet"]]:
                    sheet_units[r["sheet"]].append(r["disposition_unit"])
        for r in L.rows:
            if r["kind"] != "PAHU tag" or r["sheet_role"] != "working":
                continue
            us = sorted({t for k in r["tag_keys"].split("; ") for t in pahu_units.get(k, set())})
            if not us:
                us = [t for t in sheet_units.get(r["sheet"], [])]
            lo = [t for t in us if self.unit_by_tag.get(t, {}).get("list_only")]
            nl = [t for t in us if t not in lo]
            if us and not nl:
                D(r, "list only (MOT)", us[0], "listed (MOT)")
            elif len(nl) == 1:
                D(r, f"reviewed under unit {nl[0]}", nl[0], "outdoor air by the AHU row; PAHU duty on the consistency row")
            elif len(nl) > 1:
                D(r, "shared: units " + "; ".join(nl), nl[0], "shared")
            else:
                D(r, "query: PAHU drawn, link to the unit not read", "", "PAHU: link not read")
        # 7b. duty rows (after the tags they belong to)
        for r in L.rows:
            if r["kind"] == "duty":
                p = L.by_id.get(r["parent_id"])
                if p and p["disposition"]:
                    D(r, p["disposition"], p["disposition_unit"], p["disposition_class"])
                else:
                    stats["duty without tag"] += 1
        # 8. the others follow the nearest AHU tag of the sheet
        ahu_by_sheet: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for r in L.rows:
            if r["kind"] == "AHU tag" and r["sheet_role"] == "working":
                ahu_by_sheet[r["sheet"]].append(r)
        for r in L.rows:
            if r["disposition"]:
                continue
            if r["kind"] == "note":
                D(r, "not an air-side entity: general note, not a drawn path", "", "note")
            elif r["kind"] in ("OA value", "spill or relief label", "exhaust link", "BIBO tag"):
                if r["kind"] == "OA value" and r["function"].startswith("room"):
                    # the room's own value: follows the room box that holds the line
                    rb = next((x for x in L.by_sheet[r["sheet"]] if x["kind"] == "room box" and x.get("_rm") and x["_rm"].get("box_table")
                               and _overlap(r["_box"], x["_rm"]["box_table"]) > 0.8 and x["disposition"]), None)
                    if rb:
                        D(r, rb["disposition"], rb["disposition_unit"], "room outdoor air, not compared")
                        continue
                cands = [a for a in ahu_by_sheet.get(r["sheet"], []) if a["disposition"]]
                if not cands:
                    cands = [a for a in L.rows if a["kind"] == "AHU tag" and a["disposition"] and a["sheet_role"] == "working" and
                             a["disposition_unit"] and any(a["sheet"] == x for x in self.led.sheets_of_keys(
                                 [k for k in a["tag_keys"].split("; ")]))]
                if cands and r["_box"]:
                    a = min(cands, key=lambda c: SheetText._dist(c["_box"], r["_box"]))
                    cls = "no change"
                    for x in by_lid.get(r["entity_id"], []):
                        cls = x["cls"]
                    if a["disposition"].startswith("list only"):
                        D(r, "list only (MOT)", a["disposition_unit"], "listed (MOT)")
                    elif a["disposition"].startswith("query"):
                        D(r, a["disposition"], "", a["disposition_class"])
                    else:
                        u = self.unit_by_tag.get(a["disposition_unit"])
                        D(r, *unit_text(r, u, cls))
                else:
                    stats["no unit for " + r["kind"]] += 1
        # 9. what is left has no unit: 'not reviewed' with the reason (the sheet is listed 'not reviewed')
        for r in L.rows:
            if not r["disposition"]:
                D(r, "not reviewed: no AHU or unit is established on the sheet for this entity", "", "not reviewed")
                stats["not reviewed (leftover)"] += 1
        self.disp_stats = stats
        return stats

    def _sys_unassigned(self, r: Dict[str, Any]) -> bool:
        s = r.get("_sys")
        return bool(s) and (s["kind"] == "working_unassigned_rooms")

    def _box_tags_list_only(self, rm: Dict[str, Any]) -> bool:
        texts = [a["value"] if isinstance(a, dict) else a for a in rm["ahu_tags_as_written"]]
        return bool(texts) and all(self.unit_list_only(t) or self.drawn_only_list_only(t) or bool(tag_keys(t) & self.mot_sched_keys)
                                   for t in texts)

    def _fan_disp(self, g: Dict[str, Any], mention: bool = False) -> Tuple[str, str, str]:
        suffix = " (tag named in a label or note)" if mention else ""
        if g["list_only"]:
            return "list only (MOT)", "", "listed (MOT)"
        row = g.get("row")
        cls = row["cls"] if row else "fan"
        own = g["owners"]
        if not own:
            return "query: fan drawn, link to the unit not read", "", cls
        names = [o["tag"] for o in own]
        if len(names) > 1:
            return "shared: units " + "; ".join(names), names[0], cls
        return f"reviewed under unit {names[0]}{suffix}", names[0], cls

    # -- duplicate rows (build line 27) ------------------------------------------------------------------------ #
    def drop_duplicates(self) -> None:
        seen: Dict[Tuple[str, str, str, str], Row] = {}
        n = 0
        for r in self.rows:
            if r["row_type"] != "Room" or not r["needs_id"]:
                continue
            k = (r["unit"], nname(r["item"]), r["action"], r["sheet"])
            if k in seen and seen[k] is not r:
                r["needs_id"] = False
                r["cls"] = "reviewed on another tab"
                r["action"] = "No separate row: the same room and action are on an earlier row of this unit"
                n += 1
            else:
                seen[k] = r
        self.n_dups = n

    # -- change IDs: persistent, keyed by (sheet, entity, quantity, unit) (D-087, build line 8) ----------------------- #
    def assign_ids(self, out: Path = OUT) -> None:
        store = ChangeIdLedger(out / "change_id_ledger.csv", out / "draft1_change_list.csv", self.i.root)
        uniq = list({id(r): r for r in self.rows}.values())
        used: set = set()
        for r in uniq:
            if not r["needs_id"] or r["cls"] == SUPPRESSED_CLASS or r["cls"] == "reviewed on another tab":
                continue
            sheet = r["sheet"] or "NEW"
            ent = str(r["item"])
            qty, un = str(r["quantity"]), str(r["unit"])
            k = (sheet, ent, qty, un)
            if k in used:                       # the same key twice in one build: tell the rows apart, in a fixed order
                for cand in (f"{ent} [{r['row_type']}]", f"{ent} [{r['row_type']}] @{r['box']}"):
                    if (sheet, cand, qty, un) not in used:
                        k = (sheet, cand, qty, un)
                        break
                else:
                    n_ = 2
                    while (sheet, f"{ent} #{n_}", qty, un) in used:
                        n_ += 1
                    k = (sheet, f"{ent} #{n_}", qty, un)
            used.add(k)
            r["change_id"] = store.take(k)
            r["_key"] = k
        store.retire_unseen()
        store.save()
        self.id_store = store
        for r in self.rows:
            w = r.get("ref")
            if w is not None and w.get("change_id"):
                r["action"] += f" (change ID {w['change_id']})"

    def id_map(self, out: Path = OUT) -> List[Dict[str, str]]:
        """change_id_map.csv: every draft-1 change ID of the present issue folder -> new ID or 'withdrawn' with the reason."""
        p = out / "draft1_change_list.csv"
        if not p.exists():
            return []
        cur: Dict[Tuple[str, str, str, str], Row] = {}
        for r in {id(x): x for x in self.rows}.values():
            cur.setdefault((r["sheet"] or "NEW", str(r["item"]), str(r["quantity"]), str(r["unit"])), r)
        by_cur_unit = {t for t in self.unit_by_tag}
        res = []
        for d in read_csv(p):
            if not d.get("change_id"):
                continue
            k = (d["sheet"], d["item"], d["quantity"], d["unit"])
            hit = cur.get(k)
            if hit is not None and hit["change_id"]:
                res.append({"draft1_id": d["change_id"], "new_id": hit["change_id"], "reason": "same key" if hit["change_id"] == d["change_id"]
                            else "same key, new number (the persisted ledger starts at this build)", "sheet": d["sheet"], "item": d["item"],
                            "quantity": d["quantity"], "unit": d["unit"]})
                continue
            if self.unit_list_only(d["unit"]) or self.unit_list_only(d["item"]) or self.drawn_only_list_only(d["unit"]):
                why = "list-only unit or fan (MOT NSC): no change ID, no mark-up"
            elif hit is not None:
                why = f"now '{hit['cls']}': {hit['action']} (no change ID)"
            elif d["quantity"] == "Pressure target":
                why = "pressure row: compared with the workbook row of the room itself; no difference now"
            elif d["layer_type"] == "new systems" or "Missing" in d.get("action", "") or "New system" in d.get("action", ""):
                why = "no 'add' or 'missing' row without a negative lookup on the ledger: the entity is drawn, or the row is now a query under another key"
            elif "return fan" in d.get("action", "").lower() or "spill" in d.get("action", "").lower():
                why = "return fan, spill or relief path with nothing drawn is a query, never an instruction (D-078)"
            elif "Outdoor" in d["quantity"]:
                why = "outdoor-air call-out re-anchored on the drawn value box or held as a query (D-078, D-083)"
            elif d["unit"] in by_cur_unit and not self.gate(self.unit_by_tag[d["unit"]])["ok"]:
                why = "unit-level value from a room list outside the gate (D-083): now a query under another key or not raised"
            else:
                why = "row is not produced by the rebuilt review (rule, link or gate changed it)"
            res.append({"draft1_id": d["change_id"], "new_id": "withdrawn", "reason": why, "sheet": d["sheet"], "item": d["item"],
                        "quantity": d["quantity"], "unit": d["unit"]})
        return res


class ChangeIdLedger:
    """out/schematics/change_id_ledger.csv.  An existing key keeps its ID; a new key takes the next free number of its
    sheet; a retired ID is not reused.  The pilot IDs of IP-BH-05010 that were shown are seeded from the draft-1 list."""
    COLS = ["change_id", "sheet", "entity", "quantity", "unit", "status", "first_build", "last_build", "retired_build"]

    def __init__(self, path: Path, draft1: Path, root: Path):
        self.path = path
        self.rows: Dict[Tuple[str, str, str, str], Dict[str, str]] = {}
        self.max: Counter = Counter()
        self.seen: set = set()
        if path.exists():
            for r in read_csv(path):
                self.rows[(r["sheet"], r["entity"], r["quantity"], r["unit"])] = r
        else:
            if not draft1.exists():
                src = root / "out" / "issue" / "SCHEMATICS_DRAFT" / "change_list.csv"
                if src.exists():
                    draft1.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, draft1)
            if draft1.exists():
                for d in read_csv(draft1):
                    if d["sheet"] == "IP-BH-05010" and d.get("change_id"):
                        k = (d["sheet"], d["item"], d["quantity"], d["unit"])
                        self.rows.setdefault(k, {"change_id": d["change_id"], "sheet": k[0], "entity": k[1], "quantity": k[2], "unit": k[3],
                                                 "status": "active", "first_build": "pilot IP-BH-05010", "last_build": "", "retired_build": ""})
        for r in self.rows.values():
            m = re.match(r"CH-([0-9A-Z]+)-(\d+)$", r["change_id"])
            if m:
                self.max[m.group(1)] = max(self.max[m.group(1)], int(m.group(2)))

    def take(self, k: Tuple[str, str, str, str]) -> str:
        r = self.rows.get(k)
        if r is None:
            m = re.search(r"IP-BH-([0-9A-Z]{5})", k[0])
            code = m.group(1) if m else ("APPD" if k[0] == "App D" else "NEW")
            self.max[code] += 1
            r = {"change_id": f"CH-{code}-{self.max[code]:04d}", "sheet": k[0], "entity": k[1], "quantity": k[2], "unit": k[3],
                 "status": "active", "first_build": BUILD_ID, "last_build": BUILD_ID, "retired_build": ""}
            self.rows[k] = r
        else:
            r["status"], r["retired_build"], r["last_build"] = "active", "", BUILD_ID
        self.seen.add(k)
        return r["change_id"]

    def retire_unseen(self) -> None:
        for k, r in self.rows.items():
            if k not in self.seen and r["status"] != "retired":
                r["status"], r["retired_build"] = "retired", BUILD_ID

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=self.COLS)
            w.writeheader()
            for r in sorted(self.rows.values(), key=lambda r: r["change_id"]):
                w.writerow({c: r.get(c, "") for c in self.COLS})


# --------------------------------------------------------------------------- #
# Run, CSV and workbook output
# --------------------------------------------------------------------------- #
def run_review(root: Path = ROOT, out: Path = OUT) -> Review:
    """Fixed order (build line 3): model (read), ledger, scope table, review, then the outputs."""
    inp = Inputs(root)
    rv = Review(inp)                      # builds the ledger and the scope table
    rv.review_all()
    rv.dedupe_boxes()
    rv.zoning_queries()
    rv.missing_global()
    rv.consistency()
    rv.appd_review()
    rv.mot_review()
    rv.drop_duplicates()
    rv.assign_dispositions()
    rv.assign_ids(out)
    return rv


CHANGE_COLS = ["change_id", "sheet", "page", "bbox", "layer_type", "drawn", "required", "action", "source",
               "unit", "item", "quantity", "unit_of_measure", "class", "note"] + EXTRA_COLS


def _plain(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 2)
    return v


def _row_vals(r: Row) -> List[Any]:
    return [r["change_id"], r["sheet"], r["page"], r["box"], r["layer"], _plain(r["drawn"]),
            _plain(r["required"]), r["action"], r["source"], r["unit"], r["item"], r["quantity"],
            r["uom"], r["cls"], r["note"]] + [_plain(r.get(c, "")) for c in EXTRA_COLS]


def write_csvs(rv: Review, out: Path) -> Tuple[int, int]:
    out.mkdir(parents=True, exist_ok=True)
    seen = set()
    n = 0
    with open(out / "change_register.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CHANGE_COLS)
        for r in rv.rows:
            if id(r) in seen:
                continue
            # a row of the change list has a change ID, or it is an increase of 5 L/s or less that is recorded and not marked up
            if not r["needs_id"] and r["cls"] != SUPPRESSED_CLASS:
                continue
            if r["cls"] == SUPPRESSED_CLASS and (rv.unit_list_only(r["unit"]) or not r["sheet"]):
                continue
            seen.add(id(r))
            w.writerow(_row_vals(r))
            n += 1
    m = 0
    seen2 = set()
    with open(out / "missing_systems.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["change_id", "system", "category", "what_is_needed", "required_value", "unit_of_measure",
                    "belongs_on_sheet", "page", "bbox", "source", "unit", "indicative_value", "cover", "rooms_counted", "rooms_zoned"])
        for r in rv.missing + getattr(rv, "untag_rows", []):
            if id(r) in seen2:
                continue
            seen2.add(id(r))
            w.writerow([r["change_id"], r["item"], r["quantity"], r["note"], _plain(r["required"]), r["uom"],
                        r["sheet"] or "NEW", r["page"], r["box"], r["source"], r["unit"], r.get("indicative", ""), r.get("cover", ""),
                        r.get("rooms_counted", ""), r.get("rooms_zoned", "")])
            m += 1
    write_conflicts(rv)
    write_status_files(rv, out)
    rv.led.write(out / "entity_ledger.csv")
    rv.write_scope(out / "scope.csv")
    with open(out / "change_id_map.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["draft1_id", "new_id", "reason", "sheet", "item", "quantity", "unit"])
        w.writeheader()
        for r in rv.id_map(out):
            w.writerow(r)
    return n, m


def is_instruction(r: Row) -> bool:
    return bool(r["change_id"]) and not r["action"].startswith("Query") and not r["layer"] == "queries"


def write_status_files(rv: Review, out: Path) -> None:
    """sheet_status.csv (one row for every working sheet, for the list of drawings) and review_counts.json (counts that the
    README of the issue folder states)."""
    uniq = list({id(r): r for r in rv.rows}.values())
    boxes = Counter(r["sheet"] for r in rv.led.rows if r["kind"] == "room box")
    nrows = Counter(r["sheet"] for r in uniq if r["row_type"] in ("Room", "Room airflow", "AHU total", "Fan"))
    inst = Counter(r["sheet"] for r in uniq if is_instruction(r) and not rv.unit_list_only(r["unit"]))
    qry = Counter(r["sheet"] for r in uniq if r["change_id"] and not is_instruction(r) and not rv.unit_list_only(r["unit"]))
    undisp = Counter(r["sheet"] for r in rv.led.rows if not r["disposition"])
    with open(out / "sheet_status.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["sheet", "status", "reason", "room_boxes", "ledger_rows", "rows", "instructions", "queries",
                    "boxes_not_reviewed", "list_only"])
        for sh in sorted(rv.sheet_scope):
            lo = rv.sheet_list_only(sh)
            nr = rv.not_reviewed_sheets.get(sh)
            part = rv.partial_not_reviewed.get(sh, 0)
            if nr:
                st, why = "not reviewed", nr
            elif undisp[sh]:
                st, why = "not reviewed", "ledger rows without a disposition"
            elif lo:
                st, why = "reviewed, listed, no mark-up (MOT)", "every AHU on the sheet is list-only"
            elif inst[sh] + qry[sh] > 0:
                st, why = "marked up", ("boxes not reviewed: unit not established" if part else "")
            else:
                st, why = "reviewed, no mark-up", ""
            w.writerow([sh, st, why, boxes[sh], sum(1 for r in rv.led.rows if r["sheet"] == sh), nrows[sh], inst[sh], qry[sh],
                        boxes[sh] if nr else part, int(lo)])
    ins = Counter(re.sub(r" \(change ID [^)]*\)", "", r["action"]) for r in uniq if is_instruction(r) and not rv.unit_list_only(r["unit"]))
    qs = Counter(re.sub(r" \(change ID [^)]*\)", "", r["action"]) for r in uniq if r["change_id"] and not is_instruction(r)
                 and not rv.unit_list_only(r["unit"]))
    gate_units = [u for u in rv.i.units if u["has_tag"] and u["type"] in ("AHU", "CRAC") and not u["list_only"] and u["systems"]]
    cnt = {
        "instructions_total": sum(ins.values()), "instructions_by_action": dict(ins.most_common()),
        "queries_total": sum(qs.values()), "queries_by_action": dict(qs.most_common()),
        "open_points_order": ["pressure direction conflicts", "zoning decisions", "other query groups"],
        "pressure_direction_conflicts": sum(1 for r in uniq if r["cls"] == "pressure direction conflict"),
        "zoning_decisions": len(rv.zoning_rows),
        "increases_5_Ls_or_less_not_marked_up": sum(1 for r in uniq if r["cls"] == SUPPRESSED_CLASS),
        "increases_passing_one_test_only": sum(1 for r in uniq if r["cls"] == "increase" and "passes the" in str(r["note"]) and r["change_id"]),
        "units_with_working_sheet": len(gate_units),
        "units_passing_the_room_list_gate": sum(1 for u in gate_units if rv.gate(u)["ok"]),
        "units_outside_the_gate": sum(1 for u in gate_units if not rv.gate(u)["ok"]),
        "list_only_units": sum(1 for u in rv.i.units if u["has_tag"] and u["list_only"]),
        "list_only_fans": sum(1 for r in rv.scope_rows if r["kind"] == "fan" and r["list_only"]),
        "list_only_sheets": sorted(sh for sh in rv.sheet_scope if rv.sheet_list_only(sh)),
        "sheets_not_reviewed": rv.not_reviewed_sheets, "sheets_with_boxes_not_reviewed": rv.partial_not_reviewed,
        "ledger_rows": len(rv.led.rows), "ledger_rows_without_disposition": sum(1 for r in rv.led.rows if not r["disposition"]),
        "fan_groups": len(rv.fan_groups), "fan_groups_duty_standby_pairs": sum(1 for g in rv.fan_groups if g["status"] == "pair"),
        "boxes_given_to_a_unit_by_name": getattr(rv, "box_assigned_by_name", 0),
        "change_ids": sum(1 for r in uniq if r["change_id"]),
        "assumptions": ["equal A/B pair = duty/standby (provisional)", "typical box = average of the linked rooms, range printed",
                        "outdoor-air fraction rule of the workbook (highest room fraction x supply)",
                        "increases of 5 L/s or less are not marked up",
                        "5 % or 10 L/s is read as either test",
                        "a unit-level or fan increase above 2 times the drawn value is a query"],
    }
    (out / "review_counts.json").write_text(json.dumps(cnt, indent=1, default=str))


def write_conflicts(rv: Review) -> int:
    """One row in conflicts.csv for each zoning conflict (D-085).  A row that is there already is not written again."""
    import fcntl
    path = rv.i.root / "out" / "registers" / "conflicts.csv"
    if not rv.zoning_conflicts or not path.exists():
        return 0
    n = 0
    with open(str(path) + ".lock", "a+") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        rows = read_csv(path)
        head = list(rows[0].keys()) if rows else ["conflict_id", "topic", "item", "value_a", "source_a", "value_b", "source_b",
                                                  "precedence_applied", "resolution", "status", "flag"]
        have = {r["topic"] + "|" + r["item"] for r in rows}
        nxt = max([int(re.sub(r"\D", "", r["conflict_id"]) or 0) for r in rows] + [0])
        new = []
        for c in rv.zoning_conflicts:
            topic = "Service area on the schedule against the approved zoning"
            if topic + "|" + c["unit"] in have:
                continue
            nxt += 1
            new.append({"conflict_id": f"CF-{nxt:04d}", "topic": topic, "item": c["unit"],
                        "value_a": f"serves {c['schedule']}", "source_a": f"Equipment schedule (Corr 5) p{c['page']}",
                        "value_b": "serves " + "; ".join(c["rooms"][:6]),
                        "source_b": f"Approved zoning, zone schedule row {c['unit_row']}; record {c['record']}",
                        "precedence_applied": "zoning governs (Gate 3b); nothing re-zoned",
                        "resolution": "query on the update list; mark-ups of the unit held", "status": "open", "flag": "VERIFY"})
        if new:
            raw = path.read_bytes()
            with open(path, "a", newline="", encoding="utf-8") as fh:
                if raw and not raw.endswith(b"\n"):
                    fh.write("\r\n" if b"\r\n" in raw[:2000] else "\n")
                w = csv.DictWriter(fh, fieldnames=head)
                for r in new:
                    w.writerow({k: r.get(k, "") for k in head})
            n = len(new)
        fcntl.flock(lk, fcntl.LOCK_UN)
    return n


def write_pressure_report(rv: Review, path: Path, out: Path) -> Dict[str, int]:
    """Fix report: the pressure rows of the issued set (draft 1), one by one (D-081)."""
    p1 = out / "issued_pressure_rows.csv"          # the 27 pressure rows of the issued set (frozen input)
    rows = []
    if p1.exists():
        rows = [d for d in read_csv(p1) if d["quantity"] == "Pressure target"]
    cur: Dict[Tuple[str, str, str], Row] = {}
    for r in {id(x): x for x in rv.rows}.values():
        if r["quantity"] == "Pressure target":
            cur[(r["sheet"], str(r["item"]), str(r["unit"]))] = r
    res = Counter()
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["draft1_id", "sheet", "room_box", "unit", "draft1_drawn_Pa", "draft1_required", "status_now", "class_now",
                    "new_id", "drawn_now", "workbook_set_Pa_now", "source_now"])
        for d in rows:
            c = cur.get((d["sheet"], d["item"], d["unit"]))
            if c is None:
                st, cl, nid, dn, rq, sc = "row not produced: the room is not reviewed with a pressure target now", "", "", "", "", ""
            elif c["cls"] == "no change":
                st, cl, nid, dn, rq, sc = "no difference", c["cls"], "", c["drawn"], c["required"], c["source"]
            elif c["cls"] == "pressure direction conflict":
                st, cl, nid, dn, rq, sc = "direction conflict", c["cls"], c["change_id"], c["drawn"], c["required"], c["source"]
            else:
                st, cl, nid, dn, rq, sc = "still a query", c["cls"], c["change_id"], c["drawn"], c["required"], c["source"]
            res[st.split(":")[0]] += 1
            w.writerow([d["change_id"], d["sheet"], d["item"], d["unit"], d["drawn"], d["required"], st, cl, nid, _plain(dn), _plain(rq), sc])
    return dict(res)


def run_checks(rv: Review) -> Dict[str, Any]:
    """Checks of the review itself (brief 9.5): rooms of the zoning in the rows, one result for each box,
    no change ID on a pointer or list-only row, every change ID unique."""
    bad_rooms = [t for t, i in rv.unit_info.items() if i.get("zone_keys") and i["zone_keys"] - i["classed_keys"]]
    acts: Dict[Tuple[str, ...], set] = defaultdict(set)
    for r in rv.rows:
        if r["needs_id"] and r["row_type"] in ("Room", "Room airflow", "Fan") and r["box"] and r["quantity"]:
            acts[(r["sheet"], r["box"], r["item"], r["quantity"])].add(r["action"])
    conflicts = [k for k, v in acts.items() if len(v) > 1]
    ids = [r["change_id"] for r in {id(r): r for r in rv.rows if r["needs_id"]}.values()]
    mot_marked = [t for t, i in rv.unit_info.items() if i.get("list_only") and any(r["needs_id"] for r in i["rows"])]
    one_test = sum(1 for r in {id(r): r for r in rv.rows}.values()
                   if r["cls"] == "increase" and "passes the" in str(r["note"]) and r["needs_id"])
    return {"units_reviewed": len(rv.unit_info),
            "units_with_zone_rooms_not_in_rows": bad_rooms,
            "box_keys_with_conflicting_actions": len(conflicts),
            "change_ids": len(ids), "change_ids_unique": len(ids) == len(set(ids)),
            "mot_theatre_units_with_markup": mot_marked,
            "pointer_rows_without_own_id": sum(1 for r in rv.rows if r["cls"] == "reviewed on another tab"),
            "increase_rows_passing_one_test_only": one_test,
            "ledger_rows": len(rv.led.rows),
            "ledger_rows_without_disposition": sum(1 for r in rv.led.rows if not r["disposition"]),
            "ledger_dispositions": dict(Counter(re.sub(r" unit .*$", " unit X", r["disposition"])[:60] for r in rv.led.rows).most_common(12)),
            "suppressed_increases": sum(1 for r in {id(x): x for x in rv.rows}.values() if r["cls"] == SUPPRESSED_CLASS),
            "change_ids_on_list_only_units": [r["change_id"] for r in {id(x): x for x in rv.rows}.values()
                                              if r["change_id"] and (rv.unit_list_only(r["unit"]) or rv.drawn_only_list_only(r["unit"]))],
            "overall_schematics": getattr(rv, "overall_info", {})}


def counts(rv: Review) -> Dict[str, Any]:
    acts: Counter = Counter()
    per_sheet: Dict[str, Counter] = defaultdict(Counter)
    layers: Counter = Counter()
    seen = set()
    for r in rv.rows:
        if id(r) in seen:
            continue
        seen.add(id(r))
        acts[re.sub(r"see the tab of .*$", "see the tab of another unit", re.sub(r" \(change ID [^)]*\)", "", r["action"]))] += 1
        if r["needs_id"]:
            per_sheet[r["sheet"] or "NEW"][r["layer"]] += 1
            layers[r["layer"]] += 1
    return {"per_action": acts, "per_sheet": per_sheet, "per_layer": layers}


def _short_tag(t: str, code: bool = True) -> str:
    t = re.sub(r"^(?:P?AHU|FCU|CRAC)-B-(?:B\d|\d\d|RF)-", "", t.strip())
    if not code:
        t = re.sub(r"^(?:\d\d|B\d)(?=[A-Z])", "", t)      # served-level code
    t = re.sub(r"(?<=\d)[A-Z](?:/[A-Z])*$", "", t)          # duty/standby letters
    return t


def safe_sheet_name(tag: str, used: set) -> str:
    """Tab name of at most 31 characters, cut at a member boundary: a group is 'G first + second'."""
    if tag.startswith("AHU group:"):
        raw = [x for x in tag[len("AHU group:"):].split(";") if x.strip()]
        parts = [_short_tag(x) for x in raw]
        if len("G " + " + ".join(parts)) > 31:
            parts = [_short_tag(x, code=False) for x in raw]
        t = "G " + " + ".join(parts)
        while len(t) > 31 and len(parts) > 1:
            parts = parts[:-1]
            t = "G " + " + ".join(parts) + " +"
    else:
        t = re.sub(r"[\\/*?:\[\]]", "_", tag)
    t = re.sub(r"[\\/*?:\[\]]", "_", t)
    if len(t) > 31:
        t = t[:31].rstrip(" +;-")
    base, n = t, 1
    while t.lower() in used:
        n += 1
        suf = f"~{n}"
        t = base[:31 - len(suf)] + suf
    used.add(t.lower())
    return t


def _name_map():
    try:
        from . import drawings_common as dc
        return dc.NameMap()
    except Exception:                                  # the name map is the work of another file: print as written
        class _Id:
            missed: Dict[str, int] = {}

            def show(self, n: str, d: str = "") -> str:
                return n
        return _Id()


def write_xlsx(rv: Review, path: Path) -> Dict[str, str]:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.properties import PageSetupProperties

    proj = "AIGH redevelopment, Contract 2, Inpatient building, ACMV"
    today = date.today().isoformat()
    F = lambda **k: Font(name="Arial", size=k.pop("size", 10), **k)  # noqa: E731
    MONO = Font(name="Consolas", size=10)
    hair = Side(style="hair", color="808080")
    HEAD_B = Border(bottom=hair)
    band = PatternFill("solid", fgColor="F5F5F5")
    numfmt = '#,##0;−#,##0;"–"'
    numfmt1 = '#,##0.0;−#,##0.0;"–"'
    pctfmt = '0.0%;−0.0%;"–"'
    wb = Workbook()
    wb.remove(wb.active)

    def setup(ws, ncols: int, last_row: int, header_row: Optional[int]) -> None:
        ws.sheet_view.showGridLines = False
        ws.page_setup.orientation = "landscape"
        ws.page_setup.paperSize = ws.PAPERSIZE_A3
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
        if header_row:
            ws.print_title_rows = f"{header_row}:{header_row}"
        ws.print_area = f"A1:{get_column_letter(ncols)}{max(last_row, 2)}"
        ws.oddFooter.left.text = proj
        ws.oddFooter.center.text = "&A"
        ws.oddFooter.right.text = f"Rev {REVISION}  {today}  Page &P of &N"
        for part in (ws.oddFooter.left, ws.oddFooter.center, ws.oddFooter.right):
            part.size = 8
            part.font = "Arial"
        ws.page_margins.left = ws.page_margins.right = 0.4

    def title(ws, text: str, sub: List[str]) -> int:
        ws["A1"] = text
        ws["A1"].font = F(size=18, bold=True)
        r = 2
        for s in sub:
            ws.cell(r, 1, s).font = F(size=8, italic=True, color="595959")
            r += 1
        return r + 1

    def table(ws, r0: int, headers: List[str], data: List[List[Any]], widths: List[float],
              mono: Sequence[int] = (), fmts: Optional[Dict[int, str]] = None, wrap: Sequence[int] = (),
              freeze_cols: int = 0, int_uom: Optional[int] = None) -> int:
        fmts = fmts or {}
        for c, h in enumerate(headers, 1):
            cell = ws.cell(r0, c, h)
            cell.font = F(bold=True)
            cell.border = HEAD_B
            cell.alignment = Alignment(wrap_text=True, vertical="bottom",
                                       horizontal="right" if (c - 1) in fmts else "left")
            ws.column_dimensions[get_column_letter(c)].width = widths[c - 1]
        for n, row in enumerate(data):
            rr = r0 + 1 + n
            for c, v in enumerate(row, 1):
                cell = ws.cell(rr, c, v)
                cell.font = MONO if (c - 1) in mono else F()
                if (c - 1) in fmts and isinstance(v, (int, float)):
                    cell.number_format = fmts[c - 1]
                    if int_uom is not None and row[int_uom] in ("no.", "pt", "rooms") and fmts[c - 1] == numfmt1:
                        cell.number_format = numfmt
                    cell.alignment = Alignment(horizontal="right", vertical="top")
                else:
                    cell.alignment = Alignment(wrap_text=(c - 1) in wrap, vertical="top",
                                               horizontal="left")
                if n % 2 == 1:
                    cell.fill = band
        if freeze_cols is not None:
            ws.freeze_panes = ws.cell(r0 + 1, freeze_cols + 1)
        return r0 + len(data)

    NM = _name_map()

    def room_rows(rows: List[Row]) -> List[List[Any]]:
        out = []
        for r in rows:
            item = NM.show(r["item"]) if r["row_type"] in ("Room", "Room airflow") else r["item"]
            out.append([r["layer"], r["row_type"], item, r["quantity"], r["uom"], r["drawn"], r["required"],
                        r["diff"], r["diff_pct"], r["cls"], r["action"], r["change_id"], r["sheet"], r["page"],
                        r["box"], r["source"], r["note"]])
        return out

    UNIT_HEAD = ["Layer", "Row type", "Item", "Quantity", "Unit of value", "Drawn value (unit in column E)",
                 "Required value (unit in column E)", "Difference, required − drawn", "Difference (%)", "Class",
                 "Action", "Change ID", "Drawing sheet", "Page", "Box on the page, x0 y0 x1 y1 (pt)", "Source", "Note"]
    UNIT_W = [11, 14, 34, 24, 8, 14, 14, 14, 10, 24, 40, 16, 13, 6, 34, 50, 60]
    UNIT_FMT = {5: numfmt1, 6: numfmt1, 7: numfmt1, 8: pctfmt, 13: "0"}
    UNIT_MONO = (2, 11, 12, 14)

    # -------- unit sheets
    used = {"summary", "app d reconciliation", "mot differences", "missing systems", "consistency", "fcu no tag", "other queries"}
    sheet_of_unit: Dict[str, str] = {}
    unit_sheets = []
    for u in rv.i.units:
        if not u["has_tag"]:
            continue
        nm = safe_sheet_name(u["tag"], used)
        sheet_of_unit[u["tag"]] = nm
        unit_sheets.append((u, nm))

    # -------- summary (built last, placed first)
    cnt = counts(rv)

    # App D sheet
    ws = wb.create_sheet("App D reconciliation")
    r0 = title(ws, "Appendix D reconciliation", [
        "Layout tags against the equipment schedule, spaces against equipment, and scheduled duties against the common-areas workbook.",
        "Scheduled duty is in L/s. Rows for spaces give the area (m²); rows for distance give points. The row type says which."])
    hdr = ["Check", "Item", "Storey or level", "Unit of value", "Scheduled value (unit in column D)",
           "Workbook D00 value (unit in column D)", "Difference, workbook − schedule", "Result", "Action",
           "Change ID", "Sheet", "Page", "Box on the page (pt)", "Source", "Note"]
    data = [[r["row_type"], r["item"], r["quantity"], r["uom"], r["drawn"], r["required"], r["required"] - r["drawn"]
             if isinstance(r["required"], (int, float)) and isinstance(r["drawn"], (int, float)) else None,
             r["cls"], r["action"], r["change_id"], r["sheet"], r["page"], r["box"], r["source"], r["note"]]
            for r in rv.appd_rows]
    last = table(ws, r0, hdr, data, [30, 34, 14, 8, 14, 14, 14, 18, 40, 16, 8, 6, 26, 50, 60],
                 mono=(1, 9, 12), fmts={4: numfmt1, 5: numfmt1, 6: numfmt1}, wrap=(14,), freeze_cols=2)
    setup(ws, len(hdr), last, r0)

    # MOT sheet
    ws = wb.create_sheet("MOT differences")
    r0 = title(ws, "MOT air-side sheets against the MOT schedules", [
        "Sheets of the MOT NSC are reviewed and listed. They are not marked up. Operating theatres keep their specified values.",
        "The schedule value is from the latest corrigendum that lists the tag."])
    hdr = ["Drawing sheet", "Tag", "Drawn duty (L/s)", "Scheduled duty (L/s)", "Difference, schedule − drawn (L/s)",
           "Result", "Schedule issue", "Page", "Box on the page (pt)", "Source", "Note"]
    data = [[m["sheet"], m["tag"], m["drawn"], m["scheduled"], m["diff"], m["result"], m["issue"], m["page"], m["box"],
             m["source"], m["note"]] for m in rv.mot_rows]
    last = table(ws, r0, hdr, data, [14, 30, 12, 12, 14, 34, 12, 6, 28, 50, 60], mono=(0, 1, 8),
                 fmts={2: numfmt, 3: numfmt, 4: numfmt}, wrap=(5, 10), freeze_cols=2)
    setup(ws, len(hdr), last, r0)

    # Missing systems
    ws = wb.create_sheet("Missing systems")
    r0 = title(ws, "Missing systems", [
        "Systems that the zone schedule, the criteria or the workbooks need, and that no working sheet shows.",
        "Where a sheet is not known the belongs-on column says NEW."])
    hdr = ["Change ID", "System", "Category", "Required value (L/s or no.)", "Belongs on sheet", "What is needed", "Source"]
    mrows = []
    seen3 = set()
    for r in rv.missing + getattr(rv, "untag_rows", []):
        if id(r) in seen3:
            continue
        seen3.add(id(r))
        mrows.append([r["change_id"], r["item"], r["quantity"], _plain(r["required"]) if not isinstance(r["required"], str) else None,
                      r["sheet"] or "NEW", r["note"], r["source"]])
    last = table(ws, r0, hdr, mrows, [16, 40, 40, 14, 14, 80, 50], mono=(0, 1, 4), fmts={3: numfmt1}, wrap=(1, 2, 5, 6),
                 freeze_cols=2)
    setup(ws, len(hdr), last, r0)

    # Consistency
    ws = wb.create_sheet("Consistency")
    r0 = title(ws, "Consistency across sheets", [
        "A tag has one duty wherever it is drawn. An outdoor-air unit gives the sum of what its AHUs take. A room is on one AHU."])
    last = table(ws, r0, UNIT_HEAD, room_rows(rv.consist), UNIT_W, mono=UNIT_MONO, fmts=UNIT_FMT, wrap=(2, 9, 10, 14, 15, 16),
                 freeze_cols=3, int_uom=4)
    setup(ws, len(UNIT_HEAD), last, r0)

    # Other queries: rows that sit on no unit tab (zoning queries, fans whose unit is not read)
    ws = wb.create_sheet("Other queries")
    r0 = title(ws, "Queries that belong to no unit tab", [
        "Units drawn on a sheet and absent from the zone schedule, sheets whose unit is not established, and fans whose link to a unit is not read.",
        "The drawn value stays in every case. Nothing is added or removed."])
    sheet_tags = set(sheet_of_unit)
    on_tabs = {id(x) for x in rv.missing} | {id(x) for x in rv.consist} | {id(x) for x in rv.appd_rows} | \
        {id(x) for t in rv.unit_info.values() for x in t.get("rows", [])}
    other = [x for x in {id(x): x for x in rv.rows}.values() if x["needs_id"] and id(x) not in on_tabs]
    other += [x for x in rv.zoning_rows if x not in other]
    last = table(ws, r0, UNIT_HEAD, room_rows(other), UNIT_W, mono=UNIT_MONO, fmts=UNIT_FMT, wrap=(2, 9, 10, 14, 15, 16), freeze_cols=3, int_uom=4)
    setup(ws, len(UNIT_HEAD), last, r0)

    # FCU no tag
    ws = wb.create_sheet("FCU no tag")
    r0 = title(ws, "Units with no tag in the documents", [
        "The zone schedule holds rooms on these units. No document gives them a tag. Give each unit a tag, then draw it."])
    hdr = ["Unit as listed", "Class", "Storeys", "Rooms (no.)", "Area (m²)", "Action", "Change ID", "What is needed", "Source"]
    data = [[r["item"], r["quantity"], (rv.unit_by_tag[r["unit"]]["storeys"] if r["unit"] in rv.unit_by_tag else ""),
             rv.unit_by_tag[r["unit"]]["rooms"], rv.unit_by_tag[r["unit"]]["area"], r["action"], r["change_id"],
             r["note"], r["source"]] for r in getattr(rv, "untag_rows", [])]
    last = table(ws, r0, hdr, data, [44, 30, 14, 10, 10, 40, 16, 80, 40], mono=(0, 6), fmts={3: numfmt, 4: numfmt1},
                 wrap=(1, 7), freeze_cols=1)
    setup(ws, len(hdr), last, r0)

    # per unit sheets
    for u, nm in unit_sheets:
        info = rv.unit_info.get(u["tag"], {})
        ws = wb.create_sheet(nm)
        sheets = ", ".join(info.get("sheets", [])) or "none"
        tv = []
        for sh in info.get("sheets", []):
            t = rv.i.tender_summary.get(sh)
            if not t:
                tv.append(f"{sh}: no tender issue of this sheet is in the read set")
            elif not t.get("tender_issue"):
                tv.append(f"{sh}: new in the working set; no tender issue of this sheet exists, so every room counts as added "
                          f"({t.get('rooms_added') or 0} rooms)")
            else:
                parts = [f"rooms added {t.get('rooms_added') or 0}", f"rooms removed {t.get('rooms_removed') or 0}",
                         f"rooms with airflow changed {t.get('rooms_airflow_changed') or 0}",
                         f"connection changes {t.get('connection_changes') or 0}"]
                tv.append(f"{sh} against {t['tender_issue']}: " + ", ".join(parts))
        rl = u["room_list"].strip()
        if rl and not rl.endswith("."):
            rl += "."
        sub = [f"Type {u['type']}. Storeys {u['storeys']}. Plant level {u['plant']}. Rooms in the zone list: {u['rooms']} ({u['area']} m²).",
               f"Drawn on working sheet: {sheets}. Room list of the unit: {rl}",
               ("Working sheet against tender issue: " + "; ".join(tv) + ".") if tv else "Working sheet against tender issue: not applicable; no working sheet shows this unit.",
               "Update rule: an increase of more than 5 % or 10 L/s is marked up, where the room list of the unit is established "
               "(complete, and the counted area is 0.90 to 1.10 of the zoned area). A reduction of the supply is marked up only on the same basis. "
               "Where the room list is not established, the row is a query and the drawn value stays."]
        if u["list_only"]:
            sub.append("MOT NSC unit: the specified values are kept. The rows are listed and are not marked up.")
        r0 = title(ws, u["tag"], sub)
        last = table(ws, r0, UNIT_HEAD, room_rows(info.get("rows", [])), UNIT_W, mono=UNIT_MONO, fmts=UNIT_FMT,
                     wrap=(2, 9, 10, 14, 15, 16), freeze_cols=3, int_uom=4)
        setup(ws, len(UNIT_HEAD), last, r0)
        ws.sheet_properties.tabColor = None

    # Summary
    ws = wb.create_sheet("Summary", 0)
    r = title(ws, "Schematic update list", [
        proj,
        f"Revision {REVISION}. {today}. Status: draft for checking.",
        "Review of every AHU, PAHU and FCU group of the zone schedule against the working drawings."])
    def section(txt):
        nonlocal r
        ws.cell(r, 1, txt).font = F(size=12, bold=True)
        r += 1
    # the two decision lists come first (build lines 10 and 21)
    pr_conf = [x for x in {id(x): x for x in rv.rows}.values() if x["cls"] == "pressure direction conflict"]
    section("Pressure direction conflicts: the drawn regime and the workbook have opposite signs (" + str(len(pr_conf)) + ")")
    hdr = ["Unit", "Room box", "Drawn (Pa)", "Workbook set pressure (Pa)", "Change ID", "Drawing sheet", "Source"]
    data = [[x["unit"], NM.show(x["item"]), x["drawn"], x["required"], x["change_id"], x["sheet"], x["source"]] for x in
            sorted(pr_conf, key=lambda x: (x["sheet"], x["unit"], x["item"]))]
    r1 = table(ws, r, hdr, data, [60, 40, 14, 18, 16, 14, 70], mono=(0, 4, 5), fmts={2: numfmt1, 3: numfmt1}, freeze_cols=None)
    r = r1 + 2
    section("Decisions on zoning needed before drafting (" + str(len(rv.zoning_rows)) + ")")
    hdr = ["Unit or sheet", "Reading", "Drawn", "Change ID", "Drawing sheet", "Note"]
    data = [[x["item"], x["quantity"], x["drawn"], x["change_id"], x["sheet"], x["note"]] for x in rv.zoning_rows]
    r1 = table(ws, r, hdr, data, [60, 40, 14, 16, 14, 100], mono=(0, 3, 4), fmts={}, wrap=(1, 5), freeze_cols=None)
    r = r1 + 2
    section("How to read this book")
    for t in ["One tab for each unit. Each row gives the drawn value, the required value, the difference, the action and the change ID.",
              "A change ID links the row to the mark-up on the drawing and to the change list.",
              "Required values come from the department workbooks. Rooms come from the zone schedule.",
              "A unit-level value is an instruction only where the unit is in the zone schedule, its room list is complete and the counted area "
              "is 0.90 to 1.10 of the zoned area. Otherwise the row is a query, the drawn value stays and the calculated value is marked indicative.",
              "Where the zone link of a room is not known, the row says not established. It does not say add or remove.",
              "A room increase is an instruction where the box links to the workbook room by number, or one-to-one by name on the same unit. "
              "A link by name only with a ratio above 2 or below 0.5, or across departments, is a query.",
              "Every pressure row is a query to the workbook. The drawn pressure stays. No cloud is put on a drawn pressure. "
              "Offset air is in the note only.",
              "Fan pressure cannot be taken from a schematic. The drawn value is kept.",
              "A fan belongs to the unit whose name is in its tag. Two fans of a pair with the same drawn duty are read as duty and standby (to confirm). "
              "The pair counts once.",
              "Return fan, spill or relief path with nothing drawn is a query. It is not an instruction.",
              "VAV boxes and venturi valves: the VAV and Venturi Valves tabs of the workbooks have no rows. The sheets draw a VAV on the supply and return branches of many rooms, "
              "and these cannot be compared with the workbooks. They are not compared.",
              "Required airflow of a room box is the average of the workbook rooms linked to the box. Where the linked rooms differ by more than 25 % "
              "and the answer depends on which room is used, the row is a query. The average is to be checked by a person.",
              "An increase is marked up when it is more than 5 % of the drawn value or more than 10 L/s, and more than 5 L/s. A row that passes only one of the two tests says so "
              "in its note. An increase of 5 L/s or less is recorded and not marked up. The reading of the update rule is to be confirmed.",
              "A drawn box is reviewed once. Other tabs that show the same box carry a pointer row and no change ID of their own.",
              "List-only units and fans (the MOT NSC units) are listed on the MOT differences tab. They have no change ID and no mark-up."]:
        ws.cell(r, 1, t).font = F()
        r += 1
    r += 1
    section("Counts per action")
    rowsA = sorted(cnt["per_action"].items(), key=lambda kv: -kv[1])
    r1 = table(ws, r, ["Action", "Count (no.)"], [[a, n] for a, n in rowsA], [60, 14], fmts={1: numfmt}, freeze_cols=None)
    r = r1 + 2
    section("Counts of change IDs per layer")
    r1 = table(ws, r, ["Layer", "Change IDs (no.)"], [[l, cnt["per_layer"].get(l, 0)] for l in LAYERS], [60, 14], fmts={1: numfmt}, freeze_cols=None)
    r = r1 + 2
    section("Counts of change IDs per drawing sheet")
    hdr = ["Drawing sheet", "rooms (no.)", "airflows (no.)", "fans (no.)", "new systems (no.)", "queries (no.)", "Total (no.)"]
    data = []
    for sh, c in sorted(cnt["per_sheet"].items()):
        data.append([sh] + [c.get(l, 0) for l in LAYERS] + [sum(c.values())])
    r1 = table(ws, r, hdr, data, [60, 14, 14, 14, 16, 14, 14], mono=(0,), fmts={i: numfmt for i in range(1, 7)}, freeze_cols=None)
    r = r1 + 2
    chk = run_checks(rv)
    section("Checks of the review")
    r1 = table(ws, r, ["Check", "Result"], [
        ["Units reviewed (no.)", chk["units_reviewed"]],
        ["Units with a zone room that has no row (no.)", len(chk["units_with_zone_rooms_not_in_rows"])],
        ["Drawn boxes with conflicting actions on several tabs (no.)", chk["box_keys_with_conflicting_actions"]],
        ["Change IDs (no.)", chk["change_ids"]],
        ["Pointer rows: box reviewed on another tab, no own change ID (no.)", chk["pointer_rows_without_own_id"]],
        ["MOT theatre units with a mark-up (no.)", len(chk["mot_theatre_units_with_markup"])],
        ["Increase rows that pass one of the two tests only (no.); reading of the update rule to confirm",
         chk["increase_rows_passing_one_test_only"]],
        ["Drawn entities listed (no.)", chk["ledger_rows"]],
        ["Drawn entities without a disposition (no.)", chk["ledger_rows_without_disposition"]],
        ["Increases of 5 L/s or less, recorded and not marked up (no.)", chk["suppressed_increases"]]], [60, 14], fmts={1: numfmt}, freeze_cols=None)
    r = r1 + 2
    section("Units")
    hdr = ["Unit", "Type", "Storeys", "Drawn on sheet", "Rooms in zone list (no.)", "Supply drawn (L/s)", "Supply required (L/s)",
           "Drawn duty covers the requirement", "Change IDs (no.)", "Tab"]
    data = []
    for u, nm in unit_sheets:
        info = rv.unit_info.get(u["tag"], {})
        d, q = (info.get("drawn") or {}).get("supply"), (info.get("req") or {}).get("supply")
        cov = "not established" if d is None or q is None else ("yes" if d >= q else "no")
        nid = sum(1 for x in info.get("rows", []) if x["needs_id"])
        data.append([u["tag"], u["type"], u["storeys"], ", ".join(info.get("sheets", [])) or "none", u["rooms"], d, q, cov, nid, nm])
    r1 = table(ws, r, hdr, data, [60, 14, 14, 24, 14, 14, 14, 18, 14, 22], mono=(0, 3, 9), fmts={4: numfmt, 5: numfmt, 6: numfmt, 8: numfmt},
               freeze_cols=None)
    for n, (u, nm) in enumerate(unit_sheets):
        c = ws.cell(r + 1 + n, 10)
        c.hyperlink = f"#'{nm}'!A1"
        c.font = Font(name="Consolas", size=10, color="1F4E79", underline="single")
    r = r1 + 2
    ws.freeze_panes = "A5"
    setup(ws, 10, r, None)
    ws.column_dimensions["A"].width = 60
    wb.save(path)
    return sheet_of_unit


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.schematics.review")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("run", help="run the review and write the outputs")
    rp.add_argument("--no-xlsx", action="store_true")
    a = ap.parse_args(argv)
    rv = run_review(ROOT, OUT)
    OUT.mkdir(parents=True, exist_ok=True)
    n, m = write_csvs(rv, OUT)
    if not a.no_xlsx:
        write_xlsx(rv, OUT / "schematic_update_register.xlsx")
    pres = write_pressure_report(rv, ROOT / "out" / "qa" / "pressure_rows_report.csv", OUT)
    print("pressure rows of draft 1:", pres)
    c = counts(rv)
    chk = run_checks(rv)
    (OUT / "review_check.json").write_text(json.dumps(chk, indent=1))
    print(json.dumps(chk, indent=1))
    print(f"change IDs: {n}; missing systems: {m}")
    print("per action:")
    for k, v in c["per_action"].most_common():
        print(f"  {v:6d}  {k}")
    print("per layer:", dict(c["per_layer"]))
    print("sheets with changes:", len(c["per_sheet"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
