"""Regression tests for incremental turn durability and safe tool batching.

These cover the Hermes-inspired reliability changes: per-write persistence
boundary, synthetic-control filtering, and conservative tool-call batching.
"""

from unittest.mock import MagicMock

import pytest

from markbot.agent.iteration import (
    IterationRunner,
    LoopState,
    _INTERNAL_CONTEXT_TAG,
)
from markbot.agent.services.executor import ToolExecutor
from markbot.tools.base import BaseTool
from markbot.tools.registry import ToolRegistry
from markbot.types.tool import ToolDefinition


class _Rot(BaseTool):
    def __init__(self, name, readonly=True, res=None):
        self._name = name
        self._readonly = readonly
        self._res = res or ("ok:" + name)

    @property
    def definition(self):
        return ToolDefinition(
            name=self._name, description="d", parameters=[], is_read_only=self._readonly
        )

    @property
    def name(self):
        return self._name

    def is_read_only(self, params):
        return self._readonly

    async def execute(self, params, context):
        return self._res


class _FakeTools:
    def __init__(self):
        self.map = {}

    def get(self, n):
        return self.map.get(n)

    async def execute(self, name, params, context=None):
        return await self.map[name].execute(params, context)


class _Loop:
    def __init__(self, tools):
        self.tools = tools
        self.tool_executor = ToolExecutor(tools)
        self.sessions = MagicMock()
        self._messages_revision = 0


@pytest.fixture
def runner():
    tools = _FakeTools()
    tools.map["read"] = _Rot("read", True)
    tools.map["write"] = _Rot("write", False)
    loop = _Loop(tools)
    session = MagicMock()
    session.messages = []
    return IterationRunner(loop, "cli", "c1", "m1", session=session), session, loop


class TestSaveMessagesFiltering:
    def test_drops_verify_stop_nudge(self):
        ex = ToolExecutor(ToolRegistry())
        s = MagicMock()
        s.messages = []
        ex.save_messages(
            s,
            [
                {"role": "assistant", "content": "answer"},
                {"role": "user", "content": "nudge", "_verify_stop_nudge": True},
            ],
        )
        assert len(s.messages) == 1
        assert s.messages[0]["role"] == "assistant"

    def test_drops_todo_reinjection(self):
        ex = ToolExecutor(ToolRegistry())
        s = MagicMock()
        s.messages = []
        ex.save_messages(
            s,
            [
                {"role": "system", "content": "snapshot", "_todo_reinjection": True},
                {"role": "user", "content": "real"},
            ],
        )
        assert len(s.messages) == 1
        assert s.messages[0]["content"] == "real"

    def test_truncates_tool_result(self):
        ex = ToolExecutor(ToolRegistry())
        s = MagicMock()
        s.messages = []
        ex.save_messages(s, [{"role": "tool", "name": "exec", "content": "x" * 20000}])
        assert len(s.messages) == 1
        assert len(s.messages[0]["content"]) < 20000

    def test_save_turn_backward_compatible(self):
        ex = ToolExecutor(ToolRegistry())
        s = MagicMock()
        s.messages = []
        ex.save_turn(
            s,
            [
                {"role": "system", "content": "prompt"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
            skip=1,
        )
        assert len(s.messages) == 2
        assert s.messages[0]["content"] == "hello"


class TestIncrementalFlush:
    def test_persists_inbound_before_side_effects(self, runner):
        r, session, loop = runner
        state = LoopState(
            messages=[{"role": "system", "content": "p"}, {"role": "user", "content": "q"}],
            initial_count=2,
            new_msg_start=2,
            persisted_upto=0,
        )
        r._flush_current_inbound(state)
        assert len(session.messages) == 1
        assert session.messages[0]["content"] == "q"
        assert state.persisted_upto == 2
        loop.sessions.save.assert_called_once_with(session)

    def test_advances_cursor_without_saving_synthetic(self, runner):
        r, session, loop = runner
        state = LoopState(
            messages=[{"role": "system", "content": "p"}, {"role": "user", "content": "q"}],
            initial_count=2,
            new_msg_start=2,
            persisted_upto=2,
        )
        state.messages.append({"role": "assistant", "content": "answer"})
        r._flush_messages(state)
        assert len(session.messages) == 1
        state.messages.append({"role": "user", "content": "nudge", "_verify_stop_nudge": True})
        r._flush_messages(state)
        # cursor advanced but synthetic was not persisted
        assert len(session.messages) == 1
        assert state.persisted_upto == 4


class TestSafeBatching:
    def test_groups_reads_serializes_writes(self, runner):
        r, _, _ = runner
        calls = [type("T", (), {"name": "write", "arguments": {}})(),
                 type("T", (), {"name": "read", "arguments": {}})(),
                 type("T", (), {"name": "read", "arguments": {}})(),
                 type("T", (), {"name": "write", "arguments": {}})()]
        assert r._plan_tool_batches(calls) == [[0], [1, 2], [3]]

    def test_preserves_order_in_results(self, runner):
        import asyncio
        r, _, _ = runner
        calls = [
            type("T", (), {"name": "read", "arguments": {}})(),
            type("T", (), {"name": "write", "arguments": {}})(),
            type("T", (), {"name": "read", "arguments": {}})(),
        ]
        res = asyncio.run(r._execute_tool_calls_safely(calls, None))
        assert res == ["ok:read", "ok:write", "ok:read"]
