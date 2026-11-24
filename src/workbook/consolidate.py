"""One workbook per AHU (D-089), then a full regeneration of the workbooks and the master summary.

Rule: when the rooms of one AHU (or PAHU) are in more than one workbook, every room of that unit moves to the department workbook that holds the most
rooms of it (D00, the common areas, keeps a unit only when no department workbook has a room on it). Ties go to the lower department number and are
logged. AHU-B-06-06STAFF-01 goes to D47, as instructed by the engineer on 24 Nov 2026.

A moved room keeps all its inputs (criteria, loads, envelope rows, provenance). The sources it cites, the flags on it, the space-type, activity and
load-default rows it uses and the fans it names are copied to the receiving workbook. The unit row of the receiving workbook then counts every room
of the unit (no rooms in other departments). The unit row and the fans that serve only that unit are removed from the giving workbook.

``python3 -m src.workbook.consolidate --issue out/issue/DRAFT_2026-11-24 [--jobs 3]``
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Tuple

IN_DIR = Path("out/loads/inputs")
CA_JSON = Path("out/loads/inputs_common/D00.json")
WB_DIR = Path("out/loads/workbooks")
REPORT = Path("out/qa/unit_consolidation.json")
STOREY_ORDER = ["LB3", "B3", "B2", "B1", "L01", "L02", "L03", "L04", "L05", "L06", "L07", "L08"]
UNIT_RE = re.compile(r"^P?AHU-")
FORCED_OWNER = {"AHU-B-06-06STAFF-01": "47"}        # engineer's instruction, 24 Nov 2026


def _assemble(num: str) -> Tuple[str, Dict[str, Any]]:
    if num == "00":
        from .common_areas import assemble_common_areas
        return num, assemble_common_areas(Path("."))
    from .assemble import assemble_department
    return num, assemble_department(num, None)


def owners(datasets: Dict[str, Dict[str, Any]]) -> Tuple[Dict[str, str], Dict[str, Counter], List[str]]:
    count: Dict[str, Counter] = defaultdict(Counter)
    for n, d in datasets.items():
        for r in d["rooms"]:
            t = r.get("ahu") or ""
            if UNIT_RE.match(t):
                count[t][n] += 1
    own, notes = {}, []
    for t, c in count.items():
        if len(c) < 2:
            continue
        if t in FORCED_OWNER:
            own[t] = FORCED_OWNER[t]
            continue
        dept = {k: v for k, v in c.items() if k != "00"}
        if not dept:
            continue
        top = max(dept.values())
        cands = sorted(k for k, v in dept.items() if v == top)
        own[t] = cands[0]
        if len(cands) > 1:
            notes.append(f"{t}: tie between {cands}; D{cands[0]} keeps the unit (lower number)")
    return own, count, notes


def _strings(obj: Any, out: set) -> None:
    if isinstance(obj, str):
        out.add(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _strings(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _strings(v, out)


def _label(num: str, d: Dict[str, Any]) -> str:
    return f"D{num} {d['department'].get('name', '')}".strip()


def consolidate(datasets: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    own, count, notes = owners(datasets)
    moved_log: List[Dict[str, Any]] = []
    for unit, dest in sorted(own.items()):
        D = datasets[dest]
        src_ids = {s["id"] for s in D["sources"]}
        names = {k: {e.get("name") for e in D.get(k) or []} for k in ("space_types", "activities", "space_type_defaults", "orientations")}
        fan_tags = {f["tag"] for f in D.get("fans") or []}
        numbers = {r["number"] for r in D["rooms"]}
        for n, S in datasets.items():
            if n == dest:
                continue
            take = [r for r in S["rooms"] if r.get("ahu") == unit]
            if not take:
                continue
            nums = {r["number"] for r in take}
            clash = nums & numbers
            if clash:
                raise SystemExit(f"{unit}: room numbers {sorted(clash)} exist in both D{n} and D{dest}")
            S["rooms"] = [r for r in S["rooms"] if r.get("ahu") != unit]
            for r in take:
                r = copy.deepcopy(r)
                r.pop("_plan_room", None)
                r["remark"] = (r.get("remark") or "").rstrip() + f" Room moved from workbook {_label(n, S)}: every room of {unit} is in this workbook (D-089)."
                D["rooms"].append(r)
            numbers |= nums
            # flags on the moved rooms
            fl = [f for f in S.get("flags") or [] if f.get("ref") in nums]
            S["flags"] = [f for f in S.get("flags") or [] if f.get("ref") not in nums]
            D.setdefault("flags", []).extend(copy.deepcopy(fl))
            # fans named by the moved rooms, or serving the unit
            fans = [f for f in S.get("fans") or [] if f["tag"] in {r.get("exhaust_fan") for r in take} or unit in (f.get("serves"), f.get("serves2"))]
            for f in fans:
                if f["tag"] not in fan_tags:
                    D.setdefault("fans", []).append(copy.deepcopy(f))
                    fan_tags.add(f["tag"])
            # reference rows by name
            for k in names:
                for e in S.get(k) or []:
                    if e.get("name") not in names[k]:
                        D.setdefault(k, []).append(copy.deepcopy(e))
                        names[k].add(e.get("name"))
            # sources cited by what moved
            cited: set = set()
            _strings(take, cited)
            _strings(fl, cited)
            _strings(fans, cited)
            for s in S["sources"]:
                if s["id"] in cited and s["id"] not in src_ids:
                    D["sources"].append(copy.deepcopy(s))
                    src_ids.add(s["id"])
            # storeys of the receiving workbook
            for r in take:
                st = r["storey"]
                if st not in D["storeys"]:
                    D["storeys"].append(st)
                    D["storeys"].sort(key=lambda x: STOREY_ORDER.index(x) if x in STOREY_ORDER else 99)
                    D.setdefault("storey_labels", {})[st] = (S.get("storey_labels") or {}).get(st, st)
            # unit row and fans left on the giving workbook
            if not any(r.get("ahu") == unit for r in S["rooms"]):
                S["ahus"] = [a for a in S.get("ahus") or [] if a.get("tag") != unit]
                served = {a.get("tag") for a in S.get("ahus") or []}
                keep_fans = []
                for f in S.get("fans") or []:
                    serves = {f.get("serves"), f.get("serves2")} - {None, f["tag"]}
                    named = any(r.get("exhaust_fan") == f["tag"] for r in S["rooms"])
                    if named or (serves & served) or not (serves & {unit}) and not (f.get("type") == "EXHAUST" and not named):
                        keep_fans.append(f)
                S["fans"] = keep_fans
            moved_log.append(dict(unit=unit, from_wb=f"D{n}", to_wb=f"D{dest}", rooms=sorted(nums)))
        # the unit row of the receiving workbook: no rooms of the unit in other departments now
        for a in D.get("ahus") or []:
            if a.get("tag") == unit:
                a["other_rooms"] = 0
                a["consolidated"] = True
                from .capture import copy_sources
                for S in datasets.values():
                    copy_sources(S, D, a)
        if not any(a.get("tag") == unit for a in D.get("ahus") or []):
            for n, S in datasets.items():
                hit = [a for a in S.get("ahus") or [] if a.get("tag") == unit]
                if hit:
                    a = copy.deepcopy(hit[0])
                    a["other_rooms"] = 0
                    D.setdefault("ahus", []).append(a)
                    from .capture import copy_sources
                    copy_sources(S, D, a)
                    break
            else:
                raise SystemExit(f"{unit}: no unit row in any workbook")
        D.setdefault("flags", []).append(dict(type="VERIFY", ref=unit, source="",
                                              text=f"Every room of {unit} is in this workbook (D-089): {sum(count[unit].values())} room(s), of which "
                                                   f"{sum(v for k, v in count[unit].items() if k != dest)} moved from other workbooks. Confirm the department that owns the unit."))
    return dict(owners=own, counts={t: dict(c) for t, c in count.items() if len(c) > 1}, notes=notes, moves=moved_log)


def _build(job: Tuple[str, str, str]) -> str:
    num, src_json, dest = job
    from .build import build_workbook
    from .recalc import recalc
    with tempfile.TemporaryDirectory(prefix=f"cons_{num}_") as tmp:
        built = Path(tmp) / Path(dest).name
        ctx = build_workbook(Path(src_json), built)
        rc = recalc(built, Path(tmp) / "rc")
        shutil.copy(rc, dest)
    return f"D{num}: {len(ctx.rooms)} rooms"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue", required=True)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--no-build", action="store_true")
    a = ap.parse_args(argv)
    nums = sorted(p.stem[1:] for p in IN_DIR.glob("D*.json")) + ["00"]
    with ProcessPoolExecutor(max_workers=a.jobs) as ex:
        datasets = dict(ex.map(_assemble, nums))
    print("assembled", len(datasets), flush=True)
    from .capture import capture, ensure_unit_rows
    cap = capture(datasets)
    cap["unit_rows_made"] = ensure_unit_rows(datasets)
    from .dpa_ids import rebase_ids
    ids = rebase_ids(datasets)
    from .services_rooms import services
    svc = services(datasets, ids.pop("removed_rooms"), Path("."))
    back = {a["space"] for a in svc["restored"]}
    ids["removed"] = [x for x in ids["removed"] if x["space"] not in back]
    _rw = Path("out/qa/spaces_without_brief_room.csv")
    if ids["removed"]:
        import csv as _csv
        with _rw.open("w", newline="", encoding="utf-8") as fh:
            _w = _csv.DictWriter(fh, fieldnames=list(ids["removed"][0]))
            _w.writeheader()
            _w.writerows(ids["removed"])
    print(f"services rooms (D-095): {svc['inventory']} in the SOA, {len(svc['restored'])} restored from drawing spaces, {len(svc['added'])} added with no outline, "
          f"{len(svc['spaces_left'])} services spaces with no DPA room", flush=True)
    cap["unit_rows_made"] += ensure_unit_rows(datasets)
    print(f"room IDs (D-093): {len(ids['became'])} space(s) became DPA rooms, {len(ids['merged'])} merged into brief rooms, "
          f"{len(ids['removed'])} taken out (no brief room)", flush=True)
    print(f"capture: {len(cap['linked'])} room(s) linked to a unit by zoning, {len(cap['added'])} room(s) added from the drawing", flush=True)
    rep = consolidate(datasets)
    rep["capture"] = cap
    rep["room_ids"] = ids
    rep["services"] = svc
    rep["zone_facade"] = share_zone_facade(datasets)
    from .room_coverage import run as coverage
    cov = coverage(Path("."), datasets, write=True)
    rep["coverage_not_represented"] = [r for r in cov if not r["represented_by"]]
    print(f"coverage: {len(cov)} zoned plan rooms, {len(rep['coverage_not_represented'])} not represented", flush=True)
    empty = [n for n, d in datasets.items() if not d["rooms"]]          # D-091: a workbook with no rooms left is not issued
    rep["not_issued"] = [f"D{n}" for n in empty]
    for n in empty:
        datasets.pop(n)
        nums.remove(n)
        for p in WB_DIR.glob(f"AIGH_C2_D{n}_*_RevA.xlsx"):
            ret = Path("out/work/retired"); ret.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), ret / p.name)
        (IN_DIR / f"D{n}.json").exists() and shutil.move(str(IN_DIR / f"D{n}.json"), Path("out/work/retired") / f"D{n}.json")
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(rep, indent=1), encoding="utf-8")
    print(f"units consolidated: {len(rep['owners'])}; moves: {len(rep['moves'])}; notes: {rep['notes']}; not issued: {rep['not_issued']}", flush=True)
    zf = rep["zone_facade"]
    print("zone facade: %d unit-storeys, zone %.0f m, carried %.0f m, shared %.0f m, unallocated %.0f m" % (
        len(zf), sum(sum(r["zone_m"].values()) for r in zf), sum(sum(r["carried_m"].values()) for r in zf),
        sum(r.get("shared_m", 0) for r in zf), sum(r.get("unallocated_m", 0) for r in zf)), flush=True)
    for n, d in datasets.items():
        p = CA_JSON if n == "00" else IN_DIR / f"D{n}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
    if a.no_build:
        return 0
    jobs = []
    for n in nums:
        hit = [p for p in WB_DIR.glob(f"AIGH_C2_D{n}_*_RevA.xlsx")]
        if len(hit) != 1:
            raise SystemExit(f"D{n}: expected one workbook in {WB_DIR}, found {len(hit)}")
        jobs.append((n, str(CA_JSON if n == "00" else IN_DIR / f"D{n}.json"), str(hit[0])))
    with ProcessPoolExecutor(max_workers=a.jobs) as ex:
        for msg in ex.map(_build, jobs):
            print(msg, flush=True)
    from .proposals import update_csv
    for _, _, dest in jobs:
        if not dest.endswith("D00_Common_Areas_Thermal_Load_Calc_RevA.xlsx"):
            update_csv(Path(dest))
    from .regenerate import master
    print(master(), flush=True)
    out = Path(a.issue) / "workbooks"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    for p in WB_DIR.glob("*.xlsx"):
        shutil.copy(p, out / p.name)
    print("issued to", out, flush=True)
    return 0



def _room_area(r: Dict[str, Any]) -> float:
    for k in ("dpa_area_m2", "soa_area_m2", "measured_area_m2"):
        v = r.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return 0.0


def share_zone_facade(datasets: Dict[str, Dict[str, Any]], root: Path = Path(".")) -> List[Dict[str, Any]]:
    """D-090: the facade of the plan zone of each unit and storey that the rooms with their own outline do not carry is split by area among the rooms of
    the unit with no envelope rows (each room at most 2 x square root of its area). Returns one report row per unit and storey."""
    import csv
    import math
    from . import envelope as envm
    env = envm.Envelope(root)
    zone_of: Dict[str, Dict[str, List[str]]] = {}
    for st in envm.ENVELOPE_STOREYS:
        p = root / "out/registers" / f"zone_assignment_{st}.csv"
        if not p.exists():
            continue
        z: Dict[str, List[str]] = defaultdict(list)
        with p.open(newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if (r.get("class") or "").strip():
                    z[r["class"].strip()].append(r["room_id"])
        zone_of[st] = z
    report = []
    for n, d in datasets.items():
        groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
        for r in d["rooms"]:
            if r.get("ahu") and r["storey"] in zone_of:
                groups[(r["ahu"], r["storey"])].append(r)
        for (tag, st), rooms in sorted(groups.items()):
            members = re.findall(r"(?:P?AHU|FCU)-[A-Z0-9][\w/-]*", tag) if tag.lower().startswith("ahu group") else [tag]
            pids = [pid for m in members for pid in (zone_of[st].get(m) or [])]
            if not pids:
                continue
            prf = env.plan_room_facade(st)
            zone: Dict[str, float] = defaultdict(float)
            for pid in pids:
                for k, v in (prf.get(pid) or {}).items():
                    zone[k] += v
            if not zone:
                continue
            h = env.storey_height(st) or envm.DEFAULT_STOREY_H
            carried: Dict[str, float] = defaultdict(float)
            for r in rooms:
                for e in r.get("envelope") or []:
                    carried[e["orientation"]] += (float(e["glazing_m2"]) + float(e["wall_m2"])) / h
            rest = {k: zone[k] - carried.get(k, 0.0) for k in zone if zone[k] - carried.get(k, 0.0) >= 0.5}
            takers = [r for r in rooms if not r.get("envelope") and not r.get("_geom") and _room_area(r) > 0]   # rooms with their own outline are not given a share
            row = dict(workbook=f"D{n}", unit=tag, storey=st, zone_m={k: round(v, 1) for k, v in zone.items()},
                       carried_m={k: round(v, 1) for k, v in carried.items()}, rest_m={k: round(v, 1) for k, v in rest.items()}, rooms_sharing=len(takers))
            if not rest or not takers:
                row["unallocated_m"] = round(sum(rest.values()), 1)
                report.append(row)
                continue
            tot_rest = sum(rest.values())
            tot_area = sum(_room_area(r) for r in takers)
            given = 0.0
            for r in takers:
                a = _room_area(r)
                share = tot_rest * a / tot_area
                cap = 2.0 * math.sqrt(a)
                f = min(1.0, cap / share) if share > 0 else 0.0
                lengths = {k: v * a / tot_area * f for k, v in rest.items()}
                erows = env.rows_from_lengths(st, r["number"], lengths, "ETTV-ENV")
                if not erows:
                    continue
                for e in erows:
                    e["remark"] = (f"Share of the facade of the zone of {tag} on storey {st}: the zone has {zone[e['orientation']]:.1f} m on the {e['orientation']} facade; "
                                   f"rooms with their own outline carry {carried.get(e['orientation'], 0.0):.1f} m; the remaining {rest[e['orientation']]:.1f} m is split by area "
                                   f"among the {len(takers)} room(s) of the unit with no plan outline ({lengths[e['orientation']]:.1f} m to this room"
                                   + (", limited to 2 x the square root of the area" if f < 1.0 else "") + "). Approximate: the room may be internal. Confirm (D-090).")
                r["envelope"] = erows
                given += sum(lengths.values())
            row["shared_m"] = round(given, 1)
            row["unallocated_m"] = round(tot_rest - given, 1)
            report.append(row)
    return report


if __name__ == "__main__":
    raise SystemExit(main())
