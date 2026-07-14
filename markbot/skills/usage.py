"""Skill usage tracking and persistence.

Stores view_count, use_count, and last_activity_at per skill
in a JSON file separate from SKILL.md to avoid polluting definitions.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Optional

from loguru import logger

from markbot.types.skill import SkillState


@dataclass
class SkillUsageEntry:
    """Usage data for a single skill."""

    view_count: int = 0
    use_count: int = 0
    last_activity_at: float | None = None
    created_at: float = field(default_factory=time.time)
    state: str = SkillState.ACTIVE


class SkillUsageStore:
    """Persistent store for skill usage metrics.

    Data is persisted to ``{workspace}/.skill_usage.json`` using
    atomic writes (temp + rename) for crash safety.
    """

    def __init__(self, workspace: Path):
        self._path = workspace / ".skill_usage.json"
        self._data: Dict[str, SkillUsageEntry] = {}
        self._load()

    # -- Public API ----------------------------------------------------------

    def get(self, skill_name: str) -> SkillUsageEntry:
        """Get usage entry for a skill (creates one if missing)."""
        if skill_name not in self._data:
            self._data[skill_name] = SkillUsageEntry()
        return self._data[skill_name]

    def peek(self, skill_name: str) -> Optional[SkillUsageEntry]:
        """Return the usage entry for a skill without creating one.

        Use this in read-only paths (e.g. lifecycle evaluation, listing)
        to avoid persisting empty entries for skills that were never used.
        """
        return self._data.get(skill_name)

    def bump_view(self, skill_name: str) -> None:
        """Increment view counter and update activity timestamp."""
        entry = self.get(skill_name)
        entry.view_count += 1
        entry.last_activity_at = time.time()
        self._persist()

    def bump_use(self, skill_name: str) -> None:
        """Increment use counter and update activity timestamp."""
        entry = self.get(skill_name)
        entry.use_count += 1
        entry.last_activity_at = time.time()
        self._persist()

    def set_created_at(self, skill_name: str, ts: float | None = None) -> None:
        """Set or update created_at for a skill (used during skill creation)."""
        entry = self.get(skill_name)
        if ts is None:
            ts = time.time()
        entry.created_at = ts
        self._persist()

    def set_state(self, skill_name: str, state: str) -> None:
        """Update the lifecycle state for a skill."""
        entry = self.get(skill_name)
        entry.state = state
        self._persist()

    def remove(self, skill_name: str) -> None:
        """Remove a skill's usage entry entirely (used when a skill is deleted)."""
        if skill_name in self._data:
            del self._data[skill_name]
            self._persist()

    def get_all(self) -> Dict[str, SkillUsageEntry]:
        """Return all usage entries."""
        return dict(self._data)

    # -- Internal ------------------------------------------------------------

    def _load(self) -> None:
        """Load usage data from disk."""
        if not self._path.exists():
            return
        try:
            import dataclasses
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
            known_fields = {f.name for f in dataclasses.fields(SkillUsageEntry)}
            for name, entry_dict in data.items():
                filtered = {k: v for k, v in entry_dict.items() if k in known_fields}
                self._data[name] = SkillUsageEntry(**filtered)
        except Exception as e:
            logger.warning("Failed to load skill usage data: {}", e)

    def _persist(self) -> None:
        """Atomically write usage data to disk."""
        data = {name: asdict(entry) for name, entry in self._data.items()}
        content = json.dumps(data, indent=2, ensure_ascii=False)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                suffix=".tmp",
                prefix=".skill_usage_",
                dir=str(self._path.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                os.replace(tmp_path, str(self._path))
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.warning("Failed to persist skill usage data: {}", e)


__all__ = ["SkillUsageStore", "SkillUsageEntry"]
