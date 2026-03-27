"""CLI commands for markbot."""

import asyncio
from contextlib import contextmanager, nullcontext

import os
import select
import signal
import sys
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
from rich.table import Table
from rich.text import Text

from markbot import __logo__, __version__
from markbot.cli.stream import StreamRenderer, ThinkingSpinner
from markbot.cli.skills import app as skills_app
from markbot.config.paths import get_cron_dir, get_workspace_path, is_default_workspace
from markbot.config.schema import Config
from markbot.utils.helpers import sync_workspace_templates

app = typer.Typer(
    name="markbot",
    context_settings={"help_option_names": ["-h", "--help"]},
    help=f"{__logo__} NarkBot - Personal AI Assistant",
    no_args_is_help=True,
)

# Add skill management subcommands
app.add_typer(skills_app, name="skills")

console = Console()
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}

# ---------------------------------------------------------------------------
# CLI input: prompt_toolkit for editing, paste, history, and display
# ---------------------------------------------------------------------------

_PROMPT_SESSION: PromptSession | None = None
_SAVED_TERM_ATTRS = None  # original termios settings, restored on exit


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
    console.print(f"[cyan]{__logo__} NarkBot[/cyan]")
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
                c.print(f"[cyan]{__logo__} NarkBot[/cyan]"),
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
                HTML("<b fg='ansiblue'>></b> "),
            )
    except EOFError as exc:
        raise KeyboardInterrupt from exc



def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} NarkBot v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
):
    """NarkBot - Personal AI Assistant."""
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================


@app.command()
def onboard(
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    wizard: bool = typer.Option(False, "--wizard", help="Use interactive wizard"),
):
    """Initialize NarkBot configuration and workspace."""
    from markbot.config.loader import get_config_path, load_config, save_config, set_config_path
    from markbot.config.schema import Config

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

    # Create or update config
    if config_path.exists():
        if wizard:
            config = _apply_workspace_override(load_config(config_path))
        else:
            console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
            console.print("  [bold]y[/bold] = overwrite with defaults (existing values will be lost)")
            console.print("  [bold]N[/bold] = refresh config, keeping existing values and adding new fields")
            if typer.confirm("Overwrite?"):
                config = _apply_workspace_override(Config())
                save_config(config, config_path)
                console.print(f"[green]✓[/green] Config reset to defaults at {config_path}")
            else:
                config = _apply_workspace_override(load_config(config_path))
                save_config(config, config_path)
                console.print(f"[green]✓[/green] Config refreshed at {config_path} (existing values preserved)")
    else:
        config = _apply_workspace_override(Config())
        # In wizard mode, don't save yet - the wizard will handle saving if should_save=True
        if not wizard:
            save_config(config, config_path)
            console.print(f"[green]✓[/green] Created config at {config_path}")

    # Run interactive wizard if enabled
    if wizard:
        from markbot.cli.onboard import run_onboard

        try:
            result = run_onboard(initial_config=config)
            if not result.should_save:
                console.print("[yellow]Configuration discarded. No changes were saved.[/yellow]")
                return

            config = result.config
            save_config(config, config_path)
            console.print(f"[green]✓[/green] Config saved at {config_path}")
        except Exception as e:
            console.print(f"[red]✗[/red] Error during configuration: {e}")
            console.print("[yellow]Please run 'markbot onboard' again to complete setup.[/yellow]")
            raise typer.Exit(1)
    _onboard_plugins(config_path)

    # Create workspace, preferring the configured workspace path.
    workspace_path = get_workspace_path(config.workspace_path)
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
    if wizard:
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

    from markbot.channels.registry import discover_all

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
    """Create the appropriate LLM provider from config.

    Routing is driven by ``ProviderSpec.backend`` in the registry.
    """
    from markbot.providers.base import GenerationSettings
    from markbot.providers.registry import find_by_name

    model = config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    p = config.get_provider(model)
    spec = find_by_name(provider_name) if provider_name else None
    backend = spec.backend if spec else "openai_compat"

    # --- validation ---
    if backend == "azure_openai":
        if not p or not p.api_key or not p.api_base:
            console.print("[red]Error: Azure OpenAI requires api_key and api_base.[/red]")
            console.print("Set them in ~/.markbot/config.json under providers.azure_openai section")
            console.print("Use the model field to specify the deployment name.")
            raise typer.Exit(1)
    elif backend == "openai_compat" and not model.startswith("bedrock/"):
        needs_key = not (p and p.api_key)
        exempt = spec and (spec.is_oauth or spec.is_local or spec.is_direct)
        if needs_key and not exempt:
            console.print("[red]Error: No API key configured.[/red]")
            console.print("Set one in ~/.markbot/config.json under providers section")
            raise typer.Exit(1)

    # --- instantiation by backend ---
    if backend == "openai_codex":
        from markbot.providers.openai_codex_provider import OpenAICodexProvider
        provider = OpenAICodexProvider(default_model=model)
    elif backend == "azure_openai":
        from markbot.providers.azure_openai_provider import AzureOpenAIProvider
        provider = AzureOpenAIProvider(
            api_key=p.api_key,
            api_base=p.api_base,
            default_model=model,
        )
    elif backend == "anthropic":
        from markbot.providers.anthropic_provider import AnthropicProvider
        provider = AnthropicProvider(
            api_key=p.api_key if p else None,
            api_base=config.get_api_base(model),
            default_model=model,
            extra_headers=p.extra_headers if p else None,
        )
    else:
        from markbot.providers.openai_compat_provider import OpenAICompatProvider
        provider = OpenAICompatProvider(
            api_key=p.api_key if p else None,
            api_base=config.get_api_base(model),
            default_model=model,
            extra_headers=p.extra_headers if p else None,
            spec=spec,
        )

    defaults = config.agents.defaults
    provider.generation = GenerationSettings(
        temperature=defaults.temperature,
        max_tokens=defaults.max_tokens,
        reasoning_effort=defaults.reasoning_effort,
    )
    return provider


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


@gateway_app.command("start")
def gateway_start(
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    daemon: bool = typer.Option(True, "--daemon/--foreground", "-d", help="Run as daemon (background)"),
):
    """Start the markbot gateway service."""
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
    _ensure_pid_dir()
    
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
        time.sleep(0.5)
        
        if process.poll() is not None:
            console.print("[red]Failed to start gateway daemon.[/red]")
            raise typer.Exit(1)
        
        _write_pid(process.pid)
        console.print(f"[green]✓[/green] Gateway started in background (PID: {process.pid})")
        
    else:
        # Linux/Unix: use os.fork()
        pid = os.fork()
        if pid > 0:
            _write_pid(pid)
            console.print(f"[green]✓[/green] Gateway started in background (PID: {pid})")
            console.print(f"  Log file: {GATEWAY_LOG_FILE}")
            console.print(f"  Use [cyan]markbot gateway status[/cyan] to check status.")
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
    from loguru import logger
    from markbot.agent.loop import AgentLoop
    from markbot.bus.queue import MessageBus
    from markbot.channels.manager import ChannelManager
    from markbot.config.loader import load_config
    from markbot.cron.service import CronService
    from markbot.cron.types import CronJob
    from markbot.heartbeat.service import HeartbeatService
    from markbot.session.manager import SessionManager

    # Configure logging
    import logging
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
    
    logger.add(
        sys.stderr,
        level=log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        colorize=True,
        backtrace=True,
        diagnose=True,
        catch=True,
        filter=log_filter,
    )
    
    logger.add(
        GATEWAY_LOG_FILE,
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
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
    import traceback
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
    
    console.print(f"{__logo__} Starting NarkBot gateway on port {port}...")
    
    sync_workspace_templates(config.workspace_path)
    bus = MessageBus()
    provider = _make_provider(config)
    session_manager = SessionManager(config.workspace_path)
    
    cron_store_path = get_cron_dir(config.workspace_path) / "jobs.json"
    cron = CronService(cron_store_path)
    
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        context_window_tokens=config.agents.defaults.context_window_tokens,
        web_search_config=config.tools.web.search,
        web_proxy=config.tools.web.proxy or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=session_manager,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        timezone=config.agents.defaults.timezone,
    )
    
    async def on_cron_job(job: CronJob) -> str | None:
        from markbot.agent.tools.cron import CronTool
        from markbot.agent.tools.message import MessageTool
        from markbot.utils.evaluator import evaluate_response
        
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
        provider=provider,
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
            await agent.close_mcp()
            heartbeat.stop()
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
    logs: bool = typer.Option(False, "--logs/--no-logs", help="Show NarkBot runtime logs during chat"),
):
    """Interact with the agent directly."""
    from loguru import logger

    from markbot.agent.loop import AgentLoop
    from markbot.bus.queue import MessageBus
    from markbot.cron.service import CronService

    config = _load_runtime_config(config, workspace)
    sync_workspace_templates(config.workspace_path)
    
    # Setup file logging - always log to file for debugging
    #log_file = config.workspace_path / "markbot.log"
    #from loguru import logger
    #logger.add(
    #    str(log_file),
    #    rotation="10 MB",
    #    retention="7 days",
    #    level="DEBUG",
    #    encoding="utf-8",
    #    filter=lambda record: record["name"].startswith("markbot"),
    #)
    #print(f"[日志] 详细日志已保存到: {log_file}")

    bus = MessageBus()
    provider = _make_provider(config)

    cron_store_path = get_cron_dir(config.workspace_path) / "jobs.json"
    cron = CronService(cron_store_path)

    if logs:
        logger.enable("markbot")
    else:
        logger.disable("markbot")

    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        context_window_tokens=config.agents.defaults.context_window_tokens,
        web_search_config=config.tools.web.search,
        web_proxy=config.tools.web.proxy or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        timezone=config.agents.defaults.timezone,
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
        console.print(f"{__logo__} Interactive mode (type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit)\n")

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
    """Stop the NarkBot gateway service."""
    import time
    
    pid = _read_pid()
    if not pid:
        console.print("[yellow]Gateway is not running (no PID file found)[/yellow]")
        raise typer.Exit(0)

    if not _is_process_running(pid):
        _remove_pid()
        console.print("[yellow]Gateway is not running (stale PID file removed)[/yellow]")
        raise typer.Exit(0)

    # Terminate the process
    try:
        if sys.platform == "win32":
            # Windows
            import ctypes
            from ctypes import wintypes
            
            kernel32 = ctypes.windll.kernel32
            PROCESS_TERMINATE = 0x0001
            handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
            
            if handle == 0:
                _remove_pid()
                console.print("[yellow]Gateway process not found (cleaned up PID file)[/yellow]")
                raise typer.Exit(0)
            
            exit_code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                if exit_code.value != 259:  # Not STILL_ACTIVE
                    _remove_pid()
                    kernel32.CloseHandle(handle)
                    console.print("[yellow]Gateway was already stopped.[/yellow]")
                    raise typer.Exit(0)
            
            kernel32.TerminateProcess(handle, 1 if force else 0)
            kernel32.CloseHandle(handle)
        else:
            # Linux/Unix
            import os
            import signal
            
            if force:
                os.kill(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGTERM)
        
        console.print(f"[green]✓[/green] Sent {'force kill' if force else 'terminate'} to gateway (PID: {pid})")

        for _ in range(10):
            time.sleep(0.5)
            if not _is_process_running(pid):
                _remove_pid()
                console.print("[green]✓[/green] Gateway stopped successfully.")
                raise typer.Exit(0)

        console.print("[yellow]Gateway did not stop gracefully. Use --force to kill it.[/yellow]")
        raise typer.Exit(1)
    except Exception as e:
        _remove_pid()
        console.print(f"[yellow]Gateway process error: {e} (cleaned up PID file)[/yellow]")
        raise typer.Exit(0)


@gateway_app.command("restart")
def gateway_restart(
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    force: bool = typer.Option(False, "--force", "-f", help="Force kill before restart"),
):
    """Restart the NarkBot gateway service."""
    import time
    
    pid = _read_pid()

    if pid and _is_process_running(pid):
        console.print(f"[yellow]Stopping gateway (PID: {pid})...[/yellow]")
        try:
            if sys.platform == "win32":
                # Windows
                import ctypes
                from ctypes import wintypes
                
                kernel32 = ctypes.windll.kernel32
                PROCESS_TERMINATE = 0x0001
                handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
                
                if handle != 0:
                    kernel32.TerminateProcess(handle, 1 if force else 0)
                    kernel32.CloseHandle(handle)
            else:
                # Linux/Unix
                import os
                import signal
                
                if force:
                    os.kill(pid, signal.SIGKILL)
                else:
                    os.kill(pid, signal.SIGTERM)

            for _ in range(10):
                time.sleep(0.5)
                if not _is_process_running(pid):
                    _remove_pid()
                    console.print("[green]✓[/green] Gateway stopped.")
                    break
            else:
                console.print("[red]Gateway did not stop gracefully. Use --force to kill it.[/red]")
                raise typer.Exit(1)
        except Exception as e:
            _remove_pid()
            console.print(f"[yellow]Error stopping gateway: {e} (cleaned up)[/yellow]")
    else:
        if pid:
            _remove_pid()
        console.print("[yellow]Gateway was not running.[/yellow]")

    console.print("[cyan]Starting gateway...[/cyan]")
    _start_daemon(port, workspace, config, verbose)


@gateway_app.command("status")
def gateway_status():
    """Check the status of the NarkBot gateway service."""
    from markbot.config.loader import load_config
    import json
    from datetime import datetime

    table = Table(title=f"\n{__logo__} NarkBot Gateway Status", title_justify="left")
    table.add_column("Component", style="cyan", width=20)
    table.add_column("Status", style="green", width=20)
    table.add_column("Details", width=50)

    pid = _read_pid()
    running = False
    uptime_str = ""

    if pid:
        running = _is_process_running(pid)
        if running:
            try:
                import psutil
                proc = psutil.Process(pid)
                create_time = datetime.fromtimestamp(proc.create_time())
                uptime = datetime.now() - create_time
                days = uptime.days
                hours, remainder = divmod(uptime.seconds, 3600)
                minutes, _ = divmod(remainder, 60)
                uptime_str = f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"
                mem_mb = proc.memory_info().rss / 1024 / 1024
                table.add_row("Process", "[green]● Running[/green]", f"PID: {pid}, Uptime: {uptime_str}, Memory: {mem_mb:.1f}MB", end_section=True)
            except ImportError:
                table.add_row("Process", "[green]● Running[/green]", f"PID: {pid}", end_section=True)
            except Exception:
                table.add_row("Process", "[green]● Running[/green]", f"PID: {pid}", end_section=True)
        else:
            table.add_row("Process", "[red]● Stopped[/red]", "Stale PID file", end_section=True)
            _remove_pid()
    else:
        table.add_row("Process", "[red]● Stopped[/red]", "No PID file", end_section=True)

    if GATEWAY_LOG_FILE.exists():
        log_size = GATEWAY_LOG_FILE.stat().st_size / 1024
        log_mtime = datetime.fromtimestamp(GATEWAY_LOG_FILE.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        table.add_row("Log File", f"{log_size:.1f}KB", f"Last modified: {log_mtime}")
    else:
        table.add_row("Log File", "[yellow]○ Not found[/yellow]", str(GATEWAY_LOG_FILE))

    config = load_config()
    workspace = config.workspace_path
    table.add_row("Workspace", "[green]✓[/green]" if workspace.exists() else "[red]✗[/red]", str(workspace), end_section=True)

    table.add_row("Model", config.agents.defaults.model, f"Context: {config.agents.defaults.context_window_tokens}")

    hb_cfg = config.gateway.heartbeat
    hb_status = "[green]● Enabled[/green]" if hb_cfg.enabled else "[yellow]○ Disabled[/yellow]"
    table.add_row("Heartbeat", hb_status, f"Interval: {hb_cfg.interval_s}s")

    # Count enabled channels
    enabled_channels = []
    channels_config = config.channels
    if channels_config:
        for channel_name in ["feishu", "email", "dingtalk"]:
            section = getattr(channels_config, channel_name, None)
            if section is not None:
                if isinstance(section, dict) and section.get("enabled"):
                    enabled_channels.append(channel_name)
                elif hasattr(section, "enabled") and getattr(section, "enabled"):
                    enabled_channels.append(channel_name)
    
    channels_str = ", ".join(enabled_channels) if enabled_channels else "None"
    table.add_row("Channels", str(len(enabled_channels)), channels_str, end_section=True)

    cron_store_path = get_cron_dir(workspace) / "jobs.json"
    cron_jobs = 0
    active_jobs = 0
    if cron_store_path.exists():
        try:
            data = json.loads(cron_store_path.read_text())
            jobs = data.get("jobs", [])
            cron_jobs = len(jobs)
            active_jobs = sum(1 for j in jobs if j.get("enabled", True))
        except Exception:
            pass
    table.add_row("Cron Jobs", f"{active_jobs}/{cron_jobs} active", str(cron_store_path))

    sessions_dir = workspace / "sessions"
    session_count = len(list(sessions_dir.glob("*.json"))) if sessions_dir.exists() else 0
    table.add_row("Sessions", str(session_count), str(sessions_dir))

    mcp_count = len(config.tools.mcp_servers) if config.tools.mcp_servers else 0
    mcp_names = ", ".join(config.tools.mcp_servers.keys()) if mcp_count else "None"
    table.add_row("MCP Servers", str(mcp_count), mcp_names)

    console.print(table)

    if running:
        console.print(f"\n[green]✓[/green] Gateway is running normally.")
    else:
        console.print(f"\n[yellow]Gateway is not running.[/yellow]")
        console.print("Use [cyan]markbot gateway start[/cyan] to start it.")


# ============================================================================
# Channel Commands
# ============================================================================

channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status():
    """Show channel status."""
    from markbot.channels.registry import discover_all
    from markbot.config.loader import load_config

    config = load_config()

    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled", style="green")

    for name, cls in sorted(discover_all().items()):
        section = getattr(config.channels, name, None)
        if section is None:
            enabled = False
        elif isinstance(section, dict):
            enabled = section.get("enabled", False)
        else:
            enabled = getattr(section, "enabled", False)
        table.add_row(
            cls.display_name,
            "[green]\u2713[/green]" if enabled else "[dim]\u2717[/dim]",
        )

    console.print(table)


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
    force: bool = typer.Option(False, "--force", "-f", help="Force re-authentication even if already logged in"),
):
    """Authenticate with a channel via QR code or other interactive login."""
    from markbot.channels.registry import discover_all
    from markbot.config.loader import load_config

    config = load_config()
    channel_cfg = getattr(config.channels, channel_name, None) or {}

    # Validate channel exists
    all_channels = discover_all()
    if channel_name not in all_channels:
        available = ", ".join(all_channels.keys())
        console.print(f"[red]Unknown channel: {channel_name}[/red]  Available: {available}")
        raise typer.Exit(1)

    console.print(f"{__logo__} {all_channels[channel_name].display_name} Login\n")

    channel_cls = all_channels[channel_name]
    channel = channel_cls(channel_cfg, bus=None)

    success = asyncio.run(channel.login(force=force))

    if not success:
        raise typer.Exit(1)


# ============================================================================
# Plugin Commands
# ============================================================================

plugins_app = typer.Typer(help="Manage channel plugins")
app.add_typer(plugins_app, name="plugins")


@plugins_app.command("list")
def plugins_list():
    """List all discovered channels (built-in and plugins)."""
    from markbot.channels.registry import discover_all, discover_channel_names
    from markbot.config.loader import load_config

    config = load_config()
    builtin_names = set(discover_channel_names())
    all_channels = discover_all()

    table = Table(title="Channel Plugins")
    table.add_column("Name", style="cyan")
    table.add_column("Source", style="magenta")
    table.add_column("Enabled", style="green")

    for name in sorted(all_channels):
        cls = all_channels[name]
        source = "builtin" if name in builtin_names else "plugin"
        section = getattr(config.channels, name, None)
        if section is None:
            enabled = False
        elif isinstance(section, dict):
            enabled = section.get("enabled", False)
        else:
            enabled = getattr(section, "enabled", False)
        table.add_row(
            cls.display_name,
            source,
            "[green]yes[/green]" if enabled else "[dim]no[/dim]",
        )

    console.print(table)


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status():
    """Show NarkBot status."""
    from markbot.config.loader import get_config_path, load_config

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{__logo__} NarkBot Status\n")

    console.print(f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
    console.print(f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}")

    if config_path.exists():
        from markbot.providers.registry import PROVIDERS

        console.print(f"Model: {config.agents.defaults.model}")

        # Check API keys from registry
        for spec in PROVIDERS:
            p = getattr(config.providers, spec.name, None)
            if p is None:
                continue
            if spec.is_oauth:
                console.print(f"{spec.label}: [green]✓ (OAuth)[/green]")
            elif spec.is_local:
                # Local deployments show api_base instead of api_key
                if p.api_base:
                    console.print(f"{spec.label}: [green]✓ {p.api_base}[/green]")
                else:
                    console.print(f"{spec.label}: [dim]not set[/dim]")
            else:
                has_key = bool(p.api_key)
                console.print(f"{spec.label}: {'[green]✓[/green]' if has_key else '[dim]not set[/dim]'}")


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
    from markbot.providers.registry import PROVIDERS

    key = provider.replace("-", "_")
    spec = next((s for s in PROVIDERS if s.name == key and s.is_oauth), None)
    if not spec:
        names = ", ".join(s.name.replace("_", "-") for s in PROVIDERS if s.is_oauth)
        console.print(f"[red]Unknown OAuth provider: {provider}[/red]  Supported: {names}")
        raise typer.Exit(1)

    handler = _LOGIN_HANDLERS.get(spec.name)
    if not handler:
        console.print(f"[red]Login not implemented for {spec.label}[/red]")
        raise typer.Exit(1)

    console.print(f"{__logo__} OAuth Login - {spec.label}\n")
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
    help="Manage NarkBot configuration (get/set values).",
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
    key: str = typer.Argument(..., help="Configuration key (dot notation, e.g., agents.defaults.model)"),
    raw: bool = typer.Option(False, "--raw", "-r", help="Output raw value without formatting"),
):
    """Get a configuration value."""
    from markbot.config.loader import load_config

    config = load_config()
    data = config.model_dump(by_alias=True)
    value = _get_nested_value(data, key)

    if value is None:
        console.print(f"[yellow]Key '{key}' not found or is null[/yellow]")
        raise typer.Exit(1)

    if raw:
        if isinstance(value, (dict, list)):
            import json
            print(json.dumps(value, indent=2, ensure_ascii=False))
        else:
            print(value)
    else:
        if isinstance(value, (dict, list)):
            import json
            console.print(json.dumps(value, indent=2, ensure_ascii=False))
        else:
            console.print(f"[green]{key}[/green] = [cyan]{value}[/cyan]")


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Configuration key (dot notation, e.g., agents.defaults.model)"),
    value: str = typer.Argument(..., help="Value to set (auto-detected type: string, number, boolean, null)"),
):
    """Set a configuration value."""
    import json
    from markbot.config.loader import load_config, save_config
    from markbot.config.schema import Config

    config = load_config()
    data = config.model_dump(by_alias=True)

    if _get_nested_value(data, key) is None:
        console.print(f"[yellow]Warning: Key '{key}' does not exist in config schema[/yellow]")

    parsed_value = _parse_value(value)
    if not _set_nested_value(data, key, parsed_value):
        console.print(f"[red]Error: Failed to set '{key}'. Check if the path is valid.[/red]")
        raise typer.Exit(1)

    try:
        new_config = Config.model_validate(data)
    except Exception as e:
        console.print(f"[red]Error: Invalid value for '{key}': {e}[/red]")
        raise typer.Exit(1)

    save_config(new_config)

    if isinstance(parsed_value, (dict, list)):
        console.print(f"[green]✓[/green] Set {key} = {json.dumps(parsed_value, ensure_ascii=False)}")
    else:
        console.print(f"[green]✓[/green] Set {key} = [cyan]{parsed_value}[/cyan]")


@config_app.command("list")
def config_list(
    prefix: str = typer.Option("", "--prefix", "-p", help="Filter keys by prefix"),
):
    """List all configuration keys and values."""
    import json
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
        console.print(f"[yellow]No keys found with prefix '{prefix}'[/yellow]")
        return

    table = Table(title=f"\n{__logo__} NarkBot Configuration", title_justify="left")
    table.add_column("Key", style="cyan")
    table.add_column("Value", style="green")

    for key, value in sorted(items):
        if isinstance(value, (dict, list)):
            value_str = json.dumps(value, ensure_ascii=False)
            if len(value_str) > 50:
                value_str = value_str[:47] + "..."
        else:
            value_str = str(value)
            if len(value_str) > 50:
                value_str = value_str[:47] + "..."
        table.add_row(key, value_str)

    console.print(table)


app.add_typer(config_app, name="config")


if __name__ == "__main__":
    app()
