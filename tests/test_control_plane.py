"""Tests for markbot control-plane enhancements.

Covers:
- pure tool_guardrails decision engine + pre-block
- cache mutation deferred policy (config-driven)
- DelegationPolicy spawn limits / force_auto wiring surface
- service-gated tool registry exposure
- CommandDef catalog / help SSOT
"""

from __future__ import annotations

import pytest

from markbot.agent.cache_policy import CacheMutationPolicy, MutationKind
from markbot.agent.subagent.capability import CapabilityToken
from markbot.agent.subagent.policy import DelegationPolicy, DelegationTracker
from markbot.agent.tool_guardrails import (
    GuardrailAction,
    GuardrailConfig,
    PreblockedResult,
    ToolCallGuardrail,
    is_failure_result,
    is_preblocked_result,
)
from markbot.cli.slash_commands.builtin import build_builtin_catalog, register_builtin_commands
from markbot.cli.slash_commands.router import CommandRouter
from markbot.config.schema import Config, ToolsConfig
from markbot.tools.base import BaseTool
from markbot.tools.registry import ToolRegistry
from markbot.types.tool import ToolContext, ToolDefinition


# ---------------------------------------------------------------------------
# Tool guardrails
# ---------------------------------------------------------------------------


class TestToolCallGuardrail:
    def test_exact_failure_warn_then_block(self):
        g = ToolCallGuardrail(
            GuardrailConfig(exact_failure_warn=2, exact_failure_block=3)
        )
        args = {"path": "/nope"}
        d1 = g.observe("read_file", args, "Error: not found", is_failure=True)
        assert d1.action is GuardrailAction.ALLOW
        d2 = g.observe("read_file", args, "Error: not found", is_failure=True)
        assert d2.action is GuardrailAction.WARN
        d3 = g.observe("read_file", args, "Error: not found", is_failure=True)
        assert d3.action is GuardrailAction.BLOCK
        assert g.is_call_blocked("read_file", args)
        assert g.is_signature_blocked("read_file", args)
        msg = g.block_message("read_file", args)
        assert "blocked" in msg.lower()
        assert "read_file" in msg

    def test_tool_streak_blocks_whole_tool(self):
        g = ToolCallGuardrail(
            GuardrailConfig(tool_streak_warn=2, tool_streak_block=3)
        )
        for i, args in enumerate(({"c": "a"}, {"c": "b"}, {"c": "c"})):
            d = g.observe("exec", args, "Error: fail", is_failure=True)
            if i < 1:
                assert d.action is GuardrailAction.ALLOW
            elif i == 1:
                assert d.action is GuardrailAction.WARN
            else:
                assert d.action is GuardrailAction.BLOCK
        assert g.is_call_blocked("exec", {"c": "anything"})
        assert "blocked for this turn" in g.block_message("exec").lower()

    def test_no_progress_on_idempotent_tool(self):
        g = ToolCallGuardrail(
            GuardrailConfig(no_progress_warn=2, no_progress_block=3)
        )
        args = {"path": "README.md"}
        content = "hello world content"
        assert g.observe("read_file", args, content).action is GuardrailAction.ALLOW
        assert g.observe("read_file", args, content).action is GuardrailAction.WARN
        assert g.observe("read_file", args, content).action is GuardrailAction.BLOCK
        assert g.is_call_blocked("read_file", args)

    def test_failure_window_halt_after_reflections(self):
        g = ToolCallGuardrail(
            GuardrailConfig(
                window_size=4,
                window_failure_threshold=3,
                max_reflections=1,
            )
        )
        for _ in range(3):
            g.observe("exec", {"command": "x"}, "Error: fail", is_failure=True)
        # First window trip is WARN (reflection available).
        w = g.evaluate_failure_window()
        assert w.action is GuardrailAction.WARN
        g.note_reflection()
        for _ in range(3):
            g.observe("exec", {"command": "y"}, "Error: fail", is_failure=True)
        w2 = g.evaluate_failure_window()
        assert w2.action is GuardrailAction.HALT

    def test_success_clears_exact_failure(self):
        g = ToolCallGuardrail(GuardrailConfig(exact_failure_warn=2))
        args = {"path": "a"}
        g.observe("read_file", args, "Error: x", is_failure=True)
        g.observe("read_file", args, "ok content", is_failure=False)
        d = g.observe("read_file", args, "Error: x", is_failure=True)
        assert d.action is GuardrailAction.ALLOW

    def test_persisted_roundtrip(self):
        g = ToolCallGuardrail()
        g.note_reflection()
        g.note_failed_method("exec", "Error: boom")
        g.note_forced_stop()
        payload = g.to_persisted()
        g2 = ToolCallGuardrail()
        g2.load_persisted(payload)
        assert g2.state.reflection_count == 1
        assert g2.state.forced_stop_count == 1
        assert g2.state.failed_methods == ["exec: Error: boom"]

    def test_from_settings_mapping(self):
        cfg = GuardrailConfig.from_settings(
            {"enabled": False, "exact_failure_warn": 9, "window_size": 10}
        )
        assert cfg.enabled is False
        assert cfg.exact_failure_warn == 9
        assert cfg.window_size == 10
        g = ToolCallGuardrail(cfg)
        d = g.observe("exec", {"c": "x"}, "Error: x", is_failure=True)
        assert d.action is GuardrailAction.ALLOW  # disabled


# ---------------------------------------------------------------------------
# Cache policy
# ---------------------------------------------------------------------------


class TestCacheMutationPolicy:
    def test_defers_during_active_turn(self):
        p = CacheMutationPolicy(defer_mutations=True)
        p.begin_turn()
        called = {"n": 0}

        def apply():
            called["n"] += 1

        msg = p.request(MutationKind.TOOLS, "disable web", apply)
        assert "Deferred" in msg
        assert called["n"] == 0
        assert p.peek_pending() == ["disable web"]

        applied = p.end_turn()
        assert applied == ["disable web"]
        assert called["n"] == 1
        assert p.peek_pending() == []

    def test_force_now_applies_immediately(self):
        p = CacheMutationPolicy(defer_mutations=True)
        p.begin_turn()
        called = {"n": 0}
        msg = p.request(
            MutationKind.SKILLS, "reload", lambda: called.__setitem__("n", 1), now=True
        )
        assert "Applied now" in msg
        assert called["n"] == 1

    def test_outside_turn_applies_immediately(self):
        p = CacheMutationPolicy(defer_mutations=True)
        called = {"n": 0}
        msg = p.request(MutationKind.PROFILE, "switch", lambda: called.__setitem__("n", 1))
        assert "Applied now" in msg
        assert called["n"] == 1

    def test_defer_mutations_false_applies_immediately(self):
        p = CacheMutationPolicy.from_settings({"defer_mutations": False})
        assert p.defer_mutations is False
        p.begin_turn()
        called = {"n": 0}
        msg = p.request(MutationKind.TOOLS, "flip", lambda: called.__setitem__("n", 1))
        assert "Applied now" in msg
        assert called["n"] == 1
        assert p.peek_pending() == []

    def test_from_settings_object(self):
        p = CacheMutationPolicy.from_settings(
            type("C", (), {"defer_mutations": False})()
        )
        assert p.defer_mutations is False


# ---------------------------------------------------------------------------
# Delegation policy
# ---------------------------------------------------------------------------


class TestDelegationPolicy:
    def test_depth_limit(self):
        p = DelegationPolicy(max_spawn_depth=1)
        ok, reason = p.check_can_spawn(
            current_depth=1, running_children=0, session_child_count=0
        )
        assert not ok
        assert "depth" in reason.lower()

    def test_concurrency_limit(self):
        p = DelegationPolicy(max_concurrent_children=2)
        ok, reason = p.check_can_spawn(
            current_depth=0, running_children=2, session_child_count=0
        )
        assert not ok
        assert "concurrent" in reason.lower()

    def test_harden_capability_merges_blocked(self):
        p = DelegationPolicy(blocked_tools=frozenset({"spawn", "message", "cron"}))
        cap = CapabilityToken(
            allowed_tools=("read_file", "spawn"),
            forbidden_tools=("exec",),
        )
        hard = p.harden_capability(cap)
        assert "spawn" in hard.forbidden_tools
        assert "message" in hard.forbidden_tools
        assert "cron" in hard.forbidden_tools
        assert "exec" in hard.forbidden_tools

    def test_tracker_depth(self):
        t = DelegationTracker(policy=DelegationPolicy(max_spawn_depth=2))
        assert t.depth_of(None) == 0
        d = t.register_child("c1", None, "sess")
        assert d == 1
        d2 = t.register_child("c2", "c1", "sess")
        assert d2 == 2
        assert t.session_count("sess") == 2

    def test_from_mapping_force_auto(self):
        p = DelegationPolicy.from_mapping(
            {
                "force_auto_permission": False,
                "max_spawn_depth": 2,
                "blocked_tools": ["spawn", "cron"],
            }
        )
        assert p.force_auto_permission is False
        assert p.max_spawn_depth == 2
        assert "spawn" in p.blocked_tools
        assert "cron" in p.blocked_tools


# ---------------------------------------------------------------------------
# Service-gated tools
# ---------------------------------------------------------------------------


class _GatedTool(BaseTool):
    def __init__(self, name: str, available: bool = True):
        self._name = name
        self._available = available

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self._name,
            description="test",
            parameters=[],
            is_read_only=True,
        )

    def available_when(self) -> bool:
        return self._available

    async def execute(self, params, context: ToolContext):
        return "ok"


class TestServiceGatedRegistry:
    def test_unavailable_tool_hidden_from_definitions(self):
        reg = ToolRegistry()
        reg.register(_GatedTool("always", available=True))
        reg.register(_GatedTool("never", available=False))
        names = [d.name for d in reg.definitions]
        assert "always" in names
        assert "never" not in names
        openai_names = [d["function"]["name"] for d in reg.get_definitions()]
        assert "always" in openai_names
        assert "never" not in openai_names

    def test_register_gated_skips_unavailable(self):
        reg = ToolRegistry()
        ok = reg.register_gated(_GatedTool("x", available=False))
        assert ok is False
        assert not reg.has("x")
        ok2 = reg.register_gated(_GatedTool("y", available=True))
        assert ok2 is True
        assert reg.has("y")

    def test_definitions_cache_invalidates_on_gate_flip(self):
        reg = ToolRegistry()
        tool = _GatedTool("flip", available=True)
        reg.register(tool)
        assert len(reg.get_definitions()) == 1
        tool._available = False
        # Cache key is based on available names — re-eval should drop it.
        assert reg.get_definitions() == []


# ---------------------------------------------------------------------------
# CommandDef catalog
# ---------------------------------------------------------------------------


class TestCommandCatalog:
    def test_catalog_registers_priority_and_exact(self):
        router = CommandRouter()
        register_builtin_commands(router)
        assert "/stop" in router._priority
        assert "/steer" in router._priority
        assert "/help" in router._exact
        assert "/new" in router._exact
        catalog = getattr(router, "catalog", None)
        assert catalog is not None
        help_text = catalog.help_text()
        assert "/stop" in help_text
        assert "/help" in help_text

    def test_build_builtin_catalog_has_control_plane(self):
        catalog = build_builtin_catalog()
        control = [d for d in catalog if d.control_plane]
        names = {d.canonical for d in control}
        assert "/stop" in names
        assert "/steer" in names
        assert "/status" in names


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


class TestControlPlaneConfig:
    def test_tools_config_has_guardrails_delegation_cache(self):
        cfg = Config()
        assert cfg.tools.guardrails.enabled is True
        assert cfg.tools.delegation.max_spawn_depth == 1
        assert cfg.tools.delegation.max_concurrent_children == 3
        assert "spawn" in cfg.tools.delegation.blocked_tools
        assert cfg.tools.delegation.force_auto_permission is True
        assert cfg.tools.cache_policy.defer_mutations is True

    def test_tools_config_nested_override(self):
        tools = ToolsConfig.model_validate(
            {
                "guardrails": {"exactFailureWarn": 5, "enabled": False},
                "delegation": {
                    "maxSpawnDepth": 2,
                    "maxConcurrentChildren": 5,
                    "forceAutoPermission": False,
                },
                "cache_policy": {"deferMutations": False},
            }
        )
        assert tools.guardrails.enabled is False
        assert tools.guardrails.exact_failure_warn == 5
        assert tools.delegation.max_spawn_depth == 2
        assert tools.delegation.max_concurrent_children == 5
        assert tools.delegation.force_auto_permission is False
        assert tools.cache_policy.defer_mutations is False


# ---------------------------------------------------------------------------
# Context engine adapter smoke
# ---------------------------------------------------------------------------


class TestContextEngine:
    def test_adapter_importable(self):
        from markbot.agent.context_engine import (
            CompactorContextEngine,
            ContextEngine,
            ContextEngineResult,
        )

        assert issubclass(CompactorContextEngine, ContextEngine)
        r = ContextEngineResult(messages=[], action="none")
        assert r.changed is False
        r2 = ContextEngineResult(messages=[], action="auto_compact")
        assert r2.changed is True


# ---------------------------------------------------------------------------
# Spawn schema coherence
# ---------------------------------------------------------------------------


class TestSpawnToolSchema:
    def test_task_and_tasks_both_optional_in_schema(self):
        from markbot.agent.subagent.spawn import SpawnTool

        class _Mgr:
            pass

        tool = SpawnTool(_Mgr())  # type: ignore[arg-type]
        schema = tool.definition.to_openai_schema()
        props = schema["function"]["parameters"]["properties"]
        required = schema["function"]["parameters"].get("required") or []
        assert "task" in props
        assert "tasks" in props
        assert "template" in props
        assert "task" not in required
        assert "tasks" not in required


# ---------------------------------------------------------------------------
# Footprint / Outcome / Templates / Runtime budget
# ---------------------------------------------------------------------------


class TestToolFootprint:
    def test_soft_disable_hides_from_schema(self):
        from markbot.agent.footprint import ToolFootprint, apply_footprint_to_registry
        from markbot.tools.base import BaseTool
        from markbot.tools.registry import ToolRegistry
        from markbot.types.tool import ToolDefinition

        class _T(BaseTool):
            def __init__(self, name: str):
                self._name = name

            @property
            def definition(self) -> ToolDefinition:
                return ToolDefinition(name=self._name, description="t", parameters=[])

            async def execute(self, params, context):
                return "ok"

        reg = ToolRegistry()
        reg.register(_T("read_file"))
        reg.register(_T("computer_use"))
        assert "computer_use" in [d["function"]["name"] for d in reg.get_definitions()]

        fp = ToolFootprint()
        fp.disable("computer_use")
        apply_footprint_to_registry(reg, fp)
        names = [d["function"]["name"] for d in reg.get_definitions()]
        assert "read_file" in names
        assert "computer_use" not in names
        # Soft-disable does not unregister.
        assert reg.has("computer_use")

    @pytest.mark.asyncio
    async def test_soft_disabled_execute_denied(self):
        """Soft-disabled tools stay registered but must not execute."""
        from markbot.agent.footprint import ToolFootprint, apply_footprint_to_registry
        from markbot.tools.base import BaseTool
        from markbot.tools.registry import ToolRegistry
        from markbot.types.tool import ToolDefinition

        class _T(BaseTool):
            def __init__(self, name: str):
                self._name = name
                self.ran = False

            @property
            def definition(self) -> ToolDefinition:
                return ToolDefinition(name=self._name, description="t", parameters=[])

            async def execute(self, params, context):
                self.ran = True
                return "ok"

        reg = ToolRegistry()
        tool = _T("computer_use")
        reg.register(tool)
        fp = ToolFootprint()
        fp.disable("computer_use")
        apply_footprint_to_registry(reg, fp)
        result = await reg.execute("computer_use", {})
        assert "soft-disabled" in str(result).lower() or "cannot be executed" in str(result)
        assert tool.ran is False

    def test_profile_apply_disables_desktop_group(self):
        from markbot.agent.footprint import ToolFootprint
        from markbot.config.profile import get_profile

        fp = ToolFootprint()
        fp.apply_profile(get_profile("assistant"))
        assert "computer_use" in fp.soft_disabled


class TestOutcomeGate:
    def test_nudge_on_unverified_mutations(self):
        from markbot.agent.outcome import OutcomeAction, OutcomeGate, OutcomeGateConfig

        gate = OutcomeGate(OutcomeGateConfig(max_nudges=2))
        d = gate.evaluate(
            surface="cli",
            file_mutations=[{"path": "a.py"}],
            verification_done=False,
            side_effect_pending=False,
            nudge_count=0,
        )
        assert d.action is OutcomeAction.NUDGE

    def test_allow_on_messaging_surface(self):
        from markbot.agent.outcome import OutcomeAction, OutcomeGate

        gate = OutcomeGate()
        d = gate.evaluate(
            surface="feishu",
            file_mutations=[{"path": "a.py"}],
            verification_done=False,
            side_effect_pending=True,
            nudge_count=0,
        )
        assert d.action is OutcomeAction.ALLOW

    def test_footer_after_max_nudges(self):
        from markbot.agent.outcome import OutcomeAction, OutcomeGate, OutcomeGateConfig

        gate = OutcomeGate(OutcomeGateConfig(max_nudges=1))
        d = gate.evaluate(
            surface="cli",
            file_mutations=[{"path": "a.py"}],
            verification_done=False,
            side_effect_pending=False,
            nudge_count=1,
        )
        assert d.action is OutcomeAction.FOOTER

    def test_runner_footer_after_max_nudges(self):
        """IterationRunner FOOTER path is wired (not just OutcomeGate unit)."""
        from unittest.mock import MagicMock

        from markbot.agent.iteration import IterationRunner, LoopState
        from markbot.agent.outcome import OutcomeGate, OutcomeGateConfig

        runner = IterationRunner.__new__(IterationRunner)
        runner.channel = "cli"
        runner.loop = MagicMock()
        runner._outcome_gate = OutcomeGate(OutcomeGateConfig(max_nudges=1))
        state = LoopState(
            messages=[],
            initial_count=0,
            file_mutations=[{"path": "a.py", "ok": True}],
            verify_nudges=1,
            verification_done=False,
        )
        assert runner._should_inject_verify_nudge(state) is False
        footer = runner._outcome_footer_message(state)
        assert "Outcome Gate" in footer
        assert "without verification" in footer


class TestCapabilityTemplates:
    def test_resolve_template_name(self):
        from markbot.agent.subagent.templates import list_templates, resolve_capability

        assert "research" in list_templates()
        cap = resolve_capability(template="research")
        assert "read_file" in cap.allowed_tools
        assert not cap.allows("write_file")

    def test_resolve_capability_template_shorthand(self):
        from markbot.agent.subagent.templates import resolve_capability

        cap = resolve_capability({"template": "verify"})
        assert cap.allows("exec")
        assert not cap.allows("write_file")

    def test_format_result_payload_structured(self):
        from markbot.agent.subagent.templates import format_result_payload

        text = format_result_payload(
            status="ok",
            task_id="t1",
            label="lab",
            task="do thing",
            result="done",
            artifacts=["a.py"],
            evidence=["exec: ok"],
            residual_risk="",
        )
        assert "status: ok" in text
        assert "artifacts:" in text
        assert "a.py" in text


class TestRuntimeBudget:
    def test_wall_time_hit(self):
        from markbot.agent.budget_axes import BudgetAxis, RuntimeBudget, RuntimeBudgetConfig

        b = RuntimeBudget(RuntimeBudgetConfig(max_wall_seconds=0.0))
        hit = b.evaluate()
        assert hit.hit
        assert hit.axis is BudgetAxis.WALL_TIME

    def test_token_hit(self):
        from markbot.agent.budget_axes import BudgetAxis, RuntimeBudget, RuntimeBudgetConfig

        b = RuntimeBudget(RuntimeBudgetConfig(max_total_tokens=10))
        b.add_tokens(11)
        hit = b.evaluate()
        assert hit.axis is BudgetAxis.TOKENS


class TestSkillMutationDefer:
    def test_bind_cache_policy_defers_script_reload(self, tmp_path):
        from markbot.agent.cache_policy import CacheMutationPolicy, MutationKind
        from markbot.skills.core.registry import SkillRegistry
        from markbot.tools.registry import ToolRegistry
        from markbot.types.skill import SkillDefinition

        reg = ToolRegistry()
        skills = SkillRegistry(tmp_path, tool_registry=reg)
        policy = CacheMutationPolicy(defer_mutations=True)
        policy.begin_turn()
        skills.bind_cache_policy(policy)

        skill = SkillDefinition(
            name="demo",
            description="d",
            when_to_use="test",
            scripts=[],
        )
        # Directly schedule reload with empty scripts (no-op apply still queued).
        skills._schedule_script_tools_reload(skill)
        assert policy.peek_pending()
        assert any("demo" in p for p in policy.peek_pending())
        # Apply at turn end.
        applied = policy.end_turn()
        assert applied


class TestMcpRegistrationDefer:
    def test_staged_mcp_tools_register_via_policy(self):
        """MCP wrappers staged outside registry, then registered by mutation."""
        from markbot.agent.cache_policy import CacheMutationPolicy, MutationKind
        from markbot.tools.base import BaseTool
        from markbot.tools.registry import ToolRegistry
        from markbot.types.tool import ToolDefinition

        class _FakeMcp(BaseTool):
            def __init__(self, name: str):
                self._name = name

            @property
            def definition(self) -> ToolDefinition:
                return ToolDefinition(name=self._name, description="mcp", parameters=[])

            async def execute(self, params, context):
                return "ok"

        reg = ToolRegistry()
        policy = CacheMutationPolicy(defer_mutations=True)
        policy.begin_turn()
        tools = [_FakeMcp("mcp_demo_ping"), _FakeMcp("mcp_demo_list")]

        def _register() -> None:
            for t in tools:
                reg.register(t)
            reg.invalidate_definitions_cache()

        status = policy.request(
            MutationKind.TOOLS, f"register {len(tools)} MCP tool(s)", _register
        )
        assert "Deferred" in status
        assert not reg.has("mcp_demo_ping")
        applied = policy.end_turn()
        assert applied
        assert reg.has("mcp_demo_ping")
        assert reg.has("mcp_demo_list")
        names = [d["function"]["name"] for d in reg.get_definitions()]
        assert "mcp_demo_ping" in names


class TestProfileHotSwitch:
    def test_set_profile_defers_tool_surface_mid_turn(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from markbot.agent.cache_policy import CacheMutationPolicy
        from markbot.agent.footprint import ToolFootprint, apply_footprint_to_registry
        from markbot.agent.loop import AgentLoop
        from markbot.config.profile import get_profile
        from markbot.tools.base import BaseTool
        from markbot.tools.registry import ToolRegistry
        from markbot.types.tool import ToolDefinition

        class _T(BaseTool):
            def __init__(self, name: str):
                self._name = name

            @property
            def definition(self) -> ToolDefinition:
                return ToolDefinition(name=self._name, description="t", parameters=[])

            async def execute(self, params, context):
                return "ok"

        reg = ToolRegistry()
        for n in ("read_file", "computer_use", "browser_navigate"):
            reg.register(_T(n))

        loop = AgentLoop.__new__(AgentLoop)
        loop.tools = reg
        loop.tool_footprint = ToolFootprint()
        loop.tool_footprint.apply_profile(get_profile("coding"))
        apply_footprint_to_registry(reg, loop.tool_footprint)
        loop.cache_mutation_policy = CacheMutationPolicy(defer_mutations=True)
        loop.cache_mutation_policy.begin_turn()
        loop.ctx = SimpleNamespace(profile=get_profile("coding"), app_state=None)
        loop.request_cache_mutation = (
            lambda kind, description, apply, now=False: loop.cache_mutation_policy.request(
                kind, description, apply, now=now
            )
        )

        # coding has computer_use available; assistant soft-disables it.
        before = [d["function"]["name"] for d in reg.get_definitions()]
        assert "computer_use" in before

        status = AgentLoop.set_profile(loop, "assistant")
        assert "Deferred" in status
        # Mid-turn: schema not yet changed.
        mid = [d["function"]["name"] for d in reg.get_definitions()]
        assert "computer_use" in mid

        loop.cache_mutation_policy.end_turn()
        after = [d["function"]["name"] for d in reg.get_definitions()]
        assert "computer_use" not in after
        assert "read_file" in after
        assert loop.tool_footprint.profile_name == "assistant"

    def test_set_profile_applies_immediately_outside_turn(self):
        from types import SimpleNamespace

        from markbot.agent.cache_policy import CacheMutationPolicy
        from markbot.agent.footprint import ToolFootprint, apply_footprint_to_registry
        from markbot.agent.loop import AgentLoop
        from markbot.config.profile import get_profile
        from markbot.tools.base import BaseTool
        from markbot.tools.registry import ToolRegistry
        from markbot.types.tool import ToolDefinition

        class _T(BaseTool):
            def __init__(self, name: str):
                self._name = name

            @property
            def definition(self) -> ToolDefinition:
                return ToolDefinition(name=self._name, description="t", parameters=[])

            async def execute(self, params, context):
                return "ok"

        reg = ToolRegistry()
        reg.register(_T("computer_use"))
        reg.register(_T("read_file"))

        loop = AgentLoop.__new__(AgentLoop)
        loop.tools = reg
        loop.tool_footprint = ToolFootprint()
        loop.tool_footprint.apply_profile(get_profile("coding"))
        apply_footprint_to_registry(reg, loop.tool_footprint)
        loop.cache_mutation_policy = CacheMutationPolicy(defer_mutations=True)
        # no begin_turn → outside turn
        loop.ctx = SimpleNamespace(profile=get_profile("coding"), app_state=None)
        loop.request_cache_mutation = (
            lambda kind, description, apply, now=False: loop.cache_mutation_policy.request(
                kind, description, apply, now=now
            )
        )

        status = AgentLoop.set_profile(loop, "assistant")
        assert "Applied now" in status
        names = [d["function"]["name"] for d in reg.get_definitions()]
        assert "computer_use" not in names
        assert loop.tool_footprint.profile_name == "assistant"
