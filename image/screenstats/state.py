"""Atomic publish helpers for the files shared between `renderer` and `server`.

Two processes with different uids read and write the same directory tree
(PLAN.md §3: `renderer` runs as root with the panel devices, `server` runs as
uid 1000 with no hardware). A reader must therefore never observe a
half-written file, and a file published by root must stay readable by the
unprivileged service — so every published file is written through a temporary
file in the *same directory*, renamed into place, and left at mode 0644.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

__all__ = ["publish_json", "publish_bytes", "read_json", "ensure_dirs"]

# Anything a reader may pick up must be world-readable: `renderer` writes as
# root, `server` reads as uid 1000 (PLAN.md §8, "root-owned-file direction is
# one-way").
PUBLISHED_MODE = 0o644


def publish_bytes(path: Path, blob: bytes) -> None:
    """Write `blob` to `path` atomically, leaving it mode 0644.

    The temporary file is created in `path.parent` and not in /tmp because
    `os.replace` is only atomic *within a single filesystem*; a cross-device
    rename raises OSError and a copy-then-move would reintroduce the
    torn-read window this function exists to close.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        with open(tmp, "wb") as fh:
            fh.write(blob)
            fh.flush()
            # Survive a power cut with either the old or the new content,
            # never a truncated file.
            os.fsync(fh.fileno())
        # Fix the mode on the temp inode: after the rename that inode *is*
        # the published file, so it is never visible with a restrictive mode.
        os.chmod(tmp, PUBLISHED_MODE)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def publish_json(path: Path, obj: dict) -> None:
    """Serialise `obj` and publish it atomically at `path`."""
    blob = json.dumps(obj, indent=2, sort_keys=True, default=str).encode("utf-8")
    publish_bytes(path, blob + b"\n")


def read_json(path: Path, default: dict | None = None) -> dict | None:
    """Read a JSON object, returning `default` for anything that is not one.

    Never raises: a missing file, a partially-written file from an older
    non-atomic writer, a permission error, or a JSON document whose top level
    is not an object all collapse to `default`.
    """
    try:
        with open(path, "rb") as fh:
            parsed = json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return default
    if not isinstance(parsed, dict):
        return default
    return parsed


def ensure_dirs(root: Path) -> None:
    """Create the data-directory layout: `root`, `root/cache`, `root/state`."""
    root = Path(root)
    for d in (root, root / "cache", root / "state"):
        d.mkdir(parents=True, exist_ok=True)
