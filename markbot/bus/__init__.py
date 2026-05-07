"""Message bus module for decoupled channel-agent communication."""

from markbot.bus.events import EventType, Event, InboundMessage, OutboundMessage
from markbot.bus.queue import (
    MessageBus,
    Priority,
    BackpressurePolicy,
    QueueFullError,
)
from markbot.bus.emitter import EventEmitter, get_event_emitter

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
