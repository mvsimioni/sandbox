"""Shared table helpers: header rows, formula templating, conditional formats."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from .ctx import Ctx
from .model import Layout
from .styles import FILL_FAIL, FILL_BAND, put

_LOCAL = re.compile(r"\{(\w+)\}")
_CROSS = re.compile(r"\{([A-Z]{2,3})\.(\w+)\}")
_RANGE = re.compile(r"\[([A-Z]{2,3})\.(\w+)\]")


def expand(ctx: Ctx, tpl: str, lay: Layout, i: int, idx: Optional[Dict[str, int]] = None) -> str:
    """Expand a formula template.

    ``{key}``       same-sheet cell of the current row of ``lay``;
    ``{RI.key}``    cell of layout RI at row index ``idx['RI']`` (default ``i``);
    ``[EN.key]``    absolute full-column range of a layout.
    """
    idx = idx or {}

    def cross(m):
        L = ctx.lay[m.group(1)]
        return L.addr(m.group(2), idx.get(m.group(1), i))

    def rng(m):
        return ctx.lay[m.group(1)].rng(m.group(2))

    out = _CROSS.sub(cross, tpl)
    out = _RANGE.sub(rng, out)

    def local(m):
        return f"{lay.col(m.group(1))}{lay.row(i)}"

    return _LOCAL.sub(local, out)


def write_headers(ws, lay: Layout, spec: List[Tuple[str, str, str, float]], band_row: int, bands: Dict[str, str],
                  *, unit_row: bool = True, code_row: Optional[Dict[str, str]] = None) -> None:
    """Header block: band row, header row, unit row (optional code row)."""
    hdr = band_row + 1
    left_keys = {"remark", "rem", "note", "desc", "name", "crit", "tsrc", "doc", "text", "loc", "serves", "serves2", "label", "page", "issue", "method",
                 "flag", "why", "oasrc", "oaby", "oacond", "type_s"}
    for key, header, unit, width in spec:
        c = lay.col(key)
        put(ws, f"{c}{hdr}", header, "HeaderLeft" if key in left_keys else "Header")
        if unit_row:
            put(ws, f"{c}{hdr + 1}", unit or None, "Unit")
        if code_row is not None:
            put(ws, f"{c}{hdr + 2}", code_row.get(key), "Unit")
        ws.column_dimensions[c].width = width
    order = sorted(bands, key=lambda k: lay.idx[k])
    last_idx = max(lay.idx[k] for k, _, _, _ in spec)
    for n, key in enumerate(order):
        a = lay.idx[key]
        b = (lay.idx[order[n + 1]] - 1) if n + 1 < len(order) else last_idx
        for ix in range(a, b + 1):
            put(ws, f"{get_column_letter(ix)}{band_row}", bands[key].upper() if ix == a else None, "Band")
    ws.row_dimensions[hdr].height = 42


def band_rows(ws, rng: str) -> None:
    """Register a banded range. The rule is added by ``apply_banding`` after every other conditional format of the sheet, so that it has
    the lowest priority: a failed check keeps its light-red fill and bold on every row."""
    if not hasattr(ws, "_bands"):
        ws._bands = []
    ws._bands.append(rng)


def apply_banding(ws) -> None:
    for rng in getattr(ws, "_bands", []):
        first = rng.split(":")[0].replace("$", "")
        ws.conditional_formatting.add(rng, FormulaRule(formula=["MOD(ROW(),2)=0"], fill=PatternFill("solid", bgColor="FFF4F4F4",
                                                                                                    fgColor="FFF4F4F4")))
    ws._bands = []


def cf_text(ws, rng: str, first_cell: str, text: str, *, bold=True, fill=FILL_FAIL) -> None:
    ws.conditional_formatting.add(
        rng, FormulaRule(formula=[f'{first_cell}="{text}"'], font=Font(bold=bold),
                         fill=PatternFill("solid", bgColor=fill, fgColor=fill)))


def cf_formula(ws, rng: str, formula: str, *, bold=True, fill=FILL_FAIL) -> None:
    ws.conditional_formatting.add(
        rng, FormulaRule(formula=[formula], font=Font(bold=bold), fill=PatternFill("solid", bgColor=fill, fgColor=fill)))


def list_validation(ws, rng: str, name: str, allow_blank: bool = True) -> None:
    dv = DataValidation(type="list", formula1=f"={name}", allow_blank=allow_blank, showErrorMessage=True)
    ws.add_data_validation(dv)
    dv.add(rng)


def title_block(ws, title: str, subtitle: str, ctx: Ctx) -> None:
    put(ws, "A1", title, "Title")
    put(ws, "A2", subtitle, "Note")
    ws["A2"].alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
    ws.row_dimensions[1].height = 28


def group_cols(ws, lay: Layout, first_key: str, last_key: str, collapsed: bool = False) -> None:
    # one dimension per column (openpyxl's own group() drops the widths of all but the first column)
    from openpyxl.utils import get_column_letter
    for ix in range(lay.idx[first_key], lay.idx[last_key] + 1):
        dim = ws.column_dimensions[get_column_letter(ix)]
        dim.outline_level = 1
        dim.hidden = collapsed


THERMAL_NOTE_SHORT = "Calculated airflow and coil duty include people, lighting and small power. Equipment gain is not included and envelope gain is not assessed."
THERMAL_NOTE = "The calculated airflow does not include equipment or envelope gains. The reduction is an upper limit."      # only on a text that states a reduction
ROOM_THERMAL_NOTE = ("Thermal load: people, lighting and small power. Equipment gain is 0 W and envelope gain is not assessed. "
                     "The thermal-load airflow is a lower limit.")
COMPLETE = "Complete"          # a completeness text starts with this word when every zoned room of the unit is counted (D-027)


def incomplete(part: str) -> str:
    """Excel boolean: the completeness text `part` says 'Not complete' (the room list of the unit is not complete)."""
    return f'ISNUMBER(SEARCH("Not complete",{part}))'


def tender_remark(tender: str, calc: str, red: str, redp: str, src: str, what: str = "airflow", zero: str = "", lead: str = '""',
                  share_note: bool = True, partial: str = None, note: str = None, all_spec: str = None, thermal: bool = True,
                  oa_above: str = None, oa_calc: str = None, oa_tender: str = None, crit_partial: bool = False,
                  extra: str = None, small: tuple = None) -> str:
    """Remarks formula of the D-013 columns, built from the cells of the same row (D-027).

    Tender airflow is information only: it is never a target or a cap (D-012). The text states
    * VERIFY where the calculated airflow is above tender, and VERIFY where the calculated outdoor air is above the tender outdoor airflow (on every unit and every row);
    * a reduction only where the calculated airflow is below tender and the room list of the unit is complete (`partial` is the completeness text of the unit: it
      starts with 'Complete' when every zoned room is counted);
    * nothing about a reduction in every other case: no 'partial' text and no 'no reduction' text.
    `lead` is an Excel text expression put before the list of causes of a stated reduction (a missing ceiling height, a missing criteria row).
    `extra` is an Excel text expression (empty text when it does not apply) added at the end of the result (the single-pass room that sets the fraction).
    `small` is (condition, text): a text added to a stated reduction for a unit whose zone is one or two small rooms (open for Marcus).
    The sentence 'The reduction is an upper limit.' is on a text that states a reduction and on no other."""
    causes = ("Causes to examine: area basis, air-change or thermal criteria, duct leakage of "
              "\"&TEXT(DuctLeakage,\"0.0 %\")&\" against \"&TEXT(TenderLeakage,\"0.0 %\")&\" on the tender schematics, outdoor-air rule.")
    fn = f'&" {THERMAL_NOTE}"' if thermal else ""
    zero_b = f'IF({calc}=0,"Calculated airflow is 0 L/s. "&IF({lead}="","{zero}",{lead}),' if zero else ""
    lead_sp = f'IF({lead}="","",{lead}&" ")'
    if oa_above:
        oa_s = (f'"Outdoor air "&TEXT({oa_calc},"#,##0")&" L/s is above tender outdoor air "&TEXT({oa_tender},"#,##0")&" L/s ("&{src}&"). VERIFY. "')
        oa_add = f'&IF({oa_above}," "&{oa_s},"")'
    else:
        oa_s, oa_add = None, ""
    share = ("This workbook adds up only its own rooms on the unit; the reduction of the whole unit is on the AHU master summary. "
             if share_note else "")
    zero_e = ")" if zero else ""
    none_t = f'IF({note}="","No tender {what} found.","No tender {what} used. "&{note}&".")' if note else f'"No tender {what} found."'
    spec_text = "Rooms are specified by the MOT NSC. The difference is the duct leakage allowance and rooms not counted."
    tail = f'IF({all_spec},"{spec_text}","{share}{causes}")' if all_spec else f'"{share}{causes}"'
    small_t = f'&IF({small[0]}," "&{small[1]},"")' if small else ""
    complete = f"NOT({incomplete(partial)})" if partial else "TRUE"
    ex_sp = f'&IF({extra}<>""," "&{extra},"")' if extra else ""
    ex_alone = extra if extra else '""'
    reduced = (f'"Reduced "&TEXT({tender},"#,##0")&" to "&TEXT({calc},"#,##0")&" L/s (−"&TEXT({redp},"0.0%")&", −"&TEXT({red},"#,##0")'
               f'&" L/s) against tender ("&{src}&"). "&{lead_sp}&{tail}{small_t}{fn}{ex_sp}')
    above = (f'"VERIFY: calculated "&TEXT({calc},"#,##0")&" L/s is above tender "&TEXT({tender},"#,##0")&" L/s (+"'
             f'&TEXT({calc}/{tender}-1,"0.0%")&") ("&{src}&")."{oa_add}{ex_sp}')
    if oa_above:
        # outdoor air above tender outdoor air: VERIFY, and the supply is not worded as a reduction for this unit
        oa_only = f'IF({oa_above},"Supply "&TEXT({calc},"#,##0")&" L/s against tender "&TEXT({tender},"#,##0")&" L/s ("&{src}&"). "&{oa_s}{ex_sp},'
        oa_only_e = ")"
    else:
        oa_only, oa_only_e = "", ""
    main = (f'{zero_b}IF({calc}>{tender},{above},{oa_only}IF(AND({complete},ISNUMBER({red}),{red}>0),{reduced},{ex_alone}){oa_only_e}){zero_e}')
    body = (f'IF({tender}="",{none_t}{ex_sp},IF({tender}=0,"Tender {what} is 0 L/s ("&{src}&"). Calculated "&TEXT({calc},"#,##0")&" L/s."{ex_sp},{main}))')
    return "=" + body
