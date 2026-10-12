"""Tests for per-call tool routing (cross-session contamination).

The tool registry holds *singletons*. Several of them kept their routing on
instance fields written by ``AgentLoop._set_tool_context``, which is called
once per turn — and, until this fix, again mid-turn. With the default of 3
concurrent sessions, whichever turn ran last decided where everyone else's
messages, cron jobs and memory writes went.

The fix is that every tool that needs routing reads the per-call
``ToolContext`` and treats the instance field as a fallback only.
"""

from __future__ import annotations

import asyncio

import pytest

from markbot.types.permission import PermissionMode, ToolPermissionContext
from markbot.types.tool import ToolContext


def _ctx(channel: str, chat_id: str, *, is_group: bool | None = None) -> ToolContext:
    return ToolContext(
        session_id=f"{channel}:{chat_id}",
        workspace=".",
        permission_mode=PermissionMode.DEFAULT,
        tool_permission_context=ToolPermissionContext(mode=PermissionMode.DEFAULT),
        is_non_interactive=False,
        channel=channel,
        chat_id=chat_id,
        is_group=is_group,
    )


class TestCronTool:
    def _tool(self):
        from markbot.tools.cron import CronTool

        class _Cron:
            def __init__(self):
                self.added = []

            def add_job(self, **kw):
                self.added.append(kw)
                return type("J", (), {"name": kw["name"], "id": "j1"})()

        return CronTool(cron_service=_Cron())

    async def test_uses_per_call_context(self):
        t = self._tool()
        t.set_context("feishu", "group:STALE")
        await t._legacy_execute(
            action="add", message="ping", every_seconds=60,
            _tool_context=_ctx("feishu", "group:REAL"),
        )
        assert t._cron.added[0]["channel"] == "feishu"
        assert t._cron.added[0]["to"] == "group:REAL"

    async def test_does_not_mutate_instance_state(self):
        """Writing the resolved channel back onto the singleton is the bug."""
        t = self._tool()
        t.set_context("feishu", "original")
        await t._legacy_execute(
            action="add", message="ping", every_seconds=60,
            _tool_context=_ctx("slack", "other"),
        )
        assert t._channel == "feishu"
        assert t._chat_id == "original"

    async def test_concurrent_sessions_do_not_cross(self):
        t = self._tool()
        t.set_context("feishu", "default")

        async def call(channel: str, chat: str) -> None:
            await t._legacy_execute(
                action="add", message="ping", every_seconds=60,
                _tool_context=_ctx(channel, chat),
            )

        await asyncio.gather(
            call("feishu", "alice"),
            call("slack", "bob"),
            call("discord", "carol"),
        )
        targets = sorted(j["to"] for j in t._cron.added)
        assert targets == ["alice", "bob", "carol"]

    async def test_falls_back_without_context(self):
        t = self._tool()
        t.set_context("cli", "direct")
        out = await t._legacy_execute(
            action="add", message="ping", every_seconds=60,
        )
        assert "Created job" in out
        assert t._cron.added[0]["to"] == "direct"


class TestMemoryToolsPrivacyGate:
    """The curated-memory gate is a privacy boundary: a group chat must not
    read or write a private user's MEMORY.md."""

    def _save_tool(self):
        from markbot.tools.memory_tools import MemorySaveTool

        calls = []

        class _Mgr:
            def allow_curated_memory(self, channel, *, session_id=None, is_group=None):
                from markbot.utils.constants import is_main_memory_session

                calls.append(channel)
                return is_main_memory_session(channel, is_group=is_group)

            async def add_memory(self, **kw):
                calls.append(kw)
                return True

        t = MemorySaveTool()
        t._memory_manager = _Mgr()
        return t, calls

    async def test_group_caller_blocked_despite_stale_singleton(self):
        t, calls = self._save_tool()
        # A private cli session ran first and left the singleton pointing at
        # itself. The group call must still be evaluated as a group.
        t.set_session_context("cli", "user", is_group=False)
        out = await t._legacy_execute(
            content="secret", _tool_context=_ctx("feishu", "group:1", is_group=True)
        )
        assert "only available in" in out
        assert calls == ["feishu"]

    async def test_private_caller_allowed_even_if_singleton_is_group(self):
        t, calls = self._save_tool()
        t.set_session_context("feishu", "group:1", is_group=True)
        out = await t._legacy_execute(
            content="a preference", _tool_context=_ctx("cli", "direct", is_group=False)
        )
        assert "Error" not in out
        assert calls[0] == "cli"

    async def test_writes_carry_per_call_channel(self):
        t, calls = self._save_tool()
        await t._legacy_execute(
            content="x", _tool_context=_ctx("cli", "direct", is_group=False)
        )
        assert calls[1]["channel"] == "cli"


class TestContextExplorerPrivacyGate:
    def _load_tool(self, tmp_path):
        from markbot.tools.context_explorer import LoadContextTool
        from markbot.utils.constants import MEMORY_FILENAME

        (tmp_path / MEMORY_FILENAME).write_text("private facts\n", encoding="utf-8")
        return LoadContextTool(workspace=tmp_path)

    async def test_group_cannot_load_memory_md(self, tmp_path):
        tool = self._load_tool(tmp_path)
        tool.set_session_context("cli", "user", is_group=False)
        out = await tool._legacy_execute(
            context_id="MEMORY_md", _tool_context=_ctx("feishu", "g1", is_group=True)
        )
        assert "unavailable on shared channels" in out
        assert "private facts" not in out

    async def test_private_session_can(self, tmp_path):
        tool = self._load_tool(tmp_path)
        tool.set_session_context("feishu", "g1", is_group=True)
        out = await tool._legacy_execute(
            context_id="MEMORY_md", _tool_context=_ctx("cli", "direct", is_group=False)
        )
        assert "unavailable on shared channels" not in out

    async def test_fallback_without_context_unchanged(self, tmp_path):
        """Callers that do not build a ToolContext must keep working."""
        tool = self._load_tool(tmp_path)
        tool.set_session_context("feishu", "g1", is_group=True)
        out = await tool._legacy_execute(context_id="MEMORY_md")
        assert "unavailable on shared channels" in out


class TestPerCallSessionHelper:
    def test_prefers_tool_context(self):
        from markbot.tools.context_explorer import _per_call_session

        class T:
            _channel = "stale"
            _chat_id = "stale"
            _is_group = True

        ch, cid, grp = _per_call_session(
            T(), {"_tool_context": _ctx("slack", "c1", is_group=False)}
        )
        assert (ch, cid, grp) == ("slack", "c1", False)

    def test_falls_back_to_instance(self):
        from markbot.tools.context_explorer import _per_call_session

        class T:
            _channel = "stale"
            _chat_id = "s1"
            _is_group = True

        assert _per_call_session(T(), {}) == ("stale", "s1", True)

    def test_empty_context_channel_falls_back(self):
        from markbot.tools.context_explorer import _per_call_session

        class T:
            _channel = "fallback"
            _chat_id = "f1"
            _is_group = None

        ch, cid, _ = _per_call_session(T(), {"_tool_context": _ctx("", "")})
        assert (ch, cid) == ("fallback", "f1")

    def test_group_flag_falls_back_when_context_omits_it(self):
        """Callers that build a ToolContext without ``is_group`` keep the
        previous behaviour rather than silently becoming 'unknown'."""
        from markbot.tools.context_explorer import _per_call_session

        class T:
            _channel = "cli"
            _chat_id = "d"
            _is_group = True

        ch, _, grp = _per_call_session(T(), {"_tool_context": _ctx("cli", "d")})
        assert ch == "cli"
        assert grp is True


class TestNoMidTurnRebinding:
    def test_iteration_does_not_rebind_tools(self):
        """The mid-turn call was what made the contamination happen on every
        single turn rather than only at turn start. Checked via the AST so a
        comment mentioning the name cannot mask a real call."""
        import ast
        import inspect
        import textwrap

        from markbot.agent.iteration import IterationRunner

        tree = ast.parse(
            textwrap.dedent(inspect.getsource(IterationRunner._phase_execute_tools))
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                assert name != "_set_tool_context", (
                    "mid-turn tool re-binding reintroduced cross-session leakage"
                )

    def test_still_bound_at_turn_start(self):
        from markbot.agent.loop import AgentLoop

        assert callable(AgentLoop._set_tool_context)
