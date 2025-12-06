#!/usr/bin/env python3
import argparse
import os
import sys
import stat
import subprocess
from typing import Any, Tuple


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract a ROOTFS from an EXT4 image using debugfs (no mount, no FUSE), "
            "including directories, regular files, and symlinks."
        )
    )
    parser.add_argument("image", help="Path to EXT4 image file")
    parser.add_argument(
        "--outdir",
        required=True,
        help="Output ROOTFS directory where the image content will be extracted.",
    )
    parser.add_argument(
        "--chmod",
        metavar="MODE",
        help=(
            "If set to e.g. 755, apply 'chmod -R MODE' to the output ROOTFS "
            "after extraction (best-effort)."
        ),
    )
    parser.add_argument(
        "--logfile-rootfs",
        help="Log file path for rootfs extraction logs (overwritten on each run).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not actually write anything; only log what would be done.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose output to stdout."
    )
    return parser.parse_args()


def open_log(path: str | None) -> Any:
    if not path:
        return None
    # Overwrite on each run
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    return open(path, "w", encoding="utf-8")


def close_log(fp: Any) -> None:
    if fp is not None:
        fp.close()


def log(fp: Any, level: str, msg: str) -> None:
    line = f"[{level}] {msg}\n"
    if fp is not None:
        fp.write(line)
    # Only INFO and ERROR/WARNING go to stdout/stderr depending on level
    if level in ("INFO", "WARNING"):
        sys.stdout.write(line)
    elif level == "ERROR":
        sys.stderr.write(line)


def check_debugfs() -> None:
    try:
        subprocess.check_output(["debugfs", "-V"], stderr=subprocess.STDOUT)
    except Exception as e:
        print("[ERROR] 'debugfs' not found or not runnable.", file=sys.stderr)
        print("        Please install e2fsprogs (debugfs).", file=sys.stderr)
        print(f"        Original error: {e}", file=sys.stderr)
        sys.exit(1)


def run_debugfs_cmd(image: str, cmd: str) -> str:
    """
    Run a debugfs command and return its stdout text.

    Note: This wraps the 'debugfs -R <cmd> <image>' invocation.
    """
    full_cmd = ["debugfs", "-R", cmd, image]
    return subprocess.check_output(full_cmd, stderr=subprocess.STDOUT, text=True)


def apply_chmod_recursive(
    root: str, mode_str: str, dry_run: bool, fp: Any, verbose: bool
) -> Tuple[int, int]:
    """
    Apply chmod -R MODE to root (best-effort). Returns (success_count, error_count).
    """
    try:
        mode_val = int(mode_str, 8)
    except ValueError:
        log(fp, "ERROR", f"Invalid chmod mode: {mode_str}")
        return 0, 1

    success = 0
    errors = 0

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            if dry_run:
                if verbose:
                    log(fp, "INFO", f"[DRY-RUN] chmod {mode_str} {full}")
                success += 1
                continue
            try:
                os.chmod(full, mode_val)
                success += 1
            except OSError as e:
                log(fp, "WARNING", f"chmod {mode_str} {full} failed: {e}")
                errors += 1

    return success, errors


def count_entries(root: str) -> Tuple[int, int, int]:
    """
    Count (dirs, files, symlinks) under root.
    """
    dir_count = 0
    file_count = 0
    symlink_count = 0

    if not os.path.isdir(root):
        return 0, 0, 0

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Directories: all dirnames
        dir_count += len(dirnames)
        # Files and symlinks
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                symlink_count += 1
            elif stat.S_ISREG(st.st_mode):
                file_count += 1
            else:
                # treat others as files for counting purposes
                file_count += 1

    return dir_count, file_count, symlink_count


def extract_rootfs_debugfs(
    image: str,
    outdir: str,
    dry_run: bool,
    chmod_mode: str | None,
    logfile: str | None,
    verbose: bool,
) -> int:
    log_fp = open_log(logfile)

    image = os.path.abspath(image)
    outdir = os.path.abspath(outdir)

    if verbose:
        log(log_fp, "INFO", f"IMAGE: {image}")
        log(log_fp, "INFO", f"OUTPUT DIR: {outdir}")
        if chmod_mode:
            log(log_fp, "INFO", f"CHMOD: {chmod_mode}")
        log(log_fp, "INFO", f"DRY-RUN: {dry_run}")

    # Ensure output directory exists (as an empty directory or existing root)
    if not dry_run:
        os.makedirs(outdir, exist_ok=True)
    else:
        if verbose:
            log(log_fp, "INFO", f"[DRY-RUN] mkdir -p {outdir}")

    # Use debugfs rdump to recursively extract the entire filesystem tree.
    # NOTE: We do NOT quote outdir here, because the string is parsed by
    # debugfs itself (not a shell). Quoting would become part of the argument.
    rdump_cmd = f"rdump / {outdir}"
    if dry_run:
        if verbose:
            log(
                log_fp,
                "INFO",
                f"[DRY-RUN] debugfs -R \"{rdump_cmd}\" {image}",
            )
    else:
        try:
            if verbose:
                log(log_fp, "INFO", f"Running: debugfs -R \"{rdump_cmd}\" {image}")
            run_debugfs_cmd(image, rdump_cmd)
        except subprocess.CalledProcessError as e:
            log(log_fp, "ERROR", f"debugfs rdump failed: {e.output.strip()}")
            close_log(log_fp)
            return 1

    # Count entries after extraction (or from existing tree in dry-run, if any)
    dirs, files, symlinks = count_entries(outdir)

    chmod_errors = 0
    if chmod_mode:
        log(log_fp, "INFO", f"Applying chmod -R {chmod_mode} to {outdir}")
        _, chmod_errors = apply_chmod_recursive(
            outdir, chmod_mode, dry_run, log_fp, verbose
        )

    # Summary
    print("[Convert RootFS (debugfs)]")
    print(f"       IMAGE: {image}")
    print(f"       OUTPUT DIR: {outdir}")
    print(f"       Logfile: {logfile if logfile else '-'}")
    print(f"       Dirs: {dirs}")
    print(f"       Files: {files}")
    print(f"       Symlinks: {symlinks}")
    print(f"       ERRORS: {chmod_errors}")

    close_log(log_fp)

    return 0 if chmod_errors == 0 else 1


def main() -> None:
    check_debugfs()
    args = parse_args()

    rc = extract_rootfs_debugfs(
        image=args.image,
        outdir=args.outdir,
        dry_run=args.dry_run,
        chmod_mode=args.chmod,
        logfile=args.logfile_rootfs,
        verbose=args.verbose,
    )
    if rc != 0:
        sys.exit(rc)
    print("[INFO] Done.")


if __name__ == "__main__":
    main()
