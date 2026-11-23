"""Round 4: D-025 (plug load and lighting from ASHRAE), D-026 (presumed criteria), D-027 (reduction wording and completeness), D-028 (pressure from the airtightness
criterion, no door data), and the round-3 findings R3-02, R3-05, R3-06, R3-07, R3-08, R3-10."""
import csv
import re
from pathlib import Path

import openpyxl
import pytest

ROOT = Path(__file__).resolve().parents[1]
WB = ROOT / "out" / "loads" / "workbooks"
INP = ROOT / "out" / "loads" / "inputs"

needs_outputs = pytest.mark.skipif(not (WB / "AHU_master_summary.xlsx").exists() or not (INP / "D30.json").exists(), reason="outputs not present")


_CACHE: dict = {}


def _wb(num):
    if num not in _CACHE:
        f = next(WB.glob(f"AIGH_C2_D{num}_*_RevA.xlsx"))
        _CACHE[num] = openpyxl.load_workbook(f, data_only=True)
    return _CACHE[num]


def _all_text(path):
    """Every text cell of a workbook (read-only pass: fast)."""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    for ws in wb:
        for row in ws.iter_rows(values_only=True):
            for v in row:
                if isinstance(v, str):
                    yield ws.title, v


# D-025 -------------------------------------------------------------------------------------------------------------
def test_load_use_mapping():
    from src.workbook.assemble import load_use
    assert load_use("Office")[0] == "office"
    assert load_use("1 Bed Room - ICU")[0] == "icu_hdu_bed"
    assert load_use("Patient Bay - Recovery")[0] == "recovery"
    assert load_use("CT Scanning - Procedure Room")[0] == "procedure_imaging"
    assert load_use("Nurse Station")[0] == "nurse_station"
    assert load_use("Satellite Supply Station")[0] == "store"
    assert load_use("Locker - Staff")[0] == "toilet"
    assert load_use("Foo Bar")[0] is None


@needs_outputs
def test_loads_are_ashrae_values_in_red_with_a_remarks_note():
    import json
    import yaml
    cfg = yaml.safe_load((ROOT / "config" / "project.yaml").read_text(encoding="utf-8"))["internal_loads"]
    d = json.loads((INP / "D30.json").read_text(encoding="utf-8"))
    office = next(r for r in d["rooms"] if r.get("load_use") == "office")
    assert office["lighting_w_m2"] == cfg["lighting_w_m2"]["office"] and office["small_power_w_m2"] == cfg["plug_w_m2"]["office"]
    assert office["prov"]["lighting"][0] == "A" and office["prov"]["small_power"][0] == "A"
    wb = _wb("30")
    ws = wb["Room Information"]
    hdr = [c.value for c in ws[5]]
    n_red = 0
    for row in ws.iter_rows(min_row=7):
        if not row[0].value:
            continue
        light, power, rem = row[hdr.index("Lighting")], row[hdr.index("Small power")], row[hdr.index("Remarks")].value or ""
        if light.value not in (None, ""):
            for c in (light, power):                       # red text (the named style Assumption-Load); the issued file is read back by font, as its style ids are renumbered
                assert c.font.color is not None and c.font.color.rgb == "FFC00000", c.coordinate
            assert "Plug load assumed:" in rem and "Lighting assumed:" in rem and "No project value. To confirm." in rem
            n_red += 1
    assert n_red > 0
    # equipment 0 with a VERIFY flag on an equipment-led room
    fl = [r for r in wb["Flags"].iter_rows(min_row=7, values_only=True) if r[0] and str(r[3]).startswith("Room ") and ("Equipment gain is 0 W" in str(r[3]) or "provisional" in str(r[3]))]
    assert fl and all(r[1] == "VERIFY" for r in fl)


@needs_outputs
def test_no_occupants_only_wording_anywhere():
    pat = re.compile(r"OCCUPANTS ONLY|occupants only|no approved value", re.I)
    for f in list(WB.glob("*.xlsx")) + [ROOT / "out" / "zoning" / "zone_schedule.xlsx"]:
        for title, v in _all_text(f):
            assert not pat.search(v), (f.name, title, v[:80])


def test_thermal_airflow_includes_the_loads_in_both_engines():
    """Lighting and small power are in the sensible load of the workbook engine and of the recomputation."""
    from src.recompute import thermal as th
    import inspect
    src = inspect.getsource(th.room_loads)
    assert "lighting_w_m2" in src and "small_power_w_m2" in src and "sensible = (people_sens + lighting + power" in src
    from src.workbook.sheets_rooms import ENG_F
    assert "{lgt_w}" in ENG_F["sens"] and "{pwr_w}" in ENG_F["sens"]


# D-026 -------------------------------------------------------------------------------------------------------------
@pytest.mark.skipif(not (ROOT / "out" / "registers" / "criteria.csv").exists(), reason="registers not present")
def test_presumed_criteria_from_the_closest_other_department():
    from src.workbook.assemble import room_use
    from src.workbook.linking import split_code
    from src.workbook.presume import Presumption
    P = Presumption(ROOT, room_use, split_code)
    hit, why = P.find("43", "Office")
    assert hit and hit["department"] != "43" and "same name" in hit["how"]
    hit, why = P.find("04", "Toilet - Patient")
    assert hit and int(hit["department"]) <= 28                           # a clinical room prefers a clinical department
    hit, why = P.find("04", "Foo")
    assert hit is None and "no other department" in why
    hit, why = P.find("04", "PTS Plant Room")                             # a plant room takes no row from its use class
    assert hit is None and "plant" in why


@needs_outputs
def test_presumed_criteria_style_and_remark():
    wb = _wb("43")
    ws = wb["Criteria"]
    hdr = [c.value for c in ws[5]]
    n = 0
    for row in ws.iter_rows(min_row=7):
        if not row[0].value:
            continue
        rem = row[hdr.index("Remark")].value or ""
        if rem.startswith("Presumed criteria: taken from"):
            crit = row[hdr.index("Criteria row (as printed)")]
            assert crit.font.i and crit.font.color.rgb.endswith("00B050")          # italic green (the named style Presumed-Criteria)
            assert row[hdr.index("Code")].value == "A" and "booklet p" in rem and "No row in this department. To confirm." in rem
            n += 1
    assert n > 0


@needs_outputs
def test_every_room_with_a_use_has_criteria_or_a_listed_reason():
    import json
    for f in sorted(INP.glob("D*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        for r in d["rooms"]:
            if not r["criteria"]:
                assert "No criteria row:" in r["gaps"].get("criteria", ""), (f.name, r["number"])


# D-027 -------------------------------------------------------------------------------------------------------------
@needs_outputs
def test_master_total_row_adds_only_rows_with_a_stated_reduction():
    wb = openpyxl.load_workbook(WB / "AHU_master_summary.xlsx", data_only=True)
    ws = wb["AHU Master"]
    hdr = {c.value: i for i, c in enumerate(ws[5])}
    rows = [r for r in ws.iter_rows(min_row=7, values_only=True) if r[0]]
    body = [r for r in rows if not str(r[0]).startswith("TOTAL")]
    total = next(r for r in rows if r[0] == "TOTAL")
    stated = [r for r in body if isinstance(r[hdr["Reduction = tender − calculated"]], (int, float))]
    assert stated
    for k in ("Tender airflow", "Reduction = tender − calculated", "Calculated airflow"):
        assert total[hdr[k]] == pytest.approx(sum(r[hdr[k]] for r in stated), abs=1.0), k
    assert total[hdr["Tender airflow"]] - total[hdr["Calculated airflow"]] == pytest.approx(total[hdr["Reduction = tender − calculated"]], abs=1.0)
    for r in body:                                        # a reduction only where calculated < tender and the room list is complete
        red = r[hdr["Reduction = tender − calculated"]]
        if isinstance(red, (int, float)):
            assert r[hdr["Calculated airflow"]] < r[hdr["Tender airflow"]] and str(r[hdr["Room list of the unit (rooms counted)"]]).startswith("Complete")


@needs_outputs
def test_above_tender_is_verify_in_master_and_counted_by_the_check():
    wb = openpyxl.load_workbook(WB / "AHU_master_summary.xlsx", data_only=True)
    ws = wb["AHU Master"]
    hdr = {c.value: i for i, c in enumerate(ws[5])}
    n_sup = n_oa = 0
    for r in ws.iter_rows(min_row=7, values_only=True):
        if not r[0] or str(r[0]).startswith("TOTAL"):
            continue
        calc, ten = r[hdr["Calculated airflow"]], r[hdr["Tender airflow"]]
        if isinstance(ten, (int, float)) and calc > ten:
            n_sup += 1
            assert str(r[hdr["Against tender"]]).startswith("VERIFY"), r[0]
        toa, oa = r[hdr["Tender outdoor airflow"]], r[hdr["AHU outdoor air = fraction × calculated airflow"]]
        if isinstance(toa, (int, float)) and oa > toa:
            n_oa += 1
            assert str(r[hdr["Outdoor air against tender"]]).startswith("VERIFY"), r[0]
    checks = {r[1]: r[2] for r in wb["Checks"].iter_rows(min_row=7, values_only=True) if r[1]}
    assert checks["AHU rows whose calculated airflow is above tender"] == n_sup
    assert checks["AHUs with outdoor air above tender outdoor air"] == n_oa


@needs_outputs
def test_no_partial_or_no_reduction_wording_in_remarks():
    pat = re.compile(r"partial|No reduction is stated|PARTIAL ROOM LIST", re.I)
    for f in list(WB.glob("*.xlsx")) + [ROOT / "out" / "zoning" / "zone_schedule.xlsx"]:
        for title, v in _all_text(f):
            assert not (pat.search(v) and "partial cover" not in v.lower()), (f.name, title, v[:80])


# D-028 -------------------------------------------------------------------------------------------------------------
@needs_outputs
def test_pressure_tab_calculates_every_room_with_a_target_and_no_door_wording():
    import json
    for num in ("11", "22", "30"):
        d = json.loads((INP / f"D{num}.json").read_text(encoding="utf-8"))
        wb = _wb(num)
        ws = wb["Pressure"]
        text = " ".join(str(c.value) for row in ws.iter_rows() for c in row if isinstance(c.value, str)).lower()
        assert "no door data" not in text and "door gaps:" not in text
        nodes = {r[0] for r in ws.iter_rows(min_row=7, values_only=True) if r[0]}
        def _pa(r):
            return next((r["criteria"][k] for k in ("pressure_pa_tender", "pressure_pa_working", "pressure_pa_booklet") if r["criteria"].get(k) is not None), None)
        with_pa = [r["number"] for r in d["rooms"] if r["criteria"] and _pa(r) not in (None, 0)]          # a neutral room (0 Pa) has no Pressure row
        neutral = [r["number"] for r in d["rooms"] if r["criteria"] and _pa(r) == 0]
        assert not any(n in nodes for n in neutral), num
        assert with_pa and all(n in nodes for n in with_pa), num
        assert not any(r["gaps"].get("door") for r in d["rooms"])
        flags = " ".join(str(r[3]) for r in wb["Flags"].iter_rows(min_row=7, values_only=True) if r[0]).lower()
        assert "door data" not in flags
    # a room with a pressure row: offset needed = envelope leakage at the set pressure (n = 0.65), design offset = 2.5 x surface
    wb = _wb("30")
    ws = wb["Pressure"]
    hdr = {}
    for i, c in enumerate(ws[5]):
        hdr.setdefault(c.value, i)
    for r in ws.iter_rows(min_row=7, values_only=True):
        if r[hdr["Type"]] == "ROOM" and isinstance(r[hdr["Design offset"]], (int, float)):
            assert r[hdr["Design offset"]] == pytest.approx(2.5 * r[hdr["Internal surface"]], rel=1e-6)
            assert r[hdr["Offset needed"]] == pytest.approx(2.5 * r[hdr["Internal surface"]] * (abs(r[hdr["Set pressure"]]) / 50.0) ** 0.65, rel=1e-3)
            break
    else:
        pytest.fail("no pressure room in D30")


# R3-02 -------------------------------------------------------------------------------------------------------------
def test_area_plausibility_refuses_a_link_from_a_much_smaller_plan_room():
    from src.workbook.linking import plan_links
    zone = {"class": "AHU-X-01", "class_kind": "AHU", "ahu": "AHU-X-01"}
    mk = lambda no, area: dict(number=no, name="Office", storey="L02", dpa_area=area, ref_area=area, typical_count=1)
    plan = [dict(storey="L02", name="Office", area=5.0, zone=zone, shared=False, poly=dict(room_id="L02-R001"))]
    score = lambda a, b: 1.0 if a.lower() == b.lower() else 0.0
    rej: list = []
    assert plan_links([mk("A 01", 15.0)], plan, set(), score, ("ahu",), {"AHU"}, max_area_ratio=2.0, rejected=rej) == {}
    assert rej and rej[0]["basis"] == "name and count" and rej[0]["plan_area"] == 5.0
    ok = plan_links([mk("A 01", 8.0)], plan, set(), score, ("ahu",), {"AHU"}, max_area_ratio=2.0, rejected=[])
    assert ok["A 01"]["tag"] == "AHU-X-01"
    three = plan_links([mk("A 01", 15.0), mk("A 02", 15.0), mk("A 03", 15.0)], plan * 3, set(), score, ("ahu",), {"AHU"}, max_area_ratio=2.0, rejected=[])
    assert three == {}                                                 # three rooms of 15 m2 (45 m2) against three plan rooms of 5 m2 (15 m2)


# R3-05 -------------------------------------------------------------------------------------------------------------
def test_ucv_note_text_and_unit():
    from src.workbook.tag_notes import unit_note
    t = unit_note("AHU-B-07-05MOTCT-01")
    assert "5,320 L/s" in t and "future UCV" in t and "IP-BH-16057" in t and unit_note("AHU-B-07-05MOTE-01A/B") == ""


@needs_outputs
def test_ucv_note_in_d22_remarks_master_zone_schedule_and_open_items():
    wb = _wb("22")
    ws = wb["AHU"]
    hdr = [c.value for c in ws[5]]
    rem = next(r[hdr.index("Remarks")] for r in ws.iter_rows(min_row=7, values_only=True) if r[0] == "AHU-B-07-05MOTCT-01")
    assert "5,320 L/s" in rem
    sch = openpyxl.load_workbook(ROOT / "out" / "zoning" / "zone_schedule.xlsx", data_only=True)["AHU summary"]
    h2 = [c.value for c in sch[1]]
    assert any(r[0] == "AHU-B-07-05MOTCT-01" and "5,320 L/s" in str(r[h2.index("Remarks")]) for r in sch.iter_rows(min_row=2, values_only=True))
    assert "5,320 L/s" in (ROOT / "out" / "qa" / "open_items_for_marcus.md").read_text(encoding="utf-8")


# R3-06 / R3-09 -----------------------------------------------------------------------------------------------------
@needs_outputs
def test_unit_names_in_d22_remarks_equal_the_unit_of_the_room():
    import json
    d = json.loads((INP / "D22.json").read_text(encoding="utf-8"))
    rx = re.compile(r"AHU-B-\d\d-\d\d[0-9A-Z]+-\d+[AB]?(?:/B)?")
    for r in d["rooms"]:
        for text in (r["remark"], (r["criteria"] or {}).get("remark", "")):
            for tag in set(rx.findall(text)):
                assert r["ahu"] and tag.replace("/B", "") in r["ahu"].replace("/B", ""), (r["number"], tag, r["ahu"])


@needs_outputs
def test_mothyb_02_is_listed_and_not_used_in_workbook_master_and_zone_schedule():
    """D-059: AHU-B-07-05MOTHYB-02 is a variant tag. It has no unit row in D22, the master or the zone schedule; D22 Flags list it."""
    tag = "AHU-B-07-05MOTHYB-02"
    ws = _wb("22")["AHU"]
    assert not [r for r in ws.iter_rows(min_row=7, values_only=True) if r[0] == tag]
    fl = _wb("22")["Flags"]
    assert any(tag in str(r[2]) and "not used" in str(r[3]) for r in fl.iter_rows(min_row=5, values_only=True))
    m = openpyxl.load_workbook(WB / "AHU_master_summary.xlsx", data_only=True)["AHU Master"]
    assert not [r for r in m.iter_rows(min_row=7, values_only=True) if r[0] == tag]
    z = openpyxl.load_workbook(ROOT / "out" / "zoning" / "zone_schedule.xlsx", data_only=True)["AHU summary"]
    assert not [r for r in z.iter_rows(min_row=2, values_only=True) if r[0] == tag]


# R3-07 -------------------------------------------------------------------------------------------------------------
@pytest.mark.skipif(not (ROOT / "out" / "qa" / "open_items_for_marcus.md").exists(), reason="open items not present")
def test_open_items_settled_closed_and_conflicts_listed():
    text = (ROOT / "out" / "qa" / "open_items_for_marcus.md").read_text(encoding="utf-8")
    head, _, tail = text.partition("## Settled by decisions D-025 to D-028")
    open_section = head
    for oid in ("OI-04-1", "OI-04-2", "OI-04-3", "OI-04-4", "OI-R103-1"):
        assert not re.search(rf"\*\*{oid} ", open_section), oid
        assert oid in tail
    assert "**D-021 " not in open_section and "D-021" in tail
    assert "Common areas of LB3, B3, B2, B1 and L01 are outside Appendix D" in open_section
    rows = list(csv.DictReader(open(ROOT / "out" / "registers" / "conflicts.csv", encoding="utf-8")))
    n_open = sum(1 for r in rows if r["status"].strip().lower().startswith("open") or "open on" in r["status"].lower() or r["status"].strip().lower() == "open")
    m = re.search(r"## Open conflicts between documents \((\d+)\)", text)
    assert m and int(m.group(1)) == n_open


# R3-08 -------------------------------------------------------------------------------------------------------------
@needs_outputs
def test_no_reduction_status_for_a_room_without_criteria():
    for num in ("04", "45"):
        wb = _wb(num)
        ws = wb["Airflow Comparison"]
        hdr = [c.value for c in ws[5]]
        crit = {r[0]: r[2] for r in wb["Criteria"].iter_rows(min_row=7, values_only=True) if r[0]}
        for r in ws.iter_rows(min_row=7, values_only=True):
            if r[0] and r[hdr.index("Against tender")] == "REDUCTION AGAINST TENDER":
                assert crit.get(r[0]), (num, r[0])


# round 4, pass 1 ----------------------------------------------------------------------------------------------------
needs_registers = pytest.mark.skipif(not (ROOT / "out" / "registers" / "criteria.csv").exists(), reason="registers not present")


@needs_registers
def test_own_booklet_row_is_used_before_any_presumption():
    from src.workbook.assemble import room_use, _expand_abbreviations
    from src.workbook.linking import split_code
    from src.workbook.presume import Presumption
    P = Presumption(ROOT, room_use, split_code, _expand_abbreviations)
    own = P.find_own("47", "Office - Typical", "L06", "Admin Workspace")
    assert own and own["row"]["room_type_as_printed"] == "L6 Admin Workspace Office" and own["row"]["booklet_page"] == "293"
    own = P.find_own("47", "Office - Typical", "L07", "Admin Workspace")
    assert own and own["row"]["room_type_as_printed"] == "L7 Admin Workspace Office"
    own = P.find_own("50", "Lecture Room", "L10", "TRED Learning Centre")
    assert own and own["row"]["room_type_as_printed"] == "Lecture Room (64)"
    assert P.find_own("04", "Foo Bar", "L04", "ICU") is None


@needs_registers
def test_use_class_alone_is_not_the_same_space():
    from src.workbook.assemble import room_use, _expand_abbreviations
    from src.workbook.linking import split_code
    from src.workbook.presume import Presumption
    P = Presumption(ROOT, room_use, split_code, _expand_abbreviations)
    for name in ("Space for Roll Call", "Bay - Resus Trolley x", "Scrub Up"):
        hit, why = P.find("30", name)
        assert hit is None, (name, hit and hit["row"]["room_type_as_printed"])
    hit, why = P.find("04", "Patient Bay - Holding, Recliner, 6 sqm")
    assert hit is None or "NP Isolation" not in hit["row"]["room_type_as_printed"]
    # a row at negative pressure or on single pass is not taken by a room whose name does not say toilet, dirty or isolation
    for name in ("Store - Consumables, Anaesthetics", "Bay - Linen", "Staff Accom"):
        hit, why = P.find("22", name)
        if hit:
            row = hit["row"]
            assert not (row["single_pass"].upper().startswith("Y") or "NEG" in row["pressure_regime_as_printed"].upper()), (name, row["room_type_as_printed"])


@needs_outputs
def test_d47_rooms_use_their_own_rows_and_the_workbook_says_so():
    ws = _wb("47")["Criteria"]
    hdr = [c.value for c in ws[5]]
    n_own = n_pres = 0
    for r in ws.iter_rows(min_row=7, values_only=True):
        if not r[0]:
            continue
        rem = r[hdr.index("Remark")] or ""
        if "of the booklet of this department, mapped to the room type" in rem:
            n_own += 1
            assert "Admin Workspace" in r[hdr.index("Criteria row (as printed)")] and r[hdr.index("Code")] == "A"
        if rem.startswith("Presumed criteria") and r[1] in ("Office - Typical", "Office - Type B"):
            n_pres += 1
        assert "same name" not in rem or "same space" not in rem
    assert n_own >= 40 and n_pres == 0


@needs_registers
def test_outdoor_air_rates_are_read_from_the_printed_text():
    import csv as _csv
    from src.workbook.assemble import criteria_from_row
    rows = list(_csv.DictReader(open(ROOT / "out" / "registers" / "criteria.csv", encoding="utf-8")))
    mmd = next(r for r in rows if r["room_type_as_printed"] == "MMD Office")
    c = criteria_from_row(mmd, "X", "same name")
    assert c["oa_lps_per_m2"] == 0.6 and "Outdoor air as printed: 0.6 L/s per m²" in c["remark"]
    lec = next(r for r in rows if r["room_type_as_printed"] == "Lecture Room (64)")
    assert criteria_from_row(lec, "X", "same name")["oa_lps_per_person"] == 10
    both = next(r for r in rows if "whichever higher" in r["raw_cells"])
    c = criteria_from_row(both, "X", "same name")
    assert c["oa_lps_per_person"] == 10 and c["oa_lps_per_m2"] == 0.6


@needs_outputs
def test_outdoor_air_of_rooms_with_a_rate_is_not_zero():
    ws = _wb("31")["Airflow"]
    hdr = [c.value for c in ws[5]]
    rows = [r for r in ws.iter_rows(min_row=7, values_only=True) if r[0] and r[hdr.index("Final supply")]]
    assert rows
    cr = _wb("31")["Criteria"]
    ch = [c.value for c in cr[5]]
    per_area = {r[0] for r in cr.iter_rows(min_row=7, values_only=True) if r[0] and r[ch.index("Outdoor air per area")]}
    assert per_area
    zero = [r[0] for r in rows if r[0] in per_area and not r[hdr.index("Outdoor air")]]
    assert not zero, zero[:5]


@needs_outputs
def test_no_double_stop_double_space_or_zero_width_in_any_workbook():
    zw = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")
    for f in list(WB.glob("*.xlsx")) + [ROOT / "out" / "zoning" / "zone_schedule.xlsx"]:
        for title, v in _all_text(f):
            assert not zw.search(v), (f.name, title, v[:60])
            assert not re.search(r"(?<![.\d])\.\.(?!\.)", v), (f.name, title, v[:80])
            assert "  " not in v, (f.name, title, v[:80])


@needs_outputs
def test_no_door_wording_in_pressure_texts():
    for f in WB.glob("*.xlsx"):
        for title, v in _all_text(f):
            low = v.lower()
            assert "internal doors cancel" not in low and "door-gap" not in low and "provisional gap areas" not in low, (f.name, title, v[:80])


@needs_outputs
def test_shared_unit_is_not_stated_as_reduced_in_a_department_tab():
    """A unit that other departments also serve is judged on the master only: no department tab states a reduction for it (D-027)."""
    for f in WB.glob("AIGH_C2_D*_RevA.xlsx"):
        ws = openpyxl.load_workbook(f, data_only=True)["AHU"]
        hdr = [c.value for c in ws[5]]
        for r in ws.iter_rows(min_row=7, values_only=True):
            if not r[0] or r[0] == "TOTAL" or str(r[0]).startswith("B. "):
                continue
            oth = r[hdr.index("Rooms of other departments on this unit")]
            if isinstance(oth, (int, float)) and oth > 0:
                assert str(r[hdr.index("Room list of the unit (rooms counted)")]).startswith("Not complete"), (f.name, r[0])
                assert "Reduced" not in str(r[hdr.index("Remarks")]), (f.name, r[0])
                assert r[hdr.index("Reduction = tender − calculated supply")] in (None, ""), (f.name, r[0])


@needs_outputs
def test_completeness_text_gives_a_reason_and_is_grammatical():
    for f in WB.glob("AIGH_C2_D*_RevA.xlsx"):
        ws = openpyxl.load_workbook(f, data_only=True)["AHU"]
        hdr = [c.value for c in ws[5]]
        for r in ws.iter_rows(min_row=7, values_only=True):
            t = str(r[hdr.index("Room list of the unit (rooms counted)")] or "")
            assert "all 1 zoned rooms" not in t and "1 of 1 zoned rooms" not in t, (f.name, r[0], t)
            if re.match(r"Not complete: (the zoned room is counted|all \d+ zoned rooms are counted)$", t):
                pytest.fail(f"no reason: {f.name} {r[0]} {t}")


def test_comparison_does_not_tag_an_open_rule_as_a_cause():
    import src.recompute.compare_workbooks as cw
    assert not hasattr(cw, "CAUSE_OFFSET") and not hasattr(cw, "CAUSE_EXH")
    # D-030 and D-032 are decided: there is one reading of each rule in both engines (the interim switch is gone)
    from src.recompute.thermal import WORKBOOK_RULES
    assert WORKBOOK_RULES == {}
