"""Presumed criteria (D-026): a room with no criteria row in its own department takes the row of the same space in the closest other department.

Order of the match (``presumed_criteria.match_order`` of the project configuration):

1. ``same_room_type_code``: a room data sheet of another department has the same room type code, and the room type that it names has a criteria row there;
2. ``same_name``: a criteria row of another department prints the same room type name (or lists the name inside a multi-name row);
3. ``same_use_class``: a criteria row of another department whose room type has the same room use (the use rules that give the default occupancy).

Closest department: clinical rooms prefer clinical departments (departments 01 to 28 of the booklets); then the department whose booklet prints most of the same
room types as the booklet of the room's own department (a non-clinical room prefers departments of its own kind); then the lower department number. A plant room takes a row only by the first two steps: no row is
presumed for a plant room from its use class (the criteria of plant rooms are open for Marcus). The booklet and the page of the row stay as the source.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import conf

REG = Path("out/registers")
CLINICAL_MAX = 28
CLINICAL_USES = {"patient_room", "medical_procedure", "operating_room", "recovery_icu", "physical_therapy"}


def _norm(s: Optional[str]) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _dk(dept_no: str) -> str:
    return str(int(re.sub(r"\D", "", dept_no) or 0))


def _alternatives(row: Dict[str, str]) -> List[str]:
    t = row.get("room_type_as_printed") or ""
    alts = [t] + [x for x in re.split(r"\s*/\s*|,\s*|\s+&\s+", t) if x.strip()]
    return [a.strip() for a in alts if a.strip()]


def _page(row: Dict[str, str]) -> float:
    try:
        return float(row.get("booklet_page") or 0)
    except ValueError:
        return 0.0


class Presumption:
    """Index of the criteria rows of every department, built once from ``out/registers/criteria.csv`` and the room data sheets."""

    def __init__(self, root: Path, room_use: Callable[[str], Tuple[Optional[str], str]], split_code: Callable[[Optional[str]], Tuple[str, str, str]]):
        self.root = Path(root)
        self.room_use = room_use
        self.split_code = split_code
        self.order = list((conf.presumed_cfg(root).get("match_order")) or ["same_room_type_code", "same_name", "same_use_class"])
        with (self.root / REG / "criteria.csv").open(newline="", encoding="utf-8") as fh:
            self.rows = list(csv.DictReader(fh))
        self.by_dept: Dict[str, List[Dict[str, str]]] = defaultdict(list)
        for r in self.rows:
            self.by_dept[_dk(r["dept_no"])].append(r)
        self.names: Dict[str, set] = {d: {_norm(a) for r in rs for a in _alternatives(r)} for d, rs in self.by_dept.items()}
        self.by_name: Dict[str, List[Tuple[str, Dict[str, str], str]]] = defaultdict(list)
        for d, rs in self.by_dept.items():
            for r in rs:
                for k, a in enumerate(_alternatives(r)):
                    self.by_name[_norm(a)].append((d, r, "same name" if k == 0 else "same name, listed inside a multi-name booklet row"))
        self.by_use: Dict[str, List[Tuple[str, Dict[str, str]]]] = defaultdict(list)
        for d, rs in self.by_dept.items():
            for r in rs:
                first = re.split(r"\s*/\s*|,\s*|\s+&\s+", r["room_type_as_printed"] or "")[0]       # the room use is the one of the first name of the row
                use, _ = room_use(first)
                if not use:
                    use, _ = room_use(r["room_type_as_printed"])
                if use:
                    self.by_use[use].append((d, r))
        # room type codes of the room data sheets of every department, with the criteria row that the room type name has in that department
        self.by_code: Dict[str, List[Tuple[str, Dict[str, str]]]] = defaultdict(list)
        p = self.root / REG / "rds_rooms.csv"
        if p.exists():
            with p.open(newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    d = _dk(r.get("dept_no", ""))
                    n, b, _v = split_code(r.get("room_code"))
                    key = _norm(n + b) if (n or b) else ""
                    if not key or d not in self.by_dept:
                        continue
                    hit = [(dd, rr) for dd, rr, how in self.by_name.get(_norm(r.get("room_name")), []) if dd == d]
                    if hit:
                        self.by_code[key].append(hit[0])
        self._sim: Dict[Tuple[str, str], float] = {}

    def similarity(self, a: str, b: str) -> float:
        k = (a, b)
        if k not in self._sim:
            na, nb = self.names.get(a, set()), self.names.get(b, set())
            self._sim[k] = (len(na & nb) / len(na | nb)) if na and nb else 0.0
        return self._sim[k]

    def _best(self, own: str, cands: List[Tuple[str, Dict[str, str]]], clinical: bool) -> Optional[Tuple[str, Dict[str, str]]]:
        cands = [(d, r) for d, r in cands if d != own]
        if not cands:
            return None
        own_clinical = int(own) <= CLINICAL_MAX

        def key(c):
            d, r = c
            cand_clinical = int(d) <= CLINICAL_MAX
            group = (0 if cand_clinical else 1) if clinical else (0 if cand_clinical == own_clinical else 1)       # a clinical room prefers clinical departments
            return (group, -round(self.similarity(own, d), 6), int(d), _page(r))
        return sorted(cands, key=key)[0]

    def find(self, own_dept: str, name: str, code: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], str]:
        """({row, department, how, department_label}, reason). Reason is given when no row is found."""
        own = _dk(own_dept)
        use, _label = self.room_use(name)
        clinical = use in CLINICAL_USES
        plant = use == "plant_room"
        why: List[str] = []
        for step in self.order:
            if step == "same_room_type_code":
                n, b, _v = self.split_code(code)
                key = _norm(n + b) if (n or b) else ""
                hit = self._best(own, self.by_code.get(key, []), clinical) if key else None
                if hit:
                    return dict(row=hit[1], department=hit[0], how="same room type code"), ""
            elif step == "same_name":
                cands = [(d, r) for d, r, _how in self.by_name.get(_norm(name), [])]
                hit = self._best(own, cands, clinical)
                if hit:
                    how = next(h for d, r, h in self.by_name[_norm(name)] if d == hit[0] and r is hit[1])
                    return dict(row=hit[1], department=hit[0], how=how), ""
            elif step == "same_use_class":
                if plant:
                    why.append("a plant room takes no row from its use class (the criteria of plant rooms are open for Marcus)")
                    continue
                if use is None:
                    why.append("the room name does not give a room use, so no use class fits it")
                    continue
                hit = self._best(own, self.by_use.get(use, []), clinical)
                if hit:
                    return dict(row=hit[1], department=hit[0], how=f"same use class ({use.replace('_', ' ')})"), ""
                why.append(f"no other department has a criteria row for the use class '{use.replace('_', ' ')}'")
        base = "no other department has a criteria row for this room type name or code"
        return None, base + ("; " + "; ".join(dict.fromkeys(why)) if why else "")
