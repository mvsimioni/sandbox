"""Tests for src.rds_parser on synthetic Room Data Sheets built with pymupdf (never the real files).

Two synthetic layouts are built here and described by their own template JSON:

* ``SYN-A``  labels in a left column with the value to the right (row_band), an inline
  two-column block (label and value glued or separate), and a ruled equipment table;
* ``SYN-B``  labels with the value below (rule ``below``), as a second layout.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pymupdf
import pytest

from src import rds_parser as rp

PAGE_W, PAGE_H = 595.0, 842.0


# --------------------------------------------------------------------------- #
# Templates for the two synthetic layouts
# --------------------------------------------------------------------------- #
def template() -> Dict[str, Any]:
    """A layouts JSON (as data) with the two synthetic layouts in one family."""
    return {
        "schema": 1,
        "normalise": {"#": "ti"},
        "defaults": {"x_max": 566.0, "label_x_max": 150.0},
        "families": [{
            "id": "SYN", "name": "synthetic", "page_size": [PAGE_W, PAGE_H], "size_tol": 6.0,
            "header": {"fields": [
                {"key": "room_name", "label_text": "(header room name)", "region": [140.0, 30.0, 400.0, 62.0]},
                {"key": "room_code", "label_text": "(header room code)", "region": [400.0, 30.0, 590.0, 62.0]},
            ]},
            "layouts": ["SYN-A", "SYN-B"],
        }],
        "layouts": [
            {
                "id": "SYN-A", "family": "SYN", "page_type": "room_summary", "starts_room": True,
                "match": {"anchors": ["Revision", "Issue Date", "Area", "Hours of Operation", "Occupancy", "ACMV", "Equipment"], "min_fraction": 0.6},
                "fields": [
                    {"key": "revision", "labels": ["Revision"], "rule": "row_band", "expected": True},
                    {"key": "issue_date", "labels": ["Issue Date"], "rule": "row_band", "expected": True},
                    {"key": "area", "labels": ["Area"], "rule": "row_band", "expected": True},
                    {"key": "hours_of_operation", "labels": ["Hours of Operation"], "rule": "row_band", "expected": True},
                    {"key": "occupancy", "labels": ["Occupancy"], "rule": "row_band", "expected": True},
                ],
                "sections": [{
                    "key": "acmv", "labels": ["ACMV"], "expected": True, "cols": [[150.0, 360.0], [360.0, 566.0]],
                    "fields": [
                        {"key": "room_temperature", "labels": ["Room Temperature"], "col": 0, "rule": "inline", "expected": True},
                        {"key": "room_pressure", "labels": ["Room Pressure"], "col": 0, "rule": "inline", "expected": True},
                        {"key": "room_humidity", "labels": ["Room Humidity"], "col": 0, "rule": "inline", "expected": True},
                        {"key": "exhaust_requirements", "labels": ["Exhaust Requirements"], "col": 1, "rule": "inline", "expected": True},
                    ],
                }],
                "tables": [{"key": "equipment", "title": ["Equipment"], "expected": True, "columns": [["Code"], ["Description"], ["Qty"]]}],
            },
            {
                "id": "SYN-B", "family": "SYN", "page_type": "room_notes", "starts_room": True,
                "match": {"anchors": ["Form B", "Room Function", "Design Notes"], "min_fraction": 0.6},
                "fields": [
                    {"key": "room_function", "labels": ["Room Function"], "rule": "below", "expected": True},
                    {"key": "design_notes", "labels": ["Design Notes"], "rule": "below", "expected": True},
                ],
            },
        ],
        "column_sets": {},
    }


@pytest.fixture()
def layouts(tmp_path: Path) -> Dict[str, Any]:
    path = tmp_path / "layouts.json"
    path.write_text(json.dumps(template()), encoding="utf-8")
    return rp.load_layouts(path)


# --------------------------------------------------------------------------- #
# Synthetic PDF builders
# --------------------------------------------------------------------------- #
def _line(page: pymupdf.Page, p1: tuple, p2: tuple, width: float = 1.0, dashes: Optional[str] = None) -> None:
    shape = page.new_shape()
    shape.draw_line(p1, p2)
    shape.finish(width=width, color=(0, 0, 0), dashes=dashes)
    shape.commit()


def _header(page: pymupdf.Page, code: str, name: str) -> None:
    page.insert_text((150, 48), name, fontsize=9, fontname="hebo")
    page.insert_text((480, 48), code, fontsize=9, fontname="hebo")


def _label(page: pymupdf.Page, text: str, baseline_y: float, right_x: float = 130.0) -> None:
    width = pymupdf.get_text_length(text, fontname="hebo", fontsize=8)
    page.insert_text((right_x - width, baseline_y), text, fontsize=8, fontname="hebo")


def build_syn_a(path: Path, *, rules: bool = True, occupancy_label: str = "Occupancy", drop_occupancy: bool = False,
                extra_label: bool = False, annot: bool = False, code: str = "TR-001", name: str = "Test Room One",
                hours_label: str = "Hours of Opera#on") -> Path:
    """Layout SYN-A: row bands, an inline ACMV block and a ruled equipment table."""
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    _header(page, code, name)
    rows = [("Revision", 82, 70, 90, ["B"]), ("Issue Date", 102, 90, 110, ["JAN 2026"]),
            ("Area", 122, 110, 130, ["12.50 m2"]), (hours_label, 142, 130, 165, ["Mon-Fri 8am-6pm", "Sat 8am-12pm"]),
            (occupancy_label, 177, 165, 185, ["3"])]
    for label, base, top, bottom, values in rows:
        if drop_occupancy and label == occupancy_label:
            continue
        _label(page, label, base)
        if rules:
            _line(page, (140, top), (140, bottom), width=1.0)
        for i, value in enumerate(values):
            page.insert_text((150, base + 8 * i), value, fontsize=8, fontname="helv")
    if extra_label:
        _label(page, "Special Notes", 195)
        page.insert_text((150, 195), "Bring the key", fontsize=8, fontname="helv")
    # ACMV section between two full-width solid rules
    if rules:
        _line(page, (147, 200), (562, 200))
        _line(page, (147, 285), (562, 285))
    _label(page, "ACMV", 225, right_x=135)
    page.insert_text((160, 225), "Room Temperature", fontsize=8, fontname="helv")
    page.insert_text((280, 225), "21 to 24 deg C", fontsize=8, fontname="helv")
    page.insert_text((160, 240), "Room Pressure+15Pa", fontsize=8, fontname="helv")      # label and value glued in one text run
    page.insert_text((160, 255), "Room Humidity <60%", fontsize=8, fontname="helv")
    page.insert_text((370, 225), "Exhaust Requirements", fontsize=8, fontname="helv")
    page.insert_text((480, 225), "High Plume Fan, 15 ACH", fontsize=8, fontname="helv")
    if rules:
        for y, x0 in ((227.5, 278), (242.5, 216), (257.5, 232), (227.5, 478)):
            _line(page, (x0, y), (358 if x0 < 360 else 562, y), width=0.5, dashes="[2] 0")
    # ruled table
    page.insert_text((40, 318), "Equipment", fontsize=8, fontname="hebo")
    ys = [330, 348, 366, 384]
    xs = [36, 120, 400, 562]
    for y in ys:
        _line(page, (36, y), (562, y))
    for x in xs:
        _line(page, (x, 330), (x, 384))
    for x, head in ((40, "Code"), (125, "Description"), (405, "Qty")):
        page.insert_text((x, 343), head, fontsize=8, fontname="hebo")
    for y, cells in ((361, ("EQ-01", "Chilled water pump", "2")), (379, ("EQ-02", "Fan coil unit", "4"))):
        for x, cell in zip((40, 125, 405), cells):
            page.insert_text((x, y), cell, fontsize=8, fontname="helv")
    if annot:
        page.add_freetext_annot(pymupdf.Rect(148, 92, 230, 106), "FEB 2026", fontsize=8)
    doc.save(str(path))
    doc.close()
    return path


def build_syn_b(path: Path, *, rules: bool = True, code: str = "TR-002", name: str = "Test Room Two") -> Path:
    """Layout SYN-B: labels with the value below."""
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    _header(page, code, name)
    page.insert_text((40, 80), "Form B", fontsize=9, fontname="hebo")
    page.insert_text((40, 100), "Room Function", fontsize=8, fontname="hebo")
    page.insert_text((40, 114), "Cleanroom ISO 7", fontsize=8, fontname="helv")
    page.insert_text((40, 150), "Design Notes", fontsize=8, fontname="hebo")
    page.insert_text((40, 164), "Positive pressure, HEPA", fontsize=8, fontname="helv")
    page.insert_text((40, 174), "filtered supply", fontsize=8, fontname="helv")
    if rules:
        _line(page, (36, 130), (300, 130))
        _line(page, (36, 190), (300, 190))
    doc.save(str(path))
    doc.close()
    return path


def by_key(room: rp.Room) -> Dict[str, rp.FieldRec]:
    return {f.key: f for f in room.fields}


# --------------------------------------------------------------------------- #
# Layout SYN-A
# --------------------------------------------------------------------------- #
def test_row_band_values_bboxes_and_confidence(tmp_path: Path, layouts: Dict[str, Any]) -> None:
    pdf = build_syn_a(tmp_path / "a.pdf")
    fr = rp.parse_pdf(pdf, layouts, rel_path="a.pdf")
    assert len(fr.rooms) == 1
    room = fr.rooms[0]
    got = by_key(room)
    assert room.code == "TR-001" and room.name == "Test Room One"
    assert got["revision"].value == "B"
    assert got["issue_date"].value == "JAN 2026"
    assert got["area"].value == "12.50 m2"
    assert got["occupancy"].value == "3"
    # wrapped value: both lines are read, joined in reading order
    assert got["hours_of_operation"].value == "Mon-Fri 8am-6pm Sat 8am-12pm"
    for key in ("revision", "issue_date", "area", "hours_of_operation", "occupancy"):
        f = got[key]
        assert f.confidence == 1.0 and f.method == "label+row_band_rule"
        assert f.value_bbox is not None and f.label_bbox is not None
        assert f.value_bbox[0] >= 148.0, "value starts right of the divider rule (x=140) at the value column"
        assert f.label_bbox[2] <= 135.0
        assert f.layout_id == "SYN-A" and f.page == 1
    # the value bbox lies inside its row band (rule to rule) and next to the label
    assert 70 <= got["revision"].value_bbox[1] and got["revision"].value_bbox[3] <= 90
    assert 130 <= got["hours_of_operation"].value_bbox[1] and got["hours_of_operation"].value_bbox[3] <= 165


def test_ligature_normalisation_in_labels(tmp_path: Path, layouts: Dict[str, Any]) -> None:
    """The printed label 'Opera#on' reads as 'Operation' through the template's normalise map."""
    pdf = build_syn_a(tmp_path / "a.pdf", hours_label="Hours of Opera#on")
    room = rp.parse_pdf(pdf, layouts).rooms[0]
    f = by_key(room)["hours_of_operation"]
    assert f.label == "Hours of Operation"
    assert f.confidence == 1.0


def test_inline_pairs_glued_and_separate(tmp_path: Path, layouts: Dict[str, Any]) -> None:
    room = rp.parse_pdf(build_syn_a(tmp_path / "a.pdf"), layouts).rooms[0]
    got = by_key(room)
    assert got["acmv.room_temperature"].value == "21 to 24 deg C"      # separate text run
    assert got["acmv.room_pressure"].value == "+15Pa"                    # glued to its label in the text layer
    assert got["acmv.room_humidity"].value == "<60%"
    assert got["acmv.exhaust_requirements"].value == "High Plume Fan, 15 ACH"   # right-hand column
    glued = got["acmv.room_pressure"]
    assert glued.confidence == 1.0 and "underline" in glued.method
    # the value box starts after the label box (split inside one text run), not at the label start
    assert glued.value_bbox[0] >= glued.label_bbox[2] - 0.5
    assert glued.label.replace(" ", "") == "RoomPressure"


def test_ruled_table_rows(tmp_path: Path, layouts: Dict[str, Any]) -> None:
    room = rp.parse_pdf(build_syn_a(tmp_path / "a.pdf"), layouts).rooms[0]
    cells = [f for f in room.fields if f.section == "equipment"]
    rows: Dict[str, Dict[str, rp.FieldRec]] = {}
    for f in cells:
        rows.setdefault(f.row, {})[f.key.split(".")[1]] = f
    assert sorted(rows) == ["1", "2"]
    assert rows["1"]["code"].value == "EQ-01" and rows["1"]["description"].value == "Chilled water pump" and rows["1"]["qty"].value == "2"
    assert rows["2"]["code"].value == "EQ-02" and rows["2"]["qty"].value == "4"
    assert all(f.confidence == 1.0 for f in cells)
    assert rows["2"]["code"].value_bbox[1] >= 366 - 1 and rows["2"]["code"].value_bbox[3] <= 384 + 1
    assert rows["1"]["code"].label == "Code"


def test_complete_sheet_counts_every_expected_item(tmp_path: Path, layouts: Dict[str, Any]) -> None:
    fr = rp.parse_pdf(build_syn_a(tmp_path / "a.pdf"), layouts)
    room = fr.rooms[0]
    assert room.expected == room.found == 5 + 1 + 4 + 1   # row fields + section + inline fields + table
    assert room.unmatched == []


# --------------------------------------------------------------------------- #
# Heuristic and fallback confidence
# --------------------------------------------------------------------------- #
def test_region_heuristics_score_point_seven(tmp_path: Path, layouts: Dict[str, Any]) -> None:
    """Without divider rules the region comes from the next label: right values, confidence 0.7."""
    room = rp.parse_pdf(build_syn_a(tmp_path / "a.pdf", rules=False), layouts).rooms[0]
    got = by_key(room)
    for key, value in (("revision", "B"), ("issue_date", "JAN 2026"), ("area", "12.50 m2"), ("occupancy", "3"),
                       ("hours_of_operation", "Mon-Fri 8am-6pm Sat 8am-12pm")):
        assert got[key].value == value
        assert got[key].confidence == 0.7
        assert got[key].method == "label+next_label_region"
    # inline values lose the section rules and underlines: still read, no longer exact
    assert got["acmv.room_pressure"].value == "+15Pa"
    assert got["acmv.room_pressure"].confidence == 0.7
    assert "section_heuristic" in got["acmv.room_pressure"].method


def test_fuzzy_label_is_a_fallback_at_point_four(tmp_path: Path, layouts: Dict[str, Any]) -> None:
    room = rp.parse_pdf(build_syn_a(tmp_path / "a.pdf", occupancy_label="Ocupancy"), layouts).rooms[0]
    f = by_key(room)["occupancy"]
    assert f.value == "3"
    assert f.confidence == 0.4
    assert f.method.startswith("fuzzy_label")
    assert f.label == "Ocupancy", "the label is reported as printed, not as the template spells it"


def test_header_region_is_not_label_anchored(tmp_path: Path, layouts: Dict[str, Any]) -> None:
    room = rp.parse_pdf(build_syn_a(tmp_path / "a.pdf"), layouts).rooms[0]
    got = by_key(room)
    assert got["room_code"].value == "TR-001" and got["room_code"].confidence == 0.7
    assert got["room_name"].method == "header_region"


# --------------------------------------------------------------------------- #
# Unmatched labels are reported, not guessed
# --------------------------------------------------------------------------- #
def test_unknown_label_is_reported_and_not_interpreted(tmp_path: Path, layouts: Dict[str, Any]) -> None:
    room = rp.parse_pdf(build_syn_a(tmp_path / "a.pdf", extra_label=True), layouts).rooms[0]
    unmatched = [u for u in room.unmatched if u.kind == "unmatched_label"]
    assert [u.label for u in unmatched] == ["Special Notes"]
    assert unmatched[0].bbox is not None and unmatched[0].page == 1
    assert all("Bring the key" not in f.value for f in room.fields), "the unknown label's value must not be read into any field"
    assert all(f.label != "Special Notes" for f in room.fields)
    # everything the template knows is still found
    assert room.found == room.expected


def test_missing_template_label_is_reported(tmp_path: Path, layouts: Dict[str, Any]) -> None:
    room = rp.parse_pdf(build_syn_a(tmp_path / "a.pdf", drop_occupancy=True), layouts).rooms[0]
    missing = [u for u in room.unmatched if u.kind == "missing_label"]
    assert [u.label for u in missing] == ["Occupancy"]
    assert "occupancy" not in by_key(room)
    assert room.found == room.expected - 1


def test_page_matching_no_template_is_unknown(tmp_path: Path, layouts: Dict[str, Any]) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    _header(page, "ZZ-9", "Strange page")
    page.insert_text((40, 120), "Totally Different", fontsize=8, fontname="hebo")
    page.insert_text((150, 120), "value", fontsize=8, fontname="helv")
    pdf = tmp_path / "x.pdf"
    doc.save(str(pdf))
    doc.close()
    fr = rp.parse_pdf(pdf, layouts)
    pr = fr.pages[0]
    assert pr.layout_id == "UNKNOWN"
    assert pr.fields == []
    assert [u.kind for u in pr.unmatched] == ["unknown_page"] and pr.unmatched[0].label == "Totally Different"


# --------------------------------------------------------------------------- #
# Layout SYN-B and multi-layout PDFs
# --------------------------------------------------------------------------- #
def test_below_rule_second_layout(tmp_path: Path, layouts: Dict[str, Any]) -> None:
    fr = rp.parse_pdf(build_syn_b(tmp_path / "b.pdf"), layouts)
    room = fr.rooms[0]
    got = by_key(room)
    assert fr.pages[0].layout_id == "SYN-B"
    assert got["room_function"].value == "Cleanroom ISO 7"
    assert got["design_notes"].value == "Positive pressure, HEPA filtered supply"
    for key in ("room_function", "design_notes"):
        assert got[key].confidence == 1.0 and got[key].method == "label+below_region_rule"
        assert got[key].value_bbox[1] >= got[key].label_bbox[3] - 0.5
    assert got["design_notes"].value_bbox[3] <= 190
    # without the rules the same value is read from the next label's position at 0.7
    room2 = rp.parse_pdf(build_syn_b(tmp_path / "b2.pdf", rules=False), layouts).rooms[0]
    got2 = by_key(room2)
    assert got2["room_function"].value == "Cleanroom ISO 7" and got2["room_function"].confidence == 0.7
    assert got2["design_notes"].value == "Positive pressure, HEPA filtered supply"


def test_two_layouts_in_one_pdf_make_two_rooms(tmp_path: Path, layouts: Dict[str, Any]) -> None:
    a = build_syn_a(tmp_path / "a.pdf")
    b = build_syn_b(tmp_path / "b.pdf")
    merged = pymupdf.open(str(a))
    merged.insert_pdf(pymupdf.open(str(b)))
    pdf = tmp_path / "ab.pdf"
    merged.save(str(pdf))
    fr = rp.parse_pdf(pdf, layouts)
    assert [r.code for r in fr.rooms] == ["TR-001", "TR-002"]
    assert [p.layout_id for p in fr.pages] == ["SYN-A", "SYN-B"]
    assert by_key(fr.rooms[1])["room_function"].value == "Cleanroom ISO 7"


# --------------------------------------------------------------------------- #
# Reviewer markup stays out of the printed text
# --------------------------------------------------------------------------- #
def test_annotation_does_not_contaminate_printed_value(tmp_path: Path, layouts: Dict[str, Any]) -> None:
    pdf = build_syn_a(tmp_path / "a.pdf", annot=True)
    before = pdf.read_bytes()
    fr = rp.parse_pdf(pdf, layouts)
    assert pdf.read_bytes() == before, "the source file is never written"
    got = by_key(fr.rooms[0])
    assert got["issue_date"].value == "JAN 2026"
    assert len(fr.annotations) == 1
    note = fr.annotations[0]
    assert note["type"] == "FreeText" and note["content"] == "FEB 2026" and note["page"] == 1
    assert note["overlaps_field"] == "issue_date"


# --------------------------------------------------------------------------- #
# Batch run and the registers
# --------------------------------------------------------------------------- #
def test_run_writes_registers(tmp_path: Path) -> None:
    src = tmp_path / "source"
    rel_a = "DD Sign-OFF/30 - Pharmacy/RDS/a_RDS.pdf"
    rel_b = "DD Sign-OFF/30 - Pharmacy/RDS/b_RDS.pdf"
    rel_c = "DD Sign-OFF/30 - Pharmacy/RDS/c_RDS.msg"
    rel_x = "DD Sign-OFF/99 - Other/RDS/x_RDS.pdf"
    for rel in (rel_a, rel_b, rel_x):
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
    build_syn_a(src / rel_a)
    build_syn_b(src / rel_b)
    build_syn_a(src / rel_x)
    (src / rel_c).write_bytes(b"not a pdf")
    manifest = tmp_path / "manifest.csv"
    cols = ["drive_id", "rel_path", "root", "dept_no", "dept_name", "doc_class", "pulled", "sha256"]
    with manifest.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=cols)
        writer.writeheader()
        for rel, dept, cls, pulled in ((rel_a, "30", "RDS", "y"), (rel_b, "30", "RDS", "y"), (rel_c, "30", "RDS", "y"),
                                       (rel_x, "99", "RDS", "y"), ("DD Sign-OFF/30 - Pharmacy/RDS/n.pdf", "30", "RDS", "n")):
            writer.writerow({"drive_id": "id", "rel_path": rel, "root": "DD Sign-OFF", "dept_no": dept, "dept_name": "Pharmacy",
                             "doc_class": cls, "pulled": pulled, "sha256": "abc"})
    layouts_path = tmp_path / "layouts.json"
    layouts_path.write_text(json.dumps(template()), encoding="utf-8")
    out = tmp_path / "registers"
    summary = rp.run(manifest, depts=["30"], layouts_path=layouts_path, out_dir=out, source_root=src)
    assert summary["files"] == 3 and summary["parsed"] == 2          # dept 99 and the unpulled row are not selected
    fields = list(csv.DictReader((out / "rds_fields.csv").open(encoding="utf-8")))
    assert list(fields[0].keys())[:11] == ["rel_path", "dept_no", "page", "layout_id", "field_label_as_printed", "field_key",
                                           "value_as_printed", "value_bbox", "label_bbox", "confidence", "method"]
    row = next(r for r in fields if r["rel_path"] == rel_a and r["field_key"] == "area")
    assert row["value_as_printed"] == "12.50 m2" and row["confidence"] == "1.0" and row["layout_id"] == "SYN-A"
    x0, y0, x1, y1 = (float(v) for v in row["value_bbox"].split(","))
    assert x1 > x0 and y1 > y0 and row["sha256"] == "abc"
    summ = {r["rel_path"]: r for r in csv.DictReader((out / "rds_summary.csv").open(encoding="utf-8"))}
    assert summ[rel_a]["area_m2"] == "12.50" and summ[rel_a]["room_code"] == "TR-001" and summ[rel_a]["department"] == "Pharmacy"  # no header tag in this template: manifest name
    assert summ[rel_a]["fields_found"] == summ[rel_a]["fields_expected"] == "11"
    assert summ[rel_c]["status"] == "skipped_not_pdf"
    layouts_rows = {r["layout_id"]: r for r in csv.DictReader((out / "rds_layouts.csv").open(encoding="utf-8"))}
    assert layouts_rows["SYN-A"]["files"] == rel_a and layouts_rows["SYN-B"]["files"] == rel_b
    assert "Revision" in layouts_rows["SYN-A"]["label_list"] and "Room Function" in layouts_rows["SYN-B"]["label_list"]


# --------------------------------------------------------------------------- #
# The shipped template
# --------------------------------------------------------------------------- #
def test_shipped_templates_are_consistent() -> None:
    data = rp.load_layouts()
    ids = {lay["id"] for lay in data["layouts"]}
    for fam in data["families"]:
        assert set(fam["layouts"]) <= ids
    for lay in data["layouts"]:
        assert lay["match"]["anchors"] and 0 < lay["match"]["min_fraction"] <= 1
        for spec in lay.get("fields", []):
            assert spec["labels"] and spec["rule"] in ("row_band", "below")
        for tbl in lay.get("tables", []):
            assert tbl.get("columns") or tbl.get("columns_ref") in data["column_sets"]
    assert rp.snake("Auto / Manual") == "auto_manual"


def test_quantity_summary_cross_check() -> None:
    """The summary page must equal the per-code quantities summed over the component tables."""
    def cell(section: str, row: str, col: str, value: str) -> rp.FieldRec:
        return rp.FieldRec(1, "X", col, "%s.%s" % (section, col), value, None, None, 1.0, "t", section=section, row=row)

    room = rp.Room(index=1)
    room.fields = [
        cell("engineering_services", "1", "code", "ELBO-020"), cell("engineering_services", "1", "qty", "2"),
        cell("medical_equipment", "1", "code", "ELBO-020"), cell("medical_equipment", "1", "qty", "1"),   # sub item counts too
        cell("engineering_services", "2", "code", "LIFX-029"), cell("engineering_services", "2", "qty", "2"),
        cell("quantity_summary", "1", "code", "ELBO-020"), cell("quantity_summary", "1", "total_room_qty", "3"),
        cell("quantity_summary", "2", "code", "LIFX-029"), cell("quantity_summary", "2", "total_room_qty", "5"),   # wrong on purpose
    ]
    assert rp.quantity_check(room) == (1, 2)
