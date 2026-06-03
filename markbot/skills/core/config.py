"""Skill configuration resolver.

Resolves config variables declared in skill frontmatter and injects them
into the skill content at runtime. Config values come from:
1. Environment variables (MARKBOT_SKILL_<KEY>)
2. Workspace config file (skills/.config.yaml)
3. Skill frontmatter defaults
4. Interactive prompts (if no default and no env var)
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from markbot.types.skill import SkillConfigVar, SkillDefinition

CONFIG_ENV_PREFIX = "MARKBOT_SKILL_"
CONFIG_VAR_RE = re.compile(r'\{\{\s*config\.(\w[\w.-]*)\s*\}\}')


class SkillConfigResolver:
    """Resolves and injects config variables into skill content."""

    def __init__(self, workspace: Path):
        self._workspace = workspace
        self._config_file = workspace / "skills" / ".config.yaml"
        self._file_config: dict[str, str] | None = None

    def resolve(self, skill: SkillDefinition) -> dict[str, str]:
        """Resolve all config vars for a skill, returning key->value mapping."""
        if not skill.config_vars:
            return {}

        file_config = self._load_file_config()
        resolved: dict[str, str] = {}

        for var in skill.config_vars:
            value = self._resolve_var(var, skill.name, file_config)
            if value is not None:
                resolved[var.key] = value
            elif var.default is not None:
                resolved[var.key] = var.default
            else:
                logger.debug(
                    "Skill '{}' config var '{}' has no value (no env, file config, or default)",
                    skill.name, var.key,
                )

        return resolved

    def inject(self, content: str, config: dict[str, str]) -> str:
        """Replace {{config.key}} placeholders in content with resolved values."""

        def replacer(match: re.Match) -> str:
            key = match.group(1)
            if key in config:
                return config[key]
            logger.debug("Unresolved config placeholder: {{config.{}}}", key)
            return match.group(0)

        return CONFIG_VAR_RE.sub(replacer, content)

    def _resolve_var(self, var: SkillConfigVar, skill_name: str, file_config: dict[str, str]) -> Optional[str]:
        """Resolve a single config var from env -> file config -> None."""
        env_key = f"{CONFIG_ENV_PREFIX}{var.key.upper().replace('.', '_').replace('-', '_')}"
        env_value = os.environ.get(env_key)
        if env_value is not None:
            return env_value

        file_value = file_config.get(var.key)
        if file_value is None:
            file_value = file_config.get(f"{skill_name}.{var.key}")
        if file_value is not None:
            return str(file_value)

        return None

    def _load_file_config(self) -> dict[str, str]:
        """Load config from workspace skills/.config.yaml."""
        if self._file_config is not None:
            return self._file_config

        if not self._config_file.exists():
            self._file_config = {}
            return self._file_config

        try:
            import yaml
            with open(self._config_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict):
                self._file_config = self._flatten_dict(data)
            else:
                self._file_config = {}
        except Exception as e:
            logger.warning("Failed to load skill config from {}: {}", self._config_file, e)
            self._file_config = {}

        return self._file_config

    @staticmethod
    def _flatten_dict(d: dict, parent_key: str = "", sep: str = ".") -> dict[str, str]:
        """Flatten a nested dict into dot-notation keys."""
        items: dict[str, str] = {}
        for k, v in d.items():
            new_key = f"{parent_key}{sep}{k}" if parent_key else k
            if isinstance(v, dict):
                items.update(SkillConfigResolver._flatten_dict(v, new_key, sep))
            else:
                items[new_key] = str(v)
        return items

    def save_config(self, skill_name: str, config: dict[str, str]) -> None:
        """Save resolved config values for a skill to the config file."""
        file_config = self._load_file_config()
        for key, value in config.items():
            file_config[f"{skill_name}.{key}"] = value

        self._write_file_config(file_config)

    def _write_file_config(self, config: dict[str, str]) -> None:
        """Write config dict back to the YAML file."""
        self._config_file.parent.mkdir(parents=True, exist_ok=True)

        nested: dict[str, Any] = {}
        for key, value in config.items():
            parts = key.split(".")
            current = nested
            for part in parts[:-1]:
                if part not in current:
                    current[part] = {}
                current = current[part]
            current[parts[-1]] = value

        try:
            import yaml
            with open(self._config_file, "w", encoding="utf-8") as f:
                yaml.safe_dump(nested, f, default_flow_style=False, allow_unicode=True)
            self._file_config = config
        except Exception as e:
            logger.warning("Failed to write skill config to {}: {}", self._config_file, e)

    def get_missing_vars(self, skill: SkillDefinition) -> list[SkillConfigVar]:
        """Return config vars that have no value (no env, no file config, no default)."""
        if not skill.config_vars:
            return []

        file_config = self._load_file_config()
        missing: list[SkillConfigVar] = []

        for var in skill.config_vars:
            value = self._resolve_var(var, skill.name, file_config)
            if value is None and var.default is None:
                missing.append(var)

        return missing
