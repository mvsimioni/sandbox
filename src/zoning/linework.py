"""Vector linework of a plan page: stroke extraction, wall-class analysis, door arcs and door closure.

All geometry here is in page-view pt (see the package docstring).  The wall classes and
the door closers computed here are shared by method A (:mod:`rooms_vector`) and method B
(:mod:`rooms_raster`), so the two methods start from the same strokes.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

CURVE_STEPS = 8
ARC_MAX_SEG_PT = 6.0          # door-swing arcs are drawn as chains of short segments
ARC_MIN_SEGS = 4
ARC_MIN_RADIUS_PT = 5.0       # 0.44 m at 1:250
ARC_MAX_RADIUS_PT = 30.0      # 2.6 m
ARC_MAX_RMS_PT = 0.35
NODE_JOIN_PT = 0.12           # segment ends this close are one node of an arc chain
ARC_MAX_TURN_DEG = 50.0       # a chain is cut where the path turns by more than this between two consecutive pieces
ARC_MIN_SPAN_DEG = 35.0
ARC_MAX_SPAN_DEG = 200.0
JOIN_TOL_PT = 0.12            # end points closer than this are the same node


@dataclass
class StrokeSet:
    """All stroked line segments of a page in page-view pt, with their style."""

    seg: np.ndarray        # (N, 4) x0, y0, x1, y1
    style: np.ndarray      # (N,) index into ``styles``
    styles: List[Tuple[Tuple[float, float, float], float]]  # (rgb, width pt)
    curve: np.ndarray      # (N,) bool: segment is a flattened Bezier curve piece
    fills: List[dict] = field(default_factory=list)  # filled polygons (rgb, pts)

    def select(self, widths: Iterable[float], colour=(0.0, 0.0, 0.0), tol=0.011) -> np.ndarray:
        """Boolean mask of segments of one colour whose width is within ``tol`` of any in ``widths``."""
        ok = np.zeros(len(self.styles), bool)
        for i, (c, w) in enumerate(self.styles):
            if all(abs(a - b) < 0.011 for a, b in zip(c, colour)) and any(abs(w - x) < tol for x in widths):
                ok[i] = True
        return ok[self.style]


def _bezier_points(p0, p1, p2, p3, n=CURVE_STEPS):
    t = np.linspace(0, 1, n + 1)[:, None]
    p0, p1, p2, p3 = (np.asarray(p, float) for p in (p0, p1, p2, p3))
    return ((1 - t) ** 3) * p0 + 3 * ((1 - t) ** 2) * t * p1 + 3 * (1 - t) * (t ** 2) * p2 + (t ** 3) * p3


def extract_strokes(page) -> StrokeSet:
    """Extract every stroked path of ``page`` (page content only) as straight segments in page-view pt."""
    m = page.rotation_matrix
    a, b, c, d, e, f = m.a, m.b, m.c, m.d, m.e, m.f

    def tr(p):
        return (a * p[0] + c * p[1] + e, b * p[0] + d * p[1] + f)

    segs: List[Tuple[float, float, float, float]] = []
    sty: List[int] = []
    crv: List[bool] = []
    styles: Dict[Tuple[Tuple[float, float, float], float], int] = {}
    fills: List[dict] = []
    for p in page.get_cdrawings():
        typ = p["type"]
        col = p.get("color")
        if typ in ("s", "fs") and col is not None:
            key = (tuple(round(float(v), 2) for v in col), round(float(p.get("width") or 0.0), 2))
            sid = styles.setdefault(key, len(styles))
            for it in p["items"]:
                k = it[0]
                if k == "l":
                    p0, p1 = tr(it[1]), tr(it[2])
                    segs.append((p0[0], p0[1], p1[0], p1[1])); sty.append(sid); crv.append(False)
                elif k == "re":
                    r = it[1]
                    pts = [tr((r[0], r[1])), tr((r[2], r[1])), tr((r[2], r[3])), tr((r[0], r[3]))]
                    for i in range(4):
                        q0, q1 = pts[i], pts[(i + 1) % 4]
                        segs.append((q0[0], q0[1], q1[0], q1[1])); sty.append(sid); crv.append(False)
                elif k == "qu":
                    q = it[1]
                    pts = [tr(q[0]), tr(q[1]), tr(q[3]), tr(q[2])]
                    for i in range(4):
                        q0, q1 = pts[i], pts[(i + 1) % 4]
                        segs.append((q0[0], q0[1], q1[0], q1[1])); sty.append(sid); crv.append(False)
                elif k == "c":
                    pts = _bezier_points(tr(it[1]), tr(it[2]), tr(it[3]), tr(it[4]))
                    for i in range(len(pts) - 1):
                        segs.append((pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])); sty.append(sid); crv.append(True)
        if typ in ("f", "fs") and p.get("fill") is not None:
            fc = tuple(round(float(v), 2) for v in p["fill"])
            if fc != (1.0, 1.0, 1.0):
                # one ring per sub-path: a new ring starts when a line does not begin where the previous one ended
                ring: List[Tuple[float, float]] = []

                def flush():
                    if len(ring) >= 3:
                        fills.append({"rgb": fc, "pts": list(ring)})
                    ring.clear()

                for it in p["items"]:
                    if it[0] == "l":
                        a_, b_ = tr(it[1]), tr(it[2])
                        if ring and math.hypot(ring[-1][0] - a_[0], ring[-1][1] - a_[1]) > 1e-3:
                            flush()
                        if not ring:
                            ring.append(a_)
                        ring.append(b_)
                    elif it[0] == "re":
                        flush()
                        r = it[1]
                        ring.extend([tr((r[0], r[1])), tr((r[2], r[1])), tr((r[2], r[3])), tr((r[0], r[3]))])
                        flush()
                    elif it[0] == "qu":
                        flush()
                        q = it[1]
                        ring.extend([tr(q[0]), tr(q[1]), tr(q[3]), tr(q[2])])
                        flush()
                flush()
    inv = [None] * len(styles)
    for k, i in styles.items():
        inv[i] = k
    return StrokeSet(np.asarray(segs, float).reshape(-1, 4), np.asarray(sty, np.int32), inv, np.asarray(crv, bool), fills)


# --------------------------------------------------------------------------- #
# Wall-class analysis
# --------------------------------------------------------------------------- #
def class_table(ss: StrokeSet, bbox: Optional[Tuple[float, float, float, float]] = None) -> List[dict]:
    """Per (colour, width) statistics used to choose the wall strokes.

    ``axis_share`` is the length-weighted share of segments within 0.5 deg of the page axes (the
    plan is orthogonal to within 0.02 deg), ``mean_len`` the mean segment length in pt and
    ``joined_share`` the share of segment end points that coincide (within 0.12 pt) with an end
    point of another segment of the same class, which is high for wall runs and low for text-like marks.
    """
    out = []
    seg = ss.seg
    if bbox is not None:
        mx = (seg[:, 0] + seg[:, 2]) / 2
        my = (seg[:, 1] + seg[:, 3]) / 2
        inside = (mx >= bbox[0]) & (mx <= bbox[2]) & (my >= bbox[1]) & (my <= bbox[3])
    else:
        inside = np.ones(len(seg), bool)
    for i, (col, w) in enumerate(ss.styles):
        sel = (ss.style == i) & inside & (~ss.curve)
        n = int(sel.sum())
        if n == 0:
            continue
        s = seg[sel]
        dx, dy = s[:, 2] - s[:, 0], s[:, 3] - s[:, 1]
        ln = np.hypot(dx, dy)
        ang = np.degrees(np.arctan2(np.abs(dy), np.abs(dx)))
        axis = (ang < 0.5) | (ang > 89.5)
        tot = float(ln.sum())
        keys = np.round(np.concatenate([s[:, :2], s[:, 2:]]) / JOIN_TOL_PT).astype(np.int64)
        _, inv, cnt = np.unique(keys, axis=0, return_inverse=True, return_counts=True)
        joined = float((cnt[inv.ravel()] > 1).mean())
        out.append({"colour": col, "width_pt": w, "segments": n, "total_len_pt": round(tot, 1),
                    "mean_len_pt": round(tot / n, 2), "axis_share": round(float(ln[axis].sum() / max(tot, 1e-9)), 3),
                    "joined_share": round(joined, 3)})
    out.sort(key=lambda r: -r["total_len_pt"])
    return out


# --------------------------------------------------------------------------- #
# Door-swing arcs
# --------------------------------------------------------------------------- #
@dataclass
class Arc:
    cx: float
    cy: float
    r: float
    p0: Tuple[float, float]
    p1: Tuple[float, float]
    span_deg: float
    rms: float
    seg_idx: Tuple[int, ...] = ()      # indices (into the stroke set) of the chain of short segments that forms the arc


def _fit_circle(pts: np.ndarray):
    x, y = pts[:, 0], pts[:, 1]
    A = np.c_[2 * x, 2 * y, np.ones(len(x))]
    sol, *_ = np.linalg.lstsq(A, x ** 2 + y ** 2, rcond=None)
    cx, cy = sol[0], sol[1]
    r2 = sol[2] + cx ** 2 + cy ** 2
    if r2 <= 0:
        return None
    r = math.sqrt(r2)
    rms = float(np.sqrt(np.mean((np.hypot(x - cx, y - cy) - r) ** 2)))
    return cx, cy, r, rms


def detect_door_arcs(ss: StrokeSet, widths=(0.24, 0.36), colour=(0.0, 0.0, 0.0)) -> List[Arc]:
    """Find door-swing arcs: chains of short segments (or flattened curves) that fit a circle.

    Segments of the given style that are shorter than ``ARC_MAX_SEG_PT`` are chained through shared
    end points; a chain of at least ``ARC_MIN_SEGS`` pieces is an arc when the least-squares circle has
    a radius of 0.44-2.6 m, an RMS error below 0.35 pt, and subtends 35-200 deg.
    """
    mask = ss.select(widths, colour)
    seg = ss.seg
    ln = np.hypot(seg[:, 2] - seg[:, 0], seg[:, 3] - seg[:, 1])
    idx = np.where(mask & (ln <= ARC_MAX_SEG_PT) & (ln > 0.05))[0]
    node: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    node_xy: Dict[Tuple[int, int], List[Tuple[float, float]]] = defaultdict(list)
    for i in idx:
        for k in (0, 2):
            key = (int(round(seg[i, k] / 0.15)), int(round(seg[i, k + 1] / 0.15)))
            node[key].append(int(i))
            node_xy[key].append((float(seg[i, k]), float(seg[i, k + 1])))
    # neighbour-tolerant joining: also look in adjacent cells
    parent = {int(i): int(i) for i in idx}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def end_toward(i: int, nx: float, ny: float):
        """(far end, near end) of segment i, the near end being the one closer to the node (nx, ny)."""
        a, b = seg[i, :2], seg[i, 2:]
        if math.hypot(a[0] - nx, a[1] - ny) <= math.hypot(b[0] - nx, b[1] - ny):
            return b, a
        return a, b

    for (kx, ky), lst in list(node.items()):
        cand = list(lst)
        ref = np.mean(np.asarray(node_xy[(kx, ky)]), axis=0)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx or dy:
                    # an end in a neighbouring cell joins only when it is really at this node (grid cells are 0.15 pt: two arcs
                    # that approach a cusp pass within 0.2 pt of each other without touching)
                    for i_, (x_, y_) in zip(node.get((kx + dx, ky + dy), []), node_xy.get((kx + dx, ky + dy), [])):
                        if math.hypot(x_ - ref[0], y_ - ref[1]) <= NODE_JOIN_PT:
                            cand.append(i_)
        uniq = sorted(set(cand))
        if len(uniq) > 2:  # a branch point is not part of a single arc
            continue
        if len(uniq) == 2:
            # two arcs that meet at a cusp (a double door: both leaves close on the same point) are two arcs, not one:
            # the path turns back on itself there, a smooth arc turns by a few degrees only
            i, j = uniq
            nx, ny = float(ref[0]), float(ref[1])
            fi, ni = end_toward(i, nx, ny)
            fj, nj = end_toward(j, nx, ny)
            vi = np.array([ni[0] - fi[0], ni[1] - fi[1]])
            vj = np.array([fj[0] - nj[0], fj[1] - nj[1]])
            li, lj = np.hypot(*vi), np.hypot(*vj)
            if li > 1e-9 and lj > 1e-9:
                turn = math.degrees(math.acos(max(-1.0, min(1.0, float(vi @ vj) / (li * lj)))))
                if turn > ARC_MAX_TURN_DEG:
                    continue
        for j in cand[1:]:
            ra, rb = find(cand[0]), find(j)
            if ra != rb:
                parent[ra] = rb
    groups: Dict[int, List[int]] = defaultdict(list)
    for i in idx:
        groups[find(int(i))].append(int(i))
    arcs: List[Arc] = []
    for g in groups.values():
        if len(g) < ARC_MIN_SEGS:
            continue
        pts = np.unique(np.round(np.concatenate([seg[g][:, :2], seg[g][:, 2:]]), 3), axis=0)
        fit = _fit_circle(pts)
        if fit is None:
            continue
        cx, cy, r, rms = fit
        if not (ARC_MIN_RADIUS_PT <= r <= ARC_MAX_RADIUS_PT) or rms > ARC_MAX_RMS_PT:
            continue
        ang = np.degrees(np.arctan2(pts[:, 1] - cy, pts[:, 0] - cx))
        # span from the largest angular gap
        a = np.sort(ang)
        gaps = np.diff(np.r_[a, a[0] + 360])
        k = int(np.argmax(gaps))
        span = 360 - float(gaps[k])
        if not (ARC_MIN_SPAN_DEG <= span <= ARC_MAX_SPAN_DEG):
            continue
        a0 = a[(k + 1) % len(a)]
        a1 = a[k]
        p0 = (cx + r * math.cos(math.radians(a0)), cy + r * math.sin(math.radians(a0)))
        p1 = (cx + r * math.cos(math.radians(a1)), cy + r * math.sin(math.radians(a1)))
        arcs.append(Arc(cx, cy, r, p0, p1, span, rms, tuple(int(x) for x in g)))
    return arcs


# --------------------------------------------------------------------------- #
# Long thin runs (partitions drawn with fine pens)
# --------------------------------------------------------------------------- #
def long_axis_runs(seg: np.ndarray, min_len_pt: float, gap_pt: float = 0.3, axis_tol_deg: float = 0.5) -> np.ndarray:
    """Merge collinear, touching axis-aligned segments and keep the runs at least ``min_len_pt`` long.

    Fine-pen partition lines are drawn as many short pieces; furniture and symbols never produce a long
    straight run.  Returns the kept runs as (K, 4) segments.
    """
    dx = seg[:, 2] - seg[:, 0]
    dy = seg[:, 3] - seg[:, 1]
    ang = np.degrees(np.arctan2(np.abs(dy), np.abs(dx)))
    out: List[Tuple[float, float, float, float]] = []
    for horizontal in (True, False):
        sel = ang < axis_tol_deg if horizontal else ang > 90 - axis_tol_deg
        s = seg[sel]
        if len(s) == 0:
            continue
        if horizontal:
            pos = (s[:, 1] + s[:, 3]) / 2
            a = np.minimum(s[:, 0], s[:, 2]); b = np.maximum(s[:, 0], s[:, 2])
        else:
            pos = (s[:, 0] + s[:, 2]) / 2
            a = np.minimum(s[:, 1], s[:, 3]); b = np.maximum(s[:, 1], s[:, 3])
        key = np.round(pos / 0.15).astype(np.int64)
        order = np.lexsort((a, key))
        key, a, b, pos = key[order], a[order], b[order], pos[order]
        i = 0
        n = len(key)
        while i < n:
            j = i
            lo, hi = a[i], b[i]
            p = pos[i]
            while j + 1 < n and key[j + 1] - key[i] <= 1 and a[j + 1] <= hi + gap_pt:
                j += 1
                hi = max(hi, b[j])
            if hi - lo >= min_len_pt:
                out.append((lo, p, hi, p) if horizontal else (p, lo, p, hi))
            i = j + 1
    return np.asarray(out, float).reshape(-1, 4)
