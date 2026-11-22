"""DPA Revit room database (``DPA_SOA.xlsx``, sheet ``Database``) to ``out/registers/dpa_rooms.csv``.

``python3 -m src.dpa_soa run``

One row per database row, values as printed plus normalised fields.  The storey is taken from the Unique Room
Identifier only where the identifier carries one (``LO SFLABM.LB3.04`` -> ``LB3``); departmental identifiers
(``IWPR2 05.02.01``) carry group numbers, not storeys, and are left blank for the storey.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import common

SOURCE = "data/source/Basis/DPA SOA (uploaded)/DPA_SOA.xlsx"
SHEET = "Database"
STOREY_RE = re.compile(r"\.(LB3|B3|B2|B1|\d{1,2}M?|RF|RFDK|UMD|URF|LMR)\.[0-9A-Z]+$")
# Overarching groups whose identifiers are storey-based (lift lobbies, risers, plant: ``LL FLLIP.05.06``).  In
# departmental identifiers the middle number is a sub-group (``DSOT CI.01.01`` lies on the 6th storey), so it is kept
# only as a candidate there.
STOREY_GROUPS = {"Mechanical & Electrical", "Interdepartmental Circulation"}
COLUMNS = ["row", "revit_sync_date", "kpu", "kpu_norm", "overarching_group", "fpu_department_no", "fpu_department_name",
           "fpu_group_no", "fpu_group_name", "room_id", "unique_room_identifier", "storey_from_identifier", "storey_candidate_from_identifier",
           "soa_std_room_code", "room_name", "designed_area_m2", "programmed_area_m2", "parent_child", "parent_group_name",
           "room_type", "sd_revision_comment", "dd_revision_comment", "mp_revision_note", "source_file", "method"]


def norm_kpu(v: str) -> str:
    """``KPU 10``, ``KPU10 ``, ``10`` -> ``KPU10``; suffixes such as ``- NECE`` kept after a space."""
    s = re.sub(r"\s+", " ", (v or "").strip())
    m = re.match(r"^(?:KPU\s*)?(\d{1,2})\s*(?:-\s*(.+))?$", s, re.I)
    if not m:
        return s
    return "KPU%02d" % int(m.group(1)) + (" " + m.group(2).strip().upper() if m.group(2) else "")


def storey_of(identifier: str, group_no: str = "") -> str:
    """Storey from ``<dept> <group>.<storey>.<seq>``, only where the group code is not a number (a departmental
    identifier ``IWPR2 05.02.01`` has group and sub-group numbers there, not a storey)."""
    ident = (identifier or "").strip()
    head = ident.split(" ", 1)[-1].split(".", 1)[0]
    if not head or head.isdigit() or (group_no and head != group_no.strip()):
        return ""
    m = STOREY_RE.search(ident)
    if not m:
        return ""
    s = m.group(1)
    if re.fullmatch(r"\d{1,2}", s):
        return "L%02d" % int(s)
    if re.fullmatch(r"\d{1,2}M", s):
        return "L%02dM" % int(s[:-1])
    return s


def _num(v) -> str:
    if v is None or v == "":
        return ""
    try:
        return repr(float(v)).rstrip("0").rstrip(".") if float(v) != int(float(v)) else str(int(float(v)))
    except (TypeError, ValueError):
        return str(v)


def read_rows(path: Path) -> List[Dict[str, str]]:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[SHEET]
    it = ws.iter_rows(values_only=True)
    head = [str(h or "").strip() for h in next(it)]
    out: List[Dict[str, str]] = []
    for i, r in enumerate(it, start=2):
        if not any(c not in (None, "") for c in r):
            continue
        d = {h: ("" if v is None else str(v).strip() if not isinstance(v, (int, float)) else v) for h, v in zip(head, r)}
        uid = str(d.get("Unique Room Identifier", ""))
        cand = storey_of(uid, str(d.get("FPU Group (Number)", "")))
        out.append({
            "row": str(i), "revit_sync_date": str(d.get("Revit Synchronise Date", "")), "kpu": str(d.get("KPU", "")),
            "kpu_norm": norm_kpu(str(d.get("KPU", ""))), "overarching_group": str(d.get("Overarching Group (Name)", "")),
            "fpu_department_no": str(d.get("FPU Department (Number)", "")), "fpu_department_name": str(d.get("FPU Department (Name)", "")),
            "fpu_group_no": str(d.get("FPU Group (Number)", "")), "fpu_group_name": str(d.get("FPU Group (Name)", "")),
            "room_id": _num(d.get("Room ID")), "unique_room_identifier": uid,
            "storey_from_identifier": cand if str(d.get("Overarching Group (Name)", "")) in STOREY_GROUPS else "",
            "storey_candidate_from_identifier": "" if str(d.get("Overarching Group (Name)", "")) in STOREY_GROUPS else cand,
            "soa_std_room_code": str(d.get("SOA Std. Room Code", "")), "room_name": str(d.get("Room Name", "")),
            "designed_area_m2": _num(d.get("Designed Area")), "programmed_area_m2": _num(d.get("Programmed Area")),
            "parent_child": str(d.get("GL-Parent Child Rooms: Name", "")), "parent_group_name": str(d.get("Parent Group Name", "")),
            "room_type": str(d.get("GL-Room Type: Name", "")), "sd_revision_comment": str(d.get("SD Phase Revision Comments", "")),
            "dd_revision_comment": str(d.get("DD Phase Revision Comments", "")), "mp_revision_note": str(d.get("MP-Revision Note: Name", "")),
            "source_file": SOURCE, "method": "openpyxl cached values, sheet %s row %d" % (SHEET, i)})
    return out


def run(src: Optional[str] = None) -> Dict[str, object]:
    root = common.project_root()
    P = common.resolve_paths()
    rows = read_rows(root / (src or SOURCE))
    common.write_csv_dicts(P["registers"] / "dpa_rooms.csv", rows, COLUMNS)
    dup = Counter(r["unique_room_identifier"] for r in rows if r["unique_room_identifier"])
    summary = {"rows": len(rows), "departments": len({r["fpu_department_no"] for r in rows}),
               "with_storey": sum(1 for r in rows if r["storey_from_identifier"]),
               "duplicate_identifiers": sum(1 for k, n in dup.items() if n > 1),
               "zero_designed_area": sum(1 for r in rows if r["designed_area_m2"] in ("", "0"))}
    print(summary)
    return summary


def _write(path: Path, rows: Sequence[Dict[str, str]]) -> None:
    import csv
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    ap.parse_args(argv)
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
