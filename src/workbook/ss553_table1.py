"""SS 553:2026 Table 1 outdoor-air rates on the SS 553 rooms (D-111, 26 Nov 2026).

Scope (D-110): a room is SS 553 when its criteria row gives outdoor air per person or per m², or the HFDS row refers to SS 553. Each such
room gets one of:
  - a Table 1 occupancy type: minimum outdoor air = Table 1 rate (L/s per m²) x floor area (rules N1, N2);
  - MEETING (62.1, rule N3): Vbz = 2.5 L/s per person x RDS occupancy + 0.3 L/s per m² x area (MS rules, section 4, N3);
  - STORE OR RISER (cl 5.3.5): 0 L/s below 6 m² (exempt; the adjacent space covers it), otherwise the booklet rate;
  - COLD ROOM (cl 5.3.5): storage-only cold room, exempt (0 L/s);
  - QP RATE (cl 5.3.6): space not in Table 1; the booklet / project design-basis rate stays (Marcus, 26 Nov 2026).
Decisions of Marcus, 26 Nov 2026: corridor bays take Lobbies and corridors; unlisted spaces keep the booklet rate; cl 5.3.5 exemption applied.
The type is chosen from the room name; out/qa/ss553_table1_rooms.csv lists every room for review.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SRC_T1 = dict(id="SS553-T1", document="SS 553:2026 Code of practice for air-conditioning and mechanical ventilation in buildings",
              issue="2026 (pages sent by the engineer on 26 Nov 2026)", page="Table 1, cl 5.3.3.4, 5.3.5, 5.3.6 (printed pages 15 to 17)",
              location="Table 1 - Outdoor air supply requirements by occupancy type, column 'Minimum outdoor air supply, l/s per m² floor area'",
              text_as_read="Concourses 1.1; Offices 0.6; Lobbies and corridors 0.3; Shops 1.1; Retail general clinics 1.4; Retail dental clinics 1.9; "
                           "Classrooms 2.8; Restaurants 3.4; Theatre/cinema seating area 2.0; Gymnasiums 3.8 (l/s per m²). cl 5.3.5: storage spaces and "
                           "risers less than 6 m², cold rooms used for storage only can be exempt. cl 5.3.6: undefined spaces by the QP, 62.1 for reference.",
              method="Read from the pages of the standard", confidence="high")
SRC_MS = dict(id="OA-RULES-MS", document="AH minimum OA calculation rules for multispace AHUs (MS, 26.11.2026), internal",
              issue="26 Nov 2026", page="section 4, rule N3", location="meeting room: 62.1 Table 6-1 with RDS occupancy",
              text_as_read="Vbz = Rp x Pz + Ra x Az (meeting room 2.5 L/s per person + 0.3 L/s per m2)",
              method="Rule set by the engineer", confidence="high")

TABLE1: List[Tuple[str, str, float]] = [          # (regex on the room name, Table 1 type, L/s per m²)
    (r"lecture theatre|auditorium", "Theatre/cinema seating area", 2.0),
    (r"training room|lecture room|computer lab|classroom|seminar|tutorial", "Classrooms", 2.8),
    (r"\bcaf[eé]\b|cafeteria|dining|restaurant|canteen|food court", "Restaurants", 3.4),
    (r"concourse", "Concourses", 1.1),
    (r"\bshop\b|retail|kiosk", "Shops, supermarkets and department stores", 1.1),
    (r"\boffice\b|workstation|open workspace", "Offices", 0.6),
    (r"corridor|lobby|\bbay\b|trolley exchange|\bagv\b|amr park|c-arm parking", "Lobbies and corridors", 0.3),
]
MEETING = re.compile(r"meeting|conference|huddle|board ?room|discussion|interview", re.I)
STORE = re.compile(r"\bstore\b|storage|storeroom|\briser\b|\bshaft\b", re.I)
COLD = re.compile(r"cold room", re.I)


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def in_scope(c: Dict[str, Any]) -> bool:
    """SS 553 scope of D-110 (same test as the Criteria formula)."""
    pp, pa = _num(c.get("oa_lps_per_person")), _num(c.get("oa_lps_per_m2"))
    if pp + pa + _num(c.get("min_oa_ach")) + _num(c.get("oa_specified_lps")) == 0:
        return False
    return bool(c.get("oa_ss553")) or pp > 0 or pa > 0


def classify(name: str) -> Dict[str, Any]:
    n = name or ""
    if COLD.search(n):
        return dict(kind="COLD ROOM (cl 5.3.5)", lpm2=None, lpp=None, src=SRC_T1["id"])
    if STORE.search(n):
        return dict(kind="STORE OR RISER (cl 5.3.5)", lpm2=None, lpp=None, src=SRC_T1["id"])
    if MEETING.search(n):
        return dict(kind="MEETING (62.1, rule N3)", lpm2=0.3, lpp=2.5, src=SRC_MS["id"])
    for rx, t, v in TABLE1:
        if re.search(rx, n, re.I):
            return dict(kind=t, lpm2=v, lpp=None, src=SRC_T1["id"])
    return dict(kind="QP RATE (cl 5.3.6)", lpm2=None, lpp=None, src=SRC_T1["id"])


def apply(datasets: Dict[str, Dict[str, Any]], qa: Optional[Path] = Path("out/qa/ss553_table1_rooms.csv")) -> List[Dict[str, Any]]:
    log = []
    for n, d in datasets.items():
        used = set()
        for r in d["rooms"]:
            c = r.get("criteria") or {}
            if not in_scope(c):
                continue
            t = classify(r.get("name") or "")
            c["ss553_t1"] = t
            used.add(t["src"])
            log.append(dict(workbook=f"D{n}", room=r["number"], name=r.get("name"), criteria_row=c.get("criteria_row"),
                            type=t["kind"], lps_per_m2=t["lpm2"], lps_per_person=t["lpp"],
                            booklet_lps_per_person=c.get("oa_lps_per_person"), booklet_lps_per_m2=c.get("oa_lps_per_m2"),
                            booklet_oa_ach=c.get("min_oa_ach")))
        for s in (SRC_T1, SRC_MS):
            if s["id"] in used and not any(x["id"] == s["id"] for x in d["sources"]):
                d["sources"].append(dict(s))
    if qa and log:
        qa.parent.mkdir(parents=True, exist_ok=True)
        with qa.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(log[0]))
            w.writeheader()
            w.writerows(log)
    return log
