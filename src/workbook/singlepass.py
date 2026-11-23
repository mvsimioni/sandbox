"""Class of a single-pass room: CLEAN (100 % outdoor air for clean or sterile supply) or DIRTY (single pass for exhaust reasons only).

Rule of the engineer's instruction of 23 Nov 2026 (D-031): a single-pass room sets the outdoor-air fraction of its unit only where its criteria need 100 % outdoor air
for clean or sterile supply (isolation, protective, theatre, clean rooms: air class 1 or HEPA filtration, or a specified 100 % outdoor air). A dirty room (dirty utility,
disposal, soiled, decontamination, toilets and similar: negative or neutral, no sterile need) takes recirculated supply from the unit and a dedicated exhaust with no return.
The class is an assumption (code A), to confirm. The recomputation (src/recompute) holds its own copy of this rule and does not import this one.
"""

from __future__ import annotations

import re
from typing import Any, Dict

CLEAN_WORDS = re.compile(r"isolation|protective|theatre|operating|clean|steril|aseptic|compounding|cleanroom|hepa|laminar|cath|angio|ucv", re.I)
DIRTY_WORDS = re.compile(r"dirty|soiled|disposal|decontam|toilet|\bwc\b|ensuite|shower|bathroom|cleaner|sluice|waste|pan room|washroom|latrine|janitor|bedpan|linen", re.I)


def _num(v: Any):
    return None if v is None or v == "" else float(v)


def classify_single_pass(name: str, crit: Dict[str, Any]) -> str:
    """CLEAN, DIRTY, or '' when the room is not single pass. A stated class wins."""
    if str(crit.get("single_pass") or "").upper() != "Y":
        return ""
    given = str(crit.get("sp_class") or "").upper()
    if given in ("CLEAN", "DIRTY"):
        return given
    sup, oa = _num(crit.get("supply_specified_lps")), _num(crit.get("oa_specified_lps"))
    if str(crit.get("code") or "").upper() == "S" and sup and oa is not None and oa >= 0.99 * sup:
        return "CLEAN"
    if str(crit.get("air_class") or "").strip() == "1" or "HEPA" in str(crit.get("filter") or "").upper():
        return "CLEAN"
    if DIRTY_WORDS.search(name or ""):
        return "DIRTY"
    if CLEAN_WORDS.search(name or ""):
        return "CLEAN"
    regime = str(crit.get("pressure_regime") or "").upper()
    return "DIRTY" if regime in ("", "NEGATIVE", "NEUTRAL") else "CLEAN"
