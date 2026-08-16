#!/usr/bin/env python3
"""Build the App Store gallery images from a REAL rendered frame.

umbrelOS shows `gallery:` from the manifest verbatim, so these files must exist at
the raw.githubusercontent URLs or the store listing shows broken images.

Frame 1 is generated here from layout.render(), so it can never drift from what
the panel actually shows. Frames 2 and 3 are screenshots captured separately from
a live instance and simply converted/padded to a consistent canvas.

Usage:
    python3 tools/make_gallery.py                       # regenerate 1.jpg
    python3 tools/make_gallery.py --shot page.png 2     # convert a screenshot
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "image"))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from screenstats import layout  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "assets" / "gallery"
CANVAS = (1280, 800)
BG = (21, 26, 33)
FG = (230, 237, 243)
DIM = (139, 148, 158)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return layout.font(name, size)


def frame_shot() -> Image.Image:
    """The panel frame, upscaled, on a dark card. Generated from the real layout."""
    snap = layout.Snapshot()
    panel = layout.render(snap).convert("L").convert("RGB")

    scale = 4
    panel = panel.resize((layout.W * scale, layout.H * scale), Image.NEAREST)

    canvas = Image.new("RGB", CANVAS, BG)
    d = ImageDraw.Draw(canvas)

    d.text((64, 44), "ScreenStats", font=_font(44, bold=True), fill=FG)
    d.text(
        (64, 102),
        "Waveshare 2.13\u2033 e-Paper HAT V4 \u00b7 250\u00d7122 \u00b7 1-bit",
        font=_font(23),
        fill=DIM,
    )

    # White bezel around the panel so the 1-bit frame reads as a physical display.
    # Position is derived, not hardcoded: an earlier hardcoded py let the card
    # bottom (py + height + pad) run underneath the captions.
    bezel = 18
    px = (CANVAS[0] - panel.width) // 2
    py = 168
    card_bottom = py + panel.height + bezel
    d.rounded_rectangle(
        (px - bezel, py - bezel, px + panel.width + bezel, card_bottom),
        radius=12,
        fill=(244, 244, 239),
    )
    canvas.paste(panel, (px, py))

    caption_top = card_bottom + 26
    d.text(
        (64, caption_top),
        "Clock, host CPU / RAM / disk, time-of-day greeting and local weather.",
        font=_font(25),
        fill=FG,
    )
    d.text(
        (64, caption_top + 38),
        "Refreshes every 5 minutes and sleeps between refreshes, per Waveshare's 180 s minimum.",
        font=_font(21),
        fill=DIM,
    )
    assert caption_top + 38 + 26 < CANVAS[1], "captions overflow the canvas"
    return canvas


def fit_shot(src: Path) -> Image.Image:
    """Letterbox an arbitrary screenshot onto the shared canvas."""
    img = Image.open(src).convert("RGB")
    canvas = Image.new("RGB", CANVAS, BG)
    scale = min(CANVAS[0] / img.width, CANVAS[1] / img.height)
    img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
    canvas.paste(img, ((CANVAS[0] - img.width) // 2, (CANVAS[1] - img.height) // 2))
    return canvas


def save(img: Image.Image, index: int) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{index}.jpg"
    img.save(path, format="JPEG", quality=88, optimize=True)
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shot", type=Path, help="screenshot to convert instead of generating")
    ap.add_argument("index", nargs="?", type=int, default=1)
    args = ap.parse_args()

    img = fit_shot(args.shot) if args.shot else frame_shot()
    path = save(img, args.index)
    print(f"wrote {path.relative_to(path.parent.parent.parent)} {img.size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
