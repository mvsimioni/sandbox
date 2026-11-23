"""Pressure tab: known answers in the recalculated workbook and the fixed-step solution against src.pressure."""

from __future__ import annotations

import copy
from pathlib import Path

import openpyxl
import pytest

from src import pressure as pm
from src.workbook import selftest as st
from src.workbook.build import build_workbook
from src.workbook.layouts import ps_keys
from src.workbook.recalc import recalc

ROOT = Path(__file__).resolve().parents[1]


def _nodes(rc):
    wb = openpyxl.load_workbook(rc, data_only=True)
    ws = wb["Pressure"]
    keys, _ = ps_keys()
    out = {}
    for r in ws.iter_rows(min_row=7, max_row=20, values_only=True):
        if r[0] and r[1] in ("ROOM", "EXTERNAL"):
            out[r[0]] = dict(zip(keys, r))
    return out


@pytest.fixture(scope="module")
def ka(tmp_path_factory):
    work = tmp_path_factory.mktemp("ka")
    data = st.ka_input()
    ctx = build_workbook(data, work / "ka.xlsx")
    rc = recalc(work / "ka.xlsx", work / "rc")
    return dict(rc=rc, ctx=ctx, work=work, data=data)


def test_known_answer_block_all_pass(ka):
    cases = st.compare_ka(ka["rc"])
    assert len(cases) >= 19
    assert all(c["ok"] for c in cases), [c for c in cases if not c["ok"]]


def test_known_answers_through_node_and_path_tables(ka):
    res = st.compare_ka_tables(ka["rc"])
    assert all(c["ok"] for c in res), [c for c in res if not c["ok"]]


def test_matches_pressure_module_for_every_space(ka):
    """D-028: every room is its own envelope-only space: the node table agrees with the pressure module."""
    n = _nodes(ka["rc"])
    suite = pm.Suite("ka", [pm.Space("ANTE", -15, n["ANTE"]["surf"]), pm.Space("ROOM", -30, n["ROOM"]["surf"]), pm.Space("ENSUITE", -31, 37.5)], [], externals={}).solve()
    for name in ("ANTE", "ROOM", "ENSUITE"):
        r = suite.space(name)
        w = n[name]
        assert w["needed"] == pytest.approx(r.offset_needed, abs=0.05), name
        assert w["design"] == pytest.approx(r.design_offset, abs=0.05)
        assert w["env_in"] == pytest.approx(r.envelope_in, abs=0.05)
        assert w["gap_in"] == 0 and w["gap_out"] == 0
        assert w["kindoff"].lower() == r.offset_kind
        assert w["ratio_n50"] == pytest.approx(r.ratio_n050, rel=0.02)
        assert w["ratio_n75"] == pytest.approx(r.ratio_n075, rel=0.02)


def test_no_door_columns_or_door_wording_on_the_pressure_tab(ka):
    wb = openpyxl.load_workbook(ka["rc"], data_only=True)
    text = " ".join(str(c.value) for row in wb["Pressure"].iter_rows() for c in row if isinstance(c.value, str)).lower()
    assert "no door data" not in text and "gaps × 2" not in text and "door gaps:" not in text


def test_perimeter_from_the_area_where_no_outline(tmp_path):
    """No measured outline: perimeter = 4 x square root of the area (named cell PerimFactor, code A)."""
    data = st.ka_input()
    data["rooms"][0]["pressure"] = dict(data["rooms"][0]["pressure"], perimeter_m=None, perimeter_rule="factor")
    build_workbook(data, tmp_path / "p.xlsx")
    rc = recalc(tmp_path / "p.xlsx", tmp_path / "rc")
    n = _nodes(rc)
    area = n["ANTE"]["area"]
    assert n["ANTE"]["perim"] == pytest.approx(4 * area ** 0.5, abs=0.01)
    assert n["ANTE"]["perim_c"] == "A"


def test_free_space_newton_matches_exact_solution(tmp_path):
    data = st.ka_input()
    free = copy.deepcopy(data["rooms"][2])
    free.update(number="ENS2", name="Free ensuite", soa_area_m2=4.0)
    free["criteria"] = dict(free["criteria"], pressure_pa_tender=None, exhaust_specified_lps=10, min_total_ach=None, min_oa_ach=None)
    free["pressure"] = dict(mode="free", suite="S5", perimeter_m=3.0, prov=["D", "KA-8.3"], surface_override_m2=30.0, surface_prov=["D", "KA-8.3"],
                            doors=[dict(id="D9", to="ROOM", kind="door louvre (free area)", gap_cm2=200.0, code="D", source="KA-8.3"),
                                   dict(id="D10", to="CORRIDOR", kind="door", gap_cm2=15.0, code="D", source="KA-8.3")])
    data["rooms"].append(free)
    data["pressure_nodes"].append(dict(tag="CORRIDOR", description="Corridor", pressure_pa=0, code="S", source="GEN-PRESSURE"))
    build_workbook(data, tmp_path / "f.xlsx")
    rc = recalc(tmp_path / "f.xlsx", tmp_path / "rc")
    n = _nodes(rc)
    f = n["ENS2"]
    assert f["status"] == "FREE", f
    suite = pm.Suite("f", [pm.Space("ROOM", -30, n["ROOM"]["surf"]), pm.Space("ENS2", None, 30.0, exhaust_m3h=f["exhaust"], supply_m3h=f["supply"])],
                     [pm.Link("ENS2", "ROOM", gap_area_m2=pm.cm2_to_m2(200)), pm.Link("ENS2", "CORRIDOR", gap_area_m2=pm.cm2_to_m2(15))],
                     externals={"CORRIDOR": 0}).solve_exact()
    assert f["pfin"] == pytest.approx(suite.pressures["ENS2"], abs=0.05)
    assert abs(f["resid"]) < 0.5
    assert st.scan_errors(rc) == []
