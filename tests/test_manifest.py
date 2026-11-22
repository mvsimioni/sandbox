"""Tests for src.manifest using the synthetic fixtures under tests/fixtures/manifest only."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Dict, List

import pytest

from src import manifest
from src.common import read_csv_dicts
from src.state import Progress

FIXTURES = Path(__file__).parent / "fixtures" / "manifest"
LISTING_NAME = "listing_20261122T100000Z.json"


@pytest.fixture()
def workspace(tmp_path: Path) -> Dict[str, Path]:
    """A throw-away project tree: state (with listings/latest), out dirs and local copies of pulled files."""
    state = tmp_path / "state"
    listings = state / "listings"
    listings.mkdir(parents=True)
    shutil.copy(FIXTURES / LISTING_NAME, listings / LISTING_NAME)
    (listings / "latest").symlink_to(listings / LISTING_NAME)
    for name in ("pull_ledger.jsonl", "pull_plan.csv", "unstable_departments.json"):
        shutil.copy(FIXTURES / name, state / name)
    for record in (json.loads(line) for line in (FIXTURES / "pull_ledger.jsonl").read_text().splitlines()):
        if record["drive_id"] == "a2":
            continue  # deliberately missing on disk
        local = tmp_path / record["local_path"]
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(b"x" * 10)
    return {"root": tmp_path, "state": state, "registers": tmp_path / "out" / "registers", "qa": tmp_path / "out" / "qa"}


def _build(ws: Dict[str, Path]) -> Dict[str, Dict[str, str]]:
    """Run the build and index the manifest rows by drive_id."""
    rc = manifest.main(["--state-dir", str(ws["state"]), "--registers-dir", str(ws["registers"]),
                        "--qa-dir", str(ws["qa"]), "--project-root", str(ws["root"]), "build"])
    assert rc == 0
    return {row["drive_id"]: row for row in read_csv_dicts(ws["registers"] / "manifest.csv")}


# --------------------------------------------------------------------------- #
# Manifest columns
# --------------------------------------------------------------------------- #
def test_manifest_columns_and_row_count(workspace: Dict[str, Path]) -> None:
    rows = _build(workspace)
    raw = read_csv_dicts(workspace["registers"] / "manifest.csv")
    assert list(raw[0].keys()) == list(manifest.MANIFEST_COLUMNS)
    listing = json.loads((FIXTURES / LISTING_NAME).read_text())
    assert len(raw) == len(listing["files"])  # one row per non-folder file, zips included
    assert [r["rel_path"] for r in raw] == sorted(r["rel_path"] for r in raw)


def test_every_column_for_a_pulled_file(workspace: Dict[str, Path]) -> None:
    row = _build(workspace)["a1"]
    assert row == {
        "drive_id": "a1",
        "rel_path": "DD Sign-OFF/30 - Pharmacy/Submitted/03_SOA/30_Pharmacy_SOA.xlsx",
        "root": "DD Sign-OFF",
        "dept_no": "30",
        "dept_name": "Pharmacy",
        "dept_alias": "",
        "stage": "Submitted",
        "doc_class": "SOA",
        "ext": "xlsx",
        "bytes": "1000",
        "drive_modified": "2026-11-21T00:00:00Z",
        "drive_md5": "m1",
        "sha256": "s1" * 32,
        "pulled": "y",
        "duplicate_of": "DD Sign-OFF/30 - Pharmacy/AH sign off with comments/03_SOA/30_Pharmacy_SOA.xlsx",
        "notes": "",
    }


def test_sha_duplicates_point_to_first_by_rel_path(workspace: Dict[str, Path]) -> None:
    rows = _build(workspace)
    assert rows["a2"]["duplicate_of"] == ""  # 'AH sign off...' sorts before 'Submitted'
    assert rows["a1"]["duplicate_of"] == rows["a2"]["rel_path"]
    assert rows["b1"]["duplicate_of"] == ""
    assert rows["s1"]["duplicate_of"] == rows["b1"]["rel_path"]
    assert "stray folder" in rows["s1"]["notes"]
    assert "local file missing" in rows["a2"]["notes"]


def test_probable_duplicates_for_unpulled_files(workspace: Dict[str, Path]) -> None:
    rows = _build(workspace)
    assert rows["a4"]["pulled"] == "n" and rows["a4"]["duplicate_of"] == ""
    assert rows["a3"]["duplicate_of"] == rows["a4"]["rel_path"]
    assert manifest.PROBABLE_DUP_NOTE in rows["a3"]["notes"]
    # a6 has the same size as a3/a4 but a different name and md5: not a duplicate
    assert rows["a6"]["duplicate_of"] == ""


def test_alias_number_in_file_name(workspace: Dict[str, Path]) -> None:
    rows = _build(workspace)
    assert rows["a3"]["dept_alias"] == "47" and rows["a3"]["doc_class"] == "drawing list"
    assert rows["a6"]["dept_alias"] == "14"
    assert rows["a1"]["dept_alias"] == ""  # 30 matches the folder
    assert rows["b1"]["dept_alias"] == ""
    assert manifest.dept_alias("7_x.pdf", "07") == ""
    assert manifest.dept_alias("notes 47.pdf", "30") == ""


def test_zip_rows_and_blocked_and_other_roots(workspace: Dict[str, Path]) -> None:
    rows = _build(workspace)
    assert rows["z1"]["doc_class"] == "zip" and rows["z1"]["pulled"] == "n" and rows["z1"]["stage"] == "other"
    assert rows["a5"]["pulled"] == "n" and "blocked: connector limit" in rows["a5"]["notes"]
    assert rows["a5"]["doc_class"] == "Booklet"
    assert rows["c1"] ["root"] == "Basis" and rows["c1"]["stage"] == "" and rows["c1"]["pulled"] == "y"
    assert rows["t1"]["root"] == "Template" and rows["t1"]["dept_no"] == ""
    assert rows["n1"]["dept_no"] == "" and rows["n1"]["stage"] == "other"


# --------------------------------------------------------------------------- #
# Skipped zips
# --------------------------------------------------------------------------- #
def test_skipped_zip_register(workspace: Dict[str, Path]) -> None:
    _build(workspace)
    zips = {r["rel_path"]: r for r in read_csv_dicts(workspace["registers"] / "skipped_zip.csv")}
    assert list(next(iter(zips.values())).keys()) == list(manifest.SKIPPED_ZIP_COLUMNS)
    assert len(zips) == 4
    assert zips["DD Sign-OFF/Archive.zip"]["nested"] == "n"
    dept_zip = zips["DD Sign-OFF/30 - Pharmacy/30 - Pharmacy (2).zip"]
    assert dept_zip["nested"] == "n" and dept_zip["parent_folder"] == "DD Sign-OFF/30 - Pharmacy"
    assert dept_zip["bytes"] == "5000" and dept_zip["drive_id"] == "z1"
    assert zips["DD Sign-OFF/29 - Lab/Submitted/Photos/old photos.zip"]["nested"] == "y"
    assert manifest.zip_is_nested("Basis/x.zip") is True


# --------------------------------------------------------------------------- #
# Anomaly report
# --------------------------------------------------------------------------- #
def test_anomaly_report_sections(workspace: Dict[str, Path]) -> None:
    _build(workspace)
    text = (workspace["qa"] / "inventory_anomalies.md").read_text(encoding="utf-8")
    for heading in (
        "## 1. Counts by root, department and stage",
        "## 2. Department folders",
        "## 3. Zips (excluded from the pull)",
        "## 4. Duplicate zips named ' (2)'",
        "## 5. Stray folders compared with '29 - Lab'",
        "## 6. File names whose number differs from the folder",
        "## 7. Hash-identical duplicates across stages",
        "## 8. Files blocked by the connector limit",
        "## 9. Unstable departments",
    ):
        assert heading in text, heading
    # counts
    assert "| Basis | 1 |" in text and "| Template | 1 |" in text
    assert "| 30 | Pharmacy |  | 8 | 2 | 2 |" in text
    # departments: two numbered, two strays, one unnumbered, 48 gaps
    assert "Numbered department folders found: 2 (expected 50" in text
    assert "- Numbering gaps (01-50): 01, 02" in text and "28, 31" in text and "49, 50" in text
    assert "- Stray folders (leading '__'): __29 - Lab, __29 - Lab" in text
    assert "- Unnumbered folders under DD Sign-OFF: Misc notes" in text
    # zips
    assert "Zips in the listing: 4, total 11,600" in text and "nested: 1." in text
    assert "| DD Sign-OFF/29 - Lab/Submitted/Photos/old photos.zip | 700 |" in text
    assert "| DD Sign-OFF/30 - Pharmacy/30 - Pharmacy (2).zip | 5000 | DD Sign-OFF/30 - Pharmacy/30 - Pharmacy.zip | 5000 | same size |" in text
    # strays
    assert "Stray folders found: 2 (fs1, fs2); governing folder '29 - Lab': found." in text
    assert "### A = __29 - Lab #1 (fs1); B = 29 - Lab (f29)" in text
    assert "| in both and identical | 1 | Submitted/29_Lab_RDS.pdf (sha256) |" in text
    assert "### A = __29 - Lab #2 (fs2); B = 29 - Lab (f29)" in text
    assert "| only in A | 1 | Submitted/29_Lab_extra.pdf |" in text
    assert "### A = __29 - Lab #1 (fs1); B = __29 - Lab #2 (fs2)" in text
    # aliases, cross-stage, blocked, unstable
    assert "| 30 | 47 | DD Sign-OFF/30 - Pharmacy/Submitted/47_AN Hub_Drawing List.xlsx |" in text
    assert "| 30 | 14 | DD Sign-OFF/30 - Pharmacy/Submitted/14_TRED_Drawing List.xlsx |" in text
    assert "| s1s1s1s1s1s1 | AH sign off with comments, Submitted |" in text
    assert "Blocked files: 1, total 20,000,000" in text
    assert "| DD Sign-OFF | 30 | Pharmacy | 1 | 20,000,000" in text
    assert "| 30 - Pharmacy | 3 files changed between listings |" in text


# --------------------------------------------------------------------------- #
# Resumability, listing resolution, summary, failures
# --------------------------------------------------------------------------- #
def test_rebuild_is_idempotent_and_records_progress(workspace: Dict[str, Path]) -> None:
    _build(workspace)
    first = (workspace["registers"] / "manifest.csv").read_bytes()
    _build(workspace)
    assert (workspace["registers"] / "manifest.csv").read_bytes() == first
    item = Progress(workspace["state"] / "progress.json").get("manifest")
    assert item and item["status"] == "done" and len(item["hash"]) == 64 and item["files"] == 16


def test_listing_resolution_without_latest(workspace: Dict[str, Path]) -> None:
    (workspace["state"] / "listings" / "latest").unlink()
    assert manifest.resolve_listing_path(workspace["state"]).name == LISTING_NAME
    (workspace["state"] / "listings" / "latest").write_text(LISTING_NAME)  # pointer file
    assert manifest.resolve_listing_path(workspace["state"]).name == LISTING_NAME
    with pytest.raises(FileNotFoundError):
        manifest.resolve_listing_path(workspace["root"] / "nowhere")


def test_summary_and_missing_inputs(workspace: Dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    args = ["--state-dir", str(workspace["state"]), "--registers-dir", str(workspace["registers"]),
            "--qa-dir", str(workspace["qa"]), "--project-root", str(workspace["root"])]
    assert manifest.main(args + ["summary"]) == 1  # nothing built yet
    assert manifest.main(args) == 0  # build is the default
    assert manifest.main(args + ["summary"]) == 0
    assert '"files": 16' in capsys.readouterr().out
    rows = manifest.summary(workspace["registers"])
    assert rows["files"] == 16 and rows["zips"] == 4 and rows["pulled"] == 5 and rows["departments"] == 2
    assert rows["by_root"] == {"Basis": 1, "DD Sign-OFF": 14, "Template": 1}
    assert manifest.main(["--state-dir", str(workspace["root"] / "empty"), "build"]) == 1


def test_build_without_ledger_or_plan(workspace: Dict[str, Path]) -> None:
    (workspace["state"] / "pull_ledger.jsonl").unlink()
    (workspace["state"] / "pull_plan.csv").unlink()
    (workspace["state"] / "unstable_departments.json").unlink()
    rows = _build(workspace)
    assert all(r["pulled"] == "n" and r["sha256"] == "" for r in rows.values())
    assert "blocked: connector limit" in rows["a5"]["notes"]  # size rule when no plan exists
    assert rows["s1"]["duplicate_of"] == rows["b1"]["rel_path"] and manifest.PROBABLE_DUP_NOTE in rows["s1"]["notes"]
    text = (workspace["qa"] / "inventory_anomalies.md").read_text(encoding="utf-8")
    assert "unstable_departments.json not present" in text
    assert "| in both and identical | 1 | Submitted/29_Lab_RDS.pdf (name+size) |" in text


# --------------------------------------------------------------------------- #
# Review fixes M-1 to M-5: stale pulls, plan coverage, duplicate ids, empty summary, transport
# --------------------------------------------------------------------------- #
def _args(ws: Dict[str, Path]) -> List[str]:
    return ["--state-dir", str(ws["state"]), "--registers-dir", str(ws["registers"]),
            "--qa-dir", str(ws["qa"]), "--project-root", str(ws["root"])]


def _rewrite_listing(ws: Dict[str, Path], mutate) -> None:
    """Apply ``mutate(listing_dict)`` to the workspace listing in place."""
    path = ws["state"] / "listings" / LISTING_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_stale_pull_when_drive_size_or_md5_moved(workspace: Dict[str, Path]) -> None:
    def mutate(data: dict) -> None:
        for rec in data["files"]:
            if rec["id"] == "c1":
                rec["fileSize"] = "501"  # grew on Drive after the fetch
            if rec["id"] == "b1":
                rec["md5Checksum"] = "changed"
    _rewrite_listing(workspace, mutate)
    # give b1's ledger record a Drive md5 at fetch time so the md5 comparison is meaningful
    ledger_path = workspace["state"] / "pull_ledger.jsonl"
    records = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for rec in records:
        if rec["drive_id"] == "b1":
            rec["drive_md5"] = "original"
    ledger_path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    rows = _build(workspace)
    assert rows["c1"]["pulled"] == "n" and rows["c1"]["sha256"] == "" and rows["c1"]["bytes"] == "501"
    assert manifest.STALE_NOTE in rows["c1"]["notes"] and "Drive size 500 at fetch, now 501" in rows["c1"]["notes"]
    assert rows["b1"]["pulled"] == "n" and "Drive md5 changed since fetch" in rows["b1"]["notes"]
    assert rows["a1"]["pulled"] == "y" and manifest.STALE_NOTE not in rows["a1"]["notes"]  # unchanged file
    # the stray twin s1 is still pulled, so it no longer has a sha256 partner: no duplicate_of by hash
    assert rows["s1"]["duplicate_of"] == ""
    text = (workspace["qa"] / "inventory_anomalies.md").read_text(encoding="utf-8")
    assert "## 10. Listing integrity" in text and "stale pulls: 2;" in text
    assert "| Basis/App D/Appendix D - ACMV For Common Areas.pdf | 501 | Drive size 500 at fetch, now 501 |" in text


def test_size_mismatch_at_fetch_is_not_reported_stale() -> None:
    row = {"_has_size": True, "bytes": 9999, "drive_md5": ""}
    assert manifest.stale_reasons(row, {"bytes": 1000, "size_match": False}) == []
    assert manifest.stale_reasons(row, {"bytes": 1000, "size_match": True}) == ["Drive size 1000 at fetch, now 9999"]
    row_md5 = {"_has_size": False, "bytes": 0, "drive_md5": "new"}
    assert manifest.stale_reasons(row_md5, {"md5": "old", "md5_match": None}) == []  # local md5 never verified
    assert manifest.stale_reasons(row_md5, {"md5": "old", "md5_match": True}) == ["Drive md5 changed since fetch"]


def test_oversize_file_absent_from_plan_is_blocked_and_listed(workspace: Dict[str, Path]) -> None:
    def mutate(data: dict) -> None:
        data["files"].append({"id": "new1", "title": "Late booklet.pdf", "mimeType": "application/pdf",
                              "fileSize": "30000000", "fileExtension": "pdf", "modifiedTime": "2026-11-22T11:00:00Z",
                              "parentId": "f30sub", "rel_path": "DD Sign-OFF/30 - Pharmacy/Submitted/Late booklet.pdf"})
    _rewrite_listing(workspace, mutate)
    rows = _build(workspace)
    assert "blocked: connector limit" in rows["a5"]["notes"]  # from the plan
    assert "blocked: connector limit" in rows["new1"]["notes"] and "not in pull plan" in rows["new1"]["notes"]
    text = (workspace["qa"] / "inventory_anomalies.md").read_text(encoding="utf-8")
    assert "Transport: connector. Blocked files: 2, total 50,000,000" in text
    assert "files absent from the pull plan: 1." in text
    assert "| DD Sign-OFF/30 - Pharmacy/Submitted/Late booklet.pdf | 30000000 | n |" in text


def test_duplicate_drive_id_only_inherits_matching_ledger_record(workspace: Dict[str, Path]) -> None:
    twin = "DD Sign-OFF/30 - Pharmacy/DD Closure/Appendix D copy.pdf"

    def mutate(data: dict) -> None:
        data["files"].append({"id": "c1", "title": "Appendix D copy.pdf", "mimeType": "application/pdf",
                              "fileSize": "777", "fileExtension": "pdf", "modifiedTime": "2026-11-22T11:00:00Z",
                              "parentId": "f30dd", "rel_path": twin})
    _rewrite_listing(workspace, mutate)
    _build(workspace)  # its drive_id index would collapse the two c1 rows, so index by path
    by_path = {r["rel_path"]: r for r in read_csv_dicts(workspace["registers"] / "manifest.csv")}
    assert len(by_path) == 17
    original = by_path["Basis/App D/Appendix D - ACMV For Common Areas.pdf"]
    assert original["pulled"] == "y" and manifest.DUPLICATE_ID_NOTE in original["notes"]
    assert by_path[twin]["pulled"] == "n" and by_path[twin]["sha256"] == "" and by_path[twin]["duplicate_of"] == ""
    assert f"{manifest.DUPLICATE_ID_NOTE} (2 entries)" in by_path[twin]["notes"]
    text = (workspace["qa"] / "inventory_anomalies.md").read_text(encoding="utf-8")
    assert "Drive ids listed more than once: 1 (2 entries)" in text
    assert f"| c1 | {twin} | 777 | n |" in text


def test_ledger_lookup_rules() -> None:
    ledger = {"id:x": {"drive_id": "x", "rel_path": "A/a.pdf"}, "path:B/b.pdf": {"drive_id": "y", "rel_path": "B/b.pdf"},
              "path:C/c.pdf": {"rel_path": "C/c.pdf"}}
    assert manifest.ledger_lookup(ledger, "x", "A/a.pdf") is ledger["id:x"]
    assert manifest.ledger_lookup(ledger, "x", "A/renamed.pdf") is ledger["id:x"]  # unique id: a rename
    assert manifest.ledger_lookup(ledger, "x", "A/renamed.pdf", unique_id=False) is None
    assert manifest.ledger_lookup(ledger, "other", "B/b.pdf") is None  # path record belongs to another id
    assert manifest.ledger_lookup(ledger, "y", "B/b.pdf") is ledger["path:B/b.pdf"]
    assert manifest.ledger_lookup(ledger, "z", "C/c.pdf") is ledger["path:C/c.pdf"]  # hand-uploaded, no id


def test_renamed_file_keeps_pull_with_a_note(workspace: Dict[str, Path]) -> None:
    def mutate(data: dict) -> None:
        for rec in data["files"]:
            if rec["id"] == "c1":
                rec["rel_path"] = "Basis/App D/Appendix D - renamed.pdf"
                rec["title"] = "Appendix D - renamed.pdf"
    _rewrite_listing(workspace, mutate)
    rows = _build(workspace)
    assert rows["c1"]["pulled"] == "y" and "moved or renamed since fetch" in rows["c1"]["notes"]


def test_summary_of_empty_manifest_succeeds(workspace: Dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    _rewrite_listing(workspace, lambda data: data.update(files=[]))
    assert manifest.main(_args(workspace) + ["build"]) == 0
    assert manifest.main(_args(workspace) + ["summary"]) == 0
    assert '"files": 0' in capsys.readouterr().out
    assert manifest.summary(workspace["registers"]) == {
        "files": 0, "by_root": {}, "by_stage": {}, "by_doc_class": {}, "pulled": 0, "duplicates": 0,
        "zips": 0, "departments": 0}
    (workspace["registers"] / "manifest.csv").unlink()
    with pytest.raises(FileNotFoundError):
        manifest.summary(workspace["registers"])


def test_rclone_transport_blocks_nothing(workspace: Dict[str, Path]) -> None:
    assert manifest.main(_args(workspace) + ["--transport", "rclone", "build"]) == 0
    rows = {r["drive_id"]: r for r in read_csv_dicts(workspace["registers"] / "manifest.csv")}
    assert "blocked: connector limit" not in rows["a5"]["notes"]
    assert manifest.OVER_LIMIT_NOTE in rows["a5"]["notes"] and rows["a5"]["pulled"] == "n"
    text = (workspace["qa"] / "inventory_anomalies.md").read_text(encoding="utf-8")
    assert "Transport: rclone. Blocked files: 0, total 0" in text
    assert "rclone has no size limit: 1 unpulled files over the connector limit (20,000,000" in text
    progress = Progress(workspace["state"] / "progress.json").load()["items"]["manifest"]
    assert progress["transport"] == "rclone"
    # the configured default (connector) still blocks
    assert manifest.main(_args(workspace) + ["build"]) == 0
    rows = {r["drive_id"]: r for r in read_csv_dicts(workspace["registers"] / "manifest.csv")}
    assert "blocked: connector limit" in rows["a5"]["notes"]
    assert manifest.configured_transport() == "connector"
    with pytest.raises(ValueError):
        manifest.configured_transport("ftp")
