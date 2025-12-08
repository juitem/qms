#!/usr/bin/env python3
"""
parser.py

Stack dump parser for QSS.

Responsibilities:
  - Parse raw log files that contain one or more stack traces.
  - Detect stack frames of the form:
        "#N 0xADDR (/path/lib.so+0xOFF) (BuildId:ABCD...)"
    and also already-decoded frames like:
        "#N 0xADDR in func file.c:10"
  - Produce structured StackTrace / Frame objects for symbolization.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple, Iterable, Set


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Frame:
    trace_index: int
    frame_index: int          # as found in the stack ("#N")
    address: str              # e.g. "0x0000fffff000abcd"
    obj_path: Optional[str]   # ELF path in the log, e.g. "/hal/lib64/libfoo.so"
    obj_offset: Optional[str] # e.g. "0x1fdb8"
    build_id: Optional[str]   # e.g. "aa0d6e0..."
    raw_line: str             # the original line from the log


@dataclass
class StackTrace:
    trace_index: int
    frames: List[Frame]
    preamble: List[str]


# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

# Generic frame prefix: "#<n> 0xADDR ..."
FRAME_PREFIX_RE = re.compile(
    r"^\s*#(?P<idx>\d+)\s+(?P<addr>0x[0-9a-fA-F]+)\s+(?P<rest>.*)$"
)

# Path + offset + optional BuildId in a parenthesis block:
#   (/path/lib.so+0xOFFSET) (BuildId:abcdef...)
PATH_OFFSET_BUILDID_RE = re.compile(
    r"\((?P<path>/[^+]+)\+(?P<offset>0x[0-9a-fA-F]+)\)\s*"
    r"(?:\(BuildId:(?P<buildid>[0-9A-Fa-f]+)\))?"
)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_frame_line(
    raw_line: str,
    trace_index: int,
) -> Optional[Frame]:
    """
    Try to parse a single stack frame line.

    Returns:
        Frame if the line looks like a stack frame, otherwise None.
    """
    m = FRAME_PREFIX_RE.match(raw_line)
    if not m:
        return None

    idx_str = m.group("idx")
    addr = m.group("addr")
    rest = m.group("rest")

    try:
        frame_idx = int(idx_str)
    except ValueError:
        frame_idx = 0

    # Try to find "(/path/lib.so+0xOFF) (BuildId:...)" pattern in the rest
    obj_path: Optional[str] = None
    obj_offset: Optional[str] = None
    build_id: Optional[str] = None

    m2 = PATH_OFFSET_BUILDID_RE.search(rest)
    if m2:
        obj_path = m2.group("path")
        obj_offset = m2.group("offset")
        build_id = m2.group("buildid")

    return Frame(
        trace_index=trace_index,
        frame_index=frame_idx,
        address=addr,
        obj_path=obj_path,
        obj_offset=obj_offset,
        build_id=build_id,
        raw_line=raw_line.rstrip("\n"),
    )


def parse_stack_lines(lines: Iterable[str]) -> List[StackTrace]:
    """
    Parse an iterable of lines into a list of StackTrace objects.

    Heuristic:
      - A new trace starts when we see a frame with index 0 ("#0 ...")
        or when we see the first frame after non-frame lines.
      - Frames after that belong to the same trace until we either:
          * encounter another "#0 ..." frame (new trace), or
          * reach the end of input.
      - We do not attempt to parse "preamble" lines; they are collected
        but not deeply analyzed.
    """
    traces: List[StackTrace] = []
    current_frames: List[Frame] = []
    current_preamble: List[str] = []
    current_trace_index = -1
    inside_trace = False

    for raw in lines:
        line = raw.rstrip("\n")
        frame = _parse_frame_line(line, trace_index=current_trace_index + 1)

        if frame is None:
            # Not a frame line
            if inside_trace:
                # Still part of the current trace's preamble/footer.
                current_preamble.append(line)
            else:
                # Outside any trace; standalone text.
                current_preamble.append(line)
            continue

        # We have a frame line
        if not inside_trace or frame.frame_index == 0:
            # Close previous trace if any
            if inside_trace and current_frames:
                traces.append(
                    StackTrace(
                        trace_index=current_trace_index,
                        frames=current_frames,
                        preamble=current_preamble,
                    )
                )
                current_frames = []
                current_preamble = []

            # Start new trace
            current_trace_index += 1
            inside_trace = True
            frame.trace_index = current_trace_index

        else:
            # Continuation of the same trace
            frame.trace_index = current_trace_index

        current_frames.append(frame)

    # Close last trace
    if inside_trace and current_frames:
        traces.append(
            StackTrace(
                trace_index=current_trace_index,
                frames=current_frames,
                preamble=current_preamble,
            )
        )

    return traces


def parse_stack_file(path: Path, encoding: str = "utf-8") -> List[StackTrace]:
    """
    Read a stack file and parse it into StackTrace objects.
    """
    with path.open("r", encoding=encoding, errors="replace") as f:
        lines = f.readlines()
    return parse_stack_lines(lines)


def collect_unique_elf_build_ids(traces: Iterable[StackTrace]) -> List[Tuple[str, Optional[str]]]:
    """
    Collect unique (obj_path, build_id) pairs from a list of StackTrace objects.

    Returns:
        List of (obj_path, build_id) pairs.
    """
    seen: Set[Tuple[str, Optional[str]]] = set()

    for trace in traces:
        for frame in trace.frames:
            if frame.obj_path:
                key = (frame.obj_path, frame.build_id)
                seen.add(key)

    return list(seen)


__all__ = [
    "Frame",
    "StackTrace",
    "parse_stack_file",
    "parse_stack_lines",
    "collect_unique_elf_build_ids",
]