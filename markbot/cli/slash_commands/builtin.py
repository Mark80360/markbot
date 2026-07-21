"""Built-in slash command handlers."""

from __future__ import annotations

import asyncio
import os
import sys

from loguru import logger

from markbot import __version__
from markbot.bus.events import OutboundMessage, make_session_key
from markbot.cli.slash_commands.router import CommandContext, CommandRouter
from markbot.types.permission import PermissionMode
from markbot.utils.helpers import build_status_content


async def cmd_stop(ctx: CommandContext) -> OutboundMessage:
    """Cancel all active tasks and subagents for the session."""
    loop = ctx.loop
    msg = ctx.msg
    loop.clear_steer(msg.session_key)
    tasks = loop._active_tasks.pop(msg.session_key, [])
    cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
    for t in tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.opt(exception=True).warning("Active task raised an exception while being stopped")
    sub_cancelled = await loop.subagents.cancel_by_session(msg.session_key)
    total = cancelled + sub_cancelled
    session = loop.sessions.get_or_create(msg.session_key)
    loop.sessions.save(session)
    content = f"Stopped {total} task(s)." if total else "No active task to stop."
    return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content)


async def cmd_restart(ctx: CommandContext) -> OutboundMessage:
    """Restart the process in-place via os.execv."""
    msg = ctx.msg

    async def _do_restart():
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable, "-m", "markbot"] + sys.argv[1:])

    asyncio.create_task(_do_restart())
    return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content="Restarting...")


async def cmd_status(ctx: CommandContext) -> OutboundMessage:
    """Build an outbound status message for a session."""
    loop = ctx.loop
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    token_summary = loop.cost_tracker.get_token_summary()
    cumulative = token_summary["total"]
    last = loop._last_usage

    base = build_status_content(
        version=__version__, model=loop.model,
        start_time=loop._start_time,
        context_window_tokens=loop.context_window_tokens,
        context_tokens=last.get("prompt_tokens", 0),
        session_msg_count=len(session.messages),
        session_history_count=len(session.get_history(max_messages=0)),
        tool_count=len(loop.tools.get_definitions()),
        last_usage=last,
        cumulative_input=cumulative["input_tokens"],
        cumulative_output=cumulative["output_tokens"],
        cumulative_cache_creation=cumulative["cache_creation_input_tokens"],
        cumulative_cache_read=cumulative["cache_read_input_tokens"],
        api_calls=token_summary["api_calls"],
    )
    # Control-plane surface: footprint ladder + deferred mutations + engine.
    if hasattr(loop, "footprint_status_lines"):
        try:
            cp_lines = loop.footprint_status_lines()
            if cp_lines:
                base = base.rstrip() + "\n\n[Control Plane]\n" + "\n".join(cp_lines)
        except Exception:
            pass

    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=base,
        metadata={"render_as": "text"},
    )


async def cmd_new(ctx: CommandContext) -> OutboundMessage:
    """Start a fresh session with memory summarization."""
    loop = ctx.loop
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    mm = getattr(loop, "memory_manager", None)
    if mm and session.messages:
        mm.add_async_summary_task(
            messages=[{"role": m.get("role", "user"), "content": m.get("content", "")} for m in session.messages if m.get("content")],
        )
    if mm:
        session_key = make_session_key(ctx.msg.channel, ctx.msg.chat_id)
        if session_key:
            mm.set_compressed_summary("", session_key=session_key)
    session.clear()
    loop.sessions.save(session)
    loop.sessions.invalidate(session.key)
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content="New session started. Summary task dispatched in background.",
    )


async def cmd_compact(ctx: CommandContext) -> OutboundMessage:
    """Manually trigger memory compaction."""
    loop = ctx.loop
    mm = getattr(loop, "memory_manager", None)
    if not mm:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="Memory manager is not available.",
        )
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    history = session.get_history(max_messages=0)
    if not history:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="No messages to compact.",
        )
    session_key = make_session_key(ctx.msg.channel, ctx.msg.chat_id)
    # Run the synchronous compaction first, then reuse its output as
    # input for the async long-term summary task.  This mirrors the
    # automatic compaction hook (agent/hooks/compaction.py) and avoids
    # a redundant LLM call over the same raw messages.
    summary = await mm.compact_memory(
        messages=history,
        previous_summary=mm.get_compressed_summary(session_key=session_key),
    )
    if summary:
        mm.set_compressed_summary(summary, session_key=session_key)
        mm.add_async_summary_task(
            messages=history,
            compact_summary=summary,
        )
        # Advance last_consolidated to mark old messages as archived
        # without deleting them, so get_history() still has access.
        keep_recent = 6
        session.last_consolidated = max(
            session.last_consolidated,
            max(0, len(session.messages) - keep_recent),
        )
        loop.sessions.save(session)
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=f"Compact complete!\n\n**Compressed Summary:**\n{summary}",
        )
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content="Compact failed. Check logs for details.",
    )


async def cmd_compact_str(ctx: CommandContext) -> OutboundMessage:
    """Show the current compressed summary."""
    loop = ctx.loop
    mm = getattr(loop, "memory_manager", None)
    if not mm:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="Memory manager is not available.",
        )
    session_key = make_session_key(ctx.msg.channel, ctx.msg.chat_id)
    summary = mm.get_compressed_summary(session_key=session_key)
    if not summary:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="No compressed summary yet. Use /compact or wait for auto-compaction.",
        )
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=f"**Compressed Summary:**\n\n{summary}",
    )


async def cmd_clear(ctx: CommandContext) -> OutboundMessage:
    """Clear conversation history and compressed summary."""
    loop = ctx.loop
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    mm = getattr(loop, "memory_manager", None)
    if mm:
        # Always clear the per-session summary first (this is what
        # /compact_str, /compact, and the agent loop read).  Also clear
        # the legacy global slot for backwards compatibility.
        session_key = make_session_key(ctx.msg.channel, ctx.msg.chat_id)
        if session_key:
            mm.set_compressed_summary("", session_key=session_key)
        mm.set_compressed_summary("")
    session.clear()
    loop.sessions.save(session)
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content="History cleared. Compressed summary reset.",
    )


async def cmd_steer(ctx: CommandContext) -> OutboundMessage:
    """Inject a mid-task instruction into the running agent loop."""
    loop = ctx.loop
    msg = ctx.msg
    steer_text = (ctx.args or "").strip()
    if not steer_text:
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content="Usage: /steer <instruction>\nExample: /steer focus on error handling",
        )
    session_key = msg.session_key
    # Steer is only consumed by ``_inject_pending_steer`` inside an active
    # agent iteration.  If no task is running for this session, queueing
    # the instruction would silently sit in ``_pending_steer`` until the
    # next run starts, which is misleading.  Refuse instead.
    tasks = loop._active_tasks.get(session_key, [])
    has_running = any(not t.done() for t in tasks)
    if not has_running:
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content="No agent loop is currently running for this session.",
        )
    accepted = loop.steer(session_key, steer_text)
    if accepted:
        preview = steer_text[:80] + "..." if len(steer_text) > 80 else steer_text
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content=f"Steer queued: {preview}",
        )
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id,
        content="Steer rejected (empty instruction).",
    )


async def cmd_help(ctx: CommandContext) -> OutboundMessage:
    """Return available slash commands from the CommandDef catalog."""
    from markbot.cli.slash_commands.command_def import CommandCatalog

    catalog = None
    if ctx.loop is not None:
        commands = getattr(ctx.loop, "commands", None)
        catalog = getattr(commands, "catalog", None) if commands is not None else None
        if catalog is None:
            catalog = getattr(ctx.loop, "_command_catalog", None)
    if isinstance(catalog, CommandCatalog):
        content = catalog.help_text()
    else:
        # Fallback when catalog not attached (tests / minimal loops).
        content = build_builtin_catalog().help_text()
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata={"render_as": "text"},
    )


_MODE_ALIASES = {
    "default": PermissionMode.DEFAULT,
    "plan": PermissionMode.PLAN,
    "accept_edits": PermissionMode.ACCEPT_EDITS,
    "accept": PermissionMode.ACCEPT_EDITS,
    "auto": PermissionMode.AUTO,
    "bypass": PermissionMode.BYPASS,
    "bypass_permissions": PermissionMode.BYPASS,
}


async def cmd_mode(ctx: CommandContext) -> OutboundMessage:
    """Switch the active permission mode for tool execution.

    Usage:
      /mode            — show current mode
      /mode default    — confirm before destructive tools (interactive approval)
      /mode plan       — read-only only, no mutations
      /mode auto       — allow all tools without confirmation (recommended)
      /mode bypass     — bypass all permission checks (dangerous)
    """
    loop = ctx.loop
    app_state = getattr(getattr(loop, "ctx", None), "app_state", None)
    if app_state is None:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="App state provider is not available.",
        )

    arg = (ctx.args or "").strip().lower()
    if not arg:
        current = app_state.get_permission_mode()
        lines = [
            "Permission modes:",
            "  default    — confirm before destructive tools (interactive approval)",
            "  plan       — read-only only, blocks all mutations",
            "  accept_edits — allow file edits, still confirm destructive ops",
            "  auto       — allow all tools without confirmation",
            "  bypass     — bypass all permission checks (dangerous)",
            "",
            f"Current mode: {current.value}",
            "Use: /mode <mode> to switch.",
        ]
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="\n".join(lines),
            metadata={"render_as": "text"},
        )

    mode = _MODE_ALIASES.get(arg)
    if mode is None:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=f"Unknown mode '{arg}'. Valid: {', '.join(sorted(_MODE_ALIASES))}.",
        )

    app_state.set_permission_mode(mode)
    # Persist to config.json so the choice survives restarts. Best-effort:
    # the in-memory switch above already took effect, so a failed write
    # only means the user will need to /mode again after next restart.
    # Key path uses camelCase to match the JSON alias (schema Base uses
    # to_camel alias_generator), not the snake_case Python field name.
    from markbot.config.loader import update_config_value
    persisted = update_config_value(
        ["agents", "defaults", "defaultPermissionMode"], mode.value,
    )
    content = f"Permission mode set to: {mode.value}"
    if not persisted:
        content += " (warning: failed to persist to config.json — mode will reset on restart)"
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=content,
    )



async def cmd_profile(ctx: CommandContext) -> OutboundMessage:
    """Show or persist the runtime profile.

    Usage:
      /profile              — show current profile
      /profile coding       — software development defaults
      /profile assistant    — interactive personal assistant
      /profile unattended   — cron/heartbeat defaults
    """
    from markbot.config.profile import get_profile, list_profiles

    loop = ctx.loop
    config = getattr(loop, "config", None)
    current = "coding"
    try:
        current = config.agents.defaults.profile if config else "coding"
    except Exception:
        current = getattr(getattr(loop, "ctx", None), "profile", None)
        current = getattr(current, "name", None) or "coding"

    arg = (ctx.args or "").strip().lower()
    if not arg:
        lines = ["Runtime profiles:"]
        for name in list_profiles():
            p = get_profile(name)
            mark = " (current)" if name == current else ""
            lines.append(f"  {name}{mark} — {p.description}")
            lines.append(
                f"    permission={p.permission_mode}, "
                f"desktop={p.enable_desktop}, browser={p.enable_browser}, "
                f"autopilot={p.enable_autopilot}, min_skill_score={p.min_skill_score}"
            )
        lines.append("")
        lines.append(
            "Use: /profile <name> to switch now "
            "(tool surface is deferred mid-turn for cache safety; "
            "permission mode applies immediately)."
        )
        # Show live footprint profile when available.
        live = None
        try:
            live = getattr(getattr(loop, "tool_footprint", None), "profile_name", None)
        except Exception:
            live = None
        if live and live != current:
            lines.append(f"Live process profile: {live}")
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="\n".join(lines),
            metadata={"render_as": "text"},
        )

    if arg not in list_profiles():
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=f"Unknown profile '{arg}'. Valid: {', '.join(list_profiles())}.",
        )

    from markbot.config.loader import update_config_value
    persisted = update_config_value(["agents", "defaults", "profile"], arg)
    profile = get_profile(arg)
    # Also switch permission mode immediately for this process.
    app_state = getattr(getattr(loop, "ctx", None), "app_state", None)
    if app_state is not None:
        try:
            app_state.set_permission_mode(PermissionMode(profile.permission_mode))
        except Exception:
            pass
    content = (
        f"Profile set to: {arg} (permission default: {profile.permission_mode}). "
        "Tool surface changes apply on next gateway restart."
    )
    if not persisted:
        content += " (warning: failed to persist to config.json)"
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id, content=content,
    )


def build_builtin_catalog() -> "CommandCatalog":
    """Single-source CommandDef catalog for built-in slash commands."""
    from markbot.cli.slash_commands.command_def import (
        CommandCatalog,
        CommandDef,
        CommandTier,
    )

    catalog = CommandCatalog()
    catalog.register(
        CommandDef(
            name="/stop",
            handler=cmd_stop,
            description="Cancel all active tasks and subagents",
            tier=CommandTier.PRIORITY,
            category="control",
            control_plane=True,
        ),
        CommandDef(
            name="/steer",
            handler=cmd_steer,
            description="Inject mid-task instruction into running agent",
            tier=CommandTier.PRIORITY,
            args_hint="<text>",
            category="control",
            control_plane=True,
        ),
        CommandDef(
            name="/restart",
            handler=cmd_restart,
            description="Restart the agent process",
            tier=CommandTier.PRIORITY,
            category="control",
            control_plane=True,
        ),
        CommandDef(
            name="/status",
            handler=cmd_status,
            description="Show session status, token usage, and statistics",
            tier=CommandTier.PRIORITY,
            category="session",
            control_plane=True,
        ),
        CommandDef(
            name="/new",
            handler=cmd_new,
            description="Start fresh session with memory summary",
            category="session",
        ),
        CommandDef(
            name="/compact",
            handler=cmd_compact,
            description="Force manual context compaction",
            category="session",
        ),
        CommandDef(
            name="/compact_str",
            handler=cmd_compact_str,
            description="View current compressed summary",
            category="session",
        ),
        CommandDef(
            name="/mode",
            handler=cmd_mode,
            description="Show or set permission mode (default/plan/auto/bypass)",
            args_hint="[mode]",
            category="runtime",
        ),
        CommandDef(
            name="/profile",
            handler=cmd_profile,
            description="Show or set runtime profile (coding/assistant/unattended)",
            args_hint="[name]",
            category="runtime",
        ),
        CommandDef(
            name="/clear",
            handler=cmd_clear,
            description="Clear history and compressed summary",
            category="session",
        ),
        CommandDef(
            name="/help",
            handler=cmd_help,
            description="Show available slash commands",
            category="general",
        ),
    )
    return catalog


def register_builtin_commands(router: CommandRouter) -> None:
    """Register the default set of slash commands via CommandDef catalog."""
    catalog = build_builtin_catalog()
    catalog.apply_to_router(router)
    # Stash on router for help / autocomplete consumers.
    setattr(router, "catalog", catalog)
