"""ETTV report parser (decision D-042).

Source: ``data/source/Short Version DD Booklet/20260129_AIGH_ETTV_Report.pdf`` (67 pages, a print of
the Excel ETTV workbook in the BCA "BCA-BP-FETTV" format).  Text layer only (PyMuPDF words with
positions for the column tables, ``pdftotext -layout`` for the per-facade calculation sheets).  No
value is read from an image: the images in the file are the signature stamp and logos (page 1),
a logo strip (page 2) and decorative glass-section thumbnails (Appendix 2 sheets); none carries a
number the text layer lacks.  Unknown stays blank; conflicts are listed in the report, not resolved.

CLI::

    python3 -m src.ettv_parser run [--pdf PATH] [--out-root DIR]

Page map (verified by script, see ``PAGE_KINDS``):

* 1       summary form: ETTV per facade and the building average
* 2       U-value computation: wall and glazing types W1-W3, F1 (constructions)
* 3-10    Appendix 1 (BCA-BP-FETTV-A1): ETTV calculation per orientation N S E W NE NW SE SW
* 11-18   Appendix 2 (A2): envelope areas per orientation
* 19-26   Appendix 3 (A3): opaque wall particulars per orientation
* 27-34   Appendix 4 (A4): fenestration particulars (SC1, SC2, shading method) per orientation
* 35-67   shading calculation per facade module (FT type, orientation, SC2, sun-vector table)
"""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

DOC = "data/source/Short Version DD Booklet/20260129_AIGH_ETTV_Report.pdf"
DOC_NAME = "20260129_AIGH_ETTV_Report.pdf"
OUT_FACADES = "out/registers/ettv_facades.csv"
OUT_CONSTR = "out/registers/ettv_constructions.csv"
OUT_NOTES = "out/registers/ettv_notes.csv"
REPORT = "out/qa/ettv_report.md"

ORIENTATIONS = ["N", "S", "E", "W", "NE", "NW", "SE", "SW"]  # order of the sheets (A1..A4 pages)
FACADE_ORDER_P1 = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]  # Facade 1..8 on the summary form
WORD_TO_ORIENT = {"North": "N", "South": "S", "East": "E", "West": "W", "NorthEast": "NE",
                  "NorthWest": "NW", "SouthEast": "SE", "SouthWest": "SW"}
PAGE_KINDS = (
    [(1, "summary")] + [(2, "u_values")]
    + [(3 + i, "A1") for i in range(8)] + [(11 + i, "A2") for i in range(8)]
    + [(19 + i, "A3") for i in range(8)] + [(27 + i, "A4") for i in range(8)]
    + [(p, "module") for p in range(35, 68)]
)
METHOD_TEXT = "text layer (pdftotext -layout, regex)"
METHOD_WORDS = "text layer (PyMuPDF words, column assignment by x position)"

FACADE_FIELDS = [
    "record_id", "row_type", "block", "facade_no", "orientation", "storey_or_zone", "item_code",
    "description", "gross_wall_area_m2", "opaque_wall_area_m2", "window_area_m2", "item_area_m2",
    "wwr", "wwr_incl_spandrel", "wall_u_w_m2k", "wall_u_area_weighted", "glazing_u_w_m2k",
    "sc1", "sc2", "sc_product", "sc_area_weighted", "shading_method", "correction_factor_cf",
    "gross_heat_gain_w", "ettv_w_m2", "n_sun_vectors", "document", "page", "location",
    "text_as_printed", "method", "confidence", "evidence_other_pages", "notes",
]
CONSTR_FIELDS = [
    "record_id", "type_code", "kind", "description", "orientation", "components", "total_r_m2k_w",
    "u_value_w_m2k", "weight_kg_m2", "sc1", "sc2", "sc_product", "shading_method", "k_density_source",
    "document", "page", "location", "text_as_printed", "method", "confidence", "notes",
]
NOTE_FIELDS = ["note_id", "category", "item", "value", "document", "page", "location",
               "text_as_printed", "method", "confidence", "notes"]


# --------------------------------------------------------------------------- text access

def pdf_page_texts(pdf: Path) -> List[str]:
    """One ``pdftotext -layout`` string per page (form feed separated)."""
    out = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], check=True, capture_output=True).stdout
    pages = out.decode("utf-8", "replace").split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


def pdf_page_words(pdf: Path) -> List[List[Tuple[float, float, float, float, str]]]:
    try:
        import pymupdf as fitz  # type: ignore
    except ImportError:  # pragma: no cover
        import fitz  # type: ignore
    doc = fitz.open(str(pdf))
    return [[(w[0], w[1], w[2], w[3], w[4]) for w in p.get_text("words")] for p in doc]


def _num(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _fmt(v: Optional[float], nd: int = 6) -> str:
    if v is None:
        return ""
    return ("%.*f" % (nd, v)).rstrip("0").rstrip(".") if "." in ("%.*f" % (nd, v)) else str(v)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------- page 1

def parse_summary(text: str) -> Dict[str, Any]:
    facades = []
    for m in re.finditer(r"Facade\s+(\d)\s+facing\s+(\w+)\s+ETTV\s*=\s*([\d.]+)\s*W/m", text):
        facades.append({"facade_no": int(m.group(1)), "orientation": m.group(2),
                        "ettv": float(m.group(3)), "printed": _clean(m.group(0))})
    avg = re.search(r"average ETTV of the building is\s+([\d.]+)\s*W/m", text)
    ref = re.search(r"Project Ref\. No:\s+(\S+)", text)
    date = re.search(r"Date:\s+(\d{1,2} \w{3} \d{4})", text)
    return {"facades": facades, "average": float(avg.group(1)) if avg else None,
            "average_printed": _clean(avg.group(0)) if avg else "",
            "project_ref": ref.group(1) if ref else "", "cert_date": date.group(1) if date else ""}


# --------------------------------------------------------------------------- page 2

def _group_lines(words, y_tol: float = 3.0):
    rows: List[List[Any]] = []
    for w in sorted(words, key=lambda w: (round(w[1], 1), w[0])):
        if rows and abs(rows[-1][0] - w[1]) <= y_tol:
            rows[-1][1].append(w)
        else:
            rows.append([w[1], [w]])
    return [(y, sorted(ws, key=lambda w: w[0])) for y, ws in rows]


def parse_u_values(words) -> List[Dict[str, Any]]:
    """Construction types from the U-value computation sheet, by column x position.

    Columns (x0): type code and description < 137, components 138, thickness 253, K 281, R 313, density 344,
    total R ~365, U-value 403, layer weight 437, composite weight 455-475, remarks 482, location 530.
    The table ends above the page footer (y 575)."""
    body = [w for w in words if 200 < w[1] < 575]
    codes = sorted((w[1], w[4]) for w in body if w[0] < 100 and re.fullmatch(r"[WF]\d", w[4]))
    types = []
    for i, (y0, code) in enumerate(codes):
        y1 = codes[i + 1][0] if i + 1 < len(codes) else 575
        ws = [w for w in body if y0 - 3 <= w[1] < y1 - 3]
        comps = []
        for y, line in _group_lines([w for w in ws if 137 <= w[0] < 460]):
            left = _clean(" ".join(w[4] for w in line if w[0] < 250))
            nums = [(round(w[0]), w[4]) for w in line if w[0] >= 250]
            comps.append((left, nums))
        u_val = rt = weight = sc1 = None
        for y, line in _group_lines([w for w in ws if w[0] >= 137]):
            label = _clean(" ".join(x[4] for x in line if x[0] < 250))
            for w in line:
                v = _num(w[4])
                if v is None:
                    continue
                if 395 <= w[0] <= 420:
                    if "Shading" in label:
                        sc1 = v
                    else:
                        u_val = v
                elif 360 <= w[0] < 395:
                    rt = v
                elif 455 <= w[0] <= 475:
                    weight = v
        desc = _clean(" ".join(w[4] for w in sorted(ws, key=lambda w: (round(w[1]), w[0])) if 75 <= w[0] < 137))
        remarks = _clean(" ".join(w[4] for w in sorted(ws, key=lambda w: (round(w[1]), w[0])) if 475 <= w[0] < 528))
        loc = _clean(" ".join(w[4] for w in sorted(ws, key=lambda w: (round(w[1]), w[0])) if w[0] >= 528))
        types.append({"code": code, "components": comps, "u": u_val, "rt": rt, "weight": weight, "sc1": sc1,
                      "desc": desc, "remarks": remarks, "location": loc, "words": ws})
    return types


# --------------------------------------------------------------------------- A1 pages (layout text)

_NUM = r"(-?\d+(?:\.\d+)?)"


def parse_a1(text: str) -> Dict[str, Any]:
    orient = re.search(r"FACADE ORIENTATION\s+(\w+)", text)
    walls = []
    in_fen = False
    buf: List[str] = []
    fens = []
    wall_sub = fen_sub = None
    for line in text.splitlines():
        if "FENESTRATION" in line and "Subtotal" not in line:
            in_fen = True
            continue
        if not in_fen:
            m = re.match(r"^\s*(W\d)\s+(.+?)\s{2,}" + _NUM + r"\s+" + _NUM + r"\s+" + _NUM + r"\s*$", line)
            if m:
                walls.append({"code": m.group(1), "desc": _clean(m.group(2)), "area": float(m.group(3)),
                              "u": float(m.group(4)), "heat": float(m.group(5)), "printed": _clean(line)})
                continue
            m = re.match(r"^\s*Subtotal\s+" + _NUM + r"\s+" + _NUM + r"\s*$", line)
            if m and wall_sub is None:
                wall_sub = {"area": float(m.group(1)), "heat": float(m.group(2)), "printed": _clean(line)}
            continue
        if re.match(r"^\s*Subtotal", line):
            m = re.match(r"^\s*Subtotal\s+" + _NUM + r"\s+" + _NUM + r"\s+" + _NUM + r"\s*$", line)
            if m:
                fen_sub = {"area": float(m.group(1)), "h1": float(m.group(2)), "h2": float(m.group(3)),
                           "printed": _clean(line)}
            buf = []
            continue
        if "Gross Area" in line:
            break
        m = re.search(_NUM + r"\s+" + _NUM + r"\s+" + _NUM + r"\s+" + _NUM + r"\s+" + _NUM + r"\s*$", line)
        buf.append(line)
        if m:
            block = " ".join(buf)
            code = re.search(r"F1\(SC(\d+)", block)
            desc_lines = [re.sub(r"F1\(SC\d+\)?", "", re.sub(_NUM + r"(\s+" + _NUM + r"){4}\s*$", "", l)) for l in buf
                          if not re.search(r"#nf\.|SC=SC1|S/No\.|\bCF\b|^\s*C2\s*$", l)]
            fens.append({"sc_index": int(code.group(1)) if code else None,
                         "desc": _clean(" ".join(desc_lines).replace(")", " ")),
                         "af": float(m.group(1)), "uf": float(m.group(2)), "sc": float(m.group(3)),
                         "h1": float(m.group(4)), "h2": float(m.group(5)),
                         "printed": _clean(" | ".join(_clean(l) for l in buf if not re.search(r"#nf\.|SC=SC1|S/No\.|\bCF\b|^\s*C2\s*$", l) and l.strip()))})
            buf = []
    cf = re.search(r"\bCF\s+" + _NUM, text)
    ao = re.search(r"Gross Area of External Walls \(Ao\):\s+" + _NUM, text)
    ggh = re.search(r"Gross Heat Gain:\s+" + _NUM, text)
    ettv = re.search(r"ETTV = Gross Heat Gain/\s+" + _NUM, text)
    return {"orientation": orient.group(1) if orient else None, "walls": walls, "wall_sub": wall_sub,
            "fens": fens, "fen_sub": fen_sub,
            "cf": float(cf.group(1)) if cf else None, "cf_printed": _clean(cf.group(0)) if cf else "",
            "ao": float(ao.group(1)) if ao else None, "ao_printed": _clean(ao.group(0)) if ao else "",
            "gross_heat": float(ggh.group(1)) if ggh else None,
            "gross_heat_printed": _clean(ggh.group(0)) if ggh else "",
            "ettv": float(ettv.group(1)) if ettv else None,
            "ettv_printed": _clean(ettv.group(0)) if ettv else ""}


# --------------------------------------------------------------------------- A2 / A3 / A4 (words)

def _orient_from_words(words) -> Optional[str]:
    txt = " ".join(w[4] for w in sorted(words, key=lambda w: (round(w[1]), w[0])))
    m = re.search(r"ORIENTATION\s+(\w+)\s+\(N,S", txt) or re.search(r"facing:\s+(\w+)\s+\(N,S", txt)
    return m.group(1) if m else None


def parse_a2(words) -> Dict[str, Any]:
    """Envelope area per item code (W1.., F1(SCn)); area words are right-aligned at x > 480."""
    items = []
    codes = [w for w in words if w[0] < 125 and re.fullmatch(r"(W\d|F1\(SC\d+\))", w[4])]
    areas = [w for w in words if w[0] > 480 and w[1] > 250 and re.fullmatch(r"\d+(\.\d+)?", w[4])]
    for c in codes:
        best = min(areas, key=lambda a: abs(a[1] - c[1]), default=None)
        if best is not None and abs(best[1] - c[1]) <= 14:
            items.append({"code": c[4], "area": float(best[4]), "printed": f"{c[4]} ... {best[4]}"})
    return {"orientation": _orient_from_words(words), "items": items}


def _label_y(words, label: str, nth: int = 0, xmax: float = 200) -> Optional[float]:
    ys = sorted(w[1] for w in words if w[4] == label and w[0] < xmax)
    return ys[nth] if len(ys) > nth else None


def _columns(words, header_re: str, y_lo: float, y_hi: float):
    """Column headers as (left edge x0, label), left to right."""
    heads = [w for w in words if re.fullmatch(header_re, w[4]) and y_lo <= w[1] <= y_hi]
    heads.sort(key=lambda w: w[0])
    return [(w[0], w[4]) for w in heads]


def _assign(cols, w, offset: float) -> Optional[int]:
    """Column index of a word: column i spans [x0_i + offset, x0_(i+1) + offset), the last one is open.

    Data centres sit about 40 pt right of the header centre (headers are left aligned over wider
    cells), so the headers' left edges plus a fixed offset are the boundaries; the last column is wider."""
    cx = (w[0] + w[2]) / 2
    if not cols or cx < cols[0][0] + offset:
        return None
    idx = None
    for i, (x0, _) in enumerate(cols):
        if cx >= x0 + offset:
            idx = i
    return idx


def parse_a3(words) -> Dict[str, Any]:
    cols = _columns(words, r"W\d", 190, 230)
    ys = {k: _label_y(words, k) for k in ("Material", "Thermal", "Weight", "Area")}
    ys_m = [w[1] for w in words if w[4] == "Materials" and w[0] < 200]
    ys_m.sort()
    out = {c[1]: {} for c in cols}
    body = [w for w in words if w[0] > 150 and 215 <= w[1] < 760]
    bands = {
        "material": (ys["Material"] - 3, ys_m[0] - 3 if ys_m else 255),
        "internal_finish": ((ys_m[0] - 3) if ys_m else 255, (ys_m[1] - 3) if len(ys_m) > 1 else 305),
        "external_finish": ((ys_m[1] - 3) if len(ys_m) > 1 else 305, (ys_m[2] - 3) if len(ys_m) > 2 else 345),
        "insulation": ((ys_m[2] - 3) if len(ys_m) > 2 else 345, ys["Thermal"] - 3),
        "u": (ys["Thermal"] - 3, ys["Weight"] - 3),
        "weight": (ys["Weight"] - 3, ys["Area"] - 3),
        "area": (ys["Area"] - 3, 600),
    }
    for field, (lo, hi) in bands.items():
        for w in body:
            if lo <= w[1] < hi:
                i = _assign(cols, w, -5.0)
                if i is not None:
                    d = out[cols[i][1]]
                    d[field] = (d.get(field, "") + " " + w[4]).strip()
    return {"orientation": _orient_from_words(words), "cols": out}


def parse_a4(words) -> Dict[str, Any]:
    cols = _columns(words, r"F1\(SC\d+\)", 200, 260)
    y_type = _label_y(words, "Type")
    y_therm = _label_y(words, "Themal")
    y_shade = [w[1] for w in words if w[4] == "Shading" and w[0] < 150]
    y_shade.sort()
    y_meth = _label_y(words, "Method")
    y_area = _label_y(words, "Area", xmax=150)
    out = {c[1]: {} for c in cols}
    body = [w for w in words if w[0] > 180 and 255 <= w[1] < 700]
    bands = {
        "glass": (y_type - 3, y_therm - 3),
        "uf": (y_therm - 3, y_shade[0] - 3),
        "sc1": (y_shade[0] - 3, y_meth - 3),
        "shading_method": (y_meth - 3, y_shade[1] - 3),
        "sc2": (y_shade[1] - 3, y_area - 3),
        "area": (y_area - 3, 700),
    }
    for field, (lo, hi) in bands.items():
        for w in sorted(body, key=lambda w: (round(w[1] / 2), w[0])):
            if lo <= w[1] < hi:
                i = _assign(cols, w, 3.0)
                if i is not None:
                    d = out[cols[i][1]]
                    d[field] = (d.get(field, "") + " " + w[4]).strip()
    return {"orientation": _orient_from_words(words), "cols": out}


# --------------------------------------------------------------------------- module pages

def parse_module(text: str) -> Dict[str, Any]:
    m = re.search(r"Fa[cç]ade Module\s+(\S+)\s+Orientation\s+(\w+)\s+SC2\s+" + _NUM, text)
    t = re.search(r"ETTV Report Fa[cç]ade Type\s+(F1\(SC\d+\))", text)
    rows = re.findall(r"\[[-\d.]+,\s*[-\d.]+,\s*[-\d.]+\]\s+" + _NUM + r"\s+" + _NUM + r"\s+" + _NUM + r"\s+" + _NUM, text)
    glass = sorted({float(r[1]) for r in rows})
    return {"module": m.group(1) if m else None, "orientation_word": m.group(2) if m else None,
            "sc2": float(m.group(3)) if m else None, "f1_type": t.group(1) if t else None,
            "n_vectors": len(rows), "glass_areas": glass,
            "printed": _clean(m.group(0)) if m else ""}


# --------------------------------------------------------------------------- derived values

def derive(a1: Dict[str, Any]) -> Dict[str, Optional[float]]:
    ao = a1["ao"]
    wall_a = a1["wall_sub"]["area"] if a1["wall_sub"] else None
    fen_a = a1["fen_sub"]["area"] if a1["fen_sub"] else None
    w1 = next((w["area"] for w in a1["walls"] if w["code"] == "W1"), 0.0)
    wwr = fen_a / ao if ao and fen_a is not None else None
    wwr2 = (fen_a + w1) / ao if ao and fen_a is not None else None
    uw = (sum(w["area"] * w["u"] for w in a1["walls"]) / wall_a) if wall_a else None
    afs = sum(f["af"] for f in a1["fens"])
    scw = (sum(f["af"] * f["sc"] for f in a1["fens"]) / afs) if afs else None
    return {"wwr": wwr, "wwr_incl_spandrel": wwr2, "uw": uw, "sc_w": scw, "wall_area": wall_a, "fen_area": fen_a}


def check_a1(a1: Dict[str, Any]) -> List[str]:
    """Arithmetic checks of one calculation sheet; returns the list of mismatches (empty = ok)."""
    bad = []
    tol = 0.01
    wall_heat = sum(w["heat"] for w in a1["walls"])
    if a1["wall_sub"] and abs(wall_heat - a1["wall_sub"]["heat"]) > 0.05 * max(1, len(a1["walls"])):
        bad.append(f"wall heat rows sum {wall_heat:.3f} vs subtotal {a1['wall_sub']['heat']}")
    wall_area = sum(w["area"] for w in a1["walls"])
    if a1["wall_sub"] and abs(wall_area - a1["wall_sub"]["area"]) > 0.01:
        bad.append(f"wall area rows sum {wall_area:.3f} vs subtotal {a1['wall_sub']['area']}")
    af = sum(f["af"] for f in a1["fens"])
    if a1["fen_sub"] and abs(af - a1["fen_sub"]["area"]) > 0.02:
        bad.append(f"window area rows sum {af:.3f} vs subtotal {a1['fen_sub']['area']}")
    if a1["ao"] is not None and a1["wall_sub"] and a1["fen_sub"]:
        if abs(a1["wall_sub"]["area"] + a1["fen_sub"]["area"] - a1["ao"]) > 0.05:
            bad.append("Ao differs from opaque + window area")
    if a1["gross_heat"] is not None and a1["wall_sub"] and a1["fen_sub"]:
        gh = a1["wall_sub"]["heat"] + a1["fen_sub"]["h1"] + a1["fen_sub"]["h2"]
        if abs(gh - a1["gross_heat"]) > max(1.0, gh * 1e-4):
            bad.append(f"gross heat gain {a1['gross_heat']} vs sum of subtotals {gh:.1f}")
    if a1["gross_heat"] and a1["ao"] and a1["ettv"] is not None:
        if abs(a1["gross_heat"] / a1["ao"] - a1["ettv"]) > 0.005:
            bad.append(f"ETTV {a1['ettv']} vs heat/Ao {a1['gross_heat'] / a1['ao']:.4f}")
    return bad


# --------------------------------------------------------------------------- orientation / grid mapping helpers

SECTORS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def orientation_from_bearing(bearing_deg: float) -> str:
    """Compass sector (45 degree wide, centred on N, NE, ...) of a facade-normal bearing, deg clockwise from north."""
    return SECTORS[int(((bearing_deg % 360) + 22.5) // 45) % 8]


def normal_bearing_from_building_xy(dx: float, dy: float, grid_north_bearing_deg: float = 0.0) -> float:
    """Bearing of an outward normal given in the grid-model frame (X along grids 1->23, Y along A->W).

    The model has no confirmed bearing (grid_model.json: bearing null); ``grid_north_bearing_deg`` is the
    bearing of the +Y axis and defaults to 0 (plans drawn north-up, north arrow page-up).
    """
    import math
    return (grid_north_bearing_deg + math.degrees(math.atan2(dx, dy))) % 360


def facade_record(facades: Sequence[Dict[str, str]], orientation: str) -> Optional[Dict[str, str]]:
    """The ``row_type == 'facade'`` row of ettv_facades.csv for an orientation, or None."""
    for r in facades:
        if r.get("row_type") == "facade" and r.get("orientation") == orientation:
            return r
    return None


# --------------------------------------------------------------------------- build

def build(pdf: Path) -> Dict[str, Any]:
    texts = pdf_page_texts(pdf)
    words = pdf_page_words(pdf)
    n_pages = len(texts)
    kinds = dict(PAGE_KINDS)
    summary = parse_summary(texts[0])
    u_types = parse_u_values(words[1])
    a1: Dict[str, Dict[str, Any]] = {}
    a2: Dict[str, Dict[str, Any]] = {}
    a3: Dict[str, Dict[str, Any]] = {}
    a4: Dict[str, Dict[str, Any]] = {}
    modules: List[Dict[str, Any]] = []
    pages: Dict[str, Dict[str, int]] = defaultdict(dict)
    for p in range(1, n_pages + 1):
        k = kinds.get(p)
        if k == "A1":
            d = parse_a1(texts[p - 1]); a1[d["orientation"]] = d; pages["A1"][d["orientation"]] = p
        elif k == "A2":
            d = parse_a2(words[p - 1]); a2[d["orientation"]] = d; pages["A2"][d["orientation"]] = p
        elif k == "A3":
            d = parse_a3(words[p - 1]); a3[d["orientation"]] = d; pages["A3"][d["orientation"]] = p
        elif k == "A4":
            d = parse_a4(words[p - 1]); a4[d["orientation"]] = d; pages["A4"][d["orientation"]] = p
        elif k == "module":
            d = parse_module(texts[p - 1]); d["page"] = p; modules.append(d)
    return {"texts": texts, "summary": summary, "u_types": u_types, "a1": a1, "a2": a2, "a3": a3,
            "a4": a4, "modules": modules, "pages": pages, "n_pages": n_pages}


def run_checks(d: Dict[str, Any]) -> List[Dict[str, str]]:
    """Cross-checks between the sheets.  Each entry: check, where, status (ok / CONFLICT), detail."""
    res: List[Dict[str, str]] = []

    def add(check, where, ok, detail=""):
        res.append({"check": check, "where": where, "status": "ok" if ok else "CONFLICT", "detail": detail})

    s = d["summary"]
    add("page 1 lists 8 facades", "p1", len(s["facades"]) == 8, f"found {len(s['facades'])}")
    tot_h = tot_a = 0.0
    for f in s["facades"]:
        o = f["orientation"]; a = d["a1"].get(o)
        if not a:
            add("A1 sheet exists", o, False, "missing"); continue
        add("summary ETTV equals A1 ETTV (3 dp)", f"{o} p1 vs p{d['pages']['A1'][o]}",
            abs(round(a["ettv"], 3) - f["ettv"]) < 0.0015, f"{f['ettv']} vs {a['ettv']}")
        for msg in check_a1(a):
            add("A1 arithmetic", o, False, msg)
        add("A1 arithmetic", o, not check_a1(a))
        tot_h += a["gross_heat"]; tot_a += a["ao"]
        # A2 areas vs A1
        a2 = {i["code"]: i["area"] for i in d["a2"][o]["items"]}
        for w in a["walls"]:
            add("A2 area equals A1 area", f"{o} {w['code']}", abs(a2.get(w["code"], -1) - w["area"]) < 0.006,
                f"A2 {a2.get(w['code'])} vs A1 {w['area']}")
        for fe in a["fens"]:
            c = f"F1(SC{fe['sc_index']})"
            add("A2 area equals A1 area", f"{o} {c}", abs(a2.get(c, -1) - fe["af"]) < 0.006,
                f"A2 {a2.get(c)} vs A1 {fe['af']}")
        # A4 SC1, SC2, area, Uf vs A1
        for fe in a["fens"]:
            c = f"F1(SC{fe['sc_index']})"
            col = d["a4"][o]["cols"].get(c)
            if not col:
                add("A4 column exists", f"{o} {c}", False, "missing"); continue
            sc1 = _num(col.get("sc1")); sc2 = _num(col.get("sc2")); uf = _num(col.get("uf"))
            ok_sc = sc1 is not None and sc2 is not None and abs(sc1 * sc2 - fe["sc"]) <= 0.0075
            add("A4 SC1 x SC2 (2 dp printed) matches A1 SC", f"{o} {c}", ok_sc,
                f"A4 {sc1} x {sc2} = {None if sc1 is None or sc2 is None else round(sc1*sc2,4)} vs A1 {fe['sc']}")
            add("A4 Uf equals A1 Uf", f"{o} {c}", uf is not None and abs(uf - fe["uf"]) < 1e-9, f"{uf} vs {fe['uf']}")
            ar = _num(col.get("area"))
            add("A4 area equals A1 area (0 if blank)", f"{o} {c}",
                abs((ar or 0.0) - fe["af"]) < 0.006, f"A4 {ar} vs A1 {fe['af']}")
        # A3 U and areas vs A1
        for w in a["walls"]:
            col = d["a3"][o]["cols"].get(w["code"], {})
            uu = _num(col.get("u")); ar = _num(col.get("area"))
            add("A3 U equals A1 Uw", f"{o} {w['code']}", uu is not None and abs(uu - w["u"]) < 1e-9, f"{uu} vs {w['u']}")
            add("A3 area equals A1 area", f"{o} {w['code']}", ar is not None and abs(ar - w["area"]) < 0.006, f"{ar} vs {w['area']}")
    if s["average"] is not None and tot_a:
        wavg = tot_h / tot_a
        add("building ETTV = total heat gain / total Ao (3 dp)", "p1", abs(round(wavg, 3) - s["average"]) < 0.0015,
            f"area-weighted {wavg:.4f} vs printed {s['average']}")
        simple = sum(f["ettv"] for f in s["facades"]) / max(1, len(s["facades"]))
        add("building ETTV is not the simple mean of 8 facades (info)", "p1", True,
            f"simple mean {simple:.3f}; area-weighted {wavg:.4f}; printed {s['average']}")
    # module SC2 vs A4
    for m in d["modules"]:
        o = WORD_TO_ORIENT.get(m["orientation_word"] or "")
        col = d["a4"].get(o, {}).get("cols", {}).get(m["f1_type"] or "")
        if col is None:
            add("module F1 type exists on A4", f"p{m['page']}", False, f"{o} {m['f1_type']}"); continue
        sc2 = _num(col.get("sc2"))
        add("module SC2 rounds to A4 SC2", f"p{m['page']} {m['module']} {o} {m['f1_type']}",
            sc2 is not None and m["sc2"] is not None and abs(round(m["sc2"], 2) - sc2) < 0.0051,
            f"module {m['sc2']} vs A4 {sc2}")
    return res


def _prov(page: int, location: str, printed: str, method: str = METHOD_TEXT, conf: str = "high") -> Dict[str, str]:
    return {"document": DOC_NAME, "page": str(page), "location": location, "text_as_printed": printed,
            "method": method, "confidence": conf}


def facade_rows(d: Dict[str, Any]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    s = d["summary"]
    no_of = {f["orientation"]: f for f in s["facades"]}
    for o in FACADE_ORDER_P1:
        a = d["a1"][o]; pa = d["pages"]["A1"][o]; dv = derive(a)
        f = no_of.get(o)
        other = (f"ettv p1: {f['printed']}; area p{d['pages']['A2'][o]} (A2); wall U p{d['pages']['A3'][o]} (A3); "
                 f"SC p{d['pages']['A4'][o]} (A4)")
        row = {k: "" for k in FACADE_FIELDS}
        row.update({
            "record_id": f"ETTV-{o}", "row_type": "facade", "block": "", "facade_no": str(f["facade_no"]) if f else "",
            "orientation": o, "storey_or_zone": "", "item_code": "", "description": f"whole-building facade facing {o} (IP and OP blocks not separated in the report)",
            "gross_wall_area_m2": _fmt(a["ao"]), "opaque_wall_area_m2": _fmt(dv["wall_area"]),
            "window_area_m2": _fmt(dv["fen_area"]), "wwr": _fmt(dv["wwr"], 4),
            "wwr_incl_spandrel": _fmt(dv["wwr_incl_spandrel"], 4),
            "wall_u_area_weighted": _fmt(dv["uw"], 4), "glazing_u_w_m2k": _fmt(a["fens"][0]["uf"]) if a["fens"] else "",
            "sc_area_weighted": _fmt(dv["sc_w"], 4), "correction_factor_cf": _fmt(a["cf"]),
            "gross_heat_gain_w": _fmt(a["gross_heat"]), "ettv_w_m2": _fmt(a["ettv"], 5),
            "evidence_other_pages": other,
            "notes": "wwr, wwr_incl_spandrel, wall_u_area_weighted and sc_area_weighted are DERIVED here from the printed areas; "
                     "the report prints none of them. Opaque wall includes W1 spandrel glazing and W3 mullion. "
                     "Block and storey are not given: one set of areas per orientation for the whole building.",
        })
        row.update(_prov(pa, "Appendix 1 sheet, Gross Area / Gross Heat Gain / ETTV lines",
                         " | ".join([a["ao_printed"], a["gross_heat_printed"], a["ettv_printed"], a["cf_printed"]]) ))
        rows.append(row)
        # wall rows
        for w in a["walls"]:
            r = {k: "" for k in FACADE_FIELDS}
            col = d["a3"][o]["cols"].get(w["code"], {})
            r.update({"record_id": f"ETTV-{o}-{w['code']}", "row_type": "wall", "orientation": o, "item_code": w["code"],
                      "description": w["desc"], "item_area_m2": _fmt(w["area"]), "wall_u_w_m2k": _fmt(w["u"]),
                      "gross_heat_gain_w": _fmt(w["heat"]),
                      "evidence_other_pages": f"p{d['pages']['A3'][o]} (A3): U {col.get('u','')}, area {col.get('area','')}; p{d['pages']['A2'][o]} (A2) area",
                      "notes": "gross_heat_gain_w is the printed 12 x Aw x Uw term"})
            r.update(_prov(pa, "Appendix 1 sheet, OPAQUE WALLS table", w["printed"]))
            rows.append(r)
        for fe in a["fens"]:
            c = f"F1(SC{fe['sc_index']})"
            col = d["a4"][o]["cols"].get(c, {})
            r = {k: "" for k in FACADE_FIELDS}
            r.update({"record_id": f"ETTV-{o}-{c}", "row_type": "window", "orientation": o, "item_code": c,
                      "description": fe["desc"], "item_area_m2": _fmt(fe["af"]), "window_area_m2": _fmt(fe["af"]),
                      "glazing_u_w_m2k": _fmt(fe["uf"]), "sc1": col.get("sc1", ""), "sc2": col.get("sc2", ""),
                      "sc_product": _fmt(fe["sc"]), "shading_method": col.get("shading_method", ""),
                      "correction_factor_cf": _fmt(a["cf"]),
                      "gross_heat_gain_w": _fmt(fe["h2"] + fe["h1"]),
                      "evidence_other_pages": f"p{d['pages']['A4'][o]} (A4): SC1 {col.get('sc1','')}, SC2 {col.get('sc2','')} (2 dp printed), method '{col.get('shading_method','')}'; p{d['pages']['A2'][o]} (A2) area",
                      "notes": "sc_product is the SC printed on Appendix 1 (full precision); sc1 and sc2 are the 2 dp values of Appendix 4; "
                               "gross_heat_gain_w = 3.4 x Af x Uf + 211 x Af x SC x CF terms added"})
            r.update(_prov(pa, "Appendix 1 sheet, FENESTRATION table", fe["printed"]))
            rows.append(r)
    # module rows
    for m in d["modules"]:
        o = WORD_TO_ORIENT.get(m["orientation_word"] or "", "")
        a1 = d["a1"].get(o, {})
        fe = next((x for x in a1.get("fens", []) if f"F1(SC{x['sc_index']})" == m["f1_type"]), None)
        r = {k: "" for k in FACADE_FIELDS}
        r.update({"record_id": f"ETTV-MOD-p{m['page']}", "row_type": "shading_module", "orientation": o,
                  "storey_or_zone": "", "item_code": m["module"] or "", "description": f"shading calculation of facade module {m['module']} for {m['f1_type']}",
                  "sc2": _fmt(m["sc2"], 9), "sc_product": _fmt(fe["sc"]) if fe else "",
                  "window_area_m2": _fmt(m["glass_areas"][0]) if len(m["glass_areas"]) == 1 else "",
                  "n_sun_vectors": str(m["n_vectors"]),
                  "notes": "window_area_m2 is the module's 'Total Glass Area' (one module, not the facade); facade type " + (m["f1_type"] or "") +
                           ("" if len(m["glass_areas"]) == 1 else f"; several glass areas printed {m['glass_areas']}")})
        r.update(_prov(m["page"], "module header line", m["printed"]))
        rows.append(r)
    # building total
    r = {k: "" for k in FACADE_FIELDS}
    r.update({"record_id": "ETTV-BUILDING", "row_type": "building", "orientation": "all",
              "description": "average ETTV of the building", "ettv_w_m2": _fmt(s["average"], 5)})
    tot_a = sum(d["a1"][o]["ao"] for o in ORIENTATIONS)
    tot_h = sum(d["a1"][o]["gross_heat"] for o in ORIENTATIONS)
    r["gross_wall_area_m2"] = _fmt(tot_a); r["gross_heat_gain_w"] = _fmt(tot_h)
    r["notes"] = f"gross_wall_area_m2 and gross_heat_gain_w are sums of the 8 facades computed here; sum heat / sum area = {tot_h / tot_a:.4f}"
    r.update(_prov(1, "summary form, 'The average ETTV of the building is'", s["average_printed"]))
    rows.append(r)
    return rows


def construction_rows(d: Dict[str, Any]) -> List[Dict[str, str]]:
    rows = []
    for t in d["u_types"]:
        comps = [(left + " " + " ".join(n[1] for n in nums)).strip() for left, nums in t["components"]]
        comps = [c for c in comps if c]
        r = {k: "" for k in CONSTR_FIELDS}
        kind = "wall" if t["code"].startswith("W") else "glazing"
        r.update({"record_id": f"CON-{t['code']}", "type_code": t["code"], "kind": kind, "description": t["desc"],
                  "orientation": t["location"], "components": " ; ".join(comps),
                  "total_r_m2k_w": _fmt(t["rt"]), "u_value_w_m2k": _fmt(t["u"]), "weight_kg_m2": _fmt(t["weight"]),
                  "sc1": _fmt(t["sc1"]), "k_density_source": t["remarks"],
                  "notes": "components as printed (name, thickness mm, K, R, density, per-layer values); weight_kg_m2 is the composite weight column; blank where the sheet prints none"})
        r.update(_prov(2, f"U-value sheet, type {t['code']}", " | ".join(comps), METHOD_WORDS))
        rows.append(r)
    # per-orientation wall and fenestration variants from A3 / A4
    for o in ORIENTATIONS:
        for code, col in d["a3"][o]["cols"].items():
            r = {k: "" for k in CONSTR_FIELDS}
            r.update({"record_id": f"CON-{o}-{code}", "type_code": code, "kind": "wall (A3)", "orientation": o,
                      "description": col.get("material", ""),
                      "components": f"internal finish: {col.get('internal_finish','')}; external finish: {col.get('external_finish','')}; insulation or air gap: {col.get('insulation','')}",
                      "u_value_w_m2k": col.get("u", ""), "weight_kg_m2": col.get("weight", "")})
            r.update(_prov(d["pages"]["A3"][o], "Appendix 3 table column " + code,
                           f"{col.get('material','')} U {col.get('u','')} wt {col.get('weight','')} area {col.get('area','')}", METHOD_WORDS))
            rows.append(r)
        for code, col in d["a4"][o]["cols"].items():
            sc1 = _num(col.get("sc1")); sc2 = _num(col.get("sc2"))
            a1f = next((f for f in d["a1"][o]["fens"] if f"F1(SC{f['sc_index']})" == code), None)
            r = {k: "" for k in CONSTR_FIELDS}
            r.update({"record_id": f"CON-{o}-{code}", "type_code": code, "kind": "glazing (A4)", "orientation": o,
                      "description": col.get("glass", ""), "u_value_w_m2k": col.get("uf", ""),
                      "sc1": col.get("sc1", ""), "sc2": col.get("sc2", ""),
                      "sc_product": _fmt(a1f["sc"]) if a1f else "", "shading_method": col.get("shading_method", ""),
                      "notes": "sc2 here is printed to 2 dp; sc_product is the full-precision SC of Appendix 1 (sc1 x module SC2)"})
            r.update(_prov(d["pages"]["A4"][o], "Appendix 4 table column " + code,
                           f"{code} Uf {col.get('uf','')} SC1 {col.get('sc1','')} SC2 {col.get('sc2','')} {col.get('shading_method','')} area {col.get('area','')}", METHOD_WORDS))
            rows.append(r)
    return rows


def note_rows(d: Dict[str, Any]) -> List[Dict[str, str]]:
    rows = []
    n = [0]

    def add(cat, item, value, page, loc, printed, method=METHOD_TEXT, conf="high", notes=""):
        n[0] += 1
        rows.append({"note_id": f"ETTVN-{n[0]:03d}", "category": cat, "item": item, "value": value,
                     "document": DOC_NAME, "page": str(page), "location": loc, "text_as_printed": printed,
                     "method": method, "confidence": conf, "notes": notes})

    s = d["summary"]
    t0 = d["texts"][0]
    add("standard", "method", "BCA ETTV calculation format for an air-conditioned building (form BPD_BP04, Appendices 1 to 4, sheet codes BCA-BP-FETTV-A1..A4)",
        1, "page title", "ETTV CALCULATION FORMAT IN RESPECT OF AN AIRCONDITIONED BUILDING")
    m = re.search(r"in accordance with the Guidelines on\s+Envelope Thermal Transfer Value For Buildings", t0)
    add("standard", "guideline cited", "Guidelines on Envelope Thermal Transfer Value For Buildings (edition and year not stated)", 1,
        "certification statement (3)", _clean(m.group(0)) if m else "", notes="the document does not print the edition or a limit value")
    add("project", "project ref", s["project_ref"], 1, "Project Ref. No", "Project Ref. No: " + s["project_ref"])
    add("project", "buildings", "1 block of 23-storey general hospital and 1 block of 12-storey specialist outpatient clinics, 3 basements", 1,
        "Description of Building", "COMPRISING OF 3 BASEMENTS, 1 BLOCK OF 23-STOREY GENERAL HOSPITAL AND 1 BLOCK OF 12-STOREY SPECIALIST OUTPATIENT CLINICS",
        notes="one set of figures for both blocks; the report does not split IP and OP")
    add("project", "certification date", s["cert_date"], 1, "QP certification (3)", "Date: " + s["cert_date"])
    add("project", "u-value sheet author and date", "Designer SDA, date 12/4/2025, Job No 5837723", 2, "U-value sheet header",
        "Designer: SDA  Date: 12/4/2025  Job No: 5837723", METHOD_WORDS)
    add("project", "workbook title (PDF metadata)", "20260127_AIGH-ETTV_Latest.xlsx, printed to PDF 2026-01-29, author Valent Tan (DPASG)", 0,
        "PDF document properties", "Title: 20260127_AIGH-ETTV_Latest.xlsx", "pdfinfo", "high", notes="page 0 = file properties, not a page")
    add("formula", "ETTV", "ETTV = Gross Heat Gain / Gross Area of External Walls (Ao), W/m2", 3, "end of Appendix 1 sheet",
        d["a1"]["N"]["ettv_printed"])
    add("formula", "heat gain terms", "gross heat gain = sum(12 x Aw x Uw) + sum(3.4 x Af x Uf) + sum(211 x Af x SC x CF)", 3,
        "column headings of Appendix 1 sheet", "12xAWxUW ... 3.4xAf xUf 211xAf xSCxCF",
        notes="the coefficients 12, 3.4 and 211 are printed in the headings; the report does not name the terms (wall conduction, glass conduction, glass solar)")
    add("formula", "gross area", "Ao = opaque wall area (W1+W2+W3, spandrel glazing and mullion counted as wall) + fenestration area", 3,
        "Appendix 1 sheet", d["a1"]["N"]["ao_printed"], "text layer + arithmetic check", "high",
        notes="verified for all 8 sheets (ao_equals_wall_plus_window check)")
    add("formula", "shading coefficient", "SC = SC1 x SC2 (glass SC times external-shading SC)", 3, "Appendix 1 column heading", "SC=SC1xSC2")
    add("formula", "building average", "printed average = total gross heat gain / total gross area (area-weighted), not the simple mean of the 8 facade ETTVs", 1,
        "summary form", s["average_printed"], "arithmetic check on printed values", "high",
        notes="see report; the report prints no formula for the average")
    for o in ORIENTATIONS:
        a = d["a1"][o]
        add("correction factor", f"CF {o}", _fmt(a["cf"]), d["pages"]["A1"][o], "Appendix 1 sheet, FENESTRATION header", a["cf_printed"],
            notes="orientation-dependent solar correction factor as printed; source table of CF is not given in the report")
    for f in s["facades"]:
        add("orientation", f"Facade {f['facade_no']}", f["orientation"], 1, "summary form list", f["printed"],
            notes="orientation labels are the 8 compass points N NE E SE S SW W NW; no bearing range or definition of 'facing' is printed")
    add("orientation", "module orientation words", "North, South, East, West, NorthEast, NorthWest, SouthEast, SouthWest", 35,
        "module header lines of pages 35-67", "Orientation North ... SouthWest", notes="same eight orientations as the sheets; no angle")
    add("shading", "external shading method", "Fins & Mullions for every fenestration type on every sheet checked", 27,
        "Appendix 4 'Method of external shading'", "Fins & Mullions", METHOD_WORDS,
        notes="; ".join(sorted({c.get("shading_method", "") for o in ORIENTATIONS for c in d["a4"][o]["cols"].values()})))
    add("shading", "module shading sheets", "per facade module (FT03-A, FT04-A, FT06, FT08, FT14, GW100AA etc.) and F1 type: SC2 plus a table of sun vectors with exposed area, glass area, shaded heat gain and total heat gain",
        35, "pages 35-67", "Total Exposed Area / Total Glass Area / Shaded Heat Gain / Total Heat Gain",
        notes="the sun-vector table is not extracted beyond row count; SC2 is the value carried to Appendix 4")
    add("scope", "storey or zone split", "none: the sheets print one area set per orientation", 11, "Appendix 2 note",
        "Where the envelope design varies from floor to floor a separate area calculation should be presented for each floor.",
        notes="the note asks for per-floor sheets where the design varies; none are in the file (8 Appendix sheets each, one per orientation)")
    add("scope", "images in the file", "no data values read from images; page 1 stamp and logos, page 2 logo strip, Appendix 2 glass thumbnails only", 1,
        "pdfimages listing", "", "pdfimages -list", "high", notes="no second read list needed: nothing was image-read")
    return rows


def write_csv(path: Path, fields: Sequence[str], rows: Sequence[Dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(fields), extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def run(pdf: Path, out_root: Path) -> Dict[str, Any]:
    d = build(pdf)
    checks = run_checks(d)
    write_csv(out_root / OUT_FACADES, FACADE_FIELDS, facade_rows(d))
    write_csv(out_root / OUT_CONSTR, CONSTR_FIELDS, construction_rows(d))
    write_csv(out_root / OUT_NOTES, NOTE_FIELDS, note_rows(d))
    conflicts = [c for c in checks if c["status"] != "ok"]
    return {"data": d, "checks": checks, "conflicts": conflicts}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="ettv_parser")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--pdf", default=None)
    r.add_argument("--out-root", default=None)
    args = ap.parse_args(argv)
    root = Path(args.out_root) if args.out_root else Path(__file__).resolve().parents[1]
    pdf = Path(args.pdf) if args.pdf else root / DOC
    res = run(pdf, root)
    print(f"wrote {OUT_FACADES}, {OUT_CONSTR}, {OUT_NOTES}; checks {len(res['checks'])}, conflicts {len(res['conflicts'])}")
    for c in res["conflicts"]:
        print("CONFLICT", c["check"], c["where"], c["detail"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
