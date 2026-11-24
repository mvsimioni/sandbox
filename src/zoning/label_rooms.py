"""Label every room on the storey sheets of the per-storey zoning PDFs with its DPA room ID and room name (D-093, D-094).

The labels are on their own layer 'Room IDs (DPA)' (on). A room is placed at its outline: its App D outline, else the one plan room it is linked to.
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
    placed, unplaced = 0, []
    for num, info in sorted(rooms.items()):
        gs = geo.get(num)
        if not gs:
            unplaced.append(num)
            continue
        g = max(gs, key=lambda q: q.area)
        p = g.representative_point()
        side = max(g.area, 1.0) ** 0.5
        fs = max(1.6, min(4.5, side / 9.0))
        l1, l2 = num, (info["name"][:34] + ("…" if len(info["name"]) > 34 else ""))
        w = max(pymupdf.get_text_length(l1, fontname="helv", fontsize=fs), pymupdf.get_text_length(l2, fontname="helv", fontsize=fs * 0.85))
        h = fs * 2.1
        # box in page view, then to the unrotated page
        x0, y0 = p.x - w / 2 - 0.6, p.y - h / 2 - 0.4
        corners = _unrot_pts([(x0, y0), (x0 + w + 1.2, y0 + h + 0.8)], page)
        r = pymupdf.Rect(corners[0], corners[1])
        r.normalize()
        page.draw_rect(r, color=None, fill=(1, 1, 1), fill_opacity=0.78, oc=oc, overlay=True)
        a = _unrot_pts([(p.x - w / 2, p.y - h / 2 + fs * 0.95)], page)[0]
        b = _unrot_pts([(p.x - w / 2, p.y - h / 2 + fs * 1.95)], page)[0]
        page.insert_text(a, l1, fontname="hebo", fontsize=fs, color=(0.05, 0.15, 0.45), rotate=page.rotation, oc=oc, overlay=True)
        page.insert_text(b, l2, fontname="helv", fontsize=fs * 0.85, color=(0.15, 0.15, 0.15), rotate=page.rotation, oc=oc, overlay=True)
        placed += 1
    note = (f"Room IDs (DPA): {placed} of {len(rooms)} rooms of the workbooks on {storey} are placed at their outline (App D outline or linked plan room). "
            f"{len(unplaced)} rooms have no outline on this plan and are not placed: see the room lists of the workbooks.")
    a = _unrot_pts([(60, 40)], page)[0]
    page.insert_text(a, note, fontname="helv", fontsize=14, color=(0.05, 0.15, 0.45), rotate=page.rotation, oc=oc, overlay=True)
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
