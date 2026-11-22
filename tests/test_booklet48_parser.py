"""Tests for src/booklet48_parser.py, using synthetic PDFs built with pymupdf.

The real booklets are never opened here.  The fixtures reproduce the layout traits the
parser relies on: centred header cells with wrapped titles and a small footnote
superscript, line-wrapped room names, U+200B in cell text, a section row inside the
table, a negative and a positive regime, NR cells, a notes block, and a standby table
whose 'Level NN wing' heading is a merged cell centred on its group of rows.
"""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

import pymupdf
import pytest

from src import booklet48_parser as b

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
FONT_FILE = next((f for f in FONT_CANDIDATES if Path(f).is_file()), None)
pytestmark = pytest.mark.skipif(FONT_FILE is None, reason="no TrueType font with U+200B available")

ZW = "​"
W, H = 1190.0, 842.0


class Canvas:
    """Small drawing helper: centred or left-aligned text on a page."""

    def __init__(self, doc: "pymupdf.Document"):
        self.page = doc.new_page(width=W, height=H)
        self.page.insert_font(fontname="f", fontfile=FONT_FILE)
        self.font = pymupdf.Font(fontfile=FONT_FILE)

    def text(self, x: float, y: float, s: str, size: float = 12.0, centre: bool = False) -> None:
        """Baseline at y + 0.8 * size so ``y`` is the top of the glyph box."""
        if centre:
            x -= self.font.text_length(s, fontsize=size) / 2.0
        self.page.insert_text((x, y + size * 0.85), s, fontname="f", fontsize=size)

    def rect(self, x0: float, y0: float, x1: float, y1: float, fill=(0.8, 0.85, 0.95)) -> None:
        self.page.draw_rect(pymupdf.Rect(x0, y0, x1, y1), color=None, fill=fill)


COLS = [("room", 50, 250), ("pressure", 250, 340), ("oa", 340, 420), ("total", 420, 500),
        ("single", 500, 580), ("aclass", 580, 660), ("filter", 660, 740), ("rh", 740, 840),
        ("temp", 840, 940), ("after", 940, 1040), ("setback", 1040, 1140)]
CENTRE = {k: (a + b_) / 2.0 for k, a, b_ in COLS}


def _footer(cv: Canvas, number: str) -> None:
    cv.text(60, 805, "ALEXANDRA HOSPITAL REDEVELOPMENT PROJECT | DETAILED DESIGN BOOKLET", 11)
    cv.text(1150, 808, number, 14)


def build_criteria_page(doc: "pymupdf.Document", booklet_page: str = "77", first_total: str = "8",
                        interview: bool = False, temp_wrapped: bool = False, pa_rows: bool = False,
                        star_rows: bool = False, caption: str = "Room conditions – Part 3",
                        low_row: bool = False, low_note: bool = False) -> None:
    cv = Canvas(doc)
    cv.text(60, 100, "DESIGN CRITERIA", 14)
    cv.rect(50, 130, 1140, 200, fill=(0.35, 0.6, 0.83))
    head = {
        "room": ["Room"], "pressure": ["Pressure", "Regime, Pa"], "oa": ["Min.", "Outdoor", "ACH"],
        "total": ["Min.", "Total ACH"], "single": ["Single", "Pass"], "aclass": ["Air Class"],
        "filter": ["Filter"], "rh": ["Relative", "Humidity"],
        "temp": ["Design", "Temperatu", "re, C"] if temp_wrapped else ["Design", "Temperature", ", C"],
        "after": ["Afterhours", "mode, Y/N"], "setback": ["Setback", "mode, Y/N"],
    }
    for key, lines in head.items():
        top = 165 - 9 * len(lines)
        for i, line in enumerate(lines):
            cv.text(CENTRE[key], top + 17 * i, line, 14, centre=True)
    cv.text(CENTRE["after"] + 36, 178, "1", 9)          # footnote superscript, smaller type

    def row(y: float, room_lines, cells) -> None:
        mid = y + 8 + 9 * (len(room_lines) - 1)
        for i, line in enumerate(room_lines):
            cv.text(60, y + 18 * i, line, 14)
        for key, val in cells.items():
            cv.text(CENTRE[key], mid - 8, val, 14, centre=True)

    base = {"filter": "MERV 15", "rh": "Max 60%", "temp": "21 – 24", "after": "N", "setback": "N",
            "aclass": "2" + ZW, "single": "NR" + ZW}
    row(215, ["Bed Room" + ZW], {**base, "pressure": "Neutral" + ZW, "oa": "2" + ZW, "total": first_total + ZW})
    row(245, ["1 Bed Room (Iso Type B, A", "Class)"], {**base, "pressure": "Neutral", "oa": "2", "total": "8",
                                                       "temp": "21 – 30"})
    row(290, ["Ensuite / Toilet"], {"pressure": "Negative", "oa": "-", "total": "20" + ZW, "single": "Yes",
                                    "aclass": "4", "filter": "MERV 8", "rh": "NR", "temp": "NR",
                                    "after": "N", "setback": "N"})
    row(320, ["Isolation Room"], {**base, "pressure": "-15 Pa", "oa": "12", "total": "12", "single": "Yes"})
    row(350, ["Procedure Room"], {**base, "pressure": "Positive", "oa": "2", "total": "15"})
    row(380, ["Plant Room"], {**base, "pressure": "+10 Pa", "oa": "NR", "total": "6", "temp": "27"})
    cv.text(CENTRE["aclass"] + 20, 412, "Pandemic Mode", 14, centre=True)     # section row
    row(440, ["Ward"], {**base, "pressure": "Neutral", "oa": "12", "total": "12"})
    if interview:
        # the real 'Interview Room' row: the room text ends with a space and a zero-width space that
        # sits far to the right, so a plain word box for the neighbouring value reaches into the Room column
        cv.text(60, 500, "Interview Room " + ZW, 14)
        cv.text(CENTRE["pressure"], 500, "Neutral", 14, centre=True)
        for key, val in {**base, "oa": "2", "total": "6"}.items():
            if key not in ("pressure",):
                cv.text(CENTRE[key], 500, val, 14, centre=True)
    if pa_rows:      # Pa figures: a split sign, a range, a range across zero
        row(540, ["Iso Anteroom"], {**base, "pressure": "- 15", "oa": "10", "total": "10", "single": "Yes"})
        row(570, ["Iso Range Room"], {**base, "pressure": "-10 to -15", "oa": "12", "total": "12", "single": "Yes"})
        row(600, ["Swing Room"], {**base, "pressure": "-5 to +5", "oa": "2", "total": "6"})
    if star_rows:
        row(540, ["Iso Anteroom*"], {**base, "pressure": "+ 15", "oa": "10", "total": "10", "single": "Yes"})
        cv.text(60, 600, "* One out of six wards to provide positive anteroom", 14)
        cv.text(70, 618, "and negative bedroom with HEPA supply", 14)
    if low_row:      # the last row prints on the line of the footer stamp (below 0.93 of the page height)
        row(795, ["Cleaner Room"], {**base, "pressure": "Neutral", "oa": "2", "total": "6", "single": "Yes"})
    elif low_note:   # the NOTE text runs on below 0.93 of the page height, towards the footer stamp
        cv.text(60, 745, "NOTE:-", 14)
        cv.text(85, 763, "1 – AFTERHOURS MODE APPLIES TO DEPARTMENTS THAT DO NOT OPERATE", 14)
        cv.text(85, 781, "CONTINUOUSLY (24/7) BUT HAVE SPECIFIC ROOMS", 14)
        cv.text(85, 797, "REQUIRING AIR CONDITIONING. ALLOWANCE FOR PUSH-BUTTON.", 14)
    else:
        cv.text(60, 740, "NOTE:-", 14)
        cv.text(85, 758, "1 – AFTERHOURS MODE APPLIES TO DEPARTMENTS THAT DO NOT OPERATE", 14)
        cv.text(85, 776, "CONTINUOUSLY (24/7) BUT HAVE SPECIFIC ROOMS.", 14)
    if caption:
        cv.text(60, 40, caption, 30)
    _footer(cv, booklet_page)


def build_standby_page(doc: "pymupdf.Document", booklet_page: str = "78") -> None:
    cv = Canvas(doc)
    cv.text(60, 60, "EQUIPMENT STANDBY CONFIGURATION SUMMARY", 20)
    cv.rect(64, 148, 296, 196, fill=(0.35, 0.6, 0.83))
    cv.rect(296, 148, 527, 196, fill=(0.35, 0.6, 0.83))
    cv.rect(527, 148, 758, 196, fill=(0.35, 0.6, 0.83))
    cv.rect(758, 148, 939, 196, fill=(0.35, 0.6, 0.83))
    cv.text(180, 165, "IP TEST", 14, centre=True)
    cv.text(411, 165, "Equipment Tag", 14, centre=True)
    cv.text(642, 165, "Area", 14, centre=True)
    cv.text(848, 152, "Equipment Standby", 14, centre=True)
    cv.text(848, 172, "Configuration 1,2", 14, centre=True)
    groups = [("Level 5 NW wing" + ZW, [("AHU-B-05-05TESTNWWG-2", "Single Bedroom", "AHU-CONFIG-E"),
                                        ("AHU-B-05-05TESTNWWG-1", "Subsidized Wards", "AHU-CONFIG-E"),
                                        ("AHU-B-05-05TESTNWCR-1", "Clinical Support Area", "AHU-CONFIG-E")]),
              ("Level 5 SW wing", [("AHU-B-05-05TESTSWWG-1", "1 & 4 Bedroom", "AHU-CONFIG-F"),
                                   ("AHU-B-05-05TESTSWCR-1", "Clinical Support Area", "AHU-CONFIG-E")])]
    y = 196.0
    for heading, rows in groups:
        height = 29.0 * len(rows)
        cv.rect(64, y, 296, y + height, fill=(0.82, 0.87, 0.94))
        cv.text(180, y + height / 2.0 - 8, heading, 14, centre=True)
        for i, (tag, area, config) in enumerate(rows):
            top = y + 29.0 * i + 6
            cv.text(303, top, tag, 14)
            cv.text(534, top, area, 14)
            cv.text(814, top, config, 14)
        y += height
    cv.text(85, 740, "NOTE:-", 14)
    cv.text(85, 758, "1 – REFER TO APPENDIX 7 FOR EQUIPMENT STANDBY CONFIGURATION TYPES", 14)
    _footer(cv, booklet_page)


def build_zoning_page(doc: "pymupdf.Document", legend: bool) -> None:
    cv = Canvas(doc)
    cv.text(900, 150, "IP 5TH STOREY - KEYPLAN", 14)
    cv.text(300, 300, "AHU-B-05-05TESTNWWG-1", 12)
    cv.text(400, 400, "6 BED ROOM:", 10)
    cv.text(400, 412, "B2 CLASS", 10)
    cv.text(500, 500, "STAFF", 10)
    if legend:
        cv.rect(900, 700, 920, 714, fill=(1.0, 0.0, 0.0))
        cv.text(930, 700, "Duty zone", 12)
    _footer(cv, "79")


def build_pressure_page(doc: "pymupdf.Document") -> None:
    cv = Canvas(doc)
    cv.text(100, 60, "PRESSURE REGIME (PANDEMIC MODE)", 20)
    cv.text(300, 300, "WARD", 12)
    cv.text(300, 330, "-5", 12)
    cv.text(380, 330, "+15 Pa", 12)
    _footer(cv, "80")


@pytest.fixture()
def booklet(tmp_path: Path) -> Path:
    doc = pymupdf.open()
    build_criteria_page(doc)
    build_standby_page(doc)
    build_zoning_page(doc, legend=False)
    build_zoning_page(doc, legend=True)
    build_pressure_page(doc)
    cv = Canvas(doc)
    cv.text(100, 100, "Something else entirely", 14)
    _footer(cv, "81")
    sensor = Canvas(doc)
    sensor.text(100, 100, "Room Temperature", 14)
    sensor.text(100, 120, "Sensor @ 1500 mm AFFL", 14)
    _footer(sensor, "82")
    path = tmp_path / "IP_TEST_Section_4.8_Mechanical_Ventilation_Strategy.pdf"
    doc.save(str(path))
    return path


def _parse(path: Path, tmp_path: Path):
    return b.parse_pdf(path, tmp_path)


# --------------------------------------------------------------------------- #
def test_page_classes(booklet: Path, tmp_path: Path) -> None:
    res = _parse(booklet, tmp_path)
    assert [p["class"] for p in res["pages"]] == [
        "criteria", "standby summary", "zoning keyplan", "zoning keyplan", "pressure regime", "other",
        "sensor schedule"]
    assert [p["booklet_page"] for p in res["pages"]][:5] == ["77", "78", "79", "79", "80"]


def test_zero_width_space_stripped_everywhere(booklet: Path, tmp_path: Path) -> None:
    res = _parse(booklet, tmp_path)
    for key in ("criteria", "standby", "zoning", "pressure", "notes"):
        for row in res[key]:
            assert all(ZW not in str(v) for v in row.values()), row
    assert b.clean("Room%s  x y" % ZW) == "Room x y"


def test_criteria_rows_by_column_range(booklet: Path, tmp_path: Path) -> None:
    rows = {r["room_type_as_printed"]: r for r in _parse(booklet, tmp_path)["criteria"]}
    assert list(rows) == ["Bed Room", "1 Bed Room (Iso Type B, A Class)", "Ensuite / Toilet",
                          "Isolation Room", "Procedure Room", "Plant Room", "Ward"]
    wrapped = rows["1 Bed Room (Iso Type B, A Class)"]       # wrapped cell joined by row band
    assert wrapped["temp_as_printed"] == "21 – 30" and wrapped["temp_min_c"] == "21" and wrapped["temp_max_c"] == "30"
    assert wrapped["pressure_direction"] == "neutral" and wrapped["pressure_pa"] == ""
    assert wrapped["room_type_norm"] == "1 bed room (iso type b, a class)"
    first = rows["Bed Room"]
    assert (first["booklet_page"], str(first["pdf_page"]), first["table_part"]) == ("77", "1", "Part 3")
    assert first["min_oa_ach"] == "2" and first["min_total_ach"] == "8" and first["air_class"] == "2"
    assert first["filter"] == "MERV 15" and first["rh_max_pct"] == "60" and first["rh_min_pct"] == ""
    assert first["afterhours"] == "N" and first["setback"] == "N"
    assert first["note_refs"] == "1"                          # header footnote on the afterhours column
    assert len(first["bbox"].split(",")) == 4 and 0.9 <= float(first["confidence"]) <= 1.0


def test_negative_positive_and_nr_cells(booklet: Path, tmp_path: Path) -> None:
    rows = {r["room_type_as_printed"]: r for r in _parse(booklet, tmp_path)["criteria"]}
    neg_word = rows["Ensuite / Toilet"]
    assert neg_word["pressure_direction"] == "negative" and neg_word["pressure_pa"] == ""
    assert neg_word["min_oa_ach"] == "" and json_raw(neg_word)["min_oa"] == "-"   # as printed kept
    assert neg_word["single_pass"] == "Y" and neg_word["rh_as_printed"] == "NR" and neg_word["rh_max_pct"] == ""
    assert neg_word["temp_as_printed"] == "NR" and neg_word["temp_min_c"] == ""
    neg_pa = rows["Isolation Room"]
    assert neg_pa["pressure_pa"] == "-15" and neg_pa["pressure_direction"] == "negative"
    assert neg_pa["pressure_regime_as_printed"] == "-15 Pa"
    assert rows["Procedure Room"]["pressure_direction"] == "positive"
    pos_pa = rows["Plant Room"]
    assert pos_pa["pressure_pa"] == "10" and pos_pa["pressure_direction"] == "positive"
    assert pos_pa["min_oa_ach"] == "" and json_raw(pos_pa)["min_oa"] == "NR"
    assert pos_pa["temp_min_c"] == "27" and pos_pa["temp_max_c"] == "27"      # single value
    assert rows["Bed Room"]["single_pass"] == "NR"


def test_section_row_applies_to_rows_below(booklet: Path, tmp_path: Path) -> None:
    rows = _parse(booklet, tmp_path)["criteria"]
    assert rows[-1]["room_type_as_printed"] == "Ward"
    assert rows[-1]["table_part"] == "Part 3; Pandemic Mode"
    assert all(";" not in r["table_part"] for r in rows[:-1])


def test_notes_verbatim_and_wrapped(booklet: Path, tmp_path: Path) -> None:
    notes = _parse(booklet, tmp_path)["notes"]
    assert len(notes) == 1 and notes[0]["note_number"] == "1" and notes[0]["page"] == "77"
    assert notes[0]["text"] == ("AFTERHOURS MODE APPLIES TO DEPARTMENTS THAT DO NOT OPERATE "
                                "CONTINUOUSLY (24/7) BUT HAVE SPECIFIC ROOMS.")


def test_no_field_failures_on_clean_fixture(booklet: Path, tmp_path: Path) -> None:
    res = _parse(booklet, tmp_path)
    assert res["failures"] == [] and res["page_failures"] == []


def test_unparseable_cell_is_reported(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_criteria_page(doc, first_total="see note")      # not a number in a numeric column
    path = tmp_path / "IP_BAD_Section_4.8_X.pdf"
    doc.save(str(path))
    res = b.parse_pdf(path, tmp_path)
    assert any(f["field"] == "min_total" for f in res["failures"])
    bad = res["criteria"][0]
    assert bad["min_total_ach"] == "" and float(bad["confidence"]) < 0.8


def test_standby_rows_with_merged_heading(booklet: Path, tmp_path: Path) -> None:
    rows = _parse(booklet, tmp_path)["standby"]
    assert [r["tag_as_written"] for r in rows] == [
        "AHU-B-05-05TESTNWWG-2", "AHU-B-05-05TESTNWWG-1", "AHU-B-05-05TESTNWCR-1",
        "AHU-B-05-05TESTSWWG-1", "AHU-B-05-05TESTSWCR-1"]
    assert [r["level_wing_heading"] for r in rows] == ["Level 5 NW wing"] * 3 + ["Level 5 SW wing"] * 2
    assert rows[0]["area"] == "Single Bedroom" and rows[0]["configuration"] == "AHU-CONFIG-E"
    assert rows[3]["area"] == "1 & 4 Bedroom" and rows[3]["configuration"] == "AHU-CONFIG-F"
    assert rows[0]["tag_normalised"] == "AHU-B-05-05TESTNWWG-2" and rows[0]["booklet_page"] == "78"
    assert len(rows[0]["bbox"].split(",")) == 4 and rows[0]["department"] == "IP TEST"


def test_standby_note_refs_and_notes(booklet: Path, tmp_path: Path) -> None:
    res = _parse(booklet, tmp_path)
    assert {r["note_refs"] for r in res["standby"]} == {"1,2"}      # printed beside the Configuration header
    assert [(n["note_number"], n["text"]) for n in res["standby_notes"]] == [
        ("1", "REFER TO APPENDIX 7 FOR EQUIPMENT STANDBY CONFIGURATION TYPES")]
    assert res["standby_notes"][0]["pdf_page"] == 2 and res["standby_notes"][0]["booklet_page"] == "78"


def test_value_word_reaching_into_room_column(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_criteria_page(doc, interview=True)
    path = tmp_path / "IP_TEST_Section_4.8_Mechanical_Ventilation_Strategy.pdf"
    doc.save(str(path))
    # a plain pymupdf word box for the value starts inside the Room column (the zero-width space is part of it)
    plain = [w for w in pymupdf.open(str(path))[0].get_text("words") if w[4].strip(ZW) == "Neutral"
             and 495 < w[1] < 520]
    assert plain and plain[0][0] < 250
    rows = {r["room_type_as_printed"]: r for r in _parse(path, tmp_path)["criteria"]}
    row = rows["Interview Room"]
    assert row["pressure_regime_as_printed"] == "Neutral" and row["pressure_direction"] == "neutral"
    assert row["min_total_ach"] == "6" and row["filter"] == "MERV 15"


def test_normalise_tag() -> None:
    assert b.normalise_tag(" ahu – b-05 - 05X%s-1 " % ZW) == "AHU-B-05-05X-1"


def test_zoning_labels_and_legend_flag(booklet: Path, tmp_path: Path) -> None:
    res = _parse(booklet, tmp_path)
    by_page: dict = {}
    for r in res["zoning"]:
        by_page.setdefault(r["pdf_page"], []).append(r)
    assert set(by_page) == {3, 4}
    first = by_page[3]
    assert all(r["keyplan_title"] == "IP 5TH STOREY - KEYPLAN" for r in first)
    kinds = {(r["label_kind"], r["text"]) for r in first}
    assert ("equipment_tag", "AHU-B-05-05TESTNWWG-1") in kinds
    assert ("room_label", "6 BED ROOM:") in kinds and ("room_label", "STAFF") in kinds
    assert ("keyplan_title", "IP 5TH STOREY - KEYPLAN") in kinds
    assert {r["has_colour_legend"] for r in first} == {"N"}
    assert {r["has_colour_legend"] for r in by_page[4]} == {"Y"}
    assert all(len(r["bbox"].split(",")) == 4 for r in first)


def test_pressure_labels(booklet: Path, tmp_path: Path) -> None:
    labels = _parse(booklet, tmp_path)["pressure"]
    got = {(r["label_kind"], r["text"]) for r in labels}
    assert ("text_label", "WARD") in got and ("pressure_value", "-5") in got and ("pressure_value", "+15 Pa") in got
    assert all(r["page_heading"] == "PRESSURE REGIME (PANDEMIC MODE)" for r in labels)
    assert not any(r["text"] == "80" for r in labels)      # the printed page number is not a label


def _annotated_pressure_booklet(tmp_path: Path) -> Path:
    doc = pymupdf.open()
    build_pressure_page(doc)
    page = doc[0]
    annot = page.add_freetext_annot(pymupdf.Rect(100, 400, 420, 470),
                                    "Sign-off does not include endorsement for the ACMV/Pressure Regime portion", fontsize=11)
    annot.set_info(title="reviewer", content="Sign-off does not include endorsement for the ACMV/Pressure Regime portion")
    annot.update()
    path = tmp_path / "IP_TEST_Section_4.8_Mechanical_Ventilation_Strategy.pdf"
    doc.save(str(path))
    return path


def test_pressure_annotations_kept_apart_from_drawing_labels(tmp_path: Path) -> None:
    labels = _parse(_annotated_pressure_booklet(tmp_path), tmp_path)["pressure"]
    ann = [r for r in labels if r["label_kind"] == "reviewer_annotation"]
    assert len(ann) == 1 and ann[0]["text"].startswith("Sign-off does not include endorsement")
    assert ann[0]["annotation_author"] == "reviewer" and len(ann[0]["bbox"].split(",")) == 4
    drawing = {(r["label_kind"], r["text"]) for r in labels if r["label_kind"] != "reviewer_annotation"}
    assert drawing == {("text_label", "WARD"), ("pressure_value", "-5"), ("pressure_value", "+15 Pa")}
    assert not any("Sign-off" in r["text"] for r in labels if r["label_kind"] != "reviewer_annotation")


def test_raster_colour_legend_is_flagged(tmp_path: Path) -> None:
    doc = pymupdf.open()
    cv = Canvas(doc)
    cv.text(900, 150, "IP 5TH STOREY - KEYPLAN", 14)
    cv.text(300, 300, "AHU-B-05-05TESTNWWG-1", 12)
    sw = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 160, 90), False)
    sw.set_rect(sw.irect, (255, 255, 255))
    sw.set_rect(pymupdf.IRect(5, 10, 40, 25), (100, 240, 240))
    sw.set_rect(pymupdf.IRect(5, 40, 40, 55), (250, 180, 130))
    cv.page.insert_image(pymupdf.Rect(960, 600, 1120, 690), pixmap=sw)
    _footer(cv, "79")
    path = tmp_path / "IP_TEST_Section_4.8_Mechanical_Ventilation_Strategy.pdf"
    doc.save(str(path))
    rows = _parse(path, tmp_path)["zoning"]
    assert {r["has_colour_legend"] for r in rows} == {"Y"} and {r["legend_source"] for r in rows} == {"image"}
    assert rows[0]["legend_bbox"].startswith("960.0,600.0")


def test_cell_parsers() -> None:
    assert b.parse_pressure("+15 Pa") == ("15", "positive", True)
    assert b.parse_pressure("-5 Pa") == ("-5", "negative", True)
    assert b.parse_pressure("Negative") == ("", "negative", True)
    assert b.parse_pressure("NR") == ("", "", True)
    assert b.parse_pressure("15")[2] is False                         # unsigned number: direction unknown
    assert b.parse_rh("40 – 60%") == ("60", "40", True) and b.parse_rh("Max 60%") == ("60", "", True)
    assert b.parse_temp("24+-1") == ("23", "25", True) and b.parse_temp("abc")[2] is False
    assert b.parse_single_pass("Yes") == ("Y", True) and b.parse_single_pass("NR") == ("NR", True)
    assert b.classify_page("Room Temperature \nSensor @ 1500") == "sensor schedule"
    assert b.department_from_name(Path("IP_FAST_Section_4.8_Mechanical.pdf")) == "IP FAST"


def test_cli_run_writes_registers_and_report(booklet: Path, tmp_path: Path, monkeypatch, capsys) -> None:
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    shutil.copy(Path(__file__).resolve().parents[1] / "config" / "project.yaml", root / "config" / "project.yaml")
    folder = root / "data" / "source" / b.BOOKLET_DIR_NAME
    folder.mkdir(parents=True)
    shutil.copy(booklet, folder / booklet.name)
    shutil.copy(booklet, folder / "IP_OTHER_Section_4.8_Mechanical_Ventilation_Strategy.pdf")
    monkeypatch.setenv("AIGH_ROOT", str(root))
    assert b.main(["run"]) == 0
    reg = root / "out" / "registers"
    with (reg / "criteria.csv").open(encoding="utf-8", newline="") as fh:
        crit = list(csv.DictReader(fh))
    assert list(crit[0].keys()) == b.CRITERIA_FIELDS
    assert len(crit) == 14 and {r["department"] for r in crit} == {"IP TEST", "IP OTHER"}
    for name, expect in (("criteria_notes", 2), ("booklet_equipment_standby", 10), ("booklet_zoning_labels", None),
                         ("booklet_pressure_labels", None)):
        with (reg / (name + ".csv")).open(encoding="utf-8", newline="") as fh:
            n = len(list(csv.DictReader(fh)))
        assert n > 0 and (expect is None or n == expect), name
    report = (root / "out" / "qa" / "booklet48_parser_report.md").read_text(encoding="utf-8")
    assert "Page map" in report and "Differences between departments" in report
    assert "identical on all" in report
    # --paths takes a single file
    assert b.main(["run", "--paths", str(booklet)]) == 0
    with (reg / "criteria.csv").open(encoding="utf-8", newline="") as fh:
        assert len(list(csv.DictReader(fh))) == 7


def json_raw(row: dict) -> dict:
    import json
    return json.loads(row["raw_cells"])


# --------------------------------------------------------------------------- #
# Second batch: Pa values and ranges, header variants, room data table, annotations over tables,
# NOTE blocks on pressure pages, row rectangles, tag-less standby tables
# --------------------------------------------------------------------------- #
def _save(doc: "pymupdf.Document", tmp_path: Path, name: str = "IP_TEST_Section_4.8_Mechanical_Ventilation_Strategy.pdf") -> Path:
    path = tmp_path / name
    doc.save(str(path))
    return path


def test_pressure_pa_values_and_ranges() -> None:
    def got(cell: str):
        r = b.parse_pressure_full(cell)
        return r["pa"], r["pa_min"], r["pa_max"], r["direction"], r["ok"]

    assert got("-5") == ("-5", "-5", "-5", "negative", True)
    assert got("- 15") == ("-15", "-15", "-15", "negative", True)         # sign split from the figure
    assert got("+10 Pa") == ("10", "10", "10", "positive", True)
    assert got("-10 to -15") == ("", "-15", "-10", "negative", True)         # a range has no single value
    assert got("−10 ~ −15 Pa") == ("", "-15", "-10", "negative", True)
    assert got("-5 to +5") == ("", "-5", "5", "mixed", True)
    assert got("Negative 10 to 15") == ("", "-15", "-10", "negative", True)  # sign from the word
    assert got("10 to 15")[4] is False and got("15")[4] is False          # no sign, no word: reported
    assert got("Negative") == ("", "", "", "negative", True) and got("NR") == ("", "", "", "", True)
    assert b.parse_pressure("+15 Pa") == ("15", "positive", True)          # the older call is unchanged


def test_pa_cells_reach_the_criteria_rows(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_criteria_page(doc, pa_rows=True)
    rows = {r["room_type_as_printed"]: r for r in _parse(_save(doc, tmp_path), tmp_path)["criteria"]}
    anteroom, ranged, swing = rows["Iso Anteroom"], rows["Iso Range Room"], rows["Swing Room"]
    assert anteroom["pressure_regime_as_printed"] == "- 15" and anteroom["pressure_pa"] == "-15"
    assert (anteroom["pressure_pa_min"], anteroom["pressure_pa_max"]) == ("-15", "-15")
    assert ranged["pressure_regime_as_printed"] == "-10 to -15" and ranged["pressure_pa"] == ""
    assert (ranged["pressure_pa_min"], ranged["pressure_pa_max"], ranged["pressure_direction"]) == ("-15", "-10", "negative")
    assert (swing["pressure_pa_min"], swing["pressure_pa_max"], swing["pressure_direction"]) == ("-5", "5", "mixed")
    word = rows["Bed Room"]                                                # words only: no Pa figures
    assert (word["pressure_pa"], word["pressure_pa_min"], word["pressure_pa_max"]) == ("", "", "")
    assert b.CRITERIA_FIELDS.index("pressure_pa_min") == b.CRITERIA_FIELDS.index("pressure_pa") + 1


def test_wrapped_temperature_heading_is_mapped(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_criteria_page(doc, temp_wrapped=True)       # 'Temperatu' / 're, C', as in the ISO AH booklet
    res = _parse(_save(doc, tmp_path), tmp_path)
    assert res["page_failures"] == [] and len(res["criteria"]) == 7
    assert res["criteria"][0]["temp_as_printed"] == "21 – 24" and res["criteria"][0]["temp_max_c"] == "24"


def test_caption_without_part_number(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_criteria_page(doc, caption="Room conditions")
    res = _parse(_save(doc, tmp_path), tmp_path)
    assert res["page_failures"] == []
    assert {r["table_part"].split(";")[0] for r in res["criteria"]} == {"Room conditions (unnumbered)"}
    doc = pymupdf.open()
    build_criteria_page(doc, caption="")
    assert any("caption" in msg for _, msg in _parse(_save(doc, tmp_path, "IP_NOCAP_Section_4.8_X.pdf"), tmp_path)["page_failures"])


def test_asterisk_footnote_is_a_note_not_a_row(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_criteria_page(doc, star_rows=True)
    res = _parse(_save(doc, tmp_path), tmp_path)
    rows = {r["room_type_as_printed"]: r for r in res["criteria"]}
    assert len(res["criteria"]) == 8 and "Iso Anteroom*" in rows
    star = rows["Iso Anteroom*"]
    assert star["room_type_norm"] == "iso anteroom" and "*" in star["note_refs"].split(";")
    assert star["pressure_pa"] == "15" and star["pressure_direction"] == "positive"
    foot = [n for n in res["notes"] if n["note_number"] == "*"]
    assert len(foot) == 1 and foot[0]["note_kind"] == "table note"
    assert foot[0]["text"] == "One out of six wards to provide positive anteroom and negative bedroom with HEPA supply"
    assert not any("One out of six" in r["room_type_as_printed"] for r in res["criteria"])


def build_banded_criteria_page(doc: "pymupdf.Document") -> None:
    """Room column drawn as one rectangle per row: a room name alone in its row ('Laundry') and a tall row whose
    Filter cell is wrapped over four lines, the first of them well outside 14 points of the value line."""
    cv = Canvas(doc)
    cv.text(60, 100, "DESIGN CRITERIA", 14)
    cv.rect(50, 130, 1140, 200, fill=(0.35, 0.6, 0.83))
    head = {"room": ["Room"], "pressure": ["Pressure", "Regime, Pa"], "oa": ["Min.", "Outdoor", "ACH"],
            "total": ["Min.", "Total ACH"], "single": ["Single", "Pass"], "aclass": ["Air Class"],
            "filter": ["Filter"], "rh": ["Relative", "Humidity"], "temp": ["Design", "Temperature", ", C"],
            "after": ["Afterhours", "mode, Y/N"], "setback": ["Setback", "mode, Y/N"]}
    for key, lines in head.items():
        top = 165 - 9 * len(lines)
        for i, line in enumerate(lines):
            cv.text(CENTRE[key], top + 17 * i, line, 14, centre=True)
    base = {"pressure": "Neutral", "oa": "2", "total": "6", "single": "NR", "aclass": "2", "filter": "MERV 15",
            "rh": "Max 60%", "temp": "21 – 24", "after": "N", "setback": "N"}
    shades = [(0.82, 0.87, 0.94), (0.92, 0.94, 0.97)]
    # row 1: 210-240; row 2 (Laundry, no values): 240-270; row 3 (tall): 270-360
    for n, (top, bottom) in enumerate([(210, 240), (240, 270), (270, 360)]):
        cv.rect(50, top, 250, bottom, fill=shades[n % 2])
    cv.text(60, 218, "Clinical Workroom", 14)
    for key, val in base.items():
        cv.text(CENTRE[key], 218, val, 14, centre=True)
    cv.text(60, 248, "Laundry", 14)
    cv.text(60, 308, "Low Stimulus Room", 14)
    for key, val in {**base, "pressure": "-30", "oa": "12", "total": "12", "single": "Yes", "aclass": "4",
                     "filter": ""}.items():
        if val:
            cv.text(CENTRE[key], 308, val, 14, centre=True)
    for i, line in enumerate(["MERV 15", "(Supply)", "BIBO HEPA", "(Exhaust)"]):
        cv.text(CENTRE["filter"], 275 + 19 * i, line, 14, centre=True)
    cv.text(60, 740, "NOTE:-", 14)
    cv.text(85, 758, "1 – AFTERHOURS MODE APPLIES TO DEPARTMENTS THAT DO NOT OPERATE", 14)
    cv.text(60, 40, "Room conditions – Part 1", 30)
    _footer(cv, "90")


def test_row_rectangles_keep_wrapped_cells_and_blank_rows(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_banded_criteria_page(doc)
    res = _parse(_save(doc, tmp_path), tmp_path)
    rows = {r["room_type_as_printed"]: r for r in res["criteria"]}
    assert list(rows) == ["Clinical Workroom", "Laundry", "Low Stimulus Room"]
    assert rows["Low Stimulus Room"]["filter"] == "MERV 15 (Supply) BIBO HEPA (Exhaust)"
    assert rows["Low Stimulus Room"]["pressure_pa"] == "-30"
    assert ";" not in rows["Low Stimulus Room"]["table_part"]            # 'MERV 15' is not a section heading
    laundry = rows["Laundry"]
    assert laundry["pressure_regime_as_printed"] == "" and laundry["min_total_ach"] == "" and laundry["single_pass"] == ""
    assert float(laundry["confidence"]) < 0.8
    assert [(f["row"], f["field"]) for f in res["failures"]] == [("Laundry", "pressure (cell empty)")]
    assert rows["Clinical Workroom"]["filter"] == "MERV 15" and rows["Clinical Workroom"]["temp_max_c"] == "24"


def build_room_data_page(doc: "pymupdf.Document", with_notes: bool = True, rules: bool = True) -> None:
    cv = Canvas(doc)
    cv.text(700, 60, "DESIGN CRITERIA", 28)          # the strip that makes it look like a criteria page
    cv.text(70, 95, "TEMPERATURE MONITORING OF BME EQUIPMENTS (EG. FRIDGES ETC)", 16)
    cols = [("d_template_room_code", 70, 190, ["D_Template Room", "Code"]),
            ("d_room_name", 190, 340, ["D_Room Name"]),
            ("d_unique_room_identifier", 340, 450, ["D_Unique Room", "Identifier"]),
            ("item_description", 450, 830, ["Item Description"]),
            ("qty03_v12", 830, 890, ["Qty03 (v12)"]),
            ("type_of_tms", 890, 980, ["Type of TMS"]),
            ("d_template_room_name", 980, 1140, ["D_Template Room Name"])]
    for _, x0, x1, lines in cols:
        cv.rect(x0, 134, x1, 173, fill=(0.05, 0.62, 0.85))
        for i, line in enumerate(lines):
            cv.text(x0 + 3, 138 + 14 * i, line, 10)
    data = [("078C_OSUA_P2C2", "OSU-Type A - Medication", "Room", "IWFB11 04.01.01",
             "refrigerator (~160 to 200L) / freezer", "combo medication 2 doors", "1", "Link to BMS", "OSU-Type A - Medication Room"),
            ("093B_OSUB_P2", "OSU-Type B - Medication", "Room", "IWIA1 04.01.01",
             "refrigerator pharmaceutical single door", "link to BMS with lock", "1", "Link to BMS", "OSU-Type B - Medication Room")]
    top = 173.0
    for code, name1, name2, ident, d1, d2, qty, tms, tname in data:
        cv.text(72, top + 6, code, 10)
        cv.text(192, top + 6, name1, 10)
        cv.text(192, top + 19, name2, 10)
        cv.text(342, top + 12, ident, 10)
        cv.text(452, top + 6, d1, 10)
        cv.text(452, top + 19, d2, 10)
        cv.text(855, top + 12, qty, 10)
        cv.text(892, top + 12, tms, 10)
        cv.text(982, top + 12, tname, 10)
        top += 36.0
        if rules:
            cv.page.draw_line((70, top), (1140, top), color=(0, 0, 0), width=0.8)
    if with_notes:
        cv.text(64, 669, "NOTE:-", 14)
        cv.text(64, 683, "1 – AFTERHOURS MODE APPLIES TO DEPARTMENTS THAT DO NOT OPERATE", 14)
        cv.text(64, 697, "CONTINUOUSLY (24/7).", 14)
        cv.text(64, 726, "2. THERE IS 2 TYPE OF TMS PROJECT WIDE:", 14)
        cv.text(112, 741, "A. LINK DIRECTLY TO BMS", 14)
    _footer(cv, "79")


def test_room_data_table_is_its_own_class_and_register(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_room_data_page(doc)
    path = _save(doc, tmp_path)
    assert b.classify_page(pymupdf.open(str(path))[0].get_text()) == "room data table"   # not 'criteria'
    res = _parse(path, tmp_path)
    assert [p["class"] for p in res["pages"]] == ["room data table"] and res["pages"][0]["rows"] == 2
    assert res["criteria"] == [] and res["failures"] == [] and res["page_failures"] == []
    first, second = res["room_data"]
    assert first["d_template_room_code"] == "078C_OSUA_P2C2" and first["d_room_name"] == "OSU-Type A - Medication Room"
    assert first["d_unique_room_identifier"] == "IWFB11 04.01.01" and first["qty03_v12"] == "1"
    assert first["item_description"] == "refrigerator (~160 to 200L) / freezer combo medication 2 doors"
    assert first["type_of_tms"] == "Link to BMS" and first["d_template_room_name"] == "OSU-Type A - Medication Room"
    assert second["d_template_room_code"] == "093B_OSUB_P2" and second["row"] == 2
    assert (first["department"], first["booklet_page"], first["pdf_page"]) == ("IP TEST", "79", 1)
    assert len(first["bbox"].split(",")) == 4 and float(first["confidence"]) > 0.9
    assert '"D_Unique Room Identifier"' in first["columns_as_printed"] and first["other_columns"] == ""
    assert [(n["note_kind"], n["note_number"]) for n in res["notes"]] == [("room data table note", "1"),
                                                                           ("room data table note", "2")]
    assert res["notes"][1]["text"] == "THERE IS 2 TYPE OF TMS PROJECT WIDE: A. LINK DIRECTLY TO BMS"


def test_room_data_rows_without_rules_are_cut_between_quantities(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_room_data_page(doc, rules=False)
    res = _parse(_save(doc, tmp_path), tmp_path)
    assert [r["d_unique_room_identifier"] for r in res["room_data"]] == ["IWFB11 04.01.01", "IWIA1 04.01.01"]
    assert any("no row rules" in msg for _, msg in res["page_failures"])


def test_cli_writes_room_data_register(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    shutil.copy(Path(__file__).resolve().parents[1] / "config" / "project.yaml", root / "config" / "project.yaml")
    folder = root / "data" / "source" / b.BOOKLET_DIR_NAME
    folder.mkdir(parents=True)
    doc = pymupdf.open()
    build_room_data_page(doc)
    doc.save(str(folder / "IP_TEST_Section_4.8_Mechanical_Ventilation_Strategy.pdf"))
    monkeypatch.setenv("AIGH_ROOT", str(root))
    assert b.main(["run"]) == 0
    with (root / "out" / "registers" / "booklet_room_data.csv").open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0].keys()) == b.ROOM_DATA_FIELDS and len(rows) == 2
    assert rows[0]["department"] == "IP TEST" and rows[0]["bbox"]
    with (root / "out" / "registers" / "criteria_notes.csv").open(encoding="utf-8", newline="") as fh:
        notes = list(csv.DictReader(fh))
    assert list(notes[0].keys()) == b.NOTES_FIELDS and {n["note_kind"] for n in notes} == {"room data table note"}


def _annotate(path_doc: "pymupdf.Document", rect, text: str, author: str = "reviewer") -> None:
    annot = path_doc[0].add_freetext_annot(rect, text, fontsize=11, fill_color=(1, 1, 0.8))
    annot.set_info(title=author, content=text)
    annot.update()


def test_reviewer_comment_over_a_criteria_table_is_a_note_not_cells(tmp_path: Path) -> None:
    plain = pymupdf.open()
    build_criteria_page(plain)
    expected = [(r["room_type_as_printed"], r["pressure_regime_as_printed"], r["min_total_ach"])
                for r in _parse(_save(plain, tmp_path, "IP_PLAIN_Section_4.8_X.pdf"), tmp_path)["criteria"]]
    doc = pymupdf.open()
    build_criteria_page(doc)
    # over the Room and Pressure columns of the table, and over the heading
    _annotate(doc, pymupdf.Rect(60, 300, 330, 360), "should be negative pressured treatment room.")
    _annotate(doc, pymupdf.Rect(600, 60, 900, 100), "MDT to confirm the Pa regime. Current AH Isolation: min. -5pa.")
    res = _parse(_save(doc, tmp_path), tmp_path)
    assert [(r["room_type_as_printed"], r["pressure_regime_as_printed"], r["min_total_ach"])
            for r in res["criteria"]] == expected
    assert res["failures"] == [] and res["page_failures"] == []
    ann = [n for n in res["notes"] if n["note_kind"] == "reviewer annotation"]
    assert sorted(n["text"] for n in ann) == ["MDT to confirm the Pa regime. Current AH Isolation: min. -5pa.",
                                              "should be negative pressured treatment room."]
    assert all(n["author"] == "reviewer" and n["note_number"] == "" and len(n["bbox"].split(",")) == 4 for n in ann)
    assert res["pages"][0]["annotations"] == 2
    assert [n["note_kind"] for n in res["notes"]].count("table note") == 1


def _pressure_page_with_note(doc: "pymupdf.Document", heading: str) -> None:
    cv = Canvas(doc)
    cv.text(100, 60, heading, 28)
    cv.text(300, 300, "ANTEROOM", 12)
    cv.text(300, 330, "-15", 12)
    cv.text(82, 727, "NOTE:-", 14)
    cv.text(82, 744, "AH INFECTION CONTROL TO CONFIRM THE LOCATION OF POSITIVE", 14)
    cv.text(82, 761, "ANTEROOM AND NEGATIVE ISO BEDROOM", 14)
    _footer(cv, "96")


def test_pressure_regime_and_pandemic_notes_are_recorded_verbatim(tmp_path: Path) -> None:
    doc = pymupdf.open()
    _pressure_page_with_note(doc, "PRESSURE REGIME")
    _pressure_page_with_note(doc, "PRESSURE REGIME (PANDEMIC MODE)")
    res = _parse(_save(doc, tmp_path), tmp_path)
    assert [(n["note_kind"], n["pdf_page"], n["note_number"]) for n in res["notes"]] == [
        ("pressure regime note", 1, ""), ("pandemic mode note", 2, "")]
    for n in res["notes"]:
        assert n["text"] == ("AH INFECTION CONTROL TO CONFIRM THE LOCATION OF POSITIVE "
                             "ANTEROOM AND NEGATIVE ISO BEDROOM")
        assert n["department"] == "IP TEST" and n["booklet_page"] == "96" and len(n["bbox"].split(",")) == 4
    texts = {r["text"] for r in res["pressure"]}
    assert "ANTEROOM" in texts and "-15" in texts
    assert not any("INFECTION CONTROL" in t or t.startswith("NOTE") for t in texts)     # the NOTE block is not a drawing label


def test_pressure_page_reviewer_comment_also_goes_to_criteria_notes(tmp_path: Path) -> None:
    res = _parse(_annotated_pressure_booklet(tmp_path), tmp_path)
    kinds = [(n["note_kind"], n["author"]) for n in res["notes"]]
    assert kinds == [("reviewer annotation", "reviewer")]
    assert res["notes"][0]["text"].startswith("Sign-off does not include endorsement")


def test_big_heading_decides_pressure_regime_over_a_keyplan_title() -> None:
    text = "PRESSURE REGIME 4.8 MECHANICAL VENTILATION STRATEGY OP 8TH STOREY KEYPLAN"
    assert b.classify_page(text) == "zoning keyplan"                       # without the heading
    assert b.classify_page(text, "PRESSURE REGIME") == "pressure regime"
    assert b.classify_page("DESIGN CRITERIA Room conditions", "PRESSURE REGIME") == "criteria"
    assert b.classify_page("D_Template Room Code D_Room Name D_Unique Room Identifier Item Description DESIGN CRITERIA"
                           ) == "room data table"


def test_footer_year_is_not_a_page_number(tmp_path: Path) -> None:
    doc = pymupdf.open()
    cv = Canvas(doc)
    cv.text(100, 100, "KEYPLAN", 14)
    cv.text(60, 805, "ALEXANDRA HOSPITAL REDEVELOPMENT PROJECT | DETAILED DESIGN BOOKLET | MAY 2025", 11)
    page = doc[0]
    assert b.printed_page_number(b.page_words(page), page.rect.height, page.rect.width) == ""
    cv.text(1150, 808, "136", 14)
    cv.text(1152, 808, "13", 14)                     # a second number printed over the first
    assert b.printed_page_number(b.page_words(page), page.rect.height, page.rect.width) == ""
    doc2 = pymupdf.open()
    cv2 = Canvas(doc2)
    cv2.text(60, 805, "DETAILED DESIGN BOOKLET | MAY 2025", 11)
    cv2.text(1150, 808, "136", 14)
    p2 = doc2[0]
    assert b.printed_page_number(b.page_words(p2), p2.rect.height, p2.rect.width) == "136"


def build_banded_standby_page(doc: "pymupdf.Document", tags: bool = True, annotate: bool = False,
                              wide_area: bool = False) -> None:
    """Standby table with one rectangle per row in every column.  Area text is centred in a wide cell and wraps
    above and below the tag line, Configuration sits a few points off it, and the level heading wraps in a merged cell."""
    cv = Canvas(doc)
    cv.text(60, 60, "EQUIPMENT STANDBY CONFIGURATION SUMMARY", 20)
    x_level, x_tag, x_area, x_cfg = 64.0, 164.0, 387.0, 945.0
    if not tags:
        x_tag = x_level      # no tag column: Level | Area | Configuration
        x_area = 295.0
    if wide_area:            # an Area cell 704 points wide (Paeds), wider than the 700 point cut-off for page-wide fills
        x_area, x_cfg = 251.0, 955.0
    x_end = 1143.0
    heads = [(x_level, x_tag if tags else x_area, "IP TEST")]
    if tags:
        heads.append((x_tag, x_area, "Equipment Tag"))
    heads += [(x_area, x_cfg, "Area"), (x_cfg, x_end, "Equipment Standby")]
    for x0, x1, label in heads:
        cv.rect(x0, 148, x1, 200, fill=(0.35, 0.6, 0.83))
        cv.text((x0 + x1) / 2.0, 160, label, 14, centre=True)
    cv.text((x_cfg + x_end) / 2.0, 180, "Configuration 1,2", 14, centre=True)
    rows = [("AHU-B-15-15TESTNWWG-1", ["Patient Bay (Dialysis/Infusions) / Staff Station / Bay /", "Interview Room / Patient Room"], "AHU-CONFIG-E"),
            ("AHU-B-15-15TESTNWCR-1", ["Clinical Workroom / Procedure Room", "Pantry / Store", "Cleaner Room"], "AHU-CONFIG-F")]
    y = 200.0
    # merged level heading across both rows
    heights = [52.0, 70.0]
    cv.rect(x_level, y, x_tag if tags else x_area, y + sum(heights), fill=(0.82, 0.87, 0.94))
    cv.text(((x_level + (x_tag if tags else x_area)) / 2.0), y + sum(heights) / 2.0 - 16, "Level 15", 14, centre=True)
    cv.text(((x_level + (x_tag if tags else x_area)) / 2.0), y + sum(heights) / 2.0 + 2, "NW wing", 14, centre=True)
    for (tag, area_lines, cfg), h in zip(rows, heights):
        if tags:
            cv.rect(x_tag, y, x_area, y + h, fill=(0.92, 0.94, 0.97))
        cv.rect(x_area, y, x_cfg, y + h, fill=(0.92, 0.94, 0.97))
        cv.rect(x_cfg, y, x_end, y + h, fill=(0.92, 0.94, 0.97))
        mid = y + h / 2.0
        if tags:
            cv.text(x_tag + 7, mid - 7, tag, 14)
        n = len(area_lines)
        for i, line in enumerate(area_lines):
            cv.text((x_area + x_cfg) / 2.0, mid - 7 + (i - (n - 1) / 2.0) * 17, line, 14, centre=True)
        cv.text((x_cfg + x_end) / 2.0, mid - 14, cfg, 14, centre=True)      # a few points above the tag line
        y += h
    cv.text(85, 740, "NOTE:-", 14)
    cv.text(85, 758, "1 – REFER TO APPENDIX 7 FOR EQUIPMENT STANDBY CONFIGURATION TYPES", 14)
    _footer(cv, "82")
    if annotate:
        _annotate(doc, pymupdf.Rect(168, 600, 420, 640), "NEP room to be consistent with Iso ward")


def test_standby_rows_follow_row_rectangles(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_banded_standby_page(doc)
    res = _parse(_save(doc, tmp_path), tmp_path)
    assert res["failures"] == [] and res["page_failures"] == []
    first, second = res["standby"]
    assert first["tag_as_written"] == "AHU-B-15-15TESTNWWG-1" and second["tag_as_written"] == "AHU-B-15-15TESTNWCR-1"
    assert first["area"] == "Patient Bay (Dialysis/Infusions) / Staff Station / Bay / Interview Room / Patient Room"
    assert second["area"] == "Clinical Workroom / Procedure Room Pantry / Store Cleaner Room"
    assert (first["configuration"], second["configuration"]) == ("AHU-CONFIG-E", "AHU-CONFIG-F")   # not read as tags
    assert {r["level_wing_heading"] for r in res["standby"]} == {"Level 15 NW wing"}          # two-line merged heading joined


def test_standby_table_without_a_tag_column(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_banded_standby_page(doc, tags=False)
    res = _parse(_save(doc, tmp_path), tmp_path)
    assert res["failures"] == [] and res["page_failures"] == []
    assert [r["tag_as_written"] for r in res["standby"]] == ["", ""]
    assert [r["configuration"] for r in res["standby"]] == ["AHU-CONFIG-E", "AHU-CONFIG-F"]
    assert res["standby"][0]["area"].startswith("Patient Bay") and res["standby"][1]["area"].endswith("Cleaner Room")
    assert {r["confidence"] for r in res["standby"]} == {"0.97"} and "no equipment tag column" in res["pages"][0]["detail"]


def test_comment_over_a_standby_table_is_a_note(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_banded_standby_page(doc, annotate=True)
    res = _parse(_save(doc, tmp_path), tmp_path)
    assert len(res["standby"]) == 2 and res["failures"] == []
    assert [(n["note_kind"], n["text"]) for n in res["standby_notes"] if n["note_kind"] == "reviewer annotation"] == [
        ("reviewer annotation", "NEP room to be consistent with Iso ward")]
    assert not any("NEP room" in r["area"] for r in res["standby"])


def test_comparison_flags_differences_between_departments(tmp_path: Path) -> None:
    a_doc, b_doc = pymupdf.open(), pymupdf.open()
    build_criteria_page(a_doc)
    build_criteria_page(b_doc, first_total="12")          # Bed Room total ACH 8 -> 12
    ra = _parse(_save(a_doc, tmp_path, "IP_ALPHA_Section_4.8_X.pdf"), tmp_path)
    rb = _parse(_save(b_doc, tmp_path, "IP_BETA_Section_4.8_X.pdf"), tmp_path)
    data = b.comparison_data([ra, rb])
    assert data["groups"] == 7 and data["identical"] == 6 and len(data["diffs"]) == 1
    diff = data["diffs"][0]
    assert diff["label"] == "Bed Room" and diff["fields"][0][0] == "min_total_ach"
    assert sorted(ds[0] for _, ds in diff["fields"][0][1]) == ["IP ALPHA", "IP BETA"]
    text = "\n".join(b.compare_departments([ra, rb]))
    assert "identical on all" in text and "| Bed Room | min_total_ach |" in text


# --------------------------------------------------------------------------- #
# Footer region: rows and note lines below 0.93 of the page height
# --------------------------------------------------------------------------- #
def test_row_on_the_footer_stamp_line_is_read(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_criteria_page(doc, low_row=True)
    page = doc[0]
    assert 795 > page.rect.height * 0.93            # the fixture row really is below the old cut-off
    res = _parse(_save(doc, tmp_path), tmp_path)
    rows = {r["room_type_as_printed"]: r for r in res["criteria"]}
    last = rows["Cleaner Room"]
    assert (last["pressure_direction"], last["min_oa_ach"], last["min_total_ach"], last["single_pass"]) == \
        ("neutral", "2", "6", "Y")
    assert (last["filter"], last["rh_as_printed"], last["temp_as_printed"], last["setback"]) == \
        ("MERV 15", "Max 60%", "21 – 24", "N")
    assert float(last["bbox"].split(",")[1]) > page.rect.height * 0.93
    assert "ALEXANDRA" not in " ".join(str(v) for v in last.values())      # the footer stamp is not cell text
    assert last["setback"] == "N" and last["afterhours"] == "N"            # the page number is not read into a cell
    assert res["failures"] == [] and res["page_failures"] == []
    assert len(res["criteria"]) == 8 and res["pages"][0]["booklet_page"] == "77"


def test_note_text_below_the_old_cut_off_is_kept_verbatim(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_criteria_page(doc, low_note=True)
    res = _parse(_save(doc, tmp_path), tmp_path)
    notes = [n for n in res["notes"] if n["note_kind"] == "table note"]
    assert [n["note_number"] for n in notes] == ["1"]
    assert notes[0]["text"] == ("AFTERHOURS MODE APPLIES TO DEPARTMENTS THAT DO NOT OPERATE CONTINUOUSLY (24/7) BUT HAVE "
                                "SPECIFIC ROOMS REQUIRING AIR CONDITIONING. ALLOWANCE FOR PUSH-BUTTON.")
    assert "ALEXANDRA" not in notes[0]["text"]


def test_footer_furniture_is_the_stamp_and_the_page_number_only() -> None:
    doc = pymupdf.open()
    build_criteria_page(doc, low_row=True)
    page = doc[0]
    words = b.page_words(page)
    gone = {w.text for w in b.footer_furniture(words, page.rect.height, page.rect.width)}
    assert gone == {"ALEXANDRA", "HOSPITAL", "REDEVELOPMENT", "PROJECT", "|", "DETAILED", "DESIGN", "BOOKLET", "77"}
    kept = b.strip_furniture(words, page.rect.height, page.rect.width)
    assert {"Cleaner", "Neutral", "N"} <= {w.text for w in kept}


# --------------------------------------------------------------------------- #
# Pages of other sections of the booklet (AV, communication, IT strategy)
# --------------------------------------------------------------------------- #
def _strip_page(doc: "pymupdf.Document", strip: str, keyplan: bool = True) -> None:
    cv = Canvas(doc)
    cv.text(760, 18, "4.1 DEPARTMENT DRAWINGS", 28)
    cv.text(760, 54, strip, 20)
    cv.text(100, 100, "Note: Highlighted are rooms with AV requirements", 14)
    if keyplan:
        cv.text(900, 200, "IP 17TH STOREY - KEYPLAN", 14)
    cv.text(300, 400, "ENS", 12)
    _footer(cv, "90")


def test_page_of_another_section_is_other_even_with_a_keyplan_title(tmp_path: Path) -> None:
    doc = pymupdf.open()
    _strip_page(doc, "4.1.10 AV STRATEGY")
    _strip_page(doc, "4.1.8 MECHANICAL VENTILATION STRATEGY")
    _strip_page(doc, "4.9 IT STRATEGY", keyplan=False)
    res = _parse(_save(doc, tmp_path), tmp_path)
    assert [p["class"] for p in res["pages"]] == ["other", "zoning keyplan", "other"]
    assert "4.1.10 AV STRATEGY" in res["pages"][0]["detail"] and "another section" in res["pages"][0]["detail"]
    assert {z["pdf_page"] for z in res["zoning"]} == {2}                  # nothing of the AV page is a zoning label
    assert b.classify_page("OP 8TH STOREY KEYPLAN", "", "4.1.10 AV STRATEGY") == "other"
    assert b.classify_page("OP 8TH STOREY KEYPLAN", "", "4.8 MECHANICAL VENTILATION STRATEGY | DESIGN CRITERIA"
                           ) == "zoning keyplan"
    assert b.classify_page("OP 8TH STOREY KEYPLAN", "", "") == "zoning keyplan"


# --------------------------------------------------------------------------- #
# Reviewer markup without text: strike-through, callout arrows, boxes
# --------------------------------------------------------------------------- #
def _strike(doc: "pymupdf.Document", p1, p2, author: str = "reviewer") -> None:
    annot = doc[0].add_line_annot(p1, p2)
    annot.set_colors(stroke=(1, 0, 0))
    annot.set_info(title=author, content="")
    annot.update()


def test_strike_through_and_callout_are_linked_to_the_row(tmp_path: Path) -> None:
    plain = pymupdf.open()
    build_criteria_page(plain)
    before = _parse(_save(plain, tmp_path, "IP_PLAIN_Section_4.8_X.pdf"), tmp_path)["criteria"]
    doc = pymupdf.open()
    build_criteria_page(doc)
    _strike(doc, (58, 327), (190, 327))                       # across 'Isolation Room' (row at y 320)
    annot = doc[0].add_freetext_annot(pymupdf.Rect(60, 420, 330, 470), "should be negative pressured treatment room.",
                                      fontsize=11, fill_color=(1, 1, 0.8),
                                      callout=((100, 336), (100, 400), (120, 400)))
    annot.set_info(title="reviewer", content="should be negative pressured treatment room.")
    annot.update()
    _annotate(doc, pymupdf.Rect(600, 650, 900, 690), "a comment with no arrow")          # not linked to any row
    res = _parse(_save(doc, tmp_path), tmp_path)
    rows = {r["room_type_as_printed"]: r for r in res["criteria"]}
    iso = rows["Isolation Room"]
    assert iso["reviewer_markup"] == "struck through by a reviewer Line annotation (reviewer)"
    assert iso["reviewer_comments"] == "should be negative pressured treatment room."
    assert iso["confidence"] == "0.50"                          # read as printed, but the row is not current
    assert iso["pressure_pa"] == "-15" and iso["min_total_ach"] == "12"        # values are still as printed
    others = [r for r in res["criteria"] if r["room_type_as_printed"] != "Isolation Room"]
    assert all(r["reviewer_markup"] == "" and r["reviewer_comments"] == "" for r in others)
    # nothing else about the rows changed
    keep = [k for k in before[0] if k not in ("reviewer_markup", "reviewer_comments", "confidence",
                                                      "source_file", "department")]
    assert [{k: r[k] for k in keep} for r in res["criteria"]] == [{k: r[k] for k in keep} for r in before]
    notes = [n for n in res["notes"] if n["note_kind"] == "reviewer markup"]
    assert [n["text"] for n in notes] == ["[Line annotation, no text; strikes through: Isolation Room]"]
    assert notes[0]["author"] == "reviewer" and len(notes[0]["bbox"].split(",")) == 4
    assert sum(1 for n in res["notes"] if n["note_kind"] == "reviewer annotation") == 2
    assert res["failures"] == []


def test_markup_without_text_on_a_pressure_page_is_recorded(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_pressure_page(doc)
    box = doc[0].add_rect_annot(pymupdf.Rect(300, 300, 420, 380))
    box.set_info(title="reviewer", content="")
    box.update()
    res = _parse(_save(doc, tmp_path), tmp_path)
    marks = [r for r in res["pressure"] if r["label_kind"] == "reviewer_markup"]
    assert [m["text"] for m in marks] == ["[Square annotation, no text]"] and marks[0]["annotation_author"] == "reviewer"
    assert [n["text"] for n in res["notes"] if n["note_kind"] == "reviewer markup"] == ["[Square annotation, no text]"]
    assert not any(r["label_kind"] == "reviewer_annotation" for r in res["pressure"])


def test_page_markup_marks_only_thin_horizontal_lines_as_strikes() -> None:
    doc = pymupdf.open()
    Canvas(doc)
    page = doc[0]
    for p1, p2 in (((50, 100), (200, 100)), ((50, 200), (200, 260))):
        a = page.add_line_annot(p1, p2)
        a.set_info(title="x", content="")
        a.update()
    marks = b.page_markup(page)
    assert [m["strike"] for m in marks] == [True, False] and [m["kind"] for m in marks] == ["Line", "Line"]


# --------------------------------------------------------------------------- #
# Emergency 4.1.8: zoned criteria layout, stacked standby tags, pair tags on keyplans, page numbers, Paeds standby
# --------------------------------------------------------------------------- #
STRIP_418 = "4.1.8 MECHANICAL VENTILATION STRATEGY"          # 15 pt in the strip at the top right, as in the booklets


def build_zoned_criteria_page(doc: "pymupdf.Document", caption: str = "Rooms Part 1", booklet_page: str = "130",
                              extra_rows=None) -> None:
    """The Emergency layout: caption 'Rooms Part N' (20 pt) beside the 4.1.8 strip, a header whose temperature
    heading wraps mid-word, no zone column (the zone opens the room name), 'Non-' / 'Recirculating' over two lines in
    Single Pass, '-ve' pressures and an 'N' under Filter, Humidity and Temperature for toilets."""
    cv = Canvas(doc)
    cv.text(745, 55, STRIP_418, 15)
    cv.text(60, 95, "DESIGN CRITERIA", 12)
    cv.text(91, 54, caption, 20)
    cv.rect(50, 118, 1140, 162, fill=(0.35, 0.6, 0.83))
    head = {
        "room": ["Room"], "pressure": ["Pressure", "Regime, Pa"], "oa": ["Min.", "Outdoor", "ACH"],
        "total": ["Min. Total", "ACH"], "single": ["Single", "Pass"], "aclass": ["Air Class"],
        "filter": ["Filter"], "rh": ["Relative", "Humidity"], "temp": ["Design", "Temperatur", "e, C"],
        "after": ["Afterhours", "mode, Y/N"], "setback": ["Setback", "mode, Y/N"],
    }
    for key, lines in head.items():
        top = 140 - 7 * len(lines)
        for i, line in enumerate(lines):
            x = 60 if key == "room" else CENTRE[key]
            cv.text(x, top + 14 * i, line, 11, centre=key != "room")
    base = {"filter": "MERV 15", "rh": "≤65", "temp": "21-24", "after": "N", "setback": "N"}
    rows = [
        ("P3 TRIAGE", {**base, "pressure": "Neutral", "oa": "2", "total": "12", "single": "NONREC", "aclass": "4",
                       "filter": "HEPA"}),
        ("P3 TOILET", {**base, "pressure": "-ve", "oa": "2", "total": "15", "single": "Y", "aclass": "4",
                       "filter": "N", "rh": "N", "temp": "N"}),
        ("FEVER Gown Down", {**base, "pressure": "+5 Pa", "oa": "10", "total": "10", "single": "Y", "aclass": "4"}),
        ("P2 South NP Patient Bay", {**base, "pressure": "-10 Pa", "oa": "2", "total": "12", "single": "N", "aclass": "3"}),
        ("P2 South Neutra Staff Station / Linen", {**base, "pressure": "Neutral", "oa": "2", "total": "12",
                                                   "single": "N", "aclass": "3"}),
        ("CAC FEVER Triage", {**base, "pressure": "-5 Pa", "oa": "12", "total": "12", "single": "Y", "aclass": "4"}),
        ("P3 Bed Room", {**base, "pressure": "Neutral", "oa": "2", "total": "9", "single": "N", "aclass": "2"}),
    ] + list(extra_rows or [])
    y = 190.0
    for room, cells in rows:
        cv.text(60, y, room, 11)
        for key, val in cells.items():
            if val == "NONREC":
                cv.text(CENTRE[key], y - 8, "Non-", 11, centre=True)
                cv.text(CENTRE[key], y + 8, "Recirculating", 11, centre=True)
            else:
                cv.text(CENTRE[key], y, val, 11, centre=True)
        y += 34.0
    _footer(cv, booklet_page)


def test_zone_of_reads_the_leading_zone_words() -> None:
    assert b.zone_of("P3 RECEPTION / WAITING") == ("P3", "P3")
    assert b.zone_of("P2 South NP Staff Station / Linen / store") == ("P2 South NP", "P2 South NP")
    assert b.zone_of("P2 South Neutra Staff Station / Linen") == ("P2 South Neutral", "P2 South Neutra")
    assert b.zone_of("P2 North CU MEDS")[0] == "P2 North" and b.zone_of("P2 Interview")[0] == "P2"
    assert b.zone_of("CAC FEVER Triage")[0] == "CAC FEVER" and b.zone_of("CAC Toilet")[0] == "CAC"
    assert b.zone_of("FEVER / RESUS NP")[0] == "FEVER" and b.zone_of("RESUS")[0] == "RESUS"
    assert b.zone_of("Staff Support Pods")[0] == "Staff Support" and b.zone_of("Plant Store") == ("", "")
    assert b.zone_of("P20 Room") == ("", "")                      # a prefix must end at a word boundary


def test_direction_words_and_the_new_cell_values() -> None:
    assert b.parse_pressure_full("-ve") == {"pa": "", "pa_min": "", "pa_max": "", "direction": "negative", "ok": True}
    assert b.parse_pressure_full("+ve")["direction"] == "positive" and b.parse_pressure_full("-ve")["pa"] == ""
    assert b.parse_pressure_full("-10 Pa")["pa"] == "-10" and b.parse_pressure_full("+5 Pa")["pa"] == "5"
    assert b.parse_pressure_full("Neutral")["direction"] == "neutral"
    assert b.parse_single_pass("Non- Recirculating") == ("Y", True) and b.parse_single_pass("Non-Recirculating") == ("Y", True)
    assert b.parse_single_pass("N") == ("N", True) and b.parse_single_pass("Recirculating")[1] is False
    assert b.parse_rh("≤65") == ("65", "", True) and b.parse_rh("N") == ("", "", True) and b.parse_rh("Max 60%") == ("60", "", True)
    assert b.parse_temp("N") == ("", "", True) and b.parse_temp("21-24") == ("21", "24", True)


def test_zoned_layout_reads_zone_pressure_and_words(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_zoned_criteria_page(doc)
    build_zoned_criteria_page(doc, caption="Rooms Part 6 – PANDEMIC SURGE AFFECTED ROOMS", booklet_page="135")
    res = _parse(_save(doc, tmp_path, "IP_ZONED_Section_4.1.8_X.pdf"), tmp_path)
    assert res["failures"] == [] and res["page_failures"] == []
    assert [p["class"] for p in res["pages"]] == ["criteria", "criteria"]
    rows = res["criteria"]
    assert len(rows) == 14 and set(rows[0]) == set(b.CRITERIA_FIELDS)
    first = rows[:7]
    assert [r["zone"] for r in first] == ["P3", "P3", "FEVER", "P2 South NP", "P2 South Neutral", "CAC FEVER", "P3"]
    assert [r["room_type_as_printed"] for r in first][:2] == ["P3 TRIAGE", "P3 TOILET"]      # the name is never altered
    assert json_raw(first[4])["zone_prefix"] == "P2 South Neutra" and json_raw(first[0])["zone_prefix"] == "P3"
    triage, toilet, fever, south, _, cac, _ = first
    assert triage["single_pass"] == "Y" and json_raw(triage)["single_pass"] == "Non- Recirculating"   # as printed kept
    assert triage["filter"] == "HEPA" and triage["rh_as_printed"] == "≤65" and triage["rh_max_pct"] == "65"
    assert toilet["pressure_regime_as_printed"] == "-ve" and toilet["pressure_direction"] == "negative"
    assert toilet["pressure_pa"] == "" and toilet["filter"] == "N" and toilet["rh_as_printed"] == "N"
    assert toilet["rh_max_pct"] == "" and toilet["temp_as_printed"] == "N" and toilet["temp_min_c"] == ""
    assert (toilet["afterhours"], toilet["setback"]) == ("N", "N") and toilet["min_total_ach"] == "15"
    assert fever["pressure_pa"] == "5" and fever["pressure_direction"] == "positive"          # signed
    assert south["pressure_pa"] == "-10" and cac["pressure_pa"] == "-5"
    assert {r["confidence"] for r in rows} == {"0.97"} and all(r["booklet_page"] in ("130", "135") for r in rows)
    assert {r["table_part"] for r in rows[:7]} == {"Part 1"}
    assert {r["table_part"] for r in rows[7:]} == {"Part 6; PANDEMIC SURGE AFFECTED ROOMS"}   # mode from the caption
    z = res["zoned_pages"][0]
    assert z["strip"].startswith("4.1.8") and z["caption"] == "Rooms Part 1" and z["header"]["temp"] == "Design Temperatur e, C"
    assert z["header"]["pressure"] == "Pressure Regime, Pa" and z["header"]["single_pass"] == "Single Pass"
    assert z["header"]["afterhours"] == "Afterhours mode, Y/N" and z["rows"] == 7


def test_a_room_with_no_known_zone_is_a_failed_field(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_zoned_criteria_page(doc, extra_rows=[("Plant Store", {"pressure": "Neutral", "oa": "2", "total": "6", "single": "N",
                                                               "aclass": "2", "filter": "MERV 15", "rh": "≤65", "temp": "21-24",
                                                               "after": "N", "setback": "N"})])
    res = _parse(_save(doc, tmp_path, "IP_ZONED_Section_4.1.8_X.pdf"), tmp_path)
    assert [(f["row"], f["field"]) for f in res["failures"]] == [("Plant Store", "zone")]
    assert res["criteria"][-1]["zone"] == "" and float(res["criteria"][-1]["confidence"]) < 0.9


def test_the_4_1_8_strip_alone_does_not_select_the_zoned_handler(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_criteria_page(doc)                                   # caption 'Room conditions - Part 3', standard table
    doc[0].insert_text((745, 68), STRIP_418, fontname="helv", fontsize=15)
    res = _parse(_save(doc, tmp_path), tmp_path)
    assert "4.1.8" in b.section_strip(b.page_words(pymupdf.open(str(tmp_path / "IP_TEST_Section_4.8_Mechanical_Ventilation_Strategy.pdf"))[0]))
    assert res["zoned_pages"] == [] and {r["zone"] for r in res["criteria"]} == {""}
    assert res["criteria"][0]["table_part"] == "Part 3" and len(res["criteria"]) == 7 and res["failures"] == []
    assert b.zoned_caption(b.page_words(doc[0])) is None


def test_zone_column_is_blank_for_other_departments_and_signed_pressure_is_kept() -> None:
    assert b.CRITERIA_FIELDS.index("zone") == b.CRITERIA_FIELDS.index("table_part") + 1
    assert b.CRITERIA_FIELDS.index("pressure_pa_min") == b.CRITERIA_FIELDS.index("pressure_pa") + 1


def test_comparison_goes_zone_by_zone_with_the_prefix_taken_off(tmp_path: Path) -> None:
    z_doc, s_doc = pymupdf.open(), pymupdf.open()
    build_zoned_criteria_page(z_doc)
    build_criteria_page(s_doc)                                 # 'Bed Room': total ACH 8 against the zone P3 'Bed Room': 9
    rz = _parse(_save(z_doc, tmp_path, "IP_ZONED_Section_4.1.8_X.pdf"), tmp_path)
    rs = _parse(_save(s_doc, tmp_path, "IP_PLAIN_Section_4.8_X.pdf"), tmp_path)
    data = b.comparison_data([rz, rs])
    assert "IP ZONED / P3" in data["departments"] and "IP ZONED / CAC FEVER" in data["departments"]
    diff = next(d for d in data["diffs"] if d["label"] == "Bed Room")
    assert diff["fields"][0][0] == "min_total_ach"
    assert sorted(ds[0] for _, ds in diff["fields"][0][1]) == ["IP PLAIN", "IP ZONED / P3"]
    assert rz["criteria"][-1]["room_type_norm"] == "p3 bed room"                      # criteria.csv keeps the prefix


def build_pair_standby_page(doc: "pymupdf.Document") -> None:
    """Emergency standby table: Equipment Tag | Area Served | Equipment Standby Configuration (no level column); a
    duty and standby tag stacked in one cell; numbered notes with no NOTE marker, one of which holds 'CONFIGURATION'."""
    cv = Canvas(doc)
    cv.text(120, 40, "EQUIPMENT STANDBY CONFIGURATION SUMMARY", 20)
    for x0, x1, lines in ((84.0, 338.0, ["Equipment Tag"]), (338.0, 947.0, ["Area Served"]),
                          (947.0, 1125.0, ["Equipment Standby", "Configuration 1,2"])):
        cv.rect(x0, 105, x1, 182, fill=(0.35, 0.6, 0.83))
        for i, line in enumerate(lines):
            cv.text((x0 + x1) / 2.0, 135 + 18 * i - 9 * (len(lines) - 1), line, 14, centre=True)
    y = 182.0
    for tag, area, cfg in (("AHU-B-02-01EDP3WAIT", "P3 Waiting & Consult", "CONFIG-B"),
                           ("AHU-B-03-01EDFEVER", "Fever", "CONFIG-A")):
        for x0, x1 in ((84.0, 338.0), (338.0, 947.0), (947.0, 1125.0)):
            cv.rect(x0, y, x1, y + 58, fill=(0.82, 0.87, 0.94))
        cv.text(90, y + 8, tag + "-01A", 14)
        cv.text(90, y + 28, tag + "-01B", 14)
        cv.text(345, y + 20, area, 14)
        cv.text(1036, y + 20, cfg, 14, centre=True)
        y += 58
    cv.text(85, 740, "1 – REFER TO APPENDIX 4.B FOR RESILIENCE STRATEGY.", 14)
    cv.text(85, 757, "2 – EXCEPT FOR EQUIPMENT STANDBY CONFIGURATION TYPE H, ALL CONFIGURATIONS ALLOWS FOR REDUNDANCY.", 14)
    _footer(cv, "139")


def test_standby_with_stacked_tags_and_no_level_column(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_pair_standby_page(doc)
    res = _parse(_save(doc, tmp_path, "IP_ZONED_Section_4.1.8_X.pdf"), tmp_path)
    assert res["failures"] == [] and res["page_failures"] == [] and res["pages"][0]["class"] == "standby summary"
    got = [(r["tag_as_written"], r["area"], r["configuration"], r["level_wing_heading"]) for r in res["standby"]]
    assert got == [("AHU-B-02-01EDP3WAIT-01A", "P3 Waiting & Consult", "CONFIG-B", ""),
                   ("AHU-B-02-01EDP3WAIT-01B", "P3 Waiting & Consult", "CONFIG-B", ""),
                   ("AHU-B-03-01EDFEVER-01A", "Fever", "CONFIG-A", ""),
                   ("AHU-B-03-01EDFEVER-01B", "Fever", "CONFIG-A", "")]
    assert {r["confidence"] for r in res["standby"]} == {"0.97"} and {r["note_refs"] for r in res["standby"]} == {"1,2"}
    assert [n["note_number"] for n in res["standby_notes"]] == ["1", "2"]
    assert res["standby_notes"][1]["text"].startswith("EXCEPT FOR EQUIPMENT STANDBY CONFIGURATION TYPE H")
    assert not any(r["tag_as_written"].startswith(("1", "2")) for r in res["standby"])      # a note is not a row


def test_standby_area_cell_wider_than_700_points(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_banded_standby_page(doc, tags=False, wide_area=True)
    res = _parse(_save(doc, tmp_path), tmp_path)
    assert res["failures"] == [] and res["page_failures"] == []
    assert {r["level_wing_heading"] for r in res["standby"]} == {"Level 15 NW wing"}
    assert res["standby"][0]["area"].startswith("Patient Bay") and res["standby"][1]["area"].endswith("Cleaner Room")
    assert [r["configuration"] for r in res["standby"]] == ["AHU-CONFIG-E", "AHU-CONFIG-F"]


def test_keyplan_pair_tags_only_for_the_zoned_file(tmp_path: Path) -> None:
    def keyplan(doc):
        cv = Canvas(doc)
        cv.text(900, 150, "IP 1ST STOREY KEYPLAN", 14)
        cv.text(300, 300, "AHU-B-02-01EDP3WAIT-01A/B", 12)
        cv.text(400, 400, "STAFF", 10)
        _footer(cv, "136")
    zoned, plain = pymupdf.open(), pymupdf.open()
    build_zoned_criteria_page(zoned)
    keyplan(zoned)
    keyplan(plain)
    rz = _parse(_save(zoned, tmp_path, "IP_ZONED_Section_4.1.8_X.pdf"), tmp_path)
    rp = _parse(_save(plain, tmp_path, "IP_PLAIN_Section_4.8_X.pdf"), tmp_path)
    kinds = lambda res: {z["text"]: z["label_kind"] for z in res["zoning"]}
    assert kinds(rz)["AHU-B-02-01EDP3WAIT-01A/B"] == "equipment_tag" and "1 tags" in rz["pages"][1]["detail"]
    assert kinds(rp)["AHU-B-02-01EDP3WAIT-01A/B"] == "room_label"          # other departments are left as they were


def test_pressure_page_heading_keeps_its_second_line(tmp_path: Path) -> None:
    doc = pymupdf.open()
    cv = Canvas(doc)
    cv.text(100, 18, "PRESSURE REGIME P3 WAITING & CONSULT", 28)
    cv.text(100, 56, "BUSINESS AS USUAL", 28)
    cv.text(300, 300, "TRIAGE", 12)
    _footer(cv, "140")
    res = _parse(_save(doc, tmp_path, "IP_ZONED_Section_4.1.8_X.pdf"), tmp_path)
    assert res["pages"][0]["class"] == "pressure regime"
    assert res["pressure"][0]["page_heading"] == "PRESSURE REGIME P3 WAITING & CONSULT BUSINESS AS USUAL"
    assert [p["text"] for p in res["pressure"]] == ["TRIAGE"] and res["pages"][0]["detail"].endswith("BUSINESS AS USUAL")


def _numbered_booklet(tmp_path: Path, ink_on_middle: bool) -> dict:
    doc = pymupdf.open()
    for number in ("112", "", "114"):
        cv = Canvas(doc)
        cv.text(900, 150, "IP 9TH STOREY KEYPLAN", 14)
        cv.text(60, 805, "ALEXANDRA HOSPITAL REDEVELOPMENT PROJECT | DETAILED DESIGN BOOKLET", 11)
        if number:
            cv.text(1150, 808, number, 14)
        elif ink_on_middle:                            # a page number that is a picture: ink, but no text
            cv.page.draw_rect(pymupdf.Rect(1150, 808, 1172, 824), color=None, fill=(0, 0, 0))
    return _parse(_save(doc, tmp_path, "IP_NUM_Section_4.8_X.pdf"), tmp_path)


def test_a_page_that_prints_no_number_is_a_source_gap_not_a_failure(tmp_path: Path) -> None:
    res = _numbered_booklet(tmp_path, ink_on_middle=False)
    assert res["page_failures"] == []
    assert res["page_gaps"] and res["page_gaps"][0][0] == 2 and "113 (112 and 114" in res["page_gaps"][0][1]
    assert [p["booklet_page"] for p in res["pages"]] == ["112", "", "114"]         # as printed: stays blank
    assert res["pages"][1]["booklet_inferred"] == "113" and b._unnumbered_label(res["pages"][1]) == "not printed (113 by sequence)"
    assert all(z["booklet_page"] == "" for z in res["zoning"] if z["pdf_page"] == 2)


def test_a_page_number_that_is_ink_but_not_text_stays_a_failure(tmp_path: Path) -> None:
    res = _numbered_booklet(tmp_path, ink_on_middle=True)
    assert res["page_gaps"] == [] and [pg for pg, _ in res["page_failures"]] == [2]
