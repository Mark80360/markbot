"""Tests for markbot.agent.pipeline — Message processing pipeline."""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock

from markbot.agent.pipeline.engine import MessagePipeline, ProcessContext
from markbot.bus.events import InboundMessage, OutboundMessage


@pytest.fixture
def inbound_msg():
    return InboundMessage(
        channel="test",
        chat_id="chat123",
        sender_id="user1",
        content="hello",
    )


@pytest.fixture
def process_ctx(inbound_msg):
    return ProcessContext(msg=inbound_msg, channel="test", chat_id="chat123")


@pytest.fixture
def pipeline():
    return MessagePipeline()


class TestProcessContext:
    def test_basic_context(self, inbound_msg):
        ctx = ProcessContext(msg=inbound_msg)
        assert ctx.msg == inbound_msg
        assert ctx.session_key is None
        assert ctx.session is None
        assert ctx.channel == ""
        assert ctx.chat_id == ""
        assert ctx.extra == {}

    def test_context_with_extra(self, inbound_msg):
        ctx = ProcessContext(msg=inbound_msg, extra={"key": "value"})
        assert ctx.extra["key"] == "value"


class TestMessagePipeline:
    def test_empty_pipeline(self, pipeline):
        assert pipeline.middleware_count == 0

    def test_use_adds_middleware(self, pipeline):
        mw = MagicMock()
        pipeline.use(mw)
        assert pipeline.middleware_count == 1

    def test_use_chaining(self, pipeline):
        mw1 = MagicMock()
        mw2 = MagicMock()
        result = pipeline.use(mw1).use(mw2)
        assert result is pipeline
        assert pipeline.middleware_count == 2

    @pytest.mark.asyncio
    async def test_process_calls_handler(self, pipeline, process_ctx):
        handler = AsyncMock(return_value=OutboundMessage(
            channel="test", chat_id="chat123", content="response"
        ))
        result = await pipeline.process(process_ctx, handler)
        handler.assert_called_once_with(process_ctx)
        assert result.content == "response"

    @pytest.mark.asyncio
    async def test_before_short_circuit(self, pipeline, process_ctx):
        short_circuit = OutboundMessage(
            channel="test", chat_id="chat123", content="short"
        )
        mw = MagicMock()
        mw.before = AsyncMock(return_value=short_circuit)
        mw.after = AsyncMock(side_effect=lambda ctx, r: r)
        mw.on_error = AsyncMock()

        pipeline.use(mw)
        handler = AsyncMock()

        result = await pipeline.process(process_ctx, handler)
        handler.assert_not_called()
        assert result.content == "short"

    @pytest.mark.asyncio
    async def test_before_no_short_circuit(self, pipeline, process_ctx):
        mw = MagicMock()
        mw.before = AsyncMock(return_value=None)
        mw.after = AsyncMock(side_effect=lambda ctx, r: r)
        mw.on_error = AsyncMock()

        pipeline.use(mw)
        response = OutboundMessage(
            channel="test", chat_id="chat123", content="ok"
        )
        handler = AsyncMock(return_value=response)

        result = await pipeline.process(process_ctx, handler)
        handler.assert_called_once()
        assert result.content == "ok"

    @pytest.mark.asyncio
    async def test_after_modifies_response(self, pipeline, process_ctx):
        original = OutboundMessage(
            channel="test", chat_id="chat123", content="original"
        )
        modified = OutboundMessage(
            channel="test", chat_id="chat123", content="modified"
        )

        mw = MagicMock()
        mw.before = AsyncMock(return_value=None)
        mw.after = AsyncMock(return_value=modified)
        mw.on_error = AsyncMock()

        pipeline.use(mw)
        handler = AsyncMock(return_value=original)

        result = await pipeline.process(process_ctx, handler)
        assert result.content == "modified"

    @pytest.mark.asyncio
    async def test_multiple_middleware_order(self, pipeline, process_ctx):
        call_order = []

        mw1 = MagicMock()
        mw1.before = AsyncMock(side_effect=lambda ctx: call_order.append("mw1_before") or None)
        mw1.after = AsyncMock(side_effect=lambda ctx, r: call_order.append("mw1_after") or r)
        mw1.on_error = AsyncMock()

        mw2 = MagicMock()
        mw2.before = AsyncMock(side_effect=lambda ctx: call_order.append("mw2_before") or None)
        mw2.after = AsyncMock(side_effect=lambda ctx, r: call_order.append("mw2_after") or r)
        mw2.on_error = AsyncMock()

        pipeline.use(mw1).use(mw2)
        handler = AsyncMock(return_value=OutboundMessage(
            channel="test", chat_id="chat123", content="ok"
        ))

        await pipeline.process(process_ctx, handler)

        # before: mw1 -> mw2
        # after: mw2 -> mw1 (reversed)
        assert call_order == ["mw1_before", "mw2_before", "mw2_after", "mw1_after"]

    @pytest.mark.asyncio
    async def test_handler_error_calls_on_error(self, pipeline, process_ctx):
        mw = MagicMock()
        mw.before = AsyncMock(return_value=None)
        mw.after = AsyncMock()
        mw.on_error = AsyncMock()

        pipeline.use(mw)
        handler = AsyncMock(side_effect=ValueError("handler error"))

        with pytest.raises(ValueError, match="handler error"):
            await pipeline.process(process_ctx, handler)

        mw.on_error.assert_called_once()
        error = mw.on_error.call_args[0][1]
        assert isinstance(error, ValueError)

    @pytest.mark.asyncio
    async def test_before_error_calls_on_error(self, pipeline, process_ctx):
        mw = MagicMock()
        mw.before = AsyncMock(side_effect=ValueError("before error"))
        mw.after = AsyncMock()
        mw.on_error = AsyncMock()

        pipeline.use(mw)
        handler = AsyncMock(return_value=OutboundMessage(
            channel="test", chat_id="chat123", content="ok"
        ))

        # Should not raise - error is caught
        result = await pipeline.process(process_ctx, handler)
        mw.on_error.assert_called_once()
