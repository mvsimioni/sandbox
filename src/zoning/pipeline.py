"""Pilot driver: 4th storey IP (AR-IP-01-04-001), Sections 7.1 to 7.4.

``python3 -I src/zoning/pilot_l4.py [--stages calibrate,vector,raster,compare,zones,pdf,report] [--work DIR]``

Intermediate results are cached as pickles in the work directory (default: ``out/zoning/pilot/work``) so a later
stage can be re-run without repeating the slow ones (method A about 2 min, method B several minutes).
"""

from __future__ import annotations

import argparse
import json
import math
import pickle
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np
import shapely
from shapely.geometry import Polygon
from shapely.strtree import STRtree

from .. import common
from . import calibrate as cal
from . import compare as cmp
from . import linework as lw
from . import rooms_raster as rr
from . import report as rep
from . import rooms_vector as rv
from . import write_pdf as wp
from . import zones as zn

BG_PDF = "data/source/Basis/Arch Combined Tender .pdf"
SAMPLE_PDF = "data/source/Template/AHU Zoning - RFI-ME-0233.pdf"
APPD_PDF = "data/source/Basis/App D/Appendix D - ACMV For Common Areas_Combined.pdf"
BG_PAGE = 7          # 0-based; page 8 of the combined tender PDF (AR-IP-01-04-001)
APPD_PAGE = 4        # 0-based; page 5 of App D
DRAWING = "AR-IP-01-04-001"
STOREY = "L04"


def paths() -> dict:
    root = common.project_root()
    P = common.resolve_paths()
    out = P["zoning"] / "pilot"
    return {"root": root, "reg": P["registers"], "qa": P["qa"], "out": out, "work": out / "work"}


def _open(root: Path, rel: str, page: int):
    import pymupdf
    return pymupdf.open(str(root / rel))[page]


def load_tags(reg: Path) -> List[dict]:
    rows = [r for r in common.read_csv_dicts(reg / "room_tags_plans.csv") if r["page"] == "8" and r["kind"] == "room"]
    for i, r in enumerate(rows):
        r["x_pv"] = float(r["x_pv"]); r["y_pv"] = float(r["y_pv"]); r["t_index"] = i
    return rows


def load_code_tags(reg: Path) -> List[dict]:
    """Shaft and riser codes of the sheet (lift cars, risers): blockers, not rooms."""
    rows = [r for r in common.read_csv_dicts(reg / "room_tags_plans.csv") if r["page"] == "8" and r["kind"] == "code"]
    for r in rows:
        r["x_pv"] = float(r["x_pv"]); r["y_pv"] = float(r["y_pv"])
    return rows


# --------------------------------------------------------------------------- #
def stage_calibrate(ctx: dict) -> dict:
    P = ctx["P"]
    grid = cal.load_grid_model(P["reg"] / "grid_model.json")
    bg = _open(P["root"], BG_PDF, BG_PAGE)
    sm = _open(P["root"], SAMPLE_PDF, 0)
    ad = _open(P["root"], APPD_PDF, APPD_PAGE)
    cb, bb = cal.calibrate_page(bg, "background p8")
    cs, bs = cal.calibrate_page(sm, "sample p1")
    ca, ba = cal.calibrate_page(ad, "App D p5")
    creg, breg = cal.calibrate_page(bg, register_csv=P["reg"] / "grid_observations_plans.csv", page_number=8)
    plan_cal = [r for r in common.read_csv_dicts(P["reg"] / "sheet_calibration_plans.csv") if r["page"] == "8"][0]
    out = {
        "grid": grid, "cal_bg": cb, "cal_sample": cs, "cal_appd": ca, "cal_bg_register": creg,
        "bubbles": {"bg": bb, "sample": bs, "appd": ba, "bg_register": breg},
        "frame_sample_vs_bg": cal.compare_frames(bb, bs), "frame_appd_vs_bg": cal.compare_frames(bb, ba),
        "frame_register_vs_detected": cal.compare_frames(breg, bb),
        "register_row": plan_cal,
    }
    # transfer residual: transform each sample/App D bubble centre into the background frame and compare with the bg bubble
    for name, c, bubs in (("sample", cs, bs), ("appd", ca, ba)):
        bgm = {(b.axis, b.label): b for b in bb}
        errs = []
        for b in bubs:
            o = bgm.get((b.axis, b.label))
            if o is None or b.offset or o.offset:
                continue
            q = cal.transfer_points(np.array([[b.x, b.y]]), c, cb)[0]
            X1, Y1 = cb.to_building(q[0], q[1]); X0, Y0 = cb.to_building(o.x, o.y)
            # numbered bubbles pin X only, lettered Y only
            errs.append(abs(X1 - X0) if b.axis == "numbered" else abs(Y1 - Y0))
        out["transfer_residual_%s_mm" % name] = {"max": float(max(errs)), "rms": float(np.sqrt(np.mean(np.square(errs)))), "n": len(errs)}
    return out


def stage_vector(ctx: dict) -> dict:
    P = ctx["P"]
    bg = _open(P["root"], BG_PDF, BG_PAGE)
    ss = lw.extract_strokes(bg)
    cb: cal.Calibration = ctx["cal"]["cal_bg"]
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
    cb: cal.Calibration = ctx["cal"]["cal_bg"]
    t0 = time.time()
    res, diag = rr.measure_rooms(vec.wall_segments, vec.thin_segments, ctx["tags"], 0.5 * (cb.scale_x_mm_per_pt + cb.scale_y_mm_per_pt), blocker_tags=ctx["codes"])
    diag["seconds"] = round(time.time() - t0, 1)
    return {"regions": res, "diag": diag}


def _sorted_regions(vec: rv.VectorResult) -> List[int]:
    cent = [(r.polygon.centroid.y, r.polygon.centroid.x) for r in vec.regions]
    return sorted(range(len(vec.regions)), key=lambda i: (round(cent[i][0] / 60.0), cent[i][1]))


def assemble_rooms(ctx: dict) -> List[dict]:
    """Room dicts for the comparison: A face and centre line, B face and centre line, tags."""
    vec: rv.VectorResult = ctx["vector"]["vec"]
    ras = ctx["raster"]
    cb: cal.Calibration = ctx["cal"]["cal_bg"]
    k2 = cb.area_scale_mm2_per_pt2() / 1e6
    tags = ctx["tags"]
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
        rooms.append({"room_id": "%s-R%03d" % (STOREY, n), "tag": tg[0]["text"], "name": tg[0]["name"], "mode": tg[0]["mode"],
                      "tags_inside": [t["text"] for t in tg], "a_face": face, "a_cl": a_cl, "b_face": b_face, "b_cl": b_cl,
                      "b_face_area_m2": b_area, "flags": "; ".join(flags), "vec_index": i, "gap_pt": float(gaps[i])})
    return rooms


def stage_compare(ctx: dict) -> dict:
    P = ctx["P"]
    cb = ctx["cal"]["cal_bg"]; ca = ctx["cal"]["cal_appd"]
    ad = _open(P["root"], APPD_PDF, APPD_PAGE)
    appd = cmp.load_appd_polygons(ad, ca, cb)
    rooms = assemble_rooms(ctx)
    rows, stats = cmp.build_room_rows(rooms, appd, cb, ctx["cal"]["grid"])
    for r in rows:
        r["storey"] = STOREY
        r["source"] = "method A: %s; method B: raster %s; App D: %s p5" % (BG_PDF, "seeded region growing", APPD_PDF)
    fields = ["room_id", "storey", "room_tag", "room_name", "mode", "n_tags_in_region", "other_tags", "a_area_m2", "a_perimeter_m", "b_area_m2",
              "wall_face_area_m2", "centreline_area_m2", "boundary_convention", "centroid_x_mm", "centroid_y_mm", "grid_location",
              "method_agreement", "a_minus_b_pct", "appd_caption_m2", "appd_annotation_index", "appd_overlap_room", "appd_overlap_polygon",
              "error_vs_caption_pct", "error_b_vs_caption_pct", "flags", "source"]
    common.write_csv_dicts(P["reg"] / "room_polygons_L04.csv", rows, fields)
    # the wall-face polygons (and the centre-line and method B outlines) in building mm, as WKT
    cb_ = ctx["cal"]["cal_bg"]

    def to_mm(poly):
        if poly is None:
            return ""
        def conv(coords):
            xs, ys = cb_.to_building(np.array([c[0] for c in coords]), np.array([c[1] for c in coords]))
            return list(zip(np.round(xs, 0).tolist(), np.round(ys, 0).tolist()))
        shell = conv(poly.exterior.coords)
        holes = [conv(r.coords) for r in poly.interiors]
        return Polygon(shell, holes).wkt

    wkt_rows = [{"room_id": r["room_id"], "wall_face_wkt_mm": to_mm(r["a_face"]), "centreline_wkt_mm": to_mm(r["a_cl"]),
                 "method_b_wkt_mm": to_mm(r["b_face"]), "frame": "building mm, origin grid 2 / A, X along numbered grids, Y along lettered grids (grid_model.json)",
                 "source": "%s page 8, calibrated by src/zoning/calibrate.py" % BG_PDF} for r in rooms]
    common.write_csv_dicts(P["reg"] / "room_polygons_L04_wkt.csv", wkt_rows, ["room_id", "wall_face_wkt_mm", "centreline_wkt_mm", "method_b_wkt_mm", "frame", "source"])
    return {"appd": appd, "rooms": rooms, "rows": rows, "stats": stats}


def stage_zones(ctx: dict) -> dict:
    P = ctx["P"]
    C = ctx["cal"]
    sm = _open(P["root"], SAMPLE_PDF, 0)
    ad = _open(P["root"], APPD_PDF, APPD_PAGE)
    sample_rows = common.read_csv_dicts(P["reg"] / "sample_zones.csv")
    tag_map, listed = zn.governing_tags(common.read_csv_dicts(P["reg"] / "conflicts.csv"), common.read_csv_dicts(P["reg"] / "tag_crossref_rfi_me_0233.csv"))
    polys = zn.read_sample_polygons(sm, sample_rows)
    sample_zones = zn.build_sample_zones(polys, C["cal_sample"], C["cal_bg"], tag_map, listed, C["transfer_residual_sample_mm"]["max"])
    appd = ctx["compare"]["appd"]
    classes = zn.classify_appd_spaces(appd, common.read_csv_dicts(P["reg"] / "appd_spaces.csv") and [r for r in common.read_csv_dicts(P["reg"] / "appd_spaces.csv") if r["page"] == "5"],
                                      [r for r in common.read_csv_dicts(P["reg"] / "appd_equipment.csv") if r["page"] == "5"])
    appd_zones = zn.merge_appd_zones(appd, classes)
    for z in appd_zones:
        z.transfer_residual_mm = C["transfer_residual_appd_mm"]["max"]
    zones = sample_zones + appd_zones
    k2 = C["cal_bg"].area_scale_mm2_per_pt2() / 1e6
    rooms = ctx["compare"]["rooms"]
    rows = ctx["compare"]["rows"]
    conv = ctx["compare"]["stats"]["adopted_convention"]
    zrooms = []
    for r, row in zip(rooms, rows):
        poly = r["a_face"] if conv == "wall_face" else r["a_cl"]
        zrooms.append({"room_id": r["room_id"], "polygon": poly, "area_m2": poly.area * k2, "tag": r["tag"], "name": r["name"]})
    assigned = zn.assign_rooms(zrooms, zones)
    stats: Dict[str, dict] = {}
    for z in zones:
        stats[z.key] = {"area_m2": 0.0, "poly_m2": sum(p.area for p in z.polygons_pv) * k2, "n_rooms": 0}
    for a in assigned:
        if a["zone_key"]:
            s = stats[a["zone_key"]]
            s["area_m2"] += a["area_m2"] * 1.0
            s["n_rooms"] += 1
    return {"zones": zones, "assigned": assigned, "zone_stats": stats, "appd_classes": classes, "tag_map": tag_map, "zrooms": zrooms}


def stage_pdf(ctx: dict) -> dict:
    P = ctx["P"]
    Z = ctx["zones"]
    zones: List[zn.Zone] = Z["zones"]
    out_pdf = P["out"] / "AIGH_IP_AHU_Zoning_L04_pilot.pdf"
    rooms_ann = []
    for a, r in zip(Z["assigned"], ctx["compare"]["rooms"]):
        zr = [x for x in Z["zrooms"] if x["room_id"] == a["room_id"]][0]
        rooms_ann.append({"room_id": a["room_id"], "polygon": r["a_face"],
                          "contents": "%s | %s | %.1f m2 | zone: %s (%.0f%%)" % (a["room_id"], r["tag"], zr["area_m2"], a["class_label"], 100 * a["confidence"])})
    manifest = wp.write_zoning_pdf(P["root"] / BG_PDF, BG_PAGE, out_pdf, zones, Z["zone_stats"], rooms_ann,
                                   "AHU zoning, 4th storey (IP): pilot (AR-IP-01-04-001, tender Corrigendum 6)",
                                   "Zones: RFI-ME-0233 sample (5 Nov 2026) and Appendix D (1 Aug 2025). Fill opacity 0.35. Layers per AHU; room outlines on 'Rooms (measured)' (off).")
    expect_tags = [z.tag for z in zones]
    expect_layers = [wp.layer_name(z) for z in zones] + [wp.LEGEND_LAYER, wp.ROOMS_LAYER]
    ver = wp.verify_zoning_pdf(out_pdf, P["root"] / BG_PDF, BG_PAGE, expect_tags, expect_layers)
    import pymupdf
    pg = pymupdf.open(str(out_pdf))[0]
    sc = 1568.0 / max(pg.rect.width, pg.rect.height)
    pix = pg.get_pixmap(matrix=pymupdf.Matrix(sc, sc), annots=True)
    pix.save(str(P["out"] / "preview_L04.png"))
    ver["preview_size"] = [pix.width, pix.height]
    return {"manifest": manifest, "verify": ver, "pdf": str(out_pdf)}


def stage_report(ctx: dict) -> dict:
    P = ctx["P"]
    bg = _open(P["root"], BG_PDF, BG_PAGE)
    return rep.write_qa_report(ctx, P["qa"] / "pilot_room_measurement_L04.md", P["qa"] / "pilot_L04", bg)


def write_schedule(ctx: dict) -> None:
    P = ctx["P"]
    Z = ctx["zones"]
    rows = []
    zmap = {z.key: z for z in Z["zones"]}
    rm = {r["room_id"]: r for r in ctx["compare"]["rooms"]}
    for a in Z["assigned"]:
        z = zmap.get(a["zone_key"])
        src = ""
        if z:
            src = "; ".join(z.refs[:2]) + "; tag basis: " + z.tag_basis
        rows.append({"room": a["room_id"], "room_tag": a["tag"], "ahu_or_class": a["class_label"], "zone_kind": z.kind if z else "",
                     "zone_source": z.source if z else "", "area_m2": round(a["area_m2"], 3), "confidence": round(a["confidence"], 3),
                     "low_confidence": "yes" if (z and a["confidence"] < zn.CONFIDENCE_MIN) else "",
                     "other_overlaps": a["overlaps"], "sources": src or "no zone polygon covers the majority of the room"})
    common.write_csv_dicts(P["out"].parent.parent / "registers" / "zone_schedule_L04.csv", rows,
                           ["room", "room_tag", "ahu_or_class", "zone_kind", "zone_source", "area_m2", "confidence", "low_confidence", "other_overlaps", "sources"])


STAGES = ["calibrate", "vector", "raster", "compare", "zones", "pdf", "report"]


def run(stages: Sequence[str], work: Optional[Path] = None) -> dict:
    P = paths()
    work = Path(work) if work else P["work"]
    common.ensure_dir(work)
    ctx: dict = {"P": P, "tags": load_tags(P["reg"]), "codes": load_code_tags(P["reg"])}
    fn = {"calibrate": stage_calibrate, "vector": stage_vector, "raster": stage_raster, "compare": stage_compare,
          "zones": stage_zones, "pdf": stage_pdf, "report": stage_report}
    last = max(STAGES.index(s) for s in stages)
    for st in STAGES[:last + 1]:
        pk = work / ("%s.pkl" % st)
        if st in stages:
            t0 = time.time()
            ctx[st] = fn[st](ctx)
            with pk.open("wb") as fh:
                pickle.dump(ctx[st], fh)
            print("stage %s done in %.1f s" % (st, time.time() - t0), flush=True)
        elif pk.exists():
            with pk.open("rb") as fh:
                ctx[st] = pickle.load(fh)
        else:
            raise SystemExit("stage %s needs the cached result of stage %s (%s); run it first" % (stages[0], st, pk))
        if st == "calibrate":
            ctx["cal"] = ctx[st]
        if st == "zones" and "zones" in stages:
            write_schedule(ctx)
    return ctx


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stages", default=",".join(STAGES))
    ap.add_argument("--work", default=None)
    a = ap.parse_args(argv)
    run(a.stages.split(","), a.work)
    return 0


if __name__ == "__main__":
    sys.exit(main())
