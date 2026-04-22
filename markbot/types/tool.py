"""Tool system types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from markbot.types.permission import PermissionMode, ToolPermissionContext


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
                "name": self.name,
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
            "name": self.name,
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

    report_progress: Optional[Callable[[str, Optional[float]], None]] = None
    add_notification: Optional[Callable[[str, str], None]] = None
