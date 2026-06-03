"""``markbot config`` group: read/write configuration values."""
from __future__ import annotations

from pathlib import Path

import typer

from markbot.cli.ui import console, markbot_banner
from markbot.config.schema import Config

app = typer.Typer(
    name="config",
    help="Manage MarkBot configuration (get/set values).",
)


def _get_nested_value(data: dict, path: str):
    """Get a nested value from a dict using dot notation path."""
    keys = path.split(".")
    current = data
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return None
    return current


def _set_nested_value(data: dict, path: str, value) -> bool:
    """Set a nested value in a dict using dot notation path. Returns True if successful."""
    keys = path.split(".")
    current = data
    for key in keys[:-1]:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return False
    if isinstance(current, dict) and keys[-1] in current:
        current[keys[-1]] = value
        return True
    return False


def _parse_value(value_str: str):
    """Parse a string value into appropriate Python type."""
    if value_str.lower() == "true":
        return True
    if value_str.lower() == "false":
        return False
    if value_str.lower() == "null" or value_str.lower() == "none":
        return None
    try:
        if "." in value_str:
            return float(value_str)
        return int(value_str)
    except ValueError:
        pass
    return value_str


@app.command("get")
def get(
    key: str = typer.Argument(..., help="Configuration key (dot notation, e.g., agents.defaults.modelChain)"),
    raw: bool = typer.Option(False, "--raw", "-r", help="Output raw value without formatting"),
):
    """Get a configuration value."""
    from rich.text import Text

    from markbot.config.loader import load_config

    config = load_config()
    data = config.model_dump(by_alias=True)
    value = _get_nested_value(data, key)

    if value is None:
        markbot_banner()
        console.print()
        console.print(f"  [yellow]○[/yellow] Key '{key}' not found or is null")
        console.print()
        raise typer.Exit(1)

    if raw:
        if isinstance(value, (dict, list)):
            import json
            print(json.dumps(value, indent=2, ensure_ascii=False))
        else:
            print(value)
    else:
        markbot_banner()

        W = 72  # total width

        def section(title: str, color: str = "cyan") -> None:
            title_text = f"  {title}  "
            pad = W - len(title_text) - 2
            line = Text.from_markup(f"[{color}]{title_text}[/][dim]{'─' * pad}[/]")
            console.print(line)

        def kv(k: str, v: str, key_w: int = 14) -> None:
            line = Text.from_markup(f"  [cyan]{k:<{key_w}}[/cyan] {v}")
            console.print(line)

        def divider() -> None:
            line = Text.from_markup(f"[dim]{'─' * (W - 2)}[/]")
            console.print(line)

        console.print()

        # ─ Config Value ──────────────────────────────────────────────────────────
        section("Configuration", "cyan")
        kv("Key", key)

        if isinstance(value, (dict, list)):
            import json
            value_str = json.dumps(value, indent=2, ensure_ascii=False)
            kv("Value", value_str)
        else:
            kv("Value", str(value))

        divider()
        console.print()


@app.command("set")
def set_(  # noqa: A001  (shadows builtin, intentional for CLI sub-command)
    key: str = typer.Argument(..., help="Configuration key (dot notation, e.g., agents.defaults.modelChain)"),
    value: str = typer.Argument(..., help="Value to set (auto-detected type: string, number, boolean, null)"),
):
    """Set a configuration value."""
    import json

    from rich.text import Text

    from markbot.config.loader import load_config, save_config

    config = load_config()
    data = config.model_dump(by_alias=True)

    parsed_value = _parse_value(value)

    if _get_nested_value(data, key) is None:
        markbot_banner()
        console.print()
        console.print(f"  [yellow]⚠[/yellow] Warning: Key '{key}' does not exist in config schema")
        console.print()

    if not _set_nested_value(data, key, parsed_value):
        markbot_banner()
        console.print()
        console.print(f"  [red]✗[/red] Error: Failed to set '{key}'. Check if the path is valid.")
        console.print()
        raise typer.Exit(1)

    try:
        new_config = Config.model_validate(data)
    except Exception as e:
        markbot_banner()
        console.print()
        console.print(f"  [red]✗[/red] Error: Invalid value for '{key}': {e}")
        console.print()
        raise typer.Exit(1)

    save_config(new_config)

    markbot_banner()

    W = 72  # total width

    def section(title: str, color: str = "cyan") -> None:
        title_text = f"  {title}  "
        pad = W - len(title_text) - 2
        line = Text.from_markup(f"[{color}]{title_text}[/][dim]{'─' * pad}[/]")
        console.print(line)

    def kv(k: str, v: str, key_w: int = 14) -> None:
        line = Text.from_markup(f"  [cyan]{k:<{key_w}}[/cyan] {v}")
        console.print(line)

    def divider() -> None:
        line = Text.from_markup(f"[dim]{'─' * (W - 2)}[/]")
        console.print(line)

    console.print()

    # ─ Config Set ────────────────────────────────────────────────────────────
    section("Configuration", "green")
    kv("Key", key)

    if isinstance(parsed_value, (dict, list)):
        kv("Value", json.dumps(parsed_value, ensure_ascii=False))
    else:
        kv("Value", str(parsed_value))

    divider()
    console.print()


@app.command("list")
def list_(  # noqa: A001  (shadows builtin, intentional for CLI sub-command)
    prefix: str = typer.Option("", "--prefix", "-p", help="Filter keys by prefix"),
):
    """List all configuration keys and values."""
    import json

    from rich.text import Text

    from markbot.config.loader import load_config

    config = load_config()
    data = config.model_dump(by_alias=True)

    def flatten_dict(d: dict, prefix_str: str = "") -> list[tuple[str, any]]:
        items = []
        for k, v in d.items():
            key = f"{prefix_str}.{k}" if prefix_str else k
            if isinstance(v, dict):
                items.extend(flatten_dict(v, key))
            else:
                items.append((key, v))
        return items

    items = flatten_dict(data)
    if prefix:
        items = [(k, v) for k, v in items if k.startswith(prefix)]

    if not items:
        markbot_banner()
        console.print()
        console.print(f"  [yellow]○[/yellow] No keys found with prefix '{prefix}'")
        console.print()
        return

    markbot_banner()

    W = 72  # total width

    def section(title: str, color: str = "cyan") -> None:
        title_text = f"  {title}  "
        pad = W - len(title_text) - 2
        line = Text.from_markup(f"[{color}]{title_text}[/][dim]{'─' * pad}[/]")
        console.print(line)

    def kv(k: str, v: str, key_w: int = 20) -> None:
        line = Text.from_markup(f"  [cyan]{k:<{key_w}}[/cyan] {v}")
        console.print(line)

    def divider() -> None:
        line = Text.from_markup(f"[dim]{'─' * (W - 2)}[/]")
        console.print(line)

    console.print()

    # ─ Configuration ─────────────────────────────────────────────────────────
    section("Configuration", "cyan")

    for key, value in sorted(items):
        if isinstance(value, (dict, list)):
            value_str = json.dumps(value, ensure_ascii=False)
            if len(value_str) > 45:
                value_str = value_str[:42] + "..."
        else:
            value_str = str(value)
            if len(value_str) > 45:
                value_str = value_str[:42] + "..."
        kv(key, value_str)

    divider()

    # ─ Summary ───────────────────────────────────────────────────────────────
    section("Summary", "blue")
    kv("Total Keys", str(len(items)))
    if prefix:
        kv("Filter", f"prefix='{prefix}'")

    divider()
    console.print()


@app.command("provider")
def provider(
    config_path: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Interactively configure an LLM provider and its models."""
    from markbot.cli.onboard import run_onboard
    from markbot.config.loader import get_config_path, load_config, save_config, set_config_path

    if config_path:
        path = Path(config_path).expanduser().resolve()
        set_config_path(path)
    else:
        path = get_config_path()

    cfg = load_config(path) if path.exists() else Config()
    result = run_onboard(initial_config=cfg)
    if result.should_save:
        save_config(result.config, path)
        console.print(f"[green]✓[/green] Config saved at {path}")
    else:
        console.print("[yellow]No changes saved.[/yellow]")


@app.command("channel")
def channel(
    config_path: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Interactively configure chat channels."""
    from markbot.cli.onboard import _configure_channels
    from markbot.config.loader import get_config_path, load_config, save_config, set_config_path

    if config_path:
        path = Path(config_path).expanduser().resolve()
        set_config_path(path)
    else:
        path = get_config_path()

    cfg = load_config(path) if path.exists() else Config()
    original = cfg.model_copy(deep=True)
    _configure_channels(cfg)
    if cfg.model_dump(by_alias=True) != original.model_dump(by_alias=True):
        save_config(cfg, path)
        console.print(f"[green]✓[/green] Config saved at {path}")
    else:
        console.print("[dim]No changes made.[/dim]")


@app.command("show")
def show(
    config_path: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Display a rich summary of the current configuration."""
    from pathlib import Path

    from markbot.cli.onboard import _show_summary
    from markbot.config.loader import get_config_path, load_config, set_config_path

    if config_path:
        path = Path(config_path).expanduser().resolve()
        set_config_path(path)
    else:
        path = get_config_path()

    cfg = load_config(path) if path.exists() else Config()
    _show_summary(cfg)
