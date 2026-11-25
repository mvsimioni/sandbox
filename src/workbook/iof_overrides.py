"""Intermittent occupancy factors set by the engineer (D-106): Marcus typed I.O.F. 0.4 (meeting rooms) and 0.7 (offices, workstations, open
workspace) on the Room Zones tabs of the D47 workbook that he uploaded on 25 Nov 2026 (data/uploads/d47_20261125/). The values are read from that
workbook (iof_d47_20261125.json, room number -> factor) and set on the rooms, code D, with that workbook as the source."""
import json
from pathlib import Path
from typing import Any, Dict, List

FILE = Path(__file__).with_name("iof_d47_20261125.json")
SRC = dict(id="IOF-D47-20261125", document="AIGH_C2_D47_Admin_Workspace_Thermal_Load_Calc_RevA.xlsx as returned by the engineer", issue="25 Nov 2026",
           page="Level 6 Room Zones; Level 7 Room Zones", location="column I.O.F.", text_as_read="0.4 (meeting rooms); 0.7 (offices, workstations, open workspace)",
           method="Value typed by the engineer on the workbook", confidence="high")


def apply(datasets: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    table = json.loads(FILE.read_text())
    log = []
    for n, d in datasets.items():
        hit = False
        for r in d["rooms"]:
            v = table.get(r["number"])
            if v is None:
                continue
            r["iof"] = v
            r.setdefault("prov", {})["iof"] = ["D", SRC["id"]]
            hit = True
            log.append(dict(workbook=f"D{n}", room=r["number"], iof=v))
        if hit and not any(s["id"] == SRC["id"] for s in d["sources"]):
            d["sources"].append(dict(SRC))
    return log
