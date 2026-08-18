"""Tests for the mmap (page-fault) read detection layer.

dimergio cannot rely on fatrace/fanotify for apps that mmap files (llama.cpp
and friends): fanotify does not report mmap accesses.  These tests lock in the
MmapWatcher behaviors — candidate scanning, inode→path resolution, and the
tracer subprocess protocol — without needing a real kernel.
"""

import io
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import dimergio.mmap as mm
from dimergio.mmap import MmapWatcher


def _watcher(binary: str | None = None) -> MmapWatcher:
    return MmapWatcher(path_ok=lambda p: True, binary=binary)


# ─── candidate scan ────────────────────────────────────────────────
def test_scan_filters_and_sorts(monkeypatch):
    w = _watcher()
    monkeypatch.setattr(os, "listdir", lambda _: ["100", "101", "999", "x"])
    monkeypatch.setattr(mm, "_proc_comm", lambda pid: "" if pid == 999 else f"p{pid}")
    monkeypatch.setattr(
        mm, "_proc_io_read_bytes", lambda pid: {100: 500, 101: 9000, 999: 100000}[pid]
    )
    monkeypatch.setattr(w, "_has_mapping_under_data", lambda pid: pid in (100, 101, 999))
    res = w.scan()
    # 999 skipped (no comm), 101 > 100 by monotonic read_bytes.
    assert [(c.pid, c.process_name, c.read_bytes) for c in res] == [
        (101, "p101", 9000),
        (100, "p100", 500),
    ]
    assert w.candidates() == res


def test_scan_skips_process_without_mapping(monkeypatch):
    w = _watcher()
    monkeypatch.setattr(os, "listdir", lambda _: ["1"])
    monkeypatch.setattr(mm, "_proc_comm", lambda pid: "k")
    monkeypatch.setattr(mm, "_proc_io_read_bytes", lambda pid: 123)
    monkeypatch.setattr(w, "_has_mapping_under_data", lambda pid: False)
    assert w.scan() == []


def test_proc_io_read_bytes_parses():
    with mock.patch("builtins.open", return_value=io.StringIO(
        "rchar: 100\nwchar: 50\nread_bytes: 4242\nwrite_bytes: 10\n"
    )):
        assert mm._proc_io_read_bytes(123) == 4242


def test_proc_io_read_bytes_missing():
    with mock.patch("builtins.open", side_effect=OSError("no such file")):
        assert mm._proc_io_read_bytes(123) == 0


def test_has_mapping_under_data():
    w = MmapWatcher(path_ok=lambda p: p == Path("/pool/data/model.bin"), binary=None)
    maps = "7f..-7f.. r--p 00000000 00:1f 1001 /pool/data/model.bin\n"
    with mock.patch("builtins.open", return_value=io.StringIO(maps)):
        assert w._has_mapping_under_data(123)
    other = "7f..-7f.. r--p 00000000 00:1f 1001 /pool/other/x.bin\n"
    with mock.patch("builtins.open", return_value=io.StringIO(other)):
        assert not w._has_mapping_under_data(123)
    # (deleted) mappings are ignored entirely.
    deleted = "7f..-7f.. r--p 00000000 00:1f 1001 /pool/data/model.bin (deleted)\n"
    with mock.patch("builtins.open", return_value=io.StringIO(deleted)):
        assert not w._has_mapping_under_data(123)


# ─── inode → path resolution ───────────────────────────────────────
def test_build_ino_map_skips_deleted(monkeypatch):
    w = _watcher()
    maps = (
        "7f..-7f.. r--p 00000000 00:1f 1001 /pool/model.bin (deleted)\n"
        "7f..-7f.. r--p 00000000 00:1f 2002 /pool/model.bin\n"
        "7f..-7f.. rw-p 00000000 00:1f 3003 /other/x\n"
    )
    def fake_stat(self):
        return SimpleNamespace(st_ino=1000 + len(str(self)))

    monkeypatch.setattr(Path, "stat", fake_stat)
    with mock.patch("builtins.open", return_value=io.StringIO(maps)):
        d = w._build_ino_map(123)
    assert 1001 not in d  # (deleted) must not be resolved
    assert d[1000 + len("/pool/model.bin")] == Path("/pool/model.bin")
    assert d[1000 + len("/other/x")] == Path("/other/x")


def test_resolve_path_caches_per_pid(monkeypatch):
    w = _watcher()
    d = {77: Path("/pool/a"), 88: Path("/pool/b")}
    monkeypatch.setattr(w, "_build_ino_map", lambda pid: d)
    assert w.resolve_path(1, 77) == Path("/pool/a")
    calls = []

    def rebuild(pid):
        calls.append(pid)
        return d

    monkeypatch.setattr(w, "_build_ino_map", rebuild)
    # Cache hit: same pid must not rebuild /proc maps.
    assert w.resolve_path(1, 88) == Path("/pool/b")
    assert calls == []


def _blocking_proc() -> tuple[mock.MagicMock, threading.Event]:
    """A fake tracer proc whose stdout stays open until ``terminate`` is
    called — mirrors a real subprocess the way the fatrace tests do."""
    stop = threading.Event()

    def lines():
        while not stop.is_set():
            yield b""

    fake_proc = mock.MagicMock()
    fake_proc.stdout = lines()
    fake_proc.stdin = io.StringIO()
    fake_proc.terminate.side_effect = stop.set
    return fake_proc, stop


# ─── tracer subprocess protocol ────────────────────────────────────
def test_enable_streams_events():
    w = _watcher(binary="/nonexistent/dimergio-mmap")
    received: list[tuple[int, int, int]] = []

    def on_event(pid, ino, count):
        received.append((pid, ino, count))

    fake_proc = mock.MagicMock()
    fake_proc.stdout = io.BytesIO(b"101 42 3\n102 43 7\ngarbage line\n100 44 2\n")
    fake_proc.stdin = io.StringIO()
    with mock.patch("dimergio.mmap.subprocess.Popen", return_value=fake_proc):
        assert w.enable(101, on_event)
    assert w._thread is not None
    w._thread.join(timeout=2)
    assert received == [(101, 42, 3), (102, 43, 7), (100, 44, 2)]
    # Reader thread clears the stale proc ref on exit.
    assert w._proc is None


def test_enable_fails_without_binary():
    with mock.patch.object(MmapWatcher, "_find_binary", return_value=None):
        w = MmapWatcher(path_ok=lambda p: True)
    assert not w.available
    assert w.enable(101, lambda *a: None) is False


def test_enable_duplicate_is_idempotent():
    w = _watcher(binary="/fake")
    fake_proc, stop = _blocking_proc()
    with mock.patch("dimergio.mmap.subprocess.Popen", return_value=fake_proc):
        assert w.enable(101, lambda *a: None)
        assert w.enable(101, lambda *a: None)  # no-op
    assert w.is_watching(101)
    assert w.watching() == {101}
    w.stop()
    assert fake_proc.terminate.call_count == 1


def test_disable_stops_when_last_pid():
    w = _watcher(binary="/fake")
    fake_proc, stop = _blocking_proc()
    with mock.patch("dimergio.mmap.subprocess.Popen", return_value=fake_proc):
        w.enable(101, lambda *a: None)
        w.enable(102, lambda *a: None)
    assert fake_proc.terminate.call_count == 0
    w.disable(101)
    assert fake_proc.terminate.call_count == 0  # still watching 102
    w.disable(102)
    assert fake_proc.terminate.call_count == 1  # last watch → stop tracer
    assert not w.watching()
    # Disabling a non-watched pid is a harmless no-op.
    w.disable(999)


def test_set_interval_sends_i_command():
    w = _watcher(binary="/fake")
    fake_proc, stop = _blocking_proc()
    with mock.patch("dimergio.mmap.subprocess.Popen", return_value=fake_proc):
        w.enable(101, lambda *a: None)
        w.set_interval_ms(33)
    assert "i 33\n" in fake_proc.stdin.getvalue()
    w.stop()


# ─── CLI: bare `dimergio` must still expose watch defaults ─────────
def test_parser_no_args_exposes_mmap_pid():
    """`dimergio` with no subcommand runs cmd_watch, so every watch default
    must exist on the namespace (set via parser.set_defaults). Regression for
    AttributeError 'Namespace' object has no attribute 'mmap_pid'."""
    from dimergio.cli import build_parser

    ns = build_parser().parse_args([])
    assert ns.command is None
    assert ns.mmap_pid == []


def test_parser_watch_mmap_pid_accumulates():
    from dimergio.cli import build_parser

    ns = build_parser().parse_args(["watch", "--mmap-pid", "42", "--mmap-pid", "43"])
    assert ns.command == "watch"
    assert ns.mmap_pid == [42, 43]