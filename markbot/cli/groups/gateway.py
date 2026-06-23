"""``markbot gateway`` group: background gateway lifecycle.

Provides start / stop / restart / status subcommands plus the
foreground run loop extracted from ``markbot.cli.commands``.
"""
from __future__ import annotations

import asyncio
import json
import platform
from datetime import datetime
from pathlib import Path

import typer

from markbot import __logo__
from markbot.cli.daemon import (
    _gateway_paths,
    is_process_running,
    read_pid,
    remove_pid,
    start_daemon,
    terminate_process,
)
from markbot.cli.runtime import make_provider
from markbot.cli.ui import console, make_section_helpers, markbot_banner
from markbot.config.paths import get_cron_dir
from markbot.utils.helpers import sync_workspace_templates

app = typer.Typer(
    name="gateway",
    help="Manage the markbot gateway service (start/stop/restart/status).",
)


@app.command("start")
def start(
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    daemon: bool = typer.Option(True, "--daemon/--foreground", "-d", help="Run as daemon (background)"),
):
    """Start the Markbot gateway service."""
    markbot_banner()

    existing_pid = read_pid()
    if existing_pid and is_process_running(existing_pid):
        console.print(f"[yellow]Gateway is already running (PID: {existing_pid})[/yellow]")
        console.print("Use [cyan]markbot gateway stop[/cyan] to stop it first.")
        raise typer.Exit(1)

    # Pre-flight: detect port conflicts before spawning the daemon so we
    # don't leave a stale PID file behind.
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        console.print(
            f"[red]✗ Port {port} is already in use.[/red] "
            "Stop the other process or use [cyan]--port[/cyan] to pick a different one."
        )
        raise typer.Exit(1)
    finally:
        probe.close()

    if daemon:
        start_daemon(port, workspace, config, verbose)
    else:
        run_gateway_foreground(port, workspace, config, verbose)


def run_gateway_foreground(port: int, workspace: str | None, config: str | None, verbose: bool) -> None:
    """Run the gateway in foreground mode."""
    from loguru import logger

    from markbot.agent.loop import AgentLoop
    from markbot.bus.queue import MessageBus
    from markbot.channels.manager import ChannelManager
    from markbot.config.loader import load_config
    from markbot.log.core import setup_logging
    from markbot.schedule.cron import CronJob, CronService
    from markbot.schedule.heartbeat import HeartbeatService
    from markbot.session.session import SessionManager

    setup_logging(verbose=verbose, log_file=_gateway_paths()["log_file"])

    config_path = Path(config) if config else None
    config = load_config(config_path)
    if workspace:
        config.agents.defaults.workspace = workspace

    console.print(f"{__logo__} Starting MarkBot gateway on port {port}...")

    sync_workspace_templates(config.workspace_path)
    bus = MessageBus()
    provider = make_provider(config)
    session_manager = SessionManager(config.workspace_path)

    cron_store_path = get_cron_dir(config.workspace_path) / "jobs.json"
    cron = CronService(cron_store_path)

    agent = AgentLoop(
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
        from markbot.schedule.evaluator import evaluate_response
        from markbot.tools.cron import CronTool
        from markbot.tools.message import MessageTool

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
            cron_session = agent.sessions.get_or_create(f"cron:{job.id}")
            cron_session.retain_recent_legal_suffix(8)
            agent.sessions.save(cron_session)

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

            dream_service = None
            dream_cron = config.tools.memory.dream_cron
            if dream_cron and agent.memory_manager is not None:
                from markbot.schedule.dream import DreamService

                # Dream is system-triggered only.  The is_busy_fn guard
                # ensures it never fires while a conversation is in
                # progress (requirement: no concurrent dream + chat).
                dream_service = DreamService(
                    cron_expr=dream_cron,
                    dream_fn=agent.memory_manager.dream,
                    state_dir=config.workspace_path,
                    is_busy_fn=agent.has_active_conversations,
                    timezone=config.agents.defaults.timezone,
                )
                await dream_service.start()
                console.print(f"[green]✓[/green] Dream: cron={dream_cron}")

            await heartbeat.start()

            # Start the skill curator for lifecycle management
            # (auto-archive stale skills, evaluate quality).
            curator = None
            if agent.skill_registry is not None:
                from markbot.skills.curator import CuratorService
                curator = CuratorService(
                    workspace=config.workspace_path,
                    skill_registry=agent.skill_registry,
                    auto_archive=True,
                    interval_hours=6,
                )
                await curator.start()
                console.print("[green]✓[/green] Skill curator: interval=6h")

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
            if dream_service:
                await dream_service.stop()
            if curator:
                await curator.stop()
            await agent.close_mcp()
            await heartbeat.stop()
            cron.stop()
            agent.stop()
            await channels.stop_all()

    asyncio.run(run())


@app.command("stop")
def stop(
    force: bool = typer.Option(False, "--force", "-f", help="Force kill the process"),
):
    """Stop the MarkBot gateway service."""
    markbot_banner()

    section, kv, divider = make_section_helpers()

    console.print()

    pid = read_pid()
    if not pid:
        section("Status", "yellow")
        console.print("  [yellow]○ Gateway is not running (no PID file found)[/yellow]")
        divider()
        console.print()
        raise typer.Exit(0)

    if not is_process_running(pid):
        remove_pid()
        section("Status", "yellow")
        console.print("  [yellow]○ Gateway is not running (stale PID file removed)[/yellow]")
        divider()
        console.print()
        raise typer.Exit(0)

    section("Status", "cyan")
    kv("State", f"[bold cyan]● STOPPING[/bold cyan] (PID: {pid})")
    kv("Action", "Force kill" if force else "Graceful terminate")
    console.print()

    if terminate_process(pid, force):
        remove_pid()
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


@app.command("restart")
def restart(
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    force: bool = typer.Option(False, "--force", "-f", help="Force kill before restart"),
):
    """Restart the Markbot gateway service."""
    markbot_banner()

    section, kv, divider = make_section_helpers()

    console.print()

    pid = read_pid()

    if pid and is_process_running(pid):
        section("Stop", "cyan")
        kv("PID", str(pid))
        kv("Action", "Force kill" if force else "Graceful terminate")
        divider()

        if terminate_process(pid, force):
            remove_pid()
            section("Stop", "green")
            kv("State", "[bold green]● STOPPED[/bold green]")
            divider()
        else:
            remove_pid()
            section("Stop", "red")
            kv("State", "[bold red]● FAILED[/bold red]")
            console.print("  [red]Gateway did not stop gracefully. Use --force to kill it.[/red]")
            divider()
            raise typer.Exit(1)
    else:
        if pid:
            remove_pid()
        section("Stop", "yellow")
        console.print("  [yellow]○ Gateway was not running[/yellow]")
        divider()

    start_daemon(port, workspace, config, verbose)


@app.command("status")
def status():
    """Check the status of the MarkBot gateway service."""
    from rich.text import Text

    from markbot.config.loader import load_config

    markbot_banner()

    config = load_config()
    workspace = config.workspace_path

    # ── Gather all data ─────────────────────────────────────────────────────
    pid = read_pid()
    running = False
    proc = None
    if pid:
        running = is_process_running(pid)
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

    log_exists = _gateway_paths()["log_file"].exists()
    log_size_kb = 0.0
    log_mtime = ""
    if log_exists:
        _log_file = _gateway_paths()["log_file"]
        log_size_kb = _log_file.stat().st_size / 1024
        log_mtime = datetime.fromtimestamp(_log_file.stat().st_mtime).strftime("%Y-%m-%d %H:%M")

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
    home_str = str(Path.home())
    if ws_raw.startswith(home_str + "/"):
        ws_str = "~" + ws_raw[len(home_str):]
    else:
        ws_str = ws_raw

    # ── Render ─────────────────────────────────────────────────────────────
    console.print()

    section, kv, divider = make_section_helpers()

    # ─ Process ──────────────────────────────────────────────────────────────
    section("Process", "green" if running else "red")
    if running and proc:
        try:
            uptime = datetime.now() - datetime.fromtimestamp(proc.create_time())
            days, r = divmod(uptime.seconds, 3600)
            hours, minutes = divmod(r, 60)
            uptime_str = f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"
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
    if config.agents.defaults.model_chain:
        for i, model in enumerate(config.agents.defaults.model_chain):
            kv("Model Chain" if i == 0 else "", model)
    else:
        kv("Model Chain", "[not configured]")
    kv("Context Window", f"{config.agents.defaults.context_window_tokens} tokens")
    hb_on = config.gateway.heartbeat.enabled
    kv("Heartbeat", f"[green]●[/green] Enabled every {config.gateway.heartbeat.interval_s}s" if hb_on else "[yellow]○[/yellow] Disabled")
    kv("Channels", f"{len(enabled_channels)} Enabled  ({', '.join(enabled_channels) or 'none'})")
    kv("Cron Jobs", f"[yellow]{active_jobs}[/yellow] / {cron_jobs} Active")

    divider()

    # ─ Log File ──────────────────────────────────────────────────────────
    section("Log File", "yellow" if log_exists else "dim")
    if log_exists:
        kv("Path", str(_gateway_paths()["log_file"]))
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
