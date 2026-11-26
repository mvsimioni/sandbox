"""Corrections from the architectural drawings (D-107, D-108): room names, ventilation modes and room links of the AHU zoning.

D-108 (26 Nov 2026): the Mode of Ventilation plans AR-IP-MOV-xx-001 (working drawings, May 2026; src.arch.mov, out/registers/arch_mov_tags.csv)
are the latest source of the mode and govern. The part-plan tags (D-107, D-108) are used only for rooms that the MOV plans do not place.

Source: data/uploads/arch_20261126/IP_part (AR-IP-02-xx-nnn, 1:50). Each room tag (Revit room number, short name, area, mode) is placed on the
zoning background by src.arch.part_plans (out/registers/arch_part_tags.csv). A tag lies in the plan room (zoning outline) that contains it.

Zoning (``zoning``), per plan room:
* the tags inside are written to the zone assignment register (arch_numbers, arch_names, arch_modes); where every tag inside has the same
  short name, that name becomes room_name (the earlier name is kept in room_name_tender);
* a plan room on a unit whose tags are all mechanically or naturally ventilated (MV, NV) takes that mode as its class;
* a plan room with no unit (no information, MV, NV) whose tags are all air-conditioned takes the unit of the neighbouring room on a unit
  (AHU, FCU or CRAC; not a plant room taking spill air, not the
  duty/standby FCU of one services room) with the longest shared boundary (assumed, flagged);
* a class set by an explicit ruling of the engineer (D-029, D-036, D-037, D-038, D-095, D-100, D-101) or a cold room by a specialist is not
  changed; the disagreement is listed.
Every change and every disagreement is in out/qa/arch_zoning_corrections.csv.

Workbooks (``workbooks``): a workbook room whose SOA Revit room number is placed on the drawing is linked to the plan room that holds the tag
(this replaces the links by name of D-092 and D-103). It takes the unit of that plan room; a room drawn MV or NV takes that system type.
Listed in out/qa/arch_room_links.csv.
"""
from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List

from shapely import wkt
from shapely.geometry import Point
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parent.parent.parent
REG = ROOT / "out/registers"
QA = ROOT / "out/qa"
PROTECTED = ("D-029", "D-036", "D-037", "D-038", "D-095", "D-100", "D-101")
UNIT_KINDS = ("AHU", "FCU", "AHU spill", "CRAC")
TAGRE = re.compile(r"(?:P?AHU|FCU|CRAC|DXFCU)-[A-Z0-9][\w/-]*")
SRC_TEXT = ("Mode of Ventilation plans AR-IP-MOV-xx-001 (working drawings, May 2026; data/uploads/mov_20261126) and architectural part plans "
            "AR-IP-02 (1:50; data/uploads/arch_20261126/IP_part): room tag with Revit room number, short name, area and mode of ventilation")
csv.field_size_limit(1 << 30)


def _read(p: Path):
    with p.open(newline="", encoding="utf-8") as fh:
        r = csv.DictReader(fh)
        return list(r), list(r.fieldnames)


def _write(p: Path, rows, cols):
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def located_tags() -> Dict[str, List[Dict[str, Any]]]:
    """storey -> tags with the plan room that holds them (one row per number and name)."""
    # D-108: the Mode of Ventilation plans (May 2026) govern; a part-plan tag is used only for a room number the MOV plans do not place
    mov = [dict(x, source="MOV") for x in (_read(REG / "arch_mov_tags.csv")[0] if (REG / "arch_mov_tags.csv").exists() else [])]
    part = [dict(x, source="part plan") for x in _read(REG / "arch_part_tags.csv")[0]]
    in_mov = {x["number"] for x in mov if x["bg_x"]}
    seen, out = set(), defaultdict(list)
    for x in mov + [x for x in part if x["number"] not in in_mov]:
        if not x["bg_x"] or (x["number"], x["name"]) in seen:
            continue
        seen.add((x["number"], x["name"]))
        out[x["storey"]].append(x)
    for st, tags in out.items():
        polys = [p for p in _read(REG / f"room_polygons_{st}.csv")[0] if p["polygon_pv_wkt"]]
        G = [wkt.loads(p["polygon_pv_wkt"]) for p in polys]
        T = STRtree(G)
        for x in tags:
            pt = Point(float(x["bg_x"]), float(x["bg_y"]))
            hits = [i for i in T.query(pt) if G[i].contains(pt)]
            x["plan_room"] = polys[min(hits, key=lambda i: G[i].area)]["room_id"] if hits else ""
    return out


def _mode(m: str) -> str:
    return "AC" if m.startswith("AC") else m


def zoning() -> List[Dict[str, Any]]:
    log = []
    for st, tags in sorted(located_tags().items()):
        path = REG / f"zone_assignment_{st}.csv"
        rows, cols = _read(path)
        for c in ("arch_numbers", "arch_names", "arch_modes", "arch_source", "room_name_tender"):
            if c not in cols:
                cols.append(c)
        polys = {p["room_id"]: wkt.loads(p["polygon_pv_wkt"]) for p in _read(REG / f"room_polygons_{st}.csv")[0] if p["polygon_pv_wkt"]}
        inside = defaultdict(list)
        for x in tags:
            if x["plan_room"]:
                inside[x["plan_room"]].append(x)
        by_id = {r["room_id"]: r for r in rows}
        for rid, xs in inside.items():
            r = by_id.get(rid)
            if r is None:
                continue
            r["arch_numbers"] = " | ".join(x["number"] for x in xs)
            r["arch_names"] = " | ".join(x["name"] for x in xs)
            r["arch_modes"] = " | ".join(x["mode"] or "-" for x in xs)
            r["arch_source"] = " | ".join(sorted({x["source"] for x in xs}))
            names = {x["name"] for x in xs if x["name"]}
            if len(names) == 1:
                new = names.pop()
                if new != r["room_name"]:
                    r["room_name_tender"] = r.get("room_name_tender") or r["room_name"]
                    log.append(dict(storey=st, room_id=rid, change="room name", before=r["room_name"], after=new, evidence=r["arch_numbers"]))
                    r["room_name"] = new
            modes = {_mode(x["mode"]) for x in xs if x["mode"]}
            kind = r["class_kind"]
            ruled = [p for p in PROTECTED if p in (r.get("ruling") or "")]
            cold = "cold room" in (r["class"] or "").lower()
            target = None
            if modes and modes <= {"MV"} and kind in UNIT_KINDS:
                target = ("MV", "MV")
            elif modes and modes <= {"NV"} and kind in UNIT_KINDS:
                target = ("NV", "NV")
            elif modes and modes <= {"AC"} and kind in ("no information", "MV", "NV"):
                target = ("neighbour", None)
            elif modes and modes <= {"AC"} and cold:
                log.append(dict(storey=st, room_id=rid, change="not changed: cold room by a specialist", before=r["class"], after="",
                                evidence=f"{r['arch_names']} drawn {r['arch_modes']}"))
            if not target:
                continue
            if ruled:
                log.append(dict(storey=st, room_id=rid, change=f"not changed: class set by {', '.join(ruled)}", before=r["class"],
                                after=target[0], evidence=f"{r['arch_names']} drawn {r['arch_modes']}"))
                continue
            if target[0] == "neighbour":
                g = polys.get(rid)
                score = Counter()
                if g is not None:
                    ring = g.buffer(6)
                    for o in rows:
                        if (o["room_id"] == rid or o["class_kind"] not in ("AHU", "FCU", "CRAC") or o["room_id"] not in polys
                                or "duty/standby" in o["class"] or "tag to confirm" in o["class"]):
                            continue
                        a = ring.intersection(polys[o["room_id"]]).area
                        if a > 1:
                            score[o["class"]] += a
                if not score:
                    log.append(dict(storey=st, room_id=rid, change="not changed: drawn AC, no neighbouring room on a unit", before=r["class"],
                                    after="", evidence=f"{r['arch_names']} drawn {r['arch_modes']}"))
                    continue
                cls = score.most_common(1)[0][0]
                nk = next(o["class_kind"] for o in rows if o["class"] == cls)
                note = f"Drawn air-conditioned on the part plans ({r['arch_names']}); unit of the neighbouring room with the longest shared boundary, assumed (D-107, D-108)."
                new = (cls, nk)
            else:
                new = target
                note = f"Drawn {target[0]} on the part plans ({r['arch_names']}, {r['arch_numbers']}) (D-107, D-108)."
            log.append(dict(storey=st, room_id=rid, change="class", before=r["class"], after=new[0], evidence=note))
            r["class_before_ruling"] = r.get("class_before_ruling") or r["class"]
            r["class"], r["class_kind"] = new
            r["ruling"] = "; ".join(x for x in [r.get("ruling", ""), "D-107"] if x)
            r["notes"] = ((r.get("notes") or "") + " " + note).strip()
            r["sources"] = ((r.get("sources") or "") + "; " + SRC_TEXT).strip("; ")
        _write(path, rows, cols)
    QA.mkdir(parents=True, exist_ok=True)
    _write(QA / "arch_zoning_corrections.csv", log, ["storey", "room_id", "change", "before", "after", "evidence"])
    return log


def workbooks(datasets: Dict[str, Dict[str, Any]], root: Path = ROOT) -> List[Dict[str, Any]]:
    tags = {x["number"]: (st, x) for st, xs in located_tags().items() for x in xs if x["plan_room"]}
    za = {}
    for st in {v[0] for v in tags.values()}:
        for z in _read(REG / f"zone_assignment_{st}.csv")[0]:
            za[z["room_id"]] = z
    log = []
    for n, d in datasets.items():
        for r in d["rooms"]:
            num = (r.get("soa_revit") or {}).get("number")
            if not num or num not in tags:
                continue
            st, x = tags[num]
            z = za.get(x["plan_room"], {})
            cls, kind, mode = z.get("class", ""), z.get("class_kind", ""), _mode(x["mode"])
            before = r.get("ahu") or r.get("system_type") or ""
            sid = f"ARCH-{num}"
            if not any(s["id"] == sid for s in d["sources"]):
                d["sources"].append(dict(id=sid, document=SRC_TEXT, issue="26 Nov 2026", page=x["sheet"], location=f"tag {num}",
                                         text_as_read=f"{x['source']} tag {num} '{x['name']}', {x['area_m2']} m², {x['mode'] or 'no mode'}; in plan room {x['plan_room']} ({z.get('room_name', '')}, class {cls})",
                                         method="Room tag of the part plan placed on the zoning background; unit of the plan room that holds it (D-107, D-108)",
                                         confidence="high"))
            r["plan_room_id"] = x["plan_room"]
            r.setdefault("prov", {})["zoning"] = ["D", sid]
            (r.get("gaps") or {}).pop("plan", None)
            if mode in ("MV", "NV"):
                new_unit, new_st = None, mode
            elif kind in UNIT_KINDS and TAGRE.search(cls or ""):
                new_unit, new_st = cls, ("FCU" if cls.startswith(("FCU", "DXFCU")) else (r.get("system_type") if r.get("system_type") not in ("MV", "NV", "OTHER", "NO INFORMATION") else None))
            elif kind in UNIT_KINDS:
                new_unit, new_st = r.get("ahu"), r.get("system_type") or "FCU"
            else:
                new_unit, new_st = r.get("ahu"), r.get("system_type")
            changed = (new_unit or new_st or "") != before
            r["ahu"], r["system_type"] = new_unit, new_st
            note = (f"Drawing (D-107, D-108): tag {num} '{x['name']}' ({x['mode'] or 'no mode'}) is in plan room {x['plan_room']} "
                    f"({z.get('room_name', '')}), class {cls}.")
            r.setdefault("gaps", {}).pop("ahu", None)
            if changed:
                r["remark"] = (r.get("remark") or "").rstrip() + f" Unit changed from {before or 'none'} to {new_unit or new_st or 'none'}: " + note
            log.append(dict(workbook=f"D{n}", room=r["number"], name=r.get("name"), revit_number=num, source=x["source"], drawing_name=x["name"], drawing_mode=x["mode"],
                            plan_room=x["plan_room"], zone_class=cls, before=before, after=new_unit or new_st or "", changed="Y" if changed else "N"))
    QA.mkdir(parents=True, exist_ok=True)
    _write(QA / "arch_room_links.csv", log, ["workbook", "room", "name", "revit_number", "source", "drawing_name", "drawing_mode", "plan_room", "zone_class",
                                             "before", "after", "changed"])
    return log


if __name__ == "__main__":
    lg = zoning()
    print(Counter(x["change"].split(":")[0] for x in lg))
