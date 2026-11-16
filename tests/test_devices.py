"""Branch device naming, the rotational probe, and tier classification.

The device name is what lets the sampler find a branch in /proc/diskstats:
a branch on a bare NVMe partition used to resolve to "" and was therefore
never charged any I/O wait.
"""

from pathlib import Path

from dimergio.model import SPEED_WEIGHTS, speed_class_for
from dimergio.pool import _device_name, _rotational


def test_device_name_nvme_partition():
    assert _device_name("/dev/nvme0n1p1") == "nvme0n1p1"


def test_device_name_device_mapper():
    assert _device_name("/dev/dm-11") == "dm-11"


def test_device_name_follows_symlink(tmp_path):
    real = tmp_path / "dm-3"
    real.write_text("")
    link = tmp_path / "mapper" / "vg-lv"
    link.parent.mkdir()
    link.symlink_to(real)
    assert _device_name(str(link), dev_root=tmp_path) == "dm-3"


def test_device_name_rejects_non_block_source():
    assert _device_name("server:/export") is None


def _sysfs(tmp_path: Path, device: str, rotational: int | None) -> Path:
    sys = tmp_path / "sys"
    (sys / "block" / device).mkdir(parents=True)
    if rotational is not None:
        queue = sys / "block" / device / "queue"
        queue.mkdir()
        (queue / "rotational").write_text(f"{rotational}\n")
    return sys


def test_rotational_reads_the_flag(tmp_path):
    assert _rotational("dm-4", _sysfs(tmp_path, "dm-4", 1)) is True
    assert _rotational("dm-11", _sysfs(tmp_path, "dm-11", 0)) is False


def test_rotational_falls_back_to_the_parent_disk(tmp_path):
    sys = _sysfs(tmp_path, "nvme0n1", 0)
    parent = sys / "devices" / "pci0" / "nvme" / "nvme0n1"
    parent.mkdir(parents=True)
    entry = sys / "class" / "block" / "nvme0n1p1"
    entry.parent.mkdir(parents=True)
    entry.symlink_to(parent / "nvme0n1p1")
    parent.joinpath("nvme0n1p1").mkdir()

    assert _rotational("nvme0n1p1", sys) is False


def test_rotational_defaults_to_spinning(tmp_path):
    assert _rotational("unknown0", _sysfs(tmp_path, "unknown0", None)) is True


def test_speed_class_matrix():
    assert speed_class_for("nvme0n1p1", False) == "nvme"
    assert speed_class_for("dm-11", False) == "ssd"
    assert speed_class_for("dm-4", True) == "hdd"
    assert speed_class_for("sda", True) == "hdd"
    assert speed_class_for("md0", False) == "hdd"
    assert speed_class_for("", True) == "hdd"


def test_every_speed_class_has_a_weight():
    for tier in ("nvme", "ssd", "hdd"):
        assert SPEED_WEIGHTS[tier] >= 1
    assert SPEED_WEIGHTS["nvme"] > SPEED_WEIGHTS["ssd"] > SPEED_WEIGHTS["hdd"]
