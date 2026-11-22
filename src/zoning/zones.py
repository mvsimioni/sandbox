"""AHU zones: transfer the RFI-ME-0233 sample polygons onto the background page, assign rooms, classify App D spaces.

Precedence (config ``precedence``): the later-dated zoning document governs where two overlap, so the RFI sample
(5 Nov 2026) governs over Appendix D (1 Aug 2025); every overlap is recorded in the schedule.  The governing AHU tags come
from ``tag_crossref_rfi_me_0233.csv`` and conflict CF-0003 (the sample's ``AHU-B-04-07DIAGRE-01A/B`` and
``AHU-B-04-07DIAGSU-01`` are written ``AHU-B-07-04DIAGRE-01A/B`` and ``AHU-B-04-04DIAGSU-01`` by the RFI memo).
"""

from __future__ import annotations

import colorsys
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import shapely
from shapely.geometry import Polygon
from shapely.strtree import STRtree

from .. import common
from . import calibrate as cal

RED = "#FF0000"
REPLACEMENT_FOR_RED = "#FFC000"     # amber: not used by any other sample zone, distinct from every sample fill
TAG_RE = re.compile(r"^(AHU|PAHU|FCU|EAF|OAF|TEAF|RAF|SAF)-B-(\d{2})-(\d{2})([A-Z0-9]+)-(\d{2}[AB]?(?:/B)?)$")
MAJORITY = 0.5
CONFIDENCE_MIN = 0.9


@dataclass
class Zone:
    key: str                        # unique zone key (governing AHU tag, or class key)
    kind: str                       # "AHU" | "FCU" | "MV"
    source: str                     # "RFI-ME-0233 sample" | "Appendix D"
    tag: str                        # governing tag (subject of the annotation)
    sample_tag: str = ""            # tag as written on the source document
    tag_basis: str = ""
    polygons_pv: List[Polygon] = field(default_factory=list)   # background page-view frame
    fill_hex: str = "#808080"
    sample_fill_hex: str = ""
    departments: str = ""
    storey_served: str = ""
    plant_level: str = ""
    refs: List[str] = field(default_factory=list)
    transfer_residual_mm: float = 0.0
    mots: bool = False
    members: List[dict] = field(default_factory=list)


def parse_tag_levels(tag: str) -> Tuple[str, str]:
    """(plant level, storey served) from ``AHU-B-<plant>-<served><DEPT>-<nn>``; empty strings when it does not parse."""
    m = TAG_RE.match(tag.strip())
    if not m:
        return "", ""
    return str(int(m.group(2))), str(int(m.group(3)))


def hex_to_rgb(h: str) -> Tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def avoid_pure_red(hex_colour: str) -> str:
    """Replace pure red (#FF0000, reserved for review mark-ups) by amber."""
    return REPLACEMENT_FOR_RED if hex_colour.upper() == RED else hex_colour.upper()


def governing_tags(conflicts_rows: Sequence[dict], crossref_rows: Sequence[dict]) -> Tuple[Dict[str, Tuple[str, str]], set]:
    """Map sample tag -> (governing tag, basis).

    CF-0003 supplies the two transposed sample tags; the cross-reference file confirms the governing tags.
    Returns the map and the set of tags the cross-reference lists.
    """
    mapping: Dict[str, Tuple[str, str]] = {}
    for r in conflicts_rows:
        if r.get("conflict_id") == "CF-0003":
            a = [x.strip() for x in r["value_a"].split(";")]
            b = [x.strip() for x in r["value_b"].split(";")]
            for sa, sb in zip(a, b):
                mapping[sa] = (sb, "CF-0003 (RFI-ME-0233 memo Table 3 governs)")
    listed = set()
    for r in crossref_rows:
        for col in ("governing_tag_airside_schematic",):
            for tok in re.split(r"[;]| and |, (?=AHU)", r[col]):
                tok = tok.strip()
                if tok:
                    listed.add(tok)
    return mapping, listed


def read_sample_polygons(sample_page, sample_zones_rows: Sequence[dict]) -> List[dict]:
    """The 15 Polygon annotations of the zoning sample with their vertices (page-view pt) and fill."""
    import pymupdf

    rot = sample_page.rotation_matrix
    by_xref = {int(r["annot_xref"]): r for r in sample_zones_rows}
    out = []
    for a in sample_page.annots():
        if a.xref not in by_xref:
            continue
        pts = np.array([tuple(pymupdf.Point(p) * rot) for p in a.vertices], float)
        row = by_xref[a.xref]
        out.append({"xref": a.xref, "pts_pv": pts, "row": row, "fill": row["fill_hex"].upper(),
                    "tag": row["tags_freetext_inside"]})
    return out


def build_sample_zones(sample_polys: Sequence[dict], calib_sample: cal.Calibration, calib_bg: cal.Calibration,
                       tag_map: Dict[str, Tuple[str, str]], crossref_listed: set, bubble_residual_mm: float) -> List[Zone]:
    """Transfer each sample polygon to the background frame and attach governing tag, levels and colour."""
    zones = []
    for sp in sample_polys:
        q = cal.transfer_points(sp["pts_pv"], calib_sample, calib_bg)
        poly = Polygon(q).buffer(0)
        tag = sp["tag"]
        gov, basis = tag_map.get(tag, (tag, ""))
        if not basis:
            basis = "tag as written; listed in tag_crossref_rfi_me_0233.csv" if any(tag == x or tag.replace("/B", "") in x for x in crossref_listed) \
                else "tag as written on the sample (not listed in the cross-reference)"
        plant, served = parse_tag_levels(gov)
        row = sp["row"]
        mot = "MOT" in (row.get("contents", "") or "") or row.get("pattern", "") not in ("", None)
        z = Zone(key=gov, kind="AHU", source="RFI-ME-0233 sample", tag=gov, sample_tag=tag, tag_basis=basis,
                 polygons_pv=[poly], fill_hex=avoid_pure_red(sp["fill"]), sample_fill_hex=sp["fill"],
                 departments="%s (%s)" % (row["dept_code"], row["department"]), storey_served=served or "4", plant_level=plant,
                 refs=["data/source/Template/AHU Zoning - RFI-ME-0233.pdf p1 annot xref %s" % sp["xref"],
                       "out/registers/sample_zones.csv zone_index %s" % row["zone_index"],
                       "out/registers/tag_crossref_rfi_me_0233.csv", "out/registers/conflicts.csv CF-0003" if tag in tag_map else ""],
                 transfer_residual_mm=bubble_residual_mm, mots=bool(row.get("pattern")))
        z.refs = [r for r in z.refs if r]
        zones.append(z)
    return zones


_INSIDE_RE = re.compile(r"([A-Z0-9/\-]+)\s*\[ann (\d+), ([\d.]+) pt\]")


def classify_appd_spaces(appd_polys: Sequence, space_rows: Sequence[dict], equipment_rows: Sequence[dict]) -> List[dict]:
    """Classify each App D polygon as an AHU, FCU or MV zone from the equipment tags in or beside it.

    The tag nearest the polygon decides (inside = 0 pt); an AHU tag outranks an FCU tag, which outranks fans, on
    equal distance.  Fans only (EAF, OAF, TEAF) or a mode label ``MV`` with no AHU or FCU give an MV zone.
    Confidence: 1.0 for a tag inside the polygon, 0.7 for one within 20 pt, 0.5 within 60 pt.
    """
    by_idx = {int(r["annotation_index"]): r for r in space_rows}
    eq_by_space: Dict[int, List[dict]] = {}
    for e in equipment_rows:
        eq_by_space.setdefault(int(e["space_annotation_index"]), []).append(e)
    out = []
    rank = {"AHU": 0, "PAHU": 0, "FCU": 1}
    for ap in appd_polys:
        row = by_idx.get(ap.annotation_index)
        cands = []
        if row:
            for m in _INSIDE_RE.finditer(row.get("equipment_tags_within_60pt", "")):
                cands.append((float(m.group(3)), m.group(1)))
        cands.sort(key=lambda c: (c[0], rank.get(c[1].split("-")[0], 2)))
        mode = row.get("mode_label", "") if row else ""
        names = row.get("space_names_text_layer", "") if row else ""
        if cands:
            d, tag = cands[0]
            typ = tag.split("-")[0]
            kind = "AHU" if typ in ("AHU", "PAHU") else "FCU" if typ == "FCU" else "MV"
            conf = 1.0 if d == 0.0 else 0.7 if d <= 20 else 0.5
            basis = "%s %s pt" % (tag, "inside" if d == 0.0 else "%.1f" % d)
        elif mode == "MV":
            tag, kind, conf, basis = "", "MV", 0.5, "mode label MV, no equipment tag within 60 pt"
        else:
            tag, kind, conf, basis = "", "", 0.0, "no equipment tag within 60 pt"
        out.append({"annotation_index": ap.annotation_index, "xref": ap.xref, "tag": tag, "kind": kind, "confidence": conf,
                    "basis": basis, "mode": mode, "names": names, "caption_m2": ap.caption_m2})
    return out


def merge_appd_zones(appd_polys: Sequence, classes: Sequence[dict]) -> List[Zone]:
    """One zone per governing tag (AHU or fan set) from the App D polygons; unclassified polygons are dropped (listed elsewhere)."""
    groups: Dict[Tuple[str, str], List[Tuple[object, dict]]] = {}
    for ap, c in zip(appd_polys, classes):
        if not c["kind"]:
            continue
        key = (c["kind"], c["tag"] or "MV (no tag)")
        groups.setdefault(key, []).append((ap, c))
    zones = []
    palette = _palette(len(groups))
    for (kind, tag), items in sorted(groups.items()):
        plant, served = parse_tag_levels(tag)
        z = Zone(key=("APPD:%s" % tag), kind=kind, source="Appendix D", tag=tag, tag_basis="App D equipment tag in/near polygon",
                 polygons_pv=[ap.polygon_pv_bg for ap, _ in items], fill_hex=palette.pop(0),
                 departments="Common areas (App D)", storey_served=served or "4", plant_level=plant,
                 refs=["data/source/Basis/App D/Appendix D - ACMV For Common Areas_Combined.pdf p5 annots %s" %
                       ",".join(str(ap.annotation_index) for ap, _ in items),
                       "out/registers/appd_spaces.csv", "out/registers/appd_equipment.csv"],
                 members=[c for _, c in items])
        zones.append(z)
    return zones


def _palette(n: int) -> List[str]:
    """Distinct mid-saturation colours, never red, for the App D zones."""
    out = []
    hues = [0.55, 0.30, 0.75, 0.12, 0.45, 0.65, 0.20, 0.85, 0.38, 0.08]
    for i in range(n):
        h = hues[i % len(hues)]
        s, v = (0.45, 0.85) if i < len(hues) else (0.6, 0.7)
        r, g, b = colorsys.hsv_to_rgb(h, s, v)
        out.append("#%02X%02X%02X" % (int(r * 255), int(g * 255), int(b * 255)))
    return out


def assign_rooms(rooms: Sequence[dict], zones: Sequence[Zone]) -> List[dict]:
    """Assign each room (dict with ``polygon`` in page view and ``area_pt2``) to the zone with the majority of its area.

    RFI sample zones are tried first (they are the later document); only rooms the sample does not cover are classed
    from App D.  ``share`` is the covering share of the winning zone, ``confidence`` the same value (1.0 = wholly inside).
    """
    ordered = [z for z in zones if z.source.startswith("RFI")] + [z for z in zones if not z.source.startswith("RFI")]
    geoms: List[Tuple[int, Polygon]] = []
    for zi, z in enumerate(ordered):
        for p in z.polygons_pv:
            geoms.append((zi, p))
    tree = STRtree([g for _, g in geoms])
    out = []
    for r in rooms:
        poly: Polygon = r["polygon"]
        cover: Dict[int, float] = {}
        for gi in tree.query(poly, predicate="intersects"):
            zi, g = geoms[gi]
            cover[zi] = cover.get(zi, 0.0) + poly.intersection(g).area
        rfi = {zi: a for zi, a in cover.items() if ordered[zi].source.startswith("RFI")}
        other = {zi: a for zi, a in cover.items() if not ordered[zi].source.startswith("RFI")}
        pick_from: Dict[int, float] = {}
        for cand in (rfi, other):
            if cand and max(cand.values()) / poly.area > MAJORITY:
                pick_from = cand
                break
        rec = dict(r)
        if pick_from and max(pick_from.values()) / poly.area > MAJORITY:
            zi = max(pick_from, key=pick_from.get)
            z = ordered[zi]
            share = pick_from[zi] / poly.area
            rec.update(zone_key=z.key, zone_tag=z.tag, zone_kind=z.kind, zone_source=z.source, share=share, confidence=share,
                       overlaps=";".join("%s=%.2f" % (ordered[k].tag, v / poly.area) for k, v in sorted(cover.items(), key=lambda kv: -kv[1]) if k != zi),
                       class_label=z.tag)
        else:
            best = max(cover.values()) / poly.area if cover else 0.0
            rec.update(zone_key="", zone_tag="", zone_kind="", zone_source="", share=best, confidence=0.0, overlaps=
                       ";".join("%s=%.2f" % (ordered[k].tag, v / poly.area) for k, v in sorted(cover.items(), key=lambda kv: -kv[1])),
                       class_label="no information")
        out.append(rec)
    return out
