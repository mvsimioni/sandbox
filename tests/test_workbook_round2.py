"""Generator round 2: tender columns (D-013), default occupancy (D-017), language scan (D-015), master summary, common areas (D-011)."""

from __future__ import annotations

import json
from pathlib import Path

import openpyxl
import pytest

from src.workbook import selftest as st
from src.workbook.build import build_workbook
from src.workbook.master import build_master
from src.workbook.recalc import recalc
from src.workbook.reva import build_reva_input

ROOT = Path(__file__).resolve().parents[1]


def test_tender_columns_reduction_and_remarks(tmp_path):
    res = st.d013_check(build_reva_input(ROOT), tmp_path)
    assert res["fails"] == [] and res["errors"] == 0
    assert res["ahu_remarks"].startswith("Reduced 3,500 to 3,036 L/s") and "against tender" in res["ahu_remarks"]


def test_calculated_above_tender_is_verify_not_fail_and_tender_never_caps(tmp_path):
    d = build_reva_input(ROOT)
    tag = d["rooms"][0]["ahu"]
    d["ahus"] = [dict(tag=tag, type="AHU", sched_supply_lps=1000.0, sched_code="D", sched_source="RA-AHU-REF", tender_source_text="tender equipment schedule, test")]
    build_workbook(d, tmp_path / "a.xlsx")
    rc = recalc(tmp_path / "a.xlsx", tmp_path / "rc")
    wb = openpyxl.load_workbook(rc, data_only=True)
    from src.workbook.layouts import AHU_KEYS
    row = [r for r in wb["AHU"].iter_rows(min_row=7, values_only=True) if r[0] == tag][0]
    k = lambda key: row[AHU_KEYS.index(key)]
    assert k("sa_ahu") == pytest.approx(3036.0) and k("red") == pytest.approx(1000.0 - 3036.0) and k("redp") == pytest.approx((1000 - 3036) / 1000)
    assert k("rem").startswith("VERIFY: calculated 3,036 L/s is above tender 1,000 L/s")
    assert [r for r in wb["Checks"].iter_rows(min_row=7, values_only=True) if r[1] and r[4] == "FAIL"] == []
    cp = [r for r in wb["Airflow Comparison"].iter_rows(min_row=7, values_only=True) if r[0] == tag]
    assert cp and any(isinstance(x, str) and x.startswith("VERIFY: ABOVE TENDER") for x in cp[0])


def test_default_occupancy_density_rounded_up_red_with_note(tmp_path):
    res = st.occupancy_check(build_reva_input(ROOT), tmp_path)
    assert res["fails"] == [] and res["people"] == res["expected"]
    assert res["note"].startswith("Occupancy assumed: 7 p/100 m² x") and "To confirm." in res["note"]


def test_language_scan_finds_forbidden_terms_and_internal_references(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Notes"
    ws["A1"] = "Checked by Claude with a pipeline"
    ws["A2"] = '="See the register "&"model"'
    ws["A3"] = "Air handling unit AIR flow; see out/qa/x.md and Section 8.3"
    ws["A4"] = "Plain text about the room."
    wb.save(tmp_path / "t.xlsx")
    hits = st.language_scan(tmp_path / "t.xlsx")
    text = " ".join(h[2] for h in hits)
    for term in ("'Claude'", "'pipeline'", "'register'", "'model'", "internal reference 'out/qa'", "'Section 8.3'"):
        assert term in text, term
    assert not any(h[1] == "A4" for h in hits) and not any("'AI'" in h[2] for h in hits)


def test_no_author_and_build_date_revision(tmp_path):
    ctx = build_workbook(build_reva_input(ROOT), tmp_path / "r.xlsx")
    wb = openpyxl.load_workbook(tmp_path / "r.xlsx")
    assert (wb.properties.creator or "") == "" and (wb.properties.lastModifiedBy or "") == ""
    rev = ctx.data["project"]["revision"]
    import datetime
    assert rev["date"] == datetime.date.today().isoformat() and rev["by"] == "" and rev["checked"] == "" and rev["approved"] == ""
    import yaml
    people = yaml.safe_load((ROOT / "config" / "project.yaml").read_text())["workbook"]["people"]
    assert all(v == "" for v in people.values())


def test_master_counts_each_ahu_once(tmp_path):
    ind = tmp_path / "in"
    ind.mkdir()
    for num, name, supply, tender in (("30", "Pharmacy", 1000.0, 5000.0), ("41", "CSSD", 500.0, 5000.0)):
        (ind / f"d{num}.json").write_text(json.dumps(dict(department=dict(number=num, name=name), ahus=[
            dict(tag="AHU-SHARED-01", type="AHU", rooms=3, area_m2=100.0, room_supply_lps=supply, outdoor_air_lps=100.0, tender_lps=tender,
                 tender_source="Equipment Schedule Corr 5, page 7, AHU-SHARED-01", tender_source_id="EQS5-SHARED"),
            dict(tag=f"AHU-ONLY-{num}", type="AHU", rooms=1, area_m2=10.0, room_supply_lps=50.0, outdoor_air_lps=5.0, tender_lps=None)])))
    res = build_master(ind, tmp_path / "AHU_master_summary.xlsx", ROOT)
    assert res["departments"] == 2 and res["ahus"] == 3
    rc = recalc(tmp_path / "AHU_master_summary.xlsx", tmp_path / "rc")
    assert st.scan_errors(rc) == [] and st.language_scan(rc) == []
    wb = openpyxl.load_workbook(rc, data_only=True)
    rows = {r[0]: r for r in wb["AHU Master"].iter_rows(min_row=7, values_only=True) if r[0] and r[0] != "TOTAL"}
    assert set(rows) == {"AHU-SHARED-01", "AHU-ONLY-30", "AHU-ONLY-41"}
    sh = rows["AHU-SHARED-01"]
    assert sh[2] == 2 and sh[3] == 6 and sh[6] == pytest.approx(1500.0) and sh[8] == pytest.approx(1650.0)        # departments, rooms, room supply, with 10 % leakage
    assert sh[10] == 5000 and sh[14] == pytest.approx(5000 - 1650) and sh[15] == pytest.approx((5000 - 1650) / 5000)
    assert sh[17].startswith("Reduced 5,000 to 1,650 L/s")
    assert rows["AHU-ONLY-30"][16] == "NO TENDER VALUE"
    fs = st.scan_formulas(tmp_path / "AHU_master_summary.xlsx")
    assert not fs["bad_funcs"] and not fs["literals"] and not fs["iferror"]


def test_master_reads_department_workbooks(tmp_path):
    d = build_reva_input(ROOT)
    tag = d["rooms"][0]["ahu"]
    d["ahus"] = [dict(tag=tag, type="AHU", sched_supply_lps=3500.0, sched_code="D", sched_source="RA-AHU-REF", tender_source_text="tender equipment schedule, test")]
    ind = tmp_path / "in"
    ind.mkdir()
    build_workbook(d, ind / "dept.xlsx")                                  # no stored values: the reader recalculates
    res = build_master(ind, tmp_path / "m.xlsx", ROOT)
    rc = recalc(tmp_path / "m.xlsx", tmp_path / "rc")
    wb = openpyxl.load_workbook(rc, data_only=True)
    row = [r for r in wb["AHU Master"].iter_rows(min_row=7, values_only=True) if r[0] == tag][0]
    assert row[8] == pytest.approx(3036.0) and row[10] == 3500 and res["ahus"] == 1


def test_common_areas_assembly_uses_default_criteria_and_lists_units(tmp_path):
    from src.workbook.common_areas import assemble_common_areas, space_name, unit_tags
    d = assemble_common_areas(ROOT)
    s = d["assemble_stats"]
    assert s["rooms"] > 300 and s["units"] > 50 and s["units_with_tender"] > 0
    # D-011: the default only for circulation, corridor and lobby spaces; a booklet row for a space whose room type a booklet prints; none otherwise
    circ = [r for r in d["rooms"] if r["criteria"] and r["criteria"]["criteria_row"] == "Circulation default (assumed)"]
    assert circ and all(r["criteria"]["source"] == "CIRC-DEFAULT" and r["criteria"]["min_total_ach"] == 6 and r["criteria"]["min_oa_ach"] == 2 for r in circ)
    assert all(r["criteria"]["code"] == "A" for r in d["rooms"] if r["criteria"])
    toilets = [r for r in d["rooms"] if r["name"] == "Toilet"]
    assert toilets and all(r["criteria"] and r["criteria"]["criteria_row"] == "Toilet" and r["criteria"].get("exhaust_ach") for r in toilets)
    assert not any(r["criteria"] for r in d["rooms"] if r["name"] in ("ELEC", "Batt", "Common space"))
    assert sum(1 for r in d["rooms"] if r["criteria"]) < len(d["rooms"])
    assert any(f["type"] == "ASSUMPTION" and "default criteria apply to the" in f["text"] for f in d["flags"])
    assert any(f["type"] == "OUTDOOR AIR" for f in d["fans"])
    assert all(r["storey"] in ("L02", "L03", "L04", "L05", "L06", "L07") for r in d["rooms"])
    assert space_name("B | B | FFL: +23.000 | SFL: +22.900 | Corr") == "Corr"
    assert unit_tags("FCU-B-02-02COR-18 [ann 39, 0.0 pt]; OAF-B-02-02COR-03 [ann 79, 5 pt]") == ["FCU-B-02-02COR-18", "OAF-B-02-02COR-03"]
    d["rooms"] = d["rooms"][:6]
    d["ahus"] = [a for a in d["ahus"] if a["tag"] in {r["ahu"] for r in d["rooms"]}][:4]
    d["fans"] = []
    ctx = build_workbook(d, tmp_path / "ca.xlsx")
    rc = recalc(tmp_path / "ca.xlsx", tmp_path / "rc")
    assert st.scan_errors(rc) == [] and st.language_scan(rc) == []
    assert len(ctx.rooms) == 6


def test_flags_one_per_gap_without_duplicates(tmp_path):
    d = build_reva_input(ROOT)
    for r in d["rooms"][:2]:
        r["ahu"] = None
        r["terminal_tag"] = None
        r["criteria"] = {}
    d["flags"] = [dict(type="VERIFY", ref="X", text="Same text.", source=""), dict(type="VERIFY", ref="X", text="Same text.", source="")]
    build_workbook(d, tmp_path / "f.xlsx")
    wb = openpyxl.load_workbook(tmp_path / "f.xlsx")
    texts = [(r[1], r[3]) for r in wb["Flags"].iter_rows(min_row=7, values_only=True) if r[0] and str(r[0]).startswith("F-") and not str(r[3] or "").startswith("=")]
    assert len(texts) == len(set(texts))
    for gap, n in (("no AHU or system", 2), ("no document shows its terminal type", len(d["rooms"])), ("no criteria row", 2)):
        per_room = [t for t in texts if gap in t[1]]
        assert len(per_room) == n and len({t[1] for t in per_room}) == n        # one flag per room and gap, never one per field


def test_unit_with_rooms_but_no_supply_counts_its_rooms_and_names_the_missing_height(tmp_path):
    """Fix round 2: Rooms and Area count every room of the unit, not only rooms with supply above 0; the Remarks name the missing height."""
    import sys
    sys.path.insert(0, str(ROOT / "tests"))
    from test_workbook_systems import _dept, _room
    from src.workbook.layouts import AHU_KEYS
    rooms = [_room("A1", "Lift lobby", 60.7, extra=dict(ceiling_height_m=None, lighting_w_m2=0, small_power_w_m2=0)), _room("A2", "Store", 20.0, extra=dict(ceiling_height_m=None, lighting_w_m2=0, small_power_w_m2=0))]
    for r in rooms:
        r["criteria"].update(min_total_ach=6, supply_lps_m2=None)
    d = _dept(rooms, ahus=[dict(tag="AHU-T1", type="AHU", sched_supply_lps=5000.0, sched_code="D", sched_source="RA-AHU-REF", tender_source_text="tender schedule, test")])
    build_workbook(d, tmp_path / "z.xlsx")
    rc = recalc(tmp_path / "z.xlsx", tmp_path / "rc")
    wb = openpyxl.load_workbook(rc, data_only=True)
    row = [r for r in wb["AHU"].iter_rows(min_row=7, values_only=True) if r[0] == "AHU-T1"][0]
    k = lambda key: row[AHU_KEYS.index(key)]
    assert k("sa_room") == 0 and k("n") == 2 and k("area") == pytest.approx(80.7) and k("area_sup") == 0 and k("noh") == 2
    assert k("rem").startswith("Reduced 5,000 to 0 L/s") and "ceiling height is not set for 2 of 2 room(s)" in k("rem")
    assert not k("rem").endswith(" ") and "No room" not in k("rem")
    # the master summary reads the same counts and names the same cause
    wd = tmp_path / "dept"
    wd.mkdir()
    (wd / "AIGH_C2_D99_Test_Thermal_Load_Calc_RevA.xlsx").write_bytes((tmp_path / "rc" / "z.xlsx").read_bytes())
    build_master(wd, tmp_path / "m.xlsx")
    mrc = recalc(tmp_path / "m.xlsx", tmp_path / "mrc")
    m = [r for r in openpyxl.load_workbook(mrc, data_only=True)["AHU Master"].iter_rows(min_row=7, values_only=True) if r[0] == "AHU-T1"][0]
    assert m[3] == 2 and m[4] == pytest.approx(80.7) and m[5] == 2 and "ceiling height is not set for 2 of 2 room(s)" in m[-1]


def test_duplicate_criteria_text_lists_each_distinct_set_of_values():
    from src.workbook.assemble import _variant_text
    row = dict(booklet_page="139", min_total_ach="15", min_oa_ach="2", pressure_regime_as_printed="+15", air_class="4")
    v = [dict(row, booklet_page="140", min_total_ach="6"), dict(row, booklet_page="142", min_total_ach="6")]
    t = _variant_text(row, v)
    assert t.startswith("The booklet prints this room type more than once with different values. Page 139 gives 15 total ACH")
    assert "Pages 140 and 142 give 6 total ACH" in t and t.endswith("The table on page 139 is used.")
