"""Tests of src/zoning/booklet_zones.py: title mapping, polygons from drawings, similarity fit, tag assignment, registration on synthetic data,
and one run on a real booklet page (skipped when the sources are absent)."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

pymupdf = pytest.importorskip("pymupdf")
cv2 = pytest.importorskip("cv2")
shapely = pytest.importorskip("shapely")
from shapely.geometry import Polygon, box

from src.zoning import booklet_zones as bz

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ titles
@pytest.mark.parametrize("title,key", [
    ("IP 3RD STOREY- KEYPLAN", "L03"), ("IP 3RD STOREY KEYPLAN", "L03"), ("IP 2nd STOREY KEYPLAN", "L02"),
    ("IP BASEMENT 2- KEYPLAN", "B2"), ("IP BASEMENT 2 KEYPLAN", "B2"), ("IP 7TH STOREY KEYPLAN", "L07"),
    ("IP 4TH STOREY KEYPLAN – PART 1", "L04"), ("IP 6TH STOREY PART 2- KEYPLAN", "L06"), ("IP 1ST STOREY - KEYPLAN", "L01"),
    ("IP 8TH STOREY - KEYPLAN", None), ("OP 5TH STOREY KEYPLAN – PART 1", None), ("OP BASEMENT 1 (OP) KEYPLAN", None),
    ("IP 22ND & 23RD STOREY - KEYPLAN", None), ("IP 19TH, 20TH & 21ST STOREY - KEYPLAN", None), ("", None),
])
def test_title_to_key(title, key):
    assert bz.title_to_key(title) == key


def test_tag_regex_as_printed():
    for t in ["AHU-B-03-CSSD-03A", "FCU-B-03-03TR-01", "PAHU-B-B1-B2HOSP-01", "AHU-8-03-04RAD-01", "AHU-B-02-01EDP3WAIT-01A/B"]:
        assert bz.TAG_RE.match(t)
    for t in ["AHU ZONING", "AHU", "Meeting Room", "FCU"]:
        assert not bz.TAG_RE.match(t)


# ------------------------------------------------------------------ polygons from drawing items
def test_ring_with_hole_even_odd():
    items = [("re", pymupdf.Rect(0, 0, 100, 100), 1), ("re", pymupdf.Rect(40, 40, 60, 60), 1)]
    poly = bz.subpaths_to_polygon(bz.drawing_subpaths(items))
    assert poly.area == pytest.approx(10000 - 400)


def test_line_items_subpaths_split_on_gap():
    P = pymupdf.Point
    items = [("l", P(0, 0), P(10, 0)), ("l", P(10, 0), P(10, 10)), ("l", P(10, 10), P(0, 10)),
             ("l", P(50, 50), P(60, 50)), ("l", P(60, 50), P(60, 60)), ("l", P(60, 60), P(50, 60))]
    subs = bz.drawing_subpaths(items)
    assert len(subs) == 2
    assert bz.subpaths_to_polygon(subs).area == pytest.approx(200)


def test_rotation_matrix_applied():
    R = pymupdf.Matrix(0, 1, -1, 0, 842, 0)            # 90 degree page rotation
    subs = bz.drawing_subpaths([("re", pymupdf.Rect(0, 0, 10, 20), 1)], R)
    xs = sorted({round(p[0]) for p in subs[0]})
    ys = sorted({round(p[1]) for p in subs[0]})
    assert xs == [822, 842] and ys == [0, 10]


# ------------------------------------------------------------------ similarity fit
def _synthetic_matches(n=200, outliers=120, seed=3):
    rng = np.random.default_rng(seed)
    src = rng.uniform(0, 1500, (n, 2))
    ang, s, t = math.radians(7.0), 1.139, np.array([3131.0, 1369.0])
    M = np.array([[s * math.cos(ang), -s * math.sin(ang), t[0]], [s * math.sin(ang), s * math.cos(ang), t[1]]])
    dst = bz.apply_affine(M, src) + rng.normal(0, 0.6, (n, 2))
    bad = rng.choice(n, outliers, replace=False)
    dst[bad] = rng.uniform(0, 5000, (outliers, 2))
    return src, dst, M


def test_ransac_recovers_similarity_with_outliers():
    src, dst, M = _synthetic_matches()
    n, M0, inl = bz.ransac_similarity(src, dst, 3.0, 0.3, 3.0)
    assert n >= 70
    M1, inl1 = bz.refine_similarity(src, dst, M0, 3.0)
    assert np.allclose(M1, M, atol=0.5, rtol=0.01)
    assert inl1.sum() >= 78


def test_ransac_scale_window_rejects_wrong_scale():
    src, dst, M = _synthetic_matches(outliers=0)
    n, M0, inl = bz.ransac_similarity(src, dst, 3.0, 2.0, 3.0)       # true scale 1.139 is outside the window
    assert M0 is None or n < 10


def test_umeyama_has_no_reflection():
    src = np.array([[0, 0], [10, 0], [10, 5], [0, 5.0]])
    dst = src * [1, -1]                                              # mirror: a similarity without reflection cannot fit it
    M = bz.umeyama_similarity(src, dst)
    assert np.linalg.det(M[:, :2]) > 0


def test_fit_pv_to_mm_and_compose():
    rng = np.random.default_rng(0)
    pv = rng.uniform(0, 3000, (50, 2))
    A_true = np.array([[88.19, 0.0007, -3000.0], [-0.0005, -88.19, 150000.0]])
    mm = bz.apply_affine(A_true, pv)
    A, rms = bz.fit_pv_to_mm(pv, mm)
    assert rms < 1e-6 and np.allclose(A, A_true)
    B = np.array([[2.0, 0, 5], [0, 2.0, 7]])
    C = bz.compose(B, A_true)                                          # B first, then A_true
    p = np.array([[3.0, 4.0]])
    assert np.allclose(bz.apply_affine(C, p), bz.apply_affine(A_true, bz.apply_affine(B, p)))


# ------------------------------------------------------------------ tags and leaders on a synthetic page
def _synthetic_page(tmp_path):
    doc = pymupdf.open()
    pg = doc.new_page(width=800, height=600)
    # zone A (blue 0.5) with a tag printed inside; zone B (orange 0.5) reached by a leader from a red tag box; zone C (cyan) without any tag
    pg.draw_rect(pymupdf.Rect(50, 100, 250, 300), color=(1, 0, 0), fill=(0, 112 / 255, 192 / 255), fill_opacity=0.5)
    pg.insert_text((80, 200), "AHU-B-01-INSIDE-01", fontsize=9)
    pg.draw_rect(pymupdf.Rect(300, 100, 500, 300), color=(1, 0, 0), fill=(237 / 255, 125 / 255, 49 / 255), fill_opacity=0.5)
    pg.draw_rect(pymupdf.Rect(600, 400, 760, 430), color=(1, 0, 0), fill=(1, 1, 1))
    pg.insert_text((610, 420), "AHU-B-01-LEADER-01", fontsize=9)
    pg.draw_line((600, 415), (400, 200), color=(1, 0, 0), width=1)
    pg.draw_rect(pymupdf.Rect(300, 350, 500, 450), color=(1, 0, 0), fill=(0, 1, 1), fill_opacity=0.5)
    # legend swatch with label
    pg.draw_rect(pymupdf.Rect(600, 40, 640, 55), color=(0, 0, 0), fill=(0, 1, 1), fill_opacity=0.5)
    pg.insert_text((650, 52), "Recirculation", fontsize=9)
    pg.insert_text((60, 30), "AHU ZONING", fontsize=14)
    return pg


def test_vector_zones_tags_and_leaders(tmp_path):
    pg = _synthetic_page(tmp_path)
    R = pg.rotation_matrix
    drawings = pg.get_drawings()
    lines = bz.page_lines(pg, R)
    clip = pymupdf.Rect(0, 80, 800, 480)
    zones, used, swatches = bz.vector_zones(pg, R, drawings, lines, clip)
    assert len(zones) == 3
    assert len(swatches) == 1 and swatches[0]["label"] == "Recirculation"
    assert {z["colour"] for z in zones} == {"#0070C0", "#ED7D31", "#00FFFF"}
    cyan = [z for z in zones if z["colour"] == "#00FFFF"][0]
    assert cyan["legend"][0] == "Recirculation"
    tags = bz.page_tags(pg, R)
    assert [t["text"] for t in tags] == ["AHU-B-01-INSIDE-01", "AHU-B-01-LEADER-01"]
    bz.tag_boxes(drawings, tags, R)
    comps = bz.leader_components(drawings, R, set(used))
    notes = bz.assign_tags(zones, tags, comps)
    by = {z["colour"]: z for z in zones}
    assert [(t["text"], m.split(" ")[0]) for t, m, c in by["#0070C0"]["tags"]] == [("AHU-B-01-INSIDE-01", "tag")]
    assert [t["text"] for t, m, c in by["#ED7D31"]["tags"]] == ["AHU-B-01-LEADER-01"]
    assert "leader" in by["#ED7D31"]["tags"][0][1]
    assert by["#00FFFF"]["tags"] == []                                  # unknown stays blank
    assert notes == []


def test_nearest_tag_is_low_confidence():
    zones = [{"poly_pt": box(0, 0, 100, 100)}]
    tags = [{"text": "AHU-X-01", "bbox": (120, 40, 160, 50), "box": (120, 40, 160, 50)}]
    notes = bz.assign_tags(zones, tags, [])
    assert zones[0]["tags"][0][2] == "low" and notes == []
    far = [{"text": "AHU-Y-01", "bbox": (500, 40, 540, 50), "box": (500, 40, 540, 50)}]
    zones2 = [{"poly_pt": box(0, 0, 100, 100)}]
    notes = bz.assign_tags(zones2, far, [])
    assert zones2[0]["tags"] == [] and "not linked" in notes[0]


# ------------------------------------------------------------------ registration on synthetic drawings
class _FakeTender:
    """Stands in for Tender: a synthetic plan, SIFT features computed on it, page view = px / TENDER_SC, 88.19 mm per pt."""

    def __init__(self, img):
        self._img = img
        kp, des = cv2.SIFT_create().detectAndCompute(img, None)
        self._pts = np.float32([k.pt for k in kp])
        self._des = des
        self.A_pv_mm = np.array([[88.19, 0, 0], [0, -88.19, 200000.0]])
        self.mm_per_px = 88.19 / bz.TENDER_SC

    def features(self):
        return self._pts, self._des

    def render_gray(self):
        return self._img


def _plan(seed=5, w=2400, h=1700):
    rng = np.random.default_rng(seed)
    img = np.full((h, w), 255, np.uint8)
    for _ in range(260):                                             # walls, rooms, columns
        x, y = int(rng.integers(0, w - 200)), int(rng.integers(0, h - 200))
        ww, hh = int(rng.integers(20, 200)), int(rng.integers(20, 160))
        cv2.rectangle(img, (x, y), (x + ww, y + hh), int(rng.integers(0, 90)), int(rng.integers(1, 4)))
    for _ in range(120):
        c = (int(rng.integers(0, w)), int(rng.integers(0, h)))
        cv2.circle(img, c, int(rng.integers(5, 25)), 0, 2)
    for _ in range(150):
        cv2.line(img, (int(rng.integers(0, w)), int(rng.integers(0, h))), (int(rng.integers(0, w)), int(rng.integers(0, h))), 60, 1)
    return img


def test_register_page_recovers_known_transform_and_rejects_noise():
    plan = _plan()
    tender = _FakeTender(plan)
    # keyplan = a crop of the plan at another scale and rotation: tender px = M @ keyplan px
    ang, s = math.radians(4.0), 1.25
    M_true = np.array([[s * math.cos(ang), -s * math.sin(ang), 700.0], [s * math.sin(ang), s * math.cos(ang), 400.0]])
    Minv = np.linalg.inv(np.vstack([M_true, [0, 0, 1]]))[:2]
    book = cv2.warpAffine(plan, Minv, (1000, 700), flags=cv2.INTER_AREA, borderValue=255)
    res = bz.register_page(tender, book)
    assert res["ok"], res.get("reason")
    assert res["rms_mm"] < 150
    assert np.allclose(res["M"], M_true, atol=1.5, rtol=0.01)
    assert res["ncc"] > res["ncc_shift_max"] * bz.NCC_RATIO
    # an unrelated drawing does not register
    other = _plan(seed=99, w=1000, h=700)
    bad = bz.register_page(tender, other)
    assert not bad["ok"] and bad.get("reason")


def test_verify_fit_peaks_at_true_fit():
    plan = _plan(seed=8)
    book = plan[200:900, 300:1300].copy()
    M = np.array([[1.0, 0, 300.0], [0, 1.0, 200.0]])
    n0, nsh = bz.verify_fit(plan, book, M)
    assert n0 > 0.6 and nsh < 0.5
    n1, _ = bz.verify_fit(plan, book, np.array([[1.0, 0, 340.0], [0, 1.0, 245.0]]))
    assert n1 < n0 - 0.3


def test_residual_limit_is_one_and_a_half_metres():
    assert bz.MAX_RESIDUAL_MM == 1500.0


# ------------------------------------------------------------------ real page (skipped without sources)
SRC = ROOT / "data/source/Short Version DD Booklet/IGH_DD Booklet_AW_R2_30 June 2025 _AH 20250707.pdf"


@pytest.mark.skipif(not (SRC.exists() and (ROOT / "data/source/Basis/Arch Combined Tender .pdf").exists()
                         and (ROOT / "out/registers/room_polygons_L07.csv").exists()), reason="sources not present")
def test_real_page_l07_registers_and_agrees_between_pages(tmp_path, monkeypatch):
    infos = [i for i in bz.discover_pages(ROOT, ["L07"]) if i["page"] in (9, 24)]
    assert len(infos) == 2
    tender = bz.Tender("L07", ROOT)
    assert tender.A_rms < 20                                        # page view -> building mm affine of the room register
    res = [bz.process_page(ROOT, i, tender, None) for i in infos]
    for r in res:
        assert r["status"] == "registered", r.get("reason")
        assert r["reg"]["rms_mm"] < bz.MAX_RESIDUAL_MM
        row = r["rows"][0]
        assert row["ahu_tag_as_printed"] == "AHU-B-07-ADMIN-01"
        assert set(row) == set(bz.CSV_FIELDS)
        assert 600 < float(row["area_m2"]) < 700
    # two pages of one booklet show the same plan: their zones coincide in building mm
    a, b = res[0]["zones"][0]["poly_mm"], res[1]["zones"][0]["poly_mm"]
    assert a.intersection(b).area / a.union(b).area > 0.97
