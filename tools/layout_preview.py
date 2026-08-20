#!/usr/bin/env python3
"""Render the exact 250x122 panel frame to a PNG, with no hardware attached.

This is the layout iteration loop. Never iterate on the panel itself: Waveshare
mandates a >=180 s refresh interval, so on-panel iteration is both glacial and
harmful.

It renders through the SAME `screenstats.layout` the renderer uses, rather than
keeping its own copy of the drawing code. An earlier version duplicated the
layout here and the two drifted; importing the real thing makes that impossible.

Usage:
    python3 tools/layout_preview.py out.png [--case nominal] [--scale 4]
    python3 tools/layout_preview.py --all /tmp/frames.png     # every case stacked
    python3 tools/layout_preview.py --icons /tmp/icons.png    # weather glyph sheet
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "image"))

from PIL import Image, ImageDraw  # noqa: E402

from screenstats import icons, layout  # noqa: E402

#: Named scenarios. `overflow` is the adversarial one: longest greeting, longest
#: city and WMO label, negative 4-character temperatures, saturated gauges and the
#: stale-weather marker all at once.
CASES: dict[str, layout.Snapshot] = {
    "nominal": layout.Snapshot(),
    "overflow": layout.Snapshot(
        clock="08:05", date="Wed 31 Dec", greeting="Good Morning",
        cpu=1.0, ram=1.0, disk=0.995,
        city="Charlottesville", temp_c=-12.4, condition="T-storm hail",
        lo_c=-18.0, hi_c=-7.0, code=99, is_day=True, stale="14:20",
    ),
    "evening": layout.Snapshot(
        clock="21:40", date="Mon 01 Sep", greeting="Good Evening",
        cpu=0.07, ram=0.55, disk=0.82,
        city="Reykjavik", temp_c=3.0, condition="Rime fog",
        lo_c=1.0, hi_c=6.0, code=48, is_day=False,
    ),
    "clear-night": layout.Snapshot(
        clock="23:15", greeting="Good Evening", city="Melbourne",
        temp_c=9.0, condition="Clear", lo_c=6.0, hi_c=18.0,
        code=0, is_day=False,
    ),
    "rain": layout.Snapshot(
        temp_c=7.0, condition="Rain", lo_c=4.0, hi_c=9.0, code=63,
    ),
    "snow": layout.Snapshot(
        clock="07:30", greeting="Good Morning", city="Tromso",
        temp_c=-3.0, condition="Heavy snow", lo_c=-8.0, hi_c=-1.0, code=75,
    ),
    "no-weather": layout.Snapshot(
        temp_c=0.0, condition="no data", lo_c=0.0, hi_c=0.0, code=None,
    ),
}


def check(name: str, snap: layout.Snapshot) -> Image.Image:
    """Render one case and assert the two invariants that matter."""
    img = layout.render(snap)
    assert img.size == (layout.W, layout.H), img.size

    # Mirror getbuffer(): it rotates the (250,122) landscape frame to the panel's
    # native (122,250) before tobytes(). Only then is the stride 16 B and the frame
    # 4000 B; the landscape image itself packs to 32 B x 122 = 3904 B.
    native = img.rotate(90, expand=True)
    raw = native.tobytes("raw")
    assert native.size == (122, 250), native.size
    assert len(raw) == 4000, f"{name}: frame must be 4000 B, got {len(raw)}"

    problems = layout.clipped_ink(snap)
    assert not problems, f"{name}: {problems}"
    return img


def icon_sheet() -> Image.Image:
    kinds = ["clear", "clear_night", "partly", "partly_night", "cloud", "fog",
             "drizzle", "rain", "showers", "snow", "sleet", "storm"]
    s, cols, pad = layout.ICON, 6, 16
    cw, ch = s + pad, s + pad + 12
    img = Image.new("1", (cols * cw, ((len(kinds) + cols - 1) // cols) * ch), 1)
    d = ImageDraw.Draw(img)
    f = layout.font("DejaVuSans.ttf", 9)
    for i, k in enumerate(kinds):
        cx, cy = (i % cols) * cw, (i // cols) * ch
        icons.draw(d, k, (cx + pad // 2, cy + pad // 2, s, s))
        d.text((cx + 1, cy + s + pad // 2), k[:13], font=f, fill=layout.BLACK)
    return img


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default="preview.png")
    ap.add_argument("--scale", type=int, default=4)
    ap.add_argument("--case", default="nominal", choices=sorted(CASES))
    ap.add_argument("--all", action="store_true", help="stack every case")
    ap.add_argument("--icons", action="store_true", help="render the glyph sheet")
    args = ap.parse_args()

    if args.icons:
        img = icon_sheet()
    elif args.all:
        frames = [check(n, s) for n, s in sorted(CASES.items())]
        img = Image.new("1", (layout.W, (layout.H + 4) * len(frames)), 1)
        for i, f in enumerate(frames):
            img.paste(f, (0, i * (layout.H + 4)))
        print(f"all {len(frames)} cases: 4000 B, no clipping")
    else:
        img = check(args.case, CASES[args.case])
        print(f"{args.case}: 4000 B, no clipping")

    img.resize((img.width * args.scale, img.height * args.scale), Image.NEAREST).save(args.out)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
