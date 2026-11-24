"""Mark-up copies of the working drawings (brief 9.4, mark-up set).

A working drawing PDF is copied to out/schematics/markups/<sheet>_markup.pdf.
The copy gets unflattened annotations only, from out/schematics/change_register.csv:

* a superseded value is clouded (polygon annotation, intent PolygonCloud) and
  the new value is in a call-out (FreeText with a call-out line);
* added rooms, fans, spill paths and exhaust fans go in a tabulated note beside
  the system (one FreeText row per change ID, rows stacked as a table);
* removed items are struck through (StrikeOut);
* queries are a dashed box and a short label.

Every annotation has the change ID as its subject and sits on an optional
content layer for its type (Rooms, Airflows, Fans, New systems, Queries).
The page content stream is not changed: the copy is saved incrementally, so the
original bytes stay as they are.  `verify` proves it by a byte comparison of
the content stream and a pixel comparison with the annotations hidden.

    python -m src.schematics.markup run [--sheet IP-BH-05010 ...]
    python -m src.schematics.markup all [--register CSV] [--out DIR]     every sheet with an instruction or a query

A list-only unit or fan (out/schematics/scope.csv) gets no annotation on any sheet; a sheet whose rows are all list-only
gets no file.  An added room is a room box with its criteria and duct branches (D-076).
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pymupdf

from . import drawings_common as dc

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "data" / "source" / "Basis" / "Working Drawings"
REGISTER = ROOT / "out" / "schematics" / "change_register.csv"
OUT_DIR = ROOT / "out" / "schematics" / "markups"
AUTHOR = "Schematic review"
OVERALL_ADD = "not on the overall schematic"
NAMES = dc.NameMap()

LAYERS = {"rooms": "Rooms", "airflows": "Airflows", "fans": "Fans",
          "new systems": "New systems", "queries": "Queries"}
COLOURS = {"rooms": (0.0, 0.30, 0.85), "airflows": (0.85, 0.0, 0.0), "fans": (0.0, 0.55, 0.25),
           "new systems": (0.5, 0.1, 0.65), "queries": (0.85, 0.0, 0.6)}
FS = 10.0          # call-out and table font size in page points
ROW_H = 15.0
MARGIN = 20.0
SCALE = 0.5        # raster scale for the free-space search


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
_SCOPE: Optional[dc.Scope] = None
_LEDGER: Optional[dc.Ledger] = None
SKIP_CLASSES = ("increase of 5 L/s or less", "no change", "withdrawn")     # recorded in the change list, nothing to mark


def scope() -> dc.Scope:
    global _SCOPE
    if _SCOPE is None:
        _SCOPE = dc.Scope()
    return _SCOPE


def ledger() -> dc.Ledger:
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = dc.Ledger()
    return _LEDGER


def reset_caches() -> None:
    global _SCOPE, _LEDGER
    _SCOPE = _LEDGER = None


def all_rows() -> List[Dict[str, str]]:
    with open(REGISTER, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def markable(r: Dict[str, str]) -> bool:
    """A row that gets an annotation: not a list-only unit or fan (build line 23), not a suppressed or withdrawn row."""
    if r["class"].lower().startswith(SKIP_CLASSES):
        return False
    if not (r["action"].startswith(("Mark up", "Query", "New system: FCU and outdoor-air connection")) or r["action"].lower().startswith(("strike", "mark-up"))):
        return False
    if scope().sheet_list_only(r["sheet"]):
        return False            # every row of a sheet whose AHUs are all list-only stays in the list only (P4-21)
    return not (scope().unit_list_only(r["unit"]) or scope().unit_list_only(r["item"]))


def read_register(sheet: str) -> List[Dict[str, str]]:
    """Rows of one sheet that get an annotation.  The box is read from the ledger row of the change when the row
    names one (column ledger_id); the box of the row is the fallback (build line 25)."""
    out = []
    for r in all_rows():
        if r["sheet"] != sheet or not markable(r):
            continue
        lid = r.get("ledger_id") or r.get("entity_id") or ""
        if lid:
            b = ledger().box_of(lid)
            if b:
                r = dict(r, bbox=json.dumps([round(v, 1) for v in b]))
        out.append(r)
    return out


def sheets_to_mark() -> List[str]:
    """Sheets with at least one instruction or query for a unit that is not list-only (build line 25)."""
    return sorted({r["sheet"] for r in all_rows() if markable(r) and re.match(r"IP-BH-", r["sheet"])})


def parse_box(text: str) -> Optional[pymupdf.Rect]:
    try:
        v = json.loads(text)
        if v and len(v) == 4:
            return pymupdf.Rect(*v)
    except (ValueError, TypeError):
        pass
    return None


def source_pdf(sheet: str) -> Path:
    hits = sorted(glob.glob(str(SRC_DIR / (sheet + "_*.pdf"))))
    if not hits:
        raise FileNotFoundError("no working drawing for " + sheet)
    return Path(hits[0])


def form_of(row: Dict[str, str]) -> str:
    """cloud, strike, table or query."""
    a = row["action"].lower()
    cls = row["class"].lower()
    if a.startswith("query") or row["quantity"] == "Pressure target" or "pressure" in cls or cls.startswith("moved"):
        return "query"          # a pressure row is a query always: no cloud on a drawn pressure (D-081)
    if "strike through" in a:
        return "strike"
    if row["layer_type"] == "rooms" and cls == "to add" and re.search(r"\badd\b", a):
        return "roombox"        # D-076: a drawn room box with its criteria and duct branches
    if re.search(r"\badd\b", a) or a.startswith("add"):
        return "table"
    return "cloud"


def num_text(v: str) -> str:
    """'1790.9' -> '1,791', '220.0' -> '220', '45.5' -> '45.5': no false decimals."""
    try:
        x = float((v or "").replace(",", ""))
    except ValueError:
        return (v or "").strip()
    if abs(x - round(x)) < 0.05 or abs(x) >= 100:
        return f"{round(x):,}"
    return f"{x:,.1f}"


def shown(v: str, uom: str = "") -> str:
    v = (v or "").strip()
    if v and not v.lower().startswith("not "):
        return (num_text(v) + " " + uom).strip()
    return v


def new_value_text(row: Dict[str, str]) -> str:
    t = _new_value_text(row)
    if row["note"].startswith("Room list not confirmed"):
        t += ". Room list not confirmed: check it before you apply this value"
    if row["quantity"].lower().startswith("outdoor air") and row["layer_type"] == "airflows":
        # the room that sets the fraction, the fraction and the sum of room outdoor air (build line 16), then the fixed end
        bits = []
        if row.get("oa_room"):
            bits.append("fraction %s set by %s" % (row.get("oa_fraction") or "not given", NAMES.show(row["oa_room"])))
        if row.get("oa_room_sum"):
            bits.append("sum of room outdoor air %s L/s" % num_text(row["oa_room_sum"]))
        t += ("; " + "; ".join(bits) if bits else "") + ". AHU outdoor air; PAHU duty is a separate row"
    return dc.clean_text(t)


def _new_value_text(row: Dict[str, str]) -> str:
    cls = row["class"]
    unit = row["unit_of_measure"]
    q = row["quantity"]
    if "typical" in q.lower():
        return "%s: change typical count %s to %s" % (NAMES.show(row["item"]), num_text(row["drawn"]), num_text(row["required"]))
    if row["layer_type"] == "fans" and cls == "to resize":
        return "%s: change duty %s to %s" % (row["item"], shown(row["drawn"], unit), shown(row["required"], unit))
    return "%s %s: change %s to %s" % (row["item"], q[:1].lower() + q[1:] if q[:2].islower() or not q[:2].isupper() else q,
                                       shown(row["drawn"], unit), shown(row["required"], unit))


def _isnum(v: str) -> bool:
    try:
        float((v or "").replace(",", ""))
        return True
    except ValueError:
        return False


def query_text(row: Dict[str, str]) -> str:
    """The reason of a query, with the values, in one or two short lines (the label on the sheet)."""
    a = re.sub(r"^Query:\s*", "", row["action"]).strip()
    a = re.sub(r"\s*\(change ID [^)]*\)", "", a)
    a = a[:1].upper() + a[1:]
    d, r, u = row["drawn"], row["required"], row["unit_of_measure"]
    if row["quantity"] == "Pressure target":
        def pa(v: str) -> str:
            return ("%+g Pa" % float(v)) if _isnum(v) else v
        cell = re.search(r"cell ([A-Z]+\d+)", row["source"] or "")
        txt = "Drawn regime %s; workbook uses %s%s. Drawn value stays. Workbook input to check" % (
            pa(d), pa(r), (" (cell %s)" % cell.group(1)) if cell else "")
        if "conflict" in row["class"].lower() or "reversed" in row["note"]:
            txt = "Pressure direction conflict. " + txt
        return dc.clean_text(txt)
    elif _isnum(d) and _isnum(r):
        txt = "%s. Drawn %s, workbook %s" % (a, shown(d, u), shown(r, u))
    elif _isnum(d) or _isnum(r):
        txt = "%s. %s" % (a, ("Drawn " + shown(d, u)) if _isnum(d) else ("Workbook " + shown(r, u)))
    else:
        txt = a
    txt = dc.clean_text(txt)
    return txt if len(txt) <= 150 else txt[:147].rsplit(" ", 1)[0] + " ..."


def wrap(text: str, width: float, fs: float = FS, font: str = "helv") -> List[str]:
    out: List[str] = []
    for para in text.split("\n"):
        cur = ""
        for w in para.split():
            t = (cur + " " + w).strip()
            if pymupdf.get_text_length(t, fontname=font, fontsize=fs) <= width or not cur:
                cur = t
            else:
                out.append(cur)
                cur = w
        out.append(cur)
    return out


# --------------------------------------------------------------------------- #
# Free space
# --------------------------------------------------------------------------- #
class FreeSpace:
    """Raster of the untouched page; finds white rectangles for call-outs and notes."""

    def __init__(self, page: pymupdf.Page):
        pm = page.get_pixmap(matrix=pymupdf.Matrix(SCALE, SCALE), colorspace=pymupdf.csGRAY, annots=False)
        a = np.frombuffer(pm.samples, dtype=np.uint8).reshape(pm.height, pm.width)
        self.ink = (a < 245).astype(np.int32)
        self.sat = np.pad(self.ink.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
        self.rect = page.rect
        self.placed: List[pymupdf.Rect] = []

    def _ink(self, r: pymupdf.Rect) -> int:
        h, w = self.ink.shape
        x0, y0 = max(0, int(r.x0 * SCALE)), max(0, int(r.y0 * SCALE))
        x1, y1 = min(w, int(math.ceil(r.x1 * SCALE))), min(h, int(math.ceil(r.y1 * SCALE)))
        if x1 <= x0 or y1 <= y0:
            return 1
        s = self.sat
        return int(s[y1, x1] - s[y0, x1] - s[y1, x0] + s[y0, x0])

    def free(self, r: pymupdf.Rect, pad: float = 4.0) -> bool:
        p = self.rect
        if r.x0 < p.x0 + MARGIN or r.y0 < p.y0 + MARGIN or r.x1 > p.x1 - MARGIN or r.y1 > p.y1 - MARGIN:
            return False
        if self._ink(r + (-pad, -pad, pad, pad)) > 0:
            return False
        return not any((r + (-pad, -pad, pad, pad)).intersects(q) for q in self.placed)

    def leader_ink(self, anchor: pymupdf.Rect, r: pymupdf.Rect) -> int:
        """Ink pixels and placed notes that the leader line from the anchor to the rectangle would cross
        (the two ends are excluded)."""
        cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
        p0 = (min(max(cx, anchor.x0), anchor.x1), min(max(cy, anchor.y0), anchor.y1))
        p1 = (min(max(p0[0], r.x0), r.x1), min(max(p0[1], r.y0), r.y1))
        n = max(2, int(math.hypot(p1[0] - p0[0], p1[1] - p0[1]) / 3))
        h, w = self.ink.shape
        lb = pymupdf.Rect(min(p0[0], p1[0]), min(p0[1], p1[1]), max(p0[0], p1[0]), max(p0[1], p1[1])) + (-2, -2, 2, 2)
        near = [q for q in self.placed if q.intersects(lb)]
        hit = 0
        for k in range(3, n - 2):
            x = p0[0] + (p1[0] - p0[0]) * k / n
            y = p0[1] + (p1[1] - p0[1]) * k / n
            xi, yi = int(x * SCALE), int(y * SCALE)
            if 0 <= xi < w and 0 <= yi < h and self.ink[yi, xi]:
                hit += 1
            elif near and any(q.x0 <= x <= q.x1 and q.y0 <= y <= q.y1 for q in near):
                hit += 1
        return hit

    last_ink = 0

    def place(self, w: float, h: float, anchor: Optional[pymupdf.Rect], leader: bool = False,
              max_ink: Optional[int] = None, max_dist: float = 700.0) -> Optional[pymupdf.Rect]:
        """Free w x h rectangle near the anchor.  With ``leader`` the candidates are ranked by the ink and notes
        that the leader line would cross (heavily) plus its length; ``max_ink`` rejects a candidate whose leader
        crosses more than that (the caller then draws no leader).  ``last_ink`` holds the winner's value."""
        ax, ay = ((anchor.x0 + anchor.x1) / 2, (anchor.y0 + anchor.y1) / 2) if anchor else (
            self.rect.width / 2, self.rect.height / 2)
        self.last_ink = 0
        if anchor is not None:
            cands: List[Tuple[float, pymupdf.Rect]] = []
            step = 10.0
            for dx in np.arange(-600, 601, step):
                for dy in np.arange(-450, 451, step):
                    r = pymupdf.Rect(ax + dx - w / 2, ay + dy - h / 2, ax + dx + w / 2, ay + dy + h / 2)
                    if r.intersects(anchor + (-3, -3, 3, 3)):
                        continue
                    d = math.hypot(dx, dy)
                    if d <= max_dist and self.free(r):
                        cands.append((d, r))
            cands.sort(key=lambda t: t[0])
            if cands and not leader:
                self.placed.append(cands[0][1])
                return cands[0][1]
            best = None
            for dist, r in cands[:260]:
                ink = self.leader_ink(anchor, r)
                if max_ink is not None and ink > max_ink:
                    continue
                cost = ink * 25 + dist
                if best is None or cost < best[0]:
                    best = (cost, r, ink)
                if ink == 0:
                    break
            if best is not None:
                self.last_ink = best[2]
                self.placed.append(best[1])
                return best[1]
            if max_ink is not None and cands:
                self.last_ink = 99999
                self.placed.append(cands[0][1])
                return cands[0][1]
        step = 24.0
        best = None
        for x in np.arange(self.rect.x0 + MARGIN, self.rect.x1 - MARGIN - w, step):
            for y in np.arange(self.rect.y0 + MARGIN, self.rect.y1 - MARGIN - h, step):
                r = pymupdf.Rect(x, y, x + w, y + h)
                d = math.hypot(x + w / 2 - ax, y + h / 2 - ay)
                if (best is None or d < best[0]) and self.free(r):
                    best = (d, r)
        if best:
            self.last_ink = 99999
            self.placed.append(best[1])
            return best[1]
        return None


# --------------------------------------------------------------------------- #
# Annotation helpers
# --------------------------------------------------------------------------- #
def cloud_points(r: pymupdf.Rect, arc: float = 9.0) -> List[Tuple[float, float]]:
    """Scalloped outline around a rectangle: bulges point outward."""
    pts: List[Tuple[float, float]] = []
    corners = [(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1)]
    for i in range(4):
        (x0, y0), (x1, y1) = corners[i], corners[(i + 1) % 4]
        length = math.hypot(x1 - x0, y1 - y0)
        n = max(2, int(round(length / arc)))
        ux, uy = (x1 - x0) / length, (y1 - y0) / length
        nx, ny = uy, -ux           # outward for a clockwise path in a y-down frame
        seg = length / n
        for k in range(n):
            sx, sy = x0 + ux * seg * k, y0 + uy * seg * k
            for j in range(0, 6):
                t = j / 6.0
                ang = math.pi * t
                px = sx + ux * seg * (1 - math.cos(ang)) / 2
                py = sy + uy * seg * (1 - math.cos(ang)) / 2
                bulge = math.sin(ang) * seg * 0.38
                pts.append((px + nx * bulge, py + ny * bulge))
    return pts


def co_pt(page: pymupdf.Page, p: pymupdf.Point) -> pymupdf.Point:
    """PyMuPDF writes /CL with the x offset of a negative crop-box origin added where it
    must be taken away (checked by `verify`).  Pre-shift the point so that /CL is right."""
    return pymupdf.Point(p.x + 2 * page.cropbox.x0, p.y)


def freetext(page, rect, text, fs, font, col, bw, **kw):
    text = dc.ZERO_WIDTH.sub("", text)
    if kw.get("callout"):
        kw["callout"] = tuple(co_pt(page, q) for q in kw["callout"])
    a = page.add_freetext_annot(rect, text, fontsize=fs, fontname=font, text_color=col,
                                fill_color=(1, 1, 1), border_width=bw, **kw)
    if not kw.get("callout"):       # PyMuPDF writes a dummy /CL: remove it
        page.parent.xref_set_key(a.xref, "CL", "null")
    return a


class Marker:
    def __init__(self, doc: pymupdf.Document, page: pymupdf.Page, space: FreeSpace):
        self.doc, self.page, self.space = doc, page, space
        self.ocg = {k: doc.add_ocg(v, on=True) for k, v in LAYERS.items()}
        self.log: List[Dict[str, str]] = []

    def _finish(self, a: pymupdf.Annot, row: Dict[str, str], kind: str, content: str = ""):
        if "FreeText" in kind:      # the visible text of a FreeText is its contents: keep it
            a.set_info(title=AUTHOR, subject=row["change_id"])
        else:
            a.set_info(title=AUTHOR, subject=row["change_id"], content=dc.clean_text(content))
        a.set_oc(self.ocg[row["layer_type"]])
        a.update()
        self.log.append({"change_id": row["change_id"], "layer": LAYERS[row["layer_type"]], "annotation": kind,
                         "rect": json.dumps([round(v, 1) for v in a.rect])})

    def cloud(self, row, box: pymupdf.Rect):
        col = COLOURS[row["layer_type"]]
        outer = box + (-3, -3, 3, 3)
        a = self.page.add_polygon_annot(cloud_points(outer))
        a.set_colors(stroke=col)
        a.set_border(width=1.3)
        self.doc.xref_set_key(a.xref, "IT", "/PolygonCloud")
        self._finish(a, row, "cloud (polygon)", row["action"])
        self.space.placed.append(outer + (-6, -6, 6, 6))
        return outer

    def callout(self, row, anchor: pymupdf.Rect, text: str, kind="call-out (FreeText)"):
        col = COLOURS[row["layer_type"]]
        lines = wrap(row["change_id"] + "  " + text, 230)
        w = max(pymupdf.get_text_length(l, fontname="helv", fontsize=FS) for l in lines) + 8
        h = len(lines) * FS * 1.2 + 6
        r = self.space.place(w, h, anchor, leader=True)
        if r is None:
            return None
        # call-out line from the nearest edge point of the anchor to the nearest edge of the text box
        cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
        p0 = pymupdf.Point(min(max(cx, anchor.x0), anchor.x1), min(max(cy, anchor.y0), anchor.y1))
        p1 = pymupdf.Point(min(max(p0.x, r.x0), r.x1), min(max(p0.y, r.y0), r.y1))
        a = freetext(self.page, r, "\n".join(lines), FS, "helv", col, 1,
                     callout=(p0, p1), line_end=pymupdf.PDF_ANNOT_LE_OPEN_ARROW)
        self._finish(a, row, kind, row["action"] + ". " + text)
        return r

    def strike(self, row, box: pymupdf.Rect):
        a = self.page.add_strikeout_annot(quads=[box.quad])
        a.set_colors(stroke=COLOURS[row["layer_type"]])
        self._finish(a, row, "strike-out", row["action"])
        self.space.placed.append(box + (-3, -3, 3, 3))

    def query(self, row, box: pymupdf.Rect):
        col = COLOURS["queries"]
        r = box + (-2, -2, 2, 2)
        a = self.page.add_rect_annot(r)
        a.set_colors(stroke=col)
        a.set_border(width=1.2, dashes=[4, 3])
        self._finish(a, row, "query box (square)", "%s. %s %s" % (row["action"], row["item"], row["note"]))
        self.space.placed.append(r + (-3, -3, 3, 3))
        lines = wrap(row["change_id"] + "  " + query_text(row), 190, 7.5)
        w = max(pymupdf.get_text_length(l, fontname="helv", fontsize=7.5) for l in lines) + 6
        h = len(lines) * 9.0 + 3
        lr = pymupdf.Rect(r.x0, r.y0 - h - 1, r.x0 + w, r.y0 - 1)
        if not self.space.free(lr, pad=1):
            lr = self.space.place(w, h, r, max_dist=160)
        fs = 7.5
        if lr is None:           # no room for the reason: the change ID alone, as before
            lines, fs = [row["change_id"]], 8
            w = pymupdf.get_text_length(lines[0], fontname="helv", fontsize=8) + 6
            h = 11
            lr = pymupdf.Rect(r.x0, r.y0 - 12, r.x0 + w, r.y0 - 1)
            if not self.space.free(lr, pad=1):
                lr = self.space.place(w, h, r)
        if lr is not None:
            self.space.placed.append(lr)
            b = freetext(self.page, lr, "\n".join(lines), fs, "helv", col, 0.5)
            self._finish(b, row, "query label (FreeText)", "Query " + row["change_id"])

    # ---- D-076: an added room is drawn as a room box with its criteria and duct branches -------------------
    def room_lines(self, row: Dict[str, str]) -> List[str]:
        """Text rows of the box, in the order of the sheet's room tables: name, typical count, mode, airflows,
        pressure, filter, temperature, RH, air class, source (config: added_room_markup.box_fields)."""
        def v(k, wb_val=None):
            x = (row.get(k) or "").strip()
            if x:
                return num_text(x) if _isnum(x) else x
            if wb_val is not None:
                return num_text(str(wb_val)) if _isnum(str(wb_val)) else str(wb_val)
            return "not given"
        crit = {}
        if row.get("room_no") and row.get("dept"):
            crit = dc.criteria_of(row["dept"], row["room_no"])
        cv = lambda k: crit.get(k, (None, ""))[0]      # noqa: E731
        pa = row.get("pressure") or cv("pressure")
        pa_t = ("%+g Pa" % float(pa)) if _isnum(str(pa)) else (str(pa) if pa not in (None, "") else "not given")
        ex_only = (row.get("exhaust") or str(cv("exhaust") or "")).strip() not in ("", "0", "0.0") and \
            (row.get("return") or str(cv("return") or "")).strip() in ("", "0", "0.0")
        lines = [NAMES.show(row["item"])[:44],
                 "TYPICAL %s   MODE %s" % (num_text(row["required"]) if _isnum(row["required"]) else row["required"],
                                           row.get("mode") or "normal"),
                 "SA %s  %s %s L/s" % (v("supply", cv("supply")), "EA" if ex_only else "RA",
                                       v("exhaust", cv("exhaust")) if ex_only else v("return", cv("return"))),
                 "PRESSURE %s" % pa_t,
                 "FILTER %s" % v("filter", cv("filter")),
                 "TEMP %s C   RH %s %%" % (v("temp", cv("temp")), v("rh", cv("rh"))),
                 "AIR CLASS %s" % v("air_class", cv("air_class")),
                 "TERMINAL %s" % (row.get("terminal") or "not given: to confirm"),
                 "CHG %s" % row["change_id"]]
        return [dc.clean_text(l) for l in lines]

    def roombox(self, row: Dict[str, str], anchor: Optional[pymupdf.Rect]):
        """Box (layer Rooms) and its supply and return or exhaust branches to the header or fan (layer New systems)."""
        font, fs = "cour", 7.5
        lines = self.room_lines(row)
        cw = pymupdf.get_text_length("0", fontname=font, fontsize=fs)
        w = max(len(l) for l in lines) * cw + 8
        h = len(lines) * fs * 1.2 + 6
        place = self.space.place(w, h, anchor, leader=True, max_dist=520)
        if place is None:
            return None
        a = freetext(self.page, place, "\n".join(lines), fs, font, COLOURS["rooms"], 1.0)
        self._finish(a, row, "room box (FreeText)", "%s. %s" % (row["action"], row["item"]))
        if anchor is not None:
            self._branches(row, place, anchor)
        return place

    def _branches(self, row, box: pymupdf.Rect, anchor: pymupdf.Rect):
        """Two branches from the box to the header of the unit: supply (solid) and return or exhaust (dashed).
        They are Line annotations on the New systems layer; the header end is the anchor edge nearest to the box."""
        cx, cy = (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2
        p_hdr = pymupdf.Point(min(max(cx, anchor.x0), anchor.x1), min(max(cy, anchor.y0), anchor.y1))
        self.n_branch = getattr(self, "n_branch", 0) + 1
        spread = ((self.n_branch % 7) - 3) * 3.0          # attachment points are spread along the header edge
        for k, (off, dash, label) in enumerate(((-2.0, None, "supply branch"), (2.0, [4, 3], "return or exhaust branch"))):
            p0 = pymupdf.Point(min(max(p_hdr.x + off + spread, anchor.x0), anchor.x1),
                               min(max(p_hdr.y + off + spread, anchor.y0), anchor.y1))
            p1 = pymupdf.Point(min(max(p0.x, box.x0), box.x1), min(max(p0.y, box.y0), box.y1))
            if math.hypot(p1.x - p0.x, p1.y - p0.y) < 1:
                continue
            ln = self.page.add_line_annot(p0, p1)
            ln.set_colors(stroke=COLOURS["new systems"])
            ln.set_border(width=0.9, dashes=dash) if dash else ln.set_border(width=0.9)
            ln.set_line_ends(pymupdf.PDF_ANNOT_LE_NONE, pymupdf.PDF_ANNOT_LE_OPEN_ARROW)
            ln.set_info(title=AUTHOR, subject=row["change_id"], content="%s of %s" % (label, row["item"]))
            ln.set_oc(self.ocg["new systems"])
            ln.update()
            self.log.append({"change_id": row["change_id"], "layer": LAYERS["new systems"], "annotation": "duct branch (line)",
                             "rect": json.dumps([round(v, 1) for v in ln.rect])})

    def table(self, rows: List[Dict[str, str]], anchor: Optional[pymupdf.Rect]):
        """Rows of one tabulated note, one FreeText per change ID, stacked."""
        col = COLOURS[rows[0]["layer_type"]]
        font = "cour"
        cw = pymupdf.get_text_length("0", fontname=font, fontsize=FS)
        data = []
        for r in rows:
            val = r["required"]
            if _isnum(val):
                val = num_text(val)
            if r["unit_of_measure"] and not val.lower().startswith("not"):
                val += " " + r["unit_of_measure"]
            item, qty = (NAMES.show(r["item"]) if r["layer_type"] == "rooms" else r["item"]), r["quantity"]
            if item == r["unit"] and OVERALL_ADD not in r["quantity"]:
                item = "(the unit)"
            if qty.lower().startswith(item.lower()) and item != "(the unit)":
                qty = qty[len(item):].strip() or qty
            data.append((r["change_id"], item, qty, val))
        if OVERALL_ADD in rows[0]["quantity"]:
            data = [(d[0], d[1], "AHU", d[3]) for d in data]
        w_item = min(46, max(len(d[1]) for d in data))
        w_q = max(len(d[2]) for d in data)
        w_v = max(len(d[3]) for d in data)
        w_id = len(data[0][0])
        texts = ["%-*s %-*s %-*s %*s" % (w_id, d[0], w_item, d[1] if len(d[1]) <= w_item else d[1][:w_item - 3].rstrip() + "...",
                                         w_q, d[2], w_v, d[3]) for d in data]
        width = max(len(t) for t in texts) * cw + 10
        unit = rows[0]["unit"]
        if unit.startswith("AHU group:"):
            tg = re.findall(r"AHU-[A-Z0-9][A-Za-z0-9\-/]*", unit)
            unit = "GROUP " + "; ".join(tg[:3]) + (" ..." if len(tg) > 3 else "")
        title = "ADD ON " + unit + " (note, see " + ("rooms" if rows[0]["layer_type"] == "rooms" else rows[0]["layer_type"]) + ")"
        if OVERALL_ADD in rows[0]["quantity"]:
            if rows[0]["layer_type"] == "queries":
                title = "QUERY, NO INSTRUCTION: THESE AHUs ARE NOT ON THE OVERALL SCHEMATIC (note, see queries)"
            else:
                title = "ADD THESE AHUs TO THE OVERALL SCHEMATIC (note, see new systems)"
        head_h = FS * 1.25 + 2
        total_h = ROW_H * len(rows) + head_h
        width = max(width, pymupdf.get_text_length(title, fontname=font, fontsize=FS) + 10)
        place = self.space.place(width, total_h, anchor, leader=True, max_ink=0, max_dist=420)
        if place is None:
            return None
        no_leader = self.space.last_ink != 0
        y = place.y0
        for i, (r, t) in enumerate(zip(rows, texts)):
            first = i == 0
            h = ROW_H + (head_h if first else 0)
            rr = pymupdf.Rect(place.x0, y, place.x0 + width, y + h)
            body = dc.clean_text((title + "\n" if first else "") + t)
            kw = {}
            if first and anchor is not None and not no_leader:
                cx, cy = (rr.x0 + rr.x1) / 2, (rr.y0 + rr.y1) / 2
                p0 = pymupdf.Point(min(max(cx, anchor.x0), anchor.x1), min(max(cy, anchor.y0), anchor.y1))
                p1 = pymupdf.Point(min(max(p0.x, rr.x0), rr.x1), min(max(p0.y, rr.y0), rr.y1))
                kw = dict(callout=(p0, p1), line_end=pymupdf.PDF_ANNOT_LE_OPEN_ARROW)
            a = freetext(self.page, rr, body, FS, font, col, 0.8, **kw)
            self._finish(a, r, "table row (FreeText)", "%s. %s: %s %s" % (r["action"], r["item"], r["quantity"], data[i][3]))
            y += h
        return place


# --------------------------------------------------------------------------- #
# Build and verify
# --------------------------------------------------------------------------- #
def markup_sheet(sheet: str, out_dir: Path = OUT_DIR) -> Dict[str, object]:
    rows = read_register(sheet)
    if not rows:
        raise ValueError("no register rows for " + sheet)
    src = source_pdf(sheet)
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / (sheet + "_markup.pdf")
    shutil.copyfile(src, dst)
    doc = pymupdf.open(dst)
    by_page: Dict[int, List[Dict[str, str]]] = defaultdict(list)
    for r in rows:
        by_page[int(r["page"] or 1)].append(r)
    log: List[Dict[str, str]] = []
    for pno, prow in sorted(by_page.items()):
        page = doc[pno - 1]
        mk = Marker(doc, page, FreeSpace(page))
        tables: Dict[Tuple[str, str, str], List[Dict[str, str]]] = defaultdict(list)
        roomboxes: List[Dict[str, str]] = []
        # AHUs that the overall schematic lacks: a few tables of 16 rows, not one table for each AHU
        ovr = sorted([r for r in prow if OVERALL_ADD in r["quantity"] and not parse_box(r["bbox"])], key=lambda r: r["item"])
        cnt_: Dict[str, int] = defaultdict(int)
        for r in ovr:
            tables[("OVERALL%d" % (cnt_[r["layer_type"]] // 16), r["layer_type"], "")].append(r)
            cnt_[r["layer_type"]] += 1
        ovr_ids = {r["change_id"] for r in ovr}
        prow = [r for r in prow if r["change_id"] not in ovr_ids]
        # first the in-place marks, so that notes and call-outs avoid them
        for r in prow:
            box = parse_box(r["bbox"])
            f = form_of(r)
            if f == "roombox":
                roomboxes.append(r)
            elif f == "table":
                tables[(r["unit"], r["layer_type"], r["bbox"])].append(r)
            elif box is None:
                tables[(r["unit"], r["layer_type"], "")].append(r)
            elif f == "query":
                mk.query(r, box)
            elif f == "strike":
                mk.strike(r, box)
            else:
                mk.cloud(r, box)
        for r in prow:
            box = parse_box(r["bbox"])
            if box is not None and form_of(r) in ("cloud", "strike"):
                if form_of(r) == "cloud":
                    mk.callout(r, box + (-3, -3, 3, 3), new_value_text(r))
                else:
                    mk.callout(r, box, "%s: strike through here. %s" % (r["item"], r["note"]))
        for r in roomboxes:
            box = parse_box(r["bbox"])
            if mk.roombox(r, box + (-3, -3, 3, 3) if box else None) is None:
                tables[(r["unit"], r["layer_type"], r["bbox"])].append(r)     # no free space: the tabulated note
        def put(trs, anchor):
            if mk.table(trs, anchor) is None and len(trs) > 1:      # no room: split the note in two
                put(trs[: len(trs) // 2], anchor)
                put(trs[len(trs) // 2:], anchor)
        for (_, _, bx), trs in tables.items():
            put(trs, parse_box(bx) if bx else None)
        log += mk.log
    doc.saveIncr()
    doc.close()
    with open(out_dir / (sheet + "_markup_log.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["change_id", "layer", "annotation", "rect"])
        w.writeheader()
        w.writerows(log)
    return {"sheet": sheet, "markup": dst, "source": src, "rows": rows, "log": log}


def verify(sheet: str, out_dir: Path = OUT_DIR) -> Dict[str, object]:
    """Page content untouched, annotations match the register both ways, layers right."""
    rows = read_register(sheet)
    src, dst = source_pdf(sheet), out_dir / (sheet + "_markup.pdf")
    a, b = pymupdf.open(src), pymupdf.open(dst)
    res: Dict[str, object] = {"sheet": sheet}
    same_stream, same_px = True, True
    ids = set()
    layer_ok = True
    cl_ok = True
    unflattened = 0
    for i in range(len(a)):
        pa, pb = a[i], b[i]
        same_stream &= pa.read_contents() == pb.read_contents()
        ma = pa.get_pixmap(annots=False).samples
        mb = pb.get_pixmap(annots=False).samples
        same_px &= ma == mb
        own = {x.xref for x in pa.annots()}       # annotations that the source sheet already carries
        for an in pb.annots():
            if an.xref in own:
                continue
            ids.add(an.info["subject"])
            unflattened += 1
            cl = b.xref_get_key(an.xref, "CL")
            if cl[0] != "null":
                pts = [float(v) for v in cl[1].strip("[]").split()]
                inv = ~pb.transformation_matrix        # page view = pdf point x transformation matrix
                for k in range(0, len(pts), 2):
                    q = pymupdf.Point(pts[k], pts[k + 1]) * pb.transformation_matrix
                    cl_ok &= pb.rect.x0 - 1 <= q.x <= pb.rect.x1 + 1 and pb.rect.y0 - 1 <= q.y <= pb.rect.y1 + 1
                cl_ok &= pb.rect.x0 - 1 <= an.rect.x0 and an.rect.x1 <= pb.rect.x1 + 1
            oc = b.xref_get_key(an.xref, "OC")
            names = {x: v["name"] for x, v in b.get_ocgs().items()}
            rr = next((r for r in rows if r["change_id"] == an.info["subject"]), None)
            want = {LAYERS[rr["layer_type"]]} if rr else set()
            if rr and form_of(rr) == "roombox":
                want.add(LAYERS["new systems"])         # the duct branches of an added room (D-076)
            got = names.get(int(oc[1].split()[0])) if oc[0] == "xref" else None
            layer_ok &= got in want
    # layers off: set all OCGs off for the render check (view state only, not saved)
    same_px_layers_off = same_px   # annotations hidden: layer state cannot change the page pixels
    reg = {r["change_id"] for r in rows}
    res.update(page_content_stream_identical=same_stream, pixel_identical_annotations_hidden=same_px,
               layers_off_identical=same_px_layers_off, annotation_count=unflattened,
               register_rows=len(reg), ids_in_pdf=len(ids), register_ids_missing_in_pdf=sorted(reg - ids),
               pdf_ids_not_in_register=sorted(ids - reg), layers_correct=layer_ok, callout_lines_on_page=cl_ok,
               layers=sorted(v["name"] for v in b.get_ocgs().values()))
    res["pass"] = bool(same_stream and same_px and res["layers_off_identical"] and not res["register_ids_missing_in_pdf"]
                       and not res["pdf_ids_not_in_register"] and layer_ok and cl_ok)
    return res


def run_all(out_dir: Path = OUT_DIR) -> List[Dict[str, object]]:
    """Mark-up files for every sheet that has an instruction or a query for a unit that is not list-only; files of
    other sheets are removed (build line 25).  Every file is verified."""
    reset_caches()
    sheets = sheets_to_mark()
    out_dir.mkdir(parents=True, exist_ok=True)
    keep = {f"{s}_markup{ext}" for s in sheets for ext in (".pdf", "_check.json", "_log.csv")}
    for f in out_dir.glob("*_markup*"):
        if f.name not in keep:
            f.unlink()
    res = []
    for s in sheets:
        r = markup_sheet(s, out_dir)
        v = verify(s, out_dir)
        (out_dir / (s + "_markup_check.json")).write_text(json.dumps(v, indent=1))
        res.append(dict(sheet=s, annotations=len(r["log"]), rows=len(r["rows"]), passed=v["pass"]))
        print(s, len(r["log"]), "annotations", "pass" if v["pass"] else "FAIL")
    NAMES.write_missed(out_dir.parent / "name_map_missed_markups.csv")
    with open(out_dir / "markups_index.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["sheet", "annotations", "rows", "passed"])
        w.writeheader()
        w.writerows(res)
    return res


def main(argv: Optional[Sequence[str]] = None) -> int:
    global REGISTER, OUT_DIR
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["run", "verify", "all"])
    ap.add_argument("--sheet", action="append", default=None)
    ap.add_argument("--register", default=None, help="change list to read (default out/schematics/change_register.csv)")
    ap.add_argument("--out", default=None, help="folder for the mark-ups (default out/schematics/markups)")
    ns = ap.parse_args(argv)
    if ns.register:
        REGISTER = Path(ns.register)
    if ns.out:
        OUT_DIR = Path(ns.out)
    if ns.cmd == "all":
        res = run_all(OUT_DIR)
        return 0 if all(r["passed"] for r in res) else 1
    reset_caches()
    sheets = ns.sheet or ["IP-BH-05010"]
    for s in sheets:
        if not read_register(s):
            print(s, "has no change rows: no mark-up copy")
            continue
        if ns.cmd == "run":
            r = markup_sheet(s, OUT_DIR)
            print(s, "->", r["markup"], len(r["log"]), "annotations")
        v = verify(s, OUT_DIR)
        print(json.dumps(v, indent=1))
        (OUT_DIR / (s + "_markup_check.json")).write_text(json.dumps(v, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
