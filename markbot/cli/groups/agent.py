"""``markbot agent`` group: interactive agent loop.

Moved from ``markbot.cli.commands`` in the P1-1 refactor. Owns the
agent command and its 4 prompt_toolkit-driven helpers (interactive
line / response / progress printing and async input read).
"""
from __future__ import annotations

import asyncio
import signal
import sys
from contextlib import nullcontext

import typer
from loguru import logger
from prompt_toolkit import print_formatted_text
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.formatted_text import ANSI, HTML
from prompt_toolkit.patch_stdout import patch_stdout

from markbot import __logo__, __version__
from markbot.cli.daemon import _gateway_paths
from markbot.cli.runtime import load_runtime_config, make_provider
from markbot.cli.stream import StreamRenderer, ThinkingSpinner
from markbot.cli.ui import (
    console,
    flush_pending_tty_input,
    get_prompt_session,
    init_prompt_session,
    is_exit_command,
    markbot_banner,
    print_agent_response,
    print_cli_progress_line,
    render_interactive_ansi,
    response_renderable,
    restore_terminal,
)
from markbot.config.paths import get_cron_dir
from markbot.utils.helpers import sync_workspace_templates

app = typer.Typer(
    help="Interact with the MarkBot agent.",
    invoke_without_command=True,
)


# ---------------------------------------------------------------------------
# Interactive-mode helpers (prompt_toolkit-driven)
# ---------------------------------------------------------------------------


async def _print_interactive_line(text: str) -> None:
    """Print async interactive updates with prompt_toolkit-safe Rich styling."""
    def _write() -> None:
        ansi = render_interactive_ansi(
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
        ansi = render_interactive_ansi(
            lambda c: (
                c.print(),
                c.print(f"[cyan]{__logo__} MarkBot[/cyan]"),
                c.print(response_renderable(content, render_markdown, metadata)),
                c.print(),
            )
        )
        print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


async def _print_interactive_progress_line(text: str, thinking: ThinkingSpinner | None) -> None:
    """Print an interactive progress line, pausing the spinner if needed."""
    with thinking.pause() if thinking else nullcontext():
        await _print_interactive_line(text)


async def _read_interactive_input_async() -> str:
    """Read user input using prompt_toolkit (handles paste, history, display)."""
    session = get_prompt_session()
    if session is None:
        raise RuntimeError("Call init_prompt_session() first")
    try:
        with patch_stdout():
            return await session.prompt_async(
                HTML("<b fg='ansiblue'>❯</b> "),
            )
    except EOFError as exc:
        raise KeyboardInterrupt from exc


# ---------------------------------------------------------------------------
# ``markbot agent`` callback
# ---------------------------------------------------------------------------


@app.callback()
def agent(
    ctx: typer.Context,
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:direct", "--session", "-s", help="Session ID"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    markdown: bool = typer.Option(True, "--markdown/--no-markdown", help="Render assistant output as Markdown"),
    logs: bool = typer.Option(False, "--logs/--no-logs", help="Show MarkBot runtime logs during chat"),
):
    """Interact with the agent directly."""
    if ctx.invoked_subcommand is not None:
        return

    import time

    from markbot.agent.loop import AgentLoop
    from markbot.bus.queue import MessageBus
    from markbot.log.core import setup_logging
    from markbot.schedule.cron import CronService

    _gateway_paths()["agent_log_dir"].mkdir(parents=True, exist_ok=True)
    setup_logging(
        console_level="DEBUG" if logs else "ERROR",
        log_file=_gateway_paths()["agent_log_file"],
    )

    # Bridge TOOL_PROGRESS events to loguru so streamed tool output
    # (e.g. ``pip install`` lines) is visible in the session log file.
    # The producer in agent.iteration emits these unconditionally; without
    # a subscriber the bus is fire-and-forget and the user sees no progress.
    from markbot.cli.progress import register_progress_subscriber
    register_progress_subscriber()

    logger.info("agent command starting...")

    _t0 = time.time()
    config = load_runtime_config(config, workspace)
    logger.debug("Config loaded, took {:.3f}s", time.time() - _t0)

    _t0 = time.time()
    sync_workspace_templates(config.workspace_path)
    logger.debug("Workspace templates synced, took {:.3f}s", time.time() - _t0)

    _t0 = time.time()
    bus = MessageBus()
    logger.debug("MessageBus created, took {:.3f}s", time.time() - _t0)

    _t0 = time.time()
    provider = make_provider(config)
    logger.debug("Provider created, took {:.3f}s", time.time() - _t0)

    cron_store_path = get_cron_dir(config.workspace_path) / "jobs.json"
    cron = CronService(cron_store_path)

    _t0 = time.time()
    logger.info("Creating AgentLoop...")
    agent_loop = AgentLoop(
        ctx_or_bus=bus,
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
    logger.info("AgentLoop created, took {:.3f}s", time.time() - _t0)
    if hasattr(agent_loop, 'ctx') and hasattr(agent_loop.ctx, 'init_summary'):
        logger.info("AgentContext init breakdown:\n{}", agent_loop.ctx.init_summary)
    else:
        logger.warning("No timing data available from AgentContext")

    # Shared reference for progress callbacks
    _thinking: ThinkingSpinner | None = None

    async def _cli_progress(content: str, *, tool_hint: bool = False) -> None:
        ch = agent_loop.channels_config
        if ch and tool_hint and not ch.send_tool_hints:
            return
        if ch and not tool_hint and not ch.send_progress:
            return
        print_cli_progress_line(content, _thinking)

    if message:
        # Single message mode — direct call, no bus needed
        async def run_once():
            renderer = StreamRenderer(render_markdown=markdown)
            try:
                response = await agent_loop.process_direct(
                    message, session_id,
                    on_progress=_cli_progress,
                    on_stream=renderer.on_delta,
                    on_stream_end=renderer.on_end,
                )
                if not renderer.streamed:
                    await renderer.close()
                    print_agent_response(
                        response.content if response else "",
                        render_markdown=markdown,
                        metadata=response.metadata if response else None,
                    )
            finally:
                await agent_loop.close_mcp()

        asyncio.run(run_once())
    else:
        # Interactive mode — route through bus like other channels
        from markbot.bus.events import InboundMessage

        if not sys.stdin.isatty():
            console.print("[red]Error:[/red] Interactive mode requires a terminal (stdin is not a TTY).")
            console.print("Use [bold]markbot agent --message <text>[/bold] for non-interactive mode.")
            raise typer.Exit(1)

        init_prompt_session()

        markbot_banner()
        console.print(f"{__logo__} MarkBot v{__version__} (type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit)\n")

        if ":" in session_id:
            cli_channel, cli_chat_id = session_id.split(":", 1)
        else:
            cli_channel, cli_chat_id = "cli", session_id

        # Event used to wake up the interactive loop so it can exit cleanly
        # (instead of sys.exit which would skip finally blocks / MCP cleanup).
        shutdown_event = asyncio.Event()

        def _handle_signal(signum, frame):
            restore_terminal()
            # Schedule shutdown on the running loop instead of hard-exiting.
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No running loop — fall back to hard exit.
                sys.exit(0)
            loop.call_soon_threadsafe(shutdown_event.set)

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
                    if shutdown_event.is_set():
                        restore_terminal()
                        console.print("\nGoodbye!")
                        break
                    try:
                        flush_pending_tty_input()
                        # Race prompt_async against shutdown_event so
                        # Ctrl+C (which sets shutdown_event via the signal
                        # handler) can interrupt a blocked prompt.
                        prompt_task = asyncio.ensure_future(_read_interactive_input_async())
                        shutdown_task = asyncio.ensure_future(shutdown_event.wait())
                        done, pending = await asyncio.wait(
                            {prompt_task, shutdown_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for t in pending:
                            t.cancel()
                        if shutdown_task in done:
                            restore_terminal()
                            console.print("\nGoodbye!")
                            break
                        user_input = prompt_task.result()
                        command = user_input.strip()
                        if not command:
                            continue

                        if is_exit_command(command):
                            restore_terminal()
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

                        # Bound the wait so a crashed agent loop doesn't
                        # hang the CLI forever. The consumer task still
                        # drains the bus, so a late response will surface.
                        try:
                            await asyncio.wait_for(turn_done.wait(), timeout=300.0)
                        except asyncio.TimeoutError:
                            console.print("[yellow]⚠ No response within 5 minutes, aborting turn.[/yellow]")
                            turn_done.set()

                        if turn_response:
                            content, meta = turn_response[0]
                            if content and not meta.get("_streamed"):
                                if renderer:
                                    await renderer.close()
                                print_agent_response(
                                    content, render_markdown=markdown, metadata=meta,
                                )
                        elif renderer and not renderer.streamed:
                            await renderer.close()
                    except KeyboardInterrupt:
                        restore_terminal()
                        console.print("\nGoodbye!")
                        break
                    except EOFError:
                        restore_terminal()
                        console.print("\nGoodbye!")
                        break
            finally:
                agent_loop.stop()
                outbound_task.cancel()
                bus_task.cancel()
                await asyncio.gather(bus_task, outbound_task, return_exceptions=True)
                await agent_loop.close_mcp()

        asyncio.run(run_interactive())
