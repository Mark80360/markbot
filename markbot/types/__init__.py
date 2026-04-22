"""Shared type definitions for markbot."""

from markbot.types.permission import PermissionMode, PermissionDecision, ToolPermissionContext
from markbot.types.tool import ToolParameter, ToolDefinition, ToolContext
from markbot.types.skill import SkillScriptDef, SkillDefinition

__all__ = [
    "PermissionMode",
    "PermissionDecision",
    "ToolPermissionContext",
    "ToolParameter",
    "ToolDefinition",
    "ToolContext",
    "SkillScriptDef",
    "SkillDefinition",
]
