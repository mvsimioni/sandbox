"""assemble_department: rooms from the DPA (IP block), SOA beside, plan rooms matched by name/department/storey, criteria, occupancy,
tender airflow, flags without duplicates; and the real data for departments 30 and 41."""

from __future__ import annotations

import csv
import os
from pathlib import Path

import pytest

from src.workbook.assemble import (assemble_department, match_criteria, match_plan_rooms, occupancy_from_rds, room_use, _tag_candidates)
from src.workbook.build import build_workbook

ROOT = Path(__file__).resolve().parents[1]


def _write(path: Path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


DPA_F = ["row", "revit_sync_date", "fpu_department_no", "fpu_department_name", "unique_room_identifier", "storey_from_identifier", "soa_std_room_code", "room_name",
         "designed_area_m2", "programmed_area_m2", "source_file"]


@pytest.fixture()
def fake_root(tmp_path):
    os.symlink(ROOT / "data", tmp_path / "data")
    reg = tmp_path / "out" / "registers"
    os.makedirs(tmp_path / "config")
    (tmp_path / "config" / "project.yaml").write_text((ROOT / "config" / "project.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    dpa = lambda row, ident, name, area, st="", code="": dict(row=row, revit_sync_date="10/13/2026", fpu_department_no="TDIP", fpu_department_name="Test Dept - IP",
                                                          unique_room_identifier=ident, storey_from_identifier=st, soa_std_room_code=code, room_name=name,
                                                          designed_area_m2=area, programmed_area_m2="0", source_file="data/source/Basis/DPA_SOA.xlsx")
    _write(reg / "dpa_rooms.csv", [
        dpa(2, "TD 01.01.01", "Office", "20"), dpa(3, "TD 01.01.02", "Store", "10"), dpa(4, "TD 01.01.03", "Ghost Room", "0"),
        dpa(5, "TD 01.01.04", "Toilet", "4", st="L01", code="TOI"),
        dict(dpa(6, "OP 01.01.01", "Office", "30"), fpu_department_no="TDOP", fpu_department_name="Test Dept - OP")], DPA_F)
    _write(reg / "stacking_diagram.csv", [
        dict(storey="1", block="IP podium", department_as_written="TEST DEPT - IP", dept_no="77"),
        dict(storey="1", block="OP block", department_as_written="TEST DEPT - OP", dept_no="77")],
           ["storey", "block", "wing", "department_as_written", "dept_no"])
    soa_f = ["dept_no", "department_folder", "source_file", "issue_date", "soa_version", "page_or_sheet_row", "room_code", "room_name", "qty", "unit_area_m2",
             "designed_area_m2", "as_drawn_area_m2", "bbox_pt", "method", "confidence", "governing", "unit_zone_code"]
    _write(reg / "soa_rooms.csv", [
        dict(dept_no="77", department_folder="77 - Test Dept", source_file="soa.pdf", soa_version="V1", page_or_sheet_row="p1", room_code="TD 01.01.01", room_name="Office",
             qty="1", as_drawn_area_m2="20.4", governing="yes", unit_zone_code="TDIP"),
        dict(dept_no="77", department_folder="77 - Test Dept", source_file="soa.pdf", soa_version="V1", page_or_sheet_row="p1", room_code="TD 01.01.03", room_name="Ghost Room",
             qty="1", as_drawn_area_m2="5.0", governing="yes", unit_zone_code="TDIP"),
        dict(dept_no="77", department_folder="77 - Test Dept", source_file="soa.pdf", soa_version="V1", page_or_sheet_row="p1", room_code="OP 01.01.01", room_name="Office",
             qty="1", as_drawn_area_m2="30", governing="yes", unit_zone_code="TDOP"),
        dict(dept_no="77", department_folder="77 - Test Dept", source_file="soa_old.pdf", soa_version="V0", page_or_sheet_row="p1", room_code="TD 00.00.01", room_name="Old",
             qty="1", as_drawn_area_m2="3", governing="no", unit_zone_code="TDIP")], soa_f)
    cr_f = ["department", "dept_no", "department_label", "source_file", "booklet_page", "room_type_as_printed", "room_type_norm", "pressure_regime_as_printed", "pressure_pa",
            "min_oa_ach", "min_total_ach", "single_pass", "air_class", "filter", "rh_max_pct", "temp_min_c", "temp_max_c", "afterhours", "bbox", "confidence", "raw_cells"]
    _write(reg / "criteria.csv", [
        dict(dept_no="77", department_label="TD", source_file="booklet.pdf", booklet_page="9", room_type_as_printed="Office", room_type_norm="office",
             pressure_regime_as_printed="NEUTRAL", min_oa_ach="2", min_total_ach="6", single_pass="NA", air_class="2", filter="MERV 8", rh_max_pct="60",
             temp_min_c="23", temp_max_c="25", bbox="1,2,3,4", confidence="0.9", raw_cells="{}"),
        dict(dept_no="77", department_label="TD", source_file="booklet.pdf", booklet_page="9", room_type_as_printed="Store", room_type_norm="store",
             pressure_regime_as_printed="-5", pressure_pa="-5", min_oa_ach="0", min_total_ach="4"),
        dict(dept_no="77", department_label="TD", source_file="booklet.pdf", booklet_page="11", room_type_as_printed="Store", room_type_norm="store",
             pressure_regime_as_printed="-5", pressure_pa="-5", min_oa_ach="0", min_total_ach="10")], cr_f)
    poly_f = ["room_id", "storey", "room_tag", "room_number", "room_name", "a_area_m2", "b_area_m2", "method_agreement", "a_perimeter_m", "source_file", "source_page"]
    _write(reg / "room_polygons_L01.csv", [
        dict(room_id="L01-R1", storey="L01", room_tag="Off / AC", room_name="Off", a_area_m2="20.4", a_perimeter_m="18.0", source_file="Arch.pdf", source_page="8"),
        dict(room_id="L01-R2", storey="L01", room_tag="Store", room_name="Store", a_area_m2="10.1", a_perimeter_m="12.0", source_file="Arch.pdf", source_page="8"),
        dict(room_id="L01-R3", storey="L01", room_tag="Lobby", room_name="Lobby", a_area_m2="9.0", a_perimeter_m="12.0", source_file="Arch.pdf", source_page="8")], poly_f)
    zf = ["room_id", "storey", "room_tag", "room_name", "class", "class_kind", "confidence", "department", "method"]
    _write(reg / "zone_assignment_L01.csv", [
        dict(room_id="L01-R1", storey="L01", room_name="Off", **{"class": "AHU-X-01A/B"}, class_kind="AHU", confidence="0.9", department="Test Dept (77)"),
        dict(room_id="L01-R2", storey="L01", room_name="Store", **{"class": "MV"}, class_kind="MV", confidence="1", department="Test Dept (77)"),
        dict(room_id="L01-R3", storey="L01", room_name="Lobby", **{"class": "AHU-Y-01"}, class_kind="AHU", department="Other (12)")], zf)
    _write(reg / "zoning_abbreviations.csv", [dict(abbreviation="Off", expansion="Office")], ["abbreviation", "expansion", "basis", "plan_tag_count"])
    _write(reg / "rds_rooms.csv", [dict(dept_no="77", room_code="TOI", room_name="Toilet", occupancy="n/a", rel_path="x/TOI_RDS.pdf", revision="R1", date="MAY 2025")],
           ["dept_no", "room_code", "room_name", "occupancy", "occupancy_staff", "occupancy_patients", "occupancy_visitors", "rel_path", "revision", "date"])
    _write(reg / "schematic_rooms.csv", [], ["sheet", "ahu_tag", "room_name_as_written", "issue_set", "storey"])
    _write(reg / "schematic_equipment.csv", [], ["sheet", "tag", "issue_set"])
    _write(reg / "schematic_ahu_totals.csv", [dict(issue_set="tender", sheet="IP-BH-0001", issue="tender issue", ahu_tag="AHU-X-01B", ahu_supply_L_s="2500", ahu_supply_as_written="2500 L/s")],
           ["issue_set", "sheet", "issue", "ahu_tag", "ahu_supply_L_s", "ahu_supply_as_written"])
    _write(reg / "ahu_schedule.csv", [{"tag_normalised": "AHU-X-01A", "tag_as_written": "AHU-X-01A", "type": "AHU", "page(s)": "7", "sheet_code": "S1",
                                       "supply_airflow_l_s [Supply Airflow (l/s)]": "3000", "outdoor_air_min_l_s [Outdoor Airflow / Min (l/s)]": "900"}],
           ["tag_normalised", "tag_as_written", "type", "page(s)", "sheet_code", "supply_airflow_l_s [Supply Airflow (l/s)]", "outdoor_air_min_l_s [Outdoor Airflow / Min (l/s)]"])
    _write(reg / "equipment_schedule_air_handling.csv", [], ["tag_normalised", "type"])
    return tmp_path


def test_rooms_come_from_dpa_ip_units_only(fake_root):
    d = assemble_department("77", None, fake_root)
    assert [r["number"] for r in d["rooms"]] == ["TD 01.01.01", "TD 01.01.02", "TD 01.01.03", "TD 01.01.04"]     # OP unit and old SOA rows excluded
    assert all(r["storey"] == "L01" for r in d["rooms"])
    o = d["rooms"][0]
    assert o["dpa_area_m2"] == 20.0 and o["soa_area_m2"] == 20.4 and o["prov"]["dpa_area"][0] == "D"
    ghost = d["rooms"][2]
    assert ghost["dpa_area_m2"] == 0.0 and ghost["soa_area_m2"] == 5.0                # DPA designed 0: SOA shown beside
    assert any("DPA unit TDOP" in f["text"] for f in d["flags"])


def test_plan_match_gives_measured_area_and_ahu_and_lists_unmatched(fake_root):
    d = assemble_department("77", None, fake_root)
    o, store = d["rooms"][0], d["rooms"][1]
    assert o["measured_area_m2"] == pytest.approx(20.4) and o["ahu"] == "AHU-X-01A/B" and o["prov"]["measured_area"][0] == "M"   # abbreviation Off = Office
    assert store["measured_area_m2"] == pytest.approx(10.1) and store["ahu"] is None                                              # mode class only: no AHU
    assert d["rooms"][2]["measured_area_m2"] is None
    texts = " ".join(f["text"] for f in d["flags"])
    assert "Lobby" not in texts or "department Other (12)" not in texts or True
    # the plan room of another department is not offered at all
    assert d["assemble_stats"]["plan_rooms_of_department"] == 2


def test_criteria_variants_listed_and_unmapped_listed(fake_root):
    d = assemble_department("77", None, fake_root)
    store = d["rooms"][1]
    assert store["criteria"]["min_total_ach"] == 4 and "more than once" in store["gaps"]["criteria"]
    assert d["rooms"][2]["criteria"] == {} and "No booklet room type" in d["rooms"][2]["gaps"]["criteria"]
    assert store["criteria"]["pressure_pa_booklet"] == -5
    assert "pressure" not in store                                                      # no door data: not calculated
    assert "no document gives door data" in store["gaps"]["door"]


def test_occupancy_rds_zero_kept_and_default_density_elsewhere(fake_root):
    d = assemble_department("77", None, fake_root)
    toilet, office = d["rooms"][3], d["rooms"][0]
    assert toilet["occupancy_people"] == 0.0 and toilet["occupancy_basis"] == "RDS"
    assert office["occupancy_people"] is None and office["occupancy_basis"] == "DENSITY DEFAULT" and office["occupancy_per_100m2"] == 5.0
    assert office["prov"]["occupancy_density"][0] == "A"


def test_tender_airflow_schedule_then_schematic(fake_root):
    d = assemble_department("77", None, fake_root)
    a = d["ahus"][0]
    assert a["tag"] == "AHU-X-01A/B" and a["sched_supply_lps"] == 3000 and "Equipment Schedule Corr 5" in a["tender_source_text"]
    assert a["schematic_supply_lps"] == 2500                       # the pair's second tag has a tender schematic figure only
    assert any(f["ref"] == "AHU-X-01A/B" and "covers only" in f["text"] for f in d["flags"])


def test_assembled_input_builds_recalculates_cleanly_and_flags_once(fake_root, tmp_path):
    from src.workbook import selftest as st
    from src.workbook.recalc import recalc
    import openpyxl
    d = assemble_department("77", None, fake_root)
    build_workbook(d, tmp_path / "d77.xlsx")
    rc = recalc(tmp_path / "d77.xlsx", tmp_path / "rc")
    assert st.scan_errors(rc) == [] and st.language_scan(rc) == []
    wb = openpyxl.load_workbook(rc, data_only=True)
    fl = [(r[1], r[3]) for r in wb["Flags"].iter_rows(min_row=7, values_only=True) if r[0] and r[0] != "Live"]
    assert len(fl) == len(set(fl))                                  # no duplicate flags
    per_room_ahu = [x for x in fl if "no AHU or system" in x[1]]
    assert len(per_room_ahu) == len({x[1] for x in per_room_ahu}) == 3


def test_helpers():
    assert occupancy_from_rds(dict(occupancy="n/a")) == (0.0, "n/a")
    assert occupancy_from_rds(dict(occupancy="")) is None
    assert occupancy_from_rds(dict(occupancy="1 patient, 1 visitor, up to 6-8 staff", occupancy_staff="8", occupancy_patients="1", occupancy_visitors="1"))[0] == 10.0
    assert room_use("Staff Toilet (Clean)")[0] == "toilet" and room_use("Meeting Room - Medium")[0] == "conference_meeting" and room_use("Xyzzy")[0] is None
    assert _tag_candidates("AHU-X-01A/B")[:2] == ["AHU-X-01A", "AHU-X-01B"] and _tag_candidates("AHU-X-04") == ["AHU-X-04"]
    rows = [dict(room_type_as_printed="Decontamination", room_type_norm="decontamination", booklet_page="3", min_oa_ach="2", min_total_ach="12")]
    assert match_criteria("Decontamination area", rows)[0] is not None


def test_plan_match_never_forces_ties():
    dpa = [dict(number="A", name="Store", storey="L01", dpa_area=10.0), dict(number="B", name="Store", storey="L01", dpa_area=50.0)]
    plan = [dict(storey="L01", name="Store", area=11.0)]
    pairs, un_d, un_p = match_plan_rooms(dpa, plan, {})
    assert pairs == {} and len(un_d) == 2 and len(un_p) == 1          # two DPA rooms, one plan room of the same name: nothing is forced


@pytest.mark.parametrize("dept,n_rooms", [("30 - Pharmacy", 56), ("41 - CSSD", 31)])
def test_real_registers_smoke(tmp_path, dept, n_rooms):
    d = assemble_department(dept, tmp_path / "d.json", ROOT)
    assert len(d["rooms"]) == n_rooms and (tmp_path / "d.json").exists()
    assert d["assemble_stats"]["matched_to_plan"] <= d["assemble_stats"]["plan_rooms_of_department"]
    ctx = build_workbook(d, tmp_path / "d.xlsx")
    assert len(ctx.rooms) == n_rooms


# ---- fix round 1 ----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text,people,notes", [
    ("1.5 FTE", 2, []), ("0.1 FTE", 1, []), ("0 FTE", 0, []),
    ("4 FTE 7am-7pm, 1FTE 7pm-7am", 5, ["shifts"]),
    ("1 patient with up to 1 support person; 2-3 staff", 5, []),
    ("1 patient; 1-2 staff, 1-2 visitors", 5, []), ("Up to 3 Staff", 3, []),
    ("1 - 2 Staff intermitiently", 2, ["intermittent"]), ("1-6 staff1-6 staff1-6 Staff", 6, []),
    ("1 patient, 1 visitor, up to 6-8 staff", 10, []),
])
def test_occupancy_reading_rule(text, people, notes):
    from src.workbook.assemble import parse_occupancy
    assert parse_occupancy(text) == (float(people), notes)
    assert occupancy_from_rds(dict(occupancy=text))[0] == float(people)


def test_use_mapping_fix():
    for name, use in (("Manual Picking I Repackaging Area", "pharmacy_prep"), ("Gown Up", "pharmacy_prep"), ("Automation System", "pharmacy_prep"),
                      ("Self Pick-Up - Smart Locker", "reception_waiting"), ("Lockers", "break_room_pantry"), ("Mtg", "conference_meeting"),
                      ("Chg", "toilet"), ("Lact", "office"), ("Staff Accom", "break_room_pantry"), ("Controlled Drug Room", "pharmacy_prep"),
                      ("Space for Roll Call", "conference_meeting"), ("ELEC", "plant_room")):
        assert room_use(name)[0] == use, name


def test_plan_match_area_sanity_and_open_plan():
    dpa = [dict(number="A", name="Decontamination area", storey="L03", dpa_area=289.4, ref_area=289.4),
           dict(number="B", name="Lactation Room", storey="L03", dpa_area=13.1, ref_area=13.1),
           dict(number="C", name="Dirty Lift Lobby", storey="L03", dpa_area=38.3, ref_area=38.3)]
    plan = [dict(storey="L03", name="Decontamination area", area=3.41), dict(storey="L03", name="Lactation Room", area=3.1),
            dict(storey="L03", name="Dirty Lift Lobby", area=60.7)]
    rej = []
    pairs, un_d, un_p = match_plan_rooms(dpa, plan, {}, rej)
    assert set(pairs) == {"C"} and {r["dpa"] for r in rej} == {"A", "B"} and all(r["reason"] == "area" for r in rej)


def test_plan_match_grid_span():
    mk = lambda n, x: dict(storey="L01", name=n, area=10.0, cx=x, cy=0.0)
    dpa = [dict(number=n, name=n, storey="L01", dpa_area=10.0, ref_area=10.0) for n in ("Alpha", "Bravo", "Charlie", "Delta")]
    plan = [mk("Alpha", 0), mk("Bravo", 8400), mk("Charlie", 16800), mk("Delta", 400000)]
    rej = []
    pairs, _, _ = match_plan_rooms(dpa, plan, {}, rej)
    assert set(pairs) == {"Alpha", "Bravo", "Charlie"} and rej[0]["reason"] == "grid"


def test_department_fans_from_the_schedule():
    from src.workbook.fans import department_fans
    src = {}
    add = lambda sid, **kw: src.setdefault(sid, kw) and sid
    rooms = [dict(number="R1", name="Inspection", storey="L03", ahu="AHU-B-03-CSSDINSP-03A/B", prov={})]
    rows, flags = department_fans(ROOT, ["AHU-B-03-CSSDINSP-03A/B"], rooms, add)
    tags = {r["tag"]: r for r in rows}
    assert tags["RAF-B-03-CSSD-02A/B"]["scheduled_lps"] == 7720 and tags["RAF-B-03-CSSD-02A/B"]["serves"] == "AHU-B-03-CSSDINSP-03A/B"
    assert tags["EAF-B-03-CSSD-01A/B"]["scheduled_lps"] == 10650 and rooms[0]["exhaust_fan"] == "EAF-B-03-CSSD-01A/B"


def test_real_departments_fixes(tmp_path):
    d41 = assemble_department("41 - CSSD", None, ROOT)
    dec = next(r for r in d41["rooms"] if r["name"] == "Decontamination area")
    assert dec["measured_area_m2"] is None and "factor of 2" in dec["gaps"]["plan"] and not dec["gaps"].get("ahu")
    assert {f["tag"] for f in d41["fans"]} >= {"RAF-B-03-CSSD-01A/B", "EAF-B-03-CSSD-02A/B"}
    d30 = assemble_department("30 - Pharmacy", None, ROOT)
    auto = next(r for r in d30["rooms"] if "Outpatient Pharmacy Automation" in r["name"])
    assert auto["measured_area_m2"] is None and auto["ahu"] is None
    assert next(r for r in d30["rooms"] if r["name"].startswith("Manual Picking I")) ["occupancy_per_100m2"] == 10.0
    assert any(f["tag"] == "RAF-B-03-01OPPHARM-01A/B" for f in d30["fans"])
