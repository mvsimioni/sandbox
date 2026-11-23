"""Final typographic pass over a finished workbook: banding at the lowest priority, header heights that fit, ID column widths,
left gutter, subtitle block. It changes layout only: no value and no formula."""

from __future__ import annotations

import copy
from typing import Dict, Optional

from openpyxl.styles import Alignment
from openpyxl.utils import get_column_letter

from . import textfit
from .common import apply_banding
from .styles import col_dims

LEFT_HEADER_KEYS = {"remark", "rem", "note", "desc", "name", "crit", "tsrc", "doc", "text", "loc", "serves", "serves2", "label"}


def _width_of(ws, ix: int) -> Optional[float]:
    d = col_dims(ws).get(ix)
    return d.width if d is not None and d.width else None


def _set_width(ws, ix: int, w: float) -> None:
    dims = col_dims(ws)
    d = dims.get(ix)
    letter = get_column_letter(ix)
    if d is not None and (d.min or ix) != (d.max or ix):
        return                      # a range dimension: leave it
    ws.column_dimensions[letter].width = w


def _hidden(ws, ix: int) -> bool:
    d = col_dims(ws).get(ix)
    return bool(d is not None and d.hidden)


def id_widths(ctx) -> Dict[str, float]:
    """Column width for each kind of identifier: the longest Consolas string plus two characters."""
    def w(strings, extra=2.0):
        m = max((textfit.px(str(x), mono=True) for x in strings if x), default=0.0)
        return round((m + 9.0) / 7.0 + extra, 1)
    src = [s["id"] for s in ctx.sources if len(s["id"]) <= 34]      # the few longer criteria IDs are cut at the cell edge; the link shows the full row
    nos = [r["number"] for r in ctx.rooms]
    names = [r.get("name") or "" for r in ctx.rooms]
    ahus = [a["tag"] for a in ctx.ahus] + [r.get("ahu") for r in ctx.rooms]
    tags = [f.get("tag") for f in ctx.fans] + [t.get("tag") for t in getattr(ctx, "terminals", [])] + \
           [v.get("tag") for v in getattr(ctx, "valves", [])]
    name_px = max((textfit.px(n) for n in names), default=0.0)
    return {"src": max(w(src), 12.0), "no": max(w(nos), 12.0), "ahu": max(w(ahus), 20.0), "tag": max(w(tags + ahus), 20.0),
            "name": min(max(round((name_px + 9) / 7.0 + 3, 1), 20.0), 46.0)}


ID_HEADERS = {"Source ID": "src", "Room number": "no", "Room": "no", "AHU or system tag": "ahu", "AHU or system": "ahu", "AHU": "ahu",
              "Original AHU or system": "ahu", "Fan tag": "tag", "Tag": "tag", "Terminal tag": "tag", "Exhaust fan tag": "tag",
              "Room name": "name", "Reference": "tag", "Zone tag": "tag", "Flag": None}


def polish(ctx, wb=None, idw=None, repage=True) -> None:
    wb = wb or ctx.wb
    idw = idw or id_widths(ctx)
    for ws in wb.worksheets:
        apply_banding(ws)
        header_cells = []
        for row in ws.iter_rows():
            for c in row:
                v = c.value
                if v is None:
                    continue
                st = c.style
                if st in ("Header", "HeaderLeft") and c.row > 3:
                    header_cells.append(c)
                al = c.alignment
                if al.horizontal == "left" and not al.indent and st not in ("Title", "Section", "Subhead", "Band", "BandLeft"):
                    c.alignment = Alignment(horizontal="left", vertical=al.vertical, wrap_text=al.wrap_text, indent=1)
        # ID columns by their header text
        for c in header_cells:
            kind = ID_HEADERS.get(str(c.value).strip())
            if kind and not _hidden(ws, c.column) and c.row <= 12 or (kind and str(c.value).strip() == "Source ID"):
                cur = _width_of(ws, c.column) or 8.43
                if idw.get(kind) and cur < idw[kind] and not _hidden(ws, c.column):
                    _set_width(ws, c.column, idw[kind])
        # header words must not break: widen to the longest word; row height from the lines needed
        heights: Dict[int, int] = {}
        for c in header_cells:
            if _hidden(ws, c.column):
                continue
            text = str(c.value)
            need = textfit.width_units(textfit.longest_word_px(text, bold=True))
            cur = _width_of(ws, c.column) or 8.43
            if need > cur:
                _set_width(ws, c.column, min(need, 40.0))
                cur = min(need, 40.0)
            n = textfit.lines(text, cur - 0.5, bold=True)
            while n > 3 and cur < 26.0:          # at most three lines: widen the column
                cur += 1.0
                _set_width(ws, c.column, cur)
                n = textfit.lines(text, cur - 0.5, bold=True)
            heights[c.row] = max(heights.get(c.row, 1), n)
        for row, n in heights.items():
            ws.row_dimensions[row].height = max(13.0 * n + 5.0, 18.0)
        # remark rows: one height, so that the vertical rhythm holds (two lines at the remark width)
        if ws.title in ("AHU", "FCU", "Fans", "Airflow Comparison", "Comparison by AHU", "Reference Rows"):
            for row in ws.iter_rows(min_row=7):
                if any(c.style == "Remark" and c.value is not None for c in row):
                    ws.row_dimensions[row[0].row].height = 28.0
        # units row: no clipping
        for row in range(4, 9):
            for c in ws[row]:
                if c.style == "Unit" and c.value and not _hidden(ws, c.column):
                    need = textfit.width_units(textfit.px(str(c.value), pt=8))
                    cur = _width_of(ws, c.column) or 8.43
                    if need > cur:
                        _set_width(ws, c.column, min(need, 30.0))
        # subtitle: one block across the first columns, wrapped
        a2 = ws["A2"] if ws.title != "README" else None
        if a2 is not None and a2.value and ws.title != "Basis of Design":
            total, last = 0.0, 1
            for ix in range(1, 40):
                if not _hidden(ws, ix):
                    total += _width_of(ws, ix) or 8.43
                last = ix
                if total >= 150:
                    break
            if last > 1:
                ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=last)
                n = textfit.lines(str(a2.value), total, pt=8)
                ws.row_dimensions[2].height = max(12.0 * n + 3.0, 15.0)
                a2.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)

    if repage:
        _repage(wb)


def _repage(wb) -> None:
    """Widths changed after the page set-up: work out again how many A3 pages wide each sheet needs at the minimum legible scale."""
    import re
    from openpyxl.utils import column_index_from_string as ci
    from .styles import A3_PRINTABLE_IN, _col_inches, print_pages_wide
    for ws in wb.worksheets:
        pa = ws.print_area
        if not pa:
            continue
        last_col = ci(re.sub(r"[^A-Z]", "", pa.split(":")[1].replace("$", "")))
        repeat_to = 0
        if ws.freeze_panes:
            m = re.match(r"([A-Z]+)\d+$", ws.freeze_panes)
            fc = ci(m.group(1)) if m else 1
            repeat_to = fc - 1 if fc > 1 else 0
            if repeat_to and sum(_col_inches(ws, last_col)[:repeat_to]) > 0.3 * A3_PRINTABLE_IN:
                repeat_to = 1
        pages = print_pages_wide(ws, last_col, repeat_to)
        ws.page_setup.fitToWidth = pages
        ws.print_title_cols = f"A:{get_column_letter(repeat_to)}" if (pages > 1 and repeat_to) else None
