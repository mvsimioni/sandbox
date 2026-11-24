"""Facade exposure coverage per storey (D-102): the facade line of the storey, the part on conditioned zones (AHU / FCU) of the zoning, the
part on natural ventilation, MV, plant or unclassified spaces, and the part carried by the envelope rows of the workbooks.

``python3 -m src.workbook.facade_coverage``  -> out/qa/facade_coverage.csv
"""
from __future__ import annotations

import csv
import glob
import json
from collections import defaultdict
from pathlib import Path

from . import envelope as envm

ROOT = Path(".")


def run():
    env = envm.Envelope(ROOT)
    wb = defaultdict(float)
    for f in sorted(glob.glob("out/loads/inputs/D*.json")) + ["out/loads/inputs_common/D00.json"]:
        for r in json.load(open(f, encoding="utf-8"))["rooms"]:
            h = env.storey_height(r.get("storey"))
            for e in r.get("envelope") or []:
                if h:
                    wb[r["storey"]] += (float(e.get("glazing_m2") or 0) + float(e.get("wall_m2") or 0)) / h
    rows = []
    for st in envm.ENVELOPE_STOREYS:
        o = env._outline(st)
        if o is None:
            continue
        total = o[1].length / 1000.0
        with open(f"out/registers/zone_assignment_{st}.csv", newline="", encoding="utf-8") as fh:
            kind = {z["room_id"]: (z["class_kind"], z["class"]) for z in csv.DictReader(fh)}
        by = defaultdict(float)
        for rid, secs in env.plan_room_facade(st).items():
            k = kind.get(rid, ("no zoning row", ""))[0]
            grp = "conditioned (AHU/FCU/CRAC)" if k in ("AHU", "FCU", "CRAC") else ("AHU plant room (spill)" if k == "AHU spill" else k or "no information")
            by[grp] += sum(secs.values())
        alloc = sum(by.values())
        rows.append(dict(storey=st, facade_line_m=round(total, 1), on_conditioned_zones_m=round(by.get("conditioned (AHU/FCU/CRAC)", 0), 1),
                         on_NV_m=round(by.get("NV", 0), 1), on_MV_m=round(by.get("MV", 0), 1), on_plant_rooms_m=round(by.get("AHU plant room (spill)", 0), 1),
                         on_other_m=round(alloc - sum(by.get(k, 0) for k in ("conditioned (AHU/FCU/CRAC)", "NV", "MV", "AHU plant room (spill)")), 1),
                         not_on_any_space_m=round(total - alloc, 1), in_workbook_envelope_m=round(wb.get(st, 0), 1)))
    Path("out/qa").mkdir(parents=True, exist_ok=True)
    with open("out/qa/facade_coverage.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return rows


if __name__ == "__main__":
    for r in run():
        print(r)
