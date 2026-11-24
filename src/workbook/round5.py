"""Round 5 rules applied when a department input is assembled (decisions D-035 to D-067, with the simplifications of D-071).

Each function is small and documented with the decision it carries. The two calculation engines (workbook formulas and src/recompute) read the result.
"""

from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import conf

REG = Path("out/registers")


def _read(p: Path) -> List[Dict[str, str]]:
    if not Path(p).exists():
        return []
    with Path(p).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _f(v: Any) -> Optional[float]:
    try:
        return float(str(v).replace(",", "")) if str(v).strip() not in ("", "None") else None
    except ValueError:
        return None


# --------------------------------------------------------------------------------------------- #
# D-051 / D-052: the unit of a room comes from out/registers/room_unit_links.csv when it exists.
LINK_FILE = REG / "room_unit_links.csv"
NO_UNIT = re.compile(r"^(no information|none|n/?a|-|)$", re.I)


@lru_cache(maxsize=4)
def _links(root: str) -> Optional[Dict[Tuple[str, str], Dict[str, str]]]:
    p = Path(root) / LINK_FILE
    if not p.exists():
        return None
    out: Dict[Tuple[str, str], Dict[str, str]] = {}
    for r in _read(p):
        dept = (r.get("dept") or "").strip().lstrip("0") or "0"
        out[(dept, (r.get("dpa_room_id") or r.get("room") or r.get("room_id") or "").strip())] = r
    return out


def links_available(root: Path = Path(".")) -> bool:
    return _links(str(root)) is not None


STEP_TEXT = {
    "0a": "RFI-ME-0233 zoning of the diagnostic imaging units", "0b": "MOT NSC cross-reference workbook", "0c": "ruling of the engineer for electrical, IT, LAN, UPS and battery rooms",
    "1": "DD Booklet room list, zone under the room label", "2": "plan room link", "3": "air-side schematic room table", "4": "unit of the parent group",
    "5": "functional group with one unit", "6": "Appendix D space", "7": "plan room zoning of the common areas",
}
_PROCESS = re.compile(r"\b(council|ruling|decision)\b|\bD-\d{3}\b", re.I)


def _plain(text: str) -> str:
    """Link-table text for a deliverable: process words and decision numbers are taken out."""
    t = re.sub(r"council\s+ruling\s+D-\d+(?:\s*/\s*D-\d+)*", "ruling of the engineer", text or "", flags=re.I)
    return _PROCESS.sub("", t).replace("  ", " ").strip()


def apply_unit_links(rooms: List[Dict[str, Any]], dept: str, root: Path, add_src: Callable[..., str], ahu_kinds: Dict[str, str]) -> Dict[str, int]:
    """Set the unit of every room from the link table (the only interface between zoning and the workbooks). A room that the table leaves with no unit keeps
    no unit; a fan coil duty and standby pair is a system class (FCU, tag to confirm) and not a unit tag; a group of units is one unit row. Returns counts by basis.
    Without the table nothing changes (the earlier linking of the assembly stays as the fallback)."""
    links = _links(str(root))
    if links is None:
        return {}
    key = str(dept).lstrip("0") or "0"
    counts: Dict[str, int] = {}
    for room in rooms:
        row = links.get((key, room["number"]))
        if row is None:
            continue
        unit = (row.get("unit_class") or "").strip()
        step = (row.get("step") or "").strip()
        basis = STEP_TEXT.get(step, "") or _plain(row.get("basis") or "")
        counts[basis or "no link"] = counts.get(basis or "no link", 0) + 1
        kind = (row.get("unit_kind") or "").upper()
        if NO_UNIT.match(unit):
            if room.get("ahu") and room.get("plan_room_id"):        # D-092: the unit of the plan room that holds the space stays; the table gives no unit
                room["remark"] = (room.get("remark") or "").rstrip() + " The link table gives no unit; the unit of the plan room that holds the space is kept (D-092)."
                continue
            room["ahu"] = None
            continue
        if kind == "CLASS" or not re.match(r"^(AHU|PAHU|FCU|DXFCU|CRAC|FAN|OAF|EAF)", unit, re.I):
            room["ahu"] = None
            room["system_type"] = unit.upper()
            continue
        sid = add_src(f"LINK-{re.sub(r'[^A-Za-z0-9]', '', room['number'])}"[:60], document="Room-to-unit link table of the project", issue="Rev A", page=f"link basis {step}",
                      location=f"room {room['number']}", text_as_read=_plain(f"unit {unit}; basis {basis}; reference {row.get('source_ref', '')}")[:300],
                      method="Unit of the room from the link of the room list to the zoning, in the order of the project documents", confidence=str(row.get("confidence", "")))
        if re.match(r"^FCU duty/standby|\(tag to confirm\)", unit, re.I) and not re.match(r"^FCU-", unit):
            room["ahu"] = None
            room["system_type"] = "FCU"
            room["remark"] = (room.get("remark") or "").rstrip() + f" Served by a fan coil unit pair ({unit}); link basis: {basis}."
        else:
            room["ahu"] = unit
            if kind in ("FCU", "DXFCU"):
                room["system_type"] = "FCU"
            room["remark"] = (room.get("remark") or "").rstrip() + f" Unit {unit} by link basis: {basis}."
            ahu_kinds.setdefault(unit, "AHU" if kind.startswith("AHU") or not kind else kind)
        room["prov"]["zoning"] = [(row.get("code") or "A")[:1], sid]
        room["gaps"].pop("ahu", None)
    return counts


# --------------------------------------------------------------------------------------------- #
# D-043: provisional equipment W/m2 (red, Remarks, flagged for vendor data)
@lru_cache(maxsize=4)
def _equip_table(root: str) -> List[Tuple[str, float, "re.Pattern[str]"]]:
    cfg = conf.config(Path(root)).get("equipment_provisional_w_m2_table") or {}
    return [(k, float(v["w_m2"]), re.compile(v["pattern"], re.I)) for k, v in cfg.items()]


def equipment_provisional(name: str, root: Path = Path(".")) -> Optional[Tuple[str, float]]:
    """(use, W/m2) of the provisional equipment gain for an equipment-led room, or None."""
    for use, w, rx in _equip_table(str(root)):
        if rx.search(name or ""):
            return use, w
    return None


# --------------------------------------------------------------------------------------------- #
# D-054 / D-055: tender base and second room list
def tender_base_rule(a: Dict[str, Any], sched_val: Optional[float], schematic_val: Optional[float], root: Path = Path("."), is_mot: bool = False) -> Tuple[str, str]:
    """(reason that no reduction is stated, note for the Remarks) from the tender base of the unit.
    D-055: schedule vs schematic difference <= 2 %: schedule stands; 2 to 10 %: schedule stands, both values printed, VERIFY; above 10 %: no reduction;
    two printed values and no schedule: tender blank, no reduction; a schedule supply of 9,000 L/s is a possible placeholder: no reduction.
    D-057: no reduction is stated for an MOT unit (MOT equipment is not resized)."""
    cfg = (conf.config(Path(root)).get("reduction_round5") or {})
    lim = cfg.get("tender_diff_pct") or {}
    acc, ver = float(lim.get("accept_up_to", 2)), float(lim.get("verify_up_to", 10))
    placeholder = float(cfg.get("placeholder_supply_lps", 9000))
    reason, note = "", ""
    if is_mot:
        return "MOT equipment is not resized: the reduction is not shown for this unit", ""
    others = a.get("tender_other_values") or []
    if sched_val is None:
        if len({round(v) for v, _ in others}) > 1:
            vals = " and ".join(f"{v:,.0f}" for v, _ in others)
            return f"the sheets of the tender print two values ({vals} L/s) and the Equipment Schedule has none: no value is chosen", ""
        return "", ""
    if abs(sched_val - placeholder) < 0.5:
        reason = (f"the Equipment Schedule prints {placeholder:,.0f} L/s for this unit, which may be a placeholder: the reduction is not shown until the print is read again")
        return reason, reason + ". VERIFY"
    if schematic_val is not None and sched_val > 0:
        d = abs(sched_val - schematic_val) / sched_val * 100.0
        if d > ver:
            reason = (f"the Equipment Schedule prints {sched_val:,.0f} L/s and the tender schematic prints {schematic_val:,.0f} L/s ({d:.0f} % apart)")
            return reason, reason + ". The schedule value is in the tender column. VERIFY"
        if d > acc:
            note = (f"The Equipment Schedule prints {sched_val:,.0f} L/s and the tender schematic prints {schematic_val:,.0f} L/s ({d:.0f} % apart). The schedule value is used. VERIFY")
    return "", note


def second_room_list(tag_candidates: List[str], sched_rooms: List[Dict[str, str]]) -> Optional[Tuple[int, str]]:
    """(rooms, note) of the room table of the tender air-side schematic for the unit: sum of the typical counts of the room boxes that carry the unit tag."""
    n, sheets = 0, set()
    for s in sched_rooms:
        tags = [t.strip() for t in (s.get("ahu_tag") or "").split(";")]
        if any(t in tags for t in tag_candidates):
            try:
                n += max(1, int(float(s.get("typical_count") or 1)))
            except ValueError:
                n += 1
            sheets.add(s.get("sheet", ""))
    return (n, ", ".join(sorted(x for x in sheets if x))) if n else None


# --------------------------------------------------------------------------------------------- #
# D-039, D-057, D-058: operating theatres from the HEPA terminals
class MotHepa:
    """Theatres with a laminar-flow ceiling module: design supply = face area x 0.35 m/s (the HEPA schedule), capability = face area x 0.40 m/s."""

    def __init__(self, root: Path = Path(".")):
        cfg = conf.config(Path(root)).get("mot_round5") or {}
        self.v_design = float(cfg.get("design_face_velocity_m_s", 0.35))
        self.v_cap = float(cfg.get("capability_face_velocity_m_s", 0.40))
        self.future = {str(k): float(v) for k, v in (cfg.get("future_ucv_room_supply_lps") or {}).items()}
        rows = _read(Path(root) / REG / "mot_ot_airflow.csv")
        self.rows = [r for r in rows if r.get("room_kind") == "operating theatre" and _f(r.get("face_area_m2_total")) and _f(r.get("face_velocity_used_m_s"))]
        self.root = Path(root)

    @staticmethod
    def _tags(r: Dict[str, str]) -> List[str]:
        return [t.strip().replace("lSONP", "ISONP") for t in re.split(r"[;|]", r.get("ahu_tags_schematic") or "") if t.strip()]

    def for_tags(self, tags: List[str]) -> Optional[Dict[str, Any]]:
        want = {t.replace("lSONP", "ISONP") for t in tags if t}
        for r in self.rows:
            if want & set(self._tags(r)):
                area = _f(r["face_area_m2_total"])
                return dict(row=r, face_area=area, design=round(area * self.v_design * 1000.0, 1), capability=round(area * self.v_cap * 1000.0, 1),
                            terminal=r["terminals_used"], scheduled=_f(r.get("scheduled_terminal_air_flow_l_s")), module=r["module_sizes_as_printed"])
        return None

    def check_text(self, spec: Dict[str, Any]) -> str:
        sch = spec.get("scheduled")
        txt = (f"HEPA terminal {spec['terminal']} ({spec['module']} module, face area {spec['face_area']:.2f} m²): design supply {spec['design']:,.0f} L/s at {self.v_design:g} m/s "
               f"(the Max Face Velocity of the HEPA schedule); capability {spec['capability']:,.0f} L/s at {self.v_cap:g} m/s")
        if sch:
            dev = (spec["design"] - sch) / sch * 100.0
            txt += f"; the HEPA schedule prints {sch:,.0f} L/s ({dev:+.1f} %)" + ("" if abs(dev) <= 1.0 else ". VERIFY")
        return txt + "."
