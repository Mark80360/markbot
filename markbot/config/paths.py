"""Runtime path helpers derived from the active config context."""

from __future__ import annotations

from pathlib import Path

from markbot.config.loader import get_config, get_config_path
from markbot.utils.helpers import ensure_dir

_DEFAULT_WORKSPACE = "~/.markbot/workspace"


def get_data_dir() -> Path:
    """Return the instance-level runtime data directory."""
    return ensure_dir(get_config_path().parent)


def get_runtime_subdir(name: str) -> Path:
    """Return a named runtime subdirectory under the instance data dir."""
    return ensure_dir(get_data_dir() / name)


def get_media_dir(channel: str | None = None) -> Path:
    """Return the media directory under workspace, optionally namespaced per channel."""
    base = get_workspace_path() / "media"
    return ensure_dir(base / channel) if channel else ensure_dir(base)


def get_cron_dir(workspace: str | Path | None = None) -> Path:
    """Return the cron storage directory within a workspace."""
    return get_workspace_path(workspace) / "cron"


def get_logs_dir() -> Path:
    """Return the logs directory."""
    return get_runtime_subdir("logs")


def get_code_run_dir() -> Path:
    """Return the directory for ephemeral code execution scripts."""
    return get_runtime_subdir(".run")


def get_artifacts_dir() -> Path:
    """Return the directory for offloaded tool output artifacts."""
    return get_runtime_subdir(".artifacts")


def get_gateway_dir() -> Path:
    """Return the gateway runtime directory (pid, log)."""
    return get_runtime_subdir("gateway")


def get_workspace_path(workspace: str | None = None) -> Path:
    """Resolve and ensure the agent workspace path.

    When *workspace* is omitted the value is read from the active
    configuration (``agents.defaults.workspace``).  Falls back to the
    schema default ``~/.markbot/workspace`` when no config is loaded.
    """
    if workspace:
        return ensure_dir(Path(workspace).expanduser())
    try:
        return ensure_dir(get_config().workspace_path)
    except Exception:
        return ensure_dir(Path(_DEFAULT_WORKSPACE).expanduser())


def is_default_workspace(workspace: str | Path | None) -> bool:
    """Return whether a workspace resolves to markbot's default workspace path."""
    if workspace is not None:
        current = Path(workspace).expanduser()
    else:
        try:
            current = get_config().workspace_path
        except Exception:
            current = Path(_DEFAULT_WORKSPACE).expanduser()
    default = Path(_DEFAULT_WORKSPACE).expanduser()
    return current.resolve(strict=False) == default.resolve(strict=False)


def get_cli_history_path() -> Path:
    """Return the shared CLI history file path."""
    return get_data_dir() / "history" / "cli_history"


def get_bridge_install_dir() -> Path:
    """Return the bridge installation directory."""
    return get_runtime_subdir("bridge")


def get_legacy_sessions_dir() -> Path:
    """Return the legacy global session directory used for migration fallback."""
    return get_runtime_subdir("sessions")
