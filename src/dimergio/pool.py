from __future__ import annotations

import os
import re
from pathlib import Path

from .model import Branch, Pool


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
