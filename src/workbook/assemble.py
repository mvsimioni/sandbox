"""Build a department input JSON from the registers (out/registers/*.csv).

Matching of rooms to layout polygons, criteria rows and schematic rows is by normalised name (and area) only; whatever does
not match uniquely is listed on Flags and left blank, never guessed. Works with or without zone_assignment_<storey>.csv: without
an AHU column the AHU stays blank and every room is flagged.
"""

from __future__ import annotations

import csv
import difflib
import glob
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .reva import build_reva_input

REG = Path("out/registers")
UNCLASSIFIED = "UNCLASSIFIED"
AHU_COLUMNS = ("ahu", "ahu_tag", "ahu_or_system", "system_tag")
ZONE_COLUMNS = ("zone_tag", "zone", "zone_id")
SYSTYPE_COLUMNS = ("system_type", "systype")


def _norm(s: Optional[str]) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _f(v: Any) -> Optional[float]:
    try:
        x = float(str(v).replace(",", "").strip())
        return x
    except (TypeError, ValueError):
        return None


def _read(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _regime(text: str) -> Optional[str]:
    t = (text or "").upper()
    if not t or t == "NA":
        return None
    if "-VE" in t or "NEGATIVE" in t:
        return "NEGATIVE"
    if "+VE" in t or "POSITIVE" in t:
        return "POSITIVE"
    if "NEUTRAL" in t:
        return "NEUTRAL"
    return None


def _storey_label(code: str) -> str:
    m = re.fullmatch(r"L0?(\d+)", code)
    if m:
        return f"Level {int(m.group(1))}"
    m = re.fullmatch(r"B(\d+)", code)
    if m:
        return f"Basement {m.group(1)}"
    return f"Storey {code}" if code != "NS" else "Storey not established"


def _dept_key(dept: str) -> str:
    m = re.match(r"\s*0*(\d+)", str(dept))
    if not m:
        raise ValueError(f"department '{dept}' has no number")
    return f"{int(m.group(1)):02d}"


def load_polygons(root: Path) -> Dict[str, List[Dict[str, str]]]:
    out: Dict[str, List[Dict[str, str]]] = {}
    for f in sorted(glob.glob(str(root / REG / "room_polygons_*.csv"))):
        if f.endswith("_wkt.csv"):
            continue
        st = Path(f).stem.replace("room_polygons_", "")
        out[st] = _read(Path(f))
    return out


def load_zone_assignments(root: Path) -> Dict[str, Dict[str, Dict[str, str]]]:
    """storey -> room_id -> row, for every zone_assignment_<storey>.csv that exists."""
    out: Dict[str, Dict[str, Dict[str, str]]] = {}
    for f in sorted(glob.glob(str(root / REG / "zone_assignment_*.csv"))):
        st = Path(f).stem.replace("zone_assignment_", "")
        out[st] = {r["room_id"]: r for r in _read(Path(f)) if r.get("room_id")}
    return out


def _first(row: Dict[str, str], cols: Tuple[str, ...]) -> str:
    for c in cols:
        if row.get(c):
            return row[c].strip()
    return ""


def _people(root: Path) -> Dict[str, str]:
    """Author, checker and approver from config/project.yaml (workbook.people); blank when not given, never invented."""
    import yaml
    try:
        cfg = yaml.safe_load((root / "config" / "project.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        cfg = {}
    p = (cfg.get("workbook") or {}).get("people") or {}
    return dict(by=str(p.get("author") or ""), checked=str(p.get("checked_by") or ""), approved=str(p.get("approved_by") or ""))


def _revision_date(root: Path) -> str:
    """Revision date: workbook.revision_date in config/project.yaml when given, else the build date (never the Rev A example date)."""
    import datetime
    import yaml
    try:
        cfg = yaml.safe_load((root / "config" / "project.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        cfg = {}
    given = (cfg.get("workbook") or {}).get("revision_date")
    return str(given) if given else datetime.date.today().isoformat()


def assemble_department(dept: str, out: Optional[Path] = None, root: Path = Path(".")) -> Dict[str, Any]:
    key = _dept_key(dept)
    soa = [r for r in _read(root / REG / "soa_rooms.csv") if r["dept_no"] == key and r.get("governing", "").lower() == "yes"]
    if not soa:
        raise ValueError(f"no governing SOA rows for department {key} in soa_rooms.csv")
    dept_name = soa[0]["department_folder"]
    dpa = {r["unique_room_identifier"]: r for r in _read(root / REG / "dpa_rooms.csv") if r.get("unique_room_identifier")}
    criteria = [r for r in _read(root / REG / "criteria.csv") if r["dept_no"].lstrip("0") == key.lstrip("0")]
    polygons = load_polygons(root)
    zones = load_zone_assignments(root)
    sched_rooms = _read(root / REG / "schematic_rooms.csv")
    sched_equip = _read(root / REG / "schematic_equipment.csv")
    people = _people(root)
    base = build_reva_input(root)  # the project default design basis, read from the Rev A workbook

    sources: List[Dict[str, Any]] = [s for s in base["sources"] if s["id"] in ("RA-RZ-TBL", "RA-BOD-C19", "RA-BOD-C28", "RA-BOD-C30", "RA-BOD-C31",
                                                                              "RA-AHU-B29", "RA-OCC", "RA-DD21")]
    sources.append(dict(id="ASM-DEFAULT", document="Workbook generator defaults", issue="", page="", location="space type UNCLASSIFIED",
                        text_as_read="design room condition of the Rev A basis (24 C, 52.6 kJ/kg) applied where no criteria row or space type is mapped",
                        method="assumption flagged; replace when the room is classified", confidence="low"))
    flags: List[Dict[str, Any]] = []
    src_ids = {s["id"] for s in sources}

    def add_src(sid, **kw):
        if sid not in src_ids:
            sources.append(dict(id=sid, **kw))
            src_ids.add(sid)

    # polygon index by normalised name
    poly_by_name: Dict[str, List[Tuple[str, Dict[str, str]]]] = defaultdict(list)
    for st, rows in polygons.items():
        for p in rows:
            for k in ("room_number", "room_tag", "room_name"):
                if p.get(k):
                    poly_by_name[_norm(p[k])].append((st, p))
    crit_index = {_norm(c["room_type_as_printed"]): c for c in criteria}
    used_poly = set()
    rooms: List[Dict[str, Any]] = []
    seen_numbers = set()
    for r in soa:
        code = r["room_code"].strip()
        number = code
        while number in seen_numbers:
            number += "'"
        seen_numbers.add(number)
        sid = f"SOA-{key}-{_norm(code)}"
        add_src(sid, document=r["source_file"], issue=f"{r.get('soa_version', '')} {r.get('issue_date', '')}".strip(), page=r.get("page_or_sheet_row", ""),
                location=r.get("bbox_pt", ""), text_as_read=r.get("raw_cells", "")[:400], method=r.get("method", "")[:200],
                confidence=str(r.get("confidence", "")))
        area = _f(r.get("as_drawn_area_m2")) or _f(r.get("designed_area_m2")) or _f(r.get("unit_area_m2"))
        qty = _f(r.get("qty")) or 1
        if qty > 1 and area:
            flags.append(dict(type="VERIFY", ref=number, text=f"Room {number}: SOA quantity {qty:g}; the area entered is the unit area, not the total.", source=sid))
        dparea = None
        if area is None and code in dpa:
            dparea = _f(dpa[code].get("designed_area_m2")) or _f(dpa[code].get("programmed_area_m2"))
            if dparea:
                sid_d = f"DPA-{_norm(code)}"
                add_src(sid_d, document=dpa[code]["source_file"], issue="DPA", page=dpa[code]["row"], location="sheet Database", text_as_read=f"designed {dpa[code].get('designed_area_m2')}; programmed {dpa[code].get('programmed_area_m2')}", method="openpyxl cached values", confidence="medium")
        room: Dict[str, Any] = dict(number=number, name=r["room_name"], storey=None, ahu=None, zone_tag=None, system_type=None, terminal_tag=None,
                                    exhaust_fan=None, space_type=UNCLASSIFIED, room_condition="Comfort", soa_area_m2=area if area is not None else dparea,
                                    measured_area_m2=None, area_selector=None, ceiling_height_m=None, occupancy_people=None, occupancy_per_100m2=None,
                                    iof=None, activity=None, operation_hours=None, lighting_w_m2=None, small_power_w_m2=None,
                                    prov={"identity": ["D", sid], "zoning": ["D", sid], "space_type": ["A", "ASM-DEFAULT"],
                                          "soa_area": ["D", sid if area is not None else f"DPA-{_norm(code)}"]},
                                    criteria={}, envelope=[], remark=f"SOA {r.get('soa_version', '')} {r.get('page_or_sheet_row', '')}: {r['room_name']}")
        if room["soa_area_m2"] is None:
            flags.append(dict(type="CONFIRM", ref=number, text=f"Room {number} {r['room_name']}: no area in the SOA or DPA.", source=sid))
        # criteria
        cm = crit_index.get(_norm(r["room_name"]))
        if cm is None:
            names = list(crit_index)
            close = difflib.get_close_matches(_norm(r["room_name"]), names, n=1, cutoff=0.9)
            cm = crit_index[close[0]] if close else None
        if cm is not None:
            csid = f"CRIT-{key}-{_norm(cm['room_type_as_printed'])}"[:60]
            add_src(csid, document=cm["source_file"], issue=cm.get("department_label", ""), page=f"p.{cm['booklet_page']}", location=cm.get("bbox", ""),
                    text_as_read=cm.get("raw_cells", "")[:400], method="criteria register (booklet criteria table)", confidence=str(cm.get("confidence", "")))
            room["criteria"] = dict(criteria_row=cm["room_type_as_printed"], page=f"p.{cm['booklet_page']}", min_oa_ach=_f(cm["min_oa_ach"]),
                                    min_total_ach=_f(cm["min_total_ach"]), temp_min_c=_f(cm["temp_min_c"]), temp_max_c=_f(cm["temp_max_c"]),
                                    rh_max_pct=_f(cm["rh_max_pct"]), pressure_regime=_regime(cm["pressure_regime_as_printed"]),
                                    pressure_pa_booklet=_f(cm["pressure_pa"]), air_class=(cm["air_class"] if cm["air_class"] not in ("", "NA") else None),
                                    filter=cm["filter"] or None, single_pass=("Y" if cm["single_pass"].upper().startswith("Y") else ("N" if cm["single_pass"] else None)),
                                    afterhours=cm["afterhours"] or None, source=csid, code="D")
            if cm["pressure_regime_as_printed"] and room["criteria"]["pressure_regime"] and room["criteria"]["pressure_pa_booklet"] is None:
                flags.append(dict(type="VERIFY", ref=number, text=f"Room {number} {r['room_name']}: pressure given as a direction only ({cm['pressure_regime_as_printed']}); "
                                  "the tender and working drawings have not yet been read for a value; not calculated on Pressure.", source=csid))
        else:
            flags.append(dict(type="CONFIRM", ref=number, text=f"Room {number} {r['room_name']}: no criteria row maps to this room name; criteria cells blank.", source=sid))
        # polygon (layout)
        uniq = {(st, p["room_id"]): (st, p) for st, p in poly_by_name.get(_norm(r["room_name"]), [])}
        cands = [sp for k, sp in uniq.items() if k not in used_poly]
        if area and len(cands) > 1:
            cands = [(st, p) for st, p in cands if _f(p.get("a_area_m2")) and abs(_f(p["a_area_m2"]) - area) <= 0.1 * area]
        if len(cands) == 1:
            st, p = cands[0]
            used_poly.add((st, p["room_id"]))
            psid = f"LAYOUT-{p['room_id']}"
            add_src(psid, document=f"out/registers/room_polygons_{st}.csv", issue="", page=st, location=f"room {p['room_id']}",
                    text_as_read=f"a_area_m2 {p.get('a_area_m2')}; b_area_m2 {p.get('b_area_m2')}; perimeter {p.get('a_perimeter_m')}; {p.get('method_agreement')}",
                    method="two-method room measurement", confidence=str(p.get("method_agreement", "")))
            room["storey"] = st
            room["measured_area_m2"] = _f(p.get("a_area_m2"))
            room["prov"]["measured_area"] = ["M", psid]
            room["_perimeter"] = (_f(p.get("a_perimeter_m")), psid)
            za = zones.get(st, {}).get(p["room_id"])
            if za:
                room["ahu"] = _first(za, AHU_COLUMNS) or None
                room["zone_tag"] = _first(za, ZONE_COLUMNS) or None
                room["system_type"] = _first(za, SYSTYPE_COLUMNS) or None
                zsid = f"ZONE-{st}-{p['room_id']}"
                add_src(zsid, document=f"out/registers/zone_assignment_{st}.csv", issue="", page=st, location=p["room_id"], text_as_read=json.dumps(za)[:300],
                        method="zoning assignment", confidence=str(za.get("confidence", "")))
                room["prov"]["zoning"] = ["D", zsid]
        else:
            why = "several candidates, none unique" if len(cands) > 1 else "no layout polygon with this name"
            flags.append(dict(type="VERIFY", ref=number, text=f"Room {number} {r['room_name']}: not matched to a layout polygon ({why}); measured area, perimeter and storey left blank.", source=sid))
        if room["storey"] is None:
            sc = _first(dpa.get(code, {}), ("storey_from_identifier",)) or "NS"
            room["storey"] = sc
        if not room["ahu"]:
            flags.append(dict(type="CONFIRM", ref=number, text=f"Room {number} {r['room_name']}: no AHU or system (approved zoning not available for this room).", source=sid))
        rooms.append(room)

    # pressure blocks: only where a booklet value exists and a perimeter was measured
    for room in rooms:
        c = room["criteria"]
        pa = c.get("pressure_pa_booklet") if c else None
        per = room.pop("_perimeter", None)
        if pa is not None and per and per[0]:
            room["pressure"] = dict(mode="controlled", perimeter_m=per[0], prov=["M", per[1]], doors=[])
            flags.append(dict(type="VERIFY", ref=room["number"], text=f"Room {room['number']}: pressure {pa:g} Pa calculated with the envelope only; doors.csv is not available, so door gaps are missing.", source=c["source"]))
        elif pa is not None:
            flags.append(dict(type="VERIFY", ref=room["number"], text=f"Room {room['number']}: pressure {pa:g} Pa but no measured perimeter; not calculated on Pressure.", source=c["source"]))
    # schematic comparison, only for rooms with a known AHU
    ahus: Dict[str, Dict[str, Any]] = {}
    for room in rooms:
        if not room["ahu"]:
            continue
        tag = room["ahu"]
        ahus.setdefault(tag, dict(tag=tag, type="AHU"))
        rows = [s for s in sched_rooms if tag in (s["ahu_tag"] or "") and _norm(s["room_name_as_written"]) == _norm(room["name"]) and s["issue_set"] == "working"
                and (not room["storey"] or s["storey"] == room["storey"])]
        if len(rows) == 1:
            s = rows[0]
            ssid = f"SCH-{s['sheet']}-{_norm(s['room_name_as_written'])}"[:60]
            add_src(ssid, document=s["drive_rel_path"], issue=s["issue"], page=str(s["page"]), location=s["box_page_view_table"], text_as_read=s["airflow_as_printed"],
                    method=s["method"][:200], confidence=s["confidence"])
            room["schematic"] = dict(supply_lps=_f(s["supply_L_s"]), return_lps=_f(s["return_L_s"]), exhaust_lps=_f(s["exhaust_L_s"]),
                                     pressure_pa=_f(re.sub(r"[^\d\-.]", "", s["pressure"] or "")), source=ssid, code="D")
    for tag, a in ahus.items():
        eq = [e for e in sched_equip if e["tag"] == tag and e["issue_set"] == "working"]
        if len(eq) == 1:
            m = re.match(r"\s*([\d,]+)\s*L/s", eq[0]["duty_as_written"] or "")
            if m:
                esid = f"SCHEQ-{_norm(tag)}"
                add_src(esid, document=eq[0]["sheet"], issue=eq[0]["issue"], page=str(eq[0]["page"]), location=eq[0]["box_page_view_duty"], text_as_read=eq[0]["duty_as_written"],
                        method=eq[0]["method"][:200], confidence=eq[0]["confidence"])
                a.update(schematic_supply_lps=float(m.group(1).replace(",", "")), schematic_source=esid, schematic_code="D")
    flags.append(dict(type="CONFIRM", ref="Envelope", text="Envelope rows not yet assigned (Section 8.4 pending): every room is treated as internal, with no glazing, wall or roof gain.", source="ASM-DEFAULT"))
    flags.append(dict(type="CONFIRM", ref="Room Information", text="Ceiling height, occupancy, lighting, small power, equipment and operation time are blank: no document read yet; occupancy fallback and space-type defaults need Marcus's approval.", source="ASM-DEFAULT"))
    flags.append(dict(type="VERIFY", ref="VAV", text="Terminal types (VAV, CAV, venturi) are not yet read from the air-side schematics and specifications; no room is given a terminal.", source="ASM-DEFAULT"))
    if not all(people.values()):
        flags.append(dict(type="CONFIRM", ref="README", text="Author, checker and approver of the revision block are not given in config/project.yaml (workbook.people); left blank.", source="ASM-DEFAULT"))
    storeys = sorted({r["storey"] for r in rooms})
    sp = list(base["space_types"]) + [dict(name=UNCLASSIFIED, room_t_c=base["space_types"][0]["room_t_c"], room_h_kjkg=base["space_types"][0]["room_h_kjkg"],
                                           code="A", source="ASM-DEFAULT")]
    data = dict(base)
    data.update(
        department=dict(number=key, name=re.sub(r"^\d+\s*-\s*", "", dept_name)),
        project=dict(base["project"], revision=dict(rev="A", date=_revision_date(root), description="Generated from the registers", **people)),
        scope=f"Thermal load, airflow, outdoor air and pressure workbook for {dept_name}, assembled from the registers; unmatched rooms are listed on Flags.",
        storeys=storeys, storey_labels={s: _storey_label(s) for s in storeys}, space_types=sp, sources=sources, rooms=rooms,
        ahus=list(ahus.values()), flags=flags, references=[], documents=[d for d in base["documents"][:1]],
        key_flags=[f["text"] for f in flags[-3:]], criteria_origin=["Room criteria: the criteria table of the department's DD Booklet (out/registers/criteria.csv)."])
    data["constants"] = dict(base["constants"])
    data["constants"]["SupplyTemp"] = dict(base["constants"]["SupplyTemp"])
    if out is not None:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return data
