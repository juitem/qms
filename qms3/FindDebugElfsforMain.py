#!/usr/bin/env python3
"""
FindDebugElfForMain.py

Given one or more *main* ELF file paths inside a rootfs, try to find the best
matching *debug* ELF for each main ELF.

Search strategy (per main ELF):

  1) Resolve realpath of the main ELF inside the rootfs.
  2) Confirm it is an ELF and extract its GNU build-id.
  3) Generate name-based debug candidates and check existence:
       - Same-dir: <dir>/<base>.debug
       - Overlay : /usr/lib/debug<real_rel>.debug
       - Overlay2: /usr/lib/debug<real_rel_without_/usr_prefix>.debug
       - Tizen   : /usr/lib/debug/lib64.tizen-debug/<base>.debug
  4) Generate build-id-based debug candidates under debug-roots:
       - <debug_root>/aa/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.debug
         (if symlink, resolve target to get the actual debug ELF)
  5) Collect all existing candidates and pick the best:
       - Priority: buildid > overlay > same-dir
       - Within the same priority, choose the largest file size.

This script does NOT create or modify any links or files. It only reads.
It is intended for cross/sysroot environments (no chroot required).

It can be used as:
  - CLI tool (see --help)
  - Library function (see resolve_debug_for_main_many())
"""

import argparse
import os
import sys
import subprocess
from typing import List, Dict, Optional, Tuple

# Optional pyelftools import
try:
    from elftools.elf.elffile import ELFFile  # type: ignore
    HAS_PYELFTOOLS = True
except ImportError:
    HAS_PYELFTOOLS = False


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


def get_build_id_with_readelf(path: str) -> Optional[str]:
    """Extract GNU build-id using external `readelf -n`."""
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


def get_build_id_with_pyelftools(path: str) -> Optional[str]:
    """Extract GNU build-id using pyelftools (.note.gnu.build-id section)."""
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

    namesz = int.from_bytes(data[0:4], "little")
    descsz = int.from_bytes(data[4:8], "little")
    _ntype = int.from_bytes(data[8:12], "little")

    name_off = 12
    name_end = name_off + namesz
    desc_off = (name_end + 3) & ~3
    if desc_off + descsz > len(data):
        return None

    buildid_bytes = data[desc_off : desc_off + descsz]
    if not buildid_bytes:
        return None

    return buildid_bytes.hex()


def get_build_id(path: str, backend: str) -> Optional[str]:
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
                "ERROR: --buildid-backend=pyelf requested but pyelftools not installed.\n"
            )
            return None
        return get_build_id_with_pyelftools(path)

    if backend == "readelf":
        return get_build_id_with_readelf(path)

    # auto
    if HAS_PYELFTOOLS:
        val = get_build_id_with_pyelftools(path)
        if val:
            return val
    return get_build_id_with_readelf(path)


def to_rootfs_rel(rootfs: str, abs_path: str) -> str:
    """Convert absolute path under rootfs to rootfs-relative path starting with '/'."""
    rel = os.path.relpath(abs_path, rootfs)
    rel = rel.replace(os.sep, "/")
    if not rel.startswith("/"):
        rel = "/" + rel
    return rel


def normalize_inside_rootfs(rootfs_real: str, path: str) -> str:
    """
    Interpret 'path' as inside rootfs_real:

      - If 'path' is absolute (/usr/lib/...), treat it as relative to rootfs.
      - If 'path' is relative, join it directly to rootfs_real.
    """
    if os.path.isabs(path):
        rel = path.lstrip(os.sep)
        return os.path.join(rootfs_real, rel)
    else:
        return os.path.join(rootfs_real, path)


def load_file_list(path: str) -> List[str]:
    """Load file paths from a text file, ignoring empty and comment lines."""
    files: List[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            files.append(line)
    return files


# ------------------------------------------------------------
# Candidate generation
# ------------------------------------------------------------
def generate_name_based_candidates(real_rel: str) -> List[Dict]:
    """
    Generate name-based debug candidates from a main ELF real_rel path.

    real_rel: rootfs-relative path starting with '/':
      e.g. /usr/lib64/libfoo.so.1.2.3
           /usr/apps/org.tizen.app/bin/app21
           /opt/usr/apps/org.tizen.app/bin/app21
    """
    candidates: List[Dict] = []

    dir_part, base = os.path.split(real_rel)

    # 1) Same directory + ".debug"
    same_dir = f"{dir_part}/{base}.debug"
    candidates.append(
        {
            "kind": "same-dir",
            "rel": same_dir,
            "priority": 1,  # lowest
        }
    )

    # 2) Standard overlay: /usr/lib/debug<real_rel>.debug
    overlay1 = f"/usr/lib/debug{real_rel}.debug"
    candidates.append(
        {
            "kind": "overlay",
            "rel": overlay1,
            "priority": 2,
        }
    )

    # 3) Overlay with /usr prefix removed: /usr/lib/debug<real_rel_without_/usr>.debug
    if real_rel.startswith("/usr/"):
        tail = real_rel[len("/usr") :]  # e.g. "/lib64/libfoo.so.1.2.3"
        overlay2 = f"/usr/lib/debug{tail}.debug"
        candidates.append(
            {
                "kind": "overlay-no-usr",
                "rel": overlay2,
                "priority": 2,
            }
        )

    # 4) Tizen-specific: /usr/lib/debug/lib64.tizen-debug/<base>.debug
    #    Only for libs under /usr/lib64 or /lib64.
    if real_rel.startswith("/usr/lib64/") or real_rel.startswith("/lib64/"):
        tizen_debug = f"/usr/lib/debug/lib64.tizen-debug/{base}.debug"
        candidates.append(
            {
                "kind": "tizen-lib64",
                "rel": tizen_debug,
                "priority": 2,
            }
        )

    return candidates


def generate_buildid_candidates(
    build_id: str, debug_roots: List[str]
) -> List[Dict]:
    """
    Generate build-id-based debug candidates:

      <debug_root>/aa/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.debug
    """
    if len(build_id) < 3:
        prefix = build_id[:2]
        rest = build_id[2:]
    else:
        prefix = build_id[:2]
        rest = build_id[2:]

    candidates: List[Dict] = []
    for root in debug_roots:
        dir_path = os.path.join(root, prefix)
        path = os.path.join(dir_path, rest + ".debug")
        candidates.append(
            {
                "kind": "buildid",
                "abs": path,
                "priority": 3,  # highest
            }
        )
    return candidates


def resolve_buildid_candidate(
    rootfs_real: str, cand: Dict
) -> Optional[Dict]:
    """
    Given a build-id candidate (abs path), check if it exists and resolve it:

    - If it does not exist => return None.
    - If it is a symlink => resolve target relative to link dir or rootfs (for absolute).
    - If it is a regular file => treat it as a debug ELF directly.
    """
    path = cand["abs"]
    if not os.path.lexists(path):
        return None

    # If it is a symlink, resolve one step
    if os.path.islink(path):
        try:
            target = os.readlink(path)
        except OSError as e:
            sys.stderr.write(f"WARNING: failed to readlink {path}: {e}\n")
            return None

        if os.path.isabs(target):
            # absolute, interpret inside rootfs
            debug_abs = os.path.join(rootfs_real, target.lstrip(os.sep))
        else:
            # relative to directory containing the link
            debug_abs = os.path.normpath(os.path.join(os.path.dirname(path), target))
    else:
        # regular file - use as-is
        debug_abs = path

    if not os.path.exists(debug_abs):
        return None

    debug_abs = os.path.realpath(debug_abs)
    debug_rel = to_rootfs_rel(rootfs_real, debug_abs)

    return {
        "kind": "buildid",
        "abs": debug_abs,
        "rel": debug_rel,
        "priority": 3,
        "via": "buildid",
    }


# ------------------------------------------------------------
# Core per-file logic
# ------------------------------------------------------------
def find_debug_for_main(
    rootfs_real: str,
    main_path: str,
    debug_roots: List[str],
    backend: str,
) -> Dict:
    """
    Find debug ELF candidates for a given main ELF path (inside rootfs).

    main_path: path inside rootfs (absolute /usr/..., or relative)

    Returns a dict:

      {
        "main_abs": ...,
        "main_rel": ...,
        "is_elf": bool,
        "build_id": str or None,
        "candidates": [ {abs, rel, kind, priority, via, size}, ... ],
        "best": {abs, rel, kind, priority, via, size} or None,
      }
    """
    # Normalize main_path inside rootfs
    if os.path.isabs(main_path):
        rel = main_path.lstrip(os.sep)
        main_abs = os.path.join(rootfs_real, rel)
    else:
        main_abs = os.path.join(rootfs_real, main_path)

    result: Dict = {
        "main_abs": main_abs,
        "main_rel": "",
        "is_elf": False,
        "build_id": None,
        "candidates": [],
        "best": None,
    }

    if not os.path.exists(main_abs):
        return result

    main_abs = os.path.realpath(main_abs)
    main_rel = to_rootfs_rel(rootfs_real, main_abs)
    result["main_abs"] = main_abs
    result["main_rel"] = main_rel

    if not is_elf(main_abs):
        return result

    result["is_elf"] = True

    build_id = get_build_id(main_abs, backend)
    if not build_id:
        return result

    result["build_id"] = build_id

    candidates: List[Dict] = []

    # 1) Name-based candidates
    name_cands = generate_name_based_candidates(main_rel)
    for nc in name_cands:
        debug_rel = nc["rel"]
        debug_abs = normalize_inside_rootfs(rootfs_real, debug_rel)
        if os.path.exists(debug_abs):
            size = -1
            try:
                size = os.path.getsize(debug_abs)
            except OSError:
                size = -1
            real_abs = os.path.realpath(debug_abs)
            candidates.append(
                {
                    "kind": nc["kind"],
                    "priority": nc["priority"],
                    "abs": real_abs,
                    "rel": to_rootfs_rel(rootfs_real, real_abs),
                    "via": nc["kind"],
                    "size": size,
                }
            )

    # 2) Build-id-based candidates
    debug_roots_abs = [normalize_inside_rootfs(rootfs_real, r) for r in debug_roots]
    bid_cands = generate_buildid_candidates(build_id, debug_roots_abs)
    for bc in bid_cands:
        resolved = resolve_buildid_candidate(rootfs_real, bc)
        if resolved is None:
            continue
        debug_abs = resolved["abs"]
        size = -1
        try:
            size = os.path.getsize(debug_abs)
        except OSError:
            size = -1
        real_abs = os.path.realpath(debug_abs)
        candidates.append(
            {
                "kind": "buildid",
                "priority": resolved["priority"],
                "abs": real_abs,
                "rel": to_rootfs_rel(rootfs_real, real_abs),
                "via": resolved["via"],
                "size": size,
            }
        )

    # Deduplicate by real_abs
    unique: Dict[str, Dict] = {}
    for c in candidates:
        real_abs = os.path.realpath(c["abs"])
        if real_abs not in unique:
            unique[real_abs] = c
        else:
            old = unique[real_abs]
            if (c["priority"] > old["priority"]) or (
                c["priority"] == old["priority"] and c["size"] > old["size"]
            ):
                unique[real_abs] = c

    final_candidates = list(unique.values())

    # Pick best candidate
    best: Optional[Dict] = None
    for c in final_candidates:
        if best is None:
            best = c
            continue
        if c["priority"] > best["priority"]:
            best = c
        elif c["priority"] == best["priority"]:
            if c["size"] > best["size"]:
                best = c

    result["candidates"] = final_candidates
    result["best"] = best
    return result


# ------------------------------------------------------------
# Library helper for other Python code
# ------------------------------------------------------------
def resolve_debug_for_main_many(
    rootfs: str,
    main_paths: List[str],
    debug_roots: Optional[List[str]] = None,
    backend: str = "auto",
) -> List[Dict]:
    """
    Convenience API for library use.

    rootfs      : rootfs directory on host (same as --rootfs)
    main_paths  : list of main ELF paths *inside* rootfs (absolute or relative)
    debug_roots : list of debug .build-id roots inside rootfs
                  (if None, use the same 3 defaults as CLI)
    backend     : 'auto', 'pyelf', or 'readelf'

    Returns a list of result dicts, each in the same format as find_debug_for_main().
    """
    rootfs_real = os.path.realpath(os.path.abspath(rootfs))

    if debug_roots is None:
        debug_roots = [
            "/usr/lib/debug/.build-id",
            "/lib/debug/.build-id",
            "/opt/usr/lib/debug/.build-id",
        ]

    results: List[Dict] = []
    for p in main_paths:
        info = find_debug_for_main(
            rootfs_real=rootfs_real,
            main_path=p,
            debug_roots=debug_roots,
            backend=backend,
        )
        results.append(info)
    return results


# ------------------------------------------------------------
# CLI
# ------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Find debug ELF for given main ELF files inside a rootfs.\n"
            "For each main ELF, this tool searches name-based and build-id-based\n"
            "debug candidates and picks the best one."
        )
    )
    parser.add_argument(
        "--rootfs",
        required=True,
        help="Rootfs directory (host path), e.g. ./download/img/ROOTFS",
    )
    parser.add_argument(
        "--file",
        action="append",
        dest="files",
        help="Main ELF path inside rootfs (can be specified multiple times).",
    )
    parser.add_argument(
        "--file-list",
        help=(
            "Text file containing one main ELF path per line (inside rootfs). "
            "Empty lines and lines starting with '#' are ignored."
        ),
    )
    parser.add_argument(
        "--debug-root",
        action="append",
        dest="debug_roots",
        help=(
            "Debug .build-id root to search under. Can be specified multiple times.\n"
            "If absolute (starts with '/'), it is interpreted as inside rootfs.\n"
            "If relative, it is joined to rootfs.\n"
            "Default: three roots: /usr/lib/debug/.build-id, "
            "/lib/debug/.build-id, /opt/usr/lib/debug/.build-id"
        ),
    )
    parser.add_argument(
        "--buildid-backend",
        choices=["auto", "pyelf", "readelf"],
        default="auto",
        help=(
            "Backend to extract GNU build-id: "
            "'pyelf' (pyelftools), 'readelf' (external tool), or 'auto'."
        ),
    )
    parser.add_argument(
        "--tsv",
        action="store_true",
        help=(
            "Output TSV instead of human-readable text.\n"
            "Columns: main_rel, build_id, debug_rel, via, size"
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    rootfs = os.path.abspath(args.rootfs)
    if not os.path.isdir(rootfs):
        sys.stderr.write(f"ERROR: rootfs directory not found: {rootfs}\n")
        sys.exit(1)

    rootfs_real = os.path.realpath(rootfs)

    # Collect main files
    files: List[str] = []
    if args.files:
        files.extend(args.files)
    if args.file_list:
        if not os.path.exists(args.file_list):
            sys.stderr.write(f"ERROR: file-list not found: {args.file_list}\n")
            sys.exit(1)
        files.extend(load_file_list(args.file_list))

    if not files:
        sys.stderr.write("ERROR: no input files. Use --file or --file-list.\n")
        sys.exit(1)

    # Default debug roots
    if args.debug_roots:
        debug_roots = args.debug_roots
    else:
        # Tizen-friendly defaults; can be overridden by --debug-root
        debug_roots = [
            "/usr/lib/debug/.build-id",
            "/lib/debug/.build-id",
            "/opt/usr/lib/debug/.build-id",
        ]

    if args.tsv:
        # TSV header
        print("main_rel\tbuild_id\tdebug_rel\tvia\tsize")
    else:
        print(f"rootfs      : {rootfs_real}")
        print(f"debug_roots : {', '.join(debug_roots)}")
        print()

    for fpath in files:
        info = find_debug_for_main(
            rootfs_real=rootfs_real,
            main_path=fpath,
            debug_roots=debug_roots,
            backend=args.buildid_backend,
        )

        main_rel = info.get("main_rel", "") or fpath
        main_abs = info.get("main_abs", "")
        is_elf = info.get("is_elf", False)
        build_id = info.get("build_id", None)
        best = info.get("best", None)

        if args.tsv:
            if not is_elf or not build_id or best is None:
                print(f"{main_rel}\t{build_id or '-'}\t-\t-\t-")
            else:
                print(
                    f"{main_rel}\t{build_id}\t"
                    f"{best['rel']}\t{best['via']}\t{best['size']}"
                )
        else:
            print("------------------------------------------------------------")
            print(f"MAIN   : {main_rel}")
            print(f"  host : {main_abs}")
            print(f"  ELF  : {is_elf}")
            print(f"  BID  : {build_id or '(none)'}")

            if not is_elf:
                print("  NOTE : not an ELF (skip debug search).")
                continue
            if not build_id:
                print("  NOTE : no build-id (skip debug search).")
                continue

            cands: List[Dict] = info.get("candidates", [])
            if not cands:
                print("  debug candidates : (none)")
            else:
                print("  debug candidates :")
                for c in cands:
                    print(
                        f"    - {c['rel']}  "
                        f"(via={c['via']}, kind={c['kind']}, "
                        f"priority={c['priority']}, size={c['size']})"
                    )

            if best is None:
                print("  BEST  : (none)")
            else:
                print("  BEST  :")
                print(
                    f"    {best['rel']}  "
                    f"(via={best['via']}, kind={best['kind']}, size={best['size']})"
                )

    if not args.tsv:
        print("------------------------------------------------------------")


if __name__ == "__main__":
    main()