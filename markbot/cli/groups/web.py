"""``markbot web`` top-level command: start the Web UI server."""
from __future__ import annotations

from pathlib import Path

import typer

from markbot.cli.ui import markbot_banner

app = typer.Typer(
    help="Start the Markbot Web UI server.",
    invoke_without_command=True,
)


@app.callback()
def web(
    ctx: typer.Context,
    port: int = typer.Option(9120, "--port", "-p", help="Web server port"),
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Web server host"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
):
    """Start the Markbot Web UI server."""
    if ctx.invoked_subcommand is not None:
        return
    from markbot.web.server import start_server

    if config:
        from markbot.config.loader import set_config_path
        set_config_path(Path(config).expanduser().resolve())

    if workspace:
        from markbot.config.loader import set_workspace_override
        set_workspace_override(workspace)

    markbot_banner()
    start_server(host=host, port=port, workspace=workspace)
