#!/usr/bin/env python3
"""
symbolizer.py

High-level symbolization workflow for QSS, using addr2line in batched mode.

Responsibilities:
  - Take parsed StackTrace + Frame objects.
  - Resolve ELF + debug paths using resolver.py.
  - Execute addr2line (via addr2line_runner.py) in grouped fashion:
      * For each (real_elf, debug_elf) pair, collect all needed addresses.
      * Invoke addr2line once per pair with all addresses.
  - Attach symbolized info back to frames.
  - Optional thread-level parallelism across ELF groups.
  - In-memory caches for resolver and addr2line results.

This module does NOT parse stack dumps.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional, Tuple

from parser import Frame, StackTrace
from resolver import resolve_pair
from addr2line_runner import run_addr2line_multi, Addr2lineResult


LOG = logging.getLogger("symbolizer")


# ---------------------------------------------------------------------------
# Resolver cache
#   key: (rootfs_path, debug_root_path, elf_path, build_id or empty string)
#   val: (real_elf_path or None, debug_elf_path or None)
# ---------------------------------------------------------------------------

_resolve_cache_lock = Lock()
_resolve_cache: Dict[Tuple[str, str, str, str], Tuple[Optional[Path], Optional[Path]]] = {}


# ---------------------------------------------------------------------------
# Addr2line result cache
#   key: (real_elf_path, debug_elf_path or empty string, addr_for_lookup)
#   val: Addr2lineResult
# ---------------------------------------------------------------------------

_addr2line_cache_lock = Lock()
_addr2line_cache: Dict[Tuple[str, str, str], Addr2lineResult] = {}


@dataclass
class SymbolizedFrame:
    """
    Represents symbolized data for a frame.

    The original stack dump address is preserved.
    Inline frames can exist as a list.

    Additional fields (obj_path, obj_offset, build_id) are kept so that
    output formatters can reconstruct helpful hints like:
        (/usr/lib64/libc.so.6+0xdca48) (BuildId: abc123...)
    even when symbolization fails (e.g. "??").
    """
    trace_index: int
    frame_index: int
    address: str
    inlines: List[Tuple[str, str]]  # list of (function, file:line)
    raw_line: str
    obj_path: Optional[str] = None
    obj_offset: Optional[str] = None
    build_id: Optional[str] = None


@dataclass
class SymbolizedTrace:
    """
    Represents a fully symbolized stack trace.
    """
    trace_index: int
    frames: List[SymbolizedFrame]
    preamble: List[str]


def _resolve_elf_paths(
    frame: Frame,
    rootfs: Path,
    debug_root: Path,
) -> Tuple[Optional[Path], Optional[Path]]:
    """
    Resolve real ELF and debug ELF paths for a frame, with caching.
    """
    if not frame.obj_path:
        return (None, None)

    rootfs_key = str(rootfs)
    debug_root_key = str(debug_root)
    build_id_key = frame.build_id or ""
    elf_path_key = frame.obj_path

    cache_key = (rootfs_key, debug_root_key, elf_path_key, build_id_key)

    with _resolve_cache_lock:
        cached = _resolve_cache.get(cache_key)

    if cached is not None:
        return cached

    real_elf, debug_elf = resolve_pair(
        rootfs=rootfs,
        debug_root=debug_root,
        elf_path=frame.obj_path,
        build_id=frame.build_id,
    )

    with _resolve_cache_lock:
        _resolve_cache[cache_key] = (real_elf, debug_elf)

    return (real_elf, debug_elf)


def _make_unknown_frame(
    frame: Frame,
    trace_index: int,
    frame_index: int,
    with_question: bool = True,
) -> SymbolizedFrame:
    """
    Build a SymbolizedFrame for cases where we cannot or should not run
    addr2line (e.g. missing ELF, no info).
    """
    if with_question:
        inlines = [("??", "??:0")]
    else:
        inlines = []
    return SymbolizedFrame(
        trace_index=trace_index,
        frame_index=frame_index,
        address=frame.address,
        inlines=inlines,
        raw_line=frame.raw_line,
        obj_path=frame.obj_path,
        obj_offset=frame.obj_offset,
        build_id=frame.build_id,
    )


def _symbolize_group(
    group_index: int,
    real_elf: Path,
    debug_elf: Optional[Path],
    addr_to_indices: Dict[str, List[int]],
    trace: StackTrace,
    trace_index: int,
) -> Dict[int, SymbolizedFrame]:
    """
    Symbolize all frames for a single (real_elf, debug_elf) pair.

    addr_to_indices:
        Mapping from address-for-lookup (string) to list of frame indices
        that should share the same addr2line result.
    """
    LOG.info(
        "Symbolizing group %d: ELF=%s, debug=%s, %d unique addresses",
        group_index,
        real_elf,
        debug_elf,
        len(addr_to_indices),
    )

    addrs: List[str] = list(addr_to_indices.keys())
    try:
        addr_results: Dict[str, Addr2lineResult] = run_addr2line_multi(
            addrs=addrs,
            elf_file=real_elf,
            debug_file=debug_elf,
            prefer_debug=True,
        )
    except Exception as e:
        LOG.error(
            "run_addr2line_multi failed for ELF=%s debug=%s: %s",
            real_elf,
            debug_elf,
            e,
        )
        addr_results = {}

    out: Dict[int, SymbolizedFrame] = {}

    real_elf_key = str(real_elf)
    debug_elf_key = str(debug_elf) if debug_elf is not None else ""

    for addr in addrs:
        res = addr_results.get(addr)
        if res is None or not res.frames:
            inlines = [("??", "??:0")]
        else:
            inlines = list(res.frames)
            # Store in global addr2line cache for reuse.
            cache_key = (real_elf_key, debug_elf_key, addr)
            with _addr2line_cache_lock:
                _addr2line_cache[cache_key] = res

        for frame_index in addr_to_indices[addr]:
            frame = trace.frames[frame_index]
            sym = SymbolizedFrame(
                trace_index=trace_index,
                frame_index=frame_index,
                address=frame.address,
                inlines=inlines,
                raw_line=frame.raw_line,
                obj_path=frame.obj_path,
                obj_offset=frame.obj_offset,
                build_id=frame.build_id,
            )
            out[frame_index] = sym

    return out


def symbolize_trace(
    trace: StackTrace,
    trace_index: int,
    rootfs: Path,
    debug_root: Path,
    workers: int = 4,
) -> SymbolizedTrace:
    """
    Symbolize all frames in a single StackTrace.

    Steps:
      1) For each frame, decide whether it can be symbolized.
      2) Resolve (real_elf, debug_elf) for frames that have an obj_path.
      3) Check global addr2line cache; if hit, reuse directly.
      4) Otherwise, group frames by (real_elf, debug_elf) and address-for-lookup.
      5) For each group, call run_addr2line_multi once with all addresses.
      6) Merge results back into SymbolizedFrame list.
    """
    LOG.info("Symbolizing trace %d with %d frames", trace_index, len(trace.frames))

    # Prepare output array with placeholders.
    frames_out: List[Optional[SymbolizedFrame]] = [None] * len(trace.frames)

    # Group frames by (real_elf, debug_elf).
    group_list: List[Tuple[Path, Optional[Path], Dict[str, List[int]]]] = []
    group_index_map: Dict[Tuple[str, str], int] = {}

    for frame_index, frame in enumerate(trace.frames):
        # Frames that have no useful ELF information: do not run addr2line.
        if not frame.obj_path and not frame.build_id and not frame.obj_offset:
            frames_out[frame_index] = _make_unknown_frame(
                frame,
                trace_index=trace_index,
                frame_index=frame_index,
                with_question=False,
            )
            continue

        # Resolve ELF paths.
        real_elf, debug_elf = _resolve_elf_paths(frame, rootfs, debug_root)

        if not real_elf:
            LOG.warning("ELF not found for %s", frame.obj_path)
            frames_out[frame_index] = _make_unknown_frame(
                frame,
                trace_index=trace_index,
                frame_index=frame_index,
                with_question=True,
            )
            continue

        # Decide address for lookup: obj_offset (preferred) or raw address.
        addr_for_lookup = frame.obj_offset if frame.obj_offset else frame.address

        real_elf_key = str(real_elf)
        debug_elf_key = str(debug_elf) if debug_elf is not None else ""
        cache_key = (real_elf_key, debug_elf_key, addr_for_lookup)

        # First, try global addr2line cache.
        with _addr2line_cache_lock:
            cached_res = _addr2line_cache.get(cache_key)

        if cached_res is not None:
            # We already know the symbolization result for this ELF+addr.
            inlines = list(cached_res.frames)
            frames_out[frame_index] = SymbolizedFrame(
                trace_index=trace_index,
                frame_index=frame_index,
                address=frame.address,
                inlines=inlines,
                raw_line=frame.raw_line,
                obj_path=frame.obj_path,
                obj_offset=frame.obj_offset,
                build_id=frame.build_id,
            )
            continue

        # Not in cache → add to group for batched addr2line.
        group_key = (real_elf_key, debug_elf_key)

        if group_key not in group_index_map:
            addr_to_indices: Dict[str, List[int]] = defaultdict(list)
            addr_to_indices[addr_for_lookup].append(frame_index)
            group_list.append((real_elf, debug_elf, addr_to_indices))
            group_index_map[group_key] = len(group_list) - 1
        else:
            g_idx = group_index_map[group_key]
            _, _, addr_map = group_list[g_idx]
            addr_map.setdefault(addr_for_lookup, []).append(frame_index)

    # Symbolize each group. We can do this in parallel with a thread pool.
    if group_list:
        max_workers = max(1, workers)
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            fut_map = {}
            for g_idx, (real_elf, debug_elf, addr_map) in enumerate(group_list):
                fut = ex.submit(
                    _symbolize_group,
                    g_idx,
                    real_elf,
                    debug_elf,
                    addr_map,
                    trace,
                    trace_index,
                )
                fut_map[fut] = g_idx

            for fut in as_completed(fut_map):
                sym_map = fut.result()
                for frame_index, sym_frame in sym_map.items():
                    frames_out[frame_index] = sym_frame

    # Fill any still-missing entries with "unknown" frames.
    for frame_index, existing in enumerate(frames_out):
        if existing is None:
            frame = trace.frames[frame_index]
            frames_out[frame_index] = _make_unknown_frame(
                frame,
                trace_index=trace_index,
                frame_index=frame_index,
                with_question=True,
            )

    # Sort by frame_index to ensure stable order.
    frames_out_sorted = sorted(frames_out, key=lambda f: f.frame_index)  # type: ignore[arg-type]

    return SymbolizedTrace(
        trace_index=trace_index,
        frames=frames_out_sorted,  # type: ignore[list-item]
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
    for t_index, t in enumerate(traces):
        out.append(symbolize_trace(t, t_index, rootfs, debug_root, workers))
    return out


__all__ = [
    "SymbolizedFrame",
    "SymbolizedTrace",
    "symbolize_trace",
    "symbolize_all",
]