"""Host CPU / RAM / disk sampling (PLAN.md §5).

Docker does not mask `/proc`, so `/proc/stat` and `/proc/meminfo` read inside
the container are the *host* figures — which is what the panel is meant to
show. Disk comes from `statvfs` on the app data path, because umbrelOS stores
app data on the `data` partition rather than on `/`.

Nothing in here raises. A missing or malformed source (notably: `/proc` does
not exist on macOS, where the rest of the app is developed) yields zeros for
that field only, so the renderer keeps painting a frame instead of crashing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["HostMetrics", "MetricsCollector"]

PROC_STAT = "/proc/stat"
PROC_MEMINFO = "/proc/meminfo"
DEFAULT_DISK_PATH = "/data"


@dataclass(frozen=True)
class HostMetrics:
    cpu: float  # 0..1
    ram: float  # 0..1
    disk: float  # 0..1
    ram_used: int
    ram_total: int
    disk_used: int
    disk_total: int


def _clamp01(value: float) -> float:
    if value != value:  # NaN
        return 0.0
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


def _ratio(used: int, total: int) -> float:
    return _clamp01(used / total) if total > 0 else 0.0


def _read_cpu_totals() -> tuple[int, int] | None:
    """`(total, idle)` jiffies from the aggregate `cpu ` line of /proc/stat."""
    try:
        with open(PROC_STAT, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.startswith("cpu "):
                    continue
                try:
                    fields = [int(tok) for tok in line.split()[1:]]
                except ValueError:
                    return None
                # user nice system idle iowait irq softirq steal guest ...
                if len(fields) < 5:
                    return None
                total = sum(fields)
                idle = fields[3] + fields[4]  # idle + iowait
                return total, idle
    except OSError:
        return None
    return None


def _read_memory() -> tuple[int, int]:
    """`(used_bytes, total_bytes)` from /proc/meminfo; (0, 0) if unavailable.

    Used is `MemTotal - MemAvailable`: MemAvailable is the kernel's own
    estimate of what a new workload could claim, so it counts reclaimable
    page cache as free the way a user expects.
    """
    wanted = {"MemTotal:": 0, "MemAvailable:": 0}
    seen = 0
    try:
        with open(PROC_MEMINFO, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                key = line.split(" ", 1)[0]
                if key not in wanted:
                    continue
                parts = line.split()
                if len(parts) < 2:
                    return 0, 0
                try:
                    wanted[key] = int(parts[1])
                except ValueError:
                    return 0, 0
                seen += 1
                if seen == len(wanted):
                    break
    except OSError:
        return 0, 0
    if seen != len(wanted):
        return 0, 0
    # /proc/meminfo is in kB (1024 B units, despite the label).
    total = wanted["MemTotal:"] * 1024
    available = wanted["MemAvailable:"] * 1024
    if total <= 0:
        return 0, 0
    used = total - available
    return (used if used > 0 else 0), total


def _read_disk(path: str) -> tuple[int, int]:
    """`(used_bytes, total_bytes)` for the filesystem holding `path`."""
    try:
        st = os.statvfs(path)
    except OSError:
        return 0, 0
    total = st.f_blocks * st.f_frsize
    # f_bavail, not f_bfree: excludes the root-reserved blocks, so the figure
    # matches what an unprivileged process can actually still write.
    free = st.f_bavail * st.f_frsize
    if total <= 0:
        return 0, 0
    used = total - free
    return (used if used > 0 else 0), total


class MetricsCollector:
    """Stateful sampler. CPU needs two /proc/stat reads, so the previous
    totals live here and the first `sample()` reports cpu=0.0."""

    def __init__(self, disk_path: str = DEFAULT_DISK_PATH) -> None:
        self.disk_path = disk_path
        self._prev_total: int | None = None
        self._prev_idle: int | None = None

    def sample(self) -> HostMetrics:
        cpu = self._sample_cpu()
        ram_used, ram_total = _read_memory()
        disk_used, disk_total = _read_disk(self.disk_path)
        return HostMetrics(
            cpu=cpu,
            ram=_ratio(ram_used, ram_total),
            disk=_ratio(disk_used, disk_total),
            ram_used=ram_used,
            ram_total=ram_total,
            disk_used=disk_used,
            disk_total=disk_total,
        )

    def _sample_cpu(self) -> float:
        totals = _read_cpu_totals()
        if totals is None:
            # Keep the previous baseline: /proc/stat being briefly unreadable
            # should not turn the next delta into a bogus spike.
            return 0.0
        total, idle = totals
        prev_total, prev_idle = self._prev_total, self._prev_idle
        self._prev_total, self._prev_idle = total, idle
        if prev_total is None or prev_idle is None:
            return 0.0  # first sample: no delta yet
        d_total = total - prev_total
        d_idle = idle - prev_idle
        if d_total <= 0:  # counters reset, or sampled twice within a jiffy
            return 0.0
        return _clamp01(1.0 - (d_idle / d_total))


if __name__ == "__main__":
    import time

    collector = MetricsCollector(os.environ.get("SCREENSTATS_DISK_PATH") or "/")
    first = collector.sample()
    print(f"sample 1 (no cpu delta yet): {first}")
    time.sleep(1.0)
    second = collector.sample()
    print(f"sample 2 (1 s later):        {second}")
    print(
        f"cpu {second.cpu * 100:5.1f}%  "
        f"ram {second.ram * 100:5.1f}% ({second.ram_used}/{second.ram_total} B)  "
        f"disk {second.disk * 100:5.1f}% ({second.disk_used}/{second.disk_total} B)"
    )
