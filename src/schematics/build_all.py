"""One entry point for the schematic set (council C4, build lines 3, 28, 40 and 43).

    python -m src.schematics.build_all [--stage-only] [--no-swap] [--from STEP]

Fixed order: model, ledger/scope table/review/update workbook (one review run), mark-ups, new drawings, list of drawings,
issue copy.  Everything is written to the staging folder ``out/issue/_staging/SCHEMATICS_DRAFT``; a partial rerun into
staging is allowed (``--stage-only`` rebuilds only the issue copy from the files of ``out/schematics``, ``--from`` starts at a step).
``out/issue/SCHEMATICS_DRAFT`` is replaced whole, by this program only, when phase 4 passes or every remaining failure is listed
under 'Known gaps' in the README.  The two checks of the build itself (P4-12 issue folder and README language, P4-23 one build) must
pass in every case.  Nothing in the issue folder is edited in place.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[2]
SCH = ROOT / "out" / "schematics"
STAGING = ROOT / "out" / "issue" / "_staging" / "SCHEMATICS_DRAFT"
ISSUE = ROOT / "out" / "issue" / "SCHEMATICS_DRAFT"
QA = ROOT / "out" / "qa"
MANIFEST = QA / "run_manifest.json"
DATE = "2026-11-23"

STEPS: List[Tuple[str, List[str]]] = [
    ("model", ["-m", "src.schematics.model", "run", "--no-pdf"]),
    ("ledger, scope table, review, update workbook", ["-m", "src.schematics.review", "run"]),
    ("mark-ups", ["-m", "src.schematics.markup", "all"]),
    ("new drawings", ["-m", "src.schematics.newdwg_all"]),
    ("list of drawings", ["-m", "src.schematics.drawing_register", "build"]),
]

# Plain names of the checks for the README (the titles in src/checks.py are for the checker of the code).
PLAIN = {
    "P4-01": "Every unit has a tab and is covered by a disposition, a new drawing or a stated reason",
    "P4-02": "Every App D tag appears once",
    "P4-03": "Every highlighted App D space is in one box",
    "P4-04": "New-drawing values equal the change list",
    "P4-05": "No placeholder change ID on a new drawing",
    "P4-06": "No text overlaps text, a symbol or a duct",
    "P4-07": "PDF and DXF hold the same entities",
    "P4-08": "Every mark-up has a row in the change list and the reverse",
    "P4-09": "Change IDs are equal in the change list, the workbook, the mark-ups and the DXF files",
    "P4-10": "Air in equals air out",
    "P4-11": "The list of drawings matches the files",
    "P4-12": "The issue folder is complete and its text follows the language rules",
    "P4-13": "Every drawn value is in the PDF text under its box",
    "P4-14": "Every cited workbook cell opens, lies in the right row and column, and equals the required value",
    "P4-15": "Fans: owner, pair, requirement and verdict",
    "P4-16": "No add, missing or no-schematic row for something that is drawn",
    "P4-17": "Every drawn entity has one disposition and the ledger agrees with the PDF text",
    "P4-18": "Every unit has one home",
    "P4-19": "No instruction on a unit with an incomplete room list; rules for name-only links, reductions and added rooms",
    "P4-20": "Values are in range and units of measure agree",
    "P4-21": "No change ID or annotation for a list-only (MOT) unit",
    "P4-22": "Word list, sentence length and duplicate rows",
    "P4-23": "One build: hashes, order of files, change IDs, no ID reused",
}


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def rcsv(path: Path) -> List[Dict[str, str]]:
    import csv
    csv.field_size_limit(10 ** 9)
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


# --------------------------------------------------------------------------- #
# README written from the list (build line 28)
# --------------------------------------------------------------------------- #
def kind_of(action: str) -> str:
    return re.split(r"[;:]\s", action, maxsplit=1)[0] if action.startswith(("Mark up", "New system", "New drawing")) else action.replace("Query: ", "")


def group_of(r: Dict[str, str]) -> Tuple[int, str]:
    a, c = r["action"], r["class"]
    if c == "pressure direction conflict":
        return 0, "Pressure direction conflict: the drawn and the workbook pressure have opposite signs"
    if c == "zoning conflict" or a.startswith("Query: drawn unit not in the approved zoning") or "in no zone" in a:
        return 1, "Decisions on zoning needed before drafting: " + a.replace("Query: ", "")
    return 2, a.replace("Query: ", "")


def table(rows: List[Tuple[str, int]], head: str) -> List[str]:
    out = [f"| {head} | Count |", "|---|---|"]
    out += [f"| {k.replace('|', '/')} | {v} |" for k, v in rows]
    return out


ID_LIKE = re.compile(r"CH-[A-Z0-9]+-\d{4}|IP-BH-[0-9A-Z]{5}|(?:AHU|PAHU|FCU|RAF|EAF|TEAF|IEAF|EF)-[A-Z0-9/-]+|D-\d{3}|L0\d|LB3|B\d")


def failing_ids(name_key: str, evidence: List[str], fails: Dict[str, List[str]]) -> Tuple[int, List[str]]:
    """Count and IDs of the failures of one check.  Only ID-like tokens are printed, so that no text of the check report reaches the README."""
    lines = fails.get(name_key)
    if lines is None:
        lines = [e for e in evidence[1:] if ID_LIKE.match(e)] or [e for e in evidence[1:] if e.strip()]
    ids: List[str] = []
    for ln in lines:
        m = ID_LIKE.search(ln)
        ids.append(m.group(0) if m else "see out/qa/check_phase4.md")
    return len(lines), list(dict.fromkeys(ids))


CHECK_FN = {"P4-13": "check_p4_13_anchor", "P4-14": "check_p4_14_source", "P4-15": "check_p4_15_fans", "P4-16": "check_p4_16_false_add",
            "P4-17": "check_p4_17_dispositions", "P4-18": "check_p4_18_home", "P4-19": "check_p4_19_gate", "P4-20": "check_p4_20_domain",
            "P4-21": "check_p4_21_mot", "P4-22": "check_p4_22_text", "P4-09": "check_p4_09_ids", "P4-23": "check_p4_23_build"}


def make_readme(results: Optional[List[Any]]) -> str:
    changes = rcsv(SCH / "change_register.csv")
    dreg = rcsv(SCH / "drawing_register.csv") if (SCH / "drawing_register.csv").exists() else []
    instr = [r for r in changes if r["action"].startswith(("Mark up", "New system", "New drawing"))]
    queries = [r for r in changes if r["action"].startswith("Query")]
    suppressed = [r for r in changes if r["class"].startswith("increase of 5 L/s or less")]
    ic = Counter(kind_of(r["action"]) for r in instr)
    qg: Dict[Tuple[int, str], int] = defaultdict(int)
    for r in queries:
        qg[group_of(r)] += 1
    qrows = sorted(qg.items(), key=lambda kv: (kv[0][0], -kv[1], kv[0][1]))
    not_reviewed = [r["drawing_number"] for r in dreg if r["status"].startswith("NOT REVIEWED")]
    withdrawn = [r["drawing_number"] for r in dreg if r["kind"] == "withdrawn"]
    n_mark = sum(1 for r in dreg if r["kind"] == "mark-up")
    n_new = sum(1 for r in dreg if r["kind"] == "new drawing")
    L: List[str] = []
    L += ["# Air-side schematics: draft set", "", f"Status: DRAFT, for checking and drafting. Revision P1. Date: {DATE}.", "",
          "## 1. What this set is", "",
          "This set updates the air-side schematics of the inpatient building. It has three parts.", "",
          f"1. Mark-up copies of the working drawings: {n_mark} sheets. A drafter applies them in the CAD drawing files.",
          f"2. New drawings: {n_new} sheets, built where no working drawing shows a system. {len(withdrawn)} numbers were withdrawn and are not used again.",
          "3. Lists that link every change to its source: the update workbook, the change list, the list of drawings and the list of missing systems.", "",
          "We did not change the CAD drawing files. We did not change the page content of any working drawing.",
          "Every mark-up is an annotation on a copy of the sheet. You can hide all annotations.",
          "A query is not an instruction. A drawn value that has a query stays as drawn.", "",
          "## 2. Instructions and queries", "",
          f"The change list holds {len(instr)} instructions and {len(queries)} queries.", ""]
    L += table(sorted(ic.items(), key=lambda kv: -kv[1]), "Instruction") + [""]
    L += [f"Increases of 5 L/s or less are not marked up for this draft. {len(suppressed)} rows are listed in the change list with that class.", ""]
    L += ["## 3. Open points for the checker", "",
          "Pressure direction conflicts come first. Zoning decisions come second. Then the other query groups follow by count.", ""]
    L += table([(k[1], v) for k, v in qrows], "Query group") + [""]
    L += ["## 4. Assumptions", "",
          "- An A/B pair with the same drawn duty is read as duty and standby. The pair counts once. Marcus confirms each pair.",
          "- A typical room box takes the average of its linked workbook rooms. The range is printed. A spread above 25 % is a query.",
          "- Outdoor air is the highest room fraction times the supply of the unit. The room, the fraction and the sum of room outdoor air are in each row.",
          "- Increases of 5 L/s or less are not marked up.",
          "- The rule of 5 % or 10 L/s is read as either one. A difference below both is no change.",
          "- A reduction of outdoor air or spill air is a query until Marcus confirms. The drawn value stays.",
          "- A room increase above 2 times on a link by name is a query 'link to confirm'. The boxes print no room number.",
          "- Only a room matched by number stays a mark-up. Marcus decides whether a one-to-one name link counts as a number match (D-088, item 12).",
          "- 'Show the required value' is an instruction only where the sheet draws a valueless label. The unit must have a complete room list. The sheet must draw no exhaust for the room.",
          "- A tag that nearly matches a drawn tag is a query 'tag to correct'. The two tags have the same level and one code starts with the other. No unit is added.",
          "- A separated room is an instruction only where the unit has a complete room list. No working sheet may draw a box of that name. Otherwise it is a query, and no FCU box is drawn.",
          "- An App D system with an incomplete room list stays on the App D sheet as a layout for checking. Its change ID is a query. Marcus decides whether to keep it.", "",
          "## 5. What is not proved", "",
          "- Fan pressure. The drawn value stays.",
          "- Duty and standby where the sheet does not state them.",
          "- Units with a room list outside 0.90 to 1.10 of the zoned area, or with an incomplete room list. Their rows are queries.",
          "- Rooms with a queried link between the box and the workbook room.",
          f"- Sheets not reviewed: {len(not_reviewed)}" + (" (" + ", ".join(not_reviewed[:12]) + ")" if not_reviewed else "") + ".", ""]
    L += ["## 6. Known gaps", ""]
    fails: Dict[str, List[str]] = {}
    try:
        from src import phase4_c4 as c4
        fails = c4.FAILS
    except Exception:
        pass
    gaps = [r for r in (results or []) if r.status == "FAIL"]
    if not results:
        L += ["The checks of phase 4 have not run on this set."]
    elif not gaps:
        L += ["No check of phase 4 fails on this set."]
    else:
        L += [f"{len(gaps)} checks of phase 4 fail. Each failure is listed with its count and its IDs. Each is open for the build owners.", ""]
        L += ["| Check | What it tests | Failures | IDs |", "|---|---|---|---|"]
        for r in gaps:
            n, ids = failing_ids(CHECK_FN.get(r.check_id, ""), r.evidence, fails)
            shown = ", ".join(ids[:10]) + (f" and {len(ids) - 10} more" if len(ids) > 10 else "")
            L.append(f"| {r.check_id} | {PLAIN.get(r.check_id, r.title).replace('|', '/')} | {n} | {shown.replace('|', '/')} |")
    L.append("")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# Steps, manifest, checks, swap
# --------------------------------------------------------------------------- #
def run_steps(first: int = 0) -> List[Dict[str, Any]]:
    log = []
    for i, (name, args) in enumerate(STEPS):
        if i < first:
            continue
        t = time.time()
        p = subprocess.run([sys.executable] + args, cwd=ROOT, capture_output=True, text=True)
        log.append(dict(step=name, seconds=round(time.time() - t, 1), returncode=p.returncode))
        print(f"{name}: {p.returncode} in {time.time() - t:.0f} s")
        if p.returncode != 0:
            print(p.stdout[-2000:], p.stderr[-2000:])
            raise SystemExit(f"step '{name}' failed")
    return log


def point_checks_at(issue: Path) -> None:
    from src import phase4_c4 as c4, phase4_checks as p4
    c4.ISSUE = issue
    p4.ISSUE = issue
    c4.reset()
    p4._MEMO.clear()


def run_checks(write: bool = True):
    from src import checks, common
    from src import phase4_c4 as c4
    c4.EVN = 10 ** 6
    ctx = checks.Context()
    results = checks.run_phase(4, ctx)
    c4.EVN = 12
    if write:
        common.atomic_write_text(checks.report_path(4, ctx), checks.render_report(4, results, ctx))
    return results


def write_manifest(steps: List[Dict[str, Any]]) -> Dict[str, Any]:
    man = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    inputs = ["config/project.yaml", "out/registers/schematic_model.json", "out/registers/schematic_equipment.csv",
              "out/registers/room_unit_links.csv", "out/registers/mot_ahu_schedule.csv", "out/zoning/zone_schedule.xlsx"]
    outs = ["entity_ledger.csv", "scope.csv", "change_id_ledger.csv", "change_id_map.csv", "change_register.csv", "missing_systems.csv",
            "schematic_update_register.xlsx", "drawing_register.csv", "review_check.json"]
    files = {p.relative_to(STAGING).as_posix(): sha(p) for p in sorted(STAGING.rglob("*")) if p.is_file()}
    man["phase4"] = dict(
        built_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), decisions="D-072 to D-088", steps=steps,
        inputs={i: sha(ROOT / i) for i in inputs if (ROOT / i).exists()},
        outputs={o: sha(SCH / o) for o in outs if (SCH / o).exists()},
        files=files, staging="out/issue/_staging/SCHEMATICS_DRAFT", issue="out/issue/SCHEMATICS_DRAFT")
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(man, indent=1, sort_keys=True))
    return man


def swap_issue() -> None:
    """Replace out/issue/SCHEMATICS_DRAFT whole by a copy of the staging folder."""
    new = ISSUE.with_name(".SCHEMATICS_DRAFT.new")
    old = ISSUE.with_name(".SCHEMATICS_DRAFT.old")
    for p in (new, old):
        if p.exists():
            shutil.rmtree(p)
    shutil.copytree(STAGING, new)
    if ISSUE.exists():
        ISSUE.rename(old)
    new.rename(ISSUE)
    if old.exists():
        shutil.rmtree(old)


def stage(steps: List[Dict[str, Any]]) -> Tuple[List[Any], bool]:
    from src.schematics import drawing_register as dr
    dr.build_issue(STAGING, readme=make_readme(None))          # first copy: files only
    point_checks_at(STAGING)
    write_manifest(steps)
    results = run_checks(write=False)
    # README with the known gaps from the checks; the copy is made again so that staging holds the final files
    dr.build_issue(STAGING, readme=make_readme(results))
    write_manifest(steps)
    point_checks_at(STAGING)
    results = run_checks(write=False)
    build_ok = all(r.status == "PASS" for r in results if r.check_id in ("P4-12", "P4-23"))
    return results, build_ok


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stage-only", action="store_true", help="rebuild only the list of drawings and the issue copy in staging from out/schematics")
    ap.add_argument("--no-swap", action="store_true")
    ap.add_argument("--from", dest="first", type=int, default=0, help="first step of the fixed order (0 to 4)")
    a = ap.parse_args(argv)
    steps = run_steps(len(STEPS) - 1 if a.stage_only else a.first)      # stage-only still rewrites the list of drawings
    results, build_ok = stage(steps)
    fails = [r.check_id for r in results if r.status == "FAIL"]
    print("staging checks:", "all PASS" if not fails else "FAIL " + ", ".join(fails))
    if not build_ok:
        print("P4-12 or P4-23 fails: the issue folder is not replaced")
        return 1
    if a.no_swap:
        return 0
    swap_issue()
    point_checks_at(ISSUE)
    results = run_checks(write=True)
    print("issue folder replaced; final run:", ", ".join(f"{r.check_id} {r.status}" for r in results if r.status == "FAIL") or "all PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
