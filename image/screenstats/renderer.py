"""ScreenStats renderer: owns the panel and all data collection.

Two independent loops in one process:

  collect loop (COLLECT_INTERVAL, 10 s)
      host metrics + whatever weather is cached -> state/status.json
      No panel I/O at all. This is what keeps the umbrelOS widget live while the
      panel correctly sits idle, and it supplies the two /proc/stat samples the
      CPU delta needs.

  paint loop (config.refresh_seconds, floor 180 s)
      build the frame, push it, sleep the panel, publish preview.png

Why the paint loop is slow, and why that is not negotiable
----------------------------------------------------------
Waveshare's 2.13" manual, Precautions #1-#3:

  * "it is recommended that the refresh interval is at least 180s, and refresh at
    least once every 24 hours"
  * "the screen cannot be powered on for a long time. When the screen is not
    refreshed, please set the screen to sleep mode or power off it. Otherwise ...
    will damage the e-Paper and cannot be repaired!"
  * "you cannot refresh them with the partial refresh mode all the time. After
    refreshing partially several times, you need to fully refresh EPD once.
    Otherwise, the display effect will be abnormal, which cannot be repaired!"

and the V4 Specification p.9: "add a full-screen refresh after 5 consecutive
operations".

So: >=180 s between pushes, sleep() after every push, and a full refresh every
PARTIAL_BUDGET+1 ticks. The clock therefore reads to 5-minute granularity. A
per-second clock is not a feature we chose to omit; it destroys the panel.

This process is the single owner of the panel. `epdconfig` claims GPIO 17/25/18/24
at *import* time, so a second process importing it fails with GPIOPinInUse. The
server must never import the display layer.
"""

from __future__ import annotations

import io
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PIL import Image

from . import config as cfgmod
from . import geo as geomod
from . import hostinfo
from . import layout
from . import metrics as metricsmod
from . import state as statemod
from . import weather as weathermod
from .display import base as displaybase

log = logging.getLogger("screenstats.renderer")

COLLECT_INTERVAL = 10.0
WEATHER_INTERVAL = 900.0        # 15 min; Open-Meteo's `current` step is 900 s
GEO_TTL = 86400.0
PARTIAL_BUDGET = 5              # V4 spec: full refresh after at most 5 partials
MAX_IDLE = 23 * 3600.0          # force a refresh inside the 24 h ghosting window
STALE_AFTER = WEATHER_INTERVAL * 2


# --------------------------------------------------------------------------
# host preflight
#
# The gpiochip identification lives in screenstats.hostinfo, which is stdlib-only
# so it can be run standalone on a target Pi before anything is installed:
#     python3 -m screenstats.hostinfo
#
# An earlier version of this check read /sys/bus/gpio/devices/gpiochipN/label,
# which DOES NOT EXIST on current kernels (verified absent on 6.18.34+rpt-rpi-v8)
# and so verified nothing at all. hostinfo uses of_node/compatible instead, which
# is indexed by chip number and was verified on the target Pi 4B:
#     gpiochip0 brcm,bcm2711-gpio       -> header
#     gpiochip1 raspberrypi,firmware-gpio -> expander
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# presentation helpers
# --------------------------------------------------------------------------


def tzinfo_for(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown timezone %r, falling back to system local time", name)
        return None


def greeting_for(now: datetime, w: weathermod.Weather | None) -> str:
    """Local wall-clock greeting, nudged by daylight where we know it.

    Boundaries are 12:00 and 18:00. The sunset cross-check keeps an 18:30
    midsummer evening reading "Good Afternoon" while it is still broad daylight,
    which is what a person would say.
    """
    hour = now.hour
    if hour < 12:
        return "Good Morning"
    if hour < 18:
        return "Good Afternoon"
    if w is not None and w.sunset:
        try:
            # Open-Meteo sunrise/sunset are timezone-NAIVE local ISO strings.
            sunset = datetime.fromisoformat(w.sunset)
            if now.replace(tzinfo=None) < sunset:
                return "Good Afternoon"
        except ValueError:
            pass
    return "Good Evening"


def clock_text(now: datetime, cfg: cfgmod.Config) -> str:
    """The exact HH:MM at paint time.

    NOT quantised to the refresh interval. An earlier version floored to 5-minute
    buckets on the theory that a tidy time reads better, but that is strictly worse:
    a paint at 20:03 displayed 20:00 and held it until 20:08, so the panel could be
    ~8 minutes behind. The paint interval already bounds staleness at
    `refresh_seconds`; flooring only adds up to another `refresh_seconds` on top,
    and the buckets do not align to the wall clock anyway because paints are spaced
    from process start rather than from :00.

    Truncating to the minute (never rounding up) still means the panel never shows
    a time it has not reached.
    """
    if cfg.clock_24h:
        return f"{now.hour:02d}:{now.minute:02d}"
    h12 = now.hour % 12 or 12
    return f"{h12}:{now.minute:02d}{'am' if now.hour < 12 else 'pm'}"


def build_snapshot(
    now: datetime,
    cfg: cfgmod.Config,
    m: metricsmod.HostMetrics,
    g: geomod.GeoResult | None,
    w: weathermod.Weather | None,
) -> layout.Snapshot:
    stale = None
    if w is not None and (time.time() - w.fetched_at) > STALE_AFTER:
        stale = datetime.fromtimestamp(w.fetched_at, tz=now.tzinfo).strftime("%H:%M")

    city = cfg.location.label or (g.city if g else "") or "Unknown"
    return layout.Snapshot(
        clock=clock_text(now, cfg),
        date=now.strftime("%a %d %b"),
        greeting=greeting_for(now, w),
        cpu=m.cpu,
        ram=m.ram,
        disk=m.disk,
        city=city,
        temp_c=w.temp if w else 0.0,
        feels_c=w.feels if w else 0.0,
        condition=w.condition if w else "no data",
        lo_c=w.lo if w else 0.0,
        hi_c=w.hi if w else 0.0,
        stale=stale,
    )


def status_payload(
    now: datetime,
    cfg: cfgmod.Config,
    m: metricsmod.HostMetrics,
    g: geomod.GeoResult | None,
    w: weathermod.Weather | None,
    panel: dict,
    host: dict,
    snap: layout.Snapshot,
) -> dict:
    weather_obj = None
    if w is not None:
        weather_obj = {
            "temp": w.temp,
            "feels": w.feels,
            "humidity": w.humidity,
            "code": w.code,
            "condition": w.condition,
            "lo": w.lo,
            "hi": w.hi,
            "unit": w.temp_unit,
            "fetched_at": w.fetched_at,
            "stale": (time.time() - w.fetched_at) > STALE_AFTER,
        }
    location_obj = None
    if g is not None:
        location_obj = {"city": g.city, "timezone": g.timezone, "source": g.source}
    return {
        "schema": 1,
        "updated_at": time.time(),
        "metrics": {
            "cpu": m.cpu,
            "ram": m.ram,
            "disk": m.disk,
            "ram_used": m.ram_used,
            "ram_total": m.ram_total,
            "disk_used": m.disk_used,
            "disk_total": m.disk_total,
        },
        "weather": weather_obj,
        "location": location_obj,
        "clock": snap.clock,
        "date": snap.date,
        "greeting": snap.greeting,
        "panel": panel,
        "host": host,
    }


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------


@dataclass
class Panel:
    """Owns the refresh cycle and the hardware-safety invariants."""

    display: displaybase.Display
    data_dir: Path | None = None
    tick: int = 0
    last_paint: float = 0.0
    last_full: float = 0.0
    error: str | None = None

    def _publish_preview(self, img: object) -> None:
        """Publish the frame for the web UI, whatever the driver is.

        The preview must NOT come from the display driver: only PngDisplay writes
        files, so on real hardware (driver=epd) the status page preview would stay
        blank forever. The web UI is the app's only window onto the panel, so the
        renderer owns this.
        """
        if self.data_dir is None:
            return


        state_dir = self.data_dir / "state"
        try:
            buf = io.BytesIO()
            img.save(buf, format="PNG")                     # type: ignore[attr-defined]
            statemod.publish_bytes(state_dir / "preview.png", buf.getvalue())
            big = img.resize(                               # type: ignore[attr-defined]
                (layout.W * 4, layout.H * 4), Image.NEAREST
            )
            buf = io.BytesIO()
            big.save(buf, format="PNG")
            statemod.publish_bytes(state_dir / "preview-4x.png", buf.getvalue())
        except Exception as exc:                            # never fail a paint over this
            log.warning("preview publish failed: %s", exc)

    def wants_full(self) -> bool:
        if self.last_full <= 0.0:
            return True                                  # first paint of the process
        if self.tick % (PARTIAL_BUDGET + 1) == 0:
            return True                                  # V4 spec 5-partial budget
        if (time.time() - self.last_full) > MAX_IDLE:
            return True                                  # 24 h ghosting window
        return False

    def paint(self, snap: layout.Snapshot) -> None:
        full = self.wants_full()
        img = layout.render(snap)
        # Publish before pushing: if the panel is broken the web UI should still
        # show what *would* have been drawn, which is what makes the status page
        # useful for diagnosing a hardware fault.
        self._publish_preview(img)
        try:
            self.display.push(img, full=full)
            self.display.sleep()
        except Exception as exc:                          # keep the loop alive
            self.error = f"{type(exc).__name__}: {exc}"
            log.error("panel push failed: %s", self.error)
            return
        self.error = None
        self.last_paint = time.time()
        if full:
            self.last_full = self.last_paint
        self.tick += 1
        log.info(
            "painted tick=%d %s clock=%s", self.tick,
            "FULL" if full else "partial", snap.clock,
        )

    def as_dict(self, next_paint: float) -> dict:
        return {
            "driver": self.display.name,
            "last_paint": self.last_paint,
            "next_paint": next_paint,
            "tick": self.tick,
            "last_full": self.last_full,
            "error": self.error,
        }


_stop = False


def _handle_signal(signum: int, _frame: object) -> None:
    global _stop
    log.info("signal %s received, shutting down", signum)
    _stop = True


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("SCREENSTATS_LOG", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    data = cfgmod.data_dir()
    statemod.ensure_dirs(data)
    status_path = data / "state" / "status.json"

    host = displaybase.probe_host()
    # Enrich with the devicetree SPI status and per-chip roles. 'disabled' here vs
    # 'okay' is the difference between "user has not enabled SPI" and "enabled but
    # the node is missing for some other reason", which are different problems.
    spi = hostinfo.spi_status()
    host["devicetree_status"] = spi["devicetree_status"]
    host["gpiochip_roles"] = {
        name: info["role"] for name, info in hostinfo.summary()["gpiochips"].items()
    }
    problem = hostinfo.preflight_gpiochip()
    if problem:
        host["detail"] = f"{host.get('detail', '')} | gpiochip preflight: {problem}".strip(" |")
        log.error("gpiochip preflight: %s", problem)

    driver = os.environ.get("SCREENSTATS_DRIVER", "epd")
    disk_path = os.environ.get("SCREENSTATS_DISK_PATH", str(data))
    collector = metricsmod.MetricsCollector(disk_path)
    # The panel paints on the first loop iteration and then not again for
    # `refresh_seconds`. Without a warm-up the first frame reported CPU 0% and
    # that stale zero sat on the display for five minutes -- the first thing a
    # user ever sees. One second of baseline costs nothing at startup.
    collector.warm_up(1.0)

    # Fonts come from the image (fonts-dejavu-core). If they are missing every
    # render would fail identically, so fail fast and loudly instead of looping.
    boot_cfg = cfgmod.load()
    try:
        layout.render(layout.Snapshot())
    except layout.LayoutError as exc:
        log.error("layout unavailable: %s", exc)
        return 1

    try:
        display: displaybase.Display = displaybase.make_display(
            driver, data, rotate=boot_cfg.rotate
        )
    except displaybase.DisplayError as exc:
        # The expected first-run state on umbrelOS: SPI is not enabled. Do not
        # crash-loop silently -- keep publishing status so the web UI can explain
        # the problem, and retry so the app heals itself after the user reboots.
        log.error("display unavailable: %s", exc)
        display = displaybase.make_display("null", data)
        host["detail"] = f"{host.get('detail', '')} | {exc}".strip(" |")

    panel = Panel(display=display, data_dir=data)
    active_rotate = boot_cfg.rotate

    geo_cache = data / "cache" / "geo.json"
    weather_cache = data / "cache" / "weather.json"

    cached_weather = weathermod.load_cache(weather_cache)
    geo_result: geomod.GeoResult | None = None
    last_weather_try = 0.0
    next_paint = 0.0

    log.info(
        "renderer up: driver=%s disk=%s spi=%s",
        panel.display.name, disk_path, host.get("spi_present"),
    )

    while not _stop:
        cfg = cfgmod.load()
        m = collector.sample()

        # Rotation is baked into the display object, so a change from the web UI
        # needs a rebuild. Force a full refresh afterwards: a partial update
        # against a base image drawn the other way up would ghost badly.
        if cfg.rotate != active_rotate:
            log.info("rotate changed %d -> %d, rebuilding display", active_rotate, cfg.rotate)
            try:
                panel.display.close()
            except Exception as exc:
                log.warning("closing display for rebuild failed: %s", exc)
            try:
                panel.display = displaybase.make_display(driver, data, rotate=cfg.rotate)
            except displaybase.DisplayError as exc:
                log.error("display rebuild failed: %s", exc)
                panel.display = displaybase.make_display("null", data)
            active_rotate = cfg.rotate
            panel.last_full = 0.0
            next_paint = 0.0

        if cfg.location.mode == "manual" and cfg.location.lat is not None:
            geo_result = geomod.GeoResult(
                lat=cfg.location.lat,
                lon=cfg.location.lon or 0.0,
                city=cfg.location.label or "Manual",
                timezone=cfg.timezone or (geo_result.timezone if geo_result else "UTC"),
                source="manual",
                fetched_at=time.time(),
            )
        elif geo_result is None:
            geo_result = geomod.resolve(geo_cache, ttl_seconds=int(GEO_TTL))

        now_tz = tzinfo_for(
            cfg.timezone
            or (cached_weather.timezone if cached_weather else None)
            or (geo_result.timezone if geo_result else None)
        )
        now = datetime.now(tz=now_tz)

        if geo_result is not None and (time.time() - last_weather_try) >= WEATHER_INTERVAL:
            last_weather_try = time.time()
            try:
                cached_weather = weathermod.fetch(
                    geo_result.lat, geo_result.lon, units=cfg.units
                )
                weathermod.save_cache(weather_cache, cached_weather)
                log.info(
                    "weather ok: %.1f%s %s",
                    cached_weather.temp, cached_weather.temp_unit,
                    cached_weather.condition,
                )
            except Exception as exc:
                # Stale-while-error: never blank the weather block on a transient
                # 429. Open-Meteo's limiter counts failed requests, so we simply
                # wait out the interval rather than retrying hot.
                log.warning("weather fetch failed, using cache: %s", exc)

        snap = build_snapshot(now, cfg, m, geo_result, cached_weather)

        if time.time() >= next_paint:
            panel.paint(snap)
            next_paint = time.time() + cfg.refresh_seconds

        statemod.publish_json(
            status_path,
            status_payload(now, cfg, m, geo_result, cached_weather,
                           panel.as_dict(next_paint), host, snap),
        )

        # Sleep in short slices so SIGTERM is honoured promptly.
        deadline = time.time() + COLLECT_INTERVAL
        while not _stop and time.time() < deadline:
            time.sleep(min(0.5, deadline - time.time()))

    try:
        panel.display.close()
    except Exception as exc:
        log.warning("display close failed: %s", exc)
    log.info("renderer stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
