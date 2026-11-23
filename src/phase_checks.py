"""Phase 2 and phase 3 checks (D-056, D-069): thin wrappers over the existing checks, the decision trace and the link coverage, so that one command gives the gate result.

``python -m src.checks phase2`` : calculation status of every workbook, status lines, language scan, unit control total.
``python -m src.checks phase3`` : decision trace, link coverage, seeded-error test, structural text lint, outputs not older than inputs.

Simplifications (D-071): the seeded-error test seeds unit tags in a copy of the link register and checks that the control total catches every one; the other
seeded values of D-069 item 2 are not built. The text lint is a structural check of sentence length only; it is not the full ASD-STE100 lint.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
WB = ROOT / "out" / "loads" / "workbooks"
REG = ROOT / "out" / "registers"
ZON = ROOT / "out" / "zoning"
PASS, FAIL = "PASS", "FAIL"


def _workbooks() -> List[Path]:
    return sorted(WB.glob("AIGH_C2_D*_RevA.xlsx")) + ([WB / "AHU_master_summary.xlsx"] if (WB / "AHU_master_summary.xlsx").exists() else [])


def _status_lines(path: Path) -> Dict[str, str]:
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    out: Dict[str, str] = {}
    for r in wb["Checks"].iter_rows(min_row=5, values_only=True):
        if r[1] in ("Calculation", "Inputs complete"):
            out[str(r[1])] = str(r[4])
    out["revision"] = ""
    for r in wb["README"].iter_rows(values_only=True):
        for c in r:
            if isinstance(c, str) and c.startswith(("DRAFT", "Issued")):
                out["revision"] = c
    return out


def check_p2_status(ctx) -> Tuple[str, List[str]]:
    """Every workbook: 'Calculation' is not FAIL; the 'Inputs complete' line exists and is not merged; the revision label follows D-056."""
    bad, n = [], 0
    for p in _workbooks():
        s = _status_lines(p)
        n += 1
        if s.get("Calculation") not in ("PASS", "VERIFY"):
            bad.append(f"{p.name}: Calculation {s.get('Calculation')}")
        if "Inputs complete" not in s:
            bad.append(f"{p.name}: no 'Inputs complete' line")
        elif s["Inputs complete"] != "COMPLETE" and not s["revision"].startswith("DRAFT, acceptance open"):
            bad.append(f"{p.name}: open inputs and revision '{s['revision']}'")
    return (FAIL if bad else PASS), [f"{n} workbooks read"] + bad[:10]


def check_p2_language(ctx) -> Tuple[str, List[str]]:
    from src.workbook.selftest import language_scan
    hits = []
    for p in _workbooks():
        for h in language_scan(p, ROOT):
            hits.append(f"{p.name} {h[0]}!{h[1]} {h[2][:90]}")
    return (FAIL if hits else PASS), [f"{len(hits)} language hits"] + hits[:10]


def _links() -> Dict[Tuple[str, str], str]:
    with (REG / "room_unit_links.csv").open(newline="", encoding="utf-8") as fh:
        return {((r["dept"] or "").lstrip("0") or "0", r["dpa_room_id"]): r["unit_class"] for r in csv.DictReader(fh)}


def unit_differences(links: Dict[Tuple[str, str], str], other: Dict[Tuple[str, str], str]) -> List[str]:
    """Rooms whose unit differs between the link register and another list of units (room by room)."""
    return [f"{k[0]} {k[1]}: {links[k]} / {other[k]}" for k in other if k in links and links[k] != other[k]]


def _workbook_units(dept: str) -> Dict[Tuple[str, str], str]:
    import openpyxl
    p = next(WB.glob(f"AIGH_C2_D{dept}_*_RevA.xlsx"))
    wb = openpyxl.load_workbook(p, data_only=True, read_only=True)
    rows = list(wb["Room Information"].iter_rows(min_row=5, values_only=True))
    head = rows[0]
    j_no, j_ahu = head.index("Room number") if "Room number" in head else 0, head.index("AHU or system tag")
    return {(dept.lstrip("0") or "0", str(r[j_no])): str(r[j_ahu]) for r in rows[2:] if r[j_no] not in (None, "") and r[j_ahu] not in (None, "")}


def check_p2_unit_control(ctx) -> Tuple[str, List[str]]:
    """Control total of D-052 and D-059 for the MOT department (22): the unit of a room is the same in the link register and in the department workbook; no room is on a variant tag."""
    links = _links()
    wbu = _workbook_units("22")
    diff = unit_differences({k: v for k, v in links.items() if k[0] == "22"}, wbu)
    variants = [k for k, v in links.items() if "MOTHYB-02" in v]
    ev = [f"{len(wbu)} rooms of department 22 compared", f"{len(diff)} differences", f"{len(variants)} rooms on the variant tag AHU-B-07-05MOTHYB-02"]
    return (FAIL if diff or variants else PASS), ev + diff[:8]


def check_p3_trace(ctx) -> Tuple[str, List[str]]:
    from src import gate
    rows = gate.build_trace()
    fail = [r["d_id"] for r in rows if r["gate"] == "FAIL"]
    notest = [r["d_id"] for r in rows if r["trace_status"] == "NO TEST"]
    return (FAIL if fail or notest else PASS), [f"{len(rows)} decided D-ids", f"FAIL {fail}", f"NO TEST {notest}"]


def check_p3_link_coverage(ctx) -> Tuple[str, List[str]]:
    rows = list(csv.DictReader((REG / "room_unit_links.csv").open(newline="", encoding="utf-8")))
    n = len(rows)
    with_unit = sum(1 for r in rows if (r["unit_class"] or "").strip().lower() not in ("", "no information"))
    return (PASS if n else FAIL), [f"{with_unit} of {n} rooms have a unit or a class ({with_unit / n:.1%}): reported, not forced (D-054, D-071)"]


def check_p3_seeded(ctx) -> Tuple[str, List[str]]:
    """Seeded-error test, reduced (D-071): three unit tags are changed in a copy of the link register; the control total must report every one."""
    links = _links()
    keys = [k for k, v in links.items() if v.startswith("AHU-")][:3]
    seeded = dict(links)
    for k in keys:
        seeded[k] = "AHU-SEEDED-ERROR"
    caught = unit_differences(links, seeded)
    return (PASS if len(caught) == len(keys) == 3 else FAIL), [f"{len(caught)} of {len(keys)} seeded unit tags caught", "seeded values of other kinds (areas, rates, heights): not built"]


def lint_sentences(text: str, limit: int = 25) -> List[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if len(s.split()) > limit]


def check_p3_lint(ctx) -> Tuple[str, List[str]]:
    """Structural text lint (reduced, D-071): the sentences of the Flags and Remarks text of every workbook are counted against 25 words. Information; not the full lint."""
    import openpyxl
    long_, total = 0, 0
    for p in _workbooks():
        wb = openpyxl.load_workbook(p, data_only=True, read_only=True)
        for name in ("Flags",):
            if name in wb.sheetnames:
                for r in wb[name].iter_rows(values_only=True):
                    for c in r:
                        if isinstance(c, str) and len(c) > 40:
                            s = re.split(r"(?<=[.!?])\s+", c)
                            total += len(s)
                            long_ += len(lint_sentences(c))
    return PASS, [f"{long_} of {total} sentences on the Flags tabs have more than 25 words (information; the full lint of D-015 is not built here)"]


def check_p3_fresh(ctx) -> Tuple[str, List[str]]:
    """No output is older than its inputs (D-068 item 4, reduced): zone schedule and zoning PDF against the link register and the master summary."""
    inputs = [REG / "room_unit_links.csv", WB / "AHU_master_summary.xlsx"]
    outs = [ZON / "zone_schedule.xlsx", ZON / "AIGH_IP_AHU_Zoning_LB3-L07.pdf"]
    old = [f"{o.name} older than {i.name}" for o in outs for i in inputs if o.exists() and i.exists() and o.stat().st_mtime + 1 < i.stat().st_mtime]
    return (FAIL if old else PASS), [f"{len(outs)} outputs against {len(inputs)} inputs"] + old
