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
        # overprinted duplicates (same text at the same place) are kept once
        seen = set()
        uw = []
        for w in self.words:
            k = (w["text"], round(w["x0"], 1), round(w["y0"], 1))
            if k not in seen:
                seen.add(k)
                uw.append(w)
        self.words = uw
        seen = set()
        ul = []
        for l in self.lines:
            k = (l["text"], round(l["x0"], 1), round(l["y0"], 1))
            if k not in seen:
                seen.add(k)
                ul.append(l)
        self.lines = ul
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
MODE_RE = re.compile(r"^(NORM|NORMAL|SET ?BACK|SETBACK|SINGLE|PAND|PANDEMIC|FIRE|STANDBY|AFTER ?HOURS?|NIGHT|PURGE|EMERGENCY|SMOKE|UNOCCUPIED|OCCUPIED|RECOVERY|DAY|ISO|ISOLATION|FUTURE)\b", re.I)
ARROW_RE = re.compile(r"^(SA|RA|EA|EX|OA|IEA|KEA|KMA|RA ?/ ?EX|EX ?/ ?RA|EA ?/ ?EX|SA ?/ ?RA)$", re.I)
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
        self.name_tier = ""
        self.borderless = False

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


def _borderless_table(sh: Sheet, a: dict, f: dict, m: Optional[dict], below, next_left: Optional[float] = None) -> Optional[RoomTable]:
    """Table without drawn frame (MOT layout): rows from the label positions, columns from the
    MODE header cells, cell text assembled from words (the PDF may join columns into one line)."""
    t = RoomTable()
    labs = {}
    for key in ("pressure", "class", "humidity", "temperature"):
        l = below(key, 0, 260)
        if l is None:
            return None
        labs[key] = l
    t.left = a["x0"] - 3.0
    lab_lines = [a, f] + list(labs.values()) + ([m] if m else [])
    t.label_right = max(l["x1"] for l in lab_lines) + 3.0
    if m is None:
        # MOT layout: the mode names (NORMAL / FUTURE ...) are printed above the value columns
        # without a MODE label in the label column.
        cand_h = [l for l in sh.lines if abs(l["dir"][0] - 1) < 0.01 and l["size"] < 11.5 and len(_norm(l["text"])) <= 14
                  and MODE_RE.match(_norm(l["text"])) and t.label_right - 4 <= l["x0"] <= t.label_right + 200
                  and a["y0"] - 95 <= l["y0"] <= a["y0"] - 20 and (next_left is None or l["x1"] < next_left - 3.0)]
        if cand_h:
            hy = max(l["y0"] for l in cand_h)
            cand_h = [l for l in cand_h if abs(l["y0"] - hy) <= 3.5]
            m = {"x0": t.label_right - 2.0, "x1": t.label_right - 2.0, "y0": min(l["y0"] for l in cand_h), "y1": max(l["y1"] for l in cand_h),
                 "text": "", "dir": (1.0, 0.0), "size": 9.9}
            t.method_notes.append("mode names read from the column headers (no MODE label printed in the label column)")
    hdr = [] if m is None else sorted([l for l in sh.lines if abs(l["dir"][0] - 1) < 0.01 and abs(_lc(l)[1] - _lc(m)[1]) <= 3.5 and l["x0"] > m["x1"] + 2
                  and l["x0"] < m["x1"] + 320 and l["size"] < 11.5 and (next_left is None or l["x1"] < next_left - 3.0)
                  and (MODE_RE.match(_norm(l["text"])) or re.match(r"^(SET|BACK|PASS|\(R\))$", _norm(l["text"]), re.I))], key=lambda l: l["x0"])
    cells = []
    for l in hdr:
        if cells and l["x0"] - cells[-1][-1]["x1"] <= 6:
            cells[-1].append(l)
        else:
            cells.append([l])
    # stop at the first large gap (next table or notes)
    keep = []
    for c in cells:
        if keep and min(l["x0"] for l in c) - max(l["x1"] for l in keep[-1]) > 40:
            break
        keep.append(c)
    cells = keep
    if m is not None and not cells:
        return None
    if cells:
        centres = [(min(l["x0"] for l in c) + max(l["x1"] for l in c)) / 2 for c in cells]
        t.col_names = [" ".join(_norm(l["text"]) for l in c) for c in cells]
        t.col_boxes = [[min(l["x0"] for l in c), min(l["y0"] for l in c), max(l["x1"] for l in c), max(l["y1"] for l in c)] for c in cells]
        half = 29.0
        t.right = centres[-1] + half + 3
        edges = [t.label_right] + [(centres[i] + centres[i + 1]) / 2 for i in range(len(centres) - 1)] + [t.right]
        t.cols = [(edges[i], edges[i + 1]) for i in range(len(centres))]
    else:
        # no MODE row: one value column to the right of the labels
        t.right = t.label_right + 85.0
        if next_left is not None:
            t.right = min(t.right, next_left - 3.0)
        t.col_names = [""]
        t.col_boxes = [None]
        t.cols = [(t.label_right, t.right)]
    # row bands
    centre_y = {}
    keys = ["pressure", "class", "humidity", "temperature"]
    order = [("pressure", labs["pressure"]), ("class", labs["class"]), ("humidity", labs["humidity"]), ("temperature", labs["temperature"]), ("filter", f)]
    # label block centre = mean centre of the label-column lines between this label and the next
    lab_col = [l for l in sh.lines if abs(l["dir"][0] - 1) < 0.01 and abs(l["x0"] - a["x0"]) <= 12 and l["y0"] >= a["y0"] - 1 and l["y1"] <= f["y1"] + 1]
    bounds = [x[1]["y0"] for x in order] + [f["y1"] + 8]
    for i, (key, lab) in enumerate(order):
        blk = [l for l in lab_col if bounds[i] - 1 <= l["y0"] < bounds[i + 1] - 1]
        centre_y[key] = sum(_lc(l)[1] for l in blk) / len(blk) if blk else _lc(lab)[1]
    cy_list = [centre_y[k] for k, _ in order]
    if m is not None:
        t.rows["mode"] = (m["y0"] - 2.0, m["y1"] + 2.0)
        t.rows["airflow"] = (m["y1"] + 2.0, labs["pressure"]["y0"] - 1.0)
    else:
        t.rows["airflow"] = (a["y0"] - 34.0, labs["pressure"]["y0"] - 1.0)
    top_y = t.rows["mode"][1] if m is not None else t.rows["airflow"][0]
    for i, (key, _) in enumerate(order):
        lo = labs["pressure"]["y0"] - 1.0 if i == 0 else (cy_list[i - 1] + cy_list[i]) / 2
        hi = (cy_list[i] + cy_list[i + 1]) / 2 if i + 1 < len(order) else f["y1"] + 3.0
        t.rows[key] = (lo, hi)
    t.seps = sorted({v for r in t.rows.values() for v in r})
    # cell text from words
    words = [w for w in sh.words if t.label_right - 1 <= (w["x0"] + w["x1"]) / 2 <= t.right + 1 and top_y <= (w["y0"] + w["y1"]) / 2 <= t.rows["filter"][1]]
    for key, (y0, y1) in t.rows.items():
        if key == "mode":
            continue
        for ci, (c0, c1) in enumerate(t.cols):
            ws = [w for w in words if y0 <= (w["y0"] + w["y1"]) / 2 <= y1 and c0 <= (w["x0"] + w["x1"]) / 2 <= c1]
            if not ws:
                continue
            ws.sort(key=lambda w: (round((w["y0"] + w["y1"]) / 2 / 4), w["x0"]))
            rowsw = []
            for w in ws:
                if rowsw and abs((w["y0"] + w["y1"]) / 2 - rowsw[-1][0]) <= 4:
                    rowsw[-1][1].append(w)
                else:
                    rowsw.append([(w["y0"] + w["y1"]) / 2, [w]])
            seen_txt = set()
            for _, grp in rowsw:
                grp.sort(key=lambda w: w["x0"])
                kk = (" ".join(w["text"] for w in grp), round(grp[0]["x0"] / 3), round(grp[0]["y0"] / 3))
                if kk in seen_txt:
                    continue
                seen_txt.add(kk)
                t.cells.setdefault((key, ci), []).append({"x0": grp[0]["x0"], "y0": min(w["y0"] for w in grp), "x1": grp[-1]["x1"],
                                                          "y1": max(w["y1"] for w in grp), "text": " ".join(w["text"] for w in grp),
                                                          "dir": (1.0, 0.0), "size": 9.9})
    t.borderless = True
    t.method_notes.append("borderless table: no frame lines drawn; rows from the label positions, columns from the MODE header, cell text from words")
    return t



_FLOW_KEY = re.compile(r"^(SA|RA|EA|EX|OA|IEA|KEA|KMA)$", re.I)


_KEY_IN_TEXT = re.compile(r"(?:^|\s|(?<=/s)|(?<=/S))(?:SA|RA|EA|EX|OA|IEA|KEA|KMA)(?=\s|\d|$)")
_GLUED_KEY = re.compile(r"^(.*[Ss])(SA|RA|EA|EX|OA)$")


def _split_multi_mode(sh: Sheet, lines: List[dict], key: str, y0: float, y1: float) -> List[dict]:
    """Where the PDF joins the values of two mode columns into one text line
    ('SA 210 L/s SA 210 L/s'), re-cut the line from its words at every second flow key so
    that each piece can be assigned to its own column. Only AIRFLOW cells with two or more
    flow keys on one line are touched; every other line is returned unchanged."""
    out = []
    for l in lines:
        cy = _lc(l)[1]
        if key != "airflow" or not (y0 - 0.5 <= cy <= y1 + 0.5):
            out.append(l)
            continue
        if len(_KEY_IN_TEXT.findall(l["text"])) < 2:
            out.append(l)
            continue
        ws = [w for w in sh.words if l["x0"] - 0.6 <= w["x0"] and w["x1"] <= l["x1"] + 0.6 and l["y0"] - 1 <= (w["y0"] + w["y1"]) / 2 <= l["y1"] + 1]
        ws.sort(key=lambda w: w["x0"])
        toks = []
        for w in ws:
            g = _GLUED_KEY.match(w["text"])
            if g:
                # '1005L/sRA' : the unit of one column runs into the flow key of the next
                cut = w["x0"] + (w["x1"] - w["x0"]) * len(g.group(1)) / len(w["text"])
                toks.append(dict(w, text=g.group(1), x1=cut))
                toks.append(dict(w, text=g.group(2), x0=cut))
            else:
                toks.append(w)
        segs: List[List[dict]] = []
        for w in toks:
            if _FLOW_KEY.match(w["text"]) or not segs:
                segs.append([w])
            else:
                segs[-1].append(w)
        for g in segs:
            piece = dict(l)
            piece.update({"x0": g[0]["x0"], "x1": g[-1]["x1"], "y0": min(w["y0"] for w in g), "y1": max(w["y1"] for w in g),
                          "text": " ".join(w["text"] for w in g), "split_from_joined_line": True})
            out.append(piece)
    return out


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
        c = [l for l in labels["mode"] if a["x0"] - 6 <= _lc(l)[0] <= a["x0"] + 90 and -95 <= l["y0"] - a["y0"] < 0]
        if c:
            m = max(c, key=lambda l: l["y0"])
        # label-column separators: horizontal black strokes starting at the left frame
        y_lo = (m["y0"] if m else a["y0"]) - 80
        y_hi = f["y1"] + 10
        cand = [s for s in blk_h if y_lo <= s["y0"] <= y_hi and a["x0"] - 16 <= min(s["x0"], s["x1"]) <= a["x0"] - 0.5
                and abs(s["x1"] - s["x0"]) > 20]
        if not cand:
            nl = [o["x0"] for o in anchors if o is not a and o["x0"] > a["x0"] + 20 and abs(o["y0"] - a["y0"]) < 40]
            bt = _borderless_table(sh, a, f, m, below, min(nl) if nl else None)
            if bt is not None:
                tables.append(bt)
                continue
            t.warnings.append("no label-column separators at AIRFLOW (%.0f,%.0f) and borderless reading failed" % (a["x0"], a["y0"]))
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
        if t.borderless:
            continue
        lines_in = [l for l in hl if t.label_right - 1 <= _lc(l)[0] <= t.right + 1 and t.rows["airflow"][0] - 80 <= _lc(l)[1] <= t.box[3] + 1]
        # column geometry: header cells if a MODE row exists, else one column
        if "mode" in t.rows:
            my0, my1 = t.rows["mode"]
            hdr = sorted([l for l in lines_in if my0 - 0.5 <= _lc(l)[1] <= my1 + 0.5 and (MODE_RE.match(_norm(l["text"])) or re.match(r"^(SET|BACK|PASS|\(R\))$", _norm(l["text"]), re.I))],
                         key=lambda l: l["x0"])
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
                t.right = min(t.right, centres[-1] + 32.0)
                edges = [t.label_right] + [(centres[i] + centres[i + 1]) / 2 for i in range(len(centres) - 1)] + [t.right]
                t.cols = [(edges[i], edges[i + 1]) for i in range(len(centres))]
            else:
                t.warnings.append("MODE row without header text")
        if not t.cols:
            t.right = min(t.right, t.label_right + 120.0)
            t.cols = [(t.label_right, t.right)]
            t.col_names = [""]
            t.col_boxes = [None]
        for key, (y0, y1) in t.rows.items():
            if key == "mode":
                continue
            for l in _split_multi_mode(sh, lines_in, key, y0, y1):
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


NOTE_RE = re.compile(r"SHALL|REFER TO|CONNECTION|MODULE|TERMINAL|TYP\.|PORT\b|INJECTION|CEILING|ROOMSIDE|FRESH AIR|SPILL|CONNECTED|NO\. OF|\bL/S\b|\d+ ?x ?\d+ ?mm|\bNOS\b \d|\d+ NOS\b", re.I)
DAMPER_RE = re.compile(r"^(VCD|VAV|VV|NRD|FD|M|A|B|N/O|N/C|HEPA|ULPA|ATTENUATOR|PRESSURE STABILIZER|PRESSURE|STABILIZER|FCU)$", re.I)


def attach_names(sh: Sheet, tables: List[RoomTable]) -> None:
    """Room name, typical count and branch-arrow lines of each table.

    The name is looked for in three places, in this order: above the table inside the
    room box (pilot layout), to the right of the table inside the room box, below the
    table inside the room box (MOT layout).  The tier used is recorded."""
    hl = [l for l in sh.lines if abs(l["dir"][0] - 1) < 0.01 and l["x1"] < 3040]
    runs = _vertical_runs(sh)
    hsegs = [sg for sg in sh.segs if sg["col"] == BLACK and _ish(sg)]

    def in_any_table(l, skip=None):
        cx, cy = _lc(l)
        return any(o.left - 2 <= cx <= o.right + 2 and o.box[1] - 2 <= cy <= o.box[3] + 2 for o in tables)

    for t in tables:
        top, bot = t.box[1], t.box[3]
        t.outline = room_outline(runs, t, hsegs)
        if t.outline:
            x0, x1 = t.outline[0] - 2, t.outline[2] + 2
        else:
            # walls: nearest vertical runs either side that span the table rows
            lw = [r[0] for r in runs if r[0] <= t.left - 8 and r[1] <= top + 5 and r[2] >= bot - 5 and t.left - r[0] < 400]
            rw = [r[0] for r in runs if r[0] >= t.right + 8 and r[1] <= top + 5 and r[2] >= bot - 5 and r[0] - t.right < 500]
            if lw and rw:
                x0, x1 = max(lw) - 2, min(rw) + 2
                t.method_notes.append("room walls found either side of a borderless table; no outline top")
            else:
                nb = [o.left for o in tables if o is not t and o.left > t.left + 20 and abs(o.box[1] - top) < 60]
                x0, x1 = t.left - 4, min(min(nb) - 2 if nb else 1e9, t.right + 300)
                t.method_notes.append("no room outline found; name searched from the table left edge to the next table")
        arrows = [l for l in hl if ARROW_RE.match(_norm(l["text"])) and x0 <= _lc(l)[0] <= x1 and top - 90 <= l["y1"] <= top + 1]
        t.arrows = sorted(arrows, key=lambda l: l["x0"])
        ylim = (t.outline[1] - 2) if t.outline else top - 130
        first_bottom = min(v[1] for v in t.rows.values() if v[0] <= top + 0.5)

        def ok(l):
            tx = _norm(l["text"])
            return bool(tx) and not DAMPER_RE.match(tx) and not ARROW_RE.match(tx) and not NOTE_RE.search(tx) \
                and not (l["size"] > 11.5) and not in_any_table(l)

        def find_names(x0, x1):
            base = [l for l in hl if x0 <= _lc(l)[0] <= x1 and l not in arrows and l["size"] < 11.5]
            typ_pool = [l for l in base if TYPICAL_RE.match(_norm(l["text"])) and top - 120 <= l["y0"] <= bot + 80]
            above = [l for l in base if ylim <= l["y0"] and (l["y1"] <= top + 1 or (not t.borderless and _lc(l)[0] > t.right + 1 and l["y1"] <= first_bottom + 1))
                     and not TYPICAL_RE.match(_norm(l["text"])) and (_norm(l["text"]) and not DAMPER_RE.match(_norm(l["text"])) and not ARROW_RE.match(_norm(l["text"]))
                                                                 and not in_any_table(l)) and l["y0"] >= top - 130]
            tier = "above the table"
            above = [l for l in above if not re.match(r"^[A-Z]{2,5}-B-|^[A-Z]{2,5}-$", _norm(l["text"])) and not DUTY_RE.match(l["text"])]
            names = [l for l in above if not NOTE_RE.search(_norm(l["text"]))]
            if names:
                # keep the text block nearest above the table: walk upwards while the next line is within 16 pt
                names.sort(key=lambda l: -l["y1"])
                blk = [names[0]]
                top_ = names[0]["y0"]
                for l in names[1:]:
                    if l["y1"] >= top_ - 4 and top_ - l["y1"] <= 16 or abs(l["y1"] - blk[-1]["y1"]) <= 3:
                        blk.append(l)
                        top_ = min(top_, l["y0"])
                # several rooms' names can sit on one row: keep the x-cluster nearest the table
                blk.sort(key=lambda l: l["x0"])
                clusters = []
                for l in blk:
                    if clusters and l["x0"] - max(c["x1"] for c in clusters[-1]) <= 40:
                        clusters[-1].append(l)
                    else:
                        clusters.append([l])
                if len(clusters) > 1:
                    def _cd(cl):
                        c0, c1 = min(c["x0"] for c in cl), max(c["x1"] for c in cl)
                        gap = 0.0 if (c1 >= t.left and c0 <= t.right) else min(abs(c0 - t.right), abs(c1 - t.left))
                        return (gap, abs((c0 + c1) / 2 - (t.left + t.right) / 2))
                    blk = min(clusters, key=_cd)
                names = blk
            if not names:
                names = [l for l in base if _lc(l)[0] >= t.right - 1 and top - 5 <= l["y0"] and l["y1"] <= bot + 70 and ok(l)
                         and not TYPICAL_RE.match(_norm(l["text"]))]
                tier = "right of the table"
            if not names:
                big = [l for l in hl if x0 <= _lc(l)[0] <= x1 and l["size"] < 15.5 and l["size"] >= 11.5 and bot - 6 <= l["y0"] and l["y1"] <= bot + 60
                       and _norm(l["text"]) and not EQ_START.match(l["text"].strip()) and not LEVEL_RE.match(_norm(l["text"])) and not in_any_table(l)
                       and not DAMPER_RE.match(_norm(l["text"])) and not FLOW_RE.match(l["text"])]
                names = [l for l in base if bot - 6 <= l["y0"] and l["y1"] <= bot + 60 and ok(l) and not TYPICAL_RE.match(_norm(l["text"]))] + big
                tier = "below the table"
            names = [l for l in names if not any(o is not t and o.left - 2 <= _lc(l)[0] <= o.right + 2 and abs(o.box[1] - top) < 60 for o in tables)]
            return names, tier, typ_pool

        names, tier, typ_pool = [], "", []
        if t.borderless:
            # MOT layout: room names are set in large type under the table, centred in the room box
            nb0 = [o.left for o in tables if o is not t and o.left > t.left + 20 and abs(o.box[1] - top) < 60]
            xr0, xr1 = t.left - 20, min(min(nb0) - 5 if nb0 else 1e9, t.right + 450)
            big = [l for l in hl if xr0 <= l["x0"] <= xr1 and 12.5 <= l["size"] < 15.5 and l["y1"] <= bot + 60
                   and (bot - 6 <= l["y0"] or (l["x0"] > t.right - 1 and bot - 70 <= l["y0"]))
                   and _norm(l["text"]) and not EQ_START.match(l["text"].strip()) and not LEVEL_RE.match(_norm(l["text"]))
                   and not TYPICAL_RE.match(_norm(l["text"]))
                   and not DAMPER_RE.match(_norm(l["text"])) and not FLOW_RE.match(l["text"])]
            if big:
                names, tier = big, "below the table (large lettering)"
                typ_pool = [l for l in hl if TYPICAL_RE.match(_norm(l["text"])) and xr0 <= l["x0"] <= xr1 and top - 120 <= l["y0"] <= bot + 80]
        if not names:
            names, tier, typ_pool = find_names(x0, x1)
        if not names:
            nb = [o.left for o in tables if o is not t and o.left > t.left + 20 and abs(o.box[1] - top) < 60]
            names, tier, typ_pool2 = find_names(t.left - 4, min(min(nb) - 2 if nb else 1e9, t.right + 300))
            typ_pool = typ_pool or typ_pool2
            if names:
                t.method_notes.append("room name searched beyond the room walls up to the next table")
        t.name_tier = tier if names else ""
        typ = [l for l in typ_pool]
        if typ:
            typ.sort(key=lambda l: abs(l["y0"] - top))
            t.typical_line = typ[0]
            t.typical = int(TYPICAL_RE.match(_norm(typ[0]["text"])).group(1))
        t.name_lines = sorted(names, key=lambda l: (l["y0"], l["x0"]))
        if names and tier != "above the table":
            t.method_notes.append("room name %s" % tier)


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
DUTY_RE = re.compile(r"^\s*(?:SA\s*:?\s*)?([\d,]+(?:\.\d+)?)\s*L\s*/\s*S\s*(.*)$", re.I)


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
        # duty lines directly under the tag: 'NNNN L/s', 'SA: NNNN L/s', 'NNNN L/s (NORMAL)', one per mode
        cands = []
        other_tags = [o for o in hl if o is not l and EQ_START.match(o["text"].strip()) and o["size"] >= 11.5]
        for n in hl:
            mm = DUTY_RE.match(n["text"])
            if mm and -6 <= n["y0"] - box[3] <= 70 and n["x0"] < box[2] + 40 and n["x1"] > box[0] - 40 and n["size"] >= 11.5:
                if any(o["y0"] >= box[3] - 4 and o["y0"] < n["y0"] and o["x0"] < n["x1"] + 5 and o["x1"] > n["x0"] - 5
                       and o["x0"] < box[2] + 60 and o["x1"] > box[0] - 60 for o in other_tags):
                    continue
                cands.append((n, mm))
        cands.sort(key=lambda c: c[0]["y0"])
        duty_lines = []
        last = box[3]
        for n, mm in cands:
            if n["y0"] - last <= 26:
                duty_lines.append((n, mm))
                last = n["y1"]
        duty = None
        if duty_lines:
            norm = [d for d in duty_lines if re.search(r"NORMAL|NORM\b", d[1].group(2), re.I) or not d[1].group(2).strip()]
            duty = (norm or duty_lines)[0]
        dl = [d[0] for d in duty_lines]
        out.append({"tag": tag, "type": m.group(1), "qualifier": qual, "box": box, "text_lines": [p["text"].strip() for p in parts],
                    "duty_text": " | ".join(d["text"].strip() for d in dl) if dl else None,
                    "duty_primary_text": duty[0]["text"].strip() if duty else None,
                    "duty_box": [min(d["x0"] for d in dl), min(d["y0"] for d in dl), max(d["x1"] for d in dl), max(d["y1"] for d in dl)] if dl else None,
                    "duty": float(duty[1].group(1).replace(",", "")) if duty else None, "size": l["size"]})
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
AIRFLOW_LINE = re.compile(r"^\s*(SA|RA|EA|EX|OA|IEA|KEA)\s*[:\-]?\s*([\d,]+(?:\.\d+)?)?\s*(?:[xX]\s*(\d+))?\s*(?:L?\s*/\s*S|LPS)?\s*$", re.I)


def parse_airflow(lines: List[dict]) -> dict:
    """Split an AIRFLOW cell into supply / return / exhaust / OA with as-printed text."""
    res = {"supply": None, "return": None, "exhaust": None, "oa_text": None, "as_printed": [], "boxes": {}, "other": [], "flags": []}
    for l in lines:
        t = l["text"].strip()
        res["as_printed"].append(t)
        if re.search(r"\d\s*/\s*S\s*$", t, re.I):
            res["flags"].append(t)
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
        if m.group(3):
            res["flags"].append("multiplier: " + t)
        v = float(val.replace(",", "")) if val else None
        if key == "SA":
            res["supply"], res["boxes"]["supply"] = v, box
        elif key == "RA":
            res["return"], res["boxes"]["return"] = v, box
        elif key in ("EA", "EX"):
            res["exhaust"], res["boxes"]["exhaust"] = v, box
        elif key == "KEA":
            # kitchen exhaust: read as the exhaust figure unless an EA/EX figure is also printed
            # (then it is kept as printed in airflow_unparsed and the conflict is not resolved)
            if res["exhaust"] is None:
                res["exhaust"], res["boxes"]["exhaust"] = v, box
            else:
                res["other"].append(t)
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
            if 0 < n["y0"] - cur["y0"] <= 16 and abs(n["x0"] - cur["x0"]) <= 40 and len(n["text"].strip()) > 1 and not re.match(r"^(VCD|VAV|NRD|FD|M|A|SA|RA|EA|OA)\s*$", n["text"].strip()) \
                    and not re.search(r"L/s\s*$", cur["text"].strip(), re.I) and not re.match(r"^\s*(SA|RA|EA|OA|IEA)\b", n["text"]):
                parts.append(n)
                cur = n
        txt = _norm(" ".join(p["text"] for p in parts))
        box = [min(p["x0"] for p in parts), min(p["y0"] for p in parts), max(p["x1"] for p in parts), max(p["y1"] for p in parts)]
        notes.append({"text": txt, "box": box})
    # drop notes that are subsets of others
    uniq = []
    for n in notes:
        b = n["box"]
        if not any(n is not m and m["box"][0] - 3 <= b[0] and m["box"][1] - 3 <= b[1] and m["box"][2] + 3 >= b[2] and m["box"][3] + 3 >= b[3]
                   and n["text"] in m["text"] and len(m["text"]) > len(n["text"]) for m in notes):
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
    comp_by_id = {id(c): c for c in comps["SA"]}
    for a in ahus:
        cands = sorted(((_dist_comp_box(c, a["box"]), c) for c in comps["SA"]), key=lambda x: x[0])
        if not cands or cands[0][0] > 260:
            continue
        d0, c0 = cands[0]
        if len(c0["segs"]) < 12:
            # the outlet stub is a small piece: follow it to the nearest large SA component
            def _cc(ca, cb):
                pa = [(g["x0"], g["y0"]) for g in ca["segs"]] + [(g["x1"], g["y1"]) for g in ca["segs"]]
                pb = [(g["x0"], g["y0"]) for g in cb["segs"]] + [(g["x1"], g["y1"]) for g in cb["segs"]]
                return min(math.hypot(x1_ - x2_, y1_ - y2_) for x1_, y1_ in pa for x2_, y2_ in pb)
            bigs = sorted(((_cc(c0, c), c) for d, c in cands[1:] if len(c["segs"]) >= 12), key=lambda x: x[0])
            if bigs and bigs[0][0] <= 250:
                c0 = bigs[0][1]
        sa_by_comp.setdefault(id(c0), set()).add(a["tag"])
        # other comps equally close (shared header) join the same set
        for d, c in cands[1:]:
            if d <= d0 + 12 and len(c["segs"]) >= 12 and d <= 110:
                sa_by_comp.setdefault(id(c), set()).add(a["tag"])
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
    # sheets that carry no AHU of their own name the unit in a continuation note ('CONNECTED FROM AHU-...')
    if not ahus:
        cont = []
        for n in notes:
            for tg in re.findall(r"\bAHU-B-[A-Z0-9]{2}-[0-9A-Z]+-[0-9]{2}[A-Z]?(?:/[0-9A-Z]+)?", n["text"]):
                if tg not in cont:
                    cont.append(tg)
        if cont:
            for rec in room_records:
                if "SA" in rec["services"] and not rec["ahu_tags"]:
                    rec["ahu_tags"] = sorted(cont)
                    rec["assoc_method"] = "no AHU drawn on this sheet; AHU tags named in a continuation note (CONNECTED FROM/TO)"
                    rec["assoc_conf"] = "medium"
            res["warnings"].append("no AHU drawn on this sheet; AHU association taken from continuation note(s): %s" % "; ".join(cont))
    n_sa_vec = len(comps["SA"])
    all_ahu = sorted({a["tag"] for a in ahus})
    if n_sa_vec == 0 and all_ahu:
        for rec in room_records:
            if not rec["ahu_tags"]:
                rec["ahu_tags"] = all_ahu
                rec["assoc_method"] = "duct drawing carries no vector duct strokes (raster); every AHU on the sheet taken as one group"
                rec["assoc_conf"] = "low"
        res["warnings"].append("no vector SA duct strokes on the sheet: AHU association is sheet-level only")

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
    if not groups and n_sa_vec == 0 and all_ahu:
        merged = [set(all_ahu)]
    # AHUs with no SA component seen
    covered = set().union(*merged) if merged else set()
    for a in ahus:
        if a["tag"] not in covered:
            merged.append({a["tag"]})
            res["warnings"].append("AHU %s: no SA duct component found near the tag" % a["tag"])
    res["ahu_groups"] = [sorted(g) for g in merged]
    if len(merged) == 1:
        for rec in room_records:
            if not rec["ahu_tags"] and rec["services"] or (not rec["ahu_tags"] and rec["table"].cells):
                rec["ahu_tags"] = sorted(merged[0])
                rec["assoc_method"] = "only one AHU group on the sheet"
                rec["assoc_conf"] = "medium"
    res["notes_text"] = notes
    res["free_labels"] = free
    res["comps"] = {k: len(v) for k, v in comps.items()}
    res["sheet_obj"] = sh
    res["tables"] = tables
    res["levels"] = levels
    res["comps_obj"] = comps
    res["eq"] = eq
    return res


# --------------------------------------------------------------------------- #
# register rows
# --------------------------------------------------------------------------- #
ROOM_COLS = ["sheet", "issue", "drive_rel_path", "schematic_title", "ahu_tag", "room_name_as_written", "typical_count", "mode",
             "supply_L_s", "return_L_s", "exhaust_L_s", "pressure", "filter", "temperature", "humidity", "air_class", "oa_note",
             "serving_fan_tags", "isolation_exhaust_target", "services_drawn", "box_page_view_table", "box_page_view_outline",
             "method", "confidence", "storey", "issue_set",
             "page", "rev", "file_sha256", "airflow_as_printed", "airflow_unparsed", "box_page_view_name", "box_page_view_airflow_cell"]
EQUIP_COLS = ["sheet", "issue", "tag", "type", "duty_as_written", "plant_level", "served_level", "pair", "duty_standby", "accessories_nearby",
              "box_page_view_tag", "box_page_view_duty", "method", "confidence", "storey", "issue_set",
              "page", "rev", "file_sha256", "tag_plant_code", "tag_served_code", "tag_as_printed"]
CONN_COLS = ["sheet", "issue", "from_tag", "to_tag_or_room", "service", "size_flow_as_written", "method", "confidence", "box_page_view",
             "storey", "issue_set", "page", "rev", "file_sha256"]
ACC_LABELS = {"M", "A", "NRD", "FD", "BBT DAMPER", "ATTENUATOR", "W/P LOUVERS", "BiBo FILTER", "VCD", "VAV", "VV"}


def _rb(b):
    return json.dumps([round(v, 1) for v in b]) if b else ""


def _level_for_box(levels, box, x_pref=None):
    below = [l for l in levels if l["y0"] >= box[1] - 2]
    if not below:
        return None
    cx = (box[0] + box[2]) / 2
    return min(below, key=lambda l: (l["y0"] - box[1]) + 0.05 * abs(l["x"] - cx))["text"]


def build_rows(res: dict) -> dict:
    info = res["info"]
    base = {"sheet": info["sheet"], "issue": info["issue"], "storey": info.get("storey", ""), "issue_set": info["issue_set"],
            "page": 1, "rev": info.get("rev", ""), "file_sha256": res["sha256"]}
    rooms, equip, conns = [], [], []
    levels = res["levels"]
    eq = res["eq"]
    tag_info = {e["tag"]: e for e in eq}

    def ahu_flow(tag):
        e = tag_info.get(tag)
        return e["duty"] if e else None

    # ---- rooms -------------------------------------------------------
    recs = sorted(res["room_records"], key=lambda r: ((r["title_box"] or [0, 0])[1] if r["title_box"] else 0, round(r["table"].box[1] / 60), r["table"].left))
    for rec in recs:
        t = rec["table"]
        for ci, mname in enumerate(t.col_names or [""]):
            air = parse_airflow(t.cells.get(("airflow", ci), []))
            def cell(key):
                return _join(t.cells.get((key, ci), []))
            fan_txt = "; ".join(rec["fan_tags"])
            if rec["iea_target"]:
                fan_txt = "isolation exhaust line to %s (fan not on this sheet)" % rec["iea_target"]
            conf = "high"
            notes = []
            if not rec["ahu_tags"] and any(air[k] for k in ("supply",)):
                conf = "low"
                notes.append("AHU not resolved")
            elif rec["assoc_conf"] == "medium" or t.method_notes:
                conf = "medium"
            if not rec["name"]:
                conf = "low" if conf != "low" else conf
                notes.append("room name not found")
            if air["other"]:
                conf = "medium" if conf == "high" else conf
            if air["flags"]:
                conf = "medium" if conf == "high" else conf
                fl_u = [f for f in air["flags"] if not f.startswith("multiplier: ")]
                fl_m = [f[len("multiplier: "):] for f in air["flags"] if f.startswith("multiplier: ")]
                if fl_u:
                    notes.append("unit printed as '/s' without the L ('%s'); read as L/s, as-printed text kept" % "', '".join(fl_u))
                if fl_m:
                    notes.append("figure printed as 'N x M' ('%s'): the per-unit value N is taken, the multiplier is not applied (typical_count on the sheet is %s); as-printed text kept" % ("', '".join(fl_m), t.typical))
            method = "text layer cells of the room table (label-anchored grid, separators from the vector layer); AHU/fan by duct-colour component graph"
            if rec["assoc_method"]:
                method += "; " + rec["assoc_method"]
            if any(l.get("split_from_joined_line") for l in t.cells.get(("airflow", ci), [])):
                notes.append("two mode columns were joined in one text line; cut at the flow keys using the word boxes and assigned to the column by position")
            method += "".join("; " + n for n in t.method_notes + notes)
            row = dict(base)
            row.update({
                "drive_rel_path": info["rel_path"], "schematic_title": rec["title"], "ahu_tag": "; ".join(rec["ahu_tags"]),
                "room_name_as_written": rec["name"], "typical_count": t.typical, "mode": mname,
                "supply_L_s": _num(air["supply"]), "return_L_s": _num(air["return"]), "exhaust_L_s": _num(air["exhaust"]),
                "pressure": cell("pressure"), "filter": cell("filter"), "temperature": cell("temperature"), "humidity": cell("humidity"),
                "air_class": cell("class"), "oa_note": air["oa_text"] or "", "serving_fan_tags": fan_txt,
                "isolation_exhaust_target": rec["iea_target"], "services_drawn": "/".join(rec["services"]),
                "box_page_view_table": _rb(t.box), "box_page_view_outline": _rb(rec["outline"]), "method": method, "confidence": conf,
                "airflow_as_printed": " | ".join(air["as_printed"]), "airflow_unparsed": " | ".join(air["other"]),
                "box_page_view_name": _rb(rec["name_box"]),
                "box_page_view_airflow_cell": _rb([t.cols[ci][0], t.rows["airflow"][0], t.cols[ci][1], t.rows["airflow"][1]]),
            })
            rooms.append(row)
    res["room_rows"] = rooms

    # ---- equipment ---------------------------------------------------
    notes = res["notes_text"]
    # accessory labels
    acc = []
    for l in res["sheet_obj"].lines:
        tx = l["text"].strip()
        if tx in ACC_LABELS and tx not in ("VCD", "VAV", "VV") and l["x1"] < 3040:
            acc.append((tx, _lc(l)))
    acc_for = collections.defaultdict(collections.Counter)
    for tx, (cx, cy) in acc:
        best, bd = None, 1e9
        for e in eq:
            d = _dist_pt_box(cx, cy, e["box"])
            if d < bd:
                best, bd = e, d
        cap_ = 400 if tx in ("W/P LOUVERS", "FD", "ATTENUATOR", "BiBo FILTER") else 160
        if best is not None and bd <= cap_:
            acc_for[best["tag"]][tx] += 1
    # served level: level label under the first room table of the equipment's group
    group_of = {}
    for g in res["ahu_groups"]:
        for tg in g:
            group_of[tg] = g
    recs_by_ahu = collections.defaultdict(list)
    for rec in res["room_records"]:
        for tg in rec["ahu_tags"]:
            recs_by_ahu[tg].append(rec)
    for e in eq:
        stem = _stem(e["tag"])
        sibs = [x for x in eq if _stem(x["tag"]) == stem and x["type"] == e["type"]]
        pair = ""
        m = re.search(r"(?<=\d)([AB])$|/([AB])$", e["tag"])
        if len(sibs) > 1 or m:
            pair = e["tag"][:-1] + "A/B" if re.search(r"(?<=\d)[AB]$", e["tag"]) else re.sub(r"/[AB]$", "/A/B", e["tag"])
        flows = {x["duty_text"] for x in sibs}
        if e["qualifier"]:
            dsb = "as printed: %s" % e["qualifier"]
        elif len(sibs) > 1 and len(flows) == 1:
            dsb = "not written on this sheet (A/B pair, both labelled with the same airflow)"
        elif len(sibs) > 1:
            dsb = "not written on this sheet (A/B pair, labelled with different airflows)"
        else:
            dsb = "not written on this sheet (single unit)"
        served = ""
        rr = recs_by_ahu.get(e["tag"]) if e["type"] == "AHU" else None
        if not rr and e["type"] == "BIBO":
            bst = re.sub(r"^BIBO-", "", e["tag"])
            rr = [r for r in res["room_records"] if any(re.sub(r"^[A-Z]+-", "", f).startswith(bst) for f in r["fan_tags"])] or None
        if not rr:
            rr = [r for r in res["room_records"] if any(_stem(f) == stem for f in r["fan_tags"])] or None
        if rr:
            lv = collections.Counter(_level_for_box(levels, r["table"].box) for r in rr)
            served = (lv.most_common(1)[0][0] or "") if lv else ""
        row = dict(base)
        row.update({"tag": e["tag"], "type": e["type"], "duty_as_written": e["duty_text"] or "", "plant_level": e["level_band"] or "",
                    "served_level": served, "pair": pair, "duty_standby": dsb,
                    "accessories_nearby": json.dumps(dict(acc_for.get(e["tag"], {})), sort_keys=False),
                    "box_page_view_tag": _rb(e["box"]), "box_page_view_duty": _rb(e["duty_box"]),
                    "method": "text layer line(s)%s; duty: 'NNNN L/s' line directly under the tag; level: nearest storey label below the tag"
                              % (" joined (tag wraps over two lines)" if len(e["text_lines"]) > 1 else ""),
                    "confidence": "high" if e["duty_text"] or e["type"] in ("BIBO", "HPF") else "medium",
                    "tag_plant_code": e["tag_plant_code"] or "", "tag_served_code": e["tag_served_code"] or "",
                    "tag_as_printed": " / ".join(e["text_lines"])})
        equip.append(row)
    # PAHU / OA source notes
    oa_comps = res["comps_obj"]["OA"]
    ahus = [e for e in eq if e["type"] == "AHU"]
    oa_pairs = []
    for c in oa_comps:
        near_ahu = [a for a in ahus if _dist_comp_box(c, a["box"]) <= 110]
        if not near_ahu:
            continue
        pn = None
        pd = 1e9
        for n in notes:
            if re.search(r"PAHU|MAKE-UP|OUTDOOR AIR FROM|OA MAKE", n["text"], re.I):
                d = _dist_comp_box(c, n["box"])
                if d < pd:
                    pn, pd = n, d
        flows = []
        for fl in res["free_labels"]:
            if fl["kind"] == "OA":
                d = _dist_pt_box(*_lc({"x0": fl["box"][0], "x1": fl["box"][2], "y0": fl["box"][1], "y1": fl["box"][3]}), [c["bbox"][0], c["bbox"][1], c["bbox"][2], c["bbox"][3]])
                if d <= 60:
                    flows.append((d, fl))
        flows.sort(key=lambda x: x[0])
        oa_pairs.append((c, near_ahu, pn, pd, flows))
    res["oa_pairs"] = oa_pairs
    done_pahu = set()
    for c, near_ahu, pn, pd, flows in oa_pairs:
        if pn is None or pd > 400:
            continue
        tags = re.findall(r"PAHU-B-[A-Z0-9-]+(?:/[A-Z0-9]+)*", pn["text"])
        ptag = tags[0] if tags else ""
        for a in near_ahu:
            # OA label belonging to this AHU: nearest flow label to the AHU tag
            fl = None
            if flows:
                fl = min(flows, key=lambda x: _dist_pt_box(*_lc({"x0": x[1]["box"][0], "x1": x[1]["box"][2], "y0": x[1]["box"][1], "y1": x[1]["box"][3]}), a["box"]))[1]
            a.setdefault("oa_label", fl["text"] if fl else None)
            a.setdefault("oa_value", fl["value"] if fl else None)
            a["oa_source_note"] = pn["text"]
            row = dict(base)
            row.update({"from_tag": ptag or pn["text"], "to_tag_or_room": a["tag"], "service": "OA",
                        "size_flow_as_written": fl["text"] if fl else "", "method": "violet OA duct component touching the AHU; source note: '%s'" % pn["text"],
                        "confidence": "high" if (ptag and fl) else "medium", "box_page_view": _rb(pn["box"])})
            conns.append(row)
        dk = (ptag, tuple(sorted(a["tag"] for a in near_ahu)))
        if ptag and dk not in done_pahu:
            done_pahu.add(dk)
            lv = re.search(r"LEVEL \d+|BASEMENT \d", pn["text"], re.I)
            served_ = collections.Counter(x["served_level"] for x in equip if x["tag"] in {a["tag"] for a in near_ahu} and x["served_level"])
            served_txt = (served_.most_common(1)[0][0] + " (via AHUs)") if served_ else ""
            row = dict(base)
            row.update({"tag": ptag, "type": "PAHU (off-sheet source)",
                        "duty_as_written": "; ".join("%s: %s" % (a["tag"], a.get("oa_label") or "") for a in near_ahu),
                        "plant_level": lv.group(0).upper() if lv else "", "served_level": served_txt, "pair": "", "duty_standby": "",
                        "accessories_nearby": "", "box_page_view_tag": _rb(pn["box"]), "box_page_view_duty": "",
                        "method": "note text: '%s'" % pn["text"], "confidence": "high", "tag_plant_code": "", "tag_served_code": "",
                        "tag_as_printed": pn["text"]})
            equip.append(row)
    res["equip_rows"] = equip

    # ---- connections ---------------------------------------------------
    for rec in recs:
        t = rec["table"]
        air = parse_airflow(t.cells.get(("airflow", 0), []))
        nm = rec["name"] + (" (x%d)" % t.typical if t.typical > 1 else "")
        box = _rb(rec["outline"] or t.box)
        def C(frm, to, svc, flow, method, conf="high"):
            row = dict(base)
            row.update({"from_tag": frm, "to_tag_or_room": to, "service": svc, "size_flow_as_written": flow, "method": method,
                        "confidence": conf, "box_page_view": box})
            conns.append(row)
        supply = [l for l in t.cells.get(("airflow", 0), []) if re.match(r"^\s*SA\b", l["text"])]
        if supply and "SA" in rec["services"]:
            for a in rec["ahu_tags"]:
                C(a, nm, "SA", supply[0]["text"].strip(), "cyan SA riser crossing the room-box top, joined to the AHU outlet duct component")
        ret = [l for l in t.cells.get(("airflow", 0), []) if re.match(r"^\s*RA\b", l["text"])]
        exh = [l for l in t.cells.get(("airflow", 0), []) if re.match(r"^\s*(EA|EX)\b", l["text"])]
        if ret and "RA" in rec["services"]:
            for f in [x for x in rec["fan_tags"] if x.startswith("RAF")]:
                C(nm, f, "RA", ret[0]["text"].strip(), "green riser joined to the RA duct component that reaches the fan")
        if exh and "EA" in rec["services"]:
            for f in [x for x in rec["fan_tags"] if not x.startswith("RAF")]:
                C(nm, f, "EA", exh[0]["text"].strip(), "brown riser joined to the EA duct component that reaches the fan")
        if exh and "IEA" in rec["services"]:
            C(nm, rec.get("iea_note") or "isolation exhaust line (target note not found)", "EA (isolation)", exh[0]["text"].strip(),
              "yellow riser joined to the IEA duct component; the component carries the CONNECTED TO note" if rec.get("iea_note") else "yellow riser; no CONNECTED TO note found",
              "high" if rec.get("iea_note") else "low")
    # ---- fan-level links and louvre / spill / isolation-exhaust items -----------------
    sh_lines = res["sheet_obj"].lines
    titles_l = extract_titles(res["sheet_obj"])
    def _t_for(box):
        t_ = title_for(titles_l, box)
        return re.sub(r"^DEPARTMENT SCHEMATIC - L\w+ \w+ - |^DEPARTMENT SCHEMATIC - ", "", t_["text"]) if t_ else ""
    def _C(frm, to, svc, flow, method, conf, box=None):
        row = dict(base)
        row.update({"from_tag": frm, "to_tag_or_room": to, "service": svc, "size_flow_as_written": flow, "method": method, "confidence": conf,
                    "box_page_view": _rb(box) if box else ""})
        conns.append(row)
    rafs_ = [e for e in eq if e["type"] == "RAF"]
    bibos = [e for e in eq if e["type"] == "BIBO"]
    teafs = [e for e in eq if e["type"] == "TEAF"]
    for b in bibos:
        bst = re.sub(r"^BIBO-", "", b["tag"])
        for f_ in rafs_:
            if re.sub(r"^RAF-", "", f_["tag"]).startswith(bst):
                _C(b["tag"], f_["tag"], "RA", "", "tag stems agree: BIBO housing sits on the RA line ahead of the RAF pair", "medium", f_["box"])
    for f_ in rafs_:
        for a_ in ahus:
            if _stem(a_["tag"]) == _stem(f_["tag"]):
                _C(f_["tag"], a_["tag"], "RA", f_["duty_text"] or "", "tag stems agree: RAF returns to the AHU of the same name", "medium", f_["box"])
    wp = [l for l in sh_lines if re.match(r"^W/P LOUVERS", l["text"].strip()) and l["x1"] < 3040]
    for f_ in teafs:
        if wp:
            _C(f_["tag"], "W/P LOUVERS", "EA", f_["duty_text"] or "", "W/P LOUVERS label on the sheet; TEAF discharges to it (tag stem / position, not traced)", "medium", f_["box"])
    spill_labels = [x for x in res["free_labels"] if x["kind"] == "SPILL AIR"]
    louv = [l for l in sh_lines if l["text"].strip().startswith("WEATHERPROOF") and l["x1"] < 3040]
    for sl in spill_labels:
        if louv:
            _C("RA main (after RAF)", "WEATHERPROOF LOUVERS 2.5m X 2.5m", "RA (spill)", sl["text"], "SPILL AIR label on the pink spill line; weatherproof louvre note on the sheet", "medium", sl["box"])
            row = dict(base)
            row.update({"tag": "WEATHERPROOF LOUVERS 2.5m X 2.5m (%s)" % _t_for(sl["box"]), "type": "louvre (spill)", "duty_as_written": sl["text"],
                        "plant_level": _level_for_box(levels, sl["box"]) or "", "served_level": "", "pair": "", "duty_standby": "", "accessories_nearby": "",
                        "box_page_view_tag": _rb(sl["box"]), "box_page_view_duty": "", "method": "text layer SPILL AIR label", "confidence": "medium",
                        "tag_plant_code": "", "tag_served_code": "", "tag_as_printed": sl["text"]})
            equip.append(row)
    iea_labels = [x for x in res["free_labels"] if x["kind"] == "IEA"]
    for il in iea_labels:
        near = min(notes_iea, key=lambda n: _dist_pt_box(*_lc({"x0": il["box"][0], "x1": il["box"][2], "y0": il["box"][1], "y1": il["box"][3]}), n["box"]), default=None) if (notes_iea := [n for n in notes if re.search(r"CONNECTED TO", n["text"], re.I)]) else None
        tgt = near["text"] if near else ""
        _C("IEA header (yellow)", tgt, "EA (isolation)", il["text"], "IEA label on the yellow header; nearest CONNECTED TO note", "medium", il["box"])
        row = dict(base)
        row.update({"tag": "IEA line -> %s" % tgt, "type": "isolation exhaust (fan off-sheet)", "duty_as_written": il["text"], "plant_level": "",
                    "served_level": _level_for_box(levels, il["box"]) or "", "pair": "", "duty_standby": "", "accessories_nearby": "", "box_page_view_tag": _rb(near["box"]) if near else "",
                    "box_page_view_duty": _rb(il["box"]), "method": "text layer IEA label and CONNECTED TO note", "confidence": "medium",
                    "tag_plant_code": "", "tag_served_code": "", "tag_as_printed": tgt})
        equip.append(row)
    if wp:
        by_t = collections.defaultdict(list)
        for l in wp:
            by_t[_t_for([l["x0"], l["y0"], l["x1"], l["y1"]])].append(l)
        for tt, ls in by_t.items():
            duties = [t_["duty_text"] for t_ in teafs if t_["duty_text"] and _t_for(t_["box"]) == tt]
            row = dict(base)
            row.update({"tag": "W/P LOUVERS (%s)" % tt, "type": "louvre (toilet exhaust)", "duty_as_written": "; ".join(duties), "plant_level": _level_for_box(levels, [ls[0]["x0"], ls[0]["y0"], ls[0]["x1"], ls[0]["y1"]]) or "",
                        "served_level": "", "pair": "", "duty_standby": "", "accessories_nearby": "", "box_page_view_tag": _rb([ls[0]["x0"], ls[0]["y0"], ls[0]["x1"], ls[0]["y1"]]),
                        "box_page_view_duty": "", "method": "text layer W/P LOUVERS label; duty = TEAF duties of the same schematic", "confidence": "medium",
                        "tag_plant_code": "", "tag_served_code": "", "tag_as_printed": ls[0]["text"].strip()})
            equip.append(row)
    res["conn_rows"] = conns
    res["equip_rows"] = equip
    return res


# --------------------------------------------------------------------------- #
# AHU totals
# --------------------------------------------------------------------------- #
LEAK = 1.15
TOTAL_COLS = ["scope", "sheet", "issue", "issue_set", "storey", "ahu_tag", "ahu_group", "ahu_supply_as_written", "ahu_supply_L_s",
              "group_capacity_L_s", "capacity_basis", "rooms_in_group", "rooms_SA_sum_L_s_with_typicals", "rooms_SA_sum_L_s_single",
              "rooms_SA_x_1_15", "ratio_ahu_to_rooms", "diff_L_s", "oa_label_as_written", "oa_label_L_s", "rooms_OA_sum_L_s",
              "rooms_OA_x_1_15", "oa_diff_L_s", "raf_L_s", "spill_L_s", "rooms_without_supply_figure", "check_result", "check_note"]


def _oa_value(txt: str) -> Optional[float]:
    m = re.match(r"^\s*OA\s*([\d,]+(?:\.\d+)?)\s*(?:L\s*/\s*S)?\s*$", txt or "", re.I)
    return float(m.group(1).replace(",", "")) if m else None


def _capacity(tags: List[str], flow: Dict[str, Optional[float]], standby: Optional[Dict[str, bool]] = None):
    """Group capacity: A/B units with equal printed flow count once (duty or standby
    is not written), units with different flows add up."""
    standby = standby or {}
    by = collections.defaultdict(list)
    for tg in tags:
        if re.search(r"/\d+[A-Z]$", tg) or standby.get(tg):
            continue  # common standby unit (e.g. -05/06C) is not added to the duty capacity
        by[_stem(tg)].append(flow.get(tg))
    cap, basis = 0.0, []
    missing = False
    for st, vals in by.items():
        vs = [v for v in vals if v is not None]
        if not vs or len(vs) < len(vals):
            missing = True
        if not vs:
            continue
        if len(set(vs)) == 1:
            cap += vs[0]
            basis.append("%s: %d unit(s) at %s counted once" % (st, len(vs), _num(vs[0])) if len(vs) > 1 else "%s: single %s" % (st, _num(vs[0])))
        else:
            cap += sum(vs)
            basis.append("%s: %s added (different flows)" % (st, "+".join(str(_num(v)) for v in vs)))
    return (None if (missing and cap == 0) else cap), "; ".join(basis), missing


def ahu_totals_for(res: dict) -> List[dict]:
    info = res["info"]
    eq = res["eq"]
    flow = {e["tag"]: e["duty"] for e in eq if e["type"] == "AHU"}
    txt = {e["tag"]: e["duty_text"] for e in eq if e["type"] == "AHU"}
    rafs = {}
    for e in eq:
        if e["type"] == "RAF":
            rafs.setdefault(_stem(e["tag"]), e["duty"])
    out = []
    for g in res["ahu_groups"]:
        gs = set(g)
        recs = [r for r in res["room_records"] if set(r["ahu_tags"]) and set(r["ahu_tags"]) <= gs]
        sa_t = sa_1 = oa_t = 0.0
        nsa = nonum = 0
        for r in recs:
            t = r["table"]
            ci = 0
            for k, nme in enumerate(t.col_names or [""]):
                if _mode_is_norm(nme):
                    ci = k
                    break
            else:
                continue
            air = parse_airflow(t.cells.get(("airflow", ci), []))
            if air["supply"] is not None:
                sa_t += air["supply"] * t.typical
                sa_1 += air["supply"]
                nsa += 1
                ov = _oa_value(air["oa_text"] or "")
                if ov is not None:
                    oa_t += ov * t.typical
            elif "SA" in r["services"]:
                nonum += 1
        sb = {e['tag']: bool(re.search(r'STANDBY', e['qualifier'] or '', re.I)) for e in eq if e['type'] == 'AHU'}
        cap, basis, missing = _capacity(g, flow, sb)
        oa_labels = [e.get("oa_label") for e in eq if e["tag"] in gs and e.get("oa_label")]
        oa_vals = [e.get("oa_value") for e in eq if e["tag"] in gs and e.get("oa_value") is not None]
        oa_cap = None
        if oa_vals:
            by = collections.defaultdict(list)
            for e in eq:
                if e["tag"] in gs and e.get("oa_value") is not None:
                    by[_stem(e["tag"])].append(e["oa_value"])
            oa_cap = sum(v[0] if len(set(v)) == 1 else sum(v) for v in by.values())
        raf_cap = None
        stems = {_stem(t) for t in g}
        rv = [rafs[s] for s in stems if s in rafs and rafs[s] is not None]
        raf_cap = sum(rv) if rv else None
        spill = None
        for n in res["notes_text"]:
            pass
        expected = LEAK * sa_t
        if not recs or nsa == 0:
            result, note = "no_rooms", "no room table with a supply figure was associated with this AHU group on this sheet"
        elif cap is None:
            result, note = "no_ahu_flow", "no 'NNNN L/s' printed under the AHU tag"
        else:
            diff = cap - expected
            tol = max(5.0, 0.015 * cap)
            if abs(diff) <= tol:
                result, note = "ok", "AHU supply = rooms x 1.15 within %.1f L/s" % tol
            elif abs(diff) <= 0.05 * cap:
                result, note = "near", "within 5 %% but outside the %.1f L/s tolerance" % tol
            elif sa_t * LEAK < cap:
                result, note = "ahu_exceeds_rooms", "AHU supply exceeds rooms x 1.15 (rooms may continue on another sheet, or tables missing)"
            else:
                result, note = "rooms_exceed_ahu", "rooms x 1.15 exceed the AHU supply on the sheet (typical counts, shared AHU or mis-association)"
        if missing and cap:
            note += "; one or more units without a printed flow"
        if nonum:
            note += "; %d room(s) with an SA riser but no supply figure" % nonum
        for tg in g:
            out.append({"scope": "sheet", "sheet": info["sheet"], "issue": info["issue"], "issue_set": info["issue_set"],
                        "storey": info.get("storey", ""), "ahu_tag": tg, "ahu_group": "; ".join(g), "ahu_supply_as_written": txt.get(tg) or "",
                        "ahu_supply_L_s": _num(flow.get(tg)), "group_capacity_L_s": _num(cap), "capacity_basis": basis,
                        "rooms_in_group": len(recs), "rooms_SA_sum_L_s_with_typicals": _num(sa_t), "rooms_SA_sum_L_s_single": _num(sa_1),
                        "rooms_SA_x_1_15": round(expected, 1), "ratio_ahu_to_rooms": round(cap / sa_t, 4) if cap and sa_t else "",
                        "diff_L_s": round(cap - expected, 1) if cap is not None else "",
                        "oa_label_as_written": "; ".join(sorted(set(oa_labels))), "oa_label_L_s": _num(oa_cap),
                        "rooms_OA_sum_L_s": _num(oa_t), "rooms_OA_x_1_15": round(oa_t * LEAK, 1) if oa_t else "",
                        "oa_diff_L_s": round(oa_cap - oa_t * LEAK, 1) if (oa_cap is not None and oa_t) else "",
                        "raf_L_s": _num(raf_cap), "spill_L_s": "", "rooms_without_supply_figure": nonum, "check_result": result, "check_note": note})
    # rooms with SA but no AHU
    orphans = [r for r in res["room_records"] if not r["ahu_tags"] and "SA" in r["services"]]
    if orphans:
        res["warnings"].append("%d room(s) with an SA riser but no AHU association: %s" % (len(orphans), "; ".join(r["name"] for r in orphans[:6])))
    return out


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #
STOREY_PATTERNS = [
    (re.compile(r"LOWER BASEMENT 3", re.I), "LB3"),
    (re.compile(r"BASEMENT 3", re.I), "B3"),
    (re.compile(r"BASEMENT 2", re.I), "B2"),
    (re.compile(r"BASEMENT 1", re.I), "B1"),
    (re.compile(r"\b0?([1-6]) ?(?:ST|ND|RD|TH) STOREY", re.I), None),
]
IN_SCOPE_STOREYS = {"LB3", "B3", "B2", "L01", "L02", "L03", "L04", "L05", "L06"}


def storey_of(name: str) -> str:
    for rx, code in STOREY_PATTERNS:
        m = rx.search(name)
        if m:
            return code if code else "L%02d" % int(m.group(1))
    return ""


def _sheet_id(name: str) -> str:
    m = re.search(r"IP-BH-[0-9A-Z]{5}", name)
    return m.group(0) if m else ""


def discover(sheets: Optional[List[str]] = None) -> List[dict]:
    """Source PDFs in scope: IP-BH-05/06/07xxx air-side sheets for LB3..L06 (tender and working issue)
    and the MOT NSC IP-BH-160xx sheets (latest corrigendum per sheet)."""
    out: List[dict] = []
    tender_dir = BASIS / "01 ACMV Tender Schematic"
    work_dir = BASIS / "Working Drawings"
    for issue_set, d in (("tender", tender_dir), ("working", work_dir)):
        for f in sorted(d.glob("IP-BH-0[567]*.pdf")):
            name = f.name
            if "AIR-SIDE SCHEMATIC" not in name.upper() and "AIR SIDE SCHEMATIC" not in name.upper():
                continue
            st = storey_of(name)
            if st not in IN_SCOPE_STOREYS:
                continue
            sid = _sheet_id(name)
            rev = ""
            m = re.search(r"_Rev(\w+?)_", name)
            if m:
                rev = m.group(1)
            issue = ("tender issue Rev %s" % rev) if issue_set == "tender" and rev else ("tender issue" if issue_set == "tender" else "working drawing")
            out.append({"sheet": sid, "issue": issue, "issue_set": issue_set, "storey": st, "path": str(f),
                        "rel_path": str(f.relative_to(ROOT / "data" / "source")), "rev": rev, "file": name})
    # MOT NSC: latest corrigendum issue per sheet
    mot = BASIS / "MOT NSC"
    order = [("Tender Corr 3", 3), ("Tender Corr 2", 2)]
    seen: Dict[str, dict] = {}
    for folder, rank in order:
        for f in sorted((mot / folder).rglob("*.pdf")):
            sid = _sheet_id(f.name)
            if not re.match(r"IP-BH-160\d\d", sid) or sid in seen:
                continue
            if "AIR-SIDE SCHEMATIC" not in f.name.upper():
                continue
            st = storey_of(f.name)
            m = re.search(r"\((\w+)\)\.pdf$", f.name)
            rev = m.group(1) if m else ""
            seen[sid] = {"sheet": sid, "issue": "%s (Rev %s)" % (folder, rev) if rev else folder, "issue_set": "mot_nsc", "storey": st,
                         "path": str(f), "rel_path": str(f.relative_to(ROOT / "data" / "source")), "rev": rev, "file": f.name}
    out.extend(seen[k] for k in sorted(seen))
    if sheets:
        want = {s.upper() for s in sheets}
        out = [o for o in out if o["sheet"].upper() in want]
    return out


def _worker(task: dict) -> dict:
    import warnings
    warnings.filterwarnings("ignore")
    try:
        res = parse_sheet(task["path"], task)
        build_rows(res)
        totals = ahu_totals_for(res)
        tb = res["title_block"]
        return {"task": task, "ok": True, "room_rows": res["room_rows"], "equip_rows": res["equip_rows"], "conn_rows": res["conn_rows"],
                "totals": totals, "warnings": res["warnings"], "title_block": tb, "n_tables": res["n_tables"], "titles": res["titles"],
                "ahu_groups": res["ahu_groups"], "sha256": res["sha256"], "n_eq": len(res["eq"]), "pages": res["pages"]}
    except Exception as exc:  # pragma: no cover - reported, not hidden
        import traceback
        return {"task": task, "ok": False, "error": "%s: %s" % (type(exc).__name__, exc), "trace": traceback.format_exc()[-1500:]}


def aggregate_families(results: List[dict]) -> List[dict]:
    """Cross-sheet AHU check: AHU tags that share a room form one family; the rooms of the
    family on every sheet of one issue are summed against the AHU flow printed on any sheet."""
    out: List[dict] = []
    bykey = collections.defaultdict(list)
    for r in results:
        if r.get("ok"):
            bykey[(r["task"]["issue_set"], r["task"]["issue"] if r["task"]["issue_set"] == "mot_nsc" else r["task"]["issue_set"])].append(r)
    for (iset, _), rs in bykey.items():
        parent: Dict[str, str] = {}

        def find(a):
            parent.setdefault(a, a)
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a
        rooms = []
        flows: Dict[str, set] = collections.defaultdict(set)
        txt: Dict[str, set] = collections.defaultdict(set)
        standby: Dict[str, bool] = {}
        for r in rs:
            for e in r["equip_rows"]:
                if e["type"] == "AHU":
                    m = DUTY_RE.match((e["duty_as_written"] or "").split(" | ")[0]) if e["duty_as_written"] else None
                    # primary (normal) figure: the one used on the sheet
                    flows[e["tag"]].add(_primary_flow(e["duty_as_written"]))
                    txt[e["tag"]].add(e["duty_as_written"])
                    standby[e["tag"]] = standby.get(e["tag"], False) or "STANDBY" in (e.get("duty_standby") or "").upper()
            for row in r["room_rows"]:
                tags = [t for t in (row["ahu_tag"] or "").split("; ") if t]
                for t in tags:
                    find(t)
                for t in tags[1:]:
                    parent[find(t)] = find(tags[0])
                if tags and (row["mode"] == "" or _mode_is_norm(row["mode"])):
                    rooms.append((tags[0], row, r["task"]["sheet"]))
        fams = collections.defaultdict(list)
        for t in list(parent):
            fams[find(t)].append(t)
        for root, tags in fams.items():
            tags.sort()
            sa_t = oa_t = 0.0
            nrooms = 0
            nblank = 0
            sheets = set()
            for t0, row, sh_ in rooms:
                if find(t0) != root:
                    continue
                try:
                    sv = float(row["supply_L_s"]) if row["supply_L_s"] != "" else None
                except ValueError:
                    sv = None
                if sv is None:
                    # a room drawn with an SA riser (or printing an SA figure) but no supply value in the register
                    if "SA" in (row.get("services_drawn") or "").split("/") or re.search(r"\bSA\b", row.get("airflow_as_printed") or ""):
                        nblank += 1
                    continue
                typ = int(row["typical_count"] or 1)
                sa_t += sv * typ
                nrooms += 1
                sheets.add(sh_)
                ov = _oa_value(row["oa_note"])
                if ov is not None:
                    oa_t += ov * typ
            fl = {t: (sorted(v for v in flows[t] if v is not None)[0] if any(v is not None for v in flows[t]) else None) for t in tags}
            conflict = [t for t in tags if len({v for v in flows[t] if v is not None}) > 1]
            cap, basis, missing = _capacity(tags, fl, standby)
            expected = LEAK * sa_t
            if nrooms == 0:
                res_, note = "no_rooms", "no room with a supply figure on any parsed sheet"
            elif cap is None:
                res_, note = "no_ahu_flow", "no AHU flow printed on any parsed sheet"
            else:
                diff = cap - expected
                tol = max(5.0, 0.015 * cap)
                if abs(diff) <= tol:
                    res_, note = "ok", "AHU supply = rooms x 1.15 within %.1f L/s" % tol
                elif abs(diff) <= 0.05 * cap:
                    res_, note = "near", "within 5 %% but outside the %.1f L/s tolerance" % tol
                elif cap > expected:
                    res_, note = "ahu_exceeds_rooms", "AHU supply exceeds rooms x 1.15 (rooms not tabulated on the parsed sheets, or typical counts)"
                else:
                    res_, note = "rooms_exceed_ahu", "rooms x 1.15 exceed AHU supply"
            if nblank:
                note += "; %d room(s) with an SA riser or printed SA figure but no supply value in the register: the room sum is incomplete" % nblank
            if conflict:
                note += "; AHU flow differs between sheets for " + ", ".join(conflict)
            out.append({"scope": "cross_sheet_family", "sheet": "; ".join(sorted(sheets)), "issue": rs[0]["task"]["issue"] if iset == "mot_nsc" else ("tender" if iset == "tender" else "working"),
                        "issue_set": iset, "storey": "", "ahu_tag": "; ".join(tags), "ahu_group": "; ".join(tags),
                        "ahu_supply_as_written": " / ".join(sorted({x for t in tags for x in txt[t] if x})), "ahu_supply_L_s": "",
                        "group_capacity_L_s": _num(cap), "capacity_basis": basis, "rooms_in_group": nrooms,
                        "rooms_SA_sum_L_s_with_typicals": _num(sa_t), "rooms_SA_sum_L_s_single": "", "rooms_SA_x_1_15": round(expected, 1),
                        "ratio_ahu_to_rooms": round(cap / sa_t, 4) if cap and sa_t else "", "diff_L_s": round(cap - expected, 1) if cap is not None else "",
                        "oa_label_as_written": "", "oa_label_L_s": "", "rooms_OA_sum_L_s": _num(oa_t), "rooms_OA_x_1_15": round(oa_t * LEAK, 1) if oa_t else "",
                        "oa_diff_L_s": "", "raf_L_s": "", "spill_L_s": "", "rooms_without_supply_figure": nblank, "check_result": res_, "check_note": note})
    return out


def _primary_flow(duty_text: Optional[str]) -> Optional[float]:
    """Normal-mode figure from a duty string such as '4508 L/s (NORMAL) | 4508 L/s (PANDEMIC)'."""
    if not duty_text:
        return None
    parts = [p.strip() for p in duty_text.split("|")]
    best = None
    for p in parts:
        m = DUTY_RE.match(p)
        if not m:
            continue
        v = float(m.group(1).replace(",", ""))
        if re.search(r"NORMAL|NORM\b", m.group(2), re.I) or not m.group(2).strip():
            return v
        if best is None:
            best = v
    return best


# --------------------------------------------------------------------------- #
# writers
# --------------------------------------------------------------------------- #
def _sort_key(row):
    return ({"tender": 0, "working": 1, "mot_nsc": 2}.get(row["issue_set"], 3), row["storey"], row["sheet"], row.get("issue", ""))


def _write_csv(path: Path, cols: List[str], rows: List[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
    os.replace(tmp, path)


def _norm_name(n: str) -> str:
    return re.sub(r"\s+", " ", str(n) if n is not None else "").strip().upper()


def tender_vs_working(room_rows: List[dict]) -> List[dict]:
    """Per room, airflow differences between the tender and the working issue."""
    grp: Dict[tuple, Dict[str, dict]] = {}
    seen = collections.Counter()
    for r in room_rows:
        if r["issue_set"] not in ("tender", "working"):
            continue
        base = (r["sheet"], _norm_name(r["room_name_as_written"]), str(r["typical_count"]), r["mode"])
        k = (r["issue_set"],) + base
        seen[k] += 1
        grp.setdefault(base + (seen[k],), {})[r["issue_set"]] = r
    out = []
    for key in sorted(grp, key=lambda k: (k[0], k[1], k[2], k[3], k[4])):
        d = grp[key]
        t, w = d.get("tender"), d.get("working")
        ref = w or t
        row = {"sheet": key[0], "storey": ref["storey"], "room_name_as_written": ref["room_name_as_written"], "typical_count": key[2], "mode": key[3],
               "occurrence_on_sheet": key[4]}
        for lab, src in (("tender", t), ("working", w)):
            for f in ("supply_L_s", "return_L_s", "exhaust_L_s", "oa_note"):
                row["%s_%s" % (lab, f)] = src[f] if src else ""
            row["%s_ahu_tag" % lab] = src["ahu_tag"] if src else ""
        diffs = []
        for f, lab in (("supply_L_s", "SA"), ("return_L_s", "RA"), ("exhaust_L_s", "EA")):
            a = t[f] if t else ""
            b = w[f] if w else ""
            try:
                dv = (float(b) if b != "" else 0.0) - (float(a) if a != "" else 0.0)
            except ValueError:
                dv = None
            row["delta_%s" % f] = (_num(dv) if dv is not None else "") if (t and w) else ""
            if t and w and a != b:
                diffs.append("%s %s -> %s" % (lab, a or "blank", b or "blank"))
        oa_t = _oa_value(t["oa_note"]) if t else None
        oa_w = _oa_value(w["oa_note"]) if w else None
        row["delta_oa_L_s"] = _num(oa_w - oa_t) if (t and w and oa_t is not None and oa_w is not None) else ""
        if t and w and (t["oa_note"] or "") != (w["oa_note"] or ""):
            diffs.append("OA '%s' -> '%s'" % (t["oa_note"], w["oa_note"]))
        other = []
        if t and w:
            for f in ("pressure", "air_class", "filter", "temperature", "humidity", "ahu_tag"):
                if (t[f] or "") != (w[f] or ""):
                    other.append("%s '%s' -> '%s'" % (f, t[f], w[f]))
        row["airflow_differences"] = "; ".join(diffs)
        row["other_differences"] = "; ".join(other)
        if t and not w:
            row["status"] = "only_in_tender"
        elif w and not t:
            row["status"] = "only_in_working"
        elif diffs:
            row["status"] = "airflow_changed"
        elif other:
            row["status"] = "other_changed"
        else:
            row["status"] = "identical"
        out.append(row)
    return out


TVW_COLS = ["sheet", "storey", "room_name_as_written", "typical_count", "mode", "occurrence_on_sheet", "status",
            "tender_supply_L_s", "working_supply_L_s", "delta_supply_L_s", "tender_return_L_s", "working_return_L_s", "delta_return_L_s",
            "tender_exhaust_L_s", "working_exhaust_L_s", "delta_exhaust_L_s", "tender_oa_note", "working_oa_note", "delta_oa_L_s",
            "airflow_differences", "other_differences", "tender_ahu_tag", "working_ahu_tag"]


# --------------------------------------------------------------------------- #
# pilot reproduction
# --------------------------------------------------------------------------- #
PILOT_SHEET = "IP-BH-05010"
PILOT_FIELDS_ROOM = ["ahu_tag", "supply_L_s", "return_L_s", "exhaust_L_s", "pressure", "filter", "temperature", "humidity", "air_class", "oa_note",
                     "serving_fan_tags", "isolation_exhaust_target", "services_drawn", "schematic_title"]
PILOT_FIELDS_EQUIP = ["type", "duty_as_written", "plant_level", "served_level", "pair", "duty_standby", "accessories_nearby"]


def snapshot_pilot() -> Dict[str, List[dict]]:
    """Return the hand-extracted pilot rows; the first call keeps a copy under out/qa/evidence."""
    ev = QA / "evidence"
    ev.mkdir(parents=True, exist_ok=True)
    res = {}
    for nm in ("rooms", "equipment", "connections"):
        live = REG / ("schematic_%s.csv" % nm)
        snap = ev / ("schematic_pilot_%s.csv" % nm)
        if not snap.exists() and live.exists():
            with open(live, newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            if rows and "storey" not in rows[0]:
                snap.write_bytes(live.read_bytes())
        res[nm] = []
        if snap.exists():
            with open(snap, newline="", encoding="utf-8") as fh:
                res[nm] = list(csv.DictReader(fh))
    return res


def compare_pilot(pilot: Dict[str, List[dict]], rooms: List[dict], equip: List[dict], conns: List[dict]) -> dict:
    out = {"rooms": [], "equipment": [], "connections": [], "counts": {}}
    new = {"rooms": [r for r in rooms if r["sheet"] == PILOT_SHEET and r["issue_set"] in ("tender", "working")],
           "equipment": [r for r in equip if r["sheet"] == PILOT_SHEET and r["issue_set"] in ("tender", "working")],
           "connections": [r for r in conns if r["sheet"] == PILOT_SHEET and r["issue_set"] in ("tender", "working")]}
    # ---- rooms: key = (issue, name, typical, mode, occurrence)
    def keyed(rows, keyf):
        c = collections.Counter()
        d = {}
        for r in rows:
            k = keyf(r)
            c[k] += 1
            d[k + (c[k],)] = r
        return d
    kr = lambda r: (r["issue"], _norm_name(r["room_name_as_written"]), str(r["typical_count"]), r["mode"])
    po, pn = keyed(pilot["rooms"], kr), keyed(new["rooms"], kr)
    ident = 0
    for k in sorted(set(po) | set(pn)):
        a, b = po.get(k), pn.get(k)
        if a is None:
            out["rooms"].append({"issue": k[0], "key": " / ".join(map(str, k[1:])), "field": "(row)", "pilot": "", "parsed": "row only in parser"})
            continue
        if b is None:
            out["rooms"].append({"issue": k[0], "key": " / ".join(map(str, k[1:])), "field": "(row)", "pilot": "row only in pilot", "parsed": ""})
            continue
        bad = False
        for f in PILOT_FIELDS_ROOM:
            x, y = (a.get(f) or ""), (b.get(f) or "")
            if f == "services_drawn":
                x, y = "/".join(sorted(x.split("/"))), "/".join(sorted(y.split("/")))
            if f in ("serving_fan_tags", "ahu_tag"):
                x, y = "; ".join(sorted(x.split("; "))), "; ".join(sorted(y.split("; ")))
            if _norm_name(x) != _norm_name(y):
                bad = True
                out["rooms"].append({"issue": k[0], "key": " / ".join(map(str, k[1:])), "field": f, "pilot": a.get(f), "parsed": b.get(f)})
        ident += 0 if bad else 1
    out["counts"]["rooms"] = {"pilot": len(pilot["rooms"]), "parsed": len(new["rooms"]), "identical_rows": ident}
    # ---- equipment by (issue, tag)
    ke = lambda r: (r["issue"], r["tag"], r["duty_as_written"] if (r["type"].startswith("PAHU") or r["type"].startswith("isolation")) else "")
    po, pn = keyed(pilot["equipment"], ke), keyed(new["equipment"], ke)
    ident = 0
    for k in sorted(set(po) | set(pn)):
        a, b = po.get(k), pn.get(k)
        if a is None or b is None:
            out["equipment"].append({"issue": k[0], "key": k[1] + (" [" + k[2] + "]" if k[2] else ""), "field": "(row)", "pilot": "" if a is None else "row in pilot", "parsed": "" if b is None else "row in parser"})
            continue
        bad = False
        for f in PILOT_FIELDS_EQUIP:
            x, y = (a.get(f) or ""), (b.get(f) or "")
            if x != y:
                bad = True
                out["equipment"].append({"issue": k[0], "key": k[1], "field": f, "pilot": x, "parsed": y})
        ident += 0 if bad else 1
    out["counts"]["equipment"] = {"pilot": len(pilot["equipment"]), "parsed": len(new["equipment"]), "identical_rows": ident}
    # ---- connections by (issue, from, to, service, flow)
    kc = lambda r: (r["issue"], r["from_tag"], re.sub(r"\s+", " ", r["to_tag_or_room"]), r["service"], r["size_flow_as_written"])
    po, pn = collections.Counter(kc(r) for r in pilot["connections"]), collections.Counter(kc(r) for r in new["connections"])
    both = sum((po & pn).values())
    for k in sorted(set(po) | set(pn)):
        if po[k] != pn[k]:
            out["connections"].append({"issue": k[0], "key": "%s -> %s [%s]" % (k[1], k[2], k[3]), "field": "count (flow %s)" % k[4], "pilot": po[k], "parsed": pn[k]})
    out["counts"]["connections"] = {"pilot": len(pilot["connections"]), "parsed": len(new["connections"]), "identical_rows": both}
    out["full_row"] = full_row_compare(pilot, rooms, equip, conns)
    return out


PROV_COLS = {"method", "confidence", "box_page_view_table", "box_page_view_outline", "box_page_view_tag", "box_page_view_duty", "box_page_view"}


def _box_delta(a: str, b: str) -> Optional[float]:
    try:
        x, y = json.loads(a), json.loads(b)
        return max(abs(p - q) for p, q in zip(x, y)) if x and y and len(x) == len(y) else None
    except (ValueError, TypeError):
        return None


def full_row_compare(pilot: Dict[str, List[dict]], rooms: List[dict], equip: List[dict], conns: List[dict]) -> dict:
    """Exact comparison of EVERY pilot column (provenance columns included) between the hand-made
    pilot rows and the parser rows of the same sheet. Rows are paired on the keys used above."""
    new = {"rooms": [r for r in rooms if r["sheet"] == PILOT_SHEET and r["issue_set"] in ("tender", "working")],
           "equipment": [r for r in equip if r["sheet"] == PILOT_SHEET and r["issue_set"] in ("tender", "working")],
           "connections": [r for r in conns if r["sheet"] == PILOT_SHEET and r["issue_set"] in ("tender", "working")]}
    keys = {"rooms": lambda r: (r["issue"], _norm_name(r["room_name_as_written"]), str(r["typical_count"]), r["mode"]),
            "equipment": lambda r: (r["issue"], r["tag"], r["duty_as_written"] if (r["type"].startswith("PAHU") or r["type"].startswith("isolation")) else ""),
            "connections": lambda r: (r["issue"], r["from_tag"], re.sub(r"\s+", " ", r["to_tag_or_room"]), r["service"], r["size_flow_as_written"])}
    out = {}
    for reg in ("rooms", "equipment", "connections"):
        def keyed(rows):
            c, d = collections.Counter(), {}
            for r in rows:
                k = keys[reg](r)
                c[k] += 1
                d[k + (c[k],)] = r
            return d
        po, pn = keyed(pilot.get(reg, [])), keyed(new[reg])
        cols = list(pilot[reg][0].keys()) if pilot.get(reg) else []
        res = {"pilot_rows": len(pilot.get(reg, [])), "parser_rows": len(new[reg]), "paired": 0, "identical_all_columns": 0,
               "identical_excluding_provenance": 0, "col_differs": collections.Counter(), "conf_moves": collections.Counter(),
               "box_max_delta_pt": collections.defaultdict(float), "services_order_only": 0, "cols_compared": cols}
        for k in sorted(set(po) & set(pn)):
            a, b = po[k], pn[k]
            res["paired"] += 1
            diff, diff_np = [], []
            for c in cols:
                x, y = str(a.get(c) if a.get(c) is not None else ""), str(b.get(c) if b.get(c) is not None else "")
                if c == "room_name_as_written" and reg == "rooms":
                    same = _norm_name(x) == _norm_name(y)
                else:
                    same = x == y
                if not same:
                    if c == "services_drawn" and sorted(x.split("/")) == sorted(y.split("/")):
                        res["services_order_only"] += 1
                    res["col_differs"][c] += 1
                    diff.append(c)
                    if c == "confidence":
                        res["conf_moves"][(x, y)] += 1
                    if c.startswith("box_page_view"):
                        d = _box_delta(x, y)
                        if d is not None:
                            res["box_max_delta_pt"][c] = max(res["box_max_delta_pt"][c], d)
                    if c not in PROV_COLS and not (c == "services_drawn" and sorted(x.split("/")) == sorted(y.split("/"))):
                        diff_np.append(c)
            res["identical_all_columns"] += 0 if diff else 1
            res["identical_excluding_provenance"] += 0 if diff_np else 1
        res["unpaired_pilot"] = len(set(po) - set(pn))
        res["unpaired_parser"] = len(set(pn) - set(po))
        out[reg] = res
    return out


def verify_pilot_baseline(pilot: Dict[str, List[dict]], tasks: List[dict]) -> dict:
    """Independent check of the pilot snapshot against the PDF text layer (no parser code involved):
    every number printed in the pilot room rows (supply, return, exhaust, pressure, filter,
    temperature) must be a word inside the pilot's own table box on the sheet."""
    paths = {t["issue_set"]: t["path"] for t in tasks if t["sheet"] == PILOT_SHEET}
    res = {"rows_checked": 0, "rows_confirmed": 0, "values_checked": 0, "values_confirmed": 0, "not_confirmed": []}
    sheets = {}
    for r in pilot.get("rooms", []):
        iset = "tender" if str(r["issue"]).startswith("tender") else "working"
        if iset not in paths:
            continue
        if iset not in sheets:
            sheets[iset] = Sheet(paths[iset])
        sh = sheets[iset]
        try:
            b = json.loads(r["box_page_view_table"])
        except (ValueError, TypeError):
            continue
        words = [w["text"] for w in sh.words if b[0] - 3 <= (w["x0"] + w["x1"]) / 2 <= b[2] + 3 and b[1] - 3 <= (w["y0"] + w["y1"]) / 2 <= b[3] + 3]
        blob = " ".join(words)
        res["rows_checked"] += 1
        bad = []
        for f in ("supply_L_s", "return_L_s", "exhaust_L_s"):
            v = (r.get(f) or "").strip()
            if v:
                res["values_checked"] += 1
                if re.search(r"(?<![\d.])%s(?![\d.])" % re.escape(v), blob):
                    res["values_confirmed"] += 1
                else:
                    bad.append("%s=%s" % (f, v))
        for f in ("pressure", "filter", "temperature"):
            v = _norm(r.get(f) or "")
            if v:
                res["values_checked"] += 1
                dash = lambda x: re.sub("[\u2010-\u2015\u2212]", "-", x).upper().replace(" ", "")
                if dash(v) in dash(blob):
                    res["values_confirmed"] += 1
                else:
                    bad.append("%s=%s" % (f, r.get(f)))
        if not bad:
            res["rows_confirmed"] += 1
        else:
            res["not_confirmed"].append("%s / %s / %s: %s" % (r["issue"], r["room_name_as_written"], r["mode"], ", ".join(bad)))
    return res


# --------------------------------------------------------------------------- #
# equipment differences between issues
# --------------------------------------------------------------------------- #
EQD_COLS = ["sheet", "storey", "tag", "type", "tender_duty_as_written", "working_duty_as_written", "status"]


def equipment_tender_vs_working(equip_rows: List[dict]) -> List[dict]:
    d: Dict[tuple, Dict[str, dict]] = {}
    for r in equip_rows:
        if r["issue_set"] not in ("tender", "working") or r["type"] not in ("AHU", "RAF", "TEAF", "EAF", "OAF", "BIBO", "PAHU (off-sheet source)"):
            continue
        d.setdefault((r["sheet"], r["tag"], r["type"], r["duty_as_written"] if r["type"].startswith("PAHU") else ""), {})[r["issue_set"]] = r
    out = []
    for k in sorted(d):
        t, w = d[k].get("tender"), d[k].get("working")
        ref = w or t
        status = "identical" if (t and w and t["duty_as_written"] == w["duty_as_written"]) else ("only_in_tender" if t and not w else "only_in_working" if w and not t else "duty_changed")
        out.append({"sheet": k[0], "storey": ref["storey"], "tag": k[1], "type": k[2], "tender_duty_as_written": t["duty_as_written"] if t else "",
                    "working_duty_as_written": w["duty_as_written"] if w else "", "status": status})
    return out


# --------------------------------------------------------------------------- #
# orchestration and report
# --------------------------------------------------------------------------- #
def _md_escape(s) -> str:
    return str(s).replace("|", "/").replace("\n", " ")


def _mot_wd_title_block(path) -> str:
    """Short title-block summary (rev, date, stamp) of one sheet, for the report."""
    if path is None:
        return "n/a"
    try:
        tb = title_block(Sheet(str(path)))
        return "Rev %s, %s, stage %s%s" % (tb.get("rev") or "?", tb.get("date") or "?", tb.get("stage") or "?", ", stamp " + "/".join(tb["stamps"]) if tb.get("stamps") else "")
    except Exception as exc:  # pragma: no cover
        return "not readable (%s)" % type(exc).__name__


def write_report(results: List[dict], tasks: List[dict], cmp: Optional[dict], tvw: List[dict], fam: List[dict], eqd: List[dict], path: Path):
    ok = [r for r in results if r.get("ok")]
    bad = [r for r in results if not r.get("ok")]
    L: List[str] = []
    A = L.append
    A("# Schematic parser report")
    A("")
    A("Generated by `python3 -m src.schematic_parser run`. Text layer and vector layer only (PyMuPDF); no model, no network. Coordinates are page-view points, origin top-left.")
    A("")
    A("## Scope as run")
    A("")
    by = collections.Counter((r["task"]["issue_set"]) for r in ok)
    A("- Sheets parsed: %d of %d discovered (tender %d, working %d, MOT NSC %d); %d crashed." % (len(ok), len(tasks), by["tender"], by["working"], by["mot_nsc"], len(bad)))
    A("- Storeys: " + ", ".join("%s %d" % (k, v) for k, v in sorted(collections.Counter(r["task"]["storey"] for r in ok if r["task"]["issue_set"] != "mot_nsc").items())) + " (tender + working sheets); MOT NSC " + ", ".join("%s %d" % (k, v) for k, v in sorted(collections.Counter(r["task"]["storey"] for r in ok if r["task"]["issue_set"] == "mot_nsc").items())) + ".")
    A("- Request was LB3 to L06, Inpatient only. No air-side department schematic exists for Lower Basement 3 or Basement 3 in either folder (the LB3/B3 schematics in the set are MV, IP-BH-09B3x and 09LBx, which carry no room tables and are out of this parser); B1 has no IP-BH-05/06/07 air-side sheet either. Basement 2 sheets are IP-BH-05B2x and 06B2x. The job text named IP-BH-05xxx up to the 4th storey; the 5th and 6th storey Inpatient sheets (IP-BH-06040 to 06069, 07050 to 07065, 07140/07141) were added because the request runs to L06. They are marked by storey in the registers.")
    A("- MOT NSC IP-BH-160xx: Tender Corr 3 holds no 160xx sheet, so every sheet is the Tender Corr 2 issue (Rev 0A). Tender and Corr 1 are superseded and were not read. IP-BH-16059 is not in any issue.")
    wd_mot = sorted((BASIS / "Working Drawings").glob("IP-BH-160*.pdf"))
    if wd_mot:
        A("- NOT PARSED: Working Drawings also holds %d MOT NSC air-side sheets (%s). They are the working-drawing issue of the same IP-BH-160xx sheets (title block: %s). The job names the MOT NSC folder and its latest corrigendum for these sheets, so the registers carry the Tender Corr 2 issue only (title block %s); the working-drawing issue of the MOT sheets is not read, not compared with Corr 2 and not in `schematic_tender_vs_working.csv`. Corr 2 is the later date, but the two issues are different series (Corr 2 is 'for NSC ELV tender corrigendum 2', the working sheets 'for working drawing'), so Corr 2 is not shown to supersede them. Which one governs the MOT rooms is a decision for the reviewer; the parser would read these files unchanged if they were added to the scope." % (len(wd_mot), ", ".join(_sheet_id(f.name) for f in wd_mot), _mot_wd_title_block(wd_mot[0]), _mot_wd_title_block(next(iter(sorted((BASIS / "MOT NSC" / "Tender Corr 2").rglob("IP-BH-16040*.pdf"))), None) if list((BASIS / "MOT NSC" / "Tender Corr 2").rglob("IP-BH-16040*.pdf")) else None)))
    A("- Working Drawings carry no revision in the file name; the title-block REV is recorded in the table below. Sheets present in one issue only are listed under the tender-versus-working section.")
    A("")
    A("## Counts")
    A("")
    cnt = collections.Counter()
    for r in ok:
        k = r["task"]["issue_set"]
        cnt[(k, "rooms")] += len(r["room_rows"])
        cnt[(k, "equipment")] += len(r["equip_rows"])
        cnt[(k, "connections")] += len(r["conn_rows"])
        cnt[(k, "tables")] += r["n_tables"]
    A("| issue_set | sheets | room tables | room rows (one per mode) | equipment rows | connection rows |")
    A("|---|---|---|---|---|---|")
    for k in ("tender", "working", "mot_nsc"):
        A("| %s | %d | %d | %d | %d | %d |" % (k, by[k], cnt[(k, "tables")], cnt[(k, "rooms")], cnt[(k, "equipment")], cnt[(k, "connections")]))
    A("| total | %d | %d | %d | %d | %d |" % (len(ok), sum(cnt[(k, "tables")] for k in by), sum(cnt[(k, "rooms")] for k in by), sum(cnt[(k, "equipment")] for k in by), sum(cnt[(k, "connections")] for k in by)))
    A("")
    sheet_tot = [t for r in ok for t in r["totals"]]
    A("AHU check results (per AHU tag per sheet): " + ", ".join("%s %d" % (k, v) for k, v in collections.Counter(t["check_result"] for t in sheet_tot).most_common()) + ".")
    A("")
    A("Cross-sheet families (AHU tags that share a room, rooms summed over all parsed sheets of the issue): " + ", ".join("%s %d" % (k, v) for k, v in collections.Counter((f["issue_set"], f["check_result"]) for f in fam).most_common()) + ".")
    A("")
    A("Check rule: group capacity (A/B units with equal printed flow counted once, units with different flows added, common standby units such as -05/06C not added) against 1.15 x the sum of room supply x typical count in the NORM column. `ok` = within max(5 L/s, 1.5 %). `near` = within 5 %. `ahu_exceeds_rooms` and `rooms_exceed_ahu` are not errors by themselves: the AHU may serve rooms on a sheet not parsed here (sheets of other storeys or departments), or a table may be missing; each is listed per AHU in `schematic_ahu_totals.csv`.")
    A("")
    A("## Figures read with a caveat, and figures left blank")
    A("")
    allrows = [x for r in ok for x in r["room_rows"]]
    def _has_fig(row, k):
        return bool(re.search(r"\b%s\s*\d" % k, row["airflow_as_printed"]))
    blank = {f: [x for x in allrows if _has_fig(x, k) and x[f] == ""] for k, f in (("SA", "supply_L_s"), ("RA", "return_L_s"), ("EA", "exhaust_L_s"))}
    A("- Rows where `airflow_as_printed` shows an SA / RA / EA figure but the numeric field is blank: supply %d, return %d, exhaust %d (check run on the final registers; any row listed here is a parser miss, not a deliberate blank)." % (len(blank["supply_L_s"]), len(blank["return_L_s"]), len(blank["exhaust_L_s"])))
    joined = [x for x in allrows if "two mode columns were joined" in x["method"]]
    A("- Two mode columns sharing one text line ('SA 210 L/s SA 210 L/s', 'NORMAL / FUTURE', 'NORM / PAND'): the PDF text layer joins the values of neighbouring columns into one line. The parser re-cuts such AIRFLOW lines from the word boxes at each flow key and assigns each piece to its column by position. %d room rows were read this way (method says so); sheets: %s." % (len(joined), ", ".join(sorted({x["sheet"] for x in joined})) or "none"))
    fut = [x for x in allrows if "mode names read from the column headers" in x["method"]]
    A("- MOT tables whose mode names (NORMAL / FUTURE) are printed above the value columns with no MODE label: %d room rows, sheets %s. The two columns are two rows of the register, one per mode." % (len(fut), ", ".join(sorted({x["sheet"] for x in fut})) or "none"))
    mult = [x for x in allrows if "figure printed as 'N x M'" in x["method"]]
    A("- Figures printed as 'N x M L/s' (for example `SA 140 x 2 L/s`): %d rows. The per-unit value N is stored; the multiplier is NOT applied and is not written to typical_count. On the MOT sheets the multiplier equals the typical count of the room, so N is the per-room figure; on IP-BH-05038 the figure is `x 5` while the typical count read is 1, so the room sum of that AHU is understated if the five rooms are separate. These are listed, not resolved." % len(mult))
    unit = [x for x in allrows if "unit printed as '/s' without the L" in x["method"]]
    A("- Unit printed as '/s' without the L (`SA 150/s`, `OA 305/s`): %d rows. Read as L/s, as-printed text kept in `airflow_as_printed`, confidence medium. A misprint of the figure itself (for example `OA 305/s` beside `OA 1005L/s`) cannot be excluded and is not corrected." % len(unit))
    kea = [x for x in allrows if re.search(r"\bKEA\s*\d", x["airflow_as_printed"])]
    A("- Kitchen exhaust (`KEA`): %d rows. `KEA nnnn L/s` is read as the exhaust figure when no EA/EX figure is printed in the same cell. `KMA` (kitchen make-up) has no register column and stays in `airflow_unparsed` as printed. Duct labels `KEA` / `KMA` printed above a room box are not room names." % len(kea))
    both_oa = [x for x in allrows if len(re.findall(r"\bOA\s*[\d.]+", x["oa_note"])) > 1]
    A("- `oa_note` holding more than one OA figure (modes mixed): %d rows." % len(both_oa))
    fam_blank = [f for f in fam if f["rooms_without_supply_figure"] not in ("", 0)]
    A("- Cross-sheet family checks with rooms that print an SA figure or draw an SA riser but have no supply value: %d families (column `rooms_without_supply_figure` is the count per family; their sum is incomplete and the note says so)." % len(fam_blank))
    A("")
    A("## Pilot reproduction (IP-BH-05010, both issues)")
    A("")
    if cmp:
        c = cmp["counts"]
        fr = cmp.get("full_row", {})
        A("Two comparisons are made. The first uses the data fields only; the second uses every column of the pilot register, provenance columns (method, confidence, boxes) included. Reading the first alone would overstate the match.")
        A("")
        A("### 1. Data fields (provenance columns excluded)")
        A("")
        A("| register | pilot rows | parser rows | rows identical on all data fields | field differences listed below |")
        A("|---|---|---|---|---|")
        for k in ("rooms", "equipment", "connections"):
            idn = fr[k]["identical_excluding_provenance"] if fr else c[k]["identical_rows"]
            A("| %s | %d | %d | %d | %d |" % (k, c[k]["pilot"], c[k]["parsed"], idn, len(cmp[k])))
        A("")
        A("Data fields compared. rooms: AHU tags, SA/RA/EA, pressure, filter, temperature, RH, air class, OA note, serving fans, isolation target, services drawn (order-insensitive), schematic title, typical count. equipment: type, duty, plant level, served level, pair, duty/standby, accessories. connections: from, to, service, flow text. Room names are compared after collapsing whitespace (the pilot register keeps the as-printed double space in `P3 PATIENT BAY  - ACUTE ...`; the parser keeps the text-layer string as printed).")
        A("")
        A("### 2. Every pilot column, exact text (provenance included)")
        A("")
        A("| register | pilot rows | rows paired | identical on ALL columns | differing rows by column |")
        A("|---|---|---|---|---|")
        for k in ("rooms", "equipment", "connections"):
            if fr:
                cd = ", ".join("%s %d" % (c_, n_) for c_, n_ in sorted(fr[k]["col_differs"].items(), key=lambda x: -x[1]))
                A("| %s | %d | %d | %d | %s |" % (k, fr[k]["pilot_rows"], fr[k]["paired"], fr[k]["identical_all_columns"], cd))
        A("")
        if fr:
            A("What the provenance differences are (none of them changes a data value):")
            A("")
            A("- `method` differs on every row of every register: the pilot wrote hand-extraction wording (vector-rectangle columns, colour-coded riser chained to header line); the parser writes the wording of the script that actually ran (label-anchored grid, duct-colour component graph). The parser text is the true method of the rows in the registers now; the pilot text described the manual process.")
            cm = lambda k: ", ".join("%s to %s: %d" % (a_, b_, n_) for (a_, b_), n_ in sorted(fr[k]["conf_moves"].items()))
            A("- `confidence` differs: rooms %d rows (%s), equipment %d (%s), connections %d (%s). The pilot gave 'high' to nearly every row by hand; the parser grades each row by its own rule (the evidence behind each grade is written into that row's `method` and, for rooms, in the AHU association note) and so lowers some rows to 'medium'. The register now carries the parser's grade. No attempt was made to decide which grade is right for a given row." % (fr["rooms"]["col_differs"].get("confidence", 0), cm("rooms"), fr["equipment"]["col_differs"].get("confidence", 0), cm("equipment"), fr["connections"]["col_differs"].get("confidence", 0), cm("connections")))
            A("- Boxes: rooms table box differs on %d rows and outline box on %d rows (largest coordinate difference %.1f pt and %.1f pt: the pilot boxes were measured by hand, the parser takes the grid edges from the vector layer). Equipment tag box differs on %d rows and duty box on %d rows: mostly 0.1 pt rounding; the large differences are the isolation-exhaust (IEA) rows, where the pilot boxes the IEA label and the parser the CONNECTED TO note (up to %.0f pt apart), and the W/P LOUVERS rows. Connection `box_page_view` differs on %d of 238 rows (up to %.0f pt): the two registers cite different boxes for the same link, and links with an identical key (same AHU, room name and flow text) are paired in sorted order, so a large difference is not by itself a positional error. The boxes were not checked row by row against the sheet; neither set is treated as the ground truth." % (fr["rooms"]["col_differs"].get("box_page_view_table", 0), fr["rooms"]["col_differs"].get("box_page_view_outline", 0), fr["rooms"]["box_max_delta_pt"].get("box_page_view_table", 0), fr["rooms"]["box_max_delta_pt"].get("box_page_view_outline", 0), fr["equipment"]["col_differs"].get("box_page_view_tag", 0), fr["equipment"]["col_differs"].get("box_page_view_duty", 0), fr["equipment"]["box_max_delta_pt"].get("box_page_view_tag", 0), fr["connections"]["col_differs"].get("box_page_view", 0), fr["connections"]["box_max_delta_pt"].get("box_page_view", 0)))
            A("- `services_drawn` differs in %d room rows: same set of services, different order (pilot 'IEA/SA', parser 'SA/IEA'). Compared order-insensitively in table 1." % fr["rooms"]["services_order_only"])
            A("- Equipment data fields that differ are the 8 rows listed in the difference table below (duty_standby 4, accessories_nearby 4).")
            A("")
        bl = cmp.get("baseline")
        A("### Baseline and its independence")
        A("")
        A("`out/` is git-ignored, so the pilot registers were never under version control. The baseline is the copy that the parser snapshotted from `out/registers/schematic_*.csv` the first time it ran, before it overwrote them: `out/qa/evidence/schematic_pilot_{rooms,equipment,connections}.csv`" + (" (sha256 " + ", ".join("%s %s" % (k_, v_[:16]) for k_, v_ in sorted(cmp.get("snapshot_sha256", {}).items())) + ")" if cmp.get("snapshot_sha256") else "") + ". That is the builder's own copy; its integrity cannot be proven from outside. What can be checked independently of the parser is whether the pilot values are on the page:")
        A("")
        if bl:
            A("- Text-layer check (PyMuPDF words only, no parser code): of %d pilot room rows, %d have every printed supply/return/exhaust figure, pressure, filter and temperature value found as words inside the pilot's own table box on the sheet (%d of %d values)." % (bl["rows_checked"], bl["rows_confirmed"], bl["values_confirmed"], bl["values_checked"]))
            for x_ in bl["not_confirmed"][:20]:
                A("  - not confirmed: " + _md_escape(x_))
        A("- Row counts agree with the independently written note in `out/qa/gate0_notes.md` (section 'Schematic pilot IP-BH-05010': 56 rooms, 44 equipment, 238 connections over both issues) and with `out/qa/schematic_pilot_IP-BH-05010.md`.")
        A("- Both checks show the pilot values are real printed values; they do not show that the pilot register was complete or that no row was changed after the hand extraction.")
        A("")
        A("Data-field differences (all in the equipment register; rooms and connections reproduce on every data field):")
        A("")
        if cmp["equipment"] or cmp["rooms"] or cmp["connections"]:
            A("| register | issue | item | field | pilot | parser | explanation |")
            A("|---|---|---|---|---|---|---|")
            expl = {"accessories_nearby": "pilot assigned damper/louvre labels by hand-set roles and distances; parser counts each label under its nearest tag within 160 pt (400 pt for louvre, FD, attenuator labels). Counts are indicative only.",
                    "duty_standby": "pilot repeated the A/B wording on the BIBO housing; a BIBO is a single housing, parser writes 'single unit'.",
                    "served_level": "parser leaves it blank where no room table of the sheet is tied to the item.",
                    "plant_level": "parser reads the storey label nearest below the item.", "duty_as_written": "row pairing order only (same two PAHU or IEA rows, swapped between pilot and parser)."}
            for reg in ("rooms", "equipment", "connections"):
                for d in cmp[reg]:
                    A("| %s | %s | %s | %s | %s | %s | %s |" % (reg, _md_escape(d["issue"]), _md_escape(d["key"]), _md_escape(d["field"]), _md_escape(d["pilot"]), _md_escape(d["parsed"]), _md_escape(expl.get(d["field"], ""))))
        else:
            A("none")
    A("")
    A("## Tender versus working")
    A("")
    st = collections.Counter(r["status"] for r in tvw)
    A("Room rows compared: " + ", ".join("%s %d" % (k, v) for k, v in st.most_common()) + ". Detail in `out/registers/schematic_tender_vs_working.csv`; equipment duty differences in `out/qa/schematic_equipment_tender_vs_working.csv` (%s)." % ", ".join("%s %d" % (k, v) for k, v in collections.Counter(r["status"] for r in eqd).most_common()))
    only_t = sorted({r["task"]["sheet"] for r in ok if r["task"]["issue_set"] == "tender"} - {r["task"]["sheet"] for r in ok if r["task"]["issue_set"] == "working"})
    only_w = sorted({r["task"]["sheet"] for r in ok if r["task"]["issue_set"] == "working"} - {r["task"]["sheet"] for r in ok if r["task"]["issue_set"] == "tender"})
    A("")
    A("Sheets only in the tender issue: %s. Sheets only in the working issue: %s." % (", ".join(only_t) or "none", ", ".join(only_w) or "none"))
    A("")
    A("Conflicts are listed, not resolved: every room whose airflow differs between the issues is a row with status `airflow_changed`; the working issue is not treated as superseding the tender issue here.")
    A("")
    A("## Per sheet")
    A("")
    A("| issue_set | sheet | storey | issue | title-block rev / date / stage | room tables | room rows | AHUs | AHU checks | rooms without AHU | rooms without name | warnings |")
    A("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in sorted(ok, key=lambda r: (r["task"]["issue_set"], r["task"]["storey"], r["task"]["sheet"])):
        t = r["task"]
        tb = r["title_block"]
        rr = r["room_rows"]
        noahu = sum(1 for x in rr if not x["ahu_tag"] and x["supply_L_s"] != "")
        noname = sum(1 for x in rr if not x["room_name_as_written"])
        chk = collections.Counter(x["check_result"] for x in r["totals"])
        n_ahu = len({x["ahu_tag"] for x in r["totals"]})
        A("| %s | %s | %s | %s | %s / %s / %s | %d | %d | %d | %s | %d | %d | %s |" % (t["issue_set"], t["sheet"], t["storey"], _md_escape(t["issue"]), tb.get("rev") or "?", tb.get("date") or "?", tb.get("stage") or "?",
              r["n_tables"], len(rr), n_ahu, ", ".join("%s %d" % kv for kv in chk.most_common()) or "-", noahu, noname, _md_escape("; ".join(r["warnings"])[:160])))
    A("")
    A("## Sheets or tables not parsed, and why")
    A("")
    none_t = [r for r in ok if r["n_tables"] == 0]
    if none_t:
        why = {"IP-BH-05047": "the working-issue sheet carries only stamps and title block in its text layer (about 380 words, no tables, no equipment tags): a placeholder or blank schematic, nothing to extract.",
               "IP-BH-16069": "plant-level sheet at the 7th storey: 59 AHU and 8 PAHU boxes with their NORMAL/SINGLE PASS and SETBACK flows and 'FROM/TO' notes, no room tables. Its equipment rows are extracted; there are no room rows, so its AHUs have no room check (`no_rooms`). The sheet title says 6TH STOREY POD3 STERILE HOLD AND SUPPORT, the content is the AHU plant."}
        for r in none_t:
            A("- %s (%s, %s): no room table found (no AIRFLOW label with a FILTER label below it). %s Equipment extracted: %d tags." % (r["task"]["sheet"], r["task"]["issue_set"], r["task"]["issue"], why.get(r["task"]["sheet"], "").capitalize() if why.get(r["task"]["sheet"]) else "", r["n_eq"]))
    else:
        A("- every sheet yielded at least one room table")
    for r in bad:
        A("- %s (%s): parser error %s" % (r["task"]["sheet"], r["task"]["issue_set"], r["error"]))
    A("")
    A("Table-level notes (a table that is read but with a reduced method):")
    A("")
    nb = collections.Counter()
    for r in ok:
        for x in r["room_rows"]:
            for note in ("borderless table", "room name right of the table", "room name below the table", "no room outline found", "searched beyond the room walls", "top frame line not drawn", "bottom frame line not drawn"):
                if note in x["method"]:
                    nb[(r["task"]["issue_set"], note)] += 1
    for (k, note), v in sorted(nb.items()):
        A("- %s: %d room rows with '%s'" % (k, v, note))
    A("")
    A("Known limits:")
    A("")
    A("- MOT NSC sheets draw the ducts as images: there are no vector duct strokes, so room-to-AHU association is sheet-level only (all AHUs on the sheet as one group, confidence low); the group check against 1.15 x rooms is the evidence that it is usable (see the per-sheet column).")
    A("- Where an AHU is drawn on another sheet (ICU/HDU pod continuation sheets, general-space sheets), the room rows carry the AHU tag from the continuation note or stay blank; the cross-sheet family check in `schematic_ahu_totals.csv` (scope `cross_sheet_family`) joins them.")
    A("- Room names are taken from the three places the drawings use (above the table, beside it, under it); where a name is missing the row says so in `method` and confidence is low.")
    A("- Values are as printed: `OA 2495tx L/s`, `SA - L/s`, `6 ACH` in an OA position and similar are kept in `oa_note` / `airflow_unparsed` and not interpreted.")
    A("- AHU capacity uses the NORMAL figure where several mode lines are printed (NORMAL / PANDEMIC / SETBACK); every printed line is kept in `ahu_supply_as_written`.")
    A("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


def run(sheets: Optional[List[str]] = None, workers: int = 3, write: bool = True, regdir: Optional[Path] = None, qadir: Optional[Path] = None) -> dict:
    regdir = regdir or REG
    qadir = qadir or QA
    tasks = discover(sheets)
    pilot = snapshot_pilot() if write and regdir == REG else {"rooms": [], "equipment": [], "connections": []}
    if workers > 1 and len(tasks) > 1:
        from multiprocessing import Pool
        with Pool(workers) as pool:
            results = pool.map(_worker, tasks, chunksize=2)
    else:
        results = [_worker(t) for t in tasks]
    ok = [r for r in results if r.get("ok")]
    rooms = sorted([x for r in ok for x in r["room_rows"]], key=lambda r: (_sort_key(r)[0:3], r["issue"]))
    equip = sorted([x for r in ok for x in r["equip_rows"]], key=lambda r: (_sort_key(r)[0:3], r["issue"]))
    conns = sorted([x for r in ok for x in r["conn_rows"]], key=lambda r: (_sort_key(r)[0:3], r["issue"]))
    totals = sorted([x for r in ok for x in r["totals"]], key=lambda r: ({"tender": 0, "working": 1, "mot_nsc": 2}[r["issue_set"]], r["storey"], r["sheet"], r["ahu_tag"]))
    fam = aggregate_families(results)
    tvw = tender_vs_working(rooms)
    eqd = equipment_tender_vs_working(equip)
    cmp = compare_pilot(pilot, rooms, equip, conns) if pilot["rooms"] else None
    if cmp:
        cmp["baseline"] = verify_pilot_baseline(pilot, tasks)
        cmp["snapshot_sha256"] = {nm: hashlib.sha256((QA / "evidence" / ("schematic_pilot_%s.csv" % nm)).read_bytes()).hexdigest() for nm in ("rooms", "equipment", "connections")
                                  if (QA / "evidence" / ("schematic_pilot_%s.csv" % nm)).exists()}
    out = {"tasks": tasks, "results": results, "rooms": rooms, "equipment": equip, "connections": conns, "totals": totals, "families": fam,
           "tvw": tvw, "eqd": eqd, "pilot_cmp": cmp}
    if write:
        _write_csv(regdir / "schematic_rooms.csv", ROOM_COLS, rooms)
        _write_csv(regdir / "schematic_equipment.csv", EQUIP_COLS, equip)
        _write_csv(regdir / "schematic_connections.csv", CONN_COLS, conns)
        _write_csv(regdir / "schematic_ahu_totals.csv", TOTAL_COLS, totals + fam)
        _write_csv(regdir / "schematic_tender_vs_working.csv", TVW_COLS, tvw)
        _write_csv(qadir / "schematic_equipment_tender_vs_working.csv", EQD_COLS, eqd)
        write_report(results, tasks, cmp, tvw, fam, eqd, qadir / "schematic_parser_report.md")
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m src.schematic_parser", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("run", help="parse the schematics and write the registers and the report")
    rp.add_argument("--sheets", nargs="*", help="sheet numbers, e.g. IP-BH-05010 (default: all in scope)")
    rp.add_argument("--workers", type=int, default=3)
    rp.add_argument("--no-write", action="store_true", help="parse and print counts only")
    rp.add_argument("--out-registers", help="alternative register directory (tests)")
    rp.add_argument("--out-qa", help="alternative QA directory (tests)")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        import warnings
        warnings.filterwarnings("ignore")
        out = run(args.sheets, args.workers, not args.no_write, Path(args.out_registers) if args.out_registers else None,
                  Path(args.out_qa) if args.out_qa else None)
        ok = [r for r in out["results"] if r.get("ok")]
        print("sheets %d parsed of %d; rooms %d; equipment %d; connections %d; AHU rows %d; tender-vs-working rows %d" %
              (len(ok), len(out["tasks"]), len(out["rooms"]), len(out["equipment"]), len(out["connections"]), len(out["totals"]) + len(out["families"]), len(out["tvw"])))
        for r in out["results"]:
            if not r.get("ok"):
                print("FAILED", r["task"]["sheet"], r["error"])
        return 0 if all(r.get("ok") for r in out["results"]) else 1
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
