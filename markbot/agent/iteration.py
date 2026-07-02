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
from markbot.bus.events import EventType, make_session_key
from markbot.session.handoff import build_handoff_from_session
from markbot.types.permission import PermissionMode, ToolPermissionContext
from markbot.types.tool import ToolContext
from markbot.utils.helpers import strip_think

if TYPE_CHECKING:
    from markbot.agent.loop import AgentLoop

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
    Inspired by hermes-agent's ``_turn_exit_reason`` design, this replaces
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

    FAILURE_LOOP_FORCED_STOP = "failure_loop_forced_stop"
    """Consecutive tool failures triggered forced stop (reflections exhausted)."""

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
# Hermes's verify-on-stop: "completion requires both done AND verified".
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
# chat context doesn't make sense. Mirrors Hermes's surface-aware
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

# 连续失败检测配置
# 观察窗口：只看最近 N 次 tool call 结果
_FAILURE_WINDOW = 6
# 触发阈值：窗口内失败次数 >= 此值时注入反思提示
_FAILURE_THRESHOLD = 4
# 反思提示最大注入次数：超过后强制停止，避免无限循环
_MAX_REFLECTIONS = 2
# 单工具连续失败阈值：达到此值时注入禁用提示，逼 LLM 换工具类别
_TOOL_FAILURE_STREAK_THRESHOLD = 3
# Exact-failure 阈值：同一工具 + 相同参数签名连续失败 N 次时，注入
# "换个方法"提示。比 _TOOL_FAILURE_STREAK_THRESHOLD 更早触发（2 次），
# 因为相同参数重复失败几乎一定是死路。Mirrors Hermes's
# exact_failure_warn_after=2.
_EXACT_FAILURE_WARN_THRESHOLD = 2
# No-progress 阈值：幂等工具返回完全相同结果 N 次时，注入"无进展"提示。
# 适用于 read_file / search_files / glob 等只读工具——返回相同内容说明
# LLM 在原地踏步。Mirrors Hermes's no_progress_warn_after=2.
_NO_PROGRESS_WARN_THRESHOLD = 2
# 幂等/只读工具集合——对这些工具做 no-progress 检测。
# 写工具（write_file/edit_file）即使参数相同也可能合法重试（修复后重写），
# 所以不纳入 no-progress 检测。
_IDEMPOTENT_TOOL_NAMES: frozenset[str] = frozenset({
    "read_file", "list_directory", "glob", "grep", "search_files",
    "search_codebase", "find", "list", "todo",
})

# 失败标志关键词（小写匹配）
# 注意：关键词要足够具体，避免误判正常输出。
# - 不用 "404"（可能出现在文件大小、行号等正常文本中）
# - 不用 "not found"（过于宽泛，改用更具体的 "command not found" 等）
# - 不用 "timeout"（可能出现在 "no timeout" 等正常文本中，改用 "connection timed out"）
# - 不用裸 "forbidden"（可能出现在正常抓取页面内容中），用 "forbidden origin" 等更具体形式
# - 不用 "http status 4"/"status code: 4" 等文本前缀：web_fetch 成功抓取的页面
#   如果讲 HTTP 状态码就会误判。HTTP 失败靠 JSON 结构化检测（"status_code": 4xx）覆盖。
# - 不用 "connection reset"/"http error" 等宽泛词：网络教程页面会包含这些词。
#   只保留足够具体的短语（"fetch failed"、"readability failed" 等）。
_FAILURE_KEYWORDS = (
    # 通用错误（既有，exec 类失败信号）
    "error:", "traceback", "no module named", "modulenotfounderror",
    "filenotfounderror", "command not found", "no such file or directory",
    "connection refused", "connection timed out", "timed out",
    "permission denied", "access denied", "exit code: 1",
    "exit code: 2", "exit code: 127", "failed to",
    "unable to", "cannot ", "can't ", "is not installed",
    "is not available", "500 internal server error",
    # web_fetch / HTTP 类失败（仅保留足够具体的短语，避免误判正常页面内容）
    "fetch failed",
    "readability extraction failed", "readability failed",
    "jina reader", "forbidden origin",
    # JSON 格式的错误字段（web_fetch 失败常返回 {"error": "..."}）
    # 这些是结构化的，不会出现在正常页面正文里，安全。
    '"error":', '"errors":', '"error_code":', '"errcode":',
    # JSON 格式的 HTTP 状态码失败（4xx/5xx），结构化安全
    '"status_code": 4', '"status_code": 5',
    '"status": 4', '"status": 5',
)


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
    stream_finalized: bool = False
    current_tokens: int = 0
    last_response: Any = None
    last_attempts: list = field(default_factory=list)
    last_compact_action: CompactAction = CompactAction.NONE
    steer_continues: int = 0
    # Cache telemetry (populated by _phase_call_llm).
    last_cache_event: CacheEvent | None = None
    # 连续失败检测：记录最近 tool call 的成功/失败，用于识别"死胡同"循环。
    # True=成功，False=失败。只保留最近 _FAILURE_WINDOW 条。
    recent_tool_results: list = field(default_factory=list)
    # 反思提示已注入次数：避免无限注入反思，超过阈值后强制停止。
    # 注意：此值从 self.loop.get_failure_state(session_key) 加载，跨 loop
    # （compact / "继续" / handoff）持久化，仅在用户新请求时重置。
    reflection_injected_count: int = 0
    # 已尝试且失败的方法黑名单（跨 loop 持久化）。
    # 每次工具失败时追加 "tool_name: 失败摘要"，去重。
    # 用于：1) compact summary 注入；2) 反思提示中提醒 LLM 不要重试。
    failed_methods: list = field(default_factory=list)
    # 强制停止已触发次数（跨 loop 持久化）。
    # 当 forced_stop_count > 0 且再次触发失败循环时，直接强制停止，不再给反思机会。
    forced_stop_count: int = 0
    # 单工具连续失败计数（per-loop，不持久化）。
    # key=tool_name, value=连续失败次数。成功则归零。
    # 当某工具连续失败 >= _TOOL_FAILURE_STREAK_THRESHOLD 次时，
    # 注入禁用提示，逼 LLM 换工具类别（见 logs/2026-06-27.log 中
    # web_fetch 连续 3 次 Jina Reader 失败后仍继续 web_fetch 的案例）。
    tool_consecutive_failures: dict = field(default_factory=dict)
    # 已注入禁用提示的工具集合，避免反复注入。
    tools_disabled_warned: set = field(default_factory=set)
    # Exact-failure 追踪：key = "tool_name:args_signature"，value = 连续失败次数。
    # 当同一工具+相同参数连续失败 >= _EXACT_FAILURE_WARN_THRESHOLD 时注入提示。
    # 成功调用（任意参数）会清除该工具的所有 exact-failure 计数。
    exact_failure_counts: dict = field(default_factory=dict)
    # No-progress 追踪：key = "tool_name:args_signature"，value = (结果哈希, 连续次数)。
    # 当幂等工具用相同参数返回相同结果 >= _NO_PROGRESS_WARN_THRESHOLD 时注入提示。
    no_progress_counts: dict = field(default_factory=dict)
    # 已注入 exact-failure / no-progress 提示的签名集合，避免反复注入。
    guardrail_signatures_warned: set = field(default_factory=set)
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
        on_tool_start: Callable[[str, str, str | None], Awaitable[None]] | None = None,
        on_tool_complete: Callable[[str, str, str | None, str | None], Awaitable[None]] | None = None,
    ) -> None:
        self.loop = loop
        self.channel = channel
        self.chat_id = chat_id
        self.message_id = message_id
        self.on_progress = on_progress
        self.on_stream = on_stream
        self.on_stream_end = on_stream_end
        self.session_key = session_key or make_session_key(channel, chat_id) or ""
        self.on_tool_start = on_tool_start
        self.on_tool_complete = on_tool_complete
        self._stream_filter = StreamFilter(on_stream)
        self._bootstrap_report: Any | None = None

    async def run(
        self,
        initial_messages: list[dict],
    ) -> tuple[str | None, list[str], list[dict], int]:
        state = LoopState(
            messages=initial_messages,
            initial_count=len(initial_messages),
            new_msg_start=len(initial_messages),
        )

        # 从 AgentLoop 加载跨 loop 持久化的失败循环状态。
        # 关键：reflection_injected_count / failed_methods / forced_stop_count
        # 必须跨 compact / "继续" / handoff 边界保留，否则强制停止机制
        # 在整个任务尺度上失效（每次新 loop 都从 0 开始，永远凑不够阈值）。
        # 仅在用户输入新请求时由 loop.reset_failure_state() 清零。
        if self.session_key:
            try:
                persisted = self.loop.get_failure_state(self.session_key)
                state.reflection_injected_count = persisted.get(
                    "reflection_injected_count", 0
                )
                state.failed_methods = list(persisted.get("failed_methods", []))
                state.forced_stop_count = persisted.get("forced_stop_count", 0)
                if state.reflection_injected_count or state.failed_methods or state.forced_stop_count:
                    logger.info(
                        "Resumed failure-loop state for session={}: "
                        "reflections={}, failed_methods={}, forced_stops={}",
                        self.session_key,
                        state.reflection_injected_count,
                        len(state.failed_methods),
                        state.forced_stop_count,
                    )
            except Exception as e:
                logger.warning(
                    "Failed to load persisted failure-loop state: {}", e
                )

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

        return state.final_content, state.tools_used, state.messages, state.new_msg_start

    async def _run_one_iteration(self, state: LoopState) -> IterationResult:
        # Bump the messages revision so the token estimate cache
        # knows to recompute on the next call.
        rev = getattr(self.loop, "_messages_revision", None)
        if rev is not None:
            self.loop._messages_revision = rev + 1
        state.current_tokens = self._estimate_tokens(state)
        logger.debug("Estimated context tokens: {}", state.current_tokens)

        await self._phase_compact(state)
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

        state.messages, compact_result = await self.loop.compactor.maybe_compact(
            state.messages, state.current_tokens, self.loop.context_window_tokens,
            task_context=task_context,
        )
        state.last_compact_action = compact_result.action
        if compact_result.action != CompactAction.NONE:
            logger.info(
                "Compaction applied: action={}, "
                "messages: {} -> {}, tokens: {} -> {}{}",
                compact_result.action.value,
                compact_result.messages_before,
                compact_result.messages_after,
                compact_result.tokens_before,
                compact_result.tokens_after,
                f", summary={compact_result.summary[:100]}..."
                if compact_result.summary
                else "",
            )
            state.current_tokens = compact_result.tokens_after
            if len(state.messages) < state.new_msg_start:
                state.new_msg_start = self.loop._recalc_new_msg_start_after_compact(
                    state.messages, state.initial_count, state.new_msg_start,
                )
            # 关键修复：compact 后显式注入「已尝试且失败的方法」黑名单。
            # compact summary 的 LLM 生成时只会保留任务目标和工具名，
            # 细节丢失，导致 LLM compact 后立刻又去 web_fetch GitHub、
            # 又去 strings 二进制——和前一轮一模一样的失败路径（见
            # logs/2026-06-27.log Interaction #44 后的重复撞墙）。
            # 把跨 loop 持久化的 failed_methods 作为独立 system message
            # 注入到 compact summary 之后，确保 LLM 能看到并避开。
            if state.failed_methods:
                recent = state.failed_methods[-15:]
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
                # 插到所有开头 system 消息之后（system prompt / compact_msg 之后），
                # 保证 LLM 优先看到且不破坏 system prompt 的首位。
                insert_idx = 0
                for i, m in enumerate(state.messages):
                    if m.get("role") == "system":
                        insert_idx = i + 1
                    else:
                        break
                state.messages.insert(insert_idx, blacklist_msg)
                logger.info(
                    "Injected failure blacklist ({} entries) after compact ({}) at idx {}",
                    len(recent), compact_result.action.value, insert_idx,
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

        if memory_manager and hasattr(memory_manager, "prefetch"):
            try:
                session_id = self.session_key or ""
                prefetch_ctx = memory_manager.prefetch(user_text, session_id=session_id)
                if prefetch_ctx and prefetch_ctx.strip():
                    context_parts.append(prefetch_ctx)
                    logger.info("Memory prefetch context assembled")
            except Exception as e:
                logger.warning("Memory prefetch failed: {}", e)

        if memory_manager and hasattr(memory_manager, "get_memory_context"):
            try:
                memory_ctx = memory_manager.get_memory_context(
                    query=user_text,
                    session_key=self.session_key,
                )
                if memory_ctx and memory_ctx.strip():
                    context_parts.append(memory_ctx)
                    logger.info("Memory context assembled (summary + store)")
            except Exception as e:
                logger.warning("Memory context injection failed: {}", e)

        if mem_tool:
            try:
                if user_text:
                    forced_ctx = await mem_tool.get_forced_context(user_text)
                    if forced_ctx:
                        context_parts.append(forced_ctx)
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

    @staticmethod
    def _inject_internal_context(
        state: LoopState, sys_idx: int, context_block: str,
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
        state.new_msg_start += 1

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
        """判断 tool 执行结果是否为失败。

        判断依据（按优先级）：
        1. BaseException → 失败（_phase_execute_tools 通常已转为 "Error: ..." 字符串）
        2. dict 结果含非空 error/errors 字段、HTTP status >= 400、ok/success=False → 失败
        3. 字符串以 "Error:" 开头 → 失败
        4. 字符串可解析为 JSON 且解析后 dict 满足上述失败条件 → 失败
        5. 字符串包含失败关键词（exit code 非零、fetch failed 等）→ 失败

        注意：web_fetch / HTTP 类工具失败常返回 ``{"error": "Fetch failed (...)"}``
        这种 JSON 字符串，必须显式解析才能识别，纯关键词匹配会漏检。
        """
        if isinstance(result, BaseException):
            return True

        # dict 结构化结果（部分工具直接返回 dict）
        if isinstance(result, dict):
            return self._dict_has_error(result)

        if not isinstance(result, str):
            # list / int / multimodal 等视为成功
            return False

        if result.startswith("Error:"):
            return True

        # 尝试解析 JSON 字符串（web_fetch 失败常以 JSON 形式返回）
        stripped = result.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                parsed = None
            if isinstance(parsed, dict) and self._dict_has_error(parsed):
                return True

        lower = result.lower()
        return any(kw in lower for kw in _FAILURE_KEYWORDS)

    @staticmethod
    def _dict_has_error(obj: dict) -> bool:
        """检查 dict 形式的 tool 结果是否表示失败。

        覆盖三种常见失败信号：
        - 非空 error / errors / error_code / errcode 字段
        - HTTP 状态码 >= 400（status / status_code / http_status）
        - 显式 ok=False / success=False
        """
        # 非空错误字段
        for k in ("error", "errors", "error_code", "errcode"):
            v = obj.get(k)
            if v:  # 非空字符串、非零、非空列表都算
                return True
        # HTTP 状态码失败
        for k in ("status", "status_code", "http_status", "code"):
            v = obj.get(k)
            if isinstance(v, int) and v >= 400:
                return True
        # 显式失败标记
        if obj.get("ok") is False or obj.get("success") is False:
            return True
        return False

    def _record_tool_result(
        self, state: LoopState, result: Any, tool_name: str = "",
        tool_call: Any = None,
    ) -> None:
        """记录 tool 结果到滑动窗口，用于连续失败检测。

        失败时同时把 "tool_name: 简短摘要" 追加到 ``state.failed_methods``
        黑名单（去重），并同步回 AgentLoop 的持久化状态，供 compact summary
        和反思提示引用，避免 LLM compact 后失忆式重复撞墙。

        同时执行三层 guardrail 检测（mirrors Hermes's tool_guardrails）:
        1. 单工具连续失败（_TOOL_FAILURE_STREAK_THRESHOLD）→ 禁用提示
        2. Exact-failure：同一工具+相同参数签名连续失败 → "换方法"提示
        3. No-progress：幂等工具相同参数返回相同结果 → "无进展"提示
        """
        is_failure = self._is_tool_result_failure(result)
        state.recent_tool_results.append(is_failure)
        # 只保留最近 _FAILURE_WINDOW 条
        if len(state.recent_tool_results) > _FAILURE_WINDOW:
            state.recent_tool_results = state.recent_tool_results[-_FAILURE_WINDOW:]

        # 失败时记录方法黑名单（跨 loop 持久化）
        if is_failure and tool_name:
            entry = self._summarize_failed_method(tool_name, result)
            if entry and entry not in state.failed_methods:
                # 上限保护：最多保留 30 条，避免无限增长
                if len(state.failed_methods) >= 30:
                    state.failed_methods = state.failed_methods[-29:]
                state.failed_methods.append(entry)
                self._persist_failure_state(state)

        # 单工具连续失败检测：达到阈值时注入禁用提示，逼 LLM 换工具类别。
        # 仅在本次 loop 内计数（不跨 loop 持久化，因为 compact 后上下文已变）。
        if tool_name:
            if is_failure:
                state.tool_consecutive_failures[tool_name] = (
                    state.tool_consecutive_failures.get(tool_name, 0) + 1
                )
            else:
                # 成功则归零
                state.tool_consecutive_failures.pop(tool_name, None)

            streak = state.tool_consecutive_failures.get(tool_name, 0)
            if (
                streak >= _TOOL_FAILURE_STREAK_THRESHOLD
                and tool_name not in state.tools_disabled_warned
            ):
                state.tools_disabled_warned.add(tool_name)
                logger.warning(
                    "Tool '{}' failed {} times in a row — injecting disable hint",
                    tool_name, streak,
                )
                self._inject_tool_disable_hint(state, tool_name, streak)

        # Exact-failure 和 no-progress 检测需要 tool_call 来提取参数签名。
        # 当 tool_call 不可用（旧调用路径）时跳过——这两层是新增的精细检测，
        # 不影响既有的连续失败检测。
        if tool_call is not None and tool_name:
            self._check_exact_failure(state, tool_name, tool_call, result, is_failure)
            self._check_no_progress(state, tool_name, tool_call, result)

    @staticmethod
    def _args_signature(tool_call: Any) -> str:
        """Build a stable signature for a tool call's arguments.

        Used as the key for exact-failure and no-progress tracking. We sort
        dict keys so {"a": 1, "b": 2} and {"b": 2, "a": 1} produce the same
        signature. Long values are truncated to keep the signature compact.
        """
        args = getattr(tool_call, "arguments", None)
        if args is None:
            return ""
        if isinstance(args, dict):
            parts = []
            for k in sorted(args.keys()):
                v = str(args[k])
                if len(v) > 60:
                    v = v[:57] + "..."
                parts.append(f"{k}={v}")
            return ",".join(parts)
        if isinstance(args, list):
            return str(args)[:200]
        return str(args)[:200]

    @staticmethod
    def _result_signature(result: Any) -> str:
        """Build a compact hashable signature of a tool result.

        Used for no-progress detection on idempotent tools — two calls with
        the same args returning the same signature indicate the model is
        re-reading the same unchanged content. We truncate to keep memory
        bounded; we only care about *equality*, not full content.
        """
        if isinstance(result, BaseException):
            return f"exc:{type(result).__name__}"
        text = result if isinstance(result, str) else str(result)
        # 256 chars is enough to distinguish "same content" from "different
        # content" for read-only tools — we are not doing real diffing.
        return text[:256]

    def _check_exact_failure(
        self, state: LoopState, tool_name: str,
        tool_call: Any, result: Any, is_failure: bool,
    ) -> None:
        """Exact-failure guardrail: same tool + same args failing repeatedly.

        When the same call signature fails _EXACT_FAILURE_WARN_THRESHOLD
        times in a row, inject a hint telling the model to change approach.
        Any successful call with the same tool (any args) resets all
        exact-failure counters for that tool — the model found a working
        invocation.
        """
        sig = self._args_signature(tool_call)
        if not sig:
            return
        key = f"{tool_name}:{sig}"
        if is_failure:
            state.exact_failure_counts[key] = (
                state.exact_failure_counts.get(key, 0) + 1
            )
        else:
            # Success: clear all exact-failure entries for this tool —
            # the model found a working approach, do not nag it.
            to_clear = [k for k in state.exact_failure_counts if k.startswith(f"{tool_name}:")]
            for k in to_clear:
                del state.exact_failure_counts[k]
            return

        count = state.exact_failure_counts[key]
        if (
            count >= _EXACT_FAILURE_WARN_THRESHOLD
            and key not in state.guardrail_signatures_warned
        ):
            state.guardrail_signatures_warned.add(key)
            logger.warning(
                "Exact-failure guardrail: tool '{}' with args [{}] failed "
                "{} times — injecting change-approach hint",
                tool_name, sig, count,
            )
            self._inject_exact_failure_hint(state, tool_name, sig, count)

    def _check_no_progress(
        self, state: LoopState, tool_name: str,
        tool_call: Any, result: Any,
    ) -> None:
        """No-progress guardrail: idempotent tool returning identical output.

        Only applies to read-only tools (read_file / glob / grep / etc.) —
        write tools legitimately produce similar confirmations. When the
        same call signature returns the same result signature
        _NO_PROGRESS_WARN_THRESHOLD times, inject a hint telling the model
        it is re-reading unchanged content and should act instead.
        """
        lookup_name = self.loop.tools.resolve_sanitised_name(tool_name) \
            if hasattr(self.loop, "tools") else tool_name
        if lookup_name not in _IDEMPOTENT_TOOL_NAMES:
            return
        # Skip failures — those are handled by exact-failure / streak detection.
        if self._is_tool_result_failure(result):
            return
        sig = self._args_signature(tool_call)
        if not sig:
            return
        key = f"{tool_name}:{sig}"
        result_sig = self._result_signature(result)
        prev = state.no_progress_counts.get(key)
        if prev is None or prev[0] != result_sig:
            state.no_progress_counts[key] = (result_sig, 1)
            return
        new_count = prev[1] + 1
        state.no_progress_counts[key] = (result_sig, new_count)
        if (
            new_count >= _NO_PROGRESS_WARN_THRESHOLD
            and key not in state.guardrail_signatures_warned
        ):
            state.guardrail_signatures_warned.add(key)
            logger.warning(
                "No-progress guardrail: idempotent tool '{}' with args [{}] "
                "returned identical result {} times — injecting hint",
                tool_name, sig, new_count,
            )
            self._inject_no_progress_hint(state, tool_name, sig, new_count)

    def _inject_tool_disable_hint(
        self, state: LoopState, tool_name: str, streak: int
    ) -> None:
        """注入"该工具已连续失败 N 次，不要再用"的提示到最近一条 tool result。

        这是对 _phase_check_failure_loop 的补充：失败循环检测看的是整体
        失败率，而单工具连续失败检测看的是某个具体工具是否在反复撞墙。
        后者更早触发（3 次 vs 4/6 次），且针对性强——直接告诉 LLM 换工具。
        """
        hint = (
            f"\n\n[System Warning — 工具连续失败]\n"
            f"工具 `{tool_name}` 已连续失败 {streak} 次。这强烈表明该工具"
            "在当前任务/环境下不可用或方法不对。**请立即停止使用此工具**，"
            "改用完全不同的工具或方法。如果没有任何替代方案，请直接告知用户"
            "当前环境无法完成此任务，并说明缺失的条件。"
        )
        self._append_hint_to_last_tool_result(state, hint)

    def _inject_exact_failure_hint(
        self, state: LoopState, tool_name: str, args_sig: str, count: int
    ) -> None:
        """注入"相同参数已失败 N 次，换方法"提示。

        比 _inject_tool_disable_hint 更精细：不是禁用整个工具，而是指出
        *特定参数组合*行不通，引导 LLM 调整参数或换工具。例如 read_file
        用同一个不存在路径失败 2 次，提示 LLM 先 list_directory 确认路径。
        """
        hint = (
            f"\n\n[System Warning — 相同调用重复失败]\n"
            f"工具 `{tool_name}` 用参数 [{args_sig}] 已连续失败 {count} 次。"
            "**完全相同的调用注定会再次失败**——不要用同样的参数重试。"
            "请：1) 检查参数是否有拼写/路径错误；2) 用只读工具（如 "
            "list_directory / glob）确认目标；3) 或换一个完全不同的方法。"
        )
        self._append_hint_to_last_tool_result(state, hint)

    def _inject_no_progress_hint(
        self, state: LoopState, tool_name: str, args_sig: str, count: int
    ) -> None:
        """注入"无进展"提示：幂等工具返回相同结果，说明在原地踏步。

        常见场景：LLM 反复 read_file 同一文件想"重新确认内容"，但文件
        没变，结果当然一样。提示 LLM 基于已知内容采取行动，而不是重复读取。
        """
        hint = (
            f"\n\n[System Warning — 无进展检测]\n"
            f"只读工具 `{tool_name}` 用参数 [{args_sig}] 已返回**完全相同**"
            f"的结果 {count} 次。重复读取未变化的内容不会带来新信息。"
            "**停止重复调用**，基于已有信息采取行动：执行修改、换一个工具、"
            "或向用户报告当前状态。"
        )
        self._append_hint_to_last_tool_result(state, hint)

    @staticmethod
    def _append_hint_to_last_tool_result(state: LoopState, hint: str) -> None:
        """把 hint 文本追加到最近一条 tool result 消息（或作为 user 消息）。

        追加到 tool result 而非新建 user 消息，是因为这样 LLM 在下一轮
        看到的是"工具返回值末尾的指导"，语义上更连贯，也不会打断
        tool/assistant 的消息配对结构。
        """
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

    @staticmethod
    def _summarize_failed_method(tool_name: str, result: Any) -> str:
        """生成 "tool_name: 失败摘要" 用于黑名单。

        摘要要短（≤80 字符），让 LLM 在 compact summary / 反思提示中
        能快速识别"这个方法已经试过且失败了"。
        """
        if isinstance(result, BaseException):
            detail = f"{type(result).__name__}: {result}"
        elif isinstance(result, dict):
            # 取 error 字段或整体 str
            err = result.get("error") or result.get("errors") or result.get("error_code")
            detail = str(err) if err else str(result)
        elif isinstance(result, str):
            # 尝试解析 JSON
            stripped = result.lstrip()
            if stripped.startswith("{"):
                try:
                    obj = json.loads(stripped)
                    if isinstance(obj, dict):
                        err = obj.get("error") or obj.get("errors") or obj.get("error_code")
                        if err:
                            detail = str(err)
                        else:
                            detail = result
                    else:
                        detail = result
                except (json.JSONDecodeError, ValueError):
                    detail = result
            else:
                detail = result
        else:
            detail = str(result)
        # 截断到 80 字符
        if len(detail) > 80:
            detail = detail[:77] + "..."
        return f"{tool_name}: {detail}"

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
        # Skip error results — only successful mutations count.
        if isinstance(result, str) and result.startswith("Error:"):
            return
        if isinstance(result, BaseException):
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
        """Mark that the model ran a verification tool this turn.

        Used by the verify-on-stop nudge: if the model edited files and then
        called ``exec``/``shell``/``code_execution`` (even with errors), we
        consider verification attempted and do not nag again. The point is
        to catch the "edited file → immediately declared done" failure mode,
        not to police whether the tests passed.
        """
        lookup_name = self.loop.tools.resolve_sanitised_name(tool_call.name)
        if lookup_name not in VERIFICATION_TOOL_NAMES:
            return
        # Even a failed exec counts as "verification attempted" — the model
        # tried; we do not want to loop forever on a broken test suite.
        state.verification_done = True

    def _should_inject_verify_nudge(self, state: LoopState) -> bool:
        """Decide whether to inject a verify-on-stop nudge.

        Conditions (all must hold):
        0. The current channel is a local/programming surface (cli/web).
           Messaging channels (feishu/qq/weixin/...) are skipped — the
           verify nudge is noise there.
        1. The model edited at least one non-doc file this turn
           (``file_mutations`` has non-doc entries).
        2. The model has NOT called any verification tool this turn
           (``verification_done`` is False).
        3. We have not already injected ``_MAX_VERIFY_NUDGES`` nudges.

        Returns ``True`` when a nudge should be injected.
        """
        if self.channel not in _VERIFY_ON_STOP_SURFACES:
            return False
        if state.verification_done:
            return False
        if state.verify_nudges >= _MAX_VERIFY_NUDGES:
            return False
        # Look for at least one non-doc mutation.
        for mut in state.file_mutations:
            path = mut.get("path", "")
            ext = ""
            if "." in path:
                ext = "." + path.rsplit(".", 1)[-1].lower()
            if ext not in _NO_VERIFY_EXTENSIONS:
                return True
        return False

    def _build_verify_nudge(self, state: LoopState) -> str:
        """Build the verify-on-stop nudge text.

        Mentions the edited paths so the model knows what was changed, and
        suggests common verify commands (tests / linter / type-checker).
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
        """Detect file-completion claims without matching mutations; append footer.

        Returns the footer text to inject, or ``None`` if no mismatch was
        detected.

        Heuristic:
        1. Scan ``final_content`` for file-completion claim patterns.
        2. If claims found but ``state.file_mutations`` is empty, the LLM
           is reporting file work that never landed — append a verifier
           footer so the user (and next-turn LLM) sees the truth.
        3. If mutations exist, no footer is needed — the claim is backed
           by real tool calls.

        Known limitation: ``state.file_mutations`` is per-turn (reset on
        each new ``LoopState``). If the LLM did file work in a *previous*
        turn and the current turn's final response references it (e.g.
        a summary like "I've updated the file as requested"), the verifier
        will false-positive because no mutation happened *this* turn. This
        is acceptable — the footer says "may be inaccurate — verify", which
        is reasonable advice even in the false-positive case, and the user
        can dismiss it after checking.
        """
        if not final_content:
            return None
        content_lower = final_content.lower()
        has_claim = any(p in content_lower for p in _FILE_COMPLETION_CLAIM_PATTERNS)
        if not has_claim:
            return None
        if state.file_mutations:
            # Mutations exist — claim is backed by real tool calls.
            return None
        # Mismatch detected: LLM claims file completion but no mutating
        # tool succeeded this turn. Inject a verifier footer.
        footer = (
            "[System Verifier — file-mutation check]\n"
            "The assistant's response above claims file work was completed "
            "(created/updated/modified/deleted), but no successful "
            "write_file / edit_file / delete_file tool call was recorded "
            "in this turn. The claim may be inaccurate — verify the actual "
            "file system state before relying on this response.\n"
            "（系统校验：上方回复声称完成了文件操作，但本轮未记录到成功的"
            "文件写入/编辑/删除工具调用，请核查实际文件状态。）"
        )
        logger.warning(
            "File-mutation verifier: LLM claims file completion but "
            "no mutating tool succeeded this turn — injecting footer"
        )
        return footer

    def _persist_failure_state(self, state: LoopState) -> None:
        """把当前的失败循环状态同步回 AgentLoop 的 per-session 持久化存储。"""
        if not self.session_key:
            return
        try:
            persisted = self.loop.get_failure_state(self.session_key)
            persisted["reflection_injected_count"] = state.reflection_injected_count
            persisted["failed_methods"] = list(state.failed_methods)
            persisted["forced_stop_count"] = state.forced_stop_count
        except Exception as e:
            logger.warning("Failed to persist failure-loop state: {}", e)

    def _phase_check_failure_loop(self, state: LoopState) -> IterationResult | None:
        """检测连续失败循环，注入反思提示或强制停止。

        当最近 _FAILURE_WINDOW 次 tool call 中有 >= _FAILURE_THRESHOLD 次失败时：
        - 若该 session 之前已强制停止过（forced_stop_count > 0）：直接再次强制停止，
          不再给反思机会。这是关键修复——避免 compact/"继续" 后 reflection_injected_count
          被重置导致 LLM 又能继续撞墙。
        - 若反思次数 < _MAX_REFLECTIONS：注入反思提示（含失败方法黑名单），让 LLM 重新评估
        - 若反思次数 >= _MAX_REFLECTIONS：强制停止，避免无限消耗 token
        """
        if len(state.recent_tool_results) < _FAILURE_THRESHOLD:
            return None

        failure_count = sum(1 for f in state.recent_tool_results if f)
        if failure_count < _FAILURE_THRESHOLD:
            return None

        # 已达最大反思次数，或之前已强制停止过 → 直接强制停止
        # （跨 loop 持久化：forced_stop_count > 0 意味着这个任务已经在更早的
        # loop 里被强制停止过，现在 compact/"继续" 又触发了失败循环，
        # 不应再给反思机会，直接停止并汇报。）
        should_force_stop = (
            state.reflection_injected_count >= _MAX_REFLECTIONS
            or state.forced_stop_count > 0
        )
        if should_force_stop:
            reason = (
                f"max reflections ({_MAX_REFLECTIONS}) exceeded"
                if state.reflection_injected_count >= _MAX_REFLECTIONS
                else f"prior forced stop (count={state.forced_stop_count})"
            )
            logger.warning(
                "Failure loop detected ({} failures in last {} calls), {} — forcing stop",
                failure_count, len(state.recent_tool_results), reason,
            )
            state.forced_stop_count += 1
            self._persist_failure_state(state)
            # 清理 guardrail，与 _phase_final_response 行为一致
            if self.loop.guardrail_manager:
                for skill_name in list(self.loop.guardrail_manager.active_skills):
                    result = self.loop.guardrail_manager.stop_guarding(skill_name)
                    if result and result.violations:
                        logger.info(
                            "Guardrail stopped for '{}': {} violation(s)",
                            skill_name, len(result.violations),
                        )
            # 构造包含失败方法黑名单的最终汇报，让用户能看到真实阻塞点
            blacklist_text = ""
            if state.failed_methods:
                # 取最近 10 条，避免过长
                recent = state.failed_methods[-10:]
                blacklist_text = "\n\n**已尝试但失败的方法（不要重复）：**\n"
                blacklist_text += "\n".join(f"- {m}" for m in recent)
            state.final_content = (
                "我检测到任务陷入连续失败循环（最近多次 tool 调用均失败），"
                f"已第 {state.forced_stop_count} 次强制停止以避免无谓的 token 消耗。"
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
                should_break=True, exit_reason=TurnExitReason.FAILURE_LOOP_FORCED_STOP
            )

        # 注入反思提示
        state.reflection_injected_count += 1
        self._persist_failure_state(state)
        logger.warning(
            "Failure loop detected ({} failures in last {} calls), "
            "injecting reflection #{} (persisted total)",
            failure_count, len(state.recent_tool_results),
            state.reflection_injected_count,
        )

        # 构造失败方法黑名单片段，提醒 LLM 不要重试这些方法
        blacklist_section = ""
        if state.failed_methods:
            recent = state.failed_methods[-8:]
            blacklist_section = (
                "\n\n**已尝试且失败的方法（绝对不要重复这些）：**\n"
                + "\n".join(f"- {m}" for m in recent)
                + "\n\n如果你接下来要用的方法和上面任意一条相似，请立刻停止并告知用户。"
            )

        reflection_text = (
            "[System Warning — 连续失败检测]\n"
            f"系统检测到你在最近 {len(state.recent_tool_results)} 次 tool 调用中"
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
            f"{state.reflection_injected_count}/{_MAX_REFLECTIONS} 次反思，"
            "达到上限后将强制停止。不要再用相同或类似的方式重试。"
            "如果任务在当前环境不可行，直接回复用户说明原因。"
        )

        # 注入到最后一条 tool result 之后（作为 user 消息追加，
        # 这样 LLM 能在下一轮看到反思提示）
        last_tool_idx = None
        for i in range(len(state.messages) - 1, -1, -1):
            if state.messages[i].get("role") == "tool":
                last_tool_idx = i
                break

        if last_tool_idx is not None:
            existing = state.messages[last_tool_idx].get("content", "")
            if isinstance(existing, str):
                state.messages[last_tool_idx]["content"] = (
                    existing + "\n\n" + reflection_text
                )
            elif isinstance(existing, list):
                state.messages[last_tool_idx]["content"].append(
                    {"type": "text", "text": reflection_text}
                )
        else:
            # 没有 tool result 时，作为 user 消息追加
            state.messages.append({
                "role": "user",
                "content": reflection_text,
            })

        # 反思后只保留窗口的一半（而非完全清空）。
        # 完全清空会让 LLM 又得凑够 _FAILURE_THRESHOLD 次失败才再触发检测，
        # 等于给"不换方法继续撞墙"的 LLM 续命，浪费 token（见
        # logs/2026-06-27.log 中反思后立刻又重复 web_fetch 失败的案例）。
        # 保留一半意味着：反思后若继续失败，只需再积累少量失败就能再次触发，
        # 逼 LLM 要么真换方法（成功会稀释窗口），要么快速走到强制停止。
        if state.recent_tool_results:
            keep = max(1, _FAILURE_WINDOW // 2)
            state.recent_tool_results = state.recent_tool_results[-keep:]
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
            for tc in response.tool_calls:
                if not self.loop.tools.has(tc.name):
                    state.messages = await self.loop.context.add_tool_result_async(
                        state.messages, tc.id, tc.name, error_msg
                    )
                else:
                    result = await self.loop.tools.execute(tc.name, tc.arguments, context=ToolContext(
                        session_id=make_session_key(self.channel, self.chat_id) or "",
                        workspace=str(self.loop.workspace),
                        permission_mode=PermissionMode.AUTO,
                        tool_permission_context=ToolPermissionContext(mode=PermissionMode.AUTO),
                        is_non_interactive=False,
                        channel=self.channel,
                        chat_id=self.chat_id,
                        message_id=self.message_id,
                    ))
                    state.messages = await self.loop.context.add_tool_result_async(
                        state.messages, tc.id, tc.name, result
                    )
                    # Track file mutations in the recovery path too — if the
                    # LLM mixed a valid write_file with hallucinated tools,
                    # the mutation must still be recorded so the verifier
                    # doesn't false-positive on the final response.
                    self._record_file_mutation(state, tc, result)
                    self._record_verification_call(state, tc, result)
                    # Also feed the result into failure detection and
                    # guardrails, so a valid tool failing in this recovery
                    # path is still tracked (exact-failure / no-progress /
                    # recent_tool_results). Without this, the recovery path
                    # would be a blind spot for failure-loop detection.
                    self._record_tool_result(state, result, tc.name, tc)
            return IterationResult(
                should_continue=True, exit_reason=TurnExitReason.TOOL_CONTINUE
            )
        else:
            self._invalid_tool_retries = 0

        for tc in response.tool_calls:
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

        _tool_ctx = ToolContext(
            session_id=make_session_key(self.channel, self.chat_id) or "",
            workspace=str(self.loop.workspace),
            permission_mode=PermissionMode.AUTO,
            tool_permission_context=ToolPermissionContext(mode=PermissionMode.AUTO),
            is_non_interactive=False,
            channel=self.channel,
            chat_id=self.chat_id,
            message_id=self.message_id,
            report_progress=_report_progress,
        )

        for tc in response.tool_calls:
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
        for tc in response.tool_calls:
            self._emit(
                EventType.TOOL_CALLED,
                {"tool": tc.name, "call_id": tc.id},
            )

        results = await asyncio.gather(
            *(
                self.loop.tools.execute(tc.name, tc.arguments, context=_tool_ctx)
                for tc in response.tool_calls
            ),
            return_exceptions=True,
        )
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
            is_error = isinstance(result, str) and result.startswith("Error:")
            self._emit(
                EventType.TOOL_COMPLETED,
                {"tool": tool_call.name, "call_id": tool_call.id,
                 "ok": not is_error},
            )

            if self.on_tool_complete:
                error_msg = None
                summary = None
                if isinstance(result, str):
                    if result.startswith("Error:"):
                        error_msg = result
                    else:
                        summary = result[:200] if len(result) > 200 else result
                try:
                    await self.on_tool_complete(tool_call.id, tool_call.name, summary, error_msg)
                except Exception as e:
                    logger.debug("on_tool_complete callback failed: {}", e)
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
        # accepting the final response. Mirrors Hermes's verify-on-stop:
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
            # Wipe file_mutations so the nudge does not re-fire immediately
            # on the next final-response attempt without new edits. The
            # model must edit again (or call verification) to clear the gate.
            state.file_mutations = []
            return IterationResult(
                should_continue=True,
                exit_reason=TurnExitReason.TOOL_CONTINUE,
            )

        verifier_footer = self._maybe_inject_mutation_verifier_footer(
            state, clean or ""
        )
        if verifier_footer:
            saved_content = (clean or "") + "\n\n---\n" + verifier_footer
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
            if state.failed_methods:
                recent = state.failed_methods[-10:]
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
