"""Tool system types."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from markbot.types.permission import PermissionMode, ToolPermissionContext

# OpenAI-compatible providers (DeepSeek, etc.) require tool names to match
# ^[a-zA-Z0-9_-]+$. Some local names (e.g. "tmux.find-sessions" from skill
# scripts, or MCP server names containing dots/colons) include characters
# that get rejected with HTTP 400. We normalise those at schema-generation
# time so the in-process name keeps its meaning while the wire name is safe.
_TOOL_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize_tool_name(name: str, max_len: int = 64) -> str:
    """Return *name* with all characters outside [a-zA-Z0-9_-] replaced by '_'.

    The result is truncated to *max_len* to satisfy provider length limits
    (e.g. 64 chars for some OpenAI-compatible endpoints). If sanitising
    yields an empty string, a single underscore is returned.
    """
    if not isinstance(name, str):
        name = str(name)
    cleaned = _TOOL_NAME_PATTERN.sub("_", name)
    if max_len and len(cleaned) > max_len:
        cleaned = cleaned[:max_len]
    return cleaned or "_"


@dataclass
class ToolParameter:
    name: str
    type: str
    description: str
    required: bool = True
    default: Any = None
    enum: Optional[list[Any]] = None


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: list[ToolParameter]
    aliases: list[str] = field(default_factory=list)
    is_read_only: bool = False
    is_destructive: bool = False
    # Optional service-gate label for docs / tooling (e.g. "requires BRAVE_API_KEY").
    # Actual availability is enforced by BaseTool.available_when / is_available().
    availability_hint: str = ""

    def to_openai_schema(self) -> dict[str, Any]:
        properties = {}
        required = []

        for param in self.parameters:
            prop: dict[str, Any] = {
                "type": param.type,
                "description": param.description,
            }
            if param.enum:
                prop["enum"] = param.enum
            properties[param.name] = prop

            if param.required:
                required.append(param.name)

        return {
            "type": "function",
            "function": {
                "name": _sanitize_tool_name(self.name),
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    def to_anthropic_schema(self) -> dict[str, Any]:
        properties = {}
        required = []

        for param in self.parameters:
            prop: dict[str, Any] = {
                "type": param.type,
                "description": param.description,
            }
            if param.enum:
                prop["enum"] = param.enum
            properties[param.name] = prop

            if param.required:
                required.append(param.name)

        return {
            "name": _sanitize_tool_name(self.name),
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }


@dataclass
class ToolContext:
    session_id: str
    workspace: str
    permission_mode: PermissionMode
    tool_permission_context: ToolPermissionContext
    is_non_interactive: bool = False
    channel: str = ""
    chat_id: str = ""
    message_id: str | None = None

    report_progress: Optional[Callable[[str, Optional[float]], None]] = None
    add_notification: Optional[Callable[[str, str], None]] = None
