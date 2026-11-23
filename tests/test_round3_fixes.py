"""Fixes of the final verification round 2 (R2-01 to R2-13): plant outlines of D00, outdoor air against tender, AN-ED offices, template text, partial wording,
open items ids, occupancy use mapping, undrawn units, tool names in file properties."""
import re
import zipfile
from pathlib import Path

import openpyxl
import pytest

from src.workbook.assemble import room_use
from src.workbook.common import tender_remark
from src.workbook.recalc import recalc
from src.workbook.selftest import TOOL_NAMES, properties_scan, template_text_scan
from src.workbook.sheets_systems import coverage_formula

ROOT = Path(__file__).resolve().parents[1]
WB = ROOT / "out" / "loads" / "workbooks"


def _eval(tmp_path, formulas, cells):
    """Recalculate formulas placed in A1.. of a one-sheet workbook whose input cells are given; returns the values."""
    from openpyxl.workbook.defined_name import DefinedName
    wb = openpyxl.Workbook()
    ws = wb.active
    for nm, ref in (("DuctLeakage", "$X$1"), ("TenderLeakage", "$X$2")):
        wb.defined_names[nm] = DefinedName(nm, attr_text=f"Sheet!{ref}")
    ws["X1"], ws["X2"] = 0.1, 0.15
    for ref, v in cells.items():
        ws[ref] = v
    for i, f in enumerate(formulas, 1):
        ws[f"Z{i}"] = f
    p = tmp_path / "t.xlsx"
    wb.save(p)
    out = openpyxl.load_workbook(recalc(p, tmp_path / "rc"), data_only=True).active
    return [out[f"Z{i}"].value for i in range(1, len(formulas) + 1)]


# R2-05 ------------------------------------------------------------------------------------------------------
def test_no_placeholder_text_in_remark_formulas():
    for kw in ({}, dict(note="N1"), dict(partial="P1", crit_partial=True), dict(oa_above="Q1>R1", oa_calc="Q1", oa_tender="R1", partial="P1", zero="z")):
        f = tender_remark("A1", "B1", "C1", "D1", "S1", **kw)
        # braces occur only inside formula text outside the literals: none are allowed at all
        assert "{" not in f and "}" not in f, kw


def test_no_template_text_in_issued_workbooks():
    files = sorted(WB.glob("*.xlsx")) + [ROOT / "out" / "zoning" / "zone_schedule.xlsx"]
    if not files or not files[0].exists():
        pytest.skip("workbooks not present")
    bad = {f.name: template_text_scan(f)[:3] for f in files if template_text_scan(f)}
    assert not bad, bad


# R2-02 ------------------------------------------------------------------------------------------------------
def test_outdoor_air_above_tender_is_verify_and_no_reduction_wording(tmp_path):
    f = tender_remark("A1", "B1", "C1", "D1", '"src"', oa_above="AND(ISNUMBER(F1),E1>F1)", oa_calc="E1", oa_tender="F1")
    above, within, none = _eval(tmp_path, [f, f.replace("A1", "A2").replace("B1", "B2").replace("C1", "C2").replace("D1", "D2").replace("E1", "E2").replace("F1", "F2"),
                                           f.replace("E1", "E3").replace("F1", "F3")],
                                {"A1": 10000, "B1": 8000, "C1": 2000, "D1": 0.2, "E1": 3641, "F1": 457,
                                 "A2": 10000, "B2": 8000, "C2": 2000, "D2": 0.2, "E2": 300, "F2": 457, "E3": 300})
    assert "Outdoor air 3,641 L/s is above tender outdoor air 457 L/s" in above and "VERIFY" in above
    assert "Reduced" not in above and "No reduction is stated" in above
    assert within.startswith("Reduced 10,000 to 8,000 L/s")
    assert "above tender outdoor air" not in none and none.startswith("Reduced")


def test_zone_schedule_outdoor_air_formula(tmp_path):
    from src.zoning.deliver import red_formula, remarks_formula
    cells = {"I2": 10000, "J2": "src", "K2": 8000, "G2": 100, "S2": None, "T2": 457, "U2": 3641}
    red, rem = _eval(tmp_path, [red_formula(2, True), remarks_formula(2, True)], cells)
    assert red in (None, "") and "VERIFY" in rem and "Reduced" not in rem
    cells["U2"] = 300
    red, rem = _eval(tmp_path, [red_formula(2, True), remarks_formula(2, True)], cells)
    assert red == 2000 and rem.startswith("Reduced")
    cells["G2"] = 0
    red, rem = _eval(tmp_path, [red_formula(2, True), remarks_formula(2, True)], cells)
    assert red in (None, "") and rem.startswith("No zone polygon")


# R2-06 ------------------------------------------------------------------------------------------------------
def test_partial_wording_n_of_m(tmp_path):
    f = coverage_formula("B1", "C1", "D1", "E1", "F1", "G1", "H1", "I1")
    cases = [
        ({"B1": 10, "C1": 349, "D1": 1, "E1": 12, "F1": 12, "G1": 0.9, "H1": 0, "I1": 0}, "partial: the department workbooks count 1 rooms (12 m²); the zone schedule gives 10 rooms (349 m²)"),
        ({"B1": 7, "C1": 2067, "D1": 8, "E1": 2527, "F1": 2527, "G1": 0.9, "H1": 8, "I1": 2527}, "No criteria row for 8 of 8 rooms on this unit (2,527 m²)"),    # the no-criteria cause comes before the area mismatch
        ({"B1": 10, "C1": 0, "D1": 10, "E1": 300, "F1": 300, "G1": 0.9, "H1": 0, "I1": 0}, "partial: the zone schedule gives the unit a zoned area of 0"),
        ({"B1": 10, "C1": 300, "D1": 10, "E1": 300, "F1": 0, "G1": 0.9, "H1": 10, "I1": 300}, "No criteria row for 10 of 10 rooms"),
        ({"B1": 10, "C1": 300, "D1": 10, "E1": 300, "F1": 300, "G1": 0.9, "H1": 0, "I1": 0}, ""),
    ]
    got = _eval(tmp_path, [f] * len(cases), {})  # placeholder to prove the formula parses
    for i, (cells, want) in enumerate(cases):
        v = _eval(tmp_path, [f], cells)[0]
        assert (v or "").startswith(want) and (bool(want) == bool(v)), (cells, v)


def test_no_criteria_unit_remark(tmp_path):
    f = tender_remark("A1", "B1", "C1", "D1", '"src"', partial="P1", crit_partial=True, zero="z")
    v = _eval(tmp_path, [f], {"A1": 3000, "B1": 0, "C1": "", "D1": "", "P1": "No criteria row for 22 of 22 rooms on this unit (1,107 m²)"})[0]
    assert v.startswith("No criteria row for 22 of 22 rooms on this unit (1,107 m²). The calculated airflow is not known. No reduction is stated.")
    assert "is 0 L/s" not in v and "upper limit" not in v


def test_upper_limit_only_on_a_stated_reduction(tmp_path):
    f = tender_remark("A1", "B1", "C1", "D1", '"src"', partial="P1", crit_partial=True, zero="z")
    red = _eval(tmp_path, [f], {"A1": 3000, "B1": 2000, "C1": 1000, "D1": 0.333, "P1": ""})[0]
    assert red.startswith("Reduced") and red.endswith("The reduction is an upper limit.")
    for cells in ({"A1": 3000, "B1": 0, "P1": ""}, {"A1": 3000, "B1": 2000, "P1": "partial: x"}, {"A1": 3000, "B1": 4000, "P1": ""}, {"A1": "", "B1": 2000, "P1": ""}):
        v = _eval(tmp_path, [f], dict(cells, C1="", D1=""))[0]
        assert "upper limit" not in v, (cells, v)


def test_small_zone_reduction_is_marked_not_confirmed(tmp_path):
    from src.workbook.sheets_systems import small_zone
    f = tender_remark("A1", "B1", "C1", "D1", '"src"', partial="P1", crit_partial=True, zero="z", small=small_zone("M1", "N1"))
    v = _eval(tmp_path, [f], {"A1": 9000, "B1": 187, "C1": 8813, "D1": 0.979, "P1": "", "M1": 1, "N1": 33.5})[0]
    assert v.startswith("Reduced 9,000 to 187") and "OI-R206-1" in v and "not a confirmed result" in v
    v = _eval(tmp_path, [f], {"A1": 9000, "B1": 187, "C1": 8813, "D1": 0.979, "P1": "", "M1": 5, "N1": 300})[0]
    assert "OI-R206-1" not in v


# R2-01 ------------------------------------------------------------------------------------------------------
def test_plant_names():
    from src.workbook.common_areas import plant_names
    assert plant_names("SAN / AHU") == ["AHU"]
    assert plant_names("LV / TOILETS ABOVE / dotted line indicates double slab extent") == ["LV"]
    assert plant_names("Tfr") and plant_names("MG Compressor Plant (CSSD)") and plant_names("HV Switchrm")
    assert not plant_names("Corr") and not plant_names("FE / S.32 / IT / SEC / IT Riser / Toilet / Off / Collab / HC / Mtg")


@pytest.mark.skipif(not (ROOT / "out" / "registers" / "appd_spaces.csv").exists(), reason="registers not present")
def test_d00_plant_outlines_and_units():
    from src.workbook.common_areas import assemble_common_areas
    from src.workbook import appd_outlines as ao
    from src.workbook.assemble import _read, load_zone_assignments
    d = assemble_common_areas(ROOT)
    by = {r["number"]: r for r in d["rooms"]}
    for n in ("CA-L03-023", "CA-L03-027", "CA-L04-020"):
        assert not by[n]["criteria"] and "Plant space" in by[n]["gaps"]["criteria"], n          # no circulation default for a plant outline
    assert by["CA-L03-023"]["ahu"] == "AHU-B-03-ELECRM-01A/B"                                  # L03-R011 lies in the outline (CF-0034 carried)
    assert by["CA-L04-020"]["ahu"] is None                                                       # L04-R267 is 'no information' (CF-0044 carried)
    assert by["CA-L07-000"]["criteria"]                                                          # a riser outline keeps the default
    # for every space with a unit, the unit is a class of a plan room whose label point lies inside the outline
    outlines = ao.load_outlines(ROOT)
    zones = load_zone_assignments(ROOT)
    bad = []
    for r in d["rooms"]:
        if not r["ahu"] or not r["prov"]["zoning"][1].startswith("ZONE-"):
            continue
        st, idx = r["number"].split("-")[1], str(int(r["number"].split("-")[2]))
        polys = _read(ROOT / "out" / "registers" / f"room_polygons_{st}.csv")
        inside = ao.rooms_inside(ROOT, st, outlines, polys).get(idx, [])
        classes = {(zones[st][p["room_id"]].get("class") or "").strip() for p in inside if p["room_id"] in zones.get(st, {})}
        classes |= {(zones[st][p["room_id"]].get("class") or "").strip() for p in polys if p.get("appd_annotation_index") == idx and p["room_id"] in zones.get(st, {})}
        if r["ahu"] not in classes:
            bad.append(r["number"])
    assert not bad, bad


# R2-03 ------------------------------------------------------------------------------------------------------
@pytest.mark.skipif(not (ROOT / "out" / "registers" / "criteria.csv").exists(), reason="registers not present")
def test_an_ed_offices_take_the_admin_workspace_office_row():
    from src.workbook.assemble import assemble_department
    d = assemble_department("31", None, ROOT)
    offices = [r for r in d["rooms"] if r["storey"] == "L02" and re.match(r"Office\b", r["name"])]
    assert offices
    for r in offices:
        c = r["criteria"]
        assert c["code"] == "A" and c["criteria_row"] == "ED Admin Workspace Office", r["number"]
        assert c["remark"].startswith("Future EDTU, treated as office") and not re.search(r"\bD-0\d\d\b", c["remark"])


# R2-11 ------------------------------------------------------------------------------------------------------
def test_room_use_mapping_of_the_listed_names():
    for name, use in (("M&E", "plant_room"), ("Change Room - Locker", "toilet"), ("Nursing Room", "office"), ("Screening Area", "medical_procedure"),
                      ("PPE Operational allowance - PAN", "storage"), ("Recycling Room", "storage"), ("Multipurpose Room", "conference_meeting"),
                      ("Pre-function Area", "reception_waiting"), ("Sleep Pods - Staff", "break_room_pantry"), ("Reporting Room", "office"), ("BMD", "medical_procedure")):
        assert room_use(name)[0] == use, name
    assert room_use("Locker - Staff")[0] is None            # lockers stay listed: the density table has no row for them


# R2-10 ------------------------------------------------------------------------------------------------------
def test_open_items_use_the_ids_of_the_rulings():
    from src import open_items
    text = (ROOT / "out" / "qa" / "open_items_for_marcus.md").read_text(encoding="utf-8")
    rulings = (ROOT / "out" / "qa" / "final_verification_rulings.md").read_text(encoding="utf-8")
    for oid in set(re.findall(r"OI-[A-Z]?\d+-\d+", rulings)):
        assert oid in text, oid
    decisions = open_items.parse_decisions((ROOT / "out" / "qa" / "decisions_needed.md").read_text(encoding="utf-8"))
    for d in decisions:
        if d.get("Status", "").strip() == "open":
            assert d["id"] in text, d["id"]
    assert not re.search(r"OI-R1-\d\d", text)            # the old scheme is gone


# R2-12 / R2-13 ----------------------------------------------------------------------------------------------
def test_no_tool_names_in_xlsx_properties():
    files = sorted(WB.glob("*.xlsx")) + [ROOT / "out" / "zoning" / "zone_schedule.xlsx"]
    if not files or not files[0].exists():
        pytest.skip("workbooks not present")
    for f in files:
        assert not properties_scan(f), f.name
        with zipfile.ZipFile(f) as z:
            assert not re.search(r"<AppVersion>", z.read("docProps/app.xml").decode())


def test_no_tool_names_in_pdf_properties():
    pdf = ROOT / "out" / "zoning" / "AIGH_IP_AHU_Zoning_LB3-L07.pdf"
    if not pdf.exists():
        pytest.skip("pdf not present")
    raw = pdf.read_bytes()
    assert not re.search(rb"MuPDF|PyMuPDF|Producer\s*\(", raw), "tool name or producer in the PDF"
    assert set(re.findall(rb"/Creator\s*\(([^)]*)\)", raw)) <= {b"AIGH IP ACMV design team"}      # the layers name the author, not the writer
    import pymupdf
    d = pymupdf.open(str(pdf))
    assert not any(TOOL_NAMES.search(str(v or "")) for v in d.metadata.values())
    assert not (d.get_xml_metadata() or "").strip()


def test_units_without_polygon_are_not_in_legend_or_index():
    import json
    chk = ROOT / "out" / "qa" / "zoning_deliverable_checks.json"
    if not chk.exists():
        pytest.skip("checks not present")
    pdf = json.loads(chk.read_text())["pdf"]
    assert all(not v for v in pdf["not_drawn_units_listed_in_index"].values())
    import pymupdf
    d = pymupdf.open(str(ROOT / "out" / "zoning" / "AIGH_IP_AHU_Zoning_LB3-L07.pdf"))
    text = "".join(p.get_text() for p in d) + "".join((a.info.get("content", "") for p in d for a in (p.annots() or [])))
    assert "NOT DRAWN" not in text
    assert "Not drawn" in text and "See the zone schedule" in text                       # R2-12 residual: the PDF says that the units are left out
    for tag in ("AHU-B-07-06DSOT-11", "AHU-B-07-06DSOT-12A/B", "AHU-B-07-06STER-03A/B"):
        assert tag in text, tag
        for p in d:                                                                     # no zone, legend swatch or index row of the unit: the tag is in a note only
            for a in p.annots() or []:
                if tag in (a.info.get("subject") or ""):
                    raise AssertionError(f"annotation subject {tag}")
