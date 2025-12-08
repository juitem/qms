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
        metavar="STACK_FILE",
        help="Path to stack dump file.",
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
        help="Write symbolized stack traces (stack only) to this file.",
    )
    p.add_argument(
        "--merge-output",
        help=(
            "Merge symbolized traces back into the original log and "
            "write to this file. "
            "Non-stack lines from the original file are preserved."
        ),
    )
    return p


# ---------------------------------------------------------------------------
# Main processing helpers
# ---------------------------------------------------------------------------

def process_stack_file(path: Path) -> List[StackTrace]:
    LOG.info("Processing stack file: %s", path)
    traces = parse_stack_file(path)
    LOG.info("Parsed %d traces", len(traces))
    return traces


def print_summary(traces: List[StackTrace]) -> None:
    pairs = collect_unique_elf_build_ids(traces)
    LOG.info("Unique ELF + Build-id entries: %d", len(pairs))
    for elf_path, build_id in sorted(pairs):
        print(f"{elf_path}\tBuildId:{build_id or 'None'}")


def run_symbolization(
    stack_path: Path,
    rootfs: Path,
    debug_root: Path,
    workers: int,
    output: str | None,
    merge_output: str | None,
) -> None:
    traces = process_stack_file(stack_path)
    if not traces:
        LOG.warning("No stack traces found in %s", stack_path)
        return

    sym_traces: List[SymbolizedTrace] = symbolize_all(
        traces,
        rootfs=rootfs,
        debug_root=debug_root,
        workers=workers,
    )

    if merge_output:
        out_path = Path(merge_output)
        LOG.info("Merging symbolized traces into original log: %s", out_path)
        merge_original_file(stack_path, sym_traces, out_path)
        return

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
    if not stack_path.is_file():
        LOG.error("Input path is not a file: %s", stack_path)
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
        traces = process_stack_file(stack_path)
        print_summary(traces)
        return

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