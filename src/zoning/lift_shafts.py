"""Lift shafts of the plan (24 Nov 2026, Marcus: areas within lift shafts are not covered by any system and are not shown on any zone).

The lift car labels of the tender plan page of each storey (BL.01, FL/BL.01, PL.03, AL.07, CL.01, ISO.01, SL.01, TL.01 and so on) give the
shafts. The shaft outline is the face of the plan wall linework (vector stage of the storey) that holds the label, between 1.5 and 20 m2;
where no such face is found, a 2.6 m square on the label (flagged). Written to out/registers/lift_shafts_<STOREY>.csv (page view pt).
"""
from __future__ import annotations

import csv
import pickle
import re
from pathlib import Path
from typing import Dict, List

import numpy as np
import shapely
from shapely.geometry import Point, box

ROOT = Path(__file__).resolve().parent.parent.parent
REG = ROOT / "out/registers"
RX = re.compile(r"^(?:[A-Z]{1,3}L|ISO|FL/BL)\.\d{1,2}$")
STOREYS = ["LB3", "B3", "B2", "B1", "L01", "L02", "L03", "L04", "L05", "L06", "L07"]


def shafts(st: str, k2: float) -> List[Dict[str, object]]:
    import pymupdf
    from .. import common
    from .rooms_vector import polygonise
    from .storeys import BG_PDF, storey_info, storey_paths
    page = pymupdf.open(str(common.project_root() / BG_PDF))[storey_info(st)["page0"]]
    R = page.rotation_matrix
    labels = []
    for w in page.get_text("words"):
        if RX.match(w[4]):
            c = pymupdf.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2) * R
            labels.append((w[4], Point(c.x, c.y)))
    if not labels:
        return []
    v = pickle.load(open(storey_paths(st)["work"] / "vector.pkl", "rb"))
    seg = np.concatenate([q for q in (v["vec"].wall_segments, v["vec"].thin_segments) if len(q)])
    m = 1.0 / k2 ** 0.5
    out = []
    for name, p in labels:
        win = p.buffer(4 * m).bounds
        sel = seg[(np.minimum(seg[:, 0], seg[:, 2]) <= win[2]) & (np.maximum(seg[:, 0], seg[:, 2]) >= win[0]) &
                  (np.minimum(seg[:, 1], seg[:, 3]) <= win[3]) & (np.maximum(seg[:, 1], seg[:, 3]) >= win[1])]
        face = None
        try:
            for f in polygonise(sel):
                if f.buffer(0.3).contains(p) and 1.5 <= f.area * k2 <= 20.0:
                    face = f if face is None or f.area < face.area else face
        except Exception:
            face = None
        how = "wall face holding the label"
        if face is None:
            h = 1.3 * m
            face, how = box(p.x - h, p.y - h, p.x + h, p.y + h), "2.6 m square on the label (no closed wall face found)"
        out.append(dict(storey=st, label=name, polygon_pv_wkt=shapely.to_wkt(face, rounding_precision=2), area_m2=round(face.area * k2, 2), method=how))
    return out


def write(st: str, k2: float) -> List[Dict[str, object]]:
    rows = shafts(st, k2)
    with (REG / f"lift_shafts_{st}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["storey", "label", "polygon_pv_wkt", "area_m2", "method"])
        w.writeheader()
        w.writerows(rows)
    return rows
