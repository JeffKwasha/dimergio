from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class Branch:
    path: Path
    device: str           # e.g. "dm-3"
    rotational: bool      # True = HDD, False = SSD/NVMe
    total_bytes: int = 0
    free_bytes: int = 0

    @property
    def label(self) -> str:
        return self.path.name

    @property
    def device_stat_path(self) -> Path:
        return Path(f"/sys/block/{self.device}/stat")


@dataclass(slots=True)
class Pool:
    mount: Path
    name: str             # fsname from mount options (e.g. "GAMMAS")
    branches: list[Branch] = field(default_factory=list)

    @property
    def fastest_branch(self) -> Branch | None:
        ssd = [b for b in self.branches if not b.rotational]
        if ssd:
            return max(ssd, key=lambda b: b.free_bytes)
        if self.branches:
            return max(self.branches, key=lambda b: b.free_bytes)
        return None


@dataclass(slots=True)
class ReadEvent:
    file_path: Path
    pid: int
    process_name: str
    uid: int
    gid: int
    timestamp: float
    branch_idx: int
    device_busy_pct: float


@dataclass(slots=True)
class FileAccumulator:
    path: Path
    branch_idx: int
    total_reads: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0
    iowait_debt: float = 0.0

    def observe(self, ts: float, busy_pct: float) -> None:
        self.total_reads += 1
        self.iowait_debt += busy_pct
        if not self.first_seen:
            self.first_seen = ts
        self.last_seen = ts


@dataclass(slots=True)
class Candidate:
    path: Path
    pool_path: str          # relative path within pool
    reads: int
    iowait_debt: float
    iowait_pct: float       # % of total iowait
    cum_pct: float          # cumulative % across all candidates
    branch_name: str
    file_size: int

    @property
    def size_display(self) -> str:
        b = self.file_size
        if b >= 1 << 30:
            return f"{b / (1<<30):.1f}GB"
        if b >= 1 << 20:
            return f"{b / (1<<20):.0f}MB"
        if b >= 1 << 10:
            return f"{b / (1<<10):.0f}KB"
        return f"{b}B"


@dataclass(slots=True)
class MoveEntry:
    pool_path: str
    source_branch: str
    target_branch: str
    original_basename: str
    renamed_basename: str
    moved_at: str           # ISO-8601
    file_size: int
    verified_working: bool | None = None


@dataclass(slots=True)
class AnalysisResult:
    candidates: list[Candidate]
    total_reads: int
    total_iowait: float
    threshold_80_idx: int   # index into candidates where 80% is reached
    total_candidate_reads: int
    total_candidate_iowait: float
    observation_duration_s: float
    pool: Pool
