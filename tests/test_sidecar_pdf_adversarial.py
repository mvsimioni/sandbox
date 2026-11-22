"""Adversarial tests for ``src.sidecar_pdf`` written by the independent reviewer.

Every fixture is synthetic and built with PyMuPDF; no real source file is read.
Tests marked ``xfail(strict=True)`` document behaviour that the review found
wrong or missing (see ``out/qa/verify/code_sidecar_pdf_review.md``); they will
start failing (XPASS) once the code is fixed, at which point the marker is to
be removed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Dict, List

import pymupdf
import pytest

from src import sidecar_pdf
from src.common import read_jsonl

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _view_point(page: pymupdf.Page, x: float, y: float) -> List[float]:
    """PyMuPDF's own page-view transform of a raw point (independent of the module)."""
    p = pymupdf.Point(x, y) * page.rotation_matrix
    return [round(p.x, 3), round(p.y, 3)]


def _view_rect(page: pymupdf.Page, rect: pymupdf.Rect) -> List[float]:
    """PyMuPDF's own page-view transform of a raw rect, normalised."""
    r = (pymupdf.Rect(rect) * page.rotation_matrix).normalize()
    return [round(v, 3) for v in (r.x0, r.y0, r.x1, r.y1)]


def _text_page(doc: pymupdf.Document, rotation: int = 0, with_table: bool = True) -> pymupdf.Page:
    """An A4 page with enough text to be a text page, optionally a 6 x 3 text table."""
    page = doc.new_page(width=595, height=842)
    for i in range(8):
        page.insert_text((50, 60 + i * 14), f"Line {i} of the pharmacy air-side design narrative for level {i + 1}.",
                         fontsize=10)
    if with_table:
        for r in range(6):
            for c in range(3):
                page.insert_text((60 + c * 120, 300 + r * 18), f"R{r}C{c} {100 * r + c}", fontsize=10)
    if rotation:
        page.set_rotation(rotation)
    return page


def _run(pdf: Path, out: Path, **kwargs) -> Dict:
    return sidecar_pdf.build_sidecars(pdf, out, state_dir=out.parent, **kwargs)


# --------------------------------------------------------------------------- #
# 1. Rotated pages: 180 and 270, annotation geometry must follow page.rotation_matrix
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rotation", [180, 270])
def test_rotated_page_geometry_matches_pymupdf(tmp_path: Path, rotation: int) -> None:
    pdf = tmp_path / "rot.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((40, 60), "Corner text 123", fontsize=12)
    poly = page.add_polygon_annot([pymupdf.Point(50, 50), pymupdf.Point(150, 50), pymupdf.Point(100, 120)])
    poly.update()
    square = page.add_rect_annot(pymupdf.Rect(200, 100, 300, 200))
    square.update()
    page.set_rotation(rotation)
    doc.save(str(pdf))
    doc.close()

    _run(pdf, tmp_path / "out")
    doc = pymupdf.open(str(pdf))
    page = doc[0]
    info = json.loads((tmp_path / "out" / "page_info.json").read_text())["pages"][0]
    assert info["rotation"] == rotation
    expected_size = (300.0, 400.0) if rotation == 270 else (400.0, 300.0)
    assert (info["width_pt"], info["height_pt"]) == expected_size

    annots = {a["type"]: a for a in read_jsonl(tmp_path / "out" / "annotations.jsonl")}
    poly_row = annots["Polygon"]
    assert poly_row["vertices_raw"] == [[50.0, 50.0], [150.0, 50.0], [100.0, 120.0]]
    assert poly_row["vertices_view"] == [_view_point(page, 50, 50), _view_point(page, 150, 50),
                                         _view_point(page, 100, 120)]
    assert poly_row["rect_view"] == pytest.approx(_view_rect(page, pymupdf.Rect(poly_row["rect_raw"])), abs=0.01)
    sq = annots["Square"]
    assert sq["rect_view"] == pytest.approx(_view_rect(page, pymupdf.Rect(sq["rect_raw"])), abs=0.01)

    words = read_jsonl(tmp_path / "out" / "words.jsonl")
    w = next(x for x in words if x["text"] == "Corner")
    assert w["bbox_view"] == pytest.approx(_view_rect(page, pymupdf.Rect(w["bbox_raw"])), abs=0.01)
    assert all(0 <= v <= max(expected_size) for v in w["bbox_view"])
    assert w["rotation_view"] == (0 - rotation) % 360
    doc.close()


# --------------------------------------------------------------------------- #
# 2. Highlight (quadpoints) and Ink (inklist) on a 90-degree page
# --------------------------------------------------------------------------- #
def test_quadpoints_and_inklist_on_rotated_page(tmp_path: Path) -> None:
    pdf = tmp_path / "marks.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((40, 60), "Highlight me please 456", fontsize=12)
    quad = pymupdf.Rect(38, 48, 200, 64).quad
    hl = page.add_highlight_annot(quad)
    hl.update()
    ink = page.add_ink_annot([[(10, 10), (20, 30), (30, 10)], [(100, 100), (120, 120)]])
    ink.update()
    page.set_rotation(90)
    doc.save(str(pdf))
    doc.close()

    _run(pdf, tmp_path / "out")
    doc = pymupdf.open(str(pdf))
    page = doc[0]
    rows = {a["type"]: a for a in read_jsonl(tmp_path / "out" / "annotations.jsonl")}
    hl_row = rows["Highlight"]
    assert hl_row["quadpoints_raw"] and len(hl_row["quadpoints_raw"][0]) == 4
    assert hl_row["vertices_raw"] is None and hl_row["inklist_raw"] is None
    for raw, view in zip(hl_row["quadpoints_raw"][0], hl_row["quadpoints_view"][0]):
        assert view == _view_point(page, *raw)
    ink_row = rows["Ink"]
    assert [len(s) for s in ink_row["inklist_raw"]] == [3, 2]
    assert ink_row["inklist_raw"][1] == [[100.0, 100.0], [120.0, 120.0]]
    assert ink_row["inklist_view"][1] == [_view_point(page, 100, 100), _view_point(page, 120, 120)]
    assert ink_row["quadpoints_raw"] is None
    doc.close()


# --------------------------------------------------------------------------- #
# 3. Zero annotations
# --------------------------------------------------------------------------- #
def test_pdf_with_zero_annotations(tmp_path: Path) -> None:
    pdf = tmp_path / "plain.pdf"
    doc = pymupdf.open()
    _text_page(doc)
    doc.save(str(pdf))
    doc.close()
    out = tmp_path / "out"
    manifest = _run(pdf, out)
    assert (out / "annotations.jsonl").is_file() and read_jsonl(out / "annotations.jsonl") == []
    assert manifest["counts"]["annotations"] == 0
    info = json.loads((out / "page_info.json").read_text())
    assert info["pages"][0]["annotation_count"] == 0 and info["ocgs"] == []
    assert manifest["files"]["annotations.jsonl"]["bytes"] == 0
    # an empty sidecar must still count as "done" on the second run
    assert _run(pdf, out)["skipped"] is True


# --------------------------------------------------------------------------- #
# 4. FreeText with no contents, a two-deep reply chain and a dangling /IRT
# --------------------------------------------------------------------------- #
def test_freetext_without_contents_and_reply_chain(tmp_path: Path) -> None:
    pdf = tmp_path / "replies.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    free = page.add_freetext_annot(pymupdf.Rect(50, 50, 250, 90), "", fontsize=9)
    free.set_info(title="Ana", subject="Empty")
    free.update()
    r1 = page.add_text_annot((60, 60), "first reply")
    r1.set_info(title="Bob")
    r1.update()
    r2 = page.add_text_annot((70, 70), "second reply")
    r2.set_info(title="Cy")
    r2.update()
    orphan = page.add_text_annot((80, 80), "orphan")
    orphan.update()
    doc.xref_set_key(r1.xref, "IRT", f"{free.xref} 0 R")
    doc.xref_set_key(r2.xref, "IRT", f"{r1.xref} 0 R")
    doc.xref_set_key(r2.xref, "RT", "/Group")
    doc.xref_set_key(orphan.xref, "IRT", "999 0 R")  # dangling reference
    xrefs = {"free": free.xref, "r1": r1.xref, "r2": r2.xref, "orphan": orphan.xref}
    doc.save(str(pdf))
    doc.close()

    _run(pdf, tmp_path / "out")
    rows = read_jsonl(tmp_path / "out" / "annotations.jsonl")
    assert len(rows) == 4 and all(r["type"] != "error" for r in rows)
    by_xref = {r["xref"]: r for r in rows}
    ft = by_xref[xrefs["free"]]
    assert ft["type"] == "FreeText" and ft["contents"] == "" and ft["title"] == "Ana"
    assert ft["default_appearance"] and "Tf" in ft["default_appearance"]
    assert by_xref[xrefs["r1"]]["irt_xref"] == xrefs["free"]
    assert by_xref[xrefs["r2"]]["irt_xref"] == xrefs["r1"] and by_xref[xrefs["r2"]]["reply_type"] == "/Group"
    # chain rebuild: r2 -> r1 -> free -> None
    chain = []
    cur = xrefs["r2"]
    while cur is not None and cur in by_xref:
        chain.append(cur)
        cur = by_xref[cur]["irt_xref"]
    assert chain == [xrefs["r2"], xrefs["r1"], xrefs["free"]]
    # a dangling IRT must not crash and must not resolve to a row of this file
    assert by_xref[xrefs["orphan"]]["irt_xref"] in (999, None)
    assert 999 not in by_xref


# --------------------------------------------------------------------------- #
# 5. Unicode file names with spaces and brackets, in and out
# --------------------------------------------------------------------------- #
def test_unicode_paths_with_spaces_and_brackets(tmp_path: Path) -> None:
    src_dir = tmp_path / "30 - Pharmacy [DD Closure] (ünïcödé)"
    src_dir.mkdir()
    pdf = src_dir / "AHU Zoning – RFI-ME-0233 [Rev A] (copy).pdf"
    doc = pymupdf.open()
    _text_page(doc)
    doc.save(str(pdf))
    doc.close()
    out = tmp_path / "out [sidecar] (ünï)"
    rc = sidecar_pdf.main(["run", "--pdf", str(pdf), "--out", str(out), "--no-progress"])
    assert rc == 0
    manifest = json.loads((out / "sidecar_manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_path"] == str(pdf) and manifest["counts"]["tables"] >= 1
    assert pdf.name in (out / "notes.md").read_text(encoding="utf-8")
    assert list(out.glob("tables_p1_*.csv"))
    # second run through the CLI is a no-op; a forced run clears and rewrites the table CSVs
    assert sidecar_pdf.main(["run", "--pdf", str(pdf), "--out", str(out), "--no-progress"]) == 0
    stale = out / "tables_p9_9.csv"
    stale.write_text("stale\n")
    assert sidecar_pdf.main(["run", "--pdf", str(pdf), "--out", str(out), "--no-progress", "--force"]) == 0
    assert not stale.exists()


# --------------------------------------------------------------------------- #
# 6. Tables on a rotated text page: bbox must be in page view
# --------------------------------------------------------------------------- #
def _rotated_table_run(tmp_path: Path) -> Path:
    pdf = tmp_path / "rot_table.pdf"
    doc = pymupdf.open()
    _text_page(doc, rotation=90)
    doc.save(str(pdf))
    doc.close()
    out = tmp_path / "out"
    manifest = _run(pdf, out)
    info = json.loads((out / "page_info.json").read_text())["pages"][0]
    assert info["is_text_page"] and (info["width_pt"], info["height_pt"]) == (842.0, 595.0)
    tables = json.loads((out / "tables.json").read_text())
    assert tables and manifest["counts"]["tables"] == len(tables)
    return out


def test_tables_on_rotated_text_page_bbox_is_page_view(tmp_path: Path) -> None:
    out = _rotated_table_run(tmp_path)
    for t in json.loads((out / "tables.json").read_text()):
        x0, y0, x1, y1 = t["bbox"]
        assert 0 <= x0 < x1 <= 842.0 and 0 <= y0 < y1 <= 595.0, t
        assert (out / t["file"]).is_file()


@pytest.mark.xfail(strict=True, reason="BUG: on a /Rotate 90 text page pdfplumber sees vertical glyphs and the "
                                       "table CSVs hold one character per cell (R5,R4,... / C,C,C / ne,ne,ne)")
def test_tables_on_rotated_text_page_keep_cells_intact(tmp_path: Path) -> None:
    out = _rotated_table_run(tmp_path)
    tables = json.loads((out / "tables.json").read_text())
    best = max(tables, key=lambda t: t["rows"] * t["cols"])
    text = (out / best["file"]).read_text()
    assert "R0C0" in text and "502" in text


# --------------------------------------------------------------------------- #
# 7. Encrypted PDF, --max-pages 0, blank page
# --------------------------------------------------------------------------- #
def test_encrypted_pdf_fails_cleanly(tmp_path: Path) -> None:
    pdf = tmp_path / "locked.pdf"
    doc = pymupdf.open()
    _text_page(doc)
    doc.save(str(pdf), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner")
    doc.close()
    out = tmp_path / "out"
    assert sidecar_pdf.main(["run", "--pdf", str(pdf), "--out", str(out), "--no-progress"]) == 1
    assert not (out / "sidecar_manifest.json").exists()


def test_max_pages_zero_and_blank_page(tmp_path: Path) -> None:
    pdf = tmp_path / "blank.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.new_page()
    doc.save(str(pdf))
    doc.close()
    out0 = tmp_path / "out0"
    m0 = _run(pdf, out0, max_pages=0)
    assert m0["pages_processed"] == 0 and m0["page_count"] == 2
    assert all((out0 / name).is_file() for name in m0["files"])
    assert json.loads((out0 / "page_info.json").read_text())["pages"] == []
    out = tmp_path / "out"
    m = _run(pdf, out)
    assert m["counts"]["words"] == 0 and read_jsonl(out / "words.jsonl") == []
    info = json.loads((out / "page_info.json").read_text())["pages"][0]
    assert info["text_chars"] == 0 and info["page_class"] == "mixed" and not info["is_text_page"]
    assert (out / "blocks.csv").read_text().splitlines()[0] == ",".join(sidecar_pdf.BLOCK_COLUMNS)
    # a different --max-pages is a different run key
    assert _run(pdf, out, max_pages=1)["skipped"] is False


# --------------------------------------------------------------------------- #
# 8. Widgets and links are not in page.annots(): documented scope of annotation_count
# --------------------------------------------------------------------------- #
def test_widget_and_link_are_outside_annotation_count(tmp_path: Path) -> None:
    pdf = tmp_path / "form.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(10, 10, 100, 30), "uri": "about:blank"})
    widget = pymupdf.Widget()
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    widget.field_name = "f1"
    widget.rect = pymupdf.Rect(10, 50, 200, 80)
    page.add_widget(widget)
    page.add_text_annot((300, 300), "note")
    doc.save(str(pdf))
    doc.close()
    _run(pdf, tmp_path / "out")
    rows = read_jsonl(tmp_path / "out" / "annotations.jsonl")
    # Current behaviour: only markup annotations; Widget and Link are silently left out.
    assert [r["type"] for r in rows] == ["Text"]
    info = json.loads((tmp_path / "out" / "page_info.json").read_text())["pages"][0]
    assert info["annotation_count"] == 1


# --------------------------------------------------------------------------- #
# 9. Known defects (strict xfail: flip to XPASS when fixed)
# --------------------------------------------------------------------------- #
@pytest.mark.xfail(strict=True, reason="BUG: first drawing-number-like string wins, even a cross-reference; "
                                       "the value next to the DRAWING NO label is ignored")
def test_title_block_prefers_labelled_drawing_number() -> None:
    # Mirrors the real Template zoning sheet: an elevation cross-reference appears before the label.
    strings = [{"text": t} for t in [
        "REFER AR-IP-31-ZZ-002 FOR ELEVATION", "DRAWING TITLE", "01-ZONE PLANS", "4TH STOREY (IP)",
        "DRAWING NO.", "REV.", "JOB NO.", "A", "TENDER CORRIGENDUM 6", "AR-IP-01-04-001", "SCALE 1 : 250",
    ]]
    parsed = sidecar_pdf.parse_title_block(strings)
    assert parsed["drawing_number"] == "AR-IP-01-04-001"
    assert parsed["revision"] == "A"


@pytest.mark.xfail(strict=True, reason="BUG: PyMuPDF's -1 'no border' sentinel is written as border_width")
def test_border_width_sentinel_is_not_leaked(tmp_path: Path) -> None:
    pdf = tmp_path / "nb.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    poly = page.add_polygon_annot([pymupdf.Point(10, 10), pymupdf.Point(60, 10), pymupdf.Point(30, 50)])
    poly.update()
    doc.xref_set_key(poly.xref, "Border", "null")
    doc.xref_set_key(poly.xref, "BS", "null")
    doc.save(str(pdf))
    doc.close()
    _run(pdf, tmp_path / "out")
    row = read_jsonl(tmp_path / "out" / "annotations.jsonl")[0]
    assert row["border_width"] is None or row["border_width"] >= 0


@pytest.mark.xfail(strict=True, reason="BUG: a broken annotation becomes a type='error' row that the manifest "
                                       "counts as an annotation and never mentions in its notes")
def test_error_annotation_rows_are_surfaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = tmp_path / "bad.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.add_text_annot((10, 10), "ok")
    page.add_text_annot((50, 50), "boom")
    doc.save(str(pdf))
    doc.close()
    real = sidecar_pdf.annotation_record

    def flaky(doc, page, annot, number, index, ocgs):  # noqa: ANN001
        if index == 1:
            raise ValueError("synthetic damage")
        return real(doc, page, annot, number, index, ocgs)

    monkeypatch.setattr(sidecar_pdf, "annotation_record", flaky)
    manifest = _run(pdf, tmp_path / "out")
    rows = read_jsonl(tmp_path / "out" / "annotations.jsonl")
    assert any(r.get("type") == "error" for r in rows)
    assert manifest["counts"]["annotations"] == 1  # only the good one
    assert any("annotation" in n and "error" in n.lower() for n in manifest["notes"])


@pytest.mark.xfail(strict=True, reason="BUG: in a worker thread the SIGALRM budget raises ValueError, which is "
                                       "swallowed; every page is then classed vector_drawing with -1 paths")
def test_worker_thread_does_not_silently_degrade(tmp_path: Path) -> None:
    pdf = tmp_path / "t.pdf"
    doc = pymupdf.open()
    _text_page(doc)
    doc.save(str(pdf))
    doc.close()
    script = textwrap.dedent(f"""
        import json, sys, threading
        sys.path.insert(0, {str(PROJECT_ROOT)!r})
        from src import sidecar_pdf
        res = {{}}
        def work():
            res["m"] = sidecar_pdf.build_sidecars({str(pdf)!r}, {str(tmp_path / 'out')!r}, state_dir={str(tmp_path)!r})
        t = threading.Thread(target=work); t.start(); t.join()
        info = json.load(open({str(tmp_path / 'out' / 'page_info.json')!r}))["pages"][0]
        print(json.dumps({{"paths": info["vector_path_count"], "cls": info["page_class"], "notes": info["notes"]}}))
    """)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout.strip().splitlines()[-1])
    assert data["paths"] >= 0 and data["cls"] == "text", data
