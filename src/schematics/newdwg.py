"""New air-side schematic for the App D common-area systems of one storey (brief 9.4).

The drawing is built once as DXF (ezdxf) with named layers and blocks.  The PDF is
rendered from the same DXF document, so the two cannot differ.  Every value text
carries its change ID and its source in the DXF itself (XDATA, application IPSCH), and
`check` reads the DXF back and compares every value with the registers.

    python -m src.schematics.newdwg run [--storey 4]
    python -m src.schematics.newdwg check

Storey choice: L02 if its App D systems fit one A0 sheet; if not, the storey with the
fewest App D systems (decided in `choose_storey`).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from collections import OrderedDict, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import ezdxf
import pymupdf
import shapely.wkt as swkt
from ezdxf.addons.drawing import Frontend, RenderContext, layout as dlayout
from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy, Configuration
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
from ezdxf.fonts import fonts as ezfonts

from . import model

ROOT = Path(__file__).resolve().parents[2]
REG = ROOT / "out" / "registers"
OUT = ROOT / "out" / "schematics"
NEWDIR = OUT / "new_drawings"
LEGEND_PDF = ROOT / "data" / "source" / "Basis" / "Working Drawings" / "IP-BH-00002_ACMV NOTES AND LEGEND 2.pdf"
WORKING = ROOT / "data" / "source" / "Basis" / "Working Drawings"
APPID = "IPSCH"

# Sheet: A0 landscape, same as the working drawings (3367.8 x 2384.16 pt)
SHEET_W, SHEET_H = 1188.084, 841.0864
PT = 25.4 / 72.0
STRIP_X0 = 1072.0          # right strip with notes, key plan and title block
STYLE = "IPNARROW"
X0_PAGE = 46.0              # left edge of the drawing area
X_MAX = STRIP_X0 - 12.0     # right edge of the drawing area
Y_TOP = 822.0               # top of the drawing area
Y_MIN = 104.0               # bottom of the drawing area (the legend band is below it)
SHELF_GAP = 24.0            # between two bands (a level line sits in the gap)
ORD = {2: "2ND", 3: "3RD", 4: "4TH", 5: "5TH", 6: "6TH", 7: "7TH"}
WF = 0.85                   # width factor, as the condensed type of the working drawings
H = 3.0                     # body text height (mm)
HT = 3.8                    # tag text height
HS = 2.4                    # source and change ID text height
LP = 1.5                    # line pitch factor

LAYERS = OrderedDict([
    ("IP-FRAME", 7), ("IP-TITLE", 7), ("IP-NOTES", 7), ("IP-LEVELS", 8),
    ("IP-ROOMBOX", 7), ("IP-ROOMTEXT", 7), ("IP-EQUIP", 7), ("IP-EQUIPTEXT", 7),
    ("IP-DUCT-SA", 4), ("IP-DUCT-RA", 3), ("IP-DUCT-OA", 5), ("IP-DUCT-EA", 1), ("IP-DUCT-TEA", 30),
    ("IP-ANNO-ID", 8), ("IP-QUERY", 6), ("IP-KEYPLAN", 8), ("IP-KEYPLAN-SPACES", 5),
    ("IP-KEYPLAN-TEXT", 7), ("IP-LEGEND", 7),
])
DUCT_RGB = {"SA": (128, 255, 255), "RA": (89, 255, 89), "OA": (145, 145, 255),
            "EA": (255, 128, 128), "TEA": (155, 78, 0)}   # legend sheet IP-BH-00002, duty colour swatches
DUCT_LAYER = {"SA": "IP-DUCT-SA", "RA": "IP-DUCT-RA", "OA": "IP-DUCT-OA", "EA": "IP-DUCT-EA", "TEA": "IP-DUCT-TEA"}
DUCT_NAME = {"SA": "SUPPLY AIR", "RA": "RETURN AIR", "OA": "OUTDOOR AIR", "EA": "EXHAUST AIR - GENERAL",
             "TEA": "EXHAUST AIR - TOILET"}

PROJECT_TITLE = ("PROPOSED REDEVELOPMENT OF ALEXANDRA HOSPITAL COMPRISING OF 3 BASEMENTS, 1 BLOCK OF "
                 "23-STOREY GENERAL HOSPITAL AND 1 BLOCK OF 12-STOREY SPECIALIST OUTPATIENT CLINICS ON "
                 "PT MK03-05593C, PT MK03-05624P, PT MK03-00389P, PT MK01-03641A, MK01-99437L AT "
                 "ALEXANDRA ROAD (QUEENSTOWN PLANNING AREA)")
JOB_NO = "5837723"


# --------------------------------------------------------------------------- #
# Text measure
# --------------------------------------------------------------------------- #
_FONTS: Dict[float, Any] = {}


def tw(text: str, h: float) -> float:
    """Width of a text of cap height h in the drawing font (same engine as the renderer)."""
    f = _FONTS.get(h)
    if f is None:
        f = _FONTS[h] = ezfonts.make_font("arial.ttf", h, WF)
    return f.text_width(text)


def wrap(text: str, h: float, width: float) -> List[str]:
    """Wrap on spaces to a width; a word longer than the width stays whole on its own line."""
    out: List[str] = []
    for para in str(text).split("\n"):
        cur = ""
        for w in para.split(" "):
            t = (cur + " " + w) if cur else w
            if tw(t, h) <= width or not cur:
                cur = t
            else:
                out.append(cur)
                cur = w
        out.append(cur)
    return out


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
csv.field_size_limit(10 ** 9)


def read_csv(p: Path) -> List[Dict[str, str]]:
    with open(p, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def choose_storey(appd: Dict[str, Any], capacity: int = 24) -> Tuple[int, str]:
    """L02 unless it crowds one A0 sheet, then the storey with the fewest App D systems."""
    n: Dict[int, int] = defaultdict(int)
    for s in appd["systems"]:
        n[s["storey"][0]] += 1
    if n[2] <= capacity:
        return 2, "L02 has %d App D systems and fits one sheet." % n[2]
    st = min(n, key=lambda k: (n[k], k))
    return st, ("L02 has %d App D systems, more than the %d that fit one sheet; storey %d has the fewest (%d)."
                % (n[2], capacity, st, n[st]))


class Value:
    """One value on the drawing with its change ID and source."""
    _n = 0

    def __init__(self, field: str, text: str, chg: str, src: str, owner: str):
        Value._n += 1
        self.id = "V%04d" % Value._n
        self.field, self.text, self.chg, self.src, self.owner = field, text, chg, src, owner


def fnum(v: str) -> str:
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else ("%g" % f)
    except (TypeError, ValueError):
        return v


def load_inputs(storey: int) -> Dict[str, Any]:
    appd = model.build_appd()
    systems = [s for s in appd["systems"] if s["storey"] == [storey] and s["kind"] != "appd_space_without_equipment"]
    unserved = [s for s in appd["systems"] if s["storey"] == [storey] and s["kind"] == "appd_space_without_equipment"]
    chg = read_csv(OUT / "change_register.csv")
    new_rows = {r["unit"]: r for r in chg if r["sheet"] == "App D" and r["layer_type"] == "new systems"}
    q_rows = [r for r in chg if r["sheet"] == "App D" and r["layer_type"] == "queries"
              and r["quantity"] == "Storey %d" % storey]
    spaces = {int(r["annotation_index"]): r for r in read_csv(REG / "appd_spaces.csv") if r["storey"] == str(storey)}
    recon = {r["appd_tag_as_written"]: r for r in read_csv(REG / "appd_schedule_reconciliation.csv")
             if r["appd_storeys"] == str(storey)}
    sp_q: Dict[int, List[Dict[str, str]]] = defaultdict(list)
    for r in chg:
        m = re.match(r"Space (\d+):", r["item"])
        if r["sheet"] == "App D" and m and r["quantity"] == "Storey %d" % storey:
            sp_q[int(m.group(1))].append(r)
    # highlighted spaces with no equipment: their query row, matched on page and box
    un_q: Dict[int, List[Dict[str, str]]] = defaultdict(list)
    for u in unserved:
        sp0 = u["spaces"][0]
        for r in chg:
            if r["sheet"] == "App D" and "space has no fan" in r["action"] and r["page"] == str(sp0["page"]):
                try:
                    bb = [float(v) for v in r["bbox"].strip("[]").split(",")]
                except ValueError:
                    continue
                if all(abs(a - b) < 0.6 for a, b in zip(bb, sp0["box"])):
                    un_q[int(sp0["annotation_index"])].append(r)
    air_rows = {r["item"]: r for r in chg if r["sheet"] == "App D" and r["layer_type"] == "airflows"
                and r["quantity"] == "Storey %d" % storey}
    sched_only = [r for r in chg if r["sheet"] == "App D" and r["layer_type"] == "queries"
                  and r["quantity"] == "Level %02d" % storey and "on no App D layout" in r["action"]]
    equip_rows = [r for r in read_csv(REG / "appd_equipment.csv") if r["storey"] == str(storey)]
    fan_types = {r["tag_as_written"]: r.get("Fan Type (1)", "") for r in read_csv(REG / "equipment_schedule_fans.csv")}
    return dict(appd=appd, systems=systems, unserved=unserved, new_rows=new_rows, q_rows=q_rows,
                spaces=spaces, recon=recon, chg=chg, d00=d00_cells(storey), sp_q=sp_q, fan_types=fan_types,
                un_q=un_q, equip_rows=equip_rows, air_rows=air_rows, sched_only=sched_only)


def d00_cells(storey: int) -> Dict[int, Dict[str, Any]]:
    """Per-space airflows of the common-areas workbook (tab Airflow) and set pressure (tab Pressure), with cells."""
    import glob
    import openpyxl
    from openpyxl.utils import get_column_letter
    wb = openpyxl.load_workbook(glob.glob(str(ROOT / "out/loads/workbooks/AIGH_C2_D00_*.xlsx"))[0], data_only=True, read_only=True)
    rows = list(wb["Airflow"].iter_rows(values_only=True))
    head = {h: i for i, h in enumerate(rows[4]) if h}
    prow = list(wb["Pressure"].iter_rows(values_only=True))
    ph = {h: i for i, h in enumerate(prow[4]) if h}
    press = {str(r[0]): (r[ph["Set pressure"]], n + 1, get_column_letter(ph["Set pressure"] + 1))
             for n, r in enumerate(prow) if n >= 6 and r[0] and "Set pressure" in ph}
    out: Dict[int, Dict[str, Any]] = {}
    for n, r in enumerate(rows):
        if n < 6 or not r[0]:
            continue
        m = re.match(r"CA-L%02d-(\d{3})$" % storey, str(r[0]))
        if not m:
            continue
        row = n + 1

        def cell(col: str):
            c = head.get(col)
            return (r[c], "%s%d" % (get_column_letter(c + 1), row)) if c is not None else (None, "")
        d = {"room": str(r[0]), "row": row, "ahu": r[head["AHU or system"]] if "AHU or system" in head else None}
        for k, col in (("name", "Room name"), ("supply", "Final supply"), ("return", "Return"), ("exhaust", "Exhaust")):
            d[k], d[k + "_cell"] = cell(col)
        pv = press.get(str(r[0]))
        d["pressure"], d["pressure_cell"] = (pv[0], "%s%d" % (pv[2], pv[1])) if pv and pv[0] is not None else (None, "")
        out[int(m.group(1))] = d
    return out


def plan_sheet(inp: Dict[str, Any], storey: int) -> Dict[str, Any]:
    """Systems, their equipment and the room boxes, each space once (under its first system)."""
    systems = sorted(inp["systems"], key=lambda s: s["system_id"])
    assigned: Dict[int, str] = {}
    for rel in ("equipment inside or nearest", "equipment tag within 60 pt of the space"):
        for s in systems:
            for sp in s["spaces"]:
                if sp["relation"] == rel and sp["annotation_index"] not in assigned:
                    assigned[sp["annotation_index"]] = s["system_id"]
    for u in inp["unserved"]:
        assigned.setdefault(u["spaces"][0]["annotation_index"], "UNSERVED")
    # tags by space, from the registers: inside = tags whose space is this space; near = tags within 60 pt, not inside
    inside: Dict[int, List[str]] = defaultdict(list)
    near: Dict[int, List[str]] = defaultdict(list)
    for e in inp["equip_rows"]:
        if e["space_annotation_index"] not in ("", None):
            i = int(float(e["space_annotation_index"]))
            if e["tag_as_written"] not in inside[i]:
                inside[i].append(e["tag_as_written"])
    for i, sp in inp["spaces"].items():
        for part in (sp["equipment_tags_within_60pt"] or "").split(";"):
            mm = re.match(r"\s*(\S+)\s*\[ann", part)
            if mm and mm.group(1) not in inside[i] and mm.group(1) not in near[i]:
                near[i].append(mm.group(1))
    return dict(systems=systems, assigned=assigned, inside=inside, near=near)


# --------------------------------------------------------------------------- #
# Legend symbols (read from the legend sheet)
# --------------------------------------------------------------------------- #
def legend_symbols() -> Dict[str, Any]:
    """The legend rows used, their cell boxes and the geometry found in them."""
    rows = [r for r in read_csv(REG / "acmv_legend.csv") if r["issue"] == "working" and r["kind"] == "symbol"]
    want = {"AIR HANDLING UNIT": "AHU", "AIRFLOW DIRECTION": "ARROW", "AXIAL FAN": "AXIAL",
            "CENTRIFUGAL FAN": "CENTRIF"}
    doc = pymupdf.open(LEGEND_PDF)
    page = doc[0]
    dr = page.get_drawings()
    out: Dict[str, Any] = {}
    for r in rows:
        key = want.get(r["description_or_meaning"])
        if not key:
            continue
        b = [float(v) for v in r["symbol_cell_pt"].strip("()").split(",")]
        cell = pymupdf.Rect(*b)
        paths = [q for q in dr if q["rect"].intersects(cell)]
        out[key] = dict(cell=cell, paths=paths, row=r)
    return out


# --------------------------------------------------------------------------- #
# DXF building
# --------------------------------------------------------------------------- #
class Sheet:
    def __init__(self, storey: int, number: str, idx: int = 1, total: int = 1, numbers: Optional[List[str]] = None):
        self.storey, self.number = storey, number
        self.idx, self.total, self.numbers = idx, total, numbers or [number]
        self.shown: set = set()
        self.ins: List[Tuple[float, float, float, float, str]] = []
        self.doc = ezdxf.new("R2018", setup=True)
        self.doc.units = ezdxf.units.MM
        self.msp = self.doc.modelspace()
        self.doc.layout("Layout1").page_setup(size=(SHEET_W, SHEET_H), margins=(0, 0, 0, 0), units="mm")
        self.doc.appids.add(APPID)
        self.doc.styles.add(STYLE, font="arial.ttf")
        for name, col in LAYERS.items():
            self.doc.layers.add(name, color=col)
        for k, rgb in DUCT_RGB.items():
            ly = self.doc.layers.get(DUCT_LAYER[k])
            ly.rgb = rgb
        self.values: List[Value] = []
        self.texts: List[Dict[str, Any]] = []     # every text with box info for the checks
        self.ducts: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
        self.symbols: List[Tuple[float, float, float, float, str]] = []

    # --- primitives
    def text(self, x: float, y: float, s: str, h: float = H, layer: str = "IP-ROOMTEXT", value: Optional[Value] = None,
             part: Tuple[int, int] = (0, 1), align: str = "left", owner: str = "", color: Optional[int] = None) -> None:
        if not s:
            return
        w = tw(s, h)
        x0 = x if align == "left" else (x - w if align == "right" else x - w / 2)
        t = self.msp.add_text(s, dxfattribs=dict(layer=layer, height=h, style=STYLE, width=WF, insert=(x0, y)))
        if color is not None:
            t.dxf.color = color
        if value is not None:
            t.set_xdata(APPID, [(1000, "VAL=" + value.id), (1000, "FIELD=" + value.field), (1000, "CHG=" + value.chg),
                                (1000, "SRC=" + value.src), (1000, "PART=%d/%d" % part)])
        self.texts.append(dict(x0=x0, y0=y - 0.3 * h, x1=x0 + w, y1=y + h, s=s, layer=layer, owner=owner))

    def value_text(self, x: float, y_top: float, v: Value, h: float, width: float, layer: str = "IP-ROOMTEXT",
                   owner: str = "", label: str = "", label_w: float = 0.0) -> float:
        """Wrapped value; returns the y below its last line.  Value text (not the label) holds the XDATA."""
        self.values.append(v)
        pitch = h * LP
        lines = wrap(v.text, h, width - label_w)
        y = y_top - h
        if label:
            self.text(x, y, label, HS, layer, owner=owner)
        for i, ln in enumerate(lines):
            self.text(x + label_w, y, ln, h, layer, v, (i, len(lines)), owner=owner)
            y -= pitch
        return y + pitch - 0.4 * h        # just below the last line

    def line(self, p0, p1, layer: str, dashed: bool = False) -> None:
        a = dict(layer=layer)
        if dashed:
            a["linetype"] = "DASHED"
            a["ltscale"] = 3.0
        self.msp.add_line(p0, p1, dxfattribs=a)
        if layer.startswith("IP-DUCT"):
            self.ducts.append((p0, p1))

    def rect(self, x0, y0, x1, y1, layer: str):
        self.msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True, dxfattribs=dict(layer=layer))

    def duct_lw(self, layer: str) -> None:
        pass


def make_blocks(sh: Sheet, sym: Dict[str, Any]) -> None:
    """Blocks: legend symbols (AHU outline, airflow arrow), box frame, labelled fan box."""
    doc = sh.doc
    # AHU: the legend gives a plain rectangle with the label AHU; we draw its outline at drawing scale
    ahu = doc.blocks.new("SYM_AHU")
    ahu.add_lwpolyline([(0, 0), (1, 0), (1, 1), (0, 1)], close=True, dxfattribs=dict(layer="IP-EQUIP", lineweight=35))
    # airflow arrow: the legend arrow, normalised to length 1 along +x
    arrow = doc.blocks.new("SYM_ARROW")
    a = sym.get("ARROW")
    if a:
        cell: pymupdf.Rect = a["cell"]
        pts = [q for q in a["paths"]]
        xs = [v for q in pts for v in (q["rect"].x0, q["rect"].x1)]
        x0, x1 = min(xs), max(xs)
        ys = [v for q in pts for v in (q["rect"].y0, q["rect"].y1)]
        ymid = (min(ys) + max(ys)) / 2
        sc = 1.0 / max(1e-6, (x1 - x0))
        for q in pts:
            if q.get("fill") and q["type"] in ("f", "fs"):
                poly = []
                for it in q["items"]:
                    if it[0] == "l":
                        poly += [it[1], it[2]]
                seen = []
                for p in poly:
                    if not seen or (abs(seen[-1][0] - p[0]) > 1e-6 or abs(seen[-1][1] - p[1]) > 1e-6):
                        seen.append(p)
                if len(seen) >= 3:
                    h = arrow.add_hatch(color=0, dxfattribs=dict(layer="IP-DUCT-SA"))
                    h.paths.add_polyline_path([((p[0] - x0) * sc, -(p[1] - ymid) * sc) for p in seen], is_closed=True)
            else:
                for it in q["items"]:
                    if it[0] == "l":
                        arrow.add_line(((it[1][0] - x0) * sc, -(it[1][1] - ymid) * sc),
                                       ((it[2][0] - x0) * sc, -(it[2][1] - ymid) * sc))
    # fan symbols of the legend, normalised to a unit box (lines and curves of the legend cell)
    for key, bname in (("AXIAL", "SYM_FAN_AXIAL"), ("CENTRIF", "SYM_FAN_CENTRIF")):
        g = sym.get(key)
        if not g:
            continue
        ps = [q for q in g["paths"] if q["rect"].width < g["cell"].width * 0.8]
        xs = [v for q in ps for v in (q["rect"].x0, q["rect"].x1)]
        ys = [v for q in ps for v in (q["rect"].y0, q["rect"].y1)]
        bx0, bx1, by0, by1 = min(xs), max(xs), min(ys), max(ys)
        blk = doc.blocks.new(bname)

        def nrm(pt, bx0=bx0, bx1=bx1, by0=by0, by1=by1):
            return ((pt[0] - bx0) / (bx1 - bx0), 1 - (pt[1] - by0) / (by1 - by0))
        for q in ps:
            for it in q["items"]:
                if it[0] == "l":
                    blk.add_line(nrm(it[1]), nrm(it[2]), dxfattribs=dict(layer="IP-EQUIP"))
                elif it[0] == "c":
                    p0, p1, p2, p3 = it[1:5]
                    pts = []
                    for k in range(9):
                        t = k / 8
                        pts.append(((1 - t) ** 3 * p0[0] + 3 * (1 - t) ** 2 * t * p1[0] + 3 * (1 - t) * t * t * p2[0] + t ** 3 * p3[0],
                                    (1 - t) ** 3 * p0[1] + 3 * (1 - t) ** 2 * t * p1[1] + 3 * (1 - t) * t * t * p2[1] + t ** 3 * p3[1]))
                    for a_, b_ in zip(pts, pts[1:]):
                        blk.add_line(nrm(a_), nrm(b_), dxfattribs=dict(layer="IP-EQUIP"))
                elif it[0] == "re":
                    r_ = it[1]
                    c4 = [nrm((r_.x0, r_.y0)), nrm((r_.x1, r_.y0)), nrm((r_.x1, r_.y1)), nrm((r_.x0, r_.y1))]
                    blk.add_lwpolyline(c4, close=True, dxfattribs=dict(layer="IP-EQUIP"))
        sh.sym_aspect = getattr(sh, "sym_aspect", {})
        sh.sym_aspect[bname] = (bx1 - bx0) / (by1 - by0)
    # labelled box (for symbols the legend does not have)
    lb = doc.blocks.new("BOX_LABELLED")
    lb.add_lwpolyline([(0, 0), (1, 0), (1, 1), (0, 1)], close=True, dxfattribs=dict(layer="IP-EQUIP", lineweight=25))
    fcu = doc.blocks.new("SYM_FCU")        # legend symbol FAN COIL UNIT: a plain rectangle, as the legend cell
    fcu.add_lwpolyline([(0, 0), (1, 0), (1, 1), (0, 1)], close=True, dxfattribs=dict(layer="IP-EQUIP", lineweight=35))
    rb = doc.blocks.new("ROOMBOX")
    rb.add_lwpolyline([(0, 0), (1, 0), (1, 1), (0, 1)], close=True, dxfattribs=dict(layer="IP-ROOMBOX", lineweight=25))
    # level mark (triangle) as on the working drawings
    lv = doc.blocks.new("LEVEL_MARK")
    lv.add_lwpolyline([(0, 0), (4, 0), (2, -3.5)], close=True, dxfattribs=dict(layer="IP-LEVELS"))


def insert(sh: Sheet, block: str, x: float, y: float, sx: float, sy: float, layer: str, rot: float = 0.0):
    if block in ("ROOMBOX", "SYM_AHU", "SYM_FCU", "BOX_LABELLED"):
        sh.ins.append((x, y, x + sx, y + sy, block))
    sh.msp.add_blockref(block, (x, y), dxfattribs=dict(layer=layer, xscale=sx, yscale=sy, rotation=rot))


def arrow_at(sh: Sheet, x: float, y: float, layer: str, length: float = 7.0, rot: float = 0.0):
    insert(sh, "SYM_ARROW", x, y, length, length, layer, rot)
    sh.symbols.append((x - 0.5, y - 2, x + length + 0.5, y + 2, "arrow") if rot == 0 else
                      (x - 2, y - 0.5, x + 2, y + length + 0.5, "arrow"))


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
BW = 72.0          # room box width
PITCH = 74.0
GAP = 6.0


NOCHG = "CH-APPD-NONE"      # the change register has no row for this item (listed in the report)


def system_chg(inp, system: Dict[str, Any]) -> str:
    r = inp["new_rows"].get(system["system_id"])
    return r["change_id"] if r else NOCHG


def box_content(inp, plan, sp_idx: int, system: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    sp = inp["spaces"][sp_idx]
    if system is None:           # highlighted space with no equipment on the layout
        uq = inp["un_q"].get(sp_idx, [])
        return dict(idx=sp_idx, sp=sp, chg=uq[0]["change_id"] if uq else NOCHG, system=None,
                    name=box_name(inp, sp_idx), queries=list(uq) + inp["sp_q"].get(sp_idx, []), relation="",
                    tags_in=[], tags_near=[])
    return dict(idx=sp_idx, sp=sp, chg=system_chg(inp, system), system=system, name=box_name(inp, sp_idx),
                queries=inp["sp_q"].get(sp_idx, []),
                relation=next((x["relation"] for x in system["spaces"] if x["annotation_index"] == sp_idx), ""),
                tags_in=plan["inside"].get(sp_idx, []), tags_near=plan["near"].get(sp_idx, []))


def draw_box(sh: Sheet, c: Dict[str, Any], x: float, y_top: float, h_box: float, inp, plan) -> None:
    sp = c["sp"]
    idx = c["idx"]
    d0 = inp["d00"].get(idx) or {}
    ref = "SPACE %d" % idx
    src = "Appendix D, %s, p%s, annotation %d" % (sp["drawing_number"], sp["page"], idx)
    chg = c["chg"]
    owner = "box:%d" % idx
    insert(sh, "ROOMBOX", x, y_top - h_box, BW, h_box, "IP-ROOMBOX")
    ix, w = x + 1.6, BW - 3.2
    y = y_top - 1.2
    v_ref = Value("space_ref", ref, chg, src, owner)
    y = sh.value_text(ix, y, v_ref, HS, w, owner=owner) - 0.9
    nsrc = "Workbook D00 tab Airflow cell %s; space %d" % (d0.get("name_cell", ""), idx)
    v_name = Value("name", box_name(inp, idx), chg, nsrc if d0 else src, owner)
    y = sh.value_text(ix, y, v_name, H + 0.1, w, owner=owner) - 1.1
    v_cnt = Value("count", "1", chg, src, owner)
    area = fnum(sp["printed_area_m2"] or sp["area_m2_at_1_250"])
    v_area = Value("area_m2", area, chg, src, owner)
    sh.values += [v_cnt, v_area]
    sh.text(ix, y - H, "COUNT", HS, owner=owner)
    sh.text(ix + 14, y - H, v_cnt.text, H, value=v_cnt, owner=owner)
    sh.text(ix + 22, y - H, "AREA", HS, owner=owner)
    sh.text(ix + 33, y - H, v_area.text + " m2", H, value=v_area, owner=owner)
    y -= H * LP + 0.4
    v_mode = Value("mode", sp["mode_label"], chg, src, owner)
    sh.values.append(v_mode)
    sh.text(ix, y - H, "MODE", HS, owner=owner)
    sh.text(ix + 14, y - H, v_mode.text, H, value=v_mode, owner=owner)
    y -= H * LP + 0.4

    def wv(field, key, unit_note=""):
        val = d0.get(key)
        t = "not given" if val is None else fnum(str(round(val, 1) if isinstance(val, float) else val))
        s_ = "Workbook D00 tab %s cell %s; space %d" % ("Pressure" if key == "pressure" else "Airflow", d0.get(key + "_cell", ""), idx) \
            if val is not None else src
        return Value(field, t, chg, s_, owner)
    vs = [wv("supply_L_s", "supply"), wv("return_L_s", "return"), wv("exhaust_L_s", "exhaust")]
    vp = wv("pressure", "pressure")
    sh.values += vs + [vp]
    sh.text(ix, y - H, "SA L/S", HS, owner=owner)
    sh.text(ix + 14, y - H, vs[0].text, H, value=vs[0], owner=owner)
    sh.text(ix + 36, y - H, "RA L/S", HS, owner=owner)
    sh.text(ix + 52, y - H, vs[1].text, H, value=vs[1], owner=owner)
    y -= H * LP + 0.4
    sh.text(ix, y - H, "EA L/S", HS, owner=owner)
    sh.text(ix + 14, y - H, vs[2].text, H, value=vs[2], owner=owner)
    sh.text(ix + 36, y - H, "PRESS PA", HS, owner=owner)
    sh.text(ix + 52, y - H, vp.text, H, value=vp, owner=owner)
    y -= H * LP + 0.6
    if c["tags_in"]:
        v = Value("tags_inside", ", ".join(c["tags_in"]), chg, src, owner)
        y = sh.value_text(ix, y, v, H, w, owner=owner, label="TAGS", label_w=tw("NEAR", HS) + 2.0) - 0.8
    if c["tags_near"]:
        v = Value("tags_near", ", ".join(c["tags_near"]), chg, src, owner)
        y = sh.value_text(ix, y, v, H, w, owner=owner, label="NEAR", label_w=tw("NEAR", HS) + 2.0) - 0.8
    for q in c["queries"]:
        for ln in wrap("QUERY %s: %s. %s" % (q["change_id"], q["action"].replace("Query: ", ""), q["note"]), HS, w):
            sh.text(ix, y - HS, ln, HS, "IP-QUERY", owner=owner + ":q")
            y -= HS * LP
    v = Value("change_id", chg, chg, src, owner)
    sh.values.append(v)
    sh.text(ix, y_top - h_box + 1.2 + HS * LP, "CHG " + chg, HS, "IP-ANNO-ID", v, owner=owner)
    sh.text(ix, y_top - h_box + 1.2, "APP D SPACE %d" % idx + (", NO EQUIPMENT" if c["system"] is None else
                                      (", TAG NEAR" if c["relation"] != "equipment inside or nearest" else "")),
            HS, "IP-ANNO-ID", owner=owner)


def box_name(inp, idx: int) -> str:
    d0 = inp["d00"].get(idx)
    if d0 and d0.get("name"):
        return str(d0["name"])
    return re.sub(r"\s*\|\s*", " ", re.sub(r"\b(AC \(24HR\)|[FS]FL: ?[+\-0-9.]+|PLL|BLL|\?)\b", "", inp["spaces"][idx]["space_names_text_layer"])).strip() or "SPACE"


def box_height(c: Dict[str, Any]) -> float:
    w = BW - 3.2
    n_name = len(wrap(c.get("name") or c["sp"]["space_names_text_layer"], H + 0.1, w))
    n_in = len(wrap(", ".join(c["tags_in"]), H, w)) if c["tags_in"] else 0
    n_near = len(wrap(", ".join(c["tags_near"]), H, w)) if c["tags_near"] else 0
    lines = 1.1 + HS * LP + 0.9 + n_name * (H + 0.1) * LP + 1.1 + 4 * (H * LP + 0.4) + 0.2 + \
        (n_in * H * LP + 0.8 if n_in else 0) + (n_near * H * LP + 0.8 if n_near else 0) + 2 * HS * LP + 3.0
    nq = sum(len(wrap("QUERY %s: %s. %s" % (q["change_id"], q["action"], q["note"]), HS, w)) for q in c.get("queries", []))
    return round(lines + 0.5 + nq * HS * LP, 1)


KIND_ORDER = ["SA", "RA", "OA", "EA", "TEA"]
FEEDS = {"OAF": ["OA"], "EAF": ["EA"], "TEAF": ["TEA"], "FCU": ["SA", "RA"], "AHU": ["SA", "RA"],
         "OAF/EAF": ["OA", "EA"], "other": ["OA", "EA"]}


def tag_feeds(tag: str, etype: str) -> List[str]:
    if tag.startswith("OAF/EAF"):
        return FEEDS["OAF/EAF"]
    return FEEDS.get(etype, ["EA"])


def duct_pair(system: Dict[str, Any]) -> List[str]:
    kinds = set()
    for e in system["equipment"]:
        kinds.update(tag_feeds(e["tag_as_written"], e["type"]))
    return [k for k in KIND_ORDER if k in kinds]


def equipment_values(inp, system, e) -> Dict[str, Any]:
    """Duty, pressure and the schedule tag for one equipment tag, from the schedule reconciliation."""
    r = inp["recon"].get(e["tag_as_written"])
    return r or {}


def fan_symbol(inp, tag: str, system) -> Optional[str]:
    r = inp["recon"].get(tag, {})
    t = inp["fan_types"].get(r.get("schedule_tag_as_written", "").split(" | ")[0], "") or inp["fan_types"].get(tag, "")
    return {"Axial": "SYM_FAN_AXIAL", "Centrif SISW": "SYM_FAN_CENTRIF"}.get(t)


def draw_fan_box(sh: Sheet, inp, system, tag: str, etype: str, x: float, y_top: float, w: float, chg: str,
                 qrows: List[Dict[str, str]]) -> float:
    """Fan: the legend symbol where the schedule gives the fan type (axial, centrifugal), else a labelled box."""
    r = inp["recon"].get(tag, {})
    owner = "eq:" + tag
    src = "Appendix D, p5, tag as written; schedule row %s" % (r.get("schedule_pages") and ("p" + r["schedule_pages"]) or "none")
    blk = fan_symbol(inp, tag, system)
    is_fcu = etype == "FCU"
    xo = 14.0 if blk else 1.6
    lines: List[Tuple[str, Optional[Value], float]] = []
    vtag = Value("tag", tag, chg, src, owner)
    lines.append((tag, vtag, HT))
    if blk:
        ftype = "AXIAL" if blk == "SYM_FAN_AXIAL" else "CENTRIFUGAL"
        v = Value("fan_type", ftype, chg, "Equipment schedule, column Fan Type", owner)
        lines.append(("FAN TYPE %s" % ftype, v, H))
    if r.get("airflow") and is_fcu:
        v = Value("supply_airflow_L_s", fnum(r["airflow"]), chg, src, owner)
        lines.append(("SUPPLY AIRFLOW %s L/s" % v.text, v, H))
    elif r.get("airflow"):
        v = Value("airflow_L_s", fnum(r["airflow"]), chg, src, owner)
        lines.append(("MIN AIRFLOW %s L/s" % v.text, v, H))
    ar = inp["air_rows"].get(tag)
    if ar:      # D-072: the workbook value is larger than the scheduled duty, show it
        cid = ar["change_id"]
        v = Value("workbook_airflow_L_s", fnum(ar["required"]), cid, ar["source"], owner)
        lines.append(("WORKBOOK AIRFLOW %s L/s" % v.text, v, H))
        vc2 = Value("change_id", cid, cid, ar["source"], owner)
        lines.append(("CHG " + cid, vc2, HS))
    if r.get("static_pressure_Pa"):
        v = Value("static_pressure_Pa", fnum(r["static_pressure_Pa"]), chg, src, owner)
        lines.append(("MIN STATIC PRESSURE %s Pa" % v.text, v, H))
    if r.get("schedule_tag_as_written"):
        v = Value("schedule_tag", r["schedule_tag_as_written"], chg, src, owner)
        lines.append(("SCHEDULE %s" % v.text, v, H))
    elif not r:
        lines.append(("NOT IN THE SCHEDULE", None, H))
    for q in qrows:
        lines.append(("QUERY %s: %s" % (q["change_id"], q["action"].replace("Query: ", "")), None, HS))
    wrapped: List[Tuple[str, Optional[Value], float, int]] = []
    ht = 2.0
    for s_, v, hh in lines:
        parts = wrap(s_, hh, w - xo - 1.6)
        ht += len(parts) * hh * LP + 0.3
        wrapped.append((s_, v, hh, len(parts)))
    ht += 2 * HS * LP + 2.4
    if blk:
        ht = max(ht, 22.0)
        sy = 15.0
        sxw = sy * sh.sym_aspect[blk]
        insert(sh, blk, x + 1.0, y_top - ht / 2 - sy / 2, sxw, sy, "IP-EQUIP")
        sh.symbols.append((x + 1.0, y_top - ht / 2 - sy / 2, x + 1.0 + sxw, y_top - ht / 2 + sy / 2, "legend fan"))
    elif is_fcu:
        insert(sh, "SYM_FCU", x, y_top - ht, w, ht, "IP-EQUIP")
        sh.symbols.append((x, y_top - ht, x + w, y_top, "legend FCU"))
    else:
        insert(sh, "BOX_LABELLED", x, y_top - ht, w, ht, "IP-EQUIP")
        sh.symbols.append((x, y_top - ht, x + w, y_top, "labelled box"))
    y = y_top - 1.2
    first = True
    for s_, v, hh, n in wrapped:
        parts = wrap(s_, hh, w - xo - 1.6)
        if v is not None:
            sh.values.append(v)
        for i, ptxt in enumerate(parts):
            y -= hh if first else hh * LP
            first = False
            if v is not None:
                sh.text(x + xo, y, ptxt, hh, "IP-EQUIPTEXT", v, (i, len(parts)), owner=owner + ":box")
            else:
                sh.text(x + xo, y, ptxt, hh, "IP-QUERY" if hh == HS else "IP-EQUIPTEXT", owner=owner + ":box")
        y -= 0.3
    vc = Value("change_id", chg, chg, src, owner)
    sh.values.append(vc)
    sh.text(x + xo, y_top - ht + 1.2 + HS * LP, "CHG " + chg, HS, "IP-ANNO-ID", vc, owner=owner + ":box")
    sh.text(x + xo, y_top - ht + 1.2, "SCHEDULE PAGE %s" % (r.get("schedule_pages") or "-"), HS, "IP-ANNO-ID", owner=owner + ":box")
    return ht


def draw_unserved_group(sh, inp, plan, bx, gx, ytop, ncols) -> None:
    """Highlighted spaces with no fan, FCU or AHU on the layout: boxes with no duct line."""
    sh.text(gx, ytop - 3.0, "HIGHLIGHTED SPACES WITH NO FAN, FCU OR AHU ON THE LAYOUT. NO DUCT IS DRAWN.", 3.0, "IP-NOTES",
            owner="unserved")
    y = ytop - 9.0
    bottoms = []
    for rr in range(math.ceil(len(bx) / ncols)):
        chunk = bx[rr * ncols:(rr + 1) * ncols]
        hrow = max(box_height(c) for c in chunk)
        for j, c in enumerate(chunk):
            draw_box(sh, c, gx + j * PITCH, y, hrow, inp, plan)
        bottoms.append(y - hrow)
        y -= hrow + 10.0
    sh.last_bottom = min(bottoms) - 1.0


def draw_sched_table(sh, inp, rows, gx, ytop) -> None:
    """Scheduled for the common areas of this storey, on no App D layout: listed with the reason, not drawn."""
    colw = 250.0
    ncol = 1 if len(rows) <= 12 else (2 if len(rows) <= 44 else 3)
    per = math.ceil(len(rows) / ncol)
    head = ("SCHEDULED FOR THE COMMON AREAS OF THIS STOREY, ON NO APP D LAYOUT. NOT DRAWN: NO SPACE IS TIED TO THEM "
            "ON THE LAYOUT.")
    y0 = ytop
    for i, ln in enumerate(wrap(head, 3.0, ncol * colw - 4)):
        sh.text(gx, y0 - 3.0 - i * 4.4, ln, 3.0, "IP-NOTES", owner="schedtable")
    y0 -= 3.0 + 4.4 * len(wrap(head, 3.0, ncol * colw - 4)) + 3.0
    bottoms = [y0]
    for c in range(ncol):
        y = y0
        for r in rows[c * per:(c + 1) * per]:
            x = gx + c * colw
            owner = "sched:" + r["item"]
            vt = Value("sched_only_tag", r["item"], r["change_id"], r["source"], owner)
            vc = Value("change_id", r["change_id"], r["change_id"], r["source"], owner)
            sh.values += [vt, vc]
            sh.text(x, y - H, r["item"], H, "IP-ROOMTEXT", vt, owner=owner)
            sh.text(x + 66, y - H, "CHG " + r["change_id"], HS, "IP-ANNO-ID", vc, owner=owner)
            pg = re.search(r"p([\d+]+)\s*$", r["source"])
            sh.text(x + 98, y - H, "SCHEDULE P" + (pg.group(1) if pg else "-"), HS, "IP-ANNO-ID", owner=owner)
            lines = wrap(r["note"], HS, colw - 134)
            for i, ln in enumerate(lines):
                sh.text(x + 130, y - HS - i * HS * LP, ln, HS, "IP-ROOMTEXT", owner=owner)
            y -= max(H * LP, len(lines) * HS * LP) + 1.6
        bottoms.append(y)
    sh.last_bottom = min(bottoms) - 1.0


class Block:
    """One piece of the sheet: a system with its boxes, drawn at a given top-left corner."""

    def __init__(self, bid: str, kind: str, draw, spaces: Sequence[int], tags: Sequence[str]):
        self.bid, self.kind, self.draw = bid, kind, draw
        self.spaces, self.tags = list(spaces), list(tags)
        self.w = self.h = self.xmin = 0.0


def measure(scratch: "Sheet", blk: Block) -> None:
    scratch.msp.delete_all_entities()
    scratch.texts, scratch.ducts, scratch.symbols, scratch.ins, scratch.values = [], [], [], [], []
    n0 = Value._n
    blk.draw(scratch, 0.0, 0.0)
    Value._n = n0
    xs, ys = [], []
    for t in scratch.texts:
        xs += [t["x0"], t["x1"]]
        ys += [t["y0"], t["y1"]]
    for (p0, p1) in scratch.ducts:
        xs += [p0[0], p1[0]]
        ys += [p0[1], p1[1]]
    for (x0, y0, x1, y1, _n) in scratch.ins:
        xs += [x0, x1]
        ys += [y0, y1]
    blk.xmin = min(xs) - 1.0
    blk.w = max(xs) - min(xs) + 2.0
    blk.h = (0.0 - min(ys)) + 1.0


def build_blocks(inp, plan, scratch) -> Tuple[List[Block], Dict[str, Any]]:
    systems = plan["systems"]
    by_id = {s["system_id"]: s for s in systems}
    boxes: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for idx in sorted(plan["assigned"]):
        sid = plan["assigned"][idx]
        boxes[sid].append(box_content(inp, plan, idx, None if sid == "UNSERVED" else by_id[sid]))
    qmap: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for q in inp["q_rows"]:
        qmap[q["item"]].append(q)
    hmax = Y_TOP - Y_MIN
    out: List[Block] = []
    for s in systems:
        sid = s["system_id"]
        bx = boxes[sid]
        spaces = [c["idx"] for c in bx]
        tags = sorted({e["tag_as_written"] for e in s["equipment"]})
        if s["kind"] == "appd_ahu_system":
            chosen = None
            for nc in range(6, 14):
                blk = Block(sid, "ahu", lambda sh, gx, yt, s=s, bx=bx, nc=nc: draw_ahu_group(sh, inp, plan, s, bx, gx, yt, nc, qmap),
                            spaces, tags)
                measure(scratch, blk)
                chosen = blk
                if blk.h <= hmax * 0.75 and blk.w <= X_MAX - X0_PAGE:
                    break
            out.append(chosen)
        else:
            blk = Block(sid, "fan", lambda sh, gx, yt, s=s, bx=bx: draw_fan_group(sh, inp, plan, s, bx, gx, yt, qmap),
                        spaces, tags)
            measure(scratch, blk)
            out.append(blk)
    if boxes.get("UNSERVED"):
        bx = boxes["UNSERVED"]
        for nc in range(6, 14):
            blk = Block("UNSERVED", "unserved", lambda sh, gx, yt, bx=bx, nc=nc: draw_unserved_group(sh, inp, plan, bx, gx, yt, nc),
                        [c["idx"] for c in bx], [])
            measure(scratch, blk)
            if blk.h <= hmax * 0.75 and blk.w <= X_MAX - X0_PAGE:
                break
        out.append(blk)
    if inp["sched_only"]:
        rows = sorted(inp["sched_only"], key=lambda r: r["item"])
        blk = Block("SCHEDTABLE", "table", lambda sh, gx, yt: draw_sched_table(sh, inp, rows, gx, yt), [],
                    [r["item"] for r in rows])
        measure(scratch, blk)
        out.append(blk)
    return out, dict(boxes=boxes, qmap=qmap)


def paginate(blocks: List[Block]) -> List[List[List[Tuple[Block, float]]]]:
    """Sheets of bands (shelves) of blocks.  A band is a list of (block, x of its left edge)."""
    ahu = sorted([b for b in blocks if b.kind == "ahu"], key=lambda b: (-b.h, b.bid))
    fans = sorted([b for b in blocks if b.kind == "fan"], key=lambda b: (-round(b.h / 30.0), b.bid))
    rest_u = [b for b in blocks if b.kind == "unserved"]
    rest_t = [b for b in blocks if b.kind == "table"]
    order = ahu + fans + rest_u + rest_t
    sheets: List[List[List[Tuple[Block, float]]]] = []
    bands: List[List[Tuple[Block, float]]] = []
    band: List[Tuple[Block, float]] = []
    y_top = Y_TOP
    x = X0_PAGE
    row_h = 0.0

    def new_sheet():
        nonlocal bands, band, y_top, x, row_h
        if band:
            bands.append(band)
        if bands:
            sheets.append(bands)
        bands, band, y_top, x, row_h = [], [], Y_TOP, X0_PAGE, 0.0
    for b in order:
        gapx = 8.0
        fits_here = band and x + b.w <= X_MAX and y_top - max(row_h, b.h) >= Y_MIN
        if not fits_here and band:
            ny = y_top - row_h - SHELF_GAP
            if ny - b.h >= Y_MIN and b.w <= X_MAX - X0_PAGE:
                bands.append(band)
                band, y_top, x, row_h = [], ny, X0_PAGE, 0.0
            else:
                new_sheet()
        band.append((b, x))
        x += b.w + gapx
        row_h = max(row_h, b.h)
    if band:
        bands.append(band)
    if bands:
        sheets.append(bands)
    return sheets


def build_storey(storey: int, numbers_fn) -> List[Tuple["Sheet", Dict[str, Any]]]:
    """All sheets of one storey.  numbers_fn(n_sheets) returns the provisional drawing numbers."""
    inp = load_inputs(storey)
    plan = plan_sheet(inp, storey)
    sym = legend_symbols()
    scratch = Sheet(storey, "SCRATCH")
    make_blocks(scratch, sym)
    blocks, extra = build_blocks(inp, plan, scratch)
    sheets_layout = paginate(blocks)
    numbers = numbers_fn(len(sheets_layout))
    out = []
    for i, bands in enumerate(sheets_layout):
        Value._n = 0
        sh = Sheet(storey, numbers[i], i + 1, len(sheets_layout), numbers)
        make_blocks(sh, sym)
        frame(sh)
        y_top = Y_TOP
        level_ys: List[float] = []
        for band in bands:
            hmax = max(b.h for b, _x in band)
            for b, bx0 in band:
                b.draw(sh, bx0 - b.xmin, y_top)
                sh.shown.update(b.spaces)
            level_ys.append(y_top - hmax - 6.0)
            y_top = y_top - hmax - SHELF_GAP
        sh.band_bottom = level_ys[-1] + 6.0
        levels(sh, level_ys)
        sh.sched_rows = bool(inp["sched_only"]) and any(b.kind == "table" for band in bands for b, _ in band)
        sh.block_ids = [b.bid for band in bands for b, _ in band]
        strip(sh, inp, plan, sym)
        key_plan(sh, inp, plan)
        legend_list(sh, sym, sh.band_bottom)
        caption(sh)
        out.append((sh, dict(inp=inp, plan=plan, sym=sym, blocks=[b for band in bands for b, _ in band])))
    return out


def draw_ahu_group(sh, inp, plan, s, bx, gx, ytop, ncols, qmap) -> None:
    chg = system_chg(inp, s)
    tag = s["equipment"][0]["tag_as_written"]
    r = inp["recon"].get(tag, {})
    owner = "eq:" + tag
    src = "Appendix D, p5, tag as written; schedule row p%s" % (r.get("schedule_pages") or "none")
    aw, ah = 130.0, 54.0
    width = max(min(ncols, len(bx)) * PITCH, aw + 130)
    ax = gx + (width - aw) / 2
    ay = ytop - ah
    insert(sh, "SYM_AHU", ax, ay, aw, ah, "IP-EQUIP")
    sh.symbols.append((ax, ay, ax + aw, ay + ah, "AHU symbol"))
    y = ytop - 2.0
    lines: List[Tuple[str, Optional[Value], float, str]] = []
    vt = Value("tag", tag, chg, src, owner)
    lines.append((tag, vt, HT, ""))
    n_lab = len(s["equipment"])
    if r.get("airflow"):
        v = Value("supply_airflow_L_s", fnum(r["airflow"]), chg, src, owner)
        lines.append(("SUPPLY AIRFLOW %s L/s" % v.text, v, H, ""))
    if r.get("outdoor_air"):
        v = Value("outdoor_air", r["outdoor_air"], chg, src, owner)
        lines.append(("OUTDOOR AIRFLOW %s L/s" % v.text, v, H, ""))
    if r.get("static_pressure_Pa"):
        v = Value("external_resistance_Pa", fnum(r["static_pressure_Pa"]), chg, src, owner)
        lines.append(("EXTERNAL RESISTANCE %s Pa" % v.text, v, H, ""))
    if r.get("schedule_tag_as_written"):
        v = Value("schedule_tag", r["schedule_tag_as_written"], chg, src, owner)
        lines.append(("SCHEDULE %s" % v.text, v, H, ""))
    for q in qmap.get(tag, []):
        lines.append(("QUERY %s: %s" % (q["change_id"], q["action"].replace("Query: ", "")), None, HS, "q"))
    y = ytop - 1.5
    first = True
    for st, v, hh, _ in lines:
        parts = wrap(st, hh, aw - 3.2)
        if v is not None:
            sh.values.append(v)
        for i, ptxt in enumerate(parts):
            y -= hh if first else hh * LP
            first = False
            sh.text(ax + 1.6, y, ptxt, hh, "IP-QUERY" if _ == "q" else "IP-EQUIPTEXT", v, (i, len(parts)), owner="sym:AHU")
        y -= 0.6
    assert y > ay + 4, "AHU label does not fit its symbol: %s %.1f %.1f" % (tag, y, ay)
    vc = Value("change_id", chg, chg, src, owner)
    sh.values.append(vc)
    sh.text(ax + aw + 3, ay + 1.2 + HS * LP, "CHG " + chg, HS, "IP-ANNO-ID", vc, owner=owner)
    sh.text(ax + aw + 3, ay + 1.2, "SRC AppD p5, schedule p%s" % r.get("schedule_pages", "-"), HS, "IP-ANNO-ID", owner=owner)
    # headers
    cols = min(ncols, len(bx))
    rows_n = math.ceil(len(bx) / ncols)
    hdr_y0 = ay - 12.0
    sa_x = ax + aw / 2 - 14
    ra_x = ax + aw / 2 + 14
    hy = {"SA": hdr_y0, "RA": hdr_y0 - 5.0}
    # feeders from AHU
    sh.line((sa_x, ay), (sa_x, hy["SA"]), DUCT_LAYER["SA"])
    sh.line((ra_x, ay), (ra_x, hy["RA"]), DUCT_LAYER["RA"])
    # rows of boxes
    y_row_top = hdr_y0 - 14.0
    riser_sa = gx - 5.0
    riser_ra = gx - 8.5
    row_tops = []
    y = y_row_top
    heights = []
    for rr in range(rows_n):
        chunk = bx[rr * ncols:(rr + 1) * ncols]
        hrow = max(box_height(c) for c in chunk)
        heights.append(hrow)
        row_tops.append(y)
        y -= hrow + 14.0
    # first row header from the AHU feeders
    x_first = gx
    x_last = gx + len(bx[0:ncols]) * PITCH - (PITCH - BW)
    sh.line((sa_x, hy["SA"]), (x_last - 3, hy["SA"]), DUCT_LAYER["SA"])
    sh.line((ra_x, hy["RA"]), (x_last - 3, hy["RA"]), DUCT_LAYER["RA"])
    sh.line((sa_x, hy["SA"]), (riser_sa, hy["SA"]), DUCT_LAYER["SA"])
    sh.line((ra_x, hy["RA"]), (riser_ra, hy["RA"]), DUCT_LAYER["RA"])
    bottoms = []
    for rr in range(rows_n):
        chunk = bx[rr * ncols:(rr + 1) * ncols]
        rt = row_tops[rr]
        hrow = heights[rr]
        sa_row = rt + 8.0
        ra_row = rt + 4.0
        if rr > 0:
            sh.line((riser_sa, sa_row), (gx + len(chunk) * PITCH - 6, sa_row), DUCT_LAYER["SA"])
            sh.line((riser_ra, ra_row), (gx + len(chunk) * PITCH - 6, ra_row), DUCT_LAYER["RA"])
            prev_sa = row_tops[rr - 1] + 8.0
            sh.line((riser_sa, hy["SA"]), (riser_sa, sa_row), DUCT_LAYER["SA"])
            sh.line((riser_ra, hy["RA"]), (riser_ra, ra_row), DUCT_LAYER["RA"])
        else:
            sa_row, ra_row = hy["SA"], hy["RA"]
            if rows_n > 1:
                sh.line((riser_sa, hy["SA"]), (riser_sa, row_tops[rows_n - 1] + 8.0), DUCT_LAYER["SA"])
                sh.line((riser_ra, hy["RA"]), (riser_ra, row_tops[rows_n - 1] + 4.0), DUCT_LAYER["RA"])
        for j, c in enumerate(chunk):
            x = gx + j * PITCH
            draw_box(sh, c, x, rt, hrow, inp, plan)
            dashed = c["relation"] != "equipment inside or nearest"
            sh.line((x + 8, sa_row), (x + 8, rt), DUCT_LAYER["SA"], dashed)
            sh.line((x + 14, ra_row), (x + 14, rt), DUCT_LAYER["RA"], dashed)
        bottoms.append(rt - hrow)
        # labels at the start of the row headers
    arrow_at(sh, ax - 26.0, hy["SA"] + 1.4, DUCT_LAYER["SA"], 7.0)
    sh.text(ax - 40.0, hy["SA"] + 1.6, "SA", HS, "IP-DUCT-SA", color=None)
    arrow_at(sh, ax - 26.0, hy["RA"] - 3.4, DUCT_LAYER["RA"], 7.0)
    sh.text(ax - 40.0, hy["RA"] - 3.2, "RA", HS, "IP-DUCT-RA")
    sh.last_bottom = min(bottoms) - 1.0


def draw_fan_group(sh, inp, plan, s, bx, gx, ytop, qmap) -> None:
    """Fans, FCUs and the other single pieces of equipment of one system, with the ducts they feed and the
    room boxes under them.  Each fan or FCU box is drawn once, at the system."""
    chg = system_chg(inp, s)
    ducts = duct_pair(s)
    n = max(1, len(bx))
    fw = 80.0
    width = max(n * PITCH - (PITCH - BW), 96.0)
    fx = gx + (width - fw) / 2
    ORDER = {"FCU": 0, "OAF": 1, "EAF": 2, "TEAF": 2}
    tags: List[Tuple[str, str]] = []
    for e in sorted(s["equipment"], key=lambda e: ORDER.get(e["type"], 3)):
        if (e["tag_as_written"], e["type"]) not in tags:
            tags.append((e["tag_as_written"], e["type"]))
    y = ytop
    fan_edges: List[Tuple[List[str], float, float]] = []   # duct kinds fed, box bottom y, box mid y
    for tag, et in tags:
        q = [r for r in qmap.get(tag, [])]
        ht = draw_fan_box(sh, inp, s, tag, et, fx, y, fw, chg, q)
        fan_edges.append((tag_feeds(tag, et), y - ht, y - ht / 2))
        y -= ht + 4.0
    bottom_fans = y + 4.0
    hdr = {}
    yh = bottom_fans - 10.0
    for k in ducts:
        hdr[k] = yh
        yh -= 5.0
    x_right = gx + width - 4
    x_left = gx + 1.0
    for idx, (kinds, ybot, ymid) in enumerate(fan_edges):
        last = idx == len(fan_edges) - 1
        if last and len(kinds) == 1:
            k = kinds[0]
            xs = fx + fw / 2 + (6 if len(fan_edges) > 1 else 0)
            sh.line((xs, ybot), (xs, hdr[k]), DUCT_LAYER[k])
            sh.line((min(xs, x_left + 3), hdr[k]), (x_right, hdr[k]), DUCT_LAYER[k])
            continue
        for j, k in enumerate(kinds):
            xs = fx - 1.2 - 2.4 * ducts.index(k)
            yy = ymid + (j - (len(kinds) - 1) / 2.0) * 2.4
            sh.line((fx, yy), (xs, yy), DUCT_LAYER[k])
            sh.line((xs, yy), (xs, hdr[k]), DUCT_LAYER[k])
            sh.line((xs, hdr[k]), (x_right, hdr[k]), DUCT_LAYER[k])
    yb_top = yh - 12.0
    if not bx:
        # no room box under this system: its spaces are boxed under other systems
        owners = []
        for sp in s["spaces"]:
            o = plan["assigned"].get(sp["annotation_index"], "")
            owners.append("%d (UNDER %s)" % (sp["annotation_index"], o.replace("APPD:", "")))
        txt = "NO ROOM BOX HERE. SPACES SERVED, BOXED ELSEWHERE ON THE STOREY: " + "; ".join(owners) if owners else \
            "NO SPACE IS TIED TO THIS EQUIPMENT ON THE LAYOUT"
        yy = yh - 4.0
        for ln in wrap(txt, HS, width - 4):
            sh.text(gx + 2, yy - HS, ln, HS, "IP-NOTES", owner="nobox")
            yy -= HS * LP
        sh.last_bottom = yy - 1.0
        return
    hrow = max([box_height(c) for c in bx] or [40.0])
    for j, c in enumerate(bx):
        x = gx + j * PITCH
        draw_box(sh, c, x, yb_top, hrow, inp, plan)
        dashed = c["relation"] != "equipment inside or nearest"
        for m, k in enumerate(ducts):
            xk = x + 8 + 6 * m
            sh.line((xk, hdr[k]), (xk, yb_top), DUCT_LAYER[k], dashed)
    sh.last_bottom = yb_top - hrow - 1.0


def levels(sh: Sheet, ys: List[float]) -> None:
    """Level lines and labels in the left margin, as on the working drawings: one under each band."""
    label = "%s STOREY LEVEL" % ORD[sh.storey]

    def mark(y: float):
        sh.msp.add_line((14, y), (STRIP_X0 - 8, y), dxfattribs=dict(layer="IP-LEVELS", linetype="DASHED", ltscale=4.0))
        insert(sh, "LEVEL_MARK", 14, y - 1.0, 1.0, 1.0, "IP-LEVELS")
        sh.text(20, y + 1.2, label, 2.8, "IP-LEVELS", owner="level")
    for y in ys:
        mark(y)
    sh.text(14, 828.0, "PLANT AND SERVED LEVEL: %s STOREY (FROM THE TAGS, B-%02d)" % (ORD[sh.storey], sh.storey), 2.8,
            "IP-LEVELS", owner="level")


def caption(sh: Sheet) -> None:
    t = "COMMON AREAS (APPENDIX D) - %s STOREY AIR-SIDE SCHEMATIC" % ORD[sh.storey]
    if sh.total > 1:
        t += " - SHEET %d OF %d" % (sh.idx, sh.total)
    sh.text(14, 16.0, t, 5.0, "IP-TITLE", owner="caption")
    sh.msp.add_line((14, 13.5), (14 + tw(t, 5.0), 13.5), dxfattribs=dict(layer="IP-TITLE", lineweight=50))


# --------------------------------------------------------------------------- #
# Frame, strip, key plan, legend list
# --------------------------------------------------------------------------- #
def frame(sh: Sheet) -> None:
    sh.rect(5, 5, SHEET_W - 5, SHEET_H - 5, "IP-FRAME")
    sh.rect(8, 8, SHEET_W - 8, SHEET_H - 8, "IP-FRAME")
    sh.msp.add_line((STRIP_X0 - 4, 8), (STRIP_X0 - 4, SHEET_H - 8), dxfattribs=dict(layer="IP-FRAME"))


def strip(sh: Sheet, inp, plan, sym) -> None:
    x0, x1 = STRIP_X0, SHEET_W - 12
    w = x1 - x0
    # --- title block, bottom up, fields of the working drawings
    y = 12.0

    def cell(xa, xb, ya, yb, label, value=None, hv=H):
        sh.rect(xa, ya, xb, yb, "IP-TITLE")
        sh.text(xa + 1.0, yb - 3.2, label, HS, "IP-TITLE", owner="title")
        if value:
            sh.text(xa + 1.0, ya + 2.0, value, hv, "IP-TITLE", owner="title")
    cell(x0, x0 + 30, y, y + 11, "JOB NO.", JOB_NO)
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
    # drawing title
    title = ["ACMV SERVICES", "INPATIENT %s STOREY AIR-SIDE" % ORD[sh.storey], "SCHEMATIC - COMMON AREAS", "(APPENDIX D)"]
    if sh.total > 1:
        title.append("SHEET %d OF %d" % (sh.idx, sh.total))
    ht = 3.6 + len(title) * 5.6 + 2
    sh.rect(x0, y, x1, y + ht, "IP-TITLE")
    sh.text(x0 + 1.0, y + ht - 3.2, "DRAWING TITLE", HS, "IP-TITLE", owner="title")
    for i, t in enumerate(title):
        sh.text(x0 + 1.0, y + ht - 9.6 - i * 5.6, t, 4.2, "IP-TITLE", owner="title")
    y += ht
    # project title
    lines = wrap(PROJECT_TITLE, 3.4, w - 2.0)
    ht = 4.2 + len(lines) * 4.4 + 1.2
    sh.rect(x0, y, x1, y + ht, "IP-TITLE")
    sh.text(x0 + 1.0, y + ht - 3.2, "PROJECT TITLE", HS, "IP-TITLE", owner="title")
    for i, ln in enumerate(lines):
        sh.text(x0 + 1.0, y + ht - 8.0 - i * 4.4, ln, 3.4, "IP-TITLE", owner="title")
    y += ht
    # status and revision table
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
    # --- notes (top of the strip)
    notes = [
        "THIS SHEET HAS A PROVISIONAL NUMBER. THE DRAWING OFFICE MUST CONFIRM IT.",
        "THE SHEET SHOWS COMMON-AREA SYSTEMS OF THE %s STOREY IN APPENDIX D. IT SHALL BE READ WITH THE ACMV NOTES AND LEGEND SHEETS IP-BH-00001 AND IP-BH-00002." % ORD[sh.storey]
        + ("" if sh.total == 1 else " THE STOREY IS ON %d SHEETS: %s. EACH SYSTEM IS ON ONE SHEET." % (sh.total, ", ".join(sh.numbers))),
        "THE AREAS, MODES AND EQUIPMENT TAGS ARE AS WRITTEN IN APPENDIX D. THE DUTIES AND FAN TYPES ARE AS WRITTEN IN THE EQUIPMENT SCHEDULE.",
        "EACH VALUE HAS A CHANGE ID. THE CHANGE ID GIVES THE ENTRY IN THE SCHEMATIC UPDATE REGISTER.",
        "SPACE NAMES, SUPPLY, RETURN AND EXHAUST AIRFLOWS AND PRESSURES ARE FROM THE COMMON-AREAS WORKBOOK (D00). A VALUE THAT THE WORKBOOK DOES NOT GIVE IS SHOWN AS NOT GIVEN.",
        "APPENDIX D DOES NOT SHOW THE OUTDOOR-AIR INTAKE OR THE EXHAUST DISCHARGE. THEY ARE NOT DRAWN.",
        "A DASHED DUCT LINE SHOWS A SPACE WITH A TAG NEAR IT, NOT INSIDE IT.",
        "THE PLANT LEVEL AND THE SERVED LEVEL COME FROM THE TAGS.",
        "THE SYMBOLS FOLLOW IP-BH-00002. A FAN WITH A TYPE IN THE SCHEDULE HAS THE LEGEND SYMBOL. A LABELLED BOX SHOWS A FAN WITH NO TYPE. AN FCU HAS THE LEGEND SYMBOL FOR A FAN COIL UNIT.",
        "WORKBOOK AIRFLOW IS THE LARGER VALUE OF THE COMMON-AREAS WORKBOOK, SHOWN BESIDE THE SCHEDULE DUTY (D-072).",
        "A SPACE IS IN ONE BOX ONLY. AN EQUIPMENT TAG IS DRAWN ONCE, AT ITS SYSTEM. THE TAGS LISTED IN A SPACE BOX ARE FOR REFERENCE.",
    ]
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


def key_plan(sh: Sheet, inp, plan) -> None:
    """Storey outline from the zoning polygons, with the App D spaces of this sheet filled."""
    st = "L%02d" % sh.storey
    zr = read_csv(REG / ("room_polygons_%s_wkt.csv" % st))
    zpoly = [swkt.loads(r["wall_face_wkt_mm"]) for r in zr if r["wall_face_wkt_mm"]]
    ap = {int(r["annotation_index"]): swkt.loads(r["polygon_building_mm_wkt"]) for r in read_csv(REG / "appd_outlines_mm.csv")
          if r["storey"] == st}
    minx = min(p.bounds[0] for p in zpoly)
    miny = min(p.bounds[1] for p in zpoly)
    maxx = max(p.bounds[2] for p in zpoly)
    maxy = max(p.bounds[3] for p in zpoly)
    x0, x1 = STRIP_X0, SHEET_W - 12
    box_w = x1 - x0
    scale = (box_w - 6) / (maxx - minx)        # mm of paper per mm of building
    box_h = (maxy - miny) * scale + 14
    top = sh.title_top + 4 + box_h
    ox, oy = x0 + 3 - minx * scale, sh.title_top + 4 + 3 - miny * scale
    sh.rect(x0, sh.title_top + 4, x1, top, "IP-KEYPLAN")

    def tx(p):
        return (p[0] * scale + ox, p[1] * scale + oy)

    def simp(coords):
        """Paper points of a ring, simplified to 0.1 mm (the key plan is not to scale)."""
        from shapely.geometry import LinearRing
        pts = [tx(c) for c in coords]
        try:
            r = LinearRing(pts).simplify(0.1)
            out = list(r.coords)[:-1]
            return out if len(out) >= 3 else pts
        except Exception:
            return pts
    for g in zpoly:
        for poly in (g.geoms if hasattr(g, "geoms") else [g]):
            sh.msp.add_lwpolyline(simp(poly.exterior.coords), close=True,
                                  dxfattribs=dict(layer="IP-KEYPLAN", lineweight=5))
    shown = set(sh.shown)
    labels: List[Tuple[float, float, float, float]] = []
    for idx, g in ap.items():
        if idx not in shown:
            continue
        for poly in (g.geoms if hasattr(g, "geoms") else [g]):
            hatch = sh.msp.add_hatch(color=0, dxfattribs=dict(layer="IP-KEYPLAN-SPACES"))
            hatch.rgb = (255, 200, 120)
            hatch.paths.add_polyline_path(simp(poly.exterior.coords), is_closed=True)
            for ring in poly.interiors:
                hatch.paths.add_polyline_path(simp(ring.coords), is_closed=True)
            sh.msp.add_lwpolyline(simp(poly.exterior.coords), close=True,
                                  dxfattribs=dict(layer="IP-KEYPLAN-SPACES", lineweight=9))
    # numbers where they fit inside the space and do not touch another number
    kp_labels = []
    for idx, g in sorted(ap.items(), key=lambda kv: -kv[1].area):
        if idx not in shown:
            continue
        s = str(idx)
        h = 2.0
        w = tw(s, h)
        pt = g.representative_point()
        cx, cy = tx((pt.x, pt.y))
        r = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
        from shapely.geometry import box as sbox
        rg = sbox((r[0] - ox) / scale, (r[1] - oy) / scale, (r[2] - ox) / scale, (r[3] - oy) / scale)
        if not g.contains(rg):
            continue
        if any(not (r[2] + 0.6 < o[0] or r[0] - 0.6 > o[2] or r[3] + 0.6 < o[1] or r[1] - 0.6 > o[3]) for o in labels):
            continue
        labels.append(r)
        sh.text(cx - w / 2, cy - h / 2, s, h, "IP-KEYPLAN-TEXT", owner="keyplan")
    cap = ["KEY PLAN, %s STOREY (NTS)" % ORD[sh.storey], "SPACES OF THIS SHEET IN ORANGE; NUMBER = APPENDIX D ANNOTATION",
           "OUTLINES FROM THE ZONING ROOM POLYGONS (STOREY L%02d)" % sh.storey]
    yy = top - 4.2
    sh.text(x0 + 1.0, yy, cap[0], 2.8, "IP-KEYPLAN-TEXT", owner="keyplan")
    for i, c in enumerate(cap[1:]):
        for ln in wrap(c, 2.0, box_w - 2):
            yy -= 3.0
            sh.text(x0 + 1.0, yy, ln, 2.0, "IP-KEYPLAN-TEXT", owner="keyplan")
    sh.keyplan_top = top
    sh.keyplan_labels = len(labels)
    sh.keyplan_total = len(shown)


def legend_list(sh: Sheet, sym, y_bot: float) -> None:
    """Symbols used (from the legend sheet) and the labelled boxes used (not in the legend)."""
    x = 380.0
    y = 96.0
    sh.text(x, y, "SYMBOLS USED (IP-BH-00002)", 3.0, "IP-LEGEND", owner="legend")
    y -= 7.0
    # AHU
    insert(sh, "SYM_AHU", x, y - 6, 18.0, 8.0, "IP-LEGEND")
    sh.symbols.append((x, y - 6, x + 18.0, y + 2, "legend AHU"))
    sh.text(x + 22, y - 3.6, "AIR HANDLING UNIT", H, "IP-LEGEND", owner="legend")
    y -= 9.0
    insert(sh, "SYM_FCU", x, y - 6, 18.0, 8.0, "IP-LEGEND")
    sh.symbols.append((x, y - 6, x + 18.0, y + 2, "legend FCU"))
    sh.text(x + 22, y - 3.6, "FAN COIL UNIT", H, "IP-LEGEND", owner="legend")
    for k in ("SA", "RA", "OA", "EA", "TEA"):
        y -= 9.0
        sh.msp.add_line((x, y), (x + 18, y), dxfattribs=dict(layer=DUCT_LAYER[k], lineweight=50))
        sh.ducts.append(((x, y), (x + 18, y)))
        sh.text(x + 22, y - 0.9, DUCT_NAME[k], H, "IP-LEGEND", owner="legend")
    y_col1 = y
    x += 230.0                      # second column
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
    sh.text(x + 22, y - 0.9, "LABELLED BOX: FAN WITH NO TYPE IN THE SCHEDULE. NOT IN THE LEGEND.", H, "IP-LEGEND", owner="legend")
    sh.legend_bottom = min(y, y_col1) - 8


# --------------------------------------------------------------------------- #
# Output and checks
# --------------------------------------------------------------------------- #
def _rgb(doc, e) -> Tuple[float, float, float]:
    from ezdxf import colors
    if e.dxf.hasattr("true_color"):
        r, g, b = colors.int2rgb(e.dxf.true_color)
    else:
        aci = e.dxf.color if e.dxf.hasattr("color") else 256
        if aci in (256, 0):
            ly = doc.layers.get(e.dxf.layer) if e.dxf.layer in doc.layers else None
            if ly is not None and ly.dxf.hasattr("true_color"):
                r, g, b = colors.int2rgb(ly.dxf.true_color)
            else:
                aci = ly.dxf.color if ly is not None else 7
                r, g, b = colors.aci2rgb(abs(aci))
        else:
            r, g, b = colors.aci2rgb(aci)
    if (r, g, b) == (255, 255, 255):
        r = g = b = 0
    return (r / 255, g / 255, b / 255)


def primitives(doc) -> List[Dict[str, Any]]:
    """Every drawn primitive of the DXF model space (blocks expanded): text, line segments, fill polygons.
    The PDF is drawn from this list and the check reads the PDF back against it."""
    out: List[Dict[str, Any]] = []

    def walk(e):
        t = e.dxftype()
        if t == "INSERT":
            for v in e.virtual_entities():
                walk(v)
            return
        col = _rgb(doc, e)
        lw = e.dxf.lineweight if e.dxf.hasattr("lineweight") and e.dxf.lineweight > 0 else 13
        dash = e.dxf.hasattr("linetype") and e.dxf.linetype.upper() == "DASHED"
        ls = e.dxf.ltscale if e.dxf.hasattr("ltscale") else 1.0
        if t == "TEXT":
            out.append(dict(kind="text", x=e.dxf.insert.x, y=e.dxf.insert.y, s=e.dxf.text, h=e.dxf.height, col=col,
                            layer=e.dxf.layer))
        elif t == "LINE":
            if math.hypot(e.dxf.end.x - e.dxf.start.x, e.dxf.end.y - e.dxf.start.y) < 0.03:
                return                      # a point: nothing to draw
            out.append(dict(kind="seg", p0=(e.dxf.start.x, e.dxf.start.y), p1=(e.dxf.end.x, e.dxf.end.y), col=col, lw=lw,
                            dash=dash, ls=ls))
        elif t == "LWPOLYLINE":
            pts = [(p[0], p[1]) for p in e.get_points("xy")]
            n = len(pts)
            for i in range(n if e.closed else n - 1):
                if math.hypot(pts[(i + 1) % n][0] - pts[i][0], pts[(i + 1) % n][1] - pts[i][1]) < 0.03:
                    continue                # a point: nothing to draw
                out.append(dict(kind="seg", p0=pts[i], p1=pts[(i + 1) % n], col=col, lw=lw, dash=False, ls=1.0))
        elif t == "HATCH":
            for path in e.paths:
                pts = [(v[0], v[1]) for v in path.vertices]
                if len(pts) >= 3:
                    out.append(dict(kind="fill", pts=pts, col=col))

    for e in doc.modelspace():
        walk(e)
    return out


def render_pdf(dxf_path: Path, pdf_path: Path, png_path: Optional[Path] = None) -> None:
    """Draw the PDF from the primitives of the DXF: one PDF path for every segment and fill, one visible text
    run for every TEXT entity.  Nothing is added and nothing is left out."""
    doc = ezdxf.readfile(dxf_path)
    k = 72.0 / 25.4
    pdf = pymupdf.open()
    page = pdf.new_page(width=SHEET_W * k, height=SHEET_H * k)

    def P(x, y):
        return pymupdf.Point(x * k, (SHEET_H - y) * k)
    prims = primitives(doc)
    groups: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = defaultdict(list)
    for pr in prims:
        if pr["kind"] == "seg":
            groups[(pr["col"], pr["lw"], pr["dash"], pr["ls"])].append(pr)
    for (col, lw, dash, ls), segs in groups.items():
        sh = page.new_shape()
        for pr in segs:
            sh.draw_line(P(*pr["p0"]), P(*pr["p1"]))
        dsh = "[%.2f %.2f] 0" % (1.6 * ls * k, 0.8 * ls * k) if dash else None
        sh.finish(color=col, width=max(lw / 100.0 * k, 0.2), dashes=dsh, closePath=False)
        sh.commit()
    for pr in prims:
        if pr["kind"] == "seg":
            continue
        if pr["kind"] == "fill":
            sh = page.new_shape()
            sh.draw_polyline([P(*q) for q in pr["pts"]])
            sh.finish(color=None, fill=pr["col"], closePath=True)
            sh.commit()
        else:
            pt = P(pr["x"], pr["y"])
            page.insert_text(pt, pr["s"], fontsize=pr["h"] * k / 0.716, fontname="helv", color=pr["col"],
                             morph=(pt, pymupdf.Matrix(WF, 1)))
    pdf.set_metadata({"title": "IP-BH new drawing (draft)", "creator": "newdwg", "producer": "pymupdf, drawn from the DXF"})
    pdf.save(str(pdf_path), deflate=True)
    if png_path:
        page.get_pixmap(dpi=60).save(str(png_path))
    pdf.close()


def used_numbers() -> set:
    used = {m.group(1) for p in WORKING.glob("IP-BH-*") for m in [re.match(r"IP-BH-(\w{5})", p.name)] if m}
    tender = ROOT / "data" / "source" / "Basis" / "01 ACMV Tender Schematic"
    used |= {m.group(1) for p in tender.glob("IP-BH-*") for m in [re.match(r"IP-BH-(\w{5})", p.name)] if m}
    return used


# Free numbers of the IP-BH-05xxx series.  Block 050n0..050n9 is the block of storey n in the working
# drawings (05010 = 1st storey, 05040 = 4th storey).  The last n free numbers of the block are taken,
# so the 4th storey keeps IP-BH-05049.  The 3rd-storey block has no free number: the next free block
# above it (the 4th-storey block, 05043 to 05046) is used and the case is put to Marcus.
BLOCK_ORDER = {4: [4], 2: [2], 3: [3, 4], 5: [5], 6: [6], 7: [7]}
_TAKEN: set = set()


def provisional_numbers(storey: int, n: int) -> List[str]:
    used = used_numbers() | _TAKEN
    if storey != 4:
        used.add("05049")           # kept for the 4th storey (pilot number)
    got: List[str] = []
    for blk in BLOCK_ORDER[storey]:
        free = [c for c in ("050%d%d" % (blk, d) for d in range(10)) if c not in used]
        need = n - len(got)
        take = free[-need:] if need > 0 else []
        got = take + got if blk != BLOCK_ORDER[storey][0] else take
        if len(got) >= n:
            break
    if len(got) < n:
        raise RuntimeError("not enough free numbers for storey %d: need %d, got %s" % (storey, n, got))
    _TAKEN.update(got)
    return ["IP-BH-" + c for c in sorted(got)]


def free_number() -> set:
    return used_numbers()


def provisional_number(storey: int) -> str:
    return provisional_numbers(storey, 1)[0]


def write_sheet(sh: "Sheet") -> Dict[str, Any]:
    dxf = NEWDIR / (sh.number + "_P1.dxf")
    pdf = NEWDIR / (sh.number + "_P1.pdf")
    png = NEWDIR / (sh.number + "_P1.png")
    sh.doc.saveas(dxf)
    render_pdf(dxf, pdf, png)
    vals = [dict(value_id=v.id, field=v.field, text=v.text, change_id=v.chg, source=v.src, owner=v.owner)
            for v in sh.values]
    with open(NEWDIR / (sh.number + "_P1_values.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(vals[0]))
        w.writeheader()
        w.writerows(vals)
    return dict(dxf=dxf, pdf=pdf, png=png)


def run(storey: Optional[int] = None) -> List[Dict[str, Any]]:
    """Build every sheet of one storey (default: the pilot storey 4)."""
    NEWDIR.mkdir(parents=True, exist_ok=True)
    storey = storey or 4
    results = []
    for sh, ctx in build_storey(storey, lambda n: provisional_numbers(storey, n)):
        paths = write_sheet(sh)
        results.append(dict(storey=storey, number=sh.number, sheet=sh, ctx=ctx, why="storey %d" % storey, **paths))
    return results


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["run", "check"])
    ap.add_argument("--storey", type=int, action="append", default=None, help="storey 2 to 7; repeat; default all")
    ns = ap.parse_args(argv)
    from . import newdwg_check
    storeys = ns.storey or [4, 2, 3, 5, 6, 7]
    rc = 0
    allres = []
    for st in storeys:
        res = run(st)
        for r in res:
            print(r["number"], r["dxf"].name, r["pdf"].name)
        rc |= newdwg_check.main_storey(res)
        allres.append(res)
    return rc


if __name__ == "__main__":
    sys.exit(main())
