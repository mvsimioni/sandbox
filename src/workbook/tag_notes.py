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


def tag_note(tag: str) -> Optional[str]:
    return cath_tag_note(tag) or None
