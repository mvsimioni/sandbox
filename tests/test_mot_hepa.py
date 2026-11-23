"""Tests for src/mot_hepa.py (D-038/D-039)."""
from __future__ import annotations

import pytest

from src import mot_hepa as m

SCHED = m.find_latest(m.HEPA_SCHEDULE_NAME)
SPEC = m.find_latest(m.SPEC_NAME)
needs_sched = pytest.mark.skipif(SCHED is None or not SCHED.is_file(), reason="MOT HEPA schedule not in checkout")
needs_spec = pytest.mark.skipif(SPEC is None or not SPEC.is_file(), reason="MOT Tech Spec not in checkout")


def test_fmt_keeps_trailing_zeros_of_integers():
    assert m.fmt(2750, 0) == "2750"
    assert m.fmt(0.35) == "0.35"
    assert m.fmt(7.84, 4) == "7.84"
    assert m.fmt(None) == ""


def test_corr_rank_prefers_corr3():
    assert m.corr_rank("x/Tender Corr 3/y (Corri 3).pdf") == 3
    assert m.corr_rank("x/Tender Corr 2/y.pdf") == 2


@needs_sched
def test_latest_schedule_is_corr3():
    assert "Corr 3" in str(SCHED)


@needs_sched
def test_schedule_parse_counts_and_values():
    parsed = m.parse_hepa_schedule(SCHED)
    tags = [p["tag"] for p in parsed]
    assert len(tags) == len(set(tags)) == 57
    gs = next(p for p in parsed if p["tag"] == "HEPA-L5-MOTGS-01")
    assert (gs["size"], gs["max_air_flow"], gs["max_face_velocity"], gs["page"]) == ("2800x2800", "2750", "0.35", 1)
    ucv = next(p for p in parsed if p["tag"] == "HEPA-L5-MOTCT-01")
    assert ucv["size"] == "3200x3200" and "UCV" in ucv["location"]
    st = next(p for p in parsed if p["tag"] == "HEPA-L5-STER-02A")
    assert st["duct_size"] == "Per drawings" and st["remarks"].startswith("Apply multiple number of HEPA")
    p2 = next(p for p in parsed if p["tag"] == "HEPA-L6-DSOT-09")
    assert p2["page"] == 2


@needs_sched
def test_terminal_rows_area_and_check():
    rows = m.terminal_rows(m.parse_hepa_schedule(SCHED), "doc")
    gs = next(r for r in rows if r["terminal_tag"] == "HEPA-L5-MOTGS-01")
    assert gs["face_area_m2"] == "7.84" and gs["area_x_velocity_l_s"] == "2744" and gs["flags"] == ""
    cath = next(r for r in rows if r["terminal_tag"] == "HEPA-L4-CATH-02")
    assert "differs from printed max air flow" in cath["flags"]
    assert all(r["quantity"] == 1 for r in rows)


@needs_spec
def test_velocity_clauses_found_with_pages():
    rows = m.clause_rows(SPEC, "spec")
    by = {r["clause"]: r for r in rows}
    r = by["1.17.4 e) i"]
    assert (r["velocity_min_m_s"], r["velocity_max_m_s"]) == ("0.25", "0.4")
    assert "0.25" in r["text_as_printed"] and r["pdf_page"] == 56 and r["printed_page"].startswith("TS/ACMV/54")
    assert by["1.17.4 e) ii"]["velocity_min_m_s"] == "0.127"
    assert all(x["confidence"] == "high" for x in rows), [x["clause"] for x in rows if x["confidence"] != "high"]


def _rooms():
    base = {k: "" for k in ("room_name_printed", "level", "pod_or_suite", "is_operating_theatre", "ahu_pahu_tag", "supply_air",
                             "mode_columns", "sa_norm_l_s", "sa_setback_l_s", "sa_singlepass_l_s", "hepa_terminal_tags", "source_kind",
                             "source_file", "page_or_sheet", "box_or_row")}
    a = dict(base, room_name_printed="OPERATING ROOM", level="L5", is_operating_theatre="y", ahu_pahu_tag="AHU-B-07-05MOTX-01A | AHU-B-07-05MOTX-01B",
             sa_norm_l_s="2750 L/s", sa_setback_l_s="1290 L/s", hepa_terminal_tags="HEPA-L5-MOTX-01", source_kind="air-side schematic room box", source_file="s.pdf")
    b = dict(base, room_name_printed="OR - NOHEPA", level="L5", is_operating_theatre="y", ahu_pahu_tag="AHU-B-07-05MOTY-02", source_kind="Appendix J schedule of modes row", source_file="j.pdf")
    return [a, b]


def _terms():
    return [{"terminal_tag": "HEPA-L5-MOTX-01", "location_as_printed": "Operating Room - X", "level": "L5", "room_kind": "operating theatre",
             "module_size_as_printed": "2800x2800", "face_area_m2": "7.84", "max_face_velocity_m_s_as_printed": "0.35",
             "max_air_flow_l_s_as_printed": "2750", "flags": ""}]


def test_airflow_row_calc_leakage_and_missing():
    ahu = [{"tag": "AHU-B-07-05MOTX-01A", "supply_airflow_l_s": "3450"}, {"tag": "AHU-B-07-05MOTX-01B", "supply_airflow_l_s": "3450"}]
    rows = m.airflow_rows(_terms(), _rooms(), ahu, 10.0)
    ok = rows[0]
    assert ok["supply_l_s_face_area_x_velocity"] == "2744"
    assert ok["ahu_l_s_with_leakage"] == "3018"
    assert ok["schematic_supply_norm_l_s"] == "2750"
    assert ok["ahu_schedule_supply_l_s"] == "3450"  # A/B twins: not summed
    assert ok["supply_l_s_at_spec_0p25"] == "1960" and ok["supply_l_s_at_spec_0p40"] == "3136"
    assert ok["status"] == "calculated"
    miss = [r for r in rows if r["status"] != "calculated"]
    assert len(miss) == 1 and miss[0]["theatre"] == "OR - NOHEPA"


@needs_sched
def test_run_writes_registers(tmp_path):
    res = m.run(tmp_path)
    assert res["terminals"] == 57
    for n in ("mot_hepa_terminals.csv", "mot_face_velocity.csv", "mot_ot_airflow.csv"):
        assert (tmp_path / n).is_file()
