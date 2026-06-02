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
from markbot.cli.groups import agent, channels, config, gateway, onboard, provider
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

# Add agent + onboard + gateway + channels + provider + config subcommands (moved from this file in P1-1)
app.add_typer(agent.app, name="agent")
app.add_typer(channels.app, name="channels")
app.add_typer(config.app, name="config")
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

if __name__ == "__main__":
    app()
