"""Tests for src.rls_heights (D-019 ceiling heights from RLS PDFs)."""

import fitz
import pytest

from src import rls_heights as rh


@pytest.mark.parametrize("text,expected", [
    ("CH=2700", [2700]),
    ("CH 2700", [2700]),
    ("C.H.=2400", [2400]),
    ("C.H. 3000", [3000]),
    ("CLG HT 2700", [2700]),
    ("CLG. HEIGHT = 2400", [2400]),
    ("CEILING HEIGHT: 3000", [3000]),
    ("FCL 2700", [2700]),
    ("CH=2700-3000", [2700, 3000]),
    ("CH=2700/3000", [2700, 3000]),
    ("CH=2.7M", [2700]),
    ("(CH=2400)", [2400]),
])
def test_find_heights_variants(text, expected):
    hits = rh.find_heights(text)
    assert len(hits) == 1
    assert hits[0]["values_mm"] == expected


@pytest.mark.parametrize("text", ["CHAIR 2700", "CH-CB-01", "ARCH 2700", "2700", "CL-CB-01", "CH=12", "CHECKED 2025"])
def test_find_heights_rejects(text):
    assert rh.find_heights(text) == []


def test_range_flag():
    assert rh.find_heights("CH=2700-3000")[0]["range"] is True
    assert rh.find_heights("CH=2700")[0]["range"] is False


def test_parse_filename_rls_underscore():
    f = rh.parse_filename("RLS_074_DISP-15_P1C16_Disposal Room_Fever.pdf")
    assert (f["rls_number"], f["room_type_code"], f["variant"]) == ("074", "DISP-15", "P1C16")
    assert f["name_from_file"].startswith("Disposal Room")


def test_parse_filename_mp():
    f = rh.parse_filename("MP-RLS-32-PBTR-HR-STR-001 - Patient Bay.pdf")
    assert f["room_type_code"] == "PBTR-HR-STR"
    assert f["rls_number"] == "" and f["sheet_no"] == "001"   # trailing 001 is a sheet sequence, not an RLS number


def test_parse_filename_ar():
    f = rh.parse_filename("AR_RLS_32-037_P1C6-MULTI-PURPOSE ROOM-.pdf")
    assert f["rls_number"] == "037" and f["variant"] == "P1C6"


def test_parse_date():
    assert rh.parse_date("06/06/25") == "2025-06-06"
    assert rh.parse_date("1 JUN 2025") == "2025-06-01"
    assert rh.parse_date("garbage") is None
    assert rh.parse_date("31/02/25") is None


def test_title_block():
    lines = ["x", "ROOM LAYOUT SHEET", "MULTI-PURPOSE ROOM", "037_MPR_P1C6", "AR_RLS_32-037_P1C6", "MAY 2025"]
    tb = rh.title_block(lines)
    assert tb["room_name"] == "MULTI-PURPOSE ROOM"
    assert tb["code"] == "MPR"
    assert tb["month"] == "2025-05"
    assert tb["drawing_no"].startswith("AR_RLS_32")


def _make_pdf(path, texts):
    doc = fitz.open()
    page = doc.new_page(width=800, height=600)
    y = 50
    for t in texts:
        page.insert_text((50, y), t, fontsize=10)
        y += 20
    doc.save(str(path))
    doc.close()


def test_extract_pdf_reads_heights(tmp_path):
    p = tmp_path / "a.pdf"
    _make_pdf(p, ["ROOM LAYOUT SHEET", "TEST ROOM", "AR_RLS_32-001_P1C1", "CH=2700", "CH=2700", "CH=3000"])
    rec = rh.extract_pdf(str(p))
    vals = sorted(v for c in rec["candidates"] for v in c["values_mm"])
    assert vals == [2700, 2700, 3000]
    assert rec["candidates"][0]["page"] == 1 and len(rec["candidates"][0]["bbox"]) == 4
    assert rec["title"]["room_name"] == "TEST ROOM"


def test_extract_pdf_no_height_and_broken(tmp_path):
    p = tmp_path / "b.pdf"
    _make_pdf(p, ["nothing here", "2700"])
    assert rh.extract_pdf(str(p))["candidates"] == []
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    assert rh.extract_pdf(str(bad))["error"]


def _row(rel, sha="s"):
    return {"rel_path": rel, "sha256": sha, "local_path": rel}


def _cache(rel, cands, revs=(), title=None, sha="s", text_chars=500):
    return {"key": sha, "rel_path": rel, "sha256": sha, "dept_no": "04", "pages": 1, "text_chars": text_chars,
            "candidates": [{"kind": "CH", "text": "CH=%d" % v, "values_mm": [v], "range": False, "page": 1,
                            "bbox": [1, 2, 3, 4], "size": 5, "line": ""} for v in cands],
            "mentions": [], "title": title or {}, "revisions": list(revs), "error": ""}


def test_build_governing_modal_and_flag():
    rel = "DD Sign-OFF/04 - X/RLS_001_ABC_P1C1_Room.pdf"
    rows = rh.build_rows([_row(rel)], {"s": _cache(rel, [2700, 2700, 3000])})
    r = rows[0]
    assert r["governing_height_mm"] == 2700
    assert "several_heights" in r["conflict_flag"]
    assert r["room_type_code"] == "ABC" and r["rls_number"] == "001"


def test_build_tie_takes_lowest_and_low_confidence():
    rel = "DD Sign-OFF/04 - X/RLS_001_ABC_P1C1_Room.pdf"
    r = rh.build_rows([_row(rel)], {"s": _cache(rel, [2700, 3000])})[0]
    assert r["governing_height_mm"] == 2700 and "tie" in r["conflict_flag"] and r["confidence"] == "low"


def test_build_no_height_stays_unknown():
    rel = "DD Sign-OFF/04 - X/RLS_001_ABC_P1C1_Room.pdf"
    r = rh.build_rows([_row(rel)], {"s": _cache(rel, [], text_chars=0)})[0]
    assert r["governing_height_mm"] == ""
    assert "empty" in r["method"]


def test_governing_issue_latest_per_type():
    a = "DD Sign-OFF/04 - X/RLS_001_ABC_P1C1_Room.pdf"
    b = "DD Sign-OFF/04 - X/RLS_001_ABC_P1C1_Room_R2.pdf"
    rev = lambda d: [{"date_text": d, "date": rh.parse_date(d), "rev": "A", "description": "ISSUE", "page": 1, "y": 1}]
    cache = {"a": _cache(a, [2700], rev("01/01/25"), sha="a"), "b": _cache(b, [3000], rev("01/02/25"), sha="b")}
    rows = rh.build_rows([_row(a, "a"), _row(b, "b")], cache)
    gi = {r["rel_path"]: r["governing_issue"] for r in rows}
    assert gi[a] == "N" and gi[b] == "Y"
    assert "different heights" in [r for r in rows if r["rel_path"] == b][0]["governing_issue_note"]


def test_revision_rows_from_words():
    # DATE/REV/DESCRIPTION header at y=28..40, one row below
    words = [(2070, 28, 2088, 40, "DATE"), (2108, 28, 2121, 40, "REV"), (2213, 28, 2257, 40, "DESCRIPTION"),
             (2059, 39, 2078, 47, "06/06/25"), (2112, 39, 2118, 47, "A"), (2213, 39, 2240, 47, "ISSUE"),
             (2245, 39, 2260, 47, "FOR"), (2265, 39, 2280, 47, "DD")]
    rows = rh.revision_rows(words, 2384)
    assert len(rows) == 1
    assert rows[0]["date"] == "2025-06-06" and rows[0]["rev"] == "A" and rows[0]["description"] == "ISSUE FOR DD"


@pytest.mark.parametrize("name,num,var,code,multi", [
    ("MP-RLS-32-290_STDEC_S-001 - Decay Tank.pdf", "290", "", "STDEC", False),
    ("MP-RLS-32-042_LNPY-RH-PT_P1C0-001 - X.pdf", "042", "P1C0", "LNPY-RH-PT", False),
    ("20_AR-RLS-32-255_P1C1-001 - Foo.pdf", "255", "P1C1", "", False),
    ("02 AR-RLS-32-006_P1C7_271_P1C11-001-STORE - GENERAL_SS_R1.pdf", "006", "P1C7", "", True),
    ("RLS_004_STEQ_P1C39.pdf", "004", "P1C39", "STEQ", False),
    ("AR-RLS-32-009_071_P1C5-001-CHANGE ROOM.pdf", "009", "P1C5", "", True),
    ("RLS_005_SSTN_P1C10, 280_SBWT_P1C20_Reception.pdf", "005", "P1C10", "SSTN", True),
    ("MP-RLS-32-LAB-HAEMO-001B - Haematology.pdf", "", "", "LAB-HAEMO", False),
])
def test_parse_filename_numbers_variants_multi(name, num, var, code, multi):
    f = rh.parse_filename(name)
    assert (f["rls_number"], f["variant"], f["room_type_code"], f["multi_room"]) == (num, var, code, multi)


def test_parse_filename_prefixed_name_not_raw():
    f = rh.parse_filename("02 AR-RLS-32-006_P1C7_271_P1C11-001-STORE - GENERAL_SS_R1.pdf")
    assert f["name_from_file"].startswith("STORE - GENERAL") and f["sheet_no"] == "001"
    assert "271" in f["rooms_on_sheet"] and "P1C11" in f["rooms_on_sheet"]


def test_no_rls_number_for_mp_sheet_row():
    rel = "DD Sign-OFF/04 - X/MP-RLS-32-1BR-IC-B-001 - Room.pdf"
    r = rh.build_rows([_row(rel)], {"s": _cache(rel, [3000])})[0]
    assert r["rls_number"] == "" and r["sheet_no"] == "001" and r["room_type_code"] == "1BR-IC-B"


def test_ungrouped_row_is_explained():
    rel = "DD Sign-OFF/04 - X/RLS_STORE BLOOD_FLUID_PHARM_L5.pdf"
    r = rh.build_rows([_row(rel)], {"s": _cache(rel, [2700])})[0]
    assert r["governing_issue"] == "" and r["governing_issue_note"].startswith("not grouped")


def test_governing_issue_separates_variants():
    a = "DD Sign-OFF/04 - X/RLS_004_STEQ_P1C117_Room.pdf"
    b = "DD Sign-OFF/04 - X/RLS_004_STEQ_P1C83_Room.pdf"
    rev = lambda d: [{"date_text": d, "date": rh.parse_date(d), "rev": "A", "description": "ISSUE", "page": 1, "y": 1}]
    cache = {"a": _cache(a, [2700], rev("01/02/25"), sha="a"), "b": _cache(b, [2700], rev("01/01/25"), sha="b")}
    rows = rh.build_rows([_row(a, "a"), _row(b, "b")], cache)
    assert [r["governing_issue"] for r in rows] == ["Y", "Y"]


def test_lowest_height_kept_alongside_governing():
    rel = "DD Sign-OFF/04 - X/RLS_001_ABC_P1C1_Room.pdf"
    r = rh.build_rows([_row(rel)], {"s": _cache(rel, [2700, 2700, 2400])})[0]
    assert r["governing_height_mm"] == 2700 and r["lowest_height_mm"] == 2400
