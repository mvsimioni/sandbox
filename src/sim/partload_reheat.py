"""Part-load reheat check of every AHU of the workbooks (Marcus, 27 Nov 2026; method of the ME-0274 review, src/rfi/me0274_partload.py).

For each AHU / PAHU (fan coil units are not covered: no reheat coil) and each hour of the Changi TMYx year:
* room loads at part load (no safety factor): glazing and wall by facade orientation with the hourly solar irradiance, infiltration of envelope
  rooms, lighting / small power / equipment / people of the Room Information sheet on office schedules (ASSUMPTION) in working hours
  Mon-Fri 08:00-18:00 (ASSUMPTION: the DD Booklets give no working hours); rooms that operate 24 h (DD Booklet after-hours Y, operation time 24 h,
  or plan mode AC (24HR)) at steady use all hours;
* air side: VAV rooms between the VAV minimum and the design supply, CAV rooms at the design supply; cooling coil leaving at the coil leaving dry
  bulb of the AHU tab at the coil leaving RH of the tender schedule; supply fan rise of the AHU tab x (flow / design)^2; outdoor air of the AHU tab;
* test: hours in which a room is below its DD Booklet minimum temperature or above its RH limit with no reheat, as a fraction of the hours
  it operates (working hours, or all hours for 24 h rooms). Below 0.4 %: reheat omitted. Else the reheat is sized:
  A central reheat on the AHU (best single supply temperature each hour), B reheat at each VAV terminal to the higher of the DD Booklet
  minimum and the temperature that keeps the room RH within its limit.
Council C7 corrections (D-126): the room temperature is not taken below the supply temperature; rooms with no DD Booklet temperature or RH
criteria are left out of the test; the installed terminal reheat of each room is sized schedule-free: VAV minimum airflow at zero internal
load, from the supply temperature at the boxes at minimum flow to the higher of the DD Booklet minimum and the temperature that keeps the
supply moisture within the RH limit (the hourly room peak where that is higher). The coincident hourly peak is the heating-water diversity.
The RH-only fraction (no temperature test) is reported as a variant.
Each AHU is run on two supply bases: the coil leaving + fan rise of the AHU tab, and the supply temperature on which the room airflows are
sized (Basis of Design SupplyTemp).

``python3 -m src.sim.partload_reheat [--issue out/issue/DRAFT_2026-11-27_R21]`` writes <issue>/partload/ (CSV) and
<issue>/AIGH_IP_Part_Load_Reheat_Check.xlsx.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import openpyxl

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.rfi.me0274_partload import (EQP, LGT, OCC, WORK, THRESHOLD, psat, rh_of, vertical_irradiance, w_of, weather)  # noqa: E402

ETTV = ROOT / "out/registers/ettv_facades.csv"
REG = ROOT / "out/registers"
LEAK = 0.10
RH_COIL = 0.889
UNIT_TYPES = ("AHU", "PAHU")
_G = {}


def _init():
    wx = weather()
    _G["wx"] = wx
    _G["I"] = vertical_irradiance(wx)
    _G["fac"] = {r["orientation"]: (float(r["wall_u_area_weighted"]), 0.87 * float(r["sc_area_weighted"]))
                 for r in csv.DictReader(ETTV.open(encoding="utf-8")) if r["row_type"] == "facade" and r["orientation"]}
    dow = np.array([(np.datetime64(f"2025-{m:02d}-{d:02d}").astype("datetime64[D]").view("int64") - 4) % 7 for m, d in zip(wx["month"], wx["day"])])
    _G["weekday"] = dow < 5
    _G["work"] = _G["weekday"] & (wx["hour"] >= WORK[0]) & (wx["hour"] < WORK[1])
    m24 = set()
    for p in REG.glob("zone_assignment_*.csv"):
        for z in csv.DictReader(p.open(encoding="utf-8")):
            if "24" in (z.get("mode_label") or ""):
                m24.add(z["room_id"])
    plan = {}
    for p in (ROOT / "out/loads/inputs").glob("D*.json"):
        for r in json.loads(p.read_text(encoding="utf-8"))["rooms"]:
            if r.get("plan_room_id"):
                plan[r["number"]] = r["plan_room_id"]
    _G["h24_plan"] = {n for n, pid in plan.items() if pid in m24}


def _section_rows(ws, title):
    rows = list(ws.iter_rows(values_only=True))
    for i, r in enumerate(rows):
        if isinstance(r[0], str) and r[0].startswith(title):
            k = next(k for k in range(i + 1, i + 5) if rows[k][0] == "AHU or system")
            hdr = [str(x) if x is not None else "" for x in rows[k]]
            j, out = k + 2, {}
            while j < len(rows) and rows[j][0] not in (None, "TOTAL"):
                out[rows[j][0]] = dict(zip(hdr, rows[j]))
                j += 1
            return out
    return {}


def read_workbook(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    af, ri, cr, en, ah = wb["Airflow"], wb["Room Information"], wb["Criteria"], wb["Envelope"], wb["AHU"]
    sup_t = wb["Basis of Design"]["C17"].value or 14
    secA, secC = _section_rows(ah, "A. "), _section_rows(ah, "C. ")
    rooms = {}
    for r in range(7, af.max_row + 1):
        n = af.cell(r, 1).value
        if n:
            g = lambda c: af.cell(r, c).value or 0
            rooms[n] = dict(no=n, name=af.cell(r, 2).value, unit=af.cell(r, 4).value, vol=g(7), vmax=g(20), vmin=g(28), env=[],
                            fixed=bool(af.cell(r, 34).value), single_pass=str(af.cell(r, 22).value or "").upper().startswith("Y"))
    for r in range(7, ri.max_row + 1):
        n = ri.cell(r, 1).value
        if n in rooms:
            g = lambda c: ri.cell(r, c).value
            rooms[n].update(area=g(50) or 0, people=g(64) or 0, act=g(65), hours=g(68), light=g(71) or 0, power=g(74) or 0, eqs=g(77) or 0,
                            eql=g(80) or 0, systype=str(g(16) or ""))
    for r in range(7, cr.max_row + 1):
        n = cr.cell(r, 1).value
        if n in rooms:
            rooms[n].update(tmin=cr.cell(r, 17).value, tmax=cr.cell(r, 18).value, rhmax=cr.cell(r, 19).value, after=cr.cell(r, 38).value,
                            pressure=str(cr.cell(r, 20).value or ""))
    for row in en.iter_rows(min_row=7, values_only=True):
        if row[0] in rooms and row[2] == "Facade" and (row[6] or row[7]):
            rooms[row[0]]["env"].append((row[5], float(row[6] or 0), float(row[7] or 0)))
    units = {}
    for u, a in secA.items():
        if a.get("Type") not in UNIT_TYPES:
            continue
        c = secC.get(u, {})
        oa = next((v for k, v in a.items() if k.startswith("AHU outdoor air =")), None)
        units[u] = dict(unit=u, type=a.get("Type"), supply=a.get("AHU supply airflow"), oa=oa, t_coil=c.get("COIL LEAVING DRY BULB"),
                        fan_rise=c.get("Supply fan temperature rise"), reheat_wb=c.get("REHEAT COIL DUTY"), rooms=[])
    for v in rooms.values():
        if v["unit"] in units and (v.get("vmax") or 0) > 0:
            units[v["unit"]]["rooms"].append(v)
    return [u for u in units.values() if u["rooms"] and u["t_coil"] and u["oa"]], sup_t


def evaluate(u, t_room_supply, coil_leaving=None, oa_hourly=None, ret_locals=False):
    """coil_leaving: dew-point coil leaving temperature when it is below the supply temperature less fan rise (central reheat between);
    oa_hourly: outdoor-air intake by hour (reset); ret_locals: return the local arrays."""
    wx, I, fac, weekday, work = _G["wx"], _G["I"], _G["fac"], _G["weekday"], _G["work"]
    rooms = u["rooms"]
    n, H = len(rooms), len(wx["db"])
    fan = float(u["fan_rise"] or 0)
    t_coil = t_room_supply - fan
    oa = float(u["oa"])
    w_oa = w_of(wx["dp"], 1.0)
    hour = wx["hour"]
    Qs = np.zeros((n, H)); Ql = np.zeros((n, H))
    h24 = np.zeros(n, bool)
    for i, r in enumerate(rooms):
        is24 = (str(r.get("after") or "").upper().startswith("Y") or (isinstance(r.get("hours"), (int, float)) and r["hours"] >= 24)
                or r["no"] in _G["h24_plan"])
        h24[i] = is24
        if is24:
            occ, lgt, eqp = np.full(H, 0.5), np.ones(H), np.ones(H)
        else:
            on = weekday.astype(float)
            occ = np.array([OCC[h] for h in hour]) * on
            lgt = np.array([LGT[h] for h in hour]) * on + (1 - on) * 0.05
            eqp = np.array([EQP[h] for h in hour]) * on + (1 - on) * 0.4
        act = (r.get("act") or "").lower()
        ps, pl = {"seated": (70, 45), "office": (75, 55), "standing/walking": (75, 70), "medium": (80, 140), "heavy": (170, 300)}.get(act, (70, 45) if r["people"] else (0, 0))
        q = r["people"] * ps * occ + r["light"] * r["area"] * lgt + (r["power"] * r["area"] + r["eqs"]) * eqp
        ql = r["people"] * pl * occ + r["eql"] * eqp
        for o, glz, wall in r["env"]:
            U, shgc = fac.get(o, fac["N"])
            q = q + glz * (1.65 * (wx["db"] - 24) + shgc * I.get(o, I["N"])) + wall * U * (wx["db"] + 0.6 * I.get(o, I["N"]) / 22.7 - 24)
        if r["env"]:
            inf = 0.5 * (r["vol"] or 0) / 3.6
            q = q + 1.21 * inf * (wx["db"] - 24)
            ql = ql + 3010 * inf * np.maximum(w_oa - w_of(24.0, 0.6), 0)
        Qs[i], Ql[i] = q, ql
    vmax = np.array([float(r["vmax"]) for r in rooms])[:, None]
    # VAV unless the room must hold its airflow: fixed by specification, single pass, or a pressure regime other than neutral (ASSUMPTION);
    # unit type VAV from the tender Equipment Schedule (all AHU / PAHU are VAV there)
    vav = np.array([("CAV" not in r.get("systype", "").upper()) and not r.get("fixed") and not r.get("single_pass")
                    and (r.get("pressure", "").strip().lower() in ("", "neutral", "none", "n/a")) for r in rooms])[:, None]
    vmin = np.where(vav, np.array([float(r["vmin"] or r["vmax"]) for r in rooms])[:, None], vmax)
    tmin = np.array([r["tmin"] if r.get("tmin") is not None else 18 for r in rooms], float)[:, None]
    tmax = np.array([r["tmax"] if r.get("tmax") is not None else 30 for r in rooms], float)[:, None]
    rhmax = np.array([(r["rhmax"] or 100) / 100 for r in rooms], float)[:, None]
    vdes = vmax.sum() * (1 + LEAK)
    oa = np.minimum(oa_hourly, vdes) if oa_hourly is not None else min(oa, vdes)
    crit = np.array([r.get("tmin") is not None or r.get("rhmax") is not None for r in rooms])
    hours_eval = np.where(h24[:, None], True, work[None, :]) & crit[:, None]

    def air_side(extra):
        ts = np.full(H, t_coil + fan) + extra
        for _ in range(3):
            V = np.clip(Qs / (1.21 * np.maximum(24 - ts, 0.5)), vmin, vmax)
            vahu = V.sum(0) * (1 + LEAK)
            ts = t_coil + fan * (vahu / vdes) ** 2 + extra
        T = np.maximum(ts + Qs / (1.21 * V), ts)
        T = np.where((V > vmin + 1e-6) & (V < vmax - 1e-6), np.minimum(T, 24.0), T)
        w_room = np.full((n, H), w_of(24.0, 0.6))
        for _ in range(4):
            w_ret = (V * w_room).sum(0) / V.sum(0)
            w_mix = (oa * w_oa + (vahu - oa) * w_ret) / vahu
            w_sup = np.minimum(w_mix, w_of(t_coil if coil_leaving is None else coil_leaving, RH_COIL))
            w_room = w_sup + Ql / (3010 * V)
        return V, vahu, T, rh_of(T, w_room)

    V, vahu, T, RH = air_side(np.zeros(H))
    cold = (T < tmin - 0.05) & hours_eval
    wet = (RH > rhmax + 0.005) & hours_eval
    hot = (T > tmax + 0.05) & hours_eval
    op_hours = hours_eval.any(0)
    frac0 = float((cold | wet).any(0)[op_hours].mean()) if op_hours.any() else 0.0
    frac_rh = float(wet.any(0)[op_hours].mean()) if op_hours.any() else 0.0
    # A central
    best = np.zeros(H); nbest = np.full(H, 10 ** 6)
    for e in np.arange(0, 15.01, 0.5):
        _, _, Te, RHe = air_side(np.full(H, e))
        nout = (((Te < tmin - 0.05) | (RHe > rhmax + 0.005)) & hours_eval).sum(0) * 1000 + ((Te > tmax + 0.05) & hours_eval).sum(0)
        better = nout < nbest
        best = np.where(better, e, best); nbest = np.where(better, nout, nbest)
    _, vr, Tr, RHr = air_side(best)
    badA = ((Tr < tmin - 0.05) | (Tr > tmax + 0.05) | (RHr > rhmax + 0.005)) & hours_eval
    qA = 1.21 * vr * best / 1000
    cwA = ((Tr < tmin - 0.05) | (RHr > rhmax + 0.005)) & hours_eval
    # B terminal
    pv = RH * psat(T)
    a_ = np.log(np.maximum(pv / rhmax, 1e-6) / 0.61094)
    t_rh = 243.04 * a_ / (17.625 - a_)
    target = np.minimum(np.maximum(tmin, t_rh + 0.1), tmax)
    qB = np.where(hours_eval, 1.21 * V * np.maximum(target - T, 0) / 1000, 0)
    Tz = np.where(hours_eval, np.maximum(T, target), T)
    RHz = pv / psat(Tz)
    coldwetB = ((Tz < tmin - 0.05) | (RHz > rhmax + 0.005)) & hours_eval
    hotB = (Tz > tmax + 0.05) & hours_eval
    vav_any = bool(vav.any())
    # schedule-free installed terminal reheat (C7): zero internal load at the VAV minimum
    ts_min = t_coil + fan * (min(vmin.sum() * (1 + LEAK), vdes) / vdes) ** 2
    w_s = w_of(t_coil if coil_leaving is None else coil_leaving, RH_COIL)
    pv0 = w_s * 101.325 / (0.622 + w_s)
    a0 = np.log(np.maximum(pv0 / rhmax[:, 0], 1e-6) / 0.61094)
    t_rh0 = 243.04 * a0 / (17.625 - a0)
    tgt0 = np.maximum(np.array([r["tmin"] if r.get("tmin") is not None else -99 for r in rooms], float), t_rh0)
    q_free = np.where(crit, 1.21 * vmin[:, 0] * np.maximum(tgt0 - ts_min, 0) / 1000, 0)
    q_inst = np.maximum(q_free, qB.max(1))
    res = dict(
        supply_basis_degC=round(t_room_supply, 1), t_coil_degC=round(t_coil, 2), rooms=n, rooms_24h=int(h24.sum()), vav_rooms=int(vav.sum()),
        vmin_sum=round(float(vmin.sum())), vmax_sum=round(float(vmax.sum())),
        rooms_no_criteria=int((~crit).sum()), frac_out_of_range_no_reheat=round(frac0, 4), frac_rh_only=round(frac_rh, 4),
        ts_boxes_at_min_flow_degC=round(ts_min, 2), B_installed_schedule_free_kW=round(float(q_free.sum()), 1),
        B_installed_kW=round(float(q_inst.sum()), 1), hours_cold=int(cold.any(0).sum()), hours_humid=int(wet.any(0).sum()), hours_hot=int(hot.any(0).sum()),
        reheat_needed=bool(frac0 >= THRESHOLD),
        A_central_peak_kW=round(float(qA[op_hours].max()) if op_hours.any() else 0, 1),
        A_frac_out_of_range=round(float(badA.any(0)[op_hours].mean()) if op_hours.any() else 0, 4),
        A_frac_cold_or_humid=round(float(cwA.any(0)[op_hours].mean()) if op_hours.any() else 0, 4),
        B_terminal_peak_kW=round(float(qB.sum(0).max()), 1), B_terminal_sum_room_peaks_kW=round(float(qB.max(1).sum()), 1),
        B_rooms_with_reheat=int((qB.max(1) > 0.05).sum()),
        B_frac_cold_or_humid=round(float(coldwetB.any(0)[op_hours].mean()) if op_hours.any() else 0, 4),
        B_frac_overheating=round(float(hotB.any(0)[op_hours].mean()) if op_hours.any() else 0, 4),
        full_flow_to_tmin_kW=round(1.21 * vdes * max(float(tmin.min()) - t_coil, 0) / 1000, 1))
    per_room = [dict(room=rooms[i]["no"], name=rooms[i]["name"], h24=bool(h24[i]), vav=bool(vav[i, 0]), vmin=float(vmin[i, 0]), vmax=float(vmax[i, 0]),
                     tmin=float(tmin[i, 0]), tmax=float(tmax[i, 0]), rhmax=float(rhmax[i, 0]),
                     frac_out_no_reheat=round(float(((cold[i] | wet[i])[hours_eval[i]]).mean()), 4) if hours_eval[i].any() else None,
                     min_T=round(float(T[i][hours_eval[i]].min()), 2) if hours_eval[i].any() else None,
                     max_RH=round(float(RH[i][hours_eval[i]].max()), 3) if hours_eval[i].any() else None,
                     frac_rh_no_reheat=round(float(wet[i][hours_eval[i]].mean()), 4) if hours_eval[i].any() else None,
                     frac_hot_with_terminal=round(float(hotB[i][hours_eval[i]].mean()), 4) if hours_eval[i].any() else None,
                     frac_out_central=round(float(badA[i][hours_eval[i]].mean()), 4) if hours_eval[i].any() else None,
                     central_cold=round(float(((Tr[i] < tmin[i] - 0.05))[hours_eval[i]].mean()), 4) if hours_eval[i].any() else None,
                     central_hot=round(float(((Tr[i] > tmax[i] + 0.05))[hours_eval[i]].mean()), 4) if hours_eval[i].any() else None,
                     central_humid=round(float(((RHr[i] > rhmax[i] + 0.005))[hours_eval[i]].mean()), 4) if hours_eval[i].any() else None,
                     criteria=bool(crit[i]), target_degC=round(float(tgt0[i]), 1) if crit[i] else None,
                     terminal_reheat_kW=round(float(qB[i].max()), 2), installed_kW=round(float(q_inst[i]), 2)) for i in range(n)]
    if ret_locals:
        return res, per_room, locals()
    return res, per_room, vav_any


def verdict(res, vav_any):
    if not res["reheat_needed"]:
        return "OMIT: rooms within the DD Booklet range for more than 99.6 % of the operating hours with no reheat"
    if res["B_frac_cold_or_humid"] < THRESHOLD and (res["A_frac_out_of_range"] >= THRESHOLD or vav_any):
        return "TERMINAL REHEAT at the room terminals (central reheat cannot hold every room)"
    if res["A_frac_out_of_range"] < THRESHOLD:
        return "CENTRAL REHEAT on the AHU"
    return "TERMINAL REHEAT, with the residual hours to review"


def one_workbook(path):
    units, sup_t = read_workbook(path)
    out = []
    for u in units:
        cases = {}
        for label, ts in (("coil", float(u["t_coil"]) + float(u["fan_rise"] or 0)), ("sizing", float(sup_t))):
            res, per_room, vav_any = evaluate(u, ts)
            cases[label] = (res, per_room, vav_any)
        out.append(dict(workbook=Path(path).name, unit=u["unit"], type=u["type"], supply=u["supply"], oa=u["oa"], reheat_wb=u["reheat_wb"],
                        sizing_supply=sup_t, cases={k: dict(res=v[0], rooms=v[1], verdict=verdict(v[0], v[2])) for k, v in cases.items()}))
    return out


def write_xlsx(results, path):
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary"
    hdr = ["Workbook", "AHU", "Type", "Rooms", "24 h rooms", "VAV rooms", "Design supply L/s", "Sum VAV min L/s", "Outdoor air L/s",
           "Basis", "Supply at rooms degC", "Coil leaving degC", "Out of range, no reheat (fraction of operating hours)", "RH only, no reheat", "Reheat needed (>= 0.4 %)",
           "A central reheat peak kW", "A out of range", "B terminal reheat peak kW", "B sum of room peaks kW", "B rooms with reheat",
           "B cold or humid", "B overheating", "Supply at boxes, min flow degC", "B installed terminal reheat kW (schedule-free)", "Workbook reheat kW", "Verdict"]
    ws.append(hdr)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="2F4F6F"); c.alignment = Alignment(wrap_text=True, vertical="top")
    for r in results:
        for basis in ("sizing", "coil"):
            x = r["cases"][basis]; s = x["res"]
            ws.append([r["workbook"].replace("AIGH_C2_", "").replace("_Thermal_Load_Calc_RevA.xlsx", ""), r["unit"], r["type"], s["rooms"], s["rooms_24h"],
                       s["vav_rooms"], s["vmax_sum"], s["vmin_sum"], round(float(r["oa"])), "room sizing basis" if basis == "sizing" else "coil leaving + fan",
                       s["supply_basis_degC"], s["t_coil_degC"], s["frac_out_of_range_no_reheat"], s["frac_rh_only"], "YES" if s["reheat_needed"] else "NO",
                       s["A_central_peak_kW"], s["A_frac_out_of_range"], s["B_terminal_peak_kW"], s["B_terminal_sum_room_peaks_kW"], s["B_rooms_with_reheat"],
                       s["B_frac_cold_or_humid"], s["B_frac_overheating"], s["ts_boxes_at_min_flow_degC"], s["B_installed_kW"], r["reheat_wb"], x["verdict"]])
    for col, w in zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", (22, 28, 6, 7, 7, 7, 9, 9, 9, 16, 9, 9, 13, 9, 9, 9, 9, 9, 9, 8, 9, 9, 9, 11, 9, 60)):
        ws.column_dimensions[col].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row[12:22]:
            if isinstance(c.value, float) and c.value <= 1 and c.column in (13, 14, 17, 21, 22):
                c.number_format = "0.0%"
    ws.freeze_panes = "C2"
    wr = wb.create_sheet("Rooms")
    wr.append(["AHU", "Basis", "Room", "Name", "24 h", "VAV", "VAV min L/s", "Design L/s", "T min", "T max", "RH max", "Out of range, no reheat",
               "Min T no reheat", "Max RH no reheat", "Criteria", "Reheat target degC", "Hourly peak reheat kW", "Installed terminal reheat kW"])
    for c in wr[1]:
        c.font = Font(bold=True)
    for r in results:
        for basis in ("sizing", "coil"):
            for p in r["cases"][basis]["rooms"]:
                wr.append([r["unit"], basis, p["room"], p["name"], p["h24"], p["vav"], p["vmin"], p["vmax"], p["tmin"], p["tmax"], p["rhmax"],
                           p["frac_out_no_reheat"], p["min_T"], p["max_RH"], p["criteria"], p["target_degC"], p["terminal_reheat_kW"], p["installed_kW"]])
    wm = wb.create_sheet("Method")
    import re
    for line in (__doc__ or "").strip().splitlines():
        if "python3" in line or "writes <issue>" in line or line.startswith("<issue>"):
            continue
        line = re.sub(r"\(Marcus, 27 Nov 2026; method of the ME-0274 review, [^)]*\)", "(method of the ME-0274 review, 27 Nov 2026)", line)
        wm.append([line])
    wm.append([])
    wm.append(["Office schedules by hour (occupancy / lighting / equipment), ASSUMPTION:"])
    wm.append(["hour"] + list(range(24)))
    wm.append(["occupancy"] + OCC); wm.append(["lighting"] + LGT); wm.append(["equipment"] + EQP)
    wm.column_dimensions["A"].width = 160
    wb.properties.creator = "AIGH ACMV"
    wb.properties.lastModifiedBy = "AIGH ACMV"
    wb.save(path)
    from src.sim.report import _neutral_props
    _neutral_props(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue", default="out/issue/DRAFT_2026-11-27_R21")
    a = ap.parse_args()
    issue = ROOT / a.issue
    files = sorted((issue / "workbooks").glob("AIGH_C2_D*.xlsx"))
    with Pool(4, initializer=_init) as pool:
        parts = pool.map(one_workbook, files)
    results = [x for p in parts for x in p]
    out = issue / "partload"
    out.mkdir(parents=True, exist_ok=True)
    (out / "partload_results.json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    write_xlsx(results, issue / "AIGH_IP_Part_Load_Reheat_Check.xlsx")
    from collections import Counter
    print(len(results), "units;", Counter(r["cases"]["sizing"]["verdict"].split(":")[0].split(",")[0] for r in results))
    for b in ("sizing", "coil"):
        print(b, "installed", round(sum(r["cases"][b]["res"]["B_installed_kW"] for r in results)), "coincident",
              round(sum(r["cases"][b]["res"]["B_terminal_peak_kW"] for r in results)), "central", round(sum(r["cases"][b]["res"]["A_central_peak_kW"] for r in results)))


if __name__ == "__main__":
    main()
