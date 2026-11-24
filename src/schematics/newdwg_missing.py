"""New air-side schematics for the systems that no working sheet shows (brief 9.4, decision D-073).

Covers, per storey (LB3, B3, B2, B1, L01 to L07):
  * every unit of missing_systems.csv with the category "Unit with no schematic" that Appendix D does not
    already draw (the Appendix D units are drawn by newdwg.py, IP-BH-050x9 and neighbours),
  * the FCU and the outdoor-air connection of every room that the outdoor-air rule separates,
  * the outdoor-air units that the working sheets name and no sheet draws.

The drawing is built once as DXF (ezdxf) and the PDF is drawn from the same DXF primitives (newdwg.render_pdf).
Style, title block, legend and symbols are those of the approved pilot (newdwg.py).

    python -m src.schematics.newdwg_missing run
    python -m src.schematics.newdwg_missing check
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import re
import sys
from collections import OrderedDict, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import openpyxl
import shapely.wkt as swkt
from openpyxl.utils import get_column_letter

from . import newdwg as nd
from . import review as rv
from . import drawings_common as dc
from .newdwg import (H, HS, HT, LP, SHEET_H, SHEET_W, STRIP_X0, X0_PAGE, X_MAX, Y_MIN, Y_TOP, SHELF_GAP, Sheet, Value,
                     fnum, tw, wrap, insert, arrow_at)

ROOT = nd.ROOT
REG = nd.REG
OUT = nd.OUT
NEWDIR = nd.NEWDIR
MISSING = OUT / "missing_systems.csv"
csv.field_size_limit(10 ** 9)

STOREYS = ["LB3", "B3", "B2", "B1", "L01", "L02", "L03", "L04", "L05", "L06", "L07"]
LEVEL_NAME = {"LB3": "LOWER BASEMENT 3", "B3": "BASEMENT 3", "B2": "BASEMENT 2", "B1": "BASEMENT 1", "L01": "1ST STOREY",
              "L02": "2ND STOREY", "L03": "3RD STOREY", "L04": "4TH STOREY", "L05": "5TH STOREY", "L06": "6TH STOREY",
              "L07": "7TH STOREY"}
BW, PITCH = nd.BW, nd.PITCH


def code_level(code: str) -> str:
    """Level name from a two-character level code of a tag (01, 02, B1, B2, ...)."""
    c = (code or "").upper()
    if re.fullmatch(r"\d\d", c):
        return LEVEL_NAME.get("L" + c, "LEVEL " + c)
    return LEVEL_NAME.get(c, "")


def parse_tag_levels(tag: str) -> Tuple[str, str]:
    """(plant level, served level) from the tag: AHU-B-<plant>-<served><name>-<nn>.  '' when the tag does not give it."""
    m = re.match(r"^(?:AHU|FCU|PAHU)-B-(B\d|\d\d)-(B\d|\d\d)?", tag)
    if not m:
        return "", ""
    return code_level(m.group(1)), code_level(m.group(2) or "")


def ukey(tag: str) -> str:
    """Key of a unit or a fan of the unit: type prefix, pair letter and spaces removed."""
    t = str(tag).upper().strip()
    t = re.sub(r"^(OAF/EAF|AHU|FCU|PAHU|CRAC|OAF|EAF|TEAF|TEF|RAF|SAF|FAF)[- ]\s*", "", t)
    t = t.split("/")[0]
    t = re.sub(r"(?<=\d)[A-Z]$", "", t)
    return re.sub(r"[^A-Z0-9]", "", t)


def fnum1(v: Any) -> str:
    """A number as text: integer when whole, else at most one decimal."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    r = round(f, 1)
    return str(int(r)) if r == int(r) else "%g" % r


# --------------------------------------------------------------------------- #
# Workbook access with cell addresses
# --------------------------------------------------------------------------- #
class Book:
    def __init__(self, path: Path):
        self.path = path
        self._wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        self._tabs: Dict[str, Tuple[Dict[str, int], List[Tuple[Any, ...]]]] = {}

    def tab(self, n: str) -> Tuple[Dict[str, int], List[Tuple[Any, ...]]]:
        if n not in self._tabs:
            rows = list(self._wb[n].iter_rows(values_only=True))
            head = {h: i for i, h in enumerate(rows[4]) if h}
            self._tabs[n] = (head, rows)
        return self._tabs[n]

    def rowfind(self, tab: str, key: str, col: int = 0) -> Optional[int]:
        head, rows = self.tab(tab)
        for n, r in enumerate(rows):
            if n >= 6 and r[col] is not None and str(r[col]) == key:
                return n + 1
        return None

    def get(self, tab: str, row: int, col: str) -> Tuple[Any, str]:
        head, rows = self.tab(tab)
        i = head.get(col)
        if i is None:
            return None, ""
        return rows[row - 1][i], "%s%d" % (get_column_letter(i + 1), row)


_BOOKS: Dict[str, Book] = {}


def book(dept: str) -> Book:
    if dept not in _BOOKS:
        p = glob.glob(str(ROOT / "out/loads/workbooks") + "/AIGH_C2_D%s_*.xlsx" % dept)[0]
        _BOOKS[dept] = Book(Path(p))
    return _BOOKS[dept]


def master_cells() -> Tuple[Dict[str, int], Dict[str, int], List[Tuple[Any, ...]]]:
    wb = openpyxl.load_workbook(ROOT / "out/loads/workbooks/AHU_master_summary.xlsx", data_only=True, read_only=True)
    rows = list(wb["AHU Master"].iter_rows(values_only=True))
    head = {h: i for i, h in enumerate(rows[4]) if h}
    return head, {str(r[0]): n + 1 for n, r in enumerate(rows) if n >= 6 and r[0]}, rows


# --------------------------------------------------------------------------- #
# Data: units, separated rooms, outdoor-air units
# --------------------------------------------------------------------------- #
def appd_covered_tags() -> set:
    """Tags that the Appendix D drawings carry (the register of the model, and the coverage files of newdwg)."""
    from . import model
    tags = set()
    for s in model.build_appd()["systems"]:
        for e in s["equipment"]:
            tags.add(e["tag_as_written"])
    return tags


def load_zone_rooms() -> Dict[str, List[Dict[str, Any]]]:
    """Rooms of the zone schedule by unit class, with the row of the storey tab of zone_schedule.xlsx."""
    wb = openpyxl.load_workbook(ROOT / "out/zoning/zone_schedule.xlsx", data_only=True, read_only=True)
    out: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for st in STOREYS:
        rows = list(wb[st].iter_rows(values_only=True))
        head = rows[0]
        for n, r in enumerate(rows[1:], start=2):
            if not r[0]:
                continue
            d = dict(zip(head, r))
            out[str(d["System / class"])].append(dict(room_id=d["Room ID"], name=d["Room name"], area=d["Area (m²)"],
                                                      conf=d["Confidence"], storey=st, row=n, tag=d["Room tag"]))
    return out


class Data:
    def __init__(self):
        self.inp = rv.Inputs()
        self.mrows = list(csv.DictReader(open(MISSING, encoding="utf-8")))
        self.zrooms = load_zone_rooms()
        self.mhead, self.mrow, self.mrows_all = master_cells()
        self.appd_tags = appd_covered_tags()
        self.sched_rows = list(csv.DictReader(open(REG / "ahu_schedule.csv", encoding="utf-8")))
        self.fans = list(csv.DictReader(open(REG / "equipment_schedule_fans.csv", encoding="utf-8")))
        self.air_sched = list(csv.DictReader(open(REG / "equipment_schedule_air_handling.csv", encoding="utf-8")))
        self.sys = list(csv.DictReader(open(REG / "schematic_systems.csv", encoding="utf-8")))
        self.links = defaultdict(list)
        for l in self.inp.links:
            self.links[l["unit_class"]].append(l)
        self.units_zs = {u["tag"]: u for u in self.inp.units}
        wb = openpyxl.load_workbook(ROOT / "out/zoning/zone_schedule.xlsx", data_only=True, read_only=True)
        rows = list(wb["AHU summary"].iter_rows(values_only=True))
        self.zs_head = {h: i for i, h in enumerate(rows[0])}
        self.zs_rows = {str(r[0]): (n + 1, r) for n, r in enumerate(rows) if n >= 1 and r[0]}

    # --- the lists of the job.  Working sheet wins (D-079): a tag that the ledger finds on a working sheet is not
    #     drawn on a new drawing; it is listed in withdrawn_rows() with the sheet that draws it.
    def link_count(self) -> Dict[Tuple[str, str], int]:
        """Units a room is linked to in the zoning link list: 1 is a one-to-one link."""
        if not hasattr(self, "_lc"):
            seen: Dict[Tuple[str, str], set] = defaultdict(set)
            for cls, ls in self.links.items():
                for l in ls:
                    seen[(l["dept"], l["dpa_room_id"])].add(cls)
            self._lc = {k: len(v) for k, v in seen.items()}
        return self._lc

    @property
    def led(self) -> dc.Ledger:
        if not hasattr(self, "_led"):
            self._led = dc.Ledger()
        return self._led

    @property
    def scope(self) -> dc.Scope:
        if not hasattr(self, "_scope"):
            self._scope = dc.Scope()
        return self._scope

    def homes(self, tag: str) -> List[str]:
        return self.led.homes(tag)

    def xref_rows(self) -> List[Dict[str, Any]]:
        """Units that a working sheet draws and that an earlier build drew on a new drawing, or that the list of missing
        systems still names: one cross-reference line each ('shown on IP-BH-nnnnn'), on a new drawing of the storey."""
        out: Dict[str, Dict[str, Any]] = {}
        for w in self.withdrawn_rows():
            out[w["item"]] = dict(item=w["item"], homes=w["homes"], storey=storey_of_tag(self, w["item"]))
        for r in dc.read_csv(PREV_ITEMS):
            if r["kind"] in ("unit", "outdoor-air unit") and r["item"] not in out:
                h = self.homes(r["item"])
                if h and not self.scope.unit_list_only(r["item"]):
                    out[r["item"]] = dict(item=r["item"], homes=h, storey=r["storey"])
        return sorted(out.values(), key=lambda x: x["item"])

    def withdrawn_rows(self) -> List[Dict[str, str]]:
        out = []
        for r in self.mrows:
            if r["category"] in ("Unit with no schematic",) or r["category"].startswith("Outdoor-air unit referred to"):
                h = self.homes(r["system"])
                if h:
                    out.append(dict(item=r["system"], change_id=r["change_id"], category=r["category"], homes=h))
        return out

    def unit_rows(self) -> List[Dict[str, str]]:
        return [r for r in self.mrows if r["category"] == "Unit with no schematic" and r["system"] not in self.appd_tags
                and not self.homes(r["system"]) and not self.scope.unit_list_only(r["system"])]

    def sep_rows(self) -> List[Dict[str, str]]:
        """Separated rooms (D-076): only a row that the review keeps as an instruction (build lines 11 and 15)."""
        if not hasattr(self, "_instr_ids"):
            self._instr_ids = {r["change_id"] for r in dc.read_csv(OUT / "change_register.csv") if r["action"].startswith(("Mark up", "New system", "New drawing"))}
        return [r for r in self.mrows if r["category"].startswith("Separated room") and r["change_id"] in self._instr_ids]

    def pahu_rows(self) -> List[Dict[str, str]]:
        return [r for r in self.mrows if r["category"].startswith("Outdoor-air unit referred to") and not self.homes(r["system"])]

    # --- schedule row of a unit (equipment schedule, air handling)
    def schedule_row(self, tag: str) -> Optional[Dict[str, str]]:
        k = ukey(tag)
        hit = [r for r in self.sched_rows if ukey(r["tag_normalised"] or r["tag_as_written"]) == k]
        return hit[0] if hit else None

    def fans_of(self, tag: str) -> List[Dict[str, str]]:
        k = ukey(tag)
        out, seen = [], set()
        for f in self.fans:
            t = f["tag_as_written"]
            if re.match(r"^(OAF|EAF|TEAF|TEF|RAF|SAF|FAF)", t.upper()) and ukey(t) == k and t not in seen:
                seen.add(t)
                out.append(f)
        return out


PREV_ITEMS = OUT / "new_drawing_prev_items.csv"       # items that an earlier build drew on a new drawing (kept)


def storey_of_tag(d: Data, tag: str) -> str:
    zs = d.zs_rows.get(tag)
    if zs:
        first = str(zs[1][d.zs_head["Storeys served"]] or "").split(",")[0].strip()
        if first in STOREYS:
            return first
    pl, sv = parse_tag_levels(tag)
    return sv if sv in STOREYS else (pl if pl in STOREYS else "B2")


def storey_of_unit(d: Data, tag: str, row: Dict[str, str]) -> str:
    zs = d.zs_rows.get(tag)
    if zs:
        v = str(zs[1][d.zs_head["Storeys served"]] or "")
        first = v.split(",")[0].strip()
        if first in STOREYS:
            return first
    m = re.search(r"new sheet of the (\w+) air-side series", row["what_is_needed"])
    if m:
        return {"B3": "B3", "B2": "B2", "L01": "L01", "L02": "L02"}.get(m.group(1), m.group(1))
    return "B2"


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
def expand_tag(tag: str) -> List[str]:
    """Individual tags of a written tag with '/' (01A/B, 01/02/03)."""
    parts = tag.split("/")
    first = parts[0]
    out = [first]
    for p in parts[1:]:
        out.append(first[: len(first) - len(p)] + p)
    return out


def nk(tag: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(tag).upper())


def src_wb(dept: str, tab: str, cell: str) -> str:
    return "Workbook D%s tab %s cell %s" % (dept, tab, cell)


def sched_air_rows(d: Data, tag: str) -> List[Dict[str, str]]:
    exp = expand_tag(tag)
    ks = {ukey(t) for t in exp}
    typ = tag.split("-")[0]
    hit = []
    for r in d.air_sched:
        t = r["tag_as_written"]
        if t.split("-")[0] == typ and ukey(t) in ks and ("/" in t) == ("/" in tag):
            hit.append(r)
    return hit


def unit_zone_cells(d: Data, tag: str) -> Dict[str, Tuple[Any, str]]:
    """Zone schedule AHU summary values of the unit with their cell address."""
    n, r = d.zs_rows[tag]
    out = {}
    for h, i in d.zs_head.items():
        out[h] = (r[i], "%s%d" % (get_column_letter(i + 1), n))
    return out


def unit_master_cells(d: Data, tag: str) -> Dict[str, Tuple[Any, str]]:
    n = d.mrow.get(tag)
    if not n:
        return {}
    r = d.mrows_all[n - 1]
    return {h: (r[i], "%s%d" % (get_column_letter(i + 1), n)) for h, i in d.mhead.items()}


def build_unit(d: Data, row: Dict[str, str]) -> Dict[str, Any]:
    tag = row["system"]
    chg = row["change_id"]
    typ = tag.split("-")[0]
    zc = unit_zone_cells(d, tag)
    mc = unit_master_cells(d, tag)
    u: Dict[str, Any] = dict(tag=tag, type=typ, chg=chg, mrow=row, zone=zc, master=mc, kind="unit")
    # rooms of the unit from the room links and the workbooks
    rooms: List[Dict[str, Any]] = []
    first_storey = ""
    for l in d.links.get(tag, []):
        dept, rno = l["dept"], l["dpa_room_id"]
        b = book(dept)
        ar = b.rowfind("Airflow", rno)
        if ar is None:
            continue
        rec = dict(dept=dept, room=rno, row=ar)
        for k, col in (("name", "Room name"), ("storey", "Storey"), ("area", "Area"), ("sa", "Final supply"), ("ra", "Return"),
                       ("ea", "Exhaust"), ("oa", "Outdoor air"), ("term", "Terminal tag"), ("fan", "Exhaust fan tag")):
            rec[k], rec[k + "_cell"] = b.get("Airflow", ar, col)
        pr = b.rowfind("Pressure", rno)
        rec["press"], rec["press_cell"] = (b.get("Pressure", pr, "Set pressure") if pr else (None, ""))
        fr = b.rowfind("FCU", rno, 1)
        if fr:
            rec["oasrc"], rec["oasrc_cell"] = b.get("FCU", fr, "Outdoor-air source (PAHU or fan)")
            rec["oaby"], _ = b.get("FCU", fr, "Source chosen by")
        rooms.append(rec)
        first_storey = first_storey or str(rec["storey"] or "")
    # group the rooms that are alike (typical rooms)
    groups: OrderedDict = OrderedDict()
    for r in rooms:
        key = (r["dept"], str(r["name"]), r["area"], r["sa"], r["ra"], r["ea"], r["oa"], r["press"])
        groups.setdefault(key, []).append(r)
    u["room_groups"] = [dict(first=g[0], members=g) for g in groups.values()]
    # D-076 / build line 15: a room box is drawn only for a room with a one-to-one zoning link to this unit, and only where the
    # unit passes the gate (zone-schedule cover 0.90 to 1.10, room list complete)
    n_links = d.link_count()
    keep = []
    for g in u["room_groups"]:
        mem = [m for m in g["members"] if n_links.get((m["dept"], str(m["room"])), 1) == 1]
        if mem:
            keep.append(dict(first=mem[0], members=mem))
    u["room_groups"] = keep
    u["gate_text"] = ""
    try:
        cover = float(row.get("cover") or "")
    except ValueError:
        cover = None
    counted, zoned = row.get("rooms_counted") or "", row.get("rooms_zoned") or ""
    complete = counted != "" and zoned != "" and counted == zoned
    if cover is None or not (0.90 <= cover <= 1.10) or not complete:
        u["gate_text"] = ("ROOM BOXES ARE NOT DRAWN: THE ROOM LIST OF THIS UNIT IS NOT ESTABLISHED (%s OF THE ZONED AREA, %s OF %s ROOMS). "
                          "THE ZONE TABLE LISTS THE ROOMS." % (("COUNTED AREA IS %.2f" % cover) if cover is not None else "COVER NOT KNOWN",
                                                              counted or "NO", zoned or "NO"))
        u["room_groups"] = []
    u["n_rooms_wb"] = len(rooms)
    u["storey"] = storey_of_unit(d, tag, row)
    if u["storey"] not in STOREYS or (zc["Storeys served"][0] in (None, "no information") and first_storey in STOREYS):
        u["storey"] = first_storey if first_storey in STOREYS else u["storey"]
    m = re.search(r"new sheet of the (\w+) air-side series", row["what_is_needed"])
    if zc["Storeys served"][0] in (None, "no information") and not first_storey and m and m.group(1) in STOREYS:
        u["storey"] = m.group(1)
    u["zone_rooms"] = d.zrooms.get(tag, [])
    u["sched"] = sched_air_rows(d, tag)
    u["fans"] = d.fans_of(tag)
    pl, sv = parse_tag_levels(tag)
    u["plant"], u["served"] = pl, sv
    u["oa_src"] = next((r for r in rooms if r.get("oasrc")), None)
    return u


# --------------------------------------------------------------------------- #
# Value formats (the drawing text is prefix + value + suffix; the check reads it back with the same table)
# --------------------------------------------------------------------------- #
FMT = {
    "change_id": ("CHG ", ""),
    "area_m2": ("", " m2"),
    "unit_type": ("TYPE ", ""),
    "tender_supply_L_s": ("TENDER SUPPLY AIRFLOW ", " L/s"),
    "workbook_supply_L_s": ("WORKBOOK SUPPLY AIRFLOW ", " L/s"),
    "tender_outdoor_L_s": ("TENDER OUTDOOR AIRFLOW ", " L/s"),
    "workbook_outdoor_L_s": ("WORKBOOK OUTDOOR AIRFLOW ", " L/s"),
    "sched_supply_L_s": ("SUPPLY ", " L/s"),
    "sched_outdoor_L_s": ("OUTDOOR MIN ", " L/s"),
    "sched_esp_Pa": ("EXTERNAL RESISTANCE ", " Pa"),
    "sched_cooling_kW": ("COOLING ", " kW"),
    "zone_rooms": ("ROOMS IN THE ZONE SCHEDULE ", ""),
    "zone_area_m2": ("ZONED AREA ", " m2"),
    "wb_rooms": ("ROOMS COUNTED IN THE WORKBOOKS ", ""),
    "fan_airflow_L_s": ("MIN AIRFLOW ", " L/s"),
    "fan_static_Pa": ("MIN STATIC PRESSURE ", " Pa"),
    "fan_type": ("FAN TYPE ", ""),
    "fan_wb_required_L_s": ("WORKBOOK REQUIRED AIRFLOW ", " L/s"),
    "fan_wb_tender_L_s": ("WORKBOOK TENDER AIRFLOW ", " L/s"),
    "fcu_supply_L_s": ("ROOM SUPPLY ", " L/s"),
    "fcu_oa_L_s": ("OUTDOOR AIR ", " L/s"),
    "fcu_coil_kW": ("FCU COIL DUTY ", " kW"),
    "pahu_sum_oa_L_s": ("SUM OF THE OUTDOOR AIR OF ITS AHUS ", " L/s"),
    "pahu_oa_L_s": ("TO ", ""),
    "original_ahu": ("ORIGINAL AHU ", ""),
    "sched_tag": ("SCHEDULE ", ""),
    "zone_room_area_m2": ("", " m2"),
    "named_on_sheets": ("NAMED AS OUTDOOR-AIR SOURCE ON ", ""),
}


class Stack:
    """Text and values of a box laid out from the top down, relative to the top left corner (0, 0)."""

    def __init__(self, w: float):
        self.w = w
        self.y = 0.0
        self.ops: List[Tuple[float, float, str, float, str, Optional[Value], Tuple[int, int], str]] = []
        self.values: List[Value] = []
        self.owner = ""

    def gap(self, g: float) -> None:
        self.y -= g

    def text(self, s: str, h: float = H, layer: str = "IP-ROOMTEXT", value: Optional[Value] = None, dx: float = 0.0,
             prefix: str = "", suffix: str = "", label: str = "", label_w: float = 0.0, gap: float = 0.3) -> None:
        """One paragraph (wrapped).  With a value the text is prefix + value + suffix and the value is registered."""
        full = prefix + s + suffix
        if value is not None:
            self.values.append(value)
        width = self.w - dx - label_w
        lines = wrap(full, h, width)
        first = self.y > -0.01 or self.y > -2.0
        for i, ln in enumerate(lines):
            self.y -= h if first else h * LP
            if i == 0 and label:
                self.ops.append((dx, self.y, label, HS, layer, None, (0, 1), self.owner))
            first = False
            self.ops.append((dx + label_w, self.y, ln, h, layer, value, (i, len(lines)), self.owner))
        self.y -= gap

    def pair(self, items: List[Tuple[float, str, Optional[Value], str]], h: float = H) -> None:
        """A row of label and value pairs: (dx, label, value, unit suffix)."""
        self.y -= h
        for dx, lab, val, suf in items:
            self.ops.append((dx, self.y, lab, HS, "IP-ROOMTEXT", None, (0, 1), self.owner))
            if val is not None:
                self.values.append(val)
            self.ops.append((dx + 14.0 if dx == 0 else dx + 15.0, self.y, val.text + suf if val is not None else "", h,
                             "IP-ROOMTEXT", val, (0, 1), self.owner))
        self.y -= h * LP - h + 0.4

    @property
    def height(self) -> float:
        return -self.y


def emit(sh: Sheet, st: Stack, x: float, y_top: float) -> None:
    sh.values += st.values
    for dx, dy, s, h, layer, val, part, owner in st.ops:
        sh.text(x + dx, y_top + dy, s, h, layer, val, part, owner=owner)


def ValueOf(field: str, text: str, chg: str, src: str, owner: str) -> Value:
    return Value(field, text, chg, src, owner)


def numtxt(v: Any) -> str:
    return "not given" if v is None or v == "" else fnum1(v)


# --------------------------------------------------------------------------- #
# Room boxes
# --------------------------------------------------------------------------- #
def room_box_stack(g: Dict[str, Any], chg: str, uid: str) -> Stack:
    f = g["first"]
    mem = g["members"]
    owner = "box:%s:%s" % (uid, f["room"])
    st = Stack(BW - 3.2)
    st.owner = owner
    dept, row = f["dept"], f["row"]
    st.gap(1.2)
    nos = ", ".join(m["room"] for m in mem)
    src_n = src_wb(dept, "Airflow", f["name_cell"])
    st.text(nos, HS, value=Value("room_no", nos, chg, "Workbook D%s tab Airflow cells %s" % (dept, "; ".join("A%d" % m["row"] for m in mem)),
                                 owner), gap=0.9)
    st.text(str(f["name"]), H + 0.1, value=Value("name", str(f["name"]), chg, src_n, owner), gap=1.1)
    cnt = Value("count", str(len(mem)), chg, "Workbook D%s tab Airflow cells %s" % (dept, "; ".join("A%d" % m["row"] for m in mem)), owner)
    area = Value("area_m2", numtxt(f["area"]), chg, src_wb(dept, "Airflow", f["area_cell"]), owner)
    st.values += [cnt, area]
    st.y -= H
    st.ops.append((0, st.y, "COUNT", HS, "IP-ROOMTEXT", None, (0, 1), owner))
    st.ops.append((14, st.y, cnt.text, H, "IP-ROOMTEXT", cnt, (0, 1), owner))
    st.ops.append((22, st.y, "AREA", HS, "IP-ROOMTEXT", None, (0, 1), owner))
    st.ops.append((33, st.y, area.text + " m2", H, "IP-ROOMTEXT", area, (0, 1), owner))
    st.y -= H * LP - H + 0.4

    def wv(field, key):
        v = f[key]
        return Value(field, numtxt(v), chg, src_wb(dept, "Airflow", f[key + "_cell"]), owner)
    sa, ra, ea, oa = wv("supply_L_s", "sa"), wv("return_L_s", "ra"), wv("exhaust_L_s", "ea"), wv("outdoor_L_s", "oa")
    st.pair([(0, "SA L/S", sa, ""), (36, "RA L/S", ra, "")])
    st.pair([(0, "EA L/S", ea, ""), (36, "OA L/S", oa, "")])
    if f["press"] is not None:
        pv = Value("pressure", numtxt(f["press"]), chg, src_wb(dept, "Pressure", f["press_cell"]), owner)
        st.pair([(0, "PRESS PA", pv, "")])
    # D-076: the design-criteria rows of the room box: filter, temperature, RH and air class of the room's criteria row
    crit = dc.criteria_of(dept, f["room"])

    def cv(fld, key):
        val, cell = crit[key]
        src = src_wb(dept, "Criteria", cell) if cell else "Workbook D%s tab Criteria, no row for room %s" % (dept, f["room"])
        return Value(fld, numtxt(val), chg, src, owner)
    flt, tmp, rh, cls = cv("filter", "filter"), cv("temperature_C", "temp"), cv("rh_pct", "rh"), cv("air_class", "air_class")
    st.pair([(0, "FILTER", flt, "")])
    st.pair([(0, "TEMP C", tmp, ""), (36, "RH %", rh, "")])
    st.pair([(0, "CLASS", cls, "")])
    st.gap(0.2)
    for fld, lab, key in (("terminal_tag", "TERMINAL", "term"), ("exhaust_fan_tag", "EXH FAN", "fan")):
        if f[key]:
            st.text(lab, HS, gap=0.1)
            st.text(str(f[key]), H, value=Value(fld, str(f[key]), chg, src_wb(dept, "Airflow", f[key + "_cell"]), owner), gap=0.8)
    st.gap(2.0)
    return st


def room_box_height(g: Dict[str, Any], chg: str, uid: str) -> float:
    n0 = Value._n
    st = room_box_stack(g, chg, uid)
    Value._n = n0
    return round(st.height + 2 * HS * LP + 3.4, 1)


def draw_room_box(sh: Sheet, g: Dict[str, Any], chg: str, uid: str, x: float, y_top: float, h_box: float,
                  note: str = "") -> None:
    owner = "box:%s:%s" % (uid, g["first"]["room"])
    insert(sh, "ROOMBOX", x, y_top - h_box, BW, h_box, "IP-ROOMBOX")
    st = room_box_stack(g, chg, uid)
    emit(sh, st, x + 1.6, y_top)
    v = Value("change_id", chg, chg, src_wb(g["first"]["dept"], "Airflow", "A%d" % g["first"]["row"]), owner)
    sh.values.append(v)
    sh.text(x + 1.6, y_top - h_box + 1.2 + HS * LP, "CHG " + chg, HS, "IP-ANNO-ID", v, owner=owner)
    sh.text(x + 1.6, y_top - h_box + 1.2, "D%s ROOM %s" % (g["first"]["dept"], g["first"]["room"])
            if len(g["members"]) == 1 else "D%s, %d ROOMS" % (g["first"]["dept"], len(g["members"])), HS, "IP-ANNO-ID",
            owner=owner)


# --------------------------------------------------------------------------- #
# Cards (unit and fan boxes with their symbol)
# --------------------------------------------------------------------------- #
def draw_card(sh: Sheet, st: Stack, blk: str, x: float, y_top: float, w: float, chg: str, foot: str, owner: str,
              sym: Optional[str] = None, min_h: float = 0.0, foot_src: str = "") -> float:
    """Box with its symbol block (AHU, FCU, labelled box) or with the legend fan symbol at its left; returns its height."""
    ht = max(st.height + 2 * HS * LP + 2.4, min_h)
    if sym:
        ht = max(ht, 22.0)
    if blk == "FAN":
        sy = 15.0
        sxw = sy * sh.sym_aspect[sym]
        insert(sh, sym, x + 1.0, y_top - ht / 2 - sy / 2, sxw, sy, "IP-EQUIP")
        sh.symbols.append((x + 1.0, y_top - ht / 2 - sy / 2, x + 1.0 + sxw, y_top - ht / 2 + sy / 2, "legend fan"))
    else:
        insert(sh, blk, x, y_top - ht, w, ht, "IP-EQUIP")
        sh.symbols.append((x, y_top - ht, x + w, y_top, blk))
    emit(sh, st, x, y_top)
    xo = 14.0 if blk == "FAN" else 1.6
    v = Value("change_id", chg, chg, foot_src, owner)
    sh.values.append(v)
    sh.text(x + xo, y_top - ht + 1.2 + HS * LP, "CHG " + chg, HS, "IP-ANNO-ID", v, owner=owner)
    sh.text(x + xo, y_top - ht + 1.2, foot, HS, "IP-ANNO-ID", owner=owner)
    return ht


def sched_src(page: str, tag: str, col: str) -> str:
    return "Equipment schedule, air handling, page %s, tag %s, column %s" % (page, tag, col)


def fan_src(page: str, tag: str, col: str) -> str:
    return "Equipment schedule, fans, page %s, tag %s, column %s" % (page, tag, col)


FAN_SYM = {"Axial": "SYM_FAN_AXIAL", "Centrif SISW": "SYM_FAN_CENTRIF"}


def fan_kinds(tag: str) -> List[str]:
    t = tag.upper()
    if t.startswith("OAF/EAF"):
        return ["OA", "EA"]
    if t.startswith("OAF"):
        return ["OA"]
    if t.startswith("RAF"):
        return ["RA"]
    if t.startswith("TEAF") or t.startswith("TEF"):
        return ["TEA"]
    return ["EA"]


def wb_fan(d_inputs: rv.Inputs, tag: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    for dept, w in d_inputs.wb.items():
        for f in w["fans"]:
            if nk(f["tag"]) == nk(tag):
                return dept, f
    return None


def fan_stack(U: Dict[str, Any], f: Dict[str, str], inp: rv.Inputs, w: float) -> Tuple[Stack, Optional[str]]:
    tag = f["tag_as_written"]
    chg = U["chg"]
    owner = "eq:" + tag
    sym = FAN_SYM.get(f.get("Fan Type (1)", ""))
    xo = 14.0 if sym else 1.6
    st = Stack(w - xo - 1.6)
    st.owner = owner
    pg = f["page"]
    st.gap(1.2)
    st.text(tag, HT, "IP-EQUIPTEXT", Value("tag", tag, chg, fan_src(pg, tag, "tag_as_written"), owner), dx=xo)
    if sym:
        ft = "AXIAL" if sym == "SYM_FAN_AXIAL" else "CENTRIFUGAL"
        st.text(ft, H, "IP-EQUIPTEXT", Value("fan_type", ft, chg, fan_src(pg, tag, "Fan Type (1)") + ", value " + f["Fan Type (1)"],
                                              owner), dx=xo, prefix="FAN TYPE ")
    if f.get("Min Airflow (l/s)"):
        st.text(fnum(f["Min Airflow (l/s)"]), H, "IP-EQUIPTEXT",
                Value("fan_airflow_L_s", fnum(f["Min Airflow (l/s)"]), chg, fan_src(pg, tag, "Min Airflow (l/s)"), owner),
                dx=xo, prefix="MIN AIRFLOW ", suffix=" L/s")
    if f.get("Min Fan Static Pressure (2) (4) (Pa)"):
        st.text(fnum(f["Min Fan Static Pressure (2) (4) (Pa)"]), H, "IP-EQUIPTEXT",
                Value("fan_static_Pa", fnum(f["Min Fan Static Pressure (2) (4) (Pa)"]), chg,
                      fan_src(pg, tag, "Min Fan Static Pressure (2) (4) (Pa)"), owner), dx=xo, prefix="MIN STATIC PRESSURE ", suffix=" Pa")
    wf = wb_fan(inp, tag)
    if wf:
        dept, wr = wf
        b = book(dept)
        rr = b.rowfind("Fans", wr["tag"])
        if rr:
            req, c1 = b.get("Fans", rr, "Required airflow")
            if req is not None:
                st.text(fnum1(req), H, "IP-EQUIPTEXT", Value("fan_wb_required_L_s", fnum1(req), chg, src_wb(dept, "Fans", c1), owner),
                        dx=xo, prefix="WORKBOOK REQUIRED AIRFLOW ", suffix=" L/s")
    if not f.get("Min Airflow (l/s)"):
        st.text("NO AIRFLOW IN THE SCHEDULE ROW", HS, "IP-QUERY", dx=xo)
    st.gap(1.0)
    return st, sym


def draw_fan(sh: Sheet, U: Dict[str, Any], f: Dict[str, str], inp: rv.Inputs, x: float, y_top: float, w: float) -> float:
    st, sym = fan_stack(U, f, inp, w)
    tag = f["tag_as_written"]
    return draw_card(sh, st, "FAN" if sym else "BOX_LABELLED", x, y_top, w, U["chg"], "SCHEDULE PAGE %s" % f["page"],
                     "eq:" + tag, sym=sym, foot_src=fan_src(f["page"], tag, "tag_as_written"))


def fan_height(U, f, inp, w) -> float:
    n0 = Value._n
    st, sym = fan_stack(U, f, inp, w)
    Value._n = n0
    return max(st.height + 2 * HS * LP + 2.4, 22.0 if sym else 0.0)


def unit_zs(U: Dict[str, Any], col: str) -> Tuple[Any, str]:
    return U["zone"][col]


def unit_card_stack(U: Dict[str, Any], w: float) -> Stack:
    chg = U["chg"]
    tag = U["tag"]
    owner = "eq:" + tag
    st = Stack(w - 3.2)
    st.owner = owner
    zc = U["zone"]
    mc = U["master"]
    zsrc = lambda col: "Zone schedule, tab AHU summary, cell %s" % zc[col][1]  # noqa: E731
    st.gap(1.2)
    st.text(tag, HT, "IP-EQUIPTEXT", Value("tag", tag, chg, zsrc("AHU or FCU tag"), owner), dx=1.6)
    typ = {"AHU": "AIR HANDLING UNIT", "FCU": "FAN COIL UNIT", "PAHU": "OUTDOOR-AIR HANDLING UNIT (PAHU)",
           "CRAC": "COMPUTER ROOM AIR CONDITIONER (CRAC)"}.get(U["type"], U["type"])
    st.text(str(zc["Type"][0]), H, "IP-EQUIPTEXT", Value("unit_type", str(zc["Type"][0]), chg, zsrc("Type"), owner), dx=1.6,
            prefix="TYPE ", gap=0.1)
    st.text(typ, HS, "IP-EQUIPTEXT", dx=1.6)
    # supply and outdoor airflow: tender and workbook, each with its cell
    t = zc["Tender airflow (L/s)"][0]
    if t is not None:
        st.text(fnum1(t), H, "IP-EQUIPTEXT", Value("tender_supply_L_s", fnum1(t), chg, zsrc("Tender airflow (L/s)"), owner),
                dx=1.6, prefix="TENDER SUPPLY AIRFLOW ", suffix=" L/s")
    cs = mc.get("Calculated airflow")
    if cs and cs[0] is not None:
        st.text(fnum1(cs[0]), H, "IP-EQUIPTEXT",
                Value("workbook_supply_L_s", fnum1(cs[0]), chg, "Workbook master summary, tab AHU Master, cell %s" % cs[1], owner),
                dx=1.6, prefix="WORKBOOK SUPPLY AIRFLOW ", suffix=" L/s")
    to = zc["Tender outdoor airflow (L/s)"][0]
    if to is not None:
        st.text(fnum1(to), H, "IP-EQUIPTEXT", Value("tender_outdoor_L_s", fnum1(to), chg, zsrc("Tender outdoor airflow (L/s)"), owner),
                dx=1.6, prefix="TENDER OUTDOOR AIRFLOW ", suffix=" L/s")
    co = mc.get("AHU outdoor air = fraction × calculated airflow")
    if co and co[0] is not None:
        st.text(fnum1(co[0]), H, "IP-EQUIPTEXT",
                Value("workbook_outdoor_L_s", fnum1(co[0]), chg, "Workbook master summary, tab AHU Master, cell %s" % co[1], owner),
                dx=1.6, prefix="WORKBOOK OUTDOOR AIRFLOW ", suffix=" L/s")
    for r in U["sched"]:
        pg = r["page(s)"]
        st.text(r["tag_as_written"], H, "IP-EQUIPTEXT", Value("sched_tag", r["tag_as_written"], chg,
                                                               sched_src(pg, r["tag_as_written"], "tag_as_written"), owner),
                dx=1.6, prefix="SCHEDULE ", suffix="", gap=0.1)
        for fld, col, lab in (("sched_supply_L_s", "Supply Airflow (l/s)", "SUPPLY "), ("sched_outdoor_L_s", "Outdoor Airflow / Min (l/s)",
                                                                                        "OUTDOOR MIN "),
                              ("sched_esp_Pa", "External Resistanc e (1) (Pa)", "EXTERNAL RESISTANCE "),
                              ("sched_cooling_kW", "Min Total Cooling (kW)", "COOLING ")):
            v = r.get(col, "")
            if v not in ("", None):
                st.text(fnum(v), H, "IP-EQUIPTEXT", Value(fld, fnum(v), chg, sched_src(pg, r["tag_as_written"], col), owner), dx=6.0,
                        prefix=lab, suffix=" kW" if "kW" in fld else (" Pa" if "Pa" in fld else " L/s"), gap=0.1)
        st.gap(0.4)
    nz = zc["Rooms"][0]
    st.text(str(nz), H, "IP-EQUIPTEXT", Value("zone_rooms", str(nz), chg, zsrc("Rooms"), owner), dx=1.6, prefix="ROOMS IN THE ZONE SCHEDULE ",
            gap=0.1)
    st.text(fnum1(zc["Area (m²)"][0]), H, "IP-EQUIPTEXT", Value("zone_area_m2", fnum1(zc["Area (m²)"][0]), chg, zsrc("Area (m²)"), owner),
            dx=1.6, prefix="ZONED AREA ", suffix=" m2", gap=0.1)
    st.text(str(U["n_rooms_wb"]), H, "IP-EQUIPTEXT",
            Value("wb_rooms", str(U["n_rooms_wb"]), chg, "List room_unit_links.csv, unit_class %s, rooms found on the workbook tab Airflow" % tag,
                  owner),
            dx=1.6, prefix="ROOMS COUNTED IN THE WORKBOOKS ", gap=0.1)
    st.gap(1.0)
    return st


# --------------------------------------------------------------------------- #
# Unit block
# --------------------------------------------------------------------------- #
class Ctx:
    """Inputs shared by all blocks."""
    def __init__(self, d: Data):
        self.d = d
        self.inp = d.inp


def unit_queries(U: Dict[str, Any]) -> List[str]:
    """Queries of the unit, each as written on the drawing (change ID of the unit's row)."""
    chg = U["chg"]
    q: List[str] = []
    zc = U["zone"]
    oa = U["oa_src"]
    if U["type"] == "FCU":
        if oa is None:
            q.append("QUERY %s: OUTDOOR-AIR SOURCE OF THIS FCU IS NOT IN THE WORKBOOK AND IN NO DOCUMENT. CONFIRM IT." % chg)
        elif str(oa["oasrc"]).lower() == "not set":
            q.append("QUERY %s: OUTDOOR-AIR SOURCE NOT SET IN THE WORKBOOK. NO DOCUMENT NAMES IT. CONFIRM IT." % chg)
        else:
            q.append("QUERY %s: OUTDOOR-AIR SOURCE %s IS THE WORKBOOK DEFAULT. NO SHEET SHOWS IT. CONFIRM IT." % (chg, oa["oasrc"]))
    elif U["type"] in ("AHU", "CRAC"):
        q.append("QUERY %s: NO SHEET OR SCHEDULE NAMES THE OUTDOOR-AIR SOURCE OF THIS UNIT. CONFIRM IT." % chg)
    if str(zc["Tender airflow source"][0] or "").startswith("No information"):
        q.append("QUERY %s: THE UNIT IS NOT IN THE EQUIPMENT SCHEDULE, THE MOT NSC SCHEDULE OR THE TENDER SCHEMATICS. NO TENDER DUTY." % chg)
    rl = str(zc["Room list of the unit"][0] or "")
    if rl.startswith("Not complete"):
        q.append("QUERY %s: ROOM LIST NOT COMPLETE. %s" % (chg, rl[len("Not complete: "):].split(";")[0].upper() + "."))
    # the room boxes come from the workbooks; the approved zoning may give the unit other rooms or none
    awb = sum(float(m["area"] or 0) for g in U["room_groups"] for m in g["members"])
    nz = len(U["zone_rooms"])
    az = float(zc["Area (m²)"][0] or 0)
    if U["n_rooms_wb"] and (nz == 0 or abs(awb - az) > 0.1 * max(az, 1.0)):
        q.append("QUERY %s: THE WORKBOOKS COUNT %d ROOMS (%s M2) FOR THIS UNIT. THE APPROVED ZONING GIVES %d ROOM%s (%s M2). "
                 "THE BOXES SHOW THE WORKBOOK ROOMS AND THEIR VALUES. THE ZONING TABLE SHOWS THE ZONED ROOMS. "
                 "CONFIRM WHICH ROOMS THE UNIT SERVES." % (chg, U["n_rooms_wb"], numtxt(round(awb, 1)), nz, "" if nz == 1 else "S",
                                                            numtxt(round(az, 1))))
    if any(g["first"]["ea"] not in (None, 0) and float(g["first"]["ea"]) > 0 for g in U["room_groups"]) and not U["fans"]:
        q.append("QUERY %s: ROOMS OF THIS UNIT HAVE EXHAUST. NO EXHAUST FAN IS IN THE SCHEDULE FOR THIS UNIT. CONFIRM THE FAN." % chg)
    return q


def ea_total(U: Dict[str, Any]) -> bool:
    return any(g["first"]["ea"] not in (None, 0) and float(g["first"]["ea"]) > 0 for g in U["room_groups"])


def draw_header_note(sh: Sheet, U: Dict[str, Any], x: float, y: float) -> None:
    pl = U["plant"] or "NOT GIVEN BY THE TAG"
    sv = U["served"] or "NOT GIVEN BY THE TAG"
    sh.text(x, y - HS, "PLANT LEVEL: %s. SERVED LEVEL: %s. (FROM THE TAG)" % (pl, sv), HS, "IP-LEVELS", owner="unit:" + U["tag"])


def draw_zone_table(sh: Sheet, U: Dict[str, Any], x: float, y_top: float, ncol: int, colw: float) -> float:
    """Rooms of the zone schedule on this unit: ID, name, area.  Returns the y below the table."""
    chg = U["chg"]
    rows = U["zone_rooms"]
    owner = "ztab:" + U["tag"]
    head = "ROOMS OF THE ZONE SCHEDULE ON THIS UNIT (%d). ROOM ID, NAME, AREA IN M2." % len(rows) if rows else \
        "NO ROOM OF THE ZONE SCHEDULE IS ON THIS UNIT."
    sh.text(x, y_top - 3.0, head, 3.0, "IP-NOTES", owner=owner)
    y0 = y_top - 3.0 - 4.4
    per = max(1, math.ceil(len(rows) / ncol))
    bottoms = [y0]
    for c in range(ncol):
        y = y0
        for r in rows[c * per:(c + 1) * per]:
            xx = x + c * colw
            src = lambda col: "Zone schedule, tab %s, cell %s%d" % (r["storey"], col, r["row"])  # noqa: E731
            vid = Value("zone_room_id", str(r["room_id"]), chg, src("A"), owner)
            vn = Value("zone_room_name", str(r["name"]), chg, src("C"), owner)
            va = Value("zone_room_area_m2", numtxt(r["area"]), chg, src("G"), owner)
            sh.values += [vid, vn, va]
            sh.text(xx, y - H, vid.text, H, "IP-ROOMTEXT", vid, owner=owner)
            nm = vn.text if tw(vn.text, H) <= colw - 52 else vn.text
            sh.text(xx + 22, y - H, nm, H, "IP-ROOMTEXT", vn, owner=owner)
            sh.text(xx + colw - 8 - tw(va.text + " m2", H), y - H, va.text + " m2", H, "IP-ROOMTEXT", va, owner=owner)
            y -= H * LP + 0.6
        bottoms.append(y)
    return min(bottoms) - 1.0


def draw_unit(sh: Sheet, cx: Ctx, U: Dict[str, Any], gx: float, ytop: float, ncols: int) -> None:
    chg = U["chg"]
    tag = U["tag"]
    owner = "unit:" + tag
    groups = U["room_groups"]
    draw_header_note(sh, U, gx, ytop)
    aw = 160.0
    ax = gx + 14.0
    y_card = ytop - 24.0
    # card
    kind_blk = {"AHU": "SYM_AHU", "FCU": "SYM_FCU"}.get(U["type"], "BOX_LABELLED")
    st = unit_card_stack(U, aw)
    ht = draw_card(sh, st, kind_blk, ax, y_card, aw, chg, "ZONE SCHEDULE CELL %s" % U["zone"]["AHU or FCU tag"][1], "eq:" + tag,
                   foot_src="Zone schedule, tab AHU summary, cell %s" % U["zone"]["AHU or FCU tag"][1])
    ay = y_card - ht
    # outdoor-air connection (above the card)
    xo = ax + aw / 2
    sh.line((xo, y_card + 12.0), (xo, y_card), nd.DUCT_LAYER["OA"])
    oa = U["oa_src"]
    if U["type"] == "FCU" and oa is not None and str(oa["oasrc"]).lower() != "not set":
        oatxt = "OUTDOOR AIR FROM %s (WORKBOOK DEFAULT, NOT ON ANY SHEET)" % oa["oasrc"]
        v = Value("oa_source", str(oa["oasrc"]), chg, src_wb(oa["dept"], "FCU", oa["oasrc_cell"]), owner)
        sh.values.append(v)
        sh.text(xo + 3, y_card + 6.0, "OUTDOOR AIR FROM ", H, "IP-DUCT-OA", owner=owner)
        sh.text(xo + 3 + tw("OUTDOOR AIR FROM ", H), y_card + 6.0, v.text, H, "IP-DUCT-OA", v, owner=owner)
    elif U["type"] == "FCU" and oa is not None:
        v = Value("oa_source", "not set", chg, src_wb(oa["dept"], "FCU", oa["oasrc_cell"]), owner)
        sh.values.append(v)
        sh.text(xo + 3, y_card + 6.0, "OUTDOOR-AIR SOURCE (WORKBOOK) ", H, "IP-DUCT-OA", owner=owner)
        sh.text(xo + 3 + tw("OUTDOOR-AIR SOURCE (WORKBOOK) ", H), y_card + 6.0, v.text, H, "IP-DUCT-OA", v, owner=owner)
    else:
        sh.text(xo + 3, y_card + 6.0, "OUTDOOR-AIR SOURCE: NOT SHOWN IN ANY DOCUMENT", H, "IP-DUCT-OA", owner=owner)
    # fans
    fans = sorted(U["fans"], key=lambda f: (0 if fan_kinds(f["tag_as_written"])[0] == "OA" else 1, f["tag_as_written"]))
    fw = 84.0
    fx0 = ax + aw + 14.0
    fan_pos = []
    fb = ay
    for i, f in enumerate(fans):
        fx = fx0 + i * (fw + 6.0)
        h_f = draw_fan(sh, U, f, cx.inp, fx, y_card, fw)
        fan_pos.append((f, fx, y_card - h_f))
        fb = min(fb, y_card - h_f)
    # headers
    kinds = ["SA", "RA"]
    if ea_total(U) or any(fan_kinds(f["tag_as_written"])[0] in ("EA",) or "EA" in fan_kinds(f["tag_as_written"]) for f in fans):
        kinds.append("EA")
    if any("TEA" in fan_kinds(f["tag_as_written"]) for f in fans):
        kinds.append("TEA")
    hdr0 = fb - 10.0
    hy = {k: hdr0 - 5.0 * i for i, k in enumerate(kinds)}
    gw = max(1, min(ncols, len(groups))) * PITCH - (PITCH - BW)
    x_last = max(gx + gw - 3, ax + aw)
    if fan_pos:
        x_last = max(x_last, fan_pos[-1][1] + fw / 2 + 4)
    sa_x, ra_x = ax + aw / 2 - 14, ax + aw / 2 + 14
    riser = {"SA": gx - 5.0, "RA": gx - 8.5, "EA": gx - 12.0, "TEA": gx - 15.5}
    rows_n = math.ceil(len(groups) / ncols) if groups else 0
    row_tops, heights = [], []
    y = min(hy.values()) - 14.0
    for rr in range(rows_n):
        chunk = groups[rr * ncols:(rr + 1) * ncols]
        hrow = max(room_box_height(g, chg, tag) for g in chunk)
        heights.append(hrow)
        row_tops.append(y)
        y -= hrow + 14.0
    sh.line((sa_x, ay), (sa_x, hy["SA"]), nd.DUCT_LAYER["SA"])
    sh.line((ra_x, ay), (ra_x, hy["RA"]), nd.DUCT_LAYER["RA"])
    # OA fan into the card
    for f, fx, fbot in fan_pos:
        ks = fan_kinds(f["tag_as_written"])
        for k in ks:
            if k == "OA":
                ym = y_card - 11.0
                sh.line((fx, ym), (ax + aw, ym), nd.DUCT_LAYER["OA"])
            elif k in hy:
                xk = fx + fw / 2 + (6 if (k == ks[-1] and len(ks) > 1 and ks.index(k) > 0) else 0)
                sh.line((xk, fbot), (xk, hy[k]), nd.DUCT_LAYER[k])
    left = gx - 2.0
    for k in kinds:
        xs = sa_x if k == "SA" else (ra_x if k == "RA" else left)
        xr = riser[k] if rows_n > 1 else min(xs, left)
        sh.line((min(xr, xs) if k in ("SA", "RA") else left, hy[k]), (x_last, hy[k]), nd.DUCT_LAYER[k])
    if rows_n > 1:
        for k in ("SA", "RA"):
            sh.line((riser[k], hy[k]), (riser[k], row_tops[-1] + (8.0 if k == "SA" else 4.0)), nd.DUCT_LAYER[k])
        if "EA" in hy:
            sh.line((riser["EA"], hy["EA"]), (riser["EA"], row_tops[-1] + 12.0), nd.DUCT_LAYER["EA"])
    bottoms = [ay, fb]
    for rr in range(rows_n):
        chunk = groups[rr * ncols:(rr + 1) * ncols]
        rt, hrow = row_tops[rr], heights[rr]
        sa_row = hy["SA"] if rr == 0 else rt + 8.0
        ra_row = hy["RA"] if rr == 0 else rt + 4.0
        if rr > 0:
            sh.line((riser["SA"], sa_row), (gx + len(chunk) * PITCH - 6, sa_row), nd.DUCT_LAYER["SA"])
            sh.line((riser["RA"], ra_row), (gx + len(chunk) * PITCH - 6, ra_row), nd.DUCT_LAYER["RA"])
            if "EA" in hy:
                sh.line((riser["EA"], rt + 12.0), (gx + len(chunk) * PITCH - 6, rt + 12.0), nd.DUCT_LAYER["EA"])
        ea_row = hy["EA"] if (rr == 0 and "EA" in hy) else rt + 12.0
        for j, g in enumerate(chunk):
            x = gx + j * PITCH
            draw_room_box(sh, g, chg, tag, x, rt, hrow)
            sh.line((x + 8, sa_row), (x + 8, rt), nd.DUCT_LAYER["SA"])
            sh.line((x + 14, ra_row), (x + 14, rt), nd.DUCT_LAYER["RA"])
            ea = g["first"]["ea"]
            if "EA" in hy and ea not in (None, 0) and float(ea) > 0:
                sh.line((x + 20, ea_row), (x + 20, rt), nd.DUCT_LAYER["EA"])
        bottoms.append(rt - hrow)
    # labels and direction arrows of the headers, as the pilot
    for k in kinds:
        sh.text(x_last + 3.0, hy[k] - 0.8, k, HS, nd.DUCT_LAYER[k], owner=owner)
        arrow_at(sh, x_last + 10.0, hy[k] + 0.5, nd.DUCT_LAYER[k], 7.0)
    ybot = min(bottoms) - 8.0 if rows_n else min(hy.values()) - 8.0
    if not groups:
        for i_, ln_ in enumerate(wrap(U.get("gate_text") or
                                      "NO ROOM OF THIS UNIT HAS A ROW IN THE DEPARTMENT WORKBOOKS. NO ROOM BOX IS DRAWN.", 3.0, 330.0)):
            sh.text(gx, min(hy.values()) - 9.0 - 4.4 * i_, ln_, 3.0, "IP-NOTES", owner=owner)
        ybot = min(hy.values()) - 14.0 - (4.4 if U.get("gate_text") else 0.0)
    # zone table and queries
    ncol_z = 1 if len(U["zone_rooms"]) <= 4 else (2 if len(U["zone_rooms"]) <= 10 else 3)
    ybot = draw_zone_table(sh, U, gx, ybot, ncol_z, 150.0) - 3.0
    for qline in unit_queries(U):
        for ln in wrap(qline, HS, max(gw, 300.0)):
            sh.text(gx, ybot - HS, ln, HS, "IP-QUERY", owner=owner + ":q")
            ybot -= HS * LP
    sh.last_bottom = ybot - 1.0


def unit_ncols(U: Dict[str, Any]) -> List[int]:
    return list(range(4, 12))


# --------------------------------------------------------------------------- #
# Separated rooms: FCU and outdoor-air connection
# --------------------------------------------------------------------------- #
def build_sep(d: Data, row: Dict[str, str]) -> Dict[str, Any]:
    ms = re.match(r"Workbook D(\d\d) tab FCU row (\d+)", row["source"])
    dept, frow = ms.group(1), int(ms.group(2))
    b = book(dept)
    rno = str(b.tab("FCU")[1][frow - 1][1])
    assert row["system"].startswith("Room " + rno), (rno, row["system"])
    ar = b.rowfind("Airflow", rno)
    rec = dict(dept=dept, room=rno, row=ar)
    for k, col in (("name", "Room name"), ("storey", "Storey"), ("area", "Area"), ("sa", "Final supply"), ("ra", "Return"),
                   ("ea", "Exhaust"), ("oa", "Outdoor air"), ("term", "Terminal tag"), ("fan", "Exhaust fan tag")):
        rec[k], rec[k + "_cell"] = b.get("Airflow", ar, col)
    pr = b.rowfind("Pressure", rno)
    rec["press"], rec["press_cell"] = (b.get("Pressure", pr, "Set pressure") if pr else (None, ""))
    f = {}
    for k, col in (("orig", "Original AHU or system"), ("reason", "Reason"), ("supply", "Room supply"), ("oa", "Outdoor air"),
                   ("src", "Outdoor-air source (PAHU or fan)"), ("by", "Source chosen by"), ("coil", "FCU coil duty"),
                   ("arrive", "Outdoor air arrives")):
        f[k], f[k + "_cell"] = b.get("FCU", frow, col)
    sheet = re.search(r"IP-BH-\w{5}", row["belongs_on_sheet"] or "")
    storey = str(rec["storey"] or "")
    return dict(kind="sep", chg=row["change_id"], rec=rec, fcu=f, frow=frow, dept=dept, row=row, storey=storey,
                orig=str(f["orig"]), sheet=row["belongs_on_sheet"])


def sep_fcu_stack(S: Dict[str, Any], w: float) -> Stack:
    chg, f, dept, fr = S["chg"], S["fcu"], S["dept"], S["frow"]
    owner = "fcu:%s" % S["rec"]["room"]
    st = Stack(w - 3.2)
    st.owner = owner
    st.gap(1.2)
    st.text("NEW FCU, TAG TO CONFIRM", HT, "IP-EQUIPTEXT", dx=1.6)
    st.text(str(f["orig"]), H, "IP-EQUIPTEXT", Value("original_ahu", str(f["orig"]), chg, src_wb(dept, "FCU", f["orig_cell"]), owner),
            dx=1.6, prefix="ORIGINAL AHU ", gap=0.2)
    st.text(fnum1(f["supply"]), H, "IP-EQUIPTEXT", Value("fcu_supply_L_s", fnum1(f["supply"]), chg, src_wb(dept, "FCU", f["supply_cell"]), owner),
            dx=1.6, prefix="ROOM SUPPLY ", suffix=" L/s", gap=0.2)
    st.text(fnum1(f["oa"]), H, "IP-EQUIPTEXT", Value("fcu_oa_L_s", fnum1(f["oa"]), chg, src_wb(dept, "FCU", f["oa_cell"]), owner),
            dx=1.6, prefix="OUTDOOR AIR ", suffix=" L/s", gap=0.2)
    if f["coil"] is not None:
        st.text(fnum1(f["coil"]), H, "IP-EQUIPTEXT", Value("fcu_coil_kW", fnum1(f["coil"]), chg, src_wb(dept, "FCU", f["coil_cell"]), owner),
                dx=1.6, prefix="FCU COIL DUTY ", suffix=" kW", gap=0.2)
    st.gap(1.0)
    return st


def draw_sep(sh: Sheet, cx: Ctx, G: Dict[str, Any], gx: float, ytop: float, ncols: int = 1) -> None:
    """One original AHU's separated rooms of a storey: the outdoor-air source, one FCU per room, the room boxes."""
    members = G["members"]
    owner = "sepgrp:" + G["orig"]
    first = members[0]
    head = "ROOMS SEPARATED FROM %s BY THE OUTDOOR-AIR RULE (%s)" % (G["orig"].upper(), G["sheet"] if G["sheet"] != "NEW" else "NO SHEET NAMED")
    sh.text(gx, ytop - 3.0, head, 3.0, "IP-NOTES", owner=owner)
    fw, off = 104.0, 18.0
    x_f = gx + 14.0
    x_r = x_f + fw + off
    # outdoor-air source box
    y = ytop - 8.0
    st = Stack(fw - 3.2)
    st.owner = owner + ":src"
    f0 = first["fcu"]
    st.gap(1.2)
    st.text("OUTDOOR-AIR SOURCE", HT, "IP-EQUIPTEXT", dx=1.6)
    vsrc = Value("oa_source", str(f0["src"]), first["chg"], src_wb(first["dept"], "FCU", f0["src_cell"]), owner + ":src")
    st.text(str(f0["src"]), H, "IP-EQUIPTEXT", vsrc, dx=1.6, prefix="WORKBOOK: ", gap=0.2)
    st.gap(1.2)
    for ln in wrap("QUERY %s: OUTDOOR-AIR SOURCE %s. THE SOURCE IS STILL TO BE CHOSEN FROM THE ZONING. NO DOCUMENT NAMES IT." %
                   (first["chg"], "NOT SET IN THE WORKBOOK" if str(f0["src"]).lower() == "not set" else "IS A WORKBOOK DEFAULT"), HS, fw - 3.2):
        st.y -= HS
        st.ops.append((1.6, st.y, ln, HS, "IP-QUERY", None, (0, 1), owner + ":src"))
        st.y -= HS * LP - HS
    st.gap(1.0)
    ht_src = draw_card(sh, st, "BOX_LABELLED", x_f, y, fw, first["chg"], "WORKBOOK D%s TAB FCU" % first["dept"], owner + ":src",
                       foot_src=src_wb(first["dept"], "FCU", "A%d" % first["frow"]))
    y_oa_bottom = y - ht_src
    y_row = y_oa_bottom - 12.0
    oa_x = gx + 4.0
    sh.line((oa_x, y - ht_src / 2), (x_f, y - ht_src / 2), nd.DUCT_LAYER["OA"])
    sh.text(gx - 0.0, y - ht_src / 2 + 1.6, "OA", HS, nd.DUCT_LAYER["OA"], owner=owner)
    rows = []
    yy = y_row
    for S in members:
        g = dict(first=S["rec"], members=[S["rec"]])
        h_room = room_box_height(g, S["chg"], "sep")
        st = sep_fcu_stack(S, fw)
        n0 = Value._n
        h_f = max(st.height + 2 * HS * LP + 2.4, 10.0)
        Value._n = n0
        hrow = max(h_room, h_f)
        rows.append((S, g, yy, hrow, h_f))
        yy -= hrow + 10.0
    ylast = rows[-1][2] - rows[-1][4] / 2 if rows else y_row
    sh.line((oa_x, y - ht_src / 2), (oa_x, ylast), nd.DUCT_LAYER["OA"])
    for S, g, yt, hrow, h_f in rows:
        st = sep_fcu_stack(S, fw)
        hh = draw_card(sh, st, "SYM_FCU", x_f, yt, fw, S["chg"], "WORKBOOK D%s TAB FCU" % S["dept"], "fcu:%s" % S["rec"]["room"],
                       foot_src=src_wb(S["dept"], "FCU", "B%d" % S["frow"]))
        draw_room_box(sh, g, S["chg"], "sep", x_r, yt, max(hrow, hh))
        sh.line((oa_x, yt - 4.0), (x_f, yt - 4.0), nd.DUCT_LAYER["OA"])
        sh.line((x_f + fw, yt - 6.0), (x_r, yt - 6.0), nd.DUCT_LAYER["SA"])
        sh.line((x_r, yt - 11.0), (x_f + fw, yt - 11.0), nd.DUCT_LAYER["RA"])
        sh.text(x_f + fw + 2.0, yt - 4.6, "SA", HS, nd.DUCT_LAYER["SA"], owner=owner)
        sh.text(x_f + fw + 2.0, yt - 9.8, "RA", HS, nd.DUCT_LAYER["RA"], owner=owner)
    sh.last_bottom = rows[-1][2] - rows[-1][3] - 1.0 if rows else y_row


def group_seps(d: Data) -> List[Dict[str, Any]]:
    out: Dict[Tuple[str, str], Dict[str, Any]] = OrderedDict()
    for r in d.sep_rows():
        S = build_sep(d, r)
        st = S["storey"]
        out.setdefault((st, S["orig"]), dict(kind="sepgrp", storey=st, orig=S["orig"], sheet=S["sheet"], members=[]))["members"].append(S)
    return list(out.values())


# --------------------------------------------------------------------------- #
# Outdoor-air units named on the working sheets and drawn on none
# --------------------------------------------------------------------------- #
def build_pahu(d: Data, row: Dict[str, str]) -> Dict[str, Any]:
    tag = row["system"]
    k = ukey(tag)
    sheets = re.findall(r"IP-BH-\w{5}", row["source"])
    sched = [x for x in d.air_sched if x["tag_as_written"].startswith("PAHU") and ukey(x["tag_as_written"]) == k]
    served = []
    for s in d.sys:
        if s["source_set"] != "working" or s["sheet"] not in sheets or "PAHU" not in s["oa_source"].upper():
            continue
        toks = re.findall(r"PAHU[- ]?([A-Z0-9][A-Z0-9\-/ ]*)", s["oa_source"].upper())
        ok = False
        for t in toks:
            t = t.strip()
            if ukey("PAHU-" + t) == k or (k in ("XXX", "XXXX") and re.match(r"^%s\b" % k, t)):
                ok = True
        if ok:
            served.append(s)
    cnt: Dict[str, int] = defaultdict(int)
    for s in served:
        cnt[s["storey"]] += 1
    pl, sv = parse_tag_levels(tag if tag.startswith("PAHU-") else "PAHU-" + tag[5:])
    if cnt:
        storey = max(cnt, key=lambda x: (cnt[x], x))
    else:
        m = re.match(r"^PAHU[- ]B-(?:B\d|\d\d)-(B\d|\d\d)", tag)
        c = m.group(1) if m else "B2"
        storey = ("L" + c) if re.fullmatch(r"\d\d", c) else c
    return dict(kind="pahu", chg=row["change_id"], tag=tag, row=row, sched=sched, served=served, storey=storey, sheets=sheets,
                plant=pl, served_level=sv)


def pahu_stack(P: Dict[str, Any], w: float) -> Stack:
    chg = P["chg"]
    owner = "eq:" + P["tag"]
    st = Stack(w - 3.2)
    st.owner = owner
    st.gap(1.2)
    msrc = "List missing_systems.csv, change ID %s, column system" % chg
    st.text(P["tag"], HT, "IP-EQUIPTEXT", Value("tag", P["tag"], chg, msrc, owner), dx=1.6)
    st.text("OUTDOOR-AIR UNIT. NOT DRAWN ON ANY SHEET.", HS, "IP-EQUIPTEXT", dx=1.6)
    st.text(", ".join(P["sheets"]), H, "IP-EQUIPTEXT", Value("named_on_sheets", ", ".join(P["sheets"]), chg,
                                                             "List missing_systems.csv, change ID %s, column source" % chg, owner),
            dx=1.6, prefix="NAMED AS OUTDOOR-AIR SOURCE ON ", gap=0.4)
    for r in P["sched"]:
        pg = r["page(s)"]
        st.text(r["tag_as_written"], H, "IP-EQUIPTEXT", Value("sched_tag", r["tag_as_written"], chg,
                                                               sched_src(pg, r["tag_as_written"], "tag_as_written"), owner),
                dx=1.6, prefix="SCHEDULE ", gap=0.1)
        for fld, col, lab, suf in (("sched_supply_L_s", "Supply Airflow (l/s)", "SUPPLY ", " L/s"),
                                   ("sched_outdoor_L_s", "Outdoor Airflow / Min (l/s)", "OUTDOOR MIN ", " L/s"),
                                   ("sched_esp_Pa", "External Resistanc e (1) (Pa)", "EXTERNAL RESISTANCE ", " Pa")):
            v = r.get(col, "")
            if v not in ("", None):
                st.text(fnum(v), H, "IP-EQUIPTEXT", Value(fld, fnum(v), chg, sched_src(pg, r["tag_as_written"], col), owner), dx=6.0,
                        prefix=lab, suffix=suf, gap=0.1)
        st.gap(0.3)
    if not P["sched"]:
        st.text("NOT IN THE EQUIPMENT SCHEDULE UNDER THIS TAG", HS, "IP-QUERY", dx=1.6)
    st.gap(1.0)
    return st


def draw_pahu(sh: Sheet, cx: Ctx, P: Dict[str, Any], gx: float, ytop: float, ncols: int = 1) -> None:
    chg = P["chg"]
    owner = "pahu:" + P["tag"]
    pl = P["plant"] or "NOT GIVEN BY THE TAG"
    sh.text(gx, ytop - HS, "PLANT LEVEL: %s. (FROM THE TAG)" % pl, HS, "IP-LEVELS", owner=owner)
    aw = 150.0
    ax = gx + 4.0
    y_card = ytop - 16.0
    sh.text(ax + aw / 2 + 3, y_card + 6.0, "OUTDOOR-AIR INTAKE: NOT SHOWN IN ANY DOCUMENT", H, "IP-DUCT-OA", owner=owner)
    sh.line((ax + aw / 2, y_card + 10.0), (ax + aw / 2, y_card), nd.DUCT_LAYER["OA"])
    st = pahu_stack(P, aw)
    ht = draw_card(sh, st, "BOX_LABELLED", ax, y_card, aw, chg, "SOURCE: MISSING SYSTEMS LIST", owner,
                   foot_src="List missing_systems.csv, change ID %s, column source" % chg)
    y = y_card - ht
    # one outdoor-air duct and one line per system on the working sheets that takes outdoor air from this unit
    x_main = ax + 12.0
    y0 = y - 8.0
    sh.line((x_main, y), (x_main, y0 - 1.0), nd.DUCT_LAYER["OA"])
    yy = y0
    n_ok = 0
    for s in P["served"]:
        ofs = s["oa_L_s_as_written"]
        txt = "%s: %s" % (s["ahu_tags"], ofs if ofs.replace(":", "").strip() else "OUTDOOR AIRFLOW NOT WRITTEN")
        v = Value("pahu_serves", s["ahu_tags"] + " | " + s["oa_L_s_as_written"], chg,
                  "List schematic_systems.csv, system_id %s, columns ahu_tags and oa_L_s_as_written" % s["system_id"], owner)
        sh.values.append(v)
        lines = wrap(s["sheet"] + "  " + v.text, H, aw + 90 - 16)
        sh.line((x_main, yy - 1.0), (x_main + 8.0, yy - 1.0), nd.DUCT_LAYER["OA"])
        for i, ln in enumerate(lines):
            sh.text(x_main + 11.0, yy - H - i * H * LP, ln, H, "IP-ROOMTEXT", v, (i, len(lines)), owner=owner)
        yy -= len(lines) * H * LP + 1.8
        n_ok += 1
    sh.line((x_main, y0 - 1.0), (x_main, yy + 0.8), nd.DUCT_LAYER["OA"])
    note = "QUERY %s: NO SHEET DRAWS THIS UNIT WITH ITS DUTY. THE OUTDOOR AIRFLOW OF EACH SYSTEM ABOVE IS AS WRITTEN ON ITS SHEET. CONFIRM THE DUTY AND THE INTAKE." % chg
    if not P["served"]:
        note = "QUERY %s: NO SYSTEM ON THE NAMED SHEETS CAN BE TIED TO THIS UNIT. CONFIRM THE UNIT, ITS DUTY AND ITS INTAKE." % chg
    yy -= 2.0
    for ln in wrap(note, HS, aw + 90):
        sh.text(gx, yy - HS, ln, HS, "IP-QUERY", owner=owner + ":q")
        yy -= HS * LP
    sh.last_bottom = yy - 1.0


# --------------------------------------------------------------------------- #
# Sheets
# --------------------------------------------------------------------------- #
SERIES = {"LB3": ["06LB%d"], "B3": ["06B3%d"], "B2": ["06B2%d"], "B1": ["06B1%d"], "L01": ["0601%d"], "L02": ["0602%d"],
          "L03": ["0603%d"], "L04": ["0604%d"], "L05": ["0605%d", "0705%d"], "L06": ["0606%d"], "L07": ["0607%d"]}
_TAKEN: set = set()


def provisional_numbers(storey: str, n: int) -> List[str]:
    """The first free numbers of the block of the storey in the IP-BH series (06 series; 05 series is used by newdwg.py)."""
    used = nd.used_numbers() | _TAKEN
    for p in NEWDIR.glob("IP-BH-*_P1.dxf"):
        if not p.name[6:8] == "06" and not p.name[6:8] == "07":
            used.add(p.name[6:11])
    got: List[str] = []
    for pat in SERIES[storey]:
        for dgt in range(10):
            c = pat % dgt
            if c not in used and len(got) < n:
                got.append(c)
    if len(got) < n:
        raise RuntimeError("not enough free numbers for %s: %s" % (storey, got))
    _TAKEN.update(got)
    return ["IP-BH-" + c for c in got]


class SheetX(Sheet):
    """Sheet of this job: the storey is a string (B2, L03, ...)."""
    def __init__(self, storey: str, number: str, idx: int, total: int, numbers: List[str]):
        super().__init__(2, number, idx, total, numbers)
        self.storey = storey
        self.zone_shown: set = set()
        self.items: List[Dict[str, Any]] = []


def levels2(sh: SheetX, ys: List[float]) -> None:
    label = "%s LEVEL" % LEVEL_NAME[sh.storey]

    def mark(y: float):
        sh.msp.add_line((14, y), (STRIP_X0 - 8, y), dxfattribs=dict(layer="IP-LEVELS", linetype="DASHED", ltscale=4.0))
        insert(sh, "LEVEL_MARK", 14, y - 1.0, 1.0, 1.0, "IP-LEVELS")
        sh.text(20, y + 1.2, label, 2.8, "IP-LEVELS", owner="level")
    for y in ys:
        mark(y)
    sh.text(14, 828.0, "SERVED LEVEL OF THE SHEET: %s. THE PLANT LEVEL AND THE SERVED LEVEL OF EACH UNIT ARE WRITTEN OVER IT (FROM THE TAG)."
            % LEVEL_NAME[sh.storey], 2.8, "IP-LEVELS", owner="level")


def caption2(sh: SheetX) -> None:
    t = (getattr(sh, "caption_kind", None) or "UNITS WITH NO WORKING SHEET") + " - %s AIR-SIDE SCHEMATIC" % LEVEL_NAME[sh.storey]
    if sh.total > 1:
        t += " - SHEET %d OF %d" % (sh.idx, sh.total)
    sh.text(14, 16.0, t, 5.0, "IP-TITLE", owner="caption")
    sh.msp.add_line((14, 13.5), (14 + tw(t, 5.0), 13.5), dxfattribs=dict(layer="IP-TITLE", lineweight=50))


def strip2(sh: SheetX) -> None:
    x0, x1 = STRIP_X0, SHEET_W - 12
    w = x1 - x0
    y = 12.0

    def cell(xa, xb, ya, yb, label, value=None, hv=H):
        sh.rect(xa, ya, xb, yb, "IP-TITLE")
        sh.text(xa + 1.0, yb - 3.2, label, HS, "IP-TITLE", owner="title")
        if value:
            sh.text(xa + 1.0, ya + 2.0, value, hv, "IP-TITLE", owner="title")
    cell(x0, x0 + 30, y, y + 11, "JOB NO.", nd.JOB_NO)
    cell(x0 + 30, x0 + 86, y, y + 11, "DRAWING NO.", sh.number, 3.4)
    cell(x0 + 86, x1, y, y + 11, "REV.", "P1", 3.8)
    y += 11
    cell(x0, x0 + 30, y, y + 11, "STAGE", None)
    cell(x0 + 30, x0 + 62, y, y + 11, "CHECKED", None)
    cell(x0 + 62, x1, y, y + 11, "APPROVED", None)
    y += 11
    cw = w / 4
    cell(x0, x0 + cw, y, y + 11, "SCALE", "NTS")
    cell(x0 + cw, x0 + 2 * cw, y, y + 11, "DATE", "NOV 2026")
    cell(x0 + 2 * cw, x0 + 3 * cw, y, y + 11, "DESIGNED", None)
    cell(x0 + 3 * cw, x1, y, y + 11, "DRAWN", None)
    y += 11
    title = ["ACMV SERVICES", "%s AIR-SIDE SCHEMATIC" % LEVEL_NAME[sh.storey]] + list(getattr(sh, "title_kind", None) or [
        "UNITS WITH NO WORKING SHEET", "AND SEPARATED ROOMS"])
    if sh.total > 1:
        title.append("SHEET %d OF %d" % (sh.idx, sh.total))
    ht = 3.6 + len(title) * 5.6 + 2
    sh.rect(x0, y, x1, y + ht, "IP-TITLE")
    sh.text(x0 + 1.0, y + ht - 3.2, "DRAWING TITLE", HS, "IP-TITLE", owner="title")
    for i, t in enumerate(title):
        sh.text(x0 + 1.0, y + ht - 9.6 - i * 5.6, t, 4.2, "IP-TITLE", owner="title")
    y += ht
    lines = wrap(nd.PROJECT_TITLE, 3.4, w - 2.0)
    ht = 4.2 + len(lines) * 4.4 + 1.2
    sh.rect(x0, y, x1, y + ht, "IP-TITLE")
    sh.text(x0 + 1.0, y + ht - 3.2, "PROJECT TITLE", HS, "IP-TITLE", owner="title")
    for i, ln in enumerate(lines):
        sh.text(x0 + 1.0, y + ht - 8.0 - i * 4.4, ln, 3.4, "IP-TITLE", owner="title")
    y += ht
    sh.rect(x0, y, x1, y + 12.0, "IP-TITLE")
    sh.text(x0 + 1.0, y + 8.6, "STATUS", HS, "IP-TITLE", owner="title")
    sh.text(x0 + 1.0, y + 2.6, "DRAFT, FOR CHECKING AND DRAFTING", 4.2, "IP-TITLE", owner="title")
    y += 12.0
    sh.rect(x0, y, x1, y + 16.0, "IP-TITLE")
    sh.text(x0 + 1.0, y + 12.6, "DATE", HS, "IP-TITLE", owner="title")
    sh.text(x0 + 30, y + 12.6, "REV.", HS, "IP-TITLE", owner="title")
    sh.text(x0 + 44, y + 12.6, "DESCRIPTION", HS, "IP-TITLE", owner="title")
    sh.text(x0 + 1.0, y + 3.0, "NOV 2026", H, "IP-TITLE", owner="title")
    sh.text(x0 + 30, y + 3.0, "P1", H, "IP-TITLE", owner="title")
    sh.text(x0 + 44, y + 3.0, "FIRST ISSUE FOR CHECKING", H, "IP-TITLE", owner="title")
    sh.title_top = y + 16.0
    notes = [
        "THIS SHEET HAS A PROVISIONAL NUMBER. THE DRAWING OFFICE MUST CONFIRM IT.",
        (getattr(sh, "note_kind", None) or
         "THE SHEET SHOWS UNITS OF THE %s THAT NO WORKING SHEET DRAWS, THE FCU AND THE OUTDOOR-AIR CONNECTION OF ROOMS SEPARATED BY THE "
         "OUTDOOR-AIR RULE, AND OUTDOOR-AIR UNITS THAT THE WORKING SHEETS NAME AND NO SHEET DRAWS.") % LEVEL_NAME[sh.storey]
        + " IT SHALL BE READ WITH THE ACMV NOTES AND LEGEND SHEETS IP-BH-00001 AND IP-BH-00002."
        + ("" if sh.total == 1 else " THE LEVEL IS ON %d SHEETS: %s. EACH UNIT IS ON ONE SHEET." % (sh.total, ", ".join(sh.numbers))),
        "EACH VALUE HAS A CHANGE ID AND A SOURCE. THE CHANGE ID GIVES THE ENTRY IN THE SCHEMATIC UPDATE LIST AND IN THE MISSING SYSTEMS LIST.",
        "ROOM NAMES, AREAS AND AIRFLOWS ARE FROM THE DEPARTMENT WORKBOOKS (TAB AIRFLOW). PRESSURE IS SHOWN ONLY FOR A ROOM WITH A PRESSURE TARGET "
        "(TAB PRESSURE). A VALUE THAT THE WORKBOOK DOES NOT GIVE IS SHOWN AS NOT GIVEN.",
        "TENDER VALUES ARE FROM THE ZONE SCHEDULE. WORKBOOK VALUES ARE FROM THE AHU MASTER SUMMARY. SCHEDULE VALUES AND TAGS ARE AS WRITTEN IN THE "
        "EQUIPMENT SCHEDULE. WHERE TWO VALUES ARE SHOWN FOR ONE ITEM, THE DRAWING HAS BOTH AND DOES NOT CHOOSE.",
        "ROOMS OF THE ZONE SCHEDULE ARE LISTED UNDER EACH UNIT. A ROOM BOX IS DRAWN FOR A ROOM THAT HAS A ROW IN A DEPARTMENT WORKBOOK. ROOMS "
        "THAT ARE ALIKE ARE IN ONE BOX WITH THEIR COUNT.",
        "THE OUTDOOR-AIR SOURCE IS SHOWN AS FAR AS THE DOCUMENTS GIVE IT. WHERE THEY DO NOT, THE DRAWING HAS A QUERY IN MAGENTA.",
        "THE PLANT LEVEL AND THE SERVED LEVEL COME FROM THE TAG.",
        "THE SYMBOLS FOLLOW IP-BH-00002. A UNIT OR FAN WITH NO SYMBOL IN THE LEGEND IS A LABELLED BOX. THE LABELLED BOXES ARE LISTED IN THE "
        "SYMBOL LIST.",
        "A NEW FCU FOR A SEPARATED ROOM HAS NO TAG YET. ITS TAG IS TO BE CONFIRMED.",
    ]
    for x in getattr(sh, "xref", []):        # cross-reference line: tag and sheet, no value, no change ID
        notes.append("%s: SHOWN ON %s." % (x["item"].upper(), ", ".join(x["homes"])))
    yn = SHEET_H - 14.0
    sh.text(x0, yn - 2.8, "NOTES:", 2.8, "IP-NOTES", owner="notes")
    yn -= 6.5
    for i, n in enumerate(notes, 1):
        parts = wrap(n, 2.4, w - 8)
        sh.text(x0, yn - 2.4, "%d." % i, 2.4, "IP-NOTES", owner="notes")
        for ln in parts:
            sh.text(x0 + 6, yn - 2.4, ln, 2.4, "IP-NOTES", owner="notes")
            yn -= 3.6
        yn -= 1.2
    sh.notes_bottom = yn


def key_plan2(sh: SheetX) -> None:
    """Storey outline from the zoning room polygons, the zoned rooms of this sheet filled."""
    st = sh.storey
    zr = nd.read_csv(REG / ("room_polygons_%s_wkt.csv" % st))
    polys = {r["room_id"]: swkt.loads(r["wall_face_wkt_mm"]) for r in zr if r["wall_face_wkt_mm"]}
    zpoly = list(polys.values())
    minx = min(p.bounds[0] for p in zpoly)
    miny = min(p.bounds[1] for p in zpoly)
    maxx = max(p.bounds[2] for p in zpoly)
    maxy = max(p.bounds[3] for p in zpoly)
    x0, x1 = STRIP_X0, SHEET_W - 12
    box_w = x1 - x0
    scale = (box_w - 6) / (maxx - minx)
    box_h = (maxy - miny) * scale + 14
    top = sh.title_top + 4 + box_h
    ox, oy = x0 + 3 - minx * scale, sh.title_top + 4 + 3 - miny * scale
    sh.rect(x0, sh.title_top + 4, x1, top, "IP-KEYPLAN")

    def tx(p):
        return (p[0] * scale + ox, p[1] * scale + oy)

    def simp(coords):
        from shapely.geometry import LinearRing
        pts = [tx(c) for c in coords]
        pts = [(round(a * 4) / 4.0, round(b * 4) / 4.0) for a, b in pts]
        try:
            r = LinearRing(pts).simplify(0.3)
            raw = list(r.coords)[:-1]
            out = []
            for q in raw:
                if not out or math.hypot(q[0] - out[-1][0], q[1] - out[-1][1]) >= 0.25:
                    out.append(q)
            while len(out) > 3 and math.hypot(out[0][0] - out[-1][0], out[0][1] - out[-1][1]) < 0.25:
                out.pop()
            return out if len(out) >= 3 else pts
        except Exception:
            return pts
    for g in zpoly:
        for poly in (g.geoms if hasattr(g, "geoms") else [g]):
            sh.msp.add_lwpolyline(simp(poly.exterior.coords), close=True, dxfattribs=dict(layer="IP-KEYPLAN", lineweight=5))
    shown = [rid for rid in sorted(sh.zone_shown) if rid in polys]
    for rid in shown:
        g = polys[rid]
        for poly in (g.geoms if hasattr(g, "geoms") else [g]):
            if poly.area * scale * scale < 1.0 or len(simp(poly.exterior.coords)) < 3:
                continue
            hatch = sh.msp.add_hatch(color=0, dxfattribs=dict(layer="IP-KEYPLAN-SPACES"))
            hatch.rgb = (255, 200, 120)
            hatch.paths.add_polyline_path(simp(poly.exterior.coords), is_closed=True)
            for ring in poly.interiors:
                rr = simp(ring.coords)
                if len(rr) >= 3 and abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(rr, rr[1:] + rr[:1]))) / 2 >= 1.0:
                    hatch.paths.add_polyline_path(rr, is_closed=True)
            sh.msp.add_lwpolyline(simp(poly.exterior.coords), close=True, dxfattribs=dict(layer="IP-KEYPLAN-SPACES", lineweight=9))
    from shapely.geometry import box as sbox
    labels: List[Tuple[float, float, float, float]] = []
    for rid in sorted(shown, key=lambda r: -polys[r].area):
        g = polys[rid]
        s_ = rid.split("-")[-1].lstrip("R").lstrip("0") or "0"
        h = 2.0
        wd = tw(s_, h)
        pt = g.representative_point()
        cx, cy = tx((pt.x, pt.y))
        r = (cx - wd / 2, cy - h / 2, cx + wd / 2, cy + h / 2)
        rg = sbox((r[0] - ox) / scale, (r[1] - oy) / scale, (r[2] - ox) / scale, (r[3] - oy) / scale)
        if not g.contains(rg):
            continue
        if any(not (r[2] + 0.6 < o[0] or r[0] - 0.6 > o[2] or r[3] + 0.6 < o[1] or r[1] - 0.6 > o[3]) for o in labels):
            continue
        labels.append(r)
        sh.text(cx - wd / 2, cy - h / 2, s_, h, "IP-KEYPLAN-TEXT", owner="keyplan")
    cap = ["KEY PLAN, %s (NTS)" % LEVEL_NAME[st], "ZONED ROOMS OF THE UNITS ON THIS SHEET IN ORANGE; NUMBER = ZONING ROOM NUMBER",
           "OUTLINES FROM THE ZONING ROOM POLYGONS (STOREY %s). A ROOM WITH NO ZONING ROOM NUMBER IS NOT SHOWN." % st]
    yy = top - 4.2
    sh.text(x0 + 1.0, yy, cap[0], 2.8, "IP-KEYPLAN-TEXT", owner="keyplan")
    for c in cap[1:]:
        for ln in wrap(c, 2.0, box_w - 2):
            yy -= 3.0
            sh.text(x0 + 1.0, yy, ln, 2.0, "IP-KEYPLAN-TEXT", owner="keyplan")
    sh.keyplan_total = len(shown)
    sh.keyplan_labels = len(labels)
    sh.keyplan_top = top


def legend_list2(sh: SheetX) -> None:
    x, y = 380.0, 96.0
    sh.text(x, y, "SYMBOLS USED (IP-BH-00002)", 3.0, "IP-LEGEND", owner="legend")
    y -= 7.0
    insert(sh, "SYM_AHU", x, y - 6, 18.0, 8.0, "IP-LEGEND")
    sh.symbols.append((x, y - 6, x + 18.0, y + 2, "legend AHU"))
    sh.text(x + 22, y - 3.6, "AIR HANDLING UNIT", H, "IP-LEGEND", owner="legend")
    y -= 9.0
    insert(sh, "SYM_FCU", x, y - 6, 18.0, 8.0, "IP-LEGEND")
    sh.symbols.append((x, y - 6, x + 18.0, y + 2, "legend FCU"))
    sh.text(x + 22, y - 3.6, "FAN COIL UNIT", H, "IP-LEGEND", owner="legend")
    for k in ("SA", "RA", "OA", "EA", "TEA"):
        y -= 9.0
        sh.msp.add_line((x, y), (x + 18, y), dxfattribs=dict(layer=nd.DUCT_LAYER[k], lineweight=50))
        sh.ducts.append(((x, y), (x + 18, y)))
        sh.text(x + 22, y - 0.9, nd.DUCT_NAME[k], H, "IP-LEGEND", owner="legend")
    x += 230.0
    y = 96.0 - 7.0
    insert(sh, "SYM_ARROW", x + 5, y, 8.0, 8.0, "IP-LEGEND")
    sh.symbols.append((x + 4.5, y - 2, x + 13.5, y + 2, "legend arrow"))
    sh.text(x + 22, y - 0.9, "AIRFLOW DIRECTION", H, "IP-LEGEND", owner="legend")
    y -= 9.0
    for blk, lab in (("SYM_FAN_AXIAL", "AXIAL FAN"), ("SYM_FAN_CENTRIF", "CENTRIFUGAL FAN")):
        if blk in sh.doc.blocks:
            sxw = 12.0 * sh.sym_aspect[blk]
            insert(sh, blk, x + 1, y - 12, sxw, 12.0, "IP-LEGEND")
            sh.symbols.append((x + 1, y - 12, x + 1 + sxw, y, "legend fan"))
            sh.text(x + 22, y - 7.0, lab, H, "IP-LEGEND", owner="legend")
            y -= 15.0
    insert(sh, "BOX_LABELLED", x, y - 5, 18.0, 8.0, "IP-LEGEND")
    sh.symbols.append((x, y - 5, x + 18.0, y + 3, "legend box"))
    sh.text(x + 22, y - 0.9, "LABELLED BOX: OUTDOOR-AIR UNIT, CRAC, OUTDOOR-AIR SOURCE OR FAN WITH NO TYPE. NOT IN THE LEGEND.", H,
            "IP-LEGEND", owner="legend")


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #
def build_items(d: Data) -> Dict[str, List[Dict[str, Any]]]:
    by: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in d.unit_rows():
        U = build_unit(d, r)
        by[U["storey"]].append(U)
    for G in group_seps(d):
        G["bid"] = "SEP:" + G["orig"]
        by[G["storey"]].append(G)
    for r in d.pahu_rows():
        P = build_pahu(d, r)
        by[P["storey"]].append(P)
    return by


def item_id(it: Dict[str, Any]) -> str:
    return it["tag"] if it["kind"] in ("unit", "pahu") else it["bid"]


def make_block(cx: Ctx, scratch: SheetX, it: Dict[str, Any]) -> nd.Block:
    hmax = Y_TOP - Y_MIN
    k = it["kind"]
    if k == "unit":
        chosen = None
        for nc in range(4, 12):
            blk = nd.Block(item_id(it), "ahu", lambda sh, gx, yt, it=it, nc=nc: draw_unit(sh, cx, it, gx, yt, nc), [], [it["tag"]])
            nd.measure(scratch, blk)
            chosen = blk
            if blk.h <= hmax * 0.75 and blk.w <= X_MAX - X0_PAGE:
                break
        return chosen
    if k == "sepgrp":
        blk = nd.Block(item_id(it), "ahu", lambda sh, gx, yt, it=it: draw_sep(sh, cx, it, gx, yt), [], [])
    else:
        blk = nd.Block(item_id(it), "ahu", lambda sh, gx, yt, it=it: draw_pahu(sh, cx, it, gx, yt), [], [it["tag"]])
    nd.measure(scratch, blk)
    return blk


def zone_ids(it: Dict[str, Any]) -> List[str]:
    if it["kind"] == "unit":
        return [str(r["room_id"]) for r in it["zone_rooms"]]
    if it["kind"] == "sepgrp":
        return [m["rec"]["room"] for m in it["members"]]
    return []


def build_storey(cx: Ctx, storey: str, items: List[Dict[str, Any]], numbers_fn) -> List[Tuple[SheetX, Dict[str, Any]]]:
    sym = nd.legend_symbols()
    scratch = SheetX(storey, "SCRATCH", 1, 1, ["SCRATCH"])
    nd.make_blocks(scratch, sym)
    order = {"unit": 0, "sepgrp": 1, "pahu": 2}
    items = sorted(items, key=lambda i: (order[i["kind"]], item_id(i)))
    blocks = []
    by_id = {}
    for it in items:
        blk = make_block(cx, scratch, it)
        blocks.append(blk)
        by_id[blk.bid] = it
    layout = nd.paginate(blocks)
    numbers = numbers_fn(len(layout))
    out = []
    for i, bands in enumerate(layout):
        Value._n = 0
        sh = SheetX(storey, numbers[i], i + 1, len(layout), numbers)
        sh.xref = [x for x in cx.d.xref_rows() if x["storey"] == storey] if i == 0 else []
        nd.make_blocks(sh, sym)
        nd.frame(sh)
        y_top = Y_TOP
        level_ys: List[float] = []
        for band in bands:
            hmax = max(b.h for b, _x in band)
            for b, bx0 in band:
                b.draw(sh, bx0 - b.xmin, y_top)
                it = by_id[b.bid]
                sh.items.append(it)
                sh.zone_shown.update(zone_ids(it))
            level_ys.append(y_top - hmax - 6.0)
            y_top = y_top - hmax - SHELF_GAP
        sh.band_bottom = level_ys[-1] + 6.0
        levels2(sh, level_ys)
        strip2(sh)
        key_plan2(sh)
        legend_list2(sh)
        caption2(sh)
        out.append((sh, dict(blocks=[b for band in bands for b, _ in band])))
    return out


def run(storeys: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    NEWDIR.mkdir(parents=True, exist_ok=True)
    d = Data()
    cx = Ctx(d)
    by = build_items(d)
    results = []
    for st in (storeys or STOREYS):
        if not by.get(st):
            continue
        for sh, ctx in build_storey(cx, st, by[st], lambda n, st=st: provisional_numbers(st, n)):
            paths = nd.write_sheet(sh)
            results.append(dict(storey=st, number=sh.number, sheet=sh, ctx=ctx, **paths))
            print(sh.number, st, "%d items" % len(sh.items), [item_id(i) for i in sh.items])
    return results


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["run", "check"])
    ap.add_argument("--storey", action="append", default=None)
    ns = ap.parse_args(argv)
    res = run(ns.storey)
    if ns.cmd == "check":
        from . import newdwg_missing_check as chk
        return chk.main(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
