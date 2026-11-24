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
import shapely.affinity
import shapely.geometry
from shapely import wkt
from shapely.geometry import MultiPoint, Polygon
from shapely.ops import voronoi_diagram

csv.field_size_limit(1 << 30)
ROOT = Path(__file__).resolve().parent.parent.parent
REG = ROOT / "out/registers"
APPC = "data/source/Basis/MOT NSC/Tender Corr 2/Doc/P3 SPECS/004 M&E (MOT) Specifications/Appendix - Particular Specs/"
PODS = [dict(storey="L06", region="L06-R250", file=APPC + "Appendix C - DD Booklet_DSOT_BATCH 1.pdf", page=8, title="IP 6TH STOREY KEYPLAN - PART 2",
             dept="Day Surgery Operating Theatre"),
        dict(storey="L05", region="L05-R058", file=APPC + "Appendix C - DD Booklet_MSOT_BATCH 1.pdf", page=7, title="IP 5TH STOREY KEYPLAN - PART 1",
             dept="Major Operating Theatre"),
        dict(storey="L05", region="L05-R261", file=APPC + "Appendix C - DD Booklet_MSOT_BATCH 1.pdf", page=8, title="IP 5TH STOREY KEYPLAN - PART 2",
             dept="Major Operating Theatre")]
PINK = np.array([249, 229, 234])
CYAN = np.array([179, 255, 255])
MAX_ZONE_PT2 = 8000.0      # 190 m2 at the booklet scale: a larger area is not one theatre zone


def _own(r):
    """The room outline without the open floor that gap_fill added to it."""
    g = shapely.make_valid(wkt.loads(r["polygon_pv_wkt"])).buffer(0)
    if r.get("gap_fill_pv_wkt"):
        g = g.difference(shapely.make_valid(wkt.loads(r["gap_fill_pv_wkt"])).buffer(0.4)).buffer(0)
    return g


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
    closed = [_own(r) for r in rows if r["polygon_pv_wkt"] and "-T" not in r["room_id"]]
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
    # D-096 rev: the zones are the room blocks of the booklet (pink or cyan areas between its red lines), each block whole to one unit, so the
    # zone lines follow the booklet room lines (no split across a room). A block that holds plan tags takes the unit of most of them; any other
    # block takes the unit of the nearest tag of the same kind (theatre unit for a pink block, support unit for a cyan block).
    cut = cv2.dilate(red, np.ones((3, 3), np.uint8)) > 0
    dark = cv2.dilate((img.max(axis=2) < 110).astype(np.uint8), np.ones((3, 3), np.uint8)) > 0   # room lines of the booklet plan
    tag_px = list(zip(xs, ys))
    pad = 60 * z
    xa, xb, ya, yb = max(min(xs) - pad, 0), max(xs) + pad, max(min(ys) - pad, 0), max(ys) + pad
    blocks = {}
    key = 0

    def add_blocks(mask, is_pink):
        nonlocal key
        nb, labb, statb, centb = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=4)
        for c_ in [int(c_) for c_ in np.unique(labb[ya:yb, xa:xb]) if c_ > 0 and statb[c_, cv2.CC_STAT_AREA] > 6 * z * z]:
            mk = (labb == c_)
            inside = [i for i, (x_, y_) in enumerate(tag_px) if 0 <= y_ < labb.shape[0] and 0 <= x_ < labb.shape[1]
                      and mk[max(y_ - 6, 0):y_ + 7, max(x_ - 6, 0):x_ + 7].any()]
            units_in = {cls[tags[i]["room_id"]] for i in inside if cls[tags[i]["room_id"]] in theatre_units}
            if is_pink and len(units_in) > 1 and mask is not None:
                sub = mk & ~dark                                   # several theatre units in one pink block: cut along the room lines
                add_sub(sub, inside)
                continue
            cs, _ = cv2.findContours(cv2.dilate(mk.astype(np.uint8), np.ones((5, 5), np.uint8)), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            g = shapely.union_all([Polygon(book_to_pv(cn[:, 0, :])).buffer(0) for cn in cs if len(cn) >= 3]).intersection(pod_space)
            if g.is_empty or g.area * k2 < 1.0:
                continue
            key += 1
            blocks[key] = dict(g=g, pink=is_pink, tags=inside, cx=centb[c_][0], cy=centb[c_][1], pool=None)

    def add_sub(sub, parent_tags):
        nonlocal key
        nb, labs, stats_, cents = cv2.connectedComponentsWithStats(sub.astype(np.uint8), connectivity=4)
        for c_ in range(1, nb):
            if stats_[c_, cv2.CC_STAT_AREA] < 4 * z * z:
                continue
            mk = (labs == c_)
            inside = [i for i in parent_tags if mk[max(tag_px[i][1] - 6, 0):tag_px[i][1] + 7, max(tag_px[i][0] - 6, 0):tag_px[i][0] + 7].any()]
            cs, _ = cv2.findContours(cv2.dilate(mk.astype(np.uint8), np.ones((7, 7), np.uint8)), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            g = shapely.union_all([Polygon(book_to_pv(cn[:, 0, :])).buffer(0) for cn in cs if len(cn) >= 3]).intersection(pod_space)
            if g.is_empty or g.area * k2 < 0.5:
                continue
            key += 1
            blocks[key] = dict(g=g, pink=True, tags=inside, cx=cents[c_][0], cy=cents[c_][1],
                               pool=[i for i in parent_tags if cls[tags[i]["room_id"]] in theatre_units])

    pk = cv2.morphologyEx(pinkf.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8)).astype(bool) & ~cut
    cy_ = cv2.morphologyEx(cyan.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8)).astype(bool) & ~cut & ~pk
    add_blocks(pk, True)
    add_blocks(cy_, False)
    is_theatre = {i: (cls[tags[i]["room_id"]] in theatre_units) for i in range(len(tags))}
    owner_tag = {}
    for c_, b in blocks.items():
        own = [i for i in b["tags"] if is_theatre[i] == b["pink"]] or b["tags"]
        if own:                                                  # the tag nearest the centre of the block owns it
            owner_tag[c_] = min(own, key=lambda i: (tag_px[i][0] - b["cx"]) ** 2 + (tag_px[i][1] - b["cy"]) ** 2)
        else:
            pool = b["pool"] or [i for i in range(len(tags)) if is_theatre[i] == b["pink"]] or list(range(len(tags)))
            owner_tag[c_] = min(pool, key=lambda i: (tag_px[i][0] - b["cx"]) ** 2 + (tag_px[i][1] - b["cy"]) ** 2)
    parts_of = {}
    for c_, i in owner_tag.items():
        parts_of.setdefault(i, []).append(blocks[c_]["g"])
    # pieces of the pod left by the booklet (red tag boxes, text, line gaps) go to the neighbouring part with the longest shared edge
    owned = {i: shapely.make_valid(shapely.union_all([shapely.make_valid(g) for g in v])).buffer(0) for i, v in parts_of.items()}
    pod_space = shapely.make_valid(pod_space).buffer(0)
    left = pod_space.difference(shapely.union_all(list(owned.values())).buffer(0.05)) if owned else pod_space
    for piece in getattr(left, "geoms", [left]):
        if piece.is_empty or piece.geom_type != "Polygon" or piece.area * k2 < 0.3:
            continue
        best_i, best_l = None, 0.0
        for i, g in owned.items():
            try:
                l_ = piece.buffer(0.15 * m).intersection(g.boundary).length
            except Exception:
                l_ = 0.0
            if l_ > best_l:
                best_i, best_l = i, l_
        if best_i is not None:
            parts_of[best_i].append(piece)
    # refine to the plan rooms: the faces of the plan linework (walls and thin panel lines, door arcs as closers) inside the pod; a face with a
    # plan tag takes that tag, any other face the tag whose booklet block holds its centre (D-096 rev 2)
    try:
        import pickle
        from .storeys import storey_paths
        from .rooms_vector import polygonise
        v_ = pickle.load(open(storey_paths(st)["work"] / "vector.pkl", "rb"))
        segs = [v_["vec"].wall_segments, v_["vec"].thin_segments] + ([v_["dash_bridges"]] if len(v_.get("dash_bridges", [])) else [])
        allseg = np.concatenate([q for q in segs if len(q)])
        bx = pod_space.buffer(1 * m).bounds
        keep_ = ((np.minimum(allseg[:, 0], allseg[:, 2]) <= bx[2]) & (np.maximum(allseg[:, 0], allseg[:, 2]) >= bx[0]) &
                 (np.minimum(allseg[:, 1], allseg[:, 3]) <= bx[3]) & (np.maximum(allseg[:, 1], allseg[:, 3]) >= bx[1]))
        bnd = np.array([[*a, *b] for g in [pod_space.buffer(0.02 * m)] for ring in [g.exterior] if hasattr(g, "exterior")
                        for a, b in zip(ring.coords[:-1], ring.coords[1:])]) if hasattr(pod_space.buffer(0.02 * m), "exterior") else np.zeros((0, 4))
        faces = [f.intersection(pod_space) for f in polygonise(np.concatenate([allseg[keep_], bnd]) if len(bnd) else allseg[keep_])]
        faces = [f for f in faces if not f.is_empty and f.area * k2 > 0.8]
        owned2 = {i: shapely.make_valid(shapely.union_all([shapely.make_valid(g) for g in v])).buffer(0) for i, v in parts_of.items()}
        new_parts: Dict[int, list] = {}
        for f in faces:
            rp = f.representative_point()
            inside = [i for i, c in enumerate(cen) if f.buffer(0.1 * m).contains(c)]
            if inside:
                i = min(inside, key=lambda i: cen[i].distance(f.centroid))
            else:
                hit_ = [i for i, g in owned2.items() if g.contains(rp)]
                if not hit_:
                    continue
                i = hit_[0]
            new_parts.setdefault(i, []).append(f)
        covered = shapely.union_all([g for v in new_parts.values() for g in v]) if new_parts else Polygon()
        if new_parts and covered.area > 0.6 * pod_space.area:
            for i, g in owned2.items():                        # the part of a booklet block that no plan face covers stays with its tag
                rest_i = g.difference(covered.buffer(0.05 * m))
                if not rest_i.is_empty and rest_i.area * k2 > 1.0:
                    new_parts.setdefault(i, []).append(rest_i)
            parts_of = new_parts
            refine_note = f"plan faces {len(faces)}"
        else:
            refine_note = "plan faces not used (they cover too little of the pod)"
    except Exception as e:                                    # the booklet blocks stay
        refine_note = f"plan faces not used ({type(e).__name__})"
    zones = {}
    for i, gs in parts_of.items():
        zones.setdefault(cls[tags[i]["room_id"]], []).extend(gs)
    zones = {u: shapely.union_all(v) for u, v in zones.items()}
    rest = pod_space.difference(shapely.union_all(list(zones.values()))) if zones else pod_space
    patched = 0
    for i, r in enumerate(tags):
        u = cls[r["room_id"]]
        if i in parts_of:
            part = shapely.union_all(parts_of[i])
        else:                                                    # a tag whose block went to another tag of its unit: its tag box only
            b = list(map(float, r["tag_box_pv"].split(",")))
            part = shapely.geometry.box(*b).intersection(zones.get(u, pod_space)) or shapely.geometry.box(*b)
            if part.is_empty:
                part = shapely.geometry.box(*b)
        if part.geom_type != "Polygon":
            ps = [g for g in getattr(part, "geoms", []) if g.geom_type == "Polygon"]
            part = shapely.geometry.MultiPolygon(ps) if len(ps) > 1 else (ps[0] if ps else part)
        part = part.simplify(0.3, preserve_topology=True)
        note = (f"approximate outline: room blocks of the MOT NSC DD Booklet zoning ({Path(pod['file']).name} p{pod['page']}, registered to the "
                f"grid, RMS {rms:.0f} mm), each block whole to one unit. Check on the drawing")
        r["polygon_pv_wkt"] = shapely.to_wkt(part, rounding_precision=2)
        r["polygon_building_mm_wkt"] = shapely.to_wkt(shapely.affinity.affine_transform(part, [M[0, 0], M[1, 0], M[0, 1], M[1, 1], M[2, 0], M[2, 1]]), rounding_precision=0)
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
            "support_m2": round(rest.area * k2, 1), "patched": patched, "tags": len(tags), "refine": refine_note}





# ---------------------------------------------------------------------------------------------------------------------------------------------
# D-096 rev 3: pods drawn by inspection. Rectangles in plan page-view pt, read from the tender plan room lines (theatre panels between the EA
# columns, anaes prep rooms, sterile prep) and given the unit of the MOT NSC DD Booklet zoning page. Editable markups; check on the drawing.
DRAWN = {
    "L06-R250": dict(
        source="tender plan room lines (theatre panel lines, EA columns) and DSOT DD Booklet p8 (MOT NSC Appendix C, Corr 2), drawn by inspection",
        pod=(640 + 160 / 2.083, 1300 + 62 / 2.083, 640 + 1165 / 2.083, 1300 + 560 / 2.083),     # pod 3 below the pod 2 wall
        units={
            "AHU-B-07-06DSOT-09": [(415, 137, 585, 335), (292, 137, 415, 210)],
            "AHU-B-07-06DSOT-10A/B": [(765, 137, 935, 335), (935, 137, 1060, 210)],
            # OR-11, OR-12 and the sterile hold share the south facade (Marcus, 24 Nov 2026): their zones run to the facade line, no support strip
            "AHU-B-07-06DSOT-11": [(415, 335, 585, 560), (292, 485, 415, 560)],
            "AHU-B-07-06DSOT-12A/B": [(765, 335, 935, 560), (935, 485, 1060, 560)],
            "AHU-B-07-06STER-03A/B": [(585, 287, 765, 560)],
        },
        frame=(640, 1300, 2.083),
        support="AHU-B-07-06DSOTSUPP-03A/B",
        above="AHU-B-07-06DSOTSUPP-02A/B",
        above_box=[(677, 1262, 1005, 1330), (677, 1110, 772, 1262)]),     # pod 2 south and west sides, to the PRE-OP wall          # pod 2 bottom row (bay exits, scrub) above the pod 3 wall: pod 2 support zone
    # L05 MOT pods 1 and 2 (support rings) and pod 3: the pod box minus every room that has its own closed outline (MSOT DD Booklet p7, p8)
    "L05-R058": dict(
        source="tender plan pod walls and MSOT DD Booklet p7 (MOT NSC Appendix C, Corr 2), drawn by inspection",
        frame=(0, 0, 1.0),
        units={"AHU-B-07-05MOTSUPP-01A/B": [(724, 400, 1005, 860)], "AHU-B-07-05MOTSUPP-02A/B": [(724, 860, 1005, 1345)]}),   # to the PACU / PRE-OP wall
    "L05-R261": dict(
        source="tender plan pod walls and MSOT DD Booklet p8 (MOT NSC Appendix C, Corr 2), drawn by inspection",
        frame=(0, 0, 1.0),
        units={"AHU-B-07-05MOTSUPP-03A/B": [(724, 1345, 1214, 1672)]}),   # to the core wall (east) and the south corridor
}


def run_drawn(region: str) -> Dict[str, object]:
    from .open_region import _affine, _to_mm
    from shapely.geometry import box as _box
    spec = DRAWN[region]
    st = region.split("-")[0]
    path = REG / f"room_polygons_{st}.csv"
    rows, cols = _rd(path)
    M = _affine(rows)
    k2 = float(np.median([float(r["a_area_m2"]) / wkt.loads(r["polygon_pv_wkt"]).area for r in rows
                          if r["polygon_pv_wkt"] and r["a_area_m2"] and "-T" not in r["room_id"]]))
    tags = [r for r in rows if r["room_id"].startswith(region + "-T") and r["tag_box_pv"]]
    zr, zc = _rd(REG / f"zone_assignment_{st}.csv")
    cls = {z_["room_id"]: z_["class"] for z_ in zr}
    closed = [_own(r) for r in rows if r["polygon_pv_wkt"] and "-T" not in r["room_id"]]
    others = shapely.union_all(closed).buffer(0.2)
    x0, y0, f = spec["frame"]
    pv = lambda b: _box(x0 + b[0] / f, y0 + b[1] / f, x0 + b[2] / f, y0 + b[3] / f)
    zones = {u: shapely.union_all([pv(b) for b in bs]).difference(others) for u, bs in spec["units"].items()}
    pod = _box(*spec["pod"]).difference(others) if spec.get("pod") else shapely.union_all(list(zones.values()))
    if spec.get("support"):
        zones[spec["support"]] = pod.difference(shapely.union_all(list(zones.values())))
    if spec.get("above_box"):
        zones[spec["above"]] = shapely.union_all([_box(*b) for b in spec["above_box"]]).difference(others).difference(pod)
    cen = {r["room_id"]: shapely.Point((lambda b: ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2))(list(map(float, r["tag_box_pv"].split(","))))) for r in tags}
    # the tags above the pod (pod 2 support) keep their earlier outline; each unit of the pod goes whole to the tag of that unit nearest its centre
    lead = {}
    for u, g in zones.items():
        mine = [r["room_id"] for r in tags if cls[r["room_id"]] == u]
        if mine:
            lead[u] = min(mine, key=lambda rid: cen[rid].distance(g.centroid))
    note_src = spec["source"]
    n = 0
    for r in tags:
        u = cls[r["room_id"]]
        if u not in zones:
            continue
        if lead.get(u) == r["room_id"]:
            part = zones[u]
        else:                                                    # the other tags of the unit: their tag box inside the zone
            b = list(map(float, r["tag_box_pv"].split(",")))
            part = _box(*b).intersection(zones[u])
            if part.is_empty:
                part = _box(*b)
        part = shapely.make_valid(part)
        r["polygon_pv_wkt"] = shapely.to_wkt(part, rounding_precision=2)
        r["polygon_building_mm_wkt"] = shapely.to_wkt(shapely.affinity.affine_transform(part, [M[0, 0], M[1, 0], M[0, 1], M[1, 1], M[2, 0], M[2, 1]]), rounding_precision=0)
        r["a_area_m2"] = f"{part.area * k2:.3f}"
        r["method_agreement"] = "drawn zone of an open region"
        note = f"outline drawn by inspection from the {note_src}. Check on the drawing"
        r["measurement_confidence"] = "medium: " + note
        r["flags"] = (r.get("flags") or "").split(" | approximate outline")[0].split(" | outline drawn")[0] + " | " + note
        n += 1
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    by = {r["room_id"]: r for r in tags}
    for z_ in zr:
        if z_["room_id"] in by and by[z_["room_id"]].get("method_agreement") == "drawn zone of an open region":
            z_["area_m2"] = by[z_["room_id"]]["a_area_m2"]
            z_["notes"] = (z_.get("notes") or "").split(" Outline approximate")[0].split(" Outline drawn")[0].strip() + f" Outline drawn by inspection from the {note_src}; check on the drawing."
    with (REG / f"zone_assignment_{st}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=zc)
        w.writeheader()
        w.writerows(zr)
    return {"region": region, "zones_m2": {u: round(g.area * k2, 1) for u, g in zones.items()}, "tags": n}


if __name__ == "__main__":
    for p in PODS:
        print(run_drawn(p["region"]) if p["region"] in DRAWN else run(p))
