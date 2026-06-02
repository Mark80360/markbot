"""CLI commands for the autopilot pipeline."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="autopilot",
    help="Manage the autopilot automation pipeline.",
    no_args_is_help=True,
)

console = Console()


def _resolve_workspace(workspace: str | None) -> Path:
    if workspace:
        return Path(workspace).expanduser().resolve()
    from markbot.config.paths import get_workspace_path
    return get_workspace_path()


@app.command("list")
def list_tasks(
    status: Optional[str] = typer.Option(None, "--status", "-s", help="Filter by status"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
):
    """List all autopilot tasks."""
    from markbot.autopilot.store import AutopilotStore

    ws = _resolve_workspace(workspace)
    store = AutopilotStore(ws)
    cards = store.list_cards(status=status if status else None)
    stats = store.stats()

    if not cards:
        console.print("[yellow]No autopilot tasks found.[/yellow]")
        console.print(f"Workspace: {ws / 'autopilot'}")
        console.print("Use [cyan]markbot autopilot add[/cyan] to create a task.")
        return

    console.print(f"\n[bold]Autopilot Tasks[/bold] (workspace: {ws / 'autopilot'})\n")

    if stats:
        stat_parts = []
        for k, v in sorted(stats.items()):
            if k == "completed":
                stat_parts.append(f"[green]{k}={v}[/green]")
            elif k == "queued":
                stat_parts.append(f"[yellow]{k}={v}[/yellow]")
            else:
                stat_parts.append(f"{k}={v}")
        console.print(f"  Stats: {' | '.join(stat_parts)}\n")

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("ID", style="cyan", width=14)
    table.add_column("Status", width=12)
    table.add_column("Score", width=6)
    table.add_column("Source", width=16)
    table.add_column("Title")

    for card in cards:
        status_style = {
            "queued": "[yellow]queued[/yellow]",
            "running": "[blue]running[/blue]",
            "completed": "[green]completed[/green]",
            "failed": "[red]failed[/red]",
            "rejected": "[dim]rejected[/dim]",
            "repairing": "[magenta]repairing[/magenta]",
            "verifying": "[blue]verifying[/blue]",
        }.get(card.status, card.status)
        table.add_row(card.id, status_style, str(card.score), card.source_kind, card.title)

    console.print(table)


@app.command("add")
def add_task(
    title: str = typer.Argument(..., help="Task title"),
    body: str = typer.Option("", "--body", "-b", help="Task description"),
    source: str = typer.Option("manual_idea", "--source", help="Source kind"),
    ref: str = typer.Option("", "--ref", help="Source reference"),
    labels: Optional[str] = typer.Option(None, "--labels", "-l", help="Comma-separated labels"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
):
    """Add a new task to the autopilot pipeline."""
    from markbot.autopilot.store import AutopilotStore

    ws = _resolve_workspace(workspace)
    store = AutopilotStore(ws)

    parsed_labels = [lbl.strip() for lbl in labels.split(",") if lbl.strip()] if labels else None
    card, created = store.enqueue_card(
        source_kind=source,
        title=title,
        body=body,
        source_ref=ref,
        labels=parsed_labels,
    )

    action = "Created" if created else "Updated"
    console.print(f"[green]✓[/green] {action} task [cyan]{card.id}[/cyan]")
    console.print(f"  Title: {card.title}")
    console.print(f"  Score: {card.score} ({', '.join(card.score_reasons)})")
    console.print(f"  Status: {card.status}")


@app.command("show")
def show_task(
    task_id: str = typer.Argument(..., help="Task ID"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
):
    """Show detailed information about a task."""
    from markbot.autopilot.store import AutopilotStore

    ws = _resolve_workspace(workspace)
    store = AutopilotStore(ws)
    card = store.get_card(task_id)

    if card is None:
        console.print(f"[red]Task '{task_id}' not found.[/red]")
        raise typer.Exit(1)

    console.print(f"\n[bold cyan]Task: {card.id}[/bold cyan]\n")
    console.print(f"  [bold]Title:[/bold] {card.title}")
    console.print(f"  [bold]Status:[/bold] {card.status}")
    console.print(f"  [bold]Source:[/bold] {card.source_kind} ({card.source_ref or 'N/A'})")
    console.print(f"  [bold]Score:[/bold] {card.score}")
    console.print(f"  [bold]Score reasons:[/bold] {', '.join(card.score_reasons)}")
    if card.labels:
        console.print(f"  [bold]Labels:[/bold] {', '.join(card.labels)}")
    if card.body:
        console.print(f"  [bold]Body:[/bold]\n    {card.body}")
    if card.metadata:
        console.print("  [bold]Metadata:[/bold]")
        for key, value in card.metadata.items():
            console.print(f"    {key}: {value}")


@app.command("tick")
def tick(
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: Optional[str] = typer.Option(None, "--config", "-c", help="Config file path"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override model"),
    max_turns: Optional[int] = typer.Option(None, "--max-turns", help="Override max turns"),
):
    """Run the next queued autopilot task."""
    from markbot.cli.runtime import load_runtime_config, make_provider

    ws = _resolve_workspace(workspace)

    from markbot.autopilot.service import AutopilotService
    from markbot.autopilot.store import AutopilotStore

    store = AutopilotStore(ws)
    service = AutopilotService(store)

    runtime_config = load_runtime_config(config, str(ws))
    provider = make_provider(runtime_config)

    from markbot.agent.loop import AgentLoop
    from markbot.bus.queue import MessageBus
    from markbot.schedule.cron import CronService

    bus = MessageBus()
    cron_store_path = ws / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    agent_loop = AgentLoop(
        ctx_or_bus=bus,
        fallback_manager=provider,
        config=runtime_config,
        workspace=ws,
        max_iterations=runtime_config.agents.defaults.max_tool_iterations,
        context_window_tokens=runtime_config.agents.defaults.context_window_tokens,
        web_search_config=runtime_config.tools.web.search,
        web_proxy=runtime_config.tools.web.proxy or None,
        exec_config=runtime_config.tools.exec,
        filesystem_config=runtime_config.tools.filesystem,
        memory_config=runtime_config.tools.memory,
        cron_service=cron,
        restrict_to_workspace=runtime_config.tools.restrict_to_workspace,
        mcp_servers=runtime_config.tools.mcp_servers,
        channels_config=runtime_config.channels,
        timezone=runtime_config.agents.defaults.timezone,
    )

    service.bind_agent_loop(agent_loop)

    async def _run():
        result = await service.tick(model=model, max_turns=max_turns)
        if result is None:
            console.print("[yellow]No task was executed.[/yellow]")
            console.print("Either no queued tasks or another task is already active.")
            return

        console.print("\n[bold]Autopilot Execution Result[/bold]\n")
        console.print(f"  [bold]Task ID:[/bold] {result.card_id}")
        console.print(f"  [bold]Status:[/bold] {result.status}")
        console.print(f"  [bold]Attempts:[/bold] {result.attempt_count}")
        if result.assistant_summary:
            summary = result.assistant_summary[:300]
            console.print(f"  [bold]Summary:[/bold] {summary}")
        if result.run_report_path:
            console.print(f"  [bold]Run report:[/bold] {result.run_report_path}")
        if result.verification_report_path:
            console.print(f"  [bold]Verification report:[/bold] {result.verification_report_path}")
        if result.verification_steps:
            passed = sum(1 for s in result.verification_steps if s.status == "success")
            total = len(result.verification_steps)
            console.print(f"  [bold]Verification:[/bold] {passed}/{total} steps passed")
            for step in result.verification_steps:
                icon = "✓" if step.status == "success" else "✗"
                console.print(f"    {icon} `{step.command}` (rc={step.returncode})")

        await agent_loop.close_mcp()

    asyncio.run(_run())


@app.command("reject")
def reject_task(
    task_id: str = typer.Argument(..., help="Task ID"),
    reason: str = typer.Option("", "--reason", "-r", help="Rejection reason"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
):
    """Reject an autopilot task."""
    from markbot.autopilot.store import AutopilotStore

    ws = _resolve_workspace(workspace)
    store = AutopilotStore(ws)

    try:
        card = store.update_status(task_id, status="rejected", note=reason or "rejected by user")
        console.print(f"[green]✓[/green] Task [cyan]{card.id}[/cyan] rejected.")
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)


@app.command("requeue")
def requeue_task(
    task_id: str = typer.Argument(..., help="Task ID"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
):
    """Requeue a previously failed or rejected task."""
    from markbot.autopilot.store import AutopilotStore

    ws = _resolve_workspace(workspace)
    store = AutopilotStore(ws)

    try:
        card = store.update_status(task_id, status="queued", note="requeued by user")
        console.print(
            f"[green]✓[/green] Task [cyan]{card.id}[/cyan] "
            f"requeued (score={card.score})."
        )
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)


@app.command("context")
def show_context(
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
):
    """Show the active autopilot context."""
    from markbot.autopilot.store import AutopilotStore

    ws = _resolve_workspace(workspace)
    store = AutopilotStore(ws)
    context = store.load_active_context()
    if not context:
        console.print("[yellow]No active autopilot context.[/yellow]")
        return
    console.print(context)


@app.command("journal")
def show_journal(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of entries to show"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
):
    """Show the autopilot journal."""
    import time as _time

    from markbot.autopilot.store import AutopilotStore

    ws = _resolve_workspace(workspace)
    store = AutopilotStore(ws)
    entries = store.load_journal(limit=limit)

    if not entries:
        console.print("[yellow]Journal is empty.[/yellow]")
        return

    console.print(f"\n[bold]Autopilot Journal[/bold] (last {len(entries)} entries)\n")
    for entry in entries:
        ts = _time.strftime("%Y-%m-%d %H:%M:%S", _time.gmtime(entry.timestamp))
        task_ref = f" [{entry.task_id}]" if entry.task_id else ""
        console.print(f"  [dim]{ts}[/dim] [cyan]{entry.kind}[/cyan]{task_ref}: {entry.summary}")


@app.command("stats")
def show_stats(
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
):
    """Show autopilot statistics."""
    from markbot.autopilot.store import AutopilotStore

    ws = _resolve_workspace(workspace)
    store = AutopilotStore(ws)
    stats = store.stats()

    if not stats:
        console.print("[yellow]No autopilot tasks yet.[/yellow]")
        return

    console.print("\n[bold]Autopilot Statistics[/bold]\n")
    for status, count in sorted(stats.items()):
        style = {
            "completed": "green",
            "queued": "yellow",
            "running": "blue",
            "failed": "red",
        }.get(status, "white")
        console.print(f"  [{style}]{status}[/{style}]: {count}")
    console.print(f"  [bold]Total:[/bold] {sum(stats.values())}")
