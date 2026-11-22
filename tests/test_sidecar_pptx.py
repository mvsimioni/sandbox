"""Tests for ``src.sidecar_pptx`` on a small synthetic deck.

The deck is built with python-pptx (text, a table with a merged cell, the
same generated PNG placed twice with one crop, a group with a text box and a
picture, a chart and speaker notes).  A legacy comment part and a modern
comment part are then injected into the package zip by hand, because
python-pptx cannot write either.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path
from typing import Dict, List

import pytest
from PIL import Image

from src import sidecar_pptx as sp

LEGACY_AUTHORS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<p:cmAuthorLst xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
    '<p:cmAuthor id="0" name="Alice Reviewer" initials="AR" lastIdx="1" clrIdx="0"/>'
    '</p:cmAuthorLst>'
)
LEGACY_COMMENTS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<p:cmLst xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
    '<p:cm authorId="0" dt="2026-03-01T09:30:00.000" idx="1"><p:pos x="80" y="160"/>'
    '<p:text>Legacy: check the 1,250 L/s figure</p:text></p:cm>'
    '</p:cmLst>'
)
MODERN_AUTHORS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<p1810:authorLst xmlns:p1810="http://schemas.microsoft.com/office/powerpoint/2018/10/main">'
    '<p1810:author id="{AAAA1111-0000-0000-0000-000000000001}" name="Bob Modern" initials="BM" userId="bob" providerId="AD"/>'
    '<p1810:author id="{AAAA1111-0000-0000-0000-000000000002}" name="Cara Reply" initials="CR" userId="cara" providerId="AD"/>'
    '</p1810:authorLst>'
)
MODERN_COMMENTS_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<p188:cmLst xmlns:p188="http://schemas.microsoft.com/office/powerpoint/2018/8/main"'
    ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    ' xmlns:pc="http://schemas.microsoft.com/office/powerpoint/2013/main/command"'
    ' xmlns:ac="http://schemas.microsoft.com/office/drawing/2013/main/command">'
    '<p188:cm id="{CCCC0000-0000-0000-0000-000000000001}" authorId="{AAAA1111-0000-0000-0000-000000000001}"'
    ' created="2026-03-02T10:00:00.000">'
    '<ac:spMkLst><pc:docMk/><pc:sldMk cId="1" sldId="{SLD_ID}"/><ac:spMk id="{SHAPE_ID}" creationId="{D}"/></ac:spMkLst>'
    '<p188:pos x="1270000" y="635000"/>'
    '<p188:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="en-GB"/><a:t>Modern: table row 2 needs 45 Pa</a:t></a:r></a:p></p188:txBody>'
    '<p188:replyLst><p188:reply id="{CCCC0000-0000-0000-0000-000000000002}"'
    ' authorId="{AAAA1111-0000-0000-0000-000000000002}" created="2026-03-02T11:00:00.000">'
    '<p188:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Reply: agreed, 45 Pa</a:t></a:r></a:p></p188:txBody>'
    '</p188:reply></p188:replyLst>'
    '</p188:cm>'
    '</p188:cmLst>'
)
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"


def _png_bytes(size=(64, 40), colour=(200, 30, 30)) -> bytes:
    """A small generated PNG."""
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def build_deck(path: Path) -> Dict[str, int]:
    """Build the synthetic deck with python-pptx; return ids the comment injection needs."""
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Emu, Inches, Pt

    prs = Presentation()
    # Slide 1: title and a text box with numbers.
    s1 = prs.slides.add_slide(prs.slide_layouts[5])
    s1.shapes.title.text = "AHU-07 supply 1,250 L/s"
    box = s1.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1))
    para = box.text_frame.paragraphs[0]
    para.text = "Room pressure "
    run = para.add_run()
    run.text = "+15 Pa"
    box.text_frame.add_paragraph().text = "Second paragraph 3.5 ACH"
    s1.notes_slide.notes_text_frame.text = "Presenter: confirm 1250 with Beca."
    # Slide 2: a table with a merged cell, and a chart.
    s2 = prs.slides.add_slide(prs.slide_layouts[6])
    table = s2.shapes.add_table(3, 3, Inches(0.5), Inches(0.5), Inches(6), Inches(2)).table
    cells = [["Room", "Area m2", "Pressure Pa"], ["Pharmacy", "32.5", "45"], ["Store", "12", ""]]
    for r, row in enumerate(cells):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    table.cell(1, 2).merge(table.cell(2, 2))  # the spanned cell is blank, as PowerPoint stores it
    chart_data = CategoryChartData()
    chart_data.categories = ["A", "B"]
    chart_data.add_series("Load kW", (12.0, 8.5))
    s2.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(3), Inches(4), Inches(3), chart_data)
    # Slide 3: the same picture twice (one cropped) and a group with a text box and a picture.
    s3 = prs.slides.add_slide(prs.slide_layouts[6])
    png = path.parent / "pic.png"
    png.write_bytes(_png_bytes())
    s3.shapes.add_picture(str(png), Inches(0.5), Inches(0.5), Inches(2), Inches(1.25))
    cropped = s3.shapes.add_picture(str(png), Inches(3), Inches(0.5), Inches(2), Inches(1.25))
    cropped.crop_left = 0.25
    cropped.crop_bottom = 0.1
    group = s3.shapes.add_group_shape()
    group.shapes.add_textbox(Inches(1), Inches(3), Inches(2), Inches(0.5)).text_frame.text = "Grouped label 7"
    group.shapes.add_picture(str(png), Inches(1), Inches(3.6), Inches(1), Inches(0.625))
    prs.save(str(path))
    return {"table_shape_id": table._graphic_frame.shape_id if hasattr(table, "_graphic_frame") else 0,  # noqa: SLF001
            "sld_id_2": s2.slide_id, "s2_table_id": s2.shapes[0].shape_id}


def _add_rel(xml: bytes, rel_id: str, rel_type: str, target: str) -> bytes:
    """Append a relationship to a .rels part."""
    from lxml import etree

    root = etree.fromstring(xml)
    etree.SubElement(root, f"{{{REL_NS}}}Relationship", Id=rel_id, Type=rel_type, Target=target)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _add_overrides(xml: bytes, overrides: Dict[str, str]) -> bytes:
    """Append content-type overrides to [Content_Types].xml."""
    from lxml import etree

    root = etree.fromstring(xml)
    for part, content_type in overrides.items():
        etree.SubElement(root, f"{{{CT_NS}}}Override", PartName=part, ContentType=content_type)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def inject_comments(src: Path, dst: Path, sld_id_2: int, shape_id: int) -> None:
    """Copy ``src`` to ``dst`` adding a legacy comment on slide 1 and a modern comment thread on slide 2."""
    modern = MODERN_COMMENTS_TEMPLATE.replace("{SLD_ID}", str(sld_id_2)).replace("{SHAPE_ID}", str(shape_id))
    modern = modern.replace("{D}", "{DDDD0000-0000-0000-0000-000000000001}")
    new_parts = {
        "ppt/commentAuthors.xml": LEGACY_AUTHORS.encode(),
        "ppt/comments/comment1.xml": LEGACY_COMMENTS.encode(),
        "ppt/authors.xml": MODERN_AUTHORS.encode(),
        "ppt/comments/modernComment_100_0.xml": modern.encode(),
    }
    overrides = {
        "/ppt/commentAuthors.xml": "application/vnd.openxmlformats-officedocument.presentationml.commentAuthors+xml",
        "/ppt/comments/comment1.xml": "application/vnd.openxmlformats-officedocument.presentationml.comments+xml",
        "/ppt/authors.xml": "application/vnd.ms-powerpoint.authors+xml",
        "/ppt/comments/modernComment_100_0.xml": "application/vnd.ms-powerpoint.comments+xml",
    }
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "[Content_Types].xml":
                data = _add_overrides(data, overrides)
            elif item.filename == "ppt/_rels/presentation.xml.rels":
                data = _add_rel(data, "rIdCmA", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/commentAuthors", "commentAuthors.xml")
                data = _add_rel(data, "rIdAuth", "http://schemas.microsoft.com/office/2018/10/relationships/authors", "authors.xml")
            elif item.filename == "ppt/slides/_rels/slide1.xml.rels":
                data = _add_rel(data, "rIdCm1", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments", "../comments/comment1.xml")
            elif item.filename == "ppt/slides/_rels/slide2.xml.rels":
                data = _add_rel(data, "rIdCm2", "http://schemas.microsoft.com/office/2018/10/relationships/comments", "../comments/modernComment_100_0.xml")
            zout.writestr(item, data)
        for name, data in new_parts.items():
            zout.writestr(name, data)


@pytest.fixture(scope="module")
def deck(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Path]:
    """Build the deck once, inject comments, run the extractor and return the paths."""
    base = tmp_path_factory.mktemp("sidecar_pptx")
    plain = base / "plain.pptx"
    ids = build_deck(plain)
    final = base / "DD Beca reply deck.pptx"
    inject_comments(plain, final, ids["sld_id_2"], ids["s2_table_id"])
    out = base / "out"
    state = base / "state"
    state.mkdir()
    ledger_row = {"rel_path": "DD Sign-OFF/30 - Pharmacy/Submitted/DD Beca reply deck.pptx",
                  "local_path": str(final), "sha256": sp.sha256_file(final), "drive_id": "drive123",
                  "drive_modified": "2026-01-01T00:00:00Z", "transport": "connector"}
    (state / "pull_ledger.jsonl").write_text(json.dumps(ledger_row) + "\n", encoding="utf-8")
    progress = base / "progress.json"
    rc = sp.main(["run", "--pptx", str(final), "--out", str(out), "--state-dir", str(state), "--progress", str(progress)])
    assert rc == 0
    return {"pptx": final, "out": out, "state": state, "progress": progress, "plain": plain}


def _jsonl(path: Path) -> List[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# numeric_tokens
# --------------------------------------------------------------------------- #
def test_numeric_tokens() -> None:
    assert sp.numeric_tokens("supply 1,250 L/s at +15 Pa, 3.5 ACH and .5 m") == ["1250", "+15", "3.5", ".5"]
    assert sp.numeric_tokens("AHU-07 and AHU 07, room R2, 2024-05-01") == ["07", "07", "2024", "05", "01"]
    assert sp.numeric_tokens("") == []


# --------------------------------------------------------------------------- #
# slides.jsonl
# --------------------------------------------------------------------------- #
def test_slides_jsonl(deck: Dict[str, Path]) -> None:
    rows = _jsonl(deck["out"] / "slides.jsonl")
    assert [r["slide"] for r in rows] == [1, 2, 3]
    assert all(r["layout"] for r in rows)
    s1 = rows[0]["shapes"]
    texts = [s["text"] for s in s1]
    assert "AHU-07 supply 1,250 L/s" in texts
    body = next(s for s in s1 if s["text"].startswith("Room pressure"))
    assert body["paragraphs"][0]["runs"] == ["Room pressure ", "+15 Pa"]
    assert body["text"] == "Room pressure +15 Pa\nSecond paragraph 3.5 ACH"
    assert body["emu"]["left"] == 914400 and body["pt"]["left"] == 72.0
    assert [s["z_order"] for s in s1] == list(range(len(s1)))
    s2 = rows[1]["shapes"]
    assert sum(s["is_table"] for s in s2) == 1 and sum(s["is_chart"] for s in s2) == 1
    assert next(s for s in s2 if s["is_chart"])["chart_type"] == "COLUMN_CLUSTERED"
    s3 = rows[2]["shapes"]
    group = next(s for s in s3 if s["is_group"])
    children = [s for s in s3 if s["parent_id"] == group["shape_id"]]
    assert len(children) == 2 and all(c["depth"] == 1 for c in children)
    assert any(c["text"] == "Grouped label 7" for c in children)
    assert any(c["is_picture"] for c in children)
    label = next(c for c in children if c["text"] == "Grouped label 7")
    assert label["pt"]["left"] == pytest.approx(72.0, abs=0.5) and label["pt"]["top"] == pytest.approx(216.0, abs=0.5)
    assert sum(s["is_picture"] for s in s3) == 3


# --------------------------------------------------------------------------- #
# tables
# --------------------------------------------------------------------------- #
def test_tables(deck: Dict[str, Path]) -> None:
    tables = json.loads((deck["out"] / "tables.json").read_text(encoding="utf-8"))
    assert tables["count"] == 1
    entry = tables["tables"][0]
    assert entry["slide"] == 2 and entry["path"] == "tables_s2_1.csv"
    assert entry["rows"] == 3 and entry["cols"] == 3
    assert entry["merged_cells"] == [{"row": 2, "col": 2, "origin_row": 1, "origin_col": 2,
                                      "span_rows": 2, "span_cols": 1, "repeated": True}]
    with (deck["out"] / entry["path"]).open(encoding="utf-8", newline="") as handle:
        grid = list(csv.reader(handle))
    assert grid == [["Room", "Area m2", "Pressure Pa"], ["Pharmacy", "32.5", "45"], ["Store", "12", "45"]]


# --------------------------------------------------------------------------- #
# notes
# --------------------------------------------------------------------------- #
def test_notes(deck: Dict[str, Path]) -> None:
    text = (deck["out"] / "notes.md").read_text(encoding="utf-8")
    assert "## Slide 1\n\nPresenter: confirm 1250 with Beca." in text
    assert "## Slide 2\n\n(no notes)" in text
    assert text.count("## Slide") == 3


# --------------------------------------------------------------------------- #
# comments: both kinds
# --------------------------------------------------------------------------- #
def test_comments_both_kinds(deck: Dict[str, Path]) -> None:
    rows = _jsonl(deck["out"] / "comments.jsonl")
    assert [r["index"] for r in rows] == [1, 2, 3]
    legacy = [r for r in rows if r["kind"] == "legacy"]
    modern = [r for r in rows if r["kind"] == "modern"]
    assert len(legacy) == 1 and len(modern) == 2
    lg = legacy[0]
    assert lg["slide"] == 1 and lg["author"] == "Alice Reviewer" and lg["initials"] == "AR"
    assert lg["date"] == "2026-03-01T09:30:00.000" and lg["text"] == "Legacy: check the 1,250 L/s figure"
    assert lg["pos_x"] == 80 and lg["pos_x_pt"] == 10.0 and lg["pos_units"] == "eighth-pt"
    assert lg["reply_to"] is None and lg["part"] == "ppt/comments/comment1.xml"
    root = next(r for r in modern if r["reply_to"] is None)
    reply = next(r for r in modern if r["reply_to"] is not None)
    assert root["slide"] == 2 and root["author"] == "Bob Modern" and root["text"] == "Modern: table row 2 needs 45 Pa"
    assert root["date"] == "2026-03-02T10:00:00.000" and root["pos_x_pt"] == 100.0 and root["pos_units"] == "EMU"
    assert root["anchor_type"] == "shape" and isinstance(root["anchor_shape_id"], int)
    assert reply["reply_to"] == root["comment_id"] == "{CCCC0000-0000-0000-0000-000000000001}"
    assert reply["author"] == "Cara Reply" and reply["text"] == "Reply: agreed, 45 Pa" and reply["slide"] == 2


def test_modern_comment_resolved_by_sld_id_when_unlinked(deck: Dict[str, Path], tmp_path: Path) -> None:
    """A modern part that no slide relationship points at still lands on its slide via sldMk/sldId."""
    src, dst = deck["pptx"], tmp_path / "unlinked.pptx"
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "ppt/slides/_rels/slide2.xml.rels":
                data = data.replace(b"../comments/modernComment_100_0.xml", b"../comments/missing.xml")
            zout.writestr(item, data)
    rows, summary = sp.read_comments(dst)
    assert summary["unlinked_parts"] == ["ppt/comments/modernComment_100_0.xml"]
    assert [r["slide"] for r in rows if r["kind"] == "modern"] == [2, 2]


# --------------------------------------------------------------------------- #
# pictures
# --------------------------------------------------------------------------- #
def test_pictures(deck: Dict[str, Path]) -> None:
    pictures = json.loads((deck["out"] / "pictures.json").read_text(encoding="utf-8"))
    placed = [i for i in pictures["items"] if i["slide"] is not None]
    assert len(placed) == 3 and pictures["placements"] == 3
    assert pictures["unique_images"] == 1
    shas = {i["sha256"] for i in placed}
    assert len(shas) == 1
    paths = {i["path"] for i in placed}
    assert len(paths) == 1
    (path,) = paths
    assert path.startswith("pictures/3_") and path.endswith(f"_{next(iter(shas))[:8]}.png")
    assert (deck["out"] / path).is_file()
    assert sp.sha256_file(deck["out"] / path) == next(iter(shas))
    assert all(i["pixel_width"] == 64 and i["pixel_height"] == 40 for i in placed)
    cropped = [i for i in placed if i["crop"]]
    assert len(cropped) == 1
    assert cropped[0]["crop"]["left"] == pytest.approx(0.25) and cropped[0]["crop"]["bottom"] == pytest.approx(0.1)
    assert cropped[0]["shape_pt"]["left"] == 216.0
    assert sum(i["duplicate_of_first_placement"] for i in placed) == 2
    assert len(list((deck["out"] / "pictures").iterdir())) == 1


# --------------------------------------------------------------------------- #
# slide_info and manifest
# --------------------------------------------------------------------------- #
def test_slide_info(deck: Dict[str, Path]) -> None:
    info = json.loads((deck["out"] / "slide_info.json").read_text(encoding="utf-8"))
    assert info["slide_count"] == 3 and info["slide_width_pt"] == 720.0 and info["slide_height_pt"] == 540.0
    by_slide = {s["slide"]: s for s in info["slides"]}
    assert by_slide[1]["comment_count"] == 1 and by_slide[2]["comment_count"] == 2 and by_slide[3]["comment_count"] == 0
    assert by_slide[2]["table_count"] == 1 and by_slide[2]["chart_count"] == 1
    assert by_slide[3]["picture_count"] == 3 and by_slide[3]["group_count"] == 1
    assert 0 < by_slide[3]["picture_coverage_pct"] < 100
    assert by_slide[1]["text_chars"] > 0 and by_slide[1]["numeric_tokens"] >= 3
    assert by_slide[1]["notes_chars"] == len("Presenter: confirm 1250 with Beca.")
    assert info["totals"]["comment_count"] == 3 and info["totals"]["unique_images"] == 1


def test_manifest_and_checks(deck: Dict[str, Path]) -> None:
    manifest = json.loads((deck["out"] / "sidecar_manifest.json").read_text(encoding="utf-8"))
    assert manifest["sha256"] == sp.sha256_file(deck["pptx"])
    assert manifest["rel_path"].endswith("DD Beca reply deck.pptx") and manifest["drive_id"] == "drive123"
    assert manifest["comment_parts"]["legacy_parts"] == ["ppt/comments/comment1.xml"]
    assert manifest["comment_parts"]["modern_parts"] == ["ppt/comments/modernComment_100_0.xml"]
    assert all(c["pass"] for c in manifest["checks"]), manifest["checks"]
    for name in manifest["files"]:
        assert (deck["out"] / name).is_file(), name
    assert {"slides.jsonl", "tables.json", "notes.md", "comments.jsonl", "pictures.json", "slide_info.json",
            "tables_s2_1.csv"} <= set(manifest["files"])
    progress = json.loads(deck["progress"].read_text(encoding="utf-8"))
    item = progress["items"][f"sidecar_pptx:{manifest['sha256']}"]
    assert item["status"] == "done" and item["hash"] == manifest["sha256"]


# --------------------------------------------------------------------------- #
# idempotency and CLI failures
# --------------------------------------------------------------------------- #
def test_rerun_is_skipped_and_force_rewrites(deck: Dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    manifest_path = deck["out"] / "sidecar_manifest.json"
    before = manifest_path.read_text(encoding="utf-8")
    args = ["run", "--pptx", str(deck["pptx"]), "--out", str(deck["out"]), "--state-dir", str(deck["state"]),
            "--progress", str(deck["progress"])]
    assert sp.main(args) == 0
    assert manifest_path.read_text(encoding="utf-8") == before
    assert "skip (done)" in capsys.readouterr().out
    (deck["out"] / "notes.md").unlink()
    assert sp.main(args) == 0  # a missing output invalidates the done state
    assert (deck["out"] / "notes.md").is_file()
    assert sp.main(args + ["--force"]) == 0
    assert "done:" in capsys.readouterr().out


def test_default_out_dir_from_ledger(deck: Dict[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(sp.PATHS, "sidecar", tmp_path / "sidecar")
    assert sp.main(["run", "--pptx", str(deck["pptx"]), "--state-dir", str(deck["state"]), "--no-progress"]) == 0
    expected = tmp_path / "sidecar" / "DD Sign-OFF/30 - Pharmacy/Submitted/DD Beca reply deck.pptx"
    assert (expected / "slides.jsonl").is_file()


def test_cli_failures(tmp_path: Path, deck: Dict[str, Path]) -> None:
    assert sp.main(["run", "--pptx", str(tmp_path / "missing.pptx"), "--out", str(tmp_path), "--no-progress"]) == 1
    not_pptx = tmp_path / "x.txt"
    not_pptx.write_text("x")
    assert sp.main(["run", "--pptx", str(not_pptx), "--out", str(tmp_path), "--no-progress"]) == 1
    empty_state = tmp_path / "state"
    empty_state.mkdir()
    assert sp.main(["run", "--pptx", str(deck["plain"]), "--state-dir", str(empty_state), "--no-progress"]) == 1
    corrupt = tmp_path / "corrupt.pptx"
    corrupt.write_bytes(b"not a zip")
    assert sp.main(["run", "--pptx", str(corrupt), "--out", str(tmp_path / "o"), "--no-progress"]) == 1


def test_plain_deck_without_comment_parts(deck: Dict[str, Path], tmp_path: Path) -> None:
    rows, summary = sp.read_comments(deck["plain"])
    assert rows == [] and summary["legacy_parts"] == [] and summary["modern_parts"] == []
    manifest = sp.run_one(deck["plain"], tmp_path / "plain_out", tmp_path)
    assert manifest["counts"]["comment_count"] == 0 and manifest["rel_path"] is None
    assert all(c["pass"] for c in manifest["checks"])


# --------------------------------------------------------------------------- #
# review fixes: pictures by content type, fields and breaks, unreadable parts,
# duplicate shape ids, coordinate frames in groups
# --------------------------------------------------------------------------- #
def _rewrite_zip(src: Path, dst: Path, edit) -> None:
    """Copy a package, letting ``edit(name, data)`` replace any part's bytes."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            zout.writestr(item, edit(item.filename, zin.read(item.filename)))


def test_image_ext_from_content_type_then_part_name() -> None:
    assert sp._image_ext("image/x-emf", "/ppt/media/image1.wmf") == "emf"  # noqa: SLF001
    assert sp._image_ext("image/png", "/ppt/media/image1.bin") == "png"  # noqa: SLF001
    assert sp._image_ext("application/octet-stream", "/ppt/media/image2.svg") == "svg"  # noqa: SLF001
    assert sp._image_ext("", "/ppt/media/blob") == "bin"  # noqa: SLF001


def test_unidentifiable_picture_blob_is_exported_with_note(deck: Dict[str, Path], tmp_path: Path) -> None:
    """A blob Pillow cannot open (EMF, HD Photo, corrupt) keeps its placement, hash and file; the run exits 0."""
    corrupt = tmp_path / "corrupt.pptx"
    _rewrite_zip(deck["plain"], corrupt, lambda n, d: b"\x01\x02 not an image" if n.startswith("ppt/media/") else d)
    out = tmp_path / "out"
    assert sp.main(["run", "--pptx", str(corrupt), "--out", str(out), "--no-progress"]) == 0
    pictures = json.loads((out / "pictures.json").read_text(encoding="utf-8"))
    placed = [i for i in pictures["items"] if i["slide"] is not None]
    assert len(placed) == 3 and all("error" not in i for i in placed)
    assert all(i["pixel_width"] is None and i["pixel_note"] for i in placed)
    assert all(i["content_type"] == "image/png" and i["path"].endswith(".png") for i in placed)
    assert all((out / i["path"]).is_file() for i in placed)
    assert (out / placed[0]["path"]).read_bytes() == b"\x01\x02 not an image"
    manifest = json.loads((out / "sidecar_manifest.json").read_text(encoding="utf-8"))
    assert all(c["pass"] for c in manifest["checks"]), manifest["checks"]


def test_fields_and_line_breaks_in_paragraphs(tmp_path: Path) -> None:
    from lxml import etree
    from pptx import Presentation
    from pptx.util import Inches

    a_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1))
    para = box.text_frame.paragraphs[0]
    para.text = "Rev "
    fld = etree.SubElement(para._p, f"{{{a_ns}}}fld", id="{B1C2D3E4-0000-0000-0000-000000000002}", type="slidenum")  # noqa: SLF001
    etree.SubElement(fld, f"{{{a_ns}}}t").text = "3"
    etree.SubElement(para._p, f"{{{a_ns}}}br")  # noqa: SLF001
    run = etree.SubElement(para._p, f"{{{a_ns}}}r")  # noqa: SLF001
    etree.SubElement(run, f"{{{a_ns}}}t").text = "flow 1,250 L/s"
    deck_path = tmp_path / "fld.pptx"
    prs.save(str(deck_path))
    record = sp.extract_deck(deck_path).slides[0]["shapes"][0]
    assert record["text"] == "Rev 3\nflow 1,250 L/s"
    assert record["paragraphs"][0]["runs"] == ["Rev ", "3", "flow 1,250 L/s"]
    assert record["paragraphs"][0]["fields"] == 1 and record["paragraphs"][0]["line_breaks"] == 1
    assert sp.numeric_tokens(record["text"]) == ["3", "1250"]


def test_malformed_author_part_is_recorded_and_fails_a_check(deck: Dict[str, Path], tmp_path: Path) -> None:
    bad = tmp_path / "bad_authors.pptx"
    _rewrite_zip(deck["pptx"], bad, lambda n, d: d[: len(d) // 2] if n == "ppt/commentAuthors.xml" else d)
    rows, summary = sp.read_comments(bad)
    assert [e["part"] for e in summary["unreadable_parts"]] == ["ppt/commentAuthors.xml"]
    assert len(rows) == 3 and next(r for r in rows if r["kind"] == "legacy")["author"] == ""
    manifest = sp.run_one(bad, tmp_path / "out", tmp_path)
    by_name = {c["check"]: c for c in manifest["checks"]}
    parsed = by_name["every comment, author, presentation and relationship part parsed"]
    assert parsed["pass"] is False and "commentAuthors.xml" in parsed["evidence"]
    assert manifest["comment_parts"]["unreadable_parts"] == summary["unreadable_parts"]
    # a clean deck carries no unreadable_parts entry and the check passes
    clean = json.loads((deck["out"] / "sidecar_manifest.json").read_text(encoding="utf-8"))
    assert "unreadable_parts" not in clean["comment_parts"]
    assert next(c for c in clean["checks"] if c["check"] == parsed["check"])["pass"] is True


def test_duplicate_shape_ids_are_reported(tmp_path: Path) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_table(1, 1, Inches(0.5), Inches(0.5), Inches(2), Inches(1)).table.cell(0, 0).text = "FIRST 1"
    slide.shapes.add_table(1, 1, Inches(0.5), Inches(2.5), Inches(2), Inches(1)).table.cell(0, 0).text = "SECOND 2"
    slide.shapes[1]._element.nvGraphicFramePr.cNvPr.id = slide.shapes[0].shape_id  # noqa: SLF001
    deck_path = tmp_path / "dup.pptx"
    prs.save(str(deck_path))
    out = tmp_path / "out"
    manifest = sp.run_one(deck_path, out, tmp_path)
    assert manifest["duplicate_shape_ids"] == [{"slide": 1, "shape_id": slide.shapes[0].shape_id, "count": 2}]
    assert manifest["counts"]["duplicate_shape_ids"] == 1 and manifest["counts"]["table_count"] == 2
    tables = json.loads((out / "tables.json").read_text(encoding="utf-8"))["tables"]
    assert [t["path"] for t in tables] == ["tables_s1_1.csv", "tables_s1_2.csv"]
    assert [t["numeric_tokens"] for t in tables] == [1, 1]
    assert (out / "tables_s1_1.csv").read_text(encoding="utf-8").strip() == "FIRST 1"
    assert (out / "tables_s1_2.csv").read_text(encoding="utf-8").strip() == "SECOND 2"
    assert all(c["pass"] for c in manifest["checks"]), manifest["checks"]


@pytest.mark.parametrize("flip_h", [False, True])
def test_group_rotation_and_flip_compose_into_children(tmp_path: Path, flip_h: bool) -> None:
    """A group of 2 x 1 in at (1, 1) in rotated 90 degrees (and optionally mirrored) about its centre."""
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    group = slide.shapes.add_group_shape()
    group.shapes.add_textbox(Inches(1), Inches(1), Inches(1), Inches(0.5)).text_frame.text = "a"
    group.shapes.add_textbox(Inches(2), Inches(1.5), Inches(1), Inches(0.5)).text_frame.text = "b"
    group.shapes[0].rotation = 10
    xfrm = group._element.grpSpPr.xfrm  # noqa: SLF001
    xfrm.rot, xfrm.flipH = 90.0, flip_h
    deck_path = tmp_path / "rot_group.pptx"
    prs.save(str(deck_path))
    top, a, b = sp.extract_deck(deck_path).slides[0]["shapes"]
    assert top["emu_frame"] == "slide" and top["emu_slide"] == top["emu"] and top["rotation_slide"] == 90.0
    assert top["flip_h"] is flip_h and top["flip_h_slide"] is flip_h and top["flip_v_slide"] is False
    assert a["emu_frame"] == b["emu_frame"] == "group"
    assert a["emu"] == {"left": 914400.0, "top": 914400.0, "width": 914400.0, "height": 457200.0}  # stored, child space
    # child centres rotate about the group centre (2 in, 1.5 in); sizes keep their magnitude
    assert a["pt"] == {"left": 126.0, "top": 126.0 if flip_h else 54.0, "width": 72.0, "height": 36.0}
    assert b["pt"] == {"left": 90.0, "top": 54.0 if flip_h else 126.0, "width": 72.0, "height": 36.0}
    assert a["emu_slide"]["left"] == 1600200.0 and a["emu_slide"]["width"] == 914400.0
    assert a["emu_slide"]["left"] / sp.EMU_PER_PT == a["pt"]["left"]
    # the child's own 10 degrees compose with the group's 90 (mirroring reverses the sense)
    assert a["rotation"] == 10.0 and a["rotation_slide"] == (80.0 if flip_h else 100.0)
    assert b["rotation_slide"] == 90.0 and a["flip_h_slide"] is flip_h and a["flip_h"] is False
