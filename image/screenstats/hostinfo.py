"""Host identification for the SPI/GPIO path. Stdlib only, no Pillow.

Kept dependency-free on purpose so it can be run directly on a target Pi as a
diagnostic before anything else is installed:

    python3 -m screenstats.hostinfo

Why this module exists
----------------------
gpiozero's LGPIOFactory picks the gpiochip index for us:

    chip = 4 if (revision & 0xff0) >> 4 == 0x17 and exists('/dev/gpiochip4') else 0

On a Pi 4B (board type 0x11) that is always chip 0. If the kernel ever enumerated
a firmware/expander GPIO first, the Waveshare driver would drive the wrong lines
with no visible symptom, so we verify which chip is the 40-pin header.

Reading the label is not as simple as it looks:

  * `/sys/bus/gpio/devices/gpiochipN/label` DOES NOT EXIST on current kernels
    (verified absent on Raspberry Pi OS kernel 6.18.34+rpt-rpi-v8). An earlier
    version of this check read that path and therefore silently verified nothing.
  * `/sys/class/gpio/gpiochip*/label` does exist, but the directory is named after
    the GPIO *base* number (`gpiochip512`, `gpiochip570`), not the chip index, so
    it cannot be mapped back to `/dev/gpiochipN` reliably.
  * `/sys/bus/gpio/devices/gpiochipN/of_node/compatible` IS indexed by chip number
    and is the dependable source. Verified on a Pi 4B:
        gpiochip0 -> gpio@7e200000, compatible 'brcm,bcm2711-gpio'   (header)
        gpiochip1 -> gpio,          compatible 'raspberrypi,firmware-gpio'

Policy: only complain when a chip is *positively identified* as not the header.
Staying silent when undeterminable is deliberate -- a check that cries wolf on
unfamiliar hardware is worse than no check.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path

GPIO_DEVICES = "/sys/bus/gpio/devices"
SPI_PATH = "/dev/spidev0.0"

# Devicetree root. On a host, /proc/device-tree symlinks to
# /sys/firmware/devicetree/base and everything just works.
#
# In a container it does NOT: Docker mounts a fresh sysfs over /sys, which
# silently swallows any bind mount placed under /sys/... (verified: mounting
# /sys/firmware/devicetree into a container leaves the path absent, with no
# error). The /proc/device-tree symlink survives but dangles.
#
# So the devicetree must be bind-mounted somewhere OUTSIDE /sys and pointed at
# with SCREENSTATS_DEVICETREE. compose uses:
#     - /proc/device-tree:/host-devicetree:ro
#     SCREENSTATS_DEVICETREE: /host-devicetree
DT_ROOT_ENV = "SCREENSTATS_DEVICETREE"
DT_ROOT_DEFAULT = "/proc/device-tree"
DT_BASE_MARKER = "devicetree/base/"


def devicetree_root() -> Path:
    return Path(os.environ.get(DT_ROOT_ENV) or DT_ROOT_DEFAULT)


def _dt_relative(link_target: str) -> str | None:
    """Turn an of_node symlink target into a path relative to the devicetree base.

    of_node points at e.g.
        ../../../../../firmware/devicetree/base/soc/gpio@7e200000
    which resolves under /sys and is therefore unreadable in a container. The
    readlink itself works fine, so take the part after 'devicetree/base/' and
    re-root it on whatever devicetree we can actually read.
    """
    idx = link_target.find(DT_BASE_MARKER)
    if idx < 0:
        return None
    rel = link_target[idx + len(DT_BASE_MARKER):].strip("/")
    return rel or None

# Substrings that mark a chip as the 40-pin header controller.
HEADER_HINTS = (
    "bcm2835-gpio",     # Pi 1/2/3/Zero
    "bcm2836-gpio",
    "bcm2711-gpio",     # Pi 4B  <- verified target
    "bcm2712-gpio",     # Pi 5
    "rp1-gpio",         # Pi 5 RP1
    "pinctrl-bcm",      # label form
)

# Substrings that mark a chip as definitely NOT the header.
NON_HEADER_HINTS = (
    "firmware-gpio",    # raspberrypi,firmware-gpio  (the mailbox expander)
    "expgpio",
    "exp-gpio",
    "brcmexp",
    "brcmvirt",
)


def _read_nul(path: Path) -> str:
    """Read a devicetree string property (NUL-separated, NUL-terminated)."""
    try:
        return path.read_bytes().replace(b"\0", b",").decode("ascii", "replace").strip(",")
    except OSError:
        return ""


def _read_text(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def gpiochips() -> dict[int, dict[str, str]]:
    """Map chip index -> {'compatible','of_node','label','dev'}.

    Indexed by the number in `gpiochipN`, which is the same N as `/dev/gpiochipN`
    and the same index gpiozero/lgpio open.
    """
    out: dict[int, dict[str, str]] = {}
    root = Path(GPIO_DEVICES)
    if not root.is_dir():
        return out
    dt = devicetree_root()
    for entry in sorted(root.glob("gpiochip*")):
        try:
            index = int(entry.name.removeprefix("gpiochip"))
        except ValueError:
            continue
        of_node = entry / "of_node"

        # readlink works even when the target is unreadable (container case).
        link = ""
        try:
            link = os.readlink(of_node)
        except OSError:
            pass

        # Prefer the re-rooted devicetree path; fall back to following the symlink
        # directly, which works on a host where /sys is real.
        compatible = ""
        rel = _dt_relative(link)
        if rel:
            compatible = _read_nul(dt / rel / "compatible")
        if not compatible:
            compatible = _read_nul(of_node / "compatible")

        out[index] = {
            "dev": f"/dev/{entry.name}",
            "of_node": os.path.basename(link) if link else "",
            "compatible": compatible,
            # May legitimately be absent; see module docstring.
            "label": _read_text(entry / "label"),
        }
    return out


def classify(info: dict[str, str]) -> str:
    """'header' | 'other' | 'unknown' for one chip."""
    blob = f"{info.get('compatible', '')} {info.get('label', '')}".lower()
    if any(h in blob for h in NON_HEADER_HINTS):
        return "other"
    if any(h in blob for h in HEADER_HINTS):
        return "header"
    return "unknown"


def preflight_gpiochip(expected: int = 0) -> str | None:
    """Return an actionable problem string, or None when all is well.

    `expected` is the chip gpiozero will open (0 on every Pi except a Pi 5 that
    exposes /dev/gpiochip4).
    """
    chips = gpiochips()
    if not chips:
        return None                     # not Linux / no gpio subsystem: not our call

    info = chips.get(expected)
    if info is None:
        return (
            f"gpiozero will open /dev/gpiochip{expected}, but the kernel exposes no "
            f"gpiochip{expected}; present: {sorted(chips)}"
        )

    kind = classify(info)
    if kind == "header":
        return None
    if kind == "unknown":
        return None                     # deliberately silent; see module docstring

    headers = [i for i, c in chips.items() if classify(c) == "header"]
    hint = (
        f" The header controller looks like /dev/gpiochip{headers[0]}."
        if headers
        else ""
    )
    return (
        f"/dev/gpiochip{expected} is {info['compatible'] or info['label'] or 'unidentified'!r}, "
        f"which is not the 40-pin header controller, but gpiozero will open it "
        f"anyway -- the panel would be driven on the wrong GPIO lines.{hint} "
        f"Set GPIOZERO_PIN_FACTORY and pin the chip explicitly."
    )


def spi_status() -> dict[str, object]:
    """Kernel-visible SPI0 state, including the devicetree node."""
    node = devicetree_root() / "soc" / "spi@7e204000" / "status"
    dt = _read_nul(node)
    return {
        "spi_present": os.path.exists(SPI_PATH),
        "spi_path": SPI_PATH,
        "devicetree_status": dt or "unknown",
        "spidevs": sorted(glob.glob("/dev/spidev*")),
    }


def summary() -> dict[str, object]:
    chips = gpiochips()
    return {
        **spi_status(),
        "gpiochips": {
            f"gpiochip{i}": {**c, "role": classify(c)} for i, c in sorted(chips.items())
        },
        "gpiochip_problem": preflight_gpiochip(),
    }


def main() -> int:
    import json

    data = summary()
    print(json.dumps(data, indent=2, sort_keys=True))
    problem = data["gpiochip_problem"]
    if problem:
        print(f"\nPROBLEM: {problem}")
    if not data["spi_present"]:
        print(
            f"\nSPI is not enabled: {SPI_PATH} is missing and the devicetree node reads "
            f"{data['devicetree_status']!r}. Add 'dtparam=spi=on' to config.txt on the "
            "active boot slot and reboot (see tools/enable-spi.sh)."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
