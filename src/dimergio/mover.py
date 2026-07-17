from __future__ import annotations

import hashlib
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .model import Candidate, MoveEntry, MovePlan, Pool
from .pool import PoolContext
from .state import StateManager


class VerifyError(OSError):
    """Raised when SHA256 verification of a copied file fails."""


def execute_move_plan(
    plans: list[MovePlan],
    pool: Pool,
    prefix: str = "_dimergio_",
    verify: bool = False,
    dry_run: bool = False,
) -> tuple[int, int, list[str], list[dict], int]:
    state = PoolContext(pool).state
    total = len(plans)
    succeeded = 0
    failed: list[str] = []
    operations: list[dict] = []
    total_bytes = 0

    print(f"\nExecuting {total} moves...\n")

    for i, plan in enumerate(plans, 1):
        rel = plan.file.path.relative_to(pool.mount)
        if plan.file.branch_idx >= len(pool.branches) or plan.target_branch_idx >= len(pool.branches):
            print(f"  {i}/{total} \u2717 {plan.file.path.name:<50s}  invalid branch index")
            failed.append(str(rel))
            continue
        src_branch = pool.branches[plan.file.branch_idx]
        dst_branch = pool.branches[plan.target_branch_idx]
        if src_branch is None or dst_branch is None:
            print(f"  {i}/{total} \u2717 {plan.file.path.name:<50s}  unknown branch")
            failed.append(str(rel))
            continue

        src_path = src_branch.path / rel
        dst_path = dst_branch.path / rel

        if dry_run:
            print(f"  {i}/{total} dry-run {plan.file.path.name:<50s}  {src_branch.label} \u2192 {dst_branch.label}")
            succeeded += 1
            continue

        try:
            if plan.is_rename_only:
                entry = _smart_rename(src_path, dst_path, prefix, state, str(rel), src_branch.label, dst_branch.label)
            else:
                entry = _copy_and_rename(src_path, dst_path, prefix, state, str(rel), src_branch.label, dst_branch.label, verify)

            copied = 0 if plan.is_rename_only else entry.file_size
            total_bytes += copied
            operations.append({"pool_path": str(rel), "src": src_branch.label, "dst": dst_branch.label, "bytes": copied, "ok": True})
            print(f"  {i}/{total} \u2713 {plan.file.path.name:<50s}  {src_branch.label} \u2192 {dst_branch.label}  {_fmt_bytes(copied)}")
            succeeded += 1

        except OSError as e:
            if dst_path.exists() and not plan.is_rename_only:
                dst_path.unlink(missing_ok=True)
            print(f"  {i}/{total} \u2717 {plan.file.path.name:<50s}  {e}")
            failed.append(str(rel))
            operations.append({"pool_path": str(rel), "src": src_branch.label, "dst": dst_branch.label, "bytes": 0, "ok": False})

    print()
    if succeeded:
        print(f"Done: {succeeded} moved, {len(failed)} failed.")
        if not dry_run:
            print(f"Original files renamed with prefix '{prefix}'.")
            print()
            print("Restart your program and test. If everything works, run:")
            print(f"  dimergio cleanup --pool {pool.mount}")
    else:
        print("No files were moved.")

    return succeeded, len(failed), failed, operations, total_bytes


def _smart_rename(
    src_path: Path,
    dst_path: Path,
    prefix: str,
    state: StateManager,
    pool_path: str,
    src_label: str,
    dst_label: str,
) -> MoveEntry:
    renamed_name = f"{prefix}{src_path.name}"
    renamed_path = src_path.parent / renamed_name

    if renamed_path.exists():
        renamed_path.rename(dst_path)
    elif src_path.exists():
        src_path.rename(dst_path)
    else:
        raise OSError(f"source not found: {src_path}")

    entry = MoveEntry(
        pool_path=pool_path,
        source_branch=src_label,
        target_branch=dst_label,
        original_basename=src_path.name,
        renamed_basename=dst_path.name,
        moved_at=datetime.now(tz=timezone.utc).isoformat(),
        file_size=dst_path.stat().st_size,
    )
    state.add(entry)
    return entry


def _copy_and_rename(
    src_path: Path,
    dst_path: Path,
    prefix: str,
    state: StateManager,
    pool_path: str,
    src_label: str,
    dst_label: str,
    verify: bool,
) -> MoveEntry:
    if not dst_path.parent.exists():
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copymode(src_path.parent, dst_path.parent)
            shutil.copystat(src_path.parent, dst_path.parent)
        except OSError:
            pass

    shutil.copy2(src_path, dst_path)

    src_stat = src_path.stat()
    dst_stat = dst_path.stat()
    if dst_stat.st_size != src_stat.st_size:
        raise OSError(f"size mismatch: {dst_stat.st_size} != {src_stat.st_size}")

    if verify:
        src_hash = _hash_file(src_path)
        dst_hash = _hash_file(dst_path)
        if src_hash != dst_hash:
            raise VerifyError(f"SHA256 mismatch: {src_path} != {dst_path}")

    renamed_name = f"{prefix}{src_path.name}"
    renamed_path = src_path.parent / renamed_name
    if renamed_path.exists():
        raise OSError(f"renamed target already exists: {renamed_path}")
    src_path.rename(renamed_path)

    entry = MoveEntry(
        pool_path=pool_path,
        source_branch=src_label,
        target_branch=dst_label,
        original_basename=src_path.name,
        renamed_basename=renamed_name,
        moved_at=datetime.now(tz=timezone.utc).isoformat(),
        file_size=dst_stat.st_size,
    )
    state.add(entry)
    return entry


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _fmt_bytes(b: int) -> str:
    if b >= 1 << 30:
        return f"{b / (1<<30):.1f}GB"
    if b >= 1 << 20:
        return f"{b / (1<<20):.0f}MB"
    if b >= 1 << 10:
        return f"{b / (1<<10):.0f}KB"
    return f"{b}B"
