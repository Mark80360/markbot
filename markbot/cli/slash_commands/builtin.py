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

    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=build_status_content(
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
        ),
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
    """Return available slash commands."""
    lines = [
        "🦞 MarkBot commands:",
        "/new — Start fresh session with memory summary",
        "/compact — Force manual context compaction",
        "/compact_str — View current compressed summary",
        "/clear — Clear history and compressed summary",
        "/stop — Cancel all active tasks and subagents",
        "/steer <text> — Inject mid-task instruction into running agent",
        "/status — Show session status, token usage, and statistics",
        "/mode [mode] — Show or set permission mode (default/plan/auto/bypass)",
        "/restart — Restart the agent process",
        "/help — Show available slash commands",
    ]
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content="\n".join(lines),
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
      /mode default    — confirm before destructive tools (recommended)
      /mode plan       — read-only only, no mutations
      /mode auto       — allow all tools without confirmation
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
            "  default    — confirm before destructive tools (current default)",
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


def register_builtin_commands(router: CommandRouter) -> None:
    """Register the default set of slash commands."""
    router.priority("/stop", cmd_stop)
    router.priority("/steer", cmd_steer)
    router.priority("/restart", cmd_restart)
    router.priority("/status", cmd_status)
    router.exact("/new", cmd_new)
    router.exact("/compact", cmd_compact)
    router.exact("/compact_str", cmd_compact_str)
    router.exact("/mode", cmd_mode)
    router.exact("/clear", cmd_clear)
    router.exact("/help", cmd_help)
