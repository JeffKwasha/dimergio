from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .collector import Collector
from .config import load as load_config
from .mover import execute_move_plan
from .pool import PoolContext, find_pool, find_pool_for_cwd
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


def _get_default_pool() -> str | None:
    """Get the pool path auto-detected from CWD, or None if not found."""
    pool = find_pool_for_cwd()
    if pool:
        return str(pool.mount)
    return None


def cmd_watch(args: argparse.Namespace) -> None:
    ctx = PoolContext(args.pool)
    pool = ctx.pool

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

    if args.offline:
        if args.from_log:
            log_path = Path(args.from_log)
            if not log_path.exists():
                print(f"Error: log file '{log_path}' not found")
                sys.exit(1)
            accumulators = _parse_log(log_path, pool, data_path)
            if not accumulators:
                print("No relevant read events found in log.")
                return
        else:
            from .stats import load_accumulators
            accumulators = load_accumulators(pool, data_path)
            if not accumulators:
                print("No saved stats found. Run a watch session first.")
                return

        collector = Collector(
            pool=pool,
            data_path=data_path,
            no_interactive=args.no_interactive,
            preloaded=accumulators,
        )
        collector.run()

        if collector.move_plans:
            cfg = load_config()
            summary = execute_move_plan(collector.move_plans, pool, prefix=cfg.get("prefix", "_dimergio_"), verify=args.verify)
            _, moved = _print_results(summary, pool)
            _offer_free_originals(pool, cfg.get("prefix", "_dimergio_"), moved)
    else:
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

        if collector.move_plans:
            cfg = load_config()
            summary = execute_move_plan(collector.move_plans, pool, prefix=cfg.get("prefix", "_dimergio_"), verify=args.verify)
            _, moved = _print_results(summary, pool)
            _offer_free_originals(pool, cfg.get("prefix", "_dimergio_"), moved)

        if accumulators:
            from .stats import load_stats, merge_stats, save_stats
            existing = load_stats(pool)
            merged = merge_stats(existing, accumulators, data_path)
            save_stats(pool, merged)


def cmd_status(args: argparse.Namespace) -> None:
    ctx = PoolContext(args.pool)
    state = ctx.state
    entries = state.all()

    if not entries:
        print(f"No migrations recorded for pool '{ctx.name}'.")
        return

    print(f"\n=== dimergio status \u2014 pool: {ctx.name} ===\n")
    from rich.console import Console
    from rich.table import Table

    status_color = {"verified": "green", "pending": "yellow", "problem": "red"}
    table = Table(show_header=True, header_style="bold", expand=False)
    table.add_column("File", ratio=1, no_wrap=True)
    table.add_column("Status", width=10, no_wrap=True)
    table.add_column("Moved", width=21, no_wrap=True)
    table.add_column("Size", justify="right", width=10, no_wrap=True)

    for e in entries:
        if e.verified_working is True:
            status, sc = "verified", "green"
        elif e.verified_working is False:
            status, sc = "problem", "red"
        else:
            status, sc = "pending", "yellow"
        table.add_row(
            e.pool_path,
            f"[{sc}]{status}[/{sc}]",
            e.moved_at[:19],
            _fmt_bytes(e.file_size),
        )

    Console().print(table)


def cmd_cleanup(args: argparse.Namespace) -> None:
    ctx = PoolContext(args.pool)
    state = ctx.state
    days = args.days if args.days is not None else 7
    entries = state.unverified(older_than_days=days)

    if not entries:
        print(f"No unverified migrations older than {days} days for pool '{ctx.name}'.")
        return

    print(f"\n=== dimergio cleanup \u2014 pool: {ctx.name} ===\n")
    for e in entries:
        print(f"File: {e.pool_path}")
        print(f"  Orig: {e.source_branch} / {e.original_basename}  \u2192  {e.target_branch}")
        print(f"  Moved: {e.moved_at[:19]}")

        if args.all:
            ans = "y"
        else:
            ans = input("  Does your program still work? [Y/n/skip]: ").strip().lower()

        if ans in ("", "y", "yes"):
            branch_path = ctx.branch_path(e.source_branch)
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
            _undo_one(ctx, e, state)
        else:
            print("  Skipped.")
        print()


def cmd_undo(args: argparse.Namespace) -> None:
    ctx = PoolContext(args.pool)
    state = ctx.state
    entries = state.all()

    if not entries:
        print(f"No migrations to undo for pool '{ctx.name}'.")
        return

    if not args.all:
        print(f"\n=== dimergio undo \u2014 pool: {ctx.name} ===\n")
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
        _undo_one(ctx, e, state)


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


def _undo_one(ctx: PoolContext, entry, state: StateManager) -> None:
    branch_path = ctx.branch_path(entry.source_branch)
    fast_path = ctx.branch_path(entry.target_branch)
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


def _branch_speed_class(pool, label: str) -> str:
    for b in pool.branches:
        if b.label == label:
            return b.speed_class
    return "hdd"


def _print_results(summary, pool) -> tuple[int, list[str]]:
    """Render the post-move operations list, color-coded by destination branch.
    Returns (succeeded, moved_pool_paths) for the operations that succeeded."""
    succeeded, failed, _failed_list, operations, total_bytes = summary
    moved_paths: list[str] = []
    if not operations:
        return succeeded, moved_paths
    from rich.console import Console
    from rich.table import Table

    color = {"hdd": "blue", "ssd": "teal", "nvme": "green"}
    console = Console()
    table = Table(title="Move operations", show_header=True, header_style="bold", expand=False)
    table.add_column("#", justify="right", width=4)
    table.add_column("FROM", width=10, no_wrap=True)
    table.add_column("TO", width=10, no_wrap=True)
    table.add_column("FILE", ratio=1, no_wrap=True)
    table.add_column("COPIED", justify="right", width=10, no_wrap=True)

    for i, op in enumerate(operations, 1):
        sc = color.get(_branch_speed_class(pool, op["src"]), "red")
        dc = color.get(_branch_speed_class(pool, op["dst"]), "red")
        if op["ok"]:
            table.add_row(
                str(i),
                f"[{sc}]{op['src']}[/{sc}]",
                f"[{dc}]{op['dst']}[/{dc}]",
                op["pool_path"],
                _fmt_bytes(op["bytes"]),
            )
            moved_paths.append(op["pool_path"])
        else:
            table.add_row(str(i), op["src"], op["dst"], f"[red]{op['pool_path']}[/red]", "[red]FAILED[/red]")

    console.print(table)
    unit = "GB" if total_bytes > 10 * (1 << 30) else "MB"
    console.print(f"[bold]Total copied:[/bold] {_fmt_bytes(total_bytes)} ({unit})  —  {succeeded} moved, {failed} failed")
    return succeeded, moved_paths


def _offer_free_originals(pool, prefix: str, moved_paths: list[str]) -> None:
    """After moves, offer to delete the now-redundant renamed originals
    for the files moved in this session only."""
    if not moved_paths:
        return
    ctx = PoolContext(pool)
    state = ctx.state
    entries = {e.pool_path: e for e in state.all()}
    if not entries:
        return

    to_free = []
    total = 0
    for pool_path in moved_paths:
        e = entries.get(pool_path)
        if e is None:
            continue
        branch_path = ctx.branch_path(e.source_branch)
        if not branch_path:
            continue
        renamed = branch_path / Path(e.pool_path).parent / e.renamed_basename
        if renamed.exists():
            to_free.append((e, renamed))
            try:
                total += renamed.stat().st_size
            except OSError:
                pass

    if not to_free:
        return

    ans = input(f"\nFree {len(to_free)} redundant original file(s) ({_fmt_bytes(total)})? [y/N]: ").strip().lower()
    if ans in ("y", "yes"):
        for e, renamed in to_free:
            try:
                renamed.unlink()
                state.remove(e.pool_path)
                print(f"  Freed: {e.source_branch}/{e.pool_path}")
            except OSError as ex:
                print(f"  Error freeing {renamed}: {ex}")
    else:
        print("  Kept originals. Run 'dimergio cleanup' to verify & free later, or 'dimergio undo' to revert.")


def build_parser() -> argparse.ArgumentParser:
    from . import __version__

    default_pool = _get_default_pool()

    parser = argparse.ArgumentParser(
        prog="dimergio",
        description="Smart file redistribution for mergerfs pools based on read observation.",
    )
    parser.add_argument("--version", action="version", version=f"dimergio {__version__}")
    sub = parser.add_subparsers(dest="command")

    pool_help = "Mergerfs pool mount point"
    if default_pool:
        pool_help += f" (default: auto-detected '{default_pool}')"

    wp = sub.add_parser("watch", help="Run fatrace, collect, analyze, and move files")
    wp.add_argument("--pool", default=default_pool, help=pool_help)
    wp.add_argument("--data", help="Restrict to reads under this path (default: CWD inside pool, else pool root)")
    wp.add_argument("--process",
                    help="Auto-quit when this process exits (matches /proc/<pid>/cmdline)")
    wp.add_argument("--pid", type=int, help="Auto-quit when this PID exits")
    wp.add_argument("--no-sudo", dest="sudo", action="store_false",
                    help="Run fatrace without sudo (requires CAP_SYS_ADMIN on fatrace binary)")
    wp.add_argument("--iowait-interval", type=int, default=10,
                    help="I/O wait sampling interval in ms (default: 10 = 100Hz)")
    wp.add_argument("--verify", action="store_true",
                    help="SHA256-verify each file after copy")
    wp.add_argument("--no-interactive", action="store_true",
                    help="Disable interactive PID monitor (headless mode)")
    wp.add_argument("--verbose", "-v", action="store_true",
                    help="Log raw fatrace output and parsing decisions to stderr")
    wp.add_argument("--offline", action="store_true",
                    help="Use saved stats instead of live fatrace collection")
    wp.add_argument("--from-log", dest="from_log",
                    help="Path to fatrace log file (with --offline)")

    sp = sub.add_parser("status", help="Show migration state")
    sp.add_argument("--pool", default=default_pool, help=pool_help)

    cp = sub.add_parser("cleanup", help="Verify old migrations and clean up originals")
    cp.add_argument("--pool", default=default_pool, help=pool_help)
    cp.add_argument("-a", "--all", action="store_true", help="Clean all eligible migrations without prompting")
    cp.add_argument("-d", "--days", nargs="?", type=int, const=0, default=None,
                    help="Only consider migrations older than N days (default: 7; with -d but no number: 0)")

    up = sub.add_parser("undo", help="Undo migrations")
    up.add_argument("--pool", default=default_pool, help=pool_help)
    up.add_argument("--all", action="store_true", help="Undo all migrations")

    parser.set_defaults(
        sudo=True,
        pool=default_pool,
        data=None,
        process=None,
        pid=None,
        iowait_interval=10,
        verify=False,
        no_interactive=False,
        verbose=False,
        offline=False,
        from_log=None,
    )
    return parser