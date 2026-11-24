"""Tests of the C4 checks P4-13 to P4-23 and of the seeded-error test (D-072 to D-088).  Small synthetic inputs, no build files."""
import csv
import re
from pathlib import Path

import pytest

from src import phase4_c4 as c4
from src import phase4_seeded as seeded

COLS = ["change_id", "sheet", "page", "bbox", "layer_type", "drawn", "required", "action", "source", "unit", "item", "quantity",
        "unit_of_measure", "class", "note", "ledger_id", "ledger_ids", "room_no", "dept", "link_type", "pressure"]


def write_rows(path: Path, rows, cols=COLS):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


@pytest.fixture()
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(c4, "SCH", tmp_path)
    monkeypatch.setattr(c4, "NEWD", tmp_path / "new_drawings")
    monkeypatch.setattr(c4, "MARK", tmp_path / "markups")
    c4.reset()
    yield tmp_path
    c4.reset()


def test_tag_keys_and_fragments():
    assert c4.tkey("RAF-B-02-01EDP3PABAY-01A/B") == ("RAF", "B-02-01EDP3PABAY-01")
    assert c4.tkey("AHU-B-07-04ICU-01/02C") == ("AHU", "B-07-04ICU-01")
    assert c4.core_of("AHU-B-B1-B2COR-01") == "B-B1-B2COR-01"
    # a fragment of a tag (broken over two lines) and a placeholder are not tags
    assert c4.tags_in("see AHU-B-08-08 and RAF-B-04-XXX and AHU-B-B1-B2") == []
    assert c4.tags_in("AHU-B-02-01EDP3WAIT-01A/B 8262 L/s") == ["AHU-B-02-01EDP3WAIT-01A/B"]


def test_numbers_under_box():
    assert c4.has_number("OA 3,300 L/s", 3300)
    assert c4.has_number("-15 Pa", 15, by_abs=True)
    assert not c4.has_number("MULTIPURPOSE ROOM", 2)
    assert c4.numbers_in("SA 1,933 L/s EA 0") == [1933.0, 0.0]


def test_cell_reference_parsing():
    m = list(c4.CELL_RE.finditer("Working drawing p1; Workbook D40 tab Airflow cell T12 (room X); Workbook master summary, tab AHU Master, cell L26"))
    assert [(x.group(1), x.group(2), x.group(3)) for x in m] == [("D40", "Airflow", "T12"), ("master summary", "AHU Master", "L26")]
    assert c4.col_index("T") == 19 and c4.col_index("AA") == 26


def test_disposition_heads_count_one():
    one = "shared: units AHU-A; AHU-B"
    two = "reviewed under unit X; query: drawn, not in the zone schedule"
    assert len(c4.DISP_HEADS.findall(one)) == 1
    assert len(c4.DISP_HEADS.findall(two)) == 2
    assert len(c4.DISP_HEADS.findall("")) == 0


def test_p4_20_pressure_and_count(scratch):
    write_rows(scratch / "change_register.csv", [
        dict(change_id="CH-X-0001", sheet="IP-BH-0X", quantity="Pressure target", required="450", unit_of_measure="Pa", action="Query: drawn pressure differs"),
        dict(change_id="CH-X-0002", sheet="IP-BH-0X", quantity="Typical count", required="1.5", unit_of_measure="no.", action="Mark up: change the typical count"),
        dict(change_id="CH-X-0003", sheet="IP-BH-0X", quantity="Supply airflow", required="-5", unit_of_measure="L/s", action="Mark up: increase")])
    (scratch / "markups").mkdir()
    st, ev = c4.check_p4_20_domain(None)
    assert st == c4.FAIL
    txt = " ".join(ev)
    assert "outside -60 to +60" in txt and "whole number" in txt and "negative airflow" in txt


def test_p4_16_add_return_fan_is_refused(scratch, monkeypatch):
    monkeypatch.setattr(c4, "working_sheets", lambda: [])
    write_rows(scratch / "change_register.csv", [
        dict(change_id="CH-X-0001", sheet="IP-BH-0X", action="Mark up: add a return fan", quantity="Return fan airflow", unit="AHU-B-02-01EDP3WAIT-01A/B")])
    st, ev = c4.check_p4_16_false_add(None)
    assert st == c4.FAIL and "add a return fan" in " ".join(ev)


def test_p4_16_tag_drawn_on_a_sheet(scratch, monkeypatch):
    monkeypatch.setattr(c4, "working_sheets", lambda: ["IP-BH-05010"])
    monkeypatch.setattr(c4, "pdf_text", lambda sh: "AHU-B-02-01EDP3WAIT-01A\nSPILL AIR BRANCH SHALL BE X")
    write_rows(scratch / "change_register.csv", [
        dict(change_id="CH-NEW-0001", sheet="NEW", action="New system: draw on a sheet", quantity="Unit with no schematic", unit="AHU-B-02-01EDP3WAIT-01A/B",
             item="AHU-B-02-01EDP3WAIT-01A/B", **{"class": "missing system"})])
    st, ev = c4.check_p4_16_false_add(None)
    assert st == c4.FAIL and "printed on IP-BH-05010" in " ".join(ev)


def test_p4_21_list_only_unit_with_change_id(scratch, monkeypatch):
    monkeypatch.setattr(c4, "mot_keys", lambda: {("AHU", "B-04-04CATH-01")})
    monkeypatch.setattr(c4, "markup_subjects", lambda: {})
    monkeypatch.setattr(c4, "working_sheets", lambda: [])
    write_rows(scratch / "change_register.csv", [
        dict(change_id="CH-05042-0001", sheet="IP-BH-05042", action="Mark up: increase", unit="AHU-B-04-04CATH-01", item="AHU-B-04-04CATH-01")])
    write_rows(scratch / "scope.csv", [], cols=["kind", "unit", "sheet", "list_only", "reason", "department", "keys", "sheets", "status"])
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "MOT differences"
    wb.save(scratch / "schematic_update_register.xlsx")
    st, ev = c4.check_p4_21_mot(None)
    assert st == c4.FAIL and "list-only" in " ".join(ev)


def test_seed_inventory_is_fifteen():
    assert sum(seeded.SEED_COUNT.get(s[0], 1) for s in seeded.SEEDS) == 15


def test_readme_has_no_banned_word_and_short_sentences():
    from src.schematics import build_all
    if not (c4.SCH / "change_register.csv").exists():
        pytest.skip("no change list")
    text = build_all.make_readme(None)
    for t in c4.forbidden():
        assert not c4.word_hits(text, [t]), t
    prose = "\n".join(l for l in text.splitlines() if not l.startswith(("|", "#")))
    assert not [s for s in re.split(r"(?<=[.!?])\s+", prose) if len(s.split()) > 25]
