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
from markbot.cli.groups import agent, channels, config, gateway, onboard, plugins, provider, status, web
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

# Add agent + onboard + gateway + channels + provider + config + plugins + status + web subcommands (moved from this file in P1-1)
app.add_typer(agent.app, name="agent")
app.add_typer(channels.app, name="channels")
app.add_typer(config.app, name="config")
app.add_typer(gateway.app, name="gateway")
app.add_typer(onboard.app, name="onboard")
app.add_typer(plugins.app, name="plugins")
app.add_typer(provider.app, name="provider")
app.add_typer(status.app, name="status")
app.add_typer(web.app, name="web")

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

if __name__ == "__main__":
    app()
