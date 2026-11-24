"""Room outlines of an operating-theatre pod from the MOT NSC DD Booklet zoning page (24 Nov 2026; replaces the nearest-tag partition of D-071 there).

The pod is an open region of the plan (modular theatre panels drawn with thin, broken lines): its tags have no closed outline. The MOT NSC
Appendix C DD Booklet zoning page draws each theatre AHU zone as a block of the HEPA colour (pink) and the pod support zone in the
recirculation colour (cyan). The page is registered to the building grid by src.zoning.booklet_zones (similarity fit, residual in mm), so:

1. the pink blocks of the page become polygons in building mm and then in plan page view;
2. a pink block that holds a tag of a theatre unit is the zone of that unit (its tags share it by nearest tag);
3. the rest of the pod (cyan) is the zone of the support unit, shared by its tags by nearest tag;
4. everything is cut to the floor plate and never covers a room that has its own closed outline.

The class of every tag is not changed (it is the zoning class). The outline is approximate and the register says so.

``python3 -m src.zoning.booklet_pod``  (L06 pod 3: DSOT booklet page 8)
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, List

import numpy as np
import shapely
from shapely import wkt
from shapely.geometry import MultiPoint, Polygon
from shapely.ops import voronoi_diagram

csv.field_size_limit(1 << 30)
ROOT = Path(__file__).resolve().parent.parent.parent
REG = ROOT / "out/registers"
APPC = "data/source/Basis/MOT NSC/Tender Corr 2/Doc/P3 SPECS/004 M&E (MOT) Specifications/Appendix - Particular Specs/"
PODS = [dict(storey="L06", region="L06-R250", file=APPC + "Appendix C - DD Booklet_DSOT_BATCH 1.pdf", page=8, title="IP 6TH STOREY KEYPLAN - PART 2",
             dept="Day Surgery Operating Theatre")]
PINK = np.array([249, 229, 234])
CYAN = np.array([179, 255, 255])
MAX_ZONE_PT2 = 8000.0      # 190 m2 at the booklet scale: a larger area is not one theatre zone


def _rd(p: Path):
    with p.open(newline="", encoding="utf-8") as fh:
        r = csv.DictReader(fh)
        return list(r), r.fieldnames


def run(pod: dict) -> Dict[str, object]:
    import cv2
    import pymupdf
    from . import booklet_zones as bz
    from .open_region import _affine, _to_mm
    st, region = pod["storey"], pod["region"]
    info = dict(file=pod["file"], page=pod["page"], title=pod["title"], key=st, department=pod["dept"], booklet_page="",
                legend_in_register="", legend_source="", legend_bbox="")
    res = bz.process_page(ROOT, info, bz.Tender(st, ROOT), None)
    if not res.get("reg_ok"):
        return {"region": region, "status": "booklet page not registered"}
    T = np.vstack([res["pt2mm"], [0, 0, 1]])                     # booklet page-view pt -> building mm
    rms = res["reg"].get("rms_mm")
    page = pymupdf.open(str(ROOT / pod["file"]))[pod["page"] - 1]
    z = 4
    pix = page.get_pixmap(matrix=pymupdf.Matrix(z, z))
    img = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3].astype(int)
    # theatre zones: the HEPA pink fill, closed over text and equipment lines (9 px = 0.35 m), then cut along the red zone lines
    red = ((img[:, :, 0] > 150) & (img[:, :, 1] < 120) & (img[:, :, 2] < 120)).astype(np.uint8)
    is_pink = (np.abs(img - PINK).max(axis=2) <= 14).astype(np.uint8)
    pink = cv2.morphologyEx(is_pink, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    pink[cv2.dilate(red, np.ones((3, 3), np.uint8)) > 0] = 0
    path = REG / f"room_polygons_{st}.csv"
    rows, cols = _rd(path)
    M = _affine(rows)                                             # plan page view -> mm
    Mi = np.linalg.inv(np.vstack([M.T, [0, 0, 1]]))               # mm -> plan page view
    k2 = float(np.median([float(r["a_area_m2"]) / wkt.loads(r["polygon_pv_wkt"]).area for r in rows
                          if r["polygon_pv_wkt"] and r["a_area_m2"] and "-T" not in r["room_id"]]))

    def book_to_pv(xy_px):
        pts = np.hstack([np.asarray(xy_px, float) / z, np.ones((len(xy_px), 1))]) @ T.T
        return (pts @ Mi.T)[:, :2]

    n, lab, stats, _ = cv2.connectedComponentsWithStats(pink, connectivity=4)
    pink_share = np.ones(n)
    tags = [r for r in rows if r["room_id"].startswith(region + "-T") and r["tag_box_pv"]]
    zr, zc = _rd(REG / f"zone_assignment_{st}.csv")
    cls = {z_["room_id"]: z_["class"] for z_ in zr}
    cen = []
    for r in tags:
        b = list(map(float, r["tag_box_pv"].split(",")))
        cen.append(shapely.Point((b[0] + b[2]) / 2, (b[1] + b[3]) / 2))
    Tinv = np.linalg.inv(T)
    # pink blocks (rooms of the booklet, cut by the red lines): seed each block that holds a plan tag with the unit of the tag, then let a unit
    # take the pink blocks next to it that hold no tag of another unit, until nothing changes (the whole HEPA zone of the theatre)
    ys, xs = [], []
    seeds: Dict[int, str] = {}
    seed_tags: Dict[int, List[int]] = {}
    for r, c in zip(tags, cen):
        mm = np.array([c.x, c.y, 1.0]) @ M
        bp = Tinv @ np.array([mm[0], mm[1], 1.0])
        px, py = int(round(bp[0] * z)), int(round(bp[1] * z))
        xs.append(px)
        ys.append(py)
        for rad in (0, 3, 6, 10):
            win = lab[max(py - rad, 0):py + rad + 1, max(px - rad, 0):px + rad + 1]
            ok = [int(c_) for c_ in np.unique(win) if c_ > 0 and stats[c_, cv2.CC_STAT_AREA] > 30 * z * z]
            if ok:
                cid = max(ok, key=lambda c_: stats[c_, cv2.CC_STAT_AREA])
                seed_tags.setdefault(cid, []).append(len(xs) - 1)
                if cid not in seeds:
                    seeds[cid] = cls[r["room_id"]]
                break
    # only the theatre units of the pod take pink blocks (a support-unit tag inside pink keeps the support zone)
    theatre_units = {cls[tags[i]["room_id"]] for lst in seed_tags.values() for i in lst} - {u for u in seeds.values() if "SUPP" in u}
    theatre_units = {u for u in theatre_units if "SUPP" not in u}
    seeds = {c_: u for c_, u in seeds.items() if u in theatre_units}
    pad = 60 * z
    xa, xb, ya, yb = max(min(xs) - pad, 0), max(xs) + pad, max(min(ys) - pad, 0), max(ys) + pad
    sub = lab[ya:yb, xa:xb]
    comps = [int(c_) for c_ in np.unique(sub) if c_ > 0 and stats[c_, cv2.CC_STAT_AREA] > 8 * z * z]
    near: Dict[int, set] = {c_: set() for c_ in comps}
    for c_ in comps:
        mk = cv2.dilate((sub == c_).astype(np.uint8), np.ones((9, 9), np.uint8))
        near[c_] = {int(x) for x in np.unique(sub[mk > 0]) if x > 0 and x != c_ and int(x) in near}
    owner = dict(seeds)
    changed = True
    while changed:
        changed = False
        for c_ in comps:
            if c_ in owner:
                continue
            us = {owner[x] for x in near[c_] if x in owner}
            if len(us) == 1:
                owner[c_] = us.pop()
                changed = True
    unit_comp: Dict[str, set] = {}
    for c_, u in owner.items():
        unit_comp.setdefault(u, set()).add(c_)
    closed = [wkt.loads(r["polygon_pv_wkt"]) for r in rows if r["polygon_pv_wkt"] and "-T" not in r["room_id"]]
    others = shapely.union_all(closed).buffer(0.2)
    m = 1.0 / k2 ** 0.5
    plate = shapely.union_all(closed).buffer(3 * m).buffer(-3 * m)
    plate = shapely.union_all([Polygon(g.exterior) for g in getattr(plate, "geoms", [plate]) if g.geom_type == "Polygon"])   # an open region is a hole of the joined rooms: fill it
    # the pod outline is the coloured fill of the booklet (recirculation cyan or HEPA pink) that holds the tags of the region: the plan has no
    # closed outline for it (the pod is open to the terrace side, so the joined plan rooms leave a notch, not a hole)
    cyan = np.abs(img - CYAN).max(axis=2) <= 18
    pinkf = np.abs(img - PINK).max(axis=2) <= 14
    col = cv2.morphologyEx((cyan | pinkf).astype(np.uint8), cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    nc, labc = cv2.connectedComponents(col, connectivity=4)
    hit = set()
    for x_, y_ in zip(xs, ys):
        win = labc[max(y_ - 12, 0):y_ + 13, max(x_ - 12, 0):x_ + 13]
        hit |= {int(c_) for c_ in np.unique(win) if c_ > 0}
    outline = []
    for c_ in hit:
        cs, _ = cv2.findContours((labc == c_).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        outline += [Polygon(book_to_pv(cn[:, 0, :])).buffer(0) for cn in cs if len(cn) >= 3]
    pod_outline = shapely.union_all(outline).intersection(MultiPoint(cen).envelope.buffer(4 * m)) if outline else MultiPoint(cen).envelope.buffer(1 * m)
    pod_space = pod_outline.difference(others)
    zones: Dict[str, Polygon] = {}
    unit_polys: Dict[str, list] = {}
    for u, cids in unit_comp.items():
        for cid in cids:
            mask = cv2.dilate((lab == cid).astype(np.uint8), np.ones((5, 5), np.uint8))   # back over the cut red line
            cs, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            blk = shapely.union_all([Polygon(book_to_pv(cn[:, 0, :])).buffer(0) for cn in cs if len(cn) >= 3])
            units_in = sorted({cls[tags[i]["room_id"]] for i in seed_tags.get(cid, []) if cls[tags[i]["room_id"]] in theatre_units})
            if len(units_in) <= 1:
                unit_polys.setdefault(u, []).append(blk)
                continue
            # one pink block holds tags of several theatre units (theatres and the sterile hold between them, no red line drawn):
            # split by nearest tag, the parts of the tags of one unit together
            idx = [i for i in seed_tags[cid] if cls[tags[i]["room_id"]] in theatre_units]
            cells = voronoi_diagram(MultiPoint([cen[i] for i in idx]), envelope=blk.buffer(10).envelope)
            for i in idx:
                cell = next((g for g in cells.geoms if g.contains(cen[i])), None)
                if cell is not None:
                    unit_polys.setdefault(cls[tags[i]["room_id"]], []).append(cell.intersection(blk))
    for u, polys in unit_polys.items():
        if polys:
            raw = shapely.union_all(polys)
            zones[u] = raw.buffer(0.3 * m).intersection(pod_space)
    theatre = shapely.union_all(list(zones.values())) if zones else Polygon()
    rest = pod_space.difference(theatre)
    by_unit: Dict[str, List[int]] = {}
    for i, r in enumerate(tags):
        u = cls[r["room_id"]]
        by_unit.setdefault(u if u in zones else "_support", []).append(i)   # the support units share the rest of the pod once, by nearest tag
    patched = 0
    for u, idx in by_unit.items():
        zone = zones.get(u, rest)
        if zone.is_empty:
            continue
        if len(idx) == 1:
            parts = {idx[0]: zone}
        else:
            cells = voronoi_diagram(MultiPoint([cen[i] for i in idx]), envelope=zone.buffer(5).envelope)
            parts = {i: next((g for g in cells.geoms if g.contains(cen[i])), zone).intersection(zone) for i in idx}
        for i, part in parts.items():
            if part.is_empty:
                continue
            if part.geom_type != "Polygon":
                part = max((g for g in part.geoms if g.geom_type == "Polygon"), key=lambda g: g.area)
            part = part.simplify(0.3, preserve_topology=True)
            r = tags[i]
            note = (f"approximate outline: {'theatre zone' if u in zones else 'pod support zone'} of the MOT NSC DD Booklet page "
                    f"({Path(pod['file']).name} p{pod['page']}, registered to the grid, RMS {rms:.0f} mm)"
                    + ("; shared by nearest tag" if len(idx) > 1 else "") + ". Check on the drawing")
            r["polygon_pv_wkt"] = shapely.to_wkt(part, rounding_precision=2)
            r["polygon_building_mm_wkt"] = shapely.to_wkt(_to_mm(part, M), rounding_precision=0)
            r["a_area_m2"] = f"{part.area * k2:.3f}"
            r["method_agreement"] = "booklet zone of an open region"
            r["measurement_confidence"] = "low: " + note
            r["flags"] = (r.get("flags") or "").split(" | approximate outline")[0] + " | " + note
            patched += 1
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    by = {r["room_id"]: r for r in tags}
    for z_ in zr:
        if z_["room_id"] in by:
            z_["area_m2"] = by[z_["room_id"]]["a_area_m2"]
            z_["notes"] = (z_.get("notes") or "").split(" Outline approximate")[0].strip() + \
                f" Outline approximate: zone of the MOT NSC DD Booklet {Path(pod['file']).name} p{pod['page']} registered to the plan; check on the drawing."
    with (REG / f"zone_assignment_{st}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=zc)
        w.writeheader()
        w.writerows(zr)
    return {"region": region, "rms_mm": rms, "theatre_units": {u: round(g.area * k2, 1) for u, g in zones.items()},
            "support_m2": round(rest.area * k2, 1), "patched": patched, "tags": len(tags)}


if __name__ == "__main__":
    for p in PODS:
        print(run(p))
