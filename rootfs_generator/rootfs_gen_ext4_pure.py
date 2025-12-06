#!/usr/bin/env python3
import argparse
import os
import sys
import stat
import errno

# ext4.py (cubinator/ext4) must be in the same directory or on PYTHONPATH
import ext4  # type: ignore


def parse_args():
    parser = argparse.ArgumentParser(
        description="Extract EXT4 image in pure userspace using cubinator/ext4."
    )
    parser.add_argument("image", help="Path to EXT4 image file")
    parser.add_argument(
        "--outdir",
        required=True,
        help="Output ROOTFS directory (will be created if missing)",
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Byte offset of ext4 filesystem inside the image (default: 0)",
    )
    parser.add_argument(
        "--chmod",
        help="Apply chmod -R <value> to OUTPUT dir after extraction (e.g. 755)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write any files, only print what would happen",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose output",
    )
    parser.add_argument(
        "--logfile-rootfs",
        help="Path for rootfs extraction log (overwritten each run)",
    )
    return parser.parse_args()


def open_log(log_path: str | None):
    if not log_path:
        return None
    try:
        log_dir = os.path.dirname(log_path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        return open(log_path, "w", encoding="utf-8")
    except OSError as e:
        print(f"[ERROR] Cannot open logfile-rootfs '{log_path}': {e}", file=sys.stderr)
        return None


def log(fp, level: str, msg: str):
    if fp is not None:
        fp.write(f"[{level}] {msg}\n")


def ensure_dir(path: str, dry_run: bool, verbose: bool, log_fp):
    if dry_run:
        if verbose:
            print(f"[DRY-RUN] mkdir -p {path}")
        log(log_fp, "INFO", f"mkdir -p {path}")
        return

    if not os.path.isdir(path):
        if verbose:
            print(f"[INFO] mkdir -p {path}")
        log(log_fp, "INFO", f"mkdir -p {path}")
        os.makedirs(path, exist_ok=True)


def write_file(target_path: str, inode, dry_run: bool, verbose: bool, log_fp):
    """
    Create or overwrite a regular file at target_path with the contents
    of the given ext4 inode.
    """
    parent = os.path.dirname(target_path)
    ensure_dir(parent, dry_run, verbose, log_fp)

    if dry_run:
        if verbose:
            print(f"[DRY-RUN] write file {target_path}")
        log(log_fp, "INFO", f"write file {target_path}")
        return

    if verbose:
        print(f"[INFO] write file {target_path}")
    log(log_fp, "INFO", f"write file {target_path}")

    reader = inode.open_read()
    data = reader.read()  # For very large files you may want to stream in chunks
    with open(target_path, "wb") as f:
        f.write(data)

    # Optionally apply original mode bits (owner/group will still be the current user)
    try:
        mode = inode.inode.i_mode & 0o7777
        os.chmod(target_path, mode)
    except Exception as e:
        log(log_fp, "WARNING", f"chmod failed for {target_path}: {e}")


def write_symlink(target_path: str, inode, out_root: str, dry_run: bool, verbose: bool, log_fp):
    """
    Create a symlink at target_path. Target is read from the inode contents
    (ext4 symlinks store the path as bytes).
    """
    parent = os.path.dirname(target_path)
    ensure_dir(parent, dry_run, verbose, log_fp)

    # For logging, show the path relative to the output root directory so that logs
    # reflect the layout inside ROOTFS instead of host absolute paths.
    try:
        rel_log_path = os.path.relpath(target_path, out_root)
    except Exception:
        rel_log_path = target_path

    # Read symlink target
    reader = inode.open_read()
    target_bytes = reader.read()
    try:
        link_target = target_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # Fallback: best-effort byte-to-str mapping
        link_target = target_bytes.decode("utf-8", errors="replace")

    if dry_run:
        if verbose:
            print(f"[DRY-RUN] ln -s {link_target} {rel_log_path}")
        log(log_fp, "INFO", f"ln -s {link_target} -> {rel_log_path}")
        return

    if verbose:
        print(f"[INFO] ln -s {link_target} {rel_log_path}")
    log(log_fp, "INFO", f"ln -s {link_target} -> {rel_log_path}")

    # Remove existing file/symlink if present
    try:
        os.unlink(target_path)
    except FileNotFoundError:
        pass
    except OSError as e:
        # Only treat non-ENOENT as warning
        if e.errno != errno.ENOENT:
            log(log_fp, "WARNING", f"Failed to remove existing {target_path}: {e}")

    os.symlink(link_target, target_path)


def extract_directory(volume, inode, outdir: str, relpath: str, dry_run: bool, verbose: bool, log_fp):
    """
    Recursively extract a directory inode (and its contents) into outdir/relpath.
    `inode` is an ext4.Inode object corresponding to the directory.
    """
    # Create the directory itself
    dir_path = os.path.join(outdir, relpath)
    ensure_dir(dir_path, dry_run, verbose, log_fp)

    # Iterate directory entries
    try:
        entries = list(inode.open_dir())
    except Exception as e:
        msg = f"Failed to open_dir for {relpath or '/'}: {e}"
        print(f"[ERROR] {msg}", file=sys.stderr)
        log(log_fp, "ERROR", msg)
        return

    for name, child_idx, file_type in entries:
        # Skip "." and ".."
        if name in (".", ".."):
            continue

        child_rel = name if not relpath else f"{relpath}/{name}"

        try:
            child_inode = inode.get_inode(name)
        except Exception as e:
            msg = f"Failed to get_inode('{name}') under {relpath or '/'}: {e}"
            print(f"[WARNING] {msg}", file=sys.stderr)
            log(log_fp, "WARNING", msg)
            continue

        mode = child_inode.inode.i_mode
        is_dir = stat.S_ISDIR(mode)
        is_reg = stat.S_ISREG(mode)
        is_lnk = stat.S_ISLNK(mode)

        if is_dir:
            extract_directory(volume, child_inode, outdir, child_rel, dry_run, verbose, log_fp)
        elif is_reg:
            dst = os.path.join(outdir, child_rel)
            write_file(dst, child_inode, dry_run, verbose, log_fp)
        elif is_lnk:
            dst = os.path.join(outdir, child_rel)
            write_symlink(dst, child_inode, outdir, dry_run, verbose, log_fp)
        else:
            # Device nodes, sockets, fifos, etc. – log and skip
            msg = f"Skipping unsupported inode type for {child_rel}"
            if verbose:
                print(f"[WARNING] {msg}")
            log(log_fp, "WARNING", msg)


def main():
    args = parse_args()

    image_path = os.path.abspath(args.image)
    outdir = os.path.abspath(args.outdir)
    dry_run = args.dry_run
    verbose = args.verbose
    chmod_value = args.chmod
    log_fp = open_log(args.logfile_rootfs)

    if not os.path.isfile(image_path):
        print(f"Error: image not found: {image_path}", file=sys.stderr)
        sys.exit(1)

    log(log_fp, "INFO", f"IMAGE: {image_path}")
    log(log_fp, "INFO", f"OUTDIR: {outdir}")
    log(log_fp, "INFO", f"OFFSET: {args.offset}")
    log(log_fp, "INFO", f"DRY-RUN: {dry_run}")

    if verbose:
        print(f"[Convert RootFS (pure ext4)]")
        print(f"       IMAGE: {image_path}")
        print(f"       OUTPUT DIR: {outdir}")
        print(f"       OFFSET: {args.offset}")
        print(f"       DRY-RUN: {dry_run}")
        print(f"       Logfile: {args.logfile_rootfs}")

    if not dry_run:
        ensure_dir(outdir, dry_run=False, verbose=verbose, log_fp=log_fp)

    # Open ext4 volume
    try:
        f = open(image_path, "rb")
    except OSError as e:
        print(f"Error: failed to open image '{image_path}': {e}", file=sys.stderr)
        sys.exit(1)

    try:
        volume = ext4.Volume(f, offset=args.offset)
    except Exception as e:
        print(f"Error: failed to parse ext4 volume: {e}", file=sys.stderr)
        f.close()
        sys.exit(1)

    root_inode = volume.root  # ext4.Inode for "/"

    # Recursively extract from root
    extract_directory(volume, root_inode, outdir, relpath="", dry_run=dry_run, verbose=verbose, log_fp=log_fp)

    f.close()

    # Optional chmod after extraction
    if chmod_value and not dry_run:
        if verbose:
            print(f"[INFO] Applying chmod -R {chmod_value} to {outdir}")
        log(log_fp, "INFO", f"chmod -R {chmod_value} {outdir}")
        # Do not fail hard on chmod errors; just best-effort
        os.system(f"chmod -R {chmod_value} '{outdir}'")

    # Simple summary (1 success if we got here)
    print("[Convert RootFS (pure ext4)]")
    print(f"       IMAGE: {image_path}")
    print(f"       OUTPUT DIR: {outdir}")
    print(f"       Logfile: {args.logfile_rootfs}")
    print(f"       Success: {0 if dry_run else 1}")
    print(f"       ERRORS: 0")
    print(f"       WARNINGS: 0")
    print("[INFO] Done.")

    if log_fp is not None:
        log_fp.close()

    sys.exit(0)


if __name__ == "__main__":
    main()