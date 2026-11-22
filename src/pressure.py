"""Room pressure-offset method for the AIGH IP ACMV pipeline (the engineer's own method).

The method answers one question per pressurised room: *is the design offset
airflow (the airtightness allowance of 2.5 m3/h per m2 of internal surface at
50 Pa) enough to hold the set pressure against the real leakage paths?*

Units throughout: airflow ``Q`` in m3/h (``lps`` converts to L/s), pressure
difference ``dP`` in Pa, areas in m2.  Gap areas in the door-gap constants are
in cm2 and converted at the point of use.

Model
-----
* Every space has an internal surface (``2 * floor + perimeter * height``)
  whose whole envelope leaks to a reference node (``REF`` at 0 Pa by default):
  the cautious side, because the leakage then always works against the offset.
  When ``leaks_to`` names another space of the suite the same flow is booked
  on that space too, so mass is conserved across the suite.
* Links between spaces carry either a gap (door undercut, louvre) whose flow is
  ``cd * A * sqrt(2 |dP| / rho) * 3600`` or a fixed flow (a sealed shaft door
  with a known transfer).  Air flows from the higher to the lower pressure.
* A negative room needs extra exhaust ``air_in - air_out``; a positive room
  needs extra supply ``air_out - air_in``.  ``design_offset = limit * surface``
  and ``ratio = design_offset / offset_needed``: FAIL below 1.0, VERIFY from
  1.0 to 1.2, PASS above.  A room whose leakage runs the wrong way (a negative
  room with more leakage out than in) is *reversed*: it needs the other kind
  of offset, is never better than VERIFY, and its row says so.
* A free-pressure space (no set pressure, its own fixed supply/exhaust) settles
  where its balance closes; the suite solves those by bisection, 60 steps each,
  swept 60 times (``solve``), or to machine tolerance (``solve_exact``).  A
  free space whose balance does not close is reported UNSOLVED with its residual.
* Suite totals: when every controlled room needs the same kind of offset the
  internal links cancel and the suite offset is the sum of the room offsets;
  a suite mixing supply and exhaust rooms reports the two totals separately
  (status MIXED) because two fans cannot share one margin.
* Sensitivity per room: ``n`` at 0.50 and 0.75, every gap doubled, and the
  pressure the room would reach at the design offset with no control.  A room
  that fails only in a sensitivity case is *flagged*, not failed.

``python -m src.pressure --selftest`` runs the known-answer tests and exits
non-zero on any miss.  Nothing in this module touches the network.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:  # package import (python -m src.pressure, tests)
    from src.common import load_config, write_csv_dicts
except ModuleNotFoundError:  # run as a plain script from inside src/
    from common import load_config, write_csv_dicts  # type: ignore[no-redef]

# --------------------------------------------------------------------------- #
# Constants (defaults; ``config/project.yaml`` ``pressure_method`` overrides)
# --------------------------------------------------------------------------- #
REF = "REF"
"""Name of the reference space (ambient / corridor datum) at 0 Pa."""

AIRTIGHTNESS_LIMIT = 2.5
"""Envelope leakage allowance, m3/h per m2 of internal surface at 50 Pa."""
TIGHTER_ROOM_LIMIT = 1.0
"""Tighter construction allowance, m3/h per m2 at 50 Pa."""
FLOW_EXPONENT = 0.65
FLOW_EXPONENT_RANGE: Tuple[float, float] = (0.50, 0.75)
DISCHARGE_COEFFICIENT = 0.6
AIR_DENSITY = 1.2
REFERENCE_PA = 50.0
"""Pressure at which the airtightness allowance is quoted."""
APPLIES_AT_OR_BEYOND_PA = 5.0
"""Below this set pressure the method is not meaningful; rooms are noted."""
RATIO_FAIL_BELOW = 1.0
RATIO_VERIFY_BELOW = 1.2

SEALED_GAP_CM2: Dict[str, float] = {
    "hinged_sealed": 15.0,
    "sliding_sealed": 10.0,
    "bed_width_sealed": 20.0,
    "shaft_access_sealed": 10.0,
}
"""Provisional equivalent leakage areas of sealed doors, cm2."""
UNSEALED_GAP_MM: Dict[str, float] = {"sides_top": 3.0, "bottom": 10.0}

BISECTION_STEPS = 60
SWEEPS = 60
PRESSURE_BRACKET_PA = 1000.0
"""Half-width of the pressure bracket used by the bisections."""
RESIDUAL_TOL_M3H = 0.05
"""A free space whose balance residual exceeds this is reported UNSOLVED."""
OFFSET_EPS_M3H = 1e-9
"""Offsets smaller than this in magnitude count as zero."""

STATUS_FAIL = "FAIL"
STATUS_VERIFY = "VERIFY"
STATUS_PASS = "PASS"
STATUS_FREE = "FREE"
STATUS_UNSOLVED = "UNSOLVED"
"""A free space whose balance did not close inside the pressure bracket."""
STATUS_MIXED = "MIXED"
"""Suite total of a suite with both supply and exhaust rooms (no single margin)."""
BAD_STATUSES = frozenset({STATUS_FAIL, STATUS_UNSOLVED})
"""Statuses that make the command line exit non-zero."""

KIND_EXHAUST = "exhaust"
KIND_SUPPLY = "supply"
KIND_FREE = "free"

ROW_FIELDS: Tuple[str, ...] = (
    "suite", "space", "set_pressure_pa", "solved_pressure_pa", "leaks_to", "surface_m2", "limit_m3h_m2",
    "envelope_in_m3h", "envelope_out_m3h", "gap_in_m3h", "gap_out_m3h", "fixed_in_m3h", "fixed_out_m3h",
    "air_in_m3h", "air_out_m3h", "offset_kind", "offset_needed_m3h", "offset_needed_lps",
    "design_offset_m3h", "design_offset_lps", "tighter_offset_m3h", "ratio", "status",
    "ratio_n050", "ratio_n075", "ratio_gaps_x2", "uncontrolled_pa", "flagged", "notes",
)
"""Column order of :func:`to_rows` (one row per space, then one suite total row)."""


def _config_section() -> Dict[str, Any]:
    """The ``pressure_method`` block of the project configuration (empty when there is none).

    A missing configuration file leaves the built-in defaults in place; a
    configuration whose ``pressure_method`` block is not a mapping is an error.
    """
    try:
        config = load_config()
    except FileNotFoundError:
        return {}
    section = config.get("pressure_method")
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ValueError("config/project.yaml: pressure_method must be a mapping")
    return section


def _apply_config() -> None:
    """Override the module constants from ``config/project.yaml`` when present."""
    global AIRTIGHTNESS_LIMIT, TIGHTER_ROOM_LIMIT, FLOW_EXPONENT, FLOW_EXPONENT_RANGE  # noqa: PLW0603
    global DISCHARGE_COEFFICIENT, AIR_DENSITY, APPLIES_AT_OR_BEYOND_PA, RATIO_FAIL_BELOW, RATIO_VERIFY_BELOW  # noqa: PLW0603
    cfg = _config_section()
    AIRTIGHTNESS_LIMIT = float(cfg.get("airtightness_m3h_per_m2_at_50pa", AIRTIGHTNESS_LIMIT))
    TIGHTER_ROOM_LIMIT = float(cfg.get("tighter_room_m3h_per_m2_at_50pa", TIGHTER_ROOM_LIMIT))
    exponent = cfg.get("flow_exponent", {}) or {}
    if isinstance(exponent, dict):
        FLOW_EXPONENT = float(exponent.get("n", FLOW_EXPONENT))
        rng = exponent.get("range")
        if isinstance(rng, (list, tuple)) and len(rng) == 2:
            FLOW_EXPONENT_RANGE = (float(rng[0]), float(rng[1]))
    DISCHARGE_COEFFICIENT = float(cfg.get("discharge_coefficient", DISCHARGE_COEFFICIENT))
    AIR_DENSITY = float(cfg.get("air_density_kg_m3", AIR_DENSITY))
    APPLIES_AT_OR_BEYOND_PA = float(cfg.get("applies_at_or_beyond_pa", APPLIES_AT_OR_BEYOND_PA))
    ratio = cfg.get("ratio", {}) or {}
    if isinstance(ratio, dict):
        RATIO_FAIL_BELOW = float(ratio.get("fail_below", RATIO_FAIL_BELOW))
        RATIO_VERIFY_BELOW = float(ratio.get("verify_below", RATIO_VERIFY_BELOW))
    gaps = cfg.get("door_gap_cm2", {}) or {}
    if isinstance(gaps, dict):
        SEALED_GAP_CM2.update({str(k): float(v) for k, v in gaps.items()})
    unsealed = cfg.get("unsealed_gap_mm", {}) or {}
    if isinstance(unsealed, dict):
        UNSEALED_GAP_MM.update({str(k): float(v) for k, v in unsealed.items()})


_apply_config()


# --------------------------------------------------------------------------- #
# Elementary functions
# --------------------------------------------------------------------------- #
def lps(m3h: float) -> float:
    """Convert m3/h to L/s."""
    return m3h / 3.6


def m3h(lps_value: float) -> float:
    """Convert L/s to m3/h."""
    return lps_value * 3.6


def cm2_to_m2(cm2: float) -> float:
    """Convert cm2 to m2."""
    return cm2 / 10_000.0


def internal_surface(floor_area: float, perimeter: float, height: float) -> float:
    """Internal surface of a room, m2: floor + ceiling + walls (``2 * floor + perimeter * height``)."""
    return 2.0 * floor_area + perimeter * height


def envelope_leak(surface: float, dp: float, n: float = FLOW_EXPONENT, limit: float = AIRTIGHTNESS_LIMIT) -> float:
    """Envelope leakage magnitude, m3/h, at ``|dp|`` Pa for ``surface`` m2 at ``limit`` m3/h per m2 at 50 Pa.

    ``limit * surface * (|dp| / 50) ** n``; zero when the pressure difference is zero.
    """
    magnitude = abs(dp)
    if magnitude <= 0.0 or surface <= 0.0:
        return 0.0
    return limit * surface * (magnitude / REFERENCE_PA) ** n


def limit_at(dp: float, n: float = FLOW_EXPONENT, limit: float = AIRTIGHTNESS_LIMIT) -> float:
    """The airtightness allowance converted to ``|dp|`` Pa, m3/h per m2 (``limit * (|dp|/50) ** n``)."""
    return envelope_leak(1.0, dp, n=n, limit=limit)


def gap_flow(area_m2: float, dp: float, cd: float = DISCHARGE_COEFFICIENT, rho: float = AIR_DENSITY) -> float:
    """Orifice flow magnitude through a gap, m3/h: ``cd * A * sqrt(2 |dp| / rho) * 3600``."""
    magnitude = abs(dp)
    if magnitude <= 0.0 or area_m2 <= 0.0:
        return 0.0
    return cd * area_m2 * math.sqrt(2.0 * magnitude / rho) * 3600.0


def unsealed_door_gap_area(width_m: float, height_m: float, side_top_mm: float = UNSEALED_GAP_MM["sides_top"],
                           bottom_mm: float = UNSEALED_GAP_MM["bottom"]) -> float:
    """Crack area of an unsealed door leaf, m2: two sides and the top at ``side_top_mm``, the bottom at ``bottom_mm``.

    A 0.9 x 2.1 m leaf gives about 240 cm2; a small 0.6 x 1.8 m access door about 190 cm2.
    """
    return (2.0 * height_m + width_m) * side_top_mm / 1000.0 + width_m * bottom_mm / 1000.0


def sealed_gap_area(kind: str) -> float:
    """Provisional leakage area of a sealed door by kind (``hinged``, ``sliding``, ``bed_width``, ``shaft_access``), m2."""
    key = kind if kind in SEALED_GAP_CM2 else f"{kind}_sealed"
    if key not in SEALED_GAP_CM2:
        raise KeyError(f"unknown sealed door kind '{kind}'; known: {sorted(SEALED_GAP_CM2)}")
    return cm2_to_m2(SEALED_GAP_CM2[key])


def tighter_room_offset(surface: float, limit: float = TIGHTER_ROOM_LIMIT) -> float:
    """Design offset for tighter construction, m3/h (``1.0 * surface`` by default)."""
    return limit * surface


def design_offset(surface: float, limit: float = AIRTIGHTNESS_LIMIT) -> float:
    """Design offset airflow, m3/h: the airtightness allowance over the internal surface (``limit * surface``)."""
    return limit * surface


def classify_ratio(ratio: Optional[float]) -> str:
    """Status for a ratio: FAIL below 1.0, VERIFY from 1.0 to 1.2, PASS above (``None`` is FREE)."""
    if ratio is None:
        return STATUS_FREE
    if ratio < RATIO_FAIL_BELOW:
        return STATUS_FAIL
    if ratio < RATIO_VERIFY_BELOW:
        return STATUS_VERIFY
    return STATUS_PASS


def _ratio(design: float, needed: float) -> float:
    """``design / needed``; infinite when nothing is needed (the margin is unlimited)."""
    if needed <= OFFSET_EPS_M3H:
        return math.inf
    return design / needed


def _bisect(func: Callable[[float], float], lo: float, hi: float, steps: int) -> Optional[float]:
    """Root of a monotone ``func`` on ``[lo, hi]`` by ``steps`` bisections; ``None`` without a sign change."""
    f_lo, f_hi = func(lo), func(hi)
    if f_lo == 0.0:
        return lo
    if f_hi == 0.0:
        return hi
    if (f_lo < 0.0) == (f_hi < 0.0):
        return None
    for _ in range(steps):
        mid = 0.5 * (lo + hi)
        f_mid = func(mid)
        if f_mid == 0.0:
            return mid
        if (f_mid < 0.0) == (f_lo < 0.0):
            lo, f_lo = mid, f_mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# --------------------------------------------------------------------------- #
# Suite model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Space:
    """A room of the suite.

    ``set_pressure_pa`` is the controlled pressure relative to ``REF`` or
    ``None`` for a free-pressure space.  ``supply_m3h`` / ``exhaust_m3h`` are
    fixed mechanical flows (they settle a free space; for a controlled space
    they are reported beside the offset).  ``limit`` is the airtightness
    allowance of this space and ``n`` its flow exponent.
    """

    name: str
    set_pressure_pa: Optional[float]
    surface_m2: float
    leaks_to: str = REF
    supply_m3h: float = 0.0
    exhaust_m3h: float = 0.0
    limit: float = AIRTIGHTNESS_LIMIT
    n: float = FLOW_EXPONENT

    @property
    def free(self) -> bool:
        """True when the space has no set pressure."""
        return self.set_pressure_pa is None


@dataclass(frozen=True)
class Link:
    """An air path between two nodes (spaces, externals or ``REF``).

    Either ``gap_area_m2`` (orifice flow from the higher to the lower pressure)
    or ``fixed_flow_m3h`` (a known transfer; positive flows from ``a`` to ``b``,
    negative from ``b`` to ``a``) must be given.  ``kind`` is a label for the row.
    """

    a: str
    b: str
    gap_area_m2: Optional[float] = None
    fixed_flow_m3h: Optional[float] = None
    kind: str = "door"

    def __post_init__(self) -> None:
        if (self.gap_area_m2 is None) == (self.fixed_flow_m3h is None):
            raise ValueError(f"link {self.a}-{self.b}: give exactly one of gap_area_m2 or fixed_flow_m3h")
        if self.a == self.b:
            raise ValueError(f"link {self.a}-{self.b}: both ends are the same node")
        if self.gap_area_m2 is not None and self.gap_area_m2 < 0.0:
            raise ValueError(f"link {self.a}-{self.b}: negative gap area {self.gap_area_m2}")

    def other(self, name: str) -> str:
        """The node at the far end from ``name``."""
        return self.b if name == self.a else self.a

    def touches(self, name: str) -> bool:
        """True when ``name`` is one end of the link."""
        return name in (self.a, self.b)


@dataclass
class SpaceResult:
    """Pressure balance of one space at solved pressures (all flows m3/h, magnitudes)."""

    name: str
    set_pressure_pa: Optional[float]
    pressure_pa: float
    leaks_to: str
    surface_m2: float
    limit: float
    n: float
    envelope_in: float = 0.0
    envelope_out: float = 0.0
    gap_in: float = 0.0
    gap_out: float = 0.0
    fixed_in: float = 0.0
    fixed_out: float = 0.0
    supply_m3h: float = 0.0
    exhaust_m3h: float = 0.0
    offset_kind: str = KIND_EXHAUST
    offset_needed: float = 0.0
    design_offset: float = 0.0
    tighter_offset: float = 0.0
    ratio: Optional[float] = None
    status: str = STATUS_FREE
    reversed: bool = False
    """True when the room needs the opposite kind of offset to its polarity (leakage runs the wrong way)."""
    ratio_n050: Optional[float] = None
    ratio_n075: Optional[float] = None
    ratio_gaps_x2: Optional[float] = None
    uncontrolled_pa: Optional[float] = None
    flagged: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def controlled(self) -> bool:
        """True for a room with a set pressure (its row carries an offset and a ratio)."""
        return self.set_pressure_pa is not None

    @property
    def air_in(self) -> float:
        """Total inward airflow through leakage paths (gaps, fixed transfers, envelope)."""
        return self.envelope_in + self.gap_in + self.fixed_in

    @property
    def air_out(self) -> float:
        """Total outward airflow through leakage paths."""
        return self.envelope_out + self.gap_out + self.fixed_out

    @property
    def signed_offset(self) -> float:
        """Offset as extra exhaust (positive) or extra supply (negative): ``air_in - air_out``."""
        return self.air_in - self.air_out

    @property
    def balance_residual(self) -> float:
        """``supply + air_in - exhaust - air_out``; zero for a free space at its solved pressure."""
        return self.supply_m3h + self.air_in - self.exhaust_m3h - self.air_out


@dataclass
class SuiteResult:
    """Results of a suite solve: per-space results plus suite totals.

    For a suite whose controlled rooms all need the same kind of offset the
    internal links cancel and ``offset_needed`` is the suite's fan offset with
    a ratio and a status.  For a mixed suite ``offset_needed`` is the sum of the
    exhaust and supply totals, ``ratio`` is ``None`` and the status is MIXED;
    the per-kind figures are in ``exhaust_needed`` / ``supply_needed``.
    """

    suite: str
    spaces: List[SpaceResult]
    pressures: Dict[str, float]
    offset_needed: float = 0.0
    design_offset: float = 0.0
    tighter_offset: float = 0.0
    ratio: Optional[float] = None
    status: str = STATUS_FREE
    offset_kind: str = ""
    exhaust_needed: float = 0.0
    exhaust_design: float = 0.0
    supply_needed: float = 0.0
    supply_design: float = 0.0
    flagged: bool = False
    sweeps: int = 0
    max_residual: float = 0.0
    notes: List[str] = field(default_factory=list)

    @property
    def worst_status(self) -> str:
        """FAIL or UNSOLVED when any space (or the total) carries it, else the suite status."""
        statuses = {r.status for r in self.spaces} | {self.status}
        for bad in (STATUS_FAIL, STATUS_UNSOLVED):
            if bad in statuses:
                return bad
        return self.status

    def space(self, name: str) -> SpaceResult:
        """The result for ``name`` (KeyError when absent)."""
        for item in self.spaces:
            if item.name == name:
                return item
        raise KeyError(name)

    def to_rows(self) -> List[Dict[str, Any]]:
        """Rows for a workbook tab (see :func:`to_rows`)."""
        return to_rows(self)


class Suite:
    """A group of spaces, links and externals solved together.

    ``externals`` are nodes outside the suite with a known pressure (a corridor
    at 0 Pa, a neighbouring room at -30 Pa).  ``REF`` is always present at
    ``reference_pa`` (0 Pa).
    """

    def __init__(self, name: str, spaces: Sequence[Space], links: Sequence[Link] = (),
                 externals: Optional[Dict[str, float]] = None, reference_pa: float = 0.0) -> None:
        self.name = name
        self.spaces: List[Space] = list(spaces)
        self.links: List[Link] = list(links)
        self.externals: Dict[str, float] = {REF: reference_pa, **(externals or {})}
        self._validate()

    # -- construction ------------------------------------------------------- #
    def _validate(self) -> None:
        """Reject duplicate or colliding names, unknown link ends and unknown ``leaks_to`` nodes."""
        names = [s.name for s in self.spaces]
        if len(set(names)) != len(names):
            raise ValueError(f"suite {self.name}: duplicate space names")
        clashes = sorted(set(names) & set(self.externals))
        if clashes:
            raise ValueError(f"suite {self.name}: space name(s) {clashes} collide with REF or an external node")
        known = set(names) | set(self.externals)
        for space in self.spaces:
            if space.leaks_to not in known:
                raise ValueError(f"space {space.name}: leaks_to '{space.leaks_to}' is not a known node")
            if space.leaks_to == space.name:
                raise ValueError(f"space {space.name}: cannot leak to itself")
            if space.surface_m2 < 0.0:
                raise ValueError(f"space {space.name}: negative surface")
        for link in self.links:
            for end in (link.a, link.b):
                if end not in known:
                    raise ValueError(f"link {link.a}-{link.b}: '{end}' is not a known node")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Suite":
        """Build a suite from a JSON-style mapping (``spaces``, ``links``, ``externals``, ``name``).

        Links may give ``gap_area_cm2`` instead of ``gap_area_m2`` or a sealed
        door ``sealed`` kind (``hinged``, ``sliding``, ``bed_width``, ``shaft_access``).
        """
        spaces = [Space(**_space_kwargs(item)) for item in data.get("spaces", [])]
        links = [Link(**_link_kwargs(item)) for item in data.get("links", [])]
        externals = {str(k): float(v) for k, v in (data.get("externals") or {}).items()}
        return cls(str(data.get("name", "suite")), spaces, links, externals, float(data.get("reference_pa", 0.0)))

    # -- pressures ---------------------------------------------------------- #
    def _space(self, name: str) -> Space:
        """The space named ``name``."""
        for space in self.spaces:
            if space.name == name:
                return space
        raise KeyError(name)

    def _initial_pressures(self) -> Dict[str, float]:
        """Known pressures; free spaces start at the mean of their set neighbours (or 0)."""
        pressures = dict(self.externals)
        for space in self.spaces:
            if not space.free:
                pressures[space.name] = float(space.set_pressure_pa)  # type: ignore[arg-type]
        for space in self.spaces:
            if space.free:
                neighbours = [pressures[l.other(space.name)] for l in self.links
                              if l.touches(space.name) and l.other(space.name) in pressures]
                pressures[space.name] = sum(neighbours) / len(neighbours) if neighbours else 0.0
        return pressures

    def _leaking_into(self, name: str) -> List[Space]:
        """Spaces of the suite whose envelope leaks to the node ``name``."""
        return [s for s in self.spaces if s.leaks_to == name and s.name != name]

    def _neighbours(self, space: Space) -> List[str]:
        """Every node whose pressure ``space`` can see: its sink, its links and the spaces leaking into it."""
        names = [space.leaks_to] + [l.other(space.name) for l in self.links if l.touches(space.name)]
        return names + [s.name for s in self._leaking_into(space.name)]

    def _book_envelope(self, res: SpaceResult, surface: float, dp: float, n: float, limit: float) -> None:
        """Add an envelope flow at ``dp`` (own minus other pressure) to ``res``: out when positive, in when negative."""
        env = envelope_leak(surface, dp, n=n, limit=limit)
        if dp < 0.0:
            res.envelope_in += env
        else:
            res.envelope_out += env

    def _flows(self, space: Space, pressures: Dict[str, float], n_override: Optional[float],
               gap_factor: float, limit_override: Optional[float] = None) -> SpaceResult:
        """Evaluate every leakage path of ``space`` at the given pressures.

        The envelope of ``space`` leaks to its ``leaks_to`` node; the envelopes
        of other spaces that leak *to* ``space`` are booked here as well, so the
        same flow appears on both rows and mass is conserved.
        """
        p_self = pressures[space.name]
        n = space.n if n_override is None else n_override
        limit = space.limit if limit_override is None else limit_override
        res = SpaceResult(space.name, space.set_pressure_pa, p_self, space.leaks_to, space.surface_m2, limit, n,
                          supply_m3h=space.supply_m3h, exhaust_m3h=space.exhaust_m3h)
        self._book_envelope(res, space.surface_m2, p_self - pressures[space.leaks_to], n, limit)
        for other in self._leaking_into(space.name):
            other_n = other.n if n_override is None else n_override
            other_limit = other.limit if limit_override is None else limit_override
            self._book_envelope(res, other.surface_m2, p_self - pressures[other.name], other_n, other_limit)
        for link in self.links:
            if not link.touches(space.name):
                continue
            p_other = pressures[link.other(space.name)]
            if link.gap_area_m2 is not None:
                flow = gap_flow(link.gap_area_m2 * gap_factor, p_self - p_other)
                if p_other > p_self:
                    res.gap_in += flow
                elif p_other < p_self:
                    res.gap_out += flow
            else:
                signed = float(link.fixed_flow_m3h or 0.0)
                into_self = signed if link.b == space.name else -signed
                if into_self > 0.0:
                    res.fixed_in += into_self
                else:
                    res.fixed_out += -into_self
        return res

    def _residual(self, space: Space, pressures: Dict[str, float], n_override: Optional[float],
                  gap_factor: float) -> float:
        """Balance residual of a free space: ``supply + in - exhaust - out`` (decreasing in its own pressure)."""
        return self._flows(space, pressures, n_override, gap_factor).balance_residual

    def _bracket(self, space: Space, pressures: Dict[str, float]) -> Tuple[float, float]:
        """Pressure bracket for a free space: well beyond every pressure it can see."""
        seen = [pressures[name] for name in self._neighbours(space)]
        return min(seen) - PRESSURE_BRACKET_PA, max(seen) + PRESSURE_BRACKET_PA

    def _solve_free(self, space: Space, pressures: Dict[str, float], n_override: Optional[float],
                    gap_factor: float, steps: int) -> float:
        """Pressure at which the free ``space`` balances, holding every other node fixed (bisection).

        When the balance does not close inside the bracket the bracket end
        nearest to closing is returned; the evaluation then reports the space
        UNSOLVED with its residual.
        """
        lo, hi = self._bracket(space, pressures)

        def func(p: float) -> float:
            trial = dict(pressures)
            trial[space.name] = p
            return self._residual(space, trial, n_override, gap_factor)

        root = _bisect(func, lo, hi, steps)
        if root is not None:
            return root
        return lo if abs(func(lo)) <= abs(func(hi)) else hi

    def _sweep(self, pressures: Dict[str, float], n_override: Optional[float], gap_factor: float,
               steps: int) -> float:
        """One Gauss-Seidel sweep over the free spaces; returns the largest pressure change."""
        change = 0.0
        for space in self.spaces:
            if not space.free:
                continue
            new = self._solve_free(space, pressures, n_override, gap_factor, steps)
            change = max(change, abs(new - pressures[space.name]))
            pressures[space.name] = new
        return change

    def solve_pressures(self, n_override: Optional[float] = None, gap_factor: float = 1.0,
                        sweeps: int = SWEEPS, steps: int = BISECTION_STEPS,
                        tolerance: Optional[float] = None) -> Tuple[Dict[str, float], int]:
        """Pressures of every node after at most ``sweeps`` sweeps of per-space bisection.

        With ``tolerance`` the sweeps stop early once no free pressure moves by more.
        Returns ``(pressures, sweeps_done)``.
        """
        pressures = self._initial_pressures()
        if not any(s.free for s in self.spaces):
            return pressures, 0
        done = 0
        for done in range(1, sweeps + 1):
            change = self._sweep(pressures, n_override, gap_factor, steps)
            if tolerance is not None and change <= tolerance:
                break
        return pressures, done

    # -- evaluation --------------------------------------------------------- #
    def _evaluate(self, pressures: Dict[str, float], n_override: Optional[float], gap_factor: float,
                  limit_override: Optional[float] = None) -> List[SpaceResult]:
        """Per-space balances, offsets, ratios and statuses at the given pressures."""
        results: List[SpaceResult] = []
        for space in self.spaces:
            res = self._flows(space, pressures, n_override, gap_factor, limit_override)
            res.design_offset = design_offset(space.surface_m2, res.limit)
            res.tighter_offset = tighter_room_offset(space.surface_m2)
            if space.free:
                self._evaluate_free(res)
            else:
                self._evaluate_controlled(res, pressures[space.name] < pressures[space.leaks_to])
            results.append(res)
        return results

    @staticmethod
    def _evaluate_free(res: SpaceResult) -> None:
        """Status of a free space: FREE when its balance closes, UNSOLVED (and flagged) otherwise."""
        res.offset_kind = KIND_FREE
        res.offset_needed = 0.0
        res.ratio = None
        residual = res.balance_residual
        if abs(residual) <= RESIDUAL_TOL_M3H:
            res.status = STATUS_FREE
            return
        res.status = STATUS_UNSOLVED
        res.flagged = True
        res.notes.append(f"balance does not close inside +/-{PRESSURE_BRACKET_PA:g} Pa of its neighbours: "
                         f"residual {residual:.1f} m3/h at {res.pressure_pa:.0f} Pa; check the fixed flows and gaps")

    @staticmethod
    def _evaluate_controlled(res: SpaceResult, negative: bool) -> None:
        """Offset, ratio and status of a controlled room; a reversed room is capped at VERIFY and noted."""
        nominal, other = (KIND_EXHAUST, KIND_SUPPLY) if negative else (KIND_SUPPLY, KIND_EXHAUST)
        needed = res.signed_offset if negative else -res.signed_offset
        if needed >= -OFFSET_EPS_M3H:
            res.offset_kind = nominal
            res.offset_needed = max(needed, 0.0)
            res.ratio = _ratio(res.design_offset, res.offset_needed)
            res.status = classify_ratio(res.ratio)
            return
        res.reversed = True
        res.offset_kind = other
        res.offset_needed = -needed
        res.ratio = _ratio(res.design_offset, res.offset_needed)
        res.status = STATUS_FAIL if classify_ratio(res.ratio) == STATUS_FAIL else STATUS_VERIFY
        res.flagged = True
        polarity = "negative" if negative else "positive"
        res.notes.append(f"leakage runs the wrong way for a {polarity} room (out {res.air_out:.1f}, in "
                         f"{res.air_in:.1f} m3/h): it needs a {other} offset of {res.offset_needed:.1f} m3/h, "
                         f"not {nominal}; check the neighbouring pressures")

    def _uncontrolled_pressure(self, space: Space, pressures: Dict[str, float]) -> Optional[float]:
        """Pressure the room reaches when the design offset is applied with no control.

        Solves ``offset(p) = design_offset`` for the room's own pressure with its
        neighbours fixed: exhaust-type rooms are pulled further negative, supply-type
        rooms further positive, when the design offset exceeds the offset needed.
        """
        if space.free:
            return None
        target = design_offset(space.surface_m2, space.limit)
        negative = pressures[space.name] < pressures[space.leaks_to]
        sign = 1.0 if negative else -1.0

        def func(p: float) -> float:
            trial = dict(pressures)
            trial[space.name] = p
            return sign * self._flows(space, trial, None, 1.0).signed_offset - target

        lo, hi = self._bracket(space, pressures)
        return _bisect(func, lo, hi, BISECTION_STEPS)

    def _sensitivity(self, base: List[SpaceResult], pressures: Dict[str, float], sweeps: int, steps: int,
                     tolerance: Optional[float]) -> None:
        """Fill the sensitivity ratios, the uncontrolled pressure and the flags in place."""
        low_n, high_n = FLOW_EXPONENT_RANGE
        cases = {
            "ratio_n050": (low_n, 1.0),
            "ratio_n075": (high_n, 1.0),
            "ratio_gaps_x2": (None, 2.0),
        }
        labels = {"ratio_n050": f"n = {low_n:.2f}", "ratio_n075": f"n = {high_n:.2f}", "ratio_gaps_x2": "gaps doubled"}
        for attr, (n_override, gap_factor) in cases.items():
            case_pressures, _ = self.solve_pressures(n_override, gap_factor, sweeps, steps, tolerance)
            for res, case in zip(base, self._evaluate(case_pressures, n_override, gap_factor)):
                setattr(res, attr, case.ratio)
                if case.status == STATUS_FAIL and res.status != STATUS_FAIL:
                    res.flagged = True
                    res.notes.append(f"fails with {labels[attr]} (ratio {case.ratio:.2f})")
        for res in base:
            space = self._space(res.name)
            res.uncontrolled_pa = self._uncontrolled_pressure(space, pressures)
            if not space.free and abs(float(space.set_pressure_pa)) < APPLIES_AT_OR_BEYOND_PA:  # type: ignore[arg-type]
                res.notes.append(f"set pressure below {APPLIES_AT_OR_BEYOND_PA:g} Pa; method not applicable")
            if res.ratio is not None and math.isinf(res.ratio):
                res.notes.append("no offset needed at the set pressure")

    def _totals(self, results: List[SpaceResult], pressures: Dict[str, float], sweeps: int) -> SuiteResult:
        """Suite totals per offset kind; a single-kind suite gets a ratio (internal links cancel), a mixed one MIXED."""
        controlled = [r for r in results if r.controlled]
        exhaust = [r for r in controlled if r.offset_kind == KIND_EXHAUST]
        supply = [r for r in controlled if r.offset_kind == KIND_SUPPLY]
        suite = SuiteResult(self.name, results, pressures, sweeps=sweeps)
        suite.exhaust_needed, suite.exhaust_design = _kind_totals(exhaust)
        suite.supply_needed, suite.supply_design = _kind_totals(supply)
        suite.offset_needed = suite.exhaust_needed + suite.supply_needed
        suite.design_offset = suite.exhaust_design + suite.supply_design
        suite.tighter_offset = sum(r.tighter_offset for r in controlled)
        suite.offset_kind = "+".join(k for k, rows in ((KIND_EXHAUST, exhaust), (KIND_SUPPLY, supply)) if rows)
        if exhaust and supply:
            suite.status = STATUS_MIXED
            suite.notes.append(f"mixed suite: exhaust rooms need {suite.exhaust_needed:.1f} m3/h (design "
                               f"{suite.exhaust_design:.1f}), supply rooms {suite.supply_needed:.1f} m3/h (design "
                               f"{suite.supply_design:.1f}); no single margin, see the space rows")
        elif controlled:
            suite.ratio = _ratio(suite.design_offset, suite.offset_needed)
            suite.status = classify_ratio(suite.ratio)
            suite.notes.append("internal links cancel")
        if any(r.reversed for r in controlled):
            suite.notes.append("includes a reversed room (leakage runs the wrong way)")
        suite.flagged = any(r.flagged for r in results)
        suite.max_residual = max((abs(r.balance_residual) for r in results if not r.controlled), default=0.0)
        suite.notes.append(f"{sweeps} sweeps; free-space residual {suite.max_residual:.3g} m3/h")
        return suite

    def solve(self, sweeps: int = SWEEPS, steps: int = BISECTION_STEPS, sensitivity: bool = True) -> SuiteResult:
        """Solve the suite with a fixed number of iterations (``sweeps`` x ``steps`` bisection per free space)."""
        pressures, done = self.solve_pressures(None, 1.0, sweeps, steps)
        results = self._evaluate(pressures, None, 1.0)
        if sensitivity:
            self._sensitivity(results, pressures, sweeps, steps, None)
        return self._totals(results, pressures, done)

    def solve_exact(self, tolerance: float = 1e-9, max_sweeps: int = 1000, steps: int = 200,
                    sensitivity: bool = True) -> SuiteResult:
        """Solve to ``tolerance`` Pa: sweeps continue until no free pressure moves by more (or ``max_sweeps``)."""
        pressures, done = self.solve_pressures(None, 1.0, max_sweeps, steps, tolerance)
        results = self._evaluate(pressures, None, 1.0)
        if sensitivity:
            self._sensitivity(results, pressures, max_sweeps, steps, tolerance)
        return self._totals(results, pressures, done)


def _kind_totals(rows: List[SpaceResult]) -> Tuple[float, float]:
    """``(offset_needed, design_offset)`` summed over rooms of one offset kind."""
    return sum(r.offset_needed for r in rows), sum(r.design_offset for r in rows)


SPACE_KEYS = frozenset({"name", "set_pressure_pa", "surface_m2", "leaks_to", "supply_m3h", "exhaust_m3h", "limit", "n"})
SPACE_GEOMETRY_KEYS = frozenset({"floor_area", "perimeter", "height"})
LINK_KEYS = frozenset({"a", "b", "gap_area_m2", "gap_area_cm2", "sealed", "fixed_flow_m3h", "kind"})


def _reject_unknown_keys(what: str, data: Dict[str, Any], allowed: frozenset) -> None:
    """Raise ``ValueError`` naming any key of ``data`` outside ``allowed`` (typos must not pass silently)."""
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"{what}: unknown key(s) {unknown}; allowed: {sorted(allowed)}")


def _space_kwargs(item: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a JSON space mapping to ``Space`` keyword arguments (geometry keys accepted)."""
    data = dict(item)
    _reject_unknown_keys(f"space {data.get('name', '?')}", data, SPACE_KEYS | SPACE_GEOMETRY_KEYS)
    if "surface_m2" not in data and SPACE_GEOMETRY_KEYS <= set(data):
        data["surface_m2"] = internal_surface(float(data.pop("floor_area")), float(data.pop("perimeter")),
                                              float(data.pop("height")))
    elif SPACE_GEOMETRY_KEYS & set(data):
        raise ValueError(f"space {data.get('name', '?')}: give surface_m2 or all of floor_area, perimeter, height")
    return data


def _link_kwargs(item: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a JSON link mapping to ``Link`` keyword arguments (cm2 and sealed kinds accepted)."""
    data = dict(item)
    _reject_unknown_keys(f"link {data.get('a', '?')}-{data.get('b', '?')}", data, LINK_KEYS)
    if "gap_area_cm2" in data:
        data["gap_area_m2"] = cm2_to_m2(float(data.pop("gap_area_cm2")))
    if "sealed" in data:
        data["gap_area_m2"] = sealed_gap_area(str(data.pop("sealed")))
    return data


# --------------------------------------------------------------------------- #
# Rows for a workbook tab
# --------------------------------------------------------------------------- #
def _fmt(value: Optional[float], digits: int = 1) -> Any:
    """Round a float for a row; ``None`` stays blank and infinity is spelt out."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isinf(value):
        return "inf"
    return round(float(value), digits)


def _space_row(suite: SuiteResult, res: SpaceResult) -> Dict[str, Any]:
    """One row of :func:`to_rows` for a space."""
    return {
        "suite": suite.suite,
        "space": res.name,
        "set_pressure_pa": _fmt(res.set_pressure_pa, 1),
        "solved_pressure_pa": _fmt(res.pressure_pa, 2),
        "leaks_to": res.leaks_to,
        "surface_m2": _fmt(res.surface_m2, 2),
        "limit_m3h_m2": _fmt(res.limit, 2),
        "envelope_in_m3h": _fmt(res.envelope_in),
        "envelope_out_m3h": _fmt(res.envelope_out),
        "gap_in_m3h": _fmt(res.gap_in),
        "gap_out_m3h": _fmt(res.gap_out),
        "fixed_in_m3h": _fmt(res.fixed_in),
        "fixed_out_m3h": _fmt(res.fixed_out),
        "air_in_m3h": _fmt(res.air_in),
        "air_out_m3h": _fmt(res.air_out),
        "offset_kind": res.offset_kind,
        "offset_needed_m3h": _fmt(res.offset_needed),
        "offset_needed_lps": _fmt(lps(res.offset_needed)),
        "design_offset_m3h": _fmt(res.design_offset),
        "design_offset_lps": _fmt(lps(res.design_offset)),
        "tighter_offset_m3h": _fmt(res.tighter_offset),
        "ratio": _fmt(res.ratio, 2),
        "status": res.status,
        "ratio_n050": _fmt(res.ratio_n050, 2),
        "ratio_n075": _fmt(res.ratio_n075, 2),
        "ratio_gaps_x2": _fmt(res.ratio_gaps_x2, 2),
        "uncontrolled_pa": _fmt(res.uncontrolled_pa, 1),
        "flagged": "y" if res.flagged else "n",
        "notes": "; ".join(res.notes),
    }


def _suite_row(suite: SuiteResult) -> Dict[str, Any]:
    """The totals row of :func:`to_rows`."""
    controlled = [r for r in suite.spaces if r.controlled]
    row = {key: "" for key in ROW_FIELDS}
    row.update({
        "suite": suite.suite,
        "space": "SUITE TOTAL",
        "surface_m2": _fmt(sum(r.surface_m2 for r in controlled), 2),
        "offset_kind": suite.offset_kind,
        "offset_needed_m3h": _fmt(suite.offset_needed),
        "offset_needed_lps": _fmt(lps(suite.offset_needed)),
        "design_offset_m3h": _fmt(suite.design_offset),
        "design_offset_lps": _fmt(lps(suite.design_offset)),
        "tighter_offset_m3h": _fmt(suite.tighter_offset),
        "ratio": _fmt(suite.ratio, 2),
        "status": suite.status,
        "flagged": "y" if suite.flagged else "n",
        "notes": "; ".join(suite.notes),
    })
    return row


def to_rows(result: SuiteResult) -> List[Dict[str, Any]]:
    """Rows for a workbook tab: one per space (``ROW_FIELDS`` order) followed by a ``SUITE TOTAL`` row."""
    rows = [_space_row(result, res) for res in result.spaces]
    rows.append(_suite_row(result))
    return rows


# --------------------------------------------------------------------------- #
# Known-answer tests
# --------------------------------------------------------------------------- #
TOL_M3H = 0.2
TOL_PER_M2 = 0.001
TOL_RATIO = 0.01
TOL_AREA = 0.05

ROOM_SMALL = internal_surface(1.88 * 2.94, 2.0 * (1.88 + 2.94), 3.0)
"""The 1.88 x 2.94 x 3.0 m room used by the known answers (about 40.0 m2)."""
ROOM_LARGE = internal_surface(3.97 * 4.83, 2.0 * (3.97 + 4.83), 3.0)
"""The 3.97 x 4.83 x 3.0 m room used by the known answers (about 91.2 m2)."""


def known_answer_anteroom() -> Suite:
    """Case 4: the small room as an anteroom at -15 Pa between a corridor, a -30 Pa room and a shaft."""
    return Suite(
        "KA-4 anteroom",
        [Space("ANTE", -15.0, ROOM_SMALL)],
        [
            Link("CORRIDOR", "ANTE", gap_area_m2=cm2_to_m2(15.0), kind="hinged sealed door"),
            Link("SHAFT", "ANTE", fixed_flow_m3h=10.2, kind="sealed shaft door (fixed)"),
            Link("ANTE", "ROOM30", gap_area_m2=cm2_to_m2(10.0), kind="sliding sealed door"),
        ],
        externals={"CORRIDOR": 0.0, "ROOM30": -30.0, "SHAFT": 0.0},
    )


def known_answer_suite() -> Suite:
    """Case 5: the large room at -30 Pa with its ensuite at -31 Pa, fed from the corridor and the anteroom."""
    return Suite(
        "KA-5 isolation suite",
        [Space("ROOM", -30.0, ROOM_LARGE), Space("ENSUITE", -31.0, 37.5)],
        [
            Link("CORRIDOR", "ROOM", gap_area_m2=cm2_to_m2(20.0), kind="bed-width sealed door"),
            Link("ANTE", "ROOM", gap_area_m2=cm2_to_m2(10.0), kind="sliding sealed door"),
            Link("ROOM", "ENSUITE", gap_area_m2=cm2_to_m2(10.0), kind="sliding sealed door (internal)"),
        ],
        externals={"CORRIDOR": 0.0, "ANTE": -15.0},
    )


def known_answer_cases() -> List[Tuple[str, str, float, float, float]]:
    """``(group, label, computed, expected, tolerance)`` for every known-answer check."""
    cases: List[Tuple[str, str, float, float, float]] = []
    for pa, expected in ((5, 0.560), (15, 1.143), (25, 1.593), (50, 2.500), (70, 3.111)):
        cases.append(("1 limit conversion", f"{pa} Pa, m3/h per m2", limit_at(pa), expected, TOL_PER_M2))
    cases.append(("2 small room", "surface m2", ROOM_SMALL, 40.0, TOL_AREA))
    cases.append(("2 small room", "envelope at 50 Pa", envelope_leak(ROOM_SMALL, 50), 99.9, TOL_M3H))
    cases.append(("2 small room", "envelope at 15 Pa", envelope_leak(ROOM_SMALL, 15), 45.7, TOL_M3H))
    for cm2, pa, expected in ((15, 15, 16.2), (10, 15, 10.8), (20, 30, 30.5)):
        cases.append(("3 gaps", f"{cm2} cm2 at {pa} Pa", gap_flow(cm2_to_m2(cm2), pa), expected, TOL_M3H))
    ante = known_answer_anteroom().solve().space("ANTE")
    cases.append(("4 anteroom -15 Pa", "offset needed", ante.offset_needed, 61.3, TOL_M3H))
    cases.append(("4 anteroom -15 Pa", "design offset", ante.design_offset, 99.9, TOL_M3H))
    cases.append(("4 anteroom -15 Pa", "ratio", ante.ratio or 0.0, 1.63, TOL_RATIO))
    suite = known_answer_suite().solve()
    cases.append(("5 suite -30/-31 Pa", "room envelope", suite.space("ROOM").envelope_in, 163.5, TOL_M3H))
    cases.append(("5 suite -30/-31 Pa", "ensuite envelope", suite.space("ENSUITE").envelope_in, 68.7, TOL_M3H))
    cases.append(("5 suite -30/-31 Pa", "offset needed", suite.offset_needed, 273.5, TOL_M3H))
    cases.append(("5 suite -30/-31 Pa", "design offset", suite.design_offset, 321.6, TOL_M3H))
    cases.append(("5 suite -30/-31 Pa", "ratio", suite.ratio or 0.0, 1.18, TOL_RATIO))
    cases.append(("door gaps", "0.9 x 2.1 leaf, cm2", unsealed_door_gap_area(0.9, 2.1) * 1e4, 240.0, 5.0))
    cases.append(("door gaps", "0.6 x 1.8 access door, cm2", unsealed_door_gap_area(0.6, 1.8) * 1e4, 190.0, 5.0))
    return cases


def run_selftest(out: Optional[Any] = None) -> int:
    """Run the known-answer tests, print a table (to ``out`` or stdout) and return the number of misses."""
    out = sys.stdout if out is None else out
    cases = known_answer_cases()
    misses = 0
    print(f"{'group':22s} {'check':30s} {'computed':>10s} {'expected':>10s} {'tol':>7s}  result", file=out)
    for group, label, computed, expected, tol in cases:
        ok = abs(computed - expected) <= tol
        misses += 0 if ok else 1
        print(f"{group:22s} {label:30s} {computed:10.3f} {expected:10.3f} {tol:7.3f}  {'ok' if ok else 'MISS'}",
              file=out)
    verdict = "pass" if misses == 0 else "FAIL"
    print(f"selftest: {len(cases) - misses} of {len(cases)} known-answer checks pass ({verdict})", file=out)
    return misses


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def _run_suite_file(path: Path, out: Optional[Path], exact: bool, as_json: bool) -> int:
    """Solve a suite described in a JSON file; write a CSV (idempotent) or print rows."""
    data = json.loads(path.read_text(encoding="utf-8"))
    suite = Suite.from_dict(data)
    result = suite.solve_exact() if exact else suite.solve()
    rows = result.to_rows()
    if out is not None:
        write_csv_dicts(out, rows, ROW_FIELDS)
        print(f"wrote {len(rows)} rows to {out}")
    elif as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            print(f"{row['space']:14s} {str(row['set_pressure_pa']):>7s} Pa  offset {str(row['offset_needed_m3h']):>7s}"
                  f"  design {str(row['design_offset_m3h']):>7s}  ratio {str(row['ratio']):>5s}  {row['status']}"
                  f"{'  flagged' if row['flagged'] == 'y' else ''}")
    bad = [r.name for r in result.spaces if r.status in BAD_STATUSES]
    if result.status == STATUS_FAIL:
        bad.append("SUITE TOTAL")
    if bad:
        print(f"pressure: {result.worst_status} in {', '.join(bad)}", file=sys.stderr)
        return 3
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Argument parser: ``--selftest`` or a suite JSON file to solve."""
    parser = argparse.ArgumentParser(description="Room pressure-offset method (known-answer self-test or a suite file).")
    parser.add_argument("--selftest", action="store_true", help="run the known-answer tests; exit non-zero on a miss")
    parser.add_argument("--suite", type=Path, default=None, help="JSON file describing a suite to solve")
    parser.add_argument("--out", type=Path, default=None, help="CSV to write the rows to (with --suite)")
    parser.add_argument("--exact", action="store_true", help="use solve_exact instead of the fixed-iteration solve")
    parser.add_argument("--json", action="store_true", help="print rows as JSON (with --suite, no --out)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point; exit code 1 on a self-test miss or error, 3 when any space or the suite FAILs or is UNSOLVED."""
    args = build_parser().parse_args(argv)
    try:
        if args.selftest:
            return 1 if run_selftest() else 0
        if args.suite is not None:
            return _run_suite_file(args.suite, args.out, args.exact, args.json)
    except Exception as exc:  # noqa: BLE001 - surface any failure as a non-zero exit
        print(f"error: {exc}", file=sys.stderr)
        return 1
    build_parser().print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
