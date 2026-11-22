"""Tests for src.workbook: Rev A reproduction, formulas, style proof and traceability."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import openpyxl
import pytest

from src.workbook import selftest as st
from src.workbook.build import build_workbook
from src.workbook.ctx import Ctx
from src.workbook.layouts import AHU_KEYS, ENG
from src.workbook.model import InputError
from src.workbook.recalc import recalc
from src.workbook.reva import build_reva_input

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def reva(tmp_path_factory):
    work = tmp_path_factory.mktemp("reva")
    data = build_reva_input(ROOT)
    ctx = build_workbook(data, work / "reva.xlsx")
    rc = recalc(work / "reva.xlsx", work / "rc")
    return dict(ctx=ctx, path=work / "reva.xlsx", rc=rc, work=work, data=data)


def _ahu(rc, key):
    wb = openpyxl.load_workbook(rc, data_only=True)
    return wb["AHU"].cell(row=7, column=AHU_KEYS.index(key) + 1).value


def test_reva_totals(reva):
    assert _ahu(reva["rc"], "area") == pytest.approx(451.5, rel=0.005)
    assert _ahu(reva["rc"], "sa_room") == pytest.approx(2760, rel=0.005)
    assert _ahu(reva["rc"], "oa_ms") == pytest.approx(779.6, rel=0.005)
    assert _ahu(reva["rc"], "sens") == pytest.approx(32.47, rel=0.005)
    assert _ahu(reva["rc"], "lat") == pytest.approx(4.64, rel=0.005)
    assert _ahu(reva["rc"], "coil_ms") == pytest.approx(70.97, rel=0.005)


def test_outdoor_air_rule_names_room_and_applies_fraction(reva):
    assert _ahu(reva["rc"], "crit") == "LOCKERS"
    assert _ahu(reva["rc"], "oa_rule") == pytest.approx(_ahu(reva["rc"], "frac") * _ahu(reva["rc"], "sa_ahu"), rel=1e-9)
    assert _ahu(reva["rc"], "sa_ahu") == pytest.approx(_ahu(reva["rc"], "sa_room") * 1.1, rel=1e-9)


def test_room_lines_match_reva_exactly(reva):
    cmp_ = st.compare_reva(reva["rc"], ROOT)
    for rm in cmp_["rooms"]:
        for k, (a, b) in rm.items():
            if k != "zone":
                assert a == pytest.approx(b, rel=1e-9, abs=1e-9), (rm["zone"], k)


def test_no_errors_no_iferror_no_typed_numbers(reva):
    assert st.scan_errors(reva["rc"]) == []
    fs = st.scan_formulas(reva["path"])
    assert fs["iferror"] == [] and fs["bad_funcs"] == [] and fs["external"] == []
    assert fs["literals"] == []


def test_style_proof(reva):
    sp = st.style_proof(reva["ctx"], reva["path"])
    assert sp["problems"] == [] and sp["stray_font"] == [] and sp["unnamed"] == []
    assert sp["n_coded"] > 300


def test_every_tab_present_and_print_setup(reva):
    wb = openpyxl.load_workbook(reva["path"])
    for name in ["README", "Basis of Design", "Room Information", "Criteria", "Envelope", "Level 1 Room Zones", "Airflow", "Pressure", "AHU", "FCU",
                 "Fans", "VAV", "Venturi Valves", "Flow Balance", "Airflow Comparison", "Checks", "Sources", "Flags"]:
        ws = wb[name]
        assert ws.page_setup.paperSize == 8 and ws.page_setup.orientation == "landscape", name
        assert ws.page_setup.fitToWidth == 1 and ws.sheet_properties.pageSetUpPr.fitToPage, name
        assert ws.print_area, name
        assert "Page &P of &N" in ws.oddFooter.right.text, name
        assert ws.sheet_view.showGridLines is False, name


def test_named_constants_exist(reva):
    wb = openpyxl.load_workbook(reva["path"])
    for n in ["SafetyFactor", "DuctLeakage", "SupplyTemp", "AirtightLimit", "FlowN", "DoorCd", "OAShare", "OADrop", "SpaceTypeName"]:
        assert n in wb.defined_names, n


def test_input_without_source_id_is_rejected():
    data = build_reva_input(ROOT)
    del data["rooms"][0]["prov"]["ceiling_height"]
    data["rooms"][0].pop("prov_default", None)
    with pytest.raises(InputError):
        build_workbook(data)
    data = build_reva_input(ROOT)
    data["rooms"][0]["prov"]["soa_area"] = ["D", "NO-SUCH-SOURCE"]
    with pytest.raises(InputError):
        build_workbook(data)


def test_required_constant_missing_is_rejected():
    data = build_reva_input(ROOT)
    del data["constants"]["SupplyTemp"]
    with pytest.raises(InputError):
        build_workbook(data)


def test_unknown_value_stays_blank(reva):
    wb = openpyxl.load_workbook(reva["path"])
    ws = wb["Room Information"]
    assert ws["A7"].value == "Z1"
    # no measured area in Rev A: the cell is blank, not zero
    from src.workbook.layouts import ri_spec
    keys = [k for k, *_ in ri_spec()]
    assert ws.cell(row=7, column=keys.index("meas") + 1).value is None


def test_flags_list_missing_terminals(reva):
    wb = openpyxl.load_workbook(reva["path"])
    ws = wb["Flags"]
    text = " ".join(str(r[3]) for r in ws.iter_rows(min_row=7, values_only=True) if r[3])
    assert "terminal type" in text
    assert "ASSUMPTION" in {r[1] for r in ws.iter_rows(min_row=7, values_only=True)}
