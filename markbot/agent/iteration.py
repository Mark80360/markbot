"""Iteration runner: decomposes the monolithic _run_agent_loop into phases.

Each phase is a separate method, making the loop logic testable and
extensible without touching the 400+ line method directly.

Enhanced with:
- Session handoff generation on loop finalization
- Session bootstrap validation on first iteration
- Active memory encoding on loop finalization
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from markbot.agent.cache_policy import CacheMutationPolicy
from markbot.agent.compact import CompactAction, is_prompt_too_long_error, offload_tool_output
from markbot.agent.cost import BudgetExceededError
from markbot.agent.cache_protocol import (
    CanonicalUsage,
    CacheEvent,
    UsageNormaliser,
)
from markbot.agent.prefix_cache import (
    PrefixStabilityManager,
    system_prompt_text,
)
from markbot.agent.stream import StreamFilter
from markbot.agent.tokens import token_count_with_estimation
from markbot.agent.tool_guardrails import (
    GuardrailAction,
    GuardrailConfig,
    PreblockedResult,
    ToolCallGuardrail,
    is_failure_result,
    is_preblocked_result,
)
from markbot.bus.events import EventType, make_session_key
from markbot.session.handoff import build_handoff_from_session
from markbot.types.permission import PermissionMode, ToolPermissionContext
from markbot.types.tool import ToolContext
from markbot.utils.helpers import strip_think

if TYPE_CHECKING:
    from markbot.agent.loop import AgentLoop
    from markbot.session.session import Session

# Tag for internal context messages injected after system prompt.
# These are per-turn and should not be persisted to session history.
_INTERNAL_CONTEXT_TAG = "[Agent Internal Context"


# ---------------------------------------------------------------------------
# Turn-exit-reason taxonomy
# ---------------------------------------------------------------------------

class TurnExitReason(str, Enum):
    """Structured reason for why an agent turn ended.

    Set on ``LoopState.exit_reason`` at every exit point and consumed by
    ``_finalize_loop`` for logging, handoff documentation, and metrics.
    Inspired by agent's ``_turn_exit_reason`` design, this replaces
    ad-hoc log-only exit reporting with a queryable enum that flows into
    session handoffs so the next turn / "继续" knows why the last turn stopped.
    """

    COMPLETED = "completed"
    """LLM returned a final response with no tool calls — normal completion."""

    MAX_ITERATIONS = "max_iterations"
    """Hit ``max_iterations`` limit without a final response."""

    BUDGET_EXCEEDED = "budget_exceeded"
    """Cost budget exceeded mid-turn."""

    LLM_ERROR = "llm_error"
    """LLM call failed with a non-recoverable error."""

    CONTENT_FILTER = "content_filter"
    """Provider content-safety policy blocked the response."""

    PTL_RECOVERY_FAILED = "ptl_recovery_failed"
    """Prompt-too-long error and reactive compaction could not fix it."""

    GUARDRAIL_HALT = "guardrail_halt"
    """Tool-loop guardrail forced stop (failure window / reflections exhausted)."""

    INVALID_TOOL_MAX_RETRIES = "invalid_tool_max_retries"
    """LLM kept calling non-existent tools after 3 self-correction retries."""

    STEER_CONTINUE = "steer_continue"
    """User injected a mid-task instruction; loop continues (not a real exit)."""

    TOOL_CONTINUE = "tool_continue"
    """Tools executed; loop continues to next iteration (not a real exit)."""


# ---------------------------------------------------------------------------
# File-mutation verifier constants
# ---------------------------------------------------------------------------

# Tools whose successful execution produces a real file-system mutation.
# Used by the file-mutation verifier footer to detect "LLM claims done but
# nothing actually changed on disk" — a common failure mode where the model
# confidently reports completing file work that never landed.
MUTATING_TOOL_NAMES: frozenset[str] = frozenset({
    "write_file",
    "edit_file",
    "delete_file",
})

# Tools that count as "verification" for the verify-on-stop nudge. When the
# model edits files and then tries to end its turn *without* having called
# one of these, we inject a nudge asking it to run the project's verify
# command (tests / linter / type-checker) before declaring done. Mirrors
# agent's verify-on-stop: "completion requires both done AND verified".
VERIFICATION_TOOL_NAMES: frozenset[str] = frozenset({
    "exec",          # shell — runs pytest, npm test, make, etc.
    "shell",
    "run_command",
    "code_execution",  # in-process Python (sandboxed)
})

# File extensions that are pure documentation — editing them does not
# require running a verify command. Avoids spurious nudges when the model
# only touched README / changelog / docs.
_NO_VERIFY_EXTENSIONS: frozenset[str] = frozenset({
    ".md", ".markdown", ".mdx", ".rst", ".txt",
    ".license", ".licence",
})

# Maximum number of verify-on-stop nudges per turn. Two is enough: the
# first gives the model a chance to run tests; if it still tries to end
# without verifying, we let it (the user can see the warning footer).
_MAX_VERIFY_NUDGES = 2

# Channels where verify-on-stop is active. On messaging channels
# (feishu/qq/weixin/dingtalk/email/telegram/discord), the verify nudge
# is noise — users can't see terminal output and running pytest in a
# chat context doesn't make sense. Mirrors agent's surface-aware
# toggle: local programming surfaces ON, messaging OFF.
_VERIFY_ON_STOP_SURFACES: frozenset[str] = frozenset({"cli", "web"})

# Keywords in the LLM's final response that signal a file-completion claim.
# When these appear but no mutating tool succeeded this turn, the verifier
# footer is appended to surface the mismatch.
_FILE_COMPLETION_CLAIM_PATTERNS: tuple[str, ...] = (
    # English
    "i've updated", "i have updated", "i've created", "i have created",
    "i've modified", "i have modified", "i've written", "i have written",
    "i've deleted", "i have deleted", "i've changed", "i have changed",
    "i've edited", "i have edited", "i've saved", "i have saved",
    "the file has been", "changes saved", "done editing", "file updated",
    "file created", "file modified", "file written", "successfully wrote",
    "successfully edited", "successfully updated",
    # Chinese
    "已更新", "已创建", "已修改", "已删除", "已写入", "已编辑", "已保存",
    "已完成修改", "已完成创建", "已完成删除", "已完成编辑",
    "修改完成", "创建完成", "删除完成", "编辑完成",
    "我已经更新", "我已经创建", "我已经修改", "我已经删除",
    "已经写入文件", "已经保存到文件", "文件已更新", "文件已创建",
    "文件已修改", "文件已保存", "文件已写入",
)

# Keywords signalling a side-effect action completion claim (restart /
# start / stop / install / pull / kill). When these appear but no verify
# command confirmed the post-condition, the verifier footer is appended.
# Separate from _FILE_COMPLETION_CLAIM_PATTERNS because file mutations
# are tracked via mutating tools (write_file/edit_file), while action
# effects are tracked via the side_effect_pending flag.
_ACTION_COMPLETION_CLAIM_PATTERNS: tuple[str, ...] = (
    # English
    "restarted", "restart completed", "restart succeeded",
    "started the service", "started the gateway",
    "stopped the service",
    "service is running", "service is up", "service is active",
    "gateway is running",
    "installed", "installation complete", "installation succeeded",
    "pulled the latest", "pulled latest", "git pull complete",
    "killed the process", "process killed",
    "launched", "launched successfully",
    # Chinese
    "已重启", "已启动", "已停止", "已安装", "已拉取", "已更新到最新",
    "已更新代码", "已杀掉进程", "已结束进程",
    "重启完成", "启动完成", "停止完成", "安装完成",
    "拉取完成", "更新完成",
    "服务已启动", "服务已重启", "服务已停止",
    "gateway 已启动",
    "飞书已连接", "已连接飞书", "websocket 已连接",
)

# Shell command classification: distinguish actions that produce side
# effects (restart/install/pull/kill) from verifies that read state
# (status/ps/curl/logs). Conflating them causes the "false completion"
# failure mode where 'systemctl restart X' is treated as verification
# when it is actually the thing needing verification.
#
# (first_token, second_token) pairs. Empty second token ("") matches any
# second token. Matching is on the first segment of a chained command
# (before && || ; |) and after stripping env prefixes (VAR=val, sudo).
# Conservative: unrecognized commands default to "neutral".
_SHELL_ACTION_PREFIXES: frozenset[tuple[str, str]] = frozenset({
    ("systemctl", "restart"), ("systemctl", "start"), ("systemctl", "stop"),
    ("systemctl", "reload"), ("systemctl", "try-restart"),
    ("service", "restart"), ("service", "start"), ("service", "stop"),
    ("service", "reload"),
    ("launchctl", "kickstart"), ("launchctl", "load"), ("launchctl", "unload"),
    ("pip", "install"), ("pip3", "install"),
    ("npm", "install"), ("npm", "i"), ("npm", "add"),
    ("pnpm", "add"), ("pnpm", "install"), ("pnpm", "i"),
    ("yarn", "add"), ("yarn", "install"),
    ("bun", "add"), ("bun", "install"),
    ("brew", "install"), ("brew", "remove"), ("brew", "upgrade"),
    ("apt", "install"), ("apt-get", "install"),
    ("apt", "remove"), ("apt-get", "remove"),
    ("yum", "install"), ("dnf", "install"),
    ("git", "pull"), ("git", "push"), ("git", "checkout"),
    ("git", "merge"), ("git", "reset"), ("git", "rebase"),
    ("docker", "restart"), ("docker", "start"), ("docker", "stop"),
    ("docker", "rm"), ("docker", "kill"),
    ("pkill", ""), ("killall", ""),
    ("rm", ""), ("mv", ""), ("chmod", ""), ("chown", ""),
})

_SHELL_VERIFY_PREFIXES: frozenset[tuple[str, str]] = frozenset({
    ("systemctl", "is-active"), ("systemctl", "status"), ("systemctl", "is-enabled"),
    ("service", "status"),
    ("launchctl", "list"), ("launchctl", "print"),
    ("ps", ""), ("pgrep", ""), ("pidof", ""),
    ("ss", ""), ("netstat", ""), ("lsof", ""),
    ("curl", ""), ("wget", ""),
    ("journalctl", ""),
    ("docker", "ps"), ("docker", "logs"), ("docker", "inspect"), ("docker", "stats"),
    ("git", "log"), ("git", "status"), ("git", "diff"), ("git", "show"),
    ("pip", "show"), ("pip3", "show"),
    ("npm", "list"), ("npm", "ls"), ("npm", "info"),
    ("pnpm", "list"), ("pnpm", "ls"),
    ("cat", ""), ("head", ""), ("tail", ""), ("less", ""),
    ("ls", ""), ("find", ""), ("tree", ""),
    ("grep", ""), ("rg", ""),
    ("test", ""),
    ("pytest", ""), ("tox", ""), ("nox", ""),
    ("ruff", ""), ("flake8", ""), ("mypy", ""), ("pyright", ""),
    ("eslint", ""), ("tsc", ""),
    ("make", "test"), ("make", "check"), ("make", "verify"), ("make", "lint"),
    ("cargo", "test"), ("cargo", "check"), ("cargo", "clippy"),
    ("go", "test"), ("go", "vet"),
})

# Verify tools that may appear as a subcommand (e.g. `python -m pytest`,
# `python -m mypy`). When any of these appears as a token, the command is
# classified as verify. This is conservative — false-verify (marking a
# neutral command as verify) only sets verification_done=True, which is
# safe; false-action would be the dangerous case, and this set does not
# affect action classification.
_KNOWN_VERIFY_TOOLS: frozenset[str] = frozenset({
    "pytest", "tox", "nox", "unittest",
    "ruff", "flake8", "mypy", "pyright", "pylint", "isort", "black",
    "eslint", "tsc", "prettier",
    "jest", "vitest", "mocha", "karma",
})


def _classify_shell_command(command: str) -> str:
    """Classify a shell command as 'action', 'verify', or 'neutral'.

    Used by ``_record_verification_call`` to avoid treating action commands
    (restart/install/pull/kill) as verification. Treating an action as
    verification is the 'false completion' failure mode: the model runs
    ``systemctl restart X`` and the loop marks ``verification_done=True``,
    so the verify-on-stop nudge never fires even though nobody confirmed
    the service actually came up.

    Conservative: unrecognized commands return 'neutral' (neither flag is
    set in the caller's neutral branch, which preserves the existing
    ``verification_done = True`` behavior to avoid regressing the
    file-edit verify path — most exec calls in a coding session ARE
    verification like pytest).
    """
    if not command or not isinstance(command, str):
        return "neutral"
    cmd = command.strip()
    # Classify the first segment only (before && || ; |) — a chained
    # command is classified by its leading action.
    for sep in ("&&", "||", ";", "|"):
        if sep in cmd:
            cmd = cmd.split(sep, 1)[0].strip()
    tokens = cmd.split()
    # Strip leading env assignments: VAR=val cmd ...
    while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
        tokens = tokens[1:]
    # Strip command prefixes: sudo / env / time / noglob
    while tokens and tokens[0] in ("sudo", "env", "time", "noglob"):
        tokens = tokens[1:]
        while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
            tokens = tokens[1:]
    if not tokens:
        return "neutral"
    first = tokens[0]
    second = tokens[1] if len(tokens) > 1 else ""

    # `service` 命令格式是 `service <name> <action>`，action 在第三个
    # token（与 systemctl <action> <name> 相反）。特殊处理。
    if first == "service" and len(tokens) >= 3:
        third = tokens[2]
        if third in ("restart", "start", "stop", "reload", "force-reload"):
            return "action"
        if third in ("status",):
            return "verify"

    if (first, second) in _SHELL_ACTION_PREFIXES or (first, "") in _SHELL_ACTION_PREFIXES:
        return "action"
    if (first, second) in _SHELL_VERIFY_PREFIXES or (first, "") in _SHELL_VERIFY_PREFIXES:
        return "verify"

    # 已知 verify 工具作为子命令出现（如 `python -m pytest`、
    # `python -m mypy`）。保守判定：误判 neutral 为 verify 只会设
    # verification_done=True，是安全的；不会误判为 action。
    if any(t in _KNOWN_VERIFY_TOOLS for t in tokens):
        return "verify"

    return "neutral"


# Todo 主动 reinjection 间隔（iteration 数）。
# 长任务若不 compact，todo 长期不在上下文里；每 N 轮主动注入一次活跃 todo。
_TODO_REINJECT_INTERVAL = 8


# ---------------------------------------------------------------------------
# Provider-specific usage normalisers
# ---------------------------------------------------------------------------

class _OpenAICompatNormaliser(UsageNormaliser):
    """DeepSeek / OpenAI / CodeWhale: ``prompt_cache_hit/miss_tokens``."""

    cache_read_key = "prompt_cache_hit_tokens"
    cache_miss_key = "prompt_cache_miss_tokens"


class _CodexResponsesNormaliser(UsageNormaliser):
    """OpenAI Codex Responses API: ``cached_tokens``."""

    cache_read_key = "cached_tokens"
    cache_miss_key = None  # implicit (input_tokens - cached)


class _AnthropicNormaliser(UsageNormaliser):
    """Anthropic Messages: ``cache_read_input_tokens`` /
    ``cache_creation_input_tokens``."""

    cache_read_key = "cache_read_input_tokens"
    cache_creation_key = "cache_creation_input_tokens"
    cache_miss_key = None


#: Lookup table; ``provider`` here is the provider class name (or
#: a lowercase short-form).  Falls back to :class:`_OpenAICompatNormaliser`
#: since it covers the largest set.
USAGE_NORMALISERS: dict[str, UsageNormaliser] = {
    "openai_compat": _OpenAICompatNormaliser(),
    "deepseek": _OpenAICompatNormaliser(),
    "openai": _OpenAICompatNormaliser(),
    "azure_openai": _OpenAICompatNormaliser(),
    "openai_codex": _CodexResponsesNormaliser(),
    "codex": _CodexResponsesNormaliser(),
    "anthropic": _AnthropicNormaliser(),
    "claude": _AnthropicNormaliser(),
}


def normalise_usage(
    provider_name: str | None,
    usage: dict[str, Any] | None,
) -> CanonicalUsage:
    """Pick the right normaliser for a provider name and apply it."""
    key = (provider_name or "").lower()
    normaliser = USAGE_NORMALISERS.get(key) or _OpenAICompatNormaliser()
    return normaliser.normalise(usage)


@dataclass
class LoopState:
    messages: list[dict]
    initial_count: int
    iteration: int = 0
    final_content: str | None = None
    tools_used: list[str] = field(default_factory=list)
    budget_exceeded: bool = False
    new_msg_start: int = 0
    persisted_upto: int = 0
    stream_finalized: bool = False
    current_tokens: int = 0
    last_response: Any = None
    last_attempts: list = field(default_factory=list)
    last_compact_action: CompactAction = CompactAction.NONE
    steer_continues: int = 0
    # Cache telemetry (populated by _phase_call_llm).
    last_cache_event: CacheEvent | None = None
    # 结构化退出原因：在每个 return IterationResult 处赋值，由 _finalize_loop
    # 写入日志和 handoff。None 表示尚未到退出点（仍在迭代中）。
    exit_reason: TurnExitReason | None = None
    # 本轮（loop 生命周期内）成功的文件变更记录，用于 file-mutation verifier。
    # 每条记录: {"tool": "write_file", "path": "...", "ok": True}
    # 在 _phase_final_response 中，如果 LLM 声称完成了文件操作但此列表为空，
    # 则追加 verifier footer 揭示真相，防止 LLM 谎报完成。
    file_mutations: list = field(default_factory=list)
    # Verify-on-stop nudge 已注入次数（per-loop，不持久化）。
    # 当本轮有文件变更但 LLM 想直接结束（无验证工具调用）时注入 nudge，
    # 让 LLM 先跑测试/lint/type-check。超过 _MAX_VERIFY_NUDGES 后放行，
    # 避免无限验证循环。
    verify_nudges: int = 0
    # 本轮是否已调用过验证类工具（exec/shell/code_execution）。
    # 用于 verify-on-stop 判定：如果已验证过，则不再注入 nudge。
    verification_done: bool = False
    # 本轮是否执行了副作用动作命令（restart/install/pull/kill 等）
    # 但尚未运行验证命令确认其效果。与 verification_done 互补：
    # action 命令置 True，verify 命令清除它。用于 verify-on-stop
    # 覆盖"跑了动作就宣告完成"的假完成模式——systemctl restart 不是
    # 验证，是需要被验证的动作。
    side_effect_pending: bool = False
    # 上次主动 reinject todo 的 iteration 编号（per-loop，不持久化）。
    # compact 内部已 reinject todo 时由 _phase_compact 同步更新为本轮 iteration，
    # 否则由 _phase_maybe_reinject_todo 在间隔 _TODO_REINJECT_INTERVAL 轮后
    # 主动注入并更新。初始 -1 让首轮若未触发 compact 也能尽快注入。
    last_todo_reinject_iter: int = -1


@dataclass
class IterationResult:
    should_break: bool = False
    should_continue: bool = False
    # 结构化退出原因，由调用方写入 LoopState.exit_reason。
    exit_reason: TurnExitReason | None = None


class IterationRunner:
    """Decomposes the agent loop iteration into distinct phases.

    Usage::

        runner = IterationRunner(loop, channel, chat_id, message_id,
                                 on_progress, on_stream, on_stream_end)
        result = await runner.run(initial_messages)
    """

    def __init__(
        self,
        loop: AgentLoop,
        channel: str,
        chat_id: str,
        message_id: str | None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        session_key: str | None = None,
        session: Session | None = None,
        on_tool_start: Callable[[str, str, str | None], Awaitable[None]] | None = None,
        on_tool_complete: Callable[[str, str, str | None, str | None], Awaitable[None]] | None = None,
        permission_mode_override: PermissionMode | None = None,
    ) -> None:
        self.loop = loop
        self.channel = channel
        self.chat_id = chat_id
        self.message_id = message_id
        self.on_progress = on_progress
        self.on_stream = on_stream
        self.on_stream_end = on_stream_end
        self.session_key = session_key or make_session_key(channel, chat_id) or ""
        self.session = session
        self.on_tool_start = on_tool_start
        self.on_tool_complete = on_tool_complete
        self._stream_filter = StreamFilter(on_stream)
        self._bootstrap_report: Any | None = None
        self.permission_mode_override = permission_mode_override
        # Single control-plane decision engine (config-driven).
        self._tool_guardrail = self._build_tool_guardrail()
        # Skill-guardrail hard-constraint block map: tc.id -> violation message.
        # Populated during the pre-execution check_all pass, enforced in
        # ``_execute_one_tool_call`` so error/critical violations never run.
        self._guardrail_blocked: dict[str, str] = {}
        policy = getattr(loop, "cache_mutation_policy", None)
        if policy is None:
            raise RuntimeError("AgentLoop.cache_mutation_policy is required")
        self._cache_policy: CacheMutationPolicy = policy
        # Outcome gate + multi-axis runtime budget (config-driven).
        from markbot.agent.budget_axes import RuntimeBudget, RuntimeBudgetConfig
        from markbot.agent.outcome import OutcomeGate, OutcomeGateConfig

        tools_cfg = getattr(getattr(loop, "config", None), "tools", None)
        self._outcome_gate = OutcomeGate(
            OutcomeGateConfig.from_settings(
                getattr(tools_cfg, "outcome_gate", None) if tools_cfg else None
            )
        )
        self._runtime_budget = RuntimeBudget(
            RuntimeBudgetConfig.from_settings(
                getattr(tools_cfg, "runtime_budget", None) if tools_cfg else None
            )
        )

    def _build_tool_guardrail(self) -> ToolCallGuardrail:
        """Build ToolCallGuardrail from ToolsConfig.guardrails."""
        tools_cfg = getattr(getattr(self.loop, "config", None), "tools", None)
        g = getattr(tools_cfg, "guardrails", None) if tools_cfg is not None else None
        return ToolCallGuardrail(config=GuardrailConfig.from_settings(g))

    def _durable_turn_enabled(self) -> bool:
        return self.session is not None

    def _flush_messages(self, state: LoopState, start: int | None = None) -> None:
        """Append newly produced turn messages to durable session history."""
        if self.session is None:
            return
        start_idx = state.persisted_upto if start is None else start
        start_idx = max(0, min(start_idx, len(state.messages)))
        if start_idx >= len(state.messages):
            state.persisted_upto = max(state.persisted_upto, len(state.messages))
            return
        try:
            saved = self.loop.tool_executor.save_messages(
                self.session, state.messages[start_idx:]
            )
            self.loop.sessions.save(self.session)
            state.persisted_upto = len(state.messages)
            logger.debug(
                "Incremental turn persistence flushed {} message(s), cursor={}",
                saved,
                state.persisted_upto,
            )
        except Exception as exc:
            logger.warning("Incremental turn persistence failed: {}", exc)

    def _flush_current_inbound(self, state: LoopState) -> None:
        """Persist the inbound message before model/tool side effects begin."""
        if self.session is None or state.persisted_upto:
            return
        current_idx = state.new_msg_start - 1
        if 0 <= current_idx < len(state.messages):
            self._flush_messages(state, current_idx)

    @staticmethod
    def _account_insert(state: LoopState, index: int, count: int = 1) -> None:
        """Keep persistence and save-boundary cursors valid after insertion."""
        if index <= state.new_msg_start:
            state.new_msg_start += count
        if index <= state.persisted_upto:
            state.persisted_upto += count

    def _current_permission_context(self) -> tuple[PermissionMode, ToolPermissionContext]:
        """Return the active permission mode and tool context for this turn.

        Priority:
        1. ``permission_mode_override`` — set by unattended callers (cron /
           autopilot / heartbeat) via ``process_direct`` so they don't depend
           on the global app_state mode being set by a user.
        2. Global ``app_state.permission_mode`` — the single place slash
           commands / UI code change the active mode for interactive turns.
        3. ``PermissionMode.DEFAULT`` fallback.
        """
        if self.permission_mode_override is not None:
            return self.permission_mode_override, ToolPermissionContext(
                mode=self.permission_mode_override
            )
        provider = getattr(getattr(self.loop, "ctx", None), "app_state", None)
        try:
            state = provider.get() if provider is not None else None
            mode = getattr(state, "permission_mode", None)
            tool_ctx = getattr(state, "tool_permission_context", None)
            if isinstance(mode, PermissionMode) and isinstance(tool_ctx, ToolPermissionContext):
                return mode, tool_ctx
        except Exception as exc:
            logger.debug("Failed to read app permission state: {}", exc)
        return PermissionMode.DEFAULT, ToolPermissionContext(mode=PermissionMode.DEFAULT)

    def _is_non_interactive(self) -> bool:
        """Unattended callers pass permission_mode_override and should not block on ask."""
        return self.permission_mode_override is not None

    def _is_read_only_call(self, name: str, params: Any) -> bool:
        """Conservatively classify a tool call as read-only for batching.

        Defaults to serial (False) for unknown tools or any tool that reports
        side effects, so destructive/interactive/external tools never race.
        """
        tool = self.loop.tools.get(name)
        if tool is None:
            return False
        try:
            return bool(tool.is_read_only(params or {}))
        except Exception:
            return False

    def _plan_tool_batches(
        self, tool_calls: list[Any]
    ) -> list[list[int]]:
        """Group tool-call indices into sequential execution batches.

        Consecutive read-only calls are collapsed into one concurrent batch;
        every other call is its own serial batch. Byte order is preserved, so
        appended results stay in the original tool-call order.
        """
        batches: list[list[int]] = []
        for i, tc in enumerate(tool_calls):
            readonly = self._is_read_only_call(tc.name, tc.arguments)
            if readonly and batches:
                last = batches[-1]
                if len(last) >= 1 and self._is_read_only_call(
                    tool_calls[last[-1]].name, tool_calls[last[-1]].arguments
                ):
                    last.append(i)
                    continue
            batches.append([i])
        return batches

    def _check_skill_guardrail(self, tc: Any) -> None:
        """Run skill-guardrail checks for *tc* and populate the block map.

        Activates guarding if *tc* targets a skill tool, then runs
        ``check_all`` and records error/critical violations in
        ``self._guardrail_blocked`` so ``_execute_one_tool_call`` can
        short-circuit the call with a ``PreblockedResult``.

        Called from both the normal tool phase and the invalid-tool
        recovery path so guardrail coverage is identical in both.
        """
        # Resolve provider-echoed (sanitised) name back to in-process name
        # so downstream detection (skill guardrail, etc.) still works.
        lookup_name = self.loop.tools.resolve_sanitised_name(tc.name)
        if "." in lookup_name:
            skill_name = lookup_name.split(".", 1)[0]
            skill_def = self.loop.skill_registry.get(skill_name)
            if skill_def and skill_name not in self.loop.guardrail_manager.active_skills:
                self.loop.guardrail_manager.start_guarding(skill_def)
                logger.info("Guardrail activated for skill: {}", skill_name)

        guardrail_results = self.loop.guardrail_manager.check_all(tc.name, tc.arguments)
        for gr in guardrail_results:
            for v in gr.violations:
                logger.warning(
                    "Guardrail violation [{}] {} (tool: {}, skill: {})",
                    v.severity.upper(),
                    v.message,
                    tc.name,
                    getattr(v, 'tool_name', 'unknown'),
                )
                # Enforce hard constraint: error/critical violations block
                # the tool call before execution. The PreblockedResult
                # returned by _execute_one_tool_call flows through the
                # existing observe() skip path (is_preblocked_result) so
                # the failure window is not inflated by re-observation.
                if v.severity in ("error", "critical") and tc.id not in self._guardrail_blocked:
                    self._guardrail_blocked[tc.id] = v.message

    async def _execute_one_tool_call(self, tc: Any, ctx: ToolContext) -> Any:
        """Execute a single tool call, or return a pre-block denial without running it.

        Pre-block is the single enforcement point for two layers:
          * ``ToolCallGuardrail.blocked_tools`` / ``blocked_signatures``
            (loop-level failure-streak / no-progress defense)
          * ``SkillGuardrail`` error/critical violations (skill hard
            constraints such as "do not modify SOUL.md during a skill")
        """
        if self._tool_guardrail.is_call_blocked(tc.name, tc.arguments):
            msg = self._tool_guardrail.block_message(tc.name, tc.arguments)
            logger.warning(
                "Guardrail pre-blocked tool={} args={}",
                tc.name,
                str(getattr(tc, "arguments", ""))[:120],
            )
            return PreblockedResult(msg)
        skill_block_msg = self._guardrail_blocked.get(tc.id)
        if skill_block_msg is not None:
            logger.warning(
                "Skill guardrail blocked tool={} (violation: {})",
                tc.name,
                skill_block_msg[:120],
            )
            return PreblockedResult(
                f"Blocked by skill guardrail: {skill_block_msg}"
            )
        return await self.loop.tools.execute(tc.name, tc.arguments, context=ctx)

    async def _execute_tool_calls_safely(
        self, tool_calls: list[Any], ctx: ToolContext
    ) -> list[Any]:
        """Execute tool calls in safe batches, preserving call order.

        Each call is pre-checked against the guardrail block list so
        blocked tools/signatures never hit the real executor.
        """
        out: list[Any] = [None] * len(tool_calls)
        for batch in self._plan_tool_batches(tool_calls):
            if len(batch) == 1:
                idx = batch[0]
                out[idx] = await self._execute_one_tool_call(tool_calls[idx], ctx)
            else:
                gathered = await asyncio.gather(
                    *(self._execute_one_tool_call(tool_calls[i], ctx) for i in batch),
                    return_exceptions=True,
                )
                for i, res in zip(batch, gathered):
                    out[i] = res
        return out

    async def run(
        self,
        initial_messages: list[dict],
    ) -> tuple[str | None, list[str], list[dict], int]:
        state = LoopState(
            messages=initial_messages,
            initial_count=len(initial_messages),
            new_msg_start=len(initial_messages),
            persisted_upto=0,
        )

        self._flush_current_inbound(state)

        # Load cross-loop guardrail state (reflection / forced-stop / blacklist).
        # Reset only on a brand-new user request via loop.reset_failure_state().
        if self.session_key:
            try:
                persisted = self.loop.get_failure_state(self.session_key)
                self._tool_guardrail.load_persisted(persisted)
                gs = self._tool_guardrail.state
                if gs.reflection_count or gs.failed_methods or gs.forced_stop_count:
                    logger.info(
                        "Resumed guardrail state for session={}: "
                        "reflections={}, failed_methods={}, forced_stops={}",
                        self.session_key,
                        gs.reflection_count,
                        len(gs.failed_methods),
                        gs.forced_stop_count,
                    )
            except Exception as e:
                logger.warning("Failed to load persisted guardrail state: {}", e)

        # Turn-local counters (blocked tools/signatures, streaks) always start
        # clean for this IterationRunner; only cross-loop fields persist.
        self._tool_guardrail.reset_turn_local()
        self._guardrail_blocked.clear()
        self._runtime_budget.reset()

        # Cache-safe turn boundary: apply any deferred mutations, then lock.
        try:
            applied = self._cache_policy.apply_pending()
            if applied:
                logger.info("Applied deferred cache mutations: {}", applied)
            self._cache_policy.begin_turn()
        except Exception as e:
            logger.debug("cache_policy begin_turn failed: {}", e)

        logger.info(
            "Starting agent loop with {} initial messages",
            len(initial_messages),
        )

        while state.iteration < self.loop.max_iterations:
            state.iteration += 1
            logger.info(
                "=== Iteration {}/{} (channel={}, chat_id={}) ===",
                state.iteration,
                self.loop.max_iterations,
                self.channel,
                self.chat_id,
            )
            logger.info("Current message count: {}", len(state.messages))

            # Wall-time axis must be checked before another LLM call so a
            # long-running turn can halt cleanly without waiting for usage.
            axis_hit = self._runtime_budget.evaluate()
            if axis_hit.hit:
                logger.error("Runtime budget axis hit: {}", axis_hit.message)
                state.budget_exceeded = True
                state.exit_reason = TurnExitReason.BUDGET_EXCEEDED
                if state.final_content is None:
                    state.final_content = (
                        f"[Runtime Budget] {axis_hit.message}. "
                        "Stopping turn with residual work open."
                    )
                break

            result = await self._run_one_iteration(state)
            # Propagate exit reason from the iteration result to loop state.
            # The last non-None exit_reason wins; for should_break this is the
            # final exit reason, for should_continue it's telemetry only.
            if result.exit_reason is not None:
                state.exit_reason = result.exit_reason
            if result.should_break:
                break

        # If we exited the loop without an explicit break (i.e. hit the
        # max_iterations boundary), set the reason here.
        if state.exit_reason is None:
            state.exit_reason = TurnExitReason.MAX_ITERATIONS

        await self._finalize_loop(state)

        save_start = len(state.messages) + 1 if self._durable_turn_enabled() else state.new_msg_start
        return state.final_content, state.tools_used, state.messages, save_start

    async def _run_one_iteration(self, state: LoopState) -> IterationResult:
        # Bump the messages revision so the token estimate cache
        # knows to recompute on the next call.
        rev = getattr(self.loop, "_messages_revision", None)
        if rev is not None:
            self.loop._messages_revision = rev + 1
        state.current_tokens = self._estimate_tokens(state)
        logger.debug("Estimated context tokens: {}", state.current_tokens)

        await self._phase_compact(state)
        state.persisted_upto = max(state.new_msg_start, min(state.persisted_upto, len(state.messages)))
        await self._phase_maybe_reinject_todo(state)
        if state.iteration == 1:
            await self._phase_inject_memory_context(state)
            await self._phase_session_bootstrap(state)

        tool_defs = self.loop.tools.get_definitions()
        logger.debug(
            "Available tools: {}",
            [t.get("function", {}).get("name") for t in tool_defs[:5]],
        )

        state.messages = self.loop._strip_orphan_tool_results(state.messages)

        await self._phase_memory_compaction_hook(state)

        self._inject_pending_steer(state)

        llm_result = await self._phase_call_llm(state, tool_defs)
        if llm_result is not None:
            return llm_result

        response, attempts = state.last_response, state.last_attempts

        self._phase_track_response(state, response, attempts)

        budget_result = self._phase_check_budget(state, response)
        if budget_result is not None:
            return budget_result

        if response.has_tool_calls:
            return await self._phase_execute_tools(state, response, tool_defs)
        else:
            return await self._phase_final_response(state, response)

    async def _phase_compact(self, state: LoopState) -> None:
        task_context = self.loop._gather_task_context()
        engine = getattr(self.loop, "context_engine", None)

        if engine is not None and hasattr(engine, "should_compress"):
            # Narrow waist: all compression goes through ContextEngine.
            if not engine.should_compress(
                state.messages,
                state.current_tokens,
                self.loop.context_window_tokens,
            ):
                return
            engine_result = await engine.compress(
                state.messages,
                state.current_tokens,
                self.loop.context_window_tokens,
                task_context=task_context,
            )
            state.messages = engine_result.messages
            action_name = engine_result.action or "none"
            try:
                state.last_compact_action = CompactAction(action_name)
            except Exception:
                state.last_compact_action = (
                    CompactAction.NONE
                    if action_name in ("", "none", None)
                    else CompactAction.AUTO_COMPACT
                )
            tokens_before = engine_result.tokens_before
            tokens_after = engine_result.tokens_after
            messages_before = 0  # engine may not report
            messages_after = len(state.messages)
            summary = engine_result.summary or ""
            compact_changed = engine_result.changed
        else:
            state.messages, compact_result = await self.loop.compactor.maybe_compact(
                state.messages, state.current_tokens, self.loop.context_window_tokens,
                task_context=task_context,
            )
            state.last_compact_action = compact_result.action
            tokens_before = compact_result.tokens_before
            tokens_after = compact_result.tokens_after
            messages_before = compact_result.messages_before
            messages_after = compact_result.messages_after
            summary = compact_result.summary or ""
            compact_changed = compact_result.action != CompactAction.NONE

        if compact_changed:
            action_val = getattr(state.last_compact_action, "value", state.last_compact_action)
            logger.info(
                "Compaction applied: action={}, "
                "messages: {} -> {}, tokens: {} -> {}{}",
                action_val,
                messages_before or "?",
                messages_after,
                tokens_before,
                tokens_after,
                f", summary={summary[:100]}..." if summary else "",
            )
            state.current_tokens = tokens_after
            if len(state.messages) < state.new_msg_start:
                state.new_msg_start = self.loop._recalc_new_msg_start_after_compact(
                    state.messages, state.initial_count, state.new_msg_start,
                )
            # 关键修复：compact 后显式注入「已尝试且失败的方法」黑名单。
            failed_methods = self._tool_guardrail.state.failed_methods
            if failed_methods:
                recent = failed_methods[-15:]
                blacklist_msg = {
                    "role": "system",
                    "content": (
                        "[Failure Blacklist — 已尝试且失败的方法]\n"
                        "以下方法在本次任务中已尝试且失败，**绝对不要重复这些方法或其变体**。"
                        "如果下一步要用方法和下面任意一条相似，请立刻改用完全不同的方法，"
                        "或直接告知用户当前环境无法完成任务并说明缺失的条件。\n\n"
                        + "\n".join(f"- {m}" for m in recent)
                    ),
                }
                insert_idx = 0
                for i, m in enumerate(state.messages):
                    if m.get("role") == "system":
                        insert_idx = i + 1
                    else:
                        break
                state.messages.insert(insert_idx, blacklist_msg)
                self._account_insert(state, insert_idx)
                logger.info(
                    "Injected failure blacklist ({} entries) after compact ({}) at idx {}",
                    len(recent), action_val, insert_idx,
                )
            state.last_todo_reinject_iter = state.iteration

    async def _phase_maybe_reinject_todo(self, state: LoopState) -> None:
        """主动 reinject 活跃 todo 快照，避免长任务中 plan 失踪。

        markbot 原先只在 compact 后 reinject todo（compact.py 内部），
        但长任务若一直没触发 compact，todo 长期不在上下文里，模型可能
        忘记剩余 plan 或重复已完成步骤。每 ``_TODO_REINJECT_INTERVAL``
        轮主动注入一次，作为 system message 插到开头连续 system 消息之后
        （与 failure blacklist 注入位置一致，保证不破坏 system prompt 首位）。

        compact 触发的那一轮由 ``_phase_compact`` 同步更新计数，本方法
        在间隔不足时直接返回，不会重复注入。
        """
        if state.iteration - state.last_todo_reinject_iter < _TODO_REINJECT_INTERVAL:
            return
        todo_tool = self.loop.tools.get("todo")
        if todo_tool is None or not hasattr(todo_tool, "format_for_injection"):
            return
        try:
            snapshot = todo_tool.format_for_injection()
        except Exception as exc:
            logger.debug("Failed to format todo for reinjection: {}", exc)
            return
        if not snapshot:
            # 没有活跃 todo —— 也更新计数，避免每轮都重试 format_for_injection。
            state.last_todo_reinject_iter = state.iteration
            return
        insert_idx = 0
        for i, m in enumerate(state.messages):
            if m.get("role") == "system":
                insert_idx = i + 1
            else:
                break
        state.messages.insert(insert_idx, {
            "role": "system",
            "content": snapshot,
            # Marker so downstream consumers can identify it as a synthetic
            # re-injection (not part of the original system prompt).
            "_todo_reinjection": True,
        })
        self._account_insert(state, insert_idx)
        state.last_todo_reinject_iter = state.iteration
        logger.info(
            "Re-injected active todo snapshot at iteration {} (idx={})",
            state.iteration, insert_idx,
        )

    async def _phase_inject_memory_context(self, state: LoopState) -> None:
        mem_tool = self.loop._memory_search_tool
        memory_manager = getattr(self.loop, "memory_manager", None)

        user_text = ""
        for m in reversed(state.messages):
            if m.get("role") == "user":
                content = m.get("content", "")
                if content is None:
                    content = ""
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            block_text = block.get("text", "")
                            if block_text is None:
                                block_text = ""
                            user_text += block_text
                elif isinstance(content, str):
                    user_text = content
                if user_text:
                    break

        if not user_text:
            return

        sys_idx = next(
            (i for i, m in enumerate(state.messages) if m.get("role") == "system"),
            None,
        )
        if sys_idx is None:
            return

        context_parts: list[str] = []
        seen_snippets: set[str] = set()

        def _remember_snippet(text: str) -> None:
            # Track coarse fingerprints so prefetch / summary / forced search
            # do not inject the same fact three times in one turn.
            for line in text.splitlines():
                s = line.strip().lower()
                if len(s) >= 24:
                    seen_snippets.add(s[:240])

        def _is_duplicate_block(text: str) -> bool:
            lines = [ln.strip().lower() for ln in text.splitlines() if len(ln.strip()) >= 24]
            if not lines:
                return False
            hits = sum(1 for ln in lines if ln[:240] in seen_snippets)
            return hits >= max(1, len(lines) // 2)

        if memory_manager and hasattr(memory_manager, "prefetch"):
            try:
                session_id = self.session_key or ""
                prefetch_ctx = memory_manager.prefetch(user_text, session_id=session_id)
                if prefetch_ctx and prefetch_ctx.strip() and not _is_duplicate_block(prefetch_ctx):
                    context_parts.append(prefetch_ctx)
                    _remember_snippet(prefetch_ctx)
                    logger.info("Memory prefetch context assembled")
            except Exception as e:
                logger.warning("Memory prefetch failed: {}", e)

        if memory_manager and hasattr(memory_manager, "get_memory_context"):
            try:
                # Curated MEMORY.md is bootstrap-only for main sessions.
                # Here we only inject the session compressed summary to
                # avoid double-loading MEMORY.md every turn.
                memory_ctx = memory_manager.get_memory_context(
                    query=user_text,
                    session_key=self.session_key,
                    channel=self.channel,
                    include_curated=False,
                )
                if memory_ctx and memory_ctx.strip() and not _is_duplicate_block(memory_ctx):
                    context_parts.append(memory_ctx)
                    _remember_snippet(memory_ctx)
                    logger.info("Memory context assembled (session summary)")
            except Exception as e:
                logger.warning("Memory context injection failed: {}", e)

        if mem_tool:
            try:
                if user_text:
                    forced_ctx = await mem_tool.get_forced_context(user_text)
                    if forced_ctx and not _is_duplicate_block(forced_ctx):
                        context_parts.append(forced_ctx)
                        _remember_snippet(forced_ctx)
                        logger.info("Forced memory search context assembled")
            except Exception as e:
                logger.warning("Forced memory search failed: {}", e)

        if not context_parts:
            return

        # Inject as a standalone user message after system to preserve
        # system prompt immutability for Anthropic prompt caching.
        context_block = "\n\n".join(context_parts)
        self._inject_internal_context(state, sys_idx, context_block)
        logger.info("Memory context injected as standalone message")

    async def _phase_memory_compaction_hook(self, state: LoopState) -> None:
        if state.current_tokens <= self.loop.context_window_tokens * 0.6:
            return
        _skip_context_compact = state.last_compact_action in (
            CompactAction.AUTO_COMPACT,
            CompactAction.HISTORY_SNIP,
        )
        system_msg = next(
            (m.get("content", "") for m in state.messages if m.get("role") == "system"), ""
        )
        new_summary = await self.loop.compaction_hook(
            messages=state.messages,
            system_prompt=system_msg,
            skip_context_compact=_skip_context_compact,
            channel=self.channel,
            chat_id=self.chat_id,
        )
        if not new_summary:
            return

        # Remove messages marked as compacted by the hook, then inject the
        # summary so the LLM retains the essential context for this turn.
        # System messages and per-turn internal-context messages are always
        # preserved — only history messages that the hook summarised are
        # eligible for removal.
        def _is_compactable(m: dict) -> bool:
            if m.get("role") == "system":
                return False
            content = m.get("content")
            if isinstance(content, str) and content.startswith(_INTERNAL_CONTEXT_TAG):
                return False
            meta = m.get("metadata")
            return isinstance(meta, dict) and meta.get("_markbot_compacted", False)

        # Capture original index boundary before removal so we can tell
        # history messages (index < new_msg_start) from current-turn ones.
        original_new_msg_start = state.new_msg_start

        before_count = len(state.messages)
        # Determine which messages are compacted and whether they were
        # history (loaded from session) or new (produced this turn).
        history_archived = 0
        kept: list[dict] = []
        for i, m in enumerate(state.messages):
            if _is_compactable(m):
                if i < original_new_msg_start:
                    history_archived += 1
            else:
                kept.append(m)
        state.messages = kept
        removed = before_count - len(state.messages)

        if removed > 0:
            # Fix orphaned tool_call/tool_result pairs left by removal.
            state.messages = self.loop._strip_orphan_tool_results(state.messages)

            # Inject the summary as internal context so the LLM still has
            # the essential information from the removed messages.
            sys_idx = next(
                (i for i, m in enumerate(state.messages) if m.get("role") == "system"),
                None,
            )
            if sys_idx is not None:
                summary_block = (
                    "[Context Summary — the following messages were compacted "
                    "to save space. Use this summary, not the original messages.]\n"
                    + new_summary
                )
                self._inject_internal_context(state, sys_idx, summary_block)

            # Recalculate indices and token count for the shrunken context.
            state.new_msg_start = self.loop._recalc_new_msg_start_after_compact(
                state.messages, state.initial_count, state.new_msg_start,
            )
            state.persisted_upto = max(state.new_msg_start, min(state.persisted_upto, len(state.messages)))
            state.current_tokens = self._estimate_tokens(state)

            # Persist the archived count so the hook can skip these
            # messages on the next turn (metadata markers are stripped
            # by get_history during session save/load).
            if history_archived > 0:
                session_key = make_session_key(self.channel, self.chat_id) or ""
                mm = getattr(self.loop, "memory_manager", None)
                if mm is not None:
                    old_count = mm.get_archived_count(session_key=session_key)
                    mm.set_archived_count(
                        old_count + history_archived,
                        session_key=session_key,
                    )
                    # Note: the archived_tail_hash is set by the compaction
                    # hook itself (on messages_to_compact[-1] after marking).
                    # We don't re-set it here — the hook has the authoritative
                    # last-archived message reference, and re-deriving it from
                    # state.messages would either match the same message (no-op)
                    # or, if the hook skipped context compaction, set a stale
                    # value that overrides the hook's own skip logic next turn.

            logger.info(
                "Memory compaction applied to current context: "
                "removed {} messages ({} history), {} remaining, ~{} tokens",
                removed, history_archived, len(state.messages), state.current_tokens,
            )
        else:
            logger.info("Memory compaction summary stored for next turn")

    async def _phase_session_bootstrap(self, state: LoopState) -> None:
        bootstrap = getattr(self.loop, "session_bootstrap", None)
        if bootstrap is None:
            return
        try:
            report = await bootstrap.run(self.session_key)
            self._bootstrap_report = report
            if report.handoff_loaded or report.has_warnings or report.feature_list_loaded or report.init_sh_available:
                context_block = report.to_context_block()
                if context_block:
                    sys_idx = next(
                        (i for i, m in enumerate(state.messages) if m.get("role") == "system"),
                        None,
                    )
                    if sys_idx is not None:
                        # Append to existing internal context message (from memory
                        # injection) or create a new one. This keeps all per-turn
                        # runtime context in a single user message after system,
                        # preserving system prompt immutability for prompt caching.
                        self._inject_internal_context(state, sys_idx, context_block)
                        logger.info(
                            "Session bootstrap context injected "
                            "(handoff={}, warnings={}, features={}, init_sh={})",
                            report.handoff_loaded,
                            len(report.warnings),
                            report.feature_list_loaded,
                            report.init_sh_available,
                        )
        except Exception as e:
            logger.warning("Session bootstrap failed: {}", e)

    def _phase_active_memory_encoding(self, state: LoopState) -> None:
        encoder = getattr(self.loop, "memory_encoder", None)
        if encoder is None:
            return
        try:
            matches = encoder.scan_messages(state.messages)
            if matches:
                encoded = encoder.encode_preferences(matches)
                if encoded > 0:
                    logger.info(
                        "Active memory encoding: {} new preferences",
                        encoded,
                    )
        except Exception as e:
            logger.debug("Active memory encoding failed: {}", e)

    def _inject_internal_context(
        self, state: LoopState, sys_idx: int, context_block: str,
    ) -> None:
        """Append *context_block* into the internal-context user message after system.

        If an internal-context message already exists (from a prior injection this
        turn), the block is appended to it.  Otherwise a new user message is
        inserted at ``sys_idx + 1`` and ``new_msg_start`` is incremented.

        Keeping all per-turn runtime context in a **single** user message:
        * avoids consecutive same-role messages that some providers reject,
        * minimises message-list growth, and
        * preserves the system prompt verbatim for Anthropic prompt caching.
        """
        # Check if an internal-context message already exists right after system.
        if sys_idx + 1 < len(state.messages):
            next_msg = state.messages[sys_idx + 1]
            content = next_msg.get("content")
            if (
                isinstance(content, str)
                and content.startswith(_INTERNAL_CONTEXT_TAG)
            ):
                state.messages[sys_idx + 1]["content"] = (
                    content + "\n\n" + context_block
                )
                return

        # No existing internal-context message — create one.
        runtime_msg = {
            "role": "user",
            "content": (
                _INTERNAL_CONTEXT_TAG
                + " — informational only, do not acknowledge]\n"
                + context_block
            ),
        }
        state.messages.insert(sys_idx + 1, runtime_msg)
        self._account_insert(state, sys_idx + 1)

    def _inject_pending_steer(self, state: LoopState) -> None:
        steer_text = self.loop.drain_steer(self.session_key)
        if not steer_text:
            return
        last_tool_idx = None
        for i in range(len(state.messages) - 1, -1, -1):
            if state.messages[i].get("role") == "tool":
                last_tool_idx = i
                break
        steer_content = (
            "[User mid-task instruction — adjust your approach accordingly]\n"
            + steer_text
        )
        if last_tool_idx is not None:
            existing = state.messages[last_tool_idx].get("content", "")
            if isinstance(existing, str):
                state.messages[last_tool_idx]["content"] = (
                    existing + "\n\n" + steer_content
                )
            elif isinstance(existing, list):
                state.messages[last_tool_idx]["content"].append(
                    {"type": "text", "text": steer_content}
                )
            logger.info(
                "Steer injected into last tool result at message {}",
                last_tool_idx,
            )
        else:
            last_msg = state.messages[-1]
            existing = last_msg.get("content", "")
            if isinstance(existing, str):
                last_msg["content"] = existing + "\n\n" + steer_content
            elif isinstance(existing, list):
                last_msg["content"].append(
                    {"type": "text", "text": steer_content}
                )
            logger.info(
                "Steer appended to last message (role={})",
                last_msg.get("role"),
            )

    def _is_tool_result_failure(self, result: Any) -> bool:
        """Delegate to the single failure classifier in tool_guardrails."""
        return is_failure_result(result)

    def _record_tool_result(
        self, state: LoopState, result: Any, tool_name: str = "",
        tool_call: Any = None,
    ) -> None:
        """Feed one tool observation into ToolCallGuardrail and apply hints.

        All counters / blocks / blacklists live on ``self._tool_guardrail``.

        Pre-blocked denials are not re-observed — they would otherwise
        inflate the failure window without new information.
        """
        if not tool_name:
            return
        if is_preblocked_result(result):
            return
        args = getattr(tool_call, "arguments", None) if tool_call is not None else None
        decision = self._tool_guardrail.observe(tool_name, args, result)
        self._persist_failure_state()
        if decision.action is GuardrailAction.WARN and decision.message:
            self._append_hint_to_last_tool_result(
                state, f"\n\n[System Warning — guardrail]\n{decision.message}"
            )
        elif decision.action is GuardrailAction.BLOCK and decision.message:
            self._append_hint_to_last_tool_result(
                state, f"\n\n[System Warning — guardrail BLOCK]\n{decision.message}"
            )
            logger.warning(
                "Guardrail BLOCK tool={} reason={} count={}",
                tool_name, decision.reason, decision.count,
            )

    @staticmethod
    def _append_hint_to_last_tool_result(state: LoopState, hint: str) -> None:
        """Append hint text to the latest tool result (or as a user message)."""
        last_tool_idx = None
        for i in range(len(state.messages) - 1, -1, -1):
            if state.messages[i].get("role") == "tool":
                last_tool_idx = i
                break
        if last_tool_idx is not None:
            existing = state.messages[last_tool_idx].get("content", "")
            if isinstance(existing, str):
                state.messages[last_tool_idx]["content"] = existing + hint
            elif isinstance(existing, list):
                state.messages[last_tool_idx]["content"].append(
                    {"type": "text", "text": hint}
                )
        else:
            state.messages.append({"role": "user", "content": hint})

    def _record_file_mutation(
        self, state: LoopState, tool_call: Any, result: Any
    ) -> None:
        """Record a successful file-mutation tool call for verifier checks.

        Only tools in :data:`MUTATING_TOOL_NAMES` are tracked, and only when
        the result is not an error. The recorded path is extracted from the
        tool call's ``path`` argument. This list is consumed by
        :meth:`_maybe_inject_mutation_verifier_footer` to detect the case
        where the LLM claims file completion in its final response but no
        mutating tool actually succeeded.
        """
        lookup_name = self.loop.tools.resolve_sanitised_name(tool_call.name)
        if lookup_name not in MUTATING_TOOL_NAMES:
            return
        # Skip error results — only successful mutations count. Mirror
        # _is_tool_result_failure so JSON-error results (e.g. write_file
        # returning ``{"error": "permission denied"}``) don't get recorded
        # as successful mutations and suppress the verify-on-stop nudge.
        if self._is_tool_result_failure(result):
            return
        path = ""
        if isinstance(tool_call.arguments, dict):
            path = str(tool_call.arguments.get("path") or "")
        state.file_mutations.append({
            "tool": lookup_name,
            "path": path,
            "ok": True,
        })

    def _record_verification_call(
        self, state: LoopState, tool_call: Any, result: Any
    ) -> None:
        """Track shell/exec commands for the verify-on-stop nudge.

        Distinguishes action commands from verify commands:
        - action (restart/install/pull/kill): set ``side_effect_pending``,
          do NOT set ``verification_done`` — the action itself is what
          needs verifying, not a verification of something else.
        - verify (status/ps/curl/logs/tests): set ``verification_done``
          and clear ``side_effect_pending`` — the model checked the
          post-condition.
        - neutral (unrecognized): set ``verification_done`` (so the file-
          edit nudge path treats it as verification — most exec calls in
          a coding session ARE verification like pytest) but do NOT clear
          ``side_effect_pending``. ``_should_inject_verify_nudge`` checks
          ``side_effect_pending`` BEFORE the ``verification_done`` short-
          circuit, so a neutral command (echo/python/node/...) run after
          an action does NOT satisfy the action-verify gate — only a true
          verify command does. This preserves the file-edit path's lenient
          behavior while keeping the action path strict.

        The action/verify split prevents the 'false completion' failure
        mode where ``systemctl restart X`` is treated as verification,
        letting the model declare done without confirming the service
        actually came up.
        """
        lookup_name = self.loop.tools.resolve_sanitised_name(tool_call.name)
        if lookup_name not in VERIFICATION_TOOL_NAMES:
            return
        # code_execution is in-process Python (ad-hoc checks, not ops
        # actions) — always counts as verification.
        if lookup_name == "code_execution":
            state.verification_done = True
            state.side_effect_pending = False
            return
        # exec/shell: classify the actual command.
        command = ""
        if isinstance(tool_call.arguments, dict):
            command = str(tool_call.arguments.get("command") or "")
        kind = _classify_shell_command(command)
        if kind == "action":
            state.side_effect_pending = True
            # Do NOT set verification_done — the action needs a follow-up
            # verify command (status/is-active/ps/logs) to confirm it.
        elif kind == "verify":
            state.verification_done = True
            state.side_effect_pending = False
        else:
            # Neutral (unrecognised command): set verification_done so the
            # file-edit nudge path treats it as verification (most exec
            # calls in a coding session ARE verification like pytest).
            # Intentionally do NOT clear side_effect_pending — only a true
            # verify command (status/is-active/ps/logs) should clear it.
            # _should_inject_verify_nudge checks side_effect_pending BEFORE
            # the verification_done short-circuit, so a neutral command
            # after an action does NOT silently satisfy the action-verify
            # gate.
            state.verification_done = True

    def _non_doc_mutations(self, state: LoopState) -> list[dict]:
        """File mutations that require verification (exclude docs)."""
        out: list[dict] = []
        for mut in state.file_mutations:
            path = mut.get("path", "")
            ext = ""
            if "." in path:
                ext = "." + path.rsplit(".", 1)[-1].lower()
            if ext not in _NO_VERIFY_EXTENSIONS:
                out.append(mut)
        return out

    def _outcome_decision(self, state: LoopState):
        """Evaluate OutcomeGate for the current turn surface / mutations."""
        non_doc = self._non_doc_mutations(state)
        return self._outcome_gate.evaluate(
            surface=self.channel,
            file_mutations=non_doc,
            verification_done=state.verification_done,
            side_effect_pending=state.side_effect_pending,
            nudge_count=state.verify_nudges,
            claims_completion=True,
        )

    def _should_inject_verify_nudge(self, state: LoopState) -> bool:
        """Decide whether to inject a verify-on-stop nudge.

        Delegates the final decision to :class:`OutcomeGate` (config-driven
        surfaces / max_nudges / verification requirements) while preserving
        the non-doc file filter and side-effect-pending semantics.
        """
        from markbot.agent.outcome import OutcomeAction

        return self._outcome_decision(state).action is OutcomeAction.NUDGE

    def _outcome_footer_message(self, state: LoopState) -> str:
        """Return OutcomeGate footer text when verification is still missing."""
        from markbot.agent.outcome import OutcomeAction

        decision = self._outcome_decision(state)
        if decision.action is OutcomeAction.FOOTER and decision.message:
            return decision.message.strip()
        return ""

    def _build_verify_nudge(self, state: LoopState) -> str:
        """Build the verify-on-stop nudge text.

        Adapts to what the model did this turn:
        - file edits: suggest the project's verify command (tests/lint).
        - side-effect action: suggest a post-condition check (status/ps).
        """
        state.verify_nudges += 1
        attempt = state.verify_nudges
        # Build a concise list of edited non-doc paths (cap at 5).
        edited: list[str] = []
        for mut in state.file_mutations:
            path = mut.get("path", "")
            if not path:
                continue
            ext = ""
            if "." in path:
                ext = "." + path.rsplit(".", 1)[-1].lower()
            if ext in _NO_VERIFY_EXTENSIONS:
                continue
            edited.append(path)
            if len(edited) >= 5:
                break

        # Side-effect action path: nudge for post-condition verification.
        if state.side_effect_pending and not edited:
            return (
                f"[System — verification required before completion, attempt {attempt}/{_MAX_VERIFY_NUDGES}]\n"
                "You ran a side-effecting command this turn (restart/install/pull/kill "
                "or similar) but have not verified it took effect. Completion requires "
                "BOTH done AND verified.\n\n"
                "Before declaring done, confirm the action succeeded by checking its "
                "post-condition, e.g.:\n"
                "  - After `systemctl restart X`: `systemctl is-active X && journalctl -u X -n 30 --no-pager`\n"
                "  - After `service X restart`: `service X status`\n"
                "  - After `pip install X`: `pip show X`\n"
                "  - After `git pull`: `git log -1 --oneline`\n"
                "  - After `pkill -f X` / `kill <pid>`: `ps aux | grep X` to confirm it stopped\n"
                "  - After `docker restart X`: `docker ps --filter name=X`\n\n"
                "If the check shows the action failed, fix it and re-run; do not declare done.\n"
                "If the action has no meaningful post-condition, briefly state so and proceed.\n\n"
                "（系统提示：本轮执行了副作用命令但未验证结果。完成 = 执行 + 验证生效。"
                "请先确认动作生效再结束。）"
            )

        # File-edit path (existing): nudge for project verify command.
        paths_text = "\n".join(f"  - {p}" for p in edited) if edited else "  (paths unavailable)"
        return (
            f"[System — verification required before completion, attempt {attempt}/{_MAX_VERIFY_NUDGES}]\n"
            "You edited files this turn but have not run any verification command "
            "(tests / linter / type-checker). Completion requires BOTH done AND verified.\n"
            "Files changed this turn:\n"
            f"{paths_text}\n\n"
            "Before declaring done, run the project's verify command, e.g.:\n"
            "  - Python: `pytest` or `python -m pytest`\n"
            "  - Node.js: `npm test` / `npm run lint` / `npx tsc --noEmit`\n"
            "  - Make-based: `make test` / `make check`\n"
            "  - Or read the project's README / pyproject.toml / package.json for the right command.\n"
            "If the verify command fails, fix the issue and re-run; do not declare done.\n"
            "If the project has no verify command, briefly state so and proceed.\n\n"
            "（系统提示：本轮修改了文件但未运行验证命令。完成 = 改完 + 验证通过。"
            "请先跑测试/lint/type-check 再结束。）"
        )

    def _maybe_inject_mutation_verifier_footer(
        self, state: LoopState, final_content: str
    ) -> str | None:
        """Detect completion claims without matching evidence; append footer.

        Returns the footer text to inject, or ``None`` if no mismatch was
        detected. Two checks:

        1. File-completion claim (created/updated/modified) without a
           successful mutating tool call this turn.
        2. Action-completion claim (restarted/installed/pulled) without a
           follow-up verify command confirming the post-condition
           (``side_effect_pending`` still True). ``side_effect_pending`` is
           the authoritative signal: a neutral command (echo/python/node/...)
           sets ``verification_done`` but does NOT clear
           ``side_effect_pending``, so the footer still fires — only a true
           verify command (status/is-active/ps/logs) clears it via
           ``_record_verification_call``.

        Either mismatch appends a verifier footer so the user (and next-
        turn LLM) sees that the completion claim is unverified.

        Known limitation: ``state.file_mutations`` and
        ``side_effect_pending`` are per-turn (reset on each new
        ``LoopState``). If the LLM did work in a *previous* turn and the
        current turn's final response references it, the verifier may
        false-positive. This is acceptable — the footer says "may be
        inaccurate — verify", which is reasonable advice even then.
        """
        if not final_content:
            return None
        content_lower = final_content.lower()

        # Check 1: file-completion claim without file mutation.
        has_file_claim = any(p in content_lower for p in _FILE_COMPLETION_CLAIM_PATTERNS)
        if has_file_claim and not state.file_mutations:
            logger.warning(
                "File-mutation verifier: LLM claims file completion but "
                "no mutating tool succeeded this turn — injecting footer"
            )
            return (
                "[System Verifier — file-mutation check]\n"
                "The assistant's response above claims file work was completed "
                "(created/updated/modified/deleted), but no successful "
                "write_file / edit_file / delete_file tool call was recorded "
                "in this turn. The claim may be inaccurate — verify the actual "
                "file system state before relying on this response.\n"
                "（系统校验：上方回复声称完成了文件操作，但本轮未记录到成功的"
                "文件写入/编辑/删除工具调用，请核查实际文件状态。）"
            )

        # Check 2: action-completion claim without post-condition verification.
        # side_effect_pending is the authoritative signal — see docstring.
        has_action_claim = any(p in content_lower for p in _ACTION_COMPLETION_CLAIM_PATTERNS)
        if has_action_claim and state.side_effect_pending:
            logger.warning(
                "Action-verification verifier: LLM claims action completion "
                "but no verify command confirmed the post-condition — injecting footer"
            )
            return (
                "[System Verifier — action-verification check]\n"
                "The assistant's response above claims a side-effecting action "
                "(restart/start/install/pull/kill) succeeded, but no follow-up "
                "verify command (status/is-active/ps/logs/curl) was run to "
                "confirm the post-condition. The claim may be inaccurate — "
                "verify the actual service/process state before relying on "
                "this response.\n"
                "（系统校验：上方回复声称副作用动作已完成，但本轮未运行验证"
                "命令确认结果，请核查实际服务/进程状态。）"
            )

        return None

    def _persist_failure_state(self) -> None:
        """Write guardrail cross-loop fields back to AgentLoop session store."""
        if not self.session_key:
            return
        try:
            persisted = self.loop.get_failure_state(self.session_key)
            persisted.update(self._tool_guardrail.to_persisted())
        except Exception as e:
            logger.warning("Failed to persist guardrail state: {}", e)

    def _phase_check_failure_loop(self, state: LoopState) -> IterationResult | None:
        """Apply failure-window policy after a tool batch.

        WARN → inject reflection (count toward max_reflections).
        HALT → force-stop the turn with a structured final message.
        """
        decision = self._tool_guardrail.evaluate_failure_window()
        if decision.action is GuardrailAction.ALLOW:
            return None

        gs = self._tool_guardrail.state
        cfg = self._tool_guardrail.config
        failure_count = decision.count
        window_len = len(gs.recent_failures)
        failed_methods = list(gs.failed_methods)

        if decision.action is GuardrailAction.HALT:
            forced = self._tool_guardrail.note_forced_stop()
            self._persist_failure_state()
            logger.warning(
                "Guardrail HALT ({} failures in last {} calls): {}",
                failure_count, window_len, decision.message,
            )
            if self.loop.guardrail_manager:
                for skill_name in list(self.loop.guardrail_manager.active_skills):
                    result = self.loop.guardrail_manager.stop_guarding(skill_name)
                    if result and result.violations:
                        logger.info(
                            "Guardrail stopped for '{}': {} violation(s)",
                            skill_name, len(result.violations),
                        )
            blacklist_text = ""
            if failed_methods:
                recent = failed_methods[-10:]
                blacklist_text = "\n\n**已尝试但失败的方法（不要重复）：**\n"
                blacklist_text += "\n".join(f"- {m}" for m in recent)
            state.final_content = (
                "我检测到任务陷入连续失败循环（最近多次 tool 调用均失败），"
                f"已第 {forced} 次强制停止以避免无谓的 token 消耗。"
                + blacklist_text
                + "\n\n**可能的原因：**\n"
                "- 当前环境缺少完成任务所需的依赖（如 ffmpeg、特定 Python 包等）\n"
                "- 网络限制导致无法安装依赖或抓取外部资源\n"
                "- 任务目标在当前环境下不可行（如 headless 环境缺少 GUI/扩展支持）\n\n"
                "**建议：**\n"
                "1. 检查上方「已尝试但失败的方法」清单，确认阻塞点\n"
                "2. 手动安装缺失依赖或调整环境后重试\n"
                "3. 或调整任务目标，避开当前环境限制\n"
                "4. 如果继续重试，请先说明你打算用什么**不同**的方法"
            )
            state.messages = self.loop.context.add_assistant_message(
                state.messages,
                state.final_content,
            )
            return IterationResult(
                should_break=True, exit_reason=TurnExitReason.GUARDRAIL_HALT
            )

        # WARN → reflection
        reflection_n = self._tool_guardrail.note_reflection()
        self._persist_failure_state()
        logger.warning(
            "Guardrail failure-window WARN ({} failures in last {} calls), "
            "injecting reflection #{}",
            failure_count, window_len, reflection_n,
        )
        blacklist_section = ""
        if failed_methods:
            recent = failed_methods[-8:]
            blacklist_section = (
                "\n\n**已尝试且失败的方法（绝对不要重复这些）：**\n"
                + "\n".join(f"- {m}" for m in recent)
                + "\n\n如果你接下来要用的方法和上面任意一条相似，请立刻停止并告知用户。"
            )
        reflection_text = (
            "[System Warning — 连续失败检测]\n"
            f"系统检测到你在最近 {window_len} 次 tool 调用中"
            f"有 {failure_count} 次失败。这通常意味着当前策略遇到了"
            "环境或依赖层面的硬性限制，继续用类似方式尝试只会浪费资源。"
            + blacklist_section
            + "\n\n请立即停下来反思：\n"
            "1. **根因分析**：这些失败的共同原因是什么？"
            "（缺少依赖？网络限制？权限问题？headless 环境限制？）\n"
            "2. **可行性判断**：在当前环境下，这个任务是否真的能完成？"
            "如果不能，请直接告知用户缺失了什么，而不是继续尝试。\n"
            "3. **策略调整**：如果可行，换一种**完全不同**的方法；"
            "如果不可行，明确告知用户并停止。\n\n"
            "重要：这是第 "
            f"{reflection_n}/{cfg.max_reflections} 次反思，"
            "达到上限后将强制停止。不要再用相同或类似的方式重试。"
            "如果任务在当前环境不可行，直接回复用户说明原因。"
        )
        self._append_hint_to_last_tool_result(state, "\n\n" + reflection_text)
        return None

    def _phase_memory_sync(self, state: LoopState) -> None:
        memory_manager = getattr(self.loop, "memory_manager", None)
        if memory_manager is None:
            return
        try:
            user_content = ""
            assistant_content = state.final_content or ""
            for m in reversed(state.messages):
                if m.get("role") == "user":
                    content = m.get("content", "")
                    if isinstance(content, str):
                        user_content = content
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                user_content = block.get("text", "")
                                break
                    if user_content:
                        break

            session_id = self.session_key or ""
            if user_content or assistant_content:
                if hasattr(memory_manager, "sync_turn"):
                    memory_manager.sync_turn(
                        user_content=user_content,
                        assistant_content=assistant_content,
                        session_id=session_id,
                    )
                    logger.debug("Memory sync_turn completed")
                if hasattr(memory_manager, "queue_prefetch"):
                    memory_manager.queue_prefetch(
                        query=user_content,
                        session_id=session_id,
                    )
                    logger.debug("Memory queue_prefetch completed")
        except Exception as e:
            logger.debug("Memory sync failed: {}", e)

    async def _phase_call_llm(
        self, state: LoopState, tool_defs: list[dict]
    ) -> IterationResult | None:
        # If a previous model returned content_filter, the stream buffer may be
        # polluted. Discard it before starting a new LLM call.
        if self.on_stream and self.on_stream_end and state.last_attempts:
            had_content_filter = any(
                getattr(a, "error", "") == "content_filter"
                for a in state.last_attempts
            )
            if had_content_filter:
                logger.info(
                    "Discarding polluted stream buffer before new LLM call"
                )
                await self.on_stream_end(discard=True)
                self._stream_filter.reset()
        # ------------------------------------------------------------------
        # Prefix-stability check.  Runs *before* the LLM call so the
        # operator can see the drift on the same frame as the response
        # that triggered it.  See markbot.agent.prefix_cache.
        # ------------------------------------------------------------------
        self._check_prefix_stability(state, tool_defs)
        # ------------------------------------------------------------------
        # LLM response cache: short-circuit deterministic requests.
        # See markbot.agent.llm_response_cache for the rationale.
        # ------------------------------------------------------------------
        cache_hit = self._try_response_cache(state, tool_defs)
        if cache_hit is not None:
            state.last_response = cache_hit
            state.last_attempts = []
            return None
        try:
            if self.on_stream:
                logger.info("Calling LLM with streaming...")
                response, attempts = await self.loop.fallback_manager.chat_stream_with_fallback(
                    messages=state.messages,
                    tools=tool_defs,
                    on_content_delta=self._stream_filter,
                )
            else:
                logger.info("Calling LLM without streaming...")
                response, attempts = await self.loop.fallback_manager.chat_with_fallback(
                    messages=state.messages,
                    tools=tool_defs,
                )
            state.last_response = response
            state.last_attempts = attempts
            # Normalise the response usage and feed it to the cost tracker.
            # The provider name comes from the most recent successful
            # attempt when available, else we fall back to "unknown".
            self._record_canonical_usage(response, attempts)
            # Refresh the cache event's token fields with the *current*
            # response's usage.  ``_check_prefix_stability`` ran before
            # the call and could only see the previous turn's usage, so
            # without this refresh the TUI chip would always lag one
            # turn behind.  ``CacheEvent`` is frozen, so we use
            # ``dataclasses.replace`` to build a new instance.
            self._refresh_cache_event_tokens(state, response, attempts)
            # Write to the LLM response cache if the request was cacheable.
            self._maybe_write_response_cache(state, tool_defs, response, attempts)
            self._emit_model_succeeded(response, attempts)
            return None
        except Exception as llm_exc:
            self._emit_model_failed(llm_exc)
            return await self._handle_llm_error(state, tool_defs, llm_exc)

    def _check_prefix_stability(
        self, state: LoopState, tool_defs: list[dict]
    ) -> None:
        """Run :class:`PrefixStabilityManager` and emit a :class:`CacheEvent`.

        The event payload is logged at INFO level so the operator gets
        a one-line summary of every turn; the bus payload itself is
        kept tiny so it can also be forwarded to TUI / Langfuse
        without redaction overhead.
        """
        pm: PrefixStabilityManager | None = getattr(
            self.loop, "prefix_stability", None
        )
        if pm is None:
            return
        sys_text = system_prompt_text(state.messages)
        try:
            stable, change = pm.check_and_update(sys_text, tool_defs or None)
        except Exception as exc:
            logger.debug("prefix_stability check failed: {}", exc)
            return
        # Compute the cache_read_tokens from the *previous* response
        # (if any) so the TUI chip can render a sensible rate.
        last_usage: dict[str, Any] = {}
        last_resp = state.last_response
        if last_resp is not None:
            usage = getattr(last_resp, "usage", None)
            if isinstance(usage, dict):
                last_usage = usage
        provider_name = ""
        # Try to find the provider name from the latest attempts.
        for a in reversed(state.last_attempts or []):
            provider_cfg = getattr(a, "provider", None)
            if provider_cfg is not None and getattr(provider_cfg, "type", None):
                provider_name = str(provider_cfg.type)
                break
        canonical = normalise_usage(provider_name, last_usage)
        event = CacheEvent(
            description=(
                "stable" if stable
                else (str(change) if change is not None else "drift")
            ),
            system_prompt_changed=bool(change and change.system_changed),
            tools_changed=bool(change and change.tools_changed),
            stability_pct=int(round(pm.stability_ratio * 100)),
            changed=not stable,
            pinned_combined_hash=(
                pm.pinned.short() if pm.pinned else ""
            ),
            cache_read_tokens=canonical.cache_read_tokens,
            cache_miss_tokens=canonical.cache_miss_tokens,
        )
        if not stable:
            logger.warning(
                "[cache] prefix drift detected: {} (stability={}%)",
                event.description,
                event.stability_pct,
            )
        else:
            logger.debug(
                "[cache] prefix stable ({}% over {} turns, last_change={})",
                event.stability_pct,
                pm.check_count,
                event.description if event.changed else "none",
            )
        # Stash the event on the state so the slash command / TUI
        # can surface it without re-running the check.
        state.last_cache_event = event  # type: ignore[attr-defined]

    def _emit(self, event_type: Any, payload: dict[str, Any]) -> None:
        """Fire-and-forget emit on the global EventEmitter.

        Wraps the emitter lookup so a missing emitter or a subscriber
        error never breaks the agent loop.  The event is scheduled on
        the running loop so this stays sync-callable from any phase.
        """
        try:
            from markbot.bus.emitter import get_event_emitter

            emitter = get_event_emitter()
            loop = asyncio.get_running_loop()
            loop.create_task(
                emitter.emit(event_type, payload, session_key=self.session_key)
            )
        except Exception as e:
            logger.debug("emit {} failed: {}", getattr(event_type, "name", event_type), e)

    def _emit_model_succeeded(self, response: Any, attempts: list[Any] | None) -> None:
        """Emit MODEL_SUCCEEDED with a compact payload for observability."""
        try:
            from markbot.bus.events import EventType

            usage = getattr(response, "usage", None) or {}
            model = getattr(response, "model", "") or ""
            if not model and attempts:
                model = getattr(attempts[-1], "model", "") or ""
            self._emit(
                EventType.MODEL_SUCCEEDED,
                {
                    "model": str(model),
                    "usage": usage if isinstance(usage, dict) else {},
                    "tool_calls": len(getattr(response, "tool_calls", []) or []),
                },
            )
        except Exception as e:
            logger.debug("_emit_model_succeeded failed: {}", e)

    def _emit_model_failed(self, error: BaseException) -> None:
        """Emit MODEL_FAILED so monitors can alert on provider outages."""
        try:
            from markbot.bus.events import EventType

            self._emit(
                EventType.MODEL_FAILED,
                {"error": type(error).__name__, "message": str(error)[:300]},
            )
        except Exception as e:
            logger.debug("_emit_model_failed failed: {}", e)

    def _record_canonical_usage(
        self,
        response: Any,
        attempts: list[Any] | None,
    ) -> None:
        """Normalise the response usage and feed it to the cost tracker.

        Falls back to ``update_from_response`` (legacy path) when no
        attempts are recorded, so the cost tracker keeps working for
        provider adapters that haven't been migrated yet.
        """
        if response is None:
            return
        usage = getattr(response, "usage", None)
        if not isinstance(usage, dict):
            return
        provider_name = ""
        if attempts:
            for a in reversed(attempts):
                provider_cfg = getattr(a, "provider", None)
                if provider_cfg is not None and getattr(provider_cfg, "type", None):
                    provider_name = str(provider_cfg.type)
                    break
        canonical = normalise_usage(provider_name, usage)
        # Best-effort: also surface to cost tracker.
        cost_tracker = getattr(self.loop, "cost_tracker", None)
        model = ""
        if attempts:
            for a in reversed(attempts):
                m = getattr(a, "model_ref", None)
                if m:
                    model = str(m)
                    break
        if cost_tracker is not None and hasattr(cost_tracker, "add_canonical_usage"):
            try:
                cost_tracker.add_canonical_usage(canonical, model=model or "unknown")
            except Exception as exc:
                # Don't break the loop on a billing glitch.
                logger.debug("cost_tracker.add_canonical_usage failed: {}", exc)

    def _refresh_cache_event_tokens(
        self,
        state: LoopState,
        response: Any,
        attempts: list[Any] | None,
    ) -> None:
        """Update ``state.last_cache_event`` with the current response's
        cache token counts.

        :meth:`_check_prefix_stability` runs *before* the LLM call and
        can only see the previous turn's usage, so the token fields on
        the event it produced describe the wrong turn.  This method
        re-normalises the *current* response's usage and rebuilds the
        event via :func:`dataclasses.replace` (``CacheEvent`` is
        frozen).  The drift-related fields (description,
        ``system_prompt_changed``, ``stability_pct``, ...) are
        preserved unchanged because they describe the state going
        *into* the call, which is exactly what we want.
        """
        event = getattr(state, "last_cache_event", None)
        if event is None or response is None:
            return
        usage = getattr(response, "usage", None)
        if not isinstance(usage, dict):
            return
        provider_name = ""
        if attempts:
            for a in reversed(attempts):
                provider_cfg = getattr(a, "provider", None)
                if provider_cfg is not None and getattr(provider_cfg, "type", None):
                    provider_name = str(provider_cfg.type)
                    break
        try:
            canonical = normalise_usage(provider_name, usage)
        except Exception as exc:
            logger.debug("refresh cache event tokens failed: {}", exc)
            return
        # Only overwrite when we actually got fresh data; otherwise
        # leave the previous (possibly None) values in place so the
        # chip doesn't flicker between "unknown" and "0".
        if canonical.cache_read_tokens is None and canonical.cache_miss_tokens is None:
            return
        new_event = replace(
            event,
            cache_read_tokens=canonical.cache_read_tokens,
            cache_miss_tokens=canonical.cache_miss_tokens,
        )
        state.last_cache_event = new_event

    async def _handle_llm_error(
        self, state: LoopState, tool_defs: list[dict], llm_exc: Exception
    ) -> IterationResult:
        if not is_prompt_too_long_error(llm_exc):
            logger.error("LLM call failed: {}", llm_exc)
            state.final_content = (
                f"Sorry, I encountered an error calling the AI model: {str(llm_exc)[:200]}"
            )
            return IterationResult(
                should_break=True, exit_reason=TurnExitReason.LLM_ERROR
            )

        logger.warning("Prompt-too-long error, attempting reactive compaction")
        task_context = self.loop._gather_task_context()
        reactive_result = await self.loop.compactor.reactive_compact(
            state.messages, self.loop.context_window_tokens, llm_exc,
            task_context=task_context,
        )
        if reactive_result is None:
            logger.error("LLM call failed (not a PTL error): {}", llm_exc)
            state.final_content = (
                f"Sorry, I encountered an error calling the AI model: {str(llm_exc)[:200]}"
            )
            return IterationResult(
                should_break=True, exit_reason=TurnExitReason.LLM_ERROR
            )

        state.messages, compact_result_ptl = reactive_result
        state.current_tokens = compact_result_ptl.tokens_after
        if len(state.messages) < state.new_msg_start:
            state.new_msg_start = self.loop._recalc_new_msg_start_after_compact(
                state.messages, state.initial_count, state.new_msg_start,
            )
        logger.info(
            "Reactive compaction applied: {} -> {} tokens",
            compact_result_ptl.tokens_before,
            compact_result_ptl.tokens_after,
        )
        try:
            if self.on_stream:
                response, attempts = await self.loop.fallback_manager.chat_stream_with_fallback(
                    messages=state.messages,
                    tools=tool_defs,
                    on_content_delta=self._stream_filter,
                )
            else:
                response, attempts = await self.loop.fallback_manager.chat_with_fallback(
                    messages=state.messages,
                    tools=tool_defs,
                )
            state.last_response = response
            state.last_attempts = attempts
            self._emit_model_succeeded(response, attempts)
            return None
        except Exception as retry_exc:
            self._emit_model_failed(retry_exc)
            logger.error(
                "LLM call failed after reactive compaction: {}", retry_exc
            )
            state.final_content = (
                "Sorry, the conversation context became too large and compaction was unable "
                "to reduce it enough. Please start a new session or simplify your request."
            )
            return IterationResult(
                should_break=True, exit_reason=TurnExitReason.PTL_RECOVERY_FAILED
            )

    # ------------------------------------------------------------------
    # LLM response cache helpers
    # ------------------------------------------------------------------

    def _try_response_cache(
        self, state: LoopState, tool_defs: list[dict]
    ) -> Any | None:
        """Check the LLM response cache for a deterministic hit.

        Returns the cached response on hit, or ``None`` on miss /
        when the request is not cacheable.
        """
        cache = getattr(self.loop, "llm_response_cache", None)
        if cache is None:
            return None
        from markbot.agent.llm_response_cache import (
            request_is_cacheable,
            make_response_cache_key,
            canonicalise_body,
        )
        # Streaming requests are never cacheable.
        if self.on_stream:
            return None
        if not request_is_cacheable(
            stream=False,
            tools=tool_defs or None,
        ):
            return None
        # Build a cache key from the messages body.
        try:
            body = canonicalise_body(state.messages)
            key = make_response_cache_key(
                provider="markbot",
                base_url="",
                path_suffix=None,
                api_key=None,
                body=body,
            )
            entry = cache.get(key)
            if entry is not None:
                logger.info(
                    "[llm_response_cache] hit: key={}.., hits={}",
                    key.hex()[:12],
                    cache.stats["hits"],
                )
                return entry.body
        except Exception as exc:
            logger.debug("[llm_response_cache] lookup failed: {}", exc)
        return None

    def _maybe_write_response_cache(
        self,
        state: LoopState,
        tool_defs: list[dict],
        response: Any,
        attempts: list[Any] | None,
    ) -> None:
        """Write the response to the LLM response cache if cacheable."""
        cache = getattr(self.loop, "llm_response_cache", None)
        if cache is None:
            return
        from markbot.agent.llm_response_cache import (
            request_is_cacheable,
            make_response_cache_key,
            canonicalise_body,
        )
        if self.on_stream:
            return
        if not request_is_cacheable(
            stream=False,
            tools=tool_defs or None,
        ):
            return
        try:
            body = canonicalise_body(state.messages)
            key = make_response_cache_key(
                provider="markbot",
                base_url="",
                path_suffix=None,
                api_key=None,
                body=body,
            )
            cache.put(key, response)
            logger.debug(
                "[llm_response_cache] write: key={}..",
                key.hex()[:12],
            )
        except Exception as exc:
            logger.debug("[llm_response_cache] write failed: {}", exc)

    def _estimate_tokens(self, state: LoopState) -> int:
        """Estimate the current token count, using the estimate cache
        when available."""
        tec = getattr(self.loop, "token_estimate_cache", None)
        rev = getattr(self.loop, "_messages_revision", 0)
        if tec is not None:
            try:
                # Extract the system prompt text so the cache's
                # ``system_fingerprint`` key actually invalidates when
                # the system prompt bytes change.  Without this, the
                # fingerprint would always be 0 and the cache would
                # rely solely on ``messages_revision`` being bumped —
                # fragile if any future code path forgets to bump it.
                sys_text = system_prompt_text(state.messages)
                return tec.lookup_or_compute(
                    messages_revision=rev,
                    system_prompt=sys_text,
                    messages=state.messages,
                )
            except Exception:
                pass
        return token_count_with_estimation(state.messages)

    def _phase_track_response(
        self, state: LoopState, response: Any, attempts: list[Any]
    ) -> None:
        logger.info(
            "LLM response received: finish_reason={}, "
            "has_tool_calls={}, content_length={}",
            response.finish_reason,
            response.has_tool_calls,
            len(response.content or ""),
        )

        _actual_model = self.loop.model
        for _a in reversed(attempts):
            if _a.success and _a.model:
                _actual_model = _a.model.name
                break

        self.loop._interaction_log.log_interaction(
            iteration=state.iteration,
            messages=state.messages,
            tool_defs=self.loop.tools.get_definitions(),
            response=response,
            model=_actual_model,
            channel=self.channel,
            chat_id=self.chat_id,
            tokens_before=state.current_tokens,
        )

        usage = response.usage or {}
        self.loop._last_usage = {
            "prompt_tokens": int(usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0) or 0),
            "completion_tokens": int(usage.get("completion_tokens", 0) or usage.get("output_tokens", 0) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
            "cache_creation_input_tokens": int(
                usage.get("cache_creation_input_tokens", 0) or 0
            ),
            "cache_read_input_tokens": int(usage.get("cache_read_input_tokens", 0) or 0),
        }
        self.loop._last_context_tokens = state.current_tokens

    def _phase_check_budget(
        self, state: LoopState, response: Any
    ) -> IterationResult | None:
        _actual_model = self.loop.model
        for _a in reversed(state.last_attempts):
            if _a.success and _a.model:
                _actual_model = _a.model.name
                break

        # Token axis (in addition to CostTracker USD).
        usage = getattr(response, "usage", None) or {}
        total = int(
            usage.get("total_tokens", 0)
            or (
                int(usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0) or 0)
                + int(
                    usage.get("completion_tokens", 0)
                    or usage.get("output_tokens", 0)
                    or 0
                )
            )
            or 0
        )
        if total:
            self._runtime_budget.add_tokens(total)
        axis_hit = self._runtime_budget.evaluate()
        if axis_hit.hit:
            logger.error("Runtime budget axis hit: {}", axis_hit.message)
            state.budget_exceeded = True
            state.final_content = (
                f"[Runtime Budget] {axis_hit.message}. "
                "Stopping turn with residual work open."
            )
            return IterationResult(
                should_break=True, exit_reason=TurnExitReason.BUDGET_EXCEEDED
            )

        try:
            call_cost = self.loop.cost_tracker.update_from_response(
                response, model=_actual_model
            )
            if call_cost > 0:
                logger.debug("API cost this call: ${:.6f}", call_cost)
            return None
        except BudgetExceededError as exc:
            logger.error("Budget exceeded: {}. Stopping loop early.", exc)
            state.budget_exceeded = True
            return IterationResult(
                should_break=True, exit_reason=TurnExitReason.BUDGET_EXCEEDED
            )

    async def _phase_execute_tools(
        self, state: LoopState, response: Any, tool_defs: list[dict]
    ) -> IterationResult:
        logger.info(
            "LLM requested {} tool call(s): {}",
            len(response.tool_calls),
            [tc.name for tc in response.tool_calls],
        )

        if self.on_stream and self.on_stream_end:
            had_content_filter = any(
                getattr(a, "error", "") == "content_filter"
                for a in (state.last_attempts or [])
            )
            if had_content_filter:
                logger.info(
                    "Discarding polluted stream buffer "
                    "(content_filter in fallback chain)"
                )
                await self.on_stream_end(resuming=True, discard=True)
                self._stream_filter.reset()
            else:
                await self.on_stream_end(resuming=True)
                self._stream_filter.reset()

        if self.on_progress:
            if not self.on_stream:
                thought = strip_think(response.content) or None
                if thought:
                    await self.on_progress(thought)
            tool_hint = self.loop._tool_hint(response.tool_calls)
            tool_hint = strip_think(tool_hint) or None
            await self.on_progress(tool_hint, tool_hint=True)

        tool_call_dicts = [tc.to_openai_tool_call() for tc in response.tool_calls]
        state.messages = self.loop.context.add_assistant_message(
            state.messages,
            response.content,
            tool_call_dicts,
            reasoning_content=response.reasoning_content,
            thinking_blocks=response.thinking_blocks,
        )
        logger.debug(
            "Added assistant message with tool calls, total messages: {}",
            len(state.messages),
        )
        self._flush_messages(state)

        for tc in response.tool_calls:
            state.tools_used.append(tc.name)
            args_str = json.dumps(tc.arguments, ensure_ascii=False)
            safe_args = args_str[:200].replace("{", "{{").replace("}", "}}")
            logger.info("Tool call: {}({})", tc.name, safe_args)

        self.loop._set_tool_context(self.channel, self.chat_id, self.message_id)

        invalid_tool_calls = self._detect_and_repair_hallucinated_tools(response)
        if invalid_tool_calls:
            self._invalid_tool_retries = getattr(self, "_invalid_tool_retries", 0) + 1
            available = ", ".join(sorted(self.loop.tools.tool_names))
            invalid_name = invalid_tool_calls[0]
            invalid_preview = invalid_name[:80] + "..." if len(invalid_name) > 80 else invalid_name
            logger.warning(
                "Unknown tool '{}' — sending error to model for self-correction ({}/3)",
                invalid_preview, self._invalid_tool_retries,
            )

            if self._invalid_tool_retries >= 3:
                logger.error("Max retries (3) for invalid tool calls exceeded. Stopping.")
                self._invalid_tool_retries = 0
                state.final_content = (
                    f"I tried to call a tool that doesn't exist ('{invalid_preview}') "
                    f"too many times. Available tools: {available}"
                )
                return IterationResult(
                    should_break=True,
                    exit_reason=TurnExitReason.INVALID_TOOL_MAX_RETRIES,
                )

            error_msg = (
                f"Tool '{invalid_name}' does not exist. "
                f"Available tools: {available}. "
                f"Please use only the tools listed above."
            )
            permission_mode, permission_context = self._current_permission_context()
            recovery_ctx = ToolContext(
                session_id=self.session_key,
                workspace=str(self.loop.workspace),
                permission_mode=permission_mode,
                tool_permission_context=permission_context,
                is_non_interactive=self._is_non_interactive(),
                channel=self.channel,
                chat_id=self.chat_id,
                message_id=self.message_id,
            )
            for tc in response.tool_calls:
                if not self.loop.tools.has(tc.name):
                    state.messages = await self.loop.context.add_tool_result_async(
                        state.messages, tc.id, tc.name, error_msg
                    )
                else:
                    # Run skill guardrail checks (same as normal tool phase)
                    # so error/critical violations block execution here too.
                    self._check_skill_guardrail(tc)
                    # Same pre-block path as the normal tool phase.
                    result = await self._execute_one_tool_call(tc, recovery_ctx)
                    state.messages = await self.loop.context.add_tool_result_async(
                        state.messages, tc.id, tc.name, result
                    )
                    # Track file mutations in the recovery path too — if the
                    # LLM mixed a valid write_file with hallucinated tools,
                    # the mutation must still be recorded so the verifier
                    # doesn't false-positive on the final response.
                    self._record_file_mutation(state, tc, result)
                    self._record_verification_call(state, tc, result)
                    # Feed recovery-path results into the guardrail engine.
                    self._record_tool_result(state, result, tc.name, tc)
            self._flush_messages(state)
            # Batch-end failure-window check (same as normal tool phase).
            failure_result = self._phase_check_failure_loop(state)
            if failure_result is not None:
                return failure_result
            return IterationResult(
                should_continue=True, exit_reason=TurnExitReason.TOOL_CONTINUE
            )
        else:
            self._invalid_tool_retries = 0

        for tc in response.tool_calls:
            self._check_skill_guardrail(tc)

        logger.info("Executing {} tool calls...", len(response.tool_calls))

        # Build a per-turn progress callback so long-running tools
        # (run_code streaming a pip install, exec running a build) can push
        # incremental stdout lines onto the event bus. The bus-side payload
        # carries the session key so subscribers (CLI, web UI) can render
        # live progress. report_progress is sync per the ToolContext
        # contract; we hop into the loop to emit the async event.
        _progress_session_key = self.session_key
        _progress_loop = asyncio.get_running_loop()
        _active_tool_names = [tc.name for tc in response.tool_calls]

        def _report_progress(text: str, percent: float | None = None) -> None:
            try:
                from markbot.bus.emitter import get_event_emitter
                from markbot.bus.events import EventType

                emitter = get_event_emitter()
                payload = {
                    "text": text,
                    "percent": percent,
                    "tools": _active_tool_names,
                    "channel": self.channel,
                    "chat_id": self.chat_id,
                }
                _progress_loop.create_task(
                    emitter.emit(
                        EventType.TOOL_PROGRESS,
                        payload,
                        session_key=_progress_session_key,
                    )
                )
            except Exception as e:
                logger.debug("report_progress emit failed: {}", e)

        permission_mode, permission_context = self._current_permission_context()
        _tool_ctx = ToolContext(
            session_id=self.session_key,
            workspace=str(self.loop.workspace),
            permission_mode=permission_mode,
            tool_permission_context=permission_context,
            is_non_interactive=self._is_non_interactive(),
            channel=self.channel,
            chat_id=self.chat_id,
            message_id=self.message_id,
            report_progress=_report_progress,
        )

        for tc in response.tool_calls:
            if tc.id in self._guardrail_blocked:
                continue
            if self.on_tool_start:
                context_str = None
                if isinstance(tc.arguments, dict):
                    args_preview = json.dumps(tc.arguments, ensure_ascii=False)[:200]
                    context_str = f"{tc.name}({args_preview})"
                try:
                    await self.on_tool_start(tc.id, tc.name, context_str)
                except Exception as e:
                    logger.debug("on_tool_start callback failed: {}", e)

        # Emit TOOL_CALLED for each tool so monitors can track usage.
        # Skip guardrail-blocked calls — they never actually executed.
        for tc in response.tool_calls:
            if tc.id in self._guardrail_blocked:
                continue
            self._emit(
                EventType.TOOL_CALLED,
                {"tool": tc.name, "call_id": tc.id},
            )

        results = await self._execute_tool_calls_safely(response.tool_calls, _tool_ctx)
        logger.info("Tool execution completed, {} results", len(results))

        for idx, (tool_call, result) in enumerate(zip(response.tool_calls, results)):
            if isinstance(result, BaseException):
                logger.error(
                    "Tool {} failed with exception: {}", tool_call.name, result
                )
                self._emit(
                    EventType.TOOL_FAILED,
                    {"tool": tool_call.name, "call_id": tool_call.id,
                     "error": type(result).__name__},
                )
                result = f"Error: {type(result).__name__}: {result}"
            elif isinstance(result, dict) and result.get("_multimodal"):
                # Multimodal results (e.g. computer_use screenshots) carry a
                # base64 image that dwarfs the inline-char threshold. They must
                # NOT be stringified or offloaded — add_tool_result unwraps the
                # dict into a content array (text + image_url) for the provider.
                # Logging the text_summary keeps base64 out of the log.
                preview = (result.get("text_summary") or "")[:100]
                safe_preview = preview.replace("{", "{{").replace("}", "}}")
                logger.info("Tool {} result (multimodal): {}", tool_call.name, safe_preview)
            else:
                result_str = str(result)
                # Dynamic per-tool budget: when the model calls N tools in one
                # turn, each tool's inline allowance shrinks proportionally so
                # the total injected text stays bounded regardless of fan-out.
                # This prevents a single turn with many verbose tools from
                # forcing a compaction that would lose information.
                _tool_count = max(1, len(response.tool_calls))
                _inline_budget = max(
                    2000, self.loop.compactor.config.tool_output_inline_chars // _tool_count
                )
                if len(result_str) > _inline_budget:
                    inline, artifact_path = offload_tool_output(
                        result_str,
                        tool_call.name,
                        tool_call.id,
                        inline_limit=_inline_budget,
                        preview_chars=self.loop.compactor.config.tool_output_preview_chars,
                    )
                    if artifact_path:
                        logger.info(
                            "Tool {} output offloaded to {} ({} chars)",
                            tool_call.name, artifact_path, len(result_str),
                        )
                        result = inline
                    else:
                        result_preview = (
                            result_str[:100] + "..." if len(result_str) > 100 else result_str
                        )
                        safe_preview = result_preview.replace("{", "{{").replace("}", "}}")
                        logger.info(
                            "Tool {} result: {}", tool_call.name, safe_preview
                        )
                else:
                    result_preview = (
                        result_str[:100] + "..." if len(result_str) > 100 else result_str
                    )
                    safe_preview = result_preview.replace("{", "{{").replace("}", "}}")
                    logger.info(
                        "Tool {} result: {}", tool_call.name, safe_preview
                    )
            state.messages = await self.loop.context.add_tool_result_async(
                state.messages, tool_call.id, tool_call.name, result
            )

            # 记录 tool 结果用于连续失败检测
            self._record_tool_result(state, result, tool_call.name, tool_call)

            # File-mutation tracking: record successful mutating-tool calls so
            # the verifier in _phase_final_response can detect "LLM claims
            # done but nothing landed on disk". We only track tools in
            # MUTATING_TOOL_NAMES (write_file / edit_file / delete_file) and
            # only when the result is not an error. Shell/exec tools are
            # excluded because their file effects are not deterministically
            # parseable from arguments.
            self._record_file_mutation(state, tool_call, result)
            # Track verification tool calls (exec/shell/code_execution) for
            # the verify-on-stop nudge — see _should_inject_verify_nudge.
            self._record_verification_call(state, tool_call, result)

            # Emit TOOL_COMPLETED so monitors can track tool latency/usage.
            # Mirror _is_tool_result_failure so the event's `ok` field
            # agrees with the guardrail's failure classifier — a tool
            # returning ``{"error": "..."}`` (JSON string) would otherwise
            # be flagged as failure for steering but reported as success here.
            is_error = self._is_tool_result_failure(result)
            self._emit(
                EventType.TOOL_COMPLETED,
                {"tool": tool_call.name, "call_id": tool_call.id,
                 "ok": not is_error},
            )

            if self.on_tool_complete:
                error_msg = None
                summary = None
                if is_error:
                    error_msg = result if isinstance(result, str) else str(result)
                elif isinstance(result, str):
                    summary = result[:200] if len(result) > 200 else result
                try:
                    await self.on_tool_complete(tool_call.id, tool_call.name, summary, error_msg)
                except Exception as e:
                    logger.debug("on_tool_complete callback failed: {}", e)
            self._flush_messages(state)
        logger.info(
            "Added {} tool results to messages, continue to next iteration",
            len(results),
        )

        self._inject_pending_steer(state)

        # 连续失败检测：在所有 tool 执行完毕后检查是否陷入失败循环
        failure_result = self._phase_check_failure_loop(state)
        if failure_result is not None:
            return failure_result

        return IterationResult(
            should_continue=True, exit_reason=TurnExitReason.TOOL_CONTINUE
        )

    async def _phase_final_response(
        self, state: LoopState, response: Any
    ) -> IterationResult:
        logger.info("LLM returned final response (no tool calls)")

        # The agent is done calling tools, so any active skill guardrails
        # have served their purpose. Stop them to avoid leaking guardrail
        # state across unrelated turns / sessions.
        if self.loop.guardrail_manager:
            for skill_name in list(self.loop.guardrail_manager.active_skills):
                result = self.loop.guardrail_manager.stop_guarding(skill_name)
                if result and result.violations:
                    logger.info(
                        "Guardrail stopped for '{}': {} violation(s)",
                        skill_name, len(result.violations),
                    )

        if self.on_stream and self.on_stream_end:
            had_content_filter = any(
                getattr(a, "error", "") == "content_filter"
                for a in (state.last_attempts or [])
            )
            if had_content_filter:
                logger.info(
                    "Discarding polluted stream buffer "
                    "(content_filter in fallback chain)"
                )
                await self.on_stream_end(discard=True)
                self._stream_filter.reset()
                clean = strip_think(response.content) or None
                if clean and response.finish_reason not in ("error", "content_filter"):
                    await self._stream_filter(clean)
                    await self.on_stream_end(resuming=False)
                    self._stream_filter.reset()
            else:
                await self.on_stream_end(resuming=False)
                self._stream_filter.reset()
            state.stream_finalized = True

        clean = strip_think(response.content) or None
        if response.finish_reason == "error":
            safe_clean = (clean or "")[:200].replace("{", "{{").replace("}", "}}")
            logger.error("LLM returned error finish_reason: {}", safe_clean)
            state.final_content = clean or "Sorry, I encountered an error calling the AI model."
            return IterationResult(
                should_break=True, exit_reason=TurnExitReason.LLM_ERROR
            )
        if response.finish_reason == "content_filter":
            logger.warning(
                "LLM returned content_filter (content safety block), "
                "not saving filtered response to history"
            )
            state.final_content = (
                "抱歉，模型的内容安全策略阻止了本次回复，请尝试换一种方式描述您的请求。"
            )
            return IterationResult(
                should_break=True, exit_reason=TurnExitReason.CONTENT_FILTER
            )
        # File-mutation verifier: if the LLM claims file completion in its
        # final response but no mutating tool succeeded this turn, append a
        # verifier footer surfacing the mismatch. This catches the common
        # failure mode where the model confidently reports "I've updated
        # the file" without actually having called write_file/edit_file,
        # or having called them with errors it didn't acknowledge.
        #
        # The footer is included IN the assistant message content (not as a
        # separate system message) because Anthropic's API overwrites the
        # system prompt on every `role: system` message — injecting one
        # mid-conversation would clobber the main system prompt. Using a
        # `---` separator + `[System Verifier]` header keeps it clearly
        # distinguishable from the assistant's own words while remaining
        # provider-agnostic. Both the user (via final_content) and the next
        # LLM turn (via message history) see the warning.

        # Verify-on-stop nudge: if the model edited files this turn but
        # tries to end without calling any verification tool (exec/shell/
        # code_execution), inject a nudge and continue the loop instead of
        # accepting the final response. Mirrors agent's verify-on-stop:
        # "completion requires both done AND verified". Capped at
        # _MAX_VERIFY_NUDGES per turn to avoid infinite loops. Skipped for
        # doc-only edits (.md/.txt/.rst) since they have no verify surface.
        if self._should_inject_verify_nudge(state):
            nudge = self._build_verify_nudge(state)
            logger.info(
                "Verify-on-stop nudge injected (attempt {}/{}) — "
                "files edited but no verification tool called",
                state.verify_nudges, _MAX_VERIFY_NUDGES,
            )
            # Save the assistant's intended final response to history so
            # the model remembers what it was about to say, then append
            # the nudge as a user message and continue the loop.
            state.messages = self.loop.context.add_assistant_message(
                state.messages,
                clean,
                reasoning_content=response.reasoning_content,
                thinking_blocks=response.thinking_blocks,
            )
            state.messages.append({
                "role": "user",
                "content": nudge,
                "_verify_stop_nudge": True,
            })
            state.new_msg_start += 2
            self._flush_messages(state)
            # Keep file_mutations so OutcomeGate can still see residual
            # unverified work and escalate NUDGE → FOOTER via nudge_count.
            # side_effect_pending is likewise not cleared — only a real
            # verification tool (status/tests/ps) clears it.
            return IterationResult(
                should_continue=True,
                exit_reason=TurnExitReason.TOOL_CONTINUE,
            )

        verifier_footer = self._maybe_inject_mutation_verifier_footer(
            state, clean or ""
        )
        outcome_footer = self._outcome_footer_message(state)
        footers = [f for f in (verifier_footer, outcome_footer) if f]
        if footers:
            saved_content = (clean or "") + "\n\n---\n" + "\n".join(footers)
        else:
            saved_content = clean

        state.messages = self.loop.context.add_assistant_message(
            state.messages,
            saved_content,
            reasoning_content=response.reasoning_content,
            thinking_blocks=response.thinking_blocks,
        )
        state.final_content = saved_content
        logger.info("Final response captured, length={}", len(saved_content or ""))
        self._flush_messages(state)

        steer_text = self.loop.drain_steer(self.session_key)
        if steer_text and state.steer_continues < 3:
            steer_content = (
                "[User mid-task instruction — adjust your approach accordingly]\n"
                + steer_text
            )
            state.messages.append({
                "role": "user",
                "content": steer_content,
            })
            state.new_msg_start += 1
            state.steer_continues += 1
            logger.info(
                "Steer injected after final response, continuing loop ({} chars, attempt {}/3)",
                len(steer_text),
                state.steer_continues,
            )
            return IterationResult(
                should_continue=True, exit_reason=TurnExitReason.STEER_CONTINUE
            )

        return IterationResult(
            should_break=True, exit_reason=TurnExitReason.COMPLETED
        )

    def _detect_and_repair_hallucinated_tools(self, response: Any) -> list[str]:
        """Detect and attempt to repair hallucinated tool call names.

        Models sometimes invent tool names that don't exist. This method:
        1. Tries fuzzy matching to auto-repair minor misspellings
        2. Returns a list of tool names that are still invalid after repair

        Args:
            response: LLM response with tool_calls.

        Returns:
            List of tool names that could not be repaired.
        """
        valid_names = set(self.loop.tools.tool_names)
        # Sanitised names are accepted because we send the sanitised form on
        # the wire and the model echoes that back in tool_call.function.name.
        valid_names.update(self.loop.tools._sanitised.keys())
        invalid: list[str] = []

        for tc in response.tool_calls:
            if tc.name in valid_names:
                continue

            repaired = self._try_repair_tool_name(tc.name, valid_names)
            if repaired:
                logger.info(
                    "Auto-repaired hallucinated tool name: '{}' -> '{}'",
                    tc.name, repaired,
                )
                tc.name = repaired
            else:
                invalid.append(tc.name)

        return invalid

    @staticmethod
    def _try_repair_tool_name(name: str, valid_names: set[str]) -> str | None:
        """Try to repair a hallucinated tool name using fuzzy matching.

        Handles common patterns:
        - Underscore vs hyphen: read_file vs read-file
        - Missing prefix: search vs grep
        - Extra words: file_read vs read_file
        - Case differences: ReadFile vs read_file

        Returns repaired name if a close match is found, None otherwise.
        """
        if name in valid_names:
            return name

        normalized = name.lower().replace("-", "_").replace(" ", "_")

        candidates = []
        for valid in valid_names:
            valid_norm = valid.lower().replace("-", "_")
            if normalized == valid_norm:
                candidates.append(valid)
            elif normalized.replace("_", "") == valid_norm.replace("_", ""):
                candidates.append(valid)

        if len(candidates) == 1:
            return candidates[0]

        if "." in name:
            parts = name.split(".", 1)
            skill_name = parts[0]
            script_name = parts[1].lower().replace("-", "_").replace(" ", "_")
            for valid in valid_names:
                if "." not in valid:
                    continue
                v_parts = valid.split(".", 1)
                if v_parts[0] == skill_name and v_parts[1].lower().replace("-", "_") == script_name:
                    return valid

        return None

    async def _finalize_loop(self, state: LoopState) -> None:
        if state.final_content is None and state.budget_exceeded:
            cost_summary = self.loop.cost_tracker.get_summary()
            state.final_content = (
                f"Budget limit reached (${cost_summary['total_cost_usd']:.4f} / "
                f"${cost_summary['budget_limit_usd']:.2f}). "
                "I've stopped to avoid exceeding the spending limit. "
                "You can increase the budget or break the task into smaller steps."
            )
            # 把中断原因作为 assistant 消息追加到 messages，
            # 这样 save_turn 会把它持久化到 session history，
            # 下次用户输入"继续"时 LLM 能看到上次中断的上下文。
            state.messages = self.loop.context.add_assistant_message(
                state.messages,
                state.final_content,
            )
        elif state.final_content is None and state.iteration >= self.loop.max_iterations:
            logger.warning(
                "Max iterations ({}) reached", self.loop.max_iterations
            )
            # 构造包含失败方法黑名单的中文摘要，让用户能看到真实阻塞点，
            # 而不是泛泛的英文模板。同时保留失败方法清单，让"继续"时
            # LLM 能从 history 看到已尝试的方法。
            blacklist_text = ""
            failed_methods = self._tool_guardrail.state.failed_methods
            if failed_methods:
                recent = failed_methods[-10:]
                blacklist_text = "\n\n**已尝试但失败的方法（继续时不要重复）：**\n"
                blacklist_text += "\n".join(f"- {m}" for m in recent)
            state.final_content = (
                f"已达到工具调用迭代上限（{self.loop.max_iterations} 轮），"
                "任务尚未完成，已停止以避免继续消耗资源。"
                + blacklist_text
                + "\n\n**当前状态：**\n"
                "- 已完成的步骤请见上方对话历史\n"
                "- 未完成的子任务需要进一步处理\n\n"
                "**下一步建议：**\n"
                "1. 如果输入「继续」，请用与上方「已尝试但失败的方法」**不同**的方式推进\n"
                "2. 或把任务拆成更小的子任务逐个完成\n"
                "3. 如果当前环境不可行，请直接说明缺失了什么条件"
            )
            # 同样把中断原因写入 history，让下次"继续"能看到上次中断点。
            state.messages = self.loop.context.add_assistant_message(
                state.messages,
                state.final_content,
            )

        # 统一兜底：如果 final_content 已由其他退出路径（LLM 错误、无效工具、
        # 内容过滤、失败循环强制停止等）设置，但尚未追加到 messages，
        # 则在此统一追加，确保 save_turn 能持久化中断原因，让"继续"可见。
        if state.final_content:
            last_msg = state.messages[-1] if state.messages else None
            last_is_final = (
                last_msg is not None
                and last_msg.get("role") == "assistant"
                and isinstance(last_msg.get("content"), str)
                and last_msg.get("content") == state.final_content
            )
            if not last_is_final:
                state.messages = self.loop.context.add_assistant_message(
                    state.messages,
                    state.final_content,
                )
        self._flush_messages(state)

        if self.on_stream and self.on_stream_end and not state.stream_finalized:
            if state.final_content:
                await self.on_stream(state.final_content)
            await self.on_stream_end(resuming=False)
            self._stream_filter.reset()
            state.stream_finalized = True

        logger.info(
            "Loop ended: exit_reason={}, iterations={}/{}, tools_used={}, "
            "has_final_content={}, file_mutations={}",
            state.exit_reason.value if state.exit_reason else "unknown",
            state.iteration,
            self.loop.max_iterations,
            len(state.tools_used),
            state.final_content is not None,
            len(state.file_mutations),
        )

        token_summary = self.loop.cost_tracker.get_token_summary()
        logger.info(
            "Token usage - Total: input={}, output={}, "
            "cache_creation={}, cache_read={}",
            token_summary["total"]["input_tokens"],
            token_summary["total"]["output_tokens"],
            token_summary["total"]["cache_creation_input_tokens"],
            token_summary["total"]["cache_read_input_tokens"],
        )

        # Log cache effectiveness from the last cache event.
        cache_event = getattr(state, "last_cache_event", None)
        if cache_event is not None:
            from markbot.agent.cache_chip import render_cache_status_line
            try:
                status_text = render_cache_status_line(cache_event)
                logger.info("Cache status: {}", status_text.plain)
            except Exception:
                logger.info(
                    "Cache status: stability={}%, read={}, miss={}",
                    cache_event.stability_pct,
                    cache_event.cache_read_tokens,
                    cache_event.cache_miss_tokens,
                )

        cost_summary = self.loop.cost_tracker.get_summary()
        logger.info(
            "Cost - total=${:.6f}, calls={}, budget={}{}",
            cost_summary["total_cost_usd"],
            cost_summary["total_api_calls"],
            f"${cost_summary['budget_limit_usd']}"
            if cost_summary["budget_limit_usd"]
            else "unlimited",
            ", OVER BUDGET" if cost_summary["over_budget"] else "",
        )

        self._phase_active_memory_encoding(state)

        self._phase_memory_sync(state)

        # End of turn: unlock prefix-cache mutation queue and apply deferred
        # tool/skill/prompt surface changes for the *next* turn only.
        try:
            applied = self._cache_policy.end_turn()
            if applied:
                logger.info(
                    "Applied deferred cache mutations after turn: {}", applied
                )
        except Exception as e:
            logger.debug("cache_policy end_turn failed: {}", e)

        await self._phase_generate_handoff(state, cost_summary)

    async def _phase_generate_handoff(self, state: LoopState, cost_summary: dict) -> None:
        handoff_manager = getattr(self.loop, "handoff_manager", None)
        if handoff_manager is None:
            return
        try:
            handoff = build_handoff_from_session(
                session_key=self.session_key,
                messages=state.messages,
                tools_used=state.tools_used,
                cost_usd=cost_summary.get("total_cost_usd", 0.0),
                task_tracker=getattr(self.loop, "task_tracker", None),
                memory_manager=getattr(self.loop, "memory_manager", None),
                last_exit_reason=state.exit_reason.value if state.exit_reason else "",
            )
            handoff_manager.save(handoff)
            logger.info(
                "Session handoff saved: {} tasks, {} decisions",
                len(handoff.active_tasks),
                len(handoff.key_decisions),
            )
        except Exception as e:
            logger.warning("Failed to generate session handoff: {}", e)
