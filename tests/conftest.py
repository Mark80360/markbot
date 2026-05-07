"""Shared test fixtures for markbot test suite."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def sample_config_data() -> dict[str, Any]:
    return {
        "agents": {
            "defaults": {
                "workspace": "/tmp/markbot-test",
                "modelChain": ["anthropic/claude-sonnet"],
                "maxTokens": 4096,
                "contextWindowTokens": 65536,
                "temperature": 0.1,
                "maxToolIterations": 20,
                "timezone": "UTC",
            }
        },
        "providers": {
            "anthropic": {
                "apiKey": "sk-test-key-1234567890abcdef",
                "models": [
                    {
                        "id": "claude-sonnet",
                        "name": "claude-sonnet-4-20250514",
                        "maxTokens": 8192,
                        "contextWindow": 65536,
                    }
                ],
            }
        },
        "channels": {"sendProgress": True},
        "tools": {
            "web": {
                "search": {"provider": "duckduckgo"},
            },
            "exec": {"enable": True, "timeout": 30},
        },
        "budget": {"enabled": True, "maxBudgetUsd": 5.0, "warnThresholdUsd": 1.0},
    }


@pytest.fixture
def mock_llm_response():
    from markbot.providers.base import LLMResponse

    return LLMResponse(content="Test response", finish_reason="stop", usage={"input_tokens": 10, "output_tokens": 5})


@pytest.fixture
def mock_fallback_manager():
    fm = AsyncMock()
    fm.chat_with_fallback = AsyncMock(return_value=MagicMock(
        content="Test response",
        has_tool_calls=False,
        tool_calls=[],
        finish_reason="stop",
    ))
    fm.chat_stream_with_fallback = AsyncMock(return_value=MagicMock(
        content="Test response",
        has_tool_calls=False,
        tool_calls=[],
        finish_reason="stop",
    ))
    return fm


@pytest.fixture
def mock_bus():
    from markbot.bus.queue import MessageBus
    return MessageBus(maxsize=10)


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
