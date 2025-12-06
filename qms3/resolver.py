#!/usr/bin/env python3
"""
resolver.py

ELF + Build-id resolver for QSS.

Responsibilities:
  - Given (elf_path, build_id), locate:
        * real ELF file inside rootfs
        * corresponding debug ELF file inside debug-root
  - Handle symbolic links
  - Provide a clean API for qss.py to obtain resolved paths
  - No addr2line execution here

This module does NOT parse stack dumps.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Tuple


LOG = logging.getLogger("resolver")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _try_path(p: Path) -> Optional[Path]:
    """Return p if it exists, otherwise None."""
    if p.exists():
        return p
    return None


def _resolve_build_id_path(debug_root: Path, build_id: str) -> Optional[Path]:
    """
    Given debug-root = /usr/lib/debug/.build-id
    and build_id = 'aa0d6e0...', locate:

        /usr/lib/debug/.build-id/aa/0d6e0....debug

    build-id hex: first 2 chars = directory, remaining = filename.
    """
    if len(build_id) < 3:
        return None

    prefix = build_id[:2]
    suffix = build_id[2:]
    candidate = debug_root / prefix / (suffix + ".debug")

    return candidate if candidate.exists() else None


# ---------------------------------------------------------------------------
# Public Resolver API
# ---------------------------------------------------------------------------

def resolve_elf(
    rootfs: Path,
    elf_path: str
) -> Optional[Path]:
    """
    Resolve an ELF path relative to rootfs.

    Input elf_path examples:
        /usr/lib64/libfoo.so
        /usr/share/app/.../bin/app1

    If rootfs = /mnt/rootfs:
        return /mnt/rootfs/usr/lib64/libfoo.so  (if exists)
    """
    p = rootfs / elf_path.lstrip("/")
    return p if p.exists() else None


def resolve_debug_file(
    debug_root: Path,
    build_id: Optional[str]
) -> Optional[Path]:
    """
    Resolve a debug file from build-id.

    If build-id is None: cannot resolve → return None.
    """
    if not build_id:
        return None

    return _resolve_build_id_path(debug_root, build_id)


def resolve_pair(
    rootfs: Path,
    debug_root: Path,
    elf_path: str,
    build_id: Optional[str],
) -> Tuple[Optional[Path], Optional[Path]]:
    """
    Resolve both actual ELF and debug-ELF.

    Return:
        (resolved_elf, resolved_debug_elf)
    """
    real_elf = resolve_elf(rootfs, elf_path)
    debug_elf = resolve_debug_file(debug_root, build_id)

    LOG.debug("Resolved pair: %s → ELF:%s DEBUG:%s", elf_path, real_elf, debug_elf)
    return real_elf, debug_elf


__all__ = [
    "resolve_elf",
    "resolve_debug_file",
    "resolve_pair",
]
