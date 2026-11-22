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
from .assemble import (REG, _expand_abbreviations, _f, _norm, _read, _tag_candidates, _tender_for_tag, criteria_from_row, match_criteria, room_use)
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
            text_as_read="design room condition of the Rev A design basis (24 °C, 52.6 kJ/kg) applied where no space type is set", method="assumption, to confirm", confidence="low")
    add_src("ASM-ACTIVITY", document="Workbook defaults", issue="", page="", location="activity of occupants",
            text_as_read="activity Seated (Rev A table) applied where occupancy is assumed", method="assumption, to confirm", confidence="low")
    add_src("OCC-DENSITY", document="ASHRAE Standard 62.1, Table 6.2.2.1: default occupant density", issue="edition to confirm (health-care rows: 62.1-2007)",
            page="Table 6.2.2.1", location="persons per 100 m² by occupancy category", text_as_read="default densities per 100 m², as listed in the project design basis",
            method="project design basis", confidence="medium")
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

    def criteria_for(name: str, number: str):
        """(criteria dict or None, kind) for a space: booklet row, circulation default, or none."""
        key = name.lower()
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
                              location=row.get("bbox", ""), text_as_read=(row.get("raw_cells") or "")[:300],
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

    # unit of each space: nearest AHU, PAHU or FCU
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
                      method="area label and outline read from the markup", confidence="high" if s.get("printed_area_m2") else "medium")
        room: Dict[str, Any] = dict(number=number, name=name, storey=st, ahu=None, zone_tag=None, system_type=None, terminal_tag=None, exhaust_fan=None,
                                    space_type="UNCLASSIFIED", room_condition="Comfort", dpa_area_m2=None, soa_area_m2=printed if printed else None,
                                    measured_area_m2=meas, area_selector="SOA" if printed else None, ceiling_height_m=None, occupancy_people=None,
                                    occupancy_per_100m2=None, iof=None, activity=None, operation_hours=None, lighting_w_m2=None, small_power_w_m2=None,
                                    gaps={}, criteria={}, envelope=[],
                                    prov={"identity": ["D", sid], "zoning": ["D", sid], "space_type": ["A", "ASM-DEFAULT"], "soa_area": ["D", sid],
                                          "measured_area": ["M", sid], "area_selector": ["S", "GEN-CONFIG"]},
                                    remark=f"App D page {s['page']}: area label {printed:g} m²" if printed else f"App D page {s['page']}: outline {meas:g} m² at 1:250")
        cc, kind = criteria_for(name, number)
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
        cands = [t for t in unit_tags(s.get("equipment_tags_within_60pt", "")) if type_of.get(_norm(t)) in UNIT_TYPES]
        if cands:
            room["ahu"] = cands[0]
            room["system_type"] = "FCU" if type_of[_norm(cands[0])] == "FCU" else None
            if len(cands) > 1:
                room["remark"] += f"; other units near the space: {', '.join(cands[1:4])}"
            n_unit += 1
        else:
            room["gaps"]["ahu"] = "No App D unit is marked inside or beside this space."
        m = dpa_by.get((st, _norm(name)))
        if m and len(m) == 1 and _f(m[0].get("designed_area_m2")):
            room["dpa_area_m2"] = _f(m[0]["designed_area_m2"])
            dsid = add_src(f"DPA-{_norm(m[0]['unique_room_identifier'])}", document=Path(m[0]["source_file"]).name, issue="DPA", page=f"row {m[0]['row']}",
                           location=f"room {m[0]['unique_room_identifier']}", text_as_read=f"designed {m[0]['designed_area_m2']} m²", method="read from the DPA sheet", confidence="high")
            room["prov"]["dpa_area"] = ["D", dsid]
            n_dpa += 1
        use, label = room_use(name)
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
            room["remark"] += "; occupancy not set (0 people is a placeholder)"
            room["gaps"]["occupancy"] = "Occupancy is not set. No RDS applies to the common areas. No room use fits the space name for the default density."
        room["_space"] = s
        rooms.append(room)

    # units: every AHU, PAHU and FCU of App D, with the tender airflow
    ahus: List[Dict[str, Any]] = []
    on_rooms = {r["ahu"] for r in rooms if r["ahu"]}
    fans: List[Dict[str, Any]] = []
    for tag in dict.fromkeys(e["tag_normalised"] for e in equip):
        typ = type_of[_norm(tag)]
        if typ in UNIT_TYPES:
            t = _tender_for_tag(tag, ahu_sched, fcu_sched, sched_equip, sched_totals, add_src)
            a = dict(tag=tag, type=t.pop("type", typ))
            a.update(t)
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
                              location=f"tag {tag}", text_as_read=f"{tag}: airflow {lps:g} L/s", method="read from the schedule table", confidence="high")
                f.update(scheduled_lps=lps, code="D", source=sid, tender_source_text=f"Equipment Schedule Corr 5, page {rr.get('schedule_pages', '')}, {tag}")
            else:
                f.update(code="D", source="CIRC-DEFAULT" if False else "ASM-DEFAULT")
            fans.append(f)
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
    flags.append(dict(type="CONFIRM", ref="Room Information", text="Ceiling height, lighting, small power, equipment and operation time are not set: no document gives them.", source="ASM-DEFAULT"))
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
