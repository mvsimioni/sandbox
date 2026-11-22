"""Tests for src.schematic_parser: pure helpers on synthetic data, plus the IP-BH-05010
pilot sheet (both issues) when the source PDFs are present."""
from __future__ import annotations

import csv
import warnings
from pathlib import Path

import pytest

from src import schematic_parser as sp

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parents[1]
PILOT_SNAPSHOT = ROOT / "out" / "qa" / "evidence" / "schematic_pilot_rooms.csv"


def L(text, x0, y0, size=10.0):
    return {"x0": x0, "y0": y0, "x1": x0 + 5 * len(text), "y1": y0 + 10, "text": text, "dir": (1.0, 0.0), "size": size}


# ---------------------------------------------------------------- helpers
def test_parse_airflow_keeps_text_and_flags_odd_lines():
    air = sp.parse_airflow([L("SA 705 l/s", 0, 0), L("RA 625 l/s", 0, 10), L("OA 705 l/s", 0, 20), L("KMA 420 L/s", 0, 30)])
    assert air["supply"] == 705 and air["return"] == 625 and air["exhaust"] is None
    assert air["oa_text"] == "OA 705 l/s"
    assert air["as_printed"][0] == "SA 705 l/s"
    assert air["other"] == ["KMA 420 L/s"]


def test_parse_airflow_dash_is_blank_not_zero():
    air = sp.parse_airflow([L("SA - L/s", 0, 0), L("EA 70 L/s", 0, 10), L("OA 2495tx L/s", 0, 20)])
    assert air["supply"] is None and air["exhaust"] == 70
    assert air["oa_text"] == "OA 2495tx L/s"  # printed typo kept, not interpreted
    assert sp._oa_value(air["oa_text"]) is None


def test_duty_lines_and_primary_flow():
    assert sp._primary_flow("4508 L/s (NORMAL) | 4508 L/s (PANDEMIC)") == 4508
    assert sp._primary_flow("3450 L/s NORMAL/SINGLE PASS | 1750 L/s SETBACK") == 3450
    assert sp._primary_flow("SA: 1012 L/s") == 1012
    assert sp._primary_flow(None) is None
    m = sp.DUTY_RE.match("SA:1012 L/s")
    assert m and m.group(1) == "1012"


def test_storey_codes():
    assert sp.storey_of("IP-BH-05B25_Rev0C_INPATIENT BASEMENT 2 AIR-SIDE SCHEMATIC.pdf") == "B2"
    assert sp.storey_of("INPATIENT 1ST STOREY AIR-SIDE") == "L01"
    assert sp.storey_of("INPATIENT 4TH STOREY AIR-SIDE") == "L04"
    assert sp.storey_of("INPATIENT LOWER BASEMENT 3 MV SCHEMATICS") == "LB3"
    assert sp.storey_of("INPATIENT 12TH STOREY AIR-SIDE") == ""  # outside LB3..L06 (not a 1-6 storey)


def test_tag_stem_pairs_ab_units_and_standby():
    assert sp._stem("AHU-B-02-01EDP3WAIT-01A") == sp._stem("RAF-B-02-01EDP3WAIT-01B") == "B-02-01EDP3WAIT-01"
    assert sp._stem("RAF-B-07-04ICU-06/A") == "B-07-04ICU-06"


def test_capacity_counts_equal_pair_once_adds_different_and_skips_standby():
    flow = {"AHU-B-02-X-01A": 8262.0, "AHU-B-02-X-01B": 8262.0}
    cap, basis, missing = sp._capacity(sorted(flow), flow)
    assert cap == 8262.0 and not missing
    flow = {"AHU-B-04-04MRI-01A": 6160.0, "AHU-B-04-04MRI-01B": 2360.0}
    cap, _, _ = sp._capacity(sorted(flow), flow)
    assert cap == 8520.0
    flow = {"AHU-B-07-04ICU-05": 3070.0, "AHU-B-07-04ICU-06": 3645.0, "AHU-B-07-04ICU-05/06C": 3645.0}
    cap, basis, _ = sp._capacity(sorted(flow), flow)
    assert cap == 6715.0  # common standby -05/06C not added
    cap, _, missing = sp._capacity(["AHU-B-02-Y-01A"], {"AHU-B-02-Y-01A": None})
    assert cap is None and missing


def test_service_components_join_touching_strokes():
    class S:  # minimal Sheet stand-in
        segs = [
            {"x0": 0, "y0": 0, "x1": 100, "y1": 0, "col": (0.5, 1.0, 1.0), "svc": "SA"},
            {"x0": 100, "y0": 0, "x1": 100, "y1": 50, "col": (0.5, 1.0, 1.0), "svc": "SA"},
            {"x0": 500, "y0": 500, "x1": 600, "y1": 500, "col": (0.5, 1.0, 1.0), "svc": "SA"},
            {"x0": 0, "y0": 10, "x1": 90, "y1": 10, "col": (0.35, 1.0, 0.35), "svc": "RA"},
        ]
    comps = sp.service_components(S(), "SA", 10)
    assert sorted(len(c["segs"]) for c in comps) == [1, 2]


def test_tender_vs_working_reports_airflow_change_and_missing_rows():
    def row(iset, name, sa, ra="", ea="", oa=""):
        return {"sheet": "IP-BH-X", "storey": "L01", "issue_set": iset, "room_name_as_written": name, "typical_count": 1, "mode": "NORM",
                "supply_L_s": sa, "return_L_s": ra, "exhaust_L_s": ea, "oa_note": oa, "ahu_tag": "AHU-1", "pressure": "NEUTRAL",
                "air_class": "CLASS 3", "filter": "MERV 15", "temperature": "21-24", "humidity": "<=65%"}
    rows = [row("tender", "POCT", 170, 170), row("working", "POCT", 170, 140, 30), row("tender", "A", 10), row("working", "A", 10),
            row("working", "NEW", 5)]
    out = {r["room_name_as_written"]: r for r in sp.tender_vs_working(rows)}
    assert out["POCT"]["status"] == "airflow_changed"
    assert out["POCT"]["delta_return_L_s"] == -30 and out["POCT"]["delta_exhaust_L_s"] == 30
    assert "RA 170 -> 140" in out["POCT"]["airflow_differences"]
    assert out["A"]["status"] == "identical"
    assert out["NEW"]["status"] == "only_in_working"


def test_discover_scope_and_mot_latest_issue():
    tasks = sp.discover()
    if not tasks:
        pytest.skip("source drawings not present")
    sets = {t["issue_set"] for t in tasks}
    assert {"tender", "working", "mot_nsc"} <= sets
    assert all(t["storey"] in sp.IN_SCOPE_STOREYS for t in tasks if t["issue_set"] != "mot_nsc")
    mot = [t for t in tasks if t["issue_set"] == "mot_nsc"]
    assert all("Tender Corr 2" in t["issue"] or "Tender Corr 3" in t["issue"] for t in mot)
    ids = [t["sheet"] for t in mot]
    assert len(ids) == len(set(ids))  # one issue per sheet


# --------------------------------------------------------------- pilot sheet
@pytest.fixture(scope="module")
def pilot_run():
    tasks = sp.discover(["IP-BH-05010"])
    if len(tasks) != 2:
        pytest.skip("IP-BH-05010 sources not present")
    return sp.run(["IP-BH-05010"], workers=1, write=False)


def test_pilot_sheet_counts_both_issues(pilot_run):
    assert len(pilot_run["rooms"]) == 56
    assert {r["issue"] for r in pilot_run["rooms"]} == {"tender issue Rev 0D", "working drawing"}
    assert len(pilot_run["connections"]) == 238
    assert len([e for e in pilot_run["equipment"] if e["issue"] == "working drawing"]) == 22


def test_pilot_sheet_values(pilot_run):
    rows = [r for r in pilot_run["rooms"] if r["issue"] == "working drawing"]
    poct = [r for r in rows if r["room_name_as_written"] == "P3 POCT"][0]
    assert (poct["supply_L_s"], poct["return_L_s"], poct["exhaust_L_s"]) == (170, 140, 30)
    assert poct["oa_note"] == "OA 68 L/s" and poct["air_class"] == "CLASS 3" and poct["temperature"] == "21-24°C"
    assert poct["storey"] == "L01" and poct["issue_set"] == "working"
    assert poct["ahu_tag"] == "AHU-B-02-01EDP3WAIT-01A; AHU-B-02-01EDP3WAIT-01B"
    tri = [r for r in rows if r["room_name_as_written"] == "P3 TRIAGE ROOM"][0]
    assert tri["typical_count"] == 4 and tri["isolation_exhaust_target"] == "BIBO-B-02-01EDFEVER"


def test_pilot_sheet_ahu_totals_confirm_leakage_allowance(pilot_run):
    tot = [t for t in pilot_run["totals"] if t["issue_set"] == "working"]
    assert {t["check_result"] for t in tot} == {"ok"}
    wait = [t for t in tot if t["ahu_tag"] == "AHU-B-02-01EDP3WAIT-01A"][0]
    assert wait["rooms_SA_sum_L_s_with_typicals"] == 7185 and wait["group_capacity_L_s"] == 8262
    assert abs(wait["ratio_ahu_to_rooms"] - 1.15) < 0.01


def test_pilot_sheet_tender_vs_working_finds_the_known_change(pilot_run):
    changed = [r for r in pilot_run["tvw"] if r["status"] == "airflow_changed"]
    assert any(r["room_name_as_written"] == "P3 POCT" and "RA 170 -> 140" in r["airflow_differences"] for r in changed)


def test_pilot_rooms_reproduce_the_hand_extracted_register(pilot_run):
    if not PILOT_SNAPSHOT.exists():
        pytest.skip("pilot snapshot not present")
    with open(PILOT_SNAPSHOT, newline="", encoding="utf-8") as fh:
        pilot = list(csv.DictReader(fh))
    cmp = sp.compare_pilot({"rooms": pilot, "equipment": [], "connections": []}, pilot_run["rooms"], [], [])
    assert cmp["rooms"] == []
    assert cmp["counts"]["rooms"]["identical_rows"] == 56


def test_every_row_keeps_provenance(pilot_run):
    for r in pilot_run["rooms"]:
        assert r["drive_rel_path"].startswith("Basis/") and r["page"] == 1 and r["file_sha256"]
        assert r["box_page_view_table"] and r["method"] and r["confidence"] in ("high", "medium", "low")
        assert r["airflow_as_printed"]


def test_cli_run_no_write(capsys):
    if not sp.discover(["IP-BH-05010"]):
        pytest.skip("sources not present")
    assert sp.main(["run", "--sheets", "IP-BH-05010", "--workers", "1", "--no-write"]) == 0
    out = capsys.readouterr().out
    assert "sheets 2 parsed of 2" in out and "rooms 56" in out


# ------------------------------------------------- fixes after verification
def test_parse_airflow_unit_variants_and_multiplier_are_flagged_not_hidden():
    air = sp.parse_airflow([L("SA 150/s", 0, 0), L("RA 140 x 2 L/s", 0, 10), L("EA 1005L/s", 0, 20), L("KEA 1340 L/s", 0, 30), L("KMA 1204 L/s", 0, 40)])
    assert air["supply"] == 150 and air["return"] == 140 and air["exhaust"] == 1005
    assert any("multiplier" in f for f in air["flags"]) and "SA 150/s" in air["flags"]
    assert air["other"] == ["KEA 1340 L/s", "KMA 1204 L/s"]  # EA already printed: KEA kept as printed, not resolved
    only_k = sp.parse_airflow([L("SA 136 L/s", 0, 0), L("KEA 1340 L/s", 0, 10)])
    assert only_k["exhaust"] == 1340


def test_arrow_labels_include_kitchen_duct_tags():
    assert sp.ARROW_RE.match("KEA") and sp.ARROW_RE.match("KMA")  # duct labels above a box, not room names


def test_split_multi_mode_cuts_joined_line_at_flow_keys():
    sh = type("S", (), {})()
    sh.words = [{"x0": 254.6, "x1": 265.6, "y0": 781.3, "y1": 795.3, "text": "SA"}, {"x0": 267.8, "x1": 281.5, "y0": 781.3, "y1": 795.3, "text": "210"},
                {"x0": 283.8, "x1": 294.7, "y0": 781.3, "y1": 795.3, "text": "L/s"}, {"x0": 296.6, "x1": 307.6, "y0": 781.3, "y1": 795.3, "text": "SA"},
                {"x0": 309.8, "x1": 336.7, "y0": 781.3, "y1": 795.3, "text": "210L/s"}]
    joined = {"x0": 254.6, "x1": 336.7, "y0": 781.3, "y1": 795.3, "text": "SA 210 L/s SA 210L/s", "dir": (1.0, 0.0), "size": 10.0}
    out = sp._split_multi_mode(sh, [joined], "airflow", 770, 800)
    assert [o["text"] for o in out] == ["SA 210 L/s", "SA 210L/s"]
    assert out[0]["x1"] < out[1]["x0"]
    # glued unit/key word: '1005L/sRA'
    sh.words = [{"x0": 0, "x1": 10, "y0": 0, "y1": 10, "text": "RA"}, {"x0": 12, "x1": 52, "y0": 0, "y1": 10, "text": "1005L/sRA"}, {"x0": 54, "x1": 80, "y0": 0, "y1": 10, "text": "1005L/s"}]
    j2 = {"x0": 0, "x1": 80, "y0": 0, "y1": 10, "text": "RA 1005L/sRA 1005L/s", "dir": (1.0, 0.0), "size": 10.0}
    assert [o["text"] for o in sp._split_multi_mode(sh, [j2], "airflow", -5, 15)] == ["RA 1005L/s", "RA 1005L/s"]
    # a line with one key, or outside the airflow row, is untouched
    one = {"x0": 0, "x1": 30, "y0": 0, "y1": 10, "text": "SA 100 L/s", "dir": (1.0, 0.0), "size": 10.0}
    assert sp._split_multi_mode(sh, [one], "airflow", -5, 15) == [one]
    assert sp._split_multi_mode(sh, [j2], "pressure", -5, 15) == [j2]


def _icu_rows(sheet, issue_set="tender"):
    tasks = [t for t in sp.discover([sheet]) if t["issue_set"] == issue_set]
    if not tasks:
        pytest.skip("%s sources not present" % sheet)
    return sp.run([sheet], workers=1, write=False)["rooms"], tasks


def test_icu_two_mode_columns_each_keep_their_own_flows_and_oa():
    rooms, _ = _icu_rows("IP-BH-06045")
    rows = [r for r in rooms if r["issue_set"] == "tender" and r["room_name_as_written"] == "ICU BED 04.02.07"]
    by = {r["mode"]: r for r in rows}
    assert (by["NORM"]["supply_L_s"], by["NORM"]["exhaust_L_s"], by["NORM"]["oa_note"]) == (210, 130, "OA 65 L/s")
    assert (by["PAND"]["supply_L_s"], by["PAND"]["exhaust_L_s"], by["PAND"]["oa_note"]) == (210, 285, "OA 210 L/s")
    assert "joined in one text line" in by["PAND"]["method"]


def test_no_row_prints_a_figure_but_leaves_its_field_blank_on_two_mode_sheets():
    import re
    for sheet in ("IP-BH-06040", "IP-BH-06043"):
        rooms, _ = _icu_rows(sheet)
        for r in rooms:
            for k, f in (("SA", "supply_L_s"), ("RA", "return_L_s"), ("EA", "exhaust_L_s")):
                if re.search(r"\b%s\s*\d" % k, r["airflow_as_printed"]):
                    assert r[f] != "", (sheet, r["room_name_as_written"], r["mode"], r["airflow_as_printed"])
            assert len(re.findall(r"\bOA\s*[\d.]+", r["oa_note"])) <= 1


def test_kitchen_boxes_are_named_boh_and_kitchen_exhaust_is_read():
    rooms, _ = _icu_rows("IP-BH-05027")
    kit = [r for r in rooms if r["issue_set"] == "tender" and "KEA" in r["airflow_as_printed"]]
    assert kit and all(r["room_name_as_written"] == "BOH" for r in kit)
    assert all(r["exhaust_L_s"] == 1340 and "KMA" in r["airflow_unparsed"] for r in kit)
    assert not [r for r in rooms if r["room_name_as_written"] in ("KEA KMA", "KEA", "KMA")]


def test_mot_future_columns_become_two_mode_rows_with_the_room_name():
    rooms, _ = _icu_rows("IP-BH-16041", "mot_nsc")
    rows = [r for r in rooms if r["room_name_as_written"] == "STAFF REST (FUTURE STERILE PREP)"]
    by = {r["mode"]: r for r in rows}
    assert set(by) == {"NORMAL", "FUTURE"}
    assert (by["NORMAL"]["supply_L_s"], by["NORMAL"]["return_L_s"], by["NORMAL"]["oa_note"]) == (90, 90, "OA 55 L/s")
    assert (by["FUTURE"]["supply_L_s"], by["FUTURE"]["return_L_s"], by["FUTURE"]["oa_note"]) == (220, 155, "OA 155 L/s")
    assert not [r for r in rooms if r["room_name_as_written"] == "NORMAL FUTURE"]


def test_family_check_counts_rooms_without_a_supply_value():
    res = {"ok": True, "task": {"issue_set": "tender", "issue": "x", "sheet": "S"}, "equip_rows": [
        {"type": "AHU", "tag": "AHU-1", "duty_as_written": "1000 L/s", "duty_standby": ""}],
        "room_rows": [
        {"ahu_tag": "AHU-1", "mode": "NORM", "supply_L_s": 500, "typical_count": 1, "oa_note": "", "services_drawn": "SA/RA", "airflow_as_printed": "SA 500 L/s"},
        {"ahu_tag": "AHU-1", "mode": "NORM", "supply_L_s": "", "typical_count": 1, "oa_note": "", "services_drawn": "SA/RA", "airflow_as_printed": "SA 300 x 2 L/s"}]}
    fam = sp.aggregate_families([res])
    assert fam[0]["rooms_without_supply_figure"] == 1 and "incomplete" in fam[0]["check_note"]


def test_full_row_compare_reports_provenance_columns(pilot_run):
    if not PILOT_SNAPSHOT.exists():
        pytest.skip("pilot snapshot not present")
    pilot = sp.snapshot_pilot()
    fr = sp.full_row_compare(pilot, pilot_run["rooms"], pilot_run["equipment"], pilot_run["connections"])
    assert fr["rooms"]["paired"] == 56 and fr["rooms"]["identical_excluding_provenance"] == 56
    assert fr["rooms"]["identical_all_columns"] == 0          # method text differs on every row: disclosed, not hidden
    assert fr["rooms"]["col_differs"]["method"] == 56 and fr["rooms"]["services_order_only"] == 44
    assert fr["connections"]["identical_excluding_provenance"] == 238
    assert fr["equipment"]["identical_excluding_provenance"] == 36


def test_pilot_baseline_values_are_on_the_page(pilot_run):
    if not PILOT_SNAPSHOT.exists():
        pytest.skip("pilot snapshot not present")
    bl = sp.verify_pilot_baseline(sp.snapshot_pilot(), sp.discover(["IP-BH-05010"]))
    assert bl["rows_checked"] == 56 and bl["rows_confirmed"] == 56 and bl["not_confirmed"] == []
