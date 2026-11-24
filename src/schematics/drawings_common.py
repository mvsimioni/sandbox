"""Shared reads for the drawing generators (mark-ups and new drawings), council C4 build lines 7, 19, 23-27.

* ``Scope``   one predicate for list-only units and sheets, read from out/schematics/scope.csv (build line 7).
              When the file is missing, the list-only set is derived from out/registers/mot_ahu_schedule.csv.
* ``Ledger``  the entity ledger out/schematics/entity_ledger.csv: where a tag is drawn on a working sheet.
              Nobody builds a second tag index: the generators only look tags up here.
* ``clean_text`` the D-015 word replacements for text that the generators write (build line 27).
* ``NameMap`` room names printed as the DPA room name with the plan abbreviation in brackets (build line 27).
* ``criteria_of`` room criteria (temperature, humidity, pressure, filter, air class) of a room, from its workbook.
"""
from __future__ import annotations

import csv
import glob
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "out" / "schematics"
REG = ROOT / "out" / "registers"
import os
SCOPE_CSV = Path(os.environ.get("SCHEMATICS_SCOPE") or OUT / "scope.csv")           # env names: for tests on a scratch copy
LEDGER_CSV = Path(os.environ.get("SCHEMATICS_LEDGER") or OUT / "entity_ledger.csv")
MOT_SCHEDULE = REG / "mot_ahu_schedule.csv"
csv.field_size_limit(10 ** 9)

TAG_RE = re.compile(r"(?:PAHU|AHU|FCU|CRAC|OAF|EAF|TEAF|IEAF|RAF|TEF|HPF|SAF|FAF)-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?:/[A-Z0-9]+)*")


def read_csv(p: Path) -> List[Dict[str, str]]:
    if not Path(p).exists():
        return []
    with open(p, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def tag_keys(text: str) -> frozenset:
    """'TYPE:key' for every tag in a text, A/B letters removed (the same rule as the review: 'AHU-B-05-05CORE-01A/B'
    gives {'AHU:B-05-05CORE-01'}; a pair of numbers 'AHU-B-07-04ICU-05/06' gives two keys)."""
    out = set()
    for t in TAG_RE.findall(str(text or "").upper()):
        pre, _, rest = t.partition("-")
        pair = re.search(r"^(.*-)(\d+)/(\d+)[A-Z]?$", rest)
        if pair:
            for n in (pair.group(2), pair.group(3)):
                out.add(f"{pre}:{pair.group(1)}{n}")
            continue
        rest = re.sub(r"(?<=\d)(?:/[A-Z])+$|(?<=\d)[A-Z](?:/[A-Z])*$", "", rest)
        out.add(f"{pre}:{rest}")
    return frozenset(out)


def core_of(tag: str) -> str:
    """Name core of a tag without its type prefix: 'AHU:B-05-05CORE-01' -> '05CORE-01'."""
    k = sorted(tag_keys(tag))
    if not k:
        return ""
    r = k[0].split(":", 1)[1]
    parts = r.split("-")
    return "-".join(parts[-2:]) if len(parts) >= 2 else r


def sheet_id(s: str) -> str:
    m = re.search(r"IP-BH-[0-9A-Z]{5}", str(s or ""))
    return m.group(0) if m else str(s or "")


def _truthy(v: Any) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "y", "list-only", "list only", "listed")


# --------------------------------------------------------------------------- #
# Scope: one predicate for list-only units and sheets
# --------------------------------------------------------------------------- #
class Scope:
    def __init__(self):
        self.rows = read_csv(SCOPE_CSV)
        self.units: Set[str] = set()      # tag keys of list-only units and fans
        self.sheets: Set[str] = set()     # sheets where every AHU is list-only
        self.from_file = bool(self.rows)
        if self.rows:
            head = list(self.rows[0].keys())
            lo = next((h for h in head if re.sub(r"[^a-z]", "", h.lower()) in ("listonly", "list", "mot", "islistonly", "listonlyunit")), None)
            for r in self.rows:
                flag = _truthy(r.get(lo)) if lo else False
                if not flag:
                    continue
                kind = (r.get("kind") or r.get("level") or r.get("row_type") or r.get("type") or "").lower()
                tag = r.get("unit") or r.get("tag") or r.get("key") or ""
                sh = r.get("sheet") or ""
                if kind.startswith("sheet") or (sh and not tag):
                    self.sheets.add(sheet_id(sh or tag))
                else:
                    self.units |= set(tag_keys(tag))
                    self.units.add(re.sub(r"[^A-Z0-9]", "", tag.upper()))
        else:
            for r in read_csv(MOT_SCHEDULE):
                self.units |= set(tag_keys(r.get("tag", "")))
                self.units.add(re.sub(r"[^A-Z0-9]", "", r.get("tag", "").upper()))

    def unit_list_only(self, text: str) -> bool:
        """True when a tag (or a list of tags) names a list-only unit or fan."""
        ks = tag_keys(text)
        if ks and all(k in self.units for k in ks):
            return True
        return re.sub(r"[^A-Z0-9]", "", str(text or "").upper()) in self.units

    def sheet_list_only(self, sheet: str) -> bool:
        return sheet_id(sheet) in self.sheets


# --------------------------------------------------------------------------- #
# Ledger: where is a tag drawn
# --------------------------------------------------------------------------- #
class Ledger:
    def __init__(self):
        self.rows = read_csv(LEDGER_CSV)
        self.from_file = bool(self.rows)
        self.by_key: Dict[str, Set[str]] = defaultdict(set)     # tag key -> working sheets
        self.by_id: Dict[str, Dict[str, str]] = {}
        for i, r in enumerate(self.rows):
            sh = sheet_id(r.get("sheet", ""))
            kind = (r.get("kind") or "").lower()
            if kind.startswith("note") or (r.get("sheet_role") or "working") != "working" or kind.startswith("overall"):
                continue        # a general note is not a drawn path; the overall schematic is not a working sheet
            txt = " ".join(str(r.get(c, "")) for c in ("text", "text_as_written", "tag", "tag_keys"))
            for k in tag_keys(txt):
                self.by_key[k].add(sh)
            for k in re.findall(r"[A-Z]+:[A-Z0-9][A-Z0-9\-]*", str(r.get("tag_keys", ""))):
                self.by_key[k].add(sh)
            rid = r.get("entity_id") or r.get("id") or r.get("ledger_id") or str(i)
            self.by_id[rid] = r

    def homes(self, tag_text: str) -> List[str]:
        """Working sheets that draw any variant of the tag (empty: nothing drawn)."""
        out: Set[str] = set()
        for k in tag_keys(tag_text):
            out |= self.by_key.get(k, set())
        if not out:
            # correction pass (Opus check item 2): a fan of the unit (the tag core is in the fan tag) drawn on a working sheet
            # means the unit is drawn there; a new drawing does not draw it again.
            for k in tag_keys(tag_text):
                if k.startswith("AHU:"):
                    core = k.split(":", 1)[1]
                    for pre in ("RAF", "EAF", "TEAF", "IEAF"):
                        out |= self.by_key.get(f"{pre}:{core}", set())
        return sorted(out)

    def box_of(self, ledger_id: str) -> Optional[List[float]]:
        r = self.by_id.get(ledger_id)
        if not r:
            return None
        try:
            v = json.loads(r.get("bbox", ""))
            return [float(x) for x in v] if v and len(v) == 4 else None
        except (ValueError, TypeError):
            return None


# --------------------------------------------------------------------------- #
# Text: D-015 words
# --------------------------------------------------------------------------- #
_D015 = [
    (re.compile(r"\bschematic_update_register\b"), "schematic_update_register"),     # file name kept (D-086)
    (re.compile(r"\bdrawing_register\b"), "drawing_register"),
    (re.compile(r"\bchange register\b", re.I), "change list"),
    (re.compile(r"\bdrawing register\b", re.I), "list of drawings"),
    (re.compile(r"\bupdate register\b", re.I), "update list"),
    (re.compile(r"\bregisters\b", re.I), "lists"),
    (re.compile(r"\bregister\b", re.I), "list"),
]


ZERO_WIDTH = re.compile("[\u200b-\u200f\u2060\ufeff]")


def clean_text(text: str) -> str:
    """Replace the word banned by D-015 ('register') in text that is printed, by 'change list', 'list of drawings',
    'update list' or 'list'.  Words kept as written: the file names of D-086."""
    keep = {}

    def hide(m):
        k = "\x00%d\x00" % len(keep)
        keep[k] = m.group(0)
        return k
    t = re.sub(r"schematic_update_register|drawing_register", hide, ZERO_WIDTH.sub("", str(text)))
    for rx, rep in _D015[2:]:
        t = rx.sub(lambda m, rep=rep: rep.capitalize() if m.group(0)[0].isupper() else rep, t)
    for k, v in keep.items():
        t = t.replace(k, v)
    return t


# --------------------------------------------------------------------------- #
# Room names: DPA room name with the plan abbreviation in brackets
# --------------------------------------------------------------------------- #
class NameMap:
    """Scripted name map.  A plan name is printed as 'DPA name (plan name)' when the DPA list holds the same words
    once abbreviations are expanded; a name the map does not hold is printed as written and listed in ``missed``."""

    def __init__(self):
        self.dpa: Dict[str, str] = {}
        for r in read_csv(REG / "dpa_rooms.csv"):
            n = ZERO_WIDTH.sub("", (r.get("room_name") or r.get("name") or "")).strip()
            if n:
                self.dpa.setdefault(self._k(n), n)
        self.abbr: Dict[str, Set[str]] = defaultdict(set)
        for r in read_csv(REG / "zoning_abbreviations.csv"):
            self.abbr[r["abbreviation"].upper()].add(r["expansion"].upper())
        self.missed: Dict[str, int] = defaultdict(int)

    @staticmethod
    def _k(s: str) -> str:
        t = re.sub(r"[^A-Z0-9 ]+", " ", str(s).upper().replace("&", " AND "))
        return re.sub(r"\s+", " ", t).strip()

    def _expand(self, name: str) -> List[str]:
        toks = self._k(name).split()
        out = [""]
        for t in toks:
            ex = self.abbr.get(t)
            opts = sorted(ex) if ex and len(ex) == 1 else [t]
            out = [(o + " " + x).strip() for o in out for x in opts]
        return out[:4]

    def show(self, plan_name: str, dpa_name: str = "") -> str:
        plan_name = re.sub(r"\s+", " ", ZERO_WIDTH.sub("", str(plan_name or ""))).strip()
        if not plan_name:
            return plan_name
        d = dpa_name or next((self.dpa[k] for k in [self._k(plan_name)] + self._expand(plan_name) if k in self.dpa), "")
        if d:
            if self._k(d) == self._k(plan_name):
                return d
            return "%s (%s)" % (d, plan_name)
        self.missed[plan_name] += 1
        return plan_name

    def write_missed(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["name_as_written", "uses", "note"])
            for k, n in sorted(self.missed.items()):
                w.writerow([k, n, "not in the name map: printed as written"])


# --------------------------------------------------------------------------- #
# Room criteria from the department workbook (Criteria tab), with the cell addresses
# --------------------------------------------------------------------------- #
_WB: Dict[str, Dict[str, Any]] = {}


def _wb_tab(dept: str, tab: str):
    key = dept + "|" + tab
    if key in _WB:
        return _WB[key]
    import openpyxl
    hits = glob.glob(str(ROOT / "out/loads/workbooks") + "/AIGH_C2_D%s_*.xlsx" % dept)
    res = None
    if hits:
        wb = openpyxl.load_workbook(hits[0], data_only=True, read_only=True)
        if tab in wb.sheetnames:
            rows = list(wb[tab].iter_rows(values_only=True))
            if len(rows) > 5:
                res = ({h: i for i, h in enumerate(rows[4]) if h}, rows)
        wb.close()
    _WB[key] = res
    return res


def criteria_of(dept: str, room: str) -> Dict[str, Any]:
    """Temperature, relative humidity, pressure, filter and air class of a room with their workbook cells.
    A missing value is None (printed 'not given')."""
    from openpyxl.utils import get_column_letter
    res = {k: (None, "") for k in ("temp", "rh", "pressure", "filter", "air_class", "supply", "return", "exhaust")}
    t = _wb_tab(dept, "Criteria")
    if t:
        head, rows = t
        for n, r in enumerate(rows):
            if n >= 6 and r and r[0] is not None and str(r[0]) == str(room):
                def g(*names, zero_is_blank=False):
                    for nm in names:
                        i = head.get(nm)
                        if i is not None and r[i] not in (None, "") and not (zero_is_blank and r[i] == 0):
                            return r[i], "%s%d" % (get_column_letter(i + 1), n + 1)
                    return None, ""
                res["temp"] = g("Temperature set point", "Design room temperature", "Temperature max", zero_is_blank=True)
                res["rh"] = g("Humidity max", zero_is_blank=True)      # the design-humidity fraction (0.6) is not a set value
                res["filter"] = g("Filter")
                res["air_class"] = g("Air class")
                break
    p = _wb_tab(dept, "Pressure")
    if p:
        head, rows = p
        i = head.get("Set pressure")
        for n, r in enumerate(rows):
            if n >= 6 and r and r[0] is not None and str(r[0]) == str(room) and i is not None and r[i] is not None:
                res["pressure"] = (r[i], "%s%d" % (get_column_letter(i + 1), n + 1))
                break
    a = _wb_tab(dept, "Airflow")
    if a:
        head, rows = a
        for n, r in enumerate(rows):
            if n >= 6 and r and r[0] is not None and str(r[0]) == str(room):
                for k, col in (("supply", "Final supply"), ("return", "Return"), ("exhaust", "Exhaust")):
                    i = head.get(col)
                    if i is not None and r[i] is not None:
                        res[k] = (r[i], "%s%d" % (get_column_letter(i + 1), n + 1))
                break
    return res
