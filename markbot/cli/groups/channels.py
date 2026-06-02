"""``markbot channels`` group: IM bridge status and login."""
from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from markbot import __logo__
from markbot.cli.ui import console, markbot_banner

app = typer.Typer(help="Manage channels")


@app.command("status")
def status():
    """Show channel status."""
    from rich.text import Text

    from markbot.channels.discovery import discover_all
    from markbot.config.loader import load_config

    config = load_config()

    markbot_banner()

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


@app.command("login")
def login(
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
        markbot_banner()
        console.print()
        console.print(f"  [red]✗[/red] Unknown channel: [cyan]{channel_name}[/cyan]")
        console.print(f"  Available: {', '.join(all_channels.keys())}")
        console.print()
        raise typer.Exit(1)

    markbot_banner()

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
