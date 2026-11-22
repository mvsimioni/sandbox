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
    for key, header, unit, width in spec:
        c = lay.col(key)
        put(ws, f"{c}{hdr}", header, "Header")
        if unit_row:
            put(ws, f"{c}{hdr + 1}", unit or None, "Unit")
        if code_row is not None:
            put(ws, f"{c}{hdr + 2}", code_row.get(key), "Unit")
        ws.column_dimensions[c].width = width
    for key, text in bands.items():
        put(ws, f"{lay.col(key)}{band_row}", text, "HeaderLeft")
    ws.row_dimensions[hdr].height = 42


def band_rows(ws, rng: str) -> None:
    ws.conditional_formatting.add(rng, FormulaRule(formula=["MOD(ROW(),2)=0"], fill=PatternFill("solid", bgColor="FFF4F4F4",
                                                                                                fgColor="FFF4F4F4")))


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
    ws["A2"].alignment = Alignment(horizontal="left", vertical="top", wrap_text=False)
    ws.row_dimensions[1].height = 28


def group_cols(ws, lay: Layout, first_key: str, last_key: str, collapsed: bool = False) -> None:
    # one dimension per column (openpyxl's own group() drops the widths of all but the first column)
    from openpyxl.utils import get_column_letter
    for ix in range(lay.idx[first_key], lay.idx[last_key] + 1):
        dim = ws.column_dimensions[get_column_letter(ix)]
        dim.outline_level = 1
        dim.hidden = collapsed
