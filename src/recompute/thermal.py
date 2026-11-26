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
    "fculeakageducted": 0.10,      # D-060: ducted fan coil unit
    "fculeakagehiwall": 0.0,       # D-060: hi-wall (non-ducted) fan coil unit
    "designtref": 24.0,            # D-053: reference design dry-bulb
    "designrhmax": 0.60,           # D-053: upper bound of the design humidity
    "psya": 0.61094,               # saturation pressure kPa = A exp(B t / (C + t)) (Magnus form)
    "psyb": 17.625,
    "psyc": 243.04,
    "psyp": 101.325,               # kPa
    "psyeps": 0.622,
    "psycpa": 1.006,               # kJ/(kg K)
    "psycpv": 1.86,
    "psyhfg": 2501.0,              # kJ/kg
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


def _round_half_up(value: float, ndigits: int = 0) -> float:
    """Spreadsheet ROUND: halves go away from zero (Python's round() takes 10.25 to 10.2, the spreadsheet to 10.3)."""
    f = 10.0 ** ndigits
    x = abs(value) * f
    return math.copysign(math.floor(x + 0.5 + 1e-9) / f, value)


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



# ---- design condition (D-053) --------------------------------------------------------------- #
def design_condition(crit: Dict[str, Any], c: Dict[str, Any]) -> Optional[Tuple[float, float, float]]:
    """(dry-bulb degC, humidity fraction, enthalpy kJ/kg) from the printed criteria range, or None where the row prints no temperature.
    Dry-bulb: the printed set point where there is one; else the reference value 24 degC limited to the printed range
    (24 inside the range, the upper limit where the range is below 24, the lower limit where it is above).
    Humidity: the smaller of 60 % and the printed maximum."""
    tset, tmin, tmax = _num(crit.get("temp_setpoint_c")), _num(crit.get("temp_min_c")), _num(crit.get("temp_max_c"))
    if tset is None and tmin is None and tmax is None:
        return None
    if tset is not None:
        t = tset
    else:
        lo = tmax if tmin is None else tmin
        hi = tmin if tmax is None else tmax
        t = min(max(c["designtref"], lo), hi)
    rh_max = _num(crit.get("rh_max_pct"))
    rh = c["designrhmax"] if rh_max is None else min(c["designrhmax"], rh_max / 100.0)
    pv = rh * c["psya"] * math.exp(c["psyb"] * t / (c["psyc"] + t))
    w = c["psyeps"] * pv / (c["psyp"] - pv)
    h = c["psycpa"] * t + w * (c["psyhfg"] + c["psycpv"] * t)
    return t, rh, h


# ---- single-pass class (D-031) -------------------------------------------------------------- #
_CLEAN_WORDS = re.compile(r"isolation|protective|theatre|operating|clean|steril|aseptic|compounding|cleanroom|hepa|laminar|cath|angio|ucv", re.I)
_DIRTY_WORDS = re.compile(r"dirty|soiled|disposal|decontam|toilet|\bwc\b|ensuite|shower|bathroom|cleaner|sluice|waste|pan room|washroom|latrine|janitor|bedpan|linen", re.I)


def classify_single_pass(name: str, crit: Dict[str, Any]) -> str:
    """CLEAN, DIRTY or '' (not a single-pass room). A stated class (``sp_class``) wins.
    CLEAN: needs 100 % outdoor air for clean or sterile supply (specified 100 % outdoor air, air class 1, HEPA, theatre, isolation words).
    DIRTY: single pass for exhaust reasons only (negative or neutral, no sterile need, dirty-room words): recirculated supply, dedicated exhaust."""
    if str(crit.get("single_pass") or "").upper() != "Y":
        return ""
    given = str(crit.get("sp_class") or "").upper()
    if given in ("CLEAN", "DIRTY"):
        return given
    sup, oa = _num(crit.get("supply_specified_lps")), _num(crit.get("oa_specified_lps"))
    if str(crit.get("code") or "").upper() == "S" and sup and oa is not None and oa >= 0.99 * sup:
        return "CLEAN"
    if str(crit.get("air_class") or "").strip() == "1" or "HEPA" in str(crit.get("filter") or "").upper():
        return "CLEAN"
    if _DIRTY_WORDS.search(name or ""):
        return "DIRTY"
    if _CLEAN_WORDS.search(name or ""):
        return "CLEAN"
    regime = str(crit.get("pressure_regime") or "").upper()
    return "DIRTY" if regime in ("", "NEGATIVE", "NEUTRAL") else "CLEAN"


# ---- room geometry and loads ---------------------------------------------------------------- #
def select_area(room: Dict[str, Any], c: Dict[str, Any]) -> Tuple[float, str]:
    """Area used (rounded to 0.1 m2) and the basis; brief 8.3 and Section 11 item 18."""
    soa_true, meas = _num(room.get("soa_area_m2")), _num(room.get("measured_area_m2"))
    soa = soa_true
    dpa = _num(room.get("dpa_area_m2"))
    sel = str(room.get("area_selector") or "AUTO").upper()
    if sel == "DPA" and dpa is not None:
        return _round_half_up(dpa, 1), "DPA"
    if sel == "SOA" and soa_true is not None:
        return _round_half_up(soa_true, 1), "SOA"           # D-067: the SOA as drawn, where the rule selects it
    if dpa is not None and dpa > 0:
        soa = dpa                                    # the design area: DPA designed area, else SOA
    if soa is None and meas is None:
        raise RecomputeError(f"room {room.get('number')}: no area")
    if soa is None:
        return _round_half_up(meas, 1), "MEASURED"
    if meas is None:
        return _round_half_up(soa, 1), "SOA"
    if sel == "SOA":
        return _round_half_up(soa, 1), "SOA"
    if sel == "MEASURED":
        return _round_half_up(meas, 1), "MEASURED"
    if sel == "LARGER":
        return _round_half_up(max(soa, meas), 1), "LARGER"
    base_r = _round_half_up(soa, 1)                 # the design area is taken to 0.1 m2 before the test, as on Room Information
    diff = abs(base_r - meas)
    if diff <= c["area_agree_m2"] or (base_r and diff / base_r * 100.0 <= c["area_agree_pct"]):
        return _round_half_up(soa, 1), "SOA"
    return _round_half_up(meas, 1), "MEASURED"


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
    dc = design_condition(crit, c)                      # D-053: per-room design condition from the criteria range
    design_rh = None
    if dc is not None:
        room_t, design_rh, room_h = dc
    defaults = tables["defaults"].get(st_name, {})

    people = _num(room.get("occupancy_people"))
    if people is not None and str(room.get("occupancy_basis") or "").upper() == "RDS":
        people = float(math.ceil(round(people, 9)))          # RDS count (D-016): a fraction of a person is rounded up
    if people is None:
        dens = _num(room.get("occupancy_per_100m2"))
        if dens is None:
            dens = _num(defaults.get("density_per_100m2"))
        people = (dens or 0.0) * area / 100.0
        if str(room.get("occupancy_basis") or "").upper() == "DENSITY DEFAULT":
            people = float(math.ceil(round(people, 9)))      # default density (D-017): rounded up to a whole person
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
    # D-110: no occupant diversity (IOF) on outdoor air
    oa_req = max(people * oa_pp, _z(crit.get("oa_lps_per_m2")) * area, _z(crit.get("min_oa_ach")) * vol / 3.6, _z(crit.get("oa_specified_lps")))
    t1 = crit.get("ss553_t1") or {}                                   # D-111: SS 553 Table 1 / 62.1 / cl 5.3.5 exemption
    if t1.get("kind") == "COLD ROOM (cl 5.3.5)" or (t1.get("kind") == "STORE OR RISER (cl 5.3.5)" and area < 6):
        oa_req = 0.0
    elif _z(t1.get("lpm2")) + _z(t1.get("lpp")) > 0:
        oa_req = _z(t1.get("lpp")) * people + _z(t1.get("lpm2")) * area
    # D-110 outdoor-air scope: HFDS / ASHRAE 170 (air changes or specified flow, no per-person or per-area rate, no 'Refer to SS553') or SS 553
    if _z(crit.get("min_oa_ach")) + _z(crit.get("oa_specified_lps")) + oa_pp + _z(crit.get("oa_lps_per_m2")) == 0:
        oa_scope = ""
    elif not crit.get("oa_ss553") and oa_pp == 0 and _z(crit.get("oa_lps_per_m2")) == 0:
        oa_scope = "HFDS/170"
    else:
        oa_scope = "SS 553"
    min_supply = max(_z(crit.get("supply_lps_m2")) * area, _z(crit.get("min_total_ach")) * vol / 3.6,
                     _z(crit.get("supply_specified_lps")), oa_req)     # the outdoor air is a floor of the supply
    exh_calc = max(_z(crit.get("exhaust_lps_m2")) * area, _z(crit.get("exhaust_ach")) * vol / 3.6)
    exh_spec = _num(crit.get("exhaust_specified_lps"))
    # the example rounds the calculated exhaust up once, like the supply: AG = ROUNDUP(AF, -1)
    exhaust_req = exh_spec if exh_spec is not None else (_roundup(exh_calc, c["supplyrounding"]) if exh_calc > 0 else 0.0)

    dt = room_t - float(c["supplytemp"])
    if dt <= 0:
        raise RecomputeError(f"room {room.get('number')}: room temperature {room_t} not above supply {c['supplytemp']}")
    thermal = sensible / (c["airsensible"] * dt)
    # D-053: information only, thermal airflow at the lower limit of the printed range
    t_low = _num(crit.get("temp_min_c"))
    thermal_lo = sensible / (c["airsensible"] * (t_low - float(c["supplytemp"]))) if (t_low is not None and t_low > float(c["supplytemp"]) and dc is not None) else None

    return {
        "area_m2": area, "area_basis": area_basis, "height_m": height, "volume_m3": vol,
        "room_t_c": room_t, "room_h_kjkg": room_h, "people": people, "iof": iof, "oa_scope": oa_scope,
        "people_sensible_w": people_sens, "lighting_w": lighting, "power_w": power, "glazing_w": glass,
        "transmission_w": transmission, "infiltration_lps": infil_lps, "infiltration_sensible_w": infil_sens,
        "people_latent_w": people_lat, "equipment_latent_w": equip_lat, "infiltration_latent_w": infil_lat,
        "sensible_w": sensible, "latent_w": latent,
        "thermal_airflow_lps": thermal, "min_supply_lps": min_supply, "oa_required_lps": oa_req,
        "exhaust_required_lps": exhaust_req, "exhaust_specified": exh_spec is not None,
        "supply_temp_c": float(c["supplytemp"]), "pressure_regime": regime,
        "fixed_by_specification": str(crit.get("code") or "").upper() == "S",
        "single_pass": str(crit.get("single_pass") or "").upper() == "Y",
        "sp_class": classify_single_pass(str(room.get("name") or ""), crit),
        "design_rh": design_rh, "thermal_lo_lps": thermal_lo,
        "design_range": (_num(crit.get("temp_min_c")), _num(crit.get("temp_max_c"))),
        "specified_supply_lps": _num(crit.get("supply_specified_lps")),
        "specified_oa_lps": _num(crit.get("oa_specified_lps")),
    }


def _pressure_target(room: Dict[str, Any]) -> Optional[float]:
    """Tender value, then working drawing, then booklet (brief 8.3 / schema), as printed (signed); the workbook reads the same cell."""
    crit = room.get("criteria") or {}
    for key in ("pressure_pa_tender", "pressure_pa_working", "pressure_pa_booklet"):
        v = _num(crit.get(key))
        if v is not None:
            return v
    return None


# ---- pressure suites ------------------------------------------------------------------------ #
PERIMETER_FACTOR = 4.0     # D-028: perimeter of a room with no measured outline = factor x square root of the area (the named cell PerimFactor of the workbook)
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
                if p.get("perimeter_m") is not None:
                    perimeter = float(p["perimeter_m"])
                elif p.get("perimeter_rule") == "factor":
                    perimeter = PERIMETER_FACTOR * math.sqrt(area)            # D-028: no measured outline, perimeter = 4 x square root of the area used
                else:
                    raise RecomputeError(f"room {no}: no perimeter and no surface override")
                surface = pm.internal_surface(area, perimeter, height)
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
# D-030: no supply floor from exhaust where the room has no pressure offset (the exhaust is made up by transfer air).
# D-031: only a clean single-pass room (100 % outdoor air) takes all its supply as outdoor air; a dirty single-pass room is
#        supplied from recirculated air and has a dedicated exhaust (no return); it carries only its own minimum outdoor air.
# D-032: the exhaust and the offset air of every room are made up at the AHU (see ahu_totals), not in the room.
# D-033: outdoor air per person or per area is a floor on the room supply (oa_req inside min_supply).
WORKBOOK_RULES: Dict[str, str] = {}      # kept for callers: there is one reading now


def _apply_airflow(res: Dict[str, Any], c: Dict[str, Any], pres: Optional[Dict[str, Any]], rules: Optional[Dict[str, str]] = None) -> None:
    """Final supply, extract, exhaust, return and outdoor air of one supply room, with the governing case (brief 8.3)."""
    thermal, minimum = res["thermal_airflow_lps"], res["min_supply_lps"]
    exh_req = res["exhaust_required_lps"]
    offset = 0.0
    kind = ""
    if pres and pres["mode"] == "controlled" and pres["status"] not in ("UNSOLVED",):
        offset, kind = pres["offset_needed_lps"], pres["offset_kind"]
    if kind == "exhaust":        # negative room: extract = supply + offset
        floor = max(exh_req - offset, 0.0)
    elif kind == "supply":       # positive room: extract = supply - offset
        floor = exh_req + offset
    else:
        floor = 0.0              # D-030
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
    supply = supply_raw if governing == "specified" else _roundup(supply_raw, step)
    if res["fixed_by_specification"] and res["exhaust_specified"] and exh_req > 0:
        extract = exh_req         # specified airflows are not resized; the offset needed is reported beside them
    elif kind == "exhaust":
        extract = supply + offset
    elif kind == "supply":
        extract = supply - offset
    else:
        extract = max(supply, exh_req)
    if res["single_pass"]:
        exhaust = extract         # no return from a single-pass room (clean or dirty)
    elif kind == "exhaust":
        exhaust = max(exh_req, offset)
    else:
        exhaust = exh_req
    oa = res["oa_required_lps"]
    if res["fixed_by_specification"] and res["specified_oa_lps"] is not None and res["specified_oa_lps"] > 0:
        oa = res["specified_oa_lps"]
    elif res["single_pass"] and res["sp_class"] == "CLEAN":
        oa = supply               # D-031: clean or sterile supply is 100 % outdoor air
    oa = min(oa, supply) if supply > 0 else 0.0
    if not res.get("oa_scope") and oa > 0 and (res["fixed_by_specification"] or (res["single_pass"] and res["sp_class"] == "CLEAN")):
        res["oa_scope"] = "HFDS/170"
    res.update({
        "supply_lps_unrounded": supply_raw, "supply_lps": supply, "extract_lps": extract, "exhaust_lps": exhaust,
        "return_lps": extract - exhaust,
        "offset_lps": offset, "offset_kind": kind, "oa_lps": oa,
        "offset_specified_lps": ((extract - supply) if kind == "exhaust" else (supply - extract))
        if (res["fixed_by_specification"] and kind) else None,
        "oa_fraction": oa / supply if supply > 0 else 0.0,
        "governing_case": governing,
        "case_margin_thermal_vs_air_change_lps": thermal - minimum,
        "achieved_ach": supply * 3.6 / res["volume_m3"] if res["volume_m3"] else None,
        "required_supply_temp_c": res["room_t_c"] - res["sensible_w"] / (c["airsensible"] * supply) if supply else None,
    })


# ---- AHU totalisation ----------------------------------------------------------------------- #
def unit_leakage(unit: Optional[Dict[str, Any]], c: Dict[str, Any]) -> float:
    """Duct leakage of the unit (D-060): 10 % on an AHU or a ducted fan coil unit, 0 % on a hi-wall fan coil unit."""
    if unit and str(unit.get("type") or "").upper() in ("FCU", "DXFCU"):
        return c["fculeakagehiwall"] if str(unit.get("ducted") or "").upper() == "N" else c["fculeakageducted"]
    return c["ductleakage"]


def ahu_totals(tag: str, rooms: List[Dict[str, Any]], c: Dict[str, Any], unit: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    supply_rooms = [r for r in rooms if r["supply_lps"] > 0]
    leak = unit_leakage(unit, c)
    supply = sum(r["supply_lps"] for r in supply_rooms)
    spill_req = float((unit or {}).get("spill_req_lps") or 0.0)         # D-036, D-062: spill air to the plant rooms that the unit serves
    ucv_add = float((unit or {}).get("ucv_add_lps") or 0.0)           # D-058: future ultra-clean duty minus the day-1 supply of the theatre
    supply_leak = (supply + spill_req + ucv_add) * (1.0 + leak)
    sum_oa = sum(r["oa_lps"] for r in supply_rooms)
    exhaust = sum(r["exhaust_lps"] for r in rooms)
    sens = sum(r["sensible_w"] for r in supply_rooms) / 1000.0
    lat = sum(r["latent_w"] for r in supply_rooms) / 1000.0
    area = sum(r["area_m2"] for r in supply_rooms)

    crit = max(supply_rooms, key=lambda r: r["oa_fraction"], default=None)
    frac = crit["oa_fraction"] if crit else 0.0
    oa_highest = frac * supply_leak
    # D-032: the air that leaves the zone (exhaust with no return, net positive offset air) is made up from outdoor air
    off_net = sum(r.get("offset_lps", 0.0) * (1.0 if r.get("offset_kind") == "supply" else -1.0 if r.get("offset_kind") == "exhaust" else 0.0) for r in rooms)
    oa_balance = exhaust + max(off_net, 0.0) + spill_req
    # D-110: HFDS/170 unit = straight sum of the room outdoor air; SS 553 and mixed units stay on the fraction basis until SS 553 Table 1 is set
    oa_clin = sum(r["oa_lps"] for r in supply_rooms if r.get("oa_scope") == "HFDS/170")
    oa_nc = sum(r["oa_lps"] for r in supply_rooms if r.get("oa_scope") == "SS 553")
    ahu_class = "MIXED" if oa_clin > 0 and oa_nc > 0 else ("HFDS/170" if oa_clin > 0 else ("SS 553" if oa_nc > 0 else ""))
    oa_min = oa_clin + oa_nc                                          # D-111
    oa_need = max(oa_min, oa_balance)
    oa_short = max(oa_need - supply_leak, 0.0)
    oa_final = min(oa_need, supply_leak)
    dirty = [r for r in supply_rooms if r.get("sp_class") == "DIRTY"]
    # multiple-space (Rev A AHU Summary S8..AA8): Vot = Z x Vst, Z = X/(1+X-Y)
    x = sum_oa / supply if supply else 0.0
    y = frac
    vot = x / (1.0 + x - y) * supply if supply else 0.0
    oa_rev_a = max(sum_oa, vot)
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
        "HIGHEST_FRACTION": {"oa_lps": oa_final, **coil(oa_final)},
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
        "oa_highest_fraction_lps": oa_highest, "oa_clin_lps": oa_clin, "oa_nc_lps": oa_nc, "ahu_class": ahu_class, "oa_min_lps": oa_min,
        "oa_balance_lps": oa_balance, "spill_req_lps": spill_req, "oa_short_lps": oa_short,
        "offset_net_lps": off_net,
        "return_lps": return_air, "recirculated_lps": return_air, "exhaust_lps": exhaust,
        "positive_air_lps": base - return_air - exhaust, "spill_lps": oa - exhaust - off_net,
        "dirty_single_pass_rooms": [r["number"] for r in dirty], "dirty_single_pass_supply_lps": sum(r["supply_lps"] for r in dirty),
        "dirty_single_pass_exhaust_lps": sum(r["exhaust_lps"] for r in dirty),
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
def recompute(data: Dict[str, Any], oa_method: Optional[str] = None, rules: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
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
        _apply_airflow(res, c, None, rules)
        res["supply_lps_nominal"], res["exhaust_lps_nominal"] = res["supply_lps"], res["exhaust_lps"]
        results[no] = res
    pres, unresolved = solve_pressure(rooms_in, {**results}, data.get("pressure_nodes") or [])
    for no in unresolved:
        flags.append({"type": "VERIFY", "ref": no, "text": "pressure direction only, no value: not calculated"})
    for no, res in results.items():
        _apply_airflow(res, c, pres.get(no), rules)
        res["pressure"] = pres.get(no)
        if pres.get(no) and pres[no]["status"] in ("FAIL", "VERIFY", "UNSOLVED"):
            flags.append({"type": "VERIFY", "ref": no, "text": f"pressure ratio {pres[no]['ratio']}: {pres[no]['status']}"})
        if res.get("thermal_lo_lps") is not None and res["thermal_lo_lps"] > res["supply_lps"] + 1e-9:
            flags.append({"type": "VERIFY", "ref": no, "text": "thermal airflow at the lower limit of the temperature range is above the final supply"})
        lo, hi = res["design_range"]
        if lo is not None or hi is not None:
            lo_, hi_ = (hi if lo is None else lo), (lo if hi is None else hi)
            if not (lo_ - 1e-9 <= res["room_t_c"] <= hi_ + 1e-9) and not res["fixed_by_specification"]:
                flags.append({"type": "FAIL", "ref": no, "text": "design temperature is outside the criteria range"})
        if res["required_supply_temp_c"] is not None and res["required_supply_temp_c"] < c["supplytemp"] - 1e-9:
            flags.append({"type": "VERIFY", "ref": no, "text": "required supply temperature below design supply temperature"})
    by_ahu: Dict[str, List[Dict[str, Any]]] = {}
    for res in list(results.values()) + list(exhaust_only.values()):
        if res.get("ahu"):
            by_ahu.setdefault(str(res["ahu"]), []).append(res)
        else:
            flags.append({"type": "VERIFY", "ref": res["number"], "text": "room has no AHU"})
    unit_by_tag = {str(u.get('tag')): u for u in (data.get('ahus') or [])}
    ahus = {tag: ahu_totals(tag, rooms, c, unit_by_tag.get(tag)) for tag, rooms in by_ahu.items()}
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
