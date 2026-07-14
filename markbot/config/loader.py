"""Configuration loading utilities."""

import json
from pathlib import Path
from typing import Any

import pydantic
from loguru import logger

from markbot.config.schema import Config

# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None
_current_config: Config | None = None
_last_validation_warnings: list[str] = []


class ConfigValidationError(Exception):
    """Raised when configuration fails validation."""

    def __init__(self, message: str, details: list[str] | None = None):
        super().__init__(message)
        self.details = details or []


def set_config_path(path: Path) -> None:
    """Set the current config path (used to derive data directory)."""
    global _current_config_path, _current_config
    _current_config_path = path
    _current_config = None


def get_config_path() -> Path:
    """Get the configuration file path."""
    if _current_config_path:
        return _current_config_path
    return Path.home() / ".markbot" / "config.json"


def get_config() -> Config:
    """Get the current cached config, loading from default path if needed."""
    global _current_config
    if _current_config is None:
        _current_config = load_config()
    return _current_config


def get_validation_warnings() -> list[str]:
    """Return warnings from the most recent config validation.

    Empty if config has not been loaded yet or if there were no warnings.
    """
    return list(_last_validation_warnings)


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file with validation.

    Raises:
        ConfigValidationError: If configuration is invalid

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    path = config_path or get_config_path()
    global _current_config

    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)

            config = Config.model_validate(data)

            errors = config.validate_model_chain()
            warnings: list[str] = []

            from markbot.config.validator import Severity
            from markbot.config.validator import validate_config as _validate_config
            vr = _validate_config(config)
            for issue in vr.issues:
                if issue.severity == Severity.ERROR:
                    errors.append(f"{issue.field}: {issue.message}")
                elif issue.severity == Severity.WARNING:
                    warnings.append(f"{issue.field}: {issue.message}")

            global _last_validation_warnings
            _last_validation_warnings = warnings

            if errors:
                raise ConfigValidationError(
                    "Configuration validation failed",
                    details=errors
                )

            _current_config = config

            return config

        except json.JSONDecodeError as e:
            raise ConfigValidationError(f"Invalid JSON in {path}: {e}")
        except pydantic.ValidationError as e:
            raise ConfigValidationError(f"Schema validation failed: {e}")

    config = Config()
    _current_config = config
    return config


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump(mode="json", by_alias=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def update_config_value(
    key_path: list[str],
    value: Any,
    config_path: Path | None = None,
) -> bool:
    """Patch a single nested field in ``config.json`` in place.

    Unlike :func:`save_config`, this preserves the existing file structure
    (key order, neighboring fields) by reading the raw JSON dict, walking
    ``key_path`` to the target field, and writing the dict back. Missing
    intermediate dicts are created.

    Used by slash commands (e.g. ``/mode``) to persist runtime changes so
    they survive restarts without forcing the user to edit config.json.

    Args:
        key_path: Nested keys, e.g. ``["agents", "defaults", "default_permission_mode"]``.
        value: JSON-serializable value to set.
        config_path: Optional target path. Defaults to :func:`get_config_path`.

    Returns:
        ``True`` on success, ``False`` on failure (logged at WARNING). The
        caller's primary effect (e.g. in-memory mode switch) should still
        succeed regardless of the return value — persistence is best-effort.
    """
    if not key_path:
        logger.warning("update_config_value called with empty key_path")
        return False

    path = config_path or get_config_path()
    try:
        if path.exists():
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                logger.warning("Config file {} is not a JSON object", path)
                return False
        else:
            data = {}

        node = data
        for k in key_path[:-1]:
            existing = node.get(k)
            if not isinstance(existing, dict):
                node[k] = {}
            node = node[k]
        node[key_path[-1]] = value

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        # Refresh cached config so get_config() reflects the change.
        global _current_config
        if _current_config is not None:
            try:
                _current_config = Config.model_validate(data)
            except Exception as exc:
                logger.debug("Failed to refresh cached config after update: {}", exc)

        return True
    except Exception as exc:
        logger.warning("Failed to persist config value at {}: {}", key_path, exc)
        return False
