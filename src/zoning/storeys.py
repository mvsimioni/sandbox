"""Zoning for every storey LB3 to L06 of the Inpatient building (generalises the L04 pilot, ``pipeline.py``).

``python3 -m src.zoning.storeys run --storeys LB3,B3,B2,B1,L01,L02,L03,L04,L05,L06 [--stages calibrate,vector,raster,compare,assign,pdf,report] [--jobs 3]``

Per storey (background page from ``out/registers/sheet_index.csv``, sheet of ``Arch Combined Tender .pdf``):

* ``calibrate``  grid-bubble affine page view -> building mm (``calibrate`` module; ``sheet_calibration_plans.csv`` as cross-check);
                 App D page and RFI-ME-0233 sample (L04 only) calibrated too where they exist;
* ``vector``     method A, closed wall-face regions from the vector linework, seeded by the room tags of ``room_tags_plans.csv``;
* ``raster``     method B, region growing on the wall raster (check);
* ``compare``    A against B (and against App D captions where the storey has an App D page); writes
                 ``out/registers/room_polygons_<KEY>.csv`` (+ ``_wkt``) and the tags-without-polygon list;
* ``assign``     writes ``out/registers/zone_assignment_<KEY>.csv``.  The stub classes a room from its printed tag mode label only
                 (MV, NV; AC or no label -> ``no information``); a CSV that another method wrote (``method`` not ``stub``) is never overwritten;
* ``pdf``        one multi-page PDF (all storeys requested, index page first): ``out/zoning/AIGH_IP_AHU_Zoning_<first>-<last>.pdf``;
* ``report``     ``out/qa/zoning_storeys_<first>-<last>.csv`` (per storey statistics and PDF check) and the regression against the pilot.

Per-storey stage results are cached as pickles in ``out/zoning/storeys/<KEY>/work`` so a later stage can be re-run alone.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import pickle
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import shapely
from shapely.geometry import Polygon
from shapely.strtree import STRtree

from .. import common
from . import calibrate as cal
from . import compare as cmp
from . import linework as lw
from . import rooms_raster as rr
from . import rooms_vector as rv
from . import write_pdf as wp
from . import zones as zn

BG_PDF = "data/source/Basis/Arch Combined Tender .pdf"
SAMPLE_PDF = "data/source/Template/AHU Zoning - RFI-ME-0233.pdf"
APPD_PDF = "data/source/Basis/App D/Appendix D - ACMV For Common Areas_Combined.pdf"
ALL_STOREYS = ["LB3", "B3", "B2", "B1", "L01", "L02", "L03", "L04", "L05", "L06"]
SAMPLE_STOREY = "L04"            # the RFI-ME-0233 sample is the 4th storey only
CONVENTION = "wall_face"         # boundary convention of the adopted polygon (the pilot adopted it on L04; one rule for all storeys)
STAGES = ["calibrate", "vector", "raster", "compare", "assign", "pdf", "report"]
STOREY_STAGES = ["calibrate", "vector", "raster", "compare", "assign"]
AB_TARGET = 0.95
PIXEL_BUDGET = 180e6           # method B raster pixels per storey (memory: about 45 bytes per pixel at peak)
PILOT_PIXEL_BUDGET = 300e6     # L04 keeps the pilot's 8 mm/px (275 Mpx, about 13 GB) so the regression is like for like

POLY_FIELDS = ["room_id", "storey", "room_tag", "room_number", "room_name", "mode", "a_area_m2", "b_area_m2", "method_agreement",
               "a_minus_b_pct", "polygon_pv_wkt", "polygon_building_mm_wkt", "grid_location",
               "n_tags_in_region", "other_tags", "a_perimeter_m", "wall_face_area_m2", "centreline_area_m2", "boundary_convention",
               "centroid_x_mm", "centroid_y_mm", "centroid_inside_polygon", "label_point_x_mm", "label_point_y_mm", "grid_location_label_point",
               "appd_caption_m2", "appd_annotation_index", "appd_overlap_room", "appd_overlap_polygon", "error_vs_caption_pct",
               "error_b_vs_caption_pct", "appd_class_tag", "appd_class_kind", "appd_class_confidence", "appd_class_basis",
               "area_printed_m2", "region_kind", "region_area_m2", "a_first_closure_m2", "w_only_region_m2", "boundary_basis", "boundary_check", "mode_source", "flags", "source_file", "source_page", "tag_box_pv", "tag_method", "measurement_confidence", "method", "coord_frame"]
ASSIGN_FIELDS = ["room_id", "storey", "room_tag", "room_name", "mode_label", "class", "class_kind", "confidence", "area_m2",
                 "department", "sources", "method", "notes"]
FRAME_TEXT = ("polygon_pv_wkt: page view (raw*page.rotation_matrix), pt, origin top-left, y down; polygon_building_mm_wkt: building mm, origin grid 2 / A, "
              "X along numbered grids, Y along lettered grids (grid_model.json)")


# --------------------------------------------------------------------------- #
# Storey table and paths
# --------------------------------------------------------------------------- #
def storey_info(key: str) -> dict:
    P = common.resolve_paths()
    reg = P["registers"]
    rows = [r for r in common.read_csv_dicts(reg / "sheet_index.csv") if r["storey_key"] == key and int(r["page"]) <= 10]
    if not rows:
        raise SystemExit("storey %s is not on pages 1 to 10 of out/registers/sheet_index.csv" % key)
    r = rows[0]
    ap = sorted({int(x["page"]) for x in common.read_csv_dicts(reg / "appd_spaces.csv") if x["drawing_number"] == r["drawing_number"]})
    return {"key": key, "page": int(r["page"]), "page0": int(r["page"]) - 1, "drawing": r["drawing_number"], "title": r["title"],
            "storey_label": r["storey_label"], "appd_page": ap[0] if ap else None, "appd_page0": (ap[0] - 1) if ap else None}


def storey_paths(key: str) -> dict:
    root = common.project_root()
    P = common.resolve_paths()
    out = P["zoning"] / "storeys" / key
    return {"root": root, "reg": P["registers"], "qa": P["qa"], "zoning": P["zoning"], "out": out, "work": out / "work"}


def _open(root: Path, rel: str, page0: int):
    import pymupdf
    return pymupdf.open(str(root / rel))[page0]


def load_tags(reg: Path, page: int, kind: str = "room") -> List[dict]:
    rows = [r for r in common.read_csv_dicts(reg / "room_tags_plans.csv") if r["page"] == str(page) and r["kind"] == kind]
    for i, r in enumerate(rows):
        r["x_pv"] = float(r["x_pv"]); r["y_pv"] = float(r["y_pv"]); r["t_index"] = i
        r["mode_source"] = "out/registers/room_tags_plans.csv column mode (printed on the tag)" if r.get("mode", "").strip() else ""
    return rows


NA_MAX_BELOW_PT = 9.0


def recover_na_modes(root: Path, page0: int, tags: List[dict]) -> Tuple[int, List[dict]]:
    """Mode label ``NA`` printed under a room name.  The tag register leaves ``mode`` blank for it; the page text layer has the word.

    A word ``NA`` of the text layer is the mode label of the tag whose name box ends at most 9 pt above it and whose horizontal extent holds the word
    (the rule the register uses for MV, NV and AC: mode span below the name span).  Only tags with a blank mode take it, one word per tag.
    Returns the number of tags filled and the words that no tag took (listed in ``na_words_<KEY>.csv``).
    """
    return na_modes_from_page(_open(root, BG_PDF, page0), tags)


def na_modes_from_page(page, tags: List[dict]) -> Tuple[int, List[dict]]:
    import pymupdf
    rot = page.rotation_matrix
    taken: Dict[int, Tuple[float, tuple]] = {}
    loose: List[dict] = []
    for w in page.get_text("words"):
        if w[4] != "NA":
            continue
        r = pymupdf.Rect(w[:4]) * rot
        cx = (r.x0 + r.x1) / 2.0
        best = None
        for t in tags:
            if t.get("mode", "").strip() or not t.get("box_pv"):
                continue
            b = [float(x) for x in t["box_pv"].split(",")]
            dy = r.y0 - b[3]
            if -1.0 <= dy <= NA_MAX_BELOW_PT and b[0] - 2.0 <= cx <= b[2] + 2.0:
                score = abs(dy) + 0.1 * abs(cx - (b[0] + b[2]) / 2.0)
                if best is None or score < best[0]:
                    best = (score, t)
        if best is None or (best[1]["t_index"] in taken and taken[best[1]["t_index"]][0] <= best[0]):
            loose.append({"word_box_pv": ",".join("%.1f" % v for v in (r.x0, r.y0, r.x1, r.y1))})
            continue
        taken[best[1]["t_index"]] = (best[0], (r.x0, r.y0, r.x1, r.y1))
    for t in tags:
        if t["t_index"] in taken:
            b = taken[t["t_index"]][1]
            t["mode"] = "NA"
            t["mode_source"] = ("page text layer: word 'NA' at box_pv %s, below the name span of the tag (%s); method: proximity as the register's mode spans; "
                                "confidence 0.9" % (",".join("%.1f" % v for v in b), t.get("box_pv", "")))
    return len(taken), loose


# --------------------------------------------------------------------------- #
# Stages (per storey)
# --------------------------------------------------------------------------- #
def stage_calibrate(ctx: dict) -> dict:
    P, info = ctx["P"], ctx["info"]
    grid = cal.load_grid_model(P["reg"] / "grid_model.json")
    bg = _open(P["root"], BG_PDF, info["page0"])
    reg_row = [r for r in common.read_csv_dicts(P["reg"] / "sheet_calibration_plans.csv") if r["page"] == str(info["page"])][0]
    notes: List[str] = []
    det = None
    try:
        det = cal.calibrate_page(bg, "background p%d" % info["page"], grid=grid)
    except Exception as e:   # too few bubbles detected
        notes.append("bubble detection failed: %s" % e)
    creg, breg = cal.calibrate_page(bg, register_csv=P["reg"] / "grid_observations_plans.csv", page_number=info["page"], grid=grid)
    rsx, rsy = float(reg_row["scale_mm_per_pt_x"]), float(reg_row["scale_mm_per_pt_y"])

    def scale_dev(c):
        return max(abs(c.scale_x_mm_per_pt / rsx - 1), abs(c.scale_y_mm_per_pt / rsy - 1)) * 100.0
    use, bubbles, basis = creg, breg, "grid_observations_plans.csv (register bubbles)"
    if det is not None:
        cb, bb = det
        if scale_dev(cb) < 0.5 and cb.max_mm < 25.0:
            use, bubbles, basis = cb, bb, "bubbles detected on the background page"
        else:
            notes.append("detected calibration rejected (scale deviation %.2f %%, max residual %.1f mm); register bubbles used" % (scale_dev(cb), cb.max_mm))
    out = {"grid": grid, "cal_bg": use, "cal_basis": basis, "bubbles_bg": bubbles, "register_row": reg_row, "notes": notes,
           "cal_bg_register": creg, "cal_bg_detected": det[0] if det else None,
           "scale_dev_vs_register_pct": scale_dev(use), "frame_register_vs_detected": cal.compare_frames(breg, det[1]) if det else {}}
    if info["appd_page0"] is not None:
        ad = _open(P["root"], APPD_PDF, info["appd_page0"])
        ca, ba = cal.calibrate_page(ad, "App D p%d" % info["appd_page"], grid=grid)
        out.update({"cal_appd": ca, "frame_appd_vs_bg": cal.compare_frames(bubbles, ba)})
        errs = []
        bgm = {(b.axis, b.label): b for b in bubbles}
        for b in ba:
            o = bgm.get((b.axis, b.label))
            if o is None or b.offset or o.offset:
                continue
            q = cal.transfer_points(np.array([[b.x, b.y]]), ca, use)[0]
            X1, Y1 = use.to_building(q[0], q[1]); X0, Y0 = use.to_building(o.x, o.y)
            errs.append(abs(X1 - X0) if b.axis == "numbered" else abs(Y1 - Y0))
        out["transfer_residual_appd_mm"] = {"max": float(max(errs)) if errs else float("nan"),
                                            "rms": float(np.sqrt(np.mean(np.square(errs)))) if errs else float("nan"), "n": len(errs)}
    if info["key"] == SAMPLE_STOREY:
        sm = _open(P["root"], SAMPLE_PDF, 0)
        cs, bs = cal.calibrate_page(sm, "sample p1", grid=grid)
        out.update({"cal_sample": cs, "frame_sample_vs_bg": cal.compare_frames(bubbles, bs)})
        errs = []
        bgm = {(b.axis, b.label): b for b in bubbles}
        for b in bs:
            o = bgm.get((b.axis, b.label))
            if o is None or b.offset or o.offset:
                continue
            q = cal.transfer_points(np.array([[b.x, b.y]]), cs, use)[0]
            X1, Y1 = use.to_building(q[0], q[1]); X0, Y0 = use.to_building(o.x, o.y)
            errs.append(abs(X1 - X0) if b.axis == "numbered" else abs(Y1 - Y0))
        out["transfer_residual_sample_mm"] = {"max": float(max(errs)) if errs else float("nan"),
                                              "rms": float(np.sqrt(np.mean(np.square(errs)))) if errs else float("nan"), "n": len(errs)}
    return out


def strip_dimension_strokes(ss: lw.StrokeSet) -> Tuple[lw.StrokeSet, dict]:
    """Remove the dimension strings from a stroke set: dimension lines and extension stubs are not walls.

    A dimension string is recognised by its oblique ticks: two 0.6 pt black segments of 1.8 to 3.4 pt at +-45 deg that leave one node in opposite
    directions.  Removed: the tick segments, every fine-pen black segment (0.24, 0.36, 0.48 pt) longer than 6.5 pt with both ends on tick nodes
    (the dimension line) and every fine-pen segment of at most 10 pt with one end on a tick node (extension stub).  Walls are heavy pens and are
    never touched.  (Both the dimension line and its extension lines closed room polygons on the first run: B3 Hydrant Tank 2, L05 FLL.)
    """
    from scipy.spatial import cKDTree
    seg = ss.seg
    n = len(seg)
    ln = np.hypot(seg[:, 2] - seg[:, 0], seg[:, 3] - seg[:, 1])
    ang = np.degrees(np.arctan2(seg[:, 3] - seg[:, 1], seg[:, 2] - seg[:, 0]))
    diag = ss.select((0.6,)) & (ln >= 1.8) & (ln <= 3.4) & (np.abs((np.abs(ang) % 90.0) - 45.0) < 4.0) & (~ss.curve)
    pts, vec, own = [], [], []
    for i in np.where(diag)[0]:
        a, b = seg[i, :2], seg[i, 2:]
        u = (b - a) / ln[i]
        pts += [a, b]; vec += [u, -u]; own += [i, i]
    empty = {"n_tick_nodes": 0, "n_tick_segments": 0, "n_dimension_lines": 0, "n_extension_stubs": 0, "n_removed": 0}
    if not pts:
        return ss, empty
    pts, vec, own = np.asarray(pts), np.asarray(vec), np.asarray(own)
    node_ends = set()
    for i, j in cKDTree(pts).query_pairs(0.15):
        if own[i] != own[j] and float(vec[i] @ vec[j]) < -0.95:
            node_ends.update((i, j))
    if not node_ends:
        return ss, empty
    node_ends = sorted(node_ends)
    node_tree = cKDTree(pts[node_ends])
    remove = np.zeros(n, bool)
    tick_segs = sorted({int(own[k]) for k in node_ends})
    remove[tick_segs] = True
    ti = np.where(ss.select((0.24, 0.36, 0.48)) & (~ss.curve))[0]
    on0 = node_tree.query(seg[ti, :2])[0] <= 0.2
    on1 = node_tree.query(seg[ti, 2:])[0] <= 0.2
    dim_line = ti[on0 & on1 & (ln[ti] > 6.5)]
    stub = ti[(on0 ^ on1) & (ln[ti] <= 10.0)]
    remove[dim_line] = True
    remove[stub] = True
    keep = ~remove
    return (lw.StrokeSet(seg[keep], ss.style[keep], ss.styles, ss.curve[keep], ss.fills),
            {"n_tick_nodes": len(node_ends) // 2, "n_tick_segments": len(tick_segs), "n_dimension_lines": int(len(dim_line)),
             "n_extension_stubs": int(len(stub)), "n_removed": int(remove.sum())})


def stage_vector(ctx: dict) -> dict:
    P = ctx["P"]
    bg = _open(P["root"], BG_PDF, ctx["info"]["page0"])
    ss = lw.extract_strokes(bg)
    ss, dim_diag = strip_dimension_strokes(ss) if ctx.get("fixes", True) else (ss, {"n_removed": 0, "note": "pilot-compatible run: dimension strings kept"})
    cb: cal.Calibration = ctx["calibrate"]["cal_bg"]
    k2 = cb.area_scale_mm2_per_pt2() / 1e6
    tags = ctx["tags"]
    plan_box = (min(t["x_pv"] for t in tags) - 60, min(t["y_pv"] for t in tags) - 60, max(t["x_pv"] for t in tags) + 60, max(t["y_pv"] for t in tags) + 60)
    t0 = time.time()
    classes = lw.class_table(ss, bbox=plan_box)
    vec = rv.measure_rooms(ss, tags, k2, blocker_tags=ctx["codes"])
    vec.diagnostics["seconds"] = round(time.time() - t0, 1)
    vec.diagnostics["stroke_classes"] = classes
    vec.diagnostics["n_strokes_total"] = int(len(ss.seg))
    vec.diagnostics["plan_box_pv"] = plan_box
    vec.diagnostics["dimension_strip"] = dim_diag
    # second opinion on the closure: the same wall set WITHOUT the fine-pen lines (furniture, finish and annotation lines are not walls)
    wreg = []
    if ctx.get("fixes", True):
        wreg = rv.regions_with_tags(rv.polygonise(vec.wall_segments), tags, k2)
    vec.diagnostics["n_w_only_regions"] = len(wreg)
    return {"vec": vec, "wreg": wreg}


def w_adoption_map(vec: rv.VectorResult, wreg: Sequence, k2: float) -> Dict[int, Polygon]:
    """Region index -> wall-only polygon, for the regions whose boundary is re-derived from heavy wall strokes.

    When the wall-only region (no fine-pen lines) holds exactly the same tags as the first closed region, contains it and is larger, a fine-pen line
    (finish, handrail, annotation) closed the first region; the wall-only region is the room.  Bounded to 400 m2 and four times the first region
    (+20 m2), so a leak through an unclosed opening is never adopted.
    """
    wmap = {}
    for wr in wreg:
        for t in wr.tags:
            wmap[t["t_index"]] = wr
    out: Dict[int, Polygon] = {}
    for i, reg in enumerate(vec.regions):
        wr = wmap.get(reg.tags[0]["t_index"])
        if wr is None:
            continue
        first = reg.polygon
        w_area = wr.polygon.area * k2
        same = {t["t_index"] for t in wr.tags} == {t["t_index"] for t in reg.tags}
        inside = first.difference(wr.polygon.buffer(0.3)).area <= 0.01 * first.area
        grows = w_area - first.area * k2 > max(0.3, 0.02 * first.area * k2)
        if same and inside and grows and w_area <= min(W_ADOPT_MAX_M2, W_ADOPT_MAX_RATIO * first.area * k2 + 20.0):
            out[i] = wr.polygon
    return out


def stage_raster(ctx: dict) -> dict:
    vec: rv.VectorResult = ctx["vector"]["vec"]
    cb: cal.Calibration = ctx["calibrate"]["cal_bg"]
    t0 = time.time()
    mm_pt = 0.5 * (cb.scale_x_mm_per_pt + cb.scale_y_mm_per_pt)
    # the raster costs about 30 bytes per pixel of the seed bounding box: 8 mm/px as in the pilot while it stays within PIXEL_BUDGET
    # (L04: 275 Mpx), coarser on a wider storey (a 16 GB limit is shared with other jobs)
    seeds = np.array([[t["x_pv"], t["y_pv"]] for t in list(ctx["tags"]) + list(ctx["codes"])], float)
    x0, y0, x1, y1 = rr.roi_from_points(seeds, 40.0)
    area_pt2 = (x1 - x0) * (y1 - y0)
    budget = PILOT_PIXEL_BUDGET if ctx["info"]["key"] == SAMPLE_STOREY else PIXEL_BUDGET
    mm_px = max(8.0, mm_pt / math.sqrt(budget / area_pt2))
    old_cap = rr.MAX_MM_PER_PX
    rr.MAX_MM_PER_PX = max(old_cap, mm_px)       # the module caps the pixel at 10 mm; a wide storey needs a coarser one to fit in memory
    try:
        res, diag = rr.measure_rooms(vec.wall_segments, vec.thin_segments, ctx["tags"], mm_pt, target_mm_per_px=mm_px,
                                     blocker_tags=ctx["codes"], wall_bands=vec.wall_bands)
    finally:
        rr.MAX_MM_PER_PX = old_cap
    diag["target_mm_per_px"] = mm_px
    # rooms whose boundary method A re-derived from the heavy walls: method B is grown again on the heavy-wall raster alone (no fine-pen lines), so
    # that the check judges the same boundary definition (the first B run treats fine-pen lines as soft barriers just as the first A closure did)
    res_w = []
    if ctx.get("fixes", True):
        k2 = cb.area_scale_mm2_per_pt2() / 1e6
        adopt = w_adoption_map(vec, ctx["vector"].get("wreg", []), k2)
        tags_w = [t for i in adopt for t in vec.regions[i].tags]
        if tags_w:
            old_cap = rr.MAX_MM_PER_PX
            rr.MAX_MM_PER_PX = max(old_cap, mm_px)
            try:
                res_w, diag_w = rr.measure_rooms(vec.wall_segments, np.zeros((0, 4)), tags_w, mm_pt, target_mm_per_px=mm_px,
                                                 blocker_tags=ctx["codes"], wall_bands=vec.wall_bands)
            finally:
                rr.MAX_MM_PER_PX = old_cap
            diag["n_tags_second_pass_heavy_walls_only"] = len(tags_w)
    diag["seconds"] = round(time.time() - t0, 1)
    return {"regions": res, "diag": diag, "regions_w": res_w}


def _sorted_regions(vec: rv.VectorResult) -> List[int]:
    cent = [(r.polygon.centroid.y, r.polygon.centroid.x) for r in vec.regions]
    return sorted(range(len(vec.regions)), key=lambda i: (round(cent[i][0] / 60.0), cent[i][1]))


LEAK_MIN_TAGS = 4          # a closed region that holds this many tags ...
LEAK_MIN_M2 = 150.0        # ... and is at least this large is several rooms that the walls did not separate (an opening left unclosed)
LEAK_BIG_M2 = 1000.0       # two or more tags in a region this large: also a leak
W_ADOPT_MAX_M2 = 400.0
W_ADOPT_MAX_RATIO = 4.0


def is_leaked(n_tags: int, area_m2: float) -> bool:
    return (n_tags >= LEAK_MIN_TAGS and area_m2 >= LEAK_MIN_M2) or (n_tags >= 2 and area_m2 >= LEAK_BIG_M2)


def assemble_rooms(ctx: dict) -> List[dict]:
    """Room dicts for the comparison (same rules and ordering as the pilot ``pipeline.assemble_rooms``; ids ``<KEY>-Rnnn``).

    Two corrections (``ctx['fixes']``, on by default; off in the pilot-compatible regression run):

    * heavy-wall closure: when the wall-only region (no fine-pen lines) holds exactly the same tags as the first closed region, contains it and is larger,
      the fine-pen line that closed the first region is not a wall (a finish or annotation line) and the wall-only region is the room (flag, both areas kept);
    * leaked regions: a region of several tags and much larger than a room (``is_leaked``) is not a room; it is not measured (``leaked`` True).
    """
    key = ctx["info"]["key"]
    fixes = ctx.get("fixes", True)
    vec: rv.VectorResult = ctx["vector"]["vec"]
    ras = ctx["raster"]
    cb: cal.Calibration = ctx["calibrate"]["cal_bg"]
    k2 = cb.area_scale_mm2_per_pt2() / 1e6
    by_tag = {sr.tag_index: sr for sr in ras["regions"]}
    adopt = w_adoption_map(vec, ctx["vector"].get("wreg", []), k2) if fixes else {}
    wmap = {t["t_index"]: wr for wr in ctx["vector"].get("wreg", []) for t in wr.tags}
    by_tag_w = {sr.tag_index: sr for sr in ras.get("regions_w", [])}
    # tag index of the second pass refers to the position in its own tag list: map back through the tag identity
    w_tags = [t for i in adopt for t in vec.regions[i].tags]
    by_tag_w = {w_tags[sr.tag_index]["t_index"]: sr for sr in ras.get("regions_w", []) if sr.tag_index < len(w_tags)}
    gaps = vec.wall_gaps_pt
    like = [f for f, ok in zip(vec.faces, rv._room_like(vec.faces, k2)) if ok]
    tree = STRtree(like)
    rooms = []
    for n, i in enumerate(_sorted_regions(vec), start=1):
        reg = vec.regions[i]
        face = first = reg.polygon
        half = float(gaps[i]) / 2.0
        flags = []
        w_area = None
        if i in adopt:
            face = adopt[i]
            w_area = face.area * k2
            basis = "heavy wall strokes only (the fine-pen line(s) that closed the first region are not walls)"
            flags.append("boundary re-derived from heavy wall strokes: first closed region %.1f m2 (fine-pen boundary), wall-only region %.1f m2 adopted"
                         % (first.area * k2, w_area))
        else:
            basis = "first closed region (heavy walls and fine-pen lines)"
            w_area = None
        check = ""
        if fixes:
            wr = wmap.get(reg.tags[0]["t_index"])
            fa = first.area * k2
            if i in adopt:
                check = "wall-only region adopted (fine-pen line closed the first region)"
            elif wr is None:
                check = "fine-pen lines needed to close: the heavy walls alone give no region of plausible size"
            elif wr.polygon.area * k2 - fa <= max(0.3, 0.02 * fa):
                check = "wall-closed: heavy walls alone give the same region"
            else:
                same = {t["t_index"] for t in wr.tags} == {t["t_index"] for t in reg.tags}
                check = "UNRESOLVED: heavy walls alone give %.1f m2 (%s) against %.1f m2 closed with fine-pen lines" % (
                    wr.polygon.area * k2, "same tags" if same else "%d tags: other rooms or a leak" % len(wr.tags), fa)
                flags.append("boundary check: " + check)
        a_cl = rv.centreline_polygon(face, half, tree, like)
        c = face.representative_point()
        tg = sorted(reg.tags, key=lambda t: (t["x_pv"] - c.x) ** 2 + (t["y_pv"] - c.y) ** 2)
        srs = [(by_tag_w if i in adopt else by_tag).get(t["t_index"]) for t in reg.tags]
        b_face = b_cl = None
        b_area = None
        if all(s is not None and s.polygon is not None for s in srs) and srs:
            b_face = shapely.union_all([s.polygon for s in srs])
            if b_face.geom_type == "MultiPolygon":
                b_face = max(b_face.geoms, key=lambda g: g.area)
            b_area = sum(s.area_m2 for s in srs)
            b_cl = rv.centreline_polygon(b_face, half, tree, like)
            if any(s.shared_component for s in srs) and len(reg.tags) == 1:
                flags.append("B shares its component with a neighbouring seed")
        else:
            flags.append("B: no closed region (%s)" % "; ".join(sorted({s.notes or "missing" for s in srs if s is None or s.polygon is None})) if srs else "B: no seed result")
        if len(reg.tags) > 1:
            flags.append("open plan: %d tags in one closed region" % len(reg.tags))
        if face.area * k2 > 400:
            flags.append("large region (%.0f m2): check closure" % (face.area * k2))
        leaked = bool(fixes and is_leaked(len(reg.tags), face.area * k2))
        if leaked:
            flags.append("LEAKED REGION: %d tags in %.0f m2 - the walls do not separate these rooms (unclosed opening); not measured" % (len(reg.tags), face.area * k2))
        rooms.append({"room_id": "%s-R%03d" % (key, n), "tag": tg[0]["text"], "name": tg[0]["name"], "mode": tg[0]["mode"], "number": tg[0].get("number", ""),
                      "tag_row": tg[0], "tags_inside": [t["text"] for t in tg], "modes_inside": [t["mode"] for t in tg], "tag_rows": tg,
                      "a_face": face, "a_cl": a_cl, "b_face": b_face, "b_cl": b_cl, "a_first": first, "boundary_basis": basis, "boundary_check": check,
                      "w_only_area_m2": w_area, "leaked": leaked,
                      "b_face_area_m2": b_area, "flags": "; ".join(flags), "vec_index": i, "gap_pt": float(gaps[i])})
    return rooms


def _wkt_mm(poly: Optional[Polygon], calib: cal.Calibration) -> str:
    if poly is None or poly.is_empty:
        return ""

    def conv(coords):
        xs, ys = calib.to_building(np.array([c[0] for c in coords]), np.array([c[1] for c in coords]))
        return list(zip(np.round(xs, 0).tolist(), np.round(ys, 0).tolist()))
    return Polygon(conv(poly.exterior.coords), [conv(r.coords) for r in poly.interiors]).wkt


def _wkt_pv(poly: Optional[Polygon]) -> str:
    if poly is None or poly.is_empty:
        return ""
    r = lambda cs: [(round(x, 2), round(y, 2)) for x, y in cs]
    return Polygon(r(poly.exterior.coords), [r(i.coords) for i in poly.interiors]).wkt


def load_appd_polygons_checked(appd_page, calib_appd: cal.Calibration, calib_bg: cal.Calibration) -> List[cmp.AppdPolygon]:
    """``cmp.load_appd_polygons`` (unchanged pilot module) without the polygons whose caption is 0 sq m (an unfinished measurement: the error
    ratio would divide by zero and there is no area to compare with)."""
    import pymupdf
    rot = appd_page.rotation_matrix
    out: List[cmp.AppdPolygon] = []
    for idx, a in enumerate(appd_page.annots()):
        if a.type[1] != "Polygon":
            continue
        capv = cmp.parse_caption(a.info.get("content", ""))
        if capv is None or capv <= 0 or not a.vertices:
            continue
        pts = np.array([tuple(pymupdf.Point(q) * rot) for q in a.vertices], float)
        P_appd = Polygon(pts).buffer(0)
        P_bg = Polygon(cal.transfer_points(pts, calib_appd, calib_bg)).buffer(0)
        own = P_appd.area * calib_appd.area_scale_mm2_per_pt2() / 1e6
        out.append(cmp.AppdPolygon(idx, a.xref, capv, P_appd, P_bg, own, 100.0 * (own - capv) / capv))
    return out


def _empty_row(room_id: str, key: str, t: dict, kind: str, status: str, flags: str, n_tags: int, region_area: Optional[float], page: int) -> dict:
    """Room row of a tag that has no polygon of its own (leaked region, or a tag that fell in no closed region)."""
    r = {f: "" for f in POLY_FIELDS}
    r.update({"room_id": room_id, "storey": key, "room_tag": t["text"], "room_number": t.get("number", ""), "room_name": t["name"], "mode": t["mode"],
              "mode_source": t.get("mode_source", ""), "method_agreement": status, "n_tags_in_region": n_tags, "region_kind": kind,
              "region_area_m2": "" if region_area is None else round(region_area, 1), "area_printed_m2": t.get("area_m2_printed", ""), "flags": flags,
              "source_file": BG_PDF, "source_page": page, "tag_box_pv": t.get("box_pv", ""), "tag_method": t.get("method", ""),
              "measurement_confidence": "none: no polygon (%s)" % status, "coord_frame": FRAME_TEXT,
              "method": "A vector closed regions (primary); B raster region growing (check): this tag was not given a polygon of its own"})
    return r


def stage_compare(ctx: dict) -> dict:
    P, info = ctx["P"], ctx["info"]
    C = ctx["calibrate"]
    cb = C["cal_bg"]
    key = info["key"]
    k2 = cb.area_scale_mm2_per_pt2() / 1e6
    appd, appd_classes = [], []
    if info["appd_page0"] is not None:
        ad = _open(P["root"], APPD_PDF, info["appd_page0"])
        appd = load_appd_polygons_checked(ad, C["cal_appd"], cb)
        pg = str(info["appd_page"])
        appd_classes = zn.classify_appd_spaces(appd, [r for r in common.read_csv_dicts(P["reg"] / "appd_spaces.csv") if r["page"] == pg],
                                               [r for r in common.read_csv_dicts(P["reg"] / "appd_equipment.csv") if r["page"] == pg])
    all_rooms = assemble_rooms(ctx)
    rooms = [r for r in all_rooms if not r["leaked"]]
    leaked = [r for r in all_rooms if r["leaked"]]
    rows, stats = cmp.build_room_rows(rooms, appd, cb, C["grid"], convention=CONVENTION)
    cls_by_idx = {c["annotation_index"]: c for c in appd_classes}
    appd_by_idx = {a.annotation_index: a for a in appd}
    appd_src = "; App D: %s p%s" % (APPD_PDF, info["appd_page"]) if info["appd_page"] else "; no App D page for this storey"
    review: List[dict] = []

    def rev(room_id, tag, kind, detail):
        review.append({"storey": key, "room_id": room_id, "room_tag": tag, "kind": kind, "detail": detail})
    appd_rows: List[dict] = []
    for r, room in zip(rows, rooms):
        tag = room["tag_row"]
        poly = room["a_face"] if CONVENTION == "wall_face" else room["a_cl"]
        r["storey"] = key
        r["room_number"] = room["number"]
        r["polygon_pv_wkt"] = _wkt_pv(poly)
        r["polygon_building_mm_wkt"] = _wkt_mm(poly, cb)
        r["area_printed_m2"] = tag.get("area_m2_printed", "")
        ai = r.get("appd_annotation_index")
        c = cls_by_idx.get(int(ai)) if ai not in ("", None) else None
        r["appd_class_tag"] = c["tag"] if c else ""
        r["appd_class_kind"] = c["kind"] if c else ""
        r["appd_class_confidence"] = c["confidence"] if c else ""
        r["appd_class_basis"] = c["basis"] if c else ""
        r["mode_source"] = tag.get("mode_source", "")
        r["region_kind"] = "open plan (%d tags share this polygon)" % len(room["tags_inside"]) if len(room["tags_inside"]) > 1 else "room"
        r["region_area_m2"] = round(room["a_face"].area * k2, 3)
        r["a_first_closure_m2"] = round(room["a_first"].area * k2, 3)
        r["w_only_region_m2"] = "" if room["w_only_area_m2"] is None else round(room["w_only_area_m2"], 3)
        r["boundary_basis"] = room["boundary_basis"]
        r["boundary_check"] = room["boundary_check"]
        r["source_file"] = BG_PDF
        r["source_page"] = info["page"]
        r["tag_box_pv"] = tag.get("box_pv", "")
        r["tag_method"] = tag.get("method", "")
        agree = r["method_agreement"]
        r["measurement_confidence"] = {"agree": "high: A and B agree (2 % or 0.3 m2)", "differ": "medium: A (primary); B differs beyond tolerance",
                                       "no B region": "medium: A only, no B region"}.get(agree, agree)
        if room["boundary_basis"].startswith("heavy"):
            r["measurement_confidence"] = "medium: A re-derived from heavy walls; B grown on the raster with fine-pen lines, " + ("agrees" if agree == "agree" else "does not agree")
        r["method"] = "A vector closed regions (primary); B raster region growing (check)%s" % appd_src
        r["coord_frame"] = FRAME_TEXT
        rid = r["room_id"]
        if agree == "differ":
            rev(rid, r["room_tag"], "A and B differ", "A %.2f m2, B %s m2 (%s %%)" % (float(r["a_area_m2"]), r["b_area_m2"], r["a_minus_b_pct"]))
        elif agree == "no B region":
            rev(rid, r["room_tag"], "no B region", r["flags"])
        if room["boundary_basis"].startswith("heavy"):
            rev(rid, r["room_tag"], "boundary re-derived from heavy walls", r["flags"])
        if room["boundary_check"].startswith("UNRESOLVED"):
            rev(rid, r["room_tag"], "boundary unresolved: fine-pen line(s) close the polygon", room["boundary_check"])
        if len(room["tags_inside"]) > 1:
            rev(rid, r["room_tag"], "open plan: several tags share one polygon", "tags: " + " | ".join(room["tags_inside"][:12]) + (" ..." if len(room["tags_inside"]) > 12 else ""))
        if len({m for m in room["modes_inside"] if m}) > 1:
            rev(rid, r["room_tag"], "conflicting mode labels in one region", " | ".join(sorted({m for m in room["modes_inside"] if m})))
        if r.get("appd_caption_m2") not in ("", None):
            cap = float(r["appd_caption_m2"])
            af, ac = float(r["wall_face_area_m2"]), float(r["centreline_area_m2"])
            okf, okc = cmp.within(af, cap, cmp.CAPTION_PCT, AB_M2), cmp.within(ac, cap, cmp.CAPTION_PCT, AB_M2)
            appd_rows.append({"storey": key, "room_id": rid, "room_tag": r["room_tag"], "appd_annotation_index": r["appd_annotation_index"], "appd_caption_m2": cap,
                              "a_wall_face_m2": af, "a_centreline_m2": ac, "error_face_pct": round(100.0 * (af - cap) / cap, 2),
                              "error_centreline_pct": round(100.0 * (ac - cap) / cap, 2), "face_within_2pct_or_0.3m2": "yes" if okf else "no",
                              "centreline_within_2pct_or_0.3m2": "yes" if okc else "no",
                              "appd_polygon_own_area_m2": round(appd_by_idx[int(r["appd_annotation_index"])].own_area_m2, 3),
                              "appd_polygon_own_vs_caption_pct": round(appd_by_idx[int(r["appd_annotation_index"])].own_vs_caption_pct, 2), "overlap_room": r["appd_overlap_room"], "overlap_polygon": r["appd_overlap_polygon"],
                              "source": "%s page %s annot %s; %s page %s" % (APPD_PDF, info["appd_page"], r["appd_annotation_index"], BG_PDF, info["page"])})
            if not okf:
                rev(rid, r["room_tag"], "App D caption differs", "A %.2f m2 vs caption %.2f m2 (%s %%); centreline %.2f m2" % (af, cap, r["error_vs_caption_pct"], ac))
    # rooms without a polygon of their own: tags of leaked regions and tags that fell in no closed region
    extra: List[dict] = []
    for room in leaked:
        for k, t in enumerate(sorted(room["tag_rows"], key=lambda t: (t["y_pv"], t["x_pv"])), start=1):
            rid = "%s-T%02d" % (room["room_id"], k)
            extra.append(_empty_row(rid, key, t, "leaked region", "not measured: leaked region",
                                    "tag lies in closed region %s of %.0f m2 holding %d tags (unclosed openings); no polygon" % (room["room_id"], room["a_face"].area * k2, len(room["tag_rows"])),
                                    len(room["tag_rows"]), room["a_face"].area * k2, info["page"]))
        rev(room["room_id"], room["tag"], "leaked region", "%d tags in %.0f m2 at page-view box %s; not measured; tags: %s" % (
            len(room["tag_rows"]), room["a_face"].area * k2, ",".join("%.0f" % v for v in room["a_face"].bounds),
            " | ".join(t["text"] for t in room["tag_rows"][:10]) + (" ..." if len(room["tag_rows"]) > 10 else "")))
    covered = {t["t_index"] for r in ctx["vector"]["vec"].regions for t in r.tags}
    missing = [t for t in ctx["tags"] if t["t_index"] not in covered]
    for k, t in enumerate(missing, start=1):
        rid = "%s-U%03d" % (key, k)
        extra.append(_empty_row(rid, key, t, "no closed region", "not measured: tag in no closed region", "tag lies in no closed face of plausible room size", 1, None, info["page"]))
        rev(rid, t["text"], "tag in no closed region", "no polygon")
    for t in ctx["tags"]:
        if t["mode"] == "NA" and "page text layer" in t.get("mode_source", ""):
            rev("", t["text"], "mode label NA recovered from the text layer", t["mode_source"])
    for loose in ctx.get("na_loose", []):
        rev("", "", "NA word without a tag", "word box_pv %s is under no tag name" % loose["word_box_pv"])
    all_rows = rows + extra
    common.write_csv_dicts(P["out_reg"] / ("room_polygons_%s.csv" % key), all_rows, POLY_FIELDS)
    wkt_rows = [{"room_id": r["room_id"], "wall_face_wkt_mm": _wkt_mm(r["a_face"], cb), "centreline_wkt_mm": _wkt_mm(r["a_cl"], cb),
                 "method_b_wkt_mm": _wkt_mm(r["b_face"], cb), "wall_face_wkt_pv": _wkt_pv(r["a_face"]),
                 "frame": "building mm, origin grid 2 / A (grid_model.json); _pv columns: page view pt",
                 "source": "%s page %d, calibrated by src/zoning/calibrate.py%s" % (BG_PDF, info["page"], "; LEAKED REGION (not a room)" if r["leaked"] else "")} for r in all_rooms]
    common.write_csv_dicts(P["out_reg"] / ("room_polygons_%s_wkt.csv" % key), wkt_rows,
                           ["room_id", "wall_face_wkt_mm", "centreline_wkt_mm", "method_b_wkt_mm", "wall_face_wkt_pv", "frame", "source"])
    qd = common.ensure_dir(P["out_qa"] / "zoning_storeys")
    # every tag that has no polygon of its own, with the reason
    nopoly = [{"storey": key, "page": info["page"], "room_id": r["room_id"], "tag": r["room_tag"], "name": r["room_name"], "mode": r["mode"], "reason": r["region_kind"],
               "region_area_m2": r["region_area_m2"], "n_tags_in_region": r["n_tags_in_region"], "box_pv": r["tag_box_pv"], "source_file": BG_PDF} for r in extra]
    for room in rooms:
        if len(room["tags_inside"]) > 1:
            for t in room["tag_rows"][1:]:
                nopoly.append({"storey": key, "page": info["page"], "room_id": room["room_id"], "tag": t["text"], "name": t["name"], "mode": t["mode"],
                               "reason": "shares the polygon of %s (open plan)" % room["room_id"], "region_area_m2": round(room["a_face"].area * k2, 1),
                               "n_tags_in_region": len(room["tag_rows"]), "box_pv": t.get("box_pv", ""), "source_file": BG_PDF})
    common.write_csv_dicts(qd / ("tags_without_polygon_%s.csv" % key), nopoly,
                           ["storey", "page", "room_id", "tag", "name", "mode", "reason", "region_area_m2", "n_tags_in_region", "box_pv", "source_file"])
    common.write_csv_dicts(qd / ("review_%s.csv" % key), review, ["storey", "room_id", "room_tag", "kind", "detail"])
    common.write_csv_dicts(qd / ("appd_agreement_%s.csv" % key), appd_rows,
                           ["storey", "room_id", "room_tag", "appd_annotation_index", "appd_caption_m2", "a_wall_face_m2", "a_centreline_m2", "error_face_pct", "error_centreline_pct",
                            "face_within_2pct_or_0.3m2", "centreline_within_2pct_or_0.3m2", "appd_polygon_own_area_m2", "appd_polygon_own_vs_caption_pct",
                            "overlap_room", "overlap_polygon", "source"])
    stats = dict(stats)
    stats.pop("matches_a", None)
    stats["appd_rows"] = len(appd_rows)
    stats["appd_face_within"] = sum(1 for r in appd_rows if r["face_within_2pct_or_0.3m2"] == "yes")
    stats["appd_centreline_within"] = sum(1 for r in appd_rows if r["centreline_within_2pct_or_0.3m2"] == "yes")
    stats["appd_polygons_total"] = len(appd)
    shared_tags = sum(len(r["tags_inside"]) for r in rooms if len(r["tags_inside"]) > 1)
    return {"rooms": rooms, "rows": rows, "leaked_rooms": leaked, "extra_rows": extra, "stats": stats, "appd": appd, "appd_classes": appd_classes, "n_tags": len(ctx["tags"]),
            "tags_without_polygon": [t["text"] for t in missing], "n_regions": len(all_rooms), "n_regions_measured": len(rooms), "review": review,
            "tag_counts": {"tags": len(ctx["tags"]), "own_polygon": sum(1 for r in rooms if len(r["tags_inside"]) == 1),
                           "shared_open_plan": shared_tags, "leaked": sum(len(r["tag_rows"]) for r in leaked), "no_region": len(missing)}}


MODE_CLASS = {"MV": "MV", "NV": "NV"}


def stub_class(modes: Sequence[str]) -> Tuple[str, float, str]:
    """(class, confidence, note) of a room from the printed mode labels of the tags in its region.  AC, 24HR and unlabelled -> no information."""
    labelled = [m.strip() for m in modes if m and m.strip()]
    if not labelled:
        return "no information", 0.0, "tag carries no mode label"
    cls = {MODE_CLASS.get(m) for m in labelled}
    if len(set(labelled)) > 1 and len(cls) > 1:
        return "no information", 0.0, "conflicting mode labels in one region: %s" % " | ".join(sorted(set(labelled)))
    if cls == {None}:
        return "no information", 0.0, "mode label %s names no AHU (AHU unknown)" % "/".join(sorted(set(labelled)))
    if None in cls:
        return "no information", 0.0, "conflicting mode labels in one region: %s" % " | ".join(sorted(set(labelled)))
    c = cls.pop()
    return c, (1.0 if len(labelled) == len(modes) else 0.5), "" if len(labelled) == len(modes) else "some tags of the region carry no label"


def evidence_zones(ctx: dict) -> List[zn.Zone]:
    """Zones of the documents that carry AHU evidence for this storey: the RFI-ME-0233 sample (L04 only, as in the pilot) and Appendix D (where the
    storey has an App D page).  Polygons are in the background page-view frame."""
    P, info = ctx["P"], ctx["info"]
    C = ctx["calibrate"]
    zones: List[zn.Zone] = []
    if info["key"] == SAMPLE_STOREY and "cal_sample" in C:
        sm = _open(P["root"], SAMPLE_PDF, 0)
        tag_map, listed = zn.governing_tags(common.read_csv_dicts(P["reg"] / "conflicts.csv"), common.read_csv_dicts(P["reg"] / "tag_crossref_rfi_me_0233.csv"))
        polys = zn.read_sample_polygons(sm, common.read_csv_dicts(P["reg"] / "sample_zones.csv"))
        zones += zn.build_sample_zones(polys, C["cal_sample"], C["cal_bg"], tag_map, listed, C["transfer_residual_sample_mm"]["max"])
    comp = ctx["compare"]
    if comp["appd"]:
        az = zn.merge_appd_zones(comp["appd"], comp["appd_classes"])
        for z in az:
            z.transfer_residual_mm = C["transfer_residual_appd_mm"]["max"]
            z.storey_served = z.storey_served or info["key"]
        zones += az
    return zones


def stage_assign(ctx: dict) -> dict:
    """Room assignment table.  Evidence in order: RFI-ME-0233 sample (L04), Appendix D (storeys with an App D page), both through the pilot's
    ``zones.assign_rooms`` (a zone takes a room when it covers more than the configured majority of it); otherwise the printed tag mode label
    alone (MV, NV; anything else -> ``no information``, AHU unknown).  The real AHU evidence is added later by another job."""
    P, info = ctx["P"], ctx["info"]
    key = info["key"]
    path = P["out_reg"] / ("zone_assignment_%s.csv" % key)
    if path.exists() and not ctx.get("force_assign"):
        old = common.read_csv_dicts(path)
        if old and any(not str(r.get("method", "stub")).startswith("stub") for r in old):
            return {"written": False, "reason": "existing assignment is not the stub; left untouched", "n": len(old)}
    comp = ctx["compare"]
    k2 = ctx["calibrate"]["cal_bg"].area_scale_mm2_per_pt2() / 1e6
    zones = evidence_zones(ctx)
    zrooms = [{"room_id": room["room_id"], "polygon": room["a_face"], "area_m2": room["a_face"].area * k2} for room in comp["rooms"]]
    majority, _ = zn.configured_thresholds()
    assigned = {a["room_id"]: a for a in zn.assign_rooms(zrooms, zones, majority=majority)} if zones and zrooms else {}
    zone_by_key = {z.key: z for z in zones}
    rows = []

    def base(room_id, tag, name, modes, area):
        return {"room_id": room_id, "storey": key, "room_tag": tag, "room_name": name, "mode_label": " | ".join(m for m in modes if m) or "", "area_m2": area, "department": ""}
    for room, row in zip(comp["rooms"], comp["rows"]):
        a = assigned.get(room["room_id"])
        r = base(room["room_id"], room["tag"], room["name"], room["modes_inside"], row["a_area_m2"])
        if a is not None and a.get("zone_key"):
            z = zone_by_key[a["zone_key"]]
            cls = z.tag if z.kind == "AHU" else z.kind
            r.update({"class": cls, "class_kind": z.kind, "confidence": round(a["confidence"], 3), "department": z.departments if z.kind == "AHU" else "",
                      "sources": "%s; zone %s covers %.0f %% of the room; %s" % (z.source, z.tag, 100 * a["share"], "; ".join(z.refs[:2])),
                      "method": "stub (evidence: %s)" % z.source, "notes": "%s %s: %s" % (z.kind, z.tag, z.tag_basis) if z.kind != "AHU" else z.tag_basis})
        else:
            cls, conf, note = stub_class(room["modes_inside"])
            cand = a.get("candidate") if a else ""
            if a and a.get("class_label", "").startswith("review"):
                note = ("; ".join(x for x in (note, "partial cover (%.0f %%) by zone %s: not assigned" % (100 * a["candidate_share"], cand)) if x))
            r.update({"class": cls, "class_kind": cls if cls in ("MV", "NV", "FCU") else ("no information" if cls == "no information" else "AHU"), "confidence": conf,
                      "sources": "%s p%s tag text '%s' (mode label printed on the tag); out/registers/room_tags_plans.csv%s" % (
                          BG_PDF, info["page"], room["tag"], "; " + room["tag_row"].get("mode_source", "") if "page text layer" in room["tag_row"].get("mode_source", "") else ""),
                      "method": "stub (mode label only)", "notes": note})
        rows.append(r)
    for er in comp["extra_rows"]:
        cls, conf, note = stub_class([er["mode"]])
        r = base(er["room_id"], er["room_tag"], er["room_name"], [er["mode"]], "")
        r.update({"class": cls, "class_kind": cls if cls in ("MV", "NV", "FCU") else "no information", "confidence": conf,
                  "sources": "%s p%s tag text '%s' (mode label printed on the tag)" % (BG_PDF, info["page"], er["room_tag"]),
                  "method": "stub (mode label only)", "notes": "; ".join(x for x in (note, er["method_agreement"] + ": no polygon, not placed on the sheet") if x)})
        rows.append(r)
    common.write_csv_dicts(path, rows, ASSIGN_FIELDS)
    return {"written": True, "n": len(rows), "evidence_zones": [z.tag for z in zones]}


STAGE_FN = {"calibrate": stage_calibrate, "vector": stage_vector, "raster": stage_raster, "compare": stage_compare, "assign": stage_assign}


def run_storey(key: str, stages: Sequence[str], force_assign: bool = False, fixes: bool = True, work_name: str = "work") -> dict:
    """Run the per-storey stages (cached pickles for the stages not requested)."""
    P = storey_paths(key)
    info = storey_info(key)
    work = common.ensure_dir(P["out"] / work_name)
    P = dict(P, work=work, out_reg=P["reg"], out_qa=P["qa"])
    if not fixes:           # pilot-compatible regression run: outputs kept apart from the registers
        P["out_reg"] = common.ensure_dir(P["out"] / "compat" / "registers")
        P["out_qa"] = common.ensure_dir(P["out"] / "compat" / "qa")
    ctx: dict = {"P": P, "info": info, "tags": load_tags(P["reg"], info["page"]), "codes": load_tags(P["reg"], info["page"], "code"), "force_assign": force_assign,
                 "fixes": fixes}
    n_na, ctx["na_loose"] = recover_na_modes(P["root"], info["page0"], ctx["tags"])
    ctx["n_na_recovered"] = n_na
    wanted = [s for s in stages if s in STOREY_STAGES]
    if not wanted:
        return {"key": key}
    last = max(STOREY_STAGES.index(s) for s in wanted)
    tfile = work / "timings.json"
    timings = json.loads(tfile.read_text()) if tfile.exists() else {}
    for st in STOREY_STAGES[:last + 1]:
        pk = work / ("%s.pkl" % st)
        if st in wanted:
            t0 = time.time()
            ctx[st] = STAGE_FN[st](ctx)
            timings[st] = round(time.time() - t0, 1)
            with pk.open("wb") as fh:
                pickle.dump(ctx[st], fh)
            print("[%s] stage %s done in %.1f s" % (key, st, timings[st]), flush=True)
        elif pk.exists():
            with pk.open("rb") as fh:
                ctx[st] = pickle.load(fh)
        else:
            raise SystemExit("[%s] stage %s needs the cached result of stage %s (%s); run it first" % (key, wanted[0], st, pk))
    tfile.write_text(json.dumps(timings, indent=1))
    return {"key": key, "timings": timings}


def _worker(args):
    return run_storey(*args)


# --------------------------------------------------------------------------- #
# Zones from an assignment table, colours
# --------------------------------------------------------------------------- #
CLASS_COLOURS = {"MV": "#7FC8F8", "NV": "#A8E6A1", "FCU": "#FFB74D"}
AHU_RE = re.compile(r"^(.*-\d{2})([AB])(?:/B)?$")


def zone_key(tag: str) -> Tuple[str, str]:
    """(zone key, display tag): a duty/standby pair (``...-01A`` and ``...-01B``, or ``...-01A/B``) is one zone."""
    t = tag.strip()
    m = AHU_RE.match(t)
    if m:
        return m.group(1), m.group(1) + "A/B"
    return t, t


def sample_colours() -> Dict[str, str]:
    """Governing AHU tag -> fill colour of the RFI-ME-0233 sample (pure red replaced by amber)."""
    P = common.resolve_paths()
    rows = common.read_csv_dicts(P["registers"] / "sample_zones.csv")
    tag_map, _ = zn.governing_tags(common.read_csv_dicts(P["registers"] / "conflicts.csv"), common.read_csv_dicts(P["registers"] / "tag_crossref_rfi_me_0233.csv"))
    out: Dict[str, str] = {}
    for r in rows:
        gov = tag_map.get(r["tags_freetext_inside"], (r["tags_freetext_inside"], ""))[0]
        out[zone_key(gov)[0]] = zn.avoid_pure_red(r["fill_hex"].upper())
    return out


def colour_for(tag_key: str, used: Optional[Dict[str, str]] = None) -> str:
    """The same colour for an AHU on every page: sample colour when the sample has the AHU, else a hash-derived mid-saturation colour, never red."""
    import colorsys
    samp = sample_colours()
    if tag_key in samp:
        return samp[tag_key]
    if tag_key in CLASS_COLOURS:
        return CLASS_COLOURS[tag_key]
    h = int(hashlib.md5(tag_key.encode()).hexdigest(), 16)
    sv = [(0.55, 0.85), (0.7, 0.75), (0.45, 0.95)][(h >> 8) % 3]
    hue = 0.07 + 0.86 * (((h >> 16) % 997) / 997.0)       # 0.07 .. 0.93: stays clear of red at 0 / 1
    avoid = [zn.hex_to_rgb(c) for c in list(samp.values()) + list(CLASS_COLOURS.values())]
    for _ in range(40):
        r, g, b = colorsys.hsv_to_rgb(hue, *sv)
        if min(math.dist((r, g, b), a) for a in avoid) > 0.12:
            break
        hue = 0.07 + ((hue - 0.07 + 0.037) % 0.86)
    return zn.avoid_pure_red("#%02X%02X%02X" % (int(r * 255), int(g * 255), int(b * 255)))


def build_page_zones(key: str, info: dict, P: dict, k2: float) -> Tuple[List[zn.Zone], dict, List[dict], dict]:
    """Zones (union of the room polygons per AHU / class) of a storey from its assignment table; returns zones, stats, room annotations, summary.

    ``k2`` is m2 per pt2 of the page view (calibration of the storey): every area printed on the sheet is in m2, polygon areas included.
    """
    prow = {r["room_id"]: r for r in common.read_csv_dicts(P["reg"] / ("room_polygons_%s.csv" % key))}
    arows = common.read_csv_dicts(P["reg"] / ("zone_assignment_%s.csv" % key))
    groups: Dict[str, dict] = {}
    rooms_ann: List[dict] = []
    unclassified = {"n": 0, "area": 0.0}
    no_polygon = 0
    for a in arows:
        pr = prow.get(a["room_id"])
        if pr is None or not pr["polygon_pv_wkt"]:
            no_polygon += 1
            continue
        poly = shapely.from_wkt(pr["polygon_pv_wkt"])
        area = float(pr["a_area_m2"] or 0.0)
        cls = (a["class"] or "").strip() or "no information"
        conf = float(a["confidence"] or 0.0)
        rooms_ann.append({"room_id": a["room_id"], "polygon": poly,
                          "contents": "%s | %s | %.1f m2 | class: %s (%.0f%%) | A/B: %s" % (a["room_id"], pr["room_tag"], area, cls, 100 * conf, pr["method_agreement"])})
        if cls.lower() in ("no information", "review: partial cover") or cls.lower().startswith("review"):
            unclassified["n"] += 1
            unclassified["area"] += area
            continue
        zk, disp = zone_key(cls)
        g = groups.setdefault(zk, {"tags": set(), "polys": [], "area": 0.0, "n": 0, "depts": set(), "src": set(), "confs": []})
        g["tags"].add(cls)
        g["polys"].append(poly)
        g["area"] += area
        g["n"] += 1
        if a.get("department"):
            g["depts"].add(a["department"])
        g["src"].add(a["sources"])
        g["confs"].append(conf)
    zones, stats = [], {}
    for zk in sorted(groups):
        g = groups[zk]
        tags = sorted(g["tags"])
        disp = zone_key(tags[0])[1] if len(tags) > 1 or AHU_RE.match(tags[0]) else tags[0]
        parts: List[Polygon] = []
        u = shapely.union_all(g["polys"])
        for part in (list(u.geoms) if u.geom_type == "MultiPolygon" else [u]):
            if part.geom_type == "Polygon" and not part.is_empty:
                parts.extend(zn.simple_parts(part))
        is_class = zk in CLASS_COLOURS
        plant, served = ("", "") if is_class else zn.parse_tag_levels(tags[0])
        depts = "; ".join(sorted(g["depts"]))
        if not depts:
            depts = "not stated (class from the printed mode label only)" if is_class else "not stated in the assignment table"
        z = zn.Zone(key=zk, kind=zk if is_class else "AHU", source="zone assignment table", tag=disp, tag_basis="assignment table zone_assignment_%s.csv" % key,
                    polygons_pv=parts, draw_polygons_pv=parts, fill_hex=colour_for(zk), departments=depts,
                    storey_served=key, plant_level=plant and ("L%s" % plant), refs=sorted(g["src"])[:3] + ["out/registers/zone_assignment_%s.csv" % key,
                                                                                                         "out/registers/room_polygons_%s.csv" % key])
        zones.append(z)
        stats[z.key] = {"area_m2": g["area"], "poly_m2": sum(q.area for q in parts) * k2, "n_rooms": g["n"], "min_conf": min(g["confs"]) if g["confs"] else 0.0}
    return zones, stats, rooms_ann, {"unclassified_rooms": unclassified["n"], "unclassified_area_m2": unclassified["area"], "n_rooms": len(arows), "rooms_no_polygon": no_polygon}


# --------------------------------------------------------------------------- #
# PDF and report stages (all storeys)
# --------------------------------------------------------------------------- #
def out_pdf_path(storeys: Sequence[str]) -> Path:
    return common.resolve_paths()["zoning"] / ("AIGH_IP_AHU_Zoning_%s-%s.pdf" % (storeys[0], storeys[-1]))


def stage_pdf(storeys: Sequence[str]) -> dict:
    root = common.project_root()
    specs = []
    summaries = {}
    area_warnings: List[str] = []
    for key in storeys:
        info = storey_info(key)
        P = storey_paths(key)
        cb = pickle.load((P["work"] / "calibrate.pkl").open("rb"))["cal_bg"]
        k2 = cb.area_scale_mm2_per_pt2() / 1e6
        zones, stats, rooms_ann, summ = build_page_zones(key, info, P, k2)
        for zk, st_ in stats.items():          # a zone drawn from the union of its rooms: polygon area and room-area sum agree (guards the unit of poly_m2)
            if st_["area_m2"] > 0 and abs(st_["poly_m2"] - st_["area_m2"]) > 0.02 * st_["area_m2"] + 1.0:
                area_warnings.append("%s zone %s: polygon %.1f m2 vs rooms %.1f m2" % (key, zk, st_["poly_m2"], st_["area_m2"]))
        specs.append({"key": key, "label": info["storey_label"], "drawing": info["drawing"], "page_index": info["page0"], "zones": zones, "zone_stats": stats,
                      "rooms": rooms_ann, "unclassified": summ,
                      "legend_title": "AHU zoning, %s (%s): AHU assignment from the stub table, class from the printed mode label only" % (info["storey_label"], info["drawing"]),
                      "sheet_note": "Zones: union of measured room polygons per class or AHU. Fill opacity 0.35. Layers per AHU or class; room outlines on 'Rooms (measured)' (off). "
                                    "%d rooms not classified (no information), %.0f m2, not filled; %d tags have no polygon (leaked region or no closed region)." % (
                          summ["unclassified_rooms"], summ["unclassified_area_m2"], summ["rooms_no_polygon"])})
        summaries[key] = summ
    out = out_pdf_path(storeys)
    manifest = wp.write_multi_zoning_pdf(root / BG_PDF, specs, out, "AIGH IP AHU zoning, %s to %s" % (storeys[0], storeys[-1]))
    ver = wp.verify_multi_zoning_pdf(out, root / BG_PDF, manifest["page_map"], manifest["expected_layers"], manifest["expected_tags"])
    (common.project_root() / "out" / "zoning" / "storeys").mkdir(parents=True, exist_ok=True)
    ver["zone_area_units_consistent"] = not area_warnings
    ver["zone_area_warnings"] = area_warnings
    if area_warnings:
        ver["ok"] = False
        ver.setdefault("problems", []).extend(area_warnings)
    return {"manifest": manifest, "verify": ver, "pdf": str(out), "summaries": summaries}


def _read_polys(path: Path) -> Dict[str, dict]:
    return {r["room_id"]: r for r in common.read_csv_dicts(path)} if path.exists() else {}


def regression_l04() -> dict:
    """L04 through the new path against the pilot (``out/zoning/pilot/room_polygons_L04_pilot.csv``).

    ``compat``: the pilot-compatible run (``fixes=False``: no dimension-string strip, no heavy-wall re-derivation, no leak rule; same stages, same ids)
    must give the pilot's rooms, tags, A/B status and areas within 0.1 m2 by room id.
    ``production``: the corrected run against the pilot, matched by polygon overlap (ids shift when a region is added or removed); every pilot room
    whose area changed by more than 0.1 m2, or that is no longer measured, is listed.
    """
    P = common.resolve_paths()
    old = _read_polys(P["zoning"] / "pilot" / "room_polygons_L04_pilot.csv")
    out: dict = {"n_pilot": len(old)}
    if not old:
        return {"ok": None, "note": "pilot copy missing"}
    comp_p = storey_paths("L04")["out"] / "compat" / "registers" / "room_polygons_L04.csv"
    cm = _read_polys(comp_p)
    if cm:
        only_old, only_new = sorted(set(old) - set(cm)), sorted(set(cm) - set(old))
        diffs = []
        for k in sorted(set(old) & set(cm)):
            d = abs(float(old[k]["a_area_m2"]) - float(cm[k]["a_area_m2"] or 0))
            if d > 0.1 or old[k]["room_tag"] != cm[k]["room_tag"] or old[k]["method_agreement"] != cm[k]["method_agreement"]:
                diffs.append({"room_id": k, "pilot_a": old[k]["a_area_m2"], "new_a": cm[k]["a_area_m2"], "tag_pilot": old[k]["room_tag"], "tag_new": cm[k]["room_tag"],
                              "agreement_pilot": old[k]["method_agreement"], "agreement_new": cm[k]["method_agreement"]})
        out["compat"] = {"ok": not only_old and not only_new and not diffs, "n_new": len(cm), "only_in_pilot": only_old, "only_in_new": only_new, "differences": diffs,
                         "max_area_diff_m2": max([abs(float(old[k]["a_area_m2"]) - float(cm[k]["a_area_m2"])) for k in set(old) & set(cm)] or [0.0])}
    else:
        out["compat"] = {"ok": None, "note": "compat run missing (run stages with --compat-l04)"}
    new = _read_polys(P["registers"] / "room_polygons_L04.csv")
    if new:
        npoly = {k: shapely.from_wkt(r["polygon_pv_wkt"]) for k, r in new.items() if r["polygon_pv_wkt"]}
        ids = list(npoly)
        tree = STRtree([npoly[i] for i in ids])
        changed, unmeasured, matched, same = [], [], 0, 0
        used = set()
        for k, r in old.items():
            po = shapely.from_wkt(r["polygon_pv_wkt"])
            best, bi = 0.0, None
            for j in tree.query(po, predicate="intersects"):
                q = npoly[ids[j]]
                iou = po.intersection(q).area / max(po.union(q).area, 1e-9)
                if iou > best:
                    best, bi = iou, ids[j]
            if bi is None or best < 0.5:
                tags = [n for n, rr in new.items() if rr["room_tag"] == r["room_tag"] and not rr["polygon_pv_wkt"]]
                unmeasured.append({"pilot_room_id": k, "tag": r["room_tag"], "pilot_a_m2": r["a_area_m2"], "new_status": ", ".join(new[t]["method_agreement"] for t in tags[:1]) or "no overlapping polygon"})
                continue
            matched += 1
            d = abs(float(r["a_area_m2"]) - float(new[bi]["a_area_m2"]))
            if d <= 0.1:
                same += 1
            else:
                changed.append({"pilot_room_id": k, "new_room_id": bi, "tag": r["room_tag"], "pilot_a_m2": r["a_area_m2"], "new_a_m2": new[bi]["a_area_m2"],
                                "basis": new[bi]["boundary_basis"], "flags": new[bi]["flags"][:160]})
        out["production"] = {"n_new_rows": len(new), "matched": matched, "same_within_0.1m2": same, "changed": changed, "pilot_rooms_no_longer_measured": unmeasured}
    return out


def stage_report(storeys: Sequence[str], pdf_result: Optional[dict]) -> dict:
    P = common.resolve_paths()
    rows = []
    for key in storeys:
        SP = storey_paths(key)
        info = storey_info(key)
        comp = pickle.load((SP["work"] / "compare.pkl").open("rb"))
        C = pickle.load((SP["work"] / "calibrate.pkl").open("rb"))
        V = pickle.load((SP["work"] / "vector.pkl").open("rb"))
        R = pickle.load((SP["work"] / "raster.pkl").open("rb"))
        tfile = SP["work"] / "timings.json"
        tm = json.loads(tfile.read_text()) if tfile.exists() else {}
        r = comp["rows"]
        ab = [x for x in r if x["method_agreement"] in ("agree", "differ")]
        agree = sum(1 for x in ab if x["method_agreement"] == "agree")
        single = [x for x in ab if x["region_kind"] == "room"]
        agree_single = sum(1 for x in single if x["method_agreement"] == "agree")
        asg = common.read_csv_dicts(P["registers"] / ("zone_assignment_%s.csv" % key))
        classes: Dict[str, int] = {}
        for a in asg:
            classes[a["class"]] = classes.get(a["class"], 0) + 1
        tc = comp["tag_counts"]
        st = comp["stats"]
        rows.append({"storey": key, "page": info["page"], "drawing": info["drawing"], "tags_total": tc["tags"],
                     "tags_own_polygon": tc["own_polygon"], "tags_in_shared_open_plan_polygon": tc["shared_open_plan"], "tags_in_leaked_regions": tc["leaked"],
                     "tags_in_no_closed_region": tc["no_region"], "rooms_without_polygon": tc["leaked"] + tc["no_region"],
                     "tags_without_own_polygon": tc["tags"] - tc["own_polygon"], "regions_total": comp["n_regions"], "regions_measured": comp["n_regions_measured"],
                     "leaked_regions": len(comp["leaked_rooms"]), "ab_compared": len(ab), "ab_agree": agree,
                     "ab_agree_share": round(agree / len(ab), 4) if ab else "", "ab_target_met": "yes" if ab and agree / len(ab) >= AB_TARGET else "no",
                     "ab_agree_share_single_tag_rooms": round(agree_single / len(single), 4) if single else "",
                     "boundary_wall_closed": sum(1 for x in r if str(x["boundary_check"]).startswith("wall-closed")),
                     "boundary_wall_only_adopted": sum(1 for x in r if str(x["boundary_check"]).startswith("wall-only")),
                     "boundary_fine_pen_needed": sum(1 for x in r if str(x["boundary_check"]).startswith("fine-pen")),
                     "boundary_unresolved": sum(1 for x in r if str(x["boundary_check"]).startswith("UNRESOLVED")),
                     "ab_agree_and_wall_boundary_ok": sum(1 for x in ab if x["method_agreement"] == "agree" and str(x["boundary_check"]).startswith(("wall-closed", "wall-only"))),
                     "no_b_region": sum(1 for x in r if x["method_agreement"] == "no B region"), "ab_differ": sum(1 for x in r if x["method_agreement"] == "differ"),
                     "boundary_rederived_from_heavy_walls": sum(1 for x in r if str(x["boundary_basis"]).startswith("heavy")),
                     "dimension_lines_removed": V["vec"].diagnostics.get("dimension_strip", {}).get("n_dimension_lines", ""),
                     "raster_mm_per_px": round(R["diag"].get("target_mm_per_px", 0.0), 2) if R.get("diag") else "",
                     "na_mode_labels_recovered": sum(1 for t in comp["review"] if t["kind"].startswith("mode label NA")),
                     "calibration_basis": C["cal_basis"], "cal_max_residual_mm": round(C["cal_bg"].max_mm, 2), "cal_scale_dev_vs_register_pct": round(C["scale_dev_vs_register_pct"], 4),
                     "appd_page": info["appd_page"] or "", "appd_polygons": st.get("appd_polygons_total", ""), "appd_matched_rooms": st.get("appd_rows", ""),
                     "appd_wall_face_within_2pct_or_0.3m2": st.get("appd_face_within", ""), "appd_centreline_within_2pct_or_0.3m2": st.get("appd_centreline_within", ""),
                     "classes": "; ".join("%s=%d" % kv for kv in sorted(classes.items())),
                     "seconds_calibrate": tm.get("calibrate", ""), "seconds_vector": tm.get("vector", ""), "seconds_raster": tm.get("raster", ""),
                     "seconds_compare": tm.get("compare", ""), "seconds_total": round(sum(v for v in tm.values()), 1) if tm else "",
                     "calibration_notes": " | ".join(C["notes"])})
    ver = (pdf_result or {}).get("verify")
    if ver:
        for row in rows:
            row["pdf_check_ok"] = ver["ok"]
    name = "zoning_storeys_%s-%s.csv" % (storeys[0], storeys[-1])
    fields = list(rows[0].keys())
    common.write_csv_dicts(P["qa"] / name, rows, fields)
    reg = regression_l04() if "L04" in storeys else None
    summary = {"rows": rows, "pdf_verify": ver, "regression_L04": reg, "qa_csv": str(P["qa"] / name)}
    (P["qa"] / ("zoning_storeys_%s-%s.json" % (storeys[0], storeys[-1]))).write_text(json.dumps(summary, indent=1, default=str))
    return summary


# --------------------------------------------------------------------------- #
def run(storeys: Sequence[str], stages: Sequence[str], jobs: int = 1, force_assign: bool = False, compat_l04: bool = False) -> dict:
    result: dict = {}
    if compat_l04:      # pilot-compatible L04 (no corrections), kept apart: out/zoning/storeys/L04/compat, for the regression
        run_storey("L04", ["calibrate", "vector", "raster", "compare"], False, False, "work_compat")
    per = [s for s in stages if s in STOREY_STAGES]
    if per:
        args = [(k, per, force_assign) for k in storeys]
        if jobs > 1:
            with ProcessPoolExecutor(max_workers=jobs) as ex:
                list(ex.map(_worker, args))
        else:
            for a in args:
                run_storey(*a)
    pdfr = None
    if "pdf" in stages:
        t0 = time.time()
        pdfr = stage_pdf(storeys)
        print("stage pdf done in %.1f s; check ok = %s" % (time.time() - t0, pdfr["verify"]["ok"]), flush=True)
        result["pdf"] = pdfr
    if "report" in stages:
        result["report"] = stage_report(storeys, pdfr)
        print("report written: %s" % result["report"]["qa_csv"], flush=True)
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--storeys", default=",".join(ALL_STOREYS))
    r.add_argument("--stages", default=",".join(STAGES))
    r.add_argument("--jobs", type=int, default=1, help="storeys processed in parallel")
    r.add_argument("--force-assign", action="store_true", help="overwrite the assignment CSV even if it is not the stub")
    r.add_argument("--compat-l04", action="store_true", help="also run L04 without the corrections (pilot-compatible) for the regression check")
    a = ap.parse_args(argv)
    storeys = [s.strip() for s in a.storeys.split(",") if s.strip()]
    for s in storeys:
        if s not in ALL_STOREYS:
            raise SystemExit("unknown storey %s (expected %s)" % (s, ",".join(ALL_STOREYS)))
    run(storeys, [s.strip() for s in a.stages.split(",")], a.jobs, a.force_assign, a.compat_l04)
    return 0


if __name__ == "__main__":
    sys.exit(main())
