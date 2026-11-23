"""Tests of the fix pass: D-006, D-056, D-058, D-059, D-063, D-068, D-069, D-071."""
import csv
from pathlib import Path

import pytest

from src import phase_checks
from src.workbook.clean import clean_text, clean_workbook

ROOT = Path(__file__).resolve().parents[1]


def test_clean_text_removes_internal_names():
    t = "Interim, open for Marcus (OI-R206-1): the zone gives 1 room. Basis: council ruling D-029/D-036 (D-058). Round 5."
    c = clean_text(t)
    for bad in ("Marcus", "OI-R", "council", "D-0", "Round 5"):
        assert bad not in c
    assert "Interim, to confirm" in c


def test_clean_formula_literals_only():
    from src.workbook.clean import _clean_formula
    f = '=IF(A1>0," open for Marcus (OI-R305-1) ",B1)'
    assert "Marcus" not in _clean_formula(f) and _clean_formula(f).startswith("=IF(A1>0,")


def test_seeded_unit_errors_are_caught():
    links = {("22", f"R{i}"): f"AHU-{i}" for i in range(5)}
    seeded = dict(links)
    for k in list(links)[:3]:
        seeded[k] = "AHU-SEEDED"
    assert len(phase_checks.unit_differences(links, seeded)) == 3


def test_lint_sentences():
    assert phase_checks.lint_sentences("Short one. " + "word " * 30 + "end.")


def test_d059_no_room_on_variant_tag_and_pod_split():
    p = ROOT / "out/registers/room_unit_links.csv"
    if not p.exists():
        pytest.skip("no link register")
    rows = {r["dpa_room_id"]: r["unit_class"] for r in csv.DictReader(p.open(newline="", encoding="utf-8")) if r["dept"] == "22"}
    assert not any("MOTHYB-02" in v for v in rows.values())
    assert rows.get("MSOT 05.01.12") == "AHU-B-07-05MOTHYB-01"
    assert rows.get("MSOT 03.01.04") == rows.get("MSOT 03.02.04") == "AHU-B-07-05MOT-01"


def test_d058_ucv_text_has_no_open_item():
    from src.workbook.tag_notes import ucv_unit_note
    n = ucv_unit_note("AHU-B-07-05MOTCT-01")
    assert "open for" not in n and "future duty governs" in n


def test_d056_status_lines_in_checks_code():
    s = (ROOT / "src/workbook/sheets_basic.py").read_text()
    assert '"Inputs complete"' in s and '"Calculation"' in s and "Overall" not in s.split("def write_readme")[0].split("def write_checks")[-1]


def test_d068_trace_builds():
    from src import gate
    rows = gate.build_trace()
    assert not [r for r in rows if r["gate"] == "FAIL"]
    by = {r["d_id"]: r["trace_status"] for r in rows}
    for d in ("D-006", "D-056", "D-058", "D-063", "D-068", "D-069", "D-071"):
        assert by[d] == "TRACED", d
