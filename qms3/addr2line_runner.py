#!/usr/bin/env python3
"""
addr2line_runner.py

Thin wrapper around the external 'addr2line' command.

Responsibilities:
  - Run addr2line with proper options (-f -C -i -e)
  - Parse its output into (function, file:line) pairs
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple


LOG = logging.getLogger("addr2line")


@dataclass
class Addr2lineResult:
    frames: List[Tuple[str, str]]  # list of (function, file:line)


def _pair_lines(lines: List[str]) -> List[Tuple[str, str]]:
    """
    Given a list like:
        [func1, file1:line1, func2, file2:line2, ...]
    produce pairs:
        [(func1, file1:line1), (func2, file2:line2), ...]
    """
    out: List[Tuple[str, str]] = []
    it = iter(lines)
    for func in it:
        try:
            file_line = next(it)
        except StopIteration:
            file_line = "??"
        out.append((func, file_line))
    return out


def run_addr2line(
    addr: str,
    elf_file: Path,
    debug_file: Path | None = None,
    prefer_debug: bool = True,
) -> Addr2lineResult:
    """
    Run addr2line for a single address.

    Parameters:
        addr:
            Address string for addr2line (e.g. "0x1fdb8").
        elf_file:
            Path to the main ELF file.
        debug_file:
            Path to the debug ELF file (if any).
        prefer_debug:
            If True and debug_file exists, use it instead of elf_file.

    Returns:
        Addr2lineResult with parsed frames.
    """
    target = elf_file
    if prefer_debug and debug_file is not None and debug_file.is_file():
        target = debug_file

    cmd = [
        "addr2line",
        "-f",      # print function names
        "-C",      # demangle C++ names
        "-i",      # print all inlined frames
        "-e", str(target),
        addr,
    ]

    LOG.debug("Running addr2line: %s", " ".join(cmd))

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as e:
        LOG.error("Failed to execute addr2line: %s", e)
        return Addr2lineResult(frames=[("??", "??")])

    if proc.returncode != 0:
        LOG.warning("addr2line returned non-zero exit code %d: %s",
                    proc.returncode, proc.stderr.strip())
        return Addr2lineResult(frames=[("??", "??")])

    raw_lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    if not raw_lines:
        return Addr2lineResult(frames=[("??", "??")])

    pairs = _pair_lines(raw_lines)
    return Addr2lineResult(frames=pairs)


__all__ = [
    "Addr2lineResult",
    "run_addr2line",
]