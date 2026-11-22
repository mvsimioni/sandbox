"""Tests for src.pressure: known answers, sign conventions, free spaces, sensitivity and the CLI."""

from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

import pytest

from src import pressure as pm
from src.common import read_csv_dicts
from src.pressure import (
    Link,
    Space,
    Suite,
    cm2_to_m2,
    envelope_leak,
    gap_flow,
    internal_surface,
    limit_at,
    lps,
    tighter_room_offset,
    unsealed_door_gap_area,
)

FIXTURES = Path(__file__).parent / "fixtures" / "pressure"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOL_M3H = 0.2
TOL_RATIO = 0.01

SMALL = internal_surface(1.88 * 2.94, 2 * (1.88 + 2.94), 3.0)
LARGE = internal_surface(3.97 * 4.83, 2 * (3.97 + 4.83), 3.0)


# --------------------------------------------------------------------------- #
# Known answers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("pa,expected", [(5, 0.560), (15, 1.143), (25, 1.593), (50, 2.500), (70, 3.111)])
def test_limit_conversion(pa: float, expected: float) -> None:
    assert limit_at(pa) == pytest.approx(expected, abs=0.001)


def test_small_room_surface_and_envelope() -> None:
    assert SMALL == pytest.approx(40.0, abs=0.05)
    assert envelope_leak(SMALL, 50) == pytest.approx(99.9, abs=TOL_M3H)
    assert envelope_leak(SMALL, 15) == pytest.approx(45.7, abs=TOL_M3H)


@pytest.mark.parametrize("cm2,pa,expected", [(15, 15, 16.2), (10, 15, 10.8), (20, 30, 30.5)])
def test_gap_flows(cm2: float, pa: float, expected: float) -> None:
    assert gap_flow(cm2_to_m2(cm2), pa) == pytest.approx(expected, abs=TOL_M3H)


def test_anteroom_case_4() -> None:
    res = pm.known_answer_anteroom().solve().space("ANTE")
    assert res.envelope_in == pytest.approx(45.7, abs=TOL_M3H)
    assert res.gap_in == pytest.approx(16.2, abs=TOL_M3H)
    assert res.fixed_in == pytest.approx(10.2, abs=1e-9)
    assert res.gap_out == pytest.approx(10.8, abs=TOL_M3H)
    assert res.offset_kind == "exhaust"
    assert res.offset_needed == pytest.approx(61.3, abs=TOL_M3H)
    assert res.design_offset == pytest.approx(99.9, abs=TOL_M3H)
    assert res.ratio == pytest.approx(1.63, abs=TOL_RATIO)
    assert res.status == "PASS"


def test_suite_case_5() -> None:
    result = pm.known_answer_suite().solve()
    assert result.space("ROOM").envelope_in == pytest.approx(163.5, abs=TOL_M3H)
    assert result.space("ENSUITE").envelope_in == pytest.approx(68.7, abs=TOL_M3H)
    assert result.offset_needed == pytest.approx(273.5, abs=TOL_M3H)
    assert result.design_offset == pytest.approx(321.6, abs=TOL_M3H)
    assert result.ratio == pytest.approx(1.18, abs=TOL_RATIO)
    assert result.status == "VERIFY"
    # The internal door between room and ensuite cancels in the suite total.
    room, ensuite = result.space("ROOM"), result.space("ENSUITE")
    assert room.gap_out == pytest.approx(ensuite.gap_in, abs=1e-9)
    assert room.offset_needed + ensuite.offset_needed == pytest.approx(result.offset_needed, abs=1e-9)


def test_suite_total_independent_of_internal_gap() -> None:
    base = pm.known_answer_suite().solve()
    wide = Suite(
        "wide door",
        [Space("ROOM", -30.0, LARGE), Space("ENSUITE", -31.0, 37.5)],
        [
            Link("CORRIDOR", "ROOM", gap_area_m2=cm2_to_m2(20)),
            Link("ANTE", "ROOM", gap_area_m2=cm2_to_m2(10)),
            Link("ROOM", "ENSUITE", gap_area_m2=cm2_to_m2(200)),
        ],
        externals={"CORRIDOR": 0.0, "ANTE": -15.0},
    ).solve()
    assert wide.offset_needed == pytest.approx(base.offset_needed, abs=1e-9)
    assert wide.space("ROOM").offset_needed < base.space("ROOM").offset_needed


# --------------------------------------------------------------------------- #
# Elementary helpers and sign conventions
# --------------------------------------------------------------------------- #
def test_units_and_door_gap_helpers() -> None:
    assert lps(3600.0) == pytest.approx(1000.0)
    assert pm.m3h(lps(123.4)) == pytest.approx(123.4)
    assert unsealed_door_gap_area(0.9, 2.1) * 1e4 == pytest.approx(240.0, abs=5.0)
    assert unsealed_door_gap_area(0.6, 1.8) * 1e4 == pytest.approx(190.0, abs=5.0)
    assert pm.SEALED_GAP_CM2["hinged_sealed"] == 15
    assert pm.SEALED_GAP_CM2["sliding_sealed"] == 10
    assert pm.SEALED_GAP_CM2["bed_width_sealed"] == 20
    assert pm.SEALED_GAP_CM2["shaft_access_sealed"] == 10
    assert pm.sealed_gap_area("hinged") == pytest.approx(0.0015)
    assert pm.sealed_gap_area("shaft_access_sealed") == pytest.approx(0.0010)
    with pytest.raises(KeyError):
        pm.sealed_gap_area("revolving")
    assert tighter_room_offset(SMALL) == pytest.approx(1.0 * SMALL)
    assert pm.design_offset(SMALL) == pytest.approx(2.5 * SMALL)


def test_magnitudes_and_zero_pressure() -> None:
    assert envelope_leak(40.0, -15) == pytest.approx(envelope_leak(40.0, 15))
    assert envelope_leak(40.0, 0) == 0.0
    assert gap_flow(0.0015, -15) == pytest.approx(gap_flow(0.0015, 15))
    assert gap_flow(0.0015, 0) == 0.0
    assert gap_flow(0.0, 15) == 0.0


def test_air_flows_from_higher_to_lower_pressure() -> None:
    suite = Suite("two rooms", [Space("HI", 10.0, 10.0), Space("LO", -10.0, 10.0)],
                  [Link("HI", "LO", gap_area_m2=0.001)])
    result = suite.solve(sensitivity=False)
    hi, lo = result.space("HI"), result.space("LO")
    assert hi.gap_out > 0 and hi.gap_in == 0
    assert lo.gap_in == pytest.approx(hi.gap_out)
    assert hi.envelope_out > 0 and hi.envelope_in == 0
    assert lo.envelope_in > 0 and lo.envelope_out == 0
    assert hi.offset_kind == "supply" and lo.offset_kind == "exhaust"
    assert hi.signed_offset < 0 < lo.signed_offset


def test_fixed_flow_direction_follows_sign() -> None:
    forward = Suite("fwd", [Space("R", -15.0, 40.0)], [Link("SHAFT", "R", fixed_flow_m3h=10.2)],
                    externals={"SHAFT": 0.0}).solve(sensitivity=False).space("R")
    backward = Suite("bwd", [Space("R", -15.0, 40.0)], [Link("R", "SHAFT", fixed_flow_m3h=-10.2)],
                     externals={"SHAFT": 0.0}).solve(sensitivity=False).space("R")
    assert forward.fixed_in == pytest.approx(10.2) and forward.fixed_out == 0
    assert backward.fixed_in == pytest.approx(10.2) and backward.fixed_out == 0
    outward = Suite("out", [Space("R", -15.0, 40.0)], [Link("R", "SHAFT", fixed_flow_m3h=10.2)],
                    externals={"SHAFT": 0.0}).solve(sensitivity=False).space("R")
    assert outward.fixed_out == pytest.approx(10.2) and outward.fixed_in == 0


def test_positive_room_offset_is_extra_supply() -> None:
    suite = Suite("positive", [Space("CLEAN", 15.0, SMALL)],
                  [Link("CLEAN", "CORRIDOR", gap_area_m2=cm2_to_m2(15))], externals={"CORRIDOR": 0.0})
    res = suite.solve().space("CLEAN")
    assert res.offset_kind == "supply"
    assert res.envelope_out == pytest.approx(45.7, abs=TOL_M3H)
    assert res.gap_out == pytest.approx(16.2, abs=TOL_M3H)
    assert res.air_in == 0.0
    assert res.offset_needed == pytest.approx(45.7 + 16.2, abs=2 * TOL_M3H)
    assert res.ratio == pytest.approx(99.9 / 61.9, abs=TOL_RATIO)
    assert res.status == "PASS"
    # With no control the design offset pushes the room further positive than its set point.
    assert res.uncontrolled_pa is not None and res.uncontrolled_pa > 15.0


def test_status_bands() -> None:
    assert pm.classify_ratio(0.99) == "FAIL"
    assert pm.classify_ratio(1.0) == "VERIFY"
    assert pm.classify_ratio(1.19) == "VERIFY"
    assert pm.classify_ratio(1.2) == "PASS"
    assert pm.classify_ratio(None) == "FREE"


def test_validation_errors() -> None:
    with pytest.raises(ValueError):
        Link("A", "B")
    with pytest.raises(ValueError):
        Link("A", "B", gap_area_m2=0.001, fixed_flow_m3h=5.0)
    with pytest.raises(ValueError):
        Suite("bad", [Space("R", -15.0, 40.0)], [Link("R", "NOWHERE", gap_area_m2=0.001)])
    with pytest.raises(ValueError):
        Suite("bad", [Space("R", -15.0, 40.0, leaks_to="PLENUM")])
    with pytest.raises(ValueError):
        Suite("bad", [Space("R", -15.0, 40.0), Space("R", -20.0, 40.0)])


# --------------------------------------------------------------------------- #
# Free-pressure spaces
# --------------------------------------------------------------------------- #
def _free_ensuite_suite(exhaust: float = 150.0) -> Suite:
    """A -30 Pa room with an ensuite that has no set pressure, a door louvre and its own exhaust."""
    return Suite(
        "free ensuite",
        [Space("ROOM", -30.0, LARGE), Space("ENSUITE", None, 37.5, exhaust_m3h=exhaust)],
        [Link("CORRIDOR", "ROOM", gap_area_m2=cm2_to_m2(20)), Link("ROOM", "ENSUITE", gap_area_m2=cm2_to_m2(100))],
        externals={"CORRIDOR": 0.0},
    )


def test_free_ensuite_converges() -> None:
    result = _free_ensuite_suite().solve()
    ens = result.space("ENSUITE")
    assert ens.status == "FREE" and ens.offset_kind == "free" and ens.ratio is None
    assert ens.pressure_pa < -30.0, "an exhausted ensuite sits below the room it draws from"
    assert abs(ens.balance_residual) < 1e-6
    assert ens.envelope_in + ens.gap_in == pytest.approx(150.0, abs=1e-6)
    assert result.max_residual < 1e-6
    assert result.sweeps == pm.SWEEPS
    room = result.space("ROOM")
    assert room.gap_out == pytest.approx(ens.gap_in)
    # Only the controlled room counts towards the suite offset.
    assert result.offset_needed == pytest.approx(room.offset_needed)
    assert result.design_offset == pytest.approx(room.design_offset)


def test_solve_exact_agrees_with_fixed_iterations() -> None:
    fixed = _free_ensuite_suite().solve()
    exact = _free_ensuite_suite().solve_exact()
    assert exact.pressures["ENSUITE"] == pytest.approx(fixed.pressures["ENSUITE"], abs=1e-6)
    assert exact.space("ENSUITE").balance_residual == pytest.approx(0.0, abs=1e-6)
    assert exact.sweeps < pm.SWEEPS


def test_free_space_with_more_exhaust_sits_lower() -> None:
    low = _free_ensuite_suite(100.0).solve(sensitivity=False).pressures["ENSUITE"]
    high = _free_ensuite_suite(200.0).solve(sensitivity=False).pressures["ENSUITE"]
    assert high < low < -30.0


def test_free_space_with_weak_exhaust_sits_above_the_room() -> None:
    # The envelope leaks to REF (the cautious side): at -30 Pa it alone supplies about 67 m3/h to a
    # 37.5 m2 ensuite, so a 60 m3/h exhaust cannot hold it below the room and air leaves it to the room.
    result = _free_ensuite_suite(60.0).solve(sensitivity=False)
    ens = result.space("ENSUITE")
    assert -30.0 < ens.pressure_pa < 0.0
    assert ens.gap_out > 0 and ens.gap_in == 0
    assert abs(ens.balance_residual) < 1e-6


def test_two_coupled_free_spaces() -> None:
    suite = Suite(
        "coupled",
        [Space("ROOM", -30.0, LARGE), Space("LOBBY", None, 20.0, exhaust_m3h=100.0),
         Space("WC", None, 15.0, exhaust_m3h=80.0)],
        [Link("ROOM", "LOBBY", gap_area_m2=cm2_to_m2(200)), Link("LOBBY", "WC", gap_area_m2=cm2_to_m2(200))],
    )
    result = suite.solve_exact()
    assert result.pressures["WC"] < result.pressures["LOBBY"] < -30.0
    assert result.max_residual < 1e-6


# --------------------------------------------------------------------------- #
# Sensitivity
# --------------------------------------------------------------------------- #
def test_sensitivity_directions_below_50_pa() -> None:
    res = pm.known_answer_anteroom().solve().space("ANTE")
    assert res.ratio is not None
    # Below 50 Pa a smaller exponent means more envelope leakage, so a lower ratio; and vice versa.
    assert res.ratio_n050 < res.ratio < res.ratio_n075
    assert res.ratio_gaps_x2 < res.ratio
    assert res.flagged is False and res.status == "PASS"
    # With no control the design offset (larger than needed) pulls the room below its set point.
    assert res.uncontrolled_pa is not None and res.uncontrolled_pa < -15.0


def test_sensitivity_exponents_match_direct_calculation() -> None:
    res = pm.known_answer_anteroom().solve().space("ANTE")
    for attr, n in (("ratio_n050", 0.50), ("ratio_n075", 0.75)):
        needed = envelope_leak(SMALL, 15, n=n) + 16.2 + 10.2 - 10.8
        assert getattr(res, attr) == pytest.approx(99.9 / needed, abs=0.02)
    doubled = 45.7 + 2 * 16.2 + 10.2 - 2 * 10.8
    assert res.ratio_gaps_x2 == pytest.approx(99.9 / doubled, abs=0.02)


def test_uncontrolled_pressure_balances_design_offset() -> None:
    suite = pm.known_answer_anteroom()
    res = suite.solve().space("ANTE")
    pressures = dict(suite.solve().pressures)
    pressures["ANTE"] = res.uncontrolled_pa
    at_design = suite._evaluate(pressures, None, 1.0)[0]  # noqa: SLF001 - checking the solved root
    assert at_design.signed_offset == pytest.approx(res.design_offset, abs=1e-6)


def test_room_failing_only_in_sensitivity_is_flagged_not_failed() -> None:
    suite = Suite("leaky door", [Space("ANTE", -15.0, SMALL)],
                  [Link("CORRIDOR", "ANTE", gap_area_m2=cm2_to_m2(40))], externals={"CORRIDOR": 0.0})
    result = suite.solve()
    res = result.space("ANTE")
    assert res.status == "VERIFY"
    assert res.ratio_gaps_x2 < 1.0
    assert res.flagged is True and result.flagged is True
    assert any("gaps doubled" in note for note in res.notes)


def test_outright_failure_is_not_flagged() -> None:
    res = Suite("too leaky", [Space("ANTE", -15.0, SMALL)],
                [Link("CORRIDOR", "ANTE", gap_area_m2=cm2_to_m2(80))], externals={"CORRIDOR": 0.0}).solve().space("ANTE")
    assert res.status == "FAIL" and res.flagged is False


def test_low_set_pressure_is_noted() -> None:
    res = Suite("weak", [Space("R", -3.0, 40.0)]).solve().space("R")
    assert any("not applicable" in note for note in res.notes)


# --------------------------------------------------------------------------- #
# Rows, JSON loading and the command line
# --------------------------------------------------------------------------- #
def test_to_rows_shape() -> None:
    result = pm.known_answer_suite().solve()
    rows = result.to_rows()
    assert len(rows) == 3 and rows[-1]["space"] == "SUITE TOTAL"
    for row in rows:
        assert tuple(row) == pm.ROW_FIELDS
    assert rows[-1]["offset_needed_m3h"] == pytest.approx(273.5, abs=TOL_M3H)
    assert rows[-1]["offset_needed_lps"] == pytest.approx(lps(273.5), abs=0.1)
    assert rows[0]["status"] in ("PASS", "VERIFY", "FAIL") and rows[0]["flagged"] in ("y", "n")
    assert pm.to_rows(result) == rows


def test_suite_from_dict_and_cli(tmp_path: Path) -> None:
    import json

    data = json.loads((FIXTURES / "suite_small.json").read_text())
    suite = Suite.from_dict(data)
    assert suite.spaces[0].surface_m2 == pytest.approx(SMALL, abs=0.01)
    assert suite.links[0].gap_area_m2 == pytest.approx(0.0015)
    assert suite.links[1].gap_area_m2 == pytest.approx(0.005)
    out = tmp_path / "rows.csv"
    assert pm.main(["--suite", str(FIXTURES / "suite_small.json"), "--out", str(out)]) == 0
    rows = read_csv_dicts(out)
    assert list(rows[0]) == list(pm.ROW_FIELDS)
    assert [r["space"] for r in rows] == ["ROOM", "ENSUITE", "SUITE TOTAL"]
    assert rows[1]["status"] == "FREE"
    first = out.read_text()
    assert pm.main(["--suite", str(FIXTURES / "suite_small.json"), "--out", str(out)]) == 0
    assert out.read_text() == first, "re-running is idempotent"
    assert pm.main(["--suite", str(FIXTURES / "suite_small.json"), "--exact", "--json"]) == 0


def test_cli_selftest_and_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert pm.main(["--selftest"]) == 0
    assert "known-answer checks pass" in capsys.readouterr().out
    assert pm.main(["--suite", str(tmp_path / "missing.json")]) == 1
    assert pm.main([]) == 2


def test_module_selftest_subprocess() -> None:
    proc = subprocess.run([sys.executable, "-m", "src.pressure", "--selftest"], cwd=str(PROJECT_ROOT),
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "21 of 21" in proc.stdout


def test_selftest_detects_a_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    original = pm.known_answer_cases

    def tampered():  # type: ignore[no-untyped-def]
        cases = original()
        group, label, computed, expected, tol = cases[0]
        cases[0] = (group, label, computed + 1.0, expected, tol)
        return cases

    monkeypatch.setattr(pm, "known_answer_cases", tampered)
    assert pm.run_selftest(out=open("/dev/null", "w")) == 1
    assert pm.main(["--selftest"]) == 1


def test_infinite_ratio_when_nothing_needed() -> None:
    # A room at the reference pressure with no links needs no offset at all.
    res = Suite("neutral", [Space("R", 0.0, 40.0)]).solve().space("R")
    assert res.offset_needed == 0.0 and res.ratio is not None and math.isinf(res.ratio)
    assert res.status == "PASS"
