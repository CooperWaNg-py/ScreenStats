"""Coarse IP geolocation, cached on disk.

Two keyless providers, tried in order:

1. ``http://ip-api.com/json/`` — the free tier is **plaintext HTTP only**.
   Requesting the same path over ``https://`` returns ``403`` with
   ``{"status":"fail","message":"SSL unavailable for this endpoint"}``, so the
   scheme below is deliberate and must not be "fixed" to https.
2. ``https://ipwho.is/`` — HTTPS fallback for hosts where plaintext egress is
   blocked. Its IANA timezone name lives at ``timezone.id``.

Called once per container start and then at most once per day: a home IP's
coarse location does not move, and neither provider belongs anywhere near the
paint loop.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

if __package__ in (None, ""):  # direct execution: python3 image/screenstats/geo.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from screenstats.state import publish_json, read_json

# Some providers reject the stock "Python-urllib/3.x" agent outright.
USER_AGENT = "screenstats-eink/0.1 (+https://github.com/CooperWaNg-py/ScreenStats)"

# urllib has no separate connect timeout: the single `timeout=` argument is
# applied to every blocking socket operation, so 10 s is the whole-request
# ceiling (the design's "5 s connect / 10 s read" cannot be expressed without
# dropping to raw sockets, and 10 s already keeps a hung provider off the
# paint loop).
TIMEOUT_SECONDS = 10

# fields=33604048 -> status,message,city,lat,lon,timezone,offset
IP_API_URL = "http://ip-api.com/json/?fields=33604048"
IPWHO_IS_URL = "https://ipwho.is/?fields=ip,latitude,longitude,timezone,city"


@dataclass(frozen=True)
class GeoResult:
    lat: float
    lon: float
    city: str
    timezone: str
    source: str
    fetched_at: float


def _log(message: str) -> None:
    print(f"geo: {message}", file=sys.stderr, flush=True)


def _get(url: str) -> tuple[int, dict[str, str], bytes]:
    """GET ``url`` and return ``(status, headers, body)``.

    An HTTP error status is data, not an exception: ip-api throttles with a
    ``429`` whose body is empty, and its lookup failures arrive as ``200``.
    Callers need the status and the headers in both cases, so ``HTTPError`` is
    unwrapped back into the same tuple shape. ``(0, {}, b"")`` means the
    request never completed (DNS, refused, timeout, TLS).
    """
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return int(response.status), dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        headers = dict(exc.headers.items()) if exc.headers else {}
        try:
            body = exc.read() or b""
        except OSError:
            body = b""
        return int(exc.code), headers, body
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _log(f"{url} unreachable: {exc}")
        return 0, {}, b""


def _decode(url: str, body: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError) as exc:
        _log(f"{url} returned unparseable body ({len(body)} B): {exc}")
        return {}
    if not isinstance(parsed, dict):
        _log(f"{url} returned {type(parsed).__name__}, expected object")
        return {}
    return parsed


def _coords(raw: dict[str, Any], lat_key: str, lon_key: str) -> tuple[float, float] | None:
    try:
        lat = float(raw[lat_key])
        lon = float(raw[lon_key])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return lat, lon


def _fetch_ip_api() -> GeoResult | None:
    status, headers, body = _get(IP_API_URL)

    # Throttle check comes first: the 429 body is empty (Content-Length: 0),
    # so parsing before branching raises instead of backing off. X-Ttl is the
    # only backoff hint ip-api gives — there is no Retry-After.
    if status == 429:
        ttl = headers.get("X-Ttl") or headers.get("x-ttl")
        _log(f"ip-api throttled (429){f', X-Ttl={ttl}s' if ttl else ''}")
        return None
    if status != 200:
        _log(f"ip-api HTTP {status}")
        return None

    raw = _decode(IP_API_URL, body)
    # ip-api reports lookup failure as HTTP 200 + status:"fail". A status-code
    # check alone would happily accept it.
    if raw.get("status") != "success":
        _log(f"ip-api fail: {raw.get('message') or raw.get('status') or 'no status field'}")
        return None

    coords = _coords(raw, "lat", "lon")
    if coords is None:
        _log("ip-api success without usable lat/lon")
        return None

    timezone = raw.get("timezone")
    return GeoResult(
        lat=coords[0],
        lon=coords[1],
        city=str(raw.get("city") or ""),
        # IANA identifier only; ip-api's numeric `offset` is a point-in-time
        # value that goes stale at the next DST transition.
        timezone=str(timezone) if isinstance(timezone, str) and timezone else "",
        source="ip-api.com",
        fetched_at=time.time(),
    )


def _fetch_ipwho_is() -> GeoResult | None:
    status, _headers, body = _get(IPWHO_IS_URL)
    if status != 200:
        _log(f"ipwho.is HTTP {status}")
        return None

    raw = _decode(IPWHO_IS_URL, body)
    # ipwho.is flags failure with success:false (also on HTTP 200).
    if raw.get("success") is False:
        message = raw.get("message")
        if isinstance(message, dict):
            message = message.get("message")
        _log(f"ipwho.is fail: {message or 'no message'}")
        return None

    coords = _coords(raw, "latitude", "longitude")
    if coords is None:
        _log("ipwho.is response without usable latitude/longitude")
        return None

    timezone = raw.get("timezone")
    if isinstance(timezone, dict):  # documented shape: {"id": "Australia/Melbourne", ...}
        timezone = timezone.get("id")
    return GeoResult(
        lat=coords[0],
        lon=coords[1],
        city=str(raw.get("city") or ""),
        timezone=str(timezone) if isinstance(timezone, str) and timezone else "",
        source="ipwho.is",
        fetched_at=time.time(),
    )


PROVIDERS: tuple[Callable[[], "GeoResult | None"], ...] = (_fetch_ip_api, _fetch_ipwho_is)


def _from_cache(cache_path: Path) -> GeoResult | None:
    raw = read_json(cache_path)
    if not raw:
        return None
    coords = _coords(raw, "lat", "lon")
    if coords is None:
        return None
    try:
        fetched_at = float(raw.get("fetched_at", 0.0))
    except (TypeError, ValueError):
        fetched_at = 0.0
    return GeoResult(
        lat=coords[0],
        lon=coords[1],
        city=str(raw.get("city") or ""),
        timezone=str(raw.get("timezone") or ""),
        source=str(raw.get("source") or "cache"),
        fetched_at=fetched_at,
    )


def resolve(cache_path: Path, ttl_seconds: int = 86400, force: bool = False) -> GeoResult | None:
    """Return a location, preferring a fresh cache entry over a network call.

    A fresh cache short-circuits entirely. Otherwise the providers are tried
    in order; the first success is persisted to ``cache_path`` and returned.
    If every provider fails the stale cache entry is returned unchanged (a
    day-old city beats a blank panel), and only a total absence of both
    network and cache yields ``None``.
    """
    cached = _from_cache(cache_path)
    if cached is not None and not force:
        age = time.time() - cached.fetched_at
        if 0.0 <= age < ttl_seconds:
            return cached

    for provider in PROVIDERS:
        result = provider()
        if result is not None:
            try:
                publish_json(cache_path, asdict(result))
            except OSError as exc:  # a read-only cache dir must not lose the lookup
                _log(f"cache write to {cache_path} failed: {exc}")
            return result

    if cached is not None:
        _log(f"all providers failed; serving cache from {time.time() - cached.fetched_at:.0f}s ago")
    return cached


if __name__ == "__main__":
    import tempfile

    smoke_cache = Path(tempfile.gettempdir()) / "screenstats-geo-smoke.json"
    geo = resolve(smoke_cache, force=True)
    if geo is None:
        print("FAIL: no provider returned a location (see diagnostics above)")
        raise SystemExit(1)
    print("ip geolocation")
    print(f"  lat      : {geo.lat}")
    print(f"  lon      : {geo.lon}")
    print(f"  city     : {geo.city!r}")
    print(f"  timezone : {geo.timezone!r}")
    print(f"  source   : {geo.source}")
    print(f"  cache    : {smoke_cache}")
    cached_again = resolve(smoke_cache)
    print(f"  cache hit: {cached_again == geo}")
