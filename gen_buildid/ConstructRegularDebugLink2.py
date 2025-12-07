#!/usr/bin/env python3
"""
ConstructRegularDebugLink.py

Scan ELF files under the given rootfs (and target directories inside it),
extract GNU build-id, and create /usr/lib/debug/.build-id/xx/yyyy.debug
symlinks for each unique physical ELF (realpath-based).

Design / policy:

  - main (stripped) ELF:
      Rootfs contains stripped main ELFs for execution.
      Their build-id -> main mapping (e.g. /usr/lib/.build-id/xx/yyyy) is
      considered read-only and is NOT modified by this script.

  - debug ELF:
      Separate full debug ELFs exist under /usr/lib/debug/<elf_rel>.debug.
      We create and maintain *debug* build-id links:
          /usr/lib/debug/.build-id/xx/yyyy.debug
      that point to those debug ELFs.

  - realpath-based:
      Each physical ELF file is processed only once.
      Multiple symlink paths that resolve to the same real file share the same
      build-id and debug target (dedup by (build_id, real_abs)).

TSV logging:

  By default, we log all actions to ./download/logs/genID.tsv in TSV format:

      action  build_id  elf_type  elf_path_rel  debug_target_rel  link_path_rel

  where:
    - action: one of
        "created",
        "overwritten",
        "kept",
        "skipped_existing_symlink",
        "skipped_existing_nonsymlink",
        "error_readlink",
        "realpath_outside_rootfs",
        "no_debug_candidate"
    - build_id: hex string of GNU build-id (no spaces)
    - elf_type: "main" or "debug" (based on ELF path)
    - elf_path_rel: rootfs-relative path to the *real* ELF (starting with '/')
    - debug_target_rel: rootfs-relative path to debug ELF (or "-" if none)
    - link_path_rel: rootfs-relative path to the .build-id.debug symlink
                     (or "-" if not applicable)
"""

import argparse
import os
import sys
import subprocess

# Optional pyelftools import (for build-id parsing from ELF notes)
try:
    from elftools.elf.elffile import ELFFile  # type: ignore
    HAS_PYELFTOOLS = True
except ImportError:
    HAS_PYELFTOOLS = False

DEFAULT_LOG_TSV_PATH = os.path.join(".", "download", "logs", "genID.tsv")


# ------------------------------------------------------------
# Argument parsing
# ------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Construct debug .build-id symlinks under /usr/lib/debug/.build-id "
            "based on GNU build-id of ELFs in the rootfs."
        )
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
            "TSV log file path for actions. "
            "Default: ./download/logs/genID.tsv"
        ),
    )
    parser.add_argument(
        "--buildid-backend",
        choices=["auto", "pyelf", "readelf"],
        default="auto",
        help=(
            "Backend to extract GNU build-id: "
            "'pyelf' (pyelftools), 'readelf' (external tool), or 'auto' "
            "(try pyelftools first if available, then fall back to readelf)."
        ),
    )
    parser.add_argument(
        "--follow-symlinks",
        action="store_true",
        help="Follow symlinks to directories during scan.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing debug .build-id links if they already exist (symlinks only).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not create or modify any files; only print/log what would be done.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed processing information.",
    )
    return parser.parse_args()


# ------------------------------------------------------------
# Basic helpers
# ------------------------------------------------------------
def is_elf(path: str) -> bool:
    """Return True if file at path looks like an ELF (checks magic bytes)."""
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
        return magic == b"\x7fELF"
    except OSError:
        return False


def get_build_id_with_readelf(path: str) -> str | None:
    """
    Extract GNU build-id using external `readelf -n`.

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
        sys.stderr.write("ERROR: 'readelf' not found. Please install binutils.\n")
        return None

    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("Build ID:"):
            value = line.split("Build ID:", 1)[1].strip()
            value = value.replace(" ", "")
            if value:
                return value
    return None


def get_build_id_with_pyelftools(path: str) -> str | None:
    """
    Extract GNU build-id using pyelftools by reading the .note.gnu.build-id section.

    Returns:
        hex string of build-id (no spaces), or None if not found or error.
    """
    if not HAS_PYELFTOOLS:
        return None

    try:
        with open(path, "rb") as f:
            elf = ELFFile(f)
    except Exception as e:
        sys.stderr.write(f"WARNING: pyelftools failed to open {path}: {e}\n")
        return None

    sec = elf.get_section_by_name(".note.gnu.build-id")
    if sec is None:
        return None

    try:
        data = sec.data()
    except Exception as e:
        sys.stderr.write(
            f"WARNING: pyelftools failed to read .note.gnu.build-id from {path}: {e}\n"
        )
        return None

    if len(data) < 16:
        return None

    # Parse ELF note header: namesz, descsz, type (each 4 bytes, little-endian)
    namesz = int.from_bytes(data[0:4], "little")
    descsz = int.from_bytes(data[4:8], "little")
    _ntype = int.from_bytes(data[8:12], "little")

    # Name starts at offset 12, then padded to 4-byte boundary
    name_off = 12
    name_end = name_off + namesz
    desc_off = (name_end + 3) & ~3  # align up to 4
    if desc_off + descsz > len(data):
        return None

    buildid_bytes = data[desc_off : desc_off + descsz]
    if not buildid_bytes:
        return None

    return buildid_bytes.hex()


def get_build_id(path: str, backend: str) -> str | None:
    """
    Wrapper to extract GNU build-id using the requested backend.

    backend:
      - 'pyelf'   : use pyelftools only
      - 'readelf' : use external readelf only
      - 'auto'    : try pyelftools first (if available), then fall back to readelf
    """
    if backend == "pyelf":
        if not HAS_PYELFTOOLS:
            sys.stderr.write(
                "ERROR: --buildid-backend=pyelf requested but pyelftools is not installed.\n"
            )
            return None
        return get_build_id_with_pyelftools(path)

    if backend == "readelf":
        return get_build_id_with_readelf(path)

    # auto
    if HAS_PYELFTOOLS:
        value = get_build_id_with_pyelftools(path)
        if value:
            return value
    # fallback to readelf
    return get_build_id_with_readelf(path)


def ensure_log_header(path: str) -> None:
    """Ensure the TSV log file exists and has a header row."""
    log_dir = os.path.dirname(path)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)

    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                "action\tbuild_id\telf_type\telf_path_rel\t"
                "debug_target_rel\tlink_path_rel\n"
            )


def log_link(
    action: str,
    build_id: str | None,
    elf_type: str,
    elf_path_rel: str,
    debug_target_rel: str,
    link_path_rel: str,
    log_path: str,
    dry_run: bool,
) -> None:
    """
    Append a line describing the action for this build-id/link to the TSV log.

    For actions where build_id or link_path_rel is not applicable, pass "-" for those fields.
    """
    if dry_run:
        return  # Do not write TSV during dry-run; only simulate.

    ensure_log_header(log_path)
    bid = build_id if build_id is not None else "-"
    line = (
        f"{action}\t{bid}\t{elf_type}\t{elf_path_rel}"
        f"\t{debug_target_rel}\t{link_path_rel}\n"
    )
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line)


def to_rootfs_rel(rootfs: str, abs_path: str) -> str:
    """
    Convert an absolute path under rootfs to a rootfs-relative path starting with '/'.

    Example:
        rootfs  = /mnt/rootfs
        abs_path = /mnt/rootfs/usr/lib/libfoo.so
        -> /usr/lib/libfoo.so
    """
    rel = os.path.relpath(abs_path, rootfs)
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


def find_debug_candidate(rootfs: str, elf_rel: str) -> str | None:
    """
    Given rootfs and a rootfs-relative *real* main ELF path, find a separate debug ELF.

    Policy:
      - Only use /usr/lib/debug/<elf_rel>.debug as debug target.
      - Never fall back to the stripped main ELF.

    Returns:
        rootfs-relative path to debug target, or None if not found.
    """
    # Standard separate debug file: /usr/lib/debug/<elf_rel>.debug
    # Example:
    #   elf_rel   = /usr/lib64/libfoo.so.1.2.3
    #   debug_rel = /usr/lib/debug/usr/lib64/libfoo.so.1.2.3.debug
    debug_rel = "/usr/lib/debug" + elf_rel + ".debug"
    debug_abs = os.path.join(rootfs, debug_rel.lstrip("/"))
    if os.path.exists(debug_abs):
        return debug_rel

    # No separate debug ELF -> do not create a build-id link for this ELF
    return None


def make_build_id_debug_link_path(debug_root: str, build_id: str) -> str:
    """
    Construct the host-absolute path to the debug build-id link:
      debug_root/xx/yyyy...debug

    debug_root: host path to debug .build-id root (e.g. <rootfs>/usr/lib/debug/.build-id)
    build_id: full hex build-id string
    """
    if len(build_id) < 3:
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


def count_buildid_links(root: str, kind: str) -> int:
    """
    Count build-id links under the given root.

    kind:
      - "debug": count files ending with ".debug" (debug build-id links)
      - "main" : count files NOT ending with ".debug" (main/stripped build-id links)

    The function walks the tree but is typically used on a 2-level .build-id layout:
      root/xx/yyyy[.debug]
    """
    if not os.path.isdir(root):
        return 0

    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        for fname in filenames:
            is_debug = fname.endswith(".debug")
            if kind == "debug" and is_debug:
                total += 1
            elif kind == "main" and not is_debug:
                total += 1
    return total


# ------------------------------------------------------------
# Core processing
# ------------------------------------------------------------
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
      - resolve realpath
      - check it is inside rootfs
      - get build-id
      - deduplicate by (build_id, real_abs)
      - find debug target (separate debug ELF)
      - create/overwrite/keep/skip debug .build-id.symlink
      - log to TSV
    """
    stats["elf_files"] += 1

    # 1) Realpath canonicalization
    real_abs = os.path.realpath(abs_path)

    # Ensure real_abs is inside rootfs
    rootfs_real = os.path.realpath(rootfs)
    if not (real_abs == rootfs_real or real_abs.startswith(rootfs_real + os.sep)):
        # ELF resolves outside the rootfs (e.g. symlink to host), skip it
        if args.verbose:
            print(f"[skip-outside-rootfs] {abs_path} -> {real_abs}")
        stats["realpath_outside_rootfs"] += 1
        elf_rel = "-"
        elf_type = "main"
        log_link(
            action="realpath_outside_rootfs",
            build_id=None,
            elf_type=elf_type,
            elf_path_rel=elf_rel,
            debug_target_rel="-",
            link_path_rel="-",
            log_path=log_tsv_path,
            dry_run=args.dry_run,
        )
        return

    # 2) Compute rootfs-relative path for the real file
    elf_rel = to_rootfs_rel(rootfs_real, real_abs)
    elf_type = classify_elf_type(elf_rel)

    # 2.5) Skip debug ELF as a *source*; we only use main ELFs to create build-id links
    if elf_type == "debug":
        if args.verbose:
            print(f"[skip-debug-elf] {elf_rel}")
        return

    # 3) Extract build-id
    build_id = get_build_id(real_abs, args.buildid_backend)
    if not build_id:
        stats["no_build_id"] += 1
        if args.verbose:
            print(f"[no-build-id] {real_abs} (backend={args.buildid_backend})")
        log_link(
            action="no_build_id",
            build_id=None,
            elf_type=elf_type,
            elf_path_rel=elf_rel,
            debug_target_rel="-",
            link_path_rel="-",
            log_path=log_tsv_path,
            dry_run=args.dry_run,
        )
        return

    # 4) Deduplicate by (build_id, real_abs)
    key = (build_id, real_abs)
    if key in stats["seen_keys"]:
        # Already processed this physical ELF for this build-id
        if args.verbose:
            print(f"[duplicate-skip] {real_abs} (build-id={build_id})")
        return
    stats["seen_keys"].add(key)

    # 5) Locate separate debug ELF (do not fall back to main)
    debug_target_rel = find_debug_candidate(rootfs_real, elf_rel)
    if not debug_target_rel:
        stats["no_debug_candidate"] += 1
        if args.verbose:
            print(f"[no-debug-target] {real_abs} (build-id={build_id})")
        log_link(
            action="no_debug_candidate",
            build_id=build_id,
            elf_type=elf_type,
            elf_path_rel=elf_rel,
            debug_target_rel="-",
            link_path_rel="-",
            log_path=log_tsv_path,
            dry_run=args.dry_run,
        )
        return

    # 6) Compute debug build-id link path under debug_root (host path)
    link_abs = make_build_id_debug_link_path(debug_root, build_id)
    link_dir = os.path.dirname(link_abs)
    ensure_dir(link_dir)

    # Debug link target is rootfs-relative path (e.g. /usr/lib/debug/...)
    link_target = debug_target_rel

    # Rootfs-relative link path for logging
    link_rel = to_rootfs_rel(rootfs_real, link_abs)

    # 7) Handle existing / missing link_abs
    if os.path.lexists(link_abs):
        # Something already exists at link_abs
        if os.path.islink(link_abs):
            # Existing symlink: check its current target (string)
            try:
                current_target = os.readlink(link_abs)
            except OSError as e:
                if args.verbose:
                    print(f"[symlink-read-error] {link_abs}: {e}")
                stats["links_existing_skipped"] += 1
                log_link(
                    action="error_readlink",
                    build_id=build_id,
                    elf_type=elf_type,
                    elf_path_rel=elf_rel,
                    debug_target_rel=debug_target_rel,
                    link_path_rel=link_rel,
                    log_path=log_tsv_path,
                    dry_run=args.dry_run,
                )
                return

            if current_target == link_target:
                # Already points to the desired target -> keep it
                if args.verbose:
                    print(
                        f"[kept] {link_abs} -> {current_target} "
                        f"(from {elf_rel}, build-id={build_id})"
                    )
                stats["links_existing_skipped"] += 1
                log_link(
                    action="kept",
                    build_id=build_id,
                    elf_type=elf_type,
                    elf_path_rel=elf_rel,
                    debug_target_rel=debug_target_rel,
                    link_path_rel=link_rel,
                    log_path=log_tsv_path,
                    dry_run=args.dry_run,
                )
                return

            # Symlink exists but points to a different target
            if args.overwrite:
                # Compare file sizes of current_target vs. new link_target.
                # Idea:
                #   - If new candidate is larger -> overwrite (more debug info likely).
                #   - If existing target is larger or equal -> keep existing link.
                current_size = -1
                new_size = -1

                # Both targets are stored as rootfs-relative paths (e.g. /usr/lib/debug/...)
                # Try to resolve them to real files under rootfs for size comparison.
                if current_target.startswith("/"):
                    current_abs = os.path.join(rootfs_real, current_target.lstrip("/"))
                    if os.path.exists(current_abs):
                        try:
                            current_size = os.path.getsize(current_abs)
                        except OSError:
                            current_size = -1

                if link_target.startswith("/"):
                    new_abs = os.path.join(rootfs_real, link_target.lstrip("/"))
                    if os.path.exists(new_abs):
                        try:
                            new_size = os.path.getsize(new_abs)
                        except OSError:
                            new_size = -1

                # Decide whether to overwrite based on size.
                # - If we cannot get sizes for either, fall back to always overwriting.
                # - If new_size > current_size -> overwrite.
                # - If new_size <= current_size and current_size >= 0 -> keep existing.
                do_overwrite = False

                if new_size < 0 and current_size < 0:
                    # No reliable size info; keep previous behavior (always overwrite).
                    do_overwrite = True
                elif new_size > current_size:
                    do_overwrite = True
                else:
                    do_overwrite = False

                if do_overwrite:
                    if args.verbose:
                        print(
                            f"[overwrite] {link_abs} : {current_target} -> {link_target} "
                            f"(from {elf_rel}, build-id={build_id}, "
                            f"current_size={current_size}, new_size={new_size})"
                        )
                    if not args.dry_run:
                        try:
                            os.remove(link_abs)
                            os.symlink(link_target, link_abs)
                        except OSError as e:
                            sys.stderr.write(
                                f"ERROR: failed to overwrite symlink {link_abs}: {e}\n"
                            )
                            return
                    stats["links_overwritten"] += 1
                    log_link(
                        action="overwritten",
                        build_id=build_id,
                        elf_type=elf_type,
                        elf_path_rel=elf_rel,
                        debug_target_rel=debug_target_rel,
                        link_path_rel=link_rel,
                        log_path=log_tsv_path,
                        dry_run=args.dry_run,
                    )
                else:
                    # Existing target is larger or equal; keep it.
                    if args.verbose:
                        print(
                            f"[keep-larger] {link_abs} (symlink, "
                            f"current={current_target} size={current_size}, "
                            f"candidate={link_target} size={new_size}, "
                            f"build-id={build_id})"
                        )
                    stats["links_existing_skipped"] += 1
                    log_link(
                        action="kept_larger_existing",
                        build_id=build_id,
                        elf_type=elf_type,
                        elf_path_rel=elf_rel,
                        debug_target_rel=debug_target_rel,
                        link_path_rel=link_rel,
                        log_path=log_tsv_path,
                        dry_run=args.dry_run,
                    )
            else:
                # Overwrite not allowed, keep existing
                if args.verbose:
                    print(
                        f"[exists-skip] {link_abs} (symlink, target={current_target}, "
                        f"desired={link_target}, build-id={build_id})"
                    )
                stats["links_existing_skipped"] += 1
                log_link(
                    action="skipped_existing_symlink",
                    build_id=build_id,
                    elf_type=elf_type,
                    elf_path_rel=elf_rel,
                    debug_target_rel=debug_target_rel,
                    link_path_rel=link_rel,
                    log_path=log_tsv_path,
                    dry_run=args.dry_run,
                )
                return

    # 8) No existing link/file: create new debug build-id symlink
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
    log_link(
        action="created",
        build_id=build_id,
        elf_type=elf_type,
        elf_path_rel=elf_rel,
        debug_target_rel=debug_target_rel,
        link_path_rel=link_rel,
        log_path=log_tsv_path,
        dry_run=args.dry_run,
    )


# ------------------------------------------------------------
# main()
# ------------------------------------------------------------
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
        print(f"rootfs          : {rootfs}")
        print(f"debug_root      : {debug_root}")
        print(f"tsv_log         : {tsv_log_path}")
        print(f"buildid_backend : {args.buildid_backend}")
        print(f"scan_roots      :")
        for sr in scan_roots:
            print(f"  - {sr}")
        print(f"overwrite       : {args.overwrite}")
        print(f"dry_run         : {args.dry_run}")
        print()

    stats: dict = {
        "scanned_files": 0,
        "elf_files": 0,
        "no_build_id": 0,
        "no_debug_candidate": 0,
        "realpath_outside_rootfs": 0,
        "links_created": 0,
        "links_overwritten": 0,
        "links_existing_skipped": 0,
        "seen_keys": set(),  # (build_id, real_abs)
    }

    rootfs_real = os.path.realpath(rootfs)

    for scan_root in scan_roots:
        for dirpath, dirnames, filenames in os.walk(
            scan_root, followlinks=args.follow_symlinks
        ):
            for fname in filenames:
                full_path = os.path.join(dirpath, fname)
                stats["scanned_files"] += 1

                # process regular files or symlinks that look like ELF
                if not os.path.isfile(full_path) and not os.path.islink(full_path):
                    continue
                if not is_elf(full_path):
                    continue

                process_elf(full_path, rootfs_real, debug_root, args, stats, tsv_log_path)

    # Summary
    print("=== ConstructRegularDebugLink summary ===")
    print(f"rootfs                  : {rootfs_real}")
    print(f"debug_root (debug)      : {debug_root}")
    print(f"scanned files           : {stats['scanned_files']}")
    print(f"ELF files               : {stats['elf_files']}")
    print(f"no build-id             : {stats['no_build_id']}")
    print(f"no debug candidate      : {stats['no_debug_candidate']}")
    print(f"realpath outside rootfs : {stats['realpath_outside_rootfs']}")
    print(f"debug links created     : {stats['links_created']}")
    print(f"debug links overwritten : {stats['links_overwritten']}")
    print(f"debug links skipped     : {stats['links_existing_skipped']}")

    # Count existing debug build-id links (.debug) under debug_root
    debug_links_existing = count_buildid_links(debug_root, kind="debug")

    # Heuristic: main build-id root (strip ELF) is typically /usr/lib/.build-id under rootfs
    main_buildid_root = os.path.join(rootfs_real, "usr", "lib", ".build-id")
    main_links_existing = 0
    if os.path.isdir(main_buildid_root):
        main_links_existing = count_buildid_links(main_buildid_root, kind="main")

    print(f"debug links existing    : {debug_links_existing}")
    if main_links_existing > 0:
        print(f"main  build-id root     : {main_buildid_root}")
        print(f"main  links existing    : {main_links_existing}")
    else:
        # Some images may not have a separate main .build-id tree.
        pass

    if not args.dry_run:
        print(f"TSV log                 : {tsv_log_path}")


if __name__ == "__main__":
    main()