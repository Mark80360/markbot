"""Channel manager for coordinating chat channels."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from markbot.bus.events import OutboundMessage
from markbot.bus.queue import MessageBus
from markbot.channels.base import BaseChannel
from markbot.config.schema import Config

# Retry delays for message sending (exponential backoff: 1s, 2s, 4s)
_SEND_RETRY_DELAYS = (1, 2, 4)

# Health check interval in seconds
_HEALTH_CHECK_INTERVAL_S = 60


class ChannelManager:
    """
    Manages chat channels and coordinates message routing.

    Responsibilities:
    - Start/stop channels
    - Route outbound messages
    - Health checking with auto-restart
    """

    # Auto-restart thresholds
    _MAX_CONSECUTIVE_FAILURES = 3
    _RESTART_COOLDOWN_S = 300  # 5 minutes between restart attempts

    def __init__(self, config: Config, bus: MessageBus):
        self.config = config
        self.bus = bus
        self.channels: dict[str, BaseChannel] = {}
        self._dispatch_task: asyncio.Task | None = None
        self._health_check_task: asyncio.Task | None = None
        self._consecutive_failures: dict[str, int] = {}
        self._last_restart: dict[str, float] = {}
        self._restarting: dict[str, bool] = {}

        self._init_channels()

    def _init_channels(self) -> None:
        """Initialize channels discovered via pkgutil scan + entry_points plugins."""
        from markbot.channels.discovery import discover_all

        groq_key = self.config.providers.groq.api_key

        for name, cls in discover_all().items():
            section = getattr(self.config.channels, name, None)
            if section is None:
                continue
            enabled = (
                section.get("enabled", False)
                if isinstance(section, dict)
                else getattr(section, "enabled", False)
            )
            if not enabled:
                continue
            try:
                channel = cls(section, self.bus)
                channel.transcription_api_key = groq_key
                self.channels[name] = channel
                logger.info("{} channel enabled", cls.display_name)
            except Exception as e:
                logger.warning("{} channel not available: {}", name, e)

        self._validate_allow_from()

    def _validate_allow_from(self) -> None:
        for name, ch in self.channels.items():
            if getattr(ch.config, "allow_from", None) == []:
                raise SystemExit(
                    f'Error: "{name}" has empty allowFrom (denies all). '
                    f'Set ["*"] to allow everyone, or add specific user IDs.'
                )

    async def _start_channel(self, name: str, channel: BaseChannel) -> None:
        """Start a channel and log any exceptions."""
        try:
            await channel.start()
        except Exception as e:
            logger.error("Failed to start channel {}: {}", name, e)

    async def start_all(self) -> None:
        """Start all channels and the outbound dispatcher."""
        if not self.channels:
            logger.warning("No channels enabled")
            return

        # Start outbound dispatcher
        self._dispatch_task = asyncio.create_task(self._dispatch_outbound())

        # Start health check loop
        self._health_check_task = asyncio.create_task(self._health_check_loop())

        # Start channels
        tasks = []
        for name, channel in self.channels.items():
            logger.info("Starting {} channel...", name)
            tasks.append(asyncio.create_task(self._start_channel(name, channel)))

        # Wait for all to complete (they should run forever)
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop_all(self) -> None:
        """Stop all channels and the dispatcher."""
        logger.info("Stopping all channels...")

        # Stop dispatcher
        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass

        # Stop health check loop
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

        # Stop all channels
        for name, channel in self.channels.items():
            try:
                await channel.stop()
                logger.info("Stopped {} channel", name)
            except Exception as e:
                logger.error("Error stopping {}: {}", name, e)

    async def _dispatch_outbound(self) -> None:
        """Dispatch outbound messages to the appropriate channel."""
        logger.info("Outbound dispatcher started")

        while True:
            try:
                msg = await asyncio.wait_for(self.bus.consume_outbound(), timeout=1.0)

                if msg.metadata.get("_progress"):
                    if msg.metadata.get("_tool_hint") and not self.config.channels.send_tool_hints:
                        continue
                    if (
                        not msg.metadata.get("_tool_hint")
                        and not self.config.channels.send_progress
                    ):
                        continue

                channel = self.channels.get(msg.channel)
                if channel:
                    await self._send_with_retry(channel, msg)
                else:
                    logger.warning("Unknown channel: {}", msg.channel)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    @staticmethod
    async def _send_once(channel: BaseChannel, msg: OutboundMessage) -> None:
        """Send one outbound message without retry policy."""
        is_stream_end = msg.metadata.get("_stream_end")
        is_stream_delta = msg.metadata.get("_stream_delta")
        is_streamed = msg.metadata.get("_streamed")
        if is_stream_end:
            logger.info(
                "[MANAGER] stream end: chat_id={}",
                msg.chat_id,
            )
        elif is_stream_delta:
            logger.debug(
                "[MANAGER] stream delta: chat_id={}",
                msg.chat_id,
            )
        else:
            logger.info(
                "[MANAGER] send: chat_id={}, streamed={}",
                msg.chat_id,
                is_streamed,
            )
        if is_stream_delta or is_stream_end:
            await channel.send_delta(msg.chat_id, msg.content, msg.metadata)
        elif not is_streamed:
            await channel.send(msg)

    async def _send_with_retry(self, channel: BaseChannel, msg: OutboundMessage) -> None:
        """Send a message with retry on failure using exponential backoff.

        Stream delta/end messages are sent without retry to avoid out-of-order
        delivery, since they are ordered and must arrive in sequence.

        Note: CancelledError is re-raised to allow graceful shutdown.
        """
        # Stream deltas are ordered; retrying would cause out-of-order delivery.
        if msg.metadata.get("_stream_delta") or msg.metadata.get("_stream_end"):
            try:
                await self._send_once(channel, msg)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    "Stream send to {} failed (no retry): {} - {}",
                    msg.channel,
                    type(e).__name__,
                    e,
                )
            return

        max_attempts = max(self.config.channels.send_max_retries, 1)

        for attempt in range(max_attempts):
            try:
                await self._send_once(channel, msg)
                return  # Send succeeded
            except asyncio.CancelledError:
                raise  # Propagate cancellation for graceful shutdown
            except Exception as e:
                if attempt == max_attempts - 1:
                    logger.error(
                        "Failed to send to {} after {} attempts: {} - {}",
                        msg.channel,
                        max_attempts,
                        type(e).__name__,
                        e,
                    )
                    return
                delay = _SEND_RETRY_DELAYS[min(attempt, len(_SEND_RETRY_DELAYS) - 1)]
                logger.warning(
                    "Send to {} failed (attempt {}/{}): {}, retrying in {}s",
                    msg.channel,
                    attempt + 1,
                    max_attempts,
                    type(e).__name__,
                    delay,
                )
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    raise  # Propagate cancellation during sleep

    def get_channel(self, name: str) -> BaseChannel | None:
        """Get a channel by name."""
        return self.channels.get(name)

    def get_status(self) -> dict[str, Any]:
        """Get status of all channels including health and restart info."""
        return {
            name: {
                "enabled": True,
                "running": channel.is_running,
                "consecutive_failures": self._consecutive_failures.get(name, 0),
                "restarting": self._restarting.get(name, False),
            }
            for name, channel in self.channels.items()
        }

    @property
    def enabled_channels(self) -> list[str]:
        """Get list of enabled channel names."""
        return list(self.channels.keys())

    async def _health_check_loop(self) -> None:
        """Periodically check health of all channels with auto-restart."""
        await asyncio.sleep(10)  # Wait 10s for channels to start

        while True:
            try:
                for name, channel in self.channels.items():
                    if self._restarting.get(name):
                        continue

                    try:
                        result = await channel.health_check()
                        if result.get("healthy", False):
                            logger.debug("Health check passed for {}", name)
                            self._consecutive_failures[name] = 0
                        else:
                            error = result.get("error", "Unknown error")
                            self._consecutive_failures[name] = (
                                self._consecutive_failures.get(name, 0) + 1
                            )
                            failures = self._consecutive_failures[name]
                            logger.warning(
                                "Health check failed for {} ({}/{}): {}",
                                name,
                                failures,
                                self._MAX_CONSECUTIVE_FAILURES,
                                error,
                            )
                            if failures >= self._MAX_CONSECUTIVE_FAILURES:
                                await self._try_restart_channel(name, channel)
                    except Exception as e:
                        logger.error("Health check error for {}: {}", name, e)
                        self._consecutive_failures[name] = (
                            self._consecutive_failures.get(name, 0) + 1
                        )
                        failures = self._consecutive_failures[name]
                        if failures >= self._MAX_CONSECUTIVE_FAILURES:
                            await self._try_restart_channel(name, channel)

                await asyncio.sleep(_HEALTH_CHECK_INTERVAL_S)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Health check loop error: {}", e)
                await asyncio.sleep(_HEALTH_CHECK_INTERVAL_S)

    async def _try_restart_channel(self, name: str, channel: BaseChannel) -> None:
        """Attempt to restart a failing channel with cooldown."""
        import time

        now = time.monotonic()
        last = self._last_restart.get(name, 0)
        if now - last < self._RESTART_COOLDOWN_S:
            logger.info(
                "Skipping restart for {} — cooldown ({:.0f}s remaining)",
                name,
                self._RESTART_COOLDOWN_S - (now - last),
            )
            return

        self._restarting[name] = True
        logger.info("Attempting to restart {} channel...", name)
        try:
            await channel.restart()
            self._consecutive_failures[name] = 0
            self._last_restart[name] = time.monotonic()
            logger.info("Successfully restarted {} channel", name)
        except Exception as e:
            logger.error("Failed to restart {} channel: {}", name, e)
            self._last_restart[name] = time.monotonic()
        finally:
            self._restarting[name] = False
