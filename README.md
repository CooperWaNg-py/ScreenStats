# ScreenStats

An Umbrel community-app-store app that drives a **Waveshare 2.13" e-Paper HAT V4**
(250x122, 1-bit) attached to the Raspberry Pi that runs umbrelOS. The panel shows
the time, a time-of-day greeting, host CPU / RAM / disk usage, and current weather
for the location resolved from your public IP.

- Store id: `screenstats` · App id: `screenstats-eink` · Panel driver: `epd2in13_V4`
- Target: umbrelOS on a **Raspberry Pi 4 Model B** (board type `0x11`, microSD boot)
- No API keys, no accounts.

The full design, with sources for every non-obvious claim, is in [`PLAN.md`](PLAN.md).

## Prerequisite: enable SPI on the host

umbrelOS's Raspberry Pi boot config enables I²C and **not** SPI. There is no
`dtparam=spi=on` anywhere in the umbrelOS image, so **`/dev/spidev0.0` does not
exist on a stock install** and no container can talk to the HAT. This is a host
change; the app cannot do it for you, and it needs a reboot.

The app installs and runs without it — the web UI, host metrics, weather and the
umbrelOS widget all work — but the panel stays blank and the status page says why.

### Fix it over SSH (recommended)

`tools/enable-spi.sh` resolves the active boot slot from `autoboot.txt` rather
than guessing, checks before it writes, backs up `config.txt`, and is idempotent:

```bash
scp tools/enable-spi.sh umbrel@<pi>:/tmp/
ssh umbrel@<pi> 'sudo sh /tmp/enable-spi.sh --check'   # read-only report
ssh umbrel@<pi> 'sudo sh /tmp/enable-spi.sh'           # apply
ssh umbrel@<pi> 'sudo reboot'
```

Verify afterwards:

```bash
ls -l /dev/spidev0.0
cat /proc/device-tree/soc/spi@7e204000/status   # -> okay
```

### Or edit the microSD card offline

Two traps make this easy to get wrong. Both have bitten us:

- **`/boot/firmware/config.txt` on the running system is a decoy.** It exists, it
  looks exactly like the file you want, and the Pi firmware never reads it — it is
  a build-time staging copy left in the root filesystem. Editing it does nothing.
- **The boot partitions have no filesystem label.** Verified on umbrelOS 1.7.x:
  `lsblk -o NAME,SIZE,FSTYPE,LABEL,PARTLABEL` shows both FAT partitions with an
  empty label. Advice elsewhere (including an earlier version of this file) that
  says "edit the volume named `BOOT-A`" is wrong for this image.

So identify the partition **by size and contents**, not by name. Inserting the
card surfaces two unlabelled FAT volumes:

| partition | size | contains | is this the one? |
|---|---|---|---|
| `p1` | **256 MiB** | `autoboot.txt`, `pieeprom.upd`, `vl805.bin` | ✗ no `config.txt` here |
| `p2` | **128 MiB** | `config.txt`, `cmdline.txt`, `*.dtb`, `overlays/` | ✓ **this one** |

1. Shut down Umbrel (Settings → Shut Down) and unplug the Pi.
2. Move the microSD to another machine. Open the **128 MiB** FAT volume — the one
   that contains `config.txt` and a folder named `overlays`.
3. Append a line to `config.txt`:

   ```
   dtparam=spi=on
   ```

4. Save, eject, reseat the card, power on.
5. The ScreenStats status page should now report `/dev/spidev0.0` present.

If your Pi boots the standby slot (`boot_partition=3` in `p1`'s `autoboot.txt`),
the 128 MiB partition to edit is `p3` instead. The SSH script handles this for you.

### What does and does not survive a reboot on umbrelOS

Learned the hard way on a real device. umbrelOS persists **only `/data`**
(`/etc/rugix/state/data.toml` contains exactly `[[persist]] directory = "/data"`).
The root filesystem is A/B managed and reset at boot.

| change | survives reboot? | why |
|---|---|---|
| `dtparam=spi=on` in `config.txt` | ✅ yes | lives on the boot partition, not the rootfs |
| `usermod -aG docker umbrel` | ❌ **no** | `/etc/group` is on the rootfs and is reverted |
| `/etc/sudoers.d/...` | ❌ **no** | same |
| installed Umbrel apps | ✅ yes | umbreld state lives under `/data` and it restarts them |

Two consequences:

1. **Do not rely on host account tweaks.** `tools/dev-stack.yml` needs the invoking
   user to reach the Docker socket, so it stops working after a reboot. It is a
   bring-up tool, not a deployment.
2. **Install as a real Umbrel app for anything permanent.** umbreld runs Docker as
   root, needs no group membership, and auto-starts the app after a reboot.

Also prefer `http://umbrel.local` over an IP: umbrelOS takes its address from DHCP
and it does change.

### If `umbrel.local` does not resolve

Check what avahi is actually advertising:

```bash
pgrep -a avahi-daemon      # -> "avahi-daemon: running [umbrel-3.local]"
```

A name like `umbrel-3.local` means avahi hit a name conflict and renamed itself.
The usual cause on a Pi is being connected to **Ethernet and Wi-Fi at the same
time on the same subnet**:

```bash
ip -4 addr show scope global | grep -E "^[0-9]+:|inet "
#   end0  ... inet 192.168.0.39/24
#   wlan0 ... inet 192.168.0.34/24     <- two addresses, one LAN
```

Avahi announces `umbrel.local` on one interface, receives its own announcement on
the other, calls it a conflict, and appends a counter. Fixes, best first:

1. **Use one interface.** Turn off Wi-Fi when Ethernet is plugged in (or unplug
   Ethernet). Restart avahi, or reboot, and it reclaims `umbrel.local`.
2. Use the advertised name (`umbrel-3.local`) or the IP in the meantime.
3. Pinning `allow-interfaces=` in `/etc/avahi/avahi-daemon.conf` works but does
   **not** survive a reboot, because umbrelOS resets everything outside `/data`.

The counter increments on each conflict, so the name is not stable -- fix the
dual-homing rather than hardcoding `umbrel-3.local` anywhere.


### An OTA update can revert it

umbrelOS updates install a whole new boot slot and switch to it; only `/data` is
declared persistent. A `config.txt` edit therefore lives in one slot and is not
carried into the next update's slot. If the panel goes blank after an umbrelOS
update, re-apply the edit.

The app's `hooks/pre-start` hook runs on the host on every app start. **By default
it is strictly read-only**: it probes SPI and writes
`${APP_DATA_DIR}/data/state/host-spi.json`, which the status page renders. It
never touches the boot partition unless you explicitly opt in by creating the
marker file:

```bash
touch ~/umbrel/app-data/screenstats-eink/data/enable-spi-autofix
```

With the marker present, the hook backs `config.txt` up to
`config.txt.screenstats-bak` and appends `dtparam=spi=on` if missing, re-applying
itself after an OTA update. It is opt-in because editing a live A/B boot slot from
an app hook can leave a device unbootable, and umbreld swallows hook failures
(`"${hook}" || true`), so a partial write would be invisible.

## Install

1. In umbrelOS, open the **App Store**, then the **⋮** menu → **Community App
   Stores**.
2. Add this repository URL:

   ```
   https://github.com/CooperWaNg-py/ScreenStats
   ```

3. Install **ScreenStats** from the ScreenStats store that appears.
4. Enable SPI as above and reboot if you have not already.

The app exposes its UI on port `4180` via `app_proxy` and adds a `four-stats`
home-screen widget (CPU / RAM / Disk / Weather, refreshed every 30 s).

## The clock updates every 5 minutes, not every second

That is a hardware limit, not a shortcut. Waveshare's
[2.13inch e-Paper HAT Manual](https://www.waveshare.com/wiki/2.13inch_e-Paper_HAT_Manual)
precautions state that the refresh interval should be **at least 180 s**, that the
panel must be put to sleep after every refresh (otherwise it sits at high voltage
and is *"damaged and cannot be repaired"*), that no more than **5 consecutive
partial refreshes** may occur before a full one, and that it must be refreshed at
least once every 24 h to avoid image sticking.

So: `refresh_seconds` defaults to 300, is **clamped to a 180 s floor in code**, the
panel is slept after every paint, and every 6th tick is a full refresh. The
umbrelOS widget and the web UI update every 10 s — only the panel is slow.

## Local development (no hardware)

Everything except the panel runs on x86/arm64 Linux and macOS with no HAT
attached. Select the `png` display driver, which writes `state/preview.png`
instead of pushing to SPI:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install Pillow                      # spidev/gpiozero/lgpio are Linux-only

export SCREENSTATS_DATA=/tmp/screenstats
export SCREENSTATS_DRIVER=png           # epd | png | null
export SCREENSTATS_DISK_PATH=/
mkdir -p "$SCREENSTATS_DATA"/{cache,state}

PYTHONPATH=image:image/vendor python3 -m screenstats.renderer &
PYTHONPATH=image:image/vendor python3 -m screenstats.server    # http://localhost:8080
```

### Layout preview

Iterate on the frame with the standalone harness — never on the panel, since the
180 s floor makes that unusable and harmful. It renders the exact 250x122 1-bit
frame, asserts the rotated buffer is 4000 bytes, and asserts no ink falls
off-panel:

```bash
python3 tools/layout_preview.py /tmp/p.png --case nominal    # nominal | evening | overflow
```

### Building the image

The image must be a multi-arch manifest list; umbrelOS runs on `linux/amd64` and
`linux/arm64` and the app linter rejects single-arch images:

The image is published automatically by `.github/workflows/publish-image.yml` on
every push that touches `image/`. Manual equivalent:

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  -t ghcr.io/cooperwang-py/screenstats:0.1.0 --push image/
docker buildx imagetools inspect ghcr.io/cooperwang-py/screenstats:0.1.0
```

Copy the reported **index** digest into `screenstats-eink/docker-compose.yml`.

## Attribution and data sources

- Weather data by [Open-Meteo.com](https://open-meteo.com/), licensed
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
- Geolocation by [ip-api.com](https://ip-api.com/), with
  [ipwho.is](https://ipwho.is/) as a fallback. Coordinates can be overridden
  manually in the app's settings.
- The Waveshare `epd2in13_V4` driver is vendored under `image/vendor/waveshare_epd/`
  at a pinned upstream commit, MIT licensed, headers retained.
