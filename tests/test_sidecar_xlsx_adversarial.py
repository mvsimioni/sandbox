"""Adversarial tests for ``src.sidecar_xlsx`` written by the independent reviewer.

Every fixture is generated in ``tmp_path`` with openpyxl (plus hand-edited zip
parts); no real source file is used.  Tests marked ``xfail(strict=True)``
expose a confirmed defect listed in ``out/qa/verify/code_sidecar_xlsx_review.md``
under the issue id named in the reason; they flip to XPASS (and so fail the
suite) as soon as the defect is fixed, which is the intended signal.
"""

from __future__ import annotations

import json
import re
import time
import zipfile
from pathlib import Path
from typing import Dict, List

import pytest

from src import sidecar_xlsx as sx
from src.common import read_csv_dicts, read_jsonl, sha256_file


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def rewrite_zip(src: Path, dst: Path, replace: Dict[str, bytes], add: Dict[str, bytes], drop: List[str] = ()) -> None:
    """Copy a package zip, replacing, adding and dropping parts."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename in drop:
                continue
            zout.writestr(item, replace.get(item.filename, zin.read(item.filename)))
        for name, data in add.items():
            zout.writestr(name, data)


def state_dir(tmp_path: Path) -> Path:
    """An empty state directory (no ledgers)."""
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    return state


def manifest_of(out: Path) -> Dict:
    return json.loads((out / sx.MANIFEST_NAME).read_text(encoding="utf-8"))


def check(manifest: Dict, prefix: str) -> Dict:
    return next(c for c in manifest["checks"] if c["check"].startswith(prefix))


def load_json(out: Path, name: str) -> Dict:
    return json.loads((out / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Behaviour that holds (documented)
# --------------------------------------------------------------------------- #
def test_very_hidden_sheet_and_formula_error(tmp_path: Path) -> None:
    """A veryHidden sheet is exported with its state; cached error values are typed ``e``."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws["A1"] = "=1/0"
    ws["A2"] = "=NA()"
    ws["A3"] = 7
    secret = wb.create_sheet("Secret")
    secret["A1"] = 42
    secret.sheet_state = "veryHidden"
    plain = tmp_path / "vh.xlsx"
    wb.save(plain)
    # openpyxl writes no cached values; inject the error results as Excel would store them
    with zipfile.ZipFile(plain) as zin:
        sheet_xml = zin.read("xl/worksheets/sheet1.xml").decode("utf-8")
    sheet_xml = sheet_xml.replace('<c r="A1"><f>1/0</f><v></v></c>', '<c r="A1" t="e"><f>1/0</f><v>#DIV/0!</v></c>')
    sheet_xml = sheet_xml.replace('<c r="A2"><f>NA()</f><v></v></c>', '<c r="A2" t="e"><f>NA()</f><v>#N/A</v></c>')
    assert "#DIV/0!" in sheet_xml and "#N/A" in sheet_xml, "fixture injection did not match openpyxl's XML"
    src = tmp_path / "vh_err.xlsx"
    rewrite_zip(plain, src, {"xl/worksheets/sheet1.xml": sheet_xml.encode("utf-8")}, {})

    out = tmp_path / "out"
    manifest = sx.run_one(src, out, state_dir(tmp_path))
    values = {r["cell"]: r for r in read_csv_dicts(out / "Sheet_values.csv")}
    assert values["A1"] == {"cell": "A1", "value": "#DIV/0!", "type": "e"}
    assert values["A2"] == {"cell": "A2", "value": "#N/A", "type": "e"}
    assert values["A3"]["type"] == "n"
    hidden = load_json(out, sx.HIDDEN_NAME)
    assert hidden["Secret"]["state"] == "veryHidden"
    wb_json = load_json(out, sx.WORKBOOK_NAME)
    assert [s["state"] for s in wb_json["sheets"]] == ["visible", "veryHidden"]
    assert read_csv_dicts(out / "Secret_values.csv") == [{"cell": "A1", "value": "42", "type": "n"}]
    assert manifest["counts"]["hidden_sheets"] == 1
    assert check(manifest, "cached value present")["pass"] is True
    assert check(manifest, "merged ranges")["pass"] is True


def test_unicode_file_name_with_spaces_and_brackets_cli(tmp_path: Path) -> None:
    """Unicode path with spaces and brackets runs via the CLI; unicode sheet names get unique stems."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "負荷計算"
    ws["A1"] = 1
    wb.create_sheet("風量 (2)")["A1"] = 2
    wb.create_sheet("Résumé")["A1"] = 3
    src = tmp_path / "Thermal [Rev A] (final) é.xlsx"
    wb.save(src)
    out = tmp_path / "uni out [1]"
    rc = sx.main(["run", "--xlsx", str(src), "--out", str(out), "--state-dir", str(state_dir(tmp_path)),
                  "--no-progress"])
    assert rc == 0
    manifest = manifest_of(out)
    assert manifest["source_path"] == str(src) and manifest["sha256"] == sha256_file(src)
    stems = [s["files"]["values"] for s in load_json(out, sx.WORKBOOK_NAME)["sheets"]]
    assert len(set(stems)) == 3 and all((out / f).is_file() for f in stems)
    # the stems are ASCII-only, so the mapping back to the sheet lives only in workbook.json (I-09)
    assert all(re.fullmatch(r"[A-Za-z0-9._-]+", f) for f in stems)
    assert all(c["pass"] for c in manifest["checks"])


def test_workbook_with_zero_comments_cli(tmp_path: Path) -> None:
    """No comment parts at all: empty comments.jsonl, zero counts, every check passes."""
    from openpyxl import Workbook

    wb = Workbook()
    wb.active["A1"] = "plain"
    src = tmp_path / "nocomments.xlsx"
    wb.save(src)
    out = tmp_path / "out"
    assert sx.main(["run", "--xlsx", str(src), "--out", str(out), "--state-dir", str(state_dir(tmp_path)),
                    "--no-progress"]) == 0
    assert (out / sx.COMMENTS_NAME).read_text(encoding="utf-8") == ""
    manifest = manifest_of(out)
    assert manifest["counts"]["comment_count"] == 0
    assert check(manifest, "comment rows equal")["evidence"] == "legacy 0/0, threaded 0/0"
    assert all(c["pass"] for c in manifest["checks"])
    assert load_json(out, sx.WORKBOOK_NAME)["threaded_comment_parts"] == {"threaded_parts": [], "persons": 0,
                                                                            "unlinked_parts": [],
                                                                            "unreadable_parts": []}


def test_hidden_column_group_is_expanded(tmp_path: Path) -> None:
    """A ``<col min=5 max=7 hidden=1>`` group yields E, F and G, not just E."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws["A1"] = 1
    ws.column_dimensions.group("E", "G", hidden=True)
    src = tmp_path / "group.xlsx"
    wb.save(src)
    out = tmp_path / "out"
    sx.run_one(src, out, state_dir(tmp_path))
    assert load_json(out, sx.HIDDEN_NAME)["Sheet"]["cols"] == ["E", "F", "G"]


def test_chartsheet_workbook_runs_but_chartsheet_is_not_listed(tmp_path: Path) -> None:
    """A chartsheet does not break the run; it is absent from workbook.json (documented, I-13)."""
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference

    wb = Workbook()
    ws = wb.active
    ws["A1"] = 1
    chart_sheet = wb.create_chartsheet("Chart1")
    chart = BarChart()
    chart.add_data(Reference(ws, min_col=1, min_row=1, max_row=1))
    chart_sheet.add_chart(chart)
    src = tmp_path / "chartsheet.xlsx"
    wb.save(src)
    out = tmp_path / "out"
    manifest = sx.run_one(src, out, state_dir(tmp_path))
    assert all(c["pass"] for c in manifest["checks"])
    with zipfile.ZipFile(src) as archive:
        assert [name for name, _part in sx.sheet_parts(archive)] == ["Sheet", "Chart1"]
    assert [s["name"] for s in load_json(out, sx.WORKBOOK_NAME)["sheets"]] == ["Sheet"]


def test_cell_error_literal_and_type_codes() -> None:
    """Error strings are typed ``e`` whichever Excel error they are; plain text stays ``s``."""
    for literal in ("#DIV/0!", "#N/A", "#NAME?", "#NULL!", "#NUM!", "#REF!", "#VALUE!"):
        assert sx.value_type(literal) == "e", literal
    assert sx.value_type("#1 priority") == "s"
    assert sx.value_type(True) == "b" and sx.value_type(2.5) == "n"


# --------------------------------------------------------------------------- #
# Defects fixed after the review (I-01 to I-05): now ordinary regression tests
# --------------------------------------------------------------------------- #
def test_gradient_fill_does_not_abort_run(tmp_path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import GradientFill

    wb = Workbook()
    ws = wb.active
    ws["A1"] = 1
    ws["A1"].fill = GradientFill(stop=["FFFF0000", "FF0000FF"])
    src = tmp_path / "gradient.xlsx"
    wb.save(src)
    out = tmp_path / "out"
    manifest = sx.run_one(src, out, state_dir(tmp_path))
    assert (out / "Sheet_values.csv").is_file()
    fills = load_json(out, sx.FILLS_NAME)["Sheet"]
    assert fills and fills[0]["fill_type"] in ("linear", "path", "gradient")
    assert manifest["counts"]["cells_non_empty"] == 1


def test_conditional_format_fill_colour_from_bgcolor(tmp_path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.formatting.rule import CellIsRule
    from openpyxl.styles import PatternFill

    wb = Workbook()
    ws = wb.active
    ws["A1"] = 500
    # Excel writes <dxf><fill><patternFill><bgColor rgb="FFFFC7CE"/></patternFill></fill></dxf>
    ws.conditional_formatting.add("A1:A5", CellIsRule(operator="greaterThan", formula=["100"],
                                                      fill=PatternFill(bgColor="FFFFC7CE")))
    src = tmp_path / "dxf.xlsx"
    wb.save(src)
    with zipfile.ZipFile(src) as archive:
        styles = archive.read("xl/styles.xml").decode("utf-8")
    assert '<bgColor rgb="FFFFC7CE"/>' in styles and "fgColor" not in styles.split("<dxfs")[1]
    out = tmp_path / "out"
    sx.run_one(src, out, state_dir(tmp_path))
    (rule,) = load_json(out, sx.CONDITIONAL_NAME)["Sheet"][0]["rules"]
    assert rule["fill"] == "rgb:FFFFC7CE"


def test_data_table_formula_is_not_written_as_object_repr(tmp_path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.worksheet.formula import DataTableFormula

    wb = Workbook()
    ws = wb.active
    ws["A1"] = 5
    ws["B2"] = DataTableFormula(ref="B2:B4", dt2D=False, r1="A1")
    src = tmp_path / "datatable.xlsx"
    wb.save(src)
    out = tmp_path / "out"
    sx.run_one(src, out, state_dir(tmp_path))
    values = {r["cell"]: r for r in read_csv_dicts(out / "Sheet_values.csv")}
    assert "object at 0x" not in values["B2"]["value"]
    formulas = {r["cell"] for r in read_csv_dicts(out / "Sheet_formulas.csv")}
    assert "B2" in formulas


def test_malformed_threaded_part_is_reported(tmp_path: Path) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    wb.active["A1"] = 1
    plain = tmp_path / "plain.xlsx"
    wb.save(plain)
    src = tmp_path / "badtc.xlsx"
    rewrite_zip(plain, src, {}, {"xl/threadedComments/threadedComment1.xml":
                                 b"<ThreadedComments><threadedComment ref='A1'><text>lost</text>"})
    out = tmp_path / "out"
    manifest = sx.run_one(src, out, state_dir(tmp_path))
    summary = load_json(out, sx.WORKBOOK_NAME)["threaded_comment_parts"]
    unreadable = summary.get("unreadable_parts") or summary.get("malformed_parts")
    assert unreadable == ["xl/threadedComments/threadedComment1.xml"]
    assert not all(c["pass"] for c in manifest["checks"]), "a lost comment part must fail a check"


def test_sparse_styled_cell_does_not_blow_up_runtime(tmp_path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill

    wb = Workbook()
    ws = wb.active
    ws["A1"] = 1
    ws.cell(20000, 26).fill = PatternFill("solid", fgColor="FFFF0000")  # styled, empty, far away
    src = tmp_path / "sparse.xlsx"
    wb.save(src)
    out = tmp_path / "out"
    started = time.monotonic()
    manifest = sx.run_one(src, out, state_dir(tmp_path))
    elapsed = time.monotonic() - started
    assert manifest["counts"]["cells_non_empty"] == 1
    assert elapsed < 2.0, f"two stored cells took {elapsed:.1f}s (520k synthetic cells per pass)"


# --------------------------------------------------------------------------- #
# Confirmed defects still open (strict xfail; each names its issue id)
# --------------------------------------------------------------------------- #
@pytest.mark.xfail(strict=True, reason="I-06: a changed source reuses the out dir and leaves the old sheet CSVs on disk")
def test_source_changed_in_place_removes_stale_files(tmp_path: Path) -> None:
    from openpyxl import Workbook

    src = tmp_path / "changing.xlsx"
    out = tmp_path / "out"
    state = state_dir(tmp_path)
    wb = Workbook()
    wb.active.title = "Old"
    wb.active["A1"] = 1
    wb.save(src)
    sx.run_one(src, out, state)
    wb = Workbook()
    wb.active.title = "New"
    wb.active["A1"] = 1
    wb.save(src)
    manifest = sx.run_one(src, out, state)
    assert manifest["files"] and not any(f.startswith("Old") for f in manifest["files"])
    assert not any(p.name.startswith("Old") for p in out.iterdir()), "stale sidecars from the previous sha remain"


@pytest.mark.xfail(strict=True, reason="I-07: ledger fallback by local_path attaches Drive metadata of a different sha without a flag")
def test_ledger_path_fallback_flags_sha_mismatch(tmp_path: Path) -> None:
    from openpyxl import Workbook

    src = tmp_path / "ledgered.xlsx"
    wb = Workbook()
    wb.active["A1"] = 1
    wb.save(src)
    state = state_dir(tmp_path)
    (state / "pull_ledger.jsonl").write_text(json.dumps({
        "sha256": "0" * 64, "rel_path": "DD Sign-OFF/30 - Pharmacy/Submitted/ledgered.xlsx",
        "local_path": str(src), "drive_id": "stale-id", "drive_modified": "2020-01-01T00:00:00Z",
    }) + "\n", encoding="utf-8")
    manifest = sx.run_one(src, tmp_path / "out", state)
    assert manifest["sha256"] == sha256_file(src)
    assert manifest["drive_id"] is None or manifest.get("ledger_sha_mismatch") is True


@pytest.mark.xfail(strict=True, reason="I-08: legacy comment recount is by part-name pattern, not by relationship type")
def test_legacy_comment_part_under_another_name_still_passes_check(tmp_path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.comments import Comment

    wb = Workbook()
    ws = wb.active
    ws["A1"] = 1
    ws["A1"].comment = Comment("note", "Ann")
    plain = tmp_path / "plain.xlsx"
    wb.save(plain)
    with zipfile.ZipFile(plain) as zin:
        rels = zin.read("xl/worksheets/_rels/sheet1.xml.rels").decode("utf-8")
        content_types = zin.read("[Content_Types].xml").decode("utf-8")
        comment_part = zin.read("xl/comments/comment1.xml")
    assert "/xl/comments/comment1.xml" in rels
    src = tmp_path / "renamed_part.xlsx"
    rewrite_zip(plain, src, {
        "xl/worksheets/_rels/sheet1.xml.rels": rels.replace("/xl/comments/comment1.xml", "/xl/notes/n1.xml").encode(),
        "[Content_Types].xml": content_types.replace("/xl/comments/comment1.xml", "/xl/notes/n1.xml").encode(),
    }, {"xl/notes/n1.xml": comment_part}, drop=["xl/comments/comment1.xml"])
    out = tmp_path / "out"
    manifest = sx.run_one(src, out, state_dir(tmp_path))
    rows = read_jsonl(out / sx.COMMENTS_NAME)
    assert len(rows) == 1 and rows[0]["text"] == "note"  # openpyxl follows the relationship
    assert check(manifest, "comment rows equal")["pass"] is True  # the recount does not


@pytest.mark.xfail(strict=True, reason="I-12: comments are sorted by cell string, so D10 precedes D2")
def test_comments_sorted_by_cell_position(tmp_path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.comments import Comment

    wb = Workbook()
    ws = wb.active
    for coordinate in ("D10", "D2", "D9"):
        ws[coordinate] = 1
        ws[coordinate].comment = Comment("n " + coordinate, "A")
    src = tmp_path / "order.xlsx"
    wb.save(src)
    out = tmp_path / "out"
    sx.run_one(src, out, state_dir(tmp_path))
    assert [r["cell"] for r in read_jsonl(out / sx.COMMENTS_NAME)] == ["D2", "D9", "D10"]
