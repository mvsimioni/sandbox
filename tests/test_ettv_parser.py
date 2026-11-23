"""Tests for src/ettv_parser.py (D-042).  Real-PDF tests skip if the source file is absent."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from src import ettv_parser as ep

ROOT = Path(__file__).resolve().parents[1]
PDF = ROOT / ep.DOC

needs_pdf = pytest.mark.skipif(not PDF.exists(), reason="ETTV PDF not on disk")

SHEET = """
                             FACADE ORIENTATION N (N,S,NW, etc)
 OPAQUE WALLS
W1       Spandrel Glazing                       100.000                0.40                     480
W2       Insulated Precast Panel                 50.000                0.58                     348
Subtotal                                        150.000                                           828
                                        FENESTRATION
#nf.fen.0                                                                      CF                          0.8
        8mm clear HS with Low-E
F1(SC1) laminated                         50.00         1.65         0.2 280.5           1688
Subtotal                                  50.00                      280.5               1688
#nf.gross.0Gross Area of External Walls (Ao):      200 m²
           Gross Heat Gain:                        2796.5 W
           ETTV = Gross Heat Gain/                 13.9825 W/m²
"""


def test_parse_a1_synthetic():
    a = ep.parse_a1(SHEET)
    assert a["orientation"] == "N" and a["cf"] == 0.8 and a["ao"] == 200
    assert [w["code"] for w in a["walls"]] == ["W1", "W2"]
    assert a["fens"][0]["sc_index"] == 1 and a["fens"][0]["af"] == 50.0 and a["fens"][0]["sc"] == 0.2
    assert a["fen_sub"]["area"] == 50.0
    assert ep.check_a1(a) == []
    dv = ep.derive(a)
    assert dv["wwr"] == pytest.approx(0.25)
    assert dv["wwr_incl_spandrel"] == pytest.approx(0.75)


def test_check_a1_flags_bad_ettv():
    a = ep.parse_a1(SHEET.replace("13.9825", "15.0"))
    assert any("ETTV" in m for m in ep.check_a1(a))


def test_parse_summary_synthetic():
    t = "Project Ref. No:   SG1\n (a)  Facade 1 facing   N   ETTV =  22.116 W/m²\n The average ETTV of the building is     24.934 W/m²\nDate: 29 Jan 2026"
    s = ep.parse_summary(t)
    assert s["facades"][0]["orientation"] == "N" and s["average"] == 24.934 and s["cert_date"] == "29 Jan 2026"


def test_parse_module_synthetic():
    t = ("Project Façade Module  FT06  Orientation  SouthEast  SC2  0.8197\n ETTV Report Façade Type  F1(SC26)\n"
         " [-0.9, 0.01, -0.39]  0  57.793  2774.075  2774.075\n [0.1, 0.02, -0.98]  22.5  57.793  9480.6  16066.5\n")
    m = ep.parse_module(t)
    assert m["module"] == "FT06" and m["f1_type"] == "F1(SC26)" and m["n_vectors"] == 2 and m["glass_areas"] == [57.793]


def test_orientation_helpers():
    assert ep.orientation_from_bearing(0) == "N" and ep.orientation_from_bearing(359) == "N"
    assert ep.orientation_from_bearing(44) == "NE" and ep.orientation_from_bearing(181) == "S"
    assert ep.orientation_from_bearing(300) == "NW"
    assert ep.normal_bearing_from_building_xy(0, 1) == pytest.approx(0)
    assert ep.normal_bearing_from_building_xy(1, 0) == pytest.approx(90)
    assert ep.normal_bearing_from_building_xy(1, 0, grid_north_bearing_deg=45) == pytest.approx(135)


def test_assign_columns():
    cols = [(189.0, "A"), (251.0, "B"), (312.0, "C")]
    mk = lambda cx: (cx - 5, 0, cx + 5, 0, "x")
    assert ep._assign(cols, mk(246), 3.0) == 0
    assert ep._assign(cols, mk(307), 3.0) == 1
    assert ep._assign(cols, mk(522), 3.0) == 2
    assert ep._assign(cols, mk(100), 3.0) is None


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    if not PDF.exists():
        pytest.skip("ETTV PDF not on disk")
    out = tmp_path_factory.mktemp("ettv")
    return ep.run(PDF, out), out


@needs_pdf
def test_real_summary_and_building_average(built):
    res, out = built
    s = res["data"]["summary"]
    assert [f["orientation"] for f in s["facades"]] == ep.FACADE_ORDER_P1
    assert s["average"] == 24.934
    ettv = {f["orientation"]: f["ettv"] for f in s["facades"]}
    assert ettv["N"] == 22.116 and ettv["SE"] == 29.864


@needs_pdf
def test_real_page_counts(built):
    d = built[0]["data"]
    assert d["n_pages"] == 67
    assert set(d["a1"]) == set(d["a2"]) == set(d["a3"]) == set(d["a4"]) == set(ep.ORIENTATIONS)
    assert len(d["modules"]) == 33
    assert {c["code"] for c in d["u_types"]} == {"W1", "W2", "W3", "F1"}


@needs_pdf
def test_real_north_sheet(built):
    a = built[0]["data"]["a1"]["N"]
    assert a["ao"] == 7300.343 and a["gross_heat"] == 161451.5 and a["ettv"] == 22.11561 and a["cf"] == 0.8
    assert [f["sc_index"] for f in a["fens"]] == [1, 2, 3, 4, 5]
    assert a["fen_sub"]["area"] == 4111.38


@needs_pdf
def test_real_only_known_conflict(built):
    conflicts = built[0]["conflicts"]
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c["where"] == "N F1(SC3)" and "SC1 x SC2" in c["check"]


@needs_pdf
def test_real_building_average_is_area_weighted(built):
    d = built[0]["data"]
    h = sum(d["a1"][o]["gross_heat"] for o in ep.ORIENTATIONS)
    a = sum(d["a1"][o]["ao"] for o in ep.ORIENTATIONS)
    assert round(h / a, 3) == 24.934


@needs_pdf
def test_csv_provenance_and_blanks(built):
    _, out = built
    rows = list(csv.DictReader(open(out / ep.OUT_FACADES, encoding="utf-8")))
    assert rows and all(r["document"] and r["page"] and r["text_as_printed"] and r["method"] and r["confidence"] for r in rows)
    fac = {r["orientation"]: r for r in rows if r["row_type"] == "facade"}
    assert len(fac) == 8 and fac["N"]["block"] == "" and fac["N"]["storey_or_zone"] == ""
    assert float(fac["N"]["wwr"]) == pytest.approx(4111.38 / 7300.343, abs=1e-4)
    assert ep.facade_record(rows, "SE")["ettv_w_m2"] == "29.86399"
    notes = list(csv.DictReader(open(out / ep.OUT_NOTES, encoding="utf-8")))
    assert any(n["category"] == "formula" for n in notes) and all(n["document"] for n in notes)
    con = list(csv.DictReader(open(out / ep.OUT_CONSTR, encoding="utf-8")))
    byid = {c["record_id"]: c for c in con}
    assert byid["CON-W1"]["u_value_w_m2k"] == "0.4" and byid["CON-W3"]["u_value_w_m2k"] == "8"
    assert byid["CON-F1"]["u_value_w_m2k"] == "1.65" and byid["CON-F1"]["sc1"] == "0.25"
