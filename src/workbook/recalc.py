"""Headless LibreOffice recalculation and PDF export."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

SOFFICE = shutil.which("soffice") or "/usr/bin/soffice"


def _run(args, timeout=300):
    with tempfile.TemporaryDirectory(prefix="lo_profile_") as prof:
        cmd = [SOFFICE, f"-env:UserInstallation=file://{prof}", "--headless", "--norestore", *args]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return res


def recalc(src: Path, out_dir: Path) -> Path:
    """Open in LibreOffice (which computes every formula) and save as xlsx with cached values."""
    out_dir.mkdir(parents=True, exist_ok=True)
    res = _run(["--convert-to", "xlsx:Calc MS Excel 2007 XML", "--outdir", str(out_dir), str(src)])
    out = out_dir / Path(src).name
    if not out.exists():
        raise RuntimeError(f"recalculation failed: {res.stdout} {res.stderr}")
    return out


def to_pdf(src: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    res = _run(["--convert-to", "pdf", "--outdir", str(out_dir), str(src)], timeout=600)
    out = out_dir / (Path(src).stem + ".pdf")
    if not out.exists():
        raise RuntimeError(f"pdf export failed: {res.stdout} {res.stderr}")
    return out
