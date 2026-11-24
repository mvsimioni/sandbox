"""Storey report of the AHU zoning: every unit, its rooms (DPA IDs), area, supply and facade, with plan images. PDF and CSV.

``python3 -m src.reports.storey_ahu_report --storey L06 --issue out/issue/DRAFT_2026-11-24_R4``

Sources: the issued workbooks of the issue folder (Room Information, Airflow, Envelope; AHU master summary), the assembled inputs (outline links of
each room), the plan room outlines and App D outlines (geometry only), the Arch Combined Tender storey sheet (background).
"""

from __future__ import annotations

import argparse
import colorsys
import csv
import glob
import html
import json
import re
from collections import defaultdict
from pathlib import Path

import numpy as np

REG = Path("out/registers")


def _rows(ws, first=7):
    return [r for r in ws.iter_rows(min_row=first, values_only=True)]


def load_workbooks(issue: Path, storey: str):
    import openpyxl
    rooms = []
    for p in sorted(glob.glob(str(issue / "workbooks" / "AIGH_C2_D*_RevA.xlsx"))):
        wbk = "D" + re.search(r"AIGH_C2_D(\d+)_", p).group(1)
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        ri = wb["Room Information"]
        hdr = list(ri.iter_rows(min_row=5, max_row=5, values_only=True))[0]
        ia = hdr.index("Area used")
        afh = list(wb["Airflow"].iter_rows(min_row=5, max_row=5, values_only=True))[0]
        isup = afh.index("Final supply")
        af = {r[0]: r for r in _rows(wb["Airflow"]) if r[0]}
        env = defaultdict(list)
        for r in _rows(wb["Envelope"]):
            if r[0]:
                env[r[0]].append(dict(o=r[5], glass=float(r[6] or 0), wall=float(r[7] or 0), approx="Approximate" in str(r[18] or ""),
                                      fen=float(r[15] or 0) if isinstance(r[15], (int, float)) else 0.0, trans=float(r[16] or 0) if isinstance(r[16], (int, float)) else 0.0))
        for r in _rows(ri):
            if r[0] and r[6] == storey:
                rooms.append(dict(workbook=wbk, number=r[0], name=r[3] or "", unit=r[9] or "", area=float(r[ia] or 0),
                                  supply=float((af.get(r[0]) or [0] * (isup + 1))[isup] or 0), env=env.get(r[0], [])))
    return rooms


def load_master(issue: Path):
    import openpyxl
    wb = openpyxl.load_workbook(issue / "workbooks" / "AHU_master_summary.xlsx", read_only=True, data_only=True)
    rows = list(wb["AHU Master"].iter_rows(values_only=True))
    hdr = next(r for r in rows if r and r[0] == "AHU or system")
    ix = {h: i for i, h in enumerate(hdr) if h}
    out = {}
    for r in rows:
        if r and isinstance(r[0], str) and re.match(r"^(P?AHU|FCU)-|^AHU group", r[0]):
            out[r[0]] = dict(calc=r[ix.get("Calculated airflow")], tender=r[ix.get("Tender airflow")], rooms=r[ix.get("Rooms")])
    return out


def load_geometry(storey: str):
    """Room number -> list of page-view polygons (App D outline, or the one plan room the room is linked to)."""
    from shapely import wkt
    from src.workbook.room_coverage import cited_ids
    poly = list(csv.DictReader(open(REG / f"room_polygons_{storey}.csv", encoding="utf-8")))
    A, B, pv = [], [], {}
    for r in poly:
        if not r["polygon_pv_wkt"]:
            continue
        try:
            gp = wkt.loads(r["polygon_pv_wkt"]).buffer(0)
        except Exception:
            continue
        pv[r["room_id"]] = gp
        if r["polygon_building_mm_wkt"]:
            gm = wkt.loads(r["polygon_building_mm_wkt"]).buffer(0)
            if not gm.is_empty and gm.area > 2e6 and not gp.is_empty:
                A.append([gm.centroid.x, gm.centroid.y, 1])
                B.append([gp.centroid.x, gp.centroid.y])
    M = np.linalg.lstsq(np.array(A), np.array(B), rcond=None)[0]
    appd = {r["annotation_index"]: wkt.loads(r["polygon_building_mm_wkt"]).buffer(0)
            for r in csv.DictReader(open(REG / "appd_outlines_mm.csv", encoding="utf-8")) if r["storey"] == storey}

    def mm_to_pv(g):
        from shapely.geometry import Polygon
        gs = list(g.geoms) if hasattr(g, "geoms") else [g]
        return [Polygon([tuple(np.array([x, y, 1]) @ M) for x, y in q.exterior.coords]) for q in gs if not q.is_empty]
    geo = {}
    files = sorted(glob.glob("out/loads/inputs/D*.json")) + ["out/loads/inputs_common/D00.json"]
    for f in files:
        d = json.load(open(f, encoding="utf-8"))
        src = {s["id"]: s for s in d["sources"]}
        for r in d["rooms"]:
            if r.get("storey") != storey:
                continue
            gs = []
            for sp in ([r["appd_space"]] if r.get("appd_space") else []) + list(r.get("appd_spaces") or []):
                if str(sp[1]) in appd:
                    gs += mm_to_pv(appd[str(sp[1])])
            if not gs:
                ids = [i for i in cited_ids(r, src) if i.startswith(storey + "-")]
                if len(ids) == 1 and ids[0] in pv:
                    gs = [pv[ids[0]]]
            if gs:
                geo[r["number"]] = gs
    return geo


def colours(n):
    out = []
    for i in range(n):
        h = (i * 0.618033988749895) % 1.0
        r, g, b = colorsys.hsv_to_rgb(h, 0.75, 0.85)
        out.append((int(r * 255), int(g * 255), int(b * 255)))
    return out


def render(storey, items, out_png, focus=None, dpi=110, margin=20, label=True):
    """items: list of (polygons, colour, label, strong_edge). focus: polygons to frame (else whole page)."""
    import pymupdf
    from PIL import Image, ImageDraw, ImageFont
    from src.zoning.crops import BG_PDF, storey_page
    doc = pymupdf.open(BG_PDF)
    page = doc[storey_page(storey) - 1]
    if focus:
        xs = [x for g in focus for x in g.bounds[0::2]]
        ys = [y for g in focus for y in g.bounds[1::2]]
        clip = pymupdf.Rect(min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin) & page.rect
    else:
        allg = [g for it in items for g in it[0]]
        xs = [x for g in allg for x in g.bounds[0::2]]
        ys = [y for g in allg for y in g.bounds[1::2]]
        clip = pymupdf.Rect(min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin) & page.rect
    k = dpi / 72.0
    pix = page.get_pixmap(dpi=dpi, clip=clip, alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("RGBA")
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    font = ImageFont.truetype("DejaVuSans-Bold.ttf", max(10, int(dpi / 9)))
    P = lambda g: [((x - clip.x0) * k, (y - clip.y0) * k) for x, y in g.exterior.coords]
    for gs, c, lab, strong in items:
        for g in gs:
            if g.is_empty:
                continue
            pts = P(g)
            d.polygon(pts, fill=c + (85,))
            d.line(pts + [pts[0]], fill=(255, 120, 0, 255) if strong else c + (255,), width=4 if strong else 2)
    if label:
        for gs, c, lab, strong in items:
            if not gs or not lab:
                continue
            g = max(gs, key=lambda q: q.area)
            p = g.representative_point()
            x, y = (p.x - clip.x0) * k, (p.y - clip.y0) * k
            tb = d.textbbox((x, y), lab, font=font, anchor="mm")
            d.rectangle((tb[0] - 2, tb[1] - 1, tb[2] + 2, tb[3] + 1), fill=(255, 255, 255, 215))
            d.text((x, y), lab, fill=tuple(int(v * 0.7) for v in c) + (255,), font=font, anchor="mm")
    Image.alpha_composite(img, ov).convert("RGB").save(out_png, quality=68)
    doc.close()


def short(unit):
    return re.sub(r"^AHU-B-0\d-0\dDSOT|^AHU-B-0\d-0\d", "", unit).replace("AHU group: ", "grp ") or unit


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--storey", default="L06")
    ap.add_argument("--issue", required=True)
    a = ap.parse_args(argv)
    st, issue = a.storey, Path(a.issue)
    outdir = issue / f"{st}_AHU_report"
    (outdir / "img").mkdir(parents=True, exist_ok=True)
    rooms = load_workbooks(issue, st)
    master = load_master(issue)
    geo = load_geometry(st)
    try:
        ids = json.load(open("out/qa/unit_consolidation.json", encoding="utf-8"))["room_ids"]
    except Exception:
        ids = {"merged": [], "became": []}
    how = {m["brief_room"]: (m["space"], m["how"]) for m in ids.get("merged", [])}
    how.update({m["dpa_id"]: (m["space"], m["how"]) for m in ids.get("became", [])})

    def basis(n):
        if n not in how:
            return "brief room link (schematic, plan room or zoning)"
        sp, h = how[n]
        if h.startswith("plan tag and area"):
            return f"{sp}: tag and area" + (", one of identical rooms" if "one of" in h else "")
        if "only room" in h:
            return f"{sp}: only room with this tag"
        m = re.search(r"differs by (\d+) %", h)
        return f"{sp}: tag and department, area {m.group(1)} %" if m else f"{sp}: {h[:40]}"

    units = sorted({r["unit"] for r in rooms if r["unit"]})
    col = dict(zip(units, colours(len(units))))
    # CSV
    with open(outdir / f"{st}_AHU_rooms_facade.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Storey", "Unit", "Workbook", "DPA room ID", "Room name", "Area used m2", "Final supply L/s", "Facade orientation", "Glazing m2", "Wall m2",
                    "Facade basis", "Drawing space and match basis"])
        for r in sorted(rooms, key=lambda x: (x["unit"] or "~", x["number"])):
            if r["env"]:
                for e in r["env"]:
                    w.writerow([st, r["unit"] or "(no unit)", r["workbook"], r["number"], r["name"], round(r["area"], 1), round(r["supply"]), e["o"], e["glass"], e["wall"],
                                "approximate (zone share)" if e["approx"] else "own outline", basis(r["number"])])
            else:
                w.writerow([st, r["unit"] or "(no unit)", r["workbook"], r["number"], r["name"], round(r["area"], 1), round(r["supply"]), "", "", "", "internal", basis(r["number"])])
    # overall plan
    items = []
    for r in rooms:
        if r["unit"] and r["number"] in geo:
            items.append((geo[r["number"]], col[r["unit"]], "", any(not e["approx"] for e in r["env"])))
    # one label per unit at its largest room
    lab_items = []
    for u in units:
        gs = [g for r in rooms if r["unit"] == u and r["number"] in geo for g in geo[r["number"]]]
        if gs:
            from shapely.ops import unary_union
            lab_items.append(([unary_union(gs).buffer(0)], col[u], short(u), False))
        # overlay labels separately (draw the units' merged outline labels)
    render(st, items, outdir / "img" / "storey.jpg", dpi=110, label=False)
    from PIL import Image, ImageDraw, ImageFont
    # labels on the overall plan: re-render with only label items on top of the coloured image
    import pymupdf
    from src.zoning.crops import storey_page, BG_PDF
    allg = [g for it in items for g in it[0]]
    xs = [x for g in allg for x in g.bounds[0::2]]
    ys = [y for g in allg for y in g.bounds[1::2]]
    pg = pymupdf.open(BG_PDF)[storey_page(st) - 1]
    clip = pymupdf.Rect(min(xs) - 20, min(ys) - 20, max(xs) + 20, max(ys) + 20) & pg.rect
    im = Image.open(outdir / "img" / "storey.jpg").convert("RGBA")
    ov = Image.new("RGBA", im.size, (0, 0, 0, 0))
    dr = ImageDraw.Draw(ov)
    f = ImageFont.truetype("DejaVuSans-Bold.ttf", 15)
    k = 110 / 72.0
    for gs, c, lab, _ in lab_items:
        p = gs[0].representative_point()
        x, y = (p.x - clip.x0) * k, (p.y - clip.y0) * k
        tb = dr.textbbox((x, y), lab, font=f, anchor="mm")
        dr.rectangle((tb[0] - 2, tb[1] - 1, tb[2] + 2, tb[3] + 1), fill=(255, 255, 255, 225), outline=c + (255,))
        dr.text((x, y), lab, fill=tuple(int(v * 0.6) for v in c) + (255,), font=f, anchor="mm")
    Image.alpha_composite(im, ov).convert("RGB").save(outdir / "img" / "storey.jpg", quality=85)
    # per unit
    for u in units:
        ur = [r for r in rooms if r["unit"] == u]
        its = [(geo[r["number"]], col[u], r["number"].split(" ", 1)[-1] if r["number"].startswith(("ANWSIP6", "DSOT", "AMSC", "MSOT")) else r["number"],
                any(not e["approx"] for e in r["env"])) for r in ur if r["number"] in geo]
        if its:
            ctx = [(geo[r["number"]], (190, 190, 190), "", False) for r in rooms if r["unit"] and r["unit"] != u and r["number"] in geo]
            render(st, ctx + its, outdir / "img" / (re.sub(r"[^A-Za-z0-9]", "_", u) + ".jpg"), focus=[g for it in its for g in it[0]], dpi=100, margin=40)
    # HTML
    E = html.escape
    tot = lambda rs: (len(rs), sum(r["area"] for r in rs), sum(r["supply"] for r in rs), sum(e["glass"] for r in rs for e in r["env"]),
                      sum(e["wall"] for r in rs for e in r["env"]), sum(1 for r in rs if any(not e["approx"] for e in r["env"])))
    h = [f"<h1>{st} AHU zoning: rooms and facade</h1><p class=sub>Issue DRAFT_2026-11-24_R4. Room IDs from the DPA (D-093, D-094). Area used, final supply and envelope rows from "
         "the issued workbooks. Facade: glazing and wall area of the rooms on the facade line; 'approximate' rows are shares of the zone facade (D-090). Calculated and tender "
         "airflow from the AHU master summary (all storeys of the unit).</p>"]
    h.append("<h2>Summary by unit</h2><table><tr><th>Unit</th><th>Workbook</th><th>Rooms</th><th>Area m²</th><th>Supply L/s</th><th>Rooms on facade</th><th>Glazing m²</th>"
             "<th>Wall m²</th><th>Calculated L/s</th><th>Tender L/s</th></tr>")
    for u in units:
        ur = [r for r in rooms if r["unit"] == u]
        n, A, S, G, W, F = tot(ur)
        m = master.get(u, {})
        fmt = lambda v: f"{v:,.0f}" if isinstance(v, (int, float)) else "–"
        h.append(f"<tr><td><span class=sw style='background:rgb{col[u]}'></span>{E(u)}</td><td>{', '.join(sorted({r['workbook'] for r in ur}))}</td><td>{n}</td><td>{A:,.1f}</td>"
                 f"<td>{S:,.0f}</td><td>{F}</td><td>{G:,.1f}</td><td>{W:,.1f}</td><td>{fmt(m.get('calc'))}</td><td>{fmt(m.get('tender'))}</td></tr>")
    nu = [r for r in rooms if not r["unit"]]
    n, A, S, G, W, F = tot([r for r in rooms if r["unit"]])
    h.append(f"<tr class=tot><td>All units</td><td></td><td>{n}</td><td>{A:,.1f}</td><td>{S:,.0f}</td><td>{F}</td><td>{G:,.1f}</td><td>{W:,.1f}</td><td></td><td></td></tr>")
    n2, A2, S2, G2, W2, F2 = tot(nu)
    h.append(f"<tr class=tot><td>Rooms with no unit</td><td></td><td>{n2}</td><td>{A2:,.1f}</td><td>{S2:,.0f}</td><td>{F2}</td><td>{G2:,.1f}</td><td>{W2:,.1f}</td><td></td><td></td></tr></table>")
    h.append("<div class=pb></div><h2>Storey plan: rooms by unit</h2><p class=sub>Each colour is one unit. Orange edge: room with its own facade rows. "
             "Rooms with no outline (no App D outline and no single plan room) are not drawn; they are in the tables.</p><img class=full src='img/storey.jpg'>")
    for u in units:
        ur = sorted([r for r in rooms if r["unit"] == u], key=lambda x: x["number"])
        n, A, S, G, W, F = tot(ur)
        m = master.get(u, {})
        h.append(f"<div class=pb></div><h2><span class=sw style='background:rgb{col[u]}'></span>{E(u)}</h2>")
        h.append(f"<p class=sub>{n} rooms, {A:,.1f} m², room supply {S:,.0f} L/s; facade glazing {G:,.1f} m², wall {W:,.1f} m². "
                 f"Calculated airflow {('{:,.0f}'.format(m['calc'])) if isinstance(m.get('calc'), (int, float)) else '–'} L/s, tender {('{:,.0f}'.format(m['tender'])) if isinstance(m.get('tender'), (int, float)) else '–'} L/s.</p>")
        imgp = "img/" + re.sub(r"[^A-Za-z0-9]", "_", u) + ".jpg"
        if (outdir / imgp).exists():
            h.append(f"<img class=crop src='{imgp}'>")
        h.append("<table><tr><th>DPA room ID</th><th>Name</th><th>Wb</th><th>Area m²</th><th>Supply L/s</th><th>Facade (glazing / wall m²)</th><th>Drawing space and match</th></tr>")
        for r in ur:
            fac = "; ".join(f"{e['o']} {e['glass']:g} / {e['wall']:g}" + (" (approx.)" if e["approx"] else "") for e in r["env"]) or "internal"
            drawn = "" if r["number"] in geo else " <i>(not drawn)</i>"
            h.append(f"<tr><td>{E(r['number'])}{drawn}</td><td>{E(r['name'])}</td><td>{r['workbook']}</td><td>{r['area']:,.1f}</td><td>{r['supply']:,.0f}</td><td>{E(fac)}</td><td>{E(basis(r['number']))}</td></tr>")
        h.append("</table>")
    h.append("<div class=pb></div><h2>Rooms on the storey with no unit</h2><p class=sub>Rooms of the brief on this storey that no document links to a unit. "
             "They are calculated in their workbook but not counted on any AHU.</p><table><tr><th>DPA room ID</th><th>Name</th><th>Wb</th><th>Area m²</th></tr>")
    for r in sorted(nu, key=lambda x: (x["workbook"], x["number"])):
        h.append(f"<tr><td>{E(r['number'])}</td><td>{E(r['name'])}</td><td>{r['workbook']}</td><td>{r['area']:,.1f}</td></tr>")
    h.append("</table>")
    sp = [x for x in csv.DictReader(open("out/qa/spaces_without_brief_room.csv", encoding="utf-8")) if x["storey"] == st]
    h.append("<h2>Drawing spaces with no brief room</h2><table><tr><th>Space</th><th>Tag</th><th>Unit (zoning)</th><th>Area m²</th><th>Facade m²</th></tr>")
    for x in sp:
        h.append(f"<tr><td>{E(x['space'])}</td><td>{E(x['name'])}</td><td>{E(x['unit'])}</td><td>{x['area_m2']}</td><td>{x['envelope_m2']}</td></tr>")
    h.append("</table>")
    css = """@page{size:A4 landscape;margin:12mm}body{font:9pt 'DejaVu Sans',sans-serif;color:#111}h1{font-size:16pt;margin:0 0 4px}h2{font-size:12pt;margin:6px 0}
    .sub{color:#444;margin:2px 0 6px}table{border-collapse:collapse;width:100%;margin-bottom:8px}th,td{border:1px solid #bbb;padding:2px 4px;text-align:left;vertical-align:top}
    th{background:#eee}td:nth-child(n+4){white-space:nowrap}.tot td{font-weight:bold;background:#f6f6f6}.sw{display:inline-block;width:10px;height:10px;margin-right:5px;border:1px solid #555}
    .pb{page-break-after:always}img.full{width:100%;max-height:175mm;object-fit:contain}img.crop{max-width:100%;max-height:105mm;display:block;margin:2px 0 6px}"""
    (outdir / f"{st}_AHU_report.html").write_text(f"<!doctype html><html><head><meta charset=utf-8><title>{st} AHU zoning</title><style>{css}</style></head><body>{''.join(h)}</body></html>", encoding="utf-8")
    import subprocess
    js = ("const {chromium}=require('playwright');(async()=>{const b=await chromium.launch();const p=await b.newPage();"
          f"await p.goto({json.dumps((outdir / f'{st}_AHU_report.html').resolve().as_uri())},{{waitUntil:'load'}});"
          f"await p.pdf({{path:{json.dumps(str((issue / f'AIGH_IP_{st}_AHU_rooms_facade.pdf').resolve()))},format:'A4',landscape:true,printBackground:true,"
          "margin:{top:'12mm',bottom:'12mm',left:'10mm',right:'10mm'}});await b.close();})();")
    subprocess.run(["node", "-e", js], check=True)
    import shutil
    shutil.copy(outdir / f"{st}_AHU_rooms_facade.csv", issue / f"AIGH_IP_{st}_AHU_rooms_facade.csv")
    print("units", len(units), "rooms", len(rooms), "drawn", sum(1 for r in rooms if r["number"] in geo))


if __name__ == "__main__":
    main()
