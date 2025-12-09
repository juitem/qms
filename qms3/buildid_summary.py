#!/usr/bin/env python3
"""
buildid_summary.py

Summarize ELF Build IDs seen in stack logs, and optionally enrich them
by reading Build IDs from the rootfs ELF files.

Usage example:

  # Human-readable summary to stdout
  python3 ./qms3/buildid_summary.py \
      --rootfs ./download/img/ROOTFS \
      ./sample.log

  # With mismatch check and JSON/CSV output
  python3 ./qms3/buildid_summary.py \
      --rootfs ./download/img/ROOTFS \
      --check-mismatch \
      --output-json buildids.json \
      --output-csv  buildids.csv \
      ./sample.log ./other.log

Options:

  --check-mismatch
      Also read Build IDs for entries that already have a BuildId in the log
      and report mismatches as "MISMATCH". This is disabled by default to
      avoid unnecessary overhead.

  --output-json PATH
      Write the summary as a JSON array to PATH.

  --output-csv PATH
      Write the summary as a CSV file to PATH.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from parser import (
    StackTrace,
    parse_stack_file,
    collect_unique_elf_build_ids,
)


@dataclass
class ElfBuildIdInfo:
    path: str
    log_build_id: Optional[str] = None
    found_build_id: Optional[str] = None
    mismatch: bool = False

    @property
    def effective_build_id(self) -> Optional[str]:
        """
        The BuildId value that should be used for lookups:

          1) log_build_id if present
          2) found_build_id if log_build_id is None
          3) None otherwise
        """
        if self.log_build_id:
            return self.log_build_id
        if self.found_build_id:
            return self.found_build_id
        return None

    @property
    def tags(self) -> List[str]:
        """
        Tag list for this entry:

          LOG      - BuildId came from the log
          FOUND    - BuildId was missing in the log but found in rootfs
          MISMATCH - LOG and FOUND differ (only when --check-mismatch is used)
        """
        tags: List[str] = []

        if self.log_build_id:
            tags.append("LOG")

        if self.found_build_id and self.log_build_id is None:
            tags.append("FOUND")

        if self.mismatch:
            tags.append("MISMATCH")

        return tags


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_build_id_from_elf(elf_path: Path) -> Optional[str]:
    """
    Read Build ID from an ELF file using 'readelf -n'.

    Returns:
        Hex string of Build ID, or None if not found or on error.
    """
    if not elf_path.is_file():
        return None

    try:
        # Use readelf -n to dump notes and look for "Build ID:" lines.
        proc = subprocess.run(
            ["readelf", "-n", str(elf_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except Exception:
        return None

    if proc.returncode != 0:
        return None

    for line in proc.stdout.splitlines():
        line = line.strip()
        # Typical line: "Build ID: 3cc7f3e9255e3f40d1194c5d3814cfc74dc4a163"
        if line.startswith("Build ID: "):
            return line[len("Build ID: ") :].strip()

    return None


def _load_traces_from_files(stack_paths: Iterable[Path]) -> List[StackTrace]:
    """
    Load StackTrace objects from a list of stack log paths.

    Missing files are skipped with a warning.
    """
    traces: List[StackTrace] = []
    for path in stack_paths:
        if not path.is_file():
            print(f"[WARN] Stack file not found: {path}", file=sys.stderr)
            continue
        traces.extend(parse_stack_file(path))
    return traces


def _build_summary(
    traces: List[StackTrace],
    rootfs: Path,
    check_mismatch: bool = False,
) -> Dict[str, ElfBuildIdInfo]:
    """
    Build a summary mapping ELF path -> ElfBuildIdInfo.

    - log_build_id: BuildId as seen in the log (may be None).
    - found_build_id: BuildId read from rootfs ELF when log_build_id is None,
      or when check_mismatch is True (to verify).
    """
    pairs = collect_unique_elf_build_ids(traces)
    summary: Dict[str, ElfBuildIdInfo] = {}

    # 1) Initialize from log data
    for path, log_bid in pairs:
        info = ElfBuildIdInfo(path=path, log_build_id=log_bid)
        summary[path] = info

    # 2) Enrich from rootfs
    for path, info in summary.items():
        need_read = False

        if info.log_build_id is None:
            # If log has no BuildId, always try to enrich from rootfs.
            need_read = True
        elif check_mismatch:
            # If mismatch check is requested, read for all entries with log_bid.
            need_read = True

        if not need_read:
            continue

        # Resolve ELF path inside the rootfs.
        # In the log, paths are absolute like "/usr/lib64/libfoo.so".
        # We simply join rootfs with the stripped leading '/'.
        elf_path = rootfs / info.path.lstrip("/")

        found_bid = _read_build_id_from_elf(elf_path)
        info.found_build_id = found_bid

        if check_mismatch and info.log_build_id and found_bid:
            if info.log_build_id != found_bid:
                info.mismatch = True

    return summary


def _format_summary_line(info: ElfBuildIdInfo) -> str:
    """
    Format one ElfBuildIdInfo into a human-readable line.
    """
    path = info.path
    effective = info.effective_build_id
    build_id_str = effective if effective is not None else "None"

    tag_str = ""
    tags = info.tags
    if tags:
        tag_str = "  " + ",".join(tags)

    return f"{path:<60}  BuildId: {build_id_str}{tag_str}"


def print_summary(
    summary: Dict[str, ElfBuildIdInfo],
) -> None:
    """
    Print summary lines sorted by ELF path.
    """
    for path in sorted(summary.keys()):
        line = _format_summary_line(summary[path])
        print(line)


def summary_to_dicts(summary: Dict[str, ElfBuildIdInfo]) -> List[dict]:
    """
    Convert summary mapping into a list of dictionaries suitable for JSON/CSV.
    """
    rows: List[dict] = []
    for path in sorted(summary.keys()):
        info = summary[path]
        row = {
            "path": info.path,
            "log_build_id": info.log_build_id,
            "found_build_id": info.found_build_id,
            "effective_build_id": info.effective_build_id,
            "mismatch": info.mismatch,
            "tags": ",".join(info.tags) if info.tags else "",
        }
        rows.append(row)
    return rows


def write_json(summary: Dict[str, ElfBuildIdInfo], out_path: Path) -> None:
    rows = summary_to_dicts(summary)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)


def write_csv(summary: Dict[str, ElfBuildIdInfo], out_path: Path) -> None:
    rows = summary_to_dicts(summary)
    if not rows:
        # Still create an empty CSV with headers.
        fieldnames = [
            "path",
            "log_build_id",
            "found_build_id",
            "effective_build_id",
            "mismatch",
            "tags",
        ]
    else:
        fieldnames = list(rows[0].keys())

    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize and enrich ELF Build IDs from stack logs."
    )
    parser.add_argument(
        "stacks",
        nargs="+",
        help="Stack log files to parse.",
    )
    parser.add_argument(
        "--rootfs",
        required=True,
        help="Path to the rootfs directory (used to read ELF Build IDs).",
    )
    parser.add_argument(
        "--check-mismatch",
        action="store_true",
        help=(
            "Also verify BuildIds from rootfs for entries that already have "
            "a BuildId in the log, and mark mismatches as 'MISMATCH'. "
            "Disabled by default for performance."
        ),
    )
    parser.add_argument(
        "--output-json",
        help="Write summary as JSON to this path.",
    )
    parser.add_argument(
        "--output-csv",
        help="Write summary as CSV to this path.",
    )

    args = parser.parse_args()

    stack_paths = [Path(p) for p in args.stacks]
    rootfs = Path(args.rootfs)

    traces = _load_traces_from_files(stack_paths)
    summary = _build_summary(
        traces=traces,
        rootfs=rootfs,
        check_mismatch=args.check_mismatch,
    )

    # Always print human-readable summary to stdout.
    print_summary(summary)

    # Optionally emit machine-readable outputs.
    if args.output_json:
        write_json(summary, Path(args.output_json))
    if args.output_csv:
        write_csv(summary, Path(args.output_csv))


if __name__ == "__main__":
    main()