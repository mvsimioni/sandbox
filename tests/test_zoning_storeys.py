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
    assert st.colour_for("AHU-B-04-04MRI-01") == "#0000FF"      # the sample's colour is reused


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
