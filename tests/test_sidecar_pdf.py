"""Tests for ``src.sidecar_pdf`` on small synthetic PDFs built with PyMuPDF.

The fixture has three pages: an A4 text page with a table drawn as text rows,
a notes block and a small image; an A1 drawing page with 600 rectangles, a
title block, a NOTES block, an OCG layer and three annotations (a Polygon with
vertices on the layer, a FreeText and a Text reply linked by ``/IRT``); and a
small page rotated 90 degrees to exercise the page-view transform.  No real
source file is used.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pymupdf
import pytest

from src import sidecar_pdf
from src.common import read_csv_dicts, read_jsonl
from src.state import Progress

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Fixture
# --------------------------------------------------------------------------- #
def _text_page(doc: pymupdf.Document) -> None:
    """A4 page: prose, a five-column table as text rows, a GENERAL NOTES block and an image."""
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 60), "Pharmacy ACMV room data summary for Level 3, issued for DD closure review.", fontsize=10)
    page.insert_text((50, 76), "This document lists the air-side design values for each room in the department.", fontsize=10)
    header = ["Room", "Area m2", "Supply L/s", "Pressure Pa", "Load kW"]
    rows = [["PH-301", "24.5", "180", "+15", "2.4"], ["PH-302", "18.0", "120", "-5", "1.8"],
            ["PH-303", "32.75", "1,200", "0", "4.1"], ["PH-304", "12.0", "95", "+10", "0.9"],
            ["PH-305", "40.0", "600x300", "+5", "3.6"]]
    xs = [50, 150, 250, 350, 450]
    for ci, cell in enumerate(header):
        page.insert_text((xs[ci], 120), cell, fontsize=10)
    for ri, row in enumerate(rows):
        for ci, cell in enumerate(row):
            page.insert_text((xs[ci], 140 + ri * 16), cell, fontsize=10)
    page.insert_text((50, 300), ["GENERAL NOTES", "1. All flows in L/s at 1.2 kg/m3.",
                                 "2. Pressures relative to corridor."], fontsize=10)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 6), 0)
    pix.clear_with(180)
    page.insert_image(pymupdf.Rect(400, 400, 480, 460), pixmap=pix)


def _drawing_page(doc: pymupdf.Document) -> Dict[str, int]:
    """A1 page: 600 rectangles, OCG layer, title block, NOTES block and three annotations."""
    page = doc.new_page(width=2384, height=1684)
    for i in range(600):
        x, y = 60 + (i % 40) * 40, 60 + (i // 40) * 60
        fill = (1, 0, 0) if i % 3 == 0 else ((0, 0, 1) if i % 3 == 1 else None)
        page.draw_rect(pymupdf.Rect(x, y, x + 25, y + 25), color=(0, 0, 0), fill=fill)
    ocg = doc.add_ocg("Rooms (measured)", on=True)
    page.draw_rect(pymupdf.Rect(300, 1200, 500, 1300), color=(0, 1, 0), oc=ocg)
    page.insert_text((2000, 1500), "DRAWING TITLE", fontsize=9)
    page.insert_text((2000, 1515), "INPATIENT 3RD STOREY AIR-SIDE SCHEMATIC POD 2", fontsize=9)
    page.insert_text((2000, 1560), "DRAWING NO", fontsize=9)
    page.insert_text((2000, 1575), "IP-BH-05010", fontsize=12)
    page.insert_text((2200, 1575), "REV B", fontsize=9)
    page.insert_text((2000, 1620), "SCALE 1:100", fontsize=9)
    page.insert_text((2200, 1620), "DATE 12/11/2026", fontsize=9)
    page.insert_text((100, 1550), ["NOTES", "1. Duct sizes 600x300 unless noted.",
                                   "2. Refer IP-BH-05011 for pod 3."], fontsize=10)
    page.insert_text((1200, 100), "AHU-3-01 2400 L/s", fontsize=10)
    polygon = page.add_polygon_annot([pymupdf.Point(500, 500), pymupdf.Point(700, 500), pymupdf.Point(600, 650)])
    polygon.set_info(title="Marcus", subject="Zone", content="AHU-3-01 zone")
    polygon.set_colors(stroke=(1, 0, 0), fill=(0, 0.5, 0))
    polygon.set_opacity(0.5)
    polygon.set_oc(ocg)
    polygon.update()
    freetext = page.add_freetext_annot(pymupdf.Rect(900, 900, 1100, 960), "Confirm flow 2400 L/s", fontsize=10)
    freetext.set_info(title="Ana", subject="Query", content="Confirm flow 2400 L/s")
    freetext.update()
    reply = page.add_text_annot(pymupdf.Point(920, 910), "Confirmed by Jacobs")
    reply.set_info(title="Bob", subject="Re: Query")
    reply.update()
    doc.xref_set_key(reply.xref, "IRT", f"{freetext.xref} 0 R")
    doc.xref_set_key(reply.xref, "RT", "/R")
    doc.xref_set_key(polygon.xref, "CreationDate", "(D:20261112093000+08'00')")
    return {"ocg": ocg, "polygon": polygon.xref, "freetext": freetext.xref, "reply": reply.xref}


def _rotated_page(doc: pymupdf.Document) -> None:
    """A 400 x 200 pt page rotated 90 degrees with one line of text."""
    page = doc.new_page(width=400, height=200)
    page.insert_text((300, 150), "ROTATED 42", fontsize=12)
    page.set_rotation(90)


def build_fixture(path: Path) -> Dict[str, int]:
    """Write the three-page fixture PDF and return the xrefs of its annotations and OCG."""
    doc = pymupdf.open()
    _text_page(doc)
    xrefs = _drawing_page(doc)
    _rotated_page(doc)
    doc.save(str(path))
    doc.close()
    return xrefs


@pytest.fixture(scope="module")
def fixture_pdf(tmp_path_factory: pytest.TempPathFactory) -> Tuple[Path, Dict[str, int]]:
    """Build the fixture once per module."""
    path = tmp_path_factory.mktemp("fixture") / "fixture.pdf"
    return path, build_fixture(path)


@pytest.fixture(scope="module")
def run(fixture_pdf: Tuple[Path, Dict[str, int]], tmp_path_factory: pytest.TempPathFactory):
    """Run the extractor once and expose (pdf, xrefs, out_dir, manifest)."""
    pdf, xrefs = fixture_pdf
    out = tmp_path_factory.mktemp("out")
    manifest = sidecar_pdf.build_sidecars(pdf, out, state_dir=tmp_path_factory.mktemp("state"))
    return pdf, xrefs, out, manifest


def _page_rows(rows: List[dict], page: int) -> List[dict]:
    return [r for r in rows if r["page"] == page]


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
def test_all_files_written(run) -> None:
    _, _, out, manifest = run
    expected = {"words.jsonl", "blocks.csv", "page_info.json", "annotations.jsonl", "images.json",
                "fills.json", "tables.json", "titleblock.json", "notes.md", "sidecar_manifest.json"}
    assert expected <= {p.name for p in out.iterdir()}
    assert manifest["skipped"] is False
    assert manifest["pages_processed"] == 3 and manifest["page_count"] == 3
    assert {c["name"] for c in manifest["converters"]} >= {"pymupdf", "pdfplumber"}
    for name, entry in manifest["files"].items():
        assert (out / name).is_file() and entry["bytes"] > 0 and len(entry["sha256"]) == 64


def test_words_have_raw_and_view_boxes(run) -> None:
    _, _, out, _ = run
    words = read_jsonl(out / "words.jsonl")
    assert {"page", "block", "line", "word_no", "text", "bbox_raw", "bbox_view", "rotation"} <= set(words[0])
    texts = {w["text"] for w in words}
    assert {"IP-BH-05010", "PH-303", "1,200", "600x300"} <= texts
    rotated = [w for w in words if w["text"] == "ROTATED"][0]
    assert rotated["page"] == 3
    raw = rotated["bbox_raw"]
    # Page 3 is 400 x 200 rotated 90: view x = 200 - raw y, view y = raw x.
    assert rotated["bbox_view"] == pytest.approx([200 - raw[3], raw[0], 200 - raw[1], raw[2]], abs=0.01)
    assert rotated["rotation"] == 0 and rotated["rotation_view"] == 270


def test_blocks_csv(run) -> None:
    _, _, out, _ = run
    blocks = read_csv_dicts(out / "blocks.csv")
    assert list(blocks[0].keys()) == list(sidecar_pdf.BLOCK_COLUMNS)
    page2 = [b for b in blocks if b["page"] == "2"]
    assert any(b["text"].startswith("NOTES") for b in page2)
    assert all(float(b["x1"]) >= float(b["x0"]) and float(b["y1"]) >= float(b["y0"]) for b in blocks)


def test_page_info(run, fixture_pdf) -> None:
    _, xrefs, out, _ = run
    info = json.loads((out / "page_info.json").read_text(encoding="utf-8"))
    assert info["page_count"] == 3
    assert [o["name"] for o in info["ocgs"]] == ["Rooms (measured)"]
    assert info["ocgs"][0]["xref"] == xrefs["ocg"] and info["ocgs"][0]["on"] is True
    p1, p2, p3 = info["pages"]
    assert p1["is_text_page"] and p1["page_class"] == "text" and p1["text_chars"] >= 200
    assert p1["image_count"] == 1 and 0 < p1["image_coverage_pct"] < 5
    assert p2["is_drawing_page"] and p2["a1_or_larger"] and p2["vector_path_count"] >= 600
    assert p2["annotation_count"] == 3 and p2["page_class"] == "vector_drawing"
    assert p3["rotation"] == 90 and (p3["width_pt"], p3["height_pt"]) == (200.0, 400.0)
    assert (p3["raw_width_pt"], p3["raw_height_pt"]) == (400.0, 200.0)


def test_annotations(run) -> None:
    _, xrefs, out, _ = run
    annots = read_jsonl(out / "annotations.jsonl")
    assert len(annots) == 3 and [a["index"] for a in annots] == [0, 1, 2]
    by_type = {a["type"]: a for a in annots}
    poly = by_type["Polygon"]
    assert poly["xref"] == xrefs["polygon"]
    assert poly["title"] == "Marcus" and poly["subject"] == "Zone" and poly["contents"] == "AHU-3-01 zone"
    assert poly["vertices_raw"] == [[500.0, 500.0], [700.0, 500.0], [600.0, 650.0]]
    assert poly["vertices_view"] == poly["vertices_raw"]  # page 2 is not rotated
    assert poly["stroke_colour"] == "#ff0000" and poly["fill_colour"] == "#008000"
    assert poly["opacity"] == 0.5 and poly["border_width"] == 1.0
    assert poly["oc_xref"] == xrefs["ocg"] and poly["oc_name"] == "Rooms (measured)"
    assert poly["creation_date_iso"] == "2026-11-12T09:30:00+08:00"
    assert poly["rect_raw"][0] <= 500.0 and poly["rect_raw"][2] >= 700.0
    free = by_type["FreeText"]
    assert free["xref"] == xrefs["freetext"] and "Helv" in free["default_appearance"]
    assert free["rect_raw"] == [900.0, 900.0, 1100.0, 960.0]
    reply = by_type["Text"]
    assert reply["xref"] == xrefs["reply"] and reply["irt_xref"] == xrefs["freetext"]
    assert reply["reply_type"] == "/R" and reply["title"] == "Bob"
    assert reply["popup_xref"] is not None and isinstance(reply["flags"], int)
    # Reply chains can be rebuilt from irt_xref alone.
    parents = {a["xref"]: a["irt_xref"] for a in annots}
    assert parents[xrefs["reply"]] == xrefs["freetext"] and parents[xrefs["freetext"]] is None


def test_images_and_fills(run) -> None:
    _, _, out, _ = run
    images = json.loads((out / "images.json").read_text(encoding="utf-8"))
    assert len(images) == 1
    img = images[0]
    assert img["page"] == 1 and (img["width_px"], img["height_px"]) == (8, 6)
    assert img["bbox_view"] == [400.0, 400.0, 480.0, 460.0] and "RGB" in img["colourspace"]
    fills = {f["page"]: f for f in json.loads((out / "fills.json").read_text(encoding="utf-8"))}
    assert fills[1]["filled_paths"] == 0 and fills[1]["top_fills"] == []
    top = {f["colour"]: f["count"] for f in fills[2]["top_fills"]}
    assert top["#ff0000"] == 200 and top["#0000ff"] == 200
    assert top.get("#008000") == 1  # the Polygon annotation's appearance stream is also a filled path
    assert fills[2]["paths_total"] >= 600 and len(fills[2]["top_fills"]) <= 20


def test_tables(run) -> None:
    _, _, out, _ = run
    tables = json.loads((out / "tables.json").read_text(encoding="utf-8"))
    assert tables and all(t["page"] == 1 for t in tables)
    assert {t["strategy"] for t in tables} <= {"lines", "text"}
    best = max(tables, key=lambda t: t["rows"])
    assert best["rows"] >= 6 and best["cols"] >= 5 and len(best["bbox"]) == 4
    csv_path = out / best["file"]
    assert csv_path.name.startswith("tables_p1_") and csv_path.is_file()
    text = csv_path.read_text(encoding="utf-8")
    assert "PH-303" in text and "32.75" in text and "1,200" in text


def test_title_block(run) -> None:
    _, _, out, _ = run
    blocks = json.loads((out / "titleblock.json").read_text(encoding="utf-8"))
    assert len(blocks) == 1 and blocks[0]["page"] == 2
    zone = blocks[0]["zone"]
    assert zone["x_min"] == pytest.approx(2384 * 0.82) and zone["y_min"] == pytest.approx(1684 * 0.88)
    strings = blocks[0]["strings"]
    assert all(s["bbox_view"][0] >= zone["x_min"] or s["bbox_view"][1] >= zone["y_min"] - 20 for s in strings)
    assert {"IP-BH-05010", "REV B", "SCALE 1:100"} <= {s["text"] for s in strings}
    assert "AHU-3-01 2400 L/s" not in {s["text"] for s in strings}
    parsed = blocks[0]["parsed"]
    assert parsed == {"drawing_number": "IP-BH-05010", "title": "INPATIENT 3RD STOREY AIR-SIDE SCHEMATIC POD 2",
                      "revision": "B", "date": "12/11/2026", "scale": "1:100"}


def test_notes_md(run) -> None:
    _, _, out, _ = run
    text = (out / "notes.md").read_text(encoding="utf-8")
    assert "## Page 1" in text and "GENERAL NOTES\n1. All flows in L/s at 1.2 kg/m3." in text
    assert "## Page 2" in text and "NOTES\n1. Duct sizes 600x300 unless noted." in text
    assert "DRAWING TITLE" not in text


def test_manifest_counts_and_provenance(run) -> None:
    pdf, _, out, manifest = run
    counts = manifest["counts"]
    assert counts["annotations"] == 3 and counts["images"] == 1 and counts["notes_blocks"] == 2
    assert counts["drawing_pages"] == 1 and counts["text_pages"] == 1 and counts["ocgs"] == 1
    assert counts["words"] == len(read_jsonl(out / "words.jsonl"))
    assert counts["numeric_tokens"] > 0
    assert manifest["sha256"] == sidecar_pdf.sha256_file(pdf) and manifest["bytes"] == pdf.stat().st_size
    assert manifest["pipeline_version"] == sidecar_pdf.PIPELINE_VERSION
    assert "pdfplumber-tables" in manifest["methods_used"]
    on_disk = json.loads((out / "sidecar_manifest.json").read_text(encoding="utf-8"))
    assert on_disk["sha256"] == manifest["sha256"] and on_disk["files"] == manifest["files"]


# --------------------------------------------------------------------------- #
# Idempotency, max-pages, ledger
# --------------------------------------------------------------------------- #
def test_rerun_is_skipped_and_force_rewrites(run, tmp_path: Path) -> None:
    pdf, _, out, _ = run
    manifest_path = out / "sidecar_manifest.json"
    before = manifest_path.stat().st_mtime_ns
    again = sidecar_pdf.build_sidecars(pdf, out, state_dir=tmp_path)
    assert again["skipped"] is True and manifest_path.stat().st_mtime_ns == before
    forced = sidecar_pdf.build_sidecars(pdf, out, force=True, state_dir=tmp_path)
    assert forced["skipped"] is False and forced["sha256"] == again["sha256"]


def test_changed_source_is_reprocessed(tmp_path: Path) -> None:
    pdf = tmp_path / "a.pdf"
    build_fixture(pdf)
    out = tmp_path / "out"
    first = sidecar_pdf.build_sidecars(pdf, out, max_pages=1, state_dir=tmp_path)
    assert first["pages_processed"] == 1 and (out / "titleblock.json").read_text() .strip() == "[]"
    assert not list(out.glob("tables_p2_*.csv"))
    doc = pymupdf.open(str(pdf))
    doc[0].insert_text((50, 500), "Appended line 77", fontsize=10)
    doc.saveIncr()
    doc.close()
    second = sidecar_pdf.build_sidecars(pdf, out, max_pages=1, state_dir=tmp_path)
    assert second["skipped"] is False and second["sha256"] != first["sha256"]
    assert "77" in sidecar_pdf.numeric_tokens(read_jsonl(out / "words.jsonl"))


def test_ledger_lookup(tmp_path: Path) -> None:
    pdf = tmp_path / "b.pdf"
    build_fixture(pdf)
    sha = sidecar_pdf.sha256_file(pdf)
    state = tmp_path / "state"
    state.mkdir()
    (state / "pull_ledger.jsonl").write_text(
        json.dumps({"drive_id": "abc123", "rel_path": "DD Sign-OFF/30 - Pharmacy/x.pdf", "sha256": sha}) + "\n",
        encoding="utf-8")
    manifest = sidecar_pdf.build_sidecars(pdf, tmp_path / "out", max_pages=1, state_dir=state)
    assert manifest["drive_id"] == "abc123" and manifest["rel_path"] == "DD Sign-OFF/30 - Pharmacy/x.pdf"
    assert sidecar_pdf.ledger_entry(pdf, "0" * 64, state) == {}


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_numeric_tokens() -> None:
    words = ["Supply", "1200L/s", "45.5Pa", "1,200", "600x300", "(12)", "+15", "-5", "1:100", "m2", "IP-BH-05010",
             "12%", "24.5", "3.6kW", "Rev", "B", "2026-11-12", {"text": "99"}, (0, 0, 1, 1, "7", 0, 0, 0)]
    tokens = sidecar_pdf.numeric_tokens(words)
    assert {"1200", "45.5", "600", "300", "12", "15", "-5", "1", "100", "24.5", "3.6", "99", "7"} <= tokens
    assert "05010" not in tokens and "m2" not in tokens and "" not in tokens
    assert sidecar_pdf.numeric_tokens([]) == set()


def test_geometry_helpers() -> None:
    assert sidecar_pdf.rotation_from_dir((1, 0)) == 0
    assert sidecar_pdf.rotation_from_dir((0, -1)) == 90
    assert sidecar_pdf.rotation_from_dir((-1, 0)) == 180
    assert sidecar_pdf.rotation_from_dir((0, 1)) == 270
    assert sidecar_pdf.colour_hex([1, 0, 0]) == "#ff0000"
    assert sidecar_pdf.colour_hex([0.5]) == "#808080"
    assert sidecar_pdf.colour_hex([0, 0, 0, 1]) == "#000000"
    assert sidecar_pdf.colour_hex([]) is None
    assert sidecar_pdf.pdf_date_iso("D:20261112093000Z") == "2026-11-12T09:30:00Z"
    assert sidecar_pdf.pdf_date_iso("D:20261112") == "2026-11-12T00:00:00"
    assert sidecar_pdf.pdf_date_iso("") is None
    assert sidecar_pdf.is_a1_or_larger(2384, 1684) and sidecar_pdf.is_a1_or_larger(1684, 2384)
    assert not sidecar_pdf.is_a1_or_larger(1191, 842)
    assert sidecar_pdf.is_text_page(300, 10) and not sidecar_pdf.is_text_page(300, 900)
    assert sidecar_pdf.is_drawing_page(2384, 1684, None) and not sidecar_pdf.is_drawing_page(595, 842, 900)


def test_parse_title_block_variants() -> None:
    strings = [{"text": t} for t in ["PROJECT", "AIGH IP BUILDING", "TITLE", "LEVEL 5 ACMV LAYOUT PLAN",
                                     "DRAWING NO", "AR-IP-01-05-123", "REVISION", "P02", "DATE", "3 Nov 2026",
                                     "SCALE", "NTS"]]
    parsed = sidecar_pdf.parse_title_block(strings)
    assert parsed["drawing_number"] == "AR-IP-01-05-123" and parsed["revision"] == "P02"
    assert parsed["date"] == "3 Nov 2026" and parsed["scale"] == "NTS"
    assert parsed["title"] == "LEVEL 5 ACMV LAYOUT PLAN"
    assert sidecar_pdf.parse_title_block([])["drawing_number"] is None
    elevation = sidecar_pdf.parse_title_block([{"text": "AR-OP-31-ZZ-007 Rev C"}, {"text": "HS-BH-123 x"}])
    assert elevation["drawing_number"] == "AR-OP-31-ZZ-007" and elevation["revision"] == "C"


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def test_cli_run_and_progress(fixture_pdf, tmp_path: Path) -> None:
    pdf, _ = fixture_pdf
    out = tmp_path / "cli_out"
    progress = tmp_path / "progress.json"
    rc = sidecar_pdf.main(["run", "--pdf", str(pdf), "--out", str(out), "--max-pages", "2",
                           "--progress", str(progress)])
    assert rc == 0 and (out / "sidecar_manifest.json").is_file()
    sha = sidecar_pdf.sha256_file(pdf)
    record = Progress(progress).get(f"sidecar_pdf:{sha}")
    assert record and record["status"] == "done" and record["hash"] == sha
    assert sidecar_pdf.main(["run", "--pdf", str(tmp_path / "missing.pdf"), "--out", str(out), "--no-progress"]) == 1


def test_cli_subprocess(fixture_pdf, tmp_path: Path) -> None:
    pdf, _ = fixture_pdf
    result = subprocess.run(
        [sys.executable, "-m", "src.sidecar_pdf", "run", "--pdf", str(pdf), "--out", str(tmp_path / "o"),
         "--max-pages", "1", "--no-progress"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "written" in result.stdout
    assert "--pdf" in subprocess.run([sys.executable, "-m", "src.sidecar_pdf", "run", "--help"],
                                     cwd=PROJECT_ROOT, capture_output=True, text=True, check=False).stdout


# --------------------------------------------------------------------------- #
# Fixes from the independent review (rotated-page tables, labelled title block, worker threads)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_unrotate_box_inverts_pymupdf_rotation_matrix(rotation: int) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.set_rotation(rotation)
    raw = (30.0, 40.0, 120.0, 90.0)
    view = pymupdf.Rect(raw) * page.rotation_matrix
    back = sidecar_pdf.unrotate_box((view.x0, view.y0, view.x1, view.y1), rotation, 400, 300)
    assert back == pytest.approx(raw)
    doc.close()


def _rotated_table_pdf(path: Path, rotation: int) -> None:
    """A4 text page with prose and a five-column text table, a CropBox offset and ``/Rotate rotation``."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    for i in range(6):
        page.insert_text((50, 60 + i * 14), f"Paragraph {i} of the design narrative for the pharmacy level {i + 1}.",
                         fontsize=10)
    header = ["Room", "Area m2", "Supply L/s", "Pressure Pa", "Load kW"]
    rows = [["PH-301", "24.5", "180", "+15", "2.4"], ["PH-302", "18.0", "120", "-5", "1.8"],
            ["PH-303", "32.75", "1,200", "0", "4.1"], ["PH-304", "12.0", "95", "+10", "0.9"]]
    xs = [60, 160, 260, 360, 460]
    for ci, cell in enumerate(header):
        page.insert_text((xs[ci], 300), cell, fontsize=10)
    for ri, row in enumerate(rows):
        for ci, cell in enumerate(row):
            page.insert_text((xs[ci], 320 + ri * 18), cell, fontsize=10)
    page.set_cropbox(pymupdf.Rect(20, 30, 575, 800))
    if rotation:
        page.set_rotation(rotation)
    doc.save(str(path))
    doc.close()


@pytest.mark.parametrize("rotation", [90, 270])
def test_tables_follow_the_text_direction_on_rotated_pages(tmp_path: Path, rotation: int) -> None:
    pdf = tmp_path / "rot_table.pdf"
    _rotated_table_pdf(pdf, rotation)
    out = tmp_path / "out"
    manifest = sidecar_pdf.build_sidecars(pdf, out, state_dir=tmp_path)
    info = json.loads((out / "page_info.json").read_text(encoding="utf-8"))["pages"][0]
    assert info["is_text_page"] and info["rotation"] == rotation
    assert info["dominant_text_rotation"] == 0 and info["dominant_text_rotation_view"] == (0 - rotation) % 360
    width, height = info["width_pt"], info["height_pt"]
    assert (width, height) == (770.0, 555.0)
    tables = json.loads((out / "tables.json").read_text(encoding="utf-8"))
    assert tables and manifest["counts"]["tables"] == len(tables)
    best = max(tables, key=lambda t: t["rows"] * t["cols"])
    assert best["text_rotation"] == 0 and best["page_rotation"] == rotation
    text = (out / best["file"]).read_text(encoding="utf-8")
    assert "PH-303" in text and "1,200" in text and "32.75" in text
    cells = [cell for line in text.splitlines() for cell in line.split(",")]
    assert "PH-303" in cells  # a whole word, not split by a column edge
    # the table box is in the page view of the source page: it must hold the word boxes of its cells
    words = read_jsonl(out / "words.jsonl")
    for token in ("PH-303", "Room", "0.9"):
        box = next(w["bbox_view"] for w in words if w["text"] == token)
        assert all(0 <= v <= max(width, height) for v in box)
        x0, y0, x1, y1 = best["bbox"]
        assert x0 - 1 <= box[0] and box[2] <= x1 + 1 and y0 - 1 <= box[1] and box[3] <= y1 + 1, (token, box, best["bbox"])


def test_table_rows_from_words_keeps_tokens_whole_and_drops_empty_columns() -> None:
    class _Row:
        def __init__(self, top: float, cells: List) -> None:
            self.bbox = (0.0, top, 100.0, top + 10.0)
            self.cells = cells

    class _Table:
        rows = [_Row(0.0, [(0.0, 0.0, 30.0, 10.0), (30.0, 0.0, 60.0, 10.0), (60.0, 0.0, 100.0, 10.0)]),
                _Row(10.0, [(0.0, 10.0, 30.0, 20.0), None, (60.0, 10.0, 100.0, 20.0)]),
                _Row(20.0, [(0.0, 20.0, 30.0, 30.0), (30.0, 20.0, 60.0, 30.0), (60.0, 20.0, 100.0, 30.0)])]

    words = [{"text": "PH-301", "x0": 20.0, "x1": 45.0, "top": 1.0, "bottom": 9.0},  # straddles the 30 pt edge
             {"text": "180", "x0": 70.0, "x1": 80.0, "top": 1.0, "bottom": 9.0},
             {"text": "PH-302", "x0": 5.0, "x1": 25.0, "top": 11.0, "bottom": 19.0},
             {"text": "120", "x0": 70.0, "x1": 80.0, "top": 11.0, "bottom": 19.0}]
    rows = sidecar_pdf.table_rows_from_words(_Table(), words)
    assert rows == [["", "PH-301", "180"], ["PH-302", "", "120"]]
    assert sidecar_pdf.table_rows_from_words(_Table(), []) == []


def test_title_block_prefers_the_value_next_to_the_label_text_only() -> None:
    strings = [{"text": t} for t in [
        "REFER AR-IP-31-ZZ-002 FOR ELEVATION", "DRAWING TITLE", "01-ZONE PLANS", "4TH STOREY (IP)", "DD.A.01.V",
        "DRAWING NO.", "REV.", "JOB NO.", "A", "TENDER CORRIGENDUM 6", "AR-IP-01-04-001", "SCALE 1 : 250",
    ]]
    parsed = sidecar_pdf.parse_title_block(strings)
    assert parsed == {"drawing_number": "AR-IP-01-04-001", "title": "01-ZONE PLANS 4TH STOREY (IP)",
                      "revision": "A", "date": None, "scale": "1:250"}


def test_title_block_uses_geometry_and_the_last_label_when_boxes_are_known() -> None:
    def s(text: str, x: float, y: float) -> dict:
        return {"text": text, "bbox_view": [x, y, x + 6.0 * len(text), y + 8.0], "rotation": 0}

    strings = [
        # revision history table (first in reading order): its DATE / REV headers must not win
        s("DATE", 3057, 37), s("REV", 3094, 37), s("DESCRIPTION", 3199, 37),
        s("14/03/25", 3056, 48), s("-", 3100, 48), s("ISSUED FOR TENDER", 3116, 48),
        s("27/06/25", 3056, 64), s("A.3", 3099, 64), s("CHANGE OF LAYOUT", 3116, 64),
        s("REFER AR-IP-31-ZZ-002 FOR ELEVATION", 2700, 900),
        # title block proper
        s("DRAWING TITLE", 3050, 2084), s("+123.0", 2780, 2090), s("01-ZONE PLANS", 3050, 2096),
        s("4TH STOREY (IP)", 3050, 2115), s("DD.A.01.V", 3050, 2133),
        s("SCALE", 3050, 2198), s("DATE", 3121, 2198), s("1 : 250", 3050, 2209), s("12/03/25", 3121, 2209),
        s("STAGE", 3050, 2226), s("TD", 3050, 2237),
        s("DRAWING NO.", 3121, 2254), s("REV.", 3298, 2254), s("JOB NO.", 3050, 2255),
        s("A", 3298, 2266), s("SG22A.0057", 3050, 2267), s("AR-IP-01-04-001", 3121, 2267),
    ]
    parsed = sidecar_pdf.parse_title_block(strings)
    assert parsed == {"drawing_number": "AR-IP-01-04-001", "title": "01-ZONE PLANS 4TH STOREY (IP)",
                      "revision": "A", "date": "12/03/25", "scale": "1:250"}


def test_drawing_number_fallback_skips_cross_references_in_reading_order() -> None:
    assert sidecar_pdf.parse_title_block([{"text": "SEE HS-BH-77 FOR DETAILS"}, {"text": "IP-BH-00042"}])["drawing_number"] == "IP-BH-00042"
    assert sidecar_pdf.parse_title_block([{"text": "REFER IP-BH-00001"}])["drawing_number"] == "IP-BH-00001"
    assert sidecar_pdf.parse_title_block([{"text": "HS-BH-5 and AR-IP-01-02-003"}])["drawing_number"] == "HS-BH-5"


def test_worker_thread_runs_unbounded_and_records_it(tmp_path: Path) -> None:
    import threading
    import warnings

    from src import census

    pdf = tmp_path / "t.pdf"
    _rotated_table_pdf(pdf, 0)
    result: Dict[str, object] = {}

    def work() -> None:
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                with census.time_budget(0.01, "probe"):
                    pass
                result["warned"] = any(issubclass(w.category, RuntimeWarning) for w in caught)
            result["manifest"] = sidecar_pdf.build_sidecars(pdf, tmp_path / "out", state_dir=str(tmp_path))
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertions below
            result["error"] = repr(exc)

    thread = threading.Thread(target=work)
    thread.start()
    thread.join(timeout=120)
    assert "error" not in result, result.get("error")
    assert result["warned"] is True
    assert census._DEADLINES == []
    manifest = result["manifest"]
    info = json.loads((tmp_path / "out" / "page_info.json").read_text(encoding="utf-8"))["pages"][0]
    assert info["vector_path_count"] >= 0 and info["page_class"] == "text" and info["is_text_page"]
    assert "path_count_budget_not_enforced:worker_thread" in info["notes"]
    assert manifest["counts"]["tables"] >= 1
    assert "tables_budget_not_enforced:worker_thread" in manifest["notes"]
    # the same file on the main thread carries no such note
    main_manifest = sidecar_pdf.build_sidecars(pdf, tmp_path / "out_main", state_dir=tmp_path)
    main_info = json.loads((tmp_path / "out_main" / "page_info.json").read_text(encoding="utf-8"))["pages"][0]
    assert main_info["notes"] == [] and not any("not_enforced" in n for n in main_manifest["notes"])


def test_time_budget_does_not_leak_a_deadline_when_arming_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from src import census

    def broken() -> None:
        raise ValueError("signal only works in main thread")

    monkeypatch.setattr(census, "_arm_alarm", broken)
    with pytest.raises(ValueError):
        with census.time_budget(1.0, "probe"):
            pass
    assert census._DEADLINES == []
