"""Memory search tool for querying historical conversations and memories.

Provides both keyword-based and semantic search capabilities across
the tiered memory system (Hot/Warm/Cold layers).
"""

import re
from typing import Any

from markbot.agent.tools.base import Tool


class SearchHistoryTool(Tool):
    """Search historical conversations, memories, and past discussions.

    Use this tool when you need to:
    - Find information from previous conversations or decisions
    - Look up user preferences or past choices
    - Retrieve context about earlier technical discussions
    - Answer questions that reference "before", "previously", "last time", etc.
    - Gather context when you're uncertain about historical details

    This tool searches across all memory layers:
    - **Cold Memory**: Semantic search in persistent archived memories
    - **Warm Memory**: Recent conversation logs and activity records
    - **Hot Memory**: Current important facts and working context
    """

    _is_lightweight_tool = True

    def __init__(self, memory_manager=None, **kwargs):
        super().__init__(**kwargs)
        self._memory_manager = memory_manager

    @property
    def name(self) -> str:
        return "search_history"

    @property
    def description(self) -> str:
        return (
            "Search historical conversations, memories, and past discussions. "
            "Use this when uncertain about previous decisions, user preferences, "
            "past conversations, or when the user refers to something mentioned earlier. "
            "Supports both keyword matching and semantic search."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Search query - keywords, topic, question, or phrase to search for. "
                        "Examples: 'API design', 'database schema', 'user preferences', 'deployment config'"
                    )
                },
                "scope": {
                    "type": "string",
                    "enum": ["all", "cold", "warm", "hot"],
                    "default": "all",
                    "description": (
                        "Memory layer(s) to search:\n"
                        "- **all**: Search all layers (default)\n"
                        "- **cold**: Persistent archived memories (semantic search)\n"
                        "- **warm**: Recent activity logs (last 30 days)\n"
                        "- **hot**: Current important facts and context"
                    )
                },
                "limit": {
                    "type": "integer",
                    "default": 5,
                    "ge=1": True,
                    "le=20": True,
                    "description": "Maximum number of results to return (1-20)"
                },
                "days": {
                    "type": "integer",
                    "default=30": True,
                    "description": "For warm memory: number of recent days to search (default: 30)"
                }
            },
            "required": ["query"]
        }

    async def _legacy_execute(
        self,
        query: str | None = None,
        scope: str = "all",
        limit: int = 5,
        days: int = 30,
        **kwargs: Any,
    ) -> str:
        if not query:
            return "Error: Query parameter is required. Please provide a search term."

        if not self._memory_manager:
            return "Error: Memory manager not available. Memory search is disabled."

        results = []
        
        try:
            if scope in ("all", "cold"):
                cold_results = self._search_cold_memory(query, limit)
                results.extend(cold_results)

            if scope in ("all", "warm"):
                warm_results = self._search_warm_memory(query, days, limit)
                results.extend(warm_results)

            if scope in ("all", "hot"):
                hot_results = self._search_hot_memory(query)
                results.extend(hot_results)

            if not results:
                return f"No memories found for query: '{query}'\n\nSuggestions:\n- Try different keywords\n- Use broader terms\n- Check spelling"

            formatted = self._format_results(results[:limit], query)
            return formatted

        except Exception as e:
            return f"Error searching memory: {e}"

    def _search_cold_memory(self, query: str, limit: int) -> list[dict]:
        """Search cold (persistent) memory using semantic search."""
        if not hasattr(self._memory_manager, 'search_cold_memory'):
            return []

        try:
            results = self._memory_manager.search_cold_memory(query, limit=limit)
            return [
                {
                    "source": "cold_memory",
                    "title": r.get("metadata", {}).get("title", r.get("title", "Untitled")),
                    "content": r.get("content", ""),
                    "score": round((1 - r.get("distance", 1)) * 10, 2) if r.get("distance") is not None else r.get("score", 5),
                    "date": r.get("metadata", {}).get("date", r.get("date", "")),
                }
                for r in results
            ]
        except Exception:
            return []

    def _search_warm_memory(self, query: str, days: int, limit: int) -> list[dict]:
        """Search warm (recent) memory using keyword matching."""
        if not hasattr(self._memory_manager, 'warm') or not self._memory_manager.warm:
            return []

        try:
            warm = self._memory_manager.warm
            if not hasattr(warm, 'search_recent'):
                return []

            results = warm.search_recent(query, days=days, limit=limit)
            return [
                {
                    "source": "warm_memory",
                    "title": r.get("header", f"Activity - {r.get('date', 'Unknown')}"),
                    "content": r.get("preview", ""),
                    "date": r.get("date", ""),
                    "relevance": self._calculate_keyword_score(query, r.get("preview", "")),
                }
                for r in results
            ]
        except Exception:
            return []

    def _search_hot_memory(self, query: str) -> list[dict]:
        """Search hot (current) memory for relevant facts."""
        if not hasattr(self._memory_manager, 'hot') or not self._memory_manager.hot:
            return []

        try:
            hot = self._memory_manager.hot
            if not hasattr(hot, 'get_context'):
                return []

            context = hot.get_context()
            if not context:
                return []

            score = self._calculate_keyword_score(query, context)
            if score > 0:
                return [{
                    "source": "hot_memory",
                    "title": "Current Context",
                    "content": context,
                    "relevance": score,
                }]
            return []
        except Exception:
            return []

    @staticmethod
    def _calculate_keyword_score(query: str, text: str) -> int:
        """Calculate relevance score based on keyword overlap."""
        if not text:
            return 0
        
        query_tokens = {token.lower() for token in re.findall(r"[A-Za-z0-9_\u4e00-\u9fff]+", query)
                       if len(token) >= 2}
        text_lower = text.lower()
        
        score = sum(1 for token in query_tokens if token in text_lower)
        return score

    def _format_results(self, results: list[dict], query: str) -> str:
        """Format search results into readable output."""
        lines = [
            f"## Search Results for: '{query}'\n",
            f"Found {len(results)} relevant memor{'y' if len(results) == 1 else 'ies'}:\n",
        ]

        for idx, result in enumerate(results, 1):
            source = result.get("source", "unknown")
            title = result.get("title", "Untitled")
            content = result.get("content", "")
            date = result.get("date", "")
            score = result.get("score") or result.get("relevance", 0)

            lines.append(f"### {idx}. {title}")
            lines.append(f"- **Source**: {source}")
            if date:
                lines.append(f"- **Date**: {date}")
            if score:
                lines.append(f"- **Relevance Score**: {score}/10")
            lines.append("- **Content**:")
            lines.append("```")
            
            max_content_len = 2000
            if len(content) > max_content_len:
                content = content[:max_content_len] + "\n... [truncated]"
            lines.append(content)
            
            lines.append("```\n")

        lines.append("---")
        lines.append("**Tip**: Use specific details from these results to provide accurate, contextual responses.")

        return "\n".join(lines)
