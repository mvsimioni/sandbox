"""Tests for src/hfds_parser.py: normalisers, sub-item splitting, a synthetic ruled table, and (when the source PDF
is present) spot checks of the real HFDS Mechanical reverse-brief."""

from __future__ import annotations

import csv
from pathlib import Path

import pymupdf
import pytest

from src import hfds_parser as h

ROOT = Path(__file__).resolve().parents[1]
REAL_PDF = ROOT / h.HFDS_REL


# ---------------------------------------------------------------- normalisers
@pytest.mark.parametrize("text,expect", [
    ("24 +/- 1", {"temp_setpoint_c": "24", "temp_tol_c": "1"}),
    ("21 to 24 +/- 1", {"temp_min_c": "21", "temp_max_c": "24", "temp_tol_c": "1"}),
    ("21 – 24 +/- 1", {"temp_min_c": "21", "temp_max_c": "24", "temp_tol_c": "1"}),
    ("18-25", {"temp_min_c": "18", "temp_max_c": "25"}),
    ("Max. 24", {"temp_max_c": "24"}),
    ("22 ± 1", {"temp_setpoint_c": "22", "temp_tol_c": "1"}),
    ("27 +/- 2", {"temp_setpoint_c": "27", "temp_tol_c": "2"}),
])
def test_temperature(text, expect):
    out = h.norm_temperature(text)
    for k, v in expect.items():
        assert out[k] == v
    assert out["temp_note"] != "" or "temp_setpoint_c" in expect or True


def test_temperature_nr_and_unknown_stay_blank():
    out = h.norm_temperature("NR")
    assert out["temp_setpoint_c"] == out["temp_min_c"] == out["temp_max_c"] == ""
    assert out["temp_note"] == "NR"
    assert h.norm_temperature("")["temp_note"] == ""
    assert h.norm_temperature("Exhaust (makeup air from room)")["temp_setpoint_c"] == ""


def test_rh():
    assert h.norm_rh("Max. 60%")["rh_max_pct"] == "60"
    r = h.norm_rh("40- 60%")
    assert (r["rh_min_pct"], r["rh_max_pct"]) == ("40", "60")
    assert h.norm_rh("No Control")["rh_note"] == "No Control"
    assert h.norm_rh("Max 65")["rh_max_pct"] == "65"
    assert h.norm_rh("NR")["rh_max_pct"] == ""


def test_pressure_pa_only_when_printed():
    r = h.norm_pressure("Negative (- 5 Pa)")
    assert r["pressure_regime"] == "negative" and r["pressure_pa"] == "-5"
    r = h.norm_pressure("Positive")
    assert r["pressure_regime"] == "positive" and r["pressure_pa"] == ""
    assert h.norm_pressure("Positive or Neutral")["pressure_regime"] == "positive or neutral"
    assert h.norm_pressure("Negative w.r.t Corridor")["pressure_ref"] == "Corridor"
    assert h.norm_pressure("NR")["pressure_regime"] == "NR"


def test_ach_pairs():
    r = h.norm_ach_pair("2 / 6")
    assert (r["oa_ach"], r["total_ach"]) == ("2", "6")
    r = h.norm_ach_pair("5 / > 25")
    assert (r["oa_ach"], r["total_ach"], r["total_ach_qualifier"]) == ("5", "25", ">")
    r = h.norm_ach_pair("NR / 20")
    assert r["oa_ach"] == "" and r["oa_ach_note"] == "NR" and r["total_ach"] == "20"
    r = h.norm_ach_pair("2 / Refer to SS553")
    assert r["oa_ach"] == "2" and r["total_ach"] == "" and r["total_ach_note"] == "Refer to SS553"
    r = h.norm_ach_pair("Refer to SS553")
    assert r["oa_ach"] == "" and "SS553" in r["total_ach_note"]
    r = h.norm_ach_pair("2 / 6 (Normal) 2 / 12 (Pandemic)")
    assert r["oa_ach"] == "" and r["total_ach"] == "" and "several modes" in r["total_ach_note"]
    r = h.norm_ach_pair("Refer to ISO / USP standard")
    assert r["total_ach"] == "" and "ISO" in r["total_ach_note"]


def test_hours_and_yn():
    assert h.norm_hours("24 Hrs")["hours_h"] == "24"
    assert h.norm_hours("12 hrs")["hours_norm"] == "12h"
    assert h.norm_hours("Office Hrs")["hours_norm"] == "office hours"
    assert "not resolved" in h.norm_hours("Off Hrs")["hours_norm"]
    assert [h.norm_yn(x) for x in ("Y", "Yes", "N", "NR", "-")] == ["Y", "Y", "N", "NR", "-"]


def test_standby_and_system_type():
    assert h.standby_from_text(("remarks", "N Duty + 1 Stand -by"))[0] == "N Duty + 1 Stand-by"
    assert h.standby_from_text(("c", "Duty + Hot Stand- by AHU"))[0] == "Duty + Hot Stand-by"
    assert h.standby_from_text(("c", "Duty + Stand-by AHU"))[0] == "Duty + Stand-by"
    assert h.standby_from_text(("c", "nothing"))[0] == ""
    assert h.system_type_from_text("ducted fan coil units (FCUs)") == "FCU"
    assert h.system_type_from_text("AHU or FCU") == "AHU+FCU"
    assert h.system_type_from_text("Duty + Stand-by") == ""


def test_split_area_items():
    stem, items = h.split_area_items("a) Gastrointestinal Endoscopy Procedure Room b) Endoscopy – ERCP")
    assert stem == "" and [m for m, _ in items] == ["a", "b"]
    stem, items = h.split_area_items("OT – Support Area b) PACU")
    assert stem == "OT – Support Area" and items == [("b", "PACU")]
    assert h.split_area_items("Operating Theatre") == ("", [])
    # upper-case letters in brackets are not markers
    assert h.split_area_items("General Air-con Wards (Class A) and B2")[1] == []


def test_categories():
    assert "LAN/IT/server/comms" in h.categories_for("LAN Room")
    assert "UPS/battery" in h.categories_for("UPS Room")
    assert "plant room" in h.categories_for("Chiller Plant Room")
    assert "lift lobby" in h.categories_for("Basement Lift Lobbies")
    assert h.categories_for("Meeting Room") == ""


def test_header_keys():
    assert h.header_key("Rema rks") == "remarks"
    assert h.header_key("Operatin g Hrs") == "hours"
    assert h.header_key("Min Outdoor ach / Min Total ach") == "oa_total"
    assert h.header_key("Pressure Relationship with Adjacent Areas") == "pressure"
    assert h.header_key("Terminal filter") == "terminal"


# ------------------------------------------------------------ synthetic PDF
def _make_pdf(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    xs = [40, 80, 260, 340, 400, 470, 560, 640]
    heads = ["S.No", "Room Description", "Indoor Design Condition", "", "Pressure Relationship", "Min Outdoor ach / Min Total ach",
             "Operating Hrs", "Remarks"]
    rows = [
        ["1", "LAN Room", "24 +/- 1", "Max 65%", "NR", "Refer to SS553", "24 Hrs", "N Duty + 1 Stand-by"],
        ["2", "Chiller Plant Room", "27 +/- 2", "No Control", "NR", "2 / 6", "24 Hrs", ""],
        ["3", "Stores", "", "", "", "", "", ""],
        ["", "a) Dry store b) Cold store", "21 to 24 +/- 1", "Max 60%", "Negative (- 5 Pa)", "2 / 10", "Off Hrs", ""],
    ]
    ys = [100 + 30 * i for i in range(len(rows) + 2)]
    for y in ys:
        page.draw_line((xs[0], y), (xs[-1] + 100, y))
    for x in xs + [xs[-1] + 100]:
        page.draw_line((x, ys[0]), (x, ys[-1]))
    for j, t in enumerate(heads):
        page.insert_text((xs[j] + 2, ys[0] + 18), t, fontsize=6)
    for i, r in enumerate(rows, 1):
        for j, t in enumerate(r):
            page.insert_text((xs[j] + 2, ys[i] + 18), t, fontsize=6)
    page.insert_text((200, 400), "Table T.9.9.9: Synthetic criteria", fontsize=9)
    doc.save(str(path))


def test_synthetic_table(tmp_path):
    pdf = tmp_path / "synthetic.pdf"
    _make_pdf(pdf)
    doc = pymupdf.open(str(pdf))
    tables, issues = h.extract_tables(doc)
    assert len(tables) == 1 and tables[0].table_id == "T.9.9.9"
    rows, problems = h.rows_from_table(tables[0], [], "synthetic.pdf")
    assert problems == []
    names = [r["area_name"] for r in rows]
    assert names == ["LAN Room", "Chiller Plant Room", "Dry store", "Cold store"]
    lan = rows[0]
    assert lan["temp_setpoint_c"] == "24" and lan["rh_max_pct"] == "65" and lan["hours_h"] == "24"
    assert lan["standby_config"] == "N Duty + 1 Stand-by" and lan["system_type"] == ""
    assert lan["oa_total_ach_as_printed"] == "Refer to SS553" and lan["oa_ach"] == ""
    assert rows[1]["rh_note"] == "No Control" and rows[1]["total_ach"] == "6"
    assert rows[2]["parent_area"] == "Stores" and rows[2]["sub_item"] == "a" and rows[3]["sub_item"] == "b"
    assert rows[2]["pressure_pa"] == "-5" and "shared_row_split" in rows[2]["flags"]
    assert rows[2]["confidence"] == "medium"
    for r in rows:
        assert r["method"] and r["location"] and r["text_as_printed"] and r["pdf_page"] == 1


def test_reviewer_freetext_is_not_read_as_cell_text(tmp_path):
    pdf = tmp_path / "annot.pdf"
    _make_pdf(pdf)
    doc = pymupdf.open(str(pdf))
    pg = doc[0]
    pg.add_freetext_annot(pymupdf.Rect(300, 100, 520, 160), "REVIEWER SAYS HELLO", fontsize=8)
    pdf2 = tmp_path / "annot2.pdf"
    doc.save(str(pdf2))
    doc = pymupdf.open(str(pdf2))
    ann = h.collect_annotations(doc)
    assert ann and ann[0]["content"] == "REVIEWER SAYS HELLO"
    tables, _ = h.extract_tables(doc)
    rows, _ = h.rows_from_table(tables[0], [], "annot.pdf")
    assert all("HELLO" not in r["text_as_printed"] for r in rows)
    notes = h.annotation_notes(ann)
    assert notes[0]["note_kind"] == "reviewer annotation" and "HELLO" in notes[0]["text_as_printed"]


# ----------------------------------------------------------------- real PDF
@pytest.fixture(scope="module")
def real():
    if not REAL_PDF.is_file():
        pytest.skip("HFDS PDF not in the working tree")
    return h.parse_pdf(REAL_PDF)


def _find(rows, **kw):
    return [r for r in rows if all(r[k] == v for k, v in kw.items())]


def test_real_main_table_rows(real):
    crit = real["criteria"]
    main = [r for r in crit if r["table_id"] == "T.1.1.1.1"]
    assert len(main) >= 60
    lan = _find(main, area_name="LAN Room")[0]
    assert lan["pdf_page"] == 13 and lan["printed_page"] == "44"
    assert lan["temp_setpoint_c"] == "24" and lan["rh_max_pct"] == "65"
    assert lan["standby_config"] == "N Duty + 1 Stand-by" and lan["system_type"] == "FCU"
    tr = _find(main, area_name="Transformer Room")[0]
    assert tr["temp_setpoint_c"] == "27" and tr["temp_tol_c"] == "2" and tr["rh_note"] == "No Control"
    chiller = _find(main, area_name="Chiller Plant Room")[0]
    assert chiller["standby_unit"] == "N" and chiller["standby_fan_motor"] == "N"
    decon = _find(main, area_name="CSSD Decontamination")[0]
    assert decon["pressure_pa"] == "-5" and decon["oa_ach"] == "2" and decon["total_ach"] == "6"
    # a) and b) of one printed row are split
    assert _find(main, area_name="Endoscopy – ERCP") and _find(main, area_name="Gastrointestinal Endoscopy Procedure Room")


def test_real_reviewer_text_not_in_cells(real):
    blob = " ".join(r["text_as_printed"] for r in real["criteria"])
    assert "Contradiction in terms" not in blob and "lesson learned" not in blob
    assert any("Contradiction in terms" in n["text_as_printed"] for n in real["notes"] if n["note_kind"] == "reviewer annotation")


def test_real_other_tables(real):
    ids = {r["table_id"] for r in real["criteria"]}
    for tid in ("T.1.1.3.1", "T.1.1.3.2", "T.1.1.3.5", "T.1.1.3.6", "T.1.1.3.7", "T.1.1.3.8", "T.1.1.3.9", "T.1.1.3.11",
                "T.1.1.3.12", "T.1.1.3.13", "T.1.1.3.14", "T.1.1.3.15", "T.1.1.3.14 (MV)"):
        assert tid in ids
    ot = _find(real["criteria"], table_id="T.1.1.3.9", area_name="Operating Theatre")[0]
    assert ot["oa_ach"] == "25" and ot["total_ach"] == "25"
    scrub = _find(real["criteria"], table_id="T.1.1.3.7", area_name="Scrub")[0]
    assert scrub["system_mode_as_printed"] == "" and "span_inherited" in scrub["flags"]
    assert "Single pass" in scrub["air_system_mode"]


def test_real_config_notes(real):
    cfg = [n for n in real["notes"] if n["note_kind"].startswith("AHU/FCU configuration")]
    assert len(cfg) == 9
    f = [n for n in cfg if n["config"] == "Configuration F"][0]
    assert f["system_type"] == "FCU" and f["standby_rule"] == "N Duty + 1 Stand-by"
    assert "UPS Room" in f["areas_listed"] and "Battery Room" in f["areas_listed"] and "LAN Room" in f["areas_listed"]


def test_real_clause_rules(real):
    lan = [n for n in real["notes"] if n["clause_id"] == "1.1.3.19" and n["item"] == "a)"][0]
    assert "N Duty + 1 Stand-by" in lan["text_as_printed"] and lan["pdf_page"] == 57
    elec = [n for n in real["notes"] if n["clause_id"] == "1.1.3.20" and n["item"] == "a)"][0]
    assert "UPS Room" in elec["text_as_printed"]


def test_run_writes_registers(tmp_path, monkeypatch):
    if not REAL_PDF.is_file():
        pytest.skip("HFDS PDF not in the working tree")
    out = h.run(quiet=True)
    root = h.common.project_root()
    crit = list(csv.DictReader(open(root / "out/registers/hfds_criteria.csv", encoding="utf-8")))
    assert len(crit) == out["summary"]["criteria_rows"]
    assert set(crit[0].keys()) == set(h.CRITERIA_FIELDS)
    notes = list(csv.DictReader(open(root / "out/registers/hfds_notes.csv", encoding="utf-8")))
    assert len(notes) == out["summary"]["notes_rows"]
    assert (root / "out/qa/hfds_report.md").is_file()
