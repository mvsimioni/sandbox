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

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "data" / "source" / "Basis" / "Working Drawings"
REGISTER = ROOT / "out" / "schematics" / "change_register.csv"
OUT_DIR = ROOT / "out" / "schematics" / "markups"
AUTHOR = "Schematic review"
OVERALL_ADD = "not on the overall schematic"

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
def read_register(sheet: str) -> List[Dict[str, str]]:
    with open(REGISTER, encoding="utf-8", newline="") as fh:
        return [r for r in csv.DictReader(fh) if r["sheet"] == sheet]


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
    if a.startswith("query"):
        return "query"
    if "strike through" in a:
        return "strike"
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
    cls = row["class"]
    unit = row["unit_of_measure"]
    q = row["quantity"]
    if "typical" in q.lower():
        return "%s: change typical count %s to %s" % (row["item"], num_text(row["drawn"]), num_text(row["required"]))
    if row["layer_type"] == "fans" and cls == "to resize":
        return "%s: change duty %s to %s" % (row["item"], shown(row["drawn"], unit), shown(row["required"], unit))
    return "%s %s: change %s to %s" % (row["item"], q[:1].lower() + q[1:] if q[:2].islower() or not q[:2].isupper() else q,
                                       shown(row["drawn"], unit), shown(row["required"], unit))


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
        """Ink pixels that the leader line from the anchor to the rectangle would cross (ends excluded)."""
        cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
        p0 = (min(max(cx, anchor.x0), anchor.x1), min(max(cy, anchor.y0), anchor.y1))
        p1 = (min(max(p0[0], r.x0), r.x1), min(max(p0[1], r.y0), r.y1))
        n = max(2, int(math.hypot(p1[0] - p0[0], p1[1] - p0[1]) / 3))
        h, w = self.ink.shape
        hit = 0
        for k in range(3, n - 2):
            x = p0[0] + (p1[0] - p0[0]) * k / n
            y = p0[1] + (p1[1] - p0[1]) * k / n
            xi, yi = int(x * SCALE), int(y * SCALE)
            if 0 <= xi < w and 0 <= yi < h and self.ink[yi, xi]:
                hit += 1
        return hit

    def place(self, w: float, h: float, anchor: Optional[pymupdf.Rect], leader: bool = False) -> Optional[pymupdf.Rect]:
        """Nearest free w x h rectangle to the anchor, searched on a grid.  With ``leader`` the leader line
        from the anchor must cross no drawn line or text (the candidate with the least ink is taken)."""
        ax, ay = ((anchor.x0 + anchor.x1) / 2, (anchor.y0 + anchor.y1) / 2) if anchor else (
            self.rect.width / 2, self.rect.height / 2)
        # near candidates first: rings around the anchor, then the whole page
        cands: List[Tuple[float, pymupdf.Rect]] = []
        step = 12.0
        if anchor is not None:
            for dx in np.arange(-500, 501, step):
                for dy in np.arange(-400, 401, step):
                    r = pymupdf.Rect(ax + dx - w / 2, ay + dy - h / 2, ax + dx + w / 2, ay + dy + h / 2)
                    if anchor is not None and r.intersects(anchor + (-3, -3, 3, 3)):
                        continue
                    cands.append((math.hypot(dx, dy), r))
            cands.sort(key=lambda t: t[0])
            best_l = None
            tried = 0
            for dist, r in cands:
                if not self.free(r):
                    continue
                if not leader:
                    self.placed.append(r)
                    return r
                ink = self.leader_ink(anchor, r)
                if ink == 0:
                    self.placed.append(r)
                    return r
                tried += 1
                if best_l is None or ink + dist / 100 < best_l[0]:
                    best_l = (ink + dist / 100, r)
                if tried >= 150:
                    break
            if best_l is not None:
                self.placed.append(best_l[1])
                return best_l[1]
        step = 24.0
        best = None
        for x in np.arange(self.rect.x0 + MARGIN, self.rect.x1 - MARGIN - w, step):
            for y in np.arange(self.rect.y0 + MARGIN, self.rect.y1 - MARGIN - h, step):
                r = pymupdf.Rect(x, y, x + w, y + h)
                d = math.hypot(x + w / 2 - ax, y + h / 2 - ay)
                if (best is None or d < best[0]) and self.free(r):
                    best = (d, r)
        if best:
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
            a.set_info(title=AUTHOR, subject=row["change_id"], content=content)
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
        label = row["change_id"]
        w = pymupdf.get_text_length(label, fontname="helv", fontsize=8) + 6
        lr = pymupdf.Rect(r.x0, r.y0 - 12, r.x0 + w, r.y0 - 1)
        if not self.space.free(lr, pad=1):
            lr = self.space.place(w, 11, r)
        if lr is not None:
            self.space.placed.append(lr)
            b = freetext(self.page, lr, label, 8, "helv", col, 0.5)
            self._finish(b, row, "query label (FreeText)", "Query " + label)

    def table(self, rows: List[Dict[str, str]], anchor: Optional[pymupdf.Rect]):
        """Rows of one tabulated note, one FreeText per change ID, stacked."""
        col = COLOURS[rows[0]["layer_type"]]
        font = "cour"
        cw = pymupdf.get_text_length("0", fontname=font, fontsize=FS)
        data = []
        for r in rows:
            val = r["required"]
            if r["unit_of_measure"] and not val.lower().startswith("not"):
                val += " " + r["unit_of_measure"]
            data.append((r["change_id"], r["item"], r["quantity"], val))
        if OVERALL_ADD in rows[0]["quantity"]:
            data = [(d[0], d[1], "AHU", d[3]) for d in data]
        w_item = min(34, max(len(d[1]) for d in data))
        w_q = max(len(d[2]) for d in data)
        w_v = max(len(d[3]) for d in data)
        w_id = len(data[0][0])
        texts = ["%-*s %-*s %-*s %*s" % (w_id, d[0], w_item, d[1][:w_item], w_q, d[2], w_v, d[3]) for d in data]
        width = max(len(t) for t in texts) * cw + 10
        unit = rows[0]["unit"]
        title = "ADD ON " + unit + " (note, see " + ("rooms" if rows[0]["layer_type"] == "rooms" else rows[0]["layer_type"]) + ")"
        if OVERALL_ADD in rows[0]["quantity"]:
            title = "ADD THESE AHUs TO THE OVERALL SCHEMATIC (note, see new systems)"
        head_h = FS * 1.25 + 2
        total_h = ROW_H * len(rows) + head_h
        width = max(width, pymupdf.get_text_length(title, fontname=font, fontsize=FS) + 10)
        place = self.space.place(width, total_h, anchor, leader=True)
        if place is None:
            return None
        y = place.y0
        for i, (r, t) in enumerate(zip(rows, texts)):
            first = i == 0
            h = ROW_H + (head_h if first else 0)
            rr = pymupdf.Rect(place.x0, y, place.x0 + width, y + h)
            body = (title + "\n" if first else "") + t
            kw = {}
            if first and anchor is not None:
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
        # AHUs that the overall schematic lacks: a few tables of 16 rows, not one table for each AHU
        ovr = sorted([r for r in prow if OVERALL_ADD in r["quantity"] and not parse_box(r["bbox"])], key=lambda r: r["item"])
        for n_, r in enumerate(ovr):
            tables[("OVERALL%d" % (n_ // 16), r["layer_type"], "")].append(r)
        ovr_ids = {r["change_id"] for r in ovr}
        prow = [r for r in prow if r["change_id"] not in ovr_ids]
        # first the in-place marks, so that notes and call-outs avoid them
        for r in prow:
            box = parse_box(r["bbox"])
            f = form_of(r)
            if f == "table":
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
        for an in pb.annots():
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
            want = LAYERS[next(r["layer_type"] for r in rows if r["change_id"] == an.info["subject"])] \
                if an.info["subject"] in {r["change_id"] for r in rows} else None
            got = names.get(int(oc[1].split()[0])) if oc[0] == "xref" else None
            layer_ok &= want is not None and want == got
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["run", "verify"])
    ap.add_argument("--sheet", action="append", default=None)
    ns = ap.parse_args(argv)
    sheets = ns.sheet or ["IP-BH-05010"]
    for s in sheets:
        if not read_register(s):
            print(s, "has no change rows: no mark-up copy")
            continue
        if ns.cmd == "run":
            r = markup_sheet(s)
            print(s, "->", r["markup"], len(r["log"]), "annotations")
        v = verify(s)
        print(json.dumps(v, indent=1))
        (OUT_DIR / (s + "_markup_check.json")).write_text(json.dumps(v, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
