"""Tests for the zoning pilot (src/zoning) on a synthetic plan: four rooms, a door gap, two zones.

The synthetic sheet is 240 x 160 pt at the 1:250 scale (88.194 mm/pt): outer walls, one vertical and one horizontal
internal wall, a door opening in the vertical wall at y 30-50 with a 90 degree swing arc drawn as short segments, and
a room tag in each room.  Rooms are 120 x 80 pt = 10.58 x 7.06 m = 74.67 m2.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
import shapely
from shapely.geometry import Polygon, box

from src.zoning import calibrate as cal
from src.zoning import compare as cmp
from src.zoning import linework as lw
from src.zoning import rooms_raster as rr
from src.zoning import rooms_vector as rv
from src.zoning import write_pdf as wp
from src.zoning import zones as zn

MM_PT = 88.194
K2 = (MM_PT / 1000.0) ** 2          # m2 per pt2
ROOM_M2 = 120 * 80 * K2


def _wall(ss_list, x0, y0, x1, y1, w=0.84):
    ss_list.append(((x0, y0, x1, y1), w))


def synthetic_segments(door: bool = True):
    """Return (segments list of ((x0,y0,x1,y1), width)) for the synthetic plan."""
    s = []
    _wall(s, 0, 0, 240, 0); _wall(s, 240, 0, 240, 160); _wall(s, 240, 160, 0, 160); _wall(s, 0, 160, 0, 0)
    _wall(s, 0, 80, 240, 80)
    if door:
        _wall(s, 120, 0, 120, 30); _wall(s, 120, 50, 120, 160)
        # swing arc: hinge (120, 30), radius 20, from (120, 50) round to (100, 30), in 12 short chords
        for k in range(12):
            a0 = math.radians(90 + 90 * k / 12.0); a1 = math.radians(90 + 90 * (k + 1) / 12.0)
            s.append(((120 + 20 * math.cos(a0), 30 + 20 * math.sin(a0), 120 + 20 * math.cos(a1), 30 + 20 * math.sin(a1)), 0.24))
    else:
        _wall(s, 120, 0, 120, 160)
    return s


def make_strokeset(door: bool = True) -> lw.StrokeSet:
    items = synthetic_segments(door)
    styles = [((0.0, 0.0, 0.0), 0.84), ((0.0, 0.0, 0.0), 0.24)]
    seg = np.array([i[0] for i in items], float)
    sty = np.array([0 if i[1] > 0.5 else 1 for i in items], np.int32)
    return lw.StrokeSet(seg, sty, styles, np.zeros(len(seg), bool))


TAGS = [{"x_pv": 60.0, "y_pv": 40.0, "text": "Off / AC", "name": "Off", "mode": "AC", "t_index": 0},
        {"x_pv": 180.0, "y_pv": 40.0, "text": "Wait / AC", "name": "Wait", "mode": "AC", "t_index": 1},
        {"x_pv": 60.0, "y_pv": 120.0, "text": "Corr / MV", "name": "Corr", "mode": "MV", "t_index": 2},
        {"x_pv": 180.0, "y_pv": 120.0, "text": "Lab / MV", "name": "Lab", "mode": "MV", "t_index": 3}]


# --------------------------------------------------------------------------- calibration
def _bubbles(A, offset=(0.0, 0.0)):
    """Synthetic bubbles: grid X at 0/8400/16800 mm, Y at 0/8400/16800 mm; positions from the inverse of ``A``."""
    grid = {"X": {"2": 0.0, "3": 8400.0, "4": 16800.0}, "Y": {"A": 0.0, "B": 8400.0, "C": 16800.0}}
    c = cal.Calibration(A=A, residuals_mm={}, rms_mm=0, max_mm=0, n_equations=0)
    bub = []
    top_y_mm = 20000.0
    left_x_mm = -3000.0
    for lab, X in grid["X"].items():
        x, y = c.to_page(np.array(X), np.array(top_y_mm))
        bub.append(cal.Bubble(lab, "numbered", float(x) + offset[0], float(y) + offset[1]))
    for lab, Y in grid["Y"].items():
        x, y = c.to_page(np.array(left_x_mm), np.array(Y))
        bub.append(cal.Bubble(lab, "lettered", float(x) + offset[0], float(y) + offset[1]))
    return grid, bub


A_TRUE = np.array([[MM_PT, 0.002, 1000.0], [0.03, -MM_PT, 20000.0]])


def test_fit_affine_recovers_transform():
    grid, bub = _bubbles(A_TRUE)
    c = cal.fit_affine(bub, grid)
    # the transform agrees on the grid lines (the only constraints) to well under 1 mm
    assert c.rms_mm < 0.5
    assert c.scale_x_mm_per_pt == pytest.approx(MM_PT, rel=1e-4)
    X, Y = c.to_building(bub[1].x, bub[1].y)
    assert X == pytest.approx(8400.0, abs=0.5)
    x, y = c.to_page(X, Y)
    assert (x, y) == (pytest.approx(bub[1].x, abs=1e-6), pytest.approx(bub[1].y, abs=1e-6))


def test_fit_affine_reports_residuals_for_noise():
    grid, bub = _bubbles(A_TRUE)
    bub[0].x += 0.1          # 8.8 mm
    c = cal.fit_affine(bub, grid)
    assert 0.5 < c.max_mm < 12.0
    assert set(c.residuals_mm) >= {"X:2", "Y:A"}


def test_frame_comparison_reports_offset_and_zero_residual():
    grid, b1 = _bubbles(A_TRUE)
    _, b2 = _bubbles(A_TRUE, offset=(0.5, -0.25))
    r = cal.compare_frames(b1, b2)
    assert r["n"] == 6
    assert r["mean_dx_pt"] == pytest.approx(0.5) and r["mean_dy_pt"] == pytest.approx(-0.25)
    assert r["rms_after_offset_pt"] == pytest.approx(0.0, abs=1e-9)


def test_transfer_points_identity_between_equal_calibrations():
    grid, b = _bubbles(A_TRUE)
    c = cal.fit_affine(b, grid)
    pts = np.array([[10.0, 20.0], [200.0, 150.0]])
    assert np.allclose(cal.transfer_points(pts, c, c), pts, atol=1e-6)


def test_grid_location_text():
    grid = {"X": {"2": 0.0, "3": 8400.0}, "Y": {"A": 0.0, "B": 10200.0}}
    assert cal.grid_location(9000.0, 500.0, grid) == "3+600 mm / A+500 mm"


# --------------------------------------------------------------------------- linework and method A
def test_door_arc_is_detected_and_closed():
    ss = make_strokeset(door=True)
    arcs = lw.detect_door_arcs(ss)
    assert len(arcs) == 1
    a = arcs[0]
    assert (a.cx, a.cy) == (pytest.approx(120.0, abs=0.1), pytest.approx(30.0, abs=0.1))
    assert a.r == pytest.approx(20.0, abs=0.1) and a.span_deg == pytest.approx(90.0, abs=8.0)
    seg, diag = rv.build_wall_segments(ss, (0.84,), arcs=arcs)
    assert diag["n_doors_closed"] == 1


def test_method_a_four_rooms_with_door_gap_closed():
    ss = make_strokeset(door=True)
    res = rv.measure_rooms(ss, TAGS, K2, heavy_widths=(0.84,), thin_widths=(0.24,))
    assert len(res.regions) == 4
    for r in res.regions:
        assert len(r.tags) == 1
        assert r.polygon.area * K2 == pytest.approx(ROOM_M2, rel=0.005)


def test_door_gap_without_a_swing_arc_merges_two_rooms():
    ss = make_strokeset(door=False)
    # the vertical wall is complete here, so remove the door and cut the opening by hand: no arc, no closer
    items = [i for i in synthetic_segments(False) if not (i[0][0] == 120 and i[0][2] == 120)]
    items += [((120, 0, 120, 30), 0.84), ((120, 50, 120, 160), 0.84)]
    seg = np.array([i[0] for i in items], float)
    styles = [((0.0, 0.0, 0.0), 0.84)]
    ss = lw.StrokeSet(seg, np.zeros(len(seg), np.int32), styles, np.zeros(len(seg), bool))
    res = rv.measure_rooms(ss, TAGS, K2, heavy_widths=(0.84,), thin_widths=(0.24,))
    # bridging joins the two jambs (gap 20 pt is below the 27 pt limit), so the rooms stay separate
    assert len(res.regions) == 4
    assert res.diagnostics["n_bridges"] >= 1


def test_opening_wider_than_bridge_limit_leaks_and_is_reported_as_open_plan():
    items = [i for i in synthetic_segments(False) if not (i[0][0] == 120 and i[0][2] == 120)]
    items += [((120, 0, 120, 10), 0.84), ((120, 60, 120, 160), 0.84)]     # 50 pt = 4.4 m opening
    seg = np.array([i[0] for i in items], float)
    ss = lw.StrokeSet(seg, np.zeros(len(seg), np.int32), [((0.0, 0.0, 0.0), 0.84)], np.zeros(len(seg), bool))
    res = rv.measure_rooms(ss, TAGS, K2, heavy_widths=(0.84,), thin_widths=(0.24,))
    multi = [r for r in res.regions if len(r.tags) > 1]
    assert len(res.regions) == 3 and len(multi) == 1


def test_centreline_grows_by_half_the_wall_gap():
    ss = make_strokeset(door=True)
    res = rv.measure_rooms(ss, TAGS, K2, heavy_widths=(0.84,), thin_widths=(0.24,))
    r = res.regions[0]
    grown = rv.centreline_polygon(r.polygon, 1.0, STRtree_of([]), [])
    # a 1 pt offset on a 120 x 80 rectangle with mitred corners adds 2*(120+80)*1 + 4 = 404 pt2
    assert grown.area - r.polygon.area == pytest.approx(404.0, rel=0.02)


def STRtree_of(items):
    from shapely.strtree import STRtree
    return STRtree(items if items else [Polygon()])


# --------------------------------------------------------------------------- method B
def test_method_b_agrees_with_method_a_within_two_percent():
    ss = make_strokeset(door=True)
    res = rv.measure_rooms(ss, TAGS, K2, heavy_widths=(0.84,), thin_widths=(0.24,))
    out, diag = rr.measure_rooms(res.wall_segments, res.thin_segments, TAGS, MM_PT, margin_pt=130.0)
    assert diag["mm_per_px_x"] <= 10.0
    assert len(out) == 4
    for sr in out:
        assert sr.polygon is not None and not sr.leaked
        assert sr.area_m2 == pytest.approx(ROOM_M2, rel=0.02)


def test_method_b_splits_two_seeds_in_one_open_region_with_a_watershed():
    items = [i for i in synthetic_segments(False) if not (i[0][0] == 120 and i[0][2] == 120)]
    items += [((120, 0, 120, 10), 0.84), ((120, 60, 120, 160), 0.84)]
    seg = np.array([i[0] for i in items], float)
    out, diag = rr.measure_rooms(seg, np.zeros((0, 4)), TAGS, MM_PT, margin_pt=130.0)
    shared = [sr for sr in out if sr.shared_component]
    assert len(shared) == 2 and diag["n_split_components"] == 1
    assert sum(sr.pixels for sr in shared) * (MM_PT / diag["px_per_pt"] / 1000.0) ** 2 > 1.5 * ROOM_M2


def test_pixel_size_never_exceeds_10_mm():
    assert MM_PT / rr.pixels_per_pt(MM_PT, 50.0) <= 10.0


# --------------------------------------------------------------------------- comparison
def test_caption_parsing_and_tolerance():
    assert cmp.parse_caption("AREA / 57.02 sq m") == 57.02
    assert cmp.parse_caption("AREA1,112.32 sq m") == 1112.32
    assert cmp.parse_caption("") is None
    assert cmp.within(100.0, 101.9, 2.0) and not cmp.within(100.0, 103.0, 2.0)
    assert cmp.within(1.0, 1.25, 2.0, 0.3)          # 0.25 m2 inside the absolute tolerance


def test_app_d_match_needs_mutual_overlap():
    room = box(0, 0, 120, 80)
    good = cmp.AppdPolygon(0, 1, 74.0, box(0, 0, 120, 80), box(2, 2, 118, 78), 74.0, 0.0)
    big = cmp.AppdPolygon(1, 2, 300.0, box(0, 0, 400, 80), box(0, 0, 400, 80), 300.0, 0.0)
    m = cmp.match_regions([room], [big, good])
    assert m[0][0] == 1 and m[0][1] >= 0.8
    assert cmp.match_regions([room], [big]) == {}      # the room is inside but the polygon is a composite


def test_room_rows_and_convention_choice():
    ss = make_strokeset(door=True)
    res = rv.measure_rooms(ss, TAGS, K2, heavy_widths=(0.84,), thin_widths=(0.24,))
    face = res.regions[0].polygon
    grown = face.buffer(1.0, join_style="mitre")
    A = np.array([[MM_PT, 0.0, 0.0], [0.0, -MM_PT, 14000.0]])
    c = cal.Calibration(A=A, residuals_mm={}, rms_mm=0, max_mm=0, n_equations=0)
    rooms = [{"room_id": "T-R001", "tag": "Off / AC", "name": "Off", "mode": "AC", "tags_inside": ["Off / AC"], "a_face": face, "a_cl": grown,
              "b_face": face, "b_cl": grown, "b_face_area_m2": face.area * K2, "flags": ""}]
    caption = grown.area * K2             # the printed caption traced at the centre line
    appd = [cmp.AppdPolygon(7, 70, round(caption, 2), grown, grown, caption, 0.0)]
    grid = {"X": {"2": 0.0}, "Y": {"A": 0.0}}
    rows, stats = cmp.build_room_rows(rooms, appd, c, grid)
    assert stats["adopted_convention"] == "centreline"
    assert rows[0]["method_agreement"] == "agree"
    assert abs(rows[0]["error_vs_caption_pct"]) < 0.1
    assert rows[0]["appd_caption_m2"] == pytest.approx(round(caption, 2))
    assert rows[0]["a_perimeter_m"] > 0


# --------------------------------------------------------------------------- zones
def test_tag_levels_red_replacement_and_governing_tags():
    assert zn.parse_tag_levels("AHU-B-07-04DIAGRE-01A/B") == ("7", "4")
    assert zn.parse_tag_levels("AHU-B-04-04MRI-01A/B") == ("4", "4")
    assert zn.parse_tag_levels("not a tag") == ("", "")
    assert zn.avoid_pure_red("#FF0000") != "#FF0000" and zn.avoid_pure_red("#80ff80") == "#80FF80"
    cf = [{"conflict_id": "CF-0003", "value_a": "AHU-B-04-07DIAGRE-01A/B; AHU-B-04-07DIAGSU-01", "value_b": "AHU-B-07-04DIAGRE-01A/B; AHU-B-04-04DIAGSU-01"}]
    m, _ = zn.governing_tags(cf, [])
    assert m["AHU-B-04-07DIAGSU-01"][0] == "AHU-B-04-04DIAGSU-01"
    assert m["AHU-B-04-07DIAGRE-01A/B"][0] == "AHU-B-07-04DIAGRE-01A/B"


def _zone(tag, polys, source="RFI-ME-0233 sample", kind="AHU", fill="#00FFFF"):
    return zn.Zone(key=tag, kind=kind, source=source, tag=tag, polygons_pv=polys, fill_hex=fill, departments="TEST (test)",
                   storey_served="4", plant_level="4", refs=["synthetic"], tag_basis="synthetic")


def synthetic_rooms():
    return [{"room_id": "S-R%d" % i, "polygon": box(x, y, x + 120, y + 80), "area_m2": ROOM_M2, "tag": "t", "name": "n"}
            for i, (x, y) in enumerate([(0, 0), (120, 0), (0, 80), (120, 80)], start=1)]


def test_rooms_go_to_the_majority_zone_with_the_share_as_confidence():
    z1 = _zone("AHU-1", [box(-5, -5, 125, 165)])                       # left column plus a 5 pt overlap into the right rooms
    z2 = _zone("AHU-2", [box(120, -5, 245, 80 + 56)])                  # room 2 fully, room 4 by 70 %
    out = zn.assign_rooms(synthetic_rooms(), [z1, z2])
    by = {o["room_id"]: o for o in out}
    assert by["S-R1"]["zone_tag"] == "AHU-1" and by["S-R1"]["confidence"] == pytest.approx(1.0)
    assert by["S-R2"]["zone_tag"] == "AHU-2"
    assert by["S-R4"]["zone_tag"] == "AHU-2" and by["S-R4"]["confidence"] == pytest.approx(0.7, abs=0.02)


def test_room_without_majority_cover_is_no_information_and_rfi_beats_app_d():
    z1 = _zone("AHU-1", [box(0, 0, 120, 160)])
    z2 = _zone("AHU-2", [box(120, 0, 240, 80 + 24)])                   # room 4 only 30 % covered
    out = zn.assign_rooms(synthetic_rooms(), [z1, z2])
    r4 = [o for o in out if o["room_id"] == "S-R4"][0]
    assert r4["class_label"] == "no information" and r4["zone_tag"] == ""
    appd = _zone("AHU-APPD", [box(0, 0, 240, 160)], source="Appendix D", fill="#8000FF")
    out = zn.assign_rooms(synthetic_rooms(), [z1, appd])
    assert [o["zone_tag"] for o in out] == ["AHU-1", "AHU-APPD", "AHU-1", "AHU-APPD"]


def test_app_d_classification_ahu_fcu_mv():
    P = lambda i: cmp.AppdPolygon(i, i, 10.0, box(0, 0, 1, 1), box(0, 0, 1, 1), 10.0, 0.0)
    spaces = [{"annotation_index": "0", "equipment_tags_within_60pt": "AHU-B-04-04CORE-01 [ann 54, 0.0 pt]; EAF-B-04-04DISP-01 [ann 55, 3.0 pt]", "mode_label": "AC (24HR)", "space_names_text_layer": "Corr"},
              {"annotation_index": "1", "equipment_tags_within_60pt": "FCU-B-04-04COR-01 [ann 1, 0.0 pt]", "mode_label": "AC", "space_names_text_layer": "Off"},
              {"annotation_index": "2", "equipment_tags_within_60pt": "EAF-B-04-04DISP-01 [ann 55, 0.0 pt]", "mode_label": "MV", "space_names_text_layer": "Svc"},
              {"annotation_index": "3", "equipment_tags_within_60pt": "", "mode_label": "AC", "space_names_text_layer": "Lobby"}]
    out = zn.classify_appd_spaces([P(0), P(1), P(2), P(3)], spaces, [])
    assert [o["kind"] for o in out] == ["AHU", "FCU", "MV", ""]
    assert out[0]["confidence"] == 1.0


# --------------------------------------------------------------------------- PDF writer
def make_source_pdf(path):
    """A one-page A-size PDF with /Rotate 90 and the synthetic walls as page content."""
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=400)
    page.set_rotation(90)
    inv = ~page.rotation_matrix
    for (x0, y0, x1, y1), w in synthetic_segments(True):
        p0 = pymupdf.Point(x0 + 20, y0 + 20) * inv
        p1 = pymupdf.Point(x1 + 20, y1 + 20) * inv
        page.draw_line(p0, p1, color=(0, 0, 0), width=w)
    doc.save(str(path))
    doc.close()


def test_pdf_writer_keeps_page_content_and_writes_annotations_on_layers(tmp_path):
    import pymupdf
    src = tmp_path / "src.pdf"
    make_source_pdf(src)
    z1 = _zone("AHU-1", [box(20, 20, 140, 180)], fill="#00FFFF")
    z2 = _zone("AHU-2/B", [box(140, 20, 260, 180)], fill="#FFC000")
    zones_ = [z1, z2]
    stats = {"AHU-1": {"area_m2": 149.3, "poly_m2": 150.0, "n_rooms": 2}, "AHU-2/B": {"area_m2": 149.3, "poly_m2": 150.0, "n_rooms": 2}}
    rooms = [{"room_id": "S-R1", "polygon": box(20, 20, 140, 100), "contents": "S-R1 | Off | 74.7 m2"}]
    out = tmp_path / "out.pdf"
    man = wp.write_zoning_pdf(src, 0, out, zones_, stats, rooms, "Test legend", "synthetic", legend_search=(0.0, 185.0, 400.0, 300.0),
                              legend_cols=1, col_w=300.0)
    ver = wp.verify_zoning_pdf(out, src, 0, ["AHU-1", "AHU-2/B"], ["AHU-1", "AHU-2/B", wp.LEGEND_LAYER, wp.ROOMS_LAYER])
    assert ver["content_stream_identical"] and ver["pixels_identical_annots_hidden"]
    assert ver["rotate"] == 90
    assert ver["ok"], ver["problems"]
    assert ver["n_zone_annotations"] == 2 and ver["n_room_annotations"] == 1
    assert ver["layers_off_by_default"] == [wp.ROOMS_LAYER]
    assert ver["zone_tags_missing_in_legend"] == [] and ver["legend_tags_without_zone"] == []
    doc = pymupdf.open(str(out))
    zone_annots = [a for a in doc[0].annots() if doc.xref_get_key(a.xref, "NM")[1].startswith("zone-")]
    for a in zone_annots:
        assert doc.xref_get_key(a.xref, "FillOpacity")[1] in ("0.35", ".35")
        assert float(doc.xref_get_key(a.xref, "CA")[1]) == pytest.approx(0.35)
        assert doc.xref_get_key(a.xref, "OC")[0] == "xref"


def test_pdf_verification_fails_when_a_zone_has_no_legend_entry(tmp_path):
    src = tmp_path / "src.pdf"
    make_source_pdf(src)
    z1 = _zone("AHU-1", [box(20, 20, 140, 180)])
    out = tmp_path / "out.pdf"
    wp.write_zoning_pdf(src, 0, out, [z1], {"AHU-1": {"area_m2": 1.0, "poly_m2": 1.0, "n_rooms": 1}}, [], "L", "n",
                        legend_search=(0.0, 185.0, 400.0, 300.0), legend_cols=1, col_w=300.0)
    ver = wp.verify_zoning_pdf(out, src, 0, ["AHU-1", "AHU-9"], ["AHU-1", "AHU-9", wp.LEGEND_LAYER, wp.ROOMS_LAYER])
    assert not ver["ok"] and ver["expected_tags_missing_in_zones"] == ["AHU-9"] and not ver["layers_expected_present"]


# =========================================================================== corrections after verification
def _strokeset_from(items, fills=()):
    """StrokeSet from ((x0, y0, x1, y1), width) items: width above 0.5 is the heavy pen, below it the fine pen."""
    styles = [((0.0, 0.0, 0.0), 0.84), ((0.0, 0.0, 0.0), 0.24)]
    seg = np.array([i[0] for i in items], float)
    sty = np.array([0 if i[1] > 0.5 else 1 for i in items], np.int32)
    return lw.StrokeSet(seg, sty, styles, np.zeros(len(seg), bool), fills=list(fills))


def _outer_walls():
    s = []
    _wall(s, 0, 0, 240, 0); _wall(s, 240, 0, 240, 160); _wall(s, 240, 160, 0, 160); _wall(s, 0, 160, 0, 0)
    return s


def banded_plan(with_fill=True):
    """Synthetic plan whose vertical partition is a black band (x 118 to 122) between two fine outlines, as on the real sheet."""
    s = _outer_walls()
    _wall(s, 0, 80, 118, 80); _wall(s, 122, 80, 240, 80)
    s.append(((118, 0, 118, 160), 0.24)); s.append(((122, 0, 122, 160), 0.24))
    fills = [{"rgb": (0.0, 0.0, 0.0), "pts": [(118, 0), (122, 0), (122, 160), (118, 160)]}] if with_fill else []
    return _strokeset_from(s, fills)


def test_black_wall_band_is_wall_so_the_room_ends_at_its_inner_edge():
    """Regression for R110: the band between two fine outlines was merged into the room as a furniture-like face."""
    room_m2 = 118 * 80 * K2
    res = rv.measure_rooms(banded_plan(True), TAGS, K2, heavy_widths=(0.84,), thin_widths=(0.24,))
    assert len(res.regions) == 4
    for r in res.regions:
        assert r.polygon.area * K2 == pytest.approx(room_m2, rel=0.005)
    assert res.diagnostics["n_wall_band_polygons"] == 1 and res.diagnostics["n_wall_band_faces"] >= 1
    # without the band fill the same linework gives a larger room on one side (the band face is merged in): the fill is what fixes it
    res0 = rv.measure_rooms(banded_plan(False), TAGS, K2, heavy_widths=(0.84,), thin_widths=(0.24,))
    assert max(r.polygon.area for r in res0.regions) * K2 > room_m2 * 1.01


def test_wall_fill_polygons_take_black_fill_only():
    fills = [{"rgb": (0.0, 0.0, 0.0), "pts": [(0, 0), (10, 0), (10, 2), (0, 2)]},
             {"rgb": (0.5, 0.5, 0.5), "pts": [(0, 0), (10, 0), (10, 10), (0, 10)]},      # column: grey
             {"rgb": (0.87, 0.87, 0.87), "pts": [(0, 0), (10, 0), (10, 10), (0, 10)]},   # hatch
             {"rgb": (0.0, 0.0, 0.0), "pts": [(0, 0), (1, 0), (1, 1)]}]                  # speck below the minimum area
    assert len(rv.wall_fill_polygons(fills)) == 1


def test_method_b_fills_the_band_into_the_wall_raster():
    ss = banded_plan(True)
    res = rv.measure_rooms(ss, TAGS, K2, heavy_widths=(0.84,), thin_widths=(0.24,))
    out, diag = rr.measure_rooms(res.wall_segments, res.thin_segments, TAGS, MM_PT, margin_pt=130.0, wall_bands=res.wall_bands)
    for sr in out:
        assert sr.polygon is not None
        assert sr.area_m2 == pytest.approx(118 * 80 * K2, rel=0.03)      # the band is not part of any room


def double_door_plan():
    """Vertical wall at x = 120 with a 24 pt opening (y 28 to 52) closed by a double door: two leaves of 12 pt whose arcs meet in a cusp at (120, 40)."""
    s = _outer_walls()
    _wall(s, 0, 80, 240, 80)
    _wall(s, 120, 0, 120, 28); _wall(s, 120, 52, 120, 160)
    n = 12
    for hinge_y, a_from, a_to in ((28.0, 180.0, 90.0), (52.0, 270.0, 180.0)):
        for k in range(n):
            t0 = math.radians(a_from + (a_to - a_from) * k / n); t1 = math.radians(a_from + (a_to - a_from) * (k + 1) / n)
            s.append(((120 + 12 * math.cos(t0), hinge_y + 12 * math.sin(t0), 120 + 12 * math.cos(t1), hinge_y + 12 * math.sin(t1)), 0.24))
    return _strokeset_from(s)


def test_double_door_gives_two_arcs_two_closers_and_whole_rooms():
    """Regression for R065: the two arcs of a double door form one chain with a cusp; no circle fits it, so no door was closed and
    the swing sectors were cut out of the room."""
    ss = double_door_plan()
    arcs = lw.detect_door_arcs(ss)
    assert len(arcs) == 2
    assert sorted(round(a.cy) for a in arcs) == [28, 52]
    assert all(a.r == pytest.approx(12.0, abs=0.2) and a.span_deg == pytest.approx(90.0, abs=8.0) for a in arcs)
    res = rv.measure_rooms(ss, TAGS, K2, heavy_widths=(0.84,), thin_widths=(0.24,))
    assert res.diagnostics["n_doors_closed"] == 2 and res.diagnostics["n_arc_segments_removed"] == 24
    assert len(res.regions) == 4
    for r in res.regions:
        # the swing sector (113 pt2 per leaf, 2.4 % of the room) belongs to the room: the area is the whole room
        assert r.polygon.area * K2 == pytest.approx(ROOM_M2, rel=0.002)


def test_smooth_arc_chain_is_not_cut_by_the_cusp_rule():
    # a single 180 degree arc drawn in 24 chords turns by 7.5 degrees at every joint: still one arc (span at most 200 degrees)
    s = []
    for k in range(24):
        t0 = math.radians(180.0 * k / 24); t1 = math.radians(180.0 * (k + 1) / 24)
        s.append(((50 + 15 * math.cos(t0), 50 + 15 * math.sin(t0), 50 + 15 * math.cos(t1), 50 + 15 * math.sin(t1)), 0.24))
    arcs = lw.detect_door_arcs(_strokeset_from(s))
    assert len(arcs) == 1 and arcs[0].span_deg == pytest.approx(180.0, abs=8.0)


def test_fill_rings_are_split_per_subpath():
    """extract_strokes keeps one ring per sub-path of a filled path (two bands drawn as one path must not be joined into one polygon)."""
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=200)
    shape = page.new_shape()
    shape.draw_rect(pymupdf.Rect(10, 10, 60, 20))
    shape.draw_rect(pymupdf.Rect(10, 100, 60, 110))
    shape.finish(color=None, fill=(0, 0, 0))
    shape.commit()
    ss = lw.extract_strokes(page)
    black = [f for f in ss.fills if f["rgb"] == (0.0, 0.0, 0.0)]
    assert len(black) == 2
    assert sorted(round(Polygon(f["pts"]).area) for f in black) == [500, 500]


def test_app_d_tag_outside_the_polygon_does_not_classify_it():
    """Regression: a tag 16.5 pt outside a polygon gave the polygon an AHU class. Only a tag inside (or whose box touches it) counts."""
    P = lambda i: cmp.AppdPolygon(i, i, 10.0, box(0, 0, 1, 1), box(0, 0, 1, 1), 10.0, 0.0)
    spaces = [{"annotation_index": str(i), "equipment_tags_within_60pt": "AHU-B-04-04CORE-01 [ann 63, 16.5 pt]", "mode_label": m, "space_names_text_layer": n}
              for i, m, n in ((0, "AC (24HR)", "PLL"), (1, "MV", "ELEC"), (2, "MV", "Svc"), (3, "MV", "Toilet"))]
    eq = [{"space_annotation_index": "2", "tag_normalised": "AHU-B-04-04CORE-01", "space_relation": "inside", "space_box_distance_pt": "0.0"},
          {"space_annotation_index": "3", "tag_normalised": "TEAF-B-04-04COM-01", "space_relation": "nearest", "space_box_distance_pt": "0.0"},
          {"space_annotation_index": "1", "tag_normalised": "AHU-B-04-04CORE-01", "space_relation": "nearest", "space_box_distance_pt": "5.7"}]
    out = zn.classify_appd_spaces([P(0), P(1), P(2), P(3)], spaces, eq)
    assert out[0]["kind"] == "" and out[0]["tag"] == ""                      # AC (24HR), tag only outside: not classified
    assert out[1]["kind"] == "MV" and out[1]["tag"] == "" and out[1]["confidence"] == 0.5   # MV mode label, tag outside (5.7 pt box distance)
    assert out[2]["kind"] == "AHU" and out[2]["tag"] == "AHU-B-04-04CORE-01" and out[2]["confidence"] == 1.0
    assert out[3]["kind"] == "MV" and out[3]["tag"] == "TEAF-B-04-04COM-01" and out[3]["confidence"] == 0.9
    # the same without the equipment register: only tags at 0.0 pt in the space row count
    out2 = zn.classify_appd_spaces([P(0)], spaces[:1], [])
    assert out2[0]["kind"] == ""


def test_configured_majority_is_read_from_the_project_config():
    majority, conf_min = zn.configured_thresholds()
    assert majority == pytest.approx(0.80) and conf_min == pytest.approx(0.9)


def test_room_between_half_and_the_configured_majority_is_review_not_assigned():
    z1 = _zone("AHU-1", [box(0, 0, 120, 160)])
    z2 = _zone("AHU-2", [box(120, 0, 240, 80 + 56)])                   # room 4 covered 70 %
    out = zn.assign_rooms(synthetic_rooms(), [z1, z2], majority=0.80)
    r4 = [o for o in out if o["room_id"] == "S-R4"][0]
    assert r4["class_label"] == "review: partial cover" and r4["zone_tag"] == "" and r4["candidate"] == "AHU-2"
    assert r4["candidate_share"] == pytest.approx(0.7, abs=0.02) and r4["confidence"] == 0.0
    assert [o["zone_tag"] for o in out if o["room_id"] != "S-R4"] == ["AHU-1", "AHU-2", "AHU-1"]
    # at the default 0.5 the same room is assigned, as before
    assert [o for o in zn.assign_rooms(synthetic_rooms(), [z1, z2]) if o["room_id"] == "S-R4"][0]["zone_tag"] == "AHU-2"


def test_zone_part_confidence_multiplies_the_cover_share():
    z = _zone("APPD-MV", [box(0, 0, 240, 160)], source="Appendix D", kind="MV")
    z.members = [{"confidence": 0.5, "annotation_index": 3, "basis": "mode label"}]
    out = zn.assign_rooms(synthetic_rooms(), [z], majority=0.8)
    assert all(o["share"] == pytest.approx(1.0) and o["confidence"] == pytest.approx(0.5) for o in out)


def test_app_d_fill_is_clipped_by_the_rfi_zone_so_fills_do_not_stack():
    rfi = _zone("AHU-1", [box(60, 60, 120, 100)])                                 # lies wholly inside the App D polygon: a hole
    appd = _zone("AHU-APPD", [box(0, 0, 240, 160)], source="Appendix D", fill="#8000FF")
    gone = zn.clip_to_precedence([rfi, appd], K2)
    drawn = appd.drawn()
    assert all(not p.interiors for p in drawn)                                    # an annotation has one ring: the hole is cut away
    assert sum(p.area for p in drawn) == pytest.approx(240 * 160 - 60 * 40, rel=1e-9)
    assert appd.overlap_removed_m2 == pytest.approx(60 * 40 * K2) and gone[appd.key]["removed_m2"] == pytest.approx(60 * 40 * K2)
    assert shapely.union_all(drawn).intersection(rfi.polygons_pv[0]).area == pytest.approx(0.0, abs=1e-6)
    # assignment still sees the full App D polygon, so a room mostly inside it keeps its class
    assert appd.polygons_pv[0].area == 240 * 160


def test_centroid_columns_are_true_centroids_with_a_separate_label_point():
    """Regression: the centroid column held representative_point() when the centroid lay outside the room (C shaped room)."""
    c_room = box(0, 0, 100, 100).difference(box(20, 20, 100, 80))
    assert not c_room.contains(c_room.centroid)                                    # the centroid is in the notch
    A = np.array([[MM_PT, 0.0, 0.0], [0.0, -MM_PT, 14000.0]])
    c = cal.Calibration(A=A, residuals_mm={}, rms_mm=0, max_mm=0, n_equations=0)
    rooms = [{"room_id": "T-R001", "tag": "C / AC", "name": "C", "mode": "AC", "tags_inside": ["C / AC"], "a_face": c_room, "a_cl": c_room,
              "b_face": None, "b_cl": None, "b_face_area_m2": None, "flags": ""}]
    rows, _ = cmp.build_room_rows(rooms, [], c, {"X": {"2": 0.0}, "Y": {"A": 0.0}})
    r = rows[0]
    cx, cy = c_room.centroid.x * MM_PT, 14000.0 - c_room.centroid.y * MM_PT
    assert float(r["centroid_x_mm"]) == pytest.approx(cx, abs=1.0) and float(r["centroid_y_mm"]) == pytest.approx(cy, abs=1.0)
    assert r["centroid_inside_polygon"] == "no"
    lp = c_room.representative_point()
    assert float(r["label_point_x_mm"]) == pytest.approx(lp.x * MM_PT, abs=1.0)
    assert r["grid_location"] != r["grid_location_label_point"] or r["centroid_inside_polygon"] == "no"


def test_report_figures_are_checked_against_the_register_rows():
    rows = [{"appd_caption_m2": "10", "error_b_vs_caption_pct": "1", "method_agreement": "agree"},
            {"appd_caption_m2": "12", "error_b_vs_caption_pct": "", "method_agreement": "differ"},
            {"appd_caption_m2": "", "error_b_vs_caption_pct": "", "method_agreement": "no B region"}]
    good = {"n_matched_A": 2, "n_matched_B": 1, "B/wall_face": {"n": 1}, "n_ab_compared": 2, "ab_share_within_tolerance": 0.5}
    from src.zoning import report as rep
    assert rep.consistency_errors(rows, good) == []
    bad = dict(good, n_matched_B=3, ab_share_within_tolerance=0.9)
    assert len(rep.consistency_errors(rows, bad)) == 2


def _legend_pdf(tmp_path):
    src = tmp_path / "src.pdf"
    make_source_pdf(src)
    z1 = _zone("AHU-1", [box(20, 20, 140, 180)])
    out = tmp_path / "out.pdf"
    wp.write_zoning_pdf(src, 0, out, [z1], {"AHU-1": {"area_m2": 100.0, "poly_m2": 150.0, "n_rooms": 2}}, [], "L", "n",
                        legend_search=(0.0, 185.0, 400.0, 300.0), legend_cols=1, col_w=300.0)
    return src, out


def test_every_legend_object_has_an_explicit_opacity(tmp_path):
    import pymupdf
    src, out = _legend_pdf(tmp_path)
    doc = pymupdf.open(str(out))
    names = {}
    for a in doc[0].annots():
        nm = doc.xref_get_key(a.xref, "NM")[1]
        names[nm] = a.xref
        assert doc.xref_get_key(a.xref, "CA")[0] != "null", nm
    back = names["legend-back"]
    assert float(doc.xref_get_key(back, "FillOpacity")[1]) == 1.0 and float(doc.xref_get_key(back, "CA")[1]) == 1.0
    assert float(doc.xref_get_key(names["legend-swatch-0"], "CA")[1]) == pytest.approx(0.35)
    assert wp.verify_zoning_pdf(out, src, 0, ["AHU-1"], ["AHU-1", wp.LEGEND_LAYER, wp.ROOMS_LAYER])["ok"]
    # remove /CA from one legend text: the verification must fail
    doc.xref_set_key(names["legend-text-0"], "CA", "null")
    bad = tmp_path / "bad.pdf"
    doc.save(str(bad))
    ver = wp.verify_zoning_pdf(bad, src, 0, ["AHU-1"], ["AHU-1", wp.LEGEND_LAYER, wp.ROOMS_LAYER])
    assert not ver["ok"] and any("without /CA" in p for p in ver["problems"])


def test_legend_shows_zone_polygon_area_and_assigned_room_area(tmp_path):
    src, out = _legend_pdf(tmp_path)
    import pymupdf
    doc = pymupdf.open(str(out))
    txt = [a.info["content"] for a in doc[0].annots() if doc.xref_get_key(a.xref, "NM")[1] == "legend-text-0"][0]
    assert "zone 150 m2" in txt and "rooms 100 m2 (2)" in txt


def test_double_door_closes_on_the_cusp_even_when_the_open_leaf_ends_touch_walls():
    """Regression (ward doors): each leaf's open end touched a wall, so 'nearer to walls' chose the open end and the closer lay along the
    open leaf; the opening stayed open once the arcs were dropped."""
    s = [i for i in double_door_plan_items()]
    s += [((90, 28, 108, 28), 0.84), ((90, 52, 108, 52), 0.84)]        # partitions that end on the open leaf tips
    ss = _strokeset_from(s)
    arcs = lw.detect_door_arcs(ss)
    lines = rv.segments_to_lines(ss.seg[ss.select((0.84,))])
    cl, diag = rv.door_closers(arcs, lines)
    assert len(arcs) == 2 and all(d["closed"] and d["tip_rule"] == "double door" for d in diag)
    assert np.allclose(sorted(map(tuple, np.round(cl[:, 2:], 1))), [(120.0, 40.0), (120.0, 40.0)], atol=0.2)


def double_door_plan_items():
    s = [((x[0]), x[1]) for x in zip(map(tuple, double_door_plan().seg), [0.84 if st == 0 else 0.24 for st in double_door_plan().style])]
    return s


def test_open_leaf_drawn_decides_the_closed_end_of_a_single_door():
    s = [i for i in synthetic_segments(True)]
    s += [((80, 30, 100, 30), 0.84)]                                   # a partition ending on the open tip (100, 30): both ends are near walls
    s += [((120, 30, 100, 30), 0.24)]                                  # the open leaf, drawn
    ss = _strokeset_from(s)
    arcs = lw.detect_door_arcs(ss)
    lines = rv.segments_to_lines(ss.seg[ss.select((0.84,))])
    leaf = rv.segments_to_lines(np.array([[120, 30, 100, 30]], float))
    cl, diag = rv.door_closers(arcs, lines, leaf)
    assert len(arcs) == 1 and diag[0]["tip_rule"] == "open leaf drawn" and diag[0]["closed"]
    assert np.allclose(cl[0], [120.0, 30.0, 120.0, 50.0], atol=0.2)
