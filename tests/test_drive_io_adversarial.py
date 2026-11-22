"""Adversarial tests for src.drive_io written by an independent reviewer.

Every test runs against a throw-away project tree (``AIGH_ROOT`` -> ``tmp_path``)
built from the synthetic fixtures under tests/fixtures/drive_io; the real
``data/source`` and ``state`` are never touched.

Tests marked ``xfail(strict=True)`` document confirmed defects: they assert the
behaviour the specification asks for, fail today, and will flip to an
unexpected pass (and so fail the suite) once the code is fixed, which is the
signal to remove the marker.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from src import common, drive_io

REAL_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "drive_io"
BLOBS = FIXTURES / "blobs"
LISTED_A = "2026-11-22T09:36:31Z"
LISTED_B = "2026-11-22T09:51:31Z"


# --------------------------------------------------------------------------- #
# Helpers and fixtures
# --------------------------------------------------------------------------- #
def fixture_rows() -> List[Dict[str, Any]]:
    """The entries of listing_a.jsonl as dictionaries (stale duplicate line included)."""
    return [json.loads(line) for line in (FIXTURES / "listing_a.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]


def file_entry(fid: str, rel_path: str, size: int, md5: Optional[str] = None, listed_at: str = LISTED_A,
               mime: str = "application/octet-stream", modified: str = "2026-11-20T02:00:00Z") -> Dict[str, Any]:
    """A minimal raw listing entry in the shared JSONL shape."""
    entry: Dict[str, Any] = {"id": fid, "title": rel_path.rsplit("/", 1)[-1], "mimeType": mime, "fileSize": str(size),
                             "fileExtension": common.file_ext(rel_path), "modifiedTime": modified,
                             "rel_path": rel_path, "listed_at": listed_at}
    if md5:
        entry["md5Checksum"] = md5
    return entry


def write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def run(*argv: str) -> int:
    return drive_io.main(list(argv))


def plan_by_id(root: Path) -> Dict[str, Dict[str, str]]:
    return {r["drive_id"]: r for r in common.read_csv_dicts(root / "state" / "pull_plan.csv")}


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fresh project tree with listing_a loaded as state/drive_listing/a.jsonl; connector transport forced."""
    (tmp_path / "config").mkdir()
    shutil.copy(REAL_ROOT / "config" / "project.yaml", tmp_path / "config" / "project.yaml")
    monkeypatch.setenv("AIGH_ROOT", str(tmp_path))
    write_jsonl(tmp_path / "state" / "drive_listing" / "a.jsonl", fixture_rows())
    monkeypatch.setattr(drive_io, "detect_transport", lambda: "connector")
    yield tmp_path
    monkeypatch.setenv("AIGH_ROOT", str(tmp_path))
    if (tmp_path / "data" / "source").exists():
        drive_io.unlock_source()
    # anything written outside data/source by a traversal test must be deletable too
    for stray in tmp_path.rglob("*"):
        try:
            subprocess.run(["chattr", "-i", str(stray)], capture_output=True)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# list: empty, malformed, duplicate and changed inputs
# --------------------------------------------------------------------------- #
def test_list_with_no_jsonl_files_fails_without_writing(project: Path) -> None:
    (project / "state" / "drive_listing" / "a.jsonl").unlink()
    assert run("list") == 1
    assert not (project / "state" / "listings").exists()
    assert not (project / "state" / "unstable_departments.json").exists()


def test_list_drops_entries_without_id_and_fails_on_malformed_line(project: Path) -> None:
    rows = fixture_rows()
    rows.append({"title": "no id.pdf", "mimeType": "application/pdf", "fileSize": "10", "rel_path": "Basis/no id.pdf",
                 "listed_at": LISTED_A})
    write_jsonl(project / "state" / "drive_listing" / "a.jsonl", rows)
    assert run("list") == 0
    listing = drive_io.latest_listing()
    assert listing is not None
    assert all(f.get("id") for f in listing["files"]) and len(listing["files"]) == 6
    # A malformed line is a hard error (exit 1), not a silently truncated listing.
    with (project / "state" / "drive_listing" / "a.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    assert run("list") == 1


def test_list_changed_filesize_alone_marks_department_unstable(project: Path) -> None:
    assert run("list") == 0
    rows = [dict(r, listed_at=LISTED_B) for r in fixture_rows()]
    for row in rows:
        if row["id"] == "fid_twin2":
            row["fileSize"] = "2161"  # same id, same modifiedTime, one byte more
    write_jsonl(project / "state" / "drive_listing" / "a.jsonl", rows)
    assert run("list") == 0
    report = json.loads((project / "state" / "unstable_departments.json").read_text(encoding="utf-8"))
    assert report["unstable"] == ["26 - Diagnostic Imaging"]
    assert report["stable"] == ["30 - Pharmacy"]


def test_list_duplicate_id_across_two_jsonl_files_keeps_latest(project: Path) -> None:
    moved = file_entry("fid_mot", "Basis/MOT NSC/Corrigendum 3/mot_nsc_c2.pdf", 450,
                       md5="85fb7571d6a7f198b3c409e04bef5a3a", listed_at=LISTED_B, mime="application/pdf")
    write_jsonl(project / "state" / "drive_listing" / "b.jsonl", [moved])
    assert run("list") == 0
    listing = drive_io.latest_listing()
    assert listing is not None
    mot = [f for f in listing["files"] if f["id"] == "fid_mot"]
    assert len(mot) == 1 and mot[0]["rel_path"] == "Basis/MOT NSC/Corrigendum 3/mot_nsc_c2.pdf"
    assert len(listing["files"]) == 6


@pytest.mark.xfail(strict=True, reason="consolidation merges every JSONL by id, so a file that disappeared "
                                       "from the second capture survives from the first and its department "
                                       "is reported stable")
def test_list_file_missing_from_second_capture_is_unstable(project: Path) -> None:
    assert run("list") == 0
    second = [dict(r, listed_at=LISTED_B) for r in fixture_rows() if r["id"] != "fid_twin2"]
    write_jsonl(project / "state" / "drive_listing" / "b.jsonl", second)
    assert run("list") == 0
    report = json.loads((project / "state" / "unstable_departments.json").read_text(encoding="utf-8"))
    assert "26 - Diagnostic Imaging" in report["unstable"]


@pytest.mark.xfail(strict=True, reason="taken_at is the wall clock of the `list` run, not the listed_at of "
                                       "the capture, so minutes_apart does not measure the listing interval")
def test_list_minutes_apart_reflects_capture_times(project: Path) -> None:
    assert run("list") == 0
    rows = [dict(r, listed_at=LISTED_B) for r in fixture_rows()]
    for row in rows:
        if row["id"] == "fid_twin1":
            row["modifiedTime"] = "2026-11-22T09:45:00Z"
    write_jsonl(project / "state" / "drive_listing" / "a.jsonl", rows)
    assert run("list") == 0
    report = json.loads((project / "state" / "unstable_departments.json").read_text(encoding="utf-8"))
    assert report["minutes_apart"] is not None and report["minutes_apart"] >= 14.0


# --------------------------------------------------------------------------- #
# pull plan edge cases
# --------------------------------------------------------------------------- #
def test_pull_plan_when_nothing_is_fetchable(project: Path, capsys: pytest.CaptureFixture) -> None:
    rows = [file_entry("z1", "DD Sign-OFF/42 - Data Centre/Submitted/dc.ZIP", 10, mime="application/zip"),
            file_entry("b1", "Basis/App D/huge.pdf", drive_io.CONNECTOR_LIMIT + 1, mime="application/pdf"),
            file_entry("e1", "Basis/App D/exactly_limit.pdf", drive_io.CONNECTOR_LIMIT, mime="application/pdf")]
    gdoc = file_entry("g1", "Template/Native doc", 0, mime="application/vnd.google-apps.document")
    del gdoc["fileSize"]  # Google-native documents report no size
    rows.append(gdoc)
    write_jsonl(project / "state" / "drive_listing" / "a.jsonl", rows)
    assert run("list") == 0 and run("pull") == 0
    plan = plan_by_id(project)
    assert plan["z1"]["status"] == "skipped_zip"  # upper-case extension still a zip
    assert plan["b1"]["status"] == "blocked_connector_limit"
    assert plan["e1"]["status"] == "pending" and plan["e1"]["transport_eligible"] == "y"  # boundary inclusive
    assert plan["g1"]["status"] == "pending" and plan["g1"]["bytes"] == "0"
    out = capsys.readouterr().out
    assert "2 pending, 1 blocked" in out and "INSTRUCTION (connector mode)" in out


@pytest.mark.xfail(strict=True, reason="plan_status applies the connector size limit in rclone mode too: a "
                                       "large file the rclone copy did not deliver is reported "
                                       "blocked_connector_limit instead of pending")
def test_rclone_pull_plan_never_reports_connector_limit(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(argv: Any, cwd: Any = None, timeout: Any = None) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(list(argv), 0, "", "")  # copy delivers nothing

    monkeypatch.setattr(drive_io, "_run", _fake)
    assert run("list") == 0
    assert run("--transport", "rclone", "pull") == 0
    big = plan_by_id(project)["fid_big"]
    assert big["transport_eligible"] == "y"
    assert big["status"] == "pending"


# --------------------------------------------------------------------------- #
# ingest: unicode, traversal, --force with a bad blob, self-move, md5 mismatch
# --------------------------------------------------------------------------- #
def test_ingest_unicode_and_spaces_in_path_round_trips(project: Path, tmp_path: Path) -> None:
    data = b"%PDF-1.4 r\xc3\xa9vis\xc3\xa9\n" * 7
    rel = "DD Sign-OFF/26 - Diagnostic Imaging/AH sign off with comments/02_Plans/Plan – Ebene 2 (révisé) 平面图.pdf"
    rows = fixture_rows() + [file_entry("fid_uni", rel, len(data), md5=md5_bytes(data), mime="application/pdf")]
    write_jsonl(project / "state" / "drive_listing" / "a.jsonl", rows)
    blob = tmp_path / "scratch" / "download (1) é.pdf"
    blob.parent.mkdir()
    blob.write_bytes(data)
    assert run("list") == 0 and run("pull") == 0
    assert run("ingest", "--blob", str(blob), "--drive-id", "fid_uni", "--move") == 0
    dest = project / "data" / "source" / rel
    assert dest.is_file() and dest.read_bytes() == data and not blob.exists()
    row = drive_io.ledger_by_id()["fid_uni"]
    assert row["rel_path"] == rel and row["md5_match"] is True and row["size_match"] is True
    assert plan_by_id(project)["fid_uni"]["status"] == "fetched"
    assert run("verify") == 0
    # The CSV plan survives a unicode round trip.
    assert any(r["rel_path"] == rel for r in common.read_csv_dicts(project / "state" / "pull_plan.csv"))


@pytest.mark.xfail(strict=True, reason="rel_path from the listing is joined to data/source without "
                                       "normalisation, so a '..' segment writes outside the source tree")
def test_ingest_refuses_rel_path_that_escapes_source(project: Path, tmp_path: Path) -> None:
    data = b"escape"
    rows = fixture_rows() + [file_entry("fid_trav", "Basis/../../escaped.txt", len(data), md5=md5_bytes(data))]
    write_jsonl(project / "state" / "drive_listing" / "a.jsonl", rows)
    blob = tmp_path / "escaped.txt"
    blob.write_bytes(data)
    assert run("list") == 0
    code = run("ingest", "--blob", str(blob), "--drive-id", "fid_trav")
    outside = [p for p in project.rglob("escaped.txt") if (project / "data" / "source") not in p.parents]
    assert code != 0 and outside == []


@pytest.mark.xfail(strict=True, reason="--force supersedes (renames away) the existing good copy before the "
                                       "size check; when the new blob is the wrong size it is deleted and "
                                       "rel_path is left with no file while the ledger still points at it")
def test_ingest_force_with_wrong_size_blob_keeps_original_in_place(project: Path) -> None:
    assert run("list") == 0
    assert run("ingest", "--blob", str(BLOBS / "sample_calc.xlsx"), "--drive-id", "fid_tpl") == 0
    dest = project / "data" / "source" / "Template" / "sample_calc.xlsx"
    code = run("ingest", "--blob", str(BLOBS / "wrong_size.bin"), "--drive-id", "fid_tpl", "--force")
    assert code != 0
    assert dest.is_file() and dest.read_bytes() == (BLOBS / "sample_calc.xlsx").read_bytes()
    assert run("verify") == 0


@pytest.mark.xfail(strict=True, reason="ingest --move with a blob that already is the file under data/source "
                                       "takes the 'already present' branch and unlinks the blob, i.e. the "
                                       "record document; only an enforced chattr lock prevents it")
def test_ingest_move_of_blob_already_inside_source_never_deletes_it(project: Path) -> None:
    assert run("list") == 0
    assert run("ingest", "--blob", str(BLOBS / "mot_nsc_c2.pdf"), "--drive-id", "fid_mot") == 0
    dest = project / "data" / "source" / "Basis" / "MOT NSC" / "Corrigendum 2" / "Annex B" / "mot_nsc_c2.pdf"
    assert run("unlock") == 0  # chmod fallback or an unlocked tree: nothing stops the unlink
    run("ingest", "--blob", str(dest), "--drive-id", "fid_mot", "--move")
    assert dest.is_file()


@pytest.mark.xfail(strict=True, reason="an md5-mismatched ingest appends a ledger row, and build_pull_plan "
                                       "treats any ledger row as fetched, so the next `pull` flips the row "
                                       "from pending to fetched despite the mismatch")
def test_md5_mismatch_row_stays_pending_after_pull(project: Path, tmp_path: Path) -> None:
    assert run("list") == 0 and run("pull") == 0
    other = tmp_path / "twin_other.xlsx"
    other.write_bytes(b"y" * 2160)  # right size, wrong content
    assert run("ingest", "--blob", str(other), "--drive-id", "fid_twin1") == 3
    assert plan_by_id(project)["fid_twin1"]["status"] == "pending"
    assert run("pull") == 0
    assert plan_by_id(project)["fid_twin1"]["status"] == "pending"


def test_ingest_blob_that_is_a_directory_is_refused(project: Path, tmp_path: Path) -> None:
    assert run("list") == 0
    folder = tmp_path / "a_directory"
    folder.mkdir()
    assert run("ingest", "--blob", str(folder), "--drive-id", "fid_tpl") == 1
    assert not (project / "data" / "source" / "Template" / "sample_calc.xlsx").exists()


def test_ingest_dir_empty_map_and_blob_names_with_spaces(project: Path, tmp_path: Path) -> None:
    assert run("list") == 0
    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    assert run("ingest-dir", "--dir", str(tmp_path), "--map", str(empty)) == 1
    scratch = tmp_path / "batch"
    scratch.mkdir()
    shutil.copy(BLOBS / "sample_calc.xlsx", scratch / "my download (1).xlsx")
    shutil.copy(BLOBS / "mot_nsc_c2.pdf", scratch / "MOT NSC C2 – copy.pdf")
    mapping = tmp_path / "map.csv"
    mapping.write_text("blob,drive_id\nmy download (1).xlsx,fid_tpl\nMOT NSC C2 – copy.pdf,fid_mot\n",
                       encoding="utf-8")
    assert run("ingest-dir", "--dir", str(scratch), "--map", str(mapping)) == 0
    assert set(drive_io.ledger_by_id()) == {"fid_tpl", "fid_mot"}
    # Re-running the same batch is idempotent: no new ledger rows.
    assert run("ingest-dir", "--dir", str(scratch), "--map", str(mapping)) == 0
    assert len(common.read_jsonl(project / "state" / "pull_ledger.jsonl")) == 2


# --------------------------------------------------------------------------- #
# verify against a listing whose Drive metadata moved on
# --------------------------------------------------------------------------- #
def test_verify_flags_drive_size_change_after_fetch(project: Path, capsys: pytest.CaptureFixture) -> None:
    assert run("list") == 0
    assert run("ingest", "--blob", str(BLOBS / "sample_calc.xlsx"), "--drive-id", "fid_tpl") == 0
    assert run("verify") == 0
    rows = [dict(r, listed_at=LISTED_B) for r in fixture_rows()]
    for row in rows:
        if row["id"] == "fid_tpl":
            row["fileSize"] = "218"
    write_jsonl(project / "state" / "drive_listing" / "a.jsonl", rows)
    assert run("list") == 0
    assert run("verify") == 1
    assert "size differs from Drive" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# rclone listing: the hash key rclone really emits
# --------------------------------------------------------------------------- #
@pytest.mark.xfail(strict=True, reason="rclone lsjson --hash emits Hashes.MD5 (upper case); the mapper reads "
                                       "Hashes.md5, so md5Checksum is never populated in rclone mode")
def test_rclone_listing_maps_real_md5_hash_key(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(argv: Any, cwd: Any = None, timeout: Any = None) -> subprocess.CompletedProcess:
        stdout = ""
        if argv[:2] == ["rclone", "lsjson"]:
            stdout = json.dumps([{"Path": "App D/x.pdf", "Name": "x.pdf", "Size": 3, "MimeType": "application/pdf",
                                  "ModTime": "2026-11-20T02:00:00Z", "ID": "rid1",
                                  "Hashes": {"MD5": "b1946ac92492d2347c6235b4d2611184", "SHA-1": "f572d3"}}])
        return subprocess.CompletedProcess(list(argv), 0, stdout, "")

    monkeypatch.setattr(drive_io, "_run", _fake)
    assert run("--transport", "rclone", "list") == 0
    listing = drive_io.latest_listing()
    assert listing is not None
    assert listing["files"][0].get("md5Checksum") == "b1946ac92492d2347c6235b4d2611184"


# --------------------------------------------------------------------------- #
# push-markdown with nothing to push; lock state honesty
# --------------------------------------------------------------------------- #
def test_push_markdown_without_markdown_dir_writes_empty_plan(project: Path) -> None:
    assert run("push-markdown") == 0
    plan = project / "state" / "push_plan.csv"
    assert plan.is_file()
    assert plan.read_text(encoding="utf-8").strip() == ",".join(drive_io.PUSH_PLAN_COLUMNS)
    assert common.read_csv_dicts(plan) == []


def test_lock_with_nested_unicode_dirs_round_trips(project: Path) -> None:
    nested = project / "data" / "source" / "DD Sign-OFF" / "26 - Diagnostic Imaging" / "Pläne 平面"
    nested.mkdir(parents=True)
    (nested / "a b.pdf").write_bytes(b"x")
    assert run("lock") == 0
    state = json.loads((project / "state" / "source_lock.json").read_text(encoding="utf-8"))
    assert state["locked"] is True and isinstance(state["enforced"], bool)
    if state["enforced"]:
        with pytest.raises(OSError):
            (nested / "new.txt").write_bytes(b"y")
    assert run("unlock") == 0
    (nested / "new.txt").write_bytes(b"y")
    assert (nested / "new.txt").is_file()
    assert not (project / "data" / "source" / ".lock_probe").exists()
