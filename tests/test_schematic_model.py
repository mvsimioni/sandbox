"""Schematic model: key rules, box rules and register-to-model completeness."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.schematics import model as m

ROOT = Path(__file__).resolve().parents[1]
HAVE = (ROOT / "out/registers/schematic_rooms.csv").exists()


def test_ahu_key_strips_type_and_duty_letter():
    assert m.ahu_key("AHU-B-02-01EDP3PABAY-01A") == "B-02-01EDP3PABAY-01"
    assert m.ahu_key("RAF-B-02-01EDP3PABAY-01B") == "B-02-01EDP3PABAY-01"
    assert m.ahu_key("AHU-B-B1-B2COR-02") == "B-B1-B2COR-02"
    assert m.ahu_key("OAF/EAF-B-3-03AHU1-01A/B") == "B-3-03AHU1-01"
    assert m.tail2("B-B2-FS-06") == "FS-06"


def test_num_and_box():
    assert m.num("OA 2375 L/s") == 2375.0
    assert m.num("-") is None and m.num(None) is None
    assert m.box("[1, 2, 3.04, 4]") == [1.0, 2.0, 3.0, 4.0]
    assert m.box("") is None and m.box("[1,2]") is None


def test_val_without_box_says_why():
    v = m.val(None, None, None, "S", 1)
    assert v["box"] is None and v["note"]


@pytest.mark.skipif(not HAVE, reason="registers not built")
class TestRegisters:
    @pytest.fixture(scope="class")
    def built(self):
        reg = m.Reg(ROOT)
        return reg, m.build_working(reg, read_notes=False), m.build_appd(ROOT)

    def test_every_working_room_box_is_in_the_model(self, built):
        reg, w, _ = built
        keys = {m._room_key(r) for r in reg.rooms if r["issue_set"] == "working"}
        got = {r["room_id"] for s in w["systems"] for r in s["rooms"]}
        assert len(got) == len(keys)

    def test_every_room_row_has_a_mode_record(self, built):
        reg, w, _ = built
        rows = sum(1 for r in reg.rooms if r["issue_set"] == "working")
        seen = {}
        for s in w["systems"]:
            for r in s["rooms"]:
                seen[r["room_id"]] = len(r["modes"])
        assert sum(seen.values()) == rows

    def test_every_ahu_tag_in_exactly_one_system(self, built):
        reg, w, _ = built
        tags = [e["tag"] for e in reg.equip if e["issue_set"] == "working" and e["type"] == "AHU"]
        got = [a["tag"]["value"] for s in w["systems"] for a in s.get("ahus", [])]
        assert sorted(tags) == sorted(got)

    def test_values_keep_a_box(self, built):
        _, w, _ = built
        for s in w["systems"]:
            for a in s.get("ahus", []):
                assert a["duty_supply_L_s"]["box"] and len(a["duty_supply_L_s"]["box"]) == 4
            for r in s["rooms"]:
                assert r["name"]["box"] is not None
                for md in r["modes"]:
                    for k in ("supply_L_s", "return_L_s", "exhaust_L_s"):
                        v = md[k]
                        if v["value"] is not None:
                            assert v["box"] and v["as_written"]

    def test_room_supply_equals_register_value(self, built):
        reg, w, _ = built
        first = next(r for r in reg.rooms if r["issue_set"] == "working" and r["supply_L_s"])
        found = [md for s in w["systems"] for r in s["rooms"]
                 if r["name"]["value"] == first["room_name_as_written"] and r["name"]["sheet"] == first["sheet"]
                 for md in r["modes"] if md["mode"] == first["mode"]]
        assert any(md["supply_L_s"]["value"] == float(first["supply_L_s"]) for md in found)

    def test_appd_every_equipment_row_once(self, built):
        _, _, a = built
        n = sum(len(s["equipment"]) for s in a["systems"])
        assert n == len(m.read_csv(ROOT / "out/registers/appd_equipment.csv"))

    def test_appd_every_space_listed(self, built):
        _, _, a = built
        refs = {sp["space_ref"] for s in a["systems"] for sp in s["spaces"]}
        assert len(refs) == a["space_count"]

    def test_flat_row_has_all_columns(self, built):
        _, w, a = built
        for s in (w["systems"][0], a["systems"][0]):
            assert set(m.flat_row(s)) == set(m.CSV_COLS)
