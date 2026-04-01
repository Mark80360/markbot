"""Core type definitions for markbot.

Inspired by MarkBot's architecture.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import (
    Any,
    Callable,
    Literal,
    Optional,
    Protocol,
    runtime_checkable,
)

# ============================================================================
# Permission System
# ============================================================================


class PermissionMode(Enum):
    """Permission modes for tool execution.

    Inspired by MarkBot's permission system.
    """

    DEFAULT = "default"  # 默认模式：询问所有非只读操作
    PLAN = "plan"  # 计划模式：暂停执行等待确认
    ACCEPT_EDITS = "accept_edits"  # 自动接受编辑操作
    BYPASS = "bypass_permissions"  # 绕过权限检查（危险）
    AUTO = "auto"  # 自动模式：使用分类器判断


@dataclass(frozen=True)
class PermissionDecision:
    """Decision from permission check."""

    behavior: Literal["allow", "deny", "ask"]
    reason: Optional[str] = None
    updated_input: Optional[dict[str, Any]] = None


# ============================================================================
# Tool System
# ============================================================================


@dataclass
class ToolParameter:
    """Tool parameter definition."""

    name: str
    type: str  # string, integer, number, boolean, array, object
    description: str
    required: bool = True
    default: Any = None
    enum: Optional[list[Any]] = None


@dataclass
class ToolDefinition:
    """Complete tool definition for LLM."""

    name: str
    description: str
    parameters: list[ToolParameter]
    aliases: list[str] = field(default_factory=list)
    is_read_only: bool = False
    is_destructive: bool = False

    def to_openai_schema(self) -> dict[str, Any]:
        """Convert to OpenAI function schema."""
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
        """Convert to Anthropic tool schema."""
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
    """Context for tool execution."""

    session_id: str
    workspace: str
    permission_mode: PermissionMode
    tool_permission_context: ToolPermissionContext
    is_non_interactive: bool = False

    # Callbacks
    report_progress: Optional[Callable[[str, Optional[float]], None]] = None
    add_notification: Optional[Callable[[str, str], None]] = None


@dataclass
class ToolPermissionContext:
    """Context for permission decisions."""

    mode: PermissionMode
    always_allow: set[str] = field(default_factory=set)
    always_deny: set[str] = field(default_factory=set)
    always_ask: set[str] = field(default_factory=set)
    is_bypass_available: bool = False


# ============================================================================
# Skill System
# ============================================================================


@dataclass
class SkillScriptDef:
    """Executable script definition within a skill (data class)."""

    name: str
    description: str
    entry: str
    language: Literal["python", "bash", "javascript"]
    parameters: list[ToolParameter]
    sandbox_config: Optional[dict[str, Any]] = None


@dataclass
class SkillDefinition:
    """Skill definition."""

    name: str
    description: str
    when_to_use: str
    scripts: list[SkillScriptDef] = field(default_factory=list)
    is_builtin: bool = False
    is_always_active: bool = False


# ============================================================================
# State Management
# ============================================================================


@dataclass
class Message:
    """Conversation message."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]]
    timestamp: str = field(
        default_factory=lambda: __import__("datetime").datetime.now().isoformat()
    )
    tool_calls: Optional[list[dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Session:
    """Conversation session."""

    id: str
    messages: list[Message] = field(default_factory=list)
    created_at: str = field(
        default_factory=lambda: __import__("datetime").datetime.now().isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: __import__("datetime").datetime.now().isoformat()
    )
    metadata: dict[str, Any] = field(default_factory=dict)

    def add_message(self, role: str, content: Any, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = Message(role=role, content=content, **kwargs)
        self.messages.append(msg)
        self.updated_at = __import__("datetime").datetime.now().isoformat()


@dataclass
class AppState:
    """Global application state."""

    # Session
    current_session: Optional[Session] = None

    # Permissions
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    tool_permission_context: ToolPermissionContext = field(
        default_factory=lambda: ToolPermissionContext(mode=PermissionMode.DEFAULT)
    )

    # Tools & Skills
    active_tools: list[str] = field(default_factory=list)
    active_skills: list[str] = field(default_factory=list)

    # Execution
    is_processing: bool = False
    current_tool_use: Optional[dict[str, Any]] = None

    # UI
    theme: str = "default"
    verbose: bool = False

    def copy(self) -> AppState:
        """Create a copy of the state."""
        from dataclasses import replace

        return replace(self)


# ============================================================================
# Events
# ============================================================================


class EventType(Enum):
    """Event types for the event bus."""

    STATE_CHANGED = auto()
    TOOL_CALLED = auto()
    TOOL_COMPLETED = auto()
    PERMISSION_REQUESTED = auto()
    PERMISSION_GRANTED = auto()
    PERMISSION_DENIED = auto()
    MESSAGE_RECEIVED = auto()
    MESSAGE_SENT = auto()
    SESSION_CREATED = auto()
    SESSION_LOADED = auto()


@dataclass
class Event:
    """Event for the event bus."""

    type: EventType
    payload: Any
    timestamp: str = field(
        default_factory=lambda: __import__("datetime").datetime.now().isoformat()
    )
