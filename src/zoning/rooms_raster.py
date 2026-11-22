"""Method B: seeded region growing on the wall raster.

No segmentation-model weights are available, so the second method is classical: the same strokes that method A
uses (heavy wall strokes, door closers, bridges and fine-pen lines) are rasterised in tiles at 1 px <= 10 mm, the free
pixels are labelled with 4-connectivity, and each room-tag seed claims its connected component.  Components that
hold several seeds are split by a watershed on the distance to the wall raster with the seeds as markers (so
neighbouring seeds act as barriers); small unseeded components bounded by fine-pen lines (furniture, door swings)
are absorbed by the neighbour they share most fine-pen boundary with, as in method A.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import cv2
import shapely
from scipy import ndimage as ndi
from shapely.geometry import Polygon

TILE_PX = 2048
MAX_MM_PER_PX = 10.0
FURNITURE_MAX_M2 = 8.0
FLIGHT_MAX_M2 = 8.0
THIN_FRACTION_MIN = 0.5
THIN_FRACTION_LARGE = 0.8
BLOB_MAX_M2 = 150.0


def pixels_per_pt(mm_per_pt: float, target_mm_per_px: float = 8.0) -> float:
    """Raster scale (px per pt) giving at most ``MAX_MM_PER_PX`` mm per pixel (default target 8 mm)."""
    s = mm_per_pt / min(target_mm_per_px, MAX_MM_PER_PX)
    assert mm_per_pt / s <= MAX_MM_PER_PX + 1e-9
    return s


@dataclass
class Raster:
    x0: float
    y0: float
    S: float
    shape: Tuple[int, int]
    wall: np.ndarray    # uint8, heavy strokes + closers + bridges
    thin: np.ndarray    # uint8, fine-pen strokes (excluding where wall is set)

    @property
    def mm_per_px(self) -> float:
        return float("nan")


def render_tiles(segs: np.ndarray, roi: Tuple[float, float, float, float], S: float, tile_px: int = TILE_PX,
                 out: Optional[np.ndarray] = None) -> np.ndarray:
    """Rasterise segments (page-view pt) as 1 px lines into ``out`` (uint8), one tile at a time."""
    x0, y0, x1, y1 = roi
    W = int(math.ceil((x1 - x0) * S))
    H = int(math.ceil((y1 - y0) * S))
    if out is None:
        out = np.zeros((H, W), np.uint8)
    sx = np.minimum(segs[:, 0], segs[:, 2]); ex = np.maximum(segs[:, 0], segs[:, 2])
    sy = np.minimum(segs[:, 1], segs[:, 3]); ey = np.maximum(segs[:, 1], segs[:, 3])
    SH = 4
    for ty in range(0, H, tile_px):
        for tx in range(0, W, tile_px):
            tw, th = min(tile_px, W - tx), min(tile_px, H - ty)
            # tile bounds in pt, with a 1 px margin so lines crossing the edge are drawn
            bx0, by0 = x0 + (tx - 1) / S, y0 + (ty - 1) / S
            bx1, by1 = x0 + (tx + tw + 1) / S, y0 + (ty + th + 1) / S
            sel = np.nonzero((ex >= bx0) & (sx <= bx1) & (ey >= by0) & (sy <= by1))[0]
            if len(sel) == 0:
                continue
            tile = np.zeros((th, tw), np.uint8)
            s = segs[sel]
            px = ((s[:, [0, 2]] - x0) * S - tx + 0.5) * (1 << SH)
            py = ((s[:, [1, 3]] - y0) * S - ty + 0.5) * (1 << SH)
            pxi = np.round(px).astype(np.int64)
            pyi = np.round(py).astype(np.int64)
            for a, b, c, d in zip(pxi[:, 0], pyi[:, 0], pxi[:, 1], pyi[:, 1]):
                cv2.line(tile, (int(a), int(b)), (int(c), int(d)), 255, 1, cv2.LINE_8, SH)
            out[ty:ty + th, tx:tx + tw] |= tile
    return out


def render_fills(polys: Sequence[Polygon], roi: Tuple[float, float, float, float], S: float, out: np.ndarray, tile_px: int = TILE_PX) -> np.ndarray:
    """Fill polygons (page-view pt, shell only) into ``out`` (uint8, 255), one tile at a time: the black wall bands."""
    x0, y0, _, _ = roi
    H, W = out.shape
    SH = 4
    for ty in range(0, H, tile_px):
        for tx in range(0, W, tile_px):
            tw, th = min(tile_px, W - tx), min(tile_px, H - ty)
            bx0, by0 = x0 + (tx - 1) / S, y0 + (ty - 1) / S
            bx1, by1 = x0 + (tx + tw + 1) / S, y0 + (ty + th + 1) / S
            tile = None
            for g in polys:
                gx0, gy0, gx1, gy1 = g.bounds
                if gx1 < bx0 or gx0 > bx1 or gy1 < by0 or gy0 > by1:
                    continue
                if tile is None:
                    tile = np.zeros((th, tw), np.uint8)
                pts = np.asarray(g.exterior.coords, float)
                px = np.round(((pts[:, 0] - x0) * S - tx + 0.5) * (1 << SH)).astype(np.int32)
                py = np.round(((pts[:, 1] - y0) * S - ty + 0.5) * (1 << SH)).astype(np.int32)
                cv2.fillPoly(tile, [np.stack([px, py], axis=1)], 255, cv2.LINE_8, SH)
            if tile is not None:
                out[ty:ty + th, tx:tx + tw] |= tile
    return out


def _pair_contacts(lab: np.ndarray, wall: np.ndarray, offsets=(2, 3, 4), chunk_rows: int = 1024) -> Dict[Tuple[int, int], List[int]]:
    """Contact counts between components across 1-3 px barriers: ``{(a, b): [thin_count, wall_count]}``.

    Pixels ``k`` apart (k = 2..4) with only barrier pixels between them and different non-zero labels are in contact;
    the contact is "wall" when any in-between pixel is a heavy-wall pixel and "thin" otherwise.  Rows are processed in
    chunks and the transposed pass works on a contiguous copy so the 300 Mpx raster stays cache friendly.
    """
    acc: Dict[Tuple[int, int], List[int]] = defaultdict(lambda: [0, 0])
    n = int(lab.max()) + 1
    maxk = max(offsets)
    for axis in (1, 0):
        L = lab if axis == 1 else np.ascontiguousarray(lab.T)
        Wm = wall if axis == 1 else np.ascontiguousarray(wall.T)
        H, Wd = L.shape
        for r0 in range(0, H, chunk_rows):
            Lc = L[r0:r0 + chunk_rows]
            Wc = Wm[r0:r0 + chunk_rows]
            for k in offsets:
                a = Lc[:, :-k]
                b = Lc[:, k:]
                sel = (a > 0) & (b > 0) & (a != b)
                if not sel.any():
                    continue
                for j in range(1, k):       # all pixels in between must be barrier
                    sel &= Lc[:, j:Wd - k + j] == 0
                r, c = np.nonzero(sel)
                if len(r) == 0:
                    continue
                la, lb = a[r, c].astype(np.int64), b[r, c].astype(np.int64)
                wallhit = np.zeros(len(r), bool)
                for j in range(1, k):
                    wallhit |= Wc[r, c + j] > 0
                lo, hi = np.minimum(la, lb), np.maximum(la, lb)
                code = lo * n + hi
                for w, flag in ((wallhit, 1), (~wallhit, 0)):
                    if not w.any():
                        continue
                    u, cnt = np.unique(code[w], return_counts=True)
                    for uu, cc in zip(u, cnt):
                        acc[(int(uu // n), int(uu % n))][flag] += int(cc)
    return acc


def _boundary_pixel_counts(lab: np.ndarray, wall: np.ndarray, n: int, chunk_rows: int = 1024) -> Tuple[np.ndarray, np.ndarray]:
    """Per component: number of its pixels 4-adjacent to a heavy-wall pixel and to a fine-pen-only pixel."""
    wall_adj = np.zeros(n, np.int64)
    thin_adj = np.zeros(n, np.int64)
    H, W = lab.shape
    for r0 in range(0, H, chunk_rows):
        r1 = min(r0 + chunk_rows, H)
        L = lab[r0:r1]
        Wm = wall[r0:r1] > 0
        for dr, dc in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            # neighbour of each pixel in direction (dr, dc), clipped to the chunk's rows with a one-row halo
            ra, rb = max(r0 + dr, 0), min(r1 + dr, H)
            ca, cb_ = max(dc, 0), W + min(dc, 0)
            sub = L[(ra - dr - r0):(rb - dr - r0), (ca - dc):(cb_ - dc)]
            nb_lab = lab[ra:rb, ca:cb_]
            nb_wall = wall[ra:rb, ca:cb_] > 0
            sel = (sub > 0) & (nb_lab == 0)
            if not sel.any():
                continue
            wall_adj += np.bincount(sub[sel & nb_wall], minlength=n)
            thin_adj += np.bincount(sub[sel & ~nb_wall], minlength=n)
    return wall_adj, thin_adj


@dataclass
class SeedRegion:
    tag_index: int
    pixels: int
    area_m2: float
    perimeter_m: float
    polygon: Optional[Polygon]
    component: int
    shared_component: bool      # seed shares its component with another seed (split by watershed)
    leaked: bool                # component touches the ROI border (open to the outside)
    notes: str = ""


def grow_regions(wall: np.ndarray, thin: np.ndarray, roi: Tuple[float, float, float, float], S: float,
                 seeds_pv: np.ndarray, m2_per_px: float, pt_per_px: float,
                 max_region_m2: float = 4000.0, n_room_seeds: Optional[int] = None) -> Tuple[List[SeedRegion], dict]:
    """Seeded region growing from tag seeds (page-view pt) on the wall and thin rasters.

    The first ``n_room_seeds`` seeds are room tags (they get regions and split shared components); the rest are shaft and
    riser codes that only stop their component being absorbed as furniture.
    """
    n_room_seeds = len(seeds_pv) if n_room_seeds is None else n_room_seeds
    x0, y0, _, _ = roi
    barrier = (wall > 0) | (thin > 0)
    lab, n = ndi.label(~barrier)           # 4-connected
    diag = {"n_components": int(n), "px_shape": list(wall.shape), "mm_per_px": float(math.sqrt(m2_per_px) * 1000.0)}
    # seed pixels, nudged off barrier pixels
    seed_px = np.zeros((len(seeds_pv), 2), int)
    seed_lab = np.zeros(len(seeds_pv), np.int64)
    for i, (x, y) in enumerate(seeds_pv):
        c = int((x - x0) * S); r = int((y - y0) * S)
        c = min(max(c, 0), lab.shape[1] - 1); r = min(max(r, 0), lab.shape[0] - 1)
        if lab[r, c] == 0:
            found = False
            for rad in range(1, 12):
                rs, re_ = max(r - rad, 0), min(r + rad + 1, lab.shape[0])
                cs, ce = max(c - rad, 0), min(c + rad + 1, lab.shape[1])
                win = lab[rs:re_, cs:ce]
                nz = np.argwhere(win > 0)
                if len(nz):
                    d = ((nz[:, 0] + rs - r) ** 2 + (nz[:, 1] + cs - c) ** 2)
                    k = int(np.argmin(d))
                    r, c = nz[k][0] + rs, nz[k][1] + cs
                    found = True
                    break
            diag["n_seeds_nudged"] = diag.get("n_seeds_nudged", 0) + 1
            if not found:
                seed_px[i] = (r, c); seed_lab[i] = 0
                continue
        seed_px[i] = (r, c)
        seed_lab[i] = lab[r, c]
    areas = np.bincount(lab.ravel(), minlength=n + 1).astype(np.int64)
    # border-touching components are open to the outside
    border = np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))
    border_set = set(int(b) for b in border if b > 0)
    has_seed = np.zeros(n + 1, bool)
    has_seed[seed_lab[seed_lab > 0]] = True
    # absorb small unseeded components bounded mostly by fine-pen lines
    parent = np.arange(n + 1)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    # candidates: unseeded components bounded mostly by fine pen (same thresholds as method A)
    cand = (~has_seed) & (areas * m2_per_px < FLIGHT_MAX_M2)
    cand[0] = False
    wall_adj, thin_adj = _boundary_pixel_counts(lab, wall, n + 1)
    own = wall_adj + thin_adj
    thin_frac = np.divide(thin_adj, own, out=np.zeros(n + 1), where=own > 0)
    need = np.where(areas * m2_per_px < FURNITURE_MAX_M2, THIN_FRACTION_MIN, THIN_FRACTION_LARGE)
    cand &= thin_frac >= need
    for b_ in border_set:
        cand[b_] = False
    contacts = _pair_contacts(lab, wall)
    diag["n_contact_pairs"] = len(contacts)
    diag["n_candidate_components"] = int(cand.sum())
    # blobs of candidates joined through fine-pen contact (a row of stair treads), then merged into the best anchor
    for (a_, b_), (t, w_) in contacts.items():
        if t > 0 and cand[a_] and cand[b_]:
            ra, rb = find(a_), find(b_)
            if ra != rb:
                parent[ra] = rb
    blob_members: Dict[int, List[int]] = defaultdict(list)
    for c in np.nonzero(cand)[0]:
        blob_members[find(int(c))].append(int(c))
    score: Dict[int, Dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for (a_, b_), (t, w_) in contacts.items():
        if t <= 0:
            continue
        if cand[a_] and not cand[b_]:
            score[find(a_)][b_] += t
        elif cand[b_] and not cand[a_]:
            score[find(b_)][a_] += t
    final_parent = np.arange(n + 1)
    merged = 0
    blob_unplaced = 0
    for root, members in blob_members.items():
        tot = sum(areas[m] for m in members) * m2_per_px
        sc = score.get(root)
        if tot > BLOB_MAX_M2 or not sc:
            blob_unplaced += 1
            continue
        anchor = max(sc, key=lambda k: (has_seed[k], sc[k], areas[k]))
        for m in members:
            final_parent[m] = anchor
            merged += 1
    diag["n_absorbed_components"] = merged
    diag["n_blobs_unplaced"] = blob_unplaced
    parent = final_parent
    root = np.array([find(i) for i in range(n + 1)], dtype=np.int64)
    lab2 = root[lab]
    del lab
    # group seeds by merged component
    comp_seeds: Dict[int, List[int]] = defaultdict(list)
    for i, l in enumerate(seed_lab):
        if l > 0 and i < n_room_seeds:
            comp_seeds[int(root[l])].append(i)
    results: List[SeedRegion] = []
    h, w = lab2.shape
    objs = ndi.find_objects(lab2)
    border2 = {int(root[b]) for b in border_set}
    for comp, idxs in comp_seeds.items():
        sl = objs[comp - 1]
        if sl is None:
            continue
        r0, r1 = max(sl[0].start - 1, 0), min(sl[0].stop + 1, h)
        c0, c1 = max(sl[1].start - 1, 0), min(sl[1].stop + 1, w)
        mask = lab2[r0:r1, c0:c1] == comp
        leaked = comp in border2
        if len(idxs) == 1:
            parts = {idxs[0]: mask}
        else:
            from skimage.segmentation import watershed
            markers = np.zeros(mask.shape, np.int32)
            for k, i in enumerate(idxs, start=1):
                markers[seed_px[i][0] - r0, seed_px[i][1] - c0] = k
            dist = ndi.distance_transform_edt(mask)
            ws = watershed(-dist, markers, mask=mask)
            parts = {i: ws == k for k, i in enumerate(idxs, start=1)}
        for i, m in parts.items():
            px = int(m.sum())
            area = px * m2_per_px
            if area > max_region_m2 and leaked:
                results.append(SeedRegion(i, px, area, 0.0, None, comp, len(idxs) > 1, True, "component open to the outside"))
                continue
            poly, perim = _mask_polygon(m, r0, c0, x0, y0, S)
            if poly is not None:
                area = poly.area * m2_per_px * S * S      # polygon on the line centre: same convention as the vector face
            results.append(SeedRegion(i, px, area, perim * pt_per_px, poly, comp, len(idxs) > 1, leaked))
    diag["n_seed_regions"] = len(results)
    diag["n_split_components"] = sum(1 for idxs in comp_seeds.values() if len(idxs) > 1)
    diag["n_unplaced_seeds"] = int((seed_lab == 0).sum())
    return results, diag


def _mask_polygon(mask: np.ndarray, r0: int, c0: int, x0: float, y0: float, S: float):
    """Polygon (page-view pt) of a boolean mask: the traced outline (through the centres of the outermost region pixels) grown by one
    pixel, which puts it on the centre of the 1 px wall line, the same place as the vector face; holes kept; returns (polygon, perimeter pt)."""
    m = np.pad(mask.astype(np.uint8), 1)
    cnts, hier = cv2.findContours(m, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hier is None:
        return None, 0.0
    hier = hier[0]
    rings: Dict[int, np.ndarray] = {}
    for k, c in enumerate(cnts):
        pts = c[:, 0, :].astype(float) - 1.0
        pts = np.stack([x0 + (pts[:, 0] + c0 + 0.5) / S, y0 + (pts[:, 1] + r0 + 0.5) / S], axis=1)
        if len(pts) >= 3:
            rings[k] = pts
    polys = []
    for k, pts in rings.items():
        if hier[k][3] >= 0:      # a hole: attached to its parent shell below
            continue
        holes = [rings[j] for j in rings if hier[j][3] == k]
        polys.append(Polygon(pts, holes).buffer(0))
    if not polys:
        return None, 0.0
    poly = polys[0] if len(polys) == 1 else shapely.union_all(polys)
    poly = poly.buffer(1.0 / S, join_style="mitre", mitre_limit=2.0)
    if poly.geom_type == "MultiPolygon":
        poly = max(poly.geoms, key=lambda g: g.area)
    return poly, float(poly.length)


def roi_from_points(pts: np.ndarray, margin_pt: float = 40.0) -> Tuple[float, float, float, float]:
    """Bounding box of seed points plus a margin."""
    return (float(pts[:, 0].min() - margin_pt), float(pts[:, 1].min() - margin_pt),
            float(pts[:, 0].max() + margin_pt), float(pts[:, 1].max() + margin_pt))


def measure_rooms(wall_segments: np.ndarray, thin_segments: np.ndarray, tags: Sequence[dict], mm_per_pt: float,
                  target_mm_per_px: float = 8.0, margin_pt: float = 40.0, blocker_tags: Sequence[dict] = (),
                  wall_bands: Sequence[Polygon] = ()) -> Tuple[List[SeedRegion], dict]:
    """Rasterise in tiles and grow a region from each tag seed; returns regions and diagnostics.

    ``wall_bands`` are the black filled wall bands of the sheet: they are filled into the wall raster, so a band is a wall of its
    own thickness and the room ends at its inner edge (the same convention as the vector face).
    """
    seeds = np.array([[t["x_pv"], t["y_pv"]] for t in list(tags) + list(blocker_tags)], float)
    roi = roi_from_points(seeds, margin_pt)
    S = pixels_per_pt(mm_per_pt, target_mm_per_px)
    wall = render_tiles(wall_segments, roi, S)
    if len(wall_bands):
        render_fills(wall_bands, roi, S, wall)
    thin = render_tiles(thin_segments, roi, S)
    thin[wall > 0] = 0
    m2_per_px = (mm_per_pt / S / 1000.0) ** 2
    res, diag = grow_regions(wall, thin, roi, S, seeds, m2_per_px, mm_per_pt / 1000.0, n_room_seeds=len(tags))
    diag.update({"roi_pv": list(roi), "px_per_pt": S, "mm_per_px_x": mm_per_pt / S, "tile_px": TILE_PX})
    return res, diag
