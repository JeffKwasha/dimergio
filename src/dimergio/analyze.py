from __future__ import annotations

from pathlib import Path

from .collector import _effective_size_bytes
from .model import AnalysisResult, Candidate, FileAccumulator, Pool


def analyze(
    accumulators: dict[Path, FileAccumulator],
    pool: Pool,
    data_path: Path | None = None,
    force_move: bool = False,
) -> AnalysisResult:
    dp = data_path or pool.mount
    fastest = pool.fastest_branch
    fastest_idx = pool.branches.index(fastest) if fastest in pool.branches else -1

    observation_duration = 0.0
    if accumulators:
        first = min(a.first_seen for a in accumulators.values())
        last = max(a.last_seen for a in accumulators.values())
        observation_duration = last - first if last > first else 0.0

    total_reads = 0
    total_iowait = 0.0
    raw: list[Candidate] = []

    for acc in accumulators.values():
        if not force_move and acc.branch_idx == fastest_idx:
            continue  # already on fastest branch
        if acc.write_count > 0:
            continue  # files written during observation are ineligible to move

        rel = _safe_relative(acc.path, dp)
        try:
            file_size = acc.path.stat().st_size
        except OSError:
            file_size = 0

        effective_size = _effective_size_bytes(file_size)
        iowait_per_mb = acc.iowait_debt / (effective_size / 1_000_000)

        total_reads += acc.total_reads
        total_iowait += acc.iowait_debt

        raw.append(Candidate(
            path=acc.path,
            pool_path=rel,
            reads=acc.total_reads,
            iowait_debt=acc.iowait_debt,
            iowait_pct=0.0,
            cum_pct=0.0,
            branch_name=pool.branches[acc.branch_idx].label,
            file_size=file_size,
            effective_size=effective_size,
            iowait_per_mb=iowait_per_mb,
        ))

    if not raw:
        return AnalysisResult(
            candidates=[],
            total_reads=0,
            total_iowait=0.0,
            threshold_80_idx=0,
            total_candidate_reads=0,
            total_candidate_iowait=0.0,
            observation_duration_s=observation_duration,
            pool=pool,
        )

    # Sort by iowait cost per MB (byte-weighted) descending
    raw.sort(key=lambda c: c.iowait_per_mb, reverse=True)

    # Compute percentages
    for c in raw:
        c.iowait_pct = (c.iowait_debt / total_iowait * 100) if total_iowait > 0 else 0.0

    cum = 0.0
    threshold_80_idx = 0
    for i, c in enumerate(raw):
        cum += c.iowait_debt
        c.cum_pct = (cum / total_iowait * 100) if total_iowait > 0 else 0.0
        if c.cum_pct < 80.0:
            threshold_80_idx = i

    total_candidate_iowait = total_iowait
    total_candidate_reads = total_reads

    return AnalysisResult(
        candidates=raw,
        total_reads=total_reads,
        total_iowait=total_iowait,
        threshold_80_idx=threshold_80_idx,
        total_candidate_reads=total_candidate_reads,
        total_candidate_iowait=total_candidate_iowait,
        observation_duration_s=observation_duration,
        pool=pool,
    )


def rank_by_reads(result: AnalysisResult) -> AnalysisResult:
    """Return a new AnalysisResult sorted by read count instead of iowait."""
    sorted_candidates = sorted(result.candidates, key=lambda c: c.reads, reverse=True)
    total_iowait = result.total_iowait
    total_reads = result.total_reads

    cum_iowait = 0.0
    cum_reads = 0
    threshold_80 = 0
    for i, c in enumerate(sorted_candidates):
        cum_iowait += c.iowait_debt
        c.cum_pct = (cum_iowait / total_iowait * 100) if total_iowait > 0 else 0.0
        if cum_reads / total_reads * 100 < 80.0 if total_reads > 0 else True:
            threshold_80 = i
        cum_reads += c.reads
        c.iowait_pct = (c.iowait_debt / total_iowait * 100) if total_iowait > 0 else 0.0

    return AnalysisResult(
        candidates=sorted_candidates,
        total_reads=result.total_reads,
        total_iowait=result.total_iowait,
        threshold_80_idx=threshold_80,
        total_candidate_reads=result.total_candidate_reads,
        total_candidate_iowait=result.total_candidate_iowait,
        observation_duration_s=result.observation_duration_s,
        pool=result.pool,
    )


def rank_by_density(result: AnalysisResult) -> AnalysisResult:
    """Sort by read density (reads/second)."""
    dur = result.observation_duration_s or 1.0
    sorted_candidates = sorted(
        result.candidates,
        key=lambda c: c.reads / dur,
        reverse=True,
    )
    total_iowait = result.total_iowait
    cum = 0.0
    threshold_80 = 0
    for i, c in enumerate(sorted_candidates):
        cum += c.iowait_debt
        c.cum_pct = (cum / total_iowait * 100) if total_iowait > 0 else 0.0
        c.iowait_pct = (c.iowait_debt / total_iowait * 100) if total_iowait > 0 else 0.0
        if c.cum_pct < 80.0:
            threshold_80 = i

    return AnalysisResult(
        candidates=sorted_candidates,
        total_reads=result.total_reads,
        total_iowait=result.total_iowait,
        threshold_80_idx=threshold_80,
        total_candidate_reads=result.total_candidate_reads,
        total_candidate_iowait=result.total_candidate_iowait,
        observation_duration_s=result.observation_duration_s,
        pool=result.pool,
    )


def _safe_relative(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return path.name
