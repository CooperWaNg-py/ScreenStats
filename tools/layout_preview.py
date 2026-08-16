#!/usr/bin/env python3
"""Layout proof / preview harness for the ScreenStats 2.13" V4 e-ink frame.

Renders the exact 250x122 1-bit frame the renderer will push to the panel, with
no hardware present. This is the design tool: iterate here, never on the panel
(Waveshare mandates a >=180 s refresh interval, so on-panel iteration is both
slow and damaging).

Usage:
    python3 tools/layout_preview.py out.png [--scale 4]

Geometry facts this file depends on (verified against upstream sources):
  * waveshare_epd/epd2in13_V4.py: EPD_WIDTH=122, EPD_HEIGHT=250
  * getbuffer() accepts a (250,122) landscape image and rotates it 90deg itself
  * mode '1': bit 1 = white, bit 0 = black; row stride 16 B => 4000 B frame
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

# Landscape frame. Portrait native is (122, 250); getbuffer() rotates for us.
W, H = 250, 122

BLACK, WHITE = 0, 1

# Debian: fonts-dejavu-core. Local dev falls back to matplotlib's bundled copy.
FONT_DIRS = [
    "/usr/share/fonts/truetype/dejavu",
    os.path.expanduser(
        "~/Library/Python/3.14/lib/python/site-packages/matplotlib/mpl-data/fonts/ttf"
    ),
]


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    for d in FONT_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    raise SystemExit(f"font {name} not found in {FONT_DIRS}")


@dataclass
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
    feels_c: float = 9.9
    condition: str = "Part cloudy"
    lo_c: float = 4.7
    hi_c: float = 11.3
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
    temp = f"{s.temp_c:.0f}\u00b0"
    tw = text_w(d, temp, f_temp)
    txt(W - 4 - tw, 66, temp, f_temp)

    feels = f"feels {s.feels_c:.0f}\u00b0"
    fw = text_w(d, feels, f_small)
    feels_x = W - 8 - tw - fw
    txt(feels_x, 74, feels, f_small)

    txt(4, 69, fit(d, s.greeting, f_greet, feels_x - 8), f_greet)

    rule(0, 90, W - 1, 90)

    hi_lo = f"{s.lo_c:.0f}\u00b0/{s.hi_c:.0f}\u00b0"
    hw = text_w(d, hi_lo, f_body)
    txt(W - 4 - hw, 93, hi_lo, f_body)

    left = f"{s.city} \u00b7 {s.condition}"
    txt(4, 93, fit(d, left, f_body, W - 16 - hw), f_body)

    if s.stale:
        txt(4, 108, f"weather {s.stale}", f_small)


def render(s: Snapshot) -> Image.Image:
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default="preview.png")
    ap.add_argument("--scale", type=int, default=4)
    ap.add_argument("--case", default="nominal")
    args = ap.parse_args()

    cases = {
        "nominal": Snapshot(),
        # Worst case: long city, longest WMO label, 3-digit pcts, stale marker
        "overflow": Snapshot(
            clock="08:05",
            date="Wed 31 Dec",
            greeting="Good Morning",
            cpu=1.0,
            ram=1.0,
            disk=0.995,
            city="Charlottesville",
            temp_c=-12.4,
            feels_c=-19.6,
            condition="T-storm hail",
            lo_c=-18.0,
            hi_c=-7.0,
            stale="14:20",
        ),
        "evening": Snapshot(
            clock="21:40",
            date="Mon 01 Sep",
            greeting="Good Evening",
            cpu=0.07,
            ram=0.55,
            disk=0.82,
            city="Reykjavik",
            temp_c=3.0,
            feels_c=-1.0,
            condition="Rime fog",
            lo_c=1.0,
            hi_c=6.0,
        ),
    }
    snap = cases[args.case]
    img = render(snap)

    assert img.size == (W, H), img.size
    # Mirror getbuffer(): it rotates the (250,122) landscape image to the panel's
    # native (122,250) before tobytes(). Only then is the stride 16 B and the
    # frame 4000 B. The landscape image itself packs to 32 B x 122 = 3904 B.
    native = img.rotate(90, expand=True)
    assert native.size == (122, 250), native.size
    raw = native.tobytes("raw")
    assert len(raw) == 4000, f"frame must be 4000 B for epd2in13_V4, got {len(raw)}"

    problems = clipped_ink(snap)
    if problems:
        for p in problems:
            print(f"{args.case}: FAIL {p}", file=sys.stderr)
        return 1

    img.resize((W * args.scale, H * args.scale), Image.NEAREST).save(args.out)
    print(f"{args.case}: {args.out} ok  frame={len(raw)} B  no clipping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
