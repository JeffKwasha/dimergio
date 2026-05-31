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
        show_exited = False
        selected_idx = 0
        auto_quit = False
        nand_warn = True

        def _fmt_duration(secs: float) -> str:
            m, s = divmod(int(secs), 60)
            h, m = divmod(m, 60)
            if h:
                return f"{h}:{m:02d}:{s:02d}"
            return f"{m:02d}:{s:02d}"

        def _build_layout(now: float) -> Panel:
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
                    status_text = "No reads detected — fatrace may need --sudo (fanotify requires CAP_SYS_ADMIN)"
                    status_style = "bold yellow"
                else:
                    status_text = "Waiting for fatrace... (launch app in another terminal)"
            elif not auto_detect_done:
                status_text = "Detecting active PIDs..."
            elif tracked and all(s.exited for s in tracked):
                status_text = "All tracked PIDs exited — stopping."
            elif quiesce_start is not None:
                rem = max(0, 30 - int(now - quiesce_start))
                status_text = f"Quiescing {rem}s — Space to cancel"
                status_style = "bold yellow"
            elif tracked_names:
                status_text = f"tracking: {tracked_names}"

            total_iowait = sum(a.iowait_debt for a in self._accumulators.values())
            ro_iowait = sum(
                a.iowait_debt
                for p, a in self._accumulators.items()
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

            branches = self.pool.branches
            tier_parts = []
            for i, b in enumerate(branches):
                tier_parts.append(f"{i}:{b.short_label}({b.speed_weight}x)")
            tiers_line = Text.from_markup(
                f"[bold]Tiers:[/bold]  {'  '.join(tier_parts)}   "
                f"[dim]a:auto-quit:[/dim]{'ON' if auto_quit else 'OFF'}  "
                f"[dim]M:nand:[/dim]{'ON' if nand_warn else 'OFF'}"
            )

            watching_parts = []
            for b in branches:
                watching_parts.append(str(b.path))
            watching_line = Text.from_markup(
                f"[bold]Watching:[/bold]  {'  '.join(watching_parts)}"
            )

            # --- Process table ---
            proc_table = Table(
                show_header=True,
                header_style="bold",
                box=_SIMPLE_BOX,
                expand=True,
                pad_edge=False,
            )
            proc_table.add_column("PROCESS", width=18, no_wrap=True)
            proc_table.add_column("READS", justify="right", width=10, no_wrap=True)
            proc_table.add_column("WRITES", justify="right", width=10, no_wrap=True)
            proc_table.add_column("IOWAIT(s)", justify="right", width=10, no_wrap=True)
            proc_table.add_column("STATUS", width=7, no_wrap=True)

            ranked_pids = sorted(
                self._pid_stats.values(),
                key=lambda s: s.read_count,
                reverse=True,
            )
            visible_pids = [s for s in ranked_pids if show_exited or not s.exited]

            if not visible_pids:
                proc_table.add_row("No reads collected yet.", "", "", "", "", style="dim")
            else:
                for idx, s in enumerate(visible_pids[:10]):
                    name = s.process_name[:18]
                    reads = f"{s.read_count:,}"
                    iowait = f"{s.total_iowait_sec:.3f}"
                    if s.exited:
                        status = "[red]exited[/red]"
                    elif s.tracked:
                        status = "[green]run[/green]"
                    else:
                        status = "[dim]run[/dim]"
                    proc_table.add_row(name, reads, str(s.write_count), iowait, status)

            # --- File table ---
            file_table = Table(
                show_header=True,
                header_style="bold",
                box=_SIMPLE_BOX,
                expand=True,
                pad_edge=False,
            )
            file_table.add_column("#", justify="right", width=4, no_wrap=True)
            file_table.add_column("READS", justify="right", width=10, no_wrap=True)
            file_table.add_column("WRITES", justify="right", width=10, no_wrap=True)
            file_table.add_column("IOWAIT", justify="right", width=10, no_wrap=True)
            file_table.add_column("BRANCH", width=8, no_wrap=True)
            file_table.add_column("FILE", no_wrap=True)

            sorted_files = sorted(
                self._accumulators.values(),
                key=lambda a: a.iowait_debt,
                reverse=True,
            )
            if not sorted_files:
                file_table.add_row("", "", "", "", "", "No files tracked yet.", style="dim")
            else:
                for rank, acc in enumerate(sorted_files[:15], 1):
                    br = branches[acc.branch_idx] if acc.branch_idx < len(branches) else branches[0]
                    file_table.add_row(
                        str(rank),
                        f"{acc.total_reads:,}",
                        str(acc.write_count),
                        f"{acc.iowait_debt:.3f}",
                        br.short_label,
                        str(acc.path),
                    )

            # --- Tier stats ---
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
                pct = tier_reads[i] / total_r * 100
                reads_parts.append(f"{b.short_label} {tier_reads[i]:,} ({pct:.0f}%)")
            total_io = sum(tier_iowait) or 1.0
            for i, b in enumerate(branches):
                pct = tier_iowait[i] / total_io * 100
                iowait_parts.append(f"{b.short_label} {tier_iowait[i]:.1f}s ({pct:.0f}%)")

            tier_stats = Text.from_markup(
                f"[bold]Tier reads:[/bold]  {'  '.join(reads_parts)}\n"
                f"[bold]     iowait:[/bold]  {'  '.join(iowait_parts)}"
            )

            status = Text(status_text, style=status_style) if status_text else ""

            return Panel(
                Group(header, tiers_line, watching_line, proc_table, file_table, tier_stats, status),
                title=f"dimergio — {self.pool.mount}",
                subtitle="q:quit  ↑↓:select  Space:toggle  s:show-exited  []:sample-rate",
                border_style="dim",
            )

            return Panel(
                Group(header, table, status),
                title=f"dimergio — {self.pool.mount}",
                subtitle="q:quit  ↑↓:select  Space:toggle  s:show-exited  []:sample-rate",
                border_style="dim",
            )

        def _handle_key(key: str) -> bool:
            nonlocal selected_idx, show_exited, quiesce_start, auto_detect_done, auto_quit, nand_warn
            if key == "q":
                return True
            elif key == "\x1b[A":
                selected_idx = max(0, selected_idx - 1)
            elif key == "\x1b[B":
                ranked = sorted(
                    self._pid_stats.values(),
                    key=lambda s: s.read_count,
                    reverse=True,
                )
                visible = [s for s in ranked if show_exited or not s.exited]
                selected_idx = min(len(visible) - 1, selected_idx + 1) if visible else 0
            elif key == " ":
                ranked = sorted(
                    self._pid_stats.values(),
                    key=lambda s: s.read_count,
                    reverse=True,
                )
                visible = [s for s in ranked if show_exited or not s.exited]
                if 0 <= selected_idx < len(visible):
                    visible[selected_idx].tracked = not visible[selected_idx].tracked
                    auto_detect_done = True
                    quiesce_start = None
            elif key == "s":
                show_exited = not show_exited
                selected_idx = 0
            elif key == "[":
                sampler.set_interval_ms(sampler.interval_ms - 5)
            elif key == "]":
                sampler.set_interval_ms(sampler.interval_ms + 5)
            elif key == "a":
                auto_quit = not auto_quit
            elif key == "M":
                nand_warn = not nand_warn
            return False

        import termios

        fd = sys.stdin.fileno()
        old_attr = termios.tcgetattr(fd)
        # Single-key input: disable line buffering, echo, signal keys
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

        # Run the input reader on its own thread with raw termios so
        # Rich's output (main thread / auto-refresh) is never affected.
        import queue
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
            return _build_layout(time.time())

        with Live(
            console=console,
            screen=True,
            refresh_per_second=4,
            get_renderable=_get_layout,
        ) as live:
            try:
                while not self._stop_flag.is_set():
                    now = time.time()

                    try:
                        key = _key_q.get(timeout=0.15)
                    except queue.Empty:
                        key = None
                    if key and _handle_key(key):
                        break

                    if not auto_detect_done and now >= auto_detect_at and self._pid_stats:
                        self._auto_detect_tracked()
                        auto_detect_done = True

                    if auto_detect_done and now >= re_eval_at:
                        self._auto_detect_tracked()
                        re_eval_at = now + 30

                    if self._check_tracked_exited():
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
