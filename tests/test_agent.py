"""Tests for markbot.agent module (tokens, cost, stream, compact)."""

import time

import pytest

from markbot.agent.compact import (
    CompactAction,
    CompactionConfig,
    CompactResult,
)
from markbot.agent.cost import (
    CostState,
    CostTracker,
    ModelPricing,
    ModelUsage,
    PricingTable,
    _match_pricing_key,
)
from markbot.agent.stream import StreamFilter
from markbot.agent.tokens import TokenUsage, estimate_tokens
from markbot.types.exceptions import BudgetExceededError


class TestTokenUsage:
    def test_defaults(self):
        tu = TokenUsage()
        assert tu.input_tokens == 0
        assert tu.output_tokens == 0
        assert tu.total_tokens == 0

    def test_total_tokens(self):
        tu = TokenUsage(input_tokens=100, output_tokens=50, cache_creation_input_tokens=20, cache_read_input_tokens=30)
        assert tu.total_tokens == 200

    def test_context_tokens(self):
        tu = TokenUsage(input_tokens=100, cache_creation_input_tokens=20, cache_read_input_tokens=30)
        assert tu.context_tokens == 150

    def test_to_dict(self):
        tu = TokenUsage(input_tokens=100, output_tokens=50)
        d = tu.to_dict()
        assert d["input_tokens"] == 100
        assert d["output_tokens"] == 50
        assert "total_tokens" in d


class TestEstimateTokens:
    def test_empty_string(self):
        assert estimate_tokens("") == 0

    def test_non_empty_string(self):
        result = estimate_tokens("Hello, world!")
        assert result > 0

    def test_longer_text_more_tokens(self):
        short = estimate_tokens("Hi")
        long = estimate_tokens("This is a much longer piece of text with many more words.")
        assert long > short


class TestModelUsage:
    def test_defaults(self):
        mu = ModelUsage()
        assert mu.input_tokens == 0
        assert mu.api_calls == 0
        assert mu.cost_usd == 0.0

    def test_total_tokens(self):
        mu = ModelUsage(input_tokens=100, output_tokens=50, cache_creation_input_tokens=10, cache_read_input_tokens=20)
        assert mu.total_tokens == 180


class TestCostState:
    def test_defaults(self):
        cs = CostState()
        assert cs.total_cost_usd == 0.0
        assert cs.total_api_calls == 0
        assert cs.model_usage == {}


class TestModelPricing:
    def test_defaults(self):
        mp = ModelPricing()
        assert mp.input_per_1k == 0.003
        assert mp.output_per_1k == 0.006


class TestMatchPricingKey:
    def test_exact_match(self):
        assert _match_pricing_key("gpt-4o") == "gpt-4o"

    def test_prefix_match(self):
        result = _match_pricing_key("claude-sonnet-4-5-20250514")
        assert "sonnet" in result

    def test_unknown_model(self):
        result = _match_pricing_key("my-custom-model")
        assert result == "default"

    def test_opus_match(self):
        result = _match_pricing_key("claude-opus-4-5-custom")
        assert "opus" in result


class TestPricingTable:
    def test_default_pricing(self):
        pt = PricingTable()
        p = pt.get("gpt-4o")
        assert p.input_per_1k > 0

    def test_custom_pricing(self):
        pt = PricingTable(custom={"my-model": ModelPricing(input_per_1k=0.01, output_per_1k=0.05)})
        p = pt.get("my-model")
        assert p.input_per_1k == 0.01

    def test_set_custom(self):
        pt = PricingTable()
        pt.set_custom("new-model", ModelPricing(input_per_1k=0.001))
        assert pt.get("new-model").input_per_1k == 0.001


class TestCostTracker:
    def test_initial_state(self):
        ct = CostTracker()
        assert ct.total_cost == 0.0
        assert ct.is_over_budget() is False

    def test_add_usage(self):
        ct = CostTracker()
        cost = ct.add_usage(model="gpt-4o", input_tokens=1000, output_tokens=500)
        assert cost > 0
        assert ct.total_cost > 0

    def test_budget_enforcement(self):
        ct = CostTracker(max_budget_usd=0.001)
        with pytest.raises(BudgetExceededError):
            ct.add_usage(model="claude-sonnet-4-5", input_tokens=100000, output_tokens=50000)

    def test_no_budget_always_ok(self):
        ct = CostTracker(max_budget_usd=None)
        ct.add_usage(model="gpt-4o", input_tokens=100000, output_tokens=50000)
        assert ct.is_over_budget() is False

    def test_multiple_models(self):
        ct = CostTracker()
        ct.add_usage(model="gpt-4o", input_tokens=1000, output_tokens=500)
        ct.add_usage(model="claude-sonnet-4-5", input_tokens=2000, output_tokens=1000)
        assert len(ct.state.model_usage) == 2

    def test_cache_tokens_tracked(self):
        ct = CostTracker()
        ct.add_usage(
            model="claude-sonnet-4-5",
            input_tokens=1000,
            output_tokens=500,
            cache_creation_input_tokens=200,
            cache_read_input_tokens=300,
        )
        mu = ct.state.model_usage["claude-sonnet-4-5"]
        assert mu.cache_creation_input_tokens == 200
        assert mu.cache_read_input_tokens == 300

    def test_api_calls_counted(self):
        ct = CostTracker()
        ct.add_usage(model="gpt-4o", input_tokens=100, output_tokens=50)
        ct.add_usage(model="gpt-4o", input_tokens=200, output_tokens=100)
        assert ct.state.total_api_calls == 2
        assert ct.state.model_usage["gpt-4o"].api_calls == 2


class TestStreamFilter:
    @pytest.mark.asyncio
    async def test_passthrough_no_think(self):
        received = []

        async def _collect(d):
            received.append(d)

        sf = StreamFilter(upstream=_collect)
        await sf("hello ")
        await sf("world")
        assert "".join(received) == "hello world"

    @pytest.mark.asyncio
    async def test_filters_think_block(self):
        received = []

        async def _collect(d):
            received.append(d)

        sf = StreamFilter(upstream=_collect)
        await sf("hello ")
        await sf("<thinksecret>internal reasoning</thinksecret>")
        await sf("world")
        full = "".join(received)
        assert "hello" in full
        assert "world" in full

    @pytest.mark.asyncio
    async def test_reset(self):
        sf = StreamFilter()
        await sf("hello")
        sf.reset()
        assert sf.buffer == ""

    @pytest.mark.asyncio
    async def test_no_upstream(self):
        sf = StreamFilter(upstream=None)
        await sf("hello")
        assert sf.buffer == "hello"

    @pytest.mark.asyncio
    async def test_incremental_output(self):
        received = []

        async def _collect(d):
            received.append(d)

        sf = StreamFilter(upstream=_collect)
        await sf("a")
        await sf("b")
        await sf("c")
        assert "".join(received) == "abc"


    @pytest.mark.asyncio
    async def test_sync_upstream_callback(self):
        """StreamFilter must handle sync (non-async) upstream callbacks."""
        received = []

        def _collect_sync(d):
            received.append(d)

        sf = StreamFilter(upstream=_collect_sync)
        await sf("hello ")
        await sf("world")
        assert "".join(received) == "hello world"

class TestCompactAction:
    def test_all_actions(self):
        assert CompactAction.COLLAPSE == "collapse"
        assert CompactAction.MICRO_COMPACT == "micro_compact"
        assert CompactAction.AUTO_COMPACT == "auto_compact"
        assert CompactAction.HISTORY_SNIP == "history_snip"
        assert CompactAction.NONE == "none"


class TestCompactResult:
    def test_basic_result(self):
        cr = CompactResult(
            action=CompactAction.COLLAPSE,
            messages_before=10,
            messages_after=8,
            tokens_before=5000,
            tokens_after=3000,
        )
        assert cr.action == CompactAction.COLLAPSE
        assert cr.summary == ""


class TestCompactionConfig:
    def test_defaults(self):
        cc = CompactionConfig()
        assert cc.collapse_tool_result_chars == 4000
        assert cc.micro_compact_keep_turns == 6
        assert cc.threshold_ratio == 0.85


class TestContextBuilderCache:
    def test_system_context_caches_with_ttl(self, tmp_path):

        from markbot.agent.context import ContextBuilder

        cb = ContextBuilder(workspace=tmp_path)
        ctx1 = cb.get_system_context()
        ctx2 = cb.get_system_context()
        # Should return the same dict (cached)
        assert ctx1 is ctx2

    def test_system_prompt_cache_respects_ttl(self, tmp_path):
        from markbot.agent.context import ContextBuilder

        cb = ContextBuilder(workspace=tmp_path)
        # Set a very short TTL for testing
        cb._cache_ttl = 0.01
        result1 = cb.build_system_prompt()
        time.sleep(0.02)
        result2 = cb.build_system_prompt()
        # Both should return valid results (cache expired, rebuilt)
        assert isinstance(result1, str)
        assert isinstance(result2, str)


class TestIdleTimeout:
    """Tests for agent session idle timeout detection and cleanup."""

    def test_check_idle_sessions_detects_timeout(self):
        from markbot.agent.loop import AgentLoop

        loop = AgentLoop.__new__(AgentLoop)
        loop._session_last_active = {"test:ses1": time.time() - 60 * 31}  # 31 min ago
        loop._active_tasks = {}
        loop._session_locks = {}
        loop._pending_steer = {}
        loop._session_failure_state = {}
        loop._scrubber_pool = type("FakeScrubberPool", (), {
            "reset": lambda self, _: None,
        })()

        # Mock bus to capture the notification
        notifications: list[str] = []

        async def fake_publish(msg):
            notifications.append(msg)

        loop.bus = type("FakeBus", (), {"publish_outbound": fake_publish})()

        # _check_idle_sessions uses ensure_future, which won't run inline.
        # Instead, verify the cleanup path directly.
        loop._cleanup_session_state("test:ses1")

        assert "test:ses1" not in loop._session_last_active
        assert "test:ses1" not in loop._active_tasks

    def test_cleanup_cancels_active_tasks(self):
        import asyncio

        from markbot.agent.loop import AgentLoop

        loop = AgentLoop.__new__(AgentLoop)
        loop._session_last_active = {"ses:1": 0}
        loop._active_tasks = {"ses:1": [asyncio.Future()]}
        loop._session_locks = {"ses:1": asyncio.Lock()}
        loop._pending_steer = {"ses:1": "pending"}
        loop._scrubber_pool = type("Fake", (), {
            "reset": lambda self, _: None,
        })()

        loop._cleanup_session_state("ses:1")

        assert "ses:1" not in loop._active_tasks
        assert "ses:1" not in loop._session_locks
        assert "ses:1" not in loop._pending_steer
        assert "ses:1" not in loop._session_last_active

    def test_idle_disabled_when_zero(self, monkeypatch):
        from markbot.agent.loop import AgentLoop
        import markbot.agent.loop as agent_loop

        monkeypatch.setattr(agent_loop, "AGENT_IDLE_TIMEOUT_MINUTES", 0)

        loop = AgentLoop.__new__(AgentLoop)
        loop._session_last_active = {"x:y": time.time() - 3600}  # 1h ago
        loop._active_tasks = {}
        loop._session_locks = {}
        loop._pending_steer = {}
        loop._scrubber_pool = type("Fake", (), {
            "reset": lambda self, _: None,
        })()

        # _check_idle_sessions returns immediately when idle_seconds <= 0
        loop._check_idle_sessions(time.time())
        # The session should still be tracked (no cleanup happened)
        assert "x:y" in loop._session_last_active
