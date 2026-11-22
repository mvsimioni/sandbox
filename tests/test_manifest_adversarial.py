"""Adversarial tests for src.manifest written by the independent reviewer.

Every workspace here is synthetic and built in ``tmp_path``; the only file
fixture is ``tests/fixtures/manifest_adversarial/listing_unicode.json``.
Tests marked ``xfail(strict=True)`` document behaviour the reviewer judged
wrong against the specification; they turn into hard failures (XPASS) the
moment the code is fixed, so the marker must then be removed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pytest

from src import manifest
from src.common import read_csv_dicts

FIXTURES = Path(__file__).parent / "fixtures" / "manifest_adversarial"
LISTING_NAME = "listing_20261122T120000Z.json"
PLAN_HEADER = "drive_id,rel_path,bytes,mime_type,transport_eligible,status\n"


def _file(drive_id: str, rel_path: str, size: Optional[str] = "1000", **extra: Any) -> Dict[str, Any]:
    """A minimal Drive file record for a synthetic listing."""
    rec: Dict[str, Any] = {
        "id": drive_id, "title": rel_path.split("/")[-1], "mimeType": "application/pdf",
        "fileSize": size, "fileExtension": "pdf", "modifiedTime": "2026-11-21T00:00:00Z",
        "createdTime": "2026-11-20T00:00:00Z", "parentId": "p", "owner": "owner@example.com",
        "etag": "e", "rel_path": rel_path, "listed_at": "2026-11-22T12:00:00Z",
    }
    rec.update(extra)
    return rec


def _ledger(drive_id: str, rel_path: str, sha: str, size: int = 1000, **extra: Any) -> Dict[str, Any]:
    """A minimal pull-ledger record."""
    rec: Dict[str, Any] = {
        "drive_id": drive_id, "rel_path": rel_path, "local_path": "data/source/" + rel_path, "bytes": size,
        "sha256": sha * 64, "md5": "m" * 32, "drive_md5": None, "size_match": True, "md5_match": None,
        "fetched_at": "2026-11-22T12:00:00Z", "transport": "connector",
    }
    rec.update(extra)
    return rec


class Workspace:
    """A throw-away project tree with a listing, optional ledger and plan."""

    def __init__(self, root: Path, files: Iterable[Dict[str, Any]], folders: Iterable[Dict[str, Any]] = (),
                 ledger: Iterable[Dict[str, Any]] = (), plan: Optional[str] = None, listing_json: Optional[str] = None,
                 local_files: bool = True) -> None:
        self.root = root
        self.state = root / "state"
        self.registers = root / "out" / "registers"
        self.qa = root / "out" / "qa"
        listings = self.state / "listings"
        listings.mkdir(parents=True)
        text = listing_json if listing_json is not None else json.dumps(
            {"taken_at": "2026-11-22T12:00:00Z", "files": list(files), "folders": list(folders)}, ensure_ascii=False)
        (listings / LISTING_NAME).write_text(text, encoding="utf-8")
        (listings / "latest").symlink_to(listings / LISTING_NAME)
        with (self.state / "pull_ledger.jsonl").open("w", encoding="utf-8") as handle:
            for rec in ledger:
                handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if local_files:
                    local = root / rec["local_path"]
                    local.parent.mkdir(parents=True, exist_ok=True)
                    local.write_bytes(b"x" * int(rec["bytes"]))
        if plan is not None:
            (self.state / "pull_plan.csv").write_text(plan, encoding="utf-8")

    def args(self) -> List[str]:
        return ["--state-dir", str(self.state), "--registers-dir", str(self.registers),
                "--qa-dir", str(self.qa), "--project-root", str(self.root)]

    def build(self) -> int:
        return manifest.main(self.args() + ["build"])

    def rows(self) -> Dict[str, Dict[str, str]]:
        return {r["drive_id"]: r for r in read_csv_dicts(self.registers / "manifest.csv")}

    def rows_by_path(self) -> Dict[str, Dict[str, str]]:
        return {r["rel_path"]: r for r in read_csv_dicts(self.registers / "manifest.csv")}

    def report(self) -> str:
        return (self.qa / "inventory_anomalies.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Empty and malformed inputs
# --------------------------------------------------------------------------- #
def test_empty_listing_builds_header_only_registers_and_full_report(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, files=[])
    assert ws.build() == 0
    manifest_text = (ws.registers / "manifest.csv").read_text(encoding="utf-8")
    assert manifest_text.strip() == ",".join(manifest.MANIFEST_COLUMNS)
    zips_text = (ws.registers / "skipped_zip.csv").read_text(encoding="utf-8")
    assert zips_text.strip() == ",".join(manifest.SKIPPED_ZIP_COLUMNS)
    text = ws.report()
    for n in range(1, 10):
        assert f"## {n}." in text, n
    assert "Numbered department folders found: 0 (expected 50" in text
    assert "No stray folders in this listing." in text


@pytest.mark.xfail(strict=True, reason="summary treats an empty (header-only) manifest as missing and exits 1")
def test_summary_of_an_empty_manifest_succeeds(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, files=[])
    assert ws.build() == 0
    assert (ws.registers / "manifest.csv").is_file()
    assert manifest.main(ws.args() + ["summary"]) == 0


def test_corrupt_ledger_line_fails_loudly_without_writing_outputs(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, files=[_file("x1", "DD Sign-OFF/30 - Pharmacy/Submitted/a.pdf")])
    (ws.state / "pull_ledger.jsonl").write_text('{"drive_id": "x1", \n', encoding="utf-8")
    assert ws.build() == 1
    assert not (ws.registers / "manifest.csv").exists()
    assert not (ws.qa / "inventory_anomalies.md").exists()
    assert not (ws.state / "progress.json").exists()


def test_listing_that_is_not_an_object_fails_with_exit_one(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, files=[], listing_json="[1, 2, 3]")
    assert ws.build() == 1
    assert not (ws.registers / "manifest.csv").exists()


def test_non_dict_ledger_lines_and_blank_lines_are_ignored(tmp_path: Path) -> None:
    rel = "DD Sign-OFF/30 - Pharmacy/Submitted/a.pdf"
    ws = Workspace(tmp_path, files=[_file("x1", rel)], ledger=[_ledger("x1", rel, "a")])
    with (ws.state / "pull_ledger.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('\n["not", "a", "record"]\n\n"string"\n')
    assert ws.build() == 0
    assert ws.rows()["x1"]["pulled"] == "y"


# --------------------------------------------------------------------------- #
# Unicode, punctuation and spaces in paths
# --------------------------------------------------------------------------- #
def test_unicode_quotes_commas_and_pipes_round_trip(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, files=[], listing_json=(FIXTURES / "listing_unicode.json").read_text(encoding="utf-8"))
    assert ws.build() == 0
    rows = ws.rows()
    assert len(rows) == 4
    title = '14_Déjà vu – plan, "final" | v2 .pdf'
    assert rows["u1"]["rel_path"].endswith("/" + title)
    assert rows["u1"]["dept_alias"] == "14" and rows["u2"]["dept_alias"] == "14"
    assert rows["u1"]["dept_no"] == "26" and rows["u1"]["dept_name"] == "Diagnostic Imaging"
    assert rows["u1"]["stage"] == "Submitted" and rows["u2"]["stage"] == "DD Closure"
    assert rows["u1"]["doc_class"] == "pdf document"
    # unpulled twins with the same title, size and md5: DD Closure sorts first and is the original
    assert rows["u2"]["duplicate_of"] == ""
    assert rows["u1"]["duplicate_of"] == rows["u2"]["rel_path"]
    assert manifest.PROBABLE_DUP_NOTE in rows["u1"]["notes"]
    # the " (2)" twin zip with a non-ASCII stem is matched to its original
    text = ws.report()
    assert "Ünïcödé (2).zip | 300 | DD Sign-OFF/26 - Diagnostic Imaging/Submitted/Ünïcödé.zip | 300 | same size |" in text
    # the alias row carries the full name; the pipe is escaped so the table cell survives
    assert '| 26 | 14 | DD Sign-OFF/26 - Diagnostic Imaging/Submitted/01_Layouts/14_Déjà vu – plan, "final" \\| v2 .pdf |' in text
    zips = {r["rel_path"]: r for r in read_csv_dicts(ws.registers / "skipped_zip.csv")}
    assert zips["DD Sign-OFF/26 - Diagnostic Imaging/Submitted/Ünïcödé (2).zip"]["nested"] == "y"


def test_rebuild_replaces_stale_outputs_rather_than_appending(tmp_path: Path) -> None:
    rel = "DD Sign-OFF/30 - Pharmacy/Submitted/a.pdf"
    ws = Workspace(tmp_path, files=[_file("x1", rel)])
    ws.registers.mkdir(parents=True)
    (ws.registers / "manifest.csv").write_text("stale,garbage\n1,2\n3,4\n", encoding="utf-8")
    (ws.qa).mkdir(parents=True)
    (ws.qa / "inventory_anomalies.md").write_text("STALE REPORT\n", encoding="utf-8")
    assert ws.build() == 0
    assert list(ws.rows()) == ["x1"]
    assert "STALE" not in ws.report()
    first = (ws.registers / "manifest.csv").read_bytes()
    assert ws.build() == 0
    assert (ws.registers / "manifest.csv").read_bytes() == first


# --------------------------------------------------------------------------- #
# Duplicate ids and changed sizes
# --------------------------------------------------------------------------- #
def test_duplicate_drive_ids_keep_both_rows(tmp_path: Path) -> None:
    rel_a = "DD Sign-OFF/30 - Pharmacy/Submitted/a.pdf"
    rel_b = "DD Sign-OFF/30 - Pharmacy/DD Closure/b.pdf"
    ws = Workspace(tmp_path, files=[_file("dup", rel_a), _file("dup", rel_b, size="2000")],
                   ledger=[_ledger("dup", rel_a, "b")])
    assert ws.build() == 0
    rows = ws.rows_by_path()
    assert set(rows) == {rel_a, rel_b}
    assert rows[rel_a]["pulled"] == "y" and rows[rel_a]["sha256"] == "b" * 64


@pytest.mark.xfail(strict=True, reason="a second listing entry with the same drive id inherits the ledger record "
                                       "of the first (pulled=y, same sha256) although its size differs; no note")
def test_duplicate_drive_id_with_different_size_is_not_marked_pulled(tmp_path: Path) -> None:
    rel_a = "DD Sign-OFF/30 - Pharmacy/Submitted/a.pdf"
    rel_b = "DD Sign-OFF/30 - Pharmacy/DD Closure/b.pdf"
    ws = Workspace(tmp_path, files=[_file("dup", rel_a), _file("dup", rel_b, size="2000")],
                   ledger=[_ledger("dup", rel_a, "b")])
    assert ws.build() == 0
    row = ws.rows_by_path()[rel_b]
    assert row["pulled"] == "n" or "duplicate drive id" in row["notes"].lower()


@pytest.mark.xfail(strict=True, reason="listing fileSize/md5Checksum changed since the pull; the row still says "
                                       "pulled=y with the stale sha256 and carries no stale/size note")
def test_changed_file_size_since_pull_is_flagged(tmp_path: Path) -> None:
    rel = "DD Sign-OFF/30 - Pharmacy/Submitted/a.pdf"
    ws = Workspace(tmp_path, files=[_file("x1", rel, size="9999", md5Checksum="newmd5")],
                   ledger=[_ledger("x1", rel, "a", size=1000, drive_md5="oldmd5", md5="oldmd5", md5_match=True)])
    assert ws.build() == 0
    row = ws.rows()["x1"]
    assert row["bytes"] == "9999"
    notes = row["notes"].lower()
    assert "stale" in notes or "size" in notes or row["pulled"] == "n"


# --------------------------------------------------------------------------- #
# Connector limit and zips
# --------------------------------------------------------------------------- #
@pytest.mark.xfail(strict=True, reason="once the pull plan has any blocked entry, an oversize file absent from the "
                                       "plan (added to Drive after planning) is not flagged")
def test_oversize_file_missing_from_plan_is_still_blocked(tmp_path: Path) -> None:
    rel1 = "DD Sign-OFF/30 - Pharmacy/Submitted/big.pdf"
    rel2 = "DD Sign-OFF/30 - Pharmacy/Submitted/big2.pdf"
    plan = PLAN_HEADER + f"x1,{rel1},30000000,application/pdf,n,blocked_connector_limit\n"
    ws = Workspace(tmp_path, files=[_file("x1", rel1, size="30000000"), _file("x2", rel2, size="30000000")], plan=plan)
    assert ws.build() == 0
    rows = ws.rows()
    assert "blocked: connector limit" in rows["x1"]["notes"]
    assert "blocked: connector limit" in rows["x2"]["notes"]


def test_blocked_section_aggregates_by_root_and_department(tmp_path: Path) -> None:
    rel1 = "DD Sign-OFF/30 - Pharmacy/Submitted/big.pdf"
    rel2 = "Basis/App D/huge.pdf"
    ws = Workspace(tmp_path, files=[_file("x1", rel1, size="11000000"), _file("x2", rel2, size="12000000"),
                                    _file("x3", "Basis/App D/small.pdf", size="12")])
    assert ws.build() == 0
    text = ws.report()
    assert "Blocked files: 2, total 23,000,000" in text
    assert "| Basis |  |  | 1 | 12,000,000" in text
    assert "| DD Sign-OFF | 30 | Pharmacy | 1 | 11,000,000" in text


def test_zip_present_in_ledger_is_still_reported_unpulled(tmp_path: Path) -> None:
    rel = "DD Sign-OFF/30 - Pharmacy/x.zip"
    ws = Workspace(tmp_path, files=[_file("z", rel, mimeType="application/zip", fileExtension="zip")],
                   ledger=[_ledger("z", rel, "c", transport="rclone")])
    assert ws.build() == 0
    row = ws.rows()["z"]
    assert row["doc_class"] == "zip" and row["pulled"] == "n" and row["sha256"] == ""
    zips = read_csv_dicts(ws.registers / "skipped_zip.csv")
    assert len(zips) == 1 and zips[0]["nested"] == "n" and zips[0]["parent_folder"] == "DD Sign-OFF/30 - Pharmacy"


def test_zip_nesting_rules_for_odd_locations() -> None:
    assert manifest.zip_is_nested("DD Sign-OFF/Archive.zip") is False
    assert manifest.zip_is_nested("DD Sign-OFF/30 - Pharmacy/x.zip") is False
    assert manifest.zip_is_nested("DD Sign-OFF/__29 - Lab/x.zip") is False  # a stray is still a department folder
    assert manifest.zip_is_nested("DD Sign-OFF/Misc notes/x.zip") is True  # not a department folder
    assert manifest.zip_is_nested("DD Sign-OFF/30 - Pharmacy/Submitted/x.zip") is True
    assert manifest.zip_is_nested("Template/x.zip") is True
    assert manifest.zip_is_nested("") is True


# --------------------------------------------------------------------------- #
# Alias numbers, Google-native files and sizes
# --------------------------------------------------------------------------- #
def test_alias_edge_cases() -> None:
    assert manifest.dept_alias("123_x.pdf", "30") == ""  # three digits is not a department number
    assert manifest.dept_alias("3D model.pdf", "30") == ""
    assert manifest.dept_alias("7x.pdf", "30") == ""
    assert manifest.dept_alias("47-AN Hub.xlsx", "30") == "47"
    assert manifest.dept_alias("47 AN Hub.xlsx", "30") == "47"
    assert manifest.dept_alias("  7 - x.pdf", "30") == "7"
    assert manifest.dept_alias("30_x.pdf", "30") == ""
    assert manifest.dept_alias("47_x.pdf", "") == ""  # no folder number to differ from


def test_google_native_document_and_garbage_size(tmp_path: Path) -> None:
    files = [
        _file("g1", "DD Sign-OFF/30 - Pharmacy/Submitted/Sheet", size=None,
              mimeType="application/vnd.google-apps.spreadsheet", fileExtension=None),
        _file("g2", "DD Sign-OFF/30 - Pharmacy/Submitted/b.pdf", size="1e3"),
        _file("g3", "DD Sign-OFF/30 - Pharmacy/Submitted/c.pdf", size=" 42 "),
    ]
    ws = Workspace(tmp_path, files=files)
    assert ws.build() == 0
    rows = ws.rows()
    assert rows["g1"]["bytes"] == "0" and rows["g1"]["ext"] == "" and "Google-native" in rows["g1"]["notes"]
    assert rows["g1"]["pulled"] == "n" and rows["g1"]["duplicate_of"] == ""
    assert rows["g2"]["bytes"] == "0"  # unparseable size coerces to zero (silently; see review)
    assert rows["g3"]["bytes"] == "42"


# --------------------------------------------------------------------------- #
# Strays without a folder tree and two identical rel_paths
# --------------------------------------------------------------------------- #
def test_two_strays_with_identical_rel_paths_and_no_folder_records(tmp_path: Path) -> None:
    rel = "DD Sign-OFF/__29 - Lab/Submitted/29_Lab_RDS.pdf"
    files = [_file("s1", rel, parentId="unknown1"), _file("s2", rel, parentId="unknown2"),
             _file("b1", "DD Sign-OFF/29 - Lab/Submitted/29_Lab_RDS.pdf", parentId="unknown3")]
    ws = Workspace(tmp_path, files=files, ledger=[_ledger("s1", rel, "d"), _ledger("s2", rel, "e")])
    assert ws.build() == 0
    rows = ws.rows()
    assert len(rows) == 3 and rows["s1"]["pulled"] == "y" and rows["s2"]["pulled"] == "y"
    assert rows["s1"]["duplicate_of"] == "" and rows["s2"]["duplicate_of"] == ""  # different sha256
    assert "stray folder" in rows["s1"]["notes"] and "stray folder" not in rows["b1"]["notes"]
    text = ws.report()
    assert "## 5. Stray folders compared with '29 - Lab'" in text
    assert "Stray folders found: 0" in text  # no folder records: nothing to compare, but no crash
    assert "No stray folders in this listing." in text


def test_latest_pointer_variants(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, files=[_file("x1", "Basis/a.pdf")])
    listings = ws.state / "listings"
    (listings / "latest").unlink()
    # a dangling symlink falls back to the newest listing_*.json
    (listings / "latest").symlink_to(listings / "gone.json")
    assert manifest.resolve_listing_path(ws.state).name == LISTING_NAME
    (listings / "latest").unlink()
    # a directory named latest holding listings
    (listings / "latest").mkdir()
    (listings / "latest" / "listing_20261122T130000Z.json").write_text('{"taken_at": "", "files": [], "folders": []}')
    assert manifest.resolve_listing_path(ws.state).name == "listing_20261122T130000Z.json"
    # an explicit listing that does not exist is an error, not a silent fallback
    with pytest.raises(FileNotFoundError):
        manifest.resolve_listing_path(ws.state, ws.state / "nope.json")
    assert manifest.main(ws.args() + ["--listing", str(ws.state / "nope.json"), "build"]) == 1


def test_build_never_touches_data_source(tmp_path: Path) -> None:
    rel = "DD Sign-OFF/30 - Pharmacy/Submitted/a.pdf"
    ws = Workspace(tmp_path, files=[_file("x1", rel)], ledger=[_ledger("x1", rel, "a")])
    source = ws.root / "data" / "source"
    before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in source.rglob("*") if p.is_file()}
    assert before
    assert ws.build() == 0
    after = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in source.rglob("*") if p.is_file()}
    assert after == before
    assert ws.rows()["x1"]["notes"] == ""  # local file present: no "missing" note
