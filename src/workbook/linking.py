"""Links between the DPA rooms of a department and the other room tables: RDS, RLS ceiling heights and plan rooms.

Every link carries its basis in words. A room that no rule links stays unlinked and is listed on both sides: nothing is forced and no
conflict between two documents is resolved without a flag.

* RDS: by room code (and variant where the RDS is variant-specific), then by normalised name within the department.
* RLS ceiling height: by the same code and variant; a room with no RLS height keeps a flagged default.
* Plan rooms and AHU: see `plan_links` (name or abbreviation, department, storey, count; then the department rule).
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# room codes
# --------------------------------------------------------------------------- #
NO_CODE = {"", "-", "–", "—", "NA", "N/A"}


def _alnum(s: Optional[str]) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def split_code(code: Optional[str]) -> Tuple[str, str, str]:
    """(number, code, variant) of a standard room code: '054_ST-T_P1C0' gives ('054', 'ST-T', 'P1C0'), '092B_DCNA _S' gives
    ('092B', 'DCNA', 'S'), 'MNPK-B' gives ('', 'MNPK-B', ''). Blank and '-' give three empty parts. All parts are upper case with no spaces."""
    c = re.sub(r"\s+", "", (code or "").upper())
    if c in NO_CODE:
        return "", "", ""
    parts = c.split("_")
    if len(parts) >= 3:
        return parts[0], "_".join(parts[1:-1]), parts[-1]
    if len(parts) == 2 and re.fullmatch(r"\d+[A-Z]?", parts[0]):
        return parts[0], parts[1], ""
    return "", c, ""


def code_key(code: Optional[str]) -> str:
    """Comparison key of a full code (letters and digits only). Empty for a blank code, so two blanks never match."""
    n, b, v = split_code(code)
    return _alnum(n + b + v) if (n or b or v) else ""


def base_key(code: Optional[str]) -> str:
    n, b, _ = split_code(code)
    return _alnum(n + b) if (n or b) else ""


_NAME_DROP = re.compile(r"\b(i)\b")


def name_key(name: Optional[str]) -> str:
    """Normalised room name: lower case, '&' as 'and', a lone 'I' or '|' (the printed '/') removed, no punctuation."""
    t = (name or "").lower().replace("&", " and ").replace("|", " ").replace("/", " ")
    t = _NAME_DROP.sub(" ", t)
    return re.sub(r"[^a-z0-9]", "", t)


_MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def issue_date(text: Optional[str]) -> Tuple[int, int]:
    """(year, month) of an issue date printed as 'SEP 2026'; (0, 0) when it cannot be read."""
    m = re.search(r"([A-Za-z]{3})[A-Za-z]*\.?\s+(\d{4})", text or "")
    if m and m.group(1).upper() in _MONTHS:
        return int(m.group(2)), _MONTHS[m.group(1).upper()]
    return 0, 0


def _squash(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


# --------------------------------------------------------------------------- #
# DPA to RDS
# --------------------------------------------------------------------------- #
def _resolve_rds_rows(rows: Sequence[Dict[str, str]]) -> Dict[str, Any]:
    """One RDS room code can have several rows (issues, files). The latest issue date governs. Where the rows of that issue give
    different occupancy texts, the conflict is returned and no value is chosen. Older issues that differ are named in `notes`."""
    latest = max(issue_date(r.get("date")) for r in rows)
    newest = [r for r in rows if issue_date(r.get("date")) == latest]
    texts: List[str] = []
    for r in newest:
        t = _squash(r.get("occupancy"))
        if t and t not in texts:
            texts.append(t)
    notes: List[str] = []
    row = next((r for r in newest if _squash(r.get("occupancy"))), newest[0])
    conflict = texts if len(texts) > 1 else []
    for r in rows:
        if r in newest:
            continue
        t = _squash(r.get("occupancy"))
        if t and (not texts or t not in texts):
            notes.append(f"the {r.get('date') or 'earlier'} issue gives '{(r.get('occupancy') or '').strip()}'")
    if texts and any(not _squash(r.get("occupancy")) for r in newest):
        notes.append("another file of the same issue leaves the occupancy field empty")
    return dict(row=row, rows=list(rows), issue=(r_date(row)), conflict=conflict, notes=notes)


def r_date(row: Dict[str, str]) -> str:
    return f"{row.get('revision', '')} {row.get('date', '')}".strip()


def link_rds(dpa_rooms: Sequence[Dict[str, Any]], rds_rows: Sequence[Dict[str, str]]) -> Dict[str, Any]:
    """Link DPA rooms (dicts with 'number', 'name', 'code') to the RDS rows of the same department.

    Order of the rules for one DPA room:
    1. code and variant: the full standard code equals the RDS room code (both with a variant, or both without one);
    2. code, RDS not variant-specific: the number and code are equal and the RDS code has no variant;
    3. name: the normalised name equals the normalised RDS name and all rows with that name carry one RDS code.
    A room code with the same number and code but another variant is not a link (the occupancy of another variant is not this room's).
    A blank code never matches a blank code. Where several RDS codes carry the name, the room stays unlinked.

    Returns {'links': number -> {basis, code, resolved (see _resolve_rds_rows)}, 'unlinked_dpa': [{number, name, code, reason}],
             'unlinked_rds': [{code, name, files, reason}]}."""
    by_full: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    by_base: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    by_name: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for r in rds_rows:
        ck = code_key(r.get("room_code"))
        if ck:
            by_full[ck].append(r)
            n, b, v = split_code(r.get("room_code"))
            if n or v:
                by_base[base_key(r.get("room_code"))].append(r)
        nk = name_key(r.get("room_name"))
        if nk:
            by_name[nk].append(r)
    links: Dict[str, Dict[str, Any]] = {}
    unlinked: List[Dict[str, Any]] = []
    used_codes = set()
    for d in dpa_rooms:
        number = d["number"]
        code = d.get("code") or ""
        ck, bk = code_key(code), base_key(code)
        n, b, v = split_code(code)
        basis, rows = None, None
        if ck and ck in by_full:
            rows = by_full[ck]
            basis = "code and variant" if v else "code"
        elif ck and v and bk in by_full and all(not split_code(x.get("room_code"))[2] for x in by_full[bk]):
            rows = by_full[bk]
            basis = "code (RDS not variant-specific)"
        reason = ""
        if rows is None:
            nk = name_key(d.get("name"))
            cands = by_name.get(nk, []) if nk else []
            codes = {code_key(x.get("room_code")) for x in cands}
            if cands and len(codes) == 1:
                rows = cands
                basis = "name within the department"
            elif cands:
                reason = "the name is carried by several RDS codes: " + ", ".join(sorted({(x.get("room_code") or "").strip() for x in cands}))
            else:
                others = sorted({split_code(x.get("room_code"))[2] for x in rds_rows
                                 if bk and base_key(x.get("room_code")) == bk and split_code(x.get("room_code"))[2]})
                if bk and v and others:
                    reason = f"the RDS has this code for variant {', '.join(others)}, not variant {v}"
                else:
                    reason = "no RDS code or name of the department matches"
        if rows is not None:
            res = _resolve_rds_rows(rows)
            rcode = (res["row"].get("room_code") or "").strip()
            code_note = ""
            if basis == "name within the department" and ck and code_key(rcode) != ck:
                code_note = (f"the DPA room code {code.strip()} differs from the RDS room code {rcode}; the link rests on the name only"
                             + (f" ({'same number and code, other variant' if bk == base_key(rcode) else 'another code'})"))
            links[number] = dict(basis=basis, code=rcode, resolved=res, code_note=code_note)
            used_codes.add(code_key(res["row"].get("room_code")))
        else:
            unlinked.append(dict(number=number, name=d.get("name", ""), code=code.strip(), reason=reason))
    unl_rds: List[Dict[str, Any]] = []
    seen = set()
    for r in rds_rows:
        ck = code_key(r.get("room_code")) or "name:" + name_key(r.get("room_name"))
        if ck in used_codes or ck in seen:
            continue
        seen.add(ck)
        files = sorted({Path(x.get("rel_path") or "").name for x in rds_rows if (code_key(x.get("room_code")) or "name:" + name_key(x.get("room_name"))) == ck})
        unl_rds.append(dict(code=(r.get("room_code") or "").strip(), name=(r.get("room_name") or "").strip(), files=files,
                            reason="no DPA room of this workbook links to it"))
    return dict(links=links, unlinked_dpa=unlinked, unlinked_rds=unl_rds)


# --------------------------------------------------------------------------- #
# DPA to RLS ceiling heights
# --------------------------------------------------------------------------- #
RLS_FILE = Path("out/registers/rls_ceiling_heights.csv")
_CODE_COLS = ("room_type_code", "std_room_code", "soa_std_room_code", "room_code", "rls_code", "code")
_VAR_COLS = ("variant",)
_NUM_COLS = ("rls_number",)
_MM_COLS = ("governing_height_mm", "ceiling_height_mm", "ch_mm", "height_mm", "ceiling_height")
_M_COLS = ("ceiling_height_m", "ch_m", "height_m")
_DOC_COLS = ("source_file", "rel_path", "file")
_PAGE_COLS = ("page", "source_page", "rls_page")
_TEXT_COLS = ("height_text_as_printed", "source_text", "text", "ch_text", "text_as_read", "raw_text", "raw")


def _col(row: Dict[str, str], cols: Sequence[str]) -> str:
    for c in cols:
        if (row.get(c) or "").strip():
            return row[c].strip()
    return ""


def rls_printed_values_m(row: Dict[str, str]) -> List[float]:
    """Every ceiling height printed on the sheet, in metres, from the 'CH=' notes (D-035: where a sheet prints two or more, the highest is used)."""
    txt = (row.get("heights_mm_as_printed") or "")
    vals = []
    for m in re.finditer(r"(\d{3,5}(?:\.\d+)?)", txt):
        v = float(m.group(1))
        vals.append(round(v / 1000.0 if v > 20 else v, 4))
    return sorted(set(vals))


def rls_height_m(row: Dict[str, str]) -> Optional[float]:
    """Ceiling height of an RLS register row in metres. A value above 20 is read as millimetres (a ceiling is between 1 and 20 m). None when the row gives none.
    D-035: where the sheet prints more than one height, the highest of them."""
    printed = rls_printed_values_m(row)
    if printed:
        return printed[-1]
    for cols in (_M_COLS, _MM_COLS):
        t = _col(row, cols)
        if not t:
            continue
        try:
            v = float(t.replace(",", ""))
        except ValueError:
            continue
        return round(v / 1000.0 if v > 20 else v, 4)
    return None


def load_rls(root: Path) -> List[Dict[str, str]]:
    p = Path(root) / RLS_FILE
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _dept_of(value: Any) -> str:
    m = re.match(r"\s*0*(\d+)", str(value or ""))
    return m.group(1) if m else ""


_VARIANT_IN_NAME = re.compile(r"P\d+C\d+", re.I)


def _rls_key(row: Dict[str, str]) -> Tuple[str, str, str, str]:
    """(number, code, variant, where the variant comes from) of an RLS register row.

    The register carries the short room type code ('PTRY') and keeps the number in its own column (`rls_number`); a full code
    ('205_PTRY_P1C7') is also read. The variant comes from its own column. Where the column is empty, it is read from the file name of the
    sheet when that name prints '<number>_<code>_<variant>' for this row (a sheet that carries several room types prints the variant of each one
    in its name, for example 'RLS_205_PTRY_P1C7, 072_CHST-LKR_P1C3_...'); the fourth part says so ('column', 'file name' or '')."""
    n, b, v = split_code(_col(row, _CODE_COLS))
    num_txt = n or _col(row, _NUM_COLS)
    n = _alnum(num_txt)
    code_txt = b
    b = _alnum(b)
    v = _alnum(v or _col(row, _VAR_COLS))
    if v:
        return n, b, v, "column"
    name = Path(_col(row, _DOC_COLS)).name
    if n and code_txt:
        m = re.search(rf"(?<![A-Z0-9]){re.escape(num_txt.strip())}_{re.escape(code_txt)}_({_VARIANT_IN_NAME.pattern})(?![A-Z0-9])", name, re.I)
        if m:
            return n, b, _alnum(m.group(1)), "file name"
    return n, b, "", ""


def link_rls(dpa_rooms: Sequence[Dict[str, Any]], rls_rows: Sequence[Dict[str, str]], dept: Optional[str] = None) -> Dict[str, Any]:
    """Ceiling height of each DPA room from the RLS register, linked by the standard room code of the room's own department.

    Only the rows of department `dept` are read (the department number of the register, `dept_no`; None, or a register with no department column, reads every row). A code can carry different heights in different departments: they are different documents and never pool.
    The code of a DPA room is '<number>_<code>_<variant>'. A register row is a candidate when its room type code equals the code and, where
    both sides print a number, the numbers are equal: 'PTRY' number 183 (Pantry - Patient) is not 'PTRY' number 205. Then:
    1. code and variant: the variants are equal;
    2. code, RLS not variant-specific: the DPA room has a variant and every candidate row that gives a height has no variant (the RDS rule).
       A code that is variant-specific for some rows and variant-free for others is not a fallback: the other variants are not this room's.
    A row with no height is not used. Rows of one link that give different heights, or a row with a conflict flag, are a conflict: no height is
    chosen and the room is listed. A room with no link has no height here (the caller applies the flagged default).
    Returns {'heights': number -> {height_m, basis, code, row, n_rows}, 'conflicts': [{number, code, values}], 'unlinked_dpa': [...],
             'unlinked_rls': [{number, code, variant, height_m, rows}]}."""
    want = _dept_of(dept) if dept is not None else ""
    if want and not any((r.get("dept_no") or "").strip() for r in rls_rows):
        want = ""                                   # a register with no department column: every row is read
    pool: List[Tuple[Dict[str, str], Tuple[str, str, str, str]]] = []
    for r in rls_rows:
        if want and _dept_of(r.get("dept_no")) != want:
            continue
        k = _rls_key(r)
        if k[1] and rls_height_m(r) is not None:
            pool.append((r, k))
    by_code: Dict[str, List[Tuple[Dict[str, str], Tuple[str, str, str, str]]]] = defaultdict(list)
    for r, k in pool:
        by_code[k[1]].append((r, k))
    heights: Dict[str, Dict[str, Any]] = {}
    conflicts: List[Dict[str, Any]] = []
    unlinked: List[Dict[str, Any]] = []
    used = set()
    for d in dpa_rooms:
        code = d.get("code") or ""
        n, b, v = split_code(code)
        n, b, v = _alnum(n), _alnum(b), _alnum(v)
        cands = [(r, k) for r, k in by_code.get(b, []) if not (n and k[0] and k[0] != n)] if b else []
        rows, basis = None, ""
        if cands and v:
            exact = [x for x in cands if x[1][2] == v]
            if exact:
                rows, basis = exact, "code and variant"
            elif all(not x[1][2] for x in cands):
                rows, basis = cands, "code (RLS not variant-specific)"
        elif cands and not v:
            free = [x for x in cands if not x[1][2]]
            if free:
                rows, basis = free, "code"
        if rows is None:
            variants = sorted({x[1][2] for x in cands if x[1][2]})
            numbers = sorted({x[1][0] for x in by_code.get(b, []) if x[1][0]}) if b else []
            if not b:
                why = "the DPA room has no standard room code"
            elif not by_code.get(b):
                why = "no RLS height for this code in the department"
            elif not cands:
                why = f"the RLS of the department has this code under number {', '.join(numbers)}, not number {n}"
            elif variants:
                why = f"the RLS has this code for variant {', '.join(variants)}, not variant {v or '(none)'}"
            else:
                why = "no RLS height for this code in the department"
            unlinked.append(dict(number=d["number"], name=d.get("name", ""), code=code.strip(), reason=why))
            continue
        rr = [x[0] for x in rows]
        vals = sorted({rls_height_m(x) for x in rr})
        every = sorted({v for x in rr for v in (rls_printed_values_m(x) or [rls_height_m(x)]) if v is not None})
        used.update(id(x) for x in rr)
        # D-035: the highest height printed (no conflict is listed any more where a sheet prints two or more)
        heights[d["number"]] = dict(height_m=vals[-1], basis=basis, code=_col(rr[0], _CODE_COLS), row=rr[0], n_rows=len(rr),
                                    variant_from=rows[0][1][3], values=every)
    grouped: Dict[Tuple[str, str, str], List[Dict[str, str]]] = defaultdict(list)
    for r, k in pool:
        if id(r) not in used:
            grouped[(k[0], k[1], k[2])].append(r)
    unl_rls = [dict(number=k[0], code=_col(rr[0], _CODE_COLS), variant=k[2], height_m=rls_height_m(rr[0]), rows=len(rr)) for k, rr in grouped.items()]
    return dict(heights=heights, conflicts=conflicts, unlinked_dpa=unlinked, unlinked_rls=unl_rls)


def rls_source_fields(row: Dict[str, str]) -> Dict[str, str]:
    return dict(document=Path(_col(row, _DOC_COLS)).name or "Room layout sheet", page=_col(row, _PAGE_COLS), text=_col(row, _TEXT_COLS))


# --------------------------------------------------------------------------- #
# DPA to plan rooms and AHU
# --------------------------------------------------------------------------- #
GENERIC_PLAN_NAMES = {"room", "area", "rm", "space", "zone", "unit"}


def plan_system(za: Dict[str, str], tag_cols: Tuple[str, ...], kinds: Iterable[str]) -> Tuple[str, str]:
    """(system tag, class kind) of a zone assignment row: the tag column, else the class when its kind is an air-handling system."""
    kind = (za.get("class_kind") or "").upper()
    for c in tag_cols:
        if (za.get(c) or "").strip():
            return za[c].strip(), kind
    if kind in set(kinds):
        return (za.get("class") or "").strip(), kind
    return "", kind


def _class_value(p: Dict[str, Any], tag_cols: Tuple[str, ...], kinds: Iterable[str]) -> str:
    tag, kind = plan_system(p["zone"], tag_cols, kinds)
    return tag or (p["zone"].get("class") or "").strip() or "(none)"


def _usable_plan_name(name: str) -> bool:
    w = re.findall(r"[a-z0-9]+", (name or "").lower())
    return bool(w) and not all(x in GENERIC_PLAN_NAMES for x in w)


def _dpa_area(d: Dict[str, Any]) -> float:
    return float(d.get("ref_area") or d.get("dpa_area") or 0.0)


def area_plausible(cands: Sequence[Dict[str, Any]], group: Sequence[Dict[str, Any]], ratio: float, each: bool = False) -> Tuple[bool, float, Optional[float]]:
    """Area plausibility of a unit link (R3-02). `cands` are the DPA rooms that the link gives the unit of `group` (plan rooms).

    The total design area of the DPA rooms (a typical room counted as many times as its quantity) must be within the factor `ratio` of the total measured area of
    the plan rooms, on both sides. With `each` (the department rule: the counts differ) the design area of each DPA room must be within the factor `ratio` of the area
    of at least one plan room of the group. Where a plan room has no measured area, or the DPA rooms have no area, the test cannot be made and the link stands.
    Returns (plausible, total DPA area, total plan area or None)."""
    dpa_total = sum(_dpa_area(d) * int(d.get("typical_count") or 1) for d in cands)
    areas = [p.get("area") for p in group]
    if not dpa_total or not areas or any(a is None or a <= 0 for a in areas):
        return True, dpa_total, None
    plan_total = float(sum(areas))
    if each:
        ok = all(any(1.0 / ratio <= _dpa_area(d) / float(a) <= ratio for a in areas) for d in cands if _dpa_area(d))
    else:
        ok = 1.0 / ratio <= dpa_total / plan_total <= ratio
    return ok, dpa_total, plan_total


def plan_links(dpa_rooms: Sequence[Dict[str, Any]], plan_rooms: Sequence[Dict[str, Any]], excluded: Iterable[str],
               score: Callable[[str, str], float], tag_cols: Tuple[str, ...], kinds: Iterable[str], min_score: float = 0.85,
               rule_min_score: float = 0.95, max_area_ratio: Optional[float] = None,
               rejected: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Dict[str, Any]]:
    """Zoning links for the DPA rooms that the strict room match left unmatched (`excluded` holds the numbers that are already linked).

    1. 'name and count': the plan rooms of the storey whose name fits the DPA room name (name or abbreviation, `score` >= `min_score`), taken as
       the plan-name group; when the number of DPA rooms of the storey that fit that plan name (each counted `typical_count` times, default 1)
       equals the number of plan rooms in the group, and all plan rooms of the group carry one AHU or system class, each DPA room of the group
       takes that class. The area of the plan room is not taken (the strict match decides that).
    2. 'department rule' (flag 'AHU by department rule'): a DPA room that is still without a plan room takes the AHU of the plan rooms of the
       same name in the department on its storey when all of them share one AHU, whatever the count. This rule needs the name or the abbreviation
       to be the same (`rule_min_score`), not only to contain it: 'AGV Dispatch (OT)' is not 'AGV Dispatch'.
    A plan room with no measured area (a tag of a leaked region, a tag with no closed region) takes part: only its adjudicated AHU is used.
    A plan name that is generic ('Room', 'Area') links nothing. A group whose plan rooms carry different AHUs links nothing: nothing is forced.
    Area plausibility (R3-02, with `max_area_ratio`): a link needs the total design area of the DPA rooms and the measured area of the plan rooms within that factor
    (for the department rule: the design area of the room within that factor of one plan room of the group). A link that fails is not made and is appended to
    `rejected` (dicts with the DPA room numbers, the plan room ids, the basis and the two areas).
    Returns number -> {basis ('name and count' | 'department rule'), plan: [plan rooms], value (tag or class), tag, kind, n_dpa, n_plan}."""
    excluded = set(excluded)
    out: Dict[str, Dict[str, Any]] = {}
    storeys = sorted({d["storey"] for d in dpa_rooms})
    for st in storeys:
        d_st = [d for d in dpa_rooms if d["storey"] == st]
        p_st = [p for p in plan_rooms if p["storey"] == st and (not p.get("shared") or p.get("area") is None) and _usable_plan_name(p["name"])]
        groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for p in p_st:
            groups[name_key(p["name"])].append(p)
        fit: Dict[str, List[str]] = defaultdict(list)           # DPA number -> plan-name keys that fit
        for d in d_st:
            for k, g in groups.items():
                if score(d["name"], g[0]["name"]) >= min_score:
                    fit[d["number"]].append(k)
        sat: Dict[str, Tuple[str, str, str]] = {}                # plan-name key -> (value, tag, kind) of a group whose count and AHU agree
        sat_n: Dict[str, int] = {}
        for k, g in groups.items():
            cands = [d for d in d_st if k in fit[d["number"]]]
            if not cands:
                continue
            n_dpa = sum(int(d.get("typical_count") or 1) for d in cands)
            vals = {_class_value(p, tag_cols, kinds) for p in g}
            if n_dpa != len(g) or len(vals) != 1:
                continue
            if max_area_ratio:
                ok_, dt_, pt_ = area_plausible(cands, g, max_area_ratio)
                if not ok_:
                    if rejected is not None:
                        rejected.append(dict(dpa=[d["number"] for d in cands], plan=[p["poly"]["room_id"] for p in g], plan_name=g[0]["name"], basis="name and count",
                                             dpa_area=dt_, plan_area=pt_))
                    continue
            tag, kind = plan_system(g[0]["zone"], tag_cols, kinds)
            sat[k] = (next(iter(vals)), tag, kind)
            sat_n[k] = n_dpa
        for d in d_st:
            ks = fit[d["number"]]
            if d["number"] in excluded or d["number"] in out or not ks or any(k not in sat for k in ks):
                continue
            if len({sat[k] for k in ks}) != 1:                    # the plan names that fit this room carry different AHUs: nothing is forced
                continue
            value, tag, kind = sat[ks[0]]
            plan_g = [p for k in ks for p in groups[k]]
            out[d["number"]] = dict(basis="name and count", plan=plan_g, value=value, tag=tag, kind=kind, n_dpa=sat_n[ks[0]], n_plan=len(groups[ks[0]]))
        for d in d_st:
            if d["number"] in excluded or d["number"] in out:
                continue
            strict = [k for k in fit[d["number"]] if score(d["name"], groups[k][0]["name"]) >= rule_min_score]
            if len(strict) != 1 or len(fit[d["number"]]) != 1:
                continue
            g = groups[strict[0]]
            vals = {_class_value(p, tag_cols, kinds) for p in g}
            if len(vals) != 1:
                continue
            tag, kind = plan_system(g[0]["zone"], tag_cols, kinds)
            if max_area_ratio:
                ok_, dt_, pt_ = area_plausible([d], g, max_area_ratio, each=True)
                if not ok_:
                    if rejected is not None:
                        rejected.append(dict(dpa=[d["number"]], plan=[p["poly"]["room_id"] for p in g], plan_name=g[0]["name"], basis="department rule",
                                             dpa_area=dt_, plan_area=pt_))
                    continue
            out[d["number"]] = dict(basis="department rule", plan=list(g), value=next(iter(vals)), tag=tag, kind=kind,
                                    n_dpa=sum(int(x.get("typical_count") or 1) for x in d_st if fit[x["number"]] == fit[d["number"]]), n_plan=len(g))
    return out
