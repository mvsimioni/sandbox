"""Air-side department schematic parser (text layer + vector layer, no models).

Reads the room tables (room name, typical count, mode columns, SA/RA/EA/OA
airflow lines, pressure regime, air class, RH, temperature, filter), the
AHU / RAF / TEAF / PAHU / BIBO equipment with airflows and the colour coded
duct links from the department schematics (IP-BH-05xxx/06xxx/07xxx and MOT NSC
IP-BH-160xx).  Every value keeps its as-printed text, source file, box (page
view points, origin top-left), method and confidence.

CLI:  python3 -m src.schematic_parser run [--sheets IP-BH-05010 ...]
"""
from __future__ import annotations

import argparse
import collections
import csv
import glob
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:  # PyMuPDF
    import pymupdf as fitz  # type: ignore
except Exception:  # pragma: no cover
    import fitz  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
BASIS = ROOT / "data" / "source" / "Basis"
REG = ROOT / "out" / "registers"
QA = ROOT / "out" / "qa"

# stroke colours of the duct services (rounded to 2 dp)
SERVICE_COLOURS = {
    (0.5, 1.0, 1.0): "SA",
    (0.35, 1.0, 0.35): "RA",
    (0.61, 0.31, 0.0): "EA",
    (0.97, 0.82, 0.37): "IEA",
    (1.0, 0.5, 0.5): "SPILL",
    (0.57, 0.57, 1.0): "OA",
}
BLACK = (0.0, 0.0, 0.0)


def _rc(c):
    return tuple(round(float(x), 2) for x in c) if c else None


# --------------------------------------------------------------------------- #
# extraction layer
# --------------------------------------------------------------------------- #
class Sheet:
    """Text and vector layer of one single-page schematic, page-view points."""

    def __init__(self, path: str):
        self.path = path
        doc = fitz.open(path)
        self.pages = len(doc)
        p = doc[0]
        self.rotation = p.rotation
        M = p.rotation_matrix
        self.width, self.height = (p.rect.x1, p.rect.y1)
        self.words: List[dict] = []
        for w in p.get_text("words"):
            r = fitz.Rect(w[:4]) * M
            self.words.append({"x0": r.x0, "y0": r.y0, "x1": r.x1, "y1": r.y1, "text": w[4]})
        self.lines: List[dict] = []
        for b in p.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            for l in b["lines"]:
                r = fitz.Rect(l["bbox"]) * M
                txt = "".join(s["text"] for s in l["spans"])
                size = max((s["size"] for s in l["spans"]), default=0)
                self.lines.append({"x0": r.x0, "y0": r.y0, "x1": r.x1, "y1": r.y1, "text": txt,
                                   "dir": l["dir"], "size": size})
        self.segs: List[dict] = []
        self.rects: List[dict] = []
        for dr in p.get_drawings():
            col = _rc(dr.get("color"))
            for it in dr["items"]:
                if it[0] == "l":
                    a = fitz.Point(it[1]) * M
                    b = fitz.Point(it[2]) * M
                    self.segs.append({"x0": a.x, "y0": a.y, "x1": b.x, "y1": b.y, "col": col,
                                      "svc": SERVICE_COLOURS.get(col) if col else None})
                elif it[0] == "re":
                    r = fitz.Rect(it[1]) * M
                    r.normalize()
                    self.rects.append({"x0": r.x0, "y0": r.y0, "x1": r.x1, "y1": r.y1,
                                       "col": col, "fill": _rc(dr.get("fill"))})
        doc.close()
        self.sha256 = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        self.table_warnings: List[str] = []


# --------------------------------------------------------------------------- #
# geometry helpers
# --------------------------------------------------------------------------- #
def _isv(s):
    return abs(s["x0"] - s["x1"]) < 0.05


def _ish(s):
    return abs(s["y0"] - s["y1"]) < 0.05


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t.replace(" ", " ")).strip()


def _cluster(vals, tol):
    out = []
    for v in sorted(vals):
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return [sum(c) / len(c) for c in out]


def _merge_runs(runs, gap=4.0):
    runs = sorted(runs)
    out = []
    for a, b in runs:
        if out and a <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


AIRFLOW_LABEL = re.compile(r"^AIR ?FLOWS?$", re.I)
ROW_PATTERNS = [
    ("mode", re.compile(r"^MODE$", re.I)),
    ("airflow", AIRFLOW_LABEL),
    ("pressure", re.compile(r"^PRESSURE( REGIME)?$", re.I)),
    ("class", re.compile(r"^AIR CLASS$", re.I)),
    ("humidity", re.compile(r"^RELATIVE( HUMIDITY)?$|^HUMIDITY$", re.I)),
    ("temperature", re.compile(r"^TARGETED( TEMPERATURE)?$|^TEMPERATURE$", re.I)),
    ("filter", re.compile(r"^FILTER$", re.I)),
]
ROW_ORDER = [k for k, _ in ROW_PATTERNS]
ARROW_RE = re.compile(r"^(SA|RA|EA|EX|OA|IEA)$", re.I)
TYPICAL_RE = re.compile(r"^\s*(\d+)\s*\.?\s*NOS?\.? OF TYPICAL( ROOMS?)?", re.I)


def _lc(l):
    return ((l["x0"] + l["x1"]) / 2, (l["y0"] + l["y1"]) / 2)


class RoomTable:
    def __init__(self):
        self.left = None          # x of the label column frame
        self.right = None         # x of the right edge of the value cells
        self.label_right = None
        self.seps: List[float] = []
        self.rows: Dict[str, Tuple[float, float]] = {}
        self.cols: List[Tuple[float, float]] = []
        self.col_names: List[str] = []
        self.col_boxes: List[Optional[List[float]]] = []
        self.cells: Dict[Tuple[str, int], List[dict]] = {}
        self.name_lines: List[dict] = []
        self.typical_line: Optional[dict] = None
        self.typical = 1
        self.arrows: List[dict] = []
        self.warnings: List[str] = []
        self.method_notes: List[str] = []
        self.outline = None

    @property
    def box(self):
        top = min(v[0] for v in self.rows.values())
        bot = max(v[1] for v in self.rows.values())
        return [self.left, top, self.right, bot]


def _chain_right(segs_at_y, x_start, gap=3.0):
    """Follow end-to-end collinear separator pieces to the right."""
    pieces = sorted(segs_at_y)
    x = x_start
    moved = True
    while moved:
        moved = False
        for a, b in pieces:
            if a <= x + gap and b > x + 0.5:
                x = b
                moved = True
    return x


def find_room_tables(sh: Sheet) -> List[RoomTable]:
    """Locate room tables by their label column and read the grid around it."""
    hl = [l for l in sh.lines if abs(l["dir"][0] - 1) < 0.01]
    labels: Dict[str, List[dict]] = {k: [] for k in ROW_ORDER}
    for l in hl:
        t = _norm(l["text"])
        for k, rx in ROW_PATTERNS:
            if rx.match(t):
                labels[k].append(l)
    blk_h = [s for s in sh.segs if s["col"] == BLACK and _ish(s)]
    tables: List[RoomTable] = []
    anchors = sorted(labels["airflow"], key=lambda l: (l["x0"], l["y0"]))
    for a in anchors:
        t = RoomTable()
        def below(key, dymin, dymax):
            c = [l for l in labels[key] if abs(l["x0"] - a["x0"]) <= 12 and dymin <= l["y0"] - a["y0"] <= dymax]
            return min(c, key=lambda l: l["y0"]) if c else None
        f = below("filter", 20, 260)
        if f is None:
            t.warnings.append("AIRFLOW label at (%.0f,%.0f) has no FILTER label below" % (a["x0"], a["y0"]))
            sh.table_warnings.append(t.warnings[-1])
            continue
        m = None
        c = [l for l in labels["mode"] if a["x0"] - 6 <= _lc(l)[0] <= a["x0"] + 90 and -60 <= l["y0"] - a["y0"] < 0]
        if c:
            m = max(c, key=lambda l: l["y0"])
        # label-column separators: horizontal black strokes starting at the left frame
        y_lo = (m["y0"] if m else a["y0"]) - 80
        y_hi = f["y1"] + 10
        cand = [s for s in blk_h if y_lo <= s["y0"] <= y_hi and a["x0"] - 16 <= min(s["x0"], s["x1"]) <= a["x0"] - 0.5
                and abs(s["x1"] - s["x0"]) > 20]
        if not cand:
            t.warnings.append("no label-column separators at AIRFLOW (%.0f,%.0f)" % (a["x0"], a["y0"]))
            sh.table_warnings.append(t.warnings[-1])
            continue
        xl = _cluster([min(s["x0"], s["x1"]) for s in cand], 2.0)
        x_left = min(xl, key=lambda x: abs(x - (a["x0"] - 6)))
        cand = [s for s in cand if abs(min(s["x0"], s["x1"]) - x_left) <= 2.0]
        ys = _cluster([s["y0"] for s in cand], 1.5)
        # keep the separators between the MODE/AIRFLOW top and the FILTER bottom
        top_ref = (m["y0"] if m else a["y0"])
        ys = [y for y in ys if y <= f["y1"] + 8]
        above = [y for y in ys if y <= top_ref + 1]
        if above:
            top = max(above)
        else:
            top = top_ref - 2.0
            ys = [top] + ys
            t.method_notes.append("top frame line not drawn at the label column; row top taken from the label")
        bot_c = [y for y in ys if y >= f["y1"] - 1]
        if bot_c:
            bot = min(bot_c)
        else:
            bot = f["y1"] + 4.0
            ys = ys + [bot]
            t.method_notes.append("bottom frame line not drawn at the label column; row bottom taken from the FILTER label")
        seps = [y for y in ys if top - 0.5 <= y <= bot + 0.5]
        t.seps = seps
        t.left = x_left
        t.label_right = None
        # right edge: chain separator pieces on the top and bottom lines
        rights = []
        for ysep in seps:
            pcs = [(min(s["x0"], s["x1"]), max(s["x0"], s["x1"])) for s in blk_h if abs(s["y0"] - ysep) <= 1.0
                   and min(s["x0"], s["x1"]) >= x_left - 2]
            rights.append(_chain_right(pcs, x_left))
        t.right = max(rights) if rights else x_left
        # label column ends after the widest label text
        lab_lines = [a, f] + ([m] if m else [])
        for key in ("pressure", "class", "humidity", "temperature"):
            c2 = below(key, 0, 260)
            if c2 is not None:
                lab_lines.append(c2)
        t.label_right = max(l["x1"] for l in lab_lines) + 3.0
        # rows from the label centres
        for key in ROW_ORDER:
            if key == "mode" and m is None:
                continue
            lab = a if key == "airflow" else (m if key == "mode" else below(key, 0, 260))
            if lab is None:
                continue
            cy = _lc(lab)[1]
            band = [(p, q) for p, q in zip(seps[:-1], seps[1:]) if p - 0.5 <= cy <= q + 0.5]
            if band:
                t.rows[key] = band[0]
        for need in ("airflow", "filter"):
            if need not in t.rows:
                t.warnings.append("row %s not resolved" % need)
        if t.warnings:
            sh.table_warnings.extend(t.warnings)
            continue
        tables.append(t)
    # tables that touch their right neighbour share the frame line
    for t in tables:
        nb = [o.left for o in tables if o is not t and o.left > t.left + 20 and abs(o.box[1] - t.box[1]) < 40]
        if nb and min(nb) < t.right - 1.0:
            t.right = min(nb) + 0.5
    # ---- value columns -------------------------------------------------
    for t in tables:
        lines_in = [l for l in hl if t.label_right - 1 <= _lc(l)[0] <= t.right + 1 and t.rows["airflow"][0] - 80 <= _lc(l)[1] <= t.box[3] + 1]
        # column geometry: header cells if a MODE row exists, else one column
        if "mode" in t.rows:
            my0, my1 = t.rows["mode"]
            hdr = sorted([l for l in lines_in if my0 - 0.5 <= _lc(l)[1] <= my1 + 0.5], key=lambda l: l["x0"])
            if hdr:
                # merge pieces of one header cell (e.g. 'SET' 'BACK') that sit within 6 pt of each other
                cells = []
                for l in hdr:
                    if cells and l["x0"] - cells[-1][-1]["x1"] <= 6:
                        cells[-1].append(l)
                    else:
                        cells.append([l])
                centres = [(min(l["x0"] for l in c) + max(l["x1"] for l in c)) / 2 for c in cells]
                t.col_names = [" ".join(_norm(l["text"]) for l in c) for c in cells]
                t.col_boxes = [[min(l["x0"] for l in c), min(l["y0"] for l in c), max(l["x1"] for l in c), max(l["y1"] for l in c)] for c in cells]
                edges = [t.label_right] + [(centres[i] + centres[i + 1]) / 2 for i in range(len(centres) - 1)] + [t.right]
                t.cols = [(edges[i], edges[i + 1]) for i in range(len(centres))]
            else:
                t.warnings.append("MODE row without header text")
        if not t.cols:
            t.cols = [(t.label_right, t.right)]
            t.col_names = [""]
            t.col_boxes = [None]
        for key, (y0, y1) in t.rows.items():
            if key == "mode":
                continue
            for l in lines_in:
                cx, cy = _lc(l)
                if y0 - 0.5 <= cy <= y1 + 0.5:
                    for ci, (c0, c1) in enumerate(t.cols):
                        if c0 - 0.5 <= cx <= c1 + 0.5:
                            t.cells.setdefault((key, ci), []).append(l)
                            break
        for k in t.cells:
            t.cells[k].sort(key=lambda l: (l["y0"], l["x0"]))
    return tables


def _vertical_runs(sh: Sheet):
    byx = collections.defaultdict(list)
    for s in sh.segs:
        if s["col"] == BLACK and _isv(s):
            byx[round(s["x0"], 1)].append((min(s["y0"], s["y1"]), max(s["y0"], s["y1"])))
    runs = []
    for x, iv in byx.items():
        for a, b in _merge_runs(iv, 4.0):
            runs.append((x, a, b))
    return runs


def room_outline(runs, t: RoomTable, hsegs=None):
    """Left and right walls of the room box that holds the table (None if absent)."""
    top, bot = t.box[1], t.box[3]
    left = [r for r in runs if abs(r[0] - t.left) < 2 and r[1] <= top - 30 and r[2] >= bot - 3]
    if not left:
        return None
    ytop = max(r[1] for r in left)
    if ytop < top - 130 and hsegs is not None:
        # the left wall is merged with the department frame: use the horizontal top line across the table
        ys = [sg["y0"] for sg in hsegs if min(sg["x0"], sg["x1"]) <= t.left + 2 and max(sg["x0"], sg["x1"]) >= t.right - 2
              and top - 130 <= sg["y0"] <= top - 30]
        if not ys:
            return None
        ytop = max(ys)
    elif ytop < top - 220:
        return None
    rights = sorted([r for r in runs if r[0] > t.right - 2 and r[1] <= ytop + 3 and r[2] >= bot - 3], key=lambda r: r[0])
    if not rights:
        return None
    return [t.left, ytop, rights[0][0], bot]


def attach_names(sh: Sheet, tables: List[RoomTable]) -> None:
    """Room name, typical count and branch-arrow lines above each table."""
    hl = [l for l in sh.lines if abs(l["dir"][0] - 1) < 0.01]
    runs = _vertical_runs(sh)
    hsegs = [sg for sg in sh.segs if sg["col"] == BLACK and _ish(sg)]
    for t in tables:
        top = t.box[1]
        t.outline = room_outline(runs, t, hsegs)
        if t.outline:
            x0, x1 = t.outline[0] - 2, t.outline[2] + 2
        else:
            x0, x1 = t.left - 4, t.right + 4
            t.method_notes.append("no room outline found; name taken from the table width")
        # the arrow row (SA/RA/EA) sits above the name block
        arrows = [l for l in hl if ARROW_RE.match(_norm(l["text"])) and x0 <= _lc(l)[0] <= x1 and top - 90 <= l["y1"] <= top + 1]
        t.arrows = sorted(arrows, key=lambda l: l["x0"])
        if t.arrows:
            ylim = max(l["y1"] for l in t.arrows) - 1
        else:
            ylim = top - 70
        first_bottom = min(v[1] for v in t.rows.values() if v[0] <= top + 0.5)
        zone = [l for l in hl if l not in arrows and x0 <= _lc(l)[0] <= x1 and l["y0"] >= ylim
                and (l["y1"] <= top + 1 or (_lc(l)[0] > t.right + 1 and l["y1"] <= first_bottom + 1))
                and _norm(l["text"]) and not re.match(r"^(VCD|VAV|VV|NRD|FD|M|A|B)$", _norm(l["text"]))]
        # a name block belongs to the nearest table below it
        zone = [l for l in zone if not any(o is not t and o.left - 4 <= _lc(l)[0] <= o.right + 4 and o.box[1] - 2 <= _lc(l)[1] <= o.box[3]
                                              for o in tables)]
        # lines already inside the table itself (header row, cells) are not names
        zone = [l for l in zone if not (t.left - 2 <= _lc(l)[0] <= t.right + 2 and _lc(l)[1] >= top - 1)]
        zone.sort(key=lambda l: (l["y0"], l["x0"]))
        typ = [l for l in zone if TYPICAL_RE.match(_norm(l["text"]))]
        if typ:
            t.typical_line = typ[0]
            t.typical = int(TYPICAL_RE.match(_norm(typ[0]["text"])).group(1))
        t.name_lines = [l for l in zone if l not in typ]


def service_components(sh: Sheet, svc: str, tol: float = 10.0):
    """Union of same-colour duct strokes that touch or lie within ``tol`` points."""
    ss = [s for s in sh.segs if s["svc"] == svc]
    n = len(ss)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    cell = 40.0
    grid = collections.defaultdict(list)
    boxes = []
    for i, s in enumerate(ss):
        b = (min(s["x0"], s["x1"]) - tol, min(s["y0"], s["y1"]) - tol,
             max(s["x0"], s["x1"]) + tol, max(s["y0"], s["y1"]) + tol)
        boxes.append(b)
        for gx in range(int(b[0] // cell), int(b[2] // cell) + 1):
            for gy in range(int(b[1] // cell), int(b[3] // cell) + 1):
                grid[(gx, gy)].append(i)
    for lst in grid.values():
        for ii in range(len(lst)):
            i = lst[ii]
            bi = boxes[i]
            for jj in range(ii + 1, len(lst)):
                j = lst[jj]
                bj = boxes[j]
                if bi[0] <= bj[2] and bj[0] <= bi[2] and bi[1] <= bj[3] and bj[1] <= bi[3]:
                    union(i, j)
    comps = collections.defaultdict(list)
    for i in range(n):
        comps[find(i)].append(ss[i])
    out = []
    for segs in comps.values():
        xs = [v for s in segs for v in (s["x0"], s["x1"])]
        ys = [v for s in segs for v in (s["y0"], s["y1"])]
        out.append({"svc": svc, "segs": segs, "bbox": (min(xs), min(ys), max(xs), max(ys))})
    out.sort(key=lambda c: -len(c["segs"]))
    return out


# --------------------------------------------------------------------------- #
# equipment, levels, titles
# --------------------------------------------------------------------------- #
EQ_PREFIX = r"(?:PAHU|AHU|RAF|TEAF|TEF|EAF|OAF|SAF|FCU|CRAC|BIBO|HPF)"
EQ_START = re.compile(r"^(" + EQ_PREFIX + r")-")
EQ_FULL = re.compile(r"^(" + EQ_PREFIX + r")-(B-[A-Z0-9]{2}-[0-9A-Z]+-[0-9]{2}[A-Z]?(?:/[A-Z0-9]+)?|B-[A-Z0-9]{2}-[A-Z0-9]+)\s*(\(.*\))?\s*$")
LEVEL_RE = re.compile(r"^(?:LOWER BASEMENT \d|BASEMENT \d|\d{1,2} ?(?:ST|ND|RD|TH) STOREY(?: LEVEL)?|ROOF(?: TOP)?(?: LEVEL)?|GROUND (?:STOREY|FLOOR)(?: LEVEL)?)$", re.I)
FLOW_RE = re.compile(r"^\s*([\d,]+(?:\.\d+)?)\s*L\s*/\s*S\s*$", re.I)


def _flow_value(text: str) -> Optional[float]:
    m = FLOW_RE.match(text or "")
    return float(m.group(1).replace(",", "")) if m else None


def extract_equipment(sh: Sheet) -> List[dict]:
    hl = [l for l in sh.lines if abs(l["dir"][0] - 1) < 0.01 and l["x1"] < 3040]
    out: List[dict] = []
    used = set()
    for i, l in enumerate(hl):
        t = l["text"].strip()
        if not EQ_START.match(t) or l["size"] < 11.5:
            continue
        parts = [l]
        text = t
        # wrapped tag: 'AHU-' + 'B-02-...' or 'EAF-B-B3-' + continuation
        if not EQ_FULL.match(text) or text.endswith("-"):
            for j, n in enumerate(hl):
                if j == i or j in used:
                    continue
                nt = n["text"].strip()
                if n["y0"] - l["y1"] > -8 and n["y0"] - l["y1"] < 12 and abs(_lc(n)[0] - _lc(l)[0]) < 70 and n["size"] >= 11.5 \
                        and re.match(r"^(B-[A-Z0-9]{2}|[0-9A-Z]{2,}-?[0-9]{0,2}[A-Z]?)", nt) and not re.search(r"L/S", nt, re.I) and not EQ_START.match(nt):
                    text = text.rstrip() + nt
                    parts.append(n)
                    used.add(j)
                    break
        m = EQ_FULL.match(text)
        if not m:
            continue
        used.add(i)
        box = [min(p["x0"] for p in parts), min(p["y0"] for p in parts), max(p["x1"] for p in parts), max(p["y1"] for p in parts)]
        tag = m.group(1) + "-" + m.group(2)
        qual = (m.group(3) or "").strip()
        # duty: nearest 'NNNN L/s' directly under the tag (x-overlap)
        duty = None
        for n in hl:
            if FLOW_RE.match(n["text"]) and -6 <= n["y0"] - box[3] <= 26 and n["x0"] < box[2] + 4 and n["x1"] > box[0] - 4:
                if duty is None or n["y0"] < duty["y0"]:
                    duty = n
        out.append({"tag": tag, "type": m.group(1), "qualifier": qual, "box": box, "text_lines": [p["text"].strip() for p in parts],
                    "duty_text": duty["text"].strip() if duty else None, "duty_box": [duty["x0"], duty["y0"], duty["x1"], duty["y1"]] if duty else None,
                    "duty": _flow_value(duty["text"]) if duty else None, "size": l["size"]})
    return out


def extract_levels(sh: Sheet) -> List[dict]:
    out = []
    for l in sh.lines:
        t = _norm(l["text"])
        if abs(l["dir"][0] - 1) < 0.01 and l["x1"] < 3040 and LEVEL_RE.match(t) and l["size"] >= 11.5:
            out.append({"text": t, "x": _lc(l)[0], "y0": l["y0"], "y1": l["y1"], "box": [l["x0"], l["y0"], l["x1"], l["y1"]]})
    return out


def extract_titles(sh: Sheet) -> List[dict]:
    out = []
    for l in sh.lines:
        if abs(l["dir"][0] - 1) < 0.01 and 17.5 <= l["size"] <= 22 and l["x1"] < 3040 and _norm(l["text"]) and l["y0"] < 2260:
            out.append({"text": _norm(l["text"]), "x0": l["x0"], "x1": l["x1"], "y0": l["y0"], "y1": l["y1"], "box": [l["x0"], l["y0"], l["x1"], l["y1"]]})
    out.sort(key=lambda t: (t["y0"], t["x0"]))
    return out


def title_for(titles: List[dict], box) -> Optional[dict]:
    best, bs = None, 1e18
    for t in titles:
        dy = t["y0"] - box[3]
        if dy < -5:
            continue
        cx0, cx1 = box[0], box[2]
        dx = 0.0 if (t["x1"] >= cx0 and t["x0"] <= cx1) else min(abs(t["x0"] - cx1), abs(t["x1"] - cx0))
        sc = dy + 0.5 * dx
        if sc < bs:
            best, bs = t, sc
    return best


# --------------------------------------------------------------------------- #
# duct graph and associations
# --------------------------------------------------------------------------- #
def _dist_pt_box(px, py, b):
    dx = max(b[0] - px, 0, px - b[2])
    dy = max(b[1] - py, 0, py - b[3])
    return math.hypot(dx, dy)


def _dist_comp_box(c, b):
    best = 1e9
    for s in c["segs"]:
        for (px, py) in ((s["x0"], s["y0"]), (s["x1"], s["y1"])):
            best = min(best, _dist_pt_box(px, py, b))
    return best


def build_components(sh: Sheet, tol: float = 20.0) -> Dict[str, List[dict]]:
    """Same-colour duct components; short risers that stop at a damper or VAV
    symbol are linked to the header drawn above them (gap <= 110 pt)."""
    out = {}
    for svc in ("SA", "RA", "EA", "IEA", "SPILL", "OA"):
        comps = service_components(sh, svc, tol)
        def _is_big(c):
            xs_ = [v for sg in c["segs"] for v in (sg["x0"], sg["x1"])]
            return len(c["segs"]) >= 14 or (max(xs_) - min(xs_)) >= 150
        big = [c for c in comps if _is_big(c)]
        parent = list(range(len(comps)))
        idx = {id(c): i for i, c in enumerate(comps)}

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a
        for c in comps:
            if _is_big(c):
                continue
            bx = c["bbox"]
            if bx[3] - bx[1] > 160 or bx[2] - bx[0] > 120:
                continue
            xc = (bx[0] + bx[2]) / 2
            best, bg = None, 1e9
            for h in big:
                if not (h["bbox"][0] - 3 <= xc <= h["bbox"][2] + 3):
                    continue
                for s in h["segs"]:
                    if _ish(s):
                        gap = bx[1] - s["y0"]
                        if -3 <= gap <= 110 and gap < bg:
                            best, bg = h, gap
            if best is not None:
                parent[find(idx[id(c)])] = find(idx[id(best)])
        # collinear continuation across damper/VAV symbols: vertical pieces at the same x,
        # one ending <= 60 pt above where the next begins
        vends = []
        for c in comps:
            for sg in c["segs"]:
                if _isv(sg):
                    vends.append((round(sg["x0"], 1), min(sg["y0"], sg["y1"]), max(sg["y0"], sg["y1"]), idx[id(c)]))
        vends.sort()
        for i1 in range(len(vends)):
            xa, ya0, ya1, ca = vends[i1]
            for i2 in range(i1 + 1, len(vends)):
                xb, yb0, yb1, cb = vends[i2]
                if xb - xa > 4:
                    break
                if ca == cb or find(ca) == find(cb):
                    continue
                gap = max(ya0 - yb1, yb0 - ya1)
                if -1 <= gap <= 60:
                    parent[find(ca)] = find(cb)
        merged = collections.defaultdict(list)
        for c in comps:
            merged[find(idx[id(c)])].extend(c["segs"])
        res = []
        for segs in merged.values():
            xs = [v for s in segs for v in (s["x0"], s["x1"])]
            ys = [v for s in segs for v in (s["y0"], s["y1"])]
            res.append({"svc": svc, "segs": segs, "bbox": (min(xs), min(ys), max(xs), max(ys))})
        out[svc] = res
    return out


def riser_component(comps: List[dict], x0: float, x1: float, ytop: float):
    """Component of one service whose vertical stroke crosses the room-box top edge."""
    best, bn = None, 0
    for c in comps:
        n = 0
        for s in c["segs"]:
            if _isv(s) and x0 - 3 <= s["x0"] <= x1 + 3 and min(s["y0"], s["y1"]) <= ytop + 6 and max(s["y0"], s["y1"]) >= ytop - 25:
                n += 1
        if n > bn:
            best, bn = c, n
    return best


# --------------------------------------------------------------------------- #
# value parsing
# --------------------------------------------------------------------------- #
AIRFLOW_LINE = re.compile(r"^\s*(SA|RA|EA|EX|OA|IEA)\s*[:\-]?\s*([\d,]+(?:\.\d+)?)?\s*(?:L\s*/\s*S|l/s|LPS)?\s*$", re.I)


def parse_airflow(lines: List[dict]) -> dict:
    """Split an AIRFLOW cell into supply / return / exhaust / OA with as-printed text."""
    res = {"supply": None, "return": None, "exhaust": None, "oa_text": None, "as_printed": [], "boxes": {}, "other": []}
    for l in lines:
        t = l["text"].strip()
        res["as_printed"].append(t)
        m = AIRFLOW_LINE.match(t)
        box = [l["x0"], l["y0"], l["x1"], l["y1"]]
        if not m:
            if re.match(r"^\s*OA\b", t, re.I) or re.search(r"\bACH\b", t, re.I):
                res["oa_text"] = t
                res["boxes"]["oa"] = box
            else:
                res["other"].append(t)
            continue
        key, val = m.group(1).upper(), m.group(2)
        v = float(val.replace(",", "")) if val else None
        if key == "SA":
            res["supply"], res["boxes"]["supply"] = v, box
        elif key == "RA":
            res["return"], res["boxes"]["return"] = v, box
        elif key in ("EA", "EX"):
            res["exhaust"], res["boxes"]["exhaust"] = v, box
        elif key == "OA":
            res["oa_text"] = t
            res["boxes"]["oa"] = box
    return res


def _num(v):
    if v is None:
        return ""
    return int(v) if float(v).is_integer() else v


def _join(lines):
    return " ".join(l["text"].strip() for l in lines)


def _mode_is_norm(name: str) -> bool:
    n = (name or "").upper().replace(" ", "")
    return n in ("NORM", "NORMAL", "NORM(R)", "")


# --------------------------------------------------------------------------- #
# title block and sheet-level facts
# --------------------------------------------------------------------------- #
def title_block(sh: Sheet) -> dict:
    """Drawing number, revision, date, stage and title from the right-hand title block."""
    tb = [l for l in sh.lines if l["x0"] > 3040 and abs(l["dir"][0] - 1) < 0.01]
    out = {}

    def below(label, dx=30, dy=40):
        cands = [l for l in tb if _norm(l["text"]).upper() == label]
        if not cands:
            return None
        lab = max(cands, key=lambda l: l["y0"])
        vals = [l for l in tb if 0 < l["y0"] - lab["y0"] <= dy and abs(l["x0"] - lab["x0"]) <= dx and _norm(l["text"]).upper() != label
                and l["size"] >= 11]
        vals.sort(key=lambda l: l["y0"])
        return vals[0] if vals else None
    for key, label in (("drawing_no", "DRAWING NO."), ("rev", "REV."), ("date", "DATE"), ("stage", "STAGE"),
                       ("scale", "SCALE"), ("job_no", "JOB NO.")):
        v = below(label)
        out[key] = _norm(v["text"]) if v else None
        out[key + "_box"] = [round(v["x0"], 1), round(v["y0"], 1), round(v["x1"], 1), round(v["y1"], 1)] if v else None
    # drawing title lines (size 16)
    tl = sorted([l for l in tb if 15.5 <= l["size"] <= 16.5 and l["y0"] > 1850], key=lambda l: l["y0"])
    tl = [l for l in tl if not re.fullmatch(r"\d+[A-Z]?", _norm(l["text"]))]
    out["drawing_title"] = " ".join(_norm(l["text"]) for l in tl) if tl else None
    stamps = [_norm(l["text"]) for l in sh.lines if re.search(r"CORRIGENDUM \d|WORKING DRAWING|CONTRACT DRAWING|TENDER", l["text"], re.I) and l["size"] >= 12]
    out["stamps"] = sorted(set(stamps))
    # revision table (right-hand column): date / rev / description rows
    rows = []
    desc = [l for l in tb if re.search(r"FOR (NSC )?TENDER|FOR WORKING|CORRIGENDUM|FOR CONSTRUCTION|FOR TENDER", l["text"], re.I) and l["size"] < 9 and l["y0"] < 700]
    for d in sorted(desc, key=lambda l: l["y0"]):
        cells = [l for l in tb if abs(_lc(l)[1] - _lc(d)[1]) < 3.5 and l is not d and l["size"] < 9]
        cells.sort(key=lambda l: l["x0"])
        rows.append([_norm(c["text"]) for c in cells] + [_norm(d["text"])])
    out["revisions"] = rows
    return out


# --------------------------------------------------------------------------- #
# per-sheet model
# --------------------------------------------------------------------------- #
def _tag_stem(tag: str) -> str:
    """Tag without type prefix and without the trailing A/B unit letter."""
    t = re.sub(r"^[A-Z]+-", "", tag)
    t = re.sub(r"(/[A-Z0-9]+)+$", "", t)
    t = re.sub(r"(?<=\d)[A-Z]$", "", t)
    return t


def _stem(tag: str) -> str:
    # 'AHU-B-02-01EDP3WAIT-01A' -> 'B-02-01EDP3WAIT-01'; 'RAF-...-05/A' -> '...-05'
    t = re.sub(r"^[A-Z]+-", "", tag)
    t = re.sub(r"/[A-Z0-9/]+$", "", t)
    t = re.sub(r"(?<=\d)[A-Z]$", "", t)
    return t


def _connect_texts(sh: Sheet) -> List[dict]:
    """Size-10 note lines that mention a tag or a connection (joined when they wrap)."""
    hl = sorted([l for l in sh.lines if abs(l["dir"][0] - 1) < 0.01 and l["x1"] < 3040 and l["size"] < 11.5 and l["y0"] < 2260],
                key=lambda l: (round(l["x0"] / 4), l["y0"]))
    notes = []
    key = re.compile(r"CONNECTED (TO|FROM)|MAKE-UP|OUTDOOR AIR FROM|PAHU|BIBO|SPILL AIR|REFER TO|OA MAKE", re.I)
    taken = set()
    for i, l in enumerate(sorted(hl, key=lambda l: (l["y0"], l["x0"]))):
        pass
    srt = sorted(hl, key=lambda l: (l["y0"], l["x0"]))
    for i, l in enumerate(srt):
        if i in taken or not key.search(l["text"]):
            continue
        parts = [l]
        cur = l
        for j in range(len(srt)):
            n = srt[j]
            if j in taken or n is cur or n in parts:
                continue
            if 0 < n["y0"] - cur["y0"] <= 16 and abs(n["x0"] - cur["x0"]) <= 40 and not re.match(r"^(VCD|VAV|NRD|FD|M|A|SA|RA|EA|OA)\s*$", n["text"].strip()) \
                    and not re.search(r"L/s\s*$", cur["text"].strip(), re.I) and not re.match(r"^\s*(SA|RA|EA|OA|IEA)\b", n["text"]):
                parts.append(n)
                cur = n
        txt = _norm(" ".join(p["text"] for p in parts))
        box = [min(p["x0"] for p in parts), min(p["y0"] for p in parts), max(p["x1"] for p in parts), max(p["y1"] for p in parts)]
        notes.append({"text": txt, "box": box})
    # drop notes that are subsets of others
    uniq = []
    for n in notes:
        if not any(n is not m and n["text"] in m["text"] and len(m["text"]) > len(n["text"]) for m in notes):
            uniq.append(n)
    return uniq


def _free_flow_labels(sh: Sheet, tables: List[RoomTable]) -> List[dict]:
    out = []
    for l in sh.lines:
        t = _norm(l["text"])
        m = re.match(r"^(OA|IEA|SPILL AIR|SA|RA|EA)\s*-?\s*([\d,]*)\s*L\s*/\s*[sS]$", t)
        if not m or abs(l["dir"][0] - 1) > 0.01 and abs(l["dir"][1]) < 0.9:
            continue
        cx, cy = _lc(l)
        if any(tb.left - 1 <= cx <= tb.right + 1 and tb.box[1] - 1 <= cy <= tb.box[3] + 1 for tb in tables):
            continue
        if l["x0"] > 3040:
            continue
        out.append({"text": t, "kind": m.group(1), "value": float(m.group(2).replace(",", "")) if m.group(2) else None,
                    "box": [l["x0"], l["y0"], l["x1"], l["y1"]], "vertical": abs(l["dir"][0]) < 0.5})
    return out


FAN_TYPES_RA = ("RAF",)
FAN_TYPES_EA = ("TEAF", "EAF", "TEF", "OAF", "SAF")


def parse_sheet(path: str, info: dict) -> dict:
    """Parse one schematic PDF into rooms, equipment, connections and AHU groups."""
    sh = Sheet(path)
    res = {"info": info, "warnings": list(), "rooms": [], "equipment": [], "connections": [], "groups": [],
           "pages": sh.pages, "sha256": sh.sha256, "path": path}
    if sh.pages != 1:
        res["warnings"].append("PDF has %d pages; only page 1 read" % sh.pages)
    tb = title_block(sh)
    res["title_block"] = tb
    tables = find_room_tables(sh)
    attach_names(sh, tables)
    res["warnings"].extend(sh.table_warnings)
    eq = extract_equipment(sh)
    levels = extract_levels(sh)
    titles = extract_titles(sh)
    comps = build_components(sh)
    notes = _connect_texts(sh)
    free = _free_flow_labels(sh, tables)
    res["titles"] = [t["text"] for t in titles]
    res["n_tables"] = len(tables)
    prov_file = info["rel_path"]

    def prov(box, method, conf):
        return {"file": prov_file, "page": 1, "box": [round(v, 1) for v in box] if box else None, "method": method, "confidence": conf}

    # ---- equipment -----------------------------------------------------
    for e in eq:
        # level band: nearest level label below the tag (labels sit under their dashed line)
        below = [l for l in levels if l["y0"] >= e["box"][1] - 2]
        # prefer labels in the same drawing block: the closest one vertically
        lvl = min(below, key=lambda l: (l["y0"] - e["box"][1]) + 0.05 * abs(l["x"] - _lc({"x0": e["box"][0], "x1": e["box"][2], "y0": e["box"][1], "y1": e["box"][3]})[0])) if below else None
        e["level_band"] = lvl["text"] if lvl else None
        m = re.match(r"^[A-Z]+-B-([A-Z0-9]{2})-([0-9A-Z]{2})", e["tag"])
        e["tag_plant_code"], e["tag_served_code"] = (m.group(1), m.group(2)) if m else (None, None)
    ahus = [e for e in eq if e["type"] == "AHU"]
    res["equipment_raw"] = eq

    # ---- rooms ---------------------------------------------------------
    sa_by_comp: Dict[int, set] = {}
    for ci, c in enumerate(comps["SA"]):
        near = [a["tag"] for a in ahus if _dist_comp_box(c, a["box"]) <= 110]
        if near:
            sa_by_comp[id(c)] = set(near)
    notes_iea = [n for n in notes if re.search(r"CONNECTED TO", n["text"], re.I)]
    room_records = []
    for t in tables:
        top = t.box[1]
        o = t.outline
        if o:
            ox0, ox1, oy = o[0], o[2], o[1]
        else:
            ox0, ox1, oy = t.left, t.right, top - 60
        raw_name = " ".join(l["text"].strip() for l in sorted(t.name_lines, key=lambda l: (l["y0"], l["x0"])))
        name_box = [min(l["x0"] for l in t.name_lines), min(l["y0"] for l in t.name_lines), max(l["x1"] for l in t.name_lines),
                    max(l["y1"] for l in t.name_lines)] if t.name_lines else None
        tit = title_for(titles, [ox0, oy, ox1, t.box[3]])
        services, svc_comp = [], {}
        for svc in ("SA", "RA", "EA", "IEA"):
            c = riser_component(comps[svc], ox0, ox1, oy)
            if c is not None:
                services.append(svc)
                svc_comp[svc] = c
        rec = {"table": t, "name": raw_name, "name_box": name_box, "title": tit["text"] if tit else "", "title_box": tit["box"] if tit else None,
               "outline": o, "services": services, "svc_comp": svc_comp, "ahu_tags": [], "fan_tags": [], "iea_target": "",
               "assoc_method": "", "assoc_conf": ""}
        if "SA" in svc_comp and id(svc_comp["SA"]) in sa_by_comp:
            rec["ahu_tags"] = sorted(sa_by_comp[id(svc_comp["SA"])])
            rec["assoc_method"] = "SA (cyan) riser crossing the room-box top, joined to the AHU outlet duct"
            rec["assoc_conf"] = "high"
        for svc, types in (("RA", FAN_TYPES_RA), ("EA", FAN_TYPES_EA)):
            if svc in svc_comp:
                fans = [e["tag"] for e in eq if e["type"] in types and _dist_comp_box(svc_comp[svc], e["box"]) <= 125]
                rec["fan_tags"].extend(sorted(set(fans)))
        if "IEA" in svc_comp:
            best, bd = None, 1e9
            for n in notes_iea:
                d = _dist_comp_box(svc_comp["IEA"], n["box"])
                if d < bd:
                    best, bd = n, d
            if best is not None and bd <= 260:
                mm = re.search(r"\(?(BIBO-[A-Z0-9-]+)\)?", best["text"])
                rec["iea_target"] = mm.group(1) if mm else ""
                rec["iea_note"] = best["text"]
        room_records.append(rec)

    # ---- fall-back AHU association by fan stem / drawing block ----------
    ahu_stems = collections.defaultdict(list)
    for a in ahus:
        ahu_stems[_stem(a["tag"])].append(a["tag"])
    for rec in room_records:
        if rec["ahu_tags"]:
            continue
        stems = {_stem(f) for f in rec["fan_tags"]}
        tags = sorted({tg for s in stems for tg in ahu_stems.get(s, [])})
        if tags:
            rec["ahu_tags"] = tags
            rec["assoc_method"] = "no SA riser; serving fan tag shares the AHU tag stem"
            rec["assoc_conf"] = "medium"
    res["room_records"] = room_records

    # ---- groups: AHUs sharing one SA component ---------------------------
    groups = []
    seen = set()
    for ci, c in enumerate(comps["SA"]):
        tags = sa_by_comp.get(id(c))
        if tags and frozenset(tags) not in seen:
            seen.add(frozenset(tags))
            groups.append(sorted(tags))
    # merge groups that overlap
    merged = []
    for g in groups:
        gs = set(g)
        for m_ in merged:
            if gs & m_:
                m_ |= gs
                break
        else:
            merged.append(gs)
    # AHUs with no SA component seen
    covered = set().union(*merged) if merged else set()
    for a in ahus:
        if a["tag"] not in covered:
            merged.append({a["tag"]})
            res["warnings"].append("AHU %s: no SA duct component found near the tag" % a["tag"])
    res["ahu_groups"] = [sorted(g) for g in merged]
    res["notes_text"] = notes
    res["free_labels"] = free
    res["comps"] = {k: len(v) for k, v in comps.items()}
    res["sheet_obj"] = sh
    res["tables"] = tables
    res["levels"] = levels
    res["comps_obj"] = comps
    res["eq"] = eq
    return res
