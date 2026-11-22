"""Tests for src.census using synthetic fixtures built by tests/fixtures/census/build_fixtures.py."""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src import census
from src.common import read_csv_dicts, sha256_file
from src.state import Progress

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "census"
PDF_REL = "DD Sign-OFF/30 - Pharmacy/Submitted/01_Booklet/Pharmacy_DD_Booklet.pdf"
PPTX_REL = "Template/AHU Zoning Deck.pptx"


def _load_builder() -> Any:
    """Import the fixture builder module from its file path."""
    spec = importlib.util.spec_from_file_location("build_fixtures", FIXTURE_DIR / "build_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Path]:
    """Freshly generated PDF and PPTX fixtures (also refreshes the committed copies when missing)."""
    builder = _load_builder()
    if not (FIXTURE_DIR / builder.PDF_NAME).exists() or not (FIXTURE_DIR / builder.PPTX_NAME).exists():
        builder.build_all(FIXTURE_DIR)
    return builder.build_all(tmp_path_factory.mktemp("census_fixtures"))


@pytest.fixture()
def workspace(tmp_path: Path, fixtures: Dict[str, Path]) -> Dict[str, Any]:
    """A throw-away project tree with a pull ledger naming the two fixtures (plus a duplicate and an xlsx)."""
    root = tmp_path
    state = root / "state"
    state.mkdir()
    local_pdf = root / "data" / "source" / PDF_REL
    local_pptx = root / "data" / "source" / PPTX_REL
    for src, dst in ((fixtures["pdf"], local_pdf), (fixtures["pptx"], local_pptx)):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
    pdf_sha, pptx_sha = sha256_file(local_pdf), sha256_file(local_pptx)
    records = [
        {"drive_id": "p1", "rel_path": PDF_REL, "local_path": local_pdf.relative_to(root).as_posix(), "sha256": pdf_sha},
        {"drive_id": "p2", "rel_path": "DD Sign-OFF/29 - Lab/Submitted/copy.pdf",
         "local_path": local_pdf.relative_to(root).as_posix(), "sha256": pdf_sha},  # hash-identical duplicate
        {"drive_id": "k1", "rel_path": PPTX_REL, "local_path": local_pptx.relative_to(root).as_posix(), "sha256": pptx_sha},
        {"drive_id": "x1", "rel_path": "Basis/App D/soa.xlsx", "local_path": "data/source/Basis/App D/soa.xlsx",
         "sha256": "0" * 64},  # not a census kind
    ]
    ledger = state / "pull_ledger.jsonl"
    ledger.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return {"root": root, "state": state, "ledger": ledger, "registers": root / "out" / "registers",
            "logs": root / "logs", "pdf_sha": pdf_sha, "pptx_sha": pptx_sha}


def _run(ws: Dict[str, Any], *extra: str) -> int:
    """Run the census over the workspace ledger with a single worker."""
    return census.main(["--registers-dir", str(ws["registers"]), "--logs-dir", str(ws["logs"]), "run",
                        "--ledger", str(ws["ledger"]), "--progress", str(ws["state"] / "progress.json"),
                        "--project-root", str(ws["root"]), "--workers", "1", *extra])


def _pages(ws: Dict[str, Any]) -> List[Dict[str, str]]:
    return read_csv_dicts(ws["registers"] / "page_census.csv")


def _files(ws: Dict[str, Any]) -> List[Dict[str, str]]:
    return read_csv_dicts(ws["registers"] / "file_census.csv")


# --------------------------------------------------------------------------- #
# Columns and per-page values
# --------------------------------------------------------------------------- #
def test_run_writes_both_registers_with_exact_columns(workspace: Dict[str, Any]) -> None:
    assert _run(workspace) == 0
    pages, files = _pages(workspace), _files(workspace)
    assert list(pages[0].keys()) == list(census.PAGE_COLUMNS)
    assert list(files[0].keys()) == list(census.FILE_COLUMNS)
    assert len(files) == 2  # the duplicate shares a sha256 and the xlsx is ignored
    assert len(pages) == 4 + 3
    assert (workspace["logs"] / "census.log").read_text().count("done ") == 2


def test_pdf_pages(workspace: Dict[str, Any]) -> None:
    assert _run(workspace) == 0
    rows = {int(r["page"]): r for r in _pages(workspace) if r["kind"] == "pdf"}
    assert set(rows) == {1, 2, 3, 4}
    for row in rows.values():
        assert row["rel_path"] == PDF_REL and row["sha256"] == workspace["pdf_sha"]
        assert (row["width_pt"], row["height_pt"], row["rotation"], row["layer_count"]) == ("595.0", "842.0", "0", "1")
    text, vector, raster, annotated = rows[1], rows[2], rows[3], rows[4]
    assert int(text["text_chars"]) >= 200 and int(text["words"]) > 100 and text["page_class"] == "text"
    assert text["vector_paths"] == "0" and text["image_count"] == "0" and text["image_coverage_pct"] == "0.0"
    assert vector["vector_paths"] == "1000" and vector["page_class"] == "vector_drawing"
    assert raster["image_count"] == "1" and float(raster["image_coverage_pct"]) >= 99.0
    assert raster["text_chars"] == "0" and raster["page_class"] == "raster_or_scan"
    assert annotated["annotation_count"] == "3"
    assert annotated["annotation_types"] == "FreeText=1;Square=1;Text=1"
    assert annotated["page_class"] == "mixed"


def test_pptx_slides(workspace: Dict[str, Any]) -> None:
    assert _run(workspace) == 0
    rows = {int(r["page"]): r for r in _pages(workspace) if r["kind"] == "pptx"}
    assert set(rows) == {1, 2, 3}
    for row in rows.values():
        assert row["rel_path"] == PPTX_REL and row["sha256"] == workspace["pptx_sha"]
        assert (row["width_pt"], row["height_pt"], row["rotation"], row["layer_count"]) == ("720.0", "540.0", "0", "0")
    text, picture, table = rows[1], rows[2], rows[3]
    assert int(text["text_chars"]) >= 200 and text["page_class"] == "text"
    assert text["annotation_count"] == "2" and text["annotation_types"] == "comment=2"  # classic comments part
    assert "notes_chars=33" in text["notes"]
    assert picture["image_count"] == "1" and float(picture["image_coverage_pct"]) >= 99.0
    assert picture["page_class"] == "raster_or_scan"
    assert picture["annotation_count"] == "1"  # modern comments part
    assert "tables=1" in table["notes"] and table["page_class"] == "mixed"
    assert int(table["text_chars"]) >= len("R0C0") * 9  # table cell text is counted


def test_file_rows_aggregate_pages(workspace: Dict[str, Any]) -> None:
    assert _run(workspace) == 0
    files = {r["kind"]: r for r in _files(workspace)}
    pdf, pptx = files["pdf"], files["pptx"]
    assert (pdf["pages"], pdf["text_pages"], pdf["vector_pages"], pdf["raster_pages"], pdf["mixed_pages"]) == \
        ("4", "1", "1", "1", "1")
    assert pdf["total_annotations"] == "3" and pdf["error"] == "" and float(pdf["seconds"]) >= 0
    assert (pptx["pages"], pptx["text_pages"], pptx["raster_pages"], pptx["mixed_pages"]) == ("3", "1", "1", "1")
    assert pptx["total_annotations"] == "3"
    progress = Progress(workspace["state"] / "progress.json")
    assert progress.status(f"census:{PDF_REL}") == "done"
    assert progress.get(f"census:{PDF_REL}")["hash"] == workspace["pdf_sha"]


# --------------------------------------------------------------------------- #
# Resumability, limit, errors, ad hoc paths
# --------------------------------------------------------------------------- #
def test_second_run_adds_nothing(workspace: Dict[str, Any]) -> None:
    assert _run(workspace) == 0
    before = ((workspace["registers"] / "page_census.csv").read_bytes(),
              (workspace["registers"] / "file_census.csv").read_bytes())
    assert _run(workspace) == 0
    after = ((workspace["registers"] / "page_census.csv").read_bytes(),
             (workspace["registers"] / "file_census.csv").read_bytes())
    assert before == after
    assert "2 already censused, 0 to do" in (workspace["logs"] / "census.log").read_text()


def test_limit_processes_one_file_then_resumes(workspace: Dict[str, Any]) -> None:
    assert _run(workspace, "--limit", "1") == 0
    assert len(_files(workspace)) == 1
    assert _run(workspace) == 0
    assert len(_files(workspace)) == 2
    assert len(_pages(workspace)) == 7


def test_orphan_page_rows_are_repaired(workspace: Dict[str, Any]) -> None:
    assert _run(workspace) == 0
    file_csv = workspace["registers"] / "file_census.csv"
    rows = [r for r in _files(workspace) if r["kind"] != "pptx"]  # simulate a crash before the pptx file row
    census.write_csv_dicts(file_csv, rows, census.FILE_COLUMNS)
    assert _run(workspace) == 0
    pages = _pages(workspace)
    assert len(pages) == 7 and len([r for r in pages if r["kind"] == "pptx"]) == 3  # not duplicated
    assert len(_files(workspace)) == 2


def test_missing_file_is_recorded_as_error_and_run_continues(workspace: Dict[str, Any]) -> None:
    with workspace["ledger"].open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"drive_id": "m1", "rel_path": "Basis/missing.pdf",
                                 "local_path": "data/source/Basis/missing.pdf", "sha256": "f" * 64}) + "\n")
    assert _run(workspace) == 1
    files = {r["rel_path"]: r for r in _files(workspace)}
    assert len(files) == 3
    assert files["Basis/missing.pdf"]["pages"] == "0" and files["Basis/missing.pdf"]["error"]
    assert Progress(workspace["state"] / "progress.json").status("census:Basis/missing.pdf") == "error"


def test_paths_mode(workspace: Dict[str, Any], fixtures: Dict[str, Path]) -> None:
    assert _run(workspace, "--paths", str(fixtures["pptx"])) == 0
    files = _files(workspace)
    assert len(files) == 1 and files[0]["kind"] == "pptx" and files[0]["pages"] == "3"


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
def test_summary(workspace: Dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(workspace) == 0
    rc = census.main(["--registers-dir", str(workspace["registers"]), "--logs-dir", str(workspace["logs"]), "summary"])
    assert rc == 0
    rows = {r["root"]: r for r in read_csv_dicts(workspace["registers"] / "census_summary.csv")}
    assert list(next(iter(rows.values())).keys()) == list(census.SUMMARY_COLUMNS)
    dd, template, total = rows["DD Sign-OFF"], rows["Template"], rows["TOTAL"]
    assert (dd["files"], dd["pages"], dd["text"], dd["vector_drawing"], dd["raster_or_scan"], dd["mixed"]) == \
        ("1", "4", "1", "1", "1", "1")
    assert (template["files"], template["pages"], template["text"], template["raster_or_scan"], template["mixed"]) == \
        ("1", "3", "1", "1", "1")
    assert (total["files"], total["pages"], total["total_annotations"], total["errors"]) == ("2", "7", "6", "0")
    out = capsys.readouterr().out
    assert "DD Sign-OFF" in out and "TOTAL" in out


def test_summary_without_census_fails(tmp_path: Path) -> None:
    assert census.main(["--registers-dir", str(tmp_path), "--logs-dir", str(tmp_path), "summary"]) == 1


# --------------------------------------------------------------------------- #
# Rules and budgets
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "vector_paths, text_chars, coverage, expected",
    [
        (500, 0, 0.0, "vector_drawing"),
        (-1, 1000, 0.0, "vector_drawing"),
        (0, 49, 60.0, "raster_or_scan"),
        (0, 50, 60.0, "mixed"),
        (499, 200, 29.9, "text"),
        (0, 200, 30.0, "mixed"),
        (0, 199, 0.0, "mixed"),
    ],
)
def test_pdf_class_rules(vector_paths: int, text_chars: int, coverage: float, expected: str) -> None:
    assert census.classify_pdf_page(vector_paths, text_chars, coverage) == expected


@pytest.mark.parametrize(
    "text_chars, coverage, largest, expected",
    [(0, 60.0, 60.0, "raster_or_scan"), (49, 90.0, 59.0, "mixed"), (200, 29.0, 29.0, "text"), (200, 30.0, 30.0, "mixed")],
)
def test_slide_class_rules(text_chars: int, coverage: float, largest: float, expected: str) -> None:
    assert census.classify_slide(text_chars, coverage, largest) == expected


def test_coverage_is_a_clipped_union() -> None:
    boxes = [(0, 0, 50, 100), (25, 0, 75, 100), (90, -50, 200, 10)]  # overlap and spill are not double counted
    assert census.coverage_pct(boxes, 100, 100) == 76.0
    assert census.coverage_pct([], 100, 100) == 0.0


class _SlowPage:
    """Stands in for a pymupdf page whose path extraction never finishes in time."""

    def get_cdrawings(self) -> List[Any]:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            time.sleep(0.01)
        return []


def test_path_count_timeout_records_minus_one() -> None:
    started = time.monotonic()
    assert census._count_paths(_SlowPage(), 0.1) == (-1, "path_count_timeout")
    assert time.monotonic() - started < 1.5


def test_file_budget_wins_over_page_budget() -> None:
    with pytest.raises(census.BudgetExceeded) as info:
        with census.time_budget(0.1, "file"):
            census._count_paths(_SlowPage(), 5.0)
    assert info.value.label == "file"
    assert census._DEADLINES == []


def test_file_timeout_is_recorded_not_raised(tmp_path: Path, fixtures: Dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    def slow_census(path: Path, page_budget_s: float = 20.0) -> List[Dict[str, Any]]:
        time.sleep(2.0)
        return []

    monkeypatch.setattr(census, "census_pdf", slow_census)
    result = census.census_file({"rel_path": "x.pdf", "sha256": "a" * 64, "kind": "pdf",
                                 "local_path": str(fixtures["pdf"]), "file_timeout_s": 0.2})
    assert result["pages"] == [] and result["file"]["error"].startswith("timeout after")
