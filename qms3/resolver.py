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
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Tuple


LOG = logging.getLogger("resolver")


def _resolve_elf(rootfs: Path, elf_path: str) -> Optional[Path]:
    """
    Resolve the ELF inside the rootfs.

    Example:
        rootfs = /mnt/rootfs
        elf_path = /hal/lib64/libfoo.so
        -> /mnt/rootfs/hal/lib64/libfoo.so
    """
    elf = rootfs / elf_path.lstrip("/")
    if elf.is_file():
        return elf
    # Fallback: try as-is (host path)
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


def resolve_pair(
    rootfs: Path,
    debug_root: Path,
    elf_path: str,
    build_id: Optional[str],
) -> Tuple[Optional[Path], Optional[Path]]:
    """
    Resolve ELF and debug file paths.

    Returns:
        (real_elf, debug_elf or None)
    """
    real_elf = _resolve_elf(rootfs, elf_path)
    debug_elf: Optional[Path] = None

    if build_id:
        debug_elf = _resolve_debug_by_buildid(debug_root, build_id)

    # Fallback: if debug_root is not .build-id style, you could add more rules here,
    # for example /usr/lib/debug/<path>.debug, but we keep it simple for now.

    return real_elf, debug_elf


__all__ = [
    "resolve_pair",
]