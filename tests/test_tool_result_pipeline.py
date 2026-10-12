"""Tests for the unified tool-result pipeline.

``_phase_execute_tools`` used to contain the result post-processing inline,
plus a second, reduced copy on the hallucinated-tool recovery path. That copy
silently skipped output truncation/offload, the progress callbacks, the
TOOL_CALLED/TOOL_COMPLETED events, the inactivity-clock touch and the
stagnation feed — so a single model typo degraded a turn into an unmonitored,
unbounded-output execution mode.

These tests assert both paths now share one implementation, and that the
shared implementation actually applies every guarantee.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from types import SimpleNamespace

import pytest

from markbot.agent.iteration import IterationRunner


class _FakeContext:
    """Stands in for ContextBuilder; mirrors the real return contract."""

    async def add_tool_result_async(self, messages, cid, name, result):
        messages.append(
            {"role": "tool", "tool_call_id": cid, "name": name,
             "content": result if isinstance(result, str) else result}
        )
        return messages


def _runner():
    r = object.__new__(IterationRunner)
    r.loop = SimpleNamespace(
        compactor=SimpleNamespace(
            config=SimpleNamespace(
                tool_output_inline_chars=20000,
                tool_output_preview_chars=200,
            )
        ),
        context=_FakeContext(),
    )
    r._runtime_budget = SimpleNamespace(touch_activity=lambda: None)
    r._flush_messages = lambda state: None
    r._record_tool_result = lambda *a, **k: None
    r._record_file_mutation = lambda *a, **k: None
    r._record_verification_call = lambda *a, **k: None
    r._is_tool_result_failure = lambda result, name: False
    r._batch_tool_count = 1
    emitted: list = []
    r._emit = lambda et, payload: emitted.append((et, payload))
    r.emitted = emitted
    r.on_tool_complete = None
    return r


def _state():
    return SimpleNamespace(messages=[], file_mutations=[])


def _call(cid="c1", name="exec"):
    return SimpleNamespace(id=cid, name=name, arguments={})


class TestSharedImplementation:
    def test_both_paths_call_apply_tool_result(self):
        main = textwrap.dedent(
            inspect.getsource(IterationRunner._phase_execute_tools)
        )
        recovery = textwrap.dedent(
            inspect.getsource(IterationRunner._run_hallucinated_tool_recovery)
        )
        assert "_apply_tool_result(" in main
        assert "_apply_tool_result(" in recovery, (
            "the recovery path must go through _apply_tool_result too"
        )

    def test_no_duplicate_inline_post_processing(self):
        """The inline block is gone; only the shared helper owns it."""
        for method in (
            IterationRunner._phase_execute_tools,
            IterationRunner._run_hallucinated_tool_recovery,
        ):
            tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
            calls = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    f = node.func
                    calls.add(getattr(f, "attr", None) or getattr(f, "id", None))
            for moved in (
                "offload_tool_output",
                "_record_file_mutation",
                "_record_verification_call",
                "_record_tool_result",
            ):
                assert moved not in calls, (
                    f"{moved} must live in _apply_tool_result, not be "
                    f"duplicated in {method.__name__}"
                )

    def test_recovery_path_also_notifies_stagnation(self):
        rec = textwrap.dedent(
            inspect.getsource(IterationRunner._run_hallucinated_tool_recovery)
        )
        assert "_note_tool_batch_execution(" in rec
        assert "_inject_pending_steer(" in rec
        assert "_phase_check_failure_loop(" in rec


class TestApplyToolResult:
    async def test_appends_result(self):
        r = _runner()
        s = _state()
        await r._apply_tool_result(s, _call(), "hello")
        assert s.messages[0]["content"] == "hello"

    async def test_exception_coerced_to_string(self):
        r = _runner()
        s = _state()
        await r._apply_tool_result(s, _call(), ValueError("boom"))
        assert "ValueError" in s.messages[0]["content"]
        assert any(e[0].name == "TOOL_FAILED" for e in r.emitted)

    async def test_emits_tool_completed(self):
        r = _runner()
        await r._apply_tool_result(_state(), _call(), "ok")
        assert any(e[0].name == "TOOL_COMPLETED" for e in r.emitted)

    async def test_multimodal_not_stringified(self):
        r = _runner()
        s = _state()
        payload = {"_multimodal": True, "text_summary": "shot taken"}
        await r._apply_tool_result(s, _call("c", "computer_use"), payload)
        # The dict survives so add_tool_result can split text + image.
        assert isinstance(s.messages[0]["content"], dict)

    async def test_on_tool_complete_called(self):
        r = _runner()
        seen = []

        async def cb(cid, name, summary, error):
            seen.append((cid, name, summary, error))

        r.on_tool_complete = cb
        await r._apply_tool_result(_state(), _call(), "done")
        assert seen and seen[0][2] == "done"

    async def test_touches_inactivity_clock(self):
        r = _runner()
        touched = []
        r._runtime_budget = SimpleNamespace(touch_activity=lambda: touched.append(1))
        await r._apply_tool_result(_state(), _call(), "x")
        assert touched


class TestInlineBudget:
    def test_floor_applies_at_high_fanout(self):
        r = _runner()
        r._batch_tool_count = 1
        assert r._inline_budget_for("x", None) == 20000
        # 8 tools -> 2500, still above the floor.
        r._batch_tool_count = 8
        assert r._inline_budget_for("x", None) == 2500
        # 100 tools -> 200, clamped to the floor.
        r._batch_tool_count = 100
        assert r._inline_budget_for("x", None) == 2000

    async def test_oversized_output_is_bounded(self):
        r = _runner()
        s = _state()
        r._batch_tool_count = 1
        big = "x" * 50000
        await r._apply_tool_result(s, _call(), big)
        # Either offloaded (marked) or truncated — never the full 50k inline.
        assert len(s.messages[0]["content"]) < len(big)


class TestSourceStats:
    def test_phase_execute_tools_is_much_smaller(self):
        """The whole point: the god-method shed its inlined body.

        It was 343 lines with the result pipeline duplicated; the shared
        helpers now own that logic.
        """
        body = inspect.getsource(IterationRunner._phase_execute_tools)
        assert len(body.splitlines()) < 180, len(body.splitlines())

    def test_progress_reporter_is_a_factory(self):
        """Extracted so the recovery path gets the same reporting; the
        recovery branch previously had no progress callback at all."""
        assert callable(IterationRunner._make_progress_reporter)
