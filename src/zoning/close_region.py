"""Room outlines inside an open region of the plan by wall closure (replaces the nearest-tag partition of D-071 where the walls allow it).

An open region is a plan area whose walls do not close (door openings, glazed screens): its room tags have no outline. Here the walls of the
plan (heavy wall strokes, door leaves and swing arcs as closers, from the vector stage of the storey) are widened by a half-gap h, which closes
every opening narrower than 2h. The free space that is left splits into faces; a face that holds tags is the room of those tags, grown back by
h so that it reaches the wall line. The half-gap is the smallest of 0.3, 0.45, 0.6, 0.75 and 0.9 m that gives each tag its own face, or else the
one that separates most tags. Tags that still share a face share it by nearest tag (the D-071 partition, inside that face only). A tag whose
face is lost (room narrower than 2h) keeps the D-071 part.

Every outline made here is approximate and says so in the register (method_agreement 'wall closure of an open region'). Check on the drawing.

``python3 -m src.zoning.close_region [--storey L06 --region L06-R250]``   (run after src.zoning.open_region; writes room_polygons_<STOREY>.csv)
"""
from __future__ import annotations

import argparse
import csv
import pickle
from pathlib import Path
from typing import Dict, List

import numpy as np
import shapely
from shapely import wkt
from shapely.geometry import Polygon, LineString, MultiPoint, box
from shapely.ops import voronoi_diagram

csv.field_size_limit(1 << 30)
ROOT = Path(__file__).resolve().parent.parent.parent
REG = ROOT / "out/registers"
HS_M = (0.3, 0.45, 0.6, 0.75, 0.9)


def _rd(p: Path):
    with p.open(newline="", encoding="utf-8") as fh:
        r = csv.DictReader(fh)
        return list(r), r.fieldnames


def run(storey: str, region: str) -> Dict[str, object]:
    from .open_region import _affine, _to_mm
    from .storeys import storey_paths
    path = REG / f"room_polygons_{storey}.csv"
    rows, cols = _rd(path)
    tags = [r for r in rows if r["room_id"].startswith(region + "-T") and r["tag_box_pv"]]
    if not tags:
        return {"patched": 0}
    k2 = float(np.median([float(r["a_area_m2"]) / wkt.loads(r["polygon_pv_wkt"]).area for r in rows
                          if r["polygon_pv_wkt"] and r["a_area_m2"] and "-T" not in r["room_id"]]))
    m = 1.0 / k2 ** 0.5                                   # 1 m in page-view pt
    boxes = [tuple(map(float, r["tag_box_pv"].split(","))) for r in tags]
    cen = [shapely.Point((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in boxes]
    closed = [wkt.loads(r["polygon_pv_wkt"]) for r in rows if r["polygon_pv_wkt"] and "-T" not in r["room_id"]]
    others = shapely.union_all(closed).buffer(0.2)
    plate = shapely.union_all(closed).buffer(3 * m).buffer(-3 * m)
    plate = shapely.union_all([Polygon(g.exterior) for g in getattr(plate, "geoms", [plate]) if g.geom_type == "Polygon"])   # an open region is a hole of the joined rooms: fill it
    old = {r["room_id"]: wkt.loads(r["polygon_pv_wkt"]) for r in tags if r["polygon_pv_wkt"]}
    win = MultiPoint(cen).envelope.buffer(12 * m)
    v = pickle.load(open(storey_paths(storey)["work"] / "vector.pkl", "rb"))
    seg = np.concatenate([v["vec"].wall_segments] + ([v["dash_bridges"]] if len(v.get("dash_bridges", [])) else []))
    lines = [LineString([(a, b), (c, d)]) for a, b, c, d in seg if box(*win.bounds).intersects(LineString([(a, b), (c, d)]))]
    space = win.intersection(plate).difference(others)
    best = None
    for h in HS_M:
        walls = shapely.union_all([g.buffer(h * m, cap_style="flat") for g in lines])
        free = space.difference(walls)
        faces = [g for g in getattr(free, "geoms", [free]) if g.geom_type == "Polygon" and g.area * k2 > 0.8]
        tag_face = []
        for c in cen:
            f = next((i for i, g in enumerate(faces) if g.contains(c)), None)
            if f is None:                                 # the tag text sits on a wall band: nearest face within 1 m
                d = [(g.distance(c), i) for i, g in enumerate(faces)]
                f = min(d)[1] if d and min(d)[0] < 1.0 * m else None
            tag_face.append(f)
        distinct = len({f for f in tag_face if f is not None})
        lost = sum(f is None for f in tag_face)
        score = (distinct - 2 * lost, -h)
        if best is None or score > best[0]:
            best = (score, h, faces, tag_face)
        if distinct == len(cen) and not lost:
            break
    _, h, faces, tag_face = best
    M = _affine(rows)
    patched, shared, kept = 0, 0, 0
    groups: Dict[int, List[int]] = {}
    for i, f in enumerate(tag_face):
        if f is not None:
            groups.setdefault(f, []).append(i)
    for f, idx in groups.items():
        face = faces[f].buffer(h * m).intersection(space)
        if len(idx) == 1:
            parts = {idx[0]: face}
        else:
            shared += len(idx)
            cells = voronoi_diagram(MultiPoint([cen[i] for i in idx]), envelope=face.buffer(5).envelope)
            parts = {i: next((g for g in cells.geoms if g.contains(cen[i])), face).intersection(face) for i in idx}
        for i, part in parts.items():
            if part.is_empty:
                continue
            if part.geom_type != "Polygon":
                part = max((g for g in part.geoms if g.geom_type == "Polygon"), key=lambda g: g.area)
            part = part.simplify(0.3, preserve_topology=True)
            r = tags[i]
            r["polygon_pv_wkt"] = shapely.to_wkt(part, rounding_precision=2)
            r["polygon_building_mm_wkt"] = shapely.to_wkt(_to_mm(part, M), rounding_precision=0)
            r["a_area_m2"] = f"{part.area * k2:.3f}"
            note = (f"approximate outline: wall closure of an open region (openings narrower than {2 * h:.1f} m closed"
                    + ("; the face is shared with other tags, split by nearest tag" if len(idx) > 1 else "") + "). Check on the drawing")
            r["method_agreement"] = "wall closure of an open region"
            r["measurement_confidence"] = "low: " + note
            r["flags"] = (r.get("flags") or "").split(" | approximate outline")[0] + " | " + note
            patched += 1
    kept = sum(f is None for f in tag_face)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    zp = REG / f"zone_assignment_{storey}.csv"
    zr, zc = _rd(zp)
    by = {r["room_id"]: r for r in tags}
    for z in zr:
        if z["room_id"] in by and by[z["room_id"]].get("method_agreement") == "wall closure of an open region":
            z["area_m2"] = by[z["room_id"]]["a_area_m2"]
            z["notes"] = (z.get("notes") or "").split(" Outline approximate")[0].strip() + \
                f" Outline approximate: wall closure of the open region {region} (openings under {2 * h:.1f} m closed); check on the drawing."
    with zp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=zc)
        w.writeheader()
        w.writerows(zr)
    return {"storey": storey, "region": region, "half_gap_m": h, "tags": len(tags), "own_face": patched - shared, "shared_face": shared,
            "kept_partition": kept}


def run_all() -> List[Dict[str, object]]:
    out = []
    for st in ["LB3", "B3", "B2", "B1", "L01", "L02", "L03", "L04", "L05", "L06", "L07"]:
        rows, _ = _rd(REG / f"room_polygons_{st}.csv")
        for rg in sorted({r["room_id"].rsplit("-T", 1)[0] for r in rows if "-T" in r["room_id"] and r["tag_box_pv"]
                          and ("nearest-tag partition" in (r.get("method_agreement") or "") or "wall closure" in (r.get("method_agreement") or ""))}):
            out.append(run(st, rg))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--storey")
    ap.add_argument("--region")
    a = ap.parse_args()
    for x in ([run(a.storey, a.region)] if a.storey else run_all()):
        print(x)
