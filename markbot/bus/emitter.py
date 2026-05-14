"""EventEmitter — publish/subscribe system for markbot lifecycle events.

Provides async-first event emission with sync callback support,
wildcard subscriptions, and optional event persistence for auditing.

Usage::

    from markbot.bus.emitter import get_event_emitter, EventType, Event

    emitter = get_event_emitter()

    @emitter.on(EventType.TOOL_CALLED)
    async def on_tool(event: Event):
        print(f"Tool called: {event.payload}")

    await emitter.emit(EventType.TOOL_CALLED, {"tool": "read_file"})
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Union

from loguru import logger

from markbot.bus.events import Event, EventType

SyncCallback = Callable[[Event], None]
AsyncCallback = Callable[[Event], Any]
Callback = Union[SyncCallback, AsyncCallback]


class _Subscription:
    __slots__ = ("callback", "once")

    def __init__(self, callback: Callback, once: bool = False) -> None:
        self.callback = callback
        self.once = once


class EventEmitter:
    """Async-first event emitter with wildcard support.

    Features:
    - Typed event subscriptions via ``EventType`` enum.
    - Wildcard: ``EventType`` = ``None`` receives all events.
    - ``once()`` for one-shot subscriptions.
    - Optional event log persistence (append-only JSONL).
    - Thread-safe for single-event-loop use.
    """

    def __init__(self, *, persist_path: Path | None = None, max_log_size: int = 10_000) -> None:
        self._subs: dict[EventType | None, list[_Subscription]] = defaultdict(list)
        self._persist_path = persist_path
        self._max_log_size = max_log_size
        self._log_count = 0
        self._history: list[Event] = []
        self._persist_file: Any = None

        if persist_path:
            persist_path.parent.mkdir(parents=True, exist_ok=True)
            self._persist_file = open(persist_path, "a", encoding="utf-8")

    def on(self, event_type: EventType, callback: Callback | None = None) -> Callable[[Callback], Callback]:
        """Register a callback for an event type.  Use as decorator or method call."""
        if callback is not None:
            self._subs[event_type].append(_Subscription(callback))
            return callback

        def decorator(fn: Callback) -> Callback:
            self._subs[event_type].append(_Subscription(fn))
            return fn

        return decorator

    def once(self, event_type: EventType, callback: Callback | None = None) -> Callable[[Callback], Callback]:
        """Register a one-shot callback that auto-removes after first fire."""
        if callback is not None:
            self._subs[event_type].append(_Subscription(callback, once=True))
            return callback

        def decorator(fn: Callback) -> Callback:
            self._subs[event_type].append(_Subscription(fn, once=True))
            return fn

        return decorator

    def off(self, event_type: EventType, callback: Callback) -> None:
        """Remove a specific callback from an event type."""
        subs = self._subs.get(event_type, [])
        self._subs[event_type] = [s for s in subs if s.callback is not callback]

    async def emit(self, event_type: EventType, payload: Any = None, *, correlation_id: str = "", session_key: str = "") -> None:
        """Emit an event and invoke all matching subscribers."""
        event = Event(
            type=event_type,
            payload=payload,
            correlation_id=correlation_id,
            session_key=session_key,
        )

        self._history.append(event)
        if len(self._history) > self._max_log_size:
            self._history = self._history[-self._max_log_size:]

        if self._persist_path:
            self._persist_event(event)

        subscribers = list(self._subs.get(event_type, [])) + list(self._subs.get(None, []))

        to_remove: list[_Subscription] = []
        for sub in subscribers:
            try:
                result = sub.callback(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error("Subscriber error for {}: {}", event_type.name, e)

            if sub.once:
                to_remove.append(sub)

        for sub in to_remove:
            try:
                self._subs[event_type].remove(sub)
            except ValueError:
                pass

    def _persist_event(self, event: Event) -> None:
        if self._persist_file is None:
            return
        try:
            line = json.dumps({
                "type": event.type.name,
                "payload": event.payload if isinstance(event.payload, (str, int, float, bool, list, dict, type(None))) else str(event.payload),
                "timestamp": event.timestamp,
                "correlation_id": event.correlation_id,
                "session_key": event.session_key,
            }, ensure_ascii=False, default=str)
            self._persist_file.write(line + "\n")
            self._persist_file.flush()
            self._log_count += 1
        except Exception as e:
            logger.warning("Persist failed: {}", e)

    @property
    def history(self) -> list[Event]:
        return list(self._history)

    def clear_history(self) -> None:
        self._history.clear()

    def close(self) -> None:
        if self._persist_file is not None:
            try:
                self._persist_file.close()
            except Exception:
                pass
            self._persist_file = None


_emitter: EventEmitter | None = None


def get_event_emitter(*, persist_path: Path | None = None) -> EventEmitter:
    """Get the global singleton EventEmitter."""
    global _emitter
    if _emitter is None:
        _emitter = EventEmitter(persist_path=persist_path)
    return _emitter


def reset_event_emitter() -> None:
    """Reset the global emitter (useful for testing)."""
    global _emitter
    if _emitter is not None:
        _emitter.close()
    _emitter = None
