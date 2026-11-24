"""Council C6 (24 Nov 2026): apply the verified proposals of the zoning review to the zone assignment and write the equipment assignment register.

Inputs: out/work/council_r6/proposals_<G>.json and verdicts_<G>.json (one reviewer and one adversarial verifier per storey group).
Only proposals with verdict 'accept' or 'amend' (the amended object) are applied. A rejected proposal changes nothing and is listed.

* room classes: the class of the plan room becomes the proposed class (ruling 'C6'); the earlier class stays in class_before_ruling;
* units 'zone': the rooms named take the unit as class, where their class is not already another air handling unit (else a conflict is listed);
* fans: written to the register with their unit, rooms or system; MV rooms keep the class MV and carry the fan tags in the column 'fans'.

Outputs: out/registers/zone_assignment_<STOREY>.csv (updated), out/registers/equipment_assignment.csv (every unit and fan of the Equipment Schedule
that serves LB3 to L07, with its disposition), out/qa/council_c6_changes.csv, out/qa/council_c6_rejected.csv, out/qa/council_c6_conflicts.csv.

``python3 -m src.zoning.council_c6``
"""
from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent.parent
REG = ROOT / "out/registers"
WORK = ROOT / "out/work/council_r6"
QA = ROOT / "out/qa"
STOREYS = ["LB3", "B3", "B2", "B1", "L01", "L02", "L03", "L04", "L05", "L06", "L07"]
GENERIC = re.compile(r"^(no information|MV|NV|FCU( duty/standby)? \(tag to confirm\).*)$", re.I)
UNIT_KIND = {"AHU": "AHU", "PAHU": "AHU", "FCU": "FCU", "DXFCU": "FCU", "CRAC": "CRAC"}


def rd(p: Path) -> List[Dict[str, str]]:
    with p.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def wr(p: Path, rows: List[Dict[str, Any]], cols: List[str] = None) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    cols = cols or (list(rows[0]) if rows else ["none"])
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: (json.dumps(r[c], ensure_ascii=False) if isinstance(r.get(c), (list, dict)) else r.get(c, "")) for c in cols})


def ev_text(ev: Any) -> str:
    if not isinstance(ev, list):
        return str(ev or "")
    return " | ".join(f"{e.get('document', '')} {e.get('page_or_row', '')}: {e.get('text_as_read', '')}".strip() for e in ev if isinstance(e, dict))


def verified() -> Dict[str, List[Dict[str, Any]]]:
    out = defaultdict(list)
    rejected = []
    for pf in sorted(WORK.glob("proposals_G*.json")):
        g = pf.stem.split("_")[1]
        prop = json.loads(pf.read_text(encoding="utf-8"))
        vf = WORK / f"verdicts_{g}.json"
        verd = {}
        if vf.exists():
            for v in json.loads(vf.read_text(encoding="utf-8")).get("verdicts", []):
                verd[(v.get("section"), str(v.get("key")))] = v
        for sec, keyf in (("room_classes", "room_id"), ("unit_assignments", "tag"), ("fan_assignments", "tag")):
            for p in prop.get(sec, []):
                k = str(p.get(keyf))
                v = verd.get((sec, k))
                if v is None:
                    rejected.append(dict(group=g, section=sec, key=k, verdict="not verified", reason="no verdict", proposal=p))
                    continue
                if v["verdict"] == "accept":
                    out[sec].append(dict(p, _group=g, _verdict="accept", _vreason=v.get("reason", "")))
                elif v["verdict"] == "amend" and isinstance(v.get("amended"), dict):
                    out[sec].append(dict(v["amended"], _group=g, _verdict="amend", _vreason=v.get("reason", "")))
                else:
                    rejected.append(dict(group=g, section=sec, key=k, verdict=v["verdict"], reason=v.get("reason", ""), proposal=p))
    out["_rejected"] = rejected
    return out


def main() -> Dict[str, Any]:
    ok = verified()
    zones = {st: rd(REG / f"zone_assignment_{st}.csv") for st in STOREYS}
    cols = {st: list(rows[0].keys()) for st, rows in zones.items()}
    for st in STOREYS:
        for c in ("fans", "c6_evidence"):
            if c not in cols[st]:
                cols[st].append(c)
    by_id = {(st, r["room_id"]): r for st in STOREYS for r in zones[st]}
    changes, conflicts = [], []

    def set_class(st, rid, new, kind, why, ev, conf):
        r = by_id.get((st, rid))
        if r is None:
            conflicts.append(dict(storey=st, room_id=rid, item=new, conflict="room not in the zoning of this storey"))
            return False
        old = r["class"]
        if old == new:
            return True
        if not GENERIC.match(old) and r["class_kind"] in ("AHU", "CRAC") and kind in ("AHU", "CRAC"):
            conflicts.append(dict(storey=st, room_id=rid, item=new, conflict=f"room is already on {old}; council proposes {new}. Not changed."))
            return False
        if not r.get("class_before_ruling"):
            r["class_before_ruling"] = old
        r["class"], r["class_kind"] = new, kind
        r["ruling"] = "; ".join(dict.fromkeys(x for x in (r.get("ruling") or "").split("; ") + ["C6"] if x))
        r["ruling_reason"] = "; ".join(x for x in [r.get("ruling_reason", ""), why] if x)
        r["basis"] = "council C6"
        r["confidence"] = str(conf or r.get("confidence") or "")
        r["c6_evidence"] = ev
        r["notes"] = ((r.get("notes") or "") + f" Council C6: {why}").strip()
        changes.append(dict(storey=st, room_id=rid, room_name=r["room_name"], class_before=old, class_after=new, reason=why, evidence=ev))
        return True

    for p in ok["room_classes"]:
        kind = p.get("kind") or "unresolved"
        if kind == "unresolved" or not p.get("proposed_class"):
            continue
        set_class(p["storey"], p["room_id"], p["proposed_class"], kind, p.get("reason", ""), ev_text(p.get("evidence")), p.get("confidence"))

    # equipment register: every in-scope unit and fan, already zoned or assigned now
    ah = rd(REG / "equipment_schedule_air_handling.csv")
    fn = rd(REG / "equipment_schedule_fans.csv")

    def served(t):
        p_ = t.split("-")
        if len(p_) < 4:
            return None
        m = re.match(r"^(LB3|B[123]|\d\d)", p_[3])
        if not m:
            return None
        x = m.group(1)
        return x if x[0] in "LB" else "L" + x

    for p in ok["unit_assignments"]:
        if p.get("disposition") == "zone" and p.get("rooms"):
            t = p["tag"]
            disp = t
            kind = UNIT_KIND.get(t.split("-")[0], "AHU")
            st = p.get("storey") or served(t)
            for rid in p["rooms"]:
                rst = rid.split("-")[0] if rid.split("-")[0] in STOREYS else st
                set_class(rst, rid, disp, kind, p.get("reason", "") or f"Equipment Schedule unit {t}", ev_text(p.get("evidence")), p.get("confidence"))
    fans_of = defaultdict(list)
    for p in ok["fan_assignments"]:
        if p.get("disposition") == "mv_rooms":
            for rid in p.get("rooms") or []:
                fans_of[rid].append(p["tag"])
    for st in STOREYS:
        for r in zones[st]:
            if r["room_id"] in fans_of:
                r["fans"] = "; ".join(sorted(set(fans_of[r["room_id"]])))
    for st in STOREYS:
        wr(REG / f"zone_assignment_{st}.csv", zones[st], cols[st])

    # the register
    zoned = defaultdict(list)
    for st in STOREYS:
        for r in zones[st]:
            zoned[re.sub(r"\s*\[.*\]$", "", r["class"])].append(f"{st}:{r['room_id']}")

    def base(t):
        return re.sub(r"(A/B/C|A/B|[A-C])$", "", t)
    zb = defaultdict(list)
    for k, v in zoned.items():
        zb[base(k)] += v
    ua = {p["tag"]: p for p in ok["unit_assignments"]}
    fa = {p["tag"]: p for p in ok["fan_assignments"]}
    rej = {(x["section"], x["key"]): x for x in ok["_rejected"]}
    reg, seen = [], set()
    for r in ah + fn:
        t = r["tag_normalised"]
        st = served(t)
        if st not in STOREYS or t in seen:
            continue
        seen.add(t)
        is_fan = r in fn
        rooms = zoned.get(t) or zb.get(base(t)) or []
        p = (fa if is_fan else ua).get(t)
        if rooms and not is_fan:
            disp, det, ev, src = "zone", f"{len(rooms)} plan room(s)", "", "zoning"
        elif p:
            disp = p.get("disposition", "")
            det = "; ".join(x for x in [p.get("unit") or "", ", ".join(p.get("serves") or []), ", ".join(p.get("rooms") or []), p.get("system") or ""] if x)
            ev, src = ev_text(p.get("evidence")), f"council C6 ({p['_verdict']})"
        else:
            x = rej.get(("fan_assignments" if is_fan else "unit_assignments", t))
            disp, det, ev, src = "unresolved", (x or {}).get("reason", "no proposal verified"), "", "council C6 (rejected)" if x else ""
        reg.append(dict(tag=t, type=r.get("type", ""), served_storey=st, location=r.get("Location", ""), description=r.get("Description", ""),
                        disposition=disp, assigned_to=det, rooms=";".join(rooms[:60]), evidence=ev, source=src))
    wr(REG / "equipment_assignment.csv", reg)
    wr(QA / "council_c6_changes.csv", changes, ["storey", "room_id", "room_name", "class_before", "class_after", "reason", "evidence"])
    wr(QA / "council_c6_conflicts.csv", conflicts, ["storey", "room_id", "item", "conflict"])
    wr(QA / "council_c6_rejected.csv", ok["_rejected"], ["group", "section", "key", "verdict", "reason", "proposal"])
    from collections import Counter
    return dict(changes=len(changes), conflicts=len(conflicts), rejected=len(ok["_rejected"]),
                register=dict(Counter((x["type"] in ("EAF", "RAF", "OAF", "TEAF", "CEAF", "CSAF", "SSAF", "SAF", "SPAF", "IEF", "FLLF", "KEAF", "IEAF", "TEF", "HPF", "FAF", "SEF", "SFLF"), x["disposition"]) for x in reg)))


if __name__ == "__main__":
    print(main())
