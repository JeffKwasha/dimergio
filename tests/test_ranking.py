"""Byte-weighted iowait ranking tests.

Covers: the SSD block-size constant + effective-size rounding, the
iowait-per-MB sort metric, exclusion of files written during the run, and
the Left/Right sort-column rotation.
"""

from pathlib import Path

from dimergio.analyze import analyze
from dimergio.collector import SSD_BLOCK_BYTES, _cycle_sort_key, _effective_size_bytes
from dimergio.model import Branch, FileAccumulator, Pool


def _pool():
    hdd = Branch(path=Path("/hdd"), device="dm-0", rotational=True, free_bytes=100)
    ssd = Branch(path=Path("/ssd"), device="dm-1", rotational=False, free_bytes=50)
    return Pool(mount=Path("/pool"), name="POOL", branches=[hdd, ssd])


def _acc(path: Path, iowait: float, size: int, branch_idx: int = 0, writes: int = 0) -> FileAccumulator:
    path.write_bytes(b"x" * size)
    acc = FileAccumulator(path=path, branch_idx=branch_idx, iowait_debt=iowait)
    acc.write_count = writes
    return acc


# ─── SSD block-size constant + effective size ───────────────────────
def test_ssd_block_constant_is_128k():
    assert SSD_BLOCK_BYTES == 128 * 1024


def test_effective_size_rounds_up_to_block():
    blk = SSD_BLOCK_BYTES
    assert _effective_size_bytes(0) == blk
    assert _effective_size_bytes(1) == blk
    assert _effective_size_bytes(blk) == blk
    assert _effective_size_bytes(blk + 1) == 2 * blk
    assert _effective_size_bytes(blk * 3 - 1) == 3 * blk


# ─── analyze: iowait-per-MB ranking + written-file exclusion ───────
def test_analyze_sorts_by_iowait_per_mb(tmp_path):
    # Small file, modest iowait → high iowait/MB.
    small = _acc(tmp_path / "small.bin", 1.0, size=128 * 1024, branch_idx=0)
    # Huge file, same iowait → low iowait/MB.
    huge = _acc(tmp_path / "huge.bin", 1.0, size=128 * 1024 * 1000, branch_idx=0)

    result = analyze({small.path: small, huge.path: huge}, _pool(), data_path=tmp_path)
    assert result.candidates[0].path == small.path  # higher iowait/MB first
    assert result.candidates[0].iowait_per_mb > result.candidates[1].iowait_per_mb


def test_analyze_excludes_written_files(tmp_path):
    read_only = _acc(tmp_path / "ro.bin", 5.0, size=256 * 1024, branch_idx=0)
    written = _acc(tmp_path / "w.bin", 99.0, size=256 * 1024, branch_idx=0, writes=3)

    result = analyze({read_only.path: read_only, written.path: written}, _pool(), data_path=tmp_path)
    paths = {c.path for c in result.candidates}
    assert read_only.path in paths
    assert written.path not in paths


def test_analyze_populates_effective_size_and_per_mb(tmp_path):
    acc = _acc(tmp_path / "f.bin", 2.0, size=128 * 1024, branch_idx=0)
    result = analyze({acc.path: acc}, _pool(), data_path=tmp_path)
    c = result.candidates[0]
    assert c.effective_size == 128 * 1024
    assert c.iowait_per_mb == 2.0 / (128 * 1024 / 1_000_000)


# ─── sort-column rotation ──────────────────────────────────────────
def test_cycle_sort_key_rotates():
    keys = ("iowait_per_mb", "iowait", "reads")
    assert _cycle_sort_key("iowait_per_mb", 1, keys) == "iowait"
    assert _cycle_sort_key("iowait", 1, keys) == "reads"
    # wraps around
    assert _cycle_sort_key("reads", 1, keys) == "iowait_per_mb"
    # left is the inverse of right
    assert _cycle_sort_key("iowait_per_mb", -1, keys) == "reads"
    assert _cycle_sort_key("reads", -1, keys) == "iowait"
