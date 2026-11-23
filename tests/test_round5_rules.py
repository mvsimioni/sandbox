"""Round 5 calculation rules (D-030 to D-033, D-035, D-036, D-039/D-057/D-058, D-043, D-053, D-054/D-055, D-060, D-066): known answers in both engines.

The recomputation (src/recompute) is tested on its own; the workbook formulas are tested by building a small department, recalculating it and comparing every room
and AHU line with the recomputation.
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import pytest

from src.recompute import thermal as th

ROOT = Path(__file__).resolve().parents[1]
REVA = ROOT / "tests" / "fixtures" / "recompute" / "reva_pharmacy.json"


@pytest.fixture(scope="module")
def reva_data():
    return json.loads(REVA.read_text(encoding="utf-8"))


def room(no, name="room", area=20.0, ahu="A1", crit=None, **kw):
    c = dict(source="T", code="D", min_total_ach=6)
    c.update(crit or {})
    r = {"number": no, "name": name, "storey": "L01", "ahu": ahu, "zone_tag": no, "system_type": "VAV", "space_type": "Pharmacy (OP)",
         "room_condition": "Comfort", "soa_area_m2": area, "ceiling_height_m": 3.0, "occupancy_people": 0, "lighting_w_m2": 0, "small_power_w_m2": 0, "criteria": c}
    r.update(kw)
    return r


def base(reva_data, rooms, ahus=None):
    d = copy.deepcopy(reva_data)
    d["constants"].pop("OAMethod", None)
    d["rooms"] = rooms
    if ahus is not None:
        d["ahus"] = ahus
    return d


# ---- D-030: no supply floor from exhaust without a pressure offset ------------------------------ #
def test_d030_exhaust_is_no_supply_floor_without_offset(reva_data):
    r = room("T1", "Toilet", 4.0, crit=dict(min_total_ach=0, exhaust_ach=20))
    out = th.recompute(base(reva_data, [r]))["rooms"][0]
    assert out["supply_lps"] == 0.0                      # no supply: the exhaust is made up by transfer air
    assert out["exhaust_lps"] == pytest.approx(math.ceil(20 * 4.0 * 3.0 / 3.6 / 10) * 10)
    assert out["extract_lps"] == out["exhaust_lps"]


# ---- D-031: clean single-pass rooms set the fraction, dirty ones do not -------------------------- #
def test_d031_dirty_single_pass_does_not_set_the_fraction(reva_data):
    clean = room("C1", "Isolation Room", 20.0, crit=dict(single_pass="Y", air_class="1", min_oa_ach=2))
    dirty = room("D1", "Dirty Utility", 10.0, crit=dict(single_pass="Y", min_oa_ach=2, min_total_ach=10, exhaust_ach=10, pressure_regime="NEGATIVE"))
    other = room("O1", "Office", 30.0, crit=dict(min_oa_ach=2))
    res = th.recompute(base(reva_data, [clean, dirty, other]))
    by = {r["number"]: r for r in res["rooms"]}
    assert by["C1"]["sp_class"] == "CLEAN" and by["D1"]["sp_class"] == "DIRTY"
    assert by["C1"]["oa_lps"] == by["C1"]["supply_lps"]          # clean: 100 % outdoor air
    assert by["D1"]["oa_lps"] < by["D1"]["supply_lps"]           # dirty: only its own minimum outdoor air
    assert by["D1"]["return_lps"] == 0.0 and by["D1"]["exhaust_lps"] == by["D1"]["extract_lps"]
    ahu = res["ahus"][0]
    assert ahu["dirty_single_pass_rooms"] == ["D1"]
    # without the clean room the fraction comes from the other rooms only
    res2 = th.recompute(base(reva_data, [dirty, other]))
    assert res2["ahus"][0]["highest_oa_fraction"] < 0.5


def test_classifier_rules():
    f = th.classify_single_pass
    assert f("Anything", {"single_pass": "N"}) == ""
    assert f("Anything", {"single_pass": "Y", "sp_class": "dirty"}) == "DIRTY"
    assert f("OT", {"single_pass": "Y", "code": "S", "supply_specified_lps": 1000, "oa_specified_lps": 1000}) == "CLEAN"
    assert f("Room", {"single_pass": "Y", "air_class": "1"}) == "CLEAN"
    assert f("Toilet - Staff", {"single_pass": "Y", "pressure_regime": "NEGATIVE"}) == "DIRTY"
    assert f("Dirty Utility", {"single_pass": "Y", "pressure_regime": "NEGATIVE"}) == "DIRTY"
    assert f("Isolation Room", {"single_pass": "Y", "pressure_regime": "NEGATIVE"}) == "CLEAN"
    from src.workbook.singlepass import classify_single_pass as g
    for name, c in (("Toilet - Staff", {"single_pass": "Y", "pressure_regime": "NEGATIVE"}), ("Isolation Room", {"single_pass": "Y"}), ("Store", {"single_pass": "Y", "pressure_regime": "POSITIVE"})):
        assert f(name, c) == g(name, c)          # the two copies of the rule agree


# ---- D-032: balance at the AHU ------------------------------------------------------------------- #
def test_d032_ahu_balance(reva_data):
    # one negative room with an offset, one exhaust room with no offset
    iso = room("ISO", "Ward Room", 19.2, crit=dict(min_total_ach=6, pressure_regime="NEGATIVE", pressure_pa_tender=-15, exhaust_ach=6),
               pressure=dict(mode="controlled", suite="S1", perimeter_m=17.6, doors=[]))
    ex = room("EX", "Store", 10.0, crit=dict(min_total_ach=6, exhaust_ach=10))
    d = base(reva_data, [iso, ex])
    d["pressure_nodes"] = [{"tag": "REF", "pressure_pa": 0.0}]
    res = th.recompute(d)
    a = res["ahus"][0]
    rooms = {r["number"]: r for r in res["rooms"]}
    assert a["exhaust_lps"] == pytest.approx(sum(r["exhaust_lps"] for r in rooms.values()))
    assert a["offset_net_lps"] == pytest.approx(-rooms["ISO"]["offset_lps"])          # a negative room draws air in through the envelope
    assert a["oa_balance_lps"] == pytest.approx(a["exhaust_lps"])                     # net offset air entering the zone adds no make-up
    assert a["oa_lps"] == pytest.approx(min(a["supply_lps"], max(a["oa_highest_fraction_lps"], a["oa_balance_lps"])))
    assert a["recirculated_lps"] == pytest.approx(a["supply_lps"] - a["oa_lps"])
    assert a["spill_lps"] == pytest.approx(a["oa_lps"] - a["exhaust_lps"] - a["offset_net_lps"])


def test_d032_positive_room_offset_air_is_made_up_by_outdoor_air(reva_data):
    pos = room("POS", "Clean Room", 20.0, crit=dict(min_total_ach=20, pressure_regime="POSITIVE", pressure_pa_tender=15),
               pressure=dict(mode="controlled", suite="S2", perimeter_m=18.0, doors=[]))
    d = base(reva_data, [pos])
    d["pressure_nodes"] = [{"tag": "REF", "pressure_pa": 0.0}]
    a = th.recompute(d)["ahus"][0]
    assert a["offset_net_lps"] > 0
    assert a["oa_balance_lps"] == pytest.approx(a["exhaust_lps"] + a["offset_net_lps"])


# ---- D-033: outdoor air per area or per person is a supply floor ---------------------------------- #
def test_d033_outdoor_air_per_area_is_a_supply_floor(reva_data):
    r = room("OF", "Office", 100.0, crit=dict(min_total_ach=0, oa_lps_per_m2=0.6))
    out = th.recompute(base(reva_data, [r]))["rooms"][0]
    assert out["oa_required_lps"] == pytest.approx(60.0)
    assert out["supply_lps"] >= 60.0


def test_d033_outdoor_air_per_person_is_a_supply_floor(reva_data):
    r = room("OP", "Office", 20.0, crit=dict(min_total_ach=0, oa_lps_per_person=10.0), occupancy_people=8, activity="Seated", iof=1)
    out = th.recompute(base(reva_data, [r]))["rooms"][0]
    assert out["oa_required_lps"] == pytest.approx(80.0)
    assert out["supply_lps"] >= 80.0


# ---- D-053: design condition from the criteria range ---------------------------------------------- #
@pytest.mark.parametrize("lo,hi,sp,expect", [
    (21, 24, None, 24.0),        # range contains 24: 24
    (20, 23, None, 23.0),        # range below 24: the upper limit
    (25, 27, None, 25.0),        # range above 24: the lower limit
    (None, 25, None, 25.0),      # one printed value only: it is both limits
    (21, 25, 22.0, 22.0),        # a printed set point wins
])
def test_d053_design_temperature(reva_data, lo, hi, sp, expect):
    c = th.read_constants(reva_data)
    crit = {"temp_min_c": lo, "temp_max_c": hi, "temp_setpoint_c": sp}
    t, rh, h = th.design_condition(crit, c)
    assert t == pytest.approx(expect)


def test_d053_humidity_and_enthalpy(reva_data):
    c = th.read_constants(reva_data)
    t, rh, h = th.design_condition({"temp_min_c": 21, "temp_max_c": 24, "rh_max_pct": 65}, c)
    assert rh == pytest.approx(0.60)                         # smaller of 60 % and the printed maximum
    assert h == pytest.approx(52.6, abs=0.1)                 # 24 degC at 60 % RH, as the engine condition
    t, rh, h = th.design_condition({"temp_min_c": 21, "temp_max_c": 24, "rh_max_pct": 50}, c)
    assert rh == pytest.approx(0.50)
    assert th.design_condition({}, c) is None                # no printed temperature: the space type keeps the condition


def test_d053_room_uses_the_condition_and_flags_the_lower_limit(reva_data):
    r = room("R1", "Procedure", 20.0, crit=dict(temp_min_c=20, temp_max_c=23, rh_max_pct=60, min_total_ach=6), lighting_w_m2=60)
    out = th.recompute(base(reva_data, [r]))["rooms"][0]
    assert out["room_t_c"] == pytest.approx(23.0)
    assert out["thermal_lo_lps"] is not None and out["thermal_lo_lps"] > out["thermal_airflow_lps"]


# ---- D-060: duct leakage on fan coil units ---------------------------------------------------------- #
def test_d060_fcu_leakage(reva_data):
    c = th.read_constants(reva_data)
    assert th.unit_leakage({"type": "AHU"}, c) == pytest.approx(0.10)
    assert th.unit_leakage({"type": "FCU", "ducted": "Y"}, c) == pytest.approx(0.10)
    assert th.unit_leakage({"type": "FCU", "ducted": "N"}, c) == 0.0
    assert th.unit_leakage({"type": "FCU"}, c) == pytest.approx(0.10)       # unknown: assumed ducted
    r = room("F1", "Office", 20.0, ahu="FCU-1", crit=dict(min_total_ach=6))
    d = base(reva_data, [r], [{"tag": "FCU-1", "type": "FCU", "ducted": "N"}])
    a = th.recompute(d)["ahus"][0]
    assert a["supply_lps"] == pytest.approx(a["room_supply_sum_lps"])


# ---- D-067: the SOA selector returns the SOA ---------------------------------------------------------- #
def test_d067_soa_selector_with_a_dpa_area(reva_data):
    c = th.read_constants(reva_data)
    r = {"number": "x", "soa_area_m2": 39.3, "dpa_area_m2": 686.6, "area_selector": "SOA"}
    assert th.select_area(r, c) == (39.3, "SOA")
    r["area_selector"] = "DPA"
    assert th.select_area(r, c) == (686.6, "DPA")


# ---- D-035: highest ceiling height ---------------------------------------------------------------------- #
def test_d035_highest_height():
    from src.workbook.linking import rls_height_m, rls_printed_values_m
    row = {"heights_mm_as_printed": "CH=3000; CH=2700; CH=3000", "governing_height_mm": "2700"}
    assert rls_printed_values_m(row) == [2.7, 3.0]
    assert rls_height_m(row) == 3.0
    assert rls_height_m({"governing_height_mm": "2700"}) == 2.7


# ---- D-036: HFDS rows -------------------------------------------------------------------------------------- #
def test_d036_hfds_rows_cite_table_and_page():
    from src.workbook import hfds
    hit = hfds.find_row(ROOT, "UPS Room")
    assert hit is not None and hit[0]["area_name"] == "LAN Room" and hit[1] == "it"
    c = hfds.to_criteria(hit[0], "HFDS-X", hit[1], hit[2])
    assert c["temp_setpoint_c"] == 24.0 and "table T.1.1.1.1, page 44" in c["remark"] and "table T.1.1.1.1, page 44" in c["page"]
    sc = hfds.find_row(ROOT, "Scrub Up")
    assert sc[0]["table_id"] == "T.1.1.3.7" and hfds.to_criteria(sc[0], "X", sc[1], sc[2])["min_total_ach"] == 15.0
    assert hfds.find_row(ROOT, "Plant Room - AHU")[0]["area_name"] == "Chiller Plant Room"
    diffs = hfds.compare({"pressure_regime": "POSITIVE", "min_total_ach": 6.0}, hit[0])
    assert diffs == []                                           # the LAN row prints no air changes and no pressure direction
    tt = hfds.find_row(ROOT, "Dirty Utility Room")[0]
    assert hfds.compare({"pressure_regime": "NEUTRAL", "min_total_ach": 6.0}, tt) == ["pressure neutral against negative", "total air changes 6 against 10"]


# ---- D-043: provisional equipment W/m2 ------------------------------------------------------------------------- #
def test_d043_provisional_equipment():
    from src.workbook import round5
    assert round5.equipment_provisional("Data Hall", ROOT) == ("data_centre", 1000.0)
    assert round5.equipment_provisional("UPS Room", ROOT)[0] == "ups_battery"
    assert round5.equipment_provisional("Corridor", ROOT) is None


# ---- D-054 / D-055: when a reduction is stated -------------------------------------------------------------------- #
def test_d055_tender_base_rule():
    from src.workbook.round5 import tender_base_rule as f
    assert f({}, 1000.0, 1010.0)[0] == "" and f({}, 1000.0, 1010.0)[1] == ""                    # 1 %: schedule stands, no note
    r, n = f({}, 1000.0, 1060.0)
    assert r == "" and "6 % apart" in n and "VERIFY" in n                                         # 6 %: both values, VERIFY, reduction allowed
    r, n = f({}, 1000.0, 1300.0)
    assert "30 % apart" in r                                                                     # above 10 %: no reduction
    r, n = f({}, 9000.0, None)
    assert "placeholder" in r                                                                    # 9,000 L/s
    r, n = f({"tender_other_values": [(2360.0, "a"), (6160.0, "b")]}, None, None)
    assert "two values" in r                                                                     # two printed values, no schedule
    assert f({}, 3450.0, 3450.0, is_mot=True)[0].startswith("MOT equipment is not resized")      # D-057


def test_d054_coverage_formula_text():
    from src.workbook.sheets_systems import coverage_formula
    f = coverage_formula("Z", "ZA", "NOK", "AOK", "ZoneCoverMin", "NC", "OTH", zmax="ZoneCoverMax", sl="SL", n="N", norr="NR")
    assert "ZoneCoverMax" in f and "SL" in f and 'NR=""' in f and "second room list" in f


def test_d054_second_room_list():
    from src.workbook.round5 import second_room_list
    rows = [{"ahu_tag": "AHU-1; AHU-2", "typical_count": "3", "sheet": "S1"}, {"ahu_tag": "AHU-2", "typical_count": "", "sheet": "S2"}, {"ahu_tag": "AHU-9", "typical_count": "5", "sheet": "S3"}]
    assert second_room_list(["AHU-2"], rows) == (4, "S1, S2")
    assert second_room_list(["AHU-7"], rows) is None


# ---- D-039 / D-057 / D-058: operating theatres ------------------------------------------------------------------- #
def test_d057_theatre_supply_from_hepa_terminals():
    from src.workbook.round5 import MotHepa
    m = MotHepa(ROOT)
    s = m.for_tags(["AHU-B-07-05MOTGS-01"])
    assert s["face_area"] == pytest.approx(7.84)
    assert s["design"] == pytest.approx(2744.0) and s["capability"] == pytest.approx(3136.0)
    assert s["capability"] * 1.10 == pytest.approx(3449.6)                                       # the schedule duty of 3,450 L/s
    big = m.for_tags(["AHU-B-07-05MOTTKR-01"])
    assert big["design"] == pytest.approx(3584.0) and big["capability"] * 1.10 == pytest.approx(4505.6)     # above 4,450 L/s: VERIFY
    assert m.for_tags(["AHU-B-07-04CATH-02"]) is None                                            # cath lab keeps the airflow of the MOT NSC
    assert m.future["AHU-B-07-05MOTCT-01"] == 5320.0


# ---- D-066: envelope coefficients ----------------------------------------------------------------------------------- #
def test_d066_orientation_table_and_facade_geometry():
    from src.workbook.envelope import Envelope
    e = Envelope(ROOT, {"wall_w_m2_k": 12, "glass_conduction_w_m2_k": 3.4, "solar_w_m2": 211})
    tab = {o["name"]: o for o in e.orientation_table("SRC")}
    n = e.fac["N"]
    assert tab["N"]["wall_w_m2"] == pytest.approx(round(12 * n["uw"], 2))
    assert tab["N"]["window_w_m2"] == pytest.approx(round(3.4 * n["uf"] + 211 * n["cf"] * n["sc"], 2))
    # a 10 m x 5 m room whose south edge lies on the outline: a square of 10 m x 10 m has one south facade
    sq = "POLYGON ((0 0, 10000 0, 10000 10000, 0 10000, 0 0))"
    e._storey_cache["T1"] = __import__("shapely.wkt", fromlist=["loads"]).loads(sq).buffer(100).exterior
    room_w = "POLYGON ((0 0, 10000 0, 10000 5000, 0 5000, 0 0))"
    fl = e.facade_lengths("T1", room_w)
    assert fl.get("S", 0) == pytest.approx(10.0, abs=0.1) and "N" not in fl


# ---- both engines agree on a small department ------------------------------------------------------------------------ #
@pytest.fixture(scope="module")
def built(tmp_path_factory, reva_data):
    from src.workbook.build import build_workbook
    from src.workbook.recalc import recalc
    from src.workbook.reva import build_reva_input
    d = copy.deepcopy(build_reva_input(ROOT))
    d["rooms"] = [
        room("C1", "Isolation Room", 20.0, crit=dict(single_pass="Y", air_class="1", min_oa_ach=2, pressure_regime="NEGATIVE", pressure_pa_tender=-15, exhaust_ach=6, temp_min_c=21, temp_max_c=24, rh_max_pct=60),
             prov_default=["D", "RA-RZ-TBL"], pressure=dict(mode="controlled", suite="S1", perimeter_m=17.6, doors=[], prov=["D", "RA-RZ-TBL"])),
        room("D1", "Dirty Utility", 10.0, crit=dict(single_pass="Y", min_oa_ach=2, min_total_ach=10, exhaust_ach=10, pressure_regime="NEGATIVE", temp_min_c=22, temp_max_c=23), prov_default=["D", "RA-RZ-TBL"]),
        room("T1", "Toilet", 4.0, crit=dict(min_total_ach=0, exhaust_ach=20, temp_setpoint_c=26.0), prov_default=["D", "RA-RZ-TBL"]),
        room("O1", "Office", 40.0, crit=dict(min_oa_ach=2, oa_lps_per_m2=0.6, temp_min_c=25, temp_max_c=27), occupancy_people=5, activity="Seated", prov_default=["D", "RA-RZ-TBL"]),
    ]
    for r in d["rooms"]:
        r["criteria"]["source"] = "RA-RZ-TBL"
        r["prov"] = {"identity": ["D", "RA-RZ-TBL"], "zoning": ["D", "RA-RZ-TBL"], "space_type": ["D", "RA-RZ-TBL"], "soa_area": ["D", "RA-RZ-TBL"],
                     "ceiling_height": ["D", "RA-RZ-TBL"], "occupancy_people": ["D", "RA-RZ-TBL"], "activity": ["D", "RA-RZ-TBL"]}
    d["rooms"][0]["pressure"]["prov"] = ["D", "RA-RZ-TBL"]
    d["ahus"] = [{"tag": "A1", "type": "AHU"}]
    d["pressure_nodes"] = [dict(tag="REF", description="Reference", pressure_pa=0, code="S", source="GEN-PRESSURE")]
    tmp = tmp_path_factory.mktemp("r5")
    jp = tmp / "in.json"
    jp.write_text(json.dumps(d), encoding="utf-8")
    xp = tmp / "out.xlsx"
    build_workbook(jp, xp)
    rc = recalc(xp, tmp / "rc")
    return d, rc


def test_workbook_and_recomputation_agree(built):
    from src.workbook.selftest import compare_recompute
    d, rc = built
    lines = compare_recompute(d, rc)
    bad = [x for x in lines if not x["ok"]]
    assert lines and not bad, bad[:5]


def test_workbook_rules_in_cells(built):
    import openpyxl
    from src.workbook.layouts import AHU_KEYS
    d, rc = built
    wb = openpyxl.load_workbook(rc, data_only=True)
    ah = wb["AHU"]
    pick = lambda k: ah.cell(row=7, column=AHU_KEYS.index(k) + 1).value
    assert pick("dn") == 1                                                    # one dirty single-pass room, listed
    assert pick("oa_rule") == pytest.approx(min(pick("sa_ahu"), max(pick("oa_frac"), pick("oa_bal"))))
    assert pick("ret") == pytest.approx(pick("sa_ahu") - pick("oa_rule"))
    assert pick("relief") == pytest.approx(pick("oa_rule") - pick("exh") - pick("offnet"))
    assert "dirty" in str(pick("balnote")) and "recirculated supply" in str(pick("balnote"))
    cr = wb["Criteria"]
    hdr = {c.value: i for i, c in enumerate(cr[5])}
    rows = {r[0]: r for r in cr.iter_rows(min_row=7, values_only=True) if r[0]}
    td = hdr["Design room temperature"]
    assert rows["C1"][td] == pytest.approx(24.0) and rows["D1"][td] == pytest.approx(23.0) and rows["T1"][td] == pytest.approx(26.0) and rows["O1"][td] == pytest.approx(25.0)
    assert [r[hdr["Design temperature inside the range"]] for r in rows.values()][:2] == ["PASS", "PASS"]
    fb = wb["Flow Balance"]
    st = {r[0]: r[14] for r in fb.iter_rows(min_row=7, values_only=True) if r[0] in ("T1",)}
    assert st["T1"] == "TRANSFER AIR"                                          # exhaust made up by transfer air (D-030)


def test_spill_air_to_plant_rooms_is_in_supply_and_balance(reva_data):
    r = room("O1", "Office", 40.0, crit=dict(min_total_ach=6))
    d = base(reva_data, [r], [{"tag": "A1", "type": "AHU", "spill_req_lps": 100.0}])
    a = th.recompute(d)["ahus"][0]
    assert a["supply_lps"] == pytest.approx((a["room_supply_sum_lps"] + 100.0) * 1.10)
    assert a["oa_balance_lps"] == pytest.approx(a["exhaust_lps"] + 100.0)
