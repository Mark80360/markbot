"""``markbot status`` top-level command: show MarkBot status."""
from __future__ import annotations

import platform

import typer

from markbot.cli.ui import console, make_section_helpers, markbot_banner

app = typer.Typer(
    help="Show MarkBot status.",
    invoke_without_command=True,
)


@app.callback()
def status(ctx: typer.Context):
    """Show MarkBot status."""
    if ctx.invoked_subcommand is not None:
        return
    from markbot.config.loader import get_config_path, load_config
    from markbot.providers.registry import PROVIDERS

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    markbot_banner()

    section, kv, divider = make_section_helpers()

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
