#!/usr/bin/env python3
import argparse
import os
import sys
import stat
import subprocess
from typing import Any, Dict, Tuple

# Adjust sys.path so that we can import rewrite_symlinks regardless of CWD.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    # Preferred: package-style import when this file lives under rootfs_generator/
    from rootfs_generator.rewrite_symlinks import rewrite_symlinks
except ImportError:
    try:
        # Alternative: package-style import when the package name matches this directory
        from rootfs_genertor.rewrite_symlinks import rewrite_symlinks  # type: ignore
    except ImportError:
        # Fallback: module in the same directory
        from rewrite_symlinks import rewrite_symlinks  # type: ignore


# -----------------------------
# Logging helpers
# -----------------------------


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


# -----------------------------
# Command helpers
# -----------------------------


def run_cmd(cmd: list[str], fp: Any, verbose: bool, desc: str) -> str:
    """
    Run a command, log it, and return combined stdout+stderr.
    Raise CalledProcessError on failure.
    """
    if verbose:
        log(fp, "INFO", f"Running: {' '.join(cmd)} ({desc})")
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        log(fp, "ERROR", f"{desc} failed with code {proc.returncode}")
        log(fp, "ERROR", proc.stdout.strip())
        raise subprocess.CalledProcessError(proc.returncode, cmd, output=proc.stdout)
    return proc.stdout


def check_tool_exists(tool: str, args: list[str], fp: Any, desc: str) -> None:
    cmd = [tool] + args
    try:
        subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except Exception as e:
        log(fp, "ERROR", f"{desc} not available: {' '.join(cmd)}")
        log(fp, "ERROR", f"Original error: {e}")
        sys.exit(1)


# -----------------------------
# Environment checks
# -----------------------------


def check_debugfs(fp: Any) -> None:
    check_tool_exists("debugfs", ["-V"], fp, "debugfs")


def check_rsync(fp: Any) -> None:
    check_tool_exists("rsync", ["--version"], fp, "rsync")


def check_fuse2fs(fp: Any) -> None:
    check_tool_exists("fuse2fs", ["-V"], fp, "fuse2fs")


# -----------------------------
# Filesystem helpers
# -----------------------------


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
        dir_count += len(dirnames)
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                # The file disappeared or is inaccessible; skip it.
                continue
            if stat.S_ISLNK(st.st_mode):
                symlink_count += 1
            elif stat.S_ISREG(st.st_mode):
                file_count += 1
            else:
                file_count += 1

    return dir_count, file_count, symlink_count


def apply_chmod_recursive(
    root: str, mode_str: str, dry_run: bool, fp: Any, verbose: bool
) -> Tuple[int, int]:
    """
    Apply chmod -R MODE to root (best-effort).
    Returns (success_count, error_count).
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


def scan_root_owned(
    root: str,
    fp: Any,
    report_path: str | None,
    verbose: bool,
) -> Tuple[int, int]:
    """
    Scan the extracted ROOTFS for entries owned by UID 0 (root).
    Returns (root_owned_dirs, root_owned_files).
    Optionally writes a report file listing each root-owned directory/file.
    """
    root_owned_dirs = 0
    root_owned_files = 0

    if not os.path.isdir(root):
        return 0, 0

    report_fp: Any = None
    if report_path:
        os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
        report_fp = open(report_path, "w", encoding="utf-8")

    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            # Directories
            for dname in dirnames:
                full = os.path.join(dirpath, dname)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                if st.st_uid == 0:
                    root_owned_dirs += 1
                    if report_fp is not None:
                        report_fp.write(f"DIR  {full}\n")
            # Files (regular or others)
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                if st.st_uid == 0:
                    root_owned_files += 1
                    if report_fp is not None:
                        report_fp.write(f"FILE {full}\n")
    finally:
        if report_fp is not None:
            report_fp.close()

    if report_path and verbose:
        log(fp, "INFO", f"Root-owned report written to: {report_path}")

    return root_owned_dirs, root_owned_files


def ensure_dir(path: str, dry_run: bool, fp: Any, verbose: bool) -> None:
    if dry_run:
        if verbose:
            log(fp, "INFO", f"[DRY-RUN] mkdir -p {path}")
        return
    os.makedirs(path, exist_ok=True)


# -----------------------------
# Mode 1: debugfs rdump
# -----------------------------


def extract_mode1_debugfs_rdump(
    image: str,
    outdir: str,
    dry_run: bool,
    fp: Any,
    verbose: bool,
) -> Dict[str, int]:
    """
    Mode 1: Use debugfs rdump to extract the entire filesystem tree.
    """
    if dry_run:
        log(
            fp,
            "INFO",
            f"[DRY-RUN] debugfs -R \"rdump / {outdir}\" {image}",
        )
        dirs, files, symlinks = count_entries(outdir)
        return {
            "dirs": dirs,
            "files": files,
            "symlinks": symlinks,
            "warnings": 0,
            "errors": 0,
        }

    ensure_dir(outdir, dry_run=False, fp=fp, verbose=verbose)

    rdump_cmd = ["debugfs", "-R", f"rdump / {outdir}", image]
    run_cmd(rdump_cmd, fp, verbose, "debugfs rdump")

    dirs, files, symlinks = count_entries(outdir)
    return {
        "dirs": dirs,
        "files": files,
        "symlinks": symlinks,
        "warnings": 0,
        "errors": 0,
    }


# -----------------------------
# Mode 2: debugfs rdump -> rsync
# -----------------------------


def extract_mode2_debugfs_rsync(
    image: str,
    outdir: str,
    dry_run: bool,
    fp: Any,
    verbose: bool,
) -> Dict[str, int]:
    """
    Mode 2: Use debugfs rdump to a temporary directory, then rsync to outdir
    without preserving owner/group/perms.
    """
    parent = os.path.dirname(os.path.abspath(outdir))
    tmp_rdump = os.path.join(parent, ".rootfs_rdump_tmp")

    if dry_run:
        log(
            fp,
            "INFO",
            f"[DRY-RUN] debugfs -R \"rdump / {tmp_rdump}\" {image}",
        )
        log(
            fp,
            "INFO",
            f"[DRY-RUN] rsync -a --no-o --no-g --no-p --links {tmp_rdump}/ {outdir}/",
        )
        dirs, files, symlinks = count_entries(outdir)
        return {
            "dirs": dirs,
            "files": files,
            "symlinks": symlinks,
            "warnings": 0,
            "errors": 0,
        }

    # 1) rdump to tmp
    ensure_dir(tmp_rdump, dry_run=False, fp=fp, verbose=verbose)
    rdump_cmd = ["debugfs", "-R", f"rdump / {tmp_rdump}", image]
    run_cmd(rdump_cmd, fp, verbose, "debugfs rdump (mode2 tmp)")

    # 2) rsync to final outdir
    ensure_dir(outdir, dry_run=False, fp=fp, verbose=verbose)
    rsync_cmd = [
        "rsync",
        "-a",
        "--no-o",
        "--no-g",
        "--no-p",
        "--links",
        tmp_rdump.rstrip("/") + "/",
        outdir.rstrip("/") + "/",
    ]
    run_cmd(rsync_cmd, fp, verbose, "rsync tmp -> outdir (mode2)")

    # 3) Optionally remove tmp_rdump (not strictly required)
    try:
        import shutil

        shutil.rmtree(tmp_rdump)
        log(fp, "INFO", f"Removed temporary rdump directory: {tmp_rdump}")
    except Exception as e:
        log(fp, "WARNING", f"Failed to remove temporary rdump directory {tmp_rdump}: {e}")

    dirs, files, symlinks = count_entries(outdir)
    return {
        "dirs": dirs,
        "files": files,
        "symlinks": symlinks,
        "warnings": 0,
        "errors": 0,
    }


# -----------------------------
# Mode 3: fuse2fs + rsync
# -----------------------------


def try_unmount(mount_dir: str, fp: Any, verbose: bool) -> None:
    """
    Try to unmount the given mount directory using fusermount3, fusermount, or umount.
    """
    for cmd in (["fusermount3", "-u", mount_dir], ["fusermount", "-u", mount_dir], ["umount", mount_dir]):
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            if verbose:
                log(fp, "INFO", f"Unmounted {mount_dir} using {' '.join(cmd)}")
            return
        except Exception:
            continue
    log(
        fp,
        "WARNING",
        f"Failed to unmount {mount_dir} with fusermount3/fusermount/umount; manual cleanup may be required.",
    )


def cleanup_stale_mount(mount_dir: str, fp: Any, verbose: bool) -> None:
    """
    Best-effort cleanup of a stale mount directory from previous runs.

    This will only try to unmount if the directory already exists.
    """
    if not os.path.isdir(mount_dir):
        return
    try_unmount(mount_dir, fp, verbose)


def extract_mode3_fuse_rsync(
    image: str,
    outdir: str,
    dry_run: bool,
    fp: Any,
    verbose: bool,
) -> Dict[str, int]:
    """
    Mode 3: Use fuse2fs to mount the image read-only, then rsync to outdir.
    If anything fails here, the caller may decide to fall back to Mode 1.
    """
    parent = os.path.dirname(os.path.abspath(outdir))
    mount_dir = os.path.join(parent, ".rootfs_fuse_mount")

    # Best-effort cleanup of any stale mount from previous runs.
    cleanup_stale_mount(mount_dir, fp, verbose)

    if dry_run:
        log(
            fp,
            "INFO",
            f"[DRY-RUN] fuse2fs -o ro {image} {mount_dir}",
        )
        log(
            fp,
            "INFO",
            f"[DRY-RUN] rsync -a --no-o --no-g --no-p --links {mount_dir}/ {outdir}/",
        )
        dirs, files, symlinks = count_entries(outdir)
        return {
            "dirs": dirs,
            "files": files,
            "symlinks": symlinks,
            "warnings": 0,
            "errors": 0,
        }

    ensure_dir(mount_dir, dry_run=False, fp=fp, verbose=verbose)
    ensure_dir(outdir, dry_run=False, fp=fp, verbose=verbose)

    mounted = False
    try:
        # 1) Mount using fuse2fs
        fuse_cmd = ["fuse2fs", "-o", "ro", image, mount_dir]
        run_cmd(fuse_cmd, fp, verbose, "fuse2fs mount")
        mounted = True

        # 2) Sanity-check mount is not empty (except possibly lost+found)
        try:
            entries = os.listdir(mount_dir)
        except OSError as e:
            raise RuntimeError(f"Cannot list mount_dir {mount_dir}: {e}") from e

        visible = [e for e in entries if e not in (".", "..", "lost+found")]
        if not visible:
            raise RuntimeError(
                f"FUSE mount at {mount_dir} appears empty (no entries except possibly 'lost+found')."
            )

        # 3) rsync from mount to outdir
        rsync_cmd = [
            "rsync",
            "-a",
            "--no-o",
            "--no-g",
            "--no-p",
            "--links",
            mount_dir.rstrip("/") + "/",
            outdir.rstrip("/") + "/",
        ]
        run_cmd(rsync_cmd, fp, verbose, "rsync fuse -> outdir (mode3)")

    finally:
        if mounted:
            try_unmount(mount_dir, fp, verbose)

    dirs, files, symlinks = count_entries(outdir)
    return {
        "dirs": dirs,
        "files": files,
        "symlinks": symlinks,
        "warnings": 0,
        "errors": 0,
    }


# -----------------------------
# Orchestration
# -----------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Hybrid ROOTFS generator for EXT4 images.\n"
            "Mode 1: debugfs rdump\n"
            "Mode 2: debugfs rdump -> rsync (--no-owner/--no-group/--no-perms)\n"
            "Mode 3: fuse2fs + rsync (fast), fallback to Mode 1 on failure"
        )
    )
    parser.add_argument("image", help="Path to EXT4 image file")
    parser.add_argument(
        "--outdir",
        required=True,
        help="Output ROOTFS directory where the image content will be extracted.",
    )
    parser.add_argument(
        "--mode",
        type=int,
        choices=(1, 2, 3),
        default=3,
        help="Extraction mode: 1=debugfs, 2=debugfs+rsync, 3=fuse+rsync (default: 3)",
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
        "--chmod-scope",
        choices=("outdir", "rootfs"),
        default="outdir",
        help=(
            "Scope for chmod operation when --chmod is used: "
            "'outdir' (default) only touches the extracted subtree, "
            "'rootfs' uses the logical ROOTFS directory."
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
        "--rewrite-symlinks",
        action="store_true",
        help="If set, run symlink rewrite step after extraction.",
    )
    parser.add_argument(
        "--logfile-symlink",
        help="Log file path for symlink rewrite logs (overwritten on each run).",
    )
    parser.add_argument(
        "--broken-report-symlink",
        help="Log file path for broken symlink report.",
    )
    parser.add_argument(
        "--root-owned-report",
        help="Path to save report of root-owned directories and files.",
    )
    parser.add_argument(
        "--rootfs-dir",
        help=(
            "Logical ROOTFS directory corresponding to '/'. "
            "If not set, defaults to the outdir for this image."
        ),
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose output to stdout."
    )
    return parser.parse_args()


def run_hybrid(
    image: str,
    outdir: str,
    mode: int,
    chmod_mode: str | None,
    chmod_scope: str,
    logfile_rootfs: str | None,
    dry_run: bool,
    verbose: bool,
    rewrite_symlinks_flag: bool,
    logfile_symlink: str | None,
    broken_report_symlink: str | None,
    root_owned_report: str | None,
    rootfs_dir: str | None,
) -> int:
    log_fp = open_log(logfile_rootfs)
    image = os.path.abspath(image)
    outdir = os.path.abspath(outdir)
    rootfs_dir_effective = (
        os.path.abspath(rootfs_dir) if rootfs_dir else outdir
    )

    # Basic environment checks
    if mode in (1, 2, 3):
        check_debugfs(log_fp)
    if mode in (2, 3):
        check_rsync(log_fp)
    if mode == 3:
        check_fuse2fs(log_fp)

    if verbose:
        log(log_fp, "INFO", f"IMAGE: {image}")
        log(log_fp, "INFO", f"OUTPUT DIR: {outdir}")
        log(log_fp, "INFO", f"ROOTFS DIR (logical '/'): {rootfs_dir_effective}")
        log(log_fp, "INFO", f"MODE: {mode}")
        if chmod_mode:
            log(log_fp, "INFO", f"CHMOD: {chmod_mode} (scope={chmod_scope})")
        log(log_fp, "INFO", f"DRY-RUN: {dry_run}")

    result: Dict[str, int] = {
        "dirs": 0,
        "files": 0,
        "symlinks": 0,
        "warnings": 0,
        "errors": 0,
    }
    mode_used = mode
    fallback_used = False

    try:
        if mode == 1:
            result = extract_mode1_debugfs_rdump(image, outdir, dry_run, log_fp, verbose)
        elif mode == 2:
            result = extract_mode2_debugfs_rsync(image, outdir, dry_run, log_fp, verbose)
        elif mode == 3:
            try:
                result = extract_mode3_fuse_rsync(image, outdir, dry_run, log_fp, verbose)
            except Exception as e:
                # Fallback to Mode 1
                fallback_used = True
                log(
                    log_fp,
                    "WARNING",
                    f"Mode 3 (fuse2fs+rsync) failed: {e}. Falling back to Mode 1 (debugfs rdump).",
                )
                result = extract_mode1_debugfs_rdump(image, outdir, dry_run, log_fp, verbose)
                mode_used = 1
    finally:
        pass

    chmod_errors = 0
    if chmod_mode:
        if chmod_scope == "rootfs":
            chmod_target = rootfs_dir_effective
        else:
            chmod_target = outdir
        log(
            log_fp,
            "INFO",
            f"Applying chmod -R {chmod_mode} to {chmod_target} (scope={chmod_scope})",
        )
        _, chmod_errors = apply_chmod_recursive(
            chmod_target, chmod_mode, dry_run, log_fp, verbose
        )

    # Optional symlink rewrite step
    # Note: rewrite_symlinks() itself prints its own [Convert Symlinks] summary.
    extra_rewrite_error = 0
    if rewrite_symlinks_flag:
        log(log_fp, "INFO", "Running symlink rewrite step.")
        try:
            rewrite_symlinks(
                rootfs_dir=rootfs_dir_effective,
                target_dir=outdir,
                dry_run=dry_run,
                logfile_symlink=logfile_symlink,
                broken_report=broken_report_symlink,
                verbose=verbose,
            )
        except Exception as e:
            log(log_fp, "ERROR", f"Symlink rewrite failed: {e}")
            extra_rewrite_error = 1

    # Scan for root-owned entries in the final ROOTFS (skip in dry-run)
    root_owned_dirs = 0
    root_owned_files = 0
    if not dry_run:
        root_owned_dirs, root_owned_files = scan_root_owned(
            outdir, log_fp, root_owned_report, verbose
        )

    # Summary
    logfile_display = logfile_rootfs if logfile_rootfs else "-"
    warnings_total = result.get("warnings", 0) + chmod_errors
    errors_total = result.get("errors", 0) + extra_rewrite_error
    # warnings_total = result.get("warnings", 0)
    # errors_total = result.get("errors", 0) + chmod_errors + extra_rewrite_error

    print("[Convert RootFS]")
    print(f"       IMAGE: {image}")
    print(f"       OUTPUT DIR: {outdir}")
    print(f"       MODE: {mode_used}")
    print(f"       Logfile: {logfile_display}")
    print(f"       Dirs: {result.get('dirs', 0)}")
    print(f"       Files: {result.get('files', 0)}")
    print(f"       Symlinks: {result.get('symlinks', 0)}")
    print(f"       ROOT-OWNED DIRS: {root_owned_dirs}")
    print(f"       ROOT-OWNED FILES: {root_owned_files}")
    print(f"       WARNINGS: {warnings_total}")
    print(f"       ERRORS: {errors_total}")
    if fallback_used:
        print(f"       FALLBACK: mode 3 -> mode 1")

    close_log(log_fp)

    return 0 if errors_total == 0 else 1


def main() -> None:
    args = parse_args()
    rc = run_hybrid(
        image=args.image,
        outdir=args.outdir,
        mode=args.mode,
        chmod_mode=args.chmod,
        chmod_scope=args.chmod_scope,
        logfile_rootfs=args.logfile_rootfs,
        dry_run=args.dry_run,
        verbose=args.verbose,
        rewrite_symlinks_flag=args.rewrite_symlinks,
        logfile_symlink=args.logfile_symlink,
        broken_report_symlink=args.broken_report_symlink,
        root_owned_report=args.root_owned_report,
        rootfs_dir=args.rootfs_dir,
    )
    if rc != 0:
        sys.exit(rc)
    print("[INFO] Done.")


if __name__ == "__main__":
    main()