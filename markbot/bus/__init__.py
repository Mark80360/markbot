"""Message bus module for decoupled channel-agent communication."""

from markbot.bus.emitter import EventEmitter, get_event_emitter
from markbot.bus.events import Event, EventType, InboundMessage, OutboundMessage
from markbot.bus.queue import (
    BackpressurePolicy,
    MessageBus,
    Priority,
    QueueFullError,
)

__all__ = [
    "MessageBus",
    "EventType",
    "Event",
    "InboundMessage",
    "OutboundMessage",
    "Priority",
    "BackpressurePolicy",
    "QueueFullError",
    "EventEmitter",
    "get_event_emitter",
]
