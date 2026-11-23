"""Regenerate every workbook of ``out/loads/workbooks`` from the registers: the department workbooks, the common-areas workbook (D00) and the AHU master summary.

``python3 -m src.workbook.regenerate [--only 04,11,00] [--no-master] [--jobs 4]``

For each department input ``out/loads/inputs/D<nn>.json`` the input is assembled again from the registers, the workbook is built, recalculated (so that the stored
values are present) and written over the issued file; the outdoor-air rule proposals are updated from the recalculated workbook. D00 is built from the Appendix D
spaces. The master summary is built last, from the recalculated department workbooks. Nothing is edited by hand: a change goes into the code and this command runs again.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List

IN_DIR = Path("out/loads/inputs")
WB_DIR = Path("out/loads/workbooks")
_lock = threading.Lock()


def _issued_name(num: str) -> Path:
    hit = [p for p in WB_DIR.glob("AIGH_C2_D*_RevA.xlsx") if re.match(rf"AIGH_C2_D{num}_", p.name)]
    if len(hit) != 1:
        raise SystemExit(f"department {num}: expected one issued workbook in {WB_DIR}, found {len(hit)}")
    return hit[0]


def _publish(rc: Path, dest: Path) -> None:
    with _lock:
        shutil.copy(rc, dest)


def department(num: str) -> str:
    from .assemble import assemble_department, write_link_audit
    from .build import build_workbook
    from .proposals import update_csv
    from .recalc import recalc
    src = IN_DIR / f"D{num}.json"
    data = assemble_department(num, src)
    write_link_audit(data, Path("out/qa") / f"dpa_link_audit_D{data['department']['number']}.csv")
    with tempfile.TemporaryDirectory(prefix=f"regen_{num}_") as tmp:
        built = Path(tmp) / _issued_name(num).name
        ctx = build_workbook(src, built)
        rc = recalc(built, Path(tmp) / "rc")
        with _lock:
            update_csv(rc)
        _publish(rc, _issued_name(num))
    return f"D{num}: {len(ctx.rooms)} rooms"


def common_areas() -> str:
    from .common_areas import build_common_areas
    from .recalc import recalc
    dest = next(WB_DIR.glob("AIGH_C2_D00_*_RevA.xlsx"))
    with tempfile.TemporaryDirectory(prefix="regen_00_") as tmp:
        built = Path(tmp) / dest.name
        res = build_common_areas(built)
        rc = recalc(built, Path(tmp) / "rc")
        _publish(rc, dest)
    return f"D00: {res['rooms']} spaces, {res['units']} units"


def master() -> str:
    from .master import build_master
    from .recalc import recalc
    dest = WB_DIR / "AHU_master_summary.xlsx"
    with tempfile.TemporaryDirectory(prefix="regen_master_") as tmp:
        built = Path(tmp) / dest.name
        res = build_master(WB_DIR, built)
        rc = recalc(built, Path(tmp) / "rc")
        _publish(rc, dest)
    return f"master: {res['departments']} departments, {res['ahus']} AHU rows"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", default=None, help="comma-separated department numbers (00 = common areas)")
    ap.add_argument("--no-master", action="store_true")
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args(argv)
    nums: List[str] = sorted(p.stem[1:] for p in IN_DIR.glob("D*.json"))
    want = [x.strip() for x in a.only.split(",")] if a.only else nums + ["00"]
    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as ex:
        futs = [ex.submit(common_areas if n == "00" else department, *(() if n == "00" else (n,))) for n in want]
        for f in futs:
            print(f.result(), flush=True)
    if not a.no_master:
        print(master(), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
