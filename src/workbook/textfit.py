"""Text measurement for column widths and header heights (Arial and Consolas metrics, 96 dpi)."""

from __future__ import annotations

from functools import lru_cache
from typing import List

from PIL import ImageFont

_FONTS = {
    (False, False): "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    (True, False): "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    (False, True): "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    (True, True): "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
}
CONSOLAS_EM = 0.55 / 0.6       # Consolas is narrower than Liberation Mono (0.55 em against 0.6 em)


@lru_cache(maxsize=16)
def _font(bold: bool, mono: bool):
    try:
        return ImageFont.truetype(_FONTS[(bold, mono)], 100)
    except OSError:
        return None


def px(text: str, *, bold: bool = False, mono: bool = False, pt: float = 10.0) -> float:
    """Width of one line of text in pixels at 96 dpi."""
    f = _font(bold, mono)
    size = pt * 96.0 / 72.0
    if f is None:
        return len(text) * size * 0.58
    w = f.getlength(text) * size / 100.0
    return w * CONSOLAS_EM if mono else w


def width_units(pixels: float) -> float:
    """Excel column width (characters of the default font, 7 px each, 5 px padding) that holds ``pixels``."""
    return round((pixels + 5.0 + 4.0) / 7.0 + 0.3, 1)


def lines(text: str, col_width: float, *, bold: bool = False, mono: bool = False, pt: float = 10.0) -> int:
    """Number of lines a wrapped text takes in a column of the given width."""
    avail = 7.0 * col_width - 4.0
    total = 0
    for para in str(text).split("\n"):
        cur = ""
        n = 1
        for word in para.split(" "):
            trial = (cur + " " + word).strip()
            if cur and px(trial, bold=bold, mono=mono, pt=pt) > avail:
                n += 1
                cur = word
            else:
                cur = trial
        total += n
    return max(total, 1)


def longest_word_px(text: str, *, bold: bool = False, mono: bool = False, pt: float = 10.0) -> float:
    words: List[str] = str(text).replace("\n", " ").replace("/", "/ ").replace("-", "- ").split()
    return max((px(w, bold=bold, mono=mono, pt=pt) for w in words), default=0.0)
