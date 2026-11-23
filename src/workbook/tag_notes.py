"""Tag forms that two documents print differently, with the documents that print each form."""

from __future__ import annotations

import re
from typing import Optional

CATH = re.compile(r"AHU-B-0[47]-04CATH-0([12])")


def cath_tag_note(tag: str) -> str:
    """Both written forms of a cath lab unit tag. The form that governs is not decided."""
    m = CATH.search(tag or "")
    if not m:
        return ""
    n = m.group(1)
    return (f"Tag as written on IP-BH-07141 and IP-BH-16041: AHU-B-04-04CATH-0{n}. Tag as written on the RFI-ME-0233 zoning sample and in memo Table 1: AHU-B-07-04CATH-0{n}. "
            "Memo Table 3 prints AHU-B-04-04CATH-01, -02. The memo is not consistent in itself. The form that governs is not decided.")


UCV_UNIT = re.compile(r"^AHU-B-07-05MOTCT-01$")


def ucv_unit_note(tag: str) -> str:
    """R3-05: the note of the MOT NSC air-side schematic of the CT and UCV theatre. The note gives the supply airflow that the room needs for the future UCV."""
    if not UCV_UNIT.match((tag or "").strip()):
        return ""
    return ("MOT NSC note on air-side schematic IP-BH-16057 (Corr 2, rev 0A) for the CT and UCV theatre (MSOT 05.02.10): an approximate supply airflow of 5,320 L/s shall be "
            "provided and achieved to the room to accommodate the future UCV, and the ductwork sizing shall account for the pressure loss. The schematic room table gives 3,600 L/s "
            "(normal mode and single pass). The unit carries two duties: the day-1 duty (room table) and the future duty of 5,320 L/s to the room. The future duty governs the unit.")


HYB_UNIT = re.compile(r"^AHU-B-07-05MOTHYB-02$")


def hyb_unit_note(tag: str) -> str:
    """R3-09: the unit that the schedule of modes names for the hybrid theatre and that the equipment schedule does not list."""
    if not HYB_UNIT.match((tag or "").strip()):
        return ""
    return ("Conflict between documents: the MOT NSC schedule of modes (Appendix J, theatre MSOT 05.02.12) names this unit. The MOT NSC Overall AC Equipment Schedule lists only "
            "AHU-B-07-05MOTHYB-01, and the zone schedule does not show this unit. No tender airflow is used. The tag is a variant of AHU-B-07-05MOTHYB-01: it is listed and it is not used (D-059).")


def unit_note(tag: str) -> str:
    """Notes of a unit that the documents print and that the calculation does not resolve."""
    return " ".join(x for x in (ucv_unit_note(tag), hyb_unit_note(tag)) if x)


def tag_note(tag: str) -> Optional[str]:
    return cath_tag_note(tag) or ucv_unit_note(tag) or hyb_unit_note(tag) or None
