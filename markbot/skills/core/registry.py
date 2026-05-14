"""Skill registry for markbot.

Integrates skill loading, config resolution, conditional activation,
security scanning, and tool registration.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from loguru import logger

from markbot.skills.core.config import SkillConfigResolver
from markbot.skills.core.helpers import build_constraint_block, load_skill_body
from markbot.skills.core.loader import SkillLoader
from markbot.skills.core.scanner import Finding, ScanResult, SecurityScanner, should_allow
from markbot.skills.core.tool import SkillTool
from markbot.types.skill import SkillDefinition

if TYPE_CHECKING:
    from markbot.tools.registry import ToolRegistry


class SkillRegistry:
    """
    Skill registry that integrates with tool system.

    Skills can register their scripts as tools.
    Supports config resolution, conditional activation, and security scanning.
    """

    def __init__(
        self,
        workspace: Path,
        tool_registry: Optional["ToolRegistry"] = None,
        available_tool_names: Optional[list[str]] = None,
    ):
        self.workspace = workspace
        self.tool_registry = tool_registry
        self._available_tool_names = set(available_tool_names or [])
        self._loader = SkillLoader(workspace)
        self._config_resolver = SkillConfigResolver(workspace)
        self._skills: dict[str, SkillDefinition] = {}
        self._config_cache: dict[str, dict[str, str]] = {}

    def load_all(self) -> None:
        """Load all skills from workspace and builtin."""
        skills = self._loader.load_all()

        for skill in skills:
            if not self._loader.check_requirements(skill):
                logger.debug("Skill '{}' requirements not met, skipping", skill.name)
                continue

            trust_level = "builtin" if skill.is_builtin else "workspace"
            skill_path = self._loader.get_skill_path(skill.name)
            if skill_path:
                scanner = SecurityScanner()
                scan_result = scanner.scan_skill_dir(skill_path, trust_level)
                allowed, reason = should_allow(scan_result, trust_level)
                if not allowed:
                    logger.warning("Skill '{}' blocked by security scan: {}", skill.name, reason)
                    continue
                if reason:
                    logger.info("Skill '{}' scan note: {}", skill.name, reason)

            self._skills[skill.name] = skill

            if skill.config_vars:
                config = self._config_resolver.resolve(skill)
                if config:
                    self._config_cache[skill.name] = config

        logger.info("Loaded {} skills", len(self._skills))

    def register_script_tools(self) -> None:
        """Register all skill scripts as tools into the associated tool_registry.

        Call this after ``load_all()`` and after the ToolBinder has set up
        the shared ToolRegistry.  This centralises skill-tool registration
        in one place rather than splitting it between here and ToolBinder.
        """
        if not self.tool_registry:
            return
        for skill in self._skills.values():
            for script in skill.scripts:
                tool = SkillTool(skill.name, script, self.workspace)
                self.tool_registry.register(tool)
                logger.debug("Registered skill tool: {}", tool.definition.name)

    def get(self, name: str) -> Optional[SkillDefinition]:
        """Get a skill by name."""
        return self._skills.get(name)

    def list_all(self) -> list[SkillDefinition]:
        """List all loaded skills."""
        return list(self._skills.values())

    def get_active_skills(self) -> list[SkillDefinition]:
        """Get always-active skills."""
        return [s for s in self._skills.values() if s.is_always_active]

    def get_conditional_skills(self, available_tools: Optional[set[str]] = None) -> dict[str, list[SkillDefinition]]:
        """Get skills filtered by conditional activation rules.

        Returns dict with keys:
          - 'active': skills whose conditions are met (or have no conditions)
          - 'suppressed': skills whose requires_tools are not satisfied
          - 'fallback': skills activated as fallback for missing tools
        """
        tools = available_tools or self._available_tool_names
        active: list[SkillDefinition] = []
        suppressed: list[SkillDefinition] = []
        fallback: list[SkillDefinition] = []

        for skill in self._skills.values():
            if skill.is_always_active:
                active.append(skill)
                continue

            conditions = skill.conditions

            if conditions.requires_tools:
                missing = [t for t in conditions.requires_tools if t not in tools]
                if missing:
                    logger.debug(
                        "Skill '{}' suppressed: missing required tools: {}",
                        skill.name, ", ".join(missing),
                    )
                    suppressed.append(skill)
                    continue

            if conditions.fallback_for_tools:
                missing_fallback = [t for t in conditions.fallback_for_tools if t not in tools]
                if missing_fallback:
                    logger.debug(
                        "Skill '{}' activated as fallback for missing tools: {}",
                        skill.name, ", ".join(missing_fallback),
                    )
                    fallback.append(skill)
                    continue

            active.append(skill)

        return {"active": active, "suppressed": suppressed, "fallback": fallback}

    def build_skills_summary(self) -> str:
        """Build markdown summary of available skills."""
        lines = []

        for skill in sorted(self._skills.values(), key=lambda s: s.name):
            tag = "📦 built-in" if skill.is_builtin else "📁 workspace"
            lines.append(f"### {skill.name} `[{tag}]`")
            lines.append(f"{skill.description}")
            lines.append(f"**When to use**: {skill.when_to_use}")

            if skill.scripts:
                lines.append("\n**Available scripts**:")
                for script in skill.scripts:
                    lines.append(f"- `{skill.name}.{script.name}`: {script.description}")
            else:
                skill_path = self._loader.get_skill_path(skill.name)
                if skill_path:
                    lines.append(f"\n> **Instruction-only skill** (no scripts). Read its guide: `{skill_path / 'SKILL.md'}`")

            if skill.config_vars:
                config = self._config_cache.get(skill.name, {})
                missing = self._config_resolver.get_missing_vars(skill)
                if missing:
                    lines.append("\n**Config required**:")
                    for var in missing:
                        default_note = f" (default: {var.default})" if var.default else ""
                        lines.append(f"- `{var.key}`: {var.description}{default_note}")
                elif config:
                    lines.append("\n**Config**:")
                    for var in skill.config_vars:
                        val = config.get(var.key, var.default or "not set")
                        lines.append(f"- `{var.key}`: {val}")

            if skill.conditions.requires_tools:
                lines.append(f"\n**Requires tools**: {', '.join(skill.conditions.requires_tools)}")
            if skill.conditions.fallback_for_tools:
                lines.append(f"\n**Fallback for**: {', '.join(skill.conditions.fallback_for_tools)}")

            lines.append("")

        return "\n".join(lines)

    def build_skills_index(self) -> str:
        """Build a compact one-line-per-skill index for system prompt injection."""
        lines = []
        for skill in sorted(self._skills.values(), key=lambda s: s.name):
            desc = skill.description[:80] + "..." if len(skill.description) > 80 else skill.description
            has_scripts = " [executable]" if skill.scripts else ""
            conditions = ""
            if skill.conditions.requires_tools:
                conditions += f" [requires: {','.join(skill.conditions.requires_tools)}]"
            if skill.conditions.fallback_for_tools:
                conditions += f" [fallback-for: {','.join(skill.conditions.fallback_for_tools)}]"
            lines.append(f"  - {skill.name}: {desc}{has_scripts}{conditions}")
        return "\n".join(lines)

    def load_skill_content(self, name: str) -> str | None:
        """Load skill content for context, with config injection."""
        skill_path = self._loader.get_skill_path(name)
        if not skill_path:
            return None
        content = load_skill_body(skill_path)
        if content and name in self._config_cache:
            content = self._config_resolver.inject(content, self._config_cache[name])
        return content

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """Load specific skills for inclusion in agent context."""
        parts = []

        for name in skill_names:
            content = self.load_skill_content(name)
            if content:
                parts.append(f"### Skill: {name}\n\n{content}")

        return "\n\n---\n\n".join(parts) if parts else ""

    def build_skill_constraint_block(self, skill_name: str) -> str:
        """Build a hard constraint block for a skill's full instructions."""
        body = self.load_skill_content(skill_name)
        if not body:
            return ""
        return build_constraint_block(skill_name, body)

    def get_always_active_content(self) -> str:
        """Get content of always-active skills, with config injection."""
        always_skills = self.get_active_skills()
        if not always_skills:
            return ""

        content = self.load_skills_for_context([s.name for s in always_skills])
        if content:
            return f"# Active Skills\n\n{content}"
        return ""

    def list_executable_scripts(self, skill_name: Optional[str] = None) -> list[dict[str, Any]]:
        """List all executable scripts across all skills."""
        scripts = []

        if skill_name:
            skill = self.get(skill_name)
            if skill:
                for script in skill.scripts:
                    scripts.append({
                        "name": script.name,
                        "skill_name": skill_name,
                        "description": script.description,
                        "entry": script.entry,
                        "language": script.language,
                    })
        else:
            for skill in self._skills.values():
                for script in skill.scripts:
                    scripts.append({
                        "name": script.name,
                        "skill_name": skill.name,
                        "description": script.description,
                        "entry": script.entry,
                        "language": script.language,
                    })

        return scripts

    def scan_script(self, skill_name: str, script_name: str) -> ScanResult | None:
        """Scan a script for security issues."""
        skill = self.get(skill_name)
        if not skill:
            return ScanResult(
                is_safe=False,
                findings=[Finding(
                    line=0,
                    pattern="",
                    severity="critical",
                    message=f"Skill '{skill_name}' not found",
                )],
            )

        script = None
        for s in skill.scripts:
            if s.name == script_name:
                script = s
                break

        if not script:
            return ScanResult(
                is_safe=False,
                findings=[Finding(
                    line=0,
                    pattern="",
                    severity="critical",
                    message=f"Script '{script_name}' not found in skill '{skill_name}'",
                )],
            )

        skill_path = self._loader.get_skill_path(skill_name)
        if not skill_path:
            return ScanResult(
                is_safe=False,
                findings=[Finding(
                    line=0,
                    pattern="",
                    severity="critical",
                    message=f"Skill path not found: {skill_name}",
                )],
            )

        entry_path = skill_path / script.entry
        scanner = SecurityScanner()
        return scanner.scan(entry_path, script.language)

    def set_available_tools(self, tool_names: list[str]) -> None:
        """Update the set of available tool names for conditional activation."""
        self._available_tool_names = set(tool_names)
