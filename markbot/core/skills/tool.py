"""Tool wrapper for skill scripts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from loguru import logger

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
            is_read_only=False,
            is_destructive=True,
        )

    @property
    def is_enabled(self) -> bool:
        return True

    async def check_permission(
        self, params: dict[str, Any], context: ToolContext
    ) -> PermissionDecision:
        if context.is_non_interactive:
            return PermissionDecision(behavior="allow", reason="Non-interactive mode")
        return PermissionDecision(behavior="ask")

    def _resolve_skill_path(self) -> Path:
        skill_path = self._workspace / "skills" / self._skill_name
        if skill_path.exists():
            return skill_path
        from markbot.core.skills.loader import BUILTIN_SKILLS_DIR
        builtin_path = BUILTIN_SKILLS_DIR / self._skill_name
        if builtin_path.exists():
            return builtin_path
        return skill_path

    async def execute(self, params: dict[str, Any], context: ToolContext) -> Any:
        """Execute the skill script via Sandbox."""
        from markbot.agent.skill_execution.sandbox import Sandbox
        from markbot.agent.skill_execution.scanner import SecurityScanner

        skill_path = self._resolve_skill_path()
        entry_path = skill_path / self._script.entry
        if not entry_path.exists():
            return f"Error: Script entry not found: {entry_path}"

        scanner = SecurityScanner()
        scan_result = scanner.scan(entry_path, self._script.language)
        if not scan_result.is_safe:
            findings_str = "\n".join(
                f"  Line {f.line}: [{f.severity}] {f.message}"
                for f in scan_result.findings
                if f.severity in ("high", "critical")
            )
            return f"Error: Script failed security check\n{findings_str}"

        sandbox_config = self._script.sandbox_config or {}
        sandbox_config.setdefault("environment", {})
        sandbox_config["environment"]["SKILL_NAME"] = self._skill_name
        sandbox_config["environment"]["SCRIPT_NAME"] = self._script.name
        sandbox_config["environment"]["WORKSPACE_ROOT"] = str(Path(context.workspace).resolve())

        sandbox = Sandbox(sandbox_config)

        if sandbox.config.allowed_paths:
            allowed, msg = sandbox.validate_script_access(entry_path)
            if not allowed:
                return f"Error: {msg}"

        try:
            logger.info("Executing skill script: {}.{}", self._skill_name, self._script.name)
            result = await sandbox.run(
                script=entry_path.resolve(),
                language=self._script.language,
                args=params,
                cwd=skill_path,
            )
            if result.success:
                output = result.stdout
                if result.stderr:
                    output += f"\n\n[stderr]:\n{result.stderr}"
                return output
            else:
                error_msg = f"Script execution failed (exit code: {result.exit_code})"
                if result.stderr:
                    error_msg += f"\n{result.stderr}"
                return f"Error: {error_msg}"
        except Exception as e:
            logger.exception("Error executing skill script {}.{}", self._skill_name, self._script.name)
            return f"Error: Failed to execute script: {e}"

    def get_activity_description(self, params: dict[str, Any]) -> Optional[str]:
        return f"Running {self._skill_name}.{self._script.name}"
