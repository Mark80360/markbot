"""Tests for memory plugin discovery and ChromaDB provider."""

import pytest

from markbot.memory.plugins.discovery import MemoryPluginDiscovery, MemoryProviderInfo
from markbot.memory.provider import MemoryProvider


class TestMemoryPluginDiscovery:
    def test_discover_empty(self):
        discovery = MemoryPluginDiscovery()
        providers = discovery.discover()
        # May find installed packages, but at least returns a list
        assert isinstance(providers, list)

    def test_register_manual(self):
        discovery = MemoryPluginDiscovery()

        class TestProvider(MemoryProvider):
            @property
            def name(self):
                return "test"

            def is_available(self):
                return True

            def initialize(self, session_id, **kwargs):
                pass

        discovery.register("test", TestProvider)
        providers = discovery.discover()
        names = [p.name for p in providers]
        assert "test" in names

    def test_register_non_subclass_raises(self):
        discovery = MemoryPluginDiscovery()
        with pytest.raises(TypeError):
            discovery.register("bad", dict)

    def test_create_provider_manual(self):
        discovery = MemoryPluginDiscovery()

        class TestProvider(MemoryProvider):
            def __init__(self):
                self._plugin_config = {}

            @property
            def name(self):
                return "test"

            def is_available(self):
                return True

            def initialize(self, session_id, **kwargs):
                pass

        discovery.register("test", TestProvider)
        provider = discovery.create_provider("test", config={"key": "value"})
        assert provider is not None
        assert provider._plugin_config == {"key": "value"}

    def test_create_provider_not_found(self):
        discovery = MemoryPluginDiscovery()
        provider = discovery.create_provider("nonexistent")
        assert provider is None

    def test_list_available(self):
        discovery = MemoryPluginDiscovery()
        discovery.register("test", type("TestProvider", (MemoryProvider,), {
            "name": property(lambda self: "test"),
            "is_available": lambda self: True,
            "initialize": lambda self, sid, **kw: None,
        }))
        names = discovery.list_available()
        assert isinstance(names, list)


class TestMemoryProviderInfo:
    def test_defaults(self):
        info = MemoryProviderInfo(name="test")
        assert info.name == "test"
        assert info.description == ""
        assert info.is_available is True


class TestChromaMemoryProvider:
    def test_is_available_without_chromadb(self):
        """Should return False if chromadb is not installed."""
        from markbot.memory.providers.chroma import ChromaMemoryProvider
        provider = ChromaMemoryProvider()
        # This test passes if chromadb is NOT installed
        # If it IS installed, just check the return type
        result = provider.is_available()
        assert isinstance(result, bool)

    def test_name(self):
        from markbot.memory.providers.chroma import ChromaMemoryProvider
        provider = ChromaMemoryProvider()
        assert provider.name == "chroma"

    def test_system_prompt_block_uninitialized(self):
        from markbot.memory.providers.chroma import ChromaMemoryProvider
        provider = ChromaMemoryProvider()
        assert provider.system_prompt_block() == ""

    def test_prefetch_uninitialized(self):
        from markbot.memory.providers.chroma import ChromaMemoryProvider
        provider = ChromaMemoryProvider()
        assert provider.prefetch("test query") == ""

    def test_store_uninitialized(self):
        from markbot.memory.providers.chroma import ChromaMemoryProvider
        provider = ChromaMemoryProvider()
        assert provider.store("test") == ""

    def test_query_uninitialized(self):
        from markbot.memory.providers.chroma import ChromaMemoryProvider
        provider = ChromaMemoryProvider()
        assert provider.query("test") == []

    def test_shutdown(self):
        from markbot.memory.providers.chroma import ChromaMemoryProvider
        provider = ChromaMemoryProvider()
        provider.shutdown()
        assert provider._initialized is False
