"""AHU-zoning keyplan pages of the DD Booklets registered to the building grid (D-040).

``python3 -m src.zoning.booklet_zones run [--storeys B2,L01,...] [--pages "<file>:<page>,..."] [--no-overlays]``

D-040: the zoning is issued as received from the DD Booklets.  Every AHU-zoning keyplan page of the IP block (storeys B2, L01..L07; the
booklets carry no keyplan for LB3, B3 and B1) is handled in four steps:

(a) zone polygons: vector fills of the page first (pymupdf ``get_drawings``: saturated fills that are not legend swatches); where the page has
    no vector fill, raster colour segmentation of the rendered plan (method and confidence say so; every such value is *image-read* and listed
    for a second read);
(b) registration to the building grid: SIFT features of the rendered keyplan are matched to the rendered tender plan of the storey
    (``Arch Combined Tender .pdf``) and a similarity transform is fitted by RANSAC and refitted on the inliers.  The tender page view is tied to
    building mm by an affine fitted to the vertices of ``out/registers/room_polygons_<KEY>.csv`` (page view and building mm of the same
    polygons).  Residual = RMS of the inlier distances in building mm; a fit above 1.5 m, with fewer than 15 inliers, with inliers confined to
    a small part of the plan, or whose zones do not fall on the tender rooms is rejected;
(c) colour to AHU: the zones are coloured by the operating-mode legend of the page (not per AHU), so the AHU tag is taken from the leader line
    of a red tag box that ends in the zone, else from the tag printed inside the zone, else the nearest tag (low confidence).  Tags are
    copied as printed; no tag stays blank;
(d) ``out/registers/booklet_zones_<KEY>.csv`` and ``out/qa/booklet_zones_report.md`` (per page result, pages not registered or not coloured
    and why, per storey share of floor area covered by booklet zones, conflicts listed and not resolved).

Nothing is read from an image except where the ``flags`` column says ``image-read``.  Unknown values stay blank.
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import sys
import warnings
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from .. import common

warnings.filterwarnings("ignore", category=FutureWarning)

TENDER_PDF = "data/source/Basis/Arch Combined Tender .pdf"
BOOKLET_DIR = "data/source/Short Version DD Booklet"
LABELS_CSV = "out/registers/booklet_zoning_labels.csv"
SHEET_INDEX = "out/registers/sheet_index.csv"
WORK_SUBDIR = "booklet_zones"
STOREYS = ["LB3", "B3", "B2", "B1", "L01", "L02", "L03", "L04", "L05", "L06", "L07"]
TENDER_SC = 2.0                 # tender render, px per pt (44.1 mm per px at 1:250)
BOOK_SC = 2.0                   # booklet render, px per pt
BOOK_SCALES = [1.5, 1.0, 0.7, 0.5, 0.35]   # extra resize factors of the booklet render (the plan scale of a booklet differs from the tender)
RANSAC_THR_PX = 3.0             # inlier threshold, tender px (about 130 mm)
MIN_INLIERS = 8                 # feature inliers; the dense edge correlation below is the main independent check
MIN_NCC = 0.18                  # edge correlation of the warped keyplan with the tender sheet at the fit
NCC_RATIO = 1.6                 # ... and at least this multiple of the best correlation at shifts of 15 to 40 px (a wrong fit has no peak)
MAX_RESIDUAL_MM = 1500.0        # task rule: reject fits above 1.5 m
MIN_SPAN_FRAC = 0.2             # inliers must span this share of the keyplan width and height
MIN_ROOM_OVERLAP = 0.30         # below this share of zone area on tender room polygons the page is noted (not rejected: untagged rooms are not in the register)
SCALE_RANGE = (0.3, 3.0)        # tender px per booklet px
LEGEND_RE = re.compile(r"^(?:recirculation|single pass|local non|ac\b|mv\b|nv\b|hc\b|na\b|pandemic|normal|mv zone|exhaust|non-recirc)", re.I)
TAG_RE = re.compile(r"^(?:P?AHU|FCU|PAU|FAHU|MAU|DOAS|EAF|SAF)-\S+", re.I)
LEGEND_PHRASES = [
    "Recirculation with HEPA", "Local Non-Recirculation via Exhaust", "Recirculation", "Single Pass",
    "AC (24HR)", "AC", "MV", "NV", "HC", "NA",
]
CSV_FIELDS = [
    "zone_id", "storey", "department", "booklet_file", "page", "keyplan_title", "ahu_tag_as_printed", "ahu_tag_other", "tag_method", "tag_location",
    "colour_hex", "colour_basis", "fill_opacity", "legend_label_as_printed", "legend_location", "polygon_building_mm_wkt", "area_m2",
    "registration_residual_mm", "registration_inliers", "registration_scale_mm_per_pt", "tender_room_overlap_pct", "polygon_page_pt_wkt",
    "method", "confidence", "flags", "notes", "source",
]


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def title_to_key(title: str) -> Optional[str]:
    """Storey key of a keyplan title ('IP 3RD STOREY- KEYPLAN' -> L03).  None outside the IP block, for combined and for out-of-range titles."""
    u = re.sub(r"\s+", " ", (title or "").upper()).strip()
    if not u.startswith("IP "):
        return None
    if "&" in u or "," in u:
        return None
    m = re.match(r"IP (LOWER )?BASEMENT (\d)\b", u)
    if m:
        n = int(m.group(2))
        if m.group(1):
            return "LB%d" % n if n == 3 else None
        return {1: "B1", 2: "B2", 3: "B3"}.get(n)
    m = re.match(r"IP (\d+)(?:ST|ND|RD|TH) STOREY", u)
    if m:
        n = int(m.group(1))
        return "L%02d" % n if 1 <= n <= 7 else None
    return None


def hex_of(rgb: Sequence[float]) -> str:
    return "#%02X%02X%02X" % tuple(int(round(max(0.0, min(1.0, c)) * 255)) for c in rgb[:3])


def saturation(rgb: Sequence[float]) -> float:
    return max(rgb[:3]) - min(rgb[:3])


def is_red(rgb: Optional[Sequence[float]]) -> bool:
    return bool(rgb) and rgb[0] > 0.7 and rgb[1] < 0.35 and rgb[2] < 0.35


def bbox_str(b: Sequence[float]) -> str:
    return ",".join("%.1f" % v for v in b)


def _p(pt, R) -> Tuple[float, float]:
    import pymupdf
    q = pymupdf.Point(pt[0], pt[1]) * R
    return (float(q.x), float(q.y))


def _rect(r, R) -> Tuple[float, float, float, float]:
    import pymupdf
    q = pymupdf.Rect(r) * R
    return (float(q.x0), float(q.y0), float(q.x1), float(q.y1))


# --------------------------------------------------------------------------- #
# Vector polygons of a drawing
# --------------------------------------------------------------------------- #
def _bezier(p0, p1, p2, p3, n: int = 6):
    out = []
    for i in range(1, n + 1):
        t = i / float(n)
        u = 1 - t
        out.append((u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
                    u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]))
    return out


def drawing_subpaths(items: Iterable, R=None) -> List[List[Tuple[float, float]]]:
    """Sub-paths (point lists) of a pymupdf drawing's ``items``.  A new sub-path starts where a line does not continue the previous one."""
    def tf(p):
        return _p(p, R) if R is not None else (float(p[0]), float(p[1]))

    subs: List[List[Tuple[float, float]]] = []
    cur: List[Tuple[float, float]] = []

    def flush():
        nonlocal cur
        if len(cur) >= 3:
            subs.append(cur)
        cur = []

    for it in items:
        k = it[0]
        if k == "l":
            a, b = tf(it[1]), tf(it[2])
            if not cur or math.hypot(cur[-1][0] - a[0], cur[-1][1] - a[1]) > 0.6:
                flush()
                cur = [a]
            cur.append(b)
        elif k == "c":
            a = tf(it[1])
            if not cur or math.hypot(cur[-1][0] - a[0], cur[-1][1] - a[1]) > 0.6:
                flush()
                cur = [a]
            cur.extend(_bezier(a, tf(it[2]), tf(it[3]), tf(it[4])))
        elif k == "re":
            flush()
            x0, y0, x1, y1 = it[1]
            cur = [tf((x0, y0)), tf((x1, y0)), tf((x1, y1)), tf((x0, y1))]
            flush()
        elif k == "qu":
            flush()
            q = it[1]
            cur = [tf(q.ul), tf(q.ur), tf(q.lr), tf(q.ll)]
            flush()
    flush()
    return subs


def subpaths_to_polygon(subs: Sequence[Sequence[Tuple[float, float]]]):
    """Even-odd combination of the sub-paths (a sub-path inside another is a hole).  None when nothing closes."""
    from shapely.geometry import Polygon
    poly = None
    for s in subs:
        if len(s) < 3:
            continue
        g = Polygon(s)
        if not g.is_valid:
            g = g.buffer(0)
        if g.is_empty or g.area <= 0:
            continue
        poly = g if poly is None else poly.symmetric_difference(g)
    if poly is None or poly.is_empty:
        return None
    return poly


# --------------------------------------------------------------------------- #
# Registration: features, RANSAC similarity
# --------------------------------------------------------------------------- #
def sim_from_pairs(a: np.ndarray, b: np.ndarray) -> Optional[np.ndarray]:
    """2x3 similarity (rotation, uniform scale, translation, no reflection) mapping the two points ``a`` onto ``b``."""
    za = complex(*(a[1] - a[0]))
    zb = complex(*(b[1] - b[0]))
    if abs(za) < 1e-9:
        return None
    s = zb / za
    t = complex(*b[0]) - s * complex(*a[0])
    return np.array([[s.real, -s.imag, t.real], [s.imag, s.real, t.imag]])


def umeyama_similarity(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Least-squares similarity (no reflection) src -> dst, 2x3."""
    ms, md = src.mean(0), dst.mean(0)
    S, D = src - ms, dst - md
    var = (S ** 2).sum() / len(src)
    cov = D.T @ S / len(src)
    U, sig, Vt = np.linalg.svd(cov)
    d = np.ones(2)
    if np.linalg.det(U) * np.linalg.det(Vt) < 0:
        d[-1] = -1
    Rm = U @ np.diag(d) @ Vt
    s = (sig * d).sum() / var if var > 0 else 1.0
    t = md - s * Rm @ ms
    return np.hstack([s * Rm, t[:, None]])


def apply_affine(M: np.ndarray, pts: np.ndarray) -> np.ndarray:
    return pts @ M[:, :2].T + M[:, 2]


def ransac_similarity(src: np.ndarray, dst: np.ndarray, thr: float, smin: float, smax: float, iters: int = 20000,
                      seed: int = 1, min_sep: float = 30.0):
    """Best similarity by two-point RANSAC with a scale window.  Returns (n_inliers, M, inlier_mask) or (0, None, None)."""
    n = len(src)
    if n < 2:
        return 0, None, None
    rng = np.random.default_rng(seed)
    best = (0, None, None)
    batch = 400
    for _ in range(max(1, iters // batch)):
        i = rng.integers(0, n, batch)
        j = rng.integers(0, n, batch)
        ok = i != j
        i, j = i[ok], j[ok]
        a0, a1, b0, b1 = src[i], src[j], dst[i], dst[j]
        za = (a1[:, 0] - a0[:, 0]) + 1j * (a1[:, 1] - a0[:, 1])
        zb = (b1[:, 0] - b0[:, 0]) + 1j * (b1[:, 1] - b0[:, 1])
        good = np.abs(za) > min_sep
        if not good.any():
            continue
        i, j, za, zb, a0, b0 = i[good], j[good], za[good], zb[good], a0[good], b0[good]
        s = zb / za
        sc = np.abs(s)
        win = (sc >= smin) & (sc <= smax)
        if not win.any():
            continue
        s, a0, b0 = s[win], a0[win], b0[win]
        t = (b0[:, 0] + 1j * b0[:, 1]) - s * (a0[:, 0] + 1j * a0[:, 1])
        zs = src[:, 0] + 1j * src[:, 1]
        pred = s[:, None] * zs[None, :] + t[:, None]
        err = np.abs(pred - (dst[:, 0] + 1j * dst[:, 1])[None, :])
        cnt = (err < thr).sum(1)
        k = int(cnt.argmax())
        if cnt[k] > best[0]:
            M = np.array([[s[k].real, -s[k].imag, t[k].real], [s[k].imag, s[k].real, t[k].imag]])
            best = (int(cnt[k]), M, err[k] < thr)
    return best


def refine_similarity(src: np.ndarray, dst: np.ndarray, M: np.ndarray, thr: float, rounds: int = 3):
    """Refit on the inliers a few times; returns (M, inlier mask)."""
    inl = np.linalg.norm(apply_affine(M, src) - dst, axis=1) < thr
    for _ in range(rounds):
        if inl.sum() < 3:
            break
        M = umeyama_similarity(src[inl], dst[inl])
        inl = np.linalg.norm(apply_affine(M, src) - dst, axis=1) < thr
    return M, inl


def match_features(kp_b, des_b, kp_t_pts: np.ndarray, des_t, ratio: float = 0.8):
    """Lowe-ratio matches booklet -> tender, one booklet point per tender point (best distance wins).  Returns (src, dst) arrays."""
    import cv2
    cv2.setRNGSeed(1)
    if des_b is None or des_t is None or len(des_b) < 2:
        return np.zeros((0, 2), np.float32), np.zeros((0, 2), np.float32)
    fl = cv2.FlannBasedMatcher(dict(algorithm=1, trees=4), dict(checks=64))
    m = fl.knnMatch(des_b, des_t, k=2)
    best: Dict[int, Tuple[float, int]] = {}
    for pair in m:
        if len(pair) < 2:
            continue
        a, b = pair
        if a.distance < ratio * b.distance:
            cur = best.get(a.trainIdx)
            if cur is None or a.distance < cur[0]:
                best[a.trainIdx] = (a.distance, a.queryIdx)
    if not best:
        return np.zeros((0, 2), np.float32), np.zeros((0, 2), np.float32)
    ti = np.array(list(best.keys()))
    qi = np.array([v[1] for v in best.values()])
    return np.float32([kp_b[i].pt for i in qi]), kp_t_pts[ti]


def fit_pv_to_mm(pairs_pv: np.ndarray, pairs_mm: np.ndarray) -> Tuple[np.ndarray, float]:
    """Affine (2x3) tender page view pt -> building mm from matching vertices; returns (A, rms_mm)."""
    X = np.hstack([pairs_pv, np.ones((len(pairs_pv), 1))])
    sol, *_ = np.linalg.lstsq(X, pairs_mm, rcond=None)
    A = sol.T
    res = np.linalg.norm(X @ sol - pairs_mm, axis=1)
    return A, float(np.sqrt((res ** 2).mean()))


def compose(*Ms: np.ndarray) -> np.ndarray:
    """Chain 2x3 affines: compose(A, B)(x) = B(A(x))."""
    T = np.eye(3)
    for M in Ms:
        T = np.vstack([M, [0, 0, 1]]) @ T
    return T[:2]


# --------------------------------------------------------------------------- #
# Tags, legend, leaders
# --------------------------------------------------------------------------- #
def page_tags(page, R) -> List[dict]:
    """Equipment tags of the text layer, as printed, with bbox in the rendered frame.  A line ending with '-' is joined with the next line."""
    d = page.get_text("dict")
    lines = []
    for b in d.get("blocks", []):
        for ln in b.get("lines", []):
            txt = "".join(s["text"] for s in ln.get("spans", [])).strip()
            if txt:
                lines.append((txt, _rect(ln["bbox"], R)))
    out = []
    used = set()
    for i, (txt, bb) in enumerate(lines):
        if i in used:
            continue
        if not TAG_RE.match(txt):
            continue
        full, box = txt, list(bb)
        j = i
        while full.endswith("-") and j + 1 < len(lines):
            j += 1
            full += lines[j][0]
            used.add(j)
            box = [min(box[0], lines[j][1][0]), min(box[1], lines[j][1][1]), max(box[2], lines[j][1][2]), max(box[3], lines[j][1][3])]
        out.append({"text": full, "bbox": tuple(box)})
    return out


def page_lines(page, R) -> List[Tuple[str, Tuple[float, float, float, float]]]:
    d = page.get_text("dict")
    res = []
    for b in d.get("blocks", []):
        for ln in b.get("lines", []):
            txt = " ".join("".join(s["text"] for s in ln.get("spans", [])).split())
            if txt:
                res.append((txt, _rect(ln["bbox"], R)))
    return res


def tag_boxes(drawings: Sequence[dict], tags: List[dict], R) -> None:
    """Attach to each tag the red-stroked white box that holds it (key ``box``), else its own bbox."""
    boxes = []
    for d in drawings:
        if d.get("fill") and min(d["fill"]) > 0.97 and is_red(d.get("color")) and d["rect"].get_area() < 40000:
            boxes.append(_rect(d["rect"], R))
    for t in tags:
        cx, cy = (t["bbox"][0] + t["bbox"][2]) / 2, (t["bbox"][1] + t["bbox"][3]) / 2
        cand = [b for b in boxes if b[0] - 1 <= cx <= b[2] + 1 and b[1] - 1 <= cy <= b[3] + 1]
        t["box"] = min(cand, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])) if cand else t["bbox"]
        t["boxed"] = bool(cand)


def _dist_pt_rect(p, r) -> float:
    dx = max(r[0] - p[0], 0, p[0] - r[2])
    dy = max(r[1] - p[1], 0, p[1] - r[3])
    return math.hypot(dx, dy)


def leader_components(drawings: Sequence[dict], R, skip_ids: set) -> List[List[Tuple[float, float]]]:
    """Connected groups (vertex lists) of red line/arrow drawings that are neither zone fills nor tag boxes."""
    segs = []
    for idx, d in enumerate(drawings):
        if idx in skip_ids:
            continue
        col, fil = d.get("color"), d.get("fill")
        if not (is_red(col) or is_red(fil)):
            continue
        if fil and min(fil) > 0.97:
            continue
        pts = []
        for s in drawing_subpaths(d["items"], R):
            pts.extend(s)
        if not pts:
            for it in d["items"]:
                if it[0] == "l":
                    pts.extend([_p(it[1], R), _p(it[2], R)])
        if len(pts) >= 2:
            segs.append(pts)
    # union-find on vertex proximity
    parent = list(range(len(segs)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            if find(i) == find(j):
                continue
            a = np.array(segs[i])
            b = np.array(segs[j])
            if np.min(np.linalg.norm(a[:, None, :] - b[None, :, :], axis=2)) <= 2.5:
                parent[find(i)] = find(j)
    comps: Dict[int, List[Tuple[float, float]]] = defaultdict(list)
    for i, s in enumerate(segs):
        comps[find(i)].extend(s)
    return list(comps.values())


def assign_tags(zones: List[dict], tags: List[dict], comps: List[List[Tuple[float, float]]]) -> List[str]:
    """Fill ``zone['tags']`` (list of (tag dict, method, confidence)) in place.  Returns the notes of tags that no zone took."""
    from shapely.geometry import Point
    notes: List[str] = []
    if not zones:
        return ["no zone polygons on the page"] if tags else []
    for z in zones:
        z["tags"] = []
    zareas = [z["poly_pt"].area for z in zones]
    order = sorted(range(len(zones)), key=lambda i: zareas[i])      # smallest first

    def containing(pt, tol=0.0):
        P = Point(pt)
        for i in order:
            g = zones[i]["poly_pt"]
            if g.contains(P) or (tol and g.distance(P) <= tol):
                return i
        return None

    for t in tags:
        taken = False
        box = t["box"]
        # (1) leader line from the tag box
        hit = set()
        for comp in comps:
            if min(_dist_pt_rect(p, box) for p in comp) > 5.0:
                continue
            for p in comp:
                if _dist_pt_rect(p, box) < 8.0:
                    continue
                zi = containing(p, tol=3.0)
                if zi is not None:
                    hit.add(zi)
        if hit:
            # one end vertex per leader normally; if several zones were hit, the ones whose vertex is farthest are the arrow tips: keep all (a leader may branch)
            for zi in hit:
                zones[zi]["tags"].append((t, "leader line from the tag box ends in the zone", "high" if len(hit) == 1 else "medium"))
            taken = True
        if taken:
            continue
        # (2) tag printed inside the zone
        c = ((t["bbox"][0] + t["bbox"][2]) / 2, (t["bbox"][1] + t["bbox"][3]) / 2)
        zi = containing(c)
        if zi is not None:
            zones[zi]["tags"].append((t, "tag printed inside the zone", "high"))
            continue
        # (3) nearest zone within 40 pt (low)
        P = Point(c)
        ds = sorted((zones[i]["poly_pt"].distance(P), i) for i in range(len(zones)))
        if ds and ds[0][0] <= 40.0:
            zones[ds[0][1]]["tags"].append((t, "nearest zone to the tag (no leader, not inside), %.0f pt" % ds[0][0], "low"))
        else:
            notes.append("tag %s (%s) not linked to a zone: no leader ending in a zone, not inside one, nearest zone %.0f pt away" % (
                t["text"], bbox_str(t["bbox"]), ds[0][0] if ds else float("nan")))
    return notes


# --------------------------------------------------------------------------- #
# Page analysis: vector and raster zones
# --------------------------------------------------------------------------- #
def plan_clip(page, R):
    """Rendered-frame rectangle of the plan: union of the large images (>= 40 000 pt2), else the page without margins."""
    import pymupdf
    rects = []
    for im in page.get_image_info():
        r = pymupdf.Rect(im["bbox"]) * R
        if r.get_area() >= 40000 and r.y0 < 790:
            rects.append(r)
    pr = page.rect          # already the rotated frame
    if not rects:
        return pymupdf.Rect(pr.x0 + 30, pr.y0 + 80, pr.x1 - 30, pr.y1 - 60)
    u = rects[0]
    for r in rects[1:]:
        u |= r
    return u & pr


def vector_zones(page, R, drawings: Sequence[dict], lines, clip) -> Tuple[List[dict], set, List[dict]]:
    """Zone polygons from saturated vector fills.  Returns (zones, ids of the drawings used, legend swatches)."""
    from shapely.geometry import box
    zones, used, swatches = [], set(), []
    seen = set()
    for idx, d in enumerate(drawings):
        fil = d.get("fill")
        if not fil or saturation(fil) < 0.15:
            continue
        if d["rect"].get_area() < 40:
            continue
        op = d.get("fill_opacity")
        op = 1.0 if op is None else float(op)
        if is_red(fil) and op >= 0.99:
            continue                      # arrow heads and tag leaders
        poly = subpaths_to_polygon(drawing_subpaths(d["items"], R))
        if poly is None or poly.area < 40:
            continue
        r = poly.bounds
        w, h = r[2] - r[0], r[3] - r[1]
        # legend swatch: small rectangle with a text line starting just to its right
        lab = None
        if poly.area < 4000 and 10 <= w <= 90 and 5 <= h <= 45:
            for txt, bb in lines:
                if bb[0] >= r[2] - 2 and bb[0] - r[2] <= 55 and bb[1] - 4 <= (r[1] + r[3]) / 2 <= bb[3] + 4 and not TAG_RE.match(txt) and LEGEND_RE.match(txt.strip()):
                    lab = (txt, bb)
                    break
        if lab is not None:
            swatches.append({"colour": hex_of(fil), "opacity": op, "label": lab[0], "bbox": lab[1], "swatch": r})
            used.add(idx)
            continue
        c = poly.centroid
        if not (clip.x0 - 10 <= c.x <= clip.x1 + 10 and clip.y0 - 10 <= c.y <= clip.y1 + 10):
            continue                      # outside the plan: legend or title graphics
        key = (hex_of(fil), round(op, 2), tuple(round(v, 0) for v in r))
        if key in seen:
            continue
        seen.add(key)
        used.add(idx)
        zones.append({"poly_pt": poly, "colour": hex_of(fil), "opacity": op, "kind": "vector", "legend": None})
    # legend meaning of each colour from the swatches of the page
    for z in zones:
        for s in swatches:
            if s["colour"] == z["colour"] and abs(s["opacity"] - z["opacity"]) < 0.06:
                z["legend"] = (s["label"], s["bbox"])
                break
        if z["legend"] is None:
            for s in swatches:
                if s["colour"] == z["colour"]:
                    z["legend"] = (s["label"], s["bbox"])
                    break
    return zones, used, swatches


DEFAULT_PALETTE = [      # nominal fill (RGB 0-1) at 0.5 opacity over white; used only where the legend swatches are not in the text layer
    ("#00FFFF", (0.0, 1.0, 1.0)), ("#FFFF00", (1.0, 1.0, 0.0)), ("#7030A0", (0.44, 0.19, 0.63)), ("#ED7D31", (0.93, 0.49, 0.19)),
]


def raster_zones(page, R, clip, lines, z: float = BOOK_SC) -> Tuple[List[dict], List[str], Optional[np.ndarray]]:
    """Zones by colour segmentation of the rendered plan.  Palette: legend swatch pixels beside known legend phrases, else DEFAULT_PALETTE."""
    import cv2
    import pymupdf
    from shapely.geometry import Polygon
    notes: List[str] = []
    pm = page.get_pixmap(matrix=pymupdf.Matrix(z, z), clip=clip, colorspace=pymupdf.csRGB)
    img = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, 3)
    full = page.get_pixmap(matrix=pymupdf.Matrix(z, z), colorspace=pymupdf.csRGB)
    fimg = np.frombuffer(full.samples, np.uint8).reshape(full.height, full.width, 3)
    lab_all = cv2.cvtColor(img, cv2.COLOR_RGB2LAB).astype(np.float32)
    palette = []
    legend_boxes = []
    for txt, bb in lines:
        for ph in LEGEND_PHRASES[:4]:
            if txt.strip().lower() == ph.lower():
                x0 = int((bb[0] - 22) * z)
                x1 = int((bb[0] - 4) * z)
                y0, y1 = int(bb[1] * z), int(bb[3] * z)
                patch = fimg[max(0, y0):y1, max(0, x0):x1].reshape(-1, 3)
                if len(patch) and (patch.max(1).astype(int) - patch.min(1)).mean() > 25:
                    col = np.median(patch, axis=0)
                    palette.append((txt.strip(), "#%02X%02X%02X" % tuple(int(v) for v in col), col, bb))
                legend_boxes.append(bb)
    if not palette:
        notes.append("legend swatches are not in the text layer: default palette (cyan, yellow, purple, orange) used, colour meaning not read")
        for nm, rgb in DEFAULT_PALETTE:
            col = np.array([255 * (0.5 + 0.5 * c) for c in rgb])
            palette.append((None, nm, col, None))
    mask_total = np.zeros(img.shape[:2], bool)
    zones = []
    for label, hx, col, lbb in palette:
        lab_c = cv2.cvtColor(np.uint8([[col]]), cv2.COLOR_RGB2LAB).astype(np.float32)[0, 0]
        dab = np.linalg.norm(lab_all[:, :, 1:] - lab_c[1:], axis=2)
        chroma = np.linalg.norm(lab_all[:, :, 1:] - 128.0, axis=2)
        m = ((dab < 14) & (chroma > 12)).astype(np.uint8)
        k = max(3, int(z * 3) | 1)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((k, k), np.uint8))
        # legend block is not zone area
        for bb in legend_boxes:
            x0, y0 = int((bb[0] - 30 - clip.x0) * z), int((bb[1] - 4 - clip.y0) * z)
            x1, y1 = int((bb[2] + 10 - clip.x0) * z), int((bb[3] + 4 - clip.y0) * z)
            m[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = 0
        cnts, hier = cv2.findContours(m, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        if hier is None:
            continue
        hier = hier[0]
        for i, c in enumerate(cnts):
            if hier[i][3] != -1 or cv2.contourArea(c) < 160:
                continue
            holes = [cnts[j].reshape(-1, 2) for j in range(len(cnts)) if hier[j][3] == i and cv2.contourArea(cnts[j]) >= 160]
            outer = c.reshape(-1, 2)
            if len(outer) < 3:
                continue
            pg = Polygon(outer, [h for h in holes if len(h) >= 3])
            if not pg.is_valid:
                pg = pg.buffer(0)
            pg = pg.simplify(0.8)
            if pg.is_empty or pg.area < 160:
                continue
            from shapely import affinity
            pg = affinity.scale(pg, 1.0 / z, 1.0 / z, origin=(0, 0))
            pg = affinity.translate(pg, clip.x0, clip.y0)
            sel = np.zeros(m.shape, np.uint8)
            cv2.drawContours(sel, [c], -1, 1, -1)
            med = np.median(img[sel > 0], axis=0)
            zones.append({"poly_pt": pg, "colour": hx, "opacity": None, "kind": "raster", "legend": (label, lbb) if label else None,
                          "median_hex": "#%02X%02X%02X" % tuple(int(v) for v in med), "default_palette": label is None})
        mask_total |= m > 0
    return zones, notes, img


# --------------------------------------------------------------------------- #
# Tender side: pv->mm affine, floor union, features
# --------------------------------------------------------------------------- #
class Tender:
    """Tender plan of one storey: rendered page, SIFT features, page view -> building mm affine, room polygons."""

    def __init__(self, key: str, root: Path):
        self.key = key
        self.root = root
        reg = root / "out" / "registers"
        idx = [r for r in common.read_csv_dicts(reg / "sheet_index.csv") if r["storey_key"] == key and int(r["page"]) <= 11]
        if not idx:
            raise ValueError("storey %s has no tender sheet" % key)
        self.page_no = int(idx[0]["page"])
        self.drawing = idx[0]["drawing_number"]
        self._load_rooms(reg / ("room_polygons_%s.csv" % key))
        self.work = root / "out" / "zoning" / WORK_SUBDIR / "work"
        self.work.mkdir(parents=True, exist_ok=True)
        self._pts = self._des = None

    def _load_rooms(self, path: Path) -> None:
        from shapely import wkt
        from shapely.ops import unary_union
        pv, mm, polys = [], [], []
        for r in common.read_csv_dicts(path):
            try:
                gp = wkt.loads(r["polygon_pv_wkt"])
                gm = wkt.loads(r["polygon_building_mm_wkt"])
            except Exception:
                continue
            if gm.is_valid and not gm.is_empty:
                polys.append(gm)
            if gp.geom_type != "Polygon" or gm.geom_type != "Polygon":       # an approximate multipart outline is not a registration pair
                continue
            ap, am = list(gp.exterior.coords), list(gm.exterior.coords)
            if len(ap) == len(am):
                pv.extend(ap)
                mm.extend(am)
        if len(pv) < 6:
            raise ValueError("%s: no matching page-view / building-mm vertices" % path)
        self.A_pv_mm, self.A_rms = fit_pv_to_mm(np.array(pv), np.array(mm))
        self.n_pairs = len(pv)
        self.rooms = polys
        self.floor = unary_union([g if g.is_valid else g.buffer(0) for g in polys])

    def features(self):
        """SIFT keypoints/descriptors of the tender render (cached in out/zoning/booklet_zones/work)."""
        import cv2
        import pymupdf
        if self._pts is not None:
            return self._pts, self._des
        cache = self.work / ("tender_%s_sift_sc%g.npz" % (self.key, TENDER_SC))
        if cache.exists():
            z = np.load(cache)
            self._pts, self._des = z["pts"], z["des"]
            return self._pts, self._des
        pg = pymupdf.open(str(self.root / TENDER_PDF))[self.page_no - 1]
        pm = pg.get_pixmap(matrix=pymupdf.Matrix(TENDER_SC, TENDER_SC), colorspace=pymupdf.csGRAY)
        img = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width)
        self.render_shape = img.shape
        kp, des = cv2.SIFT_create().detectAndCompute(img, None)
        self._pts = np.float32([k.pt for k in kp])
        self._des = des
        np.savez(cache, pts=self._pts, des=self._des)
        return self._pts, self._des

    def render_gray(self):
        import pymupdf
        if getattr(self, "_gray", None) is None:
            pg = pymupdf.open(str(self.root / TENDER_PDF))[self.page_no - 1]
            pm = pg.get_pixmap(matrix=pymupdf.Matrix(TENDER_SC, TENDER_SC), colorspace=pymupdf.csGRAY)
            self._gray = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width)
        return self._gray

    @property
    def mm_per_px(self) -> float:
        return float(math.sqrt(abs(np.linalg.det(self.A_pv_mm[:, :2])))) / TENDER_SC


def _edge_map(g: np.ndarray) -> np.ndarray:
    import cv2
    b = cv2.GaussianBlur(g, (0, 0), 1.5).astype(np.float32)
    return np.hypot(cv2.Sobel(b, cv2.CV_32F, 1, 0), cv2.Sobel(b, cv2.CV_32F, 0, 1))


def _ncc(a: np.ndarray, b: np.ndarray, m: np.ndarray) -> float:
    a, b = a[m], b[m]
    a = a - a.mean()
    b = b - b.mean()
    d = math.sqrt(float((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / d) if d > 0 else 0.0


def verify_fit(tender_gray: np.ndarray, book_gray: np.ndarray, M: np.ndarray, s: float = 0.5) -> Tuple[float, float]:
    """Independent check of a fit: normalised correlation of the edge maps of the warped keyplan and the tender sheet, at the fit and at
    shifts of 15 to 40 px (at scale ``s``).  Returns (ncc at the fit, best ncc at a shift)."""
    import cv2
    Mw = compose(M, np.array([[s, 0, 0], [0, s, 0]]))
    h, w = tender_gray.shape
    W, H = int(w * s), int(h * s)
    warped = cv2.warpAffine(book_gray, Mw, (W, H), flags=cv2.INTER_AREA, borderValue=0)
    mask = cv2.warpAffine(np.full(book_gray.shape, 255, np.uint8), Mw, (W, H)) > 250
    if mask.sum() < 2500:
        return 0.0, 1.0
    ts = cv2.resize(tender_gray, (W, H), interpolation=cv2.INTER_AREA)
    ys, xs = np.where(mask)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    ea = _edge_map(warped)[y0:y1, x0:x1]
    et = _edge_map(ts)[y0:y1, x0:x1]
    m = cv2.erode(mask[y0:y1, x0:x1].astype(np.uint8), np.ones((15, 15), np.uint8)) > 0
    if m.sum() < 1000:
        return 0.0, 1.0
    n0 = _ncc(ea, et, m)
    sh = []
    for dy in range(-40, 41, 10):
        for dx in range(-40, 41, 10):
            if max(abs(dx), abs(dy)) < 15:
                continue
            sh.append(_ncc(ea, np.roll(et, (dy, dx), (0, 1)), m))
    return n0, max(sh)


def register_page(tender: Tender, gray: np.ndarray) -> dict:
    """Register a rendered keyplan (grey, BOOK_SC px/pt) to the tender: returns a dict with ``M`` (keyplan px -> tender px), residual and checks.

    Candidates: the keyplan as rendered and contrast-equalised (CLAHE), each at several resize factors.  The three with most inliers are
    checked by the edge correlation; the first that passes is taken."""
    import cv2
    pts_t, des_t = tender.features()
    sift = cv2.SIFT_create()
    variants = [("raw", gray), ("clahe", cv2.createCLAHE(3.0, (8, 8)).apply(gray))]
    cands = []
    log = []
    for vname, vimg in variants:
        for bs in BOOK_SCALES:
            img = cv2.resize(vimg, None, fx=bs, fy=bs, interpolation=cv2.INTER_AREA if bs < 1 else cv2.INTER_CUBIC)
            kp, des = sift.detectAndCompute(img, None)
            src, dst = match_features(kp, des, pts_t, des_t)
            src = src / bs
            if len(src) < MIN_INLIERS:
                log.append("%s x%.2f: %d matches" % (vname, bs, len(src)))
                continue
            n, M, inl = ransac_similarity(src, dst, RANSAC_THR_PX, SCALE_RANGE[0], SCALE_RANGE[1])
            if M is None:
                log.append("%s x%.2f: no similarity" % (vname, bs))
                continue
            M, inl = refine_similarity(src, dst, M, RANSAC_THR_PX)
            log.append("%s x%.2f: %d matches, %d inliers" % (vname, bs, len(src), int(inl.sum())))
            if inl.sum() >= MIN_INLIERS:
                cands.append({"n": int(inl.sum()), "M": M, "src": src[inl], "dst": dst[inl], "variant": "%s x%.2f" % (vname, bs)})
    res = {"ok": False, "log": "; ".join(log), "n_inliers": max([c["n"] for c in cands], default=0)}
    if not cands:
        res["reason"] = "too few matching features between the keyplan and the tender sheet (best %d inliers, need %d)" % (res["n_inliers"], MIN_INLIERS)
        return res
    cands.sort(key=lambda c: -c["n"])
    A = tender.A_pv_mm
    tg = tender.render_gray()
    why = []
    for best in cands[:3]:
        M = best["M"]
        mapped = apply_affine(M, best["src"]) / TENDER_SC
        target = best["dst"] / TENDER_SC
        d_mm = np.linalg.norm(apply_affine(A, mapped) - apply_affine(A, target), axis=1)
        rms = float(np.sqrt((d_mm ** 2).mean()))
        sx = best["src"]
        span = (float(np.ptp(sx[:, 0]) / gray.shape[1]), float(np.ptp(sx[:, 1]) / gray.shape[0]))
        scale_tp = math.hypot(M[0, 0], M[1, 0])
        n0, nsh = verify_fit(tg, variants[1][1], M)
        r = {"ok": False, "log": res["log"], "M": M, "n_inliers": best["n"], "rms_mm": rms, "span": span, "scale_tender_per_book": scale_tp,
             "rot_deg": math.degrees(math.atan2(M[1, 0], M[0, 0])), "mm_per_book_px": scale_tp * tender.mm_per_px, "ncc": n0, "ncc_shift_max": nsh,
             "variant": best["variant"]}
        reasons = []
        if rms > MAX_RESIDUAL_MM:
            reasons.append("residual %.0f mm above %.0f mm" % (rms, MAX_RESIDUAL_MM))
        if min(span) < MIN_SPAN_FRAC:
            reasons.append("inliers span only %.0f%% x %.0f%% of the keyplan" % (100 * span[0], 100 * span[1]))
        if n0 < MIN_NCC or n0 < NCC_RATIO * nsh:
            reasons.append("edge correlation %.2f at the fit against %.2f at shifted positions: no peak (need >= %.2f and >= %.1f x)" % (
                n0, nsh, MIN_NCC, NCC_RATIO))
        r["ok"] = not reasons
        if reasons:
            r["reason"] = "%s (%d inliers, %s)" % ("; ".join(reasons), best["n"], best["variant"])
            why.append(r)
            continue
        return r
    out = why[0]
    out["reason"] = " | ".join(w["reason"] for w in why)
    return out


# --------------------------------------------------------------------------- #
# Page driver
# --------------------------------------------------------------------------- #
def discover_pages(root: Path, only_storeys: Optional[Sequence[str]] = None) -> List[dict]:
    """Keyplan pages of the label register whose title is an IP storey keyplan in scope."""
    rows = common.read_csv_dicts(root / LABELS_CSV)
    pages: Dict[Tuple[str, int], dict] = {}
    for r in rows:
        key = title_to_key(r["keyplan_title"])
        if key is None or (only_storeys and key not in only_storeys):
            continue
        k = (r["source_file"], int(r["pdf_page"]))
        pages.setdefault(k, {"file": r["source_file"], "page": int(r["pdf_page"]), "title": r["keyplan_title"], "key": key,
                             "department": r["department_label"] or r["department"], "booklet_page": r.get("booklet_page", ""),
                             "legend_in_register": r.get("has_colour_legend", ""), "legend_source": r.get("legend_source", ""),
                             "legend_bbox": r.get("legend_bbox", "")})
    return sorted(pages.values(), key=lambda p: (p["key"], p["file"], p["page"]))


def header_text(lines, R_rect) -> str:
    top = [(bb[1], bb[0], t) for t, bb in lines if bb[1] < 70 and bb[0] < 700]
    return " / ".join(t for _, _, t in sorted(top))[:120]


def process_page(root: Path, info: dict, tender: Tender, overlays: Optional[Path]) -> dict:
    """Analyse one booklet page.  Returns {'info', 'status', 'rows', 'notes', 'reg', ...}."""
    import cv2
    import pymupdf
    from shapely import affinity
    path = root / info["file"]
    doc = pymupdf.open(str(path))
    page = doc[info["page"] - 1]
    R = page.rotation_matrix
    out = {"info": info, "rows": [], "notes": [], "second_read": []}
    text_up = page.get_text().upper()
    lines = page_lines(page, R)
    out["header"] = header_text(lines, R)
    if "ZONING" not in text_up:
        out["status"] = "not an AHU zoning page"
        out["reason"] = "no 'ZONING' text; header '%s' (layout, mode or pressure page)" % out["header"]
        return out
    drawings = page.get_drawings()
    clip = plan_clip(page, R)
    zones, used, swatches = vector_zones(page, R, drawings, lines, clip)
    method = "vector fills (pymupdf get_drawings)"
    notes = []
    gray_img = None
    if not zones:
        zones, rnotes, _ = raster_zones(page, R, clip, lines)
        notes.extend(rnotes)
        method = "raster colour segmentation of the rendered plan, %.0f px/pt, crop box %s pt" % (BOOK_SC, bbox_str(tuple(clip)))
        if zones:
            out["second_read"].append("page %d: zones from colour segmentation (image-read), crop box %s: second read of the polygons%s" % (
                info["page"], bbox_str(tuple(clip)), "" if any(z["legend"] for z in zones) else " and of the colour meaning"))
    if not zones:
        out["status"] = "no zones"
        out["reason"] = "AHU zoning page without a coloured zone: no saturated vector fill and no legend colour found in the rendered plan"
        out["notes"] = notes
        return out
    unl = sorted({z["colour"] for z in zones if not z["legend"]})
    if unl and info.get("legend_in_register") == "Y" and info.get("legend_source") == "image":
        out["second_read"].append("page %d: the colour legend is a picture (register legend box %s pt), so the meaning of %s was not read: read the legend picture" % (
            info["page"], info.get("legend_bbox", ""), ", ".join(unl)))
    elif unl:
        out["second_read"].append("page %d: no legend text beside the colours %s in the text layer: meaning of the colours not read" % (info["page"], ", ".join(unl)))
    # tags
    tags = page_tags(page, R)
    tag_boxes(drawings, tags, R)
    zone_ids = set(used)
    comps = leader_components(drawings, R, zone_ids)
    tnotes = assign_tags(zones, tags, comps)
    notes.extend(tnotes)
    out["n_tags_page"] = len(tags)
    # registration
    cpx = clip
    pm = page.get_pixmap(matrix=pymupdf.Matrix(BOOK_SC, BOOK_SC), clip=cpx, colorspace=pymupdf.csGRAY)
    gray = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width)
    reg = register_page(tender, gray)
    out["reg"] = reg
    out["notes"] = notes
    out["method"] = method
    out["clip"] = tuple(cpx)
    pt2px = np.array([[BOOK_SC, 0, -cpx.x0 * BOOK_SC], [0, BOOK_SC, -cpx.y0 * BOOK_SC]])
    reg_ok = bool(reg.get("ok"))
    if reg_ok:
        pt2mm = compose(pt2px, reg["M"], np.array([[1 / TENDER_SC, 0, 0], [0, 1 / TENDER_SC, 0]]), tender.A_pv_mm)
        out["pt2mm"] = pt2mm
        from shapely.affinity import affine_transform
        a, b, c, d, e, f = pt2mm[0, 0], pt2mm[0, 1], pt2mm[1, 0], pt2mm[1, 1], pt2mm[0, 2], pt2mm[1, 2]
        overl = []
        for z in zones:
            gm = affine_transform(z["poly_pt"], [a, b, c, d, e, f])
            if not gm.is_valid:
                gm = gm.buffer(0)
            z["poly_mm"] = gm
            inter = gm.intersection(tender.floor).area if not gm.is_empty else 0.0
            z["overlap"] = inter / gm.area if gm.area > 0 else 0.0
            overl.append((inter, gm.area))
        tot = sum(x for x, _ in overl) / max(1.0, sum(y for _, y in overl))
        out["room_overlap"] = tot
        if tot < MIN_ROOM_OVERLAP:
            out["notes"].append("only %.0f%% of the zone area falls on tender room polygons (rooms without a tag are not in the register); fit kept, "
                                "edge correlation %.2f against %.2f at shifted positions" % (100 * tot, reg["ncc"], reg["ncc_shift_max"]))
    out["reg_ok"] = reg_ok
    # rows
    for zi, z in enumerate(zones, 1):
        row = {k: "" for k in CSV_FIELDS}
        tg = z.get("tags", [])
        row["zone_id"] = "%s-%s-p%d-Z%02d" % (info["key"], re.sub(r"[^A-Za-z0-9]+", "", Path(info["file"]).stem)[:18], info["page"], zi)
        row["storey"] = info["key"]
        row["department"] = info["department"]
        row["booklet_file"] = info["file"]
        row["page"] = info["page"]
        row["keyplan_title"] = info["title"]
        if tg:
            row["ahu_tag_as_printed"] = tg[0][0]["text"]
            row["ahu_tag_other"] = "; ".join(t["text"] for t, _, _ in tg[1:])
            row["tag_method"] = tg[0][1]
            row["tag_location"] = "page %d bbox %s pt (text layer)" % (info["page"], bbox_str(tg[0][0]["bbox"]))
        row["colour_hex"] = z["colour"]
        if z["kind"] == "vector":
            row["colour_basis"] = "nominal vector fill colour"
            row["fill_opacity"] = "%.2f" % z["opacity"]
        else:
            row["colour_basis"] = ("colour class of the segmentation: %s; median rendered pixel colour of the zone %s" % (
                "default palette, nominal fill at 0.5 opacity (legend swatch is not text)" if z.get("default_palette") else "legend swatch pixel colour",
                z.get("median_hex", "")))
        if z["legend"]:
            row["legend_label_as_printed"] = z["legend"][0]
            row["legend_location"] = "page %d bbox %s pt (text layer)" % (info["page"], bbox_str(z["legend"][1]))
        row["polygon_page_pt_wkt"] = z["poly_pt"].wkt if len(z["poly_pt"].wkt) < 30000 else z["poly_pt"].simplify(0.5).wkt
        flags = []
        if z["kind"] == "raster":
            flags.append("image-read (colour segmentation, crop box %s pt; second read listed)" % bbox_str(tuple(cpx)))
        if tg and tg[0][2] == "low":
            flags.append("tag by proximity only")
        if len(tg) > 1:
            flags.append("%d tags linked to the zone" % len(tg))
        if not tg:
            flags.append("no AHU tag linked to the zone")
        if reg_ok:
            gm = z["poly_mm"]
            row["polygon_building_mm_wkt"] = gm.wkt
            row["area_m2"] = "%.2f" % (gm.area / 1e6)
            row["registration_residual_mm"] = "%.0f" % reg["rms_mm"]
            row["registration_inliers"] = reg["n_inliers"]
            row["registration_scale_mm_per_pt"] = "%.2f" % (math.sqrt(abs(np.linalg.det(out["pt2mm"][:, :2]))))
            row["tender_room_overlap_pct"] = "%.0f" % (100 * z["overlap"])
            conf = "high" if (reg["rms_mm"] < 250 and reg["n_inliers"] >= 40 and out.get("room_overlap", 0) >= MIN_ROOM_OVERLAP) else "medium"
            if z["kind"] == "raster" or (tg and tg[0][2] == "low"):
                conf = "low"
            elif tg and tg[0][2] == "medium" and conf == "high":
                conf = "medium"
            row["confidence"] = conf
        else:
            row["confidence"] = "none"
            row["registration_inliers"] = reg.get("n_inliers", "")
            flags.append("unregistered: %s" % reg.get("reason", "?"))
        row["method"] = "%s; registration by SIFT features keyplan to tender sheet %s, similarity RANSAC refit on inliers" % (method, tender.drawing)
        row["flags"] = "; ".join(flags)
        row["notes"] = ""
        row["source"] = "%s page %d (title as printed '%s')" % (info["file"], info["page"], info["title"])
        out["rows"].append(row)
    out["zones"] = zones
    out["status"] = "registered" if reg_ok else "not registered"
    if not reg_ok:
        out["reason"] = reg.get("reason", "")
    # overlay for the eye
    if overlays is not None and "pt2mm" in out:
        try:
            _write_overlay(overlays, info, tender, zones, out["pt2mm"], "" if reg_ok else "_REJECTED")
        except Exception as e:                                   # the overlay is a convenience only
            out["notes"].append("overlay not written: %s" % e)
    return out


def _write_overlay(folder: Path, info: dict, tender: Tender, zones: List[dict], pt2mm: np.ndarray, suffix: str = "") -> None:
    import cv2
    folder.mkdir(parents=True, exist_ok=True)
    img = tender.render_gray()
    inv = np.linalg.inv(np.vstack([tender.A_pv_mm, [0, 0, 1]]))[:2]
    over = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    layer = over.copy()
    xs, ys = [], []
    for z in zones:
        g = z["poly_mm"]
        for part in (g.geoms if hasattr(g, "geoms") else [g]):
            if part.is_empty:
                continue
            pv = apply_affine(inv, np.array(part.exterior.coords)) * TENDER_SC
            xs.extend(pv[:, 0])
            ys.extend(pv[:, 1])
            col = tuple(int(z["colour"][i:i + 2], 16) for i in (5, 3, 1))
            cv2.fillPoly(layer, [pv.astype(np.int32)], col)
            cv2.polylines(over, [pv.astype(np.int32)], True, (0, 0, 255), 3)
    over = cv2.addWeighted(layer, 0.45, over, 0.55, 0)
    if xs:
        x0, x1, y0, y1 = int(min(xs)) - 150, int(max(xs)) + 150, int(min(ys)) - 150, int(max(ys)) + 150
        over = over[max(0, y0):y1, max(0, x0):x1]
    s = 1500.0 / max(over.shape[:2])
    over = cv2.resize(over, None, fx=min(1.0, s), fy=min(1.0, s), interpolation=cv2.INTER_AREA)
    name = "%s_%s_p%d%s.jpg" % (info["key"], re.sub(r"[^A-Za-z0-9]+", "", Path(info["file"]).stem)[:18], info["page"], suffix)
    cv2.imwrite(str(folder / name), over, [cv2.IMWRITE_JPEG_QUALITY, 80])


# --------------------------------------------------------------------------- #
# Storey statistics, conflicts, report
# --------------------------------------------------------------------------- #
def storey_coverage(tender: Tender, results: List[dict]) -> dict:
    from shapely.ops import unary_union
    polys = []
    for r in results:
        if r.get("reg_ok"):
            polys.extend(z["poly_mm"] for z in r["zones"])
    if not polys:
        return {"floor_m2": tender.floor.area / 1e6, "zones_m2": 0.0, "covered_m2": 0.0, "share_pct": 0.0, "n": 0}
    u = unary_union([p if p.is_valid else p.buffer(0) for p in polys])
    cov = u.intersection(tender.floor)
    return {"floor_m2": tender.floor.area / 1e6, "zones_m2": u.area / 1e6, "covered_m2": cov.area / 1e6,
            "share_pct": 100 * cov.area / tender.floor.area if tender.floor.area else 0.0, "n": len(polys)}


def find_conflicts(results: List[dict]) -> List[str]:
    """Overlaps of zones of different pages with different AHU tags, listed and not resolved."""
    zs = []
    for r in results:
        if r.get("reg_ok"):
            for z, row in zip(r["zones"], r["rows"]):
                if row["ahu_tag_as_printed"]:
                    zs.append((r["info"], z, row))
    out = []
    for i in range(len(zs)):
        for j in range(i + 1, len(zs)):
            ia, za, ra = zs[i]
            ib, zb, rb = zs[j]
            if (ia["file"], ia["page"]) == (ib["file"], ib["page"]) or ra["ahu_tag_as_printed"] == rb["ahu_tag_as_printed"]:
                continue
            if not za["poly_mm"].bounds[0] < zb["poly_mm"].bounds[2] or not zb["poly_mm"].bounds[0] < za["poly_mm"].bounds[2]:
                continue
            inter = za["poly_mm"].intersection(zb["poly_mm"]).area
            small = min(za["poly_mm"].area, zb["poly_mm"].area)
            if small > 0 and inter / small > 0.25:
                out.append("%s: zone %s (%s, %s p%d) overlaps %s (%s, %s p%d) by %.1f m2 (%.0f%% of the smaller); tags differ, both kept" % (
                    ra["storey"], ra["zone_id"], ra["ahu_tag_as_printed"], Path(ia["file"]).name, ia["page"], rb["zone_id"], rb["ahu_tag_as_printed"],
                    Path(ib["file"]).name, ib["page"], inter / 1e6, 100 * inter / small))
    return out


def write_report(root: Path, results_by_key: Dict[str, List[dict]], tenders: Dict[str, Tender], cover: Dict[str, dict], all_pages: List[dict],
                 conflicts: List[str], errors: List[str]) -> Path:
    L: List[str] = []
    L.append("# Booklet zoning registered to the building grid (D-040)")
    L.append("")
    L.append("Generated by `python3 -m src.zoning.booklet_zones run`. Sources: the AHU-zoning keyplan pages of `%s`, the label register `%s` and the tender "
             "plans `%s`. Registers: `out/registers/booklet_zones_<KEY>.csv`." % (BOOKLET_DIR, LABELS_CSV, TENDER_PDF))
    L.append("")
    L.append("## Method and limits")
    L.append("")
    L.append("- Zone polygons: vector fills first (pymupdf `get_drawings`); where a page has no vector fill the rendered plan is segmented by colour "
             "(raster; flagged image-read and listed for a second read below).")
    L.append("- Registration: SIFT features of the rendered keyplan matched to the rendered tender sheet of the storey, similarity fit by RANSAC and "
             "refit on the inliers; tender page view tied to building mm by an affine fitted to the vertices of `room_polygons_<KEY>.csv`. "
             "Rejected above %.0f mm residual, below %d inliers, if the inliers span less than %.0f%% of the keyplan, if the edge correlation of the warped "
             "keyplan with the tender sheet at the fit is below %.2f or below %.1f x its best value at shifts of 15 to 40 px (independent check: a wrong fit "
             "has no correlation peak). The share of a zone that falls on tender room polygons is reported per zone and noted below %.0f%%, but does not "
             "reject a fit (rooms without a tag are not in the room register)." % (
                 MAX_RESIDUAL_MM, MIN_INLIERS, 100 * MIN_SPAN_FRAC, MIN_NCC, NCC_RATIO, 100 * MIN_ROOM_OVERLAP))
    L.append("- The fills of these pages are coloured by operating mode of the page legend (for example Recirculation, Single Pass, Recirculation with "
             "HEPA, AC, MV), not one colour per AHU. The AHU of a zone is read from the tag box whose leader line ends in the zone, else the tag printed "
             "inside the zone, else the nearest tag (confidence low). A zone without such a link has a blank AHU tag.")
    L.append("- Grid bubbles of the keyplans are raster pictures; none was read. The keyplans are registered by drawing content, not by bubbles.")
    L.append("- The residual is the RMS distance, in building mm, between the matched feature points of the keyplan and of the tender sheet after the fit "
             "(inliers within about 130 mm of the fit by construction). It measures the fit of the features, not the drawing accuracy of the booklet.")
    L.append("- Scope: IP storeys B2 and L01 to L07. The label register holds IP keyplans for no LB3, B3 or B1 page (OP BASEMENT 1 and other OP titles are "
             "the outpatient building and are out of scope), so nothing could be registered there.")
    L.append("")
    L.append("## Per storey: share of floor area covered by booklet zones")
    L.append("")
    L.append("Floor area = union of the tender room polygons of the storey (`room_polygons_<KEY>.csv`, wall-face convention). Covered = union of the "
             "registered booklet zones that fall on those rooms. Overlapping zones of different pages are counted once.")
    L.append("")
    L.append("| Storey | Tender sheet | Floor area m2 | Zones registered | Union of zones m2 | Zones on floor m2 | Share of floor area |")
    L.append("|---|---|---:|---:|---:|---:|---:|")
    for key in STOREYS:
        t = tenders.get(key)
        if key in cover:
            c = cover[key]
            L.append("| %s | %s | %.0f | %d | %.0f | %.0f | %.1f %% |" % (key, t.drawing if t else "", c["floor_m2"], c["n"], c["zones_m2"], c["covered_m2"], c["share_pct"]))
        else:
            L.append("| %s | %s | %s | 0 | 0 | 0 | 0.0 %% (no AHU-zoning keyplan page in the booklets) |" % (key, t.drawing if t else "", ("%.0f" % (t.floor.area / 1e6)) if t else ""))
    L.append("")
    L.append("## Per page result")
    L.append("")
    L.append("| Storey | Booklet file | Page | Title as printed | Result | Zones | With AHU tag | Residual mm | Inliers | Edge corr. fit / shifted | Scale mm/pt | Room overlap | Note |")
    L.append("|---|---|---:|---|---|---:|---:|---:|---:|---|---:|---:|---|")
    for key in STOREYS:
        for r in results_by_key.get(key, []):
            i = r["info"]
            rows = r.get("rows", [])
            reg = r.get("reg", {})
            note = r.get("reason", "")
            if r["status"] == "registered":
                note = "%s; %s" % (r.get("method", "").split(" (")[0].split(",")[0], reg.get("variant", ""))
            L.append("| %s | %s | %d | %s | %s | %d | %d | %s | %s | %s | %s | %s | %s |" % (
                key, Path(i["file"]).name, i["page"], i["title"], r["status"], len(rows), sum(1 for x in rows if x["ahu_tag_as_printed"]),
                ("%.0f" % reg["rms_mm"]) if "rms_mm" in reg else "", reg.get("n_inliers", ""),
                ("%.2f / %.2f" % (reg["ncc"], reg["ncc_shift_max"])) if "ncc" in reg else "",
                ("%.1f" % math.sqrt(abs(np.linalg.det(r["pt2mm"][:, :2])))) if "pt2mm" in r else "",
                ("%.0f %%" % (100 * r["room_overlap"])) if "room_overlap" in r else "", note.replace("|", "/")))
    L.append("")
    bad = [(k, r) for k in STOREYS for r in results_by_key.get(k, []) if r["status"] in ("not registered", "no zones")]
    L.append("## Pages that could not be registered or coloured")
    L.append("")
    if not bad:
        L.append("None.")
    for k, r in bad:
        L.append("- %s, %s page %d (%s): %s. %s" % (k, Path(r["info"]["file"]).name, r["info"]["page"], r["status"], r.get("reason", ""),
                                                   ("Feature matching log: " + r["reg"]["log"]) if r.get("reg", {}).get("log") else ""))
    L.append("")
    L.append("## Pages of the keyplan register that are not AHU-zoning pages")
    L.append("")
    nz = [(k, r) for k in STOREYS for r in results_by_key.get(k, []) if r["status"] == "not an AHU zoning page"]
    L.append("%d pages of the in-scope keyplan titles carry no 'ZONING' text (department layout, mode, pressure or room-data pages); not processed." % len(nz))
    L.append("")
    for k, r in nz:
        L.append("- %s, %s page %d: %s" % (k, Path(r["info"]["file"]).name, r["info"]["page"], r.get("reason", "")))
    L.append("")
    L.append("## Tags not linked to a zone and other page notes")
    L.append("")
    any_note = False
    for k in STOREYS:
        for r in results_by_key.get(k, []):
            for n in r.get("notes", []):
                any_note = True
                L.append("- %s, %s page %d: %s" % (k, Path(r["info"]["file"]).name, r["info"]["page"], n))
    if not any_note:
        L.append("None.")
    L.append("")
    L.append("## Second read (image-read values)")
    L.append("")
    sr = [s for k in STOREYS for r in results_by_key.get(k, []) for s in [("%s, %s" % (k, Path(r["info"]["file"]).name), x) for x in r.get("second_read", [])]]
    if not sr:
        L.append("None: every zone polygon came from vector fills; tags, legend text and titles come from the text layer.")
    for a, b in sr:
        L.append("- %s: %s" % (a, b))
    L.append("")
    L.append("## Conflicts (listed, not resolved)")
    L.append("")
    if conflicts:
        for c in conflicts:
            L.append("- " + c)
    else:
        L.append("No overlap of zones with different tags between pages above 25 % of the smaller zone.")
    if errors:
        L.append("")
        L.append("## Errors")
        L.append("")
        for e in errors:
            L.append("- " + e)
    L.append("")
    p = root / "out" / "qa" / "booklet_zones_report.md"
    common.atomic_write_text(p, "\n".join(L))
    return p


def run(storeys: Optional[Sequence[str]] = None, pages: Optional[Sequence[str]] = None, overlays: bool = True, root: Optional[Path] = None) -> dict:
    root = root or common.project_root()
    keys = list(storeys) if storeys else STOREYS
    infos = discover_pages(root, keys)
    if pages:
        want = set(pages)
        infos = [i for i in infos if ("%s:%d" % (Path(i["file"]).name, i["page"])) in want]
    results_by_key: Dict[str, List[dict]] = defaultdict(list)
    tenders: Dict[str, Tender] = {}
    errors: List[str] = []
    for key in keys:
        try:
            tenders[key] = Tender(key, root)
        except Exception as e:
            errors.append("storey %s: tender data not loaded (%s)" % (key, e))
    ov = root / "out" / "zoning" / WORK_SUBDIR / "overlays" if overlays else None
    for info in infos:
        t = tenders.get(info["key"])
        if t is None:
            continue
        try:
            r = process_page(root, info, t, ov)
        except Exception as e:                      # one bad page must not stop the run
            r = {"info": info, "rows": [], "status": "error", "reason": "%s: %s" % (type(e).__name__, e), "notes": [], "second_read": []}
            errors.append("%s page %d: %s: %s" % (Path(info["file"]).name, info["page"], type(e).__name__, e))
        results_by_key[info["key"]].append(r)
    cover: Dict[str, dict] = {}
    for key, res in results_by_key.items():
        if key in tenders:
            cover[key] = storey_coverage(tenders[key], res)
        rows = [row for r in res for row in r["rows"]]
        reg = root / "out" / "registers"
        common.write_csv_dicts(reg / ("booklet_zones_%s.csv" % key), rows, CSV_FIELDS)
    allr = [r for res in results_by_key.values() for r in res]
    conflicts = find_conflicts(allr)
    rep = write_report(root, results_by_key, tenders, cover, infos, conflicts, errors)
    return {"report": str(rep), "pages": len(infos), "results": results_by_key, "cover": cover, "errors": errors}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="process the AHU-zoning keyplan pages and write the registers and the report")
    r.add_argument("--storeys", default="", help="comma list of storey keys (default: all in scope)")
    r.add_argument("--pages", default="", help='comma list of "<file name>:<page>"')
    r.add_argument("--no-overlays", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        res = run([s for s in a.storeys.split(",") if s] or None, [s for s in a.pages.split(",") if s] or None, not a.no_overlays)
        for key, rs in res["results"].items():
            for x in rs:
                print("%s %s p%d: %s%s" % (key, Path(x["info"]["file"]).name[:40], x["info"]["page"], x["status"],
                                           (" (%d zones, residual %.0f mm, %d inliers)" % (len(x["rows"]), x["reg"]["rms_mm"], x["reg"]["n_inliers"]))
                                           if x.get("reg", {}).get("rms_mm") is not None else (": " + x.get("reason", ""))))
        print("report:", res["report"])
        return 1 if res["errors"] else 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
