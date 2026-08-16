"""PNG display: writes the frame to disk instead of a panel.

This is the driver that makes the full app runnable on a machine with no HAT
(PLAN.md 3, "Display abstraction"), and it is also what feeds the web UI's
preview image on real hardware, so it never touches SPI or GPIO.
"""

from __future__ import annotations

import io
import time
from pathlib import Path

from PIL import Image

from ..state import ensure_dirs, publish_bytes

#: The panel is 250x122; a 1:1 PNG is unreadable in a browser, so also publish a
#: nearest-neighbour upscale. Nearest, not bilinear: interpolating a 1-bit frame
#: invents grey that the panel cannot show.
SCALE = 4


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


class PngDisplay:
    """Publishes `state/preview.png` and `state/preview-4x.png`."""

    name = "png"

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        ensure_dirs(self.data_dir)
        self.path = self.data_dir / "state" / "preview.png"
        self.path_4x = self.data_dir / "state" / "preview-4x.png"
        self.pushes = 0
        self.last_push = 0.0

    def push(self, img: Image.Image, full: bool) -> None:
        """Publish the frame. `full` is irrelevant to a file: no waveforms here."""
        del full
        publish_bytes(self.path, _png_bytes(img))
        big = img.resize(
            (img.width * SCALE, img.height * SCALE), resample=Image.Resampling.NEAREST
        )
        publish_bytes(self.path_4x, _png_bytes(big))
        self.pushes += 1
        self.last_push = time.time()

    def sleep(self) -> None:
        """No panel to power down."""

    def close(self) -> None:
        """No host resources held."""
