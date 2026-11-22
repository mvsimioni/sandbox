"""Tests for src.drive_io using synthetic fixtures under tests/fixtures/drive_io only.

Every test runs against a throw-away project tree (``AIGH_ROOT`` points at
``tmp_path``), so nothing under the real ``data/source`` or ``state`` is touched,
and the transport is forced to ``connector`` unless a test says otherwise.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src import common, drive_io

REAL_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "drive_io"
BLOBS = FIXTURES / "blobs"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh project tree under tmp_path with listing_a loaded, connector transport forced."""
    (tmp_path / "config").mkdir()
    shutil.copy(REAL_ROOT / "config" / "project.yaml", tmp_path / "config" / "project.yaml")
    monkeypatch.setenv("AIGH_ROOT", str(tmp_path))
    raw = tmp_path / "state" / "drive_listing"
    raw.mkdir(parents=True)
    shutil.copy(FIXTURES / "listing_a.jsonl", raw / "dd.jsonl")
    monkeypatch.setattr(drive_io, "detect_transport", lambda: "connector")
    yield tmp_path
    # leave the tree deletable whatever a test did to data/source
    monkeypatch.setenv("AIGH_ROOT", str(tmp_path))
    if (tmp_path / "data" / "source").exists():
        drive_io.unlock_source()


def run(*argv: str) -> int:
    """Run the command line and return its exit code."""
    return drive_io.main(list(argv))


def plan_by_id(root: Path) -> Dict[str, Dict[str, str]]:
    return {r["drive_id"]: r for r in common.read_csv_dicts(root / "state" / "pull_plan.csv")}


# --------------------------------------------------------------------------- #
# AIGH_ROOT override
# --------------------------------------------------------------------------- #
def test_aigh_root_overrides_paths(project: Path) -> None:
    assert common.project_root() == project.resolve()
    assert common.resolve_paths()["source"] == (project / "data" / "source").resolve()
    assert drive_io.state_dir() == (project / "state").resolve()


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #
def test_list_consolidates_dedupes_and_detects_unstable(project: Path, capsys: pytest.CaptureFixture) -> None:
    assert run("list") == 0
    listings = sorted((project / "state" / "listings").glob("listing_*.json"))
    assert len(listings) == 1
    first = json.loads(listings[0].read_text())
    assert set(first) >= {"taken_at", "files", "folders"}
    assert len(first["files"]) == 6 and len(first["folders"]) == 3
    tpl = [f for f in first["files"] if f["id"] == "fid_tpl"]
    assert tpl[0]["title"] == "sample_calc.xlsx"  # the newer listed_at wins over the stale duplicate line
    assert "only one consolidated listing" in capsys.readouterr().out

    # Unchanged inputs: idempotent, no second snapshot.
    assert run("list") == 0
    assert len(list((project / "state" / "listings").glob("listing_*.json"))) == 1

    # Second JSONL with department 30 changed -> new snapshot and comparison.
    shutil.copy(FIXTURES / "listing_b.jsonl", project / "state" / "drive_listing" / "dd.jsonl")
    assert run("list") == 0
    listings = sorted((project / "state" / "listings").glob("listing_*.json"))
    assert len(listings) == 2
    report = json.loads((project / "state" / "unstable_departments.json").read_text())
    assert report["compared"] == [listings[0].name, listings[1].name]
    assert isinstance(report["minutes_apart"], (int, float))
    assert report["unstable"] == ["30 - Pharmacy"]
    assert report["stable"] == ["26 - Diagnostic Imaging"]
    out = capsys.readouterr().out
    assert "1 unstable" in out and "unstable: 30 - Pharmacy" in out


def test_list_without_data_fails(project: Path) -> None:
    (project / "state" / "drive_listing" / "dd.jsonl").write_text("")
    assert run("list") == 1


# --------------------------------------------------------------------------- #
# pull plan
# --------------------------------------------------------------------------- #
def test_pull_requires_listing(project: Path) -> None:
    assert run("pull") == 1


def test_pull_plan_statuses_and_lock(project: Path, capsys: pytest.CaptureFixture) -> None:
    assert run("list") == 0
    assert run("pull") == 0
    plan = plan_by_id(project)
    assert list(common.read_csv_dicts(project / "state" / "pull_plan.csv")[0]) == list(drive_io.PLAN_COLUMNS)
    assert plan["fid_zip"]["status"] == "skipped_zip"
    assert plan["fid_big"]["status"] == "blocked_connector_limit" and plan["fid_big"]["transport_eligible"] == "n"
    for fid in ("fid_twin1", "fid_twin2", "fid_mot", "fid_tpl"):
        assert plan[fid]["status"] == "pending" and plan[fid]["transport_eligible"] == "y"
    assert plan["fid_mot"]["rel_path"] == "Basis/MOT NSC/Corrigendum 2/Annex B/mot_nsc_c2.pdf"
    out = capsys.readouterr().out
    assert "pending                       4" in out and "skipped_zip" in out and "INSTRUCTION (connector mode)" in out
    assert "DD Sign-OFF" in out and "Basis" in out and "Template" in out
    lock_state = json.loads((project / "state" / "source_lock.json").read_text())
    assert lock_state["locked"] is True
    # Re-running is idempotent: same plan.
    before = (project / "state" / "pull_plan.csv").read_text()
    assert run("pull") == 0
    assert (project / "state" / "pull_plan.csv").read_text() == before


# --------------------------------------------------------------------------- #
# ingest
# --------------------------------------------------------------------------- #
def test_ingest_success_twins_and_repeat(project: Path, tmp_path: Path) -> None:
    assert run("list") == 0 and run("pull") == 0
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    shutil.copy(BLOBS / "pharmacy_soa.xlsx", scratch / "pharmacy_soa.xlsx")
    shutil.copy(BLOBS / "di_soa.xlsx", scratch / "di_soa.xlsx")

    assert run("ingest", "--blob", str(scratch / "pharmacy_soa.xlsx"), "--drive-id", "fid_twin1", "--move") == 0
    dest = project / "data" / "source" / "DD Sign-OFF" / "30 - Pharmacy" / "Submitted" / "03_SOA" / "pharmacy_soa.xlsx"
    assert dest.is_file() and not (scratch / "pharmacy_soa.xlsx").exists()
    rows = common.read_jsonl(project / "state" / "pull_ledger.jsonl")
    assert len(rows) == 1
    row = rows[0]
    assert list(row) == list(drive_io.LEDGER_FIELDS)
    assert row["size_match"] is True and row["md5_match"] is True and row["transport"] == "connector"
    assert row["sha256"] == common.sha256_file(dest) and row["drive_md5"] == row["md5"]
    assert plan_by_id(project)["fid_twin1"]["status"] == "fetched"
    assert json.loads((project / "state" / "progress.json").read_text())["items"]["pull:fid_twin1"]["status"] == "fetched"
    assert json.loads((project / "state" / "source_lock.json").read_text())["locked"] is True  # lock restored

    # Same file again (copy mode): already present, no duplicate ledger row.
    assert run("ingest", "--blob", str(BLOBS / "pharmacy_soa.xlsx"), "--drive-id", "fid_twin1") == 0
    assert len(common.read_jsonl(project / "state" / "pull_ledger.jsonl")) == 1

    # Hash-identical twin at another path is ingested in its own right.
    assert run("ingest", "--blob", str(scratch / "di_soa.xlsx"), "--drive-id", "fid_twin2") == 0
    rows = common.read_jsonl(project / "state" / "pull_ledger.jsonl")
    assert len(rows) == 2 and rows[0]["sha256"] == rows[1]["sha256"] and rows[0]["rel_path"] != rows[1]["rel_path"]
    assert (project / "data" / "source" / "DD Sign-OFF" / "26 - Diagnostic Imaging" / "DD Closure" / "03_SOA" / "di_soa.xlsx").is_file()


def test_ingest_size_mismatch_deletes_copy(project: Path) -> None:
    assert run("list") == 0 and run("pull") == 0
    code = run("ingest", "--blob", str(BLOBS / "wrong_size.bin"), "--drive-id", "fid_mot")
    assert code == 2
    assert not (project / "data" / "source" / "Basis" / "MOT NSC" / "Corrigendum 2" / "Annex B" / "mot_nsc_c2.pdf").exists()
    assert common.read_jsonl(project / "state" / "pull_ledger.jsonl") == []
    assert plan_by_id(project)["fid_mot"]["status"] == "pending"
    assert (BLOBS / "wrong_size.bin").is_file()  # the blob itself is never touched without --move


def test_ingest_refuses_overwrite_unless_forced(project: Path, tmp_path: Path) -> None:
    assert run("list") == 0
    assert run("ingest", "--blob", str(BLOBS / "sample_calc.xlsx"), "--drive-id", "fid_tpl") == 0
    dest = project / "data" / "source" / "Template" / "sample_calc.xlsx"
    other = tmp_path / "other.xlsx"
    other.write_bytes(b"x" * (BLOBS / "sample_calc.xlsx").stat().st_size)  # same size, different content
    assert run("ingest", "--blob", str(other), "--drive-id", "fid_tpl") == 1
    assert dest.read_bytes() == (BLOBS / "sample_calc.xlsx").read_bytes()
    assert run("ingest", "--blob", str(other), "--drive-id", "fid_tpl", "--force") == 0
    assert dest.read_bytes() == other.read_bytes()
    superseded = list(dest.parent.glob("sample_calc.xlsx.superseded.*"))
    assert len(superseded) == 1 and superseded[0].read_bytes() == (BLOBS / "sample_calc.xlsx").read_bytes()
    assert drive_io.ledger_by_id()["fid_tpl"]["sha256"] == common.sha256_file(other)


def test_ingest_unknown_id_and_missing_blob(project: Path) -> None:
    assert run("ingest", "--blob", str(BLOBS / "sample_calc.xlsx"), "--drive-id", "fid_tpl") == 1  # no listing yet
    assert run("list") == 0
    assert run("ingest", "--blob", str(BLOBS / "sample_calc.xlsx"), "--drive-id", "nope") == 1
    assert run("ingest", "--blob", str(BLOBS / "does_not_exist"), "--drive-id", "fid_tpl") == 1


def test_ingest_dir_with_map(project: Path, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert run("list") == 0 and run("pull") == 0
    scratch = tmp_path / "batch"
    scratch.mkdir()
    for name in ("mot_nsc_c2.pdf", "sample_calc.xlsx"):
        shutil.copy(BLOBS / name, scratch / name)
    assert run("ingest-dir", "--dir", str(scratch), "--map", str(FIXTURES / "blob_map.json"), "--move") == 0
    assert "2 ok, 0 failed" in capsys.readouterr().out
    plan = plan_by_id(project)
    assert plan["fid_mot"]["status"] == "fetched" and plan["fid_tpl"]["status"] == "fetched"
    assert len(drive_io.ledger_by_id()) == 2
    # A CSV map with a missing blob makes the batch fail.
    csv_map = tmp_path / "map.csv"
    csv_map.write_text("blob,drive_id\nabsent.bin,fid_twin1\n")
    assert run("ingest-dir", "--dir", str(scratch), "--map", str(csv_map)) == 1


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
def test_verify_detects_tampering(project: Path, capsys: pytest.CaptureFixture) -> None:
    assert run("list") == 0
    assert run("verify") == 0  # empty ledger
    assert run("ingest", "--blob", str(BLOBS / "mot_nsc_c2.pdf"), "--drive-id", "fid_mot") == 0
    assert run("verify") == 0
    dest = project / "data" / "source" / "Basis" / "MOT NSC" / "Corrigendum 2" / "Annex B" / "mot_nsc_c2.pdf"
    drive_io.unlock_source()
    dest.write_bytes(dest.read_bytes()[:-1] + b"X")  # same size, different content
    assert run("verify") == 1
    out = capsys.readouterr().out
    assert "MISMATCH" in out and "sha256 differs from ledger" in out and "md5 differs from Drive" in out
    dest.unlink()
    assert run("verify") == 1
    assert "missing:" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# lock / unlock
# --------------------------------------------------------------------------- #
def test_lock_unlock_state_file(project: Path) -> None:
    source = project / "data" / "source" / "Template"
    source.mkdir(parents=True)
    (source / "a.txt").write_text("a")
    assert run("lock") == 0
    state = json.loads((project / "state" / "source_lock.json").read_text())
    assert state["locked"] is True and state["method"] in ("chattr", "chmod")
    assert isinstance(state["enforced"], bool) and "at" in state
    if state["method"] == "chattr":
        assert state["enforced"] is True
    assert not (project / "data" / "source" / ".lock_probe").exists()
    assert run("lock") == 0  # idempotent
    assert run("unlock") == 0
    state = json.loads((project / "state" / "source_lock.json").read_text())
    assert state["locked"] is False
    probe = project / "data" / "source" / "probe.txt"
    probe.write_text("writable again")
    assert probe.is_file()


# --------------------------------------------------------------------------- #
# rclone mode: exactly the commands from the brief, nothing else
# --------------------------------------------------------------------------- #
@pytest.fixture()
def fake_run(monkeypatch: pytest.MonkeyPatch) -> List[List[str]]:
    """Record every external command instead of running it."""
    calls: List[List[str]] = []

    def _fake(argv: Any, cwd: Any = None, timeout: Any = None) -> subprocess.CompletedProcess:
        calls.append(list(argv))
        stdout = ""
        if argv[:2] == ["rclone", "listremotes"]:
            stdout = "aigh_src:\naigh_md:\n"
        elif argv[:2] == ["rclone", "lsjson"]:
            stdout = json.dumps([{"Path": "App D/x.pdf", "Name": "x.pdf", "Size": 3, "MimeType": "application/pdf",
                                  "ModTime": "2026-11-20T02:00:00Z", "ID": "rid1", "Hashes": {"md5": "abc"}}])
        return subprocess.CompletedProcess(list(argv), 0, stdout, "")

    monkeypatch.setattr(drive_io, "_run", _fake)
    return calls


def test_transport_detection(fake_run: List[List[str]]) -> None:
    assert drive_io.detect_transport() == "rclone"


def test_rclone_commands_are_exact(project: Path, fake_run: List[List[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    ids = drive_io.drive_folder_ids()
    dest = project.resolve() / "data" / "source"
    assert run("--transport", "rclone", "list") == 0
    lsjson = [c for c in fake_run if c[:2] == ["rclone", "lsjson"]]
    assert lsjson == [["rclone", "lsjson", "-R", "--files-only", "--hash", f"aigh_src,root_folder_id={ids[k]}:"]
                      for k in ("dd_signoff", "template", "basis")]
    listing = drive_io.latest_listing()
    assert listing is not None
    assert [f["rel_path"] for f in listing["files"]] == ["Basis/App D/x.pdf", "DD Sign-OFF/App D/x.pdf", "Template/App D/x.pdf"]
    assert listing["files"][0]["md5Checksum"] == "abc" and listing["files"][0]["fileSize"] == "3"

    fake_run.clear()
    assert run("--transport", "rclone", "pull") == 0
    rclone_calls = [c for c in fake_run if c[0] == "rclone"]
    dd, tpl, basis = (f"aigh_src,root_folder_id={ids[k]}:" for k in ("dd_signoff", "template", "basis"))
    assert rclone_calls == [
        ["rclone", "copy", dd, str(dest / "DD Sign-OFF"), "--exclude", "*.zip", "--ignore-case"],
        ["rclone", "check", dd, str(dest / "DD Sign-OFF"), "--one-way", "--exclude", "*.zip", "--ignore-case"],
        ["rclone", "copy", tpl, str(dest / "Template")],
        ["rclone", "check", tpl, str(dest / "Template"), "--one-way"],
        ["rclone", "copy", basis, str(dest / "Basis")],
        ["rclone", "check", basis, str(dest / "Basis"), "--one-way"],
    ]
    assert all(c[0] in ("rclone", "chattr") for c in fake_run)

    fake_run.clear()
    (project / "out" / "markdown").mkdir(parents=True)
    (project / "out" / "markdown" / "a.md").write_text("# a\n")
    assert run("--transport", "rclone", "push-markdown") == 0
    assert fake_run == [["rclone", "copy", "out/markdown", "aigh_md:"],
                        ["rclone", "check", "out/markdown", "aigh_md:", "--one-way"]]
    assert drive_io.push_ledger_by_path()["a.md"]["transport"] == "rclone"


# --------------------------------------------------------------------------- #
# push-markdown (connector) and record-push
# --------------------------------------------------------------------------- #
def test_push_plan_and_record_push(project: Path) -> None:
    md = project / "out" / "markdown" / "DD Sign-OFF" / "30 - Pharmacy"
    md.mkdir(parents=True)
    (md / "soa.md").write_text("# SOA\n")
    assert run("push-markdown") == 0
    rows = common.read_csv_dicts(project / "state" / "push_plan.csv")
    assert list(rows[0]) == list(drive_io.PUSH_PLAN_COLUMNS)
    assert rows[0]["rel_path"] == "DD Sign-OFF/30 - Pharmacy/soa.md" and rows[0]["status"] == "pending"
    assert rows[0]["drive_path"] == "MarkDown/DD Sign-OFF/30 - Pharmacy/soa.md"
    assert run("record-push", "--path", "DD Sign-OFF/30 - Pharmacy/soa.md", "--drive-id", "md1") == 0
    assert run("push-markdown") == 0
    assert common.read_csv_dicts(project / "state" / "push_plan.csv")[0]["status"] == "pushed"
    (md / "soa.md").write_text("# SOA revised\n")
    assert run("push-markdown") == 0
    assert common.read_csv_dicts(project / "state" / "push_plan.csv")[0]["status"] == "pending"
    assert run("record-push", "--path", "nope.md", "--drive-id", "x") == 1


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
def test_doctor_report(project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    def _fake(argv: Any, cwd: Any = None, timeout: Any = None) -> subprocess.CompletedProcess:
        table = {"soffice": "LibreOffice 24.2.7.2\n", "rclone": "rclone v1.68.2\n" if argv[1] == "version" else ""}
        return subprocess.CompletedProcess(list(argv), 0, table.get(argv[0], ""), "")

    monkeypatch.setattr(drive_io, "_run", _fake)
    assert run("list") == 0
    capsys.readouterr()
    assert run("doctor") == 0
    out = capsys.readouterr().out
    report = json.loads(out.split("\n\n", 1)[0])
    assert report["soffice"].startswith("LibreOffice") and report["packages"]["markitdown"]
    assert report["transport"] == "connector" and report["listing"]["files"] == 6 and report["ledger_files"] == 0
    assert report["cpu_count"] and report["disk_free_bytes"] > 0 and report["problems"] == []
    assert "transport      connector" in out

    monkeypatch.setattr(drive_io, "_soffice_version", lambda: None)
    assert run("doctor") == 1


# --------------------------------------------------------------------------- #
# Fixer regressions: re-planning, branch relock, lock state honesty
# --------------------------------------------------------------------------- #
def test_file_modified_on_drive_after_fetch_is_replanned(project: Path) -> None:
    assert run("list") == 0 and run("pull") == 0
    assert run("ingest", "--blob", str(BLOBS / "sample_calc.xlsx"), "--drive-id", "fid_tpl") == 0
    assert plan_by_id(project)["fid_tpl"]["status"] == "fetched"
    rows = [json.loads(l) for l in (FIXTURES / "listing_b.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    for row in rows:
        if row["id"] == "fid_tpl":
            row["fileSize"], row["modifiedTime"] = "300", "2026-11-23T08:00:00Z"
    (project / "state" / "drive_listing" / "dd.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert run("list") == 0 and run("pull") == 0
    plan = plan_by_id(project)
    assert plan["fid_tpl"]["status"] == "pending"  # changed on Drive: re-fetch
    assert plan["fid_mot"]["status"] == "pending" and plan["fid_zip"]["status"] == "skipped_zip"
    # The unchanged ledger row is still trusted once Drive reports the old size again.
    for row in rows:
        if row["id"] == "fid_tpl":
            row["fileSize"] = "217"
    (project / "state" / "drive_listing" / "dd.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert run("list") == 0 and run("pull") == 0
    assert plan_by_id(project)["fid_tpl"]["status"] == "fetched"


def test_ingest_relocks_only_the_branch(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert run("list") == 0 and run("pull") == 0  # pull leaves data/source locked
    state = json.loads((project / "state" / "source_lock.json").read_text())
    calls: List[List[str]] = []
    real_run = drive_io._run

    def _recording(argv: Any, cwd: Any = None, timeout: Any = None) -> subprocess.CompletedProcess:
        calls.append(list(argv))
        return real_run(argv, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(drive_io, "_run", _recording)
    assert run("ingest", "--blob", str(BLOBS / "mot_nsc_c2.pdf"), "--drive-id", "fid_mot") == 0
    assert not any(c[:3] == ["chattr", "-R", "+i"] for c in calls)  # no whole-tree relock per file
    dest = project / "data" / "source" / "Basis" / "MOT NSC" / "Corrigendum 2" / "Annex B" / "mot_nsc_c2.pdf"
    assert dest.is_file()
    if state["method"] == "chattr" and state["enforced"]:
        with pytest.raises(OSError):
            dest.write_bytes(b"tamper")
        with pytest.raises(OSError):
            (dest.parent / "new.txt").write_bytes(b"x")
    assert json.loads((project / "state" / "source_lock.json").read_text())["locked"] is True
    assert run("verify") == 0


def test_corrupt_lock_state_does_not_mask_ingest_exit_code(project: Path) -> None:
    assert run("list") == 0
    (project / "state" / "source_lock.json").write_text("{not json", encoding="utf-8")
    assert run("ingest", "--blob", str(BLOBS / "wrong_size.bin"), "--drive-id", "fid_mot") == 2
    assert run("ingest", "--blob", str(BLOBS / "mot_nsc_c2.pdf"), "--drive-id", "fid_mot") == 0
    assert drive_io.read_lock_state() == {}


def test_lock_state_reports_probe_and_failures(project: Path) -> None:
    nested = project / "data" / "source" / "Basis" / "App D"
    nested.mkdir(parents=True)
    (nested / "x.pdf").write_bytes(b"%PDF")
    assert run("lock") == 0
    state = json.loads((project / "state" / "source_lock.json").read_text())
    assert state["probed_dirs"] == 3 and state["failures"] == 0 and isinstance(state["writable_dirs"], list)
    assert state["enforced"] == (state["writable_dirs"] == [])
    assert not list((project / "data" / "source").rglob(".lock_probe"))
    assert drive_io.relock_branch([]) is True
    assert run("unlock") == 0
    assert drive_io.relock_branch([nested / "x.pdf"]) is True  # not locked: nothing to do
    (nested / "x.pdf").write_bytes(b"%PDF-1.4")


def test_rel_path_safety_rules() -> None:
    assert drive_io.rel_path_is_safe("Basis/App D/x.pdf")
    assert drive_io.rel_path_is_safe("DD Sign-OFF/30 - Pharmacy/Submitted/Plan – Ebene 2 (révisé).pdf")
    for bad in ("", "/etc/passwd", "Basis/../x", "./x", "Basis//x.pdf", "Basis/./x.pdf", "..", "a/.."):
        assert not drive_io.rel_path_is_safe(bad), bad


# --------------------------------------------------------------------------- #
# Upload ledger counts as fetched in the pull plan
# --------------------------------------------------------------------------- #
def test_pull_plan_treats_upload_ledger_files_as_fetched(project: Path) -> None:
    """A file over the connector limit that the engineer uploaded is fetched (transport upload), not blocked."""
    assert run("list") == 0
    assert run("pull") == 0
    assert plan_by_id(project)["fid_big"]["status"] == "blocked_connector_limit"
    big = plan_by_id(project)["fid_big"]
    # by Drive id, even though the working copy's bytes differ from Drive's (an assembled copy)
    common.append_jsonl(project / "state" / "upload_ledger.jsonl",
                        {"rel_path": big["rel_path"], "drive_id": "fid_big", "transport": "upload-assembled",
                         "bytes": 1, "size_match": False})
    assert run("pull") == 0
    assert plan_by_id(project)["fid_big"]["status"] == "fetched"
    # by rel_path alone (a parent id recorded as free text, as for the uploaded parts)
    (project / "state" / "upload_ledger.jsonl").unlink()
    common.append_jsonl(project / "state" / "upload_ledger.jsonl",
                        {"rel_path": big["rel_path"], "drive_id": "fid_big (parent file, 9 B)", "transport": "upload"})
    assert run("pull") == 0
    assert plan_by_id(project)["fid_big"]["status"] == "fetched"
    assert plan_by_id(project)["fid_zip"]["status"] == "skipped_zip"  # zips stay skipped
    assert plan_by_id(project)["fid_tpl"]["status"] == "pending"  # the upload ledger does not touch other rows
    before = (project / "state" / "pull_plan.csv").read_text()
    assert run("pull") == 0 and (project / "state" / "pull_plan.csv").read_text() == before
    assert json.loads((project / "state" / "source_lock.json").read_text())["locked"] is True
