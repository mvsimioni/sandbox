"""Criteria rows from the HFDS Mechanical reverse-brief (D-036, D-037, D-064, D-065).

Used for rooms that have no booklet row (plant, comms, riser, IT, UPS, battery rooms, the Innovation Lab, scrub rooms, clean and patient
corridors, toilets and every other room with no row), and as the comparison row of D-064. Each row keeps the HFDS table and the printed page.
The match is by room name; the rule table below is the whole mapping and is printed on Basis of Design.
"""

from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REG = Path("out/registers")
HFDS_DOC = "HFDS - Mechanical reverse-brief.pdf"

# (name pattern, table id, area name as in the register, kind, reason)
RULES: List[Tuple[str, str, str, str, str]] = [
    # D-095: HFDS Vol 2 cl. 1.1.3.20 puts UPS and battery rooms with the electrical plant rooms (duty + standby FCUs, 27 deg C or less)
    (r"\bups\b|\bbatt", "T.1.1.1.1", "Transformer Room", "plant",
     "UPS and battery rooms are electrical plant rooms (HFDS Vol 2 cl. 1.1.3.20: N duty + 1 standby FCUs, 27 deg C or less) and follow the HFDS transformer room row"),
    (r"\b(lan|comms?|communications?|telecom|data ?cent|server|\bit\b|ict|riser comms)\b", "T.1.1.1.1", "LAN Room", "it",
     "IT and LAN rooms follow the HFDS LAN room row on fan coil units"),
    (r"transformer|\btfr\b|\bhv\b|\blv\b|\bht\b|\bcsr\b|switch ?(room|gear|rm)|elec(trical)?\b|\bmv ?room|generator|genset", "T.1.1.1.1", "Transformer Room", "plant",
     "electrical rooms follow the HFDS transformer room row (HFDS Vol 2 cl. 1.1.3.20: N duty + 1 standby FCUs, 27 deg C or less)"),
    (r"\bahu\b|air handling|plant|pump|chiller|boiler|mechanical|compressor|\bmep\b|\bfcu room|\bhvac", "T.1.1.1.1", "Chiller Plant Room", "plant",
     "plant rooms follow the HFDS chiller plant room row"),
    (r"riser|shaft|duct|services? (room|cupboard)|\bsvc\b|cupboard", "T.1.1.1.1", "Chiller Plant Room", "riser",
     "riser and service rooms follow the HFDS plant room row, the nearest non-clinical row"),
    (r"innovation|\blab\b.*(innov|idea)", "T.1.1.1.1", "Meeting Room", "innovation",
     "the Innovation Lab has no HFDS room type; the nearest row is the meeting room row"),
    (r"scrub", "T.1.1.3.7", "Scrub", "scrub", "scrub rooms follow the HFDS scrub row of the operating theatre layout"),
    (r"clean corridor|sterile corridor", "T.1.1.3.7", "Clean Corridor", "corridor", "HFDS clean corridor row"),
    (r"patient corridor", "T.1.1.3.7", "Patient Corridor", "corridor", "HFDS patient corridor row"),
    (r"dirty utility|dirty linen|sluice|soiled", "T.1.1.1.1", "Dirty Utility Room", "dirty", "HFDS dirty utility row"),
    (r"clean utility|clean linen|clean store", "T.1.1.1.1", "Clean Utility Room", "clean", "HFDS clean utility row"),
    (r"ensuite|en-suite|bed toilet|patient toilet|shower", "T.1.1.3.14 (MV)", "Ensuite Toilets", "toilet", "HFDS ensuite toilet row (MV table)"),
    (r"toilet|\bwc\b|lavatory|washroom|restroom|accessible wc", "T.1.1.3.14 (MV)", "Public Toilet", "toilet", "HFDS public toilet row (MV table)"),
    (r"change|locker|rest|staff room|pantry|lounge", "T.1.1.1.1", "Staff Change and Rest Areas", "staff", "HFDS staff change and rest row"),
    (r"meeting|conference|training|seminar|huddle|collab", "T.1.1.1.1", "Meeting Room", "meeting", "HFDS meeting room row"),
    (r"office|workstation|reception|admin|workspace|work area|hot desk", "T.1.1.1.1", "Offices", "office", "HFDS office row"),
    (r"corridor|lobby|circulation|waiting|atrium|entrance|foyer|stair", "T.1.1.1.1", "Public Area / Circulation Corridors", "circulation", "HFDS public area row"),
    (r"anteroom|ante room|ante-room", "T.1.1.3.12", "Ante Room", "anteroom", "HFDS anteroom row"),
    # Item 4 of the fix pass (D-026, D-064 (3), D-071): names with no booklet row in any department take the nearest HFDS row, flagged VERIFY. The rule table is the whole mapping.
    (r"airlock|air lock|sound lock|gown down|\block\b", "T.1.1.3.12", "Ante Room", "anteroom", "airlocks, sound locks and gown-down rooms follow the HFDS anteroom row"),
    (r"equipment room|technical room|tech room|technical equipment|\bcontrol cabinet|\bplant\b", "T.1.1.1.1", "LAN Room", "equip",
     "equipment and technical rooms follow the HFDS LAN room row (heat from equipment, no occupants)"),
    (r"recovery", "T.1.1.1.1", "OT – Support Area PACU", "recovery", "recovery bays follow the HFDS PACU row"),
    (r"\bwait|waiting", "T.1.1.1.1", "Public waiting areas", "circulation", "HFDS public waiting row"),
    (r"slit lamp|\bexam\b|poct|procedure room|minor ot", "T.1.1.1.1", "Exam / Treatment Room", "exam", "HFDS exam and treatment room row"),
    (r"trolley decon|decon\.? area|decontamination", "T.1.1.1.1", "Dirty Utility Room", "dirty", "HFDS dirty utility row"),
    (r"deboxing|\bmmd\b|linen", "T.1.1.1.1", "MMD / Clean Linen", "clean", "HFDS MMD and clean linen row"),
    (r"pharmacy automation|pharmacy store", "T.1.1.1.1", "Pharmacy Store", "clean", "HFDS pharmacy store row"),
    (r"bath(room)?\b|trolley bath", "T.1.1.3.14 (MV)", "Ensuite Toilets", "toilet", "HFDS ensuite toilet row (MV table)"),
    (r"boots|clogs", "T.1.1.1.1", "Staff Change and Rest Areas", "staff", "HFDS staff change row"),
    (r"workshop|workroom|work room|technician|command centre|research|interview|sleep pod|detainee|\bmro\b|scdf|satellite|ops ", "T.1.1.1.1", "Offices", "office",
     "HFDS office row (nearest non-clinical room row)"),
    (r"\bsb\.\d+|substation|\bm&e\b|\bht\b|\biep\b|\bmp\b|se fan|fan room|\bpts\b|pwcs|holding tank|\btank\b|ejector|pit\b|newater|cylinder|\bfilter\b|waste|refuse|recycling|decon tank|\bwater\b",
     "T.1.1.1.1", "Chiller Plant Room", "plant", "service, tank, substation and waste spaces follow the HFDS plant room row, the nearest non-clinical row"),
    (r"vegetable prep|meat prep|pot wash|dishwash|crate wash|cook chill|r&d kitchen|recipe|sanitation|therapeutic room|receiving area", "T.1.1.1.1", "Kitchen - Food Preparation", "kitchen",
     "kitchen support rooms follow the HFDS kitchen food preparation row"),
    (r"staff accom|breakout|backstage|vip holding|holding area|tech bar|projection", "T.1.1.1.1", "Staff Change and Rest Areas", "staff", "HFDS staff change and rest row (nearest non-clinical row)"),
    (r"\bretail|\brtl\b|bus stop|external covered|protected|common space", "T.1.1.1.1", "Public Area / Circulation Corridors", "circulation", "HFDS public area row"),
    (r"\bbay\b|parking|\bpark\b|\bppe\b|allowance|\bs\.\d+\b|enclosed|kiosk|stroller|photocopy|beverage|store\b|storage|cleaner", "T.1.1.1.1", "Public Area / Circulation Corridors", "circulation",
     "bays, parking spaces and stores that are recesses of a corridor follow the HFDS public area row"),
]
CLASS_KINDS = {"it": "FCU", "plant": "", "riser": ""}     # D-037: IT, LAN, UPS and battery rooms are classed FCU


def _read(root: Path) -> List[Dict[str, str]]:
    p = Path(root) / REG / "hfds_criteria.csv"
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@lru_cache(maxsize=4)
def _rows(root: str) -> Tuple[Dict[str, str], ...]:
    return tuple(_read(Path(root)))


def _f(v: Any) -> Optional[float]:
    try:
        return float(str(v).replace(",", "")) if str(v).strip() not in ("", "NR", "-") else None
    except ValueError:
        return None


def find_row(root: Path, name: str) -> Optional[Tuple[Dict[str, str], str, str]]:
    """(HFDS register row, kind, reason) for a room name, or None where no rule matches."""
    rows = _rows(str(root))
    low = (name or "").lower()
    for pat, table, area, kind, why in RULES:
        if re.search(pat, low):
            for r in rows:
                if r["table_id"] == table and r["area_name"].startswith(area) or (r["table_id"] == table and area in r["area_name"]):
                    return r, kind, why
    return None


def to_criteria(row: Dict[str, str], source_id: str, kind: str, why: str) -> Dict[str, Any]:
    """Criteria dict of the workbook schema from an HFDS row (code D, source = HFDS table and page)."""
    sp, tol = _f(row.get("temp_setpoint_c")), _f(row.get("temp_tol_c"))
    tmin, tmax = _f(row.get("temp_min_c")), _f(row.get("temp_max_c"))
    if sp is not None and tmin is None and tmax is None and tol is not None:
        tmin, tmax = sp - tol, sp + tol
    reg = (row.get("pressure_regime") or "").strip().lower()
    regime = {"positive": "POSITIVE", "negative": "NEGATIVE", "neutral": "NEUTRAL"}.get(reg)
    if reg.startswith("positive or neutral"):
        regime = "POSITIVE"
    exh_out = (row.get("exhaust_all_to_outdoors") or "").strip().upper() == "Y"
    crit: Dict[str, Any] = dict(
        criteria_row=f"HFDS {row['table_id']}: {row['area_name']}", page=f"HFDS table {row['table_id']}, page {row['printed_page']} (PDF page {row['pdf_page']})",
        min_oa_ach=_f(row.get("oa_ach")), min_total_ach=_f(row.get("total_ach")), exhaust_ach=_f(row.get("exhaust_ach")),
        temp_setpoint_c=sp,
        temp_min_c=tmin, temp_max_c=tmax, rh_max_pct=_f(row.get("rh_max_pct")), pressure_regime=regime,
        pressure_pa_booklet=_f(row.get("pressure_pa")), filter=row.get("filter_norm") or None,
        single_pass="Y" if exh_out else None, source=source_id, code="D",
        remark=(f"HFDS row '{row['area_name']}' (table {row['table_id']}, page {row['printed_page']}). {why}. The room has no booklet row. Confirm the mapping."))
    # C-1 (D-064 item 6, D-011): an HFDS row that prints 'Refer to SS553' for outdoor air gives 0 L/s. Circulation rows take the D-011 default
    # (air changes). Every other such row takes the per-person and per-area rate of the booklet office rows (D-033, code A, to confirm).
    note = " ".join(str(row.get(k) or "") for k in ("oa_ach_note", "total_ach_note", "oa_total_ach_as_printed", "oa_ach_as_printed"))
    if re.search(r"SS\s*553", note, re.I) and crit.get("min_oa_ach") is None and "oa_lps_per_person" not in crit:
        if kind == "circulation":
            from . import conf
            circ = conf.config().get("circulation_default_criteria") or {}
            crit["min_oa_ach"] = float(circ.get("min_oa_ach", 2))
            if crit.get("min_total_ach") is None:
                crit["min_total_ach"] = float(circ.get("min_total_ach", 6))
            crit["code"] = "A"
            crit["remark"] += (" The HFDS row prints 'Refer to SS553' for outdoor air. The circulation default of the project design basis is used "
                               f"({crit['min_oa_ach']:g} outdoor-air and {crit['min_total_ach']:g} total air changes). Assumption, to confirm.")
        else:
            from .assemble import _office_oa_rates
            pp, pa = _office_oa_rates()
            if pp:
                crit["oa_lps_per_person"] = pp
            if pa:
                crit["oa_lps_per_m2"] = pa
            if pp or pa:
                crit["code"] = "A"
                crit["remark"] += (" The HFDS row prints 'Refer to SS553' for outdoor air. The rate of the booklet office rows is used "
                                   f"({(pp or 0):g} L/s per person or {(pa or 0):g} L/s per m², whichever is higher). Assumption, to confirm.")
        crit["oa_ss553"] = True
    return {k: v for k, v in crit.items() if v is not None}


def source_fields(row: Dict[str, str]) -> Dict[str, str]:
    return dict(document=HFDS_DOC, issue="uploaded 23 Nov 2026", page=f"table {row['table_id']}, page {row['printed_page']} (PDF page {row['pdf_page']})",
                location=f"row {row.get('row_number', '')}{row.get('sub_item', '')} '{row['area_name']}'",
                text_as_read=(row.get("text_as_printed") or "")[:300], method="Value read from the HFDS criteria table", confidence=row.get("confidence", "high"))


def same_type_row(root: Path, name: str) -> Optional[Dict[str, str]]:
    """The HFDS main-table row of the room type, for the D-064 comparison with a mapped or presumed booklet row."""
    hit = find_row(root, name)
    return hit[0] if hit else None


def compare(crit: Dict[str, Any], row: Dict[str, str]) -> List[str]:
    """Differences of a mapped or presumed row from the HFDS row of the same room type (pressure direction, total and outdoor air changes, exhaust)."""
    diffs: List[str] = []
    reg = (row.get("pressure_regime") or "").strip().upper()
    mine = str(crit.get("pressure_regime") or "").upper()
    if reg in ("POSITIVE", "NEGATIVE", "NEUTRAL") and mine and reg != mine:
        diffs.append(f"pressure {mine.lower()} against {reg.lower()}")
    for key, hk, lab in (("min_total_ach", "total_ach", "total air changes"), ("min_oa_ach", "oa_ach", "outdoor air changes"), ("exhaust_ach", "exhaust_ach", "exhaust air changes")):
        a, b = crit.get(key), _f(row.get(hk))
        if b is not None and a is not None and abs(float(a) - b) > 1e-9:
            diffs.append(f"{lab} {float(a):g} against {b:g}")
    return diffs
