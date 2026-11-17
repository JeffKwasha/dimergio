"""Tests for the mmap (page-fault) read detection layer.

dimergio cannot rely on fatrace/fanotify for apps that mmap files (llama.cpp
and friends): fanotify does not report mmap accesses.  These tests lock in the
MmapWatcher behaviors — candidate scanning, inode→path resolution, and the
tracer subprocess protocol — without needing a real kernel.
"""

import io
import os
import threading
import time
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
    monkeypatch.setattr(mm, "proc_comm", lambda pid: "" if pid == 999 else f"p{pid}")
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
    monkeypatch.setattr(mm, "proc_comm", lambda pid: "k")
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


# ─── mapping address → path resolution ─────────────────────────────
def test_build_addr_map_skips_deleted():
    w = _watcher()
    maps = (
        "7f00-7f10 r--p 00000000 00:2b 1001 /pool/model.bin (deleted)\n"
        "7f20-7f30 r--p 00000000 00:2b 2002 /pool/model.bin\n"
        "7f40-7f50 rw-p 00000000 00:2b 3003 /other/x\n"
        "7f60-7f70 r--p 00000000 00:2b 4004 [heap]\n"
    )
    with mock.patch("builtins.open", return_value=io.StringIO(maps)):
        d = w._build_addr_map(123)

    assert 0x7F00 not in d  # (deleted) must not be resolved
    assert d[0x7F20] == Path("/pool/model.bin")
    assert d[0x7F40] == Path("/other/x")


def test_resolve_path_caches_per_pid(monkeypatch):
    w = _watcher()
    d = {0x1000: Path("/pool/a"), 0x2000: Path("/pool/b")}
    monkeypatch.setattr(w, "_build_addr_map", lambda pid: d)
    assert w.resolve_path(1, 0x1000) == Path("/pool/a")

    calls = []

    def rebuild(pid):
        calls.append(pid)
        return d

    monkeypatch.setattr(w, "_build_addr_map", rebuild)
    # Cache hit: same pid must not rebuild /proc maps.
    assert w.resolve_path(1, 0x2000) == Path("/pool/b")
    assert calls == []
    # A miss inside the TTL does not rescan either.
    assert w.resolve_path(1, 0x9999) is None
    assert calls == []


def test_resolve_path_rescans_after_ttl(monkeypatch):
    w = _watcher()
    clock = [100.0]
    monkeypatch.setattr(mm.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(w, "_build_addr_map", lambda pid: {0x1000: Path("/pool/a")})
    assert w.resolve_path(1, 0x1000) == Path("/pool/a")

    fresh = {0x2000: Path("/pool/b")}
    monkeypatch.setattr(w, "_build_addr_map", lambda pid: fresh)
    clock[0] += w.MAPS_TTL_S + 1
    assert w.resolve_path(1, 0x2000) == Path("/pool/b")


def _blocking_proc() -> tuple[mock.MagicMock, threading.Event]:
    """A fake tracer proc whose stdout stays open until ``terminate`` is
    called, the way a real tracer behaves until we stop it."""
    stop = threading.Event()

    def lines():
        while not stop.is_set():
            yield b""

    fake_proc = mock.MagicMock()
    fake_proc.stdout = lines()
    fake_proc.stdin = io.BytesIO()
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
    fake_proc.stdin = io.BytesIO()
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
    assert b"i 33\n" in fake_proc.stdin.getvalue()
    w.stop()


# ─── tracer must never touch the TUI's terminal ─────────────────────
def test_tracer_detaches_from_controlling_terminal(monkeypatch):
    """The tracer child must be setid()'d so a (sudo) child can never read
    /dev/tty or restore termios over the TUI's raw mode — the regression
    where `--mmap-pid`/Enter-on-process silently killed all keyboard input."""
    w = _watcher(binary="/fake")
    fake_proc = mock.MagicMock()
    fake_proc.stdout = io.BytesIO(b"")
    fake_proc.stdin = io.BytesIO()
    with mock.patch("dimergio.mmap.subprocess.Popen", return_value=fake_proc) as popen:
        assert w.enable(101, lambda *a: None)
    kwargs = popen.call_args.kwargs
    assert kwargs.get("start_new_session") is True


def test_tracer_sudo_refuses_when_password_required(monkeypatch):
    """If the tracer would run under an interactive sudo (password prompt on
    /dev/tty), refuse to start it instead of corrupting the TUI's terminal."""
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    run = mock.MagicMock(return_value=SimpleNamespace(returncode=1))
    monkeypatch.setattr(mm.subprocess, "run", run)
    popen = mock.MagicMock()
    monkeypatch.setattr(mm.subprocess, "Popen", popen)
    w = MmapWatcher(path_ok=lambda p: True, binary="/fake", use_sudo=True)
    assert w.enable(101, lambda *a: None) is False
    run.assert_called_once_with(["sudo", "-n", "true"])
    popen.assert_not_called()


def test_tracer_sudo_runs_passwordless_and_detached(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(mm.subprocess, "run", mock.MagicMock(return_value=SimpleNamespace(returncode=0)))
    fake_proc = mock.MagicMock()
    fake_proc.stdout = io.BytesIO(b"")
    fake_proc.stdin = io.BytesIO()
    popen = mock.MagicMock(return_value=fake_proc)
    monkeypatch.setattr(mm.subprocess, "Popen", popen)
    w = MmapWatcher(path_ok=lambda p: True, binary="/fake", use_sudo=True)
    assert w.enable(101, lambda *a: None) is True
    assert popen.call_args.args[0] == ["sudo", "/fake", "--interval-ms", "10"]
    assert popen.call_args.kwargs.get("start_new_session") is True


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

def test_send_writes_bytes_to_the_tracer():
    """The pipes are binary; a str write raised TypeError at runtime."""
    import subprocess
    import sys

    echo = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.readline())"]
    proc = subprocess.Popen(echo, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    w = _watcher(binary="/bin/true")
    w._proc = proc
    try:
        w._send("+42\n")
        assert proc.stdout.readline() == b"+42\n"
    finally:
        proc.terminate()
        proc.wait(timeout=2)


def test_watcher_runs_the_tracer_and_reports_faults(tmp_path):
    """End to end over a real child process: start, command, events, stop."""
    import time

    tracer = tmp_path / "fake-tracer"
    tracer.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "for line in sys.stdin:\n"
        "    if line.startswith('+'):\n"
        "        pid = line[1:].strip()\n"
        "        print(f'{pid} 4242 7', flush=True)\n"
    )
    tracer.chmod(0o755)

    events = []
    w = _watcher(binary=str(tracer))
    try:
        assert w.enable(1234, lambda pid, ino, count: events.append((pid, ino, count)))
        for _ in range(200):
            if events:
                break
            time.sleep(0.01)
    finally:
        w.stop()

    assert events and events[0] == (1234, 4242, 7)


# ─── a tracer that dies must be reported with its reason ────────────
def _script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text("#!/usr/bin/env python3\n" + body)
    path.chmod(0o755)
    return path


def test_tracer_death_is_reported_with_its_reason(tmp_path, caplog):
    dying = _script(
        tmp_path,
        "dying-tracer",
        "import sys\n"
        "print('libbpf: failed to load BPF skeleton: -EPERM', file=sys.stderr, flush=True)\n"
        "sys.exit(1)\n",
    )
    failures: list[str] = []
    w = MmapWatcher(
        path_ok=lambda p: True,
        binary=str(dying),
        on_failure=failures.append,
    )
    try:
        assert w.enable(101, lambda *a: None)
        for _ in range(200):
            if failures:
                break
            time.sleep(0.01)
    finally:
        w.stop()

    assert failures == ["libbpf: failed to load BPF skeleton: -EPERM"]
    assert w.last_error == failures[0]
    assert not w.watching()
    assert any(
        "mmap tracing failed" in r.message and "-EPERM" in r.message
        for r in caplog.records
        if r.levelname == "WARNING"
    )


def test_requested_stop_is_not_a_failure(tmp_path):
    healthy = _script(
        tmp_path,
        "healthy-tracer",
        "import sys\nimport time\n"
        "for line in sys.stdin:\n"
        "    pass\n",
    )
    failures: list[str] = []
    w = MmapWatcher(path_ok=lambda p: True, binary=str(healthy), on_failure=failures.append)
    assert w.enable(101, lambda *a: None)
    w.stop()

    assert failures == []


def test_missing_tracer_says_how_to_build_it(monkeypatch):
    monkeypatch.delenv("DIMERGIO_MMAP_BIN", raising=False)
    monkeypatch.setattr(mm.shutil, "which", lambda name: None)
    w = MmapWatcher(path_ok=lambda p: True)
    assert w.enable(101, lambda *a: None) is False
    assert "make -C src/dimergio/bpf" in (w.last_error or "")


def test_root_refuses_a_writable_tracer(monkeypatch, tmp_path):
    tracer = _script(tmp_path, "tracer", "import time\n")
    tracer.chmod(0o777)
    monkeypatch.setattr(mm.os, "geteuid", lambda: 0)
    monkeypatch.setenv("DIMERGIO_MMAP_BIN", str(tracer))
    w = MmapWatcher(path_ok=lambda p: True)

    assert w.enable(101, lambda *a: None) is False
    assert "writable tracer" in (w.last_error or "")
