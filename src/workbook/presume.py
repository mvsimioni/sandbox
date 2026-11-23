"""Presumed criteria (D-026): a room with no criteria row in its own department takes the row of the same space in the closest other department.

Order of the match (``presumed_criteria.match_order`` of the project configuration):

1. ``same_room_type_code``: a room data sheet of another department has the same room type code, and the room type that it names has a criteria row there;
2. ``same_name``: a criteria row of another department prints the same room type name (or lists the name inside a multi-name row);
3. ``same_use_class``: a criteria row of another department whose room type has the same room use (the use rules that give the default occupancy) AND the same
   main word in the room type name (the last word of the room name before its qualifier: 'Toilet - Accessible' and 'Toilet', 'Office - Typical' and 'MMD Office').
   A row that has only the use class in common ('Space for Roll Call' and a laboratory header row, 'Bay - Linen' and a dirty utility row) is not the same space and
   is not taken: the room then has no criteria row and is listed with the reason.

Before these steps the booklet of the room's own department is read (``find_own``): a row of the own booklet that prints the same space with a level or department
prefix, or with a number in brackets ('L6 Admin Workspace Office' for 'Office - Typical'; 'Lecture Room (64)' for 'Lecture Room') is the row of the room. It is a
mapped row of the own booklet (code A, to confirm), not a presumed row: D-026 applies only where the own department has no row for the space.

Closest department: clinical rooms prefer clinical departments (departments 01 to 29 of the booklets, Laboratory Medicine included); then the closest room type name;
then the department whose booklet prints most of the same room types as the booklet of the room's own department; then the lower department number. A plant room takes
a row only by the first two steps: no row is presumed for a plant room from its use class (the criteria of plant rooms are open for Marcus). The booklet and the page
of the row stay as the source.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import conf

REG = Path("out/registers")
CLINICAL_MAX = 29        # departments 01 to 29 are clinical (Laboratory Medicine, 29, included)
CLINICAL_USES = {"patient_room", "medical_procedure", "operating_room", "recovery_icu", "physical_therapy"}

STOP_WORDS = {"room", "rooms", "rm", "area", "bay", "type", "typical", "and", "the", "of", "for", "a", "an", "with", "no", "sqm", "m2", "future", "space", "zone", "up", "out", "in", "on", "off", "to", "at"}
SYNONYM = {"ensuite": "toilet", "wc": "toilet", "lavatory": "toilet", "storeroom": "store", "storage": "store", "collaboration": "collab", "conference": "meeting",
           "lect": "lecture", "cleaners": "cleaner"}


def _norm(s: Optional[str]) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _dk(dept_no: str) -> str:
    return str(int(re.sub(r"\D", "", dept_no) or 0))


def _stem(w: str) -> str:
    w = SYNONYM.get(w, w)
    if len(w) > 3 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    return SYNONYM.get(w, w)


def _tokens(text: str, drop_stop: bool = True) -> List[str]:
    out: List[str] = []
    for w in re.findall(r"[a-z]+", (text or "").lower()):
        w = _stem(w)
        if drop_stop and w in STOP_WORDS:
            continue
        out.append(w)
    return out


def _base(name: str) -> str:
    """The room name before its qualifier: 'Toilet - Accessible' gives 'Toilet'; 'Patient Bay - Holding, 9 sqm' gives 'Patient Bay'."""
    return re.split(r"\s+[-–]\s+|,\s*|\s*\(", name or "")[0].strip()


def head_word(name: str) -> Optional[str]:
    """The main word of a room name: the last content word of the name before the qualifier, else the last content word of the whole name."""
    t = _tokens(_base(name)) or _tokens(name)
    return t[-1] if t else None


def _alternatives(row: Dict[str, str]) -> List[str]:
    t = row.get("room_type_as_printed") or ""
    alts = [t] + [x for x in re.split(r"\s*/\s*|,\s*|\s+&\s+|\s*\|\s*", t) if x.strip()]
    return [a.strip() for a in alts if a.strip()]


def _page(row: Dict[str, str]) -> float:
    try:
        return float(row.get("booklet_page") or 0)
    except ValueError:
        return 0.0


# A row at negative pressure or on single pass is a row for a toilet, a dirty or an isolation space. A room takes such a row from its use class only where its own
# name says that it is such a space.
HAZARD_CUE = re.compile(r"toilet|ensuite|\bwc\b|shower|change|bath|dirty|soiled|cleaner|disposal|sluice|isolation|\biso\b|\bnp\b|negative|decontam|waste|refuse|contamin|sanit|lavator|bedpan|specimen|gown", re.I)


def hazardous(row: Dict[str, str]) -> bool:
    return str(row.get("single_pass") or "").upper().startswith("Y") or "NEG" in str(row.get("pressure_regime_as_printed") or "").upper()


_LEVEL = re.compile(r"^(?:L|B|LB)\d+\b\s*", re.I)


def _own_key(text: str, dept_words: set) -> Tuple[str, ...]:
    """Words of a room type name of the own booklet with the level prefix, the number in brackets and the words of the department name taken away."""
    t = re.sub(r"\(\s*\d+\s*\)", " ", text or "")           # a number in brackets is a count, not a qualifier of the room type
    t = _LEVEL.sub("", t.strip())
    toks = [w for w in _tokens(t, drop_stop=False) if w not in dept_words and w not in ("room", "rm", "type", "typical")]
    return tuple(toks)


class Presumption:
    """Index of the criteria rows of every department, built once from ``out/registers/criteria.csv`` and the room data sheets."""

    def __init__(self, root: Path, room_use: Callable[[str], Tuple[Optional[str], str]], split_code: Callable[[Optional[str]], Tuple[str, str, str]],
                 expand: Optional[Callable[[str], str]] = None):
        self.root = Path(root)
        self.room_use = room_use
        self.split_code = split_code
        self.expand = expand or (lambda s: s)
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
        # rows by use class, with the main words of every name that the row prints
        self.by_use: Dict[str, List[Tuple[str, Dict[str, str], List[set]]]] = defaultdict(list)
        for d, rs in self.by_dept.items():
            for r in rs:
                alts = _alternatives(r)
                uses = set()
                for a in alts:
                    u, _ = room_use(a)
                    if u:
                        uses.add(u)
                words = [set(_tokens(a)) for a in alts]          # the words of each name that the row prints
                for u in uses:
                    self.by_use[u].append((d, r, words))
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

    # ------------------------------------------------------------------ own booklet
    def find_own(self, own_dept: str, name: str, storey: Optional[str] = None, dept_name: str = "") -> Optional[Dict[str, Any]]:
        """Row of the own booklet that prints the same space with a level or department prefix, or a number in brackets: {row, how, reason} or None.

        The words of the row name, after the level prefix, the brackets and the words of the department name are taken away, are the words of the room name before its
        qualifier. A row of the level of the room is preferred; else the row with the lowest page (the row of another level: the reason says so)."""
        own = _dk(own_dept)
        dwords = set(_tokens(dept_name, drop_stop=False))
        want = _own_key(_base(name), set())
        if not want:
            return None
        lvl = None
        m = re.match(r"^L0*(\d+)$", storey or "")
        if m:
            lvl = f"L{int(m.group(1))}"
        cands: List[Tuple[Tuple[int, float], Dict[str, str], str, str]] = []
        for r in self.by_dept.get(own, []):
            for a in _alternatives(r):
                if _own_key(a, dwords) != want:
                    continue
                pm = _LEVEL.match(a.strip())
                prefix = pm.group(0).strip().upper() if pm else ""
                same_level = bool(lvl and prefix == lvl)
                no_prefix = not prefix
                cands.append(((0 if same_level else 1 if no_prefix else 2, _page(r)), r, prefix, a))
        if not cands:
            return None
        cands.sort(key=lambda c: c[0])
        (rank, _pg), row, prefix, alt = cands[0]
        if row["room_type_as_printed"].strip().lower() == (name or "").strip().lower():
            return None            # the same name is matched elsewhere
        if rank == 0:
            why = f"the booklet of this department prints the same space for level {prefix[1:]}"
        elif rank == 1:
            why = "the booklet of this department prints the same space"
        else:
            why = f"the booklet of this department prints the same space for another level ({prefix})"
        return dict(row=row, how="same space in the booklet of this department", reason=why)

    # ------------------------------------------------------------------ other departments
    def _best(self, own: str, cands: List[Tuple[str, Dict[str, str]]], clinical: bool, score: Optional[Callable[[Dict[str, str]], float]] = None):
        cands = [(d, r) for d, r in cands if d != own]
        if not cands:
            return None
        own_clinical = int(own) <= CLINICAL_MAX

        def key(c):
            d, r = c
            cand_clinical = int(d) <= CLINICAL_MAX
            group = (0 if cand_clinical else 1) if clinical else (0 if cand_clinical == own_clinical else 1)       # a clinical room prefers clinical departments
            sc = -round(score(r), 3) if score else 0.0
            return (group, sc, -round(self.similarity(own, d), 6), int(d), _page(r))
        return sorted(cands, key=key)[0]

    def find(self, own_dept: str, name: str, code: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], str]:
        """({row, department, how, department_label}, reason). Reason is given when no row is found."""
        own = _dk(own_dept)
        forms = list(dict.fromkeys([name, self.expand((name or "").lower())]))
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
                for form in forms:
                    cands = [(d, r) for d, r, _how in self.by_name.get(_norm(form), [])]
                    hit = self._best(own, cands, clinical)
                    if hit:
                        how = next(h for d, r, h in self.by_name[_norm(form)] if d == hit[0] and r is hit[1])
                        return dict(row=hit[1], department=hit[0], how=how), ""
            elif step == "same_use_class":
                if plant:
                    why.append("a plant room takes no row from its use class (the criteria of plant rooms are open for Marcus)")
                    continue
                if use is None:
                    why.append("the room name does not give a room use, so no use class fits it")
                    continue
                head = head_word(forms[-1])
                mine = set(_tokens(forms[-1]))
                pool = [(d, r, [x for x in w if head in x]) for d, r, w in self.by_use.get(use, []) if head and any(head in x for x in w)]
                n_before = len(pool)
                if not HAZARD_CUE.search(forms[-1]):
                    pool = [x for x in pool if not hazardous(x[1])]
                if not pool and n_before:
                    why.append(f"the rows of the use class '{use.replace('_', ' ')}' with the main word '{head}' are negative-pressure or single-pass rows, and the room name does not say that it is a toilet, dirty or isolation space")
                    continue
                if not pool:
                    other = [d for d, r, w in self.by_use.get(use, []) if d != own]
                    why.append((f"no other department prints a row of the use class '{use.replace('_', ' ')}' with the main word '{head}' in its room type name"
                                if other else f"no other department has a criteria row for the use class '{use.replace('_', ' ')}'"))
                    continue
                sc = {id(r): max(len(mine & x) + len(mine & x) / (len(mine | x) or 1) for x in w) for d, r, w in pool}      # most shared words first, then the closest name
                hit = self._best(own, [(d, r) for d, r, w in pool], clinical, lambda r: sc.get(id(r), 0.0))
                if hit:
                    return dict(row=hit[1], department=hit[0],
                                how=f"same use class ({use.replace('_', ' ')}) and the same main word '{head}' in the room type name"), ""
                why.append(f"only the own department prints a row of the use class '{use.replace('_', ' ')}' with the main word '{head}'")
        base = "no other department has a criteria row for this room type name or code"
        return None, base + ("; " + "; ".join(dict.fromkeys(why)) if why else "")
