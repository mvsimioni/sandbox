"""Room tags of the architectural part plans (1:50, 26 Nov 2026 issue, data/uploads/arch_20261126/IP_part) placed on the zoning background.

A part-plan tag reads: Revit room number (e.g. 06.IP.LL.001), short name (ISO LL), AREA = n, ventilation mode (MV, NV, AC (24HR) ...) and the
template code (806_LIFT-ILL). The tags are read from the text layer. Each sheet is placed on the background storey page (zone plan, 1:250) by
the short names that both carry: background = part / 5 + t, t by vote over all same-text pairs, then a least-squares fit of scale and offset on
the pairs that agree (inliers within 3 pt). All words of both sheets take part in the vote (short names, grid labels, lift and
stair codes; words that occur very often are left out). Sheets with fewer than 8 inliers are not placed (listed).

``python3 -m src.arch.part_plans`` writes out/registers/arch_part_tags.csv.
"""
from __future__ import annotations

import csv
import glob
import re
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pymupdf

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "data/uploads/arch_20261126/IP_part"
OUT = ROOT / "out/registers/arch_part_tags.csv"
NUM = re.compile(r"^(\d{2}|B[123]|LB3|L?B\d)\.IP\.[A-Z0-9]+\.\d{3}$")
STOREY = {"LB3": "LB3", "B3": "B3", "B2": "B2", "B1": "B1", "01": "L01", "02": "L02", "03": "L03", "04": "L04", "05": "L05", "06": "L06", "07": "L07"}
MODES = ("MV", "NV", "AC", "ACMV", "(24HR)", "AC(24HR)", "N/A", "NA")


def _derot(page, x, y):
    p = pymupdf.Point(x, y) * page.rotation_matrix
    return p.x, p.y


def tags(page):
    ws = page.get_text("words")
    out = []
    for w in ws:
        if not NUM.match(w[4]):
            continue
        # the short name is the next text column after the number (same block on the storeys, own block in the basements)
        col = [x for x in ws if w[2] - 1 <= x[0] <= w[2] + 12 and x[1] >= w[1] - 14 and x[3] <= w[3] + 2 and x[4] not in ("AREA", "=", "m²")
               and not re.fullmatch(r"[\d.]+", x[4])]
        blocks = {x[5] for x in col}
        blk = [x for x in ws if x[5] in blocks and x is not w and x[4] != w[4]]
        dv = lambda x: _derot(page, (x[0] + x[2]) / 2, (x[1] + x[3]) / 2)
        name = " ".join(x[4] for x in sorted(blk, key=lambda x: (round(dv(x)[1] / 3), dv(x)[0])))
        # the tag reads along x of the unrotated page; collect the words in the tag box
        box = [x for x in ws if w[0] - 2 <= x[0] and x[2] <= w[0] + 80 and x[1] >= w[1] - 12 and x[3] <= w[3] + 4 and x[5] != w[5]]
        area = None
        for x in box:
            if re.fullmatch(r"\d+(\.\d+)?", x[4]) and any(y[4] == "AREA" and y[5] == x[5] for y in box):
                area = float(x[4])
        code = next((x[4] for x in box if "_" in x[4] and re.search(r"\d", x[4])), "")
        abl = {x[5] for x in box if x[4] == "AREA"}
        mw = {x[4] for x in box if x[5] not in abl and "_" not in x[4] and x[4] != "AREA"}
        mode = ("NV" if "NV" in mw else "MV" if "MV" in mw else "AC (24HR)" if "AC" in mw and "(24HR)" in mw else "AC" if "AC" in mw
                else "N/A" if mw & {"NA", "N/A"} else "")
        cx, cy = _derot(page, (w[0] + w[2]) / 2, (w[1] + w[3]) / 2)
        out.append(dict(number=w[4], name=name, area_m2=area, mode=mode, template=code, x=cx, y=cy))
    return out


def _bg_words(st):
    from .. import common
    from ..zoning.storeys import BG_PDF, storey_info
    page = pymupdf.open(str(common.project_root() / BG_PDF))[storey_info(st)["page0"]]
    res = defaultdict(list)
    for w in page.get_text("words"):
        x, y = _derot(page, (w[0] + w[2]) / 2, (w[1] + w[3]) / 2)
        res[w[4]].append((x, y))
    return res


def place(page, tg, bgw):
    """(scale, tx, ty, inliers) placing part-plan page points on the background page view."""
    pw = defaultdict(list)
    for w in page.get_text("words"):
        x, y = _derot(page, (w[0] + w[2]) / 2, (w[1] + w[3]) / 2)
        pw[w[4]].append((x, y))
    pairs = []
    for tok, pts in pw.items():
        if len(tok) < 2 or tok not in bgw or re.fullmatch(r"[\d.,]+", tok) and len(tok) < 2:
            continue
        bpts = bgw[tok]
        if len(pts) * len(bpts) > 4000:           # very common words (MV, B) add only noise
            continue
        for a in pts:
            for b in bpts:
                pairs.append((a, b))
    if not pairs:
        return None
    votes = Counter((round(b[0] - a[0] / 5), round(b[1] - a[1] / 5)) for a, b in pairs)
    # smooth the vote over 3 x 3 bins
    best, bv = None, -1
    for (tx, ty), _ in votes.most_common(60):
        v = sum(votes.get((tx + i, ty + j), 0) for i in (-1, 0, 1) for j in (-1, 0, 1))
        if v > bv:
            best, bv = (tx, ty), v
    tx, ty = best
    inl = [(a, b) for a, b in pairs if abs(b[0] - (a[0] / 5 + tx)) < 3 and abs(b[1] - (a[1] / 5 + ty)) < 3]
    if len(inl) < 8:
        return dict(scale=0.2, tx=tx, ty=ty, inliers=len(inl), placed=False)
    A = np.array([[a[0], 1, 0] for a, _ in inl] + [[a[1], 0, 1] for a, _ in inl], float)
    y = np.array([b[0] for _, b in inl] + [b[1] for _, b in inl], float)
    (s, tx, ty), *_ = np.linalg.lstsq(A, y, rcond=None)
    res = np.abs(A @ np.array([s, tx, ty]) - y)
    return dict(scale=float(s), tx=float(tx), ty=float(ty), inliers=len(inl), placed=True, resid=float(np.median(res)))


def run():
    rows, sheets = [], []
    bg_cache = {}
    for f in sorted(glob.glob(str(SRC / "*.pdf"))):
        m = re.search(r"AR-IP-02-(LB3|B3|B2|B1|0\d)-(\d{3})", f)
        st = STOREY[m.group(1)]
        page = pymupdf.open(f)[0]
        tg = tags(page)
        bgw = bg_cache.setdefault(st, _bg_words(st))
        pl = place(page, tg, bgw)
        sheets.append(dict(sheet=Path(f).name, storey=st, tags=len(tg), **(pl or {"placed": False})))
        for t in tg:
            r = dict(storey=st, sheet=Path(f).name, **t)
            if pl and pl.get("placed"):
                r["bg_x"] = round(t["x"] * pl["scale"] + pl["tx"], 2)
                r["bg_y"] = round(t["y"] * pl["scale"] + pl["ty"], 2)
            rows.append(r)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    cols = ["storey", "sheet", "number", "name", "area_m2", "mode", "template", "x", "y", "bg_x", "bg_y"]
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    with (OUT.parent / "arch_part_sheets.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["sheet", "storey", "tags", "placed", "inliers", "scale", "tx", "ty", "resid"], extrasaction="ignore")
        w.writeheader()
        w.writerows(sheets)
    return rows, sheets


if __name__ == "__main__":
    rows, sheets = run()
    print(len(rows), "tags;", sum(1 for s in sheets if s.get("placed")), "of", len(sheets), "sheets placed")
    for s in sheets:
        print(s["sheet"][:22], s["storey"], s["tags"], s.get("placed"), s.get("inliers"), round(s.get("scale", 0), 4), round(s.get("resid", 9), 2) if s.get("resid") is not None else "")
