from __future__ import annotations

import hashlib
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .model import Candidate, MoveEntry, Pool
from .state import StateManager


class VerifyError(OSError):
    """Raised when SHA256 verification of a copied file fails."""


def move_files(
    candidates: list[Candidate],
    pool: Pool,
    prefix: str = "_dimergio_",
    verify: bool = False,
) -> None:
    state = StateManager(pool.name)
    fastest = pool.fastest_branch
    if fastest is None:
        print("Error: no branches found in pool.")
        return

    total = len(candidates)
    succeeded = 0
    failed: list[str] = []

    branch_map = {b.label: b for b in pool.branches}

    print(f"\nMoving {total} files to {fastest.label}...\n")

    for i, candidate in enumerate(candidates, 1):
        src_branch_label = candidate.branch_name
        src_branch = branch_map.get(src_branch_label)
        if src_branch is None:
            print(f"  {i}/{total} \u2717 {candidate.pool_path:<50s}  unknown branch")
            failed.append(candidate.pool_path)
            continue

        # Resolve source path: branch_path + relative_path
        rel = Path(candidate.pool_path)
        src_path = src_branch.path / rel
        dst_path = fastest.path / rel
        renamed_path = src_path.parent / f"{prefix}{src_path.name}"

        try:
            # Ensure target directory exists with correct permissions
            if not dst_path.parent.exists():
                dst_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copymode(src_path.parent, dst_path.parent)
                    shutil.copystat(src_path.parent, dst_path.parent)
                except OSError:
                    pass

            # Copy
            shutil.copy2(src_path, dst_path)

            # Verify (size)
            src_stat = src_path.stat()
            dst_stat = dst_path.stat()
            if dst_stat.st_size != src_stat.st_size:
                raise OSError(f"size mismatch: {dst_stat.st_size} != {src_stat.st_size}")

            # Verify (hash)
            if verify:
                src_hash = _hash_file(src_path)
                dst_hash = _hash_file(dst_path)
                if src_hash != dst_hash:
                    raise VerifyError(
                        f"SHA256 mismatch: {src_path} != {dst_path}"
                    )

            # Rename original
            if renamed_path.exists():
                raise OSError(f"renamed target already exists: {renamed_path}")
            src_path.rename(renamed_path)

            # Record
            entry = MoveEntry(
                pool_path=candidate.pool_path,
                source_branch=src_branch_label,
                target_branch=fastest.label,
                original_basename=src_path.name,
                renamed_basename=renamed_path.name,
                moved_at=datetime.now(tz=timezone.utc).isoformat(),
                file_size=candidate.file_size,
            )
            state.add(entry)

            print(f"  {i}/{total} \u2713 {candidate.pool_path:<50s}  {_fmt_bytes(candidate.file_size):>8} \u2192 {fastest.label}")
            succeeded += 1

        except VerifyError:
            print(f"\n  ERROR: SHA256 mismatch for {candidate.pool_path}")
            print(f"  Source and destination are preserved for investigation.")
            print(f"  Source: {src_path}")
            print(f"  Destination: {dst_path}")
            return

        except OSError as e:
            if dst_path.exists():
                dst_path.unlink(missing_ok=True)
            print(f"  {i}/{total} \u2717 {candidate.pool_path:<50s}  {e}")
            failed.append(candidate.pool_path)

    print()
    if succeeded:
        print(f"Done: {succeeded} moved, {len(failed)} failed.")
        print(f"Original files renamed with prefix '{prefix}'.")
        print()
        print("Restart your program and test. If everything works, run:")
        print(f"  dimergio cleanup --pool {pool.mount}")
    else:
        print("No files were moved.")


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
