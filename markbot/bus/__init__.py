"""Message bus module for decoupled channel-agent communication."""

from markbot.bus.events import InboundMessage, OutboundMessage
from markbot.bus.queue import MessageBus

__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]
