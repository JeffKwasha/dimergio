from __future__ import annotations

import datetime
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .model import Branch, FileAccumulator, PidStat, Pool, ReadEvent, SSD_BLOCK_BYTES

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


def _effective_size_bytes(sz: int) -> int:
    """Physical cost floor for comparison: file size rounded up to the SSD
    block size, with a minimum of one block."""
    return max(SSD_BLOCK_BYTES, ((sz + SSD_BLOCK_BYTES - 1) // SSD_BLOCK_BYTES) * SSD_BLOCK_BYTES)


def _cycle_sort_key(current: str, direction: int, keys: tuple[str, ...]) -> str:
    """Rotate the active sort column by `direction` (-1 left, +1 right)."""
    idx = keys.index(current)
    return keys[(idx + direction) % len(keys)]


class _Keys:
    """Single source of truth for every interactive keybinding.

    All key constants, lookup maps, and the on-screen help hints live here so
    the handlers and the rendered legend can never drift apart. Built once per
    session (needs ``readchar.key`` constants, imported lazily).
    """

    def __init__(self) -> None:
        from readchar import key as k

        self.QUIT = ("q", "Q")
        self.ENTER = k.ENTER
        self.ESC = k.ESC
        self.SPACE = k.SPACE
        self.SORT_LEFT = k.LEFT
        self.SORT_RIGHT = k.RIGHT
        self.CLEAR_MARK = "-"
        self.CLEAR_STATS = "c"
        self.SHOW_EXITED = "s"
        self.SAMPLE_DOWN = "["
        self.SAMPLE_UP = "]"
        self.NAND = "M"

        # Arrow/paging keys → navigation verbs understood by _apply_nav.
        self.NAV = {
            k.UP: "up",
            k.DOWN: "down",
            k.PAGE_UP: "page_up",
            k.PAGE_DOWN: "page_down",
            k.HOME: "home",
            k.END: "end",
        }

        # Shifted number row → branch index (mark all rows up to cursor).
        self.SHIFT_DIGIT = {
            ")": 0, "!": 1, "@": 2, "#": 3, "$": 4,
            "%": 5, "^": 6, "&": 7, "*": 8, "(": 9,
        }

        # Sort columns cycled with Left/Right; order defines rotation.
        self.SORT = ("iowait_per_mb", "iowait", "reads")

        # Help hints rendered as panel subtitles. Kept next to the bindings so
        # a change to a key forces a change to its documentation.
        self.BROWSE_HINT = (
            "↑↓:scroll  ←/→:sort  Space:rotate  Enter:review  0-9:mark  "
            "Shift+0-9:mark above  -:clear  c:clear stats  s:show-exited  "
            "[]:sample  M:nand  q:quit"
        )
        self.PREVIEW_HINT = "Enter: execute  Esc: back  q: quit"


def _apply_nav(scroll: int, selected: int, n: int, max_vis: int, key: str) -> tuple[int, int]:
    """Pure file-list navigation.

    Returns the new (scroll, selected) given the current values, the total
    number of rows ``n``, the visible window size ``max_vis`` and a key
    string (compared against ``readchar.key`` constants by the caller).

    Unknown keys leave state unchanged. This is the single source of truth
    for scrolling in both monitor and select modes, so navigation behaves
    identically everywhere and can be unit-tested without a terminal.
    """
    if n <= 0:
        return 0, 0
    if key == "up":
        selected = max(0, selected - 1)
        if selected < scroll:
            scroll = selected
    elif key == "down":
        selected = min(n - 1, selected + 1)
        if selected >= scroll + max_vis:
            scroll = selected - max_vis + 1
    elif key == "page_up":
        scroll = max(0, scroll - max_vis)
        selected = scroll
    elif key == "page_down":
        scroll = min(max(0, n - max_vis), scroll + max_vis)
        selected = min(n - 1, scroll + max_vis - 1)
    elif key == "home":
        scroll = 0
        selected = 0
    elif key == "end":
        selected = max(0, n - 1)
        scroll = max(0, n - max_vis)
    return scroll, selected


def _read_diskstats() -> dict[str, int]:
    """Read /proc/diskstats once and return {device_name: io_ticks}.

    ``io_ticks`` is the 13th field (0-indexed column 12) in
    ``/proc/diskstats``.  Returns an empty dict if the file cannot be read.
    """
    result: dict[str, int] = {}
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 14:
                    continue
                try:
                    result[parts[2]] = int(parts[12])
                except (ValueError, IndexError):
                    continue
    except OSError:
        pass
    return result


class IOWaitSampler:
    """Samples per-device I/O busy-time and attributes it to read events.

    Each polling window's delta ``io_ticks`` is added to a pending bucket.
    Every read event consumes an equal share of the pending bucket, so I/O
    wait is attributed to the events that arrived during the busy window
    instead of being lagged by one sample or zeroed between bursts.
    """

    MIN_INTERVAL_MS = 5

    def __init__(
        self,
        branches: list[Branch],
        interval_ms: int = 10,
        debug_log: Path | None = None,
    ):
        self._branches = branches
        self._interval_ms = interval_ms
        self._interval = interval_ms / 1000
        self._pending_ms: dict[int, float] = {i: 0.0 for i in range(len(branches))}
        self._event_counts: dict[int, int] = {i: 0 for i in range(len(branches))}
        self._prev_ticks: dict[int, int] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._debug_log = debug_log
        self._debug_fh = None
        if debug_log is not None:
            try:
                self._debug_fh = open(debug_log, "w", encoding="utf-8")
                self._debug_fh.write("timestamp branch device delta_ms pending_ms events\n")
                self._debug_fh.flush()
            except OSError as exc:
                logger.warning("Cannot open debug log %s: %s", debug_log, exc)
                self._debug_log = None

    @property
    def interval_ms(self) -> int:
        return self._interval_ms

    def set_interval_ms(self, ms: int) -> None:
        ms = max(self.MIN_INTERVAL_MS, ms)
        self._interval_ms = ms
        self._interval = ms / 1000

    def record_event(self, branch_idx: int) -> None:
        with self._lock:
            self._event_counts[branch_idx] += 1

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._close_debug_log()

    def _close_debug_log(self) -> None:
        fh = self._debug_fh
        if fh is not None:
            self._debug_fh = None
            fh.close()

    def get_busy(self, branch_idx: int) -> float:
        """Return this event's share of pending I/O wait time in seconds.

        The pending bucket is divided evenly among the events that have
        been recorded but not yet charged.
        """
        with self._lock:
            cnt = self._event_counts.get(branch_idx, 0)
            if cnt <= 0:
                return 0.0
            pending = self._pending_ms.get(branch_idx, 0.0)
            share = pending / cnt
            self._pending_ms[branch_idx] = pending - share
            self._event_counts[branch_idx] = cnt - 1
            return share / 1000.0

    def _run(self) -> None:
        initial = _read_diskstats()
        for i, branch in enumerate(self._branches):
            self._prev_ticks[i] = initial.get(branch.device, 0)

        while not self._stop.is_set():
            self._stop.wait(self._interval)

            stats = _read_diskstats()
            timestamp = datetime.datetime.now().isoformat()
            with self._lock:
                for i, branch in enumerate(self._branches):
                    curr = stats.get(branch.device)
                    if curr is None:
                        continue
                    prev = self._prev_ticks.get(i, curr)
                    delta_ms = curr - prev
                    self._prev_ticks[i] = curr
                    if delta_ms < 0:
                        # Counter wrapped or device reset; discard this tick.
                        continue
                    self._pending_ms[i] += delta_ms
                    if self._debug_fh is not None:
                        self._debug_fh.write(
                            f"{timestamp} {i} {branch.device} {delta_ms} "
                            f"{self._pending_ms[i]:.1f} {self._event_counts[i]}\n"
                        )
                        self._debug_fh.flush()


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
        preloaded: dict[Path, FileAccumulator] | None = None,
        debug_log: Path | None = None,
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
        self._debug_log = debug_log
        self.force_move = False
        self.move_plans: list = []
        self._preloaded = preloaded
        self._fatrace_proc: subprocess.Popen | None = None
        self._fatrace_thread: threading.Thread | None = None
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

    def _build_fatrace_cmd(self) -> list[str]:
        """Construct the fatrace command line (single source of truth)."""
        _FATRACE = "/usr/sbin/fatrace"
        cmd = [_FATRACE, "-f", "RW", "-u", "-t", "-t"]
        if self.use_sudo and os.geteuid() != 0:
            import getpass

            user = getpass.getuser()
            fatrace_path = shutil.which("fatrace") or _FATRACE
            print("\nTip: run this once to skip the password prompt:")
            print(
                f"  echo '{user} ALL=(root) NOPASSWD: {fatrace_path}' | "
                f"sudo tee /etc/sudoers.d/dimergio && sudo chmod 0440 /etc/sudoers.d/dimergio\n"
            )
            cmd = ["sudo"] + cmd
        return cmd

    def start_fatrace(self, sampler: IOWaitSampler) -> None:
        """Spawn fatrace and its reader thread together.

        Owning both here means pause/resume can never leave a running
        fatrace with no reader (the old MONITOR↔SELECT desync bug).
        """
        if self._fatrace_proc is not None:
            return
        cmd = self._build_fatrace_cmd()

        def reader() -> None:
            assert self._fatrace_proc is not None and self._fatrace_proc.stdout is not None
            for raw in self._fatrace_proc.stdout:
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

        self._fatrace_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=str(self.pool.mount),
        )
        assert self._fatrace_proc.stdout is not None
        self._fatrace_thread = threading.Thread(target=reader, daemon=True)
        self._fatrace_thread.start()

    def stop_fatrace(self) -> None:
        """Terminate fatrace and join its reader thread cleanly."""
        if self._fatrace_proc is None:
            return
        self._fatrace_proc.terminate()
        try:
            self._fatrace_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._fatrace_proc.kill()
        if self._fatrace_thread is not None:
            self._fatrace_thread.join(timeout=2)
        self._fatrace_proc = None
        self._fatrace_thread = None

    @property
    def is_monitoring(self) -> bool:
        return self._fatrace_proc is not None

    def run(self) -> dict[Path, FileAccumulator]:
        if self._preloaded is not None:
            self._accumulators = dict(self._preloaded)
            use_interactive = (
                not self.pid
                and not self.process_name
                and not self._no_interactive
                and sys.stdin.isatty()
            )
            if use_interactive:
                sampler = IOWaitSampler([], debug_log=self._debug_log)
                self._sampler = sampler
                sampler.start()
                self._run_interactive(sampler=sampler, start_in_select=True)
                sampler.stop()
            nfiles = len(self._accumulators)
            nreads = sum(a.total_reads for a in self._accumulators.values())
            logger.info("done (preloaded) — %d reads, %d files", nreads, nfiles)
            return self._accumulators

        sampler = IOWaitSampler(
            self.pool.branches, self.iowait_interval_ms, debug_log=self._debug_log
        )
        sampler.start()
        self.start_fatrace(sampler)

        use_interactive = (
            not self.pid
            and not self.process_name
            and not self._no_interactive
            and sys.stdin.isatty()
        )

        try:
            if use_interactive:
                self._run_interactive(sampler=sampler)
            else:
                self._run_passive(self._fatrace_proc, self._fatrace_thread, sampler)
        finally:
            self._stop_flag.set()
            self.stop_fatrace()
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

    def _run_interactive(self, *, sampler: IOWaitSampler, start_in_select: bool = False) -> None:
        from rich.box import SIMPLE as _SIMPLE_BOX
        from rich.console import Console, Group
        from rich.live import Live
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        console = Console(force_terminal=True)

        if not sys.stdin.isatty():
            console.print("PIPE not supported")
            return

        KEYS = _Keys()

        self._stop_flag.clear()
        start_time = time.time()
        auto_detect_done = False
        auto_detect_at = start_time + 3
        re_eval_at = start_time + 30
        nand_warn = True

        # Single merged "browse" screen replaces the old MONITOR/SELECT split.
        # Sampling runs continuously for the whole session; there is no
        # separate monitor mode to switch into, so no fatrace respawn (and
        # thus no reader-thread desync) can occur.
        in_preview = False  # merged screen always starts in browse
        file_scroll = 0
        file_selected = 0
        # Sort columns cycled with Left/Right. Default orders by iowait cost
        # per physical MB (byte-weighted), per the design.
        sort_key = KEYS.SORT[0]
        file_marks: dict[Path, int] = {}
        pending_plans: list = []
        quit_confirm_at: float | None = None
        clear_stats_at: float | None = None
        show_exited: bool = False

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

        def _sort_value(acc, key: str) -> float:
            if key == "iowait_per_mb":
                return _iowait_per_mb(acc)
            if key == "reads":
                return float(acc.total_reads)
            return acc.iowait_debt  # "iowait"

        def _sorted_files(key: str | None = None):
            # Files written during our run are ineligible for moves, so they
            # are excluded from the ranked/selectable list.
            candidates = [a for a in self._accumulators.values() if a.write_count == 0]
            return sorted(
                candidates,
                key=lambda a: _sort_value(a, key or sort_key),
                reverse=True,
            )

        def _visible_rows() -> int:
            h = console.size.height
            reserved = 18
            return max(8, min(25, h - reserved))

        _multi_branch_cache: dict[Path, bool] = {}

        def _on_multiple_branches(acc) -> bool:
            # Memoized per session: a file's multi-branch status does not
            # change while dimergio runs, so we avoid rebuilding a
            # PoolContext on every 4 Hz render frame.
            cached = _multi_branch_cache.get(acc.path)
            if cached is not None:
                return cached
            from .pool import PoolContext
            state = PoolContext(self.pool).state
            rel = _rel_path(acc.path)
            result = any(e.pool_path == rel for e in state.all())
            _multi_branch_cache[acc.path] = result
            return result

        # File sizes are read-only for our purposes, so stat each path once
        # (the first time it is needed) and cache it for the whole session.
        # This avoids per-frame syscalls and is safe because we never move or
        # rewrite files we observed being written during the run.
        _size_cache: dict[Path, int] = {}

        def _file_size(acc) -> int:
            sz = _size_cache.get(acc.path)
            if sz is None:
                try:
                    sz = acc.path.stat().st_size
                except OSError:
                    sz = 0
                _size_cache[acc.path] = sz
            return sz

        def _effective_size(acc) -> int:
            return _effective_size_bytes(_file_size(acc))

        def _iowait_per_mb(acc) -> float:
            """iowait debt normalized by effective physical size (seconds/MB)."""
            return acc.iowait_debt / (_effective_size(acc) / 1_000_000)

        def _calc_select_stats() -> tuple[float, float, dict[int, int]]:
            marked = [
                acc for acc in _sorted_files()
                if (tidx := file_marks.get(acc.path)) is not None and tidx < len(branches)
            ]
            # Post-hoc byte-weighting (requirement #2): weight each file's
            # estimated iowait savings by its physical size relative to the
            # average marked file, so moving a large slow file counts more
            # than a tiny one with equal raw iowait. This is an approximation
            # (fatrace reports no per-event bytes); it only rescales the
            # existing per-file savings and never changes which files are
            # marked.
            avg_eff = (sum(_effective_size(a) for a in marked) / len(marked)) if marked else 1

            total_saved = 0.0
            total_time = 0.0
            space: dict[int, int] = {}
            for acc in marked:
                tidx = file_marks[acc.path]
                src_w = branches[acc.branch_idx].speed_weight
                tgt_w = branches[tidx].speed_weight
                weight = _effective_size(acc) / avg_eff
                total_saved += acc.iowait_debt * (1 - src_w / tgt_w) * weight
                sz = _file_size(acc)
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

        def _branch_legend() -> Text:
            parts = []
            for i, b in enumerate(branches):
                c = _branch_color(b.speed_class)
                parts.append(f"[{c}]{i}:{b.short_label}({b.speed_weight}x)[/{c}]")
            return Text.from_markup("  ".join(parts))

        # ─── Shared layout helpers ─────────────────────────────────────
        def _visible_file_slice(sorted_f):
            max_vis = _visible_rows()
            scroll_end = min(len(sorted_f), file_scroll + max_vis)
            return max_vis, scroll_end, sorted_f[file_scroll:scroll_end]

        def _quit_confirm_status() -> Text | None:
            if quit_confirm_at is not None:
                return Text("Press q again within 4s to quit, any other key to cancel.", style="bold yellow")
            return None

        # ─── BROWSE layout (merged monitor + select) ─────────────────
        def _build_browse(now: float) -> Panel:
            elapsed = _fmt_duration(now - start_time)
            n_reads = self._pid_stats_total_reads()
            n_files = len(self._accumulators)
            n_writes = len(self._written_paths)
            n_marked = len(file_marks)

            status_text = ""
            status_style = "dim"
            if self.is_monitoring:
                if not self._pid_stats:
                    if now - start_time > 3:
                        status_text = "No reads detected — fatrace may need --sudo"
                        status_style = "bold yellow"
                    else:
                        status_text = "Waiting for fatrace... (launch app in another terminal)"
                elif not auto_detect_done:
                    status_text = "Detecting active PIDs..."
                else:
                    tracked = [s for s in self._pid_stats.values() if s.tracked]
                    if tracked and all(s.exited for s in tracked):
                        status_text = "All tracked PIDs exited — keep watching or press Enter to review."
                    else:
                        names = ", ".join(f"{s.process_name}({s.pid})" for s in tracked[:5])
                        if names:
                            status_text = f"tracking: {names}"
            else:
                status_text = "Reviewing preloaded data (no live monitoring)."
                status_style = "bold cyan"

            if clear_stats_at is not None:
                status_text = "Press c again within 4s to clear session stats, any other key to cancel."
                status_style = "bold yellow"
            qc = _quit_confirm_status()
            if qc is not None:
                status = qc
            else:
                status = Text(status_text, style=status_style) if status_text else ""

            hz = 1000 / sampler.interval_ms
            header = Text.from_markup(
                f"[bold]since[/bold] {elapsed}  "
                f"[bold]reads[/bold] {n_reads:,}  "
                f"[bold]writes[/bold] {n_writes}  "
                f"[bold]files[/bold] {n_files}  "
                f"[bold]marked[/bold] {n_marked}  "
                f"[bold]sample[/bold] {sampler.interval_ms}ms({hz:.0f}Hz)"
            )

            tiers_line = Text.from_markup(
                f"[bold]Tiers:[/bold]  " + "  ".join(
                    f"[{_branch_color(b.speed_class)}]{i}:{b.short_label}({b.speed_weight}x)[/{_branch_color(b.speed_class)}]"
                    for i, b in enumerate(branches)
                )
                + f"   [dim]M:nand:[/dim]{'ON' if nand_warn else 'OFF'}"
            )

            # Live process table (only meaningful while monitoring).
            proc_table: Table | None = None
            if self.is_monitoring:
                proc_table = Table(show_header=True, header_style="bold", box=_SIMPLE_BOX, expand=True, pad_edge=False)
                proc_table.add_column("PROCESS", width=18, no_wrap=True)
                proc_table.add_column("READS", justify="right", width=10, no_wrap=True)
                proc_table.add_column("WRITES", justify="right", width=10, no_wrap=True)
                proc_table.add_column("IOWAIT(s)", justify="right", width=10, no_wrap=True)
                proc_table.add_column("STATUS", width=7, no_wrap=True)
                ranked_pids = sorted(self._pid_stats.values(), key=lambda s: s.read_count, reverse=True)
                visible_pids = ranked_pids if show_exited else [s for s in ranked_pids if not s.exited]
                if not visible_pids:
                    proc_table.add_row("No reads collected yet.", "", "", "", "", style="dim")
                else:
                    for s in visible_pids[:10]:
                        st = "[green]run[/green]" if not s.exited else "[red]exited[/red]"
                        proc_table.add_row(s.process_name[:18], f"{s.read_count:,}", str(s.write_count), f"{s.total_iowait_sec:.3f}", st)

            # File table with FROM/TO marking columns + cursor highlight.
            sorted_f = _sorted_files()
            max_vis, scroll_end, visible_files = _visible_file_slice(sorted_f)

            _SORT_LABEL = {"reads": "READS", "iowait": "IOWAIT", "iowait_per_mb": "IOW/MB"}
            file_table = Table(show_header=True, header_style="", box=_SIMPLE_BOX, expand=True, pad_edge=False)
            file_table.add_column("#", justify="right", width=4, no_wrap=True)
            for col_key in ("reads", "iowait", "iowait_per_mb"):
                col_name = _SORT_LABEL[col_key]
                file_table.add_column(
                    f"*{col_name}" if sort_key == col_key else f" {col_name}",
                    justify="right", width=10, no_wrap=True,
                )
            file_table.add_column("FROM", width=8, no_wrap=True)
            file_table.add_column("TO", width=8, no_wrap=True)
            file_table.add_column("FILE", no_wrap=True, ratio=1)

            if not sorted_f:
                file_table.add_row("", "", "", "", "", "", "No files tracked yet.", style="dim")
            else:
                for rank, acc in enumerate(visible_files, file_scroll + 1):
                    br = branches[acc.branch_idx] if acc.branch_idx < len(branches) else branches[0]
                    from_label = br.short_label + ("…" if _on_multiple_branches(acc) else "")
                    from_c = _branch_color(br.speed_class)

                    tidx = file_marks.get(acc.path)
                    if tidx is not None and tidx < len(branches):
                        tgt = branches[tidx]
                        to_cell = f"[{_branch_color(tgt.speed_class)}]{tgt.short_label}[/{_branch_color(tgt.speed_class)}]"
                    else:
                        to_cell = "[dim]-[/dim]"

                    row_style = "reverse" if (rank - 1 == file_selected) else ""
                    file_table.add_row(
                        str(rank),
                        f"{acc.total_reads:,}",
                        f"{acc.iowait_debt:.3f}",
                        f"{_iowait_per_mb(acc):.4f}",
                        f"[{from_c}]{from_label}[/{from_c}]",
                        to_cell,
                        _rel_path(acc.path),
                        style=row_style,
                    )

            # Tier reads / iowait summary.
            tier_reads = [0] * len(branches)
            tier_iowait = [0.0] * len(branches)
            for acc in self._accumulators.values():
                idx = acc.branch_idx if acc.branch_idx < len(branches) else 0
                tier_reads[idx] += acc.total_reads
                tier_iowait[idx] += acc.iowait_debt
            total_r = sum(tier_reads) or 1
            total_io = sum(tier_iowait) or 1.0
            reads_parts = [
                f"[{_branch_color(b.speed_class)}]{b.short_label} {tier_reads[i]:,} ({tier_reads[i] / total_r * 100:.0f}%)[/{_branch_color(b.speed_class)}]"
                for i, b in enumerate(branches)
            ]
            iowait_parts = [
                f"[{_branch_color(b.speed_class)}]{b.short_label} {tier_iowait[i]:.1f}s ({tier_iowait[i] / total_io * 100:.0f}%)[/{_branch_color(b.speed_class)}]"
                for i, b in enumerate(branches)
            ]
            tier_stats = Text.from_markup(
                f"[bold]Tier reads:[/bold]  {'  '.join(reads_parts)}\n"
                f"[bold]     iowait:[/bold]  {'  '.join(iowait_parts)}"
            )

            est_saved, est_time, space_req = _calc_select_stats()
            space_parts = [
                f"[{_branch_color(b.speed_class)}]{b.short_label} {_fmt_bytes(space_req.get(i, 0))}[/{_branch_color(b.speed_class)}]"
                for i, b in enumerate(branches)
            ]
            space_line = Text.from_markup(
                f"[bold]est. iowait saved[/bold] {est_saved:.1f}s   "
                f"[bold]est. move time[/bold] {est_time:.1f}s   "
                f"[bold]space required[/bold]  {'  '.join(space_parts)}"
            )

            sub = KEYS.BROWSE_HINT

            sort_line = Text.from_markup(f"[bold]Sort:[/bold] [reverse]{_SORT_LABEL[sort_key]}[/reverse] ▼")
            body: list = [header, sort_line, tiers_line]
            if proc_table is not None:
                body.append(proc_table)
            body += [file_table, tier_stats, space_line]
            return Panel(
                Group(*body, status),
                title=f"dimergio — {self.pool.mount}  [bold green]BROWSE[/bold green]",
                subtitle=sub,
                border_style="dim",
            )

        # ─── PREVIEW layout ─────────────────────────────────────────────
        def _build_preview() -> Panel:
            total = 0
            table = Table(show_header=True, header_style="bold", box=_SIMPLE_BOX, expand=True, pad_edge=False)
            table.add_column("#", justify="right", width=4, no_wrap=True)
            table.add_column("FROM", width=8, no_wrap=True)
            table.add_column("TO", width=8, no_wrap=True)
            table.add_column("FILE", no_wrap=True, ratio=1)
            table.add_column("SIZE", justify="right", width=10, no_wrap=True)

            for i, plan in enumerate(pending_plans, 1):
                acc = plan.file
                src = branches[acc.branch_idx] if acc.branch_idx < len(branches) else branches[0]
                tgt = branches[plan.target_branch_idx]
                sz = _file_size(acc)
                total += sz
                sc = _branch_color(src.speed_class)
                tc = _branch_color(tgt.speed_class)
                table.add_row(
                    str(i),
                    f"[{sc}]{src.short_label}[/{sc}]",
                    f"[{tc}]{tgt.short_label}[/{tc}]",
                    _rel_path(acc.path),
                    _fmt_bytes(sz),
                )

            header = Text.from_markup(
                f"[bold]{len(pending_plans)} move(s)[/bold]  total {_fmt_bytes(total)}"
            )
            qc = _quit_confirm_status()
            status = qc if qc is not None else Text("Enter: execute   Esc: back to browse   q: quit", style="bold yellow")

            return Panel(
                Group(header, table),
                title=f"dimergio — {self.pool.mount}  [bold yellow]PREVIEW[/bold yellow]",
                subtitle=KEYS.PREVIEW_HINT,
                border_style="yellow",
            )

        # ─── Key handling ───────────────────────────────────────────────
        # All keys are parsed by readchar into complete strings (see the
        # reader thread below), so handlers only ever see whole keystrokes
        # — never a half-read escape sequence. Every binding is defined once
        # on the KEYS object (see _Keys), so handlers and the on-screen legend
        # can never drift apart.

        def _rotate_sort(direction: int) -> None:
            nonlocal sort_key
            sort_key = _cycle_sort_key(sort_key, direction, KEYS.SORT)

        def _nav_file(key: str) -> bool:
            """Scroll the file list. Single navigation path for all modes."""
            nonlocal file_scroll, file_selected
            kind = KEYS.NAV.get(key)
            if kind is None:
                return False
            n = len(_sorted_files())
            max_vis = _visible_rows()
            file_scroll, file_selected = _apply_nav(file_scroll, file_selected, n, max_vis, kind)
            return True

        def _handle_key(key: str) -> bool:
            nonlocal file_scroll, file_selected, quit_confirm_at, clear_stats_at
            nonlocal nand_warn, auto_detect_done, pending_plans, in_preview

            if key in KEYS.QUIT or key == "\x03":
                now = time.time()
                if quit_confirm_at is not None and now - quit_confirm_at <= 4:
                    self._stop_flag.set()
                    return True
                quit_confirm_at = now
                return False
            if quit_confirm_at is not None:
                quit_confirm_at = None

            if clear_stats_at is not None and key != KEYS.CLEAR_STATS:
                clear_stats_at = None

            return _handle_browse_key(key) if not in_preview else _handle_preview_key(key)

        def _handle_browse_key(key: str) -> bool:
            nonlocal file_scroll, file_selected, clear_stats_at, nand_warn, pending_plans
            nonlocal show_exited, sort_key, in_preview
            sorted_f = _sorted_files()

            if _nav_file(key):
                return False
            if key == KEYS.SORT_LEFT:
                _rotate_sort(1)
            ...
            if key == KEYS.SORT_RIGHT:
                _rotate_sort(-1)
                return False
            if key == KEYS.SPACE:
                if file_selected < len(sorted_f):
                    acc = sorted_f[file_selected]
                    tidx = file_marks.get(acc.path)
                    nxt = (acc.branch_idx + 1) % len(branches) if tidx is None else (tidx + 1) % len(branches)
                    if nxt == acc.branch_idx:
                        file_marks.pop(acc.path, None)
                    else:
                        file_marks[acc.path] = nxt
            elif key in KEYS.SHIFT_DIGIT:
                br_idx = KEYS.SHIFT_DIGIT[key]
                if br_idx < len(branches) and file_selected < len(sorted_f):
                    for i in range(file_selected + 1):
                        acc = sorted_f[i]
                        if acc.write_count == 0:
                            file_marks[acc.path] = br_idx
            elif key.isdigit():
                br_idx = int(key)
                if br_idx < len(branches) and file_selected < len(sorted_f):
                    acc = sorted_f[file_selected]
                    if br_idx == acc.branch_idx:
                        file_marks.pop(acc.path, None)
                    else:
                        file_marks[acc.path] = br_idx
            elif key == KEYS.CLEAR_MARK:
                if file_selected < len(sorted_f):
                    file_marks.pop(sorted_f[file_selected].path, None)
            elif key == KEYS.CLEAR_STATS:
                now = time.time()
                if clear_stats_at is not None and now - clear_stats_at <= 4:
                    self._accumulators.clear()
                    clear_stats_at = None
                else:
                    clear_stats_at = now
            elif key == KEYS.SHOW_EXITED:
                show_exited = not show_exited
            elif key == KEYS.SAMPLE_DOWN:
                sampler.set_interval_ms(sampler.interval_ms - 5)
            elif key == KEYS.SAMPLE_UP:
                sampler.set_interval_ms(sampler.interval_ms + 5)
            elif key == KEYS.NAND:
                nand_warn = not nand_warn
            elif key == KEYS.ENTER:
                if not file_marks:
                    return False
                pending_plans = []
                for acc in sorted_f:
                    tidx = file_marks.get(acc.path)
                    if tidx is None or tidx >= len(branches):
                        continue
                    from .model import MovePlan
                    pending_plans.append(MovePlan(
                        file=acc,
                        target_branch_idx=tidx,
                        is_rename_only=False,
                    ))
                in_preview = True
            return False

        def _handle_preview_key(key: str) -> bool:
            nonlocal in_preview, pending_plans
            if key == KEYS.ENTER:
                self.move_plans = pending_plans
                return True
            elif key == KEYS.ESC:
                in_preview = False
                return False
            return False

        # ─── Keyboard reader thread ─────────────────────────────────────
        # The terminal is already in raw mode. We read bytes directly with
        # sys.stdin.read(1) and assemble escape sequences ourselves, mirroring
        # readchar.readkey() but avoiding its TCSAFLUSH which drops buffered
        # keystrokes.
        import queue

        import termios

        _fd = sys.stdin.fileno()
        _old_term = termios.tcgetattr(_fd)
        _raw_term = list(_old_term)
        _raw_term[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN)
        _raw_term[0] &= ~termios.IXON
        _raw_term[6][termios.VMIN] = 1
        _raw_term[6][termios.VTIME] = 0
        termios.tcsetattr(_fd, termios.TCSADRAIN, _raw_term)

        _key_q: "queue.Queue[str | None]" = queue.Queue()
        _stop_reader = threading.Event()

        def _read_raw_key() -> str | None:
            """Read a single keystroke directly from the raw terminal.

            Mirrors readchar.readkey() but uses sys.stdin.read(1) directly,
            avoiding readchar's TCSAFLUSH which discards buffered input and
            drops keystrokes.
            """
            try:
                ch = sys.stdin.read(1)
            except (OSError, ValueError):
                return None
            if not ch:
                return None

            if ch != "\x1b":
                return ch

            # Escape sequence: consume the rest the same way readkey() does.
            try:
                ch2 = sys.stdin.read(1)
            except (OSError, ValueError):
                return ch
            if ch2 not in "\x4f\x5b":
                return ch + ch2
            try:
                ch3 = sys.stdin.read(1)
            except (OSError, ValueError):
                return ch + ch2
            if ch3 not in "\x31\x32\x33\x35\x36":
                return ch + ch2 + ch3
            try:
                ch4 = sys.stdin.read(1)
            except (OSError, ValueError):
                return ch + ch2 + ch3
            if ch4 not in "\x30\x31\x33\x34\x35\x37\x38\x39":
                return ch + ch2 + ch3 + ch4
            try:
                ch5 = sys.stdin.read(1)
            except (OSError, ValueError):
                return ch + ch2 + ch3 + ch4
            return ch + ch2 + ch3 + ch4 + ch5

        def _key_reader() -> None:
            while not _stop_reader.is_set():
                key = _read_raw_key()
                if key is not None:
                    _key_q.put(key)

        t = threading.Thread(target=_key_reader, daemon=True)
        t.start()

        def _get_layout() -> Panel:
            return _build_preview() if in_preview else _build_browse(time.time())

        try:
            with Live(console=console, screen=True, refresh_per_second=4, get_renderable=_get_layout) as live:
                while not self._stop_flag.is_set():
                    now = time.time()

                    try:
                         key = _key_q.get(timeout=0.1)
                    except queue.Empty:
                        key = None
                    if self._verbose and key is not None:
                        logger.info("key: %r", key)
                    if key and _handle_key(key):
                        break

                    if self.is_monitoring:
                        if not auto_detect_done and now >= auto_detect_at and self._pid_stats:
                            self._auto_detect_tracked()
                            auto_detect_done = True
                        if auto_detect_done and now >= re_eval_at:
                            self._auto_detect_tracked()
                            re_eval_at = now + 30
        except KeyboardInterrupt:
            # User interrupted — exit cleanly; finally restores the terminal.
            pass
        finally:
            _stop_reader.set()
            # Unblock the reader thread (stuck in sys.stdin.read(1)) so it
            # sees the stop flag and exits.
            try:
                os.write(_fd, b"\x00")
            except OSError:
                pass
            t.join(timeout=1)
            termios.tcsetattr(_fd, termios.TCSADRAIN, _old_term)

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
