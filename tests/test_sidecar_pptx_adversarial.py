"""Adversarial tests for ``src.sidecar_pptx`` (independent review).

Every deck is synthetic (python-pptx plus hand-edited package parts); no
file under ``data/source`` is read.  The tests for issues I-01 to I-05 of
``out/qa/verify/code_sidecar_pptx_review.md`` were ``xfail(strict=True)``
until the fixes landed; they now run as ordinary tests.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path
from typing import Dict, List

import pytest
from lxml import etree
from PIL import Image

from src import sidecar_pptx as sp
from tests.test_sidecar_pptx import LEGACY_AUTHORS, LEGACY_COMMENTS, _add_overrides, _add_rel

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
MODERN_REL = "http://schemas.microsoft.com/office/2018/10/relationships/comments"
LEGACY_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"


def _png_bytes(size=(20, 10)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def _jsonl(path: Path) -> List[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _rewrite_zip(src: Path, dst: Path, replace: Dict[str, bytes] | None = None, add: Dict[str, bytes] | None = None,
                 edit=None) -> None:
    """Copy a package, replacing/adding parts and optionally editing bytes per part name."""
    replace, add = replace or {}, add or {}
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = replace.get(item.filename, zin.read(item.filename))
            if edit is not None:
                data = edit(item.filename, data)
            zout.writestr(item, data)
        for name, data in add.items():
            zout.writestr(name, data)


def _one_picture_deck(path: Path) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    png = path.parent / "p.png"
    png.write_bytes(_png_bytes())
    slide.shapes.add_picture(str(png), Inches(1), Inches(1), Inches(2), Inches(1))
    prs.save(str(path))


# --------------------------------------------------------------------------- #
# 1. A PPTX without any comment part, through the CLI
# --------------------------------------------------------------------------- #
def test_pptx_without_comment_parts_cli(tmp_path: Path) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1)).text_frame.text = "Only text, 12 Pa"
    deck = tmp_path / "plain.pptx"
    prs.save(str(deck))
    out = tmp_path / "out"
    assert sp.main(["run", "--pptx", str(deck), "--out", str(out), "--no-progress"]) == 0
    with zipfile.ZipFile(deck) as archive:
        assert not any("comment" in n.lower() for n in archive.namelist())
    assert (out / "comments.jsonl").read_text(encoding="utf-8") == ""
    manifest = json.loads((out / "sidecar_manifest.json").read_text(encoding="utf-8"))
    assert manifest["counts"]["comment_count"] == 0
    assert manifest["comment_parts"] == {"legacy_parts": [], "modern_parts": [], "legacy_authors": 0,
                                         "modern_authors": 0, "unlinked_parts": []}
    assert all(c["pass"] for c in manifest["checks"]), manifest["checks"]
    info = json.loads((out / "slide_info.json").read_text(encoding="utf-8"))
    assert info["slides"][0]["comment_count"] == 0 and info["slides"][0]["numeric_tokens"] == 1


# --------------------------------------------------------------------------- #
# 2. A deck with zero slides
# --------------------------------------------------------------------------- #
def test_empty_deck_runs_clean(tmp_path: Path) -> None:
    from pptx import Presentation

    deck = tmp_path / "empty.pptx"
    Presentation().save(str(deck))
    out = tmp_path / "out"
    assert sp.main(["run", "--pptx", str(deck), "--out", str(out), "--no-progress"]) == 0
    info = json.loads((out / "slide_info.json").read_text(encoding="utf-8"))
    assert info["slide_count"] == 0 and info["slides"] == [] and info["totals"]["shape_count"] == 0
    assert (out / "slides.jsonl").read_text(encoding="utf-8") == ""
    assert "## Slide" not in (out / "notes.md").read_text(encoding="utf-8")
    manifest = json.loads((out / "sidecar_manifest.json").read_text(encoding="utf-8"))
    assert all(c["pass"] for c in manifest["checks"])


# --------------------------------------------------------------------------- #
# 3. Unicode file name with spaces and brackets, default out dir from the ledger
# --------------------------------------------------------------------------- #
def test_unicode_name_with_spaces_and_brackets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    name = "DD Jacobs reply (Rev B) [AHU-07] Zoning éè 日本.pptx"
    folder = tmp_path / "DD Sign-OFF" / "30 - Pharmacy" / "AH sign off with comments"
    folder.mkdir(parents=True)
    deck = folder / name
    _one_picture_deck(deck)
    rel_path = f"DD Sign-OFF/30 - Pharmacy/AH sign off with comments/{name}"
    state = tmp_path / "state"
    state.mkdir()
    (state / "pull_ledger.jsonl").write_text(json.dumps({
        "rel_path": rel_path, "local_path": str(deck), "sha256": sp.sha256_file(deck), "drive_id": "u1",
        "drive_modified": "2026-02-02T00:00:00Z"}, ensure_ascii=False) + "\n", encoding="utf-8")
    monkeypatch.setitem(sp.PATHS, "sidecar", tmp_path / "sidecar")
    progress = tmp_path / "progress.json"
    args = ["run", "--pptx", str(deck), "--state-dir", str(state), "--progress", str(progress)]
    assert sp.main(args) == 0
    out = tmp_path / "sidecar" / rel_path
    assert (out / "slides.jsonl").is_file() and (out / "pictures.json").is_file()
    manifest = json.loads((out / "sidecar_manifest.json").read_text(encoding="utf-8"))
    assert manifest["rel_path"] == rel_path and manifest["drive_id"] == "u1"
    assert (out / "notes.md").read_text(encoding="utf-8").startswith(f"# Speaker notes: {name}")
    # second run is a no-op keyed by sha256
    before = (out / "sidecar_manifest.json").read_text(encoding="utf-8")
    assert sp.main(args) == 0
    assert (out / "sidecar_manifest.json").read_text(encoding="utf-8") == before
    item = json.loads(progress.read_text(encoding="utf-8"))["items"][f"sidecar_pptx:{manifest['sha256']}"]
    assert item["status"] == "done"


# --------------------------------------------------------------------------- #
# 4. A picture whose blob PIL cannot identify must not abort the whole deck
# --------------------------------------------------------------------------- #
def test_corrupt_picture_blob_does_not_abort_run(tmp_path: Path) -> None:
    plain = tmp_path / "pic.pptx"
    _one_picture_deck(plain)
    corrupt = tmp_path / "corrupt.pptx"
    _rewrite_zip(plain, corrupt, edit=lambda n, d: b"not an image" if n.startswith("ppt/media/") else d)
    out = tmp_path / "out"
    assert sp.main(["run", "--pptx", str(corrupt), "--out", str(out), "--no-progress"]) == 0
    pictures = json.loads((out / "pictures.json").read_text(encoding="utf-8"))
    assert pictures["placements"] == 1
    item = pictures["items"][0]
    assert item["pixel_width"] is None and item["sha256"] == sp.sha256_bytes(b"not an image")


# --------------------------------------------------------------------------- #
# 5. Field text (slide number) and line breaks inside a paragraph
# --------------------------------------------------------------------------- #
def test_field_and_line_break_text_preserved(tmp_path: Path) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1))
    para = box.text_frame.paragraphs[0]
    para.text = "Slide "
    fld = etree.SubElement(para._p, f"{{{A_NS}}}fld", id="{B1C2D3E4-0000-0000-0000-000000000001}", type="slidenum")  # noqa: SLF001
    etree.SubElement(fld, f"{{{A_NS}}}t").text = "7"
    etree.SubElement(para._p, f"{{{A_NS}}}br")  # noqa: SLF001
    run = etree.SubElement(para._p, f"{{{A_NS}}}r")  # noqa: SLF001
    etree.SubElement(run, f"{{{A_NS}}}t").text = "after break 42 Pa"
    deck = tmp_path / "fld.pptx"
    prs.save(str(deck))
    record = sp.extract_deck(deck).slides[0]["shapes"][0]
    assert "7" in record["text"], record["text"]
    assert sp.numeric_tokens(record["text"]) == ["7", "42"]
    assert record["text"].replace("\x0b", "\n") == "Slide 7\nafter break 42 Pa"


# --------------------------------------------------------------------------- #
# 6. A malformed comment part must be reported, not silently dropped
# --------------------------------------------------------------------------- #
def test_malformed_comment_part_is_reported(tmp_path: Path) -> None:
    plain = tmp_path / "pic.pptx"
    _one_picture_deck(plain)
    bad = tmp_path / "bad.pptx"

    def edit(name: str, data: bytes) -> bytes:
        if name == "ppt/slides/_rels/slide1.xml.rels":
            return _add_rel(data, "rIdX", MODERN_REL, "../comments/modernComment_1_0.xml")
        return data

    _rewrite_zip(plain, bad, add={"ppt/comments/modernComment_1_0.xml": b"<p188:cmLst xmlns:p188='x'><p188:cm id='1'>"},
                 edit=edit)
    manifest = sp.run_one(bad, tmp_path / "out", tmp_path)
    assert manifest["comment_parts"]["modern_parts"] == ["ppt/comments/modernComment_1_0.xml"]
    unreadable = manifest["comment_parts"].get("unreadable_parts") or manifest["comment_parts"].get("errors")
    assert unreadable, "the unreadable part is not recorded anywhere in the manifest"
    assert not all(c["pass"] for c in manifest["checks"]), "a lost comment part must fail a check"


# --------------------------------------------------------------------------- #
# 7. Two tables on one slide sharing a (duplicate) shape id
# --------------------------------------------------------------------------- #
def test_duplicate_shape_ids_tables_keep_own_content(tmp_path: Path) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    first = slide.shapes.add_table(1, 1, Inches(0.5), Inches(0.5), Inches(2), Inches(1)).table
    first.cell(0, 0).text = "FIRST"
    second = slide.shapes.add_table(1, 1, Inches(0.5), Inches(2.5), Inches(2), Inches(1)).table
    second.cell(0, 0).text = "SECOND"
    slide.shapes[1]._element.nvGraphicFramePr.cNvPr.id = slide.shapes[0].shape_id  # noqa: SLF001
    deck = tmp_path / "dup.pptx"
    prs.save(str(deck))
    out = tmp_path / "out"
    sp.run_one(deck, out, tmp_path)
    tables = json.loads((out / "tables.json").read_text(encoding="utf-8"))["tables"]
    grids = []
    for entry in tables:
        with (out / entry["path"]).open(encoding="utf-8", newline="") as handle:
            grids.append(list(csv.reader(handle)))
    assert grids == [[["FIRST"]], [["SECOND"]]]


# --------------------------------------------------------------------------- #
# 8. A legacy comment part nobody links to: kept, unresolved, check fails, exit 0
# --------------------------------------------------------------------------- #
def test_unlinked_legacy_comment_part_kept_and_flagged(tmp_path: Path) -> None:
    plain = tmp_path / "pic.pptx"
    _one_picture_deck(plain)
    deck = tmp_path / "unlinked.pptx"
    _rewrite_zip(plain, deck, add={"ppt/commentAuthors.xml": LEGACY_AUTHORS.encode(),
                                   "ppt/comments/comment1.xml": LEGACY_COMMENTS.encode()})
    out = tmp_path / "out"
    assert sp.main(["run", "--pptx", str(deck), "--out", str(out), "--no-progress"]) == 0
    rows = _jsonl(out / "comments.jsonl")
    assert len(rows) == 1 and rows[0]["slide"] is None and rows[0]["author"] == "Alice Reviewer"
    manifest = json.loads((out / "sidecar_manifest.json").read_text(encoding="utf-8"))
    assert manifest["comment_parts"]["unlinked_parts"] == ["ppt/comments/comment1.xml"]
    by_name = {c["check"]: c["pass"] for c in manifest["checks"]}
    assert by_name["every comment resolved to a slide"] is False
    assert by_name["comment rows equal comment and reply elements in the package"] is True
    assert manifest["counts"]["comments_without_slide"] == 1 and manifest["counts"]["comment_count"] == 0


# --------------------------------------------------------------------------- #
# 9. Unknown author ids, missing dates, empty text, multi-paragraph modern text
# --------------------------------------------------------------------------- #
def test_unknown_author_missing_date_and_multiparagraph_text(tmp_path: Path) -> None:
    plain = tmp_path / "pic.pptx"
    _one_picture_deck(plain)
    legacy = ('<p:cmLst xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
              '<p:cm authorId="9" idx="3"><p:pos x="0" y="0"/><p:text></p:text></p:cm></p:cmLst>')
    modern = ('<p188:cmLst xmlns:p188="http://schemas.microsoft.com/office/powerpoint/2018/8/main"'
              ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
              '<p188:cm id="{1}" authorId="{nobody}" status="resolved">'
              '<p188:txBody><a:bodyPr/><a:p><a:r><a:t>line one 5 Pa</a:t></a:r></a:p>'
              '<a:p><a:r><a:t>line </a:t></a:r><a:r><a:t>two</a:t></a:r></a:p></p188:txBody>'
              '</p188:cm></p188:cmLst>')
    deck = tmp_path / "authors.pptx"

    def edit(name: str, data: bytes) -> bytes:
        if name == "ppt/slides/_rels/slide1.xml.rels":
            data = _add_rel(data, "rIdL", LEGACY_REL, "../comments/comment1.xml")
            data = _add_rel(data, "rIdM", MODERN_REL, "../comments/modernComment_1_0.xml")
        return data

    _rewrite_zip(plain, deck, add={"ppt/comments/comment1.xml": legacy.encode(),
                                   "ppt/comments/modernComment_1_0.xml": modern.encode()}, edit=edit)
    rows, summary = sp.read_comments(deck)
    assert summary["legacy_authors"] == 0 and summary["modern_authors"] == 0
    assert [r["kind"] for r in rows] == ["legacy", "modern"]
    lg, md = rows
    assert lg["author"] == "" and lg["author_id"] == "9" and lg["date"] == "" and lg["text"] == "" and lg["slide"] == 1
    assert md["author"] == "" and md["author_id"] == "{nobody}" and md["status"] == "resolved"
    assert md["text"] == "line one 5 Pa\nline two" and md["anchor_type"] == "slide" and md["pos_x"] is None
    manifest = sp.run_one(deck, tmp_path / "out", tmp_path)
    assert manifest["counts"]["comment_count"] == 2 and all(c["pass"] for c in manifest["checks"])


# --------------------------------------------------------------------------- #
# 10. Nested, scaled groups: pt is absolute, emu is child-space (frame mismatch documented)
# --------------------------------------------------------------------------- #
def test_nested_scaled_group_absolute_pt(tmp_path: Path) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    outer = slide.shapes.add_group_shape()
    inner = outer.shapes.add_group_shape()
    inner.shapes.add_textbox(Inches(2), Inches(3), Inches(1), Inches(0.5)).text_frame.text = "deep 9"
    xfrm = outer._element.grpSpPr.xfrm  # noqa: SLF001
    xfrm.ext.cx, xfrm.ext.cy = xfrm.ext.cx * 2, xfrm.ext.cy * 2  # stretch the outer group 2x about its origin
    deck = tmp_path / "nested.pptx"
    prs.save(str(deck))
    records = sp.extract_deck(deck).slides[0]["shapes"]
    assert [r["depth"] for r in records] == [0, 1, 2] and [r["z_order"] for r in records] == [0, 1, 2]
    leaf = records[2]
    assert leaf["parent_id"] == records[1]["shape_id"] and records[1]["parent_id"] == records[0]["shape_id"]
    assert leaf["pt"] == {"left": 144.0, "top": 216.0, "width": 144.0, "height": 72.0}
    # The EMU box is the stored child-space value, not the slide-absolute one (see I-06).
    assert leaf["emu"]["width"] == 914400.0 and leaf["emu"]["width"] / sp.EMU_PER_PT != leaf["pt"]["width"]


# --------------------------------------------------------------------------- #
# 11. Rotated picture: rotation recorded; coverage uses the unrotated box
# --------------------------------------------------------------------------- #
def test_rotated_picture_rotation_recorded(tmp_path: Path) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    png = tmp_path / "p.png"
    png.write_bytes(_png_bytes())
    pic = slide.shapes.add_picture(str(png), Inches(1), Inches(1), Inches(4), Inches(1))
    pic.rotation = 90
    deck = tmp_path / "rot.pptx"
    prs.save(str(deck))
    out = tmp_path / "out"
    manifest = sp.run_one(deck, out, tmp_path)
    record = _jsonl(out / "slides.jsonl")[0]["shapes"][0]
    assert record["rotation"] == 90.0 and record["is_picture"]
    pictures = json.loads((out / "pictures.json").read_text(encoding="utf-8"))
    assert pictures["items"][0]["rotation"] == 90.0
    info = json.loads((out / "slide_info.json").read_text(encoding="utf-8"))
    assert info["slides"][0]["picture_coverage_pct"] == pytest.approx(100 * 4 / (10 * 7.5), abs=0.05)
    assert all(c["pass"] for c in manifest["checks"])


# --------------------------------------------------------------------------- #
# 12. Source replaced in place (new sha, same out dir): rerun happens, stale files linger
# --------------------------------------------------------------------------- #
def test_source_changed_in_place_reruns_and_updates_manifest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    def build(with_table: bool) -> None:
        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        if with_table:
            slide.shapes.add_table(1, 1, Inches(1), Inches(1), Inches(2), Inches(1)).table.cell(0, 0).text = "x"
        slide.shapes.add_textbox(Inches(1), Inches(3), Inches(2), Inches(1)).text_frame.text = "v%d" % with_table
        prs.save(str(deck))

    deck = tmp_path / "deck.pptx"
    out = tmp_path / "out"
    args = ["run", "--pptx", str(deck), "--out", str(out), "--no-progress"]
    build(True)
    assert sp.main(args) == 0
    first_sha = json.loads((out / "sidecar_manifest.json").read_text(encoding="utf-8"))["sha256"]
    assert (out / "tables_s1_1.csv").is_file()
    build(False)
    assert sp.main(args) == 0
    assert "done:" in capsys.readouterr().out
    manifest = json.loads((out / "sidecar_manifest.json").read_text(encoding="utf-8"))
    assert manifest["sha256"] != first_sha and manifest["counts"]["table_count"] == 0
    assert "tables_s1_1.csv" not in manifest["files"]
    # Documented behaviour (I-08): the stale CSV from the previous version is left on disk.
    assert (out / "tables_s1_1.csv").is_file()


# --------------------------------------------------------------------------- #
# 13. sldId lookup must not confuse r:id with id whatever the attribute order
# --------------------------------------------------------------------------- #
def test_sld_id_lookup_ignores_attribute_order() -> None:
    element = etree.fromstring(b'<p:sldId xmlns:p="p" xmlns:r="r" r:id="rId2" id="256"/>')
    assert sp._attr(element, "id") == "256"  # noqa: SLF001


# --------------------------------------------------------------------------- #
# 14. Macro-enabled and show variants are refused rather than read
# --------------------------------------------------------------------------- #
def test_pptm_and_ppsx_are_refused(tmp_path: Path) -> None:
    plain = tmp_path / "pic.pptx"
    _one_picture_deck(plain)
    for suffix in (".pptm", ".ppsx"):
        other = tmp_path / f"deck{suffix}"
        other.write_bytes(plain.read_bytes())
        assert sp.main(["run", "--pptx", str(other), "--out", str(tmp_path / "o"), "--no-progress"]) == 1
        assert not (tmp_path / "o").exists()
