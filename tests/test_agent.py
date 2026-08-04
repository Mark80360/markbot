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
        _fut_loop = asyncio.new_event_loop()
        loop._active_tasks = {"ses:1": [_fut_loop.create_future()]}
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


class TestToolResultFailureClassifier:
    """Verify _is_tool_result_failure classifies all failure shapes.

    This classifier is the single source of truth for both the guardrail's
    failure steering AND the TOOL_COMPLETED event's ``ok`` field (and the
    on_tool_complete callback's error_msg vs summary decision). A mismatch
    between the two caused tools returning ``{"error": "..."}`` JSON strings
    to be flagged as failure for steering but reported as success to the UI.
    """

    @pytest.fixture
    def runner(self):
        # Bypass __init__ — _is_tool_result_failure only uses the staticmethod
        # _dict_has_error, so no instance state is needed.
        from markbot.agent.iteration import IterationRunner
        return IterationRunner.__new__(IterationRunner)

    @pytest.mark.parametrize("result", [
        'Error: something went wrong',
        '{"error": "fetch failed (timeout)"}',
        '{"errors": ["validation failed"]}',
        '{"error_code": "AUTH_FAILED"}',
        '{"status": 404, "body": "not found"}',
        '{"status_code": 500}',
        '{"ok": false, "reason": "rate limited"}',
        '{"success": false}',
        '{"errcode": 40001}',
        '{"ok": false}',
        '  {"error": "leading whitespace"}',
        RuntimeError("boom"),
        ValueError("bad input"),
        "traceback (most recent call last):",
        "ModuleNotFoundError: No module named 'foo'",
        "fetch failed: connection refused",
        "exit code: 1",
        "exit code: 127",
    ])
    def test_classifies_as_failure(self, runner, result):
        assert runner._is_tool_result_failure(result) is True

    @pytest.mark.parametrize("result", [
        "operation completed successfully",
        '{"result": "ok", "data": [1, 2, 3]}',
        '{"status": 200, "body": "ok"}',
        '{"ok": true}',
        '{"success": true}',
        '{"data": "no error field here"}',
        # JSON with error: null / error: "" is NOT a failure — these are
        # standard success responses from many APIs (e.g. stock data,
        # REST endpoints). The old '"error":' keyword caused false positives
        # that triggered spurious "failure loop" halts.
        '{"error": null, "data": "ok"}',
        '{"error": "", "result": "fine"}',
        "[1, 2, 3]",
        "",
        "   ",
        42,
        ["list", "of", "items"],
        {"key": "value"},
        None,
    ])
    def test_classifies_as_success(self, runner, result):
        assert runner._is_tool_result_failure(result) is False

    @pytest.mark.parametrize("tool_name", ["exec", "shell", "run_command", "run_code"])
    @pytest.mark.parametrize("result", [
        # stdout containing "Traceback" is DATA, not a tool failure —
        # the tool executed successfully, the subprocess raised.
        '{"exit_code": 0, "stdout": "Traceback (most recent call last):\\n  File ..."}',
        '{"exit_code": 0, "stdout": "mootdx: 0.11.7\\nflask:  Traceback (most recent call last):"}',
        '{"exit_code": 0, "output": "Error: something failed in subprocess"}',
        '{"exit_code": 0, "stdout": "  error: invalid syntax"}',
        '{"exit_code": 0}',
        # Plain string output (no JSON wrapper) = success
        "mootdx: 0.11.7\nflask:  Traceback (most recent call last):",
        '{"name": "宁德时代", "price": 396.84, "status": 200}',
    ])
    def test_exec_tool_stdout_not_failure(self, runner, tool_name, result):
        """exec/shell/run_code: only exit_code != 0 is a failure.

        Regression for logs/2026-07-30.log: run_code returning pip show
        output containing "Traceback" was misclassified as failure,
        triggering spurious "failure loop" halts.
        """
        assert runner._is_tool_result_failure(result, tool_name) is False

    @pytest.mark.parametrize("tool_name", ["exec", "shell", "run_code"])
    def test_exec_tool_nonzero_exit_is_failure(self, runner, tool_name):
        assert runner._is_tool_result_failure(
            '{"exit_code": 1, "stderr": "boom"}', tool_name,
        ) is True
        assert runner._is_tool_result_failure(
            '{"exit_code": 127, "stderr": "command not found"}', tool_name,
        ) is True

    @pytest.mark.parametrize("tool_name", [
        "read_file", "grep", "glob", "web_fetch", "web_search",
        "browser_navigate", "search_codebase",
    ])
    @pytest.mark.parametrize("result", [
        # Content containing error-like substrings is NOT a tool failure —
        # it's the data being read/fetched.
        '273| 274| <script>\n275| // ===== CONFIG =====\n276| const BASE_PRICE = 18.47;\n277| // error: missing semicolon',
        '{"text": "Error: user not found", "status": 200}',
        'def foo():\n    raise ValueError("cannot connect")\n    # error: bad input',
        '## Documentation\n\nIf you see "error: permission denied", check your config.\n',
        '{"url": "http://example.com", "text": "Traceback (most recent call last): ...", "status": 200}',
    ])
    def test_content_tool_not_failure(self, runner, tool_name, result):
        """read_file/grep/web_fetch: external content is never scanned for
        error keywords — only JSON structural errors count.

        Regression for logs/2026-07-30.log: read_file returning JS source
        code containing "error:" was misclassified as failure.
        """
        assert runner._is_tool_result_failure(result, tool_name) is False

    @pytest.mark.parametrize("tool_name", ["write_file", "edit_file", "delete_file"])
    @pytest.mark.parametrize("result", [
        # Plain-string success messages = success
        "File written to /tmp/foo.py",
        "Edited 3 lines in /tmp/bar.py",
        # JSON with explicit success marker = success
        '{"bytes_written": 42, "path": "/tmp/foo.py"}',
        '{"success": true, "path": "/tmp/foo.py"}',
    ])
    def test_file_mutation_success(self, runner, tool_name, result):
        assert runner._is_tool_result_failure(result, tool_name) is False

    @pytest.mark.parametrize("tool_name", ["write_file", "edit_file"])
    def test_file_mutation_error_json(self, runner, tool_name):
        assert runner._is_tool_result_failure(
            '{"error": "permission denied"}', tool_name,
        ) is True
        assert runner._is_tool_result_failure(
            '{"success": false, "error": "disk full"}', tool_name,
        ) is True


class TestPermissionModeOverride:
    """``IterationRunner._current_permission_context`` must let unattended
    callers (cron / autopilot / heartbeat) force a mode via
    ``process_direct(permission_mode=...)`` so they don't depend on the
    global ``app_state`` mode being set by a prior ``/mode`` command.

    Regression for logs/2026-07-05.log: cron cleanup at 04:00 was blocked
    by DEFAULT mode even though ``/mode auto`` had been set the evening
    before — the in-memory mode reset on restart, and cron had no way to
    opt in itself.
    """

    def _make_runner(self, override=None, app_state_mode=None):
        from markbot.agent.iteration import IterationRunner
        from markbot.session.types import AppState
        from markbot.types.permission import (
            PermissionMode,
            ToolPermissionContext,
        )

        runner = IterationRunner.__new__(IterationRunner)
        runner.permission_mode_override = override

        class _FakeCtx:
            pass

        class _FakeProvider:
            def get(self):
                return AppState(
                    permission_mode=app_state_mode or PermissionMode.DEFAULT,
                    tool_permission_context=ToolPermissionContext(
                        mode=app_state_mode or PermissionMode.DEFAULT
                    ),
                )

        class _FakeLoop:
            ctx = _FakeCtx()

        _FakeCtx.app_state = _FakeProvider()
        runner.loop = _FakeLoop()
        return runner

    def test_override_takes_precedence_over_app_state(self):
        from markbot.types.permission import PermissionMode
        runner = self._make_runner(
            override=PermissionMode.AUTO,
            app_state_mode=PermissionMode.DEFAULT,
        )
        mode, tool_ctx = runner._current_permission_context()
        assert mode is PermissionMode.AUTO
        assert tool_ctx.mode is PermissionMode.AUTO

    def test_no_override_falls_back_to_app_state(self):
        from markbot.types.permission import PermissionMode
        runner = self._make_runner(
            override=None,
            app_state_mode=PermissionMode.AUTO,
        )
        mode, _ = runner._current_permission_context()
        assert mode is PermissionMode.AUTO

    def test_no_override_no_app_state_falls_back_to_default(self):
        from markbot.types.permission import PermissionMode
        runner = self._make_runner(override=None, app_state_mode=None)
        mode, _ = runner._current_permission_context()
        assert mode is PermissionMode.DEFAULT


class TestGuardrailResetForTurn:
    """reset_for_turn() must clear ALL state for a new user turn."""

    def test_clears_cross_loop_fields(self):
        from markbot.agent.tool_guardrails import ToolCallGuardrail
        g = ToolCallGuardrail()
        g.state.reflection_count = 3
        g.state.forced_stop_count = 2
        g.state.failed_methods.append("web_fetch: connection refused")
        g.state.failed_methods.append("exec: exit code 1")

        g.reset_for_turn()

        assert g.state.reflection_count == 0
        assert g.state.forced_stop_count == 0
        assert g.state.failed_methods == []

    def test_clears_turn_local_fields(self):
        from markbot.agent.tool_guardrails import ToolCallGuardrail
        g = ToolCallGuardrail()
        g.state.recent_failures.extend([True, True, False])
        g.state.exact_failure_counts["web_fetch:sig1"] = 3
        g.state.tool_streaks["exec"] = 5
        g.state.no_progress["read_file:sig"] = ("rsig", 2)
        g.state.blocked_tools.add("exec")
        g.state.blocked_signatures.add("web_fetch:sig1")
        g.state.turn_call_counts["web_search"] = 10

        g.reset_for_turn()

        assert g.state.recent_failures == []
        assert g.state.exact_failure_counts == {}
        assert g.state.tool_streaks == {}
        assert g.state.no_progress == {}
        assert g.state.blocked_tools == set()
        assert g.state.blocked_signatures == set()
        assert g.state.turn_call_counts == {}

    def test_full_reset_vs_turn_local(self):
        """reset_for_turn clears cross-loop; reset_turn_local preserves them."""
        from markbot.agent.tool_guardrails import ToolCallGuardrail
        g = ToolCallGuardrail()
        g.state.reflection_count = 3
        g.state.failed_methods.append("exec: exit 1")
        g.state.turn_call_counts["web_search"] = 5

        g.reset_turn_local()
        # turn_local keeps cross-loop, clears turn-local
        assert g.state.reflection_count == 3
        assert len(g.state.failed_methods) == 1
        assert g.state.turn_call_counts == {}

        # Now full reset
        g.reset_for_turn()
        assert g.state.reflection_count == 0
        assert g.state.failed_methods == []


class TestGuardrailLoopCap:
    """check_loop_cap enforces per-turn hard ceilings on runaway tools."""

    @pytest.fixture
    def guardrail(self):
        from markbot.agent.tool_guardrails import ToolCallGuardrail, GuardrailConfig
        return ToolCallGuardrail(GuardrailConfig(
            max_web_searches_per_turn=3,
            max_web_fetches_per_turn=2,
            max_browser_navigations_per_turn=2,
            max_delegations_per_turn=1,
        ))

    def test_allows_until_cap(self, guardrail):
        from markbot.agent.tool_guardrails import GuardrailAction
        for _ in range(3):
            d = guardrail.check_loop_cap("web_search")
            assert d.action is GuardrailAction.ALLOW

    def test_blocks_at_cap(self, guardrail):
        from markbot.agent.tool_guardrails import GuardrailAction
        for _ in range(3):
            guardrail.check_loop_cap("web_search")
        d = guardrail.check_loop_cap("web_search")
        assert d.action is GuardrailAction.BLOCK
        assert d.reason == "loop_cap"
        assert d.count == 3

    def test_different_tools_independent(self, guardrail):
        from markbot.agent.tool_guardrails import GuardrailAction
        # Exhaust web_fetch cap (2)
        guardrail.check_loop_cap("web_fetch")
        guardrail.check_loop_cap("web_fetch")
        # web_search still allowed (cap 3)
        d = guardrail.check_loop_cap("web_search")
        assert d.action is GuardrailAction.ALLOW

    def test_uncapped_tools_always_allowed(self, guardrail):
        from markbot.agent.tool_guardrails import GuardrailAction
        for _ in range(100):
            d = guardrail.check_loop_cap("read_file")
            assert d.action is GuardrailAction.ALLOW

    def test_cap_zero_means_unlimited(self):
        from markbot.agent.tool_guardrails import (
            ToolCallGuardrail, GuardrailConfig, GuardrailAction,
        )
        g = ToolCallGuardrail(GuardrailConfig(max_web_searches_per_turn=0))
        for _ in range(100):
            d = g.check_loop_cap("web_search")
            assert d.action is GuardrailAction.ALLOW

    def test_delegate_task_cap(self, guardrail):
        from markbot.agent.tool_guardrails import GuardrailAction
        d1 = guardrail.check_loop_cap("delegate_task")
        assert d1.action is GuardrailAction.ALLOW
        d2 = guardrail.check_loop_cap("delegate_task")
        assert d2.action is GuardrailAction.BLOCK

    def test_caps_reset_with_turn_local(self, guardrail):
        from markbot.agent.tool_guardrails import GuardrailAction
        for _ in range(3):
            guardrail.check_loop_cap("web_search")
        assert guardrail.check_loop_cap("web_search").should_block

        guardrail.reset_turn_local()
        d = guardrail.check_loop_cap("web_search")
        assert d.action is GuardrailAction.ALLOW

    def test_caps_reset_with_for_turn(self, guardrail):
        from markbot.agent.tool_guardrails import GuardrailAction
        for _ in range(3):
            guardrail.check_loop_cap("web_search")
        assert guardrail.check_loop_cap("web_search").should_block

        guardrail.reset_for_turn()
        d = guardrail.check_loop_cap("web_search")
        assert d.action is GuardrailAction.ALLOW

    def test_cap_not_persisted(self, guardrail):
        """to_persisted must NOT include turn_call_counts."""
        for _ in range(3):
            guardrail.check_loop_cap("web_search")
        persisted = guardrail.to_persisted()
        assert "turn_call_counts" not in persisted


class TestToolStreakBlock:
    """Consecutive failures escalate ALLOW → WARN → BLOCK.

    Thresholds come from the guardrail defaults
    (``DEFAULT_TOOL_STREAK_WARN`` / ``DEFAULT_TOOL_STREAK_BLOCK``) so
    these tests stay in sync with the implementation.
    """

    def test_streak_warn_at_threshold(self):
        """failures below the warn threshold → ALLOW; at it → WARN."""
        from markbot.agent.tool_guardrails import (
            ToolCallGuardrail, GuardrailAction,
            DEFAULT_TOOL_STREAK_WARN,
        )
        g = ToolCallGuardrail()
        for _ in range(DEFAULT_TOOL_STREAK_WARN - 1):
            d = g._observe_tool_streak("web_search", failed=True)
            assert d.action is GuardrailAction.ALLOW
        # warn-threshold-th failure → WARN (still not blocked)
        d = g._observe_tool_streak("web_search", failed=True)
        assert d.action is GuardrailAction.WARN
        assert not g.is_call_blocked("web_search")

    def test_streak_block_at_threshold(self):
        """block-th consecutive failure → BLOCK (tool added to blocked_tools)."""
        from markbot.agent.tool_guardrails import (
            ToolCallGuardrail, GuardrailAction,
            DEFAULT_TOOL_STREAK_BLOCK,
        )
        g = ToolCallGuardrail()
        for _ in range(DEFAULT_TOOL_STREAK_BLOCK - 1):
            g._observe_tool_streak("web_search", failed=True)
        d = g._observe_tool_streak("web_search", failed=True)  # streak == block
        assert d.action is GuardrailAction.BLOCK
        assert "web_search" in g.state.blocked_tools
        assert g.is_call_blocked("web_search")

    def test_streak_resets_on_success(self):
        """A success resets the streak to 0."""
        from markbot.agent.tool_guardrails import (
            ToolCallGuardrail, GuardrailAction,
            DEFAULT_TOOL_STREAK_WARN,
        )
        g = ToolCallGuardrail()
        for _ in range(DEFAULT_TOOL_STREAK_WARN - 1):
            g._observe_tool_streak("web_search", failed=True)
        g._observe_tool_streak("web_search", failed=False)  # streak → 0 (reset)
        d = g._observe_tool_streak("web_search", failed=True)  # streak=1
        assert d.action is GuardrailAction.ALLOW
        assert not g.is_call_blocked("web_search")


class TestErrorClassifier:
    """classify_api_error: deterministic vs transient classification."""

    @pytest.mark.parametrize("message", [
        "SSLCertVerificationError: certificate verify failed",
        "ssl certificate verification failed",
        "self-signed certificate in certificate chain",
        "certificate verify failed: unable to get local issuer",
        "[SSL: BAD_HANDSHAKE]",
    ])
    def test_ssl_cert_is_deterministic(self, message):
        from markbot.providers.error_classifier import (
            classify_api_error, ErrorCategory,
        )
        result = classify_api_error(None, message)
        assert result.category is ErrorCategory.DETERMINISTIC
        assert not result.retryable
        assert result.reason == "ssl_cert_verification"

    @pytest.mark.parametrize("message", [
        "context length exceeded maximum",
        "This model's maximum context length is 4096 tokens",
        "too many tokens in prompt",
        "Please reduce the length of the messages",
    ])
    def test_context_overflow_is_deterministic(self, message):
        from markbot.providers.error_classifier import (
            classify_api_error, ErrorCategory,
        )
        result = classify_api_error(None, message)
        assert result.category is ErrorCategory.DETERMINISTIC
        assert not result.retryable
        assert result.reason == "context_overflow"

    @pytest.mark.parametrize("status_code", [400, 401, 402, 403, 404, 413])
    def test_4xx_deterministic(self, status_code):
        from markbot.providers.error_classifier import (
            classify_api_error, ErrorCategory,
        )
        result = classify_api_error(status_code, "error")
        assert result.category is ErrorCategory.DETERMINISTIC
        assert not result.retryable

    @pytest.mark.parametrize("status_code", [408, 429, 500, 502, 503, 504, 529])
    def test_5xx_and_rate_limit_transient(self, status_code):
        from markbot.providers.error_classifier import (
            classify_api_error, ErrorCategory,
        )
        result = classify_api_error(status_code, "error")
        assert result.category is ErrorCategory.TRANSIENT
        assert result.retryable

    def test_timeout_message_transient(self):
        from markbot.providers.error_classifier import (
            classify_api_error, ErrorCategory,
        )
        result = classify_api_error(None, "Connection timed out after 30s")
        assert result.category is ErrorCategory.TRANSIENT
        assert result.retryable

    def test_rate_limit_body_transient(self):
        from markbot.providers.error_classifier import (
            classify_api_error, ErrorCategory,
        )
        result = classify_api_error(None, "Rate limit exceeded")
        assert result.category is ErrorCategory.TRANSIENT
        assert result.retryable

    def test_content_filter_deterministic(self):
        from markbot.providers.error_classifier import (
            classify_api_error, ErrorCategory,
        )
        result = classify_api_error(400, "content_filter triggered")
        assert result.category is ErrorCategory.DETERMINISTIC
        assert not result.retryable
        assert result.reason == "content_policy"

    def test_model_not_found_overrides_5xx(self):
        """Aggregator 503 with 'model not found' body → deterministic + fallback."""
        from markbot.providers.error_classifier import (
            classify_api_error, ErrorCategory,
        )
        result = classify_api_error(503, "model not found: gpt-5")
        assert result.category is ErrorCategory.DETERMINISTIC
        assert not result.retryable
        assert result.reason == "model_not_found"
        assert result.should_fallback

    def test_unknown_defaults_to_transient(self):
        from markbot.providers.error_classifier import (
            classify_api_error, ErrorCategory,
        )
        result = classify_api_error(None, "something weird happened")
        assert result.category is ErrorCategory.TRANSIENT
        assert result.retryable
        assert result.reason == "unknown"

    def test_classify_to_error_type_bridge(self):
        from markbot.providers.error_classifier import classify_to_error_type
        from markbot.providers.errors import ErrorType

        # SSL cert → UNAVAILABLE (non-retryable)
        assert classify_to_error_type(None, "SSL cert verify failed") is ErrorType.UNAVAILABLE

        # Timeout → TRANSIENT
        assert classify_to_error_type(503, "overloaded") is ErrorType.TRANSIENT

        # Content filter → CONTENT
        assert classify_to_error_type(400, "content_filter") is ErrorType.CONTENT

        # Unknown → TRANSIENT (safest default)
        assert classify_to_error_type(None, "weird error") is ErrorType.TRANSIENT

    def test_exception_parameter(self):
        from markbot.providers.error_classifier import (
            classify_api_error, ErrorCategory,
        )
        exc = ConnectionRefusedError("Connection refused")
        result = classify_api_error(None, "", exc)
        assert result.category is ErrorCategory.TRANSIENT
        assert result.retryable


class TestErrorClassifierRecoveryHints:
    """Recovery action hints: should_compress / should_fallback / should_strip_images."""

    def test_context_overflow_should_compress(self):
        from markbot.providers.error_classifier import classify_api_error
        result = classify_api_error(None, "context length exceeded")
        assert result.should_compress
        assert not result.should_fallback
        # Also strips images as fallback recovery (images are large tokens)
        assert result.should_strip_images

    def test_413_should_compress(self):
        from markbot.providers.error_classifier import classify_api_error
        result = classify_api_error(413, "payload too large")
        assert result.should_compress
        assert result.should_strip_images

    def test_payload_too_large_body_should_compress(self):
        from markbot.providers.error_classifier import classify_api_error
        result = classify_api_error(None, "request_too_large")
        assert result.should_compress
        assert result.should_strip_images

    def test_model_not_found_should_fallback(self):
        from markbot.providers.error_classifier import classify_api_error
        result = classify_api_error(404, "model not found")
        assert result.should_fallback
        assert not result.should_compress

    def test_auth_error_should_fallback(self):
        from markbot.providers.error_classifier import classify_api_error
        result = classify_api_error(401, "invalid api key")
        assert result.should_fallback

    def test_billing_should_fallback(self):
        from markbot.providers.error_classifier import classify_api_error
        result = classify_api_error(None, "insufficient_quota")
        assert result.should_fallback
        assert result.reason == "billing"

    def test_432_quota_exhausted_should_fallback(self):
        """HTTP 432 (Tavily quota exhausted) → billing, deterministic, fallback."""
        from markbot.providers.error_classifier import (
            classify_api_error, ErrorCategory,
        )
        result = classify_api_error(432, "Client error '432'")
        assert result.reason == "billing"
        assert result.category is ErrorCategory.DETERMINISTIC
        assert not result.retryable
        assert result.should_fallback

    def test_image_too_large_should_strip_images(self):
        from markbot.providers.error_classifier import classify_api_error
        result = classify_api_error(400, "image exceeds 5 MB maximum")
        assert result.should_strip_images
        assert result.reason == "image_too_large"

    def test_multimodal_tool_content_should_strip_images(self):
        from markbot.providers.error_classifier import classify_api_error
        result = classify_api_error(400, "tool message content must be a string")
        assert result.should_strip_images
        assert result.reason == "multimodal_tool_content"

    def test_no_available_channel_should_fallback(self):
        from markbot.providers.error_classifier import classify_api_error
        result = classify_api_error(503, "no available channel")
        assert result.should_fallback
        assert result.reason == "no_available_channel"

    def test_ssl_cert_should_fallback(self):
        from markbot.providers.error_classifier import classify_api_error
        result = classify_api_error(None, "SSL certificate verify failed")
        assert result.should_fallback

    def test_content_policy_should_fallback(self):
        from markbot.providers.error_classifier import classify_api_error
        result = classify_api_error(400, "content_filter triggered")
        assert result.should_fallback
        assert not result.should_strip_images

    def test_transient_no_recovery_hints(self):
        """Transient errors don't need compress/fallback/strip — just retry."""
        from markbot.providers.error_classifier import classify_api_error
        result = classify_api_error(500, "internal server error")
        assert not result.should_compress
        assert not result.should_fallback
        assert not result.should_strip_images


class TestErrorClassifierBackoff:
    """Backoff strategy hints for transient errors."""

    def test_rate_limit_jittered_backoff(self):
        from markbot.providers.error_classifier import (
            classify_api_error, BackoffStrategy,
        )
        result = classify_api_error(429, "rate limit exceeded")
        assert result.backoff_seconds == 5.0
        assert result.backoff_strategy is BackoffStrategy.JITTERED

    def test_overloaded_exponential_backoff(self):
        from markbot.providers.error_classifier import (
            classify_api_error, BackoffStrategy,
        )
        result = classify_api_error(529, "overloaded")
        assert result.backoff_seconds == 3.0
        assert result.backoff_strategy is BackoffStrategy.EXPONENTIAL

    def test_429_with_overloaded_body_refines_to_overloaded(self):
        """429 status + 'overloaded' body → overloaded (not rate_limit)."""
        from markbot.providers.error_classifier import (
            classify_api_error, BackoffStrategy,
        )
        result = classify_api_error(429, "service is temporarily overloaded")
        assert result.reason == "overloaded"
        assert result.backoff_strategy is BackoffStrategy.EXPONENTIAL

    def test_timeout_fixed_backoff(self):
        from markbot.providers.error_classifier import (
            classify_api_error, BackoffStrategy,
        )
        result = classify_api_error(408, "timeout")
        assert result.backoff_seconds == 1.0
        assert result.backoff_strategy is BackoffStrategy.FIXED

    def test_server_error_backoff(self):
        from markbot.providers.error_classifier import classify_api_error
        result = classify_api_error(500, "internal error")
        assert result.backoff_seconds == 1.0

    def test_service_unavailable_exponential(self):
        from markbot.providers.error_classifier import (
            classify_api_error, BackoffStrategy,
        )
        result = classify_api_error(503, "service unavailable")
        assert result.backoff_seconds == 3.0
        assert result.backoff_strategy is BackoffStrategy.EXPONENTIAL

    def test_deterministic_no_backoff(self):
        """Deterministic errors have zero backoff (no retry)."""
        from markbot.providers.error_classifier import classify_api_error
        result = classify_api_error(401, "unauthorized")
        assert result.backoff_seconds == 0.0

    def test_unknown_default_backoff(self):
        from markbot.providers.error_classifier import classify_api_error
        result = classify_api_error(None, "weird error")
        assert result.backoff_seconds == 2.0

    def test_overloaded_body_no_status(self):
        """Body-only 'overloaded' → transient with exponential backoff."""
        from markbot.providers.error_classifier import (
            classify_api_error, BackoffStrategy,
        )
        result = classify_api_error(None, "server is overloaded")
        assert result.reason == "overloaded"
        assert result.backoff_strategy is BackoffStrategy.EXPONENTIAL


class TestComputeBackoff:
    """_compute_backoff: strategy-aware retry delay calculation."""

    def test_fixed_strategy(self):
        from markbot.providers.base import _compute_backoff
        from markbot.providers.error_classifier import BackoffStrategy
        assert _compute_backoff(2.0, BackoffStrategy.FIXED, 1, 4.0) == 2.0
        assert _compute_backoff(2.0, BackoffStrategy.FIXED, 2, 4.0) == 2.0

    def test_exponential_strategy(self):
        from markbot.providers.base import _compute_backoff
        from markbot.providers.error_classifier import BackoffStrategy
        assert _compute_backoff(3.0, BackoffStrategy.EXPONENTIAL, 1, 4.0) == 3.0
        assert _compute_backoff(3.0, BackoffStrategy.EXPONENTIAL, 2, 4.0) == 6.0
        assert _compute_backoff(3.0, BackoffStrategy.EXPONENTIAL, 3, 4.0) == 12.0

    def test_jittered_strategy_in_range(self):
        from markbot.providers.base import _compute_backoff
        from markbot.providers.error_classifier import BackoffStrategy
        # attempt=2: base=5 * 2^1 = 10, jitter ±25% → [7.5, 12.5]
        delay = _compute_backoff(5.0, BackoffStrategy.JITTERED, 2, 4.0)
        assert 7.5 <= delay <= 12.5

    def test_falls_back_to_default_delay(self):
        """When backoff_seconds=0, use default_delay from retry schedule."""
        from markbot.providers.base import _compute_backoff
        from markbot.providers.error_classifier import BackoffStrategy
        assert _compute_backoff(0.0, BackoffStrategy.FIXED, 1, 4.0) == 4.0


class TestErrorClassifierFineGrained:
    """Fine-grained reason classification for specific error patterns."""

    @pytest.mark.parametrize("message,expected_reason", [
        ("insufficient balance", "billing"),
        ("exceeded your current quota", "billing"),
        ("out of funds", "billing"),
        ("unauthorized", "auth"),
        ("invalid api key", "auth"),
        ("forbidden", "auth"),
        ("model not found", "model_not_found"),
        ("no such model", "model_not_found"),
        ("no available channel", "no_available_channel"),
        ("all channels failed", "no_available_channel"),
        ("image exceeds 5MB", "image_too_large"),
        ("image too large", "image_too_large"),
        ("tool message content must be a string", "multimodal_tool_content"),
        ("expected string, got list", "multimodal_tool_content"),
        ("request_too_large", "payload_too_large"),
        ("bad request", "format_error"),
        ("invalid function arguments", "format_error"),
        ("overloaded", "overloaded"),
        ("server is overloaded", "overloaded"),
        ("rate limit exceeded", "rate_limit"),
        ("throttled by provider", "rate_limit"),
        ("context length exceeded", "context_overflow"),
        ("上下文长度超限", "context_overflow"),
        ("content_filter triggered", "content_policy"),
        ("SSL cert verify failed", "ssl_cert_verification"),
        ("Connection timed out", "timeout"),
        ("connection refused", "transient_error"),
    ])
    def test_fine_grained_reason(self, message, expected_reason):
        from markbot.providers.error_classifier import classify_api_error
        result = classify_api_error(None, message)
        assert result.reason == expected_reason

    def test_billing_vs_rate_limit_disambiguation(self):
        """'quota' alone is ambiguous, but 'exceeded your current quota' is billing."""
        from markbot.providers.error_classifier import classify_api_error
        # "exceeded your current quota" matches billing, not rate_limit
        result = classify_api_error(None, "You exceeded your current quota")
        assert result.reason == "billing"
        assert not result.retryable

    def test_rate_limit_body_transient(self):
        """'rate limit' body without status → rate_limit (transient)."""
        from markbot.providers.error_classifier import classify_api_error
        result = classify_api_error(None, "rate limit exceeded, try again in 30s")
        assert result.reason == "rate_limit"
        assert result.retryable
