"""Memory management tools for AI self-management.

These tools allow the AI to autonomously manage its long-term memory:
save, forget, and list entries.

Two memory stores:
- 'memory': agent notes — environment facts, project conventions, tool quirks
- 'user': user profile — name, role, preferences, communication style
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
            "or decisions that you want to recall across conversations.\n\n"
            "TWO TARGETS:\n"
            "- 'user': who the user is — name, role, preferences, communication style\n"
            "- 'memory': your notes — environment facts, project conventions, tool quirks\n\n"
            "Do NOT save task progress, session outcomes, or temporary TODO state."
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
                "target": {
                    "type": "string",
                    "enum": ["memory", "user"],
                    "description": (
                        "Which store to save to: 'memory' for agent notes, "
                        "'user' for user profile. Default: 'memory'."
                    ),
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
        target: str = "memory",
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> str:
        if not content:
            return "Error: content is required."
        if not self._memory_manager:
            return "Error: Memory manager is not available."
        if target not in ("memory", "user"):
            return "Error: target must be 'memory' or 'user'."
        try:
            result = await self._memory_manager.add_memory(
                content=content, tags=tags or [], target=target,
            )
            if not result:
                return "Error: Failed to save — character limit may have been reached."
            store_label = "user profile" if target == "user" else "memory"
            return f"Saved to {store_label}: {content[:80]}..."
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
        return (
            "Remove specific entries from your long-term memory by substring match.\n\n"
            "TWO TARGETS:\n"
            "- 'user': remove from user profile\n"
            "- 'memory': remove from agent notes"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "Short unique substring identifying the entry to forget.",
                },
                "target": {
                    "type": "string",
                    "enum": ["memory", "user"],
                    "description": "Which store to remove from. Default: 'memory'.",
                },
            },
            "required": ["memory_id"],
        }

    async def _legacy_execute(
        self,
        memory_id: str = "",
        target: str = "memory",
        **kwargs: Any,
    ) -> str:
        if not memory_id:
            return "Error: memory_id is required."
        if not self._memory_manager:
            return "Error: Memory manager is not available."
        if target not in ("memory", "user"):
            return "Error: target must be 'memory' or 'user'."
        try:
            result = await self._memory_manager.delete_memory(
                memory_id=memory_id, target=target,
            )
            if not result:
                return f"No entry containing '{memory_id[:40]}' found in {target}."
            return f"Memory entry '{memory_id[:40]}' has been forgotten from {target}."
        except Exception as e:
            return f"Error forgetting memory: {e}"


class MemoryListTool(Tool):
    """List recent long-term memory entries."""


    _is_read_only = True
    def __init__(self, memory_manager: BaseMemoryManager | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._memory_manager = memory_manager

    @property
    def name(self) -> str:
        return "memory_list"

    @property
    def description(self) -> str:
        return (
            "List your recent long-term memory entries with their IDs and summaries.\n\n"
            "TWO TARGETS:\n"
            "- 'user': list user profile entries\n"
            "- 'memory': list agent note entries (default)"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of entries to return (default 20)",
                },
                "target": {
                    "type": "string",
                    "enum": ["memory", "user"],
                    "description": "Which store to list. Default: 'memory'.",
                },
            },
        }

    async def _legacy_execute(
        self,
        limit: int = 20,
        target: str = "memory",
        **kwargs: Any,
    ) -> str:
        if not self._memory_manager:
            return "Error: Memory manager is not available."
        if target not in ("memory", "user"):
            return "Error: target must be 'memory' or 'user'."
        try:
            memories = await self._memory_manager.list_memories(
                limit=limit, target=target,
            )
            if not memories:
                return f"No {target} entries found."
            store_label = "User Profile" if target == "user" else "Agent Memory"
            lines = [f"## {store_label} ({len(memories)} entries)\n"]
            for m in memories:
                mid = m.get("id", "?")
                content = m.get("content", "")[:120]
                tags = m.get("tags", [])
                tag_str = f" [{', '.join(tags)}]" if tags else ""
                lines.append(f"- **{mid}**: {content}{tag_str}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error listing memories: {e}"


