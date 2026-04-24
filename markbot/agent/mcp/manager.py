"""MCP server connection manager.

Extracted from agent/loop.py to isolate MCP lifecycle concerns.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from markbot.tools.registry import ToolRegistry


class McpManager:
    """Manages MCP server connections with race-condition protection.

    Responsibilities:
    - One-time lazy connection to configured MCP servers
    - Clean shutdown with background task draining
    - Thread/async-safe guard against concurrent connection attempts
    """

    def __init__(self, mcp_servers: dict[str, Any] | None = None) -> None:
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._connected = False
        self._connecting = False
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._background_tasks: list[asyncio.Task] = []

    @property
    def is_connected(self) -> bool:
        """Whether MCP servers are currently connected."""
        return self._connected

    async def connect(self, tool_registry: ToolRegistry) -> None:
        """Connect to configured MCP servers (one-time, lazy).

        Safe to call concurrently — only the first caller performs the
        connection; subsequent calls are no-ops.
        """
        if self._connected or not self._mcp_servers:
            return

        async with self._lock:
            if self._connected or self._connecting:
                return
            self._connecting = True

        from markbot.tools.mcp import connect_mcp_servers  # noqa: PLC0415

        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            await connect_mcp_servers(self._mcp_servers, tool_registry, self._mcp_stack)
            self._connected = True
            self._ready.set()
        except BaseException as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._connecting = False

    async def close(self) -> None:
        """Drain pending background tasks, then close MCP connections."""
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None
        self._connected = False

    def schedule_background(self, coro: Any) -> asyncio.Task:
        """Schedule a tracked background task (drained on close)."""
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)
        task.add_done_callback(lambda t: self._background_tasks.remove(t))
        return task

    async def wait_ready(self, timeout: float | None = None) -> bool:
        """Wait until MCP is connected or timeout. Returns connected state."""
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except (asyncio.TimeoutError, Exception):
            pass
        return self._connected
