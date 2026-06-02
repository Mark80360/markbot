"""Public runtime-config and provider-factory helpers.

Extracted from ``markbot.cli.commands``. These are the public API
surface for code that needs to load MarkBot config and instantiate a
provider outside of the CLI (e.g. autopilot, web server).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from markbot.cli.ui import console
from markbot.config.schema import Config

__all__ = [
    "load_runtime_config",
    "make_provider",
    "merge_missing_defaults",
    "warn_deprecated_config_keys",
]


def merge_missing_defaults(existing: Any, defaults: Any) -> Any:
    """Recursively fill in missing values from defaults without overwriting user config."""
    if not isinstance(existing, dict) or not isinstance(defaults, dict):
        return existing

    merged = dict(existing)
    for key, value in defaults.items():
        if key not in merged:
            merged[key] = value
        else:
            merged[key] = merge_missing_defaults(merged[key], value)
    return merged


def _onboard_plugins(config_path: Path) -> None:
    """Inject default config for all discovered channels (built-in + plugins)."""
    import json

    from markbot.channels.discovery import discover_all

    all_channels = discover_all()
    if not all_channels:
        return

    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)

    channels = data.setdefault("channels", {})
    for name, cls in all_channels.items():
        if name not in channels:
            channels[name] = cls.default_config()
        else:
            channels[name] = merge_missing_defaults(channels[name], cls.default_config())

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def make_provider(config: Config):
    """
    Create fallback manager from config (V2).

    Returns:
        FallbackManager instance (replaces old single LLMProvider)
    """
    from markbot.providers import FallbackManager

    if not config.agents.defaults.model_chain:
        console.print("[red]Error: No models configured in modelChain.[/red]")
        console.print("Run 'markbot onboard' to configure models.")
        raise typer.Exit(1)

    errors = config.validate_model_chain()
    if errors:
        console.print("[red]Configuration errors:[/red]")
        for error in errors:
            console.print(f"  • {error}")
        raise typer.Exit(1)

    return FallbackManager(config)


def load_runtime_config(config: str | None = None, workspace: str | None = None) -> Config:
    """Load config and optionally override the active workspace."""
    from markbot.config.loader import load_config, set_config_path

    config_path = None
    if config:
        config_path = Path(config).expanduser().resolve()
        if not config_path.exists():
            console.print(f"[red]Error: Config file not found: {config_path}[/red]")
            raise typer.Exit(1)
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")

    loaded = load_config(config_path)
    warn_deprecated_config_keys(config_path)
    if workspace:
        loaded.agents.defaults.workspace = workspace
    return loaded


def warn_deprecated_config_keys(config_path: Path | None) -> None:
    """Hint users to remove obsolete keys from their config file."""
    import json

    from markbot.config.loader import get_config_path

    path = config_path or get_config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    if "memoryWindow" in raw.get("agents", {}).get("defaults", {}):
        console.print(
            "[dim]Hint: `memoryWindow` in your config is no longer used "
            "and can be safely removed.[/dim]"
        )
