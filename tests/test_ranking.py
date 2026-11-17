"""Byte-weighted ranking helpers: the SSD block-size constant,
effective-size rounding, and the Left/Right sort-column rotation.
"""

from dimergio.collector import _cycle_sort_key
from dimergio.model import SSD_BLOCK_BYTES, effective_size_bytes


# ─── SSD block-size constant + effective size ───────────────────────
def test_ssd_block_constant_is_128k():
    assert SSD_BLOCK_BYTES == 128 * 1024


def test_effective_size_rounds_up_to_block():
    blk = SSD_BLOCK_BYTES
    assert effective_size_bytes(0) == blk
    assert effective_size_bytes(1) == blk
    assert effective_size_bytes(blk) == blk
    assert effective_size_bytes(blk + 1) == 2 * blk
    assert effective_size_bytes(blk * 3 - 1) == 3 * blk


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
