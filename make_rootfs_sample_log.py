#!/usr/bin/env python3
"""make_rootfs_sample_log.py

Generate synthetic stack logs (sample.log) using ELF and debug files
inside a given rootfs, without any existing log as input.

Goals
-----
We try to cover several typical combinations in one file:

  * main ELF only (no separate debug file)
  * debug file only (no main ELF)
  * both main + debug existing
  * both missing (path + BuildId that do not exist in rootfs)
  * ELF under lib-like paths (e.g. /usr/lib, /usr/lib64, /lib, /lib64)
  * ELF under app-like paths (e.g. /usr/apps, /opt/apps, /usr/bin, /bin)
  * multiple independent stacks in a single file
    - each stack starts from "#0" again

NOTE
----
We only generate *input* stack lines, e.g.:

    #0 0x1ffff0000010 (/usr/lib/libfoo.so+0x10) (BuildId: abcdef1234...)

We do NOT generate addr2line output ourselves; any combination of
function / inline / file:line resolution will depend on your real
ELF + debug tree and your symbolization tool.

Usage example:

    python make_rootfs_sample_log.py \
        --rootfs ./download/img/ROOTFS \
        --output ./sample.log \
        --elf-count 4 \
        --frames-per-elf 2 \
        --stacks 3
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


def is_elf(path: Path) -> bool:
    """Check whether a file looks like an ELF by magic bytes.

    We do not rely on the 'file' command here; we just read the first 4 bytes.
    """
    try:
        with path.open("rb") as f:
            magic = f.read(4)
        return magic == b"\x7fELF"
    except Exception:
        return False


def find_elf_files(rootfs: Path, limit: int) -> List[Path]:
    """Walk the rootfs and find up to 'limit' ELF files."""
    elfs: List[Path] = []
    for dirpath, _, filenames in os.walk(rootfs):
        for fname in filenames:
            full = Path(dirpath) / fname
            # Skip obvious non-regular files quickly
            if not full.is_file():
                continue
            if is_elf(full):
                elfs.append(full)
                if len(elfs) >= limit:
                    return elfs
    return elfs


def read_build_id(path: Path) -> Optional[str]:
    """Try to read Build ID from the ELF (or debug) using 'readelf -n'.

    Returns:
        The hex build-id string (without spaces) or None if not found.
    """
    try:
        proc = subprocess.run(
            ["readelf", "-n", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None

    if proc.returncode != 0:
        return None

    for line in proc.stdout.splitlines():
        line = line.strip()
        # Typical pattern:
        #   "Build ID: 5b1278fbf110a30f2388d24871e73d215cfe06f2"
        if "Build ID:" in line:
            parts = line.split("Build ID:", 1)[1].strip()
            if parts:
                return parts
    return None


def classify_kind(rel: Path) -> str:
    """Classify an ELF path as 'lib', 'app', or 'other' based on its relative path."""
    parts = rel.parts
    # Very rough heuristics, good enough for synthetic samples
    if any(p in {"lib", "lib64"} for p in parts):
        return "lib"
    if "app" in parts or "apps" in parts or "bin" in parts or "opt" in parts:
        return "app"
    return "other"


@dataclass
class StackSource:
    """Represents one logical ELF/debug combination to generate frames from."""

    case: str
    log_path: str            # path that will appear in the stack log (e.g. '/usr/lib/libfoo.so')
    build_id: Optional[str]  # BuildId string, if we could read one (may be None)
    kind: str                # 'lib', 'app', or 'other'
    target_path: Optional[Path]  # Real ELF/debug file on disk to get symbol addresses from


# Helper to check addr2line output for func and file:line
def addr2line_has_func_and_src(path: Path, addr: int) -> bool:
    """Check if addr2line resolves both function name and file:line for the given address.

    We call: addr2line -f -C -e <path> 0xADDR

    Returns True only if:
      - function name is not "??"
      - file:line is not starting with "??"
    """
    try:
        proc = subprocess.run(
            ["addr2line", "-f", "-C", "-e", str(path), f"0x{addr:x}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        # addr2line not available
        return False

    if proc.returncode != 0:
        return False

    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    if len(lines) < 2:
        return False

    func = lines[0]
    src = lines[1]

    if func == "??":
        return False
    if src.startswith("??"):
        return False
    return True


def find_function_offsets(path: Path, limit: int, require_verified: bool = False) -> List[int]:
    """Pick up to 'limit' function addresses from the ELF's symbol table.

    These addresses are used as offsets in '...+0xOFFSET' so that
    addr2line(1) is very likely to resolve them to a real function
    and file:line when your quick_symbolizer runs.

    If require_verified is True, this function returns only addresses
    that we have already checked with addr2line_has_func_and_src() and
    confirmed that both function name and file:line are available.
    In that case, if no such addresses are found, an empty list is
    returned (no fallback).
    """
    try:
        # -sW: show symbol table, wide output (better for long names)
        proc = subprocess.run(
            ["readelf", "-sW", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []

    if proc.returncode != 0:
        return []

    offsets: List[int] = []

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("Symbol table"):
            continue
        if line.startswith("Num:"):
            continue

        parts = line.split()
        # Expected general shape:
        #   Num:    Value          Size Type    Bind   Vis      Ndx Name
        #   23: 0000000000001140  186 FUNC    GLOBAL DEFAULT   14 myfunc
        if len(parts) < 8:
            continue

        sym_type = parts[3]
        if sym_type != "FUNC":
            continue

        try:
            value = int(parts[1], 16)
        except ValueError:
            continue

        if value == 0:
            continue

        offsets.append(value)
        # Collect a bit more than we strictly need so we can filter by addr2line result
        if len(offsets) >= max(limit * 4, limit):
            break

    if not offsets:
        return []

    # First, try to keep only those addresses where addr2line returns both
    # function name and file:line (not "??" / "??:0").
    good: List[int] = []
    for value in offsets:
        if addr2line_has_func_and_src(path, value):
            good.append(value)
            if len(good) >= limit:
                break

    if good:
        return good[:limit]

    # If require_verified=True, do not fall back to unverified offsets.
    if require_verified:
        return []

    # Fallback: if we couldn't confirm any "good" addresses, just return the
    # first few candidates.
    return offsets[:limit]


def collect_main_debug_cases(rootfs: Path, elf_paths: List[Path]) -> List[StackSource]:
    """Collect representative cases from real ELF/debug files in rootfs.

    We try to find:
      - main+debug (lib/app/other)
      - main-only (lib/app/other)
      - debug-only (from /usr/lib/debug tree)
      The 'both-missing' case is synthetic and added separately.
    """
    sources: List[StackSource] = []

    # First, analyze the given ELF paths
    debug_root = rootfs / "usr" / "lib" / "debug"

    main_plus_debug: List[StackSource] = []
    main_only: List[StackSource] = []

    for elf_path in elf_paths:
        rel = elf_path.relative_to(rootfs)
        kind = classify_kind(rel)
        log_path = "/" + str(rel)

        # Check "path-style" debug file, i.e. /usr/lib/debug/<REL>.debug
        dbg_path = debug_root / rel
        if dbg_path.suffix:
            dbg_path = dbg_path.with_suffix(dbg_path.suffix + ".debug")
        else:
            dbg_path = dbg_path.with_suffix(".debug")

        debug_exists = dbg_path.is_file()

        # Check build-id style debug file
        build_id = read_build_id(elf_path)
        if build_id:
            prefix = build_id[:2]
            rest = build_id[2:]
            buildid_dbg = debug_root / ".build-id" / prefix / f"{rest}.debug"
            if buildid_dbg.is_file():
                debug_exists = True

        if debug_exists:
            main_plus_debug.append(
                StackSource(
                    case=f"main+debug({kind})",
                    log_path=log_path,
                    build_id=build_id,
                    kind=kind,
                    target_path=elf_path,
                )
            )
        else:
            main_only.append(
                StackSource(
                    case=f"main-only({kind})",
                    log_path=log_path,
                    build_id=build_id,
                    kind=kind,
                    target_path=elf_path,
                )
            )

    # Prefer to keep one or two samples per kind if available
    def pick_by_kind(candidates: List[StackSource], kind: str, limit: int = 1) -> List[StackSource]:
        picked: List[StackSource] = []
        for src in candidates:
            if src.kind == kind:
                picked.append(src)
                if len(picked) >= limit:
                    break
        return picked

    # main+debug lib/app samples
    before_len = len(sources)
    sources.extend(pick_by_kind(main_plus_debug, "lib"))
    sources.extend(pick_by_kind(main_plus_debug, "app"))
    if len(sources) == before_len and main_plus_debug:
        # Fallback: at least one sample if nothing was picked
        sources.append(main_plus_debug[0])

    # main-only lib/app samples
    before_len = len(sources)
    sources.extend(pick_by_kind(main_only, "lib"))
    sources.extend(pick_by_kind(main_only, "app"))
    if len(sources) == before_len and main_only:
        sources.append(main_only[0])

    # Now try to find debug-only cases from /usr/lib/debug
    if debug_root.is_dir():
        debug_only_added = 0
        for dirpath, _, filenames in os.walk(debug_root):
            for fname in filenames:
                if not fname.endswith(".debug"):
                    continue
                dbg_path = Path(dirpath) / fname
                rel_dbg = dbg_path.relative_to(rootfs)

                # We expect something like: usr/lib/debug/<MAIN_REL>.debug
                rel_dbg_str = str(rel_dbg)
                prefix = "usr/lib/debug/"
                if not rel_dbg_str.startswith(prefix):
                    continue
                main_rel_str = rel_dbg_str[len(prefix):]
                if not main_rel_str.endswith(".debug"):
                    continue
                main_rel_str = main_rel_str[: -len(".debug")]
                main_path = rootfs / main_rel_str
                if main_path.is_file():
                    # This is not "debug-only", main also exists
                    continue

                log_path = "/" + main_rel_str
                build_id = read_build_id(dbg_path)

                kind = classify_kind(Path(main_rel_str))
                sources.append(
                    StackSource(
                        case=f"debug-only({kind})",
                        log_path=log_path,
                        build_id=build_id,
                        kind=kind,
                        target_path=dbg_path,
                    )
                )
                debug_only_added += 1
                if debug_only_added >= 2:
                    break
            if debug_only_added >= 2:
                break

    return sources


def add_missing_both_case(sources: List[StackSource]) -> None:
    """Append a synthetic 'both-missing' case (no main, no debug in rootfs)."""
    # Use a clearly fake path + BuildId, so that nothing matches in rootfs.
    fake_path = "/usr/lib/libquick_symbolizer_missing.so"
    fake_build_id = "deadbeef00000000000000000000000000000000"
    sources.append(
        StackSource(
            case="missing-both(lib)",
            log_path=fake_path,
            build_id=fake_build_id,
            kind="lib",
            target_path=None,
        )
    )


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Generate synthetic stack logs from ELF/debug files in a rootfs."
    )
    p.add_argument(
        "--rootfs",
        required=True,
        help="Path to rootfs (e.g., ./download/img/ROOTFS).",
    )
    p.add_argument(
        "--output",
        required=True,
        help="Path to write the generated sample log (e.g., ./sample.log).",
    )
    p.add_argument(
        "--elf-count",
        type=int,
        default=8,
        help=(
            "Maximum number of ELF files to scan from rootfs "
            "to construct main/debug cases (default: 8)."
        ),
    )
    p.add_argument(
        "--frames-per-elf",
        type=int,
        default=2,
        help="Number of stack frames to generate per case (default: 2).",
    )
    p.add_argument(
        "--stacks",
        type=int,
        default=1,
        help=(
            "Number of independent stack traces to generate in one file. "
            "Each stack starts from '#0' again (default: 1)."
        ),
    )
    p.add_argument(
        "--log-count",
        type=int,
        default=1,
        help=(
            "Number of separate log files to generate. If greater than 1, "
            "--output is used as a base name: 'sample.log' -> "
            "'sample_0.log', 'sample_1.log', ... (default: 1)."
        ),
    )
    return p


def main() -> None:
    args = build_argparser().parse_args()

    rootfs = Path(args.rootfs).resolve()
    out_path = Path(args.output)
    elf_count = max(1, args.elf_count)
    frames_per_case = max(1, args.frames_per_elf)
    stack_count = max(1, args.stacks)
    log_count = max(1, args.log_count)

    if not rootfs.is_dir():
        raise SystemExit(f"rootfs directory not found: {rootfs}")

    elfs = find_elf_files(rootfs, limit=elf_count)
    if not elfs:
        raise SystemExit(f"No ELF files found under rootfs: {rootfs}")

    # Collect various kinds of cases (main-only, main+debug, debug-only, etc.)
    sources = collect_main_debug_cases(rootfs, elfs)
    if not sources:
        # Fallback: just use the found ELFs as simple main-only cases
        sources = []
        for elf_path in elfs:
            rel = elf_path.relative_to(rootfs)
            log_path = "/" + str(rel)
            build_id = read_build_id(elf_path)
            kind = classify_kind(rel)
            sources.append(
                StackSource(
                    case=f"simple({kind})",
                    log_path=log_path,
                    build_id=build_id,
                    kind=kind,
                    target_path=elf_path,
                )
            )

    # Always add one synthetic "both-missing" case at the end
    add_missing_both_case(sources)

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Precompute verified offsets per StackSource so that at least one
    # case in the generated logs is guaranteed to have function name
    # and file:line resolvable by addr2line.
    precomputed_offsets_per_src: List[List[int]] = []
    has_verified_set = False

    for src in sources:
        offsets: List[int] = []
        if src.target_path is not None:
            offsets = find_function_offsets(
                src.target_path,
                frames_per_case,
                require_verified=True,
            )
        precomputed_offsets_per_src.append(offsets)
        if not has_verified_set and len(offsets) >= frames_per_case:
            # This source can provide a full set of frames where both
            # function and file:line are known in advance.
            has_verified_set = True

    if not has_verified_set:
        print(
            "WARNING: No ELF in the given rootfs produced a full set of "
            "verified function+file:line offsets. Sample logs will not "
            "contain a guaranteed-resolvable case.",
            file=sys.stderr,
        )

    for log_index in range(log_count):
        # Decide output path for this log file
        if log_count == 1:
            current_out = out_path
        else:
            out_str = str(out_path)
            if "{index}" in out_str:
                # Allow explicit template like: --output sample_{index}.log
                current_out = Path(out_str.replace("{index}", str(log_index)))
            else:
                # Default pattern: sample.log -> sample_0.log, sample_1.log, ...
                current_out = out_path.with_name(
                    f"{out_path.stem}_{log_index}{out_path.suffix}"
                )

        lines: List[str] = []

        lines.append("===== SAMPLE STACK TRACE GENERATED FROM ROOTFS =====\n")
        lines.append(f"ROOTFS: {rootfs}\n")
        lines.append(f"LOG_INDEX: {log_index}\n")
        lines.append(f"CASES: {', '.join(src.case for src in sources)}\n\n")

        for stack_index in range(stack_count):
            lines.append(f"=== STACK {stack_index} START ===\n\n")

            for src_index, src in enumerate(sources):
                lines.append(
                    f"--- CASE[{src_index}] {src.case} ELF {src.log_path} ---\n"
                )

                # Use precomputed verified offsets if available for this source.
                # If the list is shorter than frames_per_case, remaining frames
                # will fall back to synthetic offsets (may or may not resolve).
                func_offsets: List[int] = precomputed_offsets_per_src[src_index]

                # Pre-calc base address for this (log, stack, case) triple (just for realism)
                base_addr = (
                    0x1FFFF0000000
                    + log_index * 0x1000000
                    + stack_index * 0x100000
                    + src_index * 0x1000
                )

                for j in range(frames_per_case):
                    # Each CASE is an independent stack: #0, #1, ...
                    frame_num = j

                    if j < len(func_offsets):
                        # Use a real function address so that addr2line can
                        # return a proper function/file:line.
                        offset = func_offsets[j]
                    else:
                        # Fallback: a small synthetic offset (may or may not resolve).
                        offset = 0x10 * (j + 1)

                    addr = base_addr + offset
                    addr_str = f"0x{addr:x}"

                    # Base part with ELF+offset
                    frame_prefix = f"#{frame_num} {addr_str}"
                    frame = f"{frame_prefix} ({src.log_path}+0x{offset:x})"

                    # Optionally append BuildId (if we have one)
                    if src.build_id:
                        frame = f"{frame} (BuildId: {src.build_id})"

                    lines.append(frame + "\n")

                lines.append("\n")

            lines.append(f"=== STACK {stack_index} END ===\n\n")

        # Extra: ensure we always have at least one normal-looking stack
        # for /usr/lib64/libglib-2.0.so.6, if it exists in the rootfs.
        forced_glib_rel = Path("usr/lib64/libglib-2.0.so.6")
        forced_glib_path = rootfs / forced_glib_rel
        if forced_glib_path.is_file() and is_elf(forced_glib_path):
            glib_log_path = "/" + str(forced_glib_rel)
            glib_build_id = read_build_id(forced_glib_path)

            # Pick up to 10 function offsets without requiring prior
            # addr2line verification. Even if debug info is missing,
            # we can still generate realistic-looking stack lines
            # based on the symbol table (similar to using 'nm').
            glib_offsets = find_function_offsets(
                forced_glib_path,
                10,
                require_verified=False,
            )
            if not glib_offsets:
                # Fallback: simple synthetic offsets
                glib_offsets = [0x10 * (i + 1) for i in range(10)]

            lines.append("=== STACK GLIB-2.0 (extra) START ===\n\n")

            base_addr_glib = 0x2FFFF0000000 + log_index * 0x1000000
            for i, offset in enumerate(glib_offsets[:10]):
                addr = base_addr_glib + offset
                addr_str = f"0x{addr:x}"
                frame = f"#{i} {addr_str} ({glib_log_path}+0x{offset:x})"
                if glib_build_id:
                    frame = f"{frame} (BuildId: {glib_build_id})"
                lines.append(frame + "\n")

            lines.append("\n=== STACK GLIB-2.0 (extra) END ===\n\n")

        with current_out.open("w", encoding="utf-8") as f:
            f.writelines(lines)

    print(
        f"Generated {log_count} sample stack log file(s) under {out_path.parent} "
        f"using {len(elfs)} ELF(s) and {len(sources)} case(s), stacks per log: {stack_count}, "
        f"frames per case: {frames_per_case}"
    )


if __name__ == "__main__":
    main()