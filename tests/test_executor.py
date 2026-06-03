"""Tests for markbot.agent.services.executor — Tool result handling."""

from unittest.mock import MagicMock

import pytest

from markbot.agent.services.executor import ToolExecutor
from markbot.tools.registry import ToolRegistry


@pytest.fixture
def tool_registry():
    return ToolRegistry()


@pytest.fixture
def executor(tool_registry):
    return ToolExecutor(tool_registry)


class TestToolExecutor:
    def test_default_truncation_limit(self, executor):
        assert executor.get_truncation_limit() == 16_000
        assert executor.get_truncation_limit("unknown_tool") == 16_000

    def test_heavy_tool_truncation_limit(self, tool_registry):
        from typing import Any

        from markbot.tools.base import Tool

        class HeavyTool(Tool):
            _is_heavy_tool = True

            @property
            def name(self) -> str:
                return "heavy_tool"

            @property
            def description(self) -> str:
                return "Heavy tool"

            @property
            def parameters(self) -> dict[str, Any]:
                return {}

            async def _legacy_execute(self, **kwargs: Any) -> str:
                return ""

        heavy_tool = HeavyTool()
        tool_registry.register(heavy_tool)
        executor = ToolExecutor(tool_registry)
        assert executor.get_truncation_limit("heavy_tool") == 64_000

    def test_sanitize_blocks_text(self, executor):
        blocks = [{"type": "text", "text": "hello world"}]
        result = executor.sanitize_blocks(blocks)
        assert len(result) == 1
        assert result[0]["text"] == "hello world"

    def test_sanitize_blocks_image_url(self, executor):
        blocks = [{"type": "image_url", "url": "data:image/png;base64,...", "_meta": {"path": "/tmp/img.png"}}]
        result = executor.sanitize_blocks(blocks)
        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert "/tmp/img.png" in result[0]["text"]

    def test_sanitize_blocks_image_no_meta(self, executor):
        blocks = [{"type": "image_url", "url": "data:image/png;base64,..."}]
        result = executor.sanitize_blocks(blocks)
        assert result[0]["text"] == "[image]"

    def test_sanitize_blocks_strip_ansi(self, executor):
        blocks = [{"type": "text", "text": "\x1b[31mred\x1b[0m text"}]
        result = executor.sanitize_blocks(blocks)
        assert result[0]["text"] == "red text"

    def test_sanitize_blocks_truncate_text(self, executor):
        long_text = "a" * 20000
        blocks = [{"type": "text", "text": long_text}]
        result = executor.sanitize_blocks(blocks, truncate_text=True)
        assert len(result[0]["text"]) < 20000
        assert "truncated" in result[0]["text"]

    def test_sanitize_blocks_no_truncate(self, executor):
        long_text = "a" * 20000
        blocks = [{"type": "text", "text": long_text}]
        result = executor.sanitize_blocks(blocks, truncate_text=False)
        assert result[0]["text"] == long_text


class TestSaveTurn:
    def test_save_user_message(self, executor):
        session = MagicMock()
        session.messages = []
        messages = [
            {"role": "user", "content": "hello"},
        ]
        executor.save_turn(session, messages, skip=0)
        assert len(session.messages) == 1
        assert session.messages[0]["role"] == "user"
        assert session.messages[0]["content"] == "hello"

    def test_save_assistant_message(self, executor):
        session = MagicMock()
        session.messages = []
        messages = [
            {"role": "assistant", "content": "hi there"},
        ]
        executor.save_turn(session, messages, skip=0)
        assert len(session.messages) == 1

    def test_skip_system_messages(self, executor):
        session = MagicMock()
        session.messages = []
        messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "hello"},
        ]
        executor.save_turn(session, messages, skip=0)
        assert len(session.messages) == 1
        assert session.messages[0]["role"] == "user"

    def test_skip_empty_assistant(self, executor):
        session = MagicMock()
        session.messages = []
        messages = [
            {"role": "assistant", "content": "", "tool_calls": []},
        ]
        executor.save_turn(session, messages, skip=0)
        assert len(session.messages) == 0

    def test_tool_result_truncation(self, executor):
        session = MagicMock()
        session.messages = []
        long_content = "x" * 20000
        messages = [
            {"role": "tool", "content": long_content, "name": "exec"},
        ]
        executor.save_turn(session, messages, skip=0)
        assert len(session.messages[0]["content"]) < 20000

    def test_skip_internal_context(self, executor):
        session = MagicMock()
        session.messages = []
        from markbot.agent.iteration import _INTERNAL_CONTEXT_TAG
        messages = [
            {"role": "user", "content": f"{_INTERNAL_CONTEXT_TAG}internal data"},
            {"role": "user", "content": "real message"},
        ]
        executor.save_turn(session, messages, skip=0)
        assert len(session.messages) == 1
        assert session.messages[0]["content"] == "real message"
