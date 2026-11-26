"""Review pack of the forensic envelope review (D-120): per elevation and storey, the elevation tiles (src.arch.elev_glazing), the matching
plan crop of the zoning sheet (Rev H) and the table of the rooms behind that stretch of facade with the glass read for them.

``python3 -m src.arch.envelope_review_pack`` writes out/qa/envelope_review/<ELEV>/<ST>/ : tile_*.png, plan_*.png, rooms.csv.
"""
from __future__ import annotations

import csv
import re
import shutil
from collections import defaultdict
from pathlib import Path

import numpy as np
import pymupdf
from shapely import wkt

ROOT = Path(__file__).resolve().parent.parent.parent
REG = ROOT / "out/registers"
QA = ROOT / "out/qa/envelope_review"
ZON = ROOT / "out/issue/DRAFT_2026-11-26_R13/zoning"
csv.field_size_limit(1 << 30)


def _affine(st):
    """building mm -> page view (pv) of the zoning sheet, least squares on the room centroids."""
    A, B = [], []
    for r in csv.DictReader((REG / f"room_polygons_{st}.csv").open(encoding="utf-8")):
        if r.get("polygon_pv_wkt") and r.get("polygon_building_mm_wkt"):
            try:
                a, b = wkt.loads(r["polygon_building_mm_wkt"]).centroid, wkt.loads(r["polygon_pv_wkt"]).centroid
            except Exception:
                continue
            A.append([a.x, a.y, 1.0])
            B.append([b.x, b.y])
    M, *_ = np.linalg.lstsq(np.array(A), np.array(B), rcond=None)
    return M


def run():
    steps = list(csv.DictReader((REG / "room_glazing_steps.csv").open(encoding="utf-8")))
    zone = {}
    for st in {s["storey"] for s in steps}:
        for z in csv.DictReader((REG / f"zone_assignment_{st}.csv").open(encoding="utf-8")):
            zone[z["room_id"]] = z.get("class", "")
    by = defaultdict(list)
    for s in steps:
        by[(s["elevation"], s["storey"])].append(s)
    for (el, st), ss in sorted(by.items()):
        tag = re.sub(r"[^A-Za-z]+", "_", el).strip("_")
        d = QA / tag / st
        d.mkdir(parents=True, exist_ok=True)
        for t in QA.glob(f"tile_{tag}_{st}_*.png"):
            shutil.copy(t, d / t.name)
        # rooms table
        agg = defaultdict(lambda: dict(facade_m=0.0, glass=0.0, slat=0.0, hidden=0.0, axis=[], behind=set(), ov=set()))
        for s in ss:
            if not s["room"]:
                continue
            a = agg[(s["room"], s["room_name"])]
            a["facade_m"] += float(s["len_m"])
            a["glass"] += float(s["glass_m2"])
            a["slat"] += float(s.get("slat_m2") or 0)
            a["hidden"] += float(s["len_m"]) if s["hidden"] == "True" else 0.0
            a["axis"].append(float(s["axis_mm"]))
            if s.get("behind_open"):
                a["behind"].add(s["behind_open"])
            a["ov"].update((s.get("override") or "").split())
        with (d / "rooms.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["room_id", "room_name", "zoning_class", "axis_from_mm", "axis_to_mm", "facade_m", "glass_m2_read", "slatted_m2_read",
                        "hidden_m", "through_open_space", "corrections"])
            for (rid, nm), v in sorted(agg.items(), key=lambda kv: min(kv[1]["axis"])):
                w.writerow([rid, nm, zone.get(rid, ""), round(min(v["axis"])), round(max(v["axis"])), round(v["facade_m"], 1),
                            round(v["glass"], 1), round(v["slat"], 1), round(v["hidden"], 1),
                            " ".join(sorted(v["behind"])), " ".join(sorted(v["ov"]))])
        # plan crops along this facade: one per tile range of 6 grid bays (~50 m), 25 m deep
        pdf = ZON / f"AIGH_IP_AHU_Zoning_{st}.pdf"
        if not pdf.exists():
            continue
        M = _affine(st)
        page = pymupdf.open(pdf)[1]
        vis = [s for s in ss if s["hidden"] != "True"]
        if not vis:
            continue
        ax = np.array([float(s["axis_mm"]) for s in vis])
        xs, ys = np.array([float(s["x"]) for s in vis]), np.array([float(s["y"]) for s in vis])
        horiz = el.split()[0] in ("NORTH", "SOUTH")
        lo, hi = ax.min(), ax.max()
        for k, a0 in enumerate(np.arange(lo, hi, 50000.0)):
            m = (ax >= a0) & (ax < a0 + 50000.0)
            if not m.any():
                continue
            if horiz:
                x0, x1 = a0 - 2000, a0 + 52000
                yc = ys[m]
                y0, y1 = yc.min() - 25000, yc.max() + 25000
            else:
                y0, y1 = a0 - 2000, a0 + 52000
                xc = xs[m]
                x0, x1 = xc.min() - 25000, xc.max() + 25000
            corners = np.array([[x0, y0, 1], [x1, y0, 1], [x0, y1, 1], [x1, y1, 1]]) @ M
            r = pymupdf.Rect(corners[:, 0].min(), corners[:, 1].min(), corners[:, 0].max(), corners[:, 1].max()) & page.rect
            if r.is_empty:
                continue
            page.get_pixmap(clip=r, dpi=110).save(d / f"plan_{k:02d}_axis_{round(a0 / 1000)}-{round((a0 + 50000) / 1000)}m.png")


if __name__ == "__main__":
    run()
