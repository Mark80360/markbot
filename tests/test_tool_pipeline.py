"""Tests for the five-stage tool execution pipeline and registry integration."""

import asyncio

import pytest

from markbot.errors import ErrorCode
from markbot.tools.base import BaseTool
from markbot.tools.pipeline import (
    PostToolDecision,
    PreblockedResult,
    PreToolDecision,
    is_preblocked_result,
)
from markbot.tools.registry import ToolRegistry
from markbot.types.permission import PermissionMode, ToolPermissionContext
from markbot.types.tool import ToolContext, ToolDefinition, ToolParameter


class Echo(BaseTool):
    def __init__(self, **defn_kwargs):
        self._kwargs = defn_kwargs

    @property
    def definition(self):
        defaults = dict(
            name="echo",
            description="echo",
            parameters=[ToolParameter("text", "string", "t")],
            is_read_only=True,
        )
        defaults.update(self._kwargs)
        return ToolDefinition(**defaults)

    async def execute(self, params, context):
        return params.get("text", "")


class Boom(Echo):
    @property
    def definition(self):
        d = super().definition
        d.name = "boom"
        return d

    async def execute(self, params, context):
        raise RuntimeError("kaboom")


def _ctx(mode=PermissionMode.AUTO, non_interactive=True):
    return ToolContext(
        session_id="s1",
        workspace=".",
        permission_mode=mode,
        tool_permission_context=ToolPermissionContext(mode=mode),
        is_non_interactive=non_interactive,
    )


@pytest.fixture
def registry():
    reg = ToolRegistry()
    reg.register(Echo())
    reg.register(Boom())
    return reg


class TestResolveStage:
    @pytest.mark.asyncio
    async def test_unknown_tool_has_code(self, registry):
        result = await registry.execute("ghost", {})
        assert "[TOOL_NOT_FOUND]" in result
        assert "hallucinated" in result

    @pytest.mark.asyncio
    async def test_soft_disabled_code_and_text(self, registry):
        registry.set_soft_disabled({"echo"})
        result = await registry.execute("echo", {"text": "x"})
        assert "[TOOL_SOFT_DISABLED]" in result
        assert "cannot be executed" in result


class TestGuardStage:
    @pytest.mark.asyncio
    async def test_guard_denies_execution(self, registry):
        calls = []
        registry.add_guard(lambda n, p, c: "denied by policy" if n == "echo" else None)
        result = await registry.execute("echo", {"text": "hi"})
        assert "[TOOL_PERMISSION_DENIED]" in str(result) or "denied" in str(result)
        assert not calls

    @pytest.mark.asyncio
    async def test_guards_are_monotonic_deny_only(self, registry):
        # A guard returning truthy ALWAYS denies; there is no allow channel.
        verdict = await registry.pipeline.stage_guards(
            "anything", {}, _ctx(), as_preblocked=True
        )
        assert verdict is None  # no guards installed -> abstain

        async def deny_all(name, params, context):
            return "no"

        registry.add_guard(deny_all)
        result = await registry.execute("echo", {"text": "hi"})
        assert "no" in str(result)

    @pytest.mark.asyncio
    async def test_guard_denial_is_preblocked_result_when_flagged(self, registry):
        def deny(name, params, context):
            return "blocked!"

        registry.pipeline._hooks.guards.append(deny)
        denial = await registry.pipeline.stage_guards("echo", {}, _ctx(), as_preblocked=True)
        assert isinstance(denial, PreblockedResult)
        assert is_preblocked_result(denial)


class TestPreExecuteStage:
    @pytest.mark.asyncio
    async def test_pre_hook_deny_short_circuits(self, registry):
        executed = []

        async def hook(name, params, context):
            return PreToolDecision.deny("not allowed here", ErrorCode.TOOL_PERMISSION_DENIED)

        registry.add_pre_execute_hook(hook)

        original = registry.pipeline.stage_execute

        async def spy(tool, name, params, context):
            executed.append(name)
            return await original(tool, name, params, context)

        registry.pipeline.stage_execute = spy
        result = await registry.execute("echo", {"text": "hi"})
        assert "[TOOL_PERMISSION_DENIED]" in str(result)
        assert not executed

    @pytest.mark.asyncio
    async def test_ask_with_approver_runs_approval_flow(self, registry):
        asked, decided = [], []

        async def hook(name, params, context):
            return PreToolDecision.ask(reason="needs confirmation")

        registry.add_pre_execute_hook(hook)
        registry.set_event_sink(lambda et, payload: asked.append(et) or decided.append(payload))

        async def approver(name, params, ctx, reason):
            return True

        registry.set_permission_approver(approver)
        result = await registry.execute("echo", {"text": "ok"}, _ctx(non_interactive=False))
        assert result == "ok"
        event_names = [a for a in asked]
        assert "approval.asked" in event_names
        assert "approval.decided" in event_names

    @pytest.mark.asyncio
    async def test_ask_denied_by_user(self, registry):
        async def hook(name, params, context):
            return PreToolDecision.ask()

        registry.add_pre_execute_hook(hook)

        async def approver(name, params, ctx, reason):
            return False

        registry.set_permission_approver(approver)
        result = await registry.execute("echo", {"text": "ok"}, _ctx(non_interactive=False))
        assert "[APPROVAL_DENIED]" in str(result)
        assert "denied by user" in str(result)


class TestExecuteStage:
    @pytest.mark.asyncio
    async def test_timeout_enforced_from_declaration(self, registry):
        class Slow(Echo):
            @property
            def definition(self):
                d = super().definition
                d.name = "slow"
                d.timeout_ms = 30
                return d

            async def execute(self, params, context):
                await asyncio.sleep(5)
                return "never"

        registry.register(Slow())
        import time

        start = time.monotonic()
        result = await registry.execute("slow", {})
        elapsed = time.monotonic() - start
        assert "[TOOL_TIMEOUT]" in str(result)
        assert elapsed < 2.0

    @pytest.mark.asyncio
    async def test_exception_becomes_typed_error_text(self, registry):
        result = await registry.execute("boom", {})
        assert "[TOOL_EXECUTION_FAILED]" in str(result)
        assert "kaboom" in str(result)


class TestPostExecuteStage:
    @pytest.mark.asyncio
    async def test_post_hook_can_replace_result(self, registry):
        async def redact(name, params, context, result):
            return PostToolDecision.replace("<redacted>")

        registry.add_post_execute_hook(redact)
        result = await registry.execute("echo", {"text": "secret"})
        assert result == "<redacted>"

    @pytest.mark.asyncio
    async def test_post_hook_failure_does_not_break_result(self, registry):
        async def broken(name, params, context, result):
            raise RuntimeError("hook bug")

        registry.add_post_execute_hook(broken)
        result = await registry.execute("echo", {"text": "fine"})
        assert result == "fine"


class TestEventSink:
    @pytest.mark.asyncio
    async def test_tool_call_and_result_emitted(self, registry):
        events = []

        def sink(event_type, payload):
            events.append((event_type, payload))

        registry.set_event_sink(sink)
        await registry.execute("echo", {"text": "x"})
        names = [e[0] for e in events]
        assert "tool.call" in names

    @pytest.mark.asyncio
    async def test_sink_exception_swallowed(self, registry):
        def bad_sink(event_type, payload):
            raise RuntimeError("sink down")

        registry.set_event_sink(bad_sink)
        result = await registry.execute("echo", {"text": "x"})
        assert result == "x"


class TestExtraGuards:
    @pytest.mark.asyncio
    async def test_extra_guard_denies_with_preblocked_semantics(self, registry):
        def turn_guard(name, params, context):
            if name == "echo":
                return "loop cap reached"
            return None

        result = await registry.execute("echo", {"text": "x"}, extra_guards=[turn_guard])
        # PreblockedResult is a str subclass — plain text assertions hold.
        assert "loop cap reached" in str(result)
        assert is_preblocked_result(result)

    @pytest.mark.asyncio
    async def test_extra_guard_abstain_executes_normally(self, registry):
        def turn_guard(name, params, context):
            return None

        result = await registry.execute("echo", {"text": "ran"}, extra_guards=[turn_guard])
        assert result == "ran"
        assert not is_preblocked_result(result)

    @pytest.mark.asyncio
    async def test_extra_guards_run_before_execution(self, registry):
        executed = []

        class Spy(Echo):
            @property
            def definition(self):
                d = super().definition
                d.name = "spy"
                return d

            async def execute(self, params, context):
                executed.append("spy")
                return "body"

        registry.register(Spy())

        def denying_guard(name, params, context):
            return "no"

        await registry.execute("spy", {}, extra_guards=[denying_guard])
        assert not executed

    @pytest.mark.asyncio
    async def test_extra_guard_exception_fails_closed(self, registry):
        """A raising guard denies the call — never silently disables it."""
        def broken(name, params, context):
            raise RuntimeError("guard bug")

        result = await registry.execute("echo", {"text": "should not run"}, extra_guards=[broken])
        assert "fail-closed" in str(result)
        assert is_preblocked_result(result)

    @pytest.mark.asyncio
    async def test_installed_guard_exception_fails_closed(self, registry):
        async def broken(name, params, context):
            raise RuntimeError("broken restriction")

        registry.add_guard(broken)
        result = await registry.execute("echo", {"text": "x"})
        assert "fail-closed" in str(result)
        assert not is_preblocked_result(result)  # installed guards → plain denial text

    @pytest.mark.asyncio
    async def test_installed_and_extra_guards_both_fire_installed_first(self, registry):
        order = []

        async def installed(name, params, context):
            order.append("installed")
            return None

        registry.add_guard(installed)

        def extra(name, params, context):
            order.append("extra")
            return "extra denial"

        result = await registry.execute("echo", {"text": "x"}, extra_guards=[extra])
        assert order == ["installed", "extra"]
        assert "extra denial" in str(result)
        assert is_preblocked_result(result)


class TestRestrict:
    @pytest.mark.asyncio
    async def test_denied_tool_hidden_and_blocked(self, registry):
        registry.restrict(denied={"boom"})
        defs = [d["function"]["name"] for d in registry.get_definitions()]
        assert "boom" not in defs
        assert "echo" in defs
        result = await registry.execute("boom", {})
        assert "[TOOL_PERMISSION_DENIED]" in str(result)

    @pytest.mark.asyncio
    async def test_allowed_whitelist_filters_others(self, registry):
        registry.restrict(allowed={"echo"})
        defs = [d["function"]["name"] for d in registry.get_definitions()]
        assert defs == ["echo"]

    def test_unknown_allowed_name_fails_loud(self, registry):
        with pytest.raises(ValueError, match="unknown allowed tools"):
            registry.restrict(allowed={"echos"})

    @pytest.mark.asyncio
    async def test_disposer_restores_surface(self, registry):
        dispose = registry.restrict(denied={"boom"})
        assert "boom" not in [d["function"]["name"] for d in registry.get_definitions()]
        dispose()
        defs = [d["function"]["name"] for d in registry.get_definitions()]
        assert set(defs) == {"echo", "boom"}
