"""Real panel driver: Waveshare 2.13" e-Paper HAT V4 (epd2in13_V4).

The manufacturer's constraints are not style advice -- violating them destroys
the panel. Quoted from the
`2.13inch e-Paper HAT Manual <https://www.waveshare.com/wiki/2.13inch_e-Paper_HAT_Manual>`_
Precautions, and the
`2.13inch e-Paper V4 Specification <https://files.waveshare.com/upload/4/4e/2.13inch_e-Paper_V4_Specification.pdf>`_:

* "it is recommended that the refresh interval is **at least 180s**, and refresh
  at least once every 24 hours" (Precautions #3)
* "when the screen is not refreshed, please set the screen to sleep mode or
  power off it. Otherwise, the screen will remain in a high voltage state for a
  long time, which will damage the e-Paper and cannot be repaired!"
  (Precautions #2) -- so ``sleep()`` runs after **every** refresh, no exceptions.
* Going over 24 h without any refresh causes "Ghosting" or "Image Sticking"
  (V4 specification 16.5).

Partial refresh is NOT used. Precautions #1 caps consecutive partials at five,
but the deciding constraint is simpler: sleeping after every refresh (above)
powers the controller down, and `displayPartial()` diffs against the old-image
RAM bank that the sleep destroys. See `push()`.

The paint loop owns the 180 s interval. This module is the backstop: it refuses
to let a caller bug quietly damage the hardware.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Any

from PIL import Image

from .base import SPI_PATH, SPI_REMEDY, DisplayError, probe_host

logger = logging.getLogger(__name__)

#: Landscape frame size render() produces; getbuffer() rotates it to native.
FRAME_SIZE = (250, 122)

#: 122x250 native at a 16 B stride. Upstream getbuffer() does *not* raise on a
#: size mismatch: it logs a warning and returns int(122/8)*250 == 3750 bytes,
#: which would be pushed as a garbage frame. Assert the length every push.
FRAME_BYTES = 4000



def _load_driver() -> tuple[Any, Any]:
    """Import the vendored driver, adding `image/` to sys.path if needed.

    Importing ``vendor.waveshare_epd.epdconfig`` has side effects: it selects a
    platform implementation and claims GPIO 17/25/18/24 at *module* scope. It is
    therefore only ever imported from inside EpdDisplay.__init__, never at
    module import time, so that `screenstats.display.epd` stays importable on a
    machine with no HAT (and so `server` can never accidentally claim the pins).
    """
    try:
        from vendor.waveshare_epd import epd2in13_V4, epdconfig  # noqa: PLC0415
    except ImportError:
        # image/ is the package root in the container; make the vendored copy
        # importable even when PYTHONPATH only names the screenstats package.
        image_root = str(Path(__file__).resolve().parents[2])
        if image_root not in sys.path:
            sys.path.insert(0, image_root)
        from vendor.waveshare_epd import epd2in13_V4, epdconfig  # noqa: PLC0415
    return epd2in13_V4, epdconfig


class EpdDisplay:
    """Drives the panel and enforces the Waveshare refresh policy."""

    name = "epd"

    def __init__(self, rotate: int = 0) -> None:
        if rotate not in (0, 180):
            raise DisplayError(f"rotate must be 0 or 180, got {rotate!r}")
        self.rotate = rotate

        host = probe_host()

        # epdconfig chooses its platform at import time with
        # `if "Raspberry" in output:` against /proc/cpuinfo. A miss falls
        # through to `implementation = JetsonNano()`, whose __init__ does
        # `import Jetson.GPIO` and dies with a misleading ImportError.
        if not host["cpuinfo_raspberry"]:
            raise DisplayError(
                "/proc/cpuinfo does not mention 'Raspberry', so the vendored "
                "epdconfig would select its JetsonNano backend and fail on "
                "'import Jetson.GPIO'. The 'epd' driver only runs on a "
                f"Raspberry Pi host; use SCREENSTATS_DRIVER=png here. "
                f"Probe: {host['detail']}"
            )

        # Expected first-run state on umbrelOS: its Pi config.txt enables I2C
        # and not SPI (PLAN.md 0.2), so say exactly how to fix it.
        if not host["spi_present"]:
            raise DisplayError(f"{SPI_PATH} does not exist. {SPI_REMEDY}")

        try:
            epd2in13_V4, epdconfig = _load_driver()
        except Exception as exc:  # ImportError, gpiozero pin errors, OSError
            raise DisplayError(
                "failed to load the vendored waveshare_epd driver "
                f"({type(exc).__name__}: {exc}). Requires spidev, gpiozero and "
                "lgpio, exactly one renderer replica (GPIO 17/25/18/24 are "
                "claimed at import and a second process gets GPIOPinInUse), "
                "and read/write access to /dev/gpiochip0 and "
                f"{SPI_PATH}. Probe: {host['detail']}"
            ) from exc

        self._epdconfig = epdconfig
        self._epd = epd2in13_V4.EPD()

        self.pushes = 0
        self.last_push = 0.0
        self._asleep = True
        self._closed = False

    # ---- frame preparation ------------------------------------------------

    def _buffer(self, img: Image.Image) -> bytearray:
        if img.size != FRAME_SIZE:
            raise DisplayError(
                f"frame is {img.size}, expected {FRAME_SIZE}; the panel driver "
                "would silently return a wrong-length buffer"
            )
        if self.rotate == 180:
            # Panel orientation is a display concern, not the layout's.
            img = img.rotate(180)
        buf = self._epd.getbuffer(img)
        if len(buf) != FRAME_BYTES:
            raise DisplayError(
                f"getbuffer() returned {len(buf)} bytes, expected {FRAME_BYTES}; "
                "upstream returns a 3750-byte blank buffer instead of raising "
                "when the image size is wrong"
            )
        return buf

    # ---- Display protocol -------------------------------------------------

    def push(self, img: Image.Image) -> None:
        """Paint one frame as a full refresh, then sleep the panel.

        The sequence is the vendor's own non-partial one from
        ``epd_2in13_V4_test.py``: ``init()`` -> ``Clear(0xFF)`` -> ``display()``.

        ``Clear`` before ``display`` is not redundant. Wiki FAQ: "when the EPD
        wakes up, the screen must be cleared first, to avoid the afterimage
        phenomenon to the greatest extent." Two full updates cost ~4 s, which is
        irrelevant at a >=180 s cadence.

        Why there is no partial-refresh path here any more: ``sleep()`` issues
        deep sleep (0x10/0x01) and ``module_exit()`` drops the PWR pin, so the
        controller loses its RAM; ``init()`` then issues SWRESET, clearing it
        again. ``displayPartial()`` diffs the new frame against the old-image RAM
        bank (0x26), so after a sleep/init cycle it diffs against undefined
        content and the previous image is never driven out -- it ghosts through,
        e.g. a "01" and an "11" overlapping. Partial refresh is only valid for
        successive updates inside one power-on session, which a >=180 s interval
        with a mandatory sleep can never be.
        """
        if self._closed:
            raise DisplayError("display is closed")

        buf = self._buffer(img)

        if self._epd.init() != 0:
            raise DisplayError(
                "epd.init() failed: module_init() could not open SPI/GPIO "
                f"({SPI_PATH}, /dev/gpiochip0)"
            )
        self._asleep = False

        try:
            self._epd.Clear(0xFF)
            self._epd.display(buf)
        finally:
            # Mandatory after every refresh, including a failed one: leaving
            # the panel powered at high voltage is what destroys it.
            self.sleep()

        self.pushes += 1
        self.last_push = time.time()

    def sleep(self) -> None:
        """Enter deep sleep. Idempotent -- pushes already sleep on their way out.

        The driver's sleep() ends in ``epdconfig.module_exit()``, closing SPI, so
        issuing it twice would write commands over a closed handle.
        """
        if self._closed or self._asleep:
            return
        self._asleep = True
        self._epd.sleep()

    def close(self) -> None:
        """Release SPI and the GPIO pins for the next container start.

        ``module_exit(cleanup=True)`` is the only path that closes the gpiozero
        devices; without it a restart hits GPIOPinInUse. Note the asymmetry:
        ``cleanup=True`` must **never** be passed to ``module_init``, where it
        selects a completely different ctypes DEV_Config.so path.
        """
        if self._closed:
            return
        try:
            self.sleep()
        finally:
            self._closed = True
            self._epdconfig.module_exit(cleanup=True)
