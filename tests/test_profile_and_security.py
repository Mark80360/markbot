"""Tests for runtime profiles, permission approval, exec guards, skill gates, cron reliability."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from markbot.agent.permission_approval import PermissionApprover, _parse_choice
from markbot.config.profile import get_profile, list_profiles
from markbot.config.schema import AgentDefaults, Config, ExecToolConfig, ReliabilityConfig
from markbot.schedule.cron import CronJob, CronPayload, CronSchedule, CronService, _now_ms
from markbot.tools.base import BaseTool
from markbot.tools.registry import ToolRegistry
from markbot.tools.shell import ExecTool
from markbot.types.permission import PermissionMode, ToolPermissionContext
from markbot.types.tool import ToolContext, ToolDefinition, ToolParameter


class _DummyTool(BaseTool):
    def __init__(self, name: str = "write_file", read_only: bool = False):
        self._name = name
        self._read_only = read_only
        self.executed = False

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self._name,
            description="dummy",
            parameters=[ToolParameter(name="path", type="string", description="p", required=True)],
            is_read_only=self._read_only,
            is_destructive=not self._read_only,
        )

    async def execute(self, params, context):
        self.executed = True
        return f"ok:{params.get('path')}"


def _ctx(mode: PermissionMode = PermissionMode.DEFAULT, *, non_interactive: bool = False) -> ToolContext:
    return ToolContext(
        session_id="s",
        workspace=".",
        permission_mode=mode,
        tool_permission_context=ToolPermissionContext(mode=mode),
        is_non_interactive=non_interactive,
        channel="cli",
        chat_id="u1",
    )


class TestProfiles:
    def test_list_profiles(self):
        assert set(list_profiles()) == {"coding", "assistant", "unattended"}

    def test_assistant_defaults(self):
        p = get_profile("assistant")
        assert p.permission_mode == "default"
        assert p.exec_require_allowlist is True
        assert p.enable_desktop is False

    def test_config_profile_field(self):
        c = Config()
        assert c.agents.defaults.profile == "coding"
        assert c.agents.defaults.default_permission_mode is None
        assert isinstance(c.reliability, ReliabilityConfig)


class TestPermissionApproval:
    def test_is_allow_parser(self):
        assert _parse_choice("Allow") == "allow"
        assert _parse_choice("User selected: Allow") == "allow"
        assert _parse_choice("1") == "allow"
        assert _parse_choice("Deny") == "deny"
        assert _parse_choice("User selected: Deny") == "deny"
        # Allow All
        assert _parse_choice("2") == "allow_all"
        assert _parse_choice("Allow All") == "allow_all"
        assert _parse_choice("User selected: Allow All") == "allow_all"
        assert _parse_choice("allow-all") == "allow_all"
        # Edge: "3" or unknown → deny
        assert _parse_choice("3") == "deny"
        assert _parse_choice("no") == "deny"

    @pytest.mark.asyncio
    async def test_registry_ask_uses_approver_allow(self):
        reg = ToolRegistry()
        tool = _DummyTool()
        reg.register(tool)

        async def approver(name, params, context, reason):
            assert name == "write_file"
            return True

        reg.set_permission_approver(approver)
        result = await reg.execute("write_file", {"path": "a.txt"}, context=_ctx())
        assert result == "ok:a.txt"
        assert tool.executed is True

    @pytest.mark.asyncio
    async def test_registry_ask_uses_approver_deny(self):
        reg = ToolRegistry()
        tool = _DummyTool()
        reg.register(tool)

        async def approver(name, params, context, reason):
            return False

        reg.set_permission_approver(approver)
        result = await reg.execute("write_file", {"path": "a.txt"}, context=_ctx())
        assert "denied by user" in result
        assert tool.executed is False

    @pytest.mark.asyncio
    async def test_registry_ask_non_interactive_without_approver(self):
        reg = ToolRegistry()
        reg.register(_DummyTool())
        result = await reg.execute(
            "write_file",
            {"path": "a.txt"},
            context=_ctx(non_interactive=True),
        )
        assert "non-interactive" in result


class TestExecGuard:
    def test_blocks_nested_shell(self):
        tool = ExecTool(working_dir=".", restrict_to_workspace=True)
        err = tool._guard_command("bash -c 'rm -rf /tmp/x'", ".")
        assert err is not None
        assert "blocked" in err.lower()

    def test_blocks_command_substitution(self):
        tool = ExecTool(working_dir=".", restrict_to_workspace=True)
        err = tool._guard_command("echo $(whoami)", ".")
        assert err is not None

    def test_allowlist_required(self):
        tool = ExecTool(
            working_dir=".",
            restrict_to_workspace=True,
            require_allowlist=True,
            allow_patterns=[r"^ls\b"],
        )
        assert tool._guard_command("ls -la", ".") is None
        err = tool._guard_command("curl http://example.com", ".")
        assert err is not None
        assert "allowlist" in err.lower()

    def test_empty_allowlist_with_require(self):
        tool = ExecTool(
            working_dir=".",
            require_allowlist=True,
            allow_patterns=[],
        )
        err = tool._guard_command("ls", ".")
        assert err is not None
        assert "empty" in err.lower()


class TestCronReliability:
    @pytest.mark.asyncio
    async def test_retries_then_dead_letter(self, tmp_path: Path):
        from markbot.schedule.cron import CronJob, CronJobState, CronPayload

        calls = {"n": 0}

        async def on_job(job):
            calls["n"] += 1
            raise RuntimeError("boom")

        failures = []

        async def on_failure(job, error):
            failures.append((job.id, error))

        store = tmp_path / "jobs.json"
        store.write_text('{"version": 1, "jobs": []}', encoding="utf-8")
        svc = CronService(
            store,
            on_job=on_job,
            max_retries=2,
            retry_delay_s=0.01,
            dead_letter_keep=10,
            on_failure=on_failure,
        )
        job = CronJob(
            id="x1",
            name="failjob",
            schedule=CronSchedule(kind="every", every_ms=60_000),
            payload=CronPayload(message="do thing", channel="cli", to="user"),
            state=CronJobState(next_run_at_ms=_now_ms() - 1),
        )
        svc._store = svc._load_store()
        svc._store.jobs.append(job)
        await svc._execute_job(job)
        assert calls["n"] == 3  # 1 + 2 retries
        assert job.state.last_status == "error"
        assert failures and "boom" in failures[0][1]
        letters = svc.list_dead_letters()
        assert letters and letters[-1]["job_name"] == "failjob"

    @pytest.mark.asyncio
    async def test_success_no_retry(self, tmp_path: Path):
        from markbot.schedule.cron import CronJob, CronJobState, CronPayload

        calls = {"n": 0}

        async def on_job(job):
            calls["n"] += 1
            return "ok"

        store = tmp_path / "jobs.json"
        store.write_text('{"version": 1, "jobs": []}', encoding="utf-8")
        svc = CronService(store, on_job=on_job, max_retries=3, retry_delay_s=0.01)
        job = CronJob(
            id="x2",
            name="okjob",
            schedule=CronSchedule(kind="every", every_ms=60_000),
            payload=CronPayload(message="hi"),
            state=CronJobState(),
        )
        svc._store = svc._load_store()
        svc._store.jobs.append(job)
        await svc._execute_job(job)
        assert calls["n"] == 1
        assert job.state.last_status == "ok"
