"""Event types for the message bus."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any


class EventType(Enum):
    STATE_CHANGED = auto()
    TOOL_CALLED = auto()
    TOOL_COMPLETED = auto()
    TOOL_FAILED = auto()
    PERMISSION_REQUESTED = auto()
    PERMISSION_GRANTED = auto()
    PERMISSION_DENIED = auto()
    MESSAGE_RECEIVED = auto()
    MESSAGE_SENT = auto()
    SESSION_CREATED = auto()
    SESSION_LOADED = auto()
    SESSION_CLEARED = auto()
    MODEL_CALLED = auto()
    MODEL_SUCCEEDED = auto()
    MODEL_FAILED = auto()
    CIRCUIT_OPENED = auto()
    CIRCUIT_CLOSED = auto()
    CIRCUIT_HALF_OPEN = auto()
    BUDGET_WARNING = auto()
    BUDGET_EXCEEDED = auto()
    COMPACTION_STARTED = auto()
    COMPACTION_COMPLETED = auto()
    SKILL_ACTIVATED = auto()
    SKILL_DEACTIVATED = auto()
    SUBAGENT_SPAWNED = auto()
    SUBAGENT_COMPLETED = auto()
    SUBAGENT_FAILED = auto()
    HEALTH_CHECK = auto()


@dataclass
class Event:
    type: EventType
    payload: Any
    timestamp: str = field(
        default_factory=lambda: __import__("datetime").datetime.now().isoformat()
    )
    correlation_id: str = ""
    session_key: str = ""


@dataclass
class InboundMessage:
    """Message received from a chat channel."""

    channel: str
    sender_id: str
    chat_id: str
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    session_key_override: str | None = None
    origin_channel: str | None = None
    origin_chat_id: str | None = None

    @property
    def session_key(self) -> str:
        return self.session_key_override or f"{self.channel}:{self.chat_id}"


@dataclass
class OutboundMessage:
    """Message to send to a chat channel."""

    channel: str
    chat_id: str
    content: str
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
