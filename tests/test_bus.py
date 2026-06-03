"""Tests for markbot.bus module (events, emitter, queue)."""

import pytest

from markbot.bus.emitter import EventEmitter
from markbot.bus.events import Event, EventType, InboundMessage, OutboundMessage
from markbot.bus.queue import BackpressurePolicy, MessageBus, Priority


class TestEventType:
    def test_all_types_exist(self):
        assert EventType.STATE_CHANGED is not None
        assert EventType.TOOL_CALLED is not None
        assert EventType.TOOL_COMPLETED is not None
        assert EventType.TOOL_FAILED is not None
        assert EventType.MESSAGE_RECEIVED is not None
        assert EventType.MESSAGE_SENT is not None
        assert EventType.MODEL_CALLED is not None
        assert EventType.MODEL_FAILED is not None
        assert EventType.BUDGET_EXCEEDED is not None

    def test_type_count(self):
        assert len(EventType) >= 20


class TestEvent:
    def test_basic_event(self):
        e = Event(type=EventType.TOOL_CALLED, payload={"tool": "read"})
        assert e.type == EventType.TOOL_CALLED
        assert e.payload == {"tool": "read"}
        assert e.correlation_id == ""
        assert e.session_key == ""

    def test_event_with_ids(self):
        e = Event(
            type=EventType.MESSAGE_SENT,
            payload="hello",
            correlation_id="corr-1",
            session_key="cli:main",
        )
        assert e.correlation_id == "corr-1"
        assert e.session_key == "cli:main"

    def test_event_has_timestamp(self):
        e = Event(type=EventType.STATE_CHANGED, payload=None)
        assert e.timestamp is not None
        assert len(e.timestamp) > 0


class TestInboundMessage:
    def test_basic_message(self):
        msg = InboundMessage(
            channel="cli",
            sender_id="user1",
            chat_id="chat1",
            content="Hello!",
        )
        assert msg.channel == "cli"
        assert msg.session_key == "cli:chat1"

    def test_session_key_override(self):
        msg = InboundMessage(
            channel="cli",
            sender_id="user1",
            chat_id="chat1",
            content="Hello!",
            session_key_override="custom:key",
        )
        assert msg.session_key == "custom:key"

    def test_default_media(self):
        msg = InboundMessage(
            channel="cli", sender_id="u", chat_id="c", content="hi",
        )
        assert msg.media == []
        assert msg.metadata == {}


class TestOutboundMessage:
    def test_basic_message(self):
        msg = OutboundMessage(
            channel="cli",
            chat_id="chat1",
            content="Response!",
        )
        assert msg.channel == "cli"
        assert msg.reply_to is None
        assert msg.media == []


class TestEventEmitter:
    @pytest.mark.asyncio
    async def test_on_and_emit(self):
        emitter = EventEmitter()
        received = []

        @emitter.on(EventType.TOOL_CALLED)
        async def handler(event: Event):
            received.append(event)

        await emitter.emit(EventType.TOOL_CALLED, {"tool": "read"})
        assert len(received) == 1
        assert received[0].payload == {"tool": "read"}

    @pytest.mark.asyncio
    async def test_sync_callback(self):
        emitter = EventEmitter()
        received = []

        @emitter.on(EventType.TOOL_CALLED)
        def handler(event: Event):
            received.append(event)

        await emitter.emit(EventType.TOOL_CALLED, "sync")
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_once_fires_only_once(self):
        emitter = EventEmitter()
        count = 0

        @emitter.once(EventType.TOOL_CALLED)
        async def handler(event: Event):
            nonlocal count
            count += 1

        await emitter.emit(EventType.TOOL_CALLED)
        await emitter.emit(EventType.TOOL_CALLED)
        assert count == 1

    @pytest.mark.asyncio
    async def test_off_removes_callback(self):
        emitter = EventEmitter()
        received = []

        async def handler(event: Event):
            received.append(event)

        emitter.on(EventType.TOOL_CALLED, handler)
        await emitter.emit(EventType.TOOL_CALLED, "first")
        assert len(received) == 1

        emitter.off(EventType.TOOL_CALLED, handler)
        await emitter.emit(EventType.TOOL_CALLED, "second")
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_wildcard_subscription(self):
        emitter = EventEmitter()
        received = []

        @emitter.on(None)
        async def handler(event: Event):
            received.append(event)

        await emitter.emit(EventType.TOOL_CALLED, "a")
        await emitter.emit(EventType.MESSAGE_SENT, "b")
        assert len(received) == 2

    @pytest.mark.asyncio
    async def test_subscriber_error_does_not_break_others(self):
        emitter = EventEmitter()
        received = []

        @emitter.on(EventType.TOOL_CALLED)
        async def bad_handler(event: Event):
            raise ValueError("oops")

        @emitter.on(EventType.TOOL_CALLED)
        async def good_handler(event: Event):
            received.append(event)

        await emitter.emit(EventType.TOOL_CALLED, "test")
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_persist_to_file(self, tmp_path):
        persist_path = tmp_path / "events.jsonl"
        emitter = EventEmitter(persist_path=persist_path)
        await emitter.emit(EventType.TOOL_CALLED, {"tool": "test"})
        content = persist_path.read_text()
        assert "TOOL_CALLED" in content


class TestPriority:
    def test_ordering(self):
        assert Priority.SYSTEM < Priority.HEARTBEAT < Priority.USER


class TestMessageBus:
    @pytest.mark.asyncio
    async def test_publish_and_consume_inbound(self):
        bus = MessageBus()
        msg = InboundMessage(
            channel="cli", sender_id="u", chat_id="c", content="hi",
        )
        await bus.publish_inbound(msg)
        result = await bus.consume_inbound()
        assert result.content == "hi"

    @pytest.mark.asyncio
    async def test_publish_and_consume_outbound(self):
        bus = MessageBus()
        msg = OutboundMessage(
            channel="cli", chat_id="c", content="response",
        )
        await bus.publish_outbound(msg)
        result = await bus.consume_outbound()
        assert result.content == "response"

    @pytest.mark.asyncio
    async def test_priority_ordering(self):
        bus = MessageBus(enable_priority=True)
        low = InboundMessage(channel="cli", sender_id="u", chat_id="c", content="low")
        high = InboundMessage(channel="cli", sender_id="u", chat_id="c", content="high")

        await bus.publish_inbound(low, priority=Priority.USER)
        await bus.publish_inbound(high, priority=Priority.SYSTEM)

        first = await bus.consume_inbound()
        assert first.content == "high"

    def test_backpressure_reject(self):
        bus = MessageBus(maxsize=1, enable_priority=True, backpressure=BackpressurePolicy.REJECT)
        msg1 = InboundMessage(channel="cli", sender_id="u", chat_id="c", content="1")
        msg2 = InboundMessage(channel="cli", sender_id="u", chat_id="c", content="2")

        result1 = bus.publish_inbound_nowait(msg1, priority=Priority.USER)
        result2 = bus.publish_inbound_nowait(msg2, priority=Priority.USER)
        assert result1 is True
        assert result2 is False
