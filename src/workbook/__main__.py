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
    s = sub.add_parser("selftest", help="Rev A reproduction, known answers, error scan, style proof, PDF")
    s.add_argument("--work", default=None, help="scratch directory")
    s.add_argument("--report", default=None, help="write a JSON of the results here")
    a = sub.add_parser("assemble", help="build a department input JSON from the registers")
    a.add_argument("--dept", required=True)
    a.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "build":
        from .build import build_workbook
        ctx = build_workbook(Path(args.input), Path(args.out))
        print(f"built {args.out}: {len(ctx.rooms)} rooms, {len(ctx.wb.sheetnames)} tabs")
        return 0
    if args.cmd == "selftest":
        from .selftest import run
        return run(Path(args.work) if args.work else None, Path(args.report) if args.report else None)
    from .assemble import assemble_department
    assemble_department(args.dept, Path(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
