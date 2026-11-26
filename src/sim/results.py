"""Design-day results of the EnergyPlus model against the workbook coil duties (D-109).

Reads out/sim/run_sizing/eplusout.sql (hourly ideal-loads outputs per zone and design day), sums the zones of each unit
(a unit can serve zones on several storeys) hour by hour, takes the peak hour per design day, and compares with the AHU tab of the issued
workbooks:
  room load  = ideal-loads total cooling less outdoor-air total cooling   vs  workbook room sensible + room latent (section B)
  coil load  = ideal-loads total cooling (includes outdoor air)           vs  workbook COOLING COIL DUTY (section D), else coil duty (B)
The ideal-loads system has no fan heat and no reheat; the workbook coil duty includes fan heat and the coil leaving condition.

``python3 -m src.sim.results --issue out/issue/DRAFT_2026-11-26_R11`` writes out/sim/results_design_days.csv and .xlsx.
"""
from __future__ import annotations

import argparse
import csv
import glob
import sqlite3
from collections import defaultdict
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent.parent
SIM = ROOT / "out/sim"
VARS = {"Zone Ideal Loads Supply Air Total Cooling Rate": "tot", "Zone Ideal Loads Supply Air Sensible Cooling Rate": "sen",
        "Zone Ideal Loads Supply Air Latent Cooling Rate": "lat", "Zone Ideal Loads Outdoor Air Total Cooling Rate": "oa"}


def sim_hourly(sql: Path):
    con = sqlite3.connect(sql)
    q = """select ep.EnvironmentName, d.KeyValue, d.Name, t.TimeIndex, r.Value
           from ReportData r join ReportDataDictionary d on r.ReportDataDictionaryIndex = d.ReportDataDictionaryIndex
           join Time t on r.TimeIndex = t.TimeIndex join EnvironmentPeriods ep on t.EnvironmentPeriodIndex = ep.EnvironmentPeriodIndex
           where d.ReportingFrequency = 'Hourly' and d.Name in ({})""".format(",".join("?" * len(VARS)))
    data = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))   # env -> key(zone, var) -> t -> W
    for env, key, name, t, v in con.execute(q, list(VARS)):
        zone = key.upper().removesuffix(" IDEAL LOADS AIR SYSTEM")
        data[env][(zone, VARS[name])][t] += v
    return data


def zone_units():
    with (SIM / "zone_inputs.csv").open(encoding="utf-8") as fh:
        return {r["zone"].upper(): r for r in csv.DictReader(fh)}


def workbook_duties(issue: Path):
    out = defaultdict(lambda: dict(room_s=0.0, room_l=0.0, coil_b=0.0, coil_d=0.0, books=[]))
    for f in sorted(glob.glob(str(issue / "workbooks/AIGH_C2_D*.xlsx"))):
        rows = list(openpyxl.load_workbook(f, data_only=True, read_only=True)["AHU"].iter_rows(values_only=True))
        start = {str(r[0])[:2]: i for i, r in enumerate(rows) if r and r[0] and str(r[0])[:3] in ("B. ", "D. ")}
        num = lambda v: float(v) if isinstance(v, (int, float)) else 0.0
        if "B." in start:
            hdr = rows[start["B."] + 1]
            ci = {h: i for i, h in enumerate(hdr) if h}
            for r in rows[start["B."] + 3:]:
                if not r or not r[0] or str(r[0]).startswith(("TOTAL", "C. ")):
                    break
                if r[1] == "Type":
                    continue
                u = out[str(r[0])]
                u["room_s"] += num(r[ci["Room sensible"]])
                u["room_l"] += num(r[ci["Room latent"]])
                u["coil_b"] += num(r[ci["Coil duty (outdoor-air rule)"]])
                u["books"].append(Path(f).name[8:11])
        if "D." in start:
            hdr = rows[start["D."] + 2]
            ci = {h: i for i, h in enumerate(hdr) if h}
            for r in rows[start["D."] + 4:]:
                if not r or not r[0] or str(r[0]).startswith(("TOTAL", "E. ")):
                    break
                if r[0] in out:
                    out[str(r[0])]["coil_d"] += num(r[ci["COOLING COIL DUTY"]])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue", default="out/issue/DRAFT_2026-11-26_R11")
    ap.add_argument("--sql", default=str(SIM / "run_sizing/eplusout.sql"))
    a = ap.parse_args()
    data = sim_hourly(Path(a.sql))
    zu = zone_units()
    wb = workbook_duties(ROOT / a.issue)
    envs = sorted(data)
    rows = []
    units = sorted({z["unit"] for z in zu.values()})
    for unit in units:
        zones = [k for k, z in zu.items() if z["unit"] == unit]
        r = dict(unit=unit, storeys=" ".join(sorted({zu[k]["storey"] for k in zones})), zones=len(zones),
                 rooms_m2=round(sum(float(zu[k]["rooms_m2"]) for k in zones), 1))
        for env in envs:
            d = data[env]
            ts = sorted({t for z in zones for t in d.get((z, "tot"), {})})
            series = {v: [sum(d.get((z, v), {}).get(t, 0.0) for z in zones) for t in ts] for v in VARS.values()}
            if not ts:
                continue
            i = max(range(len(ts)), key=lambda j: series["tot"][j])
            tag = "SS553" if "SS553" in env.upper() else ("DP" if "DP=>MDB" in env.upper() else ("WB" if "WB=>MDB" in env.upper() else env))
            r[f"{tag} coil kW"] = round(series["tot"][i] / 1000, 1)
            r[f"{tag} OA kW"] = round(series["oa"][i] / 1000, 1)
            r[f"{tag} room kW"] = round((series["tot"][i] - series["oa"][i]) / 1000, 1)
            r[f"{tag} latent kW"] = round(series["lat"][i] / 1000, 1)
            r[f"{tag} peak hour"] = i % 24 + 1
        w = wb.get(unit)
        if w:
            r["workbook room kW"] = round(w["room_s"] + w["room_l"], 1)
            r["workbook coil kW (D)"] = round(w["coil_d"], 1)
            r["workbook coil kW (B)"] = round(w["coil_b"], 1)
            r["workbooks"] = " ".join(sorted(set(w["books"])))
            if r.get("SS553 coil kW") is not None and w["coil_d"]:
                r["SS553 coil / workbook (D)"] = round(r["SS553 coil kW"] / w["coil_d"], 2)
            if r.get("SS553 room kW") is not None and (w["room_s"] + w["room_l"]):
                r["SS553 room / workbook"] = round(r["SS553 room kW"] / (w["room_s"] + w["room_l"]), 2)
        rows.append(r)
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with (SIM / "results_design_days.csv").open("w", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=keys)
        wr.writeheader()
        wr.writerows(rows)
    tot = defaultdict(float)
    for r in rows:
        for k, v in r.items():
            if isinstance(v, float) and k.endswith("kW"):
                tot[k] += v
    print("envs", envs)
    print({k: round(v) for k, v in tot.items()})
    print("units", len(rows), "with workbook", sum(1 for r in rows if "workbook room kW" in r))


if __name__ == "__main__":
    main()
