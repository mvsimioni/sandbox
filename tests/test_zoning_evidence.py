"""Tests for src.zoning.evidence and src.zoning.crops."""

import csv
import re
import shutil
from pathlib import Path

import pytest

from src.zoning import crops, evidence as ev

REAL = Path("out/registers")


def test_zone_key_merges_duty_standby():
    assert ev.zone_key("AHU-B-04-04RAD-01A/B") == "AHU-B-04-04RAD-01"
    assert ev.zone_key("AHU-B-07-04ANGIO-04A") == "AHU-B-07-04ANGIO-04"
    assert ev.zone_key("AHU-B-07-04ANGIO-04B") == "AHU-B-07-04ANGIO-04"
    assert ev.zone_key("AHU-B-02-01OPPPHARM-01A&B") == "AHU-B-02-01OPPPHARM-01"
    assert ev.zone_key("AHU-B-07-04ICU-01/02C") == "AHU-B-07-04ICU-01/02C"      # a different unit, not a pair


def test_storey_keys():
    assert ev.storey_stack_key("L04") == "4" and ev.storey_stack_key("B2") == "B2" and ev.storey_stack_key("LB3") is None
    assert ev.storey_from_keyplan("IP 4TH STOREY KEYPLAN – PART 1") == ["L04"]
    assert ev.storey_from_keyplan("IP BASEMENT 2- KEYPLAN") == ["B2"]
    assert ev.storey_from_keyplan("IP 19TH, 20TH & 21ST STOREY - KEYPLAN") == ["L19", "L20", "L21"]
    assert ev.storey_from_keyplan("OP 5TH STOREY KEYPLAN") == []


def test_canon_depts():
    assert ev.canon_depts("DIAGNOSTIC & IMAGING DEPARTMENT - CT SCAN") == {"DI"}
    assert {"ICU", "HDU"} <= ev.canon_depts("ICU / HDU 22 04")
    assert "ED" in ev.canon_depts("EMERGENCY DEPARTMENT -CAC")
    assert ev.canon_depts("MEP PLANT AREA") == {"MEP"}
    assert ev.dept_text_from_schematic_file("x/IP-BH-05B21_Rev0B_INPATIENT BASEMENT 2 AIR-SIDE SCHEMATIC - FOOD SERVICES SCHEMATIC 1.pdf").startswith("FOOD SERVICES")


def build_abb():
    abb = ev.Abbreviations()
    from collections import Counter
    phr = Counter({"CORRIDOR": 5, "CIRCULATION": 5, "FIRE LIFT LOBBY": 3, "PASSENGER LIFT LOBBY": 3, "TREATMENT ROOM": 4, "TREATMENT": 3,
                   "STORE": 9, "CHANGE ROOM": 4, "SERVICE FIRE LOBBY": 2, "WAITING": 3})
    abb.build(["Corr", "FLL", "PLL", "Trmt", "Str", "Chg | Toilet", "Wait"], phr)
    return abb


def test_abbreviations_seed_and_derived():
    abb = build_abb()
    assert "CORRIDOR" in abb.expand("CORR") and "CIRCULATION" in abb.expand("CORR")
    assert "FIRE LIFT LOBBY" in abb.expand("FLL")
    assert "PASSENGER LIFT LOBBY" in abb.expand("PLL")
    assert "TREATMENT" in abb.expand("TRMT") and "STORE" in abb.expand("STR") and "CHANGE" in abb.expand("CHG")
    assert "WAITING" in abb.expand("WAIT")
    basis = {r["abbreviation"]: r["basis"] for r in abb.rows() if r["expansion"] == "FIRE LIFT LOBBY"}
    assert basis["FLL"].startswith("seed")


def test_name_matching_scores():
    abb = build_abb()
    vs = ev.variants("Corr", abb)
    assert ev.best_q(vs, "CORRIDOR")[0] == 1.0
    assert ev.best_q(vs, "CIRCULATION / CORRIDOR / STAFF STATION")[0] == pytest.approx(0.9)
    assert ev.best_q(vs, "OPERATING ROOM")[0] == 0.0
    assert ev.best_q(ev.variants("FLL", abb), "FIRE LIFT LOBBY")[0] == 1.0
    assert ev.plan_parts("Chg | Toilet") == [("Chg", ""), ("Toilet", "")]
    assert ev.plan_parts("Str (Future Vdr)") == [("Str", "Future Vdr")]


def rooms_grid():
    # 10 rooms spread over a 100 m x 100 m footprint
    return [{"centroid_x_mm": str(x * 10000), "centroid_y_mm": str(y * 10000)} for x in range(11) for y in range(11)]


def test_wing_model_quadrants_and_cells():
    stack = [{"storey": "4", "block": "IP podium", "wing": w, "department_as_written": d, "dept_no": "", "match_confidence": ""}
             for w, d in (("NW", "MEP PLANT AREA"), ("SW", "CATH LAB"), ("NE", "ICU"), ("SE", "ICU"), ("SW-E cell", "AN - DI"), ("NE-W cell", "AN - ICU"))]
    wm = ev.WingModel(rooms_grid(), stack)
    assert wm.ok
    assert wm.wing(10000, 90000) == "NW" and wm.wing(10000, 10000) == "SW" and wm.wing(90000, 90000) == "NE" and wm.wing(90000, 10000) == "SE"
    assert wm.wing(55000, 10000) == "SW-E cell" and wm.wing(45000, 90000) == "NE-W cell"
    assert wm.departments("SW") == [("CATH LAB", "")]
    assert "ICU" in wm.storey_departments()
    assert not ev.WingModel(rooms_grid(), []).ok


def test_level_check_flags_other_storey():
    class R:
        level = {"AHU-B-03-04RAD-01": "4"}
    assert ev.level_check("AHU-B-03-04RAD-01A/B", "L04", R) == ""
    assert "serves level 4" in ev.level_check("AHU-B-03-04RAD-01A/B", "L05", R)
    assert ev.level_check("AHU-B-07-02COR-01", "L02", type("R", (), {"level": {}})) == ""


def test_classify_conflict_and_none():
    c1 = ev.Cand("schematic", "AHU", "AHU-B-01", "AHU-B-01", 0.7, "e", "r")
    c2 = ev.Cand("booklet", "AHU", "AHU-B-02", "AHU-B-02", 0.6, "e", "r")
    m = ev.Cand("mode", "MV", "MV", "MV", 0.6, "e", "r")
    assert ev.classify({"mode": "AC"}, [c1])["status"] == "one strong candidate"
    r = ev.classify({"mode": "AC"}, [c1, c2])
    assert r["status"] == "conflict" and r["n_ahu_strong"] == 2
    assert ev.classify({"mode": "MV"}, [c1, m])["status"] == "conflict"          # MV label against an AHU candidate
    assert ev.classify({"mode": "MV"}, [m])["status"] == "class from mode label only"
    assert ev.classify({"mode": ""}, [ev.Cand("mode", "none", "", "", 0.0, "e", "r")])["status"] == "none"
    weak = ev.Cand("schematic", "AHU", "AHU-B-03", "AHU-B-03", 0.2, "e", "r")
    assert ev.classify({"mode": "AC"}, [weak])["status"] == "weak candidates only"


@pytest.mark.skipif(not (REAL / "room_polygons_L04.csv").exists(), reason="registers not present")
def test_run_storey_l04_in_tmp(tmp_path, monkeypatch):
    for f in REAL.glob("*.csv"):
        if f.name.startswith(("zoning_evidence",)):
            continue
        shutil.copy(f, tmp_path / f.name)
    monkeypatch.setattr(ev, "REG", tmp_path)
    ref = ev.Reference()
    abb = ev.Abbreviations()
    names = [r["room_name"] for r in ev.read_csv(tmp_path / "room_polygons_L04.csv")]
    abb.build(names, ref.phrases)
    res = ev.run_storey("L04", ref, abb)
    rows = ev.read_csv(tmp_path / "zoning_evidence_L04.csv")
    rooms = ev.read_csv(tmp_path / "zoning_evidence_rooms_L04.csv")
    assert res["rooms"] == len(rooms) == len(ev.read_csv(tmp_path / "room_polygons_L04.csv"))
    assert {r["room_id"] for r in rows} == {r["room_id"] for r in rooms}                    # every room has at least the mode row
    assert set(ev.COLUMNS) == set(rows[0].keys())
    assert all(0.0 <= float(r["score"]) <= 0.98 for r in rows)
    # the RFI sample rooms carry their governing unit as the best candidate
    sample = [r for r in ev.read_csv(tmp_path / "zone_assignment_L04.csv") if "RFI-ME-0233" in r["method"] and r["class_kind"] == "AHU"]
    best = {r["room_id"]: r["best_ahu"] for r in rooms}
    assert sample and all(ev.zone_key(s["class"]) == best[s["room_id"]] for s in sample)
    assert res["rooms_with_ahu_candidate"] + res["rooms_without_any_ahu_candidate"] == res["rooms"]


@pytest.mark.skipif(not (REAL / "zoning_evidence_L04.csv").exists(), reason="evidence not run")
def test_evidence_text_has_no_internal_terms():
    import yaml
    bad = yaml.safe_load(open("config/project.yaml"))["deliverable_language"]["forbidden_terms"]
    rx = [re.compile(r"\b" + re.escape(b) + r"\b", re.I if len(b) > 3 else 0) for b in bad]
    for f in REAL.glob("zoning_evidence_[A-Z]*.csv"):
        if "_rooms_" in f.name or "summary" in f.name:
            continue
        for r in ev.read_csv(f):
            for col in ("evidence", "source_ref", "flags"):
                text = r[col]
                if '"' in text:                       # quoted document text may use any word
                    text = re.sub(r'"[^"]*"', "", text)
                assert not any(x.search(text) for x in rx), (f.name, col, r[col])
                assert "out/registers" not in text and "data/source" not in text


def test_crops_wkt_and_ids():
    rings = crops.parse_wkt_rings("POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0), (2 2, 4 2, 4 4, 2 2))")
    assert len(rings) == 2 and rings[0][2] == (10.0, 10.0)
    assert crops.parse_wkt_rings("POLYGON EMPTY") == []


@pytest.mark.skipif(not Path(crops.BG_PDF).exists() or not (REAL / "room_polygons_L04.csv").exists(), reason="inputs not present")
def test_crops_render(tmp_path):
    out = tmp_path / "c.png"
    res = crops.render("L04", ["L04-R003", "4"], str(out), dpi=150)
    assert out.exists() and res["width_px"] > 100 and res["rooms"] == ["L04-R003", "L04-R004"]
    with pytest.raises(KeyError):
        crops.pick_rooms("L04", ["L04-R9999"])
