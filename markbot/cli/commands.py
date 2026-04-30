"""CLI commands for Markbot."""

import asyncio
import os
import select
import signal
import sys
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any

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
from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.formatted_text import ANSI, HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from markbot import __logo__, __version__
from markbot.cli.skills import app as skills_app
from markbot.cli.stream import StreamRenderer, ThinkingSpinner
from markbot.config.paths import get_cron_dir, get_workspace_path
from markbot.config.schema import Config
from markbot.utils.helpers import strip_ansi, sync_workspace_templates

app = typer.Typer(
    name="markbot",
    context_settings={"help_option_names": ["-h", "--help"]},
    help=f"{__logo__} MarkBot - Personal AI Assistant",
    no_args_is_help=True,
)

# Add skill management subcommands
app.add_typer(skills_app, name="skills")

# Add autopilot pipeline subcommands
from markbot.cli.autopilot import app as autopilot_app
app.add_typer(autopilot_app, name="autopilot")

# Add doctor diagnostic subcommand
from markbot.cli.doctor import doctor_app
app.add_typer(doctor_app, name="doctor")

console = Console()
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROCESS_STOP_RETRIES = 10
PROCESS_STOP_WAIT_INTERVAL = 0.5
DAEMON_STARTUP_WAIT = 0.5

# ---------------------------------------------------------------------------
# CLI input: prompt_toolkit for editing, paste, history, and display
# ---------------------------------------------------------------------------

_PROMPT_SESSION: PromptSession | None = None
_SAVED_TERM_ATTRS = None  # original termios settings, restored on exit

def _markbot_banner():
    console.print()
    console.print("  ███╗░░░███╗░█████╗░██████╗░██╗░░██╗██████╗░░█████╗░████████╗")
    console.print("  ████╗░████║██╔══██╗██╔══██╗██║░██╔╝██╔══██╗██╔══██╗╚══██╔══╝")
    console.print("  ██╔████╔██║███████║██████╔╝█████═╝░██████╦╝██║░░██║░░░██║░░░")
    console.print("  ██║╚██╔╝██║██╔══██║██╔══██╗██╔═██╗░██╔══██╗██║░░██║░░░██║░░░")
    console.print("  ██║░╚═╝░██║██║░░██║██║░░██║██║░╚██╗██████╦╝╚█████╔╝░░░██║░░░")
    console.print("  ╚═╝░░░░░╚═╝╚═╝░░╚═╝╚═╝░░╚═╝╚═╝░░╚═╝╚═════╝░░╚════╝░░░░╚═╝░░░")

def _flush_pending_tty_input() -> None:
    """Drop unread keypresses typed while the model was generating output."""
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return
    except Exception:
        return

    try:
        import termios
        termios.tcflush(fd, termios.TCIFLUSH)
        return
    except Exception:
        pass

    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                break
            if not os.read(fd, 4096):
                break
    except Exception:
        return


def _restore_terminal() -> None:
    """Restore terminal to its original state (echo, line buffering, etc.)."""
    if _SAVED_TERM_ATTRS is None:
        return
    try:
        import termios
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _SAVED_TERM_ATTRS)
    except Exception:
        pass


def _init_prompt_session() -> None:
    """Create the prompt_toolkit session with persistent file history."""
    global _PROMPT_SESSION, _SAVED_TERM_ATTRS

    # Save terminal state so we can restore it on exit
    try:
        import termios
        _SAVED_TERM_ATTRS = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    from markbot.config.paths import get_cli_history_path

    history_file = get_cli_history_path()
    history_file.parent.mkdir(parents=True, exist_ok=True)

    _PROMPT_SESSION = PromptSession(
        history=FileHistory(str(history_file)),
        enable_open_in_editor=False,
        multiline=False,   # Enter submits (single line mode)
    )


def _make_console() -> Console:
    return Console(file=sys.stdout)


def _render_interactive_ansi(render_fn) -> str:
    """Render Rich output to ANSI so prompt_toolkit can print it safely."""
    ansi_console = Console(
        force_terminal=True,
        color_system=console.color_system or "standard",
        width=console.width,
    )
    with ansi_console.capture() as capture:
        render_fn(ansi_console)
    return capture.get()


def _print_agent_response(
    response: str,
    render_markdown: bool,
    metadata: dict | None = None,
) -> None:
    """Render assistant response with consistent terminal styling."""
    console = _make_console()
    content = response or ""
    body = _response_renderable(content, render_markdown, metadata)
    console.print()
    console.print(f"[cyan]{__logo__} MarkBot[/cyan]")
    console.print(body)
    console.print()


def _response_renderable(content: str, render_markdown: bool, metadata: dict | None = None):
    """Render plain-text command output without markdown collapsing newlines."""
    if not render_markdown:
        return Text(content)
    if (metadata or {}).get("render_as") == "text":
        return Text(content)
    return Markdown(content)


async def _print_interactive_line(text: str) -> None:
    """Print async interactive updates with prompt_toolkit-safe Rich styling."""
    def _write() -> None:
        ansi = _render_interactive_ansi(
            lambda c: c.print(f"  [dim]↳ {text}[/dim]")
        )
        print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


async def _print_interactive_response(
    response: str,
    render_markdown: bool,
    metadata: dict | None = None,
) -> None:
    """Print async interactive replies with prompt_toolkit-safe Rich styling."""
    def _write() -> None:
        content = response or ""
        ansi = _render_interactive_ansi(
            lambda c: (
                c.print(),
                c.print(f"[cyan]{__logo__} MarkBot[/cyan]"),
                c.print(_response_renderable(content, render_markdown, metadata)),
                c.print(),
            )
        )
        print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


def _print_cli_progress_line(text: str, thinking: ThinkingSpinner | None) -> None:
    """Print a CLI progress line, pausing the spinner if needed."""
    with thinking.pause() if thinking else nullcontext():
        console.print(f"  [dim]↳ {text}[/dim]")


async def _print_interactive_progress_line(text: str, thinking: ThinkingSpinner | None) -> None:
    """Print an interactive progress line, pausing the spinner if needed."""
    with thinking.pause() if thinking else nullcontext():
        await _print_interactive_line(text)


def _is_exit_command(command: str) -> bool:
    """Return True when input should end interactive chat."""
    return command.lower() in EXIT_COMMANDS


async def _read_interactive_input_async() -> str:
    """Read user input using prompt_toolkit (handles paste, history, display).

    prompt_toolkit natively handles:
    - Multiline paste (bracketed paste mode)
    - History navigation (up/down arrows)
    - Clean display (no ghost characters or artifacts)
    """
    if _PROMPT_SESSION is None:
        raise RuntimeError("Call _init_prompt_session() first")
    try:
        with patch_stdout():
            return await _PROMPT_SESSION.prompt_async(
                HTML("<b fg='ansiblue'>❯</b> "),
            )
    except EOFError as exc:
        raise KeyboardInterrupt from exc



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


# ============================================================================
# Onboard / Setup
# ============================================================================


@app.command()
def onboard(
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    wizard: bool = typer.Option(False, "--wizard", help="Use interactive menu-driven wizard"),
    guided: bool = typer.Option(False, "--guided", help="Use linear step-by-step guided setup (recommended for first-time users)"),
    defaults: bool = typer.Option(False, "--defaults", help="Use all default values (non-interactive, for scripts/Docker)"),
):
    """Initialize MarkBot configuration and workspace."""
    from markbot.config.loader import get_config_path, load_config, save_config, set_config_path
    from markbot.config.schema import Config

    _markbot_banner()
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
        console.print(f"  Run guided setup: [cyan]markbot onboard --guided[/cyan]")
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


def _merge_missing_defaults(existing: Any, defaults: Any) -> Any:
    """Recursively fill in missing values from defaults without overwriting user config."""
    if not isinstance(existing, dict) or not isinstance(defaults, dict):
        return existing

    merged = dict(existing)
    for key, value in defaults.items():
        if key not in merged:
            merged[key] = value
        else:
            merged[key] = _merge_missing_defaults(merged[key], value)
    return merged


def _onboard_plugins(config_path: Path) -> None:
    """Inject default config for all discovered channels (built-in + plugins)."""
    import json

    from markbot.channels.discovery import discover_all

    all_channels = discover_all()
    if not all_channels:
        return

    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)

    channels = data.setdefault("channels", {})
    for name, cls in all_channels.items():
        if name not in channels:
            channels[name] = cls.default_config()
        else:
            channels[name] = _merge_missing_defaults(channels[name], cls.default_config())

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _make_provider(config: Config):
    """
    Create fallback manager from config (V2).

    Returns:
        FallbackManager instance (replaces old single LLMProvider)
    """
    from markbot.providers import FallbackManager

    if not config.agents.defaults.model_chain:
        console.print("[red]Error: No models configured in modelChain.[/red]")
        console.print("Run 'markbot onboard' to configure models.")
        raise typer.Exit(1)

    errors = config.validate_model_chain()
    if errors:
        console.print("[red]Configuration errors:[/red]")
        for error in errors:
            console.print(f"  • {error}")
        raise typer.Exit(1)

    return FallbackManager(config)


def _load_runtime_config(config: str | None = None, workspace: str | None = None) -> Config:
    """Load config and optionally override the active workspace."""
    from markbot.config.loader import load_config, set_config_path

    config_path = None
    if config:
        config_path = Path(config).expanduser().resolve()
        if not config_path.exists():
            console.print(f"[red]Error: Config file not found: {config_path}[/red]")
            raise typer.Exit(1)
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")

    loaded = load_config(config_path)
    _warn_deprecated_config_keys(config_path)
    if workspace:
        loaded.agents.defaults.workspace = workspace
    return loaded


def _warn_deprecated_config_keys(config_path: Path | None) -> None:
    """Hint users to remove obsolete keys from their config file."""
    import json

    from markbot.config.loader import get_config_path

    path = config_path or get_config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    if "memoryWindow" in raw.get("agents", {}).get("defaults", {}):
        console.print(
            "[dim]Hint: `memoryWindow` in your config is no longer used "
            "and can be safely removed.[/dim]"
        )


# ============================================================================
# Gateway / Server
# ============================================================================

# Gateway subcommand app
gateway_app = typer.Typer(
    name="gateway",
    help="Manage the markbot gateway service (start/stop/restart/status).",
)
app.add_typer(gateway_app, name="gateway")

# Gateway PID file management
GATEWAY_PID_DIR = Path.home() / ".markbot" / "gateway"
GATEWAY_PID_FILE = GATEWAY_PID_DIR / "gateway.pid"
GATEWAY_LOG_FILE = GATEWAY_PID_DIR / "gateway.log"


def _ensure_pid_dir() -> None:
    GATEWAY_PID_DIR.mkdir(parents=True, exist_ok=True)


def _read_pid() -> int | None:
    if not GATEWAY_PID_FILE.exists():
        return None
    try:
        return int(GATEWAY_PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def _write_pid(pid: int) -> None:
    _ensure_pid_dir()
    GATEWAY_PID_FILE.write_text(str(pid))


def _remove_pid() -> None:
    if GATEWAY_PID_FILE.exists():
        GATEWAY_PID_FILE.unlink()


def _is_process_running(pid: int) -> bool:
    """Check if a process is running (cross-platform)."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle == 0:
                return False

            exit_code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                kernel32.CloseHandle(handle)
                return exit_code.value == 259  # STILL_ACTIVE

            kernel32.CloseHandle(handle)
            return True
        else:
            # Unix/Linux: send signal 0 to check if process exists
            import os
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError):
        return False
    except Exception:
        return False


def _terminate_process(pid: int, force: bool = False) -> bool:
    """Terminate a process by PID (cross-platform).

    Args:
        pid: Process ID to terminate.
        force: If True, use SIGKILL (Unix) or force terminate (Windows).
              If False, use SIGTERM (Unix) or graceful terminate (Windows).

    Returns:
        True if process was terminated successfully, False otherwise.
    """
    import time

    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            PROCESS_TERMINATE = 0x0001
            handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)

            if handle == 0:
                return False

            exit_code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                if exit_code.value != 259:  # Not STILL_ACTIVE
                    kernel32.CloseHandle(handle)
                    return True

            kernel32.TerminateProcess(handle, 1 if force else 0)
            kernel32.CloseHandle(handle)
        else:
            import os
            import signal

            if force:
                os.kill(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGTERM)

        for _ in range(PROCESS_STOP_RETRIES):
            time.sleep(PROCESS_STOP_WAIT_INTERVAL)
            if not _is_process_running(pid):
                return True

        return False
    except Exception:
        return False


@gateway_app.command("start")
def gateway_start(
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    daemon: bool = typer.Option(True, "--daemon/--foreground", "-d", help="Run as daemon (background)"),
):
    """Start the Markbot gateway service."""
    _markbot_banner()

    existing_pid = _read_pid()
    if existing_pid and _is_process_running(existing_pid):
        console.print(f"[yellow]Gateway is already running (PID: {existing_pid})[/yellow]")
        console.print("Use [cyan]markbot gateway stop[/cyan] to stop it first.")
        raise typer.Exit(1)

    if daemon:
        _start_daemon(port, workspace, config, verbose)
    else:
        _run_gateway_foreground(port, workspace, config, verbose)


def _start_daemon(port: int, workspace: str | None, config_path: str | None, verbose: bool) -> None:
    """Start the gateway as a daemon process (cross-platform)."""
    from rich.text import Text

    _ensure_pid_dir()

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

    if sys.platform == "win32":
        # Windows: use subprocess
        import subprocess

        cmd = [
            sys.executable, "-m", "markbot",
            "gateway", "start",
            "--port", str(port),
            "--foreground",
        ]

        if workspace:
            cmd.extend(["--workspace", workspace])
        if config_path:
            cmd.extend(["--config", config_path])
        if verbose:
            cmd.append("--verbose")

        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP |
            subprocess.CREATE_NO_WINDOW
        )

        with open(GATEWAY_LOG_FILE, "w") as log_file:
            process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                cwd=str(Path.cwd()),
            )

        import time
        time.sleep(DAEMON_STARTUP_WAIT)

        if process.poll() is not None:
            section("Status", "red")
            console.print("  [red]✗[/red] Failed to start gateway daemon")
            console.print()
            raise typer.Exit(1)

        _write_pid(process.pid)
        section("Status", "green")
        kv("State", "[bold green]● RUNNING[/bold green]")
        kv("PID", str(process.pid))
        divider()
        console.print()

    else:
        # Linux/Unix: use os.fork()
        pid = os.fork()
        if pid > 0:
            _write_pid(pid)
            section("Status", "green")
            kv("State", "[bold green]● RUNNING[/bold green]")
            kv("PID", str(pid))
            kv("Log File", str(GATEWAY_LOG_FILE))
            divider()
            console.print()
            return

        os.setsid()
        sys.stdin.close()
        _ensure_pid_dir()
        with open(GATEWAY_LOG_FILE, "w") as log_file:
            os.dup2(log_file.fileno(), sys.stdout.fileno())
            os.dup2(log_file.fileno(), sys.stderr.fileno())
        try:
            _run_gateway_foreground(port, workspace, config_path, verbose)
        except Exception as e:
            print(f"Gateway failed: {e}", file=sys.stderr)
            sys.exit(1)


def _run_gateway_foreground(port: int, workspace: str | None, config: str | None, verbose: bool) -> None:
    """Run the gateway in foreground mode."""
    # Configure logging
    import logging

    from loguru import logger

    from markbot.agent.loop import AgentLoop
    from markbot.bus.queue import MessageBus
    from markbot.channels.manager import ChannelManager
    from markbot.config.loader import load_config
    from markbot.schedule.cron import CronService
    from markbot.schedule.cron import CronJob
    from markbot.schedule.heartbeat import HeartbeatService
    from markbot.session.session import SessionManager
    logging.basicConfig(level=logging.WARNING)

    logger.remove()
    log_level = "DEBUG" if verbose else "INFO"

    def log_filter(record):
        msg = record["message"]
        if "PING" in msg or "PONG" in msg:
            if "keepalive" in msg.lower() or "websockets" in record["name"]:
                return False
        if record["name"].startswith("websockets"):
            return False
        if record["name"].startswith("urllib3") and record["level"].name == "DEBUG":
            return False
        if record["name"].startswith("httpcore") and record["level"].name == "DEBUG":
            return False
        if record["name"] == "asyncio" and "selector" in msg.lower():
            return False
        return True

    _MAX_CONSOLE_MSG_LEN = 4000

    def _console_format(record):
        msg = record["message"]
        if len(msg) > _MAX_CONSOLE_MSG_LEN:
            record["message"] = msg[:_MAX_CONSOLE_MSG_LEN] + "... [truncated]"
        return (
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>\n"
        )

    logger.add(
        sys.stderr,
        level=log_level,
        format=_console_format,
        colorize=True,
        backtrace=True,
        diagnose=True,
        catch=True,
        filter=log_filter,
    )

    _MAX_FILE_MSG_LEN = 10000

    def _file_format(record):
        time_str = record["time"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        level_str = f"{record['level'].name: <8}"
        msg = strip_ansi(record["message"])
        if len(msg) > _MAX_FILE_MSG_LEN:
            msg = msg[:_MAX_FILE_MSG_LEN] + "... [truncated]"
        return f"{time_str} | {level_str} | {record['name']}:{record['function']}:{record['line']} - {msg}\n"

    logger.add(
        GATEWAY_LOG_FILE,
        level="DEBUG",
        format=_file_format,
        rotation="10 MB",
        retention="7 days",
        backtrace=True,
        diagnose=True,
        catch=True,
        encoding="utf-8",
        filter=log_filter,
    )

    logger.info("Logging configured: level={}, backtrace=True, diagnose=True", log_level)

    # Global exception hooks
    import threading

    def handle_exception(exc_type, exc_value, exc_traceback):
        logger.exception("Unhandled exception: {}:{}",
                        exc_type.__name__, exc_value,
                        exc_info=(exc_type, exc_value, exc_traceback))

    def thread_exception_hook(args):
        logger.exception("Unhandled exception in thread {}: {}:{}",
                        args.thread.name if args.thread else "unknown",
                        args.exc_type.__name__, args.exc_value,
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = handle_exception
    threading.excepthook = thread_exception_hook
    logger.info("Global exception hooks installed")

    config_path = Path(config) if config else None
    config = load_config(config_path)
    if workspace:
        config.agents.defaults.workspace = workspace

    console.print(f"{__logo__} Starting MarkBot gateway on port {port}...")

    sync_workspace_templates(config.workspace_path)
    bus = MessageBus()
    provider = _make_provider(config)
    session_manager = SessionManager(config.workspace_path)

    cron_store_path = get_cron_dir(config.workspace_path) / "jobs.json"
    cron = CronService(cron_store_path)

    agent = AgentLoop(
        bus=bus,
        fallback_manager=provider,
        config=config,
        workspace=config.workspace_path,
        max_iterations=config.agents.defaults.max_tool_iterations,
        context_window_tokens=config.agents.defaults.context_window_tokens,
        web_search_config=config.tools.web.search,
        web_proxy=config.tools.web.proxy or None,
        exec_config=config.tools.exec,
        filesystem_config=config.tools.filesystem,
        memory_config=config.tools.memory,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=session_manager,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        timezone=config.agents.defaults.timezone,
        compaction_config=config.compaction,
        max_budget_usd=config.budget.max_budget_usd if config.budget.enabled else None,
        warn_threshold_usd=config.budget.warn_threshold_usd,
        budget_config=config.budget if config.budget.enabled else None,
    )

    async def on_cron_job(job: CronJob) -> str | None:
        from markbot.tools.cron import CronTool
        from markbot.tools.message import MessageTool
        from markbot.schedule.evaluator import evaluate_response

        reminder_note = (
            "[Scheduled Task] Timer finished.\n\n"
            f"Task '{job.name}' has been triggered.\n"
            f"Scheduled instruction: {job.payload.message}"
        )

        cron_tool = agent.tools.get("cron")
        cron_token = None
        if isinstance(cron_tool, CronTool):
            cron_token = cron_tool.set_cron_context(True)
        try:
            resp = await agent.process_direct(
                reminder_note,
                session_key=f"cron:{job.id}",
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to or "direct",
            )
        finally:
            if isinstance(cron_tool, CronTool) and cron_token is not None:
                cron_tool.reset_cron_context(cron_token)

        response = resp.content if resp else ""

        message_tool = agent.tools.get("message")
        if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
            return response

        if job.payload.deliver and job.payload.to and response:
            should_notify = await evaluate_response(
                response, job.payload.message, provider, agent.model,
            )
            if should_notify:
                from markbot.bus.events import OutboundMessage
                await bus.publish_outbound(OutboundMessage(
                    channel=job.payload.channel or "cli",
                    chat_id=job.payload.to,
                    content=response,
                ))
        return response
    cron.on_job = on_cron_job

    channels = ChannelManager(config, bus)

    def _pick_heartbeat_target() -> tuple[str, str]:
        enabled = set(channels.enabled_channels)
        for item in session_manager.list_sessions():
            key = item.get("key") or ""
            if ":" not in key:
                continue
            channel, chat_id = key.split(":", 1)
            if channel in {"cli", "system"}:
                continue
            if channel in enabled and chat_id:
                return channel, chat_id
        return "cli", "direct"

    async def on_heartbeat_execute(tasks: str) -> str:
        channel, chat_id = _pick_heartbeat_target()

        async def _silent(*_args, **_kwargs):
            pass

        resp = await agent.process_direct(
            tasks,
            session_key="heartbeat",
            channel=channel,
            chat_id=chat_id,
            on_progress=_silent,
        )

        session = agent.sessions.get_or_create("heartbeat")
        session.retain_recent_legal_suffix(hb_cfg.keep_recent_messages)
        agent.sessions.save(session)

        return resp.content if resp else ""

    async def on_heartbeat_notify(response: str) -> None:
        from markbot.bus.events import OutboundMessage
        channel, chat_id = _pick_heartbeat_target()
        if channel == "cli":
            return
        await bus.publish_outbound(OutboundMessage(channel=channel, chat_id=chat_id, content=response))

    hb_cfg = config.gateway.heartbeat

    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        fallback_manager=provider,
        model=agent.model,
        on_execute=on_heartbeat_execute,
        on_notify=on_heartbeat_notify,
        interval_s=hb_cfg.interval_s,
        enabled=hb_cfg.enabled,
        timezone=config.agents.defaults.timezone,
    )

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    console.print(f"[green]✓[/green] Heartbeat: every {hb_cfg.interval_s}s")

    async def run():
        try:
            await cron.start()

            dream_cron = config.tools.memory.dream_cron
            dream_task: asyncio.Task | None = None
            if dream_cron:
                from croniter import croniter
                from zoneinfo import ZoneInfo

                async def _dream_loop():
                    tz = ZoneInfo(config.agents.defaults.timezone)
                    while True:
                        try:
                            now = datetime.now(tz=tz)
                            cron_iter = croniter(dream_cron, now)
                            next_ts = cron_iter.get_next(float)
                            delay = next_ts - now.timestamp()
                            if delay < 0:
                                delay = 0
                            next_dt = datetime.fromtimestamp(next_ts, tz=tz)
                            logger.info(f"[Dream] Next dream at {next_dt} (in {delay:.0f}s)")
                            await asyncio.sleep(delay)
                            logger.info("[Dream] Triggering dream-based memory optimization")
                            await agent.memory_manager.dream()
                        except asyncio.CancelledError:
                            break
                        except Exception as e:
                            logger.error(f"[Dream] Dream optimization failed: {e}")
                            await asyncio.sleep(60)

                dream_task = asyncio.create_task(_dream_loop())
                console.print(f"[green]✓[/green] Dream: cron={dream_cron}")

            await heartbeat.start()
            await asyncio.gather(
                agent.run(),
                channels.start_all(),
            )
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        except Exception:
            import traceback
            console.print("\n[red]Error: Gateway crashed unexpectedly[/red]")
            console.print(traceback.format_exc())
        finally:
            if dream_task:
                dream_task.cancel()
            await agent.close_mcp()
            await heartbeat.stop()
            cron.stop()
            agent.stop()
            await channels.stop_all()

    asyncio.run(run())




# ============================================================================
# Agent Commands
# ============================================================================


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:direct", "--session", "-s", help="Session ID"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    markdown: bool = typer.Option(True, "--markdown/--no-markdown", help="Render assistant output as Markdown"),
    logs: bool = typer.Option(False, "--logs/--no-logs", help="Show MarkBot runtime logs during chat"),
):
    """Interact with the agent directly."""
    from loguru import logger

    from markbot.agent.loop import AgentLoop
    from markbot.bus.queue import MessageBus
    from markbot.schedule.cron import CronService

    config = _load_runtime_config(config, workspace)
    sync_workspace_templates(config.workspace_path)

    bus = MessageBus()
    provider = _make_provider(config)

    cron_store_path = get_cron_dir(config.workspace_path) / "jobs.json"
    cron = CronService(cron_store_path)

    if logs:
        logger.enable("markbot")
        logger.enable("reme")
    else:
        logger.disable("markbot")
        logger.disable("reme")

    agent_loop = AgentLoop(
        bus=bus,
        fallback_manager=provider,
        config=config,
        workspace=config.workspace_path,
        max_iterations=config.agents.defaults.max_tool_iterations,
        context_window_tokens=config.agents.defaults.context_window_tokens,
        web_search_config=config.tools.web.search,
        web_proxy=config.tools.web.proxy or None,
        exec_config=config.tools.exec,
        filesystem_config=config.tools.filesystem,
        memory_config=config.tools.memory,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        timezone=config.agents.defaults.timezone,
        compaction_config=config.compaction,
        max_budget_usd=config.budget.max_budget_usd if config.budget.enabled else None,
        warn_threshold_usd=config.budget.warn_threshold_usd,
        budget_config=config.budget if config.budget.enabled else None,
    )

    # Shared reference for progress callbacks
    _thinking: ThinkingSpinner | None = None

    async def _cli_progress(content: str, *, tool_hint: bool = False) -> None:
        ch = agent_loop.channels_config
        if ch and tool_hint and not ch.send_tool_hints:
            return
        if ch and not tool_hint and not ch.send_progress:
            return
        _print_cli_progress_line(content, _thinking)

    if message:
        # Single message mode — direct call, no bus needed
        async def run_once():
            renderer = StreamRenderer(render_markdown=markdown)
            response = await agent_loop.process_direct(
                message, session_id,
                on_progress=_cli_progress,
                on_stream=renderer.on_delta,
                on_stream_end=renderer.on_end,
            )
            if not renderer.streamed:
                await renderer.close()
                _print_agent_response(
                    response.content if response else "",
                    render_markdown=markdown,
                    metadata=response.metadata if response else None,
                )
            await agent_loop.close_mcp()

        asyncio.run(run_once())
    else:
        # Interactive mode — route through bus like other channels
        from markbot.bus.events import InboundMessage
        _init_prompt_session()

        _markbot_banner()
        console.print(f"{__logo__} MarkBot v{__version__} (type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit)\n")

        if ":" in session_id:
            cli_channel, cli_chat_id = session_id.split(":", 1)
        else:
            cli_channel, cli_chat_id = "cli", session_id

        def _handle_signal(signum, frame):
            sig_name = signal.Signals(signum).name
            _restore_terminal()
            # console.print(f"\nReceived {sig_name}, goodbye!")
            sys.exit(0)

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        # SIGHUP is not available on Windows
        if hasattr(signal, 'SIGHUP'):
            signal.signal(signal.SIGHUP, _handle_signal)
        # Ignore SIGPIPE to prevent silent process termination when writing to closed pipes
        # SIGPIPE is not available on Windows
        if hasattr(signal, 'SIGPIPE'):
            signal.signal(signal.SIGPIPE, signal.SIG_IGN)

        async def run_interactive():
            bus_task = asyncio.create_task(agent_loop.run())
            turn_done = asyncio.Event()
            turn_done.set()
            turn_response: list[tuple[str, dict]] = []
            renderer: StreamRenderer | None = None

            async def _consume_outbound():
                while True:
                    try:
                        msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)

                        if msg.metadata.get("_stream_delta"):
                            if renderer:
                                await renderer.on_delta(msg.content)
                            continue
                        if msg.metadata.get("_stream_end"):
                            if renderer:
                                await renderer.on_end(
                                    resuming=msg.metadata.get("_resuming", False),
                                )
                            continue
                        if msg.metadata.get("_streamed"):
                            turn_done.set()
                            continue

                        if msg.metadata.get("_progress"):
                            is_tool_hint = msg.metadata.get("_tool_hint", False)
                            ch = agent_loop.channels_config
                            if ch and is_tool_hint and not ch.send_tool_hints:
                                pass
                            elif ch and not is_tool_hint and not ch.send_progress:
                                pass
                            else:
                                await _print_interactive_progress_line(msg.content, _thinking)
                            continue

                        if not turn_done.is_set():
                            if msg.content:
                                turn_response.append((msg.content, dict(msg.metadata or {})))
                            turn_done.set()
                        elif msg.content:
                            await _print_interactive_response(
                                msg.content,
                                render_markdown=markdown,
                                metadata=msg.metadata,
                            )

                    except asyncio.TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        break

            outbound_task = asyncio.create_task(_consume_outbound())

            try:
                while True:
                    try:
                        _flush_pending_tty_input()
                        user_input = await _read_interactive_input_async()
                        command = user_input.strip()
                        if not command:
                            continue

                        if _is_exit_command(command):
                            _restore_terminal()
                            console.print("\nGoodbye!")
                            break

                        turn_done.clear()
                        turn_response.clear()
                        renderer = StreamRenderer(render_markdown=markdown)

                        await bus.publish_inbound(InboundMessage(
                            channel=cli_channel,
                            sender_id="user",
                            chat_id=cli_chat_id,
                            content=user_input,
                            metadata={"_wants_stream": True},
                        ))

                        await turn_done.wait()

                        if turn_response:
                            content, meta = turn_response[0]
                            if content and not meta.get("_streamed"):
                                if renderer:
                                    await renderer.close()
                                _print_agent_response(
                                    content, render_markdown=markdown, metadata=meta,
                                )
                        elif renderer and not renderer.streamed:
                            await renderer.close()
                    except KeyboardInterrupt:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
                    except EOFError:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
            finally:
                agent_loop.stop()
                outbound_task.cancel()
                await asyncio.gather(bus_task, outbound_task, return_exceptions=True)
                await agent_loop.close_mcp()

        asyncio.run(run_interactive())


@gateway_app.command("stop")
def gateway_stop(
    force: bool = typer.Option(False, "--force", "-f", help="Force kill the process"),
):
    """Stop the MarkBot gateway service."""
    from rich.text import Text

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

    pid = _read_pid()
    if not pid:
        section("Status", "yellow")
        console.print("  [yellow]○ Gateway is not running (no PID file found)[/yellow]")
        divider()
        console.print()
        raise typer.Exit(0)

    if not _is_process_running(pid):
        _remove_pid()
        section("Status", "yellow")
        console.print("  [yellow]○ Gateway is not running (stale PID file removed)[/yellow]")
        divider()
        console.print()
        raise typer.Exit(0)

    section("Status", "cyan")
    kv("State", f"[bold cyan]● STOPPING[/bold cyan] (PID: {pid})")
    kv("Action", "Force kill" if force else "Graceful terminate")
    console.print()

    if _terminate_process(pid, force):
        _remove_pid()
        section("Status", "green")
        kv("State", "[bold green]● STOPPED[/bold green]")
        divider()
        console.print()
        raise typer.Exit(0)

    section("Status", "red")
    kv("State", "[bold red]● FAILED[/bold red]")
    console.print("  [yellow]Gateway did not stop gracefully. Use --force to kill it.[/yellow]")
    divider()
    console.print()
    raise typer.Exit(1)


@gateway_app.command("restart")
def gateway_restart(
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    force: bool = typer.Option(False, "--force", "-f", help="Force kill before restart"),
):
    """Restart the MarkBot gateway service."""
    from rich.text import Text

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

    pid = _read_pid()

    if pid and _is_process_running(pid):
        section("Stop", "cyan")
        kv("PID", str(pid))
        kv("Action", "Force kill" if force else "Graceful terminate")
        divider()

        if _terminate_process(pid, force):
            _remove_pid()
            section("Stop", "green")
            kv("State", "[bold green]● STOPPED[/bold green]")
            divider()
        else:
            _remove_pid()
            section("Stop", "red")
            kv("State", "[bold red]● FAILED[/bold red]")
            console.print("  [red]Gateway did not stop gracefully. Use --force to kill it.[/red]")
            divider()
            raise typer.Exit(1)
    else:
        if pid:
            _remove_pid()
        section("Stop", "yellow")
        console.print("  [yellow]○ Gateway was not running[/yellow]")
        divider()

    _start_daemon(port, workspace, config, verbose)


@gateway_app.command("status")
def gateway_status():
    """Check the status of the MarkBot gateway service."""
    import json
    import platform
    from datetime import datetime

    from markbot.config.loader import load_config

    _markbot_banner()

    config = load_config()
    workspace = config.workspace_path

    # ── Gather all data ─────────────────────────────────────────────────────
    pid = _read_pid()
    running = False
    proc = None
    if pid:
        running = _is_process_running(pid)
        if running:
            try:
                import psutil
                proc = psutil.Process(pid)
            except Exception:
                proc = None

    _has_psutil = False
    cpu_cores = cpu_logical = cpu_pct = 0
    mem_used_gb = mem_total_gb = mem_pct = 0
    try:
        import psutil as _ps
        _has_psutil = True
        cpu_cores = _ps.cpu_count(logical=False) or 0
        cpu_logical = _ps.cpu_count(logical=True) or 0
        cpu_pct = _ps.cpu_percent(interval=0.1)
        mem = _ps.virtual_memory()
        mem_total_gb = mem.total / (1024**3)
        mem_used_gb = mem.used / (1024**3)
        mem_pct = mem.percent
    except Exception:
        pass

    enabled_channels = []
    channels_config = config.channels
    if channels_config:
        for ch in ["feishu", "email", "dingtalk", "weixin", "qq"]:
            sec = getattr(channels_config, ch, None)
            if sec is None:
                continue
            if isinstance(sec, dict) and sec.get("enabled"):
                enabled_channels.append(ch)
            elif hasattr(sec, "enabled") and getattr(sec, "enabled"):
                enabled_channels.append(ch)

    cron_jobs = active_jobs = 0
    cron_path = get_cron_dir(workspace) / "jobs.json"
    if cron_path.exists():
        try:
            data = json.loads(cron_path.read_text())
            jobs = data.get("jobs", [])
            cron_jobs = len(jobs)
            active_jobs = sum(1 for j in jobs if j.get("enabled", True))
        except Exception:
            pass

    log_exists = GATEWAY_LOG_FILE.exists()
    log_size_kb = 0.0
    log_mtime = ""
    if log_exists:
        log_size_kb = GATEWAY_LOG_FILE.stat().st_size / 1024
        log_mtime = datetime.fromtimestamp(GATEWAY_LOG_FILE.stat().st_mtime).strftime("%Y-%m-%d %H:%M")

    mcp_count = len(config.tools.mcp_servers) if config.tools.mcp_servers else 0

    # ─ Skills ──────────────────────────────────────────────────────────────
    from markbot.skills.core.loader import SkillLoader
    skill_loader = SkillLoader(workspace)
    all_skills = skill_loader.load_all()
    skill_count = len(all_skills)

    def dir_info(path: Path, pattern="*") -> str:
        if not path.exists():
            return "[red]✗[/red] missing"
        count = len(list(path.glob(pattern)))
        size_kb = sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1024
        return f"[green]✓[/green] {count} files ({size_kb:.0f} KB)"

    ws_raw = str(workspace)
    if ws_raw.startswith("/home/marktang/"):
        ws_str = "~/" + ws_raw[len("/home/marktang/"):]
    else:
        ws_str = ws_raw

    # ── Render ─────────────────────────────────────────────────────────────
    console.print()

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

    # ─ Process ──────────────────────────────────────────────────────────────
    section("Process", "green" if running else "red")
    if running and proc:
        try:
            uptime = datetime.now() - datetime.fromtimestamp(proc.create_time())
            days, r = divmod(uptime.seconds, 3600)
            hours, minutes = divmod(r, 60)
            uptime_str = f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"
            mem_mb = proc.memory_info().rss / 1024 / 1024
            cpu_pct_val = proc.cpu_percent(interval=0.1)
            threads = proc.num_threads()
            # line = Text.from_markup(f"  [bold green]●  RUNNING[/bold green]   [dim]PID {pid}  |  Uptime {uptime_str}  |  RAM {mem_mb:.0f} MB  |  CPU {cpu_pct_val:.0f}%  |  {threads} threads[/dim]")
            line = Text.from_markup(f"  [bold green]●  RUNNING[/bold green]   [dim]PID {pid}  |  Uptime {uptime_str}[/dim]")
        except Exception:
            line = Text.from_markup(f"  [bold green]●  RUNNING[/bold green]   [dim]PID {pid}  |  (psutil error)[/dim]")
    elif running:
        line = Text.from_markup(f"  [bold green]●  RUNNING[/bold green]   [dim]PID {pid}  |  (psutil not installed)[/dim]")
    else:
        line = Text.from_markup("  [bold red]●  STOPPED[/bold red]   [dim]Gateway is not running[/dim]")
    console.print(line)

    divider()

    # ─ System ────────────────────────────────────────────────────────────────
    section("System", "cyan")
    kv("OS", f"{platform.system()} {platform.release()}")
    kv("Python", platform.python_version())
    kv("Hostname", platform.node())
    if _has_psutil:
        kv("CPU", f"{cpu_cores} cores / {cpu_logical} logical  |  {cpu_pct:.0f}% used")
        kv("RAM", f"{mem_used_gb:.1f} / {mem_total_gb:.1f} GB  |  {mem_pct:.0f}% used")
    else:
        console.print("  [dim]CPU & RAM metrics unavailable  (run: pip install psutil)[/dim]")

    divider()

    # ─ Workspace ─────────────────────────────────────────────────────────────
    section("Workspace", "blue")
    kv("Path", ws_str)
    kv("Memory", dir_info(workspace / "memory"))
    kv("Sessions", dir_info(workspace / "sessions", "*.jsonl"))
    kv("Cron", dir_info(get_cron_dir(workspace), "*.json"))

    divider()

    # ─ Runtime ───────────────────────────────────────────────────────────────
    section("Runtime", "magenta")
    kv("Model Chain", ", ".join(config.agents.defaults.model_chain) or "[not configured]")
    kv("Context Window", f"{config.agents.defaults.context_window_tokens} tokens")
    hb_on = config.gateway.heartbeat.enabled
    kv("Heartbeat", f"[green]●[/green] Enabled every {config.gateway.heartbeat.interval_s}s" if hb_on else "[yellow]○[/yellow] Disabled")
    kv("Channels", f"{len(enabled_channels)} enabled  ({', '.join(enabled_channels) or 'none'})")
    kv("Cron Jobs", f"[yellow]{active_jobs}[/yellow] / {cron_jobs} active")

    divider()

    # ─ Log File ──────────────────────────────────────────────────────────
    section("Log File", "yellow" if log_exists else "dim")
    if log_exists:
        kv("Path", str(GATEWAY_LOG_FILE))
        kv("Size", f"{log_size_kb:.1f} KB")
        kv("Modified", log_mtime)
    else:
        console.print(Text.from_markup("  [dim]No log file found[/dim]"))

    divider()

    # ─ Servers & Skills ────────────────────────────────────────────────────
    section("Servers & Skills", "cyan")
    kv("MCP Servers", str(mcp_count))
    kv("Skills", str(skill_count))

    divider()
    console.print()
    # if running:
    #     console.print(Text.from_markup(f"  [bold green]✓ Gateway is running[/bold green]   [dim](stop: markbot gateway stop)[/dim]"))
    # else:
    #     console.print(Text.from_markup(f"  [bold yellow]⚠ Gateway is stopped[/bold yellow]   [dim](start: markbot gateway start)[/dim]"))


# ============================================================================
# Channel Commands
# ============================================================================

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
    kv("Model Chain", ", ".join(config.agents.defaults.model_chain) or "[not configured]")

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
