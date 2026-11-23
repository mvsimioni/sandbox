"""Tests for src/mot_tags.py (D-038)."""
from __future__ import annotations

import pytest

from src import mot_tags as t

needs_xlsx = pytest.mark.skipif(not t.XLSX.is_file(), reason="cross-reference workbook not in checkout")


def test_expand_tags_pairs_and_alternatives():
    assert t.expand_tags("AHU-B-07-05STER-01A/B") == ["AHU-B-07-05STER-01A", "AHU-B-07-05STER-01B"]
    assert t.expand_tags("AHU-B-07-05MOT-01 / MOTHYB-01") == ["AHU-B-07-05MOT-01", "AHU-B-07-05MOTHYB-01"]
    assert t.expand_tags("AHU-B-07-05MOTSTOR-01A/B + MOTSTORE-01") == ["AHU-B-07-05MOTSTOR-01A", "AHU-B-07-05MOTSTOR-01B", "AHU-B-07-05MOTSTORE-01"]
    assert t.expand_tags("AHU-B-07-06DSOTPACU-03A/B + -04A/B") == ["AHU-B-07-06DSOTPACU-03A", "AHU-B-07-06DSOTPACU-03B", "AHU-B-07-06DSOTPACU-04A", "AHU-B-07-06DSOTPACU-04B"]
    assert t.expand_tags("AHU-B-07-04ANGCT-01 (?)") == ["AHU-B-07-04ANGCT-01"]
    assert t.expand_tags("—") == [] and t.expand_tags("") == []
    assert t.expand_tags("AHU-B-07-07CORELB-01/02") == ["AHU-B-07-07CORELB-01", "AHU-B-07-07CORELB-02"]


def test_loose_key_ignores_level_code_pair_and_spelling():
    assert t.loose_key("AHU-B-04-04CATH-01") == t.loose_key("AHU-B-07-04CATH-01")
    assert t.loose_key("AHU-B-07-05MOTlSONP-01A") == t.loose_key("AHU-B-07-05MOTISONP-01A/B")
    assert t.loose_key("AHU-B-07-05STER-01A") != t.loose_key("AHU-B-07-05STER-02A")


def test_status():
    assert t._status("AHU-B-07-05STER-01A", ["AHU-B-07-05STER-01A"]) == "exact"
    assert t._status("AHU-B-07-05STER-01A", ["AHU-B-07-05STER-01A/B"]) == "pair form differs (A/B)"
    assert t._status("AHU-B-04-04CATH-01", ["AHU-B-07-04CATH-01"]) == "variant spelling"
    assert t._status("X", []) == "absent"


def test_compare_synthetic():
    cross = [{"crossref_row": 5, "governing_tags_expanded": "AHU-B-04-04CATH-01", "variants_replaced": "", "description_as_printed": "Cath",
              "l07_old_reference_as_printed": "AHU-B-07-04ANGCT-01 (?)", "sa_norm_l_s_as_printed": "1600", "key_remarks_as_printed": "r",
              "variants_in_evidence_cells": "", "nsc_scope": "MOT"}]
    sched = [{"tag": "AHU-B-04-04CATH-01", "supply_airflow_l_s": "1600"}]
    sch = [{"issue_set": "mot_nsc", "type": "AHU", "tag": "AHU-B-07-04CATH-01", "duty_as_written": "1700 L/s", "sheet": "S", "page": "1", "issue": "i"}]
    d = t.compare(cross, sched, sch, [], [])
    row = next(x for x in d if x["governing_tag"] == "AHU-B-04-04CATH-01")
    assert "variant spelling in MOT schematics" in row["difference_type"]
    assert "airflow differs" in row["difference_type"]
    assert row["tags_in_ac_schedule"] == "AHU-B-04-04CATH-01" and row["status"].startswith("open")


@needs_xlsx
def test_workbook_read_and_register(tmp_path):
    res = t.run(tmp_path, tmp_path)
    assert res["crossref_rows"] == 73
    import csv
    rows = list(csv.DictReader(open(tmp_path / "mot_ahu_crossref.csv", encoding="utf-8")))
    r5 = next(r for r in rows if r["crossref_row"] == "5")
    assert r5["governing_tag_as_printed"] == "AHU-B-04-04CATH-01" and r5["sa_norm_l_s_as_printed"] == "1600"
    assert r5["l07_old_reference_as_printed"].startswith("AHU-B-07-04ANGCT-01")
    assert "no formulas" in r5["notes"]
    stray = next(r for r in rows if r["crossref_row"] == "77")
    assert "no item number" in stray["notes"]
    assert (tmp_path / "mot_tag_differences.csv").is_file()
