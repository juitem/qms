#!/usr/bin/env python3
"""
resolver.py

Responsible for resolving ELF and debug file paths based on:
  - rootfs path
  - debug_root path (typically /usr/lib/debug/.build-id)
  - logged ELF path (e.g. /hal/lib64/libfoo.so)
  - optional build-id

The goal is to return:
  (real_elf_path, debug_elf_path or None)

This version includes a simple in-memory cache so that repeated
( rootfs, debug_root, elf_path, build_id ) lookups do not hit the
filesystem multiple times.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional, Tuple


LOG = logging.getLogger("resolver")


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

# Key:
#   (canonical_rootfs, canonical_debug_root, elf_path, build_id_or_empty)
#
# Value:
#   (real_elf_path_or_None, debug_elf_path_or_None)
_RESOLVE_CACHE: Dict[Tuple[str, str, str, str], Tuple[Optional[Path], Optional[Path]]] = {}


def _make_cache_key(
    rootfs: Path,
    debug_root: Path,
    elf_path: str,
    build_id: Optional[str],
) -> Tuple[str, str, str, str]:
    """
    Build a stable cache key for the resolver.

    We normalize rootfs and debug_root using resolve() so that different
    textual paths pointing to the same directory share the same entry.
    """
    try:
        rootfs_can = str(rootfs.resolve())
    except OSError:
        rootfs_can = str(rootfs)

    try:
        debug_root_can = str(debug_root.resolve())
    except OSError:
        debug_root_can = str(debug_root)

    build_id_key = build_id or ""
    return (rootfs_can, debug_root_can, elf_path, build_id_key)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_elf(rootfs: Path, elf_path: str) -> Optional[Path]:
    """
    Resolve the ELF inside the rootfs.

    Example:
        rootfs = /mnt/rootfs
        elf_path = /hal/lib64/libfoo.so
        -> /mnt/rootfs/hal/lib64/libfoo.so

    If not found under rootfs, we try the host path as-is.
    """
    elf = rootfs / elf_path.lstrip("/")
    if elf.is_file():
        return elf

    p = Path(elf_path)
    if p.is_file():
        return p

    LOG.warning("ELF file not found: %s (rootfs: %s)", elf_path, rootfs)
    return None


def _resolve_debug_by_buildid(debug_root: Path, build_id: str) -> Optional[Path]:
    """
    Resolve debug file by BuildId under debug_root.

    Typical layout:
        debug_root / "aa" / "0d6e0...debug"
    where build_id == "aa0d6e0..."
    """
    if not build_id:
        return None

    bid = build_id.strip()
    if len(bid) < 3:
        return None

    subdir = bid[:2]
    rest = bid[2:]
    candidate = debug_root / subdir / (rest + ".debug")
    if candidate.is_file():
        return candidate

    LOG.debug("Debug file not found for BuildId %s under %s", build_id, debug_root)
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_pair(
    rootfs: Path,
    debug_root: Path,
    elf_path: str,
    build_id: Optional[str],
) -> Tuple[Optional[Path], Optional[Path]]:
    """
    Resolve ELF and debug file paths.

    Parameters:
        rootfs:
            Root directory of the extracted root filesystem.
        debug_root:
            Root directory for debug files (typically /usr/lib/debug/.build-id).
        elf_path:
            Path to the ELF as written in the log (e.g. "/hal/lib64/libfoo.so").
        build_id:
            BuildId string from the log, if any.

    Returns:
        (real_elf, debug_elf or None), possibly from cache.
    """
    key = _make_cache_key(rootfs, debug_root, elf_path, build_id)

    # Cache hit
    if key in _RESOLVE_CACHE:
        return _RESOLVE_CACHE[key]

    # Cache miss: perform actual resolution
    real_elf = _resolve_elf(rootfs, elf_path)
    debug_elf: Optional[Path] = None

    if build_id:
        debug_elf = _resolve_debug_by_buildid(debug_root, build_id)

    _RESOLVE_CACHE[key] = (real_elf, debug_elf)
    return real_elf, debug_elf


__all__ = [
    "resolve_pair",
]