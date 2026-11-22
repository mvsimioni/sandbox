"""Schedule of Accommodation (SOA) parser for the AIGH IP ACMV pipeline.

Reads every SOA file listed in ``state/pull_ledger.jsonl`` under ``DD Sign-OFF/``
(PDF text layer through pdfplumber, xlsx through openpyxl cached values), never
a model, and writes

* ``out/registers/soa_rooms.csv``      one row per SOA room line
* ``out/registers/soa_totals.csv``     printed Net / Circulation / Gross lines and the
                                       check that the room lines reproduce them
* ``out/qa/soa_issue_differences.csv`` differences between issues of the same SOA
* ``out/qa/soa_annotations.csv``       FreeText comments (Bluebeam / AH) found on SOA pages
* ``out/qa/soa_report.md``             per file rows parsed, totals check, unparsed pages

Layout (PDF): one table per page, columns KPU | Functional Group (Name) | Code |
Unique Room Identifier | Room Name | As Drawn Area (sqm) [| Remarks].  Column x
positions are read from the header row of each page; rows are anchored on the
unique room identifier (``<unit> NN.NN.NN``), and every word between the
mid-lines to the neighbouring rows is assigned to a column by its x position, so
wrapped cells stay with their row.  The SOA prints one area per room ("As Drawn
Area"); it has no quantity, briefed-area or designed-area column.  Therefore

* ``qty`` is 1 (one row = one uniquely identified room; ``qty_basis`` says so),
* ``unit_area_m2`` (briefed) and ``designed_area_m2`` stay blank,
* ``as_drawn_area_m2`` and ``total_area_m2`` hold the printed As Drawn area.

Unknown stays blank: an area printed as "-" is kept as printed text and has no
number.  Nothing is averaged or repaired; conflicts are listed, not resolved.

Command line::

    python3 -m src.soa_parser run
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import sys
import warnings
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import common

ID_TAIL = re.compile(r"^[A-Z0-9]{2}\.[A-Z0-9]{2,3}\.\d{2}$")
ID_HEAD = re.compile(r"^[A-Z][A-Z0-9]*$")
NUMERIC = re.compile(r"^-?[\d,]*\.?\d+$")
DATE8 = re.compile(r"(?<!\d)(20\d{6})(?!\d)")

ROOM_COLUMNS = [
    "dept_no", "department_folder", "source_file", "issue_folder", "issue_path_segment",
    "issue_date", "soa_version", "file_unit_token", "sheet_title", "unit_zone_code",
    "kpu", "functional_group", "type_code", "page_or_sheet_row", "room_code", "room_name",
    "qty", "qty_basis", "unit_area_m2", "designed_area_m2", "as_drawn_area_m2",
    "total_area_m2", "area_as_printed", "remarks", "raw_cells", "bbox_pt", "method",
    "confidence", "governing", "governing_basis", "soa_group", "inpatient_unit",
]
TOTAL_COLUMNS = [
    "dept_no", "department_folder", "source_file", "issue_folder", "soa_version",
    "sheet_title", "kind", "label_as_printed", "printed_text", "printed_value",
    "location", "room_lines_counted", "computed_from_room_lines", "difference",
    "tolerance", "status", "note", "method", "confidence", "governing",
]
DIFF_COLUMNS = [
    "soa_group", "dept_no", "from_file", "from_issue", "to_file", "to_issue", "change_type",
    "room_code", "field", "from_value", "to_value", "difference", "note",
]
ANNOT_COLUMNS = ["dept_no", "source_file", "issue_folder", "page", "subtype", "rect_pt", "text"]

METHOD_PDF = ("pdfplumber text layer (text-flow words); rows anchored on the unique room "
              "identifier, columns from the page header x positions")
METHOD_PDF_FITZ = ("PyMuPDF text layer words (pdfplumber read no characters); rows anchored on the unique "
                   "room identifier, columns from the page header x positions")
METHOD_XLSX = "openpyxl cell read (cached values), columns from the header row labels"


# --------------------------------------------------------------------------- #
# File selection
# --------------------------------------------------------------------------- #
def is_soa_path(rel_path: str) -> bool:
    segs = rel_path.split("/")
    if not segs or segs[0] != common.ROOT_DD:
        return False
    name = segs[-1]
    folder_hit = any(s.upper().startswith("03_SOA") for s in segs[1:-1])
    name_hit = bool(re.search(r"SOA", name, re.I)) or bool(re.match(r"DD_V16_", name, re.I))
    return folder_hit or name_hit


def classify_candidate(rel_path: str) -> Tuple[bool, str]:
    """Return (is_soa_file, reason).  Room data sheets and equipment lists are excluded."""
    name = rel_path.split("/")[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in ("pdf", "xlsx", "xlsm", "xls"):
        return False, "not a pdf or xlsx"
    if re.match(r"RDS_", name, re.I):
        return False, "Room Data Sheet (RDS) filed in a 03_SOA folder, not a Schedule of Accommodation"
    if re.search(r"\bSoE\b|medical equipment|equipment list", name, re.I):
        return False, "SoE / medical equipment list"
    return True, ""


def list_candidates(root: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    ledger = root / "state" / "pull_ledger.jsonl"
    seen = set()
    keep: List[Dict[str, Any]] = []
    excluded: List[Dict[str, str]] = []
    for rec in common.read_jsonl(ledger):
        rel = rec.get("rel_path", "")
        if rel in seen or not is_soa_path(rel):
            continue
        seen.add(rel)
        ok, why = classify_candidate(rel)
        if not ok:
            excluded.append({"rel_path": rel, "reason": why})
            continue
        local = rec.get("local_path") or ("data/source/" + rel)
        keep.append({"rel_path": rel, "local_path": str(root / local), "sha256": rec.get("sha256", "")})
    keep.sort(key=lambda r: r["rel_path"])
    return keep, excluded


# --------------------------------------------------------------------------- #
# Path metadata
# --------------------------------------------------------------------------- #
def path_meta(rel_path: str) -> Dict[str, Any]:
    segs = rel_path.split("/")
    dept = common.parse_department(rel_path)
    stage = common.parse_stage(rel_path)
    issue_seg = segs[2] if len(segs) >= 4 else ""
    issue_date = ""
    for seg in segs[2:-1]:
        m = DATE8.search(seg)
        if m:
            issue_date = m.group(1)
    if not issue_date:
        m = DATE8.search(segs[-1])
        if m:
            issue_date = m.group(1)
    name = segs[-1]
    stem = name.rsplit(".", 1)[0]
    m = re.search(r"KPU\d+\.[A-Za-z0-9 ]+?(?=(?:_AH|_r \d|$|\s*_))", stem)
    token = m.group(0).strip() if m else ""
    if not token:
        m = re.search(r"(?:v16|V16)[_ ]+(.+)$", stem)
        if m:
            token = re.sub(r"(?i)_?to cons$|_AH$", "", m.group(1)).strip()
        else:
            m = re.search(r"(?:MP-SOA_DD_)([A-Z0-9]+)_v16", stem)
            token = m.group(1) if m else ""
    if not token:
        m = re.match(r"SOA[ _](.+)$", stem)
        token = m.group(1) if m else ""
    ver = ""
    m = re.search(r"[vV](\d{1,2})(?!\d)", stem)
    if m:
        ver = "V" + m.group(1)
    return {
        "dept_no": dept["dept_no"],
        "department_folder": segs[1] if len(segs) > 1 else "",
        "issue_folder": stage,
        "issue_path_segment": issue_seg if len(segs) >= 4 else "(directly in department folder)",
        "issue_date": issue_date,
        "file_unit_token": token,
        "filename_version": ver,
        "is_working": any(re.search(r"working|do not send", s, re.I) for s in segs[2:-1]),
    }


STAGE_RANK = {"Submitted": 0, "other": 1, "AH sign off with comments": 1, "DD Closure": 2}


# --------------------------------------------------------------------------- #
# PDF parsing
# --------------------------------------------------------------------------- #
def _num(text: str) -> Optional[float]:
    t = text.replace(",", "").strip()
    if not NUMERIC.match(t):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _lines_by_top(words: List[Dict[str, Any]], tol: float = 2.0) -> List[List[Dict[str, Any]]]:
    out: List[List[Dict[str, Any]]] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if out and abs(out[-1][0]["top"] - w["top"]) <= tol:
            out[-1].append(w)
        else:
            out.append([w])
    for ln in out:
        ln.sort(key=lambda w: w["x0"])
    return out


def find_header(words: List[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    """Locate the column header row and return the x positions of its columns."""
    for w in sorted(words, key=lambda w: w["top"]):
        if w["text"] != "KPU" or w["x0"] > 120:
            continue
        same = [v for v in words if abs(v["top"] - w["top"]) <= 2.5]
        texts = [v["text"] for v in same]
        if "Functional" not in texts or "Code" not in texts:
            continue
        hdr: Dict[str, float] = {"top": w["top"], "kpu_x": w["x0"]}
        for v in same:
            if v["text"] == "Functional":
                hdr["group_x"] = v["x0"]
            elif v["text"] == "Code":
                hdr["code_x"] = v["x0"]
            elif v["text"] == "Remarks":
                hdr["remarks_x"] = v["x0"]
            elif v["text"] == "As" and "area_x0" not in hdr:
                hdr["area_x0"] = v["x0"]
        for v in same:
            if v["text"] == "Name":
                prev = [p for p in same if p["x1"] <= v["x0"] + 0.5 and p["text"] == "Room"]
                hdr["name_x"] = max(prev, key=lambda p: p["x0"])["x0"] if prev else v["x0"]
        # "Unique Room Identifier": the Unique word sits left of the name column
        for v in same:
            if v["text"] == "Unique":
                hdr["id_x"] = v["x0"]
        # "Remarks" and any header cell that sits on a second header line (not needed)
        need = ("group_x", "code_x", "id_x", "name_x", "area_x0")
        if all(k in hdr for k in need):
            # header bottom: lowest word within 25pt below that belongs to a header cell
            below = [v for v in words if 0 < v["top"] - w["top"] <= 25 and v["text"] in ("Identifier", "Area", "(sqm)", "Drawn")]
            hdr["bottom"] = max([v["bottom"] for v in below] + [w["bottom"]])
            return hdr
    return None


def _title_block(words: List[Dict[str, Any]], header_top: float) -> Dict[str, Any]:
    info: Dict[str, Any] = {"title": "", "version": "", "net": None, "circ": None, "gross": None}
    top_words = [w for w in words if w["top"] < header_top - 0.5]
    for w in top_words:
        if w["text"] == "Version":
            nxt = [v for v in top_words if abs(v["top"] - w["top"]) < 3 and v["x0"] > w["x1"] and v["x0"] < w["x1"] + 30]
            if nxt:
                info["version"] = sorted(nxt, key=lambda v: v["x0"])[0]["text"]
    sched = [w for w in top_words if w["text"] == "SCHEDULE"]
    kind_pos = {}
    for kind, labels in (("net", ("Net", "Area")), ("circ", ("Circulation", "(sqm)")), ("gross", ("Gross", "Area"))):
        for w in top_words:
            if w["text"] != labels[0] or w["x0"] < 300:
                continue
            line = [v for v in top_words if abs(v["top"] - w["top"]) <= 3.5 and v["x0"] >= w["x0"] - 1]
            line.sort(key=lambda v: v["x0"])
            texts = [v["text"] for v in line]
            if "(sqm)" not in texts:
                continue
            idx = texts.index("(sqm)")
            vals = [v for v in line[idx + 1:] if _num(v["text"]) is not None]
            if vals:
                v = vals[0]
                info[kind] = {"text": v["text"], "value": _num(v["text"]),
                              "label": " ".join(texts[: idx + 1]), "bbox": (v["x0"], v["top"], v["x1"], v["bottom"])}
                kind_pos[kind] = w["x0"]
                break
    if sched:
        s_top = min(w["top"] for w in sched)
        cand = [w for w in top_words if w["top"] > s_top + 4 and w["top"] < s_top + 32 and w["text"] != "Detailed"]
        right = min(kind_pos.values()) if kind_pos else 1e9
        title_words = [w for w in cand if w["x1"] < right - 2]
        title_words.sort(key=lambda w: (round(w["top"]), w["x0"]))
        first_top = title_words[0]["top"] if title_words else None
        info["title"] = " ".join(w["text"] for w in title_words if first_top is not None and abs(w["top"] - first_top) <= 2.5)
    return info


def _fitz_words(path: Path, pno: int) -> List[Dict[str, Any]]:
    import fitz

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        doc = fitz.open(str(path))
        try:
            ws = doc[pno - 1].get_text("words")
        finally:
            doc.close()
    out = [{"x0": w[0], "top": w[1], "x1": w[2], "bottom": w[3], "text": w[4]} for w in ws]
    return sorted(out, key=lambda w: (round(w["top"]), w["x0"]))


def parse_pdf(path: Path) -> Dict[str, Any]:
    import fitz  # PyMuPDF, annotations only
    import pdfplumber

    rows: List[Dict[str, Any]] = []
    pages_info: List[Dict[str, Any]] = []
    orphans: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {"title": "", "version": "", "net": None, "circ": None, "gross": None}
    prev_hdr: Optional[Dict[str, float]] = None
    with pdfplumber.open(str(path)) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(use_text_flow=True, x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False)
            method = METHOD_PDF
            fallback_note = ""
            if not words:
                words = _fitz_words(path, pno)
                if words:
                    method = METHOD_PDF_FITZ
                    fallback_note = "pdfplumber found no characters on this page; words read with PyMuPDF"
            hdr = find_header(words)
            pinfo = {"page": pno, "rows": 0, "status": "parsed", "reason": fallback_note}
            if hdr is None:
                if not words:
                    pinfo.update(status="not parsed", reason="no text layer on page")
                    pages_info.append(pinfo)
                    continue
                if prev_hdr is None:
                    pinfo.update(status="not parsed", reason="no table header found on page")
                    pages_info.append(pinfo)
                    continue
                hdr = dict(prev_hdr, top=-1.0, bottom=-1.0)
                pinfo["reason"] = "header row missing; column positions taken from previous page"
            else:
                prev_hdr = hdr
            if pno == 1 or not meta["title"]:
                tb = _title_block(words, hdr["top"] if hdr["top"] > 0 else 1e9)
                for k, v in tb.items():
                    if v and not meta.get(k):
                        meta[k] = v
            page_rows, page_orphans = _page_rows(words, hdr, pno, method)
            for r in page_rows:
                rows.append(r)
            orphans.extend(page_orphans)
            pinfo["rows"] = len(page_rows)
            if not page_rows:
                pinfo.update(status="not parsed", reason=pinfo["reason"] or "table header found but no room rows")
            pages_info.append(pinfo)
        n_pages = len(pdf.pages)
    annots: List[Dict[str, Any]] = []
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            doc = fitz.open(str(path))
            for pno, pg in enumerate(doc, start=1):
                for a in (pg.annots() or []):
                    text = (a.info.get("content") or "").strip()
                    if text:
                        annots.append({"page": pno, "subtype": a.type[1],
                                       "rect": ",".join("%.1f" % v for v in a.rect), "text": text.replace("\r", "\n")})
            doc.close()
    except Exception as exc:  # pragma: no cover - annotation read is best effort
        annots.append({"page": 0, "subtype": "error", "rect": "", "text": "annotation read failed: %s" % exc})
    return {"format": "pdf", "meta": meta, "rows": rows, "pages": pages_info, "orphans": orphans,
            "annotations": annots, "n_pages": n_pages}


def _page_rows(words: List[Dict[str, Any]], hdr: Dict[str, float], pno: int, method: str = METHOD_PDF) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    body = [w for w in words if w["top"] > hdr["bottom"] - 0.5 or hdr["top"] < 0]
    anchors: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for w in body:
        if not ID_TAIL.match(w["text"]):
            continue
        prev = [p for p in body if abs(p["top"] - w["top"]) <= 1.5 and p["x1"] <= w["x0"] + 0.5
                and w["x0"] - p["x1"] < 8 and ID_HEAD.match(p["text"]) and p["x0"] >= hdr["id_x"] - 25]
        if prev:
            anchors.append((max(prev, key=lambda p: p["x1"]), w))
    anchors.sort(key=lambda a: a[1]["top"])
    rows: List[Dict[str, Any]] = []
    tops = [a[1]["top"] for a in anchors]
    anchor_ids = {id(a[0]) for a in anchors} | {id(a[1]) for a in anchors}
    for i, (head, tail) in enumerate(anchors):
        t = tail["top"]
        lo = (tops[i - 1] + t) / 2 if i > 0 else t - 9
        hi = (t + tops[i + 1]) / 2 if i + 1 < len(tops) else t + 9
        if i > 0 and t - tops[i - 1] > 24:
            lo = t - 9
        if i + 1 < len(tops) and tops[i + 1] - t > 24:
            hi = t + 9
        band = [w for w in body if lo - 0.01 <= w["top"] < hi - 0.01]
        rows.append(_assemble_row(band, hdr, head, tail, pno, method))
    # orphan lines: a numeric/dash word in the area column on a line that is not inside any row band
    used = set()
    for r in rows:
        used.update(r["_word_ids"])
    orphans = []
    for ln in _lines_by_top([w for w in body if id(w) not in used]):
        area_like = [w for w in ln if w["x0"] >= hdr["area_x0"] - 15 and (NUMERIC.match(w["text"]) or w["text"] == "-")]
        if area_like and any(w["x0"] < hdr["id_x"] for w in ln):
            orphans.append({"page": pno, "text": " ".join(w["text"] for w in ln)})
    for r in rows:
        r.pop("_word_ids", None)
    return rows, orphans


def _assemble_row(band: List[Dict[str, Any]], hdr: Dict[str, float], head: Dict[str, Any], tail: Dict[str, Any], pno: int, method: str = METHOD_PDF) -> Dict[str, Any]:
    gx, cx, ix, nx = hdr["group_x"], hdr["code_x"], hdr["id_x"], hdr["name_x"]
    rx = hdr.get("remarks_x")
    area_w = None
    area_candidates = [w for w in band if w["x0"] >= hdr["area_x0"] - 25 and (NUMERIC.match(w["text"]) or w["text"] == "-")
                       and (rx is None or w["x1"] <= rx - 1)]
    if area_candidates:
        area_w = max(area_candidates, key=lambda w: w["x1"])
    id_words = {id(head), id(tail)}
    cells: Dict[str, List[Dict[str, Any]]] = {k: [] for k in ("kpu", "group", "code", "id", "name", "remarks", "other")}
    for w in band:
        if id(w) in id_words:
            continue
        if area_w is not None and w is area_w:
            continue
        x = w["x0"]
        if rx is not None and x >= rx - 2:
            cells["remarks"].append(w)
        elif area_w is not None and x > area_w["x1"] + 1:
            cells["other"].append(w)
        elif x < gx - 2:
            cells["kpu"].append(w)
        elif x < cx - 2:
            cells["group"].append(w)
        elif x < ix - 2:
            cells["code"].append(w)
        elif x < nx - 2:
            cells["id"].append(w)
        else:
            cells["name"].append(w)

    def join(ws: List[Dict[str, Any]]) -> str:
        ws = sorted(ws, key=lambda w: (round(w["top"] / 3.0), w["x0"]))
        return " ".join(w["text"] for w in ws)

    code_text = join(cells["code"])
    area_text = area_w["text"] if area_w else ""
    unit = head["text"]
    room_code = "%s %s" % (head["text"], tail["text"])
    extra_id = join(cells["id"])
    all_words = band
    bbox = (min(w["x0"] for w in all_words), min(w["top"] for w in all_words),
            max(w["x1"] for w in all_words), max(w["bottom"] for w in all_words))
    name = join(cells["name"])
    raw = {"KPU": join(cells["kpu"]), "Functional Group": join(cells["group"]), "Code": code_text,
           "Unique Room Identifier": room_code, "Room Name": name, "As Drawn Area (sqm)": area_text}
    if extra_id:
        raw["unassigned_in_id_column"] = extra_id
    if cells["remarks"]:
        raw["Remarks"] = join(cells["remarks"])
    if cells["other"]:
        raw["unassigned_right_of_area"] = join(cells["other"])
    conf = 1.0
    notes: List[str] = []
    if area_w is None:
        conf = 0.5
        notes.append("area not found")
    elif area_text == "-":
        conf = 0.8
        notes.append("area printed as dash")
    if not name:
        conf = min(conf, 0.7)
        notes.append("room name not found")
    if extra_id or cells["other"]:
        conf = min(conf, 0.7)
        notes.append("text outside expected columns kept in raw_cells")
    return {
        "unit_zone_code": unit, "kpu": raw["KPU"], "functional_group": raw["Functional Group"],
        "type_code": code_text, "page_or_sheet_row": "p%d" % pno, "room_code": room_code,
        "room_name": name, "area_text": area_text, "area": _num(area_text) if area_w else None,
        "remarks": raw.get("Remarks", ""), "raw": raw, "bbox": bbox, "confidence": conf,
        "note": "; ".join(notes), "method": method, "_word_ids": {id(w) for w in all_words},
    }


# --------------------------------------------------------------------------- #
# XLSX parsing
# --------------------------------------------------------------------------- #
def _cell_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return repr(v)
    if hasattr(v, "isoformat"):
        return v.date().isoformat() if hasattr(v, "date") else v.isoformat()
    return str(v)


def parse_xlsx(path: Path) -> Dict[str, Any]:
    import openpyxl

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        wb_v = openpyxl.load_workbook(str(path), data_only=True)
        wb_f = openpyxl.load_workbook(str(path), data_only=False)
    rows: List[Dict[str, Any]] = []
    pages: List[Dict[str, Any]] = []
    orphans: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {"title": "", "version": "", "net": None, "circ": None, "gross": None}
    formulas: Dict[str, str] = {}
    for ws in wb_v.worksheets:
        hdr_row = None
        for r in range(1, min(ws.max_row, 40) + 1):
            vals = {c.column: str(c.value).strip() for c in ws[r] if c.value is not None}
            if "Unique Room Identifier" in vals.values() and "Room Name" in vals.values():
                hdr_row = r
                cols = {v: k for k, v in vals.items()}
                break
        if hdr_row is None:
            pages.append({"page": ws.title, "rows": 0, "status": "not parsed",
                          "reason": ("KPU-FPU circulation-factor summary (briefed circulation factor per FPU), no room lines"
                                     if "Circulation" in ws.title else "no room table header (Unique Room Identifier / Room Name) found")})
            continue
        wf = wb_f[ws.title]
        # title block above the header
        for r in range(1, hdr_row):
            for c in ws[r]:
                if c.value is None:
                    continue
                s = str(c.value).strip()
                if s == "Version" or s == "Version ":
                    nxt = [x for x in ws[r] if x.column > c.column and x.value is not None]
                    if nxt:
                        meta["version"] = _cell_text(nxt[0].value)
                for kind, label in (("net", "Net Area"), ("circ", "Circulation (sqm)"), ("gross", "Gross Area")):
                    if s.startswith(label):
                        nxt = [x for x in ws[r] if x.column > c.column and x.value is not None]
                        if nxt:
                            cell = nxt[0]
                            meta[kind] = {"text": _cell_text(cell.value), "value": float(cell.value) if isinstance(cell.value, (int, float)) else None,
                                          "label": s, "bbox": "%s!%s" % (ws.title, cell.coordinate)}
                            fv = wf[cell.coordinate].value
                            if isinstance(fv, str) and fv.startswith("="):
                                formulas[kind] = fv
        # title: first text row after "SCHEDULE OF ACCOMMODATION:"
        for r in range(1, hdr_row):
            vals = [c for c in ws[r] if c.value is not None]
            if vals and str(vals[0].value).strip().startswith("SCHEDULE OF ACCOMMODATION"):
                nxt = [c for c in ws[r + 1] if c.value is not None and not str(c.value).strip().endswith("(sqm)")] if r + 1 < hdr_row else []
                if nxt and not meta["title"]:
                    meta["title"] = str(nxt[0].value).strip()
        n = 0
        for r in range(hdr_row + 1, ws.max_row + 1):
            cell = {lab: ws.cell(row=r, column=col).value for lab, col in cols.items()}
            rid = cell.get("Unique Room Identifier")
            area = cell.get("As Drawn Area (sqm)")
            name = cell.get("Room Name")
            if rid in (None, "") and name in (None, "") and area in (None, ""):
                continue
            if rid in (None, ""):
                orphans.append({"page": "%s row %d" % (ws.title, r), "text": json.dumps({k: _cell_text(v) for k, v in cell.items() if v is not None})})
                continue
            raw = {k: _cell_text(v) for k, v in cell.items()}
            rid_s = str(rid).strip()
            area_n = float(area) if isinstance(area, (int, float)) and not isinstance(area, bool) else None
            area_txt = _cell_text(area)
            conf = 1.0
            notes = []
            if area_n is None:
                conf = 0.5 if area in (None, "") else 0.8
                notes.append("area cell is not numeric")
            rows.append({
                "unit_zone_code": rid_s.split(" ")[0], "kpu": _cell_text(cell.get("KPU")),
                "functional_group": _cell_text(cell.get("Functional Group")),
                "type_code": _cell_text(cell.get("Code")), "page_or_sheet_row": "%s!row %d" % (ws.title, r),
                "room_code": rid_s, "room_name": _cell_text(name), "area_text": area_txt, "area": area_n,
                "remarks": _cell_text(cell.get("Remarks")), "raw": raw, "bbox": "%s!A%d:%s%d" % (ws.title, r, _col(max(cols.values())), r),
                "confidence": conf, "note": "; ".join(notes), "method": METHOD_XLSX,
            })
            n += 1
        pages.append({"page": ws.title, "rows": n, "status": "parsed", "reason": ""})
    meta["formulas"] = formulas
    return {"format": "xlsx", "meta": meta, "rows": rows, "pages": pages, "orphans": orphans, "annotations": [],
            "n_pages": len(wb_v.worksheets)}


def _col(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


# --------------------------------------------------------------------------- #
# Totals check
# --------------------------------------------------------------------------- #
def totals_check(parsed: Dict[str, Any], fmt: str) -> List[Dict[str, Any]]:
    rows = parsed["rows"]
    meta = parsed["meta"]
    nums = [r for r in rows if r["area"] is not None]
    is_circ = lambda r: r["functional_group"].strip().lower() == "circulation"
    circ = sum(r["area"] for r in nums if is_circ(r))
    gross = sum(r["area"] for r in nums)
    net = gross - circ
    n_circ = sum(1 for r in nums if is_circ(r))
    out = []
    computed = {"net": (net, len(nums) - n_circ), "circ": (circ, n_circ), "gross": (gross, len(nums))}
    for kind in ("net", "circ", "gross"):
        p = meta.get(kind)
        label = {"net": "Net Area (sqm)", "circ": "Circulation (sqm)", "gross": "Gross Area (sqm)"}[kind]
        if not p:
            out.append({"kind": kind, "label": label, "printed_text": "", "printed_value": None, "location": "",
                        "n": computed[kind][1], "computed": round(computed[kind][0], 2), "diff": None, "tol": None,
                        "status": "NOT PRINTED", "note": "total line not found in the file"})
            continue
        val, n = computed[kind]
        pv = p["value"]
        if pv is None:
            status, diff, tol = "NOT NUMERIC", None, None
        else:
            diff = val - pv
            if fmt == "pdf":
                n_eff = max(len(nums), 1)
                tol_exact = 0.5
                tol_stat = 0.5 + 3 * 0.0289 * math.sqrt(n_eff)
                tol = round(tol_stat, 2)
                status = "MATCH" if abs(diff) <= tol_exact else ("MATCH WITHIN ROUNDING" if abs(diff) <= tol_stat else "MISMATCH")
            else:
                tol = 0.01
                status = "MATCH" if abs(diff) <= tol else "MISMATCH"
        note = ""
        if fmt == "pdf":
            note = "printed total is rounded to whole m2; room areas are printed to 0.1 m2 (tolerance = 0.5 + 3 sd of the summed rounding error)"
        else:
            f = meta.get("formulas", {}).get(kind)
            if f:
                note = "printed value is the formula %s (cached value read)" % f
        out.append({"kind": kind, "label": p["label"], "printed_text": p["text"], "printed_value": pv,
                    "location": p["bbox"] if isinstance(p["bbox"], str) else "p1 bbox %s" % ",".join("%.1f" % v for v in p["bbox"]),
                    "n": n, "computed": round(val, 2), "diff": None if diff is None else round(diff, 2),
                    "tol": tol, "status": status, "note": note})
    # internal consistency of the printed lines: Net = Gross - Circulation
    if meta.get("net") and meta.get("circ") and meta.get("gross") and None not in (meta["net"]["value"], meta["circ"]["value"], meta["gross"]["value"]):
        d = meta["gross"]["value"] - meta["circ"]["value"] - meta["net"]["value"]
        tol = 1.0 if fmt == "pdf" else 0.01
        out.append({"kind": "printed gross - circulation - net", "label": "internal consistency of the printed lines", "printed_text": "",
                    "printed_value": None, "location": "", "n": None, "computed": round(meta["gross"]["value"] - meta["circ"]["value"], 2),
                    "diff": round(d, 2), "tol": tol, "status": "MATCH" if abs(d) <= tol else "MISMATCH",
                    "note": "printed Gross minus printed Circulation compared with printed Net"})
    return out


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def inpatient_flag(unit: str, title: str) -> str:
    u = unit.upper()
    if re.match(r"^(IW|IC[HU]|SISO|SIW)", u):
        return "yes"
    if re.match(r"^REHC", u):
        return "shared (Rehab - Centralised IPW Shared)"
    return "no"


def parse_file(rec: Dict[str, Any], root: Path) -> Dict[str, Any]:
    rel = rec["rel_path"]
    path = Path(rec["local_path"])
    pm = path_meta(rel)
    result: Dict[str, Any] = {"rel_path": rel, "pm": pm, "error": ""}
    if not path.is_file():
        result.update(error="file missing on disk", rows=[], pages=[], meta={}, orphans=[], annotations=[], format="", n_pages=0)
        return result
    try:
        parsed = parse_pdf(path) if path.suffix.lower() == ".pdf" else parse_xlsx(path)
    except Exception as exc:
        result.update(error="%s: %s" % (type(exc).__name__, exc), rows=[], pages=[], meta={}, orphans=[], annotations=[], format="", n_pages=0)
        return result
    result.update(parsed)
    ver = parsed["meta"].get("version") or ""
    result["soa_version"] = ("V" + re.sub(r"\D", "", ver)) if re.search(r"\d", ver) else pm["filename_version"]
    result["version_printed"] = ver
    result["totals"] = totals_check(parsed, parsed["format"])
    return result


def assign_governing(results: List[Dict[str, Any]]) -> None:
    groups: Dict[Tuple[str, Tuple[str, ...]], List[Dict[str, Any]]] = defaultdict(list)
    for r in results:
        if r["error"] or not r["rows"]:
            r["group"] = ""
            continue
        prefixes = tuple(sorted({row["unit_zone_code"] for row in r["rows"]}))
        r["prefixes"] = prefixes
        groups[(r["pm"]["dept_no"], prefixes)].append(r)
    # files that share department and unit codes but sit in the same issue and format are different SOAs
    # (for example the three Diagnostic Imaging inpatient levels): split such groups by sheet title
    split: Dict[Tuple[str, Tuple[str, ...]], List[Dict[str, Any]]] = {}
    for (dept, pref), lst in groups.items():
        issues = Counter((r["pm"]["issue_folder"], r["pm"]["issue_date"], r["format"]) for r in lst)
        if any(v > 1 for v in issues.values()):
            for r in lst:
                split[(dept, pref + ("title=" + (r["meta"].get("title") or r["pm"]["file_unit_token"]),))] = split.get((dept, pref + ("title=" + (r["meta"].get("title") or r["pm"]["file_unit_token"]),)), []) + [r]
        else:
            split[(dept, pref)] = lst
    groups = split
    # merge groups of the same department whose prefix sets overlap (a revision that added/dropped a unit)
    by_dept: Dict[str, List[Tuple[Tuple[str, ...], List[Dict[str, Any]]]]] = defaultdict(list)
    for (dept, pref), lst in groups.items():
        by_dept[dept].append((pref, lst))
    final: List[Tuple[str, List[Dict[str, Any]]]] = []
    for dept, items in by_dept.items():
        merged: List[Tuple[set, List[Dict[str, Any]]]] = []
        for pref, lst in sorted(items, key=lambda x: -len(x[0])):
            for m in merged:
                if set(pref) & m[0] and (set(pref) <= m[0] or m[0] <= set(pref)):
                    m[0].update(pref)
                    m[1].extend(lst)
                    break
            else:
                merged.append((set(pref), list(lst)))
        for pref, lst in merged:
            final.append(("%s:%s" % (dept, "+".join(sorted(pref))), lst))
    for key, lst in final:
        def sk(r):
            pm = r["pm"]
            return (STAGE_RANK.get(pm["issue_folder"], 1), pm["issue_date"] or "00000000", 0 if pm["is_working"] else 1, 1 if r["format"] == "pdf" else 0)
        lst.sort(key=sk)
        for i, r in enumerate(lst):
            r["group"] = key
            r["issue_order"] = i
        top = lst[-1]
        if len(lst) == 1:
            top["governing"], top["governing_basis"] = "yes", "only issue on disk"
            continue
        top_key = (sk(top)[0], sk(top)[1])
        same_issue = [r for r in lst if (sk(r)[0], sk(r)[1]) == top_key]
        for r in lst:
            r["governing"], r["governing_basis"] = "no", "superseded by a later issue"
        if len(same_issue) == 1:
            top["governing"] = "yes"
            top["governing_basis"] = "latest issue (%s %s) of %d on disk" % (top["pm"]["issue_folder"], top["pm"]["issue_date"] or "undated", len(lst))
        else:
            pick = same_issue[-1]
            pick["governing"] = "yes"
            pick["governing_basis"] = ("latest issue; same-issue variants (%s) - chosen by non-working path, then pdf over xlsx; the variants are compared in soa_issue_differences.csv"
                                       % ", ".join(sorted({r['format'] for r in same_issue})))
            for r in same_issue:
                if r is not pick:
                    r["governing_basis"] = "same issue as the governing file; alternate format/working copy"
    # explicit dated-tie rule: an xlsx dated by file name and an undated pdf in the same folder stay as chosen above


def room_rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    pm = result["pm"]
    out = []
    for r in result["rows"]:
        area = r["area"]
        out.append({
            "dept_no": pm["dept_no"], "department_folder": pm["department_folder"], "source_file": result["rel_path"],
            "issue_folder": pm["issue_folder"], "issue_path_segment": pm["issue_path_segment"], "issue_date": pm["issue_date"],
            "soa_version": result["soa_version"], "file_unit_token": pm["file_unit_token"],
            "sheet_title": result["meta"].get("title", ""), "unit_zone_code": r["unit_zone_code"], "kpu": r["kpu"],
            "functional_group": r["functional_group"], "type_code": r["type_code"], "page_or_sheet_row": r["page_or_sheet_row"],
            "room_code": r["room_code"], "room_name": r["room_name"],
            "qty": 1, "qty_basis": "derived: one SOA row per uniquely identified room (no qty column printed)",
            "unit_area_m2": "", "designed_area_m2": "",
            "as_drawn_area_m2": "" if area is None else area, "total_area_m2": "" if area is None else area,
            "area_as_printed": r["area_text"], "remarks": r["remarks"],
            "raw_cells": json.dumps(r["raw"], ensure_ascii=False),
            "bbox_pt": ",".join("%.1f" % v for v in r["bbox"]) if isinstance(r["bbox"], (tuple, list)) else r["bbox"],
            "method": r["method"] + (" | " + r["note"] if r["note"] else ""), "confidence": r["confidence"],
            "governing": result.get("governing", ""), "governing_basis": result.get("governing_basis", ""),
            "soa_group": result.get("group", ""), "inpatient_unit": inpatient_flag(r["unit_zone_code"], result["meta"].get("title", "")),
        })
    return out


def totals_rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    pm = result["pm"]
    out = []
    for t in result.get("totals", []):
        out.append({
            "dept_no": pm["dept_no"], "department_folder": pm["department_folder"], "source_file": result["rel_path"],
            "issue_folder": pm["issue_folder"], "soa_version": result["soa_version"], "sheet_title": result["meta"].get("title", ""),
            "kind": t["kind"], "label_as_printed": t["label"], "printed_text": t["printed_text"],
            "printed_value": "" if t["printed_value"] is None else t["printed_value"], "location": t["location"],
            "room_lines_counted": "" if t["n"] is None else t["n"], "computed_from_room_lines": t["computed"],
            "difference": "" if t["diff"] is None else t["diff"], "tolerance": "" if t["tol"] is None else t["tol"],
            "status": t["status"], "note": t["note"],
            "method": METHOD_PDF if result["format"] == "pdf" else METHOD_XLSX, "confidence": 1.0 if t["status"] != "NOT PRINTED" else 0.0,
            "governing": result.get("governing", ""),
        })
    return out


# --------------------------------------------------------------------------- #
# Issue differences
# --------------------------------------------------------------------------- #
def _keyed(rows: List[Dict[str, Any]], dup_codes: set) -> Dict[Tuple[str, str, int], Dict[str, Any]]:
    """Key rows by room code; where a code is printed more than once, also by room name."""
    seen: Counter = Counter()
    out = {}
    for r in rows:
        name = r["room_name"].strip().lower() if r["room_code"] in dup_codes else ""
        seen[(r["room_code"], name)] += 1
        out[(r["room_code"], name, seen[(r["room_code"], name)])] = r
    return out


def issue_differences(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in results:
        if r.get("group"):
            groups[r["group"]].append(r)
    out: List[Dict[str, Any]] = []
    for lst in groups.values():
        lst.sort(key=lambda r: r.get("issue_order", 0))
    for key, lst in sorted(groups.items()):
        if len(lst) < 2:
            continue
        for a, b in zip(lst, lst[1:]):
            base = {"soa_group": key, "dept_no": a["pm"]["dept_no"], "from_file": a["rel_path"],
                    "from_issue": "%s %s" % (a["pm"]["issue_folder"], a["pm"]["issue_date"]), "to_file": b["rel_path"],
                    "to_issue": "%s %s" % (b["pm"]["issue_folder"], b["pm"]["issue_date"])}
            same_issue = (a["pm"]["issue_folder"], a["pm"]["issue_date"]) == (b["pm"]["issue_folder"], b["pm"]["issue_date"])
            if same_issue:
                base["note"] = "same issue folder: format or working-copy variants (%s vs %s)" % (a["format"], b["format"])
            elif a["format"] != b["format"]:
                base["note"] = "formats differ (%s vs %s): area rounding differences up to 0.05 m2 are expected" % (a["format"], b["format"])
            else:
                base["note"] = ""
            for kind in ("net", "circ", "gross"):
                pa, pb = a["meta"].get(kind), b["meta"].get(kind)
                va = pa["value"] if pa else None
                vb = pb["value"] if pb else None
                if va != vb:
                    cross = a["format"] != b["format"]
                    small = va is not None and vb is not None and abs(vb - va) <= (0.5 + 1e-9 if cross else 0)
                    out.append(dict(base, change_type="printed total rounding only" if small else "printed total changed", room_code="", field=kind,
                                    from_value="" if pa is None else pa["text"], to_value="" if pb is None else pb["text"],
                                    difference="" if va is None or vb is None else round(vb - va, 2)))
            start = len(out)
            dup = {c for c, v in Counter(r["room_code"] for r in a["rows"] + b["rows"]).items() if v > 2 or (v == 2 and Counter(r["room_code"] for r in a["rows"])[c] == 2) or Counter(r["room_code"] for r in b["rows"])[c] == 2}
            ka, kb = _keyed(a["rows"], dup), _keyed(b["rows"], dup)
            for k in sorted(set(ka) | set(kb)):
                ra, rb = ka.get(k), kb.get(k)
                code = k[0] if not k[1] and k[2] == 1 else "%s (%s%s)" % (k[0], k[1] or "occurrence", " #%d" % k[2] if k[2] > 1 or not k[1] else "")
                if ra is None:
                    out.append(dict(base, change_type="room added", room_code=code, field="room_name", from_value="",
                                    to_value=rb["room_name"], difference=rb["area"] if rb["area"] is not None else "", note=base["note"]))
                    continue
                if rb is None:
                    out.append(dict(base, change_type="room removed", room_code=code, field="room_name", from_value=ra["room_name"],
                                    to_value="", difference=-ra["area"] if ra["area"] is not None else "", note=base["note"]))
                    continue
                for fld, lab in (("room_name", "room_name"), ("type_code", "code"), ("functional_group", "functional_group"), ("kpu", "kpu"), ("remarks", "remarks")):
                    if ra[fld].strip() != rb[fld].strip():
                        out.append(dict(base, change_type="%s changed" % lab, room_code=code, field=lab, from_value=ra[fld],
                                        to_value=rb[fld], difference="", note=base["note"]))
                if ra["area_text"] != rb["area_text"]:
                    if ra["area"] is not None and rb["area"] is not None:
                        d = rb["area"] - ra["area"]
                        ctype = "area changed" if abs(d) > 0.05 + 1e-9 else "area rounding only"
                        out.append(dict(base, change_type=ctype, room_code=code, field="as_drawn_area_m2", from_value=ra["area_text"],
                                        to_value=rb["area_text"], difference=round(d, 2), note=base["note"]))
                    else:
                        out.append(dict(base, change_type="area changed", room_code=code, field="as_drawn_area_m2", from_value=ra["area_text"],
                                        to_value=rb["area_text"], difference="", note=base["note"]))
            if len(out) == start:
                out.append(dict(base, change_type="no differences found", room_code="", field="", from_value="", to_value="", difference="",
                                note=(base["note"] + "; " if base["note"] else "") + "%d room lines compared, printed totals, room codes, names, codes, groups, areas and remarks identical" % len(a["rows"])))
    return out


# --------------------------------------------------------------------------- #
# Anomalies and report
# --------------------------------------------------------------------------- #
def anomalies(result: Dict[str, Any]) -> List[str]:
    out = []
    rows = result["rows"]
    ids = Counter(r["room_code"] for r in rows)
    dup = [k for k, v in ids.items() if v > 1]
    if dup:
        out.append("duplicate unique room identifiers (kept, not resolved): %s" % ", ".join("%s x%d" % (k, ids[k]) for k in dup[:12]) + (" ..." if len(dup) > 12 else ""))
    zero = [r["room_code"] for r in rows if r["area"] == 0 or r["area_text"] == "-"]
    if zero:
        out.append("%d room lines with area 0 or '-' (kept as printed): %s" % (len(zero), ", ".join(zero[:10]) + (" ..." if len(zero) > 10 else "")))
    by_name: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r["area"] and r["functional_group"].strip().lower() != "circulation":
            by_name[r["room_name"]].append(r)
    for name, lst in by_name.items():
        if len(lst) >= 4:
            med = statistics.median(x["area"] for x in lst)
            for x in lst:
                if med > 0 and x["area"] > 4 * med and x["area"] - med > 20:
                    out.append("possible area outlier (not corrected): %s '%s' %s m2 vs median %.1f m2 of %d rooms of that name" % (x["room_code"], name, x["area_text"], med, len(lst)))
    if result.get("orphans"):
        out.append("%d table-like line(s) not parsed into a room row: %s" % (len(result["orphans"]), "; ".join("%s: %s" % (o["page"], o["text"][:80]) for o in result["orphans"][:5])))
    return out


def write_report(path: Path, results: List[Dict[str, Any]], excluded: List[Dict[str, str]], all_rooms: List[Dict[str, Any]]) -> None:
    L: List[str] = []
    L.append("# SOA parser report\n")
    ok = [r for r in results if not r["error"]]
    L.append("Files parsed: %d of %d SOA candidates (%d pdf, %d xlsx). Room lines: %d. Excluded non-SOA files: %d.\n" % (
        len(ok), len(results), sum(1 for r in ok if r["format"] == "pdf"), sum(1 for r in ok if r["format"] == "xlsx"),
        len(all_rooms), len(excluded)))
    L.append("Method: PDF text layer (pdfplumber) and xlsx cached cell values; no model, no OCR. One SOA row = one room: `qty` is 1 (derived), "
             "`unit_area_m2` (briefed) and `designed_area_m2` are blank because the SOA prints only an As Drawn area per room.\n")
    # per department
    L.append("## Per department\n")
    L.append("| Dept | Department | Files | Room lines (all issues) | Room lines (governing) | of which inpatient units (governing) | Totals checks MATCH / WITHIN ROUNDING / MISMATCH / NOT PRINTED |")
    L.append("|---|---|---|---|---|---|---|")
    dep: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"files": 0, "rows": 0, "gov": 0, "ip": 0, "st": Counter(), "name": ""})
    for r in ok:
        d = dep[r["pm"]["dept_no"]]
        d["files"] += 1
        d["rows"] += len(r["rows"])
        d["name"] = r["pm"]["department_folder"]
        if r.get("governing") == "yes":
            d["gov"] += len(r["rows"])
            d["ip"] += sum(1 for x in r["rows"] if inpatient_flag(x["unit_zone_code"], "") == "yes")
        for t in r["totals"]:
            if t["kind"] in ("net", "circ", "gross"):
                d["st"][t["status"]] += 1
    for k in sorted(dep):
        d = dep[k]
        L.append("| %s | %s | %d | %d | %d | %d | %d / %d / %d / %d |" % (k, d["name"], d["files"], d["rows"], d["gov"], d["ip"], d["st"]["MATCH"],
                                                                 d["st"]["MATCH WITHIN ROUNDING"], d["st"]["MISMATCH"], d["st"]["NOT PRINTED"]))
    L.append("\nTotals check: each file prints Net, Circulation and Gross area (sqm). Circulation = room lines whose functional group is "
             "'Circulation'; Gross = all room lines; Net = Gross - Circulation. PDF totals are printed rounded to whole m2 and rows to 0.1 m2, so "
             "a difference within 0.5 m2 is MATCH and one within 0.5 + 3 sd of the summed rounding error is MATCH WITHIN ROUNDING; "
             "anything larger is MISMATCH and is listed below.\n")
    # mismatches
    mism = [(r, t) for r in ok for t in r["totals"] if t["status"] not in ("MATCH", "MATCH WITHIN ROUNDING")]
    L.append("## Totals mismatches and gaps\n")
    if not mism:
        L.append("None.\n")
    else:
        L.append("| File | Line | Printed | Computed from rows | Difference | Status |")
        L.append("|---|---|---|---|---|---|")
        for r, t in mism:
            L.append("| %s | %s | %s | %s | %s | %s |" % (r["rel_path"].replace("DD Sign-OFF/", ""), t["kind"], t["printed_text"], t["computed"],
                                                      "" if t["diff"] is None else t["diff"], t["status"]))
        L.append("")
        L.append("Gross area by unit code (sum of the room lines, for diagnosing the mismatching files; nothing is corrected):\n")
        for r in sorted({id(r): r for r, _ in mism}.values(), key=lambda r: r["rel_path"]):
            sums: Dict[str, float] = defaultdict(float)
            for row in r["rows"]:
                if row["area"] is not None:
                    sums[row["unit_zone_code"]] += row["area"]
            if len(sums) > 1:
                L.append("- %s: %s" % (r["rel_path"].replace("DD Sign-OFF/", ""), ", ".join("%s %.1f" % (k, v) for k, v in sorted(sums.items()))))
        L.append("")
    # per file
    L.append("## Per file\n")
    L.append("| File | Issue | Gov. | Pages/sheets parsed | Rows | Net / Circ / Gross check | Notes |")
    L.append("|---|---|---|---|---|---|---|")
    for r in results:
        short = r["rel_path"].replace("DD Sign-OFF/", "")
        if r["error"]:
            L.append("| %s | %s |  | 0 | 0 |  | NOT PARSED: %s |" % (short, r["pm"]["issue_folder"], r["error"]))
            continue
        pg = r["pages"]
        parsed_pg = sum(1 for p in pg if p["status"] == "parsed")
        chk = " / ".join(next((t["status"] for t in r["totals"] if t["kind"] == k), "-") for k in ("net", "circ", "gross"))
        notes = []
        for p in pg:
            if p["status"] != "parsed":
                notes.append("page/sheet %s not parsed: %s" % (p["page"], p["reason"]))
            elif p["reason"]:
                notes.append("page %s: %s" % (p["page"], p["reason"]))
        notes += anomalies(r)
        if r["annotations"]:
            notes.append("%d FreeText comment(s) on the pages, see soa_annotations.csv" % len(r["annotations"]))
        L.append("| %s | %s %s | %s | %d of %d | %d | %s | %s |" % (short, r["pm"]["issue_folder"], r["pm"]["issue_date"], r.get("governing", ""),
                                                                 parsed_pg, r["n_pages"], len(r["rows"]), chk, "<br>".join(n.replace("|", "/") for n in notes)))
    L.append("\n## Excluded from the SOA set\n")
    for e in excluded:
        L.append("- %s: %s" % (e["rel_path"], e["reason"]))
    errs = [r for r in results if r["error"]]
    L.append("\n## Unparsed\n")
    unparsed = [(r, p) for r in ok for p in r["pages"] if p["status"] != "parsed"]
    if not errs and not unparsed:
        L.append("All pages of all SOA files produced room rows.\n")
    for r in errs:
        L.append("- %s: %s" % (r["rel_path"], r["error"]))
    for r, p in unparsed:
        L.append("- %s, page/sheet %s: %s" % (r["rel_path"], p["page"], p["reason"]))
    L.append("\n## Unit codes that appear in more than one department folder\n")
    owners: Dict[str, set] = defaultdict(set)
    for r in ok:
        for pref in {x["unit_zone_code"] for x in r["rows"]}:
            owners[pref].add(r["pm"]["dept_no"])
    multi = {k: v for k, v in owners.items() if len(v) > 1}
    if not multi:
        L.append("None.")
    for k, v in sorted(multi.items()):
        files = sorted({r["rel_path"].replace("DD Sign-OFF/", "") for r in ok if any(x["unit_zone_code"] == k for x in r["rows"])})
        L.append("- %s in departments %s (both kept and both marked governing within their own department; the copies are not reconciled): %s" % (k, ", ".join(sorted(v)), "; ".join(files)))
    L.append("\n## Scope notes\n")
    L.append("- The SOA carries no level or storey column; `sheet_title` (where it names a level, e.g. L10) and `unit_zone_code` are kept as printed. "
             "`inpatient_unit` is yes for unit codes IW*, IC*, SISO*; REHC* is the shared rehab block of the inpatient wards. Level assignment must come from the stacking diagram / grid.")
    L.append("- Where the same SOA sits in more than one issue folder all copies are kept; `governing` = yes on the latest issue "
             "(rank Submitted < AH sign off / other comment issue < DD Closure, then the date in the folder or file name). Differences are in out/qa/soa_issue_differences.csv.")
    common.atomic_write_text(path, "\n".join(L) + "\n")


def run(root: Optional[Path] = None) -> Dict[str, Any]:
    root = root or common.project_root()
    cands, excluded = list_candidates(root)
    results = [parse_file(c, root) for c in cands]
    assign_governing(results)
    rooms: List[Dict[str, Any]] = []
    totals: List[Dict[str, Any]] = []
    annots: List[Dict[str, Any]] = []
    for r in results:
        if r["error"]:
            continue
        rooms.extend(room_rows(r))
        totals.extend(totals_rows(r))
        for a in r["annotations"]:
            annots.append({"dept_no": r["pm"]["dept_no"], "source_file": r["rel_path"], "issue_folder": r["pm"]["issue_folder"],
                           "page": a["page"], "subtype": a["subtype"], "rect_pt": a["rect"], "text": a["text"]})
    diffs = issue_differences(results)
    out_reg = root / "out" / "registers"
    out_qa = root / "out" / "qa"
    common.write_csv_dicts(out_reg / "soa_rooms.csv", rooms, ROOM_COLUMNS)
    common.write_csv_dicts(out_reg / "soa_totals.csv", totals, TOTAL_COLUMNS)
    common.write_csv_dicts(out_qa / "soa_issue_differences.csv", diffs, DIFF_COLUMNS)
    common.write_csv_dicts(out_qa / "soa_annotations.csv", annots, ANNOT_COLUMNS)
    write_report(out_qa / "soa_report.md", results, excluded, rooms)
    return {"files": len(results), "rooms": len(rooms), "totals": len(totals), "differences": len(diffs),
            "excluded": len(excluded), "results": results}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Schedule of Accommodation parser (text layer / cells, no model).")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="parse every SOA file in the pull ledger and write the registers and QA files")
    sp = sub.add_parser("parse", help="parse one file and print JSON summary")
    sp.add_argument("file")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "run":
        res = run()
        print(json.dumps({k: v for k, v in res.items() if k != "results"}))
        return 0
    if args.cmd == "parse":
        rec = {"rel_path": args.file, "local_path": args.file}
        r = parse_file(rec, common.project_root())
        print(json.dumps({"rows": len(r["rows"]), "meta": {k: v for k, v in r["meta"].items() if k != "formulas"}, "totals": r.get("totals"),
                          "pages": r["pages"], "error": r["error"]}, default=str, indent=1))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
