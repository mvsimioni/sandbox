"""Input model: loading, validation and the cell layouts shared by every tab."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from openpyxl.utils import get_column_letter

SCHEMA_VERSION = 1
CODES = {"D", "M", "S", "A"}


class InputError(ValueError):
    """The department input breaks the schema or the traceability rule."""


class Layout:
    """Column map, first data row and size of one table on one sheet."""

    def __init__(self, sheet: str, keys: List[str], first_row: int, n: int, start_col: int = 1):
        self.sheet = sheet
        self.keys = list(keys)
        self.first = first_row
        self.n = n
        self.start = start_col
        self.idx = {k: i + start_col for i, k in enumerate(self.keys)}
        if len(self.idx) != len(self.keys):
            raise ValueError(f"duplicate keys on {sheet}")

    @property
    def last(self) -> int:
        return self.first + max(self.n, 1) - 1

    @property
    def ncols(self) -> int:
        return self.start + len(self.keys) - 1

    def col(self, key: str) -> str:
        return get_column_letter(self.idx[key])

    def row(self, i: int) -> int:
        return self.first + i

    def local(self, key: str, i: int) -> str:
        return f"{self.col(key)}{self.row(i)}"

    def addr(self, key: str, i: int) -> str:
        """Absolute-sheet reference to one cell, e.g. 'Airflow'!X12."""
        return f"'{self.sheet}'!{self.col(key)}{self.row(i)}"

    def rng(self, key: str, local: bool = False) -> str:
        c = self.col(key)
        r = f"${c}${self.first}:${c}${self.last}"
        return r if local else f"'{self.sheet}'!{r}"

    def upto(self, key: str, i: int) -> str:
        """Local range from the first row to row i (running totals)."""
        c = self.col(key)
        return f"${c}${self.first}:{c}{self.row(i)}"


def load_input(path: Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def prov_of(room: Dict[str, Any], group: str, value_present: bool = True, field: Optional[str] = None) -> Tuple[str, str]:
    """(code, source id) for a room input; a provenance keyed by the field name overrides the one of its group.
    Blank pair when the value is unknown."""
    if not value_present:
        return "", ""
    prov = room.get("prov") or {}
    p = (prov.get(field) if field else None) or prov.get(group) or room.get("prov_default")
    if not p:
        raise InputError(f"room {room.get('number')}: input '{group}' has a value but no provenance (code, source id)")
    code, src = p[0], p[1]
    if code not in CODES:
        raise InputError(f"room {room.get('number')}: provenance code '{code}' for '{group}' is not one of D M S A")
    return code, src


def validate(data: Dict[str, Any]) -> None:
    """Structural checks that keep the traceability rule: every value has a source ID that resolves."""
    for key in ("department", "rooms", "sources", "constants"):
        if key not in data:
            raise InputError(f"missing top-level key '{key}'")
    ids = [s["id"] for s in data["sources"]]
    if len(ids) != len(set(ids)):
        raise InputError("duplicate source ids")
    known = set(ids)
    numbers = [r["number"] for r in data["rooms"]]
    if len(numbers) != len(set(numbers)):
        raise InputError("duplicate room numbers")

    def need(src: Optional[str], where: str) -> None:
        if src is not None and src != "" and src not in known:
            raise InputError(f"{where}: source id '{src}' does not resolve on Sources")

    for c in data["constants"].values():
        if c.get("source") is None:
            raise InputError(f"constant {c.get('label')} has no source id")
        need(c["source"], f"constant {c.get('label')}")
    for r in data["rooms"]:
        for grp, p in (r.get("prov") or {}).items():
            need(p[1], f"room {r['number']} {grp}")
        crit = r.get("criteria") or {}
        if crit:
            need(crit.get("source"), f"room {r['number']} criteria")
        for e in r.get("envelope", []):
            need(e.get("source"), f"room {r['number']} envelope")
        pr = r.get("pressure") or {}
        for d in pr.get("doors", []):
            need(d.get("source"), f"room {r['number']} door")
    for t in ("space_types", "activities", "orientations"):
        for row in data.get(t, []):
            need(row.get("source"), f"table {t}")


def blank(v: Any) -> bool:
    return v is None or v == ""
