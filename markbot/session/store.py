"""State store with subscription support.

Inspired by Redux and MarkBot's AppState.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Generic, Optional, TypeVar

from loguru import logger

from markbot.session.types import AppState
from markbot.bus.events import Event, EventType

if TYPE_CHECKING:
    from markbot.session.session import Session

T = TypeVar("T")


@dataclass
class StateSubscription(Generic[T]):
    """Subscription to state changes."""

    selector: Callable[[AppState], T]
    callback: Callable[[T, T], None]  # (new_value, old_value)
    last_value: Any = None


class StateStore:
    """
    Centralized state store with subscription support.

    Inspired by Redux and MarkBot's AppState.
    """

    def __init__(self, initial_state: Optional[AppState] = None):
        self._state = initial_state or AppState()
        self._subscriptions: list[StateSubscription] = []
        self._listeners: list[Callable[[AppState, AppState], None]] = []
        self._history: list[AppState] = []
        self._max_history = 50

    @property
    def state(self) -> AppState:
        """Get current state."""
        return self._state

    def get(self) -> AppState:
        """Get current state (alias)."""
        return self._state

    def set(self, updater: Callable[[AppState], AppState] | AppState) -> None:
        """
        Update state.

        Args:
            updater: New state or function that receives old state and returns new state
        """
        old_state = self._state

        if callable(updater):
            new_state = updater(old_state.copy())
        else:
            new_state = updater

        # Save to history
        self._history.append(old_state)
        if len(self._history) > self._max_history:
            self._history.pop(0)

        self._state = new_state

        # Notify listeners
        self._notify(old_state, new_state)

    def _notify(self, old_state: AppState, new_state: AppState) -> None:
        """Notify all subscribers."""
        # Full state listeners
        for listener in self._listeners:
            try:
                listener(new_state, old_state)
            except Exception as e:
                logger.error(f"State listener error: {e}")

        # Selector-based subscriptions
        for sub in self._subscriptions:
            try:
                new_value = sub.selector(new_state)
                old_value = sub.selector(old_state)

                if new_value != old_value:
                    sub.callback(new_value, old_value)
                    sub.last_value = new_value

            except Exception as e:
                logger.error(f"Subscription error: {e}")

    def subscribe(
        self, callback: Callable[[AppState, AppState], None]
    ) -> Callable[[], None]:
        """
        Subscribe to all state changes.

        Returns unsubscribe function.
        """
        self._listeners.append(callback)

        def unsubscribe():
            self._listeners.remove(callback)

        return unsubscribe

    def select(
        self,
        selector: Callable[[AppState], T],
        callback: Callable[[T, T], None],
    ) -> Callable[[], None]:
        """
        Subscribe to a specific slice of state.

        Only calls callback when selected value changes.

        Returns unsubscribe function.
        """
        sub = StateSubscription(
            selector=selector,
            callback=callback,
            last_value=selector(self._state),
        )
        self._subscriptions.append(sub)

        def unsubscribe():
            self._subscriptions.remove(sub)

        return unsubscribe

    def undo(self) -> bool:
        """Undo last state change."""
        if not self._history:
            return False

        previous = self._history.pop()
        current = self._state
        self._state = previous
        self._notify(current, previous)
        return True


# ── Session persistence abstraction ──────────────────────────────────────

class SessionStore(ABC):
    """Protocol for session persistence backends.

    Implementations handle reading/writing session data to different
    storage backends (JSONL, SQLite, etc.).  The :class:`SessionManager`
    uses this interface so the backend can be swapped without changing
    the caching/lifecycle logic in the manager.
    """

    @abstractmethod
    def load(self, key: str) -> Session | None: ...

    @abstractmethod
    def save(self, session: Session) -> None: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def list_keys(self) -> list[str]: ...


class JSONLSessionStore(SessionStore):
    """JSONL-backed session store.

    Each session is stored in a separate ``.jsonl`` file under *sessions_dir*.
    The first line is a metadata record; subsequent lines are message dicts.
    """

    _FORMAT_VERSION = 1

    def __init__(self, sessions_dir: str | Path) -> None:
        self.sessions_dir = Path(sessions_dir)

    def _path(self, key: str) -> Path:
        safe = key.replace(":", "_").replace("/", "_")
        return self.sessions_dir / f"{safe}.jsonl"

    def load(self, key: str) -> Session | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            messages: list[dict[str, Any]] = []
            metadata: dict[str, Any] = {}
            created_at: datetime | None = None
            last_consolidated = 0

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("Skipping corrupted line in {}: {}", path.name, line[:80])
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
                last_consolidated=last_consolidated,
            )
        except Exception as e:
            logger.warning("Failed to load session {}: {}", key, e)
            return None

    def save(self, session: Session) -> None:
        path = self._path(session.key)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

        temp_path = path.with_suffix(".tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                meta = {
                    "_type": "metadata",
                    "format_version": self._FORMAT_VERSION,
                    "key": session.key,
                    "created_at": session.created_at.isoformat(),
                    "updated_at": session.updated_at.isoformat(),
                    "metadata": session.metadata,
                    "last_consolidated": session.last_consolidated,
                }
                f.write(json.dumps(meta, ensure_ascii=False) + "\n")
                for msg in session.messages:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")

            os.replace(str(temp_path), str(path))
        except BaseException:
            if temp_path.exists():
                temp_path.unlink()
            raise

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def list_keys(self) -> list[str]:
        keys: list[str] = []
        for path in self.sessions_dir.glob("*.jsonl"):
            try:
                with open(path, encoding="utf-8") as f:
                    first = f.readline().strip()
                    if first:
                        data = json.loads(first)
                        key = data.get("key") or path.stem.replace("_", ":", 1)
                        keys.append(key)
            except Exception:
                continue
        return keys
