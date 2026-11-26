"""Zoning amendments from the forensic envelope review (D-120, 26 Nov 2026).

Removes the stair outlines that were read from the detail insets beyond grid 23 of the storey sheets (each stair also exists at its real
place on the plan): L02-R197 and L02-R198 (S.04; real S.04 is L02-R171), L03-R163 (S.07; real L03-R038), L03-R186 (S.08; real L03-R141).
Backups go to out/work/<file>_before_D120.csv. Re-running is safe.

``python3 -m src.zoning.amend_d120``
"""
from __future__ import annotations

import csv
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
REG = ROOT / "out/registers"
WORK = ROOT / "out/work"
DROP = {"L02": {"L02-R197", "L02-R198"}, "L03": {"L03-R163", "L03-R186"}}
FILES = ["room_polygons_{st}.csv", "room_polygons_{st}_wkt.csv", "zone_assignment_{st}.csv", "zoning_evidence_{st}.csv",
         "zoning_evidence_rooms_{st}.csv"]
csv.field_size_limit(1 << 30)


def run():
    n = 0
    for st, ids in DROP.items():
        for f in FILES:
            p = REG / f.format(st=st)
            if not p.exists():
                continue
            bak = WORK / f"{p.stem}_before_D120.csv"
            if not bak.exists():
                shutil.copy(p, bak)
            with p.open(encoding="utf-8", newline="") as fh:
                rows = list(csv.reader(fh))
            head, body = rows[0], rows[1:]
            k = head.index("room_id") if "room_id" in head else 0
            keep = [r for r in body if not (len(r) > k and r[k] in ids)]
            n += len(body) - len(keep)
            with p.open("w", encoding="utf-8", newline="") as fh:
                csv.writer(fh).writerows([head] + keep)
    return n


if __name__ == "__main__":
    print(run(), "rows removed")
