"""Display abstraction: a 3-method protocol plus host capability probing.

The seam exists so the whole app is developable and testable on a machine with
no e-Paper HAT attached (`png`/`null` drivers), while the `epd` driver owns the
real panel and its Waveshare refresh policy.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from PIL import Image

SPI_PATH = "/dev/spidev0.0"

#: Actionable remedy for the expected first-run state on umbrelOS (PLAN.md 0.2).
SPI_REMEDY = (
    "SPI is not enabled on the host: add 'dtparam=spi=on' to "
    "/boot/firmware/config.txt on the Raspberry Pi's boot partition and reboot, "
    f"then confirm {SPI_PATH} exists"
)


class DisplayError(RuntimeError):
    """The requested display cannot be used. Message must be actionable."""


@runtime_checkable
class Display(Protocol):
    """Everything the paint loop is allowed to know about a panel."""

    name: str

    def push(self, img: Image.Image, full: bool) -> None:
        """Send one 250x122 mode-'1' frame. `full` selects a full refresh."""
        ...

    def sleep(self) -> None:
        """Put the panel into deep sleep. Mandatory after every refresh."""
        ...

    def close(self) -> None:
        """Release host resources (SPI handle, GPIO pins)."""
        ...


def probe_host() -> dict:
    """Report the host facts that decide whether the `epd` driver can work.

    Never raises: this is diagnostic plumbing that feeds status.json, and the
    web UI must still render when the probe itself is unhappy.
    """
    spi_present = False
    gpiochips: list[str] = []
    cpuinfo_raspberry = False
    notes: list[str] = []

    try:
        spi_present = os.path.exists(SPI_PATH)
    except OSError as exc:
        notes.append(f"spi probe failed: {exc}")

    try:
        gpiochips = sorted(glob.glob("/dev/gpiochip*"))
    except OSError as exc:
        notes.append(f"gpiochip probe failed: {exc}")

    # epdconfig picks its platform by grepping this file at import time; a miss
    # silently falls through to the JetsonNano branch (PLAN.md 7).
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as fh:
            cpuinfo_raspberry = "Raspberry" in fh.read()
    except OSError as exc:
        notes.append(f"/proc/cpuinfo unreadable: {exc}")

    if spi_present:
        notes.append(f"{SPI_PATH} present")
    else:
        notes.append(SPI_REMEDY)
    if gpiochips:
        notes.append("gpiochips: " + ", ".join(gpiochips))
    else:
        notes.append("no /dev/gpiochip* found")
    notes.append(
        "cpuinfo names Raspberry"
        if cpuinfo_raspberry
        else "cpuinfo does not name Raspberry (not a Pi host)"
    )

    return {
        "spi_present": spi_present,
        "spi_path": SPI_PATH,
        "gpiochips": gpiochips,
        "cpuinfo_raspberry": cpuinfo_raspberry,
        "detail": "; ".join(notes),
    }


def make_display(driver: str, data_dir: Path, rotate: int = 0) -> Display:
    """Build the named display. Unknown name -> DisplayError.

    `rotate` is only meaningful for `epd` (0 or 180); the preview drivers write
    the layout's own orientation so the web UI matches the composed frame.
    """
    key = (driver or "").strip().lower()
    if key == "epd":
        from .epd import EpdDisplay

        return EpdDisplay(rotate=rotate)
    if key == "png":
        from .png import PngDisplay

        return PngDisplay(data_dir)
    if key == "null":
        from .null import NullDisplay

        return NullDisplay()
    raise DisplayError(
        f"unknown display driver {driver!r}; expected one of 'epd', 'png', 'null'"
    )
