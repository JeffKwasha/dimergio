from __future__ import annotations

import logging
import os
import re
import select
import subprocess
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .model import Branch, FileAccumulator, PidStat, Pool, ReadEvent

logger = logging.getLogger(__name__)

TS    = r"(?P<ts>\d+\.\d+)"       # timestamp  1780199328.059849
PROC  = r"(?P<proc>\S+)"          # process    wineserver
PID   = r"(?P<pid>\d+)"           # pid        1603752
UID   = r"(?P<uid>\d+)"           # uid        1000
GID   = r"(?P<gid>\d+)"           # gid        1000
EVENT = r"(?P<event>\w+)"         # event      R, RC, W, O
PATH  = r"(?P<path>/.*)"          # path       /mnt/dev/HGST_r1/... (may contain spaces!)
S     = r"\s+"                    # whitespace separator

# fatrace: TIMESTAMP PROC(PID) [UID:GID]: EVENT  /PATH
_LINE_RE = re.compile(
    rf"^{TS}{S}{PROC}\({PID}\){S}\[{UID}:{GID}\]:{S}{EVENT}{S}{PATH}$"
)


class IOWaitSampler:
    """Samples per-device I/O busy-time from /sys/block/*/stat.

    I/O wait in each polling window is divided fairly among the read
    events that occurred during that window, so total iowait_sec across
    all events never exceeds wall-clock time.
    """

    MIN_INTERVAL_MS = 5

    def __init__(self, branches: list[Branch], interval_ms: int = 10):
        self._branches = branches
        self._interval_ms = interval_ms
        self._interval = interval_ms / 1000
        self._latest: dict[int, float] = {}
        self._event_counts: dict[int, int] = defaultdict(int)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def interval_ms(self) -> int:
        return self._interval_ms

    def set_interval_ms(self, ms: int) -> None:
        ms = max(self.MIN_INTERVAL_MS, ms)
        self._interval_ms = ms
        self._interval = ms / 1000

    def record_event(self, branch_idx: int) -> None:
        self._event_counts[branch_idx] += 1

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def get_busy(self, branch_idx: int) -> float:
        return self._latest.get(branch_idx, 0.0)

    def _run(self) -> None:
        prev: dict[int, int] = {}
        for i, branch in enumerate(self._branches):
            try:
                raw = branch.device_stat_path.read_text().split()
                prev[i] = int(raw[9])
            except (OSError, IndexError, ValueError):
                prev[i] = 0

        while not self._stop.is_set():
            self._stop.wait(self._interval)

            counts = {i: self._event_counts.pop(i, 0) for i in range(len(self._branches))}

            for i, branch in enumerate(self._branches):
                try:
                    raw = branch.device_stat_path.read_text().split()
                    curr = int(raw[9])
                except (OSError, IndexError, ValueError):
                    continue

                delta_ms = curr - prev.get(i, curr)
                prev[i] = curr

                cnt = counts.get(i, 0)
                if cnt > 0:
                    self._latest[i] = (delta_ms / 1000.0) / cnt
                else:
                    self._latest[i] = 0.0


class Collector:
    """Runs fatrace, accumulates read events with I/O wait correlation."""

    def __init__(
        self,
        pool: Pool,
        data_path: Path | None = None,
        process_name: str | None = None,
        pid: int | None = None,
        use_sudo: bool = False,
        iowait_interval_ms: int = 10,
        no_interactive: bool = False,
        verbose: bool = False,
    ):
        self.pool = pool
        self.data_path = data_path or pool.mount
        self.process_name = process_name
        self.pid = pid
        self.use_sudo = use_sudo
        self.iowait_interval_ms = iowait_interval_ms
        self._accumulators: dict[Path, FileAccumulator] = {}
        self._pid_stats: dict[int, PidStat] = {}
        self._my_uid = os.getuid()
        self._stop_flag = threading.Event()
        self._branch_for_path: dict[Path, int] = {}
        self._no_interactive = no_interactive
        self._volume_mounts: list[tuple[Path, Path, int]] = []
        self._written_paths: set[Path] = set()
        self._verbose = verbose
        self.force_move = False
        self.move_plans: list = []
        self._build_volume_map()

    def _build_volume_map(self) -> None:
        """Map raw btrfs volume mount paths → branch paths.

        fatrace reports paths through the btrfs volume mount (e.g.
        /mnt/dev/HGST_r1/@/games/…), but the pool/branch uses a
        subvolume mount (e.g. /mnt/@/r1_games/…).  We parse /proc/mounts
        to correlate each branch to its parent volume mount + subvol path.
        """
        mounts: list[tuple[str, Path, str]] = []
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 4 or parts[2] != "btrfs":
                        continue
                    opts = parts[3].split(",")
                    subvol = ""
                    for o in opts:
                        if o.startswith("subvol="):
                            subvol = o[7:]
                            break
                    mounts.append((parts[0], Path(parts[1]), subvol))
        except OSError:
            return

        for idx, branch in enumerate(self.pool.branches):
            branch_dev = None
            branch_subvol = None
            for dev, mp, subvol in mounts:
                if mp == branch.path:
                    branch_dev = dev
                    branch_subvol = subvol
                    break
            if not branch_dev:
                continue
            vol_root = None
            for dev, mp, subvol in mounts:
                if dev == branch_dev and subvol in ("", "/"):
                    vol_root = mp
                    break
            if vol_root is not None and branch_subvol:
                self._volume_mounts.append((vol_root, Path(branch_subvol), idx))

    def _ensure_pid_stat(self, pid: int, process_name: str, ts: float) -> PidStat:
        try:
            return self._pid_stats[pid]
        except KeyError:
            s = PidStat(pid=pid, process_name=process_name, first_seen=ts)
            self._pid_stats[pid] = s
            return s

    def _update_pid_stats(self, event: ReadEvent) -> None:
        s = self._ensure_pid_stat(event.pid, event.process_name, event.timestamp)
        s.read_count += 1
        s.last_seen = event.timestamp
        s.total_iowait_sec += event.iowait_sec
        s.process_name = event.process_name

    def _is_process_alive(self, name: str) -> bool:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                cmdline = Path(f"/proc/{entry}/cmdline").read_bytes()
                if name in cmdline.decode("utf-8", errors="replace"):
                    return True
            except OSError:
                continue
        return False

    def _auto_detect_tracked(self) -> None:
        ranked = sorted(
            self._pid_stats.values(),
            key=lambda s: s.read_count,
            reverse=True,
        )
        total = sum(s.read_count for s in ranked)
        if total == 0:
            return
        cum = 0
        for s in ranked:
            cum += s.read_count
            s.tracked = cum / total <= 0.80 or len([x for x in ranked if x.tracked]) == 0

    def _check_tracked_exited(self) -> bool:
        tracked = [s for s in self._pid_stats.values() if s.tracked]
        if not tracked:
            return False
        for s in tracked:
            try:
                s.exited = not Path(f"/proc/{s.pid}").exists()
            except OSError:
                s.exited = True
        return all(s.exited for s in tracked)

    def run(self) -> dict[Path, FileAccumulator]:
        _FATRACE = "/usr/sbin/fatrace"

        sampler = IOWaitSampler(self.pool.branches, self.iowait_interval_ms)
        sampler.start()

        cmd = [_FATRACE, "-f", "RW", "-u", "-t", "-t"]
        if self.use_sudo:
            if os.geteuid() != 0:
                # Authenticate before TUI so password prompt isn't swallowed
                subprocess.run(["sudo", "-v"])
            cmd = ["sudo"] + cmd

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=str(self.pool.mount),
        )
        assert proc.stdout is not None

        def reader():
            for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if self._verbose:
                    logger.info("fatrace: %s", line)
                event = self._parse_line(line)
                if event is not None:
                    if self._verbose:
                        logger.info("  → event: br=%d pid=%d path=%s", event.branch_idx, event.pid, event.file_path)
                    sampler.record_event(event.branch_idx)
                    self._accumulate(event, sampler)
                elif self._verbose:
                    logger.info("  → dropped")

        read_thread = threading.Thread(target=reader, daemon=True)
        read_thread.start()

        use_interactive = (
            not self.pid
            and not self.process_name
            and not self._no_interactive
            and sys.stdin.isatty()
        )

        try:
            if use_interactive:
                self._run_interactive(proc, read_thread, sampler)
            else:
                self._run_passive(proc, read_thread, sampler)
        finally:
            self._stop_flag.set()
            proc.terminate()
            proc.wait()
            read_thread.join(timeout=5)
            sampler.stop()

        elapsed = self._pid_stats_total_reads()
        nfiles = len(self._accumulators)
        nreads = sum(a.total_reads for a in self._accumulators.values())
        written = len(self._written_paths)
        logger.info("done — %d reads, %d files (%d written)", nreads, nfiles, written)

        return self._accumulators

    def _pid_stats_total_reads(self) -> int:
        return sum(s.read_count for s in self._pid_stats.values())

    def _run_passive(self, proc, read_thread, sampler) -> None:
        start_time = time.time()
        self._stop_flag.clear()

        while not self._stop_flag.is_set():
            if self.pid is not None and not Path(f"/proc/{self.pid}").exists():
                logger.info("PID %d exited", self.pid)
                break
            if self.process_name is not None:
                if not self._is_process_alive(self.process_name):
                    logger.info("process '%s' exited", self.process_name)
                    break
            self._stop_flag.wait(1.0)

    def _run_interactive(self, proc, read_thread, sampler) -> None:
        from rich.box import SIMPLE as _SIMPLE_BOX
        from rich.console import Console, Group
        from rich.live import Live
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        console = Console()

        self._stop_flag.clear()
        start_time = time.time()
        auto_detect_done = False
        auto_detect_at = start_time + 3
        quiesce_start: float | None = None
        re_eval_at = start_time + 30
        auto_quit = False
        nand_warn = True

        mode = "monitor"  # "monitor" or "select"
        monitoring = True
        file_scroll = 0
        file_selected = 0
        file_marks: dict[Path, int] = {}
        confirm_quit = False
        proc_box = [proc]

        branches = self.pool.branches

        def _fmt_duration(secs: float) -> str:
            m, s = divmod(int(secs), 60)
            h, m = divmod(m, 60)
            if h:
                return f"{h}:{m:02d}:{s:02d}"
            return f"{m:02d}:{s:02d}"

        def _rel_path(path: Path) -> str:
            try:
                return str(path.relative_to(self.data_path))
            except ValueError:
                return path.name

        def _branch_color(sc: str) -> str:
            return {"hdd": "blue", "ssd": "teal", "nvme": "green"}.get(sc, "red")

        def _sorted_files():
            return sorted(
                self._accumulators.values(),
                key=lambda a: a.iowait_debt,
                reverse=True,
            )

        def _visible_rows() -> int:
            h = console.size.height
            reserved = 18
            return max(8, min(25, h - reserved))

        def _on_multiple_branches(acc) -> bool:
            from .state import StateManager
            state = StateManager(self.pool.name)
            rel = _rel_path(acc.path)
            for e in state.all():
                if e.pool_path == rel:
                    return True
            return False

        def _calc_select_stats() -> tuple[float, float, dict[int, int]]:
            total_saved = 0.0
            total_time = 0.0
            space: dict[int, int] = {}
            for acc in _sorted_files():
                tidx = file_marks.get(acc.path)
                if tidx is None or tidx >= len(branches):
                    continue
                src_w = branches[acc.branch_idx].speed_weight
                tgt_w = branches[tidx].speed_weight
                total_saved += acc.iowait_debt * (1 - src_w / tgt_w)
                try:
                    sz = acc.path.stat().st_size
                except OSError:
                    sz = 0
                total_time += sz / (tgt_w * 100_000_000)
                space[tidx] = space.get(tidx, 0) + sz
            return total_saved, total_time, space

        def _fmt_bytes(b: int) -> str:
            if b >= 1 << 30:
                return f"{b / (1<<30):.1f}GB"
            if b >= 1 << 20:
                return f"{b / (1<<20):.0f}MB"
            if b >= 1 << 10:
                return f"{b / (1<<10):.0f}KB"
            return f"{b}B"

        # ─── MONITOR layout ─────────────────────────────────────────────
        def _build_monitor(now: float) -> Panel:
            elapsed = _fmt_duration(now - start_time)
            n_active = sum(1 for s in self._pid_stats.values() if not s.exited)
            n_reads = self._pid_stats_total_reads()
            n_files = len(self._accumulators)
            n_writes = len(self._written_paths)

            tracked = [s for s in self._pid_stats.values() if s.tracked]
            tracked_names = ", ".join(f"{s.process_name}({s.pid})" for s in tracked[:5])

            status_text = ""
            status_style = "dim"
            if not self._pid_stats:
                if now - start_time > 3:
                    status_text = "No reads detected — fatrace may need --sudo"
                    status_style = "bold yellow"
                else:
                    status_text = "Waiting for fatrace... (launch app in another terminal)"
            elif not auto_detect_done:
                status_text = "Detecting active PIDs..."
            elif tracked and all(s.exited for s in tracked):
                status_text = "All tracked PIDs exited — press Space to review files."
            elif quiesce_start is not None:
                rem = max(0, 30 - int(now - quiesce_start))
                status_text = f"Quiescing {rem}s — Space to cancel"
                status_style = "bold yellow"
            elif tracked_names:
                status_text = f"tracking: {tracked_names}"

            if not monitoring:
                status_text = "Monitoring paused — press Space to resume, or scroll files below."
                status_style = "bold cyan"

            total_iowait = sum(a.iowait_debt for a in self._accumulators.values())
            ro_iowait = sum(
                a.iowait_debt for p, a in self._accumulators.items()
                if p not in self._written_paths
            )
            iowait_str = f"{total_iowait:.1f}" if ro_iowait == total_iowait else f"{total_iowait:.1f}({ro_iowait:.1f})"
            hz = 1000 / sampler.interval_ms
            header = Text.from_markup(
                f"[bold]since[/bold] {elapsed}  "
                f"[bold]reads[/bold] {n_reads:,}  "
                f"[bold]writes[/bold] {n_writes}  "
                f"[bold]files[/bold] {n_files}  "
                f"[bold]iowait[/bold] {iowait_str}s  "
                f"[bold]active[/bold] {n_active}  "
                f"[bold]sample[/bold] {sampler.interval_ms}ms({hz:.0f}Hz)"
            )

            tier_parts = []
            for i, b in enumerate(branches):
                c = _branch_color(b.speed_class)
                tier_parts.append(f"[{c}]{i}:{b.short_label}({b.speed_weight}x)[/{c}]")
            tiers_line = Text.from_markup(
                f"[bold]Tiers:[/bold]  {'  '.join(tier_parts)}   "
                f"[dim]a:auto-quit:[/dim]{'ON' if auto_quit else 'OFF'}  "
                f"[dim]M:nand:[/dim]{'ON' if nand_warn else 'OFF'}"
            )

            proc_table = Table(show_header=True, header_style="bold", box=_SIMPLE_BOX, expand=True, pad_edge=False)
            proc_table.add_column("PROCESS", width=18, no_wrap=True)
            proc_table.add_column("READS", justify="right", width=10, no_wrap=True)
            proc_table.add_column("WRITES", justify="right", width=10, no_wrap=True)
            proc_table.add_column("IOWAIT(s)", justify="right", width=10, no_wrap=True)
            proc_table.add_column("STATUS", width=7, no_wrap=True)

            ranked_pids = sorted(self._pid_stats.values(), key=lambda s: s.read_count, reverse=True)
            visible_pids = [s for s in ranked_pids if not s.exited]
            if not visible_pids:
                proc_table.add_row("No reads collected yet.", "", "", "", "", style="dim")
            else:
                for s in visible_pids[:10]:
                    status = "[green]run[/green]" if not s.exited else "[red]exited[/red]"
                    proc_table.add_row(s.process_name[:18], f"{s.read_count:,}", str(s.write_count), f"{s.total_iowait_sec:.3f}", status)

            max_vis = _visible_rows()
            sorted_f = _sorted_files()
            scroll_end = min(len(sorted_f), file_scroll + max_vis)
            visible_files = sorted_f[file_scroll:scroll_end]

            file_table = Table(show_header=True, header_style="bold", box=_SIMPLE_BOX, expand=True, pad_edge=False)
            file_table.add_column("#", justify="right", width=4, no_wrap=True)
            file_table.add_column("READS", justify="right", width=10, no_wrap=True)
            file_table.add_column("IOWAIT", justify="right", width=10, no_wrap=True)
            file_table.add_column("BRANCH", width=8, no_wrap=True)
            file_table.add_column("FILE", no_wrap=True, ratio=1)

            if not sorted_f:
                file_table.add_row("", "", "", "", "No files tracked yet.", style="dim")
            else:
                for rank, acc in enumerate(visible_files, file_scroll + 1):
                    br = branches[acc.branch_idx] if acc.branch_idx < len(branches) else branches[0]
                    file_table.add_row(
                        str(rank),
                        f"{acc.total_reads:,}",
                        f"{acc.iowait_debt:.3f}",
                        br.short_label,
                        _rel_path(acc.path),
                    )

            tier_reads = [0] * len(branches)
            tier_iowait = [0.0] * len(branches)
            for acc in self._accumulators.values():
                idx = acc.branch_idx if acc.branch_idx < len(branches) else 0
                tier_reads[idx] += acc.total_reads
                tier_iowait[idx] += acc.iowait_debt

            total_r = sum(tier_reads) or 1
            reads_parts = []
            iowait_parts = []
            for i, b in enumerate(branches):
                c = _branch_color(b.speed_class)
                pct = tier_reads[i] / total_r * 100
                reads_parts.append(f"[{c}]{b.short_label} {tier_reads[i]:,} ({pct:.0f}%)[/{c}]")
            total_io = sum(tier_iowait) or 1.0
            for i, b in enumerate(branches):
                c = _branch_color(b.speed_class)
                pct = tier_iowait[i] / total_io * 100
                iowait_parts.append(f"[{c}]{b.short_label} {tier_iowait[i]:.1f}s ({pct:.0f}%)[/{c}]")

            tier_stats = Text.from_markup(
                f"[bold]Tier reads:[/bold]  {'  '.join(reads_parts)}\n"
                f"[bold]     iowait:[/bold]  {'  '.join(iowait_parts)}"
            )

            status = Text(status_text, style=status_style) if status_text else ""
            sub = "Space:stop/resume  q:quit  s:show-exited  []:sample-rate  a:auto-quit  Enter:review files"

            return Panel(
                Group(header, tiers_line, proc_table, file_table, tier_stats, status),
                title=f"dimergio — {self.pool.mount}  [bold cyan]MONITOR[/bold cyan]",
                subtitle=sub,
                border_style="dim",
            )

        # ─── SELECT layout ──────────────────────────────────────────────
        def _build_select(now: float) -> Panel:
            n_files = len(self._accumulators)
            n_marked = len(file_marks)
            est_saved, est_time, space_req = _calc_select_stats()

            header = Text.from_markup(
                f"[bold]files[/bold] {n_files}  "
                f"[bold]marked[/bold] {n_marked}  "
                f"[bold]est. iowait saved[/bold] {est_saved:.1f}s  "
                f"[bold]est. move time[/bold] {est_time:.1f}s"
            )

            sorted_f = _sorted_files()
            max_vis = _visible_rows()
            scroll_end = min(len(sorted_f), file_scroll + max_vis)
            visible_files = sorted_f[file_scroll:scroll_end]

            file_table = Table(show_header=True, header_style="bold", box=_SIMPLE_BOX, expand=True, pad_edge=False)
            file_table.add_column("#", justify="right", width=4, no_wrap=True)
            file_table.add_column("READS", justify="right", width=10, no_wrap=True)
            file_table.add_column("IOWAIT", justify="right", width=10, no_wrap=True)
            file_table.add_column("FROM", width=8, no_wrap=True)
            file_table.add_column("TO", width=8, no_wrap=True)
            file_table.add_column("FILE", no_wrap=True, ratio=1)

            if not sorted_f:
                file_table.add_row("", "", "", "", "", "No files tracked.", style="dim")
            else:
                for rank, acc in enumerate(visible_files, file_scroll + 1):
                    br = branches[acc.branch_idx] if acc.branch_idx < len(branches) else branches[0]
                    from_label = br.short_label
                    if _on_multiple_branches(acc):
                        from_label += "\u2026"
                    from_c = _branch_color(br.speed_class)

                    tidx = file_marks.get(acc.path)
                    if tidx is not None and tidx < len(branches):
                        tgt = branches[tidx]
                        to_label = tgt.short_label
                        to_c = _branch_color(tgt.speed_class)
                        to_cell = f"[{to_c}]{to_label}[/{to_c}]"
                    else:
                        to_cell = "[dim]-[/dim]"

                    is_selected = (file_scroll + (rank - file_scroll - 1) == file_selected) if visible_files else False
                    row_style = "reverse" if (rank - 1 == file_selected) else ""

                    file_table.add_row(
                        str(rank),
                        f"{acc.total_reads:,}",
                        f"{acc.iowait_debt:.3f}",
                        f"[{from_c}]{from_label}[/{from_c}]",
                        to_cell,
                        _rel_path(acc.path),
                        style=row_style,
                    )

            legend_parts = []
            for i, b in enumerate(branches):
                c = _branch_color(b.speed_class)
                legend_parts.append(f"[{c}]{i}:{b.short_label}({b.speed_weight}x)[/{c}]")
            legend = Text.from_markup(f"[bold]Branches:[/bold]  {'  '.join(legend_parts)}")

            space_parts = []
            for i, b in enumerate(branches):
                c = _branch_color(b.speed_class)
                sz = space_req.get(i, 0)
                space_parts.append(f"[{c}]{b.short_label} {_fmt_bytes(sz)}[/{c}]")
            space_line = Text.from_markup(f"[bold]Space required:[/bold]  {'  '.join(space_parts)}")

            if confirm_quit:
                status = Text("Quit without moving? Press Y to confirm, any other key to cancel.", style="bold yellow")
            else:
                status = Text("↑↓:scroll  0-9:mark branch  Shift+0-9:mark above  -:clear  Enter:move  q:quit", style="dim")

            return Panel(
                Group(header, file_table, legend, space_line, status),
                title=f"dimergio — {self.pool.mount}  [bold green]SELECT[/bold green]",
                subtitle="↑↓:scroll  PgUp/PgDn/Home/End  0-9:mark  Enter:move  q:quit",
                border_style="dim",
            )

        # ─── Key handling ───────────────────────────────────────────────
        _SHIFT_MAP = {")": 0, "!": 1, "@": 2, "#": 3, "$": 4, "%": 5, "^": 6, "&": 7, "*": 8, "(": 9}

        def _handle_key(key: str) -> bool:
            nonlocal mode, monitoring, file_scroll, file_selected, confirm_quit
            nonlocal auto_quit, nand_warn, quiesce_start, auto_detect_done

            if confirm_quit:
                if key in ("y", "Y", "\r", "\n"):
                    return True
                confirm_quit = False
                return False

            if mode == "monitor":
                return _handle_monitor_key(key)
            else:
                return _handle_select_key(key)

        def _handle_monitor_key(key: str) -> bool:
            nonlocal mode, monitoring, auto_quit, nand_warn

            if key == "q":
                return True
            elif key == " ":
                if monitoring:
                    monitoring = False
                    proc_box[0].terminate()
                    proc_box[0].wait()
                    read_thread.join(timeout=5)
                    sampler.stop()
                    mode = "select"
                else:
                    monitoring = True
                    sampler.start()
                    cmd = ["/usr/sbin/fatrace", "-f", "RW", "-u", "-t", "-t"]
                    if self.use_sudo:
                        cmd = ["sudo"] + cmd
                    proc_box[0] = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=str(self.pool.mount))
                    assert proc_box[0].stdout is not None
                return False
            elif key == "\x1b[A":
                pass
            elif key == "\x1b[B":
                pass
            elif key == "s":
                pass
            elif key == "[":
                sampler.set_interval_ms(sampler.interval_ms - 5)
            elif key == "]":
                sampler.set_interval_ms(sampler.interval_ms + 5)
            elif key == "a":
                auto_quit = not auto_quit
                if auto_quit and self._pid_stats:
                    self._auto_detect_tracked()
                    auto_detect_done = True
            elif key == "M":
                nand_warn = not nand_warn
            elif key == "\r" or key == "\n":
                if monitoring:
                    monitoring = False
                    proc.terminate()
                    proc.wait()
                    read_thread.join(timeout=5)
                    sampler.stop()
                mode = "select"
            return False

        def _handle_select_key(key: str) -> bool:
            nonlocal mode, monitoring, file_scroll, file_selected, confirm_quit
            sorted_f = _sorted_files()
            max_vis = _visible_rows()

            if key == "q":
                if file_marks:
                    confirm_quit = True
                else:
                    return True
            elif key == " ":
                mode = "monitor"
                monitoring = True
                sampler.start()
                cmd = ["/usr/sbin/fatrace", "-f", "RW", "-u", "-t", "-t"]
                if self.use_sudo:
                    cmd = ["sudo"] + cmd
                proc_box[0] = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=str(self.pool.mount))
                assert proc_box[0].stdout is not None
            elif key == "\x1b[A":
                file_selected = max(0, file_selected - 1)
                if file_selected < file_scroll:
                    file_scroll = file_selected
            elif key == "\x1b[B":
                file_selected = min(len(sorted_f) - 1, file_selected + 1)
                if file_selected >= file_scroll + max_vis:
                    file_scroll = file_selected - max_vis + 1
            elif key == "\x1b[5~":
                file_scroll = max(0, file_scroll - max_vis)
                file_selected = min(file_selected, file_scroll)
            elif key == "\x1b[6~":
                file_scroll = min(max(0, len(sorted_f) - max_vis), file_scroll + max_vis)
                file_selected = max(file_selected, file_scroll)
            elif key == "\x1b[H":
                file_scroll = 0
                file_selected = 0
            elif key == "\x1b[F":
                file_selected = max(0, len(sorted_f) - 1)
                file_scroll = max(0, len(sorted_f) - max_vis)
            elif key in _SHIFT_MAP:
                br_idx = _SHIFT_MAP[key]
                if br_idx < len(branches) and file_selected < len(sorted_f):
                    for i in range(file_selected + 1):
                        acc = sorted_f[i]
                        if acc.write_count == 0:
                            file_marks[acc.path] = br_idx
            elif key.isdigit():
                br_idx = int(key)
                if br_idx < len(branches) and file_selected < len(sorted_f):
                    acc = sorted_f[file_selected]
                    if file_marks.get(acc.path) == br_idx:
                        del file_marks[acc.path]
                    else:
                        file_marks[acc.path] = br_idx
            elif key == "-":
                if file_selected < len(sorted_f):
                    acc = sorted_f[file_selected]
                    file_marks.pop(acc.path, None)
            elif key == "\r" or key == "\n":
                self.move_plans = []
                for acc in sorted_f:
                    tidx = file_marks.get(acc.path)
                    if tidx is None or tidx >= len(branches):
                        continue
                    from .model import MovePlan
                    self.move_plans.append(MovePlan(
                        file=acc,
                        target_branch_idx=tidx,
                        is_rename_only=False,
                    ))
                return True
            return False

        # ─── Keyboard reader thread ─────────────────────────────────────
        import termios, queue

        fd = sys.stdin.fileno()
        old_attr = termios.tcgetattr(fd)
        raw_attr = list(old_attr)
        raw_attr[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN)
        raw_attr[0] &= ~(termios.ICRNL | termios.IXON)
        raw_attr[6][termios.VMIN] = 1
        raw_attr[6][termios.VTIME] = 0

        def _read_key(timeout: float = 0.15) -> str | None:
            if select.select([sys.stdin], [], [], timeout)[0]:
                try:
                    ch = sys.stdin.read(1)
                    if ch == "\x1b":
                        nxt = sys.stdin.read(2) if select.select([sys.stdin], [], [], 0.02)[0] else ""
                        return ch + nxt
                    return ch
                except (OSError, ValueError):
                    return None
            return None

        _key_q: queue.SimpleQueue[str | None] = queue.SimpleQueue()
        _stop_reader = threading.Event()

        def _key_reader() -> None:
            termios.tcsetattr(fd, termios.TCSADRAIN, raw_attr)
            try:
                while not _stop_reader.is_set():
                    _key_q.put(_read_key(0.1))
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)

        t = threading.Thread(target=_key_reader, daemon=True)
        t.start()

        def _get_layout() -> Panel:
            now = time.time()
            if mode == "monitor":
                return _build_monitor(now)
            return _build_select(now)

        try:
            with Live(console=console, screen=True, refresh_per_second=4, get_renderable=_get_layout) as live:
                while not self._stop_flag.is_set():
                    now = time.time()

                    try:
                        key = _key_q.get(timeout=0.15)
                    except queue.Empty:
                        key = None
                    if key and _handle_key(key):
                        break

                    if mode == "monitor" and monitoring:
                        if not auto_detect_done and now >= auto_detect_at and self._pid_stats:
                            self._auto_detect_tracked()
                            auto_detect_done = True

                        if auto_detect_done and now >= re_eval_at:
                            self._auto_detect_tracked()
                            re_eval_at = now + 30

                        if auto_quit and self._check_tracked_exited():
                            if quiesce_start is None:
                                quiesce_start = now
                            elif now - quiesce_start >= 30:
                                break
                        else:
                            if quiesce_start is not None:
                                quiesce_start = None
        finally:
            _stop_reader.set()
            t.join(timeout=2)
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)

        from .stats import load_stats, merge_stats, save_stats
        existing = load_stats(self.pool)
        merged = merge_stats(existing, self._accumulators, self.data_path)
        save_stats(self.pool, merged)

    def _remap_volume_path(self, path: Path) -> Path | None:
        """Convert a raw btrfs volume path to a pool-relative path.

        fatrace reports /mnt/dev/HGST_r1/@/games/file — remap to
        /mnt/games/file  (under self.data_path).
        """
        for vol_root, subvol_rel, _ in self._volume_mounts:
            try:
                rel = subvol_rel.relative_to(Path("/"))
            except ValueError:
                continue
            prefix = vol_root / rel
            try:
                pool_rel = path.relative_to(prefix)
            except ValueError:
                continue
            return self.data_path / pool_rel
        return None

    def _parse_line(self, line: str) -> ReadEvent | None:
        m = _LINE_RE.match(line)
        if not m:
            if self._verbose:
                logger.info("  parse: regex no match on: %s", line[:120])
            return None
        event_type = m.group("event")
        uid = int(m.group("uid"))
        path_str = m.group("path")

        if not self.use_sudo and uid != self._my_uid:
            if self._verbose:
                logger.info("  parse: uid=%d != my_uid=%d path=%s", uid, self._my_uid, path_str[:80])
            return None

        file_path = Path(path_str)
        if not self._in_data_path(file_path):
            remapped = self._remap_volume_path(file_path)
            if remapped is None:
                if self._verbose:
                    logger.info("  parse: not in data_path and no volume remap: %s", path_str[:80])
                return None
            if self._verbose:
                logger.info("  parse: remapped %s → %s", path_str[:80], remapped)
            file_path = remapped

        # Mark files that have ever been written — they're ineligible for move
        if "W" in event_type:
            self._written_paths.add(file_path)
            if self._verbose:
                logger.info("  parse: W in event=%s → marked written: %s", event_type, file_path)
            # Track write count for this PID
            ts = float(m.group("ts"))
            pid = int(m.group("pid"))
            proc = m.group("proc")
            s = self._ensure_pid_stat(pid, proc, ts)
            s.write_count += 1
            s.last_seen = ts
            s.process_name = proc
            # Track write count for this file
            branch_idx = self._resolve_branch(file_path)
            try:
                acc = self._accumulators[file_path]
            except KeyError:
                acc = FileAccumulator(
                    path=file_path,
                    branch_idx=branch_idx,
                    first_seen=ts,
                )
                self._accumulators[file_path] = acc
            acc.write_count += 1
            acc.last_seen = ts

        if event_type[0] != "R":
            return None

        ts = float(m.group("ts"))
        pid = int(m.group("pid"))
        proc = m.group("proc")

        branch_idx = self._resolve_branch(file_path)
        return ReadEvent(
            file_path=file_path,
            pid=pid,
            process_name=proc,
            uid=uid,
            gid=int(m.group("gid")),
            timestamp=ts,
            branch_idx=branch_idx,
            iowait_sec=0.0,
        )

    def _accumulate(self, event: ReadEvent, sampler: IOWaitSampler) -> None:
        iowait = sampler.get_busy(event.branch_idx)
        event.iowait_sec = iowait

        self._update_pid_stats(event)

        key = event.file_path
        try:
            acc = self._accumulators[key]
        except KeyError:
            acc = FileAccumulator(
                path=key,
                branch_idx=event.branch_idx,
                first_seen=event.timestamp,
            )
            self._accumulators[key] = acc
        acc.observe(event.timestamp, iowait)

    def _in_data_path(self, path: Path) -> bool:
        try:
            path.relative_to(self.data_path)
            return True
        except ValueError:
            return False

    def _resolve_branch(self, path: Path) -> int:
        try:
            return self._branch_for_path[path]
        except KeyError:
            pass

        try:
            rel = path.relative_to(self.data_path)
        except ValueError:
            self._branch_for_path[path] = 0
            return 0

        for idx, branch in enumerate(self.pool.branches):
            candidate = branch.path / rel
            if candidate.exists():
                self._branch_for_path[path] = idx
                return idx

        for idx, branch in enumerate(self.pool.branches):
            candidate = branch.path / rel
            if candidate.exists():
                self._branch_for_path[path] = idx
                return idx

        self._branch_for_path[path] = 0
        return 0
