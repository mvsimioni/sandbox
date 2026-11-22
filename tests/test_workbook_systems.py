"""Systems tabs and the outdoor-air rule on a synthetic department."""

from __future__ import annotations

import copy
from pathlib import Path

import openpyxl
import pytest

from src.workbook import selftest as st
from src.workbook.build import build_workbook
from src.workbook.layouts import af_spec, AHU_KEYS
from src.workbook.recalc import recalc
from src.workbook.reva import build_reva_input

ROOT = Path(__file__).resolve().parents[1]


def _room(number, name, area, storey="L01", ahu="AHU-T1", oa_spec=None, clinical=False, terminal="VAV-1", exfan=None, sched=None, extra=None):
    prov = {k: ["D", "RA-RZ-30"] for k in ("identity", "zoning", "space_type", "soa_area", "ceiling_height", "iof", "activity", "lighting", "small_power")}
    prov["area_selector"] = ["S", "RA-BOD-B42"]
    c = dict(criteria_row="test", page="p.1", min_oa_ach=2, min_total_ach=6, oa_specified_lps=oa_spec, pressure_regime="POSITIVE", air_class="2",
             filter="MERV 15", single_pass="N", source="RA-DD21", code="D")
    if clinical:
        c.update(single_pass="Y")
    r = dict(number=number, name=name, storey=storey, ahu=ahu, zone_tag=number, system_type="VAV", terminal_tag=terminal, exhaust_fan=exfan,
             space_type="Pharmacy (OP)", room_condition="Comfort", soa_area_m2=area, area_selector="SOA", ceiling_height_m=3.0, iof=1,
             activity="Seated", lighting_w_m2=12, small_power_w_m2=10, prov=prov, criteria=c, envelope=[], schematic=sched)
    r.update(extra or {})
    return r


def _dept(rooms, **kw):
    d = build_reva_input(ROOT)
    d["rooms"] = rooms
    d["storeys"] = sorted({r["storey"] for r in rooms})
    d["storey_labels"] = {"L01": "Level 1", "L02": "Level 2"}
    d["references"] = []
    d.update(kw)
    return d


def _rows(rc, sheet, first=7):
    wb = openpyxl.load_workbook(rc, data_only=True)
    return [r for r in wb[sheet].iter_rows(min_row=first, values_only=True)]


def _af(rc):
    keys = [k for k, *_ in af_spec()]
    return [dict(zip(keys, r)) for r in _rows(rc, "Airflow") if r[0]]


@pytest.fixture(scope="module")
def oa(tmp_path_factory):
    work = tmp_path_factory.mktemp("oa")
    rooms = [_room("A1", "Large 1", 300), _room("A2", "Large 2", 300), _room("A3", "Large 3", 300),
             _room("M1", "Meeting", 12, oa_spec=40.0, terminal="VAV-M")]
    rooms[0]["criteria"]["exhaust_specified_lps"] = 30.0
    rooms[0]["exhaust_fan"] = "EXF-1"
    d = _dept(rooms, fans=[dict(tag="EXF-1", type="EXHAUST", serves="EXF-1", scheduled_lps=500, source="RA-AHU-REF", code="D"),
                           dict(tag="RAF-1", type="RETURN", serves="AHU-T1", scheduled_lps=100, source="RA-AHU-REF", code="D"),
                           dict(tag="OAF-1", type="OUTDOOR AIR", serves="AHU-T1", scheduled_lps=3000, source="RA-AHU-REF", code="D")],
              terminals=[dict(tag="VAV-1", type="VAV", source="RA-AHU-REF", code="D"), dict(tag="VAV-M", type="VAV", source="RA-AHU-REF", code="D")],
              valves=[dict(tag="MV-M-S", room="M1", role="SUPPLY", source="RA-AHU-REF", code="D")],
              ahus=[dict(tag="AHU-T1", type="AHU", sched_supply_lps=1000, sched_oa_lps=300, sched_source="RA-AHU-REF", sched_code="D",
                         schematic_supply_lps=5000, schematic_source="RA-AHU-REF", schematic_code="D")])
    ctx = build_workbook(d, work / "oa.xlsx")
    return dict(rc=recalc(work / "oa.xlsx", work / "rc"), ctx=ctx, path=work / "oa.xlsx", data=d)


def test_small_high_oa_room_is_separated(oa):
    af = {r["no"]: r for r in _af(oa["rc"])}
    assert af["M1"]["status"] == "SEPARATE"
    assert af["A1"]["status"] in (None, "")
    assert af["M1"]["frac"] > 0.5


def test_clinical_room_is_listed_not_separated(tmp_path):
    rooms = [_room("A1", "Large 1", 300), _room("A2", "Large 2", 300), _room("A3", "Large 3", 300),
             _room("C1", "Isolation small", 12, oa_spec=40.0, clinical=True, terminal="VAV-C")]
    build_workbook(_dept(rooms), tmp_path / "c.xlsx")
    rc = recalc(tmp_path / "c.xlsx", tmp_path / "rc")
    af = {r["no"]: r for r in _af(rc)}
    assert af["C1"]["status"] == "LIST FOR REVIEW"
    assert af["C1"]["sepf"] == 0


def test_ahu_before_and_after_separation(oa):
    rows = _rows(oa["rc"], "AHU")
    before = dict(zip(AHU_KEYS, [r for r in rows if r[0] == "AHU-T1"][0]))
    after_row = [r for r in rows if r[0] == "AHU-T1"][1]
    after = dict(zip(AHU_KEYS, after_row))
    assert before["crit"] == "Meeting"
    assert before["oa_rule"] > after["oa_rule"]
    assert after["frac"] < before["frac"] - 0.05
    assert after["sa_room"] < before["sa_room"]
    assert after["coil"] < before["coil"]
    assert after["n"] == before["n"] - 1
    assert after["crit"] != "Meeting"


def test_fcu_tab_lists_separated_room_with_oa_coil(oa):
    from src.workbook.layouts import FCU_KEYS
    rows = _rows(oa["rc"], "FCU")
    first = dict(zip(FCU_KEYS, rows[0]))
    assert first["no"] == "M1"
    assert first["why"] == "OA RULE"
    assert first["coil"] and first["coil"] > first["sens_kw"] + first["lat_kw"]   # coil duty includes the outdoor air
    # no source on the room: the outdoor-air fan that serves its AHU on Fans is proposed, untreated, and flagged to confirm
    assert first["oasrc"] == "OAF-1" and first["oaby"] == "FANS DEFAULT" and first["oacond"] == "UNTREATED"
    assert first["cap"] in ("PASS", "VERIFY", "NO TENDER VALUE", "NOT CALCULATED")      # capacity of the source with the added airflow is tested; above tender is VERIFY, never FAIL
    assert "CONFIRM" in first["flag"]


def test_fans_vav_venturi_comparison_flow_balance(oa):
    fans = {r[0]: r for r in _rows(oa["rc"], "Fans") if r[0]}
    assert fans["EXF-1"][4] > 0
    assert fans["RAF-1"][5] == pytest.approx(0.1)
    from src.workbook.layouts import FAN_KEYS
    assert fans["EXF-1"][FAN_KEYS.index("status")] in ("PASS", "VERIFY", "NO TENDER VALUE", "NOT CALCULATED")
    vav = {r[0]: r for r in _rows(oa["rc"], "VAV") if r[0]}
    assert vav["VAV-1"][4] >= vav["VAV-1"][5] > 0
    assert 0 < vav["VAV-1"][6] <= 1
    ven = [r for r in _rows(oa["rc"], "Venturi Valves") if r[0]]
    assert ven[0][1] == "M1"
    fb = [r for r in _rows(oa["rc"], "Flow Balance") if r[0] in ("A1", "M1")]
    assert all(r[14] == "PASS" for r in fb)
    cp = [r for r in _rows(oa["rc"], "Airflow Comparison") if r[0] == "A1"]
    from src.workbook.layouts import CP_KEYS
    assert cp[0][CP_KEYS.index("flag")] == "NO TENDER VALUE"
    assert st.scan_errors(oa["rc"]) == []


def test_two_storeys_two_engine_tabs(tmp_path):
    rooms = [_room("A1", "L1 room", 100), _room("B1", "L2 room", 100, storey="L02")]
    ctx = build_workbook(_dept(rooms), tmp_path / "two.xlsx")
    wb = openpyxl.load_workbook(tmp_path / "two.xlsx")
    assert "Level 1 Room Zones" in wb.sheetnames and "Level 2 Room Zones" in wb.sheetnames
    rc = recalc(tmp_path / "two.xlsx", tmp_path / "rc")
    assert st.scan_errors(rc) == []
    ahu = [r for r in _rows(rc, "AHU") if r[0] == "AHU-T1"][0]
    assert ahu[AHU_KEYS.index("area")] == pytest.approx(200)


def test_pressure_target_room_gets_offset_into_airflow(tmp_path):
    d = st.ka_input()
    build_workbook(d, tmp_path / "k.xlsx")
    rc = recalc(tmp_path / "k.xlsx", tmp_path / "rc")
    af = {r["no"]: r for r in _af(rc)}
    # negative room: extract = supply + offset
    assert af["ROOM"]["extract"] == pytest.approx(af["ROOM"]["supply"] + af["ROOM"]["off"], rel=1e-9)
    assert af["ROOM"]["kind"] == "EXHAUST"
    assert af["ROOM"]["ret"] == pytest.approx(0, abs=1e-9)  # single-pass: all extract is exhaust
