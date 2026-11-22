"""Links of DPA rooms to RDS, RLS ceiling heights and plan rooms (job match)."""
import csv
from pathlib import Path

import pytest

from src.workbook.assemble import assemble_department, criteria_text, name_score
from src.workbook.linking import (code_key, link_rds, link_rls, name_key, plan_links, rls_height_m, split_code)
from src.workbook.selftest import generator_text_scan, method_column_scan, scan_strings
from test_workbook_assemble import fake_root  # noqa: F401  (fixture)


def rds(code, name, occ="", date="SEP 2026", f="x.pdf"):
    return dict(room_code=code, room_name=name, occupancy=occ, date=date, revision="DD-SIGN-OFF", rel_path=f)


def test_code_parts_and_keys():
    assert split_code("054_ST-T_P1C0") == ("054", "ST-T", "P1C0")
    assert split_code("092B_DCNA _S") == ("092B", "DCNA", "S")
    assert split_code("MNPK-B") == ("", "MNPK-B", "")
    assert code_key("-") == "" and code_key("") == ""
    assert name_key("Manual Picking I Repackaging Area") == name_key("Manual Picking | Repackaging Area")


def test_rds_link_by_code_variant_name_and_unlinked_both_sides():
    dpa = [dict(number="A1", name="Pantry", code="205_PTRY_P1C7"), dict(number="A2", name="Pantry", code="205_PTRY_P1C9"),
           dict(number="A3", name="Meeting Room - Large", code="OFF-OSS"), dict(number="A4", name="Kiosk", code="134_PHAK _S"),
           dict(number="A5", name="No code", code=""), dict(number="A6", name="Store", code="STO")]
    rows = [rds("205_PTRY_P1C7", "Lounge", "2 staff"), rds("205_PTRY_P1C8", "Lounge", "3 staff"), rds("190_MTRM-L_P1C7", "Meeting Room - Large", "6"),
            rds("134_PHAK_S", "Pharmacy Kiosk", "n/a"), rds("", "No code", "1"), rds("ZZZ", "Orphan"), rds("STO", "Store 1"), rds("STO2", "Store 1")]
    r = link_rds(dpa, rows)
    assert r["links"]["A1"]["basis"] == "code and variant"
    assert "A2" not in r["links"] and "P1C8" in next(u for u in r["unlinked_dpa"] if u["number"] == "A2")["reason"]   # other variant is not a link
    assert r["links"]["A3"]["basis"] == "name within the department" and r["links"]["A3"]["code"] == "190_MTRM-L_P1C7"
    assert r["links"]["A4"]["basis"] == "code and variant"                                  # spaces inside the code are ignored
    assert r["links"]["A5"]["basis"] == "name within the department"                      # a blank code never matches a blank code; the name does
    assert r["links"]["A6"]["basis"] == "code"
    assert {u["code"] for u in r["unlinked_rds"]} >= {"ZZZ", "205_PTRY_P1C8", "STO2"}


def test_rds_ambiguous_name_not_linked_and_issue_conflicts_not_resolved():
    r = link_rds([dict(number="B1", name="Waiting", code="")], [rds("W1", "Waiting", "1"), rds("W2", "Waiting", "2")])
    assert "B1" not in r["links"] and "several RDS codes" in r["unlinked_dpa"][0]["reason"]
    r = link_rds([dict(number="B1", name="X", code="C1")], [rds("C1", "X", "1 staff"), rds("C1", "X", "4 staff", f="y.pdf")])
    assert r["links"]["B1"]["resolved"]["conflict"] == ["1 staff", "4 staff"]               # same issue, two values: no value is chosen
    r = link_rds([dict(number="B1", name="X", code="C1")], [rds("C1", "X", "", date="SEP 2026"), rds("C1", "X", "4 staff", date="APR 2025")])
    res = r["links"]["B1"]["resolved"]
    assert not res["conflict"] and res["row"]["date"] == "SEP 2026" and "APR 2025" in res["notes"][0]   # the latest issue governs and the older value is named


def test_rls_height_link_by_code_and_conflict():
    assert rls_height_m(dict(ceiling_height_mm="2700")) == 2.7 and rls_height_m(dict(ceiling_height_m="3.0")) == 3.0 and rls_height_m({}) is None
    dpa = [dict(number="R1", name="a", code="054_ST-T_P1C0"), dict(number="R2", name="b", code="OFF-X"), dict(number="R3", name="c", code="Z"), dict(number="R4", name="d", code="")]
    rows = [dict(room_code="054_ST-T_P1C0", ceiling_height_mm="2700", source_file="d/RLS.pdf", page="3"),
            dict(room_code="OFF-X", ceiling_height_mm="2400"), dict(room_code="OFF-X", ceiling_height_mm="3000")]
    r = link_rls(dpa, rows)
    assert r["heights"]["R1"]["height_m"] == 2.7 and r["heights"]["R1"]["basis"] == "code and variant"
    assert r["conflicts"][0]["number"] == "R2" and r["conflicts"][0]["values"] == [2.4, 3.0]
    assert {u["number"] for u in r["unlinked_dpa"]} == {"R3", "R4"}


def test_rls_register_in_assemble_and_flagged_default(fake_root):
    d = assemble_department("77", None, fake_root)
    t = d["rooms"][3]
    assert t["ceiling_height_m"] == 3.0 and t["prov"]["ceiling_height"] == ["A", "ASM-CEILING"]            # no RLS register: flagged default
    assert any("No ceiling heights" in f["text"] for f in d["flags"])
    reg = fake_root / "out" / "registers" / "rls_ceiling_heights.csv"
    with reg.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["room_code", "ceiling_height_mm", "source_file", "page", "source_text"])
        w.writeheader()
        w.writerow(dict(room_code="TOI", ceiling_height_mm="2700", source_file="x/TOI_RLS.pdf", page="2", source_text="CH=2700"))
    d = assemble_department("77", None, fake_root)
    t, o = d["rooms"][3], d["rooms"][0]
    assert t["ceiling_height_m"] == 2.7 and t["prov"]["ceiling_height"][0] == "D" and d["assemble_stats"]["height_from_rls"] == 1
    assert o["ceiling_height_m"] == 3.0 and o["prov"]["ceiling_height"][0] == "A"
    src = next(s for s in d["sources"] if s["id"] == t["prov"]["ceiling_height"][1])
    assert src["text_as_read"] == "CH=2700" and src["method"].startswith("Value read from")


def _plan(name, st, cls, area=10.0, shared=False):
    return dict(storey=st, name=name, area=area, shared=shared, zone={"class": cls, "class_kind": "AHU" if cls.startswith("AHU") else "MV", "department": "D (77)"}, poly={"room_id": name})


def test_plan_links_name_and_count_department_rule_and_never_forced():
    sc = lambda a, b: name_score(a, b, {"mtg": ["meeting room"]})
    dpa = [dict(number="1", name="Meeting Room - Large", storey="L01"), dict(number="2", name="Meeting Room - Medium", storey="L01"),
           dict(number="3", name="Store", storey="L01"), dict(number="4", name="Pantry", storey="L01"), dict(number="5", name="Office", storey="L01")]
    plan = [_plan("Mtg", "L01", "AHU-1"), _plan("Mtg", "L01", "AHU-1"), _plan("Str", "L01", "AHU-1"), _plan("Str", "L01", "AHU-2"),
            _plan("Pantry", "L01", "AHU-3"), _plan("Pantry", "L01", "AHU-3"), _plan("Room", "L01", "AHU-9")]
    out = plan_links(dpa, plan, set(), sc, ("ahu",), ("AHU",))
    assert out["1"]["basis"] == "name and count" and out["1"]["tag"] == "AHU-1" and out["2"]["basis"] == "name and count"
    assert "3" not in out                          # two 'Str' rooms with different AHUs: not forced
    assert out["4"]["basis"] == "department rule" and out["4"]["value"] == "AHU-3"   # one DPA room, two plan rooms, one AHU
    assert "5" not in out                          # no plan room of that name; the generic plan name 'Room' links nothing
    assert plan_links(dpa, plan, {"1", "2", "4"}, sc, ("ahu",), ("AHU",)) == {}


def test_department_rule_needs_the_same_name_not_a_contained_name():
    sc = lambda a, b: name_score(a, b, {})
    dpa = [dict(number="1", name="AGV Dispatch (OT)", storey="L01")]
    plan = [_plan("AGV Dispatch", "L01", "AHU-1"), _plan("AGV Dispatch", "L01", "AHU-1"), _plan("AGV Dispatch", "L01", "AHU-1")]
    assert plan_links(dpa, plan, set(), sc, ("ahu",), ("AHU",)) == {}


def test_assembled_zoning_links_in_fake_root(fake_root):
    d = assemble_department("77", None, fake_root)
    assert d["assemble_stats"]["plan_linked"] >= 2 and "rds_links_by_basis" in d["assemble_stats"]


def test_language_scans_catch_tool_wording():
    assert scan_strings({"a": ["openpyxl read of the cell (formulas kept)"], "b": "config read", "c": "Value read from Rev A workbook, sheet X cell Y"})
    assert len(scan_strings({"a": ["openpyxl read of the cell (formulas kept)"], "b": "config read", "c": "Value read from Rev A workbook, sheet X cell Y"})) == 2
    n, hits = generator_text_scan()
    assert n > 500 and hits == []
    assert "vision" not in criteria_text(dict(room_type_as_printed="X", raw_cells='{"min_oa": "2", "read_by": "vision: page is an image"}'))


def test_method_column_scan(tmp_path):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sources"
    ws.append(["ID", "Method"])
    ws.append(["S1", "openpyxl read of the cell"])
    ws.append(["S2", "Value read from the DPA workbook, sheet Database, row 4"])
    ws.append(["S3", ""])
    wb.save(tmp_path / "s.xlsx")
    n, hits = method_column_scan(tmp_path / "s.xlsx")
    assert n == 3 and len(hits) == 2


def test_rls_register_short_code_with_variant_column_and_conflict_flag():
    rows = [dict(room_type_code="PTRY", variant="P1C7", governing_height_mm="2700", rel_path="a/PTRY RLS.pdf", page="1", height_text_as_printed="CH=2700"),
            dict(room_type_code="CLNR", variant="", governing_height_mm="3000"),
            dict(room_type_code="MTRM", variant="P1C6", governing_height_mm="2700", conflict_flag="two heights printed"),
            dict(room_type_code="STEQ", variant="", governing_height_mm="")]
    dpa = [dict(number="1", name="Pantry", code="205_PTRY_P1C7"), dict(number="2", name="Pantry", code="205_PTRY_P1C8"),
           dict(number="3", name="Cleaners", code="027_CLNR_P1C25"), dict(number="4", name="Meeting", code="189_MTRM-M_P1C6"),
           dict(number="5", name="Meeting 2", code="189_MTRM_P1C6"), dict(number="6", name="Store", code="STEQ")]
    r = link_rls(dpa, rows)
    assert r["heights"]["1"]["height_m"] == 2.7 and r["heights"]["1"]["basis"] == "code and variant"
    assert "2" not in r["heights"] and "P1C7" in next(u for u in r["unlinked_dpa"] if u["number"] == "2")["reason"]
    assert r["heights"]["3"]["basis"] == "code (RLS not variant-specific)"
    assert [c["number"] for c in r["conflicts"]] == ["5"]                       # flagged by the register: no height chosen
    assert {u["number"] for u in r["unlinked_dpa"]} >= {"4", "6"}                # MTRM-M is not MTRM; a row with no height is not a link


# --------------------------------------------------------------------------- #
# round 4: RLS join by number, variant from the file name, department scope; plan rooms with no area; abbreviation; code disagreement
def _rls(dept, number, code, variant, mm, f="a/RLS.pdf", flag=""):
    return dict(dept_no=dept, rls_number=number, room_type_code=code, variant=variant, governing_height_mm=mm, rel_path=f, conflict_flag=flag)


def test_rls_number_check_uses_the_number_column():
    rows = [_rls("30", "183", "PTRY", "", "3000", "RLS_183_PTRY-PT_P1C1_Pantry Patient.pdf")]
    r = link_rls([dict(number="1", name="Pantry - Staff", code="205_PTRY_P1C7")], rows, "30")
    assert "1" not in r["heights"] and "number 183" in r["unlinked_dpa"][0]["reason"]          # another room type: number 183 is not 205
    rows.append(_rls("30", "205", "PTRY", "", "2700"))
    r = link_rls([dict(number="1", name="Pantry - Staff", code="205_PTRY_P1C7")], rows, "30")
    assert r["heights"]["1"]["height_m"] == 2.7


def test_rls_not_variant_specific_needs_every_row_variant_free():
    rows = [_rls("47", "040", "COHU-L", "P2C1", "2700"), _rls("47", "040", "COHU-L", "P2C2", "3000"), _rls("47", "040", "COHU-L", "", "2400")]
    r = link_rls([dict(number="1", name="x", code="040_COHU-L_P2C0"), dict(number="2", name="x", code="040_COHU-L_P2C1")], rows, "47")
    assert "1" not in r["heights"] and "variant P2C1, P2C2" in r["unlinked_dpa"][0]["reason"]   # mixed rows: no fallback (same rule as the RDS)
    assert r["heights"]["2"]["height_m"] == 2.7 and r["heights"]["2"]["basis"] == "code and variant"
    free = [_rls("47", "040", "COHU-L", "", "2400"), _rls("47", "040", "COHU-L", "", "2400")]
    assert link_rls([dict(number="1", name="x", code="040_COHU-L_P2C0")], free, "47")["heights"]["1"]["basis"] == "code (RLS not variant-specific)"


def test_rls_rows_are_limited_to_the_department():
    rows = [_rls("30", "205", "PTRY", "", "2700"), _rls("26", "205", "PTRY", "", "3000"), _rls("26", "205", "PTRY", "P7C1", "3000")]
    dpa = [dict(number="1", name="Pantry", code="205_PTRY_P1C7")]
    r = link_rls(dpa, rows, "30")
    assert r["heights"]["1"]["height_m"] == 2.7 and not r["conflicts"]                       # the other department's 3.0 m never pools
    assert link_rls(dpa, rows, "26")["unlinked_dpa"]                                        # D26 has variant P7C1 only: no fallback


def test_rls_variant_read_from_the_file_name_of_a_combined_sheet():
    f = "x/RLS_205_PTRY_P1C7, 072_CHST-LKR_P1C3_Lounge, Pantry - Staff, Change Room - Locker_2_AH.pdf"
    rows = [_rls("30", "205", "PTRY", "", "2700", f), _rls("30", "205", "PTRY", "P1C18", "3000"), _rls("30", "072", "CHST-LKR", "", "2700", f)]
    r = link_rls([dict(number="1", name="a", code="205_PTRY_P1C7"), dict(number="2", name="b", code="072_CHST-LKR_P1C3")], rows, "30")
    assert r["heights"]["1"]["height_m"] == 2.7 and r["heights"]["1"]["basis"] == "code and variant" and r["heights"]["1"]["variant_from"] == "file name"
    assert r["heights"]["2"]["height_m"] == 2.7
    assert any(u["variant"] == "P1C18" for u in r["unlinked_rls"])                         # the unused RLS side is listed


def test_rls_single_height_conflict_flag_is_worded_as_a_flag():
    from src.workbook.assemble import height_conflict_text
    r = link_rls([dict(number="1", name="a", code="243_DICL_P1C0")], [_rls("30", "243", "DICL", "", "2700", flag="several_heights_tie")], "30")
    c = r["conflicts"][0]
    assert c["values"] == [2.7] and c["flags"] == ["several heights tie"]
    assert "more than one ceiling height" in height_conflict_text(c) and scan_strings({"t": [height_conflict_text(c)]}) == []


def test_plan_rooms_without_area_take_part_in_the_zoning_links():
    sc = lambda a, b: name_score(a, b, {})
    dpa = [dict(number="1", name="Space for Roll Call", storey="B2"), dict(number="2", name="Manual Picking | Repackaging Area", storey="B2")]
    plan = [dict(_plan("Space for Roll Call", "B2", "AHU-1"), area=None, shared=True), dict(_plan("Manual Picking", "B2", "AHU-1"), area=None, shared=True)]
    out = plan_links(dpa, plan, set(), sc, ("ahu",), ("AHU",))
    assert out["1"]["basis"] == "name and count" and "2" not in out                         # a contained phrase needs the phrase score
    from src.workbook.assemble import zoning_name_score
    zs = lambda a, b: zoning_name_score(a, b, {})
    out = plan_links(dpa, plan, set(), zs, ("ahu",), ("AHU",))
    assert out["2"]["tag"] == "AHU-1"
    assert zs("Office - Pharmacy Head", "Office") < 0.85                                    # a single word is not a phrase


def test_plan_name_phrases_in_one_dpa_name_with_equal_counts():
    from src.workbook.assemble import zoning_name_score
    zs = lambda a, b: zoning_name_score(a, b, {})
    dpa = [dict(number=str(i), name="Inpatient Pharmacy Automation System", storey="B2") for i in range(3)]
    plan = [dict(_plan("Inpatient Pharmacy", "B2", "AHU-1"), area=None) for _ in range(3)] + [dict(_plan("Automation System", "B2", "AHU-1"), area=None) for _ in range(3)]
    assert set(plan_links(dpa, plan, set(), zs, ("ahu",), ("AHU",))) == {"0", "1", "2"}
    plan[-1] = dict(_plan("Automation System", "B2", "AHU-2"), area=None)                  # different AHUs: nothing is forced
    assert plan_links(dpa, plan, set(), zs, ("ahu",), ("AHU",)) == {}


def test_collab_abbreviation_and_name_basis_code_disagreement(fake_root):
    from src.workbook.assemble import EXTRA_ABBREVIATIONS
    assert name_score("Collaboration Huddle", "Collab", {k: list(v) for k, v in EXTRA_ABBREVIATIONS.items()}) >= 0.85
    r = link_rds([dict(number="A", name="Meeting Room - Large", code="OFF-OSS"), dict(number="B", name="Pantry", code="205_PTRY_P1C9")],
                 [rds("190_MTRM-L_P1C7", "Meeting Room - Large", "6"), rds("205_PTRY_P1C8", "Pantry", "2")])
    assert "OFF-OSS" in r["links"]["A"]["code_note"] and "190_MTRM-L_P1C7" in r["links"]["A"]["code_note"]
    assert "other variant" in r["links"]["B"]["code_note"]
