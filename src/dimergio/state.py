from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .config import load as load_config
from .model import MoveEntry


class StateManager:
    def __init__(self, pool_name: str):
        cfg = load_config()
        state_dir = Path(cfg["state_dir"]).expanduser()
        state_dir.mkdir(parents=True, exist_ok=True)
        self._path = state_dir / f"state.{pool_name.lower()}.json"
        self._checkpoint_dir = state_dir
        self._entries: list[MoveEntry] = []
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                with open(self._path) as f:
                    data = json.load(f)
                self._entries = [MoveEntry(**e) for e in data.get("moved_files", [])]
            except (json.JSONDecodeError, OSError):
                self._entries = []

    def save(self) -> None:
        data = dict(
            updated=datetime.now(tz=timezone.utc).isoformat(),
            moved_files=[asdict(e) for e in self._entries],
        )
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(self._path)

    def add(self, entry: MoveEntry) -> None:
        self._entries.append(entry)
        self.save()

    def all(self) -> list[MoveEntry]:
        return list(self._entries)

    def unverified(self, older_than_days: int = 14) -> list[MoveEntry]:
        cutoff = datetime.now(tz=timezone.utc)
        return [
            e for e in self._entries
            if e.verified_working is None
            and self._age_days(e.moved_at) >= older_than_days
        ]

    def mark_verified(self, pool_path: str, ok: bool) -> None:
        for e in self._entries:
            if e.pool_path == pool_path:
                e.verified_working = ok
                self.save()
                return

    def remove(self, pool_path: str) -> None:
        self._entries = [e for e in self._entries if e.pool_path != pool_path]
        self.save()

    @staticmethod
    def _age_days(iso_date: str) -> int:
        try:
            dt = datetime.fromisoformat(iso_date)
            return (datetime.now(tz=dt.tzinfo or timezone.utc) - dt).days
        except (ValueError, TypeError):
            return 0

    def save_checkpoint(self, data: dict) -> None:
        path = self._checkpoint_dir / f"checkpoint.{self._path.stem.removeprefix('state.')}.json"
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(path)

    def load_checkpoint(self) -> dict | None:
        path = self._checkpoint_dir / f"checkpoint.{self._path.stem.removeprefix('state.')}.json"
        if path.exists():
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                return None
        return None

    def remove_checkpoint(self) -> None:
        path = self._checkpoint_dir / f"checkpoint.{self._path.stem.removeprefix('state.')}.json"
        path.unlink(missing_ok=True)
