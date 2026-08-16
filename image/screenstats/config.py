"""User configuration (`config.json`), written by `server`, read by `renderer`.

The one value in here that is not a preference is `refresh_seconds`.

Waveshare's 2.13" e-Paper manual (quoted in PLAN.md §0.1) states that the
refresh interval must be **at least 180 s**, that the panel must be put to
sleep between refreshes because "the screen will remain in a high voltage
state for a long time, which will damage the e-Paper and cannot be repaired",
and that refreshing partially without periodic full refreshes leaves the panel
in a state "which cannot be repaired".

So the 180 s floor is a hardware-safety limit, not a cosmetic default, and it
is enforced in code — in `Config.__post_init__`, which every construction path
(including `from_dict`) goes through — rather than being documented and hoped
for. A hand-edited `config.json` asking for a 5 s tick is silently clamped to
180 s instead of destroying the panel. `rotate` is clamped for the same
class of reason: only 0 and 180 are meaningful for a landscape frame, and any
other value would reach the driver as an unsupported rotation.

Loading is deliberately total: a missing, truncated, hand-edited or
older-schema file yields defaults and never raises, because the renderer must
keep painting regardless of what is in this file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from . import state

__all__ = [
    "REFRESH_FLOOR_SECONDS",
    "Location",
    "Config",
    "data_dir",
    "config_path",
    "to_dict",
    "from_dict",
    "load",
    "save",
    "temp_unit",
]

# Waveshare hard floor between panel refreshes. See the module docstring and
# PLAN.md §0.1 — going below this damages the panel irreversibly.
REFRESH_FLOOR_SECONDS = 180

DEFAULT_REFRESH_SECONDS = 300
DEFAULT_DATA_DIR = "/data"
CONFIG_FILENAME = "config.json"

UNITS = ("metric", "imperial")
LOCATION_MODES = ("auto", "manual")
ROTATIONS = (0, 180)


@dataclass(frozen=True)
class Location:
    mode: str = "auto"  # "auto" | "manual"
    lat: float | None = None
    lon: float | None = None
    label: str | None = None


@dataclass(frozen=True)
class Config:
    refresh_seconds: int = DEFAULT_REFRESH_SECONDS
    units: str = "metric"  # "metric" | "imperial"
    clock_24h: bool = True
    rotate: int = 0  # 0 | 180
    location: Location = Location()
    timezone: str | None = None
    quiet_hours: tuple[str, str] | None = None  # ("23:00", "07:00") local HH:MM

    def __post_init__(self) -> None:
        # Hardware safety, not cosmetics: every Config that exists anywhere in
        # the process is already clamped, so no caller can forget to do it.
        # frozen=True means the fields must be set through object.__setattr__.
        floor = max(REFRESH_FLOOR_SECONDS, _as_int(self.refresh_seconds, DEFAULT_REFRESH_SECONDS))
        if floor != self.refresh_seconds:
            object.__setattr__(self, "refresh_seconds", floor)
        if self.rotate not in ROTATIONS:
            object.__setattr__(self, "rotate", 180 if _as_int(self.rotate, 0) == 180 else 0)
        quiet = _as_quiet_hours(self.quiet_hours)
        if quiet != self.quiet_hours:
            object.__setattr__(self, "quiet_hours", quiet)


# --------------------------------------------------------------------------
# tolerant coercion
#
# `config.json` is user-writable and survives app upgrades, so every reader
# below treats its input as untrusted: wrong type, wrong range and missing
# key all fall back to the default rather than raising.
# --------------------------------------------------------------------------


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool):  # bool is an int subclass; not a count.
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return default if value != value or value in (float("inf"), float("-inf")) else int(value)
    if isinstance(value, str):
        try:
            # int(float(...)) also accepts "300.0"; OverflowError guards
            # "inf" and ValueError guards "nan" and plain garbage.
            return int(float(value.strip()))
        except (ValueError, OverflowError):
            return default
    return default


def _as_float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        f = float(value)
    elif isinstance(value, str):
        try:
            f = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    # Reject NaN / +-inf: they round-trip through JSON as invalid literals.
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off"):
            return False
    return default


def _as_choice(value: object, choices: tuple[str, ...], default: str) -> str:
    if isinstance(value, str):
        text = value.strip().lower()
        if text in choices:
            return text
    return default


def _as_str_or_none(value: object) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _as_hhmm(value: object) -> str | None:
    """Strict `HH:MM` parse, normalised to zero-padded 24-hour form.

    Digits and one colon only — no seconds, no am/pm, no whitespace inside.
    A single-digit field is accepted and padded ("7:5" -> "07:05"); anything
    else is not a time and becomes `None` (i.e. "no quiet hours").
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    hh, sep, mm = text.partition(":")
    # isascii(): str.isdigit() is true for "²" and other unicode digits that
    # int() then rejects, and this function must never raise.
    if sep != ":" or not (hh.isascii() and hh.isdigit() and mm.isascii() and mm.isdigit()):
        return None
    if not (1 <= len(hh) <= 2) or not (1 <= len(mm) <= 2):
        return None
    hour, minute = int(hh), int(mm)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def _as_quiet_hours(value: object) -> tuple[str, str] | None:
    """Coerce a 2-element `HH:MM` pair; anything else means "no quiet hours"."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    start, end = _as_hhmm(value[0]), _as_hhmm(value[1])
    if start is None or end is None:
        return None
    return (start, end)


def _location_from_dict(raw: object) -> Location:
    if not isinstance(raw, dict):
        return Location()
    mode = _as_choice(raw.get("mode"), LOCATION_MODES, "auto")
    lat = _as_float_or_none(raw.get("lat"))
    lon = _as_float_or_none(raw.get("lon"))
    # Out-of-range coordinates are as useless as absent ones, and would make
    # the weather fetch fail rather than fall back to IP geolocation.
    if lat is None or not (-90.0 <= lat <= 90.0):
        lat = None
    if lon is None or not (-180.0 <= lon <= 180.0):
        lon = None
    if mode == "manual" and (lat is None or lon is None):
        mode = "auto"
    return Location(mode=mode, lat=lat, lon=lon, label=_as_str_or_none(raw.get("label")))


# --------------------------------------------------------------------------
# paths, serialisation, io
# --------------------------------------------------------------------------


def data_dir() -> Path:
    """App data root: `$SCREENSTATS_DATA`, else `/data` (PLAN.md §8)."""
    return Path(os.environ.get("SCREENSTATS_DATA") or DEFAULT_DATA_DIR)


def config_path(path: Path | None = None) -> Path:
    return Path(path) if path is not None else data_dir() / CONFIG_FILENAME


def to_dict(cfg: Config) -> dict:
    """Serialise to the exact `config.json` schema of PLAN.md §9."""
    return {
        "refresh_seconds": cfg.refresh_seconds,
        "units": cfg.units,
        "clock_24h": cfg.clock_24h,
        "rotate": cfg.rotate,
        "location": {
            "mode": cfg.location.mode,
            "lat": cfg.location.lat,
            "lon": cfg.location.lon,
            "label": cfg.location.label,
        },
        "timezone": cfg.timezone,
        # 2-element list or null; JSON has no tuples.
        "quiet_hours": list(cfg.quiet_hours) if cfg.quiet_hours else None,
    }


def from_dict(raw: dict) -> Config:
    """Build a Config from untrusted JSON. Unknown keys ignored, bad values
    replaced by defaults, `refresh_seconds` and `rotate` clamped."""
    if not isinstance(raw, dict):
        return Config()
    rotate = _as_int(raw.get("rotate"), 0)
    return Config(
        # Clamped again in __post_init__; done here too so the intent is
        # visible at the parse site. See module docstring / PLAN.md §0.1.
        refresh_seconds=max(
            REFRESH_FLOOR_SECONDS, _as_int(raw.get("refresh_seconds"), DEFAULT_REFRESH_SECONDS)
        ),
        units=_as_choice(raw.get("units"), UNITS, "metric"),
        clock_24h=_as_bool(raw.get("clock_24h"), True),
        rotate=rotate if rotate in ROTATIONS else 0,
        location=_location_from_dict(raw.get("location")),
        timezone=_as_str_or_none(raw.get("timezone")),
        quiet_hours=_as_quiet_hours(raw.get("quiet_hours")),
    )


def load(path: Path | None = None) -> Config:
    """Read `config.json`; missing or corrupt content yields defaults."""
    raw = state.read_json(config_path(path))
    if raw is None:
        return Config()
    return from_dict(raw)


def save(cfg: Config, path: Path | None = None) -> None:
    """Publish `config.json` atomically (`renderer` may read it mid-write)."""
    # Re-run the clamps: `cfg` may have been built with dataclasses.replace on
    # a field-by-field basis by the web UI.
    state.publish_json(config_path(path), to_dict(replace(cfg)))


def temp_unit(cfg: Config) -> str:
    return "°F" if cfg.units == "imperial" else "°C"
