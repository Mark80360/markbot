"""Session management for conversation history."""

import json
import os
import shutil
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from markbot.config.paths import get_legacy_sessions_dir
from markbot.utils.helpers import ensure_dir, safe_filename


@dataclass
class Session:
    """
    A conversation session.

    Stores messages in JSONL format for easy reading and persistence.

    Important: Messages are append-only for LLM cache efficiency.
    The tiered memory system (Hot/Warm/Cold) handles long-term storage
    but does NOT modify the messages list or get_history() output.
    """

    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0  # Number of messages already consolidated to files

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()

    @staticmethod
    def _strip_orphan_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove orphan tool results whose matching assistant tool_call is missing.

        Unlike the old ``_find_legal_start`` which dropped all messages before
        the last orphan, this only removes the orphan tool results themselves,
        preserving valid history.
        """
        declared: set[str] = set()
        cleaned: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        declared.add(str(tc["id"]))
            elif role == "tool":
                tid = msg.get("tool_call_id")
                if tid and str(tid) not in declared:
                    continue
            cleaned.append(msg)
        return cleaned

    def get_history(self, max_messages: int = 500) -> list[dict[str, Any]]:
        """Return unconsolidated messages for LLM input.

        Args:
            max_messages: Max messages to return.  ``<= 0`` means no limit.
        """
        unconsolidated = self.messages[self.last_consolidated:]
        sliced = unconsolidated[-max_messages:] if max_messages > 0 else unconsolidated[:]

        # Drop leading non-user messages to avoid starting mid-turn when possible.
        for i, message in enumerate(sliced):
            if message.get("role") == "user":
                sliced = sliced[i:]
                break

        # Remove orphan tool results (missing assistant tool_calls) that some
        # providers reject. We remove them individually rather than dropping
        # all messages before the last orphan.
        sliced = self._strip_orphan_tool_results(sliced)

        out: list[dict[str, Any]] = []
        for message in sliced:
            entry: dict[str, Any] = {"role": message["role"], "content": message.get("content", "")}
            for key in ("tool_calls", "tool_call_id", "name"):
                if key in message:
                    entry[key] = message[key]
            out.append(entry)
        return out

    def clear(self) -> None:
        """Clear all messages and reset session to initial state."""
        self.messages = []
        self.last_consolidated = 0
        self.updated_at = datetime.now()

    def retain_recent_legal_suffix(self, max_messages: int) -> None:
        """Keep a legal recent suffix, mirroring get_history boundary rules."""
        if max_messages <= 0:
            self.clear()
            return
        if len(self.messages) <= max_messages:
            return

        start_idx = max(0, len(self.messages) - max_messages)

        # If the cutoff lands mid-turn, extend backward to the nearest user turn.
        while start_idx > 0 and self.messages[start_idx].get("role") != "user":
            start_idx -= 1

        retained = self.messages[start_idx:]

        # Strip orphan tool results (matching get_history() behavior).
        retained = self._strip_orphan_tool_results(retained)

        # Mirror get_history(): drop leading non-user messages so the retained
        # suffix aligns with what get_history() will actually return.
        for i, msg in enumerate(retained):
            if msg.get("role") == "user":
                retained = retained[i:]
                break

        dropped = len(self.messages) - len(retained)
        self.messages = retained
        self.last_consolidated = max(0, self.last_consolidated - dropped)
        self.updated_at = datetime.now()


class SessionManager:
    """
    Manages conversation sessions.

    Sessions are stored as JSONL files in the sessions directory.
    """

    _FORMAT_VERSION = 1
    """Schema version for JSONL session files. Incremented on breaking changes."""
    _DEFAULT_TTL_DAYS = 30
    """Default session TTL. Sessions older than this are cleaned up on init."""

    def __init__(self, workspace: Path, max_cache_size: int = 50, ttl_days: int | None = None):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(self.workspace / "sessions")
        self.legacy_sessions_dir = get_legacy_sessions_dir()
        self._cache: OrderedDict[str, Session] = OrderedDict()
        self._max_cache_size = max_cache_size
        self._ttl_days = ttl_days if ttl_days is not None else self._DEFAULT_TTL_DAYS
        self._cleanup_expired()

    def _cleanup_expired(self) -> None:
        """Remove session files older than TTL."""
        if self._ttl_days <= 0:
            return
        cutoff = datetime.now().timestamp() - self._ttl_days * 86400
        removed = 0
        for path in self.sessions_dir.glob("*.jsonl"):
            try:
                mtime = path.stat().st_mtime
                if mtime < cutoff:
                    path.unlink()
                    removed += 1
            except Exception:
                continue
        if removed:
            logger.info("Cleaned up {} expired session(s) (TTL={}d)", removed, self._ttl_days)

    def _get_session_path(self, key: str) -> Path:
        """Get the file path for a session."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.jsonl"

    def _get_legacy_session_path(self, key: str) -> Path:
        """Legacy global session path (~/.markbot/sessions/)."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.legacy_sessions_dir / f"{safe_key}.jsonl"

    def get_or_create(self, key: str) -> Session:
        """
        Get an existing session or create a new one.

        Args:
            key: Session key (usually channel:chat_id).

        Returns:
            The session.
        """
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        session = self._load(key)
        if session is None:
            session = Session(key=key)

        self._cache[key] = session
        self._evict_if_needed()
        return session

    def _load(self, key: str) -> Session | None:
        """Load a session from disk."""
        path = self._get_session_path(key)
        if not path.exists():
            legacy_path = self._get_legacy_session_path(key)
            if legacy_path.exists():
                try:
                    shutil.move(str(legacy_path), str(path))
                    logger.info("Migrated session {} from legacy path", key)
                except Exception:
                    logger.exception("Failed to migrate session {}", key)

        if not path.exists():
            return None

        try:
            messages = []
            metadata = {}
            created_at = None
            last_consolidated = 0

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("Skipping corrupted line in session {}: {}", key, line[:80])
                        continue

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
                        last_consolidated = data.get("last_consolidated", 0)
                    else:
                        messages.append(data)

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated
            )
        except Exception as e:
            logger.warning("Failed to load session {}: {}", key, e)
            return None

    def save(self, session: Session) -> None:
        """Save a session to disk using atomic write."""
        self._save_to_disk(session)
        self._cache[session.key] = session
        self._cache.move_to_end(session.key)

    def _evict_if_needed(self) -> None:
        """Evict oldest cached sessions when cache exceeds max size."""
        while len(self._cache) > self._max_cache_size:
            evicted_key, evicted_session = self._cache.popitem(last=False)
            try:
                self._save_to_disk(evicted_session)
            except Exception as e:
                logger.warning(f"Failed to save evicted session {evicted_key}: {e}")

    def _save_to_disk(self, session: Session) -> None:
        """Save a session to disk (extracted from save() for reuse)."""
        path = self._get_session_path(session.key)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

        temp_path = path.with_suffix(".tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                metadata_line = {
                    "_type": "metadata",
                    "format_version": self._FORMAT_VERSION,
                    "key": session.key,
                    "created_at": session.created_at.isoformat(),
                    "updated_at": session.updated_at.isoformat(),
                    "metadata": session.metadata,
                    "last_consolidated": session.last_consolidated
                }
                f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")
                for msg in session.messages:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")

            os.replace(temp_path, path)
        except BaseException:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass
            raise

    def invalidate(self, key: str) -> None:
        """Remove a session from the in-memory cache."""
        self._cache.pop(key, None)

    def list_sessions(self) -> list[dict[str, Any]]:
        """
        List all sessions.

        Returns:
            List of session info dicts.
        """
        sessions = []

        for path in self.sessions_dir.glob("*.jsonl"):
            try:
                # Read just the metadata line
                with open(path, encoding="utf-8") as f:
                    first_line = f.readline().strip()
                    if first_line:
                        data = json.loads(first_line)
                        if data.get("_type") == "metadata":
                            key = data.get("key") or path.stem.replace("_", ":", 1)
                            sessions.append({
                                "key": key,
                                "created_at": data.get("created_at"),
                                "updated_at": data.get("updated_at"),
                                "path": str(path)
                            })
            except Exception:
                continue

        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)
