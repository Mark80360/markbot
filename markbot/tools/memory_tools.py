"""Memory management tools for AI self-management.

These tools allow the AI to autonomously manage its long-term memory:
search (existing), save, forget, list, and trigger dream optimisation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from markbot.tools.base import Tool

if TYPE_CHECKING:
    from markbot.memory.base import BaseMemoryManager


class MemorySaveTool(Tool):
    """Save important information to long-term memory."""

    def __init__(self, memory_manager: BaseMemoryManager | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._memory_manager = memory_manager

    @property
    def name(self) -> str:
        return "memory_save"

    @property
    def description(self) -> str:
        return (
            "Save a key piece of information to your long-term memory. "
            "Use when you learn something important about the user, project, "
            "or decisions that you want to recall across conversations."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The information to remember. Be specific and include context.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags for categorisation (e.g. ['user-preference', 'project-decision'])",
                },
            },
            "required": ["content"],
        }

    async def _legacy_execute(
        self,
        content: str = "",
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> str:
        if not content:
            return "Error: content is required."
        if not self._memory_manager:
            return "Error: Memory manager is not available."
        try:
            await self._memory_manager.add_memory(content=content, tags=tags or [])
            return f"Saved to memory: {content[:80]}..."
        except Exception as e:
            return f"Error saving memory: {e}"


class MemoryForgetTool(Tool):
    """Remove specific entries from long-term memory."""

    def __init__(self, memory_manager: BaseMemoryManager | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._memory_manager = memory_manager

    @property
    def name(self) -> str:
        return "memory_forget"

    @property
    def description(self) -> str:
        return "Remove specific entries from your long-term memory by memory ID."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "The ID of the memory entry to forget",
                },
            },
            "required": ["memory_id"],
        }

    async def _legacy_execute(self, memory_id: str = "", **kwargs: Any) -> str:
        if not memory_id:
            return "Error: memory_id is required."
        if not self._memory_manager:
            return "Error: Memory manager is not available."
        try:
            await self._memory_manager.delete_memory(memory_id=memory_id)
            return f"Memory entry '{memory_id}' has been forgotten."
        except Exception as e:
            return f"Error forgetting memory: {e}"


class MemoryListTool(Tool):
    """List recent long-term memory entries."""

    def __init__(self, memory_manager: BaseMemoryManager | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._memory_manager = memory_manager

    @property
    def name(self) -> str:
        return "memory_list"

    @property
    def description(self) -> str:
        return "List your recent long-term memory entries with their IDs and summaries."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of entries to return (default 20)",
                },
            },
        }

    async def _legacy_execute(self, limit: int = 20, **kwargs: Any) -> str:
        if not self._memory_manager:
            return "Error: Memory manager is not available."
        try:
            memories = await self._memory_manager.list_memories(limit=limit)
            if not memories:
                return "No memories found."
            lines = [f"## Memories ({len(memories)} entries)\n"]
            for m in memories:
                mid = m.get("id", "?")
                content = m.get("content", "")[:120]
                tags = m.get("tags", [])
                tag_str = f" [{', '.join(tags)}]" if tags else ""
                lines.append(f"- **{mid}**: {content}{tag_str}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error listing memories: {e}"


class DreamTool(Tool):
    """Trigger dream-based memory optimisation."""

    def __init__(self, memory_manager: BaseMemoryManager | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._memory_manager = memory_manager

    @property
    def name(self) -> str:
        return "dream"

    @property
    def description(self) -> str:
        return (
            "Trigger an AI-driven memory optimisation cycle (Dream). "
            "This reads your memory store, summarises, merges duplicates, "
            "and cleans outdated entries. Use when you feel your memory "
            "is cluttered or outdated."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def _legacy_execute(self, **kwargs: Any) -> str:
        if not self._memory_manager:
            return "Error: Memory manager is not available."
        try:
            result = await self._memory_manager.dream()
            return f"Dream optimisation completed: {result}" if result else "Dream optimisation completed."
        except Exception as e:
            return f"Error during dream optimisation: {e}"
