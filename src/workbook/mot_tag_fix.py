"""AHU tags of the MOT NSC scope as printed in the MOT NSC Equipment Schedule (24 Nov 2026, Marcus; D-097).

Source of the map: the AHU Cross-Verification Register (MOT NSC Register Rev A, uploaded 24 Nov 2026; data/uploads/mot_crossref_20261124/),
column 'AHU Tag (Working Drawing)', checked against '1. Overall AC Equipment Schedule (MOT NSC)_COR2.pdf' (the schedule prints the same tags;
'MOTlSONP' in its text layer is MOTISONP). DD Booklet and old L07 layout variants that the register flags are replaced by the schedule tag.
Tags outside the MOT NSC scope (PACU, PREOP, ADMIT, MOTSTORE, DSOTSUPP-04, DIAGRE, ANGIO-01/02) are ACMV NSC units and are not changed,
except AHU-B-07-05MOTSTAFF-01, which the register gives as AHU-B-03-05MOTSTAFF-01 (working drawing).

Applied to the workbook inputs (rooms, unit rows, groups, fans, remarks) after the consolidation. A unit row that becomes a duplicate is merged
into the first one.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

SRC_DOC = "AHU_Cross_Verification_MOT_NSC_Register_RevA.xlsx (sheet AHU Register) and 1. Overall AC Equipment Schedule (MOT NSC)_COR2.pdf"

# old or booklet tag -> MOT NSC Equipment Schedule (working drawing) tag
MAP: Dict[str, str] = {
    "AHU-B-07-04CATH-01": "AHU-B-04-04CATH-01",
    "AHU-B-07-04CATH-02": "AHU-B-04-04CATH-02",
    "AHU-B-07-05MOTSTAFF-01": "AHU-B-03-05MOTSTAFF-01",
    "AHU-B-07-05MOTSTA-01": "AHU-B-03-05MOTSTAFF-01",
    "AHU-B-07-05MOTHYB-12": "AHU-B-07-05MOTHYB-01",
    "AHU-B-07-04ANGIONP-04A": "AHU-B-07-04ANGIO-04A",
    "AHU-B-07-04ANGIONP-04B": "AHU-B-07-04ANGIO-04B",
}
PATTERNS = [
    (re.compile(r"AHU-B-07-0([56])STERS-0([123])"), r"AHU-B-07-0\1STER-0\2"),
    (re.compile(r"AHU-B-07-06DOTSUPP-0([1234])"), r"AHU-B-07-06DSOTSUPP-0\1"),
]


def fix_text(s: str) -> str:
    for a, b in sorted(MAP.items(), key=lambda kv: -len(kv[0])):
        s = re.sub(re.escape(a) + r"(?![0-9])", b, s)
    for p, b in PATTERNS:
        s = p.sub(b, s)
    return s


def apply(datasets: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    log = []
    for n, d in datasets.items():
        before = json.dumps(d, ensure_ascii=False)
        after = fix_text(before)
        if after == before:
            continue
        changed = sorted({m for m in re.findall(r"(?:AHU|PAHU)-B-[0-9A-Z-]+", before) if fix_text(m) != m})
        nd = json.loads(after)
        # merge unit rows that are now duplicates (same tag): keep the first
        seen, keep = set(), []
        for a in nd.get("ahus", []):
            t = a.get("tag")
            if t in seen:
                log.append(dict(workbook=f"D{n}", action="unit row merged", tag=t))
                continue
            seen.add(t)
            keep.append(a)
        nd["ahus"] = keep
        if not any(s_.get("id") == "MOTNSC-TAGS" for s_ in nd.get("sources", [])):
            nd.setdefault("sources", []).append(dict(id="MOTNSC-TAGS", document=SRC_DOC, issue="Rev A (register); Corr 2 (schedule)", page="AHU Register; p1",
                                                    location="AHU tag columns", text_as_read="; ".join(f"{c} -> {fix_text(c)}" for c in changed),
                                                    method="Tag replaced by the MOT NSC Equipment Schedule tag (D-097)", confidence="high"))
        d.clear()
        d.update(nd)
        for c in changed:
            log.append(dict(workbook=f"D{n}", action="tag corrected", tag=c, to=fix_text(c)))
    return log
