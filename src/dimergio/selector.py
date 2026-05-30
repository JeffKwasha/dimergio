from __future__ import annotations

from .model import AnalysisResult, Branch, Candidate


def select_files(result: AnalysisResult) -> list[Candidate] | None:
    candidates = result.candidates
    if not candidates:
        print("\nNo candidate files on slow branches. Nothing to move.")
        return None

    pool = result.pool
    all_on_hdd = sum(1 for c in candidates)
    total_slow_reads = result.total_candidate_reads

    _print_header(result)

    metric = "I"  # default: iowait debt
    current = result

    while True:
        _print_table(current, metric)

        # Summary line
        at80 = current.threshold_80_idx + 1
        cum_at80 = current.candidates[at80 - 1].cum_pct if at80 <= len(current.candidates) else 100.0
        print()
        print(f"  {at80} files cover {cum_at80:.1f}% of IOWait ({current.total_candidate_iowait:.0f} units)")
        print(f"  {all_on_hdd} candidate files on slow branch(es), {total_slow_reads} total reads")

        choice = input("\n  [I]owait  [R]eads  [D]ensity  number=N  [q]uit > ").strip().lower()

        if choice == "q":
            return None
        elif choice == "i":
            metric = "I"
            current = result
            continue
        elif choice == "r":
            metric = "R"
            current = _recompute_display(result, "reads")
            continue
        elif choice == "d":
            metric = "D"
            current = _recompute_display(result, "density")
            continue

        try:
            n = int(choice)
        except ValueError:
            print("  Enter a number, I/R/D for ranking, or q to quit.")
            continue

        if n <= 0:
            print("  Enter a positive number.")
            continue

        selected = current.candidates[:n]
        cum = selected[-1].cum_pct if selected else 0.0
        total_bytes = sum(c.file_size for c in selected)
        total_bytes_disp = _fmt_bytes(total_bytes)

        confirm = input(
            f"\n  Move top {n} files ({cum:.1f}% of IOWait, ~{total_bytes_disp} to copy). Continue? [Y/n]: "
        ).strip().lower()

        if confirm in ("", "y", "yes"):
            return selected
        else:
            print("  Cancelled.")
            continue


def _print_header(result: AnalysisResult) -> None:
    pool = result.pool
    dur = result.observation_duration_s
    dur_str = f"{dur:.0f}s" if dur < 120 else f"{dur / 60:.0f}m {dur % 60:.0f}s"
    print(f"\n=== dimergio \u2014 pool: {pool.name} ({dur_str} observed) ===\n")

    print("Branches:")
    for b in pool.branches:
        rot = "HDD" if b.rotational else "SSD"
        free_disp = _fmt_bytes(b.free_bytes)
        total_disp = _fmt_bytes(b.total_bytes)
        fast = "  [FAST]" if b == pool.fastest_branch else "  [SLOW]"
        drive_str = f"{rot}{fast}"
        print(f"  {b.path.name:<20} {total_disp:>6} / {free_disp:<6}  {b.device:6}  {drive_str}")
    print()


def _print_table(result: AnalysisResult, metric: str) -> None:
    candidates = result.candidates
    if not candidates:
        return

    metric_label = {"I": "IOWait", "R": "Reads", "D": "Density"}.get(metric, "IOWait")

    header = f"  {'#':>4} {'Reads':>7} {'%R':>5} {'IOWait':>8} {'%IO':>5} {'Cum%':>5} {'Size':>8}  File"
    print(header)
    print("  " + "\u2500" * len(header))

    for i, c in enumerate(candidates[: result.threshold_80_idx + 5]):
        row = (
            f"  {i+1:>4} {c.reads:>7} {c.reads / result.total_reads * 100 if result.total_reads else 0:>4.1f}%"
            f" {c.iowait_debt:>8.1f} {c.iowait_pct:>4.1f}% {c.cum_pct:>4.1f}%"
            f" {c.size_display:>8} {c.pool_path}"
        )
        print(row)

    # ... show ellipsis if more
    remaining = len(candidates) - (result.threshold_80_idx + 5)
    if remaining > 0:
        print(f"  {'...':>4} {'':>7} {'':>5} {'':>8} {'':>5} {'':>5} {'':>8}  ({remaining} more)")

    # Totals row
    total_r = result.total_reads
    total_i = result.total_iowait
    print("  " + "\u2500" * len(header))
    print(
        f"  {'':>4} {total_r:>7}  {'':>4} {total_i:>8.1f}"
    )


def _recompute_display(result: AnalysisResult, sort_by: str) -> AnalysisResult:
    """Non-destructive re-sort for display purposes."""
    from .analyze import rank_by_reads, rank_by_density

    if sort_by == "reads":
        return rank_by_reads(result)
    return rank_by_density(result)


def _fmt_bytes(b: int) -> str:
    if b >= 1 << 30:
        return f"{b / (1<<30):.1f}GB"
    if b >= 1 << 20:
        return f"{b / (1<<20):.0f}MB"
    if b >= 1 << 10:
        return f"{b / (1<<10):.0f}KB"
    return f"{b}B"
