"""Skill registry for markbot.

Refactored to use new core types inspired by MarkBot.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from loguru import logger

from markbot.core.types import SkillDefinition
from markbot.core.skills.loader import SkillLoader
from markbot.core.skills.tool import SkillTool
from markbot.core.skills.utils import load_skill_body, build_constraint_block

if TYPE_CHECKING:
    from markbot.agent.tools.registry import ToolRegistry

# Import security scanner for CLI operations
try:
    from markbot.agent.skill_execution.scanner import SecurityScanner, ScanResult, Finding
except ImportError:
    SecurityScanner = None
    ScanResult = None
    Finding = None


class SkillRegistry:
    """
    Skill registry that integrates with tool system.

    Skills can register their scripts as tools.
    """

    def __init__(
        self,
        workspace: Path,
        tool_registry: Optional["ToolRegistry"] = None,
    ):
        self.workspace = workspace
        self.tool_registry = tool_registry
        self._loader = SkillLoader(workspace)
        self._skills: dict[str, SkillDefinition] = {}

    def load_all(self) -> None:
        """Load all skills from workspace and builtin."""
        skills = self._loader.load_all()

        for skill in skills:
            # Check requirements
            if not self._loader.check_requirements(skill):
                logger.debug(f"Skill '{skill.name}' requirements not met, skipping")
                continue

            self._skills[skill.name] = skill

            # Register scripts as tools
            if self.tool_registry:
                self._register_scripts(skill)

        logger.info(f"Loaded {len(self._skills)} skills")

    def _register_scripts(self, skill: SkillDefinition) -> None:
        """Register skill scripts as tools."""
        for script in skill.scripts:
            tool = SkillTool(skill.name, script, self.workspace)
            self.tool_registry.register(tool)
            logger.debug(f"Registered skill tool: {tool.definition.name}")

    def get(self, name: str) -> Optional[SkillDefinition]:
        """Get a skill by name."""
        return self._skills.get(name)

    def list_all(self) -> list[SkillDefinition]:
        """List all loaded skills."""
        return list(self._skills.values())

    def get_active_skills(self) -> list[SkillDefinition]:
        """Get always-active skills."""
        return [s for s in self._skills.values() if s.is_always_active]

    def build_skills_summary(self) -> str:
        """Build markdown summary of available skills."""
        lines = []

        for skill in sorted(self._skills.values(), key=lambda s: s.name):
            lines.append(f"### {skill.name}")
            lines.append(f"{skill.description}")
            lines.append(f"**When to use**: {skill.when_to_use}")

            if skill.scripts:
                lines.append("\n**Available scripts**:")
                for script in skill.scripts:
                    lines.append(f"- `{script.name}`: {script.description}")
            lines.append("")

        return "\n".join(lines)

    def load_skill_content(self, name: str) -> str | None:
        """Load skill content for context."""
        skill_path = self._loader.get_skill_path(name)
        if not skill_path:
            return None
        return load_skill_body(skill_path)

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """Load specific skills for inclusion in agent context."""
        parts = []

        for name in skill_names:
            content = self.load_skill_content(name)
            if content:
                parts.append(f"### Skill: {name}\n\n{content}")

        return "\n\n---\n\n".join(parts) if parts else ""

    def build_skill_constraint_block(self, skill_name: str) -> str:
        """Build a hard constraint block for a skill's full instructions.

        Wraps the SKILL.md body content with mandatory constraint framing
        so the LLM treats it as non-negotiable rules rather than suggestions.

        Args:
            skill_name: The name of the skill to build constraints for.

        Returns:
            Formatted constraint string, or empty string if skill not found.
        """
        body = self.load_skill_content(skill_name)
        if not body:
            return ""
        return build_constraint_block(skill_name, body)

    def get_always_active_content(self) -> str:
        """Get content of always-active skills."""
        always_skills = self.get_active_skills()
        if not always_skills:
            return ""

        content = self.load_skills_for_context([s.name for s in always_skills])
        if content:
            return f"# Active Skills\n\n{content}"
        return ""

    def list_executable_scripts(self, skill_name: Optional[str] = None) -> list[dict[str, Any]]:
        """List all executable scripts across all skills.

        Args:
            skill_name: If specified, only list scripts for this skill.

        Returns:
            List of script info dicts.
        """
        scripts = []

        if skill_name:
            # Scripts from specific skill
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
            # Scripts from all skills
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

    def scan_script(self, skill_name: str, script_name: str) -> "ScanResult | None":
        """Scan a script for security issues.

        Args:
            skill_name: Name of the skill.
            script_name: Name of the script.

        Returns:
            ScanResult with findings, or None if scanner not available.
        """
        if SecurityScanner is None or ScanResult is None or Finding is None:
            return None

        # Find the script
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

        # Get skill path and script entry
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
