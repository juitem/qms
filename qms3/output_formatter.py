#!/usr/bin/env python3
"""
output_formatter.py

Formatting utilities for QSS.

Responsibilities:
  - Take SymbolizedTrace / SymbolizedFrame objects.
  - Produce human-readable text lines.
  - Handle inline frames in a compact way.
  - Keep the convention: "#N ADDRESS in FUNC at FILE:LINE".

Rules:

1) The part right after "#N" is always the original address from the log:
       "#N <frame.address> in ..."

2) The ELF hint in parentheses is ONLY for ELF path + offset:
       "(/path/to/lib.so+0xOFFSET) (BuildId: XXXXX...)"
   where:
       - path    = frame.obj_path
       - OFFSET  = extracted pure "0x..." from frame.obj_offset
   No "#n", no full frame string, no absolute address goes into the hint.

3) If the original raw line is already fully symbolized like:
       "#N 0xADDR in FUNC src/file.c:167"
   we keep it as-is and only rewrite the frame index.

4) If the original raw line is only partially decoded like:
       "#N 0xADDR in FUNC (/path/lib.so+0xOFFSET) (BuildId: ...)"
   we treat it as NOT fully symbolized and rebuild it using our
   addr2line results (file:line, inlines, etc.).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List

from symbolizer import SymbolizedTrace, SymbolizedFrame


# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

# Matches "#<n>" prefix at the start of a frame line.
_FRAME_PREFIX_RE = re.compile(r"^(\s*)#\d+(\s+)(.*)$")

# "file:line" at the end, e.g. "foo.c:123", "foo.c:?"
_FILELINE_AT_END_RE = re.compile(r":[0-9?]+(\s*)$")

# Extract "0x..." substring from any string (for offsets)
_HEX_OFFSET_RE = re.compile(r"0x[0-9a-fA-F]+")


def _looks_fully_symbolized(raw_line: str) -> bool:
    """
    Heuristic: treat the line as 'already fully symbolized' only if:

      - it contains " in " (function name part), AND
      - it has a trailing "file:line" style pattern like "foo.c:123"
        or "foo.c:?".

    In particular, lines like:
        "#5 0xADDR in func (/path/lib.so+0xOFFSET) (BuildId: ...)"
    are NOT considered fully symbolized and should be rebuilt from our
    symbolization results, because they do not have file:line at the end.
    """
    s = raw_line.strip()
    if " in " not in s:
        return False

    # We only consider it fully symbolized if it ends with a "file:line"-style pattern.
    if _FILELINE_AT_END_RE.search(s) is None:
        return False

    return True


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _leading_indent(raw_line: str) -> str:
    """
    Return leading whitespace characters of raw_line.
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


def _normalize_offset(raw_offset: str | None) -> str | None:
    """
    Normalize an offset string to a pure '0xHEX...' form.

    Examples:
        "#0 0x1fdb8"   -> "0x1fdb8"
        "0x1234"       -> "0x1234"
        " offset=0x9"  -> "0x9"
        "" / None      -> None

    Any extra stuff (like '#0 ') is ignored.
    """
    if not raw_offset:
        return None
    m = _HEX_OFFSET_RE.search(raw_offset)
    if not m:
        return None
    return m.group(0)


def _build_elf_hint(frame: SymbolizedFrame) -> str | None:
    """
    Build a hint string from obj_path / obj_offset / build_id, e.g.:

        "(/usr/apps/.../bin/app+0x1234) (BuildId: abcdef...)"

    VERY IMPORTANT:
      - We only use frame.obj_path (ELF path) and frame.obj_offset (offset).
      - Offset is normalized to a pure "0x..." form.
      - We NEVER put "#n", full frame strings, or absolute addresses here.
    """
    if not frame.obj_path:
        return None

    norm_offset = _normalize_offset(frame.obj_offset)

    if norm_offset:
        base = f"({frame.obj_path}+{norm_offset})"
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

    Already-decoded rule:
      - If frame.raw_line already looks like a fully symbolized frame:
            "#N 0xADDR in FUNC file.c:123"
        we treat it as "final" and only rewrite the index.
    """
    out: List[str] = []

    if include_preamble and trace.preamble:
        out.extend(trace.preamble)

    current_index = 0

    # frames are expected to be sorted by frame_index in SymbolizedTrace
    for frame in trace.frames:
        raw = frame.raw_line or ""
        indent = _leading_indent(raw)

        # ------------------------------------------------------------------
        # 0) If this line already looks fully symbolized
        #    ("#N 0xADDR in FUNC file:line"), keep it as-is and
        #    only rewrite the frame index.
        # ------------------------------------------------------------------
        if raw and _looks_fully_symbolized(raw):
            out.append(_rewrite_raw_line_with_index(raw, current_index))
            current_index += 1
            continue

        # ------------------------------------------------------------------
        # 1) Raw-decoded frame with no ELF info and no inlines:
        #    keep the original content, but rewrite the "#N" number.
        # ------------------------------------------------------------------
        if not frame.obj_path and not frame.inlines:
            if raw:
                out.append(_rewrite_raw_line_with_index(raw, current_index))
                current_index += 1
                continue
            # Fallback if somehow raw_line is empty
            line = f"{indent}#{current_index} {frame.address} in ?? at ??"
            out.append(line)
            current_index += 1
            continue

        # ------------------------------------------------------------------
        # 2) Has ELF info but no inline result from addr2line.
        #    We do not know file:line, but we can show an ELF hint.
        # ------------------------------------------------------------------
        if not frame.inlines:
            file_str = "??"
            line = f"{indent}#{current_index} {frame.address} in ?? at {file_str}"
            hint = _build_elf_hint(frame)
            if hint:
                line = f"{line} {hint}"
            out.append(line)
            current_index += 1
            continue

        # ------------------------------------------------------------------
        # 3) Has inline info from addr2line.
        # ------------------------------------------------------------------
        func0, fl0 = frame.inlines[0]
        func0 = func0 or "??"
        fl0 = fl0 or "??"

        attach_hint = _is_unknown_file(fl0)
        hint_for_first = _build_elf_hint(frame) if attach_hint else None

        # First inline frame
        first = f"{indent}#{current_index} {frame.address} in {func0} at {fl0}"
        if hint_for_first:
            first = f"{first} {hint_for_first}"
        out.append(first)
        current_index += 1

        # Additional inline frames → numbered like normal frames
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