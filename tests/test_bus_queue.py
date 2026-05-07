"""Tests for markbot.bus.queue — enhanced MessageBus."""

import asyncio

import pytest

from markbot.bus.events import InboundMessage, OutboundMessage
from markbot.bus.queue import (
    BackpressurePolicy,
    MessageBus,
    Priority,
)


def _inbound(channel="cli", chat_id="direct", content="hello", **kw):
    return InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content, **kw)


class TestBasicMessageBus:
    def test_create_default(self):
        bus = MessageBus()
        assert bus.inbound_size == 0
        assert bus.outbound_size == 0

    @pytest.mark.asyncio
    async def test_publish_consume_inbound(self):
        bus = MessageBus(maxsize=10)
        msg = _inbound(content="hello")
        await bus.publish_inbound(msg)
        assert bus.inbound_size == 1

        consumed = await bus.consume_inbound()
        assert consumed.content == "hello"
        assert bus.inbound_size == 0

    @pytest.mark.asyncio
    async def test_publish_consume_outbound(self):
        bus = MessageBus(maxsize=10)
        msg = OutboundMessage(channel="cli", chat_id="direct", content="response")
        await bus.publish_outbound(msg)
        assert bus.outbound_size == 1

        consumed = await bus.consume_outbound()
        assert consumed.content == "response"

    @pytest.mark.asyncio
    async def test_stats(self):
        bus = MessageBus(maxsize=10)
        msg = _inbound(content="hello")
        await bus.publish_inbound(msg)
        stats = bus.stats
        assert stats["inbound_total"] == 1


class TestPriorityMessageBus:
    def test_create_priority(self):
        bus = MessageBus(maxsize=10, enable_priority=True)
        assert bus.inbound_size == 0

    @pytest.mark.asyncio
    async def test_priority_ordering(self):
        bus = MessageBus(maxsize=10, enable_priority=True)

        user_msg = _inbound(channel="cli", chat_id="direct", content="user")
        system_msg = _inbound(channel="system", chat_id="sys", content="system")

        await bus.publish_inbound(user_msg, priority=Priority.USER)
        await bus.publish_inbound(system_msg, priority=Priority.SYSTEM)

        first = await bus.consume_inbound()
        assert first.content == "system"

        second = await bus.consume_inbound()
        assert second.content == "user"


class TestPartitionedMessageBus:
    def test_create_partitioned(self):
        bus = MessageBus(maxsize=10, enable_partitioning=True)
        assert bus.inbound_size == 0

    @pytest.mark.asyncio
    async def test_partitioned_delivery(self):
        bus = MessageBus(maxsize=10, enable_partitioning=True)

        msg1 = _inbound(channel="cli", chat_id="user1", content="hello1", session_key_override="session-a")
        msg2 = _inbound(channel="cli", chat_id="user2", content="hello2", session_key_override="session-b")

        await bus.publish_inbound(msg1)
        await bus.publish_inbound(msg2)

        assert bus.inbound_size == 2

        consumed = await bus.consume_inbound()
        assert consumed.content in ("hello1", "hello2")
