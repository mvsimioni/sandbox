"""EnergyPlus model of the IP block LB3-L07 from the AHU zoning and the thermal-load workbooks (26 Nov 2026, D-109).

Thermal zones: one EnergyPlus zone per storey and unit (AHU, FCU, CRAC) of the zoning (out/registers/zone_assignment_<ST>.csv, after D-108).
Rooms that are MV, NV, not conditioned or without information are not modelled; their boundaries with the zones are adiabatic.

Geometry: the zoning outlines of the rooms of a zone (zoning_building_mm_wkt, else polygon_building_mm_wkt; building frame X along the
numbered grids, Y along the lettered grids = north, origin grid 2/A) joined, simplified (0.25 m) and split into hole-free pieces (spaces);
each piece is extruded over the floor-to-floor height of its storey (floor levels printed on the zone plans). A wall is exterior where it
lies on the storey footprint boundary (all rooms of the storey joined, gaps under 1 m closed; courtyards included); other walls are
adiabatic. A ceiling is a roof where more than half of it is not under the footprint of the storey above. Basement exterior walls and floors
are adiabatic (assumption). Windows: window-to-wall ratio and shading coefficient (SC1 x SC2) of the facade orientation from the ETTV report;
glazing U 1.65, SHGC = 0.87 x SC. Walls: area-weighted opaque U of the orientation (ETTV).

Loads: per room from the 'Level xx Room Zones' sheets of the issued workbooks (people and W per person, lighting W, small power W,
equipment sensible and latent W, infiltration L/s, design temperature), summed per zone; outdoor air from the AHU tab (section B, outdoor air
of the unit) shared over its zones by room supply. Schedules: always on (24 h hospital), placeholder. HVAC: ideal loads air system per zone,
cooling setpoint = supply-weighted design room temperature, dehumidifying humidistat at the design RH maximum (60 %).

Design days: SS 553:2026 cl. 5.4.4 cooling (33 / 27 degC) and the ASHRAE 0.4 % dehumidification point (27.2 degC dew point, 29.4 degC
MCDB, long-record Changi) with the date and sky of the cooling day (assumption).

``python3 -m src.sim.eplus_model --issue out/issue/DRAFT_2026-11-26_R11`` writes out/sim/AIGH_IP.osm, .idf and zone_inputs.csv.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import re
from collections import defaultdict
from pathlib import Path

import openpyxl
import openstudio as os_
from shapely import wkt
from shapely.geometry import LineString, MultiPolygon, Polygon, box
from shapely.ops import split, unary_union

ROOT = Path(__file__).resolve().parent.parent.parent
REG = ROOT / "out/registers"
OUT = ROOT / "out/sim"
csv.field_size_limit(1 << 30)

# FFL (m) printed on the zone plans (data/uploads/arch_20261126/IP_zone, most frequent FFL string); L08 from the elevation register
FFL = {"LB3": 0.0, "B3": 3.8, "B2": 8.3, "B1": 11.6, "L01": 17.0, "L02": 23.0, "L03": 29.0, "L04": 35.0, "L05": 41.0, "L06": 47.0,
       "L07": 53.0, "L08": 59.0}
ORDER = ["LB3", "B3", "B2", "B1", "L01", "L02", "L03", "L04", "L05", "L06", "L07"]
BASEMENT = {"LB3", "B3", "B2", "B1"}
UNIT_KINDS = ("AHU", "FCU", "CRAC")

# labelled assumptions (no project value)
ROOF_U = 0.5          # W/m2K, ASSUMPTION: no roof U in the project documents (ETTV covers walls); typical concrete roof with insulation
SHGC_PER_SC = 0.87    # SHGC = 0.87 x SC (definition of the shading coefficient)
GLAZING_U = 1.65      # W/m2K, ETTV F1 vision glazing (all orientations)


def _read(p):
    with open(p, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------------------------------------------------------- loads from the workbooks
HEAD = {"AHU or system": "unit", "Room number": "room", "Room area": "area", "People": None, "Sensible per person": "sp", "Latent per person": "lp",
        "Lighting": None, "Power": "power", "Equipment": None, "Infiltration": None, "Design room temperature": "t_design",
        "Final room supply, rounded up": "supply", "Minimum outdoor air": "oa_min", "Level": "storey", "Room name": "name"}


def room_loads(issue: Path):
    rows = []
    for f in sorted(glob.glob(str(issue / "workbooks/AIGH_C2_D*.xlsx"))):
        wb = openpyxl.load_workbook(f, data_only=True, read_only=True)
        for ws in wb.worksheets:
            if not ws.title.endswith("Room Zones"):
                continue
            hdr = [c.value for c in next(ws.iter_rows(min_row=5, max_row=5))]
            units = [c.value for c in next(ws.iter_rows(min_row=6, max_row=6))]
            col = defaultdict(list)
            for i, (h, u) in enumerate(zip(hdr, units)):
                if h:
                    col[(h, u)].append(i)
            g = lambda r, h, u, k=0: (r[col[(h, u)][k]] if col.get((h, u)) and len(col[(h, u)]) > k else None)
            for r in ws.iter_rows(min_row=7, values_only=True):
                if not r or not r[col[("Room number", None)][0]] or not r[col[("AHU or system", None)][0]]:
                    continue
                num = lambda v: float(v) if isinstance(v, (int, float)) else 0.0
                rows.append(dict(
                    workbook=Path(f).name[8:11], storey=g(r, "Level", None), unit=str(g(r, "AHU or system", None)), room=g(r, "Room number", None),
                    name=g(r, "Room name", None), area=num(g(r, "Room area", "m²")), height=num(g(r, "Ceiling height", "m")),
                    people=num(g(r, "People", "persons")), sens_pp=num(g(r, "Sensible per person", "W")), lat_pp=num(g(r, "Latent per person", "W")),
                    lights_w=num(g(r, "Lighting", "W")), power_w=num(g(r, "Power", "W")), equip_s_w=num(g(r, "Equipment", "W", 0)),
                    equip_l_w=num(g(r, "Equipment", "W", 1)), infil_lps=num(g(r, "Infiltration", "L/s")),
                    t_design=num(g(r, "Design room temperature", "°C")), supply_lps=num(g(r, "Final room supply, rounded up", "L/s")),
                    oa_min_lps=num(g(r, "Minimum outdoor air", "L/s"))))
    return rows


def ahu_outdoor_air(issue: Path):
    oa = {}
    for f in sorted(glob.glob(str(issue / "workbooks/AIGH_C2_D*.xlsx"))):
        ws = openpyxl.load_workbook(f, data_only=True, read_only=True)["AHU"]
        rows = list(ws.iter_rows(values_only=True))
        sec_b = next((i for i, r in enumerate(rows) if r and str(r[0] or "").startswith("B. ")), None)
        if sec_b is None:
            continue
        for r in rows[sec_b + 3:]:
            if not r or not r[0] or r[0] == "TOTAL" or str(r[0]).startswith(("C. ", "D. ")):
                if r and str(r[0] or "").startswith(("C. ", "TOTAL")):
                    break
                continue
            if isinstance(r[13], (int, float)):
                oa[str(r[0])] = float(r[13])
    return oa


# ------------------------------------------------------------------------------------------------------------------------------ geometry
def _pieces(g, min_area=4.0):
    """hole-free polygons of g (m), each larger than min_area."""
    out, todo = [], [x for x in getattr(g, "geoms", [g]) if x.geom_type == "Polygon"]
    while todo:
        p = todo.pop()
        if p.area < min_area:
            continue
        if not p.interiors:
            out.append(p)
            continue
        c = Polygon(p.interiors[0]).centroid
        minx, miny, maxx, maxy = p.bounds
        parts = split(p, LineString([(c.x, miny - 1), (c.x, maxy + 1)]))
        if len(parts.geoms) < 2:
            parts = split(p, LineString([(minx - 1, c.y), (maxx + 1, c.y)]))
        if len(parts.geoms) < 2:
            out.append(Polygon(p.exterior))
            continue
        todo += [x for x in parts.geoms if x.geom_type == "Polygon"]
    return out


def zones_geometry():
    """(storey, unit) -> list of polygons in m; storey -> footprint."""
    zones, foot = {}, {}
    for st in ORDER:
        za = {z["room_id"]: z for z in _read(REG / f"zone_assignment_{st}.csv")}
        by_unit, allp = defaultdict(list), []
        for p in _read(REG / f"room_polygons_{st}.csv"):
            w = p.get("zoning_building_mm_wkt") or p.get("polygon_building_mm_wkt")
            if not w:
                continue
            try:
                g = wkt.loads(w)
            except Exception:
                continue
            g = g.buffer(0)
            if g.is_empty:
                continue
            g = MultiPolygon([Polygon([(x / 1000, y / 1000) for x, y in q.exterior.coords], [[(x / 1000, y / 1000) for x, y in i.coords] for i in q.interiors])
                              for q in getattr(g, "geoms", [g]) if q.geom_type == "Polygon"]).buffer(0)
            allp.append(g)
            z = za.get(p["room_id"])
            if z and z["class_kind"] in UNIT_KINDS:
                by_unit[z["class"]].append(g)
        if not allp:
            continue
        foot[st] = unary_union(allp).buffer(0.5).buffer(-0.5)
        for u, gs in by_unit.items():
            g = unary_union(gs).buffer(0.15).buffer(-0.15).simplify(0.25, preserve_topology=True)
            zones[(st, u)] = _pieces(g)
    return zones, foot


def _ettv():
    """orientation -> (wwr, wall U, SC)."""
    out = {}
    for r in _read(REG / "ettv_facades.csv"):
        if r["row_type"] == "facade" and r["orientation"]:
            f = lambda k: float(r[k]) if r[k] else None
            out[r["orientation"]] = (f("wwr"), f("wall_u_area_weighted"), f("sc_area_weighted"))
    return out


ORIENT = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _orient(az):
    return ORIENT[int(((az % 360) + 22.5) // 45) % 8]


# ----------------------------------------------------------------------------------------------------------------------------- model
def build(issue: Path):
    OUT.mkdir(parents=True, exist_ok=True)
    loads = room_loads(issue)
    oa_unit = ahu_outdoor_air(issue)
    zones, foot = zones_geometry()
    ettv = _ettv()
    m = os_.model.Model()
    m.getBuilding().setName("AIGH Inpatient block LB3-L07")
    m.getBuilding().setNorthAxis(0.0)
    site = m.getSite()
    site.setName("Singapore Changi (placeholder site; confirm with the EPW)")
    site.setLatitude(1.37)
    site.setLongitude(103.98)
    site.setTimeZone(8.0)
    site.setElevation(8.0)
    sim = m.getSimulationControl()
    sim.setDoZoneSizingCalculation(True)
    sim.setRunSimulationforSizingPeriods(True)
    sim.setRunSimulationforWeatherFileRunPeriods(False)
    m.getTimestep().setNumberOfTimestepsPerHour(4)

    # constructions
    def mat_nomass(name, r):
        mm = os_.model.MasslessOpaqueMaterial(m, "Smooth", max(r, 0.01))
        mm.setName(name)
        return mm

    def conc(name, t):
        c = os_.model.StandardOpaqueMaterial(m, "MediumRough", t, 1.442, 2400.0, 840.0)
        c.setName(name)
        return c

    wall_cons = {}
    for o, (wwr, u, sc) in ettv.items():
        u = u or 0.58
        r = 1 / u - 0.044 - 0.12 - 0.075 / 1.442
        c = os_.model.Construction(m)
        c.setName(f"Ext wall {o} U{u:.3f} (ETTV)")
        c.setLayers(os_.model.MaterialVector([conc(f"Concrete 75 {o}", 0.075), mat_nomass(f"Wall insulation {o}", r)]))
        wall_cons[o] = c
    roof = os_.model.Construction(m)
    roof.setName(f"Roof U{ROOF_U} (ASSUMPTION)")
    roof.setLayers(os_.model.MaterialVector([mat_nomass("Roof insulation", 1 / ROOF_U - 0.04 - 0.10 - 0.15 / 1.442), conc("Roof slab 150", 0.15)]))
    slab = os_.model.Construction(m)
    slab.setName("Internal slab 200 (adiabatic)")
    slab.setLayers(os_.model.MaterialVector([conc("Slab 200", 0.2)]))
    part = os_.model.Construction(m)
    part.setName("Partition (adiabatic)")
    part.setLayers(os_.model.MaterialVector([conc("Partition 100", 0.1)]))
    glz = {}
    for o, (wwr, u, sc) in ettv.items():
        g = os_.model.SimpleGlazing(m, GLAZING_U, round(SHGC_PER_SC * (sc or 0.2), 4))
        g.setVisibleTransmittance(0.4)
        g.setName(f"Glazing {o} SC{sc}")
        c = os_.model.Construction(m)
        c.setName(f"Window {o} (ETTV SC {sc})")
        c.setLayers(os_.model.MaterialVector([g]))
        glz[o] = c

    always = m.alwaysOnDiscreteSchedule()

    def const_sched(name, v, typ="Temperature"):
        s = os_.model.ScheduleConstant(m)
        s.setName(name)
        s.setValue(v)
        return s

    activity = {}

    def act_sched(w):
        if w not in activity:
            activity[w] = const_sched(f"Activity {w:.0f} W", w)
        return activity[w]

    stories = {}
    for st in ORDER:
        s = os_.model.BuildingStory(m)
        s.setName(st)
        s.setNominalZCoordinate(FFL[st])
        s.setNominalFloortoFloorHeight(FFL[ORDER[ORDER.index(st) + 1]] - FFL[st] if st != "L07" else FFL["L08"] - FFL["L07"])
        stories[st] = s

    agg = defaultdict(lambda: defaultdict(float))
    rooms_of = defaultdict(list)
    for r in loads:
        k = (r["storey"], r["unit"])
        rooms_of[k].append(r["room"])
        a = agg[k]
        for f in ("area", "people", "lights_w", "power_w", "equip_s_w", "equip_l_w", "infil_lps", "supply_lps", "oa_min_lps"):
            a[f] += r[f]
        a["sens_people_w"] += r["people"] * r["sens_pp"]
        a["lat_people_w"] += r["people"] * r["lat_pp"]
        a["t_sum"] += r["t_design"] * max(r["supply_lps"], 1)
        a["t_w"] += max(r["supply_lps"], 1)
    unit_supply = defaultdict(float)
    for (st, u), a in agg.items():
        unit_supply[u] += a["supply_lps"]

    zinfo, skipped = [], []
    for (st, u), polys in sorted(zones.items()):
        a = agg.get((st, u))
        if not polys:
            skipped.append((st, u, "no outline"))
            continue
        z = os_.model.ThermalZone(m)
        zname = f"{st} {u}"[:95]
        z.setName(zname)
        h = stories[st].nominalFloortoFloorHeight().get()
        above = foot.get(ORDER[ORDER.index(st) + 1]) if st != "L07" else None
        fp = foot[st]
        bnd = fp.boundary
        area = 0.0
        for i, p in enumerate(polys):
            pts = list(p.exterior.coords)[:-1]
            if Polygon(pts).exterior.is_ccw:
                pts = pts[::-1]
            vec = os_.Point3dVector([os_.Point3d(x, y, 0) for x, y in pts])
            sp = os_.model.Space.fromFloorPrint(vec, h, m)
            if sp.empty():
                vec = os_.Point3dVector([os_.Point3d(x, y, 0) for x, y in pts[::-1]])
                sp = os_.model.Space.fromFloorPrint(vec, h, m)
            if sp.empty():
                skipped.append((st, u, f"piece {i} not extruded"))
                continue
            sp = sp.get()
            sp.setName(f"{zname} #{i + 1}")
            sp.setBuildingStory(stories[st])
            sp.setZOrigin(FFL[st])
            sp.setThermalZone(z)
            area += p.area
            exp_roof = (p.difference(above).area / p.area) if above is not None else 1.0
            for s in sp.surfaces():
                t = s.surfaceType()
                if t == "Floor":
                    s.setConstruction(slab)
                    s.setOutsideBoundaryCondition("Adiabatic")
                elif t == "RoofCeiling":
                    if exp_roof > 0.5 and st not in BASEMENT:
                        s.setConstruction(roof)
                    else:
                        s.setConstruction(slab)
                        s.setOutsideBoundaryCondition("Adiabatic")
                else:
                    vs = s.vertices()
                    xs = [(v.x(), v.y()) for v in vs]
                    mid = LineString([xs[0], xs[1]] if math.dist(xs[0], xs[1]) > 0.01 else [xs[1], xs[2]]).interpolate(0.5, normalized=True)
                    on_face = bnd.distance(mid) < 0.8 and st not in BASEMENT
                    if not on_face:
                        s.setConstruction(part)
                        s.setOutsideBoundaryCondition("Adiabatic")
                        continue
                    n = s.outwardNormal()
                    o = _orient(math.degrees(math.atan2(n.x(), n.y())))
                    s.setConstruction(wall_cons.get(o, wall_cons["N"]))
                    wwr = (ettv.get(o) or ettv["N"])[0] or 0.4
                    if s.grossArea() > 1.0:
                        sub = s.setWindowToWallRatio(min(wwr, 0.9), 0.0, True)
                        if not sub.empty():
                            sub.get().setConstruction(glz.get(o, glz["N"]))
        # loads (zone totals spread over the spaces by floor area)
        a = a or defaultdict(float)
        spaces = list(z.spaces())
        tot = sum(s.floorArea() for s in spaces) or 1.0
        for s in spaces:
            k = s.floorArea() / tot
            if a["people"] > 0:
                pd = os_.model.PeopleDefinition(m)
                pd.setNumberofPeople(a["people"] * k)
                pd.setSensibleHeatFraction(a["sens_people_w"] / max(a["sens_people_w"] + a["lat_people_w"], 1))
                p = os_.model.People(pd)
                p.setSpace(s)
                p.setActivityLevelSchedule(act_sched((a["sens_people_w"] + a["lat_people_w"]) / a["people"]))
                p.setNumberofPeopleSchedule(always)
            for nm, w, lat in (("Lights", a["lights_w"], 0), ("Small power", a["power_w"], 0), ("Equipment", a["equip_s_w"], 0),
                               ("Equipment latent", a["equip_l_w"], 1)):
                if w <= 0:
                    continue
                if nm == "Lights":
                    d = os_.model.LightsDefinition(m)
                    d.setLightingLevel(w * k)
                    o = os_.model.Lights(d)
                else:
                    d = os_.model.ElectricEquipmentDefinition(m) if not lat else os_.model.OtherEquipmentDefinition(m)
                    d.setDesignLevel(w * k)
                    if lat:
                        d.setFractionLatent(1.0)
                        o = os_.model.OtherEquipment(d)
                    else:
                        o = os_.model.ElectricEquipment(d)
                o.setName(f"{s.nameString()} {nm}")
                o.setSpace(s)
                o.setSchedule(always)
            if a["infil_lps"] > 0:
                inf = os_.model.SpaceInfiltrationDesignFlowRate(m)
                inf.setDesignFlowRate(a["infil_lps"] * k / 1000)
                inf.setSpace(s)
                inf.setSchedule(always)
        # outdoor air of the unit shared by room supply
        oa = oa_unit.get(u)
        share = a["supply_lps"] / unit_supply[u] if unit_supply.get(u) else 0
        oa_z = oa * share if oa and share else a["oa_min_lps"]
        dsoa = os_.model.DesignSpecificationOutdoorAir(m)
        dsoa.setName(f"{zname} OA")
        dsoa.setOutdoorAirFlowRate(oa_z / 1000 / len(spaces) if spaces else 0)
        dsoa.setOutdoorAirMethod("Flow/Zone")
        for s in spaces:
            s.setDesignSpecificationOutdoorAir(dsoa)
        tset = a["t_sum"] / a["t_w"] if a["t_w"] else 24.0
        th = os_.model.ThermostatSetpointDualSetpoint(m)
        th.setCoolingSetpointTemperatureSchedule(const_sched(f"{zname} clg", tset))
        th.setHeatingSetpointTemperatureSchedule(const_sched(f"{zname} htg (no heating)", 10.0))
        z.setThermostatSetpointDualSetpoint(th)
        hs = os_.model.ZoneControlHumidistat(m)
        hs.setDehumidifyingRelativeHumiditySetpointSchedule(const_sched(f"{zname} RH max", 60.0, "Humidity"))
        z.setZoneControlHumidistat(hs)
        il = os_.model.ZoneHVACIdealLoadsAirSystem(m)
        il.setDehumidificationControlType("Humidistat")
        il.setHumidificationControlType("None")
        il.setDesignSpecificationOutdoorAirObject(dsoa)
        il.setMinimumCoolingSupplyAirTemperature(12.0)
        il.addToThermalZone(z)
        sz = z.sizingZone()
        sz.setZoneCoolingDesignSupplyAirTemperature(14.0)
        zinfo.append(dict(zone=zname, storey=st, unit=u, spaces=len(spaces), outline_m2=round(area, 1), rooms_m2=round(a["area"], 1),
                          rooms=len(rooms_of.get((st, u), [])), people=round(a["people"], 1), lights_w=round(a["lights_w"]),
                          power_w=round(a["power_w"]), equip_s_w=round(a["equip_s_w"]), equip_l_w=round(a["equip_l_w"]),
                          infil_lps=round(a["infil_lps"], 1), oa_lps=round(oa_z, 1), t_cool=round(tset, 2)))

    # design days
    dd = os_.model.DesignDay(m)
    dd.setName("SS553 2026 Clg 33DB 27MCWB")
    dd.setMonth(5)
    dd.setDayOfMonth(21)
    dd.setDayType("SummerDesignDay")
    dd.setMaximumDryBulbTemperature(33.0)
    dd.setDailyDryBulbTemperatureRange(9.0)
    dd.setHumidityConditionType("Wetbulb")
    dd.setWetBulbOrDewPointAtMaximumDryBulb(27.0)
    dd.setBarometricPressure(101229)
    dd.setWindSpeed(1.9)
    dd.setWindDirection(40)
    dd.setSolarModelIndicator("ASHRAETau2017")
    dd.setAshraeClearSkyOpticalDepthForBeamIrradiance(0.463)
    dd.setAshraeClearSkyOpticalDepthForDiffuseIrradiance(2.322)
    dh = os_.model.DesignDay(m)
    # Changi TMYx DDY "Ann Clg .4% Condns DP=>MDB" (data/uploads/weather_20261126, line 474)
    dh.setName("Singapore-Changi.Intl.AP Ann Clg .4% Condns DP=>MDB")
    dh.setMonth(5)
    dh.setDayOfMonth(21)
    dh.setDayType("SummerDesignDay")
    dh.setMaximumDryBulbTemperature(29.4)
    dh.setDailyDryBulbTemperatureRange(5.7)
    dh.setHumidityConditionType("Dewpoint")
    dh.setWetBulbOrDewPointAtMaximumDryBulb(27.2)
    dh.setBarometricPressure(101245)
    dh.setWindSpeed(3.6)
    dh.setWindDirection(40)
    dh.setSolarModelIndicator("ASHRAETau2017")
    dh.setAshraeClearSkyOpticalDepthForBeamIrradiance(0.463)
    dh.setAshraeClearSkyOpticalDepthForDiffuseIrradiance(2.322)
    # Comparison only (not the basis): Changi TMYx DDY "Ann Clg .4% Condns WB=>MDB", matches ASHRAE 2025 HoF 486980 row (2) i/j
    dw = os_.model.DesignDay(m)
    dw.setName("Singapore-Changi.Intl.AP Ann Clg .4% Condns WB=>MDB")
    dw.setMonth(5)
    dw.setDayOfMonth(21)
    dw.setDayType("SummerDesignDay")
    dw.setMaximumDryBulbTemperature(30.8)
    dw.setDailyDryBulbTemperatureRange(5.8)
    dw.setHumidityConditionType("Wetbulb")
    dw.setWetBulbOrDewPointAtMaximumDryBulb(28.0)
    dw.setBarometricPressure(101245)
    dw.setWindSpeed(3.6)
    dw.setWindDirection(40)
    dw.setSolarModelIndicator("ASHRAETau2017")
    dw.setAshraeClearSkyOpticalDepthForBeamIrradiance(0.463)
    dw.setAshraeClearSkyOpticalDepthForDiffuseIrradiance(2.322)

    for v in ("Zone Ideal Loads Supply Air Total Cooling Rate", "Zone Ideal Loads Supply Air Sensible Cooling Rate",
              "Zone Ideal Loads Supply Air Latent Cooling Rate", "Zone Ideal Loads Outdoor Air Total Cooling Rate", "Zone Air Temperature",
              "Zone Air Relative Humidity"):
        ov = os_.model.OutputVariable(v, m)
        ov.setReportingFrequency("Hourly")
    m.getOutputControlReportingTolerances()
    os_.model.OutputTableSummaryReports  # noqa
    m.save(os_.path(str(OUT / "AIGH_IP.osm")), True)
    ft = os_.energyplus.ForwardTranslator()
    ws = ft.translateModel(m)
    idf = str(ws)
    idf += "\nOutput:Table:SummaryReports,\n  AllSummary,\n  ZoneComponentLoadSummary;\n\nOutputControl:Table:Style,\n  CommaAndHTML;\n\nOutput:SQLite,\n  SimpleAndTabular;\n"
    (OUT / "AIGH_IP.idf").write_text(idf, encoding="utf-8")
    errs = [str(e.logMessage()) for e in ft.errors()]
    warns = [str(e.logMessage()) for e in ft.warnings()]
    with (OUT / "zone_inputs.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(zinfo[0]))
        w.writeheader()
        w.writerows(zinfo)
    return dict(zones=len(zinfo), spaces=sum(z["spaces"] for z in zinfo), outline_m2=round(sum(z["outline_m2"] for z in zinfo)),
                rooms_m2=round(sum(z["rooms_m2"] for z in zinfo)), skipped=skipped[:20], n_skipped=len(skipped), ft_errors=errs[:5],
                ft_warnings=len(warns), loads_rooms=len(loads), units_with_oa=len(oa_unit))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue", default="out/issue/DRAFT_2026-11-26_R11")
    a = ap.parse_args()
    print(json.dumps(build(ROOT / a.issue), indent=1))
