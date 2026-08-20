"""Weather glyphs drawn for a 1-bit e-paper panel.

Drawn with primitives rather than shipped as bitmaps: no assets to license or keep
in sync, and the shapes scale to whatever box the layout can spare.

Design rules for 1 bpp at ~26 px, learned the hard way on this panel:

* **Filled silhouettes, not outlines.** There is no anti-aliasing and no grey. A
  2 px outline of a cloud reads as noise at this size; a solid cloud reads
  instantly.
* **Nothing thinner than 2 px.** Single-pixel strokes disappear against the
  panel's own texture at normal viewing distance.
* **Detail costs nothing but legibility.** Three rain streaks read as rain; six
  read as a smudge.

Codes are Open-Meteo's WMO subset (exactly 28 values, see weather.WMO). Anything
unknown falls back to the overcast cloud rather than drawing nothing, so the slot
is never mysteriously empty.
"""

from __future__ import annotations

from PIL import ImageDraw

BLACK = 0

# WMO code -> glyph kind. Kinds that vary by daylight are resolved in kind_for().
_KINDS: dict[int, str] = {
    0: "clear",
    1: "partly", 2: "partly",
    3: "cloud",
    45: "fog", 48: "fog",
    51: "drizzle", 53: "drizzle", 55: "drizzle",
    56: "sleet", 57: "sleet",
    61: "rain", 63: "rain", 65: "rain",
    66: "sleet", 67: "sleet",
    71: "snow", 73: "snow", 75: "snow", 77: "snow",
    80: "showers", 81: "showers", 82: "showers",
    85: "snow", 86: "snow",
    95: "storm", 96: "storm", 99: "storm",
}


def kind_for(code: int | None, is_day: bool = True) -> str:
    """Glyph name for a WMO code, split by daylight where it matters."""
    kind = _KINDS.get(int(code)) if code is not None else None
    if kind is None:
        return "cloud"
    if kind == "clear":
        return "clear" if is_day else "clear_night"
    if kind == "partly":
        return "partly" if is_day else "partly_night"
    return kind


def _disc(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float) -> None:
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=BLACK)


def _cloud(d: ImageDraw.ImageDraw, x: float, y: float, w: float, h: float) -> None:
    """Solid cloud silhouette filling (x, y, w, h).

    The bumps must differ in size AND height or the shape reads as a hill rather
    than a cloud -- three similar discs on a tall base is exactly what went wrong
    in the first attempt. Tallest bump right of centre, shoulder to its left, small
    bump far right, all protruding above a shallow base.
    """
    d.rectangle((x + w * 0.04, y + h * 0.62, x + w * 0.96, y + h * 0.99), fill=BLACK)
    _disc(d, x + w * 0.52, y + h * 0.42, h * 0.40)   # main bump, tallest
    _disc(d, x + w * 0.26, y + h * 0.60, h * 0.30)   # shoulder
    _disc(d, x + w * 0.80, y + h * 0.62, h * 0.27)   # small right bump


def _rays(d: ImageDraw.ImageDraw, cx: float, cy: float, r_in: float, r_out: float) -> None:
    """Eight sun rays. Axis-aligned plus diagonals only: arbitrary angles alias."""
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1),
                   (0.7, 0.7), (-0.7, 0.7), (0.7, -0.7), (-0.7, -0.7)):
        d.line((cx + dx * r_in, cy + dy * r_in, cx + dx * r_out, cy + dy * r_out),
               fill=BLACK, width=2)


def _crescent(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float) -> None:
    """Moon: a disc with an offset disc knocked out. Bold enough to survive 1 bpp."""
    _disc(d, cx, cy, r)
    d.ellipse((cx - r * 0.35, cy - r * 1.15, cx + r * 1.55, cy + r * 0.85), fill=1)


def _streaks(d: ImageDraw.ImageDraw, x: float, y: float, w: float, h: float,
             n: int = 3, slant: float = 0.0) -> None:
    for i in range(n):
        sx = x + w * (i + 0.5) / n
        d.line((sx + slant, y, sx - slant, y + h), fill=BLACK, width=2)


def _dots(d: ImageDraw.ImageDraw, x: float, y: float, w: float, n: int = 3) -> None:
    for i in range(n):
        cx = x + w * (i + 0.5) / n
        d.rectangle((cx - 1, y, cx + 1, y + 2), fill=BLACK)


def _flakes(d: ImageDraw.ImageDraw, x: float, y: float, w: float, n: int = 3) -> None:
    """Plus-shaped flakes. A six-armed asterisk turns to mush below ~9 px."""
    for i in range(n):
        cx = x + w * (i + 0.5) / n
        cy = y + 2
        d.line((cx - 2.5, cy, cx + 2.5, cy), fill=BLACK, width=2)
        d.line((cx, cy - 2.5, cx, cy + 2.5), fill=BLACK, width=2)


def _bolt(d: ImageDraw.ImageDraw, x: float, y: float, w: float, h: float) -> None:
    d.polygon(
        [(x + w * 0.62, y), (x + w * 0.20, y + h * 0.58),
         (x + w * 0.46, y + h * 0.58), (x + w * 0.30, y + h),
         (x + w * 0.82, y + h * 0.40), (x + w * 0.52, y + h * 0.40)],
        fill=BLACK,
    )


def draw(d: ImageDraw.ImageDraw, kind: str, box: tuple[int, int, int, int]) -> None:
    """Draw `kind` inside `box` = (x, y, w, h)."""
    x, y, w, h = box

    # All geometry below is expressed as a fraction of `u`, never in absolute
    # pixels. A fixed ray length looked fine at 22 px and escaped the box at 18 px;
    # keeping everything proportional makes containment scale-independent.
    u = float(min(w, h))

    if kind == "clear":
        r = u * 0.26
        ray_out = u * 0.46            # < 0.50, so rays stay inside the box
        _disc(d, x + w / 2, y + h / 2, r)
        _rays(d, x + w / 2, y + h / 2, r + u * 0.06, ray_out)
        return

    if kind == "clear_night":
        _crescent(d, x + w * 0.5, y + h * 0.5, u * 0.38)
        return

    if kind in ("partly", "partly_night"):
        # Body up-LEFT, cloud pushed down-RIGHT so the body is never buried.
        #
        # Centre at 0.37u with rays reaching 0.33u keeps the topmost and leftmost
        # rays inside the box by 0.04u at every size. This matters because the icon
        # sits between two horizontal rules in the layout, and the off-panel ink
        # detector does not catch ink colliding with another element -- only ink
        # leaving the panel. So containment has to hold by construction.
        r = u * 0.19
        cx, cy = x + u * 0.37, y + u * 0.37
        if kind == "partly":
            ray_out = u * 0.33
            assert cy - ray_out >= y and cx - ray_out >= x, "sun rays escape the box"
            _disc(d, cx, cy, r)
            _rays(d, cx, cy, r + u * 0.05, ray_out)
        else:
            # A crescent this small collapses into a hook, so night uses a plain
            # disc. Rays vs no rays is what distinguishes day from night here.
            _disc(d, cx, cy, r * 1.05)
        _cloud(d, x + w * 0.22, y + h * 0.44, w * 0.78, h * 0.52)
        return

    if kind == "cloud":
        _cloud(d, x, y + h * 0.18, w, h * 0.64)
        return

    if kind == "fog":
        _cloud(d, x, y + h * 0.06, w, h * 0.52)
        for i in range(3):
            # A width-2 line centred on `ly` covers ly-1..ly+1, so the lowest line
            # must sit at or above y+h-2. 0.60 + 2*0.13 = 0.86 satisfies that at
            # every size; 0.66 + 2*0.14 = 0.94 spilled one row at 16 px.
            ly = y + h * (0.60 + i * 0.13)
            inset = w * (0.06 if i % 2 == 0 else 0.20)
            d.line((x + inset, ly, x + w - inset, ly), fill=BLACK, width=2)
        return

    # Everything below is a cloud with something falling out of it.
    _cloud(d, x, y, w, h * 0.60)
    # 0.63 + 0.33 = 0.96, so the lowest streak ends inside the box. Using
    # 0.66 + 0.34 summed to exactly 1.0 and spilled one row past the bottom.
    below_y = y + h * 0.63
    below_h = h * 0.33

    if kind == "drizzle":
        _dots(d, x + w * 0.15, below_y, w * 0.70)
    elif kind == "rain":
        _streaks(d, x + w * 0.15, below_y, w * 0.70, below_h)
    elif kind == "showers":
        _streaks(d, x + w * 0.15, below_y, w * 0.70, below_h, slant=w * 0.07)
    elif kind == "snow":
        _flakes(d, x + w * 0.12, below_y, w * 0.76)
    elif kind == "sleet":
        # Freezing: one streak plus one flake, to read as "both".
        _streaks(d, x + w * 0.14, below_y, w * 0.34, below_h, n=1)
        _flakes(d, x + w * 0.52, below_y, w * 0.36, n=1)
    elif kind == "storm":
        _bolt(d, x + w * 0.28, below_y - h * 0.04, w * 0.44, below_h + h * 0.06)
    else:
        _dots(d, x + w * 0.15, below_y, w * 0.70)
