"""Tests for the review fixes: float-precision recall, duplicate canonical choice, killed workers."""

from __future__ import annotations

import argparse
import os
import signal
import time
from pathlib import Path
from typing import Dict

import pytest

from src import convert
from src import md_writer as mw
from src import recall_check as rc
from src.common import read_jsonl
from tests.test_convert import REL_COLL_PDF, REL_DUP, REL_PDF, REL_PNG, build_tree


# --------------------------------------------------------------------------- #
# (a) float precision
# --------------------------------------------------------------------------- #
def test_shorter_print_of_the_same_float_is_not_a_miss() -> None:
    assert rc.numbers_equivalent("113.30000000000001", "113.3")
    assert rc.numbers_equivalent("0.3074074074074074", "0.307407")
    assert rc.numbers_equivalent("0.2858695652173913", "0.28587")
    found = {"113.3", "0.307407"}
    score, misses = rc.recall({"113.30000000000001", "0.3074074074074074"}, found)
    assert score == 1.0 and misses == []


def test_a_different_or_coarser_number_is_still_a_miss() -> None:
    assert not rc.numbers_equivalent("0.3074074074074074", "0.3")
    assert not rc.numbers_equivalent("113.3", "113.4")
    assert not rc.numbers_equivalent("0.0000004", "0")
    score, misses = rc.recall({"0.3074074074074074", "113.3"}, {"0.3", "113.4"})
    assert score == 0.0 and len(misses) == 2


def test_writer_does_not_repeat_cells_that_markitdown_rounded() -> None:
    assert mw.missing_keys("113.30000000000001 flow 7", {"113.3", "flow", "7"}) == set()
    assert mw.missing_keys("113.30000000000001 flow", {"113.4", "flow"}) == {"113.30000000000001"}
    assert mw.word_missing("0.3074074074074074", {"0.307407"}) is False
    assert mw.word_missing("0.3074074074074074", {"0.31"}) is True


# --------------------------------------------------------------------------- #
# (b) duplicates
# --------------------------------------------------------------------------- #
def _args(**kw: object) -> argparse.Namespace:
    base = dict(paths=None, roots=None, dept=None, only_class=None, force=False, timeout=600, scratch=None, limit=0)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture()
def small_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = build_tree(tmp_path / "proj")
    monkeypatch.setenv("AIGH_ROOT", str(root))
    return root


def test_duplicate_outside_the_selection_is_converted_in_full(small_tree: Path) -> None:
    layout = convert.Layout.from_root(small_tree)
    rows = convert.load_source_rows(layout.state)
    tasks, _ = convert.plan_tasks(rows, layout, _args(paths=[REL_DUP]))
    assert [t.rel_path for t in tasks] == [REL_DUP] and tasks[0].duplicate_of is None


def test_duplicate_canonical_is_in_the_selection_and_ordered_first(small_tree: Path) -> None:
    layout = convert.Layout.from_root(small_tree)
    rows = convert.load_source_rows(layout.state)
    tasks, _ = convert.plan_tasks(rows, layout, _args(paths=[REL_DUP, REL_PDF]))
    by_rel = {t.rel_path: t for t in tasks}
    assert by_rel[REL_PDF].duplicate_of is None and by_rel[REL_DUP].duplicate_of == REL_PDF
    assert [t.rel_path for t in tasks].index(REL_PDF) < [t.rel_path for t in tasks].index(REL_DUP)


def test_duplicate_stub_always_points_at_an_existing_markdown(small_tree: Path) -> None:
    root = small_tree
    assert convert.main(["run", "--root", str(root), "--workers", "1", "--paths", REL_DUP]) == 0
    assert convert.main(["run", "--root", str(root), "--workers", "1", "--paths", REL_PDF]) == 0
    rows = {r["rel_path"]: r for r in read_jsonl(root / "state" / "convert_ledger.jsonl")}
    assert rows[REL_DUP]["status"] == "converted"
    assert rows[REL_PDF]["status"] == "duplicate" and rows[REL_PDF]["duplicate_of"] == REL_DUP
    assert (root / "out" / "markdown" / "Basis" / "copy of 30_Pharmacy_RDS.md").is_file()
    stub = (root / str(rows[REL_PDF]["md_path"])).read_text(encoding="utf-8")
    assert "copy of 30_Pharmacy_RDS.md" in stub


# --------------------------------------------------------------------------- #
# (c) killed worker
# --------------------------------------------------------------------------- #
def _kill_or_pass(task_dict: Dict[str, object]) -> Dict[str, object]:
    """Worker stand-in: SIGKILL itself mid-file for ``Sched.pdf``, succeed for the rest."""
    if str(task_dict["rel_path"]).endswith("Sched.pdf"):
        time.sleep(0.3)
        os.kill(os.getpid(), signal.SIGKILL)
    return convert._result(str(task_dict["sha256"]), str(task_dict["rel_path"]), convert.STATUS_CONVERTED)  # noqa: SLF001


def test_killed_worker_is_a_ledger_error_within_the_timeout_and_run_continues(
        small_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(convert, "convert_task", _kill_or_pass)
    started = time.monotonic()
    rc_code = convert.main(["run", "--root", str(small_tree), "--workers", "2", "--timeout", "30",
                            "--paths", REL_COLL_PDF, REL_PNG])
    assert time.monotonic() - started < 30
    assert rc_code == 1
    rows = {r["rel_path"]: r for r in read_jsonl(small_tree / "state" / "convert_ledger.jsonl")}
    assert rows[REL_COLL_PDF]["status"] == "error" and rows[REL_COLL_PDF]["failure"] == convert.FAILURE_WORKER_DIED
    assert rows[REL_COLL_PDF]["exit_code"] == -signal.SIGKILL
    assert rows[REL_PNG]["status"] == "converted"
