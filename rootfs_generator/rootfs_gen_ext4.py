#!/usr/bin/env python3
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

# Ensure this script's directory is in sys.path so local modules can be imported
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.append(SCRIPT_DIR)

# Import the symlink rewriting logic from the separate module

from rewrite_symlinks import rewrite_symlinks


import atexit
import signal

# Helper to find fusermount or fusermount3
def _get_fusermount_binary() -> str | None:
    """Return the name of the available fusermount binary, or None if not found."""
    for name in ("fusermount", "fusermount3"):
        if shutil.which(name) is not None:
            return name
    return None

# Global variable to track active FUSE mountpoint
ACTIVE_FUSE_MOUNT = None

def _cleanup_fuse_mount():
    """Unmount any active FUSE mount registered globally."""
    global ACTIVE_FUSE_MOUNT
    if ACTIVE_FUSE_MOUNT and os.path.ismount(ACTIVE_FUSE_MOUNT):
        fusermount_bin = _get_fusermount_binary()
        if fusermount_bin is not None:
            umount_cmd = [fusermount_bin, "-u", ACTIVE_FUSE_MOUNT]
            subprocess.run(umount_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ACTIVE_FUSE_MOUNT = None

# Register cleanup to run at normal interpreter exit
atexit.register(_cleanup_fuse_mount)

def _signal_handler(signum, frame):
    """Handle SIGINT/SIGTERM by unmounting and exiting."""
    _cleanup_fuse_mount()
    sys.exit(1)

# Register handlers for abnormal termination
signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def check_debugfs():
    """Ensure debugfs exists in PATH."""
    if shutil.which("debugfs") is None:
        print("Error: debugfs not found in PATH. Install e2fsprogs.", file=sys.stderr)
        sys.exit(1)


def check_fuse_tools():
    """Ensure fuse2fs, fusermount (or fusermount3), and rsync exist in PATH."""
    missing = []
    if shutil.which("fuse2fs") is None:
        missing.append("fuse2fs")
    if shutil.which("rsync") is None:
        missing.append("rsync")
    if _get_fusermount_binary() is None:
        missing.append("fusermount or fusermount3")
    if missing:
        print(f"Error: missing tools for FUSE mode: {', '.join(missing)}", file=sys.stderr)
        print("       Please install fuse2fs (or equivalent), fusermount/fusermount3, and rsync.", file=sys.stderr)
        sys.exit(1)


def run_debugfs_rdump(
    image_path: str,
    output_dir: str,
    dry_run: bool,
    logfile_rootfs: str | None = None,
    verbose: bool = False,
    copy_mode: str = "copytree",
):
    """
    Extract EXT4 image using debugfs rdump into a temp dir, then copy to output_dir.
    copy_mode controls how the temp dir is copied: 'copytree' (Python) or 'rsync'.
    Return (success, error_msg).
    """
    success = False
    error_msg: str | None = None

    log_fp = None
    if logfile_rootfs is not None:
        try:
            if logfile_rootfs:
                log_dir = os.path.dirname(logfile_rootfs)
                if log_dir:
                    os.makedirs(log_dir, exist_ok=True)
            log_fp = open(logfile_rootfs, "w", encoding="utf-8")
        except OSError as e:
            print(f"[ERROR] Cannot open logfile_rootfs '{logfile_rootfs}': {e}", file=sys.stderr)
            log_fp = None

    def log(prefix: str, msg: str):
        if log_fp is not None:
            log_fp.write(f"{prefix} {msg}\n")

    # Prepare a temporary directory for debugfs extraction
    tmp_parent = os.path.dirname(os.path.abspath(output_dir))
    if not tmp_parent:
        tmp_parent = "."
    tmp_dir = tempfile.mkdtemp(prefix="rootfs_extract_", dir=tmp_parent)

    try:
        cmd_str = f'debugfs -R "rdump / {tmp_dir}" {image_path}'

        if dry_run:
            msg = f"Would run: {cmd_str} and then copy to {output_dir}"
            if verbose:
                print(f"[DRY-RUN] {msg}")
            log("[INFO]", msg)
            success = True
            return success, error_msg

        # 1) Extract from image to temporary directory using debugfs
        cmd = ["debugfs", "-R", f"rdump / {tmp_dir}", image_path]

        if verbose:
            print(f"[INFO] Running: {' '.join(cmd)}")
        log("[INFO]", f"Run: {' '.join(cmd)}")

        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        if proc.returncode != 0:
            error_msg = f"debugfs rdump failed with exit code {proc.returncode}"
            if verbose:
                print("[ERROR] debugfs rdump failed.", file=sys.stderr)
                if proc.stdout:
                    print("[STDOUT]", proc.stdout, file=sys.stderr)
                if proc.stderr:
                    print("[STDERR]", proc.stderr, file=sys.stderr)
            log("[ERROR]", error_msg)
            if proc.stdout:
                log("[ERROR]", f"STDOUT: {proc.stdout.strip()}")
            if proc.stderr:
                log("[ERROR]", f"STDERR: {proc.stderr.strip()}")
            success = False
        else:
            # 2) Copy from temporary directory to final output_dir as the current user
            try:
                if verbose:
                    print(f"[INFO] Copying extracted tree from {tmp_dir} to {output_dir} using mode={copy_mode}")
                log("[INFO]", f"Copying extracted tree from {tmp_dir} to {output_dir} using mode={copy_mode}")
                os.makedirs(output_dir, exist_ok=True)

                if copy_mode == "copytree":
                    shutil.copytree(tmp_dir, output_dir, symlinks=True, dirs_exist_ok=True)
                elif copy_mode == "rsync":
                    if shutil.which("rsync") is None:
                        raise RuntimeError("rsync not found in PATH (required for --copy-mode rsync)")
                    rsync_cmd = [
                        "rsync",
                        "-a",
                        "--no-o",
                        "--no-g",
                        "--no-p",
                        "--links",
                    ]
                    if verbose:
                        rsync_cmd.append("--stats")
                    rsync_cmd.extend(
                        [
                            tmp_dir + "/",
                            output_dir + "/",
                        ]
                    )
                    if verbose:
                        print(f"[INFO] Running rsync: {' '.join(rsync_cmd)}")
                    log("[INFO]", f"Run rsync: {' '.join(rsync_cmd)}")
                    rsync_proc = subprocess.run(
                        rsync_cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    if rsync_proc.returncode != 0:
                        raise RuntimeError(
                            f"rsync failed with exit code {rsync_proc.returncode}: {rsync_proc.stderr.strip()}"
                        )
                else:
                    raise RuntimeError(f"Unknown copy_mode: {copy_mode}")

                if verbose:
                    print("[INFO] Extraction and copy completed successfully.")
                log("[INFO]", "Extraction and copy completed successfully.")
                success = True
            except Exception as e:
                error_msg = f"copy from temp dir failed: {e}"
                if verbose:
                    print(f"[ERROR] {error_msg}", file=sys.stderr)
                log("[ERROR]", error_msg)
                success = False

    finally:
        # Clean up temporary directory
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

        if log_fp is not None:
            log_fp.close()

    return success, error_msg


def run_ext4_mount_rsync(
    image_path: str,
    output_dir: str,
    dry_run: bool,
    logfile_rootfs: str | None = None,
    verbose: bool = False,
):
    """
    Extract EXT4 image by mounting it as a loop device with the kernel ext4 driver
    and copying with rsync. This requires that 'mount' and 'umount' are available,
    and typically requires sufficient privileges to perform a loop mount.

    Return (success, error_msg).
    """
    success = False
    error_msg: str | None = None

    log_fp = None
    if logfile_rootfs is not None:
        try:
            if logfile_rootfs:
                log_dir = os.path.dirname(logfile_rootfs)
                if log_dir:
                    os.makedirs(log_dir, exist_ok=True)
            log_fp = open(logfile_rootfs, "w", encoding="utf-8")
        except OSError as e:
            print(f"[ERROR] Cannot open logfile_rootfs '{logfile_rootfs}': {e}", file=sys.stderr)
            log_fp = None

    def log(prefix: str, msg: str):
        if log_fp is not None:
            log_fp.write(f"{prefix} {msg}\n")

    mount_parent = os.path.dirname(os.path.abspath(output_dir)) or "."
    mount_dir = os.path.join(mount_parent, ".rootfs_loop_mount")

    try:
        os.makedirs(mount_dir, exist_ok=True)

        # If something is already mounted here from a previous run, try to unmount it first.
        if os.path.ismount(mount_dir):
            warn_msg = f"Existing loop mount detected at {mount_dir}, attempting to unmount."
            if verbose:
                print(f"[WARNING] {warn_msg}")
            log("[WARNING]", warn_msg)
            umount_cmd = ["umount", mount_dir]
            if verbose:
                print(f"[INFO] Running: {' '.join(umount_cmd)}")
            log("[INFO]", f"Run: {' '.join(umount_cmd)}")
            umount_proc = subprocess.run(umount_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if umount_proc.returncode != 0:
                err = f"Failed to unmount previous loop mount at {mount_dir}: {umount_proc.stderr.strip()}"
                if verbose:
                    print(f"[ERROR] {err}", file=sys.stderr)
                log("[ERROR]", err)
                error_msg = err
                return False, error_msg

        mount_cmd_str = f"mount -t ext4 -o ro,loop {image_path} {mount_dir}"
        rsync_cmd_str = f"rsync -a --no-o --no-g --no-p --links {mount_dir}/ {output_dir}/"

        if dry_run:
            msg = f"Would run: {mount_cmd_str} && {rsync_cmd_str}"
            if verbose:
                print(f"[DRY-RUN] {msg}")
            log("[INFO]", msg)
            return True, None

        # 1) Mount the image with kernel ext4 via loop
        mount_cmd = ["mount", "-t", "ext4", "-o", "ro,loop", image_path, mount_dir]
        if verbose:
            print(f"[INFO] Running: {' '.join(mount_cmd)}")
        log("[INFO]", f"Run: {' '.join(mount_cmd)}")
        mount_proc = subprocess.run(mount_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if mount_proc.returncode != 0:
            error_msg = f"mount (ext4, loop) failed with exit code {mount_proc.returncode}"
            if verbose:
                print(f"[ERROR] {error_msg}", file=sys.stderr)
                if mount_proc.stdout:
                    print("[STDOUT]", mount_proc.stdout, file=sys.stderr)
                if mount_proc.stderr:
                    print("[STDERR]", mount_proc.stderr, file=sys.stderr)
            log("[ERROR]", error_msg)
            if mount_proc.stdout:
                log("[ERROR]", f"STDOUT: {mount_proc.stdout.strip()}")
            if mount_proc.stderr:
                log("[ERROR]", f"STDERR: {mount_proc.stderr.strip()}")
            return False, error_msg

        # Sanity check: mount should not be effectively empty
        try:
            entries = []
            for name in os.listdir(mount_dir):
                if name in (".", "..", "lost+found"):
                    continue
                entries.append(name)
        except OSError as e:
            error_msg = f"Failed to list contents of loop mount at {mount_dir}: {e}"
            if verbose:
                print(f"[ERROR] {error_msg}", file=sys.stderr)
            log("[ERROR]", error_msg)
            return False, error_msg

        if not entries:
            error_msg = (
                f"Loop mount at {mount_dir} appears to be empty (no entries except possibly 'lost+found'). "
                "This may indicate that the image is not a plain ext4 filesystem at offset 0."
            )
            if verbose:
                print(f"[ERROR] {error_msg}", file=sys.stderr)
            log("[ERROR]", error_msg)
            return False, error_msg

        # 2) Copy from mountpoint to output_dir using rsync
        if shutil.which("rsync") is None:
            error_msg = "rsync not found in PATH (required for ext4 mount mode)"
            if verbose:
                print(f"[ERROR] {error_msg}", file=sys.stderr)
            log("[ERROR]", error_msg)
            return False, error_msg

        os.makedirs(output_dir, exist_ok=True)
        rsync_cmd = [
            "rsync",
            "-a",
            "--no-o",
            "--no-g",
            "--no-p",
            "--links",
        ]
        if verbose:
            rsync_cmd.append("--stats")
        rsync_cmd.extend(
            [
                mount_dir + "/",
                output_dir + "/",
            ]
        )
        if verbose:
            print(f"[INFO] Running rsync: {' '.join(rsync_cmd)}")
        log("[INFO]", f"Run rsync: {' '.join(rsync_cmd)}")
        rsync_proc = subprocess.run(rsync_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if rsync_proc.returncode != 0:
            error_msg = f"rsync failed with exit code {rsync_proc.returncode}: {rsync_proc.stderr.strip()}"
            if verbose:
                print(f"[ERROR] {error_msg}", file=sys.stderr)
            log("[ERROR]", error_msg)
            success = False
        else:
            if verbose:
                print("[INFO] Extraction and copy completed successfully (ext4+rsync).")
            log("[INFO]", "Extraction and copy completed successfully (ext4+rsync).")
            success = True

    finally:
        # Always try to unmount the loop mount if it is still mounted.
        if os.path.ismount(mount_dir):
            umount_cmd = ["umount", mount_dir]
            if verbose:
                print(f"[INFO] Unmounting loop mount: {' '.join(umount_cmd)}")
            log("[INFO]", f"Run: {' '.join(umount_cmd)}")
            subprocess.run(umount_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        # Clean up mount directory if it is no longer a mount point.
        if not os.path.ismount(mount_dir):
            try:
                shutil.rmtree(mount_dir, ignore_errors=True)
            except Exception:
                pass

        if log_fp is not None:
            log_fp.close()

    return success, error_msg


def run_fuse_rsync(
    image_path: str,
    output_dir: str,
    dry_run: bool,
    logfile_rootfs: str | None = None,
    verbose: bool = False,
):
    """
    Extract EXT4 image by mounting it with fuse2fs and copying with rsync.
    This mode always uses rsync and ensures the mount is unmounted on exit.
    Return (success, error_msg).
    """
    success = False
    error_msg: str | None = None

    log_fp = None
    if logfile_rootfs is not None:
        try:
            if logfile_rootfs:
                log_dir = os.path.dirname(logfile_rootfs)
                if log_dir:
                    os.makedirs(log_dir, exist_ok=True)
            log_fp = open(logfile_rootfs, "w", encoding="utf-8")
        except OSError as e:
            print(f"[ERROR] Cannot open logfile_rootfs '{logfile_rootfs}': {e}", file=sys.stderr)
            log_fp = None

    def log(prefix: str, msg: str):
        if log_fp is not None:
            log_fp.write(f"{prefix} {msg}\n")

    # Use a fixed mount directory under the output directory's parent so we can
    # clean up correctly across runs.
    mount_parent = os.path.dirname(os.path.abspath(output_dir)) or "."
    mount_dir = os.path.join(mount_parent, ".rootfs_fuse_mount")

    # Track globally so abnormal exit can clean it
    global ACTIVE_FUSE_MOUNT
    ACTIVE_FUSE_MOUNT = mount_dir

    try:
        # Ensure mountpoint directory exists
        os.makedirs(mount_dir, exist_ok=True)

        # If something is already mounted here from a previous run, try to unmount it first.
        if os.path.ismount(mount_dir):
            warn_msg = f"Existing FUSE mount detected at {mount_dir}, attempting to unmount."
            if verbose:
                print(f"[WARNING] {warn_msg}")
            log("[WARNING]", warn_msg)
            fusermount_bin = _get_fusermount_binary()
            if fusermount_bin is None:
                err = "No fusermount/fusermount3 found in PATH to unmount existing FUSE mount."
                if verbose:
                    print(f"[ERROR] {err}", file=sys.stderr)
                log("[ERROR]", err)
                error_msg = err
                return False, error_msg
            umount_cmd = [fusermount_bin, "-u", mount_dir]
            if verbose:
                print(f"[INFO] Running: {' '.join(umount_cmd)}")
            log("[INFO]", f"Run: {' '.join(umount_cmd)}")
            umount_proc = subprocess.run(umount_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if umount_proc.returncode != 0:
                err = f"Failed to unmount previous FUSE mount at {mount_dir}: {umount_proc.stderr.strip()}"
                if verbose:
                    print(f"[ERROR] {err}", file=sys.stderr)
                log("[ERROR]", err)
                error_msg = err
                return False, error_msg

        fuse_cmd_str = f"fuse2fs -o ro {image_path} {mount_dir}"
        rsync_cmd_str = f"rsync -a --no-o --no-g --no-p --links {mount_dir}/ {output_dir}/"

        if dry_run:
            msg = f"Would run: {fuse_cmd_str} && {rsync_cmd_str}"
            if verbose:
                print(f"[DRY-RUN] {msg}")
            log("[INFO]", msg)
            return True, None

        # 1) Mount the image with fuse2fs
        fuse_cmd = ["fuse2fs", "-o", "ro", image_path, mount_dir]
        if verbose:
            print(f"[INFO] Running: {' '.join(fuse_cmd)}")
        log("[INFO]", f"Run: {' '.join(fuse_cmd)}")
        fuse_proc = subprocess.run(fuse_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if fuse_proc.returncode != 0:
            error_msg = f"fuse2fs mount failed with exit code {fuse_proc.returncode}"
            if verbose:
                print(f"[ERROR] {error_msg}", file=sys.stderr)
                if fuse_proc.stdout:
                    print("[STDOUT]", fuse_proc.stdout, file=sys.stderr)
                if fuse_proc.stderr:
                    print("[STDERR]", fuse_proc.stderr, file=sys.stderr)
            log("[ERROR]", error_msg)
            if fuse_proc.stdout:
                log("[ERROR]", f"STDOUT: {fuse_proc.stdout.strip()}")
            if fuse_proc.stderr:
                log("[ERROR]", f"STDERR: {fuse_proc.stderr.strip()}")
            return False, error_msg

        # Sanity check: FUSE mount should not be effectively empty
        try:
            entries = []
            for name in os.listdir(mount_dir):
                # Ignore typical ext* reserved dir
                if name in (".", "..", "lost+found"):
                    continue
                entries.append(name)
        except OSError as e:
            error_msg = f"Failed to list contents of FUSE mount at {mount_dir}: {e}"
            if verbose:
                print(f"[ERROR] {error_msg}", file=sys.stderr)
            log("[ERROR]", error_msg)
            return False, error_msg

        if not entries:
            error_msg = (
                f"FUSE mount at {mount_dir} appears to be empty (no entries except possibly 'lost+found'). "
                "This may indicate that the image is not a plain ext4 filesystem at offset 0."
            )
            if verbose:
                print(f"[ERROR] {error_msg}", file=sys.stderr)
            log("[ERROR]", error_msg)
            return False, error_msg

        # 2) Copy from mountpoint to output_dir using rsync
        if shutil.which("rsync") is None:
            error_msg = "rsync not found in PATH (required for FUSE mode)"
            if verbose:
                print(f"[ERROR] {error_msg}", file=sys.stderr)
            log("[ERROR]", error_msg)
            return False, error_msg

        os.makedirs(output_dir, exist_ok=True)
        rsync_cmd = [
            "rsync",
            "-a",
            "--no-o",
            "--no-g",
            "--no-p",
            "--links",
        ]
        if verbose:
            rsync_cmd.append("--stats")
        rsync_cmd.extend(
            [
                mount_dir + "/",
                output_dir + "/",
            ]
        )
        if verbose:
            print(f"[INFO] Running rsync: {' '.join(rsync_cmd)}")
        log("[INFO]", f"Run rsync: {' '.join(rsync_cmd)}")
        rsync_proc = subprocess.run(rsync_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if rsync_proc.returncode != 0:
            error_msg = f"rsync failed with exit code {rsync_proc.returncode}: {rsync_proc.stderr.strip()}"
            if verbose:
                print(f"[ERROR] {error_msg}", file=sys.stderr)
            log("[ERROR]", error_msg)
            success = False
        else:
            if verbose:
                print("[INFO] Extraction and copy completed successfully (FUSE+rsync).")
            log("[INFO]", "Extraction and copy completed successfully (FUSE+rsync).")
            success = True

    finally:
        # Always try to unmount the FUSE mount if it is still mounted.
        if os.path.ismount(mount_dir):
            fusermount_bin = _get_fusermount_binary()
            if fusermount_bin is not None:
                umount_cmd = [fusermount_bin, "-u", mount_dir]
                if verbose:
                    print(f"[INFO] Unmounting FUSE mount: {' '.join(umount_cmd)}")
                log("[INFO]", f"Run: {' '.join(umount_cmd)}")
                subprocess.run(umount_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        # Clean up mount directory if it is no longer a mount point.
        if not os.path.ismount(mount_dir):
            try:
                shutil.rmtree(mount_dir, ignore_errors=True)
            except Exception:
                pass

        # Clear global mount tracker if cleaned
        if not os.path.ismount(mount_dir):
            ACTIVE_FUSE_MOUNT = None

        if log_fp is not None:
            log_fp.close()

    return success, error_msg


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Extract EXT4 image using debugfs and optionally rewrite absolute symlinks "
            "so that '/' points to the given ROOTFS directory."
        )
    )
    parser.add_argument("image", help="Path to EXT4 image file")
    parser.add_argument("--rootfs", required=True, help="Directory that represents '/' for symlink rewriting")
    parser.add_argument("--outdir", required=False, help="Output directory. Defaults to --rootfs")

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write changes, only show what would happen",
    )

    parser.add_argument(
        "--skip-extract",
        action="store_true",
        help="Skip EXT4 extraction step",
    )

    parser.add_argument(
        "--skip-symlinks",
        action="store_true",
        help="Skip symlink rewriting step",
    )

    parser.add_argument(
        "--reset-outdir",
        action="store_true",
        help=(
            "If the output directory already exists and is non-empty, remove its contents "
            "before extraction. This helps avoid rsync Permission denied errors when "
            "reusing a ROOTFS directory that contains files or directories with restrictive permissions."
        ),
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print detailed progress to stdout",
    )

    parser.add_argument(
        "--logfile-rootfs",
        help="Path for rootfs generator logs",
    )

    parser.add_argument(
        "--logfile-symlink",
        help="Path for symlink rewrite logs",
    )

    parser.add_argument(
        "--broken-report",
        help="Path for broken symlink report file",
    )

    parser.add_argument(
        "--chmod",
        help="Apply chmod -R <value> to output directory after extraction",
    )

    parser.add_argument(
        "--copy-mode",
        choices=["copytree", "rsync"],
        default="copytree",
        help="How to copy from temporary extract dir to output dir: 'copytree' (Python) or 'rsync'",
    )

    parser.add_argument(
        "--mode",
        type=int,
        choices=[1, 2, 3],
        default=2,
        help=(
            "Extraction mode: "
            "1 = debugfs + copytree, "
            "2 = debugfs + rsync, "
            "3 = auto: ext4 loop mount + rsync, then FUSE (fuse2fs) + rsync, then debugfs + rsync"
        ),
    )

    return parser.parse_args()


def main():
    args = parse_args()

    image_path = os.path.abspath(args.image)
    rootfs_dir = os.path.abspath(args.rootfs)

    # Default output_dir = rootfs_dir if omitted
    if args.outdir is None:
        print("[INFO] outdir not provided. Using rootfs as output_dir.")
        output_dir = rootfs_dir
    else:
        output_dir = os.path.abspath(args.outdir)

    chmod_value = args.chmod
    copy_mode = args.copy_mode

    mode = args.mode

    logfile_rootfs = args.logfile_rootfs
    logfile_symlink = args.logfile_symlink
    broken_report = args.broken_report

    if not os.path.isfile(image_path):
        print(f"Error: image file not found: {image_path}", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(rootfs_dir):
        try:
            os.makedirs(rootfs_dir, exist_ok=True)
            if args.verbose:
                print(f"[INFO] rootfs directory did not exist. Created: {rootfs_dir}")
        except OSError as e:
            print(f"Error: failed to create rootfs directory '{rootfs_dir}': {e}", file=sys.stderr)
            sys.exit(1)
    elif not os.path.isdir(rootfs_dir):
        print(f"Error: rootfs exists but is not a directory: {rootfs_dir}", file=sys.stderr)
        sys.exit(1)

    # Handle reset-outdir semantics before extraction to avoid noisy rsync permission errors.
    if not args.skip_extract and os.path.isdir(output_dir):
        try:
            existing_entries = os.listdir(output_dir)
        except OSError as e:
            print(f"Error: cannot list output directory '{output_dir}': {e}", file=sys.stderr)
            sys.exit(1)

        if existing_entries:
            if args.reset_outdir:
                if args.verbose:
                    print(f"[INFO] --reset-outdir: removing existing contents of {output_dir}")
                for name in existing_entries:
                    path = os.path.join(output_dir, name)
                    # Remove directories (not symlinks) and files separately
                    if os.path.isdir(path) and not os.path.islink(path):
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        try:
                            os.unlink(path)
                        except FileNotFoundError:
                            pass
            else:
                print(
                    f"Error: output directory '{output_dir}' is not empty. "
                    "To avoid rsync Permission denied errors on existing read-only files, "
                    "either clean it manually or rerun with --reset-outdir.",
                    file=sys.stderr,
                )
                sys.exit(1)

    if mode in (1, 2):
        check_debugfs()
    # mode 3 will perform its own fallback sequence (ext4 -> FUSE -> debugfs) without hard exits here.

    # Optional pre-extraction chmod to reduce rsync permission issues when reusing an existing ROOTFS
    if chmod_value and not args.skip_extract and os.path.isdir(output_dir):
        if args.verbose:
            print(f"[INFO] Pre-extraction chmod -R {chmod_value} to {output_dir}")
        subprocess.run(["chmod", "-R", chmod_value, output_dir], check=False)

    extract_success = True
    extract_error_msg: str | None = None

    # 1) Extract EXT4 image
    if not args.skip_extract:
        if mode in (1, 2):
            # Mode 1: debugfs + copytree, Mode 2: debugfs + rsync
            effective_copy_mode = "copytree" if mode == 1 else "rsync"
            extract_success, extract_error_msg = run_debugfs_rdump(
                image_path,
                output_dir,
                args.dry_run,
                logfile_rootfs=logfile_rootfs,
                verbose=args.verbose,
                copy_mode=effective_copy_mode,
            )
        elif mode == 3:
            # Mode 3: auto chain:
            # 1) kernel ext4 loop mount + rsync
            # 2) FUSE (fuse2fs) + rsync
            # 3) debugfs + rsync
            extract_success = False
            extract_error_msg = None

            # Step 1: try kernel ext4 loop mount
            if args.verbose:
                print("[INFO] Trying ext4 loop mount + rsync...")
            extract_success, extract_error_msg = run_ext4_mount_rsync(
                image_path,
                output_dir,
                args.dry_run,
                logfile_rootfs=logfile_rootfs,
                verbose=args.verbose,
            )

            # Step 2: FUSE fallback if ext4 loop mount failed
            if not extract_success:
                if args.verbose and extract_error_msg:
                    print(f"[WARNING] ext4 loop mount + rsync failed: {extract_error_msg}")
                has_fuse2fs = shutil.which("fuse2fs") is not None
                has_rsync = shutil.which("rsync") is not None
                if has_fuse2fs and has_rsync:
                    if args.verbose:
                        print("[INFO] Falling back to FUSE (fuse2fs + rsync)...")
                    extract_success, extract_error_msg = run_fuse_rsync(
                        image_path,
                        output_dir,
                        args.dry_run,
                        logfile_rootfs=logfile_rootfs,
                        verbose=args.verbose,
                    )
                else:
                    if args.verbose:
                        print("[INFO] Skipping FUSE fallback; fuse2fs/rsync not available.")

            # Step 3: debugfs + rsync fallback if FUSE also failed
            if not extract_success:
                if args.verbose and extract_error_msg:
                    print(f"[WARNING] FUSE (fuse2fs + rsync) fallback failed: {extract_error_msg}")
                if shutil.which("debugfs") is not None:
                    if args.verbose:
                        print("[INFO] Falling back to debugfs + rsync...")
                    extract_success, extract_error_msg = run_debugfs_rdump(
                        image_path,
                        output_dir,
                        args.dry_run,
                        logfile_rootfs=logfile_rootfs,
                        verbose=args.verbose,
                        copy_mode="rsync",
                    )
                else:
                    if args.verbose:
                        print("[ERROR] debugfs not found; cannot perform final fallback.", file=sys.stderr)
        else:
            print(f"Error: unsupported mode {mode}", file=sys.stderr)
            sys.exit(1)
    else:
        print("[INFO] Skipping extraction (--skip-extract).")
        extract_success = True
        extract_error_msg = None

    # If extraction failed, report and exit immediately without chmod or symlink rewriting.
    if not extract_success:
        success_count = 0
        error_count = 1
        warning_count = 0

        print("[Convert RootFS]")
        print(f"       IMAGE: {image_path}")
        print(f"       OUTPUT DIR: {output_dir}")
        print(f"       Logfile: {logfile_rootfs}")
        print(f"       Success: {success_count}")
        print(f"       ERRORS: {error_count}")
        print(f"       WARNINGS: {warning_count}")

        print("[INFO] Done.")
        sys.exit(1)

    # 1. Extraction succeeded: optional chmod
    if chmod_value:
        if args.verbose:
            print(f"[INFO] Applying chmod -R {chmod_value} to {output_dir}")
        subprocess.run(["chmod", "-R", chmod_value, output_dir], check=False)

    # 2) Rewrite symlinks
    if not args.skip_symlinks:
        rewrite_symlinks(
            rootfs_dir,
            output_dir,
            dry_run=args.dry_run,
            logfile_symlink=logfile_symlink,
            verbose=args.verbose,
            broken_report=broken_report,
        )
    else:
        print("[INFO] Skipping symlink rewrite (--skip-symlinks).")

    # Final summary for successful extraction
    success_count = 1
    error_count = 0
    warning_count = 0

    print("[Convert RootFS]")
    print(f"       IMAGE: {image_path}")
    print(f"       OUTPUT DIR: {output_dir}")
    print(f"       Logfile: {logfile_rootfs}")
    print(f"       Success: {success_count}")
    print(f"       ERRORS: {error_count}")
    print(f"       WARNINGS: {warning_count}")

    print("[INFO] Done.")

    sys.exit(0)


if __name__ == "__main__":
    main()
