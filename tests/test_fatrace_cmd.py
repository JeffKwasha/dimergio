"""The fatrace command line is a contract with the line parser.

fatrace prints hour:minute:second.microsecond with one -t and epoch seconds
with two. The parser reads epoch seconds, so the duplicate -t is required.
"""

from pathlib import Path

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
