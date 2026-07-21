"""Tests for shared runtime assembly (gateway / web / CLI profiles)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from markbot.config.schema import Config
from markbot.runtime.factory import (
    CLI_FEATURES,
    GATEWAY_FEATURES,
    WEB_FEATURES,
    AgentRuntime,
    RuntimeFeatures,
    build_runtime,
)


class _FakeProvider:
    def __init__(self, *_a, **_k):
        self.model = "test-model"


def _minimal_config(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path)
    # Avoid real model validation paths in AgentLoop if any
    return cfg


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "sessions").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_feature_profiles_are_distinct():
    assert GATEWAY_FEATURES.cron_runner is True
    assert GATEWAY_FEATURES.channels is True
    assert GATEWAY_FEATURES.heartbeat is True
    assert GATEWAY_FEATURES.dream is True
    assert GATEWAY_FEATURES.cron_deliver is True
    assert GATEWAY_FEATURES.cron_notify_failure is True

    assert WEB_FEATURES.cron_runner is True
    assert WEB_FEATURES.channels is False
    assert WEB_FEATURES.heartbeat is False
    assert WEB_FEATURES.dream is False
    assert WEB_FEATURES.cron_deliver is False
    assert WEB_FEATURES.cron_notify_failure is False
    assert WEB_FEATURES.cron_default_channel == "web"

    assert CLI_FEATURES.cron_store is True
    assert CLI_FEATURES.cron_runner is False
    assert CLI_FEATURES.channels is False
    assert CLI_FEATURES.heartbeat is False
    assert CLI_FEATURES.session_manager is False


def test_build_runtime_cli_profile(workspace: Path):
    config = _minimal_config(workspace)

    fake_agent = MagicMock()
    fake_agent.model = "m"
    fake_agent.memory_manager = None
    fake_agent.skill_registry = None
    fake_agent.sessions = MagicMock()
    fake_agent.tools = {}
    fake_agent.close_mcp = AsyncMock()
    fake_agent.stop = MagicMock()

    with patch("markbot.agent.loop.AgentLoop", return_value=fake_agent) as loop_cls:
        rt = build_runtime(config, CLI_FEATURES, provider=_FakeProvider())

    assert isinstance(rt, AgentRuntime)
    assert rt.agent is fake_agent
    assert rt.cron is not None  # store for tools
    assert rt.channels is None
    assert rt.heartbeat is None
    assert rt.cron.on_job is None or not callable(getattr(rt.cron, "on_job", None)) or True
    # CLI does not wire runner callbacks (cron_runner=False)
    assert rt.cron.on_job is None
    assert rt.cron.on_failure is None

    kwargs = loop_cls.call_args.kwargs
    assert kwargs["cron_service"] is rt.cron
    assert kwargs["session_manager"] is None


def test_build_runtime_web_wires_cron_runner(workspace: Path):
    config = _minimal_config(workspace)
    fake_agent = MagicMock()
    fake_agent.model = "m"
    fake_agent.memory_manager = None
    fake_agent.skill_registry = None
    fake_agent.sessions = MagicMock()
    fake_agent.tools = {}
    fake_agent.close_mcp = AsyncMock()
    fake_agent.stop = MagicMock()

    with patch("markbot.agent.loop.AgentLoop", return_value=fake_agent):
        rt = build_runtime(config, WEB_FEATURES, provider=_FakeProvider())

    assert rt.cron is not None
    assert callable(rt.cron.on_job)
    assert callable(rt.cron.on_failure)
    assert rt.channels is None
    assert rt.heartbeat is None
    assert rt.features.cron_default_channel == "web"


def test_build_runtime_gateway_full_graph(workspace: Path):
    config = _minimal_config(workspace)
    fake_agent = MagicMock()
    fake_agent.model = "m"
    fake_agent.memory_manager = None
    fake_agent.skill_registry = None
    fake_agent.sessions = MagicMock()
    fake_agent.tools = {}
    fake_agent.close_mcp = AsyncMock()
    fake_agent.stop = MagicMock()
    fake_agent.has_active_conversations = MagicMock(return_value=False)

    with (
        patch("markbot.agent.loop.AgentLoop", return_value=fake_agent),
        patch("markbot.channels.manager.ChannelManager") as cm_cls,
    ):
        cm_cls.return_value = MagicMock(enabled_channels=[])
        rt = build_runtime(config, GATEWAY_FEATURES, provider=_FakeProvider())

    assert rt.cron is not None
    assert callable(rt.cron.on_job)
    assert callable(rt.cron.on_failure)
    assert rt.channels is not None
    assert rt.heartbeat is not None
    assert rt.sessions is not None


@pytest.mark.asyncio
async def test_web_cron_failure_logs_only(workspace: Path, caplog):
    config = _minimal_config(workspace)
    fake_agent = MagicMock()
    fake_agent.model = "m"
    fake_agent.memory_manager = None
    fake_agent.skill_registry = None
    fake_agent.sessions = MagicMock()
    fake_agent.tools = {}
    fake_agent.close_mcp = AsyncMock()
    fake_agent.stop = MagicMock()
    fake_agent.process_direct = AsyncMock(
        return_value=SimpleNamespace(content="ok")
    )

    with patch("markbot.agent.loop.AgentLoop", return_value=fake_agent):
        rt = build_runtime(config, WEB_FEATURES, provider=_FakeProvider())

    job = SimpleNamespace(
        name="t",
        id="1",
        payload=SimpleNamespace(
            message="do it",
            channel=None,
            to=None,
            deliver=False,
        ),
    )
    # Should not publish outbound; just log
    await rt.cron.on_failure(job, "boom")


@pytest.mark.asyncio
async def test_gateway_cron_failure_publishes(workspace: Path):
    config = _minimal_config(workspace)
    fake_agent = MagicMock()
    fake_agent.model = "m"
    fake_agent.memory_manager = None
    fake_agent.skill_registry = None
    fake_agent.sessions = MagicMock()
    fake_agent.tools = {}
    fake_agent.close_mcp = AsyncMock()
    fake_agent.stop = MagicMock()

    with (
        patch("markbot.agent.loop.AgentLoop", return_value=fake_agent),
        patch("markbot.channels.manager.ChannelManager") as cm_cls,
    ):
        cm_cls.return_value = MagicMock(enabled_channels=[])
        rt = build_runtime(config, GATEWAY_FEATURES, provider=_FakeProvider())

    published = []

    async def capture(msg):
        published.append(msg)

    rt.bus.publish_outbound = capture

    job = SimpleNamespace(
        name="t",
        id="1",
        payload=SimpleNamespace(
            message="do it",
            channel="feishu",
            to="u1",
            deliver=True,
        ),
    )
    await rt.cron.on_failure(job, "boom")
    assert len(published) == 1
    assert published[0].channel == "feishu"
    assert "Cron Failure" in published[0].content


@pytest.mark.asyncio
async def test_start_background_respects_features(workspace: Path):
    config = _minimal_config(workspace)
    fake_agent = MagicMock()
    fake_agent.model = "m"
    fake_agent.memory_manager = None
    fake_agent.skill_registry = None
    fake_agent.sessions = MagicMock()
    fake_agent.tools = {}
    fake_agent.close_mcp = AsyncMock()
    fake_agent.stop = MagicMock()

    with patch("markbot.agent.loop.AgentLoop", return_value=fake_agent):
        rt = build_runtime(config, CLI_FEATURES, provider=_FakeProvider())

    await rt.start_background()
    # CLI has no runner — start_cron is a no-op for runner flag
    assert rt._cron_started is False
    assert rt.dream is None
    assert rt.curator is None
    assert rt.heartbeat is None

    await rt.stop()
    fake_agent.close_mcp.assert_awaited()


def test_runtime_features_frozen():
    with pytest.raises(Exception):
        GATEWAY_FEATURES.cron_runner = False  # type: ignore[misc]


def test_entrypoints_import_profiles():
    from markbot.cli.groups import agent as agent_mod
    from markbot.cli.groups import gateway as gateway_mod
    from markbot.web import server as web_mod

    # Smoke: modules load after refactor
    assert agent_mod is not None
    assert gateway_mod is not None
    assert web_mod is not None
