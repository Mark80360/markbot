"""Tests for markbot.utils.resources — resource lifecycle management."""

import pytest

from markbot.utils.resources import (
    ManagedResource,
    ResourceManager,
    get_resource_manager,
    set_resource_manager,
    shutdown_resource_manager,
)


class DummyResource(ManagedResource):
    def __init__(self, name: str = "test"):
        super().__init__(name)
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        self._closed = True


class TestResourceManager:
    def test_http_client_creates(self):
        rm = ResourceManager()
        client = rm.http_client("test")
        assert client is not None
        assert "test" in rm._http_clients

    def test_http_client_reuses(self):
        rm = ResourceManager()
        c1 = rm.http_client("test")
        c2 = rm.http_client("test")
        assert c1 is c2

    def test_http_client_different_names(self):
        rm = ResourceManager()
        c1 = rm.http_client("search")
        c2 = rm.http_client("mcp")
        assert c1 is not c2

    @pytest.mark.asyncio
    async def test_close(self):
        rm = ResourceManager()
        rm.http_client("test")
        await rm.close()
        assert rm.is_closed

    @pytest.mark.asyncio
    async def test_close_lifo_order(self):
        rm = ResourceManager()
        order = []

        class TrackedResource(ManagedResource):
            def __init__(self, name: str):
                super().__init__(name)

            async def close(self) -> None:
                order.append(self.name)
                self._closed = True

        rm.register(TrackedResource("first"))
        rm.register(TrackedResource("second"))
        rm.register(TrackedResource("third"))
        await rm.close()
        assert order == ["third", "second", "first"]

    @pytest.mark.asyncio
    async def test_context_manager(self):
        async with ResourceManager() as rm:
            rm.http_client("test")
            assert not rm.is_closed
        assert rm.is_closed

    @pytest.mark.asyncio
    async def test_register_custom_resource(self):
        rm = ResourceManager()
        res = DummyResource("custom")
        rm.register(res)
        await rm.close()
        assert res.closed

    def test_health(self):
        rm = ResourceManager()
        rm.http_client("search")
        health = rm.health()
        assert health["closed"] is False
        assert health["total_resources"] == 1
        assert "search" in health["http_clients"]


class TestGlobalResourceManager:
    def test_get_singleton(self):
        rm1 = get_resource_manager()
        rm2 = get_resource_manager()
        assert rm1 is rm2

    @pytest.mark.asyncio
    async def test_set_and_shutdown(self):
        rm = ResourceManager()
        set_resource_manager(rm)
        rm.http_client("test")
        await shutdown_resource_manager()
        assert rm.is_closed
