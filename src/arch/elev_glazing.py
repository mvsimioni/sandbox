"""Glazing measured on the building elevations (D-120, forensic envelope review, 26 Nov 2026).

Source: data/uploads/arch_20261126/IP_elev (AR-IP-31-ZZ-001 to 008, external and internal elevations, 1:250, vector).
On these sheets a glass panel is a light-grey filled shape (fill 0.87); louvres are dense narrow slats with the same fill; spandrel or
opaque glass is the same fill with a diagonal hatch; opening lights carry a red triangle. A glass panel here is a light-grey shape at least
0.30 m in both directions with no diagonal hatch inside; a hatched light-grey shape is counted as opaque glass (not glazing).

Axes: the grid bubbles along the top of the sheet fitted to the grid model (out/registers/grid_model.json, building mm) give the horizontal
axis; the FFL labels fitted to their values give the vertical axis. Each storey band runs from its FFL to the FFL of the storey above
(IP levels of the sections, D-119). Results per elevation, storey and grid bay: out/registers/elev_glazing_bays.csv; per elevation and
storey an overlay image for the review: out/qa/envelope_review/overlay_<elev>_<storey>.png.
"""
from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from pathlib import Path

import numpy as np
import pymupdf
import shapely
from shapely.geometry import box

ROOT = Path(__file__).resolve().parent.parent.parent
ELEV = ROOT / "data/uploads/arch_20261126/IP_elev"
REG = ROOT / "out/registers"
QA = ROOT / "out/qa/envelope_review"
FFL = {"LB3": 0.0, "B3": 3.8, "B2": 8.3, "B1": 12.8, "L01": 17.0, "L02": 23.0, "L03": 29.0, "L04": 35.0, "L05": 41.0, "L06": 47.0,
       "L07": 53.0, "L08": 59.0}
STOREYS = ["LB3", "B3", "B2", "B1", "L01", "L02", "L03", "L04", "L05", "L06", "L07"]
GLASS = (0.87, 0.87, 0.87)
MIN_PANEL_M = 0.30
BIN = 100.0              # mm, glass profile bin along the grid axis
PROFILES = {}
SLATS = {}


def grid_mm():
    g = json.loads((REG / "grid_model.json").read_text())
    ax = lambda k: {x["label"]: float(x["offset_mm"]) for x in eval(g[k])["grids"]} if isinstance(g[k], str) else \
        {x["label"]: float(x["offset_mm"]) for x in g[k]["grids"]}
    return ax("x_axis"), ax("y_axis")


def _words(p):
    M = p.rotation_matrix
    for w in p.get_text("words"):
        yield w[4], pymupdf.Rect(w[:4]) * M


def axes(p):
    """(axis name 'X' or 'Y', fit x_pt = a + b * mm, fit y_pt = c + d * FFL m, grid label positions)."""
    gx, gy = grid_mm()
    top = [(t, r) for t, r in _words(p) if r.y0 < 340 and r.x1 < 2450]
    lab = {}
    for t, r in top:
        if t in gx or t in gy:
            lab.setdefault(t, (r.x0 + r.x1) / 2)
    nx = sum(1 for t in lab if t in gx and not t.isalpha())
    ny = sum(1 for t in lab if t in gy and t.isalpha())
    axis, gm = ("X", gx) if nx >= ny else ("Y", gy)
    pts = [(gm[t], x) for t, x in lab.items() if t in gm and (t.isdigit() if axis == "X" else t.isalpha())]
    A = np.array(pts)
    b, a = np.polyfit(A[:, 0], A[:, 1], 1)
    res = A[:, 1] - (a + b * A[:, 0])
    keep = np.abs(res) < 3
    b, a = np.polyfit(A[keep, 0], A[keep, 1], 1)
    lv = []
    ws = list(_words(p))
    for i, (t, r) in enumerate(ws):
        if t == "FFL":
            nxt = [(t2, r2) for t2, r2 in ws if abs(r2.y0 - r.y0) < 1.5 and 0 < r2.x0 - r.x1 < 20 and re.fullmatch(r"\d+\.\d\d", t2)]
            if nxt:
                lv.append((float(nxt[0][0]), (r.y0 + r.y1) / 2))
    L = np.array([v for v in lv if v[0] in FFL.values()])
    d, c = np.polyfit(L[:, 0], L[:, 1], 1)
    return axis, (a, b), (c, d), {t: x for t, x in lab.items() if t in gm}, float(np.max(np.abs(L[:, 1] - (c + d * L[:, 0]))))


def panels(p):
    """Light-grey shapes in page view; diagonal-hatched ones flagged."""
    M = p.rotation_matrix
    glass, diag, red = [], [], []
    for d in p.get_drawings():
        f = d.get("fill")
        if f is not None and tuple(round(x, 2) for x in f) == (1.0, 0.0, 0.0):       # opening-light triangle: the panel is glass
            r = d["rect"] * M
            red.append(shapely.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2))
        if f is not None and tuple(round(x, 2) for x in f) == GLASS:
            r = d["rect"] * M
            r.normalize()
            glass.append(r)
        elif d.get("fill") is None and d.get("color") is not None:
            for it in d["items"]:
                if it[0] == "l":
                    p1, p2 = it[1] * M, it[2] * M
                    dx, dy = abs(p2.x - p1.x), abs(p2.y - p1.y)
                    if dx > 0.4 and dy > 0.4 and 0.3 < dx / dy < 3:
                        sgn = 1 if (p2.x - p1.x) * (p2.y - p1.y) > 0 else -1
                        diag.append(((p1.x + p2.x) / 2, (p1.y + p2.y) / 2, sgn))
    return glass, diag, red


def run(write_overlays: bool = True):
    QA.mkdir(parents=True, exist_ok=True)
    rows, meta = [], []
    for f in sorted(ELEV.glob("*.pdf")):
        name = re.sub(r"^AR-IP-31-ZZ-\d+-|_REV.*$", "", f.stem)
        p = pymupdf.open(f)[0]
        axis, (a, b), (c, d), lab, lres = axes(p)
        pt_per_m = abs(b) * 1000
        glass, diag, red = panels(p)
        tree_r = shapely.STRtree(red) if red else None
        dpts = [shapely.Point(x, y) for x, y, _ in diag]
        dsgn = np.array([sg for _, _, sg in diag])
        tree_d = shapely.STRtree(dpts) if diag else None
        ok, hatched, slats = [], [], []
        for r in glass:
            if r.width / pt_per_m < MIN_PANEL_M or r.height / pt_per_m < MIN_PANEL_M:
                if r.width * r.height > 0.05:
                    slats.append(box(r.x0, r.y0, r.x1, r.y1).buffer(0.06 * pt_per_m, join_style="mitre"))
                continue
            g = box(r.x0, r.y0, r.x1, r.y1)
            idx = tree_d.query(g.buffer(-0.3)) if tree_d is not None and g.area > 1 else []
            # a hatch is several parallel diagonals; the glass tick (one or two short strokes) and the opening triangle (two directions) are not
            nd = max(int((dsgn[idx] > 0).sum()), int((dsgn[idx] < 0).sum())) if len(idx) else 0
            opening = tree_r is not None and len(tree_r.query(g, predicate="contains")) > 0
            (hatched if nd >= 4 and not opening else ok).append(g)
        G, H = shapely.union_all(ok), shapely.union_all(hatched)
        # slatted regions: narrow light-grey slats merged (louvres, or vertical fins in front of glass; decided in the review, D-120)
        SL = shapely.union_all(slats).buffer(-0.06 * pt_per_m, join_style="mitre").difference(G) if slats else shapely.Polygon()
        SL = shapely.union_all([q for q in getattr(SL, "geoms", [SL]) if q.area > (0.5 * pt_per_m) ** 2]) if not SL.is_empty else SL
        labels = sorted(lab.items(), key=lambda kv: kv[1])
        mm = lambda x: (x - a) / b
        meta.append(dict(elevation=name, axis=axis, pt_per_m=round(pt_per_m, 3), level_fit_residual_pt=round(lres, 2), grids=len(labels),
                         glass_panels=len(ok), hatched_panels=len(hatched)))
        xs_pt = sorted(x for _, x in labels)
        m_lo, m_hi = sorted((mm(xs_pt[0] - 200), mm(xs_pt[-1] + 200)))
        bins = np.arange(np.floor(m_lo / BIN) * BIN, m_hi + BIN, BIN)
        for st in STOREYS:
            nxt = STOREYS[STOREYS.index(st) + 1] if st != "L07" else "L08"
            y_lo, y_hi = sorted((c + d * FFL[st], c + d * FFL[nxt]))
            band = box(0, y_lo, p.rect.width, y_hi)
            gb, hb = G.intersection(band), H.intersection(band)
            sb = SL.intersection(band) if not SL.is_empty else SL
            if not sb.is_empty:
                sprof = np.zeros(len(bins) - 1)
                stree = shapely.STRtree([q for q in getattr(sb, "geoms", [sb]) if q.area > 0])
                for i in range(len(bins) - 1):
                    xa, xb = sorted((a + b * bins[i], a + b * bins[i + 1]))
                    cell = box(xa, y_lo, xb, y_hi)
                    sprof[i] = sum(stree.geometries[j].intersection(cell).area for j in stree.query(cell)) / pt_per_m ** 2 / (BIN / 1000.0)
                SLATS[(name, st)] = dict(axis=axis, bins=bins, slat_m2_per_m=sprof)
            if not gb.is_empty:
                # glass per metre of facade along the grid axis (m2 per m), 100 mm bins in building mm
                prof = np.zeros(len(bins) - 1)
                tree = shapely.STRtree([q for q in getattr(gb, "geoms", [gb]) if q.area > 0])
                for i in range(len(bins) - 1):
                    xa, xb = sorted((a + b * bins[i], a + b * bins[i + 1]))
                    cell = box(xa, y_lo, xb, y_hi)
                    ar = sum(tree.geometries[j].intersection(cell).area for j in tree.query(cell))
                    prof[i] = ar / pt_per_m ** 2 / (BIN / 1000.0)
                PROFILES[(name, st)] = dict(axis=axis, bins=bins, glass_m2_per_m=prof, storey_h=FFL[nxt] - FFL[st])
            if gb.is_empty and hb.is_empty:
                continue
            for (l1, x1), (l2, x2) in zip(labels, labels[1:]):
                bay = box(min(x1, x2), y_lo, max(x1, x2), y_hi)
                ga, ha = gb.intersection(bay).area / pt_per_m ** 2, hb.intersection(bay).area / pt_per_m ** 2
                sa = sb.intersection(bay).area / pt_per_m ** 2 if not sb.is_empty else 0.0
                if ga + ha + sa <= 0.01:
                    continue
                rows.append(dict(elevation=name, axis=axis, storey=st, bay=f"{l1}-{l2}", from_mm=round(min(mm(x1), mm(x2))),
                                 to_mm=round(max(mm(x1), mm(x2))), bay_width_m=round(abs(x2 - x1) / pt_per_m, 2),
                                 storey_height_m=round(FFL[nxt] - FFL[st], 2), glass_m2=round(ga, 2), opaque_glass_m2=round(ha, 2), slatted_m2=round(sa, 2),
                                 glass_ratio=round(ga / (abs(x2 - x1) / pt_per_m * (FFL[nxt] - FFL[st])), 3)))
            if write_overlays and st in ("L01", "L02", "L03", "L04", "L05", "L06", "L07", "B1") and not (gb.is_empty and sb.is_empty and hb.is_empty):
                _overlay(p, name, st, y_lo, y_hi, gb, hb, sb, labels, pt_per_m)
    with (REG / "elev_glazing_bays.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    import pickle
    (REG / "elev_glazing_profiles.pkl").write_bytes(pickle.dumps(PROFILES))
    (REG / "elev_slat_profiles.pkl").write_bytes(pickle.dumps(SLATS))
    with (QA / "elev_axes.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(meta[0]))
        w.writeheader()
        w.writerows(meta)
    return meta, len(rows)


def _overlay(p, name, st, y_lo, y_hi, gb, hb, sb, labels, pt_per_m):
    """Review tiles (200 dpi): about six grid bays each; blue = glass, purple = slatted (louvre or fins), orange = hatched; red = grids."""
    from PIL import Image, ImageDraw
    labs = sorted(labels, key=lambda kv: kv[1])
    dpi = 200
    sc = dpi / 72
    tag = re.sub(r"[^A-Za-z]+", "_", name).strip("_")
    for k in range(0, max(1, len(labs) - 1), 6):
        part = labs[k:k + 7]
        X0, X1 = part[0][1] - 25, part[-1][1] + 25
        Y0, Y1 = y_lo - 12, y_hi + 12
        pix = p.get_pixmap(clip=pymupdf.Rect(X0, Y0, X1, Y1), dpi=dpi)
        im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        dr = ImageDraw.Draw(im, "RGBA")
        win = box(X0, Y0, X1, Y1)

        def poly(geom, col):
            if geom.is_empty:
                return
            for g in getattr(geom.intersection(win), "geoms", [geom.intersection(win)]):
                if g.geom_type == "Polygon" and not g.is_empty:
                    dr.polygon([((x - X0) * sc, (y - Y0) * sc) for x, y in g.exterior.coords], fill=col)
        poly(gb, (0, 120, 255, 80))
        poly(sb, (150, 0, 200, 70))
        poly(hb, (255, 140, 0, 100))
        for l, x in part:
            dr.line([((x - X0) * sc, 0), ((x - X0) * sc, im.height)], fill=(255, 0, 0, 170), width=2)
            dr.text(((x - X0) * sc + 4, 4), l, fill=(255, 0, 0, 255))
        dr.text((6, im.height - 16), f"{name} {st} grids {part[0][0]}-{part[-1][0]}: blue glass, purple slatted, orange hatched", fill=(0, 0, 0, 255))
        im.save(QA / f"tile_{tag}_{st}_{part[0][0]}-{part[-1][0]}.png")


if __name__ == "__main__":
    meta, n = run()
    for m in meta:
        print(m)
    print(n, "bay rows")
