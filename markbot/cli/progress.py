"""CLI-side subscriber for ``TOOL_PROGRESS`` events.

The agent loop in :mod:`markbot.agent.iteration` emits a
``TOOL_PROGRESS`` event for every line of stdout streamed by a
long-running tool (e.g. ``run_code`` doing ``pip install``). Without
a subscriber the event is fire-and-forget — the UI never sees the
progress and the user is left staring at a spinner.

This module is the minimum-viable consumer: it taps the global
:class:`~markbot.bus.emitter.EventEmitter` and pipes each event into
loguru at ``DEBUG`` level. The CLI already routes loguru output to a
per-session log file via :func:`markbot.log.core.setup_logging`, so
progress becomes visible in the log out of the box, and downstream
consumers (TUI v3, the web SSE bridge) can subscribe a richer
renderer later without changing the producer.

The function is idempotent: calling it twice registers the subscriber
exactly once. The first call returns ``True`` (registered), the second
returns ``False`` (already registered) so callers can log it
diagnostically.
"""

from __future__ import annotations

from loguru import logger

from markbot.bus.emitter import get_event_emitter
from markbot.bus.events import Event, EventType

__all__ = ["register_progress_subscriber", "is_registered"]

# A single module-level guard is enough: subscribers are process-global
# (the EventEmitter is a singleton) and the CLI is single-instance per
# process. A re-entrant guard prevents the second ``markbot agent``
# invocation in the same Python process from stacking duplicate
# subscribers (which would each log the same line).
_registered: bool = False


def _on_tool_progress(event: Event) -> None:
    """Forward a TOOL_PROGRESS event to loguru at DEBUG.

    The payload is shaped by :class:`markbot.agent.iteration
    .IterationRunner._report_progress` and carries:
      - ``text``     : the streamed line (or humanised hint)
      - ``percent``  : optional progress fraction; ``None`` for streaming
      - ``tools``    : the list of tool names active in this turn
      - ``channel`` / ``chat_id`` : routing identifiers

    We build the final line with string concatenation rather than
    loguru's ``{}`` formatter: streamed tool output can contain
    literal ``{`` / ``%`` / ``}`` characters (e.g. ``pip`` progress
    bars) that would either confuse ``str.format`` or trip loguru's
    percent-formatter path. Concatenation is the safe, side-effect
    free choice.
    """
    payload = event.payload or {}
    text = payload.get("text", "")
    tools = payload.get("tools") or []
    channel = payload.get("channel", "")
    chat_id = payload.get("chat_id", "")
    prefix = "[tool-progress]"
    if tools:
        prefix = prefix + " " + ",".join(tools)
    if channel and chat_id:
        prefix = prefix + " " + channel + ":" + chat_id
    # DEBUG because a long install can emit thousands of lines; the log
    # file is the right sink. UI integrations that want to surface this
    # in the terminal should subscribe their own renderer at INFO/WARN.
    logger.debug(prefix + " " + str(text))


def register_progress_subscriber() -> bool:
    """Register the TOOL_PROGRESS -> loguru bridge. Returns True on first
    registration, False on subsequent calls (no-op)."""
    global _registered
    if _registered:
        return False
    emitter = get_event_emitter()
    emitter.on(EventType.TOOL_PROGRESS, _on_tool_progress)
    _registered = True
    logger.debug("TOOL_PROGRESS subscriber registered (cli.progress)")
    return True


def is_registered() -> bool:
    """Test/diagnostic accessor: True iff the subscriber is active."""
    return _registered


def reset_for_testing() -> None:
    """Clear the registration guard. Test-only — production code should
    not unregister, because the EventEmitter has no symmetric
    ``off(EventType, handler)`` API and the subscriber is intended to
    live for the whole process."""
    global _registered
    _registered = False
