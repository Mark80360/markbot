"""Pluggable context compression engine interface.

MarkBot already has a multi-level :class:`MultiLevelCompactor`. This ABC
documents the narrow waist for alternative engines (LCM, hierarchical
memory, vendor-specific compressors) without forcing a rewrite of
``IterationRunner``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ContextEngineResult:
    """Outcome of a compress attempt."""

    messages: list[dict[str, Any]]
    tokens_before: int = 0
    tokens_after: int = 0
    action: str = "none"
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return self.action not in ("", "none", None)


class ContextEngine(ABC):
    """Narrow interface for context compression strategies."""

    name: str = "base"

    @abstractmethod
    def should_compress(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int,
        context_window_tokens: int,
    ) -> bool:
        """Return True when compression should run before the next LLM call."""

    @abstractmethod
    async def compress(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int,
        context_window_tokens: int,
        *,
        task_context: str | None = None,
    ) -> ContextEngineResult:
        """Compress *messages* and return the rewritten list + telemetry."""

    def update_from_response(self, response: Any) -> None:
        """Optional hook after each model response (usage, tool counts, …)."""

    def tools(self) -> list[Any]:
        """Optional engine-specific tools to expose to the model."""
        return []


class CompactorContextEngine(ContextEngine):
    """Adapter wrapping the existing :class:`MultiLevelCompactor`."""

    name = "multilevel"

    def __init__(self, compactor: Any) -> None:
        self._compactor = compactor

    def should_compress(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int,
        context_window_tokens: int,
    ) -> bool:
        # MultiLevelCompactor.maybe_compact owns the real threshold (including
        # reserved_output / auto_compact_buffer / cooldown). Always enter
        # compress() so we don't double-gate with a diverging formula.
        return True

    async def compress(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int,
        context_window_tokens: int,
        *,
        task_context: str | None = None,
    ) -> ContextEngineResult:
        new_messages, result = await self._compactor.maybe_compact(
            messages,
            current_tokens,
            context_window_tokens,
            task_context=task_context,
        )
        action = getattr(getattr(result, "action", None), "value", None) or str(
            getattr(result, "action", "none")
        )
        return ContextEngineResult(
            messages=new_messages,
            tokens_before=getattr(result, "tokens_before", current_tokens),
            tokens_after=getattr(result, "tokens_after", current_tokens),
            action=action,
            summary=getattr(result, "summary", "") or "",
        )
