"""Tests for canonical path resolution, the symlink display map, and
move-time source validation.

These cover the real-world failure where a tracked path only existed as a
mergerfs/symlink artifact: moves used to fail with a wrong branch path. The
fix canonicalizes tracked paths to the real file and shows a human-readable
symlink name in the UI while keeping the real path for the move.
"""

import os
from pathlib import Path

from dimergio.collector import Collector, IOWaitSampler
from dimergio.model import Branch, Pool, ReadEvent
from dimergio.mover import _resolve_source
from dimergio.pool import IO_Domain


def _pool(root: Path) -> Pool:
    for name in ("branch_a", "branch_b", "pool"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return Pool(
        mount=root / "pool",
        name="TEST",
        branches=[
            Branch(path=root / "branch_a", device="dm-0", rotational=True),
            Branch(path=root / "branch_b", device="dm-1", rotational=False),
        ],
    )


def _collector(root: Path, symlink_depth: int = 3) -> Collector:
    return Collector(pool=_pool(root), data_path=root / "pool", symlink_depth=symlink_depth)


def _real_file(root: Path) -> Path:
    real = root / "branch_a" / "data" / "real.bin"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_bytes(b"x" * 16)
    return real


# ─── symlink display map (built once at startup, shortest wins) ─────
def test_symlink_map_shortest_wins(tmp_path):
    real = _real_file(tmp_path)
    (tmp_path / "pool" / "alias1").mkdir(parents=True)
    (tmp_path / "pool" / "alias2" / "deep").mkdir(parents=True)
    (tmp_path / "pool" / "alias1" / "xx.bin").symlink_to(real)
    (tmp_path / "pool" / "alias2" / "deep" / "yy.bin").symlink_to(real)

    c = _collector(tmp_path)
    assert c._symlink_map[tmp_path / "pool" / "data" / "real.bin"] == "alias1/xx.bin"


def test_symlink_map_depth_bound(tmp_path):
    real = _real_file(tmp_path)
    (tmp_path / "pool" / "a" / "b" / "c").mkdir(parents=True)
    (tmp_path / "pool" / "a" / "link.bin").symlink_to(real)
    (tmp_path / "pool" / "a" / "b" / "c" / "deep.bin").symlink_to(real)

    c = _collector(tmp_path, symlink_depth=2)
    assert c._symlink_map.get(tmp_path / "pool" / "data" / "real.bin") == "a/link.bin"


def test_symlink_map_disabled(tmp_path):
    real = _real_file(tmp_path)
    (tmp_path / "pool" / "m").mkdir(parents=True)
    (tmp_path / "pool" / "m" / "x.bin").symlink_to(real)

    c = _collector(tmp_path, symlink_depth=0)
    assert c._symlink_map == {}


# ─── display name resolution ────────────────────────────────────────
def test_display_name_uses_symlink(tmp_path):
    real = _real_file(tmp_path)
    (tmp_path / "pool" / "models").mkdir(parents=True)
    (tmp_path / "pool" / "models" / "MyModel.gguf").symlink_to(real)

    c = _collector(tmp_path)
    assert c._display_name_for(tmp_path / "pool" / "data" / "real.bin") == "models/MyModel.gguf"


def test_display_name_fallback(tmp_path):
    f = tmp_path / "branch_a" / "plain" / "file.bin"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x")

    c = _collector(tmp_path)
    assert c._display_name_for(tmp_path / "pool" / "plain" / "file.bin") == "plain/file.bin"


def test_display_name_disabled(tmp_path):
    real = _real_file(tmp_path)
    (tmp_path / "pool" / "m").mkdir(parents=True)
    (tmp_path / "pool" / "m" / "x.bin").symlink_to(real)

    c = _collector(tmp_path, symlink_depth=0)
    assert c._display_name_for(tmp_path / "pool" / "data" / "real.bin") == "data/real.bin"


def test_accumulate_sets_display_name(tmp_path):
    real = _real_file(tmp_path)
    (tmp_path / "pool" / "models").mkdir(parents=True)
    (tmp_path / "pool" / "models" / "M.bin").symlink_to(real)

    c = _collector(tmp_path)
    ev = ReadEvent(
        file_path=tmp_path / "pool" / "data" / "real.bin",
        pid=1,
        process_name="app",
        uid=os.getuid(),
        gid=1000,
        timestamp=1.0,
        branch_idx=0,
        iowait_sec=0.0,
    )
    c._accumulate(ev, IOWaitSampler([IO_Domain(devices=[])], [0], 10))
    acc = c._accumulators[tmp_path / "pool" / "data" / "real.bin"]
    assert acc.display_name == "models/M.bin"


# ─── canonicalization ───────────────────────────────────────────────
def test_canonicalize_symlink_to_real(tmp_path):
    real = _real_file(tmp_path)
    (tmp_path / "pool" / "models").mkdir(parents=True)
    (tmp_path / "pool" / "models" / "M.bin").symlink_to(real)

    c = _collector(tmp_path)
    assert c._resolve_tracked_path(tmp_path / "pool" / "models" / "M.bin") == tmp_path / "pool" / "data" / "real.bin"


def test_canonicalize_outside_pool_returns_none(tmp_path):
    (tmp_path / "pool" / "models").mkdir(parents=True)
    (tmp_path / "pool" / "models" / "bad.bin").symlink_to("/etc/hostname")

    c = _collector(tmp_path)
    assert c._resolve_tracked_path(tmp_path / "pool" / "models" / "bad.bin") is None


# ─── branch resolution ──────────────────────────────────────────────
def test_resolve_branch_none_when_unlocatable(tmp_path):
    _real_file(tmp_path)

    c = _collector(tmp_path)
    assert c._resolve_branch(tmp_path / "pool" / "ghost" / "x.bin") is None
    assert c._resolve_branch(tmp_path / "pool" / "data" / "real.bin") == 0


def test_parse_line_drops_unresolvable_read(tmp_path):
    _real_file(tmp_path)
    c = _collector(tmp_path)

    ghost = f"1748573021.456789 app(123) [{os.getuid()}:1000]: R {tmp_path / 'pool' / 'ghost' / 'x.bin'}"
    assert c._parse_line(ghost) is None

    real = f"1748573021.456789 app(123) [{os.getuid()}:1000]: R {tmp_path / 'pool' / 'data' / 'real.bin'}"
    ev = c._parse_line(real)
    assert ev is not None
    assert ev.branch_idx == 0


# ─── move-time source resolution ────────────────────────────────────
def test_mover_resolve_source_symlink(tmp_path):
    pool = _pool(tmp_path)
    real = tmp_path / "branch_b" / "data" / "real.bin"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_bytes(b"x" * 16)
    (tmp_path / "branch_a" / "data").mkdir(parents=True)
    (tmp_path / "branch_a" / "data" / "link.bin").symlink_to(real)

    src, rel = _resolve_source(pool, pool.branches[0], Path("data/link.bin"))
    assert src == real
    assert rel == Path("data/real.bin")


def test_mover_resolve_source_missing_returns_unchanged(tmp_path):
    pool = _pool(tmp_path)
    src, rel = _resolve_source(pool, pool.branches[0], Path("data/missing.bin"))
    assert src == tmp_path / "branch_a" / "data" / "missing.bin"
    assert rel == Path("data/missing.bin")


# ─── config ─────────────────────────────────────────────────────────
def test_config_default_symlink_depth():
    from dimergio.config import DEFAULTS

    assert DEFAULTS["symlink_depth"] == 3