#!/usr/bin/env python3
"""
qss_batch.py

Simple batch runner for qss.py.

This script finds stack log files from given paths (files and/or directories)
and invokes qss.py for each file, preserving the existing qss.py behavior.

It does NOT re-implement parsing or symbolization logic; it just orchestrates
multiple qss.py runs.

New feature:
  - Parallel execution with --jobs (run multiple qss.py processes at once).

Usage examples:

  # Process a single file (almost same as calling qss.py directly)
  python3 ./qms3/qss_batch.py \
      --rootfs ./download/img/ROOTFS \
      ./sample.log

  # Process all files under ./logs/, writing merged outputs to ./logs.out/
  python3 ./qms3/qss_batch.py \
      --rootfs ./download/img/ROOTFS \
      --merge-output ./logs.out \
      ./logs/

  # Process only *.log files under ./logs/ and ./more_logs/
  python3 ./qms3/qss_batch.py \
      --rootfs ./download/img/ROOTFS \
      --merge-output ./logs.out \
      --ext .log \
      ./logs/ ./more_logs/

  # Process all files under ./logs/ using 4 parallel jobs
  python3 ./qms3/qss_batch.py \
      --rootfs ./download/img/ROOTFS \
      --merge-output ./logs.out \
      --jobs 4 \
      ./logs/
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, List


def find_stack_files(paths: Iterable[Path], ext: str) -> List[Path]:
    """
    Collect stack log files from the given paths.

    - If a path is a file, it is included directly (and ext filter is ignored).
    - If a path is a directory:
        - If ext is non-empty (e.g. ".log"), only files ending with that
          suffix are included.
        - If ext is empty (""), all regular files are included.
      Search is recursive.
    """
    results: List[Path] = []

    for path in paths:
        if path.is_file():
            results.append(path)
            continue

        if path.is_dir():
            if ext:
                # Only files with the given extension
                for p in path.rglob(f"*{ext}"):
                    if p.is_file():
                        results.append(p)
            else:
                # All regular files
                for p in path.rglob("*"):
                    if p.is_file():
                        results.append(p)
        else:
            print(
                f"[WARN] Input path does not exist or is not a file/dir: {path}",
                file=sys.stderr,
            )

    # Sort for stable processing order
    results.sort()
    return results


def run_qss_on_file(
    qss_script: Path,
    stack_file: Path,
    rootfs: Path,
    debug_root: str,
    merge_output: Path | None,
    workers: int,
    extra_args: List[str],
) -> int:
    """
    Invoke qss.py as a subprocess for a single stack file.

    Returns qss.py's return code.
    """
    cmd: List[str] = [sys.executable, str(qss_script), str(stack_file)]

    cmd.extend(["--rootfs", str(rootfs)])

    if debug_root:
        cmd.extend(["--debug-root", debug_root])

    if merge_output is not None:
        cmd.extend(["--merge-output", str(merge_output)])

    if workers > 0:
        cmd.extend(["--workers", str(workers)])

    # Pass any extra arguments through to qss.py as-is.
    cmd.extend(extra_args)

    print(f"[INFO] Running qss.py for: {stack_file}", file=sys.stderr)
    proc = subprocess.run(cmd)
    return proc.returncode


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch runner for qss.py over multiple stack log files."
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="Stack log files or directories containing stack logs.",
    )
    parser.add_argument(
        "--rootfs",
        required=True,
        help="Path to the rootfs directory used by qss.py.",
    )
    parser.add_argument(
        "--debug-root",
        default="/usr/lib/debug/.build-id",
        help="Debug root passed to qss.py (default: /usr/lib/debug/.build-id).",
    )
    parser.add_argument(
        "--merge-output",
        help=(
            "If set, qss.py will be invoked with --merge-output=<DIR>. "
            "The directory is created if it does not exist."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of worker threads for symbolization (passed to qss.py).",
    )
    parser.add_argument(
        "--ext",
        default="",
        help=(
            "When scanning directories, only files with this extension are "
            "processed. Default: process all regular files (no extension filter). "
            "Example: --ext .log"
        ),
    )
    parser.add_argument(
        "--qss-script",
        default="./qms3/qss.py",
        help="Path to qss.py script (default: ./qms3/qss.py).",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help=(
            "Number of stack files to process in parallel. "
            "Default: 1 (sequential). Set >1 to run multiple qss.py "
            "processes concurrently."
        ),
    )
    parser.add_argument(
        "--",
        dest="pass_through",
        nargs=argparse.REMAINDER,
        help=(
            "Additional arguments to pass through to qss.py after '--'. "
            "Example: -- --summary"
        ),
    )

    args = parser.parse_args()

    input_paths = [Path(p) for p in args.inputs]
    rootfs = Path(args.rootfs)
    qss_script = Path(args.qss_script)

    if not qss_script.is_file():
        print(f"[ERROR] qss.py script not found at: {qss_script}", file=sys.stderr)
        sys.exit(1)

    merge_output_dir: Path | None = None
    if args.merge_output:
        merge_output_dir = Path(args.merge_output)
        merge_output_dir.mkdir(parents=True, exist_ok=True)

    ext = args.ext
    pass_through = args.pass_through or []
    jobs = max(1, args.jobs)

    stack_files = find_stack_files(input_paths, ext=ext)

    if not stack_files:
        print("[WARN] No stack files found for the given inputs.", file=sys.stderr)
        sys.exit(0)

    print(
        f"[INFO] Found {len(stack_files)} stack file(s) to process. "
        f"Using jobs={jobs}.",
        file=sys.stderr,
    )

    overall_rc = 0

    if jobs == 1:
        # Sequential mode (default, previous behavior)
        for sf in stack_files:
            rc = run_qss_on_file(
                qss_script=qss_script,
                stack_file=sf,
                rootfs=rootfs,
                debug_root=args.debug_root,
                merge_output=merge_output_dir,
                workers=args.workers,
                extra_args=pass_through,
            )
            if rc != 0:
                overall_rc = rc
    else:
        # Parallel mode
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            future_map = {
                ex.submit(
                    run_qss_on_file,
                    qss_script,
                    sf,
                    rootfs,
                    args.debug_root,
                    merge_output_dir,
                    args.workers,
                    pass_through,
                ): sf
                for sf in stack_files
            }

            for fut in as_completed(future_map):
                sf = future_map[fut]
                try:
                    rc = fut.result()
                except Exception as e:
                    print(
                        f"[ERROR] Exception while running qss.py for {sf}: {e}",
                        file=sys.stderr,
                    )
                    rc = 1
                if rc != 0:
                    overall_rc = rc

    sys.exit(overall_rc)


if __name__ == "__main__":
    main()