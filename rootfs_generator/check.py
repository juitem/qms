#!/usr/bin/env python3
import argparse
import os
import sys
from typing import Any, Tuple

# -----------------------------------------------------------------------------
# Import helpers so this script works regardless of CWD
# -----------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = SCRIPT_DIR
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

#!/usr/bin/env python3
import argparse
import os
import sys
from typing import Any, Tuple

# -----------------------------------------------------------------------------
# Local imports (same directory as other rootfs scripts)
# -----------------------------------------------------------------------------

from hybrid_rootfs_gen_v2 import count_entries, scan_root_owned
from rewrite_symlinks import rewrite_symlinks

# -----------------------------------------------------------------------------
# Logging helpers
# -----------------------------------------------------------------------------

def open_log(path: str | None) -> Any:
    if not path:
        return None
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    return open(path, "w", encoding="utf-8")


def close_log(fp: Any) -> None:
    if fp is not None:
        fp.close()


def log(fp: Any, level: str, msg: str) -> None:
    line = f"[{level}] {msg}\n"
    if fp is not None:
        fp.write(line)
    if level in ("INFO", "WARNING"):
        sys.stdout.write(line)
    elif level == "ERROR":
        sys.stderr.write(line)


# -----------------------------------------------------------------------------
# Symlink check wrapper (non-destructive)
# -----------------------------------------------------------------------------

def check_symlinks(
    rootfs_dir: str,
    target_dir: str,
    logfile_symlink: str | None,
    broken_report: str | None,
    verbose: bool,
) -> Tuple[int, int, int]:
    """
    Use rewrite_symlinks() in dry-run mode to check symlink health without
    modifying anything. This reuses the exact same logic as the actual
    rewrite step, but does not touch the filesystem.

    Returns:
        (total_symlinks_seen, broken_symlinks, errors_during_check)
    """
    # We rely on rewrite_symlinks() summary for details. Here we only
    # wrap it to catch exceptions and to provide a simple numeric summary.
    # Note: rewrite_symlinks() itself counts symlinks internally; we do
    # not double-count here. We simply treat any exception as an error.
    errors = 0

    try:
        rewrite_symlinks(
            rootfs_dir=rootfs_dir,
            target_dir=target_dir,
            dry_run=True,
            logfile_symlink=logfile_symlink,
            broken_report=broken_report,
            verbose=verbose,
        )
    except Exception as e:
        errors += 1
        sys.stderr.write(f"[ERROR] Symlink check failed: {e}\n")

    # We do not have direct access to the internal counters from
    # rewrite_symlinks(), so we return (-1, -1, errors) here to indicate
    # that counts are not available but error status is.
    # The broken symlink list (if any) is written to broken_report.
    return -1, -1, errors


# -----------------------------------------------------------------------------
# RootFS check orchestration
# -----------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "RootFS status checker.\n"
            "This script does NOT extract images. It only inspects an already "
            "assembled ROOTFS tree for statistics, root-owned entries, and "
            "symlink health (using existing shared logic)."
        )
    )
    parser.add_argument(
        "rootfs_dir",
        help="Path to the logical ROOTFS directory (corresponds to '/').",
    )
    parser.add_argument(
        "--logfile-rootfs",
        help="Log file path for rootfs status logs (overwritten on each run).",
    )
    parser.add_argument(
        "--root-owned-report",
        help="Path to save report of root-owned directories and files.",
    )
    parser.add_argument(
        "--logfile-symlink",
        help="Log file path for symlink check logs (overwritten on each run).",
    )
    parser.add_argument(
        "--broken-report",
        help="Path to save broken symlink report.",
    )
    parser.add_argument(
        "--no-symlink-check",
        action="store_true",
        help="Skip symlink health check if set.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose output to stdout."
    )
    return parser.parse_args()


def run_status_check(args: argparse.Namespace) -> int:
    rootfs_dir = os.path.abspath(args.rootfs_dir)
    logfile_rootfs = args.logfile_rootfs
    logfile_symlink = args.logfile_symlink
    broken_report = args.broken_report
    root_owned_report = args.root_owned_report
    verbose = args.verbose
    do_symlink_check = not args.no_symlink_check

    log_fp = open_log(logfile_rootfs)

    if verbose:
        log(log_fp, "INFO", f"ROOTFS DIR (logical '/'): {rootfs_dir}")
        if logfile_rootfs:
            log(log_fp, "INFO", f"RootFS log: {logfile_rootfs}")
        if root_owned_report:
            log(log_fp, "INFO", f"Root-owned report: {root_owned_report}")
        if logfile_symlink:
            log(log_fp, "INFO", f"Symlink log: {logfile_symlink}")
        if broken_report:
            log(log_fp, "INFO", f"Broken symlink report: {broken_report}")

    # 1) Basic counts: directories, files, symlinks
    dirs, files, symlinks = count_entries(rootfs_dir)

    # 2) Root-owned scan (directories and files)
    root_owned_dirs = 0
    root_owned_files = 0
    if os.path.isdir(rootfs_dir):
        rd, rf = scan_root_owned(
            rootfs_dir,
            log_fp,
            root_owned_report,
            verbose,
        )
        root_owned_dirs = rd
        root_owned_files = rf

    # 3) Symlink health check (non-destructive)
    symlink_check_errors = 0
    if do_symlink_check:
        _, _, symlink_check_errors = check_symlinks(
            rootfs_dir=rootfs_dir,
            target_dir=rootfs_dir,
            logfile_symlink=logfile_symlink,
            broken_report=broken_report,
            verbose=verbose,
        )

    # 4) Summary
    logfile_display = logfile_rootfs if logfile_rootfs else "-"

    print("[Check RootFS]")
    print(f"       ROOTFS DIR: {rootfs_dir}")
    print(f"       Logfile: {logfile_display}")
    print(f"       Dirs: {dirs}")
    print(f"       Files: {files}")
    print(f"       Symlinks: {symlinks}")
    print(f"       ROOT-OWNED DIRS: {root_owned_dirs}")
    print(f"       ROOT-OWNED FILES: {root_owned_files}")
    if do_symlink_check:
        print(f"       Symlink check: DONE (see logs for details)")
        print(f"       Symlink check errors: {symlink_check_errors}")
    else:
        print(f"       Symlink check: SKIPPED")

    close_log(log_fp)

    # Status checker is intentionally non-strict:
    # - It always returns 0 so it can be used repeatedly without breaking shells.
    # - If you need strict behavior (treat broken symlinks or root-owned entries
    #   as failures), this can be extended with an additional flag later.
    return 0


def main() -> None:
    args = parse_args()
    rc = run_status_check(args)
    if rc != 0:
        sys.exit(rc)


if __name__ == "__main__":
    main()