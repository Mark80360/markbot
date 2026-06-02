"""CLI commands for Markbot."""

import os
import sys
from pathlib import Path

# Force UTF-8 encoding for Windows console
if sys.platform == "win32":
    if sys.stdout.encoding != "utf-8":
        os.environ["PYTHONIOENCODING"] = "utf-8"
        # Re-open stdout/stderr with UTF-8 encoding
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import typer

from markbot import __logo__, __version__
from markbot.cli.autopilot import app as autopilot_app
from markbot.cli.doctor import doctor_app
from markbot.cli.groups import agent, channels, gateway, onboard, provider
from markbot.cli.skills import app as skills_app
from markbot.cli.ui import console
from markbot.config.schema import Config

app = typer.Typer(
    name="markbot",
    context_settings={"help_option_names": ["-h", "--help"]},
    help=f"{__logo__} MarkBot - Personal AI Assistant",
    no_args_is_help=True,
)

# Add skill management subcommands
app.add_typer(skills_app, name="skills")

# Add autopilot pipeline subcommands
app.add_typer(autopilot_app, name="autopilot")

# Add doctor diagnostic subcommand
app.add_typer(doctor_app, name="doctor")

# Add agent + onboard + gateway + channels + provider subcommands (moved from this file in P1-1)
app.add_typer(agent.app, name="agent")
app.add_typer(channels.app, name="channels")
app.add_typer(gateway.app, name="gateway")
app.add_typer(onboard.app, name="onboard")
app.add_typer(provider.app, name="provider")

# ---------------------------------------------------------------------------
# version_callback + main callback
# ---------------------------------------------------------------------------


def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} MarkBot v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
):
    """MarkBot - Personal AI Assistant."""
    pass



plugins_app = typer.Typer(help="Manage channel plugins")
app.add_typer(plugins_app, name="plugins")


@plugins_app.command("list")
def plugins_list():
    """List all discovered channels (built-in and plugins)."""
    from rich.text import Text

    from markbot.channels.discovery import discover_all, discover_channel_names
    from markbot.config.loader import load_config

    config = load_config()
    builtin_names = set(discover_channel_names())
    all_channels = discover_all()

    _markbot_banner()

    W = 72  # total width

    def section(title: str, color: str = "cyan") -> None:
        title_text = f"  {title}  "
        pad = W - len(title_text) - 2
        line = Text.from_markup(f"[{color}]{title_text}[/][dim]{'─' * pad}[/]")
        console.print(line)

    def kv(key: str, value: str, key_w: int = 14) -> None:
        line = Text.from_markup(f"  [cyan]{key:<{key_w}}[/cyan] {value}")
        console.print(line)

    def divider() -> None:
        line = Text.from_markup(f"[dim]{'─' * (W - 2)}[/]")
        console.print(line)

    console.print()
    console.print("[dim]This command shows channel sources (built-in vs external plugins).[/dim]")
    console.print("[dim]For runtime status (enable/disable), use: markbot channels status[/dim]\n")

    # ─ Plugins ───────────────────────────────────────────────────────────────
    section("Plugins", "cyan")

    builtin_count = 0
    plugin_count = 0
    enabled_count = 0

    for name in sorted(all_channels):
        cls = all_channels[name]
        source = "builtin" if name in builtin_names else "plugin"
        section_cfg = getattr(config.channels, name, None)
        if section_cfg is None:
            enabled = False
        elif isinstance(section_cfg, dict):
            enabled = section_cfg.get("enabled", False)
        else:
            enabled = getattr(section_cfg, "enabled", False)

        if source == "builtin":
            builtin_count += 1
        else:
            plugin_count += 1
        if enabled:
            enabled_count += 1

        status_str = "[green]● Enabled[/green]" if enabled else "[dim]○ Disabled[/dim]"
        source_str = f"[dim]({source})[/dim]"
        kv(cls.display_name, f"{status_str} {source_str}")

    divider()

    # ─ Summary ───────────────────────────────────────────────────────────────
    section("Summary", "blue")
    kv("Total", str(len(all_channels)))
    kv("Built-in", str(builtin_count))
    kv("External", str(plugin_count))
    kv("Enabled", f"[green]{enabled_count}[/green]")

    divider()

    if plugin_count == 0:
        console.print("\n[dim]💡 No external plugins installed.[/dim]")
        console.print("[dim]   Install via: pip install markbot-<channel>-plugin[/dim]")
        console.print("[dim]   Or create your own: markbot skills skill-creator[/dim]\n")

    console.print()


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def web(
    port: int = typer.Option(9120, "--port", "-p", help="Web server port"),
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Web server host"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
):
    """Start the Markbot Web UI server."""
    from markbot.web.server import start_server

    if config:
        from markbot.config.loader import set_config_path
        set_config_path(Path(config).expanduser().resolve())

    if workspace:
        from markbot.config.loader import load_config
        cfg = load_config()
        cfg.agents.defaults.workspace = workspace

    _markbot_banner()
    start_server(host=host, port=port)


@app.command()
def status():
    """Show MarkBot status."""
    import platform

    from rich.text import Text

    from markbot.config.loader import get_config_path, load_config
    from markbot.providers.registry import PROVIDERS

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    _markbot_banner()

    W = 72  # total width

    def section(title: str, color: str = "cyan") -> None:
        title_text = f"  {title}  "
        pad = W - len(title_text) - 2
        line = Text.from_markup(f"[{color}]{title_text}[/][dim]{'─' * pad}[/]")
        console.print(line)

    def kv(key: str, value: str, key_w: int = 14) -> None:
        line = Text.from_markup(f"  [cyan]{key:<{key_w}}[/cyan] {value}")
        console.print(line)

    def divider() -> None:
        line = Text.from_markup(f"[dim]{'─' * (W - 2)}[/]")
        console.print(line)

    console.print()

    # ─ System ────────────────────────────────────────────────────────────────
    section("System", "cyan")
    kv("OS", f"{platform.system()} {platform.release()}")
    kv("Python", platform.python_version())
    kv("Hostname", platform.node())

    divider()

    # ─ Configuration ─────────────────────────────────────────────────────────
    section("Configuration", "blue")
    kv("Config Path", f"{config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
    kv("Workspace", f"{workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}")
    if config.agents.defaults.model_chain:
        for i, model in enumerate(config.agents.defaults.model_chain):
            kv("Model Chain" if i == 0 else "", model)
    else:
        kv("Model Chain", "[not configured]")

    divider()

    # ─ Providers ─────────────────────────────────────────────────────────────
    section("Providers", "magenta")
    for spec in PROVIDERS:
        p = getattr(config.providers, spec.name, None)
        if p is None:
            continue
        if spec.is_oauth:
            kv(spec.label, "[green]✓[/green] (OAuth)")
        elif spec.is_local:
            if p.api_base:
                kv(spec.label, f"[green]✓[/green] {p.api_base}")
            else:
                kv(spec.label, "[dim]not set[/dim]")
        else:
            has_key = bool(p.api_key)
            kv(spec.label, "[green]✓[/green]" if has_key else "[dim]not set[/dim]")

    divider()
    console.print()


# ============================================================================
# OAuth Login
# ============================================================================

config_app = typer.Typer(
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


@config_app.command("get")
def config_get(
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
        _markbot_banner()
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
        _markbot_banner()

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


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Configuration key (dot notation, e.g., agents.defaults.modelChain)"),
    value: str = typer.Argument(..., help="Value to set (auto-detected type: string, number, boolean, null)"),
):
    """Set a configuration value."""
    import json

    from rich.text import Text

    from markbot.config.loader import load_config, save_config
    from markbot.config.schema import Config

    config = load_config()
    data = config.model_dump(by_alias=True)

    parsed_value = _parse_value(value)

    if _get_nested_value(data, key) is None:
        _markbot_banner()
        console.print()
        console.print(f"  [yellow]⚠[/yellow] Warning: Key '{key}' does not exist in config schema")
        console.print()

    if not _set_nested_value(data, key, parsed_value):
        _markbot_banner()
        console.print()
        console.print(f"  [red]✗[/red] Error: Failed to set '{key}'. Check if the path is valid.")
        console.print()
        raise typer.Exit(1)

    try:
        new_config = Config.model_validate(data)
    except Exception as e:
        _markbot_banner()
        console.print()
        console.print(f"  [red]✗[/red] Error: Invalid value for '{key}': {e}")
        console.print()
        raise typer.Exit(1)

    save_config(new_config)

    _markbot_banner()

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


@config_app.command("list")
def config_list(
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
        _markbot_banner()
        console.print()
        console.print(f"  [yellow]○[/yellow] No keys found with prefix '{prefix}'")
        console.print()
        return

    _markbot_banner()

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


app.add_typer(config_app, name="config")


@config_app.command("provider")
def config_provider(
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


@config_app.command("channel")
def config_channel(
    config_path: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Interactively configure chat channels."""
    from markbot.cli.onboard import _configure_channels, run_onboard
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


@config_app.command("show")
def config_show(
    config_path: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Display a rich summary of the current configuration."""
    from markbot.cli.onboard import _show_summary
    from markbot.config.loader import get_config_path, load_config, set_config_path

    if config_path:
        path = Path(config_path).expanduser().resolve()
        set_config_path(path)
    else:
        path = get_config_path()

    cfg = load_config(path) if path.exists() else Config()
    _show_summary(cfg)


if __name__ == "__main__":
    app()
