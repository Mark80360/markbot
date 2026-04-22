"""Permission system types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Optional


class PermissionMode(Enum):
    DEFAULT = "default"
    PLAN = "plan"
    ACCEPT_EDITS = "accept_edits"
    BYPASS = "bypass_permissions"
    AUTO = "auto"


@dataclass(frozen=True)
class PermissionDecision:
    behavior: Literal["allow", "deny", "ask"]
    reason: Optional[str] = None
    updated_input: Optional[dict[str, Any]] = None


@dataclass
class ToolPermissionContext:
    mode: PermissionMode
    always_allow: set[str] = field(default_factory=set)
    always_deny: set[str] = field(default_factory=set)
    always_ask: set[str] = field(default_factory=set)
    is_bypass_available: bool = False
