"""Tests for src.blocked_inputs on a synthetic project tree (never the real sources)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Dict, List

import pytest

from src import blocked_inputs, checks
from src.common import append_jsonl, atomic_write_json, read_csv_dicts, write_csv_dicts
from src.state import Progress

REAL_ROOT = Path(__file__).resolve().parents[1]
PLAN_COLUMNS = ["drive_id", "rel_path", "bytes", "mime_type", "transport_eligible", "status"]
MB = 1_048_576
BOOKLET = "DD Sign-OFF/26 - Diagnostic Imaging/Submitted/20250814_IGH_DD Booklet_DI.pdf"
COMMENTS = "DD Sign-OFF/30 - Pharmacy/AH sign off with comments/Pharmacy DD-sign off comments.xlsx"
BACKGROUND = "Basis/Arch Combined Tender .pdf"
APP_D = "Basis/App D/Appendix D - ACMV For Common Areas_Combined.pdf"
MOT_SPEC = "Basis/MOT NSC/Tender/Docs/0. AH MOT NSC - Particular Specification.pdf"
MOT_AV = "Basis/MOT NSC/Tender/Docs/4. AH MOT NSC - Tech Specs for AV System.pdf"
MOT_ELEC = "Basis/MOT NSC/Tender/Dwgs/ELEC/IP-BE-00303_ELECTRICAL INTERFACE SCHEMATIC.pdf"
SCHEMATIC = "Basis/Working Drawings/IP-BH-06031_INPATIENT 3RD STOREY AIR-SIDE SCHEMATIC.pdf"
MSG = "DD Sign-OFF/42 - Data Centre/DD Comments_PSR SSR/20250804_DD Comments.msg"
OTHER_DEPT = "DD Sign-OFF/11 - Emergency Dept/big.pdf"
ON_DISK = "Template/Sample.xlsx"
CORPUS = "Basis/Notes/Some report.pdf"

LISTING = [  # id, rel_path, size
    ("b1", BOOKLET, 68 * MB), ("c1", COMMENTS, 67 * MB), ("g1", BACKGROUND, 82 * MB), ("a1", APP_D, 17 * MB),
    ("m1", MOT_SPEC, 25 * MB), ("m2", MOT_AV, 12 * MB), ("m3", MOT_ELEC, 1 * MB), ("s1", SCHEMATIC, 1 * MB),
    ("e1", MSG, 23 * MB), ("o1", OTHER_DEPT, 2 * MB), ("t1", ON_DISK, 74_000), ("k1", CORPUS, 2 * MB),
    ("z1", "DD Sign-OFF/26 - Diagnostic Imaging.zip", 3 * MB), ("f1", "Basis/Small.pdf", 1000),
]


def _plan_status(rel_path: str, size: int) -> str:
    if rel_path.endswith(".zip"):
        return "skipped_zip"
    if rel_path == "Basis/Small.pdf":
        return "fetched"
    return "blocked_connector_limit" if size > 10 * MB else "pending"


@pytest.fixture()
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Project tree with a pull plan, a listing, both ledgers and a skip record."""
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    shutil.copy(REAL_ROOT / "config" / "project.yaml", root / "config" / "project.yaml")
    monkeypatch.setenv("AIGH_ROOT", str(root))
    state = root / "state"
    (state / "listings").mkdir(parents=True)
    (state / "pull_batches").mkdir()
    (root / "out" / "qa").mkdir(parents=True)
    files = [{"id": i, "title": Path(p).name, "mimeType": "application/pdf", "fileSize": str(n), "rel_path": p}
             for i, p, n in LISTING]
    atomic_write_json(state / "listings" / "listing_20261122T100000Z.json",
                      {"taken_at": "2026-11-22T10:00:00Z", "files": files,
                       "folders": [{"id": "d", "mimeType": checks.FOLDER_MIME, "rel_path": "Basis"}]})
    write_csv_dicts(state / "pull_plan.csv",
                    [{"drive_id": i, "rel_path": p, "bytes": n, "mime_type": "application/pdf",
                      "transport_eligible": "y" if n <= 10 * MB else "n", "status": _plan_status(p, n)}
                     for i, p, n in LISTING if p not in (BACKGROUND, APP_D)]
                    + [{"drive_id": i, "rel_path": p, "bytes": n, "mime_type": "application/pdf",
                        "transport_eligible": "n", "status": "fetched"}
                       for i, p, n in LISTING if p in (BACKGROUND, APP_D)], PLAN_COLUMNS)
    append_jsonl(state / "pull_ledger.jsonl", {"drive_id": "f1", "rel_path": "Basis/Small.pdf", "transport": "connector"})
    append_jsonl(state / "upload_ledger.jsonl", {"drive_id": "a1", "rel_path": APP_D, "transport": "upload"})
    append_jsonl(state / "upload_ledger.jsonl",
                 {"drive_id": "g1", "rel_path": BACKGROUND, "transport": "upload-assembled", "size_match": False})
    (state / "pull_batches" / "basis_skipped.csv").write_text(
        "rel_path,bytes,reason\n"
        f"{MOT_AV},{12 * MB},outside ACMV scope (listed, not pulled in Phase 0)\n"
        f"{MOT_ELEC},{MB},outside ACMV scope (listed, not pulled in Phase 0)\n"
        f"{SCHEMATIC},{MB},outside ACMV scope (listed, not pulled in Phase 0)\n", encoding="utf-8")
    on_disk = root / "data" / "source" / ON_DISK
    on_disk.parent.mkdir(parents=True)
    on_disk.write_bytes(b"x")
    return root


def rows_by_path(root: Path) -> Dict[str, Dict[str, str]]:
    return {r["rel_path"]: r for r in read_csv_dicts(root / "out" / "qa" / "blocked_inputs.csv")}


def test_columns_and_rows(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert blocked_inputs.main(["build"]) == 0
    rows = read_csv_dicts(tree / "out" / "qa" / "blocked_inputs.csv")
    assert list(rows[0]) == ["rel_path", "bytes", "reason", "needed_for", "drive_id", "dept_no", "doc_class", "status"]
    got = rows_by_path(tree)
    # blocked and not uploaded: booklet, comments, e-mail, MOT spec (pending) and the out-of-scope AV spec (not needed)
    assert set(got) == {BOOKLET, COMMENTS, MSG, MOT_SPEC, MOT_AV, SCHEMATIC, CORPUS, APP_D, BACKGROUND}
    for rel in (APP_D, BACKGROUND):  # over 10 MB and held by the upload ledger: listed as uploaded
        assert got[rel]["status"] == "uploaded"
    assert got[APP_D]["reason"] == "over connector 10 MB limit; supplied by upload"
    assert got[BACKGROUND]["reason"] == ("over connector 10 MB limit; uploaded in 5 parts and assembled; "
                                         "page content to be confirmed against the Drive original")
    assert (got[APP_D]["bytes"], got[APP_D]["drive_id"]) == (str(17 * MB), "a1")
    for rel in (MOT_ELEC, OTHER_DEPT, ON_DISK, "DD Sign-OFF/26 - Diagnostic Imaging.zip", "Basis/Small.pdf"):
        assert rel not in got
    booklet = got[BOOKLET]
    assert booklet["reason"] == "over connector 10 MB limit; upload or other transport needed"
    assert (booklet["bytes"], booklet["drive_id"], booklet["dept_no"], booklet["doc_class"], booklet["status"]) == \
        (str(68 * MB), "b1", "26", "Booklet", "pending")
    assert booklet["needed_for"] == "design criteria table, zoning pictures, comments (Task 1, 2, 3)"
    assert got[COMMENTS]["needed_for"] == "comment register (Task 1)"
    assert got[MSG]["needed_for"] == "comment register (Task 1)" and got[MSG]["dept_no"] == "42"
    assert got[MOT_SPEC]["needed_for"] == "MOT theatre criteria (Task 3)" and got[MOT_SPEC]["status"] == "pending"
    assert got[MOT_AV]["status"] == "not needed" and "outside ACMV scope" in got[MOT_AV]["reason"]
    assert got[SCHEMATIC]["needed_for"] == "schematic model (Task 4)" and got[SCHEMATIC]["status"] == "not needed"
    assert got[CORPUS]["status"] == "pending" and got[CORPUS]["reason"] == blocked_inputs.UNRECORDED_REASON
    assert "on disk without a ledger row" in capsys.readouterr().out  # the file outside any pilot department is not listed


def test_uploaded_and_gaps(tree: Path) -> None:
    """A pending file the upload ledger holds by rel_path is 'uploaded'; a file with no record is pending."""
    state = tree / "state"
    append_jsonl(state / "upload_ledger.jsonl", {"drive_id": "other", "rel_path": CORPUS, "transport": "upload"})
    append_jsonl(state / "upload_ledger.jsonl", {"drive_id": "x", "rel_path": MOT_ELEC, "transport": "upload"})
    (tree / "data" / "source" / ON_DISK).unlink()
    assert blocked_inputs.main(["build"]) == 0
    got = rows_by_path(tree)
    assert got[CORPUS]["status"] == "uploaded"
    assert got[MOT_ELEC]["status"] == "uploaded" and got[MOT_ELEC]["needed_for"] == "MOT theatre criteria (Task 3)"
    assert got[ON_DISK]["status"] == "pending" and got[ON_DISK]["reason"] == blocked_inputs.UNRECORDED_REASON
    assert got[ON_DISK]["needed_for"] == "Task 1 corpus"


def test_needed_for_rules() -> None:
    cases = {
        "DD Sign-OFF/30 - Pharmacy/a/Pharmacy DD Booklet.pdf": "design criteria table, zoning pictures, comments (Task 1, 2, 3)",
        "DD Sign-OFF/30 - Pharmacy/a/DD comments.xlsx": "comment register (Task 1)",
        "Basis/Specs/Particular Specification.pdf": "MOT theatre criteria (Task 3)",
        "Basis/MOT NSC/Dwgs/anything.pdf": "MOT theatre criteria (Task 3)",
        "Basis/01 ACMV Tender Schematic/IP-BH-05010.pdf": "schematic model (Task 4)",
        "DD Sign-OFF/26 - Diagnostic Imaging/note.msg": "context",
        "Basis/Other/report.pdf": "Task 1 corpus",
    }
    for rel, expected in cases.items():
        classes = blocked_inputs.common.classify_document(rel)
        assert blocked_inputs.needed_for(rel, classes) == expected, rel
    # the comment register rule takes the e-mail named 'comments' before the correspondence rule
    assert blocked_inputs.common.classify_document("x/y/comments.msg") == "comment register"


def test_idempotent_and_progress(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tree / "out" / "qa" / "blocked_inputs.csv"
    assert blocked_inputs.main(["build"]) == 0 and "wrote" in capsys.readouterr().out
    first, mtime = target.read_bytes(), target.stat().st_mtime_ns
    stamp = Progress(tree / "state" / "progress.json").get("blocked_inputs")
    assert stamp is not None and stamp["status"] == "done"
    assert blocked_inputs.main(["build"]) == 0 and "unchanged" in capsys.readouterr().out
    assert target.read_bytes() == first and target.stat().st_mtime_ns == mtime
    assert Progress(tree / "state" / "progress.json").get("blocked_inputs") == stamp
    assert b"\r\n" in first and first.count(b"\n") == len(read_csv_dicts(target)) + 1


def test_missing_plan_fails(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tree / "state" / "pull_plan.csv").unlink()
    assert blocked_inputs.main(["build"]) == 1
    assert "pull_plan.csv" in capsys.readouterr().err
    assert not (tree / "out" / "qa" / "blocked_inputs.csv").exists()


def test_reconciles_with_p0_10_and_follows_the_upload_ledger(tree: Path) -> None:
    assert blocked_inputs.main(["build"]) == 0
    result = checks.run_check(checks.Check("P0-10", "t", checks.check_blocked_inputs), checks.Context(tree))
    assert result.status == checks.PASS, result.evidence
    # the engineer uploads the MOT spec: the plan (re-run) shows it fetched, the file lists it as uploaded
    plan = read_csv_dicts(tree / "state" / "pull_plan.csv")
    for row in plan:
        if row["rel_path"] == MOT_SPEC:
            row["status"] = "fetched"
    write_csv_dicts(tree / "state" / "pull_plan.csv", plan, PLAN_COLUMNS)
    append_jsonl(tree / "state" / "upload_ledger.jsonl", {"drive_id": "m1", "rel_path": MOT_SPEC, "transport": "upload"})
    assert blocked_inputs.main(["build"]) == 0
    assert rows_by_path(tree)[MOT_SPEC]["status"] == "uploaded"  # over 10 MB: stays listed, as uploaded
    result = checks.run_check(checks.Check("P0-10", "t", checks.check_blocked_inputs), checks.Context(tree))
    assert result.status == checks.PASS, result.evidence


def test_p0_10_fails_when_the_file_is_stale(tree: Path) -> None:
    assert blocked_inputs.main(["build"]) == 0
    stale: List[Dict[str, str]] = [r for r in read_csv_dicts(tree / "out" / "qa" / "blocked_inputs.csv")
                                   if r["rel_path"] != BOOKLET]
    write_csv_dicts(tree / "out" / "qa" / "blocked_inputs.csv", stale, list(blocked_inputs.COLUMNS))
    result = checks.run_check(checks.Check("P0-10", "t", checks.check_blocked_inputs), checks.Context(tree))
    assert result.status == checks.FAIL and any("not listed" in line for line in result.evidence)
