"""Project configuration read for the workbook generator (config/project.yaml)."""

from __future__ import annotations

import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

import yaml

ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=4)
def _load(path: str) -> Dict[str, Any]:
    try:
        return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def config(root: Path = ROOT) -> Dict[str, Any]:
    return _load(str(Path(root) / "config" / "project.yaml"))


def forbidden_terms(root: Path = ROOT) -> List[str]:
    return [str(t) for t in ((config(root).get("deliverable_language") or {}).get("forbidden_terms") or [])]


def occupancy_cfg(root: Path = ROOT) -> Dict[str, Any]:
    return (config(root).get("occupancy") or {}).get("density_default") or {}


def occupancy_style(root: Path = ROOT) -> Dict[str, Any]:
    st = occupancy_cfg(root).get("style") or {}
    return dict(font_colour=st.get("font_colour", "FFC00000"), named_style=st.get("named_style", "Assumption-Occupancy"),
                code=st.get("code", "A"))


def people(root: Path = ROOT) -> Dict[str, str]:
    """Author, checker and approver of the revision block: blank until the project input gives them."""
    p = (config(root).get("workbook") or {}).get("people") or {}
    return dict(by=str(p.get("author") or ""), checked=str(p.get("checked_by") or ""), approved=str(p.get("approved_by") or ""))


def revision_date(root: Path = ROOT) -> str:
    given = (config(root).get("workbook") or {}).get("revision_date")
    return str(given) if given else datetime.date.today().isoformat()
