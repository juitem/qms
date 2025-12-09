#!/usr/bin/env python3
"""
merge_original.py

Merge symbolized stack traces back into the original log file.

High-level behavior:

  - Read the original log file as plain text lines.
  - For each SymbolizedTrace (in order), find the first frame's raw_line
    in the original file.
  - Copy all lines before that point as-is.
  - Replace the consecutive original frame lines with the formatted
    symbolized trace (stack only, no preamble).
  - Continue scanning forward; repeat for the next trace.
  - Copy any remaining lines at the end as-is.

Important assumptions:

  - Traces in `sym_traces` correspond to stack dumps in the original file
    in the same order as they were parsed.
  - For each SymbolizedTrace:
      - frames[i].raw_line is exactly one original line in the log
      - frames are contiguous in the original log (no unrelated lines in between)
  - Inline frames do NOT correspond to extra lines in the original log;
    they only affect how a single original frame line expands into multiple
    output lines.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, List

from symbolizer import SymbolizedTrace
from output_formatter import format_symbolized_trace


LOG = logging.getLogger("merge_original")


def _normalize_line(s: str) -> str:
    """
    Normalize a log line for comparison.

    We strip only the trailing newline so that raw_line (without '\n')
    matches the line read from the file.
    """
    return s.rstrip("\n\r")


def merge_original_file(
    src_path: Path,
    sym_traces: Iterable[SymbolizedTrace],
    dst_path: Path,
    encoding: str = "utf-8",
) -> None:
    """
    Merge symbolized traces into the original log file.

    Parameters:
        src_path:
            Path to the original log file.
        sym_traces:
            Iterable of SymbolizedTrace objects produced by symbolize_all().
        dst_path:
            Path to write the merged log file.
    """
    LOG.info("Merging original log: %s -> %s", src_path, dst_path)

    # Read the entire original file.
    with src_path.open("r", encoding=encoding, errors="replace") as f:
        original_lines: List[str] = f.readlines()

    out_lines: List[str] = []
    i = 0
    n = len(original_lines)

    # Process each symbolized trace in order.
    for trace in sym_traces:
        if not trace.frames:
            continue

        # We rely on the first frame's raw_line as an anchor in the original log.
        first_raw = _normalize_line(trace.frames[0].raw_line or "")

        if not first_raw:
            # If raw_line is somehow empty, we cannot match it safely; skip this trace.
            LOG.warning(
                "Trace %d has empty first raw_line; skipping merge for this trace.",
                trace.trace_index,
            )
            continue

        # Scan forward until we find the anchor line.
        found_index = -1
        while i < n:
            line_norm = _normalize_line(original_lines[i])
            if line_norm == first_raw:
                found_index = i
                break
            out_lines.append(original_lines[i])
            i += 1

        if found_index < 0:
            # Could not find this trace in the remaining lines.
            LOG.warning(
                "Could not find first frame of trace %d in original log; "
                "copying remaining lines as-is.",
                trace.trace_index,
            )
            # Copy the rest and stop processing further traces.
            out_lines.extend(original_lines[i:])
            i = n
            break

        # We found the start of this trace at found_index.
        start = found_index

        # Skip original frame lines corresponding to this trace.
        # Assumption: one frame -> one original line.
        frame_count = len(trace.frames)
        end = min(n, start + frame_count)
        i = end  # move cursor past the original frame block

        # Now insert the symbolized version of this trace.
        # We do NOT include preamble here, because preamble lines are already
        # present in the original file before 'start' and have been copied above.
        formatted = format_symbolized_trace(trace, include_preamble=False)

        for idx, line in enumerate(formatted):
            # format_symbolized_trace() returns lines WITHOUT trailing '\n'.
            # We always end lines with a single '\n' when writing back.
            if line == "" and idx == len(formatted) - 1:
                # Last blank separator line at the end of the trace:
                # keep it as a single newline (no extra blank block).
                out_lines.append("\n")
            else:
                out_lines.append(line + "\n")

    # Copy any remaining original lines after the last merged trace.
    if i < n:
        out_lines.extend(original_lines[i:])

    # Write the merged result.
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    with dst_path.open("w", encoding=encoding) as f:
        f.writelines(out_lines)

    LOG.info("Merged log written to: %s", dst_path)


__all__ = [
    "merge_original_file",
]