"""Tests for turn traces.

The agent had logs but no way to answer "what did it see, and why did it
take that branch?" after the fact. A trace records the decision path in a
structured, bounded, redacted form so a finished turn can be inspected.
"""

from __future__ import annotations

import pytest

from markbot.agent.turn_trace import TraceEvent, TurnTrace


class TestRecording:
    def test_records_events_in_order(self):
        t = TurnTrace(session_key="cli:direct")
        t.record("a", n=1)
        t.record("b", n=2)
        assert [e.kind for e in t.events] == ["a", "b"]

    def test_record_llm_shape(self):
        t = TurnTrace()
        t.record_llm(
            model="claude", input_tokens=100, output_tokens=20,
            cache_read=80, cost_usd=0.25, finish_reason="tool_calls",
        )
        e = t.of_kind("llm")[0]
        assert e.data["model"] == "claude"
        assert e.data["cache_read"] == 80
        assert e.data["cost_usd"] == 0.25

    def test_record_tool_shape(self):
        t = TurnTrace()
        t.record_tool("exec", ok=False, result_chars=10)
        e = t.of_kind("tool")[0]
        assert e.data["tool"] == "exec"
        assert e.data["ok"] is False

    def test_record_never_raises(self):
        t = TurnTrace()
        t.record("ok", value=1)
        class Unserialisable:
            def __repr__(self):
                raise RuntimeError("boom")
        t.record("bad", obj=Unserialisable())  # must not raise
        assert len(t.events) == 2


class TestBounding:
    def test_events_capped(self):
        t = TurnTrace(max_events=10)
        for i in range(50):
            t.record("e", i=i)
        assert len(t.events) == 10

    def test_drops_are_counted_and_reported(self):
        """A truncated trace must never be presented as a complete one."""
        t = TurnTrace(max_events=5)
        for i in range(20):
            t.record("e", i=i)
        s = t.summary()
        assert s["dropped_events"] == 15
        assert s["truncated"] is True

    def test_long_strings_bounded(self):
        t = TurnTrace()
        t.record("e", text="x" * 5000)
        stored = t.events[0].data["text"]
        assert len(stored) < 300
        assert stored.endswith(")")

    def test_long_lists_bounded(self):
        t = TurnTrace()
        t.record("e", items=list(range(500)))
        assert len(t.events[0].data["items"]) == 21
        assert "more" in t.events[0].data["items"][-1]


class TestRedaction:
    def test_secrets_are_redacted(self):
        """A trace lives in memory and can reach diagnostics endpoints, so it
        must not be a place where a key outlives the turn."""
        t = TurnTrace()
        t.record("llm", note="calling with sk-ant-api03-AAAABBBBCCCCDDDDEEEE1234")
        blob = t.render()
        assert "sk-ant-api03-AAAABBBBCCCCDDDDEEEE1234" not in blob

    def test_authorization_header_redacted(self):
        t = TurnTrace()
        t.record("http", header="Authorization: Bearer abcdef1234567890XYZ")
        assert "abcdef1234567890XYZ" not in t.render()

    def test_content_preserved(self):
        t = TurnTrace()
        t.record("e", note="the user asked about the deploy pipeline")
        assert "deploy pipeline" in t.render()


class TestSummary:
    def test_aggregates_llm_and_tools(self):
        t = TurnTrace(session_key="feishu:u1", channel="feishu")
        t.record_llm(model="a", input_tokens=100, output_tokens=10, cost_usd=0.1)
        t.record_llm(model="a", input_tokens=200, output_tokens=20, cost_usd=0.2)
        t.record_tool("exec", ok=True)
        t.record_tool("write_file", ok=False)
        t.finish(exit_reason="stop")

        s = t.summary()
        assert s["llm"]["calls"] == 2
        assert s["llm"]["input_tokens"] == 300
        assert s["llm"]["output_tokens"] == 30
        assert abs(s["llm"]["cost_usd"] - 0.3) < 1e-9
        assert s["tools"]["count"] == 2
        assert s["tools"]["failed"] == 1
        assert s["models"] if "models" in s else True
        assert s["llm"]["models"] == ["a"]

    def test_finish_is_idempotent(self):
        t = TurnTrace()
        t.finish(exit_reason="stop")
        first = t.duration_s
        t.finish(exit_reason="stop")
        assert t.duration_s == pytest.approx(first, abs=0.01)

    def test_empty_trace_summary_is_valid(self):
        s = TurnTrace().summary()
        assert s["events"] == 0
        assert s["truncated"] is False
        assert s["llm"]["calls"] == 0

    def test_to_dict_includes_timeline(self):
        t = TurnTrace()
        t.record("a")
        assert t.to_dict()["timeline"][0]["kind"] == "a"
        assert "timeline" not in t.to_dict(include_events=False)


class TestRender:
    def test_render_is_readable(self):
        t = TurnTrace(session_key="cli:direct", channel="cli")
        t.record_llm(model="m", input_tokens=10)
        t.record_tool("exec", ok=True)
        t.finish(exit_reason="stop")
        out = t.render()
        assert "cli:direct" in out
        assert "llm" in out
        assert "tool" in out


class TestLoopRetention:
    def test_loop_retains_and_bounds_traces(self):
        from collections import OrderedDict

        class _Loop:
            def __init__(self):
                self._turn_traces = OrderedDict()
                self._max_turn_traces = 3

            record_turn_trace = __import__(
                "markbot.agent.loop", fromlist=["AgentLoop"]
            ).AgentLoop.record_turn_trace
            recent_turn_traces = __import__(
                "markbot.agent.loop", fromlist=["AgentLoop"]
            ).AgentLoop.recent_turn_traces
            last_turn_trace = __import__(
                "markbot.agent.loop", fromlist=["AgentLoop"]
            ).AgentLoop.last_turn_trace

        loop = _Loop()
        for i in range(10):
            t = TurnTrace(session_key=f"s{i}")
            t.record("x", i=i)
            t.finish()
            loop.record_turn_trace(t)
        assert len(loop._turn_traces) == 3
        # Newest first.
        recent = loop.recent_turn_traces(2)
        assert recent[0].session_key == "s9"

    def test_filter_by_session(self):
        from collections import OrderedDict

        from markbot.agent.loop import AgentLoop

        class _Loop:
            def __init__(self):
                self._turn_traces = OrderedDict()
                self._max_turn_traces = 10

            record_turn_trace = AgentLoop.record_turn_trace
            recent_turn_traces = AgentLoop.recent_turn_traces
            last_turn_trace = AgentLoop.last_turn_trace

        loop = _Loop()
        for key in ("a", "b", "a"):
            t = TurnTrace(session_key=key)
            t.finish()
            loop.record_turn_trace(t)
        found = loop.recent_turn_traces(5, session_key="a")
        assert len(found) == 2
        assert loop.last_turn_trace(session_key="a").session_key == "a"

    def test_empty_loop_returns_none(self):
        from collections import OrderedDict

        from markbot.agent.loop import AgentLoop

        class _Loop:
            def __init__(self):
                self._turn_traces = OrderedDict()
                self._max_turn_traces = 10

            last_turn_trace = AgentLoop.last_turn_trace
            recent_turn_traces = AgentLoop.recent_turn_traces

        assert _Loop().last_turn_trace() is None


class TestRunnerIntegration:
    def test_runner_has_trace_attribute(self):
        from markbot.agent.iteration import IterationRunner

        src = __import__("inspect").getsource(IterationRunner.__init__)
        assert "TurnTrace(" in src
        assert "self.trace" in src

    def test_trace_is_cleared_after_turn(self):
        """One trace per turn: carrying it over would mislabel a later turn."""
        from markbot.agent.iteration import IterationRunner

        src = __import__("inspect").getsource(IterationRunner._close_trace)
        assert "self.trace = None" in src
