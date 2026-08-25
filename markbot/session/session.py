"""Session management for conversation history."""

import json
import os
import shutil
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout
from loguru import logger

from markbot.config.paths import get_legacy_sessions_dir
from markbot.session.events import (
    EVENT_HISTORY_RESET,
    EVENT_HISTORY_RETAINED,
    derive_messages,
    event_for_message,
    make_event,
    next_seq,
)
from markbot.utils.helpers import ensure_dir, safe_filename


@dataclass
class Session:
    """
    A conversation session.

    Stores messages in JSONL format for easy reading and persistence.

    Important: Messages are append-only for LLM cache efficiency.
    The tiered memory system (Hot/Warm/Cold) handles long-term storage
    but does NOT modify the messages list or get_history() output.

    Alongside ``messages`` an append-only typed **event log** records the
    same facts as durable events (see :mod:`markbot.session.events`). The
    log is the reconstruction source for resume / fork / replay / audit;
    model-visible history is recoverable via ``derive_messages()`` even if
    derived state is lost.
    """

    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0  # Number of messages already consolidated to files
    _written_count: int = 0  # Number of messages already persisted to disk
    # Force full write on next save (set by clear/retain to avoid stale files)
    _dirty: bool = False
    # Tracks last_consolidated value persisted to disk; mismatch triggers full write
    _written_last_consolidated: int = 0
    # Append-only typed event log (envelopes from markbot.session.events).
    events: list[dict[str, Any]] = field(default_factory=list)
    _written_event_count: int = 0  # Events already persisted to disk

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session (logged as a typed event)."""
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.append_message(msg)

    def append_message(self, message: dict[str, Any]) -> None:
        """Append a provider-format message and mirror it into the event log."""
        self.messages.append(message)
        self._log_message_event(message)
        self.updated_at = datetime.now()

    def _log_message_event(self, message: dict[str, Any]) -> None:
        try:
            event_type, payload = event_for_message(message)
        except Exception:  # noqa: BLE001 — logging must never break appending
            return
        self.events.append(make_event(next_seq(self.events), event_type, payload))

    def append_event(
        self,
        event: str,
        payload: dict[str, Any] | None = None,
        *,
        ignorable: bool = False,
    ) -> None:
        """Append a non-message durable event (audit / lifecycle / control)."""
        self.events.append(
            make_event(next_seq(self.events), event, payload, ignorable=ignorable)
        )
        self.updated_at = datetime.now()

    def derive_messages(self, upto_seq: int | None = None) -> list[dict[str, Any]]:
        """Project model-visible history from the event log (see module docs)."""
        return derive_messages(self.events, upto_seq=upto_seq)

    def rebuild_messages_from_events(self) -> int:
        """Rebuild ``messages`` from the event-log projection.

        Recovery path for sessions whose message lines were lost or
        corrupted while event lines survived. The projection honours
        history boundaries (reset/retain), so an intentionally cleared
        session projects empty and nothing is rebuilt.

        Returns the number of messages restored (0 when messages already
        exist or the projection is empty — never clobbers live state).
        """
        if self.messages:
            return 0
        projected = self.derive_messages()
        if not projected:
            return 0
        self.messages.extend(projected)
        # Everything in the log is now reflected in memory; force a full
        # write so the repaired state hits disk with fresh message lines.
        # (_dirty is required: without it a small repair would never be
        # rewritten because the append path only writes new lines.)
        self._written_count = len(self.messages)
        self._written_event_count = len(self.events)
        self._dirty = True
        self.updated_at = datetime.now()
        logger.info(
            "Session {} rebuilt {} message(s) from its event log",
            self.key,
            len(projected),
        )
        return len(projected)

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
        previous_count = len(self.messages)
        self.messages = []
        self.last_consolidated = 0
        self._written_count = 0
        # The event log is append-only: a reset is a durable fact, never an
        # erasure. Projection consumers honour history_reset as a boundary.
        self.append_event(EVENT_HISTORY_RESET, {"previous_message_count": previous_count})
        self._dirty = True
        self.updated_at = datetime.now()

    def retain_recent_legal_suffix(self, max_messages: int) -> None:
        """Keep a legal recent suffix, mirroring get_history boundary rules.

        ``max_messages <= 0`` means no limit (consistent with ``get_history``);
        use ``clear()`` to empty the session.
        """
        if max_messages <= 0:
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
        # The projection boundary records the FINAL kept count, not the
        # dropped count: every entry in ``messages`` is mirrored 1:1 by a
        # model-visible event, so "keep the last N" aligns the projection
        # with live state even when orphan-stripping removed messages from
        # mid-list (a leading-count would then drop the wrong events).
        self.append_event(
            EVENT_HISTORY_RETAINED,
            {"dropped": dropped, "retained_count": len(retained)},
        )
        self.messages = retained
        self.last_consolidated = max(0, self.last_consolidated - dropped)
        self._written_count = 0
        # Force full write: _written_count reset to 0 would otherwise append
        # duplicates to the existing file.
        self._dirty = True
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
        self._save_count = 0
        self._CLEANUP_INTERVAL = 100
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

    def _get_lock_path(self, key: str) -> Path:
        """Get the lock file path for a session."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.lock"

    def _get_lock(self, key: str) -> FileLock:
        """Get a file lock for a session."""
        return FileLock(str(self._get_lock_path(key)), timeout=30)

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
        """Load a session from disk (with file lock for concurrency safety)."""
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

        lock = self._get_lock(key)
        try:
            with lock:
                return self._read_session_file(key, path)
        except Timeout:
            logger.warning("Failed to acquire lock for session {}, loading without lock", key)
            return self._read_session_file(key, path)

    def _read_session_file(self, key: str, path: Path) -> Session | None:
        """Read and parse a session JSONL file."""
        try:
            messages = []
            events = []
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
                    elif data.get("_type") == "event":
                        # Event log lines; sessions written by older versions
                        # simply have none — both shapes load cleanly.
                        if isinstance(data.get("seq"), int):
                            events.append(data)
                    else:
                        messages.append(data)

            session = Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated,
                _written_count=len(messages),
                _written_last_consolidated=last_consolidated,
                events=events,
                _written_event_count=len(events),
            )
            if not messages and events:
                # Message lines lost/corrupted but the event log survived:
                # reconstruct model-visible history from the projection.
                try:
                    session.rebuild_messages_from_events()
                    # Persisted cursors were reset by the rebuild; mark dirty
                    # so the next save rewrites the file with fresh lines.
                    session._dirty = True
                except Exception as exc:  # noqa: BLE001 — recovery is best-effort
                    logger.warning("Event-log recovery failed for {}: {}", key, exc)
            return session
        except Exception as e:
            logger.warning("Failed to load session {}: {}", key, e)
            return None

    def save(self, session: Session) -> None:
        """Save a session to disk using atomic write."""
        self._save_to_disk(session)
        self._cache[session.key] = session
        self._cache.move_to_end(session.key)
        self._evict_if_needed()
        self._save_count += 1
        if self._save_count % self._CLEANUP_INTERVAL == 0:
            self._cleanup_expired()

    def close(self) -> None:
        """Persist all cached sessions to disk (best-effort)."""
        for key in list(self._cache):
            session = self._cache[key]
            try:
                self._save_to_disk(session)
            except Exception as e:
                logger.warning("Failed to save session {} on close: {}", key, e)

    def _evict_if_needed(self) -> None:
        """Evict oldest cached sessions when cache exceeds max size."""
        while len(self._cache) > self._max_cache_size:
            evicted_key, evicted_session = self._cache.popitem(last=False)
            try:
                self._save_to_disk(evicted_session)
            except Exception as e:
                logger.warning("Failed to save evicted session {}: {}", evicted_key, e)

    _APPEND_COMPACT_THRESHOLD = 100

    def _save_to_disk(self, session: Session) -> None:
        """Save a session to disk using append-only writes with periodic compaction.

        Uses file locking to prevent concurrent writes from corrupting the file.
        """
        path = self._get_session_path(session.key)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

        new_messages = session.messages[session._written_count:]
        append_count = len(new_messages)
        new_events = session.events[session._written_event_count:]
        event_append_count = len(new_events)
        metadata_changed = session.last_consolidated != session._written_last_consolidated

        # Skip only when there's nothing new AND metadata is unchanged AND
        # no structural change (clear/retain) flagged the session as dirty.
        if (
            append_count == 0
            and event_append_count == 0
            and not metadata_changed
            and not session._dirty
            and path.exists()
        ):
            return

        lock = self._get_lock(session.key)
        try:
            with lock:
                # Full write when: structural change, metadata changed, large
                # append, or file doesn't exist. Otherwise append new lines.
                if (
                    session._dirty
                    or metadata_changed
                    or append_count > self._APPEND_COMPACT_THRESHOLD
                    or not path.exists()
                ):
                    self._full_write(session, path)
                    session._written_count = len(session.messages)
                    session._written_last_consolidated = session.last_consolidated
                    session._written_event_count = len(session.events)
                    session._dirty = False
                else:
                    self._append_write(session, path, new_messages, new_events)
                    session._written_count += append_count
                    session._written_event_count += event_append_count
        except Timeout:
            logger.error(
                "Failed to acquire lock for session {} within timeout, "
                "data may be lost",
                session.key,
            )
            raise

    def _full_write(self, session: Session, path: Path) -> None:
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
                    "last_consolidated": session.last_consolidated,
                    "event_count": len(session.events),
                }
                f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")
                # Interleave messages and events in original append order by
                # seq where available; message lines carry no seq, so they are
                # written first followed by the event log (projection only
                # reads event lines, ordering between the two streams is not
                # semantically significant).
                for msg in session.messages:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")
                for envelope in session.events:
                    f.write(json.dumps(envelope, ensure_ascii=False) + "\n")

            os.replace(temp_path, path)
        except BaseException:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    logger.opt(exception=True).debug("Failed to clean up temp file during session write: {}", temp_path)
            raise

    def _append_write(
        self,
        session: Session,
        path: Path,
        new_messages: list[dict[str, Any]],
        new_events: list[dict[str, Any]] | None = None,
    ) -> None:
        try:
            with open(path, "a", encoding="utf-8") as f:
                for msg in new_messages:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")
                for envelope in new_events or []:
                    f.write(json.dumps(envelope, ensure_ascii=False) + "\n")
        except Exception:
            self._full_write(session, path)

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
