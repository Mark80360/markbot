"""Session management for conversation history."""

import json
import shutil
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
    The consolidation process writes summaries to MEMORY.md/HISTORY.md
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
        msg = {"role": role, "content": content, "timestamp": datetime.now().isoformat(), **kwargs}
        self.messages.append(msg)
        self.updated_at = datetime.now()

    def get_history(self, max_messages: int = 500) -> list[dict[str, Any]]:
        """Return unconsolidated messages for LLM input, aligned to a user turn."""
        unconsolidated = self.messages[self.last_consolidated :]
        sliced = unconsolidated[-max_messages:]

        # Drop leading non-user messages to avoid orphaned tool_result blocks
        for i, m in enumerate(sliced):
            if m.get("role") == "user":
                sliced = sliced[i:]
                break

        # First pass: sanitize tool_calls and collect valid tool_call_ids
        valid_tool_call_ids: set[str] = set()
        sanitized_entries: list[dict[str, Any]] = []

        for m in sliced:
            entry: dict[str, Any] = {"role": m["role"], "content": m.get("content", "")}
            for k in ("tool_calls", "tool_call_id", "name"):
                if k in m:
                    entry[k] = m[k]

            # Validate and sanitize tool_calls
            if "tool_calls" in entry:
                sanitized_tc = self._sanitize_tool_calls(entry["tool_calls"])
                if sanitized_tc:
                    entry["tool_calls"] = sanitized_tc
                    # Collect valid tool_call_ids from this assistant message
                    for tc in sanitized_tc:
                        tc_id = tc.get("id")
                        if tc_id:
                            valid_tool_call_ids.add(tc_id)
                else:
                    # All tool_calls were invalid, remove the field entirely
                    entry.pop("tool_calls", None)

            sanitized_entries.append(entry)

        # Second pass: remove orphaned tool messages without valid tool_call_ids
        out: list[dict[str, Any]] = []
        for entry in sanitized_entries:
            if entry.get("role") == "tool":
                tc_id = entry.get("tool_call_id")
                if tc_id and tc_id in valid_tool_call_ids:
                    out.append(entry)
                else:
                    logger.warning(
                        "Dropping orphaned tool message with tool_call_id '{}'",
                        tc_id,
                    )
            else:
                # For assistant messages, update valid_tool_call_ids
                if entry.get("role") == "assistant" and entry.get("tool_calls"):
                    valid_tool_call_ids = set()
                    for tc in entry["tool_calls"]:
                        tc_id = tc.get("id")
                        if tc_id:
                            valid_tool_call_ids.add(tc_id)
                out.append(entry)

        return out

    def _sanitize_tool_calls(self, tool_calls: Any) -> list[dict[str, Any]]:
        """Sanitize tool_calls to ensure arguments are valid JSON objects."""
        if not isinstance(tool_calls, list):
            return []

        sanitized = []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            func = tc.get("function", {})
            if not func:
                continue
            args = func.get("arguments")
            if args is None:
                continue
            if isinstance(args, str):
                try:
                    parsed = json.loads(args)
                    if isinstance(parsed, dict):
                        func["arguments"] = parsed
                    else:
                        logger.warning(
                            "History contains tool call '{}' with non-object arguments (type: {}), skipping",
                            func.get("name"),
                            type(parsed).__name__,
                        )
                        continue
                except json.JSONDecodeError:
                    logger.warning(
                        "History contains tool call '{}' with invalid JSON arguments, skipping",
                        func.get("name"),
                    )
                    continue
            elif isinstance(args, list):
                logger.warning(
                    "History contains tool call '{}' with array arguments (should be object), skipping",
                    func.get("name"),
                )
                continue
            elif not isinstance(args, dict):
                logger.warning(
                    "History contains tool call '{}' with invalid arguments type: {}, skipping",
                    func.get("name"),
                    type(args).__name__,
                )
                continue
            sanitized.append(tc)
        return sanitized

    def clear(self) -> None:
        """Clear all messages and reset session to initial state."""
        self.messages = []
        self.last_consolidated = 0
        self.updated_at = datetime.now()


class SessionManager:
    """
    Manages conversation sessions.

    Sessions are stored as JSONL files in the sessions directory.
    """

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(self.workspace / "sessions")
        self.legacy_sessions_dir = get_legacy_sessions_dir()
        self._cache: dict[str, Session] = {}

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
            return self._cache[key]

        session = self._load(key)
        if session is None:
            session = Session(key=key)

        self._cache[key] = session
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

                    data = json.loads(line)

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = (
                            datetime.fromisoformat(data["created_at"])
                            if data.get("created_at")
                            else None
                        )
                        last_consolidated = data.get("last_consolidated", 0)
                    else:
                        messages.append(data)

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated,
            )
        except Exception as e:
            logger.warning("Failed to load session {}: {}", key, e)
            return None

    def save(self, session: Session) -> None:
        """Save a session to disk."""
        path = self._get_session_path(session.key)

        with open(path, "w", encoding="utf-8") as f:
            metadata_line = {
                "_type": "metadata",
                "key": session.key,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": session.metadata,
                "last_consolidated": session.last_consolidated,
            }
            f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")
            for msg in session.messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        self._cache[session.key] = session

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
                            sessions.append(
                                {
                                    "key": key,
                                    "created_at": data.get("created_at"),
                                    "updated_at": data.get("updated_at"),
                                    "path": str(path),
                                }
                            )
            except Exception:
                continue

        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)
