"""Skill system types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from markbot.types.tool import ToolParameter


@dataclass
class SkillConfigVar:
    key: str
    description: str
    default: Optional[str] = None
    prompt: str = ""


@dataclass
class SkillConditions:
    requires_tools: list[str] = field(default_factory=list)
    fallback_for_tools: list[str] = field(default_factory=list)


@dataclass
class SkillScriptDef:
    name: str
    description: str
    entry: str
    language: Literal["python", "bash", "javascript"]
    parameters: list[ToolParameter]
    sandbox_config: Optional[dict[str, Any]] = None


class SkillState:
    """Skill lifecycle states."""

    ACTIVE = "active"
    STALE = "stale"
    ARCHIVED = "archived"


@dataclass
class SkillDefinition:
    name: str
    description: str
    when_to_use: str
    scripts: list[SkillScriptDef] = field(default_factory=list)
    is_builtin: bool = False
    is_always_active: bool = False
    config_vars: list[SkillConfigVar] = field(default_factory=list)
    conditions: SkillConditions = field(default_factory=SkillConditions)
    # Usage tracking (populated from SkillUsageStore at load time)
    view_count: int = 0
    use_count: int = 0
    last_activity_at: float | None = None
    state: str = SkillState.ACTIVE
