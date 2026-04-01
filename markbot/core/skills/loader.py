"""Skill loader for markbot.

Refactored to use new core types inspired by MarkBot.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Optional

from markbot.core.types import SkillDefinition, SkillScriptDef, ToolParameter

# Import PyYAML for proper YAML parsing
try:
    import yaml

    def _parse_yaml(yaml_content: str) -> dict | None:
        try:
            return yaml.safe_load(yaml_content)
        except Exception:
            return None
except ImportError:

    def _parse_yaml(yaml_content: str) -> dict | None:
        return None


logger = logging.getLogger(__name__)

# Default builtin skills directory - points to the skills/ folder with actual skill definitions
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent.parent / "skills"


class SkillLoader:
    """Loader for agent skills."""

    def __init__(
        self,
        workspace: Path,
        builtin_skills_dir: Path | None = None,
    ):
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR

    def load_all(self) -> list[SkillDefinition]:
        """Load all skills from workspace and builtin directories."""
        skills = []
        loaded_names = set()

        # Load workspace skills first (can override builtin)
        if self.workspace_skills.exists():
            for skill_dir in sorted(self.workspace_skills.iterdir()):
                if skill_dir.is_dir() and not skill_dir.name.startswith("."):
                    try:
                        skill = self._load_from_dir(skill_dir, is_builtin=False)
                        if skill:
                            skills.append(skill)
                            loaded_names.add(skill.name)
                    except Exception as e:
                        logger.warning(f"Failed to load skill from {skill_dir}: {e}")

        # Load builtin skills
        if self.builtin_skills.exists():
            for skill_dir in sorted(self.builtin_skills.iterdir()):
                if skill_dir.is_dir() and not skill_dir.name.startswith("."):
                    if skill_dir.name not in loaded_names:
                        try:
                            skill = self._load_from_dir(skill_dir, is_builtin=True)
                            if skill:
                                skills.append(skill)
                                loaded_names.add(skill.name)
                        except Exception as e:
                            logger.warning(f"Failed to load builtin skill from {skill_dir}: {e}")

        return skills

    def _load_from_dir(self, skill_dir: Path, is_builtin: bool = False) -> SkillDefinition | None:
        """Load a skill from a directory."""
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            return None

        return self.load(skill_file, is_builtin=is_builtin)

    def load(self, skill_file: Path, is_builtin: bool = False) -> SkillDefinition:
        """Load a skill from a SKILL.md file."""
        content = skill_file.read_text(encoding="utf-8")
        meta = self._parse_frontmatter(content)

        if not meta:
            raise ValueError(f"No frontmatter found in {skill_file}")

        # Parse scripts
        scripts = self._parse_scripts(meta, skill_file.parent)

        return SkillDefinition(
            name=meta.get("name", skill_file.parent.name),
            description=meta.get("description", ""),
            when_to_use=meta.get("when_to_use", ""),
            scripts=scripts,
            is_builtin=is_builtin,
            is_always_active=self._parse_markbot_metadata(meta).get("always", False),
        )

    def _parse_frontmatter(self, content: str) -> dict[str, Any] | None:
        """Parse YAML frontmatter from markdown content."""
        if not content.startswith("---"):
            return None

        # Find the end of frontmatter
        end_match = re.search(r"\n---\s*\n", content[3:])
        if not end_match:
            return None

        yaml_content = content[3 : 3 + end_match.start()]

        # Try PyYAML first
        result = _parse_yaml(yaml_content)
        if result is not None:
            return result

        # Fallback to manual parsing
        return self._manual_parse_yaml(yaml_content)

    def _manual_parse_yaml(self, yaml_content: str) -> dict[str, Any]:
        """Manual YAML parser for simple cases (fallback)."""
        result = {}
        current_key = None
        current_list = None
        current_dict = None
        dict_key = None

        for line in yaml_content.split("\n"):
            stripped = line.rstrip()
            if not stripped:
                continue

            # Check for list item
            list_match = re.match(r"^\s*-\s+(.+)$", stripped)
            if list_match and current_key:
                if current_list is None:
                    current_list = []
                    result[current_key] = current_list
                current_list.append(list_match.group(1).strip('"\''))
                continue

            # Check for key-value pair
            kv_match = re.match(r"^(\w+):\s*(.*)$", stripped)
            if kv_match:
                key, value = kv_match.groups()
                current_key = key
                current_list = None
                current_dict = None
                dict_key = None

                # Check if this starts a nested structure
                if stripped.rstrip().endswith(":") and not value.strip():
                    result[key] = {}
                    current_dict = result[key]
                    continue

                value = value.strip()
                if value:
                    # Remove quotes
                    if (value.startswith('"') and value.endswith('"')) or (
                        value.startswith("'") and value.endswith("'")
                    ):
                        value = value[1:-1]
                    result[key] = value
                else:
                    result[key] = None

        return result

    def _parse_scripts(self, meta: dict[str, Any], skill_dir: Path) -> list[SkillScriptDef]:
        """Parse executable scripts from skill metadata."""
        scripts = []

        # Get scripts section
        scripts_meta = meta.get("scripts", {})
        if isinstance(scripts_meta, dict):
            for name, script_def in scripts_meta.items():
                if isinstance(script_def, dict):
                    script = self._parse_script(name, script_def, skill_dir)
                    if script:
                        scripts.append(script)

        return scripts

    def _parse_script(
        self, name: str, script_def: dict[str, Any], skill_dir: Path
    ) -> SkillScriptDef | None:
        """Parse a single script definition."""
        entry = script_def.get("entry")
        if not entry:
            return None

        # Parse parameters
        params = []
        params_meta = script_def.get("parameters", {})
        if isinstance(params_meta, dict):
            for param_name, param_def in params_meta.items():
                if isinstance(param_def, dict):
                    param = ToolParameter(
                        name=param_name,
                        description=param_def.get("description", ""),
                        type=param_def.get("type", "string"),
                        required=param_def.get("required", False),
                        default=param_def.get("default"),
                        enum=param_def.get("enum"),
                    )
                    params.append(param)

        # Parse sandbox config
        sandbox_config = script_def.get("sandbox", {})

        return SkillScriptDef(
            name=name,
            description=script_def.get("description", ""),
            entry=entry,
            language=script_def.get("language", "python"),
            parameters=params,
            sandbox_config=sandbox_config,
        )

    def _parse_markbot_metadata(self, meta: dict[str, Any]) -> dict[str, Any]:
        """Parse markbot-specific metadata from skill frontmatter."""
        markbot_meta = meta.get("markbot", {})
        if isinstance(markbot_meta, dict):
            return markbot_meta
        return {}

    def get_skill_path(self, name: str) -> Path | None:
        """Get the path to a skill directory by name."""
        # Check workspace first
        workspace_skill = self.workspace_skills / name
        if workspace_skill.exists():
            return workspace_skill

        # Check builtin
        builtin_skill = self.builtin_skills / name
        if builtin_skill.exists():
            return builtin_skill

        return None

    def check_requirements(self, skill: SkillDefinition) -> bool:
        """Check if skill requirements are met."""
        # This would check for required binaries, env vars, etc.
        # For now, assume all requirements are met
        return True
