# ScreenStats image: one image, two entrypoints (screenstats.renderer / screenstats.server).
#
# MUST be built as a multi-arch manifest list for linux/amd64 AND linux/arm64.
# umbrelOS runs on exactly those two arches, the official app linter hard-errors
# on a single-arch image, and compose pins the *index* digest, which only exists
# for a manifest list. Build and publish with:
#
#   docker buildx build \
#     --platform linux/amd64,linux/arm64 \
#     -t ghcr.io/cooperwang-py/screenstats:0.1.0 \
#     --push image/
#   docker buildx imagetools inspect ghcr.io/cooperwang-py/screenstats:0.1.0
#
# Then copy the reported index digest into screenstats-eink/docker-compose.yml as
# ghcr.io/cooperwang-py/screenstats:0.1.0@sha256:<index-digest>.
FROM python:3.12-slim-bookworm

# fonts-dejavu-core: DejaVuSansMono-Bold for the clock (constant digit advance
#   keeps partial-refresh geometry stable) and DejaVuSans-Bold for labels.
#   Waveshare's own Font.ttc is a 4.94 MiB CJK collection with no stated license
#   and is deliberately not vendored.
# tzdata: zoneinfo needs the system database to resolve the IANA id that
#   ip-api/Open-Meteo report; the slim image does not ship it.
# gcc/libc6-dev: spidev and lgpio are C extensions with no prebuilt wheels for
#   every target; they compile from source here and the toolchain is purged in
#   the same layer so it never ships.
COPY requirements.txt /tmp/requirements.txt
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        fonts-dejavu-core \
        tzdata \
        gcc \
        libc6-dev; \
    pip install --no-cache-dir -r /tmp/requirements.txt; \
    apt-get purge -y --auto-remove gcc libc6-dev; \
    rm -rf /var/lib/apt/lists/* /tmp/requirements.txt

WORKDIR /app

# Application package, then the pinned Waveshare driver copy. `vendor/` is a
# separate PYTHONPATH root so `import waveshare_epd` resolves without turning the
# vendored tree into a subpackage of screenstats.
COPY screenstats/ /app/screenstats/
COPY vendor/ /app/vendor/

ENV PYTHONPATH=/app:/app/vendor \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC \
    SCREENSTATS_DATA=/data

# No USER directive on purpose: compose sets the uid per service. `server` runs
# as 1000:1000 and `renderer` must stay root for /dev/spidev0.0 and
# /dev/gpiochip0 (0660 root:spi / root:gpio on the host).
CMD ["python3", "-m", "screenstats.renderer"]
