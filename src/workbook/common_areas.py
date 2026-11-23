"""Common-areas workbook (D-011): AIGH_C2_D00_Common_Areas_Thermal_Load_Calc_RevA.xlsx.

Rooms are the App D spaces of storeys 2 to 7 (area as printed on the drawing, else the measured outline; the DPA designed area is shown
beside it where a DPA room has the same name on the same storey). Systems are the App D units (AHU, PAHU, FCU) and fans, outdoor-air fans included.
Criteria (D-011): a space whose room type a booklet prints takes the most frequent booklet row of that type (assumption, to confirm); a circulation,
corridor or lobby space that no booklet row covers takes the default criteria (assumption, to confirm); any other space has no criteria and is listed.
The calculated airflow of each unit is shown beside the tender one (D-013).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import conf
from .common import ROOM_THERMAL_NOTE
from .assemble import (REG, _expand_abbreviations, _f, _norm, _read, _tag_candidates, _tender_for_tag, criteria_from_row, criteria_text, load_zone_assignments,
                       match_criteria, room_use, zone_key, zone_totals)
from .build import build_workbook
from .reva import build_reva_input

STOREYS = {"2": "L02", "3": "L03", "4": "L04", "5": "L05", "6": "L06", "7": "L07"}
NOISE = re.compile(r"^(FFL|SFL|PLL|BLL)\b.*|^[A-Z]$|^(AC|MV|NV)\b.*|^\?$", re.I)
UNIT_TYPES = ("AHU", "PAHU", "FCU")


def space_name(raw: str) -> str:
    toks = [t.strip() for t in (raw or "").split("|")]
    keep: List[str] = []
    for t in toks:
        if t and not NOISE.match(t) and t not in keep:
            keep.append(t)
    return " / ".join(keep) if keep else "Common space"


def unit_tags(field: str) -> List[str]:
    """Tags of a 'tag [ann n, d pt]; tag [...]' field, nearest first."""
    out: List[str] = []
    for part in (field or "").split(";"):
        m = re.match(r"\s*([A-Z]+-[A-Z0-9\-/&]+)", part)
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def assemble_common_areas(root: Path = Path(".")) -> Dict[str, Any]:
    root = Path(root)
    cfg = conf.config(root)
    circ = cfg.get("circulation_default_criteria") or {}
    densities = conf.occupancy_cfg(root).get("per_100m2") or {}
    people = conf.people(root)
    spaces = [s for s in _read(root / REG / "appd_spaces.csv") if s["storey"] in STOREYS]
    equip = [e for e in _read(root / REG / "appd_equipment.csv") if e["storey"] in STOREYS]
    recon = {_norm(r["appd_tag_normalised"]): r for r in _read(root / REG / "appd_schedule_reconciliation.csv")}
    ahu_sched = {_norm(r["tag_normalised"]): r for r in _read(root / REG / "ahu_schedule.csv")}
    fcu_sched = {_norm(r["tag_normalised"]): r for r in _read(root / REG / "equipment_schedule_air_handling.csv") if r.get("type") == "FCU"}
    fan_sched = {_norm(r["tag_normalised"]): r for r in _read(root / REG / "equipment_schedule_fans.csv")}
    sched_equip = _read(root / REG / "schematic_equipment.csv")
    sched_totals = _read(root / REG / "schematic_ahu_totals.csv")
    dpa = [r for r in _read(root / REG / "dpa_rooms.csv") if r.get("storey_from_identifier")]
    dpa_by: Dict[Any, List[Dict[str, str]]] = defaultdict(list)
    for r in dpa:
        dpa_by[(r["storey_from_identifier"], _norm(r["room_name"]))].append(r)
    base = build_reva_input(root)
    sources: List[Dict[str, Any]] = [s for s in base["sources"] if s["id"] in ("RA-RZ-TBL", "RA-BOD-C19", "RA-BOD-C28", "RA-BOD-C30", "RA-BOD-C31",
                                                                              "RA-AHU-B29", "RA-OCC", "RA-DD21")]
    ids = {s["id"] for s in sources}

    def add_src(sid: str, **kw: Any) -> str:
        if sid not in ids:
            sources.append(dict(id=sid, **kw))
            ids.add(sid)
        return sid

    add_src("ASM-DEFAULT", document="Workbook defaults", issue="", page="", location="space type UNCLASSIFIED",
            text_as_read="design room condition of the Rev A design basis (24 °C, 52.6 kJ/kg) applied where no space type is set", method="Assumption, to confirm", confidence="low")
    add_src("ASM-ACTIVITY", document="Workbook defaults", issue="", page="", location="activity of occupants",
            text_as_read="activity Seated (Rev A table) applied where occupancy is assumed", method="Assumption, to confirm", confidence="low")
    from . import conf as _conf
    ch_default = float((_conf.config().get("ceiling_height") or {}).get("flagged_default_m") or 0) or None
    if ch_default:
        add_src("ASM-CEILING", document="Workbook defaults", issue="", page="", location="ceiling height",
                text_as_read=f"ceiling height {ch_default:g} m applied where the room layout sheets give none", method="Assumption, to confirm", confidence="low")
    add_src("OCC-DENSITY", document="ASHRAE Standard 62.1, Table 6.2.2.1: default occupant density", issue="edition to confirm (health-care rows: 62.1-2007)",
            page="Table 6.2.2.1", location="persons per 100 m² by occupancy category", text_as_read="default densities per 100 m², as listed in the project design basis",
            method="Value taken from the project design basis", confidence="medium")
    csid = add_src("CIRC-DEFAULT", document="Project design basis: default criteria for circulation, corridor and lobby spaces", issue="22 Nov 2026",
                   page="circulation default", location="DD Booklet criteria tables",
                   text_as_read="most frequent row of the corridor, circulation and lobby rows of the DD Booklet criteria tables (45 rows)",
                   method="most frequent booklet row; to confirm", confidence="medium")
    crit = dict(criteria_row="Circulation default (assumed)", page="circulation default", min_oa_ach=_f(circ.get("min_oa_ach")),
                min_total_ach=_f(circ.get("min_total_ach")), temp_min_c=(circ.get("temp_c") or [None, None])[0], temp_max_c=(circ.get("temp_c") or [None, None])[1],
                rh_max_pct=_f(circ.get("rh_max_pct")), pressure_regime=str(circ.get("pressure", "neutral")).upper(),
                air_class=str(circ.get("air_class")) if circ.get("air_class") is not None else None, filter=circ.get("filter"), source=csid, code="A",
                remark="No booklet row covers the common areas: default for circulation, corridor and lobby spaces.")

    booklet = _read(root / REG / "criteria.csv")
    crit_cache: Dict[str, Any] = {}
    n_default = n_booklet = n_none = 0
    none_names: List[str] = []

    def criteria_for(name: str, number: str, is_ac: bool = False):
        """(criteria dict or None, kind) for a space: booklet row, circulation default, or none.
        An outline with several names is not one room type. Where its App D mode is air-conditioned it takes the D-011 default for circulation and common
        space, as a whole (code A); it is not split and no single name sets its criteria."""
        key = (name.lower(), is_ac)
        parts_ = [x.strip() for x in re.split(r"\s/\s", name) if x.strip()] or [name]
        if key not in crit_cache and is_ac and (len(parts_) >= 2 or name == "Common space"):
            c_ = dict(crit)
            if len(parts_) >= 2:
                c_["remark"] = (f"Outline with {len(parts_)} names ({name}), air-conditioned on App D. It is not split: the default for circulation and common space applies to "
                                "the whole outline. To confirm.")
            crit_cache[key] = (c_, "default")
        if key not in crit_cache and len(parts_) >= 2:
            crit_cache[key] = (None, "none")
        if key not in crit_cache:
            row, variants, how = None, [], ""
            parts = [x.strip() for x in re.split(r"\s/\s", name) if x.strip()] or [name]
            circ_part = None
            for part in parts:                       # a name of several parts ('Toilet / SAN'): the first part that a row or the rule fits
                for form in dict.fromkeys((part, _expand_abbreviations(part.lower()))):
                    row, variants, how = match_criteria(form, booklet, modal=True)
                    if row is not None and how != "same name":
                        row = None                   # only a room type with the same name is a booklet row for the space
                    if row is not None:
                        break
                if row is not None:
                    break
                if circ_part is None and room_use(part)[0] == "corridor_circulation":
                    circ_part = part
                    break
            if row is not None:
                sid = add_src(f"CRIT-ALL-{_norm(row['room_type_as_printed'])}"[:60], document=Path(row["source_file"]).name,
                              issue=f"{row.get('department_label', 'booklet')} and other booklets", page=f"page {row['booklet_page']}",
                              location=row.get("bbox", ""), text_as_read=criteria_text(row),
                              method="most frequent row of this room type in the booklet criteria tables", confidence=str(row.get("confidence", "")))
                c = criteria_from_row(row, sid, how, code="A",
                                      remark=f"No booklet covers the common areas. Room type '{row['room_type_as_printed']}' is printed in the booklets; "
                                             f"this is its most frequent row ({how})" + (f"; {len(variants)} other row(s) differ" if variants else "") + ". To confirm.")
                crit_cache[key] = (c, "booklet")
            elif circ_part is not None:
                crit_cache[key] = (dict(crit), "default")
            else:
                crit_cache[key] = (None, "none")
        return crit_cache[key]

    # unit of each space: the class of the zone schedule for the plan rooms inside the App D outline (never a tag that is drawn inside it)
    zone_rows = load_zone_assignments(root)
    class_of_space: Dict[Any, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for st_key in STOREYS.values():
        pr = _read(root / REG / f"room_polygons_{st_key}.csv") if (root / REG / f"room_polygons_{st_key}.csv").exists() else []
        for r in pr:
            za = zone_rows.get(st_key, {}).get(r["room_id"])
            if r.get("appd_annotation_index") and za is not None:
                class_of_space[(st_key, r["appd_annotation_index"])][(za.get("class") or "").strip() or "no information"] += _f(r.get("a_area_m2")) or 1.0
    type_of = {_norm(e["tag_normalised"]): e["type"] for e in equip}
    rooms: List[Dict[str, Any]] = []
    skipped: List[str] = []
    n_dpa = n_unit = 0
    for s in spaces:
        st = STOREYS[s["storey"]]
        printed = _f(s.get("printed_area_m2"))
        meas = _f(s.get("area_m2_at_1_250"))
        area = printed if printed else meas
        number = f"CA-{st}-{int(s['annotation_index']):03d}"
        if not area:
            skipped.append(number)
            continue
        name = space_name(s.get("space_names_text_layer", ""))
        sid = add_src(f"APPD-{st}-{s['annotation_index']}", document="Appendix D, ACMV for common areas (combined plans)", issue="as issued",
                      page=f"page {s['page']}, drawing {s['drawing_number']}", location=f"area annotation {s['annotation_index']}",
                      text_as_read=f"area label {s.get('area_label_text') or printed}; names {s.get('space_names_text_layer', '')}; outline {meas} m² at 1:250",
                      method="Area label and room outline read from the marked-up drawing", confidence="high" if s.get("printed_area_m2") else "medium")
        room: Dict[str, Any] = dict(number=number, name=name, storey=st, ahu=None, zone_tag=None, system_type=None, terminal_tag=None, exhaust_fan=None,
                                    space_type="UNCLASSIFIED", room_condition="Comfort", dpa_area_m2=None, soa_area_m2=printed if printed else None,
                                    measured_area_m2=meas, area_selector="SOA" if printed else None, ceiling_height_m=ch_default, occupancy_people=None,
                                    occupancy_per_100m2=None, iof=None, activity=None, operation_hours=None, lighting_w_m2=None, small_power_w_m2=None,
                                    gaps={}, criteria={}, envelope=[],
                                    prov={"identity": ["D", sid], "zoning": ["D", sid], "space_type": ["A", "ASM-DEFAULT"], "soa_area": ["D", sid],
                                          "measured_area": ["M", sid], "area_selector": ["S", "GEN-BASIS"],
                                          **({"ceiling_height": ["A", "ASM-CEILING"]} if ch_default else {})},
                                    remark=f"App D page {s['page']}: area label {printed:g} m²" if printed else f"App D page {s['page']}: outline {meas:g} m² at 1:250")
        if ch_default:
            room["remark"] += f"; ceiling height {ch_default:g} m assumed (no room layout sheet applies to a common area)"
        is_ac = (s.get("mode_label") or "").upper().startswith("AC")
        cc, kind = criteria_for(name, number, is_ac)
        if cc is not None:
            room["criteria"] = dict(cc)
            if kind == "default":
                n_default += 1
            else:
                n_booklet += 1
        else:
            n_none += 1
            none_names.append(name)
            room["gaps"]["criteria"] = ("No booklet room type fits this space and it is not a circulation, corridor or lobby space. "
                                        "No criteria are set: only its thermal load can set an airflow.")
        zc = class_of_space.get((st, s["annotation_index"]))
        if zc:
            cls, _a = max(zc.items(), key=lambda kv: kv[1])
            share = zc[cls] / (sum(zc.values()) or 1.0)
            zsid = add_src(f"ZONE-{st}-APPD{s['annotation_index']}", document=f"AHU zone schedule, storey {st}", issue="Rev A", page=st,
                           location=f"plan rooms inside the App D outline {s['annotation_index']}",
                           text_as_read="; ".join(f"class {k}: {v:.0f} m²" for k, v in sorted(zc.items(), key=lambda kv: -kv[1])),
                           method="Class of the plan rooms inside the outline in the zone schedule", confidence="medium")
            if re.match(r"^(AHU|PAHU|FCU)-", cls):
                room["ahu"] = cls
                room["system_type"] = "FCU" if cls.startswith("FCU") else None
                room["prov"]["zoning"] = ["D", zsid]
                room["remark"] += f"; unit {cls} from the zone schedule ({share * 100:.0f} % of the plan room area inside the outline)"
                if len(zc) > 1:
                    room["remark"] += f"; other classes inside the outline: {', '.join(k for k in zc if k != cls)}"
                    room["gaps"]["ahu"] = f"The plan rooms inside this outline have several classes: {', '.join(f'{k} ({v:.0f} m²)' for k, v in sorted(zc.items(), key=lambda kv: -kv[1]))}. The largest is used. Confirm."
                n_unit += 1
            else:
                room["remark"] += f"; zone schedule class {cls}: no air handling or fan coil unit serves the space"
                room["prov"]["zoning"] = ["D", zsid]
        else:
            room["gaps"]["ahu"] = "No plan room of the zone schedule lies inside this outline: no unit is set. A tag drawn inside the outline is not taken as the serving unit."
        m = dpa_by.get((st, _norm(name)))
        if m and len(m) == 1 and _f(m[0].get("designed_area_m2")):
            room["dpa_area_m2"] = _f(m[0]["designed_area_m2"])
            dsid = add_src(f"DPA-{_norm(m[0]['unique_room_identifier'])}", document=Path(m[0]["source_file"]).name, issue="DPA", page=f"row {m[0]['row']}",
                           location=f"room {m[0]['unique_room_identifier']}", text_as_read=f"designed {m[0]['designed_area_m2']} m²", method="Value read from the DPA workbook, sheet Database", confidence="high")
            room["prov"]["dpa_area"] = ["D", dsid]
            n_dpa += 1
        use, label = room_use(name)
        if cc is not None and kind == "default" and len(re.split(r"\s/\s", name)) >= 2:
            use = "corridor_circulation"
        if use is not None and use in densities:
            room["occupancy_per_100m2"] = float(densities[use])
            room["occupancy_basis"] = "DENSITY DEFAULT"
            room["occupancy_use"] = use.replace("_", " ")
            room["prov"]["occupancy_density"] = ["A", "OCC-DENSITY"]
            if densities[use]:
                room["activity"] = "Seated"
                room["prov"]["activity"] = ["A", "ASM-ACTIVITY"]
        else:
            room["occupancy_basis"] = "NOT SET"
            room["remark"] += "; occupancy not set"
            room["gaps"]["occupancy"] = "Occupancy is not set. No RDS applies to the common areas. No room use fits the space name for the default density."
        room["remark"] = room["remark"].rstrip(".") + ". " + ROOM_THERMAL_NOTE
        room["_space"] = s
        rooms.append(room)

    ztot = zone_totals(root)
    mot_sched = {_norm(r["tag"].replace("lSONP", "ISONP")): r for r in _read(root / REG / "mot_ahu_schedule.csv") if r.get("equipment_type") in ("AHU", "PAHU")}
    # units: every AHU, PAHU and FCU of App D, with the tender airflow
    ahus: List[Dict[str, Any]] = []
    on_rooms = {r["ahu"] for r in rooms if r["ahu"]}
    fans: List[Dict[str, Any]] = []
    for tag in dict.fromkeys(e["tag_normalised"] for e in equip):
        typ = type_of[_norm(tag)]
        if typ in UNIT_TYPES:
            t = _tender_for_tag(tag, ahu_sched, fcu_sched, sched_equip, sched_totals, add_src, mot_sched, root=root)
            a = dict(tag=tag, type=t.pop("type", typ))
            a.update(t)
            zt = ztot.get(zone_key(tag))
            if zt is not None:
                zs_ = add_src(f"ZUNIT-{_norm(zone_key(tag))}"[:60], document="AHU zone schedule, LB3 to L07", issue="Rev A", page="storeys " + ", ".join(zt["storeys"]),
                              location=f"unit {tag}", text_as_read=f"{zt['rooms']} zoned room(s), {zt['area']:.1f} m² measured on the tender plan",
                              method="Rooms and area of the unit in the zone schedule", confidence="medium")
                a.update(zone_rooms=zt["rooms"], zone_area_m2=round(zt["area"], 1), zone_code="D", zone_source=zs_)
            if tag not in on_rooms:
                a["list_without_rooms"] = True
            ahus.append(a)
        else:
            rr = recon.get(_norm(tag)) or {}
            fr = fan_sched.get(_norm(tag))
            lps = _f(rr.get("airflow")) if rr.get("found_in_schedule") == "y" else None
            if lps is None and fr is not None:
                lps = _f(fr.get("Min Airflow (l/s)"))
            ftype = {"OAF": "OUTDOOR AIR", "EAF": "EXHAUST", "TEAF": "EXHAUST"}.get(typ)
            if ftype is None:
                continue
            f = dict(tag=tag, type=ftype, serves=tag)
            if ftype == "OUTDOOR AIR":
                # the unit with the same level, area code and number serves the same space (OAF-B-02-02COR-01 and FCU-B-02-02COR-01)
                tail = tag.split("-", 1)[1] if "-" in tag else tag
                twin = [a["tag"] for a in ahus if a["tag"].split("-", 1)[-1] == tail]
                if twin:
                    f["serves"] = twin[0]
                    f["remark"] = f"serves {twin[0]} (same level, area and number)"
                else:
                    f["remark"] = "App D shows no unit with the same level, area and number; the unit it serves is not known, so the airflow is not calculated"
            if lps is not None:
                sid = add_src(f"EQS5-{_norm(tag)}"[:60], document="Equipment Schedule, fan schedule", issue="Corr 5", page=f"page {rr.get('schedule_pages', '')}",
                              location=f"tag {tag}", text_as_read=f"{tag}: airflow {lps:g} L/s", method="Value read from the Equipment Schedule table", confidence="high")
                f.update(scheduled_lps=lps, code="D", source=sid, tender_source_text=f"Equipment Schedule Corr 5, page {rr.get('schedule_pages', '')}, {tag}")
            else:
                f.update(code="D", source="CIRC-DEFAULT" if False else "ASM-DEFAULT")
            fans.append(f)
    # units that the zoning gives to the spaces and that App D does not print under this tag form: the same tender lookup and the zone schedule totals as for the others
    have_tags = {a["tag"] for a in ahus}
    for tag in sorted(on_rooms - have_tags):
        t = _tender_for_tag(tag, ahu_sched, fcu_sched, sched_equip, sched_totals, add_src, mot_sched, root=root)
        a = dict(tag=tag, type=t.pop("type", re.match(r"^([A-Z]+)", tag).group(1) if re.match(r"^([A-Z]+)", tag) else "AHU"))
        a.update(t)
        zt = ztot.get(zone_key(tag))
        if zt is not None:
            zs_ = add_src(f"ZUNIT-{_norm(zone_key(tag))}"[:60], document="AHU zone schedule, LB3 to L07", issue="Rev A", page="storeys " + ", ".join(zt["storeys"]),
                          location=f"unit {tag}", text_as_read=f"{zt['rooms']} zoned room(s), {zt['area']:.1f} m² measured on the tender plan",
                          method="Rooms and area of the unit in the zone schedule", confidence="medium")
            a.update(zone_rooms=zt["rooms"], zone_area_m2=round(zt["area"], 1), zone_code="D", zone_source=zs_)
        ahus.append(a)
    # exhaust fan of a space: the nearest exhaust fan marked in it
    fan_tags = {f["tag"] for f in fans}
    for room in rooms:
        for t in unit_tags(room.pop("_space").get("equipment_tags_within_60pt", "")):
            if t in fan_tags:
                room["exhaust_fan"] = t
                break
    flags: List[Dict[str, Any]] = []
    flags.append(dict(type="ASSUMPTION", ref="Criteria", text=f"The default criteria apply to the {n_default} circulation, corridor and lobby spaces that no booklet row covers. "
                      f"They have {crit['min_oa_ach']:g} ACH outdoor air, {crit['min_total_ach']:g} ACH total, air class {crit['air_class']}, {crit['filter']}. To confirm.", source=csid))
    flags.append(dict(type="ASSUMPTION", ref="Criteria", text=f"{n_booklet} spaces take the most frequent booklet row of their room type (toilets, offices, meeting rooms and others). To confirm.",
                      source="CIRC-DEFAULT"))
    if n_none:
        flags.append(dict(type="VERIFY", ref="Criteria", text=f"{n_none} spaces have no criteria. No booklet room type fits them and they are not circulation spaces "
                          f"(for example plant rooms and spaces with no name). They are listed on Flags one by one.", source=""))
    if skipped:
        flags.append(dict(type="VERIFY", ref="Room Information", text=f"{len(skipped)} App D area marks have no area (degenerate outline, printed area 0). They are not rooms of this workbook.", source=""))
    flags.append(dict(type="VERIFY", ref="Room Information", text=f"{n_dpa} of {len(rooms)} spaces have a DPA room of the same name on the same storey. The DPA designed area is shown for these only.", source=""))
    for a in ahus:
        if a.get("list_without_rooms"):
            flags.append(dict(type="VERIFY", ref=a["tag"], text=f"Unit {a['tag']} is marked on App D but no space of this workbook carries it. It is listed with its tender airflow and no calculation.", source=""))
    flags.append(dict(type="CONFIRM", ref="Envelope", text="Envelope rows are not set. Every space is treated as internal, with no glazing, wall or roof gain.", source="ASM-DEFAULT"))
    flags.append(dict(type="CONFIRM", ref="Room Information", text="Lighting, small power, equipment and operation time are not set: no document gives them. The ceiling height is the flagged default.", source="ASM-DEFAULT"))
    if not all(people.values()):
        flags.append(dict(type="CONFIRM", ref="README", text="Author, checker and approver of the revision block are not given. They are left blank.", source="ASM-DEFAULT"))
    sp = list(base["space_types"]) + [dict(name="UNCLASSIFIED", room_t_c=base["space_types"][0]["room_t_c"], room_h_kjkg=base["space_types"][0]["room_h_kjkg"],
                                           code="A", source="ASM-DEFAULT")]
    storeys = sorted({r["storey"] for r in rooms})
    data = dict(base)
    data.update(
        department=dict(number="00", name="Common Areas"),
        project=dict(base["project"], revision=dict(rev="A", date=conf.revision_date(root), description="Issued for review", **people)),
        scope="Thermal load, airflow, outdoor air and pressure workbook for the common areas of storeys 2 to 7. Rooms are the App D spaces; systems are the App D equipment.",
        storeys=storeys, storey_labels={s: f"Level {int(s[1:])}" for s in storeys}, space_types=sp, sources=sources, rooms=rooms, ahus=ahus, fans=fans,
        flags=flags, references=[], terminals=[],
        documents=[dict(document="Appendix D, ACMV for common areas (combined plans)", role="Spaces and equipment", issue="as issued",
                        taken="Space outlines and area labels, equipment tags", source=rooms[0]["prov"]["identity"][1]),
                   dict(document="Project design basis: default criteria for circulation, corridor and lobby spaces", role="Criteria", issue="22 Nov 2026",
                        taken="Default criteria row", source=csid)],
        key_flags=[f["text"] for f in flags[:3]],
        criteria_origin=["Room criteria: a space takes the most frequent booklet row of its room type where a booklet prints it. Circulation, corridor and lobby spaces that no booklet row covers take the default. Any other space has no criteria. All of this is an assumption, to confirm.",
                         "Occupancy: the default density by room use, rounded up, because no RDS applies to the common areas."])
    data["constants"] = dict(base["constants"])
    data["assemble_stats"] = dict(rooms=len(rooms), skipped_spaces=len(skipped), spaces_with_unit=n_unit, spaces_with_dpa_area=n_dpa, units=len(ahus),
                                  units_with_tender=sum(1 for a in ahus if a.get("sched_supply_lps") is not None), exhaust_fans=len(fans),
                                  fans_with_tender=sum(1 for f in fans if f.get("scheduled_lps") is not None))
    return data


def build_common_areas(out: Path, json_out: Optional[Path] = None, root: Path = Path(".")) -> Dict[str, Any]:
    data = assemble_common_areas(root)
    if json_out:
        Path(json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(json_out).write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    ctx = build_workbook(data, Path(out))
    return dict(rooms=len(ctx.rooms), units=len(ctx.ahus), flags=ctx.n_flags, stats=data["assemble_stats"])
