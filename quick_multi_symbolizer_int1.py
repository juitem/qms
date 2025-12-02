#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Parallel stack trace symbolizer with GNU/LLVM modes, inline expansion, rootfs/debug-root,
recursive debug search (with symlink target basename), and SQLite cache.

Features:
- Parse lines like:
    #1 0x1ffff9de0d58 (/usr/share/.../libwakeup-engine.so+0x1fdb8) (Build-id:aa0d6e0...)
- Collect unique (orig_elf, offset, build_id).
- GNU mode: resolve separate debuginfo using:
    * .gnu_debuglink (same dir, .debug subdir)
    * multiple --debug-root paths used as build-id roots
    * Yocto-style: dirname(debug_root)/<full-orig-path>.debug
    * (optional) recursive search under each debug-root for *.debug files,
      matched by basename, including symlink target basename.
- LLVM mode: rely on llvm-addr2line's own debug search.
- Parallel symbolization per target ELF.
- Parallel file rewrite.
- SQLite cache: (orig_elf, offset) -> frames JSON [(func, loc), ...]
- Only stack-frame lines (lines starting with "#n") are rewritten.
- Non-inline mode:
    One frame per address.
    #n 0xADDR in FUNC FILE:LINE (Build-id:...)
- Inline mode (--inline):
    One address can expand to multiple frames, each as its own #n line:
        #1 0xADDR in f1 file1:line1 ...
        #2 0xADDR in f2 file2:line2 ...
      All later #n are renumbered within the same stack dump.
- Fully unknown (all frames func=="??" and loc in {"??:0","?:0"}):
    First frame:
        #n 0xADDR in ?? ??:0 (/path/lib.so+0xOFF) (Build-id:...)
    Subsequent inline frames:
        #n 0xADDR in ?? ??:0
- Failure information dumped to failed_symbolization.tsv in output dir.
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from multiprocessing import Pool, cpu_count
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import perf_counter
from typing import Dict, List, Optional, Tuple

# ==========================
# Regex patterns
# ==========================

# (/path/to/lib.so+0x1234)
STACK_ENTRY_PATTERN = re.compile(
    r'\('
    r'(?P<path>/[^()]+?)'
    r'\+'
    r'(?P<offset>0x[0-9A-Fa-f]+)'
    r'\)'
)

# (Build-id:aa0d6e0...)
BUILD_ID_PATTERN = re.compile(
    r'\((?:[Bb]uild[- ]?id)\s*:\s*([0-9A-Fa-f]+)\)'
)

# Stack frame line prefix: "   #1 0xADDR ..."
STACK_LINE_PREFIX = re.compile(r'^\s*#\d+\b')


# ==========================
# Data structures
# ==========================

@dataclass(frozen=True)
class OriginKey:
    orig_elf: str
    offset: str


@dataclass
class OriginInfo:
    build_id: Optional[str]  # may be None


@dataclass
class SymbolInfo:
    # List of frames: each is (func, loc)
    # Non-inline mode: len(frames) == 1
    # Inline mode: len(frames) >= 1 (inlined chain)
    frames: List[Tuple[str, str]]


@dataclass
class FailureInfo:
    orig_elf: str
    offset: str
    build_id: Optional[str]
    target_elf: Optional[str]
    reason: str


# ==========================
# SQLite cache
# ==========================

def _ensure_db_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS symbols ("
        "orig_elf TEXT, "
        "offset   TEXT, "
        "frames   TEXT, "  # JSON-encoded list of [func, loc]
        "PRIMARY KEY(orig_elf, offset)"
        ")"
    )
    conn.commit()


def load_cache_from_db(db_path: str) -> Dict[OriginKey, SymbolInfo]:
    cache: Dict[OriginKey, SymbolInfo] = {}
    if not db_path or not os.path.exists(db_path):
        return cache

    conn = sqlite3.connect(db_path)
    try:
        _ensure_db_schema(conn)
        cur = conn.cursor()
        cur.execute("SELECT orig_elf, offset, frames FROM symbols")
        for orig_elf, offset, frames_json in cur.fetchall():
            try:
                frames_raw = json.loads(frames_json)
            except Exception:
                frames_raw = []
            frames: List[Tuple[str, str]] = []
            for item in frames_raw:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    f, l = str(item[0]), str(item[1])
                elif isinstance(item, dict) and "func" in item and "loc" in item:
                    f, l = str(item["func"]), str(item["loc"])
                else:
                    continue
                if not f:
                    f = "??"
                if not l:
                    l = "??:0"
                if f == "???":
                    f = "??"
                if l == "??":
                    l = "??:0"
                frames.append((f, l))
            if not frames:
                frames = [("??", "??:0")]
            cache[OriginKey(orig_elf, offset)] = SymbolInfo(frames=frames)
    finally:
        conn.close()
    return cache


def save_cache_to_db(db_path: str, new_items: Dict[OriginKey, SymbolInfo]) -> None:
    if not db_path or not new_items:
        return

    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        _ensure_db_schema(conn)
        cur = conn.cursor()
        rows = []
        for k, v in new_items.items():
            frames_json = json.dumps(list(v.frames), ensure_ascii=False)
            rows.append((k.orig_elf, k.offset, frames_json))
        cur.executemany(
            "INSERT OR REPLACE INTO symbols (orig_elf, offset, frames) VALUES (?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


# ==========================
# Collect origins from logs
# ==========================

def collect_origins(root_dir: str) -> Dict[OriginKey, OriginInfo]:
    """
    Scan all files under root_dir, collect unique (orig_elf, offset, build_id).
    Assumption: Build-id, if present, is on the same line as the stack entry.
    """
    origins: Dict[OriginKey, OriginInfo] = {}

    for dirpath, _, filenames in os.walk(root_dir):
        for fn in filenames:
            in_path = os.path.join(dirpath, fn)
            try:
                with open(in_path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        stack_matches = list(STACK_ENTRY_PATTERN.finditer(line))
                        if not stack_matches:
                            continue
                        build_match = BUILD_ID_PATTERN.search(line)
                        build_id = build_match.group(1) if build_match else None

                        for sm in stack_matches:
                            path = " ".join(sm.group("path").split())
                            off = sm.group("offset")
                            key = OriginKey(path, off)
                            if key not in origins:
                                origins[key] = OriginInfo(build_id=build_id)
            except OSError:
                continue

    return origins


# ==========================
# Addr2line process wrapper (non-inline mode)
# ==========================

class Addr2LineProcess:
    """
    Persistent addr2line process for a single ELF (non-inline mode).
    Each address produces exactly 2 lines (func, loc).
    """

    def __init__(self, addr2line_bin: str, elf_path: str, demangle: bool):
        self.addr2line_bin = addr2line_bin
        self.elf_path = elf_path
        # -C => demangle C++ symbols
        if demangle:
            args = [self.addr2line_bin, "-fCpe", self.elf_path]
        else:
            args = [self.addr2line_bin, "-fpe", self.elf_path]
        self.proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )

    def symbolize_many(self, offsets: List[str]) -> Dict[str, SymbolInfo]:
        result: Dict[str, SymbolInfo] = {}
        if not offsets:
            return result

        for off in offsets:
            self.proc.stdin.write(off + "\n")
        self.proc.stdin.flush()

        for off in offsets:
            func_line = self.proc.stdout.readline()
            if not func_line:
                result[off] = SymbolInfo(frames=[("??", "??:0")])
                continue
            loc_line = self.proc.stdout.readline()
            if not loc_line:
                loc_line = "??:0\n"

            func = func_line.strip() or "??"
            loc = loc_line.strip() or "??:0"

            if func in ("???",):
                func = "??"
            if loc == "??":
                loc = "??:0"

            result[off] = SymbolInfo(frames=[(func, loc)])

        return result

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=1)
        except Exception:
            pass


# ==========================
# Inline mode addr2line wrapper
# ==========================

def run_addr2line_inline(
    addr2line_bin: str,
    elf_path: str,
    offset: str,
    demangle: bool,
) -> SymbolInfo:
    """
    Call addr2line once for a single address with -i to get inlined frames.
    We store all frames in SymbolInfo.frames.
    """
    # -i : show inlined frames
    # -C : demangle C++ symbols (optional)
    if demangle:
        cmd = [addr2line_bin, "-fCpei", elf_path, offset]
    else:
        cmd = [addr2line_bin, "-fpei", elf_path, offset]

    try:
        out = subprocess.check_output(
            cmd,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception:
        return SymbolInfo(frames=[("??", "??:0")])

    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if not lines:
        return SymbolInfo(frames=[("??", "??:0")])

    # lines: [func1, loc1, func2, loc2, ...]
    frames: List[Tuple[str, str]] = []
    it = iter(lines)
    for func in it:
        try:
            loc = next(it)
        except StopIteration:
            loc = "??:0"
        f = func or "??"
        l = loc or "??:0"
        if f == "???":
            f = "??"
        if l == "??":
            l = "??:0"
        frames.append((f, l))

    if not frames:
        frames = [("??", "??:0")]

    return SymbolInfo(frames=frames)


# ==========================
# Rootfs helper
# ==========================

def apply_rootfs(rootfs: str, logical_path: str) -> str:
    """
    Prepend rootfs to a logical absolute path like /usr/lib64/libfoo.so.
    """
    logical = logical_path.lstrip("/")
    return os.path.join(rootfs, logical)


# ==========================
# Recursive debug search (with symlink target basename)
# ==========================

def build_recursive_debug_map(
    rootfs: str,
    debug_roots_logical: List[str],
) -> Dict[str, List[str]]:
    """
    Recursively scan each debug-root (under rootfs) for files ending with ".debug".

    Returns:
        basename -> list of full absolute paths

    - Key includes:
        * basename of the .debug file itself
        * basename of its symlink target (if it is a symlink)
          e.g. /usr/lib/debug/.build-id/aa/bb.debug -> /usr/lib64/.debug/libfoo.so.debug
               mapping["libfoo.so.debug"] includes "/usr/lib/debug/.build-id/aa/bb.debug"
    """
    mapping: Dict[str, List[str]] = defaultdict(list)

    for dbg_root_logical in debug_roots_logical:
        base = apply_rootfs(rootfs, dbg_root_logical)
        if not os.path.isdir(base):
            continue

        for dirpath, _, filenames in os.walk(base):
            for fn in filenames:
                if not fn.endswith(".debug"):
                    continue
                full_path = os.path.join(dirpath, fn)

                # 1) Map by the file's own basename
                mapping[fn].append(full_path)

                # 2) If it's a symlink, also map by symlink target's basename
                if os.path.islink(full_path):
                    try:
                        real_path = os.path.realpath(full_path)
                    except OSError:
                        continue
                    real_bn = os.path.basename(real_path)
                    if real_bn.endswith(".debug"):
                        mapping[real_bn].append(full_path)

    return mapping


# ==========================
# GNU mode: resolve target ELF
# ==========================

def read_gnu_debuglink(real_elf: str) -> Optional[str]:
    """
    Try to read .gnu_debuglink via 'readelf --string-dump=.gnu_debuglink'.
    Returns the debug file name or None.
    """
    try:
        out = subprocess.check_output(
            ["readelf", "--string-dump=.gnu_debuglink", real_elf],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception:
        return None

    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if not lines:
        return None

    for ln in reversed(lines):
        if "/" in ln:
            continue
        if ".debug" in ln or "." in ln:
            return ln
    return None


def resolve_target_elf_gnu(
    orig_elf: str,
    build_id: Optional[str],
    rootfs: str,
    debug_roots_logical: List[str],
    recursive_debug_map: Dict[str, List[str]],
) -> Tuple[str, Optional[str]]:
    """
    Resolve debuginfo ELF for GNU mode.

    Search order:
      1) .gnu_debuglink in same dir and .debug subdir
      2) For each debug_root in debug_roots_logical:
         - treat as build-id root: <root>/<aa>/<rest>.debug, <root>/<aa>/<rest>
         - treat dirname(root) as debug-parent and try:
             <debug-parent>/<orig_elf>.debug
      3) If recursive_debug_map is not empty:
         - basename(orig_elf) + ".debug" as key
         - match against recursive_debug_map keys and pick a stable path
      4) Fallback to original ELF.

    Returns:
        (target_elf_real_path, debug_reason_if_fallback_or_missing)
        If debug file found cleanly, reason is None.
    """
    reason_parts: List[str] = []
    real_orig = apply_rootfs(rootfs, orig_elf)

    if not os.path.exists(real_orig):
        return real_orig, "origin ELF missing"

    # 1) gnu_debuglink
    dbg_link = read_gnu_debuglink(real_orig)
    if dbg_link:
        base_dir = os.path.dirname(real_orig)
        candidates = [
            os.path.join(base_dir, dbg_link),
            os.path.join(base_dir, ".debug", dbg_link),
        ]
        for c in candidates:
            if os.path.exists(c):
                return c, None
        reason_parts.append(f".gnu_debuglink={dbg_link} missing")

    # 2) Multiple debug_roots: build-id layout and Yocto-style layout
    if debug_roots_logical and build_id:
        bid = build_id.lower()
        if len(bid) >= 3:
            head = bid[:2]
            tail = bid[2:]
            for dbg_root_logical in debug_roots_logical:
                debug_root_real = apply_rootfs(rootfs, dbg_root_logical)
                cands = [
                    os.path.join(debug_root_real, head, tail + ".debug"),
                    os.path.join(debug_root_real, head, tail),
                ]
                for c in cands:
                    if os.path.exists(c):
                        reason = "; ".join(reason_parts) if reason_parts else None
                        return c, reason
            reason_parts.append(f"no debug file found for build-id={build_id}")

    if debug_roots_logical:
        for dbg_root_logical in debug_roots_logical:
            debug_root_real = apply_rootfs(rootfs, dbg_root_logical)
            debug_parent = os.path.dirname(debug_root_real)
            yocto_debug = os.path.join(debug_parent, orig_elf.lstrip("/") + ".debug")
            yocto_debug = os.path.join(rootfs, yocto_debug.lstrip("/"))
            if os.path.exists(yocto_debug):
                reason = "; ".join(reason_parts) if reason_parts else None
                return yocto_debug, reason

    # 3) Recursive debug search by basename (if enabled)
    if recursive_debug_map:
        base = os.path.basename(orig_elf)
        candidates_names = [base + ".debug"]
        for name in candidates_names:
            paths = recursive_debug_map.get(name)
            if paths:
                chosen = sorted(paths)[0]  # choose a stable path
                reason = "; ".join(reason_parts) if reason_parts else None
                return chosen, reason
        reason_parts.append("recursive debug search did not find a match")

    # 4) Fallback to original ELF
    if not reason_parts:
        reason = "using stripped origin ELF (no separate debug file found)"
    else:
        reason_parts.append("fallback to stripped origin ELF")
        reason = "; ".join(reason_parts)
    return real_orig, reason


# ==========================
# Build jobs per target ELF
# ==========================

def build_jobs_by_target(
    origins: Dict[OriginKey, OriginInfo],
    mode: str,
    rootfs: str,
    debug_roots_logical: List[str],
    recursive_debug_map: Dict[str, List[str]],
) -> Tuple[
    Dict[str, List[OriginKey]],
    Dict[OriginKey, str],
    List[FailureInfo],
]:
    """
    Group origins by target ELF path.

    Returns:
        jobs_by_target: target_elf_real -> list[OriginKey]
        target_for_origin: OriginKey -> target_elf_real
        debug_failures: list of FailureInfo for missing debug/origin
    """
    jobs_by_target: Dict[str, List[OriginKey]] = defaultdict(list)
    target_for_origin: Dict[OriginKey, str] = {}
    debug_failures: List[FailureInfo] = []

    for key, info in origins.items():
        orig_elf = key.orig_elf
        build_id = info.build_id

        if mode == "llvm":
            target_elf = apply_rootfs(rootfs, orig_elf)
            if not os.path.exists(target_elf):
                debug_failures.append(
                    FailureInfo(
                        orig_elf=orig_elf,
                        offset=key.offset,
                        build_id=build_id,
                        target_elf=None,
                        reason="origin ELF missing (llvm mode)",
                    )
                )
            jobs_by_target[target_elf].append(key)
            target_for_origin[key] = target_elf
            continue

        # GNU mode
        target_elf, debug_reason = resolve_target_elf_gnu(
            orig_elf,
            build_id,
            rootfs,
            debug_roots_logical,
            recursive_debug_map,
        )
        jobs_by_target[target_elf].append(key)
        target_for_origin[key] = target_elf
        if debug_reason:
            debug_failures.append(
                FailureInfo(
                    orig_elf=orig_elf,
                    offset=key.offset,
                    build_id=build_id,
                    target_elf=target_elf,
                    reason=debug_reason,
                )
            )

    return jobs_by_target, target_for_origin, debug_failures


# ==========================
# Parallel symbolization
# ==========================

def _symbolize_one_elf(args):
    addr2line_bin, target_elf, origin_keys, origins_by_target, use_inline, demangle = args
    results: Dict[OriginKey, SymbolInfo] = {}
    failures_local: List[FailureInfo] = []

    if not os.path.exists(target_elf):
        for key in origin_keys:
            info = origins_by_target[key]
            failures_local.append(
                FailureInfo(
                    orig_elf=key.orig_elf,
                    offset=key.offset,
                    build_id=info.build_id,
                    target_elf=target_elf,
                    reason="target ELF missing",
                )
            )
        return results, failures_local

    if use_inline:
        # Inline mode: one addr2line call per address
        for key in origin_keys:
            info = origins_by_target[key]
            sym = run_addr2line_inline(addr2line_bin, target_elf, key.offset, demangle)
            results[key] = sym
        return results, failures_local

    # Non-inline mode: persistent addr2line process
    proc = Addr2LineProcess(addr2line_bin, target_elf, demangle)
    try:
        offsets = sorted({key.offset for key in origin_keys})
        symmap = proc.symbolize_many(offsets)

        for key in origin_keys:
            info = origins_by_target[key]
            sym = symmap.get(key.offset)
            if sym is None:
                failures_local.append(
                    FailureInfo(
                        orig_elf=key.orig_elf,
                        offset=key.offset,
                        build_id=info.build_id,
                        target_elf=target_elf,
                        reason="no symbolization result",
                    )
                )
                continue
            results[key] = sym
    finally:
        proc.close()

    return results, failures_local


def symbolize_all_parallel(
    addr2line_bin: str,
    jobs_by_target: Dict[str, List[OriginKey]],
    origins: Dict[OriginKey, OriginInfo],
    workers: int,
    use_inline: bool,
    demangle: bool,
    progress: bool,
) -> Tuple[Dict[OriginKey, SymbolInfo], List[FailureInfo]]:
    workers = max(1, workers)
    tasks = [
        (addr2line_bin, target_elf, origin_keys, origins, use_inline, demangle)
        for target_elf, origin_keys in jobs_by_target.items()
    ]

    combined_results: Dict[OriginKey, SymbolInfo] = {}
    combined_failures: List[FailureInfo] = []

    if not tasks:
        return combined_results, combined_failures

    total = len(tasks)
    completed = 0

    with Pool(workers) as pool:
        for res, fails in pool.imap_unordered(_symbolize_one_elf, tasks):
            combined_results.update(res)
            combined_failures.extend(fails)
            if progress:
                completed += 1
                pct = (completed * 100.0) / total
                print(f"[PROGRESS] ELF {completed}/{total} ({pct:.1f}%)")

    return combined_results, combined_failures


# ==========================
# Rewrite logic (inline-aware, per-dump #n reset)
# ==========================

def transform_text(text: str, cache: Dict[OriginKey, SymbolInfo]) -> str:
    """
    Rewrite stack frame lines using symbol cache, expanding inline frames and
    renumbering #n within each stack dump.

    Rules:
    - Only lines starting with "#n" (stack frames) are rewritten and renumbered.
    - Non-stack lines are kept as-is.
    - When a line starts with "#0", the counter is reset to 0 (new stack dump).
    - For each stack frame line with "(path+0xOFF)":
        * Find SymbolInfo for (path, offset).
        * If no SymbolInfo or empty frames: treat as unknown [("??","??:0")].
        * If frames length == 1 (non-inline or single frame):
              #k 0xADDR in FUNC LOC [tail]
        * If frames length > 1 (inline):
              #k   0xADDR in FUNC_0 LOC_0 [tail]
              #k+1 0xADDR in FUNC_1 LOC_1
              #k+2 0xADDR in FUNC_2 LOC_2
        * Fully unknown: all frames (func=="??", loc in {"??:0","?:0"}):
              first frame:
                  #k 0xADDR in ?? ??:0 (/path/lib.so+0xOFF ...Build-id...)
              subsequent frames:
                  #k+1 0xADDR in ?? ??:0
    - If a stack frame line does not contain "(path+0xOFF)", we only renumber #n.
    """

    lines = text.splitlines(keepends=True)
    out_lines: List[str] = []
    next_index = 0  # current stack index, resets on each "#0" line

    for line in lines:
        # 1) Non-stack-frame line → keep as-is
        if not STACK_LINE_PREFIX.match(line):
            out_lines.append(line)
            continue

        # 2) Check original frame index (#0, #1, ...)
        m_index = re.match(r"^(\s*)#(\d+)\b", line)
        if m_index:
            try:
                orig_idx = int(m_index.group(2))
            except ValueError:
                orig_idx = None
            if orig_idx == 0:
                # New stack dump: reset counter
                next_index = 0

        # 3) Try to find "(path+0xOFF)"
        m = STACK_ENTRY_PATTERN.search(line)
        if not m:
            # No ELF+offset: just renumber "#n" and keep the rest
            m_head = re.match(r"^(\s*)#\d+\b(.*)$", line)
            if m_head:
                indent = m_head.group(1)
                rest = m_head.group(2).lstrip()
                new_line = f"{indent}#{next_index} {rest}"
                if not new_line.endswith("\n"):
                    new_line += "\n"
                out_lines.append(new_line)
                next_index += 1
            else:
                out_lines.append(line)
            continue

        # 4) Extract original path / offset
        orig_path = " ".join(m.group("path").split())
        offset = m.group("offset")
        key = OriginKey(orig_elf=orig_path, offset=offset)

        sym = cache.get(key)
        if sym is None or not sym.frames:
            frames = [("??", "??:0")]
        else:
            frames = sym.frames

        # Check if all frames are fully unknown
        all_unknown = all(
            (f.strip() == "??" and l.strip() in ("??:0", "?:0"))
            for (f, l) in frames
        )

        # 5) Extract indent + address from "#n 0xADDR ..."
        m_head = re.match(r"^(\s*)#\d+\s+(0x[0-9A-Fa-f]+)(.*)$", line)
        if m_head:
            indent = m_head.group(1)
            addr = m_head.group(2)
        else:
            indent = ""
            m_addr = re.search(r"(0x[0-9A-Fa-f]+)", line)
            addr = m_addr.group(1) if m_addr else "0x0"

        # 6) Tail:
        original_tail_full = line[m.start():]      # "(path+0xOFF) (Build-id...)"
        tail_after_paren   = line[m.end():].lstrip()

        # 7) Emit one line per frame
        for idx, (func, loc) in enumerate(frames):
            func_s = func.strip() or "??"
            loc_s = loc.strip() or "??:0"

            prefix = f"{indent}#{next_index} {addr}"

            if all_unknown:
                if idx == 0:
                    # Keep original ELF+offset + Build-id tail
                    tail = original_tail_full.lstrip()
                    new_line = f"{prefix} in ?? ??:0 {tail}"
                else:
                    new_line = f"{prefix} in ?? ??:0\n"
            else:
                if func_s == "??" and loc_s in ("??:0", "?:0"):
                    new_line = f"{prefix} in ?? ??:0\n"
                else:
                    if idx == 0:
                        if tail_after_paren:
                            new_line = f"{prefix} in {func_s} {loc_s} {tail_after_paren}"
                        else:
                            new_line = f"{prefix} in {func_s} {loc_s}\n"
                    else:
                        new_line = f"{prefix} in {func_s} {loc_s}\n"

            out_lines.append(new_line)
            next_index += 1

    return "".join(out_lines)


def _rewrite_one_file(args):
    in_path, out_path, cache = args
    try:
        with open(in_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return

    new_text = transform_text(text, cache)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(new_text)


def rewrite_files_parallel(
    input_dir: str,
    output_dir: str,
    cache: Dict[OriginKey, SymbolInfo],
    workers: int,
    progress: bool,
) -> None:
    tasks = []
    for dirpath, _, filenames in os.walk(input_dir):
        for fn in filenames:
            in_path = os.path.join(dirpath, fn)
            rel = os.path.relpath(in_path, input_dir)
            out_path = os.path.join(output_dir, rel)
            tasks.append((in_path, out_path, cache))

    workers = max(1, workers)
    if not tasks:
        return

    total_files = len(tasks)

    # Use ThreadPoolExecutor for I/O-bound file rewrite (backup-style behavior)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        if progress:
            completed = 0
            futures = [executor.submit(_rewrite_one_file, t) for t in tasks]
            for _ in as_completed(futures):
                completed += 1
                pct = (completed * 100.0) / total_files
                print(
                    f"\r[PROGRESS] files {completed}/{total_files} ({pct:.1f}%)",
                    end="",
                    flush=True,
                )
            print()
        else:
            executor.map(_rewrite_one_file, tasks)


# ==========================
# Failure log
# ==========================

def save_failures(failures: List[FailureInfo], out_dir: str) -> None:
    if not failures:
        return
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "failed_symbolization.tsv")
    with open(path, "w", encoding="utf-8") as f:
        f.write("orig_elf\toffset\tbuild_id\ttarget_elf\treason\n")
        for fi in failures:
            f.write(
                f"{fi.orig_elf}\t{fi.offset}\t{fi.build_id or ''}\t"
                f"{fi.target_elf or ''}\t{fi.reason}\n"
            )


# ==========================
# CLI / main
# ==========================

def build_addr2line_bin(base_bin: str, cross_prefix: Optional[str]) -> str:
    if cross_prefix:
        return cross_prefix + "addr2line"
    return base_bin


def main():
    parser = argparse.ArgumentParser(
        description="Parallel stack trace symbolizer with GNU/LLVM modes, inline expansion, recursive debug search, and SQLite cache."
    )
    parser.add_argument(
        "--input-dir",
        dest="input_dir",
        required=True,
        help="Input directory containing log files",
    )
    parser.add_argument(
        "--output-dir",
        dest="output_dir",
        required=True,
        help="Output directory for rewritten logs",
    )

    parser.add_argument(
        "--mode",
        choices=["llvm", "gnu"],
        default="llvm",
        help="Symbolizer mode: llvm (default) or gnu",
    )
    parser.add_argument(
        "--rootfs",
        default="./download/img/ROOTFS",
        help="Rootfs base directory (logical / mapped to this dir).",
    )
    parser.add_argument(
        "--debug-root",
        action="append",
        default=["/usr/lib/debug/.build-id"],
        help=(
            "Logical debug root for build-id or debug tree "
            "(e.g. /usr/lib/debug/.build-id). "
            "Can be given multiple times."
        ),
    )
    parser.add_argument(
        "--recursive-debug-search",
        action="store_true",
        help="Recursively scan all debug-root paths for *.debug files and match by basename (including symlink targets).",
    )

    parser.add_argument(
        "--addr2line-bin",
        default="addr2line",
        help=(
            "Base addr2line binary name/path (without cross prefix). "
            "In llvm mode, the default 'addr2line' is treated as 'llvm-addr2line'."
        ),
    )
    parser.add_argument(
        "-c",
        "--cross-prefix",
        default=None,
        help="Cross toolchain prefix (e.g. aarch64-linux-gnu-). Will prepend this "
             "to 'addr2line' in gnu mode.",
    )

    parser.add_argument(
        "--workers-symbols",
        type=int,
        default=max(1, cpu_count() // 2),
        help="Number of parallel workers for symbolization.",
    )
    parser.add_argument(
        "--workers-rewrite",
        type=int,
        default=max(1, cpu_count() // 2),
        help="Number of parallel workers for file rewrite.",
    )

    parser.add_argument(
        "--cache-db",
        default=None,
        help="SQLite cache DB path for (orig_elf, offset) -> frames(JSON).",
    )

    # Demangle switch: default = True (keep current behavior),
    # user can explicitly disable with --no-demangle.
    demangle_group = parser.add_mutually_exclusive_group()
    demangle_group.add_argument(
        "-d",
        "--demangle",
        dest="demangle",
        action="store_true",
        help="Enable C++ name demangling (pass -C to addr2line). Default: on.",
    )
    demangle_group.add_argument(
        "--no-demangle",
        dest="demangle",
        action="store_false",
        help="Disable C++ name demangling (do not pass -C).",
    )
    parser.set_defaults(demangle=True)

    parser.add_argument(
        "--inline",
        action="store_true",
        help="Use addr2line -i and expand inlined frames as separate stack entries (slower).",
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Print timing information for major phases.",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Print progress for symbolization and file rewrite.",
    )

    args = parser.parse_args()

    benchmark = args.benchmark
    if benchmark:
        t_start = perf_counter()
        t_prev = t_start

    rootfs = os.path.abspath(args.rootfs)
    if not os.path.isdir(rootfs):
        print(f"[ERROR] rootfs does not exist: {rootfs}", file=sys.stderr)
        sys.exit(1)

    debug_roots_logical: List[str] = args.debug_root
    if args.mode == "gnu" and debug_roots_logical:
        for dbg_root in debug_roots_logical:
            debug_root_real = apply_rootfs(rootfs, dbg_root)
            if not os.path.isdir(debug_root_real):
                print(
                    f"[ERROR] debug-root does not exist under rootfs: {debug_root_real}",
                    file=sys.stderr,
                )
                sys.exit(1)

    # Decide which addr2line binary to use based on mode.
    if args.mode == "llvm":
        # LLVM mode:
        #  - If user left the default 'addr2line', interpret it as 'llvm-addr2line'.
        #  - If user explicitly set something, respect it as-is.
        if args.addr2line_bin == "addr2line":
            addr2line_bin = "llvm-addr2line"
        else:
            addr2line_bin = args.addr2line_bin
    else:
        # GNU mode:
        #  - Allow cross-prefix + addr2line (e.g. aarch64-linux-gnu-addr2line).
        addr2line_bin = build_addr2line_bin(args.addr2line_bin, args.cross_prefix)

    try:
        subprocess.check_output([addr2line_bin, "--version"], stderr=subprocess.DEVNULL)
    except Exception:
        print(
            f"[ERROR] addr2line binary not executable: {addr2line_bin}",
            file=sys.stderr,
        )
        sys.exit(1)

    input_dir = os.path.abspath(args.input_dir)
    output_dir = os.path.abspath(args.output_dir)

    # 1) Collect origins
    print("[INFO] Collecting origins...")
    origins = collect_origins(input_dir)
    print(f"[INFO] Collected {len(origins)} unique (ELF, offset) pairs.")
    if benchmark:
        t_now = perf_counter()
        print(f"[BENCH] collect_origins: {t_now - t_prev:.3f}s")
        t_prev = t_now

    # 2) Load cache from DB (if any)
    cache_db_initial: Dict[OriginKey, SymbolInfo] = {}
    if args.cache_db:
        print(f"[INFO] Loading cache from DB: {args.cache_db}")
        cache_db_initial = load_cache_from_db(args.cache_db)
        print(f"[INFO] Cache DB entries: {len(cache_db_initial)}")
        if benchmark:
            t_now = perf_counter()
            print(f"[BENCH] load_cache_from_db: {t_now - t_prev:.3f}s")
            t_prev = t_now

    # 3) Recursive debug map (optional)
    recursive_debug_map: Dict[str, List[str]] = {}
    if args.mode == "gnu" and args.recursive_debug_search and debug_roots_logical:
        print("[INFO] Building recursive debug map from debug-root paths...")
        recursive_debug_map = build_recursive_debug_map(rootfs, debug_roots_logical)
        print(f"[INFO] Recursive debug map entries (basenames): {len(recursive_debug_map)}")
        if benchmark:
            t_now = perf_counter()
            print(f"[BENCH] build_recursive_debug_map: {t_now - t_prev:.3f}s")
            t_prev = t_now

    # 4) Decide which origins to symbolize this run
    # Use cache DB for both inline and non-inline modes:
    # if an origin is already in the DB, do not re-symbolize it.
    remaining_origins: Dict[OriginKey, OriginInfo] = {}
    for k, v in origins.items():
        if k not in cache_db_initial:
            remaining_origins[k] = v

    print(f"[INFO] New origins to symbolize: {len(remaining_origins)}")

    # 5) Build jobs by target ELF (for remaining origins)
    jobs_by_target, target_for_origin, debug_failures = build_jobs_by_target(
        remaining_origins,
        mode=args.mode,
        rootfs=rootfs,
        debug_roots_logical=debug_roots_logical,
        recursive_debug_map=recursive_debug_map,
    )
    if benchmark:
        t_now = perf_counter()
        print(f"[BENCH] build_jobs_by_target: {t_now - t_prev:.3f}s")
        t_prev = t_now

    # 6) Symbolize in parallel for remaining origins
    new_cache: Dict[OriginKey, SymbolInfo] = {}
    sym_failures: List[FailureInfo] = []

    if jobs_by_target:
        print(
            f"[INFO] Symbolizing for {len(jobs_by_target)} target ELFs "
            f"with {args.workers_symbols} workers "
            f"(inline={args.inline}, demangle={args.demangle})..."
        )
        sym_results, sym_failures = symbolize_all_parallel(
            addr2line_bin,
            jobs_by_target,
            remaining_origins,
            workers=args.workers_symbols,
            use_inline=args.inline,
            demangle=args.demangle,
            progress=args.progress,
        )
        new_cache.update(sym_results)
        print(f"[INFO] New symbolized entries: {len(new_cache)}")
        if benchmark:
            t_now = perf_counter()
            print(f"[BENCH] symbolize_all_parallel: {t_now - t_prev:.3f}s")
            t_prev = t_now
    else:
        print("[INFO] No new origins to symbolize.")

    # 7) Save new cache entries to DB
    if args.cache_db and new_cache:
        print(f"[INFO] Saving {len(new_cache)} new entries to cache DB...")
        save_cache_to_db(args.cache_db, new_cache)
        if benchmark:
            t_now = perf_counter()
            print(f"[BENCH] save_cache_to_db: {t_now - t_prev:.3f}s")
            t_prev = t_now

    # 8) Build final in-memory cache
    final_cache: Dict[OriginKey, SymbolInfo] = dict(cache_db_initial)
    final_cache.update(new_cache)
    print(f"[INFO] Total cache size in memory: {len(final_cache)}")

    # 9) Rewrite files in parallel
    print(
        f"[INFO] Rewriting files from '{input_dir}' to '{output_dir}' "
        f"with {args.workers_rewrite} workers..."
    )
    rewrite_files_parallel(
        input_dir,
        output_dir,
        final_cache,
        args.workers_rewrite,
        progress=args.progress,
    )
    if benchmark:
        t_now = perf_counter()
        print(f"[BENCH] rewrite_files_parallel: {t_now - t_prev:.3f}s")
        t_prev = t_now

    # 10) Save failures
    failures_all = debug_failures + sym_failures
    if failures_all:
        print(f"[INFO] Writing failure log for {len(failures_all)} items...")
        save_failures(failures_all, output_dir)
    else:
        print("[INFO] No failures recorded.")
    if benchmark:
        t_now = perf_counter()
        print(f"[BENCH] save_failures: {t_now - t_prev:.3f}s")
        print(f"[BENCH] total_time: {t_now - t_start:.3f}s")

    print("[INFO] Done.")


if __name__ == "__main__":
    main()
