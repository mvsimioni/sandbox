"""Tests for ``src.md_writer``: splitting, tables, front matter and whole bodies on synthetic sidecars."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List

import pytest
import yaml

from src import md_writer as mw
from src import recall_check as rc
from src import sidecar_pdf, sidecar_xlsx
from src.common import sha256_file
from tests.test_sidecar_pdf import build_fixture as build_pdf
from tests.test_sidecar_xlsx import build_workbook, inject_threaded


# --------------------------------------------------------------------------- #
# Splitting
# --------------------------------------------------------------------------- #
def test_split_pdf_text() -> None:
    segments, note = mw.split_pdf_text("one 1\n\n\x0ctwo 2\n\n\x0c", 2)
    assert segments == {1: "one 1\n\n", 2: "two 2\n\n"} and note is None
    segments, note = mw.split_pdf_text("joined text", 3)
    assert segments == {1: "joined text"} and "whole text is under Page 1" in note
    assert mw.split_pdf_text(None, 2) == ({}, None)
    assert mw.split_pdf_text("single", 1) == ({1: "single"}, None)


def test_split_pptx_text() -> None:
    text = "<!-- Slide number: 1 -->\n# A\n\n<!-- Slide number: 2 -->\n| x |\n"
    segments, note = mw.split_pptx_text(text, 2)
    assert segments == {1: "# A\n\n", 2: "| x |\n"} and note is None
    segments, note = mw.split_pptx_text("pre\n<!-- Slide number: 1 -->\nA", 1)
    assert segments[1] == "pre\nA" and "before" in note
    segments, note = mw.split_pptx_text("no markers", 3)
    assert segments == {1: "no markers"} and "no slide markers" in note


def test_split_xlsx_text() -> None:
    text = "## Rooms\n| a |\n| --- |\n\n## Loads & Flows\n| b |\n| --- |\n## Not a sheet\nmore"
    segments, note = mw.split_xlsx_text(text, ["Rooms", "Loads & Flows"])
    assert segments["Rooms"] == "| a |\n| --- |" and note is None
    assert segments["Loads & Flows"] == "| b |\n| --- |\n## Not a sheet\nmore"
    segments, note = mw.split_xlsx_text("plain", ["Only"])
    assert segments == {"Only": "plain"} and "no sheet headings" in note


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #
def test_md_table_escapes_and_pads() -> None:
    table = mw.md_table(("a", "b"), [("x|y", "line1\nline2"), ("short",), (None, 2.50)])
    lines = table.splitlines()
    assert lines[0] == "| a | b |" and lines[1] == "| --- | --- |"
    assert lines[2] == "| x\\|y | line1 line2 |" and lines[3] == "| short |  |" and lines[4] == "|  | 2.5 |"
    assert mw.grid_table([]) == "(empty table)"
    assert mw.grid_table([["h1", "h2"], ["1"]]).splitlines()[2] == "| 1 |  |"


def test_unit_from_number_format() -> None:
    assert mw.unit_from_number_format('0.0 "L/s"') == "L/s"
    assert mw.unit_from_number_format('#,##0" Pa"') == "Pa"
    assert mw.unit_from_number_format("0.0%") == "%"
    assert mw.unit_from_number_format("General") is None and mw.unit_from_number_format("#,##0.00") is None
    assert mw.unit_from_number_format('"a very long literal that is not a unit at all"') is None


def test_sheet_range_table_units_and_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    values = [{"cell": "A1", "value": "Room"}, {"cell": "B1", "value": "Flow"}, {"cell": "B2", "value": "1200"},
              {"cell": "A3", "value": "x"}]
    formats = {"B": {"dominant": '0 "L/s"'}}
    table, shown = mw.sheet_range_table(values, formats)
    assert shown == 3 and table.splitlines()[0] == "| row | A | B (L/s) |"
    assert "| 2 |  | 1200 |" in table
    monkeypatch.setattr(mw, "MAX_TABLE_ROWS", 2)
    capped, shown = mw.sheet_range_table(values, {})
    assert shown == 2 and "Rows 3 to 3 are not shown" in capped


def test_front_matter_order_and_duplicate() -> None:
    fields = {key: None for key in mw.FRONT_MATTER_KEYS}
    fields.update({"source_path": "Basis/b.pdf", "sha256": "ab", "grade": "pending", "converters": [{"name": "x", "version": "1"}]})
    text = mw.render_front_matter(fields)
    assert text.startswith("---\nsource_path: Basis/b.pdf\n") and text.endswith("---\n")
    parsed = yaml.safe_load(text.split("---\n")[1])
    assert list(parsed) == list(mw.FRONT_MATTER_KEYS)
    dup = mw.render_duplicate(fields, "Basis/a.pdf", "Basis/a.md")
    assert dup.count("\n---\n") == 1 and "# b.pdf" in dup and "Hash-identical duplicate of `Basis/a.pdf`" in dup
    assert "`Basis/a.md`" in dup


# --------------------------------------------------------------------------- #
# Whole bodies
# --------------------------------------------------------------------------- #
def _markitdown(path: Path) -> str:
    from markitdown import MarkItDown

    return MarkItDown(enable_plugins=False).convert(str(path)).text_content


def _headings(text: str) -> List[str]:
    return [line for line in text.splitlines() if re.match(r"^#{1,3} ", line)]


@pytest.fixture(scope="module")
def pdf_case(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, object]:
    base = tmp_path_factory.mktemp("md_writer_pdf")
    pdf = base / "30_Pharmacy_RDS.pdf"
    build_pdf(pdf)
    sidecar = base / "sidecar"
    manifest = sidecar_pdf.build_sidecars(pdf, sidecar, state_dir=base)
    source = mw.Source(path=pdf, rel_path="DD Sign-OFF/30 - Pharmacy/Submitted/30_Pharmacy_RDS.pdf",
                       sha256=sha256_file(pdf), bytes=pdf.stat().st_size, kind="pdf", drive_id="d1",
                       drive_modified="2026-01-01T00:00:00Z")
    md = mw.MarkItDownOutput(text=_markitdown(pdf), version="0.1.7", seconds=0.5)
    body = mw.build_body(source, md, sidecar, manifest)
    return {"pdf": pdf, "sidecar": sidecar, "manifest": manifest, "source": source, "md": md, "body": body}


def test_pdf_body_layout(pdf_case: Dict[str, object]) -> None:
    body: mw.Body = pdf_case["body"]  # type: ignore[assignment]
    text = body.text
    heads = _headings(text)
    assert heads[0] == "# 30_Pharmacy_RDS.pdf" and heads[1] == "## Document"
    pages = [h for h in heads if h.startswith("## Page ")]
    assert pages == ["## Page 1", "## Page 2", "## Page 3"] and body.units == pages
    # inside each unit the order is markitdown, native text, tables, annotations
    for n in (1, 2, 3):
        unit = text.split(f"## Page {n}\n")[1].split("<!-- section: unit -->")[0]
        positions = [unit.find(s) for s in ("<!-- method: markitdown -->", "### Native text (sidecar)",
                                            "<!-- method: pymupdf-text -->", "### Tables", "### Annotations and comments")]
        assert positions == sorted(positions) and -1 not in positions, n
    assert heads[-1] == "## Comments register"
    assert body.comment_rows == 3 and body.tables_written >= 1
    register = text.split("## Comments register")[1]
    assert register.count("\n| ") == 3 + 2  # header, separator, three rows
    assert "| Marcus |" in register and "Rooms (measured)" in register
    assert "Zone" in register and re.search(r"\| 3 \| page 2 \| Text \| Bob \|.*\| 2 \|", register)
    page2 = text.split("## Page 2\n")[1].split("## Page 3\n")[0]
    assert "Drawing page: every text block" in page2 and "IP-BH-05010 REV B |" in page2
    assert "Title block (parsed): drawing_number IP-BH-05010" in page2
    page1 = text.split("## Page 1\n")[1].split("## Page 2\n")[0]
    assert "Table P1-1: pdfplumber, page 1," in page1 and "| PH-303 |" in page1


def test_pdf_checks_and_document(pdf_case: Dict[str, object]) -> None:
    body: mw.Body = pdf_case["body"]  # type: ignore[assignment]
    md: mw.MarkItDownOutput = pdf_case["md"]  # type: ignore[assignment]
    checks = rc.run_checks("pdf", pdf_case["sidecar"], body.text, md.text, sidecar_added=body.sidecar_added)  # type: ignore[arg-type]
    assert checks["grade"] in ("A", "B"), checks
    assert checks["tables"]["match"] and checks["annotations"]["match"] and checks["numeric_recall"] >= 0.995
    fields = mw.front_matter_fields(pdf_case["source"], md, pdf_case["manifest"], body, checks["grade"],  # type: ignore[arg-type]
                                    checks["grade_reason"], "t")
    assert list(fields) == list(mw.FRONT_MATTER_KEYS)
    assert fields["dept_no"] == "30" and fields["stage"] == "Submitted" and fields["doc_class"] == "RDS"
    assert fields["pages_or_slides_or_sheets"] == 3 and fields["converters"][0]["name"] == "markitdown"
    assert {"pymupdf", "pdfplumber"} <= {c["name"] for c in fields["converters"]}
    provenance = mw.render_provenance(pdf_case["source"], md, "out/sidecar/x", pdf_case["manifest"], None, body, checks)  # type: ignore[arg-type]
    assert provenance.startswith("<!-- section: provenance -->\n## Provenance")
    assert "`words.jsonl`" in provenance and "### Conversion checks" in provenance and f"Grade **{checks['grade']}**" in provenance
    document = mw.render_document(fields, body, provenance)
    assert document.startswith("---\n") and rc.body_text(document).strip() == body.text.strip()


def test_pdf_fallback_when_sidecar_failed(pdf_case: Dict[str, object]) -> None:
    md: mw.MarkItDownOutput = pdf_case["md"]  # type: ignore[assignment]
    body = mw.build_body(pdf_case["source"], md, Path("/nonexistent"), None, "boom")  # type: ignore[arg-type]
    assert body.units == ["## Page 1"] and "Sidecar extraction failed: boom" in body.text
    assert "(no tables)" in body.text and "(no annotations or comments in this file)" in body.text


def test_pdf_markitdown_failure_moves_words_to_sidecar(pdf_case: Dict[str, object]) -> None:
    md = mw.MarkItDownOutput(text=None, version="0.1.7", error="RuntimeError: x")
    body = mw.build_body(pdf_case["source"], md, pdf_case["sidecar"], pdf_case["manifest"])  # type: ignore[arg-type]
    assert "(MarkItDown produced no text: RuntimeError: x)" in body.text and body.sidecar_added > 0
    checks = rc.run_checks("pdf", pdf_case["sidecar"], body.text, None, sidecar_added=body.sidecar_added,  # type: ignore[arg-type]
                           markitdown_error=md.error)
    assert checks["grade"] == "B" and checks["numeric_recall"] >= 0.995


def test_xlsx_body(tmp_path: Path) -> None:
    plain, threaded = tmp_path / "plain.xlsx", tmp_path / "30_Pharmacy_SOA.xlsx"
    build_workbook(plain)
    inject_threaded(plain, threaded)
    sidecar = tmp_path / "sidecar"
    manifest = sidecar_xlsx.run_one(threaded, sidecar, tmp_path)
    source = mw.Source(path=threaded, rel_path="DD Sign-OFF/30 - Pharmacy/Submitted/30_Pharmacy_SOA.xlsx",
                       sha256=sha256_file(threaded), bytes=threaded.stat().st_size, kind="xlsx")
    md = mw.MarkItDownOutput(text=_markitdown(threaded), version="0.1.7")
    body = mw.build_body(source, md, sidecar, manifest)
    assert body.units == ["## Sheet Loads", "## Sheet Hidden Data (2)"]
    assert "Table Loads: sheet range A1:D5" in body.text and "| row | A | B | C | D |" in body.text
    assert "<!-- method: openpyxl -->" in body.text and body.comment_rows == 4
    register = body.text.split("## Comments register")[1]
    assert "| cell D2 |" in register and "threaded reply" in register and "legacy note (threaded shadow)" in register
    checks = rc.run_checks("xlsx", sidecar, body.text, md.text, sidecar_added=body.sidecar_added)
    assert checks["grade"] in ("A", "B") and checks["tables"] == {"sidecar": 2, "markdown": 2, "match": True}
    assert checks["annotations"]["match"]
    # a hidden sheet with an unsplittable text falls back to the whole text under the first sheet
    segments, note = mw.split_xlsx_text("no headings here 7", ["Loads", "Hidden Data (2)"])
    assert segments == {"Loads": "no headings here 7"} and note


def test_render_from_sidecar_cli(tmp_path: Path) -> None:
    pdf = tmp_path / "a.pdf"
    build_pdf(pdf)
    sidecar = tmp_path / "sc"
    sidecar_pdf.build_sidecars(pdf, sidecar, state_dir=tmp_path)
    raw = tmp_path / "raw.txt"
    raw.write_text(_markitdown(pdf), encoding="utf-8")
    out = tmp_path / "a.md"
    assert mw.main(["render", "--source", str(pdf), "--rel-path", "Basis/a.pdf", "--sidecar", str(sidecar),
                    "--out", str(out), "--markitdown-text", str(raw)]) == 0
    text = out.read_text(encoding="utf-8")
    assert text.startswith("---\nsource_path: Basis/a.pdf\n") and "## Provenance" in text
    assert json.loads((sidecar / rc.CHECKS_NAME).read_text(encoding="utf-8"))["grade"] in ("A", "B")
    assert mw.main(["render", "--source", str(tmp_path / "nope.pdf"), "--rel-path", "x", "--sidecar", str(sidecar),
                    "--out", str(out)]) == 1


def test_clean_text_drops_control_characters() -> None:
    assert mw.clean_text("PM-AIGH\x00\r\n\x0cnext\ttab\x1b") == "PM-AIGH\n\nnext\ttab"
    assert mw.md_cell("a\x00|b") == "a\\|b"
    assert mw.markitdown_block("x\x00y\n", None) == "xy"
