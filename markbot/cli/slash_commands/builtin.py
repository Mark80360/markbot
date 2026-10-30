"""Built-in slash command handlers."""

from __future__ import annotations

import asyncio
import os
import sys

from loguru import logger

from markbot import __version__
from markbot.agent.authorization import denial_message, is_privileged_sender
from markbot.bus.events import OutboundMessage, derive_is_group, make_session_key
from markbot.cli.slash_commands.router import CommandContext, CommandRouter
from markbot.locales import t
from markbot.types.permission import PermissionMode
from markbot.utils.helpers import build_status_content

#: How long ``/stop`` waits for cancelled tasks to unwind before reporting.
#: Long enough for a tool call to release its subprocess, short enough that a
#: stuck task cannot wedge the inbound dispatch loop.
STOP_GRACE_SECONDS = 5.0

#: ``/trace full`` renders one line per event; a long turn can produce
#: thousands. Chat surfaces are not a pager, so the tail is elided.
TRACE_FULL_MAX_LINES = 120


def check_privileged(ctx: CommandContext, command: str) -> str | None:
    """Return a refusal message when the sender may not run *command*.

    Privileged commands change state for the *whole process* (``/mode`` also
    rewrites ``config.json``), so they must not be reachable by anyone who
    merely appears in a channel's ``allowFrom`` list. Returns ``None`` when
    the sender is authorised, otherwise operator-facing text.
    """
    msg = ctx.msg
    loop = ctx.loop
    config = getattr(loop, "config", None)
    channel_config = None
    # Per-channel sections are stashed as *attributes* of the ChannelsConfig
    # model (the model allows extra fields and the channel manager reads them
    # the same way, via ``getattr(config.channels, name)``), so an attribute
    # lookup is what resolves them. ``ChannelsConfig`` is a Pydantic model and
    # has no ``.get()``; calling one would raise and silently leave every
    # ``adminFrom`` / single-sender ``allowFrom`` rule unenforced.
    channels_config = getattr(loop, "channels_config", None) or getattr(
        config, "channels", None
    )
    if channels_config is not None and msg.channel:
        try:
            channel_config = getattr(channels_config, msg.channel, None)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("channel config lookup failed: {}", exc)

    is_group = derive_is_group(msg.metadata, msg.chat_id)

    decision = is_privileged_sender(
        channel=msg.channel,
        sender_id=msg.sender_id,
        is_group=is_group,
        channel_config=channel_config,
        global_admin_ids=getattr(
            getattr(config, "security", None), "admin_ids", None
        ),
    )
    if decision.allowed:
        return None
    reason = denial_message(decision, command=command)
    logger.warning(
        "Refused {} from {}:{} ({}): {}",
        command, msg.channel, msg.sender_id, msg.chat_id, decision.reason,
    )
    return reason


def check_privileged_in_group(ctx: CommandContext, command: str) -> str | None:
    """Gate *command* only when the message comes from a multi-party chat.

    In a 1:1 conversation the sender is the only person the command can
    affect, so they keep unconditional control of their own session — being
    unable to ``/stop`` a runaway turn because no admin is configured would be
    worse than the risk. In a group the same command cancels or erases work
    other participants are waiting on, which is the availability attack
    ``check_privileged`` exists to prevent. Unknown group-ness fails closed,
    matching :func:`is_privileged_sender`.
    """
    msg = ctx.msg
    if derive_is_group(msg.metadata, msg.chat_id) is False:
        return None
    return check_privileged(ctx, command)


async def cmd_stop(ctx: CommandContext) -> OutboundMessage:
    """Cancel all active tasks and subagents for the session."""
    loop = ctx.loop
    msg = ctx.msg
    if refusal := check_privileged_in_group(ctx, "/stop"):
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content=refusal, metadata={"render_as": "text"},
        )
    loop.clear_steer(msg.session_key)
    session_key = msg.session_key

    # Snapshot the in-flight tasks.  We must NOT pop the list here —
    # _untrack_task (done-callback) needs to find the entry to properly
    # clean up _session_locks when the last task completes.  In asyncio's
    # single-threaded model this synchronous read is atomic.
    tasks = list(loop._active_tasks.get(session_key, []))
    cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
    # Bounded wait: a task that swallows CancelledError must not block the
    # inbound dispatch loop forever.  This handler runs on the priority path,
    # i.e. directly in ``run()``, so an unbounded await here would also stall
    # every other session's messages.
    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=STOP_GRACE_SECONDS)
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                logger.opt(exception=exc).warning(
                    "Active task raised an exception while being stopped"
                )
        if pending:
            logger.warning(
                "{} task(s) ignored cancellation for session {}",
                len(pending), session_key,
            )
    sub_cancelled = await loop.subagents.cancel_by_session(session_key)
    total = cancelled + sub_cancelled
    session = loop.sessions.get_or_create(session_key)
    loop.sessions.save(session)
    content = t("cmd.stop.stopped", count=total) if total else t("cmd.stop.none")
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content=content
    )


async def cmd_restart(ctx: CommandContext) -> OutboundMessage:
    """Restart the process in-place via os.execv."""
    msg = ctx.msg

    # os.execv replaces the running daemon. From a shared group chat that
    # is an unauthenticated availability attack, so gate it like /mode.
    if refusal := check_privileged(ctx, "/restart"):
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content=refusal, metadata={"render_as": "text"},
        )

    async def _do_restart():
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable, "-m", "markbot"] + sys.argv[1:])

    asyncio.create_task(_do_restart())
    return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=t("cmd.restart.restarting"))


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


async def cmd_trace(ctx: CommandContext) -> OutboundMessage:
    """Show the decision trace of the most recent turn for this session.

    Answers "what did the agent actually do, and what did it cost?" after the
    fact. The log stream has scrolled away by then; the trace has not.
    """
    loop = ctx.loop
    getter = getattr(loop, "last_turn_trace", None)
    trace = None
    if callable(getter):
        trace = getter(session_key=ctx.msg.session_key)

    if trace is None:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="No turn trace recorded for this session yet.",
            metadata={"render_as": "text"},
        )

    arg = (ctx.args or "").strip().lower()
    if arg in ("--full", "full", "--timeline", "timeline"):
        lines = trace.render().splitlines()
        if len(lines) > TRACE_FULL_MAX_LINES:
            omitted = len(lines) - TRACE_FULL_MAX_LINES
            body = "\n".join(lines[:TRACE_FULL_MAX_LINES])
            body += f"\n… {omitted} more line(s) elided."
        else:
            body = "\n".join(lines)
    else:
        s = trace.summary()
        body = "\n".join([
            f"**Turn trace** — {s['events']} events in {s['duration_s']}s",
            f"- LLM calls: {s['llm']['calls']}"
            f"  ({', '.join(s['llm']['models']) or 'n/a'})",
            f"- Tokens: {s['llm']['input_tokens']} in / "
            f"{s['llm']['output_tokens']} out"
            f"  (cache read {s['llm']['cache_read']})",
            f"- Cost: ${s['llm']['cost_usd']:.6f}",
            f"- Tools: {s['tools']['count']}"
            + (f" ({s['tools']['failed']} failed)" if s['tools']['failed'] else ""),
            f"- Tools used: {', '.join(t for t in s['tools']['names'] if t) or 'none'}",
        ])
        if s["truncated"]:
            body += (
                f"\n\n⚠ {s['dropped_events']} earlier events were dropped; "
                "this trace is incomplete."
            )
        body += "\n\nUse `/trace full` for the step-by-step timeline."

    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=body, metadata={"render_as": "text"},
    )


async def cmd_new(ctx: CommandContext) -> OutboundMessage:
    """Archive the current conversation to long-term memory, then start fresh.

    Unlike ``/clear`` this hands the messages to the memory manager first, so
    the next session can still recall them through long-term recall. The
    compressed (in-context) summary is dropped either way — it belongs to the
    session that is ending.
    """
    loop = ctx.loop
    msg = ctx.msg
    if refusal := check_privileged_in_group(ctx, "/new"):
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content=refusal, metadata={"render_as": "text"},
        )
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
    # A steer queued for the old conversation must not leak into the new one.
    loop.clear_steer(msg.session_key)
    session.clear()
    loop.sessions.save(session)
    loop.sessions.invalidate(session.key)
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=t("cmd.new.started"),
    )


def _compact_keep_messages(loop) -> int:
    """Number of recent messages a manual ``/compact`` leaves in context.

    Mirrors the automatic hook (``agent/compact.py``), which keeps
    ``compaction.auto_compact_keep_recent`` *pairs* — i.e. twice that many
    messages. A hardcoded value here made ``/compact`` archive *more* than an
    automatic compaction would, so the command users reach for to tidy up
    silently dropped more context than the background path.
    """
    config = getattr(loop, "config", None)
    pairs = getattr(getattr(config, "compaction", None),
                    "auto_compact_keep_recent", None)
    if not isinstance(pairs, int) or pairs <= 0:
        pairs = 5
    return pairs * 2


async def cmd_compact(ctx: CommandContext) -> OutboundMessage:
    """Manually trigger memory compaction."""
    loop = ctx.loop
    mm = getattr(loop, "memory_manager", None)
    if not mm:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=t("cmd.compact.no_manager"),
        )
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    history = session.get_history(max_messages=0)
    if not history:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=t("cmd.compact.no_messages"),
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
        keep_recent = _compact_keep_messages(loop)
        archived = max(0, len(session.messages) - keep_recent)
        session.last_consolidated = max(
            session.last_consolidated, archived,
        )
        loop.sessions.save(session)
        # The summary itself lives behind /summary; dumping it here buried the
        # confirmation under a wall of text in every chat surface.
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=t("cmd.compact.complete", archived=archived,
                      kept=min(keep_recent, len(session.messages))),
        )
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=t("cmd.compact.failed"),
    )


async def cmd_compact_str(ctx: CommandContext) -> OutboundMessage:
    """Show the current compressed summary."""
    loop = ctx.loop
    mm = getattr(loop, "memory_manager", None)
    if not mm:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=t("cmd.compact_str.no_manager"),
        )
    session_key = make_session_key(ctx.msg.channel, ctx.msg.chat_id)
    summary = mm.get_compressed_summary(session_key=session_key)
    if not summary:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=t("cmd.compact_str.none"),
        )
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=t("cmd.compact_str.show", summary=summary),
    )


async def cmd_clear(ctx: CommandContext) -> OutboundMessage:
    """Clear conversation history and compressed summary."""
    loop = ctx.loop
    msg = ctx.msg
    if refusal := check_privileged_in_group(ctx, "/clear"):
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content=refusal, metadata={"render_as": "text"},
        )
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    mm = getattr(loop, "memory_manager", None)
    loop.clear_steer(msg.session_key)
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
        content=t("cmd.clear.done"),
    )


def _message_text(message: dict) -> str:
    """Extract plain text from a session message dict."""
    content = message.get("content") or ""
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return content


def _history_to_text(history: list[dict], limit: int = 12) -> str:
    """Render the tail of history as plain text for advisor context."""
    lines: list[str] = []
    for message in history[-limit:]:
        role = message.get("role", "")
        if role not in ("user", "assistant"):
            continue
        text = _message_text(message).strip()
        if text:
            lines.append(f"{role}: {text}")
    return "\n".join(lines)


def _last_user_message(history: list[dict]) -> str:
    """Most recent non-empty user message (used when /moa has no args)."""
    for message in reversed(history):
        if message.get("role") != "user":
            continue
        text = _message_text(message).strip()
        if text:
            return text
    return ""


async def cmd_moa(ctx: CommandContext) -> OutboundMessage:
    """Run Mixture-of-Agents over the recent conversation."""
    loop = ctx.loop
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    history = session.get_history(max_messages=0) or []

    question = ctx.args or _last_user_message(history)
    if not question:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=t("cmd.moa.no_question"),
        )

    from markbot.agent.moa import run_moa

    # MoA answers with prose, not with a status readout, so it is not
    # steerable: nothing inside ``run_moa`` drains ``_pending_steer``. Mark the
    # session so ``/steer`` refuses instead of queueing an instruction that
    # would resurface in an unrelated later turn.
    moa_sessions = getattr(loop, "_moa_sessions", None)
    if moa_sessions is None:
        moa_sessions = loop._moa_sessions = set()
    moa_sessions.add(ctx.msg.session_key)
    try:
        result = await run_moa(question=question, history=_history_to_text(history))
    finally:
        moa_sessions.discard(ctx.msg.session_key)
    if result.final_answer is None:
        if not result.advisors_total:
            content = t("cmd.moa.no_advisors")
        else:
            reason = result.advisor_failures[-1] if result.advisor_failures else "unknown"
            content = t("cmd.moa.failed", error=reason)
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id, content=content,
        )

    content = t(
        "cmd.moa.reply",
        answer=result.final_answer,
        advisors=f"{result.advisors_ok}/{result.advisors_total}",
    )
    # Persist the exchange. A slash command normally short-circuits before the
    # turn is written to history, which is right for /status but wrong here:
    # MoA produces content the user will keep talking about ("expand on the
    # second option"), and without this the next turn cannot see it.
    session.add_message("assistant", result.final_answer)
    loop.sessions.save(session)
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id, content=content,
    )


async def cmd_steer(ctx: CommandContext) -> OutboundMessage:
    """Inject a mid-task instruction into the running agent loop."""
    loop = ctx.loop
    msg = ctx.msg
    steer_text = (ctx.args or "").strip()
    if not steer_text:
        # Echo back the name the user actually typed, so ``/btw`` does not get
        # an error message about a command it never mentioned.
        invoked = (ctx.raw or "").strip().split(maxsplit=1)
        command = invoked[0] if invoked else "/steer"
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content=t("cmd.steer.usage", command=command),
        )
    session_key = msg.session_key
    # /moa counts as a running task but never drains the steer queue; letting
    # it through would replay the instruction inside the next agent turn.
    if session_key in getattr(loop, "_moa_sessions", ()):
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content=t("cmd.steer.moa_running"),
        )
    # Steer is only consumed by ``_inject_pending_steer`` inside an active
    # agent iteration.  If no task is running for this session, queueing
    # the instruction would silently sit in ``_pending_steer`` until the
    # next run starts, which is misleading.  Refuse instead.
    tasks = list(loop._active_tasks.get(session_key, []))
    has_running = any(not t.done() for t in tasks)
    if not has_running:
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content=t("cmd.steer.no_running"),
        )
    accepted = loop.steer(session_key, steer_text)
    if accepted:
        preview = steer_text[:80] + "..." if len(steer_text) > 80 else steer_text
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content=t("cmd.steer.queued", preview=preview),
        )
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id,
        content=t("cmd.steer.rejected"),
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
    msg = ctx.msg
    app_state = getattr(getattr(loop, "ctx", None), "app_state", None)
    if app_state is None:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=t("cmd.mode.no_app_state"),
        )

    arg = (ctx.args or "").strip().lower()
    if not arg:
        # Prefer this session's override so the readout reflects what the
        # user actually gets, not another session's global change.
        current = (
            loop.get_session_permission_mode(ctx.msg.session_key)
            if callable(getattr(loop, "get_session_permission_mode", None))
            else None
        ) or app_state.get_permission_mode()
        lines = [
            t("cmd.mode.modes_header"),
            t("cmd.mode.mode_default"),
            t("cmd.mode.mode_plan"),
            t("cmd.mode.mode_accept_edits"),
            t("cmd.mode.mode_auto"),
            t("cmd.mode.mode_bypass"),
            "",
            t("cmd.mode.current", mode=current.value),
            t("cmd.mode.usage_hint"),
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
            content=t("cmd.mode.unknown", arg=arg, valid=", ".join(sorted(_MODE_ALIASES))),
        )

    # Only an authorised sender may change the process-global permission
    # mode. Checked *after* validating the argument so a typo still gets a
    # helpful message, and *before* any state change or config write.
    if refusal := check_privileged(ctx, "/mode"):
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=refusal, metadata={"render_as": "text"},
        )

    app_state.set_permission_mode(mode)
    # Scope the change to the calling session. Writing only the global mode
    # would silently change the tool policy for every other conversation
    # sharing this daemon. The global is still updated (via the loop helper)
    # so it persists across restarts and shows up in ``/mode`` with no args.
    if setter := getattr(loop, "set_session_permission_mode", None):
        try:
            setter(msg.session_key, mode)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Failed to scope permission mode to session: {}", exc)
    # Persist to config.json so the choice survives restarts. Best-effort:
    # the in-memory switch above already took effect, so a failed write
    # only means the user will need to /mode again after next restart.
    # Key path uses camelCase to match the JSON alias (schema Base uses
    # to_camel alias_generator), not the snake_case Python field name.
    from markbot.config.loader import update_config_value
    persisted = update_config_value(
        ["agents", "defaults", "defaultPermissionMode"], mode.value,
    )
    content = t("cmd.mode.set_to", mode=mode.value)
    if not persisted:
        content += t("cmd.mode.persist_failed")
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
    from markbot.config.profile import DEFAULT_PROFILE, get_profile, list_profiles

    loop = ctx.loop
    config = getattr(loop, "config", None)
    # ``DEFAULT_PROFILE`` (assistant) is the same value ``get_profile()``
    # resolves to; the old hardcoded "coding" fallback made /profile report a
    # profile the process was not actually running.
    current = DEFAULT_PROFILE
    try:
        current = config.agents.defaults.profile if config else DEFAULT_PROFILE
    except Exception:
        current = getattr(getattr(loop, "ctx", None), "profile", None)
        current = getattr(current, "name", None) or DEFAULT_PROFILE

    arg = (ctx.args or "").strip().lower()
    if not arg:
        lines = [t("cmd.profile.header")]
        for name in list_profiles():
            p = get_profile(name)
            mark = t("cmd.profile.current_mark") if name == current else ""
            lines.append(t("cmd.profile.profile_line", name=name, mark=mark, desc=p.description))
            lines.append(
                t("cmd.profile.profile_detail",
                  permission=p.permission_mode, desktop=p.enable_desktop,
                  browser=p.enable_browser, autopilot=p.enable_autopilot,
                  min_skill_score=p.min_skill_score)
            )
        lines.append("")
        lines.append(t("cmd.profile.usage_hint"))
        # Show live footprint profile when available.
        live = None
        try:
            live = getattr(getattr(loop, "tool_footprint", None), "profile_name", None)
        except Exception:
            live = None
        if live and live != current:
            lines.append(t("cmd.profile.live_profile", name=live))
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="\n".join(lines),
            metadata={"render_as": "text"},
        )

    if arg not in list_profiles():
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=t("cmd.profile.unknown", arg=arg, valid=", ".join(list_profiles())),
        )

    # Writing a profile is at least as privileged as /mode: it persists a
    # process-global default and switches the live permission mode, and some
    # profiles raise it (``unattended`` grants auto-approval). Gating only
    # /mode would leave an equivalent door open, so the same authorisation
    # check runs here. Checked after argument validation so a typo still gets
    # a helpful message, and before any state change or config write.
    if refusal := check_privileged(ctx, "/profile"):
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=refusal, metadata={"render_as": "text"},
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
    content = t("cmd.profile.set_to", name=arg, permission=profile.permission_mode)
    if not persisted:
        content += t("cmd.profile.persist_failed")
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id, content=content,
    )


def build_builtin_catalog() -> "CommandCatalog":  # noqa: F821
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
            aliases=("/btw",),
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
            name="/trace",
            handler=cmd_trace,
            description="Show the last turn's decision trace (add `full` for the timeline)",
            tier=CommandTier.PRIORITY,
            args_hint="[full]",
            category="session",
            control_plane=True,
        ),
        CommandDef(
            name="/mode",
            handler=cmd_mode,
            description="Show or set permission mode "
                        "(default/plan/accept_edits/auto/bypass)",
            tier=CommandTier.PRIORITY,
            args_hint="[mode]",
            category="runtime",
            control_plane=True,
        ),
        CommandDef(
            name="/profile",
            handler=cmd_profile,
            description="Show or set runtime profile (coding/assistant/unattended)",
            tier=CommandTier.PRIORITY,
            args_hint="[name]",
            category="runtime",
            control_plane=True,
        ),
        CommandDef(
            name="/help",
            handler=cmd_help,
            description="Show available slash commands",
            tier=CommandTier.PRIORITY,
            category="general",
            control_plane=True,
        ),
        CommandDef(
            name="/compact_str",
            handler=cmd_compact_str,
            description="View current compressed summary",
            tier=CommandTier.PRIORITY,
            aliases=("/summary",),
            category="session",
            control_plane=True,
        ),
        CommandDef(
            name="/new",
            handler=cmd_new,
            description="Archive this session to long-term memory and start fresh",
            category="session",
        ),
        CommandDef(
            name="/compact",
            handler=cmd_compact,
            description="Force manual context compaction",
            category="session",
        ),
        CommandDef(
            name="/clear",
            handler=cmd_clear,
            description="Clear history and compressed summary",
            category="session",
        ),
        CommandDef(
            name="/moa",
            handler=cmd_moa,
            description="Mixture-of-Agents: parallel advisors + aggregator",
            args_hint="[question]",
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
