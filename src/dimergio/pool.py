from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from .model import SPEED_WEIGHTS, Branch, Pool, speed_class_for
from .state import StateManager

# (source, mountpoint, subvol option) — one entry per mounted filesystem.
Mount = tuple[str, Path, str]


def read_mounts() -> list[Mount]:
    """Parse /proc/mounts once into the entries every pool helper needs."""
    mounts: list[Mount] = []
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 4:
                    continue
                subvol = ""
                for opt in parts[3].split(","):
                    if opt.startswith("subvol="):
                        subvol = opt[len("subvol="):]
                        break
                mounts.append((parts[0], Path(parts[1]), subvol))
    except OSError:
        pass
    return mounts


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _rel(path: Path, root: Path) -> Path | None:
    try:
        return path.relative_to(root)
    except ValueError:
        return None


def _mount_source(path: Path, mounts: list[Mount]) -> str | None:
    resolved = path.resolve()
    for source, mountpoint, _ in mounts:
        if mountpoint.resolve() == resolved:
            return source
    return None


def _resolve_device(branch_path: Path, mounts: list[Mount]) -> str | None:
    """Map a branch path to the kernel device name backing it."""
    source = _mount_source(branch_path, mounts)
    return _device_name(source) if source else None


def _device_name(device_path: str, dev_root: Path = Path("/dev")) -> str | None:
    """Kernel device name for a mount source.

    /dev/nvme0n1p1 -> nvme0n1p1, /dev/dm-7 -> dm-7, and a device-mapper or
    by-uuid symlink resolves to whatever it points at.
    """
    if not _is_under(Path(device_path), dev_root):
        return None
    try:
        resolved = Path(device_path).resolve()
    except OSError:
        resolved = Path(device_path)
    return resolved.name or None


def _rotational(device: str, sys: Path = Path("/sys")) -> bool:
    """Whether the device spins. Partitions keep their queue on the parent disk."""
    queue = sys / "block" / device / "queue" / "rotational"
    if not queue.exists():
        try:
            parent = (sys / "class" / "block" / device).resolve().parent.name
        except OSError:
            parent = ""
        if parent and parent != "block":
            queue = sys / "block" / parent / "queue" / "rotational"
    try:
        return queue.read_text().strip() == "1"
    except OSError:
        return True


def branch_devices(branch_path: Path, mounts: list[Mount]) -> list[str]:
    """Every device backing the filesystem mounted at branch_path.

    A branch on a multi-device btrfs pool reports all its member devices; any
    other mount reports the single device, or [] when it cannot be resolved.
    """
    source = _mount_source(branch_path, mounts)
    device = _device_name(source) if source else None
    if device is None:
        return []

    try:
        fsid_dirs = list(Path("/sys/fs/btrfs").iterdir())
    except OSError:
        return [device]
    for fsid_dir in fsid_dirs:
        devices_dir = fsid_dir / "devices"
        if not devices_dir.is_dir():
            continue
        if (devices_dir / device).exists():
            return sorted(d.name for d in devices_dir.iterdir() if d.is_dir())
    return [device]


def io_domains(branches: list[Branch], mounts: list[Mount]) -> tuple[list[list[str]], list[int]]:
    """Group branches by the set of block devices that back them.

    Branches sharing a btrfs pool contend for the same device queue, so they
    form one IO domain. Returns (domain device lists, branch-to-domain index).
    """
    by_devices: dict[tuple[str, ...], int] = {}
    domains: list[list[str]] = []
    branch_to_domain: list[int] = []

    for branch in branches:
        devices = branch_devices(branch.path, mounts)
        key = tuple(devices)
        idx = by_devices.get(key)
        if idx is None:
            idx = len(domains)
            domains.append(devices)
            by_devices[key] = idx
        branch_to_domain.append(idx)

    return domains, branch_to_domain


def volume_mounts(pool: Pool, mounts: list[Mount]) -> list[tuple[Path, Path, int]]:
    """Correlate each branch's subvolume with its parent btrfs volume mount.

    fatrace reports paths through the volume mount (e.g.
    /mnt/dev/HGST_r1/@/games/…), while the pool/branch uses a subvolume mount
    (e.g. /mnt/@/r1_games/…). Returns [(vol_root, subvol_rel, branch_idx)].
    """
    result: list[tuple[Path, Path, int]] = []
    for idx, branch in enumerate(pool.branches):
        branch_dev = None
        branch_subvol = ""
        for source, mountpoint, subvol in mounts:
            if mountpoint == branch.path:
                branch_dev, branch_subvol = source, subvol
                break
        if not branch_dev or not branch_subvol:
            continue
        vol_root = None
        for source, mountpoint, subvol in mounts:
            if source == branch_dev and subvol in ("", "/"):
                vol_root = mountpoint
                break
        if vol_root is not None:
            result.append((vol_root, Path(branch_subvol), idx))
    return result


def remap_volume_path(path: Path, mounts: list[tuple[Path, Path, int]], pool_mount: Path) -> Path | None:
    """Convert a raw btrfs volume path to its pool-space equivalent."""
    for vol_root, subvol_rel, _ in mounts:
        rel = _rel(subvol_rel, Path("/"))
        if rel is None:
            continue
        prefix = vol_root / rel
        pool_rel_path = _rel(path, prefix)
        if pool_rel_path is not None:
            return pool_mount / pool_rel_path
    return None


def canonical_pool_path(pool: Pool, path: Path, cache: dict[Path, Path | None] | None = None) -> Path | None:
    """Resolve path to the real file's pool-space location.

    Symlinks are resolved. A real path under the pool mount is returned as-is;
    a real path on a branch mount is mapped back to its pool equivalent
    (pool.mount / branch_rel). None means the path escapes the pool.
    """
    if cache is not None and path in cache:
        return cache[path]

    try:
        real = path.resolve()
    except OSError:
        real = path

    result: Path | None = None
    if _is_under(real, pool.mount):
        result = real
    else:
        for branch in pool.branches:
            rel = _rel(real, branch.path)
            if rel is not None:
                result = pool.mount / rel
                break

    if cache is not None:
        cache[path] = result
    return result


def resolve_tracked(
    pool: Pool,
    path: Path,
    data_path: Path,
    mounts: list[tuple[Path, Path, int]] = (),
    extra_targets: object = (),
) -> Path | None:
    """Map an observed path to its tracked pool-space canonical path.

    Paths under the watched data path are tracked whatever they resolve to.
    Paths reported elsewhere are first remapped from a raw btrfs volume path,
    then accepted only when the real file lives under the data path or is one
    of ``extra_targets`` (symlink targets the user can see inside it).
    """
    if _is_under(path, data_path):
        return canonical_pool_path(pool, path)

    source = remap_volume_path(path, mounts, pool.mount) if mounts else None
    if source is None:
        if not _is_under(path, pool.mount):
            return None
        source = path

    canonical = canonical_pool_path(pool, source)
    if canonical is None:
        return None
    if _is_under(canonical, data_path) or canonical in extra_targets:
        return canonical
    return None


def branch_holding(pool: Pool, path: Path, cache: dict[Path, int | None] | None = None) -> int | None:
    """Index of the branch that holds ``path``, or None when none does.

    None is not a guess: paths recorded through a symlink or mergerfs artifact
    have no real location on any branch, and callers drop them instead of
    attributing I/O to the wrong device.
    """
    if cache is not None and path in cache:
        return cache[path]

    idx: int | None = None
    rel = _rel(path, pool.mount)
    if rel is not None:
        for i, branch in enumerate(pool.branches):
            if (branch.path / rel).exists():
                idx = i
                break

    if cache is not None:
        cache[path] = idx
    return idx


def pool_rel(path: Path, mount: Path) -> str:
    """Pool-relative string for ``path``, falling back to its name."""
    rel = _rel(path, mount)
    return str(rel) if rel is not None else path.name


def discover_pools() -> list[Pool]:
    pools_by_mount: dict[str, Pool] = {}
    mounts = read_mounts()

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
            dev = _resolve_device(bp, mounts)
            rotational = _rotational(dev) if dev else True
            speed_class = speed_class_for(dev or "", rotational)

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
                speed_class=speed_class,
                speed_weight=SPEED_WEIGHTS[speed_class],
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
