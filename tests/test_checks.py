"""Tests for src.checks and src.decisions on a synthetic project tree (never the real sources)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

from src import checks, decisions
from src.common import append_jsonl, atomic_write_json, write_csv_dicts
from src.state import Progress

FIXTURES = Path(__file__).parent / "fixtures" / "checks"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 64

LISTING_FILES: List[Dict[str, str]] = [
    {"id": "f1", "title": "30_Pharmacy_SOA.xlsx", "mimeType": "application/x-xlsx", "fileSize": "1000",
     "rel_path": "DD Sign-OFF/30 - Pharmacy/Submitted/03_SOA/30_Pharmacy_SOA.xlsx"},
    {"id": "f2", "title": "30_Pharmacy_RDS.pdf", "mimeType": "application/pdf", "fileSize": "2000",
     "rel_path": "DD Sign-OFF/30 - Pharmacy/Submitted/30_Pharmacy_RDS.pdf"},
    {"id": "f3", "title": "26 - Diagnostic Imaging.zip", "mimeType": "application/zip", "fileSize": "3000",
     "rel_path": "DD Sign-OFF/26 - Diagnostic Imaging.zip"},
    {"id": "f4", "title": "Arch Combined Tender .pdf", "mimeType": "application/pdf", "fileSize": "99000000",
     "rel_path": "Basis/Arch Combined Tender .pdf"},
]
FOLDER = {"id": "d1", "title": "30 - Pharmacy", "mimeType": checks.FOLDER_MIME, "rel_path": "DD Sign-OFF/30 - Pharmacy"}


# --------------------------------------------------------------------------- #
# Synthetic project tree
# --------------------------------------------------------------------------- #
def _listing(stamp: str, taken_at: str) -> Dict[str, object]:
    return {"taken_at": taken_at, "files": list(LISTING_FILES), "folders": [FOLDER]}


def _manifest_rows(pulled_ids: Tuple[str, ...] = ("f1", "f2")) -> List[Dict[str, str]]:
    rows = []
    for entry in LISTING_FILES:
        pulled = entry["id"] in pulled_ids
        rows.append({"drive_id": entry["id"], "rel_path": entry["rel_path"], "root": entry["rel_path"].split("/")[0],
                     "dept_no": "", "dept_name": "", "dept_alias": "", "stage": "", "doc_class": "", "ext": "",
                     "bytes": entry["fileSize"], "drive_modified": "", "drive_md5": "",
                     "sha256": SHA if pulled else "", "pulled": "y" if pulled else "n", "duplicate_of": "", "notes": ""})
    return rows


def _ledger_row(entry: Dict[str, str], size_match: bool = True) -> Dict[str, object]:
    return {"drive_id": entry["id"], "rel_path": entry["rel_path"], "local_path": "data/source/" + entry["rel_path"],
            "bytes": int(entry["fileSize"]), "sha256": SHA, "md5": "b" * 32, "drive_md5": None,
            "size_match": size_match, "md5_match": None, "fetched_at": "2026-11-22T10:00:00Z", "transport": "connector"}


def build_tree(root: Path, pressure: str = "pressure_ok.py") -> Path:
    """Create a complete synthetic project tree under ``root`` that passes every Phase 0 check."""
    for rel in ("config", "src", "state/listings", "out/registers", "out/qa", "data/source"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "project.yaml", root / "config" / "project.yaml")
    shutil.copy(FIXTURES / pressure, root / "src" / "pressure.py")
    for name in checks.BOOTSTRAP_FILES:
        target = root / name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"# synthetic {name}\n", encoding="utf-8")
    atomic_write_json(root / "state" / "progress.json", {"phase": 0, "items": {}})

    listings = root / "state" / "listings"
    atomic_write_json(listings / "listing_20261122T100000Z.json", _listing("20261122T100000Z", "2026-11-22T10:00:00Z"))
    atomic_write_json(listings / "listing_20261122T102000Z.json", _listing("20261122T102000Z", "2026-11-22T10:20:00Z"))
    atomic_write_json(root / "state" / "unstable_departments.json", {"unstable": [], "compared": 2})

    ledger = root / "state" / "pull_ledger.jsonl"
    ledger.unlink(missing_ok=True)
    append_jsonl(ledger, _ledger_row(LISTING_FILES[0]))
    append_jsonl(ledger, _ledger_row(LISTING_FILES[1]))
    plan = [
        {"drive_id": "f1", "rel_path": LISTING_FILES[0]["rel_path"], "bytes": "1000", "mime_type": "x",
         "transport_eligible": "y", "status": "fetched"},
        {"drive_id": "f2", "rel_path": LISTING_FILES[1]["rel_path"], "bytes": "2000", "mime_type": "x",
         "transport_eligible": "y", "status": "fetched"},
        {"drive_id": "f3", "rel_path": LISTING_FILES[2]["rel_path"], "bytes": "3000", "mime_type": "x",
         "transport_eligible": "y", "status": "skipped_zip"},
        {"drive_id": "f4", "rel_path": LISTING_FILES[3]["rel_path"], "bytes": "99000000", "mime_type": "x",
         "transport_eligible": "n", "status": "blocked_connector_limit"},
    ]
    write_csv_dicts(root / "state" / "pull_plan.csv", plan,
                    ["drive_id", "rel_path", "bytes", "mime_type", "transport_eligible", "status"])
    atomic_write_json(root / "state" / "source_lock.json",
                      {"locked": True, "method": "chattr", "enforced": True, "note": "", "at": "2026-11-22T10:00:00Z"})

    registers = root / "out" / "registers"
    write_csv_dicts(registers / "manifest.csv", _manifest_rows(), list(_manifest_rows()[0].keys()))
    write_csv_dicts(registers / "skipped_zip.csv",
                    [{"rel_path": LISTING_FILES[2]["rel_path"], "bytes": "3000", "parent_folder": "DD Sign-OFF",
                      "drive_id": "f3", "nested": "n"}],
                    ["rel_path", "bytes", "parent_folder", "drive_id", "nested"])
    write_csv_dicts(registers / "file_census.csv",
                    [{"rel_path": LISTING_FILES[1]["rel_path"], "sha256": SHA, "kind": "pdf", "pages": "2",
                      "text_pages": "2", "vector_pages": "0", "raster_pages": "0", "mixed_pages": "0",
                      "total_annotations": "0", "seconds": "0.1", "error": ""}],
                    ["rel_path", "sha256", "kind", "pages", "text_pages", "vector_pages", "raster_pages",
                     "mixed_pages", "total_annotations", "seconds", "error"])
    write_csv_dicts(registers / "sample_zones.csv", [{"zone": "AHU-1", "storey": "L1"}], ["zone", "storey"])

    qa = root / "out" / "qa"
    write_csv_dicts(qa / "blocked_inputs.csv",
                    [{"rel_path": LISTING_FILES[3]["rel_path"], "bytes": "99000000",
                      "reason": "above connector limit 10485760 bytes", "needed_for": "sheet index"}],
                    list(checks.BLOCKED_INPUT_COLUMNS))
    for name in checks.TEMPLATE_QA_FILES:
        (qa / name).write_text(f"# {name}\n", encoding="utf-8")
    (qa / decisions.FILE_NAME).unlink(missing_ok=True)
    decisions.add_entry(qa / decisions.FILE_NAME,
                        decisions.build_entry("D-001", "Drive transport", "no rclone credential here",
                                              ["connector", "rclone"], "connector", asked_at="2026-11-22T10:00:00Z"))
    return root


@pytest.fixture()
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A complete synthetic tree with ``AIGH_ROOT`` pointing at it."""
    root = build_tree(tmp_path / "proj")
    monkeypatch.setenv("AIGH_ROOT", str(root))
    return root


def results_by_id(root: Optional[Path] = None) -> Dict[str, checks.CheckResult]:
    """Run Phase 0 against the current ``AIGH_ROOT`` and index the results."""
    return {r.check_id: r for r in checks.run_phase(0, checks.Context(root))}


def statuses(root: Optional[Path] = None) -> Dict[str, str]:
    return {k: v.status for k, v in results_by_id(root).items()}


# --------------------------------------------------------------------------- #
# Whole phase
# --------------------------------------------------------------------------- #
def test_complete_tree_passes_and_writes_report(tree: Path) -> None:
    assert checks.main(["phase0", "--quiet"]) == 0
    report = tree / "out" / "qa" / "check_phase0.md"
    assert report.is_file()
    text = report.read_text(encoding="utf-8")
    assert "Result: **PASS**" in text
    assert "| P0-01 |" in text and "| P0-12 |" in text
    assert "FAIL" not in text.split("|---|---|---|---|")[1]


def test_registry_has_twelve_phase0_checks() -> None:
    ids = [c.check_id for c in checks.PHASE_CHECKS[0]]
    assert ids == [f"P0-{n:02d}" for n in range(1, 13)]
    assert len(set(ids)) == 12


def test_other_phases_are_stubs(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for phase in range(1, 6):
        assert checks.main([f"phase{phase}"]) == 2
    assert "not implemented" in capsys.readouterr().err
    assert checks.main(["list"]) == 0


def test_cli_via_python_dash_m(tree: Path) -> None:
    env = dict(os.environ, AIGH_ROOT=str(tree))
    proc = subprocess.run([sys.executable, "-m", "src.checks", "phase0"], cwd=str(PROJECT_ROOT), env=env,
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "12 PASS, 0 FAIL, 0 SKIP" in proc.stdout
    assert "P0-09 PASS" in proc.stdout


def test_crashing_check_becomes_fail() -> None:
    def boom(_ctx: checks.Context) -> checks.CheckOutcome:
        raise RuntimeError("synthetic")

    result = checks.run_check(checks.Check("X-01", "boom", boom), checks.Context.__new__(checks.Context))
    assert result.status == checks.FAIL
    assert "RuntimeError" in result.evidence[0]


# --------------------------------------------------------------------------- #
# Individual checks: PASS, FAIL and SKIP outcomes
# --------------------------------------------------------------------------- #
def test_p0_01_missing_bootstrap_file(tree: Path) -> None:
    (tree / "requirements.lock").unlink()
    result = results_by_id()["P0-01"]
    assert result.status == checks.FAIL
    assert "requirements.lock" in result.evidence[0]
    assert checks.main(["phase0", "--quiet"]) == 1


def test_p0_02_config_without_fourth_folder(tree: Path) -> None:
    cfg = tree / "config" / "project.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("    markdown:   {id: fx_markdown, role: upload-target}\n", ""),
                   encoding="utf-8")
    result = results_by_id()["P0-02"]
    assert result.status == checks.FAIL
    assert "markdown" in result.evidence[0]


def test_p0_03_single_listing_is_pending(tree: Path) -> None:
    (tree / "state" / "listings" / "listing_20261122T102000Z.json").unlink()
    result = results_by_id()["P0-03"]
    assert result.status == checks.FAIL
    assert "second listing pending" in result.evidence


def test_p0_03_listings_too_close(tree: Path) -> None:
    listings = tree / "state" / "listings"
    (listings / "listing_20261122T102000Z.json").unlink()
    atomic_write_json(listings / "listing_20261122T100500Z.json", _listing("", "2026-11-22T10:05:00Z"))
    result = results_by_id()["P0-03"]
    assert result.status == checks.FAIL
    assert "second listing pending" in result.evidence
    assert any("5.0 min" in line for line in result.evidence)


def test_p0_03_missing_unstable_file(tree: Path) -> None:
    (tree / "state" / "unstable_departments.json").unlink()
    result = results_by_id()["P0-03"]
    assert result.status == checks.FAIL
    assert any("unstable_departments.json missing" in line for line in result.evidence)


def test_p0_04_row_count_mismatch(tree: Path) -> None:
    rows = _manifest_rows()[:-1]
    write_csv_dicts(tree / "out" / "registers" / "manifest.csv", rows, list(rows[0].keys()))
    result = results_by_id()["P0-04"]
    assert result.status == checks.FAIL
    assert "row count differs" in result.evidence[1]


def test_p0_04_pulled_without_size_match(tree: Path) -> None:
    ledger = tree / "state" / "pull_ledger.jsonl"
    ledger.unlink()
    append_jsonl(ledger, _ledger_row(LISTING_FILES[0]))
    append_jsonl(ledger, _ledger_row(LISTING_FILES[1], size_match=False))
    result = results_by_id()["P0-04"]
    assert result.status == checks.FAIL
    assert any("size_match" in line and "RDS.pdf" in line for line in result.evidence)


def test_p0_04_pulled_without_sha(tree: Path) -> None:
    rows = _manifest_rows()
    rows[0]["sha256"] = ""
    write_csv_dicts(tree / "out" / "registers" / "manifest.csv", rows, list(rows[0].keys()))
    result = results_by_id()["P0-04"]
    assert result.status == checks.FAIL
    assert any("without sha256" in line for line in result.evidence)


def test_p0_05_zip_count_mismatch(tree: Path) -> None:
    write_csv_dicts(tree / "out" / "registers" / "skipped_zip.csv", [],
                    ["rel_path", "bytes", "parent_folder", "drive_id", "nested"])
    assert statuses()["P0-05"] == checks.FAIL
    (tree / "out" / "registers" / "skipped_zip.csv").unlink()
    assert statuses()["P0-05"] == checks.FAIL


def test_p0_06_census_missing_pdf(tree: Path) -> None:
    (tree / "out" / "registers" / "file_census.csv").unlink()
    result = results_by_id()["P0-06"]
    assert result.status == checks.FAIL
    write_csv_dicts(tree / "out" / "registers" / "file_census.csv", [{"rel_path": "other.pdf", "sha256": SHA}],
                    ["rel_path", "sha256"])
    result = results_by_id()["P0-06"]
    assert result.status == checks.FAIL
    assert any("not in census" in line for line in result.evidence)


def test_p0_06_passes_without_pdf_in_ledger(tree: Path) -> None:
    ledger = tree / "state" / "pull_ledger.jsonl"
    ledger.unlink()
    append_jsonl(ledger, _ledger_row(LISTING_FILES[0]))
    (tree / "out" / "registers" / "file_census.csv").unlink()
    assert statuses()["P0-06"] == checks.PASS


def test_p0_07_decisions_absent_or_incomplete(tree: Path) -> None:
    path = tree / "out" / "qa" / decisions.FILE_NAME
    path.write_text("# Decisions needed\n\n### D-001 Drive transport\n- Status: open\n- Context: x\n", encoding="utf-8")
    result = results_by_id()["P0-07"]
    assert result.status == checks.FAIL
    assert "D-001" in result.evidence[-1]
    path.write_text("# Decisions needed\n\nnothing yet\n", encoding="utf-8")
    assert statuses()["P0-07"] == checks.FAIL
    path.unlink()
    assert statuses()["P0-07"] == checks.FAIL
    assert statuses()["P0-01"] == checks.FAIL  # the file is also a bootstrap file


def test_p0_08_source_lock_variants(tree: Path) -> None:
    lock = tree / "state" / "source_lock.json"
    atomic_write_json(lock, {"locked": True, "method": "chmod", "enforced": False,
                             "note": "root ignores chmod bits, so the lock is advisory"})
    result = results_by_id()["P0-08"]
    assert result.status == checks.PASS
    assert "advisory" in result.evidence[0]
    atomic_write_json(lock, {"locked": True, "method": "chmod", "enforced": False, "note": ""})
    assert statuses()["P0-08"] == checks.FAIL
    atomic_write_json(lock, {"locked": True, "method": "chmod"})
    assert statuses()["P0-08"] == checks.FAIL
    lock.unlink()
    assert statuses()["P0-08"] == checks.FAIL


def test_p0_09_failing_selftest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = build_tree(tmp_path / "proj", pressure="pressure_fail.py")
    monkeypatch.setenv("AIGH_ROOT", str(root))
    result = results_by_id()["P0-09"]
    assert result.status == checks.FAIL
    assert result.evidence[0] == "exit code 1"
    assert any("0.4 Pa" in line for line in result.evidence)
    assert checks.main(["phase0", "--quiet"]) == 1


def test_p0_10_blocked_inputs_variants(tree: Path) -> None:
    path = tree / "out" / "qa" / "blocked_inputs.csv"
    write_csv_dicts(path, [], ["rel_path", "bytes", "reason", "needed_for"])
    result = results_by_id()["P0-10"]
    assert result.status == checks.FAIL
    assert any("not listed" in line for line in result.evidence)
    write_csv_dicts(path, [{"rel_path": "x", "bytes": "1", "reason": "r"}], ["rel_path", "bytes", "reason"])
    result = results_by_id()["P0-10"]
    assert result.status == checks.FAIL
    assert "needed_for" in result.evidence[0]
    path.unlink()
    assert statuses()["P0-10"] == checks.FAIL


def test_p0_10_stale_connector_entry(tree: Path) -> None:
    path = tree / "out" / "qa" / "blocked_inputs.csv"
    rows = [{"rel_path": LISTING_FILES[3]["rel_path"], "bytes": "99000000", "reason": "above connector limit",
             "needed_for": "sheet index"},
            {"rel_path": "Basis/Other.pdf", "bytes": "5", "reason": "connector limit", "needed_for": "x"}]
    write_csv_dicts(path, rows, list(checks.BLOCKED_INPUT_COLUMNS))
    result = results_by_id()["P0-10"]
    assert result.status == checks.FAIL
    assert any("Basis/Other.pdf" in line for line in result.evidence)


def test_p0_11_sheet_index_or_blocked(tree: Path) -> None:
    assert statuses()["P0-11"] == checks.PASS  # blocked_inputs lists the background PDF
    write_csv_dicts(tree / "out" / "registers" / "sheet_index.csv", [{"sheet": "IP-BH-05010"}], ["sheet"])
    result = results_by_id()["P0-11"]
    assert result.status == checks.PASS and "sheet_index.csv present" in result.evidence[0]
    (tree / "out" / "registers" / "sheet_index.csv").unlink()
    (tree / "out" / "qa" / "blocked_inputs.csv").unlink()
    assert statuses()["P0-11"] == checks.FAIL


def test_p0_12_skip_until_workflow_done(tree: Path) -> None:
    (tree / "out" / "qa" / "template_spec_B_NDH.md").unlink()
    result = results_by_id()["P0-12"]
    assert result.status == checks.SKIP
    assert "template_spec_B_NDH.md" in result.evidence[0]
    assert checks.main(["phase0", "--quiet"]) == 0  # SKIP does not fail the phase
    assert "| P0-12 | Template workflow artefacts present | SKIP |" in (tree / "out" / "qa" / "check_phase0.md").read_text()
    Progress(tree / "state" / "progress.json").set("template_workflow", "done")
    assert statuses()["P0-12"] == checks.FAIL
    assert checks.main(["phase0", "--quiet"]) == 1


# --------------------------------------------------------------------------- #
# decisions.py
# --------------------------------------------------------------------------- #
def test_decisions_add_list_next_id_idempotent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "decisions_needed.md"
    args = ["--path", str(path), "add", "--id", "D-001", "--title", "Drive transport",
            "--context", "no credential", "--options", "connector; rclone", "upload",
            "--provisional", "connector", "--asked-at", "2026-11-22T10:00:00Z"]
    assert decisions.main(args) == 0
    first = path.read_text(encoding="utf-8")
    assert "### D-001 Drive transport" in first
    assert "- Options: connector; rclone; upload" in first
    assert "- Provisional value: connector" in first
    assert decisions.main(args) == 0  # second add with the same id changes nothing
    assert path.read_text(encoding="utf-8") == first
    assert "unchanged" in capsys.readouterr().out

    assert decisions.main(["--path", str(path), "next-id"]) == 0
    assert capsys.readouterr().out.strip() == "D-002"
    assert decisions.main(["--path", str(path), "add", "--title", "Second", "--context", "c",
                           "--options", "a", "--provisional", "p"]) == 0
    _, entries = decisions.parse_file(path)
    assert [e.id for e in entries] == ["D-001", "D-002"]
    assert decisions.main(["--path", str(path), "list"]) == 0
    out = capsys.readouterr().out
    assert "D-001" in out and "D-002" in out and "Drive transport" in out


def test_decisions_update_and_set_status_preserve_preamble(tmp_path: Path) -> None:
    path = tmp_path / "decisions_needed.md"
    path.write_text("# Decisions needed\n\nIntro paragraph kept verbatim.\n\n", encoding="utf-8")
    entry = decisions.build_entry("D-003", "Leakage", "c", ["5", "10"], "10", asked_at="2026-11-22T10:00:00Z")
    assert decisions.add_entry(path, entry)[0] == "added"
    newer = decisions.build_entry("D-003", "Leakage", "c", ["5", "10"], "12", asked_at="2026-12-01T00:00:00Z")
    assert decisions.add_entry(path, newer)[0] == "unchanged"
    outcome, stored = decisions.add_entry(path, newer, update=True)
    assert outcome == "updated"
    assert stored.get("Provisional value") == "12"
    assert stored.get("Asked at") == "2026-11-22T10:00:00Z"
    assert decisions.set_status(path, "D-003", "decided") is True
    assert decisions.set_status(path, "D-999", "decided") is False
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Decisions needed\n\nIntro paragraph kept verbatim.\n")
    assert "- Status: decided" in text
    assert decisions.main(["--path", str(path), "set-status", "D-999", "x"]) == 1


def test_decisions_parser_tolerates_bold_labels() -> None:
    text = ("# Decisions\n\n### D-010 Something\n**Status:** open\n**Context:** a long\n  continued line\n"
            "- Options: x | y\n- Provisional value: x\n- Asked at: now\n")
    preamble, entries = decisions.parse_text(text)
    assert preamble.startswith("# Decisions")
    assert len(entries) == 1
    assert entries[0].get("Status") == "open"
    assert entries[0].get("Context") == "a long continued line"
    assert decisions.next_id(entries) == "D-011"


def test_decisions_rejects_bad_id() -> None:
    with pytest.raises(ValueError):
        decisions.build_entry("X1", "t", "c", ["o"], "p")


def test_decisions_default_path_honours_aigh_root(tree: Path) -> None:
    assert decisions.decisions_path() == tree / "out" / "qa" / "decisions_needed.md"
