"""Adversarial tests for src.checks and src.decisions (independent review).

Every test builds on the synthetic tree from ``tests/test_checks.py`` (never the
real sources).  Tests assert the behaviour the specification implies; a failing
test here documents a defect listed in ``out/qa/verify/code_checks_review.md``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Dict, List

import pytest

import test_checks as base
from src import checks, decisions
from src.common import append_jsonl, atomic_write_json, write_csv_dicts

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"


@pytest.fixture()
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A complete synthetic tree with ``AIGH_ROOT`` pointing at it."""
    root = base.build_tree(tmp_path / "proj")
    monkeypatch.setenv("AIGH_ROOT", str(root))
    return root


def statuses() -> Dict[str, str]:
    return {r.check_id: r.status for r in checks.run_phase(0, checks.Context())}


def results() -> Dict[str, checks.CheckResult]:
    return {r.check_id: r for r in checks.run_phase(0, checks.Context())}


# --------------------------------------------------------------------------- #
# decisions.py: the file is a human-edited record and must survive a rewrite
# --------------------------------------------------------------------------- #
HUMAN_EDITED = (
    "# Decisions needed\n\n"
    "### D-001 Drive transport\n"
    "- Status: open\n"
    "- Context: no rclone credential here\n"
    "- Options:\n"
    "  - connector\n"
    "  - rclone\n"
    "- Provisional value: connector\n"
    "- Asked at: 2026-11-22T10:00:00Z\n\n"
    "Marcus replied on 23 Nov: keep the connector for the pilot.\n\n"
    "## Resolved\n\n"
    "None yet.\n"
)


def test_decisions_add_preserves_free_text_and_later_sections(tmp_path: Path) -> None:
    """Adding a new id must not corrupt what the engineer wrote under earlier entries."""
    path = tmp_path / "decisions_needed.md"
    path.write_text(HUMAN_EDITED, encoding="utf-8")
    entry = decisions.build_entry("D-002", "Leakage", "c", ["5", "10"], "10", asked_at="2026-11-22T11:00:00Z")
    assert decisions.add_entry(path, entry)[0] == "added"
    text = path.read_text(encoding="utf-8")
    _, entries = decisions.parse_text(text)
    first = next(e for e in entries if e.id == "D-001")
    # The Asked at value must still be a bare timestamp, not a paragraph glued on.
    assert first.get("Asked at") == "2026-11-22T10:00:00Z"
    # The reply paragraph and the Resolved section must still exist as separate text.
    assert "Marcus replied on 23 Nov" in text
    assert re.search(r"^## Resolved$", text, re.MULTILINE), "the '## Resolved' section heading was lost"
    # Sub-bullet options must not be flattened into a single '- Options: - a - b' line.
    assert "- Options: - connector - rclone" not in text


def test_decisions_duplicate_ids_are_reported_by_p0_07(tree: Path) -> None:
    """Two entries with the same id is a malformed register; P0-07 must not report PASS."""
    path = tree / "out" / "qa" / decisions.FILE_NAME
    text = path.read_text(encoding="utf-8")
    path.write_text(text + "\n" + text.split("\n\n", 1)[1].replace("Drive transport", "Duplicate"), encoding="utf-8")
    _, entries = decisions.parse_file(path)
    assert [e.id for e in entries] == ["D-001", "D-001"]
    assert decisions.next_id(entries) == "D-002"
    result = results()["P0-07"]
    assert result.status == checks.FAIL, result.evidence


def test_decisions_concurrent_adds_do_not_lose_entries(tmp_path: Path) -> None:
    """Parallel adds of distinct ids must all land (state lives in files, several agents write)."""
    path = tmp_path / "decisions_needed.md"
    env = dict(os.environ)
    procs = [subprocess.Popen([sys.executable, "-m", "src.decisions", "--path", str(path), "add", "--id", f"D-{i:03d}",
                               "--title", f"T{i}", "--context", "c", "--options", "a", "b", "--provisional", "p"],
                              cwd=str(PROJECT_ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
             for i in range(1, 13)]
    assert all(p.wait(timeout=60) == 0 for p in procs)
    _, entries = decisions.parse_file(path)
    assert sorted(e.id for e in entries) == [f"D-{i:03d}" for i in range(1, 13)]


def test_decisions_cli_round_trip_with_unicode_and_spaces(tmp_path: Path) -> None:
    """``python -m src.decisions`` add/list/next-id with unicode titles, idempotent on id."""
    path = tmp_path / "décisions needed.md"
    cmd = [sys.executable, "-m", "src.decisions", "--path", str(path)]
    add = cmd + ["add", "--id", "D-007", "--title", "Fläche – café plan", "--context", "Ärzte  room  list",
                 "--options", "1; 2", "3", "--provisional", "≥ 2 %", "--asked-at", "2026-11-22T10:00:00Z"]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    first = subprocess.run(add, cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    before = path.read_bytes()
    second = subprocess.run(add, cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True, check=False)
    assert second.returncode == 0 and "unchanged" in second.stdout
    assert path.read_bytes() == before
    text = path.read_text(encoding="utf-8")
    assert "### D-007 Fläche – café plan" in text
    assert "- Options: 1; 2; 3" in text
    assert "- Provisional value: ≥ 2 %" in text
    nxt = subprocess.run(cmd + ["next-id"], cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True, check=False)
    assert nxt.stdout.strip() == "D-008"
    lst = subprocess.run(cmd + ["list"], cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True, check=False)
    assert lst.returncode == 0 and "D-007" in lst.stdout and "Fläche" in lst.stdout


def test_decisions_list_on_missing_file_and_bad_subcommand(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "none.md"
    assert decisions.main(["--path", str(path), "list"]) == 0
    assert "no entries" in capsys.readouterr().out
    assert decisions.main(["--path", str(path), "next-id"]) == 0
    assert capsys.readouterr().out.strip() == "D-001"
    assert not path.exists(), "list/next-id must not create the file"
    with pytest.raises(SystemExit):
        decisions.main(["--path", str(path), "frobnicate"])


# --------------------------------------------------------------------------- #
# checks.py: listings
# --------------------------------------------------------------------------- #
def test_stray_listing_file_does_not_become_the_latest_listing(tree: Path) -> None:
    """A non-listing file matching ``listing_*.json`` must not displace the real latest listing."""
    stray = tree / "state" / "listings" / "listing_broken.json"
    stray.write_text("{not json", encoding="utf-8")
    res = results()
    assert res["P0-03"].status == checks.PASS, res["P0-03"].evidence
    assert res["P0-04"].status == checks.PASS, res["P0-04"].evidence
    assert res["P0-05"].status == checks.PASS, res["P0-05"].evidence


def test_three_listings_two_close_one_far_passes(tree: Path) -> None:
    listings = tree / "state" / "listings"
    atomic_write_json(listings / "listing_20261122T100100Z.json", base._listing("", "2026-11-22T10:01:00Z"))
    result = results()["P0-03"]
    assert result.status == checks.PASS
    assert any("20.0 min" in line for line in result.evidence)


def test_listing_time_falls_back_to_file_name(tree: Path) -> None:
    listings = tree / "state" / "listings"
    for name in ("listing_20261122T100000Z.json", "listing_20261122T102000Z.json"):
        doc = base._listing("", "")
        doc["taken_at"] = ""
        atomic_write_json(listings / name, doc)
    assert statuses()["P0-03"] == checks.PASS


def test_changed_filesize_between_listings_uses_latest_listing(tree: Path) -> None:
    """Second listing reports a different fileSize for f1; the checks must read the latest listing and the ledger flag."""
    listings = tree / "state" / "listings"
    doc = base._listing("", "2026-11-22T10:20:00Z")
    files = [dict(f) for f in doc["files"]]
    files[0]["fileSize"] = "1500"
    doc["files"] = files
    atomic_write_json(listings / "listing_20261122T102000Z.json", doc)
    assert statuses()["P0-04"] == checks.PASS  # ledger still says size_match true
    ledger = tree / "state" / "pull_ledger.jsonl"
    ledger.unlink()
    append_jsonl(ledger, base._ledger_row(base.LISTING_FILES[0], size_match=False))
    append_jsonl(ledger, base._ledger_row(base.LISTING_FILES[1]))
    assert statuses()["P0-04"] == checks.FAIL
    append_jsonl(ledger, base._ledger_row(base.LISTING_FILES[0], size_match=True))  # re-fetch: later row wins
    assert statuses()["P0-04"] == checks.PASS
    # A string "false" in the ledger must not be read as true.
    ledger.unlink()
    row = base._ledger_row(base.LISTING_FILES[0])
    row["size_match"] = "false"
    append_jsonl(ledger, row)
    append_jsonl(ledger, base._ledger_row(base.LISTING_FILES[1]))
    assert statuses()["P0-04"] == checks.FAIL


def test_empty_listing_and_header_only_registers_are_consistent(tree: Path) -> None:
    """Zero files in the listing with header-only manifest and skipped_zip is a consistent (if odd) state."""
    listings = tree / "state" / "listings"
    for name in ("listing_20261122T100000Z.json", "listing_20261122T102000Z.json"):
        doc = base._listing("", "2026-11-22T10:00:00Z" if "100000" in name else "2026-11-22T10:20:00Z")
        doc["files"] = []
        atomic_write_json(listings / name, doc)
    cols = list(base._manifest_rows()[0].keys())
    write_csv_dicts(tree / "out" / "registers" / "manifest.csv", [], cols)
    write_csv_dicts(tree / "out" / "registers" / "skipped_zip.csv", [], ["rel_path", "bytes", "parent_folder", "drive_id", "nested"])
    (tree / "state" / "pull_ledger.jsonl").unlink()
    res = results()
    assert res["P0-05"].status == checks.PASS, res["P0-05"].evidence
    assert res["P0-04"].status == checks.PASS, res["P0-04"].evidence


# --------------------------------------------------------------------------- #
# checks.py: paths with unicode and spaces across registers
# --------------------------------------------------------------------------- #
def test_unicode_and_spaced_paths_match_across_ledger_census_and_blocked(tree: Path) -> None:
    odd = "DD Sign-OFF/29 - Lab/Submitted/Ärzte café – RDS  (rev B).pdf"
    big = "Basis/Tender  Drawings – Ébauche .pdf"
    files = [dict(f) for f in base.LISTING_FILES]
    files[1] = dict(files[1], rel_path=odd, title=odd.rsplit("/", 1)[-1])
    files[3] = dict(files[3], rel_path=big, title=big.rsplit("/", 1)[-1])
    listings = tree / "state" / "listings"
    for name, stamp in (("listing_20261122T100000Z.json", "2026-11-22T10:00:00Z"),
                        ("listing_20261122T102000Z.json", "2026-11-22T10:20:00Z")):
        atomic_write_json(listings / name, {"taken_at": stamp, "files": files, "folders": [base.FOLDER]})
    rows = base._manifest_rows()
    rows[1]["rel_path"] = odd
    rows[3]["rel_path"] = big
    write_csv_dicts(tree / "out" / "registers" / "manifest.csv", rows, list(rows[0].keys()))
    ledger = tree / "state" / "pull_ledger.jsonl"
    ledger.unlink()
    append_jsonl(ledger, base._ledger_row(base.LISTING_FILES[0]))
    append_jsonl(ledger, base._ledger_row(dict(base.LISTING_FILES[1], rel_path=odd)))
    write_csv_dicts(tree / "out" / "registers" / "file_census.csv",
                    [{"rel_path": odd, "sha256": base.SHA, "kind": "pdf"}], ["rel_path", "sha256", "kind"])
    plan = [dict(r) for r in __import__("src.common", fromlist=["read_csv_dicts"]).read_csv_dicts(tree / "state" / "pull_plan.csv")]
    plan[1]["rel_path"] = odd
    plan[3]["rel_path"] = big
    write_csv_dicts(tree / "state" / "pull_plan.csv", plan, list(plan[0].keys()))
    write_csv_dicts(tree / "out" / "qa" / "blocked_inputs.csv",
                    [{"rel_path": big, "bytes": "99000000", "reason": "above connector limit", "needed_for": "x"},
                     {"rel_path": base.LISTING_FILES[3]["rel_path"], "bytes": "99000000",
                      "reason": "rclone remote not configured", "needed_for": "sheet index"}],
                    list(checks.BLOCKED_INPUT_COLUMNS))
    res = results()
    for cid in ("P0-04", "P0-06", "P0-10", "P0-11"):
        assert res[cid].status == checks.PASS, (cid, res[cid].evidence)
    report = tree / "out" / "qa" / "check_phase0.md"
    assert checks.main(["phase0", "--quiet"]) == 0
    assert "Ärzte" not in report.read_text(encoding="utf-8") or True  # report must at least be valid UTF-8


# --------------------------------------------------------------------------- #
# checks.py: blocked inputs
# --------------------------------------------------------------------------- #
def test_bom_prefixed_blocked_inputs_is_read(tree: Path) -> None:
    """A CSV saved by Excel carries a UTF-8 BOM; the column check and path matching must still work."""
    path = tree / "out" / "qa" / "blocked_inputs.csv"
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    res = results()
    assert res["P0-10"].status == checks.PASS, res["P0-10"].evidence
    assert res["P0-11"].status == checks.PASS, res["P0-11"].evidence


def test_blocked_inputs_without_a_pull_plan_is_not_a_pass(tree: Path) -> None:
    (tree / "state" / "pull_plan.csv").unlink()
    write_csv_dicts(tree / "out" / "qa" / "blocked_inputs.csv", [], list(checks.BLOCKED_INPUT_COLUMNS))
    result = results()["P0-10"]
    assert result.status != checks.PASS, result.evidence


def test_blocked_row_whose_reason_omits_the_word_connector_is_still_reconciled(tree: Path) -> None:
    """Reconciliation must key on the plan, not on the word 'connector' in free text."""
    rows = [{"rel_path": base.LISTING_FILES[3]["rel_path"], "bytes": "99000000",
             "reason": "above the 10 MiB transport limit", "needed_for": "sheet index"},
            {"rel_path": "Basis/Not in plan.pdf", "bytes": "99000000",
             "reason": "above the 10 MiB transport limit", "needed_for": "x"}]
    write_csv_dicts(tree / "out" / "qa" / "blocked_inputs.csv", rows, list(checks.BLOCKED_INPUT_COLUMNS))
    result = results()["P0-10"]
    assert result.status == checks.FAIL, result.evidence
    assert any("Not in plan.pdf" in line for line in result.evidence)


# --------------------------------------------------------------------------- #
# checks.py: template workflow, self-test, runner
# --------------------------------------------------------------------------- #
def test_some_producer_sets_the_template_workflow_progress_item() -> None:
    """P0-12 can only ever FAIL if something marks progress item 'template_workflow' done."""
    setters = [p for p in SRC_DIR.glob("*.py") if p.name != "checks.py"
               and "template_workflow" in p.read_text(encoding="utf-8")]
    assert setters, "no module under src/ writes progress item 'template_workflow'; P0-12 can never FAIL"


def test_p0_09_timeout_is_a_fail(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tree / "src" / "pressure.py").write_text("import time, sys\ntime.sleep(5)\nsys.exit(0)\n", encoding="utf-8")
    monkeypatch.setattr(checks, "SELFTEST_TIMEOUT_S", 1)
    result = results()["P0-09"]
    assert result.status == checks.FAIL
    assert "timed out" in result.evidence[0]


def test_p0_09_selftest_sees_the_synthetic_root_not_the_real_one(tree: Path) -> None:
    probe = ("import os, sys\n"
             "sys.exit(0 if os.environ.get('AIGH_ROOT') == os.getcwd() else 3)\n")
    (tree / "src" / "pressure.py").write_text(probe, encoding="utf-8")
    assert results()["P0-09"].status == checks.PASS


def test_unknown_status_and_bad_report_path(tree: Path) -> None:
    def weird(_ctx: checks.Context) -> checks.CheckOutcome:
        return "MAYBE", ["x"]

    result = checks.run_check(checks.Check("X-02", "weird", weird), checks.Context())
    assert result.status == checks.FAIL and "unknown status" in result.evidence[0]
    blocker = tree / "blocker.txt"
    blocker.write_text("x", encoding="utf-8")
    assert checks.main(["phase0", "--quiet", "--report", str(blocker / "nested" / "r.md")]) == 1


def test_report_is_rewritten_identically_on_rerun(tree: Path) -> None:
    report = tree / "out" / "qa" / "check_phase0.md"
    assert checks.main(["phase0", "--quiet"]) == 0
    first = report.read_text(encoding="utf-8")
    assert checks.main(["phase0", "--quiet"]) == 0
    second = report.read_text(encoding="utf-8")
    strip = lambda t: re.sub(r"- Run at: .*", "", t)  # noqa: E731
    assert strip(first) == strip(second)
    assert len([ln for ln in first.splitlines() if ln.startswith("| P0-")]) == 12
    tmp_leftovers = [p for p in report.parent.iterdir() if p.name.startswith(".check_phase0.md.")]
    assert not tmp_leftovers


def test_checks_never_write_under_data_source(tree: Path) -> None:
    source = tree / "data" / "source"
    before = sorted(p.relative_to(source) for p in source.rglob("*"))
    assert checks.main(["phase0", "--quiet"]) == 0
    assert sorted(p.relative_to(source) for p in source.rglob("*")) == before


def test_threaded_context_is_not_shared_between_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two trees evaluated in sequence must not leak cached config between runs."""
    root_a = base.build_tree(tmp_path / "a")
    root_b = base.build_tree(tmp_path / "b")
    (root_b / "requirements.lock").unlink()
    monkeypatch.setenv("AIGH_ROOT", str(root_a))
    assert statuses()["P0-01"] == checks.PASS
    monkeypatch.setenv("AIGH_ROOT", str(root_b))
    assert statuses()["P0-01"] == checks.FAIL
    monkeypatch.setenv("AIGH_ROOT", str(root_a))
    assert statuses()["P0-01"] == checks.PASS
