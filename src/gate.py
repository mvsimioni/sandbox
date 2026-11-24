"""Scripted readiness gate (D-069), no model.  ``python -m src.gate [--trace-only] [--tests FILE]``

Reads the issued deliverables and the registers and writes

* ``out/qa/decision_trace.csv``   one row per decided D-id: config key, reading module, proving test, status (D-068 item 3)
* ``out/qa/readiness_gate.json``  every number below, machine readable
* ``out/qa/readiness_gate.md``    the same, one section per deliverable, and the human-check list

Per deliverable: decisions applied, coverage counts (rooms with unit, criteria, area, height, occupancy, loads),
FAIL checks of the Checks tab, VERIFY flags, language and properties scan.  Gate-level lines: tests, recomputation,
open items, zoning checks, trace.  A line that is not built says NOT BUILT; the gate never prints PASS for it.
Expedited (D-071): the trace is found by scanning the code for D-ids, with a short table of overrides below.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
QA = ROOT / "out" / "qa"
REG = ROOT / "out" / "registers"
WB = ROOT / "out" / "loads" / "workbooks"
ZON = ROOT / "out" / "zoning"

# Decisions that change process or reporting only: they need a reader only where one is named here.
PROCESS = {"D-001", "D-002", "D-003", "D-004", "D-007", "D-014", "D-045", "D-046", "D-047", "D-050", 
           "D-034", "D-041", "D-022", "D-005", "D-009", "D-023", "D-024"}
# Overrides where the scan finds nothing or the wrong place: id -> (config key, module, test)
OVERRIDE = {
    "D-016": ("occupancy.source_order", "recompute/thermal.py; workbook/assemble.py", "test_workbook_round2.py"),
    "D-018": ("occupancy.source_order", "workbook/assemble.py", "test_workbook_round2.py"),
    "D-021": ("(superseded by D-028: no door data)", "pressure.py", "test_pressure.py"),
    "D-005": ("duct leakage on AHU supply", "recompute/thermal.py", "test_recompute_thermal.py"),
    "D-020": ("(replaced by D-053)", "recompute/thermal.py", "test_round5_rules.py"),
    "D-023": ("(replaced by D-054)", "workbook/round5.py", "test_round5_rules.py"),
    "D-024": ("(settled by D-034 and D-064)", "workbook/presume.py", "test_workbook_assemble.py"),
    "D-029": ("(rule in zone class rulings)", "zoning/class_rulings.py; room_links.py", "test_room_links.py"),
    "D-037": ("hfds", "workbook/hfds.py; zoning/class_rulings.py", "test_round5_rules.py"),
    "D-040": ("(booklet zoning by room list)", "zoning/booklet_zones.py", "test_booklet_zones.py"),
    "D-048": ("(booklet zoning by room list)", "zoning/booklet_zones.py; room_links.py", "test_booklet_zones.py"),
    "D-049": ("envelope_round5", "workbook/envelope.py", "test_round5_rules.py"),
    "D-052": ("(precedence of zone class)", "zoning/class_rulings.py", "test_room_links.py"),
    "D-059": ("mot_round5", "room_links.py; zoning/class_rulings.py", "test_room_links.py"),
    "D-061": ("(default occupancy)", "workbook/assemble.py", "test_workbook_round2.py"),
    "D-062": ("(plant rooms of D00)", "workbook/common_areas.py", "test_round5_rules.py"),
    "D-064": ("(presumed rows, HFDS cross-check)", "workbook/presume.py; workbook/hfds.py", "test_round5_rules.py"),
    "D-065": ("(direction-only rooms)", "workbook/assemble.py", "test_round5_rules.py"),
    "D-070": ("(conflict classes)", "zoning/class_rulings.py", "test_room_links.py"),
    # fix pass: readers and tests that exist now
    "D-006": ("extend_to_all_departments", "room_links.py", "test_fix_pass.py"),
    "D-056": ("(two status lines, DRAFT label)", "phase_checks.py; workbook/sheets_basic.py", "test_fix_pass.py"),
    "D-058": ("mot_round5", "workbook/assemble.py; workbook/sheets_systems.py; recompute/thermal.py", "test_fix_pass.py"),
    "D-063": ("(single-method areas; text lint)", "phase_checks.py; workbook/assemble.py", "test_fix_pass.py"),
    "D-068": ("(one ordered build, trace)", "workbook/regenerate.py; gate.py; phase_checks.py", "test_fix_pass.py"),
    "D-069": ("(readiness gate, seeded test)", "gate.py; phase_checks.py", "test_fix_pass.py"),
    "D-071": ("(expedite: wording, coverage notice)", "workbook/clean.py; workbook/envelope.py", "test_fix_pass.py"),
    # Phase 4, council C4 (D-072 to D-088): readers in the schematic build, proving tests in the phase 4 checks
    "D-072": ("(update rule 5 % or 10 L/s)", "schematics/review.py; phase4_c4.py", "test_phase4_c4.py"),
    "D-073": ("(fan-out of all schematics)", "schematics/build_all.py", "test_phase4_c4.py"),
    "D-074": ("(MOT sheets listed; closed by D-082)", "schematics/review.py; phase4_c4.py", "test_phase4_c4.py"),
    "D-075": ("(council C4 rulings)", "schematics/build_all.py", "test_phase4_c4.py"),
    "D-076": ("(added rooms drawn with criteria table)", "schematics/review.py; phase4_c4.py", "test_phase4_c4.py"),
    "D-077": ("(one disposition per drawn entity)", "schematics/review.py; phase4_c4.py", "test_phase4_c4.py"),
    "D-078": ("(negative lookup before add; OA anchor)", "phase4_c4.py", "test_phase4_c4.py"),
    "D-079": ("(one home per unit)", "phase4_c4.py", "test_phase4_c4.py"),
    "D-080": ("(fan ownership; pair counted once)", "phase4_c4.py", "test_phase4_c4.py"),
    "D-081": ("(pressure rows are queries)", "phase4_c4.py", "test_phase4_c4.py"),
    "D-082": ("(MOT listed, not marked up)", "phase4_c4.py", "test_phase4_c4.py"),
    "D-083": ("(input gate for unit-level rows)", "phase4_c4.py", "test_phase4_c4.py"),
    "D-084": ("(room increases need an established link)", "phase4_c4.py", "test_phase4_c4.py"),
    "D-085": ("(sheet against approved zoning)", "phase4_c4.py", "test_phase4_c4.py"),
    "D-086": ("(wording by script; word list)", "phase4_c4.py; schematics/build_all.py", "test_phase4_c4.py"),
    "D-087": ("(one build, stable IDs, staged issue)", "schematics/build_all.py; phase4_c4.py; phase4_seeded.py", "test_phase4_c4.py"),
    "D-088": ("(acceptance: a wrong query is acceptable)", "schematics/build_all.py; phase4_seeded.py", "test_phase4_c4.py"),
}
CALC_RANGE = range(11, 68)  # decisions that change a value: a missing reader is a FAIL there


def _decisions() -> List[Dict[str, str]]:
    from src.decisions import parse_file
    out = []
    for d in parse_file(QA / "decisions_needed.md")[1]:
        out.append({"id": d.id, "title": d.title, "status": d.fields.get("Status", "").lower()})
    return out


def _scan() -> Dict[str, dict]:
    src, tst, cfg = {}, {}, {}
    for f in list((ROOT / "src").rglob("*.py")):
        if f.name == "gate.py":
            continue
        t = f.read_text(errors="ignore")
        for m in set(re.findall(r"D-0\d\d", t)):
            src.setdefault(m, set()).add(str(f.relative_to(ROOT / "src")))
    for f in (ROOT / "tests").glob("*.py"):
        t = f.read_text(errors="ignore")
        for m in set(re.findall(r"D-0\d\d", t)):
            tst.setdefault(m, set()).add(f.name)
    key = None
    for i, line in enumerate((ROOT / "config" / "project.yaml").read_text().splitlines(), 1):
        k = re.match(r"^\s*([A-Za-z_]+):", line)
        for m in set(re.findall(r"D-0\d\d", line)):
            cfg.setdefault(m, []).append(k.group(1) if k else f"line {i}")
    return {"src": src, "tst": tst, "cfg": cfg}


def build_trace() -> List[Dict[str, str]]:
    sc = _scan()
    rows = []
    for d in _decisions():
        i = d["id"]
        n = int(i[2:])
        if i in OVERRIDE:
            key, mod, tst = OVERRIDE[i]
        else:
            key = "; ".join(dict.fromkeys(sc["cfg"].get(i, [])))
            mods = sorted(m for m in sc["src"].get(i, []) if m not in ("open_items.py", "decisions.py")
                          and not m.startswith("recompute/build_all") and not m.startswith("recompute/round4"))
            mod = "; ".join(mods[:4])
            tst = "; ".join(sorted(sc["tst"].get(i, []))[:3])
        mod_ok = all((ROOT / "src" / m.strip()).exists() for m in mod.split(";") if m.strip())
        tst_ok = all((ROOT / "tests" / t.strip()).exists() for t in tst.split(";") if t.strip())
        if i in PROCESS and not mod:
            status = "PROCESS"
        elif mod and tst and mod_ok and tst_ok:
            status = "TRACED"
        elif mod and mod_ok:
            status = "NO TEST"
        elif i in PROCESS:
            status = "PROCESS"
        else:
            status = "NO READER"
        rows.append({"d_id": i, "title": d["title"], "decision_status": d["status"], "config_key": key,
                     "reading_module": mod, "test": tst, "trace_status": status,
                     "gate": ("FAIL" if status == "NO READER" and n in CALC_RANGE else
                              "VERIFY" if status in ("NO READER", "NO TEST") else "PASS")})
    return rows


def write_trace(rows):
    p = QA / "decision_trace.csv"
    with p.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return p


# ---------------------------------------------------------------- workbooks
def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def scan_workbook(path: Path) -> dict:
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    res = OrderedDict(file=path.name, tabs=len(wb.sheetnames))
    p = wb.properties
    res["properties"] = {"creator": p.creator, "last_modified_by": p.lastModifiedBy, "title": p.title,
                         "company": getattr(p, "company", None)}
    res["rooms"] = None
    if "Room Information" in wb.sheetnames:
        rows = list(wb["Room Information"].iter_rows(min_row=5, values_only=True))
        head = rows[0]
        col = {h: j for j, h in enumerate(head) if h and h not in ("Code", "Source ID")}
        data = [r for r in rows[2:] if r[0] not in (None, "")]
        def get(r, name):
            j = col.get(name)
            return r[j] if j is not None and j < len(r) else None
        unit_ok = lambda v: bool(v) and str(v).strip().lower() not in ("no information", "-", "–")
        res["rooms"] = len(data)
        res["with_unit_or_class"] = sum(1 for r in data if unit_ok(get(r, "AHU or system tag")) or unit_ok(get(r, "System type")))
        res["with_unit_tag"] = sum(1 for r in data if unit_ok(get(r, "AHU or system tag")))
        res["no_information"] = sum(1 for r in data if str(get(r, "System type") or "").strip().lower() == "no information")
        res["with_area"] = sum(1 for r in data if (_num(get(r, "Area used")) or 0) > 0)
        res["with_height"] = sum(1 for r in data if (_num(get(r, "Ceiling height")) or 0) > 0)
        res["with_occupancy"] = sum(1 for r in data if _num(get(r, "Occupancy used")) is not None)
        res["with_loads"] = sum(1 for r in data if _num(get(r, "Lighting")) is not None or _num(get(r, "Small power")) is not None
                                or _num(get(r, "Equipment, sensible")) is not None)
        res["storeys"] = sorted({str(get(r, "Storey")) for r in data if get(r, "Storey")})
    if "Criteria" in wb.sheetnames:
        rows = list(wb["Criteria"].iter_rows(min_row=5, values_only=True))
        data = [r for r in rows[2:] if r[0] not in (None, "")]
        res["with_criteria"] = sum(1 for r in data if r[2] not in (None, ""))
    fails, verifies = [], []
    if "Checks" in wb.sheetnames:
        for r in wb["Checks"].iter_rows(min_row=5, values_only=True):
            if r[0] is not None and r[4] in ("FAIL", "VERIFY"):
                (fails if r[4] == "FAIL" else verifies).append({"no": r[0], "check": str(r[1])[:110], "value": r[2], "limit": r[3],
                                                                  "kind": (r[6] if len(r) > 6 else "")})
            if r[1] == "Calculation":
                res["overall"] = r[4]
    res["fail_checks"] = fails
    res["verify_checks"] = verifies
    ds, flags = Counter(), Counter()
    for name in ("README", "Basis of Design", "Sources"):
        if name in wb.sheetnames:
            for r in wb[name].iter_rows(values_only=True):
                for c in r:
                    if isinstance(c, str):
                        for m in re.findall(r"D-0\d\d", c):
                            ds[m] += 1
    if "Flags" in wb.sheetnames:
        for r in wb["Flags"].iter_rows(min_row=7, values_only=True):
            if r[0]:
                flags[r[1]] += 1
            for c in r:
                if isinstance(c, str):
                    for m in re.findall(r"D-0\d\d", c):
                        ds[m] += 1
    res["decisions_cited"] = sorted(ds)
    res["flags"] = dict(flags)
    return res


BAD_WORDS = re.compile(r"\b(claude|anthropic|openai|gpt|llm|opus|sonnet|haiku|fable|python|openpyxl|pymupdf|pdfplumber|"
                       r"markitdown|libreoffice|script|pipeline|council|gold set|round [0-9])\b", re.I)


def language_scan_xlsx(path: Path) -> List[str]:
    import openpyxl
    hits = []
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    for ws in wb:
        for r in ws.iter_rows():
            for c in r:
                v = c.value
                if isinstance(v, str) and len(v) > 3:
                    m = BAD_WORDS.search(v)
                    if m:
                        hits.append(f"{ws.title}!{getattr(c, 'coordinate', '?')}: {m.group(0)}")
                        if len(hits) >= 5:
                            return hits
    return hits


def pdf_checks() -> dict:
    out = {}
    p = ZON / "AIGH_IP_AHU_Zoning_LB3-L07.pdf"
    j = QA / "zoning_deliverable_checks.json"
    if j.exists():
        out["zoning_deliverable_checks"] = json.loads(j.read_text())
    if p.exists():
        import fitz
        d = fitz.open(p)
        meta = d.metadata
        out["pdf_pages"] = len(d)
        out["pdf_metadata"] = {k: meta.get(k) for k in ("title", "author", "creator", "producer", "subject", "keywords")}
        out["pdf_annotations"] = sum(len(list(pg.annots() or [])) for pg in d)
        hits = []
        for pg in d:
            t = pg.get_text()
            m = BAD_WORDS.search(t)
            if m:
                hits.append(f"page {pg.number + 1}: {m.group(0)}")
        out["pdf_language_hits"] = hits[:5]
    return out


# ---------------------------------------------------------------- gate
def coverage_unit_register() -> dict:
    p = REG / "room_unit_links.csv"
    if not p.exists():
        return {}
    rows = list(csv.DictReader(p.open()))
    by = Counter()
    for r in rows:
        by[(r["dept"], "unit" if r["unit_class"] and r["unit_kind"] not in ("", "unknown") else "unknown")] += 1
    return {"rows": len(rows), "kinds": dict(Counter(r["unit_kind"] for r in rows)),
            "basis": dict(Counter(r["basis"] for r in rows))}


def recompute_summary() -> dict:
    p = QA / "recompute_comparison.csv"
    if not p.exists():
        return {"status": "NOT RUN"}
    rows = list(csv.DictReader(p.open()))
    keys = list(rows[0])
    bad = [r for r in rows if str(r.get("status", r.get("agree", ""))).upper() in ("DIFF", "DIFFER", "NO", "FAIL", "FALSE", "MISMATCH")]
    return {"lines": len(rows), "columns": keys, "mismatch": len(bad)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trace-only", action="store_true")
    ap.add_argument("--tests", default=None, help="text file with the last full pytest summary line(s)")
    ap.add_argument("--selftest", default=None, help="text file with the last selftest result")
    a = ap.parse_args(argv)
    trace = build_trace()
    write_trace(trace)
    tc = Counter(r["gate"] for r in trace)
    print(f"decision trace: {len(trace)} decided D-ids, gate {dict(tc)}")
    if a.trace_only:
        return 1 if tc.get("FAIL") else 0
    gate = OrderedDict()
    gate["trace"] = {"rows": len(trace), "by_gate": dict(tc), "by_status": dict(Counter(r["trace_status"] for r in trace))}
    gate["tests"] = Path(a.tests).read_text().strip() if a.tests and Path(a.tests).exists() else "NOT RECORDED"
    gate["selftest"] = Path(a.selftest).read_text().strip() if a.selftest and Path(a.selftest).exists() else "NOT RECORDED"
    gate["recompute"] = recompute_summary()
    gate["unit_register"] = coverage_unit_register()
    gate["zoning"] = pdf_checks()
    deliv = []
    for p in sorted(WB.glob("AIGH_C2_D*_RevA.xlsx")):
        r = scan_workbook(p)
        r["language_hits"] = language_scan_xlsx(p)
        deliv.append(r)
    m = WB / "AHU_master_summary.xlsx"
    master = scan_workbook(m) if m.exists() else {}
    if master:
        master["language_hits"] = language_scan_xlsx(m)
    gate["workbooks"] = deliv
    gate["master"] = master
    gate["not_built"] = [
        "seeded-error test of 22 altered values (D-069 item 2)",
        "input audit by a reader outside the workbook code, stratified sample (D-069 item 3)",
        "traceability walk for 10 units (D-069 item 4)",
        "run_manifest.json with hashes and output-older-than-input check (D-068 item 4)",
        "STE lint script on the deliverable text (only the keyword scan of this gate was run)",
        "model_allocation.md (D-069 item 7)",
        "conflict_rulings.csv written by script (D-070)",
    ]
    (QA / "readiness_gate.json").write_text(json.dumps(gate, indent=1, default=str))
    write_md(gate, trace)
    nfail = sum(len(w["fail_checks"]) for w in deliv) + tc.get("FAIL", 0)
    print(f"gate written: {len(deliv)} workbooks, {nfail} FAIL lines (workbook checks + trace). See out/qa/readiness_gate.md")
    return 0


def write_md(g, trace):
    L = ["# Readiness gate (D-069), scripted", "",
         "Generated by `python -m src.gate`. Draft for human check (D-071). A line marked NOT BUILT is not a pass.", ""]
    L += ["## Gate lines", "",
          f"- Decision trace: {g['trace']['rows']} decided D-ids; status {g['trace']['by_status']}; gate {g['trace']['by_gate']} (out/qa/decision_trace.csv).",
          f"- Full pytest: {g['tests']}", f"- Workbook selftest: {g['selftest']}",
          f"- Recomputation comparison (out/qa/recompute_comparison.csv): {g['recompute']}",
          f"- Room to unit register: {g['unit_register'].get('rows')} rows; kinds {g['unit_register'].get('kinds')}", ""]
    z = g["zoning"]
    L += ["- Zoning PDF: " + json.dumps({k: v for k, v in z.items() if k != "zoning_deliverable_checks"}, default=str),
          "- Zoning deliverable checks: " + json.dumps(z.get("zoning_deliverable_checks", {}), default=str)[:600], ""]
    L += ["## Per deliverable", "",
          "| Workbook | Rooms | Unit or class | Unit tag | Criteria | Area | Height | Occupancy | Loads | FAIL checks | VERIFY checks | Overall | Language hits |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for w in g["workbooks"]:
        L.append(f"| {w['file'].replace('AIGH_C2_', '').replace('_Thermal_Load_Calc_RevA.xlsx', '')} | {w['rooms']} | {w.get('with_unit_or_class')} | "
                 f"{w.get('with_unit_tag')} | {w.get('with_criteria')} | {w.get('with_area')} | {w.get('with_height')} | "
                 f"{w.get('with_occupancy')} | {w.get('with_loads')} | {len(w['fail_checks'])} | {len(w['verify_checks'])} | "
                 f"{w.get('overall')} | {len(w['language_hits'])} |")
    if g["master"]:
        mm = g["master"]
        L.append(f"| AHU master summary | - | - | - | - | - | - | - | - | {len(mm['fail_checks'])} | {len(mm['verify_checks'])} | {mm.get('overall')} | {len(mm['language_hits'])} |")
    L += ["", "## Decisions cited and FAIL checks by deliverable", ""]
    for w in g["workbooks"]:
        L.append(f"### {w['file']}")
        L.append(f"- Decisions cited in README, Basis of Design, Sources, Flags: {', '.join(w['decisions_cited']) or 'none'}")
        L.append(f"- Flags: {w['flags']}")
        for f in w["fail_checks"]:
            L.append(f"- FAIL {f['no']} ({f['kind']}): {f['check']} = {f['value']} (limit {f['limit']})")
        for h in w["language_hits"]:
            L.append(f"- LANGUAGE {h}")
        pr = w["properties"]
        L.append(f"- Properties: creator {pr['creator']!r}, last modified by {pr['last_modified_by']!r}, title {pr['title']!r}")
        L.append("")
    L += ["## Decision trace gaps", ""]
    for r in trace:
        if r["gate"] != "PASS":
            L.append(f"- {r['d_id']} {r['trace_status']} ({r['gate']}): module '{r['reading_module']}', test '{r['test']}'")
    L += ["", "## Not built (human check list, gate level)", ""] + [f"- {x}" for x in g["not_built"]]
    (QA / "readiness_gate.md").write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    sys.exit(main())
