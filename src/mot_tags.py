"""MOT NSC AHU tags: the AHU cross-verification workbook against the MOT schedule, schematics and zones (D-038).

Reads "Short Version DD Booklet/AHU Cross Verification - MOT NSC Register.xlsx" (every sheet, formulas and cached
values) into out/registers/mot_ahu_crossref.csv, then compares it with

* out/registers/mot_ahu_schedule.csv          (MOT NSC Overall AC Equipment Schedule),
* out/registers/schematic_equipment.csv       (issue_set mot_nsc, AHU and PAHU rows),
* out/registers/mot_theatre_rooms.csv         (schematic room boxes and Appendix J rows),
* out/registers/zone_assignment_L04..L06.csv  (the AHU named for each room)

and writes out/qa/mot_tag_differences.csv.  Differences are listed, never resolved: the governing tag per D-038 is the
workbook's 'AHU Tag (Working Drawing)' column; every other spelling is a variant that is shown, not used.

CLI:  python3 -m src.mot_tags run
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import OrderedDict, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
REG = ROOT / "out" / "registers"
QA = ROOT / "out" / "qa"
XLSX_REL = "Short Version DD Booklet/AHU Cross Verification - MOT NSC Register.xlsx"
XLSX = ROOT / "data" / "source" / XLSX_REL

CROSSREF_COLUMNS = [
    "crossref_row", "item_no", "governing_tag_as_printed", "governing_tags_expanded",
    "l07_old_reference_as_printed", "variants_replaced", "variants_in_evidence_cells",
    "level_as_printed", "pod", "description_as_printed", "type",
    "sa_norm_l_s_as_printed", "sa_set_l_s_as_printed",
    "air_side_schematic", "water_side_schematic", "ac_equip_schedule", "dd_booklet", "mv_schedule",
    "acmv_nsc_ahu_schedules_corr5", "acmv_nsc_drawings", "nsc_scope", "key_remarks_as_printed",
    "rooms_served_theatre_register", "rooms_served_zone_assignment",
    "sheet", "source_doc", "cells", "method", "confidence", "notes",
]

DIFF_COLUMNS = [
    "category", "crossref_row", "unit", "governing_tag", "difference_type", "detail",
    "tags_in_crossref", "tags_in_ac_schedule", "tags_in_schematic_mot_nsc", "tags_in_theatre_register", "tags_in_zone_assignment",
    "airflow_crossref_l_s", "airflow_ac_schedule_l_s", "airflow_schematic_l_s",
    "crossref_remark", "evidence", "decision", "status",
]

HEADER_ROW = 4
FIRST_DATA_ROW = 5
COLMAP = {  # worksheet column -> output field
    1: "item_no", 2: "governing_tag_as_printed", 3: "l07_old_reference_as_printed", 4: "level_as_printed", 5: "pod",
    6: "description_as_printed", 7: "type", 8: "sa_norm_l_s_as_printed", 9: "sa_set_l_s_as_printed",
    10: "air_side_schematic", 11: "water_side_schematic", 12: "ac_equip_schedule", 13: "dd_booklet", 14: "mv_schedule",
    15: "acmv_nsc_ahu_schedules_corr5", 16: "acmv_nsc_drawings", 17: "nsc_scope", 18: "key_remarks_as_printed",
}
EVIDENCE_FIELDS = ["air_side_schematic", "water_side_schematic", "ac_equip_schedule", "dd_booklet", "mv_schedule",
                   "acmv_nsc_ahu_schedules_corr5"]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.is_file():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, columns: List[str], rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="raise")
        w.writeheader()
        for r in rows:
            w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in columns})


def num(text: Any) -> Optional[float]:
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(text or ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def clean_cell(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).replace("\n", " ").strip()
    return "" if s in ("—", "-") else s


_TAG_START = re.compile(r"^(?:AHU|PAHU)-")


def expand_tags(text: str) -> List[str]:
    """Expand a workbook tag cell into single tags.

    'AHU-B-07-05STER-01A/B' -> -01A, -01B;  'AHU-B-07-05MOT-01 / MOTHYB-01' -> AHU-B-07-05MOT-01, AHU-B-07-05MOTHYB-01;
    'AHU-B-07-05MOTSTOR-01A/B + MOTSTORE-01' -> three tags; '(?)' and '(x2)' notes are dropped; 'CORELB-01/02' -> -01, -02.
    """
    t = re.sub(r"\((?:[^)]*)\)", " ", text or "").replace("×", "x")
    t = t.strip()
    if not t or t in ("—", "-"):
        return []
    parts = [p.strip() for p in re.split(r"\s+[/+]\s+|\s+\+\s+", t) if p.strip()]
    out: List[str] = []
    prefix = ""
    for p in parts:
        if _TAG_START.match(p):
            m = re.match(r"^((?:AHU|PAHU)-B-\d\d-\d\d)", p)
            prefix = m.group(1) + "" if m else prefix
            tag = p
        elif p.startswith("-") and out:
            tag = re.sub(r"-\d\d[A-Z]?$", "", out[-1]) + p  # '-04A/B' after '...PACU-03A/B' keeps the PACU stem
        else:
            m = re.match(r"^([A-Z]+-B-\d\d-\d\d)", out[0]) if out else None
            tag = (m.group(1) if m else prefix) + p
        out.extend(_expand_suffix(tag))
    seen: List[str] = []
    for o in out:
        if o and o not in seen:
            seen.append(o)
    return seen


def _expand_suffix(tag: str) -> List[str]:
    tag = tag.strip()
    m = re.match(r"^(.*-\d\d)([A-Z])((?:/[A-Z])+)$", tag)  # -01A/B or -02A/B/C
    if m:
        return [m.group(1) + m.group(2)] + [m.group(1) + x for x in m.group(3).split("/") if x]
    m = re.match(r"^(.*-)(\d\d)((?:/\d\d)+)$", tag)  # -01/02
    if m:
        return [m.group(1) + m.group(2)] + [m.group(1) + x for x in m.group(3).split("/") if x]
    return [tag]


def loose_key(tag: str) -> str:
    """Comparison key: upper case, plant-level prefix removed, A/B suffix removed, 'MOTlSONP' read as 'MOTISONP'."""
    t = (tag or "").strip().upper()
    t = re.sub(r"^(?:AHU|PAHU)-B-\d\d-", "", t)
    t = t.replace("LSONP", "ISONP")
    t = re.sub(r"(?<=\d)[AB](?:/[AB])?$", "", t)
    return t


def strict_key(tag: str) -> str:
    """Tag with the duty/standby suffix ('01A', '01B' or '01A/B') removed, plant-level prefix kept."""
    return re.sub(r"(?<=\d)[AB](?:/[AB])?$", "", (tag or "").strip())


VARIANT_TOKENS = re.compile(r"\b(?:AHU-B-\d\d-\d\d[A-Z]+-\d\d[A-Z]?|MOT[A-Z]*-\d\d[A-Z]?|DSOT[A-Z]*-\d\d[A-Z]?|STERS|MOTSTER|MOTSTA-01|MOTSTAFF-01|MOTCROB|DOTSUPP|MOTlSONP|-\d\d\b)")


def variant_tokens(text: str) -> List[str]:
    seen: List[str] = []
    for m in VARIANT_TOKENS.finditer(text or ""):
        tok = m.group(0)
        if tok not in seen:
            seen.append(tok)
    return seen


# --------------------------------------------------------------------------- #
# workbook -> register
# --------------------------------------------------------------------------- #
def read_workbook(path: Path = XLSX) -> List[Dict[str, Any]]:
    """Every sheet; one dict per non-empty data row with values and formulas side by side."""
    import openpyxl
    wf = openpyxl.load_workbook(str(path))                   # formulas
    wv = openpyxl.load_workbook(str(path), data_only=True)   # cached values
    rows: List[Dict[str, Any]] = []
    for ws in wf.worksheets:
        wsv = wv[ws.title]
        n_formulas = 0
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.startswith("="):
                    n_formulas += 1
        for r in range(FIRST_DATA_ROW, ws.max_row + 1):
            vals = {c: wsv.cell(r, c).value for c in range(1, ws.max_column + 1)}
            if all(v in (None, "") for v in vals.values()):
                continue
            rows.append({"sheet": ws.title, "row": r, "values": vals, "n_formulas_in_sheet": n_formulas,
                         "formulas": {c: ws.cell(r, c).value for c in vals if isinstance(ws.cell(r, c).value, str) and str(ws.cell(r, c).value).startswith("=")}})
    return rows


def served_by_tag(rooms: List[Dict[str, str]], zones: List[Dict[str, str]]):
    """Index theatre-register rooms and zone-assignment rooms by loose AHU key."""
    th: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for r in rooms:
        for t in re.split(r"\s*\|\s*", r.get("ahu_pahu_tag", "")):
            if t:
                th[loose_key(t)].append(r)
    zn: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for z in zones:
        if z.get("class_kind") == "AHU":
            zn[loose_key(z["class"])].append(z)
    return th, zn


def load_zones() -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for lv in ("L04", "L05", "L06"):
        for r in read_csv(REG / ("zone_assignment_%s.csv" % lv)):
            r["_file"] = "zone_assignment_%s.csv" % lv
            out.append(r)
    return out


def _zone_summary(zrows: List[Dict[str, str]]) -> str:
    if not zrows:
        return ""
    area = sum(num(z.get("area_m2")) or 0.0 for z in zrows)
    names: "OrderedDict[str, int]" = OrderedDict()
    for z in zrows:
        names[z["room_name"]] = names.get(z["room_name"], 0) + 1
    top = "; ".join("%s x%d" % (k, v) if v > 1 else k for k, v in list(names.items())[:10])
    more = " (+%d more names)" % (len(names) - 10) if len(names) > 10 else ""
    return "%d rooms, %.1f m2 (%s): %s%s" % (len(zrows), area, "/".join(sorted({z["_file"].replace("zone_assignment_", "").replace(".csv", "") for z in zrows})), top, more)


def _theatre_summary(rrows: List[Dict[str, str]]) -> str:
    if not rrows:
        return ""
    seen: "OrderedDict[str, None]" = OrderedDict()
    for r in rrows:
        k = "%s %s (%s)" % (r["room_name_printed"], r.get("pod_or_suite", ""), r["source_kind"].replace("air-side schematic room box", "schematic").replace("Appendix J schedule of modes row", "App J").replace("HEPA terminal schedule row", "HEPA sched"))
        seen[k.replace("  ", " ")] = None
    items = list(seen)[:12]
    return "; ".join(items) + (" (+%d more)" % (len(seen) - 12) if len(seen) > 12 else "")


def crossref_rows(wb_rows: List[Dict[str, Any]], rooms: List[Dict[str, str]], zones: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    th, zn = served_by_tag(rooms, zones)
    out: List[Dict[str, Any]] = []
    for wr in wb_rows:
        v = {COLMAP[c]: clean_cell(wr["values"].get(c)) for c in COLMAP}
        gov = expand_tags(v["governing_tag_as_printed"])
        old = expand_tags(v["l07_old_reference_as_printed"])
        evid = []
        for f in EVIDENCE_FIELDS:
            val = v[f]
            extra = re.sub(r"[✅⚠❌️]", "", val).strip()
            if extra:
                evid.append("%s: %s" % (f, extra))
        rem_vars = variant_tokens(v["key_remarks_as_printed"])
        variants = [x for x in old if x not in gov]
        notes: List[str] = []
        if not v["item_no"]:
            notes.append("row has no item number; level printed '%s'" % v["level_as_printed"])
        if not gov:
            notes.append("no governing tag printed in the cross-reference; only the L07 old reference (or nothing)")
        if wr["n_formulas_in_sheet"] == 0:
            notes.append("sheet holds no formulas; all cells are typed values")
        if wr["formulas"]:
            notes.append("formula cells: %s" % wr["formulas"])
        keys = {loose_key(t) for t in gov}
        trows: List[Dict[str, str]] = []
        zrows: List[Dict[str, str]] = []
        for k in keys:
            trows += th.get(k, [])
            zrows += zn.get(k, [])
        trows = [r for i, r in enumerate(trows) if r not in trows[:i]]
        zrows = [r for i, r in enumerate(zrows) if r not in zrows[:i]]
        cells = "A%d:R%d (column B tag, C L07 reference, H/I airflow, R remarks)" % (wr["row"], wr["row"])
        out.append(dict(
            v, crossref_row=wr["row"], governing_tags_expanded="; ".join(gov), variants_replaced="; ".join(variants),
            variants_in_evidence_cells="; ".join(evid + (["tokens in remarks: " + ", ".join(rem_vars)] if rem_vars else [])),
            rooms_served_theatre_register=_theatre_summary(trows), rooms_served_zone_assignment=_zone_summary(zrows),
            sheet=wr["sheet"], source_doc=XLSX_REL, cells=cells,
            method="openpyxl, formulas and cached values both read (no formulas present); text as printed, line breaks flattened",
            confidence="high", notes="; ".join(notes),
        ))
    return out


# --------------------------------------------------------------------------- #
# comparison
# --------------------------------------------------------------------------- #
def category_of(tag: str, desc: str = "") -> str:
    t = (tag or "").upper()
    d = (desc or "").upper()
    if "CATH" in t or "CATH" in d:
        return "cath lab"
    if "ANGIO" in t:
        return "angiography"
    if "HYB" in t or "HYBRID" in d or re.search(r"MOT-\d\d", t):
        return "MOTHYB hybrid theatre"
    if re.search(r"MOTE-\d\d", t) or "EMERGENCY" in d:
        return "emergency theatre"
    if "MOTCT" in t or "UCV" in d or "DSOT-12" in t or "DSOT-10" in t:
        return "CT/UCV theatre"
    if "STER" in t:
        return "sterile hold"
    if "SUPP" in t:
        return "support"
    if "ISONP" in t.replace("LSONP", "ISONP"):
        return "ISO NP theatre"
    if t.startswith("PAHU"):
        return "PAHU"
    return "other"


FOCUS_CATEGORIES = {"cath lab", "MOTHYB hybrid theatre", "emergency theatre", "CT/UCV theatre"}


def _tags_by_key(tags: List[str]) -> Dict[str, List[str]]:
    d: Dict[str, List[str]] = defaultdict(list)
    for t in tags:
        if t not in d[loose_key(t)]:
            d[loose_key(t)].append(t)
    return d


def _status(gov: str, found: List[str]) -> str:
    if not found:
        return "absent"
    if gov in found:
        return "exact"
    gk = strict_key(gov)
    if any(strict_key(f) == gk for f in found):
        return "pair form differs (A/B)"
    return "variant spelling"


def compare(cross: List[Dict[str, Any]], sched: List[Dict[str, str]], sch_eq: List[Dict[str, str]],
            rooms: List[Dict[str, str]], zones: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    sched_tags = [r["tag"] for r in sched if r["tag"].startswith(("AHU-", "PAHU-"))]
    sched_flow = {r["tag"]: num(r["supply_airflow_l_s"]) for r in sched}
    sch_rows = [r for r in sch_eq if r["issue_set"] == "mot_nsc" and r["type"] in ("AHU", "PAHU")]
    sch_tags = sorted({r["tag"] for r in sch_rows})
    sch_flow: Dict[str, float] = {}
    for r in sch_rows:
        n = num(r["duty_as_written"])
        if n is not None:
            sch_flow.setdefault(r["tag"], n)
    sch_sheet: Dict[str, str] = {}
    for r in sch_rows:
        sch_sheet.setdefault(r["tag"], "%s p%s %s" % (r["sheet"], r["page"], r["issue"]))
    room_tags = sorted({t for r in rooms for t in re.split(r"\s*\|\s*", r.get("ahu_pahu_tag", "")) if t.startswith(("AHU-", "PAHU-"))})
    zone_tags = sorted({z["class"] for z in zones if z.get("class_kind") == "AHU"})
    S, C, R, Z = (_tags_by_key(x) for x in (sched_tags, sch_tags, room_tags, zone_tags))
    diffs: List[Dict[str, Any]] = []
    gov_rows: Dict[str, List[int]] = defaultdict(list)
    all_cross_keys = set()
    for row in cross:
        gov = [g for g in row["governing_tags_expanded"].split("; ") if g]
        for g in gov:
            gov_rows[g].append(row["crossref_row"])
        for g in gov + [x for x in row["variants_replaced"].split("; ") if x]:
            all_cross_keys.add(loose_key(g))
    for row in cross:
        gov = [g for g in row["governing_tags_expanded"].split("; ") if g]
        unit = row["description_as_printed"] or row["l07_old_reference_as_printed"]
        if not gov:
            continue
        cat = category_of(gov[0], unit)
        cross_flow = num(row["sa_norm_l_s_as_printed"])
        for g in gov:
            k = loose_key(g)
            found = {"ac_schedule": S.get(k, []), "schematic": C.get(k, []), "theatre_register": R.get(k, []), "zones": Z.get(k, [])}
            # a pair written 'X-01A/B' in the zones is one tag; also accept the A and B members
            st = {n: _status(g, f) for n, f in found.items()}
            problems = {n: s for n, s in st.items() if s not in ("exact", "pair form differs (A/B)")}
            sf = [sched_flow.get(t) for t in found["ac_schedule"] if sched_flow.get(t) is not None]
            kf = [sch_flow.get(t) for t in found["schematic"] if sch_flow.get(t) is not None]
            flow_diff = []
            types: List[str] = []
            details: List[str] = []
            if cross_flow is not None and sf and abs(max(sf) - cross_flow) > 0.5:
                flow_diff.append("AC schedule %s vs cross-reference %s" % (int(max(sf)), int(cross_flow)))
            if cross_flow is not None and kf and abs(max(kf) - cross_flow) > 0.5:
                flow_diff.append("schematic %s vs cross-reference %s" % (int(max(kf)), int(cross_flow)))
            focus = cat in FOCUS_CATEGORIES
            if not problems and not flow_diff and len(gov_rows[g]) == 1 and not focus:
                continue
            if not problems and not flow_diff and len(gov_rows[g]) == 1 and focus:
                types.append("none: tag agrees in every source (zone assignment prints the A/B pair as one zone, by convention)")
            for n, s in problems.items():
                label = {"ac_schedule": "MOT AC schedule", "schematic": "MOT schematics", "theatre_register": "theatre register (schematic/App J)", "zones": "zone assignment"}[n]
                if s == "absent":
                    types.append("not in %s" % label)
                    details.append("%s: no tag with the same unit key" % label)
                else:
                    types.append("%s in %s" % (s, label))
                    details.append("%s prints %s" % (label, ", ".join(found[n])))
            if flow_diff:
                types.append("airflow differs")
                details.extend(flow_diff)
            if len(gov_rows[g]) > 1:
                types.append("same governing tag on several cross-reference rows")
                details.append("tag %s is governing on rows %s" % (g, ", ".join(map(str, gov_rows[g]))))
            if row["l07_old_reference_as_printed"]:
                details.append("variant not used (L07 old reference): %s" % row["l07_old_reference_as_printed"])
            if row["variants_in_evidence_cells"]:
                details.append("variants named in the workbook: %s" % row["variants_in_evidence_cells"])
            if row["nsc_scope"] and row["nsc_scope"] != "MOT":
                details.append("cross-reference scope: %s" % row["nsc_scope"])
            diffs.append({
                "category": "ACMV NSC scope unit" if row["nsc_scope"] == "ACMV NSC" else category_of(g, unit), "crossref_row": row["crossref_row"], "unit": unit, "governing_tag": g,
                "difference_type": "; ".join(types), "detail": "; ".join(details),
                "tags_in_crossref": "%s%s" % (g, ("  [L07 old ref: %s]" % row["l07_old_reference_as_printed"]) if row["l07_old_reference_as_printed"] else ""),
                "tags_in_ac_schedule": "; ".join(found["ac_schedule"]), "tags_in_schematic_mot_nsc": "; ".join(found["schematic"]),
                "tags_in_theatre_register": "; ".join(found["theatre_register"]), "tags_in_zone_assignment": "; ".join(found["zones"]),
                "airflow_crossref_l_s": "" if cross_flow is None else int(cross_flow),
                "airflow_ac_schedule_l_s": "; ".join(str(int(x)) for x in sf), "airflow_schematic_l_s": "; ".join(str(int(x)) for x in kf),
                "crossref_remark": row["key_remarks_as_printed"],
                "evidence": "cross-reference row %s; AC schedule %s; schematics %s" % (row["crossref_row"], "mot_ahu_schedule.csv", "; ".join(sch_sheet[t] for t in found["schematic"][:2])),
                "decision": "D-038: governing tag is the cross-reference column B; variants listed, not used", "status": "open: listed, not resolved",
            })
    # tags present in a source but unknown to the cross-reference (governing or old reference)
    for name, tags, flow in (("MOT AC schedule", sched_tags, sched_flow), ("MOT schematics", sch_tags, sch_flow), ("theatre register", room_tags, {}), ("zone assignment", zone_tags, {})):
        for t in tags:
            k = loose_key(t)
            if k in all_cross_keys:
                continue
            if not re.search(r"(MOT|DSOT|STER|CATH|ANGIO|PACU|PREOP|ADMIT|PRECON|SUPP|MOTE|STAFF|STORE)", t):
                continue
            if name == "zone assignment" and not re.search(r"-0[4-6]\w", t[:14]):
                continue
            if name == "zone assignment" and not re.search(r"(MOT|DSOT|STER|CATH|ANGIO|PACU|PREOP|ADMIT|STAFF|STORE)", t):
                continue
            diffs.append({
                "category": category_of(t), "crossref_row": "", "unit": "", "governing_tag": "",
                "difference_type": "tag not in the cross-reference", "detail": "%s prints %s; no governing tag or L07 old reference in the workbook has this unit key" % (name, t),
                "tags_in_crossref": "", "tags_in_ac_schedule": t if name == "MOT AC schedule" else "", "tags_in_schematic_mot_nsc": t if name == "MOT schematics" else "",
                "tags_in_theatre_register": t if name == "theatre register" else "", "tags_in_zone_assignment": t if name == "zone assignment" else "",
                "airflow_crossref_l_s": "", "airflow_ac_schedule_l_s": "" if flow.get(t) is None or name != "MOT AC schedule" else int(flow[t]),
                "airflow_schematic_l_s": "" if flow.get(t) is None or name != "MOT schematics" else int(flow[t]),
                "crossref_remark": "", "evidence": name, "decision": "D-038", "status": "open: listed, not resolved",
            })
    return diffs


def run(out_reg: Optional[Path] = None, out_qa: Optional[Path] = None) -> Dict[str, Any]:
    reg = out_reg or REG
    qa = out_qa or QA
    rooms = read_csv(REG / "mot_theatre_rooms.csv")
    zones = load_zones()
    wb_rows = read_workbook()
    cross = crossref_rows(wb_rows, rooms, zones)
    write_csv(reg / "mot_ahu_crossref.csv", CROSSREF_COLUMNS, cross)
    diffs = compare(cross, read_csv(REG / "mot_ahu_schedule.csv"), read_csv(REG / "schematic_equipment.csv"), rooms, zones)
    write_csv(qa / "mot_tag_differences.csv", DIFF_COLUMNS, diffs)
    by_cat: Dict[str, int] = defaultdict(int)
    for d in diffs:
        by_cat[d["category"]] += 1
    return {"crossref_rows": len(cross), "difference_rows": len(diffs), "by_category": dict(by_cat)}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="write mot_ahu_crossref.csv and mot_tag_differences.csv")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        import json
        print(json.dumps(run(), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
