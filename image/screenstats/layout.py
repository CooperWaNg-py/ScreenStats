"""Frame composition for the ScreenStats 2.13" V4 e-ink panel.

This module is the production form of ``tools/layout_preview.py``: identical
geometry, identical helpers, identical clipping detector. The harness stays the
iteration loop (never iterate layout on the panel -- Waveshare mandates a
>=180 s refresh interval, so on-panel iteration is both slow and damaging).

Geometry facts this file depends on (verified against upstream sources):
  * waveshare_epd/epd2in13_V4.py: EPD_WIDTH=122, EPD_HEIGHT=250
  * getbuffer() accepts a (250,122) landscape image and rotates it 90deg itself
  * mode '1': bit 1 = white, bit 0 = black; row stride 16 B => 4000 B frame

Two real defects the harness caught, and the fixes that stay here:

1. The 4000-byte frame only exists *after* the driver's 90-degree rotation.
   The landscape (250,122) image packs to 32 B x 122 = 3904 B; getbuffer()
   rotates to native (122,250) where the stride is 16 B -> 4000 B. Asserting
   buffer length on the landscape image fails. Assert after the rotation.
2. "100%" clipped off the right edge. A fixed x for the percentage column
   works for "42%" and silently truncates "100%". Percentages are therefore
   right-aligned against a hard x=246 margin, with the gauge width derived
   from the *measured* text width.

render() deliberately knows nothing about 180-degree rotation: user-facing
panel orientation belongs to the display layer, not the layout.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

from . import icons

# Landscape frame. Portrait native is (122, 250); getbuffer() rotates for us.
W, H = 250, 122

BLACK, WHITE = 0, 1

#: Weather glyph edge, px. Zone C row 1 runs y66..89 between two rules, so 22 at
#: y=67 keeps the precipitation streaks clear of the rule at y=90.
ICON = 22

# Debian: fonts-dejavu-core (installed in the app image). Local dev on macOS
# falls back to matplotlib's bundled copy, which is what the harness uses.
FONT_DIRS = [
    "/usr/share/fonts/truetype/dejavu",
    os.path.expanduser(
        "~/Library/Python/3.14/lib/python/site-packages/matplotlib/mpl-data/fonts/ttf"
    ),
]


class LayoutError(RuntimeError):
    """No usable font, so no frame can be composed."""


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    for d in FONT_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    raise LayoutError(
        f"font {name} not found in {FONT_DIRS}; install fonts-dejavu-core"
    )


@dataclass(frozen=True)
class Snapshot:
    """Everything the frame needs. Mirrors renderer state.json."""

    clock: str = "14:35"
    date: str = "Sat 16 Aug"
    greeting: str = "Good Afternoon"
    cpu: float = 0.42
    ram: float = 0.71
    disk: float = 0.31
    city: str = "Melbourne"
    temp_c: float = 10.9
    condition: str = "Part cloudy"
    lo_c: float = 4.7
    hi_c: float = 11.3
    code: int | None = 2          # WMO code -> weather glyph
    is_day: bool = True           # picks sun vs moon
    stale: str | None = None  # e.g. "14:20" when weather cache is old


def text_w(d: ImageDraw.ImageDraw, s: str, f: ImageFont.FreeTypeFont) -> int:
    return int(d.textlength(s, font=f))


def fit(d: ImageDraw.ImageDraw, s: str, f: ImageFont.FreeTypeFont, max_w: int) -> str:
    """Ellipsise to fit. Never let a long city name or condition overflow."""
    if text_w(d, s, f) <= max_w:
        return s
    while s and text_w(d, s + "\u2026", f) > max_w:
        s = s[:-1]
    return s + "\u2026" if s else ""


def bar(d: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, frac: float) -> None:
    """Segmented gauge. Segments read better than a solid bar at 1 bpp."""
    frac = min(1.0, max(0.0, frac))
    d.rectangle((x, y, x + w - 1, y + h - 1), outline=BLACK, fill=WHITE)
    inner_w = w - 4
    filled = int(round(inner_w * frac))
    if filled > 0:
        d.rectangle((x + 2, y + 2, x + 1 + filled, y + h - 3), fill=BLACK)


def _draw_frame(d: ImageDraw.ImageDraw, s: Snapshot, ox: int = 0, oy: int = 0) -> None:
    """Draw the frame with every coordinate offset by (ox, oy).

    The offset exists so clipped_ink() can render the identical geometry into a
    padded canvas and detect ink that would fall off a real 250x122 panel.
    """
    f_clock = font("DejaVuSansMono-Bold.ttf", 44)
    f_greet = font("DejaVuSans-Bold.ttf", 15)
    f_date = font("DejaVuSans.ttf", 12)
    f_stat = font("DejaVuSansMono-Bold.ttf", 11)
    f_temp = font("DejaVuSans-Bold.ttf", 22)
    f_body = font("DejaVuSans.ttf", 12)
    f_small = font("DejaVuSans.ttf", 10)

    def txt(x: int, y: int, s_: str, f: ImageFont.FreeTypeFont) -> None:
        d.text((ox + x, oy + y), s_, font=f, fill=BLACK)

    def rule(x0: int, y0: int, x1: int, y1: int) -> None:
        d.line((ox + x0, oy + y0, ox + x1, oy + y1), fill=BLACK)

    # ---- Zone A: clock block, x 0..147 -----------------------------------
    # Mono font => digit block width is constant, which keeps the partial-refresh
    # dirty rectangle stable across ticks.
    txt(3, 2, s.clock, f_clock)
    txt(6, 50, s.date, f_date)

    # ---- Zone B: host stats, x 152..246 ----------------------------------
    # Percentages are right-aligned against a hard margin: "100%" is wider than
    # "42%", and a fixed x for both clips the '%' off the panel edge.
    STAT_X, STAT_R = 152, 246
    for i, (label, frac) in enumerate(
        (("CPU", s.cpu), ("RAM", s.ram), ("DSK", s.disk))
    ):
        y = 3 + i * 21
        txt(STAT_X, y, label, f_stat)
        pct = f"{round(min(1.0, max(0.0, frac)) * 100)}%"
        pw = text_w(d, pct, f_stat)
        txt(STAT_R - pw, y, pct, f_stat)
        bar_x = STAT_X + 24
        bar(d, ox + bar_x, oy + y + 2, (STAT_R - pw - 4) - bar_x, 9, frac)

    # ---- Rules -----------------------------------------------------------
    rule(0, 66, W - 1, 66)      # separates A/B from C
    rule(148, 0, 148, 65)       # separates clock from stats

    # ---- Zone C: greeting + weather, y 67..121 ---------------------------
    # Row 1 is greeting | icon | temperature. The "feels like" reading used to sit
    # between them, but measurement showed row 1 has only 8 px spare in the worst
    # case (greeting 129 + feels 47 + temp 50 + margins), so there was no room for
    # a legible icon alongside it. "feels" is the least informative of the four
    # weather values -- temp, condition text and today's lo/hi all remain -- and it
    # is still reported in status.json, the widget and the web UI.
    temp = f"{s.temp_c:.0f}\u00b0"
    tw = text_w(d, temp, f_temp)
    txt(W - 4 - tw, 66, temp, f_temp)

    icon_x = W - 4 - tw - 6 - ICON
    icons.draw(d, icons.kind_for(s.code, s.is_day), (ox + icon_x, oy + 67, ICON, ICON))

    txt(4, 69, fit(d, s.greeting, f_greet, icon_x - 8), f_greet)

    rule(0, 90, W - 1, 90)

    hi_lo = f"{s.lo_c:.0f}\u00b0/{s.hi_c:.0f}\u00b0"
    hw = text_w(d, hi_lo, f_body)
    txt(W - 4 - hw, 93, hi_lo, f_body)

    left = f"{s.city} \u00b7 {s.condition}"
    txt(4, 93, fit(d, left, f_body, W - 16 - hw), f_body)

    if s.stale:
        txt(4, 108, f"weather {s.stale}", f_small)


def render(s: Snapshot) -> Image.Image:
    """Compose the landscape frame. Mode '1', size (250, 122).

    No rotation happens here. The driver's getbuffer() applies the mandatory
    90-degree rotation to native portrait; the optional user-facing 180-degree
    flip is applied by display/epd.py.
    """
    img = Image.new("1", (W, H), WHITE)
    _draw_frame(ImageDraw.Draw(img), s)
    return img


PAD = 24


def clipped_ink(s: Snapshot) -> list[str]:
    """Detect ink that would fall off a real 250x122 panel.

    Pillow silently clips draws at the canvas edge, so an over-wide string looks
    fine on the preview and only shows up as a cut-off glyph on the hardware.
    Rendering the identical geometry offset into a padded canvas makes any
    out-of-bounds ink observable.
    """
    wide = Image.new("1", (W + 2 * PAD, H + 2 * PAD), WHITE)
    _draw_frame(ImageDraw.Draw(wide), s, PAD, PAD)

    # Zero out the legitimate panel area; anything black left over is overflow.
    wide.paste(WHITE, (PAD, PAD, PAD + W, PAD + H))
    bbox = wide.point(lambda v: 1 - v, mode="1").getbbox()
    if bbox is None:
        return []
    x0, y0, x1, y1 = bbox
    return [
        f"ink outside panel: bbox x {x0 - PAD}..{x1 - 1 - PAD}, "
        f"y {y0 - PAD}..{y1 - 1 - PAD} (panel is 0..{W - 1} x 0..{H - 1})"
    ]
