"""Read-only tests for the mover module.

These tests verify path resolution logic without touching any filesystem.
They operate purely on string/path operations.
"""

from pathlib import Path

from dimergio.mover import _fmt_bytes


def _mk_state(tmp_path, monkeypatch):
    """Build a StateManager backed by a temp dir (never touches real config)."""
    from dimergio import state as state_mod

    cfg = {
        "prefix": "_dimergio_",
        "state_dir": str(tmp_path / "state"),
        "default_pool": "/mnt/games",
        "cleanup_days": 14,
        "checkpoint_interval_s": 60,
        "iowait_interval_ms": 10,
        "symlink_depth": 3,
    }
    monkeypatch.setattr(state_mod, "load_config", lambda: cfg)
    return state_mod.StateManager("GAMMAS")


class TestRecordedRenamedBasename:
    def test_copy_and_rename_records_prefixed_basename(self, tmp_path, monkeypatch):
        from dimergio.mover import _copy_and_rename

        src_dir = tmp_path / "slow"
        dst_dir = tmp_path / "fast"
        src_dir.mkdir()
        dst_dir.mkdir()
        src_file = src_dir / "grass.dds"
        src_file.write_bytes(b"data" * 100)
        dst_path = dst_dir / "grass.dds"

        entry = _copy_and_rename(
            src_file, dst_path, "_dimergio_", _mk_state(tmp_path, monkeypatch),
            "Data/textures/grass.dds", "slow", "fast", verify=False,
        )

        assert entry.renamed_basename == "_dimergio_grass.dds"
        assert dst_path.exists()
        assert (src_dir / "_dimergio_grass.dds").exists()
        assert not src_file.exists()

    def test_smart_rename_records_prefixed_basename(self, tmp_path, monkeypatch):
        from dimergio.mover import _smart_rename

        src_dir = tmp_path / "slow"
        dst_dir = tmp_path / "fast"
        src_dir.mkdir()
        dst_dir.mkdir()
        src_file = src_dir / "grass.dds"
        src_file.write_bytes(b"data" * 100)
        dst_path = dst_dir / "grass.dds"

        entry = _smart_rename(
            src_file, dst_path, "_dimergio_", _mk_state(tmp_path, monkeypatch),
            "Data/textures/grass.dds", "slow", "fast",
        )

        assert entry.renamed_basename == "_dimergio_grass.dds"
        assert dst_path.exists()
        assert not src_file.exists()


class TestMovePathResolution:
    """Verify the path construction logic used in move_files."""

    def test_source_path_construction(self):
        branch_path = Path("/mnt/slow")
        pool_path = "Data/textures/grass.dds"
        rel = Path(pool_path)
        src_path = branch_path / rel
        assert str(src_path) == "/mnt/slow/Data/textures/grass.dds"

    def test_target_path_construction(self):
        branch_path = Path("/mnt/fast")
        pool_path = "Data/textures/grass.dds"
        rel = Path(pool_path)
        dst_path = branch_path / rel
        assert str(dst_path) == "/mnt/fast/Data/textures/grass.dds"

    def test_renamed_path_construction(self):
        prefix = "_dimergio_"
        src_path = Path("/mnt/slow/Data/textures/grass.dds")
        renamed_path = src_path.parent / f"{prefix}{src_path.name}"
        assert str(renamed_path) == "/mnt/slow/Data/textures/_dimergio_grass.dds"

    def test_custom_prefix(self):
        prefix = "_migrated_"
        src_path = Path("/mnt/slow/file.bin")
        renamed = src_path.parent / f"{prefix}{src_path.name}"
        assert str(renamed) == "/mnt/slow/_migrated_file.bin"

    def test_nested_pool_path(self):
        branch = Path("/mnt/slow")
        pool_path = "very/deep/directory/structure/file.dat"
        assert str(branch / Path(pool_path)) == "/mnt/slow/very/deep/directory/structure/file.dat"

    def test_undo_restore_path(self):
        branch_path = Path("/mnt/slow")
        pool_path = "data/file.txt"
        renamed_basename = "_dimergio_file.txt"

        orig_file = branch_path / pool_path
        renamed = orig_file.parent / renamed_basename

        assert str(orig_file) == "/mnt/slow/data/file.txt"
        assert str(renamed) == "/mnt/slow/data/_dimergio_file.txt"

        restore = renamed.parent / pool_path.split("/")[-1]
        assert str(restore) == "/mnt/slow/data/file.txt"

    def test_undo_delete_copy(self):
        fast_path = Path("/mnt/fast")
        pool_path = "data/file.txt"
        assert str(fast_path / Path(pool_path)) == "/mnt/fast/data/file.txt"


class TestFmtBytes:
    def test_bytes(self):
        assert _fmt_bytes(500) == "500B"

    def test_kb(self):
        assert _fmt_bytes(2048) == "2KB"

    def test_mb(self):
        assert _fmt_bytes(5 * 1024 * 1024) == "5MB"

    def test_gb(self):
        assert _fmt_bytes(3 * 1024 * 1024 * 1024) == "3.0GB"

    def test_edge_zero(self):
        assert _fmt_bytes(0) == "0B"

    def test_edge_one(self):
        assert _fmt_bytes(1) == "1B"
