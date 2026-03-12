"""CLI commands for markbot."""

import asyncio
import os
import select
import signal
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
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from markbot import __logo__, __version__
from markbot.config.paths import get_workspace_path
from markbot.config.schema import Config
from markbot.utils.helpers import sync_workspace_templates

app = typer.Typer(
    name="markbot",
    help=f"{__logo__} MarkBot - Personal AI Assistant",
    no_args_is_help=True,
)

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


def _print_agent_response(response: str, render_markdown: bool) -> None:
    """Render assistant response with consistent terminal styling."""
    content = response or ""
    body = Markdown(content) if render_markdown else Text(content)
    console.print()
    console.print(f"[cyan]{__logo__} MarkBot[/cyan]")
    console.print(body)
    console.print()


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
def onboard():
    """Initialize MarkBot configuration and workspace."""
    from markbot.config.loader import get_config_path, load_config, save_config
    from markbot.config.schema import Config

    config_path = get_config_path()

    if config_path.exists():
        console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
        console.print("  [bold]y[/bold] = overwrite with defaults (existing values will be lost)")
        console.print("  [bold]N[/bold] = refresh config, keeping existing values and adding new fields")
        if typer.confirm("Overwrite?"):
            config = Config()
            save_config(config)
            console.print(f"[green]✓[/green] Config reset to defaults at {config_path}")
        else:
            config = load_config()
            save_config(config)
            console.print(f"[green]✓[/green] Config refreshed at {config_path} (existing values preserved)")
    else:
        save_config(Config())
        console.print(f"[green]✓[/green] Created config at {config_path}")

    # Create workspace
    workspace = get_workspace_path()

    if not workspace.exists():
        workspace.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]✓[/green] Created workspace at {workspace}")

    sync_workspace_templates(workspace)

    console.print(f"\n{__logo__} MarkBot is ready!")
    console.print("\nNext steps:")
    console.print("  1. Add your API key to [cyan]~/.markbot/config.json[/cyan]")
    console.print("  2. Chat: [cyan]markbot agent -m \"Hello!\"[/cyan]")





def _make_provider(config: Config):
    """Create the appropriate LLM provider from config."""
    from markbot.providers.openai_codex_provider import OpenAICodexProvider
    from markbot.providers.azure_openai_provider import AzureOpenAIProvider

    model = config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    p = config.get_provider(model)

    # OpenAI Codex (OAuth)
    if provider_name == "openai_codex" or model.startswith("openai-codex/"):
        return OpenAICodexProvider(default_model=model)

    # Custom: direct OpenAI-compatible endpoint, bypasses LiteLLM
    from markbot.providers.custom_provider import CustomProvider
    if provider_name == "custom":
        return CustomProvider(
            api_key=p.api_key if p else "no-key",
            api_base=config.get_api_base(model) or "http://localhost:8000/v1",
            default_model=model,
        )

    # Azure OpenAI: direct Azure OpenAI endpoint with deployment name
    if provider_name == "azure_openai":
        if not p or not p.api_key or not p.api_base:
            console.print("[red]Error: Azure OpenAI requires api_key and api_base.[/red]")
            console.print("Set them in ~/.markbot/config.json under providers.azure_openai section")
            console.print("Use the model field to specify the deployment name.")
            raise typer.Exit(1)
        
        return AzureOpenAIProvider(
            api_key=p.api_key,
            api_base=p.api_base,
            default_model=model,
        )

    from markbot.providers.litellm_provider import LiteLLMProvider
    from markbot.providers.registry import find_by_name
    spec = find_by_name(provider_name)
    if not model.startswith("bedrock/") and not (p and p.api_key) and not (spec and spec.is_oauth):
        console.print("[red]Error: No API key configured.[/red]")
        console.print("Set one in ~/.markbot/config.json under providers section")
        raise typer.Exit(1)

    return LiteLLMProvider(
        api_key=p.api_key if p else None,
        api_base=config.get_api_base(model),
        default_model=model,
        extra_headers=p.extra_headers if p else None,
        provider_name=provider_name,
    )


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
    if workspace:
        loaded.agents.defaults.workspace = workspace
    return loaded


# ============================================================================
# Gateway / Server
# ============================================================================


@app.command()
def gateway(
    port: int | None = typer.Option(None, "--port", "-p", help="Gateway port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Start the markbot gateway."""
    from markbot.agent.loop import AgentLoop
    from markbot.bus.queue import MessageBus
    from markbot.channels.manager import ChannelManager
    from markbot.config.paths import get_cron_dir
    from markbot.cron.service import CronService
    from markbot.cron.types import CronJob
    from markbot.heartbeat.service import HeartbeatService
    from markbot.session.manager import SessionManager

    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    config = _load_runtime_config(config, workspace)
    port = port if port is not None else config.gateway.port

    console.print(f"{__logo__} Starting MarkBot gateway on port {port}...")
    sync_workspace_templates(config.workspace_path)
    bus = MessageBus()
    provider = _make_provider(config)
    session_manager = SessionManager(config.workspace_path)

    # Create cron service first (callback set after agent creation)
    cron_store_path = get_cron_dir() / "jobs.json"
    cron = CronService(cron_store_path)

    # Create agent with cron service
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        temperature=config.agents.defaults.temperature,
        max_tokens=config.agents.defaults.max_tokens,
        max_iterations=config.agents.defaults.max_tool_iterations,
        memory_window=config.agents.defaults.memory_window,
        reasoning_effort=config.agents.defaults.reasoning_effort,
        web_proxy=config.tools.web.proxy or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=session_manager,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
    )

    # Set cron callback (needs agent)
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        from markbot.agent.tools.cron import CronTool
        from markbot.agent.tools.message import MessageTool
        reminder_note = (
            "[Scheduled Task] Timer finished.\n\n"
            f"Task '{job.name}' has been triggered.\n"
            f"Scheduled instruction: {job.payload.message}"
        )

        # Prevent the agent from scheduling new cron jobs during execution
        cron_tool = agent.tools.get("cron")
        cron_token = None
        if isinstance(cron_tool, CronTool):
            cron_token = cron_tool.set_cron_context(True)
        try:
            response = await agent.process_direct(
                reminder_note,
                session_key=f"cron:{job.id}",
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to or "direct",
            )
        finally:
            if isinstance(cron_tool, CronTool) and cron_token is not None:
                cron_tool.reset_cron_context(cron_token)

        message_tool = agent.tools.get("message")
        if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
            return response

        if job.payload.deliver and job.payload.to and response:
            from markbot.bus.events import OutboundMessage
            await bus.publish_outbound(OutboundMessage(
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to,
                content=response
            ))
        return response
    cron.on_job = on_cron_job

    # Create channel manager
    channels = ChannelManager(config, bus)

    def _pick_heartbeat_target() -> tuple[str, str]:
        """Pick a routable channel/chat target for heartbeat-triggered messages."""
        enabled = set(channels.enabled_channels)
        # Prefer the most recently updated non-internal session on an enabled channel.
        for item in session_manager.list_sessions():
            key = item.get("key") or ""
            if ":" not in key:
                continue
            channel, chat_id = key.split(":", 1)
            if channel in {"cli", "system"}:
                continue
            if channel in enabled and chat_id:
                return channel, chat_id
        # Fallback keeps prior behavior but remains explicit.
        return "cli", "direct"

    # Create heartbeat service
    async def on_heartbeat_execute(tasks: str) -> str:
        """Phase 2: execute heartbeat tasks through the full agent loop."""
        channel, chat_id = _pick_heartbeat_target()

        async def _silent(*_args, **_kwargs):
            pass

        return await agent.process_direct(
            tasks,
            session_key="heartbeat",
            channel=channel,
            chat_id=chat_id,
            on_progress=_silent,
        )

    async def on_heartbeat_notify(response: str) -> None:
        """Deliver a heartbeat response to the user's channel."""
        from markbot.bus.events import OutboundMessage
        channel, chat_id = _pick_heartbeat_target()
        if channel == "cli":
            return  # No external channel available to deliver to
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
    logs: bool = typer.Option(False, "--logs/--no-logs", help="Show MarkBot runtime logs during chat"),
):
    """Interact with the agent directly."""
    from loguru import logger

    from markbot.agent.loop import AgentLoop
    from markbot.bus.queue import MessageBus
    from markbot.config.paths import get_cron_dir
    from markbot.cron.service import CronService

    config = _load_runtime_config(config, workspace)
    sync_workspace_templates(config.workspace_path)

    bus = MessageBus()
    provider = _make_provider(config)

    # Create cron service for tool usage (no callback needed for CLI unless running)
    cron_store_path = get_cron_dir() / "jobs.json"
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
        temperature=config.agents.defaults.temperature,
        max_tokens=config.agents.defaults.max_tokens,
        max_iterations=config.agents.defaults.max_tool_iterations,
        memory_window=config.agents.defaults.memory_window,
        reasoning_effort=config.agents.defaults.reasoning_effort,
        web_proxy=config.tools.web.proxy or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
    )

    # Show spinner when logs are off (no output to miss); skip when logs are on
    def _thinking_ctx():
        if logs:
            from contextlib import nullcontext
            return nullcontext()
        # Animated spinner is safe to use with prompt_toolkit input handling
        return console.status("[dim]Thinking...[/dim]", spinner="dots")

    async def _cli_progress(content: str, *, tool_hint: bool = False) -> None:
        ch = agent_loop.channels_config
        if ch and tool_hint and not ch.send_tool_hints:
            return
        if ch and not tool_hint and not ch.send_progress:
            return
        console.print(f"  [dim]↳ {content}[/dim]")

    if message:
        # Single message mode — direct call, no bus needed
        async def run_once():
            with _thinking_ctx():
                response = await agent_loop.process_direct(message, session_id, on_progress=_cli_progress)
            _print_agent_response(response, render_markdown=markdown)
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
            console.print(f"\nReceived {sig_name}, goodbye!")
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
            turn_response: list[str] = []

            async def _consume_outbound():
                while True:
                    try:
                        msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
                        if msg.metadata.get("_progress"):
                            is_tool_hint = msg.metadata.get("_tool_hint", False)
                            ch = agent_loop.channels_config
                            if ch and is_tool_hint and not ch.send_tool_hints:
                                pass
                            elif ch and not is_tool_hint and not ch.send_progress:
                                pass
                            else:
                                console.print(f"  [dim]↳ {msg.content}[/dim]")
                        elif not turn_done.is_set():
                            if msg.content:
                                turn_response.append(msg.content)
                            turn_done.set()
                        elif msg.content:
                            console.print()
                            _print_agent_response(msg.content, render_markdown=markdown)
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

                        await bus.publish_inbound(InboundMessage(
                            channel=cli_channel,
                            sender_id="user",
                            chat_id=cli_chat_id,
                            content=user_input,
                        ))

                        with _thinking_ctx():
                            await turn_done.wait()

                        if turn_response:
                            _print_agent_response(turn_response[0], render_markdown=markdown)
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


# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status():
    """Show channel status."""
    from markbot.config.loader import load_config

    config = load_config()

    table = Table(title="Channel Status", title_justify="left")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled", style="green")
    table.add_column("Configuration", style="yellow")

    # Feishu
    fs = config.channels.feishu
    fs_config = f"app_id: {fs.app_id[:10]}..." if fs.app_id else "[dim]not configured[/dim]"
    table.add_row(
        "Feishu",
        "✓" if fs.enabled else "✗",
        fs_config
    )

    # DingTalk
    dt = config.channels.dingtalk
    dt_config = f"client_id: {dt.client_id[:10]}..." if dt.client_id else "[dim]not configured[/dim]"
    table.add_row(
        "DingTalk",
        "✓" if dt.enabled else "✗",
        dt_config
    )

    # Email
    em = config.channels.email
    em_config = em.imap_host if em.imap_host else "[dim]not configured[/dim]"
    table.add_row(
        "Email",
        "✓" if em.enabled else "✗",
        em_config
    )

    console.print(table)


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status():
    """Show markbot status."""
    from markbot.config.loader import get_config_path, load_config

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{__logo__} MarkBot Status\n")

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

    console.print("[cyan]Starting GitHub Copilot device flow...[/cyan]\n")

    async def _trigger():
        from litellm import acompletion
        await acompletion(model="github_copilot/gpt-4o", messages=[{"role": "user", "content": "hi"}], max_tokens=1)

    try:
        asyncio.run(_trigger())
        console.print("[green]✓ Authenticated with GitHub Copilot[/green]")
    except Exception as e:
        console.print(f"[red]Authentication error: {e}[/red]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()


# ============================================================================
# Gateway / Server
# ============================================================================

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
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


gateway_app = typer.Typer(
    name="gateway",
    help="Manage the MarkBot gateway service (start/stop/restart/status).",
)


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
        console.print("Use [cyan]MarkBot gateway stop[/cyan] to stop it first.")
        raise typer.Exit(1)

    if daemon:
        _start_daemon(port, workspace, config, verbose)
    else:
        _run_gateway_foreground(port, workspace, config, verbose)


def _start_daemon(port: int, workspace: str | None, config: str | None, verbose: bool) -> None:
    _ensure_pid_dir()
    pid = os.fork()
    if pid > 0:
        _write_pid(pid)
        console.print(f"[green]✓[/green] Gateway started in background (PID: {pid})")
        console.print(f"  Log file: {GATEWAY_LOG_FILE}")
        console.print(f"  Use [cyan]MarkBot gateway status[/cyan] to check status.")
        return

    os.setsid()
    sys.stdin.close()
    _ensure_pid_dir()
    with open(GATEWAY_LOG_FILE, "w") as log_file:
        os.dup2(log_file.fileno(), sys.stdout.fileno())
        os.dup2(log_file.fileno(), sys.stderr.fileno())
    try:
        _run_gateway_foreground(port, workspace, config, verbose)
    except Exception as e:
        print(f"Gateway failed: {e}", file=sys.stderr)
        sys.exit(1)


def _run_gateway_foreground(port: int, workspace: str | None, config: str | None, verbose: bool) -> None:
    from markbot.agent.loop import AgentLoop
    from markbot.bus.queue import MessageBus
    from markbot.channels.manager import ChannelManager
    from markbot.config.loader import load_config
    from markbot.cron.service import CronService
    from markbot.cron.types import CronJob
    from markbot.heartbeat.service import HeartbeatService
    from markbot.session.manager import SessionManager

    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    config_path = Path(config) if config else None
    config = load_config(config_path)
    if workspace:
        config.agents.defaults.workspace = workspace

    console.print(f"{__logo__} Starting MarkBot gateway on port {port}...")
    sync_workspace_templates(config.workspace_path)
    bus = MessageBus()
    provider = _make_provider(config)
    session_manager = SessionManager(config.workspace_path)

    cron_store_path = config.workspace_path / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        temperature=config.agents.defaults.temperature,
        max_tokens=config.agents.defaults.max_tokens,
        max_iterations=config.agents.defaults.max_tool_iterations,
        memory_window=config.agents.defaults.memory_window,
        reasoning_effort=config.agents.defaults.reasoning_effort,
        web_proxy=config.tools.web.proxy or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=session_manager,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
    )

    async def on_cron_job(job: CronJob) -> str | None:
        from markbot.agent.tools.cron import CronTool
        from markbot.agent.tools.message import MessageTool
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
            response = await agent.process_direct(
                reminder_note,
                session_key=f"cron:{job.id}",
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to or "direct",
            )
        finally:
            if isinstance(cron_tool, CronTool) and cron_token is not None:
                cron_tool.reset_cron_context(cron_token)

        message_tool = agent.tools.get("message")
        if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
            return response

        if job.payload.deliver and job.payload.to and response:
            from markbot.bus.events import OutboundMessage
            await bus.publish_outbound(OutboundMessage(
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to,
                content=response
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

        return await agent.process_direct(
            tasks,
            session_key="heartbeat",
            channel=channel,
            chat_id=chat_id,
            on_progress=_silent,
        )

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
        finally:
            await agent.close_mcp()
            heartbeat.stop()
            cron.stop()
            agent.stop()
            await channels.stop_all()

    asyncio.run(run())


@gateway_app.command("stop")
def gateway_stop(
    force: bool = typer.Option(False, "--force", "-f", help="Force kill the process"),
):
    """Stop the markbot gateway service."""
    pid = _read_pid()
    if not pid:
        console.print("[yellow]Gateway is not running (no PID file found)[/yellow]")
        raise typer.Exit(0)

    if not _is_process_running(pid):
        _remove_pid()
        console.print("[yellow]Gateway is not running (stale PID file removed)[/yellow]")
        raise typer.Exit(0)

    try:
        sig = signal.SIGKILL if force else signal.SIGTERM
        os.kill(pid, sig)
        console.print(f"[green]✓[/green] Sent {'SIGKILL' if force else 'SIGTERM'} to gateway (PID: {pid})")

        import time
        for _ in range(10):
            time.sleep(0.5)
            if not _is_process_running(pid):
                _remove_pid()
                console.print("[green]✓[/green] Gateway stopped successfully.")
                raise typer.Exit(0)

        console.print("[yellow]Gateway did not stop gracefully. Use --force to kill it.[/yellow]")
        raise typer.Exit(1)
    except ProcessLookupError:
        _remove_pid()
        console.print("[yellow]Gateway process not found (cleaned up PID file)[/yellow]")
        raise typer.Exit(0)


@gateway_app.command("restart")
def gateway_restart(
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    force: bool = typer.Option(False, "--force", "-f", help="Force kill before restart"),
):
    """Restart the markbot gateway service."""
    import time

    pid = _read_pid()

    if pid and _is_process_running(pid):
        console.print(f"[yellow]Stopping gateway (PID: {pid})...[/yellow]")
        try:
            sig = signal.SIGKILL if force else signal.SIGTERM
            os.kill(pid, sig)

            for _ in range(10):
                time.sleep(0.5)
                if not _is_process_running(pid):
                    _remove_pid()
                    console.print("[green]✓[/green] Gateway stopped.")
                    break
            else:
                console.print("[red]Gateway did not stop gracefully. Use --force to kill it.[/red]")
                raise typer.Exit(1)
        except ProcessLookupError:
            _remove_pid()
            console.print("[yellow]Gateway process not found (cleaned up)[/yellow]")
    else:
        if pid:
            _remove_pid()
        console.print("[yellow]Gateway was not running.[/yellow]")

    console.print("[cyan]Starting gateway...[/cyan]")
    _start_daemon(port, workspace, config, verbose)


@gateway_app.command("status")
def gateway_status():
    """Check the status of the markbot gateway service."""
    from markbot.config.loader import load_config
    import json
    from datetime import datetime

    table = Table(title=f"{__logo__} MarkBot Gateway Status", title_justify="left")
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
                table.add_row("Process", "[green]● Running[/green]", f"PID: {pid}, Uptime: {uptime_str}, Memory: {mem_mb:.1f}MB")
            except:
                table.add_row("Process", "[green]● Running[/green]", f"PID: {pid}")
        else:
            table.add_row("Process", "[red]● Stopped[/red]", "Stale PID file")
            _remove_pid()
    else:
        table.add_row("Process", "[red]● Stopped[/red]", "No PID file")

    if GATEWAY_LOG_FILE.exists():
        log_size = GATEWAY_LOG_FILE.stat().st_size / 1024
        log_mtime = datetime.fromtimestamp(GATEWAY_LOG_FILE.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        table.add_row("Log File", f"{log_size:.1f}KB", f"{GATEWAY_LOG_FILE}\nLast modified: {log_mtime}")
    else:
        table.add_row("Log File", "[yellow]Not found[/yellow]", str(GATEWAY_LOG_FILE))

    config = load_config()
    workspace = config.workspace_path
    table.add_row("Workspace", str(workspace.exists()), str(workspace))

    table.add_row("Model", config.agents.defaults.model, f"Temp: {config.agents.defaults.temperature}, Max tokens: {config.agents.defaults.max_tokens}")

    cron_store_path = workspace / "cron" / "jobs.json"
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

    hb_cfg = config.gateway.heartbeat
    hb_status = "[green]● Enabled[/green]" if hb_cfg.enabled else "[yellow]○ Disabled[/yellow]"
    table.add_row("Heartbeat", hb_status, f"Interval: {hb_cfg.interval_s}s")

    channels_config = config.channels
    enabled_channels = []
    if channels_config:
        if channels_config.feishu and channels_config.feishu.enabled:
            enabled_channels.append("feishu")
        if channels_config.email and channels_config.email.enabled:
            enabled_channels.append("email")
    channels_str = ", ".join(enabled_channels) if enabled_channels else "None"
    table.add_row("Channels", channels_str or "None", f"{len(enabled_channels)} enabled")

    sessions_dir = workspace / "sessions"
    session_count = len(list(sessions_dir.glob("*.json"))) if sessions_dir.exists() else 0
    table.add_row("Sessions", str(session_count), str(sessions_dir))

    mcp_count = len(config.tools.mcp_servers) if config.tools.mcp_servers else 0
    table.add_row("MCP Servers", str(mcp_count), ", ".join(config.tools.mcp_servers.keys()) if mcp_count else "None")

    console.print(table)

    if running:
        console.print(f"\n[green]✓[/green] Gateway is running normally.")
    else:
        console.print(f"\n[yellow]Gateway is not running.[/yellow]")
        console.print("Use [cyan]markbot gateway start[/cyan] to start it.")

app.add_typer(gateway_app, name="gateway")

# ============================================================================
# Config Commands
# ============================================================================

config_app = typer.Typer(
    name="config",
    help="Manage markbot configuration (get/set values).",
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
    from markbot.config.loader import get_config_path, load_config

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
    from markbot.config.loader import get_config_path, load_config, save_config

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

    table = Table(title=f"{__logo__} MarkBot Configuration", title_justify="left")
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

pairing_app = typer.Typer(help="Manage channel access pairing requests")


@pairing_app.command("list")
def pairing_list(
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
):
    """List all pending pairing requests."""
    from markbot.config.loader import get_config_path, load_config

    config = load_config()
    ws = Path(workspace) if workspace else config.workspace_path

    from markbot.channels.pairing import PairingManager
    manager = PairingManager(ws)
    manager.cleanup_expired()

    pending = manager.list_pairings()

    if not pending:
        console.print("[yellow]No pending pairing requests[/yellow]")
        raise typer.Exit(0)

    table = Table(title=f"{__logo__} Pending Pairing Requests", title_justify="left")
    table.add_column("Code", style="cyan")
    table.add_column("Channel", style="green")
    table.add_column("Sender ID", style="yellow")
    table.add_column("Created", style="blue")

    for item in pending:
        created = item["created_at"][:19] if item.get("created_at") else "N/A"
        table.add_row(item["code"], item["channel"], item["sender_id"], created)

    console.print(table)


@pairing_app.command("approve")
def pairing_approve(
    channel: str = typer.Argument(..., help="Channel name (e.g., feishu)"),
    code: str = typer.Argument(..., help="Pairing code"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
):
    """Approve a pairing request and add user to allow list."""
    from markbot.config.loader import get_config_path, load_config, save_config

    config = load_config()
    ws = Path(workspace) if workspace else config.workspace_path

    from markbot.channels.pairing import PairingManager
    manager = PairingManager(ws)

    pairing = manager.get_pairing(code)
    if not pairing:
        console.print(f"[red]Error: Pairing not found: {code}[/red]")
        raise typer.Exit(1)

    if not manager.approve_pairing(code):
        console.print(f"[red]Error: Failed to approve pairing: {code}[/red]")
        raise typer.Exit(1)

    if pairing["channel"] != channel:
        console.print(f"[red]Error: Channel mismatch. Expected {pairing['channel']}, got {channel}[/red]")
        raise typer.Exit(1)

    sender_id = pairing["sender_id"]

    # Add to allow list
    channel_config = getattr(config.channels, channel, None)
    if not channel_config:
        console.print(f"[red]Error: Unknown channel: {channel}[/red]")
        raise typer.Exit(1)

    allow_list = getattr(channel_config, "allow_from", [])
    if allow_list == []:
        allow_list = []

    if sender_id not in allow_list:
        allow_list.append(sender_id)
        setattr(channel_config, "allow_from", allow_list)
        save_config(config)

    console.print(f"[green]✓[/green] Approved pairing for {sender_id} on {channel}")
    console.print(f"[green]✓[/green] Added to allow list")

    # Auto restart gateway if running
    pid = _read_pid()
    if pid and _is_process_running(pid):
        console.print("[yellow]Restarting gateway...[/yellow]")
        import time
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(10):
                time.sleep(0.5)
                if not _is_process_running(pid):
                    _remove_pid()
                    break
        except ProcessLookupError:
            _remove_pid()

        # Restart gateway
        from markbot.config.loader import get_config_path
        config_path = get_config_path()
        gateway_start(port=18790, workspace=str(ws), config=str(config_path) if config_path else None, verbose=False, daemon=True)
        console.print("[green]✓[/green] Gateway restarted")
    else:
        console.print("[yellow]Gateway not running. Start it with: markbot gateway start[/yellow]")


@pairing_app.command("cancel")
def pairing_cancel(
    code: str = typer.Argument(..., help="Pairing code to cancel"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
):
    """Cancel a pending pairing request."""
    from markbot.config.loader import load_config

    config = load_config()
    ws = Path(workspace) if workspace else config.workspace_path

    from markbot.channels.pairing import PairingManager
    manager = PairingManager(ws)

    if not manager.cancel_pairing(code):
        console.print(f"[yellow]Pairing code not found: {code}[/yellow]")
        raise typer.Exit(1)

    console.print(f"[green]✓[/green] Cancelled pairing request: {code}")


app.add_typer(pairing_app, name="pairing")


def _make_provider(config: Config):
    """Create the appropriate LLM provider from config."""
    from markbot.providers.custom_provider import CustomProvider
    from markbot.providers.litellm_provider import LiteLLMProvider
    from markbot.providers.openai_codex_provider import OpenAICodexProvider

    model = config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    p = config.get_provider(model)

    # OpenAI Codex (OAuth)
    if provider_name == "openai_codex" or model.startswith("openai-codex/"):
        return OpenAICodexProvider(default_model=model)

    # Custom: direct OpenAI-compatible endpoint, bypasses LiteLLM
    if provider_name == "custom":
        return CustomProvider(
            api_key=p.api_key if p else "no-key",
            api_base=config.get_api_base(model) or "http://localhost:8000/v1",
            default_model=model,
        )

    from markbot.providers.registry import find_by_name
    spec = find_by_name(provider_name)
    if not model.startswith("bedrock/") and not (p and p.api_key) and not (spec and spec.is_oauth):
        console.print("[red]Error: No API key configured.[/red]")
        console.print("Set one in ~/.markbot/config.json under providers section")
        raise typer.Exit(1)

    return LiteLLMProvider(
        api_key=p.api_key if p else None,
        api_base=config.get_api_base(model),
        default_model=model,
        extra_headers=p.extra_headers if p else None,
        provider_name=provider_name,
    )
