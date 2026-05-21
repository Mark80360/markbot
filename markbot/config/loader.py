"""Configuration loading utilities."""

import json
from pathlib import Path

import pydantic

from markbot.config.schema import Config

# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None
_current_config: Config | None = None


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
