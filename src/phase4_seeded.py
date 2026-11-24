"""Seeded-error test of the phase 4 checks (council C4, build line 41).  Scripts only, one round, on a scratch copy.

    python -m src.phase4_seeded [--scratch DIR] [--write]

15 seeds are planted one at a time in a scratch copy of out/schematics (the big PDF and DXF files are links, a file that a seed
changes is copied first).  After each seed the checks P4-01, -02, -09, -13 to -22 run on the copy.  A seed is caught when a check
shows a failure line that the clean copy does not show.  The clean copy is the baseline, so the test works on a set that is not
yet clean.  A seed that no check catches means a new check must be added; the test then runs once more and not again.
``--write`` records the result in out/qa/model_allocation.md.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from . import phase4_c4 as c4
from . import phase4_checks as p4

ROOT = Path(__file__).resolve().parents[1]
csv.field_size_limit(10 ** 9)

CHECKS: List[Tuple[str, Callable]] = [
    ("P4-01", p4.check_p4_units), ("P4-02", p4.check_p4_appd_tags), ("P4-09", c4.check_p4_09_ids),
    ("P4-13", c4.check_p4_13_anchor), ("P4-14", c4.check_p4_14_source), ("P4-15", c4.check_p4_15_fans),
    ("P4-16", c4.check_p4_16_false_add), ("P4-17", c4.check_p4_17_dispositions), ("P4-18", c4.check_p4_18_home),
    ("P4-19", c4.check_p4_19_gate), ("P4-20", c4.check_p4_20_domain), ("P4-21", c4.check_p4_21_mot),
    ("P4-22", c4.check_p4_22_text),
]
KEEP = ("words:", "text:", "dxf:", "wbtab:", "mot_keys", "zone_units")


def point_at(sch: Path) -> None:
    for m in (c4, p4):
        m.SCH, m.NEWD, m.MARK = sch, sch / "new_drawings", sch / "markups"
    for k in list(c4._MEMO):
        if not k.startswith(KEEP):
            del c4._MEMO[k]
    p4._MEMO.clear()


def run_checks() -> Dict[str, Set[str]]:
    """Failure lines of every check, as sets of text."""
    out: Dict[str, Set[str]] = {}
    for cid, fn in CHECKS:
        c4.EVN = 10 ** 6
        try:
            st, ev = fn(None)
        except Exception as exc:  # noqa: BLE001 - a check that crashes on a seed has caught it
            st, ev = c4.FAIL, [f"crash {type(exc).__name__}: {str(exc)[:80]}"]
        c4.EVN = 12
        lines = set(c4.FAILS.get(fn.__name__, [])) if fn.__module__.endswith("phase4_c4") else set(ev[1:]) if st == c4.FAIL else set()
        if st == c4.FAIL and not lines:
            lines = set(ev)
        out[cid] = lines
    return out


def make_scratch(base: Path, src: Path, writable: Set[str]) -> Path:
    """Scratch copy of out/schematics: CSV, JSON and workbook copied; PDF, DXF, PNG linked; names in ``writable`` copied."""
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    for p in src.iterdir():
        if p.is_file():
            shutil.copy2(p, base / p.name)
    (base / "markups").symlink_to(src / "markups")
    nd = base / "new_drawings"
    nd.mkdir()
    for p in (src / "new_drawings").iterdir():
        if p.name in writable or p.suffix == ".csv":
            shutil.copy2(p, nd / p.name)
        else:
            (nd / p.name).symlink_to(p)
    return base


def edit_csv(path: Path, fn: Callable[[List[Dict[str, str]]], Any]) -> Any:
    with open(path, encoding="utf-8", newline="") as fh:
        rd = csv.DictReader(fh)
        names, rows = rd.fieldnames, list(rd)
    res = fn(rows)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=names)
        w.writeheader()
        w.writerows(rows)
    return res


# --------------------------------------------------------------------------- #
# Seeds: each takes the scratch folder and returns a one-line description
# --------------------------------------------------------------------------- #
def anchored_rows(sch: Path, quantity: Optional[Tuple[str, ...]] = None, n: int = 2, layer: str = "airflows") -> List[str]:
    """Change IDs of rows that pass P4-13 on the clean copy: numeric drawn, a box, an instruction or a query on a working sheet."""
    out = []
    for r in c4.rcsv(sch / "change_register.csv"):
        if not r["change_id"] or not r["sheet"].startswith("IP-BH-") or r["layer_type"] != layer or c4.num(r["drawn"]) is None:
            continue
        if quantity and r["quantity"] not in quantity:
            continue
        bb = c4.bbox_of(r["bbox"])
        if bb and c4.has_number(c4.text_under(r["sheet"], bb, int(r["page"] or 1)), c4.num(r["drawn"])):
            out.append(r["change_id"])
        if len(out) >= n:
            break
    return out


def seed_boxes_to_label(sch: Path) -> str:
    ids = anchored_rows(sch, ("Supply airflow", "Return airflow"), 2)
    led = c4.rcsv(sch / "entity_ledger.csv")
    done = []

    def fn(rows):
        for r in rows:
            if r["change_id"] in ids:
                labels = [l for l in led if l["sheet"] == r["sheet"] and l["kind"] in ("AHU tag", "fan tag") and c4.num(r["drawn"]) is not None
                          and not c4.has_number(l["text"], c4.num(r["drawn"]))]
                if labels:
                    r["bbox"] = labels[0]["bbox"]
                    done.append(r["change_id"])
    edit_csv(sch / "change_register.csv", fn)
    return "box moved onto a tag label: " + ", ".join(done)


def seed_values_to_sum(sch: Path) -> str:
    ids = anchored_rows(sch, ("Supply airflow", "Return airflow"), 6)[3:5]
    done = []

    def fn(rows):
        for r in rows:
            if r["change_id"] in ids:
                r["drawn"] = str(c4.num(r["drawn"]) + 90.0)
                r["ledger_ids"] = ""
                done.append(r["change_id"])
    edit_csv(sch / "change_register.csv", fn)
    return "drawn value replaced by a sum with no components: " + ", ".join(done)


def seed_fan_three_units(sch: Path) -> str:
    done = []

    def fn(rows):
        units = list(dict.fromkeys(r["unit"] for r in rows if r["unit"].startswith("AHU-") and "/" not in r["unit"]))[:3]
        for r in rows:
            if r["quantity"] in c4.FAN_QUANTITIES and r["class"] == "not established" and not done:
                r["unit"] = "; ".join(units)
                done.append(r["change_id"])
    edit_csv(sch / "change_register.csv", fn)
    return "fan row linked to 3 units: " + ", ".join(done)


def seed_fans_deleted(sch: Path) -> str:
    done = []

    def fn(rows):
        by_key: Dict[Tuple[str, Any], List[Dict[str, str]]] = {}
        for r in rows:
            if r["sheet_role"] == "working" and r["kind"] == "fan tag":
                by_key.setdefault((r["sheet"], c4.tkey((c4.tags_in(r["text"]) or [""])[0])), []).append(r)
        singles = [v[0] for k, v in by_key.items() if len(v) == 1 and k[1]][:2]
        ids = {r["entity_id"] for r in singles}
        done.extend(sorted(ids))
        rows[:] = [r for r in rows if r["entity_id"] not in ids]
    edit_csv(sch / "entity_ledger.csv", fn)
    return "2 drawn fans deleted from the ledger: " + ", ".join(done)


def seed_unit_copied(sch: Path) -> str:
    import ezdxf
    files = sorted((sch / "new_drawings").glob("IP-BH-*_P1.dxf"))
    if len(files) < 2:
        return "no seed: fewer than 2 new drawings"
    tag = None
    for t in c4.dxf_texts(Path(os.path.realpath(files[0]))):
        u = c4.unit_tags(t)
        if u and not c4.XREF_RE.search(t):
            tag = u[0]
            break
    dst = files[1]
    if dst.is_symlink():
        real = Path(os.path.realpath(dst))
        dst.unlink()
        shutil.copy2(real, dst)
    doc = ezdxf.readfile(str(dst))
    doc.modelspace().add_text(tag or "AHU-B-99-99SEED-01", dxfattribs={"height": 5, "insert": (10, 10)})
    doc.saveas(str(dst))
    return f"unit {tag} copied onto {dst.name[:11]}"


def seed_sheet_rows_deleted(sch: Path) -> str:
    sheet = "IP-BH-05010"
    edit_csv(sch / "change_register.csv", lambda rows: rows.__setitem__(slice(None), [r for r in rows if r["sheet"] != sheet]))
    edit_csv(sch / "entity_ledger.csv", lambda rows: rows.__setitem__(slice(None), [r for r in rows if r["sheet"] != sheet]))
    return f"every row of sheet {sheet} deleted from the change list and the ledger"


def seed_pressure_airflow(sch: Path) -> str:
    done = []

    def fn(rows):
        for r in rows:
            if r["quantity"] == "Pressure target" and r["required"] and not done:
                r["required"] = "450"
                done.append(r["change_id"])
    edit_csv(sch / "change_register.csv", fn)
    return "pressure row given an airflow (450): " + ", ".join(done)


def seed_mot_marked(sch: Path) -> str:
    done = []
    tag = sorted(c4.mot_keys())[0]
    mot = [r["tag"] for r in c4.rcsv(c4.REG / "mot_ahu_schedule.csv") if r["tag"].startswith("AHU-")][0]

    def fn(rows):
        for r in rows:
            if r["action"].startswith("Mark up") and r["change_id"] and r["sheet"].startswith("IP-BH-05") and not done:
                r["unit"], r["item"] = mot, mot
                done.append(r["change_id"])
    edit_csv(sch / "change_register.csv", fn)
    return f"MOT-schedule unit {mot} marked up: " + ", ".join(done)


def seed_room_10x(sch: Path) -> str:
    done = []

    def fn(rows):
        for r in rows:
            if r["action"].startswith("Mark up: increase") and r.get("room_no") and c4.num(r["drawn"]) and not done:
                r["link_type"] = "name only"
                r["required"] = str(c4.num(r["drawn"]) * 10)
                done.append(r["change_id"])
    edit_csv(sch / "change_register.csv", fn)
    return "room increase of 10 times on a name-only link: " + ", ".join(done)


def seed_add_return_fan(sch: Path) -> str:
    def fn(rows):
        base = next(r for r in rows if r["layer_type"] == "airflows" and r["sheet"].startswith("IP-BH-"))
        n = dict(base)
        n.update(change_id="CH-" + base["sheet"][6:] + "-9999", action="Mark up: add a return fan", quantity="Return fan airflow",
                 item="RAF-" + base["unit"][4:], layer_type="new systems", **{"class": "to add"})
        rows.append(n)
    edit_csv(sch / "change_register.csv", fn)
    return "'Mark up: add a return fan' on a system drawn once-through"


def seed_pair_summed(sch: Path) -> str:
    led = {r["entity_id"]: r for r in c4.rcsv(sch / "entity_ledger.csv")}
    done = []

    def fn(rows):
        for r in rows:
            comps = [x.strip() for x in re.split(r"[;,]", r.get("ledger_ids", "")) if x.strip()]
            if r["quantity"] in c4.FAN_QUANTITIES and len(comps) == 2 and c4.num(r["drawn"]) and not done:
                v = [c4.num(led[c]["value"]) for c in comps if c in led]
                if len(v) == 2 and v[0] == v[1] and abs(v[0] - c4.num(r["drawn"])) < 0.51:
                    r["drawn"] = str(v[0] + v[1])
                    done.append(r["change_id"])
    edit_csv(sch / "change_register.csv", fn)
    return "duty/standby pair summed as drawn return air: " + ", ".join(done)


def seed_cell_altered(sch: Path) -> str:
    done = []

    def fn(rows):
        for r in rows:
            m = re.search(r"(Workbook D\d\d tab Airflow cell T)(\d+)", r["source"])
            if m and r["change_id"] and not done:
                r["source"] = r["source"].replace(m.group(0), m.group(1) + str(int(m.group(2)) + 1), 1)
                done.append(r["change_id"])
    edit_csv(sch / "change_register.csv", fn)
    return "cited cell altered (next row): " + ", ".join(done)


SEEDS: List[Tuple[str, str, Callable[[Path], str]]] = [
    ("S01", "mark-up box moved to a label (1 of 2)", seed_boxes_to_label),
    ("S03", "drawn value replaced by a sum (1 of 2)", seed_values_to_sum),
    ("S05", "fan tag linked to 3 units", seed_fan_three_units),
    ("S06", "2 drawn fans deleted from the ledger", seed_fans_deleted),
    ("S08", "1 unit copied onto a second drawing", seed_unit_copied),
    ("S09", "1 sheet's rows deleted", seed_sheet_rows_deleted),
    ("S10", "1 pressure row given an airflow", seed_pressure_airflow),
    ("S11", "1 MOT-schedule unit marked up", seed_mot_marked),
    ("S12", "1 room increase of 10 times on a name-only link", seed_room_10x),
    ("S13", "1 'add return fan' instruction on a once-through system", seed_add_return_fan),
    ("S14", "1 duty/standby pair summed as drawn return air", seed_pair_summed),
    ("S15", "1 cited cell altered", seed_cell_altered),
]
# S01 and S03 plant two seeds each (2 boxes moved, 2 values replaced), S06 deletes two fans: 15 seeds in 12 runs
SEED_COUNT = {"S01": 2, "S03": 2, "S06": 2}


def run(scratch: Optional[Path] = None, only: Optional[List[str]] = None) -> Dict[str, Any]:
    src = c4.SCH
    scratch = scratch or Path(tempfile.mkdtemp(prefix="p4seed_"))
    writable: Set[str] = set()                 # the DXF that a seed changes is copied by the seed itself
    saved = (c4.SCH, c4.NEWD, c4.MARK, p4.SCH, p4.NEWD, p4.MARK)
    result: Dict[str, Any] = {"seeds": [], "planted": 0, "caught": 0}
    try:
        base = make_scratch(scratch / "base", src, set())
        point_at(base)
        clean = run_checks()
        for sid, name, fn in SEEDS:
            if only and sid not in only:
                continue
            sch = make_scratch(scratch / sid, src, writable)
            point_at(sch)
            desc = fn(sch)
            point_at(sch)
            after = run_checks()
            new = {cid: sorted(after[cid] - clean[cid]) for cid in after if after[cid] - clean[cid]}
            k = SEED_COUNT.get(sid, 1)
            caught = bool(new)
            result["seeds"].append(dict(id=sid, name=name, planted=k, what=desc, caught=caught,
                                        checks=sorted(new), example=(next(iter(new.values()))[0][:160] if new else "")))
            result["planted"] += k
            result["caught"] += k if caught else 0
            shutil.rmtree(sch)
    finally:
        c4.SCH, c4.NEWD, c4.MARK, p4.SCH, p4.NEWD, p4.MARK = saved
        c4.reset()
        p4._MEMO.clear()
        shutil.rmtree(scratch, ignore_errors=True)
    return result


def render(res: Dict[str, Any], round_no: int = 1) -> str:
    L = ["## Seeded-error test of the phase 4 checks (build line 41)", "",
         f"Round {round_no}, scripts only, scratch copy of out/schematics. Seeds planted: {res['planted']}. Caught: {res['caught']}.", "",
         "| Seed | What was planted | Caught by | Evidence |", "|---|---|---|---|"]
    for s in res["seeds"]:
        L.append(f"| {s['id']} ({s['planted']}) | {s['what'][:110]} | {', '.join(s['checks']) or 'NOT CAUGHT'} | {s['example'].replace('|', '/')} |")
    L.append("")
    return "\n".join(L)


def write_allocation(res: Dict[str, Any], round_no: int = 1) -> Path:
    p = ROOT / "out" / "qa" / "model_allocation.md"
    text = p.read_text(encoding="utf-8") if p.exists() else "# Model allocation\n\n"
    text = re.sub(r"\n## Seeded-error test of the phase 4 checks.*?(?=\n## |\Z)", "\n", text, flags=re.S)
    p.write_text(text.rstrip() + "\n\n" + render(res, round_no), encoding="utf-8")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scratch", type=Path, default=None)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--only", nargs="*", default=None)
    a = ap.parse_args(argv)
    res = run(a.scratch, a.only)
    print(render(res))
    if a.write:
        print("written:", write_allocation(res))
    return 0 if res["caught"] == res["planted"] else 1


if __name__ == "__main__":
    sys.exit(main())
