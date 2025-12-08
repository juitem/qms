#!/usr/bin/env python3
"""
output_formatter.py

Formatting utilities for QSS.

Responsibilities:
  - Take SymbolizedTrace / SymbolizedFrame objects
  - Produce human-readable text lines
  - Handle inline frames in a compact way
  - Keep the convention: "#N ADDRESS in FUNC at FILE:LINE"

Numbering rule:
  - For each SymbolizedTrace, frame numbers (#N) are assigned sequentially
    starting from 0, including inline frames and raw-decoded frames.

Indent rule:
  - Leading whitespace from the original raw line is preserved whenever possible.

Hint rule:
  - For frames whose file/line is unknown (??, ??:0, ??:?), append the ELF hint
    to the same line, e.g.:
      "#3 0xADDR in func at ??:0 (/path/lib.so+0xOFF) (BuildId: ...)"
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List

from symbolizer import SymbolizedTrace, SymbolizedFrame


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FRAME_PREFIX_RE = re.compile(r"^(\s*)#\d+(\s+)(.*)$")


def _leading_indent(raw_line: str) -> str:
    """
    Return leading whitespace characters of raw_line.
    If raw_line is empty or has no leading whitespace, return "".
    """
    if not raw_line:
        return ""
    stripped = raw_line.lstrip()
    return raw_line[: len(raw_line) - len(stripped)]


def _rewrite_raw_line_with_index(raw_line: str, new_index: int) -> str:
    """
    Replace the "#<old>" prefix in raw_line with "#<new_index>",
    preserving leading whitespace and spacing after '#<n>'.

    Examples:
        "    #10 0x123 in foo at file.c:10"
          -> "    #3 0x123 in foo at file.c:10"   (if new_index == 3)

    If no "#<n>" prefix is found, we insert "#<new_index> " after the indent:
        "    0x123 in foo" -> "    #3 0x123 in foo"
    """
    m = _FRAME_PREFIX_RE.match(raw_line)
    if not m:
        indent = _leading_indent(raw_line)
        stripped = raw_line.lstrip()
        return f"{indent}#{new_index} {stripped}"
    indent, sep, rest = m.group(1), m.group(2), m.group(3)
    return f"{indent}#{new_index}{sep}{rest}"


def _is_unknown_file(loc: str | None) -> bool:
    """
    Return True if file:line string represents an unknown location.

    Typical patterns from addr2line are:
        "??"
        "??:0"
        "??:?"
        "??:123"

    We treat anything starting with "??" as unknown.
    """
    if not loc:
        return True
    s = loc.strip()
    return s.startswith("??")


def _build_elf_hint(frame: SymbolizedFrame) -> str | None:
    """
    Build a hint string from obj_path / obj_offset / build_id, e.g.:

        "(/usr/apps/.../bin/app+0x1234) (BuildId: abcdef...)"

    If we do not have enough information, return None.
    """
    if not frame.obj_path:
        return None

    if frame.obj_offset:
        base = f"({frame.obj_path}+{frame.obj_offset})"
    else:
        base = f"({frame.obj_path})"

    if frame.build_id:
        return f"{base} (BuildId: {frame.build_id})"
    return base


# ---------------------------------------------------------------------------
# Trace formatting with global numbering
# ---------------------------------------------------------------------------

def format_symbolized_trace(
    trace: SymbolizedTrace,
    include_preamble: bool = True,
) -> List[str]:
    """
    Format a single SymbolizedTrace into text lines.

    Frame numbering (#N) is assigned sequentially starting from 0
    across all frames and inline frames in the trace.

    Hint rule:
      - For frames whose file/line is unknown (??, ??:0, ??:?), append the ELF hint
        to the same line, e.g.:
          "#3 0xADDR in func at ??:0 (/path/lib.so+0xOFF) (BuildId: ...)"
    """
    out: List[str] = []

    if include_preamble and trace.preamble:
        out.extend(trace.preamble)

    current_index = 0

    # frames are expected to be sorted by frame_index in SymbolizedTrace
    for frame in trace.frames:
        indent = _leading_indent(frame.raw_line)

        # ------------------------------------------------------------------
        # 1) Raw-decoded frame with no ELF info and no inlines:
        #    keep the original content, but rewrite the "#N" number.
        # ------------------------------------------------------------------
        if not frame.obj_path and not frame.inlines:
            if frame.raw_line:
                out.append(_rewrite_raw_line_with_index(frame.raw_line, current_index))
                current_index += 1
                continue
            # Fallback if somehow raw_line is empty
            line = f"{indent}#{current_index} {frame.address} in ?? at ??"
            out.append(line)
            current_index += 1
            continue

        # ------------------------------------------------------------------
        # 2) No inline info at all (unknown result, but had ELF info)
        # ------------------------------------------------------------------
        if not frame.inlines:
            file_str = "??"
            line = f"{indent}#{current_index} {frame.address} in ?? at {file_str}"
            # Here file is always effectively unknown, so hint is allowed.
            hint = _build_elf_hint(frame)
            if hint:
                line = f"{line} {hint}"
            out.append(line)
            current_index += 1
            continue

        # ------------------------------------------------------------------
        # 3) We have inline info. First inline frame decides hint.
        # ------------------------------------------------------------------
        func0, fl0 = frame.inlines[0]
        func0 = func0 or "??"
        fl0 = fl0 or "??"

        attach_hint = _is_unknown_file(fl0)
        hint_for_first = _build_elf_hint(frame) if attach_hint else None

        # ------------------------------------------------------------------
        # 4) First inline frame
        # ------------------------------------------------------------------
        first = f"{indent}#{current_index} {frame.address} in {func0} at {fl0}"
        if hint_for_first:
            first = f"{first} {hint_for_first}"
        out.append(first)
        current_index += 1

        # ------------------------------------------------------------------
        # 5) Additional inline frames → numbered like normal frames
        # ------------------------------------------------------------------
        for func, fl in frame.inlines[1:]:
            func = func or "??"
            fl = fl or "??"
            line = f"{indent}#{current_index} {frame.address} in {func} at {fl}"
            out.append(line)
            current_index += 1

        # Note: for inline cases we only attach the hint to the first line,
        #       not to every inline frame.

    out.append("")  # blank line between traces
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
    "format_symbolized_trace",
    "format_all_traces",
    "write_formatted_traces_to_file",
]