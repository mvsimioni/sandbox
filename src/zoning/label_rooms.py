"""Label every room on the storey sheets of the per-storey zoning PDFs with its DPA room ID and room name (D-093, D-094).

The labels are unflattened markups (polygon on the room outline, subject = DPA ID; free-text label) on their own layer 'Room IDs (DPA)' (on). A room is placed at its outline: its App D outline, else the one plan room it is linked to.
Rooms with no outline are listed in the PDF index text box of the sheet and are not placed.

``python3 -m src.zoning.label_rooms --dir out/issue/DRAFT_2026-11-24_R4/zoning``
"""

from __future__ import annotations

import argparse
import glob
import json
import re
from pathlib import Path


def rooms_of(storey: str):
    files = sorted(glob.glob("out/loads/inputs/D*.json")) + ["out/loads/inputs_common/D00.json"]
    out = {}
    for f in files:
        wb = "D" + re.search(r"D(\d+)\.json$", f).group(1)
        for r in json.load(open(f, encoding="utf-8"))["rooms"]:
            if r.get("storey") == storey:
                out[r["number"]] = dict(name=r.get("name") or "", unit=r.get("ahu") or "", wb=wb)
    return out


def label(pdf: Path, storey: str) -> dict:
    import pymupdf
    from src.reports.storey_ahu_report import load_geometry
    from src.zoning.write_pdf import _unrot_pts
    geo = load_geometry(storey)
    rooms = rooms_of(storey)
    doc = pymupdf.open(str(pdf))
    page = doc[doc.page_count - 1]                      # the storey sheet is the last page
    oc = doc.add_ocg("Room IDs (DPA)", on=True)
    from src.zoning.write_pdf import _style_annot, _unrot_rect
    placed, unplaced = 0, []
    for num, info in sorted(rooms.items()):
        gs = geo.get(num)
        if not gs:
            unplaced.append(num)
            continue
        contents = f"{num}\n{info['name']}\nUnit: {info['unit'] or 'none'}\nWorkbook: {info['wb']}"
        tagname = re.sub(r"[^A-Za-z0-9]", "", num)
        gs = [q for g0 in gs for q in getattr(g0, "geoms", [g0]) if q.geom_type == "Polygon"]
        if not gs:
            unplaced.append(num)
            continue
        for k, g in enumerate(gs):                     # room outline: an unflattened polygon markup, subject = DPA room ID
            pts = list(g.exterior.coords)[:-1]
            if len(pts) < 3:
                continue
            a = page.add_polygon_annot(_unrot_pts(pts, page))
            _style_annot(doc, a, f"room-{tagname}-{k}", num, contents, oc, None, (0.05, 0.15, 0.45), 1.0, None)
            a.set_border(width=0.4)
            a.update()
        g = max(gs, key=lambda q: q.area)
        p = g.representative_point()
        side = max(g.area, 1.0) ** 0.5
        fs = max(1.6, min(4.5, side / 9.0))
        name = info["name"][:34] + ("…" if len(info["name"]) > 34 else "")
        w = max(pymupdf.get_text_length(num, fontname="helv", fontsize=fs), pymupdf.get_text_length(name, fontname="helv", fontsize=fs)) + 2
        h = fs * 2.6
        ft = page.add_freetext_annot(_unrot_rect((p.x - w / 2, p.y - h / 2, p.x + w / 2, p.y + h / 2), page), f"{num}\n{name}", fontsize=fs, fontname="helv",
                                     text_color=(0.05, 0.15, 0.45), fill_color=(1, 1, 1), rotate=page.rotation, align=1)
        _style_annot(doc, ft, f"roomlabel-{tagname}", num, contents, oc, None, (0.05, 0.15, 0.45), 0.9, None)
        placed += 1
    note = (f"Room IDs (DPA): {placed} of {len(rooms)} rooms of the workbooks on {storey} are placed at their outline (App D outline or linked plan room). "
            f"{len(unplaced)} rooms have no outline on this plan and are not placed: see the room lists of the workbooks.")
    nt = page.add_freetext_annot(_unrot_rect((40, 20, 2400, 52), page), note, fontsize=14, fontname="helv", text_color=(0.05, 0.15, 0.45), rotate=page.rotation)
    _style_annot(doc, nt, f"roomids-note-{storey}", "Room IDs (DPA)", note, oc, None, (0.05, 0.15, 0.45), 1.0, None)
    tmp = pdf.with_suffix(".tmp.pdf")
    doc.save(str(tmp), garbage=4, deflate=True)
    doc.close()
    tmp.replace(pdf)
    return dict(storey=storey, rooms=len(rooms), placed=placed, unplaced=len(unplaced))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    a = ap.parse_args(argv)
    for f in sorted(Path(a.dir).glob("AIGH_IP_AHU_Zoning_*.pdf")):
        st = f.stem.rsplit("_", 1)[1]
        print(label(f, st))


if __name__ == "__main__":
    main()
