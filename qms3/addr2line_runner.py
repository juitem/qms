#!/usr/bin/env python3
"""
addr2line_runner.py

Helper module to run addr2line for one or many addresses.

This module provides:

  - Addr2lineResult: result type with a list of (function, file:line) pairs.
  - run_addr2line(): convenience wrapper for a single address.
  - run_addr2line_multi(): run addr2line once for many addresses for the same
    ELF file (and optional separate debug file).

The main goal is to reduce overhead by invoking addr2line once per
(real_elf, debug_elf) pair with all relevant addresses, rather than spawning
a new process per address.
"""

from __future__ import annotations

import logging
import subprocess
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

LOG = logging.getLogger("addr2line_runner")


@dataclass
class Addr2lineResult:
    """
    Result of addr2line for a single address.

    frames:
        List of (function_name, "file:line") pairs. Inline frames are included
        as additional entries in this list.
    """
    frames: List[Tuple[str, str]]


def _choose_executable(elf_file: Path, debug_file: Optional[Path], prefer_debug: bool) -> Path:
    """
    Decide which file to pass to addr2line as -e argument.

    If prefer_debug is True and debug_file is not None, use debug_file
    when it exists. Otherwise use elf_file.
    """
    if prefer_debug and debug_file is not None and debug_file.exists():
        return debug_file
    return elf_file


_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]+$")


def _normalize_addr(addr: str) -> str:
    """
    Normalize an address string so that "0x1ffff" and "1FFFF" can be matched.

    The format is "0x" + lowercase hex without leading zeros (except "0").
    """
    s = addr.strip()
    if not s:
        return ""
    if not s.startswith("0x") and not s.startswith("0X"):
        s = "0x" + s
    else:
        # Ensure we use lowercase "0x"
        s = "0x" + s[2:]
    body = s[2:]
    body = body.lstrip("0")
    if not body:
        body = "0"
    return "0x" + body.lower()


def run_addr2line_multi(
    addrs: List[str],
    elf_file: Path,
    debug_file: Optional[Path] = None,
    prefer_debug: bool = True,
) -> Dict[str, Addr2lineResult]:
    """
    Run addr2line once for many addresses for a single ELF file.

    Args:
        addrs:
            List of address strings (e.g. "0x1ffff9de0d58" or "1ffff9de0d58").
        elf_file:
            Path to the main ELF file.
        debug_file:
            Optional path to a separate debug ELF file.
        prefer_debug:
            If True and debug_file exists, it is used as the -e target.

    Returns:
        Dict mapping the original address string to Addr2lineResult.

        If addr2line fails for some address, that address may be missing
        from the result dict. Callers should handle this case and treat
        missing entries as unknown ("??").
    """
    result: Dict[str, Addr2lineResult] = {}

    if not addrs:
        return result

    exe = _choose_executable(elf_file, debug_file, prefer_debug)

    # Prepare command: we use "-a" so that addr2line prints the address
    # before each group, which allows us to split the output per address.
    cmd: List[str] = [
        "addr2line",
        "-f",      # print function names
        "-C",      # demangle
        "-i",      # show inlined functions
        "-a",      # print address before each group
        "-e",
        str(exe),
    ]
    cmd.extend(addrs)

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        LOG.error("addr2line not found when running: %s", cmd)
        return result
    except Exception as e:
        LOG.error("Failed to run addr2line: %s", e)
        return result

    if proc.returncode != 0:
        # We log stderr for debugging but otherwise just return an empty dict.
        LOG.warning(
            "addr2line exited with code %d for %s: %s",
            proc.returncode,
            exe,
            proc.stderr.strip(),
        )
        return result

    # Parse output. With "-a -f -C -i", output looks like:
    #
    #   0xADDR1
    #   func1
    #   file1:line1
    #   func1_inline
    #   file1_inline:lineX
    #   0xADDR2
    #   func2
    #   file2:line2
    #   ...
    #
    # We treat each "0xADDR" line as the start of a new address block,
    # and collect (func, file:line) pairs until the next "0xADDR".
    lines = proc.stdout.splitlines()

    addr_to_frames_raw: Dict[str, List[Tuple[str, str]]] = {}
    current_addr: Optional[str] = None
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i].strip()

        if _ADDR_RE.match(line):
            current_addr = line
            if current_addr not in addr_to_frames_raw:
                addr_to_frames_raw[current_addr] = []
            i += 1
            continue

        if current_addr is None:
            # Unexpected content before any address. Skip it.
            i += 1
            continue

        # This should be a function name line, followed by a "file:line" line.
        func_name = line if line else "??"

        file_line = "??:0"
        if i + 1 < n:
            fl = lines[i + 1].strip()
            file_line = fl if fl else "??:0"
            i += 2
        else:
            # No file:line line present; still record the function with "??:0".
            i += 1

        addr_to_frames_raw[current_addr].append((func_name, file_line))

    # Now map normalized addresses to frames, then map back to original addrs.
    normalized_map: Dict[str, List[Tuple[str, str]]] = {}
    for addr_str, frames in addr_to_frames_raw.items():
        norm = _normalize_addr(addr_str)
        # If there are multiple groups with the same normalized address,
        # extend the list of frames for that address.
        normalized_map.setdefault(norm, []).extend(frames)

    for original_addr in addrs:
        norm = _normalize_addr(original_addr)
        frames = normalized_map.get(norm)
        if frames:
            result[original_addr] = Addr2lineResult(frames=list(frames))

    return result


def run_addr2line(
    addr: str,
    elf_file: Path,
    debug_file: Optional[Path] = None,
    prefer_debug: bool = True,
) -> Addr2lineResult:
    """
    Convenience wrapper for a single address.

    Internally calls run_addr2line_multi() with a single-address list and
    returns the corresponding Addr2lineResult, or a default result with
    "??" if nothing was resolved.
    """
    mapping = run_addr2line_multi(
        addrs=[addr],
        elf_file=elf_file,
        debug_file=debug_file,
        prefer_debug=prefer_debug,
    )
    res = mapping.get(addr)
    if res is not None:
        return res
    return Addr2lineResult(frames=[("??", "??:0")])