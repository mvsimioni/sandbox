"""Checks of the FCU-with-no-tag sheets: values against the sources, overlaps, PDF against DXF, coverage."""
from __future__ import annotations

import csv
import json
from collections import Counter
from typing import Any, Dict, List

from . import newdwg as nd
from . import newdwg_check as nc
from . import newdwg_fcu as nf
from . import newdwg_missing_check as mc


def main(results: List[Dict[str, Any]], not_drawn: List[Dict[str, str]]) -> int:
    S = mc.Sources()
    fd = nf.FcuData()
    groups, _nd = fd.items()
    ok = True
    seen: Counter = Counter()
    where: Dict[str, str] = {}
    zone_seen: Dict[str, List[str]] = {}
    for r in results:
        rb = mc.read_back(r["dxf"])
        rep = dict(drawing=r["number"], storey=r["storey"])
        rep["values"] = mc.compare(rb, S, None)
        rep["overlap"] = nc.overlaps(rb)
        rep["pdf_vs_dxf"] = nc.pdf_vs_dxf(rb, r["pdf"])
        rep["sheet_size_ok"] = abs(rep["pdf_vs_dxf"]["page_pt"][0] - 3367.8) < 1 and abs(rep["pdf_vs_dxf"]["page_pt"][1] - 2384.2) < 1
        for G in r["sheet"].items:
            for F in G["members"]:
                seen[F["chg"]] += 1
                where[F["chg"]] = r["number"]
        for vid, v in rb["vals"].items():
            if v["field"] == "room_no":
                zone_seen.setdefault(v["chg"], []).append(v["value"])
        rep["pass"] = bool(not rep["values"]["errors"] and not rep["overlap"]["errors"] and rep["pdf_vs_dxf"]["identical"]
                           and rep["sheet_size_ok"])
        ok &= rep["pass"]
        (nd.NEWDIR / (r["number"] + "_P1_check.json")).write_text(json.dumps(rep, indent=1, default=str))
        print(r["number"], "values", rep["values"]["values"], "compared", rep["values"]["compared"], "errors", len(rep["values"]["errors"]),
              "overlap", len(rep["overlap"]["errors"]), "pdf identical", rep["pdf_vs_dxf"]["identical"], "pass", rep["pass"])
        for e in rep["values"]["errors"][:6]:
            print("    V", e)
        for e in rep["overlap"]["errors"][:8]:
            print("    O", e)
    cov, bad = [], []
    for st, gs in groups.items():
        for G in gs:
            for F in G["members"]:
                n = seen.get(F["chg"], 0)
                got = zone_seen.get(F["chg"], [])
                if n != 1 or got != [str(F["room"]["room_id"])]:
                    bad.append((F["chg"], n, got))
                cov.append(dict(change_id=F["chg"], item=F["tag"], drawn=n, drawing=where.get(F["chg"], ""), reason="drawn once with its room"))
    for x in not_drawn:
        cov.append(dict(change_id=x["change_id"], item=x["item"], drawn=0, drawing="", reason=x["reason"]))
    with open(nf.COVERAGE, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["change_id", "item", "drawn", "drawing", "reason"])
        w.writeheader()
        w.writerows(cov)
    print("FCU lines", len(cov), "drawn once", sum(1 for c in cov if c["drawn"] == 1), "listed with a reason", len(not_drawn), "bad", bad[:5])
    ok &= not bad
    print("ALL PASS" if ok else "FAIL")
    return 0 if ok else 1
