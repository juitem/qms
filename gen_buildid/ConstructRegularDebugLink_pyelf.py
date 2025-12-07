#!/usr/bin/env python3
"""
ConstructRegularDebugLink.py

Scan ELF files under given rootfs and target directories,
extract GNU build-id, and create /usr/lib/debug/.build-id/*/*.debug
symlinks pointing to either:
  - separate debug ELF (if found), or
  - the main ELF itself (if --use-elf-as-debug enabled).

Additionally, log created/overwritten links to a TSV file
(default: ./download/logs/genID.tsv) in TSV format:

    action  build_id  elf_type  elf_path_rel  debug_target_rel  link_path_rel

where:
  - action: "created" or "overwritten"
  - build_id: hex string of GNU build-id
  - elf_type: "main" or "debug"
  - elf_path_rel: rootfs-relative path to the original ELF (main or debug)
  - debug_target_rel: rootfs-relative path to the file used as debug target
  - link_path_rel: rootfs-relative path to the .build-id symlink
"""

import argparse
import os
import sys
import subprocess

DEFAULT_LOG_TSV_PATH = os.path.join(".", "download", "logs", "genID.tsv")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Construct .build-id symlinks and log them to TSV."
    )
    parser.add_argument(
        "--rootfs",
        required=True,
        help="Rootfs directory (host path), e.g. ./download/img/ROOTFS",
    )
    parser.add_argument(
        "--target",
        action="append",
        required=True,
        help=(
            "Target directory inside rootfs to scan. "
            "Can be specified multiple times. Use 'ALL' to scan entire rootfs."
        ),
    )
    parser.add_argument(
        "--debug-root",
        default=None,
        help=(
            "Debug .build-id root (host path). "
            "Default: <rootfs>/usr/lib/debug/.build-id"
        ),
    )
    parser.add_argument(
        "--tsv-log",
        default=DEFAULT_LOG_TSV_PATH,
        help=(
            "TSV log file path for created/overwritten links. "
            "Default: ./download/logs/genID.tsv"
        ),
    )
    parser.add_argument(
        "--follow-symlinks",
        action="store_true",
        help="Follow symlinks to directories during scan.",
    )
    parser.add_argument(
        "--no-use-elf-as-debug",
        dest="use_elf_as_debug",
        action="store_false",
        help="Do NOT fall back to using the main ELF as debug target when no .debug file is found.",
    )
    parser.set_defaults(use_elf_as_debug=True)

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing .build-id links/files if they already exist.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not create or modify any files; only print what would be done.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed processing information.",
    )
    return parser.parse_args()


def is_elf(path: str) -> bool:
    """Return True if file at path looks like an ELF (checks magic bytes)."""
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
        return magic == b"\x7fELF"
    except OSError:
        return False


def get_build_id(path: str) -> str | None:
    """
    Extract GNU build-id using `readelf -n`.

    Returns:
        hex string of build-id (no spaces), or None if not found or error.
    """
    try:
        proc = subprocess.run(
            ["readelf", "-n", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        # readelf not installed
        sys.stderr.write("ERROR: 'readelf' not found. Please install binutils.\n")
        return None

    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("Build ID:"):
            value = line.split("Build ID:", 1)[1].strip()
            # remove any internal spaces just in case
            value = value.replace(" ", "")
            if value:
                return value
    return None


def ensure_log_header(path: str) -> None:
    """Ensure the TSV log file exists and has a header row."""
    log_dir = os.path.dirname(path)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)

    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            # action first, as requested
            f.write(
                "action\tbuild_id\telf_type\telf_path_rel\t"
                "debug_target_rel\tlink_path_rel\n"
            )


def log_link(
    action: str,
    build_id: str,
    elf_type: str,
    elf_path_rel: str,
    debug_target_rel: str,
    link_path_rel: str,
    log_path: str,
) -> None:
    """Append a line describing the created/overwritten link to the TSV log."""
    ensure_log_header(log_path)
    line = (
        f"{action}\t{build_id}\t{elf_type}\t{elf_path_rel}"
        f"\t{debug_target_rel}\t{link_path_rel}\n"
    )
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line)


def to_rootfs_rel(rootfs: str, abs_path: str) -> str:
    """
    Convert an absolute path under rootfs to a rootfs-relative path starting with '/'.

    Example:
        rootfs = /mnt/rootfs
        abs_path = /mnt/rootfs/usr/lib/libfoo.so
        -> /usr/lib/libfoo.so
    """
    rel = os.path.relpath(abs_path, rootfs)
    # Normalize and ensure leading slash
    rel = rel.replace(os.sep, "/")
    if not rel.startswith("/"):
        rel = "/" + rel
    return rel


def classify_elf_type(rel_path: str) -> str:
    """
    Classify ELF type as 'main' or 'debug' based on its rootfs-relative path.
    Heuristic:
      - path under /usr/lib/debug or ending with '.debug' => 'debug'
      - otherwise => 'main'
    """
    if rel_path.startswith("/usr/lib/debug/") or rel_path.endswith(".debug"):
        return "debug"
    return "main"


def find_debug_candidate(rootfs: str, elf_rel: str, use_elf_as_debug: bool) -> str | None:
    """
    Given rootfs and a rootfs-relative ELF path, find a suitable debug target.

    Search order:
      1) Standard /usr/lib/debug/<elf_rel>.debug (host path)
      2) If use_elf_as_debug is True, fall back to the ELF itself

    Returns:
        rootfs-relative path to debug target, or None if not found.
    """
    # 1) Standard debug file: /usr/lib/debug/<elf_rel>.debug
    #    e.g. /usr/lib/debug/usr/lib64/libfoo.so.1.2.3.debug
    debug_rel = "/usr/lib/debug" + elf_rel + ".debug"
    debug_abs = os.path.join(rootfs, debug_rel.lstrip("/"))
    if os.path.exists(debug_abs):
        return debug_rel

    # 2) Fallback to the ELF itself if allowed
    if use_elf_as_debug:
        elf_abs = os.path.join(rootfs, elf_rel.lstrip("/"))
        if os.path.exists(elf_abs):
            return elf_rel

    return None


def make_build_id_link_path(debug_root: str, build_id: str) -> str:
    """
    Construct the host-absolute path to the .build-id/<xx>/<yyyy...>.debug link.

    debug_root: host path to .build-id root
    build_id: full hex build-id string
    """
    if len(build_id) < 3:
        # Build-id is suspiciously short, but still handle it
        prefix = build_id[:2]
        rest = build_id[2:]
    else:
        prefix = build_id[:2]
        rest = build_id[2:]
    dir_path = os.path.join(debug_root, prefix)
    file_name = rest + ".debug"
    return os.path.join(dir_path, file_name)


def ensure_dir(path: str) -> None:
    """Ensure the directory for the given path exists."""
    os.makedirs(path, exist_ok=True)


def process_elf(
    abs_path: str,
    rootfs: str,
    debug_root: str,
    args: argparse.Namespace,
    stats: dict,
    log_tsv_path: str,
) -> None:
    """
    Process a single ELF file:
      - get build-id
      - find debug target
      - create/overwrite .build-id symlink
      - log to TSV
    """
    stats["elf_files"] += 1

    build_id = get_build_id(abs_path)
    if not build_id:
        stats["no_build_id"] += 1
        if args.verbose:
            print(f"[no-build-id] {abs_path}")
        return

    elf_rel = to_rootfs_rel(rootfs, abs_path)
    elf_type = classify_elf_type(elf_rel)

    debug_target_rel = find_debug_candidate(rootfs, elf_rel, args.use_elf_as_debug)
    if not debug_target_rel:
        stats["no_debug_candidate"] += 1
        if args.verbose:
            print(f"[no-debug-target] {abs_path} (build-id={build_id})")
        return

    # Build-id link host path
    link_abs = make_build_id_link_path(debug_root, build_id)
    link_dir = os.path.dirname(link_abs)
    ensure_dir(link_dir)

    # Link target string inside the rootfs: use rootfs-relative (starting with '/')
    link_target = debug_target_rel

    # rootfs-relative link path for logging
    link_rel = to_rootfs_rel(rootfs, link_abs)

    if os.path.lexists(link_abs):
        # Link or file already exists
        if args.overwrite:
            if args.verbose:
                print(
                    f"[overwrite] {link_abs} -> {link_target} "
                    f"(from {elf_rel}, build-id={build_id})"
                )
            if not args.dry_run:
                try:
                    os.remove(link_abs)
                except OSError as e:
                    sys.stderr.write(f"ERROR: failed to remove {link_abs}: {e}\n")
                    return
                try:
                    os.symlink(link_target, link_abs)
                except OSError as e:
                    sys.stderr.write(f"ERROR: failed to create symlink {link_abs}: {e}\n")
                    return
            stats["links_overwritten"] += 1
            # Log overwritten
            if not args.dry_run:
                log_link(
                    action="overwritten",
                    build_id=build_id,
                    elf_type=elf_type,
                    elf_path_rel=elf_rel,
                    debug_target_rel=debug_target_rel,
                    link_path_rel=link_rel,
                    log_path=log_tsv_path,
                )
        else:
            # Existing link/file is kept; do not log
            if args.verbose:
                print(f"[exists-skip] {link_abs} (build-id={build_id})")
            stats["links_existing_skipped"] += 1
        return

    # No existing link/file: create new
    if args.verbose:
        print(
            f"[create] {link_abs} -> {link_target} "
            f"(from {elf_rel}, build-id={build_id})"
        )
    if not args.dry_run:
        try:
            os.symlink(link_target, link_abs)
        except OSError as e:
            sys.stderr.write(f"ERROR: failed to create symlink {link_abs}: {e}\n")
            return

    stats["links_created"] += 1

    if not args.dry_run:
        log_link(
            action="created",
            build_id=build_id,
            elf_type=elf_type,
            elf_path_rel=elf_rel,
            debug_target_rel=debug_target_rel,
            link_path_rel=link_rel,
            log_path=log_tsv_path,
        )


def main() -> None:
    args = parse_args()

    rootfs = os.path.abspath(args.rootfs)
    if not os.path.isdir(rootfs):
        sys.stderr.write(f"ERROR: rootfs directory not found: {rootfs}\n")
        sys.exit(1)

    if args.debug_root:
        debug_root = os.path.abspath(args.debug_root)
    else:
        debug_root = os.path.join(rootfs, "usr", "lib", "debug", ".build-id")

    # TSV log absolute path
    tsv_log_path = os.path.abspath(args.tsv_log)

    # Build scan roots from --target
    scan_roots: list[str] = []
    for tgt in args.target:
        if tgt.upper() == "ALL":
            scan_roots = [rootfs]
            break
        # Treat tgt as path inside rootfs
        if tgt.startswith("/"):
            rel = tgt.lstrip("/")
        else:
            rel = tgt
        abs_tgt = os.path.join(rootfs, rel)
        if os.path.isdir(abs_tgt):
            scan_roots.append(abs_tgt)
        else:
            sys.stderr.write(
                f"WARNING: target directory not found under rootfs: {tgt}\n"
            )

    if not scan_roots:
        sys.stderr.write("ERROR: no valid scan roots found.\n")
        sys.exit(1)

    if args.verbose:
        print(f"rootfs     : {rootfs}")
        print(f"debug_root : {debug_root}")
        print(f"tsv_log    : {tsv_log_path}")
        print(f"scan_roots :")
        for sr in scan_roots:
            print(f"  - {sr}")
        print(f"use_elf_as_debug: {args.use_elf_as_debug}")
        print(f"overwrite       : {args.overwrite}")
        print(f"dry_run         : {args.dry_run}")
        print()

    stats = {
        "scanned_files": 0,
        "elf_files": 0,
        "no_build_id": 0,
        "no_debug_candidate": 0,
        "links_created": 0,
        "links_overwritten": 0,
        "links_existing_skipped": 0,
    }

    for scan_root in scan_roots:
        for dirpath, dirnames, filenames in os.walk(
            scan_root, followlinks=args.follow_symlinks
        ):
            for fname in filenames:
                full_path = os.path.join(dirpath, fname)
                stats["scanned_files"] += 1

                if not os.path.isfile(full_path) and not os.path.islink(full_path):
                    continue
                if not is_elf(full_path):
                    continue

                process_elf(full_path, rootfs, debug_root, args, stats, tsv_log_path)

    # Summary
    print("=== ConstructRegularDebugLink summary ===")
    print(f"rootfs                : {rootfs}")
    print(f"debug_root            : {debug_root}")
    print(f"scanned files         : {stats['scanned_files']}")
    print(f"ELF files             : {stats['elf_files']}")
    print(f"no build-id           : {stats['no_build_id']}")
    print(f"no debug candidate    : {stats['no_debug_candidate']}")
    print(f"links created         : {stats['links_created']}")
    print(f"links overwritten     : {stats['links_overwritten']}")
    print(f"links existing skipped: {stats['links_existing_skipped']}")
    if not args.dry_run:
        print(f"TSV log               : {tsv_log_path}")


if __name__ == "__main__":
    main()