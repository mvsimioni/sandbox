"""Adversarial tests for src.pressure written by the independent reviewer.

Plain tests assert behaviour the module must already provide.  Tests marked
``xfail(strict=True)`` pin defects found in review: they fail today for the
reason given in ``reason`` and will turn into an XPASS failure (so the marker
must be removed) once the defect is fixed.  See
``out/qa/verify/code_pressure_review.md`` for the matching issue list.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src import pressure as pm
from src.common import read_csv_dicts
from src.pressure import Link, Space, Suite, cm2_to_m2

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SMALL = pm.ROOM_SMALL
LARGE = pm.ROOM_LARGE


# --------------------------------------------------------------------------- #
# Behaviour that must hold today
# --------------------------------------------------------------------------- #
def test_empty_suite_solves_and_cli_handles_it(tmp_path: Path) -> None:
    """A suite with no spaces is a degenerate but legal input: one total row, no crash."""
    result = Suite("empty", []).solve()
    assert result.spaces == [] and result.status == pm.STATUS_FREE and result.ratio is None
    rows = result.to_rows()
    assert len(rows) == 1 and rows[0]["space"] == "SUITE TOTAL" and tuple(rows[0]) == pm.ROW_FIELDS
    src = tmp_path / "empty.json"
    src.write_text(json.dumps({"name": "empty", "spaces": [], "links": []}), encoding="utf-8")
    out = tmp_path / "empty.csv"
    assert pm.main(["--suite", str(src), "--out", str(out)]) == 0
    assert [r["space"] for r in read_csv_dicts(out)] == ["SUITE TOTAL"]


def test_unicode_and_spaces_in_names_and_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Space names with accents, spaces and symbols survive solve, JSON output and a CSV in an odd path."""
    name_room = "Isolation  room 30 – Pharmacy (N°1)"
    name_ens = "Ensuite ✓ \"wet\" room"
    data = {
        "name": "suite ünïcode",
        "externals": {"Corridor 1": 0.0},
        "spaces": [
            {"name": name_room, "set_pressure_pa": -30, "surface_m2": LARGE},
            {"name": name_ens, "set_pressure_pa": None, "surface_m2": 37.5, "exhaust_m3h": 150},
        ],
        "links": [
            {"a": "Corridor 1", "b": name_room, "gap_area_cm2": 20},
            {"a": name_room, "b": name_ens, "gap_area_cm2": 100, "kind": "louvre ∅"},
        ],
    }
    folder = tmp_path / "département 30 – Pharmacy" / "sub folder"
    folder.mkdir(parents=True)
    src = folder / "suite ünïcode.json"
    src.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    out = folder / "rows with spaces.csv"
    assert pm.main(["--suite", str(src), "--out", str(out)]) == 0
    rows = read_csv_dicts(out)
    assert [r["space"] for r in rows] == [name_room, name_ens, "SUITE TOTAL"]
    assert rows[0]["suite"] == "suite ünïcode" and rows[1]["status"] == "FREE"
    capsys.readouterr()  # drop the 'wrote N rows' line before parsing the JSON run
    assert pm.main(["--suite", str(src), "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed[1]["space"] == name_ens


def test_two_free_spaces_fixed_iteration_solve_is_symmetric() -> None:
    """Two identical free spaces hung off one room must settle at the same pressure with the fixed solver."""
    suite = Suite(
        "twins",
        [Space("ROOM", -30.0, LARGE), Space("WC-A", None, 15.0, exhaust_m3h=60.0),
         Space("WC-B", None, 15.0, exhaust_m3h=60.0)],
        [Link("ROOM", "WC-A", gap_area_m2=cm2_to_m2(100)), Link("ROOM", "WC-B", gap_area_m2=cm2_to_m2(100))],
    )
    fixed = suite.solve()
    assert fixed.sweeps == pm.SWEEPS
    assert fixed.pressures["WC-A"] == pytest.approx(fixed.pressures["WC-B"], abs=1e-9)
    assert fixed.pressures["WC-A"] < -30.0
    assert fixed.max_residual < 1e-6
    for name in ("WC-A", "WC-B"):
        res = fixed.space(name)
        assert res.status == "FREE" and res.ratio is None and res.offset_needed == 0.0
        assert res.ratio_n050 is None and res.uncontrolled_pa is None
    exact = suite.solve_exact()
    assert exact.pressures["WC-A"] == pytest.approx(fixed.pressures["WC-A"], abs=1e-6)
    # The ROOM must see both louvres as outward flow.
    room = fixed.space("ROOM")
    assert room.gap_out == pytest.approx(fixed.space("WC-A").gap_in + fixed.space("WC-B").gap_in, abs=1e-9)


def test_two_free_spaces_in_series_order_independent() -> None:
    """Gauss-Seidel order must not change the answer: declare the chain in either order."""
    def build(order: str) -> Suite:
        spaces = [Space("LOBBY", None, 20.0, exhaust_m3h=100.0), Space("WC", None, 15.0, exhaust_m3h=80.0)]
        if order == "reversed":
            spaces.reverse()
        return Suite("chain", [Space("ROOM", -30.0, LARGE), *spaces],
                     [Link("ROOM", "LOBBY", gap_area_m2=cm2_to_m2(200)), Link("LOBBY", "WC", gap_area_m2=cm2_to_m2(200))])

    a, b = build("forward").solve(), build("reversed").solve()
    assert a.pressures["WC"] == pytest.approx(b.pressures["WC"], abs=1e-6)
    assert a.pressures["LOBBY"] == pytest.approx(b.pressures["LOBBY"], abs=1e-6)
    assert a.offset_needed == pytest.approx(b.offset_needed, abs=1e-6)


def test_free_space_is_resolved_in_every_sensitivity_case() -> None:
    """Doubling the gaps or changing n moves a free ensuite; the room's sensitivity ratios must use the moved pressure."""
    suite = Suite(
        "free ensuite",
        [Space("ROOM", -30.0, LARGE), Space("ENSUITE", None, 37.5, exhaust_m3h=150.0)],
        [Link("CORRIDOR", "ROOM", gap_area_m2=cm2_to_m2(20)), Link("ROOM", "ENSUITE", gap_area_m2=cm2_to_m2(100))],
        externals={"CORRIDOR": 0.0},
    )
    base = suite.solve()
    room = base.space("ROOM")
    for attr in ("ratio_n050", "ratio_n075", "ratio_gaps_x2"):
        value = getattr(room, attr)
        assert value is not None and math.isfinite(value) and value > 0
    doubled, _ = suite.solve_pressures(None, 2.0)
    assert doubled["ENSUITE"] != pytest.approx(base.pressures["ENSUITE"], abs=1e-3)
    # The doubled-gap ratio of the room must match a direct evaluation at the re-solved ensuite pressure.
    direct = suite._evaluate(doubled, None, 2.0)[0].ratio  # noqa: SLF001 - cross-check of the solver
    assert room.ratio_gaps_x2 == pytest.approx(direct, abs=1e-9)


def test_malformed_inputs_exit_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Broken JSON, wrong types and a missing required field all surface as exit 1 with an error line."""
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not json", encoding="utf-8")
    assert pm.main(["--suite", str(bad_json)]) == 1
    assert "error:" in capsys.readouterr().err
    typed = tmp_path / "typed.json"
    typed.write_text(json.dumps({"spaces": [{"name": "X", "set_pressure_pa": "-15", "surface_m2": "40"}]}), encoding="utf-8")
    assert pm.main(["--suite", str(typed)]) == 1
    missing = tmp_path / "missing.json"
    missing.write_text(json.dumps({"spaces": [{"name": "X", "surface_m2": 40}]}), encoding="utf-8")
    assert pm.main(["--suite", str(missing)]) == 1
    both = tmp_path / "both.json"
    both.write_text(json.dumps({"spaces": [{"name": "X", "set_pressure_pa": -15, "surface_m2": 40}],
                                "links": [{"a": "X", "b": "REF", "gap_area_cm2": 10, "fixed_flow_m3h": 5}]}), encoding="utf-8")
    assert pm.main(["--suite", str(both)]) == 1
    assert "error:" in capsys.readouterr().err


def test_inert_links_do_not_change_the_balance() -> None:
    """A zero-area gap and a zero fixed flow are legal and contribute nothing."""
    base = Suite("base", [Space("R", -15.0, SMALL)]).solve(sensitivity=False).space("R")
    inert = Suite("inert", [Space("R", -15.0, SMALL)],
                  [Link("R", "REF", gap_area_m2=0.0), Link("REF", "R", fixed_flow_m3h=0.0)]).solve(sensitivity=False).space("R")
    assert inert.air_in == pytest.approx(base.air_in) and inert.air_out == pytest.approx(base.air_out)
    assert inert.gap_in == 0.0 and inert.fixed_in == 0.0 and inert.fixed_out == 0.0


def test_solve_is_pure_and_output_is_atomic(tmp_path: Path) -> None:
    """Solving twice gives identical rows, from_dict leaves its input alone, and no temp file is left behind."""
    data = {"spaces": [{"name": "R", "set_pressure_pa": -15, "floor_area": 5.5272, "perimeter": 9.64, "height": 3.0}],
            "links": [{"a": "CORRIDOR", "b": "R", "sealed": "hinged"}], "externals": {"CORRIDOR": 0}}
    snapshot = json.dumps(data, sort_keys=True)
    suite = Suite.from_dict(data)
    assert json.dumps(data, sort_keys=True) == snapshot, "from_dict must not mutate its input"
    assert suite.solve().to_rows() == suite.solve().to_rows()
    src = tmp_path / "s.json"
    src.write_text(json.dumps(data), encoding="utf-8")
    out = tmp_path / "nested" / "rows.csv"
    assert pm.main(["--suite", str(src), "--out", str(out)]) == 0
    assert pm.main(["--suite", str(src), "--out", str(out)]) == 0
    assert sorted(p.name for p in out.parent.iterdir()) == ["rows.csv"], "atomic write must leave no temp files"


def test_free_space_next_to_an_extreme_external_still_brackets() -> None:
    """The bracket follows the neighbours: a free space beside a -900 Pa node still converges."""
    suite = Suite("deep", [Space("F", None, 10.0, exhaust_m3h=20.0)], [Link("PIT", "F", gap_area_m2=cm2_to_m2(50))],
                  externals={"PIT": -900.0})
    result = suite.solve(sensitivity=False)
    assert abs(result.space("F").balance_residual) < 1e-6
    assert -900.0 < result.pressures["F"] < 0.0


def test_selftest_prints_every_known_answer(capsys: pytest.CaptureFixture[str]) -> None:
    """The table carries one line per known answer and all five spec groups."""
    assert pm.run_selftest() == 0
    out = capsys.readouterr().out
    for marker in ("1 limit conversion", "2 small room", "3 gaps", "4 anteroom", "5 suite"):
        assert marker in out
    assert out.count(" ok") == 21 and "MISS" not in out


# --------------------------------------------------------------------------- #
# Defects pinned for the author (strict xfail: remove the marker once fixed)
# --------------------------------------------------------------------------- #
@pytest.mark.xfail(strict=True, reason="R-01: a negative room with net outward leakage gets ratio inf / PASS and "
                                       "'no offset needed' although it needs a supply offset")
def test_negative_room_with_net_outflow_is_not_a_silent_pass() -> None:
    res = Suite("ante by deep room", [Space("A", -15.0, SMALL)], [Link("A", "DEEP", gap_area_m2=cm2_to_m2(200))],
                externals={"DEEP": -60.0}).solve().space("A")
    assert res.air_out > res.air_in
    # The room cannot be held at -15 Pa by exhaust at all; the method must say so, not PASS.
    assert res.status != "PASS"
    assert not any("no offset needed" in n for n in res.notes)


@pytest.mark.xfail(strict=True, reason="R-02: a space named REF or named like an external silently overrides that node's pressure")
def test_space_name_colliding_with_ref_or_external_is_rejected() -> None:
    with pytest.raises(ValueError):
        Suite("ref clash", [Space("REF", -15.0, SMALL)])
    with pytest.raises(ValueError):
        Suite("ext clash", [Space("CORRIDOR", -15.0, SMALL)], externals={"CORRIDOR": 0.0})


@pytest.mark.xfail(strict=True, reason="R-03: a free space whose balance never closes inside the bracket keeps its "
                                       "initial guess with no note on its own row")
def test_non_converged_free_space_is_noted_on_its_row() -> None:
    result = Suite("runaway", [Space("ROOM", -30.0, SMALL), Space("ENS", None, 1.0, exhaust_m3h=2000.0)],
                   [Link("ROOM", "ENS", gap_area_m2=cm2_to_m2(1))]).solve(sensitivity=False)
    ens = result.space("ENS")
    assert abs(ens.balance_residual) > 1000.0  # the solver really did not close
    assert ens.notes, "the space row must say the balance did not close"


@pytest.mark.xfail(strict=True, reason="R-04: unknown keys in a JSON space or link (typos such as 'exhaust') are dropped silently")
def test_unknown_json_keys_are_rejected() -> None:
    with pytest.raises((ValueError, TypeError, KeyError)):
        Suite.from_dict({"spaces": [{"name": "X", "set_pressure_pa": -15, "surface_m2": 40, "exhaust": 100}]})
    with pytest.raises((ValueError, TypeError, KeyError)):
        Suite.from_dict({"spaces": [{"name": "X", "set_pressure_pa": -15, "surface_m2": 40}],
                         "links": [{"a": "X", "b": "REF", "gap_area_m2": 0.001, "fixed_flow": 5}]})


@pytest.mark.xfail(strict=True, reason="R-05: a suite mixing a positive and a negative room nets the two offsets to 0 "
                                       "and reports ratio inf / PASS on the SUITE TOTAL row")
def test_mixed_sign_suite_total_is_not_inf_pass() -> None:
    result = Suite("mixed", [Space("POS", 15.0, SMALL), Space("NEG", -15.0, SMALL)]).solve()
    assert result.ratio is None or math.isfinite(result.ratio)
    assert result.status != "PASS"


@pytest.mark.xfail(strict=True, reason="R-06: envelope leakage with leaks_to set to another space is not credited to that "
                                       "space, so mass is not conserved and the receiving room's offset is understated")
def test_envelope_leaking_to_a_space_is_credited_to_that_space() -> None:
    result = Suite("nested", [Space("A", -15.0, SMALL, leaks_to="B"), Space("B", -30.0, SMALL)]).solve(sensitivity=False)
    a, b = result.space("A"), result.space("B")
    assert a.envelope_out == pytest.approx(pm.envelope_leak(SMALL, 15), abs=0.2)
    assert b.air_in == pytest.approx(pm.envelope_leak(SMALL, 30) + a.envelope_out, abs=0.2)


@pytest.mark.xfail(strict=True, reason="R-07: the CLI exit code follows the SUITE TOTAL status only, so a FAIL on one "
                                       "space behind a passing total exits 0")
def test_cli_exit_nonzero_when_any_space_fails(tmp_path: Path) -> None:
    data = {
        "externals": {"CORRIDOR": 0.0, "ANTE": -15.0},
        "spaces": [{"name": "ROOM", "set_pressure_pa": -30, "surface_m2": LARGE},
                   {"name": "ENSUITE", "set_pressure_pa": -31, "surface_m2": 37.5}],
        "links": [{"a": "CORRIDOR", "b": "ROOM", "gap_area_cm2": 20}, {"a": "ANTE", "b": "ROOM", "gap_area_cm2": 10},
                  {"a": "ROOM", "b": "ENSUITE", "gap_area_cm2": 200}],
    }
    result = Suite.from_dict(data).solve()
    assert result.space("ENSUITE").status == "FAIL" and result.status != "FAIL"  # the condition under test
    src = tmp_path / "hidden.json"
    src.write_text(json.dumps(data), encoding="utf-8")
    assert pm.main(["--suite", str(src), "--json"]) != 0


@pytest.mark.xfail(strict=True, reason="R-08: a controlled room with zero surface and no leakage paths is reported FREE")
def test_zero_surface_controlled_room_is_not_free() -> None:
    res = Suite("zero", [Space("Z", -15.0, 0.0)]).solve().space("Z")
    assert res.status != "FREE"


@pytest.mark.xfail(strict=True, reason="R-09: a negative gap area is accepted and silently treated as no gap")
def test_negative_gap_area_rejected() -> None:
    with pytest.raises(ValueError):
        Suite("neg gap", [Space("A", -15.0, SMALL)], [Link("A", "REF", gap_area_m2=-0.01)])


@pytest.mark.xfail(strict=True, reason="R-10: a config whose pressure_method block is not a mapping is silently replaced "
                                       "by the built-in defaults (the broad except in _config_section)")
def test_wrong_shaped_config_block_is_not_silently_ignored(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    # Valid YAML (so src.common imports), but the pressure block is a scalar, not the expected mapping.
    (root / "config" / "project.yaml").write_text("paths: {}\npressure_method: 7\n", encoding="utf-8")
    env = {**os.environ, "AIGH_ROOT": str(root), "PYTHONPATH": str(PROJECT_ROOT)}
    proc = subprocess.run([sys.executable, "-c", "import src.pressure as p; print(p.AIRTIGHTNESS_LIMIT)"],
                          cwd=str(tmp_path), env=env, capture_output=True, text=True, check=False)
    assert proc.returncode != 0 or "pressure_method" in proc.stderr
