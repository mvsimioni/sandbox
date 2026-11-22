"""Tests for src.common and src.state using small synthetic fixtures only."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
from pathlib import Path

import pytest

from src import common
from src.state import Progress, file_lock

FIXTURES = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------------- #
# Project root / config / paths
# --------------------------------------------------------------------------- #
def test_project_root_and_paths() -> None:
    root = common.project_root()
    assert (root / "config" / "project.yaml").is_file()
    assert common.PATHS["state"] == (root / "state").resolve()
    assert common.PATHS["source"].is_absolute()
    assert common.load_config() is common.load_config()  # cached
    assert common.CONNECTOR_LIMIT_BYTES == 10485760


def test_utc_now_iso_format() -> None:
    stamp = common.utc_now_iso()
    assert len(stamp) == 20 and stamp.endswith("Z") and stamp[10] == "T"


# --------------------------------------------------------------------------- #
# parse_department / parse_stage / is_zip
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "rel_path, expected",
    [
        (
            "DD Sign-OFF/30 - Pharmacy/Submitted/03_SOA/a.xlsx",
            {"root": "DD Sign-OFF", "dept_no": "30", "dept_name": "Pharmacy", "stray": False},
        ),
        (
            "DD Sign-OFF/__29 - Lab/x.pdf",
            {"root": "DD Sign-OFF", "dept_no": "29", "dept_name": "Lab", "stray": True},
        ),
        ("Basis/App D/y.pdf", {"root": "Basis", "dept_no": "", "dept_name": "", "stray": False}),
        ("Template/z.xlsx", {"root": "Template", "dept_no": "", "dept_name": "", "stray": False}),
        (
            "DD Sign-OFF/33- Outpatient - Admin Neighbourhood/DD Closure/f.pdf",
            {"root": "DD Sign-OFF", "dept_no": "33", "dept_name": "Outpatient - Admin Neighbourhood", "stray": False},
        ),
        (
            "DD Sign-OFF/30 - Pharmacy.zip",
            {"root": "DD Sign-OFF", "dept_no": "30", "dept_name": "Pharmacy", "stray": False},
        ),
        ("DD Sign-OFF/Misc notes/a.pdf", {"root": "DD Sign-OFF", "dept_no": "", "dept_name": "", "stray": False}),
    ],
)
def test_parse_department(rel_path: str, expected: dict) -> None:
    assert common.parse_department(rel_path) == expected


@pytest.mark.parametrize(
    "rel_path, expected",
    [
        ("DD Sign-OFF/30 - Pharmacy/Submitted/03_SOA/a.xlsx", "Submitted"),
        ("DD Sign-OFF/30 - Pharmacy/submitted/a.xlsx", "Submitted"),
        ("DD Sign-OFF/30 - Pharmacy/ AH Sign Off With Comments /a.pdf", "AH sign off with comments"),
        ("DD Sign-OFF/30 - Pharmacy/AH sign off with comments/a.pdf", "AH sign off with comments"),
        ("DD Sign-OFF/30 - Pharmacy/DD CLOSURE/a.pdf", "DD Closure"),
        ("DD Sign-OFF/30 - Pharmacy/dd closure/x/y/a.pdf", "DD Closure"),
        ("DD Sign-OFF/30 - Pharmacy/Old/a.pdf", "other"),
        ("DD Sign-OFF/30 - Pharmacy/a.pdf", "other"),
        ("Basis/App D/y.pdf", "other"),
        ("Template/z.xlsx", "other"),
    ],
)
def test_parse_stage(rel_path: str, expected: str) -> None:
    assert common.parse_stage(rel_path) == expected


def test_is_zip() -> None:
    assert common.is_zip("DD Sign-OFF/Legend and Appendix.zip")
    assert common.is_zip("a/B.ZIP")
    assert not common.is_zip("a/zip_folder/b.pdf")
    assert not common.is_zip("")


# --------------------------------------------------------------------------- #
# classify_document
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name, expected",
    [
        ("MAY 2025_374_BSTS_P1C0_Satellite Supply Station_RDS_DD-SIGN-OFF.pdf", "RDS"),
        ("MP-RLS-32-INTF-C-IP-001 - Interview Room - Clinical.pdf", "RLS"),
        ("20250728_AIGH_DD_SOA_V16_TRED_SIMLAB.xlsx", "SOA"),
        ("TRED_SoE_v12 (30Apr25).xlsx", "SoE"),
        ("47_AN Hub_Drawing List.xlsx", "drawing list"),
        ("48_AN Hub_DWG list.xlsx", "drawing list"),
        ("Compiled Comments.xlsx", "comment register"),
        ("AR-IP-31-ZZ-001.pdf", "elevation"),
        ("AR-OP-31_ZZ-001-NORTH ELEVATION (OP)_REV-A.pdf", "elevation"),
        ("IP-BH-05010.pdf", "schematic"),
        ("HS-BH-05010_Rev0A.pdf", "schematic"),
        ("Legend and Appendix.zip", "zip"),
        ("DD Sign-OFF/30 - Pharmacy/Submitted/Pharmacy DD Booklet Rev2.pptx", "Booklet"),
        ("30_Pharmacy_DD_Reply_to_AH_comments.pdf", "Booklet"),
        ("AIGH_DD_JACOBS_Pharmacy.pptx", "Booklet"),
        ("AR-IP-01-L05-001 Layout.pdf", "layout plan"),
        ("Level 5 layout plan.pdf", "layout plan"),
        ("RE AHU zoning query.msg", "correspondence"),
        ("site visit.eml", "correspondence"),
        ("IMG_0042.JPG", "photo"),
        ("scan.heic", "photo"),
        ("NDH - Inpatients Building HVAC Load Calcs - L5 Developed Design.xlsm", "workbook"),
        ("AHU Zoning - RFI-ME-0233.pdf", "pdf document"),
        ("Dept briefing.pptx", "presentation"),
        ("notes.txt", "other"),
        ("README", "other"),
        ("Guards.pdf", "pdf document"),  # 'rds' inside a word is not an RDS token
        ("Address list.xlsx", "workbook"),  # 'dd' inside a word is not a DD marker
    ],
)
def test_classify_document(name: str, expected: str) -> None:
    assert common.classify_document(name) == expected


def test_classify_with_reason_returns_pair() -> None:
    doc_class, reason = common.classify_with_reason("IP-BH-05010.pdf")
    assert doc_class == "schematic"
    assert isinstance(reason, str) and reason


# --------------------------------------------------------------------------- #
# hashing and atomic writes
# --------------------------------------------------------------------------- #
def test_hash_helpers_match_hashlib() -> None:
    sample = FIXTURES / "sample_hash.txt"
    data = sample.read_bytes()
    assert common.sha256_file(sample) == hashlib.sha256(data).hexdigest()
    assert common.md5_file(sample) == hashlib.md5(data).hexdigest()
    assert common.sha256_file(sample, chunk=7) == common.sha256_file(sample)


def test_atomic_write_text_and_json(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "out.txt"
    common.atomic_write_text(target, "héllo\n")
    assert target.read_text(encoding="utf-8") == "héllo\n"
    common.atomic_write_text(target, "second\n")
    assert target.read_text(encoding="utf-8") == "second\n"
    assert [p.name for p in target.parent.iterdir()] == ["out.txt"]  # no temp files left behind

    json_target = tmp_path / "doc.json"
    payload = {"phase": 1, "items": {"a": {"status": "done", "hash": None}}, "text": "Fëanor"}
    common.atomic_write_json(json_target, payload)
    assert json.loads(json_target.read_text(encoding="utf-8")) == payload


def test_atomic_write_failure_leaves_old_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "keep.txt"
    common.atomic_write_text(target, "original")

    def boom(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise OSError("simulated replace failure")

    monkeypatch.setattr(common.os, "replace", boom)
    with pytest.raises(OSError):
        common.atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "original"
    assert [p.name for p in tmp_path.iterdir()] == ["keep.txt"]


# --------------------------------------------------------------------------- #
# JSONL and CSV round trips
# --------------------------------------------------------------------------- #
def test_jsonl_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    assert common.read_jsonl(path) == []
    rows = [{"drive_id": "abc", "bytes": 12, "ok": True}, {"drive_id": "déf", "nested": {"x": [1, 2]}}]
    for row in rows:
        common.append_jsonl(path, row)
    assert common.read_jsonl(path) == rows
    path.write_text(path.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")  # blank lines ignored
    assert common.read_jsonl(path) == rows


def test_csv_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "plan.csv"
    fieldnames = ["drive_id", "rel_path", "bytes", "status"]
    rows = [
        {"drive_id": "1", "rel_path": "DD Sign-OFF/30 - Pharmacy/a, b.xlsx", "bytes": 10, "status": "pending"},
        {"drive_id": "2", "rel_path": 'quote "x"', "bytes": None},
    ]
    common.write_csv_dicts(path, rows, fieldnames)
    back = common.read_csv_dicts(path)
    assert back == [
        {"drive_id": "1", "rel_path": "DD Sign-OFF/30 - Pharmacy/a, b.xlsx", "bytes": "10", "status": "pending"},
        {"drive_id": "2", "rel_path": 'quote "x"', "bytes": "", "status": ""},
    ]
    assert b"\r\n" in path.read_bytes()  # csv module line endings, not doubled by newline=""
    assert common.read_csv_dicts(tmp_path / "missing.csv") == []
    with pytest.raises(ValueError):
        common.write_csv_dicts(path, [{"drive_id": "1", "unexpected": "x"}], fieldnames)


def test_ensure_dir(tmp_path: Path) -> None:
    made = common.ensure_dir(tmp_path / "a" / "b")
    assert made.is_dir()
    assert common.ensure_dir(made) == made


# --------------------------------------------------------------------------- #
# Progress
# --------------------------------------------------------------------------- #
def test_progress_set_get(tmp_path: Path) -> None:
    progress = Progress(tmp_path / "progress.json")
    assert progress.get("x") is None
    assert progress.phase == 0

    record = progress.set("pull:abc", "done", hash="deadbeef", bytes=10)
    assert record["status"] == "done" and record["hash"] == "deadbeef" and record["bytes"] == 10
    assert record["updated_at"].endswith("Z")

    stored = progress.get("pull:abc")
    assert stored == record
    assert progress.status("pull:abc") == "done"
    assert progress.is_done("pull:abc", "deadbeef")
    assert not progress.is_done("pull:abc", "other")

    progress.set("pull:def", "pending")
    assert progress.get("pull:def")["hash"] is None
    progress.set("pull:abc", "failed")  # hash kept when not supplied
    assert progress.get("pull:abc")["hash"] == "deadbeef"

    assert set(progress.items_with_status("failed")) == {"pull:abc"}
    assert set(progress.items_with_status("pending")) == {"pull:def"}
    assert progress.items_with_status("nope") == {}

    progress.set_phase(2)
    assert progress.phase == 2
    on_disk = json.loads((tmp_path / "progress.json").read_text(encoding="utf-8"))
    assert set(on_disk) == {"phase", "items"} and on_disk["phase"] == 2

    assert progress.remove("pull:def") is True
    assert progress.remove("pull:def") is False
    assert progress.get("pull:def") is None


def _worker(path: str, prefix: str, count: int) -> None:
    progress = Progress(path)
    for i in range(count):
        progress.set(f"{prefix}:{i}", "done", hash=str(i))


def test_progress_parallel_writers_do_not_clobber(tmp_path: Path) -> None:
    path = tmp_path / "progress.json"
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_worker, args=(str(path), f"w{n}", 40)) for n in range(4)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
        assert proc.exitcode == 0
    items = Progress(path).load()["items"]
    assert len(items) == 160


def test_file_lock_is_reentrant_across_handles(tmp_path: Path) -> None:
    target = tmp_path / "x.json"
    with file_lock(target):
        pass
    assert (tmp_path / "x.json.lock").exists()
