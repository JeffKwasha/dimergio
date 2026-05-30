"""Read-only tests for the selector module.

These test the formatter functions and Candidate presentation.
The interactive selection flow is tested only for edge cases
that don't require stdin.
"""

from pathlib import Path

from dimergio.model import AnalysisResult, Candidate, Pool
from dimergio.selector import _fmt_bytes as sel_fmt
from dimergio.selector import _recompute_display


def _candidate(pool_path: str, reads: int, iowait: float, cum: float) -> Candidate:
    return Candidate(
        path=pool_path,
        pool_path=pool_path,
        reads=reads,
        iowait_debt=iowait,
        iowait_pct=0.0,
        cum_pct=cum,
        branch_name="slow",
        file_size=1000,
    )


class TestFmtBytes:
    def test_bytes(self):
        assert sel_fmt(500) == "500B"

    def test_kb(self):
        assert sel_fmt(2048) == "2KB"

    def test_mb(self):
        assert sel_fmt(10 * 1024 * 1024) == "10MB"

    def test_gb(self):
        assert sel_fmt(2 * 1024 * 1024 * 1024) == "2.0GB"

    def test_zero(self):
        assert sel_fmt(0) == "0B"

    def test_large(self):
        assert sel_fmt(15000000000) == "14.0GB"


class TestRecomputeDisplay:
    def test_identity_when_empty(self):
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
        ranked = _recompute_display(result, "reads")
        assert len(ranked.candidates) == 0

    def test_sorting_preserves_count(self):
        pool = Pool(mount=Path("/p"), name="P")
        candidates = [
            _candidate("a", 50, 100.0, 50.0),
            _candidate("b", 100, 50.0, 75.0),
            _candidate("c", 25, 200.0, 100.0),
        ]
        result = AnalysisResult(
            candidates=candidates,
            total_reads=175,
            total_iowait=350.0,
            threshold_80_idx=1,
            total_candidate_reads=175,
            total_candidate_iowait=350.0,
            observation_duration_s=100.0,
            pool=pool,
        )
        # Re-sort by reads
        ranked = _recompute_display(result, "reads")
        assert len(ranked.candidates) == 3
        # b (100) should be first, a (50) second, c (25) third
        assert ranked.candidates[0].pool_path == "b"
        assert ranked.candidates[1].pool_path == "a"
        assert ranked.candidates[2].pool_path == "c"

    def test_recompute_density(self):
        pool = Pool(mount=Path("/p"), name="P")
        candidates = [
            _candidate("fast", 100, 10.0, 33.3),
            _candidate("slow", 10, 20.0, 100.0),
        ]
        result = AnalysisResult(
            candidates=candidates,
            total_reads=110,
            total_iowait=30.0,
            threshold_80_idx=0,
            total_candidate_reads=110,
            total_candidate_iowait=30.0,
            observation_duration_s=100.0,
            pool=pool,
        )
        ranked = _recompute_display(result, "density")
        # density = reads / duration; same duration for both => fast has higher density
        assert ranked.candidates[0].pool_path == "fast"
