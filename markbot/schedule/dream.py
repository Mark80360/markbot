"""Dream scheduler — periodic AI-driven memory optimization.

Extracted from cli/commands.py to decouple dream scheduling from
the gateway lifecycle.  Can be used standalone anywhere the agent runs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime

from loguru import logger


class DreamService:
    """Periodically triggers memory optimisation (Dream) on a cron schedule.

    Usage::

        service = DreamService("0 3 * * *", memory_manager.dream, tz="Asia/Shanghai")
        await service.start()
        ...
        await service.stop()
    """

    def __init__(
        self,
        cron_expr: str,
        dream_fn: Callable[[], Awaitable[object]],
        timezone: str = "UTC",
    ) -> None:
        self._cron_expr = cron_expr
        self._dream_fn = dream_fn
        self._timezone = timezone
        self._task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        """Start the dream scheduler loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info(
            "[Dream] Scheduler started (cron={}, tz={})",
            self._cron_expr, self._timezone,
        )

    async def stop(self) -> None:
        """Stop the dream scheduler and wait for the task to finish."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        from croniter import croniter  # noqa: PLC0415
        from zoneinfo import ZoneInfo  # noqa: PLC0415

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
                logger.info("[Dream] Next dream at {} (in {:.0f}s)", next_dt, delay)
                await asyncio.sleep(delay)
                if not self._running:
                    break
                logger.info("[Dream] Triggering memory optimisation")
                await self._dream_fn()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[Dream] Optimisation failed: {}", e)
                await asyncio.sleep(60)
