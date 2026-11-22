"""Adversarial tests for ``src.convert``, ``src.md_writer`` and ``src.recall_check`` (independent review).

A synthetic project tree whose department folder and file names carry unicode,
spaces and brackets holds: a rotated text page with a table, an A1 drawing page
rotated 90 degrees, a PDF with zero annotations, a FreeText annotation without
contents at the head of a reply chain (plus a dangling reply), a deck without
any comment part, a workbook with a veryHidden sheet, a cached ``#DIV/0!`` and a
formula without a cached value, an e-mail without attachments, a hash-identical
duplicate and a zip.  No real source file is used.  Known defects found by the
review are pinned with ``xfail(strict=True)`` so the suite stays green until
they are fixed, at which point the marker must be removed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, List, Tuple

import pymupdf
import pytest
import yaml

from src import checks, convert
from src import md_writer as mw
from src import recall_check as rc
from src.common import append_jsonl, atomic_write_json, read_jsonl, sha256_file

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "convert"
DEPT = "DD Sign-OFF/30 - Pharmacy [DD Closure] (ünïcödé)"
REL_ROT = f"{DEPT}/Submitted/AHU Zoning – RFI-ME-0233 [Rev A] (copy).pdf"
REL_DRAW = f"{DEPT}/Submitted/Drawing IP-BH-05010 [A1] (rot 90).pdf"
REL_NOANN = f"{DEPT}/Submitted/plain (no annotations).pdf"
REL_FREE = f"{DEPT}/AH sign off with comments/replies [FreeText].pdf"
REL_DECK = f"{DEPT}/Submitted/DD Beca deck (no comments).pptx"
REL_BOOK = f"{DEPT}/Submitted/03_SOA/30_Pharmacy_SOA [veryHidden] (ünï).xlsx"
REL_EML = f"{DEPT}/AH sign off with comments/reply (no attachments).eml"
REL_DUP = "Basis/duplicate of plain [copy] (ünï).pdf"
REL_ZIP = f"{DEPT}/Submitted/batch [3B].zip"
VH_SHEET = "Secret (vh)"


# --------------------------------------------------------------------------- #
# Fixture builders (small synthetic files only)
# --------------------------------------------------------------------------- #
def _text_page(doc: pymupdf.Document, rotation: int = 0, with_table: bool = True) -> pymupdf.Page:
    """An A4 page with enough text to be a text page and, optionally, a 6 x 3 text table."""
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


def _drawing_page(doc: pymupdf.Document) -> None:
    """An A1 page with 600 vector paths and a few labels, rotated 90 degrees."""
    page = doc.new_page(width=2384, height=1684)
    for i in range(600):  # one path per call (a single Shape.commit would be one path)
        x = 200 + (i % 60) * 30
        y = 200 + (i // 60) * 40
        page.draw_line((x, y), (x + 20, y + 10), width=0.5)
    page.insert_text((100, 100), "AHU-07 supply 1,250 L/s", fontsize=14)
    page.insert_text((100, 300), "PH-303 PHARMACY 32.5 m2", fontsize=14)
    page.insert_text((2200, 1600), "IP-BH-05010 REV B", fontsize=12)
    page.set_rotation(90)


def _pdf(path: Path, *builders) -> None:
    doc = pymupdf.open()
    for build in builders:
        build(doc)
    doc.save(str(path))
    doc.close()


def _freetext_chain(doc: pymupdf.Document) -> None:
    """A FreeText with no contents, two replies chained behind it and a reply to a dangling xref."""
    page = doc.new_page()
    page.insert_text((50, 30), "Reply chain page 45 Pa", fontsize=11)
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
    orphan.set_info(title="Dee")
    orphan.update()
    doc.xref_set_key(r1.xref, "IRT", f"{free.xref} 0 R")
    doc.xref_set_key(r2.xref, "IRT", f"{r1.xref} 0 R")
    doc.xref_set_key(orphan.xref, "IRT", "999 0 R")


def _deck(path: Path) -> None:
    """Two slides, a table on the second, no comment part at all."""
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[5])
    s1.shapes.title.text = "AHU-07 supply 1,250 L/s"
    s1.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1)).text_frame.text = "Room pressure +15 Pa"
    s2 = prs.slides.add_slide(prs.slide_layouts[6])
    table = s2.shapes.add_table(2, 3, Inches(0.5), Inches(0.5), Inches(6), Inches(1.5)).table
    for r, row in enumerate([["Room", "Area m2", "Pressure Pa"], ["Pharmacy", "32.5", "45"]]):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    prs.save(str(path))


def _workbook(path: Path) -> None:
    """A visible sheet with a cached error and an uncached formula, plus a veryHidden sheet."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Loads & Flows (2)"
    ws.append(["Room", "Flow L/s", "Ratio"])
    ws.append(["Ward 1", 1250, "#DIV/0!"])
    ws.append(["Ward 2", 300, "=B3/0"])
    ws.append(["Ward 3", 45.5, 0.5])
    for cell in ("B2", "B3", "B4"):  # dominant format of the column names the unit
        ws[cell].number_format = '0 "L/s"'
    vh = wb.create_sheet(VH_SHEET)
    vh["A1"] = "hidden value"
    vh["B1"] = 777
    vh.sheet_state = "veryHidden"
    wb.save(path)


def _eml(path: Path) -> None:
    msg = EmailMessage()
    msg["From"] = "Alice Reviewer <alice@example.com>"
    msg["To"] = "bob@example.com"
    msg["Subject"] = "RE: Pharmacy flow 1,200 L/s"
    msg["Date"] = "Mon, 02 Mar 2026 10:15:00 +0800"
    msg.set_content("Body confirms 45 Pa and 2,400 L/s.\n")
    path.write_bytes(msg.as_bytes())


def _row(root: Path, rel: str, **extra: object) -> Dict[str, object]:
    path = root / "data" / "source" / rel
    row: Dict[str, object] = {"drive_id": f"id-{abs(hash(rel)) % 10**6}", "rel_path": rel,
                              "local_path": f"data/source/{rel}", "bytes": path.stat().st_size,
                              "sha256": sha256_file(path), "transport": "connector",
                              "fetched_at": "2026-11-22T10:00:00Z", "size_match": True}
    row.update(extra)
    return row


def _snapshot(folder: Path) -> Dict[str, Tuple[int, int]]:
    """``{relative path: (size, mtime_ns)}`` of every file under ``folder``."""
    return {str(p.relative_to(folder)): (p.stat().st_size, p.stat().st_mtime_ns) for p in folder.rglob("*") if p.is_file()}


def build_tree(root: Path) -> Dict[str, Tuple[int, int]]:
    """Create the tree, its ledgers and return a snapshot of ``data/source``."""
    for rel in ("config", "state", "out", "data/source"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "project.yaml", root / "config" / "project.yaml")
    src = root / "data" / "source"
    for rel in (REL_ROT, REL_DRAW, REL_NOANN, REL_FREE, REL_DECK, REL_BOOK, REL_EML, REL_DUP, REL_ZIP):
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
    _pdf(src / REL_ROT, lambda d: _text_page(d, rotation=90))
    _pdf(src / REL_DRAW, _drawing_page)
    _pdf(src / REL_NOANN, lambda d: _text_page(d, with_table=False))
    _pdf(src / REL_FREE, _freetext_chain)
    _deck(src / REL_DECK)
    _workbook(src / REL_BOOK)
    _eml(src / REL_EML)
    shutil.copy(src / REL_NOANN, src / REL_DUP)
    (src / REL_ZIP).write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    pull = root / "state" / "pull_ledger.jsonl"
    for rel in (REL_ROT, REL_DRAW, REL_NOANN, REL_FREE, REL_DECK, REL_BOOK, REL_DUP, REL_ZIP):
        append_jsonl(pull, _row(root, rel))
    append_jsonl(root / "state" / "upload_ledger.jsonl", _row(root, REL_EML, transport="upload"))
    atomic_write_json(root / "state" / "progress.json", {"phase": 1, "items": {}})
    return _snapshot(src)


@pytest.fixture(scope="module")
def tree(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, object]:
    root = tmp_path_factory.mktemp("convert_adv") / "proj [ünï]"
    before = build_tree(root)
    os.environ["AIGH_ROOT"] = str(root)
    rc_code = convert.main(["run", "--root", str(root), "--workers", "2", "--timeout", "600"])
    return {"root": root, "rc": rc_code, "before": before}


def _root(tree: Dict[str, object]) -> Path:
    return tree["root"]  # type: ignore[return-value]


def _by_rel(root: Path) -> Dict[str, Dict[str, object]]:
    return {str(r["rel_path"]): r for r in read_jsonl(root / "state" / "convert_ledger.jsonl")}


def _md(root: Path, rel: str) -> str:
    return (root / str(_by_rel(root)[rel]["md_path"])).read_text(encoding="utf-8")


def _checks(root: Path, rel: str) -> Dict[str, object]:
    return json.loads((root / str(_by_rel(root)[rel]["checks_path"])).read_text(encoding="utf-8"))


def _front(text: str) -> Dict[str, object]:
    return yaml.safe_load(text.split("\n---\n")[0][4:])


def _h2(text: str) -> List[str]:
    return [line for line in text.splitlines() if line.startswith("## ")]


def _section(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def _table_rows(block: str) -> List[List[str]]:
    """Data rows of the first Markdown table in ``block`` (cells stripped)."""
    lines = [line for line in block.splitlines() if line.startswith("|")]
    return [[c.strip() for c in line.strip("|").split("|")] for line in lines[2:]]


# --------------------------------------------------------------------------- #
# 1. Unicode names with spaces and brackets; the source tree is never written
# --------------------------------------------------------------------------- #
def test_unicode_tree_converts_and_source_is_untouched(tree: Dict[str, object]) -> None:
    root = _root(tree)
    assert tree["rc"] == 0
    rows = _by_rel(root)
    assert set(rows) == {REL_ROT, REL_DRAW, REL_NOANN, REL_FREE, REL_DECK, REL_BOOK, REL_EML, REL_DUP, REL_ZIP}
    for rel in (REL_ROT, REL_DRAW, REL_NOANN, REL_FREE, REL_DECK, REL_BOOK, REL_EML):
        row = rows[rel]
        assert row["status"] == "converted" and row["error"] is None, row
        expected_md = "out/markdown/" + re.sub(r"\.[A-Za-z0-9]+$", ".md", rel)
        assert row["md_path"] == expected_md, row["md_path"]
        assert (root / str(row["md_path"])).is_file()
        assert row["sidecar_dir"] == f"out/sidecar/{rel}" and (root / str(row["sidecar_dir"])).is_dir()
        front = _front(_md(root, rel))
        assert list(front) == list(mw.FRONT_MATTER_KEYS) and front["source_path"] == rel
        assert front["dept_no"] == "30" and front["dept_name"] == "Pharmacy [DD Closure] (ünïcödé)"
    assert _snapshot(root / "data" / "source") == tree["before"], "data/source changed during conversion"
    assert rows[REL_ROT]["doc_class"] == "layout plan" or rows[REL_ROT]["doc_class"] == "pdf document"
    assert rows[REL_DRAW]["doc_class"] == "schematic" and rows[REL_BOOK]["doc_class"] == "SOA"


# --------------------------------------------------------------------------- #
# 2. Rotated pages: sizes and boxes in page view, rotation column filled
# --------------------------------------------------------------------------- #
def test_rotated_text_page_uses_page_view_frame(tree: Dict[str, object]) -> None:
    root = _root(tree)
    text = _md(root, REL_ROT)
    assert "| page sizes (page view) | 842.0 x 595.0 pt (1) |" in text
    tables = _section(text, "### Tables", "### Annotations and comments")
    captions = re.findall(r"bbox ([\d.]+), ([\d.]+), ([\d.]+), ([\d.]+) \(page view\)", tables)
    assert captions, "the rotated text page lost its pdfplumber table"
    for x0, y0, x1, y1 in captions:
        # raw table occupies x 60..360, y 300..400; in page view (rotate 90) x = 842 - raw y >= 440
        assert 400 <= float(x0) < float(x1) <= 842.0 and 0 <= float(y0) < float(y1) <= 595.0, captions
    assert _checks(root, REL_ROT)["tables"]["match"] and _by_rel(root)[REL_ROT]["grade"] in ("A", "B")


def test_rotated_drawing_page_blocks_have_view_boxes_and_rotation(tree: Dict[str, object]) -> None:
    root = _root(tree)
    text = _md(root, REL_DRAW)
    assert "| drawing pages | 1 |" in text and "| page sizes (page view) | 1684.0 x 2384.0 pt (1) |" in text
    native = _section(text, "### Native text (sidecar)", "### Tables")
    assert native.splitlines()[1] == "<!-- method: pymupdf-text -->"
    assert native.splitlines()[2].startswith("Drawing page: every text block with its page-view box and rotation")
    header = [line for line in native.splitlines() if line.startswith("| text |")][0]
    assert header == "| text | x0 (pt) | y0 (pt) | x1 (pt) | y1 (pt) | rotation (deg) |"
    rows = _table_rows(native)
    texts = {r[0] for r in rows}
    assert any("1,250" in t for t in texts) and any("IP-BH-05010" in t for t in texts), texts
    for row in rows:
        x0, y0, x1, y1, rot = (float(row[1]), float(row[2]), float(row[3]), float(row[4]), int(row[5]))
        assert 0 <= x0 <= x1 <= 1684.5 and 0 <= y0 <= y1 <= 2384.5, row
        assert rot == 270, row  # unrotated text on a /Rotate 90 page is displayed at 270 degrees
    # raw label at x 100 sits at view x = 1684 - 100 on the right; the raw frame would put it near 100
    assert any(float(r[1]) > 1400 for r in rows), rows
    assert "Title block (parsed)" in native and "IP-BH-05010" in native


# --------------------------------------------------------------------------- #
# 3. Zero annotations
# --------------------------------------------------------------------------- #
def test_pdf_with_zero_annotations(tree: Dict[str, object]) -> None:
    root = _root(tree)
    text = _md(root, REL_NOANN)
    assert _h2(text) == ["## Document", "## Page 1", "## Comments register", "## Provenance"]
    assert "| annotations | 0 |" in text
    unit = _section(text, "### Annotations and comments", "<!-- section: comments-register -->")
    assert unit.strip() == "(no annotations or comments)"
    register = _section(text, "## Comments register", "<!-- section: provenance -->")
    assert register.strip() == "(no annotations or comments in this file)"
    ch = _checks(root, REL_NOANN)
    assert ch["annotations"] == {"sidecar": 0, "register": 0, "match": True}
    assert ch["grade"] in ("A", "B") and ch["native_layer"]["present"]


# --------------------------------------------------------------------------- #
# 4. FreeText without contents, reply chain, dangling reply
# --------------------------------------------------------------------------- #
def test_freetext_without_contents_and_reply_chain(tree: Dict[str, object]) -> None:
    root = _root(tree)
    text = _md(root, REL_FREE)
    register = _section(text, "## Comments register", "<!-- section: provenance -->")
    rows = _table_rows(register)
    assert len(rows) == 4 and _checks(root, REL_FREE)["annotations"]["match"]
    cols = list(mw.REGISTER_COLUMNS)
    by_author = {r[cols.index("author")]: r for r in rows}
    assert set(by_author) == {"Ana", "Bob", "Cy", "Dee"}
    ana = by_author["Ana"]
    assert ana[cols.index("type")] == "FreeText" and ana[cols.index("subject")] == "Empty"
    assert ana[cols.index("text")] == "" and ana[cols.index("reply_to")] == ""
    assert "rect" in ana[cols.index("location")] and "(page view)" in ana[cols.index("location")]
    assert by_author["Bob"][cols.index("reply_to")] == ana[cols.index("index")]
    assert by_author["Cy"][cols.index("reply_to")] == by_author["Bob"][cols.index("index")]
    assert by_author["Dee"][cols.index("reply_to")] == ""  # dangling /IRT never resolves to a row
    unit_rows = _table_rows(_section(text, "### Annotations and comments", "<!-- section: comments-register -->"))
    assert len(unit_rows) == 4 and all(r[0].isdigit() for r in unit_rows)
    assert _by_rel(root)[REL_FREE]["grade"] in ("A", "B")


# --------------------------------------------------------------------------- #
# 5. PPTX without any comment part
# --------------------------------------------------------------------------- #
def test_deck_without_comment_parts(tree: Dict[str, object]) -> None:
    root = _root(tree)
    text = _md(root, REL_DECK)
    assert _h2(text) == ["## Document", "## Slide 1", "## Slide 2", "## Comments register", "## Provenance"]
    assert "| comments | 0 |" in text and "| legacy comment parts | 0 |" in text and "| modern comment parts | 0 |" in text
    assert text.count("### Pictures") == 2 and text.count("(no pictures on this slide)") == 2
    assert text.count("(no annotations or comments)") == 2
    assert "(no annotations or comments in this file)" in _section(text, "## Comments register", "<!-- section: provenance -->")
    slide2 = _section(text, "## Slide 2\n", "<!-- section: comments-register -->")
    assert "Table S2-1: slide 2 table shape" in slide2 and "| Pharmacy | 32.5 | 45 |" in slide2
    ch = _checks(root, REL_DECK)
    assert ch["annotations"] == {"sidecar": 0, "register": 0, "match": True} and ch["tables"]["match"]
    assert ch["grade"] in ("A", "B") and _front(text)["pages_or_slides_or_sheets"] == 2


# --------------------------------------------------------------------------- #
# 6. Workbook: veryHidden sheet, cached error, formula without cached value
# --------------------------------------------------------------------------- #
def test_workbook_very_hidden_sheet_and_formula_error(tree: Dict[str, object]) -> None:
    root = _root(tree)
    text = _md(root, REL_BOOK)
    assert _h2(text) == ["## Document", "## Sheet Loads & Flows (2)", f"## Sheet {VH_SHEET}", "## Comments register",
                         "## Provenance"]
    assert f"| 2 | {VH_SHEET} | veryHidden |" in text and "| hidden sheets | 1 |" in text
    loads = _section(text, "## Sheet Loads & Flows (2)\n", f"## Sheet {VH_SHEET}\n")
    assert "1 formula cells (1 without a cached value" in loads
    assert "| 2 | Ward 1 | 1250 | #DIV/0! |" in loads, "the cached error value must be in the sheet range table"
    assert "| row | A | B (L/s) | C |" in loads, "unit from the number format belongs in the column header"
    hidden = _section(text, f"## Sheet {VH_SHEET}\n", "<!-- section: comments-register -->")
    assert "Sheet state veryHidden" in hidden and "| 1 | hidden value | 777 |" in hidden
    markitdown_part = hidden.split("### Native text (sidecar)")[0]
    assert "777" in markitdown_part, "MarkItDown's text for the veryHidden sheet must sit under its own unit"
    ch = _checks(root, REL_BOOK)
    assert ch["tables"] == {"sidecar": 2, "markdown": 2, "match": True} and ch["annotations"]["match"]
    assert ch["grade"] in ("A", "B") and ch["numeric_recall"] >= 0.995, ch
    assert _front(text)["pages_or_slides_or_sheets"] == 2


# --------------------------------------------------------------------------- #
# 7. E-mail without attachments
# --------------------------------------------------------------------------- #
def test_eml_without_attachments(tree: Dict[str, object]) -> None:
    root = _root(tree)
    text = _md(root, REL_EML)
    assert _h2(text) == ["## Document", "## Message", "## Comments register", "## Provenance"]
    assert "| subject | RE: Pharmacy flow 1,200 L/s |" in text
    assert "Attachments (0; listed only, none opened):" in text and "(no attachments)" in text
    assert "| count attachments | 0 |" in text
    ch = _checks(root, REL_EML)
    assert ch["grade"] in ("A", "B") and ch["numeric_recall"] == 1.0 and ch["native_layer"]["present"]
    front = _front(text)
    assert front["stage"] == "AH sign off with comments" and front["doc_class"] == "correspondence"


# --------------------------------------------------------------------------- #
# 8. Duplicate and zip inside the unicode tree
# --------------------------------------------------------------------------- #
def test_duplicate_and_zip(tree: Dict[str, object]) -> None:
    root = _root(tree)
    rows = _by_rel(root)
    dup = rows[REL_DUP]
    assert dup["status"] == "duplicate" and dup["duplicate_of"] == REL_NOANN and dup["grade"] == "pending"
    text = (root / str(dup["md_path"])).read_text(encoding="utf-8")
    assert f"`{REL_NOANN}`" in text and "plain (no annotations).md" in text
    assert not (root / "out" / "sidecar" / REL_DUP).exists()
    zip_row = rows[REL_ZIP]
    assert zip_row["status"] == "skipped" and zip_row["md_path"] is None and zip_row["kind"] == "zip"
    assert not (root / "out" / "sidecar" / REL_ZIP).exists()
    assert not list((root / "out" / "markdown").rglob("*batch*"))


# --------------------------------------------------------------------------- #
# 9. Idempotent re-run and path filters with unicode
# --------------------------------------------------------------------------- #
def test_rerun_is_a_no_op(tree: Dict[str, object], capsys: pytest.CaptureFixture[str]) -> None:
    root = _root(tree)
    before_out = _snapshot(root / "out")
    before_rows = len(read_jsonl(root / "state" / "convert_ledger.jsonl"))
    assert convert.main(["run", "--root", str(root), "--workers", "1"]) == 0
    assert "nothing to do" in capsys.readouterr().out
    assert _snapshot(root / "out") == before_out
    assert len(read_jsonl(root / "state" / "convert_ledger.jsonl")) == before_rows


def test_paths_filter_accepts_unicode_bracketed_paths(tree: Dict[str, object], capsys: pytest.CaptureFixture[str]) -> None:
    root = _root(tree)
    local = root / "data" / "source" / REL_BOOK
    assert convert.main(["run", "--root", str(root), "--force", "--dry-run", "--paths", str(local)]) == 0
    out = capsys.readouterr().out
    assert REL_BOOK in out and "1 file(s) would run" in out
    assert convert.main(["run", "--root", str(root), "--force", "--dry-run", "--paths", REL_DUP]) == 0
    out = capsys.readouterr().out
    assert "dup " in out and REL_DUP in out


def test_phase1_checks_pass_on_the_unicode_tree(tree: Dict[str, object]) -> None:
    root = _root(tree)
    os.environ["AIGH_ROOT"] = str(root)
    assert checks.main(["phase1", "--scope", "converted", "--quiet"]) == 0
    results = {r.check_id: r for r in checks.run_phase(1, checks.Context(scope="all"))}
    assert all(r.status == checks.PASS for r in results.values()), {k: v.evidence for k, v in results.items()}


# --------------------------------------------------------------------------- #
# 10. A worker that dies mid-file must not hang the run
# --------------------------------------------------------------------------- #
def _crash_or_pass(task_dict: Dict[str, object]) -> Dict[str, object]:
    """Worker stand-in: exit hard for the file named ``crash``, succeed for the rest."""
    if str(task_dict["rel_path"]).endswith("crash"):
        os._exit(3)
    return convert._result(str(task_dict["sha256"]), str(task_dict["rel_path"]), convert.STATUS_CONVERTED)  # noqa: SLF001


def _task(root: Path, rel: str, timeout: int) -> convert.Task:
    return convert.Task(sha256="0" * 63 + rel[-1], rel_path=rel, local_path=str(root / rel), bytes=1, drive_id=None,
                        drive_modified=None, kind="pdf", doc_class="pdf document", md_rel="x.md", root=str(root),
                        markdown_dir=str(root / "out" / "markdown"), sidecar_dir=str(root / "out" / "sidecar" / rel),
                        state_dir=str(root / "state"), timeout_s=timeout, force=True)


def test_worker_crash_becomes_error_row_and_pool_recovers(tree: Dict[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    root = _root(tree)
    monkeypatch.setattr(convert, "convert_task", _crash_or_pass)
    monkeypatch.setattr(convert, "HARD_TIMEOUT_GRACE_S", 2)
    tasks = [_task(root, "crash", 1), _task(root, "ok", 1)]
    rows = {r["rel_path"]: r for r in convert.run_tasks(tasks, workers=2)}
    assert set(rows) == {"crash", "ok"}
    assert rows["ok"]["status"] == "converted"
    assert rows["crash"]["status"] == "error" and "timeout" in str(rows["crash"]["error"])


# --------------------------------------------------------------------------- #
# 11. Known defects, pinned (remove the marker once fixed)
# --------------------------------------------------------------------------- #
@pytest.mark.xfail(strict=True, reason="split_xlsx_text compares MarkItDown's stripped '## <sheet>' heading with the "
                                       "unstripped sheet name: a sheet named 'Secret ' is glued to the previous sheet")
def test_sheet_name_with_trailing_space_is_split(tmp_path: Path) -> None:
    from markitdown import MarkItDown
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.title = "Main"
    wb.active["A1"] = 1
    vh = wb.create_sheet("Secret ")
    vh["A1"] = "hidden 777"
    path = tmp_path / "ts.xlsx"
    wb.save(path)
    text = MarkItDown(enable_plugins=False).convert(str(path)).text_content
    segments, note = mw.split_xlsx_text(text, ["Main", "Secret "])
    assert "Secret " in segments and "777" in segments["Secret "] and "777" not in segments["Main"], (segments, note)


@pytest.mark.xfail(strict=True, reason="a shape whose text is literally '<!-- Slide number: 9 -->' creates a phantom "
                                       "slide 9: the text after it is dropped from every unit without a note")
def test_literal_slide_marker_in_shape_text_is_not_lost(tmp_path: Path) -> None:
    from markitdown import MarkItDown
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[5])
    s1.shapes.title.text = "Deck 1,250 L/s"
    s2 = prs.slides.add_slide(prs.slide_layouts[5])
    s2.shapes.title.text = "Second 45 Pa"
    s2.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1)).text_frame.text = "<!-- Slide number: 9 --> note 77"
    path = tmp_path / "deck.pptx"
    prs.save(str(path))
    text = MarkItDown(enable_plugins=False).convert(str(path)).text_content
    segments, note = mw.split_pptx_text(text, 2)
    assert set(segments) <= {1, 2} or note, segments
    assert "note 77" in "".join(segments.get(n, "") for n in (1, 2))


@pytest.mark.xfail(strict=True, reason="count_tables treats a data row made of '---' cells (a slide or pdfplumber "
                                       "table with dashes for 'n/a') as a second table separator")
def test_dashes_data_row_is_not_a_second_table() -> None:
    table = mw.grid_table([["Room", "Flow"], ["Ward 1", "1250"], ["---", "---"], ["Ward 3", "45"]])
    assert rc.count_tables(table.splitlines()) == 1
