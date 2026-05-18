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
from markbot.agent.stream import StreamFilter
from markbot.agent.tokens import token_count_with_estimation
from markbot.session.handoff import build_handoff_from_session
from markbot.types.permission import PermissionMode, ToolPermissionContext
from markbot.types.tool import ToolContext
from markbot.utils.helpers import strip_think

if TYPE_CHECKING:
    from markbot.agent.loop import AgentLoop


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
    ) -> None:
        self.loop = loop
        self.channel = channel
        self.chat_id = chat_id
        self.message_id = message_id
        self.on_progress = on_progress
        self.on_stream = on_stream
        self.on_stream_end = on_stream_end
        self.session_key = session_key or f"{channel}:{chat_id}"
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
        state.current_tokens = token_count_with_estimation(state.messages)
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

        if memory_manager and hasattr(memory_manager, "prefetch"):
            try:
                session_id = self.session_key or ""
                prefetch_ctx = memory_manager.prefetch(user_text, session_id=session_id)
                if prefetch_ctx and prefetch_ctx.strip():
                    state.messages[sys_idx]["content"] = (
                        state.messages[sys_idx]["content"] + "\n\n" + prefetch_ctx
                    )
                    logger.info("Memory prefetch context injected")
            except Exception as e:
                logger.warning("Memory prefetch failed: {}", e)

        if memory_manager and hasattr(memory_manager, "get_memory_context"):
            try:
                memory_ctx = memory_manager.get_memory_context(
                    query=user_text,
                    session_key=self.session_key,
                )
                if memory_ctx and memory_ctx.strip():
                    state.messages[sys_idx]["content"] = (
                        state.messages[sys_idx]["content"] + "\n\n" + memory_ctx
                    )
                    logger.info("Memory context injected (summary + store)")
            except Exception as e:
                logger.warning("Memory context injection failed: {}", e)

        if not mem_tool:
            return
        try:
            if user_text:
                forced_ctx = await mem_tool.get_forced_context(user_text)
                if forced_ctx:
                    state.messages[sys_idx]["content"] = (
                        state.messages[sys_idx]["content"] + "\n\n" + forced_ctx
                    )
                    logger.info("Forced memory search context injected")
        except Exception as e:
            logger.warning("Forced memory search failed: {}", e)

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
        if new_summary:
            logger.info("Memory compaction applied, summary updated")

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
                        state.messages[sys_idx]["content"] = (
                            state.messages[sys_idx]["content"] + "\n\n" + context_block
                        )
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
            return None
        except Exception as llm_exc:
            return await self._handle_llm_error(state, tool_defs, llm_exc)

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

        for tc in response.tool_calls:
            if "." in tc.name:
                skill_name = tc.name.split(".", 1)[0]
                skill_def = self.loop.skill_registry.get(skill_name)
                if skill_def and skill_name not in self.loop.guardrail_manager.active_skills:
                    self.loop.guardrail_manager.start_guarding(skill_def)
                    logger.info("Guardrail activated for skill: {}", skill_name)

            violations = self.loop.guardrail_manager.check_all(tc.name, tc.arguments)
            for v in violations:
                logger.warning(
                    "Guardrail violation [{}] {} (tool: {}, skill: {})",
                    v.severity.upper(),
                    v.message,
                    tc.name,
                    getattr(v, 'tool_name', 'unknown'),
                )

        logger.info("Executing {} tool calls...", len(response.tool_calls))

        _tool_ctx = ToolContext(
            session_id=f"{self.channel}:{self.chat_id}",
            workspace=str(self.loop.workspace),
            permission_mode=PermissionMode.AUTO,
            tool_permission_context=ToolPermissionContext(mode=PermissionMode.AUTO),
            is_non_interactive=False,
            channel=self.channel,
            chat_id=self.chat_id,
            message_id=self.message_id,
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
                result = f"Error: {type(result).__name__}: {result}"
            else:
                result_str = str(result)
                if len(result_str) > self.loop.compactor.config.tool_output_inline_chars:
                    inline, artifact_path = offload_tool_output(
                        result_str,
                        tool_call.name,
                        tool_call.id,
                        inline_limit=self.loop.compactor.config.tool_output_inline_chars,
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
        logger.info(
            "Added {} tool results to messages, continue to next iteration",
            len(results),
        )
        return IterationResult(should_continue=True)

    async def _phase_final_response(
        self, state: LoopState, response: Any
    ) -> IterationResult:
        logger.info("LLM returned final response (no tool calls)")

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
        return IterationResult(should_break=True)

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
