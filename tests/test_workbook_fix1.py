"""Fix round 1 of the workbook generator: outdoor-air cap, door convention, coil basis, README units, print proof,
area rounding, author, outdoor-air rule records, MOT specified rooms, per-input provenance."""

from __future__ import annotations

import copy
import csv
from pathlib import Path

import openpyxl
import pytest

from src.recompute.thermal import recompute
from src.workbook import selftest as st
from src.workbook.build import build_workbook
from src.workbook.layouts import AHU_KEYS, FCU_KEYS, af_spec, ri_spec
from src.workbook.proposals import read_proposals, update_csv
from src.workbook.recalc import recalc, to_pdf
from tests.test_workbook_systems import _af, _dept, _room, _rows

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def ka(tmp_path_factory):
    work = tmp_path_factory.mktemp("ka")
    data = st.ka_input()
    build_workbook(data, work / "ka.xlsx")
    return dict(data=data, path=work / "ka.xlsx", rc=recalc(work / "ka.xlsx", work / "rc"), work=work)


def test_outdoor_air_never_exceeds_supply_in_single_pass_negative_rooms(ka):
    af = {r["no"]: r for r in _af(ka["rc"])}
    for no, supply in (("ANTE", 30), ("ROOM", 100), ("ENSUITE", 20)):
        assert af[no]["supply"] == supply and af[no]["oa"] == supply and af[no]["frac"] == pytest.approx(1.0)
        assert af[no]["extract"] > supply                      # exhaust beyond supply is made up by transfer air, not outdoor air
    ahu = dict(zip(AHU_KEYS, _rows(ka["rc"], "AHU")[0]))
    assert ahu["oa_rule"] == pytest.approx(ahu["sa_ahu"]) and ahu["ret"] >= 0
    checks = {r[1]: r[4] for r in _rows(ka["rc"], "Checks") if r[1]}
    assert checks["Rooms whose outdoor air exceeds their supply"] == "PASS"
    assert checks["AHU balances that do not close"] == "PASS"


def test_independent_recomputation_agrees_with_the_known_answer_department(ka):
    lines = st.compare_recompute(ka["data"], ka["rc"])
    assert lines and all(x["ok"] for x in lines), [x for x in lines if not x["ok"]]
    # the anteroom door D3 is listed once, under ANTE, and acts on both spaces: ROOM needs 202.05 m3/h (workbook) = recomputation
    need = {x["item"]: x for x in lines}["room ROOM offset needed m3/h"]
    assert need["workbook"] == pytest.approx(202.05, abs=0.1) and need["recomputed"] == pytest.approx(202.05, abs=0.2)


def test_door_listed_from_both_sides_counts_once(ka):
    twice = copy.deepcopy(ka["data"])
    room = next(r for r in twice["rooms"] if r["number"] == "ROOM")
    room["pressure"]["doors"].append(dict(id="D3", to="ANTE", kind="sliding sealed door", seal="sliding", code="A", source="GEN-PRESSURE"))
    base = {r["number"]: r["pressure"]["offset_needed_m3h"] for r in recompute(ka["data"])["rooms"]}
    dup = {r["number"]: r["pressure"]["offset_needed_m3h"] for r in recompute(twice)["rooms"]}
    assert dup == pytest.approx(base)


def test_coil_duty_on_the_example_basis_is_rev_a():
    from src.workbook.reva import build_reva_input
    # reference room condition = first space type (Rev A AHU Summary B13/B14), not a supply-weighted mean
    data = build_reva_input(ROOT)
    res = recompute(data, "MULTI_SPACE")
    assert res["ahus"][0]["reference_h_kjkg"] == pytest.approx(52.6, abs=0.05)


def test_readme_units_block_is_whole_text(ka):
    wb = openpyxl.load_workbook(ka["rc"], data_only=True)
    ws = wb["README"]
    row = next(r for r in range(1, 80) if ws.cell(row=r, column=2).value == "Units and abbreviations")
    text = ws.cell(row=row + 1, column=2).value
    assert len(text) > 150 and "ACH air changes per hour" in text and "SOA schedule of accommodation" in text
    head = next(r for r in range(1, 80) if ws.cell(row=r, column=2).value == "Revision")
    assert [ws.cell(row=head + 1, column=c).value for c in range(2, 8)] == ["Rev", "Date", "Description", "By", "Chk'd", "App'd"]


def test_print_proof_passes_and_airflow_prints_every_column(ka):
    pdf = to_pdf(ka["rc"], ka["work"] / "pdf")
    proof = st.print_proof(ka["rc"], pdf)
    assert proof["problems"] == []
    wb = openpyxl.load_workbook(ka["path"])
    assert wb["Airflow"].print_area.split(":")[1].startswith("$" + openpyxl.utils.get_column_letter(len(af_spec())))
    assert all(v["scale"] >= 0.75 for v in proof["sheets"].values())
    import re
    txt = re.sub(r"\s+", " ", __import__("subprocess").run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True).stdout)
    for word in ("Outdoor-air rule", "Separated", "Highest-fraction"):
        assert word in txt


def test_areas_are_taken_to_one_decimal(ka):
    wb = openpyxl.load_workbook(ka["rc"], data_only=True)
    ws = wb["Room Information"]
    keys = [k for k, *_ in ri_spec()]
    col = keys.index("area") + 1
    areas = [ws.cell(row=r, column=col).value for r in range(7, 10)]
    assert areas == pytest.approx([5.5, 19.2, 4.0], abs=1e-9)
    ahu = dict(zip(AHU_KEYS, _rows(ka["rc"], "AHU")[0]))
    assert ahu["area"] == pytest.approx(28.7, abs=1e-9)


def test_author_is_not_hard_coded(ka, tmp_path):
    wb = openpyxl.load_workbook(ka["path"])
    assert "Beca" not in (wb.properties.creator or "") or ka["data"]["project"]["revision"].get("by") == wb.properties.creator
    d = copy.deepcopy(ka["data"])
    d["project"]["revision"].update(by="A. Author", checked="B. Checker", approved="C. Approver")
    build_workbook(d, tmp_path / "p.xlsx")
    wb = openpyxl.load_workbook(tmp_path / "p.xlsx")
    assert wb.properties.creator == "A. Author"
    d["project"]["revision"].update(by="")
    build_workbook(d, tmp_path / "q.xlsx")
    assert openpyxl.load_workbook(tmp_path / "q.xlsx").properties.creator in ("", None)


def test_per_input_provenance_on_room_information(ka):
    wb = openpyxl.load_workbook(ka["rc"], data_only=True)
    ws = wb["Room Information"]
    hdr = [k for k, *_ in ri_spec()]
    for k in ("ahu", "zone", "systype", "terminal", "exfan", "oasrc", "oacond", "no", "name", "storey"):
        assert k + "_c" in hdr and k + "_s" in hdr
    c = hdr.index("terminal") + 1
    assert ws.cell(row=7, column=c).value is None            # blank terminal tag: no code, no source
    assert ws.cell(row=7, column=c + 1).value is None and ws.cell(row=7, column=c + 2).value is None
    c = hdr.index("ahu") + 1
    assert ws.cell(row=7, column=c + 1).value == "D"


@pytest.fixture(scope="module")
def oa_dept(tmp_path_factory):
    work = tmp_path_factory.mktemp("oadept")
    rooms = [_room("A1", "Large 1", 300), _room("A2", "Large 2", 300), _room("A3", "Large 3", 300),
             _room("M1", "Meeting", 12, oa_spec=40.0, terminal="VAV-M")]
    d = _dept(rooms, fans=[dict(tag="OAF-1", type="OUTDOOR AIR", serves="AHU-T1", scheduled_lps=3000, source="RA-AHU-REF", code="D")],
              ahus=[dict(tag="AHU-T1", type="AHU", sched_supply_lps=1000, sched_oa_lps=300, sched_source="RA-AHU-REF", sched_code="D")])
    build_workbook(d, work / "oa.xlsx")
    return dict(rc=recalc(work / "oa.xlsx", work / "rc"), work=work, data=d)


def test_separation_is_recorded_on_flags_and_in_the_csv(oa_dept):
    flags = [r for r in _rows(oa_dept["rc"], "Flags") if r[2] == "M1" and str(r[3]).startswith("Outdoor-air rule: separate")]
    assert len(flags) == 1 and flags[0][1] == "CONFIRM" and flags[0][4] == "OPEN"
    assert "FCU outdoor air from OAF-1" in flags[0][3] and "coil duty" in flags[0][3]
    out = oa_dept["work"] / "zoning_change_proposals.csv"
    rows = update_csv(oa_dept["rc"], out)
    assert len(rows) == 1
    r = next(csv.DictReader(out.open(encoding="utf-8")))
    assert r["room_no"] == "M1" and r["rule_outcome"] == "SEPARATE" and r["approval"] == "PENDING Marcus"
    assert float(r["ahu_oa_after_lps"]) < float(r["ahu_oa_before_lps"]) and float(r["coil_after_kw"]) < float(r["coil_before_kw"])
    assert r["fcu_oa_source"] == "OAF-1" and r["flag_id"].startswith("F-")
    # a second build of the same department replaces its rows
    update_csv(oa_dept["rc"], out)
    assert len(list(csv.DictReader(out.open(encoding="utf-8")))) == 1


def test_fcu_capacity_is_tested_with_the_added_air(oa_dept):
    fcu = dict(zip(FCU_KEYS, _rows(oa_dept["rc"], "FCU")[0]))
    assert fcu["oasrc"] == "OAF-1" and fcu["cap"] == "PASS"
    fans = {r[0]: r for r in _rows(oa_dept["rc"], "Fans") if r[0]}
    ahu_after = dict(zip(AHU_KEYS, [r for r in _rows(oa_dept["rc"], "AHU") if r[0] == "AHU-T1"][1]))
    # required airflow of the outdoor-air fan = AHU outdoor air after separation + the separated room's outdoor air
    assert fans["OAF-1"][3] == pytest.approx(ahu_after["oa_rule"] + fcu["oa"], rel=1e-9)


def test_mot_specified_room_is_not_resized_and_offset_is_reported(tmp_path):
    base = _room("T1", "Theatre", 40, terminal="VAV-T", extra=None)
    base["criteria"].update(code="S", supply_specified_lps=800.0, oa_specified_lps=800.0, exhaust_specified_lps=700.0, pressure_regime="NEGATIVE",
                            pressure_pa_tender=-15, single_pass="N")
    base["pressure"] = dict(mode="controlled", suite="S1", perimeter_m=26.0, prov=["M", "RA-RZ-30"], doors=[
        dict(id="TD1", to="CORRIDOR", kind="hinged door", seal="hinged", code="A", source="GEN-PRESSURE")])
    d = _dept([base, _room("A1", "Office", 100)])
    d["pressure_nodes"] = [dict(tag="CORRIDOR", description="Corridor", pressure_pa=0, code="S", source="GEN-PRESSURE")]
    build_workbook(d, tmp_path / "t.xlsx")
    rc = recalc(tmp_path / "t.xlsx", tmp_path / "rc")
    af = {r["no"]: r for r in _af(rc)}
    t1 = af["T1"]
    assert t1["fix"] == 1 and t1["supply"] == 800.0 and t1["oa"] == 800.0 and t1["gov"] == "SPECIFIED"
    assert t1["extract"] == 700.0                                   # specified exhaust, not supply + offset
    assert t1["spec_off"] == pytest.approx(-100.0)                  # the specified exhaust is below the supply: reported beside the need
    assert t1["off_chk"] == "SHORT" and t1["clin"] == "YES"          # never separated automatically
    res = {r["number"]: r for r in recompute(d)["rooms"]}["T1"]
    assert res["supply_lps"] == 800.0 and res["exhaust_lps"] == 700.0 and res["governing_case"] == "specified"
    checks = {r[1]: r[4] for r in _rows(rc, "Checks") if r[1]}
    assert checks["Specified (MOT) rooms whose specified offset is below the offset needed"] == "VERIFY"


def test_live_assumption_flags_are_grouped_and_not_garbled():
    from src.workbook.reva import build_reva_input
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        ctx = build_workbook(build_reva_input(ROOT), Path(tmp) / "r.xlsx")
        wb = openpyxl.load_workbook(Path(tmp) / "r.xlsx")
        texts = [str(r[3]) for r in wb["Flags"].iter_rows(min_row=7, values_only=True) if r[3]]
    assert not any("= row" in t or "= None" in t for t in texts)
    assert not any(t.startswith("envelope room = ") for t in texts)
    assert any(t.startswith("Envelope segment Z1-N") for t in texts)
