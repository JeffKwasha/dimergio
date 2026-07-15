from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

from .model import FileAccumulator, Pool

logger = logging.getLogger(__name__)

_STATS_FILENAME = ".dimergio.yaml"


def _stats_path(pool: Pool) -> Path:
    state_root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_root / "dimergio" / f"{pool.name}.yaml"


def load_stats(pool: Pool) -> dict[str, dict]:
    path = _stats_path(pool)
    try:
        data = yaml.safe_load(path.read_text())
        if data is None:
            return {}
        return data.get("files", {})
    except (OSError, yaml.YAMLError) as e:
        logger.info("no stats file at %s: %s", path, e)
        return {}


def load_accumulators(pool: Pool, data_path: Path) -> dict[Path, FileAccumulator]:
    raw = load_stats(pool)
    result: dict[Path, FileAccumulator] = {}
    for rel_str, entry in raw.items():
        full_path = data_path / rel_str
        acc = FileAccumulator(
            path=full_path,
            branch_idx=0,
            total_reads=entry.get("reads", 0),
            write_count=entry.get("writes", 0),
            iowait_debt=entry.get("iowait_debt", 0.0),
        )
        result[full_path] = acc
    return result


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
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"files": stats}
        path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=True))
        logger.info("saved stats to %s (%d files)", path, len(stats))
        return True
    except OSError as e:
        logger.warning("failed to save stats to %s: %s", path, e)
        return False
