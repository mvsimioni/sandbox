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
                        interview: bool = False) -> None:
    cv = Canvas(doc)
    cv.text(60, 100, "DESIGN CRITERIA", 14)
    cv.rect(50, 130, 1140, 200, fill=(0.35, 0.6, 0.83))
    head = {
        "room": ["Room"], "pressure": ["Pressure", "Regime, Pa"], "oa": ["Min.", "Outdoor", "ACH"],
        "total": ["Min.", "Total ACH"], "single": ["Single", "Pass"], "aclass": ["Air Class"],
        "filter": ["Filter"], "rh": ["Relative", "Humidity"], "temp": ["Design", "Temperature", ", C"],
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
    cv.text(60, 740, "NOTE:-", 14)
    cv.text(85, 758, "1 – AFTERHOURS MODE APPLIES TO DEPARTMENTS THAT DO NOT OPERATE", 14)
    cv.text(85, 776, "CONTINUOUSLY (24/7) BUT HAVE SPECIFIC ROOMS.", 14)
    cv.text(60, 40, "Room conditions – Part 3", 30)        # caption (here at the top; the Surgical booklet has it below)
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
