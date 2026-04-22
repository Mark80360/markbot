"""Skill system types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from markbot.types.tool import ToolParameter


@dataclass
class SkillScriptDef:
    name: str
    description: str
    entry: str
    language: Literal["python", "bash", "javascript"]
    parameters: list[ToolParameter]
    sandbox_config: Optional[dict[str, Any]] = None


@dataclass
class SkillDefinition:
    name: str
    description: str
    when_to_use: str
    scripts: list[SkillScriptDef] = field(default_factory=list)
    is_builtin: bool = False
    is_always_active: bool = False
