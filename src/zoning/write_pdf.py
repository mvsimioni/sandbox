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
    # /CA is always written (also 1 for the opaque legend box and text) so that no annotation relies on a default opacity
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
    doc.xref_set_key(annot.xref, "CA", "%.2f" % opacity)
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
        "Area: %.1f m2 (sum of %d assigned rooms, covering share at least the configured majority); zone polygon drawn %.1f m2" % (area_m2, n_rooms, polygon_area_m2),
        "Room count: %d" % n_rooms,
        "Transfer to this sheet: grid-calibrated, residual %.1f mm" % z.transfer_residual_mm if z.source.startswith("RFI") else "Geometry: App D polygon transferred through the grid calibration, residual %.1f mm" % z.transfer_residual_mm,
        ("Not drawn here: %.1f m2 of the App D polygons lie inside RFI-ME-0233 zones or inside an App D polygon drawn before (the later document governs)" % (z.overlap_removed_m2) if getattr(z, "overlap_removed_m2", 0.0) > 0 else ""),
        "Tag basis: %s%s" % (z.tag_basis, ("; sample wrote %s" % z.sample_tag) if z.sample_tag and z.sample_tag != z.tag else ""),
        "Sources: %s" % " | ".join(z.refs),
    ]
    return "\n".join(l for l in lines if l)


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
        for k, poly in enumerate(z.drawn()):
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
        label = "%s | %s | zone %.0f m2, rooms %.0f m2 (%d)" % (z.tag, z.departments if len(z.departments) < 40 else z.departments[:37] + "...", st.get("poly_m2", 0.0), st["area_m2"], st.get("n_rooms", 0))
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
        doc.xref_set_key(a.xref, "CA", "1.00")
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
    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    for r in zones:
        if r["type"] != "Polygon" or not r["subject"] or r["fill"] in ("", "null") or num(r["ca"]) != FILL_OPACITY or num(r["fill_opacity"]) != FILL_OPACITY or not r["oc"]:
            problems.append("zone annotation incomplete: %s" % r)
    for r in legends:
        if not r["subject"] or not r["oc"]:
            problems.append("legend annotation incomplete: %s" % r)
        if r["name"].startswith("legend-swatch"):
            if r["fill"] in ("", "null") or num(r["fill_opacity"]) != FILL_OPACITY or num(r["ca"]) != FILL_OPACITY:
                problems.append("legend swatch incomplete: %s" % r)
        elif num(r["ca"]) is None:
            problems.append("legend object without /CA (opacity must be explicit): %s" % r)
        elif r["name"] == "legend-back" and num(r["fill_opacity"]) is None:
            problems.append("legend backing without /FillOpacity: %s" % r)
    for r in rooms:
        if not r["subject"] or not r["oc"] or num(r["ca"]) is None:
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


# --------------------------------------------------------------------------- #
# Multi-page writer (all storeys in one PDF)
# --------------------------------------------------------------------------- #
INDEX_PAGE_SIZE = (842.0, 1191.0)    # A3 portrait, pt


def _zone_contents_multi(z: zn.Zone, st: dict, key: str, label: str) -> str:
    """/Contents of a zone annotation on a storey page: departments, storey served, plant level, area, room count, sources."""
    lines = [
        "AHU zone %s" % z.tag if z.kind == "AHU" else "%s zone (rooms classed %s, no AHU named)" % (z.kind, z.tag),
        "Departments: %s" % z.departments,
        "Storey served: %s (%s)" % (key, label),
        "Plant level: %s" % (z.plant_level or "not stated"),
        "Area: %.1f m2 (sum of %d assigned rooms); zone polygons drawn %.1f m2 (page-view measure)" % (st["area_m2"], st["n_rooms"], st["poly_m2"]),
        "Room count: %d" % st["n_rooms"],
        "Lowest room confidence: %.2f" % st.get("min_conf", 0.0),
        "Sources: %s" % " | ".join(z.refs),
    ]
    return "\n".join(lines)


def _add_poly_annot(page, doc, poly: Polygon, name: str, subject: str, contents: str, oc: int, fill_hex: str) -> int:
    pts = list(poly.exterior.coords)[:-1]
    annot = page.add_polygon_annot(_unrot_pts(pts, page))
    _style_annot(doc, annot, name, subject, contents, oc, fill_hex, _darker(fill_hex), FILL_OPACITY, FILL_OPACITY)
    return annot.xref


def write_multi_zoning_pdf(src_pdf: Path, specs: Sequence[dict], out_pdf: Path, title: str,
                           legend_cols: int = 3, col_w: float = 640.0) -> dict:
    """Write one PDF: an index page, then one page per storey, each a copy of its source page (content stream untouched).

    ``specs``: per storey ``key``, ``label``, ``drawing``, ``page_index`` (0-based page of ``src_pdf``), ``zones`` (``zones.Zone``; ``draw_polygons_pv`` are
    drawn, ``tag`` is the annotation subject, ``key`` names the layer's AHU), ``zone_stats`` (key -> area_m2, poly_m2, n_rooms), ``rooms`` (dicts with
    ``room_id``, ``polygon``, ``contents``), ``legend_title``, ``sheet_note``.  Optional-content groups are document-wide: one per AHU (named after its
    tag, the same on every page), ``Legend`` and ``Rooms (measured)`` (off by default).  An AHU has one colour on every page (``Zone.fill_hex``).
    """
    import pymupdf

    src = pymupdf.open(str(src_pdf))
    doc = pymupdf.open()
    page_map = []        # (output page index, source page index, key)
    for sp in specs:
        doc.insert_pdf(src, from_page=sp["page_index"], to_page=sp["page_index"])
    # insert_pdf copies page content; check by the verifier (stream and pixels) rather than assume
    ocgs: Dict[str, int] = {}
    layers: List[dict] = []

    def ocg_for(name: str, on: bool = True) -> int:
        if name not in ocgs:
            ocgs[name] = doc.add_ocg(name, on=on)
            layers.append({"name": name, "on": on, "xref": ocgs[name]})
        return ocgs[name]

    legend_oc = ocg_for(LEGEND_LAYER)
    rooms_oc = ocg_for(ROOMS_LAYER, on=False)
    expected_tags: Dict[int, List[str]] = {}
    manifest_pages = []
    index_rows: List[Tuple[str, str, str, int, float]] = []
    colours: Dict[str, str] = {}
    for n, sp in enumerate(specs):
        page = doc[n]
        page_map.append((n + 1, sp["page_index"], sp["key"]))          # +1: the index page is inserted first below
        zones: Sequence[zn.Zone] = sp["zones"]
        stats = sp["zone_stats"]
        pm = {"key": sp["key"], "zones": 0, "rooms": 0, "legend_box_pv": None}
        for z in zones:
            if colours.setdefault(z.tag, z.fill_hex) != z.fill_hex:
                raise ValueError("AHU %s has two colours across pages" % z.tag)
            st = stats.get(z.key, {"area_m2": 0.0, "poly_m2": 0.0, "n_rooms": 0})
            oc = ocg_for(z.tag)
            for k, poly in enumerate(z.drawn()):
                _add_poly_annot(page, doc, poly, "zone-%s-%s-%d" % (sp["key"], z.key, k), z.tag, _zone_contents_multi(z, st, sp["key"], sp["label"]), oc, z.fill_hex)
                pm["zones"] += 1
            index_rows.append((sp["key"], z.tag, z.kind, st["n_rooms"], st["area_m2"]))
        expected_tags[n + 1] = [z.tag for z in zones]
        # legend
        entries = list(zones)
        rows = math.ceil(len(entries) / legend_cols) if entries else 0
        ncol = min(legend_cols, max(1, len(entries)))
        row_h, pad = 24.0, 14.0
        box_w, box_h = ncol * col_w + 2 * pad, rows * row_h + 2 * pad + 36 + 30
        W, H = page.rect.width, page.rect.height
        found = find_blank_box(page, (box_w, box_h), (700.0, 110.0, max(W - 300, 1500.0), min(H * 0.5, 900.0)))
        if found is None:        # page too small for the search window: top-left corner, clipped to the page
            found = ((20.0, 20.0, min(20.0 + box_w, W - 20.0), min(20.0 + box_h, H - 20.0)), float("nan"))
        (bx0, by0, bx1, by1), ink = found
        pm["legend_box_pv"] = [bx0, by0, bx1, by1]
        pm["legend_box_ink_fraction"] = ink
        back = page.add_rect_annot(_unrot_rect((bx0, by0, bx1, by1), page))
        _style_annot(doc, back, "legend-back-%s" % sp["key"], "Legend", "%s\n%s" % (sp["legend_title"], sp["sheet_note"]), legend_oc, "#FFFFFF", (0.3, 0.3, 0.3), 1.0, 1.0)
        tt = page.add_freetext_annot(_unrot_rect((bx0 + pad, by0 + pad / 2, bx1 - pad, by0 + pad / 2 + 26), page), sp["legend_title"][:150],
                                     fontsize=14, fontname="hebo", text_color=(0, 0, 0), rotate=90)
        _style_annot(doc, tt, "legend-title-%s" % sp["key"], "Legend title", sp["legend_title"], legend_oc, None, (0, 0, 0), 1.0, None)
        un = sp.get("unclassified", {})
        note = "%d of %d rooms not classified (no information, %.0f m2): not filled. Sheet %s." % (un.get("unclassified_rooms", 0), un.get("n_rooms", 0), un.get("unclassified_area_m2", 0.0), sp["drawing"])
        nt = page.add_freetext_annot(_unrot_rect((bx0 + pad, by0 + pad / 2 + 28, bx1 - pad, by0 + pad / 2 + 52), page), note, fontsize=11, fontname="helv",
                                     text_color=(0, 0, 0), rotate=90)
        _style_annot(doc, nt, "legend-note-%s" % sp["key"], "Legend note", note, legend_oc, None, (0, 0, 0), 1.0, None)
        for i, z in enumerate(entries):
            col, row = divmod(i, rows)
            x = bx0 + pad + col * col_w
            y = by0 + 66 + pad + row * row_h
            st = stats.get(z.key, {"area_m2": 0.0, "poly_m2": 0.0, "n_rooms": 0})
            sw = page.add_polygon_annot(_unrot_pts([(x, y + 2), (x + 30, y + 2), (x + 30, y + row_h - 4), (x, y + row_h - 4)], page))
            _style_annot(doc, sw, "legend-swatch-%s-%d" % (sp["key"], i), z.tag, "Legend swatch for %s" % z.tag, legend_oc, z.fill_hex, _darker(z.fill_hex), FILL_OPACITY, FILL_OPACITY)
            label = "%s | %s | %.0f m2 (%d rooms)" % (z.tag, z.departments if len(z.departments) < 34 else z.departments[:31] + "...", st["area_m2"], st["n_rooms"])
            ft = page.add_freetext_annot(_unrot_rect((x + 36, y, x + col_w - 6, y + row_h), page), label, fontsize=10.5, fontname="helv", text_color=(0, 0, 0), rotate=90)
            _style_annot(doc, ft, "legend-text-%s-%d" % (sp["key"], i), z.tag, label, legend_oc, None, (0, 0, 0), 1.0, None)
        # room outlines
        for r in sp["rooms"]:
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
            doc.xref_set_key(a.xref, "CA", "1.00")
            doc.xref_set_key(a.xref, "NM", "(%s)" % ("room-" + r["room_id"]))
            pm["rooms"] += 1
        manifest_pages.append(pm)
    # ---- index pages (first): AHUs by storey ------------------------------------------------------------
    def index_lines(first_pdf_page: Dict[str, int]):
        L = [(title, 16, "hebo", 0, 20), ("Index of zones by storey. Every zone is a filled Polygon annotation (opacity 0.35) on a layer named after its AHU or class.", 8, "helv", 0, 14),
             ("Storey pages are the source sheets unchanged. Room outlines: layer 'Rooms (measured)', off by default.", 8, "helv", 0, 22)]
        for sp in specs:
            rows = [r for r in index_rows if r[0] == sp["key"]]
            un = sp.get("unclassified", {})
            L.append(("%s  %s  (%s)  PDF page %d" % (sp["key"], sp["label"], sp["drawing"], first_pdf_page[sp["key"]]), 10, "hebo", 0, 13))
            if not rows:
                L.append(("no zones (no room classified)", 8, "helv", 16, 12))
            for _, tag, kind, nr, area in rows:
                L.append(("%-46s %-6s rooms %4d   %9.1f m2" % (tag, kind, nr, area), 8, "cour", 16, 12))
            L.append(("no information (not filled): %d rooms, %.1f m2" % (un.get("unclassified_rooms", 0), un.get("unclassified_area_m2", 0.0)), 8, "helv", 16, 20))
        return L

    def paginate(L):
        pages, cur, y = [], [], 50.0
        for ln in L:
            if y + ln[4] > INDEX_PAGE_SIZE[1] - 40:
                pages.append(cur); cur, y = [], 50.0
            cur.append((y, ln)); y += ln[4]
        pages.append(cur)
        return pages

    n_index = len(paginate(index_lines({sp["key"]: 1 for sp in specs})))
    pages_idx = paginate(index_lines({sp["key"]: n_index + i + 1 for i, sp in enumerate(specs)}))
    for k, content in enumerate(pages_idx):
        ip = doc.new_page(pno=k, width=INDEX_PAGE_SIZE[0], height=INDEX_PAGE_SIZE[1])
        for y, (text, size, font, indent, _) in content:
            ip.insert_text((40 + indent, y), text, fontsize=size, fontname=font)
    page_map = [(o + (n_index - 1), s, k) for (o, s, k) in page_map]
    out_pdf = Path(out_pdf)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_pdf), garbage=0, deflate=True)
    doc.close()
    return {"pages": manifest_pages, "layers": layers, "page_map": page_map, "colours": colours,
            "expected_layers": [l["name"] for l in layers],
            "expected_tags": {str(o + (n_index - 1)): v for o, v in expected_tags.items()}, "n_index_pages": n_index}


def verify_multi_zoning_pdf(out_pdf: Path, src_pdf: Path, page_map: Sequence[Tuple[int, int, str]], expect_layers: Sequence[str],
                            expect_tags: Dict[str, Sequence[str]]) -> dict:
    """The single-page contract of :func:`verify_zoning_pdf` for every storey page.

    Per page: content stream equal to the source page's; render with annotations hidden pixel-identical; rotation kept; every zone a Polygon annotation with
    /Subj, fill (/IC), opacity 0.35 in /CA and /FillOpacity and an /OC naming the layer of its subject; legend annotations on the Legend layer with a swatch
    and text for every zone tag; room outlines on the off-by-default ``Rooms (measured)`` layer.  Across pages: one fill colour per AHU tag.
    """
    import pymupdf

    src = pymupdf.open(str(src_pdf))
    out = pymupdf.open(str(out_pdf))
    res: dict = {"n_pages": len(out), "pages": [], "problems": []}
    ocg_names = {xref: v["name"] for xref, v in out.get_ocgs().items()}
    res["layers"] = sorted(ocg_names.values())
    off = set((out.get_layer() or {}).get("off", []) or [])
    res["layers_off_by_default"] = sorted(ocg_names[x] for x in off if x in ocg_names)
    colours: Dict[str, set] = {}

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    all_ok = True
    for (oi, si, key) in page_map:
        sp, op = src[si], out[oi]
        pr: dict = {"storey": key, "out_page": oi + 1, "source_page": si + 1}
        pr["content_stream_identical"] = sp.read_contents() == op.read_contents()
        pr["rotation_kept"] = sp.rotation == op.rotation
        a = sp.get_pixmap(matrix=pymupdf.Matrix(1, 1), annots=False)
        b = op.get_pixmap(matrix=pymupdf.Matrix(1, 1), annots=False)
        pr["pixels_identical_annots_hidden"] = (a.width == b.width and a.height == b.height and a.samples == b.samples)
        zones, legends, rooms, problems = [], [], [], []
        for an in op.annots():
            xref = an.xref
            nm = out.xref_get_key(xref, "NM")[1]
            oc_ref = out.xref_get_key(xref, "OC")[1]
            oc = int(oc_ref.split()[0]) if oc_ref and oc_ref != "null" else 0
            ic = out.xref_get_key(xref, "IC")[1]
            rec = {"xref": xref, "type": an.type[1], "name": nm, "subject": an.info.get("subject", ""), "oc": ocg_names.get(oc, ""), "fill": ic,
                   "ca": out.xref_get_key(xref, "CA")[1], "fill_opacity": out.xref_get_key(xref, "FillOpacity")[1]}
            if nm.startswith("zone-"):
                zones.append(rec)
                colours.setdefault(rec["subject"], set()).add(ic)
            elif nm.startswith("legend-"):
                legends.append(rec)
            elif nm.startswith("room-"):
                rooms.append(rec)
            else:
                problems.append("p%d unexpected annotation %s (%s)" % (oi + 1, xref, nm))
        for r in zones:
            if r["type"] != "Polygon" or not r["subject"] or r["fill"] in ("", "null") or num(r["ca"]) != FILL_OPACITY or num(r["fill_opacity"]) != FILL_OPACITY or not r["oc"] or r["oc"] != r["subject"]:
                problems.append("p%d zone annotation incomplete: %s" % (oi + 1, r))
        for r in legends:
            if not r["subject"] or r["oc"] != LEGEND_LAYER:
                problems.append("p%d legend annotation incomplete or off the Legend layer: %s" % (oi + 1, r))
            if r["name"].startswith("legend-swatch") and (r["fill"] in ("", "null") or num(r["fill_opacity"]) != FILL_OPACITY or num(r["ca"]) != FILL_OPACITY):
                problems.append("p%d legend swatch incomplete: %s" % (oi + 1, r))
            elif num(r["ca"]) is None:
                problems.append("p%d legend object without /CA: %s" % (oi + 1, r))
            elif r["name"].startswith("legend-back") and num(r["fill_opacity"]) is None:
                problems.append("p%d legend backing without /FillOpacity: %s" % (oi + 1, r))
        for r in rooms:
            if not r["subject"] or r["oc"] != ROOMS_LAYER or num(r["ca"]) is None:
                problems.append("p%d room annotation incomplete: %s" % (oi + 1, r))
        zt = {r["subject"] for r in zones}
        lt = {r["subject"] for r in legends if r["name"].startswith("legend-text")}
        ls = {r["subject"] for r in legends if r["name"].startswith("legend-swatch")}
        pr["n_zone_annotations"], pr["n_legend_annotations"], pr["n_room_annotations"] = len(zones), len(legends), len(rooms)
        pr["zone_tags_missing_in_legend"] = sorted(zt - lt)
        pr["legend_tags_without_zone"] = sorted((lt | ls) - zt)
        pr["expected_tags_missing_in_zones"] = sorted(set(expect_tags.get(str(oi), [])) - zt)
        pr["problems"] = problems
        pr["ok"] = bool(pr["content_stream_identical"] and pr["pixels_identical_annots_hidden"] and pr["rotation_kept"] and not problems and not pr["zone_tags_missing_in_legend"]
                        and not pr["legend_tags_without_zone"] and not pr["expected_tags_missing_in_zones"])
        all_ok &= pr["ok"]
        res["pages"].append(pr)
    res["colour_conflicts"] = {t: sorted(v) for t, v in colours.items() if len(v) > 1}
    res["layers_expected_present"] = all(l in res["layers"] for l in expect_layers)
    res["rooms_layer_off"] = ROOMS_LAYER in res["layers_off_by_default"]
    res["legend_layer_on"] = LEGEND_LAYER not in res["layers_off_by_default"]
    res["problems"] = [p for pr in res["pages"] for p in pr["problems"]]
    res["ok"] = bool(all_ok and not res["colour_conflicts"] and res["layers_expected_present"] and res["rooms_layer_off"] and res["legend_layer_on"])
    return res
