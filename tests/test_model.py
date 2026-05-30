from pathlib import Path

from dimergio.model import (
    AnalysisResult,
    Branch,
    Candidate,
    FileAccumulator,
    MoveEntry,
    Pool,
    ReadEvent,
)


class TestBranch:
    def test_label(self):
        b = Branch(path=Path("/mnt/ssd"), device="dm-7", rotational=False)
        assert b.label == "ssd"

    def test_device_stat_path(self):
        b = Branch(path=Path("/mnt/ssd"), device="dm-7", rotational=False)
        assert b.device_stat_path == Path("/sys/block/dm-7/stat")

    def test_device_stat_path_empty(self):
        b = Branch(path=Path("/mnt/slow"), device="", rotational=True)
        assert b.device_stat_path == Path("/sys/block//stat")


class TestPool:
    def test_fastest_branch_prefers_non_rotational(self):
        hdd = Branch(path=Path("/hdd"), device="dm-0", rotational=True, free_bytes=100)
        ssd = Branch(path=Path("/ssd"), device="dm-1", rotational=False, free_bytes=50)
        pool = Pool(mount=Path("/pool"), name="POOL", branches=[hdd, ssd])
        assert pool.fastest_branch == ssd

    def test_fastest_branch_picks_largest_ssd(self):
        small = Branch(path=Path("/small"), device="dm-0", rotational=False, free_bytes=50)
        large = Branch(path=Path("/large"), device="dm-1", rotational=False, free_bytes=200)
        pool = Pool(mount=Path("/pool"), name="POOL", branches=[small, large])
        assert pool.fastest_branch == large

    def test_fastest_branch_falls_back_to_hdd(self):
        h1 = Branch(path=Path("/h1"), device="dm-0", rotational=True, free_bytes=100)
        h2 = Branch(path=Path("/h2"), device="dm-1", rotational=True, free_bytes=300)
        pool = Pool(mount=Path("/pool"), name="POOL", branches=[h1, h2])
        assert pool.fastest_branch == h2

    def test_fastest_branch_no_branches(self):
        pool = Pool(mount=Path("/pool"), name="POOL")
        assert pool.fastest_branch is None


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


class TestCandidate:
    def test_size_display_bytes(self):
        c = Candidate(
            path=Path("/f"),
            pool_path="f",
            reads=1,
            iowait_debt=0.0,
            iowait_pct=0.0,
            cum_pct=0.0,
            branch_name="test",
            file_size=500,
        )
        assert c.size_display == "500B"

    def test_size_display_kb(self):
        c = Candidate(
            path=Path("/f"),
            pool_path="f",
            reads=1,
            iowait_debt=0.0,
            iowait_pct=0.0,
            cum_pct=0.0,
            branch_name="test",
            file_size=2048,
        )
        assert c.size_display == "2KB"

    def test_size_display_mb(self):
        c = Candidate(
            path=Path("/f"),
            pool_path="f",
            reads=1,
            iowait_debt=0.0,
            iowait_pct=0.0,
            cum_pct=0.0,
            branch_name="test",
            file_size=5 * 1024 * 1024,
        )
        assert c.size_display == "5MB"

    def test_size_display_gb(self):
        c = Candidate(
            path=Path("/f"),
            pool_path="f",
            reads=1,
            iowait_debt=0.0,
            iowait_pct=0.0,
            cum_pct=0.0,
            branch_name="test",
            file_size=3 * 1024 * 1024 * 1024,
        )
        assert c.size_display == "3.0GB"


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
            device_busy_pct=75.0,
        )
        assert e.file_path.name == "f.dat"
        assert e.device_busy_pct == 75.0
        assert e.process_name == "myapp"


class TestAnalysisResult:
    def test_empty_candidates(self):
        pool = Pool(mount=Path("/p"), name="P")
        result = AnalysisResult(
            candidates=[],
            total_reads=0,
            total_iowait=0.0,
            threshold_80_idx=0,
            total_candidate_reads=0,
            total_candidate_iowait=0.0,
            observation_duration_s=0.0,
            pool=pool,
        )
        assert len(result.candidates) == 0
