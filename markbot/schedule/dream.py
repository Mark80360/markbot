"""Dream scheduler — periodic AI-driven memory optimization.

Extracted from cli/commands.py to decouple dream scheduling from
the gateway lifecycle.  Can be used standalone anywhere the agent runs.

Dream is **system-triggered only** — there is no manual entry point.
The scheduler runs an independent cron loop, persists its state between
runs (so restarts don't lose continuity), and refuses to fire while a
conversation is in progress (via the ``is_busy_fn`` callback).

Design goals (per project requirements):
  1. Independence — DreamService owns its loop; does not depend on
     CronService or the gateway.  It can be registered *as* a cron
     schedule via :meth:`as_cron_schedule`, but its primary mode is
     self-contained ``start()`` / ``stop()``.
  2. System-only trigger — no public ``run_now()``; the loop is the
     only entry point.
  3. Logging — every step and every exception is logged; ``stop()``
     only swallows ``CancelledError``.
  4. No-conversation guard — before firing, ``is_busy_fn()`` is
     consulted; if it returns ``True`` the run is deferred to the next
     cron tick and the deferral is logged.
  5. Continuity — state (last_run_at, last_status, last_summary,
     run_count, last_deferred_at) is persisted to ``.dream_state`` so
     a restart picks up where the previous process left off.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

_STATE_FILENAME = ".dream_state"


def _default_state() -> dict[str, Any]:
    return {
        "last_run_at": None,           # ISO 8601 of the last successful run
        "last_status": None,           # "ok" | "error" | "deferred"
        "last_summary": None,          # short human-readable result
        "last_error": None,            # exception text if last_status == "error"
        "run_count": 0,                # total successful runs
        "last_deferred_at": None,      # ISO 8601 of the last deferral (busy)
        "deferred_count": 0,           # total times deferred due to busy agent
    }


def _load_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return _default_state()
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            base = _default_state()
            base.update({k: v for k, v in data.items() if k in base})
            return base
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to read dream state ({}): {}", state_path, e)
    return _default_state()


def _save_state(state_path: Path, data: dict[str, Any]) -> None:
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning("Failed to save dream state ({}): {}", state_path, e)


# ---------------------------------------------------------------------------
# DreamService
# ---------------------------------------------------------------------------


class DreamService:
    """Periodically triggers memory optimisation (Dream) on a cron schedule.

    Usage::

        service = DreamService(
            cron_expr="0 3 * * *",
            dream_fn=memory_manager.dream,
            state_dir=workspace,
            is_busy_fn=lambda: agent.has_active_conversations,
            timezone="Asia/Shanghai",
        )
        await service.start()
        ...
        await service.stop()

    The service is **system-triggered only**.  There is no public method
    to force a run — the cron loop is the sole entry point.
    """

    def __init__(
        self,
        cron_expr: str,
        dream_fn: Callable[[], Awaitable[object]],
        *,
        state_dir: str | Path | None = None,
        is_busy_fn: Callable[[], bool] | None = None,
        timezone: str = "UTC",
    ) -> None:
        self._cron_expr = cron_expr
        self._dream_fn = dream_fn
        self._timezone = timezone
        self._is_busy_fn = is_busy_fn or (lambda: False)
        self._task: asyncio.Task[None] | None = None
        self._running = False
        # Serialise dream executions so a slow run can't overlap the next
        # cron tick.  System-only triggering makes overlap unlikely, but a
        # long LLM call straddling two ticks is still possible.
        self._lock = asyncio.Lock()
        # State file lives alongside other markbot state files.
        self._state_path = (
            Path(state_dir) / _STATE_FILENAME if state_dir else None
        )
        self._state: dict[str, Any] = _default_state()
        if self._state_path is not None:
            self._state = _load_state(self._state_path)

    # -- Public API ---------------------------------------------------------

    def as_cron_schedule(self) -> dict:
        """Return a CronSchedule-compatible dict for use with CronService.

        Kept for callers that prefer to register via CronService instead of
        running the built-in loop.  When registered this way the caller is
        responsible for the ``is_busy`` guard and state persistence.
        """
        return {"kind": "cron", "expr": self._cron_expr, "tz": self._timezone}

    async def start(self) -> None:
        """Start the dream scheduler loop."""
        if self._running:
            logger.warning("DreamService.start() called but already running")
            return
        if not self._cron_expr:
            logger.info("Dream disabled (empty cron expression)")
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info(
            "Dream scheduler started (cron={}, tz={}, state={})",
            self._cron_expr,
            self._timezone,
            self._state_path or "<in-memory>",
        )

    async def stop(self) -> None:
        """Stop the dream scheduler and wait for the task to finish.

        Only ``CancelledError`` is swallowed — every other exception that
        escapes the loop has already been logged inside ``_run()``.
        """
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                # Defensive: _run() already logged this, but if something
                # slipped through we record it here rather than silently
                # dropping it.
                logger.error("DreamService task exited with error: {}", e)
            self._task = None
        logger.info("Dream scheduler stopped")

    # -- Internal -----------------------------------------------------------

    def _persist_state(self) -> None:
        if self._state_path is not None:
            _save_state(self._state_path, self._state)

    async def _run(self) -> None:
        """Main cron loop — computes next fire time, waits, then runs."""
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        from croniter import croniter  # noqa: PLC0415

        tz = ZoneInfo(self._timezone)
        while self._running:
            try:
                now = datetime.now(tz=tz)
                cron_iter = croniter(self._cron_expr, now)
                next_ts = cron_iter.get_next(float)
                delay = next_ts - now.timestamp()
                if delay < 0:
                    delay = 0
                next_dt = datetime.fromtimestamp(next_ts, tz=tz)
                logger.info("Next dream at {} (in {:.0f}s)", next_dt, delay)
                await asyncio.sleep(delay)
                if not self._running:
                    break
                await self._maybe_run()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Dream scheduler iteration failed: {}", e)
                # Brief back-off so a persistent error doesn't spin the CPU.
                await asyncio.sleep(60)

    async def _maybe_run(self) -> None:
        """Guard with the busy check, then execute one dream cycle.

        The ``asyncio.Lock`` ensures that if a previous run is still in
        flight (e.g. a slow LLM call straddled two cron ticks) the new
        tick is skipped rather than queued — queuing would just delay the
        next real tick and pile up work.
        """
        if self._lock.locked():
            logger.warning(
                "Dream skipped: previous run still in progress "
                "(will retry on next cron tick)"
            )
            return

        # Conversation-in-progress guard (requirement: never run during a
        # conversation).  We check *before* acquiring the lock so a busy
        # agent never blocks the lock.
        if self._is_busy_fn():
            now_iso = datetime.now(timezone.utc).isoformat()
            self._state["last_status"] = "deferred"
            self._state["last_deferred_at"] = now_iso
            self._state["deferred_count"] = (
                self._state.get("deferred_count", 0) + 1
            )
            self._persist_state()
            logger.info(
                "Dream deferred: agent has active conversations "
                "(deferred_count={})",
                self._state["deferred_count"],
            )
            return

        async with self._lock:
            await self._execute_dream()

    async def _execute_dream(self) -> None:
        """Call the dream function and record the outcome."""
        started_at = datetime.now(timezone.utc)
        logger.info("Triggering memory optimisation (dream)")
        try:
            result = await self._dream_fn()
            summary = str(result) if result is not None else "completed"
            self._state["last_run_at"] = started_at.isoformat()
            self._state["last_status"] = "ok"
            self._state["last_summary"] = summary[:500]
            self._state["last_error"] = None
            self._state["run_count"] = self._state.get("run_count", 0) + 1
            self._persist_state()
            logger.info(
                "Dream completed successfully (run_count={}, summary={})",
                self._state["run_count"],
                summary[:200],
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._state["last_run_at"] = started_at.isoformat()
            self._state["last_status"] = "error"
            self._state["last_error"] = str(e)[:1000]
            self._persist_state()
            logger.error("Dream optimisation failed: {}", e)
            # Re-raise is unnecessary — the loop's outer except will not
            # see it because we caught it here.  We log + persist so the
            # failure is visible in .dream_state and logs.


__all__ = ["DreamService"]
