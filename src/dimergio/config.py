from __future__ import annotations

import json
from pathlib import Path


CONFIG_DIR = Path.home() / ".config" / "dimergio"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULTS = dict(
    prefix="_dimergio_",
    state_dir=str(Path.home() / ".local" / "share" / "dimergio"),
    cleanup_days=14,
    default_pool="/mnt/games",
    checkpoint_interval_s=60,
    iowait_interval_ms=10,
)


def load() -> dict:
    if not CONFIG_FILE.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        save(DEFAULTS)
        return dict(DEFAULTS)

    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        # Merge with defaults so new keys get defaults
        merged = dict(DEFAULTS)
        merged.update(cfg)
        return merged
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS)


def save(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    tmp.replace(CONFIG_FILE)
