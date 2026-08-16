#!/bin/sh
# Enable SPI0 on an umbrelOS Raspberry Pi so the e-Paper HAT can be driven.
#
# WHY THIS SCRIPT EXISTS
# ----------------------
# umbrelOS's Raspberry Pi boot config enables the I2C bus and nothing else, so
# /dev/spidev0.0 never appears and no container can talk to an SPI HAT.
#
# The file that must change is `config.txt` on the ACTIVE BOOT SLOT, which on
# umbrelOS is an UNMOUNTED vfat partition. Two things make this easy to get wrong:
#
#   1. `/boot/firmware/config.txt` exists on the running root filesystem and looks
#      exactly like the file you want. It is a build-time staging copy. The Pi
#      firmware never reads it. Editing it does nothing.
#   2. The boot partitions carry NO filesystem label on this image. Guidance that
#      says 'edit the partition labelled BOOT-A' is wrong: plugging the card into
#      another machine surfaces TWO unlabelled FAT volumes, a 256 MiB one holding
#      `autoboot.txt` (wrong) and a 128 MiB one holding `config.txt` (right).
#
# This script resolves the active slot from `autoboot.txt` instead of guessing,
# and is idempotent: running it twice changes nothing the second time.
#
# USAGE
#   sudo sh enable-spi.sh --check    # read-only: report state, change nothing
#   sudo sh enable-spi.sh            # append dtparam=spi=on if missing
#
# A reboot is required for the change to take effect.
#
# NOTE ON UPDATES: an umbrelOS update installs a whole new boot slot and switches
# to it, so this edit does not survive an OS update. Re-run the script, or install
# the ScreenStats app with the `enable-spi-autofix` marker so its pre-start hook
# re-applies it automatically on every app start.

set -eu

CONFIG_MOUNT="/run/rugix/mounts/config"
AUTOBOOT="${CONFIG_MOUNT}/autoboot.txt"
DISK="/dev/mmcblk0"
PARAM="dtparam=spi=on"
MNT=""
CHECK_ONLY=0

[ "${1:-}" = "--check" ] && CHECK_ONLY=1

log() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

cleanup() {
    if [ -n "$MNT" ] && [ -d "$MNT" ]; then
        umount "$MNT" 2>/dev/null || true
        rmdir "$MNT" 2>/dev/null || true
    fi
}
trap cleanup EXIT HUP INT TERM

[ "$(id -u)" = "0" ] || die "must run as root (use sudo); mounting the boot slot needs it"

log "== current kernel state =="
if [ -e /dev/spidev0.0 ]; then
    log "  /dev/spidev0.0            present"
else
    log "  /dev/spidev0.0            MISSING"
fi
dt_status="$(tr -d '\000' < /proc/device-tree/soc/spi@7e204000/status 2>/dev/null || echo '?')"
log "  devicetree spi@7e204000   ${dt_status}"

# ---------------------------------------------------------------------------
# Resolve the active boot slot. autoboot.txt's [all] section names the partition
# the firmware boots normally; [tryboot] is only used for update attempts.
# ---------------------------------------------------------------------------
[ -r "$AUTOBOOT" ] || die "cannot read ${AUTOBOOT} - is this an umbrelOS Pi image?"

part="$(awk '
    /^[[:space:]]*\[/ { section = $0; next }
    section ~ /\[all\]/ && /^[[:space:]]*boot_partition[[:space:]]*=/ {
        sub(/.*=[[:space:]]*/, ""); gsub(/[[:space:]]/, ""); print; exit
    }
' "$AUTOBOOT")"

[ -n "${part:-}" ] || die "no boot_partition= in the [all] section of ${AUTOBOOT}"

BOOT_DEV="${DISK}p${part}"
log ""
log "== active boot slot =="
log "  autoboot.txt boot_partition = ${part}"
log "  device                      = ${BOOT_DEV}"
[ -b "$BOOT_DEV" ] || die "${BOOT_DEV} is not a block device"

# ---------------------------------------------------------------------------
# Inspect read-only first. Never mount a live boot slot writable unless we are
# actually going to change it.
# ---------------------------------------------------------------------------
MNT="$(mktemp -d /tmp/screenstats-boot.XXXXXX)"
mount -o ro "$BOOT_DEV" "$MNT" || die "failed to mount ${BOOT_DEV} read-only"

CFG="${MNT}/config.txt"
[ -f "$CFG" ] || die "no config.txt on ${BOOT_DEV}; this does not look like the boot slot"

if grep -q "^[[:space:]]*${PARAM}[[:space:]]*$" "$CFG"; then
    HAS_PARAM=1
else
    HAS_PARAM=0
fi

log "  config.txt                  found"
log "  ${PARAM}          $([ "$HAS_PARAM" = 1 ] && echo present || echo ABSENT)"

if [ "$HAS_PARAM" = 1 ]; then
    log ""
    if [ -e /dev/spidev0.0 ]; then
        log "Nothing to do: SPI is enabled and /dev/spidev0.0 exists."
    else
        log "Nothing to change: ${PARAM} is already in config.txt."
        log "REBOOT REQUIRED for it to take effect (sudo reboot)."
    fi
    exit 0
fi

if [ "$CHECK_ONLY" = 1 ]; then
    log ""
    log "--check: would append '${PARAM}' to config.txt on ${BOOT_DEV}."
    log "Re-run without --check to apply."
    exit 0
fi

# ---------------------------------------------------------------------------
# Apply. Back up once, append once, flush before unmounting.
# ---------------------------------------------------------------------------
log ""
log "== applying =="
mount -o remount,rw "$MNT" || die "failed to remount ${BOOT_DEV} read-write"

BAK="${MNT}/config.txt.screenstats-bak"
if [ ! -f "$BAK" ]; then
    cp "$CFG" "$BAK"
    log "  backed up config.txt -> $(basename "$BAK")"
else
    log "  backup already exists, keeping it"
fi

# A trailing newline may be missing; guard so we never join onto the last line.
[ -n "$(tail -c 1 "$CFG" 2>/dev/null)" ] && printf '\n' >> "$CFG"
printf '\n# Added by ScreenStats: SPI0 for the Waveshare e-Paper HAT.\n%s\n' "$PARAM" >> "$CFG"
sync
log "  appended ${PARAM}"

mount -o remount,ro "$MNT" 2>/dev/null || true

log ""
log "Done. REBOOT REQUIRED:  sudo reboot"
log "After the reboot, /dev/spidev0.0 should exist and"
log "/proc/device-tree/soc/spi@7e204000/status should read 'okay'."
