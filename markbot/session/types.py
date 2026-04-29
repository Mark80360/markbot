"""State management types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from markbot.types.permission import PermissionMode, ToolPermissionContext


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
    current_session: Optional[Any] = None

    theme: str = "default"
    verbose: bool = False

    def copy(self) -> AppState:
        from dataclasses import replace

        return replace(self)
