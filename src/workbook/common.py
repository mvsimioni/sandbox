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


THERMAL_NOTE = ("Calculated airflow and coil duty do not include lighting, small power, equipment or envelope gains. The reduction is an upper limit.")
ROOM_THERMAL_NOTE = ("Thermal load: occupants only. Lighting, small power, equipment and envelope gains are not included, because no approved value is available. "
                     "The thermal-load airflow is a lower limit.")


def tender_remark(tender: str, calc: str, red: str, redp: str, src: str, what: str = "airflow", zero: str = "", lead: str = '""',
                  share_note: bool = True, partial: str = None, note: str = None, all_spec: str = None, thermal: bool = True) -> str:
    """Remarks formula of the D-013 columns, built from the cells of the same row.

    Tender airflow is information only: it is never a target or a cap. A calculated value below it is a reduction, not a failure;
    a calculated value above it is flagged VERIFY (D-012).

    `lead` is an Excel text expression put before the list of causes: a cause that the cells prove, for example a missing ceiling
    height (then every air-change airflow of the room is 0), is named first, so that the reduction is not put down to other causes."""
    causes = ("Causes to examine: area basis, air-change or thermal criteria, duct leakage of "
              "\"&TEXT(DuctLeakage,\"0.0 %\")&\" against \"&TEXT(TenderLeakage,\"0.0 %\")&\" on the tender schematics, outdoor-air rule.")
    zero_b = f'IF({calc}=0,"Calculated airflow is 0 L/s. "&IF({lead}="","{zero}",{lead}),' if zero else ""
    lead_sp = f'IF({lead}="","",{lead}&" ")'
    share = ("This workbook adds up only its own rooms on the unit; the reduction of the whole unit is on the AHU master summary. "
             if share_note else "")
    zero_e = ")" if zero else ""
    # D-012 and D-013: a reduction is a result only when the room list of the unit is complete. `partial` is an Excel text expression that is
    # empty when it is complete and otherwise reads "partial: n of m zoned rooms, x of y m²". No reduction is stated for a partial unit.
    if partial:
        part = (f'IF({partial}<>"",{partial}&". Tender "&TEXT({tender},"#,##0")&" L/s ("&{src}&"). Calculated for the counted rooms: "'
                f'&TEXT({calc},"#,##0")&" L/s. No reduction is stated until the room list of the unit is complete. "&IF({calc}>{tender},"The calculated airflow of the counted rooms is above the tender airflow: VERIFY. ","")&{lead_sp}&"{share}",')
        part_e = ")"
    else:
        part, part_e = "", ""
    none_t = f'IF({note}="","No tender {what} found.","No tender {what} used. "&{note}&".")' if note else '"No tender {what} found."'
    spec_text = "Rooms are specified by the MOT NSC. The difference is the duct leakage allowance and rooms not counted."
    tail = f'IF({all_spec},"{spec_text}","{share}{causes}")' if all_spec else f'"{share}{causes}"'
    body = (f'IF({tender}="",{none_t},IF({tender}=0,"Tender {what} is 0 L/s ("&{src}&"). Calculated "&TEXT({calc},"#,##0")&" L/s. No reduction is defined.",{zero_b}'
            f'{part}IF({calc}>{tender},"VERIFY: calculated "&TEXT({calc},"#,##0")&" L/s is above tender "&TEXT({tender},"#,##0")&" L/s (+"'
            f'&TEXT({calc}/{tender}-1,"0.0%")&") ("&{src}&").",'
            f'IF({calc}={tender},"Calculated "&TEXT({calc},"#,##0")&" L/s equals tender ("&{src}&").",'
            f'"Reduced "&TEXT({tender},"#,##0")&" to "&TEXT({calc},"#,##0")&" L/s (−"&TEXT({redp},"0.0%")&", −"&TEXT({red},"#,##0")'
            f'&" L/s) against tender ("&{src}&"). "&{lead_sp}&{tail})){part_e}){zero_e})')
    if thermal:
        return f'=({body})&" {THERMAL_NOTE}"'
    return "=" + body
