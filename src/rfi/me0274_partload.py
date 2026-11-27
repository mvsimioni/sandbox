"""Part-load check of the reheat coil of AHU-B-06-06STAFF-01 (RFI ME-0274 review, Marcus 27 Nov 2026).

Question: over the year, in working hours, how often does a room of this AHU leave the DD Booklet range (temperature below the minimum, or
relative humidity above the maximum) with no reheat? If it is less than 0.4 % of the working hours, the reheat coil is omitted. If reheat is
needed, the coil is sized so that every room stays within the DD Booklet temperature range.

Hourly model (one value per hour of the Changi TMYx year, 8,760 h):
* Weather: Changi TMYx EPW (dry bulb, dew point, pressure, global / direct normal / diffuse irradiance).
* Room loads, no safety factor (actual operation): glazing (U 1.65 and SHGC = 0.87 x SC of the ETTV facade) with the solar irradiance on the
  facade orientation; wall (U of the ETTV facade) with sol-air temperature (absorptance 0.6, h_o 22.7 W/m2K); infiltration 0.5 ACH for neutral
  rooms with an envelope (workbook constant InfiltrationACH); lighting, small power and equipment of the Room Information sheet; people of the
  workbook (Seated 70 / 45 W). Schedules: ASSUMPTION, office profiles (ASHRAE 90.1 User's Manual, office), working hours Mon-Fri 08:00-18:00
  (DD Booklet AW: no after-hours mode, no setback; hours not stated). Rooms marked 24 h on the plans run all hours at their working profile.
* Air side, VAV: each room box between its minimum (workbook VAV minimum) and its design supply, holding the room at 24 degC. Cooling coil
  leaving 15.4 degC DB (R21 coil leaving) at the leaving relative humidity of the tender schedule (89 %), or the mixed air if drier. Supply fan
  rise 2.2 K at design flow, in proportion to (flow / design flow)^2. Outdoor air constant 924 L/s. Return air = flow-weighted room air.
* Room state: temperature from the sensible balance where the box is at its minimum; humidity ratio = supply + latent / (3.01 x flow).
* Reheat (central, on the AHU): the supply is warmed only as far as the coldest room needs to stay at its DD Booklet minimum; the boxes then
  re-modulate. Coil size = largest hourly duty in the hours of operation, with the full-flow case at the coil leaving temperature as a check.

``python3 -m src.rfi.me0274_partload`` writes out/issue/RFI_ME-0274_reply_R20/partload/ (summary.json, hourly.csv, rooms.csv).
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import numpy as np
import openpyxl

ROOT = Path(__file__).resolve().parents[2]
WB = ROOT / "out/issue/DRAFT_2026-11-27_R21/workbooks/AIGH_C2_D47_Admin_Workspace_Thermal_Load_Calc_RevA.xlsx"
EPW = ROOT / "data/uploads/weather_20261126/SGP_SG_Singapore-Changi.Intl.AP.486980_TMYx.epw"
ETTV = ROOT / "out/registers/ettv_facades.csv"
OUT = ROOT / "out/issue/RFI_ME-0274_reply_R20/partload"
TAG = "AHU-B-06-06STAFF-01"
LAT, LON, TZ = 1.3678, 103.9826, 8.0
T_COIL, RH_COIL, FAN_RISE, OA, LEAK = 15.4, 0.889, 2.2, 924.0, 0.10
WORK = (8, 18)                    # ASSUMPTION: 08:00-18:00, Monday to Friday
THRESHOLD = 0.004                 # Marcus: omit if the hours out of range are below 0.4 % of the working hours
# ASSUMPTION: office schedules, fraction by hour of a working day (00-23), ASHRAE 90.1 User's Manual office profiles (approximate)
OCC = [0, 0, 0, 0, 0, 0, 0.1, 0.2, 0.95, 0.95, 0.95, 0.95, 0.5, 0.95, 0.95, 0.95, 0.95, 0.3, 0.1, 0.1, 0.1, 0.1, 0.05, 0.05]
LGT = [0.05, 0.05, 0.05, 0.05, 0.05, 0.1, 0.1, 0.3, 0.9, 0.9, 0.9, 0.9, 0.8, 0.9, 0.9, 0.9, 0.9, 0.5, 0.3, 0.3, 0.2, 0.2, 0.1, 0.05]
EQP = [0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.9, 0.9, 0.9, 0.9, 0.8, 0.9, 0.9, 0.9, 0.9, 0.5, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4]
AZ = dict(N=0, NE=45, E=90, SE=135, S=180, SW=225, W=270, NW=315)
P_ATM = 101.325


def psat(t):
    return 0.61094 * np.exp(17.625 * t / (243.04 + t))


def w_of(t, rh):
    pv = rh * psat(t)
    return 0.622 * pv / (P_ATM - pv)


def rh_of(t, w):
    return (w * P_ATM / (0.622 + w)) / psat(t)


def weather():
    rows = list(csv.reader(EPW.open(encoding="latin-1")))[8:]
    a = np.array([[float(r[1]), float(r[2]), float(r[3]), float(r[6]), float(r[7]), float(r[13]), float(r[14]), float(r[15])] for r in rows])
    month, day, hour = a[:, 0].astype(int), a[:, 1].astype(int), a[:, 2].astype(int) - 1      # EPW hour 1 = 00:00-01:00
    return dict(month=month, day=day, hour=hour, db=a[:, 3], dp=a[:, 4], ghi=a[:, 5], dni=a[:, 6], dhi=a[:, 7])


def vertical_irradiance(wx):
    doy = np.array([(np.datetime64(f"2025-{m:02d}-{d:02d}") - np.datetime64("2025-01-01")).astype(int) + 1 for m, d in zip(wx["month"], wx["day"])])
    B = 2 * math.pi * (doy - 81) / 364
    eot = 9.87 * np.sin(2 * B) - 7.53 * np.cos(B) - 1.5 * np.sin(B)
    solar_t = wx["hour"] + 0.5 + (4 * (LON - 15 * TZ) + eot) / 60
    ha = np.radians(15 * (solar_t - 12))
    dec = np.radians(23.45 * np.sin(2 * math.pi * (284 + doy) / 365))
    lat = math.radians(LAT)
    cz = np.sin(lat) * np.sin(dec) + np.cos(lat) * np.cos(dec) * np.cos(ha)
    alt = np.arcsin(np.clip(cz, -1, 1))
    saz = np.arctan2(np.sin(ha), np.cos(ha) * np.sin(lat) - np.tan(dec) * np.cos(lat)) + math.pi    # from north, clockwise
    out = {}
    for o, az in AZ.items():
        cos_i = np.cos(alt) * np.cos(saz - math.radians(az))
        beam = np.where((cz > 0) & (cos_i > 0), wx["dni"] * cos_i, 0.0)
        out[o] = beam + 0.5 * wx["dhi"] + 0.5 * 0.2 * wx["ghi"]
    return out


def rooms_of():
    wb = openpyxl.load_workbook(WB, data_only=True)
    af, ri, cr, en = wb["Airflow"], wb["Room Information"], wb["Criteria"], wb["Envelope"]
    rooms = {}
    for r in range(7, af.max_row + 1):
        if af.cell(r, 4).value == TAG:
            g = lambda c: af.cell(r, c).value or 0
            rooms[af.cell(r, 1).value] = dict(no=af.cell(r, 1).value, name=af.cell(r, 2).value, vol=g(7), vmax=g(20), vmin=g(28), ex=g(23))
    for r in range(7, ri.max_row + 1):
        n = ri.cell(r, 1).value
        if n in rooms:
            g = lambda c: ri.cell(r, c).value
            rooms[n].update(area=g(50) or 0, people=g(64) or 0, act=g(65), light=g(71) or 0, power=g(74) or 0, eqs=g(77) or 0, eql=g(80) or 0)
    for r in range(7, cr.max_row + 1):
        n = cr.cell(r, 1).value
        if n in rooms:
            rooms[n].update(tmin=cr.cell(r, 17).value, tmax=cr.cell(r, 18).value, rhmax=cr.cell(r, 19).value)
    for n in rooms:
        rooms[n]["env"] = []
    for row in en.iter_rows(min_row=7, values_only=True):
        if row[0] in rooms and row[2] == "Facade" and (row[6] or row[7]):
            rooms[row[0]]["env"].append((row[5], float(row[6] or 0), float(row[7] or 0)))
    # rooms of the plan marked AC (24HR) on this unit run all hours (AGV lobbies, clean utility, AGV bays, DSOT and AMSC rooms)
    for v in rooms.values():
        v["h24"] = not str(v["no"]).startswith("ANWSIP6")
        v["seated"] = v["act"] in ("Seated", "Office")
    return rooms


def run(t_room_supply=None, tag=""):
    """t_room_supply: supply temperature at the rooms at design flow (None = R21 coil leaving 15.4 + fan rise 2.2 = 17.6 degC)"""
    global T_COIL
    T_COIL = 15.4 if t_room_supply is None else t_room_supply - FAN_RISE
    OUT.mkdir(parents=True, exist_ok=True)
    wx = weather()
    I = vertical_irradiance(wx)
    fac = {r["orientation"]: (float(r["wall_u_area_weighted"]), 0.87 * float(r["sc_area_weighted"]))
           for r in csv.DictReader(ETTV.open(encoding="utf-8")) if r["row_type"] == "facade" and r["orientation"]}
    rooms = [v for v in rooms_of().values() if v["vmax"] > 0]
    n, H = len(rooms), len(wx["db"])
    w_oa = w_of(wx["dp"], 1.0)
    dow = np.array([(np.datetime64(f"2025-{m:02d}-{d:02d}").astype("datetime64[D]").view("int64") - 4) % 7 for m, d in zip(wx["month"], wx["day"])])
    weekday = dow < 5                                     # 2025-01-01 was a Wednesday; 0 = Monday
    work = weekday & (wx["hour"] >= WORK[0]) & (wx["hour"] < WORK[1])
    Qs = np.zeros((n, H)); Ql = np.zeros((n, H))
    for i, r in enumerate(rooms):
        on = np.where(r["h24"], 1.0, weekday.astype(float))
        occ = np.array([OCC[h] for h in wx["hour"]]) * on
        lgt = np.array([LGT[h] for h in wx["hour"]]) * on + (1 - on) * 0.05
        eqp = np.array([EQP[h] for h in wx["hour"]]) * on + (1 - on) * 0.4
        if r["h24"]:
            occ, lgt, eqp = np.full(H, 0.5), np.ones(H), np.ones(H)      # 24 h support rooms: steady use (ASSUMPTION)
        ps, pl = (70, 45) if r["seated"] else (0, 0)
        q = r["people"] * ps * occ + r["light"] * r["area"] * lgt + (r["power"] * r["area"] + r["eqs"]) * eqp
        ql = r["people"] * pl * occ + r["eql"] * eqp
        if r["env"]:
            for o, glz, wall in r["env"]:
                U, shgc = fac.get(o, fac["N"])
                q = q + glz * (1.65 * (wx["db"] - 24) + shgc * I[o]) + wall * U * (wx["db"] + 0.6 * I[o] / 22.7 - 24)
            inf = 0.5 * r["vol"] / 3.6
            q = q + 1.21 * inf * (wx["db"] - 24)
            ql = ql + 3010 * inf * np.maximum(w_oa - w_of(24.0, 0.6), 0)
        Qs[i], Ql[i] = q, ql
    vmin = np.array([r["vmin"] for r in rooms], float)[:, None]
    vmax = np.array([r["vmax"] for r in rooms], float)[:, None]
    tmin = np.array([r["tmin"] if r["tmin"] is not None else 18 for r in rooms], float)[:, None]
    tmax = np.array([r["tmax"] if r["tmax"] is not None else 30 for r in rooms], float)[:, None]
    rhmax = np.array([(r["rhmax"] or 100) / 100 for r in rooms], float)[:, None]
    vdes = vmax.sum() * (1 + LEAK)

    def air_side(ts_extra):
        """room flows, temperatures and humidity for the coil leaving + fan rise + reheat (ts_extra, K) of each hour"""
        ts = np.full(H, T_COIL + FAN_RISE) + ts_extra
        for _ in range(3):
            V = np.clip(Qs / (1.21 * np.maximum(24 - ts, 0.5)), vmin, vmax)
            vahu = V.sum(0) * (1 + LEAK)
            ts = T_COIL + FAN_RISE * (vahu / vdes) ** 2 + ts_extra
        T = np.where(V > vmin + 1e-6, np.minimum(24.0, ts + Qs / (1.21 * V)), ts + Qs / (1.21 * V))   # box at minimum: floats
        T = np.where(V >= vmax - 1e-6, ts + Qs / (1.21 * V), T)                                       # box at maximum: floats (may overheat)
        w_room = np.full((n, H), w_of(24.0, 0.6))
        for _ in range(4):
            w_ret = (V * w_room).sum(0) / V.sum(0)
            w_mix = (OA * w_oa + (vahu - OA) * w_ret) / vahu
            w_sup = np.minimum(w_mix, w_of(T_COIL, RH_COIL))
            w_room = w_sup + Ql / (3010 * V)
        return V, vahu, T, rh_of(T, w_room), w_sup

    V, vahu, T, RH, w_sup = air_side(np.zeros(H))
    admin = np.array([not r["h24"] for r in rooms])[:, None]
    hours_eval = np.where(admin, work[None, :], True)
    cold = (T < tmin - 0.05) & hours_eval
    wet = (RH > rhmax + 0.005) & hours_eval
    bad = cold | wet
    n_work = int(work.sum())
    frac_any = float((bad & admin).any(0)[work].mean())
    frac_any_all = float(bad.any(0).mean())
    hot0 = (T > tmax + 0.05) & hours_eval
    # Option A, central reheat on the AHU: each hour, the supply rise (0-10 K, 0.25 K steps) that leaves the fewest rooms out of range
    best = np.zeros(H); nbest = np.full(H, 10 ** 6)
    for e in np.arange(0, 10.01, 0.25):
        _, _, Te, RHe, _ = air_side(np.full(H, e))
        nout = (((Te < tmin - 0.05) | (Te > tmax + 0.05) | (RHe > rhmax + 0.005)) & hours_eval).sum(0)
        better = nout < nbest
        best = np.where(better, e, best); nbest = np.where(better, nout, nbest)
    extra = best
    Vr, vr, Tr, RHr, _ = air_side(extra)
    badA = ((Tr < tmin - 0.05) | (Tr > tmax + 0.05) | (RHr > rhmax + 0.005)) & hours_eval
    q_rh = 1.21 * vr * extra / 1000                       # kW
    # Option B, reheat at each VAV terminal (no central reheat): each room warmed from its supply to its DD Booklet minimum
    ts0 = T_COIL + FAN_RISE * (vahu / vdes) ** 2
    pv_room = RH * psat(T)                                                            # room vapour pressure (unchanged by sensible reheat)
    a_ = np.log(np.maximum(pv_room / rhmax, 1e-6) / 0.61094)
    t_rh = 243.04 * a_ / (17.625 - a_)                                                # temperature at which RH = the room limit
    target = np.minimum(np.maximum(tmin, t_rh + 0.1), tmax)
    q_zone = np.where(hours_eval, 1.21 * V * np.maximum(target - T, 0) / 1000, 0)   # kW per room
    Tz = np.where(hours_eval, np.maximum(T, target), T)
    RHz = pv_room / psat(Tz)
    badB = ((Tz < tmin - 0.05) | (Tz > tmax + 0.05) | (RHz > rhmax + 0.005)) & hours_eval
    op = hours_eval.any(0)
    res = dict(
        unit=TAG, rooms=n, working_hours=n_work, working_hours_definition="Mon-Fri 08:00-18:00 (ASSUMPTION)",
        hours_out_of_range_any_admin_room=int((bad & admin).any(0)[work].sum()), fraction_of_working_hours=round(frac_any, 4),
        hours_cold_any_room=int(cold.any(0).sum()), hours_humid_any_room=int(wet.any(0).sum()),
        fraction_all_rooms_all_operating_hours=round(frac_any_all, 4), threshold=THRESHOLD,
        reheat_needed=bool(frac_any >= THRESHOLD or frac_any_all >= THRESHOLD),
        hours_hot_any_room_no_reheat=int(hot0.any(0).sum()),
        A_central_reheat_peak_kW=round(float(q_rh[op].max()), 1), A_central_supply_rise_peak_K=round(float(extra[op].max()), 2),
        A_fraction_working_hours_out_of_range=round(float((badA & admin).any(0)[work].mean()), 4),
        A_fraction_all_hours_out_of_range=round(float(badA.any(0).mean()), 4),
        B_zone_reheat_coincident_peak_kW=round(float(q_zone.sum(0).max()), 1), B_zone_reheat_sum_of_room_peaks_kW=round(float(q_zone.max(1).sum()), 1),
        B_fraction_working_hours_out_of_range=round(float((badB & admin).any(0)[work].mean()), 4),
        B_fraction_all_hours_out_of_range=round(float(badB.any(0).mean()), 4),
        B_rooms_needing_reheat=int((q_zone.max(1) > 0.05).sum()), B_hours_humid_any_room=int(((RHz > rhmax + 0.005) & hours_eval).any(0).sum()),
        reheat_full_flow_to_24C_kW=round(1.21 * vdes * (24 - T_COIL) / 1000, 1),
        min_room_T_no_reheat=round(float(np.where(hours_eval, T, 99).min()), 2), max_room_RH_no_reheat=round(float(np.where(hours_eval, RH, 0).max()), 3),
        A_min_room_T=round(float(np.where(hours_eval, Tr, 99).min()), 2), A_max_room_T=round(float(np.where(hours_eval, Tr, 0).max()), 2),
        A_max_room_RH=round(float(np.where(hours_eval, RHr, 0).max()), 3), B_max_room_RH=round(float(np.where(hours_eval, RHz, 0).max()), 3),
        ahu_flow_min_Ls=round(float(vahu.min())), ahu_flow_max_Ls=round(float(vahu.max())), vmin_sum_Ls=round(float(vmin.sum())))
    with (OUT / f"rooms{tag}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["room", "name", "24h", "vmin", "vmax", "tmin", "tmax", "rhmax", "hours_cold", "hours_humid", "hours_hot", "fraction_of_hours", "min_T", "max_RH",
                    "zone_reheat_peak_kW", "zone_reheat_hours"])
        for i, r in enumerate(rooms):
            he = hours_eval[i]
            w.writerow([r["no"], r["name"], r["h24"], r["vmin"], r["vmax"], r["tmin"], r["tmax"], r["rhmax"], int(cold[i].sum()), int(wet[i].sum()), int(hot0[i].sum()),
                        round(float(bad[i][he].mean()), 4), round(float(T[i][he].min()), 2), round(float(RH[i][he].max()), 3),
                        round(float(q_zone[i].max()), 2), int((q_zone[i] > 0.01).sum())])
    with (OUT / f"hourly{tag}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["month", "day", "hour", "working", "db", "dp", "ahu_flow_Ls", "w_supply", "rooms_cold", "rooms_humid", "reheat_kW", "supply_rise_K"])
        for h in range(H):
            w.writerow([wx["month"][h], wx["day"][h], wx["hour"][h], int(work[h]), wx["db"][h], wx["dp"][h], round(float(vahu[h])),
                        round(float(w_sup[h]), 5), int(cold[:, h].sum()), int(wet[:, h].sum()), round(float(q_rh[h]), 2), round(float(extra[h]), 2)])
    res["case"] = tag or "supply at the rooms %.1f degC" % (T_COIL + FAN_RISE)
    (OUT / f"summary{tag}.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return res


if __name__ == "__main__":
    for t, tag in ((None, "_17.6C"), (14.0, "_14C")):
        r = run(t, tag)
        print(tag, json.dumps({k: v for k, v in r.items() if k.startswith(("fraction", "hours_", "A_", "B_", "reheat_full"))}, indent=0))
