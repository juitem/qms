

#!/usr/bin/env python3
"""
symbolizer.py

High-level symbolization workflow for QSS.

Responsibilities:
  - Take parsed StackTrace + Frame objects
  - Resolve ELF + debug paths using resolver.py
  - Execute addr2line (via addr2line_runner.py)
  - Attach symbolized info back to frames
  - ThreadPool support for faster symbolization

This module does NOT parse stack dumps.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from parser import Frame, StackTrace
from resolver import resolve_pair
from addr2line_runner import run_addr2line, Addr2lineResult


LOG = logging.getLogger("symbolizer")


# ---------------------------------------------------------------------------
# Data model for symbolized frame
# ---------------------------------------------------------------------------

@dataclass
class SymbolizedFrame:
    """
    Represents symbolized data for a frame.

    The original stack dump address is preserved.
    Inline frames can exist as a list.
    """
    trace_index: int
    frame_index: int
    address: str
    inlines: List[Tuple[str, str]]  # list of (function, file:line)
    raw_line: str


@dataclass
class SymbolizedTrace:
    """
    Represents a fully symbolized stack trace.
    """
    trace_index: int
    frames: List[SymbolizedFrame]
    preamble: List[str]


# ---------------------------------------------------------------------------
# Core symbolization logic
# ---------------------------------------------------------------------------

def _symbolize_frame(
    frame: Frame,
    rootfs: Path,
    debug_root: Path,
) -> SymbolizedFrame:
    """
    Symbolize a single frame:
      - resolve ELF + debug paths
      - run addr2line
    """
    if frame.obj_path:
        real_elf, debug_elf = resolve_pair(
            rootfs=rootfs,
            debug_root=debug_root,
            elf_path=frame.obj_path,
            build_id=frame.build_id,
        )
    else:
        real_elf, debug_elf = (None, None)

    if not real_elf:
        LOG.warning("ELF not found for %s", frame.obj_path)
        return SymbolizedFrame(
            trace_index=frame.trace_index,
            frame_index=frame.frame_index,
            address=frame.address,
            inlines=[("??", "??")],
            raw_line=frame.raw_line,
        )

    result: Addr2lineResult = run_addr2line(
        addr=frame.address,
        elf_file=real_elf,
        debug_file=debug_elf,
        prefer_debug=True,
    )

    return SymbolizedFrame(
        trace_index=frame.trace_index,
        frame_index=frame.frame_index,
        address=frame.address,
        inlines=result.frames,
        raw_line=frame.raw_line,
    )


def symbolize_trace(
    trace: StackTrace,
    rootfs: Path,
    debug_root: Path,
    workers: int = 4,
) -> SymbolizedTrace:
    """
    Symbolize all frames in a single StackTrace using a thread pool.
    """
    LOG.info("Symbolizing trace %d with %d frames", trace.trace_index, len(trace.frames))

    frames_out: List[SymbolizedFrame] = []

    with ThreadPoolExecutor(max_workers=workers) as ex:
        fut_map = {
            ex.submit(_symbolize_frame, f, rootfs, debug_root): f
            for f in trace.frames
        }

        for fut in as_completed(fut_map):
            sym = fut.result()
            frames_out.append(sym)

    frames_out.sort(key=lambda x: x.frame_index)

    return SymbolizedTrace(
        trace_index=trace.trace_index,
        frames=frames_out,
        preamble=trace.preamble,
    )


def symbolize_all(
    traces: List[StackTrace],
    rootfs: Path,
    debug_root: Path,
    workers: int = 4,
) -> List[SymbolizedTrace]:
    """
    Symbolize a list of StackTrace objects.
    """
    out: List[SymbolizedTrace] = []
    for t in traces:
        out.append(symbolize_trace(t, rootfs, debug_root, workers))
    return out


__all__ = [
    "SymbolizedFrame",
    "SymbolizedTrace",
    "symbolize_trace",
    "symbolize_all",
]