"""Configuration loading utilities."""

import json
import re
from pathlib import Path

from markbot.config.schema import Config


_current_config_path: Path | None = None


def set_config_path(path: Path) -> None:
    """Set the current config path (used to derive data directory)."""
    global _current_config_path
    _current_config_path = path


def get_config_path() -> Path:
    """Get the configuration file path."""
    if _current_config_path:
        return _current_config_path
    return Path.home() / ".markbot" / "config.json"


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    path = config_path or get_config_path()

    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data = _migrate_config(data)
            data = convert_keys(data)
            return Config.model_validate(data)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Warning: Failed to load config from {path}: {e}")
            print("Using default configuration.")

    return Config()


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump()
    data = convert_to_snake_case(data)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")
    return data


def convert_keys(data: dict) -> dict:
    """Convert camelCase keys to snake_case."""
    if not isinstance(data, dict):
        return data

    result = {}
    for key, value in data.items():
        snake_key = _camel_to_snake(key)
        if isinstance(value, dict):
            result[snake_key] = convert_keys(value)
        elif isinstance(value, list):
            result[snake_key] = [
                convert_keys(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            result[snake_key] = value
    return result


def convert_to_snake_case(data: dict) -> dict:
    """Convert camelCase keys to snake_case (alias for convert_keys)."""
    return convert_keys(data)


def _camel_to_snake(name: str) -> str:
    """Convert camelCase string to snake_case."""
    s1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", s1).lower()
