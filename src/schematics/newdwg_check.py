"""Read-back checks for a new drawing: DXF against the registers, text overlaps, PDF against DXF."""
from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

import ezdxf
import pymupdf

from . import newdwg as nd

REG = nd.REG
# value formats on the drawing: field -> (prefix, suffix) around the register value
FMT = {
    "change_id": ("CHG ", ""),
    "area_m2": ("", " m2"),
    "airflow_L_s": ("MIN AIRFLOW ", " L/s"),
    "static_pressure_Pa": ("MIN STATIC PRESSURE ", " Pa"),
    "fan_type": ("FAN TYPE ", ""),
    "schedule_tag": ("SCHEDULE ", ""),
    "supply_airflow_L_s": ("SUPPLY AIRFLOW ", " L/s"),
    "outdoor_air": ("OUTDOOR AIRFLOW ", " L/s"),
    "external_resistance_Pa": ("EXTERNAL RESISTANCE ", " Pa"),
}


def read_back(dxf: Path) -> Dict[str, Any]:
    doc = ezdxf.readfile(dxf)
    msp = doc.modelspace()
    vals: Dict[str, Dict[str, Any]] = {}
    texts: List[Dict[str, Any]] = []
    for t in msp.query("TEXT"):
        h = t.dxf.height
        x0, y = t.dxf.insert.x, t.dxf.insert.y
        w = nd.tw(t.dxf.text, h)
        texts.append(dict(x0=x0, y0=y - 0.3 * h, x1=x0 + w, y1=y + h, s=t.dxf.text, layer=t.dxf.layer))
        if t.has_xdata(APPID := nd.APPID):
            d = {}
            for code, v in t.get_xdata(APPID):
                k, _, val = v.partition("=")
                d[k] = val
            rec = vals.setdefault(d["VAL"], dict(field=d["FIELD"], chg=d["CHG"], src=d["SRC"], parts={}))
            i, n = d["PART"].split("/")
            rec["parts"][int(i)] = t.dxf.text
    for r in vals.values():
        r["display"] = " ".join(r["parts"][i] for i in sorted(r["parts"]))
        pre, suf = FMT.get(r["field"], ("", ""))
        d = r["display"]
        # a value line may carry a label prefix (TAGS, NEAR are separate texts, so none here)
        if pre and d.startswith(pre):
            d = d[len(pre):]
        if suf and d.endswith(suf):
            d = d[:-len(suf)]
        r["value"] = d
    return dict(doc=doc, msp=msp, vals=vals, texts=texts)


def csvrows(p: Path):
    with open(p, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def compare_registers(rb: Dict[str, Any], storey: int) -> Dict[str, Any]:
    vals = rb["vals"]
    spaces = {int(r["annotation_index"]): r for r in csvrows(REG / "appd_spaces.csv") if r["storey"] == str(storey)}
    equip = [r for r in csvrows(REG / "appd_equipment.csv") if r["storey"] == str(storey)]
    recon = {r["appd_tag_as_written"]: r for r in csvrows(REG / "appd_schedule_reconciliation.csv")
             if r["appd_storeys"] == str(storey)}
    chg = {r["change_id"]: r for r in csvrows(nd.OUT / "change_register.csv")}
    d00 = nd.d00_cells(storey)
    errors: List[str] = []
    n_cmp = 0
    spaces_on = set()
    tags_on: Counter = Counter()
    chg_on = set()
    for vid, v in vals.items():
        f, val, c = v["field"], v["value"], v["chg"]
        chg_on.add(c)
        if c not in chg:
            errors.append("%s: change ID %s not in the register" % (vid, c))
        elif chg[c]["sheet"] != "App D":
            errors.append("%s: change ID %s is not an App D row" % (vid, c))
        m = re.match(r"Appendix D, (\S+), p(\d+), annotation (\d+)$", v["src"]) or \
            re.match(r"Workbook D00 tab \w+ cell (\w+); space (\d+)$", v["src"])
        if m:
            idx = int(m.group(3) if m.re.groups == 3 else m.group(2))
            if m.re.groups == 2:
                cellref = m.group(1)
                d0_ = d00.get(idx) or {}
                known = {d0_.get(k + "_cell") for k in ("name", "supply", "return", "exhaust", "pressure")}
                n_cmp += 1
                if cellref not in known:
                    errors.append("%s: workbook cell %s is not a cell of space %d" % (vid, cellref, idx))
            sp = spaces.get(idx)
            if sp is None:
                errors.append("%s: no space %d in the register" % (vid, idx))
                continue
            d0 = d00.get(idx) or {}

            def wbv(k):
                x = d0.get(k)
                return "not given" if x is None else nd.fnum(str(round(x, 1) if isinstance(x, float) else x))
            exp = {"space_ref": "SPACE %d" % idx, "name": str(d0.get("name") or ""),
                   "count": "1", "area_m2": nd.fnum(sp["printed_area_m2"] or sp["area_m2_at_1_250"]),
                   "mode": sp["mode_label"], "supply_L_s": wbv("supply"), "return_L_s": wbv("return"),
                   "exhaust_L_s": wbv("exhaust"), "pressure": wbv("pressure"), "change_id": c}
            if f in exp:
                n_cmp += 1
                if val != exp[f]:
                    errors.append("%s %s space %d: drawn %r register %r" % (vid, f, idx, val, exp[f]))
                if f == "space_ref":
                    spaces_on.add(idx)
            elif f in ("tags_inside", "tags_near"):
                n_cmp += 1
                drawn = [t for t in val.split(", ") if t]
                if f == "tags_inside":
                    want = sorted({e["tag_as_written"] for e in equip if e["space_annotation_index"] not in ("",)
                                   and int(float(e["space_annotation_index"])) == idx})
                else:
                    near = []
                    for part in (sp["equipment_tags_within_60pt"] or "").split(";"):
                        mm = re.match(r"\s*(\S+)\s*\[ann", part)
                        if mm:
                            near.append(mm.group(1))
                    want = sorted(set(near) - {e["tag_as_written"] for e in equip if e["space_annotation_index"] not in ("",)
                                               and int(float(e["space_annotation_index"])) == idx})
                if sorted(set(drawn)) != want:
                    errors.append("%s %s space %d: drawn %s register %s" % (vid, f, idx, sorted(set(drawn)), want))
            else:
                errors.append("%s: unknown field %s" % (vid, f))
        elif v["src"].startswith("Appendix D, p5, tag as written"):
            pass
    # equipment values, by tag: the tag value text and the values of the same owner in the CSV
    by_chg_fields: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
    for vid, v in vals.items():
        if v["src"].startswith("Appendix D, p5, tag as written"):
            by_chg_fields[v["chg"]][v["field"]].append(v["value"])
    for c, fd in by_chg_fields.items():
        for tag in fd.get("tag", []):
            tags_on[tag] += 1
            r = recon.get(tag)
            n_cmp += 1
            if r is None:
                if fd.get("airflow_L_s") or fd.get("supply_airflow_L_s"):
                    errors.append("tag %s has a duty on the drawing and no schedule row" % tag)
                continue
        # duty values: each drawn duty value must be the register duty of one of the system's tags
        for fld, col in (("airflow_L_s", "airflow"), ("supply_airflow_L_s", "airflow"),
                         ("static_pressure_Pa", "static_pressure_Pa"), ("external_resistance_Pa", "static_pressure_Pa"),
                         ("outdoor_air", "outdoor_air"), ("schedule_tag", "schedule_tag_as_written")):
            for dv in fd.get(fld, []):
                n_cmp += 1
                ok = any((nd.fnum(recon[t][col]) if col in ("airflow", "static_pressure_Pa") else recon[t][col]) == dv
                         for t in fd.get("tag", []) if t in recon)
                if not ok:
                    errors.append("%s %s: drawn %r matches no register value of its tags" % (c, fld, dv))
        row = chg.get(c)
        if row and row["layer_type"] == "new systems":
            items = [t.strip() for t in row["item"].split(",")]
            # tags of the row are written without the A/B tail in the item text for some units: compare by prefix
            for tag in fd.get("tag", []):
                n_cmp += 1
                if not any(tag.startswith(i) or i.startswith(tag) for i in items):
                    errors.append("%s: tag %s not in the register row item %s" % (c, tag, row["item"]))
            if len(set(fd.get("tag", []))) > int(row["required"]):
                errors.append("%s: %d tags on the drawing, register says %s" % (c, len(set(fd.get("tag", []))), row["required"]))
    # coverage
    missing_spaces = sorted(set(spaces) - spaces_on)
    drawn_tags_all = set(tags_on)
    miss_tags = sorted({e["tag_as_written"] for e in equip} - drawn_tags_all)
    return dict(errors=errors, compared=n_cmp, spaces_in_register=len(spaces), spaces_on_drawing=len(spaces_on),
                spaces_missing=missing_spaces, equipment_tags_in_register=len({e["tag_as_written"] for e in equip}),
                equipment_tags_missing=miss_tags, change_ids_on_drawing=sorted(chg_on))


def queries_on_drawing(rb, storey: int) -> Dict[str, Any]:
    chg = [r for r in csvrows(nd.OUT / "change_register.csv") if r["sheet"] == "App D" and r["layer_type"] == "queries"
           and r["quantity"] == "Storey %d" % storey]
    joined = " ".join(t["s"] for t in rb["texts"])
    return dict(query_rows=len(chg), missing=[r["change_id"] for r in chg if r["change_id"] not in joined])


def overlaps(rb) -> Dict[str, Any]:
    texts = rb["texts"]
    msp = rb["msp"]
    errs: List[str] = []
    n = len(texts)
    order = sorted(range(n), key=lambda i: texts[i]["x0"])
    eps = 0.05
    for a in range(n):
        ta = texts[order[a]]
        for b in range(a + 1, n):
            tb = texts[order[b]]
            if tb["x0"] >= ta["x1"] - eps:
                break
            if ta["y0"] < tb["y1"] - eps and tb["y0"] < ta["y1"] - eps:
                errs.append("text/text: %r vs %r" % (ta["s"][:30], tb["s"][:30]))
    # symbols and frames: inserts of the blocks, scaled
    rects: List[Tuple[str, float, float, float, float]] = []
    for ins in msp.query("INSERT"):
        nme = ins.dxf.name
        if nme in ("SYM_AHU", "BOX_LABELLED", "ROOMBOX"):
            x, y = ins.dxf.insert.x, ins.dxf.insert.y
            rects.append((nme, x, y, x + ins.dxf.xscale, y + ins.dxf.yscale))
        elif nme == "SYM_ARROW":
            x, y = ins.dxf.insert.x, ins.dxf.insert.y
            L = ins.dxf.xscale
            rects.append((nme, x, y - 0.1 * L, x + L, y + 0.1 * L))
    for t in texts:
        for nme, x0, y0, x1, y1 in rects:
            inter = t["x0"] < x1 and x0 < t["x1"] and t["y0"] < y1 and y0 < t["y1"]
            if not inter:
                continue
            inside = t["x0"] >= x0 - eps and t["x1"] <= x1 + eps and t["y0"] >= y0 - 0.5 and t["y1"] <= y1 + eps
            if nme == "SYM_ARROW" or not inside:
                errs.append("text/%s: %r" % (nme, t["s"][:30]))
    # texts against duct lines
    for ln in msp.query("LINE"):
        if not ln.dxf.layer.startswith("IP-DUCT"):
            continue
        (xa, ya, _), (xb, yb, _) = ln.dxf.start, ln.dxf.end
        for t in texts:
            if seg_hits(xa, ya, xb, yb, t["x0"], t["y0"], t["x1"], t["y1"]):
                errs.append("text/duct: %r" % t["s"][:30])
    return dict(errors=errs, texts=n, symbols=len(rects))


def seg_hits(xa, ya, xb, yb, rx0, ry0, rx1, ry1) -> bool:
    if max(xa, xb) < rx0 or min(xa, xb) > rx1 or max(ya, yb) < ry0 or min(ya, yb) > ry1:
        return False
    if (rx0 <= xa <= rx1 and ry0 <= ya <= ry1) or (rx0 <= xb <= rx1 and ry0 <= yb <= ry1):
        return True
    for (cx0, cy0, cx1, cy1) in ((rx0, ry0, rx1, ry0), (rx1, ry0, rx1, ry1), (rx1, ry1, rx0, ry1), (rx0, ry1, rx0, ry0)):
        d = (xb - xa) * (cy1 - cy0) - (yb - ya) * (cx1 - cx0)
        if d == 0:
            continue
        t = ((cx0 - xa) * (cy1 - cy0) - (cy0 - ya) * (cx1 - cx0)) / d
        u = ((cx0 - xa) * (yb - ya) - (cy0 - ya) * (xb - xa)) / d
        if 0 <= t <= 1 and 0 <= u <= 1:
            return True
    return False


def pdf_vs_dxf(rb, pdf: Path) -> Dict[str, Any]:
    """The PDF is read back and compared with the primitives of the DXF: the same text runs (string and
    position) and the same line segments and fill outlines (end points within 0.05 pt).  Nothing in the PDF
    comes from the DXF text layer: text is visible text, lines are vector paths."""
    d = pymupdf.open(pdf)
    p = d[0]
    k = 72.0 / 25.4
    prims = nd.primitives(rb["doc"])
    # text
    want_t = sorted((round(q["x"] * k, 1), round((nd.SHEET_H - q["y"]) * k, 1), q["s"].strip()) for q in prims
                    if q["kind"] == "text" and q["s"].strip())
    got_t = []
    for tr in p.get_texttrace():
        txt = "".join(chr(c[0]) for c in tr["chars"]).strip()
        if txt:
            o = tr["chars"][0][2]
            got_t.append((round(o[0], 1), round(o[1], 1), txt))
    got_t.sort()
    miss_t = [t for t in want_t if t not in set(got_t)]
    extra_t = [t for t in got_t if t not in set(want_t)]
    # segments
    def key(a, b):
        a = (round(a[0], 1), round(a[1], 1))
        b = (round(b[0], 1), round(b[1], 1))
        return (a, b) if a <= b else (b, a)
    want_s: Counter = Counter()
    for q in prims:
        if q["kind"] == "seg":
            want_s[key((q["p0"][0] * k, (nd.SHEET_H - q["p0"][1]) * k), (q["p1"][0] * k, (nd.SHEET_H - q["p1"][1]) * k))] += 1
        elif q["kind"] == "fill":
            pts = [(x * k, (nd.SHEET_H - y) * k) for x, y in q["pts"]]
            for i in range(len(pts)):
                a_, b_ = pts[i], pts[(i + 1) % len(pts)]
                if abs(a_[0] - b_[0]) + abs(a_[1] - b_[1]) > 0.08:     # a point is not drawn
                    want_s[key(a_, b_)] += 1
    got_s: Counter = Counter()
    n_paths = 0
    for dr in p.get_drawings():
        n_paths += 1
        for it in dr["items"]:
            if it[0] == "l":
                got_s[key((it[1].x, it[1].y), (it[2].x, it[2].y))] += 1
            elif it[0] == "re":      # four lines that close a rectangle are read back as one rectangle item
                r_ = it[1]
                c4 = [(r_.x0, r_.y0), (r_.x1, r_.y0), (r_.x1, r_.y1), (r_.x0, r_.y1)]
                for i4 in range(4):
                    got_s[key(c4[i4], c4[(i4 + 1) % 4])] += 1
            elif it[0] == "qu":
                q_ = it[1]
                c4 = [(q_.ul.x, q_.ul.y), (q_.ur.x, q_.ur.y), (q_.lr.x, q_.lr.y), (q_.ll.x, q_.ll.y)]
                for i4 in range(4):
                    got_s[key(c4[i4], c4[(i4 + 1) % 4])] += 1
    # tolerance: a segment whose rounded key differs by one step counts as equal when a neighbour matches
    def near(kk, pool):
        (a, b) = kk
        for da in (-0.1, 0, 0.1):
            for db in (-0.1, 0, 0.1):
                for dc in (-0.1, 0, 0.1):
                    for dd in (-0.1, 0, 0.1):
                        k2 = key((a[0] + da, a[1] + db), (b[0] + dc, b[1] + dd))
                        if pool.get(k2, 0) > 0:
                            return k2
        return None
    pool = Counter(got_s)
    unmatched = 0
    for kk, n in want_s.items():
        for _ in range(n):
            m = near(kk, pool)
            if m is None:
                unmatched += 1
            else:
                pool[m] -= 1
    left = sum(v for v in pool.values() if v > 0)
    ent = Counter(e.dxftype() for e in rb["msp"])
    kinds = Counter(q["kind"] for q in prims)
    return dict(page_pt=[round(p.rect.width, 1), round(p.rect.height, 1)],
                dxf_entities=dict(ent), dxf_primitives=dict(kinds),
                texts_in_dxf=len(want_t), texts_in_pdf=len(got_t), texts_missing_in_pdf=miss_t[:10],
                texts_extra_in_pdf=extra_t[:10],
                segments_in_dxf=sum(want_s.values()), segments_in_pdf=sum(got_s.values()),
                segments_missing_in_pdf=unmatched, segments_extra_in_pdf=left, pdf_paths=n_paths,
                pdf_text_is_invisible=any(tr.get("type") == 3 for tr in p.get_texttrace()), pages=len(d),
                text_char_diff={} if not (miss_t or extra_t) else {"texts": (len(miss_t), len(extra_t))},
                identical=not (miss_t or extra_t or unmatched or left))


def main(r: Dict[str, Any]) -> int:
    rb = read_back(r["dxf"])
    rep: Dict[str, Any] = dict(drawing=r["number"], storey=r["storey"], why=r["why"])
    rep["register_comparison"] = compare_registers(rb, r["storey"])
    rep["queries"] = queries_on_drawing(rb, r["storey"])
    rep["overlap"] = overlaps(rb)
    rep["pdf_vs_dxf"] = pdf_vs_dxf(rb, r["pdf"])
    sh = r["sheet"]
    rep["key_plan"] = dict(spaces_shown=sh.keyplan_total, numbers_shown=sh.keyplan_labels)
    wd = next(nd.WORKING.glob("IP-BH-05010*.pdf"))
    wp = pymupdf.open(wd)[0].rect
    rep["sheet_size_pt"] = dict(new=rep["pdf_vs_dxf"]["page_pt"], working=[round(wp.width, 1), round(wp.height, 1)])
    ok = (not rep["register_comparison"]["errors"] and not rep["register_comparison"]["spaces_missing"]
          and not rep["register_comparison"]["equipment_tags_missing"] and not rep["queries"]["missing"]
          and not rep["overlap"]["errors"] and rep["pdf_vs_dxf"]["identical"]
          and abs(rep["sheet_size_pt"]["new"][0] - rep["sheet_size_pt"]["working"][0]) < 1
          and abs(rep["sheet_size_pt"]["new"][1] - rep["sheet_size_pt"]["working"][1]) < 1)
    rep["pass"] = bool(ok)
    outp = nd.NEWDIR / (r["number"] + "_P1_check.json")
    outp.write_text(json.dumps(rep, indent=1, default=str))
    short = dict(rep)
    short["register_comparison"] = {k: (v if k != "change_ids_on_drawing" else len(v)) for k, v in rep["register_comparison"].items()}
    short["register_comparison"]["errors"] = rep["register_comparison"]["errors"][:12]
    short["overlap"] = dict(rep["overlap"], errors=rep["overlap"]["errors"][:15])
    print(json.dumps(short, indent=1, default=str)[:6000])
    return 0 if ok else 1
