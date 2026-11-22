"""assemble_department: registers to department input, with and without zoning, unmatched rooms flagged."""

from __future__ import annotations

import csv
import os
from pathlib import Path

import pytest

from src.workbook.assemble import assemble_department
from src.workbook.build import build_workbook

ROOT = Path(__file__).resolve().parents[1]


def _write(path: Path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


@pytest.fixture()
def fake_root(tmp_path):
    os.symlink(ROOT / "data", tmp_path / "data")
    reg = tmp_path / "out" / "registers"
    soa_f = ["dept_no", "department_folder", "source_file", "issue_folder", "issue_date", "soa_version", "page_or_sheet_row", "room_code", "room_name",
             "qty", "unit_area_m2", "designed_area_m2", "as_drawn_area_m2", "raw_cells", "bbox_pt", "method", "confidence", "governing"]
    _write(reg / "soa_rooms.csv", [
        dict(dept_no="77", department_folder="77 - Test Dept", source_file="soa.pdf", soa_version="V1", page_or_sheet_row="p1", room_code="TD 01.01.01",
             room_name="Office", qty="1", as_drawn_area_m2="20.0", raw_cells="{}", governing="yes"),
        dict(dept_no="77", department_folder="77 - Test Dept", source_file="soa.pdf", soa_version="V1", page_or_sheet_row="p1", room_code="TD 01.01.02",
             room_name="Store", qty="1", as_drawn_area_m2="10.0", raw_cells="{}", governing="yes"),
        dict(dept_no="77", department_folder="77 - Test Dept", source_file="soa.pdf", soa_version="V1", page_or_sheet_row="p1", room_code="TD 01.01.03",
             room_name="Ghost", qty="1", as_drawn_area_m2="5.0", raw_cells="{}", governing="yes"),
        dict(dept_no="78", department_folder="78 - Other", source_file="soa.pdf", soa_version="V1", page_or_sheet_row="p1", room_code="OT 1", room_name="Other",
             qty="1", as_drawn_area_m2="9", governing="yes"),
        dict(dept_no="77", department_folder="77 - Test Dept", source_file="soa_old.pdf", soa_version="V0", page_or_sheet_row="p1", room_code="TD 00.00.01",
             room_name="Old room", qty="1", as_drawn_area_m2="3", governing="no")], soa_f)
    _write(reg / "dpa_rooms.csv", [], ["row", "unique_room_identifier", "source_file", "designed_area_m2", "programmed_area_m2", "storey_from_identifier"])
    cr_f = ["department", "dept_no", "department_label", "source_file", "booklet_page", "room_type_as_printed", "pressure_regime_as_printed", "pressure_pa",
            "min_oa_ach", "min_total_ach", "single_pass", "air_class", "filter", "rh_max_pct", "temp_min_c", "temp_max_c", "afterhours", "bbox", "confidence", "raw_cells"]
    _write(reg / "criteria.csv", [dict(dept_no="77", department_label="TD", source_file="booklet.pdf", booklet_page="9", room_type_as_printed="Office",
                                       pressure_regime_as_printed="NEUTRAL", min_oa_ach="2", min_total_ach="6", single_pass="NA", air_class="2", filter="MERV 8",
                                       rh_max_pct="60", temp_min_c="23", temp_max_c="25", bbox="1,2,3,4", confidence="0.9", raw_cells="{}"),
                                  dict(dept_no="77", department_label="TD", source_file="booklet.pdf", booklet_page="9", room_type_as_printed="Store",
                                       pressure_regime_as_printed="SLIGHTLY -VE", pressure_pa="-5", min_oa_ach="0", min_total_ach="4")], cr_f)
    poly_f = ["room_id", "storey", "room_tag", "room_number", "room_name", "a_area_m2", "b_area_m2", "method_agreement", "a_perimeter_m"]
    _write(reg / "room_polygons_L01.csv", [dict(room_id="L01-R1", storey="L01", room_tag="OFFICE", room_name="Office", a_area_m2="20.4", a_perimeter_m="18.0"),
                                           dict(room_id="L01-R2", storey="L01", room_tag="STORE", room_name="Store", a_area_m2="10.1", a_perimeter_m="12.0")], poly_f)
    _write(reg / "schematic_rooms.csv", [], ["sheet", "ahu_tag", "room_name_as_written", "issue_set", "storey"])
    _write(reg / "schematic_equipment.csv", [], ["sheet", "tag", "issue_set"])
    return tmp_path


def test_without_zoning_ahu_blank_and_flagged(fake_root):
    d = assemble_department("77", None, fake_root)
    assert [r["number"] for r in d["rooms"]] == ["TD 01.01.01", "TD 01.01.02", "TD 01.01.03"]   # governing SOA only, other department excluded
    assert all(r["ahu"] is None for r in d["rooms"])
    flags = " ".join(f["text"] for f in d["flags"])
    assert "no AHU or system" in flags
    ghost = d["rooms"][2]
    assert ghost["measured_area_m2"] is None and ghost["storey"] == "NS"
    assert "Ghost" in flags and "not matched to a layout polygon" in flags       # listed, not guessed
    assert "no criteria row maps" in flags                                        # criteria unmapped listed


def test_matched_rooms_carry_provenance_and_measurements(fake_root):
    d = assemble_department("77", None, fake_root)
    o = d["rooms"][0]
    assert o["storey"] == "L01" and o["measured_area_m2"] == pytest.approx(20.4)
    assert o["prov"]["measured_area"][0] == "M"
    assert o["criteria"]["min_total_ach"] == 6 and o["criteria"]["pressure_regime"] == "NEUTRAL"
    ids = {s["id"] for s in d["sources"]}
    assert o["prov"]["soa_area"][1] in ids and o["criteria"]["source"] in ids
    store = d["rooms"][1]
    assert store["criteria"]["pressure_regime"] == "NEGATIVE" and store["criteria"]["pressure_pa_booklet"] == -5
    assert store["pressure"]["mode"] == "controlled" and store["pressure"]["doors"] == []
    assert any("doors.csv" in f["text"] for f in d["flags"])


def test_with_zone_assignment_gives_ahu(fake_root):
    _write(fake_root / "out" / "registers" / "zone_assignment_L01.csv",
           [dict(room_id="L01-R1", storey="L01", ahu="AHU-X-01", zone_tag="Z1", system_type="VAV", confidence="0.95"),
            dict(room_id="L01-R2", storey="L01", ahu="AHU-X-01", zone_tag="Z2")], ["room_id", "storey", "ahu", "zone_tag", "system_type", "confidence"])
    d = assemble_department("77", None, fake_root)
    assert d["rooms"][0]["ahu"] == "AHU-X-01" and d["rooms"][0]["zone_tag"] == "Z1"
    assert [a["tag"] for a in d["ahus"]] == ["AHU-X-01"]


def test_assembled_input_builds_and_recalculates_cleanly(fake_root, tmp_path):
    from src.workbook import selftest as st
    from src.workbook.recalc import recalc
    d = assemble_department("77", None, fake_root)
    build_workbook(d, tmp_path / "d77.xlsx")
    rc = recalc(tmp_path / "d77.xlsx", tmp_path / "rc")
    assert st.scan_errors(rc) == []


def test_real_registers_pharmacy_smoke(tmp_path):
    d = assemble_department("30 - Pharmacy", tmp_path / "d30.json", ROOT)
    assert len(d["rooms"]) == 79 and (tmp_path / "d30.json").exists()
    ctx = build_workbook(d, tmp_path / "d30.xlsx")
    assert len(ctx.rooms) == 79
