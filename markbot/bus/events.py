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
    TOOL_PROGRESS = auto()
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


# Chat-type tokens indicating a multi-party (group) surface.
_GROUP_CHAT_TYPES: frozenset[str] = frozenset({
    "group",
    "channel",
    "guild",
    "forum",
    "topic",
    "topic_group",
    "room",
})

# Chat-type tokens indicating an explicit 1:1 private surface.
_PRIVATE_CHAT_TYPES: frozenset[str] = frozenset({
    "p2p",
    "dm",
    "direct",
    "private",
    "single",
    "c2c",
    "im",
})


def derive_is_group(
    metadata: dict[str, Any] | None,
    chat_id: str | None = None,
) -> bool | None:
    """Best-effort detection of whether an inbound message is from a group chat.

    Returns ``True`` for group / multi-party surfaces, ``False`` for explicit
    1:1 private surfaces, and ``None`` when the channel does not expose enough
    information (callers should fail closed for shared channels).
    """
    meta = metadata or {}
    explicit = meta.get("is_group")
    if isinstance(explicit, bool):
        return explicit
    raw = meta.get("chat_type")
    if raw is not None:
        value = str(raw).strip().lower()
        if value in _GROUP_CHAT_TYPES:
            return True
        if value in _PRIVATE_CHAT_TYPES:
            return False
    # DingTalk encodes group chats as ``group:<conversation_id>``.
    if chat_id and str(chat_id).startswith("group:"):
        return True
    return None


def make_session_key(channel: str | None, chat_id: str | None) -> str | None:
    """Build the canonical ``channel:chat_id`` session key.

    Returns ``None`` when either component is missing so callers can use
    the result directly for optional session-key fields without an extra
    conditional.  Centralising this avoids ``f"{channel}:{chat_id}"` being
    duplicated across the codebase with subtly different empty-handling.
    """
    if not channel or not chat_id:
        return None
    return f"{channel}:{chat_id}"


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
        return self.session_key_override or make_session_key(self.channel, self.chat_id) or ""


@dataclass
class OutboundMessage:
    """Message to send to a chat channel."""

    channel: str
    chat_id: str
    content: str
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
