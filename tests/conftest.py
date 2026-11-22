"""Pytest configuration: make the project root importable as ``src.*``."""

from __future__ import annotations

import sys

import pytest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(scope="module", autouse=True)
def _restore_aigh_root():
    """Some modules point AIGH_ROOT at a temporary tree (convert_task and Layout.from_root set it directly); undo it when the module ends
    so a later module reads the real config/project.yaml, whatever the order of the suite."""
    import os
    before = os.environ.get("AIGH_ROOT")
    yield
    if before is None:
        os.environ.pop("AIGH_ROOT", None)
    else:
        os.environ["AIGH_ROOT"] = before
