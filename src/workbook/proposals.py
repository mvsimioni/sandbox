"""zoning_change_proposals.csv: every room the outdoor-air rule separates or lists (brief 8.2, step 5).

Read from the recalculated workbook, so the figures are the workbook's own. Rows are keyed by department; building one
department replaces that department's rows and leaves the others alone.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List

import openpyxl

from .layouts import AHU_KEYS, FCU_KEYS

DEFAULT_PATH = Path("out/zoning/zoning_change_proposals.csv")
COLUMNS = ["department_no", "department", "room_no", "room_name", "storey", "ahu_before", "rule_outcome", "proposal",
           "room_supply_lps", "room_oa_lps", "room_oa_fraction", "ahu_oa_fraction_before", "ahu_oa_fraction_after",
           "ahu_supply_before_lps", "ahu_supply_after_lps", "ahu_oa_before_lps", "ahu_oa_after_lps",
           "coil_before_kw", "coil_after_kw", "fcu_coil_kw", "fcu_oa_source", "fcu_oa_source_chosen_by", "fcu_oa_arrives",
           "source_capacity", "flag_id", "approval", "remark"]


def _rows(ws, first: int = 7) -> List[tuple]:
    return [r for r in ws.iter_rows(min_row=first, values_only=True)]


def read_proposals(rc: Path) -> List[Dict[str, Any]]:
    """Proposal rows for the department in the recalculated workbook ``rc`` (empty when the rule triggers nothing)."""
    wb = openpyxl.load_workbook(rc, data_only=True)
    af = wb["Airflow"]
    hdr = {c.value: i for i, c in enumerate(af[5]) if c.value}
    dept_no, dept_name = "", ""
    title = wb["README"]["B1"].value or ""
    parts = str(title).split(None, 1)
    if parts:
        dept_no, dept_name = parts[0], (parts[1].strip() if len(parts) > 1 else "")
    rows7 = _rows(wb["AHU"])
    n = next((i for i, r in enumerate(rows7) if r[0] == "TOTAL"), 0)      # table A rows end at its TOTAL row
    before = {r[0]: dict(zip(AHU_KEYS, r)) for r in rows7[:n]}
    after = {r[0]: dict(zip(AHU_KEYS, r)) for r in rows7[n + 6:n + 6 + n]}   # table B starts six rows below table A's last row
    fcu = {}
    for r in _rows(wb["FCU"]):
        d = dict(zip(FCU_KEYS, r))
        if d.get("no"):
            fcu[d["no"]] = d
    flag_id = {}
    for r in _rows(wb["Flags"]):
        if r[2] and r[3] and str(r[3]).startswith("Outdoor-air rule:"):
            flag_id[r[2]] = r[0]
    out: List[Dict[str, Any]] = []
    for r in _rows(af):
        no = r[hdr["Room number"]]
        status = r[hdr["Outdoor-air rule"]]
        if not no or status not in ("SEPARATE", "LIST FOR MARCUS"):
            continue
        ahu = r[hdr["AHU or system"]]
        b, a, f = before.get(ahu, {}), after.get(ahu, {}), fcu.get(no, {})
        sep = status == "SEPARATE"
        src = f.get("oasrc")
        src_txt = "an outdoor-air source still to be chosen from the zoning" if str(src or "").strip().lower() in ("", "not set") else src
        out.append(dict(
            department_no=dept_no, department=dept_name, room_no=no, room_name=r[hdr["Room name"]], storey=r[hdr["Storey"]],
            ahu_before=ahu, rule_outcome=status,
            proposal=(f"dedicated FCU for the room load; outdoor air ducted from {src_txt}" if sep else
                      "not separated: criteria an FCU might not meet (air class, filtration, pressure control or fixed by specification)"),
            room_supply_lps=r[hdr["Final supply"]], room_oa_lps=r[hdr["Outdoor air"]], room_oa_fraction=r[hdr["Outdoor-air fraction"]],
            ahu_oa_fraction_before=b.get("frac"), ahu_oa_fraction_after=a.get("frac") if sep else b.get("frac"),
            ahu_supply_before_lps=b.get("sa_ahu"), ahu_supply_after_lps=a.get("sa_ahu") if sep else b.get("sa_ahu"),
            ahu_oa_before_lps=b.get("oa_rule"), ahu_oa_after_lps=a.get("oa_rule") if sep else b.get("oa_rule"),
            coil_before_kw=b.get("coil"), coil_after_kw=a.get("coil") if sep else b.get("coil"),
            fcu_coil_kw=f.get("coil") if sep else "", fcu_oa_source=src if sep else "",
            fcu_oa_source_chosen_by=f.get("oaby") if sep else "", fcu_oa_arrives=f.get("oacond") if sep else "",
            source_capacity=f.get("cap") if sep else "", flag_id=flag_id.get(no, ""), approval="PENDING Marcus",
            remark=(f.get("flag") or "") if sep else "listed for Marcus; not separated automatically"))
    return out


def update_csv(rc: Path, csv_path: Path = DEFAULT_PATH) -> List[Dict[str, Any]]:
    """Replace this department's rows in ``csv_path`` with the proposals read from ``rc``; returns the new rows."""
    new = read_proposals(rc)
    dept = new[0]["department_no"] if new else None
    if dept is None:
        wb = openpyxl.load_workbook(rc, data_only=True)
        dept = str(wb["README"]["B1"].value or "").split(None, 1)[0]
    keep: List[Dict[str, Any]] = []
    csv_path = Path(csv_path)
    if csv_path.exists():
        with csv_path.open(newline="", encoding="utf-8") as fh:
            keep = [r for r in csv.DictReader(fh) if r.get("department_no") != dept]
    rows = keep + new
    if not rows and not csv_path.exists():
        return new
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return new
