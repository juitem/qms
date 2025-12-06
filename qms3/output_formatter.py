#!/usr/bin/env python3
"""
output_formatter.py

Formatting utilities for QSS.

Responsibilities:
  - Take SymbolizedTrace / SymbolizedFrame objects
  - Produce human-readable text lines
  - Handle inline frames in a compact way
  - Keep the convention: "#N ADDRESS in FUNC at FILE:LINE"

This module does NOT run addr2line or touch ELF files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List

from symbolizer import SymbolizedTrace, SymbolizedFrame


# ---------------------------------------------------------------------------
# Single frame formatting
# ---------------------------------------------------------------------------

def format_symbolized_frame(frame: SymbolizedFrame) -> List[str]:
    """
    Format a single SymbolizedFrame into one or more text lines.

    Rules:
      - First inline frame is printed as:
            "#N ADDRESS in FUNC at FILE:LINE"
      - Additional inline frames (if any) are printed as:
            "    [inline] in FUNC at FILE:LINE"
      - If information is missing, use "??".
    """
    lines: List[str] = []

    if not frame.inlines:
        func = "??"
        file_line = "??"
        first_line = f"#{frame.frame_index} {frame.address} in {func} at {file_line}"
        lines.append(first_line)
        return lines

    # First inline frame
    func0, file_line0 = frame.inlines[0]
    func0 = func0 or "??"
    file_line0 = file_line0 or "??"
    first_line = f"#{frame.frame_index} {frame.address} in {func0} at {file_line0}"
    lines.append(first_line)

    # Additional inline frames
    for func, file_line in frame.inlines[1:]:
        func = func or "??"
        file_line = file_line or "??"
        lines.append(f"    [inline] in {func} at {file_line}")

    return lines


# ---------------------------------------------------------------------------
# Trace formatting
# ---------------------------------------------------------------------------

def format_symbolized_trace(
    trace: SymbolizedTrace,
    include_preamble: bool = True,
) -> List[str]:
    """
    Format a single SymbolizedTrace into text lines.

    The typical structure:
        (optional preamble lines)
        #0 ...
        #1 ...
        ...
        (blank line after each trace)
    """
    out: List[str] = []

    if include_preamble and trace.preamble:
        out.extend(trace.preamble)

    for frame in sorted(trace.frames, key=lambda f: f.frame_index):
        out.extend(format_symbolized_frame(frame))

    out.append("")  # blank line separator
    return out


def format_all_traces(
    traces: Iterable[SymbolizedTrace],
    include_preamble: bool = True,
) -> List[str]:
    """
    Format a collection of SymbolizedTrace objects into one list of lines.
    """
    out: List[str] = []
    for t in traces:
        out.extend(format_symbolized_trace(t, include_preamble=include_preamble))
    return out


# ---------------------------------------------------------------------------
# Convenience: write to file
# ---------------------------------------------------------------------------

def write_formatted_traces_to_file(
    traces: Iterable[SymbolizedTrace],
    path: Path,
    include_preamble: bool = True,
    encoding: str = "utf-8",
) -> None:
    """
    Format traces and write them to a file.
    """
    lines = format_all_traces(traces, include_preamble=include_preamble)
    text = "\n".join(lines)

    with path.open("w", encoding=encoding) as f:
        f.write(text)


__all__ = [
    "format_symbolized_frame",
    "format_symbolized_trace",
    "format_all_traces",
    "write_formatted_traces_to_file",
]
