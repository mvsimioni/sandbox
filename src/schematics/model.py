"""Schematic model: one record per air-side system, read from the Phase 1 registers.

Run:  python -m src.schematics.model run

Inputs  (read only): out/registers/schematic_{rooms,equipment,connections,ahu_totals,
        tender_vs_working}.csv, acmv_notes.csv, appd_{spaces,equipment,
        schedule_reconciliation}.csv, the App D annotation sidecar, and the
        PDF text layer of the working-drawing sheets (notes zone only).
Outputs: out/registers/schematic_model.json, out/registers/schematic_systems.csv,
        out/qa/schematic_tender_vs_working_summary.csv

Every value is a dict {value, as_written, box, sheet, page, source} where box is the
page-view box [x0, y0, x1, y1] in points (origin top-left) that the value was read
from.  A value that the registers hold without its own box carries the box of the
cell, table or outline it sits in, and ``box_level`` says which.  No value is
invented: where the sheet does not write something the field is null and
``note`` says so.  No source drawing is written.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[2]
REG = "out/registers"
APPD_PDF = "data/source/Basis/App D/Appendix D - ACMV For Common Areas_Combined.pdf"
APPD_ANN = ("out/sidecar/Basis/App D/Appendix D - ACMV For Common Areas_Combined.pdf/"
            "annotations.jsonl")
FAN_TYPES = ("RAF", "TEAF", "EAF", "OAF", "TEF", "HPF")
NOTE_REQ_RE = re.compile(r"\b(SHALL|MUST|REQUIRED|NOT LESS THAN|AT LEAST|MINIMUM|MAXIMUM)\b")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def read_csv(path: Path) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def num(text: Any) -> Optional[float]:
    """First number in a string such as 'OA 2375 L/s' (None when there is none)."""
    if text is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(text).replace(",", ""))
    return float(m.group(0)) if m else None


def box(text: Any) -> Optional[List[float]]:
    if text in (None, ""):
        return None
    try:
        v = json.loads(text) if isinstance(text, str) else list(text)
    except ValueError:
        return None
    return [round(float(x), 1) for x in v] if v and len(v) == 4 else None


def clean(text: Any) -> Optional[str]:
    t = (text or "").strip() if isinstance(text, str) else text
    return t if t not in ("", None) else None


def ahu_key(tag: str) -> str:
    """Tag without its type prefix and without the trailing A/B (duty/standby) letter."""
    m = re.match(r"^[A-Za-z/]+-(.*)$", tag.strip())
    k = m.group(1) if m else tag
    return re.sub(r"(?<=\d)[A-D](?:/[A-D])*$", "", k)


def tail2(key: str) -> str:
    return "-".join(key.split("-")[-2:])


def val(value: Any, as_written: Any, bx: Optional[List[float]], sheet: str, page: Any,
        level: str = "value", note: Optional[str] = None) -> Dict[str, Any]:
    d: Dict[str, Any] = {"value": value, "as_written": as_written, "box": bx,
                         "box_level": level, "sheet": sheet, "page": int(page or 1)}
    if bx is None:
        d["note"] = note or "no box in the register"
    elif note:
        d["note"] = note
    return d


def split_tags(text: Optional[str]) -> List[str]:
    return [t.strip() for t in (text or "").split(";") if t.strip()]


# --------------------------------------------------------------------------- #
# Notes zone of a working sheet (text layer, read only)
# --------------------------------------------------------------------------- #
def read_sheet_notes(pdf: Path) -> List[Dict[str, Any]]:
    """Numbered notes in the notes column (right edge, top quarter) with their box."""
    try:
        import pymupdf as fitz  # type: ignore
    except ImportError:  # pragma: no cover
        import fitz  # type: ignore
    try:
        page = fitz.open(str(pdf))[0]
    except Exception:  # pragma: no cover
        return []
    W, H = page.rect.width, page.rect.height
    lines = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            t = "".join(s["text"] for s in l["spans"]).strip()
            x0, y0, x1, y1 = l["bbox"]
            if t and x0 > W * 0.9 and y0 < H * 0.25:
                lines.append((y0, x0, x1, y1, t))
    lines.sort()
    notes: List[Dict[str, Any]] = []
    for y0, x0, x1, y1, t in lines:
        m = re.match(r"^(\d+)\.\s*(.*)$", t)
        if m and (not notes or x0 <= notes[-1]["_x"] + 2):
            notes.append({"number": int(m.group(1)), "_x": x0, "parts": [m.group(2)],
                          "box": [x0, y0, x1, y1]})
        elif notes:
            n = notes[-1]
            n["parts"].append(t)
            b = n["box"]
            n["box"] = [min(b[0], x0), b[1], max(b[2], x1), max(b[3], y1)]
        # lines before the first number (heading) are ignored
    out = []
    for n in notes:
        text = re.sub(r"\s+", " ", " ".join(p for p in n["parts"] if p)).strip()
        out.append({"number": n["number"], "text": text,
                    "box": [round(v, 1) for v in n["box"]]})
    return out


def norm_note(t: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", t.upper()).strip()


# --------------------------------------------------------------------------- #
# Working-drawing systems
# --------------------------------------------------------------------------- #
# Airflow arrow labels that the register can read as a room name (item: IP-BH-07050 boxes).
ARROW_LABELS = {"FA", "TA", "RA", "SA", "EA", "OA", "EX", "KEA", "KMA", "IEA"}


def _title_from_text_layer(pdf: Path, table: List[float]) -> Optional[Dict[str, Any]]:
    """Bold title above a room table: the room name, as drawn (size 12 to 14 bold spans)."""
    import pymupdf
    doc = pymupdf.open(pdf)
    try:
        pg = doc[0]
        x0, y0, x1, _ = table
        clip = pymupdf.Rect(x0 - 10, y0 - 75, x1 + 10, y0 + 3)
        spans = []
        for b in pg.get_text("dict", clip=clip)["blocks"]:
            for ln in b.get("lines", []):
                for sp in ln["spans"]:
                    t = sp["text"].strip()
                    cx = (sp["bbox"][0] + sp["bbox"][2]) / 2
                    if t and "Bold" in sp["font"] and sp["size"] >= 11.5 and x0 - 5 <= cx <= x1 + 5:
                        spans.append((sp["size"], sp["bbox"], t))
        if not spans:
            return None
        big = sorted([s for s in spans if s[0] >= 13.5], key=lambda s: (round(s[1][1] / 6), s[1][0]))
        small = sorted([s for s in spans if s[0] < 13.5], key=lambda s: (round(s[1][1] / 6), s[1][0]))
        text = " ".join(s[2] for s in big + small)
        text = re.sub(r"\s*\(\s*\d+\s*No\.?\s*\)", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        bb = [min(s[1][0] for s in spans), min(s[1][1] for s in spans),
              max(s[1][2] for s in spans), max(s[1][3] for s in spans)]
        return {"name": text, "box": [round(v, 1) for v in bb]}
    finally:
        doc.close()


def repair_room_names(reg: "Reg") -> None:
    """Replace a room name that is an airflow arrow label by the bold title on the sheet.

    The register took the label of an arrow (FA, TA) for the room name on the sheets
    IP-BH-07050 to 07052.  The title is read from the text layer above the room table.
    The source register is not changed; the repair is listed in ``reg.name_repairs``.
    """
    cache: Dict[Any, Optional[Dict[str, Any]]] = {}
    for r in reg.rooms:
        if r["issue_set"] != "working" or r["room_name_as_written"].strip().upper() not in ARROW_LABELS:
            continue
        key = (r["sheet"], r["box_page_view_table"])
        if key not in cache:
            tb = box(r["box_page_view_table"])
            pdfs = sorted((reg.root / "data/source").glob(f"**/{r['sheet']}*.pdf"))
            pdfs = [p for p in pdfs if "Working Drawings" in str(p)]
            cache[key] = _title_from_text_layer(pdfs[0], tb) if (pdfs and tb) else None
        t = cache[key]
        if t and t["name"]:
            reg.name_repairs.append({"sheet": r["sheet"], "was": r["room_name_as_written"], "now": t["name"],
                                     "table": r["box_page_view_table"]})
            r["room_name_as_written"] = t["name"]
            r["box_page_view_name"] = json.dumps(t["box"])


class Reg:
    """All registers, loaded once."""

    def __init__(self, root: Path = ROOT):
        self.root = root
        r = root / REG
        self.rooms = read_csv(r / "schematic_rooms.csv")
        self.equip = read_csv(r / "schematic_equipment.csv")
        self.conn = read_csv(r / "schematic_connections.csv")
        self.totals = read_csv(r / "schematic_ahu_totals.csv")
        self.tvw = read_csv(r / "schematic_tender_vs_working.csv")
        self.notes = read_csv(r / "acmv_notes.csv")
        etvw = root / "out/qa/schematic_equipment_tender_vs_working.csv"
        self.etvw = read_csv(etvw) if etvw.exists() else []
        self.name_repairs: List[Dict[str, str]] = []
        repair_room_names(self)


def _ystart(items: Sequence[Optional[List[float]]]) -> Optional[float]:
    ys = [b[1] for b in items if b]
    return min(ys) if ys else None


def build_working(reg: Reg, read_notes: bool = True) -> Dict[str, Any]:
    rooms = [r for r in reg.rooms if r["issue_set"] == "working"]
    equip = [r for r in reg.equip if r["issue_set"] == "working"]
    conn = [r for r in reg.conn if r["issue_set"] == "working"]
    tots = [r for r in reg.totals if r["issue_set"] == "working" and r["scope"] == "sheet"]
    sheets = sorted({r["sheet"] for r in rooms} | {r["sheet"] for r in equip})
    by_sheet = lambda rows: _group(rows, "sheet")  # noqa: E731
    rooms_by, equip_by, conn_by, tots_by = map(by_sheet, (rooms, equip, conn, tots))

    # sheet notes: from the register where it has them (05010, 00001), else the text layer
    reg_notes = defaultdict(list)
    for n in reg.notes:
        if n["issue"] == "working":
            reg_notes[n["sheet"]].append(n)
    general = [{"number": n["number"], "text": n["text"],
                "sets_requirement": n["sets_pipeline_requirement"] == "yes",
                "requirement_type": clean(n["requirement_type"]),
                "requirement_value": clean(n["requirement_value"]),
                "box": box("[" + n["box_pt_x0_y0_x1_y1"].strip("()") + "]"),
                "sheet": "IP-BH-00001", "page": 1} for n in reg_notes.get("IP-BH-00001", [])]

    sheet_meta: Dict[str, Any] = {}
    systems: List[Dict[str, Any]] = []
    for sh in sheets:
        srooms, seq, scn, stot = rooms_by[sh], equip_by[sh], conn_by[sh], tots_by[sh]
        meta_src = (srooms or seq)[0]
        page = int(meta_src["page"] or 1)
        notes = _sheet_notes(reg, root=reg.root, sheet=sh, rows=meta_src, reg_notes=reg_notes,
                             read=read_notes)
        sheet_meta[sh] = {"storey": meta_src["storey"], "page": page,
                          "drive_rel_path": meta_src["drive_rel_path"],
                          "file_sha256": meta_src["file_sha256"], "issue": meta_src["issue"],
                          "notes": notes}
        systems.extend(_build_sheet_systems(sh, page, meta_src, srooms, seq, scn, stot, notes))
    return {"systems": systems, "sheets": sheet_meta, "general_notes": general}


def _group(rows: Iterable[Dict[str, str]], key: str) -> Dict[str, List[Dict[str, str]]]:
    d: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for r in rows:
        d[r[key]].append(r)
    return d


def _sheet_notes(reg: Reg, root: Path, sheet: str, rows: Dict[str, str],
                 reg_notes: Dict[str, list], read: bool) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if sheet in reg_notes:
        for n in reg_notes[sheet]:
            text = re.sub(r"\s+", " ", n["text"]).strip()
            out.append({"number": n["number"], "text": text,
                        "sets_requirement": n["sets_pipeline_requirement"] == "yes",
                        "requirement_type": clean(n["requirement_type"]),
                        "requirement_value": clean(n["requirement_value"]),
                        "box": box("[" + n["box_pt_x0_y0_x1_y1"].strip("()") + "]"),
                        "sheet": sheet, "page": 1, "method": "acmv_notes register"})
        return out
    if not read:
        return out
    pdf = root / "data/source" / rows["drive_rel_path"]
    for n in read_sheet_notes(pdf):
        out.append({"number": n["number"], "text": n["text"],
                    "sets_requirement": bool(NOTE_REQ_RE.search(n["text"])),
                    "requirement_type": None, "requirement_value": None,
                    "box": n["box"], "sheet": sheet, "page": 1,
                    "method": "text layer of the notes column; requirement flag by keyword "
                              "(SHALL, MUST, REQUIRED, MINIMUM...), not by a ruling"})
    return out


def _room_key(r: Dict[str, str]):
    return (r["sheet"], r["box_page_view_table"], r["room_name_as_written"], r["typical_count"])


TOKEN_RE = re.compile(r"\b(SA|RA|EA|OA|IEA|KMA)\s+(-|[\d.]+)\s*L/s")


def _room_records(sheet: str, page: int, rows: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    grouped: "OrderedDict[Any, List[Dict[str, str]]]" = OrderedDict()
    for r in rows:
        grouped.setdefault(_room_key(r), []).append(r)
    out = []
    for k, rr in grouped.items():
        f = rr[0]
        tb = box(f["box_page_view_table"])
        rec = {
            "room_id": "%s#%s" % (sheet, len(out) + 1),
            "name": val(f["room_name_as_written"], f["room_name_as_written"],
                        box(f["box_page_view_name"]) or box(f["box_page_view_outline"]) or tb,
                        sheet, page,
                        "name" if box(f["box_page_view_name"]) else "outline" if box(f["box_page_view_outline"]) else "table"),
            "typical_count": int(f["typical_count"] or 1),
            "ahu_tags_as_written": split_tags(f["ahu_tag"]),
            "serving_fan_tags_as_written": split_tags(f["serving_fan_tags"]),
            "isolation_exhaust_target": clean(f["isolation_exhaust_target"]),
            "services_drawn": clean(f["services_drawn"]),
            "pressure": val(clean(f["pressure"]), clean(f["pressure"]), tb, sheet, page, "table"),
            "filter": val(clean(f["filter"]), clean(f["filter"]), tb, sheet, page, "table"),
            "temperature": val(clean(f["temperature"]), clean(f["temperature"]), tb, sheet, page, "table"),
            "humidity": val(clean(f["humidity"]), clean(f["humidity"]), tb, sheet, page, "table"),
            "air_class": val(clean(f["air_class"]), clean(f["air_class"]), tb, sheet, page, "table"),
            "box_table": tb, "box_outline": box(f["box_page_view_outline"]),
            "confidence": f["confidence"], "modes": [],
        }
        for r in rr:
            cell = box(r["box_page_view_airflow_cell"])
            toks = {t: (v, "%s %s L/s" % (t, v)) for t, v in TOKEN_RE.findall(r["airflow_as_printed"] or "")}
            def v_(t, col):
                col_v = clean(r[col]) if col else None
                tok = toks.get(t)
                value = float(col_v) if col_v is not None else (None if not tok or tok[0] == "-" else float(tok[0]))
                return val(value, tok[1] if tok else (r["airflow_as_printed"] if value is not None else None), cell if (value is not None or tok) else None,
                           sheet, page, "airflow cell",
                           None if tok else "not drawn for this room and mode")
            rec["modes"].append({
                "mode": r["mode"], "supply_L_s": v_("SA", "supply_L_s"),
                "return_L_s": v_("RA", "return_L_s"), "exhaust_L_s": v_("EA", "exhaust_L_s"),
                "outdoor_air_L_s": v_("OA", None),
                "other_airflow_as_written": clean(r["airflow_unparsed"]),
                "airflow_as_printed": r["airflow_as_printed"], "box_airflow_cell": cell})
        out.append(rec)
    return out


def _build_sheet_systems(sh, page, meta, srooms, seq, scn, stot, notes):
    rooms = _room_records(sh, page, srooms)
    ahu_rows = [e for e in seq if e["type"] == "AHU"]
    groups: "OrderedDict[str, List[Dict[str, str]]]" = OrderedDict()
    for e in ahu_rows:
        groups.setdefault(ahu_key(e["tag"]), []).append(e)
    tag2key = {e["tag"]: k for k, v in groups.items() for e in v}
    # y-start of each system (top of its AHU tag row), for placing elements that carry no tag
    ystart = {k: _ystart([box(e["box_page_view_tag"]) for e in v]) for k, v in groups.items()}
    keys = sorted(groups, key=lambda k: (ystart[k] if ystart[k] is not None else 0))
    stacked = (len(keys) > 1 and all(
        ystart[b] - ystart[a] > 300 for a, b in zip(keys, keys[1:]) if ystart[a] is not None and ystart[b] is not None))

    def place_by_y(b):
        if len(keys) == 1:
            return keys[0], "only system on the sheet"
        if stacked and b:
            cand = [k for k in keys if ystart[k] is not None and ystart[k] <= b[1] + 120]
            return (cand[-1] if cand else keys[0]), "vertical position on the sheet (stacked systems)"
        return None, None

    # room -> systems
    room_sys: Dict[str, List[str]] = defaultdict(list)
    unassigned: List[Dict[str, Any]] = []
    for rm in rooms:
        ks = []
        for t in rm["ahu_tags_as_written"]:
            k = tag2key.get(t)
            if k and k not in ks:
                ks.append(k)
        basis = "ahu tag on the room box"
        if not ks:
            def by_key(tagtxt):
                kk = ahu_key(tagtxt)
                if kk in groups:
                    return kk
                c = [g for g in groups if tail2(g) == tail2(kk)]
                return c[0] if len(c) == 1 else None
            cand = [by_key(t) for t in rm["ahu_tags_as_written"]]
            if any(cand):
                ks, basis = sorted({c for c in cand if c}), "AHU tag on the room box matched by tag number"
            else:
                cand = [by_key(t) for t in rm["serving_fan_tags_as_written"] if re.match(r"^[A-Z]+-", t)]
                if any(cand):
                    ks, basis = sorted({c for c in cand if c}), "serving fan tag on the room box matched by tag number"
        if not ks and len(keys) == 1:
            ks, basis = [keys[0]], "no AHU tag on the room box; only system on the sheet"
        rm["assignment_basis"] = basis if ks else "no AHU tag on the room box and several systems on the sheet"
        rm["unknown_ahu_tags"] = [t for t in rm["ahu_tags_as_written"] if t not in tag2key]
        rm["system_count"] = len(ks)
        if ks:
            for k in ks:
                room_sys[k].append(rm["room_id"])
        else:
            unassigned.append(rm)

    rm_by_id = {r["room_id"]: r for r in rooms}
    systems = []
    for k in keys:
        ar = groups[k]
        tags = [e["tag"] for e in ar]
        sid = "%s:%s" % (sh, ahu_key(tags[0]))
        srv = {t for rid in room_sys[k] for t in rm_by_id[rid]["serving_fan_tags_as_written"]}
        iso_targets = {rm_by_id[rid]["isolation_exhaust_target"] for rid in room_sys[k]} - {None}
        # ahus
        ahus = []
        for e in ar:
            tb = box(e["box_page_view_tag"])
            ahus.append({"tag": val(e["tag"], e["tag_as_printed"] or e["tag"], tb, sh, page, "tag"),
                         "duty_supply_L_s": val(num(e["duty_as_written"]), e["duty_as_written"],
                                                box(e["box_page_view_duty"]) or tb, sh, page,
                                                "duty" if box(e["box_page_view_duty"]) else "tag"),
                         "accessories_as_drawn": json.loads(e["accessories_nearby"] or "{}")})
        # other equipment
        fans, bibo, louvres, offsheet, iea = [], [], [], [], []
        for e in seq:
            if e["type"] == "AHU":
                continue
            tg = e["tag"]
            basis = None
            if e["type"] in FAN_TYPES or e["type"] == "BIBO":
                ek = ahu_key(tg)
                if ek == k:
                    basis = "tag key equals the AHU key"
                elif tail2(ek) == tail2(k) and tail2(ek) in {tail2(x) for x in [k]} and not any(
                        ek == x for x in groups):
                    basis = "tag number equals the AHU number (plant code differs)"
                elif tg in srv:
                    basis = "tag named on the room boxes of this system"
                elif tg in iso_targets:
                    basis = "isolation target named on the room boxes"
            elif e["type"].startswith("PAHU"):
                if any(t in (e["duty_as_written"] or "") for t in tags):
                    basis = "AHU tag named in the OA note"
            else:  # louvres, isolation exhaust line labels
                kk, why = place_by_y(box(e["box_page_view_tag"]))
                if kk == k:
                    basis = why
            if not basis:
                continue
            rec = {"tag": val(tg, e["tag_as_printed"] or tg, box(e["box_page_view_tag"]), sh, page, "tag"),
                   "type": e["type"],
                   "duty": val(num(e["duty_as_written"]), e["duty_as_written"],
                               box(e["box_page_view_duty"]) or box(e["box_page_view_tag"]), sh, page,
                               "duty" if box(e["box_page_view_duty"]) else "tag"),
                   "plant_level": clean(e["plant_level"]), "served_level": clean(e["served_level"]),
                   "pair": clean(e["pair"]), "duty_standby": clean(e["duty_standby"]),
                   "accessories_as_drawn": json.loads(e["accessories_nearby"] or "{}"),
                   "link_basis": basis}
            t = e["type"]
            (fans if t in FAN_TYPES else bibo if t == "BIBO" else louvres if t.startswith("louvre")
             else offsheet if t.startswith("PAHU") else iea).append(rec)
        # duplicate tag with differing duty on the sheet
        dup = Counter(f["tag"]["value"] for f in fans)
        for f in fans:
            f["duplicate_tag_on_sheet"] = dup[f["tag"]["value"]] > 1
        # connections
        oa, spill, ieah = [], [], []
        for c in scn:
            b = box(c["box_page_view"])
            row = {"from": c["from_tag"], "to": c["to_tag_or_room"], "service": c["service"],
                   "as_written": c["size_flow_as_written"], "value_L_s": num(c["size_flow_as_written"]),
                   "box": b, "box_level": "label", "sheet": sh, "page": page}
            if c["service"] == "OA" and c["to_tag_or_room"] in tags:
                oa.append(row)
            elif c["service"] == "RA (spill)":
                kk, _ = place_by_y(b)
                if kk == k:
                    spill.append(row)
            elif c["service"] == "EA (isolation)" and c["from_tag"].upper().startswith("IEA"):
                kk, _ = place_by_y(b)
                if kk == k:
                    ieah.append(row)
        # totals
        tt = [t for t in stot if t["ahu_tag"] in tags]
        totals = None
        if tt:
            t0 = tt[0]
            totals = {"rooms_in_group": int(t0["rooms_in_group"] or 0),
                      "rooms_SA_sum_with_typicals_L_s": num(t0["rooms_SA_sum_L_s_with_typicals"]),
                      "rooms_SA_x_1_15_L_s": num(t0["rooms_SA_x_1_15"]),
                      "ratio_ahu_to_rooms": num(t0["ratio_ahu_to_rooms"]),
                      "diff_L_s": num(t0["diff_L_s"]),
                      "rooms_OA_sum_L_s": num(t0["rooms_OA_sum_L_s"]),
                      "oa_label_L_s": num(t0["oa_label_L_s"]), "raf_L_s": num(t0["raf_L_s"]),
                      "spill_L_s": num(t0["spill_L_s"]), "check_result": t0["check_result"],
                      "check_note": t0["check_note"], "source": "schematic_ahu_totals.csv"}
        pah = [r for r in oa if r["from"].upper().startswith("PAHU")]
        oa_src = ("PAHU: " + "; ".join(sorted({r["from"] for r in pah}))) if pah else (
            "other: " + "; ".join(sorted({r["from"] for r in oa})) if oa else None)
        sheet_notes_req = [n["number"] for n in notes if n.get("sets_requirement")]
        systems.append({
            "system_id": sid, "kind": "working_ahu_system", "source_set": "working", "sheet": sh,
            "page": page, "storey": meta["storey"], "drive_rel_path": meta["drive_rel_path"],
            "file_sha256": meta["file_sha256"], "title": clean(srooms[0]["schematic_title"]) if srooms else None,
            "ahus": ahus,
            "duty_standby": ahus and clean(ar[0]["duty_standby"]),
            "plant_level": clean(ar[0]["plant_level"]), "served_level": clean(ar[0]["served_level"]),
            "outdoor_air": {"source": oa_src or "no OA line found on the sheet",
                            "lines": oa, "pahu_rows": offsheet},
            "return_air": {"fans": [f for f in fans if f["type"] == "RAF"]},
            "spill_relief": {"lines": spill,
                             "louvres": [l for l in louvres if "spill" in l["type"]],
                             "note": None if spill else "no spill label linked to this system"},
            "exhaust_fans": [f for f in fans if f["type"] != "RAF"],
            "isolation_exhaust": {"targets": sorted(iso_targets), "header_labels": ieah,
                                  "line_rows": iea},
            "bibo_units": bibo,
            "toilet_exhaust_louvres": [l for l in louvres if "toilet" in l["type"]],
            "filters_on_room_boxes": sorted({r["filter"]["value"] for rid in room_sys[k]
                                            for r in [rm_by_id[rid]] if r["filter"]["value"]}),
            "totals_register": totals,
            "rooms": [rm_by_id[i] for i in room_sys[k]],
            "requirement_note_numbers": sheet_notes_req,
        })
    if unassigned or (not keys and rooms):
        systems.append({
            "system_id": "%s:UNASSIGNED" % sh, "kind": "working_unassigned_rooms",
            "source_set": "working", "sheet": sh, "page": page, "storey": meta["storey"],
            "drive_rel_path": meta["drive_rel_path"], "file_sha256": meta["file_sha256"],
            "title": None, "ahus": [], "rooms": unassigned or rooms,
            "note": "room boxes with no AHU tag on the box and several systems (or none) on the sheet"})
    return systems


# --------------------------------------------------------------------------- #
# App D
# --------------------------------------------------------------------------- #
def load_appd_rects(root: Path) -> Dict[Any, List[float]]:
    p = root / APPD_ANN
    out: Dict[Any, List[float]] = {}
    if not p.exists():
        return out
    for line in open(p, encoding="utf-8"):
        d = json.loads(line)
        if d.get("rect_view"):
            out[(int(d["page"]), int(d["index"]))] = [round(x, 1) for x in d["rect_view"]]
    return out


APPD_TAG_RE = re.compile(r"^(AHU|FCU|OAF/EAF|OAF|EAF|TEAF)-(.+)$")


def build_appd(root: Path = ROOT) -> Dict[str, Any]:
    spaces = read_csv(root / REG / "appd_spaces.csv")
    equip = read_csv(root / REG / "appd_equipment.csv")
    recon = {r["appd_tag_normalised"]: r for r in read_csv(root / REG / "appd_schedule_reconciliation.csv")}
    rects = load_appd_rects(root)
    sp_by = {(int(s["page"]), int(s["annotation_index"])): s for s in spaces}

    def space_rec(s):
        k = (int(s["page"]), int(s["annotation_index"]))
        return {"space_ref": "AppD p%s #%s" % k, "page": k[0], "annotation_index": k[1],
                "storey": s["storey"], "drawing_number": s["drawing_number"],
                "name_as_written": s["space_names_text_layer"],
                "area_label_as_written": s["area_label_text"],
                "area_m2": num(s["printed_area_m2"]) if s["printed_area_m2"] else num(s["area_m2_at_1_250"]),
                "mode_label_as_written": clean(s["mode_label"]),
                "box": rects.get(k), "box_level": "polygon /Rect (page view)",
                "source": "%s, page %s, annotation %s" % (APPD_PDF, k[0], k[1])}

    tags_near: Dict[str, List[Any]] = defaultdict(list)
    for sp_row in spaces:
        for part in (sp_row["equipment_tags_within_60pt"] or "").split(";"):
            mm = re.match(r"\s*(\S+)\s*\[ann (\d+),", part)
            if mm:
                tags_near[mm.group(1)].append((int(sp_row["page"]), int(sp_row["annotation_index"])))
    groups: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for e in equip:
        m = APPD_TAG_RE.match(e["tag_normalised"])
        key = re.sub(r"(?<=\d)[A-D](?:/[A-D])*$", "", m.group(2)) if m else e["tag_normalised"]
        groups[key].append(e)
    systems = []
    used_spaces = set()
    for key in sorted(groups):
        rows = groups[key]
        types = {(APPD_TAG_RE.match(r["tag_normalised"]).group(1) if APPD_TAG_RE.match(r["tag_normalised"]) else r["type"]) for r in rows}
        kind = ("appd_ahu_system" if "AHU" in types else "appd_fcu_group" if "FCU" in types
                else "appd_mv_fan_system")
        equipment, sp_keys = [], OrderedDict()
        for e in rows:
            ak = (int(e["page"]), int(e["annotation_index"]))
            tag = e["tag_as_written"]
            rc = recon.get(e["tag_normalised"])
            equipment.append({
                "tag_as_written": tag, "tag_normalised": e["tag_normalised"], "type": e["type"],
                "box": rects.get(ak), "box_level": "annotation /Rect (page view)",
                "page": ak[0], "annotation_index": ak[1], "storey": e["storey"],
                "note_beside_as_written": clean(e["note_beside"]),
                "variant": clean(e["variant_reason"]),
                "space_ref": ("AppD p%s #%s" % (ak[0], e["space_annotation_index"])
                              if e["space_annotation_index"] not in ("", None) else None),
                "space_relation": e["space_relation"],
                "schedule": ({"schedule_tag_as_written": rc["schedule_tag_as_written"],
                              "supply_airflow_L_s": num(rc["airflow"]) if rc["airflow"] else None,
                              "airflow_header_as_read": rc["airflow_header_as_read"],
                              "outdoor_air_as_written": clean(rc["outdoor_air"]),
                              "static_pressure_Pa": num(rc["static_pressure_Pa"]) if rc["static_pressure_Pa"] else None,
                              "cooling_kW": num(rc["cooling_capacity_kW"]) if rc["cooling_capacity_kW"] else None,
                              "schedule_pages": rc["schedule_pages"],
                              "schedule_tag_bbox_pt": rc["schedule_tag_bbox_pt"],
                              "match_method": rc["match_method"]} if rc else None),
                "found_in_schedule": bool(rc and rc["found_in_schedule"] == "y"),
            })
            if e["space_annotation_index"] not in ("", None):
                sk = (int(e["page"]), int(float(e["space_annotation_index"])))
                if sk in sp_by:
                    sp_keys[sk] = None
                    used_spaces.add(sk)
        near = OrderedDict()
        for e in rows:
            sk0 = (int(e["page"]), int(float(e["space_annotation_index"]))) if e["space_annotation_index"] not in ("", None) else None
            for sk in tags_near.get(e["tag_as_written"], []):
                if sk != sk0 and sk not in sp_keys:
                    near[sk] = None
        sp = [dict(space_rec(sp_by[k]), relation="equipment inside or nearest") for k in sp_keys]
        sp += [dict(space_rec(sp_by[k]), relation="equipment tag within 60 pt of the space") for k in near]
        used_spaces.update(near)
        systems.append({
            "system_id": "APPD:%s" % key, "kind": kind, "source_set": "appd",
            "sheet": "App D", "drawing_numbers": sorted({e["drawing_number"] for e in rows}),
            "storey": sorted({int(e["storey"]) for e in rows}),
            "page": sorted({int(e["page"]) for e in rows}),
            "equipment": equipment, "spaces": sp,
            "outdoor_air_exhaust_path": _appd_path(types),
            "rates_beside_fans_as_written": sorted({e["note_beside_as_written"] for e in equipment if e["note_beside_as_written"]}),
        })
    unserved = [space_rec(s) for k, s in sp_by.items() if k not in used_spaces]
    for u in unserved:
        systems.append({"system_id": "APPD:SPACE:%s" % u["space_ref"].replace(" ", ""),
                        "kind": "appd_space_without_equipment", "source_set": "appd", "sheet": "App D",
                        "drawing_numbers": [u["drawing_number"]], "storey": [int(u["storey"])],
                        "page": [u["page"]], "equipment": [], "spaces": [u],
                        "outdoor_air_exhaust_path": "no equipment tag in or beside the space on the layout"})
    return {"systems": systems, "unserved_space_count": len(unserved),
            "space_count": len(sp_by)}


def _appd_path(types) -> str:
    bits = []
    if "AHU" in types:
        bits.append("AHU supply and return; outdoor-air and exhaust path not drawn on the layout (see schedule)")
    if "FCU" in types:
        bits.append("FCU recirculation")
    if "OAF" in types or "OAF/EAF" in types:
        bits.append("outdoor air by fan")
    if "EAF" in types or "TEAF" in types or "OAF/EAF" in types:
        bits.append("exhaust by fan")
    return "; ".join(bits) or "not shown"


# --------------------------------------------------------------------------- #
# Flat CSV
# --------------------------------------------------------------------------- #
CSV_COLS = ["system_id", "kind", "source_set", "sheet", "page", "storey", "title", "ahu_tags",
            "duty_standby", "supply_per_unit_L_s", "supply_box", "plant_level", "served_level",
            "oa_source", "oa_L_s_as_written", "oa_box", "return_fans", "return_duty_as_written",
            "return_box", "spill_as_written", "spill_box", "exhaust_fans", "isolation_exhaust",
            "bibo_units", "filters", "accessories_as_drawn", "room_boxes", "room_rows_with_typicals",
            "rooms_SA_sum_L_s", "check_result", "requirement_notes", "spaces", "equipment_tags",
            "rates_as_written", "oa_exhaust_path", "file_sha256"]


def _tb(v: Dict[str, Any]) -> str:
    return "%s %s%s" % (v["tag"]["value"], v["duty"]["as_written"] or "",
                        " " + json.dumps(v["duty"]["box"]) if v["duty"]["box"] else "")


def flat_row(s: Dict[str, Any]) -> Dict[str, Any]:
    r = {c: "" for c in CSV_COLS}
    r.update(system_id=s["system_id"], kind=s["kind"], source_set=s["source_set"], sheet=s["sheet"],
             page=";".join(map(str, s["page"])) if isinstance(s["page"], list) else s["page"],
             storey=";".join(map(str, s["storey"])) if isinstance(s["storey"], list) else s["storey"],
             file_sha256=s.get("file_sha256", ""), title=s.get("title") or "")
    if s["source_set"] == "working":
        a = s.get("ahus", [])
        r["ahu_tags"] = "; ".join(x["tag"]["value"] for x in a)
        if a:
            r["duty_standby"] = s.get("duty_standby") or ""
            r["supply_per_unit_L_s"] = "; ".join(x["duty_supply_L_s"]["as_written"] or "" for x in a)
            r["supply_box"] = json.dumps([x["duty_supply_L_s"]["box"] for x in a])
        r["plant_level"] = s.get("plant_level") or ""
        r["served_level"] = s.get("served_level") or ""
        oa = s.get("outdoor_air")
        if oa:
            r["oa_source"] = oa["source"]
            r["oa_L_s_as_written"] = "; ".join("%s: %s" % (l["to"], l["as_written"]) for l in oa["lines"])
            r["oa_box"] = json.dumps([l["box"] for l in oa["lines"]])
        ra = s.get("return_air", {}).get("fans", [])
        r["return_fans"] = "; ".join(f["tag"]["value"] for f in ra)
        r["return_duty_as_written"] = "; ".join(f["duty"]["as_written"] or "" for f in ra)
        r["return_box"] = json.dumps([f["duty"]["box"] for f in ra])
        sp = s.get("spill_relief", {})
        r["spill_as_written"] = "; ".join(l["as_written"] for l in sp.get("lines", []))
        r["spill_box"] = json.dumps([l["box"] for l in sp.get("lines", [])])
        r["exhaust_fans"] = "; ".join(_tb(f) for f in s.get("exhaust_fans", []))
        iso = s.get("isolation_exhaust", {})
        r["isolation_exhaust"] = "; ".join(
            ["%s %s" % (l["as_written"], json.dumps(l["box"])) for l in iso.get("header_labels", [])]
            + ["target " + t for t in iso.get("targets", [])])
        r["bibo_units"] = "; ".join(b["tag"]["value"] for b in s.get("bibo_units", []))
        r["filters"] = "; ".join(s.get("filters_on_room_boxes", []))
        acc = Counter()
        for x in a + s.get("return_air", {}).get("fans", []) + s.get("exhaust_fans", []) + s.get("bibo_units", []):
            for k, n in x["accessories_as_drawn"].items():
                acc[k] += n
        r["accessories_as_drawn"] = json.dumps(dict(acc), sort_keys=True)
        rooms = s.get("rooms", [])
        r["room_boxes"] = len(rooms)
        r["room_rows_with_typicals"] = sum(x["typical_count"] for x in rooms)
        tot = s.get("totals_register")
        if tot:
            r["rooms_SA_sum_L_s"] = tot["rooms_SA_sum_with_typicals_L_s"]
            r["check_result"] = tot["check_result"]
        r["requirement_notes"] = ";".join(map(str, s.get("requirement_note_numbers", [])))
    else:
        r["spaces"] = "; ".join("%s | %s | %s | %s" % (x["space_ref"], x["name_as_written"],
                                                       x["area_label_as_written"], x["mode_label_as_written"])
                                for x in s["spaces"])
        r["equipment_tags"] = "; ".join("%s %s" % (e["tag_as_written"], json.dumps(e["box"])) for e in s["equipment"])
        r["rates_as_written"] = "; ".join(s.get("rates_beside_fans_as_written", []))
        r["oa_exhaust_path"] = s.get("outdoor_air_exhaust_path", "")
        r["room_boxes"] = len(s["spaces"])
        r["supply_per_unit_L_s"] = "; ".join(
            "%s %s" % (e["tag_as_written"], e["schedule"]["supply_airflow_L_s"]) for e in s["equipment"]
            if e["schedule"] and e["schedule"]["supply_airflow_L_s"] is not None)
        r["storey"] = ";".join(map(str, s["storey"]))
    return r


# --------------------------------------------------------------------------- #
# Tender versus working summary
# --------------------------------------------------------------------------- #
SUM_COLS = ["sheet", "storey", "status", "tender_issue", "rooms_tender", "rooms_working",
            "rooms_identical", "rooms_added", "rooms_removed", "rooms_airflow_changed",
            "rooms_other_changed", "supply_delta_sum_L_s", "oa_note_changes", "tags_added",
            "tags_removed", "duty_changed", "connection_changes", "notes_tender", "notes_working",
            "notes_added", "notes_removed", "notes_changed", "rooms_added_names", "rooms_removed_names",
            "airflow_change_detail", "other_change_detail", "tag_change_detail", "note_change_detail",
            "comment"]


def _short(items: List[str], n: int = 12) -> str:
    items = [i for i in items if i]
    return " | ".join(items[:n]) + (" | +%d more" % (len(items) - n) if len(items) > n else "")


def build_summary(reg: Reg, working: Dict[str, Any], read_notes: bool = True) -> List[Dict[str, Any]]:
    tv = _group(reg.tvw, "sheet")
    ev = _group(reg.etvw, "sheet")
    t_rooms = _group([r for r in reg.rooms if r["issue_set"] == "tender"], "sheet")
    w_rooms = _group([r for r in reg.rooms if r["issue_set"] == "working"], "sheet")
    t_conn = _group([r for r in reg.conn if r["issue_set"] == "tender"], "sheet")
    w_conn = _group([r for r in reg.conn if r["issue_set"] == "working"], "sheet")
    t_eq = _group([r for r in reg.equip if r["issue_set"] == "tender"], "sheet")
    w_eq = _group([r for r in reg.equip if r["issue_set"] == "working"], "sheet")
    out = []
    for sh in sorted(working["sheets"]):
        meta = working["sheets"][sh]
        trows = tv.get(sh, [])
        c = Counter(r["status"] for r in trows)
        tender_issue = (t_rooms.get(sh) or t_eq.get(sh) or [{"issue": ""}])[0]["issue"]
        in_tender = bool(t_rooms.get(sh) or t_eq.get(sh))
        row = {k: "" for k in SUM_COLS}
        row.update(sheet=sh, storey=meta["storey"], tender_issue=tender_issue)
        if not in_tender:
            row.update(status="new in working set (no tender issue of this sheet in the registers)",
                       rooms_working=len(w_rooms.get(sh, [])), rooms_added=len(w_rooms.get(sh, [])),
                       notes_working=len(meta["notes"]), rooms_tender=0,
                       comment="Whole sheet is new; every room, tag and note counts as added.")
            out.append(row)
            continue
        airflow = [r for r in trows if r["status"] == "airflow_changed"]
        other = [r for r in trows if r["status"] == "other_changed"]
        added = [r for r in trows if r["status"] == "only_in_working"]
        removed = [r for r in trows if r["status"] == "only_in_tender"]
        er = ev.get(sh, [])
        ec = Counter(r["status"] for r in er)
        # tags: equipment register comparison plus anything the AHU/fan tag sets show
        real = lambda r: r["type"] in ("AHU", "BIBO", "PAHU", "PAHU (off-sheet source)") or r["type"] in FAN_TYPES  # noqa: E731
        tt = {r["tag"] for r in t_eq.get(sh, []) if real(r)}
        ww = {r["tag"] for r in w_eq.get(sh, []) if real(r)}
        tag_added, tag_removed = sorted(ww - tt), sorted(tt - ww)
        duty = [r for r in er if r["status"] == "duty_changed"]
        # connection flow changes
        def ck(rows):
            return Counter((r["from_tag"], r["to_tag_or_room"], r["service"], r["size_flow_as_written"])
                           for r in rows)
        tc, wc = ck(t_conn.get(sh, [])), ck(w_conn.get(sh, []))
        conn_changes = sum(((tc - wc) + (wc - tc)).values())
        # notes
        tn = wn = []
        notes_added = notes_removed = notes_changed = 0
        ndetail = []
        wnotes = meta["notes"]
        tnotes = _tender_notes(reg, sh, w_rooms, t_rooms, read_notes)
        if tnotes is not None:
            tmap = {norm_note(n["text"]): n for n in tnotes}
            wmap = {norm_note(n["text"]): n for n in wnotes}
            only_t = [n for k, n in tmap.items() if k not in wmap]
            only_w = [n for k, n in wmap.items() if k not in tmap]
            # pair a changed note with its counterpart by text similarity (number may move)
            import difflib
            for n in list(only_w):
                best, br = None, 0.0
                for t in only_t:
                    rr = difflib.SequenceMatcher(None, norm_note(t["text"]), norm_note(n["text"])).ratio()
                    if rr > br:
                        best, br = t, rr
                if best is not None and br >= 0.5:
                    notes_changed += 1
                    ndetail.append("note %s (tender %s) reworded, similarity %.2f" % (n["number"], best["number"], br))
                    only_t.remove(best); only_w.remove(n)
            notes_added, notes_removed = len(only_w), len(only_t)
            ndetail += ["note %s added" % n["number"] for n in only_w] + [
                "note %s removed" % n["number"] for n in only_t]
        row.update(
            status="differs" if (added or removed or airflow or other or tag_added or tag_removed or duty
                                 or conn_changes or notes_added or notes_removed or notes_changed)
            else "no difference found in the registers",
            rooms_tender=len(t_rooms.get(sh, [])), rooms_working=len(w_rooms.get(sh, [])),
            rooms_identical=c.get("identical", 0), rooms_added=c.get("only_in_working", 0),
            rooms_removed=c.get("only_in_tender", 0), rooms_airflow_changed=c.get("airflow_changed", 0),
            rooms_other_changed=c.get("other_changed", 0),
            supply_delta_sum_L_s=round(sum(num(r["delta_supply_L_s"]) or 0 for r in trows), 1),
            oa_note_changes=sum(1 for r in trows if num(r["delta_oa_L_s"])),
            tags_added=len(tag_added), tags_removed=len(tag_removed), duty_changed=len(duty),
            connection_changes=conn_changes,
            notes_tender=len(tnotes) if tnotes is not None else "not read",
            notes_working=len(wnotes), notes_added=notes_added, notes_removed=notes_removed,
            notes_changed=notes_changed,
            rooms_added_names=_short([r["room_name_as_written"] for r in added]),
            rooms_removed_names=_short([r["room_name_as_written"] for r in removed]),
            airflow_change_detail=_short(["%s: %s" % (r["room_name_as_written"], r["airflow_differences"]) for r in airflow], 8),
            other_change_detail=_short(["%s: %s" % (r["room_name_as_written"], r["other_differences"]) for r in other], 8),
            tag_change_detail=_short(["+" + t for t in tag_added] + ["-" + t for t in tag_removed]
                                     + ["%s %s -> %s" % (r["tag"], r["tender_duty_as_written"], r["working_duty_as_written"]) for r in duty], 10),
            note_change_detail=_short(ndetail, 10),
            comment="" if tnotes is not None else "tender notes not read",
        )
        out.append(row)
    return out


_TNOTES_CACHE: Dict[str, Any] = {}


def _tender_notes(reg: Reg, sh: str, w_rooms, t_rooms, read: bool):
    reg_n = [n for n in reg.notes if n["sheet"] == sh and n["issue"] == "tender"]
    if reg_n:
        return [{"number": n["number"], "text": re.sub(r"\s+", " ", n["text"])} for n in reg_n]
    if not read:
        return None
    rows = t_rooms.get(sh)
    if not rows:
        return None
    pdf = reg.root / "data/source" / rows[0]["drive_rel_path"]
    return read_sheet_notes(pdf)


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #
def run(root: Path = ROOT, read_notes: bool = True) -> Dict[str, Any]:
    reg = Reg(root)
    working = build_working(reg, read_notes)
    appd = build_appd(root)
    systems = working["systems"] + appd["systems"]
    model = {
        "meta": {"generator": "src/schematics/model.py", "units": "L/s, Pa, page-view points",
                 "box_frame": "page view, origin top-left, y down, points",
                 "note": "A value without its own box carries the box of its cell, table, label or "
                         "outline; box_level says which. Duty and standby: when the sheet does not write "
                         "which unit of an A/B pair is duty, the record says so and no unit is chosen.",
                 "counts": {}},
        "general_notes_IP-BH-00001": working["general_notes"],
        "sheets": working["sheets"], "systems": systems,
    }
    ws = [s for s in systems if s["kind"] == "working_ahu_system"]
    counts = {
        "working_sheets": len(working["sheets"]),
        "working_systems": len(ws),
        "working_unassigned_room_groups": sum(1 for s in systems if s["kind"] == "working_unassigned_rooms"),
        "working_ahu_units": sum(len(s["ahus"]) for s in ws),
        "working_room_boxes_in_systems": sum(len(s["rooms"]) for s in systems if s["source_set"] == "working"),
        "working_room_boxes_distinct": len({r["room_id"] for s in systems if s["source_set"] == "working" for r in s["rooms"]}),
        "working_fans": sum(len(s["return_air"]["fans"]) + len(s["exhaust_fans"]) for s in ws),
        "appd_systems": sum(1 for s in systems if s["kind"] in ("appd_ahu_system", "appd_fcu_group", "appd_mv_fan_system")),
        "appd_ahu_systems": sum(1 for s in systems if s["kind"] == "appd_ahu_system"),
        "appd_fcu_groups": sum(1 for s in systems if s["kind"] == "appd_fcu_group"),
        "appd_mv_fan_systems": sum(1 for s in systems if s["kind"] == "appd_mv_fan_system"),
        "appd_spaces": appd["space_count"], "appd_spaces_without_equipment": appd["unserved_space_count"],
        "appd_equipment_occurrences": sum(len(s["equipment"]) for s in systems if s["source_set"] == "appd"),
    }
    model["meta"]["counts"] = counts
    (root / REG).mkdir(parents=True, exist_ok=True)
    with open(root / REG / "schematic_model.json", "w", encoding="utf-8") as fh:
        json.dump(model, fh, indent=1, ensure_ascii=False)
    with open(root / REG / "schematic_systems.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLS)
        w.writeheader()
        for s in systems:
            w.writerow(flat_row(s))
    summ = build_summary(reg, working, read_notes)
    (root / "out/qa").mkdir(parents=True, exist_ok=True)
    with open(root / "out/qa/schematic_tender_vs_working_summary.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SUM_COLS)
        w.writeheader()
        w.writerows(summ)
    counts["summary_rows"] = len(summ)
    counts["systems_csv_rows"] = len(systems)
    return counts


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Build the schematic model, systems register and tender-versus-working summary.")
    ap.add_argument("command", choices=["run"])
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--no-pdf", action="store_true", help="do not read note text from PDFs")
    a = ap.parse_args(argv)
    counts = run(Path(a.root), read_notes=not a.no_pdf)
    print(json.dumps(counts, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
