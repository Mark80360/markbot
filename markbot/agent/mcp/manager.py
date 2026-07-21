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

    @property
    def has_servers(self) -> bool:
        """Whether any MCP servers are configured."""
        return bool(self._mcp_servers)

    async def connect(
        self,
        tool_registry: ToolRegistry,
        *,
        register_tools: bool = True,
    ) -> list[Any]:
        """Connect to configured MCP servers (one-time, lazy).

        Safe to call concurrently — only the first caller performs the
        connection; subsequent calls are no-ops.

        When *register_tools* is False, tool wrappers are returned without
        registering them so the caller can defer schema mutations.
        """
        if self._connected or not self._mcp_servers:
            return []

        async with self._lock:
            if self._connected or self._connecting:
                return []
            self._connecting = True

        from markbot.tools.mcp import connect_mcp_servers  # noqa: PLC0415

        staged: list[Any] = []
        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            staged = await connect_mcp_servers(
                self._mcp_servers,
                tool_registry,
                self._mcp_stack,
                register_tools=register_tools,
            )
            self._connected = True
            self._ready.set()
            return list(staged or [])
        except BaseException as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception as e:
                    logger.debug("Failed to close MCP stack: {}", e)
                self._mcp_stack = None
            return []
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
