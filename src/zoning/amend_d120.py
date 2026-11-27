"""Zoning amendments from the forensic envelope review (D-120, 26 Nov 2026).

Removes the stair outlines that were read from the detail insets beyond grid 23 of the storey sheets (each stair also exists at its real
place on the plan): L02-R197 and L02-R198 (S.04; real S.04 is L02-R171), L03-R163 (S.07; real L03-R038), L03-R186 (S.08; real L03-R141).
Relabels L02-R108 (tag S.33) as the lecture theatre stage, AC, on AHU-B-03-02AUDIT-08 (D-122). Backups go to out/work/<file>_before_D120.csv. Re-running is safe.

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


RELABEL = {"L02-R108": dict(room_name="Stage (Lecture Theatre TRED 09.03.05)", mode_label="AC", **{"class": "AHU-B-03-02AUDIT-08"}, class_kind="AHU",
                            notes="D-122 (Marcus, 26 Nov 2026): the outline tagged S.33 is the lecture theatre stage, AC, on the lecture theatre unit; "
                                  "the stair S.33 is a small internal stair. The curtain wall in front of the stage is not a window."),
           # D-127 (Marcus, 27 Nov 2026): the Equipment Store AMSC 10.19.03 (plan tag 'Str / AC (24HR)') is on AHU-B-06-06STAFF-01, as the tender
           # air-side schematic IP-BH-06068 shows; it replaces the MV mode of the part plans (D-107, D-108) for this room
           "L06-R205": dict(room_name="Store - Equipment (AMSC 10.19.03)", mode_label="AC (24HR)", **{"class": "AHU-B-06-06STAFF-01"}, class_kind="AHU",
                            ruling="D-127", notes="D-127 (Marcus, 27 Nov 2026): Store - Equipment AMSC 10.19.03 (Str, AC (24HR)) included in "
                                  "AHU-B-06-06STAFF-01, as on the tender air-side schematic IP-BH-06068 (STORE EQ 10.19.03).")}


def relabel():
    for st in sorted({k.split("-")[0] for k in RELABEL}):
        p = REG / f"zone_assignment_{st}.csv"
        with p.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        head = list(rows[0].keys())
        for r in rows:
            if r["room_id"] in RELABEL:
                for k, v in RELABEL[r["room_id"]].items():
                    if k in r:
                        r[k] = v
        with p.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=head)
            w.writeheader()
            w.writerows(rows)


# D-125 (Marcus, 27 Nov 2026): the lift shafts are not part of the AGV lift lobbies. The shaft stacks (AL.04-06 in L06-R086, AL.07-09 in
# L06-R188; shafts 2.11 x 1.76 m on a 2.94 m pitch, read on the 6th storey GA) are cut out of the lobby outlines, with the walls between them.
SHAFT_CUT = {"L06": {"L06-R086": (87000, 112810, 94530, 121560), "L06-R188": (87000, 78110, 94220, 86930)}}


def cut_shafts():
    import numpy as np
    from shapely import wkt
    from shapely.geometry import box
    out = []
    for st, cuts in SHAFT_CUT.items():
        p = REG / f"room_polygons_{st}.csv"
        bak = WORK / f"{p.stem}_before_D125.csv"
        if not bak.exists():
            shutil.copy(p, bak)
        with p.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        head = list(rows[0].keys())
        A, B = [], []
        for r in rows:
            if r.get("polygon_pv_wkt") and r.get("polygon_building_mm_wkt"):
                a, b = wkt.loads(r["polygon_building_mm_wkt"]).centroid, wkt.loads(r["polygon_pv_wkt"]).centroid
                A.append([a.x, a.y, 1.0]); B.append([b.x, b.y])
        M, *_ = np.linalg.lstsq(np.array(A), np.array(B), rcond=None)
        for r in rows:
            if r["room_id"] not in cuts:
                continue
            x0, y0, x1, y1 = cuts[r["room_id"]]
            mm = box(x0, y0, x1, y1)
            c = np.array([[x0, y0, 1], [x1, y0, 1], [x1, y1, 1], [x0, y1, 1]]) @ M
            pv = box(c[:, 0].min(), c[:, 1].min(), c[:, 0].max(), c[:, 1].max())
            for k, cut in (("polygon_building_mm_wkt", mm), ("zoning_building_mm_wkt", mm), ("polygon_pv_wkt", pv)):
                if r.get(k):
                    g = wkt.loads(r[k]).buffer(0)
                    if g.intersects(cut):
                        r[k] = g.difference(cut).buffer(0).wkt
            out.append((r["room_id"], round(wkt.loads(r["polygon_building_mm_wkt"]).area / 1e6, 1)))
        with p.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=head)
            w.writeheader()
            w.writerows(rows)
    return out


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
    relabel()
    print("lift shafts cut:", cut_shafts())
