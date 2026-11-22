"""PNG crop of the background plan around a room or a group of rooms, with the room polygons outlined and labelled.

``python3 -m src.zoning.crops --storey L04 --rooms L04-R003,L04-R004 --out /path/crop.png [--dpi 170] [--margin-mm 4000]``

The background page is the storey sheet of ``Arch Combined Tender .pdf`` (page from ``out/registers/sheet_index.csv``).  The page is
read as it is displayed (page view, as in ``polygon_pv_wkt``), so the polygons need no further transform.  Room ids can be written in
full (``L04-R003``) or as the number only (``3``).  The page itself is not changed.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

BG_PDF = "data/source/Basis/Arch Combined Tender .pdf"
REG = Path("out/registers")
SCALE_MM_PER_PT = 88.1944          # 1:250 sheet, 1 pt = 25.4/72 mm on paper
PALETTE = [(220, 20, 60), (0, 112, 192), (0, 150, 80), (230, 120, 0), (130, 50, 170), (0, 150, 170)]   # no pure red


def read_csv(path) -> List[dict]:
    with Path(path).open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def storey_page(key: str) -> int:
    for r in read_csv(REG / "sheet_index.csv"):
        if r["storey_key"] == key:
            return int(r["page"])
    raise KeyError(f"storey {key} is not in sheet_index.csv")


def parse_wkt_rings(wkt: str) -> List[List[Tuple[float, float]]]:
    """Rings of a POLYGON or MULTIPOLYGON WKT as lists of (x, y); the first ring of each polygon is the outer ring."""
    rings = []
    for body in re.findall(r"\(([^()]+)\)", wkt):
        pts = []
        for pair in body.split(","):
            xy = pair.split()
            if len(xy) >= 2:
                pts.append((float(xy[0]), float(xy[1])))
        if len(pts) >= 3:
            rings.append(pts)
    return rings


def pick_rooms(key: str, ids: Sequence[str]) -> List[dict]:
    rooms = read_csv(REG / f"room_polygons_{key}.csv")
    by_id = {r["room_id"]: r for r in rooms}
    out = []
    for i in ids:
        i = i.strip()
        if not i:
            continue
        rid = i if i in by_id else f"{key}-R{int(i):03d}" if i.isdigit() else i
        if rid not in by_id:
            raise KeyError(f"room {i} is not in room_polygons_{key}.csv")
        out.append(by_id[rid])
    if not out:
        raise ValueError("no room given")
    return out


def render(key: str, ids: Sequence[str], out: str, dpi: int = 170, margin_mm: float = 4000.0) -> dict:
    import pymupdf
    from PIL import Image, ImageDraw, ImageFont

    rooms = pick_rooms(key, ids)
    all_pts = []
    for r in rooms:
        for ring in parse_wkt_rings(r["polygon_pv_wkt"]):
            all_pts += ring
    if not all_pts:
        raise ValueError("the rooms have no polygon")
    m = margin_mm / SCALE_MM_PER_PT
    doc = pymupdf.open(BG_PDF)
    page = doc[storey_page(key) - 1]
    clip = pymupdf.Rect(min(p[0] for p in all_pts) - m, min(p[1] for p in all_pts) - m,
                        max(p[0] for p in all_pts) + m, max(p[1] for p in all_pts) + m) & page.rect
    pix = page.get_pixmap(dpi=dpi, clip=clip, alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("RGBA")
    k = dpi / 72.0
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", max(14, int(dpi / 8)))
    except OSError:
        font = ImageFont.load_default()
    for i, r in enumerate(rooms):
        colour = PALETTE[i % len(PALETTE)]
        rings = parse_wkt_rings(r["polygon_pv_wkt"])
        for ring in rings:
            px = [((x - clip.x0) * k, (y - clip.y0) * k) for x, y in ring]
            d.polygon(px, fill=colour + (45,))
            d.line(px + [px[0]], fill=colour + (255,), width=max(3, int(dpi / 50)))
        if rings:
            xs = [p[0] for p in rings[0]]
            ys = [p[1] for p in rings[0]]
            cx, cy = (sum(xs) / len(xs) - clip.x0) * k, (sum(ys) / len(ys) - clip.y0) * k
            label = f"{r['room_id'].split('-')[-1]} {r['room_name']}".strip()
            tb = d.textbbox((cx, cy), label, font=font)
            d.rectangle((tb[0] - 3, tb[1] - 2, tb[2] + 3, tb[3] + 2), fill=(255, 255, 255, 220))
            d.text((cx, cy), label, fill=colour + (255,), font=font)
    img = Image.alpha_composite(img, overlay).convert("RGB")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    img.save(out, dpi=(dpi, dpi))
    doc.close()
    return {"out": out, "width_px": img.width, "height_px": img.height, "rooms": [r["room_id"] for r in rooms], "page": storey_page(key)}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Crop of the background plan with room polygons")
    ap.add_argument("--storey", required=True)
    ap.add_argument("--rooms", required=True, help="room ids, comma separated")
    ap.add_argument("--out", required=True)
    ap.add_argument("--dpi", type=int, default=170)
    ap.add_argument("--margin-mm", type=float, default=4000.0)
    a = ap.parse_args(argv)
    if not 150 <= a.dpi <= 200:
        ap.error("--dpi must be between 150 and 200")
    print(render(a.storey, a.rooms.split(","), a.out, a.dpi, a.margin_mm))
    return 0


if __name__ == "__main__":
    sys.exit(main())
