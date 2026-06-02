"""``markbot plugins`` group: list discovered channel plugins."""
from __future__ import annotations

import typer

from markbot.cli.ui import console, markbot_banner

app = typer.Typer(help="Manage channel plugins")


@app.command("list")
def list_():  # noqa: A001  (shadows builtin, intentional for CLI sub-command)
    """List all discovered channels (built-in and plugins)."""
    from rich.text import Text

    from markbot.channels.discovery import discover_all, discover_channel_names
    from markbot.config.loader import load_config

    config = load_config()
    builtin_names = set(discover_channel_names())
    all_channels = discover_all()

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
