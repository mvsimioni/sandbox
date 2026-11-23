"""Headless LibreOffice recalculation and PDF export."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

SOFFICE = shutil.which("soffice") or "/usr/bin/soffice"


def _run(args, timeout=300):
    with tempfile.TemporaryDirectory(prefix="lo_profile_") as prof:
        cmd = [SOFFICE, f"-env:UserInstallation=file://{prof}", "--headless", "--norestore", *args]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return res


def set_application(path: Path, name: str = "Microsoft Excel") -> None:
    """Write the application name of the file properties (docProps/app.xml). The recalculation tool writes its own name there: the issued files carry the
    name of the spreadsheet format instead (D-015: no tool name in a deliverable)."""
    path = Path(path)
    tmp = path.with_suffix(".tmp.xlsx")
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "docProps/app.xml":
                txt = data.decode("utf-8")
                txt = re.sub(r"<Application>.*?</Application>", f"<Application>{name}</Application>", txt, flags=re.S)
                data = txt.encode("utf-8")
            elif item.filename == "docProps/core.xml":                  # British English, as the deliverable text is
                txt = data.decode("utf-8")
                if "<dc:language>" in txt:
                    txt = re.sub(r"<dc:language>.*?</dc:language>", "<dc:language>en-GB</dc:language>", txt, flags=re.S)
                else:
                    txt = txt.replace("</cp:coreProperties>", "<dc:language>en-GB</dc:language></cp:coreProperties>")
                data = txt.encode("utf-8")
            elif item.filename == "xl/styles.xml":                      # the recalculation renames the styles that clash with its own style names ('Note' to 'Note 2'): the house names are put back
                txt = data.decode("utf-8")
                for new, old in (("Note", "Note 2"), ("Result", "Result 3"), ("Text", "Text 1")):
                    if f'<cellStyle name="{new}"' not in txt:
                        txt = txt.replace(f'<cellStyle name="{old}"', f'<cellStyle name="{new}"')
                data = txt.encode("utf-8")
            zout.writestr(item, data)
    tmp.replace(path)


def recalc(src: Path, out_dir: Path) -> Path:
    """Open in LibreOffice (which computes every formula) and save as xlsx with cached values."""
    out_dir.mkdir(parents=True, exist_ok=True)
    res = _run(["--convert-to", "xlsx:Calc MS Excel 2007 XML", "--outdir", str(out_dir), str(src)])
    out = out_dir / Path(src).name
    if not out.exists():
        raise RuntimeError(f"recalculation failed: {res.stdout} {res.stderr}")
    set_application(out)
    return out


def to_pdf(src: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    res = _run(["--convert-to", "pdf", "--outdir", str(out_dir), str(src)], timeout=600)
    out = out_dir / (Path(src).stem + ".pdf")
    if not out.exists():
        raise RuntimeError(f"pdf export failed: {res.stdout} {res.stderr}")
    return out
