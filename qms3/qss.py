#!/usr/bin/env python3
"""
qss.py

Main entry point for the Quick Stack Symbolizer (QSS).

Responsibilities:
  - Load and parse stack dump files via parser.py
  - Collect unique (ELF, Build-id) pairs (summary mode)
  - Symbolize frames via symbolizer.py + addr2line_runner.py
  - Optionally merge symbolized traces back into the original log
  - Provide CLI interface

This version supports:
  - Input as a single stack file
  - Input as a directory containing multiple stack files (non-recursive)
  - --merge-output as a directory; each input file is merged to a file
    with the same basename under that directory.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import List

from parser import (
    parse_stack_file,
    collect_unique_elf_build_ids,
    StackTrace,
)
from symbolizer import symbolize_all, SymbolizedTrace
from output_formatter import (
    format_all_traces,
    write_formatted_traces_to_file,
)
from merge_original import merge_original_file


LOG = logging.getLogger("qss")


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Quick Stack Symbolizer (QSS) - stack dump parser and symbolization core.",
    )
    p.add_argument(
        "input",
        metavar="STACK_PATH",
        help="Path to stack dump file or directory containing stack files.",
    )
    p.add_argument(
        "--rootfs",
        required=True,
        help="Path to rootfs that contains the ELF files (e.g. extracted Tizen image).",
    )
    p.add_argument(
        "--debug-root",
        help=(
            "Root directory for debug files. "
            "If not set, defaults to <rootfs>/usr/lib/debug/.build-id"
        ),
    )
    p.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of worker threads for addr2line (default: 4).",
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging.",
    )
    p.add_argument(
        "--summary",
        action="store_true",
        help="Print unique ELF/Build-id summary only (no symbolization).",
    )
    p.add_argument(
        "--output",
        help=(
            "Write symbolized stack traces (stack only) to this file. "
            "Only used when --merge-output is not specified."
        ),
    )
    p.add_argument(
        "--merge-output",
        metavar="DIR",
        help=(
            "Directory to write merged logs. "
            "For each input file, a file with the same basename is created "
            "under this directory containing the original log with "
            "symbolized stacks merged in."
        ),
    )
    return p


# ---------------------------------------------------------------------------
# Input parsing helpers (for summary / stack-only output)
# ---------------------------------------------------------------------------

def _parse_from_file(path: Path) -> List[StackTrace]:
    LOG.info("Processing stack file: %s", path)
    traces = parse_stack_file(path)
    LOG.info("Parsed %d traces from %s", len(traces), path)
    return traces


def _parse_from_dir(path: Path) -> List[StackTrace]:
    """
    Parse all regular files directly under the given directory (non-recursive)
    and return a single list of StackTrace objects.

    Trace indices are renumbered globally from 0 after combining.
    This is used for summary and stack-only output.
    """
    LOG.info("Processing stack directory: %s", path)
    all_traces: List[StackTrace] = []

    files = sorted(p for p in path.iterdir() if p.is_file())
    if not files:
        LOG.warning("No regular files found in directory: %s", path)

    for f in files:
        LOG.info("Parsing stack file: %s", f)
        traces = parse_stack_file(f)
        LOG.info("Parsed %d traces from %s", len(traces), f)
        all_traces.extend(traces)

    # Renumber trace_index globally to avoid collisions across files.
    current_index = 0
    for trace in all_traces:
        trace.trace_index = current_index
        for frame in trace.frames:
            frame.trace_index = current_index
        current_index += 1

    LOG.info("Total traces from directory %s: %d", path, len(all_traces))
    return all_traces


def load_traces_from_path(path: Path) -> List[StackTrace]:
    """
    Load stack traces from a file or directory for summary / stack-only output.
    """
    if path.is_file():
        return _parse_from_file(path)
    if path.is_dir():
        return _parse_from_dir(path)

    LOG.error("Input path is neither file nor directory: %s", path)
    raise SystemExit(1)


def print_summary(traces: List[StackTrace]) -> None:
    pairs = collect_unique_elf_build_ids(traces)
    LOG.info("Unique ELF + Build-id entries: %d", len(pairs))
    for elf_path, build_id in sorted(pairs):
        print(f"{elf_path}\tBuildId:{build_id or 'None'}")


# ---------------------------------------------------------------------------
# Merge mode helpers
# ---------------------------------------------------------------------------

def _ensure_merge_dir(dir_path: Path) -> Path:
    """
    Ensure that the given path exists and is a directory.
    """
    if dir_path.exists():
        if not dir_path.is_dir():
            LOG.error("--merge-output must be a directory, but got a file: %s", dir_path)
            raise SystemExit(1)
    else:
        LOG.info("Creating merge-output directory: %s", dir_path)
        dir_path.mkdir(parents=True, exist_ok=True)
    return dir_path


def _merge_single_file(
    stack_file: Path,
    rootfs: Path,
    debug_root: Path,
    workers: int,
    out_dir: Path,
) -> None:
    """
    Symbolize and merge a single stack file, writing the result to
    out_dir / stack_file.name.
    """
    LOG.info("Merging single file: %s", stack_file)
    traces = parse_stack_file(stack_file)
    if not traces:
        LOG.warning("No stack traces found in %s; copying original content is not handled here.", stack_file)
        return

    sym_traces: List[SymbolizedTrace] = symbolize_all(
        traces,
        rootfs=rootfs,
        debug_root=debug_root,
        workers=workers,
    )

    out_path = out_dir / stack_file.name
    LOG.info("Writing merged log: %s", out_path)
    merge_original_file(stack_file, sym_traces, out_path)


def _merge_from_dir(
    stack_dir: Path,
    rootfs: Path,
    debug_root: Path,
    workers: int,
    out_dir: Path,
) -> None:
    """
    Symbolize and merge all regular files directly under stack_dir.
    Each merged result is written to out_dir / <basename>.
    """
    LOG.info("Merging directory: %s", stack_dir)
    files = sorted(p for p in stack_dir.iterdir() if p.is_file())
    if not files:
        LOG.warning("No regular files found in directory: %s", stack_dir)
        return

    for f in files:
        _merge_single_file(f, rootfs, debug_root, workers, out_dir)


def run_symbolization(
    stack_path: Path,
    rootfs: Path,
    debug_root: Path,
    workers: int,
    output: str | None,
    merge_output: str | None,
) -> None:
    # Merge mode: write merged logs into a directory, preserving file names.
    if merge_output:
        out_dir = _ensure_merge_dir(Path(merge_output))

        if stack_path.is_file():
            _merge_single_file(stack_path, rootfs, debug_root, workers, out_dir)
        elif stack_path.is_dir():
            _merge_from_dir(stack_path, rootfs, debug_root, workers, out_dir)
        else:
            LOG.error("Input path is neither file nor directory: %s", stack_path)
            raise SystemExit(1)

        return

    # Non-merge mode: summary / stack-only output.
    traces = load_traces_from_path(stack_path)
    if not traces:
        LOG.warning("No stack traces found in %s", stack_path)
        return

    sym_traces: List[SymbolizedTrace] = symbolize_all(
        traces,
        rootfs=rootfs,
        debug_root=debug_root,
        workers=workers,
    )

    if output:
        out_path = Path(output)
        LOG.info("Writing symbolized stacks to: %s", out_path)
        write_formatted_traces_to_file(
            sym_traces,
            out_path,
            include_preamble=False,
        )
        return

    # Default: print symbolized stacks (stack-only) to stdout
    lines = format_all_traces(sym_traces, include_preamble=False)
    print("\n".join(lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = build_argparser().parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[%(levelname)s] %(message)s",
    )

    stack_path = Path(args.input)
    if not stack_path.exists():
        LOG.error("Input path does not exist: %s", stack_path)
        raise SystemExit(1)

    rootfs = Path(args.rootfs)
    if not rootfs.is_dir():
        LOG.error("rootfs is not a directory: %s", rootfs)
        raise SystemExit(1)

    if args.debug_root:
        debug_root = Path(args.debug_root)
    else:
        debug_root = rootfs / "usr" / "lib" / "debug" / ".build-id"

    if args.summary:
        traces = load_traces_from_path(stack_path)
        print_summary(traces)
        return

    if args.merge_output and args.output:
        LOG.warning("--merge-output is specified; ignoring --output.")

    if not args.output and not args.merge_output:
        LOG.info("No --output or --merge-output specified; printing to stdout.")

    run_symbolization(
        stack_path=stack_path,
        rootfs=rootfs,
        debug_root=debug_root,
        workers=args.workers,
        output=args.output,
        merge_output=args.merge_output,
    )


if __name__ == "__main__":
    main()