"""Drawing register of the Phase 4 schematic set and the issue folder (brief 9.4).

``python -m src.schematics.drawing_register build``  writes ``out/schematics/drawing_register.csv``.
``python -m src.schematics.drawing_register issue``  copies the set into ``out/issue/SCHEMATICS_DRAFT/``.

One row for each drawing that is marked up, reviewed and not marked up, or created.  The rows are built from the
change register, the model, the MOT review rows and the coverage files of the new drawings.  Nothing here changes a
drawing: the issue folder gets copies, and only the file properties of the copies are cleaned.
"""

from __future__ import annotations

import csv
import json
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[2]
SCH = ROOT / "out" / "schematics"
MARK = SCH / "markups"
NEW = SCH / "new_drawings"
ISSUE = ROOT / "out" / "issue" / "SCHEMATICS_DRAFT"
REGISTER_CSV = SCH / "drawing_register.csv"
SHEET_SIZE = "A0 3367.8 x 2384.2 pt"
STATUS_NEW = "DRAFT, for checking and drafting"
STATUS_MARK = "MARKED UP, for checking and drafting"
COLUMNS = ["drawing_number", "number_status", "title", "storey", "kind", "revision", "status", "sheet_size",
           "systems", "change_id_count", "change_ids", "files", "notes"]


def read_csv(path: Path) -> List[Dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def units_of(text: str) -> List[str]:
    """Tags of a register 'unit' cell: groups are split and the group label is dropped."""
    out: List[str] = []
    for part in re.split(r";\s*", re.sub(r"^AHU group:\s*", "", text or "")):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def id_range(ids: Sequence[str]) -> str:
    """First and last ID of each prefix, for example 'CH-APPD-0001 to CH-APPD-0322; CH-06B20-0037'."""
    groups: Dict[str, List[str]] = defaultdict(list)
    for i in sorted(set(x for x in ids if x)):
        groups[i.rsplit("-", 1)[0]].append(i)
    parts = []
    for g in groups.values():
        parts.append(g[0] if len(g) == 1 else f"{g[0]} to {g[-1]}")
    return "; ".join(parts)


def short_list(items: Sequence[str], n: int = 14) -> str:
    items = list(dict.fromkeys(items))
    if len(items) <= n:
        return "; ".join(items)
    return "; ".join(items[:n]) + f"; and {len(items) - n} more"


def title_of(model: Dict[str, Any], sheet: str) -> str:
    rel = (model["sheets"].get(sheet) or {}).get("drive_rel_path", "")
    name = Path(rel).stem
    name = re.sub(r"^IP-BH-\w+?_(zz )?", "", name)
    return name.strip()


def storey_of(model: Dict[str, Any], sheet: str) -> str:
    return str((model["sheets"].get(sheet) or {}).get("storey", ""))


def build_rows() -> List[Dict[str, str]]:
    model = json.load(open(ROOT / "out/registers/schematic_model.json"))
    changes = read_csv(SCH / "change_register.csv")
    by_sheet: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for r in changes:
        by_sheet[r["sheet"]].append(r)
    sys_by_sheet: Dict[str, List[str]] = defaultdict(list)
    for s in model["systems"]:
        if s["source_set"] == "working":
            for a in s["ahus"]:
                sys_by_sheet[s["sheet"]].append(a["tag"]["value"])
    rows: List[Dict[str, str]] = []

    # 1. marked up
    for pdf in sorted(MARK.glob("*_markup.pdf")):
        sh = pdf.name[:-len("_markup.pdf")]
        rr = by_sheet.get(sh, [])
        ids = [r["change_id"] for r in rr]
        layers = sorted({r["layer_type"] for r in rr})
        rows.append(dict(
            drawing_number=sh, number_status="working issue", title=title_of(model, sh), storey=storey_of(model, sh),
            kind="mark-up", revision="working issue; mark-up copy", status=STATUS_MARK, sheet_size=SHEET_SIZE,
            systems=short_list([u for r in rr for u in units_of(r["unit"])]),
            change_id_count=str(len(set(ids))), change_ids=id_range(ids),
            files="; ".join(f"markups/{sh}_markup{x}" for x in (".pdf",)),
            notes=("Copy of the working issue with unflattened annotations on the layers " + ", ".join(layers)
                   + ". The page content is unchanged. Each annotation carries its change ID as its subject.")))

    # 2. reviewed, not marked up
    marked = {r["drawing_number"] for r in rows}
    for sh in sorted(model["sheets"]):
        if sh in marked:
            continue
        kinds = {s["kind"] for s in model["systems"] if s["sheet"] == sh}
        tags = sys_by_sheet.get(sh, [])
        if sh in ("IP-BH-07050", "IP-BH-07051", "IP-BH-07052"):
            why = "Theatre units of the MOT department. They are listed and not marked up. Specified values stay."
        elif "working_unassigned_rooms" in kinds and not tags:
            why = ("Reviewed. The sheet draws room boxes with no AHU tag and the zone schedule gives no unit to them, "
                   "so no value can be marked up. The rooms are on the tab of the unit that the zone schedule names.")
        else:
            why = ("Reviewed. The change list has no row for this sheet: each drawn value is equal to the required "
                   "value, or the sheet is covered on the tab of another unit.")
        rows.append(dict(
            drawing_number=sh, number_status="working issue", title=title_of(model, sh), storey=storey_of(model, sh),
            kind="reviewed, not marked up", revision="working issue", status="REVIEWED, no mark-up", sheet_size=SHEET_SIZE,
            systems=short_list(tags), change_id_count="0", change_ids="", files="", notes=why))
    if "IP-BH-00100" not in {r["drawing_number"] for r in rows}:
        rows.append(dict(
            drawing_number="IP-BH-00100", number_status="working issue", title="INPATIENT OVERALL ACMV SCHEMATIC 1",
            storey="", kind="reviewed, not marked up", revision="working issue", status="REVIEWED, no mark-up",
            sheet_size=SHEET_SIZE, systems="", change_id_count="0", change_ids="", files="",
            notes=("Reviewed. Its tags are above level 07 and outside the zoning, so the change list has no row. "
                   "The tags are listed on the Consistency tab. The revised AHU list is on IP-BH-00101.")))
    # MOT NSC sheets: from the MOT differences tab of the update workbook
    import openpyxl
    wb = openpyxl.load_workbook(SCH / "schematic_update_register.xlsx", read_only=True)
    mot: Dict[str, List[str]] = defaultdict(list)
    for r in list(wb["MOT differences"].iter_rows(values_only=True))[5:]:
        if r[0] and str(r[0]).startswith("IP-BH-"):
            if r[1]:
                mot[str(r[0])].append(str(r[1]))
    for sh in sorted(mot):
        rows.append(dict(
            drawing_number=sh, number_status="MOT NSC issue", title="", storey="", kind="reviewed, not marked up (MOT)",
            revision="MOT NSC issue", status="REVIEWED, listed, no mark-up", sheet_size=SHEET_SIZE,
            systems=short_list(mot[sh]), change_id_count="0", change_ids="", files="",
            notes=("Compared with the MOT schedules. The differences are on the MOT differences tab of the update workbook. "
                   "The sheet is not marked up. Operating theatres keep the specified values.")))

    # 3. created: App D storeys
    appd = read_csv(NEW / "appd_drawing_register_rows.csv")
    per_storey: Dict[str, List[str]] = defaultdict(list)
    for r in appd:
        per_storey[r["systems"].split(" ")[0]].append(r["drawing_number"].split()[0])
    for r in appd:
        num = r["drawing_number"].split()[0]
        st = r["systems"].split(" ")[0]
        k, n_all = per_storey[st].index(num) + 1, len(per_storey[st])
        ids = value_ids(num)
        sheet = f"sheet {k} of {n_all}" if n_all > 1 else "single sheet"
        rows.append(dict(
            drawing_number=num, number_status="provisional, to confirm",
            title=f"App D common-area air-side schematic, {st}, {sheet}", storey=st, kind="new drawing",
            revision="P1", status=STATUS_NEW, sheet_size=SHEET_SIZE, systems=r["systems"],
            change_id_count=str(len(ids)), change_ids=id_range(ids), files=new_files(num),
            notes=f"App D common-area air-side schematic, {st}, {sheet}. Number provisional, for confirmation."))
    # 4. created: systems with no sheet and separated rooms
    cov = read_csv(NEW / "missing_systems_coverage.csv")
    per: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for r in cov:
        per[r["drawing"]].append(r)
    for num in sorted(per):
        items = per[num]
        ids = value_ids(num)
        kinds = sorted({i["kind"] for i in items})
        rows.append(dict(
            drawing_number=num, number_status="provisional, to confirm", title=missing_title(num),
            storey=missing_storey(num), kind="new drawing", revision="P1", status=STATUS_NEW, sheet_size=SHEET_SIZE,
            systems=short_list([i["item"] for i in items], 20), change_id_count=str(len(ids)), change_ids=id_range(ids),
            files=new_files(num),
            notes="Air-side schematic of " + ", ".join({"unit": "units with no sheet", "outdoor-air unit": "outdoor-air units named and not drawn",
                  "separated room": "rooms separated onto an FCU"}[k] for k in kinds) + ". Number provisional, for confirmation."))
    # 5. created: FCUs with no tag in the zone schedule
    fcov = read_csv(NEW / "fcu_notag_coverage.csv") if (NEW / "fcu_notag_coverage.csv").exists() else []
    for p in NEW.glob("IP-BH-*_P1_check.json"):
        c = json.load(open(p))
        if p.name[:11] in {r["drawing"] for r in fcov}:
            NEW_FCU_STOREY[p.name[:11]] = str(c.get("storey", ""))
    perf: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for r in fcov:
        if r["drawing"]:
            perf[r["drawing"]].append(r)
    for num in sorted(perf):
        items = perf[num]
        ids = value_ids(num)
        rows.append(dict(
            drawing_number=num, number_status="provisional, to confirm",
            title="FCUs with no tag in the zone schedule, storey " + fcu_storey(num), storey=fcu_storey(num), kind="new drawing",
            revision="P1", status=STATUS_NEW, sheet_size=SHEET_SIZE,
            systems=f"{len(items)} FCUs, tag to confirm: " + short_list([re.search(r"\[(.*)\]", i["item"]).group(1) for i in items], 12),
            change_id_count=str(len(ids)), change_ids=id_range(ids), files=new_files(num),
            notes="Air-side schematic of the FCUs that the zone schedule lists with no tag. One FCU and one room box for each line. "
                  "Airflows are not given: no workbook carries the room. Number provisional, for confirmation."))
    return rows


def fcu_storey(num: str) -> str:
    return NEW_FCU_STOREY.get(num, "")


NEW_FCU_STOREY: Dict[str, str] = {}


def missing_storey(num: str) -> str:
    return {"06B30": "B3", "06B22": "B2", "06B23": "B2", "06011": "L01", "06020": "L02", "06030": "L03", "06046": "L04",
            "06059": "L05", "06060": "L06", "06070": "L07"}.get(num.replace("IP-BH-", ""), "")


def missing_title(num: str) -> str:
    return "Air-side schematic, storey " + missing_storey(num) + ", systems with no working sheet and separated rooms"


def new_files(num: str) -> str:
    return "; ".join(f"new_drawings/{num}_P1{x}" for x in (".pdf", ".dxf"))


def value_ids(num: str) -> List[str]:
    p = NEW / f"{num}_P1_values.csv"
    if not p.exists():
        return []
    return sorted({r["change_id"] for r in read_csv(p) if r.get("change_id")})


def write(rows: List[Dict[str, str]], path: Path, files_prefix: Optional[str] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            r = dict(r)
            if files_prefix is not None and r["files"]:
                r["files"] = "; ".join(files_prefix + f for f in r["files"].split("; "))
            w.writerow({k: r.get(k, "") for k in COLUMNS})


def clean_pdf_properties(path: Path) -> None:
    """Remove tool names from the file properties of an issued copy. The pages are not touched (incremental save)."""
    import pymupdf
    d = pymupdf.open(path)
    md = dict(d.metadata or {})
    changed = False
    for k in ("creator", "producer"):
        if md.get(k) and re.search(r"newdwg|pymupdf|fitz|ezdxf", md[k], re.I):
            md[k] = ""
            changed = True
    if changed:
        d.set_metadata(md)
        d.saveIncr()
    d.close()


README = """\
# Air-side schematics: draft set

Status: DRAFT, for checking and drafting. Revision P1. Date: {date}.

## 1. What this set is

This set updates the air-side schematics of the inpatient building.
It has three parts.

1. Mark-up copies of the working drawings. A drafter applies them in the CAD drawing files.
2. New drawings, built where no working drawing shows a system.
3. Lists that link every change to its source.

We did not change the CAD drawing files. We did not change the page content of any working drawing.
Every mark-up is an annotation on a copy of the sheet. You can hide all annotations.
The sheet then looks the same as the working drawing.

The set is readable and traceable. It is not final.
A person must check each change before the drawings are issued.

## 2. Contents of this folder

| Item | What it holds |
|---|---|
| markups | {n_markup} mark-up copies, one PDF for each working sheet that has changes. |
| new_drawings | {n_new} new drawings. Each has a PDF and a DXF from the same geometry. |
| schematic_update_register | The update workbook. One tab for each unit, and tabs for App D, MOT, missing systems and consistency. |
| drawing_register | The list of drawings. One row for each drawing marked up, reviewed or created. |
| change_list | One row for each change ID: sheet, page, box, drawn value, required value, action, source. |
| missing_systems | The systems that the zoning or the workbooks need and no working sheet shows. |

The list of drawings is a table. It opens in a spreadsheet.
The files column gives the path of each drawing in this folder.

## 3. Change IDs

Each change has one change ID. The ID is on the mark-up, on the new drawing and in the lists.

| Form | Meaning |
|---|---|
| CH-05010-0001 | A change on working sheet IP-BH-05010. The number after the sheet gives the order. |
| CH-NEW-0001 | A new system. The system has no working sheet, or the room is separated onto an FCU. |
| CH-APPD-0001 | An App D item. It is a space, a unit or a query on the common-area layouts. |

To trace a change, find its ID in the change list.
The row gives the box on the page and the source.
A source is a workbook cell, a zone schedule cell, an equipment schedule page or an App D location.

## 4. Mark-up copies

The mark-up copies are in the folder markups.
Each file has the name of the working sheet and the ending _markup.

Each annotation carries its change ID as its subject.
Each annotation is on one layer. The layer gives the type of change.

| Layer | Type of change |
|---|---|
| Rooms | A room is added, removed, moved, renamed, or its typical count changes. |
| Airflows | A room airflow or an AHU total changes, or a value is missing. |
| Fans | A fan is added, resized or removed. |
| New systems | A system is missing on the sheet. The system is added or drawn on a new sheet. |
| Queries | The documents do not give an answer. A person must decide. |

How to read the marks:

- A cloud shows a value that the new value replaces. The new value is in a call-out.
- A tabulated note beside a system lists rooms or fans to add.
- A strike-through shows an item to remove.
- A dashed box with a pink tag shows a query.
- A leader line joins a call-out to its value. On crowded sheets the line is long.

The sheet IP-BH-00101 is the overall schematic. Its mark-up lists the AHUs of the zone schedule that the sheet does not show.
It also lists tags to check.

## 5. New drawings

The new drawings are in the folder new_drawings. Each has a PDF and a DXF.
The PDF is drawn from the same geometry as the DXF. The two files show the same text and lines.

There are three groups.

1. App D common-area systems. One group of sheets for each storey from level 02 to level 07. {n_appd} sheets.
2. Systems that have no working sheet, and rooms separated onto an FCU under the outdoor-air rule. {n_missing} sheets.
3. FCUs that the zone schedule lists with no tag. One FCU and one room for each line. {n_fcu} sheets.

Each drawing follows the working drawings. Levels are bands. Room boxes are in a row.
Each room box shows the name, the count, the area, the supply, return and exhaust airflow, and the pressure where the room has a target.
Equipment tags are as written in the source.
The sheets use the symbols of the ACMV notes and legend sheets.
Where the legend has no symbol, the drawing has a labelled box. Each sheet lists these boxes.

The layers of the DXF have the prefix IP. They hold the frame, the room boxes, the equipment, the ducts by service, the text and the queries.
Each value in the DXF has data that gives its change ID and its source.

The sheet size is A0, the same as the working drawings. The title block has no logo and no signature.
Status is DRAFT. Revision is P1. The approval fields are empty.

The drawing numbers are provisional. The drawing office must confirm them.
The 3rd-storey App D sheets use two free numbers of the 4th-storey block, because the 3rd-storey block has none.

## 6. Update rule

The rule decides if a change is marked up.

- An increase of more than 5 % of the drawn value, or more than 10 L/s, is marked up.
- A reduction is marked up only where the room list of the unit is complete and the conditions for a reduction hold.
  Where they do not hold, the drawn value stays. The margin is in the update workbook.
- A fan pressure cannot come from a schematic. The drawn value stays.
- MOT NSC sheets are listed in the update workbook. They are not marked up. Operating theatres keep their specified values.

A row that passes only one of the two tests for an increase says so in its note.

## 7. Limits

- The required airflow of a room box is the average of the workbook rooms that link to the box.
  Where those rooms differ by more than 25 %, the row is a query. A person must check each average.
- VAV boxes and venturi valves are not compared. The workbooks have no rows for them.
- On crowded sheets, a call-out can be far from its value. The leader line can cross room boxes.
- The sheet IP-BH-00100 has no mark-up. Its tags are above level 07 and outside the zoning.
- The sheets IP-BH-07140 and IP-BH-07141 carry the MOT ACMV title. Their units are in the MOT NSC schedule.
  The set lists them and does not mark them up. Decision D-074 is open: the engineer must confirm this.
- The 98 FCU lines with no tag have no workbook data. Their airflows show as not given.
  One line has no room and no storey. It stays on the list of missing systems.
- The unit AHU-B-06-06STAFF-01 is on the working sheet IP-BH-06068. The App D sheet IP-BH-05067 keeps its room boxes and refers to that sheet for the unit.
- An increase of 5 L/s or less is not marked up. The drawn value stays.
- A mark-up on a unit with a room list that the workbooks over-count says so in its call-out. Check the room list before you apply it.
- The sheet IP-BH-05079 lists the schedule items that no App D layout shows. It has no schematic.
- The air balance is tested on the workbook values, not on the drawn rooms. A drawn room does not show its pressure offset.
- Rooms that take make-up air by transfer are listed, not tested.
- An independent check of a sample of systems is still to do.

## 8. Checks that were run

Every mark-up has a row in the change list, and every row of a working sheet has a mark-up.
Every value on a new drawing equals its source value. We read the DXF back to test this.
No text overlaps other text, a symbol or a duct.
Every highlighted App D space is in one room box. Every App D tag is on one drawing, or it is listed with its reason.
Air in equals air out within 1 % or 10 L/s for the workbook rooms and the AHU totals.
"""


def build_issue() -> Dict[str, int]:
    rows = build_rows()
    if ISSUE.exists():
        shutil.rmtree(ISSUE)
    (ISSUE / "markups").mkdir(parents=True)
    (ISSUE / "new_drawings").mkdir(parents=True)
    n = {"markups": 0, "pdf": 0, "dxf": 0}
    for r in rows:
        for f in filter(None, r["files"].split("; ")):
            src = NEW / Path(f).name if f.startswith("new_drawings/") else MARK / Path(f).name
            dst = ISSUE / f
            shutil.copy2(src, dst)
            if f.endswith(".pdf"):
                clean_pdf_properties(dst)
                n["markups" if f.startswith("markups/") else "pdf"] += 1
            elif f.endswith(".dxf"):
                n["dxf"] += 1
    shutil.copy2(SCH / "schematic_update_register.xlsx", ISSUE / "schematic_update_register.xlsx")
    shutil.copy2(SCH / "missing_systems.csv", ISSUE / "missing_systems.csv")
    shutil.copy2(SCH / "change_register.csv", ISSUE / "change_list.csv")
    write(rows, ISSUE / "drawing_register.csv")
    new = [r for r in rows if r["kind"] == "new drawing"]
    fill = dict(date="2026-11-23", n_markup=n["markups"], n_new=len(new),
                n_appd=sum(1 for r in new if r["title"].startswith("App D")),
                n_fcu=sum(1 for r in new if r["title"].startswith("FCUs with no tag")))
    fill["n_missing"] = fill["n_new"] - fill["n_appd"] - fill["n_fcu"]
    (ISSUE / "README.md").write_text(README.format(**fill), encoding="utf-8")
    return n


def main(argv: Optional[Sequence[str]] = None) -> int:
    cmd = (argv or sys.argv[1:] or ["build"])[0]
    if cmd == "build":
        rows = build_rows()
        # paths in the source copy are relative to out/schematics
        write(rows, REGISTER_CSV, files_prefix="out/schematics/")
        print(len(rows), "rows ->", REGISTER_CSV)
    elif cmd == "issue":
        print(build_issue())
    return 0


if __name__ == "__main__":
    sys.exit(main())
