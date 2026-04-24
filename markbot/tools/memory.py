"""Memory search tool for semantic/full-text search in memory files.

Supports both manual agent-initiated searches (via tool call) and
automatic forced injection (force_memory_search) before each LLM call.
Ported from MemorySearchTool.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from loguru import logger

from markbot.tools.base import Tool

if TYPE_CHECKING:
    from ..memory.base import BaseMemoryManager


class MemorySearchTool(Tool):
    """Search MEMORY.md and memory/*.md files semantically.

    Use this tool before answering questions about prior work, decisions,
    dates, people, preferences, or todos. Returns top relevant snippets.

    When ``force_memory_search`` is enabled on the manager, this tool also
    provides automatic pre-LLM-call search injection via ``get_forced_context()``.
    """

    def __init__(self, memory_manager: "BaseMemoryManager | None" = None, **kwargs):
        super().__init__(**kwargs)
        self._memory_manager = memory_manager

    @property
    def name(self) -> str:
        return "memory_search"

    @property
    def description(self) -> str:
        return (
            "Search MEMORY.md and memory/*.md files semantically. "
            "Use when uncertain about previous decisions, user preferences, "
            "past conversations, or when the user refers to something mentioned earlier."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Semantic search query. "
                        "Examples: 'API design', 'database schema', 'user preferences'"
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "default": 5,
                    "description": "Maximum results (default: 5)",
                },
                "min_score": {
                    "type": "number",
                    "default": 0.1,
                    "description": "Minimum similarity score (default: 0.1)",
                },
            },
            "required": ["query"],
        }

    async def _legacy_execute(
        self,
        query: str | None = None,
        max_results: int = 5,
        min_score: float = 0.1,
        **kwargs: Any,
    ) -> str:
        if not query:
            return "Error: Query parameter is required."

        if not self._memory_manager:
            return "Error: Memory manager is not enabled."

        try:
            results = await self._memory_manager.memory_search(
                query=query,
                max_results=max_results,
                min_score=min_score,
            )
            if not results:
                return f"No memories found for query: '{query}'"
            return self._format_results(results, query)
        except Exception as e:
            logger.error(f"Memory search failed: {e}")
            return f"Error: Memory search failed — {e}"

    async def get_forced_context(self, user_message: str) -> str:
        """Get forced memory search context for automatic injection.

        When ``force_memory_search`` is enabled, this is called before each
        LLM call to inject relevant memories into context.

        Args:
            user_message: Current user message to use as search query

        Returns:
            Formatted string of search results, or empty string if disabled/no results
        """
        if not self._memory_manager:
            return ""

        if not getattr(self._memory_manager, "force_memory_search", False):
            return ""

        try:
            max_r = getattr(self._memory_manager, "force_max_results", 1)
            min_s = getattr(self._memory_manager, "force_min_score", 0.3)
            results = await self._memory_manager.memory_search(
                query=user_message,
                max_results=max_r,
                min_score=min_s,
            )

            if not results:
                return ""

            lines = ["## Forced Memory Search Results\n"]
            for idx, r in enumerate(results, 1):
                content = r.get("content", "")
                source = r.get("source", r.get("file", "memory"))
                lines.append(f"{idx}. [{source}]")
                if len(content) > 1500:
                    content = content[:1500] + "\n... [truncated]"
                lines.append(content)
                lines.append("")

            return "\n".join(lines)

        except Exception as e:
            logger.warning(f"Forced memory search failed: {e}")
            return ""

    @staticmethod
    def _format_results(results: list[dict], query: str) -> str:
        lines = [
            f"## Memory Search Results: '{query}'\n",
            f"Found {len(results)} result{'s' if len(results) != 1 else ''}:\n",
        ]
        for idx, result in enumerate(results, 1):
            content = result.get("content", "")
            source = result.get("source", result.get("file", "memory"))
            score = result.get("score", result.get("relevance"))

            lines.append(f"### {idx}. {source}")
            if score is not None:
                lines.append(f"- Relevance: {score}")
            lines.append("- Content:")
            lines.append("```")

            if len(content) > 2000:
                content = content[:2000] + "\n... [truncated]"
            lines.append(content)
            lines.append("```\n")

        lines.append("---")
        return "\n".join(lines)
