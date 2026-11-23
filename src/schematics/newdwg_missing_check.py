"""Read-back checks of the drawings of newdwg_missing: DXF values against the sources, overlaps, PDF against DXF, coverage."""
from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from typing import Any, Dict, List

import ezdxf
import openpyxl
import pymupdf

from . import newdwg as nd
from . import newdwg_check as nc
from . import newdwg_missing as nm

csv.field_size_limit(10 ** 9)
_WB: Dict[str, Any] = {}


def sheet(path, tab):
    key = (str(path), tab)
    if key not in _WB:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        _WB[key] = {c.coordinate: c.value for row in wb[tab].iter_rows() for c in row if c.value is not None}
    return _WB[key]


def txt(v) -> str:
    if v is None or v == "":
        return "not given"
    if isinstance(v, (int, float)):
        return nm.fnum1(v)
    return str(v)


def read_back(dxf) -> Dict[str, Any]:
    doc = ezdxf.readfile(dxf)
    msp = doc.modelspace()
    vals: Dict[str, Dict[str, Any]] = {}
    texts = []
    for t in msp.query("TEXT"):
        h = t.dxf.height
        x0, y = t.dxf.insert.x, t.dxf.insert.y
        w = nd.tw(t.dxf.text, h)
        texts.append(dict(x0=x0, y0=y - 0.3 * h, x1=x0 + w, y1=y + h, s=t.dxf.text, layer=t.dxf.layer))
        if t.has_xdata(nd.APPID):
            d = {}
            for code, v in t.get_xdata(nd.APPID):
                k, _, val = v.partition("=")
                d[k] = val
            rec = vals.setdefault(d["VAL"], dict(field=d["FIELD"], chg=d["CHG"], src=d["SRC"], parts={}))
            i, n = d["PART"].split("/")
            rec["parts"][int(i)] = t.dxf.text
    for r in vals.values():
        d = " ".join(r["parts"][i] for i in sorted(r["parts"]))
        d = re.sub(r"^IP-BH-\w{5}  ", "", d) if r["field"] == "pahu_serves" else d
        if r["field"] == "oa_source" and d.startswith("WORKBOOK: "):
            d = d[len("WORKBOOK: "):]
        r["display"] = d
        pre, suf = nm.FMT.get(r["field"], ("", ""))
        if pre and d.startswith(pre):
            d = d[len(pre):]
        if suf and d.endswith(suf):
            d = d[:-len(suf)]
        r["value"] = d
    return dict(doc=doc, msp=msp, vals=vals, texts=texts)


class Sources:
    def __init__(self):
        self.miss = {r["change_id"]: r for r in csv.DictReader(open(nm.MISSING, encoding="utf-8")) if r["change_id"]}
        self.air = list(csv.DictReader(open(nm.REG / "equipment_schedule_air_handling.csv", encoding="utf-8")))
        self.fans = list(csv.DictReader(open(nm.REG / "equipment_schedule_fans.csv", encoding="utf-8")))
        self.sys = {r["system_id"]: r for r in csv.DictReader(open(nm.REG / "schematic_systems.csv", encoding="utf-8"))}
        self.links = list(csv.DictReader(open(nm.REG / "room_unit_links.csv", encoding="utf-8")))
        import glob
        self.wbpath = lambda d: glob.glob(str(nm.ROOT / "out/loads/workbooks") + "/AIGH_C2_D%s_*.xlsx" % d)[0]
        self.master = str(nm.ROOT / "out/loads/workbooks/AHU_master_summary.xlsx")
        self.zone = str(nm.ROOT / "out/zoning/zone_schedule.xlsx")


def expected(S: Sources, v: Dict[str, Any]):
    """Value text re-derived from the source named in the value; None when the source kind is not a cell."""
    src, f = v["src"], v["field"]
    if f == "change_id":
        return v["chg"]
    m = re.match(r"Workbook D(\d\d) tab (\w+) cells? (.*)$", src)
    if m:
        cells = [c.strip() for c in m.group(3).split(";")]
        if f == "count":
            return str(len(cells))
        if f == "room_no":
            return ", ".join(txt(sheet(S.wbpath(m.group(1)), m.group(2)).get(c)) for c in cells)
        return txt(sheet(S.wbpath(m.group(1)), m.group(2)).get(cells[0]))
    m = re.match(r"Workbook master summary, tab AHU Master, cell (\w+)", src)
    if m:
        return txt(sheet(S.master, "AHU Master").get(m.group(1)))
    m = re.match(r"Zone schedule, tab ([\w ]+), cell (\w+)", src)
    if m:
        return txt(sheet(S.zone, m.group(1)).get(m.group(2)))
    m = re.match(r"Equipment schedule, (air handling|fans), page (\S+), tag (.+?), column (.*?)(, value (.*))?$", src)
    if m:
        rows = S.air if m.group(1) == "air handling" else S.fans
        pg, tag, col = m.group(2), m.group(3), m.group(4)
        r = next((x for x in rows if x["tag_as_written"] == tag and (x.get("page(s)") or x.get("page")) == pg), None)
        if r is None:
            return "NO SCHEDULE ROW"
        val = r.get(col, "")
        if f == "fan_type":
            return {"Axial": "AXIAL", "Centrif SISW": "CENTRIFUGAL"}.get(val, "?" + val)
        return nd.fnum(val) if re.fullmatch(r"-?[\d.]+", val or "") else val
    m = re.match(r"List missing_systems.csv, change ID (\S+), column (\w+)", src)
    if m:
        r = S.miss[m.group(1)]
        if f == "named_on_sheets":
            return ", ".join(re.findall(r"IP-BH-\w{5}", r["source"]))
        return r[m.group(2)]
    m = re.match(r"List schematic_systems.csv, system_id (\S+), columns", src)
    if m:
        r = S.sys[m.group(1)]
        return r["ahu_tags"] + " | " + r["oa_L_s_as_written"]
    m = re.match(r"List room_unit_links.csv, unit_class (.+?), rooms found", src)
    if m:
        n = 0
        for l in S.links:
            if l["unit_class"] == m.group(1):
                try:
                    ws = sheet(S.wbpath(l["dept"]), "Airflow")
                except Exception:
                    continue
                if any(c.column_letter == "A" and v == l["dpa_room_id"] for c, v in ()):
                    pass
                if l["dpa_room_id"] in {v for k, v in ws.items() if k.startswith("A")}:
                    n += 1
        return str(n)
    return None


def compare(rb, S: Sources, items) -> Dict[str, Any]:
    errs: List[str] = []
    n = 0
    unknown = []
    for vid, v in rb["vals"].items():
        e = expected(S, v)
        if v["chg"] not in S.miss:
            errs.append("%s: change ID %s not in the missing systems list" % (vid, v["chg"]))
        if e is None:
            unknown.append((vid, v["field"], v["src"][:50]))
            continue
        n += 1
        if v["value"] != e:
            errs.append("%s %s: drawn %r, source %r (%s)" % (vid, v["field"], v["value"], e, v["src"][:60]))
    return dict(errors=errs, compared=n, no_cell_source=unknown[:5], values=len(rb["vals"]))


def main(results: List[Dict[str, Any]]) -> int:
    S = Sources()
    d = nm.Data()
    seen_tags: Counter = Counter()
    seen_sep: Counter = Counter()
    zone_ids: Dict[str, List[str]] = defaultdict(list)
    chg_seen = set()
    reps = []
    ok = True
    for r in results:
        rb = read_back(r["dxf"])
        rep = dict(drawing=r["number"], storey=r["storey"])
        rep["values"] = compare(rb, S, r["sheet"].items)
        rep["overlap"] = nc.overlaps(rb)
        rep["pdf_vs_dxf"] = nc.pdf_vs_dxf(rb, r["pdf"])
        for vid, v in rb["vals"].items():
            chg_seen.add(v["chg"])
            if v["field"] == "tag" and v["src"].startswith("Zone schedule"):
                seen_tags[v["value"]] += 1
            if v["field"] == "tag" and v["src"].startswith("List missing_systems"):
                seen_tags[v["value"]] += 1
            if v["field"] == "room_no" and v["owner"] if False else False:
                pass
            if v["field"] == "zone_room_id":
                zone_ids[v["chg"]].append(v["value"])
        # separated rooms: box room_no values under FCU-less boxes: those with owner "box:sep:*" are in values csv
        for it in r["sheet"].items:
            if it["kind"] == "sepgrp":
                for m in it["members"]:
                    seen_sep[m["chg"]] += 1
        rep["sheet_size_ok"] = abs(rep["pdf_vs_dxf"]["page_pt"][0] - 3367.8) < 1 and abs(rep["pdf_vs_dxf"]["page_pt"][1] - 2384.2) < 1
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
        reps.append(rep)
    cov = []
    for row in d.unit_rows():
        t = row["system"]
        cov.append(dict(kind="unit", item=t, change_id=row["change_id"], drawn=seen_tags.get(t, 0)))
    for row in d.pahu_rows():
        cov.append(dict(kind="outdoor-air unit", item=row["system"], change_id=row["change_id"], drawn=seen_tags.get(row["system"], 0)))
    for row in d.sep_rows():
        cov.append(dict(kind="separated room", item=row["system"], change_id=row["change_id"], drawn=seen_sep.get(row["change_id"], 0)))
    bad = [c for c in cov if c["drawn"] != 1]
    # zone rooms of each unit equal the zoning rooms
    zbad = []
    for r in results:
        for it in r["sheet"].items:
            if it["kind"] == "unit":
                want = sorted(str(x["room_id"]) for x in d.zrooms.get(it["tag"], []))
                got = sorted(zone_ids.get(it["chg"], []))
                if want != got:
                    zbad.append((it["tag"], want, got))
    num = {r["number"]: [nm.item_id(i) for i in r["sheet"].items] for r in results}
    with open(nd.NEWDIR / "missing_systems_coverage.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["kind", "item", "change_id", "drawn", "drawing"])
        w.writeheader()
        for c in cov:
            c["drawing"] = ";".join(r["number"] for r in results if any(
                (it["kind"] != "sepgrp" and it["kind"] != "unit" and it.get("tag") == c["item"]) or (it["kind"] == "unit" and it["tag"] == c["item"])
                or (it["kind"] == "sepgrp" and any(m["chg"] == c["change_id"] for m in it["members"])) for it in r["sheet"].items))
            w.writerow(c)
    print("coverage items", len(cov), "not drawn exactly once", bad[:5], "zone room mismatches", zbad[:3])
    ok &= not bad and not zbad
    print("ALL PASS" if ok else "FAIL")
    return 0 if ok else 1
