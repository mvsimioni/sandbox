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


def test_zone_column_is_blank_for_other_departments_and_signed_pressure_is_kept(tmp_path: Path) -> None:
    assert b.CRITERIA_FIELDS.index("zone") == b.CRITERIA_FIELDS.index("table_part") + 1
    assert b.CRITERIA_FIELDS.index("pressure_pa_min") == b.CRITERIA_FIELDS.index("pressure_pa") + 1
    doc = pymupdf.open()
    build_criteria_page(doc, pa_rows=True)                    # a standard table: the zone is blank and Pa keeps its sign
    res = _parse(_save(doc, tmp_path), tmp_path)
    assert res["criteria"] and {r["zone"] for r in res["criteria"]} == {""}
    assert {r["room_type_as_printed"]: r["pressure_pa"] for r in res["criteria"]}["Isolation Room"] == "-15"
    assert all(set(r) == set(b.CRITERIA_FIELDS) for r in res["criteria"])


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


# --------------------------------------------------------------------------- #
# Corrections after verification (3): new extracts ICU HDU, I-CARE, MDU OCP, Rehab Med, DI, Ortho, Pharmacy, Data Centre
# --------------------------------------------------------------------------- #
STRIP_48 = "4.8 MECHANICAL VENTILATION STRATEGY"
HEAD_STYLES = {
    # ICU HDU: every heading wraps mid-word in its cell
    "split": {"room": ["Room"], "pressure": ["Pressure Re", "gime, Pa"], "oa": ["Min. Outdo", "or ACH"],
              "total": ["Min. Total", "ACH"], "single": ["Single Pass"], "aclass": ["Air Class"], "filter": ["Filter"],
              "rh": ["Relative Hu", "midity"], "temp": ["Design Tem", "perature, C"],
              "after": ["Afterhours", "mode, Y/N"], "setback": ["Setback mo", "de, Y/N"]},
    # I-CARE, DI, Ortho: 'AREA / ROOM', 'Temperature' with no unit
    "area": {"room": ["AREA / ROOM"], "pressure": ["Pressure", "Regime Pa"], "oa": ["Min.", "Outdoor", "ACH"],
             "total": ["Min. Total", "ACH"], "single": ["Single Pass"], "aclass": ["Air Class"], "filter": ["Filter"],
             "rh": ["Relative", "Humidity"], "temp": ["Temperature"],
             "after": ["Afterhours", "mode, Y/N"], "setback": ["Setback", "mode, Y/N"]},
}
BASE_CELLS = {"filter": "MERV 15", "rh": "Max 60%", "temp": "21-24", "after": "N", "setback": "N", "aclass": "2",
              "single": "NR", "oa": "2", "total": "6"}


def build_variant_criteria_page(doc: "pymupdf.Document", style: str, rows, subtitle: str = "", title: str = "",
                                note: str = "", dept_column: str = "", booklet_page: str = "139",
                                footer_stamp_month: str = "MAY", strip: str = STRIP_48) -> None:
    """A criteria page with no 'Room conditions' caption and no DESIGN CRITERIA strip: a 28 pt title, an optional
    20 pt subtitle, the header of ``style``, rows of (room, cells) and an optional unnumbered 'Note:' paragraph."""
    cv = Canvas(doc)
    cv.text(745, 55, strip, 15)
    if title:
        cv.text(60, 40, title, 28)
    if subtitle:
        cv.text(60, 80, subtitle, 20)
    cv.rect(50, 120, 1140, 190, fill=(0.35, 0.6, 0.83))
    for key, lines in HEAD_STYLES[style].items():
        top = 155 - 7 * len(lines)
        for i, line in enumerate(lines):
            cv.text(CENTRE[key] if key != "room" else 200, top + 14 * i, line, 11, centre=True)
    if dept_column:
        cv.text(58, 128, "DEPARTMENT", 11)
    y = 215.0
    for room, cells in rows:
        cv.text(150 if not dept_column else 150, y, room, 11)
        for key, val in {**BASE_CELLS, **cells}.items():
            cv.text(CENTRE[key], y, val, 11, centre=True)
        y += 34.0
    if dept_column:
        cv.text(70, 300, dept_column, 22)                        # the merged vertical department cell, left of the rooms
    if note:
        cv.text(60, y + 30, note, 14)
    cv.text(60, 805, "ALEXANDRA HOSPITAL REDEVELOPMENT PROJECT | DETAILED DESIGN BOOKLET | " + footer_stamp_month + " 2025", 11)
    cv.text(1150, 808, booklet_page, 14)


def test_split_header_without_caption_is_read_and_unsigned_pressure_is_reported(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_variant_criteria_page(doc, "split", [
        ("ICU Bedrooms", {"pressure": "Positive", "single": "N/R", "rh": "≤60", "total": "10"}),
        ("Disposal/Dirty Utility", {"pressure": "-5", "single": "Yes", "total": "20", "aclass": "4"}),
        ("ICU Bedroom (PP ISO)", {"pressure": "30", "filter": "HEPA", "total": "12", "aclass": "1"}),   # no sign printed
        ("CT Scan", {"pressure": "+5", "total": "15"})], strip=STRIP_48 + " | DESIGN CRITERIA")
    res = _parse(_save(doc, tmp_path, "ICU_X_Section_4.8_Mechanical_Ventilation_Strategy.pdf"), tmp_path)
    rows = res["criteria"]
    assert res["pages"][0]["class"] == "criteria" and len(rows) == 4 and res["page_failures"] == []
    assert {r["table_part"] for r in rows} == {b.NO_CAPTION_PART}
    assert [(r["pressure_pa"], r["pressure_direction"]) for r in rows] == [("", "positive"), ("-5", "negative"), ("", ""), ("5", "positive")]
    assert [r["single_pass"] for r in rows] == ["NR", "Y", "NR", "NR"] and rows[0]["rh_max_pct"] == "60"
    assert [f["row"] for f in res["failures"]] == ["ICU Bedroom (PP ISO)"] and res["failures"][0]["cell"] == "30"
    assert rows[2]["confidence"] == "0.72"                 # the unsigned figure is not given a sign


def test_area_room_header_with_operation_mode_subtitle_is_a_criteria_page(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_variant_criteria_page(doc, "area", [
        ("Clean Utility", {"pressure": "Neutral", "after": "24 hrs AC"}),
        ("NP Isolation", {"pressure": "-5", "single": "Yes", "total": "12", "aclass": "4"}),
        ("Toilet", {"pressure": "-VE", "single": "YES", "total": "20", "aclass": "3", "filter": "NR", "rh": "NR", "temp": "NR"})],
        title="ACMV", subtitle="• I-Care – Operation Mode", footer_stamp_month="APR")
    res = _parse(_save(doc, tmp_path, "IGH_DD Booklet_I-CARE_BATCH 1_R.pdf"), tmp_path)
    rows = res["criteria"]
    assert res["department"] == "IGH I-CARE BATCH 1" and res["pages"][0]["class"] == "criteria"
    assert len(rows) == 3 and res["failures"] == [] and res["page_failures"] == []
    assert {r["table_part"] for r in rows} == {"I-Care – Operation Mode"}
    assert [r["afterhours"] for r in rows] == ["24 hrs AC", "N", "N"] and json_raw(rows[0])["afterhours"] == "24 hrs AC"
    assert [(r["pressure_pa"], r["pressure_direction"]) for r in rows] == [("", "neutral"), ("-5", "negative"), ("", "negative")]
    assert rows[2]["rh_max_pct"] == "" and rows[2]["temp_min_c"] == ""
    assert b.footer_furniture(b.page_words(doc[0]), H, W)          # 'APR 2025' is recognised as the date stamp


def test_cell_words_of_the_newer_extracts() -> None:
    assert b.parse_single_pass("N/R") == ("NR", True) and b.parse_single_pass("No (c/w Air Purging)") == ("N", True)
    assert b.parse_single_pass("Non-Recirculating") == ("Y", True)
    assert b.parse_yn("NA") == ("NA", True) and b.parse_yn("24 hrs AC") == ("24 hrs AC", True) and b.parse_yn("maybe")[1] is False
    assert b.parse_count("SS 553") == ("", True) and b.parse_count("ss553") == ("", True) and b.parse_count("x")[1] is False
    assert b.parse_rh("N/R") == ("", "", True) and b.parse_temp("N/R") == ("", "", True)
    for cell, direction in (("SLIGHT +VE", "positive"), ("SLIGHTLY +VE", "positive"), ("SLIGHTLY -VE", "negative"),
                            ("-VE", "negative"), ("NEUTRAL", "neutral")):
        r = b.parse_pressure_full(cell)
        assert (r["pa"], r["direction"], r["ok"]) == ("", direction, True)


def test_note_paragraph_and_department_column_are_not_rows(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_variant_criteria_page(doc, "area", [("Anteroom, PP", {"pressure": "+30", "total": "15"}),
                                              ("Gown-Up", {"pressure": "+15", "total": "15"})],
                                subtitle="Basement 2 Pharmacy – Compounding Lab – Setback Mode 2", dept_column="PHARM",
                                note="Note: Reducing of ACH required adjustment on venturi valve")
    res = _parse(_save(doc, tmp_path, "IGH_DD Booklet_Pharmacy_X.pdf"), tmp_path)
    assert [r["room_type_as_printed"] for r in res["criteria"]] == ["Anteroom, PP", "Gown-Up"]   # no 'PHARM', no note row
    assert any(n["text"].startswith("Note: Reducing of ACH") for n in res["notes"])
    assert res["criteria"][0]["table_part"].endswith("Setback Mode 2")


def _image_page(doc: "pymupdf.Document", strip: str = STRIP_48) -> None:
    cv = Canvas(doc)
    cv.text(745, 55, strip, 15)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 120))
    pix.clear_with(225)
    cv.page.insert_image(pymupdf.Rect(60, 100, 1130, 700), pixmap=pix)
    _footer(cv, "71")


def test_criteria_table_that_is_an_image_reads_the_keyed_in_rows_at_lower_confidence(tmp_path: Path, monkeypatch) -> None:
    import json
    sidecar = tmp_path / "vision.json"
    cols = ["room", "pressure", "min_oa", "min_total", "single_pass", "air_class", "filter", "rh", "temp", "afterhours", "setback"]
    sidecar.write_text(json.dumps({"IGH_DD Booklet_IMG.pdf": {"1": {"table_part": "Medical Day Unit – Operation Mode", "cols": cols, "rows": [
        ["Clean Utility", "+VE", "2", "6", "NR", "2", "MERV 15", "Max 60%", "21-24", "Y", "N"],
        ["NP Isolation", "-5", "2", "12", "Yes", "4", "MERV 15", "Max 60%", "21-24", "N", "N"]]}}}), encoding="utf-8")
    monkeypatch.setattr(b, "VISION_ROWS_PATH", sidecar)
    doc = pymupdf.open()
    _image_page(doc)
    res = _parse(_save(doc, tmp_path, "IGH_DD Booklet_IMG.pdf"), tmp_path)
    assert res["pages"][0]["class"] == "criteria" and "keyed in by eye" in res["pages"][0]["detail"]
    rows = res["criteria"]
    assert [(r["room_type_as_printed"], r["pressure_pa"], r["single_pass"], r["afterhours"]) for r in rows] == \
        [("Clean Utility", "", "NR", "Y"), ("NP Isolation", "-5", "Y", "N")]
    assert {r["confidence"] for r in rows} == {"0.80"} and rows[0]["table_part"] == "Medical Day Unit – Operation Mode"
    assert all("read_by" in json_raw(r) and r["pdf_page"] == 1 and r["source_file"] for r in rows)
    other = _parse(_save(doc, tmp_path, "IGH_DD Booklet_OTHER.pdf"), tmp_path)       # no entry for this file: nothing is invented
    assert other["criteria"] == [] and other["pages"][0]["class"] == "other"


def build_titled_standby_page(doc: "pymupdf.Document", title: str, tags, note_ref_title_digit: str = "4") -> None:
    cv = Canvas(doc)
    cv.text(745, 55, STRIP_48, 15)
    cv.text(60, 60, title, 20)
    for x0, x1, lines in ((84.0, 338.0, ["Equipment Tag"]), (338.0, 947.0, ["Area Served"]),
                          (947.0, 1125.0, ["Equipment Standby", "Configuration 1,2"])):
        cv.rect(x0, 105, x1, 182, fill=(0.35, 0.6, 0.83))
        for i, line in enumerate(lines):
            cv.text((x0 + x1) / 2.0, 135 + 18 * i - 9 * (len(lines) - 1), line, 14, centre=True)
    y = 182.0
    for tag, area, cfg in tags:
        for x0, x1 in ((84.0, 338.0), (338.0, 947.0), (947.0, 1125.0)):
            cv.rect(x0, y, x1, y + 40, fill=(0.82, 0.87, 0.94))
        cv.text((84 + 338) / 2.0, y + 12, tag, 14, centre=True)
        cv.text(345, y + 12, area, 14)              # left-aligned: it starts left of the midpoint between the headings
        cv.text(1036, y + 12, cfg, 14, centre=True)
        y += 40
    cv.text(85, 740, "NOTE:-", 14)
    cv.text(85, 757, "1 – REFER TO APPENDIX 7 FOR EQUIPMENT STANDBY CONFIGURATION TYPES", 14)
    _footer(cv, "140")


def test_titled_standby_table_with_two_unit_tags_and_a_level_in_the_title(tmp_path: Path) -> None:
    rows = [("AHU-B-07-04ICU-01", "POD B - ICU Bedrooms/Clean Utility", "AHU CONFIG-C"),
            ("AHU-B-07-04ICU-01/02C", "POD B - ICU Bedrooms/Clean Utility", "AHU CONFIG-C"),
            ("FCU-A-7-03/20", "OP ICARE Treatment Room Large", "CONFIG-F")]
    doc = pymupdf.open()
    build_titled_standby_page(doc, "Equipment Configuration Summary Level 4 (Part 1)", rows)
    res = _parse(_save(doc, tmp_path, "ICU_X_Section_4.8_Mechanical_Ventilation_Strategy.pdf"), tmp_path)
    assert res["pages"][0]["class"] == "standby summary" and res["page_failures"] == [] and res["failures"] == []
    assert [(r["tag_as_written"], r["area"], r["configuration"]) for r in res["standby"]] == [
        (t, a, c) for t, a, c in rows]
    assert {r["note_refs"] for r in res["standby"]} == {"1,2"}        # not the '4' of 'Level 4' in the page title
    assert [n["note_number"] for n in res["standby_notes"]] == ["1"]


def test_untitled_standby_table_and_the_frozen_file(tmp_path: Path) -> None:
    rows = [("AHU-A-10-03/04", "MDU Clinical Rooms", "CONFIG-D")]
    doc = pymupdf.open()
    build_titled_standby_page(doc, "", rows)
    fresh = _parse(_save(doc, tmp_path, "IGH_DD Booklet_MDU_OCP.pdf"), tmp_path)
    assert fresh["pages"][0]["class"] == "standby summary" and fresh["standby"][0]["tag_as_written"] == "AHU-A-10-03/04"
    frozen = _parse(_save(doc, tmp_path, b.FROZEN_LAYOUT_FILES[0]), tmp_path)       # Healthy Aging p23: left as released
    assert frozen["pages"][0]["class"] == "other" and frozen["standby"] == []
    doc2 = pymupdf.open()
    build_titled_standby_page(doc2, "Equipment Configuration Summary", rows)
    assert _parse(_save(doc2, tmp_path, b.FROZEN_LAYOUT_FILES[0]), tmp_path)["pages"][0]["class"] == "other"
    assert _parse(_save(doc2, tmp_path, "IGH_DD Booklet_X.pdf"), tmp_path)["pages"][0]["class"] == "standby summary"


def test_plan_heading_in_24_pt_and_the_stale_strip(tmp_path: Path) -> None:
    def plan(doc, heading, strip, size=24):
        cv = Canvas(doc)
        cv.text(745, 55, strip, 15)
        cv.text(60, 60, heading, size)
        cv.text(900, 150, "IP 4TH STOREY KEYPLAN – PART 1", 12)
        cv.text(300, 300, "STAFF", 10)
        cv.text(400, 400, "-5 Pa", 8)
        _footer(cv, "147")
    doc = pymupdf.open()
    plan(doc, "PRESSURE REGIME – BAU (LEVEL 4)", STRIP_48)                                   # 24 pt, not the 28 pt heading
    plan(doc, "AHU ZONING", "4.1 DEPARTMENT LAYOUT PLAN")                                    # a plan under a stale strip
    plan(doc, "SOME AV OVERVIEW", "4.1.10 AV STRATEGY")                                      # another section: still 'other'
    res = _parse(_save(doc, tmp_path), tmp_path)
    assert [p["class"] for p in res["pages"]] == ["pressure regime", "zoning keyplan", "other"]
    assert res["pressure"][0]["page_heading"].startswith("PRESSURE REGIME – BAU")
    assert b.classify_page("x", "", "4.8 MECHANICAL VENTILATION STRATEGY | PRESSURE REGIME") == "pressure regime"   # MDU OCP


def test_combined_file_page_map_names_the_section_of_each_page(tmp_path: Path) -> None:
    doc = pymupdf.open()
    for strip in ("4.1.8 MECHANICAL VENTILATION STRATEGY", "4.2.8 MECHANICAL VENTILATION STRATEGY"):
        build_standby_page(doc)
        doc[-1].insert_text((745, 68), strip, fontname="helv", fontsize=15)
    res = _parse(_save(doc, tmp_path, "Rehab_Med_Combined_Mechanical_Ventilation_Strategy.pdf"), tmp_path)
    assert res["department"] == "Rehab Med Combined"
    assert [p["section"] for p in res["pages"]] == ["4.1.8", "4.2.8"]
    assert [p["detail"].endswith("section %s" % s) for p, s in zip(res["pages"], ("4.1.8", "4.2.8"))] == [True, True]
    single = pymupdf.open()
    build_standby_page(single)
    single[-1].insert_text((745, 68), STRIP_48, fontname="helv", fontsize=15)
    assert "section" not in _parse(_save(single, tmp_path), tmp_path)["pages"][0]["detail"]     # one section: page map unchanged


def test_department_names_and_the_files_a_default_run_reads() -> None:
    for name, dept in (("ICU_HDU_Section_4.8_Mechanical_Ventilation_Strategy.pdf", "ICU HDU"),
                       ("IGH_DD Booklet_I-CARE_BATCH 1_R.pdf", "IGH I-CARE BATCH 1"),
                       ("IGH_DD Booklet_MDU_OCP.pdf", "IGH MDU OCP"),
                       ("20250819_IGH_DD Booklet_DI_Signed_All_AH.pdf", "IGH DI Signed All AH"),
                       ("Rehab_Med_Combined_Mechanical_Ventilation_Strategy.pdf", "Rehab Med Combined"),
                       ("IP_FAST_Section_4.8_Mechanical_Ventilation_Strategy.pdf", "IP FAST")):
        assert b.department_from_name(Path(name)) == dept
    assert all(b.is_booklet_name(Path(n)) for n in ("IGH_DD Booklet_MDU_OCP.pdf", "Rehab_Med_Combined_Mechanical_Ventilation_Strategy.pdf",
                                                    "Paeds_Section_4.8_Mechanical_Ventilation_Strategy.pdf"))
    assert not b.is_booklet_name(Path("5. Equipment Schedules (Corr 5).pdf"))


def test_zone_is_blank_and_pa_is_signed_on_a_standard_table(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_criteria_page(doc, pa_rows=True)
    res = _parse(_save(doc, tmp_path), tmp_path)
    assert {r["zone"] for r in res["criteria"]} == {""}                       # blank for every department but Emergency
    by = {r["room_type_as_printed"]: r for r in res["criteria"]}
    assert by["Isolation Room"]["pressure_pa"] == "-15" and by["Plant Room"]["pressure_pa"] == "10"
    assert by["Iso Anteroom"]["pressure_pa"] == "-15"                         # a split sign is still negative


def test_department_mapping_covers_the_booklet_folder_and_skips_non_booklets() -> None:
    folder = Path(__file__).resolve().parent.parent / "data" / "source" / b.BOOKLET_DIR_NAME
    mapping = b.load_departments()
    if folder.is_dir():
        assert {p.name for p in folder.iterdir() if p.is_file()} == set(mapping)        # every file of the folder has an entry
    booklets = {n: e for n, e in mapping.items() if e["booklet"]}
    assert len(booklets) == 38
    assert sorted(e["dept_no"] for e in booklets.values()) == sorted(
        "01 02 03 04 05 06 07 08 09 10 11 12 13 14 15 16 17 18 19 20 22 22 22 23 24 26 28 29 30 34 35 37 39 40 41 42 45 47".split())  # 22 has three booklets (MSOT, DSOT, AMSC)
    assert all(e["dept_title"].startswith(e["dept_no"] + " - ") and e["department_label"] for e in booklets.values())
    assert b.department_info(Path("IGH_DD Booklet_MDU_OCP.pdf")) == ("12", "Medical Day Unit")
    assert b.department_info(Path("20250804_IGH_DD Booklet_Data Centre R2_Markup.pdf")) == ("42", "Data Centre")
    assert b.department_info(Path("synthetic_booklet.pdf")) == ("", "")               # an unlisted file gets no number
    for name in ("AHU Cross Verification - MOT NSC Register.xlsx", "AH Stacking Diagram.pdf", "5. Equipment Schedules (Corr 5).pdf"):
        assert mapping[name]["booklet"] is False and not b.is_booklet_name(Path(name))
        assert b.department_info(Path(name)) == ("", "")


def test_registers_carry_dept_no_and_department_label_beside_department(tmp_path: Path) -> None:
    assert [f for f in b.CRITERIA_FIELDS[:3]] == ["department", "dept_no", "department_label"]
    for fields in (b.NOTES_FIELDS, b.STANDBY_FIELDS, b.ROOM_DATA_FIELDS, b.ZONING_FIELDS, b.PRESSURE_FIELDS):
        assert "dept_no" in fields and "department_label" in fields and "department" in fields
    doc = pymupdf.open()
    build_criteria_page(doc, pa_rows=True)
    src = _save(doc, tmp_path)
    named = src.with_name("IGH_DD Booklet_MDU_OCP.pdf")                                 # a listed name: the mapping applies
    shutil.copy(src, named)
    res = _parse(named, tmp_path)
    assert res["criteria"] and res["department"] == "IGH MDU OCP"
    assert {(r["dept_no"], r["department_label"]) for r in res["criteria"]} == {("12", "Medical Day Unit")}
    unlisted = _parse(src, tmp_path)
    assert {(r["dept_no"], r["department_label"]) for r in unlisted["criteria"]} == {("", "")}
    folder = tmp_path / "folder"
    folder.mkdir()
    shutil.copy(src, folder / "AH Stacking Diagram.pdf")                                 # marked as not a booklet: skipped
    shutil.copy(src, folder / "Unlisted_Section_4.8_Mechanical_Ventilation_Strategy.pdf")
    assert [p.name for p in b.collect_inputs([str(folder)], tmp_path)] == ["Unlisted_Section_4.8_Mechanical_Ventilation_Strategy.pdf"]


# --------------------------------------------------------------------------- #
# Corrections after verification (4): Pharmacy layout, reviewer marks over cells, DI / ICU / MDU / Rehab
# --------------------------------------------------------------------------- #
def test_slightly_pressure_word_stays_in_the_pressure_column_and_long_title_keeps_its_mode(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_variant_criteria_page(doc, "area", [
        ("IPAS / Kiosk", {"pressure": "SLIGHTLY +VE"}), ("Pantry", {"pressure": "SLIGHTLY -VE"}), ("Store", {"pressure": "NEUTRAL"})],
        title="", subtitle="Basement 2 Pharmacy – Compounding Lab – Setback Mode 1")
    page = doc[0]
    # the title runs on past the section strip's left edge (x = 650): a 20 pt line below the strip, as on Pharmacy p8
    page.insert_text((540, 100 + 20 * 0.85), "Mode 1", fontname="f", fontsize=20)
    res = _parse(_save(doc, tmp_path, "IGH_DD Booklet_Pharmacy_Y.pdf"), tmp_path)
    rows = res["criteria"]
    assert [r["room_type_as_printed"] for r in rows] == ["IPAS / Kiosk", "Pantry", "Store"]      # no 'SLIGHTLY' in the name
    assert [r["pressure_regime_as_printed"] for r in rows] == ["SLIGHTLY +VE", "SLIGHTLY -VE", "NEUTRAL"]
    assert [r["pressure_direction"] for r in rows] == ["positive", "negative", "neutral"]
    assert all(not r["room_type_norm"].endswith("slightly") for r in rows)


def test_title_words_past_x650_below_the_strip_are_part_of_the_table_part(tmp_path: Path) -> None:
    doc = pymupdf.open()
    cv = Canvas(doc)
    cv.page.insert_text((745, 18 + 15 * 0.85), STRIP_48, fontname="f", fontsize=15)
    cv.text(64, 85, "Basement 2 Pharmacy – Compounding Lab –", 22)
    cv.text(560, 85, "Setback Mode", 22)
    cv.text(716, 85, "3", 22)                                    # the mode number prints right of x = 650
    cv.rect(50, 120, 1140, 190, fill=(0.35, 0.6, 0.83))
    for key, lines in HEAD_STYLES["area"].items():
        for i, line in enumerate(lines):
            cv.text(CENTRE[key] if key != "room" else 200, 148 + 14 * i - 7 * len(lines), line, 11, centre=True)
    cv.text(150, 215, "Gown-Up", 11)
    for key, val in {**BASE_CELLS, "pressure": "+15"}.items():
        cv.text(CENTRE[key], 215, val, 11, centre=True)
    _footer(cv, "140")
    res = _parse(_save(doc, tmp_path, "IGH_DD Booklet_Pharmacy_Z.pdf"), tmp_path)
    assert [r["table_part"] for r in res["criteria"]] == ["Basement 2 Pharmacy – Compounding Lab – Setback Mode 3"]


def _rotated(page: "pymupdf.Page", x: float, y_bottom: float, text: str, size: float = 16.0) -> None:
    page.insert_text((x, y_bottom), text, fontname="f", fontsize=size, rotate=90)


def test_merged_department_cell_gives_the_zone_of_the_rows_it_spans(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_variant_criteria_page(doc, "area", [("Pantry", {"pressure": "NEUTRAL"}), ("Locker", {"pressure": "-VE"}),
                                              ("UPS Room", {"pressure": "NEUTRAL"})], title="", subtitle="Basement 2 Pharmacy")
    page = doc[0]
    page.insert_text((58, 128 + 11 * 0.85), "DEPARTMENT", fontname="f", fontsize=11)
    page.draw_rect(pymupdf.Rect(56, 205, 140, 275), color=None, fill=(0.8, 0.85, 0.95))        # merged cell over two rows
    page.draw_rect(pymupdf.Rect(56, 275, 140, 309), color=None, fill=(0.9, 0.93, 0.97))        # a cell of its own
    _rotated(page, 90, 268, "LEVEL B2 – PHARM", 7.0)                                           # reads bottom to top
    _rotated(page, 80, 300, "B", 7.0)                                                          # wrapped one character per line
    _rotated(page, 100, 300, "1", 7.0)
    page.draw_rect(pymupdf.Rect(0, 0, 51, 842), color=None, fill=(0.5, 0.5, 0.5))              # the binding gutter strip
    _rotated(page, 30, 600, "BINDING GUTER- TO BE REMOVED WHEN PRINTING", 12)
    res = _parse(_save(doc, tmp_path, "IGH_DD Booklet_Pharmacy_W.pdf"), tmp_path)
    rows = res["criteria"]
    assert [r["room_type_as_printed"] for r in rows] == ["Pantry", "Locker", "UPS Room"]
    assert [r["zone"] for r in rows] == ["LEVEL B2 – PHARM", "LEVEL B2 – PHARM", "B1"]
    # a table with no DEPARTMENT column keeps a blank zone
    doc2 = pymupdf.open()
    build_variant_criteria_page(doc2, "area", [("Pantry", {"pressure": "NEUTRAL"})], title="", subtitle="I-Care – Operation Mode")
    assert [r["zone"] for r in _parse(_save(doc2, tmp_path, "IGH_DD Booklet_I-CARE_Q.pdf"), tmp_path)["criteria"]] == [""]


def _words(*items):
    return [b.Word(x, y, x + 6.0 * len(t), y + 12.0, t, 12.0) for t, x, y in items]


def test_wrapped_room_names_are_closed_up_after_a_hyphen_and_before_a_stranded_letter() -> None:
    assert b.join_room_words(_words(("Workstation/Multi-", 60, 100), ("Purpose", 60, 118), ("Room", 110, 118))) == \
        "Workstation/Multi-Purpose Room"
    assert b.join_room_words(_words(("Preparation/Inspectio", 64, 421), ("n", 64, 442), ("Room", 76, 442))) == \
        "Preparation/Inspection Room"
    assert b.join_room_words(_words(("Sterile", 64, 205), ("Compounding", 110, 205), ("Clean", 64, 227), ("Room,", 104, 227))) == \
        "Sterile Compounding Clean Room,"
    assert b.join_room_words(_words(("ATDPS", 64, 205), ("–", 110, 205), ("Cleaing", 64, 227))) == "ATDPS – Cleaing"
    assert b.join_room_words(_words(("Wing", 64, 205), ("A", 64, 227), ("Store", 80, 227))) == "Wing A Store"      # a capital is a word


def _mark_rows(*boxes):
    return [{"cells": {}, "box": box, "marks": []} for box in boxes]


def _comment(tip, rect, text="comment", mod=None, order=0):
    return {"kind": "comment", "type": "FreeText", "text": text, "author": "", "tip": tip, "rect": rect, "mod": mod, "order": order}


def _region(kind, rect, created=None):
    return {"kind": "region", "type": kind, "author": "rev", "rect": rect, "created": created}


def test_circles_polygons_and_highlights_are_attached_to_the_rows_they_cover() -> None:
    cols = b.TableColumns({"room": (60, 120), "pressure": (260, 330), "single_pass": (500, 560)}, 150.0, {})
    rows = _mark_rows((60, 200, 560, 216), (60, 224, 560, 240), (60, 248, 560, 264))
    words = _words(("Ultrasound", 60, 224), ("+ve", 280, 224), ("No", 520, 224))
    marks = [_region("Circle", (250, 221, 340, 243)),                 # round the second row's pressure cell only
             _region("Polygon", (55, 244, 565, 268)),                 # a cloud round the whole third row
             _region("Highlight", (60, 225, 100, 238))]               # the first word of the second row
    b.link_marks(rows, marks, words, cols)
    assert rows[0]["marks"] == []
    text1 = [m["text"] for m in rows[1]["marks"]]
    assert text1[0] == "circled by a reviewer Circle annotation (rev) around its Pressure cell"
    assert text1[1].startswith("highlighted by a reviewer Highlight annotation (rev)") and "'Ultrasound'" in text1[1]
    assert [m["text"] for m in rows[2]["marks"]] == ["outlined by a reviewer Polygon annotation (rev) around its Room, Pressure and Single Pass cells"]


def test_comment_is_linked_to_circled_rows_a_second_arrow_and_a_circle_drawn_just_after_it() -> None:
    cols = b.TableColumns({"room": (60, 120), "pressure": (260, 330), "single_pass": (500, 560)}, 150.0, {})
    # rows: Toilet, Ultrasound, Ultrasound Procedure, MRI, CT Procedure, CT Control Room
    rows = _mark_rows((60, 420, 560, 437), (60, 444, 560, 461), (60, 468, 560, 485),
                      (60, 500, 560, 517), (60, 524, 560, 541), (60, 548, 560, 580))
    words: list = []
    toilet_tip = _comment((212.0, 438.0), (212.0, 44.0, 552.0, 438.0), "Why +ve?", mod=1000.0, order=0)
    air = _comment((520.0, 550.0), (600.0, 300.0, 900.0, 340.0), "Air purging", mod=5000.0, order=1)
    marks = [toilet_tip, air,
             _region("Circle", (250, 442, 340, 465), created=1009.0),         # 9 s after the first comment was saved
             _region("Circle", (250, 466, 340, 488), created=1012.0),
             _region("Circle", (480, 498, 580, 520), created=100.0),          # same cells (Single Pass) as the circle the arrow lands in
             _region("Circle", (480, 522, 580, 544), created=110.0),
             _region("Circle", (480, 546, 580, 584), created=120.0)]
    rows[0]["marks"], rows[5]["marks"] = [toilet_tip], [air]       # the arrow tips as parse_criteria_page finds them (Toilet, CT Control Room)
    b.link_marks(rows, marks, words, cols)
    held = [[m.get("text") for m in r["marks"] if m["kind"] == "comment"] for r in rows]
    assert held == [[], ["Why +ve?"], ["Why +ve?"], ["Air purging"], ["Air purging"], ["Air purging"]]
    assert all(any(m["kind"] == "region" for m in rows[i]["marks"]) for i in range(1, 6)) and not rows[0]["marks"]


def test_second_arrow_and_a_tip_below_the_last_row_attach_the_comment() -> None:
    cols = b.TableColumns({"room": (60, 120), "pressure": (260, 330)}, 150.0, {})
    rows = _mark_rows((60, 380, 560, 416), (60, 470, 560, 486))
    c = _comment((301.0, 391.0), (301.0, 361.0, 542.0, 396.0), "inconsistent", order=0)
    rows[0]["marks"] = [c]
    arrow = {"kind": "arrow", "type": "Line", "verts": [(401.0, 380.0), (307.0, 481.0)]}
    b.link_marks(rows, [c, arrow], [], cols)
    assert [[m.get("text") for m in r["marks"]] for r in rows] == [["inconsistent"], ["inconsistent"]]
    low = _comment((558.0, 500.0), (558.0, 515.0, 780.0, 616.0), "compressor", order=0)          # 14 pt below the last row
    rows2 = _mark_rows((60, 380, 560, 416), (60, 470, 560, 486))
    b.link_marks(rows2, [low], [], cols)
    assert [len(r["marks"]) for r in rows2] == [0, 1]
    far = _comment((558.0, 700.0), (558.0, 715.0, 780.0, 816.0), "far away", order=0)
    rows3 = _mark_rows((60, 380, 560, 416))
    b.link_marks(rows3, [far], [], cols)
    assert rows3[0]["marks"] == []


def test_pdf_seconds_reads_annotation_dates() -> None:
    a, c = b.pdf_seconds("D:20250916065758+08'00'"), b.pdf_seconds("D:20250916065807+08'00'")
    assert c - a == 9.0 and b.pdf_seconds("") is None and b.pdf_seconds("garbage") is None


def test_marks_round_cells_end_to_end_and_released_departments_stay_as_released(tmp_path: Path) -> None:
    def build(path_name: str) -> Path:
        doc = pymupdf.open()
        build_variant_criteria_page(doc, "area", [("Ultrasound", {"pressure": "+VE"}), ("Toilet", {"pressure": "-VE"})],
                                    title="ACMV", subtitle="I-Care – Operation Mode")
        page = doc[0]
        page.add_circle_annot(pymupdf.Rect(CENTRE["pressure"] - 30, 205, CENTRE["pressure"] + 30, 232))
        return _save(doc, tmp_path, path_name)

    rows = _parse(build("IGH_DD Booklet_I-CARE_C.pdf"), tmp_path)["criteria"]
    assert rows[0]["reviewer_markup"] == "circled by a reviewer Circle annotation around its Pressure cell" and rows[1]["reviewer_markup"] == ""
    released = _parse(build("Paeds_Section_4.8_Mechanical_Ventilation_Strategy.pdf"), tmp_path)["criteria"]
    assert [r["reviewer_markup"] for r in released] == ["", ""]          # a released department's register is not changed


def test_number_under_single_pass_is_flagged_as_a_shifted_row_and_repeated_room_names_are_flagged(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_variant_criteria_page(doc, "area", [
        ("Toilet", {"pressure": "-VE", "total": "20", "single": "20", "aclass": "NR", "filter": "3"}),
        ("Anteroom (NEP ISO)", {"pressure": "-15", "total": "10"}), ("Anteroom (NEP ISO)", {"pressure": "15", "total": "2", "filter": "HEPA"}),
        ("Store", {"pressure": "NEUTRAL"})], title="ACMV", subtitle="I-Care – Operation Mode")
    rows = _parse(_save(doc, tmp_path, "IGH_DD Booklet_I-CARE_D.pdf"), tmp_path)["criteria"]
    flag = [json_raw(r).get("source_flag", "") for r in rows]
    assert "shifted one column" in flag[0] and "'20'" in flag[0] and "'NR'" in flag[0] and "'3'" in flag[0]
    assert rows[0]["single_pass"] == "" and rows[0]["air_class"] == "NR" and rows[0]["filter"] == "3"      # as printed
    assert flag[1] == flag[2] and "printed on 2 rows" in flag[1] and "-15 / 15" in flag[1] and flag[3] == ""
    assert rows[1]["pressure_pa"] == "-15" and rows[2]["pressure_pa"] == "" and rows[2]["filter"] == "HEPA"   # values untouched (15 has no sign)


def test_vision_rows_carry_the_department_column_as_zone(tmp_path: Path, monkeypatch) -> None:
    import json
    cols = ["room", "pressure", "min_oa", "min_total", "single_pass", "air_class", "filter", "rh", "temp", "afterhours", "setback"]
    sidecar = tmp_path / "vision.json"
    sidecar.write_text(json.dumps({"IGH_DD Booklet_IMG2.pdf": {"1": {"table_part": "Medical Day Unit – Operation Mode",
                                                                      "department_column": "MDU", "cols": cols, "rows": [
        ["Clean Utility", "+VE", "2", "6", "NR", "2", "MERV 15", "Max 60%", "21-24", "Y", "N"]]}}}), encoding="utf-8")
    monkeypatch.setattr(b, "VISION_ROWS_PATH", sidecar)
    doc = pymupdf.open()
    _image_page(doc)
    assert [r["zone"] for r in _parse(_save(doc, tmp_path, "IGH_DD Booklet_IMG2.pdf"), tmp_path)["criteria"]] == ["MDU"]


def test_design_parameter_page_is_recorded_as_notes(tmp_path: Path) -> None:
    doc = pymupdf.open()
    cv = Canvas(doc)
    cv.text(745, 18, "4   DEPARTMENT DRAWINGS", 15)
    cv.text(745, 54, STRIP_48, 15)
    cv.text(64, 75, "Basement 2 Pharmacy – Compounding Lab – Design Parameter", 26.76)
    cv.text(66, 110, "1. Grade C:", 14)
    cv.text(80, 130, "- Design parameter for Clean Room is Grade C", 14)
    cv.text(66, 160, "2. Grade D:", 14)
    cv.text(80, 180, "- ISO 8 recommends an air change range of 15-25 ACH", 14)
    cv.text(82, 446, "Ramp-Down Strategies:", 14)
    cv.text(82, 475, "Mode", 14)
    cv.text(416, 475, "Description", 14)
    cv.text(82, 505, "Operation Mode", 14)
    cv.text(416, 505, "Optimal operational parameters", 14)
    cv.text(82, 534, "Setback Mode 1", 14)
    cv.text(416, 534, "ACH reduced to minimum allowable limit,", 14)
    cv.text(416, 556, "maintained", 14)
    cv.text(82, 584, "Setback Mode 2", 14)
    cv.text(416, 584, "CHW supply cut off", 14)
    cv.text(82, 643, "Recommend Setback Mode: Setback Mode", 14)
    cv.text(416, 643, "Greater Energy Savings: CHW energy usage is dominant", 14)
    cv.text(82, 664, "2", 14)
    cv.text(416, 664, "cutting-off supply", 14)
    cv.text(416, 707, "Easier Control: Adjusting CHW supply is straightforward", 14)
    _footer(cv, "137")
    res = _parse(_save(doc, tmp_path, "IGH_DD Booklet_Pharmacy_P.pdf"), tmp_path)
    assert res["pages"][0]["class"] == "other"
    got = [(n["note_number"], n["text"], n["note_kind"]) for n in res["notes"] if n["note_kind"] == "design parameter text"]
    assert got == [
        ("1", "Grade C: - Design parameter for Clean Room is Grade C", "design parameter text"),
        ("2", "Grade D: - ISO 8 recommends an air change range of 15-25 ACH", "design parameter text"),
        ("Operation Mode", "Optimal operational parameters", "design parameter text"),
        ("Setback Mode 1", "ACH reduced to minimum allowable limit, maintained", "design parameter text"),
        ("Setback Mode 2", "CHW supply cut off", "design parameter text"),
        ("Recommend Setback Mode", "Setback Mode 2", "design parameter text"),
        ("Greater Energy Savings", "CHW energy usage is dominant cutting-off supply", "design parameter text"),
        ("Easier Control", "Adjusting CHW supply is straightforward", "design parameter text")]


def test_wrapped_room_name_is_closed_up_on_a_page_and_left_as_before_for_a_released_department(tmp_path: Path) -> None:
    def build(name: str) -> list:
        doc = pymupdf.open()
        build_variant_criteria_page(doc, "area", [("Workstation/Multi-", {"pressure": "NEUTRAL"}),
                                                  ("Preparation/Inspectio", {"pressure": "NEUTRAL"})],
                                    title="ACMV", subtitle="I-Care – Operation Mode")
        doc[0].insert_text((150, 215 + 13 + 9), "Purpose Room", fontname="f", fontsize=11)
        doc[0].insert_text((150, 249 + 13 + 9), "n Room", fontname="f", fontsize=11)
        return [r["room_type_as_printed"] for r in _parse(_save(doc, tmp_path, name), tmp_path)["criteria"]]

    assert build("IGH_DD Booklet_I-CARE_E.pdf") == ["Workstation/Multi-Purpose Room", "Preparation/Inspection Room"]
    assert build("Palliative_Section_4.8_Mechanical_Ventilation_Strategy.pdf") == \
        ["Workstation/Multi- Purpose Room", "Preparation/Inspectio n Room"]        # released: its register is not changed


# --------------------------------------------------------------------------- #
# Short DD booklets: A4 pages stored portrait with /Rotate 90 (AMSC, CATH, DSOT, Endo, LabMed, MSOT)
# --------------------------------------------------------------------------- #
K = 842.0 / 1191.0           # the tests draw in A3 space; the page is A4


def _short_page(doc: "pymupdf.Document", stamp_number: str = "111"):
    """A /Rotate 90 page, portrait mediabox 595 x 842, drawn with a helper that takes upright A3 coordinates."""
    page = doc.new_page(width=595.0, height=842.0)
    page.set_rotation(90)
    page.insert_font(fontname="f", fontfile=FONT_FILE)
    inv = ~page.rotation_matrix

    def text(x: float, y: float, s: str, size: float = 12.0) -> None:
        dx, dy = text.offset          # a second layer is laid down 12 pt right and 2.4 pt down (LabMed p6)
        p = pymupdf.Point((x + dx) * K, (y + dy + size * 0.85) * K) * inv
        page.insert_text(p, s, fontname="f", fontsize=size * K, rotate=90)

    def rect(x0: float, y0: float, x1: float, y1: float) -> None:
        r = pymupdf.Rect(x0 * K, y0 * K, x1 * K, y1 * K) * inv
        page.draw_rect(pymupdf.Rect(min(r.x0, r.x1), min(r.y0, r.y1), max(r.x0, r.x1), max(r.y0, r.y1)),
                       color=None, fill=(0.8, 0.85, 0.95))

    text.offset = (0.0, 0.0)
    text(700, 24, "4 DEPARTMENT DRAWINGS", 28)
    text(660, 59, "4.8 MECHANICAL VENTILATION STRATEGY | DESIGN CRITERIA", 15)
    text(60, 800, "ALEXANDRA HOSPITAL REDEVELOPMENT PROJECT | DETAILED DESIGN BOOKLET | MAY 2025", 12)
    text(1150, 805, stamp_number, 16)
    return text, rect


def _short_header(text, first: str = "Theatre") -> None:
    text(first and 100, 142, first, 14) if first else None
    text(239, 142, "Room", 12)
    for x, lines in ((482, ["Pressure", "Regime,", "Pa"]), (551, ["Min.", "Outd", "oor ACH"]),
                     (623, ["Min.", "Total", "ACH"]), (705, ["Single", "Pass"]), (769, ["Air", "Class"]),
                     (849, ["Filter"]), (906, ["Relative H", "umidity"]), (979, ["Design Te", "mperatur", "e, C"]),
                     (1049, ["Afterhour", "s mode,", "Y/N"]), (1124, ["Setback", "mode,", "Y/N"])):
        for i, ln in enumerate(lines):
            text(x, 125 + 17 * i, ln, 12)
    text(1088, 160, "1", 9)


def _short_row(text, y: float, room: str, vals: list) -> None:
    text(246, y, room, 12)
    for x, v in zip((492, 575, 645, 715, 785, 825, 925, 990, 1070, 1140), vals):
        text(x, y, v, 12)


def test_short_booklet_rotated_page_rows_and_group_labels(tmp_path: Path) -> None:
    doc = pymupdf.open()
    text, rect = _short_page(doc)
    _short_header(text)
    rect(56, 175, 234, 300)
    rect(56, 300, 234, 420)
    for y0, y1 in ((175, 237), (237, 300), (300, 360), (360, 420)):
        rect(234, y0, 474, y1)
    text(80, 215, "Standard", 12)
    text(80, 233, "Operating Theatre", 12)
    text(80, 345, "NEP", 12)
    text(80, 363, "Operating Theatre", 12)
    vals = ["+25", "5", "25", "N/R", "2", "HEPA", "30-60", "18-25", "N", "Y"]
    _short_row(text, 200, "Operating Theatre", vals)
    _short_row(text, 262, "Clean Up & Dirty Utility", ["-5", "2", "20", "Yes", "4", "MERV 15", "≤60", "20-24", "N", "Y"])
    _short_row(text, 322, "Ante Room (Staff)", ["+35", "15", "15", "Yes", "1", "HEPA", "30-60", "18-25", "N", "Y"])
    _short_row(text, 382, "Anaesthetic Preparation", ["-5", "15", "15", "Yes", "4", "MERV 15", "30-60", "18-25", "N", "Y"])
    res = _parse(_save(doc, tmp_path, "DD Booklet_DSOT_X.pdf"), tmp_path)
    rows = res["criteria"]
    assert res["page_failures"] == [] and [r["room_type_as_printed"] for r in rows] == [
        "Operating Theatre", "Clean Up & Dirty Utility", "Ante Room (Staff)", "Anaesthetic Preparation"]
    assert [r["zone"] for r in rows] == ["Standard Operating Theatre"] * 2 + ["NEP Operating Theatre"] * 2
    first = rows[0]
    assert (first["pressure_regime_as_printed"], first["pressure_pa"], first["min_oa_ach"], first["min_total_ach"]) == ("+25", "25", "5", "25")
    assert (first["single_pass"], first["air_class"], first["filter"], first["rh_as_printed"], first["temp_as_printed"]) == \
        ("NR", "2", "HEPA", "30-60", "18-25")
    assert (first["afterhours"], first["setback"], first["note_refs"], first["booklet_page"]) == ("N", "Y", "1", "111")
    assert rows[1]["single_pass"] == "Y" and rows[1]["rh_as_printed"] == "≤60"
    x0, y0, x1, y1 = (float(v) for v in first["bbox"].split(","))        # boxes are in A4 points of the displayed page
    assert 0 < x0 < x1 <= 842.0 and 0 < y0 < y1 <= 595.0


def test_short_booklet_without_group_column_and_with_rooms_caption(tmp_path: Path) -> None:
    doc = pymupdf.open()
    text, rect = _short_page(doc, "95")
    text(73, 38, "Rooms Part 2", 20)
    _short_header(text, first="")
    for y0, y1 in ((175, 237), (237, 300)):
        rect(56, y0, 474, y1)
    _short_row(text, 200, "Anteroom (Bronchoscopy)", ["+10", "10", "10", "N/R", "4", "HEPA", "40-60", "21-24", "N", "N"])
    _short_row(text, 262, "Enclosed (Endo)", ["Neutral", "2", "6", "N/R", "2", "MERV 15", "≤60", "21-24", "N", "N"])
    res = _parse(_save(doc, tmp_path, "DD Booklet_Endo_X.pdf"), tmp_path)
    rows = res["criteria"]
    assert [r["room_type_as_printed"] for r in rows] == ["Anteroom (Bronchoscopy)", "Enclosed (Endo)"]
    assert all(r["zone"] == "" and r["table_part"] == "Rooms Part 2" for r in rows)       # not the Emergency zoned layout
    assert rows[1]["pressure_direction"] == "neutral" and rows[0]["pressure_pa"] == "10"


def test_short_booklet_second_layer_over_the_first_is_not_read_twice(tmp_path: Path) -> None:
    doc = pymupdf.open()
    text, rect = _short_page(doc)
    rect(56, 175, 234, 560)
    for i in range(8):
        rect(234, 175 + 48 * i, 474, 223 + 48 * i)
    for offset, air in (((0.0, 0.0), "3"), ((12.0, 2.4), "4")):          # the covered layer says Air Class 3, the top one 4
        text.offset = offset
        _short_header(text)
        for i in range(8):
            _short_row(text, 190 + 48 * i, "Room %d" % (i + 1), ["-20", "2", "8", "Yes", air, "MERV 15", "30-60", "21-23", "N", "N"])
        text(80, 360, "MICROBIOLOGY", 14)
    res = _parse(_save(doc, tmp_path, "DD Booklet_LabMed_X.pdf"), tmp_path)
    rows = res["criteria"]
    assert len(rows) == 8 and [r["room_type_as_printed"] for r in rows][:2] == ["Room 1", "Room 2"]
    assert {r["air_class"] for r in rows} == {"4"} and {r["min_oa_ach"] for r in rows} == {"2"}
    assert {r["zone"] for r in rows} == {"MICROBIOLOGY"}


def build_cssd_page(doc: "pymupdf.Document") -> None:
    """CSSD (4.8, A3): 'ROOM CONDITIONS' title, no 'DESIGN CRITERIA' strip; headings wrap mid-word ('Temperat' / 'ure, C',
    'Afterhour' / 's mode,'); a room name and the filter 'MERV 15' wrap over two lines; footnotes carry no NOTE marker."""
    cv = Canvas(doc)
    cv.text(744, 20, "4 DEPARTMENT DRAWINGS", 28)
    cv.text(67, 55, "ROOM CONDITIONS", 28)
    cv.text(744, 62, "4.8 MECHANICAL VENTILATION STRATEGY", 20)
    x = {"room": 213, "pressure": 420, "oa": 515, "total": 630, "single": 710, "aclass": 770, "filter": 830,
         "rh": 905, "temp": 970, "after": 1040, "setback": 1110}
    heads = (("room", "Room", 166), ("pressure", "Pressure", 150), ("pressure", "Regime,", 166), ("pressure", "Pa", 183),
             ("oa", "Min. Outdoor ACH", 166), ("total", "Min. Total ACH", 166), ("single", "Single", 158),
             ("single", "Pass", 175), ("aclass", "Air Class", 166), ("filter", "Filter", 166),
             ("rh", "Relative", 158), ("rh", "Humidity", 175), ("temp", "Design", 150), ("temp", "Temperat", 166),
             ("temp", "ure, C", 183), ("after", "Afterhour", 150), ("after", "s mode,", 166), ("after", "Y/N", 183),
             ("setback", "Setback", 150), ("setback", "mode,", 166), ("setback", "Y/N", 183))
    for key, line, y in heads:
        cv.text(x[key], y, line, 14, centre=key != "room")
    cv.text(x["after"] + 30, 183, "1,2", 9)
    rows = [
        (["Decontamination"], ["-5", "2", "12", "Y", "4", "HEPA", "≤60", "16-24", "N", "N"]),
        (["Dirty Lift Lobby"], ["-2.5", "2", "12", "Y", "4", "HEPA", "≤60", "16-24", "N", "N"]),
        (["Support Areas (Staff Facilities / Receiving / Other", "Store)"],
         ["0", "2", "8", "NA", "2", "MERV|15", "≤65", "23-25", "N", "N"]),
        (["DI Water / Waste Cleaner Room / Change Room"],
         ["-ve", "10", "10", "YES", "4", "NA", "NA", "NA", "N", "N"]),
    ]
    y = 220
    for names, vals in rows:
        two = len(names) > 1
        for i, name in enumerate(names):
            cv.text(77, y - 2 + 20 * i, name, 16)
        for key, val in zip(("pressure", "oa", "total", "single", "aclass", "filter", "rh", "temp", "after", "setback"), vals):
            if "|" in val:         # the filter wraps over two lines, above and below the value line
                top, bottom = val.split("|")
                cv.text(x[key], y - 2, top, 16, centre=True)
                cv.text(x[key], y + 18, bottom, 16, centre=True)
            else:
                cv.text(x[key], y + (8 if two else 0), val, 16, centre=True)
        y += 70 if two else 45
    cv.text(85, 640, "1 – REFER TO APPENDIX 4.D FOR DEPARTMENT SCHEDULE OF OPERATION.", 14)
    cv.text(85, 657, "2 – AFTERHOURS MODE APPLIES TO DEPARTMENTS THAT DO NOT OPERATE CONTINUOUSLY.", 14)
    _footer(cv, "88")


def test_cssd_room_conditions_page_with_wrapped_headings(tmp_path: Path) -> None:
    doc = pymupdf.open()
    build_cssd_page(doc)
    res = _parse(_save(doc, tmp_path, "20251212_IGH_DD_Booklet_CSSD_R1_DPA_AH.pdf"), tmp_path)
    assert res["pages"][0]["class"] == "criteria"
    rows = {r["room_type_as_printed"]: r for r in res["criteria"]}
    assert list(rows) == ["Decontamination", "Dirty Lift Lobby",
                          "Support Areas (Staff Facilities / Receiving / Other Store)",
                          "DI Water / Waste Cleaner Room / Change Room"]
    d = rows["Decontamination"]
    assert (d["pressure_pa"], d["min_oa_ach"], d["min_total_ach"], d["single_pass"], d["air_class"], d["filter"],
            d["rh_as_printed"], d["rh_max_pct"], d["temp_as_printed"], d["afterhours"], d["setback"]) == \
        ("-5", "2", "12", "Y", "4", "HEPA", "≤60", "60", "16-24", "N", "N")
    assert d["note_refs"] == "1;2" and d["confidence"] == "0.97"
    assert rows["Dirty Lift Lobby"]["pressure_pa"] == "-2.5"            # a decimal pressure, not 2.5 or -2
    sup = rows["Support Areas (Staff Facilities / Receiving / Other Store)"]
    assert sup["filter"] == "MERV 15" and sup["single_pass"] == "NA" and sup["pressure_direction"] == "neutral"
    di = rows["DI Water / Waste Cleaner Room / Change Room"]
    assert (di["pressure_regime_as_printed"], di["pressure_pa"], di["pressure_direction"], di["single_pass"],
            di["filter"], di["rh_as_printed"], di["temp_as_printed"], di["rh_max_pct"], di["temp_min_c"]) == \
        ("-ve", "", "negative", "Y", "NA", "NA", "NA", "", "")
    assert '"single_pass": "YES"' in di["raw_cells"] and di["confidence"] == "0.97"
    assert [n["note_number"] for n in res["notes"]] == ["1", "2"]            # the footnotes are notes, not table rows
    assert not res["failures"]
