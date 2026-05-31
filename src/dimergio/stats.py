from __future__ import annotations

import logging
from pathlib import Path

import yaml

from .model import FileAccumulator, Pool

logger = logging.getLogger(__name__)

_STATS_FILENAME = ".dimergio.yaml"


def _largest_branch(pool: Pool) -> Path | None:
    if not pool.branches:
        return None
    return max(pool.branches, key=lambda b: b.total_bytes).path


def _stats_path(pool: Pool) -> Path | None:
    root = _largest_branch(pool)
    if root is None:
        return None
    return root / _STATS_FILENAME


def load_stats(pool: Pool) -> dict[str, dict]:
    path = _stats_path(pool)
    if path is None:
        return {}
    try:
        data = yaml.safe_load(path.read_text())
        if data is None:
            return {}
        return data.get("files", {})
    except (OSError, yaml.YAMLError) as e:
        logger.warning("failed to load stats from %s: %s", path, e)
        return {}


def merge_stats(
    existing: dict[str, dict],
    accumulators: dict[Path, FileAccumulator],
    data_path: Path,
) -> dict[str, dict]:
    result = dict(existing)
    for acc in accumulators.values():
        try:
            rel = str(acc.path.relative_to(data_path))
        except ValueError:
            rel = acc.path.name
        if rel in result:
            old = result[rel]
            old["reads"] = old.get("reads", 0) + acc.total_reads
            old["writes"] = old.get("writes", 0) + acc.write_count
            old["iowait_debt"] = old.get("iowait_debt", 0.0) + acc.iowait_debt
        else:
            result[rel] = {
                "reads": acc.total_reads,
                "writes": acc.write_count,
                "iowait_debt": acc.iowait_debt,
            }
    return result


def save_stats(pool: Pool, stats: dict[str, dict]) -> bool:
    path = _stats_path(pool)
    if path is None:
        logger.warning("no branch found to save stats")
        return False
    try:
        data = {"files": stats}
        path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=True))
        logger.info("saved stats to %s (%d files)", path, len(stats))
        return True
    except OSError as e:
        logger.warning("failed to save stats to %s: %s", path, e)
        return False
