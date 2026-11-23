"""Checks of the room-to-unit register (D-051): one row per room, a basis for every unit, a reason for every room with no unit."""
import csv
from pathlib import Path

REG = Path(__file__).resolve().parent.parent / "out/registers"


def _rows():
    with (REG / "room_unit_links.csv").open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_one_row_per_room():
    rows = _rows()
    keys = [(r["dept"], r["dpa_room_id"]) for r in rows]
    assert len(keys) == len(set(keys))


def test_every_unit_has_basis_source_and_code():
    for r in _rows():
        if r["unit_class"] != "no information" and r["unit_kind"] != "class":
            assert r["basis"] and r["source_ref"] and r["code"] in ("D", "A", "S", "M", "C"), r


def test_every_room_without_unit_has_reason():
    for r in _rows():
        if r["unit_class"] == "no information":
            assert r["note"], r


def test_steps_4_and_5_are_assumptions():
    for r in _rows():
        if r["step"] in ("4", "5"):
            assert r["code"] == "A", r
