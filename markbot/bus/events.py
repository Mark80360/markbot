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
    PERMISSION_REQUESTED = auto()
    PERMISSION_GRANTED = auto()
    PERMISSION_DENIED = auto()
    MESSAGE_RECEIVED = auto()
    MESSAGE_SENT = auto()
    SESSION_CREATED = auto()
    SESSION_LOADED = auto()


@dataclass
class Event:
    type: EventType
    payload: Any
    timestamp: str = field(
        default_factory=lambda: __import__("datetime").datetime.now().isoformat()
    )


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
