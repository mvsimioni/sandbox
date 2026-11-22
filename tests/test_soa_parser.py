"""Tests for src.soa_parser: synthetic PDF/xlsx fixtures plus a smoke test on the real SOA files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import soa_parser as sp

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def make_pdf(path: Path, rows, net="30", circ="10", gross="40", remarks=False, title="IPW - Test Ward"):
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    ins = lambda x, y, t: page.insert_text((x, y), t, fontsize=7)
    ins(53, 58, "Alexandra Hospital, Singapore")
    ins(291, 60, "Version")
    ins(330, 60, "16")
    ins(513, 60, "As Drawn")
    ins(53, 70, "SCHEDULE OF ACCOMMODATION:")
    ins(53, 80, title)
    ins(430, 82, "Net Area (sqm)")
    ins(524, 82, net)
    ins(424, 91, "Circulation (sqm)")
    ins(524, 91, circ)
    ins(53, 99, "Detailed Design")
    ins(424, 99, "Gross Area (sqm)")
    ins(520, 99, gross)
    y = 108
    for x, t in ((53, "KPU"), (87, "Functional Group"), (184, "Code"), (249, "Unique Room"), (315, "Room Name"),
                 (479, "As Drawn Area (sqm)")):
        ins(x, y, t)
    if remarks:
        ins(560, y, "Remarks")
    ins(249, y + 8, "Identifier")
    y = 135
    for r in rows:
        ins(53, y, r[0])
        ins(87, y, r[1])
        if r[2]:
            ins(184, y, r[2])
        ins(249, y, r[3])
        ins(315, y, r[4])
        ins(527, y, r[5])
        if remarks and len(r) > 6:
            ins(560, y, r[6])
        y += 9
    doc.save(str(path))
    doc.close()
    return path


ROWS = [
    ("KPU01", "PATIENT CARE", "1BR-IS", "TEST1 02.01.01", "1 Bed Room - Iso", "25.0"),
    ("KPU01", "PATIENT CARE", "1BR-IS", "TEST1 02.01.02", "1 Bed Room - Iso", "25.5"),
    ("KPU01", "Circulation", "", "TEST1 CI.01.01", "Circulation", "10.0"),
    ("KPU05", "PUBLIC", "284_ADMI_P1C1", "TEST1 01.02.01", "Admin Support", "-"),
]


@pytest.fixture()
def pdf_file(tmp_path):
    return make_pdf(tmp_path / "t.pdf", ROWS, net="51", circ="10", gross="61")


# --------------------------------------------------------------------------- #
# file selection and path metadata
# --------------------------------------------------------------------------- #
def test_is_soa_path_and_exclusions():
    assert sp.is_soa_path("DD Sign-OFF/03 - IP DIALYSIS/Submitted/03_SOA/DD_V16_IWDI.pdf")
    assert sp.is_soa_path("DD Sign-OFF/01 - IP FAST/Submitted/03_SOA/x.pdf")
    assert sp.is_soa_path("DD Sign-OFF/27 - Breast Clinic/DD Closure/20250704_c/AIGH_SOA_v16_KPU02.Breast Clinic_r 0525.pdf")
    assert not sp.is_soa_path("Basis/MOT NSC/SOA.pdf")
    assert not sp.is_soa_path("DD Sign-OFF/03 - IP DIALYSIS/Submitted/01_Drawings/a.pdf")
    ok, why = sp.classify_candidate("DD Sign-OFF/22 - MOT/Submitted/03_SOA/RDS_009_CHST-F_P1C6_Change Room.pdf")
    assert not ok and "Room Data Sheet" in why
    ok, why = sp.classify_candidate("DD Sign-OFF/22 - MOT/Submitted/03_SOA/SoE list.pdf")
    assert not ok
    assert sp.classify_candidate("DD Sign-OFF/10 - X/Submitted/03_SOA/MP-SOA_DD_IWRB1_v16.pdf") == (True, "")


def test_path_meta_dates_and_stage():
    m = sp.path_meta("DD Sign-OFF/10 - IP OP Rehab Med/AH sign off with comments/20251219_Sign off comments_Resent/03_SOA/MP-SOA_DD_IWRB1_v16_to cons.pdf")
    assert m["dept_no"] == "10"
    assert m["issue_folder"] == "AH sign off with comments"
    assert m["issue_date"] == "20251219"
    assert m["filename_version"] == "V16"
    m = sp.path_meta("DD Sign-OFF/24 - Rehabilitation Services AHP/Submitted/03_SOA/AIGH_SOA_v16_KPU04.REHC1.pdf")
    assert m["issue_folder"] == "Submitted" and m["issue_date"] == "" and m["file_unit_token"] == "KPU04.REHC1"
    m = sp.path_meta("DD Sign-OFF/23 - Endoscopy/AH Commented_20250818_IGH_SOA_v16_KPU03.ENDO_R2.pdf")
    assert m["issue_folder"] == "other" and m["issue_date"] == "20250818"
    m = sp.path_meta("DD Sign-OFF/33- Outpatient - Admin Neighbourhood/Submitted/03_SOA/Working ( Do Not Send )/20250807_AIGH_DD_SOA_V16_AN OP.xlsx")
    assert m["dept_no"] == "33" and m["is_working"]


# --------------------------------------------------------------------------- #
# pdf parsing
# --------------------------------------------------------------------------- #
def test_parse_pdf_rows_and_totals(pdf_file):
    res = sp.parse_pdf(pdf_file)
    assert res["meta"]["title"] == "IPW - Test Ward"
    assert res["meta"]["version"] == "16"
    assert [r["room_code"] for r in res["rows"]] == ["TEST1 02.01.01", "TEST1 02.01.02", "TEST1 CI.01.01", "TEST1 01.02.01"]
    first = res["rows"][0]
    assert first["area"] == 25.0 and first["area_text"] == "25.0"
    assert first["type_code"] == "1BR-IS" and first["kpu"] == "KPU01" and first["functional_group"] == "PATIENT CARE"
    assert first["room_name"] == "1 Bed Room - Iso"
    circ = res["rows"][2]
    assert circ["type_code"] == "" and circ["functional_group"] == "Circulation"
    dash = res["rows"][3]
    assert dash["area"] is None and dash["area_text"] == "-" and dash["confidence"] < 1
    assert not res["orphans"]
    checks = {t["kind"]: t for t in sp.totals_check(res, "pdf")}
    assert checks["net"]["status"] == "MATCH" and checks["net"]["computed"] == 50.5
    assert checks["circ"]["status"] == "MATCH"
    assert checks["gross"]["status"] == "MATCH"
    assert checks["printed gross - circulation - net"]["status"] == "MATCH"


def test_totals_mismatch_is_reported_not_repaired(tmp_path):
    p = make_pdf(tmp_path / "m.pdf", ROWS, net="90", circ="10", gross="100")
    res = sp.parse_pdf(p)
    checks = {t["kind"]: t for t in sp.totals_check(res, "pdf")}
    assert checks["net"]["status"] == "MISMATCH"
    assert checks["net"]["diff"] == pytest.approx(-39.5)
    # the room lines themselves are untouched
    assert sum(r["area"] for r in res["rows"] if r["area"] is not None) == pytest.approx(60.5)


def test_remarks_column(tmp_path):
    rows = [("KPU01", "PATIENT CARE", "1BR", "TEST1 02.01.01", "Room", "20.0", "check area"),
            ("KPU01", "PATIENT CARE", "1BR", "TEST1 02.01.02", "Room", "21.0")]
    res = sp.parse_pdf(make_pdf(tmp_path / "r.pdf", rows, remarks=True))
    assert res["rows"][0]["remarks"] == "check area"
    assert res["rows"][1]["remarks"] == ""
    assert res["rows"][0]["area"] == 20.0


def test_page_without_header_is_reported(tmp_path):
    import fitz

    doc = fitz.open()
    doc.new_page().insert_text((50, 50), "nothing here", fontsize=8)
    p = tmp_path / "e.pdf"
    doc.save(str(p))
    res = sp.parse_pdf(p)
    assert res["rows"] == []
    assert res["pages"][0]["status"] == "not parsed" and "header" in res["pages"][0]["reason"]


def test_annotation_text_is_not_a_room_row(tmp_path):
    import fitz

    p = make_pdf(tmp_path / "a.pdf", ROWS)
    doc = fitz.open(str(p))
    doc[0].add_freetext_annot(fitz.Rect(300, 300, 500, 340), "20250811_AH missing room TEST1 09.09.09")
    doc.saveIncr()
    doc.close()
    res = sp.parse_pdf(p)
    assert len(res["rows"]) == 4
    assert any("missing room" in a["text"] for a in res["annotations"])


# --------------------------------------------------------------------------- #
# xlsx parsing
# --------------------------------------------------------------------------- #
def make_xlsx(path: Path, rows, net, circ, gross):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Database"
    ws["E2"] = "SCHEDULE OF ACCOMMODATION:"
    ws["E3"] = "IPW - Test"
    ws["H1"], ws["I1"] = "Version ", 16
    ws["I3"], ws["J3"] = "Net Area (sqm)", net
    ws["I4"], ws["J4"] = "Circulation (sqm)", circ
    ws["I5"], ws["J5"] = "Gross Area (sqm)", gross
    hdr = ["Revit Synchronise Date", "FPU Department No.", "FPU Department Name", "Room No.", "KPU", "Functional Group",
           "Code", "Unique Room Identifier", "Room Name", "As Drawn Area (sqm)"]
    for i, h in enumerate(hdr, 1):
        ws.cell(row=6, column=i, value=h)
    for j, r in enumerate(rows, 7):
        for i, v in enumerate(r, 1):
            ws.cell(row=j, column=i, value=v)
    wb.create_sheet("Circulation")["A2"] = "SCHEDULE OF ACCOMMODATION: KPU-FPU SUMMARY"
    wb.save(str(path))
    return path


def test_parse_xlsx(tmp_path):
    rows = [["1/1/2025", "T1", "IPW - Test", 1, "KPU01", "PATIENT CARE", "1BR", "T1 02.01.01", "1 Bed Room", 25.25],
            ["1/1/2025", "T1", "IPW - Test", 2, "KPU01", "Circulation", None, "T1 CI.01.01", "Circulation", 10.0]]
    p = make_xlsx(tmp_path / "t.xlsx", rows, 25.25, 10.0, 35.25)
    res = sp.parse_xlsx(p)
    assert [r["room_code"] for r in res["rows"]] == ["T1 02.01.01", "T1 CI.01.01"]
    assert res["rows"][0]["area"] == 25.25 and res["rows"][0]["page_or_sheet_row"] == "Database!row 7"
    assert res["meta"]["title"] == "IPW - Test" and res["meta"]["version"] == "16"
    statuses = {t["kind"]: t["status"] for t in sp.totals_check(res, "xlsx")}
    assert statuses["net"] == statuses["circ"] == statuses["gross"] == "MATCH"
    assert any(p_["status"] == "not parsed" for p_ in res["pages"])  # the circulation-factor sheet
    res_bad = sp.parse_xlsx(make_xlsx(tmp_path / "b.xlsx", rows, 20.0, 10.0, 35.25))
    assert {t["kind"]: t["status"] for t in sp.totals_check(res_bad, "xlsx")}["net"] == "MISMATCH"


# --------------------------------------------------------------------------- #
# governing issue and differences
# --------------------------------------------------------------------------- #
def fake_result(rel, rows, fmt="pdf", net=None):
    pm = sp.path_meta(rel)
    rr = []
    for code, name, area in rows:
        rr.append({"unit_zone_code": code.split(" ")[0], "kpu": "KPU04", "functional_group": "X", "type_code": "C",
                   "page_or_sheet_row": "p1", "room_code": code, "room_name": name, "area_text": ("%.2f" if fmt == "xlsx" else "%.1f") % area, "area": area,
                   "remarks": "", "raw": {}, "bbox": (0, 0, 1, 1), "confidence": 1.0, "note": "", "method": "m"})
    meta = {"title": "T", "version": "16", "net": None, "circ": None, "gross": None}
    if net is not None:
        meta["net"] = {"text": str(net), "value": float(net), "label": "Net", "bbox": (0, 0, 1, 1)}
    return {"rel_path": rel, "pm": pm, "error": "", "rows": rr, "format": fmt, "meta": meta, "pages": [], "orphans": [],
            "annotations": [], "n_pages": 1, "soa_version": "V16", "totals": []}


def test_governing_latest_issue_and_differences():
    base = "DD Sign-OFF/24 - Rehabilitation Services AHP/%s/03_SOA/AIGH_SOA_v16_KPU04.REHC1%s.pdf"
    sub = fake_result(base % ("Submitted", ""), [("REHC1 01.02.01", "Gym", 52.0), ("REHC1 02.01.01", "OT", 47.6)], net=100)
    ah = fake_result(base % ("AH sign off with comments", "_AH"), [("REHC1 01.02.01", "Gym", 53.0), ("REHC1 03.02.01", "Store", 15.9)], net=69)
    other = fake_result("DD Sign-OFF/26 - Diagnostic Imaging/Submitted/03_SOA/SOA DINM.pdf", [("DINM 01.01.01", "X", 5.0)])
    results = [ah, sub, other]
    sp.assign_governing(results)
    assert sub["governing"] == "no" and ah["governing"] == "yes" and other["governing"] == "yes"
    assert sub["group"] == ah["group"] != other["group"]
    diffs = sp.issue_differences(results)
    kinds = {(d["change_type"], d["room_code"]) for d in diffs}
    assert ("area changed", "REHC1 01.02.01") in kinds
    assert ("room removed", "REHC1 02.01.01") in kinds
    assert ("room added", "REHC1 03.02.01") in kinds
    assert any(d["change_type"] == "printed total changed" and d["field"] == "net" for d in diffs)


def test_closure_beats_later_dated_ah_comment():
    p = "DD Sign-OFF/27 - Breast Clinic/%s/20250704_DD Sign-off Comments_Breast Clinic/AIGH_SOA_v16_KPU02.Breast Clinic_r 0525.pdf"
    rows = [("DISA 01.01.01", "Room", 10.0)]
    a = fake_result(p % "AH sign off with comments", rows)
    b = fake_result(p % "DD Closure", rows)
    c = fake_result("DD Sign-OFF/27 - Breast Clinic/Submitted/03_SOA/AIGH_SOA_v16_KPU02.Breast Clinic.pdf", rows)
    sp.assign_governing([a, b, c])
    assert (c["governing"], a["governing"], b["governing"]) == ("no", "no", "yes")
    diffs = sp.issue_differences([a, b, c])
    assert {d["change_type"] for d in diffs} == {"no differences found"}


def test_same_issue_pdf_and_xlsx_pick_one_governing_and_working_copy_loses():
    pdf = fake_result("DD Sign-OFF/33- X/Submitted/03_SOA/20250807_AIGH_DD_SOA_V16_AN OP.pdf", [("ANOP1 01.01.01", "A", 10.0)])
    xl = fake_result("DD Sign-OFF/33- X/Submitted/03_SOA/Working ( Do Not Send )/20250807_AIGH_DD_SOA_V16_AN OP.xlsx",
                     [("ANOP1 01.01.01", "A", 10.04)], fmt="xlsx")
    sp.assign_governing([xl, pdf])
    assert pdf["governing"] == "yes" and xl["governing"] == "no"
    diffs = sp.issue_differences([xl, pdf])
    assert any(d["change_type"] == "area rounding only" for d in diffs)


def test_same_unit_code_different_titles_are_separate_sois():
    mk = lambda lvl: fake_result("DD Sign-OFF/26 - DI/Submitted/03_SOA/SOA DIIP (L%d).pdf" % lvl, [("DIIP 01.01.01", "A", 5.0)])
    a, b = mk(1), mk(4)
    a["meta"]["title"], b["meta"]["title"] = "DI - IP (1st Storey)", "DI - IP (4th Storey)"
    sp.assign_governing([a, b])
    assert a["group"] != b["group"] and a["governing"] == b["governing"] == "yes"


def test_inpatient_flag():
    assert sp.inpatient_flag("IWFA1", "") == "yes"
    assert sp.inpatient_flag("ICHA", "") == "yes"
    assert sp.inpatient_flag("OHAG", "") == "no"
    assert sp.inpatient_flag("REHC1", "").startswith("shared")


# --------------------------------------------------------------------------- #
# real data smoke test
# --------------------------------------------------------------------------- #
REAL = PROJECT_ROOT / "data/source/DD Sign-OFF/03 - IP DIALYSIS/Submitted/03_SOA/DD_V16_IWDI.pdf"


@pytest.mark.skipif(not REAL.exists(), reason="source data not present")
def test_real_dialysis_soa():
    res = sp.parse_pdf(REAL)
    assert len(res["rows"]) == 48
    assert res["meta"]["net"]["text"] == "733"
    codes = [r["room_code"] for r in res["rows"]]
    assert "IWDI 02.01.12" in codes
    r = next(x for x in res["rows"] if x["room_code"] == "IWDI 02.01.12")
    assert r["area_text"] == "294.9"  # printed value kept; flagged by the outlier check, never corrected
    st = {t["kind"]: t["status"] for t in sp.totals_check(res, "pdf")}
    assert st["net"] == st["circ"] == st["gross"] == "MATCH"
    assert any("outlier" in a for a in sp.anomalies({"rows": res["rows"], "orphans": []}))


@pytest.mark.skipif(not REAL.exists(), reason="source data not present")
def test_real_overflowing_code_cell_is_read_whole():
    f = PROJECT_ROOT / "data/source/DD Sign-OFF/01 - IP FAST/Submitted/03_SOA/20250414_AIGH_DD_SOA_V16_IP FAST.pdf"
    res = sp.parse_pdf(f)
    r = next(x for x in res["rows"] if x["room_code"] == "IWFB1 01.18.01")
    assert r["type_code"] == "042_LNPY-RH-PT_P1C0_01C0"
    assert json.loads(json.dumps(r["raw"]))["Unique Room Identifier"] == "IWFB1 01.18.01"
