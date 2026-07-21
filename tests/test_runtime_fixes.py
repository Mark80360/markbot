"""Regression tests for verified runtime wiring bugs.

Covers:
- Chroma vectorstore import path
- process_direct / subagent busy tracking for Dream guards
- gateway finally-safe service handles (smoke via source contract)
- SSRF auto-init when load_config / lazy path is used
- channel manager starts outbound dispatcher with zero channels
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from markbot.bus.queue import MessageBus
from markbot.channels.manager import ChannelManager
from markbot.config.schema import Config


class TestChromaImportPath:
    def test_vectorstores_package_exports_chroma(self):
        from markbot.memory.vectorstores import ChromaVectorStore

        assert ChromaVectorStore is not None

    def test_try_chroma_import_path_does_not_use_missing_submodule(self):
        """_try_chroma must import from package, not vectorstores.chroma."""
        import inspect

        from markbot.memory.vectorstore_factory import _try_chroma

        src = inspect.getsource(_try_chroma)
        assert "from .vectorstores import ChromaVectorStore" in src
        assert "from .vectorstores.chroma import" not in src


class TestBusyTracking:
    def _make_loop_shell(self):
        """Minimal AgentLoop-like object for has_active_conversations logic."""
        from markbot.agent.loop import AgentLoop

        # Bind the real method without full DI construction.
        loop = object.__new__(AgentLoop)
        loop._direct_inflight = 0
        loop._active_tasks = {}
        loop.subagents = None
        return loop

    def test_idle_when_no_work(self):
        loop = self._make_loop_shell()
        assert loop.has_active_conversations() is False

    def test_busy_when_direct_inflight(self):
        loop = self._make_loop_shell()
        loop._direct_inflight = 1
        assert loop.has_active_conversations() is True

    @pytest.mark.asyncio
    async def test_busy_when_active_task(self):
        loop = self._make_loop_shell()

        async def _noop():
            await asyncio.sleep(3600)

        task = asyncio.create_task(_noop())
        try:
            loop._active_tasks["cli:direct"] = [task]
            assert loop.has_active_conversations() is True
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    def test_busy_when_subagent_running(self):
        loop = self._make_loop_shell()
        loop.subagents = SimpleNamespace(has_running_tasks=lambda: True)
        assert loop.has_active_conversations() is True

    @pytest.mark.asyncio
    async def test_subagent_manager_has_running_tasks(self):
        from markbot.agent.subagent.manager import SubagentManager

        mgr = SubagentManager(
            fallback_manager=MagicMock(),
            workspace=None,
            bus=MessageBus(),
        )
        assert mgr.has_running_tasks() is False

        async def _noop():
            await asyncio.sleep(3600)

        task = asyncio.create_task(_noop())
        try:
            mgr._running_tasks["abc"] = task
            assert mgr.has_running_tasks() is True
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    @pytest.mark.asyncio
    async def test_process_direct_increments_and_decrements_inflight(self):
        from markbot.agent.loop import AgentLoop

        loop = object.__new__(AgentLoop)
        loop._direct_inflight = 0
        loop.memory_manager = None
        loop.memory_encoder = None
        loop.ctx = None

        async def _fake_connect():
            # Mid-flight: must already be marked busy.
            assert loop._direct_inflight == 1
            return None

        async def _fake_process(*_a, **_k):
            assert loop.has_active_conversations() is True
            return None

        loop._connect_mcp = _fake_connect  # type: ignore[method-assign]
        loop._process_message = _fake_process  # type: ignore[method-assign]
        loop._active_tasks = {}
        loop.subagents = None

        assert loop.has_active_conversations() is False
        await AgentLoop.process_direct(loop, "hello")
        assert loop._direct_inflight == 0
        assert loop.has_active_conversations() is False

    @pytest.mark.asyncio
    async def test_process_direct_decrements_on_error(self):
        from markbot.agent.loop import AgentLoop

        loop = object.__new__(AgentLoop)
        loop._direct_inflight = 0
        loop.memory_manager = None
        loop.memory_encoder = None
        loop.ctx = None
        loop._active_tasks = {}
        loop.subagents = None

        async def _boom():
            raise RuntimeError("mcp fail")

        loop._connect_mcp = _boom  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="mcp fail"):
            await AgentLoop.process_direct(loop, "hello")
        assert loop._direct_inflight == 0


class TestSsrfInit:
    def test_load_config_initialises_ssrf(self, tmp_path, monkeypatch):
        from markbot.config import loader
        from markbot.utils import ssrf

        # Clear module state to simulate fresh process.
        ssrf._BLOCKED_HOSTNAMES = frozenset()
        ssrf._ALWAYS_BLOCKED_IPS = ()
        ssrf._PRIVATE_NETWORKS = ()
        ssrf._INITIALIZED = False

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(loader, "get_config_path", lambda: cfg_path)
        monkeypatch.setattr(loader, "_current_config", None)
        monkeypatch.setattr(loader, "_workspace_override", None)

        loader.load_config(cfg_path)
        assert ssrf._INITIALIZED is True
        assert len(ssrf._PRIVATE_NETWORKS) > 0
        assert "metadata.google.internal" in ssrf._BLOCKED_HOSTNAMES

    def test_lazy_ensure_initialized_blocks_localhost(self):
        from markbot.utils import ssrf

        ssrf._BLOCKED_HOSTNAMES = frozenset()
        ssrf._ALWAYS_BLOCKED_IPS = ()
        ssrf._PRIVATE_NETWORKS = ()
        ssrf._INITIALIZED = False

        ok, err = ssrf.validate_url_target("http://127.0.0.1/admin")
        assert ok is False
        assert ssrf._INITIALIZED is True


class TestChannelManagerOutbound:
    @pytest.mark.asyncio
    async def test_start_all_runs_dispatcher_with_no_channels(self):
        bus = MessageBus()
        config = Config()
        mgr = ChannelManager(config, bus)
        # Force empty channel map regardless of discovery/config.
        mgr.channels = {}

        start_task = asyncio.create_task(mgr.start_all())
        # Give the dispatcher a tick to start.
        await asyncio.sleep(0.05)
        assert mgr._dispatch_task is not None
        assert not mgr._dispatch_task.done()

        await mgr.stop_all()
        # start_all should exit after dispatcher is cancelled.
        try:
            await asyncio.wait_for(start_task, timeout=1.0)
        except asyncio.CancelledError:
            pass


class TestGatewayCleanupContract:
    def test_cleanup_uses_runtime_stop(self):
        """Gateway finally must drain via AgentRuntime.stop(), not unbound locals.

        After the runtime factory refactor, dream/curator/cron/heartbeat live on
        the AgentRuntime handle (created before ``async def run``). Cleanup is
        a single ``await runtime.stop()`` so partial startup cannot raise
        UnboundLocalError for dream_service / curator.
        """
        from pathlib import Path

        src = Path("markbot/cli/groups/gateway.py").read_text(encoding="utf-8")
        assert "build_runtime" in src
        assert "GATEWAY_FEATURES" in src
        assert "await runtime.stop()" in src
        # Old pattern must not reappear (would reintroduce UnboundLocal risk if
        # only partially restored).
        assert "if dream_service:" not in src
        assert "if curator:" not in src


class TestVersionConsistency:
    def test_package_version_matches_pyproject(self):
        import re
        from pathlib import Path

        import markbot

        pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
        assert m is not None
        assert markbot.__version__ == m.group(1)
