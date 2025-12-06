#!/usr/bin/env python3
"""
addr2line_runner.py

Module responsible for executing addr2line for QSS.

Responsibilities:
  - Prepare command arguments for addr2line
  - Execute addr2line for each address (supports debug ELF if available)
  - Parse multi-line inline frame outputs
  - Thread-based parallel execution (ProcessPool optional to add later)
  - No stack parsing or ELF resolution is performed here
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple


LOG = logging.getLogger("addr2line_runner")


# ---------------------------------------------------------------------------
# Data Model
# ---------------------------------------------------------------------------

@dataclass
class Addr2lineResult:
    """
    Represents the output of addr2line for a single address.

    Inline frames appear as multiple lines:
        funcA
        fileA:lineA
        funcB
        fileB:lineB
    """
    address: str
    frames: List[Tuple[str, str]]  # list of (function, file:line)


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

def _run_addr2line(cmd: List[str]) -> List[str]:
    """
    Execute addr2line command and capture stdout lines.

    Returns:
        List[str] - raw lines (no trimming of inline structure)
    """
    try:
        LOG.debug("Executing: %s", " ".join(cmd))
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        return out.decode("utf-8", errors="replace").splitlines()
    except subprocess.CalledProcessError as e:
        LOG.error("addr2line failed: %s", e)
        return ["??", "??"]


def _pair_lines(lines: List[str]) -> List[Tuple[str, str]]:
    """
    Given a sequence like:
        [func1, file1:line1, func2, file2:line2]
    produce pairs:
        [(func1, file1:line1), (func2, file2:line2)]
    """
    frames: List[Tuple[str, str]] = []
    it = iter(lines)
    for func in it:
        try:
            file_line = next(it)
        except StopIteration:
            file_line = "??"
        frames.append((func, file_line))
    return frames


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_addr2line(
    addr: str,
    elf_file: Path,
    debug_file: Optional[Path] = None,
    prefer_debug: bool = True,
) -> Addr2lineResult:
    """
    Execute addr2line for a single address.

    Parameters:
        addr: Address string like "0x1234"
        elf_file: Path to main ELF
        debug_file: Path to debug ELF (optional)
        prefer_debug: If True, use debug_file first when available

    Returns:
        Addr2lineResult with parsed inline frames.
    """
    target = None

    if prefer_debug and debug_file and debug_file.exists():
        target = debug_file
    else:
        target = elf_file

    cmd = [
        "addr2line",
        "-f",  # function name
        "-e", str(target),
        addr,
    ]

    raw_lines = _run_addr2line(cmd)
    paired = _pair_lines(raw_lines)

    return Addr2lineResult(
        address=addr,
        frames=paired,
    )


def run_addr2line_batch(
    addrs: List[str],
    elf_file: Path,
    debug_file: Optional[Path] = None,
    prefer_debug: bool = True,
) -> List[Addr2lineResult]:
    """
    Execute addr2line for multiple addresses sequentially.

    (ThreadPool version will be provided in symbolizer layer.)
    """
    results: List[Addr2lineResult] = []
    for a in addrs:
        results.append(
            run_addr2line(
                addr=a,
                elf_file=elf_file,
                debug_file=debug_file,
                prefer_debug=prefer_debug,
            )
        )
    return results

__all__ = [
    "Addr2lineResult",
    "run_addr2line",
    "run_addr2line_batch",
]
