"""Tests for ``src.recall_check`` on synthetic Markdown and hand-written sidecar directories."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

import pytest

from src import recall_check as rc

FRONT = "---\nsource_path: x.pptx\ngrade: pending\n---\n"


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #
def test_normalise_number() -> None:
    assert rc.normalise_number("1,200.0") == "1200"
    assert rc.normalise_number("+1,200.50") == "1200.5"
    assert rc.normalise_number("007") == "7"
    assert rc.normalise_number(".5") == "0.5"
    assert rc.normalise_number("-0") == "0"
    assert rc.normalise_number("-5.10") == "-5.1"
    assert rc.normalise_number("12.") == "12"
    assert rc.normalise_number("abc") is None and rc.normalise_number("") is None


def test_numeric_tokens_for_each_kind_agree_on_common_cases() -> None:
    text = "Supply 1,200 L/s at +15 Pa, 32.75 m2, duct 600x300"
    assert {"1200", "15", "32.75"} <= rc.numeric_tokens_for("pdf", text)
    assert {"600", "300"} <= rc.numeric_tokens_for("pdf", text)
    for kind in ("pptx", "xlsx", "email"):
        assert {"1200", "15", "32.75"} <= rc.numeric_tokens_for(kind, text)


def test_word_keys_normalise_numbers_and_punctuation() -> None:
    assert rc.word_key("(1,200.0)") == "1200"
    assert rc.word_key("PH-303,") == "ph-303"
    assert rc.word_key("|") == ""
    assert rc.word_keys("Room PH-303 1,200.0 | 1200") == {"room", "ph-303", "1200"}


# --------------------------------------------------------------------------- #
# Markdown structure
# --------------------------------------------------------------------------- #
def _body(tables_in_section: int, extra_tables_outside: int = 1, register_rows: int = 2) -> str:
    table = "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    parts = [FRONT, "# f\n", "<!-- section: unit -->\n## Page 1\n<!-- method: markitdown -->\n"]
    parts += ["## Tables\n" + table] * extra_tables_outside  # MarkItDown's own heading and table: not counted
    parts.append("### Native text (sidecar)\nfoo\n<!-- section: tables -->\n### Tables\n")
    parts += [f"Table P1-{i}: pdfplumber.\n\n{table}\n" for i in range(tables_in_section)]
    parts.append("<!-- section: annotations -->\n### Annotations and comments\n" + table)
    parts.append("<!-- section: comments-register -->\n## Comments register\n")
    if register_rows:
        parts.append("| index | unit |\n| --- | --- |\n" + "".join(f"| {i} | page 1 |\n" for i in range(register_rows)))
    else:
        parts.append("(none)\n")
    parts.append("<!-- section: provenance -->\n## Provenance\n| a | b |\n| --- | --- |\n| 9999 | 8888 |\n")
    return "".join(parts)


def test_body_text_strips_front_matter_and_provenance() -> None:
    body = rc.body_text(_body(1))
    assert not body.startswith("---") and "source_path" not in body
    assert "9999" not in body and "## Comments register" in body
    assert rc.strip_front_matter("no front matter\n") == "no front matter\n"


def test_table_and_register_counts_follow_section_markers() -> None:
    assert rc.markdown_table_count(rc.body_text(_body(2))) == 2
    assert rc.markdown_table_count(rc.body_text(_body(0, extra_tables_outside=3))) == 0
    assert rc.register_row_count(rc.body_text(_body(1, register_rows=3))) == 3
    assert rc.register_row_count(rc.body_text(_body(1, register_rows=0))) == 0


def test_recall_and_misses() -> None:
    assert rc.recall(set(), {"1"}) == (1.0, [])
    value, misses = rc.recall({"1", "2", "300"}, {"1", "300", "4"})
    assert value == pytest.approx(2 / 3) and misses == ["2"]


# --------------------------------------------------------------------------- #
# Expected values and grading on a synthetic PPTX sidecar
# --------------------------------------------------------------------------- #
def _pptx_sidecar(tmp_path: Path, comments: int = 2) -> Path:
    out = tmp_path / "sidecar"
    out.mkdir()
    slides = [{"slide": 1, "shapes": [{"text": "AHU-07 supply 1,250 L/s"}, {"text": "Room +15 Pa"}]},
              {"slide": 2, "shapes": [{"text": "Room | Area m2 | Pressure Pa\nPharmacy | 32.5 | 45"}]}]
    (out / "slides.jsonl").write_text("".join(json.dumps(s) + "\n" for s in slides), encoding="utf-8")
    (out / "tables.json").write_text(json.dumps({"count": 1, "tables": [{"slide": 2}]}), encoding="utf-8")
    rows = [{"index": i + 1, "slide": 1, "text": f"c{i}"} for i in range(comments)]
    (out / "comments.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (out / "slide_info.json").write_text(json.dumps({"slide_count": 2, "totals": {"text_chars": 60}}), encoding="utf-8")
    (out / "notes.md").write_text("# Speaker notes\n\n## Slide 1\n\nconfirm 1250 with Beca\n", encoding="utf-8")
    return out


def _md(tokens: str, tables: int = 1, register_rows: int = 2) -> str:
    table = "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    md = (f"<!-- section: unit -->\n## Slide 1\n<!-- method: markitdown -->\n{tokens}\n"
          "<!-- section: tables -->\n### Tables\n" + table * tables +
          "<!-- section: annotations -->\n### Annotations and comments\n(none)\n"
          "<!-- section: comments-register -->\n## Comments register\n")
    if register_rows:
        md += "| index | unit |\n| --- | --- |\n" + "".join(f"| {i} | slide 1 |\n" for i in range(register_rows))
    return md


def test_expected_values_from_pptx_sidecar(tmp_path: Path) -> None:
    out = _pptx_sidecar(tmp_path)
    assert rc.expected_tokens("pptx", out) == {"7", "1250", "15", "32.5", "45"}
    assert rc.expected_table_count("pptx", out) == 1
    assert rc.expected_annotation_count("pptx", out) == 2
    assert rc.native_layer("pptx", out) == (True, "2 slides, 60 text chars")


def test_run_checks_grades_a_b_c(tmp_path: Path) -> None:
    out = _pptx_sidecar(tmp_path)
    full = "AHU-07 1250 15 Pa 32.5 45 1250"
    a = rc.run_checks("pptx", out, _md(full), full, threshold=0.995)
    assert a["grade"] == "A" and a["numeric_recall"] == 1.0 and a["tables"]["match"] and a["annotations"]["match"]
    assert all(c["pass"] for c in a["checks"])
    b = rc.run_checks("pptx", out, _md(full), "AHU-07 1250 15 Pa", sidecar_added=3, threshold=0.995)
    assert b["grade"] == "B" and "sidecar supplied 3" in b["grade_reason"]
    b2 = rc.run_checks("pptx", out, _md(full), None, markitdown_error="boom", threshold=0.995)
    assert b2["grade"] == "B" and not b2["markitdown_ok"]
    c = rc.run_checks("pptx", out, _md("AHU-07 1250 15 Pa"), full, threshold=0.995)
    assert c["grade"] == "C" and set(c["misses"]) == {"32.5", "45"} and "numeric recall" in c["grade_reason"]
    c2 = rc.run_checks("pptx", out, _md(full, tables=2), full, threshold=0.995)
    assert c2["grade"] == "C" and "table count" in c2["grade_reason"]
    c3 = rc.run_checks("pptx", out, _md(full, register_rows=1), full, threshold=0.995)
    assert c3["grade"] == "C" and "annotation count" in c3["grade_reason"]
    c4 = rc.run_checks("pptx", out, _md(full), full, sidecar_ok=False, threshold=0.995)
    assert c4["grade"] == "C" and "sidecar" in c4["grade_reason"]


def test_image_and_other_grades(tmp_path: Path) -> None:
    out = tmp_path / "s"
    out.mkdir()
    assert rc.run_checks("image", out, "", "")["grade"] == "pending"
    assert rc.run_checks("other", out, "", "")["grade"] == "C"


def test_write_checks_and_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = _pptx_sidecar(tmp_path)
    full = "AHU-07 1250 15 Pa 32.5 45"
    md_path = tmp_path / "deck.pptx.md"
    md_path.write_text(FRONT + _md(full) + "<!-- section: provenance -->\n## Provenance\n", encoding="utf-8")
    assert rc.main(["run", "--sidecar", str(out), "--md", str(md_path), "--threshold", "0.995"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["kind"] == "pptx" and printed["grade"] == "A"
    written = json.loads((out / rc.CHECKS_NAME).read_text(encoding="utf-8"))
    assert written["grade"] == "A" and written["kind"] == "pptx"
    assert rc.main(["run", "--kind", "pdf", "--sidecar", str(out), "--md", str(tmp_path / "missing.md")]) == 1


def test_kind_for_path() -> None:
    cases: Dict[str, str] = {"a.PDF": "pdf", "b.pptx": "pptx", "c.xlsm": "xlsx", "d.xls": "xlsx", "e.msg": "email",
                             "f.eml": "email", "g.JPG": "image", "h.heic": "image", "i.docx": "other", "j": "other"}
    for name, kind in cases.items():
        assert rc.kind_for_path(name) == kind, name
