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
               "area_printed_m2", "flags", "source_file", "source_page", "tag_box_pv", "tag_method", "measurement_confidence", "method", "coord_frame"]
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
    return rows


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
    return out


def stage_vector(ctx: dict) -> dict:
    P = ctx["P"]
    bg = _open(P["root"], BG_PDF, ctx["info"]["page0"])
    ss = lw.extract_strokes(bg)
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
    return {"vec": vec}


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
    diag["seconds"] = round(time.time() - t0, 1)
    return {"regions": res, "diag": diag}


def _sorted_regions(vec: rv.VectorResult) -> List[int]:
    cent = [(r.polygon.centroid.y, r.polygon.centroid.x) for r in vec.regions]
    return sorted(range(len(vec.regions)), key=lambda i: (round(cent[i][0] / 60.0), cent[i][1]))


def assemble_rooms(ctx: dict) -> List[dict]:
    """Room dicts for the comparison (same rules and ordering as the pilot ``pipeline.assemble_rooms``; ids ``<KEY>-Rnnn``)."""
    key = ctx["info"]["key"]
    vec: rv.VectorResult = ctx["vector"]["vec"]
    ras = ctx["raster"]
    cb: cal.Calibration = ctx["calibrate"]["cal_bg"]
    k2 = cb.area_scale_mm2_per_pt2() / 1e6
    by_tag = {sr.tag_index: sr for sr in ras["regions"]}
    gaps = vec.wall_gaps_pt
    like = [f for f, ok in zip(vec.faces, rv._room_like(vec.faces, k2)) if ok]
    tree = STRtree(like)
    rooms = []
    for n, i in enumerate(_sorted_regions(vec), start=1):
        reg = vec.regions[i]
        face = reg.polygon
        half = float(gaps[i]) / 2.0
        a_cl = rv.centreline_polygon(face, half, tree, like)
        c = face.representative_point()
        tg = sorted(reg.tags, key=lambda t: (t["x_pv"] - c.x) ** 2 + (t["y_pv"] - c.y) ** 2)
        srs = [by_tag.get(t["t_index"]) for t in reg.tags]
        flags = []
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
        rooms.append({"room_id": "%s-R%03d" % (key, n), "tag": tg[0]["text"], "name": tg[0]["name"], "mode": tg[0]["mode"], "number": tg[0].get("number", ""),
                      "tag_row": tg[0], "tags_inside": [t["text"] for t in tg], "modes_inside": [t["mode"] for t in tg],
                      "a_face": face, "a_cl": a_cl, "b_face": b_face, "b_cl": b_cl,
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


def stage_compare(ctx: dict) -> dict:
    P, info = ctx["P"], ctx["info"]
    C = ctx["calibrate"]
    cb = C["cal_bg"]
    key = info["key"]
    appd, appd_classes = [], []
    if info["appd_page0"] is not None:
        ad = _open(P["root"], APPD_PDF, info["appd_page0"])
        appd = cmp.load_appd_polygons(ad, C["cal_appd"], cb)
        pg = str(info["appd_page"])
        appd_classes = zn.classify_appd_spaces(appd, [r for r in common.read_csv_dicts(P["reg"] / "appd_spaces.csv") if r["page"] == pg],
                                               [r for r in common.read_csv_dicts(P["reg"] / "appd_equipment.csv") if r["page"] == pg])
    rooms = assemble_rooms(ctx)
    rows, stats = cmp.build_room_rows(rooms, appd, cb, C["grid"], convention=CONVENTION)
    cls_by_idx = {c["annotation_index"]: c for c in appd_classes}
    appd_src = "; App D: %s p%s" % (APPD_PDF, info["appd_page"]) if info["appd_page"] else "; no App D page for this storey"
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
        r["source_file"] = BG_PDF
        r["source_page"] = info["page"]
        r["tag_box_pv"] = tag.get("box_pv", "")
        r["tag_method"] = tag.get("method", "")
        agree = r["method_agreement"]
        r["measurement_confidence"] = {"agree": "high: A and B agree (2 % or 0.3 m2)", "differ": "medium: A (primary); B differs beyond tolerance",
                                       "no B region": "medium: A only, no B region"}.get(agree, agree)
        r["method"] = "A vector closed regions (primary); B raster region growing (check)%s" % appd_src
        r["coord_frame"] = FRAME_TEXT
    common.write_csv_dicts(P["reg"] / ("room_polygons_%s.csv" % key), rows, POLY_FIELDS)
    wkt_rows = [{"room_id": r["room_id"], "wall_face_wkt_mm": _wkt_mm(r["a_face"], cb), "centreline_wkt_mm": _wkt_mm(r["a_cl"], cb),
                 "method_b_wkt_mm": _wkt_mm(r["b_face"], cb), "wall_face_wkt_pv": _wkt_pv(r["a_face"]),
                 "frame": "building mm, origin grid 2 / A (grid_model.json); _pv columns: page view pt",
                 "source": "%s page %d, calibrated by src/zoning/calibrate.py" % (BG_PDF, info["page"])} for r in rooms]
    common.write_csv_dicts(P["reg"] / ("room_polygons_%s_wkt.csv" % key), wkt_rows,
                           ["room_id", "wall_face_wkt_mm", "centreline_wkt_mm", "method_b_wkt_mm", "wall_face_wkt_pv", "frame", "source"])
    # room tags that ended up in no region
    covered = {t["t_index"] for r in ctx["vector"]["vec"].regions for t in r.tags}
    missing = [t for t in ctx["tags"] if t["t_index"] not in covered]
    common.write_csv_dicts(P["qa"] / "zoning_storeys" / ("tags_without_polygon_%s.csv" % key),
                           [{"storey": key, "page": info["page"], "tag": t["text"], "name": t["name"], "mode": t["mode"], "x_pv": t["x_pv"], "y_pv": t["y_pv"],
                             "box_pv": t.get("box_pv", ""), "source_file": BG_PDF} for t in missing],
                           ["storey", "page", "tag", "name", "mode", "x_pv", "y_pv", "box_pv", "source_file"])
    stats = dict(stats)
    stats.pop("matches_a", None)
    return {"rooms": rooms, "rows": rows, "stats": stats, "appd": appd, "appd_classes": appd_classes, "n_tags": len(ctx["tags"]),
            "tags_without_polygon": [t["text"] for t in missing], "n_regions": len(rooms)}


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


def stage_assign(ctx: dict) -> dict:
    P, info = ctx["P"], ctx["info"]
    key = info["key"]
    path = P["reg"] / ("zone_assignment_%s.csv" % key)
    if path.exists() and not ctx.get("force_assign"):
        old = common.read_csv_dicts(path)
        if old and any(r.get("method", "stub") != "stub" for r in old):
            return {"written": False, "reason": "existing assignment is not the stub; left untouched", "n": len(old)}
    rows = []
    for room, row in zip(ctx["compare"]["rooms"], ctx["compare"]["rows"]):
        cls, conf, note = stub_class(room["modes_inside"])
        rows.append({"room_id": room["room_id"], "storey": key, "room_tag": room["tag"], "room_name": room["name"], "mode_label": " | ".join(m for m in room["modes_inside"] if m) or "",
                     "class": cls, "class_kind": cls if cls in ("MV", "NV", "FCU") else ("no information" if cls == "no information" else "AHU"),
                     "confidence": conf, "area_m2": row["a_area_m2"], "department": "",
                     "sources": "%s p%s tag text '%s' (mode label printed on the tag); out/registers/room_tags_plans.csv" % (BG_PDF, info["page"], room["tag"]),
                     "method": "stub", "notes": note})
    common.write_csv_dicts(path, rows, ASSIGN_FIELDS)
    return {"written": True, "n": len(rows)}


STAGE_FN = {"calibrate": stage_calibrate, "vector": stage_vector, "raster": stage_raster, "compare": stage_compare, "assign": stage_assign}


def run_storey(key: str, stages: Sequence[str], force_assign: bool = False) -> dict:
    """Run the per-storey stages (cached pickles for the stages not requested)."""
    P = storey_paths(key)
    info = storey_info(key)
    work = common.ensure_dir(P["work"])
    ctx: dict = {"P": P, "info": info, "tags": load_tags(P["reg"], info["page"]), "codes": load_tags(P["reg"], info["page"], "code"), "force_assign": force_assign}
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


def build_page_zones(key: str, info: dict, P: dict) -> Tuple[List[zn.Zone], dict, List[dict], dict]:
    """Zones (union of the room polygons per AHU / class) of a storey from its assignment table; returns zones, stats, room annotations, summary."""
    prow = {r["room_id"]: r for r in common.read_csv_dicts(P["reg"] / ("room_polygons_%s.csv" % key))}
    arows = common.read_csv_dicts(P["reg"] / ("zone_assignment_%s.csv" % key))
    groups: Dict[str, dict] = {}
    rooms_ann: List[dict] = []
    unclassified = {"n": 0, "area": 0.0}
    for a in arows:
        pr = prow.get(a["room_id"])
        if pr is None or not pr["polygon_pv_wkt"]:
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
        z = zn.Zone(key=zk, kind=zk if is_class else "AHU", source="zone assignment table", tag=disp, tag_basis="assignment table room_assignment_%s" % key,
                    polygons_pv=parts, draw_polygons_pv=parts, fill_hex=colour_for(zk),
                    departments="; ".join(sorted(g["depts"])) or "not stated (stub assignment: class from the printed mode label only)" if is_class
                    else ("; ".join(sorted(g["depts"])) or "not stated in the assignment table"),
                    storey_served=key, plant_level=plant and ("L%s" % plant), refs=sorted(g["src"])[:3] + ["out/registers/zone_assignment_%s.csv" % key,
                                                                                                         "out/registers/room_polygons_%s.csv" % key])
        zones.append(z)
        stats[z.key] = {"area_m2": g["area"], "poly_m2": sum(p.area for p in parts), "n_rooms": g["n"], "min_conf": min(g["confs"]) if g["confs"] else 0.0}
    return zones, stats, rooms_ann, {"unclassified_rooms": unclassified["n"], "unclassified_area_m2": unclassified["area"], "n_rooms": len(arows)}


# --------------------------------------------------------------------------- #
# PDF and report stages (all storeys)
# --------------------------------------------------------------------------- #
def out_pdf_path(storeys: Sequence[str]) -> Path:
    return common.resolve_paths()["zoning"] / ("AIGH_IP_AHU_Zoning_%s-%s.pdf" % (storeys[0], storeys[-1]))


def stage_pdf(storeys: Sequence[str]) -> dict:
    root = common.project_root()
    specs = []
    summaries = {}
    k_per_pt2 = {}
    for key in storeys:
        info = storey_info(key)
        P = storey_paths(key)
        zones, stats, rooms_ann, summ = build_page_zones(key, info, P)
        cb = pickle.load((P["work"] / "calibrate.pkl").open("rb"))["cal_bg"]
        specs.append({"key": key, "label": info["storey_label"], "drawing": info["drawing"], "page_index": info["page0"], "zones": zones, "zone_stats": stats,
                      "rooms": rooms_ann, "unclassified": summ,
                      "legend_title": "AHU zoning, %s (%s): AHU assignment from the stub table, class from the printed mode label only" % (info["storey_label"], info["drawing"]),
                      "sheet_note": "Zones: union of measured room polygons per class or AHU. Fill opacity 0.35. Layers per AHU or class; room outlines on 'Rooms (measured)' (off). "
                                    "%d rooms not classified (no information), %.0f m2, not filled." % (summ["unclassified_rooms"], summ["unclassified_area_m2"])})
        summaries[key] = summ
    out = out_pdf_path(storeys)
    manifest = wp.write_multi_zoning_pdf(root / BG_PDF, specs, out, "AIGH IP AHU zoning, %s to %s" % (storeys[0], storeys[-1]))
    ver = wp.verify_multi_zoning_pdf(out, root / BG_PDF, manifest["page_map"], manifest["expected_layers"], manifest["expected_tags"])
    (common.project_root() / "out" / "zoning" / "storeys").mkdir(parents=True, exist_ok=True)
    return {"manifest": manifest, "verify": ver, "pdf": str(out), "summaries": summaries}


def regression_l04() -> dict:
    """New L04 room polygons against the pilot's (``out/zoning/pilot/room_polygons_L04_pilot.csv``): same rooms, areas within 0.1 m2."""
    P = common.resolve_paths()
    old_p = P["zoning"] / "pilot" / "room_polygons_L04_pilot.csv"
    new_p = P["registers"] / "room_polygons_L04.csv"
    if not old_p.exists() or not new_p.exists():
        return {"ok": None, "note": "pilot copy or new polygons missing"}
    old = {r["room_id"]: r for r in common.read_csv_dicts(old_p)}
    new = {r["room_id"]: r for r in common.read_csv_dicts(new_p)}
    only_old, only_new = sorted(set(old) - set(new)), sorted(set(new) - set(old))
    diffs = []
    for k in sorted(set(old) & set(new)):
        d = abs(float(old[k]["a_area_m2"]) - float(new[k]["a_area_m2"]))
        tag_same = old[k]["room_tag"] == new[k]["room_tag"]
        agree_same = old[k]["method_agreement"] == new[k]["method_agreement"]
        if d > 0.1 or not tag_same or not agree_same:
            diffs.append({"room_id": k, "pilot_a": old[k]["a_area_m2"], "new_a": new[k]["a_area_m2"], "tag_pilot": old[k]["room_tag"], "tag_new": new[k]["room_tag"],
                          "agreement_pilot": old[k]["method_agreement"], "agreement_new": new[k]["method_agreement"]})
    return {"ok": not only_old and not only_new and not diffs, "n_pilot": len(old), "n_new": len(new), "only_in_pilot": only_old, "only_in_new": only_new,
            "differences": diffs, "max_area_diff_m2": max([abs(float(old[k]["a_area_m2"]) - float(new[k]["a_area_m2"])) for k in set(old) & set(new)] or [0.0])}


def stage_report(storeys: Sequence[str], pdf_result: Optional[dict]) -> dict:
    P = common.resolve_paths()
    rows = []
    for key in storeys:
        SP = storey_paths(key)
        info = storey_info(key)
        comp = pickle.load((SP["work"] / "compare.pkl").open("rb"))
        C = pickle.load((SP["work"] / "calibrate.pkl").open("rb"))
        tfile = SP["work"] / "timings.json"
        tm = json.loads(tfile.read_text()) if tfile.exists() else {}
        r = comp["rows"]
        ab = [x for x in r if x["method_agreement"] in ("agree", "differ")]
        agree = sum(1 for x in ab if x["method_agreement"] == "agree")
        asg = common.read_csv_dicts(P["registers"] / ("zone_assignment_%s.csv" % key))
        classes: Dict[str, int] = {}
        for a in asg:
            classes[a["class"]] = classes.get(a["class"], 0) + 1
        rows.append({"storey": key, "page": info["page"], "drawing": info["drawing"], "tags_total": comp["n_tags"], "regions_rooms": comp["n_regions"],
                     "tags_without_polygon": len(comp["tags_without_polygon"]), "ab_compared": len(ab), "ab_agree": agree,
                     "ab_agree_share": round(agree / len(ab), 4) if ab else "", "ab_target_met": "yes" if ab and agree / len(ab) >= AB_TARGET else "no",
                     "no_b_region": sum(1 for x in r if x["method_agreement"] == "no B region"),
                     "calibration_basis": C["cal_basis"], "cal_max_residual_mm": round(C["cal_bg"].max_mm, 2), "cal_scale_dev_vs_register_pct": round(C["scale_dev_vs_register_pct"], 4),
                     "appd_page": info["appd_page"] or "", "appd_matched_rooms": comp["stats"].get("n_matched_A", ""),
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
def run(storeys: Sequence[str], stages: Sequence[str], jobs: int = 1, force_assign: bool = False) -> dict:
    result: dict = {}
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
    a = ap.parse_args(argv)
    storeys = [s.strip() for s in a.storeys.split(",") if s.strip()]
    for s in storeys:
        if s not in ALL_STOREYS:
            raise SystemExit("unknown storey %s (expected %s)" % (s, ",".join(ALL_STOREYS)))
    run(storeys, [s.strip() for s in a.stages.split(",")], a.jobs, a.force_assign)
    return 0


if __name__ == "__main__":
    sys.exit(main())
