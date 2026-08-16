"""Display drivers. Import `base` and select with `base.make_display`.

`epd` is deliberately not imported here: pulling in the vendored driver claims
GPIO pins as a side effect of module import, so it is loaded lazily by
`make_display` only when the `epd` driver is actually requested.
"""

from __future__ import annotations

from .base import Display, DisplayError, make_display, probe_host

__all__ = ["Display", "DisplayError", "make_display", "probe_host"]
