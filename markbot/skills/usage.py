"""Skill usage tracking and persistence.

Stores view_count, use_count, and last_activity_at per skill
in a JSON file separate from SKILL.md to avoid polluting definitions.

Concurrency: the store is safe across processes (agent CLI, web server,
skill manage tool).  Every mutation re-reads the JSON under an
inter-process lock on a sidecar lock file, so concurrent updates are
serialized and counters are never lost.  Reads use an in-memory cache
that is refreshed on each mutation; changes made by other processes
become visible here at the next mutation.
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

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None


class _InterProcessLock:
    """Blocking cross-process lock via a sidecar lock file.

    Uses ``msvcrt.locking`` on Windows and ``fcntl.flock`` on POSIX.
    Locks are released automatically by the OS if the holding process
    dies, so a stale lock can never block later writers.
    """

    def __init__(self, path: Path):
        self._path = path
        self._fd: int | None = None

    def __enter__(self) -> "_InterProcessLock":
        if msvcrt is None and fcntl is None:
            return self
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._path), os.O_RDWR | os.O_CREAT, 0o600)
        self._fd = fd
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\x00")
            os.lseek(fd, 0, os.SEEK_SET)
            if msvcrt is not None:
                self._lock_msvcrt(fd)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX)
        except Exception:
            os.close(fd)
            self._fd = None
            raise
        return self

    @staticmethod
    def _lock_msvcrt(fd: int) -> None:
        # msvcrt.locking retries internally for ~10s before raising
        # OSError; retry a few times under heavy contention, then give up.
        deadline = time.time() + 30.0
        while True:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                return
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.05)

    def __exit__(self, *exc_info) -> bool:
        if self._fd is not None:
            try:
                os.lseek(self._fd, 0, os.SEEK_SET)
                if msvcrt is not None:
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                elif fcntl is not None:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                os.close(self._fd)
                self._fd = None
        return False


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
    atomic writes (temp + rename) for crash safety, guarded by an
    inter-process lock so concurrent writers never lose updates.
    """

    def __init__(self, workspace: Path):
        self._path = workspace / ".skill_usage.json"
        self._lock = _InterProcessLock(workspace / ".skill_usage.json.lock")
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
        def _apply() -> None:
            entry = self.get(skill_name)
            entry.view_count += 1
            entry.last_activity_at = time.time()
        self._mutate(_apply)

    def bump_use(self, skill_name: str) -> None:
        """Increment use counter and update activity timestamp."""
        def _apply() -> None:
            entry = self.get(skill_name)
            entry.use_count += 1
            entry.last_activity_at = time.time()
        self._mutate(_apply)

    def set_created_at(self, skill_name: str, ts: float | None = None) -> None:
        """Set or update created_at for a skill (used during skill creation)."""
        def _apply() -> None:
            entry = self.get(skill_name)
            entry.created_at = ts if ts is not None else time.time()
        self._mutate(_apply)

    def set_state(self, skill_name: str, state: str) -> None:
        """Update the lifecycle state for a skill."""
        def _apply() -> None:
            entry = self.get(skill_name)
            entry.state = state
        self._mutate(_apply)

    def remove(self, skill_name: str) -> None:
        """Remove a skill's usage entry entirely (used when a skill is deleted)."""
        def _apply() -> None:
            if skill_name in self._data:
                del self._data[skill_name]
        self._mutate(_apply)

    def get_all(self) -> Dict[str, SkillUsageEntry]:
        """Return all usage entries."""
        return dict(self._data)

    # -- Internal ------------------------------------------------------------

    def _mutate(self, fn) -> None:
        """Apply a mutation under the inter-process lock.

        The full JSON is re-read from disk inside the critical section so
        this process observes (and merges with) updates made by other
        processes instead of overwriting them from a stale cache.
        """
        with self._lock:
            self._load()
            fn()
            self._persist()

    def _load(self) -> None:
        """Load usage data from disk (replaces the in-memory cache)."""
        if not self._path.exists():
            self._data = {}
            return
        try:
            import dataclasses
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
            known_fields = {f.name for f in dataclasses.fields(SkillUsageEntry)}
            entries = {}
            for name, entry_dict in data.items():
                filtered = {k: v for k, v in entry_dict.items() if k in known_fields}
                entries[name] = SkillUsageEntry(**filtered)
            self._data = entries
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
