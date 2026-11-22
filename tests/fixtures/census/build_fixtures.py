"""Build the small synthetic census fixtures (PDF via pymupdf, PPTX via python-pptx).

Run ``python tests/fixtures/census/build_fixtures.py [out_dir]`` or call
:func:`build_all`; the tests regenerate the files when they are missing.
Nothing here derives from a real project document.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path
from typing import Dict

HERE = Path(__file__).resolve().parent

PDF_NAME = "census_fixture.pdf"
PPTX_NAME = "census_fixture.pptx"

TEXT_PARAGRAPH = ("The pharmacy air handling unit serves the dispensary and the clean room suite. "
                  "Supply air is 1,200 litres per second at 14 degrees Celsius with a 10 per cent allowance "
                  "for duct leakage. ") * 4

COMMENTS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<p:cmLst xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
    '<p:cm authorId="0" dt="2026-11-22T10:00:00" idx="1"><p:pos x="10" y="10"/><p:text>First</p:text></p:cm>'
    '<p:cm authorId="0" dt="2026-11-22T10:01:00" idx="2"><p:pos x="20" y="20"/><p:text>Second</p:text></p:cm>'
    '</p:cmLst>'
)
MODERN_COMMENTS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<p188:cmLst xmlns:p188="http://schemas.microsoft.com/office/powerpoint/2018/8/main">'
    '<p188:cm id="{1}" authorId="{A}" created="2026-11-22T10:02:00"><p188:txBody/></p188:cm>'
    '</p188:cmLst>'
)


def build_pdf(path: Path) -> None:
    """Four A4 pages: text, 1000 tiny vector rectangles, one full-bleed image, 3 annotations + an OCG."""
    import pymupdf

    doc = pymupdf.open()
    # page 1: text
    page = doc.new_page(width=595, height=842)
    page.insert_textbox(pymupdf.Rect(50, 50, 545, 800), TEXT_PARAGRAPH, fontsize=10)
    # page 2: 1000 tiny rectangles, each its own path
    page = doc.new_page(width=595, height=842)
    shape = page.new_shape()
    for i in range(1000):
        x, y = 10 + (i % 40) * 14, 10 + (i // 40) * 14
        shape.draw_rect(pymupdf.Rect(x, y, x + 10, y + 10))
        shape.finish(color=(0, 0, 0), width=0.5)
    shape.commit()
    # page 3: one full-bleed image, no text
    page = doc.new_page(width=595, height=842)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 80), 0)
    pix.set_rect(pix.irect, (180, 180, 180))
    page.insert_image(page.rect, pixmap=pix, keep_proportion=False)
    # layer, then page 4: three annotations of different types
    doc.add_ocg("Markups")
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 50), "Annotated page", fontsize=12)
    page.add_text_annot((30, 30), "A sticky note")
    page.add_rect_annot(pymupdf.Rect(100, 100, 200, 200))
    page.add_freetext_annot(pymupdf.Rect(300, 300, 450, 340), "Free text")
    doc.save(str(path), deflate=True)
    doc.close()


def _add_comments(pptx_path: Path) -> None:
    """Inject a classic comments part (2 comments) on slide 1 and a modern one (1 comment) on slide 2."""
    src = zipfile.ZipFile(pptx_path)
    items = {name: src.read(name) for name in src.namelist()}
    src.close()

    def add_rel(rels_name: str, target: str, rel_type: str, rel_id: str) -> None:
        xml = items[rels_name].decode("utf-8")
        rel = f'<Relationship Id="{rel_id}" Type="{rel_type}" Target="{target}"/>'
        items[rels_name] = xml.replace("</Relationships>", rel + "</Relationships>").encode("utf-8")

    items["ppt/comments/comment1.xml"] = COMMENTS_XML.encode("utf-8")
    items["ppt/comments/modernComment_100_ABC.xml"] = MODERN_COMMENTS_XML.encode("utf-8")
    add_rel("ppt/slides/_rels/slide1.xml.rels", "../comments/comment1.xml",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments", "rIdCmt1")
    add_rel("ppt/slides/_rels/slide2.xml.rels", "../comments/modernComment_100_ABC.xml",
            "http://schemas.microsoft.com/office/2018/10/relationships/comments", "rIdCmt2")
    ct = items["[Content_Types].xml"].decode("utf-8")
    overrides = ('<Override PartName="/ppt/comments/comment1.xml" '
                 'ContentType="application/vnd.openxmlformats-officedocument.presentationml.comments+xml"/>'
                 '<Override PartName="/ppt/comments/modernComment_100_ABC.xml" '
                 'ContentType="application/vnd.ms-powerpoint.comments+xml"/>')
    items["[Content_Types].xml"] = ct.replace("</Types>", overrides + "</Types>").encode("utf-8")

    with zipfile.ZipFile(pptx_path, "w", zipfile.ZIP_DEFLATED) as out:
        for name, data in items.items():
            out.writestr(name, data)


def build_pptx(path: Path) -> None:
    """Three 4:3 slides: text (with notes), one near-full-bleed picture, a table; comments on slides 1 and 2."""
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    blank = prs.slide_layouts[6]
    # slide 1: text
    slide = prs.slides.add_slide(blank)
    box = slide.shapes.add_textbox(Inches(0.5), Inches(0.5), Inches(9), Inches(6))
    box.text_frame.text = TEXT_PARAGRAPH
    slide.notes_slide.notes_text_frame.text = "Speaker notes for the text slide."
    # slide 2: one picture covering the slide
    slide = prs.slides.add_slide(blank)
    png = path.parent / "census_fixture_picture.png"
    _write_png(png)
    slide.shapes.add_picture(str(png), 0, 0, width=prs.slide_width, height=prs.slide_height)
    # slide 3: a table and a small caption
    slide = prs.slides.add_slide(blank)
    table = slide.shapes.add_table(3, 3, Inches(1), Inches(1), Inches(8), Inches(3)).table
    for r in range(3):
        for c in range(3):
            table.cell(r, c).text = f"R{r}C{c}"
    slide.shapes.add_textbox(Inches(1), Inches(5), Inches(8), Inches(1)).text_frame.text = "Table caption"
    prs.save(str(path))
    _add_comments(path)


def _write_png(path: Path) -> None:
    """A tiny flat-grey PNG."""
    from PIL import Image

    Image.new("RGB", (64, 48), (150, 150, 150)).save(path)


def build_all(out_dir: Path = HERE) -> Dict[str, Path]:
    """Build every fixture into ``out_dir`` and return ``{"pdf": ..., "pptx": ...}``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf, pptx = out_dir / PDF_NAME, out_dir / PPTX_NAME
    build_pdf(pdf)
    build_pptx(pptx)
    return {"pdf": pdf, "pptx": pptx}


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE
    for kind, built in build_all(target).items():
        print(f"{kind}: {built}")
