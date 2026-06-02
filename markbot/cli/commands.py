"""CLI commands for Markbot."""

import asyncio
import os
import sys
from datetime import datetime
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
from markbot.cli.daemon import (
    _gateway_paths,
    is_process_running,
    read_pid,
    remove_pid,
    start_daemon,
    terminate_process,
)
from markbot.cli.doctor import doctor_app
from markbot.cli.groups import agent, gateway, onboard
from markbot.cli.runtime import make_provider
from markbot.cli.ui import console, markbot_banner
from markbot.cli.autopilot import app as autopilot_app
from markbot.cli.skills import app as skills_app
from markbot.config.paths import get_cron_dir
from markbot.config.schema import Config
from markbot.utils.helpers import sync_workspace_templates

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

# Add agent + onboard + gateway subcommands (moved from this file in P1-1)
app.add_typer(agent.app, name="agent")
app.add_typer(gateway.app, name="gateway")
app.add_typer(onboard.app, name="onboard")

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


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status():
    """Show channel status."""
    from rich.text import Text

    from markbot.channels.discovery import discover_all
    from markbot.config.loader import load_config

    config = load_config()

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

    # ─ Channel Status ────────────────────────────────────────────────────────
    section("Channels", "cyan")

    all_channels = discover_all()
    enabled_count = 0

    for name, cls in sorted(all_channels.items()):
        section_cfg = getattr(config.channels, name, None)
        if section_cfg is None:
            enabled = False
        elif isinstance(section_cfg, dict):
            enabled = section_cfg.get("enabled", False)
        else:
            enabled = getattr(section_cfg, "enabled", False)

        if enabled:
            enabled_count += 1
            kv(cls.display_name, "[green]● Enabled[/green]")
        else:
            kv(cls.display_name, "[dim]○ Disabled[/dim]")

    divider()

    # ─ Summary ───────────────────────────────────────────────────────────────
    section("Summary", "blue")
    kv("Total", str(len(all_channels)))
    kv("Enabled", f"[green]{enabled_count}[/green]")
    kv("Disabled", f"[dim]{len(all_channels) - enabled_count}[/dim]")

    divider()
    console.print()


def _get_bridge_dir() -> Path:
    """Get the bridge directory, setting it up if needed."""
    import shutil
    import subprocess

    # User's bridge location
    from markbot.config.paths import get_bridge_install_dir

    user_bridge = get_bridge_install_dir()

    # Check if already built
    if (user_bridge / "dist" / "index.js").exists():
        return user_bridge

    # Check for npm
    npm_path = shutil.which("npm")
    if not npm_path:
        console.print("[red]npm not found. Please install Node.js >= 18.[/red]")
        raise typer.Exit(1)

    # Find source bridge: first check package data, then source dir
    pkg_bridge = Path(__file__).parent.parent / "bridge"  # markbot/bridge (installed)
    src_bridge = Path(__file__).parent.parent.parent / "bridge"  # repo root/bridge (dev)

    source = None
    if (pkg_bridge / "package.json").exists():
        source = pkg_bridge
    elif (src_bridge / "package.json").exists():
        source = src_bridge

    if not source:
        console.print("[red]Bridge source not found.[/red]")
        console.print("Try reinstalling: pip install --force-reinstall markbot")
        raise typer.Exit(1)

    console.print(f"{__logo__} Setting up bridge...")

    # Copy to user directory
    user_bridge.parent.mkdir(parents=True, exist_ok=True)
    if user_bridge.exists():
        shutil.rmtree(user_bridge)
    shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))

    # Install and build
    try:
        console.print("  Installing dependencies...")
        subprocess.run([npm_path, "install"], cwd=user_bridge, check=True, capture_output=True)

        console.print("  Building...")
        subprocess.run([npm_path, "run", "build"], cwd=user_bridge, check=True, capture_output=True)

        console.print("[green]✓[/green] Bridge ready\n")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Build failed: {e}[/red]")
        if e.stderr:
            console.print(f"[dim]{e.stderr.decode()[:500]}[/dim]")
        raise typer.Exit(1)

    return user_bridge


@channels_app.command("login")
def channels_login(
    channel_name: str = typer.Argument(..., help="Channel name (e.g. weixin)"),
    force: bool = typer.Option(False, "--force", "-f", help="Force re-authentication even if already logged in"),
):
    """Authenticate with a channel via QR code or other interactive login."""
    from rich.text import Text

    from markbot.channels.discovery import discover_all
    from markbot.config.loader import load_config

    config = load_config()
    channel_cfg = getattr(config.channels, channel_name, None) or {}

    # Validate channel exists
    all_channels = discover_all()
    if channel_name not in all_channels:
        _markbot_banner()
        console.print()
        console.print(f"  [red]✗[/red] Unknown channel: [cyan]{channel_name}[/cyan]")
        console.print(f"  Available: {', '.join(all_channels.keys())}")
        console.print()
        raise typer.Exit(1)

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

    # ─ Login Info ────────────────────────────────────────────────────────────
    section(f"{all_channels[channel_name].display_name} Login", "cyan")
    kv("Channel", channel_name)
    kv("Force", "Yes" if force else "No")
    divider()
    console.print()

    channel_cls = all_channels[channel_name]
    channel = channel_cls(channel_cfg, bus=None)

    success = asyncio.run(channel.login(force=force))

    if not success:
        section("Result", "red")
        console.print("  [red]✗[/red] Login failed")
        divider()
        console.print()
        raise typer.Exit(1)

    section("Result", "green")
    console.print("  [green]✓[/green] Login successful")
    divider()
    console.print()


# ============================================================================
# Plugin Commands
# ============================================================================

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

provider_app = typer.Typer(help="Manage providers")
app.add_typer(provider_app, name="provider")


_LOGIN_HANDLERS: dict[str, callable] = {}


def _register_login(name: str):
    def decorator(fn):
        _LOGIN_HANDLERS[name] = fn
        return fn
    return decorator


@provider_app.command("login")
def provider_login(
    provider: str = typer.Argument(..., help="OAuth provider (e.g. 'openai-codex', 'github-copilot')"),
):
    """Authenticate with an OAuth provider."""
    from rich.text import Text

    from markbot.providers.registry import PROVIDERS

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

    key = provider.replace("-", "_")
    spec = next((s for s in PROVIDERS if s.name == key and s.is_oauth), None)
    if not spec:
        section("Error", "red")
        console.print(f"  [red]✗[/red] Unknown OAuth provider: [cyan]{provider}[/cyan]")
        kv("Supported", ", ".join(s.name.replace("_", "-") for s in PROVIDERS if s.is_oauth))
        divider()
        console.print()
        raise typer.Exit(1)

    handler = _LOGIN_HANDLERS.get(spec.name)
    if not handler:
        section("Error", "red")
        console.print(f"  [red]✗[/red] Login not implemented for [cyan]{spec.label}[/cyan]")
        divider()
        console.print()
        raise typer.Exit(1)

    # ─ OAuth Login ───────────────────────────────────────────────────────────
    section(f"{spec.label} Login", "cyan")
    kv("Provider", spec.name.replace("_", "-"))
    kv("Type", "OAuth")
    divider()
    console.print()

    handler()


@_register_login("openai_codex")
def _login_openai_codex() -> None:
    try:
        from oauth_cli_kit import get_token, login_oauth_interactive
        token = None
        try:
            token = get_token()
        except Exception:
            pass
        if not (token and token.access):
            console.print("[cyan]Starting interactive OAuth login...[/cyan]\n")
            token = login_oauth_interactive(
                print_fn=lambda s: console.print(s),
                prompt_fn=lambda s: typer.prompt(s),
            )
        if not (token and token.access):
            console.print("[red]✗ Authentication failed[/red]")
            raise typer.Exit(1)
        console.print(f"[green]✓ Authenticated with OpenAI Codex[/green]  [dim]{token.account_id}[/dim]")
    except ImportError:
        console.print("[red]oauth_cli_kit not installed. Run: pip install oauth-cli-kit[/red]")
        raise typer.Exit(1)


@_register_login("github_copilot")
def _login_github_copilot() -> None:
    import asyncio

    from openai import AsyncOpenAI

    console.print("[cyan]Starting GitHub Copilot device flow...[/cyan]\n")

    async def _trigger():
        client = AsyncOpenAI(
            api_key="dummy",
            base_url="https://api.githubcopilot.com",
        )
        await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
        )

    try:
        asyncio.run(_trigger())
        console.print("[green]✓ Authenticated with GitHub Copilot[/green]")
    except Exception as e:
        console.print(f"[red]Authentication error: {e}[/red]")
        raise typer.Exit(1)


# ============================================================================
# Config Commands
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
