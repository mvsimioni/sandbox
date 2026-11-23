"""Unit tests for the storey generalisation (stub assignment, zone keys, colours, multi-page PDF writer)."""

import pymupdf
import pytest
from shapely.geometry import box

from src.zoning import storeys as st
from src.zoning import write_pdf as wp
from src.zoning import zones as zn


def test_stub_class_from_mode_label_only():
    assert st.stub_class(["MV"])[:2] == ("MV", 1.0)
    assert st.stub_class(["NV"])[:2] == ("NV", 1.0)
    for m in (["AC"], ["AC (24HR)"], [""], []):
        assert st.stub_class(m)[0] == "no information"
    c, conf, note = st.stub_class(["MV", "NV"])
    assert c == "no information" and "conflicting" in note
    assert st.stub_class(["MV", ""])[:2] == ("MV", 0.5)


def test_duty_standby_is_one_zone():
    assert st.zone_key("AHU-B-04-04RAD-01A")[0] == st.zone_key("AHU-B-04-04RAD-01B")[0] == st.zone_key("AHU-B-04-04RAD-01A/B")[0]
    assert st.zone_key("AHU-B-04-04RAD-01A")[1] == "AHU-B-04-04RAD-01A/B"
    assert st.zone_key("AHU-B-07-04ANGIO-01") == ("AHU-B-07-04ANGIO-01", "AHU-B-07-04ANGIO-01")


def test_colours_stable_and_never_red():
    c1 = st.colour_for("AHU-B-05-05XYZ-01")
    assert c1 == st.colour_for("AHU-B-05-05XYZ-01")
    seen = {st.colour_for("AHU-B-05-05T%03d-01" % i) for i in range(200)}
    assert "#FF0000" not in seen
    # one colour per unit on every page, chosen to be far from the colours of the units on the same storeys (the sample's colour is not reused since round 1)
    assert st.colour_for("AHU-B-04-04MRI-01") == st.colour_for("AHU-B-04-04MRI-01") != "#FF0000"


def _src(path):
    d = pymupdf.open()
    for i in range(2):
        p = d.new_page(width=800, height=600)
        p.insert_text((100, 100 + 20 * i), "sheet %d" % i)
    d.set_page_labels if False else None
    d[1].set_rotation(90)
    d.save(str(path))


def test_multi_page_writer_and_verifier(tmp_path):
    src, out = tmp_path / "s.pdf", tmp_path / "o.pdf"
    _src(src)
    specs = []
    for i, key in enumerate(["A1", "A2"]):
        z = zn.Zone(key="AHU-X-01", kind="AHU", source="t", tag="AHU-X-01A/B", polygons_pv=[box(50, 50, 150, 120)], draw_polygons_pv=[box(50, 50, 150, 120)],
                    fill_hex=st.colour_for("AHU-X-01"), departments="Test", storey_served=key, plant_level="L1", refs=["unit test"])
        mv = zn.Zone(key="MV", kind="MV", source="t", tag="MV", polygons_pv=[box(200, 50, 300, 120)], draw_polygons_pv=[box(200, 50, 300, 120)],
                     fill_hex=st.CLASS_COLOURS["MV"], departments="n/a", storey_served=key, refs=["unit test"])
        specs.append({"key": key, "label": "L", "drawing": "D", "page_index": i, "zones": [z, mv],
                      "zone_stats": {"AHU-X-01": {"area_m2": 10, "poly_m2": 10, "n_rooms": 1}, "MV": {"area_m2": 5, "poly_m2": 5, "n_rooms": 1}},
                      "rooms": [{"room_id": "%s-R1" % key, "polygon": box(50, 50, 150, 120), "contents": "room"}], "legend_title": "t", "sheet_note": "n", "unclassified": {}})
    man = wp.write_multi_zoning_pdf(src, specs, out, "title")
    assert man["n_index_pages"] == 1 and len(pymupdf.open(str(out))) == 3
    ver = wp.verify_multi_zoning_pdf(out, src, man["page_map"], man["expected_layers"], man["expected_tags"])
    assert ver["ok"], ver["problems"]
    assert "Rooms (measured)" in ver["layers_off_by_default"]
    assert pymupdf.open(str(out))[2].rotation == 90


# --------------------------------------------------------------------------- #
# Corrections after the first verification
# --------------------------------------------------------------------------- #
import numpy as np

from src import common
from src.zoning import linework as lw


def _stroke_set(segs, widths):
    styles = []
    sty = []
    for w in widths:
        key = ((0.0, 0.0, 0.0), w)
        if key not in styles:
            styles.append(key)
        sty.append(styles.index(key))
    return lw.StrokeSet(np.asarray(segs, float), np.asarray(sty, np.int32), styles, np.zeros(len(segs), bool), [])


def _dimension(x0, x1, y):
    """A horizontal dimension string: line y between two tick nodes, oblique ticks and extension stubs as the Arch sheets draw them."""
    segs, w = [(x0, y, x1, y)], [0.24]
    for x in (x0, x1):
        segs += [(x, y, x + 1.8, y + 1.8), (x, y, x - 1.8, y - 1.8)]
        w += [0.6, 0.6]
        segs += [(x, y, x, y - 5.64), (x, y, x, y + 6.0)]
        w += [0.24, 0.24]
    return segs, w


def test_dimension_strings_are_not_walls():
    wall = [(0, 0, 200, 0), (0, 80, 200, 80), (0, 0, 0, 80), (200, 0, 200, 80)]
    segs, w = _dimension(0, 200, 60)
    ss = _stroke_set(wall + segs, [0.84] * 4 + w)
    out, diag = st.strip_dimension_strokes(ss)
    assert diag["n_dimension_lines"] == 1 and diag["n_extension_stubs"] == 4
    assert len(out.seg) == 4                                   # only the four walls remain
    # a fine-pen partition that is not a dimension (no ticks) stays
    part = [(50, 0, 50, 80)]
    ss2 = _stroke_set(wall + part, [0.84] * 4 + [0.24])
    out2, d2 = st.strip_dimension_strokes(ss2)
    assert len(out2.seg) == 5 and d2["n_removed"] == 0


def test_leaked_region_rule():
    assert st.is_leaked(96, 2571.0) and st.is_leaked(4, 600.0) and st.is_leaked(2, 1100.0)
    assert not st.is_leaked(1, 3000.0) and not st.is_leaked(3, 200.0) and not st.is_leaked(30, 357.0) and not st.is_leaked(10, 126.0)


def test_na_mode_label_recovered_from_text_layer():
    d = pymupdf.open()
    p = d.new_page(width=400, height=300)
    p.insert_text((100, 100), "Hydrant Tank 2", fontsize=6)
    p.insert_text((100, 107), "NA", fontsize=4)
    p.insert_text((200, 200), "NA", fontsize=4)             # a word under no tag
    tags = [{"t_index": 0, "text": "Hydrant Tank 2", "mode": "", "box_pv": "100,94,136,100.5"}]
    n, loose = st.na_modes_from_page(p, tags)
    assert n == 1 and tags[0]["mode"] == "NA" and "page text layer" in tags[0]["mode_source"] and len(loose) == 1
    assert st.stub_class(["NA"])[0] == "no information"


def test_zone_areas_are_m2(tmp_path):
    reg = tmp_path / "reg"
    reg.mkdir()
    poly = box(0, 0, 100, 50)                                   # 5000 pt2
    common.write_csv_dicts(reg / "room_polygons_T1.csv", [{"room_id": "T1-R001", "room_tag": "x / MV", "a_area_m2": 50.0, "polygon_pv_wkt": poly.wkt, "method_agreement": "agree"}],
                           ["room_id", "room_tag", "a_area_m2", "polygon_pv_wkt", "method_agreement"])
    common.write_csv_dicts(reg / "zone_assignment_T1.csv", [{"room_id": "T1-R001", "class": "MV", "confidence": "1.0", "department": "", "sources": "s"}],
                           ["room_id", "class", "confidence", "department", "sources"])
    zones, stats, ann, summ = st.build_page_zones("T1", {}, {"reg": reg}, 0.01)    # 0.01 m2 per pt2
    assert stats["MV"]["poly_m2"] == pytest.approx(50.0) and stats["MV"]["area_m2"] == pytest.approx(50.0)
    assert summ["rooms_no_polygon"] == 0



def test_dash_bridges_close_dashed_lines_only():
    dashes = [(x, 10.0, x + 7.0, 10.0) for x in (0.0, 11.0, 22.0, 33.0)]      # four dashes, gaps of 4 pt
    br = st.dash_bridge_segments(np.asarray(dashes, float))
    assert len(br) == 3 and np.allclose(br[:, 1], 10.0) and np.allclose(br[0], (7.0, 10.0, 11.0, 10.0))
    two = st.dash_bridge_segments(np.asarray(dashes[:2], float))              # two dashes are no pattern
    assert len(two) == 0
    wide = st.dash_bridge_segments(np.asarray([(x, 10.0, x + 7.0, 10.0) for x in (0.0, 20.0, 40.0)], float))   # 13 pt gaps: an opening, not a dash gap
    assert len(wide) == 0
