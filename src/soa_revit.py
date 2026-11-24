"""SOA with Revit data (uploaded 24 Nov 2026): Revit level, Revit room number, Revit room name (the tag printed on the plan) and ACMV briefing fields
of every DPA unique room identifier. Written to out/registers/soa_revit.csv (D-094).

``python3 -m src.soa_revit`` (source: data/uploads/soa_20261124/SOA.xlsx, read only)
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

SRC = Path("data/uploads/soa_20261124/SOA.xlsx")
OUT = Path("out/registers/soa_revit.csv")
ORD = {"1ST": "01", "2ND": "02", "3RD": "03"}


def storey_code(level: str) -> str:
    """'6TH STOREY (IP)' -> 'L06'; 'BASEMENT 2 (IP)' -> 'B2'; 'LOWER BASEMENT 3 (IP)' -> 'LB3'; mezzanines and other blocks -> ''."""
    lv = (level or "").strip().upper()
    if not lv.endswith("(IP)") or " MZ " in lv or "+" in lv:
        return ""
    if lv.startswith("LOWER BASEMENT"):
        return "LB" + re.findall(r"\d+", lv)[0]
    if lv.startswith("BASEMENT"):
        return "B" + re.findall(r"\d+", lv)[0]
    m = re.match(r"(\d+)(ST|ND|RD|TH) STOREY \(IP\)$", lv)
    return f"L{int(m.group(1)):02d}" if m else ""


def build() -> int:
    import openpyxl
    wb = openpyxl.load_workbook(SRC, read_only=True, data_only=True)
    rows = list(wb.worksheets[0].iter_rows(values_only=True))
    head = [str(h).strip() for h in rows[0]]
    out = []
    for i, r in enumerate(rows[1:], start=2):
        d = dict(zip(head, r))
        rid = str(d.get("Unique Room Identifier") or "").strip()
        if not rid or rid == "..":
            continue
        out.append(dict(unique_room_identifier=rid, revit_level=str(d.get("Revit Level") or "").strip(), storey=storey_code(str(d.get("Revit Level") or "")),
                        revit_room_number=str(d.get("Room Number") or "").strip(), room_name=str(d.get("Room Name") or "").strip(),
                        revit_tag=str(d.get("Revit Room Name") or "").strip(), department=str(d.get("FPU Department (Name)") or "").strip(),
                        group=str(d.get("FPU Group (Name)") or "").strip(), acmv_hours=str(d.get("ACMV - ACMV Hours of Op.") or "").strip(),
                        acmv_temperature=str(d.get("ACMV - Room Temperature") or "").strip(), acmv_pressure=str(d.get("ACMV - Room Pressure") or "").strip(),
                        acmv_humidity=str(d.get("ACMV - Room Humidity") or "").strip(), acmv_recirculation=str(d.get("ACMV - Room Air Re-Circulation") or "").strip(),
                        acmv_control=str(d.get("ACMV - Mode of Control") or "").strip(), acmv_exhaust=str(d.get("ACMV - Exhaust Requirements") or "").strip(),
                        acmv_hepa=str(d.get("ACMV - HEPA Filtered") or "").strip(), source=f"{SRC.name}, sheet {wb.worksheets[0].title}, row {i}"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    return len(out)


if __name__ == "__main__":
    print(build(), "rows ->", OUT)
