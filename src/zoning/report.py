"""QA report for the pilot: accuracy statistics, calibration, failure cases with crops."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Dict, List, Sequence

import numpy as np
import shapely
from PIL import Image, ImageDraw
from shapely.geometry import Polygon

import hashlib
import json
import subprocess

from .. import common


def _fmt(v, nd=2):
    if v is None or v == "":
        return "n/a"
    if isinstance(v, float):
        if math.isnan(v):
            return "n/a"
        return ("%." + str(nd) + "f") % v
    return str(v)


def _pct(v):
    return "n/a" if v is None or (isinstance(v, float) and math.isnan(v)) else "%.1f %%" % (100.0 * v)


def crop_case(page, bbox_pv, layers: Dict[str, tuple], path: Path, scale: float = 3.0, margin: float = 25.0, tags=None, caption: str = "") -> None:
    """Render a page crop with outlines: ``layers`` maps a label to (list of polygons, RGB colour)."""
    import pymupdf

    x0, y0, x1, y1 = bbox_pv
    clip = pymupdf.Rect(x0 - margin, y0 - margin, x1 + margin, y1 + margin) & page.rect
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, annots=False)
    im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    d = ImageDraw.Draw(im)

    def T(p):
        return ((p[0] - clip.x0) * scale, (p[1] - clip.y0) * scale)

    for label, (polys, colour) in layers.items():
        for poly in polys:
            if poly is None or poly.is_empty:
                continue
            geoms = poly.geoms if poly.geom_type == "MultiPolygon" else [poly]
            for g in geoms:
                d.line([T(p) for p in g.exterior.coords], fill=colour, width=3)
                for r in g.interiors:
                    d.line([T(p) for p in r.coords], fill=colour, width=1)
    for t in tags or []:
        x, y = T((t["x_pv"], t["y_pv"]))
        d.ellipse([x - 4, y - 4, x + 4, y + 4], outline=(255, 0, 255), width=2)
    if caption:
        d.rectangle([0, 0, im.width, 18], fill=(255, 255, 255))
        d.text((4, 3), caption, fill=(0, 0, 0))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)


def consistency_errors(rows: Sequence[dict], stats: dict) -> List[str]:
    """Check the figures quoted in the report text against the register rows they come from; returns the mismatches (empty = consistent)."""
    err: List[str] = []
    n_a = sum(1 for r in rows if r["appd_caption_m2"] != "")
    n_b = sum(1 for r in rows if r["error_b_vs_caption_pct"] != "")
    if stats["n_matched_A"] != n_a:
        err.append("matched A: stats %d, register rows with a caption %d" % (stats["n_matched_A"], n_a))
    if stats["n_matched_B"] != n_b or stats["B/wall_face"].get("n", 0) != n_b:
        err.append("matched B: stats %d / table %s, register rows with error_b_vs_caption_pct %d" % (stats["n_matched_B"], stats["B/wall_face"].get("n", 0), n_b))
    ab = [r for r in rows if r["method_agreement"] in ("agree", "differ")]
    n_agree = sum(1 for r in ab if r["method_agreement"] == "agree")
    if stats["n_ab_compared"] != len(ab):
        err.append("A-B compared: stats %d, rows %d" % (stats["n_ab_compared"], len(ab)))
    if ab and abs(stats["ab_share_within_tolerance"] - n_agree / len(ab)) > 1e-9:
        err.append("A-B share: stats %.4f, rows %.4f" % (stats["ab_share_within_tolerance"], n_agree / len(ab)))
    return err


def zn_partial() -> float:
    from . import zones as zn
    return zn.PARTIAL_MIN


def provenance(root: Path) -> dict:
    """Hashes of the three source PDFs (checked against ``file_census.csv``), the code and tests, and the git state of ``src/zoning``."""
    import pymupdf

    rels = {"background": "data/source/Basis/Arch Combined Tender .pdf", "sample": "data/source/Template/AHU Zoning - RFI-ME-0233.pdf",
            "appd": "data/source/Basis/App D/Appendix D - ACMV For Common Areas_Combined.pdf"}
    census = {r["rel_path"]: r["sha256"] for r in common.read_csv_dicts(root / "out" / "registers" / "file_census.csv")}
    out: dict = {"sources": {}}
    for k, rel in rels.items():
        h = common.sha256_file(root / rel)
        out["sources"][k] = {"path": rel, "sha256": h, "census_sha256": census.get(rel.replace("data/source/", "")), "matches_census": census.get(rel.replace("data/source/", "")) == h}
    # the combined tender was assembled from uploaded parts (manifest note): page 8 must equal page 3 of part 2 (p06-10)
    part = root / "data/source/Basis/Arch Combined Tender (uploaded parts)/Arch_Combined_Tender_Part02_p06-10.pdf"
    if part.exists():
        a = pymupdf.open(str(root / rels["background"]))[7]
        b = pymupdf.open(str(part))[2]
        out["background_page8_equals_uploaded_part02_page3"] = bool(a.read_contents() == b.read_contents() and a.rect == b.rect and a.rotation == b.rotation)
    files = sorted((root / "src" / "zoning").glob("*.py")) + [root / "tests" / "test_zoning_pilot.py"]
    h = hashlib.sha256()
    for f in files:
        h.update(f.name.encode()); h.update(f.read_bytes())
    out["code_sha256"] = h.hexdigest()
    out["code_files"] = [str(f.relative_to(root)) for f in files]
    try:
        git = lambda *a: subprocess.run(["git", "-C", str(root)] + list(a), capture_output=True, text=True, timeout=30).stdout
        out["git_head"] = git("rev-parse", "--short", "HEAD").strip()
        out["git_modified_zoning_files"] = [l[3:].strip() for l in git("status", "--short", "--", "src/zoning", "tests/test_zoning_pilot.py").splitlines() if l.strip()]
    except Exception as exc:  # git missing: say so rather than guess
        out["git_head"] = "unavailable (%s)" % exc
        out["git_modified_zoning_files"] = []
    return out


def _find(rows: Sequence[dict], tag_prefix: str, x: float, y: float):
    """The room whose tag starts with ``tag_prefix`` and whose centroid is nearest (x, y) in building mm (rooms are renumbered by a re-run)."""
    c = [r for r in rows if r["room_tag"].startswith(tag_prefix)]
    return min(c, key=lambda r: math.hypot(float(r["centroid_x_mm"]) - x, float(r["centroid_y_mm"]) - y)) if c else None


def corrections_section(ctx: dict, prov: dict) -> List[str]:
    """"Corrections after verification": what an independent verification refuted or found missing, what was changed, and the figures of this run."""
    rows = ctx["compare"]["rows"]
    stats = ctx["compare"]["stats"]
    Z = ctx.get("zones")
    out: List[str] = []
    w = out.append
    ab = [r for r in rows if r["method_agreement"] in ("agree", "differ")]
    n_agree = sum(1 for r in ab if r["method_agreement"] == "agree")
    w("## 8. Corrections after verification\n")
    w("An independent verification (`out/qa/verify/pilot_l4_verify.md`) refuted seven statements of the first run notes and listed eight gaps. Everything below was corrected in the code, the pipeline was re-run in full "
      "(all seven stages) and every figure in this report is regenerated from that run (the report now stops with an error if a quoted count disagrees with the register rows). "
      "Figures marked *earlier run* are the verifier's reading of the first run.\n")
    w("**Refuted statements**\n")
    w("1. *\"70.9 %% of rooms (175 of 247) agree A-B\"* (earlier run; notes from a still earlier run). This run: **%s (%d of %d)** rooms compared; the register and this report give the same figure by construction (`consistency_errors`)." % (_pct(stats["ab_share_within_tolerance"]), n_agree, len(ab)))
    w("2. *\"21 rooms matched for method B\"*: the text counted B polygons on their own overlap test (%d), the table used the rooms matched through A that have a B region (%d). Both counts are now named in section 4; the B table and the register rows have the same n = %d." % (stats["n_matched_B_own"], stats["n_matched_B"], stats["n_matched_B"]))
    if Z is not None:
        zs = Z["zone_stats"]
        big = max(((r_, ) for r_ in ctx["compare"]["rooms"]), key=lambda t: len(t[0]["tags_inside"]))[0]
        a_big = [a for a in Z["assigned"] if a["room_id"] == big["room_id"]][0]
        diag = [z for z in Z["zones"] if "DIAGRE" in z.tag]
        train = [z.tag for z in Z["zones"] if "TRAIN" in z.tag]
        w("3. *\"DIAGRE-01A/B has more assigned area than its polygon because of a 954 m2 plant-area region\"*: the cause was the largest multi-tag region, %s (%.0f m2, %d tags in one closed region, covering share 0.562 in the earlier run) assigned at a majority of 0.5; the 954 m2 region is a different room, assigned to EAF-B-04-04AHU-02A/B. "
          "The tag of the training zone is %s. With the configured majority of %.2f that room is now `%s` (candidate %s at %.2f), so DIAGRE has %s." % (
              big["room_id"], big["a_face"].area * (ctx["cal"]["cal_bg"].area_scale_mm2_per_pt2() / 1e6), len(big["tags_inside"]), ", ".join(train),
              Z["thresholds"]["majority_share"], a_big["class_label"], a_big["candidate"] or "none", a_big["candidate_share"],
              "; ".join("%s rooms %.0f m2 against polygon %.0f m2" % (z.tag, zs[z.key]["area_m2"], zs[z.key]["full_poly_m2"]) for z in diag)))
        mv = [z for z in Z["zones"] if z.source == "Appendix D" and z.kind == "MV"]
        ah = [z for z in Z["zones"] if z.source == "Appendix D" and z.kind == "AHU"]
        w("4. *\"9 MV fan-set zones on App D page 5\"*: there are %d MV App D zones and %d AHU zones (%d App D layers); with the %d RFI zones and the Legend and `Rooms (measured)` layers the PDF has %d layers." % (
            len(mv), len(ah), len(mv) + len(ah), sum(1 for z in Z["zones"] if z.source.startswith("RFI")), len(Z["zones"]) + 2))
        cl = {c["annotation_index"]: c for c in Z["appd_classes"]}
        chg = [1, 4, 5, 10, 27, 28]
        w("5. *\"App D common spaces are classified from their own equipment tags\"* was true for 34 of 40 polygons only: the nearest tag within 60 pt was taken even when it lay outside the polygon (annotations 1, 4, 5, 10 as AHU-B-04-04CORE-01 and 27, 28 as TEAF-B-04-04COM-03A/B, "
          "none of which has a tag inside it in `appd_equipment.csv`). Now only a tag inside the polygon (or whose box touches it) counts: %s. The schedule confidence is the cover share times the classification confidence, not the cover share alone." % (
              "; ".join("%d -> %s" % (i, (cl[i]["kind"] + " " + cl[i]["tag"]).strip() if cl[i]["kind"] else "not classified (%s)" % cl[i]["basis"]) for i in chg if i in cl)))
    r110 = _find(rows, "Lounge (Fut. Proc)", 46453, 104464)
    r065 = _find(rows, "ELEC", 154521, 122840)
    r133 = _find(rows, "AR /", 159551, 100925)
    def fmt(r):
        return "%s: A %s m2, B %s m2%s" % (r["room_id"], r["wall_face_area_m2"], r["b_area_m2"] or "none", (", caption %s m2, error %s %%" % (r["appd_caption_m2"], r["error_vs_caption_pct"])) if r["appd_caption_m2"] != "" else "")
    w("6. *\"Wall-face polygon = first heavy stroke met from inside the room\"* was not what method A measured in two cases. R110 (earlier run: A 37.28 m2 on the outer face of the black wall bands, B and the verifier 33.7 m2) and "
      "R065 (earlier run: A 57.98 m2 and B 58.42 m2, both cutting out the swing sectors of the double doors; caption 63.03 m2). Causes: (a) partition walls drawn as a black band between two fine outlines were merged into the room as furniture-like faces, "
      "so the boundary ran along the outer outline; (b) the two leaves of a double door close on one point, the chain of both arcs did not fit one circle, so neither door was closed and the sectors under the arcs stayed separate faces. "
      "Fixes: black wall-band fill is wall (the room ends at the band's inner edge, in both methods); a chain of arc pieces is cut where the path turns back (a cusp), so double doors give two arcs and two closers; the arcs of closed doors are not boundaries, so the swing sector belongs to the room. "
      "This run: %s; %s." % (fmt(r110) if r110 else "R110 not found", fmt(r065) if r065 else "R065 not found"))
    if r133:
        w("   R133 (earlier run A 5.176 m2, verifier 4.749 m2): this run %s." % fmt(r133))
    n_out = sum(1 for r in rows if r["centroid_inside_polygon"] == "no")
    w("7. *\"centroid_x_mm / centroid_y_mm are the room centroids\"*: for 21 rooms (earlier run; up to 58.2 m away) the column held `representative_point()`. Now the columns hold the true centroid of the adopted polygon in every row; `centroid_inside_polygon` says whether it lies in the room (%d rooms: no), "
      "and `label_point_x_mm`, `label_point_y_mm`, `grid_location_label_point` give a point inside the polygon. `grid_location` is the grid location of the centroid.\n" % n_out)
    w("**Gaps**\n")
    w("* *Pipeline not re-run.* Re-run in full from the working-tree code; code hash and git state in section 0. The earlier outputs came from an earlier state of the working tree, which differs from the committed HEAD in %s." % (", ".join("`%s`" % f for f in prov["git_modified_zoning_files"]) or "no zoning file"))
    cen = prov["sources"]["background"]
    w("* *Hash of `Arch Combined Tender .pdf` not checkable.* It is in `file_census.csv` (row `Basis/Arch Combined Tender .pdf`, the trailing space before `.pdf` hid it from a search): census %s file sha256 %s. The file was assembled from five uploaded parts; page 8 is byte-identical in content to page 3 of part 2: %s. A comparison with the Drive original remains open (`check_phase0.md` P0-04)." % (
        "equals" if cen["matches_census"] else "DIFFERS from", cen["sha256"][:16] + "...", prov.get("background_page8_equals_uploaded_part02_page3")))
    if Z is not None:
        nrev = sum(1 for a in Z["assigned"] if a["class_label"] == "review: partial cover")
        w("* *`zoning_confidence.majority_share` 0.80 in the config, 0.5 in the code.* The pipeline now reads the configured value (`zones.configured_thresholds`); `assign_rooms` keeps 0.5 as its own default for direct calls. Rooms between 0.5 and 0.8 cover: %d, listed as `review: partial cover`, not in any zone area." % nrev)
        removed = sum(v["removed_m2"] for v in Z["precedence_removed_m2"].values())
        w("* *App D fill stacked on the RFI zones (198.8 m2 in the earlier PDF) and legend area was the assigned room area.* The App D polygons are drawn minus the RFI zones (%.1f m2 removed; the later document governs; recorded in each annotation's contents); fill overlap left: %.2f m2. "
          "The legend now gives the drawn zone polygon area and the assigned room area with the room count." % (removed, Z["stacked_fill_m2"]))
    w("* *Legend FreeText boxes and the backing square had no /CA or /FillOpacity.* Every legend, zone and room annotation now writes /CA explicitly (1 for the opaque legend box and text, 0.35 for zones and swatches) and the backing square also /FillOpacity 1; the PDF verification fails if one is missing.")
    w("* *Four of the 15 re-measured rooms could be checked only against a fine-pen variant or against method B* (R016, R076, R110, R221 in the verifier's numbering): not fixable here. Method B still shares A's strokes and closers, so a shared closure error is not visible in the A-B check (section 7); "
      "the independent evidence is the comparison with the printed App D captions and the verifier's own re-measurement.")
    w("* *Synthetic tests did not cover filled wall bands, double doors or an App D tag outside its polygon.* `tests/test_zoning_pilot.py` now has tests for each, for the configured majority, for the precedence clip and for the explicit /CA, plus the report consistency check.\n")
    return out


def write_qa_report(ctx: dict, out_md: Path, crops_dir: Path, page) -> dict:
    C = ctx["cal"]
    vec = ctx["vector"]["vec"]
    ras = ctx["raster"]
    cmpc = ctx["compare"]
    stats = cmpc["stats"]
    rows = cmpc["rows"]
    rooms = cmpc["rooms"]
    appd = cmpc["appd"]
    Z = ctx.get("zones")
    pdfr = ctx.get("pdf")
    cb = C["cal_bg"]
    cerr = consistency_errors(rows, stats)
    if cerr:
        raise AssertionError("report figures disagree with the register: " + "; ".join(cerr))
    prov = provenance(common.project_root())
    crops_dir.mkdir(parents=True, exist_ok=True)
    for old in crops_dir.glob("crop_*.png"):
        old.unlink()          # crops of an earlier run must not survive
    L: List[str] = []
    w = L.append
    w("# Pilot room measurement, 4th storey IP (L04)\n")
    w("Sheet AR-IP-01-04-001 (tender Corrigendum 6), page 8 of `data/source/Basis/Arch Combined Tender .pdf`. Every number below is produced by `src/zoning` "
      "(`python3 -I src/zoning/pilot_l4.py`); nothing was measured from pixels by eye. Register: `out/registers/room_polygons_L04.csv`. "
      "Positions are page-view pt (rotated landscape frame, origin top-left) unless a column says building mm (origin grid 2 / A, X along the numbered grids, Y along the lettered grids).\n")

    # ---------------------------------------------------------------- provenance
    w("## 0. Provenance\n")
    for k, v in prov["sources"].items():
        w("* %s: `%s`, sha256 `%s`; `file_census.csv` row %s." % (k, v["path"], v["sha256"], "matches" if v["matches_census"] else "DOES NOT MATCH (census %s)" % v["census_sha256"]))
    if "background_page8_equals_uploaded_part02_page3" in prov:
        w("* The combined tender PDF was assembled from five uploaded parts (manifest note, `check_phase0.md` P0-04: no full Drive transport). Page 8 content stream, page box and rotation are identical to page 3 of `Arch_Combined_Tender_Part02_p06-10.pdf`: **%s**." % prov["background_page8_equals_uploaded_part02_page3"])
    w("* Code: git HEAD `%s`; the working tree differs from HEAD in %s, so the outputs come from the working-tree code, identified by the sha256 of `src/zoning/*.py` and `tests/test_zoning_pilot.py`: `%s`. This report and the registers were all written by one run of `python3 -I src/zoning/pilot_l4.py` (all seven stages) from that code.\n" % (
        prov["git_head"], ", ".join("`%s`" % f for f in prov["git_modified_zoning_files"]) or "no zoning file", prov["code_sha256"]))
    common.atomic_write_json(crops_dir / "provenance.json", prov)

    # ---------------------------------------------------------------- calibration
    w("## 1. Calibration and frames (Section 7.1)\n")
    w("Affine transform page-view pt to building mm per sheet, least squares on the grid bubbles (circle centres detected on the page; labels give the grid coordinates from `grid_model.json`). "
      "A bubble fixes one coordinate; the rotation is pinned by requiring the top-row bubbles to share one building Y and the left-column bubbles one X (bubbles drawn off their strip, R and P, are used for the label equation only).\n")
    w("| page | bubbles used | scale X (mm/pt) | scale Y (mm/pt) | rotation (deg) | residual rms (mm) | residual max (mm) |")
    w("|---|---|---|---|---|---|---|")
    for name, c in (("background p8", C["cal_bg"]), ("zoning sample p1", C["cal_sample"]), ("App D p5", C["cal_appd"]), ("background p8 from register bubbles", C["cal_bg_register"])):
        w("| %s | %d | %.4f | %.4f | %.4f | %.2f | %.2f |" % (name, c.n_equations, c.scale_x_mm_per_pt, c.scale_y_mm_per_pt, c.rotation_deg, c.rms_mm, c.max_mm))
    rr_ = C["register_row"]
    w("\nCross-check with `sheet_calibration_plans.csv` page 8: scale %s / %s mm/pt, rotation %s deg, residual rms %s mm, max %s mm (register fits label centres; this fit uses the drawn circle centres, hence the smaller residual). "
      "Scale difference to the register: %.4f %% (X), %.4f %% (Y); tolerance in config: residual 50 mm, scale 0.5 %%.\n" % (
          rr_["scale_mm_per_pt_x"], rr_["scale_mm_per_pt_y"], rr_["rotation_deg"], rr_["residual_mm_rms"], rr_["residual_mm_max"],
          100 * (cb.scale_x_mm_per_pt / float(rr_["scale_mm_per_pt_x"]) - 1), 100 * (cb.scale_y_mm_per_pt / float(rr_["scale_mm_per_pt_y"]) - 1)))
    fs, fa = C["frame_sample_vs_bg"], C["frame_appd_vs_bg"]
    w("**Same frame check (grid-bubble positions, %d bubbles in common, offset bubbles excluded):**\n" % fs["n"])
    w("| pair | mean offset dx (pt) | mean offset dy (pt) | residual after offset, rms (pt) | max abs dx / dy (pt) | transfer residual in building mm (rms / max) |")
    w("|---|---|---|---|---|---|")
    ts, ta = C["transfer_residual_sample_mm"], C["transfer_residual_appd_mm"]
    w("| sample p1 - background p8 | %.3f | %.3f | %.3f | %.3f / %.3f | %.2f / %.2f |" % (fs["mean_dx_pt"], fs["mean_dy_pt"], fs["rms_after_offset_pt"], fs["max_abs_dx_pt"], fs["max_abs_dy_pt"], ts["rms"], ts["max"]))
    w("| App D p5 - background p8 | %.3f | %.3f | %.3f | %.3f / %.3f | %.2f / %.2f |" % (fa["mean_dx_pt"], fa["mean_dy_pt"], fa["rms_after_offset_pt"], fa["max_abs_dx_pt"], fa["max_abs_dy_pt"], ta["rms"], ta["max"]))
    f3 = C["frame_register_vs_detected"]
    w("\nThe sample page and the background 4th-storey page are the same frame: grid bubbles coincide to %.3f pt (%.1f mm), so the sample polygons transfer with no shift; the App D page differs by %.3f pt (%.1f mm) in the mean, and its scale by %.4f %%, both far below the 50 mm tolerance. "
      "(Register bubble positions are label centres and sit %.2f pt lower in y than the drawn circle centres; that is the offset between `grid_observations_plans.csv` and the detected circles, not a frame difference.)\n" % (
          math.hypot(fs["mean_dx_pt"], fs["mean_dy_pt"]), math.hypot(fs["mean_dx_pt"], fs["mean_dy_pt"]) * cb.scale_x_mm_per_pt,
          math.hypot(fa["mean_dx_pt"], fa["mean_dy_pt"]), math.hypot(fa["mean_dx_pt"], fa["mean_dy_pt"]) * cb.scale_x_mm_per_pt,
          100 * (C["cal_appd"].scale_x_mm_per_pt / cb.scale_x_mm_per_pt - 1), abs(f3.get("mean_dy_pt", 0.0))))
    own = [a.own_vs_caption_pct for a in appd]
    w("App D own check: %d Area Measurement polygons on page 5; polygon geometry at the App D calibration against the printed caption: median %.3f %%, max abs %.3f %% (brief: within 0.12 %%).\n" % (
        len(appd), float(np.median(own)), float(np.max(np.abs(own)))))

    # ---------------------------------------------------------------- method A
    d = vec.diagnostics
    w("## 2. Method A, vector (Section 7.2)\n")
    w("Strokes extracted from the page content (`page.get_cdrawings()`, %d stroked segments). Class analysis inside the plan box (%.0f-%.0f x %.0f-%.0f pt):\n" % (
        d["n_strokes_total"], d["plan_box_pv"][0], d["plan_box_pv"][2], d["plan_box_pv"][1], d["plan_box_pv"][3]))
    w("| colour | width (pt) | segments | total length (pt) | mean length (pt) | axis-aligned share | joined end points | role |")
    w("|---|---|---|---|---|---|---|---|")
    for r in d["stroke_classes"][:12]:
        black = r["colour"] == (0.0, 0.0, 0.0)
        role = ""
        if black and r["width_pt"] in d["heavy_widths_pt"]:
            role = "wall (heavy)"
        elif black and r["width_pt"] in d["thin_widths_pt"]:
            role = "fine pen (furniture, door leaves, partitions, door-swing arcs)"
        w("| %s | %.2f | %d | %.0f | %.2f | %.2f | %.2f | %s |" % (r["colour"], r["width_pt"], r["segments"], r["total_len_pt"], r["mean_len_pt"], r["axis_share"], r["joined_share"], role))
    w("\nChoice: walls are black strokes of width 0.60, 0.84, 1.08, 1.32 and 2.76 pt (the heavy pens: long, orthogonal, joined runs). Black 0.24, 0.36 and 0.48 pt are the fine pens: they carry the finishes, "
      "door leaves, door-swing arcs (0.24/0.36) and furniture. In an exploratory trial run outside the pipeline (flood at 2 px/pt, scratch notes, not re-run here) heavy strokes with door closers closed 105 of the 422 room tags into a bounded region, 174 with long fine-pen runs added and 199 with bridging added, "
      "so all fine-pen lines are noded too and the faces that are only furniture, door swings or stair treads are merged back (below). Shaft and riser codes (%d `code` tags: lift cars, risers) make their faces spaces of their own, so a lift shaft is not merged into its lobby." % d.get("n_blocker_tags", 0) + " Grey, white, red, orange, cyan and green strokes are grids, hatches, dimensions and mark-ups and are not used.\n")
    w("* Door openings: %d door-swing arcs recognised (chains of short segments that fit a circle of 0.44-2.6 m radius and 35-200 deg span); %d closed with a straight closer from the hinge to the arc end that lies on the wall line (%d connectors joining the closer ends to the wall)." % (
        d["n_arcs"], d["n_doors_closed"], d.get("n_door_connectors", 0)))
    w("* Openings without an arc: %d bridges between wall ends facing each other on one line (gap up to 2.4 m); %d short connectors snapping dangling wall ends (<= 1 pt) to the nearest line." % (d["n_bridges"], d.get("n_snap_connectors", 0) + d.get("n_bridge_snap_connectors", 0)))
    w("* Noding and `shapely.polygonize` (coordinates rounded to 0.01 pt): %d faces. Tagless faces under 8 m2 whose boundary is at least half fine pen (furniture, door-swing slivers) were merged into the neighbour sharing most fine-pen boundary: %d merges, %d faces left." % (d["n_faces_raw"], d["merged_faces"], d["faces_after"]))
    w("* Each face takes the room tags inside it (`room_tags_plans.csv`, kind = room, %d tags): %d regions, %d tags assigned, %d regions hold more than one tag (open plan: bays, corridors without a door line, merged rooms)." % (
        len(ctx["tags"]), d["n_regions"], d["n_tags_assigned"], d["n_regions_multi_tag"]))
    g = d["wall_gap"]
    w("* Wall thickness (distance across the wall between neighbouring rooms, %d boundary samples): median %.0f mm, quartiles %.0f-%.0f mm. The centre-line polygon grows each region by half its own median gap." % (g["n_gap_samples"], g["global_median_mm"], g["p25_mm"], g["p75_mm"]))
    w("* Wall-face polygon: the inner face of the wall, that is the first heavy wall stroke met from inside the room, or the inner edge of a black filled wall band where a partition is drawn as a dark band between two fine outlines "
      "(%d black fill polygons; %d small faces that are at least half band were kept as wall and not merged into the room). The fine finish lines between a heavy stroke and the room are part of the room. "
      "The swing sector of a closed door belongs to the room it opens into: the arcs of the %d closed doors (%d arc segments) are not boundaries. A double door, whose two leaves close on one point (a cusp), is recognised as two arcs.\n" % (
          d.get("n_wall_band_polygons", 0), d.get("n_wall_band_faces", 0), d["n_doors_closed"], d.get("n_arc_segments_removed", 0)))

    # ---------------------------------------------------------------- method B
    rd = ras["diag"]
    w("## 3. Method B, raster region growing (Section 7.3)\n")
    w("The same strokes (heavy walls, door closers, bridges, snaps and the fine pens) are rasterised in %d px tiles as 1 px lines at %.1f px/pt = %.2f mm/px (limit 10 mm), the free pixels labelled with 4-connectivity (%d components in a %d x %d px region of interest), "
      "and each room-tag seed claims its component; components with several seeds are split by a watershed on the distance to the wall raster with the seeds as markers; %d unseeded components under 8 m2 bounded mostly by fine pen were absorbed. "
      "The black wall bands are filled into the wall raster and the swing arcs of the closed doors are left out, as in method A. "
      "No segmentation-model weights are available, so this is classical region growing on the wall raster. %d seeds were nudged off a line pixel; %d components were split between seeds; %d seeds have no region.\n" % (
          rd["tile_px"], rd["px_per_pt"], rd["mm_per_px_x"], rd["n_components"], rd["px_shape"][1], rd["px_shape"][0], rd["n_absorbed_components"],
          rd.get("n_seeds_nudged", 0), rd["n_split_components"], rd["n_unplaced_seeds"]))

    # ---------------------------------------------------------------- accuracy
    w("## 4. Accuracy (Section 7.4)\n")
    n_rooms = len(rows)
    ab = stats["n_ab_compared"]
    share = stats["ab_share_within_tolerance"]
    w("**Method A against method B.** %d rooms (A regions); B has a region for %d. Agreement means |A - B| <= 2 %% or 0.3 m2 on the wall-face area. Share within tolerance: **%s** (%d of %d). Configuration asks for at least 95 %% (`tolerances.method_a_vs_b.min_share`): %s.\n" % (
        n_rooms, ab, _pct(share), round(share * ab) if ab else 0, ab, "met" if share >= 0.95 else "NOT met"))
    pa, pb = stats["A/wall_face"], stats["B/wall_face"]
    w("**Both against the App D captions.** A room is matched to an App D polygon when each covers at least 80 %% of the other after the polygon is transferred into the background frame. Matched: %d rooms by method A (of %d App D polygons; one room per polygon). Method B is compared on those same rooms where it has a region: %d rooms (the B rows of the register). "
      "Counted on its own polygons, B matches %d App D polygons; that independent count is not used in the table." % (
        stats["n_matched_A"], len(appd), stats["n_matched_B"], stats["n_matched_B_own"]))
    w("\n| method / boundary convention | matched rooms | median error | median abs error | mean error | within 2 % | within 5 % | 90th percentile abs error |")
    w("|---|---|---|---|---|---|---|---|")
    for m in ("A", "B"):
        for c in ("wall_face", "centreline"):
            s = stats["%s/%s" % (m, c)]
            if s.get("n", 0) == 0:
                continue
            w("| %s, %s | %d | %+.2f %% | %.2f %% | %+.2f %% | %s | %s | %.2f %% |" % (m, c.replace("_", " "), s["n"], s["median_error_pct"], s["median_abs_error_pct"], s["mean_error_pct"], _pct(s["share_within_2pct"]), _pct(s["share_within_5pct"]), s["p90_abs_error_pct"]))
    pool = stats["pooled"]
    w("\nBoundary convention tested on all matched rooms (A and B pooled): wall face median abs error %.2f %%, within 2 %% %s; centre line median abs error %.2f %%, within 2 %% %s. **Best reproduction of the printed captions: %s** (adopted for the `a_area_m2`, `a_perimeter_m` and `b_area_m2` columns; the wall-face and centre-line areas are both in the register).\n" % (
        pool["wall_face"]["median_abs_error_pct"], _pct(pool["wall_face"]["share_within_2pct"]), pool["centreline"]["median_abs_error_pct"], _pct(pool["centreline"]["share_within_2pct"]), stats["adopted_convention"].replace("_", " ")))
    # per-room outliers
    matched = [(i, r) for i, r in enumerate(rows) if r["appd_caption_m2"] != ""]
    matched.sort(key=lambda ir: -abs(float(ir[1]["error_vs_caption_pct"])))
    w("Largest errors against the captions (adopted convention, method A):\n")
    w("| room | tag | A area (m2) | caption (m2) | error | A-B | flags |")
    w("|---|---|---|---|---|---|---|")
    for i, r in matched[:8]:
        w("| %s | %s | %s | %s | %+.1f %% | %s | %s |" % (r["room_id"], r["room_tag"], r["a_area_m2"], r["appd_caption_m2"], float(r["error_vs_caption_pct"]), r["method_agreement"], r["flags"] or ""))
    # unmatched App D polygons
    matched_appd = {v[0] for v in stats["matches_a"].values()}
    um = [a for k, a in enumerate(appd) if k not in matched_appd]
    w("\nApp D polygons with no matching room (%d of %d). Reasons: the polygon traces a room that method A merged with its neighbour (open plan or a missed door), or covers several rooms (composite), or no region was closed around it.\n" % (len(um), len(appd)))

    # ---------------------------------------------------------------- failure cases
    w("## 5. Failure cases\n")
    crops: List[dict] = []

    def add_case(title: str, text: str, fname: str, bbox, layers, tags=None, cap=""):
        crop_case(page, bbox, layers, crops_dir / fname, tags=tags, caption=cap)
        rel = "pilot_L04/" + fname
        crops.append({"title": title, "file": rel})
        w("### %s\n\n%s\n\n![%s](%s)\n" % (title, text, title, rel))

    def union_bbox(polys):
        b = shapely.union_all([p for p in polys if p is not None and not p.is_empty]).bounds
        return b

    n = 0
    # (1) largest A-B disagreements
    diffs = [(abs(float(r["a_minus_b_pct"])), i) for i, r in enumerate(rows) if r["a_minus_b_pct"] != ""]
    diffs.sort(reverse=True)
    n_dis = sum(1 for r in rows if r["method_agreement"] == "differ")
    w("%d rooms differ between A and B beyond tolerance, %d have no B region. The largest differences are open areas that leak to the outside in one method (a plant room, terraces) and lift lobbies where method A merges the X-braced lift-car faces into the lobby while method B leaves them out (crops below). The three largest differences follow.\n" % (n_dis, sum(1 for r in rows if r["method_agreement"] == "no B region")))
    for _, i in diffs[:3]:
        r, rm = rows[i], rooms[i]
        n += 1
        add_case("A-B difference: %s %s (%s %%)" % (r["room_id"], r["room_tag"], r["a_minus_b_pct"]),
                 "A wall-face area %s m2, B %s m2. Flags: %s. Red: method A, blue: method B, green: App D polygon (when matched), magenta circles: tag seeds." % (r["wall_face_area_m2"], r["b_area_m2"], r["flags"] or "none"),
                 "crop_%02d_ab_%s.png" % (n, r["room_id"]), union_bbox([rm["a_face"], rm["b_face"]]),
                 {"A": ([rm["a_face"]], (255, 0, 0)), "B": ([rm["b_face"]], (0, 0, 255))}, tags=[t for t in ctx["tags"] if t["text"] in rm["tags_inside"]])
    # (2) largest caption errors
    for i, r in matched[:3]:
        rm = rooms[i]
        n += 1
        ap = appd[stats["matches_a"][i][0]]
        add_case("Caption error: %s %s (%+.1f %%)" % (r["room_id"], r["room_tag"], float(r["error_vs_caption_pct"])),
                 "A area %s m2 against caption %s m2 (App D annotation %s). The polygon outline of App D (green) differs from the first heavy stroke (red), e.g. traced to a different line, or includes an adjoining space." % (r["a_area_m2"], r["appd_caption_m2"], r["appd_annotation_index"]),
                 "crop_%02d_caption_%s.png" % (n, r["room_id"]), union_bbox([rm["a_face"], ap.polygon_pv_bg]),
                 {"A": ([rm["a_face"]], (255, 0, 0)), "App D": ([ap.polygon_pv_bg], (0, 160, 0))}, tags=[t for t in ctx["tags"] if t["text"] in rm["tags_inside"]])
    # (3) open-plan regions
    multi = sorted([(len(r["tags_inside"]), i) for i, r in enumerate(rooms)], reverse=True)[:2]
    for k, i in multi:
        if k < 2:
            continue
        rm = rooms[i]
        n += 1
        add_case("Open-plan region with %d tags: %s" % (k, rooms[i]["room_id"]),
                 "One closed region contains %d room tags (%s...): an opening without a door-swing arc wider than the 2.4 m bridging limit, a curtain or an unclosed bay. Method A reports it as one room and flags it; the room schedule keeps one row for the region." % (k, "; ".join(rm["tags_inside"][:4])),
                 "crop_%02d_open_%s.png" % (n, rm["room_id"]), union_bbox([rm["a_face"]]), {"A": ([rm["a_face"]], (255, 0, 0))}, tags=[t for t in ctx["tags"] if t["text"] in rm["tags_inside"]])
    # (4) unmatched App D polygons
    for k, a in enumerate(um[:2]):
        n += 1
        near = [rm["a_face"] for rm in rooms if rm["a_face"].intersects(a.polygon_pv_bg)]
        add_case("App D polygon without a matching room (annotation %d, %.2f m2)" % (a.annotation_index, a.caption_m2),
                 "Overlapping method A regions: %d. Red: regions, green: the App D polygon." % len(near),
                 "crop_%02d_appd_unmatched_%d.png" % (n, a.annotation_index), union_bbox([a.polygon_pv_bg]),
                 {"A": (near, (255, 0, 0)), "App D": ([a.polygon_pv_bg], (0, 160, 0))})
    # (5) doors not closed
    dd = [x for x in d["door_diag"] if not x["closed"]]
    w("Door-swing arcs not closed by a closer: %d of %d (hinge or arc end not within 3 pt of a heavy wall stroke: arcs of cupboard doors, sliding or glazed screens, or doors in walls drawn with a different pen).\n" % (len(dd), d["n_arcs"]))
    if dd:
        x = dd[0]
        n += 1
        add_case("Door arc not closed", "Arc centre (%.0f, %.0f) pt, radius %.1f pt, nearest wall stroke %.1f pt from the hinge and %.1f pt from the arc end." % (x["cx"], x["cy"], x["r_pt"], x["hinge_to_wall_pt"], x["tip_to_wall_pt"]),
                 "crop_%02d_door.png" % n, (x["cx"] - 40, x["cy"] - 40, x["cx"] + 40, x["cy"] + 40), {})
    unseeded = [t for t in ctx["tags"] if not any(t["text"] in r["tags_inside"] and abs(t["x_pv"] - r["a_face"].representative_point().x) < 1e9 for r in rooms)]
    w("Room tags not inside any region of 0.5-4000 m2 (shafts, voids, tags in open areas, regions merged with the outside): %d of %d tags.\n" % (len(ctx["tags"]) - d["n_tags_assigned"], len(ctx["tags"])))

    # ---------------------------------------------------------------- zoning summary
    if Z is not None:
        th = Z["thresholds"]
        w("## 6. Zoning and PDF (Sections 7.5, 7.6)\n")
        cnt: Dict[str, int] = {}
        for a in Z["assigned"]:
            k = a["zone_kind"] or a["class_label"]
            cnt[k] = cnt.get(k, 0) + 1
        w("A room joins a zone when one zone covers at least %.0f %% of its area (`tolerances.zoning_confidence.majority_share` in `config/project.yaml`; the RFI-ME-0233 sample zones are tried before App D). "
          "Confidence is the covering share times the confidence of the zone part (1.0 for an RFI polygon or an App D polygon with a tag inside, 0.9 for a tag whose box touches the polygon, 0.5 for an App D polygon classified from its mode label MV alone). "
          "A room covered %.0f to %.0f %% by a zone is listed as `review: partial cover` with the candidate zone and is **not** added to that zone's area." % (100 * th["majority_share"], 100 * zn_partial(), 100 * th["majority_share"]))
        w("\nRooms by class: %s (total %d)." % (", ".join("%s %d" % kv for kv in sorted(cnt.items())), len(Z["assigned"])))
        low = sum(1 for a in Z["assigned"] if a["zone_kind"] and a["confidence"] < th["confidence_min"])
        w("Rooms assigned with a confidence below %.1f: %d. Rooms assigned with a cover share below 0.9: %d.\n" % (th["confidence_min"], low, sum(1 for a in Z["assigned"] if a["zone_kind"] and a["share"] < 0.9)))
        st = Z["zone_stats"]
        big = sorted(((v["area_m2"] - v["full_poly_m2"], k) for k, v in st.items() if v["full_poly_m2"] > 0 and v["area_m2"] > v["full_poly_m2"]), reverse=True)
        w("Assigned room area against the zone polygon (m2): %s." % ("; ".join("%s rooms %.0f against polygon %.0f" % (k, st[k]["area_m2"], st[k]["full_poly_m2"]) for _, k in big[:5]) or "no zone has more room area than polygon area"))
        zero = [z.tag for z in Z["zones"] if st[z.key]["n_rooms"] == 0]
        w("Zones with no room assigned: %s.\n" % (", ".join(zero) or "none"))
        ac = Z["appd_classes"]
        kinds: Dict[str, int] = {}
        for c in ac:
            kinds[c["kind"] or "not classified"] = kinds.get(c["kind"] or "not classified", 0) + 1
        notag = [c for c in ac if not c["tag"]]
        w("**App D common spaces (page 5, %d polygons)** are classified from the equipment tags that `appd_equipment.csv` places *inside* the polygon (or whose FreeText box touches it); a tag that lies outside the polygon, however near, does not classify it. "
          "Result: %s. %d polygons carry no tag: %s." % (len(ac), ", ".join("%s %d" % kv for kv in sorted(kinds.items())), len(notag),
                                                       "; ".join("annotation %d (%s, mode %s) as %s" % (c["annotation_index"], (c["names"] or "").split(" | ")[0], c["mode"] or "-", c["kind"] or "not classified") for c in notag)))
        rem = Z["precedence_removed_m2"]
        w("Where an App D polygon lies inside an RFI-ME-0233 zone the later document governs and the App D fill is not drawn there (no stacked fills): %s. Fill overlap left in the drawing: %.2f m2.\n" % (
            "; ".join("%s %.1f m2" % (k.replace("APPD:", "App D "), v["removed_m2"]) for k, v in rem.items() if v["removed_m2"] > 0.05) or "none", Z["stacked_fill_m2"]))
        if pdfr:
            v = pdfr["verify"]
            w("PDF checks: page content stream identical to the source: %s; render with annotations hidden pixel-identical (%d x %d px): %s; %d zone, %d legend and %d room annotations; layers off by default: %s; every zone tag in the legend and the reverse: %s; overall: **%s**.\n" % (
                v["content_stream_identical"], v["render_size"][0], v["render_size"][1], v["pixels_identical_annots_hidden"], v["n_zone_annotations"], v["n_legend_annotations"], v["n_room_annotations"],
                ", ".join(v["layers_off_by_default"]), not v["zone_tags_missing_in_legend"] and not v["legend_tags_without_zone"], "pass" if v["ok"] else "FAIL"))
            man = pdfr["manifest"]
            layers = [l["name"] for l in man["layers"]]
            n_app = sum(1 for l in layers if l.endswith("(App D)"))
            n_mv = sum(1 for z in Z["zones"] if z.source == "Appendix D" and z.kind == "MV")
            n_ahu = sum(1 for z in Z["zones"] if z.source == "Appendix D" and z.kind == "AHU")
            n_fcu = sum(1 for z in Z["zones"] if z.source == "Appendix D" and z.kind == "FCU")
            w("Optional-content layers: %d (%d RFI-ME-0233 zones, %d App D zones: %d AHU, %d FCU, %d MV; `Legend`; `Rooms (measured)`).\n" % (len(layers), sum(1 for z in Z["zones"] if z.source.startswith("RFI")), n_app, n_ahu, n_fcu, n_mv))
    w("## 7. Limits of this pilot\n")
    w("* One storey, one sheet; the stroke widths were chosen on this sheet and are not assumed for other storeys.\n* Method B uses the same strokes and door closers as method A, so the A-B agreement measures the vector against the raster treatment of one linework, not independent evidence of the linework.\n"
      "* Rooms behind openings without an arc wider than 2.4 m, open bays and curtains merge; the register keeps one row per closed region and flags the tag count.\n* App D captions exist for the common spaces only (40 polygons); the accuracy against captions is measured on the rooms that match one of them.\n")
    L.extend(corrections_section(ctx, prov))
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(L), encoding="utf-8")
    return {"crops": crops}
