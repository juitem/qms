#!/usr/bin/env python3
import argparse
import os
import sys


def rewrite_symlinks(
    rootfs_dir: str,
    target_dir: str,
    dry_run: bool = False,
    logfile_symlink: str | None = None,
    verbose: bool = False,
    broken_report: str | None = None,
):
    """
    Rewrite absolute symlinks in target_dir:
    - Only absolute symlinks starting with '/' are rewritten.
    - '/' is interpreted as rootfs_dir.
    - New symlink becomes a relative path to rootfs_dir.
    """
    rootfs_dir_abs = os.path.abspath(rootfs_dir)
    target_dir_abs = os.path.abspath(target_dir)

    if verbose:
        print(f"[INFO] Rewriting symlinks")
        print(f"       ROOTFS_DIR = {rootfs_dir_abs}")
        print(f"       TARGET_DIR = {target_dir_abs}")
        print(f"       DRY-RUN    = {dry_run}")
        print(f"       LOGFILE    = {logfile_symlink}")

    log_fp = None
    if logfile_symlink is not None and not dry_run:
        try:
            log_dir = os.path.dirname(logfile_symlink)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            log_fp = open(logfile_symlink, "w", encoding="utf-8")
        except OSError as e:
            if verbose:
                print(f"[ERROR] Cannot open logfile '{logfile_symlink}': {e}", file=sys.stderr)
            log_fp = None

    broken_fp = None
    if broken_report is not None:
        try:
            if broken_report:
                broken_dir = os.path.dirname(broken_report)
                if broken_dir:
                    os.makedirs(broken_dir, exist_ok=True)
            broken_fp = open(broken_report, "w", encoding="utf-8")
        except OSError as e:
            if verbose:
                print(f"[ERROR] Cannot open broken_report '{broken_report}': {e}", file=sys.stderr)
            broken_fp = None

    def log(prefix: str, msg: str):
        """Write log only if logfile enabled and not dry-run."""
        if log_fp is not None:
            log_fp.write(f"{prefix} {msg}\n")

    def log_broken(msg: str):
        if broken_fp is not None:
            broken_fp.write(f"{msg}\n")

    success_count = 0
    warning_count = 0
    error_count = 0
    broken_list: list[str] = []

    try:
        for dirpath, dirnames, filenames in os.walk(target_dir_abs, followlinks=False):
            entries = list(dirnames) + list(filenames)

            for name in entries:
                full_path = os.path.join(dirpath, name)
                rel_full_path = os.path.relpath(full_path, start=target_dir_abs)

                try:
                    st = os.lstat(full_path)
                except FileNotFoundError:
                    continue
                except PermissionError as e:
                    msg = f"{full_path}\tPermission denied during lstat: {e}"
                    if verbose:
                        print(f"[WARNING] {msg}")
                    log("[WARNING]", msg)
                    warning_count += 1
                    continue

                if not os.path.islink(full_path):
                    continue

                try:
                    target = os.readlink(full_path)
                except OSError as e:
                    msg = f"{full_path}\tCannot readlink: {e}"
                    if verbose:
                        print(f"[WARNING] {msg}")
                    log("[WARNING]", msg)
                    warning_count += 1
                    continue

                # Only absolute symlinks
                if not target.startswith("/"):
                    continue

                # Compute the absolute target as if '/' were rootfs_dir_abs
                host_abs_target = os.path.normpath(
                    os.path.join(rootfs_dir_abs, target.lstrip("/"))
                )

                # Safety check: never rewrite a symlink to point outside ROOTFS.
                # If the resolved absolute path is not under rootfs_dir_abs, skip it.
                outside_rootfs = False
                try:
                    common = os.path.commonpath([host_abs_target, rootfs_dir_abs])
                    if common != rootfs_dir_abs:
                        outside_rootfs = True
                except ValueError:
                    # Different drives or invalid combination – treat as outside.
                    outside_rootfs = True

                if outside_rootfs:
                    msg = (
                        f"{full_path}\tTarget '{target}' resolves outside ROOTFS "
                        f"({host_abs_target}); skipping rewrite."
                    )
                    if verbose:
                        print(f"[WARNING] {msg}")
                    log("[WARNING]", msg)
                    warning_count += 1
                    # We deliberately do NOT rewrite this symlink, to avoid touching files
                    # outside of the ROOTFS boundary.
                    continue

                rel_target = os.path.relpath(host_abs_target, start=dirpath)

                is_broken = not os.path.exists(host_abs_target)
                if is_broken:
                    broken_entry = f"{rel_full_path}\t{target} -> {rel_target}"
                    broken_list.append(broken_entry)
                    log_broken(broken_entry)
                    warning_count += 1

                if verbose:
                    print(f"[INFO] SYMLINK: {rel_full_path}")
                    print(f"       {target} -> {rel_target}")

                if dry_run:
                    if verbose:
                        print("       [DRY-RUN] No change applied.")
                    continue

                try:
                    os.remove(full_path)
                    os.symlink(rel_target, full_path)

                    msg = f"{rel_full_path}\t{target} -> {rel_target}"
                    log("[INFO]", msg)
                    success_count += 1

                except OSError as e:
                    msg = f"{full_path}\tFailed to update symlink: {e}"
                    if verbose:
                        print(f"[ERROR] {msg}")
                    log("[ERROR]", msg)
                    error_count += 1

    finally:
        if log_fp is not None:
            log_fp.close()
        if broken_fp is not None:
            broken_fp.close()

    logfile_display = logfile_symlink if logfile_symlink else "-"

    print("[Convert Symlinks]")
    print(f"       ROOTFS: {rootfs_dir_abs}")
    print(f"       TARGET DIR: {target_dir_abs}")
    print(f"       Logfile: {logfile_display}")
    print(f"       Success: {success_count}")
    print(f"       ERRORS: {error_count}")
    print(f"       WARNINGS: {warning_count}")
    print(f"       Broken: {len(broken_list)}")
    if broken_list:
        print(f"       Broken symlinks:")
        for entry in broken_list:
            print(f"         {entry}")

def _parse_args():
    parser = argparse.ArgumentParser(
        description="Rewrite absolute symlinks in TARGET_DIR so that '/' points to ROOTFS_DIR."
    )
    parser.add_argument(
        "--rootfs",
        dest="rootfs_dir",
        required=True,
        help="Rootfs directory that represents '/' for absolute symlinks.",
    )
    parser.add_argument(
        "--target",
        dest="target_dir",
        required=True,
        help="Target directory whose symlinks will be rewritten.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--logfile-symlink", help="Path for symlink rewrite logs")
    parser.add_argument("--broken-report", help="Path for broken symlink report file")
    parser.add_argument("-v", "--verbose", action="store_true")

    return parser.parse_args()


def main():
    args = _parse_args()

    rootfs_dir = os.path.abspath(args.rootfs_dir)
    target_dir = os.path.abspath(args.target_dir)

    if not os.path.isdir(rootfs_dir):
        print(f"[ERROR] rootfs_dir not found: {rootfs_dir}")
        sys.exit(1)

    if not os.path.isdir(target_dir):
        print(f"[ERROR] target_dir not found: {target_dir}")
        sys.exit(1)

    rewrite_symlinks(
        rootfs_dir,
        target_dir,
        dry_run=args.dry_run,
        logfile_symlink=args.logfile_symlink,
        verbose=args.verbose,
        broken_report=args.broken_report,
    )

    if args.verbose:
        print("[INFO] Symlink rewrite finished.")


if __name__ == "__main__":
    main()