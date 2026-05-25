"""Tool execution service: handles result truncation and session persistence.

Extracted from AgentLoop to decouple tool execution concerns.
"""

from datetime import datetime
from typing import Any

from loguru import logger

from markbot.agent.context import ContextBuilder
from markbot.agent.iteration import _INTERNAL_CONTEXT_TAG
from markbot.tools.registry import ToolRegistry
from markbot.session.session import Session
from markbot.utils.helpers import strip_ansi


class ToolExecutor:
    """Manages tool execution results with intelligent truncation and persistence.

    Responsibilities:
    - Determine truncation limits per tool type (heavy vs normal)
    - Sanitize multimodal content blocks for storage
    - Save turn messages to session with proper formatting
    """

    DEFAULT_MAX_CHARS = 16_000
    HEAVY_TOOL_MAX_CHARS = 64_000

    def __init__(self, tools: ToolRegistry):
        self._tools = tools

    def get_truncation_limit(self, tool_name: str | None = None) -> int:
        """Get max chars limit for tool result, with higher limit for heavy tools."""
        if tool_name:
            tool = self._tools.get(tool_name)
            if tool and getattr(tool, '_is_heavy_tool', False):
                return self.HEAVY_TOOL_MAX_CHARS
        return self.DEFAULT_MAX_CHARS

    def sanitize_blocks(
        self,
        blocks: list[dict[str, Any]],
        truncate_text: bool = False,
        tool_name: str | None = None,
        drop_runtime: bool = False,
    ) -> list[dict[str, Any]]:
        """Strip volatile multimodal payloads before writing session history.

        Args:
            blocks: Content blocks from tool result
            truncate_text: Whether to truncate long text blocks
            tool_name: Tool name for determining truncation limit
            drop_runtime: Whether to strip runtime context tags
        """
        filtered: list[dict[str, Any]] = []

        for block in blocks:
            btype = block.get("type")

            if btype == "image_url":
                path = (block.get("_meta") or {}).get("path", "")
                filtered.append({"type": "text", "text": f"[image: {path}]" if path else "[image]"})
                continue

            if btype == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                if drop_runtime and text.lstrip().startswith(
                    ContextBuilder._RUNTIME_CONTEXT_TAG
                ):
                    continue
                text = strip_ansi(text)
                max_chars = self.get_truncation_limit(tool_name)
                if truncate_text and len(text) > max_chars:
                    text = text[:max_chars] + "\n... (truncated)"
                filtered.append({**block, "text": text})
                continue

            filtered.append(block)

        return filtered

    def save_turn(
        self,
        session: Session,
        messages: list[dict],
        skip: int,
    ) -> None:
        """Save new-turn messages into session, truncating large tool results.

        Args:
            session: Target session
            messages: Full message list from agent loop
            skip: Number of initial messages to skip (history)
        """
        # Include the current user message at ``skip - 1`` so that
        # ``get_history()`` on the *next* turn returns what the user
        # actually asked, not only assistant replies and tool results.
        _start = max(0, skip - 1)
        for m in messages[_start:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")

            # Never persist system-prompt messages (incl. compaction summaries).
            if role == "system":
                continue

            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue

            if role == "tool":
                tool_name = entry.get("name")
                max_chars = self.get_truncation_limit(tool_name)
                if isinstance(content, str):
                    content = strip_ansi(content)
                    if len(content) > max_chars:
                        content = content[:max_chars] + "\n... (truncated)"
                    entry["content"] = content
                elif isinstance(content, list):
                    filtered = self.sanitize_blocks(
                        content, truncate_text=True, tool_name=tool_name
                    )
                    if not filtered:
                        continue
                    entry["content"] = filtered

            elif role == "user":
                # Skip per-turn internal context messages (memory prefetch,
                # session bootstrap, etc.) — they must not leak into history.
                if isinstance(content, str) and content.startswith(
                    _INTERNAL_CONTEXT_TAG
                ):
                    continue
                if isinstance(content, str) and content.startswith(
                    ContextBuilder._RUNTIME_CONTEXT_TAG
                ):
                    boundary = ContextBuilder._CONTENT_BOUNDARY
                    idx = content.find(boundary)
                    if idx >= 0:
                        remaining = content[idx + len(boundary) :].strip()
                    else:
                        parts = content.split("\n\n", 1)
                        remaining = parts[1].strip() if len(parts) > 1 else ""
                    if remaining:
                        entry["content"] = strip_ansi(remaining)
                    else:
                        continue
                if isinstance(content, list):
                    filtered = self.sanitize_blocks(content, drop_runtime=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered

            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)

        session.updated_at = datetime.now()
