#!/usr/bin/env python3
"""
qss.py

Main entry point for the Quick Stack Symbolizer (QSS).

Responsibilities:
  - Load and parse stack dump files via parser.py
  - Optionally print unique (ELF, Build-id) pairs (summary mode)
  - Resolve ELF and debug paths and run addr2line (symbolize mode)
  - Format and print/write symbolized stack traces
  - Provide a simple CLI interface
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List

from parser import (
    parse_stack_file,
    collect_unique_elf_build_ids,
    StackTrace,
)
from symbolizer import symbolize_all
from output_formatter import (
    format_all_traces,
    write_formatted_traces_to_file,
)


LOG = logging.getLogger("qss")


# ---------------------------------------------------------------------------
# Core Logic
# ---------------------------------------------------------------------------

def process_stack_file(path: Path) -> List[StackTrace]:
    LOG.info("Processing stack file: %s", path)
    traces = parse_stack_file(path)
    LOG.info("Parsed %d traces", len(traces))
    return traces


def summarize_unique_elfs(traces: List[StackTrace]) -> None:
    pairs = collect_unique_elf_build_ids(traces)
    LOG.info("Unique ELF + Build-id entries: %d", len(pairs))

    for elf_path, build_id in sorted(pairs):
        print(f"{elf_path}\tBuild-id:{build_id or 'None'}")


def run_symbolization(
    traces: List[StackTrace],
    rootfs: Path,
    debug_root: Path,
    workers: int,
    output: Path | None,
) -> None:
    """
    Symbolize traces and either print them to stdout or write to a file.
    """
    LOG.info("Rootfs: %s", rootfs)
    LOG.info("Debug root: %s", debug_root)
    LOG.info("Workers: %d", workers)

    sym_traces = symbolize_all(
        traces=traces,
        rootfs=rootfs,
        debug_root=debug_root,
        workers=workers,
    )

    if output is not None:
        LOG.info("Writing symbolized traces to: %s", output)
        write_formatted_traces_to_file(sym_traces, output)
    else:
        lines = format_all_traces(sym_traces, include_preamble=True)
        for line in lines:
            print(line)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Quick Stack Symbolizer (QSS) - stack dump parser and symbolization tool."
    )
    p.add_argument(
        "input",
        metavar="STACK_FILE",
        help="Path to stack dump file.",
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
        "--rootfs",
        required=False,
        help="Rootfs directory where ELF files are located (required for symbolization).",
    )
    p.add_argument(
        "--debug-root",
        required=False,
        help="Directory for .build-id debug files. "
             "If omitted, defaults to ROOTFS/usr/lib/debug/.build-id.",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of worker threads for symbolization (default: 4).",
    )
    p.add_argument(
        "--output",
        "-o",
        required=False,
        help="Output file to write symbolized traces. If omitted, prints to stdout.",
    )
    return p


def main() -> None:
    args = build_argparser().parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[%(levelname)s] %(message)s",
    )

    path = Path(args.input)
    if not path.is_file():
        LOG.error("Input stack file does not exist or is not a file: %s", path)
        sys.exit(1)

    traces = process_stack_file(path)

    if args.summary:
        summarize_unique_elfs(traces)
        return

    if not args.rootfs:
        LOG.error("--rootfs is required for symbolization mode.")
        sys.exit(1)

    rootfs = Path(args.rootfs)
    if not rootfs.is_dir():
        LOG.error("Invalid rootfs directory: %s", rootfs)
        sys.exit(1)

    if args.debug_root:
        debug_root = Path(args.debug_root)
    else:
        debug_root = rootfs / "usr" / "lib" / "debug" / ".build-id"

    if not debug_root.is_dir():
        LOG.warning("Debug root directory does not exist: %s", debug_root)

    output_path: Path | None = Path(args.output) if args.output else None

    run_symbolization(
        traces=traces,
        rootfs=rootfs,
        debug_root=debug_root,
        workers=args.workers,
        output=output_path,
    )


if __name__ == "__main__":
    main()