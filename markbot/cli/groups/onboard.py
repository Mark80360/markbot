"""``markbot onboard`` top-level command.

Moved from ``markbot.cli.commands`` in the P1-1 refactor. The command
initializes MarkBot configuration and workspace, with three modes:

- ``--defaults``: non-interactive, all defaults (for scripts/Docker).
- ``--wizard``: interactive menu-driven setup.
- ``--guided``: linear step-by-step guided setup (recommended for
  first-time users).
"""
from __future__ import annotations

from pathlib import Path

import typer

from markbot import __logo__
from markbot.cli.runtime import _onboard_plugins
from markbot.cli.ui import console, markbot_banner
from markbot.config.paths import get_workspace_path
from markbot.utils.helpers import sync_workspace_templates

app = typer.Typer(
    help="Initialize MarkBot configuration and workspace.",
    invoke_without_command=True,
)


@app.callback()
def onboard(
    ctx: typer.Context,
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    wizard: bool = typer.Option(False, "--wizard", help="Use interactive menu-driven wizard"),
    guided: bool = typer.Option(False, "--guided", help="Use linear step-by-step guided setup (recommended for first-time users)"),
    defaults: bool = typer.Option(False, "--defaults", help="Use all default values (non-interactive, for scripts/Docker)"),
):
    """Initialize MarkBot configuration and workspace."""
    if ctx.invoked_subcommand is not None:
        return

    from markbot.config.loader import get_config_path, load_config, save_config, set_config_path
    from markbot.config.schema import Config

    markbot_banner()
    console.print()

    if config:
        config_path = Path(config).expanduser().resolve()
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")
    else:
        config_path = get_config_path()

    def _apply_workspace_override(loaded: Config) -> Config:
        if workspace:
            loaded.agents.defaults.workspace = workspace
        return loaded

    # --defaults mode: create config with all defaults, no interaction
    if defaults:
        config_obj = _apply_workspace_override(Config())
        save_config(config_obj, config_path)
        console.print(f"[green]✓[/green] Created default config at {config_path}")
        _onboard_plugins(config_path)

        workspace_path = get_workspace_path(config_obj.workspace_path)
        if not workspace_path.exists():
            workspace_path.mkdir(parents=True, exist_ok=True)
            console.print(f"[green]✓[/green] Created workspace at {workspace_path}")
        sync_workspace_templates(workspace_path)

        console.print(f"\n{__logo__} Default configuration created!")
        console.print(f"  Edit config: [cyan]{config_path}[/cyan]")
        console.print("  Run guided setup: [cyan]markbot onboard --guided[/cyan]")
        return

    # Create or update config
    is_interactive = wizard or guided
    if config_path.exists():
        if is_interactive:
            config_obj = _apply_workspace_override(load_config(config_path))
        else:
            console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
            console.print("  [bold]y[/bold] = overwrite with defaults (existing values will be lost)")
            console.print("  [bold]N[/bold] = refresh config, keeping existing values and adding new fields")
            if typer.confirm("Overwrite?"):
                config_obj = _apply_workspace_override(Config())
                save_config(config_obj, config_path)
                console.print(f"[green]✓[/green] Config reset to defaults at {config_path}")
            else:
                config_obj = _apply_workspace_override(load_config(config_path))
                save_config(config_obj, config_path)
                console.print(f"[green]✓[/green] Config refreshed at {config_path} (existing values preserved)")
    else:
        config_obj = _apply_workspace_override(Config())
        if not is_interactive:
            save_config(config_obj, config_path)
            console.print(f"[green]✓[/green] Created config at {config_path}")

    # Run interactive wizard if enabled
    if wizard:
        from markbot.cli.onboard import run_onboard

        try:
            result = run_onboard(initial_config=config_obj)
            if not result.should_save:
                console.print("[yellow]Configuration discarded. No changes were saved.[/yellow]")
                return

            config_obj = result.config
            save_config(config_obj, config_path)
            console.print(f"[green]✓[/green] Config saved at {config_path}")
        except Exception as e:
            console.print(f"[red]✗[/red] Error during configuration: {e}")
            console.print("[yellow]Please run 'markbot onboard' again to complete setup.[/yellow]")
            raise typer.Exit(1)

    # Run guided onboarding if enabled
    if guided:
        from markbot.cli.onboard import run_guided_onboard

        try:
            result = run_guided_onboard(initial_config=config_obj)
            if not result.should_save:
                console.print("[yellow]Configuration discarded. No changes were saved.[/yellow]")
                return

            config_obj = result.config
            save_config(config_obj, config_path)
            console.print(f"[green]✓[/green] Config saved at {config_path}")
        except Exception as e:
            console.print(f"[red]✗[/red] Error during configuration: {e}")
            console.print("[yellow]Please run 'markbot onboard --guided' again to complete setup.[/yellow]")
            raise typer.Exit(1)

    _onboard_plugins(config_path)

    # Create workspace, preferring the configured workspace path.
    workspace_path = get_workspace_path(config_obj.workspace_path)
    if not workspace_path.exists():
        workspace_path.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]✓[/green] Created workspace at {workspace_path}")

    sync_workspace_templates(workspace_path)

    agent_cmd = 'markbot agent -m "Hello!"'
    gateway_cmd = "markbot gateway"
    if config:
        agent_cmd += f" --config {config_path}"
        gateway_cmd += f" --config {config_path}"

    console.print(f"\n{__logo__} I'm ready!")
    console.print("\nNext steps:")
    if is_interactive:
        console.print(f"  1. Chat: [cyan]{agent_cmd}[/cyan]")
        console.print(f"  2. Start gateway: [cyan]{gateway_cmd}[/cyan]")
    else:
        console.print(f"  1. Add your API key to [cyan]{config_path}[/cyan]")
        console.print(f"  2. Chat: [cyan]{agent_cmd}[/cyan]")
