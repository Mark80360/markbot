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
from dataclasses import dataclass, field
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


@dataclass
class IterationResult:
    should_break: bool = False
    should_continue: bool = False


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
        self.session_key = session_key or f"{channel}:{chat_id}"
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
            if result.should_break:
                break

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
                session_key = f"{self.channel}:{self.chat_id}"
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
            # Write to the LLM response cache if the request was cacheable.
            self._maybe_write_response_cache(state, tool_defs, response, attempts)
            return None
        except Exception as llm_exc:
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

    async def _handle_llm_error(
        self, state: LoopState, tool_defs: list[dict], llm_exc: Exception
    ) -> IterationResult:
        if not is_prompt_too_long_error(llm_exc):
            logger.error("LLM call failed: {}", llm_exc)
            state.final_content = (
                f"Sorry, I encountered an error calling the AI model: {str(llm_exc)[:200]}"
            )
            return IterationResult(should_break=True)

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
            return IterationResult(should_break=True)

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
            return None
        except Exception as retry_exc:
            logger.error(
                "LLM call failed after reactive compaction: {}", retry_exc
            )
            state.final_content = (
                "Sorry, the conversation context became too large and compaction was unable "
                "to reduce it enough. Please start a new session or simplify your request."
            )
            return IterationResult(should_break=True)

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
                return tec.lookup_or_compute(
                    messages_revision=rev,
                    system_prompt=None,
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
            return IterationResult(should_break=True)

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
                return IterationResult(should_break=True)

            error_msg = (
                f"Tool '{invalid_name}' does not exist. "
                f"Available tools: {available}. "
                f"Please use only the tools listed above."
            )
            for tc in response.tool_calls:
                if not self.loop.tools.has(tc.name):
                    state.messages = self.loop.context.add_tool_result(
                        state.messages, tc.id, tc.name, error_msg
                    )
                else:
                    result = await self.loop.tools.execute(tc.name, tc.arguments, context=ToolContext(
                        session_id=f"{self.channel}:{self.chat_id}",
                        workspace=str(self.loop.workspace),
                        permission_mode=PermissionMode.AUTO,
                        tool_permission_context=ToolPermissionContext(mode=PermissionMode.AUTO),
                        is_non_interactive=False,
                        channel=self.channel,
                        chat_id=self.chat_id,
                        message_id=self.message_id,
                    ))
                    state.messages = self.loop.context.add_tool_result(
                        state.messages, tc.id, tc.name, result
                    )
            return IterationResult(should_continue=True)
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

            results = self.loop.guardrail_manager.check_all(tc.name, tc.arguments)
            for gr in results:
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
            session_id=f"{self.channel}:{self.chat_id}",
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
            state.messages = self.loop.context.add_tool_result(
                state.messages, tool_call.id, tool_call.name, result
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

        return IterationResult(should_continue=True)

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
            return IterationResult(should_break=True)
        if response.finish_reason == "content_filter":
            logger.warning(
                "LLM returned content_filter (content safety block), "
                "not saving filtered response to history"
            )
            state.final_content = (
                "抱歉，模型的内容安全策略阻止了本次回复，请尝试换一种方式描述您的请求。"
            )
            return IterationResult(should_break=True)
        state.messages = self.loop.context.add_assistant_message(
            state.messages,
            clean,
            reasoning_content=response.reasoning_content,
            thinking_blocks=response.thinking_blocks,
        )
        state.final_content = clean
        logger.info("Final response captured, length={}", len(clean or ""))

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
            return IterationResult(should_continue=True)

        return IterationResult(should_break=True)

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
        elif state.final_content is None and state.iteration >= self.loop.max_iterations:
            logger.warning(
                "Max iterations ({}) reached", self.loop.max_iterations
            )
            state.final_content = (
                f"I reached the maximum number of tool call iterations "
                f"({self.loop.max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            )

        if self.on_stream and self.on_stream_end and not state.stream_finalized:
            if state.final_content:
                await self.on_stream(state.final_content)
            await self.on_stream_end(resuming=False)
            self._stream_filter.reset()
            state.stream_finalized = True

        logger.info(
            "Loop ended: iterations={}, tools_used={}, has_final_content={}",
            state.iteration,
            len(state.tools_used),
            state.final_content is not None,
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
            )
            handoff_manager.save(handoff)
            logger.info(
                "Session handoff saved: {} tasks, {} decisions",
                len(handoff.active_tasks),
                len(handoff.key_decisions),
            )
        except Exception as e:
            logger.warning("Failed to generate session handoff: {}", e)
