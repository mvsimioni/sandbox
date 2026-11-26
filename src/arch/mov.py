"""Room tags of the Mode of Ventilation plans (AR-IP-MOV-xx-001, working drawings May 2026, 1:250; data/uploads/mov_20261126), placed on the
zoning background (D-108).

A MOV tag reads (horizontal text): mode below the area line (on a few tags above the number) (AC (24HR), AC, MV, NV, NA or '-'), Revit room number, short name, AREA = n. The room
is also filled in the colour of its mode (legend 'MODE OF VENTILATION' on the sheet). The mode is read from the tag text; where the tag has
no mode word, from the fill colour under the tag (legend swatch nearest in colour).

Each sheet is placed on the background storey page: same scale (1:250), offset by vote over all words both sheets carry, then a least-squares
fit of scale and offset on the pairs that agree (within 3 pt).

``python3 -m src.arch.mov`` writes out/registers/arch_mov_tags.csv (same columns as arch_part_tags.csv, plus mode_source).
"""
from __future__ import annotations

import csv
import glob
import re
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pymupdf

from .part_plans import _bg_words, _derot

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "data/uploads/mov_20261126"
OUT = ROOT / "out/registers/arch_mov_tags.csv"
NUM = re.compile(r"^(\d{2}|B[123]|LB3)\.IP\.[A-Z0-9]+\.\d{3}$")
STOREY = {"LB3": "LB3", "B3": "B3", "B2": "B2", "B1": "B1", "01": "L01", "02": "L02", "03": "L03", "04": "L04", "05": "L05", "06": "L06", "07": "L07"}
LEGEND = ("-", "AC (24HR)", "MV", "NA", "NV")


def _legend(page):
    """legend label -> fill colour of its swatch."""
    ws = page.get_text("words")
    head = [w for w in ws if w[4] == "VENTILATION"]
    if not head:
        return {}
    h = head[-1]
    box = pymupdf.Rect(h[0] - 40, h[3], h[2] + 80, h[3] + 90)
    fills = [d for d in page.get_drawings() if d.get("fill") and box.contains(d["rect"]) and d["rect"].width < 20 and d["rect"].height < 12]
    lines = defaultdict(list)
    for w in ws:
        if box.contains(pymupdf.Rect(w[:4])) and w[4] not in ("MODE", "OF", "VENTILATION"):
            lines[round(w[1])].append(w)
    out = {}
    for y, wl in lines.items():
        label = " ".join(x[4] for x in sorted(wl, key=lambda x: x[0]))
        sw = [d for d in fills if abs((d["rect"].y0 + d["rect"].y1) / 2 - (wl[0][1] + wl[0][3]) / 2) < 4]
        if sw:
            out[label] = tuple(sw[0]["fill"])
    return out


def _fill_at(fills, x, y):
    best = None
    for r, c in fills:
        if r.x0 <= x <= r.x1 and r.y0 <= y <= r.y1:
            a = r.width * r.height
            if best is None or a < best[0]:
                best = (a, c)
    return best[1] if best else None


def tags(page):
    ws = page.get_text("words")
    leg = _legend(page)
    fills = [(d["rect"], tuple(d["fill"])) for d in page.get_drawings()
             if d.get("fill") and tuple(round(c, 2) for c in d["fill"]) not in ((1.0, 1.0, 1.0),) and d["rect"].width > 15 and d["rect"].height > 15]
    out = []
    for w in ws:
        if not NUM.match(w[4]):
            continue
        name = " ".join(x[4] for x in ws if x[5] == w[5] and x[6] > w[6])
        area = None
        below = [x for x in ws if abs(x[0] - w[0]) < 25 and w[3] < x[1] < w[3] + 16 and x[5] != w[5]]
        nums = [x[4] for x in below if re.fullmatch(r"\d+(\.\d+)?", x[4])]
        if nums and any(x[4] == "AREA" for x in below):
            area = float(nums[0])
        above = [x for x in ws if w[0] - 4 <= x[0] <= w[2] + 4 and x[5] != w[5]
                 and (w[1] - 14 <= x[3] <= w[1] + 1 or w[1] + 13 <= x[1] <= w[1] + 24)]
        mw = {x[4] for x in above}
        mode = ("NV" if "NV" in mw else "MV" if "MV" in mw else "AC (24HR)" if "AC" in mw and "(24HR)" in mw else "AC" if "AC" in mw
                else "N/A" if mw & {"NA", "N/A"} else "")
        src = "tag text" if mode else ""
        cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
        if not mode and leg:
            c = _fill_at(fills, cx, cy)
            if c is not None:
                lab = min(leg, key=lambda k: sum((a - b) ** 2 for a, b in zip(leg[k], c)))
                if sum((a - b) ** 2 for a, b in zip(leg[lab], c)) < 0.01:
                    mode = {"AC (24HR)": "AC (24HR)", "MV": "MV", "NV": "NV", "NA": "N/A", "-": ""}[lab]
                    src = "fill colour" if mode else ""
        x, y = _derot(page, cx, cy)
        out.append(dict(number=w[4], name=name, area_m2=area, mode=mode, mode_source=src, x=x, y=y))
    return out


def place(page, bgw):
    pw = defaultdict(list)
    for w in page.get_text("words"):
        pw[w[4]].append(_derot(page, (w[0] + w[2]) / 2, (w[1] + w[3]) / 2))
    pairs = []
    for tok, pts in pw.items():
        if len(tok) < 2 or tok not in bgw or len(pts) * len(bgw[tok]) > 4000:
            continue
        for a in pts:
            for b in bgw[tok]:
                pairs.append((a, b))
    if not pairs:
        return dict(placed=False, inliers=0)
    votes = Counter((round(b[0] - a[0]), round(b[1] - a[1])) for a, b in pairs)
    best, bv = None, -1
    for (tx, ty), _ in votes.most_common(80):
        v = sum(votes.get((tx + i, ty + j), 0) for i in (-1, 0, 1) for j in (-1, 0, 1))
        if v > bv:
            best, bv = (tx, ty), v
    tx, ty = best
    inl = [(a, b) for a, b in pairs if abs(b[0] - (a[0] + tx)) < 3 and abs(b[1] - (a[1] + ty)) < 3]
    if len(inl) < 20:
        return dict(placed=False, inliers=len(inl), scale=1.0, tx=tx, ty=ty)
    A = np.array([[a[0], 1, 0] for a, _ in inl] + [[a[1], 0, 1] for a, _ in inl], float)
    y = np.array([b[0] for _, b in inl] + [b[1] for _, b in inl], float)
    (s, tx, ty), *_ = np.linalg.lstsq(A, y, rcond=None)
    res = np.abs(A @ np.array([s, tx, ty]) - y)
    return dict(placed=True, inliers=len(inl), scale=float(s), tx=float(tx), ty=float(ty), resid=float(np.median(res)))


def run():
    rows, sheets = [], []
    for f in sorted(glob.glob(str(SRC / "*.pdf"))):
        m = re.search(r"AR-IP-MOV-(\w+)-001", f)
        st = STOREY.get(m.group(1))
        if not st:
            continue
        page = pymupdf.open(f)[0]
        tg = tags(page)
        pl = place(page, _bg_words(st))
        sheets.append(dict(sheet=Path(f).name, storey=st, tags=len(tg), **pl))
        for t in tg:
            r = dict(storey=st, sheet=Path(f).name, template="", **t)
            if pl.get("placed"):
                r["bg_x"] = round(t["x"] * pl["scale"] + pl["tx"], 2)
                r["bg_y"] = round(t["y"] * pl["scale"] + pl["ty"], 2)
            rows.append(r)
    cols = ["storey", "sheet", "number", "name", "area_m2", "mode", "mode_source", "template", "x", "y", "bg_x", "bg_y"]
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return rows, sheets


if __name__ == "__main__":
    rows, sheets = run()
    for s in sheets:
        print(s["sheet"], s["storey"], s["tags"], s.get("placed"), s.get("inliers"), round(s.get("scale", 0), 4), round(s.get("resid", 9), 2) if s.get("resid") is not None else "")
    print(len(rows), "tags;", Counter((r["mode"], r["mode_source"]) for r in rows).most_common())
