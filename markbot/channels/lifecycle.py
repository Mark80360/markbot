"""Unified channel lifecycle management.

Extracted from channels/manager.py to separate concerns:
- manager.py: start/stop/dispatch (coordination)
- lifecycle.py: health checks, auto-reconnect, retry policy

Every channel gets a managed background task that periodically
checks its health and attempts reconnection on failure.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from markbot.channels.base import BaseChannel


class ChannelLifecycle:
    """Manages start/stop/health-check/auto-reconnect for a single channel.

    Wraps a ``BaseChannel`` and provides:

    - Controlled startup with timeout
    - Periodic health checks
    - Exponential-backoff auto-reconnect
    - Clean shutdown with resource cleanup
    """

    _INITIAL_RETRY_DELAY_S = 5
    _MAX_RETRY_DELAY_S = 300
    _HEALTH_CHECK_INTERVAL_S = 60

    def __init__(self, channel: BaseChannel) -> None:
        self.channel = channel
        self._running = False
        self._monitor_task: asyncio.Task[None] | None = None
        self._retry_delay = self._INITIAL_RETRY_DELAY_S
        self._consecutive_failures = 0

    @property
    def name(self) -> str:
        return self.channel.name

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self, timeout: float = 30.0) -> bool:
        """Start the channel and begin health monitoring.

        Returns True if the channel started successfully.
        """
        if self._running:
            return True
        try:
            await asyncio.wait_for(self.channel.start(), timeout=timeout)
            self._running = True
            self._consecutive_failures = 0
            self._retry_delay = self._INITIAL_RETRY_DELAY_S
            # Start background health monitor
            self._monitor_task = asyncio.create_task(self._monitor_loop())
            logger.info("[Lifecycle] {} started", self.channel.display_name)
            return True
        except asyncio.TimeoutError:
            logger.error("[Lifecycle] {} start timed out after {}s", self.channel.display_name, timeout)
            return False
        except Exception as e:
            logger.error("[Lifecycle] {} start failed: {}", self.channel.display_name, e)
            return False

    async def stop(self) -> None:
        """Stop the channel and cancel health monitoring."""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except (asyncio.CancelledError, Exception):
                pass
            self._monitor_task = None
        try:
            await asyncio.wait_for(self.channel.stop(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("[Lifecycle] {} stop timed out", self.channel.display_name)
        except Exception as e:
            logger.warning("[Lifecycle] {} stop error: {}", self.channel.display_name, e)
        logger.info("[Lifecycle] {} stopped", self.channel.display_name)

    async def health(self) -> dict[str, Any]:
        """Run a health check and return the result dict."""
        try:
            result = await asyncio.wait_for(self.channel.health_check(), timeout=10.0)
            return result
        except asyncio.TimeoutError:
            return {"healthy": False, "latency_ms": None, "error": "health check timed out", "details": {}}
        except Exception as e:
            return {"healthy": False, "latency_ms": None, "error": str(e), "details": {}}

    async def _monitor_loop(self) -> None:
        """Periodic health check with exponential-backoff auto-reconnect."""
        while self._running:
            await asyncio.sleep(self._HEALTH_CHECK_INTERVAL_S)
            if not self._running:
                break

            status = await self.health()
            if status.get("healthy"):
                self._consecutive_failures = 0
                self._retry_delay = self._INITIAL_RETRY_DELAY_S
                continue

            self._consecutive_failures += 1
            logger.warning(
                "[Lifecycle] {} unhealthy ({} consecutive): {} — reconnecting in {}s",
                self.channel.display_name,
                self._consecutive_failures,
                status.get("error", "unknown"),
                self._retry_delay,
            )

            await asyncio.sleep(self._retry_delay)
            if not self._running:
                break

            logger.info("[Lifecycle] {} reconnecting...", self.channel.display_name)
            try:
                await asyncio.wait_for(self.channel.stop(), timeout=10.0)
            except Exception:
                pass
            try:
                await asyncio.wait_for(self.channel.start(), timeout=30.0)
                logger.info("[Lifecycle] {} reconnected", self.channel.display_name)
                self._consecutive_failures = 0
                self._retry_delay = self._INITIAL_RETRY_DELAY_S
            except Exception as e:
                logger.error("[Lifecycle] {} reconnect failed: {}", self.channel.display_name, e)
                self._retry_delay = min(self._retry_delay * 2, self._MAX_RETRY_DELAY_S)
