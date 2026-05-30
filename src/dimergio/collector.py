from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path

from .model import Branch, FileAccumulator, Pool, ReadEvent

_LINE_RE = re.compile(
    r"^(?P<ts>\d+\.\d+) "
    r"(?P<proc>\S+)\((?P<pid>\d+)\)"
    r"\[(?P<uid>\d+):(?P<gid>\d+)\]: "
    r"(?P<event>\w+) "
    r"(?P<path>/\S+)$"
)


class IOWaitSampler:
    """Samples per-device I/O busy-time from /sys/block/*/stat at a fixed rate."""

    def __init__(self, branches: list[Branch], interval_ms: int = 10):
        self._branches = branches
        self._interval = interval_ms / 1000
        self._latest: dict[int, float] = {}  # id(branch) → smoothed busy%
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

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
        handles: dict[Branch, int] = {}
        for i, branch in enumerate(self._branches):
            try:
                raw = branch.device_stat_path.read_text().split()
                handles[branch] = int(raw[9])
            except (OSError, IndexError, ValueError):
                handles[branch] = 0

        prev = dict(handles)
        interval_s = self._interval

        while not self._stop.is_set():
            time.sleep(interval_s)

            for i, branch in enumerate(self._branches):
                try:
                    raw = branch.device_stat_path.read_text().split()
                    curr = int(raw[9])
                except (OSError, IndexError, ValueError):
                    continue

                delta_ms = curr - prev.get(branch, curr)
                prev[branch] = curr

                busy_pct = (delta_ms / (interval_s * 1000)) * 100.0
                self._latest[i] = min(busy_pct, 100.0)


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
    ):
        self.pool = pool
        self.data_path = data_path or pool.mount
        self.process_name = process_name
        self.pid = pid
        self.use_sudo = use_sudo
        self.iowait_interval_ms = iowait_interval_ms
        self._accumulators: dict[Path, FileAccumulator] = {}
        self._last_event_by_process: dict[str, float] = {}
        self._my_uid = os.getuid()
        self._stop_flag = threading.Event()
        self._branch_for_path: dict[Path, int] = {}  # cache

    def run(self) -> dict[Path, FileAccumulator]:
        sampler = IOWaitSampler(self.pool.branches, self.iowait_interval_ms)
        sampler.start()

        cmd = ["fatrace", "-c", "-f", "R", "-u", "-t", "-t"]
        if self.use_sudo:
            cmd.insert(0, "sudo")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=str(self.pool.mount),
        )
        assert proc.stdout is not None
        self._print_status("collecting...")

        def reader():
            for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                event = self._parse_line(line)
                if event is not None:
                    self._accumulate(event, sampler)

        read_thread = threading.Thread(target=reader, daemon=True)
        read_thread.start()

        start_time = time.time()
        try:
            while not self._stop_flag.is_set():
                if self.pid is not None and not Path(f"/proc/{self.pid}").exists():
                    self._print_status(f"PID {self.pid} exited")
                    break
                if self.process_name is not None:
                    last = self._last_event_by_process.get(self.process_name, 0)
                    if last and (time.time() - last) > 30:
                        self._print_status(f"process '{self.process_name}' inactive for 30s")
                        break
                self._stop_flag.wait(1.0)
        except KeyboardInterrupt:
            pass

        self._stop_flag.set()
        proc.terminate()
        proc.wait()
        read_thread.join(timeout=5)
        sampler.stop()

        elapsed = time.time() - start_time
        nfiles = len(self._accumulators)
        nreads = sum(a.total_reads for a in self._accumulators.values())
        self._print_status(f"done — {elapsed:.0f}s, {nreads} reads, {nfiles} files")

        return self._accumulators

    def _parse_line(self, line: str) -> ReadEvent | None:
        m = _LINE_RE.match(line)
        if not m:
            return None
        event_type = m.group("event")
        if event_type != "R":
            return None

        uid = int(m.group("uid"))
        if uid != self._my_uid:
            return None

        path_str = m.group("path")
        file_path = Path(path_str)
        if not self._in_data_path(file_path):
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
            device_busy_pct=0.0,  # filled in _accumulate
        )

    def _accumulate(self, event: ReadEvent, sampler: IOWaitSampler) -> None:
        busy = sampler.get_busy(event.branch_idx)
        event.device_busy_pct = busy

        self._last_event_by_process[event.process_name] = event.timestamp

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
        acc.observe(event.timestamp, busy)

    def _in_data_path(self, path: Path) -> bool:
        try:
            path.relative_to(self.data_path)
            return True
        except ValueError:
            return False

    def _resolve_branch(self, path: Path) -> int:
        # Cached lookup: which branch has this file?
        try:
            return self._branch_for_path[path]
        except KeyError:
            pass

        # Resolve: path is through pool mount, map to branch
        # First get path relative to data path
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

        # File might be on any branch if not found (check all)
        for idx, branch in enumerate(self.pool.branches):
            candidate = branch.path / rel
            if candidate.exists():
                self._branch_for_path[path] = idx
                return idx

        # Fallback: assume first (slow) branch
        self._branch_for_path[path] = 0
        return 0

    @staticmethod
    def _print_status(msg: str) -> None:
        print(f"[dimergio] {msg}", file=__import__("sys").stderr)
