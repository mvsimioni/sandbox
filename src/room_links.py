"""Room-to-unit link register (D-051): out/registers/room_unit_links.csv.

One row per department room (DPA room of the department workbooks, universe snapshot in out/registers/room_universe.csv), per
App D common-area space (D00) and per plan room of the LB3 to L01 common areas (D-044). This register is the only interface
between zoning and the workbooks.

Ladder (first hit wins, D-051 with D-052):
  0  RFI-ME-0233 for the DI units on L04 (D-006); MOT NSC and the AHU cross-reference workbook for MOT rooms (D-038, D-059)
  1  DD Booklet room list: the zone that lies under the room label on the booklet keyplan page, located through the DPA/SOA room name
  2  plan room link (one to one, open-plan region, name and count, department rule) with the area guard of OI-R302-1
  3  air-side schematic room table (working issue, then MOT NSC, then tender) by room number, else by room type and typical count
  4  DPA child row takes the unit of its parent group; a room type named inside a composite schematic row takes the unit of that row
  5  DPA/SOA functional group, where every linked room of the group has one unit (at least 3 linked rooms, at least half of the group)
Steps 4 and 5 are assumptions (code A). No unit is given by proximity. A room that no step reaches stays 'no information' with a reason.

Simplification of D-071: the booklet step reads whole-word room labels only (a label printed as split fragments is not joined),
and a typical room printed once on a booklet page is not matched by count. Both leave the room to the later steps.

Run:  python -m src.room_links [--refresh-universe]
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import re
import sys
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
REG = ROOT / "out/registers"
OUT = REG / "room_unit_links.csv"
UNIVERSE = REG / "room_universe.csv"
UNKNOWN = ROOT / "out/qa/room_unit_links_unknown.csv"
SUMMARY = ROOT / "out/qa/room_unit_links_summary.md"

COLS = ["dept", "dpa_room_id", "room_name", "storey", "unit_class", "unit_kind", "basis", "step", "source_ref", "confidence", "code",
        "other_source_unit", "note"]
UNIV_COLS = ["dept", "dpa_room_id", "room_name", "storey", "area_m2", "prior_unit", "prior_kind", "prior_src", "prior_basis", "plan_room_ids",
             "parent_child", "parent_group", "functional_group"]

STEP_BASIS = {
    "0a": "RFI-ME-0233 zoning (DI units, L04)",
    "0b": "MOT NSC cross-reference workbook",
    "0c": "council ruling D-029 / D-037 (electrical, IT, LAN, UPS and battery rooms on FCUs)",
    "1": "DD Booklet room list, zone under the room label",
    "2": "plan room link",
    "3": "air-side schematic room table",
    "4": "child takes the unit of its parent group",
    "5": "functional group, one unit for every linked room",
    "6": "App D space (common areas)",
    "7": "plan room zoning (common areas LB3 to L01)",
}


def rd(path: Path) -> List[Dict[str, str]]:
    with Path(path).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def wr(path: Path, cols: List[str], rows: List[Dict[str, Any]]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def _norm(s: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _flt(x: Any) -> Optional[float]:
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


def zkey(tag: str) -> str:
    """A duty and standby pair is one zone (key without the A/B)."""
    m = re.match(r"^(.*-\d{2})([AB])(?:/B)?$", (tag or "").strip())
    return m.group(1) if m else (tag or "").strip()


def zdisp(tag: str) -> str:
    m = re.match(r"^(.*-\d{2})([AB])(?:/B)?$", (tag or "").strip())
    return m.group(1) + "A/B" if m else (tag or "").strip()


# --------------------------------------------------------------------------- #
# universe
# --------------------------------------------------------------------------- #
def _plan_ids(text: str) -> str:
    return ";".join(sorted(set(re.findall(r"\b(?:LB3|B[123]|L0[1-7])-R\d+(?:-T\d+)?\b", text or ""))))


def build_universe() -> List[Dict[str, Any]]:
    """Snapshot of the department rooms and their earlier plan/MOT/schematic unit, from the department inputs (out/loads/inputs/D*.json)."""
    dpa = {}
    for r in rd(REG / "dpa_rooms.csv"):
        dpa.setdefault(r["unique_room_identifier"], r)
    soa = {}
    for r in rd(REG / "soa_rooms.csv"):
        if (r.get("governing") or "").lower() == "yes":
            soa.setdefault(r["room_code"].strip(), r)
    rows: List[Dict[str, Any]] = []
    for f in sorted(glob.glob(str(ROOT / "out/loads/inputs/D*.json"))):
        d = json.load(open(f))
        dept = Path(f).stem[1:]
        audit = {a["ref"]: a for a in d.get("link_audit", []) if a.get("side") == "DPA"}
        for r in d["rooms"]:
            z = (r.get("prov") or {}).get("zoning") or ["", ""]
            src = str(z[1])
            a = audit.get(r["number"], {})
            if r.get("ahu"):
                kind = "mot" if src.startswith("MOT-") else "schematic" if src.startswith("SCHROOM-") else "plan" if src.startswith("ZONE-") else "other"
            else:
                kind = "none"
            p = dpa.get(r["number"], {})
            s = soa.get(r["number"], {})
            rows.append(dict(dept=dept, dpa_room_id=r["number"], room_name=r["name"], storey=r["storey"], area_m2=r.get("dpa_area_m2") or "",
                             prior_unit=r.get("ahu") or "", prior_kind=kind, prior_src=src, prior_basis=a.get("plan_basis", ""),
                             plan_room_ids=_plan_ids(src + " " + (r.get("remark") or "")), parent_child=p.get("parent_child", ""),
                             parent_group=p.get("parent_group_name", ""), functional_group=s.get("functional_group", "")))
    return rows


def load_universe(refresh: bool = False) -> List[Dict[str, Any]]:
    if refresh or not UNIVERSE.exists():
        u = build_universe()
        wr(UNIVERSE, UNIV_COLS, u)
        return u
    return rd(UNIVERSE)


# --------------------------------------------------------------------------- #
# name matching (the same scoring as the zoning links of the workbook assembly)
# --------------------------------------------------------------------------- #
_abbr = None


@lru_cache(maxsize=None)
def name_fit(a: str, b: str) -> float:
    global _abbr
    from .workbook.assemble import _expansions, zoning_name_score
    if _abbr is None:
        _abbr = _expansions(ROOT)
    return zoning_name_score(a, b, _abbr)


# --------------------------------------------------------------------------- #
# step 0
# --------------------------------------------------------------------------- #
def step0(univ: List[Dict[str, Any]], zone_rows: Dict[str, Dict[str, str]], res: Dict[Tuple[str, str], Dict[str, Any]]) -> None:
    # 0a: DI L04 rooms whose plan room is zoned by the RFI-ME-0233 sample
    for u in univ:
        if u["dept"] != "26" or u["storey"] != "L04" or not u["prior_unit"] or u["prior_kind"] != "plan":
            continue
        ids = [i for i in u["plan_room_ids"].split(";") if i]
        zr = [zone_rows[i] for i in ids if i in zone_rows]
        if zr and all("RFI-ME-0233" in (z.get("method") or "") for z in zr):
            res[(u["dept"], u["dpa_room_id"])] = dict(unit=u["prior_unit"], step="0a", ref="RFI-ME-0233 memo, zoning sample, page 4, tag crossref; plan room " + ",".join(ids),
                                                    conf="high", code="D")
    # 0c: electrical rooms (D-029) and IT, LAN, UPS and battery rooms (D-037): FCU, tag to confirm (see zoning/class_rulings.py for the tag evidence)
    elec = re.compile(r"^(electrical room|elec|switch ?room|lv|hv|hv switchrm)$", re.I)
    itr = re.compile(r"^(battery room|ups|ipas ups|it equipment room|lan( room)?|it closet|batt)$", re.I)
    for u in univ:
        nm = u["room_name"].strip()
        k = (u["dept"], u["dpa_room_id"])
        if k in res:
            continue
        if elec.match(nm) or itr.match(nm):
            rule = "D-029" if elec.match(nm) else "D-037"
            standby = rule == "D-029" or nm.lower().startswith("lan") or u["storey"] == "B2"
            unit = "FCU duty/standby (tag to confirm)" if standby else "FCU (tag to confirm)"
            res[k] = dict(unit=unit, step="0c", conf="medium", code="A", other=u["prior_unit"] if u["prior_unit"] and not u["prior_unit"].startswith("FCU") else "",
                          ref=f"council ruling {rule}; candidate tags: Equipment Schedule Corr 5 pp12, 30, 54 to 61 (no document gives the room of each tag)",
                          note=("earlier link " + u["prior_unit"] + " replaced (listed, D-029/D-037)") if u["prior_unit"] and not u["prior_unit"].startswith("FCU") else "")
    # 0b: MOT theatres and sterile hold by the id printed in the cross-reference description; other MOT rooms keep the MOT NSC schedule of modes link
    xr = rd(REG / "mot_ahu_crossref.csv")
    by_id: Dict[str, Tuple[str, str]] = {}
    for r in xr:
        tag = (r.get("governing_tag_as_printed") or "").strip()
        if not tag:
            continue
        tag = tag.split(" / ")[0].strip()
        for m in re.finditer(r"\((MSOT|DSOT) (\d\d\.\d\d\.\d\d)\)", r.get("description_as_printed") or ""):
            by_id[f"{m.group(1)} {m.group(2)}"] = (tag, f"AHU Cross Verification MOT NSC Register, sheet {r.get('sheet') or ''} row {r.get('crossref_row')}")
    # sterile hold rooms: the id has no entry in the cross-reference; the pod gives it (DSOT 02/03/04 and MSOT 03/04/05 are pods 1/2/3)
    sterile: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for r in xr:
        tag = (r.get("governing_tag_as_printed") or "").strip().split(" / ")[0]
        if tag and (r.get("description_as_printed") or "").lower().startswith("sterile hold") and r.get("pod"):
            lvl = "L6" if "L6" in (r.get("level_as_printed") or "") else "L5" if "L5" in (r.get("level_as_printed") or "") else ""
            sterile[(lvl, r["pod"].strip().lower())] = (tag, f"AHU Cross Verification MOT NSC Register, row {r.get('crossref_row')} (Sterile Hold, {r['pod']})")
    for u in univ:
        k = (u["dept"], u["dpa_room_id"])
        if k in res:
            continue
        m = re.match(r"^(MSOT|DSOT) (\d\d)\.08\.\d\d$", u["dpa_room_id"])
        if m and u["room_name"].lower().startswith("sterile hold"):
            pod = int(m.group(2)) - (2 if m.group(1) == "MSOT" else 1)
            hit = sterile.get(("L5" if m.group(1) == "MSOT" else "L6", f"pod{pod}"))
            if hit:
                res[k] = dict(unit=zdisp(hit[0]), step="0b", ref=hit[1] + f"; pod from the room number {u['dpa_room_id']}", conf="medium", code="D")
                continue
        if u["dpa_room_id"] in by_id:
            tag, ref = by_id[u["dpa_room_id"]]
            note = ""
            if u["prior_unit"] and zkey(u["prior_unit"]) != zkey(tag):
                note = f"earlier link {u['prior_unit']}"
            res[k] = dict(unit=zdisp(tag), step="0b", ref=ref, conf="high", code="D", other=u["prior_unit"] if note else "", note=note)
        elif u["prior_kind"] == "mot":
            unit, note = u["prior_unit"], ""
            # D-059: the Appendix J tag MOTHYB-02 is a variant (listed, not used); the Pod 3 hybrid unit is MOTHYB-01. A preparation room of another pod
            # (MSOT 03.x, Pod 1) does not join the Pod 3 unit: it takes the unit of the theatre of its own pod (MSOT 03.02.04).
            if "MOTHYB-02" in unit:
                unit, note = unit.replace("MOTHYB-02", "MOTHYB-01"), "the Appendix J tag is a variant of the Pod 3 hybrid unit: listed and not used (D-059)"
            mm = re.match(r"^(MSOT) (\d\d)\.01\.(\d\d)$", u["dpa_room_id"])
            if mm and "MOTHYB-01" in unit and mm.group(2) != "05":
                th = by_id.get(f"MSOT {mm.group(2)}.02.04")
                if th:
                    unit, note = zdisp(th[0]), f"the Appendix J row gives the Pod 3 hybrid unit; the preparation room is of the pod of theatre MSOT {mm.group(2)}.02.04 (D-059)"
            res[k] = dict(unit=unit, step="0b", ref="MOT NSC schedule of modes (Appendix J) room row " + u["prior_src"], conf="medium", code="D",
                          other=u["prior_unit"] if note else "", note=note)


# --------------------------------------------------------------------------- #
# step 1: booklet
# --------------------------------------------------------------------------- #
_TITLE = [(r"BASEMENT 3", "B3"), (r"BASEMENT 2", "B2"), (r"BASEMENT 1", "B1")]


def title_storey(t: str) -> str:
    t = (t or "").upper()
    if not t.startswith("IP"):
        return ""
    for pat, k in _TITLE:
        if re.search(pat, t):
            return k
    m = re.search(r"IP\s+(\d+)(?:ST|ND|RD|TH)\s+STOREY", t)
    return f"L{int(m.group(1)):02d}" if m else ""


def booklet_zone_hits() -> List[Dict[str, Any]]:
    """Every room label of an IP keyplan page with the zone tag under it: [{dept_no, storey, text, tag, zone_id, file, page}]."""
    from shapely import wkt
    from shapely.geometry import Point
    zones = []
    for f in sorted(glob.glob(str(REG / "booklet_zones_*.csv"))):
        zones += rd(Path(f))
    bypage: Dict[Tuple[str, int], List[Tuple[Any, Dict[str, str]]]] = defaultdict(list)
    for z in zones:
        if not (z.get("polygon_page_pt_wkt") or "").startswith("POLYGON") and not (z.get("polygon_page_pt_wkt") or "").startswith("MULTI"):
            continue
        bypage[(z["booklet_file"], int(float(z["page"])))].append((wkt.loads(z["polygon_page_pt_wkt"]), z))
    out: List[Dict[str, Any]] = []
    for r in rd(REG / "booklet_zoning_labels.csv"):
        if r["label_kind"] != "room_label":
            continue
        st = title_storey(r["keyplan_title"])
        polys = bypage.get((r["source_file"], int(float(r["pdf_page"]))))
        if not st or not polys:
            continue
        x0, y0, x1, y1 = map(float, r["bbox"].split(","))
        pt = Point((x0 + x1) / 2, (y0 + y1) / 2)
        hit = [z for P, z in polys if P.contains(pt)]
        tag, zid, why = "", "", ""
        if len(hit) == 1:
            z = hit[0]
            zid = z["zone_id"]
            if not (z.get("ahu_tag_as_printed") or "").strip():
                why = "booklet zone carries no AHU tag"
            elif (z.get("ahu_tag_other") or "").strip() and "low" in (z.get("confidence") or ""):
                why = "booklet zone carries several tags (image read, low confidence)"
            else:
                tag = z["ahu_tag_as_printed"].strip()
        elif len(hit) > 1:
            why = "label lies in more than one booklet zone"
        else:
            why = "label lies in no booklet zone polygon"
        txt = (r["text"] or "").strip()
        out.append(dict(dept_no=str(int(r["dept_no"])) if (r.get("dept_no") or "").strip().isdigit() else r.get("dept_no", ""), storey=st, text=txt,
                        block=(r.get("block_text") or "").strip(), tag=tag, zone_id=zid, why=why, file=Path(r["source_file"]).name, page=int(float(r["pdf_page"])),
                        bpage=r.get("booklet_page", "")))
    return out


def step1(univ, res, reasons) -> None:
    hits = booklet_zone_hits()
    by_st: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for h in hits:
        by_st[(h["dept_no"], h["storey"])].append(h)
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for u in univ:
        groups[(u["dept"].lstrip("0") or "0", u["storey"])].append(u)
    for (dno, st), rooms in groups.items():
        labs = by_st.get((dno, st), [])
        if not labs:
            continue
        for u in rooms:
            k = (u["dept"], u["dpa_room_id"])
            if k in res:
                continue
            fit = [h for h in labs if h["text"] and len(h["text"]) > 2 and name_fit(u["room_name"], h["text"]) >= 0.95]
            if not fit:
                reasons[k].append("no room label of the department booklet on this storey fits the room name")
                continue
            n_dpa = sum(1 for x in rooms if name_fit(x["room_name"], fit[0]["text"]) >= 0.95)
            # one label per page: the same room printed on two pages (part 1 and part 2) counts once per distinct tag set
            tags = {h["tag"] for h in fit}
            if "" in tags:
                reasons[k].append("; ".join(sorted({h["why"] for h in fit if h["why"]})) or "booklet zone has no tag")
                continue
            if len(tags) != 1:
                reasons[k].append(f"room label printed in {len(tags)} booklet zones: the count per unit is known, the identity is not")
                continue
            n_lab = len({(h["page"], h["zone_id"], h["text"]) for h in fit})
            if n_lab != n_dpa and n_lab < n_dpa:
                reasons[k].append(f"booklet prints {n_lab} label(s) for {n_dpa} rooms of this name (typical room printed once)")
                continue
            h = fit[0]
            res[k] = dict(unit=zdisp(next(iter(tags))), step="1", conf="medium", code="D",
                          ref=f"{h['file']} booklet p{h['bpage']} (PDF p{h['page']}), label '{h['text']}' in zone {h['zone_id']}")


# --------------------------------------------------------------------------- #
# step 2: plan link
# --------------------------------------------------------------------------- #
def step2(univ, res) -> None:
    for u in univ:
        k = (u["dept"], u["dpa_room_id"])
        if k in res or not u["prior_unit"] or u["prior_kind"] != "plan":
            continue
        ids = u["plan_room_ids"]
        basis = u["prior_basis"] or "plan link"
        conf = "high" if basis == "one to one" else "medium"
        code = "D" if basis in ("one to one", "open-plan region") else "A"
        res[k] = dict(unit=u["prior_unit"], step="2", conf=conf, code=code, ref=f"plan room {ids or '(see zone schedule)'}; link by {basis}; zone_assignment class")


# --------------------------------------------------------------------------- #
# step 3 and the composite part of step 4: schematic room tables
# --------------------------------------------------------------------------- #
PRIO = {"working": 0, "mot_nsc": 1, "tender": 2}


def _tagset(raw: str) -> str:
    tags = [t.strip() for t in (raw or "").split(";") if t.strip() and not re.search(r"/\d+[A-Z]$", t.strip())]
    keys: Dict[str, List[str]] = defaultdict(list)
    for t in tags:
        keys[zkey(t)].append(t)
    disp = sorted(zdisp(t) if len(v) > 1 or re.search(r"\d{2}[AB]", t) else t for t, v in ((t, keys[zkey(t)]) for t in tags))
    return "; ".join(dict.fromkeys(disp))


def schematic_boxes() -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for r in rd(REG / "schematic_rooms.csv"):
        nm = (r["room_name_as_written"] or "").strip()
        if not nm or not r.get("ahu_tag") or not r.get("storey"):
            continue
        key = (r["sheet"], r.get("box_page_view_table") or "", nm, r["ahu_tag"], r.get("supply_L_s"))
        if key in seen:
            continue
        seen.add(key)
        try:
            n = max(1, int(float(r.get("typical_count") or 1)))
        except ValueError:
            n = 1
        out.append(dict(sheet=r["sheet"], issue=r["issue_set"], name=nm, n=n, storey=r["storey"], tags=_tagset(r["ahu_tag"]), page=r.get("page", ""),
                        mode=r.get("mode", "")))
    return out


_STOP = {"room", "bed", "bay", "the", "and", "for", "area", "type"}


def _word_overlap(a: str, b: str) -> bool:
    wa = set(re.findall(r"[a-z]{3,}", a.lower())) - _STOP
    wb = set(re.findall(r"[a-z]{3,}", b.lower())) - _STOP
    return bool(wa & wb) or any(w[:3] in b.lower() for w in wa if len(w) > 4) or bool(set(re.findall(r"bed", a.lower())) & set(re.findall(r"bed", b.lower())))


def _is_group(tagset: str) -> bool:
    return len({zkey(t) for t in tagset.split("; ") if t}) > 1


def step3(univ, res, reasons, boxes) -> None:
    by_st: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for b in boxes:
        by_st[b["storey"]].append(b)
    n_by_name: Dict[Tuple[str, str], int] = Counter()
    for u in univ:
        n_by_name[(u["storey"], _norm(u["room_name"]))] += 1
    num_re = re.compile(r"(\d\d\.\d\d\.\d\d)")
    for u in univ:
        k = (u["dept"], u["dpa_room_id"])
        if k in res:
            continue
        bx = by_st.get(u["storey"], [])
        m = num_re.search(u["dpa_room_id"])
        cands: List[Dict[str, Any]] = []
        how = ""
        if m:
            cands = [b for b in bx if m.group(1) in b["name"] and _word_overlap(u["room_name"], re.sub(num_re, "", b["name"]))]
            if cands:
                best_ = min(PRIO.get(b["issue"], 3) for b in cands)
                cands = [b for b in cands if PRIO.get(b["issue"], 3) == best_]
            how = "room number printed in the box"
        if not cands:
            fit = [b for b in bx if name_fit(u["room_name"], b["name"]) >= 0.95]
            if not fit:
                reasons[k].append("no room box of an air-side schematic on this storey has this room type")
                continue
            best = min(PRIO.get(b["issue"], 3) for b in fit)
            cands = [b for b in fit if PRIO.get(b["issue"], 3) == best]
            n_s = sum(b["n"] for b in cands)
            n_d = sum(1 for x in univ if x["storey"] == u["storey"] and name_fit(x["room_name"], cands[0]["name"]) >= 0.95)
            if n_s != n_d:
                reasons[k].append(f"schematic typical count {n_s} against {n_d} rooms of this type on the storey")
                continue
            how = "room type and typical count"
        sets = {b["tags"] for b in cands}
        if len(sets) != 1:
            reasons[k].append("room type printed on boxes of different units: count per unit known, identity not")
            continue
        b = min(cands, key=lambda x: (PRIO.get(x["issue"], 3), x["sheet"]))
        grp = _is_group(b["tags"])
        res[k] = dict(unit=("AHU group: " + b["tags"]) if grp else b["tags"], step="3", conf="medium", code="D" if how.startswith("room number") else "A",
                      ref=f"air-side schematic {b['sheet']} ({b['issue']}), box '{b['name']}'; link by {how}", group=grp,
                      note="room printed on several units of one pod: AHU group, total once, compare with the group capacity without the common standby unit" if grp else "")


def step3_scoped(univ, res, reasons, boxes) -> None:
    """Second pass of step 3 (code A): only the boxes of units that already serve the department on the storey (steps 0 to 3) are read, and the
    room count of the department is compared with the typical count. A room type printed on the boxes of other departments' units does not block the link."""
    by_st: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for b in boxes:
        by_st[b["storey"]].append(b)
    dept_units: Dict[Tuple[str, str], set] = defaultdict(set)
    for u in univ:
        r = res.get((u["dept"], u["dpa_room_id"]))
        if r and r["step"] in ("0a", "0b", "1", "2", "3"):
            for t in r["unit"].replace("AHU group: ", "").split("; "):
                dept_units[(u["dept"], u["storey"])].add(zkey(t))
    for u in univ:
        k = (u["dept"], u["dpa_room_id"])
        if k in res:
            continue
        mine = dept_units.get((u["dept"], u["storey"]))
        if not mine:
            continue
        fit = [b for b in by_st.get(u["storey"], []) if name_fit(u["room_name"], b["name"]) >= 0.85 and {zkey(t) for t in b["tags"].split("; ")} & mine]
        if not fit:
            continue
        best = min(PRIO.get(b["issue"], 3) for b in fit)
        c = [b for b in fit if PRIO.get(b["issue"], 3) == best]
        sets = {b["tags"] for b in c}
        n_s = sum(b["n"] for b in c)
        n_d = sum(1 for x in univ if x["dept"] == u["dept"] and x["storey"] == u["storey"] and name_fit(x["room_name"], c[0]["name"]) >= 0.85)
        if len(sets) != 1:
            reasons[k].append("room type printed on boxes of several units that serve the department: count per unit known, identity not")
            continue
        if n_s != n_d:
            reasons[k].append(f"schematic typical count {n_s} against {n_d} rooms of the department")
            continue
        b = min(c, key=lambda x: (PRIO.get(x["issue"], 3), x["sheet"]))
        grp = _is_group(b["tags"])
        res[k] = dict(unit=("AHU group: " + b["tags"]) if grp else b["tags"], step="3", conf="low", code="A", group=grp,
                      ref=f"air-side schematic {b['sheet']} ({b['issue']}), box '{b['name']}'; link by room type and typical count, boxes of units that serve the department only",
                      note="room printed on several units of one pod: AHU group" if grp else "")


def step4_composite(univ, res, reasons, boxes) -> None:
    """A room type named inside a composite schematic row ('CIRCULATION / BAY- EXIT / SCRUB UP') takes the unit of that row (code A)."""
    by_st: Dict[str, List[Tuple[str, Dict[str, Any]]]] = defaultdict(list)
    for b in boxes:
        parts = [p.strip() for p in re.split(r"\s/\s|\s&\s|,", b["name"]) if p.strip()]
        if len(parts) > 1:
            for p in parts:
                by_st[b["storey"]].append((p, b))
    for u in univ:
        k = (u["dept"], u["dpa_room_id"])
        if k in res:
            continue
        hit = [(p, b) for p, b in by_st.get(u["storey"], []) if name_fit(u["room_name"], p) >= 0.95]
        if not hit:
            continue
        sets = {b["tags"] for _, b in hit}
        if len(sets) != 1:
            reasons[k].append("room type named inside composite schematic rows of different units")
            continue
        b = min((x[1] for x in hit), key=lambda x: (PRIO.get(x["issue"], 3), x["sheet"]))
        grp = _is_group(b["tags"])
        res[k] = dict(unit=("AHU group: " + b["tags"]) if grp else b["tags"], step="4", conf="low", code="A", group=grp,
                      ref=f"air-side schematic {b['sheet']} ({b['issue']}), composite box '{b['name']}' names '{hit[0][0]}'")


def step4_child(univ, res, reasons) -> None:
    for _ in range(2):
        parents: Dict[Tuple[str, str, str], List[str]] = defaultdict(list)
        for u in univ:
            if u["parent_group"] and u["parent_child"].lower().startswith("parent"):
                k = (u["dept"], u["dpa_room_id"])
                if k in res:
                    parents[(u["dept"], u["storey"], _norm(u["parent_group"]))].append(res[k]["unit"])
        for u in univ:
            k = (u["dept"], u["dpa_room_id"])
            if k in res or not u["parent_child"].lower().startswith("child") or not u["parent_group"]:
                continue
            units = set(parents.get((u["dept"], u["storey"], _norm(u["parent_group"])), []))
            if len(units) == 1:
                res[k] = dict(unit=next(iter(units)), step="4", conf="low", code="A", ref=f"DPA child row of parent group '{u['parent_group']}' on the same storey")
            elif len(units) > 1:
                reasons[k].append("parent rooms of the group are on different units")


def step5(univ, res, reasons) -> None:
    groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for u in univ:
        if u["functional_group"]:
            groups[(u["dept"], u["storey"], u["functional_group"])].append(u)
    for (dept, st, fg), rooms in groups.items():
        linked = [u for u in rooms if (u["dept"], u["dpa_room_id"]) in res]
        units = {res[(u["dept"], u["dpa_room_id"])]["unit"] for u in linked}
        if len(linked) >= 3 and len(linked) * 2 >= len(rooms) and len(units) == 1:
            for u in rooms:
                k = (u["dept"], u["dpa_room_id"])
                if k not in res:
                    res[k] = dict(unit=next(iter(units)), step="5", conf="low", code="A",
                                  ref=f"functional group '{fg}' on {st}: {len(linked)} of {len(rooms)} rooms linked, all on this unit")


# --------------------------------------------------------------------------- #
# common areas
# --------------------------------------------------------------------------- #
COMMON_DEPT = re.compile(r"common|core|mep|plant|retail|food court", re.I)


def common_rows(zone_all: Dict[str, Dict[str, Dict[str, str]]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    from .workbook.common_areas import assemble_common_areas
    for r in assemble_common_areas(ROOT, with_others=False)["rooms"]:
        z = (r.get("prov") or {}).get("zoning") or ["", ""]
        unit = r.get("ahu") or ""
        out.append(dict(dept="00", dpa_room_id=r["number"], room_name=r["name"], storey=r["storey"], unit_class=unit, unit_kind="AHU" if unit else "",
                        basis=STEP_BASIS["6"] if unit else "", step="6" if unit else "", source_ref=f"App D space; zoning source {z[1]}" if unit else "",
                        confidence="medium" if unit else "", code="D" if unit else "",
                        reason="" if unit else "App D gives no unit for the space (exhaust fan or no equipment drawn on the space)"))
    # D-044: plan rooms of LB3 to L01 outside the departments
    for st in ("LB3", "B3", "B2", "B1", "L01"):
        for pid, z in zone_all.get(st, {}).items():
            if not COMMON_DEPT.search(z.get("department", "")):
                continue
            kind = z.get("class_kind", "")
            cls = z.get("class", "")
            unit = cls if kind in ("AHU", "FCU", "CRAC", "PAHU") or "FCU" in cls else ""
            is_cls = (not unit) and cls.strip().upper() in ("MV", "NV")
            out.append(dict(dept="00", dpa_room_id=f"CA-{pid}", room_name=z.get("room_name", ""), storey=st, unit_class=unit if unit else cls,
                            unit_kind=kind if unit else ("class" if is_cls else ""),
                            basis=STEP_BASIS["7"] if unit else ("plan mode label (class, no unit)" if is_cls else ""), step="7" if unit else "",
                            source_ref=f"plan room {pid}; zone_assignment_{st}.csv: {z.get('method', '')}" if (unit or is_cls) else "",
                            confidence=z.get("confidence", ""), code="D" if (unit or is_cls) else "",
                            reason="" if unit else f"plan class '{cls}' is not a unit (mode label or no information)"))
    return out


# --------------------------------------------------------------------------- #
def run(refresh: bool = False) -> Dict[str, Any]:
    univ = load_universe(refresh)
    zone_all: Dict[str, Dict[str, Dict[str, str]]] = {}
    for f in sorted(glob.glob(str(REG / "zone_assignment_*.csv"))):
        zone_all[Path(f).stem.replace("zone_assignment_", "")] = {r["room_id"]: r for r in rd(Path(f))}
    zone_rows = {i: r for v in zone_all.values() for i, r in v.items()}
    res: Dict[Tuple[str, str], Dict[str, Any]] = {}
    reasons: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    boxes = schematic_boxes()
    step0(univ, zone_rows, res)
    step1(univ, res, reasons)
    step2(univ, res)
    step3(univ, res, reasons, boxes)
    step3_scoped(univ, res, reasons, boxes)
    step4_composite(univ, res, reasons, boxes)
    step4_child(univ, res, reasons)
    step5(univ, res, reasons)
    rows: List[Dict[str, Any]] = []
    unknown: List[Dict[str, Any]] = []
    for u in univ:
        k = (u["dept"], u["dpa_room_id"])
        r = res.get(k)
        base = dict(dept=u["dept"], dpa_room_id=u["dpa_room_id"], room_name=u["room_name"], storey=u["storey"])
        if r:
            unit = r["unit"]
            note = r.get("note", "")
            if r["step"] in ("1", "3", "4", "5") and u["prior_unit"] and zkey(u["prior_unit"]) != zkey(unit.replace("AHU group: ", "")):
                note = (note + "; " if note else "") + f"plan or earlier link gives {u['prior_unit']} (difference listed, not applied; D-052)"
            rows.append({**base, "unit_class": unit, "unit_kind": "AHU group" if r.get("group") else ("FCU" if unit.startswith("FCU") else "AHU"),
                         "basis": STEP_BASIS.get(r["step"], r["step"]), "step": r["step"], "source_ref": r["ref"], "confidence": r["conf"], "code": r["code"],
                         "other_source_unit": r.get("other", ""), "note": note})
        else:
            why = reasons.get(k) or []
            if u["prior_basis"] == "no link: area not plausible":
                why = ["plan link refused by the area test (OI-R302-1)"] + why
            reason = "; ".join(dict.fromkeys(why)) or "no plan room, booklet label or schematic box of this room type"
            rows.append({**base, "unit_class": "no information", "unit_kind": "", "basis": "", "step": "", "source_ref": "", "confidence": "", "code": "", "note": reason})
            unknown.append({**base, "reason": reason})
    for c in common_rows(zone_all):
        reason = c.pop("reason", "")
        rows.append({**c, "unit_class": c["unit_class"] if (c["step"] or c["unit_kind"] == "class") else "no information", "note": reason})
        if not c["step"] and c["unit_kind"] != "class":
            unknown.append(dict(dept="00", dpa_room_id=c["dpa_room_id"], room_name=c["room_name"], storey=c["storey"], reason=reason))
    wr(OUT, COLS, rows)
    wr(UNKNOWN, ["dept", "dpa_room_id", "room_name", "storey", "reason"], unknown)
    summ = summarise(rows, unknown)
    return summ


def _bucket(reason: str) -> str:
    r = reason.lower()
    for key, name in (("area test", "plan link refused by the area test"), ("count per unit", "identical rooms on several units (count known, identity not)"),
                      ("typical count", "schematic typical count differs from the room count"), ("typical room printed once", "booklet prints fewer labels than rooms"),
                      ("no tag", "booklet zone has no AHU tag"), ("several tags", "booklet zone carries several tags"),
                      ("no room box", "no schematic box of this room type"), ("no room label", "no booklet label fits the room name"),
                      ("app d gives no unit", "App D gives no unit"), ("not a unit", "plan class is not a unit (mode label)")):
        if key in r:
            return name
    return "no plan room, booklet label or schematic box of this room type"


def summarise(rows, unknown) -> Dict[str, Any]:
    by_basis = Counter(r["basis"] or "no unit" for r in rows)
    by_dept: Dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        by_dept[r["dept"]]["rooms"] += 1
        by_dept[r["dept"]]["with unit" if r["step"] else "no unit"] += 1
    reasons = Counter(_bucket(u["reason"]) for u in unknown)
    summ = dict(rooms=len(rows), with_unit=sum(1 for r in rows if r["step"]), by_basis=dict(by_basis), unknown_by_reason=dict(reasons),
                by_dept={k: dict(v) for k, v in sorted(by_dept.items())})
    dr: Dict[str, Counter] = defaultdict(Counter)
    for u in unknown:
        dr[u["dept"]][_bucket(u["reason"])] += 1
    summ["unknown_by_dept_reason"] = {k: dict(v) for k, v in sorted(dr.items())}
    lines = ["# Room-to-unit links: summary", "", f"Rooms: {summ['rooms']}. With a unit: {summ['with_unit']}. No information: {summ['rooms'] - summ['with_unit']}.", "",
             "## By basis", ""] + [f"- {k}: {v}" for k, v in sorted(by_basis.items(), key=lambda x: -x[1])] + ["", "## No information, by reason", ""] + \
            [f"- {k}: {v}" for k, v in reasons.most_common()] + ["", "## By department (00 = common areas)", ""] + \
            [f"- {k}: {v.get('rooms', 0)} rooms, {v.get('with unit', 0)} with a unit, {v.get('no unit', 0)} with no unit" for k, v in sorted(by_dept.items())] + \
            ["", "## No information, by department and reason", ""] + [f"- {k}: " + "; ".join(f"{b} {n}" for b, n in v.most_common()) for k, v in sorted(dr.items())]
    SUMMARY.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summ


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--refresh-universe", action="store_true", help="rebuild room_universe.csv from the department inputs")
    a = ap.parse_args(argv)
    s = run(a.refresh_universe)
    print(json.dumps(s, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
