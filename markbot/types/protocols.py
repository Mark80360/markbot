"""Protocol interfaces for markbot core components.

These protocols enable type-safe dependency injection and loose coupling
between modules, replacing raw ``Any`` types with explicit contracts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SessionProtocol(Protocol):
    """Minimal interface that any session implementation must satisfy."""

    key: str
    messages: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any]
    last_consolidated: int

    def add_message(self, role: str, content: str, **kwargs: Any) -> None: ...

    def get_history(self, max_messages: int = 500) -> list[dict[str, Any]]: ...

    def clear(self) -> None: ...


@runtime_checkable
class SessionManagerProtocol(Protocol):
    """Minimal interface for session management."""

    def get_or_create(self, key: str) -> SessionProtocol: ...

    def save(self, session: SessionProtocol) -> None: ...

    def invalidate(self, key: str) -> None: ...


@runtime_checkable
class MemoryManagerProtocol(Protocol):
    """Minimal interface for memory management."""

    async def start(self) -> None: ...

    async def close(self) -> bool: ...

    async def memory_search(
        self,
        query: str,
        max_results: int = 5,
        min_score: float = 0.1,
        *,
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> Any: ...

    def get_compressed_summary(self, *, session_key: str | None = None) -> str: ...

    def set_compressed_summary(self, summary: str, *, session_key: str | None = None) -> None: ...


@runtime_checkable
class CostTrackerProtocol(Protocol):
    """Minimal interface for cost tracking."""

    total_cost: float

    def is_over_budget(self) -> bool: ...

    def add_usage(
        self,
        *,
        model: str = "unknown",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
    ) -> float: ...


@runtime_checkable
class FallbackManagerProtocol(Protocol):
    """Minimal interface for multi-model fallback."""

    async def chat_with_fallback(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> Any: ...

    async def chat_stream_with_fallback(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Any | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> Any: ...


@runtime_checkable
class ChannelProtocol(Protocol):
    """Minimal interface for chat channel implementations."""

    name: str
    display_name: str

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def send(self, msg: Any) -> None: ...

    async def health_check(self) -> dict[str, Any]: ...

    @property
    def is_running(self) -> bool: ...


from typing import TypedDict


class ToolCallPayload(TypedDict, total=False):
    id: str
    name: str
    arguments: dict[str, Any]
    extra_content: dict[str, Any] | None


class UsagePayload(TypedDict, total=False):
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int


class LLMResponsePayload(TypedDict, total=False):
    content: str | None
    tool_calls: list[ToolCallPayload]
    finish_reason: str
    usage: UsagePayload
    reasoning_content: str | None


class InboundMessageMetadata(TypedDict, total=False):
    question_id: str
    _wants_stream: bool
    _progress: bool
    _tool_hint: bool


class OutboundMessageMetadata(TypedDict, total=False):
    _progress: bool
    _tool_hint: bool
    # Feishu/DingTalk group @mention: list of open_id strings or
    # {"user_id": "...", "name": "..."} dicts. Use "all" to @everyone.
    mentions: list  # list[str | dict[str, str]]
