"""Memory tool — persistent curated memory with add/replace/remove actions.

Provides bounded, file-backed memory that persists across sessions. Two stores:

- MEMORY.md: agent's persistent memory (notes, conversation summaries, learned facts)
- PROFILE.md (or USER.md): what the agent knows about the user

Uses frozen snapshot pattern:
- System prompt gets a frozen snapshot at session start (stable prefix cache)
- Mid-session writes update files on disk immediately but do NOT change
  the system prompt
- Tool responses always reflect the live state

Security: all content is scanned by MemorySecurityScanner before writing.
Context fencing: memory context is wrapped in <memory-context> tags.
"""

from __future__ import annotations

import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from markbot.utils.constants import (
    DEFAULT_MEMORY_CHAR_LIMIT,
    DEFAULT_USER_CHAR_LIMIT,
    MEMORY_FILENAME,
    USER_FILENAME,
)

from .fencing import fence_context
from .scanner import MemorySecurityScanner

fcntl = None
msvcrt = None
try:
    import fcntl as _fcntl  # noqa: E402
    fcntl = _fcntl
except ImportError:
    try:
        import msvcrt as _msvcrt  # noqa: E402
        msvcrt = _msvcrt
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENTRY_DELIMITER = "\n\n---\n\n"


# ---------------------------------------------------------------------------
# Memory Store
# ---------------------------------------------------------------------------


class MemoryStore:
    """Bounded curated memory with file persistence.

    Maintains two parallel states:
      - _system_prompt_snapshot: frozen at load time, used for system prompt
        injection. Never mutated mid-session. Keeps prefix cache stable.
      - memory_entries / user_entries: live state, mutated by tool calls,
        persisted to disk. Tool responses always reflect this live state.
    """

    def __init__(
        self,
        working_dir: str | Path,
        memory_char_limit: int = DEFAULT_MEMORY_CHAR_LIMIT,
        user_char_limit: int = DEFAULT_USER_CHAR_LIMIT,
        on_write: Optional[Callable[[str, str, str], None]] = None,
    ):
        self._working_dir = Path(working_dir)
        self._memory_path = self._working_dir / MEMORY_FILENAME
        self._user_path = self._working_dir / USER_FILENAME
        # Also check USER.md as fallback
        self._user_fallback_path = self._working_dir / "USER.md"

        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit

        self.memory_entries: List[str] = []
        self.user_entries: List[str] = []

        # Frozen snapshot for system prompt injection
        self._system_prompt_snapshot: str = ""

        self._scanner = MemorySecurityScanner()

        self._lock_path = self._working_dir / ".memory.lock"

        self._on_write = on_write

        self._load_all()

    # -- Public API ----------------------------------------------------------

    @property
    def system_prompt_snapshot(self) -> str:
        """Frozen snapshot for system prompt injection.

        Returns empty string if no entries exist, or a formatted block
        wrapped in memory-context fence tags.
        """
        return self._system_prompt_snapshot

    def _notify_write(self, action: str, target: str, content: str) -> None:
        if self._on_write:
            try:
                self._on_write(action, target, content)
            except Exception:
                pass

    def refresh_snapshot(self) -> None:
        """Refresh the frozen snapshot from current entries.

        Called at session start and after bulk operations. Does NOT
        need to be called after every tool write (that would defeat
        the frozen snapshot purpose), but can be called explicitly
        when needed.
        """
        self._build_snapshot()

    def add(self, target: str, content: str) -> Dict[str, Any]:
        """Add a new entry to the specified store.

        Args:
            target: 'memory' or 'user'.
            content: The entry content.

        Returns:
            Dict with keys: success, message, entries.
        """
        # Security scan
        error = self._scanner.scan(content)
        if error:
            return {"success": False, "message": error, "entries": []}

        content = self._scanner.sanitize(content)

        if target == "memory":
            entries = self.memory_entries
            char_limit = self.memory_char_limit
        else:
            entries = self.user_entries
            char_limit = self.user_char_limit

        # Check total char limit.
        # N entries are joined by N-1 delimiters (no leading/trailing delimiter).
        # The on-disk layout also adds a header and trailing newline, but those
        # are constant overhead and not counted against the entry budget here.
        current_total = sum(len(e) for e in entries) + len(ENTRY_DELIMITER) * max(len(entries) - 1, 0)
        if current_total + len(content) + len(ENTRY_DELIMITER) > char_limit:
            return {
                "success": False,
                "message": f"Character limit reached ({char_limit}). "
                           f"Remove or replace existing entries first.",
                "entries": list(entries),
            }

        entries.append(content)
        self._persist(target)
        self._notify_write("add", target, content)
        logger.info("Added entry to {}: {}...", target, content[:60])
        return {"success": True, "message": "Entry added.", "entries": list(entries)}

    def replace(self, target: str, old_text: str, new_content: str) -> Dict[str, Any]:
        """Replace an existing entry identified by substring match.

        Args:
            target: 'memory' or 'user'.
            old_text: Substring to identify the entry to replace.
            new_content: The new entry content.

        Returns:
            Dict with keys: success, message, entries.
        """
        # Security scan
        error = self._scanner.scan(new_content)
        if error:
            return {"success": False, "message": error, "entries": []}

        new_content = self._scanner.sanitize(new_content)

        if target == "memory":
            entries = self.memory_entries
        else:
            entries = self.user_entries

        for i, entry in enumerate(entries):
            if old_text in entry:
                entries[i] = new_content
                self._persist(target)
                self._notify_write("replace", target, new_content)
                logger.info("Replaced entry in {}: {}...", target, old_text[:40])
                return {"success": True, "message": "Entry replaced.", "entries": list(entries)}

        return {"success": False, "message": f"No entry containing '{old_text[:40]}' found.", "entries": list(entries)}

    def remove(self, target: str, old_text: str) -> Dict[str, Any]:
        """Remove an entry identified by substring match.

        Args:
            target: 'memory' or 'user'.
            old_text: Substring to identify the entry to remove.

        Returns:
            Dict with keys: success, message, entries.
        """
        if target == "memory":
            entries = self.memory_entries
        else:
            entries = self.user_entries

        for i, entry in enumerate(entries):
            if old_text in entry:
                removed = entries.pop(i)
                self._persist(target)
                self._notify_write("remove", target, removed)
                logger.info("Removed entry from {}: {}...", target, removed[:40])
                return {"success": True, "message": "Entry removed.", "entries": list(entries)}

        return {"success": False, "message": f"No entry containing '{old_text[:40]}' found.", "entries": list(entries)}

    def read(self, target: str) -> Dict[str, Any]:
        """Read all entries from the specified store.

        Args:
            target: 'memory' or 'user'.

        Returns:
            Dict with keys: success, message, entries.
        """
        if target == "memory":
            entries = self.memory_entries
        else:
            entries = self.user_entries

        return {"success": True, "message": f"{len(entries)} entries.", "entries": list(entries)}

    def get_memory_context(self, query: str | None = None) -> str:
        """Get formatted memory context for system prompt injection.

        Returns the frozen snapshot, or builds one if empty. The result
        is wrapped in <memory-context> fence tags.

        Args:
            query: Optional search query (not yet used, reserved for future).

        Returns:
            Fenced memory context string, or empty string.
        """
        if not self._system_prompt_snapshot:
            self._build_snapshot()
        if not self._system_prompt_snapshot:
            return ""
        return fence_context(self._system_prompt_snapshot, system_note=True)

    # -- Internal ------------------------------------------------------------

    def _load_all(self) -> None:
        """Load entries from disk into memory."""
        self.memory_entries = self._load_entries(self._memory_path)
        # Try PROFILE.md first, then USER.md as fallback
        if self._user_path.exists():
            self.user_entries = self._load_entries(self._user_path)
        elif self._user_fallback_path.exists():
            self.user_entries = self._load_entries(self._user_fallback_path)
            self._user_path = self._user_fallback_path
        self._build_snapshot()

    def _load_entries(self, path: Path) -> List[str]:
        """Load entries from a file, splitting by delimiter."""
        if not path.exists():
            return []
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.warning("Failed to read {}: {}", path, e)
            return []

        # Split by delimiter and strip each entry
        raw_entries = re.split(r"\n---\n", text)
        entries: List[str] = []
        for raw in raw_entries:
            stripped = raw.strip()
            # Skip headers and empty sections
            if not stripped or stripped.startswith("#"):
                continue
            entries.append(stripped)
        return entries

    def _build_snapshot(self) -> None:
        """Build the frozen system prompt snapshot from current entries."""
        parts: List[str] = []

        if self.memory_entries:
            memory_block = "\n".join(f"- {e}" for e in self.memory_entries)
            parts.append(f"## Agent Memory\n\n{memory_block}")

        if self.user_entries:
            user_block = "\n".join(f"- {e}" for e in self.user_entries)
            parts.append(f"## User Profile\n\n{user_block}")

        self._system_prompt_snapshot = "\n\n".join(parts)

    @contextmanager
    def _file_lock(self):
        """Context manager for cross-platform file locking.

        Uses a single shared lock file (self._lock_path) to serialize
        all memory write operations, regardless of target file.
        """
        lock_fd = None
        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_fd = open(self._lock_path, "w")
            if fcntl:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            elif msvcrt:
                msvcrt.locking(lock_fd.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            if lock_fd:
                try:
                    if fcntl:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    elif msvcrt:
                        try:
                            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
                        except OSError:
                            pass
                    lock_fd.close()
                except Exception:
                    pass

    def _atomic_write_text(self, path: Path, content: str) -> None:
        """Write text to a file atomically using temp + rename.

        Prevents readers from seeing partially-written files.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            suffix=".tmp",
            prefix=path.stem + "_",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, str(path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _persist(self, target: str) -> None:
        """Write entries to disk with file lock and atomic write."""
        if target == "memory":
            path = self._memory_path
            entries = self.memory_entries
            header = "# Agent Memory\n\n"
        else:
            path = self._user_path
            entries = self.user_entries
            header = "# User Profile\n\n"

        if not entries:
            try:
                with self._file_lock():
                    self._atomic_write_text(path, header)
            except Exception as e:
                logger.warning("Failed to write {}: {}", path, e)
            return

        content = header + ENTRY_DELIMITER.join(entries) + "\n"
        try:
            with self._file_lock():
                self._atomic_write_text(path, content)
        except Exception as e:
            logger.warning("Failed to write {}: {}", path, e)


__all__ = [
    "MemoryStore",
    "MEMORY_FILENAME",
    "USER_FILENAME",
]
