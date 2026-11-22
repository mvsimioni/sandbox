"""Method A: closed room regions from the vector linework of the background plan.

Pipeline: wall strokes (chosen by :mod:`linework`) -> door openings closed at the door-swing arcs ->
small leaks snapped shut -> noding (``shapely.union_all``) -> ``shapely.polygonize`` -> each region
takes the room tags that lie inside it (``room_tags_plans.csv``).  The region is the wall-face polygon
(the first drawn line met from inside the room); the centre-line polygon is derived by offsetting the
face by half the measured wall thickness, so both boundary conventions can be tested against the App D captions.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import shapely
import shapely.ops
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.strtree import STRtree

from . import linework as lw

SNAP_TOL_PT = 1.0            # a dangling wall end this close to another wall line is joined to it
DOOR_SNAP_TOL_PT = 3.0       # a door closer end this close to a wall line is joined to it
MIN_FACE_M2 = 0.5
MAX_FACE_M2 = 4000.0


@dataclass
class RoomRegion:
    region_id: int
    polygon: Polygon                      # wall-face polygon, page-view pt
    tags: List[dict] = field(default_factory=list)   # register rows of the tags inside
    leaky: bool = False


def segments_to_lines(seg: np.ndarray) -> np.ndarray:
    """Return shapely LineStrings for an (N, 4) segment array, dropping zero-length ones."""
    ln = np.hypot(seg[:, 2] - seg[:, 0], seg[:, 3] - seg[:, 1])
    seg = seg[ln > 1e-6]
    coords = np.stack([seg[:, :2], seg[:, 2:]], axis=1)
    return shapely.linestrings(coords)


def dangling_ends(seg: np.ndarray, tol: float = lw.JOIN_TOL_PT) -> Tuple[np.ndarray, np.ndarray]:
    """End points (M, 2) that occur once among all segment end points (degree one), and their segment index."""
    pts = np.concatenate([seg[:, :2], seg[:, 2:]])
    keys = np.round(pts / tol).astype(np.int64)
    _, inv, cnt = np.unique(keys, axis=0, return_inverse=True, return_counts=True)
    sel = cnt[inv.ravel()] == 1
    return pts[sel], (np.arange(len(pts)) % len(seg))[sel]


def snap_dangling(seg: np.ndarray, tol: float = SNAP_TOL_PT) -> np.ndarray:
    """Add short connectors from dangling ends to the nearest *other* wall line within ``tol`` pt.

    An end that already lies on another line (a T junction) needs no connector.
    """
    seg = seg[np.hypot(seg[:, 2] - seg[:, 0], seg[:, 3] - seg[:, 1]) > 1e-6]
    ends, own = dangling_ends(seg)
    if len(ends) == 0:
        return np.zeros((0, 4))
    lines = segments_to_lines(seg)
    tree = STRtree(lines)
    pts = shapely.points(ends)
    qi, li = tree.query(pts, predicate="dwithin", distance=tol)
    keep = li != own[qi]
    qi, li = qi[keep], li[keep]
    d = shapely.distance(pts[qi], lines[li])
    best: Dict[int, Tuple[float, int]] = {}
    for q, l, dd in zip(qi, li, d):
        if q not in best or dd < best[q][0]:
            best[q] = (float(dd), int(l))
    out = []
    for q, (dd, l) in best.items():
        if dd < 1e-6:
            continue
        nearest = shapely.ops.nearest_points(pts[q], lines[l])[1]
        out.append((ends[q][0], ends[q][1], nearest.x, nearest.y))
    return np.asarray(out, float).reshape(-1, 4)


def door_closers(arcs: Sequence[lw.Arc], wall_lines: np.ndarray) -> Tuple[np.ndarray, List[dict]]:
    """Closing segments for door openings, one per door-swing arc.

    The hinge is the arc centre; the closed-leaf position is the arc end that lies on the wall line.  Both radii
    (hinge to either arc end) are candidates; the one whose two ends are nearer to wall strokes is the opening.
    Returns the segments and a diagnostic row per arc.
    """
    tree = STRtree(wall_lines)
    closers: List[Tuple[float, float, float, float]] = []
    diag: List[dict] = []
    for a in arcs:
        hinge = shapely.points(a.cx, a.cy)
        best = None
        for p in (a.p0, a.p1):
            pp = shapely.points(p[0], p[1])
            dh = _nearest_dist(tree, wall_lines, hinge)
            dp = _nearest_dist(tree, wall_lines, pp)
            score = dh + dp
            if best is None or score < best[0]:
                best = (score, p, dh, dp)
        score, p, dh, dp = best
        ok = dh <= DOOR_SNAP_TOL_PT and dp <= DOOR_SNAP_TOL_PT
        diag.append({"cx": a.cx, "cy": a.cy, "r_pt": a.r, "span_deg": a.span_deg, "closed": bool(ok),
                     "hinge_to_wall_pt": dh, "tip_to_wall_pt": dp})
        if ok:
            closers.append((a.cx, a.cy, p[0], p[1]))
    return np.asarray(closers, float).reshape(-1, 4), diag


def _nearest_dist(tree: STRtree, lines: np.ndarray, pt) -> float:
    i = tree.nearest(pt)
    return float(shapely.distance(pt, lines[i]))


def build_wall_segments(ss: lw.StrokeSet, widths: Sequence[float], include_arcs: bool = True,
                        arcs: Optional[Sequence[lw.Arc]] = None) -> Tuple[np.ndarray, dict]:
    """Wall segments for the chosen widths plus door closers and snap connectors; returns (segments, diagnostics)."""
    mask = ss.select(widths)
    base = ss.seg[mask]
    base = base[np.hypot(base[:, 2] - base[:, 0], base[:, 3] - base[:, 1]) > 1e-6]
    lines = segments_to_lines(base)
    diag: dict = {"n_wall_segments": int(len(base))}
    parts = [base]
    if include_arcs:
        arcs = arcs if arcs is not None else lw.detect_door_arcs(ss)
        cl, door_diag = door_closers(arcs, lines)
        # join each closer end to the wall when it does not touch it
        diag["n_arcs"] = len(arcs)
        diag["n_doors_closed"] = int(len(cl))
        diag["door_diag"] = door_diag
        parts.append(cl)
        if len(cl):
            allseg = np.concatenate(parts)
            conn = _connect_ends(cl, allseg, DOOR_SNAP_TOL_PT)
            parts.append(conn)
            diag["n_door_connectors"] = int(len(conn))
    seg = np.concatenate(parts)
    sn = snap_dangling(seg, SNAP_TOL_PT)
    diag["n_snap_connectors"] = int(len(sn))
    return np.concatenate([seg, sn]), diag


def _connect_ends(closers: np.ndarray, allseg: np.ndarray, tol: float) -> np.ndarray:
    """Segments joining the ends of the closers to the wall linework when not already touching it."""
    lines = segments_to_lines(allseg)
    tree = STRtree(lines)
    out = []
    for c in closers:
        for (x, y) in ((c[0], c[1]), (c[2], c[3])):
            p = shapely.points(x, y)
            li = tree.query(p, predicate="dwithin", distance=tol)
            cand = [(shapely.distance(p, lines[l]), l) for l in li]
            # ignore the closer itself (distance 0 for both its ends)
            cand = [(d, l) for d, l in cand if d > 1e-6 or not _is_same(allseg[l], c)]
            if not cand:
                continue
            d, l = min(cand)
            if d < 1e-6:
                continue
            q = shapely.ops.nearest_points(p, lines[l])[1]
            out.append((x, y, q.x, q.y))
    return np.asarray(out, float).reshape(-1, 4)


def _is_same(a, b) -> bool:
    return np.allclose(a, b, atol=1e-6)


def polygonise(seg: np.ndarray) -> List[Polygon]:
    """Node the line set and return all bounded faces.

    Coordinates are rounded to 0.01 pt (0.9 mm) first so that end points that coincide in the drawing coincide exactly.
    """
    seg = np.round(seg * 100.0) / 100.0
    lines = segments_to_lines(seg)
    noded = shapely.union_all(lines)
    polys = list(shapely.polygonize([noded]).geoms)
    return polys


def regions_with_tags(polys: Sequence[Polygon], tags: Sequence[dict], area_scale_m2_per_pt2: float) -> List[RoomRegion]:
    """Keep faces of plausible room size and attach the room tags lying inside each."""
    keep = [p for p in polys if MIN_FACE_M2 <= p.area * area_scale_m2_per_pt2 <= MAX_FACE_M2]
    tree = STRtree(keep)
    pts = shapely.points([[t["x_pv"], t["y_pv"]] for t in tags])
    regs: Dict[int, RoomRegion] = {}
    qi, li = tree.query(pts, predicate="within")
    for q, l in zip(qi, li):
        r = regs.get(int(l))
        if r is None:
            r = regs[int(l)] = RoomRegion(int(l), keep[l])
        r.tags.append(tags[q])
    return list(regs.values())


def wall_thickness_pt(polys: Sequence[Polygon], tagged: Sequence[Polygon], area_scale_m2_per_pt2: float,
                      max_pt: float = 8.0) -> Tuple[float, dict]:
    """Estimate wall thickness from the untagged thin faces between wall lines.

    A thin face is a polygon without a room tag whose minimum rotated rectangle is longer than 4x its width and
    whose width is below ``max_pt``.  The median width of those faces is the wall thickness.
    """
    tagged_ids = {id(p) for p in tagged}
    widths = []
    for p in polys:
        if id(p) in tagged_ids or p.area <= 0:
            continue
        mrr = shapely.minimum_rotated_rectangle(p)
        xs, ys = mrr.exterior.coords.xy
        e = [math.hypot(xs[i + 1] - xs[i], ys[i + 1] - ys[i]) for i in range(2)]
        w, l = min(e), max(e)
        if 0.3 < w < max_pt and l > 4 * w and p.area / max(mrr.area, 1e-9) > 0.8:
            widths.append(w)
    if not widths:
        return 0.0, {"n_faces": 0}
    arr = np.asarray(widths)
    return float(np.median(arr)), {"n_faces": int(len(arr)), "p25_pt": float(np.percentile(arr, 25)),
                                   "p75_pt": float(np.percentile(arr, 75))}


BRIDGE_MAX_PT = 27.0   # 2.4 m: widest opening bridged between two facing wall ends
BRIDGE_LATERAL_PT = 1.2


def bridge_openings(seg: np.ndarray, max_gap_pt: float = BRIDGE_MAX_PT, lateral_pt: float = BRIDGE_LATERAL_PT) -> np.ndarray:
    """Bridge gaps between two wall ends that face each other along the same straight line.

    Every dangling end of an axis-aligned segment looks along its segment direction for another dangling
    end of an axis-aligned segment on the same line (within ``lateral_pt`` sideways) at most ``max_gap_pt``
    away, facing it.  Such a pair is the two jambs of an opening and is joined by a straight closer.
    """
    ends = []
    dx = seg[:, 2] - seg[:, 0]
    dy = seg[:, 3] - seg[:, 1]
    ln = np.hypot(dx, dy)
    ok = ln > 1e-6
    pts = np.concatenate([seg[:, :2], seg[:, 2:]])
    keys = np.round(pts / lw.JOIN_TOL_PT).astype(np.int64)
    _, inv, cnt = np.unique(keys, axis=0, return_inverse=True, return_counts=True)
    deg = cnt[inv.ravel()]
    n = len(seg)
    for i in range(n):
        if not ok[i]:
            continue
        ux, uy = dx[i] / ln[i], dy[i] / ln[i]
        if min(abs(ux), abs(uy)) > 0.01:   # axis-aligned only (plan is orthogonal to 0.02 deg)
            continue
        if deg[i] == 1:        # first end dangling: outward direction is -u
            ends.append((seg[i, 0], seg[i, 1], -ux, -uy))
        if deg[i + n] == 1:    # second end dangling: outward direction is +u
            ends.append((seg[i, 2], seg[i, 3], ux, uy))
    if not ends:
        return np.zeros((0, 4))
    E = np.asarray(ends, float)
    P = shapely.points(E[:, :2])
    tree = STRtree(P)
    qi, ti = tree.query(P, predicate="dwithin", distance=max_gap_pt)
    out = {}
    for q, t in zip(qi, ti):
        if q >= t:
            continue
        v = E[t, :2] - E[q, :2]
        dist = math.hypot(*v)
        if dist < 0.3:
            continue
        # q looks toward t and t looks toward q, along one axis, on the same line
        if E[q, 2] * E[t, 2] + E[q, 3] * E[t, 3] > -0.99:
            continue
        along = v[0] * E[q, 2] + v[1] * E[q, 3]
        lateral = abs(v[0] * E[q, 3] - v[1] * E[q, 2])
        if along > 0 and lateral <= lateral_pt:
            out[(q, t)] = (E[q, 0], E[q, 1], E[t, 0], E[t, 1])
    return np.asarray(list(out.values()), float).reshape(-1, 4)


# --------------------------------------------------------------------------- #
# Furniture and door-swing faces: merge small tagless faces bounded by thin lines
# --------------------------------------------------------------------------- #
FURNITURE_MAX_M2 = 8.0       # tagless faces below this and at least half fine pen are furniture or door-swing slivers
FLIGHT_MAX_M2 = 8.0          # no separate larger class: every candidate face is under 8 m2 (a tread, a fitting, a sliver); blobs of them may be larger
THIN_FRACTION_MIN = 0.5
THIN_FRACTION_LARGE = 0.8
WALL_NEAR_PT = 0.45
SAMPLE_STEP_PT = 0.6


def _boundary_samples(face: Polygon) -> np.ndarray:
    """Points spaced ``SAMPLE_STEP_PT`` along the whole boundary (shell and holes) of a polygon."""
    pts = []
    rings = [face.exterior] + list(face.interiors)
    for ring in rings:
        n = max(int(ring.length / SAMPLE_STEP_PT), 4)
        d = np.linspace(0, ring.length, n, endpoint=False)
        pts.append(shapely.get_coordinates(shapely.line_interpolate_point(ring, d)))
    return np.concatenate(pts)


BLOB_MAX_M2 = 150.0


def merge_small_faces(polys: Sequence[Polygon], tags: Sequence[dict], wall_lines: np.ndarray,
                      area_scale_m2_per_pt2: float) -> Tuple[List[Polygon], dict]:
    """Merge furniture, door-swing, finish and stair-tread faces back into the room they sit in.

    The faces come from noding every black line (heavy wall strokes and the fine-pen furniture, door leaves and
    partitions).  A *candidate* is a tagless face bounded mostly by fine pen: under 8 m2 with at least half of its boundary
    fine pen, or under 40 m2 with at least 80 % (a stair flight, a fitted unit).  A boundary point counts as heavy wall when a
    heavy-wall line (``wall_lines``, an array of shapely lines) is within 0.45 pt, so wall-thickness faces (bounded by heavy
    strokes) are never candidates.  Candidates that touch each other through fine-pen boundary form blobs (a row of
    treads); a blob of at most 150 m2 is merged into the non-candidate face (a tagged room, a bigger space) with which it
    shares the most fine-pen boundary.  A heavy wall is never dissolved.
    """
    faces = list(polys)
    wall_tree = STRtree(wall_lines)
    tag_pts = shapely.points([[t["x_pv"], t["y_pv"]] for t in tags]) if tags else np.array([], dtype=object)
    tree = STRtree(faces)
    has_tag = np.zeros(len(faces), bool)
    if len(tag_pts):
        _, li = tree.query(tag_pts, predicate="within")
        has_tag[np.unique(li)] = True
    area = shapely.area(faces) * area_scale_m2_per_pt2
    is_cand = np.zeros(len(faces), bool)
    thin_pts: Dict[int, np.ndarray] = {}
    for i in np.where((~has_tag) & (area < FLIGHT_MAX_M2))[0]:
        sp = shapely.points(_boundary_samples(faces[i]))
        near_wall = np.zeros(len(sp), bool)
        qi, _ = wall_tree.query(sp, predicate="dwithin", distance=WALL_NEAR_PT)
        near_wall[np.unique(qi)] = True
        need = THIN_FRACTION_MIN if area[i] < FURNITURE_MAX_M2 else THIN_FRACTION_LARGE
        if (1.0 - near_wall.mean()) >= need:
            is_cand[i] = True
            thin_pts[int(i)] = sp[~near_wall]
    # thin-boundary adjacency counts
    adj: Dict[int, Dict[int, int]] = {}
    for i, pts in thin_pts.items():
        if len(pts) == 0:
            continue
        _, fj = tree.query(pts, predicate="dwithin", distance=0.3)
        cnt: Dict[int, int] = defaultdict(int)
        for j in fj:
            if j != i:
                cnt[int(j)] += 1
        adj[i] = cnt
    parent = list(range(len(faces)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, cnt in adj.items():
        for j in cnt:
            if is_cand[j]:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj
    blobs: Dict[int, List[int]] = defaultdict(list)
    for i in np.where(is_cand)[0]:
        blobs[find(int(i))].append(int(i))
    target_of: Dict[int, int] = {}
    n_blobs_unplaced = 0
    for root, members in blobs.items():
        if sum(area[m] for m in members) > BLOB_MAX_M2:
            n_blobs_unplaced += 1
            continue
        score: Dict[int, int] = defaultdict(int)
        for m in members:
            for j, c in adj.get(m, {}).items():
                if not is_cand[j]:
                    score[j] += c
        if not score:
            n_blobs_unplaced += 1
            continue
        best = max(score, key=lambda j: (has_tag[j], score[j], area[j]))
        for m in members:
            target_of[m] = best
    groups: Dict[int, List[int]] = defaultdict(list)
    for i in range(len(faces)):
        groups[target_of.get(i, i)].append(i)
    new_faces: List[Polygon] = []
    for root, members in groups.items():
        if len(members) == 1:
            new_faces.append(faces[members[0]])
        else:
            u = shapely.union_all([faces[m] for m in members])
            new_faces.extend(list(u.geoms) if u.geom_type == "MultiPolygon" else [u])
    return new_faces, {"merged_faces": int(len(target_of)), "faces_after": len(new_faces), "n_candidate_faces": int(is_cand.sum()),
                       "n_blobs": len(blobs), "n_blobs_unplaced": n_blobs_unplaced}


# --------------------------------------------------------------------------- #
# Wall thickness and centre-line polygons
# --------------------------------------------------------------------------- #
MAX_WALL_M = 0.6


def _room_like(faces: Sequence[Polygon], scale_m2: float, min_short_m: float = 1.0, min_m2: float = 1.5) -> np.ndarray:
    """Boolean mask of faces big and compact enough to be a room (not a wall body or a sliver)."""
    k = math.sqrt(scale_m2)
    out = np.zeros(len(faces), bool)
    for i, f in enumerate(faces):
        if f.area * scale_m2 < min_m2:
            continue
        mrr = shapely.minimum_rotated_rectangle(f)
        if mrr.geom_type != "Polygon":
            continue
        xs, ys = mrr.exterior.coords.xy
        short = min(math.hypot(xs[a + 1] - xs[a], ys[a + 1] - ys[a]) for a in range(2)) * k
        out[i] = short >= min_short_m
    return out


def local_wall_gaps(regions: Sequence[RoomRegion], faces: Sequence[Polygon], scale_m2: float) -> Tuple[np.ndarray, dict]:
    """Per-region wall thickness (pt): median distance across the wall to the next room-like face.

    For each sample point on a region boundary the nearest *other* room-like face within 0.6 m is found; the
    distance is the thickness of the wall between the two rooms.  Regions with no neighbour take the global median.
    """
    k = math.sqrt(scale_m2)
    max_pt = MAX_WALL_M / k
    room_like = _room_like(faces, scale_m2)
    cand = [f for f, ok in zip(faces, room_like) if ok]
    cand_arr = np.asarray(cand, dtype=object)
    index_of = {id(f): i for i, f in enumerate(cand)}
    tree = STRtree(cand)
    allg: List[float] = []
    per: List[float] = []
    for r in regions:
        samp = shapely.points(_boundary_samples(r.polygon))
        me = index_of.get(id(r.polygon), -1)
        ii, jj = tree.query(samp, predicate="dwithin", distance=max_pt)
        keep = jj != me
        ii, jj = ii[keep], jj[keep]
        if len(ii) == 0:
            per.append(float("nan"))
            continue
        d = shapely.distance(samp[ii], cand_arr[jj])
        best = np.full(len(samp), np.inf)
        np.minimum.at(best, ii, d)
        vals = best[np.isfinite(best) & (best > 0.2)]
        per.append(float(np.median(vals)) if len(vals) >= 5 else float("nan"))
        allg.extend(vals.tolist())
    per = np.asarray(per)
    glob = float(np.median(allg)) if allg else 0.0
    per = np.where(np.isnan(per), glob, per)
    stats = {"n_gap_samples": len(allg), "global_median_pt": glob,
             "global_median_mm": glob * k * 1000.0,
             "p25_mm": float(np.percentile(allg, 25) * k * 1000.0) if allg else 0.0,
             "p75_mm": float(np.percentile(allg, 75) * k * 1000.0) if allg else 0.0}
    return per, stats


def centreline_polygon(region: Polygon, half_gap_pt: float, others: STRtree, other_geoms: Sequence[Polygon]) -> Polygon:
    """Wall-face polygon grown by half the local wall thickness, never into another room-like face."""
    g = region.buffer(half_gap_pt, join_style="mitre", mitre_limit=2.0)
    for j in others.query(g, predicate="intersects"):
        o = other_geoms[j]
        if o is region or o.intersection(region).area > 0.5 * o.area:
            continue      # the region itself (or its counterpart from the other method)
        g = g.difference(o)
    if g.geom_type == "MultiPolygon":
        g = max(g.geoms, key=lambda x: x.area)
    return g


# --------------------------------------------------------------------------- #
# High-level: measure all rooms of a plan page
# --------------------------------------------------------------------------- #
HEAVY_WIDTHS = (0.6, 0.84, 1.08, 1.32, 2.76)   # black strokes, pt: wall outlines (chosen from the class analysis)
THIN_WIDTHS = (0.24, 0.36, 0.48)               # black fine-pen strokes: furniture, door leaves, partitions


@dataclass
class VectorResult:
    regions: List[RoomRegion]
    faces: List[Polygon]
    wall_gaps_pt: np.ndarray
    wall_segments: np.ndarray       # heavy strokes + door closers + bridges + snaps (the "W" set)
    thin_segments: np.ndarray
    diagnostics: dict


def measure_rooms(ss: lw.StrokeSet, tags: Sequence[dict], area_scale_m2_per_pt2: float,
                  heavy_widths: Sequence[float] = HEAVY_WIDTHS, thin_widths: Sequence[float] = THIN_WIDTHS,
                  blocker_tags: Sequence[dict] = ()) -> VectorResult:
    """Run method A on a stroke set and the room-tag seeds.

    ``blocker_tags`` are shaft and riser codes (lift cars, risers): a face holding one is a space of its own, never merged
    into a neighbouring room as furniture.
    """
    diag: dict = {"heavy_widths_pt": list(heavy_widths), "thin_widths_pt": list(thin_widths)}
    arcs = lw.detect_door_arcs(ss)
    wseg, wdiag = build_wall_segments(ss, heavy_widths, include_arcs=True, arcs=arcs)
    diag.update({k: v for k, v in wdiag.items() if k != "door_diag"})
    diag["door_diag"] = wdiag.get("door_diag", [])
    br = bridge_openings(wseg)
    diag["n_bridges"] = int(len(br))
    wseg = np.concatenate([wseg, br])
    sn = snap_dangling(wseg, SNAP_TOL_PT)
    diag["n_bridge_snap_connectors"] = int(len(sn))
    wseg = np.concatenate([wseg, sn])
    thin = ss.seg[ss.select(thin_widths)]
    allseg = np.concatenate([wseg, thin])
    polys = polygonise(allseg)
    diag["n_faces_raw"] = len(polys)
    wlines = segments_to_lines(wseg)
    faces, md = merge_small_faces(polys, list(tags) + list(blocker_tags), wlines, area_scale_m2_per_pt2)
    md["n_blocker_tags"] = len(blocker_tags)
    diag.update(md)
    regions = regions_with_tags(faces, tags, area_scale_m2_per_pt2)
    gaps, gstat = local_wall_gaps(regions, faces, area_scale_m2_per_pt2)
    diag["wall_gap"] = gstat
    diag["n_regions"] = len(regions)
    diag["n_regions_multi_tag"] = sum(1 for r in regions if len(r.tags) > 1)
    diag["n_tags_assigned"] = sum(len(r.tags) for r in regions)
    return VectorResult(regions, faces, gaps, wseg, thin, diag)
