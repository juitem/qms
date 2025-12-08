#!/usr/bin/env python3
"""
merge_original.py

Utilities to merge symbolized stack traces back into the original log file.

High-level idea:
  - Keep all non-stack lines from the original log as-is.
  - For each stack trace that has been symbolized (SymbolizedTrace),
    find the corresponding block of raw stack frames in the original file
    and replace that block with the formatted symbolized trace.

Assumptions:
  - Each SymbolizedTrace.frames[i].raw_line exactly matches a line in the
    original log file (before symbolization).
  - For a given trace, all frames appear as a contiguous block of lines in
    the original log (typical for stack dumps).
  - We only replace known stack blocks; everything else in the original file
    is preserved as-is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List

from symbolizer import SymbolizedTrace
from output_formatter import format_symbolized_trace


def _strip_newline(line: str) -> str:
    """Return the line without a trailing newline."""
    if line.endswith("\n") or line.endswith("\r"):
        return line.rstrip("\r\n")
    return line


def merge_original_lines(
    original_lines: List[str],
    traces: Iterable[SymbolizedTrace],
) -> List[str]:
    """
    Merge symbolized traces into the original log lines.

    Parameters:
        original_lines:
            List of lines read from the original log file, INCLUDING newlines.
        traces:
            Iterable of SymbolizedTrace objects produced by the symbolizer.

    Returns:
        New list of lines, INCLUDING newlines, where stack blocks have been
        replaced by formatted symbolized traces, and all other lines are
        preserved as-is.
    """
    # Work on a copy so we do not mutate the caller's list
    src_lines = list(original_lines)
    out_lines: List[str] = []

    # Flatten traces into a list
    traces_list = list(traces)
    if not traces_list:
        # Nothing to merge; return original as-is
        return src_lines

    idx = 0
    n = len(src_lines)

    # Iterate through each trace and replace its block in order
    for trace in traces_list:
        if not trace.frames:
            # No frames, nothing to replace for this trace
            continue

        # The raw line of the first frame in this trace (without newline).
        first_raw = _strip_newline(trace.frames[0].raw_line)

        # 1) Copy original lines until we find the first frame's raw line
        start_idx = -1
        while idx < n:
            current_raw = _strip_newline(src_lines[idx])
            if current_raw == first_raw:
                start_idx = idx
                break
            # Not a match; keep the original line
            out_lines.append(src_lines[idx])
            idx += 1

        if start_idx < 0:
            # Could not find this trace in the remaining original lines.
            # Stop merging further traces; append the rest as-is.
            out_lines.extend(src_lines[idx:])
            return out_lines

        # 2) We found the start of this trace's stack block at start_idx.
        #    Skip the original stack block lines corresponding to the
        #    number of frames in this trace.
        block_len = len(trace.frames)
        end_idx = min(start_idx + block_len, n)

        # Skip original block [start_idx:end_idx]
        idx = end_idx

        # 3) Append the formatted symbolized trace (without preamble,
        #    because preamble lines, if any, are usually already present
        #    in the original log around the stack block).
        formatted = format_symbolized_trace(trace, include_preamble=False)

        for line in formatted:
            # format_symbolized_trace returns lines WITHOUT newlines
            out_lines.append(line + "\n")

    # 4) After replacing all trace blocks, append remaining original lines
    if idx < n:
        out_lines.extend(src_lines[idx:])

    return out_lines


def merge_original_file(
    original_path: Path,
    traces: Iterable[SymbolizedTrace],
    output_path: Path,
    encoding: str = "utf-8",
) -> None:
    """
    Convenience helper:
      - Read original file
      - Merge symbolized traces
      - Write merged result to output file
    """
    with original_path.open("r", encoding=encoding, errors="replace") as f:
        original_lines = f.readlines()

    merged_lines = merge_original_lines(original_lines, traces)

    with output_path.open("w", encoding=encoding) as f:
        f.writelines(merged_lines)


__all__ = [
    "merge_original_lines",
    "merge_original_file",
]