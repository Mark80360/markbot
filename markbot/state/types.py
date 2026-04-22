"""State management types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from markbot.types.permission import PermissionMode, ToolPermissionContext


@dataclass
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]]
    timestamp: str = field(
        default_factory=lambda: __import__("datetime").datetime.now().isoformat()
    )
    tool_calls: Optional[list[dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AppState:
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    tool_permission_context: ToolPermissionContext = field(
        default_factory=lambda: ToolPermissionContext(mode=PermissionMode.DEFAULT)
    )

    active_tools: list[str] = field(default_factory=list)
    active_skills: list[str] = field(default_factory=list)

    is_processing: bool = False
    current_tool_use: Optional[dict[str, Any]] = None

    theme: str = "default"
    verbose: bool = False

    def copy(self) -> AppState:
        from dataclasses import replace

        return replace(self)
