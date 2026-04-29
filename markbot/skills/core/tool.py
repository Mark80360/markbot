"""Tool wrapper for skill scripts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from loguru import logger

from markbot.tools.base import BaseTool
from markbot.skills.core.helpers import load_skill_body, build_constraint_block
from markbot.types.permission import PermissionDecision
from markbot.types.skill import SkillScriptDef
from markbot.types.tool import ToolContext, ToolDefinition, ToolParameter


class SkillTool(BaseTool):
    """
    Tool wrapper that executes a skill script.

    This integrates skills into the tool system.
    After successful execution, the full SKILL.md body is injected
    as mandatory constraints to prevent the agent from improvising.
    """

    def __init__(self, skill_name: str, script: SkillScriptDef, workspace: Path):
        self._skill_name = skill_name
        self._script = script
        self._workspace = workspace

    @property
    def definition(self) -> ToolDefinition:
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
        from markbot.skills.core.loader import BUILTIN_SKILLS_DIR
        builtin_path = BUILTIN_SKILLS_DIR / self._skill_name
        if builtin_path.exists():
            return builtin_path
        return skill_path

    async def execute(self, params: dict[str, Any], context: ToolContext) -> Any:
        """Execute the skill script via Sandbox, with constraint injection."""
        from markbot.skills.core.sandbox import Sandbox
        from markbot.skills.core.scanner import SecurityScanner

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

                skill_body = load_skill_body(skill_path)
                if skill_body:
                    constraint_block = build_constraint_block(self._skill_name, skill_body)
                    output += f"\n\n{constraint_block}"

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


class SkillViewTool(BaseTool):
    """Tool that loads a skill's full SKILL.md content on demand.

    Supports progressive disclosure: the system prompt only shows a compact
    skill index; the AI calls this tool to load the full instructions when
    a skill is relevant.
    """

    def __init__(self, registry: "SkillRegistry"):  # noqa: F821
        from markbot.skills.core.registry import SkillRegistry as _SR
        self._registry: _SR = registry

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="skill_view",
            description="Load a skill's full SKILL.md content (instructions, when_to_use, available scripts). Use when a skill matches the user's request to get complete execution guidance.",
            parameters=[
                ToolParameter(
                    name="name",
                    description="Skill name to load (e.g. 'github', 'tmux'). Use skills_list to see available skills.",
                    type="string",
                    required=True,
                ),
            ],
            is_read_only=True,
            is_destructive=False,
        )

    @property
    def is_enabled(self) -> bool:
        return True

    async def check_permission(self, params: dict[str, Any], context: ToolContext) -> PermissionDecision:
        return PermissionDecision(behavior="allow")

    async def execute(self, params: dict[str, Any], context: ToolContext) -> Any:
        name = params.get("name", "")
        if not name:
            return "Error: 'name' parameter is required."

        content = self._registry.load_skill_content(name)
        if content is None:
            available = ", ".join(s.name for s in self._registry.list_all())
            return f"Error: Skill '{name}' not found. Available skills: {available}"

        skill = self._registry.get(name)
        header = f"# Skill: {name}\n\n"
        if skill:
            header += f"**Description**: {skill.description}\n"
            header += f"**When to use**: {skill.when_to_use}\n"
            if skill.scripts:
                header += "**Scripts**:\n"
                for script in skill.scripts:
                    header += f"- {skill.name}.{script.name}: {script.description}\n"
            header += "\n---\n\n"

        return header + content


class SkillsListTool(BaseTool):
    """Tool that lists available skills with optional category filtering."""

    def __init__(self, registry: "SkillRegistry"):  # noqa: F821
        from markbot.skills.core.registry import SkillRegistry as _SR
        self._registry: _SR = registry

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="skills_list",
            description="List all available skills (name + description). Use before skill_view to discover relevant skills.",
            parameters=[],
            is_read_only=True,
            is_destructive=False,
        )

    @property
    def is_enabled(self) -> bool:
        return True

    async def check_permission(self, params: dict[str, Any], context: ToolContext) -> PermissionDecision:
        return PermissionDecision(behavior="allow")

    async def execute(self, params: dict[str, Any], context: ToolContext) -> Any:
        skills = self._registry.list_all()
        if not skills:
            return "No skills available."

        lines = [f"**{len(skills)} skills available:**\n"]
        for skill in sorted(skills, key=lambda s: s.name):
            tag = "📦 builtin" if skill.is_builtin else "📁 workspace"
            desc = skill.description[:100] + "..." if len(skill.description) > 100 else skill.description
            has_scripts = " (has scripts)" if skill.scripts else ""
            lines.append(f"- **{skill.name}** `[{tag}]`: {desc}{has_scripts}")

        lines.append("\nUse `skill_view(name)` to load a skill's full instructions.")
        return "\n".join(lines)
