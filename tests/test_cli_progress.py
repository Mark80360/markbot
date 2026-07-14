"""Tests for the CLI TOOL_PROGRESS subscriber.

These verify the minimum-viable consumer in :mod:`markbot.cli.progress`
that bridges bus events to loguru. The subscriber is process-global
(its registration guard is module-level), so tests must reset that
state between cases via the exposed ``reset_for_testing`` helper.
"""

from __future__ import annotations

import asyncio

import pytest

from markbot.bus.emitter import get_event_emitter, reset_event_emitter
from markbot.bus.events import Event, EventType


@pytest.fixture(autouse=True)
def _reset_singleton():
    """The emitter and the subscriber's ``_registered`` guard are
    process-global. Reset both before and after each test so cases
    can't leak state into each other."""
    from markbot.cli import progress as progress_mod

    progress_mod.reset_for_testing()
    reset_event_emitter()
    yield
    progress_mod.reset_for_testing()
    reset_event_emitter()


def test_register_is_idempotent():
    """Calling ``register_progress_subscriber`` twice must register the
    subscriber exactly once. The bus holds a list; a duplicate would
    log every event twice, so the guard exists for a reason."""
    from markbot.cli.progress import register_progress_subscriber

    assert register_progress_subscriber() is True
    assert register_progress_subscriber() is False


def test_subscriber_logs_progress_event(caplog):
    """Emitting a TOOL_PROGRESS event must reach the registered
    subscriber, which forwards it to loguru at DEBUG level."""
    import logging

    from markbot.cli.progress import register_progress_subscriber

    register_progress_subscriber()

    payload = {
        "text": "Collecting requests>=2.31",
        "percent": None,
        "tools": ["run_code"],
        "channel": "cli",
        "chat_id": "default",
    }

    async def emit():
        emitter = get_event_emitter()
        await emitter.emit(EventType.TOOL_PROGRESS, payload, session_key="cli:default")

    # loguru pushes to its own sinks; the cleanest way to assert that
    # the handler ran is to monkey-patch the module's logger and capture
    # the call. The handler swallows all errors, so a non-throwing
    # return value is also a pass condition.
    from markbot.cli import progress as progress_mod
    captured: list[str] = []
    orig_debug = progress_mod.logger.debug

    def fake_debug(fmt, *args):
        captured.append(fmt % args if args else fmt)
        return orig_debug(fmt, *args)

    progress_mod.logger.debug = fake_debug  # type: ignore[assignment]
    try:
        asyncio.run(emit())
    finally:
        progress_mod.logger.debug = orig_debug  # type: ignore[assignment]

    assert any("Collecting requests" in line for line in captured), (
        f"Expected progress text in loguru output, got: {captured!r}"
    )


def test_subscriber_silent_on_unrelated_events():
    """A subscriber for TOOL_PROGRESS must NOT be called for other
    event types — cross-event leakage would inflate the log file."""
    from markbot.cli import progress as progress_mod
    from markbot.cli.progress import register_progress_subscriber

    register_progress_subscriber()
    captured: list[str] = []
    orig_debug = progress_mod.logger.debug
    progress_mod.logger.debug = lambda fmt, *a: captured.append(fmt % a if a else fmt)  # type: ignore[assignment]

    async def emit():
        emitter = get_event_emitter()
        await emitter.emit(EventType.TOOL_CALLED, {"name": "read_file"})

    try:
        asyncio.run(emit())
    finally:
        progress_mod.logger.debug = orig_debug  # type: ignore[assignment]

    # Only the "subscriber registered" line should be in the log; the
    # TOOL_CALLED payload text must not appear.
    assert not any("read_file" in line for line in captured), captured
