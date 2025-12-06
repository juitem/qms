#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ConstructRegularDebugLink.py

Scan stack logs to collect (orig_elf, build-id) pairs, scan the whole rootfs
for *.debug files, map their Build IDs, and then create regular build-id
symlinks under <rootfs>/<debug-root>/<aa>/<rest>.debug.

Example:

  python3 ConstructRegularDebugLink.py \
      --rootfs /mnt/rootfs \
      --stacklog-dir ./logs \
      --debug-root /usr/lib/debug/.build-id \
      --verbose

Options:
  --dry-run : Do not touch filesystem, only print what would be done
  --verbose : Print detailed progress
"""

import argparse
import os
import re
import subprocess
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple


# ==========================
# Regex patterns
# ==========================

# Matches "(/usr/lib64/libfoo.so+0x1234)"
STACK_ENTRY_PATTERN = re.compile(
    r'\('
    r'(?P<path>/[^()]+?)'
    r'\+'
    r'(?P<offset>0x[0-9A-Fa-f]+)'
    r'\)'
)

# Matches "(Build-id:aa0d6e0...)" or "(Build ID: aa0d6e0...)"
BUILD_ID_PATTERN = re.compile(
    r'\('
    r'(?:[Bb]uild[- ]?[Ii][Dd])\s*:\s*'
    r'([0-9A-Fa-f]+)'
    r'\)'
)


# ==========================
# Logging helpers
# ==========================

def vprint(verbose: bool, msg: str) -> None:
    if verbose:
        print(msg)


def eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


# ==========================
# Stack log parsing
# ==========================

def collect_elf_buildids_from_file(
    path: str,
    elf_to_buildid: Dict[str, Optional[str]],
    verbose: bool,
) -> None:
    """
    Parse a single stack log file and update elf_to_buildid mapping.

    Rules:
      - For each line, find all "( /path/.../libfoo.so+0x... )"
      - Find a "(Build-id:...)" (or "Build ID:") on the same line.
      - For each orig_elf on that line, associate the build-id.
      - If the same orig_elf appears multiple times, build-id must be the same.
        Otherwise, emit a warning.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                stack_matches = list(STACK_ENTRY_PATTERN.finditer(line))
                if not stack_matches:
                    continue

                bmatch = BUILD_ID_PATTERN.search(line)
                build_id: Optional[str] = bmatch.group(1) if bmatch else None
                if build_id:
                    build_id = build_id.lower()

                for sm in stack_matches:
                    raw_path = sm.group("path")
                    # Normalize spaces in the path
                    orig_elf = " ".join(raw_path.split())

                    if orig_elf not in elf_to_buildid:
                        elf_to_buildid[orig_elf] = build_id
                    else:
                        existing = elf_to_buildid[orig_elf]
                        if build_id and existing and build_id != existing:
                            eprint(
                                f"[WARN] Build-id collision for {orig_elf}: "
                                f"{existing} vs {build_id} (file: {path})"
                            )
    except OSError as ex:
        eprint(f"[WARN] Failed to read stacklog file: {path} ({ex})")


def collect_elf_buildids(
    stacklog_file: Optional[str],
    stacklog_dir: Optional[str],
    verbose: bool,
) -> Dict[str, Optional[str]]:
    """
    Dispatch for stacklog input.

    - If stacklog_file is given, only parse that file.
    - If stacklog_dir is given, walk all files under it.
    """
    elf_to_buildid: Dict[str, Optional[str]] = {}

    if stacklog_file:
        vprint(verbose, f"[INFO] Parsing stacklog file: {stacklog_file}")
        collect_elf_buildids_from_file(stacklog_file, elf_to_buildid, verbose)
    elif stacklog_dir:
        vprint(verbose, f"[INFO] Parsing stacklog directory: {stacklog_dir}")
        for dirpath, _, filenames in os.walk(stacklog_dir):
            for fn in filenames:
                path = os.path.join(dirpath, fn)
                collect_elf_buildids_from_file(path, elf_to_buildid, verbose)

    # Summary and warnings for missing build-id
    missing = [elf for elf, bid in elf_to_buildid.items() if not bid]
    if missing:
        eprint(
            f"[WARN] {len(missing)} ELF paths have no Build-id in logs "
            f"(will be ignored for symlink creation)."
        )

    return elf_to_buildid


# ==========================
# rootfs *.debug scanning
# ==========================

def extract_build_id_from_debug(debug_path: str) -> Optional[str]:
    """
    Run 'readelf -n' on a .debug file and extract its Build ID.
    Returns the build-id (lowercase hex) or None if not found.
    """
    try:
        out = subprocess.check_output(
            ["readelf", "-n", debug_path],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception:
        return None

    # Typical pattern: "Build ID: aa0d6e026a2f..."
    for line in out.splitlines():
        line = line.strip()
        if "Build ID:" in line or "Build-Id:" in line or "Build-id:" in line:
            parts = line.split(":")
            if len(parts) >= 2:
                bid = parts[1].strip().split()[0]
                if bid:
                    return bid.lower()
    return None


def scan_debug_files(
    rootfs: str,
    verbose: bool,
) -> Dict[str, List[str]]:
    """
    Walk the entire rootfs and find all *.debug files.

    For each candidate:
      - Run readelf -n to get Build ID
      - Map build-id -> [list of real .debug paths]

    Returns:
      buildid_to_debug_paths : { buildid_lower: [full_path, ...], ... }
    """
    buildid_to_debug_paths: Dict[str, List[str]] = defaultdict(list)

    vprint(verbose, f"[INFO] Scanning rootfs for *.debug under: {rootfs}")

    for dirpath, _, filenames in os.walk(rootfs):
        for fn in filenames:
            if not fn.endswith(".debug"):
                continue
            full_path = os.path.join(dirpath, fn)
            bid = extract_build_id_from_debug(full_path)
            if not bid:
                vprint(verbose, f"[DEBUG] No Build ID in {full_path}")
                continue
            buildid_to_debug_paths[bid].append(full_path)

    vprint(
        verbose,
        f"[INFO] Found {sum(len(v) for v in buildid_to_debug_paths.values())} "
        f"*.debug files with Build ID, "
        f"{len(buildid_to_debug_paths)} unique Build IDs.",
    )

    # Print mapping (build-id -> first path) as requested
    for bid, paths in sorted(buildid_to_debug_paths.items()):
        first = paths[0]
        print(f"FOUND: build-id {bid} → {first}")

    return buildid_to_debug_paths


# ==========================
# Symlink creation
# ==========================

def build_symlink_path(
    rootfs: str,
    debug_root_logical: str,
    build_id: str,
) -> str:
    """
    Given build-id "aa0d6e026a2f..." and logical debug root like
    "/usr/lib/debug/.build-id", create an absolute symlink path like:

      <rootfs>/usr/lib/debug/.build-id/aa/0d6e...debug
    """
    bid = build_id.lower()
    if len(bid) < 3:
        # Degenerate build-id, but still handle
        head = bid[:2]
        tail = bid[2:]
    else:
        head = bid[:2]
        tail = bid[2:]

    logical_root = debug_root_logical.lstrip("/") # drop leading '/'
    base_dir = os.path.join(rootfs, logical_root, head)
    return os.path.join(base_dir, tail + ".debug")


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def create_symlink(
    target: str,
    link_path: str,
    dry_run: bool,
    verbose: bool,
) -> None:
    """
    Create or replace symlink: ln -sf target link_path

    - Parent directories are created if needed.
    """
    parent = os.path.dirname(link_path)
    ensure_dir(parent)

    if dry_run:
        print(f"DRY-RUN LINK: {link_path} → {target}")
        return

    # Remove existing file/symlink if any
    if os.path.islink(link_path) or os.path.exists(link_path):
        try:
            os.remove(link_path)
        except OSError as ex:
            eprint(f"[WARN] Failed to remove existing {link_path}: {ex}")

    try:
        os.symlink(target, link_path)
        print(f"LINK: {link_path} → {target}")
    except OSError as ex:
        eprint(f"[ERROR] Failed to create symlink {link_path} → {target}: {ex}")


def construct_links(
    rootfs: str,
    debug_root_logical: str,
    elf_to_buildid: Dict[str, Optional[str]],
    buildid_to_debug_paths: Dict[str, List[str]],
    dry_run: bool,
    verbose: bool,
) -> None:
    """
    For each orig_elf with a valid build-id:

      - Find matching .debug file from buildid_to_debug_paths.
        * If there are multiple candidates, use the first and warn.
        * If none, emit a "Missing debug file" warning.

      - Compute symlink path under <rootfs>/<debug_root>/<aa>/<rest>.debug
      - ln -sf <real_debug_file> <symlink_path>
    """
    total_with_bid = 0
    missing_debug = 0

    for orig_elf, build_id in sorted(elf_to_buildid.items()):
        if not build_id:
            continue
        total_with_bid += 1
        bid = build_id.lower()

        paths = buildid_to_debug_paths.get(bid)
        if not paths:
            missing_debug += 1
            eprint(f"Missing debug file for build-id: {bid} (orig_elf={orig_elf})")
            continue

        # Use first candidate, warn if multiple
        if len(paths) > 1:
            eprint(
                f"[WARN] Multiple debug files for build-id {bid}, "
                f"using first:\n " + "\n ".join(paths)
            )
        real_debug = paths[0]

        symlink_path = build_symlink_path(rootfs, debug_root_logical, bid)
        vprint(
            verbose,
            f"[INFO] orig_elf={orig_elf}, build-id={bid}\n"
            f" debug={real_debug}\n"
            f" link ={symlink_path}",
        )

        create_symlink(real_debug, symlink_path, dry_run, verbose)

    print(
        f"[SUMMARY] ELF with build-id={total_with_bid}, "
        f"missing-debug={missing_debug}, "
        f"created-links={total_with_bid - missing_debug}"
        f"{' (dry-run)' if dry_run else ''}"
    )


# ==========================
# CLI / main
# ==========================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Construct regular build-id symlinks under <rootfs>/<debug-root> "
            "based on stack logs and existing *.debug files in rootfs."
        )
    )

    parser.add_argument(
        "--rootfs",
        required=True,
        help="Rootfs base directory (e.g. /mnt/rootfs).",
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--stacklog-file",
        help="Single stacklog file to parse.",
    )
    group.add_argument(
        "--stacklog-dir",
        help="Directory containing stacklog files to parse recursively.",
    )

    parser.add_argument(
        "--debug-root",
        default="/usr/lib/debug/.build-id",
        help=(
            "Logical build-id root under rootfs where symlinks will be created. "
            "Default: /usr/lib/debug/.build-id"
        ),
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not create symlinks; only print what would be done.",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed progress information.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    rootfs = os.path.abspath(args.rootfs)
    if not os.path.isdir(rootfs):
        eprint(f"[ERROR] rootfs does not exist or is not a directory: {rootfs}")
        sys.exit(1)

    if args.stacklog_file:
        stacklog_file = os.path.abspath(args.stacklog_file)
        if not os.path.isfile(stacklog_file):
            eprint(f"[ERROR] stacklog file does not exist: {stacklog_file}")
            sys.exit(1)
        stacklog_dir = None
    else:
        stacklog_file = None
        stacklog_dir = os.path.abspath(args.stacklog_dir)
        if not os.path.isdir(stacklog_dir):
            eprint(f"[ERROR] stacklog directory does not exist: {stacklog_dir}")
            sys.exit(1)

    # 1) Parse stacklogs → orig_elf -> build-id
    elf_to_buildid = collect_elf_buildids(
        stacklog_file=stacklog_file,
        stacklog_dir=stacklog_dir,
        verbose=args.verbose,
    )
    print(f"[INFO] Collected {len(elf_to_buildid)} unique orig_elf entries from stacklogs.")

    # 2) Scan rootfs for *.debug and build-id map
    buildid_to_debug_paths = scan_debug_files(rootfs, verbose=args.verbose)

    # 3) Construct build-id symlinks
    construct_links(
        rootfs=rootfs,
        debug_root_logical=args.debug_root,
        elf_to_buildid=elf_to_buildid,
        buildid_to_debug_paths=buildid_to_debug_paths,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
