"""CLI: python3 -m src.workbook build --input <json> --out <xlsx> | selftest | assemble."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m src.workbook")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build a workbook from a department input JSON")
    b.add_argument("--input", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--proposals", default=None,
                   help="zoning_change_proposals.csv to update (default out/zoning/zoning_change_proposals.csv)")
    b.add_argument("--no-proposals", action="store_true", help="do not recalculate and write the outdoor-air rule proposals")
    s = sub.add_parser("selftest", help="Rev A reproduction, known answers, error scan, style proof, PDF")
    s.add_argument("--work", default=None, help="scratch directory")
    s.add_argument("--report", default=None, help="write a JSON of the results here")
    m = sub.add_parser("master", help="AHU master summary: each AHU once across the department workbooks")
    m.add_argument("--inputs", required=True, help="directory with department workbooks (.xlsx) or summaries (.json)")
    m.add_argument("--out", required=True)
    cm = sub.add_parser("common", help="common-areas workbook from the App D spaces and equipment")
    cm.add_argument("--out", required=True, help="workbook to write")
    cm.add_argument("--json", default=None, help="also write the input JSON here")
    c = sub.add_parser("check", help="recalculate a workbook and scan it: error values, formulas, deliverable language, flags")
    c.add_argument("--xlsx", required=True)
    c.add_argument("--work", default=None, help="scratch directory for the recalculated copy")
    a = sub.add_parser("assemble", help="build a department input JSON from the registers")
    a.add_argument("--dept", required=True)
    a.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "build":
        from .build import build_workbook
        ctx = build_workbook(Path(args.input), Path(args.out))
        print(f"built {args.out}: {len(ctx.rooms)} rooms, {len(ctx.wb.sheetnames)} tabs")
        if not args.no_proposals:
            import tempfile
            from .proposals import DEFAULT_PATH, update_csv
            from .recalc import recalc
            with tempfile.TemporaryDirectory(prefix="wb_prop_") as tmp:
                rows = update_csv(recalc(Path(args.out), Path(tmp)), Path(args.proposals) if args.proposals else DEFAULT_PATH)
            print(f"outdoor-air rule: {sum(1 for r in rows if r['rule_outcome'] == 'SEPARATE')} room(s) proposed for separation, "
                  f"{sum(1 for r in rows if r['rule_outcome'] != 'SEPARATE')} listed for Marcus")
        return 0
    if args.cmd == "selftest":
        from .selftest import run
        return run(Path(args.work) if args.work else None, Path(args.report) if args.report else None)
    if args.cmd == "master":
        from .master import build_master
        res = build_master(Path(args.inputs), Path(args.out))
        print(f"built {res['out']}: {res['departments']} department(s), {res['ahus']} AHU row(s), {res['flags']} flag(s)")
        return 0
    if args.cmd == "common":
        from .common_areas import build_common_areas
        res = build_common_areas(Path(args.out), Path(args.json) if args.json else None)
        print(f"built {args.out}: {res['rooms']} spaces, {res['units']} units, {res['flags']} flags")
        return 0
    if args.cmd == "check":
        return check_workbook(Path(args.xlsx), Path(args.work) if args.work else None)
    from .assemble import assemble_department
    assemble_department(args.dept, Path(args.out))
    return 0


def check_workbook(xlsx: Path, work: Path = None) -> int:
    import tempfile
    import openpyxl
    from .recalc import recalc
    from .selftest import language_scan, scan_errors, scan_formulas
    work = work or Path(tempfile.mkdtemp(prefix="wbcheck_"))
    rc = recalc(xlsx, work)
    errs, lang, fs = scan_errors(rc), language_scan(rc), scan_formulas(xlsx)
    wb = openpyxl.load_workbook(rc, data_only=True)
    rooms = sum(1 for r in wb["Room Information"].iter_rows(min_row=7, values_only=True) if r[0]) if "Room Information" in wb.sheetnames else 0
    fl = [r for r in wb["Flags"].iter_rows(min_row=7, values_only=True) if r[0]] if "Flags" in wb.sheetnames else []
    checks = [(r[1], r[4]) for r in wb["Checks"].iter_rows(min_row=7, values_only=True) if r[1] and r[4]] if "Checks" in wb.sheetnames else []
    by_type = {}
    for r in fl:
        by_type[r[1]] = by_type.get(r[1], 0) + 1
    print(f"{xlsx.name}: rooms {rooms}; flags {len(fl)} {by_type}; formulas {fs['n_formula']}; error values {len(errs)}; "
          f"forbidden terms or internal references {len(lang)}; typed numbers {len(fs['literals'])}; unlisted functions {len(fs['bad_funcs'])}; "
          f"checks FAIL {sum(1 for _, s in checks if s == 'FAIL')}, VERIFY {sum(1 for _, s in checks if s == 'VERIFY')}")
    for e in errs[:5]:
        print("  error", e)
    for e in lang[:5]:
        print("  language", e)
    print("  recalculated copy:", rc)
    return 0 if not errs and not lang and not fs["bad_funcs"] and not fs["literals"] else 1


if __name__ == "__main__":
    sys.exit(main())
