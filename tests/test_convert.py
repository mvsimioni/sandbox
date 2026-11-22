"""End-to-end tests for ``src.convert`` on a synthetic project tree.

The tree holds the fixtures of the sidecar tests (a three-page PDF, a deck
with comments, a workbook with threaded comments), an e-mail, a photo, a
hash-identical duplicate of the PDF, two files whose names collide once the
extension is dropped, and a zip that must never be opened.  No real source
file is used.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List

import pytest
import yaml
from PIL import Image

from src import checks, convert
from src import md_writer as mw
from src.common import append_jsonl, atomic_write_json, read_jsonl, sha256_file
from tests.test_sidecar_pdf import build_fixture as build_pdf
from tests.test_sidecar_pptx import build_deck, inject_comments
from tests.test_sidecar_xlsx import build_workbook, inject_threaded

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "convert"
DEPT = "DD Sign-OFF/30 - Pharmacy"
REL_PDF = f"{DEPT}/Submitted/30_Pharmacy_RDS.pdf"
REL_PPTX = f"{DEPT}/Submitted/DD Beca reply deck.pptx"
REL_XLSX = f"{DEPT}/Submitted/03_SOA/30_Pharmacy_SOA.xlsx"
REL_EML = f"{DEPT}/AH sign off with comments/reply.eml"
REL_PNG = f"{DEPT}/Submitted/photo.png"
REL_DUP = "Basis/copy of 30_Pharmacy_RDS.pdf"
REL_COLL_PDF = "Basis/Sched.pdf"
REL_COLL_XLSX = "Basis/Sched.xlsx"
REL_ZIP = "DD Sign-OFF/26 - Diagnostic Imaging.zip"
EML_TEXT = ("From: a@example.com\nTo: b@example.com\nSubject: Flow 1,200 L/s\n"
            "Date: Mon, 02 Mar 2026 10:00:00 +0800\nContent-Type: text/plain\n\nBody confirms 45 Pa and 2400 L/s.\n")


# --------------------------------------------------------------------------- #
# Synthetic project tree
# --------------------------------------------------------------------------- #
def _small_pdf(path: Path) -> None:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 60), "Schedule 12 units 300 L/s", fontsize=11)
    doc.save(str(path))
    doc.close()


def _ledger_row(root: Path, rel: str, transport: str = "connector", **extra: object) -> Dict[str, object]:
    path = root / "data" / "source" / rel
    row: Dict[str, object] = {"drive_id": f"id-{abs(hash(rel)) % 10**6}", "rel_path": rel,
                              "local_path": f"data/source/{rel}", "bytes": path.stat().st_size,
                              "sha256": sha256_file(path), "transport": transport, "fetched_at": "2026-11-22T10:00:00Z",
                              "size_match": True}
    row.update(extra)
    return row


def build_tree(root: Path) -> Path:
    """Create the synthetic project tree with its files and ledgers."""
    for rel in ("config", "state", "out", "data/source"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "project.yaml", root / "config" / "project.yaml")
    src = root / "data" / "source"
    for rel in (REL_PDF, REL_PPTX, REL_XLSX, REL_EML, REL_PNG, REL_DUP, REL_COLL_PDF, REL_COLL_XLSX, REL_ZIP):
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
    build_pdf(src / REL_PDF)
    shutil.copy(src / REL_PDF, src / REL_DUP)
    plain_deck = root / "plain.pptx"
    ids = build_deck(plain_deck)
    inject_comments(plain_deck, src / REL_PPTX, ids["sld_id_2"], ids["s2_table_id"])
    plain_book = root / "plain.xlsx"
    build_workbook(plain_book)
    inject_threaded(plain_book, src / REL_XLSX)
    shutil.copy(plain_book, src / REL_COLL_XLSX)
    (src / REL_EML).write_text(EML_TEXT, encoding="utf-8")
    Image.new("RGB", (40, 30), (10, 20, 30)).save(src / REL_PNG)
    _small_pdf(src / REL_COLL_PDF)
    (src / REL_ZIP).write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    pull = root / "state" / "pull_ledger.jsonl"
    for rel in (REL_PDF, REL_PPTX, REL_XLSX, REL_PNG, REL_DUP, REL_COLL_PDF, REL_COLL_XLSX, REL_ZIP):
        append_jsonl(pull, _ledger_row(root, rel))
    append_jsonl(root / "state" / "upload_ledger.jsonl",
                 _ledger_row(root, REL_EML, transport="upload", drive_modified="2026-03-02T02:00:00Z"))
    atomic_write_json(root / "state" / "progress.json", {"phase": 1, "items": {}})
    return root


@pytest.fixture(scope="module")
def tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The tree converted once with a two-worker pool."""
    root = build_tree(tmp_path_factory.mktemp("convert") / "proj")
    os.environ["AIGH_ROOT"] = str(root)
    rc = convert.main(["run", "--root", str(root), "--workers", "2", "--timeout", "600"])
    assert rc == 0
    return root


def _rows(root: Path) -> List[Dict[str, object]]:
    return read_jsonl(root / "state" / "convert_ledger.jsonl")


def _by_rel(root: Path) -> Dict[str, Dict[str, object]]:
    return {str(r["rel_path"]): r for r in _rows(root)}


def _front_matter(text: str) -> Dict[str, object]:
    assert text.startswith("---\n")
    return yaml.safe_load(text.split("\n---\n")[0][4:])


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #
def test_ledger_rows_and_statuses(tree: Path) -> None:
    rows = _by_rel(tree)
    assert set(rows) == {REL_PDF, REL_PPTX, REL_XLSX, REL_EML, REL_PNG, REL_DUP, REL_COLL_PDF, REL_COLL_XLSX, REL_ZIP}
    for rel in (REL_PDF, REL_PPTX, REL_XLSX, REL_EML, REL_PNG, REL_COLL_PDF, REL_COLL_XLSX):
        row = rows[rel]
        assert row["status"] == "converted" and row["error"] is None, row
        assert (tree / str(row["md_path"])).is_file() and (tree / str(row["checks_path"])).is_file()
        assert row["pipeline_version"] == convert.PIPELINE_VERSION and row["seconds"] > 0
        assert {c["name"] for c in row["converters"]} >= {"markitdown"}
    assert rows[REL_PDF]["grade"] in ("A", "B") and rows[REL_PPTX]["grade"] in ("A", "B")
    assert rows[REL_XLSX]["grade"] in ("A", "B") and rows[REL_EML]["grade"] in ("A", "B")
    assert rows[REL_COLL_PDF]["grade"] in ("A", "B") and rows[REL_COLL_XLSX]["grade"] in ("A", "B")
    assert rows[REL_PNG]["grade"] == "pending" and rows[REL_PNG]["kind"] == "image"
    assert rows[REL_PDF]["doc_class"] == "RDS" and rows[REL_XLSX]["doc_class"] == "SOA"
    progress = json.loads((tree / "state" / "progress.json").read_text(encoding="utf-8"))["items"]
    assert progress[f"convert:{rows[REL_PDF]['sha256']}"]["status"] == "done"


def test_duplicate_points_to_first_copy(tree: Path) -> None:
    rows = _by_rel(tree)
    dup = rows[REL_DUP]
    assert dup["status"] == "duplicate" and dup["duplicate_of"] == REL_PDF and dup["sha256"] == rows[REL_PDF]["sha256"]
    text = (tree / str(dup["md_path"])).read_text(encoding="utf-8")
    front = _front_matter(text)
    assert list(front) == list(mw.FRONT_MATTER_KEYS) and front["grade"] == "pending"
    assert "hash-identical duplicate of " + REL_PDF in front["grade_reason"]
    body = text.split("\n---\n", 1)[1].strip().splitlines()
    assert body[0] == "# copy of 30_Pharmacy_RDS.pdf" and len(body) == 3 and body[2].startswith("Hash-identical duplicate")
    assert f"`{REL_PDF}`" in body[2] and "Submitted/30_Pharmacy_RDS.md" in body[2]
    assert not (tree / "out" / "sidecar" / REL_DUP).exists()


def test_zip_is_recorded_but_never_opened(tree: Path) -> None:
    row = _by_rel(tree)[REL_ZIP]
    assert row["status"] == "skipped" and row["md_path"] is None and "never opened" in str(row["grade_reason"])
    assert not (tree / "out" / "sidecar" / REL_ZIP).exists()
    assert not list((tree / "out" / "markdown").rglob("*Imaging*"))


def test_collision_keeps_extension(tree: Path) -> None:
    rows = _by_rel(tree)
    assert rows[REL_COLL_PDF]["md_path"] == "out/markdown/Basis/Sched.pdf.md"
    assert rows[REL_COLL_XLSX]["md_path"] == "out/markdown/Basis/Sched.xlsx.md"
    assert rows[REL_PDF]["md_path"] == f"out/markdown/{DEPT}/Submitted/30_Pharmacy_RDS.md"


# --------------------------------------------------------------------------- #
# Markdown layout
# --------------------------------------------------------------------------- #
def _md(tree: Path, rel: str) -> str:
    return (tree / str(_by_rel(tree)[rel]["md_path"])).read_text(encoding="utf-8")


def _h2(text: str) -> List[str]:
    return [line for line in text.splitlines() if line.startswith("## ")]


def test_pdf_markdown_layout(tree: Path) -> None:
    text = _md(tree, REL_PDF)
    front = _front_matter(text)
    assert list(front) == list(mw.FRONT_MATTER_KEYS)
    assert front["source_path"] == REL_PDF and front["root"] == "DD Sign-OFF" and front["dept_no"] == "30"
    assert front["dept_name"] == "Pharmacy" and front["stage"] == "Submitted" and front["doc_class"] == "RDS"
    assert front["pages_or_slides_or_sheets"] == 3 and front["grade"] in ("A", "B") and front["grade_reason"]
    assert front["pipeline_version"] == convert.PIPELINE_VERSION and front["methods_used"][0] == "markitdown"
    assert [c["name"] for c in front["converters"]][:2] == ["markitdown", "pymupdf"]
    heads = _h2(text)
    assert heads == ["## Document", "## Page 1", "## Page 2", "## Page 3", "## Comments register", "## Provenance"]
    assert text.split("\n---\n", 1)[1].lstrip().startswith("# 30_Pharmacy_RDS.pdf")
    page1 = text.split("## Page 1\n")[1].split("## Page 2\n")[0]
    order = [page1.find(s) for s in ("<!-- method: markitdown -->", "### Native text (sidecar)",
                                     "<!-- method: pymupdf-text -->", "### Tables", "Table P1-1: pdfplumber",
                                     "### Annotations and comments")]
    assert order == sorted(order) and -1 not in order
    register = text.split("## Comments register")[1].split("## Provenance")[0]
    assert register.count("\n| ") == 5  # header, separator, three annotations
    checks = json.loads((tree / str(_by_rel(tree)[REL_PDF]["checks_path"])).read_text(encoding="utf-8"))
    assert all(c["pass"] for c in checks["checks"] if c["required"])
    assert "## Provenance" in text and "### Conversion checks" in text and "`words.jsonl`" in text


def test_pptx_markdown_layout(tree: Path) -> None:
    text = _md(tree, REL_PPTX)
    assert _h2(text) == ["## Document", "## Slide 1", "## Slide 2", "## Slide 3", "## Comments register", "## Provenance"]
    assert "<!-- method: python-pptx -->" in text and text.count("### Pictures") == 3
    slide2 = text.split("## Slide 2\n")[1].split("## Slide 3\n")[0]
    assert "Table S2-1: slide 2 table shape" in slide2 and "| Pharmacy | 32.5 | 45 |" in slide2
    assert "| Bob Modern |" in slide2 and "modern reply" in slide2
    slide3 = text.split("## Slide 3\n")[1].split("## Comments register")[0]
    assert "pictures/3_" in slide3 and "64 x 40 px" in slide3
    register = text.split("## Comments register")[1].split("## Provenance")[0]
    assert register.count("\n| ") == 5 and "| Alice Reviewer |" in register
    assert _front_matter(text)["doc_class"] == "Booklet" and _front_matter(text)["pages_or_slides_or_sheets"] == 3


def test_xlsx_email_and_image_markdown(tree: Path) -> None:
    xlsx = _md(tree, REL_XLSX)
    assert _h2(xlsx) == ["## Document", "## Sheet Loads", "## Sheet Hidden Data (2)", "## Comments register", "## Provenance"]
    assert "Table Loads: sheet range A1:D5" in xlsx and "<!-- method: openpyxl -->" in xlsx
    assert _front_matter(xlsx)["pages_or_slides_or_sheets"] == 2
    eml = _md(tree, REL_EML)
    assert _h2(eml) == ["## Document", "## Message", "## Comments register", "## Provenance"]
    assert "<!-- method: email-stdlib -->" in eml and "| subject | Flow 1,200 L/s |" in eml
    front = _front_matter(eml)
    assert front["drive_modified"] == "2026-03-02T02:00:00Z" and front["stage"] == "AH sign off with comments"
    png = _md(tree, REL_PNG)
    assert _h2(png) == ["## Document", "## Image", "## Comments register", "## Provenance"]
    assert "<!-- method: pillow-exif -->" in png and _front_matter(png)["grade"] == "pending"
    assert "| width | 40 |" in png and "preview.jpg" in png


# --------------------------------------------------------------------------- #
# Resumability, force, filters, dry run
# --------------------------------------------------------------------------- #
def test_second_run_is_resumable_and_force_reconverts(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    before = len(_rows(tree))
    assert convert.main(["run", "--root", str(tree), "--workers", "1"]) == 0
    assert len(_rows(tree)) == before and "nothing to do" in capsys.readouterr().out
    assert convert.main(["run", "--root", str(tree), "--workers", "1", "--force", "--paths", REL_COLL_PDF]) == 0
    rows = _rows(tree)
    assert len(rows) == before + 1 and rows[-1]["rel_path"] == REL_COLL_PDF and rows[-1]["status"] == "converted"
    # a missing Markdown file invalidates the done state for that file only
    (tree / str(rows[-1]["md_path"])).unlink()
    assert convert.main(["run", "--root", str(tree), "--workers", "1"]) == 0
    assert len(_rows(tree)) == before + 2 and (tree / str(rows[-1]["md_path"])).is_file()


def test_dry_run_and_filters(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert convert.main(["run", "--root", str(tree), "--force", "--dry-run", "--only-class", "RDS"]) == 0
    out = capsys.readouterr().out
    assert REL_PDF in out and REL_DUP in out and REL_PPTX not in out and "2 file(s) would run" in out
    assert convert.main(["run", "--root", str(tree), "--force", "--dry-run", "--dept", "30", "--roots", "DD Sign-OFF",
                         "--limit", "2"]) == 0
    out = capsys.readouterr().out
    assert "2 file(s) would run" in out and "Basis/" not in out
    assert convert.main(["run", "--root", str(tree), "--force", "--dry-run", "--roots", "Template"]) == 0
    assert "0 file(s) would run" in capsys.readouterr().out
    assert convert.main(["run", "--root", str(tree), "--force", "--dry-run", "--paths",
                         str(tree / "data" / "source" / REL_EML)]) == 0
    assert REL_EML in capsys.readouterr().out


def test_phase1_checks_pass_on_converted_scope(tree: Path) -> None:
    assert checks.main(["phase1", "--scope", "converted", "--quiet"]) == 0
    report = (tree / "out" / "qa" / "check_phase1.md").read_text(encoding="utf-8")
    assert "Result: **PASS**" in report and "| P1-05 |" in report and "Scope: converted" in report
    ctx = checks.Context(scope="all")
    results = {r.check_id: r for r in checks.run_phase(1, ctx)}
    assert all(r.status == checks.PASS for r in results.values()), results
    assert any("A " in e or "B " in e for e in results["P1-05"].evidence)


def test_phase1_checks_fail_on_damaged_output(tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = tmp_path / "proj"
    shutil.copytree(tree, copy)
    monkeypatch.setenv("AIGH_ROOT", str(copy))
    rows = {str(r["rel_path"]): r for r in read_jsonl(copy / "state" / "convert_ledger.jsonl")}
    (copy / str(rows[REL_PPTX]["md_path"])).unlink()
    checks_path = copy / str(rows[REL_XLSX]["checks_path"])
    data = json.loads(checks_path.read_text(encoding="utf-8"))
    data["numeric_recall"] = 0.5
    data["misses"] = ["1250"]
    data["tables"]["match"] = False
    checks_path.write_text(json.dumps(data), encoding="utf-8")
    results = {r.check_id: r for r in checks.run_phase(1, checks.Context(scope="converted"))}
    assert results["P1-01"].status == checks.FAIL and REL_PPTX in " ".join(results["P1-01"].evidence)
    assert results["P1-02"].status == checks.FAIL and "1250" in " ".join(results["P1-02"].evidence)
    assert results["P1-03"].status == checks.FAIL and results["P1-04"].status == checks.PASS
    assert results["P1-05"].status == checks.FAIL  # the deck's front matter is gone
    empty = tmp_path / "empty"
    build_tree(empty)
    monkeypatch.setenv("AIGH_ROOT", str(empty))
    statuses = {r.check_id: r.status for r in checks.run_phase(1, checks.Context(scope="converted"))}
    assert set(statuses.values()) == {checks.SKIP}
    statuses = {r.check_id: r.status for r in checks.run_phase(1, checks.Context(scope="all"))}
    assert statuses["P1-01"] == checks.FAIL


def test_phase0_registry_untouched() -> None:
    assert [c.check_id for c in checks.PHASE_CHECKS[0]] == [f"P0-{n:02d}" for n in range(1, 13)]
    assert [c.check_id for c in checks.PHASE_CHECKS[1]] == [f"P1-{n:02d}" for n in range(1, 6)]


# --------------------------------------------------------------------------- #
# Planning helpers, timeout, errors, CLI
# --------------------------------------------------------------------------- #
def test_md_rel_path_and_collisions() -> None:
    rows = [{"rel_path": "Basis/a.pdf"}, {"rel_path": "Basis/a.xlsx"}, {"rel_path": "Basis/b.pdf"},
            {"rel_path": "Other/a.pdf"}]
    collisions = convert.colliding_stems(rows)
    assert collisions == {"Basis/a"}
    assert convert.md_rel_path("Basis/a.pdf", collisions) == "Basis/a.pdf.md"
    assert convert.md_rel_path("Basis/b.pdf", collisions) == "Basis/b.md"
    assert convert.md_rel_path("Other/a.pdf", collisions) == "Other/a.md"


def test_load_source_rows_keeps_latest_per_path_in_first_order(tmp_path: Path) -> None:
    state = tmp_path / "state"
    pull = state / "pull_ledger.jsonl"
    append_jsonl(pull, {"rel_path": "Basis/a.pdf", "sha256": "1" * 64})
    append_jsonl(pull, {"rel_path": "Basis/b.pdf", "sha256": "2" * 64})
    append_jsonl(pull, {"rel_path": "Basis/a.pdf", "sha256": "3" * 64})
    append_jsonl(pull, {"rel_path": "", "sha256": "4" * 64})
    rows = convert.load_source_rows(state)
    assert [(r["rel_path"], r["sha256"][0]) for r in rows] == [("Basis/a.pdf", "3"), ("Basis/b.pdf", "2")]
    assert convert.first_by_sha(rows)["2" * 64]["rel_path"] == "Basis/b.pdf"


def _task(tree: Path, rel: str, timeout: int = 600) -> convert.Task:
    row = next(r for r in convert.load_source_rows(tree / "state") if r["rel_path"] == rel)
    layout = convert.Layout.from_root(tree)
    return convert.Task(sha256=row["sha256"], rel_path=rel, local_path=str(tree / "data" / "source" / rel),
                        bytes=int(row["bytes"]), drive_id=None, drive_modified=None, kind="pdf", doc_class="RDS",
                        md_rel="x.md", root=str(tree), markdown_dir=str(layout.markdown), sidecar_dir=str(layout.sidecar / rel),
                        state_dir=str(layout.state), timeout_s=timeout, force=True)


def test_per_file_timeout_becomes_error_row(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(_task: convert.Task) -> Dict[str, object]:
        time.sleep(5)
        return {}

    monkeypatch.setattr(convert, "convert_file", slow)
    task = _task(tree, REL_COLL_PDF, timeout=1)
    started = time.monotonic()
    row = convert.convert_task(convert.asdict(task))
    assert time.monotonic() - started < 4
    assert row["status"] == "error" and "timeout after 1 s" in row["error"]


def test_missing_file_and_worker_exception_are_rows(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = _task(tree, REL_COLL_PDF)
    task.local_path = str(tree / "data" / "source" / "missing.pdf")
    row = convert.convert_task(convert.asdict(task))
    assert row["status"] == "error" and "local file missing" in row["error"]

    def boom(_task: convert.Task) -> Dict[str, object]:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(convert, "convert_file", boom)
    row = convert.convert_task(convert.asdict(_task(tree, REL_COLL_PDF)))
    assert row["status"] == "error" and "RuntimeError: synthetic" in row["error"] and "traceback" in row


def test_markitdown_failure_is_recorded_not_raised(tmp_path: Path) -> None:
    result = convert.run_markitdown(tmp_path / "missing.pptx")
    assert result.text is None and "FileNotFoundError" in str(result.error) and result.version
    bad = tmp_path / "bad.pptx"
    bad.write_bytes(b"not a deck")
    assert convert.run_markitdown(bad).error is None  # MarkItDown falls back to plain text; the sidecar reports it


def test_run_command_exit_code_on_error(tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def failing(task_dict: Dict[str, object]) -> Dict[str, object]:
        return convert._result(str(task_dict["sha256"]), str(task_dict["rel_path"]), "error", error="synthetic")  # noqa: SLF001

    monkeypatch.setattr(convert, "convert_task", failing)
    before = len(_rows(tree))
    assert convert.main(["run", "--root", str(tree), "--workers", "1", "--force", "--paths", REL_COLL_PDF,
                         "--no-progress"]) == 1
    assert len(_rows(tree)) == before + 1 and _rows(tree)[-1]["error"] == "synthetic"
    assert "error 1" in capsys.readouterr().out
    # the error row is not resumable: the next run tries the file again
    monkeypatch.undo()
    assert convert.main(["run", "--root", str(tree), "--workers", "1", "--paths", REL_COLL_PDF]) == 0
    assert _rows(tree)[-1]["status"] == "converted"


def test_cli_subprocess_dry_run(tree: Path) -> None:
    env = dict(os.environ, AIGH_ROOT=str(tree))
    proc = subprocess.run([sys.executable, "-m", "src.convert", "run", "--dry-run", "--force"], cwd=str(PROJECT_ROOT),
                          env=env, capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr
    assert "would run" in proc.stdout and REL_PDF in proc.stdout
    help_text = subprocess.run([sys.executable, "-m", "src.convert", "run", "--help"], cwd=str(PROJECT_ROOT),
                               capture_output=True, text=True, check=False).stdout
    assert "--only-class" in help_text and "--timeout" in help_text


def test_default_workers_and_summary() -> None:
    assert convert.default_workers() >= 1
    rows = [{"status": "converted", "grade": "A"}, {"status": "converted", "grade": "B"}, {"status": "skipped", "grade": None}]
    assert convert.summarise(rows) == "convert: converted 2, skipped 1; grades A 1, B 1"
    assert convert.summarise([]) == "convert: nothing to do"
    ns = argparse.Namespace(paths=None, roots=None, dept=None, only_class="RDS", limit=None, force=False, timeout=1,
                            scratch=None)
    assert convert._matches({"rel_path": "Basis/x_RDS.pdf"}, ns, convert.Layout.from_root()) is True  # noqa: SLF001
    assert convert._matches({"rel_path": "Basis/x.pdf"}, ns, convert.Layout.from_root()) is False  # noqa: SLF001
