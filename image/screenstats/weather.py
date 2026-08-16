"""Open-Meteo forecast client (keyless, HTTPS) plus its on-disk cache.

One request returns current conditions, today's min/max, a machine-readable
WMO condition code and the IANA timezone. ``fetch()`` raises on any failure;
caching and the stale-marker policy belong to the caller.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # direct execution: python3 image/screenstats/weather.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from screenstats.state import publish_json, read_json

USER_AGENT = "screenstats-eink/0.1 (+https://github.com/CooperWaNg-py/ScreenStats)"

# urllib applies its single `timeout=` to every blocking socket operation, so a
# separate 5 s connect budget is not expressible without raw sockets; 10 s is
# the whole-request ceiling and keeps a hung socket off the paint loop.
TIMEOUT_SECONDS = 10

API_URL = "https://api.open-meteo.com/v1/forecast"
CURRENT_VARS = (
    "temperature_2m,relative_humidity_2m,apparent_temperature,"
    "is_day,weather_code,wind_speed_10m"
)
DAILY_VARS = "weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset"

# Open-Meteo collapses WMO table 4677 to exactly these 28 codes. Labels are
# pre-fitted to the 250x122 panel: <= 12 characters each.
WMO: dict[int, str] = {
    0: "Clear",
    1: "Mainly clear",
    2: "Part cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Rime fog",
    51: "Lt drizzle",
    53: "Drizzle",
    55: "Hvy drizzle",
    56: "Frz drizzle",
    57: "Frz drizzle+",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Frz rain",
    67: "Frz rain+",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Lt showers",
    81: "Showers",
    82: "Hvy showers",
    85: "Snow shwrs",
    86: "Snow shwrs+",
    95: "T-storm",
    96: "T-storm hail",
    99: "Hail T-storm",
}

UNKNOWN_CONDITION = "\u2014"  # em dash: anything outside the 28 collapsed codes


class WeatherError(RuntimeError):
    """Raised by :func:`fetch` when a usable forecast could not be obtained."""


def wmo_label(code: int | None) -> str:
    if code is None:
        return UNKNOWN_CONDITION
    try:
        key = int(code)
    except (TypeError, ValueError):
        return UNKNOWN_CONDITION
    return WMO.get(key, UNKNOWN_CONDITION)


@dataclass(frozen=True)
class Weather:
    temp: float
    feels: float
    humidity: int
    code: int
    condition: str
    lo: float
    hi: float
    is_day: bool
    # IANA identifier echoed by timezone=auto. Never store the numeric offset
    # alone: it is a point-in-time value that goes stale at the next DST jump.
    timezone: str
    utc_offset_seconds: int
    # Timezone-NAIVE local ISO strings ("2026-08-16T06:41"). Parsing these as
    # UTC is a full-offset error.
    sunrise: str
    sunset: str
    temp_unit: str
    wind: float
    wind_unit: str
    # Wall-clock time this payload was received. NOTE: the API's own
    # `current.time` is the model's 900 s step and lags real time by up to
    # 15 minutes -- never drive the rendered clock from the API, only from
    # system time.
    fetched_at: float


def _log(message: str) -> None:
    print(f"weather: {message}", file=sys.stderr, flush=True)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result or result in (float("inf"), float("-inf")):  # NaN / Inf
        return default
    return result


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _first(seq: Any, default: Any = None) -> Any:
    """Element 0 of a `daily.*` parallel array (index 0 is today)."""
    if isinstance(seq, list) and seq:
        return seq[0]
    return default


def _query(lat: float, lon: float, units: str) -> str:
    params: dict[str, str] = {
        "latitude": f"{float(lat):.4f}",
        "longitude": f"{float(lon):.4f}",
        "current": CURRENT_VARS,
        "daily": DAILY_VARS,
        # MANDATORY. Without it the API still answers HTTP 200 but with
        # "timezone":"GMT", silently shifting the day boundaries and therefore
        # today's lo/hi, sunrise and sunset. There is no error to detect.
        "timezone": "auto",
        "forecast_days": "1",
    }
    if units == "imperial":
        params["temperature_unit"] = "fahrenheit"
        params["wind_speed_unit"] = "mph"  # echoed back as "mp/h"
        params["precipitation_unit"] = "inch"
    return f"{API_URL}?{urllib.parse.urlencode(params)}"


def fetch(lat: float, lon: float, units: str = "metric") -> Weather:
    """Fetch a forecast. Raises :class:`WeatherError` on any failure.

    The caller owns retry/backoff: Open-Meteo's limiter counts *failed*
    requests too, so a retry storm deepens a block.
    """
    url = _query(lat, lon, units)
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            status = int(response.status)
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            payload = json.loads((exc.read() or b"").decode("utf-8", "replace"))
            if isinstance(payload, dict):
                detail = f": {payload.get('reason') or payload.get('error') or ''}".rstrip(": ")
        except (ValueError, UnicodeDecodeError, OSError):
            detail = ""
        raise WeatherError(f"open-meteo HTTP {exc.code}{detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise WeatherError(f"open-meteo unreachable: {exc}") from exc

    if status != 200:
        raise WeatherError(f"open-meteo HTTP {status}")

    try:
        raw = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise WeatherError(f"open-meteo returned unparseable JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise WeatherError(f"open-meteo returned {type(raw).__name__}, expected object")
    if raw.get("error"):
        raise WeatherError(f"open-meteo error: {raw.get('reason') or 'unspecified'}")

    current = raw.get("current")
    daily = raw.get("daily")
    if not isinstance(current, dict) or not isinstance(daily, dict):
        raise WeatherError("open-meteo response missing 'current' or 'daily'")

    current_units = raw.get("current_units") if isinstance(raw.get("current_units"), dict) else {}
    daily_units = raw.get("daily_units") if isinstance(raw.get("daily_units"), dict) else {}

    # Unit strings are read back from the response, never assumed: requesting
    # wind_speed_unit=mph is reported as "mp/h", and the temperature symbol
    # already arrives fully formed ("°C" / "°F").
    temp_unit = str(
        current_units.get("temperature_2m")
        or daily_units.get("temperature_2m_max")
        or ("\u00b0F" if units == "imperial" else "\u00b0C")
    )
    wind_unit = str(current_units.get("wind_speed_10m") or ("mp/h" if units == "imperial" else "km/h"))

    timezone = raw.get("timezone")
    if not isinstance(timezone, str) or not timezone:
        raise WeatherError("open-meteo response has no timezone (was timezone=auto sent?)")

    temp = _num(current.get("temperature_2m"))
    code = _int(current.get("weather_code"), _int(_first(daily.get("weather_code")), 0))

    return Weather(
        temp=temp,
        feels=_num(current.get("apparent_temperature"), temp),
        humidity=_int(current.get("relative_humidity_2m")),
        code=code,
        condition=wmo_label(code),
        lo=_num(_first(daily.get("temperature_2m_min")), temp),
        hi=_num(_first(daily.get("temperature_2m_max")), temp),
        is_day=_int(current.get("is_day"), 1) == 1,
        timezone=timezone,
        utc_offset_seconds=_int(raw.get("utc_offset_seconds")),
        sunrise=str(_first(daily.get("sunrise")) or ""),
        sunset=str(_first(daily.get("sunset")) or ""),
        temp_unit=temp_unit,
        wind=_num(current.get("wind_speed_10m")),
        wind_unit=wind_unit,
        fetched_at=time.time(),
    )


def load_cache(path: Path) -> Weather | None:
    """Return the last good payload, or ``None`` if absent/unusable."""
    raw = read_json(path)
    if not raw:
        return None
    timezone = raw.get("timezone")
    if not isinstance(timezone, str) or not timezone:
        return None
    if "temp" not in raw:
        return None
    code = _int(raw.get("code"))
    condition = raw.get("condition")
    return Weather(
        temp=_num(raw.get("temp")),
        feels=_num(raw.get("feels")),
        humidity=_int(raw.get("humidity")),
        code=code,
        condition=str(condition) if isinstance(condition, str) and condition else wmo_label(code),
        lo=_num(raw.get("lo")),
        hi=_num(raw.get("hi")),
        is_day=bool(raw.get("is_day", True)),
        timezone=timezone,
        utc_offset_seconds=_int(raw.get("utc_offset_seconds")),
        sunrise=str(raw.get("sunrise") or ""),
        sunset=str(raw.get("sunset") or ""),
        temp_unit=str(raw.get("temp_unit") or "\u00b0C"),
        wind=_num(raw.get("wind")),
        wind_unit=str(raw.get("wind_unit") or "km/h"),
        fetched_at=_num(raw.get("fetched_at")),
    )


def save_cache(path: Path, w: Weather) -> None:
    try:
        publish_json(path, asdict(w))
    except OSError as exc:  # a read-only cache dir must not break the paint loop
        _log(f"cache write to {path} failed: {exc}")


if __name__ == "__main__":
    import tempfile

    assert len(WMO) == 28, f"WMO table has {len(WMO)} entries, expected 28"
    too_long = {code: label for code, label in WMO.items() if len(label) > 12}
    assert not too_long, f"labels wider than the panel allows: {too_long}"
    print(f"WMO table: {len(WMO)} codes, longest label {max(len(v) for v in WMO.values())} chars")

    LAT, LON = -37.8136, 144.9631  # Melbourne, AU
    for unit_system in ("metric", "imperial"):
        try:
            w = fetch(LAT, LON, unit_system)
        except WeatherError as exc:
            print(f"FAIL ({unit_system}): {exc}")
            raise SystemExit(1) from exc
        print(f"forecast for {LAT},{LON} [{unit_system}]")
        print(f"  temp      : {w.temp}{w.temp_unit} (feels {w.feels}{w.temp_unit})")
        print(f"  condition : {w.condition!r} (code {w.code})")
        print(f"  lo / hi   : {w.lo}{w.temp_unit} / {w.hi}{w.temp_unit}")
        print(f"  humidity  : {w.humidity}%")
        print(f"  wind      : {w.wind} {w.wind_unit}")
        print(f"  timezone  : {w.timezone} (utc_offset_seconds={w.utc_offset_seconds})")
        print(f"  sun       : {w.sunrise} -> {w.sunset}  (naive local)")
        print(f"  is_day    : {w.is_day}")
        print(f"  units     : temp_unit={w.temp_unit!r} wind_unit={w.wind_unit!r}")

    cache = Path(tempfile.gettempdir()) / "screenstats-weather-smoke.json"
    save_cache(cache, w)
    print(f"cache round-trip via {cache}: {load_cache(cache) == w}")
