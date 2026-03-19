"""Runtime path helpers derived from the active config context."""

from __future__ import annotations

from pathlib import Path

from markbot.config.loader import get_config_path, load_config
from markbot.utils.helpers import ensure_dir


def _get_config_workspace() -> Path:
    """Get workspace from config file, fallback to default."""
    try:
        config = load_config(get_config_path())
        return Path(config.agents.defaults.workspace).expanduser()
    except Exception:
        return Path.home() / ".markbot" / "workspace"


def _get_workspace_subdir(name: str) -> Path:
    """Return a named subdirectory under the workspace dir."""
    return ensure_dir(_get_config_workspace() / name)


def get_data_dir() -> Path:
    """Return the instance-level runtime data directory."""
    return ensure_dir(get_config_path().parent)


def get_runtime_subdir(name: str) -> Path:
    """Return a named runtime subdirectory under the instance data dir."""
    return ensure_dir(get_data_dir() / name)


def get_media_dir(channel: str | None = None) -> Path:
    """Return the media directory, optionally namespaced per channel."""
    base = _get_workspace_subdir("media")
    return ensure_dir(base / channel) if channel else base


def get_cron_dir() -> Path:
    """Return the cron storage directory."""
    return _get_workspace_subdir("cron")


def get_logs_dir() -> Path:
    """Return the logs directory."""
    return _get_workspace_subdir("logs")


def get_workspace_path(workspace: str | None = None) -> Path:
    """Resolve and ensure the agent workspace path."""
    if workspace:
        path = Path(workspace).expanduser()
    else:
        path = _get_config_workspace()
    return ensure_dir(path)


def get_cli_history_path() -> Path:
    """Return the shared CLI history file path."""
    return Path.home() / ".markbot" / "history" / "cli_history"


def get_legacy_sessions_dir() -> Path:
    """Return the legacy global session directory used for migration fallback."""
    return Path.home() / ".markbot" / "sessions"
