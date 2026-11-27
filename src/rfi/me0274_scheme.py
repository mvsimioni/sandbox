"""ME-0274, AHU-B-06-06STAFF-01: part-load scheme ruled by Marcus (27 Nov 2026, D-126), computed hour by hour.

Scheme: supply 17.6 degC at the rooms; room airflows sized on it; cooling coil leaving 12.0 degC (dehumidification) with central reheat
to 15.4 degC (fan rise 2.2 K to 17.6 degC) and an hourly reset of the central reheat; VAV minimum = DD Booklet air change, except 13 rooms
(open office, eight offices, three meeting rooms, storeroom) whose minimum is lowered to the airflow that holds the DD Booklet minimum
temperature (0.4 % of the hours may fall below); outdoor-air intake reset each hour to the room that needs the most (outdoor air of each
room delivered in proportion to its supply); offices without a 24 h note off after hours; the 24 h AGV Lift Lobbies, AGV Bays and Clean
Utility on central reheat only, out-of-range hours accepted.
``python3 -m src.rfi.me0274_scheme`` writes out/issue/RFI_ME-0274_reply/scheme.json.
"""
import json
import math
import sys
from pathlib import Path

import numpy as np
import openpyxl

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import src.sim.partload_reheat as P  # noqa: E402
from src.rfi.me0274_partload import w_of  # noqa: E402

WB = ROOT / "out/issue/DRAFT_2026-11-27_R22/workbooks/AIGH_C2_D47_Admin_Workspace_Thermal_Load_Calc_RevA.xlsx"
OUT = ROOT / "out/issue/RFI_ME-0274_reply/scheme.json"
TAG = "AHU-B-06-06STAFF-01"
TS, COIL = 17.6, 12.0
ACC = {"LL ALLIP.06.01", "LL ALLIP.06.02", "AMSC 12.01.02", "AMSC 12.01.05", "AMSC 07.06.03", "AMSC 10.19.03"}   # 24 h, negligible load (D-126, D-127)
R13 = ["ANWSIP6 Cl.06.01", "ANWSIP6 WS.06.07", "ANWSIP6 WS.06.11", "ANWSIP6 WS.06.12", "ANWSIP6 WS.06.13", "ANWSIP6 WS.06.14",
       "ANWSIP6 WS.06.15", "ANWSIP6 WS.06.16", "ANWSIP6 WS.06.24", "ANWSIP6 MR.06.01", "ANWSIP6 MR.06.02", "ANWSIP6 MR.06.03", "ANWSIP6 AM.06.01"]
BOX_MIN = 10      # smallest VAV box setting, L/s (ASSUMPTION)
P_ATM = 101.325
ps = lambda t: 0.61094 * math.exp(17.625 * t / (243.04 + t))
h_ = lambda t, w: 1.006 * t + w * (2501 + 1.86 * t)
W_wb = lambda db, wb: ((2501 - 2.326 * wb) * 0.622 * ps(wb) / (P_ATM - ps(wb)) - 1.006 * (db - wb)) / (2501 + 1.86 * db - 4.186 * wb)


def wb_of(t, w):
    lo, hi = -10.0, t
    for _ in range(60):
        m = (lo + hi) / 2
        lo, hi = (m, hi) if W_wb(t, m) < w else (lo, m)
    return m


def dp_of(w):
    a = math.log(w * P_ATM / (0.622 + w) / 0.61094)
    return 243.04 * a / (17.625 - a)


def airflow_sheet():
    ws = openpyxl.load_workbook(WB, data_only=True)["Airflow"]
    out = {}
    for r in range(7, ws.max_row + 1):
        if ws.cell(r, 4).value == TAG:
            g = lambda c: ws.cell(r, c).value
            out[g(1)] = dict(name=g(2), area=g(6) or 0, vol=g(7) or 0, qs=g(8) or 0, troom=g(10) or 24, ach=g(12) or 0, mins=g(19) or 0,
                             final=g(20) or 0, gov=g(14), exh=g(23) or 0, oa=g(26) or 0, vmin=g(28) or 0, fixed=bool(g(34)),
                             sp=str(g(22) or "").upper().startswith("Y"), scope=g(42) or "exhaust only")
    return out


def frac(m, sel):
    return float(m.any(0)[sel].mean()) if sel.any() else 0.0


def main():
    P._init()
    work, wx = P._G["work"], P._G["wx"]
    units, _ = P.read_workbook(WB)
    u0 = next(x for x in units if x["unit"] == TAG)
    A = airflow_sheet()
    fan = float(u0["fan_rise"])

    def accepted_off(v):
        w = json.loads(json.dumps(v, default=float))
        for r in w["rooms"]:
            if r["no"] in ACC:
                r["tmin"] = r["rhmax"] = r["tmax"] = None
        return w

    def summary(L, keep, central=True):
        he = L["hours_eval"] & keep[:, None]
        cold = (L["T"] < L["tmin"] - .05) & he; wet = (L["RH"] > L["rhmax"] + .005) & he; hot = (L["T"] > L["tmax"] + .05) & he
        d = dict(hours_cold_or_humid=round(100 * frac(cold | wet, work), 1), hours_humid=round(100 * frac(wet, work), 1),
                 rooms_cold_mean=round(float(cold[:, work].sum(0).mean()), 1), hours_hot=round(100 * frac(hot, work), 1))
        if central:
            cA = ((L["Tr"] < L["tmin"] - .05) | (L["RHr"] > L["rhmax"] + .005)) & he; hA = (L["Tr"] > L["tmax"] + .05) & he
            d.update(central_cold_or_humid=round(100 * frac(cA, work), 1), central_hot=round(100 * frac(hA, work), 1))
        return d

    # --- reference cases (as in the workbook, 14 degC at the rooms; 17.6 degC without the dew-point coil)
    keep0 = np.array([r["no"] not in ACC for r in u0["rooms"]])
    res14, rooms14, L14 = P.evaluate(accepted_off(u0), 14.0, ret_locals=True)
    ref14 = summary(L14, keep0) | dict(vmin_pct=round(100 * res14["vmin_sum"] / res14["vmax_sum"]), terminal_kW=res14["B_installed_kW"],
                                       rooms=res14["vmax_sum"], central_peak=res14["A_central_peak_kW"])
    # --- room airflows sized at 17.6 degC
    v = json.loads(json.dumps(u0, default=float))
    for r in v["rooms"]:
        a = A[r["no"]]
        if not (a["fixed"] or a["sp"] or not a["qs"]):
            th = a["qs"] / (1.21 * (a["troom"] - TS))
            r["vmax"] = math.ceil(max(a["ach"], th, a["mins"]) / 10) * 10
            r["gov17"] = "thermal load" if th > a["ach"] else "air change"
        else:
            r["gov17"] = "fixed" if a["fixed"] or a["sp"] else "air change"
    res176, _, L176 = P.evaluate(accepted_off(v), TS, ret_locals=True)
    ref176 = summary(L176, keep0) | dict(rooms=res176["vmax_sum"])
    # --- 13 rooms: minimum that holds the DD Booklet minimum temperature
    idx = {r["no"]: i for i, r in enumerate(v["rooms"])}
    for n in R13:
        v["rooms"][idx[n]]["vmin"] = max(A[n]["oa"], BOX_MIN)
    oa_h = None
    for it in range(4):
        res, rooms, L = P.evaluate(accepted_off(v), TS, coil_leaving=COIL, oa_hourly=oa_h, ret_locals=True)
        he, Qs, tmin = L["hours_eval"], L["Qs"], L["tmin"]
        ts = L["t_coil"] + fan * (L["vahu"] / L["vdes"]) ** 2
        if it < 2:
            for n in R13:
                i = idx[n]; h = he[i]
                vreq = Qs[i][h] / (1.21 * np.maximum(tmin[i, 0] - ts[h], 0.3))
                v["rooms"][i]["vmin"] = min(v["rooms"][i]["vmax"], max(A[n]["oa"], BOX_MIN, math.floor(float(np.quantile(vreq, 0.004)) / 5) * 5))
        # outdoor-air intake: the room that needs the most, each hour it operates
        oa_room = np.array([A[r["no"]]["oa"] for r in v["rooms"]], float)[:, None]
        on = (he | L["h24"][:, None] | np.array([r["no"] in ACC for r in v["rooms"]])[:, None]) & (oa_room > 0)
        req = np.where(on, oa_room * L["vahu"][None, :] / L["V"], 0).max(0)
        oa_h = np.maximum(req, float(u0["oa"]))
    res, rooms, L = P.evaluate(accepted_off(v), TS, coil_leaving=COIL, oa_hourly=oa_h, ret_locals=True)
    keep = keep0
    fin = summary(L, keep)
    he = L["hours_eval"]
    best, vr = L["best"], L["vr"]
    base = L["t_coil"] - COIL
    q_central = 1.21 * vr * (base + best) / 1000
    # accepted 24 h rooms: out-of-range share of their hours with central reheat only
    acc = {}
    for i, r in enumerate(v["rooms"]):
        if r["no"] in ACC:
            a = A[r["no"]]; T = L["Tr"][i]
            tm = next(x for x in u0["rooms"] if x["no"] == r["no"])["tmin"]
            acc[r["no"]] = dict(name=a["name"], tmin=tm, below_tmin_pct=round(100 * float((T < tm - .05).mean()), 1), mean_T=round(float(T.mean()), 1))
    hot_rooms = []
    for i, r in enumerate(v["rooms"]):
        if keep[i] and he[i].any():
            x = float(((L["Tr"][i] > L["tmax"][i, 0] + .05) & he[i])[he[i]].mean())
            x0 = float(((L["T"][i] > L["tmax"][i, 0] + .05) & he[i])[he[i]].mean())
            if x >= 0.004:
                hot_rooms.append(dict(room=r["no"], name=A[r["no"]]["name"], pct=round(100 * x, 1), pct_no_reheat=round(100 * x0, 1),
                                      max_T=round(float(L["Tr"][i][he[i]].max()), 1)))
    hot_rooms.sort(key=lambda d: -d["pct"])
    # outdoor air delivered at the Rev check: as-sized design with straight-sum intake (finding)
    short_design = []
    sup_d = sum(r["vmax"] for r in v["rooms"]) * 1.1
    for r in v["rooms"]:
        a = A[r["no"]]
        if a["oa"]:
            d = r["vmax"] * float(u0["oa"]) / sup_d
            if d < a["oa"] - 0.5:
                short_design.append(dict(room=r["no"], name=a["name"], required=round(a["oa"], 1), delivered=round(d, 1)))
    # design condition (SS 553 32 / 26 degC), design airflows, intake for the critical room at design flow
    vmx = np.array([r["vmax"] for r in v["rooms"]], float)
    oa_room = np.array([A[r["no"]]["oa"] for r in v["rooms"]], float)
    oa_d = max(float(u0["oa"]), float((oa_room * sup_d / vmx).max()))
    crit_d = v["rooms"][int(np.argmax(oa_room / vmx))]["no"]
    lat = sum(A[r["no"]]["qs"] for r in v["rooms"])   # noqa (room sensible, for reference)
    ah = openpyxl.load_workbook(WB, data_only=True)["AHU"]
    col = {str(ah.cell(5, c).value): c for c in range(1, ah.max_column + 1) if ah.cell(5, c).value}
    row = next(r for r in range(7, 12) if ah.cell(r, 1).value == TAG)
    room_sens, room_lat = ah.cell(row, col["Room sensible"]).value, ah.cell(row, col["Room latent"]).value
    w_off = w_of(COIL, 0.889)
    w_room = w_off + room_lat * 1000 / (3010 * vmx.sum())
    oa_des = max(oa_d, float(np.max(oa_h[work])))
    t_mix = (oa_des * 32 + (sup_d - oa_des) * 24) / sup_d
    w_mix = (oa_des * W_wb(32, 26) + (sup_d - oa_des) * w_room) / sup_d
    w_mix60 = (oa_des * W_wb(32, 26) + (sup_d - oa_des) * w_of(24.0, 0.6)) / sup_d
    coil = 1.2 * sup_d / 1000 * (h_(t_mix, w_mix) - h_(COIL, w_off))
    coil_s = 1.2 * sup_d / 1000 * 1.006 * (t_mix - COIL)
    coil60 = 1.2 * sup_d / 1000 * (h_(t_mix, w_mix60) - h_(COIL, w_off))
    # hourly coil over the year
    Vb, Tr, RHr = L["V"], L["Tr"], np.clip(L["RHr"], 0, 1)
    wroom = w_of(Tr, RHr)
    t_ret = (Vb * Tr).sum(0) / Vb.sum(0); w_ret = (Vb * wroom).sum(0) / Vb.sum(0)
    vahu = L["vahu"]; oa_e = np.minimum(oa_h, vahu); w_oa = w_of(wx["dp"], 1.0)
    tm = (oa_e * wx["db"] + (vahu - oa_e) * t_ret) / vahu; wm = (oa_e * w_oa + (vahu - oa_e) * w_ret) / vahu
    coil_h = 1.2 * vahu / 1000 * np.maximum(h_(tm, wm) - h_(COIL, np.minimum(wm, w_off)), 0)
    rooms_out = []
    for i, r in enumerate(v["rooms"]):
        a = A[r["no"]]
        basis = ("28 % rule (held at the DD Booklet minimum temperature)" if r["no"] in R13 else
                 "fixed" if (a["fixed"] or a["sp"]) else "DD Booklet air change")
        rooms_out.append(dict(room=r["no"], name=a["name"], area=a["area"], ach6=a["ach"], design=r["vmax"],
                              vmin=float(r["vmin"]) if r["no"] in R13 else float(a["vmin"] if not (a["fixed"] or a["sp"]) else r["vmax"]),
                              vmin_basis=basis, oa=a["oa"], exh=a["exh"], ret=max(r["vmax"] - a["exh"], 0), gov=r["gov17"], scope=a["scope"],
                              design_14=a["final"]))
    r13 = [d for d in rooms_out if d["room"] in R13]
    out = dict(
        unit=TAG, supply_at_rooms=TS, fan_rise=round(fan, 2), coil_leaving=COIL, coil_leaving_wb=round(wb_of(COIL, w_off), 1),
        coil_dew_point=round(dp_of(w_off), 1), reheat_to=round(L["t_coil"], 1),
        rooms=rooms_out, room_supply=round(float(vmx.sum())), ahu_supply=round(sup_d), vmin_sum=round(sum(d["vmin"] for d in rooms_out)),
        r13_ach6=round(sum(d["ach6"] for d in r13)), r13_vmin=round(sum(d["vmin"] for d in r13)),
        oa_sum_rooms=round(float(u0["oa"]), 1), oa_work_min=round(float(oa_h[work].min())), oa_work_mean=round(float(oa_h[work].mean())),
        oa_work_max=round(float(oa_h[work].max())), oa_after_max=round(float(oa_h[~work].max())), oa_design=round(oa_d), oa_design_critical=crit_d,
        exhaust=round(sum(d["exh"] for d in rooms_out)),
        central_peak=round(float(q_central.max()), 1), central_mean_work=round(float(q_central[work].mean()), 1),
        central_base_design=round(1.21 * sup_d * base / 1000, 1), reset_hours_pct=round(100 * float((best[work] > 0).mean())),
        reset_mean=round(float(best[work].mean()), 2), reset_max=round(float(best.max()), 1),
        coil_design=round(coil, 1), coil_design_sensible=round(coil_s, 1), coil_design_24_60=round(coil60, 1), coil_hourly_peak=round(float(coil_h.max()), 1),
        mix_db=round(t_mix, 1), mix_wb=round(wb_of(t_mix, w_mix), 1), mix_h=round(h_(t_mix, w_mix), 1), room_rh_design=round(w_room * P_ATM / (0.622 + w_room) / ps(24), 2),
        oa_design_intake=round(oa_des), room_sensible=room_sens, room_latent=room_lat,
        final=fin, ref14=ref14, ref176=ref176, accepted=acc, hot_rooms=hot_rooms, oa_short_straight_sum=short_design,
        vmin_pct_14=ref14["vmin_pct"], vmin_pct_17=round(100 * res176["vmin_sum"] / res176["vmax_sum"]),
        hours_work=int(work.sum()))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1, default=float), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k not in ("rooms",)}, indent=1, default=float))


if __name__ == "__main__":
    main()
