from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MmapCandidate:
    pid: int
    process_name: str
    read_bytes: int = 0


def _proc_comm(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def _proc_io_read_bytes(pid: int) -> int:
    """``read_bytes`` from /proc/<pid>/io — includes mmap page-in I/O."""
    try:
        with open(f"/proc/{pid}/io") as f:
            for line in f:
                if line.startswith("read_bytes:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return 0


class MmapWatcher:
    """Detect mmap (page-fault) reads that fanotify/fatrace never see.

    fanotify does not report accesses that happen through mmap(2), so a model
    loaded by llama.cpp leaves no trace in fatrace.  MmapWatcher closes that
    gap with a CO-RE eBPF tracer (``dimergio-mmap``) that counts page faults
    per (pid, inode) and streams them to this process, which hands them to the
    collector's normal accumulation pipeline.

    Cost scales with actual faults (zero when idle) — no PTE walks, no
    per-window scans of the whole mapping.
    """

    def __init__(
        self,
        path_ok: Callable[[Path], bool],
        use_sudo: bool = False,
        binary: str | None = None,
    ) -> None:
        self._path_ok = path_ok
        self._use_sudo = use_sudo
        self._binary = binary or self._find_binary()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._watching: set[int] = set()
        self._ino_cache: dict[int, dict[int, Path]] = {}
        self._candidates: list[MmapCandidate] = []
        self._on_event: Callable[[int, int, int], None] | None = None
        self._interval_ms = 10

    def _find_binary(self) -> str | None:
        here = Path(__file__).resolve().parent
        bundled = here / "bin" / "dimergio-mmap"
        if bundled.is_file() and os.access(bundled, os.X_OK):
            return str(bundled)
        return shutil.which("dimergio-mmap")

    @property
    def available(self) -> bool:
        return self._binary is not None

    def candidates(self) -> list[MmapCandidate]:
        with self._lock:
            return list(self._candidates)

    def is_watching(self, pid: int) -> bool:
        with self._lock:
            return pid in self._watching

    def watching(self) -> set[int]:
        with self._lock:
            return set(self._watching)

    # ── one-shot candidate scan ──────────────────────────────────────
    def scan(self) -> list[MmapCandidate]:
        """Return processes doing storage I/O whose file mappings fall under
        the watched data path, sorted by total ``read_bytes`` descending.

        ``read_bytes`` is a monotonic per-process counter (mmap page-ins
        included), so the ordering is stable across scans — unlike a rate,
        which would collapse to zero between scans.
        """
        totals: list[MmapCandidate] = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            name = _proc_comm(pid)
            if not name:
                continue
            rb = _proc_io_read_bytes(pid)
            if rb > 0 and self._has_mapping_under_data(pid):
                totals.append(MmapCandidate(pid=pid, process_name=name, read_bytes=rb))
        totals.sort(key=lambda c: c.read_bytes, reverse=True)
        with self._lock:
            self._candidates = totals
        return totals

    def _has_mapping_under_data(self, pid: int) -> bool:
        try:
            with open(f"/proc/{pid}/maps") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 6 and parts[5].startswith("/"):
                        if "(deleted)" in line:
                            continue
                        try:
                            if self._path_ok(Path(parts[5])):
                                return True
                        except OSError:
                            continue
        except OSError:
            return False
        return False

    # ── tracer lifecycle ─────────────────────────────────────────────
    def enable(self, pid: int, on_event: Callable[[int, int, int], None]) -> bool:
        """Start tracing ``pid``'s mmap page faults.  Returns False if the
        tracer binary is unavailable or failed to start."""
        if not self._binary:
            return False
        with self._lock:
            if pid in self._watching:
                return True
            self._on_event = on_event
            if self._proc is None and not self._start_tracer():
                return False
            self._watching.add(pid)
            self._send(f"+{pid}\n")
        return True

    def disable(self, pid: int) -> None:
        should_stop = False
        with self._lock:
            if pid not in self._watching:
                return
            self._watching.discard(pid)
            self._ino_cache.pop(pid, None)
            if self._proc is not None:
                self._send(f"-{pid}\n")
            should_stop = not self._watching
        if should_stop:
            self._stop_tracer()

    def set_interval_ms(self, ms: int) -> None:
        self._interval_ms = ms
        with self._lock:
            if self._proc is not None:
                self._send(f"i {ms}\n")

    def stop(self) -> None:
        with self._lock:
            self._watching.clear()
            self._ino_cache.clear()
        self._stop_tracer()

    def _start_tracer(self) -> bool:
        assert self._binary is not None
        cmd = [self._binary, "--interval-ms", str(self._interval_ms)]
        if self._use_sudo and os.geteuid() != 0:
            cmd = ["sudo"] + cmd
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            logger.warning("mmap tracer failed to start: %s", exc)
            return False
        self._proc = proc
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        for pid in self._watching:
            self._send(f"+{pid}\n")
        return True

    def _stop_tracer(self) -> None:
        proc, thread = self._proc, self._thread
        self._proc = None
        self._thread = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
        if thread is not None:
            thread.join(timeout=2)

    def _send(self, data: str) -> None:
        proc = self._proc
        if proc is not None and proc.stdin is not None:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    def _reader(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) != 3:
                continue
            try:
                pid, ino, count = int(parts[0]), int(parts[1]), int(parts[2])
            except ValueError:
                continue
            cb = self._on_event
            if cb is not None:
                cb(pid, ino, count)
        # Tracer exited (terminated by us or crashed): drop the stale ref so
        # a later enable() can restart it.
        with self._lock:
            self._proc = None
            self._thread = None

    # ── inode → path resolution ──────────────────────────────────────
    def resolve_path(self, pid: int, ino: int) -> Path | None:
        """Resolve a (pid, inode) pair to a mapped file path via
        /proc/<pid>/maps, cached per pid."""
        with self._lock:
            cache = self._ino_cache.get(pid)
        if cache is None:
            cache = self._build_ino_map(pid)
            with self._lock:
                self._ino_cache[pid] = cache
        return cache.get(ino)

    def _build_ino_map(self, pid: int) -> dict[int, Path]:
        d: dict[int, Path] = {}
        try:
            with open(f"/proc/{pid}/maps") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 6 and parts[5].startswith("/"):
                        if "(deleted)" in line:
                            continue
                        path = Path(parts[5])
                        try:
                            st = path.stat()
                        except OSError:
                            continue
                        d.setdefault(st.st_ino, path)
        except OSError:
            pass
        return d