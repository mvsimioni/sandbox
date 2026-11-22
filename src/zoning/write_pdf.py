"""Write the pilot zoning PDF: the background page unchanged, every zone, legend entry and room outline an annotation.

* page content is the source page byte for byte (``Document.select`` keeps the page object and its content stream);
* each AHU zone is a filled Polygon annotation (/Subj = governing tag, /IC = fill, /CA and Bluebeam /FillOpacity = 0.35);
* every annotation carries /OC pointing at an optional-content group named after its AHU (App D zones: tag + " (App D)");
* the legend is annotations on its own layer ``Legend``: a backing square, then per zone a swatch (Polygon) and a FreeText line;
* the measured room outlines are Polygon annotations on ``Rooms (measured)``, set off by default;
* the page is /Rotate 90: annotation geometry is written in the unrotated frame (page view times the inverse rotation
  matrix) and FreeText gets /Rotate 90 so it reads upright in the rotated view.
"""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import shapely
from shapely.geometry import Polygon

from . import zones as zn

FILL_OPACITY = 0.35
AUTHOR = "AIGH IP ACMV pipeline (Claude)"
LEGEND_LAYER = "Legend"
ROOMS_LAYER = "Rooms (measured)"


def _hex(h: str) -> Tuple[float, float, float]:
    return zn.hex_to_rgb(h)


def _darker(h: str, f: float = 0.6) -> Tuple[float, float, float]:
    r, g, b = _hex(h)
    return (r * f, g * f, b * f)


def _unrot_pts(pts_pv: Sequence[Tuple[float, float]], page) -> List["pymupdf.Point"]:
    import pymupdf
    inv = ~page.rotation_matrix
    return [pymupdf.Point(float(x), float(y)) * inv for x, y in pts_pv]


def _unrot_rect(rect_pv: Tuple[float, float, float, float], page):
    import pymupdf
    inv = ~page.rotation_matrix
    r = pymupdf.Rect(*rect_pv) * inv
    r.normalize()
    return r


def _pdf_date() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("D:%Y%m%d%H%M%S+00'00'")


def _style_annot(doc, annot, name: str, subject: str, contents: str, ocg: int, fill_hex: Optional[str], stroke_rgb,
                 opacity: float, fill_opacity: Optional[float]) -> None:
    """Set colours, opacity, /Subj, /Contents, /T, /OC and the Bluebeam /FillOpacity on a new annotation."""
    is_text = annot.type[1] == "FreeText"
    if not is_text:
        annot.set_colors(stroke=stroke_rgb, fill=_hex(fill_hex) if fill_hex else None)
        annot.set_border(width=1.0)
    annot.set_opacity(opacity)
    annot.set_info(title=AUTHOR, subject=subject, content=contents)
    annot.set_flags(4)  # print
    annot.update()
    if ocg:
        annot.set_oc(ocg)
    if fill_opacity is not None:
        doc.xref_set_key(annot.xref, "FillOpacity", "%.2f" % fill_opacity)
    doc.xref_set_key(annot.xref, "NM", "(%s)" % name)
    doc.xref_set_key(annot.xref, "M", "(%s)" % _pdf_date())


def zone_contents(z: zn.Zone, area_m2: float, polygon_area_m2: float, n_rooms: int) -> str:
    """The /Contents text of a zone annotation: departments, storey served, plant level, area, room count, sources."""
    lines = [
        "AHU zone %s" % z.tag if z.kind == "AHU" else "%s zone %s" % (z.kind, z.tag),
        "Departments: %s" % z.departments,
        "Storey served: L%s (4th storey IP)" % (z.storey_served or "4"),
        "Plant level: %s" % ("L%s" % z.plant_level if z.plant_level else "not stated in tag"),
        "Area: %.1f m2 (sum of %d assigned rooms); zone polygon %.1f m2" % (area_m2, n_rooms, polygon_area_m2),
        "Room count: %d" % n_rooms,
        "Tag basis: %s%s" % (z.tag_basis, ("; sample wrote %s" % z.sample_tag) if z.sample_tag and z.sample_tag != z.tag else ""),
        "Sources: %s" % " | ".join(z.refs),
    ]
    return "\n".join(lines)


def layer_name(z: zn.Zone) -> str:
    return z.tag if z.source.startswith("RFI") else "%s (App D)" % z.tag


def find_blank_box(page, size_pt: Tuple[float, float], search: Tuple[float, float, float, float], step: float = 40.0) -> Tuple[Tuple[float, float, float, float], float]:
    """Find the emptiest ``size_pt`` box within ``search`` (page-view pt) by ink fraction of a 0.25x render."""
    import pymupdf
    sc = 0.25
    pix = page.get_pixmap(matrix=pymupdf.Matrix(sc, sc), colorspace=pymupdf.csGRAY, annots=False)
    a = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width)
    ink = (a < 200).astype(np.float32)
    ii = np.pad(ink, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    best = None
    w, h = size_pt
    x = search[0]
    while x + w <= search[2]:
        y = search[1]
        while y + h <= search[3]:
            x0, y0, x1, y1 = [int(round(v * sc)) for v in (x, y, x + w, y + h)]
            tot = ii[y1, x1] - ii[y0, x1] - ii[y1, x0] + ii[y0, x0]
            frac = float(tot) / max((x1 - x0) * (y1 - y0), 1)
            if best is None or frac < best[1]:
                best = ((x, y, x + w, y + h), frac)
            y += step
        x += step
    return best


def write_zoning_pdf(src_pdf: Path, page_index: int, out_pdf: Path, zones: Sequence[zn.Zone], zone_stats: Dict[str, dict],
                     rooms: Sequence[dict], legend_title: str, sheet_note: str,
                     legend_search: Tuple[float, float, float, float] = (700.0, 110.0, 3000.0, 600.0),
                     legend_cols: int = 3, col_w: float = 640.0) -> dict:
    """Write the annotated one-page PDF and return a manifest of what was written."""
    import pymupdf

    doc = pymupdf.open(str(src_pdf))
    doc.select([page_index])
    page = doc[0]
    manifest: dict = {"zones": [], "legend": [], "rooms": 0, "layers": []}
    ocgs: Dict[str, int] = {}

    def ocg_for(name: str, on: bool = True) -> int:
        if name not in ocgs:
            ocgs[name] = doc.add_ocg(name, on=on)
            manifest["layers"].append({"name": name, "on": on, "xref": ocgs[name]})
        return ocgs[name]

    # ---- zones -----------------------------------------------------------------------------------------
    for z in zones:
        st = zone_stats.get(z.key, {"area_m2": 0.0, "poly_m2": 0.0, "n_rooms": 0})
        oc = ocg_for(layer_name(z))
        for k, poly in enumerate(z.polygons_pv):
            pts = list(poly.exterior.coords)[:-1]
            annot = page.add_polygon_annot(_unrot_pts(pts, page))
            name = "zone-%s-%d" % (z.key, k)
            _style_annot(doc, annot, name, z.tag, zone_contents(z, st["area_m2"], st["poly_m2"], st["n_rooms"]), oc,
                         z.fill_hex, _darker(z.fill_hex), FILL_OPACITY, FILL_OPACITY)
            manifest["zones"].append({"xref": annot.xref, "subject": z.tag, "layer": layer_name(z), "fill": z.fill_hex, "part": k})

    # ---- legend ----------------------------------------------------------------------------------------
    legend_oc = ocg_for(LEGEND_LAYER)
    entries = list(zones)
    ncol = legend_cols
    rows = math.ceil(len(entries) / ncol) if entries else 0
    row_h, pad = 24.0, 14.0
    box_w, box_h = ncol * col_w + 2 * pad, rows * row_h + 2 * pad + 36
    (bx0, by0, bx1, by1), ink = find_blank_box(page, (box_w, box_h), legend_search)
    manifest["legend_box_pv"] = [bx0, by0, bx1, by1]
    manifest["legend_box_ink_fraction"] = ink
    back = page.add_rect_annot(_unrot_rect((bx0, by0, bx1, by1), page))
    _style_annot(doc, back, "legend-back", "Legend", "%s\n%s" % (legend_title, sheet_note), legend_oc, "#FFFFFF", (0.3, 0.3, 0.3), 1.0, 1.0)
    title = page.add_freetext_annot(_unrot_rect((bx0 + pad, by0 + pad / 2, bx1 - pad, by0 + pad / 2 + 26), page), legend_title,
                                    fontsize=16, fontname="hebo", text_color=(0, 0, 0), rotate=90)
    _style_annot(doc, title, "legend-title", "Legend title", legend_title, legend_oc, None, (0, 0, 0), 1.0, None)
    for i, z in enumerate(entries):
        col, row = divmod(i, rows)
        x = bx0 + pad + col * col_w
        y = by0 + 36 + pad + row * row_h
        st = zone_stats.get(z.key, {"area_m2": 0.0, "n_rooms": 0})
        sw = page.add_polygon_annot(_unrot_pts([(x, y + 2), (x + 30, y + 2), (x + 30, y + row_h - 4), (x, y + row_h - 4)], page))
        _style_annot(doc, sw, "legend-swatch-%d" % i, z.tag, "Legend swatch for %s" % z.tag, legend_oc, z.fill_hex, _darker(z.fill_hex), FILL_OPACITY, FILL_OPACITY)
        label = "%s | %s | %.0f m2" % (z.tag, z.departments if len(z.departments) < 44 else z.departments[:41] + "...", st["area_m2"])
        ft = page.add_freetext_annot(_unrot_rect((x + 36, y, x + col_w - 6, y + row_h), page), label, fontsize=10.5, fontname="helv",
                                     text_color=(0, 0, 0), rotate=90)
        _style_annot(doc, ft, "legend-text-%d" % i, z.tag, label, legend_oc, None, (0, 0, 0), 1.0, None)
        manifest["legend"].append({"tag": z.tag, "swatch_xref": sw.xref, "text_xref": ft.xref, "text": label})

    # ---- measured rooms (off by default) ----------------------------------------------------------------
    rooms_oc = ocg_for(ROOMS_LAYER, on=False)
    for r in rooms:
        poly: Polygon = r["polygon"]
        if poly.is_empty:
            continue
        pts = list(poly.exterior.coords)[:-1]
        if len(pts) < 3:
            continue
        a = page.add_polygon_annot(_unrot_pts(pts, page))
        a.set_colors(stroke=(0.0, 0.25, 0.7), fill=None)
        a.set_opacity(1.0)
        a.set_info(title=AUTHOR, subject="Room outline (measured)", content=r["contents"])
        a.set_flags(4)
        a.set_border(width=0.5)
        a.update()
        a.set_oc(rooms_oc)
        doc.xref_set_key(a.xref, "NM", "(%s)" % ("room-" + r["room_id"]))
        manifest["rooms"] += 1
    out_pdf = Path(out_pdf)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_pdf), garbage=0, deflate=True)
    doc.close()
    return manifest


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
def verify_zoning_pdf(out_pdf: Path, src_pdf: Path, page_index: int, expect_tags: Sequence[str], expect_layers: Sequence[str]) -> dict:
    """Check the written PDF against the contract; returns a dict of results (``ok`` is the conjunction).

    * page content stream equals the source page's, and a render with annotations hidden is pixel-identical;
    * every zone, swatch, legend text and room outline is an annotation with /Subj, and every zone and legend object
      has a fill (zones and swatches), opacity 0.35 in /CA and /FillOpacity, and an /OC naming an existing layer;
    * every zone tag has a legend entry and every legend entry a zone;
    * the Rooms (measured) layer is off in the default configuration.
    """
    import pymupdf

    res: dict = {}
    src = pymupdf.open(str(src_pdf))
    sp = src[page_index]
    out = pymupdf.open(str(out_pdf))
    op = out[0]
    res["content_stream_identical"] = sp.read_contents() == op.read_contents()
    res["n_pages"] = len(out)
    res["rotate"] = op.rotation
    res["rotate_source"] = sp.rotation
    m = pymupdf.Matrix(1, 1)
    a = sp.get_pixmap(matrix=m, annots=False)
    b = op.get_pixmap(matrix=m, annots=False)
    res["render_size"] = [a.width, a.height]
    res["pixels_identical_annots_hidden"] = (a.width == b.width and a.height == b.height and a.samples == b.samples)
    if not res["pixels_identical_annots_hidden"] and a.width == b.width and a.height == b.height:
        diff = np.frombuffer(a.samples, np.uint8) != np.frombuffer(b.samples, np.uint8)
        res["pixels_different"] = int(diff.sum())
    ocg_names = {xref: v["name"] for xref, v in out.get_ocgs().items()}
    res["layers"] = sorted(ocg_names.values())
    layer = out.get_layer()
    off = set()
    for item in (layer or {}).get("off", []) or []:
        off.add(item)
    res["layers_off_by_default"] = sorted(ocg_names[x] for x in off if x in ocg_names)
    zones, legends, rooms, problems = [], [], [], []
    for an in op.annots():
        xref = an.xref
        obj = out.xref_object(xref, compressed=False)
        nm = out.xref_get_key(xref, "NM")[1]
        subj = an.info.get("subject", "")
        oc_ref = out.xref_get_key(xref, "OC")[1]
        oc = int(oc_ref.split()[0]) if oc_ref and oc_ref != "null" else 0
        fo = out.xref_get_key(xref, "FillOpacity")[1]
        ca = out.xref_get_key(xref, "CA")[1]
        ic = out.xref_get_key(xref, "IC")[1]
        rec = {"xref": xref, "type": an.type[1], "name": nm, "subject": subj, "oc": ocg_names.get(oc, ""), "fill": ic, "ca": ca, "fill_opacity": fo}
        if nm.startswith("zone-"):
            zones.append(rec)
        elif nm.startswith("legend-"):
            legends.append(rec)
        elif nm.startswith("room-"):
            rooms.append(rec)
        else:
            problems.append("unexpected annotation %s (%s)" % (xref, nm))
    for r in zones:
        if r["type"] != "Polygon" or not r["subject"] or r["fill"] in ("", "null") or r["ca"] != "0.35" and r["ca"] != ".35" or r["fill_opacity"] not in ("0.35", ".35") or not r["oc"]:
            problems.append("zone annotation incomplete: %s" % r)
    for r in legends:
        if not r["subject"] or not r["oc"]:
            problems.append("legend annotation incomplete: %s" % r)
        if r["name"].startswith("legend-swatch") and (r["fill"] in ("", "null") or r["fill_opacity"] not in ("0.35", ".35")):
            problems.append("legend swatch incomplete: %s" % r)
    for r in rooms:
        if not r["subject"] or not r["oc"]:
            problems.append("room annotation incomplete: %s" % r)
    zone_tags = {r["subject"] for r in zones}
    legend_tags = {r["subject"] for r in legends if r["name"].startswith("legend-text")}
    res["n_zone_annotations"] = len(zones)
    res["n_legend_annotations"] = len(legends)
    res["n_room_annotations"] = len(rooms)
    res["zone_tags_missing_in_legend"] = sorted(zone_tags - legend_tags)
    res["legend_tags_without_zone"] = sorted(legend_tags - zone_tags)
    res["expected_tags_missing_in_zones"] = sorted(set(expect_tags) - zone_tags)
    res["zone_layers_named_after_tag"] = all((r["oc"] == r["subject"] or r["oc"] == r["subject"] + " (App D)") for r in zones)
    res["layers_expected_present"] = all(l in res["layers"] for l in expect_layers)
    res["problems"] = problems
    res["ok"] = bool(res["content_stream_identical"] and res["pixels_identical_annots_hidden"] and not problems
                     and not res["zone_tags_missing_in_legend"] and not res["legend_tags_without_zone"]
                     and res["zone_layers_named_after_tag"] and res["layers_expected_present"]
                     and ROOMS_LAYER in res["layers_off_by_default"] and res["rotate"] == res["rotate_source"])
    return res
