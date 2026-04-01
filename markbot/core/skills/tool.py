"""Tool wrapper for skill scripts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from markbot.agent.tools.base import BaseTool
from markbot.core.types import (
    PermissionDecision,
    SkillScriptDef,
    ToolContext,
    ToolDefinition,
    ToolParameter,
)


class SkillTool(BaseTool):
    """
    Tool wrapper that executes a skill script.

    This integrates skills into the tool system.
    """

    def __init__(self, skill_name: str, script: SkillScriptDef, workspace: Path):
        self._skill_name = skill_name
        self._script = script
        self._workspace = workspace

    @property
    def definition(self) -> ToolDefinition:
        """Get tool definition from script metadata."""
        return ToolDefinition(
            name=f"{self._skill_name}.{self._script.name}",
            description=self._script.description,
            parameters=self._script.parameters,
            is_read_only=False,  # Skills can do anything
            is_destructive=True,  # Assume destructive for safety
        )

    @property
    def is_enabled(self) -> bool:
        return True

    async def check_permission(
        self, params: dict[str, Any], context: ToolContext
    ) -> PermissionDecision:
        """Skills require explicit permission."""
        # Skills run in sandbox but still need permission
        return PermissionDecision(behavior="ask")

    async def execute(self, params: dict[str, Any], context: ToolContext) -> Any:
        """Execute the skill script."""
        # Import here to avoid circular dependency
        from markbot.agent.skill_execution import SkillScriptExecutor

        executor = SkillScriptExecutor()

        # Resolve entry path
        skill_path = self._workspace / "skills" / self._skill_name
        if not skill_path.exists():
            # Try builtin
            import markbot

            skill_path = Path(markbot.__file__).parent / "skills" / self._skill_name

        entry_path = skill_path / self._script.entry
        if not entry_path.exists():
            return f"Error: Script entry not found: {entry_path}"

        # Execute
        result = await executor.execute(
            script_path=entry_path,
            language=self._script.language,
            parameters=params,
            sandbox_config=self._script.sandbox_config,
            workspace=context.workspace,
        )

        return result

    def get_activity_description(self, params: dict[str, Any]) -> Optional[str]:
        return f"Running {self._skill_name}.{self._script.name}"
