"""Apply the class rulings of D-029, D-036, D-037, D-038, D-052, D-059 and D-070 to ``out/registers/zone_assignment_<KEY>.csv``.

``python3 -m src.zoning.class_rulings [--storeys LB3,B3,...]``

Idempotent: a row keeps its earlier class in ``class_before_ruling`` the first time a ruling changes it; the change list is rewritten in full to
``out/registers/zone_class_changes.csv`` (storey, room, name, class before, class after, ruling, reason). New columns on every row:

* ``basis``   source of the class, by the single precedence of D-052: RFI-ME-0233 / MOT NSC / DD Booklet zoning / working air-side schematic /
              App D / tender air-side schematic / plan mode label (a row with none of these says 'no basis');
* ``ruling``  rulings applied to the row (empty when none).

Rulings applied (the rest of D-070 is listed in ``out/qa/zone_rulings_not_applied.csv`` for the human check):
* D-029  electrical rooms (ELEC, LV, HV, HV Switchrm, Switchroom): FCU duty/standby, one zone per room; tag from the Equipment Schedule (Corr 5)
         only where one tag pair fits one room, else 'FCU duty/standby (tag to confirm)' with the candidate tags in the notes;
* D-037  IT closets, LAN, UPS and battery rooms: FCU (duty/standby for LAN rooms: HFDS table T.1.1.1.1 p13 'N Duty + 1 Stand-by'; UPS, battery
         and IT closet: Equipment Schedule prints duty/standby for B2 UPS and battery rooms), tag as for D-029;
* D-036  AHU plant rooms (plan name AHU) and B1-R001: served by spill air of the AHUs located on the level (Equipment Schedule Corr 5, location column),
         class 'AHU spill (plant room)'; HFDS plant-room criteria;
* D-038, D-059  MOT units: governing tag of the MOT cross-reference workbook (cath lab AHU-B-04-04CATH-01/-02, staff unit AHU-B-03-05MOTSTAFF-01);
* D-070 class A  plan mode label MV where an air-side schematic room table gives supply air: the unit of the schematic box, where one box
         type of that name names one unit on the storey (code A); else the row keeps MV and the ruling is noted.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent.parent
REG = ROOT / "out/registers"
CHANGES = REG / "zone_class_changes.csv"
NOT_APPLIED = ROOT / "out/qa/zone_rulings_not_applied.csv"
STOREYS = ["LB3", "B3", "B2", "B1", "L01", "L02", "L03", "L04", "L05", "L06", "L07"]
NEW_COLS = ["class_before_ruling", "basis", "ruling", "ruling_reason"]

ELEC_RE = re.compile(r"^(ELEC|LV|HV|HV Switchrm|Switchroom|Switchrm|Elec Room|Electrical Room)$", re.I)
IT_RE = re.compile(r"^(IT Closet|LAN|UPS|Batt|Battery)$", re.I)
# D-095: electrical plant rooms of HFDS Vol 2 cl. 1.1.3.20 (CSR, HT, UPS, battery) and the transformer room of table T.1.1.1.1 under other plan names
ELEC_PLANT_RE = re.compile(r"^(HT|HT CSR|CSR|HT Switchgear|HV Switchgear|HV Switchroom|Tfr|Transformer|Transformer Room|LV Switchroom|Low Voltage Switch Room|"
                           r"Generator Sync|SPG Switchroom|UPS|Batt|Battery|Battery Room)$", re.I)
VOL2_SRC = "HFDS Vol 2 MEP Standardization Report (14 Oct 25), cl. 1.1.3.20 p88 and table T.1.1.1.1 p44"
AHUROOM_RE = re.compile(r"^AHU( (room|space|plant|deck))?$", re.I)
CLASS_A = ["CF-0015", "CF-0021", "CF-0030", "CF-0039", "CF-0041", "CF-0055", "CF-0065", "CF-0078"]
MOT_TAG = {"AHU-B-07-04CATH-01": "AHU-B-04-04CATH-01", "AHU-B-07-04CATH-02": "AHU-B-04-04CATH-02",
           "AHU-B-07-05MOTSTAFF-01": "AHU-B-03-05MOTSTAFF-01"}
ELEC_SRC = "Equipment Schedule Corr 5 (SCH/1) p30, FCU-B-03-03ELEC-01A/B to -09A/B ('Corridor / Plant Room', L4 to L6)"


def rd(p: Path) -> List[Dict[str, str]]:
    with p.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def wr(p: Path, cols: List[str], rows: List[Dict[str, Any]]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def derive_basis(r: Dict[str, str]) -> str:
    """D-052 precedence (RFI, MOT NSC, DD Booklet zoning, working schematic or App D, tender schematic, plan mode label), read from the method, the
    sources and the class of the row. A unit class takes the highest source that the row cites; MV and NV take the plan mode label; a row
    with no unit evidence says so."""
    kind = (r.get("class_kind") or "").upper()
    t = ((r.get("method") or "") + " " + (r.get("sources") or "")).lower()
    dept = (r.get("department") or "").lower()
    if kind in ("MV", "NV"):
        return "plan mode label"
    if (r.get("class") or "").strip().lower() == "no information" or kind == "NO INFORMATION":
        return "no unit evidence"
    if "rfi-me-0233" in t and "zoning sample" in t:
        return "RFI-ME-0233 zoning"
    if "mot nsc" in t or dept.startswith("mot") or "ip-bh-16" in t:
        return "MOT NSC"
    if "booklet" in t and ("keyplan" in t or "booklet picture" in t or "booklet zon" in t):
        return "DD Booklet zoning"
    if "working" in t and ("schematic" in t or "drawing" in t or "issue" in t):
        return "working air-side schematic"
    if "app d" in t or "appendix d" in t:
        return "App D"
    if "schematic" in t or "ip-bh-0" in t:
        return "tender air-side schematic"
    return "plan mode label"


def conflict_rooms(item: str) -> List[str]:
    out, prefix = [], ""
    for m in re.finditer(r"\b(LB3|B[123]|L0[1-7])\b(?:-([RU]\d{3}))?|\b([RU]\d{3})\b", item):
        if m.group(1) and not m.group(2):
            prefix = m.group(1)
        elif m.group(1) and m.group(2):
            prefix = m.group(1)
            out.append(f"{prefix}-{m.group(2)}")
        elif m.group(3) and prefix:
            out.append(f"{prefix}-{m.group(3)}")
    return out


def level_locations(storey: str) -> List[str]:
    """Locations of the Equipment Schedule rows that stand on the level of the storey."""
    m = re.match(r"^L0?(\d+)$", storey)
    if m:
        return ["L" + m.group(1), "IP L" + m.group(1)]
    return {"B1": ["IP B1", "IP B1 (N)", "IP B1 (S)"], "B2": ["IP B2"], "B3": ["IP B3"], "LB3": ["IP LB3"]}.get(storey, [])


def spill_sources(storey: str, ahus: List[Dict[str, str]], room_id: str = "") -> List[str]:
    locs = ["IP B1 (N)"] if room_id == "B1-R001" else level_locations(storey)
    return sorted({a["tag_normalised"] for a in ahus if (a.get("plant_level [Location]") or "").strip() in locs})


def run(storeys: List[str]) -> Dict[str, Any]:
    ahus = rd(REG / "ahu_schedule.csv")
    from ..room_links import schematic_boxes, name_fit, PRIO, zkey, zdisp
    boxes: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for b in schematic_boxes():
        boxes[b["storey"]].append(b)
    conf = [c for c in rd(REG / "conflicts.csv") if c["conflict_id"] in CLASS_A]
    a_rooms: Dict[str, str] = {}
    for c in conf:
        for rid in conflict_rooms(c["item"]):
            a_rooms[rid] = c["conflict_id"]
    changes: List[Dict[str, Any]] = []
    tally: Dict[str, int] = defaultdict(int)
    for st in storeys:
        path = REG / f"zone_assignment_{st}.csv"
        if not path.exists():
            continue
        rows = rd(path)
        cols = list(rows[0].keys())
        for c in NEW_COLS:
            if c not in cols:
                cols.append(c)
        for r in rows:
            for c in NEW_COLS:
                r.setdefault(c, "")
        n_by_name: Dict[str, int] = defaultdict(int)
        for r in rows:
            n_by_name[r["room_name"].strip().lower()] += 1

        def change(r, new, new_kind, ruling, reason, note, conf_=None):
            if not r["class_before_ruling"]:
                r["class_before_ruling"] = r["class"]
            if ruling in r["ruling"]:
                return
            r["class"], r["class_kind"] = new, new_kind
            if conf_ is not None:
                r["confidence"] = conf_
            r["ruling"] = "; ".join(x for x in [r["ruling"], ruling] if x)
            r["ruling_reason"] = "; ".join(x for x in [r["ruling_reason"], reason] if x)
            r["notes"] = ((r.get("notes") or "").rstrip() + " " + note).strip()
            if not r["method"].startswith("ruling"):
                r["method"] = f"ruling {ruling}; earlier method: {r['method']}"

        for r in rows:
            name = (r["room_name"] or "").strip()
            cls = (r["class"] or "").strip()
            rid = r["room_id"]
            # D-038 / D-059 MOT tags
            for a, b in MOT_TAG.items():
                base = re.sub(r"A/B$", "", cls)
                if cls == a or base == a:
                    change(r, b, "AHU", "D-038", f"governing tag of the MOT cross-reference workbook is {b}; {a} listed as variant",
                           f"Tag {b} follows the MOT cross-reference workbook and the MOT NSC (D-038). Variant {a} is listed, not used.")
            # D-029 electrical rooms
            if ELEC_RE.match(name):
                new = f"FCU duty/standby (tag to confirm) [{rid}]"
                change(r, new, "FCU", "D-029", "electrical room is served by duty/standby FCUs; one zone per room",
                       f"Electrical room: served by a duty/standby FCU pair (one zone, D-029). Tag to confirm: {ELEC_SRC} lists the pairs; no document gives the room of each pair.",
                       r.get("confidence") or "0.6")
            # D-037 IT, LAN, UPS, battery rooms
            elif IT_RE.match(name):
                lan = name.lower() == "lan"
                new = (f"FCU duty/standby (tag to confirm) [{rid}]" if (lan or st == "B2") else f"FCU (tag to confirm) [{rid}]")
                why = ("LAN room: HFDS table T.1.1.1.1 p13 'Duty + 1 Stand-by'" if lan else
                       "UPS or battery room on B2: Equipment Schedule Corr 5 p12 'Duty/Standby'" if st == "B2" else
                       "IT closet, UPS or battery room: FCU, HFDS gives no standby row for this room type")
                change(r, new, "FCU", "D-037", f"{name} room served by FCU: {why}",
                       f"{name} room: FCU (D-037); {why}. Equipment Schedule Corr 5 pp12, 54 to 57 and 61 list FCU-B-<storey>-IT, -IT02, B2UPS and B2BATT tags; no document gives the room of each tag.",
                       r.get("confidence") or "0.6")
            # D-036 AHU plant rooms (and B1-R001)
            elif AHUROOM_RE.match(name) or rid == "B1-R001":
                src = spill_sources(st, ahus, rid)
                change(r, "AHU spill (plant room)", "AHU spill", "D-036", "AHU plant room conditioned by spill air from the AHU supply",
                       f"AHU plant room: spill air from the supply of the AHUs located on this level conditions the room to the HFDS plant-room criteria (D-036, D-062). "
                       f"Source units (Equipment Schedule Corr 5, location column): {len(src)} unit(s){': ' + ', '.join(src[:6]) + (' and others' if len(src) > 6 else '') if src else ''}. "
                       f"Allocation pro rata to supply airflow, to confirm.", r.get("confidence") or "0.6")
            # D-070 class A
            elif rid in a_rooms and r["class_kind"] == "AHU" and "D-070 A" not in r["ruling"]:
                r["ruling"] = "; ".join(x for x in [r["ruling"], "D-070 A (class already AHU)"] if x)
                r["ruling_reason"] = "; ".join(x for x in [r["ruling_reason"], f"{a_rooms[rid]}: AHU supply with dedicated exhaust (D-031); plan label MV recorded in the notes"] if x)
            elif rid in a_rooms and cls == "MV" and "D-070 A" not in r["ruling"]:
                cand = [b for b in boxes.get(st, []) if name_fit(name, b["name"]) >= 0.85]
                if cand:
                    best = min(PRIO.get(b["issue"], 3) for b in cand)
                    cand = [b for b in cand if PRIO.get(b["issue"], 3) == best]
                sets = {b["tags"] for b in cand}
                tags = [t.strip() for t in next(iter(sets)).split(";")] if len(sets) == 1 else []
                if tags and len({zkey(t) for t in tags}) == 1:
                    tag = zdisp(tags[0])
                    change(r, tag, "AHU", "D-070 A", f"{a_rooms[rid]}: AHU supply with dedicated exhaust and no return (D-031); plan label MV recorded",
                           f"Ruling D-070 class A ({a_rooms[rid]}): the air-side schematic room table gives this room type supply air from {tag}; plan mode label MV recorded.",
                           "0.6")
                else:
                    r["ruling"] = "; ".join(x for x in [r["ruling"], "D-070 A (unit not found)"] if x)
                    r["notes"] = ((r.get("notes") or "") + f" Ruling D-070 class A ({a_rooms[rid]}): AHU supply with dedicated exhaust expected; no single schematic box fixes the unit, class stays MV.").strip()
            # D-095: every electrical plant room on duty/standby FCUs, unless a schematic or the MOT NSC names an AHU for it (that AHU is kept)
            if ELEC_PLANT_RE.match(name) and r["class_kind"] != "AHU":
                new = f"FCU duty/standby (tag to confirm) [{rid}]"
                if r["class"] != new:
                    change(r, new, "FCU", "D-095", f"electrical plant room '{name}': N duty + 1 standby FCUs, 27 deg C or less ({VOL2_SRC})",
                           f"Electrical plant room: ducted FCUs in N duty + 1 standby, room at 27 deg C or less ({VOL2_SRC}; D-095). Plan mode label kept in class_before_ruling. Tag to confirm.",
                           r.get("confidence") or "0.6")
            elif ELEC_PLANT_RE.match(name) and "D-095" not in r["ruling"]:
                r["ruling"] = "; ".join(x for x in [r["ruling"], "D-095 (AHU kept)"] if x)
                r["notes"] = ((r.get("notes") or "") + f" Electrical plant room on {cls}: HFDS Vol 2 cl. 1.1.3.20 asks for duty + standby FCUs; the AHU of the source is kept, to confirm (D-095).").strip()
            r["ruling"] = "; ".join(dict.fromkeys(x for x in r["ruling"].split("; ") if x))
            if "D-038" in r["ruling"]:
                r["basis"] = "MOT NSC"
            elif any(x in r["ruling"].split("; ") for x in ("D-029", "D-036", "D-037", "D-095")):
                r["basis"] = "council ruling " + ", ".join(x for x in ("D-029", "D-036", "D-037", "D-095") if x in r["ruling"].split("; "))
            elif "D-070 A" in r["ruling"] and "not found" not in r["ruling"]:
                r["basis"] = "working air-side schematic"
            else:
                r["basis"] = derive_basis({**r, "method": re.sub(r"^ruling [^;]*; earlier method: ", "", r["method"])})
            if r["class_before_ruling"] and r["class_before_ruling"] != r["class"]:
                changes.append(dict(storey=r["storey"], room_id=rid, room_name=r["room_name"], class_before=r["class_before_ruling"], class_after=r["class"],
                                    ruling=r["ruling"], reason=r["ruling_reason"]))
                for x in r["ruling"].split("; "):
                    tally[x] += 1
        wr(path, cols, rows)
    # the rulings of D-070 that need a human or a later pass
    na = []
    for c in rd(REG / "conflicts.csv"):
        if "C5" not in c["status"]:
            continue
        cid = c["conflict_id"]
        n = int(cid[3:])
        cls = ("A" if cid in CLASS_A else "B" if cid in {f"CF-{x:04d}" for x in (8, 13, 16, 26, 31, 36, 38, 40, 46, 57, 66)} else
               "C" if cid in {f"CF-{x:04d}" for x in (47, 50, 34, 35, 44, 9, 11, 12, 76, 45, 42)} else
               "D" if cid in {f"CF-{x:04d}" for x in (19, 20, 23, 24, 27, 29, 37, 43, 56, 58, 60, 64, 67, 69, 72, 73, 79)} else
               "E" if cid in {f"CF-{x:04d}" for x in (10, 14, 17, 22, 25, 49, 52, 53, 54, 59, 61, 62, 63, 68)} else
               "F" if cid in {f"CF-{x:04d}" for x in (18, 32, 71, 81)} else
               "G" if cid in {f"CF-{x:04d}" for x in (28, 33, 48, 70, 75, 77, 80)} else "H" if n >= 82 else "")
        status = {"A": "applied where one schematic box fixes the unit (zone_class_changes.csv), else noted",
                  "B": "no class change: tag as written on the governing document; variants listed",
                  "C": "applied through D-029, D-036, D-037, D-038 or already in the adjudicated class",
                  "D": "booklet zone governs through the room list (room_unit_links.csv step 1); plan-room class not re-cut (the booklet zone line is not registered for every page)",
                  "E": "adjudicated class already follows the later dated document",
                  "F": "NOT APPLIED: the polygon is not split on the booklet or App D line; listed for the human check",
                  "G": "links by group or count: room_unit_links.csv steps 3 to 5",
                  "H": "tender airflow: not a zoning item"}.get(cls, "")
        na.append(dict(conflict_id=cid, ruling_class=cls, item=c["item"][:160], handling=status))
    wr(NOT_APPLIED, ["conflict_id", "ruling_class", "item", "handling"], na)
    wr(CHANGES, ["storey", "room_id", "room_name", "class_before", "class_after", "ruling", "reason"], changes)
    return dict(changes=len(changes), by_ruling=dict(tally))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--storeys", default=",".join(STOREYS))
    a = ap.parse_args(argv)
    print(run(a.storeys.split(",")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
