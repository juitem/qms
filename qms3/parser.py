#!/usr/bin/env python3
"""
parser.py

Stack log parser for QSS.

Responsibilities:
  - Parse raw stack log files into structured StackTrace / StackFrame objects.
  - Extract:
      * frame index (#N)
      * address (0x...)
      * ELF path + offset from "(/path/lib.so+0xOFF)" part
      * BuildId from "(BuildId: XXXXX...)" if present
      * raw_line (original text of the frame line)
  - Split a file into multiple traces when "#0 ..." appears again.

Notes:
  - Non-frame lines are kept as 'preamble' before the first frame of each
    trace. Those preambles can be re-attached when formatting / merging.
  - We deliberately do NOT try to parse function name or "file:line" from
    the raw line here. That is handled by addr2line + symbolizer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

# Example tail:
#   (/usr/lib64/libc.so.6+0xdca48) (BuildId: 12345abced...)
#
# BuildId part is optional; when absent, we still capture path + offset.
PATH_OFFSET_BUILDID_RE = re.compile(
    r"\((?P<path>/[^+]+)\+(?P<offset>0x[0-9a-fA-F]+)\)\s*"
    r"(?:\(BuildId: (?P<buildid>[0-9A-Fa-f]+)\))?"
)

# Frame line:
#   "#3 0x1ffff9de0d58 (/usr/lib64/libfoo.so+0x1234) (BuildId: ...)"
#   "#10 0xaaaa6a36594 in _utc_decode_from_file src/utc-image.c:167"
FRAME_LINE_RE = re.compile(
    r"""
    ^\s*
    \#(?P<index>\d+)          # '#<index>'
    \s+
    (?P<addr>0x[0-9a-fA-F]+)  # '0x...'
    (?P<rest>.*)              # the rest of the line
    $
    """,
    re.VERBOSE,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class StackFrame:
    """
    Single raw stack frame as parsed from the log.

    Fields:
        raw_line:  Original line text (without trailing newline).
        index:     Frame index parsed from '#<index>'.
        address:   Address string (e.g. '0x1ffff9de0d58').
        obj_path:  ELF path from '(/path/lib.so+0xOFF)', if present.
        obj_offset:Offset from the same parenthesis, e.g. '0x1234', if present.
        build_id:  BuildId from '(BuildId: XXXXX...)', if present; else None.
    """
    raw_line: str
    index: int
    address: str
    obj_path: Optional[str] = None
    obj_offset: Optional[str] = None
    build_id: Optional[str] = None
Frame=StackFrame


@dataclass
class StackTrace:
    """
    Collection of frames representing a single logical stack trace.

    Fields:
        preamble:
            Lines that appear before the first frame of this trace in the log.
            These are preserved so that we can re-attach them when formatting.
        frames:
            List of StackFrame objects for this trace.
    """
    preamble: List[str] = field(default_factory=list)
    frames: List[StackFrame] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_frame_line(line: str) -> Optional[StackFrame]:
    """
    Try to parse a single line as a stack frame.

    Returns:
        StackFrame if the line matches a '#<n> 0x...' pattern; otherwise None.
    """
    m = FRAME_LINE_RE.match(line)
    if not m:
        return None

    idx_str = m.group("index")
    addr = m.group("addr")
    rest = m.group("rest") or ""

    try:
        index = int(idx_str)
    except ValueError:
        # Should not happen if regex is correct.
        return None

    obj_path: Optional[str] = None
    obj_offset: Optional[str] = None
    build_id: Optional[str] = None

    # Look for "(/path/lib.so+0xOFF) (BuildId: XXXX...)" pattern in the tail.
    m2 = PATH_OFFSET_BUILDID_RE.search(rest)
    if m2:
        obj_path = m2.group("path")
        obj_offset = m2.group("offset")
        build_id = m2.group("buildid")

    return StackFrame(
        raw_line=line.rstrip("\n"),
        index=index,
        address=addr,
        obj_path=obj_path,
        obj_offset=obj_offset,
        build_id=build_id,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_stack_file(path: Path) -> List[StackTrace]:
    """
    Parse a stack log file into a list of StackTrace objects.

    Splitting rules:
      - We scan the file line by line.
      - When we encounter a frame line with '#0 ...' and we already have
        frames collected for the current trace, we:
          * close the current trace,
          * start a new trace and treat any accumulated non-frame lines
            as its preamble.
      - Lines that are not frame lines and appear before the first frame
        of a trace are stored as preamble for that trace.
      - Lines that are not frame lines and appear after we started collecting
        frames are ignored here; they are expected to be preserved by the
        outer merge logic, which edits the original file rather than only
        using these traces.
    """
    traces: List[StackTrace] = []
    current_preamble: List[str] = []
    current_frames: List[StackFrame] = []

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            # Keep the original line string (with trailing newline)
            stripped_line = line.rstrip("\n")

            frame = _parse_frame_line(stripped_line)
            if frame is None:
                # Non-frame line
                if not current_frames:
                    # This belongs to the preamble of the next trace.
                    current_preamble.append(stripped_line)
                else:
                    # We are in the middle of frames; for now we ignore
                    # such lines here. The merge step works on the original
                    # file and will preserve them there.
                    pass
                continue

            # We have a frame line.
            if frame.index == 0 and current_frames:
                # Start of a new trace: close the previous one.
                traces.append(
                    StackTrace(
                        preamble=current_preamble,
                        frames=current_frames,
                    )
                )
                current_preamble = []
                current_frames = []

            current_frames.append(frame)

    # Flush the last trace if any frames were collected.
    if current_frames:
        traces.append(
            StackTrace(
                preamble=current_preamble,
                frames=current_frames,
            )
        )

    return traces


def collect_unique_elf_build_ids(
    traces: Iterable[StackTrace],
) -> Set[Tuple[str, Optional[str]]]:
    """
    Collect unique (ELF path, BuildId) pairs from all traces.

    Used by '--summary' to print a list like:
        /usr/lib64/libfoo.so    BuildId:abcd1234...
        /usr/lib64/libbar.so    BuildId:None
    """
    pairs: Set[Tuple[str, Optional[str]]] = set()

    for trace in traces:
        for frame in trace.frames:
            if frame.obj_path:
                pairs.add((frame.obj_path, frame.build_id))

    return pairs


__all__ = [
    "StackFrame",
    "Frame"
    "StackTrace",
    "parse_stack_file",
    "collect_unique_elf_build_ids",
]