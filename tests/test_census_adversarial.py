"""Adversarial tests for src.census written by an independent reviewer.

Tests that were marked ``xfail(strict=True)`` documented defects found during
review (rotated-page coverage, the uninterruptible path budget, the hash-less
ledger entry, the ``--limit`` log line); the defects are fixed and the markers
removed, so every test here must pass.
"""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src import census
from src.common import read_csv_dicts, sha256_file

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "census_adversarial"


def _load_builder() -> Any:
    spec = importlib.util.spec_from_file_location("census_adv_fixtures", FIXTURE_DIR / "build_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def adv_fixtures(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Path]:
    return _load_builder().build_all(tmp_path_factory.mktemp("census_adv"))


def _ledger_entry(root: Path, rel_path: str, src: Path, drive_id: str = "id", sha: str | None = None) -> Dict[str, Any]:
    """Copy ``src`` under ``data/source/<rel_path>`` and return a ledger record for it."""
    local = root / "data" / "source" / rel_path
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(src.read_bytes())
    return {"drive_id": drive_id, "rel_path": rel_path, "local_path": local.relative_to(root).as_posix(),
            "sha256": sha if sha is not None else sha256_file(local)}


def _write_ledger(root: Path, records: List[Dict[str, Any]]) -> Path:
    state = root / "state"
    state.mkdir(exist_ok=True)
    ledger = state / "pull_ledger.jsonl"
    ledger.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    return ledger


def _run(root: Path, *extra: str, workers: str = "1") -> int:
    return census.main(["--registers-dir", str(root / "out" / "registers"), "--logs-dir", str(root / "logs"), "run",
                        "--ledger", str(root / "state" / "pull_ledger.jsonl"),
                        "--progress", str(root / "state" / "progress.json"),
                        "--project-root", str(root), "--workers", workers, *extra])


def _files(root: Path) -> List[Dict[str, str]]:
    return read_csv_dicts(root / "out" / "registers" / "file_census.csv")


def _pages(root: Path) -> List[Dict[str, str]]:
    return read_csv_dicts(root / "out" / "registers" / "page_census.csv")


# --------------------------------------------------------------------------- #
# Empty and degenerate inputs
# --------------------------------------------------------------------------- #
def test_empty_ledger_is_a_clean_no_op(tmp_path: Path) -> None:
    _write_ledger(tmp_path, [])
    assert _run(tmp_path) == 0
    assert not (tmp_path / "out" / "registers" / "file_census.csv").exists()
    assert "0 candidate files" in (tmp_path / "logs" / "census.log").read_text(encoding="utf-8")
    # summary on an empty census is a reported failure, not a crash
    assert census.main(["--registers-dir", str(tmp_path / "out" / "registers"), "--logs-dir", str(tmp_path / "logs"),
                        "summary"]) == 1


def test_missing_ledger_file_is_a_clean_no_op(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    assert _run(tmp_path) == 0


def test_zero_area_geometry_never_divides_by_zero(adv_fixtures: Dict[str, Path]) -> None:
    assert census.coverage_pct([(0, 0, 10, 10)], 0, 0) == 0.0
    assert census.coverage_pct([(5, 5, 5, 9)], 10, 10) == 0.0  # degenerate (zero-width) box is ignored
    assert census.coverage_pct([(10, 10, 0, 0)], 10, 10) == 100.0  # reversed corners are normalised
    rows = census.census_pdf(adv_fixtures["tiny"])
    assert len(rows) == 1
    assert (rows[0]["width_pt"], rows[0]["height_pt"]) == (3.0, 3.0)
    assert rows[0]["image_coverage_pct"] == 0.0 and rows[0]["page_class"] == "mixed"


# --------------------------------------------------------------------------- #
# Paths: unicode, spaces, uppercase extensions, absolute local paths
# --------------------------------------------------------------------------- #
def test_unicode_and_spaces_in_paths_round_trip(tmp_path: Path, adv_fixtures: Dict[str, Path]) -> None:
    rel = "DD Sign-OFF/26 - Diagnostic Imaging/AH sign off with comments/02 Récépissé/Plan – étage 3 (rév A).PDF"
    records = [_ledger_entry(tmp_path, rel, adv_fixtures["text_a"], "u1")]
    _write_ledger(tmp_path, records)
    assert _run(tmp_path) == 0
    files = _files(tmp_path)
    assert len(files) == 1 and files[0]["rel_path"] == rel and files[0]["kind"] == "pdf"  # .PDF accepted as pdf
    assert files[0]["error"] == "" and files[0]["pages"] == "2"
    assert all(r["rel_path"] == rel for r in _pages(tmp_path))
    assert rel in (tmp_path / "logs" / "census.log").read_text(encoding="utf-8")
    summary_rc = census.main(["--registers-dir", str(tmp_path / "out" / "registers"),
                              "--logs-dir", str(tmp_path / "logs"), "summary"])
    assert summary_rc == 0
    summary = {r["root"]: r for r in read_csv_dicts(tmp_path / "out" / "registers" / "census_summary.csv")}
    assert summary["DD Sign-OFF"]["pages"] == "2" and summary["DD Sign-OFF"]["text"] == "2"


def test_absolute_local_path_in_ledger_is_honoured(tmp_path: Path, adv_fixtures: Dict[str, Path]) -> None:
    rec = _ledger_entry(tmp_path, "Basis/App D/abs.pdf", adv_fixtures["text_a"], "a1")
    rec["local_path"] = str(tmp_path / rec["local_path"])  # absolute instead of project-relative
    _write_ledger(tmp_path, [rec])
    assert _run(tmp_path) == 0
    assert _files(tmp_path)[0]["error"] == ""


def test_paths_mode_rejects_non_census_extension_without_writing(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    stray = tmp_path / "soa.xlsx"
    stray.write_bytes(b"not a workbook")
    assert _run(tmp_path, "--paths", str(stray)) == 1
    assert not (tmp_path / "out" / "registers" / "file_census.csv").exists()


# --------------------------------------------------------------------------- #
# Duplicates, changed files, resumability
# --------------------------------------------------------------------------- #
def test_duplicate_drive_ids_and_identical_hashes_are_measured_once(tmp_path: Path, adv_fixtures: Dict[str, Path]) -> None:
    first = _ledger_entry(tmp_path, "Basis/one.pdf", adv_fixtures["text_a"], "dup")
    second = dict(first)  # the very same ledger line appended twice (a re-run of the pull)
    third = _ledger_entry(tmp_path, "Template/copy of one.pdf", adv_fixtures["text_a"], "dup")  # same id, other path
    _write_ledger(tmp_path, [first, second, third])
    assert _run(tmp_path) == 0
    files = _files(tmp_path)
    assert [f["rel_path"] for f in files] == ["Basis/one.pdf", "Template/copy of one.pdf"]  # the repeated line adds nothing
    assert files[1]["note"] == "duplicate of Basis/one.pdf" and files[1]["pages"] == files[0]["pages"] == "2"
    assert len(_pages(tmp_path)) == 2  # measured once


def test_changed_file_with_same_rel_path_gets_a_second_census(tmp_path: Path, adv_fixtures: Dict[str, Path]) -> None:
    """A re-fetched file (changed fileSize / sha) must be censused again; the old rows stay keyed by sha."""
    rel = "DD Sign-OFF/30 - Pharmacy/Submitted/03_SOA/plan.pdf"
    old = _ledger_entry(tmp_path, rel, adv_fixtures["text_a"], "c1")
    _write_ledger(tmp_path, [old])
    assert _run(tmp_path) == 0
    new = _ledger_entry(tmp_path, rel, adv_fixtures["text_b"], "c1")  # overwrites the local copy, new hash
    assert new["sha256"] != old["sha256"]
    _write_ledger(tmp_path, [old, new])
    assert _run(tmp_path) == 0
    files = _files(tmp_path)
    assert [f["sha256"] for f in files] == [old["sha256"], new["sha256"]]
    assert len(_pages(tmp_path)) == 4
    assert _run(tmp_path) == 0 and len(_files(tmp_path)) == 2  # third run adds nothing


def test_error_rows_are_sticky_and_never_retried(tmp_path: Path, adv_fixtures: Dict[str, Path]) -> None:
    """Spec-literal behaviour, recorded here so a future retry switch is tested: a missing file that later
    arrives is still skipped because its sha already has a (failed) file-census row."""
    rec = _ledger_entry(tmp_path, "Basis/late.pdf", adv_fixtures["text_a"], "l1")
    local = tmp_path / rec["local_path"]
    local.unlink()
    _write_ledger(tmp_path, [rec])
    assert _run(tmp_path) == 1
    assert _files(tmp_path)[0]["error"].startswith("FileNotFoundError")
    local.write_bytes(adv_fixtures["text_a"].read_bytes())  # the file is pulled afterwards
    assert _run(tmp_path) == 0
    assert len(_files(tmp_path)) == 1 and _files(tmp_path)[0]["pages"] == "0"


def test_entry_without_sha_and_missing_file_is_idempotent(tmp_path: Path) -> None:
    _write_ledger(tmp_path, [{"drive_id": "n1", "rel_path": "Basis/nohash.pdf",
                              "local_path": "data/source/Basis/nohash.pdf"}])
    assert _run(tmp_path) == 1
    assert len(_files(tmp_path)) == 1
    assert _run(tmp_path) == 1
    assert len(_files(tmp_path)) == 1, "second run must not append another error row"


def test_corrupt_inputs_are_recorded_and_the_run_continues(tmp_path: Path, adv_fixtures: Dict[str, Path]) -> None:
    bad_pdf = tmp_path / "bad.pdf"
    bad_pdf.write_bytes(b"%PDF-1.4\nnot really a pdf")
    bad_pptx = tmp_path / "bad.pptx"
    bad_pptx.write_bytes(b"PK\x03\x04garbage")
    empty_pdf = tmp_path / "empty.pdf"
    empty_pdf.write_bytes(b"")
    records = [_ledger_entry(tmp_path, "Basis/bad.pdf", bad_pdf, "b1"),
               _ledger_entry(tmp_path, "Basis/good.pdf", adv_fixtures["text_a"], "g1"),
               _ledger_entry(tmp_path, "Basis/bad.pptx", bad_pptx, "b2"),
               _ledger_entry(tmp_path, "Basis/empty.pdf", empty_pdf, "b3")]
    _write_ledger(tmp_path, records)
    assert _run(tmp_path) == 1
    files = {f["rel_path"]: f for f in _files(tmp_path)}
    assert set(files) == {"Basis/bad.pdf", "Basis/good.pdf", "Basis/bad.pptx", "Basis/empty.pdf"}
    assert files["Basis/good.pdf"]["error"] == "" and files["Basis/good.pdf"]["pages"] == "2"
    for rel in ("Basis/bad.pdf", "Basis/bad.pptx", "Basis/empty.pdf"):
        assert files[rel]["error"] and files[rel]["pages"] == "0"
    assert len(_pages(tmp_path)) == 2
    # the summary still works when only some files have pages
    rc = census.main(["--registers-dir", str(tmp_path / "out" / "registers"), "--logs-dir", str(tmp_path / "logs"),
                      "summary"])
    assert rc == 0
    total = {r["root"]: r for r in read_csv_dicts(tmp_path / "out" / "registers" / "census_summary.csv")}["TOTAL"]
    assert (total["files"], total["errors"], total["pages"]) == ("4", "3", "2")


def test_parallel_workers_keep_ledger_order(tmp_path: Path, adv_fixtures: Dict[str, Path]) -> None:
    records = [_ledger_entry(tmp_path, f"Basis/{i:02d}.pdf", adv_fixtures["text_a" if i % 2 else "text_b"], f"o{i}")
               for i in range(2)]
    records += [_ledger_entry(tmp_path, "Template/deck.pptx", adv_fixtures["plain_pptx"], "o9")]
    _write_ledger(tmp_path, records)
    assert _run(tmp_path, workers="3") == 0
    assert [f["rel_path"] for f in _files(tmp_path)] == [r["rel_path"] for r in records]
    assert [(p["rel_path"], p["page"]) for p in _pages(tmp_path)] == \
        [("Basis/00.pdf", "1"), ("Basis/00.pdf", "2"), ("Basis/01.pdf", "1"), ("Basis/01.pdf", "2"),
         ("Template/deck.pptx", "1"), ("Template/deck.pptx", "2"), ("Template/deck.pptx", "3")]


# --------------------------------------------------------------------------- #
# PPTX without any comments part
# --------------------------------------------------------------------------- #
def test_pptx_without_comments_part(adv_fixtures: Dict[str, Path]) -> None:
    per_slide, total = census.pptx_comment_counts(adv_fixtures["plain_pptx"])
    assert (per_slide, total) == ({}, 0)
    rows = census.census_pptx(adv_fixtures["plain_pptx"])
    assert [r["page"] for r in rows] == [1, 2, 3]
    for row in rows:
        assert (row["annotation_count"], row["annotation_types"], row["layer_count"], row["rotation"]) == (0, "", 0, 0)
        assert (row["width_pt"], row["height_pt"]) == (720.0, 540.0)
    empty, drawn, placeholders = rows
    assert empty["text_chars"] == 0 and empty["image_count"] == 0 and empty["page_class"] == "mixed"
    assert empty["notes"] == "tables=0;charts=0;notes_chars=0"
    assert "charts=1" in drawn["notes"] and drawn["vector_paths"] == 1  # the connector is a drawn shape
    assert placeholders["text_chars"] == 0 and placeholders["page_class"] == "mixed"


# --------------------------------------------------------------------------- #
# Geometry and time-budget defects found in review
# --------------------------------------------------------------------------- #
def test_rotated_full_bleed_scan_is_fully_covered(adv_fixtures: Dict[str, Path]) -> None:
    rows = census.census_pdf(adv_fixtures["rotated"])
    assert rows[0]["rotation"] == 90 and rows[0]["image_count"] == 1
    assert rows[0]["image_coverage_pct"] >= 99.0


def test_rotated_scan_coverage_documented_value(adv_fixtures: Dict[str, Path]) -> None:
    """Before the fix this pinned 595^2 / (842*595) = 70.67 %; the bboxes are now rotated into page.rect space."""
    rows = census.census_pdf(adv_fixtures["rotated"])
    assert rows[0]["image_coverage_pct"] == pytest.approx(100.0, abs=0.05)
    assert (rows[0]["width_pt"], rows[0]["height_pt"]) == (842.0, 595.0)
    assert rows[0]["page_class"] == "raster_or_scan"


class _OneLongCCall:
    """A page whose path extraction is a single C-level call (like pymupdf's get_cdrawings)."""

    def __init__(self, n: int) -> None:
        self.n = n

    def get_cdrawings(self) -> List[Any]:
        sum(range(self.n))  # one C call; SIGALRM handlers only run when control returns to Python
        return []


def test_page_budget_interrupts_a_single_c_call() -> None:
    page = _OneLongCCall(40_000_000)
    started = time.monotonic()
    page.get_cdrawings()
    full = time.monotonic() - started
    started = time.monotonic()
    result = census._count_paths(page, full / 10)
    elapsed = time.monotonic() - started
    assert result == (-1, "path_count_timeout")
    assert elapsed < full / 2, f"budget {full / 10:.2f}s did not stop a {full:.2f}s call (took {elapsed:.2f}s)"


def test_budget_state_is_clean_after_timeout() -> None:
    census._count_paths(_OneLongCCall(2_000_000), 0.001)
    assert census._DEADLINES == []
    import signal
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_limit_log_line_counts_correctly(tmp_path: Path, adv_fixtures: Dict[str, Path]) -> None:
    records = [_ledger_entry(tmp_path, f"Basis/{i}.pdf", adv_fixtures["text_a" if i else "text_b"], f"l{i}")
               for i in range(2)]
    _write_ledger(tmp_path, records)
    assert _run(tmp_path, "--limit", "1") == 0
    log = (tmp_path / "logs" / "census.log").read_text(encoding="utf-8")
    assert "2 candidate files, 0 already censused, 1 to do" in log
