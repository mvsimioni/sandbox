"""Room coverage check: every plan room that the zoning puts on a unit must be represented by a room of a workbook.

A plan room is represented when (1) a workbook room cites it (plan room id in its provenance, remark or source), or (2) its label point lies inside an
App D outline that is a room of D00 (or a room moved from D00), or (3) a workbook room on the same storey and the same unit (or a unit group that
contains it) has a matching name (abbreviations expanded; score 0.6 or more), one workbook room for one plan room. The rest is listed.

``python3 -m src.workbook.room_coverage`` writes out/qa/room_coverage.csv and prints a summary.
"""

from __future__ import annotations

import csv
import glob
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from .assemble import _expansions, name_score

UNIT = re.compile(r"(?:P?AHU|FCU)-[A-Z0-9][\w/-]*")
IDRE = re.compile(r"\b((?:LB3|B[123]|L0\d))-R(\d{3}(?:-T\d\d)?)\b")


def _load_files(root: Path) -> dict:
    files = sorted(glob.glob(str(root / "out/loads/inputs/D*.json"))) + [str(root / "out/loads/inputs_common/D00.json")]
    return {re.search(r"D(\d+)\.json$", f).group(1): json.load(open(f, encoding="utf-8")) for f in files}


def cited_ids(room: dict, src: dict) -> set:
    ids = set()

    def walk(o):
        if isinstance(o, str):
            s = src.get(o)
            txt = o + (" " + s.get("location", "") + " " + s.get("text_as_read", "") if s else "")
            for m in IDRE.findall(txt):
                ids.add(f"{m[0]}-R{m[1]}")
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(room.get("prov"))
    walk(room.get("remark"))
    walk(room.get("gaps"))
    if room.get("plan_room_id"):
        ids.add(room["plan_room_id"])
    return ids


def run(root: Path = Path("."), datasets: dict = None, write: bool = True) -> list:
    from shapely import wkt
    from shapely.geometry import Point
    abbr = _expansions(root)
    datasets = datasets if datasets is not None else _load_files(root)
    cited, appd_used = set(), set()
    wb_rooms = defaultdict(list)                     # (storey, unit) -> [(workbook, number, name)]
    for num, d in datasets.items():
        wbk = "D" + num
        src = {s["id"]: s for s in d["sources"]}
        for r in d["rooms"]:
            ids = cited_ids(r, src)
            cited |= ids
            m = re.match(r"CA-(\w+)-(\d+)$", r["number"])
            if m:
                appd_used.add((m.group(1), str(int(m.group(2)))))
            for u in (UNIT.findall(r.get("ahu") or "") or [None]):
                wb_rooms[(r["storey"], u)].append((wbk, r["number"], r.get("name") or "", bool(ids) or bool(m)))
    outl = defaultdict(list)
    with open(root / "out/registers/appd_outlines_mm.csv", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if (r["storey"], r["annotation_index"]) in appd_used:
                outl[r["storey"]].append(wkt.loads(r["polygon_building_mm_wkt"]))
    rows = []
    for p in sorted(glob.glob(str(root / "out/registers/zone_assignment_*.csv"))):
        st = re.search(r"zone_assignment_(\w+)\.csv$", p).group(1)
        pp = root / f"out/registers/room_polygons_{st}.csv"
        poly = {r["room_id"]: r for r in csv.DictReader(open(pp, encoding="utf-8"))} if pp.exists() else {}
        todo = defaultdict(list)
        for z in csv.DictReader(open(p, encoding="utf-8")):
            u = (z.get("class") or "").strip()
            if not UNIT.match(u):
                continue
            rid, how = z["room_id"], ""
            if rid in cited:
                how = "cited by a workbook room"
            else:
                pr = poly.get(rid) or {}
                if pr.get("label_point_x_mm") and any(g.contains(Point(float(pr["label_point_x_mm"]), float(pr["label_point_y_mm"]))) for g in outl[st]):
                    how = "inside an App D space of a workbook"
            rows.append(dict(storey=st, plan_room=rid, plan_name=z.get("room_name", ""), area_m2=z.get("area_m2", ""), unit=u, department=z.get("department", ""), represented_by=how))
            if not how:
                todo[u].append(rows[-1])
        # name matching on the same unit and storey, one workbook room for one plan room (rooms without a plan link first)
        for u, plans in todo.items():
            pool = [w for w in wb_rooms.get((st, u), [])]
            pool.sort(key=lambda w: w[3])
            used = set()
            for pr in sorted(plans, key=lambda x: x["plan_name"]):
                best, bs = None, 0.6
                for w in pool:
                    if w[1] in used:
                        continue
                    s = name_score(w[2], pr["plan_name"], abbr)
                    if s >= bs:
                        best, bs = w, s
                if best:
                    used.add(best[1])
                    pr["represented_by"] = f"name match: {best[0]} {best[1]} '{best[2]}' ({bs:.2f})"
    if not write:
        return rows
    out = root / "out/qa/room_coverage.csv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return rows


if __name__ == "__main__":
    rows = run()
    miss = [r for r in rows if not r["represented_by"]]
    by = Counter(r["represented_by"].split(":")[0] for r in rows)
    print("zoned plan rooms on a unit:", len(rows), dict(by))
    print("not represented:", len(miss), "area m2: %.0f of %.0f" % (sum(float(r["area_m2"] or 0) for r in miss), sum(float(r["area_m2"] or 0) for r in rows)))
    for u, c in Counter(r["unit"] for r in miss).most_common(15):
        print(" ", u, c)
    print("STAFF-01:", [(r["plan_room"], r["plan_name"], r["area_m2"]) for r in miss if r["unit"] == "AHU-B-06-06STAFF-01"])
