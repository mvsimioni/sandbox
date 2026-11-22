"""Synthetic edge-case fixtures for tests/test_census_adversarial.py (built into a temp dir at test time).

Nothing here derives from a real project document.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict


def build_rotated_scan_pdf(path: Path) -> None:
    """One portrait A4 page with a full-bleed image and ``/Rotate 90`` (a typical landscape scan)."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 80), 0)
    pix.set_rect(pix.irect, (120, 120, 120))
    page.insert_image(page.rect, pixmap=pix, keep_proportion=False)
    page.set_rotation(90)
    doc.save(str(path))
    doc.close()


def build_tiny_page_pdf(path: Path) -> None:
    """A 3 x 3 pt page (near-zero area) holding one degenerate zero-width image placement and some text."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=3, height=3)
    page.insert_text((0, 2), "x", fontsize=2)
    doc.save(str(path))
    doc.close()


def build_two_page_text_pdf(path: Path, marker: str) -> None:
    """Two text pages whose content includes ``marker`` so hashes differ between variants."""
    import pymupdf

    doc = pymupdf.open()
    body = (f"Document {marker}. " + "Supply air 1,200 litres per second at 14 degrees Celsius. ") * 6
    for _ in range(2):
        page = doc.new_page(width=595, height=842)
        page.insert_textbox(pymupdf.Rect(40, 40, 555, 800), body, fontsize=10)
    doc.save(str(path))
    doc.close()


def build_plain_pptx(path: Path) -> None:
    """Three slides with no comments part at all: an empty slide, a connector + chart slide, empty placeholders."""
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    prs = Presentation()
    prs.slides.add_slide(prs.slide_layouts[6])
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_connector(1, Inches(1), Inches(1), Inches(3), Inches(3))
    data = CategoryChartData()
    data.categories = ["a", "b"]
    data.add_series("s", (1, 2))
    slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(1), Inches(4), Inches(3), data)
    prs.slides.add_slide(prs.slide_layouts[1])  # title + content placeholders, left empty
    prs.save(str(path))


def build_all(out_dir: Path) -> Dict[str, Path]:
    """Build every fixture into ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    built = {
        "rotated": out_dir / "rotated_scan.pdf",
        "tiny": out_dir / "tiny_page.pdf",
        "text_a": out_dir / "text_a.pdf",
        "text_b": out_dir / "text_b.pdf",
        "plain_pptx": out_dir / "plain.pptx",
    }
    build_rotated_scan_pdf(built["rotated"])
    build_tiny_page_pdf(built["tiny"])
    build_two_page_text_pdf(built["text_a"], "A")
    build_two_page_text_pdf(built["text_b"], "B")
    build_plain_pptx(built["plain_pptx"])
    return built
