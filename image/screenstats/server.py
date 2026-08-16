#!/usr/bin/env python3
"""ScreenStats web UI, config API and umbrelOS widget endpoint.

Stdlib ``http.server`` only: five endpoints do not justify a framework, and the
image already carries Pillow for the renderer -- adding a web stack would double
the layer size for no gain.

HARDWARE RULE: this module must never import ``screenstats.display`` or the
vendored ``waveshare_epd`` package. ``epdconfig`` claims GPIO 17/25/18/24 at
*import* time (not at ``init()``), so a second process importing it takes the
pins away from the renderer with a ``GPIOPinInUse`` error. The server is the
unprivileged half of the app: it reads files the renderer publishes and writes
``config.json``. It touches no hardware, ever.

Run with::

    python3 -m screenstats.server

Environment:
    SCREENSTATS_DATA       data root (default /data), see config.data_dir()
    SCREENSTATS_PORT       listen port (default 8080)
    SCREENSTATS_HOST       bind address (default 0.0.0.0)
    SCREENSTATS_LOG_LEVEL  logging level name (default INFO)
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

try:  # normal case: python3 -m screenstats.server
    from screenstats import config as configmod
    from screenstats import state as statemod
except ImportError:  # direct execution: python3 image/screenstats/server.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from screenstats import config as configmod
    from screenstats import state as statemod

log = logging.getLogger("screenstats.server")

MAX_BODY = 64 * 1024
FULL_EVERY = 6  # renderer paints a full refresh on every 6th tick (PLAN 3)
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


# --------------------------------------------------------------------------- #
# data access
# --------------------------------------------------------------------------- #


def status_path(root: Path) -> Path:
    return root / "state" / "status.json"


def preview_path(root: Path, scale4: bool) -> Path:
    return root / "state" / ("preview-4x.png" if scale4 else "preview.png")


def skeleton() -> dict[str, Any]:
    """A valid, fully-populated status document for "renderer has not run yet".

    Returned verbatim by /api/status when status.json is missing so consumers
    never have to special-case a 500 or a partial object.
    """
    return {
        "schema": 1,
        "updated_at": 0.0,
        "metrics": {
            "cpu": 0.0,
            "ram": 0.0,
            "disk": 0.0,
            "ram_used": 0,
            "ram_total": 0,
            "disk_used": 0,
            "disk_total": 0,
        },
        "weather": None,
        "location": None,
        "clock": "",
        "date": "",
        "greeting": "",
        "panel": {
            "driver": "",
            "last_paint": 0.0,
            "next_paint": 0.0,
            "tick": 0,
            "last_full": 0.0,
            "error": None,
        },
        "host": {
            "spi_present": False,
            "spi_path": "/dev/spidev0.0",
            "gpiochips": [],
            "cpuinfo_raspberry": True,
            "detail": "",
        },
    }


def load_status(root: Path) -> tuple[dict[str, Any], bool]:
    """(status, published) -- falls back to the skeleton, never raises."""
    raw = statemod.read_json(status_path(root))
    if isinstance(raw, dict) and raw:
        return raw, True
    return skeleton(), False


def _sub(status: dict[str, Any], key: str) -> dict[str, Any]:
    v = status.get(key)
    return v if isinstance(v, dict) else {}


def _f(d: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(d.get(key, default))
    except (TypeError, ValueError):
        return default


def _i(d: dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(d.get(key, default))
    except (TypeError, ValueError):
        return default


def _s(d: dict[str, Any], key: str, default: str = "") -> str:
    v = d.get(key, default)
    return v if isinstance(v, str) else default


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #


def human_bytes(n: int) -> str:
    if n <= 0:
        return "0 B"
    step = 1024.0
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < step or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= step
    return f"{size:.1f} TiB"


def duration(seconds: float) -> str:
    s = int(max(0.0, seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d {h:02d}h"


def clock_of(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def ago(ts: float, now: float | None = None) -> str:
    if ts <= 0:
        return "never"
    now = time.time() if now is None else now
    delta = now - ts
    if delta < 0:
        return f"{clock_of(ts)} (in {duration(-delta)})"
    return f"{clock_of(ts)} ({duration(delta)} ago)"


def upcoming(ts: float, now: float | None = None) -> str:
    if ts <= 0:
        return "unknown"
    now = time.time() if now is None else now
    if ts <= now:
        return f"{clock_of(ts)} (due now)"
    return f"{clock_of(ts)} (in {duration(ts - now)})"


def pct(frac: float) -> str:
    return f"{min(1.0, max(0.0, frac)) * 100:.0f}%"


def deg(value: float) -> str:
    return f"{value:.0f}\u00b0"


# --------------------------------------------------------------------------- #
# umbrelOS widget
# --------------------------------------------------------------------------- #


def widget_payload(status: dict[str, Any], published: bool) -> dict[str, Any]:
    """`four-stats` payload.

    Two hard umbreld requirements (widgets/routes.ts): exactly four items, and
    a non-empty `refresh` string -- routes.ts calls ms(refresh) unconditionally
    and ms() throws on undefined, which fails the entire widget query. So items
    degrade to an em dash instead of being dropped.
    """
    metrics = _sub(status, "metrics")
    weather = status.get("weather")
    items: list[dict[str, str]] = []
    for title, key in (("CPU", "cpu"), ("RAM", "ram"), ("Disk", "disk")):
        items.append(
            {"title": title, "text": pct(_f(metrics, key)) if published else "\u2014"}
        )
    if isinstance(weather, dict):
        item = {
            "title": "Weather",
            "text": deg(_f(weather, "temp")),
            "subtext": _s(weather, "condition") or "\u2014",
        }
        if weather.get("stale"):
            item["subtext"] = f"{item['subtext']} (stale)"
    else:
        item = {"title": "Weather", "text": "\u2014", "subtext": "No data"}
    items.append(item)
    return {"type": "four-stats", "link": "", "refresh": "30s", "items": items}


# --------------------------------------------------------------------------- #
# config form <-> config dict
# --------------------------------------------------------------------------- #


def _first(form: dict[str, list[str]], key: str) -> str | None:
    """Last value wins: the hidden/checkbox pair posts the field twice."""
    vals = form.get(key)
    if not vals:
        return None
    return vals[-1].strip()


def form_to_raw(form: dict[str, list[str]]) -> dict[str, Any]:
    """Translate the HTML form into the nested config.json shape.

    Only keys actually present are emitted; the caller merges over the current
    config so a partial post never silently resets unrelated fields. Malformed
    numbers are dropped (keeping the old value) rather than reset to defaults.
    """
    raw: dict[str, Any] = {}

    refresh = _first(form, "refresh_seconds")
    if refresh:
        try:
            raw["refresh_seconds"] = int(float(refresh))
        except ValueError:
            pass

    units = _first(form, "units")
    if units in ("metric", "imperial"):
        raw["units"] = units

    clock = _first(form, "clock_24h")
    if clock is not None:
        raw["clock_24h"] = clock in ("1", "true", "on", "yes")

    rotate = _first(form, "rotate")
    if rotate is not None:
        try:
            raw["rotate"] = int(rotate)
        except ValueError:
            pass

    loc: dict[str, Any] = {}
    mode = _first(form, "location_mode")
    if mode in ("auto", "manual"):
        loc["mode"] = mode
    for field, name in (("lat", "location_lat"), ("lon", "location_lon")):
        val = _first(form, name)
        if val is None:
            continue
        if val == "":
            loc[field] = None
        else:
            try:
                loc[field] = float(val)
            except ValueError:
                pass
    label = _first(form, "location_label")
    if label is not None:
        loc["label"] = label or None
    if loc:
        raw["location"] = loc

    tz = _first(form, "timezone")
    if tz is not None:
        raw["timezone"] = tz or None

    if "quiet_start" in form or "quiet_end" in form:
        start = _first(form, "quiet_start") or ""
        end = _first(form, "quiet_end") or ""
        if _HHMM.match(start) and _HHMM.match(end):
            raw["quiet_hours"] = [start, end]
        else:
            raw["quiet_hours"] = None

    return raw


def merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, val in over.items():
        cur = out.get(key)
        if isinstance(val, dict) and isinstance(cur, dict):
            out[key] = merge(cur, val)
        else:
            out[key] = val
    return out


def apply_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Merge, clamp and persist. config.from_dict owns the 180 s hard floor."""
    current = configmod.to_dict(configmod.load())
    cfg = configmod.from_dict(merge(current, raw))
    configmod.save(cfg)
    return configmod.to_dict(cfg)


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin:0; background:#0e1116; color:#e6edf3;
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
.wrap { max-width:840px; margin:0 auto; padding:22px 16px 72px; }
h1 { font-size:21px; margin:0 0 2px; }
h2 { font-size:16px; margin:0 0 10px; }
.sub { color:#8b949e; font-size:13px; margin:0 0 18px; }
.card { background:#161b22; border:1px solid #30363d; border-radius:10px;
  padding:16px; margin:0 0 16px; }
.alert { border-color:#f85149; background:#2a1214; }
.alert h2 { color:#ff7b72; font-size:18px; }
.good { border-color:#2ea043; background:#10261a; }
.good h2 { color:#56d364; }
.saved { border-color:#2ea043; background:#10261a; color:#56d364; padding:10px 16px; }
code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; background:#0b0f14;
  border:1px solid #30363d; border-radius:4px; padding:1px 5px; }
pre { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; background:#0b0f14;
  border:1px solid #30363d; border-radius:6px; padding:10px 12px; overflow-x:auto;
  margin:10px 0; font-size:14px; }
ol, ul { margin:10px 0; padding-left:22px; }
li { margin:4px 0; }
table { width:100%; border-collapse:collapse; }
td, th { text-align:left; padding:4px 10px 4px 0; vertical-align:top; font-weight:400; }
th { color:#8b949e; white-space:nowrap; width:44%; }
.bar { height:8px; background:#21262d; border-radius:4px; overflow:hidden; margin-top:5px; }
.bar > span { display:block; height:100%; background:#58a6ff; }
.preview { display:block; width:100%; max-width:500px; image-rendering:pixelated;
  background:#fff; border:1px solid #30363d; border-radius:6px; }
.empty { padding:28px 12px; text-align:center; color:#8b949e; border:1px dashed #30363d;
  border-radius:6px; }
label { display:block; margin:14px 0 4px; color:#8b949e; font-size:13px; }
input, select { width:100%; padding:9px 10px; background:#0d1117; color:#e6edf3;
  border:1px solid #30363d; border-radius:6px; font:inherit; }
.row { display:flex; gap:12px; flex-wrap:wrap; }
.row > div { flex:1 1 170px; }
.hint { color:#8b949e; font-size:12px; margin-top:5px; }
.chk { display:flex; align-items:center; gap:9px; margin-top:14px; }
.chk input { width:auto; }
.chk span { color:#e6edf3; font-size:15px; }
button { margin-top:20px; padding:10px 20px; background:#238636; border:1px solid #2ea043;
  color:#fff; border-radius:6px; font:inherit; font-weight:600; cursor:pointer; }
a { color:#58a6ff; }
.stale { color:#d29922; font-weight:600; }
.err { color:#ff7b72; }
.dim { color:#8b949e; }
footer { color:#8b949e; font-size:12px; margin-top:8px; }
@media (max-width:520px) { .wrap { padding:14px 12px 56px; } th { width:50%; } }
"""

# Identify the boot partition by SIZE AND CONTENTS, never by label. Verified on
# umbrelOS 1.7.x: neither FAT partition carries a filesystem label, and
# /boot/firmware/config.txt on the running rootfs is a build-time staging copy
# that the Pi firmware never reads. Telling people to "edit BOOT-A" or to edit
# /boot/firmware/config.txt sends them to a file that has no effect.
SPI_STEPS = (
    "Shut the Pi down completely and unplug it.",
    "Take the microSD card out and put it in another computer "
    "(a Pi 4B boots from microSD only, so the card holds the boot files).",
    "Two unlabelled FAT volumes appear. Open the <strong>128&nbsp;MiB</strong> one "
    "&mdash; the volume containing <code>config.txt</code> and a folder named "
    "<code>overlays</code>. Ignore the 256&nbsp;MiB volume that holds "
    "<code>autoboot.txt</code>; it has no <code>config.txt</code>.",
    "Append this line on its own at the end of <code>config.txt</code>:",
    "Save, eject, put the card back in the Pi and power it on.",
)

SPI_DECOY_WARNING = (
    "Do <strong>not</strong> edit <code>/boot/firmware/config.txt</code> over SSH. "
    "That file exists on the running system and looks correct, but it is a "
    "build-time staging copy and the Pi firmware never reads it. The real file "
    "lives on an unmounted boot partition."
)


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _sel(active: bool) -> str:
    return " selected" if active else ""


def _row(key: str, value: str) -> str:
    return f"<tr><th>{key}</th><td>{value}</td></tr>"


def _gauge(title: str, frac: float, used: int, total: int, sizes: bool = True) -> str:
    width = min(100.0, max(0.0, frac * 100.0))
    detail = ""
    if sizes:
        if total > 0:
            detail = " &middot; " + human_bytes(used) + " / " + human_bytes(total)
        else:
            detail = ' &middot; <span class="dim">size unknown</span>'
    return (
        f"<tr><th>{title}</th><td><b>{pct(frac)}</b>{detail}"
        f'<div class="bar"><span style="width:{width:.1f}%"></span></div></td></tr>'
    )


def render_host_card(host: dict[str, Any], published: bool) -> str:
    """The SPI banner. On a stock umbrelOS Pi this is the app's whole job."""
    spi_ok = bool(host.get("spi_present"))
    spi_path = _s(host, "spi_path", "/dev/spidev0.0") or "/dev/spidev0.0"
    chips = host.get("gpiochips")
    chips_txt = ", ".join(str(c) for c in chips) if isinstance(chips, list) and chips else "none found"
    detail = _s(host, "detail")
    facts = (
        "<table>"
        + _row("SPI device", f"<code>{_esc(spi_path)}</code> \u2014 "
               + ("present" if spi_ok else '<span class="err">missing</span>'))
        + _row("GPIO chips", _esc(chips_txt))
        + _row(
            "Raspberry Pi detected",
            "yes" if host.get("cpuinfo_raspberry") else '<span class="err">no</span>',
        )
        + (_row("Probe detail", _esc(detail)) if detail else "")
        + "</table>"
    )

    if spi_ok:
        return (
            '<section class="card good"><h2>SPI is enabled &mdash; panel reachable</h2>'
            f"<p>The kernel exposes <code>{_esc(spi_path)}</code>, so the renderer can "
            "drive the e-Paper HAT.</p>" + facts + "</section>"
        )

    steps = "".join(
        f"<li>{step}<pre>dtparam=spi=on</pre></li>" if i == 3 else f"<li>{step}</li>"
        for i, step in enumerate(SPI_STEPS)
    )
    not_yet = (
        "<p><b>The renderer has not reported yet</b>, so this is the last known state "
        "(none). If the app has only just started, give it a few seconds and reload.</p>"
        if not published
        else ""
    )
    return (
        '<section class="card alert">'
        "<h2>&#9888; SPI is not enabled &mdash; the display cannot work yet</h2>"
        "<p><b>umbrelOS does not turn SPI on.</b> Its Raspberry Pi boot configuration "
        "enables the I&sup2;C bus and nothing else, so <code>/dev/spidev0.0</code> "
        "never appears and no container &mdash; including this one &mdash; can talk to "
        "the e-Paper HAT. Nothing will be drawn on the panel until you fix this on the "
        "host. It takes about five minutes.</p>"
        f"{not_yet}"
        f"<p>{SPI_DECOY_WARNING}</p>"
        "<h3>Fix it (Raspberry Pi 4 Model B)</h3>"
        f"<ol>{steps}</ol>"
        "<p>After the reboot this page turns green and the panel starts painting.</p>"
        "<h3>Or do it over SSH</h3>"
        "<p>The repo ships <code>tools/enable-spi.sh</code>, which resolves the active "
        "boot slot from <code>autoboot.txt</code> instead of guessing, reports before it "
        "writes, backs up <code>config.txt</code>, and is safe to run twice:</p>"
        "<pre>scp tools/enable-spi.sh umbrel@&lt;pi&gt;:/tmp/\n"
        "ssh umbrel@&lt;pi&gt; 'sudo sh /tmp/enable-spi.sh --check'\n"
        "ssh umbrel@&lt;pi&gt; 'sudo sh /tmp/enable-spi.sh'\n"
        "ssh umbrel@&lt;pi&gt; 'sudo reboot'</pre>"
        "<h3>An umbrelOS update can undo this</h3>"
        "<p>umbrelOS updates install a whole new boot slot (the image keeps two, "
        "<code>boot-a</code> and <code>boot-b</code>, and switches between them). Only "
        "the data partition is carried across, so your edit to <code>config.txt</code> "
        "lives in one slot and is <b>not</b> copied into the next one. If the panel goes "
        "quiet after an umbrelOS update, come back to this page &mdash; if the warning "
        "is back, repeat the steps above.</p>" + facts + "</section>"
    )


def render_panel_card(panel: dict[str, Any], published: bool) -> str:
    tick = _i(panel, "tick")
    error = panel.get("error")
    last_paint = _f(panel, "last_paint")
    last_full = _f(panel, "last_full")
    # Waveshare forbids more than 5 consecutive partial refreshes, so the
    # renderer forces a full one on every 6th tick (and after 23 h idle). It
    # stamps last_full == last_paint when the last paint was a full refresh,
    # which is more truthful than assuming the plain tick cadence.
    if last_full > 0.0 and last_full >= last_paint:
        partials = 0
    else:
        partials = tick % FULL_EVERY if tick > 0 else 0
    rows = (
        _row("Driver", _esc(_s(panel, "driver") or "\u2014"))
        + _row("Last paint", _esc(ago(last_paint)))
        + _row("Next paint", _esc(upcoming(_f(panel, "next_paint"))))
        + _row("Tick", str(tick))
        + _row(
            "Partials since full",
            f"{partials} / {FULL_EVERY - 1}"
            ' <span class="dim">(a full refresh is forced on every 6th tick)</span>',
        )
        + _row("Last full refresh", _esc(ago(last_full)))
        + _row(
            "Error",
            f'<span class="err">{_esc(error)}</span>' if error else '<span class="dim">none</span>',
        )
    )
    head = "" if published else '<p class="dim">Awaiting the first render.</p>'
    return f'<section class="card"><h2>Panel</h2>{head}<table>{rows}</table></section>'


def render_metrics_card(metrics: dict[str, Any], published: bool) -> str:
    if not published:
        return (
            '<section class="card"><h2>Host</h2>'
            '<p class="dim">Awaiting the first sample from the renderer.</p></section>'
        )
    rows = (
        _gauge("CPU", _f(metrics, "cpu"), 0, 0, sizes=False)
        + _gauge("RAM", _f(metrics, "ram"), _i(metrics, "ram_used"), _i(metrics, "ram_total"))
        + _gauge("Disk", _f(metrics, "disk"), _i(metrics, "disk_used"), _i(metrics, "disk_total"))
    )
    return f'<section class="card"><h2>Host</h2><table>{rows}</table></section>'


def render_weather_card(
    status: dict[str, Any], cfg: configmod.Config
) -> str:
    weather = status.get("weather")
    location = status.get("location")
    attribution = (
        '<footer>Weather data by '
        '<a href="https://open-meteo.com/" rel="noopener" target="_blank">Open-Meteo.com</a>'
        " (CC BY 4.0). Location by ip-api.com.</footer>"
    )
    if not isinstance(weather, dict):
        return (
            '<section class="card"><h2>Weather</h2>'
            '<p class="dim">No weather data yet.</p>' + attribution + "</section>"
        )
    unit = _s(weather, "unit") or configmod.temp_unit(cfg)
    stale = bool(weather.get("stale"))
    temp = _esc(f"{_f(weather, 'temp'):.1f}{unit}")
    feels = _esc(f"{_f(weather, 'feels'):.1f}{unit}")
    lo = _esc(f"{_f(weather, 'lo'):.1f}{unit}")
    hi = _esc(f"{_f(weather, 'hi'):.1f}{unit}")
    condition = _esc(_s(weather, "condition") or "\u2014")
    freshness = (
        '<span class="stale">&#9888; STALE &mdash; showing the last cached reading</span>'
        if stale
        else '<span class="dim">current</span>'
    )
    rows = (
        _row("Now", f"<b>{temp}</b> &middot; {condition}")
        + _row("Feels like", feels)
        + _row("Today", f"{lo} / {hi}")
        + _row("Humidity", f"{_i(weather, 'humidity')}%")
        + _row("WMO code", str(_i(weather, "code")))
        + _row("Fetched", _esc(ago(_f(weather, "fetched_at"))))
        + _row("Freshness", freshness)
    )
    if isinstance(location, dict):
        rows += _row(
            "Location",
            _esc(
                " \u00b7 ".join(
                    x
                    for x in (
                        _s(location, "city"),
                        _s(location, "timezone"),
                        _s(location, "source"),
                    )
                    if x
                )
                or "\u2014"
            ),
        )
    return f'<section class="card"><h2>Weather</h2><table>{rows}</table>{attribution}</section>'


def render_preview_card(root: Path) -> str:
    path = preview_path(root, scale4=True)
    try:
        stamp = int(path.stat().st_mtime)
    except OSError:
        return (
            '<section class="card"><h2>Panel preview</h2>'
            '<div class="empty">No frame has been rendered yet.<br>'
            "The preview appears here after the renderer\u2019s first paint.</div>"
            "</section>"
        )
    return (
        '<section class="card"><h2>Panel preview</h2>'
        f'<img class="preview" id="preview" alt="last frame pushed to the e-Paper panel" '
        f'src="/preview-4x.png?t={stamp}">'
        f'<footer>250&times;122, 1-bit &middot; updated {_esc(ago(float(stamp)))} &middot; '
        '<a href="/preview.png">1&times; PNG</a></footer></section>'
    )


def render_form(cfg: configmod.Config) -> str:
    loc = cfg.location
    quiet = cfg.quiet_hours or ("", "")
    lat = "" if loc.lat is None else f"{loc.lat:.6f}"
    lon = "" if loc.lon is None else f"{loc.lon:.6f}"
    return f"""<section class="card"><h2>Settings</h2>
<form method="post" action="/api/config">
  <label for="refresh_seconds">Refresh interval (seconds)</label>
  <input id="refresh_seconds" name="refresh_seconds" type="number" min="{configmod.REFRESH_FLOOR_SECONDS}"
         step="10" required value="{_esc(cfg.refresh_seconds)}">
  <div class="hint">Waveshare requires at least
    {configmod.REFRESH_FLOOR_SECONDS}&nbsp;s between refreshes and at least one refresh
    every 24&nbsp;hours. Faster refreshing permanently damages the panel, so anything
    lower is clamped to {configmod.REFRESH_FLOOR_SECONDS}&nbsp;s.</div>

  <div class="row">
    <div>
      <label for="units">Units</label>
      <select id="units" name="units">
        <option value="metric"{_sel(cfg.units == "metric")}>Metric (&deg;C, km/h)</option>
        <option value="imperial"{_sel(cfg.units == "imperial")}>Imperial (&deg;F, mph)</option>
      </select>
    </div>
    <div>
      <label for="rotate">Panel rotation</label>
      <select id="rotate" name="rotate">
        <option value="0"{_sel(cfg.rotate == 0)}>0&deg;</option>
        <option value="180"{_sel(cfg.rotate == 180)}>180&deg; (upside down)</option>
      </select>
    </div>
  </div>

  <input type="hidden" name="clock_24h" value="0">
  <label class="chk"><input type="checkbox" name="clock_24h" value="1"
    {"checked" if cfg.clock_24h else ""}><span>24-hour clock</span></label>

  <label for="location_mode">Location</label>
  <select id="location_mode" name="location_mode">
    <option value="auto"{_sel(loc.mode != "manual")}>Automatic (from public IP)</option>
    <option value="manual"{_sel(loc.mode == "manual")}>Manual coordinates</option>
  </select>
  <div class="hint">IP geolocation is wrong often enough (VPN, CGNAT) that a manual
    override matters. Manual mode uses the latitude and longitude below.</div>

  <div class="row">
    <div>
      <label for="location_lat">Latitude</label>
      <input id="location_lat" name="location_lat" type="text" inputmode="decimal"
             placeholder="-37.8136" value="{_esc(lat)}">
    </div>
    <div>
      <label for="location_lon">Longitude</label>
      <input id="location_lon" name="location_lon" type="text" inputmode="decimal"
             placeholder="144.9631" value="{_esc(lon)}">
    </div>
    <div>
      <label for="location_label">Label shown on the panel</label>
      <input id="location_label" name="location_label" type="text"
             placeholder="Melbourne" value="{_esc(loc.label or "")}">
    </div>
  </div>

  <label for="timezone">Timezone override (IANA name)</label>
  <input id="timezone" name="timezone" type="text" placeholder="Australia/Melbourne"
         value="{_esc(cfg.timezone or "")}">
  <div class="hint">Leave empty to follow the location lookup. Always an IANA
    identifier such as <code>Europe/Berlin</code> &mdash; never a numeric offset,
    which breaks at the next daylight-saving change.</div>

  <div class="row">
    <div>
      <label for="quiet_start">Quiet hours start</label>
      <input id="quiet_start" name="quiet_start" type="time" value="{_esc(quiet[0])}">
    </div>
    <div>
      <label for="quiet_end">Quiet hours end</label>
      <input id="quiet_end" name="quiet_end" type="time" value="{_esc(quiet[1])}">
    </div>
  </div>
  <div class="hint">Skip repainting overnight. Leave both empty to paint around the
    clock. The panel is still refreshed once within any 24-hour window, because going
    longer than that causes permanent ghosting.</div>

  <button type="submit">Save settings</button>
</form></section>"""


def render_page(root: Path, saved: bool) -> str:
    status, published = load_status(root)
    cfg = configmod.load()
    updated = _f(status, "updated_at")
    clock = _s(status, "clock")
    date = _s(status, "date")
    greeting = _s(status, "greeting")
    head_bits = " \u00b7 ".join(x for x in (greeting, clock, date) if x)
    banner = (
        '<section class="card saved">Settings saved. The renderer applies them on its '
        "next tick.</section>"
        if saved
        else ""
    )
    state_line = (
        f"Last update {_esc(ago(updated))}"
        if published and updated > 0
        else "Awaiting the first render \u2014 the renderer has not published a status yet."
    )
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ScreenStats</title>
<style>{CSS}</style>
</head><body><div class="wrap">
<h1>ScreenStats</h1>
<p class="sub">Waveshare 2.13&Prime; e-Paper HAT V4 &middot; {_esc(head_bits) or "&mdash;"}<br>{state_line}</p>
{banner}
{render_host_card(_sub(status, "host"), published)}
{render_preview_card(root)}
{render_panel_card(_sub(status, "panel"), published)}
{render_metrics_card(_sub(status, "metrics"), published)}
{render_weather_card(status, cfg)}
{render_form(cfg)}
<footer>Data directory <code>{_esc(root)}</code> &middot;
<a href="/api/status">/api/status</a> &middot;
<a href="/api/config">/api/config</a> &middot;
<a href="/widgets/status">/widgets/status</a> &middot;
<a href="/">reload</a></footer>
</div>
<script>
// Progressive enhancement only: the page is fully usable with JS disabled.
setInterval(function () {{
  var img = document.getElementById("preview");
  if (img) {{ img.src = "/preview-4x.png?t=" + Date.now(); }}
}}, 30000);
</script>
</body></html>"""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


class Handler(BaseHTTPRequestHandler):
    server_version = "ScreenStats"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # --- plumbing ---------------------------------------------------------- #

    @property
    def root(self) -> Path:
        return self.server.data_dir  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        """Suppress BaseHTTPRequestHandler's stderr access log.

        One structured line per request is emitted by _serve() instead.
        """

    def _send(
        self,
        code: int,
        ctype: str,
        body: bytes,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._code = code
        self._sent = True
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, val in (headers or {}).items():
            self.send_header(key, val)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, "application/json; charset=utf-8", json.dumps(obj, indent=2).encode())

    def _text(self, code: int, message: str) -> None:
        self._send(code, "text/plain; charset=utf-8", (message + "\n").encode())

    def _html(self, markup: str, code: int = 200) -> None:
        self._send(code, "text/html; charset=utf-8", markup.encode())

    def _serve(self, method: str, route: Callable[[str, dict[str, list[str]]], None]) -> None:
        started = time.monotonic()
        self._code = 500
        self._sent = False
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        try:
            route(parsed.path, query)
        except (BrokenPipeError, ConnectionResetError):
            self._code = 499
        except Exception:  # never let a bad status.json take the server down
            log.exception("unhandled error serving %s %s", method, self.path)
            if not self._sent:
                self._text(500, "internal error")
        finally:
            log.info(
                "%s %s %s %s %.1fms",
                self.client_address[0] if self.client_address else "-",
                method,
                self.path,
                self._code,
                (time.monotonic() - started) * 1000.0,
            )

    def _preview(self, scale4: bool) -> None:
        path = preview_path(self.root, scale4)
        try:
            blob = path.read_bytes()
        except OSError:
            self._text(
                404,
                "No frame has been rendered yet. The renderer publishes "
                f"{path.name} after its first paint; open the status page at / "
                "to see why the panel has not painted.",
            )
            return
        self._send(200, "image/png", blob)

    # --- routes ------------------------------------------------------------ #

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._serve("GET", self._route_get)

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve("HEAD", self._route_get)

    def do_POST(self) -> None:  # noqa: N802
        self._serve("POST", self._route_post)

    def _route_get(self, path: str, query: dict[str, list[str]]) -> None:
        if path in ("/", "/index.html"):
            self._html(render_page(self.root, saved=bool(query.get("saved"))))
        elif path == "/preview.png":
            self._preview(scale4=False)
        elif path == "/preview-4x.png":
            self._preview(scale4=True)
        elif path == "/api/status":
            status, _ = load_status(self.root)
            self._json(status)
        elif path == "/api/config":
            self._json(configmod.to_dict(configmod.load()))
        elif path == "/widgets/status":
            status, published = load_status(self.root)
            self._json(widget_payload(status, published))
        elif path == "/healthz":
            self._text(200, "ok")
        elif path == "/favicon.ico":
            self._send(204, "image/x-icon", b"")
        else:
            self._text(404, f"No such page: {path}. Try / for the status page.")

    def _route_post(self, path: str, query: dict[str, list[str]]) -> None:
        if path != "/api/config":
            self._text(404, f"No such endpoint: {path}. Config posts go to /api/config.")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self._text(413, "Request body missing or too large.")
            return
        body = self.rfile.read(length) if length else b""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()

        if ctype == "application/json":
            try:
                parsed = json.loads(body.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._json({"error": f"invalid JSON: {exc}"}, code=400)
                return
            if not isinstance(parsed, dict):
                self._json({"error": "expected a JSON object"}, code=400)
                return
            saved = apply_config(parsed)
            log.info("config saved via JSON: %s", saved)
            self._json(saved)
            return

        form = urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
        saved = apply_config(form_to_raw(form))
        log.info("config saved via form: %s", saved)
        self._send(303, "text/plain; charset=utf-8", b"", headers={"Location": "/?saved=1"})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], data_dir: Path) -> None:
        self.data_dir = data_dir
        super().__init__(address, Handler)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("SCREENSTATS_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    root = configmod.data_dir()
    try:
        statemod.ensure_dirs(root)
    except OSError as exc:
        log.warning("cannot create %s: %s", root, exc)
    host = os.environ.get("SCREENSTATS_HOST", "0.0.0.0")
    port = int(os.environ.get("SCREENSTATS_PORT", "8080"))
    httpd = Server((host, port), root)
    log.info("ScreenStats server listening on %s:%d (data=%s)", host, port, root)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
