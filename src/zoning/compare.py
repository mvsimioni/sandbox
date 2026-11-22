"""Compare method A (vector) with method B (raster) and with the App D area captions.

App D polygons are Bluebeam Area Measurement annotations on a print of the same sheet.  They are
transferred into the background page-view frame through the two grid calibrations, so any offset between the
two pages is applied rather than assumed away.  A room matches an App D polygon when each covers at least 80 %
of the other (the polygon traces that room).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import shapely
from shapely.geometry import Polygon
from shapely.strtree import STRtree

from . import calibrate as cal

AB_PCT = 2.0
AB_M2 = 0.3
CAPTION_PCT = 2.0
MATCH_OVERLAP = 0.8
_NUM = re.compile(r"([\d,]+(?:\.\d+)?)\s*sq\s*m", re.I)


def parse_caption(text: str) -> Optional[float]:
    """Area in m2 from a Bluebeam caption such as ``AREA / 57.02 sq m`` or ``AREA1,112.32 sq m``."""
    m = _NUM.search(text or "")
    return float(m.group(1).replace(",", "")) if m else None


@dataclass
class AppdPolygon:
    annotation_index: int
    xref: int
    caption_m2: float
    polygon_pv_appd: Polygon
    polygon_pv_bg: Polygon
    own_area_m2: float          # polygon area from its geometry at the App D calibration
    own_vs_caption_pct: float


def load_appd_polygons(appd_page, calib_appd: cal.Calibration, calib_bg: cal.Calibration) -> List[AppdPolygon]:
    """Read the Area Measurement polygons of an App D page and transfer them into the background frame."""
    import pymupdf

    rot = appd_page.rotation_matrix
    out: List[AppdPolygon] = []
    for idx, a in enumerate(appd_page.annots()):
        if a.type[1] != "Polygon":
            continue
        cap = parse_caption(a.info.get("content", ""))
        if cap is None or cap <= 0 or not a.vertices:      # a 0 sq m caption (an unfinished measurement) is no area to compare with
            continue
        pts = np.array([tuple(pymupdf.Point(p) * rot) for p in a.vertices], float)
        P_appd = Polygon(pts).buffer(0)
        q = cal.transfer_points(pts, calib_appd, calib_bg)
        P_bg = Polygon(q).buffer(0)
        own = P_appd.area * calib_appd.area_scale_mm2_per_pt2() / 1e6
        out.append(AppdPolygon(idx, a.xref, cap, P_appd, P_bg, own, 100.0 * (own - cap) / cap))
    return out


def match_regions(room_polys: Sequence[Polygon], appd: Sequence[AppdPolygon]) -> Dict[int, Tuple[int, float, float]]:
    """Map room index -> (App D index, overlap of room, overlap of polygon), mutual overlap >= 80 %."""
    tree = STRtree([p.polygon_pv_bg for p in appd])
    out: Dict[int, Tuple[int, float, float]] = {}
    for i, r in enumerate(room_polys):
        best = None
        for j in tree.query(r, predicate="intersects"):
            P = appd[j].polygon_pv_bg
            inter = r.intersection(P).area
            ov_r, ov_p = inter / r.area, inter / P.area
            if ov_r >= MATCH_OVERLAP and ov_p >= MATCH_OVERLAP and (best is None or ov_r + ov_p > best[1] + best[2]):
                best = (int(j), ov_r, ov_p)
        if best:
            out[i] = best
    return out


def within(a: float, b: float, pct: float, m2: float = 0.0) -> bool:
    """True when ``a`` and ``b`` agree within ``pct`` % of ``b`` or ``m2`` m2 (either)."""
    return abs(a - b) <= max(pct / 100.0 * abs(b), m2)


def error_stats(ratios: Sequence[float], pct: float = CAPTION_PCT) -> dict:
    """Median signed error, median absolute error and share within ``pct`` % for area/caption ratios."""
    r = np.asarray([x for x in ratios if x is not None and np.isfinite(x)], float)
    if len(r) == 0:
        return {"n": 0}
    err = 100.0 * (r - 1.0)
    return {"n": int(len(r)), "median_error_pct": float(np.median(err)), "median_abs_error_pct": float(np.median(np.abs(err))),
            "mean_error_pct": float(err.mean()), "share_within_2pct": float(np.mean(np.abs(err) <= pct)),
            "share_within_5pct": float(np.mean(np.abs(err) <= 5.0)), "p90_abs_error_pct": float(np.percentile(np.abs(err), 90))}


# --------------------------------------------------------------------------- #
# Room table
# --------------------------------------------------------------------------- #
CONVENTIONS = ("wall_face", "centreline")


def perimeter_m(poly: Polygon, m_per_pt: float) -> float:
    """Length of all rings (shell and holes) of a page-view polygon in metres."""
    return float(poly.length * m_per_pt)


def build_room_rows(rooms: Sequence[dict], appd: Sequence[AppdPolygon], calib_bg: cal.Calibration,
                    grid: Dict[str, Dict[str, float]], convention: Optional[str] = None) -> Tuple[List[dict], dict]:
    """Per-room comparison rows and the convention statistics.

    ``rooms`` items hold: ``room_id``, ``tag``, ``name``, ``mode``, ``tags_inside``, ``a_face`` (Polygon, page view),
    ``a_cl`` (Polygon), ``b_face_area_m2`` / ``b_face`` / ``b_cl`` (or None when method B produced no region), ``flags``.
    The adopted ``convention`` defaults to the one with the higher share within 2 % of the captions over all matched
    rooms and both methods (ties: lower median absolute error).
    """
    k2 = calib_bg.area_scale_mm2_per_pt2() / 1e6   # m2 per pt2
    k1 = math.sqrt(calib_bg.area_scale_mm2_per_pt2()) / 1000.0  # m per pt
    matches_a = match_regions([r["a_face"] for r in rooms], appd)
    # method B matched separately on its own polygons
    idx_b = [i for i, r in enumerate(rooms) if r.get("b_face") is not None]
    mb = match_regions([rooms[i]["b_face"] for i in idx_b], appd) if idx_b else {}
    matches_b = {idx_b[j]: v for j, v in mb.items()}
    ratios: Dict[Tuple[str, str], List[float]] = {(m, c): [] for m in ("A", "B") for c in CONVENTIONS}
    for i, (j, ov_r, ov_p) in matches_a.items():
        cap = appd[j].caption_m2
        ratios[("A", "wall_face")].append(rooms[i]["a_face"].area * k2 / cap)
        ratios[("A", "centreline")].append(rooms[i]["a_cl"].area * k2 / cap)
        if rooms[i].get("b_face") is not None:
            ratios[("B", "wall_face")].append(rooms[i]["b_face_area_m2"] / cap)
            ratios[("B", "centreline")].append(rooms[i]["b_cl"].area * k2 / cap)
    stats = {"%s/%s" % k: error_stats(v) for k, v in ratios.items()}
    pooled = {c: error_stats(ratios[("A", c)] + ratios[("B", c)]) for c in CONVENTIONS}
    stats["pooled"] = pooled
    if convention is None:
        convention = max(CONVENTIONS, key=lambda c: (pooled[c].get("share_within_2pct", 0.0), -pooled[c].get("median_abs_error_pct", 1e9)))
    stats["adopted_convention"] = convention
    stats["n_matched_A"] = len(matches_a)
    # B is compared on the rooms that method A matched and that have a B region (the rows that carry error_b_vs_caption_pct);
    # n_matched_B_own is how many B polygons match an App D polygon on their own overlap test (a different, independent count)
    stats["n_matched_B"] = stats["B/wall_face"].get("n", 0)
    stats["n_matched_B_own"] = len(matches_b)
    rows: List[dict] = []
    for i, r in enumerate(rooms):
        face = r["a_face"]
        a_poly = face if convention == "wall_face" else r["a_cl"]
        a_area = a_poly.area * k2
        face_area = face.area * k2
        b_area = None
        if r.get("b_face") is not None:
            b_area = r["b_face_area_m2"] if convention == "wall_face" else r["b_cl"].area * k2
        b_face_area = r.get("b_face_area_m2")
        if b_face_area is None:
            agree = "no B region"
        else:
            agree = "agree" if within(face_area, b_face_area, AB_PCT, AB_M2) else "differ"
        # the centroid columns hold the true centroid of the adopted polygon, even when it lies outside the polygon (a C or L shaped room);
        # the label point (inside the polygon) and its grid location are separate columns
        c = a_poly.centroid
        lp = a_poly.representative_point()
        X, Y = calib_bg.to_building(c.x, c.y)
        LX, LY = calib_bg.to_building(lp.x, lp.y)
        row = {
            "room_id": r["room_id"], "room_tag": r["tag"], "room_name": r["name"], "mode": r["mode"],
            "n_tags_in_region": len(r["tags_inside"]), "other_tags": " | ".join(r["tags_inside"][1:]),
            "a_area_m2": round(a_area, 3), "a_perimeter_m": round(perimeter_m(a_poly, k1), 3),
            "b_area_m2": "" if b_area is None else round(b_area, 3),
            "wall_face_area_m2": round(face_area, 3), "centreline_area_m2": round(r["a_cl"].area * k2, 3),
            "boundary_convention": convention,
            "centroid_x_mm": round(float(X), 0), "centroid_y_mm": round(float(Y), 0),
            "grid_location": cal.grid_location(float(X), float(Y), grid),
            "centroid_inside_polygon": "yes" if a_poly.contains(c) else "no",
            "label_point_x_mm": round(float(LX), 0), "label_point_y_mm": round(float(LY), 0),
            "grid_location_label_point": cal.grid_location(float(LX), float(LY), grid),
            "method_agreement": agree,
            "a_minus_b_pct": "" if b_face_area is None else round(100.0 * (face_area - b_face_area) / max(face_area, 1e-9), 2),
            "appd_caption_m2": "", "appd_annotation_index": "", "appd_overlap_room": "", "appd_overlap_polygon": "",
            "error_vs_caption_pct": "", "error_b_vs_caption_pct": "", "flags": r.get("flags", ""),
        }
        if i in matches_a:
            j, ov_r, ov_p = matches_a[i]
            cap = appd[j].caption_m2
            row.update({"appd_caption_m2": cap, "appd_annotation_index": appd[j].annotation_index,
                        "appd_overlap_room": round(ov_r, 3), "appd_overlap_polygon": round(ov_p, 3),
                        "error_vs_caption_pct": round(100.0 * (a_area - cap) / cap, 2)})
            if b_area is not None:
                row["error_b_vs_caption_pct"] = round(100.0 * (b_area - cap) / cap, 2)
        rows.append(row)
    ab = [r for r in rows if r["method_agreement"] in ("agree", "differ")]
    stats["ab_share_within_tolerance"] = (sum(1 for r in ab if r["method_agreement"] == "agree") / len(ab)) if ab else float("nan")
    stats["n_ab_compared"] = len(ab)
    stats["matches_a"] = {int(i): (int(v[0]), v[1], v[2]) for i, v in matches_a.items()}
    return rows, stats
