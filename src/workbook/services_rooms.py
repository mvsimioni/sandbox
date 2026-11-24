"""Electrical rooms and plant rooms in the workbooks (D-095).

Every electrical, IT and plant room of the IP block on LB3 to L07 in the SOA with Revit data is a room of a workbook, under its DPA ID:

1. a DPA room already in a workbook is kept;
2. else a drawing space of the same room type on the same storey that the brief-ID step (D-093, D-094) took out becomes the DPA room
   (plan tags differ from the SOA names: 'Tfr' is 'Transformer', 'Ht csr' is 'HT Consumer Switchroom'); pairs by the nearest area;
3. else the DPA room is added to D00 with no outline, its DPA design area and the criteria of its room type, flagged.

Services: electrical plant rooms (HT, CSR, LV, transformer, UPS, battery, generator sync, electrical rooms) on N duty + 1 standby FCUs at
27 deg C or less (HFDS Vol 2 cl. 1.1.3.20, table T.1.1.1.1 transformer room); LAN rooms on N duty + 1 standby FCUs at 24 +/- 1 deg C
(cl. 1.1.3.19); AHU plant rooms on spill air (D-036); other plant rooms as the zoning, with the HFDS Vol 2 ventilation rate where it gives one.
Shafts, risers, ducts and tanks are not rooms for the load and are counted only. The register is out/qa/services_coverage.csv.
"""

from __future__ import annotations

import copy
import csv
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REG = Path("out/registers")
SCOPE = ["LB3", "B3", "B2", "B1", "L01", "L02", "L03", "L04", "L05", "L06", "L07"]
VOL2 = "HFDS_Vol2_MEP_deck_14Oct25.pdf"

# (key, pattern on the name, category)
KEYS: List[Tuple[str, str, str]] = [
    ("battery", r"\bbatt", "electrical"),
    ("ups", r"\bups\b", "electrical"),
    ("generator sync", r"generator", "electrical"),
    ("transformer", r"transformer|\btfr\b", "electrical"),
    ("ht", r"^(ht|hv)\b|consumer switch|\bcsr\b|^ht switchgear|^hv switch", "electrical"),
    ("lv", r"^lv\b|low voltage|^switch ?room$|^switchrm$|spg switch|lv switch", "electrical"),
    ("electrical room", r"^elec(trical)?( room)?$", "electrical"),
    ("elv", r"^elv$", "it"),
    ("lan", r"^lan( room)?$", "it"),
    ("mdf", r"^mdf$", "it"),
    ("it closet", r"^it closet$|^it$", "it"),
    ("server", r"server room$", "it"),
    ("ahu", r"^ahu( room| plant)?$", "plant"),
    ("se fan", r"^se fan|^fan room$", "plant"),
    ("fire pump", r"fire pump", "plant"),
    ("diesel pump", r"diesel pump", "plant"),
    ("ejector pump", r"ejector pump", "plant"),
    ("pump", r"^water pumps?$|^pump room$|balancing tank pump", "plant"),
    ("mg plant", r"^mg (compressor|vacuum) plant|^vacuum pump room$|agss compressor|^air compressor$|pillpick compressor", "plant"),
    ("heat plant", r"heat (recovery|pump) plant|^dhw plant$", "plant"),
    ("chiller plant", r"^chiller( plant( room)?)?$", "plant"),
    ("lift motor room", r"^lmr$|lift motor", "plant"),
]
CAT = {k: c for k, _, c in KEYS}
FCU_DS = "FCU duty/standby (tag to confirm)"


def key_of(name: str) -> Optional[str]:
    n = re.sub(r"\s+", " ", (name or "").strip().lower())
    if not n or " / " in n:
        return None
    for k, pat, _ in KEYS:
        if re.search(pat, n):
            return k
    return None


def storey_of(x: Dict[str, str]) -> Tuple[str, str]:
    """Storey of an SOA row and how it is known: the Revit level, else the storey in the IP room number (level blank or in another block)."""
    if x.get("storey"):
        return x["storey"], "Revit level"
    m = re.match(r"^(LB3|B[123]|0[1-7])\.IP\.", x.get("revit_room_number") or "")
    if m:
        s = m.group(1)
        return (s if s.startswith(("B", "LB")) else "L" + s), f"IP room number {x['revit_room_number']} (Revit level '{x.get('revit_level') or 'blank'}')"
    return "", ""


def inventory(root: Path = Path(".")) -> List[Dict[str, Any]]:
    with (root / REG / "dpa_rooms.csv").open(newline="", encoding="utf-8") as fh:
        dpa = {r["unique_room_identifier"]: r for r in csv.DictReader(fh)}
    out, seen = [], set()
    with (root / REG / "soa_revit.csv").open(newline="", encoding="utf-8") as fh:
        for x in csv.DictReader(fh):
            rid = x["unique_room_identifier"]
            if rid.endswith("..") or rid not in dpa:
                continue
            k = key_of(x["room_name"])                      # the room type is the brief's; the Revit tag of the SOA can be another room's
            if not k or rid in seen:
                continue
            seen.add(rid)
            st, how = storey_of(x)
            if st not in SCOPE:
                continue
            lv = (x.get("revit_level") or "").upper()
            try:
                area = float(dpa[rid]["designed_area_m2"] or 0)
            except ValueError:
                area = 0.0
            out.append(dict(id=rid, key=k, tag_key=key_of(x["revit_tag"]), category=CAT[k], storey=st, storey_basis=how, area=area, soa=x, dpa=dpa[rid],
                            level_note=("SOA Revit level in the OP block" if lv.endswith("(OP)") else "SOA Revit level is a mezzanine" if " MZ " in lv else "")))
    return out


def _area(r: Dict[str, Any]) -> float:
    for k in ("dpa_area_m2", "soa_area_m2", "measured_area_m2"):
        v = r.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return 0.0


def _plan_tags(root: Path) -> Dict[str, str]:
    out = {}
    for st in SCOPE:
        f = root / REG / f"zone_assignment_{st}.csv"
        if f.exists():
            with f.open(newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    out[r["room_id"]] = r["room_name"]
    return out


def _hfds_criteria(d: Dict[str, Any], name: str, root: Path) -> Optional[Dict[str, Any]]:
    from . import hfds
    hit = hfds.find_row(root, name)
    if not hit:
        return None
    row, kind, why = hit
    from .assemble import _norm
    sid = f"HFDS-{_norm(row['table_id'])}-{_norm(row['area_name'])}"[:60]
    if not any(s["id"] == sid for s in d["sources"]):
        d["sources"].append(dict(id=sid, **hfds.source_fields(row)))
    return hfds.to_criteria(row, sid, kind, why)


def _vol2_src(d: Dict[str, Any], clause: str, page: str, text: str) -> str:
    sid = "HFDS2-" + re.sub(r"[^0-9]", "", clause)
    if not any(s["id"] == sid for s in d["sources"]):
        d["sources"].append(dict(id=sid, document=VOL2, issue="14 Oct 25", page=page, location=f"clause {clause}", text_as_read=text,
                                 method="Text read from the HFDS Vol 2 MEP Standardization Report", confidence="high"))
    return sid


VOL2_TEXT = {
    "electrical": ("1.1.3.20", "page 88 (PDF page 89)", "All Electrical Plant Rooms e.g. Consumer Switch Room (CSR), HT room, UPS Room and Batter room shall be provided with "
                   "ducted fan coil units (FCUs) in N Duty + 1 Stand-by configuration with fan coil units located outside the room maintaining room temperature at "
                   "equal or less than 27 Deg C."),
    "battery": ("1.1.3.20 b", "page 88 (PDF page 89)", "For battery room, hydrogen detectors and dedicated exhaust air system shall be provided as per fire code requirements. "
                "Exhaust fan to activate upon detection of hydrogen gas inside the room."),
    "lan": ("1.1.3.19", "page 88 (PDF page 89)", "All LAN Rooms shall be provided with ducted fan coil units (FCUs) in N Duty + 1 Stand-by configuration with fan coil units "
            "located outside the room maintaining room temperature at 24 +/- 1 Deg C."),
    "mg plant": ("1.1.4.4", "page 91 (PDF page 92)", "The medical gas plant rooms shall be designed for mechanical ventilation comprising of supply and exhaust system with "
                 "minimum 8 ach as per ASHRAE requirements, or complying to local code, whichever is higher."),
    "lift motor room": ("1.1.4.6", "page 91 (PDF page 92)", "The lift machine room for service lifts shall be designed for mechanical ventilation at 15 ACH."),
}


def _apply_service(d: Dict[str, Any], r: Dict[str, Any], key: str, root: Path) -> List[str]:
    """Unit and criteria of a services room (D-095). Returns notes."""
    notes = []
    cat = CAT[key]
    if not r.get("criteria"):
        crit = _hfds_criteria(d, r.get("name") or key, root) or _hfds_criteria(d, key, root)
        if crit:
            r["criteria"] = crit
            r["prov"]["criteria"] = [crit.get("code", "A"), crit["source"]]
            notes.append(f"criteria {crit['criteria_row']}")
    if cat == "electrical" or key == "lan":
        t = VOL2_TEXT["electrical" if cat == "electrical" else "lan"]
        sid = _vol2_src(d, *t)
        if r.get("ahu") and not str(r["ahu"]).startswith("FCU"):
            r.setdefault("gaps", {})["unit"] = (f"HFDS Vol 2 cl. {t[0]} asks for ducted FCUs in N duty + 1 standby for this room; the sources give {r['ahu']}. "
                                                "The unit of the sources is kept. Confirm (D-095).")
            notes.append(f"unit {r['ahu']} kept (HFDS Vol 2 asks for FCUs)")
        else:
            r["ahu"] = None
            r["system_type"] = "FCU"
            r["terminal_tag"] = r.get("terminal_tag") or FCU_DS
            r["prov"]["zoning"] = ["D", sid] if not r["prov"].get("zoning") else r["prov"]["zoning"]
            r.setdefault("gaps", {})["unit"] = (f"Ducted FCUs in N duty + 1 standby outside the room (HFDS Vol 2 cl. {t[0]}). The FCU tags of this room are not in "
                                                "the Equipment Schedule by room. Give the tags (D-095).")
            notes.append("FCU N duty + 1 standby")
        if key == "battery":
            bs = _vol2_src(d, *VOL2_TEXT["battery"])
            r["gaps"]["exhaust"] = "Hydrogen detection and a dedicated exhaust fan started on hydrogen detection (HFDS Vol 2 cl. 1.1.3.20 b). Give the exhaust fan tag and rate."
            r["prov"]["exhaust_note"] = ["D", bs]
            notes.append("hydrogen exhaust")
    elif key in ("it closet", "mdf", "server") and not r.get("ahu") and r.get("system_type") in (None, "MV", "NO INFORMATION"):
        r["system_type"] = "FCU"
        r["terminal_tag"] = r.get("terminal_tag") or "FCU (tag to confirm)"
        r.setdefault("gaps", {})["unit"] = "IT room on a fan coil unit (D-037). The FCU tag of this room is not in the Equipment Schedule by room. Give the tag."
        notes.append("FCU (D-037)")
    elif key == "elv":
        r.setdefault("gaps", {})["system"] = ("ELV room or riser: HFDS Vol 2 gives no ACMV row for it; the zoning gives "
                                              f"{r.get('ahu') or r.get('system_type') or 'no unit'}. Give the system (D-095).")
        notes.append(f"ELV, {r.get('ahu') or r.get('system_type') or 'no unit'} (open)")
    elif key == "ahu":
        r["system_type"] = r.get("system_type") or "OTHER"
        if not r.get("ahu"):
            r.setdefault("gaps", {}).setdefault("spill", "AHU plant room: spill air of the AHUs of its plant level conditions the room (D-036). Confirm.")
        notes.append("AHU spill (D-036)")
    elif key in ("mg plant", "lift motor room"):
        t = VOL2_TEXT[key]
        sid = _vol2_src(d, *t)
        ach = 8 if key == "mg plant" else 15
        r.setdefault("gaps", {})["ventilation"] = (f"Mechanical supply and exhaust ventilation at {ach} air changes per hour minimum (HFDS Vol 2 cl. {t[0]}). "
                                                   "The rate is not a cooling load of this workbook. Give the fan tags.")
        r["prov"]["ventilation_note"] = ["D", sid]
        if not r.get("ahu") and not r.get("system_type"):
            r["system_type"] = "MV"
        notes.append(f"MV {ach} ACH")
    elif key == "chiller plant":
        r.setdefault("gaps", {})["system"] = ("HFDS table T.1.1.1.1 gives the chiller plant room 27 +/- 2 deg C, 24 h, MERV 8; the zoning gives "
                                              f"{r.get('ahu') or r.get('system_type') or 'no unit'}. Which governs is not decided (D-095).")
        notes.append("chiller plant: HFDS 27 deg C against the zoning, open")
    else:
        if not r.get("ahu") and not r.get("system_type"):
            r["system_type"] = "MV"
            r.setdefault("gaps", {})["system"] = "Plant room: mechanical ventilation assumed (no unit in the zoning). HFDS Vol 2 gives no rate for this room type. Give the system."
        notes.append(f"plant room, {r.get('ahu') or r.get('system_type')}")
    return notes


def services(datasets: Dict[str, Dict[str, Any]], removed_rooms: List[Tuple[str, Dict[str, Any]]], root: Path = Path(".")) -> Dict[str, Any]:
    from .dpa_ids import _dpa_src, _renumber
    inv = inventory(root)
    in_wb = {r["number"]: (n, r) for n, d in datasets.items() for r in d["rooms"]}
    tags = _plan_tags(root)
    # drawing spaces taken out by the brief-ID step, by storey and room type
    spaces = defaultdict(list)
    for n, r in removed_rooms:
        k = key_of(r.get("name") or "") or key_of(tags.get(r.get("plan_room_id") or "", ""))
        if not k and re.match(r"^(PLAN-)?[A-Z0-9]+-[RU]\d+$", r["number"]):
            k = key_of(tags.get(r["number"].replace("PLAN-", ""), ""))
        if k:
            spaces[(r["storey"], k)].append((n, r))
    want = defaultdict(list)
    for x in inv:
        if x["id"] not in in_wb:
            want[(x["storey"], x["key"])].append(x)
    rows, restored, added = [], [], []
    used = set()
    # pairing on each storey: (1) same room type by the DPA name, area within 60 %; (2) the SOA Revit tag of the DPA room gives the type of the
    # space, area within 60 %; (3) one DPA room and one space of the type left on the storey, whatever the areas (flagged)
    def score(x, r):
        a, b = x["area"], _area(r)
        return abs(a - b) / max(a, b) if a > 0 and b > 0 else 9.0
    pairs_all = []
    for pass_ in (1, 2, 3):
        for (st, k), xs in sorted(want.items()):
            xs = [x for x in xs if x["id"] not in in_wb and x["id"] not in {p[0]["id"] for p in pairs_all}]
            if pass_ == 2:
                sp = [s_ for kk, lst in spaces.items() if kk[0] == st for s_ in lst if id(s_[1]) not in used]
            else:
                sp = [s_ for s_ in spaces.get((st, k), []) if id(s_[1]) not in used]
            if not xs or not sp:
                continue
            if pass_ == 3:
                if len(xs) == 1 and len(sp) == 1:
                    used.add(id(sp[0][1]))
                    pairs_all.append((xs[0], sp[0], score(xs[0], sp[0][1]), "only room of the type on the storey, areas differ"))
                continue
            cand = []
            for i, x in enumerate(xs):
                for j, (n, r) in enumerate(sp):
                    if pass_ == 2 and not (x["tag_key"] and x["tag_key"] == (key_of(r.get("name") or "") or key_of(tags.get(r.get("plan_room_id") or "", "")))):
                        continue
                    sc = score(x, r)
                    if sc <= 0.6 or (sc == 9.0 and pass_ == 1):
                        cand.append((sc, i, j))
            ti, tj = set(), set()
            for sc, i, j in sorted(cand):
                if i in ti or j in tj:
                    continue
                ti.add(i)
                tj.add(j)
                used.add(id(sp[j][1]))
                pairs_all.append((xs[i], sp[j], sc, "room type of the DPA name" if pass_ == 1 else f"SOA Revit tag '{xs[i]['soa']['revit_tag']}'"))
    for gk in [None]:
        pairs = [(x, s_, sc) for x, s_, sc, _ in pairs_all]
        basis = {x["id"]: b for x, _, _, b in pairs_all}
        for x, (n, r), score in pairs:
            used.add(id(r))
            d = datasets[n]
            old, oname = r["number"], r.get("name")
            sid = _dpa_src(d, x["dpa"])
            _renumber(d, r, x["id"])
            r["name"] = x["dpa"]["room_name"]
            r["dpa_area_m2"] = x["area"] or None
            r["soa_area_m2"] = None
            r["area_selector"] = None
            r["prov"]["identity"] = ["D", sid]
            if x["area"]:
                r["prov"]["dpa_area"] = ["D", sid]
            r["prov"].pop("soa_area", None)
            r["prov"].pop("area_selector", None)
            n_alt = len(want[(x["storey"], x["key"])])
            how = (f"{basis[x['id']]}: room type '{x['key']}' on {x['storey']}" + (f", area {x['area']:g} m² against {_area(r):g} m²" if score != 9.0 else ", area not known on one side")
                   + (f"; one of {n_alt} rooms of this type, the pairing among them is by area only" if n_alt > 1 else ""))
            r["remark"] = (f"DPA room {x['id']} '{r['name']}' ({x['dpa']['fpu_department_name']}). Drawing space {old} '{oname}' matched by {how} (D-095). "
                           + (r.get("remark") or ""))
            r.setdefault("gaps", {})["identity"] = f"Room matched to the brief by {how} from drawing space {old} '{oname}'. Confirm (D-095)."
            if x["level_note"]:
                r["gaps"]["storey"] = f"{x['level_note']}; storey from the {x['storey_basis']}. Confirm (D-095)."
            r["soa_revit"] = dict(number=x["soa"]["revit_room_number"], tag=x["soa"]["revit_tag"], temperature=x["soa"]["acmv_temperature"],
                                  pressure=x["soa"]["acmv_pressure"], humidity=x["soa"]["acmv_humidity"], recirculation=x["soa"]["acmv_recirculation"],
                                  exhaust=x["soa"]["acmv_exhaust"], hepa=x["soa"]["acmv_hepa"])
            d["rooms"].append(r)
            in_wb[x["id"]] = (n, r)
            restored.append(dict(id=x["id"], space=old, space_name=oname, workbook=f"D{n}", how=how))
    # DPA services rooms with no drawing space: added to D00 with no outline
    d0 = datasets.get("00")
    tmpl_by_key = {}
    for x in inv:
        if x["id"] in in_wb:
            tmpl_by_key.setdefault(x["key"], in_wb[x["id"]][1])
    generic = next((r for r in d0["rooms"] if r.get("criteria") and r.get("lighting_w_m2") is not None), None) if d0 else None
    for gk, xs in want.items():
        for x in xs:
            if x["id"] in in_wb or d0 is None:
                continue
            sid = _dpa_src(d0, x["dpa"])
            t = tmpl_by_key.get(x["key"]) or generic or {}
            room = dict(number=x["id"], name=x["dpa"]["room_name"], storey=x["storey"], ahu=None, zone_tag=None, system_type=None, terminal_tag=None,
                        exhaust_fan=None, space_type="UNCLASSIFIED", room_condition="Comfort", dpa_area_m2=x["area"] or None, soa_area_m2=None,
                        measured_area_m2=None, area_selector=None, ceiling_height_m=t.get("ceiling_height_m"), occupancy_people=None,
                        occupancy_per_100m2=t.get("occupancy_per_100m2"), iof=None, activity=None, operation_hours=None,
                        lighting_w_m2=t.get("lighting_w_m2"), small_power_w_m2=t.get("small_power_w_m2"), gaps={}, criteria={}, envelope=[],
                        prov={"identity": ["D", sid], "space_type": ["A", "ASM-DEFAULT"]},
                        remark=(f"DPA room {x['id']} '{x['dpa']['room_name']}' ({x['dpa']['fpu_department_name']}), SOA room number {x['soa']['revit_room_number']}, "
                                f"tag '{x['soa']['revit_tag']}'. No drawing space of this room type on {x['storey']} is free to match: the room is added with no "
                                "outline (D-095)."))
            if x["area"]:
                room["prov"]["dpa_area"] = ["D", sid]
            for k_, pk in (("ceiling_height_m", "ceiling_height"), ("occupancy_per_100m2", "occupancy_density"), ("lighting_w_m2", "lighting"), ("small_power_w_m2", "small_power")):
                if room[k_] is not None:
                    v = (t.get("prov") or {}).get(pk)
                    room["prov"][pk] = ["A", v[1]] if isinstance(v, (list, tuple)) and len(v) == 2 else ["A", "ASM-CEILING" if pk == "ceiling_height" else "ASM-DEFAULT"]
            for k_ in ("occupancy_basis", "occupancy_use", "load_basis", "load_use"):
                if t.get(k_) is not None:
                    room[k_] = t[k_]
            if t:
                from .capture import copy_sources
                for n_, (dn, _) in ((n_, v) for n_, v in in_wb.items() if v[1] is t):
                    copy_sources(datasets[dn], d0, t.get("prov"))
            room["gaps"]["outline"] = (f"The room is in the DPA and the SOA ({x['soa']['revit_room_number']}, storey from the {x['storey_basis']}) but no drawing "
                                       "space of this room type is free on the plan: no outline, no measured area, no facade. Give the position (D-095).")
            if x["level_note"]:
                room["gaps"]["storey"] = f"{x['level_note']}; storey from the {x['storey_basis']}. Confirm (D-095)."
            if not x["area"]:
                room["gaps"]["area"] = "The DPA gives no design area for this room. Give the area."
            room["soa_revit"] = dict(number=x["soa"]["revit_room_number"], tag=x["soa"]["revit_tag"], temperature=x["soa"]["acmv_temperature"],
                                     pressure=x["soa"]["acmv_pressure"], humidity=x["soa"]["acmv_humidity"], recirculation=x["soa"]["acmv_recirculation"],
                                     exhaust=x["soa"]["acmv_exhaust"], hepa=x["soa"]["acmv_hepa"])
            d0["rooms"].append(room)
            if x["storey"] not in d0["storeys"]:
                d0["storeys"].append(x["storey"])
            d0.setdefault("flags", []).append(dict(type="VERIFY", ref=x["id"], source=sid,
                                                   text=f"Room {x['id']} ('{room['name']}') is in the DPA and SOA on {x['storey']} with no free drawing space: added with no outline (D-095)."))
            in_wb[x["id"]] = ("00", room)
            added.append(dict(id=x["id"], workbook="D00"))
    # unit and criteria of every services room in the workbooks
    how_of = {a["id"]: f"restored from drawing space {a['space']} ('{a['space_name']}')" for a in restored}
    how_of.update({a["id"]: "added with no outline" for a in added})
    for x in inv:
        n, r = in_wb[x["id"]]
        notes = _apply_service(datasets[n], r, x["key"], root)
        rows.append(dict(storey=x["storey"], dpa_id=x["id"], dpa_name=x["dpa"]["room_name"], soa_room_number=x["soa"]["revit_room_number"],
                         soa_tag=x["soa"]["revit_tag"], department=x["dpa"]["fpu_department_name"], room_type=x["key"], category=x["category"],
                         dpa_area_m2=x["area"] or "", workbook=f"D{n}", coverage=how_of.get(x["id"], "in the workbook"),
                         unit=r.get("ahu") or r.get("terminal_tag") or r.get("system_type") or "", outline="yes" if r.get("_geom") or r.get("plan_room_id") or r.get("appd_space") or r.get("appd_spaces") else "no",
                         criteria=(r.get("criteria") or {}).get("criteria_row", ""), notes="; ".join(notes + ([x["level_note"]] if x["level_note"] else []))))
    # drawing spaces of a services type that stay out (no DPA room of the type free on the storey)
    left = [dict(storey=r["storey"], space=r["number"], name=r.get("name"), workbook=f"D{n}", area_m2=round(_area(r), 1), room_type=k)
            for (st, k), lst in spaces.items() for n, r in lst if id(r) not in used]
    out = root / "out/qa/services_coverage.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    rows.sort(key=lambda z: (SCOPE.index(z["storey"]), z["category"], z["room_type"], z["dpa_id"]))
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    if left:
        with (root / "out/qa/services_spaces_without_dpa_room.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(left[0]))
            w.writeheader()
            w.writerows(left)
    return dict(inventory=len(inv), restored=restored, added=added, spaces_left=left)
