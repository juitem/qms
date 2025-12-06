#!/usr/bin/env python3
"""
parser.py

Low-level parser utilities for qss (quick stack symbolizer).

This module is responsible for:
  - Parsing raw stack dump text files.
  - Detecting individual stack traces inside a file.
  - Extracting frame information such as:
      * frame index (#N)
      * raw address (0x...)
      * object file path and offset (/path/libfoo.so+0x1234)
      * build-id (if available: (Build-id:...))
  - Providing helpers to collect unique (elf_path, build_id) pairs.

The parser is intentionally kept independent from any addr2line logic.
Higher-level code (e.g. qss.py) can consume the parsed structures.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Set, Tuple


LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Frame:
    """
    Represents a single frame line in the original stack dump.

    Example input line:
        "#1 0x1ffff9de0d58 (/usr/share/.../libwakeup-engine.so+0x1fdb8) (Build-id:aa0d6e0...)"

    Fields:
        trace_index: Index of this stack trace within the file (0-based).
                     Each "#0" starts a new trace.
        frame_index: Value after '#' in the stack dump (0, 1, 2, ...)
        address:     Raw address string, e.g. "0x1ffff9de0d58"
        obj_path:    Object file path, e.g. "/usr/share/.../libwakeup-engine.so"
        obj_offset:  Offset part after '+', e.g. "0x1fdb8"
        build_id:    Build-id if present, e.g. "aa0d6e0..." (without "Build-id:")
        raw_line:    Original full line text (without trailing newline)
    """

    trace_index: int
    frame_index: int
    address: str
    obj_path: Optional[str]
    obj_offset: Optional[str]
    build_id: Optional[str]
    raw_line: str


@dataclasses.dataclass
class StackTrace:
    """
    Represents a single stack trace (#0, #1, ... lines) in the stack dump.

    Fields:
        trace_index: 0-based index of this trace inside the file.
        frames:      Ordered list of Frame objects.
        preamble:    Optional lines that appear *before* the first "#0" of this trace,
                     but logically belong to this crash (e.g. thread header).
    """

    trace_index: int
    frames: List[Frame]
    preamble: List[str]


# ---------------------------------------------------------------------------
# Regex definitions
# ---------------------------------------------------------------------------

# Matches a typical frame line:
#   "   #1 0x1ffff9de0d58 (/usr/lib64/libfoo.so+0x1234) (Build-id:abcd...)"
_FRAME_LINE_RE = re.compile(
    r"""
    ^\s*                                   # leading spaces
    \#(?P<frame_index>\d+)\s+              # '#' + frame index
    (?P<address>0x[0-9a-fA-F]+)            # address
    (?P<rest>.*)$                          # the rest of line
    """,
    re.VERBOSE,
)

# Extracts "(/path/to/lib.so+0x1234)" and optional "(Build-id:abcd...)"
_ELF_AND_BUILDID_RE = re.compile(
    r"""
    \(                                     # opening parenthesis
        (?P<elf_path>\/[^\s\)]+)           # ELF path starting with '/'
        \+0x(?P<offset>[0-9a-fA-F]+)       # '+0x' + hex offset
    \)
    (?:\s*                                 # optional spaces
        \(Build-id:(?P<build_id>[0-9a-fA-F]+)\)  # '(Build-id:...)'
    )?
    """,
    re.VERBOSE,
)


# ---------------------------------------------------------------------------
# Core parsing logic
# ---------------------------------------------------------------------------


def _parse_frame_line(
    line: str,
    current_trace_index: int,
) -> Optional[Frame]:
    """
    Parse a single stack frame line.

    Returns:
        Frame instance or None if the line is not a recognized frame line.
    """
    m = _FRAME_LINE_RE.match(line)
    if not m:
        return None

    frame_index = int(m.group("frame_index"))
    address = m.group("address")
    rest = m.group("rest") or ""

    obj_path: Optional[str] = None
    obj_offset: Optional[str] = None
    build_id: Optional[str] = None

    em = _ELF_AND_BUILDID_RE.search(rest)
    if em:
        obj_path = em.group("elf_path")
        obj_offset = "0x" + em.group("offset")
        build_id = em.group("build_id")

    frame = Frame(
        trace_index=current_trace_index,
        frame_index=frame_index,
        address=address,
        obj_path=obj_path,
        obj_offset=obj_offset,
        build_id=build_id,
        raw_line=line.rstrip("\n"),
    )
    return frame


def parse_stack_lines(lines: Iterable[str]) -> List[StackTrace]:
    """
    Parse an iterable of stack dump lines into a list of StackTrace objects.

    Rules:
      - Any line matching '#N ...' is treated as a frame.
      - A new StackTrace starts when we see a frame with frame_index == 0.
      - Lines that are not frame lines and appear between traces are added
        to the 'preamble' of the next trace (if any).

    This function is deterministic and does not do any IO by itself.
    """
    traces: List[StackTrace] = []

    current_trace_index = -1
    current_frames: List[Frame] = []
    current_preamble: List[str] = []
    pending_preamble: List[str] = []  # preamble collected between traces

    def flush_current_trace() -> None:
        nonlocal current_frames, current_preamble, current_trace_index
        if not current_frames:
            return
        traces.append(
            StackTrace(
                trace_index=current_trace_index,
                frames=current_frames,
                preamble=current_preamble,
            )
        )
        current_frames = []
        current_preamble = []

    for raw_line in lines:
        line = raw_line.rstrip("\n")
        frame = _parse_frame_line(line, current_trace_index if current_trace_index >= 0 else 0)

        if frame is None:
            # Non-frame line: accumulate as pending preamble if we are between traces,
            # or as part of current trace preamble if no frame yet.
            if current_frames:
                # This is inside an existing trace but not a frame line; we ignore it
                # as a frame but keep the raw log unchanged in higher-level code
                # if needed. For now just skip.
                LOG.debug("Non-frame line inside trace %d: %s", current_trace_index, line)
            else:
                pending_preamble.append(line)
            continue

        # If this is frame #0, we start a new trace.
        if frame.frame_index == 0:
            # Finish previous trace if any.
            flush_current_trace()
            current_trace_index = len(traces)
            # Apply any pending preamble to this new trace.
            current_preamble = pending_preamble
            pending_preamble = []
            # Rebuild frame with updated trace index.
            frame = dataclasses.replace(frame, trace_index=current_trace_index)

        elif current_trace_index < 0:
            # We saw frame index > 0 before any '#0'. Treat as trace 0 implicitly.
            current_trace_index = 0
            current_preamble = pending_preamble
            pending_preamble = []
            frame = dataclasses.replace(frame, trace_index=current_trace_index)

        current_frames.append(frame)

    # Flush the last trace.
    flush_current_trace()

    return traces


def parse_stack_file(path: str | Path, encoding: str = "utf-8") -> List[StackTrace]:
    """
    Read a stack dump file and parse it into a list of StackTrace objects.
    """
    p = Path(path)
    LOG.debug("Parsing stack file: %s", p)

    with p.open("r", encoding=encoding, errors="replace") as f:
        return parse_stack_lines(f)


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def collect_unique_elf_build_ids(traces: Iterable[StackTrace]) -> Set[Tuple[str, Optional[str]]]:
    """
    Collect unique (elf_path, build_id) pairs from a list of StackTrace objects.

    Returns:
        A set of (elf_path, build_id) tuples.
        build_id can be None if not found in the original stack line.

    Note:
        This mirrors the behavior you described earlier:
        - Lines that start with "spaces + # + number" and contain "Build-id:"
          are used to extract:
              * "파일경로" (elf_path)
              * "Build-id" (build_id)
        - Duplicates are removed via the returned set.
    """
    result: Set[Tuple[str, Optional[str]]] = set()

    for trace in traces:
        for frame in trace.frames:
            if not frame.obj_path:
                continue
            key = (frame.obj_path, frame.build_id)
            result.add(key)

    return result


__all__ = [
    "Frame",
    "StackTrace",
    "parse_stack_lines",
    "parse_stack_file",
    "collect_unique_elf_build_ids",
]
