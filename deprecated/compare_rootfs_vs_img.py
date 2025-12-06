#!/usr/bin/env python3
import argparse
import os
import sys
import stat
import subprocess
from typing import Dict, Tuple, Set


def parse_args():
    parser = argparse.ArgumentParser(
        description="Compare EXT4 image directory tree (via debugfs) with extracted ROOTFS directory, with special focus on symlinks."
    )
    parser.add_argument("image", help="Path to EXT4 image file")
    parser.add_argument(
        "--rootfs",
        required=True,
        help="Path to extracted ROOTFS directory",
    )
    parser.add_argument(
        "--logdir",
        default="./logs",
        help="Directory to store comparison reports",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose output",
    )
    return parser.parse_args()


def run_debugfs_ls(image_path: str) -> str:
    """
    Run debugfs to list the directory tree from '/' recursively.
    We use 'ls -l -p -r /' and parse the results.
    """
    cmd = ["debugfs", "-R", "ls -l -p -r /", image_path]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
        return out
    except subprocess.CalledProcessError as e:
        print("[ERROR] debugfs failed:", file=sys.stderr)
        print(e.output, file=sys.stderr)
        sys.exit(1)


def parse_debugfs_output(output: str) -> Dict[str, Dict[str, str]]:
    """
    Parse the output of 'debugfs -R \"ls -l -p -r /\" image'.
    Return a dict mapping:
        relative_path (stripped leading '/') -> {'type': 'd'|'f'|'l', 'target': target_str_or_empty}
    For symlinks, 'target' stores the raw link target string after '->'.
    """
    result: Dict[str, Dict[str, str]] = {}
    current_dir = "/"

    for line in output.splitlines():
        line = line.rstrip("\n")
        if not line:
            continue

        # Directory heading lines usually look like "/usr/bin:"
        if line.endswith(":") and line.startswith("/"):
            current_dir = line[:-1]  # drop trailing ':'
            if current_dir == "":
                current_dir = "/"
            continue

        # Entry lines usually start with a space and an inode number, e.g.
        # " 1234 (12) drwxr-xr-x ..."
        stripped = line.lstrip()
        if not stripped or not stripped[0].isdigit():
            # Not an entry line we recognize
            continue

        parts = stripped.split()
        if len(parts) < 6:
            # Too short to be a valid ls -l line
            continue

        # Example: inode (gen) mode nlink uid gid size date time name [-> target]
        # parts[2] is mode string like "drwxr-xr-x" or "-rwxr-xr-x" or "lrwxrwxrwx"
        mode_str = parts[2]
        type_char = mode_str[0] if mode_str else "?"

        # Name starts after date/time.
        # We do a rough parse: join everything after the 8th token as "name ...".
        # Format is typically: inode (gen) mode nlink uid gid size date time name...
        if len(parts) < 9:
            continue
        name_and_rest = " ".join(parts[8:])

        link_target = ""
        if " -> " in name_and_rest:
            name, link_target = name_and_rest.split(" -> ", 1)
        else:
            name = name_and_rest

        if name in (".", ".."):
            continue

        # Build full path
        if current_dir == "/":
            full_path = f"/{name}"
        else:
            full_path = f"{current_dir.rstrip('/')}/{name}"

        # Normalize and strip leading '/'
        norm = os.path.normpath(full_path)
        if norm.startswith("/"):
            norm = norm[1:]

        if not norm:
            norm = "."

        # Map debugfs type to simplified type_char
        if type_char == "d":
            t = "d"
        elif type_char == "l":
            t = "l"
        elif type_char == "-":
            t = "f"
        else:
            # treat others (c,b,s,p) as 'f'
            t = "f"

        result[norm] = {"type": t, "target": link_target}

    # Ensure root is present
    if "." not in result:
        result["."] = {"type": "d", "target": ""}
    return result


def scan_rootfs(rootfs: str) -> Dict[str, Dict[str, str]]:
    """
    Walk the ROOTFS directory and return a dict mapping:
        relative_path (from rootfs) -> {'type': 'd'|'f'|'l', 'target': target_str_or_empty}
    For symlinks, 'target' stores the raw link target string from os.readlink().
    """
    result: Dict[str, Dict[str, str]] = {}
    rootfs = os.path.abspath(rootfs)

    for dirpath, dirnames, filenames in os.walk(rootfs, followlinks=False):
        rel_dir = os.path.relpath(dirpath, rootfs)
        if rel_dir == ".":
            rel_dir = ""
        # Directory itself
        if rel_dir and rel_dir not in result:
            result[rel_dir] = {"type": "d", "target": ""}

        # Files and symlinks
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, rootfs)
            try:
                st = os.lstat(full)
            except OSError:
                continue

            if stat.S_ISDIR(st.st_mode):
                t = "d"
                target = ""
            elif stat.S_ISLNK(st.st_mode):
                t = "l"
                try:
                    target = os.readlink(full)
                except OSError:
                    target = ""
            elif stat.S_ISREG(st.st_mode):
                t = "f"
                target = ""
            else:
                t = "f"
                target = ""

            result[rel] = {"type": t, "target": target}

    # Ensure root is present
    if "." not in result and "" not in result:
        result["."] = {"type": "d", "target": ""}

    return result


def write_list(path: str, items: Set[str]):
    with open(path, "w", encoding="utf-8") as f:
        for x in sorted(items):
            f.write(x + "\n")


def write_mismatch(path: str, items: Set[str], img_types: Dict[str, Dict[str, str]], fs_types: Dict[str, Dict[str, str]]):
    with open(path, "w", encoding="utf-8") as f:
        for x in sorted(items):
            img_t = img_types.get(x, {}).get("type", "?")
            fs_t = fs_types.get(x, {}).get("type", "?")
            f.write(f"{x}\timage:{img_t}\trootfs:{fs_t}\n")


def write_symlink_target_mismatch(path: str, items: Set[str], img_map: Dict[str, Dict[str, str]], fs_map: Dict[str, Dict[str, str]]):
    with open(path, "w", encoding="utf-8") as f:
        for x in sorted(items):
            img_tgt = img_map.get(x, {}).get("target", "")
            fs_tgt = fs_map.get(x, {}).get("target", "")
            f.write(f"{x}\timage_target:{img_tgt}\trootfs_target:{fs_tgt}\n")


def main():
    args = parse_args()

    image_path = os.path.abspath(args.image)
    rootfs_path = os.path.abspath(args.rootfs)
    logdir = os.path.abspath(args.logdir)

    if not os.path.isfile(image_path):
        print(f"[ERROR] Image not found: {image_path}", file=sys.stderr)
        sys.exit(1)
    if not os.path.isdir(rootfs_path):
        print(f"[ERROR] ROOTFS not found or not a directory: {rootfs_path}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(logdir, exist_ok=True)

    if args.verbose:
        print(f"[INFO] Image: {image_path}")
        print(f"[INFO] ROOTFS: {rootfs_path}")
        print("[INFO] Collecting tree from debugfs...")

    dbg_out = run_debugfs_ls(image_path)
    img_tree = parse_debugfs_output(dbg_out)

    if args.verbose:
        print(f"[INFO] debugfs entries: {len(img_tree)}")
        print("[INFO] Scanning ROOTFS...")

    fs_tree = scan_rootfs(rootfs_path)

    if args.verbose:
        print(f"[INFO] ROOTFS entries: {len(fs_tree)}")

    img_paths = set(img_tree.keys())
    fs_paths = set(fs_tree.keys())

    only_in_image = img_paths - fs_paths
    only_in_rootfs = fs_paths - img_paths
    common_paths = img_paths & fs_paths

    type_mismatch = {p for p in common_paths if img_tree.get(p, {}).get("type") != fs_tree.get(p, {}).get("type")}

    # Symlink-specific sets
    img_symlinks = {p for p, meta in img_tree.items() if meta.get("type") == "l"}
    fs_symlinks = {p for p, meta in fs_tree.items() if meta.get("type") == "l"}

    symlink_only_in_image = img_symlinks - fs_symlinks
    symlink_only_in_rootfs = fs_symlinks - img_symlinks
    common_symlinks = img_symlinks & fs_symlinks

    symlink_target_mismatch = {
        p for p in common_symlinks
        if img_tree.get(p, {}).get("target", "") != fs_tree.get(p, {}).get("target", "")
    }

    # Write reports
    write_list(os.path.join(logdir, "compare_only_in_image.txt"), only_in_image)
    write_list(os.path.join(logdir, "compare_only_in_rootfs.txt"), only_in_rootfs)
    write_mismatch(
        os.path.join(logdir, "compare_type_mismatch.txt"),
        type_mismatch,
        img_types=img_tree,
        fs_types=fs_tree,
    )
    write_list(os.path.join(logdir, "compare_symlink_only_in_image.txt"), symlink_only_in_image)
    write_list(os.path.join(logdir, "compare_symlink_only_in_rootfs.txt"), symlink_only_in_rootfs)
    write_symlink_target_mismatch(
        os.path.join(logdir, "compare_symlink_target_mismatch.txt"),
        symlink_target_mismatch,
        img_map=img_tree,
        fs_map=fs_tree,
    )

    # Summary
    print("[Compare RootFS vs Image]")
    print(f"       IMAGE: {image_path}")
    print(f"       ROOTFS: {rootfs_path}")
    print(f"       LOGDIR: {logdir}")
    print(f"       Entries (image):           {len(img_tree)}")
    print(f"       Entries (rootfs):          {len(fs_tree)}")
    print(f"       Only in image:             {len(only_in_image)}")
    print(f"       Only in rootfs:            {len(only_in_rootfs)}")
    print(f"       Type mismatch:             {len(type_mismatch)}")
    print(f"       Symlinks only in image:    {len(symlink_only_in_image)}")
    print(f"       Symlinks only in rootfs:   {len(symlink_only_in_rootfs)}")
    print(f"       Symlink target mismatch:   {len(symlink_target_mismatch)}")
    print("[INFO] Reports:")
    print(f"       {os.path.join(logdir, 'compare_only_in_image.txt')}")
    print(f"       {os.path.join(logdir, 'compare_only_in_rootfs.txt')}")
    print(f"       {os.path.join(logdir, 'compare_type_mismatch.txt')}")
    print(f"       {os.path.join(logdir, 'compare_symlink_only_in_image.txt')}")
    print(f"       {os.path.join(logdir, 'compare_symlink_only_in_rootfs.txt')}")
    print(f"       {os.path.join(logdir, 'compare_symlink_target_mismatch.txt')}")

    sys.exit(0)


if __name__ == "__main__":
    main()
