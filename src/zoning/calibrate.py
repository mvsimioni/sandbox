"""Per-sheet affine transform from page-view pt to building mm, from the grid bubbles.

The transform is a general 2-D affine map (six parameters) fitted by least
squares.  Each lettered bubble fixes the building Y of its grid line, each
numbered bubble fixes X.  A single bubble gives one equation only, so the
rotation is pinned by the extra constraint that the bubbles of the top row all
lie on one building Y and those of the left column on one building X (each
with a free common value).  Bubbles that were drawn offset along their grid
line (R and P on the left, some shifted sheets) are used for the label equation
only, not for the common-line constraint.

Inputs reused from the registers: ``grid_model.json`` (grid label to building mm),
``sheet_calibration_plans.csv`` (scale and rotation, cross-check) and
``grid_observations_plans.csv`` (bubble centres of the background sheets).
The sample and App D pages are measured here by :func:`detect_bubbles`.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from .. import common

LABEL_RE = re.compile(r"^(?:[A-HJ-NP-SU-W]|\d{1,2})$")
BUBBLE_DIAMETER_PT = (26.5, 30.5)
TOP_STRIP_Y = (30.0, 80.0)
LEFT_STRIP_X = (50.0, 115.0)
OFFSET_TOLERANCE_PT = 6.0  # bubble centre further than this from the strip median is "offset"


# --------------------------------------------------------------------------- #
# Grid model
# --------------------------------------------------------------------------- #
def load_grid_model(path: Optional[Path] = None) -> Dict[str, Dict[str, float]]:
    """Return ``{"X": {label: mm}, "Y": {label: mm}}`` from ``grid_model.json``."""
    path = Path(path) if path else common.PATHS["registers"] / "grid_model.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "X": {g["label"]: float(g["offset_mm"]) for g in data["x_axis"]["grids"]},
        "Y": {g["label"]: float(g["offset_mm"]) for g in data["y_axis"]["grids"]},
    }


@dataclass
class Bubble:
    label: str
    axis: str  # "numbered" (fixes X) or "lettered" (fixes Y)
    x: float   # page-view pt
    y: float
    source: str = ""
    offset: bool = False


# --------------------------------------------------------------------------- #
# Bubble detection (sample page, App D page, check of the background page)
# --------------------------------------------------------------------------- #
def detect_bubbles(page) -> List[Bubble]:
    """Detect grid bubbles on a page: 16 pt labels in the top and left strips, snapped to the circle.

    Returns bubbles in page-view pt.  The circle centre is used when a vector circle of
    26.5-30.5 pt diameter lies within 8 pt of the label centre; otherwise the label centre.
    """
    import pymupdf

    rot = page.rotation_matrix
    circles: List[Tuple[float, float]] = []
    for d in page.get_drawings():
        r = d["rect"]
        w, h = r.width, r.height
        if BUBBLE_DIAMETER_PT[0] <= w <= BUBBLE_DIAMETER_PT[1] and abs(w - h) < 1.5:
            if sum(1 for it in d["items"] if it[0] == "c") >= 4:
                c = pymupdf.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2) * rot
                circles.append((c.x, c.y))
    # page content only: drawings never include annotations
    labels: List[Tuple[str, float, float]] = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                txt = span["text"].strip()
                if not LABEL_RE.match(txt) or not (13.5 <= span["size"] <= 17.5):
                    continue
                b = pymupdf.Rect(span["bbox"])
                # span bbox is in unrotated coordinates; transform the centre
                c = pymupdf.Point((b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2) * rot
                labels.append((txt, c.x, c.y))
    out: List[Bubble] = []
    for txt, cx, cy in labels:
        is_top = TOP_STRIP_Y[0] <= cy <= TOP_STRIP_Y[1] and txt.isdigit()
        is_left = LEFT_STRIP_X[0] <= cx <= LEFT_STRIP_X[1] and txt.isalpha()
        if not (is_top or is_left):
            continue
        best = None
        for (px, py) in circles:
            dd = math.hypot(px - cx, py - cy)
            if dd <= 8.0 and (best is None or dd < best[0]):
                best = (dd, px, py)
        x, y, src = (best[1], best[2], "circle centre") if best else (cx, cy, "label centre")
        out.append(Bubble(txt, "numbered" if is_top else "lettered", x, y, src))
    _flag_offsets(out)
    return out


def _flag_offsets(bubbles: Sequence[Bubble]) -> None:
    """Mark bubbles that sit off their strip (R and P on the left; shifted bubbles)."""
    for axis, coord in (("numbered", "y"), ("lettered", "x")):
        group = [b for b in bubbles if b.axis == axis]
        if not group:
            continue
        med = float(np.median([getattr(b, coord) for b in group]))
        for b in group:
            b.offset = abs(getattr(b, coord) - med) > OFFSET_TOLERANCE_PT


def bubbles_from_register(csv_path: Path, page: int, x_col: str = "x_pv", y_col: str = "y_pv",
                          label_col: str = "label", axis_col: str = "axis") -> List[Bubble]:
    """Read the bubble centres of one page from a grid-observation register."""
    out: List[Bubble] = []
    for r in common.read_csv_dicts(csv_path):
        if int(r["page"]) != page:
            continue
        out.append(Bubble(r[label_col], r[axis_col], float(r[x_col]), float(r[y_col]),
                          source=Path(csv_path).name))
    _flag_offsets(out)
    return out


# --------------------------------------------------------------------------- #
# Affine fit
# --------------------------------------------------------------------------- #
@dataclass
class Calibration:
    """Affine map page-view pt -> building mm, ``[X, Y] = A @ [x, y, 1]``."""

    A: np.ndarray                      # 2 x 3
    residuals_mm: Dict[str, float]     # per label equation, signed
    rms_mm: float
    max_mm: float
    n_equations: int
    scale_x_mm_per_pt: float = 0.0
    scale_y_mm_per_pt: float = 0.0
    rotation_deg: float = 0.0
    source: str = ""
    notes: List[str] = field(default_factory=list)

    def to_building(self, x, y):
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        return (self.A[0, 0] * x + self.A[0, 1] * y + self.A[0, 2],
                self.A[1, 0] * x + self.A[1, 1] * y + self.A[1, 2])

    def to_page(self, X, Y):
        X = np.asarray(X, dtype=float)
        Y = np.asarray(Y, dtype=float)
        M = self.A[:, :2]
        inv = np.linalg.inv(M)
        d = np.stack([X - self.A[0, 2], Y - self.A[1, 2]])
        res = inv @ d.reshape(2, -1)
        return res[0].reshape(X.shape), res[1].reshape(X.shape)

    def area_scale_mm2_per_pt2(self) -> float:
        return float(abs(np.linalg.det(self.A[:, :2])))

    def as_dict(self) -> dict:
        return {"A": self.A.tolist(), "rms_mm": self.rms_mm, "max_mm": self.max_mm,
                "n_equations": self.n_equations, "scale_x_mm_per_pt": self.scale_x_mm_per_pt,
                "scale_y_mm_per_pt": self.scale_y_mm_per_pt, "rotation_deg": self.rotation_deg,
                "source": self.source}


def fit_affine(bubbles: Sequence[Bubble], grid: Dict[str, Dict[str, float]], source: str = "") -> Calibration:
    """Least-squares affine fit page-view pt -> building mm from grid bubbles.

    Unknowns: a0..a2 (X), b0..b2 (Y), Y_top (common building Y of the top row), X_left
    (common building X of the left column).  Needs at least two numbered and two lettered bubbles.
    """
    rows: List[List[float]] = []
    rhs: List[float] = []
    tags: List[str] = []
    num = [b for b in bubbles if b.axis == "numbered" and b.label in grid["X"]]
    let = [b for b in bubbles if b.axis == "lettered" and b.label in grid["Y"]]
    if len(num) < 2 or len(let) < 2:
        raise ValueError("need at least two numbered and two lettered bubbles to calibrate")
    for b in num:
        rows.append([b.x, b.y, 1, 0, 0, 0, 0, 0]); rhs.append(grid["X"][b.label]); tags.append("X:" + b.label)
    for b in let:
        rows.append([0, 0, 0, b.x, b.y, 1, 0, 0]); rhs.append(grid["Y"][b.label]); tags.append("Y:" + b.label)
    n_label_rows = len(rows)
    for b in num:
        if not b.offset:
            rows.append([0, 0, 0, b.x, b.y, 1, -1, 0]); rhs.append(0.0)
    for b in let:
        if not b.offset:
            rows.append([b.x, b.y, 1, 0, 0, 0, 0, -1]); rhs.append(0.0)
    A_ = np.asarray(rows, float)
    y_ = np.asarray(rhs, float)
    sol, *_ = np.linalg.lstsq(A_, y_, rcond=None)
    A = np.array([[sol[0], sol[1], sol[2]], [sol[3], sol[4], sol[5]]])
    res = (A_[:n_label_rows] @ sol) - y_[:n_label_rows]
    residuals = {t: float(r) for t, r in zip(tags, res)}
    # scale / rotation from the linear part (X right, Y up; page y is down)
    sx = math.hypot(A[0, 0], A[1, 0])
    sy = math.hypot(A[0, 1], A[1, 1])
    rot = math.degrees(math.atan2(-A[1, 0], A[0, 0]))  # building-X axis angle in page view
    return Calibration(A=A, residuals_mm=residuals, rms_mm=float(np.sqrt(np.mean(res ** 2))),
                       max_mm=float(np.max(np.abs(res))), n_equations=n_label_rows,
                       scale_x_mm_per_pt=sx, scale_y_mm_per_pt=sy, rotation_deg=rot, source=source)


def calibrate_page(page, page_label: str = "", register_csv: Optional[Path] = None, page_number: Optional[int] = None,
                   grid: Optional[Dict[str, Dict[str, float]]] = None) -> Tuple[Calibration, List[Bubble]]:
    """Calibrate a page from a register of bubble centres when given, else from detected bubbles."""
    grid = grid or load_grid_model()
    if register_csv is not None:
        bub = bubbles_from_register(register_csv, int(page_number))
        src = "%s page %s" % (Path(register_csv).name, page_number)
    else:
        bub = detect_bubbles(page)
        src = "detected on %s" % page_label
    return fit_affine(bub, grid, source=src), bub


# --------------------------------------------------------------------------- #
# Frame comparison between two pages
# --------------------------------------------------------------------------- #
def compare_frames(b1: Sequence[Bubble], b2: Sequence[Bubble]) -> dict:
    """Compare the page-view positions of the bubbles common to two pages.

    Returns the mean offset (b2 - b1) in pt, the residual after removing it, and the per-axis
    breakdown, using only bubbles that are not offset on either page.
    """
    m1 = {(b.axis, b.label): b for b in b1}
    dx, dy, names = [], [], []
    for b in b2:
        o = m1.get((b.axis, b.label))
        if o is None or o.offset or b.offset:
            continue
        dx.append(b.x - o.x); dy.append(b.y - o.y); names.append(b.label)
    dx, dy = np.asarray(dx), np.asarray(dy)
    if len(dx) == 0:
        return {"n": 0}
    out = {"n": int(len(dx)),
           "mean_dx_pt": float(dx.mean()), "mean_dy_pt": float(dy.mean()),
           "rms_after_offset_pt": float(np.sqrt(np.mean((dx - dx.mean()) ** 2 + (dy - dy.mean()) ** 2))),
           "max_abs_dx_pt": float(np.abs(dx).max()), "max_abs_dy_pt": float(np.abs(dy).max())}
    num = np.array([n.isdigit() for n in names])
    # a numbered bubble pins x only (its y is its strip), a lettered one pins y only
    if num.any():
        out["numbered_mean_dx_pt"] = float(dx[num].mean()); out["numbered_max_abs_dx_pt"] = float(np.abs(dx[num]).max())
    if (~num).any():
        out["lettered_mean_dy_pt"] = float(dy[~num].mean()); out["lettered_max_abs_dy_pt"] = float(np.abs(dy[~num]).max())
    return out


def transfer_points(pts_xy: np.ndarray, src: Calibration, dst: Calibration) -> np.ndarray:
    """Map page-view points of the source page to page-view points of the destination page through building mm."""
    pts = np.asarray(pts_xy, dtype=float).reshape(-1, 2)
    X, Y = src.to_building(pts[:, 0], pts[:, 1])
    x, y = dst.to_page(X, Y)
    return np.stack([x, y], axis=1)


def grid_location(X_mm: float, Y_mm: float, grid: Optional[Dict[str, Dict[str, float]]] = None) -> str:
    """Express a building point as ``<numbered grid> +dx mm / <lettered grid> +dy mm`` (nearest grid below/left)."""
    grid = grid or load_grid_model()
    xs = sorted(grid["X"].items(), key=lambda kv: kv[1])
    ys = sorted(grid["Y"].items(), key=lambda kv: kv[1])
    xl = max((kv for kv in xs if kv[1] <= X_mm), default=xs[0], key=lambda kv: kv[1])
    yl = max((kv for kv in ys if kv[1] <= Y_mm), default=ys[0], key=lambda kv: kv[1])
    return "%s%+.0f mm / %s%+.0f mm" % (xl[0], X_mm - xl[1], yl[0], Y_mm - yl[1])
