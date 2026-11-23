"""Named styles, cell writer and page set-up for the department workbook (Section 8.2).

Colour is never the only signal: every coded cell has a named style and a code letter.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from openpyxl.styles import Alignment, Border, Font, NamedStyle, PatternFill, Side
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.worksheet.hyperlink import Hyperlink

FONT = "Arial"
MONO = "Consolas"
BLUE = "FF0000FF"
GREEN = "FF007A33"
GREY_TXT = "FF595959"
BLACK = "FF000000"
FILL_SPEC = "FFD9D9D9"
FILL_ASSUME = "FFFFF2CC"
FILL_FAIL = "FFF4C7C3"
FILL_HEAD = "FFF0F2F5"
FILL_BAND = "FFF7F7F7"

try:
    from .conf import occupancy_style as _occ_style
    OCC_STYLE = _occ_style()
except Exception:                                                  # configuration unreadable: built-in values
    OCC_STYLE = dict(font_colour="FFC00000", named_style="Assumption-Occupancy", code="A")
OCC_NAME = OCC_STYLE["named_style"]
try:
    from .conf import load_style as _load_style, presumed_style as _presumed_style
    LOAD_STYLE = _load_style()
    PRES_STYLE = _presumed_style()
except Exception:
    LOAD_STYLE = dict(font_colour="FFC00000", named_style="Assumption-Load", code="A")
    PRES_STYLE = dict(font_colour="FF00B050", font_italic=True, named_style="Presumed-Criteria", code="A")
LOAD_NAME = LOAD_STYLE["named_style"]        # D-025: plug load and lighting assumed from ASHRAE (red text)
PRES_NAME = PRES_STYLE["named_style"]        # D-026: criteria presumed from the same space in another department (italic green)

HAIR = Side(style="hair", color="FF808080")
THIN = Side(style="thin", color="FF404040")

# code letter -> (named style, meaning)
CODE_STYLES: Dict[str, tuple] = {
    "D": ("Input", "Value taken from a document"),
    "M": ("Input", "Value measured from a drawing"),
    "S": ("Specified", "Value fixed by a specification or set by the engineer"),
    "A": ("Assumption", "Assumed value, to confirm"),
    "C": ("Calc", "Formula"),
    "L": ("Link", "Formula that only brings a value from another tab"),
    "R": ("Result", "Governing result or total"),
    "K": ("Check", "Pass or fail test"),
}
# styles allowed for a code letter (tag and total variants)
CODE_ALLOWED: Dict[str, set] = {
    "D": {"Input", "InputTag"}, "M": {"Input", "InputTag"}, "S": {"Specified", "SpecifiedTag"},
    "A": {"Assumption", "AssumptionTag", OCC_NAME, LOAD_NAME, PRES_NAME}, "C": {"Calc", "CalcTag"}, "L": {"Link", "LinkTag"},
    "R": {"Result", "Total", "ResultTag"}, "K": {"Check"},
}

FMT = {
    "int": '#,##0;"−"#,##0;"–"',
    "d0": '#,##0;"−"#,##0;"–"',
    "d1": '#,##0.0;"−"#,##0.0;"–"',
    "d2": '#,##0.00;"−"#,##0.00;"–"',
    "d3": '#,##0.000;"−"#,##0.000;"–"',
    "pct0": '0%;"−"0%;"–"',
    "pct1": '0.0%;"−"0.0%;"–"',
    "text": "@",
    "date": "yyyy-mm-dd",
}


def _font(color=BLACK, bold=False, italic=False, size=10, name=FONT):
    return Font(name=name, size=size, bold=bold, italic=italic, color=color)


def _ns(name, font, fill=None, align=None, border=None):
    s = NamedStyle(name=name)
    s.font = font
    if fill:
        s.fill = PatternFill("solid", fgColor=fill)
    s.alignment = align or Alignment(vertical="center")
    if border:
        s.border = border
    return s


def set_default_font(wb) -> None:
    """Normal style = Arial 10, so that empty cells, default row heights and footers use the body face."""
    from openpyxl.utils.indexed_list import IndexedList
    base = Font(name=FONT, size=10, family=2, scheme=None)
    fonts = IndexedList([base])
    for f in list(wb._fonts)[1:]:
        fonts.add(f)
    wb._fonts = fonts
    try:
        wb._named_styles["Normal"].font = base
    except Exception:
        pass


def install_styles(wb) -> None:
    """Add every named style used by the workbook."""
    set_default_font(wb)
    left = Alignment(horizontal="left", vertical="center")
    right = Alignment(horizontal="right", vertical="center")
    centre = Alignment(horizontal="center", vertical="center")
    wrap = Alignment(horizontal="left", vertical="top", wrap_text=True)
    defs = [
        _ns("Title", _font(bold=True, size=18), align=left),
        _ns("Section", _font(bold=True, size=12), align=left),
        _ns("Subhead", _font(bold=True), align=left),
        _ns("Header", _font(bold=True), fill=FILL_HEAD, border=Border(bottom=HAIR),
            align=Alignment(horizontal="center", vertical="center", wrap_text=True)),
        _ns("HeaderLeft", _font(bold=True), fill=FILL_HEAD, border=Border(bottom=HAIR),
            align=Alignment(horizontal="left", vertical="center", wrap_text=True)),
        _ns("Unit", _font(color=GREY_TXT, size=8), fill=FILL_HEAD, align=centre),
        _ns("Text", _font(), align=left),
        _ns("TextWrap", _font(), align=wrap),
        _ns("Tag", _font(name=MONO), align=left),
        _ns("Code", _font(name=MONO), align=centre),
        _ns("Note", _font(color=GREY_TXT, italic=True, size=8), align=Alignment(horizontal="left", vertical="center",
                                                                              wrap_text=True)),
        _ns("Remark", _font(), align=Alignment(horizontal="left", vertical="top", wrap_text=True)),
        _ns("Band", _font(bold=True), fill=FILL_HEAD, border=Border(bottom=HAIR),
            align=Alignment(horizontal="centerContinuous", vertical="center")),
        _ns("BandLeft", _font(bold=True), fill=FILL_HEAD, border=Border(bottom=HAIR),
            align=Alignment(horizontal="left", vertical="center")),
        _ns("Input", _font(color=BLUE), align=right),
        _ns("InputTag", _font(color=BLUE, name=MONO), align=left),
        _ns("Specified", _font(color=BLUE), fill=FILL_SPEC, align=right),
        _ns("SpecifiedTag", _font(color=BLUE, name=MONO), fill=FILL_SPEC, align=left),
        _ns("Assumption", _font(color=BLUE), fill=FILL_ASSUME, align=right),
        _ns("AssumptionTag", _font(color=BLUE, name=MONO), fill=FILL_ASSUME, align=left),
        _ns(OCC_STYLE["named_style"], _font(color=OCC_STYLE["font_colour"]), fill=FILL_ASSUME, align=right),
        _ns(LOAD_STYLE["named_style"], _font(color=LOAD_STYLE["font_colour"]), fill=FILL_ASSUME, align=right),
        _ns(PRES_STYLE["named_style"], _font(color=PRES_STYLE["font_colour"], italic=bool(PRES_STYLE.get("font_italic", True))), align=right),
        _ns("Calc", _font(), align=right),
        _ns("CalcTag", _font(name=MONO), align=left),
        _ns("Link", _font(color=GREEN), align=right),
        _ns("LinkTag", _font(color=GREEN, name=MONO), align=left),
        _ns("Result", _font(bold=True), border=Border(top=HAIR), align=right),
        _ns("ResultTag", _font(bold=True, name=MONO), border=Border(top=HAIR), align=left),
        _ns("Total", _font(bold=True), border=Border(top=HAIR), align=right),
        _ns("ResultText", _font(bold=True), border=Border(top=HAIR), align=left),
        _ns("ResultCheck", _font(bold=True), border=Border(top=HAIR), align=centre),
        _ns("Check", _font(), align=centre),
    ]
    for s in defs:
        wb.add_named_style(s)


def tag_variant(style: str) -> str:
    """Monospace variant of a numeric/text style for tags and IDs."""
    return {"Input": "InputTag", "Specified": "SpecifiedTag", "Assumption": "AssumptionTag", "Calc": "CalcTag",
            "Link": "LinkTag", "Result": "ResultTag", "Total": "ResultTag"}.get(style, style)


def style_for_code(code: str) -> str:
    return CODE_STYLES[code][0]


_LINK = re.compile(r"^=((?:'[^']+'|[A-Za-z_][\w ]*)!)?\$?[A-Z]{1,3}\$?\d+$")


def put(ws, ref: str, value: Any, style: str, fmt: Optional[str] = None, align: Optional[str] = None, text: bool = False):
    """Write a value or formula with a named style (and optional number format key or string).

    A text link to another cell (``=Sheet!A1``) shows nothing, not 0, where the source cell is blank: unknown stays unknown."""
    c = ws[ref]
    if isinstance(value, str) and (text or style in ("LinkTag",)) and _LINK.match(value):
        value = f'=IF({value[1:]}="","",{value[1:]})'
    c.value = value
    c.style = style
    if fmt:
        c.number_format = FMT.get(fmt, fmt)
    if align:
        c.alignment = Alignment(horizontal=align, vertical="center", wrap_text=c.alignment.wrap_text)
    return c


def link_to(cell, sheet: str, target: str) -> None:
    """Internal hyperlink to ``sheet!target``."""
    cell.hyperlink = Hyperlink(ref=cell.coordinate, location=f"'{sheet}'!{target}", display=str(cell.value))


FOOTER_DEPT = ""          # set by the workbook builder: department number and name, put in the footer of every tab
PRINT_MIN_SCALE = 0.75     # body text 10 pt must print at no less than 7.5 pt
A3_PRINTABLE_IN = 16.54 - 1.0   # A3 landscape width less 0.5 in margins


def col_dims(ws) -> Dict[int, Any]:
    """Column dimension of every column index, expanding the min..max ranges that openpyxl keys by their first letter."""
    out: Dict[int, Any] = {}
    for key, dim in ws.column_dimensions.items():
        lo = dim.min or column_index_from_string(key)
        hi = dim.max or lo
        for i in range(lo, hi + 1):
            out[i] = dim
    return out


def _col_inches(ws, last_col: int) -> List[float]:
    """Approximate printed width of columns 1..last_col in inches (7 px per width unit + 5 px padding, 96 dpi);
    hidden (collapsed) columns print nothing."""
    dims = col_dims(ws)
    out = []
    for i in range(1, last_col + 1):
        dim = dims.get(i)
        if dim is not None and dim.hidden:
            out.append(0.0)
            continue
        w = dim.width if dim is not None and dim.width else 8.43
        out.append((7.0 * w + 5.0) / 96.0)
    return out


def print_pages_wide(ws, last_col: int, repeat_to: int) -> int:
    """Smallest number of A3 pages across that keeps the print scale at or above PRINT_MIN_SCALE."""
    widths = _col_inches(ws, last_col)
    total = sum(widths)
    rep = sum(widths[:repeat_to])
    if total * PRINT_MIN_SCALE <= A3_PRINTABLE_IN:
        return 1
    n = 2
    while n < 40 and n * (A3_PRINTABLE_IN - PRINT_MIN_SCALE * rep) < PRINT_MIN_SCALE * (total - rep):
        n += 1
    return n


def setup_sheet(ws, title: str, *, rev: str, date: str, project: str, print_rows: Optional[str] = None,
                last_col: int = 10, last_row: int = 50, tab_colour: Optional[str] = None,
                freeze: Optional[str] = None, zoom: int = 90) -> None:
    """Gridlines off, A3 landscape, repeated header rows, footer, print area. A sheet wider than one A3 page at 75 %
    prints across as many pages as it needs, with its label columns repeated, so that no column is cut and the text
    stays legible (brief 8.5)."""
    ws.sheet_view.showGridLines = False
    ws.sheet_view.zoomScale = zoom
    ws.page_setup.paperSize = 8
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_options.horizontalCentered = False
    ws.page_margins.left = ws.page_margins.right = 0.5
    ws.page_margins.top = 0.75
    ws.page_margins.bottom = 0.75
    if print_rows:
        ws.print_title_rows = print_rows
    last_col = max(last_col, 1)
    ws.print_area = f"A1:{get_column_letter(last_col)}{max(last_row, 1)}"
    repeat_to = 0
    if freeze:
        m = re.match(r"([A-Z]+)\d+$", freeze)
        if m:
            fc = column_index_from_string(m.group(1))
            repeat_to = fc if fc > 1 else 0
            widths = _col_inches(ws, last_col)
            if repeat_to and sum(widths[:repeat_to]) > 0.3 * A3_PRINTABLE_IN:
                repeat_to = 1
    pages = print_pages_wide(ws, last_col, repeat_to)
    ws.page_setup.fitToWidth = pages
    if pages > 1 and repeat_to:
        ws.print_title_cols = f"A:{get_column_letter(repeat_to)}"
    left = f"{project}" + (f"  |  {FOOTER_DEPT}" if FOOTER_DEPT else "")
    for part, text in ((ws.oddFooter.left, left), (ws.oddFooter.center, "&A"), (ws.oddFooter.right, f"Rev {rev}  {date}  Page &P of &N")):
        part.text = text
        part.size = 8
        part.font = "Arial,Regular"
    ws.page_setup.pageOrder = "overThenDown"
    if tab_colour:
        ws.sheet_properties.tabColor = tab_colour
    if freeze:
        ws.freeze_panes = freeze


def col_widths(ws, widths: Dict[int, float]) -> None:
    for idx, w in widths.items():
        ws.column_dimensions[get_column_letter(idx)].width = w
