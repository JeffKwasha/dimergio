"""The fatrace command line is a contract with the line parser and the host.

fatrace prints hour:minute:second.microsecond with one -t and epoch seconds
with two. The parser reads epoch seconds, so the duplicate -t is required.
The binary on PATH wins over the distro path, so a local build or a setcap
install works without root-owned /usr/sbin.
"""

from pathlib import Path
from unittest import mock

from dimergio.collector import Collector
from dimergio.model import Branch, Pool


def _collector(tmp_path: Path) -> Collector:
    pool = Pool(
        mount=tmp_path / "pool",
        name="TEST",
        branches=[Branch(path=tmp_path / "a", device="dm-0", rotational=True)],
    )
    return Collector(pool=pool, data_path=tmp_path / "pool")


def test_fatrace_uses_double_timestamp_for_epoch_seconds(tmp_path):
    cmd = _collector(tmp_path)._build_fatrace_cmd()

    assert cmd.count("-t") == 2
    assert cmd[1:3] == ["-f", "RW"]
    assert "-u" in cmd


def test_fatrace_has_no_pid_filter(tmp_path):
    cmd = _collector(tmp_path)._build_fatrace_cmd()

    assert "-p" not in cmd


def test_fatrace_comes_from_path(tmp_path):
    with mock.patch("shutil.which", return_value="/home/me/bin/fatrace"):
        cmd = _collector(tmp_path)._build_fatrace_cmd()

    assert cmd[0] == "/home/me/bin/fatrace"


def test_fatrace_falls_back_to_the_distro_path(tmp_path):
    with mock.patch("shutil.which", return_value=None):
        cmd = _collector(tmp_path)._build_fatrace_cmd()

    assert cmd[0] == "/usr/sbin/fatrace"
