"""Tests for ``src.sidecar_xlsx`` on a small synthetic workbook.

The workbook is built with openpyxl (two sheets, one hidden, formulas and
values, merged cells, legacy comments, fills, a data validation, a
conditional format, a defined name, frozen panes, a hidden row and column).
Threaded comments and a fake VBA project are then injected into the package
zip by hand, because openpyxl cannot write either.  The legacy ``.xls`` case
converts the same workbook with LibreOffice when it is installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Dict, List

import pytest

from src import sidecar_xlsx as sx
from src.common import read_csv_dicts, read_jsonl, sha256_file

PERSON_1 = "{AAAA1111-0000-0000-0000-000000000001}"
PERSON_2 = "{AAAA1111-0000-0000-0000-000000000002}"
ROOT_ID = "{CCCC0000-0000-0000-0000-000000000001}"
REPLY_ID = "{CCCC0000-0000-0000-0000-000000000002}"
TC_NS = "http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments"
PERSONS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<personList xmlns="{TC_NS}">'
    f'<person displayName="Bob Modern" id="{PERSON_1}" userId="bob" providerId="AD"/>'
    f'<person displayName="Cara Reply" id="{PERSON_2}" userId="cara" providerId="AD"/>'
    '</personList>'
)
THREADED_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<ThreadedComments xmlns="{TC_NS}">'
    f'<threadedComment ref="D2" dT="2026-03-02T10:00:00.00" personId="{PERSON_1}" id="{ROOT_ID}">'
    '<text>Root: confirm 45 Pa for D2</text></threadedComment>'
    f'<threadedComment ref="D2" dT="2026-03-02T11:00:00.00" personId="{PERSON_2}" id="{REPLY_ID}" parentId="{ROOT_ID}">'
    '<text>Reply: agreed, 45 Pa</text></threadedComment>'
    '</ThreadedComments>'
)
TC_REL = ('<Relationship Id="rIdTc1" Type="http://schemas.microsoft.com/office/2017/10/relationships/threadedComment"'
          ' Target="../threadedComments/threadedComment1.xml"/>')
PERSON_REL = ('<Relationship Id="rIdPers" Type="http://schemas.microsoft.com/office/2017/10/relationships/person"'
              ' Target="persons/person.xml"/>')
TC_CT = ('<Override PartName="/xl/threadedComments/threadedComment1.xml"'
         ' ContentType="application/vnd.ms-excel.threadedcomments+xml"/>'
         '<Override PartName="/xl/persons/person.xml" ContentType="application/vnd.ms-excel.person+xml"/>')


def build_workbook(path: Path) -> None:
    """Build the synthetic workbook with openpyxl."""
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.formatting.rule import CellIsRule
    from openpyxl.styles import PatternFill
    from openpyxl.workbook.defined_name import DefinedName
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    ws = wb.active
    ws.title = "Loads"
    ws.append(["Room", "Area m2", "Total", "Status"])
    ws.append(["Ward 1", 12.5, "=B2*2", "Yes"])
    ws.append(["Ward 2", 1250, "=B3*2", "No"])
    ws["A5"] = "Merged title"
    ws.merge_cells("A5:C5")
    ws["B2"].comment = Comment("check 12.5", "Alice")
    ws["D2"].comment = Comment("[Threaded comment]\n\nYour version of Excel ...", "Bob Modern")
    ws["A2"].fill = PatternFill("solid", fgColor="FFFF0000")
    ws["A3"].fill = PatternFill("solid", fgColor="FFFF0000")
    ws["B3"].fill = PatternFill("solid", fgColor="FF00FF00")
    ws["B2"].number_format = "0.00"
    ws["B3"].number_format = "#,##0"
    dv = DataValidation(type="list", formula1='"Yes,No"', allow_blank=True)
    ws.add_data_validation(dv)
    dv.add("D2:D10")
    ws.conditional_formatting.add("B2:B10", CellIsRule(operator="greaterThan", formula=["100"],
                                                       fill=PatternFill("solid", fgColor="FFFFFF00")))
    ws.freeze_panes = "A2"
    ws.row_dimensions[4].hidden = True
    ws.column_dimensions["E"].hidden = True
    hidden = wb.create_sheet("Hidden Data (2)")
    hidden["A1"] = "secret"
    hidden["B1"] = 7
    hidden["C1"] = "=B1+1"
    hidden.sheet_state = "hidden"
    wb.defined_names["TotalArea"] = DefinedName("TotalArea", attr_text="Loads!$B$2")
    wb.save(path)


def rewrite_zip(src: Path, dst: Path, replace: Dict[str, bytes], add: Dict[str, bytes]) -> None:
    """Copy a package zip, replacing and adding parts."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            zout.writestr(item, replace.get(item.filename, zin.read(item.filename)))
        for name, data in add.items():
            zout.writestr(name, data)


def inject_threaded(src: Path, dst: Path, link: bool = True) -> None:
    """Add threaded comments and persons to the package (linked from sheet1 unless ``link`` is False)."""
    with zipfile.ZipFile(src) as zin:
        sheet_rels = zin.read("xl/worksheets/_rels/sheet1.xml.rels").decode("utf-8")
        wb_rels = zin.read("xl/_rels/workbook.xml.rels").decode("utf-8")
        content_types = zin.read("[Content_Types].xml").decode("utf-8")
    replace = {
        "xl/_rels/workbook.xml.rels": wb_rels.replace("</Relationships>", PERSON_REL + "</Relationships>").encode(),
        "[Content_Types].xml": content_types.replace("</Types>", TC_CT + "</Types>").encode(),
    }
    if link:
        replace["xl/worksheets/_rels/sheet1.xml.rels"] = sheet_rels.replace(
            "</Relationships>", TC_REL + "</Relationships>").encode()
    rewrite_zip(src, dst, replace, {"xl/threadedComments/threadedComment1.xml": THREADED_XML.encode(),
                                    "xl/persons/person.xml": PERSONS_XML.encode()})


def inject_fake_vba(src: Path, dst: Path) -> None:
    """Add a bogus ``xl/vbaProject.bin`` so the extraction path runs without any real macro."""
    with zipfile.ZipFile(src) as zin:
        content_types = zin.read("[Content_Types].xml").decode("utf-8")
    ct = content_types.replace("</Types>", '<Override PartName="/xl/vbaProject.bin"'
                               ' ContentType="application/vnd.ms-office.vbaProject"/></Types>')
    rewrite_zip(src, dst, {"[Content_Types].xml": ct.encode()}, {"xl/vbaProject.bin": b"not an OLE file at all"})


@pytest.fixture(scope="module")
def book(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Path]:
    """Build the fixtures once and run the module on the threaded workbook."""
    base = tmp_path_factory.mktemp("xlsx")
    plain, threaded, macro = base / "plain.xlsx", base / "threaded.xlsx", base / "macro.xlsm"
    build_workbook(plain)
    inject_threaded(plain, threaded)
    inject_fake_vba(plain, macro)
    state = base / "state"
    state.mkdir()
    (state / "pull_ledger.jsonl").write_text(json.dumps({
        "sha256": sha256_file(threaded), "rel_path": "DD Sign-OFF/30 - Pharmacy/Submitted/threaded.xlsx",
        "drive_id": "drive-threaded", "drive_modified": "2026-03-01T00:00:00Z", "transport": "connector",
    }) + "\n", encoding="utf-8")
    out, progress = base / "out", base / "progress.json"
    rc = sx.main(["run", "--xlsx", str(threaded), "--out", str(out), "--state-dir", str(state),
                  "--progress", str(progress)])
    assert rc == 0
    return {"plain": plain, "threaded": threaded, "macro": macro, "state": state, "out": out,
            "progress": progress, "base": base}


def _manifest(out: Path) -> Dict:
    return json.loads((out / sx.MANIFEST_NAME).read_text(encoding="utf-8"))


def _check(manifest: Dict, prefix: str) -> Dict:
    return next(c for c in manifest["checks"] if c["check"].startswith(prefix))


def test_numeric_tokens_and_value_text() -> None:
    assert sx.numeric_tokens("1,250.5 L/s at 45 Pa, AHU-07, R2, .5") == ["1250.5", "45", "07", ".5"]
    assert sx.numeric_tokens(12.0) == ["12"] and sx.numeric_tokens(None) == [] and sx.numeric_tokens(True) == ["1"]
    assert sx.value_text(1250.0) == "1250" and sx.value_text(12.5) == "12.5"
    assert sx.value_type("#REF!") == "e" and sx.value_type("text") == "s" and sx.value_type(3) == "n"


def test_safe_sheet_stem_is_unique() -> None:
    taken: Dict[str, int] = {}
    assert sx.safe_sheet_stem("Loads", taken) == "Loads"
    assert sx.safe_sheet_stem("loads", taken) == "loads_2"
    assert sx.safe_sheet_stem("Hidden Data (2)", taken) == "Hidden_Data_2"
    assert sx.safe_sheet_stem("///", taken) == "sheet"


def test_values_and_formulas_csv(book: Dict[str, Path]) -> None:
    out = book["out"]
    values = {r["cell"]: r for r in read_csv_dicts(out / "Loads_values.csv")}
    assert values["B2"] == {"cell": "B2", "value": "12.5", "type": "n"}
    assert values["B3"]["value"] == "1250" and values["A5"]["value"] == "Merged title"
    assert values["C2"]["value"] == "" and values["C2"]["type"] == ""  # openpyxl saves no cached value
    formulas = {r["cell"]: r["formula"] for r in read_csv_dicts(out / "Loads_formulas.csv")}
    assert formulas == {"C2": "=B2*2", "C3": "=B3*2"}
    # the hidden sheet is exported too, under a sanitised file name
    hidden_values = {r["cell"]: r["value"] for r in read_csv_dicts(out / "Hidden_Data_2_values.csv")}
    assert hidden_values == {"A1": "secret", "B1": "7", "C1": ""}
    assert [r["formula"] for r in read_csv_dicts(out / "Hidden_Data_2_formulas.csv")] == ["=B1+1"]


def test_workbook_json(book: Dict[str, Path]) -> None:
    wb = json.loads((book["out"] / sx.WORKBOOK_NAME).read_text(encoding="utf-8"))
    assert wb["sheet_count"] == 2 and wb["has_vba"] is False and wb["converted_from"] is None
    loads, hidden = wb["sheets"]
    assert loads["name"] == "Loads" and loads["state"] == "visible" and loads["frozen_panes"] == "A2"
    assert loads["dimensions"] == "A1:D5" and loads["formula_cells"] == 2 and loads["cached_values_missing"] == 2
    assert loads["files"] == {"values": "Loads_values.csv", "formulas": "Loads_formulas.csv"}
    assert loads["numeric_tokens"] == 4  # Ward 1, Ward 2, 12.5, 1250 (m2 is glued to a letter)
    assert hidden["name"] == "Hidden Data (2)" and hidden["state"] == "hidden"
    assert hidden["files"]["values"] == "Hidden_Data_2_values.csv"
    assert wb["defined_names"] == [{"name": "TotalArea", "scope": "workbook", "value": "Loads!$B$2", "hidden": False}]
    assert wb["external_links"]["present"] is False
    assert wb["threaded_comment_parts"]["threaded_parts"] == ["xl/threadedComments/threadedComment1.xml"]
    assert wb["threaded_comment_parts"]["persons"] == 2


def test_merged_hidden_formats_fills(book: Dict[str, Path]) -> None:
    out = book["out"]
    merged = json.loads((out / sx.MERGED_NAME).read_text(encoding="utf-8"))
    assert merged["Loads"] == [{"range": "A5:C5", "origin": "A5", "rows": 1, "cols": 3, "value": "Merged title"}]
    assert merged["Hidden Data (2)"] == []
    hidden = json.loads((out / sx.HIDDEN_NAME).read_text(encoding="utf-8"))
    assert hidden["Loads"] == {"state": "visible", "rows": [4], "cols": ["E"]}
    assert hidden["Hidden Data (2)"]["state"] == "hidden"
    formats = json.loads((out / sx.FORMATS_NAME).read_text(encoding="utf-8"))
    assert formats["Loads"]["B"]["formats"] == {"General": 1, "0.00": 1, "#,##0": 1}
    assert formats["Loads"]["B"]["sampled"] == 3 and formats["Loads"]["A"]["dominant"] == "General"
    fills = json.loads((out / sx.FILLS_NAME).read_text(encoding="utf-8"))
    red = next(f for f in fills["Loads"] if f["colour"] == "rgb:FFFF0000")
    assert red["count"] == 2 and [e["cell"] for e in red["examples"]] == ["A2", "A3"] and red["fill_type"] == "solid"
    assert {f["colour"] for f in fills["Loads"]} == {"rgb:FFFF0000", "rgb:FF00FF00"}


def test_validations_and_conditional_formats(book: Dict[str, Path]) -> None:
    out = book["out"]
    validations = json.loads((out / sx.VALIDATIONS_NAME).read_text(encoding="utf-8"))
    (dv,) = validations["Loads"]
    assert dv["sqref"] == "D2:D10" and dv["type"] == "list" and dv["formula1"] == '"Yes,No"' and dv["allow_blank"]
    conditional = json.loads((out / sx.CONDITIONAL_NAME).read_text(encoding="utf-8"))
    (cf,) = conditional["Loads"]
    assert cf["sqref"] == "B2:B10"
    (rule,) = cf["rules"]
    assert rule["type"] == "cellIs" and rule["operator"] == "greaterThan" and rule["formula"] == ["100"]
    assert rule["fill"] == "rgb:FFFFFF00"


def test_comments_legacy_and_threaded(book: Dict[str, Path]) -> None:
    rows = read_jsonl(book["out"] / sx.COMMENTS_NAME)
    assert [r["index"] for r in rows] == [1, 2, 3, 4]
    assert all(r["sheet"] == "Loads" for r in rows)
    legacy = [r for r in rows if r["kind"] == "legacy"]
    assert [(r["cell"], r["author"], r["text"], r["threaded_shadow"]) for r in legacy] == [
        ("B2", "Alice", "check 12.5", False),
        ("D2", "Bob Modern", "[Threaded comment]\n\nYour version of Excel ...", True),
    ]
    threaded = [r for r in rows if r["kind"] == "threaded"]
    root, reply = threaded
    assert root["cell"] == "D2" and root["author"] == "Bob Modern" and root["parent_id"] is None
    assert root["date"] == "2026-03-02T10:00:00.00" and root["text"] == "Root: confirm 45 Pa for D2"
    assert reply["author"] == "Cara Reply" and reply["parent_id"] == ROOT_ID and reply["comment_id"] == REPLY_ID
    assert reply["part"] == "xl/threadedComments/threadedComment1.xml"


def test_unlinked_threaded_part_is_reported(book: Dict[str, Path], tmp_path: Path) -> None:
    unlinked = tmp_path / "unlinked.xlsx"
    inject_threaded(book["plain"], unlinked, link=False)
    manifest = sx.run_one(unlinked, tmp_path / "out", book["state"])
    rows = read_jsonl(tmp_path / "out" / sx.COMMENTS_NAME)
    assert sum(1 for r in rows if r["kind"] == "threaded" and r["sheet"] is None) == 2
    assert _check(manifest, "every threaded comment resolved")["pass"] is False
    assert _check(manifest, "comment rows equal")["pass"] is True


def test_manifest_counts_and_checks(book: Dict[str, Path]) -> None:
    manifest = _manifest(book["out"])
    assert manifest["module"] == "sidecar_xlsx" and manifest["sha256"] == sha256_file(book["threaded"])
    assert manifest["rel_path"] == "DD Sign-OFF/30 - Pharmacy/Submitted/threaded.xlsx"
    assert manifest["drive_id"] == "drive-threaded" and manifest["drive_modified"] == "2026-03-01T00:00:00Z"
    assert {c["name"] for c in manifest["converters"]} == {"openpyxl", "lxml"}
    assert manifest["methods_used"] == ["openpyxl", "package-zip-threaded-comments"]
    counts = manifest["counts"]
    assert counts["sheet_count"] == 2 and counts["hidden_sheets"] == 1 and counts["comment_count"] == 4
    assert counts["legacy_comments"] == 2 and counts["threaded_comments"] == 2 and counts["defined_names"] == 1
    assert counts["merged_ranges"] == 1 and counts["validations"] == 1 and counts["conditional_formats"] == 1
    assert counts["formula_cells"] == 3 and counts["cached_values_missing"] == 3
    assert all((book["out"] / f).is_file() for f in manifest["files"])
    assert sorted(f for f in manifest["files"] if f.endswith(".csv")) == [
        "Hidden_Data_2_formulas.csv", "Hidden_Data_2_values.csv", "Loads_formulas.csv", "Loads_values.csv"]
    failed = [c["check"] for c in manifest["checks"] if not c["pass"]]
    assert failed == ["cached value present for every formula cell"]  # openpyxl writes no cached values
    assert _check(manifest, "comment rows equal")["evidence"] == "legacy 2/2, threaded 2/2"
    assert _check(manifest, "merged ranges")["pass"] is True
    progress = json.loads(book["progress"].read_text(encoding="utf-8"))
    assert progress["items"][f"sidecar_xlsx:{manifest['sha256']}"]["status"] == "done"


def test_rerun_is_skipped_and_force_rewrites(book: Dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    args = ["run", "--xlsx", str(book["threaded"]), "--out", str(book["out"]), "--state-dir", str(book["state"]),
            "--no-progress"]
    before = (book["out"] / sx.MANIFEST_NAME).read_text(encoding="utf-8")
    assert sx.main(args) == 0
    assert capsys.readouterr().out.startswith("skip (done)")
    assert (book["out"] / sx.MANIFEST_NAME).read_text(encoding="utf-8") == before
    (book["out"] / "Loads_values.csv").unlink()
    assert sx.main(args) == 0  # a missing output invalidates the done state
    assert (book["out"] / "Loads_values.csv").is_file() and capsys.readouterr().out.startswith("done:")
    assert sx.main(args + ["--force"]) == 0
    assert capsys.readouterr().out.startswith("done:")


def test_default_out_dir_from_ledger(book: Dict[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(sx.PATHS, "sidecar", tmp_path / "sidecar")
    assert sx.main(["run", "--xlsx", str(book["threaded"]), "--state-dir", str(book["state"]), "--no-progress"]) == 0
    target = tmp_path / "sidecar" / "DD Sign-OFF/30 - Pharmacy/Submitted/threaded.xlsx"
    assert (target / sx.MANIFEST_NAME).is_file() and (target / "Loads_values.csv").is_file()


def test_cli_failures(book: Dict[str, Path], tmp_path: Path) -> None:
    assert sx.main(["run", "--xlsx", str(tmp_path / "missing.xlsx"), "--out", str(tmp_path), "--no-progress"]) == 1
    wrong = tmp_path / "x.txt"
    wrong.write_text("x")
    assert sx.main(["run", "--xlsx", str(wrong), "--out", str(tmp_path), "--no-progress"]) == 1
    empty_state = tmp_path / "state"
    empty_state.mkdir()
    assert sx.main(["run", "--xlsx", str(book["plain"]), "--state-dir", str(empty_state), "--no-progress"]) == 1
    corrupt = tmp_path / "corrupt.xlsx"
    corrupt.write_bytes(b"not a zip")
    progress = tmp_path / "progress.json"
    assert sx.main(["run", "--xlsx", str(corrupt), "--out", str(tmp_path / "o"), "--progress", str(progress)]) == 1
    record = json.loads(progress.read_text(encoding="utf-8"))["items"][f"sidecar_xlsx:{sha256_file(corrupt)}"]
    assert record["status"] == "error" and record["error"]


def test_xlsm_fake_vba_is_extracted_but_not_read(book: Dict[str, Path], tmp_path: Path) -> None:
    out = tmp_path / "macro_out"
    manifest = sx.run_one(book["macro"], out, book["state"])
    wb = json.loads((out / sx.WORKBOOK_NAME).read_text(encoding="utf-8"))
    assert wb["has_vba"] is True and wb["source"]["kind"] == "xlsm"
    vba = wb["vba"]
    assert vba["present"] is True and vba["read"] is False and vba["modules"] == []
    assert "not an OLE container" in vba["reason"] or "not installed" in vba["reason"]
    assert (out / "vba" / "vbaProject.bin").read_bytes() == b"not an OLE file at all"
    assert "vba/vbaProject.bin" in manifest["files"] and "vba/vba.json" in manifest["files"]
    assert _check(manifest, "vbaProject.bin extracted")["pass"] is False
    assert "oletools-olevba" not in manifest["methods_used"]
    # the rest of the workbook is still fully extracted
    assert manifest["counts"]["cells_non_empty"] == 16 and manifest["counts"]["comment_count"] == 2


def test_vba_dump_without_project(book: Dict[str, Path], tmp_path: Path) -> None:
    record = sx.dump_vba(book["plain"], tmp_path)
    assert record["present"] is False and record["files"] == [] and "no xl/vbaProject.bin" in record["reason"]


@pytest.mark.skipif(not Path(sx.SOFFICE).is_file(), reason="LibreOffice not installed")
def test_legacy_xls_is_converted(book: Dict[str, Path], tmp_path: Path) -> None:
    profile = (tmp_path / "profile").resolve().as_uri()
    subprocess.run([sx.SOFFICE, "--headless", "--norestore", f"-env:UserInstallation={profile}", "--convert-to",
                    "xls", "--outdir", str(tmp_path), str(book["plain"])], check=True, capture_output=True,
                   timeout=sx.SOFFICE_TIMEOUT_S)
    xls = tmp_path / "plain.xls"
    assert xls.is_file()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    out = tmp_path / "xls_out"
    assert sx.main(["run", "--xlsx", str(xls), "--out", str(out), "--state-dir", str(book["state"]),
                    "--scratch", str(scratch), "--no-progress"]) == 0
    manifest = _manifest(out)
    assert manifest["sha256"] == sha256_file(xls) and manifest["source_path"] == str(xls)
    assert "soffice-xls-conversion" in manifest["methods_used"]
    assert any(c["name"] == "soffice" for c in manifest["converters"])
    wb = json.loads((out / sx.WORKBOOK_NAME).read_text(encoding="utf-8"))
    conv = wb["converted_from"]
    assert conv["ok"] is True and conv["tool"] == "soffice" and Path(conv["converted_path"]).suffix == ".xlsx"
    assert Path(conv["scratch_dir"]).parent == scratch and conv["source"] == str(xls)
    assert wb["source"]["kind"] == "xls" and wb["sheet_count"] == 2
    # LibreOffice writes cached values, so the formula check passes on the converted copy
    values = {r["cell"]: r["value"] for r in read_csv_dicts(out / "Loads_values.csv")}
    assert values["C2"] == "25" and values["B3"] == "1250"
    assert _check(manifest, "cached value present")["pass"] is True
    assert _check(manifest, "legacy .xls converted")["pass"] is True
    assert sorted(f for f in manifest["files"] if f.endswith("_values.csv")) == [
        "Hidden_Data_2_values.csv", "Loads_values.csv"]
    assert wb["sheets"][1]["state"] == "hidden"


def test_xls_conversion_failure_is_reported(book: Dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bogus = tmp_path / "bogus.xls"
    bogus.write_bytes(b"not a workbook")
    monkeypatch.setattr(sx, "SOFFICE", str(tmp_path / "no-such-soffice"))
    with pytest.raises(RuntimeError):
        sx.run_one(bogus, tmp_path / "out", book["state"], tmp_path)
    assert sx.main(["run", "--xlsx", str(bogus), "--out", str(tmp_path / "out2"), "--no-progress",
                    "--scratch", str(tmp_path)]) == 1
