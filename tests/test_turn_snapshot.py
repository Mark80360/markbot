"""Assembled-turn snapshot tests.

Drives a full IterationRunner.run() against scripted LLM responses and a
real ToolRegistry / ContextBuilder / Session, then asserts on the world's
observable output:

* the final transcript shape the model produced,
* the durable session message list,
* the session event log — including the core invariant that projecting
  the log reproduces the model-visible transcript ("model-visible ⟺ logged").

This is markbot's analogue of dsh's keyless snapshot harness: behaviour
changes to the loop surface as a diffable transcript, not as scattered
mock-call assertions.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from markbot.agent.cache_policy import CacheMutationPolicy
from markbot.agent.cost import CostTracker
from markbot.agent.iteration import IterationRunner
from markbot.agent.services.executor import ToolExecutor
from markbot.providers.base import LLMResponse, ToolCallRequest
from markbot.providers.fallback import FallbackAttempt
from markbot.session.session import Session
from markbot.tools.base import BaseTool
from markbot.tools.registry import ToolRegistry
from markbot.types.permission import PermissionMode
from markbot.types.tool import ToolDefinition, ToolParameter


class Echo(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="echo",
            description="Echo the text back",
            parameters=[ToolParameter("text", "string", "text to echo")],
            is_read_only=True,
        )

    async def execute(self, params: dict[str, Any], context: Any) -> str:
        return f"echo:{params.get('text', '')}"


class ScriptedFallbackManager:
    """Returns pre-canned LLMResponses in order, like dsh's llm-replay."""

    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    async def chat_with_fallback(self, messages, tools=None, **kwargs):
        self.calls.append([dict(m) for m in messages])
        response = self._responses.pop(0)
        attempt = FallbackAttempt(model_ref="test/model", success=True)
        return response, [attempt]

    async def chat_stream_with_fallback(self, messages, tools=None, **kwargs):
        return await self.chat_with_fallback(messages, tools, **kwargs)


def _strip_volatile(message: dict[str, Any]) -> dict[str, Any]:
    """Project a transcript message down to its model-visible identity."""
    keep = {"role", "content", "tool_call_id", "name"}
    out = {k: v for k, v in message.items() if k in keep}
    if "tool_calls" in message:
        out["tool_calls"] = [
            {
                "id": tc.get("id"),
                "function": {
                    "name": tc["function"]["name"],
                    # Normalise argument whitespace for stable snapshots.
                    "arguments": tc["function"]["arguments"],
                },
            }
            for tc in message["tool_calls"]
        ]
    return out


def _event_identity(envelope: dict[str, Any]) -> dict[str, Any]:
    """Project one event envelope to its stable identity for snapshots."""
    import json as _json

    payload = envelope.get("payload") or {}
    identity: dict[str, Any] = {"event": envelope.get("event")}
    for key in ("content", "tool_call_id", "name"):
        value = payload.get(key)
        if value:
            identity[key] = value
    if "tool_calls" in payload:
        calls = []
        for tc in payload["tool_calls"]:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = _json.loads(args)
                except (ValueError, TypeError):
                    pass
            call: dict[str, Any] = {"id": tc.get("id"), "function": {"name": fn.get("name")}}
            if args is not None:
                call["function"]["arguments"] = args
            calls.append(call)
        identity["tool_calls"] = calls
    return identity


class HarnessLoop:
    """Minimal AgentLoop stand-in exposing exactly what IterationRunner reads."""

    def __init__(self, tmp_path: Path, fallback_manager: ScriptedFallbackManager):
        self.config = None
        self.max_iterations = 5
        self.context_window_tokens = 100_000
        self.workspace = str(tmp_path)
        self.model = "test-model"

        self.tools = ToolRegistry()
        self.tools.register(Echo())
        self.tool_executor = ToolExecutor(self.tools)

        self.skill_registry = SimpleNamespace(get=lambda name: None)
        self.guardrail_manager = SimpleNamespace(
            active_skills=set(),
            check_all=lambda *a, **k: [],
            start_guarding=lambda skill: None,
        )
        self.compaction_hook = None
        self.context_engine = SimpleNamespace(should_compress=lambda *a, **k: False)
        from markbot.agent.compact import CompactionConfig

        self.compactor = SimpleNamespace(
            config=CompactionConfig(),
            maybe_compact=None,
        )
        self.prefix_stability = None
        self.llm_response_cache = None
        self.token_estimate_cache = None

        self.fallback_manager = fallback_manager
        self.cost_tracker = CostTracker()

        self.saved_sessions: list[Session] = []
        self.sessions = SimpleNamespace(save=self.saved_sessions.append)

        from markbot.agent.context import ContextBuilder

        self.context = ContextBuilder(tmp_path)

        self._messages_revision = 0
        self.cache_mutation_policy = CacheMutationPolicy()

        self.handoff_manager = None
        self.memory_manager = None
        self.bootstrap_hook = None
        self.task_tracker = None
        self._memory_search_tool = None
        self._interaction_log = SimpleNamespace(
            log_interaction=lambda **kwargs: None,
            log_tool_execution=lambda **kwargs: None,
        )

    # -- AgentLoop methods the runner calls -----------------------------

    def get_failure_state(self, session_key: str) -> dict[str, Any]:
        return {"reflection_injected_count": 0, "failed_methods": [], "forced_stop_count": 0}

    def reset_failure_state(self, session_key: str) -> None:
        pass

    @staticmethod
    def _strip_orphan_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return Session._strip_orphan_tool_results(messages)

    def _gather_task_context(self) -> str:
        return ""

    def _recalc_new_msg_start_after_compact(
        self, messages: list[dict], initial_count: int, new_msg_start: int
    ) -> int:
        return min(new_msg_start, len(messages))

    def drain_steer(self, session_key: str) -> list[Any]:
        return []

    def _set_tool_context(
        self,
        channel: str,
        chat_id: str,
        message_id: str | None = None,
        *,
        is_group: bool | None = None,
    ) -> None:
        pass

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        return ", ".join(tc.name for tc in tool_calls)


@pytest.fixture
def echo_registry(tmp_path):
    reg = ToolRegistry()
    reg.register(Echo())
    return reg


def _run_turn(tmp_path: Path, responses: list[LLMResponse]) -> tuple[Any, Session, ScriptedFallbackManager]:
    scripted = ScriptedFallbackManager(responses)
    loop = HarnessLoop(tmp_path, scripted)
    session = Session(key="cli:snapshot")
    runner = IterationRunner(
        loop,
        "cli",
        "snapshot",
        "m1",
        session=session,
        permission_mode_override=PermissionMode.AUTO,
    )
    initial_messages = [
        {"role": "system", "content": "You are a test agent."},
        {"role": "user", "content": "run the echo tool"},
    ]

    async def _drive():
        return await runner.run(initial_messages)

    final_content, tools_used, messages, save_start = asyncio.run(_drive())
    out = SimpleNamespace(
        final_content=final_content,
        tools_used=tools_used,
        messages=messages,
        save_start=save_start,
    )
    return out, session, scripted


class TestAssembledTurnSnapshot:
    def test_tool_then_final_answer_transcript(self, tmp_path):
        responses = [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(id="call_1", name="echo", arguments={"text": "hello"})
                ],
                finish_reason="tool_calls",
                usage={"input_tokens": 50, "output_tokens": 10},
            ),
            LLMResponse(
                content="All done.",
                finish_reason="stop",
                usage={"input_tokens": 80, "output_tokens": 5},
            ),
        ]
        out, session, scripted = _run_turn(tmp_path, responses)

        assert out.final_content == "All done."
        assert out.tools_used == ["echo"]

        # Transcript identity (roles/content only — volatile fields stripped).
        transcript = [_strip_volatile(m) for m in out.messages]
        assert transcript[0] == {"role": "system", "content": "You are a test agent."}
        assert transcript[1] == {"role": "user", "content": "run the echo tool"}
        assert transcript[2]["role"] == "assistant"
        assert transcript[2]["tool_calls"][0]["function"]["name"] == "echo"
        assert transcript[3] == {
            "role": "tool",
            "content": "echo:hello",
            "tool_call_id": "call_1",
            "name": "echo",
        }
        assert transcript[-1] == {"role": "assistant", "content": "All done."}

    def test_second_llm_call_sees_tool_result(self, tmp_path):
        responses = [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(id="call_9", name="echo", arguments={"text": "x"})
                ],
                finish_reason="tool_calls",
                usage={},
            ),
            LLMResponse(content="done", finish_reason="stop", usage={}),
        ]
        out, _, scripted = _run_turn(tmp_path, responses)
        second_call_messages = scripted.calls[1]
        roles = [m["role"] for m in second_call_messages]
        assert roles == ["system", "user", "assistant", "tool"]
        assert second_call_messages[-1]["content"] == "echo:x"

    def test_event_log_projection_matches_transcript(self, tmp_path):
        """Core invariant: model-visible ⟺ logged.

        Deriving model history from the session's event log must reproduce
        the durable transcript (modulo volatile fields).
        """
        responses = [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(id="call_1", name="echo", arguments={"text": "log"})
                ],
                finish_reason="tool_calls",
                usage={},
            ),
            LLMResponse(content="complete", finish_reason="stop", usage={}),
        ]
        out, session, _ = _run_turn(tmp_path, responses)

        projected = [
            _strip_volatile(m) for m in session.derive_messages()
        ]
        durable = [_strip_volatile(m) for m in session.messages]
        assert projected == durable
        # And the durable transcript ends with the final answer.
        assert durable[-1]["content"] == "complete"

    def test_event_log_snapshot_shape(self, tmp_path):
        responses = [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(id="call_1", name="echo", arguments={"text": "s"})
                ],
                finish_reason="tool_calls",
                usage={},
            ),
            LLMResponse(content="fin", finish_reason="stop", usage={}),
        ]
        _, session, _ = _run_turn(tmp_path, responses)
        identities = [_event_identity(e) for e in session.events]
        assert identities == [
            {"event": "user_message", "content": "run the echo tool"},
            {
                "event": "assistant_message",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {"name": "echo", "arguments": {"text": "s"}},
                    }
                ],
            },
            {"event": "tool_result", "content": "echo:s", "tool_call_id": "call_1", "name": "echo"},
            {"event": "assistant_message", "content": "fin"},
        ]
        seqs = [e["seq"] for e in session.events]
        assert seqs == list(range(len(seqs)))

    def test_guard_denial_flows_through_pipeline_as_preblocked(self, tmp_path):
        """A turn-scoped guard denial surfaces as PreblockedResult text in
        the transcript without executing the tool."""
        responses = [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(id="call_2", name="echo", arguments={"text": "no"})
                ],
                finish_reason="tool_calls",
                usage={},
            ),
            LLMResponse(content="gave up politely", finish_reason="stop", usage={}),
        ]
        scripted = ScriptedFallbackManager(responses)
        loop = HarnessLoop(tmp_path, scripted)
        session = Session(key="cli:blocked")

        runner = IterationRunner(
            loop, "cli", "blocked", "m1", session=session,
            permission_mode_override=PermissionMode.AUTO,
        )
        # Install a turn guard that denies everything by patching the
        # runner's enforcement closure through extra_guards: easiest via
        # a registry-level guard installed for this turn.
        dispose = loop.tools.pipeline.add_guard(lambda n, p, c: "turn budget exhausted")
        try:
            initial = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "go"},
            ]
            final_content, tools_used, messages, _ = asyncio.run(runner.run(initial))
        finally:
            dispose()

        assert final_content == "gave up politely"
        # The denial text reached the transcript as a tool result.
        tool_results = [m for m in messages if m.get("role") == "tool"]
        assert tool_results, "denial should be visible as tool result"
        assert any("turn budget exhausted" in str(m.get("content")) for m in tool_results)
