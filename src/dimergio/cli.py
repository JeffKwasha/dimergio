from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .analyze import analyze
from .collector import Collector
from .config import load as load_config
from .model import AnalysisResult
from .mover import move_files
from .pool import find_pool
from .selector import select_files
from .state import StateManager

logger = logging.getLogger(__name__)


def _resolve_data_path(args_data: str | None, pool) -> Path:
    from pathlib import Path
    if args_data:
        return Path(args_data)
    cwd = Path.cwd().resolve()
    try:
        cwd.relative_to(pool.mount)
        return cwd
    except ValueError:
        pass
    for branch in pool.branches:
        try:
            rel = cwd.relative_to(branch.path.resolve())
            mapped = pool.mount / rel
            return mapped.resolve()
        except ValueError:
            continue
    return pool.mount


def cmd_watch(args: argparse.Namespace) -> None:
    pool = find_pool(args.pool)
    if pool is None:
        print(f"Error: no mergerfs pool found at '{args.pool}'")
        sys.exit(1)

    data_path = _resolve_data_path(args.data, pool)
    if not data_path.exists():
        print(f"Error: data path '{data_path}' does not exist")
        sys.exit(1)

    logger.info("pool=%s data=%s", pool.mount, data_path)
    for b in pool.branches:
        kind = "HDD" if b.rotational else "SSD"
        logger.info("branch: %s [%s] on %s", b.path, kind, b.device)

    cfg = load_config()
    iowait_ms = args.iowait_interval or cfg.get("iowait_interval_ms", 10)

    collector = Collector(
        pool=pool,
        data_path=data_path,
        process_name=args.process,
        pid=args.pid,
        use_sudo=args.sudo,
        iowait_interval_ms=iowait_ms,
        no_interactive=args.no_interactive,
        verbose=args.verbose,
    )
    accumulators = collector.run()

    if not accumulators:
        print("No read events collected. Nothing to analyze.")
        return

    result = analyze(accumulators, pool, data_path, force_move=collector.force_move)
    _handle_result(result, pool, verify=args.verify, accumulators=accumulators)


def cmd_analyze(args: argparse.Namespace) -> None:
    pool = find_pool(args.pool)
    if pool is None:
        print(f"Error: no mergerfs pool found at '{args.pool}'")
        sys.exit(1)

    data_path = _resolve_data_path(args.data, pool)
    log_path = Path(args.log)

    if not log_path.exists():
        print(f"Error: log file '{log_path}' not found")
        sys.exit(1)

    accumulators = _parse_log(log_path, pool, data_path)
    if not accumulators:
        print("No relevant read events found in log.")
        return

    result = analyze(accumulators, pool, data_path)
    _handle_result(result, pool, verify=args.verify, accumulators=accumulators)


def cmd_status(args: argparse.Namespace) -> None:
    pool_name = Path(args.pool).name.upper()
    state = StateManager(pool_name)
    entries = state.all()

    if not entries:
        print(f"No migrations recorded for pool '{args.pool}'.")
        return

    print(f"\n=== dimergio status \u2014 pool: {pool_name} ===\n")
    print(f"{'File':<50} {'Status':<12} {'Moved':<20} {'Size':>8}")
    print("\u2500" * 92)
    for e in entries:
        status = "verified" if e.verified_working else "pending" if e.verified_working is None else "problem"
        size_str = _fmt_bytes(e.file_size)
        print(f"{e.pool_path:<50} {status:<12} {e.moved_at[:19]:<20} {size_str:>8}")


def cmd_cleanup(args: argparse.Namespace) -> None:
    pool_name = Path(args.pool).name.upper()
    state = StateManager(pool_name)
    cfg = load_config()
    days = cfg.get("cleanup_days", 14)
    entries = state.unverified(older_than_days=days)

    if not entries:
        print(f"No unverified migrations older than {days} days for pool '{args.pool}'.")
        return

    print(f"\n=== dimergio cleanup \u2014 pool: {pool_name} ===\n")
    for e in entries:
        print(f"File: {e.pool_path}")
        print(f"  Orig: {e.source_branch} / {e.original_basename}  \u2192  {e.target_branch}")
        print(f"  Moved: {e.moved_at[:19]}")
        ans = input("  Does your program still work? [Y/n/skip]: ").strip().lower()

        if ans in ("", "y", "yes"):
            branch_path = _find_branch_path(args.pool, e.source_branch)
            if branch_path:
                orig_file = branch_path / e.pool_path
                renamed = orig_file.parent / e.renamed_basename
                if renamed.exists():
                    renamed.unlink()
                    state.mark_verified(e.pool_path, True)
                    print(f"  Deleted original: {renamed}")
                else:
                    print(f"  Original not found (already deleted?): {renamed}")
                    state.mark_verified(e.pool_path, True)
            else:
                print(f"  Could not locate source branch '{e.source_branch}'")
        elif ans == "n":
            state.mark_verified(e.pool_path, False)
            _undo_one(args.pool, e, state)
        else:
            print("  Skipped.")
        print()


def cmd_undo(args: argparse.Namespace) -> None:
    pool_name = Path(args.pool).name.upper()
    state = StateManager(pool_name)
    entries = state.all()

    if not entries:
        print(f"No migrations to undo for pool '{args.pool}'.")
        return

    if not args.all:
        print(f"\n=== dimergio undo \u2014 pool: {pool_name} ===\n")
        for i, e in enumerate(entries, 1):
            print(f"  {i}. {e.pool_path}  (\u2192 {e.target_branch})")
        print()
        picks = input("Enter numbers to undo (e.g. 1,3) or 'all': ").strip()
        if picks.lower() == "all":
            to_undo = list(entries)
        else:
            try:
                indices = [int(x.strip()) - 1 for x in picks.split(",")]
                to_undo = [entries[i] for i in indices if 0 <= i < len(entries)]
            except (ValueError, IndexError):
                print("Invalid selection.")
                return
    else:
        to_undo = list(entries)

    for e in to_undo:
        _undo_one(args.pool, e, state)


def _handle_result(result: AnalysisResult, pool, *, verify: bool = False, accumulators=None) -> None:
    if not result.candidates:
        print("No files on slow branches to move.")
        ans = input("Force move files to a different tier? (downgrade) [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            return
        if accumulators is None:
            print("No accumulator data available for force-move.")
            return
        # Re-analyze with force_move to include all files
        result = analyze(accumulators, pool, force_move=True)
        if not result.candidates:
            print("Still no files to move.")
            return
        print("⚠ Force-move enabled — files may be moved to slower tiers.")

    if result.total_iowait == 0.0:
        from .analyze import rank_by_reads
        result = rank_by_reads(result)
        print("Note: no I/O wait data available (offline log). Ranking by read count.")

    selected = select_files(result)
    if selected is None:
        print("No files selected.")
        return

    cfg = load_config()
    move_files(selected, pool, prefix=cfg.get("prefix", "_dimergio_"), verify=verify)


def _parse_log(log_path: Path, pool, data_path: Path) -> dict:
    from .collector import _LINE_RE
    from .model import FileAccumulator
    import os as _os

    my_uid = _os.getuid()
    accumulators: dict[Path, FileAccumulator] = {}

    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            m = _LINE_RE.match(line)
            if not m:
                continue
            if m.group("event") != "R":
                continue
            if int(m.group("uid")) != my_uid:
                continue
            path = Path(m.group("path"))
            try:
                path.relative_to(data_path)
            except ValueError:
                continue

            branch_idx = 0
            rel = path.relative_to(data_path)
            for idx, branch in enumerate(pool.branches):
                if (branch.path / rel).exists():
                    branch_idx = idx
                    break

            ts = float(m.group("ts"))
            key = path
            if key in accumulators:
                acc = accumulators[key]
            else:
                acc = FileAccumulator(path=key, branch_idx=branch_idx, first_seen=ts)
                accumulators[key] = acc
            acc.observe(ts, 0.0)

    return accumulators


def _find_branch_path(pool_mount: str, branch_label: str) -> Path | None:
    from .pool import find_pool
    pool = find_pool(pool_mount)
    if pool:
        for b in pool.branches:
            if b.label == branch_label:
                return b.path
    return None


def _undo_one(pool_mount: str, entry, state: StateManager) -> None:
    branch_path = _find_branch_path(pool_mount, entry.source_branch)
    fast_path = _find_branch_path(pool_mount, entry.target_branch)
    if not branch_path or not fast_path:
        print(f"  Error: cannot resolve branch paths for '{entry.pool_path}'")
        return

    rel = Path(entry.pool_path)
    renamed = branch_path / rel.parent / entry.renamed_basename
    copy = fast_path / rel

    try:
        if renamed.exists():
            renamed.rename(branch_path / rel)
            print(f"  Restored original: {entry.source_branch}/{entry.pool_path}")
        else:
            print(f"  Original not found: {renamed}")
    except OSError as e:
        print(f"  Error restoring: {e}")
        return

    try:
        if copy.exists():
            copy.unlink()
            print(f"  Removed copy from: {entry.target_branch}/{entry.pool_path}")
    except OSError as e:
        print(f"  Error removing copy: {e}")

    state.remove(entry.pool_path)


def _fmt_bytes(b: int) -> str:
    if b >= 1 << 30:
        return f"{b / (1<<30):.1f}GB"
    if b >= 1 << 20:
        return f"{b / (1<<20):.0f}MB"
    if b >= 1 << 10:
        return f"{b / (1<<10):.0f}KB"
    return f"{b}B"


def build_parser() -> argparse.ArgumentParser:
    from . import __version__

    parser = argparse.ArgumentParser(
        prog="dimergio",
        description="Smart file redistribution for mergerfs pools based on read observation.",
    )
    parser.add_argument("--version", action="version", version=f"dimergio {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    wp = sub.add_parser("watch", help="Run fatrace, collect, analyze, and move files")
    wp.add_argument("--pool", default="/mnt/games", help="Mergerfs pool mount point")
    wp.add_argument("--data", help="Restrict to reads under this path (default: CWD inside pool, else pool root)")
    wp.add_argument("--process",
                    help="Auto-quit when this process exits (matches /proc/<pid>/cmdline)")
    wp.add_argument("--pid", type=int, help="Auto-quit when this PID exits")
    wp.add_argument("--sudo", action="store_true",
                    help="Run fatrace via sudo (needs CAP_SYS_ADMIN for fanotify)")
    wp.add_argument("--iowait-interval", type=int, default=10,
                    help="I/O wait sampling interval in ms (default: 10 = 100Hz)")
    wp.add_argument("--verify", action="store_true",
                    help="SHA256-verify each file after copy")
    wp.add_argument("--no-interactive", action="store_true",
                    help="Disable interactive PID monitor (headless mode)")
    wp.add_argument("--verbose", "-v", action="store_true",
                    help="Log raw fatrace output and parsing decisions to stderr")

    ap = sub.add_parser("analyze", help="Analyze existing fatrace log")
    ap.add_argument("--log", required=True, help="Path to fatrace log file")
    ap.add_argument("--pool", default="/mnt/games", help="Mergerfs pool mount point")
    ap.add_argument("--data", help="Restrict to reads under this path")
    ap.add_argument("--verify", action="store_true",
                    help="SHA256-verify each file after copy")

    sp = sub.add_parser("status", help="Show migration state")
    sp.add_argument("--pool", default="/mnt/games", help="Pool to query")

    cp = sub.add_parser("cleanup", help="Verify old migrations and clean up originals")
    cp.add_argument("--pool", default="/mnt/games", help="Pool to clean up")

    up = sub.add_parser("undo", help="Undo migrations")
    up.add_argument("--pool", default="/mnt/games", help="Pool to undo")
    up.add_argument("--all", action="store_true", help="Undo all migrations")

    return parser
