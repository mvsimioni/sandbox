"""Independent recomputation of room loads, room airflows and AHU totals.

Written once, in Python, from ``out/qa/template_spec.md`` (the Rev A example and the NDH
engine as documented there) and brief Section 8.3.  It does not read, import or share code
with ``src/workbook``.  Only ``src.pressure`` (the pressure-offset method, which has its own
known-answer tests) is reused.

Input: the department JSON of ``src/workbook/schema.md`` (version 1).  Output: a dict (JSON
serialisable) with ``rooms``, ``ahus``, ``pressure`` and ``flags``.

Method (engine column letters from the template spec in brackets)
------------------------------------------------------------------
Per supply room, all unrounded until the last step:
  volume N = area x height; people count T = stated count, else density x area / 100;
  OA X = max(T x L/s/p x IOF, OA ACH x N / 3.6, specified);
  minimum supply AB = max(L/s/m2 x area, ACH x N / 3.6, specified);
  exhaust AF = max(area x L/s/m2, exhaust ACH x N / 3.6) or the specified value (AG);
  sensible = (people + lighting + power + glazing + wall/roof + infiltration) x (1 + SF) [BO];
  latent = (infiltration latent + equipment latent + people latent) x (1 + SF) [BQ];
  thermal airflow BW = sensible / (1.21 x (room T - supply T));
  room supply BX = max(BW, AB, what exhaust and pressure regime require), rounded up once to
  the supply rounding step (10 L/s, the example's ROUNDUP(BX,-1)) [BY].
AHU: supply = sum of room BY, plus duct leakage; outdoor air by the selected method;
coil duty = room sensible + OA sensible + fan heat + room latent + OA latent (Rev A AHU Summary).

Method switch: constants ``OAMethod`` = ``HIGHEST_FRACTION`` (brief 8.2, default: largest room
OA fraction x AHU supply incl. leakage) or ``MULTI_SPACE`` (Rev A: max(sum of room OA, Vot)
with supply excluding leakage).  Both results and both coil duties are always reported.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src import pressure as pm

# ---- defaults (template_spec section k, brief Section 11, config/project.yaml) -------------- #
DEFAULTS: Dict[str, float] = {
    "safetyfactor": 0.10,          # BO/BQ literal x1.1
    "ductleakage": 0.10,           # AHU Summary D8, G8, J8
    "airsensible": 1.21,           # W per (L/s.K), BW and AHU Summary B26
    "airinfiltrationsensible": 1.2,  # BD
    "airlatent": 1.18,             # BN
    "airenthalpyahu": 1.2,         # AHU Summary B27
    "infiltrationach": 0.5,        # BB
    "supplyrounding": 10.0,        # ROUNDUP(BX,-1)
    "oarulesharepct": 5.0,         # brief Section 11 item 20
    "oaruledroppp": 5.0,
    "lps_per_m3h": 1.0 / 3.6,
    "chwsupplyc": 8.0,             # config chilled_water.normal
    "chwreturnc": 16.0,
    "waterspecificheat": 4200.0,   # J/(kg.K), template Equipment calcs
    "area_agree_pct": 3.0,         # config tolerances.area_vs_soa
    "area_agree_m2": 0.5,
}
REQUIRED = ("SupplyTemp", "OutdoorDB", "OutdoorWB", "OutdoorH", "FanPressure", "FanEfficiency", "RoofSolarGain")
EXHAUST_ONLY = re.compile(r"EXHAUST", re.I)


class RecomputeError(ValueError):
    """The input cannot be calculated (missing required value, unknown table entry)."""


def _norm(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _num(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)


def _z(value: Any) -> float:
    return 0.0 if value is None or value == "" else float(value)


def _roundup(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.ceil(value / step - 1e-9) * step


# ---- constants ------------------------------------------------------------------------------ #
def read_constants(data: Dict[str, Any]) -> Dict[str, Any]:
    """Constants by normalised name: defaults, then the department file's ``constants``."""
    out: Dict[str, Any] = dict(DEFAULTS)
    out["oamethod"] = "HIGHEST_FRACTION"
    raw = data.get("constants") or {}
    for key, item in raw.items():
        value = item.get("value") if isinstance(item, dict) else item
        out[_norm(key)] = value
    missing = [name for name in REQUIRED if out.get(_norm(name)) is None]
    if missing:
        raise RecomputeError(f"required constant(s) missing or null: {missing}")
    for key in list(out):
        if key not in ("oamethod",) and not isinstance(out[key], (int, float)):
            try:
                out[key] = float(out[key])
            except (TypeError, ValueError):
                pass
    # percentage forms: 10 means 10 % when the file gives a value above 1
    for key in ("safetyfactor", "ductleakage"):
        if out[key] > 1.0:
            out[key] = out[key] / 100.0
    out["oamethod"] = str(out["oamethod"]).upper().replace(" ", "_")
    if out["oamethod"] not in ("HIGHEST_FRACTION", "MULTI_SPACE"):
        raise RecomputeError(f"OAMethod must be HIGHEST_FRACTION or MULTI_SPACE, got {out['oamethod']}")
    return out


# ---- room geometry and loads ---------------------------------------------------------------- #
def select_area(room: Dict[str, Any], c: Dict[str, Any]) -> Tuple[float, str]:
    """Area used (rounded to 0.1 m2) and the basis; brief 8.3 and Section 11 item 18."""
    soa, meas = _num(room.get("soa_area_m2")), _num(room.get("measured_area_m2"))
    sel = str(room.get("area_selector") or "AUTO").upper()
    if soa is None and meas is None:
        raise RecomputeError(f"room {room.get('number')}: no area")
    if soa is None:
        return round(meas, 1), "MEASURED"
    if meas is None:
        return round(soa, 1), "SOA"
    if sel == "SOA":
        return round(soa, 1), "SOA"
    if sel == "MEASURED":
        return round(meas, 1), "MEASURED"
    if sel == "LARGER":
        return round(max(soa, meas), 1), "LARGER"
    diff = abs(soa - meas)
    if diff <= c["area_agree_m2"] or (soa and diff / soa * 100.0 <= c["area_agree_pct"]):
        return round(soa, 1), "SOA"
    return round(meas, 1), "MEASURED"


def _table(items: List[Dict[str, Any]], key: str = "name") -> Dict[str, Dict[str, Any]]:
    return {str(i[key]): i for i in (items or [])}


def room_loads(room: Dict[str, Any], c: Dict[str, Any], tables: Dict[str, Any]) -> Dict[str, Any]:
    """Cooling loads and airflow requirements of one supply room (unrounded)."""
    crit = room.get("criteria") or {}
    area, area_basis = select_area(room, c)
    height = _num(room.get("ceiling_height_m"))
    if height is None:
        raise RecomputeError(f"room {room.get('number')}: no ceiling height")
    vol = area * height
    st_name = room.get("space_type")
    st = tables["space_types"].get(st_name)
    if st is None:
        raise RecomputeError(f"room {room.get('number')}: space type {st_name!r} not in space_types")
    room_t, room_h = float(st["room_t_c"]), float(st["room_h_kjkg"])
    defaults = tables["defaults"].get(st_name, {})

    people = _num(room.get("occupancy_people"))
    if people is None:
        dens = _num(room.get("occupancy_per_100m2"))
        if dens is None:
            dens = _num(defaults.get("density_per_100m2"))
        people = (dens or 0.0) * area / 100.0
    iof = _num(room.get("iof"))
    iof = 1.0 if iof is None else iof
    act_name = room.get("activity")
    act = tables["activities"].get(act_name) if act_name else None
    if people > 0 and act is None:
        raise RecomputeError(f"room {room.get('number')}: activity {act_name!r} not in activities")
    sens_pp, lat_pp = (float(act["sensible_w"]), float(act["latent_w"])) if act else (0.0, 0.0)

    light_w_m2 = _num(room.get("lighting_w_m2"))
    if light_w_m2 is None:
        light_w_m2 = _num(defaults.get("lighting_w_m2")) or 0.0
    power_w_m2 = _num(room.get("small_power_w_m2"))
    if power_w_m2 is None:
        power_w_m2 = _num(defaults.get("small_power_w_m2")) or 0.0

    people_sens = people * sens_pp
    people_lat = people * lat_pp
    lighting = light_w_m2 * area
    power = power_w_m2 * area + _z(room.get("equipment_sensible_w"))
    equip_lat = _z(room.get("equipment_latent_w"))

    glass = wall_load = roof = 0.0
    external = False
    for env in room.get("envelope") or []:
        o = tables["orientations"].get(env.get("orientation"))
        if o is None:
            raise RecomputeError(f"room {room.get('number')}: orientation {env.get('orientation')!r} not in orientations")
        if str(env.get("orientation")).lower() != "internal":
            external = True
        glass += _z(env.get("glazing_m2")) * _z(o.get("window_w_m2"))
        wall_load += _z(env.get("wall_m2")) * _z(o.get("wall_w_m2"))
        roof += _z(env.get("roof_m2")) * float(c["roofsolargain"])
    transmission = wall_load + roof

    regime = str(crit.get("pressure_regime") or "").upper()
    infil_ach = 0.0 if (regime == "POSITIVE" or not external) else float(c["infiltrationach"])
    infil_lps = infil_ach * vol / 3.6
    out_t, out_h = float(c["outdoordb"]), float(c["outdoorh"])
    infil_sens = (out_t - room_t) * c["airinfiltrationsensible"] * infil_lps
    infil_lat = (out_h - room_h) * c["airlatent"] * infil_lps - infil_sens

    sf = 1.0 + c["safetyfactor"]
    sensible = (people_sens + lighting + power + glass + transmission + infil_sens) * sf
    latent = (infil_lat + equip_lat + people_lat) * sf

    # airflow requirements independent of load
    oa_pp = _z(crit.get("oa_lps_per_person"))
    oa_req = max(people * oa_pp * iof, _z(crit.get("min_oa_ach")) * vol / 3.6, _z(crit.get("oa_specified_lps")))
    min_supply = max(_z(crit.get("supply_lps_m2")) * area, _z(crit.get("min_total_ach")) * vol / 3.6,
                     _z(crit.get("supply_specified_lps")))
    exh_calc = max(_z(crit.get("exhaust_lps_m2")) * area, _z(crit.get("exhaust_ach")) * vol / 3.6)
    exh_spec = _num(crit.get("exhaust_specified_lps"))
    # the example rounds the calculated exhaust up once, like the supply: AG = ROUNDUP(AF, -1)
    exhaust_req = exh_spec if exh_spec is not None else (_roundup(exh_calc, c["supplyrounding"]) if exh_calc > 0 else 0.0)

    dt = room_t - float(c["supplytemp"])
    if dt <= 0:
        raise RecomputeError(f"room {room.get('number')}: room temperature {room_t} not above supply {c['supplytemp']}")
    thermal = sensible / (c["airsensible"] * dt)

    return {
        "area_m2": area, "area_basis": area_basis, "height_m": height, "volume_m3": vol,
        "room_t_c": room_t, "room_h_kjkg": room_h, "people": people, "iof": iof,
        "people_sensible_w": people_sens, "lighting_w": lighting, "power_w": power, "glazing_w": glass,
        "transmission_w": transmission, "infiltration_lps": infil_lps, "infiltration_sensible_w": infil_sens,
        "people_latent_w": people_lat, "equipment_latent_w": equip_lat, "infiltration_latent_w": infil_lat,
        "sensible_w": sensible, "latent_w": latent,
        "thermal_airflow_lps": thermal, "min_supply_lps": min_supply, "oa_required_lps": oa_req,
        "exhaust_required_lps": exhaust_req, "exhaust_specified": exh_spec is not None,
        "supply_temp_c": float(c["supplytemp"]), "pressure_regime": regime,
        "fixed_by_specification": str(crit.get("code") or "").upper() == "S",
        "single_pass": str(crit.get("single_pass") or "").upper() == "Y",
        "specified_supply_lps": _num(crit.get("supply_specified_lps")),
        "specified_oa_lps": _num(crit.get("oa_specified_lps")),
    }


def _pressure_target(room: Dict[str, Any]) -> Optional[float]:
    """Tender value, then working drawing, then booklet (brief 8.3 / schema); signed by the regime."""
    crit = room.get("criteria") or {}
    for key in ("pressure_pa_tender", "pressure_pa_working", "pressure_pa_booklet"):
        v = _num(crit.get(key))
        if v is not None:
            regime = str(crit.get("pressure_regime") or "").upper()
            if regime == "NEGATIVE" and v > 0:
                v = -v
            if regime == "POSITIVE" and v < 0:
                v = -v
            return v
    return None


# ---- pressure suites ------------------------------------------------------------------------ #
def _door_link(room_no: str, door: Dict[str, Any]) -> pm.Link:
    to = str(door["to"])
    kind = str(door.get("kind") or door.get("id") or "door")
    if door.get("fixed_flow_m3h") is not None:
        return pm.Link(room_no, to, fixed_flow_m3h=float(door["fixed_flow_m3h"]), kind=kind)
    if door.get("gap_cm2") is not None:
        return pm.Link(room_no, to, gap_area_m2=pm.cm2_to_m2(float(door["gap_cm2"])), kind=kind)
    if door.get("seal"):
        return pm.Link(room_no, to, gap_area_m2=pm.sealed_gap_area(str(door["seal"])), kind=kind)
    if door.get("leaf_w_m") is not None and door.get("leaf_h_m") is not None:
        area = pm.unsealed_door_gap_area(float(door["leaf_w_m"]), float(door["leaf_h_m"]))
        return pm.Link(room_no, to, gap_area_m2=area, kind=kind)
    raise RecomputeError(f"room {room_no} door {door.get('id')}: no gap area, seal, leaf size or fixed flow")


def solve_pressure(rooms: List[Dict[str, Any]], results: Dict[str, Dict[str, Any]],
                   nodes: List[Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Solve every suite exactly; return per-room pressure results and the unresolved rooms."""
    node_pa = {str(n["tag"]): float(n["pressure_pa"]) for n in nodes or []}
    node_pa.setdefault(pm.REF, 0.0)
    by_no = {str(r["number"]): r for r in rooms}
    pr_rooms = {no: r for no, r in by_no.items() if (r.get("pressure") or {}).get("mode") in ("controlled", "free")}
    unresolved: List[str] = []
    setp: Dict[str, Optional[float]] = {}
    for no, r in list(pr_rooms.items()):
        if r["pressure"]["mode"] == "controlled":
            target = _pressure_target(r)
            if target is None:
                unresolved.append(no)
                del pr_rooms[no]
                continue
            setp[no] = target
        else:
            setp[no] = None
    suites: Dict[str, List[str]] = {}
    for no, r in pr_rooms.items():
        suites.setdefault(str(r["pressure"].get("suite") or f"room {no}"), []).append(no)
    out: Dict[str, Dict[str, Any]] = {}
    for sname, members in suites.items():
        spaces: List[pm.Space] = []
        links: List[pm.Link] = []
        externals: Dict[str, float] = {}
        seen_doors: set = set()
        for no in members:
            r, p, res = by_no[no], by_no[no]["pressure"], results[no]
            area, height = res["area_m2"], res["height_m"]
            if p.get("surface_override_m2") is not None:
                surface = float(p["surface_override_m2"])
            else:
                if p.get("perimeter_m") is None:
                    raise RecomputeError(f"room {no}: no perimeter and no surface override")
                surface = pm.internal_surface(area, float(p["perimeter_m"]), height)
            spaces.append(pm.Space(no, setp[no], surface, supply_m3h=pm.m3h(res["supply_lps_nominal"]),
                                   exhaust_m3h=pm.m3h(res["exhaust_lps_nominal"])))
            # Schema convention (src/workbook/schema.md): a door is entered once, under either room, and
            # acts on both spaces it joins. Own doors first, then doors other rooms list towards this room.
            own = [(no, door) for door in p.get("doors") or []]
            reverse = [(other, door) for other, orow in by_no.items() if other != no
                       for door in (orow.get("pressure") or {}).get("doors") or []
                       if str(door["to"]) == no]
            for owner, door in own + reverse:
                key = (str(door.get("id")), frozenset((owner, str(door["to"]))))
                if door.get("id") is not None and key in seen_doors:
                    continue
                seen_doors.add(key)
                links.append(_door_link(owner, door))
                for end in (owner, str(door["to"])):
                    if end in members:
                        continue
                    if end in node_pa:
                        externals[end] = node_pa[end]
                    elif end in setp and setp[end] is not None:
                        externals[end] = setp[end]
                    else:
                        raise RecomputeError(f"room {no} door {door.get('id')} to {end!r}: not a member of suite, node or controlled room")
        suite = pm.Suite(sname, spaces, links, externals)
        solved = suite.solve_exact()
        for sp in solved.spaces:
            out[sp.name] = {
                "suite": sname, "mode": "controlled" if sp.controlled else "free", "set_pressure_pa": sp.set_pressure_pa,
                "pressure_pa": sp.pressure_pa, "surface_m2": sp.surface_m2, "air_in_m3h": sp.air_in,
                "air_out_m3h": sp.air_out, "offset_kind": sp.offset_kind, "offset_needed_m3h": sp.offset_needed,
                "offset_needed_lps": pm.lps(sp.offset_needed), "design_offset_m3h": sp.design_offset,
                "tighter_offset_m3h": sp.tighter_offset, "ratio": sp.ratio, "status": sp.status,
                "reversed": sp.reversed, "ratio_n050": sp.ratio_n050, "ratio_n075": sp.ratio_n075,
                "ratio_gaps_x2": sp.ratio_gaps_x2, "uncontrolled_pa": sp.uncontrolled_pa,
                "flagged": sp.flagged, "notes": list(sp.notes),
            }
    return out, unresolved


# ---- airflow rule --------------------------------------------------------------------------- #
def _apply_airflow(res: Dict[str, Any], c: Dict[str, Any], pres: Optional[Dict[str, Any]]) -> None:
    """Final supply, exhaust and OA of one supply room, with the governing case (brief 8.3)."""
    thermal, minimum = res["thermal_airflow_lps"], res["min_supply_lps"]
    exh_req = res["exhaust_required_lps"]
    offset = 0.0
    kind = ""
    if pres and pres["mode"] == "controlled" and pres["status"] not in ("UNSOLVED",):
        offset, kind = pres["offset_needed_lps"], pres["offset_kind"]
    if kind == "exhaust":        # negative room: exhaust = supply + offset
        floor = max(exh_req - offset, 0.0)
    elif kind == "supply":       # positive room: exhaust = supply - offset
        floor = exh_req + offset
    else:
        floor = exh_req
    if res["fixed_by_specification"] and res["specified_supply_lps"] is not None:
        supply_raw = res["specified_supply_lps"]
        governing = "specified"
    else:
        supply_raw = max(thermal, minimum, floor)
        if supply_raw == thermal and thermal >= minimum and thermal >= floor:
            governing = "thermal"
        elif supply_raw == minimum and minimum >= floor:
            governing = "air_change"
        else:
            governing = "exhaust_pressure"
    step = c["supplyrounding"]
    supply = supply_raw if res["fixed_by_specification"] else _roundup(supply_raw, step)
    if kind == "exhaust":
        exhaust = max(supply + offset, exh_req)
    elif kind == "supply":
        exhaust = max(supply - offset, 0.0)
    else:
        exhaust = exh_req
    oa = res["oa_required_lps"]
    if res["single_pass"]:
        oa = max(oa, supply)
    elif exhaust > 0:
        oa = max(oa, min(exhaust, supply))
    if kind == "supply" and not res["single_pass"]:
        # positive room: the exhaust and the air pushed out through leakage are both made up from outdoor air
        oa = max(oa, min(exhaust + offset, supply))
    if res["fixed_by_specification"] and res["specified_oa_lps"] is not None:
        oa = res["specified_oa_lps"]
    oa = min(oa, supply) if supply > 0 else oa
    res.update({
        "supply_lps_unrounded": supply_raw, "supply_lps": supply, "exhaust_lps": exhaust,
        "offset_lps": offset, "offset_kind": kind, "oa_lps": oa,
        "oa_fraction": oa / supply if supply > 0 else 0.0,
        "governing_case": governing,
        "case_margin_thermal_vs_air_change_lps": thermal - minimum,
        "achieved_ach": supply * 3.6 / res["volume_m3"] if res["volume_m3"] else None,
        "required_supply_temp_c": res["room_t_c"] - res["sensible_w"] / (c["airsensible"] * supply) if supply else None,
    })


# ---- AHU totalisation ----------------------------------------------------------------------- #
def ahu_totals(tag: str, rooms: List[Dict[str, Any]], c: Dict[str, Any]) -> Dict[str, Any]:
    supply_rooms = [r for r in rooms if r["supply_lps"] > 0]
    leak = c["ductleakage"]
    supply = sum(r["supply_lps"] for r in supply_rooms)
    supply_leak = supply * (1.0 + leak)
    sum_oa = sum(r["oa_lps"] for r in supply_rooms)
    exhaust = sum(r["exhaust_lps"] for r in rooms)
    sens = sum(r["sensible_w"] for r in supply_rooms) / 1000.0
    lat = sum(r["latent_w"] for r in supply_rooms) / 1000.0
    area = sum(r["area_m2"] for r in supply_rooms)

    crit = max(supply_rooms, key=lambda r: r["oa_fraction"], default=None)
    frac = crit["oa_fraction"] if crit else 0.0
    oa_highest = frac * supply_leak
    # multiple-space (Rev A AHU Summary S8..AA8): Vot = Z x Vst, Z = X/(1+X-Y)
    x = sum_oa / supply if supply else 0.0
    y = frac
    vot = x / (1.0 + x - y) * supply if supply else 0.0
    oa_rev_a = max(sum_oa, vot)
    # reference condition for the coil, as the example (AHU Summary B13/B14 take the first space type):
    # the first supply room of the AHU in room-list order
    ref = supply_rooms[0] if supply_rooms else None
    room_t, room_h = (ref["room_t_c"], ref["room_h_kjkg"]) if ref else (0.0, 0.0)
    out_t, out_h = float(c["outdoordb"]), float(c["outdoorh"])
    fan_kw = supply_leak / 1000.0 * float(c["fanpressure"]) / float(c["fanefficiency"]) / 1000.0

    def coil(oa: float) -> Dict[str, float]:
        oa_sens = c["airsensible"] * oa / 1000.0 * (out_t - room_t)
        oa_total = c["airenthalpyahu"] * oa / 1000.0 * (out_h - room_h)
        oa_lat = oa_total - oa_sens
        sens_kw = sens + oa_sens + fan_kw
        lat_kw = lat + oa_lat
        total = sens_kw + lat_kw
        dtw = float(c["chwreturnc"]) - float(c["chwsupplyc"])
        return {"oa_sensible_kw": oa_sens, "oa_latent_kw": oa_lat, "coil_sensible_kw": sens_kw,
                "coil_latent_kw": lat_kw, "coil_total_kw": total,
                "coil_tr": total / 3.517,
                "chw_lps": 1000.0 * total / (c["waterspecificheat"] * dtw) if dtw > 0 else None}

    by_method = {
        "HIGHEST_FRACTION": {"oa_lps": oa_highest, **coil(oa_highest)},
        "MULTI_SPACE": {"oa_lps": oa_rev_a, **coil(oa_rev_a)},
    }
    chosen = by_method[c["oamethod"]]
    oa = chosen["oa_lps"]
    base = supply_leak if c["oamethod"] == "HIGHEST_FRACTION" else supply
    return_air = base - oa
    return {
        "tag": tag, "rooms": [r["number"] for r in rooms], "room_count": len(rooms), "area_m2": area,
        "room_supply_sum_lps": supply, "duct_leakage": leak, "supply_lps": supply_leak,
        "oa_method": c["oamethod"], "oa_lps": oa, "oa_fraction": oa / base if base else 0.0,
        "highest_standard_room": crit["number"] if crit else None, "highest_oa_fraction": frac,
        "oa_sum_rooms_lps": sum_oa, "oa_multispace_vot_lps": vot, "oa_rev_a_lps": oa_rev_a,
        "oa_highest_fraction_lps": oa_highest,
        "return_lps": return_air, "exhaust_lps": exhaust, "positive_air_lps": base - return_air - exhaust,
        "room_sensible_kw": sens, "room_latent_kw": lat, "room_total_kw": sens + lat,
        "fan_heat_kw": fan_kw, "fan_temp_rise_k": fan_kw * 1000.0 / (c["airsensible"] * supply_leak) if supply_leak else 0.0,
        "reference_room": ref["number"] if ref else None, "reference_t_c": room_t, "reference_h_kjkg": room_h,
        **{k: v for k, v in chosen.items() if k != "oa_lps"},
        "by_oa_method": by_method,
        "oa_rule_candidates": oa_rule(supply_rooms, supply_leak, c),
    }


def oa_rule(rooms: List[Dict[str, Any]], supply_leak: float, c: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Rooms that would trigger the small-room outdoor-air rule (detection only, brief Section 8.2)."""
    found: List[Dict[str, Any]] = []
    left = list(rooms)
    while len(left) > 1:
        total = sum(r["supply_lps"] for r in left)
        top = max(left, key=lambda r: r["oa_fraction"])
        share = top["supply_lps"] / total * 100.0
        rest = [r for r in left if r is not top]
        drop = (top["oa_fraction"] - max(r["oa_fraction"] for r in rest)) * 100.0
        if share < c["oarulesharepct"] and drop > c["oaruledroppp"]:
            found.append({"room": top["number"], "share_of_ahu_pct": share, "fraction_drop_pp": drop,
                          "separable": top["pressure_regime"] in ("", "NEUTRAL") and not top["fixed_by_specification"]})
            left = rest
        else:
            break
    return found


# ---- driver --------------------------------------------------------------------------------- #
def recompute(data: Dict[str, Any], oa_method: Optional[str] = None) -> Dict[str, Any]:
    """Recompute every room and AHU of a department input (schema version 1)."""
    if data.get("schema_version") not in (None, 1):
        raise RecomputeError(f"unsupported schema_version {data.get('schema_version')}")
    c = read_constants(data)
    if oa_method:
        c["oamethod"] = oa_method.upper()
    tables = {
        "space_types": _table(data.get("space_types")), "activities": _table(data.get("activities")),
        "orientations": _table(data.get("orientations")), "defaults": _table(data.get("space_type_defaults")),
    }
    rooms_in = data.get("rooms") or []
    flags: List[Dict[str, str]] = []
    results: Dict[str, Dict[str, Any]] = {}
    exhaust_only: Dict[str, Dict[str, Any]] = {}
    for room in rooms_in:
        no = str(room["number"])
        if EXHAUST_ONLY.search(str(room.get("system_type") or "")):
            crit = room.get("criteria") or {}
            area, _ = select_area(room, c)
            vol = area * _z(room.get("ceiling_height_m"))
            spec = _num(crit.get("exhaust_specified_lps"))
            calc = max(_z(crit.get("exhaust_lps_m2")) * area, _z(crit.get("exhaust_ach")) * vol / 3.6)
            exhaust_only[no] = {"number": no, "name": room.get("name"), "ahu": room.get("ahu"),
                                "zone_tag": room.get("zone_tag"), "area_m2": area, "volume_m3": vol,
                                "exhaust_calc_lps": calc,
                                "exhaust_lps": spec if spec is not None else (_roundup(calc, c["supplyrounding"]) if calc > 0 else 0.0),
                                "supply_lps": 0.0, "oa_lps": 0.0, "sensible_w": 0.0, "latent_w": 0.0,
                                "exhaust_only": True}
            continue
        res = room_loads(room, c, tables)
        res.update({"number": no, "name": room.get("name"), "ahu": room.get("ahu"), "zone_tag": room.get("zone_tag"),
                    "storey": room.get("storey"), "exhaust_only": False})
        # nominal flows for the pressure solve: before any pressure offset
        _apply_airflow(res, c, None)
        res["supply_lps_nominal"], res["exhaust_lps_nominal"] = res["supply_lps"], res["exhaust_lps"]
        results[no] = res
    pres, unresolved = solve_pressure(rooms_in, {**results}, data.get("pressure_nodes") or [])
    for no in unresolved:
        flags.append({"type": "VERIFY", "ref": no, "text": "pressure direction only, no value: not calculated"})
    for no, res in results.items():
        _apply_airflow(res, c, pres.get(no))
        res["pressure"] = pres.get(no)
        if pres.get(no) and pres[no]["status"] in ("FAIL", "VERIFY", "UNSOLVED"):
            flags.append({"type": "VERIFY", "ref": no, "text": f"pressure ratio {pres[no]['ratio']}: {pres[no]['status']}"})
        if res["required_supply_temp_c"] is not None and res["required_supply_temp_c"] < c["supplytemp"] - 1e-9:
            flags.append({"type": "VERIFY", "ref": no, "text": "required supply temperature below design supply temperature"})
    by_ahu: Dict[str, List[Dict[str, Any]]] = {}
    for res in list(results.values()) + list(exhaust_only.values()):
        if res.get("ahu"):
            by_ahu.setdefault(str(res["ahu"]), []).append(res)
        else:
            flags.append({"type": "VERIFY", "ref": res["number"], "text": "room has no AHU"})
    ahus = {tag: ahu_totals(tag, rooms, c) for tag, rooms in by_ahu.items()}
    return {"department": data.get("department"), "oa_method": c["oamethod"],
            "rooms": list(results.values()) + list(exhaust_only.values()),
            "ahus": list(ahus.values()), "pressure_unresolved": unresolved, "flags": flags,
            "totals": _totals(results, ahus)}


def _totals(rooms: Dict[str, Dict[str, Any]], ahus: Dict[str, Dict[str, Any]]) -> Dict[str, float]:
    return {
        "area_m2": sum(r["area_m2"] for r in rooms.values()),
        "room_supply_lps": sum(r["supply_lps"] for r in rooms.values()),
        "room_sensible_kw": sum(r["sensible_w"] for r in rooms.values()) / 1000.0,
        "room_latent_kw": sum(r["latent_w"] for r in rooms.values()) / 1000.0,
        "ahu_supply_lps": sum(a["supply_lps"] for a in ahus.values()),
        "ahu_oa_lps": sum(a["oa_lps"] for a in ahus.values()),
        "coil_total_kw": sum(a["coil_total_kw"] for a in ahus.values()),
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Independent thermal-load recomputation (JSON in, JSON out)")
    ap.add_argument("input", type=Path, help="department input JSON (src/workbook/schema.md)")
    ap.add_argument("--out", type=Path, help="write JSON here (default stdout)")
    ap.add_argument("--oa-method", choices=["HIGHEST_FRACTION", "MULTI_SPACE"])
    ns = ap.parse_args(argv)
    result = recompute(json.loads(ns.input.read_text(encoding="utf-8")), ns.oa_method)
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if ns.out:
        ns.out.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
