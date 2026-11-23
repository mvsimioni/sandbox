"""Fixes of the final verification round 1: criteria matching, occupancy use rules, fan linking, tender lookup, coverage test."""
from src.workbook.assemble import match_criteria, room_use
from src.workbook.criteria_map import mapped_row
from src.workbook.fans import area_match
from src.workbook.sheets_systems import coverage_formula
from src.workbook.tender_lookup import expand_tag


def _row(name, **kw):
    return dict(dict(room_type_as_printed=name, room_type_norm=name.lower(), booklet_page="1", min_oa_ach="2", min_total_ach="6", pressure_regime_as_printed="Neutral"), **kw)


def test_no_match_on_shared_words():
    rows = [_row("Clean Up & Dirty Utility"), _row("AV Control Room"), _row("Cath Lab")]
    for name in ("Clean Utility", "Control Room", "Vendor Technical Room - Cath Lab", "Store (Future Cath Lab)"):
        row, _, _ = match_criteria(name, rows)
        assert row is None, name


def test_exact_name_is_code_d_and_listed_name_is_marked():
    row, _, how = match_criteria("Cath Lab", [_row("Cath Lab")])
    assert row is not None and how == "same name"
    row, _, how = match_criteria("Corridor", [_row("Clean Utility/AGV Lift Lobby/Workstation/Corridor/Medication/Pantry")])
    assert row is not None and how.startswith("name listed")


def test_room_type_mapping_by_name_and_suite():
    rows = [_row("OT POD Clinical Support/Corridor/Meeting Room/Bay Exit/Store Eq/Bay Cart Holding", booklet_page="133"),
            _row("Cath Lab", booklet_page="86")]
    hit = mapped_row("22", "Bay - Exit", "MSOT 05.01.01", rows, lambda t: None)
    assert hit is not None and hit[0]["booklet_page"] == "133"
    assert mapped_row("28", "Catheter Lab - Procedure Room", "CATH 01.01.01", rows, lambda t: None)[0]["room_type_as_printed"] == "Cath Lab"
    assert mapped_row("22", "Bay - Exit", "AMSC 05.01.01", rows, lambda t: None) is None


def test_occupancy_use_rules_of_the_verification():
    for name, use in (("Bay - Handwashing, Type A", "corridor_circulation"), ("Lecture Theatre", "lecture_classroom"), ("Patient Bay - Holding", "patient_room"),
                      ("Sub-Wait - Outpatient Holding", "reception_waiting"), ("Clinical Workroom - ICU", "office"), ("Anaesthesia Prep", "medical_procedure"),
                      ("Water Treatment Room", "plant_room"), ("Ensuite - Accessible", "toilet"), ("Staff Station", "office"), ("Triage Room", "medical_procedure")):
        assert room_use(name)[0] == use, name
    assert room_use("Locker - Staff")[0] is None          # no density row: not set, flagged


def test_fans_of_another_storey_are_not_linked():
    assert not area_match("02COR", "05COR") and not area_match("02STAFFLG", "07STAFF")
    assert area_match("CSSD", "CSSDDCN") and area_match("05COR", "05COR")


def test_list_tags_expand():
    assert expand_tag("FCU-B-01-01RETAIL-01/02/03") == ["FCU-B-01-01RETAIL-01", "FCU-B-01-01RETAIL-02", "FCU-B-01-01RETAIL-03"]
    assert expand_tag("PAHU-B-B1-B2HOSP-01A/B/C") == ["PAHU-B-B1-B2HOSP-01A", "PAHU-B-B1-B2HOSP-01B", "PAHU-B-B1-B2HOSP-01C"]
    assert expand_tag("AHU-B-03-03CORE-01") == ["AHU-B-03-03CORE-01"]


def test_coverage_is_two_sided_and_needs_criteria():
    f = coverage_formula("A1", "B1", "C1", "D1", "E1", "ZoneCoverMin")
    assert "ZoneCoverMin*B1" in f and "B1/ZoneCoverMin" in f and "criteria row" in f
