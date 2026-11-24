"""Schematic review: rules for the Action column, room classes and the outputs."""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path

import pytest

from src.schematics import review as R

ROOT = Path(__file__).resolve().parents[1]
HAVE = all((ROOT / p).exists() for p in (
    "out/registers/schematic_model.json", "out/zoning/zone_schedule.xlsx",
    "out/loads/workbooks/AHU_master_summary.xlsx"))


# ---- keys -------------------------------------------------------------------
def test_tag_keys_pairs_and_types():
    assert R.tag_keys("AHU-B-05-05CORE-01A/B") == {"AHU:B-05-05CORE-01"}
    assert R.tag_keys("AHU group: AHU-B-07-04ICU-05; AHU-B-07-04ICU-06") == {
        "AHU:B-07-04ICU-05", "AHU:B-07-04ICU-06"}
    assert R.tag_keys("AHU-B-07-04ICU-05/06") == {"AHU:B-07-04ICU-05", "AHU:B-07-04ICU-06"}
    assert R.tag_keys("PAHU-B-04-01ED-01A/B/C/D/E") == {"PAHU:B-04-01ED-01"}
    assert R.tag_keys("RAF-B-02-01EDP3WAIT-01B") == {"RAF:B-02-01EDP3WAIT-01"}
    assert R.tag_keys("FCU (tag to confirm) [B3-R035]") == frozenset()


# ---- update rule ------------------------------------------------------------
def test_increase_over_five_percent_or_ten_lps_is_marked_up():
    assert R.judge(1000, 1060, False)[:1] == ("increase",)       # 6 %
    assert R.judge(1000, 1011, False)[0] == "increase"           # 11 L/s
    assert R.judge(1000, 1009, False)[0] == "no change"          # under both limits
    assert R.judge(100, 106, False)[0] == "increase"             # 6 L/s, 6 %
    assert R.judge(100, 104, False)[2] is False


def test_reduction_marked_only_when_room_list_is_complete():
    cls, act, need = R.judge(1000, 800, False)
    assert (cls, need) == ("margin", False) and "margin recorded" in act
    cls, act, need = R.judge(1000, 800, True)
    assert (cls, act, need) == ("reduction", "Mark up: reduction", True)


def test_missing_values():
    assert R.judge(1000, None, True)[0] == "not established"
    assert R.judge(None, 50, False)[0] == "not drawn"
    assert R.judge(None, 0, False)[0] == "no change"


# ---- room classes -------------------------------------------------------------
def _d(**kw):
    return {k: {"count": v, "name": k} for k, v in kw.items()}


def test_room_classes_unchanged_count_renamed_add():
    drawn = _d(**{"BED": 4, "OFFICE": 1, "STORE ROOM": 1})
    zone = _d(**{"BED": 5, "OFFICE": 1, "STORE ROOMS": 1, "PANTRY": 2})
    res = {r["dname"] or r["zname"]: r for r in R.classify_rooms(drawn, zone, True, {}, {})}
    assert res["OFFICE"]["cls"] == "unchanged"
    assert res["BED"]["cls"] == "typical count changed"
    assert res["STORE ROOM"]["cls"] == "name differs"      # wording only: no mark-up
    assert R.ROOM_ACTION["name differs"][1] is False
    assert res["PANTRY"]["cls"] == "to add"


def test_remove_needs_a_complete_zone_list():
    drawn = _d(**{"LAN": 1})
    assert R.classify_rooms(drawn, _d(**{"BED": 1}), False, {}, {})[0]["cls"] == "not established"
    assert R.classify_rooms(drawn, _d(**{"BED": 1}), True, {}, {})[0]["cls"] == "to remove"


def test_moved_rooms():
    drawn = _d(**{"LAN": 1})
    # moved only where this list and the other list are complete (D-054)
    res = R.classify_rooms(drawn, _d(**{"BED": 1}), True, {"LAN": [("FCU-X-01", True)]}, {})
    assert [r["cls"] for r in res if r["dname"] == "LAN"] == ["moved to another unit"]
    res = R.classify_rooms(drawn, _d(**{"BED": 1}), True, {"LAN": [("FCU-X-01", False)]}, {})
    assert [r["cls"] for r in res if r["dname"] == "LAN"] == ["not established"]
    res = R.classify_rooms(drawn, _d(**{"BED": 1}), False, {"LAN": [("FCU-X-01", True)]}, {})
    assert [r["cls"] for r in res if r["dname"] == "LAN"] == ["not established"]
    res = R.classify_rooms({}, _d(**{"CLEAN": 1}), False, {}, {"CLEAN": ["AHU-Y-02"]})
    assert res[0]["cls"] == "moved from another unit"


def test_composite_box_and_prefix():
    d = {"CIRCULATION RECEPTION SUB WAIT": {"count": 1, "name": "CIRCULATION / RECEPTION / SUB - WAIT", "composite": True,
                                             "tokens": R.name_tokens("CIRCULATION / RECEPTION / SUB - WAIT")}}
    z = {n: {"count": 1, "name": n, "tokens": R.name_tokens(n)} for n in ("RECEPTION", "RECEPTION SUB", "SUB WAIT")}
    res = R.classify_rooms(d, z, True, {}, {})
    assert {r["cls"] for r in res} == {"named in box"}
    assert R.canon("P3 POCT") == "POCT" and R.canon("Staff Change (CATH)") == "STAFF CHANGE"
    assert R.ROOM_ACTION["named in box"][1] is False


def test_plan_room_of_the_unit_keeps_the_drawn_room():
    d = _d(**{"REPORTING ROOM": 1})
    d["REPORTING ROOM"]["tokens"] = R.name_tokens("REPORTING ROOM")
    plan = {"REPORT": {"name": "Report", "tokens": R.name_tokens("Report")}}
    res = R.classify_rooms(d, _d(**{"BED": 1}), True, {}, {}, plan)
    assert [r["cls"] for r in res if r["dname"] == "REPORTING ROOM"] == ["unchanged"]


def test_count_below_drawn_on_incomplete_list_is_not_established():
    res = R.classify_rooms(_d(**{"BED": 4}), _d(**{"BED": 2}), False, {}, {})
    assert res[0]["cls"] == "not established"


def test_pressure_parse():
    assert R.parse_pressure("+5 Pa") == 5 and R.parse_pressure("-15 Pa") == -15
    assert R.parse_pressure("NEUTRAL") == 0 and R.parse_pressure(None) is None


# ---- outputs ------------------------------------------------------------------
@pytest.mark.skipif(not HAVE, reason="inputs not built")
class TestOutputs:
    @pytest.fixture(scope="class")
    def rv(self):
        return R.run_review(ROOT)

    def test_every_tagged_unit_has_a_row_set(self, rv):
        tagged = [u for u in rv.i.units if u["has_tag"]]
        assert tagged and all(u["tag"] in rv.unit_info for u in tagged)

    def test_change_ids_are_unique_and_complete(self, rv):
        ids = [r["change_id"] for r in rv.rows if r["needs_id"]]
        assert all(ids) and len(ids) == len({id(r) for r in rv.rows if r["needs_id"]})
        assert len(set(ids)) == len(ids)
        assert not any(r["change_id"] for r in rv.rows if not r["needs_id"])

    def test_no_reduction_markup_without_complete_list(self, rv):
        for r in rv.rows:
            if r["action"] == "Mark up: reduction":
                u = rv.unit_by_tag.get(r["unit"])
                assert u and u["complete"]

    def test_no_remove_or_add_where_zone_link_unknown(self, rv):
        for u in rv.i.units:
            if u["has_tag"] and u["systems"] and not rv.zone_rooms(u):
                for r in rv.unit_info[u["tag"]]["rows"]:
                    if r["row_type"] == "Room":
                        assert r["cls"] in ("not established", "reviewed on another tab")

    def test_return_fans_are_matched_by_system(self, rv):
        # a system with a drawn return fan never gets "add a return fan"
        for r in rv.rows:
            if r["action"] == "Mark up: add a return fan":
                u = rv.unit_by_tag[r["unit"]]
                assert not any(s["return_air"]["fans"] for s in u["systems"])

    def test_one_drawn_box_one_result(self, rv):
        acts = defaultdict(set)
        for r in rv.rows:
            if r["needs_id"] and r["row_type"] in ("Room", "Room airflow", "Fan") and r["box"] and r["quantity"]:
                acts[(r["sheet"], r["box"], r["item"], r["quantity"])].add(r["action"])
        assert not [k for k, v in acts.items() if len(v) > 1]

    def test_no_markup_on_mot_theatre_units(self, rv):
        for u in rv.i.units:
            if u.get("list_only") and u["tag"] in rv.unit_info:
                assert not any(r["needs_id"] for r in rv.unit_info[u["tag"]]["rows"])

    def test_cath_rooms_have_a_zone_link(self, rv):
        rows = rv.unit_info["AHU-B-04-04CATH-01"]["rows"]
        assert not [r for r in rows if r["row_type"] == "Room" and r["cls"] == "not established"
                    and "zone link" in r["action"] and not r["note"]]

    def test_mot_sheets_are_not_marked_up(self, rv):
        assert rv.mot_rows
        assert not any("IP-BH-160" in (r["sheet"] or "") for r in rv.rows if r["needs_id"])

    def test_text_is_plain(self, rv):
        bad = re.compile(r"\b(script|pipeline|council|gate|workflow|claude|opus|sonnet|python|model|register|agent|method|D-\d{3}|zone_assignment)\b", re.I)
        for r in rv.rows:
            unq = re.sub(r"'[^']*'", "", r["source"])
            assert not bad.search(r["action"] + " " + str(r["note"]) + " " + unq), (r["action"], r["note"], r["source"])
        for r in rv.rows:
            for s in re.split(r"(?<=[.;])\s+", r["action"]):
                assert len(s.split()) <= 25

    def test_csv_and_workbook(self, rv, tmp_path):
        n, m = R.write_csvs(rv, tmp_path)
        with open(tmp_path / "change_register.csv", newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == n == sum(1 for r in rv.rows if r["needs_id"] or r.get("class") == "increase of 5 L/s or less, not marked up" or r.get("cls") == "increase of 5 L/s or less, not marked up")
        for col in ("change_id", "sheet", "page", "bbox", "layer_type", "drawn", "required", "action", "source"):
            assert col in rows[0]
        assert {r["layer_type"] for r in rows} <= set(R.LAYERS)
        assert m > 0
        import openpyxl
        R.write_xlsx(rv, tmp_path / "x.xlsx")
        wb = openpyxl.load_workbook(tmp_path / "x.xlsx")
        assert wb.sheetnames[0] == "Summary"
        assert "App D reconciliation" in wb.sheetnames and "MOT differences" in wb.sheetnames
        ws = wb[wb.sheetnames[8]]
        assert not ws.merged_cells.ranges
        assert int(ws.page_setup.paperSize) == 8 and ws.page_setup.orientation == "landscape"
        assert ws.freeze_panes
        assert ws["A1"].font.name == "Arial"
        n_unit = sum(1 for u in rv.i.units if u["has_tag"])
        assert len(wb.sheetnames) == 7 + n_unit
