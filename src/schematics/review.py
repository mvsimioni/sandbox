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
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import re
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
    r"(?:PAHU|AHU|FCU|CRAC|OAF|EAF|TEAF|RAF|TEF|HPF)-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?:/[A-Z0-9]+)*")


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
        pair = re.search(r"^(.*-)(\d+)/(\d+)[A-Z]?$", rest)
        if pair:
            for n in (pair.group(2), pair.group(3)):
                out.add(f"{pre}:{pair.group(1)}{n}")
            continue
        rest = re.sub(r"(?<=\d)[A-Z](?:/[A-Z])*$", "", rest)
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
    """Class, action and 'needs change ID' for one airflow value (update rule)."""
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
            return "no change", "No mark-up: increase of 5 L/s or less; drawn value kept", False
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
# Row builder
# --------------------------------------------------------------------------- #
class Row(dict):
    """One review row.  Keys are the columns of the change and unit sheets."""


def mk(unit: str, layer: str, rtype: str, item: str, qty: str, uom: str,
       drawn: Any, req: Any, cls: str, action: str, needs_id: bool,
       sheet: str = "", page: Any = "", box: Any = None, source: str = "",
       note: str = "") -> Row:
    d = num(drawn) if not isinstance(drawn, str) else None
    r = num(req) if not isinstance(req, str) else None
    diff = (r - d) if (d is not None and r is not None) else None
    pct = (diff / d) if (diff is not None and d) else None
    return Row(unit=unit, layer=layer, row_type=rtype, item=item, quantity=qty, uom=uom,
               drawn=drawn, required=req, diff=diff, diff_pct=pct, cls=cls, action=action,
               needs_id=needs_id, change_id="", sheet=sheet, page=page, box=box_s(box) if box else "",
               source=clean(source), note=clean(note))


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
            out[str(r[0])] = d
        return out

    # -- department workbooks ------------------------------------------------#
    def _load_workbooks(self) -> Dict[str, Dict[str, Any]]:
        import openpyxl
        res: Dict[str, Dict[str, Any]] = {}
        for p in sorted((self.root / "out/loads/workbooks").glob("AIGH_C2_D*.xlsx")):
            m = re.search(r"_D(\d\d)_", p.name)
            if not m:
                continue
            dept = m.group(1)
            wb = openpyxl.load_workbook(p, data_only=True, read_only=True)
            d: Dict[str, Any] = {"file": p.name, "airflow": {}, "ahu": [], "fans": [], "fcu": [], "pressure": {}}
            # Airflow: one row per room
            rows = list(wb["Airflow"].iter_rows(values_only=True))
            head = {h: i for i, h in enumerate(rows[4]) if h}
            for n, r in enumerate(rows[6:], start=7):
                if r[0] is None:
                    continue
                g = lambda k: r[head[k]] if k in head else None  # noqa: E731
                d["airflow"][str(r[0])] = {
                    "row": n, "name": g("Room name"), "storey": g("Storey"), "ahu": g("AHU or system"),
                    "supply": num(g("Final supply")), "extract": num(g("Total extract")),
                    "exhaust": num(g("Exhaust")), "return": num(g("Return")), "oa": num(g("Outdoor air")),
                    "offset": num(g("Offset needed")), "offset_kind": g("Pressure offset kind"),
                    "exh_fan": g("Exhaust fan tag"), "terminal": g("Terminal tag"),
                    "clinical": g("Clinical criteria"),
                    "cols": {k: cell_addr(head[k], n) for k in ("Final supply", "Return", "Exhaust",
                                                                "Total extract", "Offset needed") if k in head},
                }
            # AHU table A
            rows = list(wb["AHU"].iter_rows(values_only=True))
            head = {h: i for i, h in enumerate(rows[4]) if h}
            for n, r in enumerate(rows[6:], start=7):
                if r[0] is None:
                    continue
                if isinstance(r[0], str) and r[0].startswith("B."):
                    break
                g = lambda k: num(r[head[k]]) if k in head else None  # noqa: E731
                d["ahu"].append({
                    "tag": str(r[0]), "row": n, "rooms": g("Rooms on this unit"),
                    "supply": g("AHU supply airflow"), "oa": g("AHU outdoor air = larger of fraction × supply and the air that leaves the rooms"),
                    "recirc": g("Recirculated air = AHU supply − outdoor air"), "exhaust": g("Exhaust"),
                    "spill": g("Spill or relief = outdoor air − exhaust − net envelope leakage"),
                    "coil": g("Coil duty (outdoor-air rule)"), "leak": g("Duct leakage"),
                    "cols": {"supply": cell_addr(head["AHU supply airflow"], n),
                             "spill": cell_addr(head["Spill or relief = outdoor air − exhaust − net envelope leakage"], n),
                             "coil": cell_addr(head["Coil duty (outdoor-air rule)"], n)},
                })
            # Fans
            rows = list(wb["Fans"].iter_rows(values_only=True))
            head = {h: i for i, h in enumerate(rows[4]) if h}
            for n, r in enumerate(rows[6:], start=7):
                if not r[0]:
                    continue
                d["fans"].append({
                    "tag": str(r[0]), "type": r[1], "serves": r[2], "also": r[3], "row": n,
                    "dept": dept,
                    "required": num(r[head["Required airflow"]]),
                    "calc": num(r[head["Calculated airflow, drawn duty"]]),
                    "tender": num(r[head["Tender airflow"]]), "status": r[head["Status"]],
                    "cell": cell_addr(head["Calculated airflow, drawn duty"], n),
                })
            # FCU tab (rooms separated by the outdoor-air rule and FCU rooms)
            rows = list(wb["FCU"].iter_rows(values_only=True))
            head = {h: i for i, h in enumerate(rows[4]) if h}
            for n, r in enumerate(rows[6:], start=7):
                if r[0] is None or r[1] is None:
                    continue
                d["fcu"].append({
                    "room": str(r[head["Room number"]]), "name": r[head["Room name"]],
                    "ahu": r[head["Original AHU or system"]], "reason": r[head["Reason"]],
                    "supply": num(r[head["Room supply"]]), "oa": num(r[head["Outdoor air"]]),
                    "oa_src": r[head["Outdoor-air source (PAHU or fan)"]], "row": n,
                    "area": num(r[head["Area"]]),
                })
            # Pressure
            rows = list(wb["Pressure"].iter_rows(values_only=True))
            head = {h: i for i, h in enumerate(rows[4]) if h}
            for n, r in enumerate(rows[6:], start=7):
                if r[0] is None:
                    continue
                # room table only: a node has a Type; the Suite totals and Known answers tables below have none
                if str(r[head["Type"]] or "") not in ("ROOM", "SPACE", "EXTERNAL") or r[head["Type"]] is None:
                    if d["pressure"]:
                        break
                    continue
                if str(r[head["Type"]]) == "EXTERNAL":
                    continue
                d["pressure"][str(r[0])] = {"set": num(r[head["Set pressure"]]), "row": n,
                                            "offset": num(r[head["Offset needed"]]) if "Offset needed" in head else None}
            res[dept] = d
        return res

    # -- equipment schedule -----------------------------------------------------#
    def _load_schedule(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for r in read_csv(self.reg / "ahu_schedule.csv"):
            t = r.get("tag_normalised") or r.get("tag_as_written")
            for k in tag_keys(t):
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
    m = re.search(r"([+\-−]?)\s*(\d+(?:\.\d+)?)", t)
    if not m:
        return None
    v = float(m.group(2))
    return -v if m.group(1) in ("-", "−") else v


def short_wb(dept: str, wb: Dict[str, Any]) -> str:
    return f"Workbook D{dept}"


# --------------------------------------------------------------------------- #
# The review
# --------------------------------------------------------------------------- #
class Review:
    def __init__(self, inp: Inputs):
        self.i = inp
        self.rows: List[Row] = []
        self.unit_info: Dict[str, Dict[str, Any]] = {}
        self.missing: List[Row] = []
        self.consist: List[Row] = []
        self.appd_rows: List[Row] = []
        self.mot_rows: List[Dict[str, Any]] = []
        self.untagged: List[Dict[str, Any]] = []
        self.one_test_rows = 0
        self._prep()

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
        # MOT theatre units: keys on a MOT NSC sheet or schedule and a theatre department (D-057)
        for u in i.units:
            u["mot"] = bool(u["akeys"] & i.mot_keys) and (
                bool(re.search(r"MOT|Operating Theatre", str(u["depts"]))) or any(x["sheet"] in i.mot_sheets for x in u["systems"]))
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
            for k in tag_keys(e["tag"]):
                if e.get("issue_set") == "working":
                    self.work_tag_keys[k].append(e)
                elif e.get("issue_set") == "tender":
                    self.tender_tag_keys[k].append(e)
                elif e.get("issue_set") == "mot_nsc":
                    self.mot_tag_keys[k].append(e)
        # model-level drawn tags (AHUs and fans of working systems)
        for s in i.working:
            for a in s["ahus"]:
                for k in tag_keys(a["tag"]["value"]):
                    self.work_tag_keys[k].append({"sheet": s["sheet"], "tag": a["tag"]["value"]})

    def _sheet_units(self, sheet: str) -> Dict[str, Tuple[Dict[str, Any], List[float], str]]:
        """Units of the zone schedule whose AHU, FCU or CRAC tag the sheet prints (unit tag -> unit, box, text)."""
        out: Dict[str, Tuple[Dict[str, Any], List[float], str]] = {}
        for t, b in self.i.st.lines(sheet):
            m = re.match(r"^((?:AHU|FCU|CRAC)-[A-Z0-9][A-Za-z0-9\-]*(?:/[A-Z0-9]+)?(?:\s[A-Z])?)", t)
            if not m:
                continue
            for k in tag_keys(m.group(1)):
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
                            for kk in tag_keys(k["tag"]["value"]):
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
    def _per_key_sum(items: Iterable[Tuple[str, float, Dict[str, Any]]]) -> Tuple[Optional[float], List[Dict[str, Any]]]:
        """Sum over distinct keys of the largest value (an A/B pair counts once)."""
        best: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        for tag, v, ref in items:
            if v is None:
                continue
            for k in tag_keys(tag) or {tag}:
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
        return out

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

    # -- unit review ------------------------------------------------------------#
    def review_all(self) -> None:
        for u in self.i.units:
            if not u["has_tag"]:
                self.untagged.append(u)
                continue
            self.review_unit(u)
        self.review_untagged()

    def review_unit(self, u: Dict[str, Any]) -> None:
        tag = u["tag"]
        rows: List[Row] = []
        sheet, page, tagbox = self.sheet_of(u)
        info = {"tag": tag, "type": u["type"], "sheets": sorted({s["sheet"] for s in u["systems"]}),
                "complete": u["complete"], "counts": Counter(), "mot": u["mot"]}
        self.unit_info[tag] = info
        d054 = u["complete"] and u["reduction"] not in (None, "")
        info["d054"] = d054
        if not u["systems"]:
            self.no_drawn_unit(u, rows, info)
        else:
            self.rooms_block(u, rows, d054, info)
            if u["type"] in ("AHU", "CRAC"):
                self.totals_block(u, rows, d054, info)
                self.fans_block(u, rows, d054, info)
            else:
                rows.append(mk(tag, "queries", "AHU total", tag, "Unit", "", "drawn", "n/a", "not reviewed",
                               "Query: unit type is " + str(u["type"]) + "; the totals are reviewed under its AHU", True,
                               sheet, page, tagbox, u["source"]))
        if u["mot"]:
            # D-057: the specified values of a MOT theatre unit are kept; reviewed and listed, not marked up
            for r in rows:
                if r["needs_id"]:
                    r["note"] = (r["note"] + " " if r["note"] else "") + "Without this rule the action would be: " + \
                        r["action"][0].lower() + r["action"][1:] + "."
                    r["needs_id"] = False
                    r["layer"] = "queries"
                    r["action"] = "List only (MOT NSC): specified values are kept; no mark-up"
            ids = {id(r) for r in rows}
            self.missing = [m for m in self.missing if id(m) not in ids]
        for r in rows:
            r["unit"] = tag
        self.rows.extend(rows)
        info["rows"] = rows

    def no_drawn_unit(self, u: Dict[str, Any], rows: List[Row], info: Dict[str, Any]) -> None:
        tag = u["tag"]
        keys = u["keys"]
        mot = [e for k in keys for e in self.mot_tag_keys.get(k, [])]
        if mot:
            sh = sorted({e["sheet"] for e in mot})
            rows.append(mk(tag, "queries", "AHU total", tag, "Unit on a MOT sheet", "", "drawn on " + ", ".join(sh), "n/a",
                           "listed (MOT)", "List only (MOT): see the MOT tab; no mark-up", False, sh[0], mot[0]["page"],
                           jbox(mot[0]["box_page_view_tag"]), u["source"],
                           "The unit is on a sheet of the MOT NSC. Those sheets are reviewed and listed. They are not marked up."))
            info["counts"]["listed (MOT)"] += 1
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
        mr = mk(tag, "new systems", "Missing system", tag, "Unit with no schematic", "L/s",
                "not drawn", req, "missing system", "New system: draw on a sheet", True,
                belongs[0] if belongs else "NEW", 1, None, u["source"] + ("; Workbook master summary, tab AHU Master, row %s" % m["_row"] if m else ""),
                where + " " + needed)
        rows.append(mr)
        self.missing.append(mr)
        info["counts"]["missing system"] += 1
        # rooms of the zone list, listed for the new system
        zr = self.zone_rooms(u)
        for n, v in sorted(zr.items()):
            rows.append(mk(tag, "new systems", "Room", v["name"], "Rooms in the zone list", "no.", "not drawn", v["count"],
                           "to add", "Add with the new system", False, "", "", None,
                           "Zone schedule room list; " + (v["links"][0].get("source_ref") or "")[:160]))
            info["counts"]["to add"] += 1

    # item 1 and 2
    def rooms_block(self, u: Dict[str, Any], rows: List[Row], d054: bool, info: Dict[str, Any]) -> None:
        tag = u["tag"]
        drawn = self.drawn_rooms(u)
        zone = self.zone_rooms(u)
        plan = self.plan_for(u)
        sheet, page, tagbox = self.sheet_of(u)
        complete = u["complete"]
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
            # zone link unknown for the whole unit
            res = [dict(cls="not established", dname=n, zname=None, dc=v["count"], zc=None,
                        why="the zone schedule gives no room to this unit") for n, v in sorted(drawn.items())]
        else:
            res = classify_rooms(drawn, zone, complete, zother, dother, plan, sheet_names)
        info["zone_keys"] = set(zone)
        info["classed_keys"] = {r["zname"] for r in res if r["zname"] is not None}
        for r in res:
            d = drawn.get(r["dname"]) if r["dname"] is not None else None
            z = zone.get(r["zname"]) if r["zname"] is not None else None
            act, need = ROOM_ACTION[r["cls"]]
            rm = d["rm"] if d else None
            bx = rm["name"]["box"] if rm else tagbox
            sh = d["sheet"] if d else sheet
            pg = d["page"] if d else page
            dtext = (d["name"] or "no name read on the box") if d else "not drawn"
            ztext = z["name"] if z else "not in zone list"
            item = dtext if d else ztext
            note = ""
            if r["cls"] in ("name differs", "renamed"):
                note = f"Drawn name: {dtext}. Zone name: {ztext}."
            elif r["cls"] == "named in box":
                note = f"The box shows several room names: {dtext}. It names this room: {ztext}."
            elif r["cls"] == "moved to another unit":
                note = f"The zone list gives this room to {r['other']}."
            elif r["cls"] == "moved from another unit":
                note = f"The room is drawn on {r['other']}."
            elif r["cls"] == "drawn without tag":
                note = "A box with this name is on the sheet. It does not carry the tag of this unit."
            elif r.get("why"):
                note = r["why"][0].upper() + r["why"][1:] + "."
            if r.get("plan"):
                note = f"The zone assignment gives the plan room {r['plan']} to this unit."
            if r["cls"] == "typical count changed" and r.get("ratio") is not None and r["dname"] != r["zname"]:
                note = f"Drawn name: {dtext}. Zone name: {ztext}. " + note
            if r["cls"] in ("to add", "moved from another unit") and z:
                cited = set()
                for lk in z["links"]:
                    cited |= set(re.findall(r"IP-BH-[0-9A-Z]{5}", lk.get("source_ref") or ""))
                mine = {x["sheet"] for x in u["systems"]}
                if cited and not (cited & mine):
                    r["cls"] = "drawn on another sheet"
                    act, need = ROOM_ACTION["drawn on another sheet"]
                    note = "The zone list takes this room from the sheet " + ", ".join(sorted(cited)) + ". It is not drawn on this sheet."
            lay = "queries" if r["cls"] in ("not established", "drawn without tag") else "rooms"
            src = (f"Working drawing {sh} p{pg}, room box" if d else "") + \
                  ("; " if d and z else "") + \
                  (short(clean(z["links"][0].get("source_ref") or "Zone schedule room list"), 140) if z else "")
            zreq = r["zc"] if (r["zc"] is not None and not (r["cls"] == "not established" and not z)) else "not established"
            if r.get("plan"):
                zreq = r["dc"]
            rows.append(mk(tag, lay, "Room", item, "Typical count", "no.", r["dc"] if d else "not drawn",
                           zreq, r["cls"], act, need, sh, pg, bx, src, note))
            info["counts"][r["cls"]] += 1
            # airflows for rooms in both lists
            if d and z and r["cls"] in ("unchanged", "typical count changed", "name differs", "not established") \
                    and not d.get("composite"):
                self.airflow_rows(u, d, z, rows, d054, info)

    def airflow_rows(self, u: Dict[str, Any], d: Dict[str, Any], z: Dict[str, Any], rows: List[Row],
                     d054: bool, info: Dict[str, Any]) -> None:
        tag = u["tag"]
        md = d["mode"]
        if not md:
            return
        wbr = []
        for lk in z["links"]:
            dept = f"{int(lk['dept']):02d}" if str(lk["dept"]).isdigit() else str(lk["dept"])
            w = self.wb_room.get((dept, lk["dpa_room_id"]))
            if w:
                wbr.append((dept, lk["dpa_room_id"], w))
        sheet, page = d["sheet"], d["page"]
        name = d["name"]
        quantities = [("Supply airflow", "supply_L_s", "supply", "Final supply"),
                      ("Return airflow", "return_L_s", "return", "Return"),
                      ("Exhaust airflow", "exhaust_L_s", "exhaust", "Exhaust")]
        for qn, mk_key, wk, col in quantities:
            dv = md.get(mk_key) or {}
            drawn_v = dv.get("value")
            bx = dv.get("box") or d["rm"]["name"]["box"]
            vals = [w[wk] for _, _, w in wbr if w[wk] is not None]
            req = (sum(vals) / len(vals)) if vals else None
            if req is not None:
                req = round(req, 1)
            if drawn_v is None and (req is None or req <= 0.5):
                continue
            cls, act, need = judge(drawn_v, req, d054)
            note = f"Per room. Rooms drawn: {d['count']}; rooms in workbook: {len(wbr)}."
            if len(vals) > 1:
                lo, hi = min(vals), max(vals)
                note += f" The required value is the average of {len(vals)} workbook rooms ({lo:,.0f} to {hi:,.0f} L/s); check by a person."
                if req and (hi - lo) > 0.25 * req and drawn_v is not None:
                    kinds = {judge(drawn_v, v, d054)[0] for v in (lo, req, hi)}
                    if len(kinds) > 1:
                        cls, act, need = ("linked rooms differ", "Query: linked workbook rooms differ; check which rooms the box stands for", True)
            if req is None:
                src = f"Working drawing {sheet} p{page}; no workbook value for this room"
            else:
                src = f"Working drawing {sheet} p{page}; " + "; ".join(
                    f"Workbook D{dp} tab Airflow cell {w['cols'].get(col, '')} (room {rn})" for dp, rn, w in wbr[:2])
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
            rows.append(mk(tag, "airflows" if cls not in ("not established", "linked rooms differ") else "queries",
                           "Room airflow", name, qn, "L/s", drawn_v, req, cls, act, need, sheet, page, bx, src, note))
            info["counts"][cls] += 1
        # pressure and offset
        prs = [(dp, rn, self.i.wb[dp]["pressure"].get(rn)) for dp, rn, _ in wbr]
        prs = [(dp, rn, p) for dp, rn, p in prs if p and p["set"] is not None]
        pdrawn = parse_pressure((d["rm"].get("pressure") or {}).get("value"))
        if prs:
            setp = prs[0][2]["set"]
            woff = [w["offset"] for _, _, w in wbr if w["offset"] is not None]
            pb = (d["rm"].get("pressure") or {}).get("box")
            if pdrawn is None:
                cls, act, need = "not drawn", "Mark up: show the pressure target", True
            elif abs(pdrawn - setp) > 0.5:
                cls, act, need = "differs", "Query: pressure target differs", True
            else:
                cls, act, need = "no change", "No change", False
            note = "Offset air needed: " + (f"{woff[0]:,.0f} L/s" if woff else "not given") + \
                   " (Pressure tab). The sheet does not draw the offset in L/s."
            rows.append(mk(tag, "queries" if need else "airflows", "Room airflow", name, "Pressure target", "Pa",
                           pdrawn if pdrawn is not None else "not drawn", setp, cls, act, need, sheet, page, pb,
                           f"Working drawing {sheet} p{page}; Workbook D{prs[0][0]} tab Pressure row {prs[0][2]['row']}", note))
            info["counts"][cls] += 1

    # item 3 ---------------------------------------------------------------------
    def wb_ahu_rows(self, u: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
        out = []
        tags = {u["tag"]} | ({u["alias"]} if u.get("alias") else set())
        for dept, d in self.i.wb.items():
            exact = [r for r in d["ahu"] if r["tag"] in tags]
            use = exact or [r for r in d["ahu"] if tag_keys(r["tag"]) & u["akeys"] and "group" not in u["tag"].lower()
                            and "group" not in r["tag"].lower()]
            out.extend((dept, r) for r in use)
        return out

    def system_return_items(self, u: Dict[str, Any]) -> List[Tuple[str, float, Dict[str, Any]]]:
        """Return fans drawn on the systems of the unit.  A return fan serves its system; its tag names the
        fan (RAF), not the AHU, so the match is by system and not by tag."""
        items = []
        for tg, (s, f) in self.return_fans(u).items():
            items.append((tg, f["duty"]["value"], f["duty"]))
        return items

    @staticmethod
    def core(key: str) -> str:
        """Unit name of a tag key without prefix, level and A/B letter: 'RAF:B-B2-B2PHAB-02' gives 'B2PHAB-02'."""
        parts = key_only(key).split("-")
        return (parts[-2] + "-" + re.sub(r"(/[A-Z]|[A-Z])$", "", parts[-1])) if len(parts) >= 2 else key_only(key)

    def sheet_tags(self, u: Dict[str, Any], prefixes: Tuple[str, ...]) -> List[Tuple[Dict[str, Any], str, List[float]]]:
        """Tag lines of the sheets of the unit whose unit name equals that of the unit (the text of the sheet,
        read directly, because the model misses fans whose tag has no duty line)."""
        cores = {self.core(k) for k in u["akeys"]}
        out = []
        for s in u["systems"]:
            for t, b in self.i.st.lines(s["sheet"]):
                m = re.match(r"^((?:%s)-[A-Z0-9][A-Za-z0-9\-]*(?:/[A-Z0-9]+)?)" % "|".join(prefixes), t)
                if not m:
                    continue
                ks = tag_keys(m.group(1))
                if any(self.core(k) in cores for k in ks):
                    out.append((s, re.sub(r"\s+", "", m.group(1)), b))
        return out

    def return_fans(self, u: Dict[str, Any]) -> Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]]:
        """Return fans of the unit: those the model holds, else the RAF tags that the sheet prints."""
        out: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
        for s in u["systems"]:
            for f in s["return_air"]["fans"]:
                out.setdefault(f["tag"]["value"], (s, f))
        if out:
            return out
        for s, tg, b in self.sheet_tags(u, ("RAF",)):
            if tg in out:
                continue
            dn = self.i.st.duty_near(s["sheet"], b)
            out[tg] = (s, {"tag": {"value": tg, "box": b},
                           "duty": {"value": dn[0] if dn else None, "box": dn[1] if dn else b}, "type": "RETURN"})
        return out

    def totals_block(self, u: Dict[str, Any], rows: List[Row], d054: bool, info: Dict[str, Any]) -> None:
        tag = u["tag"]
        sheet, page, tagbox = self.sheet_of(u)
        wrows = self.wb_ahu_rows(u)
        mast, alias_note = self.master_for(u)
        keys = u["akeys"]
        gmast = None
        if not (mast and mast.get("Calculated airflow")):
            gmast = next((g for g in u["groups"] if self.i.master.get(g["tag"], {}).get("Calculated airflow")), None)
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
        ret_req = (recirc or 0) + max(spill_w or 0, 0) if have_wb and recirc is not None else None
        spill_req = max(spill_w, 0.0) if (have_wb and spill_w is not None) else None
        coil_req = sums("coil") if have_wb else None
        info["req"] = {"supply": sup_req, "oa": oa_req, "return": ret_req, "spill": spill_req, "coil": coil_req}
        src_wb = "; ".join(f"Workbook D{dp} tab AHU row {r['row']}" for dp, r in wrows[:3]) or "no workbook row"
        src_m = f"Workbook master summary tab AHU Master row {mast['_row']}" if mast else ""
        sup_items, oa_items, spill_vals = [], [], []
        for s in u["systems"]:
            for a in s["ahus"]:
                if tag_keys(a["tag"]["value"]) & keys:
                    sup_items.append((a["tag"]["value"], a["duty_supply_L_s"]["value"], a["duty_supply_L_s"]))
            for ln in s["outdoor_air"]["lines"]:
                if tag_keys(ln["to"]) & keys:
                    # the model's box is the label that lists the OA of each AHU; the mark-up goes on the drawn value
                    atag = next((a["tag"].get("box") for a in s["ahus"] if tag_keys(a["tag"]["value"]) & tag_keys(ln["to"])), None)
                    vb = self.i.st.oa_box(s["sheet"], ln["value_L_s"], atag)
                    ref = dict(ln)
                    if vb:
                        ref["box"] = vb
                        ref["box_level"] = "value"
                    oa_items.append((ln["to"], ln["value_L_s"], ref))
            for ln in s["spill_relief"]["lines"]:
                spill_vals.append((ln["value_L_s"], ln))
        spill_path_drawn = bool(spill_vals)
        if not spill_vals and u["systems"]:
            # a spill label that the model did not link: the label nearest to the unit's AHU tag
            best = None
            for s in u["systems"]:
                for v, b in self.i.st.spill_labels(s["sheet"]):
                    d = SheetText._dist(b, tagbox)
                    if best is None or d < best[0]:
                        best = (d, v, b)
            if best:
                spill_path_drawn = True
                if best[1] is not None:
                    spill_vals.append((best[1], {"box": best[2]}))
        ret_items = self.system_return_items(u)
        sup_d, sup_refs = self._per_key_sum(sup_items)
        oa_d, oa_refs = self._per_key_sum(oa_items)
        ret_d, ret_refs = self._per_key_sum(ret_items)
        spill_d = sum(v for v, _ in spill_vals if v is not None) if any(v is not None for v, _ in spill_vals) else None
        info["drawn"] = {"supply": sup_d, "oa": oa_d, "return": ret_d, "spill": spill_d}
        # once-through unit: the workbook recirculates no air, so a return fan is wrong; the spill needs a relief path
        once_through = bool(have_wb and recirc is not None and recirc <= BAL_LPS)
        info["once_through"] = once_through
        will_add_fan = (not ret_items and not once_through and ret_req and ret_req > BAL_LPS
                        and spill_req and spill_req > BAL_LPS)

        def bx(refs):
            return (refs[0].get("box") if refs else None) or tagbox

        def row(qty: str, uom: str, drawn: Optional[float], req: Optional[float], refs, src: str, extra: str = "") -> None:
            cls, act, need = judge(drawn, req, d054) if (drawn is not None or req is not None) else ("no change", "No change", False)
            if req is None and drawn is None:
                return
            if req is None and gmast is not None:
                rows.append(mk(tag, "airflows", "AHU total", tag, qty, uom, drawn if drawn is not None else "not drawn", "held for the group",
                               "held at group level", "No separate mark-up: the requirement is held for the group; see the tab of the group",
                               False, sheet, page, bx(refs) if refs else tagbox,
                               f"Working drawing {sheet} p{page}; Workbook master summary row of {gmast['tag']}", extra.strip()))
                info["counts"]["held at group level"] += 1
                return
            if drawn is None and qty in ("Return air", "Spill or relief air"):
                if req is None or req <= BAL_LPS:
                    return
                if qty == "Return air":
                    see = ("see the return fan row" if will_add_fan else
                           "the unit takes 100 % outdoor air, so it has no return fan; see the spill path row" if once_through else
                           "the sheet draws no return fan and the spill is not more than 10 L/s")
                else:
                    see = "see the spill path row" if not spill_path_drawn else \
                        "the spill path is drawn and its value is not printed"
                rows.append(mk(tag, "airflows", "AHU total", tag, qty, uom, "not drawn", req, "not drawn",
                               "No separate mark-up: " + see, False, sheet, page, tagbox,
                               f"Working drawing {sheet} p{page}; {src}", "Not drawn on the sheet. " + extra))
                info["counts"]["not drawn"] += 1
                return
            note = extra
            if drawn is not None and req is not None:
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
            lay = "queries" if cls == "not established" else "airflows"
            rows.append(mk(tag, lay, "AHU total", tag, qty, uom, drawn, req, cls, act, need, sheet, page,
                           bx(refs) if refs else tagbox, f"Working drawing {sheet} p{page}; {src}", note.strip()))
            info["counts"][cls] += 1

        part = alias_note + " " if alias_note else ""
        cover = (u.get("area_counted") / u["area"]) if (u.get("area_counted") and u.get("area")) else None
        info["cover"] = cover
        if cover is not None and cover > 1.10:
            part += (f"Room list not confirmed: the workbooks count {u['area_counted']:,.0f} m² for this unit and the zoning "
                     f"gives {u['area']:,.0f} m² ({cover:.0%}). The increase can come from rooms that the unit does not serve. "
                     "Check the room list before you apply it. ")
        if not u["complete"]:
            part += (f"The room list of the unit is not complete: the workbook value covers {len(u['links'])} of "
                     f"{u['rooms']} zoned rooms, so a margin is not a reduction. ")
        leak = part + f"Workbook uses {LEAK_PCT:.0%} duct leakage; the sheets print 15 %. "
        row("Supply airflow", "L/s", sup_d, sup_req, sup_refs, f"{src_m or src_wb}", leak)
        row("Outdoor air", "L/s", oa_d, oa_req, oa_refs, f"{src_m or src_wb}",
            part + ("" if oa_d is not None else "No outdoor-air line for this unit on the sheet. "))
        row("Return air", "L/s", ret_d, ret_req, ret_refs, src_wb, part + "Return = recirculated air + spill air. Drawn value: return fans of the system. ")
        spill_note = "The sheet value is for the whole system. " if len(u["systems"]) else ""
        row("Spill or relief air", "L/s", spill_d, spill_req,
            [r for _, r in spill_vals][:1], src_wb, spill_note)
        sch = self.i.schedule.get(next(iter(u["keys"]), ""), None) if u["keys"] else None
        if coil_req is not None:
            cdraw = sch["cool"] if sch else None
            rows.append(mk(tag, "queries", "AHU total", tag, "Coil duty", "kW", cdraw if cdraw is not None else "not drawn",
                           round(coil_req, 1), "not drawn on sheet",
                           "List only: coil duty is not drawn on the sheet", False, sheet, page, tagbox,
                           f"Equipment schedule (Corr 5) {sch['pages'] if sch else 'no row'}; {src_wb}",
                           "Drawn value is the scheduled cooling duty, not a value printed on the sheet."))
            info["counts"]["not drawn on sheet"] += 1
        # missing: spill path
        if spill_req and spill_req > BAL_LPS and spill_d is None and not spill_path_drawn:
            mr = mk(tag, "new systems", "Missing system", tag, "Spill or relief path", "L/s", "not drawn", round(spill_req, 1),
                    "missing system", "Mark up: add a spill or relief path", True, sheet, page, tagbox, src_wb,
                    "Outdoor air is more than the exhaust and the offset air. Add a relief path with its louvre.")
            rows.append(mr)
            self.missing.append(mr)
            info["counts"]["missing system"] += 1

    # item 4 ---------------------------------------------------------------------
    def _wb_fans(self) -> Dict[str, List[Tuple[str, Dict[str, Any]]]]:
        if not hasattr(self, "_wbf"):
            self._wbf = defaultdict(list)
            for dept, d in self.i.wb.items():
                for f in d["fans"]:
                    pre = f["tag"].split("-")[0]
                    for k in tag_keys(f["tag"]):
                        self._wbf[k].append((dept, f))
        return self._wbf

    def fan_rooms(self) -> Dict[str, List[Tuple[str, str, Dict[str, Any]]]]:
        """Workbook rooms by the exhaust fan they are tied to (key of the fan tag)."""
        if not hasattr(self, "_fr"):
            self._fr = defaultdict(list)
            for (dept, rn), w in self.wb_room.items():
                for k in tag_keys(str(w.get("exh_fan") or "")):
                    self._fr[k].append((dept, rn, w))
        return self._fr

    def shared_fan(self, found: List[Tuple[str, Dict[str, Any]]]) -> Optional[float]:
        """Total that the Fans tab ties to this fan and to at least one other fan of the same tab (same
        serves, same second AHU, same required airflow): the total counts once for each fan."""
        for dp, x in found:
            if not x.get("required"):
                continue
            same = [y for y in self.i.wb[dp]["fans"] if y["tag"] != x["tag"] and y["serves"] == x["serves"]
                    and y.get("also") == x.get("also") and y["required"] == x["required"]
                    and str(y["type"]).upper() == str(x["type"]).upper()]
            if same:
                return x["calc"] or x["required"]
        return None

    def fan_conflict(self, ks: Iterable[str], s: Dict[str, Any], req: Optional[float], drawn: Optional[float]) -> str:
        """Text when the workbook ties to the fan rooms that the sheet puts on other AHUs, or when the
        requirement is at least three times the drawn duty (a cause to check before a resize)."""
        sysk = self.sys_keys.get(s["system_id"], frozenset())
        other, tot = [], 0.0
        for k in ks:
            for dept, rn, w in self.fan_rooms().get(k, []):
                if w["exhaust"] and w["exhaust"] > 0.5:
                    tot += w["exhaust"]
                    if w["ahu"] and not (tag_keys(str(w["ahu"])) & sysk):
                        other.append((w["name"], w["exhaust"], str(w["ahu"])))
        msgs = []
        if other:
            msgs.append(f"The workbook ties {len(other)} rooms of other AHUs to this fan ({sum(x[1] for x in other):,.0f} of "
                        f"{tot:,.0f} L/s), for example {other[0][0]} on {other[0][2]}.")
        if req and drawn and req >= 3 * drawn:
            msgs.append(f"The requirement is {req / drawn:.1f} times the drawn duty.")
        if msgs:
            msgs.append("The fan assignment of the workbook is to be confirmed before the fan is resized.")
        return " ".join(msgs)

    def fans_block(self, u: Dict[str, Any], rows: List[Row], d054: bool, info: Dict[str, Any]) -> None:
        tag = u["tag"]
        sheet, page, tagbox = self.sheet_of(u)
        wbf = self._wb_fans()
        seen = set()
        iso_labels: List[Dict[str, Any]] = []
        iso_present = False
        exh_present = False
        cls_map = {"increase": "to resize", "reduction": "to resize", "no change": "adequate", "margin": "adequate",
                   "not drawn": "duty not drawn", "not established": "not established"}
        keep = "Fan pressure cannot be taken from a schematic: the drawn value is kept."
        for s in u["systems"]:
            for f in s["exhaust_fans"]:
                tg = f["tag"]["value"]
                ks = tag_keys(tg)
                if not ks or (tg, s["sheet"]) in seen:
                    continue
                seen.add((tg, s["sheet"]))
                exh_present = True
                found = [(dp, x) for k in ks for dp, x in wbf.get(k, [])]
                req = sum(x["calc"] or 0 for _, x in found) if found else None
                drawn_v = f["duty"]["value"]
                srcw = "; ".join(f"Workbook D{dp} tab Fans cell {x['cell']}" for dp, x in found[:2]) or "no row on the Fans tab"
                if req is not None and req <= 0.5:
                    if d054:
                        cls, act, need = "to remove", "Mark up: strike through the fan", True
                    else:
                        cls, act, need = "not established", "Query: no room is tied to this fan", True
                    note = "No room of the workbooks is tied to this fan. " + keep
                    req_v = 0
                else:
                    j = judge(drawn_v, round(req, 1) if req is not None else None, d054)
                    cls, act, need = cls_map[j[0]], j[1], j[2]
                    if cls == "to resize":
                        act = "Mark up: resize the fan"
                        cf = self.fan_conflict(ks, s, req, drawn_v)
                        if cf:
                            cls, act, need = "not established", "Query: workbook fan assignment to confirm", True
                    elif cls == "adequate":
                        act, need = "No change", False
                    note = keep
                    req_v = round(req, 1) if req is not None else None
                    if cls == "not established" and j[0] == "increase":
                        note = cf + " " + keep
                    if drawn_v is not None and req is not None and j[0] in ("margin", "no change"):
                        note = f"Margin {drawn_v - req:,.0f} L/s. " + keep
                lay = "fans" if cls not in ("not established",) else "queries"
                rows.append(mk(tag, lay, "Fan", tg, f"{f['type']} airflow", "L/s", drawn_v, req_v, cls, act, need,
                               s["sheet"], s["page"], f["duty"].get("box") or f["tag"].get("box"),
                               f"Working drawing {s['sheet']} p{s['page']}; {srcw}", note))
                info["counts"]["fan: " + cls] += 1
            for lb in s["isolation_exhaust"]["header_labels"]:
                iso_present = True
                iso_labels.append(lb)
            for lb in s["isolation_exhaust"]["line_rows"]:
                if lb["duty"]["value"] is not None:
                    iso_present = True
        # return fans: matched by system (a return fan tag names the fan, not the AHU)
        ret_fans = self.return_fans(u)
        req = info.get("req", {})
        ret_req, spill_req = req.get("return"), req.get("spill")
        rest: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
        for tg, (s0, f0) in sorted(ret_fans.items()):
            found, seenr = [], set()
            for k in tag_keys(tg):
                for dp, x in wbf.get(k, []):
                    if (dp, x["row"]) not in seenr and str(x.get("type")).upper().startswith("RETURN"):
                        seenr.add((dp, x["row"]))
                        found.append((dp, x))
            if not found:
                rest[tg] = (s0, f0)
                continue
            drawn_v = f0["duty"]["value"]
            reqv = round(max(x["calc"] or 0 for _, x in found), 1)
            shared = self.shared_fan(found)
            if shared and ret_req is not None:
                # the workbook ties one total to several fans: the requirement of this fan is the return air of
                # the unit alone (the total is not split here)
                reqv = round(ret_req, 1)
            j = judge(drawn_v, reqv, d054)
            cls, act, need = cls_map[j[0]], j[1], j[2]
            if cls == "to resize":
                act = "Mark up: resize the fan"
                if shared and ret_req is None:
                    cls, act, need = "not established", "Query: workbook fan assignment to confirm", True
            elif cls == "adequate":
                act, need = "No change", False
            note = keep
            if shared:
                note = (f"The Fans tab gives one total of {shared:,.0f} L/s to several fans (shared requirement). "
                        "The value here is the return air of this unit alone. The split between the fans is to be confirmed. ") + keep
            if drawn_v is not None and j[0] in ("margin", "no change"):
                note = f"Margin {drawn_v - reqv:,.0f} L/s. " + keep
            srcw = "; ".join(f"Workbook D{dp} tab Fans cell {x['cell']}" for dp, x in found[:2])
            rows.append(mk(tag, "fans" if cls != "not established" else "queries", "Fan", tg, "Return fan airflow", "L/s",
                           drawn_v, reqv, cls, act, need, s0["sheet"], s0["page"],
                           f0["duty"].get("box") or f0["tag"].get("box"),
                           f"Working drawing {s0['sheet']} p{s0['page']}; {srcw}", note))
            info["counts"]["fan: " + cls] += 1
        if rest:
            s0, f0 = next(iter(rest.values()))
            drawn_v, _ = self._per_key_sum([(t, f["duty"]["value"], f["duty"]) for t, (_, f) in rest.items()])
            tags = ", ".join(sorted(rest))
            if ret_req is None:
                cls, act, need = "not established", "Query: required return airflow not established", True
            else:
                j = judge(drawn_v, round(ret_req, 1), d054)
                cls, act, need = cls_map[j[0]], j[1], j[2]
                if cls == "to resize":
                    act = "Mark up: resize the fan"
                elif cls == "adequate":
                    act, need = "No change", False
            rows.append(mk(tag, "fans" if cls != "not established" else "queries", "Fan", tags, "Return fan airflow", "L/s",
                           drawn_v, round(ret_req, 1) if ret_req is not None else None, cls, act, need,
                           s0["sheet"], s0["page"], f0["duty"].get("box") or f0["tag"].get("box"),
                           f"Working drawing {s0['sheet']} p{s0['page']}; Workbook AHU tab (return = recirculated + spill)", keep))
            info["counts"]["fan: " + cls] += 1
        if not ret_fans and not info.get("once_through") and ret_req and ret_req > BAL_LPS and spill_req and spill_req > BAL_LPS:
            mr = mk(tag, "fans", "Fan", "Return fan", "Return fan airflow", "L/s", "not drawn", round(ret_req, 1), "to add",
                    "Mark up: add a return fan", True, sheet, page, tagbox,
                    f"Working drawing {sheet} p{page}; Workbook AHU tab (return = recirculated + spill)",
                    f"Air must spill ({spill_req:,.0f} L/s) and no return fan is drawn on the system. " + keep)
            rows.append(mr)
            info["counts"]["fan: to add"] += 1
        # isolation exhaust
        iso_rooms = [(n, d) for n, d in self.drawn_rooms(u).items()
                     if "IEA" in str(d["rm"].get("services_drawn") or "") or d["rm"].get("isolation_exhaust_target")]
        if iso_present or iso_rooms:
            zr = self.zone_rooms(u)
            reqv = None
            vals = []
            for n, d in iso_rooms:
                for lk in zr.get(n, {}).get("links", []):
                    dept = f"{int(lk['dept']):02d}" if str(lk["dept"]).isdigit() else str(lk["dept"])
                    w = self.wb_room.get((dept, lk["dpa_room_id"]))
                    if w and w["exhaust"] is not None:
                        vals.append(w["exhaust"])
            if vals:
                reqv = round(sum(vals), 1)
            lbl = sorted({lb["value_L_s"] for lb in iso_labels if lb["value_L_s"] is not None})
            if len(lbl) == 1:
                dv, note = lbl[0], keep
                j = judge(dv, reqv, d054)
                cls, act, need = cls_map[j[0]], j[1], j[2]
                if cls == "to resize":
                    act = "Mark up: resize the fan"
                elif cls == "adequate":
                    act, need = "No change", False
            else:
                dv = None
                cls, act, need = "not established", "Query: isolation exhaust duty to confirm", True
                note = ("Header labels on the sheet: " + (", ".join(f"{x:,.0f}" for x in lbl) or "none") +
                        " L/s. The sheet does not give one total for these rooms. " + keep)
            lb0 = iso_labels[0] if iso_labels else None
            rows.append(mk(tag, "fans" if cls != "not established" else "queries", "Fan", "Isolation exhaust (IEA)",
                           "Isolation exhaust airflow", "L/s", dv if dv is not None else "see note", reqv, cls, act, need,
                           sheet, page, (lb0 or {}).get("box") or tagbox,
                           f"Working drawing {sheet} p{page}; sum of room exhaust of isolation rooms, workbook tab Airflow",
                           note))
            info["counts"]["fan: " + cls] += 1
        # missing exhaust system
        zr = self.zone_rooms(u)
        exh_need = 0.0
        exh_rooms = []
        for n, v in zr.items():
            for lk in v["links"]:
                dept = f"{int(lk['dept']):02d}" if str(lk["dept"]).isdigit() else str(lk["dept"])
                w = self.wb_room.get((dept, lk["dpa_room_id"]))
                if w and w["exhaust"] and w["exhaust"] > 0.5:
                    exh_need += w["exhaust"]
                    exh_rooms.append(w["name"])
        if not exh_present:
            # exhaust fans and links that the sheet prints and the model did not read
            exh_present = bool(self.sheet_tags(u, ("EAF", "TEAF", "TEF", "HPF"))) or \
                any(self.i.st.exhaust_links(x["sheet"]) for x in u["systems"])
        if exh_need > BAL_LPS and not exh_present and not iso_present:
            mr = mk(tag, "new systems", "Missing system", tag, "Exhaust system", "L/s", "not drawn", round(exh_need, 1),
                    "missing system", "Mark up: add an exhaust fan and its ducts", True, sheet, page, tagbox,
                    f"Working drawing {sheet} p{page}; Workbook tab Airflow, room exhaust",
                    f"{len(exh_rooms)} rooms need exhaust and the sheet draws no exhaust fan for this unit.")
            rows.append(mr)
            self.missing.append(mr)
            info["counts"]["missing system"] += 1

    # -- units with no tag in any document (item 5) ---------------------------- #
    # -- one drawn box, one result (item 3) ---------------------------------------- #
    RANK = {"unchanged": 0, "named in box": 0, "name differs": 0, "no change": 0, "margin": 0, "adequate": 0,
            "drawn on another sheet": 0, "typical count changed": 1, "increase": 1, "reduction": 1, "to resize": 1, "not drawn": 1, "to add": 1,
            "differs": 2, "drawn without tag": 2, "linked rooms differ": 2, "not established": 3,
            "moved to another unit": 4, "moved from another unit": 4, "to remove": 5}

    def dedupe_boxes(self) -> None:
        """A drawn box can be reviewed under several units (an A/B pair, its group, an alias).  Keep one result
        for each (sheet, box, item, quantity): the one where a room list holds the room (unchanged first,
        removal last).  The other tabs show a pointer row with no change ID of its own."""
        groups: Dict[Tuple[Any, ...], List[Tuple[str, Row]]] = defaultdict(list)
        sysids: Dict[str, frozenset] = {}
        for tg, u in self.unit_by_tag.items():
            sysids[tg] = frozenset(s["system_id"] for s in u["systems"])
        for tg, info in self.unit_info.items():
            for r in info.get("rows", []):
                if r["row_type"] in ("Room", "Room airflow", "Fan") and r["box"] and r["quantity"] != "":
                    key = (r["sheet"], r["box"], r["item"], r["quantity"])
                    groups[key].append((tg, r))
        # new return fans and similar rows are placed beside the system: one per system, not one per unit tab
        add_rows: Dict[Tuple[str, str], List[Tuple[str, Row]]] = defaultdict(list)
        for tg, info in self.unit_info.items():
            for r in info.get("rows", []):
                if r["row_type"] in ("Fan", "Missing system") and r["cls"] in ("to add", "missing system") \
                        and r["layer"] in ("fans", "new systems") and r["quantity"] in (
                            "Return fan airflow", "Spill or relief path", "Exhaust system") and sysids.get(tg):
                    add_rows[(r["sheet"], r["quantity"])].append((tg, r))
        n_moved = 0

        def pick(cands: List[Tuple[str, Row]]) -> Tuple[str, Row]:
            return min(cands, key=lambda c: (self.RANK.get(c[1]["cls"], 3), self.unit_by_tag[c[0]]["is_group"] is False,
                                             c[0]))

        def point(win: Tuple[str, Row], lose: List[Tuple[str, Row]]) -> None:
            nonlocal n_moved
            for tg, r in lose:
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
        for key, lst in groups.items():
            if len({id(r) for _, r in lst}) < 2:
                continue
            point(pick(lst), lst)
        for key, lst in add_rows.items():
            # cluster by overlapping systems
            clusters: List[List[Tuple[str, Row]]] = []
            for tg, r in lst:
                for cl in clusters:
                    if any(sysids[tg] & sysids[t2] for t2, _ in cl):
                        cl.append((tg, r))
                        break
                else:
                    clusters.append([(tg, r)])
            for cl in clusters:
                if len(cl) > 1:
                    w = min(cl, key=lambda c: (not self.unit_by_tag[c[0]]["is_group"], c[0]))
                    point(w, cl)
        self.n_dedup = n_moved

    def merge_shared_boxes(self) -> None:
        """One drawn value, one mark-up.  Two rows that change the same drawn value (same sheet, box, drawn
        value and quantity) become one: the same new value gives a pointer row; different new values give one
        query that lists them.  A room that a group and its member both add is added once."""
        uniq = list({id(r): r for r in self.rows}.values())
        groups: Dict[Tuple[Any, ...], List[Row]] = defaultdict(list)
        for r in uniq:
            if r["needs_id"] and r["box"] and r["layer"] in ("airflows", "fans") and r["action"].startswith("Mark up") \
                    and r["row_type"] in ("AHU total", "Fan"):
                groups[(r["sheet"], r["box"], str(r["drawn"]), r["quantity"])].append(r)
        n = 0
        for lst in groups.values():
            if len(lst) < 2:
                continue
            first = lst[0]
            vals = {round(num(r["required"]) or 0.0, 0) for r in lst}
            for r in lst[1:]:
                r["ref"] = first
                r["cls"] = "reviewed on another tab"
                r["action"] = "No separate mark-up: the same drawn value is changed on another row"
                r["needs_id"] = False
                n += 1
            if len(vals) > 1:
                first["layer"] = "queries"
                first["action"] = "Query: one drawn value, several requirements"
                first["cls"] = "linked rooms differ"
                first["_merged"] = lst
        # rooms added by a group and by its member on one sheet
        adds: Dict[Tuple[str, str], List[Row]] = defaultdict(list)
        for r in uniq:
            if r["needs_id"] and r["row_type"] == "Room" and "add the room" in r["action"]:
                adds[(r["sheet"], nname(r["item"]))].append(r)
        for lst in adds.values():
            if len(lst) < 2:
                continue
            lst.sort(key=lambda r: (self.unit_by_tag.get(r["unit"], {}).get("is_group", False), r["unit"]))
            first = lst[0]
            for r in lst[1:]:
                gu = self.unit_by_tag.get(r["unit"]) or {}
                mem = {m["tag"] for m in gu.get("members", [])}
                if gu.get("is_group") and first["unit"] in mem:
                    r["ref"] = first
                    r["cls"] = "reviewed on another tab"
                    r["action"] = "No separate mark-up: the room is added by the row of its unit"
                    r["needs_id"] = False
                    n += 1
        self.n_merged = n

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
    def missing_global(self) -> None:
        i = self.i
        # rooms separated onto an FCU under the outdoor-air rule
        for dept, d in sorted(i.wb.items()):
            for f in d["fcu"]:
                if f["reason"] != "OA RULE":
                    continue
                src_unit = next((u for u in i.units if u["tag"] == f["ahu"]), None)
                sheet = ""
                page = ""
                bx = None
                if src_unit and src_unit["systems"]:
                    sheet, page, bx = self.sheet_of(src_unit)
                mr = mk(str(f["ahu"]), "new systems", "Missing system", f"Room {f['room']} {f['name']}",
                        "Separated room: FCU and outdoor-air connection", "L/s", "not drawn", f["supply"],
                        "missing system", "New system: FCU and outdoor-air connection on a new sheet", True,
                        sheet or "NEW", page or 1, bx,
                        f"Workbook D{dept} tab FCU row {f['row']}",
                        f"The room takes {f['oa']:,.1f} L/s of outdoor air from the AHU. Separate it onto its own FCU. "
                        f"Outdoor-air source in the workbook: {f['oa_src']}. Draw the FCU, its outdoor-air duct and its source.")
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
            for k in tag_keys(src):
                if k.startswith("PAHU:"):
                    named.setdefault(k, {"sheets": set(), "src": src, "box": None})["sheets"].add(s["sheet"])
                    ln = s["outdoor_air"]["lines"]
                    if ln and not named[k]["box"]:
                        named[k]["box"] = ln[0].get("box")
        eq_pahu = {k for e in i.equip if e["issue_set"] == "working" and e["type"] == "PAHU" for k in tag_keys(e["tag"])}
        for k, v in sorted(named.items()):
            if k in eq_pahu:
                continue
            sh = sorted(v["sheets"])
            mr = mk(key_only(k), "new systems", "Missing system", "PAHU " + key_only(k), "Outdoor-air unit referred to, not drawn",
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
            if ks & set(self.work_tag_keys):
                continue
            st = (s.get("storey") or [""])[0]
            e0 = s["equipment"][0] if s["equipment"] else {}
            kind = {"appd_ahu_system": "AHU", "appd_fcu_group": "FCU group", "appd_mv_fan_system": "Fan-only (MV) system"}[s["kind"]]
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
        for r in read_csv(self.i.reg / "booklet_equipment_standby.csv"):
            for k in tag_keys(r["tag_normalised"]):
                if k in seen or k in self.work_tag_keys or k in self.tender_tag_keys or k in uk or k in ak:
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
            if e["issue_set"] == "working" and e["type"] in ("AHU", "PAHU", "OAF", "RAF", "TEAF", "EAF", "TEF", "HPF"):
                byt[e["tag"]].append(e)
        for t, es in sorted(byt.items()):
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
            if not t.startswith("AHU-"):
                continue
            d = num(es[0]["duty_as_written"])
            sch = None
            for k in tag_keys(t):
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
        # outdoor-air unit airflow equals the sum of what its AHUs take
        taken: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"drawn": 0.0, "wb": 0.0, "ahus": [], "sheet": None, "box": None})
        for s in i.working:
            ks = [k for k in tag_keys(s["outdoor_air"].get("source") or "") if k.startswith("PAHU:")]
            for ln in s["outdoor_air"]["lines"]:
                for k in ks:
                    t = taken[k]
                    t["drawn"] += ln["value_L_s"] or 0
                    t["ahus"].append(ln["to"])
                    t["sheet"] = t["sheet"] or s["sheet"]
                    t["box"] = t["box"] or ln.get("box")
        for k, t in sorted(taken.items()):
            duty = [num(e["duty_as_written"]) for e in i.equip if e["issue_set"] == "working" and e["type"] == "PAHU"
                    and k in tag_keys(e["tag"])]
            duty = [d for d in duty if d]
            drawn_duty = (sum(duty) if duty else None)
            wbsum = 0.0
            for a in set(t["ahus"]):
                for u in self.units_by_key.get(next(iter(tag_keys(a)), ""), []):
                    m = self.i.master.get(u["tag"])
                    if m and m.get("AHU outdoor air = fraction × calculated airflow"):
                        wbsum += m["AHU outdoor air = fraction × calculated airflow"]
                        break
            cls, act, need = judge(drawn_duty, t["drawn"], False) if drawn_duty is not None else \
                ("not established", "Query: duty of the outdoor-air unit is not drawn", True)
            if cls in ("no change", "margin"):
                act, need = "No change", False
            self.consist.append(mk(key_only(k), "queries" if need else "airflows", "Consistency", "PAHU " + key_only(k),
                                   "Duty of the outdoor-air unit against the sum of the AHU outdoor air", "L/s",
                                   drawn_duty if drawn_duty is not None else "not drawn", round(t["drawn"], 1), cls, act, need,
                                   t["sheet"] or "", 1, t["box"], f"Working drawings; outdoor-air lines of {len(set(t['ahus']))} AHU tags",
                                   f"Sum of OA drawn on the AHU lines: {t['drawn']:,.0f} L/s. Sum of OA in the workbooks: {wbsum:,.0f} L/s."))
        # no room on two AHUs
        seen_rb = set()
        group_keys = [tag_keys(g["tag"]) for g in self.unit_by_tag.values() if g["is_group"]]
        for s in i.working:
            for rm in s["rooms"]:
                tg = frozenset().union(*[tag_keys(a["value"] if isinstance(a, dict) else a)
                                         for a in rm["ahu_tags_as_written"]]) if rm["ahu_tags_as_written"] else frozenset()
                shared = any(tg and tg <= gk for gk in group_keys)      # a declared AHU group shares its rooms
                mot = bool(tg & i.mot_keys) and any(u["mot"] for k in tg for u in self.units_by_key.get(k, []))
                if shared or mot:
                    continue
                if rm.get("system_count", 1) > 1 and rm["room_id"] not in seen_rb:
                    seen_rb.add(rm["room_id"])
                    self.consist.append(mk(s["system_id"].split(":")[1], "queries", "Consistency", rm["name"]["value"],
                                           "Room box on more than one system", "no.", rm["system_count"], 1, "differs",
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
                if not re.match(r"^(?:PAHU|AHU)-B-[A-Z0-9]+-(?:\d\d)?$", t):
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
                    self.consist.append(mk(t["tag"], "queries", "Consistency", t["tag"], "Tag on the overall schematic", "no.", 1, 0,
                                           "not in zone schedule", "Query: tag on the overall schematic is in no unit of the zone schedule",
                                           True, sh, 1, t["box"], f"Overall schematic {sh} p1",
                                           "The tag is in the schedule, the air-side sheets and the zone schedule nowhere. Check the tag form. The overall schematic gives no duty."))
        # units that no overall sheet shows
        missing: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for u in i.units:
            if not u["has_tag"] or u["is_group"] or u["type"] != "AHU" or u["mot"]:
                continue
            if u["akeys"] & set(found_keys):
                continue
            lvl = re.search(r"-B-(\d\d)-", u["tag"])
            sh = self.OVERALL[0] if (lvl and int(lvl.group(1)) >= 19) else self.OVERALL[1]
            missing[sh].append(u)
        for sh, us in sorted(missing.items()):
            for u in sorted(us, key=lambda x: x["tag"]):
                m = i.master.get(u["tag"]) or {}
                req = m.get("Calculated airflow")
                self.consist.append(mk(u["tag"], "new systems", "Consistency", u["tag"], "AHU of the zone schedule not on the overall schematic",
                                       "L/s", "not drawn", req if req else "not established", "missing on overall",
                                       "Mark up: add the AHU to the overall schematic (tabulated note)", True, sh, 1, None, u["source"],
                                       "The overall schematic gives tags and no duties. Draw the AHU at its served level."))
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
                    elif cls == "reduction":
                        act = "New drawing: show the scheduled duty; margin recorded"
                        need = False
                out.append(mk("App D", "airflows" if cls != "not established" else "queries", "Duty against workbook", t,
                              f"Storey {st}", "L/s", sched, round(wv, 1) if wv is not None else None, cls, act, need,
                              "App D", pg, bx, f"{src}; Equipment schedule (Corr 5) p{r['schedule_pages'] or '-'}; {wsrc}", note))
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
    def mot_review(self) -> None:
        i = self.i
        sched: Dict[str, Dict[str, Any]] = {}
        for r in read_csv(i.reg / "mot_ahu_schedule.csv"):
            for k in tag_keys(r["tag"]):
                cur = sched.get(k)
                if cur is None or str(r["issue"]) > str(cur["issue"]):
                    sched[k] = {"tag": r["tag"], "issue": r["issue"], "supply": num(r["supply_airflow_l_s"]),
                                "oa": num(r["outdoor_air_min_l_s"]), "page": r["page"]}
        seen = set()
        for e in i.equip:
            if e["issue_set"] != "mot_nsc" or e["type"] != "AHU":
                continue
            ks = tag_keys(e["tag"])
            d = num(e["duty_as_written"])
            sc = next((sched[k] for k in ks if k in sched), None)
            seen |= set(ks)
            if sc is None:
                res, sv = "tag not in the MOT schedule", None
            elif d is None:
                res, sv = "duty not drawn", sc["supply"]
            elif sc["supply"] is None:
                res, sv = "no scheduled value", None
            elif abs(d - sc["supply"]) <= max(INC_PCT * sc["supply"], INC_LPS) and abs(d - sc["supply"]) <= 0.5:
                res, sv = "same", sc["supply"]
            else:
                res, sv = ("same within the limit" if abs(d - sc["supply"]) <= max(INC_PCT * sc["supply"], INC_LPS)
                           else "differs"), sc["supply"]
            self.mot_rows.append({
                "sheet": e["sheet"], "tag": e["tag"], "drawn": d, "scheduled": sv,
                "diff": (sv - d) if (d is not None and sv is not None) else None,
                "result": res, "issue": sc["issue"] if sc else "", "page": e["page"],
                "box": box_s(e["box_page_view_duty"] or e["box_page_view_tag"]),
                "source": f"MOT sheet {e['sheet']} p{e['page']}; MOT AC equipment schedule {sc['issue'] if sc else '-'} p{sc['page'] if sc else '-'}",
                "note": "Reviewed and listed. Not marked up. Operating theatres keep their specified values."})
        for k, sc in sorted(sched.items()):
            if k in seen or not k.startswith(("AHU", "PAHU")):
                continue
            self.mot_rows.append({"sheet": "", "tag": sc["tag"], "drawn": None, "scheduled": sc["supply"], "diff": None,
                                  "result": "scheduled, on no MOT sheet", "issue": sc["issue"], "page": "", "box": "",
                                  "source": f"MOT AC equipment schedule {sc['issue']} p{sc['page']}",
                                  "note": "Reviewed and listed. Not marked up."})
        for r in read_csv(self.i.root / "out/qa/mot_tag_differences.csv"):
            self.mot_rows.append({"sheet": "", "tag": r["governing_tag"], "drawn": num(r["airflow_schematic_l_s"]),
                                  "scheduled": num(r["airflow_ac_schedule_l_s"]), "diff": None,
                                  "result": "tag difference: " + (r["difference_type"] or "")[:80], "issue": "", "page": "",
                                  "box": "", "source": "MOT tag cross-reference, row " + str(r["crossref_row"]),
                                  "note": (r["detail"] or "")[:300] + " Listed, not marked up."})
        # keep AHU rows with no scheduled counterpart or a different duty only for the count
        # ids

    def list_mot_sheet_rows(self) -> None:
        """Rows on a sheet titled MOT ACMV: listed, not marked up (D-072, D-074 provisional)."""
        for r in {id(x): x for x in self.rows}.values():
            if r["needs_id"] and r["sheet"] in self.i.mot_sheets:
                r["note"] = (r["note"] + " " if r["note"] else "") + "Without this rule the action would be: " + \
                    r["action"][0].lower() + r["action"][1:] + "."
                r["needs_id"] = False
                r["layer"] = "queries"
                r["action"] = "List only (MOT): sheet with the MOT ACMV title; no mark-up"

    # -- change IDs -------------------------------------------------------------------- #
    def assign_ids(self) -> None:
        counters: Counter = Counter()
        for r in self.rows:
            if not r["needs_id"] or r["change_id"]:
                continue
            m = re.search(r"IP-BH-([0-9A-Z]{5})", r["sheet"] or "")
            code = m.group(1) if m else ("APPD" if r["sheet"] == "App D" else "NEW")
            counters[code] += 1
            r["change_id"] = f"CH-{code}-{counters[code]:04d}"
        for r in self.rows:
            w = r.get("ref")
            if w is not None and w.get("change_id"):
                r["action"] += f" (change ID {w['change_id']})"
            mg = r.get("_merged")
            if mg:
                r["note"] = ((r["note"] + " ") if r["note"] else "") + "This drawn value is used by several rows with different new values: " + \
                    "; ".join(f"{x['item']}: {fmt(num(x['required']), 0)} L/s" for x in mg) + ". Confirm the one value."


# --------------------------------------------------------------------------- #
# Run, CSV and workbook output
# --------------------------------------------------------------------------- #
def run_review(root: Path = ROOT) -> Review:
    rv = Review(Inputs(root))
    rv.review_all()
    rv.dedupe_boxes()
    rv.merge_shared_boxes()
    rv.missing_global()
    rv.consistency()
    rv.appd_review()
    rv.mot_review()
    rv.list_mot_sheet_rows()
    rv.assign_ids()
    return rv


CHANGE_COLS = ["change_id", "sheet", "page", "bbox", "layer_type", "drawn", "required", "action", "source",
               "unit", "item", "quantity", "unit_of_measure", "class", "note"]


def _plain(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 2)
    return v


def write_csvs(rv: Review, out: Path) -> Tuple[int, int]:
    out.mkdir(parents=True, exist_ok=True)
    seen = set()
    n = 0
    with open(out / "change_register.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CHANGE_COLS)
        for r in rv.rows:
            if not r["needs_id"] or id(r) in seen:
                continue
            seen.add(id(r))
            w.writerow([r["change_id"], r["sheet"], r["page"], r["box"], r["layer"], _plain(r["drawn"]),
                        _plain(r["required"]), r["action"], r["source"], r["unit"], r["item"], r["quantity"],
                        r["uom"], r["cls"], r["note"]])
            n += 1
    m = 0
    seen2 = set()
    with open(out / "missing_systems.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["change_id", "system", "category", "what_is_needed", "required_value", "unit_of_measure",
                    "belongs_on_sheet", "page", "bbox", "source", "unit"])
        for r in rv.missing + getattr(rv, "untag_rows", []):
            if id(r) in seen2:
                continue
            seen2.add(id(r))
            w.writerow([r["change_id"], r["item"], r["quantity"], r["note"], _plain(r["required"]), r["uom"],
                        r["sheet"] or "NEW", r["page"], r["box"], r["source"], r["unit"]])
            m += 1
    return n, m


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
    mot_marked = [t for t, i in rv.unit_info.items() if i.get("mot") and any(r["needs_id"] for r in i["rows"])]
    one_test = sum(1 for r in {id(r): r for r in rv.rows}.values()
                   if r["cls"] == "increase" and "passes the" in str(r["note"]) and r["needs_id"])
    return {"units_reviewed": len(rv.unit_info),
            "units_with_zone_rooms_not_in_rows": bad_rooms,
            "box_keys_with_conflicting_actions": len(conflicts),
            "change_ids": len(ids), "change_ids_unique": len(ids) == len(set(ids)),
            "mot_theatre_units_with_markup": mot_marked,
            "pointer_rows_without_own_id": sum(1 for r in rv.rows if r["cls"] == "reviewed on another tab"),
            "increase_rows_passing_one_test_only": one_test,
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

    def room_rows(rows: List[Row]) -> List[List[Any]]:
        out = []
        for r in rows:
            out.append([r["layer"], r["row_type"], r["item"], r["quantity"], r["uom"], r["drawn"], r["required"],
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
    used = {"summary", "app d reconciliation", "mot differences", "missing systems", "consistency", "fcu no tag"}
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
               "Update rule: an increase of more than 5 % or 10 L/s is marked up. A reduction is marked up only where the room list is complete; "
               "otherwise the drawn value stays and the margin is recorded."]
        if u["mot"]:
            sub.append("MOT NSC unit: the specified values are kept. The rows are listed and are not marked up.")
        r0 = title(ws, u["tag"], sub)
        last = table(ws, r0, UNIT_HEAD, room_rows(info.get("rows", [])), UNIT_W, mono=UNIT_MONO, fmts=UNIT_FMT,
                     wrap=(2, 9, 10, 14, 15, 16), freeze_cols=3, int_uom=4)
        setup(ws, len(UNIT_HEAD), last, r0)
        ws.sheet_properties.tabColor = None

    # Summary
    ws = wb.create_sheet("Summary", 0)
    r = title(ws, "Schematic update register", [
        proj,
        f"Revision {REVISION}. {today}. Status: draft for checking.",
        "Review of every AHU, PAHU and FCU group of the zone schedule against the working drawings."])
    def section(txt):
        nonlocal r
        ws.cell(r, 1, txt).font = F(size=12, bold=True)
        r += 1
    section("How to read this book")
    for t in ["One tab for each unit. Each row gives the drawn value, the required value, the difference, the action and the change ID.",
              "A change ID links the row to the mark-up on the drawing and to the change list.",
              "Required values come from the department workbooks. Rooms come from the zone schedule.",
              "Where the zone link of a room is not known, the row says not established. It does not say add or remove.",
              "Fan pressure cannot be taken from a schematic. The drawn value is kept.",
              "VAV boxes and venturi valves: the VAV and Venturi Valves tabs of the workbooks have no rows. The sheets draw a VAV on the supply and return branches of many rooms, "
              "and these cannot be compared with the workbooks. They are not compared.",
              "Required airflow of a room box is the average of the workbook rooms linked to the box. Where the linked rooms differ by more than 25 % "
              "and the answer depends on which room is used, the row is a query. The average is to be checked by a person.",
              "An increase is marked up when it is more than 5 % of the drawn value or more than 10 L/s. A row that passes only one of the two tests says so "
              "in its note. The reading of the update rule is to be confirmed.",
              "A drawn box is reviewed once. Other tabs that show the same box carry a pointer row and no change ID of their own.",
              "Sheets of the MOT NSC and the theatre units of the working sheets 07050 to 07052 are listed. They are not marked up."]:
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
         chk["increase_rows_passing_one_test_only"]]], [60, 14], fmts={1: numfmt}, freeze_cols=None)
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
    rv = run_review(ROOT)
    OUT.mkdir(parents=True, exist_ok=True)
    n, m = write_csvs(rv, OUT)
    if not a.no_xlsx:
        write_xlsx(rv, OUT / "schematic_update_register.xlsx")
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
