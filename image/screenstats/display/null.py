"""Null display: discards frames and counts pushes. For tests and dry runs."""

from __future__ import annotations

import time

from PIL import Image


class NullDisplay:
    """Accepts any frame, keeps only counters."""

    name = "null"

    def __init__(self) -> None:
        self.pushes = 0
        self.sleeps = 0
        self.closed = False
        self.last_push = 0.0
        self.last_size: tuple[int, int] | None = None

    def push(self, img: Image.Image) -> None:
        # Every push is a full refresh now, so there is no separate counter.
        self.pushes += 1
        self.last_size = img.size
        self.last_push = time.time()

    def sleep(self) -> None:
        self.sleeps += 1

    def close(self) -> None:
        self.closed = True
