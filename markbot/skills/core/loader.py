"""Skill loader for markbot.

Requires PyYAML (the fallback manual parser only handles flat key-value
pairs and simple lists; nested structures require PyYAML).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from loguru import logger

from markbot.types.skill import SkillConditions, SkillConfigVar, SkillDefinition, SkillScriptDef
from markbot.types.tool import ToolParameter

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




BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "builtin"

VALID_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]*$')
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_SKILL_CONTENT_CHARS = 100_000
ALLOWED_SUBDIRS = {"references", "templates", "scripts", "assets"}


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

        if self.workspace_skills.exists():
            for skill_dir in sorted(self.workspace_skills.iterdir()):
                if skill_dir.is_dir() and not skill_dir.name.startswith("."):
                    # Skip the archived directory — it holds skills that
                    # have been moved out of the active set by the curator.
                    if skill_dir.name == "archived":
                        continue
                    try:
                        skill = self._load_from_dir(skill_dir, is_builtin=False)
                        if skill:
                            skills.append(skill)
                            loaded_names.add(skill.name)
                    except Exception as e:
                        logger.warning("Failed to load skill from {}: {}", skill_dir, e)

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
                            logger.warning("Failed to load builtin skill from {}: {}", skill_dir, e)

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

        scripts = self._parse_scripts(meta, skill_file.parent)
        config_vars = self._parse_config_vars(meta)
        conditions = self._parse_conditions(meta)

        return SkillDefinition(
            name=meta.get("name", skill_file.parent.name),
            description=meta.get("description", ""),
            when_to_use=meta.get("when_to_use", ""),
            scripts=scripts,
            is_builtin=is_builtin,
            is_always_active=self._parse_markbot_metadata(meta).get("always", False),
            config_vars=config_vars,
            conditions=conditions,
        )

    def _parse_frontmatter(self, content: str) -> dict[str, Any] | None:
        """Parse YAML frontmatter from markdown content."""
        if not content.startswith("---"):
            return None

        end_match = re.search(r"\n---\s*\n", content[3:])
        if not end_match:
            return None

        yaml_content = content[3 : 3 + end_match.start()]

        result = _parse_yaml(yaml_content)
        if result is not None:
            return result

        return self._manual_parse_yaml(yaml_content)

    def _manual_parse_yaml(self, yaml_content: str) -> dict[str, Any]:
        """Manual YAML parser for simple cases (fallback)."""
        result = {}
        current_key = None
        current_list = None

        for line in yaml_content.split("\n"):
            stripped = line.rstrip()
            if not stripped:
                continue

            list_match = re.match(r"^\s*-\s+(.+)$", stripped)
            if list_match and current_key:
                if current_list is None:
                    current_list = []
                    result[current_key] = current_list
                current_list.append(list_match.group(1).strip('"\''))
                continue

            kv_match = re.match(r"^(\w+):\s*(.*)$", stripped)
            if kv_match:
                key, value = kv_match.groups()
                current_key = key
                current_list = None

                if stripped.rstrip().endswith(":") and not value.strip():
                    result[key] = {}
                    continue

                value = value.strip()
                if value:
                    if (value.startswith('"') and value.endswith('"')) or (
                        value.startswith("'") and value.endswith("'")
                    ):
                        value = value[1:-1]
                    result[key] = value
                else:
                    result[key] = None

        return result

    def _parse_config_vars(self, meta: dict[str, Any]) -> list[SkillConfigVar]:
        """Parse config variable declarations from skill frontmatter.

        Skills declare config settings via::

            markbot:
              config:
                - key: wiki.path
                  description: Path to the knowledge base directory
                  default: "~/wiki"
                  prompt: Wiki directory path
        """
        markbot_meta = meta.get("markbot", {})
        if not isinstance(markbot_meta, dict):
            return []

        raw = markbot_meta.get("config", [])
        if not raw:
            return []
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list):
            return []

        result: list[SkillConfigVar] = []
        seen: set[str] = set()

        for item in raw:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key", "")).strip()
            if not key or key in seen:
                continue
            desc = str(item.get("description", "")).strip()
            if not desc:
                continue
            prompt_text = str(item.get("prompt", "")).strip() or desc
            default = item.get("default")
            if default is not None:
                default = str(default)
            seen.add(key)
            result.append(SkillConfigVar(
                key=key,
                description=desc,
                default=default,
                prompt=prompt_text,
            ))

        return result

    def _parse_conditions(self, meta: dict[str, Any]) -> SkillConditions:
        """Parse conditional activation fields from skill frontmatter.

        Skills declare conditions via::

            markbot:
              requires_tools: [exec, web_search]
              fallback_for_tools: [web_search]
        """
        markbot_meta = meta.get("markbot", {})
        if not isinstance(markbot_meta, dict):
            return SkillConditions()

        requires = markbot_meta.get("requires_tools", [])
        fallback = markbot_meta.get("fallback_for_tools", [])

        if isinstance(requires, str):
            requires = [requires]
        if isinstance(fallback, str):
            fallback = [fallback]

        return SkillConditions(
            requires_tools=[str(t).strip() for t in (requires or []) if str(t).strip()],
            fallback_for_tools=[str(t).strip() for t in (fallback or []) if str(t).strip()],
        )

    def _parse_scripts(self, meta: dict[str, Any], skill_dir: Path) -> list[SkillScriptDef]:
        """Parse executable scripts from skill metadata."""
        scripts = []

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
        if not name or name == "archived":
            return None

        workspace_skill = self.workspace_skills / name
        if workspace_skill.exists():
            return workspace_skill

        builtin_skill = self.builtin_skills / name
        if builtin_skill.exists():
            return builtin_skill

        return None

    def check_requirements(self, skill: SkillDefinition) -> bool:
        """Check if skill requirements are met."""
        return True

    @staticmethod
    def validate_name(name: str) -> str | None:
        """Validate a skill name. Returns error message or None if valid."""
        if not name:
            return "Skill name is required."
        if len(name) > MAX_NAME_LENGTH:
            return f"Skill name exceeds {MAX_NAME_LENGTH} characters."
        if not VALID_NAME_RE.match(name):
            return (
                f"Invalid skill name '{name}'. Use lowercase letters, numbers, "
                f"hyphens, dots, and underscores. Must start with a letter or digit."
            )
        return None

    @staticmethod
    def validate_frontmatter(content: str) -> str | None:
        """Validate that SKILL.md content has proper frontmatter with required fields."""
        if not content.strip():
            return "Content cannot be empty."

        if not content.startswith("---"):
            return "SKILL.md must start with YAML frontmatter (---)."

        end_match = re.search(r'\n---\s*\n', content[3:])
        if not end_match:
            return "SKILL.md frontmatter is not closed. Ensure you have a closing '---' line."

        yaml_content = content[3:end_match.start() + 3]

        try:
            parsed = _parse_yaml(yaml_content)
        except Exception as e:
            return f"YAML frontmatter parse error: {e}"

        if not isinstance(parsed, dict):
            return "Frontmatter must be a YAML mapping (key: value pairs)."

        if "name" not in parsed:
            return "Frontmatter must include 'name' field."
        if "description" not in parsed:
            return "Frontmatter must include 'description' field."
        if len(str(parsed.get("description", ""))) > MAX_DESCRIPTION_LENGTH:
            return f"Description exceeds {MAX_DESCRIPTION_LENGTH} characters."

        body = content[end_match.end() + 3:].strip()
        if not body:
            return "SKILL.md must have content after the frontmatter (instructions, procedures, etc.)."

        return None

    @staticmethod
    def validate_content_size(content: str, label: str = "SKILL.md") -> str | None:
        """Check that content doesn't exceed the character limit."""
        if len(content) > MAX_SKILL_CONTENT_CHARS:
            return (
                f"{label} content is {len(content):,} characters "
                f"(limit: {MAX_SKILL_CONTENT_CHARS:,}). "
                f"Consider splitting into a smaller SKILL.md with supporting files."
            )
        return None

    @staticmethod
    def validate_file_path(file_path: str) -> str | None:
        """Validate a file path for write_file/remove_file."""
        if not file_path:
            return "file_path is required."

        if ".." in Path(file_path).parts:
            return "Path traversal ('..') is not allowed."

        normalized = Path(file_path)
        if not normalized.parts or normalized.parts[0] not in ALLOWED_SUBDIRS:
            allowed = ", ".join(sorted(ALLOWED_SUBDIRS))
            return f"File must be under one of: {allowed}. Got: '{file_path}'"

        if len(normalized.parts) < 2:
            return f"Provide a file path, not just a directory. Example: '{normalized.parts[0]}/myfile.md'"

        return None
