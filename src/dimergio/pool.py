from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .model import Branch, Pool
from .state import StateManager


@dataclass
class IO_Domain:
    devices: list[str]


def discover_pools() -> list[Pool]:
    pools_by_mount: dict[str, Pool] = {}

    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        try:
            cmdline = (proc_dir / "cmdline").read_bytes()
        except OSError:
            continue

        # cmdline is NUL-separated
        args = cmdline.split(b"\x00")
        if not args:
            continue
        name = args[0].decode("utf-8", errors="replace")
        if "mergerfs" not in name and name != "mergerfs":
            continue

        # Parse: mergerfs branch1:branch2 mountpoint -o options
        args_str = [a.decode("utf-8", errors="replace") for a in args if a]
        if len(args_str) < 3:
            continue

        branch_spec = args_str[1]
        mount_point = args_str[2]

        # Extract fsname from -o options
        all_opts = " ".join(args_str[3:])
        m = re.search(r"\bfsname=([^,\s]+)", all_opts)
        name = m.group(1) if m else Path(mount_point).name

        branch_paths = [Path(p) for p in branch_spec.split(":")]
        branches: list[Branch] = []
        for bp in branch_paths:
            if not bp.exists():
                continue
            dev = _resolve_device(bp)
            rotational = True
            if dev:
                rot_path = Path(f"/sys/block/{dev}/queue/rotational")
                if rot_path.exists():
                    rotational = rot_path.read_text().strip() == "1"

            try:
                sv = os.statvfs(bp)
                total = sv.f_frsize * sv.f_blocks
                free = sv.f_frsize * sv.f_bavail
            except OSError:
                total = 0
                free = 0

            branches.append(Branch(
                path=bp,
                device=dev or "",
                rotational=rotational,
                total_bytes=total,
                free_bytes=free,
            ))

        if branches:
            mp_key = str(Path(mount_point).resolve())
            pools_by_mount[mp_key] = Pool(
                mount=Path(mount_point),
                name=name.upper(),
                branches=branches,
            )

    return list(pools_by_mount.values())


def find_pool(mount_point: str | Path) -> Pool | None:
    target = str(Path(mount_point).resolve())
    for pool in discover_pools():
        if str(pool.mount) == target:
            return pool
    return None


def find_pool_for_cwd() -> Pool | None:
    """Find mergerfs pool containing current working directory (mount or branch)."""
    cwd = Path.cwd().resolve()
    for pool in discover_pools():
        try:
            cwd.relative_to(pool.mount)
            return pool
        except ValueError:
            pass
        for branch in pool.branches:
            try:
                cwd.relative_to(branch.path.resolve())
                return pool
            except ValueError:
                continue
    return None


def _resolve_device(branch_path: Path) -> str | None:
    """Map a branch path to its dm-X device name."""
    # Get mount source from /proc/mounts
    resolved = branch_path.resolve()
    with open("/proc/mounts") as f:
        for line in f:
            parts = line.split()
            if len(parts) < 2:
                continue
            mp = Path(parts[1]).resolve()
            if mp == resolved:
                dev = parts[0]
                return _dm_name(dev)
    return None


def _dm_name(device_path: str) -> str | None:
    """Convert /dev/dm-7 or /dev/mapper/foo to 'dm-7'."""
    if device_path.startswith("/dev/dm-"):
        return device_path.removeprefix("/dev/")
    if device_path.startswith("/dev/mapper/"):
        try:
            target = os.readlink(device_path)
            name = Path(target).name
            if name.startswith("dm-"):
                return name
        except OSError:
            pass
    return None


def _resolve_btrfs_devices(branch_path: Path) -> list[str]:
    """Return all dm-X device names backing the btrfs pool at branch_path.

    For non-btrfs mounts this returns the single device (or [] if un-resolvable).
    """
    resolved = branch_path.resolve()
    mount_source = None
    with open("/proc/mounts") as f:
        for line in f:
            parts = line.split()
            if len(parts) < 2:
                continue
            mp = Path(parts[1]).resolve()
            if mp == resolved:
                mount_source = parts[0]
                break
    if mount_source is None:
        return []

    dm_name = _dm_name(mount_source)
    if dm_name is None:
        return []

    btrfs_root = Path("/sys/fs/btrfs")
    try:
        for fsid_dir in btrfs_root.iterdir():
            if not fsid_dir.is_dir() or fsid_dir.name == "features":
                continue
            devices_dir = fsid_dir / "devices"
            if not devices_dir.is_dir():
                continue
            if (devices_dir / dm_name).exists():
                return sorted(d.name for d in devices_dir.iterdir() if d.is_dir())
    except OSError:
        pass

    return [dm_name]


def _build_io_domains(branches: list[Branch]) -> tuple[list[IO_Domain], list[int]]:
    """Group branches by their backing device set.

    Returns (domains, br2domain) where br2domain[i] = domain index for branch i.
    Branches on the same btrfs pool share a domain.
    """
    branch_devices: list[list[str]] = [_resolve_btrfs_devices(b.path) for b in branches]
    seen: dict[tuple[str, ...], int] = {}
    domains: list[IO_Domain] = []
    br2domain: list[int] = []

    for devs in branch_devices:
        key = tuple(devs)
        idx = seen.get(key)
        if idx is None:
            idx = len(domains)
            domains.append(IO_Domain(devices=list(devs)))
            seen[key] = idx
        br2domain.append(idx)

    return domains, br2domain


class PoolContext:
    """A resolved mergerfs pool plus its derived resources (state, branch lookups).

    This is the single source of pool identity: every command and helper goes
    through it so the pool name used for state files can never diverge between
    code paths. Accepts either an already-resolved ``Pool`` or a mount path/string
    (which it resolves via :func:`find_pool`)."""

    def __init__(self, pool_or_mount: "Pool | str | Path"):
        if isinstance(pool_or_mount, Pool):
            self.pool = pool_or_mount
        else:
            resolved = find_pool(pool_or_mount)
            if resolved is None:
                print(f"Error: no mergerfs pool found at '{pool_or_mount}'")
                sys.exit(1)
            self.pool = resolved

        self._state: "StateManager | None" = None

    @property
    def name(self) -> str:
        return self.pool.name

    @property
    def mount(self) -> Path:
        return self.pool.mount

    @property
    def branches(self) -> list["Branch"]:
        return self.pool.branches

    @property
    def state(self) -> "StateManager":
        if self._state is None:
            self._state = StateManager(self.pool.name)
        return self._state

    def branch_path(self, label: str) -> Path | None:
        for b in self.pool.branches:
            if b.label == label:
                return b.path
        return None
