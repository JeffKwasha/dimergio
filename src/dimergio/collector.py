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

from .model import FileAccumulator, PidStat, Pool, ReadEvent, SSD_BLOCK_BYTES
from .pool import IO_Domain
from .mmap import _proc_comm

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


_CSI_U_KEYS = {
    "1": "\x1b[H",   # Home
    "4": "\x1b[F",   # End
    "5": "\x1b[5~",  # PageUp
    "6": "\x1b[6~",  # PageDown
    "9": "\t",       # Tab
    "13": "\n",      # Enter
    "27": "\x1b",    # Esc
    "32": " ",       # Space
}


def _csiu_to_key(code: str) -> str:
    """Map a kitty CSI-u key code (e.g. ``27`` from ``\x1b[27;5u``) to the
    canonical readchar key. Unknown codes pass through as ``\x1b[<code>u``."""
    base = code.split(";", 1)[0]
    return _CSI_U_KEYS.get(base, "\x1b[" + base + "u")


def _normalize_key(seq: str) -> str:
    """Normalize any terminal's escape-sequence dialect to the canonical
    readchar key constants the handlers compare against.

    Covers the common modes dimergio can be launched under: plain CSI arrows
    (``\x1b[A``), application-cursor mode (SS3 ``\x1bOA``), xterm alternate
    Home/End (``\x1b[1~``/``\x1b[4~``), modified arrows (``\x1b[1;5A``), and
    the kitty keyboard protocol (CSI-u, e.g. ``\x1b[1;1A`` / ``\x1b[27u``).
    Modifiers are stripped so Shift/Ctrl+arrow still navigates/sorts.
    """
    if len(seq) < 3 or not seq.startswith("\x1b"):
        return seq
    if seq.startswith("\x1bO"):
        # SS3/application-cursor mode: ESC O <letter> → ESC [ <letter>.
        return "\x1b[" + seq[2:]
    if not seq.startswith("\x1b["):
        return seq
    body = seq[2:]
    if body.endswith("u"):
        # Kitty CSI-u: ESC [ <code> [; <modifier>] u.
        return _csiu_to_key(body[:-1])
    final = body[-1]
    if final in "ABCDHF":
        # Arrows / Home / End, possibly with a parameter prefix (kitty
        # ``\x1b[1;1A``, xterm ``\x1b[1;5A``, DEC ``\x1b[1A``) — the plain
        # key is enough for navigation/sort.
        return "\x1b[" + final
    if final == "~":
        # ESC [ <num> [; <mod>] ~  (PageUp/Down, alternate Home/End).
        nums = [p for p in body[:-1].split(";") if p.isdigit()]
        num = nums[0] if nums else ""
        if num in ("1", "7"):
            return "\x1b[H"
        if num in ("4", "8"):
            return "\x1b[F"
        if num in ("5",):
            return "\x1b[5~"
        if num in ("6",):
            return "\x1b[6~"
    return seq


# terminfo capabilities whose sequences are mapped to the canonical keys.
# Capability → canonical readchar key (the values in _Keys.NAV etc.).
_TERMINFO_KEY_CAPS = {
    "kcub1": "\x1b[D",   # cursor left
    "kcuf1": "\x1b[C",   # cursor right
    "kcuu1": "\x1b[A",   # cursor up
    "kcud1": "\x1b[B",   # cursor down
    "khome": "\x1b[H",   # home
    "kend":  "\x1b[F",   # end
    "kpp":   "\x1b[5~",  # page up
    "knp":   "\x1b[6~",  # page down
}


def _decode_terminfo_escapes(val: str) -> str:
    """Decode infocmp's escaped capability string (``\\E[D``, ``^X``, ``\\s``,
    octal ``\\023``, ``\\\\``) into the literal byte string."""
    out: list[str] = []
    i = 0
    while i < len(val):
        c = val[i]
        if c == "\\" and i + 1 < len(val):
            n = val[i + 1]
            if n in "01234567":
                j = i + 1
                while j < len(val) and j < i + 4 and val[j] in "01234567":
                    j += 1
                out.append(chr(int(val[i + 1 : j], 8)))
                i = j
                continue
            simple = {"E": "\x1b", "s": " ", "\\": "\\", "n": "\n",
                      "t": "\t", "r": "\r", "b": "\b", "f": "\f", "0": "\x00"}
            if n in simple:
                out.append(simple[n])
                i += 2
                continue
        if c == "^" and i + 1 < len(val):
            out.append(chr(ord(val[i + 1]) & 0x1F))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _terminfo_keymap() -> dict[str, str] | None:
    """Resolve the current terminal's key sequences from its terminfo entry.

    Broad compatibility: instead of assuming one escape-sequence dialect, we
    ask ``infocmp`` what THIS terminal actually sends for each navigation key
    and map those bytes onto the canonical keys the handlers compare against.

    Returns ``{raw_sequence: canonical_key}``, or ``None`` when the lookup is
    impossible (no ``TERM``, no ``infocmp``, unknown terminal) — in which case
    the caller falls back to the built-in ``_normalize_key`` dialect table,
    which is kitty-compatible.
    """
    term = os.environ.get("TERM", "")
    if not term or shutil.which("infocmp") is None:
        return None
    try:
        out = subprocess.run(
            ["infocmp", "-1", term],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    keymap: dict[str, str] = {}
    for line in out.stdout.splitlines():
        line = line.strip().rstrip(",")
        name, sep, val = line.partition("=")
        if not sep or name not in _TERMINFO_KEY_CAPS:
            continue
        seq = _decode_terminfo_escapes(val)
        if seq:
            keymap[seq] = _TERMINFO_KEY_CAPS[name]
    return keymap or None


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
        self.TAB = k.TAB
        self.SORT_LEFT = k.LEFT
        self.SORT_RIGHT = k.RIGHT
        self.CLEAR_MARK = "-"
        self.CLEAR_STATS = "c"
        self.SHOW_EXITED = "s"
        self.SAMPLE_DOWN = "["
        self.SAMPLE_UP = "]"
        self.NAND = "M"
        self.MMAP = "m"

        # TAB switches focus between the file list and the process list
        # (Home/End kept as aliases). Arrow keys navigate within a list and
        # Left/Right re-sort whichever panel is focused.
        self.FOCUS_TOGGLE = k.TAB
        self.FOCUS_PROCS = k.HOME
        self.FOCUS_FILES = k.END

        # Arrow/paging keys → navigation verbs understood by _apply_nav.
        self.NAV = {
            k.UP: "up",
            k.DOWN: "down",
            k.PAGE_UP: "page_up",
            k.PAGE_DOWN: "page_down",
        }

        # Sort columns cycled with Left/Right; order defines rotation.
        self.SORT = ("iowait_per_mb", "iowait", "reads")
        self.PROC_SORT = ("mmap", "reads", "iowait")

        # Help hints rendered as panel subtitles. Kept next to the bindings so
        # a change to a key forces a change to its documentation.
        self.BROWSE_HINT = (
            "↑↓:scroll  ←/→:sort  Tab:files/procs  Space:mark  "
            "Enter:review  -:clear  c:clear stats  s:show-exited  "
            "m:mmap []:sample  M:nand  q:quit"
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

    Branches are grouped into *IO domains* — all branches on the same set of
    physical block devices (i.e. same btrfs pool) share one domain.  Each
    polling window's delta ``io_ticks`` across *every* device in a domain is
    summed into the domain's pending bucket.  At the next sample the bucket
    is divided evenly among the read events that arrived during the window
    and set as the per-event share for the *following* window (one-window
    lag), providing fair attribution across concurrent events.
    """

    MIN_INTERVAL_MS = 5

    def __init__(
        self,
        domains: list[IO_Domain],
        br2domain: list[int],
        interval_ms: int = 10,
        debug_log: Path | None = None,
    ):
        self._domains = domains
        self._br2domain = br2domain
        self._interval_ms = interval_ms
        self._interval = interval_ms / 1000
        nd = len(domains)
        self._pending_ms: list[float] = [0.0] * nd
        self._event_counts: list[int] = [0] * nd
        self._share_ms: list[float] = [0.0] * nd
        self._prev_ticks: dict[str, int] = {}
        self._total_iowait_ms: float = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._debug_log = debug_log
        self._debug_fh = None
        if debug_log is not None:
            try:
                self._debug_fh = open(debug_log, "w", encoding="utf-8")
                self._debug_fh.write("timestamp domain devices pending_ms share_ms events\n")
                self._debug_fh.flush()
            except OSError as exc:
                logger.warning("Cannot open debug log %s: %s", debug_log, exc)
                self._debug_log = None

    @property
    def interval_ms(self) -> int:
        return self._interval_ms

    @property
    def total_iowait_sec(self) -> float:
        """Cumulative wall-clock device-busy time across all domains (seconds)."""
        return self._total_iowait_ms / 1000.0

    def set_interval_ms(self, ms: int) -> None:
        ms = max(self.MIN_INTERVAL_MS, ms)
        self._interval_ms = ms
        self._interval = ms / 1000

    def record_event(self, branch_idx: int) -> None:
        domain_idx = self._br2domain[branch_idx]
        with self._lock:
            self._event_counts[domain_idx] += 1

    def record_events(self, branch_idx: int, n: int) -> None:
        """Accumulate ``n`` read events for one branch in a single lock take."""
        domain_idx = self._br2domain[branch_idx]
        with self._lock:
            self._event_counts[domain_idx] += n

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
        """Return this event's pre-computed share of pending I/O wait (seconds).

        The share was computed at the last sampling tick by dividing the
        previous window's accumulated device busy time evenly among the
        events that arrived during that window (one-window lag).
        """
        domain_idx = self._br2domain[branch_idx]
        return self._share_ms[domain_idx] / 1000.0

    def _run(self) -> None:
        initial = _read_diskstats()
        for domain in self._domains:
            for dev in domain.devices:
                self._prev_ticks[dev] = initial.get(dev, 0)

        while not self._stop.is_set():
            self._stop.wait(self._interval)

            stats = _read_diskstats()
            timestamp = datetime.datetime.now().isoformat()
            with self._lock:
                for domain_idx, domain in enumerate(self._domains):
                    total_delta = 0
                    for dev in domain.devices:
                        curr = stats.get(dev)
                        if curr is None:
                            continue
                        prev = self._prev_ticks.get(dev, curr)
                        self._prev_ticks[dev] = curr
                        delta = curr - prev
                        if delta >= 0:
                            total_delta += delta

                    self._pending_ms[domain_idx] += total_delta
                    self._total_iowait_ms += total_delta

                    cnt = self._event_counts[domain_idx]
                    if cnt > 0:
                        self._share_ms[domain_idx] = self._pending_ms[domain_idx] / cnt
                    else:
                        self._share_ms[domain_idx] = 0.0

                    self._pending_ms[domain_idx] = 0.0
                    self._event_counts[domain_idx] = 0

                    if self._debug_fh is not None:
                        self._debug_fh.write(
                            f"{timestamp} {domain_idx} {','.join(domain.devices)} "
                            f"{self._pending_ms[domain_idx]:.1f} "
                            f"{self._share_ms[domain_idx]:.3f} {cnt}\n"
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
        mmap_pids: list[int] | None = None,
        symlink_depth: int = 3,
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
        self._branch_for_path: dict[Path, int | None] = {}
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
        self.mmap_pids = mmap_pids or []
        self._sampler: IOWaitSampler | None = None
        self.symlink_depth = symlink_depth
        # Canonicalized-path cache (per session; paths do not change location
        # while we run) and the one-time symlink reverse map (shortest wins).
        self._resolved_paths: dict[Path, Path | None] = {}
        self._symlink_map: dict[Path, str] = self._build_symlink_map()
        from .mmap import MmapWatcher

        self._mmap_watcher = MmapWatcher(
            path_ok=lambda p: self._resolve_tracked_path(p) is not None,
            use_sudo=use_sudo,
        )
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

    def _build_symlink_map(self) -> dict[Path, str]:
        """One-time reverse map: real file → shortest symlink relpath (from pwd).

        Walks ``data_path`` (the pwd anchor) up to ``symlink_depth`` levels and
        indexes every symlink by its resolved target. On collision the shorter
        relpath wins (longer ones are forgotten). Used only for display; the
        real path is always kept for moves. Depth 0 disables the map.
        """
        result: dict[Path, str] = {}
        if self.symlink_depth <= 0 or not self.data_path.exists():
            return result

        def shorter(a: str, b: str) -> bool:
            return (a.count("/"), a) < (b.count("/"), b)

        stack: list[tuple[Path, int]] = [(self.data_path, 0)]
        while stack:
            d, level = stack.pop()
            if level >= self.symlink_depth:
                continue
            try:
                it = os.scandir(d)
            except OSError:
                continue
            with it:
                for e in it:
                    try:
                        if e.is_symlink():
                            target = self._canonicalize(Path(os.path.realpath(e.path)))
                            if target is None or target.is_dir():
                                continue
                            rel = Path(e.path).relative_to(self.data_path)
                            rel_s = str(rel)
                            cur = result.get(target)
                            if cur is None or shorter(rel_s, cur):
                                result[target] = rel_s
                        elif e.is_dir():
                            stack.append((Path(e.path), level + 1))
                    except OSError:
                        continue
        return result

    def _canonicalize(self, path: Path) -> Path | None:
        """Resolve a path to the real file's canonical pool-relative location.

        Symlinks are fully resolved; paths that end up on a branch mount are
        remapped back into ``data_path``. Returns None for paths that escape
        the pool entirely. Cached per input path.
        """
        if path in self._resolved_paths:
            return self._resolved_paths[path]
        try:
            real = path.resolve()
        except OSError:
            real = path
        if self._in_data_path(real):
            result: Path | None = real
        else:
            result = None
            for branch in self.pool.branches:
                try:
                    rel = real.relative_to(branch.path)
                except ValueError:
                    continue
                result = self.data_path / rel
                break
        self._resolved_paths[path] = result
        return result

    def _in_pool(self, path: Path) -> bool:
        if self._in_data_path(path):
            return True
        for branch in self.pool.branches:
            try:
                path.relative_to(branch.path)
                return True
            except ValueError:
                continue
        return False

    def _pool_rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.data_path))
        except ValueError:
            return path.name

    def _display_name_for(self, path: Path) -> str:
        """Symlink path for ``path`` when one is indexed, else the rel path."""
        target = self._canonicalize(path)
        if target is not None:
            link = self._symlink_map.get(target)
            if link is not None:
                return link
        return self._pool_rel(path)

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
                sampler = IOWaitSampler([], [], debug_log=self._debug_log)
                self._sampler = sampler
                sampler.start()
                self._run_interactive(sampler=sampler, start_in_select=True)
                sampler.stop()
            nfiles = len(self._accumulators)
            nreads = sum(a.total_reads for a in self._accumulators.values())
            logger.info("done (preloaded) — %d reads, %d files", nreads, nfiles)
            return self._accumulators

        from .pool import _build_io_domains

        domains, br2domain = _build_io_domains(self.pool.branches)
        sampler = IOWaitSampler(
            domains, br2domain, self.iowait_interval_ms, debug_log=self._debug_log
        )
        sampler.start()
        self._sampler = sampler
        self.start_fatrace(sampler)

        for pid in self.mmap_pids:
            if not self._mmap_watcher.enable(pid, self._on_mmap):
                logger.warning("mmap tracing for pid %d could not start (see earlier warning)", pid)

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
            self._mmap_watcher.stop()

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
        # Focus ("files" | "procs") + process-list cursor. TAB switches focus;
        # Home/End are aliases. Arrow keys navigate the focused list and
        # Left/Right re-sort whichever panel is focused.
        focus = "files"
        proc_scroll = 0
        proc_selected = 0
        # Sort column for the process panel (cycled by Left/Right when focused).
        proc_sort = KEYS.PROC_SORT[0]
        # Transient status message (e.g. mmap tracer missing), shown briefly.
        flash_msg = ""
        flash_at: float | None = None

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

        def _display_label(acc) -> str:
            """Symlink name when one is indexed, else the pool-relative path."""
            return acc.display_name or self._display_name_for(acc.path)

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
            nonlocal focus, proc_scroll, proc_selected, flash_msg, flash_at
            _SORT_LABEL = {"reads": "READS", "iowait": "IOWAIT", "iowait_per_mb": "IOW/MB"}
            _PROC_SORT_LABEL = {"mmap": "MMAP", "reads": "READS", "iowait": "IOWAIT"}
            elapsed = _fmt_duration(now - start_time)
            n_reads = self._pid_stats_total_reads()
            n_files = len(self._accumulators)
            n_writes = len(self._written_paths)
            n_marked = len(file_marks)

            if flash_msg and (flash_at is None or now - flash_at <= 4):
                status = Text(flash_msg, style="bold yellow")
            else:
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
                f"[bold]sample[/bold] {sampler.interval_ms}ms({hz:.0f}Hz)  "
                f"[bold cyan]ΔIO: {sampler.total_iowait_sec:.1f}s[/bold cyan]"
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
                proc_focus = "▸" if focus == "procs" else " "
                proc_table.add_column(f"{proc_focus}PROCESS", width=18, no_wrap=True)
                for col_key in ("reads", "mmap", "iowait"):
                    col_name = _PROC_SORT_LABEL[col_key]
                    proc_table.add_column(
                        f"*{col_name}" if proc_sort == col_key else f" {col_name}",
                        justify="right", width=10, no_wrap=True,
                    )
                proc_table.add_column("STATUS", width=7, no_wrap=True)
                rows = _proc_visible_rows()
                if proc_selected >= len(rows):
                    proc_selected = max(0, len(rows) - 1)
                if not rows:
                    proc_table.add_row("No reads collected yet.", "", "", "", "", style="dim")
                else:
                    _STYLE = {
                        "run": "[green]run[/green]",
                        "exited": "[red]exited[/red]",
                        "cand": "[cyan]cand[/cyan]",
                        "watch": "[bold yellow]watch[/bold yellow]",
                    }
                    max_proc_vis = 8
                    for i in range(proc_scroll, min(len(rows), proc_scroll + max_proc_vis)):
                        pid, name, reads, mmap_b, st = rows[i]
                        stat = self._pid_stats.get(pid)
                        row_style = "reverse" if (focus == "procs" and i == proc_selected) else ""
                        proc_table.add_row(
                            name[:18],
                            f"{reads:,}" if reads else "-",
                            _fmt_bytes(mmap_b) if mmap_b else "-",
                            f"{stat.total_iowait_sec:.3f}" if stat else "-",
                            _STYLE[st],
                            style=row_style,
                        )

            # File table with FROM/TO marking columns + cursor highlight.
            sorted_f = _sorted_files()
            max_vis, scroll_end, visible_files = _visible_file_slice(sorted_f)

            file_table = Table(show_header=True, header_style="", box=_SIMPLE_BOX, expand=True, pad_edge=False)
            file_focus = "▸" if focus == "files" else " "
            file_table.add_column(f"{file_focus}#", justify="right", width=4, no_wrap=True)
            for col_key in ("reads", "iowait", "iowait_per_mb"):
                col_name = _SORT_LABEL[col_key]
                file_table.add_column(
                    f"*{col_name}" if sort_key == col_key else f" {col_name}",
                    justify="right", width=10, no_wrap=True,
                )
            file_table.add_column("FROM", width=8, no_wrap=True)
            file_table.add_column("TO", width=8, no_wrap=True)
            file_table.add_column("SIZE", justify="right", width=10, no_wrap=True)
            file_table.add_column("FILE", no_wrap=True, ratio=1)

            if not sorted_f:
                file_table.add_row("", "", "", "", "", "", "", "No files tracked yet.", style="dim")
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

                    row_style = "reverse" if (focus == "files" and rank - 1 == file_selected) else ""
                    file_table.add_row(
                        str(rank),
                        f"{acc.total_reads:,}",
                        f"{acc.iowait_debt:.3f}",
                        f"{_iowait_per_mb(acc):.4f}",
                        f"[{from_c}]{from_label}[/{from_c}]",
                        to_cell,
                        _fmt_bytes(_file_size(acc)),
                        _display_label(acc),
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

            sort_line = Text.from_markup(
                f"[bold]Sort:[/bold] [reverse]{_SORT_LABEL[sort_key] if focus == 'files' else _PROC_SORT_LABEL[proc_sort]}[/reverse] ▼   "
                f"[dim]Focus:[/dim] [reverse]{'procs' if focus == 'procs' else 'files'}[/reverse]"
            )
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
                    _display_label(acc),
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

        def _proc_rows() -> list[tuple[int, str, int, int, str]]:
            """Ordered process rows: (pid, name, reads, mmap_bytes, status).

            Rows are sorted by the currently focused process column (``proc_sort``):
            mmap bytes, fatrace read count, or total iowait.
            """
            rows: list[tuple[int, str, int, int, str]] = []
            seen: set[int] = set()
            for c in self._mmap_watcher.candidates():
                seen.add(c.pid)
                stat = self._pid_stats.get(c.pid)
                reads = stat.read_count if stat else 0
                status = "watch" if self._mmap_watcher.is_watching(c.pid) else "cand"
                rows.append((c.pid, c.process_name, reads, c.read_bytes, status))
            for s in self._pid_stats.values():
                if s.pid in seen:
                    continue
                status = "run" if not s.exited else "exited"
                rows.append((s.pid, s.process_name, s.read_count, 0, status))
            if proc_sort == "mmap":
                rows.sort(key=lambda r: r[3], reverse=True)
            elif proc_sort == "reads":
                rows.sort(key=lambda r: r[2], reverse=True)
            elif proc_sort == "iowait":
                def _io_key(r):
                    s = self._pid_stats.get(r[0])
                    return s.total_iowait_sec if s else 0.0
                rows.sort(key=_io_key, reverse=True)
            return rows

        def _proc_visible_rows() -> list[tuple[int, str, int, int, str]]:
            """Filtered (by show_exited) process rows used by render + handler."""
            return [r for r in _proc_rows() if show_exited or r[4] != "exited"]

        def _nav_proc(key: str) -> bool:
            """Scroll the process list; returns True when key was a nav key."""
            nonlocal proc_scroll, proc_selected
            kind = KEYS.NAV.get(key)
            if kind is None:
                return False
            rows = _proc_visible_rows()
            proc_scroll, proc_selected = _apply_nav(proc_scroll, proc_selected, len(rows), 8, kind)
            return True

        def _set_flash(msg: str) -> None:
            nonlocal flash_msg, flash_at
            flash_msg = msg
            flash_at = time.time()

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
            nonlocal show_exited, sort_key, proc_sort, in_preview, focus, proc_scroll, proc_selected

            # Focus switching — TAB toggles between the file and process lists
            # (Home/End kept as aliases). Offline review has no process table,
            # so refuse to focus it there.
            def _focus_procs() -> bool:
                if not self.is_monitoring:
                    _set_flash("process list only while watching — offline review has no live processes")
                    return False
                nonlocal focus, proc_scroll, proc_selected
                focus = "procs"
                proc_scroll = 0
                proc_selected = 0
                return True

            def _focus_files() -> bool:
                nonlocal focus
                focus = "files"
                return True

            if key == KEYS.FOCUS_TOGGLE or key == KEYS.FOCUS_PROCS:
                if focus == "files":
                    _focus_procs()
                else:
                    _focus_files()
                return False
            if key == KEYS.FOCUS_FILES:
                _focus_files()
                return False

            # Keys that act globally regardless of focus.
            if key == KEYS.SHOW_EXITED:
                show_exited = not show_exited
                return False
            if key == KEYS.SAMPLE_DOWN:
                sampler.set_interval_ms(sampler.interval_ms - 5)
                self._mmap_watcher.set_interval_ms(sampler.interval_ms)
                return False
            if key == KEYS.SAMPLE_UP:
                sampler.set_interval_ms(sampler.interval_ms + 5)
                self._mmap_watcher.set_interval_ms(sampler.interval_ms)
                return False
            if key == KEYS.NAND:
                nand_warn = not nand_warn
                return False
            if key == KEYS.MMAP:
                if not self._mmap_watcher.available:
                    _set_flash("mmap tracer not available — build src/dimergio/bpf (make)")
                    return False
                self._mmap_watcher.scan()
                _focus_procs()
                return False

            # Left/Right re-sort the focused panel.
            if key == KEYS.SORT_LEFT:
                if focus == "procs":
                    proc_sort = _cycle_sort_key(proc_sort, 1, KEYS.PROC_SORT)
                else:
                    sort_key = _cycle_sort_key(sort_key, 1, KEYS.SORT)
                return False
            if key == KEYS.SORT_RIGHT:
                if focus == "procs":
                    proc_sort = _cycle_sort_key(proc_sort, -1, KEYS.PROC_SORT)
                else:
                    sort_key = _cycle_sort_key(sort_key, -1, KEYS.SORT)
                return False

            if focus == "procs":
                # Empty process list: fall through to the file list so arrows
                # never feel dead (and flip focus back to files).
                if not _proc_visible_rows():
                    focus = "files"
                else:
                    if _nav_proc(key):
                        return False
                    if key == KEYS.ENTER:
                        rows = _proc_visible_rows()
                        if rows and proc_selected < len(rows):
                            pid = rows[proc_selected][0]
                            if self._mmap_watcher.is_watching(pid):
                                self._mmap_watcher.disable(pid)
                            elif not self._mmap_watcher.enable(pid, self._on_mmap):
                                _set_flash("cannot start mmap tracer — run as root or build dimergio-mmap")
                    return False

            # ── file focus ────────────────────────────────────────────
            sorted_f = _sorted_files()

            if key == KEYS.CLEAR_STATS:
                now = time.time()
                if clear_stats_at is not None and now - clear_stats_at <= 4:
                    self._accumulators.clear()
                    clear_stats_at = None
                else:
                    clear_stats_at = now
                return False
            if _nav_file(key):
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
            elif key == KEYS.CLEAR_MARK:
                if file_selected < len(sorted_f):
                    file_marks.pop(sorted_f[file_selected].path, None)
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
        # Broad compatibility: the actual sequences come from this terminal's
        # terminfo entry. When that can't be resolved, _normalize_key's
        # kitty-compatible dialect table is the fallback.
        _keymap = _terminfo_keymap()

        def _read_raw_key() -> str | None:
            """Read a single keystroke directly from the raw terminal.

            Assembles escape sequences ourselves (mirroring readchar.readkey()
            but avoiding its TCSAFLUSH which drops buffered keystrokes) and
            decodes them to the canonical readchar constants — first against
            this terminal's terminfo entry, then the built-in dialect table
            (CSI, SS3/application-mode, xterm alternate, kitty CSI-u).
            """
            try:
                ch = sys.stdin.read(1)
            except (OSError, ValueError):
                return None
            if not ch:
                return None

            if ch != "\x1b":
                return ch

            try:
                ch2 = sys.stdin.read(1)
            except (OSError, ValueError):
                return ch
            if ch2 not in "\x4f\x5b":
                return ch + ch2

            seq = ch + ch2
            if ch2 == "\x4f":
                # SS3/application-cursor mode: ESC O <final>.
                try:
                    seq += sys.stdin.read(1)
                except (OSError, ValueError):
                    pass
                if _keymap and seq in _keymap:
                    return _keymap[seq]
                return _normalize_key(seq)

            # CSI: consume parameters (digits/;) until the final byte.
            while True:
                try:
                    c = sys.stdin.read(1)
                except (OSError, ValueError):
                    break
                if not c:
                    break
                seq += c
                if c not in "\x30\x31\x32\x33\x34\x35\x36\x37\x38\x39;":
                    break
            if _keymap and seq in _keymap:
                return _keymap[seq]
            return _normalize_key(seq)

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

    def _resolve_tracked_path(self, path: Path) -> Path | None:
        """Map any observed path to the canonical pool-relative file path.

        Symlink and raw btrfs volume paths are normalized to the real file's
        location under data_path, so moves always target the actual file (never
        a symlink). Returns None when the path is not under the watched data
        path.
        """
        if self._in_data_path(path):
            return self._canonicalize(path)
        pool_path = self._remap_volume_path(path)
        if pool_path is None:
            return None
        return self._canonicalize(pool_path)

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

        file_path = self._resolve_tracked_path(Path(path_str))
        if file_path is None:
            if self._verbose:
                logger.info("  parse: not in data_path and no volume remap: %s", path_str[:80])
            return None

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
            if branch_idx is None:
                # File not on any branch (symlink/ghost path) — nothing to
                # record for a relocatable file; write tracking already done.
                pass
            else:
                try:
                    acc = self._accumulators[file_path]
                except KeyError:
                    acc = FileAccumulator(
                        path=file_path,
                        branch_idx=branch_idx,
                        first_seen=ts,
                        display_name=self._display_name_for(file_path),
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
        if branch_idx is None:
            # Path only exists through a symlink/mergerfs artifact — it has no
            # real location on any branch, so it cannot be relocated. Drop it
            # rather than attributing the read to a guessed branch.
            if self._verbose:
                logger.info("  parse: no branch holds %s — dropped", file_path)
            return None
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
                display_name=self._display_name_for(key),
            )
            self._accumulators[key] = acc
        acc.observe(event.timestamp, iowait)

    def _on_mmap(self, pid: int, ino: int, count: int) -> None:
        """Merge an aggregated mmap page-fault window from the eBPF tracer.

        Each fault counts as one read event for iowait fairness and per-file
        accumulation, closing the gap for reads fanotify/fatrace cannot see.
        """
        if count <= 0 or self._sampler is None:
            return
        path = self._mmap_watcher.resolve_path(pid, ino)
        if path is None:
            return
        norm = self._resolve_tracked_path(path)
        if norm is None:
            return

        ts = time.time()
        branch_idx = self._resolve_branch(norm)
        if branch_idx is None:
            if self._verbose:
                logger.info("  mmap: no branch holds %s — dropped", norm)
            return

        s = self._ensure_pid_stat(pid, _proc_comm(pid), ts)
        s.read_count += count
        s.last_seen = ts
        if s.write_count == 0:
            s.process_name = _proc_comm(pid) or s.process_name

        try:
            acc = self._accumulators[norm]
        except KeyError:
            acc = FileAccumulator(
                path=norm,
                branch_idx=branch_idx,
                first_seen=ts,
                display_name=self._display_name_for(norm),
            )
            self._accumulators[norm] = acc
        iowait = self._sampler.get_busy(branch_idx)
        acc.observe_n(ts, iowait, count)
        self._sampler.record_events(branch_idx, count)

    def _in_data_path(self, path: Path) -> bool:
        try:
            path.relative_to(self.data_path)
            return True
        except ValueError:
            return False

    def _resolve_branch(self, path: Path) -> int | None:
        """Branch index holding ``path``, or None when no branch has it.

        Returns None (not a guess) when the file cannot be located on any
        branch — e.g. paths recorded through symlink/mergerfs artifacts. Callers
        drop such events rather than attributing them to a wrong branch.
        """
        try:
            return self._branch_for_path[path]
        except KeyError:
            pass

        try:
            rel = path.relative_to(self.data_path)
        except ValueError:
            self._branch_for_path[path] = None
            return None

        for idx, branch in enumerate(self.pool.branches):
            if (branch.path / rel).exists():
                self._branch_for_path[path] = idx
                return idx

        self._branch_for_path[path] = None
        return None
