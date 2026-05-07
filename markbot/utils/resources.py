"""Resource lifecycle management — shared connection pools and AsyncContextManager.

Centralizes creation and teardown of async HTTP clients, WebSocket
connections, and other long-lived resources so that:

1. Connections are reused across the process lifetime (no per-request
   ``httpx.AsyncClient()`` overhead).
2. Graceful shutdown is guaranteed via ``AsyncContextManager``.
3. Resource health can be queried for readiness probes.

Usage::

    from markbot.utils.resources import ResourceManager

    rm = ResourceManager()
    client = rm.http_client("search", proxy="http://proxy:8080")
    # ... use client ...
    await rm.close()  # graceful shutdown
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from contextlib import AsyncExitStack
from typing import Any

from loguru import logger


class ManagedResource(ABC):
    """Base class for resources that need explicit lifecycle management."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._created_at = time.monotonic()
        self._closed = False

    @abstractmethod
    async def close(self) -> None:
        ...

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def age_s(self) -> float:
        return time.monotonic() - self._created_at


class _HttpPoolEntry(ManagedResource):
    """Wraps an ``httpx.AsyncClient`` as a managed resource."""

    def __init__(self, name: str, client: Any) -> None:
        super().__init__(name)
        self.client = client

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.client.aclose()
        except Exception as e:
            logger.warning("[ResourceManager] Error closing HTTP client '{}': {}", self.name, e)


class ResourceManager:
    """Central registry for shared async resources.

    Provides:
    - **Shared HTTP connection pools** keyed by logical name so that
      tools, channels, and providers reuse the same ``httpx.AsyncClient``.
    - **AsyncContextManager** protocol for ``async with`` usage.
    - **Graceful shutdown** that closes all managed resources in LIFO order.
    - **Health reporting** for readiness probes.
    """

    def __init__(self) -> None:
        self._resources: list[ManagedResource] = []
        self._http_clients: dict[str, _HttpPoolEntry] = {}
        self._exit_stack = AsyncExitStack()
        self._closed = False

    async def __aenter__(self) -> "ResourceManager":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    def http_client(
        self,
        name: str,
        *,
        proxy: str | None = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = True,
        verify: bool = True,
        max_connections: int = 100,
        max_keepalive_connections: int = 20,
    ) -> Any:
        """Get or create a shared ``httpx.AsyncClient``.

        The first call for a given *name* creates the client; subsequent
        calls return the same instance.  This avoids the overhead of
        per-request client creation while respecting different proxy /
        timeout requirements per logical service.
        """
        import httpx

        if name in self._http_clients:
            entry = self._http_clients[name]
            if not entry.is_closed:
                return entry.client

        limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
        )

        client = httpx.AsyncClient(
            proxy=proxy,
            timeout=timeout if timeout is not None else httpx.Timeout(30.0),
            headers=headers,
            follow_redirects=follow_redirects,
            verify=verify,
            limits=limits,
        )

        entry = _HttpPoolEntry(name, client)
        self._http_clients[name] = entry
        self._resources.append(entry)

        logger.debug("[ResourceManager] Created HTTP client '{}'", name)
        return client

    def register(self, resource: ManagedResource) -> None:
        """Register an arbitrary managed resource for lifecycle tracking."""
        self._resources.append(resource)
        logger.debug("[ResourceManager] Registered resource '{}'", resource.name)

    async def close(self) -> None:
        """Close all managed resources in reverse creation order (LIFO)."""
        if self._closed:
            return
        self._closed = True

        errors: list[tuple[str, Exception]] = []
        for resource in reversed(self._resources):
            try:
                if not resource.is_closed:
                    await resource.close()
            except Exception as e:
                errors.append((resource.name, e))

        self._resources.clear()
        self._http_clients.clear()

        if errors:
            for name, err in errors:
                logger.error("[ResourceManager] Error closing '{}': {}", name, err)

        logger.info("[ResourceManager] All resources closed")

    @property
    def is_closed(self) -> bool:
        return self._closed

    def health(self) -> dict[str, Any]:
        """Return a health summary for readiness probes."""
        http_status = {}
        for name, entry in self._http_clients.items():
            http_status[name] = {
                "closed": entry.is_closed,
                "age_s": round(entry.age_s, 1),
            }

        return {
            "closed": self._closed,
            "total_resources": len(self._resources),
            "http_clients": http_status,
        }


_global_rm: ResourceManager | None = None


def get_resource_manager() -> ResourceManager:
    """Get the global singleton ResourceManager (lazy-initialized)."""
    global _global_rm
    if _global_rm is None:
        _global_rm = ResourceManager()
    return _global_rm


def set_resource_manager(rm: ResourceManager) -> None:
    """Override the global ResourceManager (useful for testing)."""
    global _global_rm
    _global_rm = rm


async def shutdown_resource_manager() -> None:
    """Close the global ResourceManager (call during app shutdown)."""
    global _global_rm
    if _global_rm is not None:
        await _global_rm.close()
        _global_rm = None
