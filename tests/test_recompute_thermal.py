"""Tests for src.recompute.thermal: Rev A totals, airflow rule, OA methods and the pressure known answers."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src import pressure as pm
from src.recompute import thermal as th

ROOT = Path(__file__).resolve().parents[1]
REVA = ROOT / "tests" / "fixtures" / "recompute" / "reva_pharmacy.json"
TOL = 0.005  # 0.5 %


@pytest.fixture(scope="module")
def reva_data():
    return json.loads(REVA.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def reva(reva_data):
    return th.recompute(reva_data)


def close(value, expected, tol=TOL):
    return abs(value - expected) <= tol * abs(expected)


# ---- Rev A totals --------------------------------------------------------------------------- #
@pytest.mark.parametrize("key,expected", [
    ("area_m2", 451.5), ("room_supply_lps", 2760.0), ("room_sensible_kw", 32.5), ("room_latent_kw", 4.6),
])
def test_reva_room_totals(reva, key, expected):
    value = reva["totals"][key]
    assert round(value, 1) == expected          # the brief's figures are rounded to its stated precision
    assert close(value, expected, 0.01)          # latent 4.6418 is 0.9 % from 4.6: rounding, not error


@pytest.mark.parametrize("key,exact", [("area_m2", 451.5), ("room_supply_lps", 2760.0),
                                       ("room_sensible_kw", 32.469965), ("room_latent_kw", 4.641835)])
def test_reva_room_totals_exact(reva, key, exact):
    assert reva["totals"][key] == pytest.approx(exact, rel=1e-9)   # the workbook's cached values


def test_reva_ahu_oa_and_coil(reva):
    ahu = reva["ahus"][0]
    assert close(ahu["oa_lps"], 780.0)
    assert close(ahu["coil_total_kw"], 71.0)
    assert close(ahu["oa_lps"], 779.6008591746083, 1e-9)
    assert close(ahu["coil_total_kw"], 70.96604025127168, 1e-9)
    assert close(ahu["fan_heat_kw"], 7.940, 1e-3)
    assert close(ahu["supply_lps"], 3036.0, 1e-9)
    assert ahu["highest_standard_room"] == "Z3"


def test_reva_room_values(reva):
    rooms = {r["number"]: r for r in reva["rooms"]}
    for no, sens, lat, supply, oa in [("Z1", 4758.6, 623.7, 400, 70.0), ("Z2", 20138.7, 2318.5, 1670, 501.8),
                                      ("Z3", 438.9, 154.0, 90, 27.7), ("Z4", 1619.8, 374.0, 140, 21.5),
                                      ("Z5", 5514.0, 1171.7, 460, 131.5)]:
        assert rooms[no]["sensible_w"] == pytest.approx(sens, abs=0.1)
        assert rooms[no]["latent_w"] == pytest.approx(lat, abs=0.1)
        assert rooms[no]["supply_lps"] == supply
        assert rooms[no]["oa_lps"] == pytest.approx(oa, abs=0.1)
    assert rooms["Z3"]["governing_case"] == "air_change"      # 6 ACH (83 L/s) above thermal 36.3
    assert rooms["Z1"]["governing_case"] == "thermal"
    assert rooms["Z1"]["thermal_airflow_lps"] == pytest.approx(393.3, abs=0.05)
    assert rooms["TEA"]["exhaust_lps"] == 80.0
    assert rooms["TEA"]["exhaust_calc_lps"] == pytest.approx(71.7, abs=0.05)


def test_reva_oa_methods_reported_together(reva):
    ahu = reva["ahus"][0]
    assert ahu["oa_sum_rooms_lps"] == pytest.approx(752.5, abs=0.05)
    assert ahu["oa_rev_a_lps"] == pytest.approx(779.6, abs=0.05)
    # brief rule: largest room OA fraction x AHU supply incl. leakage
    assert ahu["oa_highest_fraction_lps"] == pytest.approx(0.30741 * 3036.0, rel=1e-4)
    assert ahu["by_oa_method"]["HIGHEST_FRACTION"]["coil_total_kw"] > ahu["by_oa_method"]["MULTI_SPACE"]["coil_total_kw"]


def test_default_method_is_highest_fraction(reva_data):
    data = copy.deepcopy(reva_data)
    del data["constants"]["OAMethod"]
    out = th.recompute(data)
    ahu = out["ahus"][0]
    assert ahu["oa_method"] == "HIGHEST_FRACTION"
    assert ahu["oa_lps"] == pytest.approx(ahu["oa_highest_fraction_lps"])
    assert ahu["oa_fraction"] == pytest.approx(ahu["highest_oa_fraction"])


def test_no_oa_rule_trigger_on_reva(reva):
    assert reva["ahus"][0]["oa_rule_candidates"] == []


def test_oa_rule_triggers_for_small_meeting_room(reva_data):
    data = copy.deepcopy(reva_data)
    data["rooms"][2]["criteria"]["oa_specified_lps"] = 60.0     # 60 of 90 L/s: fraction 0.67, share 3.3 %
    ahu = th.recompute(data)["ahus"][0]
    assert [c["room"] for c in ahu["oa_rule_candidates"]] == ["Z3"]
    assert ahu["oa_rule_candidates"][0]["separable"] is True


def test_cli_json(tmp_path):
    out = tmp_path / "r.json"
    proc = subprocess.run([sys.executable, "-m", "src.recompute.thermal", str(REVA), "--out", str(out)],
                          cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(out.read_text())["totals"]["room_supply_lps"] == 2760.0


# ---- inputs and rules ----------------------------------------------------------------------- #
def test_required_constant_missing(reva_data):
    data = copy.deepcopy(reva_data)
    data["constants"]["SupplyTemp"]["value"] = None
    with pytest.raises(th.RecomputeError, match="SupplyTemp"):
        th.recompute(data)


def test_area_selector(reva_data):
    c = th.read_constants(reva_data)
    room = {"number": "x", "soa_area_m2": 40.0, "measured_area_m2": 40.9}
    assert th.select_area(room, c) == (40.0, "SOA")                       # within 3 %
    room["measured_area_m2"] = 46.04
    assert th.select_area(room, c) == (46.0, "MEASURED")                  # outside tolerance: measured
    assert th.select_area({**room, "area_selector": "SOA"}, c) == (40.0, "SOA")
    assert th.select_area({**room, "area_selector": "LARGER"}, c) == (46.0, "LARGER")


def test_specified_theatre_airflow_is_fixed(reva_data):
    data = copy.deepcopy(reva_data)
    room = data["rooms"][0]
    room["criteria"].update(code="S", supply_specified_lps=1234.0, oa_specified_lps=300.0)
    out = {r["number"]: r for r in th.recompute(data)["rooms"]}
    assert out["Z1"]["supply_lps"] == 1234.0
    assert out["Z1"]["oa_lps"] == 300.0
    assert out["Z1"]["governing_case"] == "specified"
    assert out["Z1"]["required_supply_temp_c"] is not None   # reported for the coil check


def test_rounding_is_applied_once_at_the_end(reva):
    z1 = {r["number"]: r for r in reva["rooms"]}["Z1"]
    assert z1["supply_lps_unrounded"] == pytest.approx(z1["thermal_airflow_lps"], rel=1e-12)
    assert z1["supply_lps"] == 400.0


# ---- pressure: known answers ---------------------------------------------------------------- #
def _base(reva_data):
    data = copy.deepcopy(reva_data)
    data["rooms"] = []
    data["constants"].pop("OAMethod", None)
    return data


def _room(no, area, **pressure):
    return {"number": no, "name": no, "storey": "L01", "ahu": "A1", "zone_tag": no, "system_type": "VAV",
            "space_type": "Pharmacy (OP)", "room_condition": "Critical", "soa_area_m2": area, "ceiling_height_m": 3.0,
            "occupancy_people": 0, "lighting_w_m2": 0, "small_power_w_m2": 0, "criteria": {
                "min_total_ach": 6, "pressure_regime": pressure.pop("regime"), "pressure_pa_tender": pressure.pop("pa"),
                "source": "T", "code": "D"}, "pressure": {"mode": "controlled", **pressure}}


SMALL_SURFACE = pm.internal_surface(1.88 * 2.94, 2 * (1.88 + 2.94), 3.0)
LARGE_SURFACE = pm.internal_surface(3.97 * 4.83, 2 * (3.97 + 4.83), 3.0)


def test_known_answer_room_at_minus_15_pa(reva_data):
    """D-028: envelope only, no door data. Offset needed = envelope leakage at the set pressure; design offset = 2.5 x surface."""
    data = _base(reva_data)
    data["pressure_nodes"] = [{"tag": "CORRIDOR", "pressure_pa": 0.0}]
    data["rooms"] = [_room("ANTE", 5.5, regime="NEGATIVE", pa=-15, suite="S1", surface_override_m2=SMALL_SURFACE, doors=[])]
    out = th.recompute(data)
    p = out["rooms"][0]["pressure"]
    assert p["offset_needed_m3h"] == pytest.approx(45.7, abs=0.2)
    assert p["design_offset_m3h"] == pytest.approx(99.9, abs=0.2)
    assert p["ratio"] == pytest.approx(2.19, abs=0.01)
    assert p["offset_kind"] == "exhaust"


def test_known_answer_room_and_ensuite(reva_data):
    data = _base(reva_data)
    data["pressure_nodes"] = [{"tag": "CORRIDOR", "pressure_pa": 0.0}]
    data["rooms"] = [
        _room("ROOM", 19.2, regime="NEGATIVE", pa=-30, suite="S2", surface_override_m2=LARGE_SURFACE, doors=[]),
        _room("ENS", 8.0, regime="NEGATIVE", pa=-31, suite="S2", surface_override_m2=37.5, doors=[]),
    ]
    p = {r["number"]: r["pressure"] for r in th.recompute(data)["rooms"]}
    assert p["ROOM"]["offset_needed_m3h"] == pytest.approx(163.5, abs=0.2)
    assert p["ENS"]["offset_needed_m3h"] == pytest.approx(68.7, abs=0.2)
    assert p["ROOM"]["design_offset_m3h"] == pytest.approx(227.9, abs=0.2)
    assert p["ENS"]["design_offset_m3h"] == pytest.approx(93.8, abs=0.2)
    assert p["ROOM"]["ratio"] == pytest.approx(1.39, abs=0.01)
    assert p["ENS"]["ratio"] == pytest.approx(1.36, abs=0.01)


def test_pressure_geometry_route_and_airflow_offset(reva_data):
    """Surface from area, perimeter and height; the negative room's exhaust is supply plus offset."""
    data = _base(reva_data)
    data["pressure_nodes"] = [{"tag": "CORRIDOR", "pressure_pa": 0.0}]
    room = _room("ISO", 19.2, regime="NEGATIVE", pa=-30, suite="S3", perimeter_m=17.6,
                 doors=[{"id": "d1", "to": "CORRIDOR", "seal": "hinged"}])
    data["rooms"] = [room]
    out = th.recompute(data)["rooms"][0]
    assert out["pressure"]["surface_m2"] == pytest.approx(2 * 19.2 + 17.6 * 3.0)
    # D-031/D-032: the total extract is supply + offset; the exhaust that leaves the zone is the larger of the required exhaust and the offset air, the rest returns
    assert out["extract_lps"] == pytest.approx(out["supply_lps"] + out["pressure"]["offset_needed_lps"])
    assert out["exhaust_lps"] == pytest.approx(max(out["exhaust_required_lps"], out["pressure"]["offset_needed_lps"]))
    assert out["offset_kind"] == "exhaust"


def test_positive_room_supply_exceeds_exhaust(reva_data):
    data = _base(reva_data)
    data["pressure_nodes"] = [{"tag": "CORRIDOR", "pressure_pa": 0.0}]
    data["rooms"] = [_room("CLEAN", 20.0, regime="POSITIVE", pa=15, suite="S4", perimeter_m=18.0,
                           doors=[{"id": "d1", "to": "CORRIDOR", "seal": "hinged"}])]
    out = th.recompute(data)["rooms"][0]
    assert out["offset_kind"] == "supply"
    assert out["extract_lps"] == pytest.approx(out["supply_lps"] - out["offset_lps"])     # D-032: extract = supply - offset; exhaust = the required exhaust
    assert out["offset_lps"] > 0


def test_direction_only_room_is_listed_not_calculated(reva_data):
    data = _base(reva_data)
    room = _room("DIR", 20.0, regime="NEGATIVE", pa=None, suite="S5", perimeter_m=18.0, doors=[])
    data["rooms"] = [room]
    out = th.recompute(data)
    assert out["pressure_unresolved"] == ["DIR"]
    assert out["rooms"][0]["pressure"] is None


def test_free_ensuite_takes_pressure_from_balance(reva_data):
    data = _base(reva_data)
    data["pressure_nodes"] = [{"tag": "CORRIDOR", "pressure_pa": 0.0}]
    iso = _room("ISO", 19.2, regime="NEGATIVE", pa=-15, suite="S6", perimeter_m=17.6,
                doors=[{"id": "d1", "to": "CORRIDOR", "seal": "hinged"}, {"id": "d2", "to": "WC", "gap_cm2": 50}])
    wc = _room("WC", 4.0, regime="NEUTRAL", pa=None, suite="S6", perimeter_m=8.0, doors=[])
    wc["pressure"]["mode"] = "free"
    wc["criteria"].update(exhaust_specified_lps=20.0, min_total_ach=0)
    data["rooms"] = [iso, wc]
    out = {r["number"]: r for r in th.recompute(data)["rooms"]}
    assert out["WC"]["pressure"]["mode"] == "free"
    assert out["WC"]["pressure"]["status"] == "FREE"
    # D-030 (no supply floor from exhaust): the WC has no supply, so its 20 L/s exhaust is made up by transfer air through the door
    # and the WC sits below the isolation room (before D-030 the supply floor put it between -15 and 0 Pa).
    assert out["WC"]["pressure"]["pressure_pa"] < -15.0


def test_pressure_module_selftest_still_passes():
    assert pm.run_selftest(out=open("/dev/null", "w")) == 0
