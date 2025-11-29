#!/usr/bin/env python3
"""
symbolize_stacks.py

Features:
  - Parse ASan/Crash-like stack logs containing frames such as:

      #1 0x1ffff9de0d58 (/usr/share/.../libfoo.so+0x1fdb8) (Build-id:aa0D6E0...)

  - Modes:
      * -llvm : use llvm-addr2line, target ELF = rootfs + original ELF path
      * -gnu  : use GNU addr2line, target ELF resolved via:
                1) .gnu_debuglink-based debug ELF (if present)
                2) build-id-based debug ELF under --debug-root (.debug and no-ext)
                3) /usr/lib/debug/<full-path>.debug (Yocto-style)
                4) fallback: rootfs + original ELF

  - rootfs prefix:
      * --rootfs /path/to/rootfs
      * Real ELF path = rootfs + path extracted from log

  - Parallelization (separated):
      * --workers-symbol  N : ELF-level symbolization, ProcessPool
      * --workers-rewrite M : file rewrite, ThreadPool
      * Defaults:
          workers-symbol  = CPU count
          workers-rewrite = max(2, CPU count * 2)

  - GNU cross toolchain support:
      * --cross-prefix PREFIX
          Example: PREFIX="arm-linux-gnueabihf-"
          => addr2line binary = "arm-linux-gnueabihf-addr2line"
      * Only used in -gnu mode when --addr2line is NOT explicitly provided.

  - Delta symbolization (persistent cache):
      * --cache-db path/to/cache.sqlite
      * On: reuse previously symbolized (orig_elf, offset) pairs from SQLite
      * Off (default): behaves exactly like the original script, no SQLite used.

  - Failure logging:
      * <output-dir>/failed_symbolization.tsv
        Fields: orig_elf, offset, build_id, resolved_target_elf, reason

  - Demangling:
      * -d / --demangle : enable C++ name demangling (passes -C to addr2line)
"""

import argparse
import os
import re
import sqlite3
import subprocess
from time import perf_counter
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from functools import lru_cache

# Module-level variable for readelf binary
READ_ELF_BIN = "readelf"
from typing import Dict, List, Tuple, Set, Optional

# ------------------------------------------------------------
# Regex patterns
# ------------------------------------------------------------

# Matches "( /path/.../lib.so+0x1234 )"
# DOTALL allows accidental newlines inside parentheses, but real logs
# normally keep this on a single line.
STACK_ENTRY_PATTERN = re.compile(
    r'\('
    r'(?P<path>/[^\+]+?)'
    r'\+'
    r'(?P<offset>0x[0-9A-Fa-f]+)'
    r'\)',
    re.DOTALL,
)

# Matches "(Build-id:xxxx)" / "(buildid: xxxx)" etc.
BUILD_ID_PATTERN = re.compile(
    r'\('
    r'(?:build[- ]?id)\s*:\s*'
    r'([0-9A-Fa-f]+)'
    r'\)',
    re.IGNORECASE,
)


# ------------------------------------------------------------
# Build-id and rootfs helpers
# ------------------------------------------------------------

def build_id_to_debug_paths(build_id: str, debug_root: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Return two possible debug file paths for a given build-id:
      1) <debug_root>/<first2>/<rest>.debug
      2) <debug_root>/<first2>/<rest>

    Some systems append ".debug", some do not.
    """
    if len(build_id) < 3:
        return None, None
    first2 = build_id[:2]
    rest = build_id[2:]
    base = os.path.join(debug_root, first2, rest)
    return base + ".debug", base


def apply_rootfs(rootfs: str, path: str) -> str:
    """
    Safely combine rootfs + absolute ELF path.
    rootfs="/mnt/rootfs", path="/usr/lib/libfoo.so"
      -> "/mnt/rootfs/usr/lib/libfoo.so"
    """
    if not rootfs:
        return path
    return os.path.join(rootfs, path.lstrip("/"))


# ------------------------------------------------------------
# .gnu_debuglink helper (via readelf)
# ------------------------------------------------------------

@lru_cache(maxsize=None)
def get_gnu_debuglink(real_elf_path: str) -> Optional[str]:
    """
    Extract .gnu_debuglink filename from an ELF using 'readelf'.

    Returns:
      debuglink file name (e.g. "libfoo.so.debug"),
      or None if section is missing or cannot be parsed.
    """
    if not os.path.exists(real_elf_path):
        return None
    try:
        out = subprocess.check_output(
            [READ_ELF_BIN, "--string-dump=.gnu_debuglink", real_elf_path],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception:
        return None

    # Typical readelf output example:
    #
    # String dump of section '.gnu_debuglink':
    #   [     0]  libfoo.so.debug
    #
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("String dump of section"):
            continue
        if line.startswith("Hex dump of section"):
            continue
        parts = line.split()
        if parts and parts[0].startswith("["):
            # last token is usually the file name
            return parts[-1]
    return None


# ------------------------------------------------------------
# Persistent addr2line runner
# ------------------------------------------------------------

class Addr2LineProcess:
    def __init__(self, addr2line_bin: str, elf_path: str, demangle: bool):
        self.elf_path = elf_path
        self.addr2line_bin = addr2line_bin
        cmd = [self.addr2line_bin, "-f"]
        if demangle:
            cmd.append("-C")
        cmd.extend(["-e", self.elf_path])
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def symbolize_many(self, addresses: List[str]) -> Dict[str, Tuple[str, str]]:
        if not addresses:
            return {}

        for addr in addresses:
            self.proc.stdin.write(addr + "\n")
        self.proc.stdin.flush()

        results: Dict[str, Tuple[str, str]] = {}
        for addr in addresses:
            func = self.proc.stdout.readline()
            loc = self.proc.stdout.readline()
            if not func or not loc:
                break
            results[addr] = (func.rstrip("\n"), loc.rstrip("\n"))
        return results

    def close(self):
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


# ------------------------------------------------------------
# 1) Scan logs and collect origins
# ------------------------------------------------------------

def collect_origins(input_dir: str) -> Tuple[Dict[Tuple[str, str], Optional[str]], List[str]]:
    """
    Scan all files under input_dir and extract:
      (orig_elf_path, offset) -> build-id (nearest match in same text region)

    Returns:
      origins: mapping from (orig_elf, offset) to build-id
      all_files: list of discovered file paths
    """
    origins: Dict[Tuple[str, str], Optional[str]] = {}
    all_files: List[str] = []

    for root, _dirs, files in os.walk(input_dir):
        for name in files:
            p = os.path.join(root, name)
            all_files.append(p)
            try:
                text = open(p, "r", encoding="utf-8", errors="replace").read()
            except Exception:
                continue

            build_ids = list(BUILD_ID_PATTERN.finditer(text))

            for m in STACK_ENTRY_PATTERN.finditer(text):
                raw_path = m.group("path")
                offset = m.group("offset")
                normalized = " ".join(raw_path.split())
                key = (normalized, offset)
                if key in origins:
                    continue

                pos = m.start()
                nearest_dist: Optional[int] = None
                chosen: Optional[str] = None
                for b in build_ids:
                    d = abs(b.start() - pos)
                    if nearest_dist is None or d < nearest_dist:
                        nearest_dist = d
                        chosen = b.group(1)

                origins[key] = chosen

    return origins, all_files


# ------------------------------------------------------------
# 2) Debug ELF resolution with fallback priority (GNU mode)
# ------------------------------------------------------------

@lru_cache(maxsize=None)
def _resolve_target_elf_single(
    orig_elf: str,
    build_id: Optional[str],
    mode: str,
    debug_root: str,
    rootfs: str,
) -> Tuple[str, Optional[str]]:
    """
    Resolve which ELF should be used to symbolize (orig_elf, build_id).

    For GNU mode, the fallback priority is:
      1) .gnu_debuglink-based paths:
         - <dir(orig_elf)>/<debuglink_name>
         - <dir(orig_elf)>/.debug/<debuglink_name>
      2) build-id-based debug files under debug_root:
         - <debug_root>/<first2>/<rest>.debug
         - <debug_root>/<first2>/<rest>
      3) /usr/lib/debug<orig_elf>.debug (Yocto-style)
      4) fallback: rootfs + orig_elf

    For LLVM mode, this simply returns rootfs + orig_elf, and LLVM will
    do its own lookup.
    """
    orig_real = apply_rootfs(rootfs, orig_elf)

    if mode != "gnu":
        # Let llvm-addr2line handle its own debug search logic
        return orig_real, None

    candidates: List[str] = []

    # 1) .gnu_debuglink-based candidates
    debuglink_name = get_gnu_debuglink(orig_real)
    if debuglink_name:
        d = os.path.dirname(orig_elf)
        candidates.append(os.path.join(d, debuglink_name))
        candidates.append(os.path.join(d, ".debug", debuglink_name))

    # 2) build-id-based candidates
    if build_id:
        cand1, cand2 = build_id_to_debug_paths(build_id, debug_root)
        if cand1:
            candidates.append(cand1)
        if cand2:
            candidates.append(cand2)
    # 3) Yocto-style: /usr/lib/debug/<full-path>.debug
    debug_root_parent = os.path.dirname(debug_root)
    # Only apply Yocto-style fallback when debug_root_parent looks meaningful
    if debug_root_parent and debug_root_parent not in (".", ""):
        candidates.append(os.path.join(debug_root_parent, orig_elf.lstrip("/")) + ".debug")
    # Deduplicate while preserving order
    seen = set()
    unique_candidates: List[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique_candidates.append(c)

    for c in unique_candidates:
        real = apply_rootfs(rootfs, c)
        if os.path.exists(real):
            return real, None

    # Nothing found, fallback
    if build_id:
        reason = f"no debug file found (build-id={build_id})"
    elif debuglink_name:
        reason = f"no debug file found (.gnu_debuglink={debuglink_name})"
    else:
        reason = "no debug file found (.gnu_debuglink/build-id not available)"

    return orig_real, reason


def build_jobs_by_target(
    origins: Dict[Tuple[str, str], Optional[str]],
    mode: str,
    debug_root: str,
    rootfs: str,
) -> Tuple[
    Dict[str, Set[str]],
    Dict[Tuple[str, str], str],
    Dict[Tuple[str, str], str],
]:
    """
    Determine which ELF should be used to symbolize each (orig_elf, offset).

    Returns:
      jobs_by_target     : map target_elf_realpath -> set(offsets)
      target_for_origin  : map (orig_elf, offset) -> resolved target ELF path
      debug_missing      : map (orig_elf, offset) -> reason if no matching debug ELF found in GNU mode
    """
    jobs_by_target: Dict[str, Set[str]] = defaultdict(set)
    target_for_origin: Dict[Tuple[str, str], str] = {}
    debug_missing: Dict[Tuple[str, str], str] = {}

    for (orig_elf, offset), build_id in origins.items():
        target_elf, reason = _resolve_target_elf_single(orig_elf, build_id, mode, debug_root, rootfs)

        if reason is not None and mode == "gnu":
            debug_missing[(orig_elf, offset)] = reason

        jobs_by_target[target_elf].add(offset)
        target_for_origin[(orig_elf, offset)] = target_elf

    return jobs_by_target, target_for_origin, debug_missing


# ------------------------------------------------------------
# 3) Parallel symbolization per ELF (ProcessPool)
# ------------------------------------------------------------

def _symbolize_one_elf(args):
    target_elf, offsets, addr2line_bin, demangle = args
    results: Dict[str, Tuple[str, str]] = {}
    fails: List[Tuple[str, str, str]] = []

    if not os.path.exists(target_elf):
        for off in offsets:
            fails.append((target_elf, off, "target ELF missing"))
        return target_elf, results, fails

    proc = Addr2LineProcess(addr2line_bin, target_elf, demangle)
    try:
        sorted_off = sorted(offsets)
        out = proc.symbolize_many(sorted_off)
        for off in sorted_off:
            if off in out:
                results[off] = out[off]
            else:
                fails.append((target_elf, off, "no symbolization result"))
    finally:
        proc.close()

    return target_elf, results, fails


def symbolize_all_parallel(jobs_by_target, addr2line_bin, workers_symbol, demangle):
    """
    Run symbolization in parallel per ELF.
    """
    target_results: Dict[Tuple[str, str], Tuple[str, str]] = {}
    fail_list: List[Tuple[str, str, str]] = []

    if not jobs_by_target:
        return target_results, fail_list

    if workers_symbol <= 1:
        for t_elf, offsets in jobs_by_target.items():
            _, res, fails = _symbolize_one_elf((t_elf, offsets, addr2line_bin, demangle))
            for off, info in res.items():
                target_results[(t_elf, off)] = info
            fail_list.extend(fails)
        return target_results, fail_list

    tasks = [(t_elf, offsets, addr2line_bin, demangle) for t_elf, offsets in jobs_by_target.items()]

    with ProcessPoolExecutor(max_workers=workers_symbol) as ex:
        futures = [ex.submit(_symbolize_one_elf, t) for t in tasks]
        for fut in as_completed(futures):
            t_elf, res, fails = fut.result()
            for off, info in res.items():
                target_results[(t_elf, off)] = info
            fail_list.extend(fails)

    return target_results, fail_list


# ------------------------------------------------------------
# 4) Build in-memory symbol cache
# ------------------------------------------------------------

def build_symbol_cache(origins, target_for_origin, target_results):
    cache: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for (orig_elf, offset) in origins.keys():
        tgt = target_for_origin[(orig_elf, offset)]
        info = target_results.get((tgt, offset))
        if info:
            cache[(orig_elf, offset)] = info
    return cache


# ------------------------------------------------------------
# 5) File rewriting (ThreadPool)
# ------------------------------------------------------------

def transform_text(text, cache):
    def rep(m):
        raw_path = m.group("path")
        off = m.group("offset")
        normalized = " ".join(raw_path.split())
        key = (normalized, off)
        info = cache.get(key)
        if not info:
            return m.group(0)
        func, loc = info
        return f"({normalized}+{off} -> {func} {loc})"

    return STACK_ENTRY_PATTERN.sub(rep, text)


def _rewrite_one_file(args):
    src_path, input_dir, output_dir, cache = args
    rel = os.path.relpath(src_path, start=input_dir)
    dst = os.path.join(output_dir, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)

    try:
        text = open(src_path, "r", encoding="utf-8", errors="replace").read()
        new_text = transform_text(text, cache)
        open(dst, "w", encoding="utf-8").write(new_text)
    except Exception as e:
        print(f"[WARN] rewrite failed for {src_path}: {e}")


def rewrite_files_parallel(all_files, input_dir, output_dir, cache, workers_rewrite):
    if not all_files:
        return

    if workers_rewrite <= 1:
        for src in all_files:
            _rewrite_one_file((src, input_dir, output_dir, cache))
        return

    tasks = [(src, input_dir, output_dir, cache) for src in all_files]
    max_workers = min(workers_rewrite, len(all_files))

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_rewrite_one_file, t) for t in tasks]
        for _ in as_completed(futures):
            pass


# ------------------------------------------------------------
# 6) Failure log
# ------------------------------------------------------------

def save_failures(output_dir, fail_list, origins, target_for_origin, debug_missing):
    out_path = os.path.join(output_dir, "failed_symbolization.tsv")

    reverse_map: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for (orig_elf, offset), _b in origins.items():
        tgt = target_for_origin[(orig_elf, offset)]
        reverse_map[(tgt, offset)].append(orig_elf)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("orig_elf\toffset\tbuild_id\tresolved_target_elf\treason\n")

        for (t_elf, offset, reason) in fail_list:
            for orig_elf in reverse_map.get((t_elf, offset), []):
                b_id = origins[(orig_elf, offset)]
                f.write(f"{orig_elf}\t{offset}\t{b_id or ''}\t{t_elf}\t{reason}\n")

        for (orig_elf, offset), msg in debug_missing.items():
            b_id = origins[(orig_elf, offset)]
            tgt = target_for_origin.get((orig_elf, offset), "")
            f.write(f"{orig_elf}\t{offset}\t{b_id or ''}\t{tgt}\t{msg}\n")

    print(f"[INFO] Failure log saved: {out_path}")


# ------------------------------------------------------------
# 7) SQLite cache for delta symbolization (optional)
# ------------------------------------------------------------

def init_cache_db(db_path: str) -> sqlite3.Connection:
    """
    Initialize (or open) SQLite DB for symbol cache.

    Table:
      symbols(orig_elf TEXT, offset TEXT, func TEXT, loc TEXT,
              PRIMARY KEY(orig_elf, offset))
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS symbols (
            orig_elf TEXT NOT NULL,
            offset   TEXT NOT NULL,
            func     TEXT NOT NULL,
            loc      TEXT NOT NULL,
            PRIMARY KEY(orig_elf, offset)
        )
        """
    )
    conn.commit()
    return conn


def load_cache_for_origins(
    conn: sqlite3.Connection,
    origins: Dict[Tuple[str, str], Optional[str]],
) -> Tuple[Dict[Tuple[str, str], Optional[str]], Dict[Tuple[str, str], Tuple[str, str]]]:
    """
    Given all origins, load any already-known (orig_elf, offset) from DB.

    Returns:
      remaining_origins : subset of origins that still need symbolization
      cache_from_db     : {(orig_elf, offset) -> (func, loc)} for hits
    """
    cur = conn.cursor()
    cache: Dict[Tuple[str, str], Tuple[str, str]] = {}

    # Group offsets by orig_elf
    offsets_by_elf: Dict[str, Set[str]] = defaultdict(set)
    for (elf, off) in origins.keys():
        offsets_by_elf[elf].add(off)

    for elf, _offsets in offsets_by_elf.items():
        cur.execute("SELECT offset, func, loc FROM symbols WHERE orig_elf = ?", (elf,))
        for off, func, loc in cur.fetchall():
            if (elf, off) in origins:
                cache[(elf, off)] = (func, loc)

    remaining: Dict[Tuple[str, str], Optional[str]] = {}
    for key, build_id in origins.items():
        if key not in cache:
            remaining[key] = build_id

    return remaining, cache


def save_new_cache_entries(
    conn: sqlite3.Connection,
    new_cache: Dict[Tuple[str, str], Tuple[str, str]],
) -> None:
    """
    Store newly symbolized entries into SQLite.
    """
    if not new_cache:
        return
    cur = conn.cursor()
    rows = [
        (elf, off, func, loc)
        for (elf, off), (func, loc) in new_cache.items()
    ]
    cur.executemany(
        "INSERT OR IGNORE INTO symbols (orig_elf, offset, func, loc) VALUES (?, ?, ?, ?)",
        rows,
    )


# ------------------------------------------------------------
# main
# ------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Parallel stack symbolizer using llvm-addr2line or GNU addr2line."
    )
    parser.add_argument("--input-dir", required=True, help="Directory containing raw log files")
    parser.add_argument("--output-dir", required=True, help="Directory to store symbolized logs")
    parser.add_argument(
        "--addr2line",
        default=None,
        help="Explicit addr2line binary. "
             "If not set: llvm-addr2line for -llvm, or [cross-prefix]addr2line for -gnu.",
    )
    parser.add_argument(
        "--debug-root",
        default="",
        help=(
            "Base directory for build-id debug files (GNU mode only). "
            "If empty, '.build-id' under the given rootfs is used as the logical base."
        ),
    )
    parser.add_argument(
        "--rootfs",
        default="",
        help="Rootfs prefix for resolving original ELF paths.",
    )
    parser.add_argument(
        "--workers-symbol",
        type=int,
        default=0,
        help="ProcessPool worker count for symbolization. "
             "0 or <=0 => CPU count.",
    )
    parser.add_argument(
        "--workers-rewrite",
        type=int,
        default=0,
        help="ThreadPool worker count for file rewrite. "
             "0 or <=0 => 2×CPU count.",
    )
    parser.add_argument(
        "-c",
        "--cross-prefix",
        default="",
        help="Cross prefix for GNU addr2line, e.g. 'arm-linux-gnueabihf-'. "
             "Used only in -gnu mode if --addr2line is not set.",
    )
    parser.add_argument(
        "--cache-db",
        default="",
        help="Optional SQLite DB path for delta symbolization. "
             "If empty, no persistent cache is used.",
    )
    parser.add_argument(
        "-d",
        "--demangle",
        action="store_true",
        help="Demangle C++ names (pass -C to addr2line). Default: off.",
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Print timing information for major phases. Default: off.",
    )

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "-gnu",
        dest="mode",
        action="store_const",
        const="gnu",
        help="Use GNU addr2line (build-id/debuglink based debug lookup).",
    )
    mode_group.add_argument(
        "-llvm",
        dest="mode",
        action="store_const",
        const="llvm",
        help="Use llvm-addr2line (default).",
    )

    args = parser.parse_args()
    mode = args.mode or "llvm"
    benchmark = args.benchmark

    # Determine addr2line binary
    if args.addr2line:
        addr2line_bin = args.addr2line
    else:
        if mode == "gnu":
            addr2line_bin = (args.cross_prefix + "addr2line") if args.cross_prefix else "addr2line"
        else:
            addr2line_bin = "llvm-addr2line"

    # Configure READ_ELF_BIN based on --cross-prefix
    global READ_ELF_BIN
    if args.cross_prefix:
        READ_ELF_BIN = args.cross_prefix + "readelf"
    else:
        READ_ELF_BIN = "readelf"

    input_dir = os.path.abspath(args.input_dir)
    output_dir = os.path.abspath(args.output_dir)
    # debug_root is a logical path inside the rootfs; if empty, default to ".build-id"
    if args.debug_root:
        debug_root = args.debug_root
    else:
        debug_root = ".build-id"
    rootfs = os.path.abspath(args.rootfs) if args.rootfs else ""
    cache_db_path = os.path.abspath(args.cache_db) if args.cache_db else ""
    
    # Determine default worker counts
    try:
        import multiprocessing
        cpu_count = multiprocessing.cpu_count()
    except Exception:
        cpu_count = 4

    workers_symbol = args.workers_symbol if args.workers_symbol > 0 else cpu_count
    workers_rewrite = args.workers_rewrite if args.workers_rewrite > 0 else max(2, cpu_count * 2)

    print(f"[INFO] mode={mode}, addr2line={addr2line_bin}")
    print(f"[INFO] input_dir={input_dir}")
    print(f"[INFO] output_dir={output_dir}")
    print(f"[INFO] rootfs={rootfs}")
    if mode == "gnu":
        print(f"[INFO] debug_root={debug_root}")
        print(f"[INFO] cross_prefix={args.cross_prefix}")
    if cache_db_path:
        print(f"[INFO] cache_db={cache_db_path} (delta symbolization enabled)")
    else:
        print(f"[INFO] cache_db disabled (no persistent cache)")
    print(f"[INFO] workers_symbol={workers_symbol}, workers_rewrite={workers_rewrite}")

    # Benchmark timers
    t_start = perf_counter()
    t_prev = t_start

    # 1) origins
    print("[INFO] Collecting origins...")
    origins_full, all_files = collect_origins(input_dir)
    print(f"[INFO] {len(origins_full)} unique ELF/offset pairs")
    print(f"[INFO] {len(all_files)} files will be rewritten")
    if benchmark:
        t_now = perf_counter()
        print(f"[BENCH] collect_origins: {t_now - t_prev:.3f}s")
        t_prev = t_now

    # 2) SQLite delta cache (optional)
    conn: Optional[sqlite3.Connection] = None
    cache_from_db: Dict[Tuple[str, str], Tuple[str, str]] = {}
    origins: Dict[Tuple[str, str], Optional[str]]

    if cache_db_path:
        conn = init_cache_db(cache_db_path)
        print("[INFO] Loading cache from SQLite...")
        origins, cache_from_db = load_cache_for_origins(conn, origins_full)
        print(f"[INFO] Cache hits={len(cache_from_db)}, remaining to symbolize={len(origins)}")
        if benchmark:
            t_now = perf_counter()
            print(f"[BENCH] load_cache_from_db: {t_now - t_prev:.3f}s")
            t_prev = t_now
    else:
        origins = origins_full

    # 3) build jobs
    print("[INFO] Building ELF symbolization jobs...")
    jobs_by_target, target_for_origin, debug_missing = build_jobs_by_target(
        origins, mode, debug_root, rootfs
    )
    print(f"[INFO] {len(jobs_by_target)} target ELFs")
    if benchmark:
        t_now = perf_counter()
        print(f"[BENCH] build_jobs_by_target: {t_now - t_prev:.3f}s")
        t_prev = t_now

    # 4) parallel symbolize
    print("[INFO] Symbolizing...")
    target_results, fail_list = symbolize_all_parallel(
        jobs_by_target, addr2line_bin, workers_symbol, args.demangle
    )
    print(f"[INFO] Newly symbolized={len(target_results)}, fails={len(fail_list)}")
    if benchmark:
        t_now = perf_counter()
        print(f"[BENCH] symbolize_all_parallel: {t_now - t_prev:.3f}s")
        t_prev = t_now

    # 5) build in-memory symbol cache for newly symbolized subset
    print("[INFO] Building symbol cache (new results)...")
    cache_new = build_symbol_cache(origins, target_for_origin, target_results)
    print(f"[INFO] New cache entries={len(cache_new)}")
    if benchmark:
        t_now = perf_counter()
        print(f"[BENCH] build_symbol_cache: {t_now - t_prev:.3f}s")
        t_prev = t_now

    # 6) store new cache entries into SQLite (delta)
    if conn is not None:
        print("[INFO] Saving new entries into SQLite cache...")
        save_new_cache_entries(conn, cache_new)
        conn.commit()
        conn.close()
        if benchmark:
            t_now = perf_counter()
            print(f"[BENCH] save_cache_to_db: {t_now - t_prev:.3f}s")
            t_prev = t_now

    # 7) final cache = DB hits + new
    cache: Dict[Tuple[str, str], Tuple[str, str]] = {}
    cache.update(cache_from_db)
    cache.update(cache_new)
    print(f"[INFO] Total in-memory cache size={len(cache)}")

    # 8) rewrite
    print("[INFO] Rewriting files...")
    os.makedirs(output_dir, exist_ok=True)
    rewrite_files_parallel(all_files, input_dir, output_dir, cache, workers_rewrite)
    if benchmark:
        t_now = perf_counter()
        print(f"[BENCH] rewrite_files: {t_now - t_prev:.3f}s")
        t_prev = t_now

    # 9) failure log (only for newly symbolized attempts)
    print("[INFO] Saving failure report...")
    save_failures(output_dir, fail_list, origins, target_for_origin, debug_missing)

    if benchmark:
        t_end = perf_counter()
        print(f"[BENCH] save_failures: {t_end - t_prev:.3f}s")
        print(f"[BENCH] total_time: {t_end - t_start:.3f}s")

    print("[INFO] Done.")


if __name__ == "__main__":
    main()
