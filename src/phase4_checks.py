"""Phase 4 checks (brief 9.5): the schematic set.

``python -m src.checks phase4`` runs these checks and writes ``out/qa/check_phase4.md``.

What is read: the change register, the update workbook, the drawing register, the mark-up PDFs, the DXF and PDF of every
new drawing and the workbooks.  The DXF files are read back with the same functions that built them, then compared with
the registers a second time here, so that a drawing and its record cannot differ without a failure.

Simplifications (D-071, readable and traceable, not perfect):
 - the air balance of a drawn room is not tested: the offset of a room is not drawn.  The balance is tested on the workbook
   values that the register uses, per room, per AHU (against the master summary) and per system (against the drawn totals);
 - rooms that take make-up air by transfer (single-pass exhaust, no offset in the workbook) are listed, not tested;
 - the 25 MOT NSC theatre units are covered by the MOT differences tab, not by a drawing.
"""

from __future__ import annotations

import csv
import glob
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
SCH = ROOT / "out" / "schematics"
NEWD = SCH / "new_drawings"
MARK = SCH / "markups"
ISSUE = ROOT / "out" / "issue" / "SCHEMATICS_DRAFT"
PASS, FAIL = "PASS", "FAIL"
ID_RE = re.compile(r"CH-[A-Z0-9]+-\d{4}")
PLACEHOLDER = "CH-APPD-NONE"
TOL_PCT, TOL_ABS = 0.01, 10.0

csv.field_size_limit(10 ** 9)
_MEMO: Dict[str, Any] = {}


def memo(key: str, fn):
    if key not in _MEMO:
        _MEMO[key] = fn()
    return _MEMO[key]


def rcsv(path: Path) -> List[Dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def sample(items, n: int = 6) -> str:
    items = list(items)
    return ", ".join(str(i) for i in items[:n]) + (f" and {len(items) - n} more" if len(items) > n else "")


# --------------------------------------------------------------------------- #
# shared loads
# --------------------------------------------------------------------------- #
def changes() -> List[Dict[str, str]]:
    return memo("changes", lambda: rcsv(SCH / "change_register.csv"))


def drawing_register() -> List[Dict[str, str]]:
    return memo("dreg", lambda: rcsv(SCH / "drawing_register.csv"))


def review():
    def load():
        from src.schematics import review as rv
        return rv.run_review()
    return memo("review", load)


def new_drawings() -> List[Dict[str, Any]]:
    """New drawings of the drawing register with their files and storey."""
    out = []
    for r in drawing_register():
        if r["kind"] != "new drawing":
            continue
        num = r["drawing_number"]
        appd = r["title"].startswith("App D")
        st = r["storey"]
        out.append(dict(number=num, appd=appd, storey=(int(st[1:]) if appd else st), dxf=NEWD / f"{num}_P1.dxf",
                        pdf=NEWD / f"{num}_P1.pdf", values=NEWD / f"{num}_P1_values.csv"))
    return out


def readback(d: Dict[str, Any]) -> Dict[str, Any]:
    def load():
        if d["appd"]:
            from src.schematics import newdwg_check as c
        else:
            from src.schematics import newdwg_missing_check as c
        return c.read_back(d["dxf"])
    return memo("rb:" + d["number"], load)


# --------------------------------------------------------------------------- #
# P4-01 every unit has a register tab and a drawing or a stated reason
# --------------------------------------------------------------------------- #
def check_p4_units(ctx) -> Tuple[str, List[str]]:
    import openpyxl
    from src.schematics.review import tag_keys
    rv = review()
    wb = openpyxl.load_workbook(SCH / "schematic_update_register.xlsx", read_only=True)
    tabs = {}
    for ws in wb.worksheets:
        a = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))[0]
        tabs[str(a)] = ws.title
    mot_rows = list(wb["MOT differences"].iter_rows(values_only=True))[5:]
    wb.close()
    units = [u for u in rv.i.units if u["has_tag"]]
    cov: Dict[str, set] = defaultdict(set)

    def add(tags, why):
        for t in tags:
            for k in tag_keys(t):
                cov[k].add(why)
    marked = {os.path.basename(p)[:11] for p in glob.glob(str(MARK / "*_markup.pdf"))}
    for r in changes():
        if r["sheet"] in marked:
            add(re.split(r";\s*", re.sub(r"^AHU group:\s*", "", r["unit"])), "marked up " + r["sheet"])
    dreg = {r["drawing_number"]: r for r in drawing_register()}
    for s in rv.i.systems:
        if s.get("source_set") == "working" and s["sheet"] in dreg:
            add([a["tag"]["value"] for a in s["ahus"]], ("marked up " if s["sheet"] in marked else "reviewed, no mark-up ") + s["sheet"])
    for tg, inf in rv.unit_info.items():
        for sh_ in inf.get("sheets_found", []):          # a unit that a working sheet prints by its tag
            add([tg], "drawn on " + sh_)
    for r in rcsv(NEWD / "missing_systems_coverage.csv"):
        add([r["item"]], "new drawing " + r["drawing"])
    for p in sorted(glob.glob(str(NEWD / "appd_L0*_coverage.csv"))):
        for r in rcsv(Path(p)):
            if r["kind"] != "Highlighted space":
                add([r["item"]], "new drawing " + r["drawing"])
    add([str(r[1]) for r in mot_rows if r[0] and str(r[0]).startswith("IP-BH-") and r[1]], "MOT differences tab")
    no_tab = [u["tag"] for u in units if u["tag"] not in tabs]
    stated = [u["tag"] for u in units if not (u["akeys"] & set(cov))
              and any(str(r.get("action", "")).startswith("List only (MOT") for r in rv.unit_info.get(u["tag"], {}).get("rows", []))]
    no_dwg = [u["tag"] for u in units if not (u["akeys"] & set(cov)) and u["tag"] not in stated]
    by = Counter()
    for u in units:
        for w in {x.split(" IP-BH")[0].split(" ")[0] + " " + x.split(" ")[1] if " " in x else x for x in cov.get(next(iter(u["akeys"]), ""), set())}:
            by[w] += 1
    ev = [f"{len(units)} units with a tag in the zone schedule; {len(units) - len(no_tab)} have a tab in the update workbook",
          f"{len(units) - len(no_dwg) - len(stated)} units are on a marked-up, reviewed or new drawing, or on the MOT differences tab",
          f"{len(stated)} units have a stated reason: MOT NSC theatre units, listed on their tab, not drawn here: " + sample(stated)]
    if no_tab:
        ev.append("no tab: " + sample(no_tab))
    if no_dwg:
        ev.append("no drawing and no reason: " + sample(no_dwg))
    untagged = [u for u in rv.i.units if not u["has_tag"]]
    ev.append(f"{len(untagged)} zone-schedule lines without a tag (FCU, tag to confirm; spill path) are on the tabs 'FCU no tag' and 'Missing systems'")
    return (FAIL if (no_tab or no_dwg) else PASS), ev


# --------------------------------------------------------------------------- #
# App D reconciliation of tags and spaces over the new drawings
# --------------------------------------------------------------------------- #
def appd_reports() -> Dict[Any, List[Dict[str, Any]]]:
    """compare_registers of every App D sheet, with the sheet number added."""
    def load():
        from src.schematics import newdwg_check as nc
        out: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
        for d in new_drawings():
            if not d["appd"]:
                continue
            rc = nc.compare_registers(readback(d), d["storey"])
            rc["drawing"] = d["number"]
            out[d["storey"]].append(rc)
        return out
    return memo("appd_reports", load)


def tag_keys_of(t: str):
    from src.schematics.review import tag_keys
    return tag_keys(t)


def working_tag_keys() -> set:
    def load():
        from src.schematics.review import tag_keys
        ks = set()
        for e in rcsv(ROOT / "out" / "registers" / "schematic_equipment.csv"):
            if e.get("issue_set") == "working":
                ks |= set(tag_keys(e["tag"]))
        m = json.load(open(ROOT / "out" / "registers" / "schematic_model.json"))
        for s in m["systems"]:
            if s["source_set"] == "working":
                for a in s["ahus"]:
                    ks |= set(tag_keys(a["tag"]["value"]))
        return ks
    return memo("working_tag_keys", load)


def check_p4_appd_tags(ctx) -> Tuple[str, List[str]]:
    reg = ROOT / "out" / "registers"
    bad, ev = [], []
    n_tags = 0
    for st, reps in sorted(appd_reports().items()):
        equip = {e["tag_as_written"] for e in rcsv(reg / "appd_equipment.csv") if e["storey"] == str(st)}
        recon = {r["appd_tag_as_written"] for r in rcsv(reg / "appd_schedule_reconciliation.csv") if r["appd_storeys"] == str(st)}
        want = equip | recon
        on: Counter = Counter()
        for rc in reps:
            for t, n in rc["tags_on"].items():
                on[t] += n
        sched_on: Counter = Counter()
        for rc in reps:
            for t in rc["sched_only_on"]:
                sched_on[t] += 1
        coverage = {r["item"]: r for r in rcsv(NEWD / f"appd_L0{st}_coverage.csv") if r["kind"] != "Highlighted space"}
        for t in sorted(want):
            n = on.get(t, 0)
            if n >= 1 and (tag_keys_of(t) & working_tag_keys()):
                bad.append(f"L0{st} {t}: drawn on an App D sheet and on a working sheet")
                continue
            if n == 1:
                continue
            if n == 0 and sched_on.get(t) == 1 and "listed" in coverage.get(t, {}).get("reason", ""):
                continue                    # listed in the table of schedule items, with its reason
            bad.append(f"L0{st} {t}: {n} times")
        extra = sorted(t for t in on if t not in want)
        if extra:
            bad.append(f"L0{st} on the drawing, not in the sources: {sample(extra)}")
        n_tags += len(want)
        ev.append(f"L0{st}: {len(want)} tags, {len(reps)} sheet(s), {sum(1 for t in want if on.get(t) == 1)} drawn once, "
                  f"{sum(1 for t in want if on.get(t, 0) == 0 and sched_on.get(t) == 1)} listed in the table with a reason")
    ev.append(f"{n_tags} tags in total")
    return (FAIL if bad else PASS), ev + bad[:12]


def check_p4_appd_spaces(ctx) -> Tuple[str, List[str]]:
    reg = ROOT / "out" / "registers"
    bad, ev = [], []
    total = 0
    for st, reps in sorted(appd_reports().items()):
        spaces = {int(r["annotation_index"]) for r in rcsv(reg / "appd_spaces.csv") if r["storey"] == str(st)}
        on: Counter = Counter()
        for rc in reps:
            for i in rc["spaces_on"]:
                on[i] += 1
        wrong = sorted(i for i in spaces if on.get(i, 0) != 1)
        unknown = sorted(i for i in on if i not in spaces)
        total += len(spaces)
        ev.append(f"L0{st}: {len(spaces)} highlighted spaces, {len(spaces) - len(wrong)} in exactly one box")
        if wrong:
            bad.append(f"L0{st} spaces not in exactly one box: {sample(wrong)}")
        if unknown:
            bad.append(f"L0{st} boxes for no space: {sample(unknown)}")
    ev.append(f"{total} highlighted spaces in total")
    return (FAIL if bad else PASS), ev + bad


# --------------------------------------------------------------------------- #
# values on the new drawings equal the register
# --------------------------------------------------------------------------- #
def check_p4_values(ctx) -> Tuple[str, List[str]]:
    errors, ev = [], []
    nvals = ncmp = 0
    for d in new_drawings():
        rb = readback(d)
        vals = rb["vals"]
        file_rows = {r["value_id"]: r for r in rcsv(d["values"])}
        if set(vals) != set(file_rows):
            errors.append(f"{d['number']}: {len(set(vals) ^ set(file_rows))} value IDs differ between the DXF and the values list")
        for vid, v in vals.items():
            fr = file_rows.get(vid)
            if fr and (fr["field"] != v["field"] or fr["change_id"] != v["chg"] or fr["text"] != v["value"]):
                errors.append(f"{d['number']} {vid}: DXF {v['field']}/{v['chg']}/{v['value'][:30]!r} against values list {fr['field']}/{fr['change_id']}/{fr['text'][:30]!r}")
        nvals += len(vals)
        if d["appd"]:
            from src.schematics import newdwg_check as nc
            rc = [r for r in appd_reports()[d["storey"]] if r["drawing"] == d["number"]][0]
            errors += [f"{d['number']}: {e}" for e in rc["errors"]]
            ncmp += rc["compared"]
        else:
            from src.schematics import newdwg_missing_check as mc
            S = memo("missing_sources", mc.Sources)
            r = mc.compare(rb, S, None)
            errors += [f"{d['number']}: {e}" for e in r["errors"]]
            ncmp += r["compared"]
    ev.append(f"{len(new_drawings())} DXF files read back: {nvals} values, {ncmp} compared with the registers and workbook cells, {len(errors)} differences")
    return (FAIL if errors else PASS), ev + errors[:12]


def check_p4_no_placeholder(ctx) -> Tuple[str, List[str]]:
    hits: Dict[str, int] = {}
    for d in new_drawings():
        n = sum(1 for v in readback(d)["vals"].values() if v["chg"] == PLACEHOLDER)
        if n:
            hits[d["number"]] = n
    if hits:
        return FAIL, [f"{sum(hits.values())} values carry the placeholder {PLACEHOLDER}: " + ", ".join(f"{k} {v}" for k, v in hits.items()),
                      "Cause: the change register has no 'new systems' row for APPD:B-06-06STAFF-01 (tag AHU-B-06-06STAFF-01); only the query CH-APPD-0479 exists.",
                      "Clear it: add that row in the review step, then build IP-BH-05067 and IP-BH-05068 again."]
    return PASS, ["no value carries a placeholder change ID"]


def check_p4_overlap(ctx) -> Tuple[str, List[str]]:
    from src.schematics import newdwg_check as nc
    bad, texts = [], 0
    for d in new_drawings():
        r = nc.overlaps(readback(d))
        texts += r["texts"]
        bad += [f"{d['number']}: {e}" for e in r["errors"]]
    return (FAIL if bad else PASS), [f"{len(new_drawings())} drawings, {texts} text lines tested against text, symbols, ducts, strip and frame; {len(bad)} overlaps"] + bad[:10]


def check_p4_pdf_dxf(ctx) -> Tuple[str, List[str]]:
    from src.schematics import newdwg_check as nc
    bad, ev = [], []
    for d in new_drawings():
        r = nc.pdf_vs_dxf(readback(d), d["pdf"])
        if not r["identical"] or abs(r["page_pt"][0] - 3367.8) > 1 or abs(r["page_pt"][1] - 2384.2) > 1:
            bad.append(f"{d['number']}: PDF and DXF differ or the sheet size is wrong ({r['page_pt']})")
    ev.append(f"{len(new_drawings())} PDF files compared with their DXF: same text runs, same line and fill segments, sheet size of the working drawings")
    return (FAIL if bad else PASS), ev + bad


# --------------------------------------------------------------------------- #
# mark-up and register, both ways
# --------------------------------------------------------------------------- #
def check_p4_markup_register(ctx) -> Tuple[str, List[str]]:
    import pymupdf
    rows = changes()
    by_sheet: Dict[str, set] = defaultdict(set)
    for r in rows:
        by_sheet[r["sheet"]].add(r["change_id"])
    bad, n_ann = [], 0
    pdfs = sorted(glob.glob(str(MARK / "*_markup.pdf")))
    for p in pdfs:
        sh = os.path.basename(p)[:11]
        ids, strays = set(), 0
        doc = pymupdf.open(p)
        srcs = sorted(glob.glob(str(ROOT / "data/source/Basis/Working Drawings" / (sh + "_*.pdf"))))
        sdoc = pymupdf.open(srcs[0]) if srcs else None
        for pno, page in enumerate(doc):
            own = {x.xref for x in sdoc[pno].annots()} if sdoc is not None else set()   # the source sheet's own annotations
            for a in page.annots():
                if a.xref in own:
                    continue
                subj = (a.info or {}).get("subject", "")
                if ID_RE.fullmatch(subj or ""):
                    ids.add(subj)
                    n_ann += 1
                else:
                    strays += 1
        doc.close()
        if ids != by_sheet.get(sh, set()):
            bad.append(f"{sh}: {len(ids - by_sheet.get(sh, set()))} IDs in the PDF with no register row; {len(by_sheet.get(sh, set()) - ids)} register rows with no mark-up")
        if strays:
            bad.append(f"{sh}: {strays} annotations with no change ID as subject")
    marked = {os.path.basename(p)[:11] for p in pdfs}
    no_markup = sorted(s for s in by_sheet if s not in marked and s not in ("NEW", "App D"))
    if no_markup:
        bad.append("register rows on sheets with no mark-up copy: " + sample(no_markup))
    # new-drawing rows: each ID of the register for NEW and App D appears on a new drawing
    on_new: set = set()
    for d in new_drawings():
        for t in readback(d)["texts"]:
            on_new.update(ID_RE.findall(t["s"]))
        on_new.update(v["chg"] for v in readback(d)["vals"].values())
    listed = {r["change_id"] for r in rcsv(SCH / "missing_systems.csv")}
    want = {r["change_id"]: r for r in rows if r["sheet"] in ("NEW", "App D")}
    alltext = " ".join(t["s"] for d in new_drawings() for t in readback(d)["texts"])
    on_list = {i for i, r in want.items() if i not in on_new and r["layer_type"] == "queries"
               and (i in listed or r["sheet"] == "App D")}          # App D queries stay on the reconciliation tab
    fcu_listed = {r["change_id"] for r in rcsv(NEWD / "fcu_notag_coverage.csv") if not r["drawing"] and r["reason"]}
    on_list |= {i for i in fcu_listed if i in want and i not in on_new}      # listed with a reason: no room, no storey
    on_tag = {i for i, r in want.items() if i not in on_new and i not in on_list and r["layer_type"] == "new systems"
              and r["quantity"] == "Unit with no schematic" and r["item"] and r["item"] in alltext}   # App D tag drawn under its App D row
    missing = sorted(i for i in want if i not in on_new and i not in on_list and i not in on_tag)
    ev = [f"{len(pdfs)} mark-up PDFs, {n_ann} annotations, each with a change ID as subject; set of IDs equal to the register rows of its sheet, both ways",
          f"{len(want)} register rows of new systems and App D: {len(want) - len(missing) - len(on_list) - len(on_tag)} appear on a new drawing as a value, a query or a listed item; "
          f"{len(on_tag)} are App D tags drawn under their App D row; {len(on_list)} are queries that stay on the list of missing systems or on the App D reconciliation tab"]
    if missing:
        cats = Counter((want[i]["layer_type"], want[i]["quantity"]) for i in missing)
        bad.append(f"{len(missing)} register rows of NEW or App D on no new drawing and not on the list ({dict(cats)}): {sample(missing)}")
    return (FAIL if bad else PASS), ev + bad[:12]


def check_p4_workbook_ids(ctx) -> Tuple[str, List[str]]:
    import openpyxl
    wb = openpyxl.load_workbook(SCH / "schematic_update_register.xlsx", read_only=True)
    ids: set = set()
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            for c in row:
                if isinstance(c, str) and ID_RE.fullmatch(c):
                    ids.add(c)
    wb.close()
    reg = {r["change_id"] for r in changes()}
    a, b = sorted(reg - ids), sorted(ids - reg)
    ev = [f"{len(reg)} change IDs in the change list; {len(ids)} in the update workbook"]
    if a:
        ev.append(f"in the change list and not in the workbook: {sample(a)}")
    if b:
        ev.append(f"in the workbook and not in the change list: {sample(b)}")
    return (FAIL if a or b else PASS), ev


# --------------------------------------------------------------------------- #
# air in equals air out
# --------------------------------------------------------------------------- #
def _num(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def workbook_rooms() -> List[Dict[str, Any]]:
    def load():
        import openpyxl
        rows = []
        for p in sorted(glob.glob(str(ROOT / "out/loads/workbooks/AIGH_C2_D*_RevA.xlsx"))):
            wb = openpyxl.load_workbook(p, data_only=True, read_only=True)
            allr = list(wb["Airflow"].iter_rows(values_only=True))
            wb.close()
            hi = [i for i, r in enumerate(allr) if r[0] == "Room number"][0]
            head = allr[hi]
            for r in allr[hi + 2:]:
                if r[0] in (None, ""):
                    continue
                d = dict(zip(head, r))
                d["_wb"] = Path(p).name.split("_")[2]
                rows.append(d)
        return rows
    return memo("wbrooms", load)


def room_balance() -> Dict[str, Any]:
    ok = 0
    bad, exempt_mot, transfer = [], [], []
    for d in workbook_rooms():
        s, te = _num(d["Final supply"]), _num(d["Total extract"])
        if s is None or te is None:
            continue
        exh, ret = _num(d["Exhaust"]) or 0.0, _num(d["Return"]) or 0.0
        off = _num(d["Offset needed"]) or 0.0
        kind = d["Pressure offset kind"]
        sign = {"SUPPLY": 1.0, "EXHAUST": -1.0}.get(kind, 0.0)
        tol = max(TOL_PCT * s, TOL_ABS)
        key = f"{d['_wb']} {d['Room number']}"
        if abs(te - (exh + ret)) > tol:
            bad.append(f"{key}: total extract {te} is not exhaust {exh} plus return {ret}")
            continue
        res = s - te - sign * off
        if abs(res) <= tol:
            ok += 1
        elif _num(d["Fixed by specification (MOT)"]) == 1:
            exempt_mot.append(key)
        elif kind is None and te > s:
            transfer.append(key)           # make-up air by transfer or from a source the workbook does not hold
        else:
            bad.append(f"{key}: supply {s}, extract {te}, offset {sign * off:+.1f}, residual {res:+.1f} L/s")
    return dict(ok=ok, bad=bad, mot=exempt_mot, transfer=transfer)


def ahu_balance() -> Dict[str, Any]:
    import openpyxl
    wb = openpyxl.load_workbook(ROOT / "out/loads/workbooks/AHU_master_summary.xlsx", data_only=True, read_only=True)
    allr = list(wb["AHU Master"].iter_rows(values_only=True))
    wb.close()
    hi = [i for i, r in enumerate(allr) if r[0] == "AHU or system"][0]
    head = allr[hi]
    n = ok = 0
    bad, skipped = [], 0
    for r in allr[hi + 2:]:
        if not r[0]:
            continue
        d = dict(zip(head, r))
        rs, lk, calc = _num(d["Room supply"]), _num(d["Duct leakage"]), _num(d["Calculated airflow"])
        if rs is None or lk is None or calc is None:
            skipped += 1
            continue
        n += 1
        exp = rs * (1 + (lk if lk < 1 else lk / 100.0))
        if abs(calc - exp) <= max(TOL_PCT * calc, TOL_ABS):
            ok += 1
        else:
            bad.append(f"{d['AHU or system']}: calculated {calc:.0f}, room supply {rs:.0f} x (1 + {lk:g}) = {exp:.0f}")
    return dict(n=n, ok=ok, bad=bad, skipped=skipped)


def system_balance() -> Dict[str, Any]:
    """Drawn systems: AHU supply on the sheet against the sum of the drawn room supplies x 1.15, with the register row that records each difference."""
    model = json.load(open(ROOT / "out/registers/schematic_model.json"))
    from src.schematics.review import tag_keys
    rowkeys = set()
    for r in review().rows:           # every row of the update workbook, with or without a change ID
        if (r["row_type"] == "AHU total" and r["quantity"] == "Supply airflow") or \
                r["quantity"] == "Unit drawn on the sheet, not in the zone schedule":
            for u in re.split(r";\s*", re.sub(r"^AHU group:\s*", "", r["unit"])):
                rowkeys |= set(tag_keys(u))
    n = ok = mot = near = 0
    open_diff = []
    rv = review()
    for s in model["systems"]:
        t = s.get("totals_register")
        if s["kind"] != "working_ahu_system" or not t or t.get("rooms_SA_x_1_15_L_s") is None:
            continue
        duty = sum((a.get("duty_supply_L_s") or {}).get("value") or 0 for a in s["ahus"])
        if not duty:
            continue
        n += 1
        diff = duty - t["rooms_SA_x_1_15_L_s"]
        if abs(diff) <= max(TOL_PCT * duty, TOL_ABS):
            ok += 1
            continue
        tags = [a["tag"]["value"] for a in s["ahus"]]
        ks = set()
        for t_ in tags:
            ks |= set(tag_keys(t_))
            m = re.match(r"^(.*?)(\d\d)/(\d\d)[A-Z]?$", t_)         # '04ICU-01/02C' is two units
            if m:
                ks |= set(tag_keys(m.group(1) + m.group(2))) | set(tag_keys(m.group(1) + m.group(3)))
        if ks & rv.i.mot_keys or s["sheet"] in ("IP-BH-07050", "IP-BH-07051", "IP-BH-07052"):
            mot += 1                      # MOT NSC theatre units: specified values, listed and not tested
            continue
        if t.get("check_result") == "near":          # within 5 %: inside the tolerance of the review itself
            near += 1
            continue
        if not (ks & rowkeys):
            open_diff.append(f"{s['system_id']}: AHU {duty:.0f}, rooms x 1.15 {t['rooms_SA_x_1_15_L_s']:.0f}")
    return dict(n=n, ok=ok, mot=mot, near=near, recorded=n - ok - mot - near - len(open_diff), open=open_diff)


def check_p4_balance(ctx) -> Tuple[str, List[str]]:
    rb, ab, sb = room_balance(), ahu_balance(), system_balance()
    tested = rb["ok"] + len(rb["bad"])
    ev = [f"rooms (workbook Airflow tabs, {tested} tested): {rb['ok']} within 1 % or 10 L/s of supply = extract + signed offset; "
          f"{len(rb['transfer'])} transfer-air rooms and {len(rb['mot'])} MOT-specified rooms listed, not tested",
          f"AHUs (master summary, {ab['n']} tested): {ab['ok']} equal room supply x (1 + leakage) within 1 % or 10 L/s",
          f"drawn systems ({sb['n']}): {sb['ok']} within 1 % or 10 L/s of rooms x 1.15; {sb['recorded']} differ and have register rows for their unit; {sb['mot']} MOT theatre systems listed, not tested; {sb['near']} within 5 %, inside the tolerance of the review; "
          f"{len(sb['open'])} differ by more than that and the update workbook has no AHU total row for the unit"]
    bad = rb["bad"] + ab["bad"] + sb["open"]
    return (FAIL if bad else PASS), ev + bad[:12]


# --------------------------------------------------------------------------- #
# drawing register and issue folder
# --------------------------------------------------------------------------- #
def check_p4_drawing_register(ctx) -> Tuple[str, List[str]]:
    reg = drawing_register()
    bad = []
    nums = Counter(r["drawing_number"] for r in reg)
    bad += [f"{n}: {c} rows" for n, c in nums.items() if c > 1]
    for r in reg:
        for f in filter(None, r["files"].split("; ")):
            if not (ROOT / f).exists():
                bad.append(f"{r['drawing_number']}: file {f} missing")
        for col in ("revision", "status", "kind", "sheet_size"):
            if not r[col]:
                bad.append(f"{r['drawing_number']}: no {col}")
        if r["kind"] == "new drawing" and (r["revision"] != "P1" or not r["status"].startswith("DRAFT")):
            bad.append(f"{r['drawing_number']}: new drawing without revision P1 or status DRAFT")
    marked = {os.path.basename(p)[:11] for p in glob.glob(str(MARK / "*_markup.pdf"))}
    got = {r["drawing_number"] for r in reg if r["kind"] == "mark-up"}
    if marked != got:
        bad.append(f"mark-up PDFs and register rows differ: {sample(sorted(marked ^ got))}")
    newfiles = {os.path.basename(p)[:11] for p in glob.glob(str(NEWD / "IP-BH-*_P1.dxf"))}
    got = {r["drawing_number"] for r in reg if r["kind"] == "new drawing"}
    if newfiles != got:
        bad.append(f"new DXF files and register rows differ: {sample(sorted(newfiles ^ got))}")
    # every working sheet of the model has a row
    model = json.load(open(ROOT / "out/registers/schematic_model.json"))
    missing = sorted(set(model["sheets"]) - set(nums))
    if missing:
        bad.append("working sheets with no row: " + sample(missing))
    kinds = Counter(r["kind"] for r in reg)
    return (FAIL if bad else PASS), [f"{len(reg)} rows: " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items()))] + bad[:10]


def check_p4_issue(ctx) -> Tuple[str, List[str]]:
    if not ISSUE.exists():
        return FAIL, [f"{ISSUE} does not exist"]
    reg = rcsv(ISSUE / "drawing_register.csv")
    bad = []
    n = 0
    for r in reg:
        for f in filter(None, r["files"].split("; ")):
            n += 1
            if not (ISSUE / f).exists():
                bad.append(f"{r['drawing_number']}: {f} missing in the issue folder")
    for f in ("schematic_update_register.xlsx", "drawing_register.csv", "missing_systems.csv", "README.md"):
        if not (ISSUE / f).exists():
            bad.append(f"{f} missing")
    # the copies equal the sources
    for sub, src in (("markups", MARK), ("new_drawings", NEWD)):
        for p in sorted((ISSUE / sub).glob("*")):
            if p.suffix == ".pdf" and sub == "markups":
                import pymupdf
                a, b = pymupdf.open(p), pymupdf.open(src / p.name)
                if a[0].read_contents() != b[0].read_contents() or len(list(a[0].annots())) != len(list(b[0].annots())):
                    bad.append(f"{sub}/{p.name}: page content or annotations differ from the source")
    # language of the README
    readme = ISSUE / "README.md"
    if readme.exists():
        from src.workbook.conf import forbidden_terms
        text = readme.read_text(encoding="utf-8")
        terms = forbidden_terms(ROOT)
        for t in terms:
            m = re.search(r"\b" + re.escape(t) + r"(s|es)?\b", text, 0 if t == "AI" else re.I)
            if m:
                bad.append(f"README: forbidden term {t!r}")
        from src.phase_checks import lint_sentences
        prose = "\n".join(l for l in text.splitlines() if not l.startswith(("|", "#")))   # tables and headings are not sentences
        long_ = lint_sentences(prose)
        if long_:
            bad.append(f"README: {len(long_)} sentences with more than 25 words")
    # language of the other files: update workbook, lists, text of the new drawings, text of the mark-ups (D-015).
    # A word that the source drawing prints in a room name (WORKFLOW TRAINING) stays as written.
    if ISSUE.exists():
        bad += language_hits()
    return (FAIL if bad else PASS), [f"{n} files of the drawing register in the issue folder"] + bad[:10]


def language_hits() -> List[str]:
    from src.workbook.conf import forbidden_terms
    import openpyxl
    import pymupdf
    terms = forbidden_terms(ROOT)
    hits: Counter = Counter()
    first: Dict[str, str] = {}

    def scan(src: str, text: str) -> None:
        for t in terms:
            for m in re.finditer(r"\b" + re.escape(t) + r"(s|es)?\b", text, 0 if t == "AI" else re.I):
                if t == "workflow" and re.match(r"\s*TRAINING", text[m.end():], re.I):
                    continue
                hits[(src, t)] += 1
                first.setdefault(f"{src}/{t}", text[max(0, m.start() - 25):m.end() + 25].replace("\n", " "))
    wb = openpyxl.load_workbook(ISSUE / "schematic_update_register.xlsx", read_only=True)
    for ws in wb:
        for row in ws.iter_rows(values_only=True):
            for v in row:
                if isinstance(v, str):
                    scan("update workbook", v)
    for f in ("change_list.csv", "missing_systems.csv", "drawing_register.csv"):
        if (ISSUE / f).exists():
            for r in rcsv(ISSUE / f):
                for v in r.values():
                    scan(f, v or "")
    for p in sorted((ISSUE / "new_drawings").glob("*.pdf")):
        d = pymupdf.open(p)
        scan("new drawings", "\n".join(pg.get_text() for pg in d))
    for p in sorted((ISSUE / "markups").glob("*.pdf")):
        d = pymupdf.open(p)
        for pg in d:
            for a in pg.annots():
                scan("mark-ups", a.info.get("content", "") or "")
    return [f"{src}: forbidden term {t!r} {n} times, for example: {first.get(f'{src}/{t}', '')}" for (src, t), n in sorted(hits.items())]
