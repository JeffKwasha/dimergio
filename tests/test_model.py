from pathlib import Path

from dimergio.model import (
    Branch,
    FileAccumulator,
    MoveEntry,
    ReadEvent,
)


class TestBranch:
    def test_label(self):
        b = Branch(path=Path("/mnt/ssd"), device="dm-7", rotational=False)
        assert b.label == "ssd"


class TestFileAccumulator:
    def test_observe_first_event(self):
        acc = FileAccumulator(path=Path("/f"), branch_idx=0)
        acc.observe(100.0, 50.0)
        assert acc.total_reads == 1
        assert acc.iowait_debt == 50.0
        assert acc.first_seen == 100.0
        assert acc.last_seen == 100.0

    def test_observe_multiple_events(self):
        acc = FileAccumulator(path=Path("/f"), branch_idx=0)
        acc.observe(100.0, 10.0)
        acc.observe(200.0, 20.0)
        acc.observe(300.0, 30.0)
        assert acc.total_reads == 3
        assert acc.iowait_debt == 60.0
        assert acc.first_seen == 100.0
        assert acc.last_seen == 300.0

    def test_observe_zero_busy(self):
        acc = FileAccumulator(path=Path("/f"), branch_idx=0)
        acc.observe(100.0, 0.0)
        assert acc.total_reads == 1
        assert acc.iowait_debt == 0.0

    def test_observe_n_counts_aggregated_events(self):
        # Aggregated mmap page-fault windows: N faults == N reads.
        acc = FileAccumulator(path=Path("/f"), branch_idx=0)
        acc.observe_n(100.0, 50.0, 5)
        assert acc.total_reads == 5
        assert acc.iowait_debt == 250.0
        assert acc.first_seen == 100.0
        assert acc.last_seen == 100.0

    def test_observe_n_keeps_first_seen(self):
        acc = FileAccumulator(path=Path("/f"), branch_idx=0)
        acc.observe_n(100.0, 0.0, 2)
        acc.observe_n(200.0, 1.0, 3)
        assert acc.total_reads == 5
        assert acc.iowait_debt == 3.0
        assert acc.first_seen == 100.0
        assert acc.last_seen == 200.0


class TestMoveEntry:
    def test_default_verified_none(self):
        e = MoveEntry(
            pool_path="data/f",
            source_branch="hdd",
            target_branch="ssd",
            original_basename="f",
            renamed_basename="_dimergio_f",
            moved_at="2026-05-30T12:00:00",
            file_size=1000,
        )
        assert e.verified_working is None

    def test_verified_true(self):
        e = MoveEntry(
            pool_path="data/f",
            source_branch="hdd",
            target_branch="ssd",
            original_basename="f",
            renamed_basename="_dimergio_f",
            moved_at="2026-05-30T12:00:00",
            file_size=1000,
            verified_working=True,
        )
        assert e.verified_working is True


class TestReadEvent:
    def test_fields(self):
        e = ReadEvent(
            file_path=Path("/mnt/pool/data/f.dat"),
            pid=1234,
            process_name="myapp",
            uid=1000,
            gid=1000,
            timestamp=1000000.0,
            branch_idx=0,
            iowait_sec=0.075,
        )
        assert e.file_path.name == "f.dat"
        assert e.iowait_sec == 0.075
        assert e.process_name == "myapp"
