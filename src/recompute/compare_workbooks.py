"""Compare the independent recomputation (``thermal.py``) with the recalculated department workbooks.

``python3 -m src.recompute.compare_workbooks [--inputs out/loads/inputs] [--workbooks out/loads/workbooks] [--out out/qa/recompute_comparison.csv]``

For each department input JSON ``D<nn>.json`` the matching workbook ``AIGH_C2_D<nn>_*.xlsx`` is read from its stored (recalculated)
values. Room: supply, outdoor air, sensible, latent, pressure offset needed (D-028). AHU: supply, outdoor air (highest-fraction method), room sensible, room latent.
A value agrees when it is within 0.5 % of the recomputed value, or within 0.05 (L/s, W or kW: rounding of the stored value).
The comparison reads the workbook by header text, not by column letter.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List

import openpyxl

from .thermal import WORKBOOK_RULES, recompute

TOL_PCT = 0.5
TOL_ABS = 0.05
ROOM_METRICS = (("supply L/s", "Final supply", "supply_lps"), ("outdoor air L/s", "Outdoor air", "oa_lps"),
                ("sensible W", "Room sensible", "sensible_w"), ("latent W", "Room latent", "latent_w"))
AHU_METRICS = (("supply L/s", "AHU supply airflow", lambda a: a["supply_lps"]),
               ("outdoor air L/s", "AHU outdoor air = fraction × supply", lambda a: a["oa_lps"]),
               ("room sensible kW", "Room sensible", lambda a: a["room_sensible_kw"]),
               ("room latent kW", "Room latent", lambda a: a["room_latent_kw"]))


def _round_areas(data: Dict[str, Any]) -> Dict[str, Any]:
    """Copy of the input with every area taken to 0.1 m2 before any test (spreadsheet ROUND: halves away from zero)."""
    def r1(v):
        return None if v is None else math.floor(abs(v) * 10.0 + 0.5 + 1e-9) / 10.0
    rooms = [dict(r, **{k: r1(r.get(k)) for k in ("dpa_area_m2", "soa_area_m2", "measured_area_m2") if r.get(k) is not None}) for r in data["rooms"]]
    return dict(data, rooms=rooms)


CAUSE_AREA = "area test at the tolerance edge: the workbook tests the areas rounded to 0.1 m2, the recomputation tests them unrounded"
CAUSE_AHU = "follows from the room rows of this AHU that carry a cause"


def _row(dept: str, level: str, ref: str, metric: str, mine: Any, theirs: Any) -> Dict[str, Any]:
    m, t = float(mine or 0.0), float(theirs or 0.0)
    diff = abs(m - t)
    pct = 100.0 * diff / abs(t) if t else (0.0 if diff < 1e-9 else 100.0)
    return dict(dept=dept, level=level, ref=ref, metric=metric, workbook=round(m, 6), recomputed=round(t, 6), diff=round(diff, 6),
                diff_pct=round(pct, 4), within_tolerance="yes" if pct <= TOL_PCT or diff <= TOL_ABS else "NO", cause="")


def compare_one(dept: str, data: Dict[str, Any], xlsx: Path) -> List[Dict[str, Any]]:
    # a room with no area (design area 0 or blank, no SOA, no measured area): the area is unknown and is taken as 0, as in the workbook
    data = dict(data, rooms=[dict(r, soa_area_m2=0.0) if not any((r.get(k) or 0) > 0 for k in ("dpa_area_m2", "soa_area_m2", "measured_area_m2")) else r
                             for r in data["rooms"]])
    # The two rules that the engines read differently (OI-13-1 supply floor from the exhaust, OI-R401-1 outdoor air of a room with an offset) are open for Marcus. The
    # comparison reads both engines under the interim reading of the workbook, so that each engine implements the same stated rule on its own. The other reading is
    # compared in ``sensitivity`` (not an explanation of a difference).
    res = recompute(data, "HIGHEST_FRACTION", rules=WORKBOOK_RULES)
    res_b = recompute(_round_areas(data), "HIGHEST_FRACTION", rules=WORKBOOK_RULES)
    wb = openpyxl.load_workbook(xlsx, data_only=True)
    rows: List[Dict[str, Any]] = []
    af = wb["Airflow"]
    hdr = {c.value: i for i, c in enumerate(af[5]) if c.value and c.value not in {}}
    by_no = {r["number"]: r for r in res["rooms"]}
    seen = set()
    for row in af.iter_rows(min_row=7, values_only=True):
        no = row[0]
        if not no or no not in by_no:
            continue
        seen.add(no)
        for lab, col, key in ROOM_METRICS:
            rows.append(_row(dept, "room", no, lab, row[hdr[col]], by_no[no][key]))
    # D-028: the pressure offset of every room with a pressure row (offset needed, m3/h) against the independent solution of the same method
    ps = wb["Pressure"]
    ph: Dict[str, int] = {}
    for i, c in enumerate(ps[5]):
        if c.value and c.value not in ph:
            ph[c.value] = i
    if "Offset needed" in ph:
        for row in ps.iter_rows(min_row=7, values_only=True):
            no = row[0]
            pr = (by_no.get(no) or {}).get("pressure") if no else None
            if pr and pr.get("mode") == "controlled" and isinstance(row[ph["Offset needed"]], (int, float)):
                rows.append(_row(dept, "room", no, "pressure offset needed m3/h", row[ph["Offset needed"]], pr["offset_needed_m3h"]))
    for no in by_no:
        if no not in seen:
            rows.append(dict(dept=dept, level="room", ref=no, metric="present in workbook", workbook="", recomputed="", diff="", diff_pct="", within_tolerance="NO", cause=""))
    ah = wb["AHU"]
    ah_hdr = {c.value: i for i, c in enumerate(ah[5]) if c.value}
    by_tag = {}
    for row in ah.iter_rows(min_row=7, values_only=True):
        if not row[0] or row[0] == "TOTAL":
            break
        by_tag[str(row[0])] = row
    for a in res["ahus"]:
        row = by_tag.get(str(a["tag"]))
        for lab, col, fn in AHU_METRICS:
            if row is None:
                rows.append(dict(dept=dept, level="AHU", ref=a["tag"], metric=lab, workbook="", recomputed=round(fn(a), 6), diff="", diff_pct="", within_tolerance="NO", cause=""))
            else:
                rows.append(_row(dept, "AHU", a["tag"], lab, row[ah_hdr[col]], fn(a)))
    # causes: a value outside the tolerance is explained only when the named rule reproduces the workbook value
    by_no_b = {r["number"]: r for r in res_b["rooms"]}
    by_tag_b = {a["tag"]: a for a in res_b["ahus"]}
    cause_rooms: Dict[str, str] = {}
    for r in rows:
        if r["level"] != "room" or r["within_tolerance"] == "yes":
            continue
        key = {"supply L/s": "supply_lps", "outdoor air L/s": "oa_lps", "sensible W": "sensible_w", "latent W": "latent_w"}.get(r["metric"])
        rb = by_no_b.get(r["ref"])
        ra = by_no[r["ref"]]
        if key and rb is not None and _row("", "", "", "", r["workbook"], rb[key])["within_tolerance"] == "yes":
            r["cause"] = CAUSE_AREA
        elif r["metric"] == "pressure offset needed m3/h" and rb is not None and (rb.get("pressure") or {}).get("offset_needed_m3h") is not None \
                and _row("", "", "", "", r["workbook"], rb["pressure"]["offset_needed_m3h"])["within_tolerance"] == "yes":
            r["cause"] = CAUSE_AREA
        if r["cause"]:
            cause_rooms[ra.get("ahu") or ""] = r["cause"]
    for r in rows:
        if r["level"] != "AHU" or r["within_tolerance"] == "yes" or r["recomputed"] == "":
            continue
        if r["ref"] in cause_rooms:
            ab = by_tag_b.get(r["ref"])
            r["cause"] = CAUSE_AREA if cause_rooms[r["ref"]] == CAUSE_AREA and ab is not None else CAUSE_AHU
    return rows


def sensitivity(dept: str, data: Dict[str, Any], rows_main: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Room values that the other reading of the two open rules (OI-13-1, OI-R401-1) would change: the recomputation under that reading against the same recomputation
    under the reading of the workbook. Information for Marcus. It does not explain a difference of the main comparison."""
    data = dict(data, rooms=[dict(r, soa_area_m2=0.0) if not any((r.get(k) or 0) > 0 for k in ("dpa_area_m2", "soa_area_m2", "measured_area_m2")) else r for r in data["rooms"]])
    a = {r["number"]: r for r in recompute(data, "HIGHEST_FRACTION", rules=WORKBOOK_RULES)["rooms"]}
    b = {r["number"]: r for r in recompute(data, "HIGHEST_FRACTION")["rooms"]}
    out = []
    for no, ra in a.items():
        rb = b.get(no)
        if rb is None:
            continue
        for lab, key in (("supply L/s", "supply_lps"), ("outdoor air L/s", "oa_lps")):
            x = _row(dept, "room", no, lab, ra.get(key), rb.get(key))
            if x["within_tolerance"] != "yes":
                out.append(dict(dept=dept, ref=no, metric=lab, workbook_reading=x["workbook"], other_reading=x["recomputed"], diff_pct=x["diff_pct"],
                                open_item="OI-13-1 and OI-R401-1: supply floor from the exhaust, and the outdoor air of a room with a pressure offset"))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", default="out/loads/inputs")
    ap.add_argument("--workbooks", default="out/loads/workbooks")
    ap.add_argument("--out", default="out/qa/recompute_comparison.csv")
    ns = ap.parse_args(argv)
    allrows: List[Dict[str, Any]] = []
    sens: List[Dict[str, Any]] = []
    for js in sorted(Path(ns.inputs).glob("D*.json")):
        dept = js.stem[1:]
        wbs = sorted(Path(ns.workbooks).glob(f"AIGH_C2_D{dept}_*.xlsx"))
        if not wbs:
            print(f"D{dept}: no workbook", file=sys.stderr)
            continue
        rows = compare_one(dept, json.loads(js.read_text(encoding="utf-8")), wbs[0])
        bad = sum(1 for r in rows if r["within_tolerance"] != "yes")
        unexp = sum(1 for r in rows if r["within_tolerance"] != "yes" and not r["cause"])
        print(f"D{dept}: {len(rows)} values, {bad} outside {TOL_PCT} %, {unexp} without a cause")
        allrows.extend(rows)
        sens.extend(sensitivity(dept, json.loads(js.read_text(encoding="utf-8")), rows))
    sp = Path(ns.out).with_name("recompute_sensitivity.csv")
    with open(sp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["dept", "ref", "metric", "workbook_reading", "other_reading", "diff_pct", "open_item"])
        w.writeheader()
        w.writerows(sens)
    Path(ns.out).parent.mkdir(parents=True, exist_ok=True)
    with open(ns.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["dept", "level", "ref", "metric", "workbook", "recomputed", "diff", "diff_pct", "within_tolerance", "cause"])
        w.writeheader()
        w.writerows(allrows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
