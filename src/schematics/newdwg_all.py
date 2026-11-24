"""All new drawings in one fixed order (build lines 24 and 43): App D sheets, missing systems, FCUs with no tag.

    python -m src.schematics.newdwg_all [--no-check]

1. The previous drawing numbers are read from the coverage files.  A drawing whose items are all drawn on a working
   sheet (ledger) is withdrawn: its number goes to out/schematics/new_drawing_numbers.csv and is not used again.
2. The new drawings are built again, whole, into out/schematics/new_drawings (old files are removed first).
3. out/schematics/new_drawings/withdrawn.csv lists every unit that a working sheet draws and that is no longer
   on a new drawing, with the sheet (the cross-reference text 'shown on IP-BH-nnnnn').
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

from . import drawings_common as dc
from . import newdwg as nd


def previous_coverage(newdir: Path) -> List[Dict[str, str]]:
    """Rows (kind, item, drawing) of the coverage files of the earlier build: from the folder and from the last commit of out/ (the
    earlier build: the folder is cleaned at each run)."""
    import io
    import subprocess
    rows: List[Dict[str, str]] = []
    for f in sorted(newdir.glob("*_coverage.csv")):
        rows += dc.read_csv(f)
    try:
        names = subprocess.run(["git", "-C", str(nd.ROOT / "out"), "ls-tree", "--name-only", "HEAD", "schematics/new_drawings/"],
                               capture_output=True, text=True, check=True).stdout.split()
        for n in names:
            if n.endswith("_coverage.csv"):
                t = subprocess.run(["git", "-C", str(nd.ROOT / "out"), "show", "HEAD:" + n], capture_output=True, text=True, check=True).stdout
                rows += list(csv.DictReader(io.StringIO(t)))
    except Exception:
        pass
    return rows


def previous_dxf_numbers(newdir: Path) -> Set[str]:
    import subprocess
    nums = {p.name[:11] for p in newdir.glob("IP-BH-*_P1.dxf")}
    try:
        names = subprocess.run(["git", "-C", str(nd.ROOT / "out"), "ls-tree", "--name-only", "HEAD", "schematics/new_drawings/"],
                               capture_output=True, text=True, check=True).stdout.split()
        nums |= {re.search(r"IP-BH-[0-9A-Z]{5}", n).group(0) for n in names if n.endswith("_P1.dxf")}
    except Exception:
        pass
    return nums


def seed_numbers(newdir: Path) -> None:
    """First run of the number ledger: the numbers of the earlier build are 'issued'."""
    if nd.NUMBER_LEDGER.exists():
        return
    with open(nd.NUMBER_LEDGER, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["drawing_number", "status", "reason", "items"])
        w.writeheader()
        for n in sorted(previous_dxf_numbers(newdir)):
            w.writerow(dict(drawing_number=n, status="issued", reason="draft 1", items=""))


def finalize_numbers(prev_cov: List[Dict[str, str]], new_numbers: Set[str]) -> List[Dict[str, str]]:
    """A number of the ledger that the new build does not use is withdrawn and is not used again.  The reason names the working
    sheet when every item of the drawing is now drawn on a working sheet, else says where the content went."""
    led = dc.Ledger()
    rows = dc.read_csv(nd.NUMBER_LEDGER)
    by_num: Dict[str, Set[str]] = defaultdict(set)
    for r in prev_cov:
        for n in re.findall(r"IP-BH-[0-9A-Z]{5}", r.get("drawing", "")):
            if r.get("item") and not r["item"].startswith("SPACE"):
                by_num[n].add(r["item"])
    out: List[Dict[str, str]] = []
    seen = set()
    for r in rows:
        n = r["drawing_number"]
        seen.add(n)
        if r["status"] == "issued" and n not in new_numbers:
            items = by_num.get(n, set())
            homes = {i: led.homes(i) for i in items}
            if items and all(homes.values()):
                sheets = sorted({h for v in homes.values() for h in v})
                r = dict(r, status="withdrawn", reason="withdrawn: drawn on " + ", ".join(sheets), items="; ".join(sorted(items)))
            else:
                moved = sorted(x for x in new_numbers if x[:10] == n[:10])
                r = dict(r, status="withdrawn", reason="withdrawn: content now on %s" % (", ".join(moved) or "no new drawing"),
                         items="; ".join(sorted(items)))
        elif r["status"] == "withdrawn":
            pass
        out.append(r)
    for n in sorted(new_numbers - seen):
        out.append(dict(drawing_number=n, status="issued", reason="", items=""))
    with open(nd.NUMBER_LEDGER, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["drawing_number", "status", "reason", "items"])
        w.writeheader()
        w.writerows(out)
    return [r for r in out if r["status"] == "withdrawn"]


def storey_of_number(num: str) -> str:
    from . import newdwg_missing as nm
    for st, pats in nm.SERIES.items():
        for pat in pats:
            if re.fullmatch(pat.replace("%d", r"\d"), num[6:11]):
                return st
    return ""


def snapshot_previous(newdir: Path) -> None:
    """Items that an earlier build drew on a new drawing, with their storey.  Kept in out/schematics (append only) so that a unit
    that a working sheet draws can still get its cross-reference line on the new drawing of its storey."""
    from . import newdwg_missing as nm
    have = {(r["item"], r["kind"]) for r in dc.read_csv(nm.PREV_ITEMS)}
    rows: List[Dict[str, str]] = []
    for r in previous_coverage(newdir):
        if r.get("kind") in ("unit", "outdoor-air unit") and (r["item"], r["kind"]) not in have:
            have.add((r["item"], r["kind"]))
            rows.append(dict(item=r["item"], kind=r["kind"], drawing=r["drawing"], storey=storey_of_number(r["drawing"])))
    if rows or not nm.PREV_ITEMS.exists():
        old = dc.read_csv(nm.PREV_ITEMS)
        with open(nm.PREV_ITEMS, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["item", "kind", "drawing", "storey"])
            w.writeheader()
            w.writerows(old + rows)


def clean(newdir: Path) -> None:
    for pat in ("IP-BH-*_P1.*", "*_coverage.csv", "appd_drawing_register_rows.csv", "withdrawn.csv"):
        for f in newdir.glob(pat):
            f.unlink()


def run(check: bool = True) -> int:
    from . import newdwg_check, newdwg_fcu, newdwg_fcu_check, newdwg_missing, newdwg_missing_check
    newdir = nd.NEWDIR
    newdir.mkdir(parents=True, exist_ok=True)
    prev_cov = previous_coverage(newdir)
    seed_numbers(newdir)
    snapshot_previous(newdir)
    clean(newdir)
    rc = 0
    appd_rows: List[Dict[str, str]] = []
    for st in [4, 2, 3, 5, 6, 7]:
        res = nd.run(st)
        for r in res:
            print(r["number"], r["dxf"].name)
            blocks = r["ctx"]["blocks"]
            tags = {t for b in blocks for t in b.tags}
            appd_rows.append(dict(
                drawing_number=r["number"] + " (provisional)", revision="P1", status="DRAFT, for checking and drafting",
                kind="new drawing", sheet_size="A0 3367.8 x 2384.2 pt",
                files="out/schematics/new_drawings/%s_P1.dxf; .pdf; _values.csv; _check.json" % r["number"],
                systems="L%02d App D: %d equipment tags, %d spaces" % (st, len(tags), len(r["sheet"].shown)),
                change_ids=str(len({v.chg for v in r["sheet"].values if v.chg})),
                notes="App D common-area air-side schematic, storey %d, sheet . Number provisional, for confirmation." % st))
        if check:
            rc |= newdwg_check.main_storey(res)
    appd_rows.sort(key=lambda r: r["drawing_number"])
    with open(newdir / "appd_drawing_register_rows.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["drawing_number", "revision", "status", "kind", "sheet_size", "files", "systems",
                                           "change_ids", "notes"])
        w.writeheader()
        w.writerows(appd_rows)
    res = newdwg_missing.run(None)
    if check:
        rc |= newdwg_missing_check.main(res)
    res_f = newdwg_fcu.run(None)
    if check:
        rc |= newdwg_fcu_check.main(res_f, getattr(newdwg_fcu.run, "not_drawn", []))
    # the list of withdrawn units: for the list of drawings, the update workbook and the coverage
    d = newdwg_missing.Data()
    rows = [dict(item=w["item"], change_id=w["change_id"], category=w["category"], shown_on="; ".join(w["homes"]),
                 text="shown on " + ", ".join(w["homes"])) for w in d.withdrawn_rows()]
    for st in range(2, 8):
        wd = nd.withdrawn(st)
        for t, h in sorted(wd["tags"].items()):
            rows.append(dict(item=t, change_id="", category="App D layout tag, storey %d" % st, shown_on="; ".join(h),
                             text="shown on " + ", ".join(h)))
    with open(newdir / "withdrawn.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["item", "change_id", "category", "shown_on", "text"])
        w.writeheader()
        w.writerows(rows)
    nd.NAMES.write_missed(newdir.parent / "name_map_missed_new_drawings.csv")
    new_numbers = {p.name[:11] for p in newdir.glob("IP-BH-*_P1.dxf")}
    gone = finalize_numbers(prev_cov, new_numbers)
    print("withdrawn units:", len(rows), "withdrawn drawing numbers:", [(g["drawing_number"], g["reason"]) for g in gone])
    return rc


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--no-check", action="store_true")
    ns = ap.parse_args(argv)
    return run(not ns.no_check)


if __name__ == "__main__":
    sys.exit(main())
