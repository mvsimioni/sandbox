"""Named styles, cell writer and page set-up for the department workbook (Section 8.2).

Colour is never the only signal: every coded cell has a named style and a code letter.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from openpyxl.styles import Alignment, Border, Font, NamedStyle, PatternFill, Side
from openpyxl.utils import get_column_letter
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
    "A": {"Assumption", "AssumptionTag"}, "C": {"Calc", "CalcTag"}, "L": {"Link", "LinkTag"},
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


def install_styles(wb) -> None:
    """Add every named style used by the workbook."""
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
        _ns("Note", _font(color=GREY_TXT, italic=True, size=8), align=Alignment(horizontal="left", vertical="top",
                                                                              wrap_text=True)),
        _ns("Input", _font(color=BLUE), align=right),
        _ns("InputTag", _font(color=BLUE, name=MONO), align=left),
        _ns("Specified", _font(color=BLUE), fill=FILL_SPEC, align=right),
        _ns("SpecifiedTag", _font(color=BLUE, name=MONO), fill=FILL_SPEC, align=left),
        _ns("Assumption", _font(color=BLUE), fill=FILL_ASSUME, align=right),
        _ns("AssumptionTag", _font(color=BLUE, name=MONO), fill=FILL_ASSUME, align=left),
        _ns("Calc", _font(), align=right),
        _ns("CalcTag", _font(name=MONO), align=left),
        _ns("Link", _font(color=GREEN), align=right),
        _ns("LinkTag", _font(color=GREEN, name=MONO), align=left),
        _ns("Result", _font(bold=True), border=Border(top=HAIR), align=right),
        _ns("ResultTag", _font(bold=True, name=MONO), border=Border(top=HAIR), align=left),
        _ns("Total", _font(bold=True), border=Border(top=HAIR), align=right),
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


def put(ws, ref: str, value: Any, style: str, fmt: Optional[str] = None, align: Optional[str] = None):
    """Write a value or formula with a named style (and optional number format key or string)."""
    c = ws[ref]
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


def setup_sheet(ws, title: str, *, rev: str, date: str, project: str, print_rows: Optional[str] = None,
                last_col: int = 10, last_row: int = 50, tab_colour: Optional[str] = None,
                freeze: Optional[str] = None, zoom: int = 90) -> None:
    """Gridlines off, A3 landscape, fit to width, repeated header rows, footer, print area."""
    ws.sheet_view.showGridLines = False
    ws.sheet_view.zoomScale = zoom
    ws.page_setup.paperSize = 8
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_options.horizontalCentered = False
    ws.page_margins.left = ws.page_margins.right = 0.5
    ws.page_margins.top = 0.75
    ws.page_margins.bottom = 0.75
    if print_rows:
        ws.print_title_rows = print_rows
    ws.print_area = f"A1:{get_column_letter(max(last_col, 1))}{max(last_row, 1)}"
    ws.oddFooter.left.text = f"{project}"
    ws.oddFooter.left.size = 8
    ws.oddFooter.center.text = "&A"
    ws.oddFooter.center.size = 8
    ws.oddFooter.right.text = f"Rev {rev}  {date}  Page &P of &N"
    ws.oddFooter.right.size = 8
    if tab_colour:
        ws.sheet_properties.tabColor = tab_colour
    if freeze:
        ws.freeze_panes = freeze


def col_widths(ws, widths: Dict[int, float]) -> None:
    for idx, w in widths.items():
        ws.column_dimensions[get_column_letter(idx)].width = w
