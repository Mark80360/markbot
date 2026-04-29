"""Agent loop: the core processing engine.

Refactored to use new core types and skill system inspired by MarkBot.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from markbot.agent.compact import CompactAction, CompactionConfig, MultiLevelCompactor
from markbot.agent.context import ContextBuilder
from markbot.agent.cost import BudgetExceededError, CostTracker, PricingTable
from markbot.agent.mcp.manager import McpManager
from markbot.agent.stream import StreamFilter
from markbot.agent.tokens import token_count_with_estimation
from markbot.agent.tool_binder import ToolBinder
from markbot.bus.events import InboundMessage, OutboundMessage
from markbot.bus.queue import MessageBus
from markbot.cli.slash_commands import CommandContext, CommandRouter, register_builtin_commands
from markbot.memory.daily_log import DailyLogManager
from markbot.agent.hooks.bootstrap import BootstrapHook
from markbot.agent.hooks.compaction import MemoryCompactionHook
from markbot.memory.manager import ReMeLightMemoryManager
from markbot.agent.services.interaction import InteractionLogger
from markbot.agent.pipeline.engine import MessagePipeline, ProcessContext
from markbot.agent.pipeline.middleware import (
    MemoryLifecycleMiddleware,
    QuestionResponseMiddleware,
)
from markbot.agent.services.executor import ToolExecutor
from markbot.skills import SkillRegistry
from markbot.skills.core.guardrail import SkillGuardrailManager
from markbot.session.session import SessionManager
from markbot.agent.subagent import SubagentManager
from markbot.tools.message import MessageTool
from markbot.tools.question import AskUserQuestionTool
from markbot.tools.registry import ToolRegistry
from markbot.types.permission import PermissionMode, ToolPermissionContext
from markbot.types.tool import ToolContext
from markbot.utils.helpers import strip_think

if TYPE_CHECKING:
    from markbot.config.schema import (
        ChannelsConfig,
        ExecToolConfig,
        FilesystemToolConfig,
        WebSearchConfig,
    )
    from markbot.schedule.cron import CronService


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    def __init__(
        self,
        bus: MessageBus,
        fallback_manager,
        config=None,
        workspace: Path | None = None,
        max_iterations: int = 40,
        context_window_tokens: int = 65_536,
        web_search_config: WebSearchConfig | None = None,
        web_proxy: str | None = None,
        exec_config: ExecToolConfig | None = None,
        filesystem_config: FilesystemToolConfig | None = None,
        memory_config=None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        timezone: str | None = None,
        compaction_config: CompactionConfig | None = None,
        max_budget_usd: float | None = None,
        warn_threshold_usd: float = 0.5,
        budget_config=None,
    ):
        self.bus = bus
        self.channels_config = channels_config
        self.fallback_manager = fallback_manager
        self.config = config
        self.workspace = workspace

        # 从 config 获取主模型信息
        self._primary_provider_config = None
        if config and config.primary_model_ref:
            primary_provider, primary_model = config.resolve_model(config.primary_model_ref)
            self.model = primary_model.name
            self._primary_provider_config = primary_provider
        else:
            self.model = "unknown"

        self.max_iterations = max_iterations
        self.context_window_tokens = context_window_tokens
        self.web_search_config = web_search_config or WebSearchConfig()
        self.web_proxy = web_proxy
        self.exec_config = exec_config or ExecToolConfig()
        self.filesystem_config = filesystem_config or FilesystemToolConfig()

        from markbot.config.schema import MemoryToolsConfig

        self.memory_config = memory_config or MemoryToolsConfig()

        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self._start_time = time.time()
        self._last_usage: dict[str, int] = {}
        self._last_context_tokens: int = 0

        _compaction_cfg = compaction_config or CompactionConfig()
        self.compactor = MultiLevelCompactor(
            fallback_manager=fallback_manager, config=_compaction_cfg
        )
        _pricing: PricingTable | None = None
        if budget_config and getattr(budget_config, "custom_pricing", None):
            from markbot.agent.cost import ModelPricing as _MP

            _custom = {k: _MP(**v) for k, v in budget_config.custom_pricing.items()}
            _pricing = PricingTable(custom=_custom)
        self.cost_tracker = CostTracker(
            max_budget_usd=max_budget_usd,
            warn_threshold_usd=warn_threshold_usd,
            pricing=_pricing,
        )

        # Initialize tool registry
        self.tools = ToolRegistry()

        # Initialize skill registry (new system)
        self.skill_registry = SkillRegistry(workspace, tool_registry=self.tools)
        self.skill_registry.load_all()

        # Skill execution guardrail manager — validates tool calls against
        # active skill constraints. SkillTool auto-activates its guardrail
        # on execution.
        self.guardrail_manager = SkillGuardrailManager(context="main")

        self.sessions = session_manager or SessionManager(workspace)
        self.subagents = SubagentManager(
            fallback_manager=fallback_manager,
            config=config,
            workspace=workspace,
            bus=bus,
            model=self.model,
            web_search_config=self.web_search_config,
            web_proxy=web_proxy,
            exec_config=self.exec_config,
            filesystem_config=self.filesystem_config,
            restrict_to_workspace=restrict_to_workspace,
            cost_tracker=self.cost_tracker,
        )

        self._running = False
        self.mcp = McpManager(mcp_servers)
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._session_locks: dict[str, asyncio.Lock] = {}
        _max = int(os.environ.get("MARKBOT_MAX_CONCURRENT_REQUESTS", "3"))
        self._concurrency_gate: asyncio.Semaphore | None = (
            asyncio.Semaphore(_max) if _max > 0 else None
        )

        memory_cfg = self.memory_config
        embedding_config = {}
        if memory_cfg:
            embedding_config = {
                "backend": getattr(memory_cfg, "embedding_backend", "openai"),
                "api_key": getattr(memory_cfg, "embedding_api_key", ""),
                "base_url": getattr(memory_cfg, "embedding_base_url", ""),
                "model_name": getattr(memory_cfg, "embedding_model_name", ""),
            }

        llm_config = {
            "backend": "openai",
            "api_key": getattr(self._primary_provider_config, 'api_key', '') or "",
            "base_url": getattr(self._primary_provider_config, 'api_base', '') or "",
            "model_name": self.model,
        }

        self.memory_manager = ReMeLightMemoryManager(
            working_dir=str(workspace),
            agent_id="markbot",
            fallback_manager=fallback_manager,
            model=self.model,
            embedding_config=embedding_config,
            llm_config=llm_config,
            language="zh",
            timezone=timezone,
            context_compact_enabled=getattr(memory_cfg, "context_compact_enabled", True),
            memory_compact_ratio=getattr(memory_cfg, "memory_compact_ratio", 0.75),
            memory_reserve_ratio=getattr(memory_cfg, "memory_reserve_ratio", 0.1),
            compact_with_thinking_block=True,
            memory_summary_enabled=getattr(memory_cfg, "memory_summary_enabled", True),
            force_memory_search=getattr(memory_cfg, "force_memory_search", False),
            force_max_results=getattr(memory_cfg, "force_max_results", 1),
            force_min_score=getattr(memory_cfg, "force_min_score", 0.3),
        )
        logger.info("Memory manager initialized (ReMeLight)")

        self.bootstrap_hook = BootstrapHook(
            working_dir=workspace,
            language="zh",
        )

        memory_compact_threshold = int(context_window_tokens * 0.75)
        self.compaction_hook = MemoryCompactionHook(
            memory_manager=self.memory_manager,
            memory_compact_threshold=memory_compact_threshold,
            memory_compact_reserve=getattr(memory_cfg, "memory_compact_reserve", 10000),
            context_compact_enabled=getattr(memory_cfg, "context_compact_enabled", True),
            memory_summary_enabled=getattr(memory_cfg, "memory_summary_enabled", True),
        )

        self.context = ContextBuilder(
            workspace,
            timezone=timezone,
            tool_registry=self.tools,
            skill_registry=self.skill_registry,
            memory_manager=self.memory_manager,
        )
        _allowed_dir = workspace if restrict_to_workspace else None
        binder = ToolBinder(
            self.tools,
            workspace=workspace,
            allowed_dir=_allowed_dir,
            web_search_config=self.web_search_config,
            web_proxy=web_proxy,
            exec_config=self.exec_config,
            filesystem_config=self.filesystem_config,
            cron_service=cron_service,
            subagent_manager=self.subagents,
            memory_manager=self.memory_manager,
            skill_registry=self.skill_registry,
            timezone=timezone,
            publish_outbound=self.bus.publish_outbound,
        )
        binder.register_all()
        self._memory_search_tool = binder.memory_search_tool
        self.question_tool = binder.question_tool

        logger.info(f"Agent has {len(self.tools)} tools available")
        self.commands = CommandRouter()
        register_builtin_commands(self.commands)

        # Initialize decoupled services (extracted from monolithic loop)
        self.tool_executor = ToolExecutor(self.tools)
        self._setup_pipeline()

    def _setup_pipeline(self) -> None:
        """Initialize the message processing pipeline with middleware."""
        self.pipeline = MessagePipeline()
        self.pipeline.use(QuestionResponseMiddleware(get_question_tool=lambda: self.question_tool))
        self._daily_log = DailyLogManager(workspace=self.workspace)
        self._interaction_log = InteractionLogger()
        if hasattr(self.memory_manager, "_daily_log_manager"):
            self.memory_manager._daily_log_manager = self._daily_log
        self.pipeline.use(
            MemoryLifecycleMiddleware(
                memory_manager=self.memory_manager,
                daily_log=self._daily_log,
                session_manager=self.sessions,
            )
        )

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        await self.mcp.connect(self.tools)

    def _set_tool_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Update context for all tools that need routing info."""
        for name in ("message", "spawn", "cron"):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    tool.set_context(channel, chat_id, *([message_id] if name == "message" else []))
        if hasattr(self, "question_tool") and self.question_tool:
            self.question_tool.set_context(channel, chat_id)
        if todo_tool := self.tools.get("todo"):
            if hasattr(todo_tool, "set_session"):
                todo_tool.set_session(f"{channel}:{chat_id}")
        if self._memory_search_tool:
            self._memory_search_tool.set_session_context(channel, chat_id)

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""

        def _fmt(tc):
            args = (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments) or {}
            val = next(iter(args.values()), None) if isinstance(args, dict) else None
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val[:40]}…")' if len(val) > 40 else f'{tc.name}("{val}")'

        return ", ".join(_fmt(tc) for tc in tool_calls)

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        *,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
    ) -> tuple[str | None, list[str], list[dict]]:
        """Run the agent iteration loop.

        *on_stream*: called with each content delta during streaming.
        *on_stream_end(resuming)*: called when a streaming session finishes.
        ``resuming=True`` means tool calls follow (spinner should restart);
        ``resuming=False`` means this is the final response.
        """
        messages = initial_messages
        _initial_count = len(initial_messages)
        iteration = 0
        final_content = None
        tools_used: list[str] = []
        _budget_exceeded = False
        _new_msg_start = _initial_count

        logger.info(
            "[AgentLoop] Starting agent loop with {} initial messages", len(initial_messages)
        )

        # Wrap on_stream with stateful think-tag filter so downstream
        # consumers (CLI, channels) never see  <think>  blocks.
        _stream_filter = StreamFilter(on_stream)

        while iteration < self.max_iterations:
            iteration += 1
            logger.info(
                "[AgentLoop] === Iteration {}/{} (channel={}, chat_id={}) ===",
                iteration,
                self.max_iterations,
                channel,
                chat_id,
            )
            logger.info("[AgentLoop] Current message count: {}", len(messages))

            current_tokens = token_count_with_estimation(messages)
            logger.debug("[AgentLoop] Estimated context tokens: {}", current_tokens)

            messages, compact_result = await self.compactor.maybe_compact(
                messages, current_tokens, self.context_window_tokens
            )
            if compact_result.action != CompactAction.NONE:
                logger.info(
                    "[AgentLoop] Compaction applied: action={}, "
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
                current_tokens = compact_result.tokens_after
                if len(messages) < _new_msg_start:
                    _new_msg_start = self._recalc_new_msg_start_after_compact(
                        messages, _initial_count, _new_msg_start,
                    )

            if iteration == 1:
                if self._memory_search_tool:
                    try:
                        user_text = ""
                        for m in reversed(messages):
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
                        if user_text:
                            forced_ctx = await self._memory_search_tool.get_forced_context(user_text)
                            if forced_ctx:
                                sys_idx = next(
                                    (i for i, m in enumerate(messages) if m.get("role") == "system"),
                                    None,
                                )
                                if sys_idx is not None:
                                    messages[sys_idx]["content"] = (
                                        messages[sys_idx]["content"] + "\n\n" + forced_ctx
                                    )
                                    logger.info("[AgentLoop] Forced memory search context injected")
                    except Exception as e:
                        logger.warning(f"[AgentLoop] Forced memory search failed: {e}")

            tool_defs = self.tools.get_definitions()
            logger.debug(
                "[AgentLoop] Available tools: {}",
                [t.get("function", {}).get("name") for t in tool_defs[:5]],
            )

            messages = self._strip_orphan_tool_results(messages)

            if current_tokens > self.context_window_tokens * 0.6:
                _skip_context_compact = compact_result.action in (
                    CompactAction.AUTO_COMPACT,
                    CompactAction.HISTORY_SNIP,
                )
                system_msg = next(
                    (m.get("content", "") for m in messages if m.get("role") == "system"), ""
                )
                new_summary = await self.compaction_hook(
                    messages=messages,
                    system_prompt=system_msg,
                    skip_context_compact=_skip_context_compact,
                    channel=channel,
                    chat_id=chat_id,
                )
                if new_summary:
                    logger.info("[AgentLoop] Memory compaction applied, summary updated")

            if on_stream:
                logger.info("[AgentLoop] Calling LLM with streaming...")
                response, attempts = await self.fallback_manager.chat_stream_with_fallback(
                    messages=messages,
                    tools=tool_defs,
                    on_content_delta=_stream_filter,
                )
            else:
                logger.info("[AgentLoop] Calling LLM without streaming...")
                response, attempts = await self.fallback_manager.chat_with_fallback(
                    messages=messages,
                    tools=tool_defs,
                )

            logger.info(
                "[AgentLoop] LLM response received: finish_reason={}, has_tool_calls={}, content_length={}",
                response.finish_reason,
                response.has_tool_calls,
                len(response.content or ""),
            )

            # Use the actual model from the successful fallback attempt for correct billing
            _actual_model = self.model
            for _a in reversed(attempts):
                if _a.success and _a.model:
                    _actual_model = _a.model.name
                    break

            self._interaction_log.log_interaction(
                iteration=iteration,
                messages=messages,
                tool_defs=tool_defs,
                response=response,
                model=_actual_model,
                channel=channel,
                chat_id=chat_id,
                tokens_before=current_tokens,
            )

            usage = response.usage or {}
            self._last_usage = {
                "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                "total_tokens": int(usage.get("total_tokens", 0) or 0),
                "cache_creation_input_tokens": int(
                    usage.get("cache_creation_input_tokens", 0) or 0
                ),
                "cache_read_input_tokens": int(usage.get("cache_read_input_tokens", 0) or 0),
            }
            self._last_context_tokens = current_tokens

            try:
                call_cost = self.cost_tracker.update_from_response(response, model=_actual_model)
                if call_cost > 0:
                    logger.debug("[AgentLoop] API cost this call: ${:.6f}", call_cost)
            except BudgetExceededError as exc:
                logger.error("[AgentLoop] Budget exceeded: {}. Stopping loop early.", exc)
                _budget_exceeded = True
                break

            if response.has_tool_calls:
                logger.info(
                    "[AgentLoop] LLM requested {} tool call(s): {}",
                    len(response.tool_calls),
                    [tc.name for tc in response.tool_calls],
                )

                if on_stream and on_stream_end:
                    await on_stream_end(resuming=True)
                    _stream_filter.reset()

                if on_progress:
                    if not on_stream:
                        thought = strip_think(response.content) or None
                        if thought:
                            await on_progress(thought)
                    tool_hint = self._tool_hint(response.tool_calls)
                    tool_hint = strip_think(tool_hint) or None
                    await on_progress(tool_hint, tool_hint=True)

                tool_call_dicts = [tc.to_openai_tool_call() for tc in response.tool_calls]
                messages = self.context.add_assistant_message(
                    messages,
                    response.content,
                    tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                logger.debug(
                    "[AgentLoop] Added assistant message with tool calls, total messages: {}",
                    len(messages),
                )

                for tc in response.tool_calls:
                    tools_used.append(tc.name)
                    args_str = json.dumps(tc.arguments, ensure_ascii=False)
                    safe_args = args_str[:200].replace("{", "{{").replace("}", "}}")
                    logger.info("Tool call: {}({})", tc.name, safe_args)

                # Re-bind tool context right before execution so that
                # concurrent sessions don't clobber each other's routing.
                self._set_tool_context(channel, chat_id, message_id)

                # Guardrail check: validate tool calls against active skill constraints.
                # Also auto-activate guardrails when a skill tool is called.
                for tc in response.tool_calls:
                    # Auto-activate guardrail for skill script calls
                    if "." in tc.name:
                        skill_name = tc.name.split(".", 1)[0]
                        skill_def = self.skill_registry.get(skill_name)
                        if skill_def and skill_name not in self.guardrail_manager.active_skills:
                            self.guardrail_manager.start_guarding(skill_def)
                            logger.info("Guardrail activated for skill: {}", skill_name)

                    violations = self.guardrail_manager.check_all(tc.name, tc.arguments)
                    for v in violations:
                        logger.warning(
                            "Guardrail violation [{}] {} (tool: {}, skill: {})",
                            v.severity.upper(),
                            v.message,
                            tc.name,
                            getattr(v, 'tool_name', 'unknown'),
                        )

                # Execute all tool calls concurrently — the LLM batches
                # independent calls in a single response on purpose.
                # return_exceptions=True ensures all results are collected
                # even if one tool is cancelled or raises BaseException.
                logger.info("[AgentLoop] Executing {} tool calls...", len(response.tool_calls))

                _tool_ctx = ToolContext(
                    session_id=f"{channel}:{chat_id}",
                    workspace=str(self.workspace),
                    permission_mode=PermissionMode.AUTO,
                    tool_permission_context=ToolPermissionContext(mode=PermissionMode.AUTO),
                    is_non_interactive=False,
                    channel=channel,
                    chat_id=chat_id,
                    message_id=message_id,
                )
                results = await asyncio.gather(
                    *(
                        self.tools.execute(tc.name, tc.arguments, context=_tool_ctx)
                        for tc in response.tool_calls
                    ),
                    return_exceptions=True,
                )
                logger.info("[AgentLoop] Tool execution completed, {} results", len(results))

                for idx, (tool_call, result) in enumerate(zip(response.tool_calls, results)):
                    if isinstance(result, BaseException):
                        logger.error(
                            "[AgentLoop] Tool {} failed with exception: {}", tool_call.name, result
                        )
                        result = f"Error: {type(result).__name__}: {result}"
                    else:
                        result_preview = (
                            str(result)[:100] + "..." if len(str(result)) > 100 else str(result)
                        )
                        safe_preview = result_preview.replace("{", "{{").replace("}", "}}")
                        logger.info("[AgentLoop] Tool {} result: {}", tool_call.name, safe_preview)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
                logger.info(
                    "[AgentLoop] Added {} tool results to messages, continue to next iteration",
                    len(results),
                )
            else:
                logger.info("[AgentLoop] LLM returned final response (no tool calls)")
                if on_stream and on_stream_end:
                    await on_stream_end(resuming=False)
                    _stream_filter.reset()

                clean = strip_think(response.content) or None
                if response.finish_reason == "error":
                    safe_clean = (clean or "")[:200].replace("{", "{{").replace("}", "}}")
                    logger.error("[AgentLoop] LLM returned error finish_reason: {}", safe_clean)
                    final_content = clean or "Sorry, I encountered an error calling the AI model."
                    break
                messages = self.context.add_assistant_message(
                    messages,
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                final_content = clean
                logger.info("[AgentLoop] Final response captured, length={}", len(clean or ""))
                break

        if final_content is None and _budget_exceeded:
            cost_summary = self.cost_tracker.get_summary()
            final_content = (
                f"Budget limit reached (${cost_summary['total_cost_usd']:.4f} / "
                f"${cost_summary['budget_limit_usd']:.2f}). "
                "I've stopped to avoid exceeding the spending limit. "
                "You can increase the budget or break the task into smaller steps."
            )
        elif final_content is None and iteration >= self.max_iterations:
            logger.warning("[AgentLoop] Max iterations ({}) reached", self.max_iterations)
            final_content = (
                f"I reached the maximum number of tool call iterations ({self.max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            )

        logger.info(
            "[AgentLoop] Loop ended: iterations={}, tools_used={}, has_final_content={}",
            iteration,
            len(tools_used),
            final_content is not None,
        )

        token_summary = self.cost_tracker.get_token_summary()
        logger.info(
            "[AgentLoop] Token usage - Total: input={}, output={}, cache_creation={}, cache_read={}",
            token_summary["total"]["input_tokens"],
            token_summary["total"]["output_tokens"],
            token_summary["total"]["cache_creation_input_tokens"],
            token_summary["total"]["cache_read_input_tokens"],
        )

        cost_summary = self.cost_tracker.get_summary()
        logger.info(
            "[AgentLoop] Cost - total=${:.6f}, calls={}, budget={}{}",
            cost_summary["total_cost_usd"],
            cost_summary["total_api_calls"],
            f"${cost_summary['budget_limit_usd']}"
            if cost_summary["budget_limit_usd"]
            else "unlimited",
            ", OVER BUDGET" if cost_summary["over_budget"] else "",
        )

        return final_content, tools_used, messages, _new_msg_start

    @staticmethod
    def _recalc_new_msg_start_after_compact(
        messages: list[dict],
        _initial_count: int,
        _new_msg_start: int,
    ) -> int:
        """Recalculate the new-message start index after compaction reshaped messages.

        Uses the count of messages added since the last save boundary
        (``len(messages) - _new_msg_start``) to derive the new boundary.
        This is more correct than the original ``_initial_count - _new_msg_start``
        which becomes stale after repeated compactions within one turn.
        """
        new_msgs = len(messages) - _new_msg_start

        if new_msgs <= 0:
            # No new messages were produced by the loop before compaction.
            # Find the current-turn user message to use as the save boundary.
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    return i + 1
            # User message was consumed by compaction (extremely long turn).
            # Default to 1 so save_turn skips the system prompt and preserves
            # whatever remains, avoiding total data loss.
            return 1

        # Adjust the boundary by the same number of new-message slots.
        target = min(new_msgs, len(messages))
        candidate = max(0, len(messages) - target)

        for i in range(candidate, len(messages)):
            if messages[i].get("role") == "user":
                return i + 1

        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                return i + 1

        return 1

    @staticmethod
    def _strip_orphan_tool_results(messages: list[dict]) -> list[dict]:
        """Remove orphan tool_result messages before sending to LLM.

        Delegates to Session._strip_orphan_tool_results to avoid duplication.
        """
        from markbot.session.session import Session

        return Session._strip_orphan_tool_results(messages)

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        await self._connect_mcp()
        try:
            await self.memory_manager.start()
            logger.info("Memory manager started")
        except Exception as e:
            logger.warning(f"Memory manager start failed: {e}")
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                # Preserve real task cancellation so shutdown can complete cleanly.
                # Only ignore non-task CancelledError signals that may leak from integrations.
                if not self._running or asyncio.current_task().cancelling():
                    raise
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            raw = msg.content.strip()
            if self.commands.is_priority(raw):
                ctx = CommandContext(msg=msg, session=None, key=msg.session_key, raw=raw, loop=self)
                result = await self.commands.dispatch_priority(ctx)
                if result:
                    await self.bus.publish_outbound(result)
                continue
            task = asyncio.create_task(self._dispatch(msg))
            self._active_tasks.setdefault(msg.session_key, []).append(task)

            def _cleanup_task(t: asyncio.Task, k: str = msg.session_key) -> None:
                tasks = self._active_tasks.get(k)
                if tasks is None:
                    self._session_locks.pop(k, None)
                    return
                try:
                    tasks.remove(t)
                except ValueError:
                    pass
                if not tasks:
                    self._active_tasks.pop(k, None)
                    self._session_locks.pop(k, None)

            task.add_done_callback(_cleanup_task)

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message: per-session serial, cross-session concurrent."""
        lock = self._session_locks.setdefault(msg.session_key, asyncio.Lock())
        gate = self._concurrency_gate or nullcontext()
        async with lock, gate:
            try:
                on_stream = on_stream_end = None
                if msg.metadata.get("_wants_stream"):

                    async def on_stream(delta: str) -> None:
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content=delta,
                                metadata={"_stream_delta": True},
                            )
                        )

                    async def on_stream_end(*, resuming: bool = False) -> None:
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content="",
                                metadata={
                                    "_stream_end": True,
                                    "_resuming": resuming,
                                    "message_id": msg.metadata.get("message_id")
                                    if msg.metadata
                                    else None,
                                    "reaction_id": msg.metadata.get("reaction_id")
                                    if msg.metadata
                                    else None,
                                },
                            )
                        )

                response = await self._process_message(
                    msg,
                    on_stream=on_stream,
                    on_stream_end=on_stream_end,
                )
                if response is not None:
                    await self.bus.publish_outbound(response)
                else:
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content="",
                            metadata=dict(msg.metadata or {}),
                        )
                    )
            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception as e:
                logger.exception("Error processing message for session {}", msg.session_key)
                error_detail = str(e).strip()
                safe_detail = error_detail[:300].replace("{", "{{").replace("}", "}}")
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=f"Sorry, I encountered an error: {safe_detail}",
                        metadata=dict(msg.metadata or {}),
                    )
                )

    async def close_mcp(self) -> None:
        """Drain pending background tasks, then close MCP connections."""
        await self.mcp.close()

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        try:
            worker = getattr(self.memory_manager, "_worker_task", None)
            if worker and not worker.done():
                worker.cancel()
        except Exception:
            pass
        logger.info("Agent loop stopping")

    def get_cost_summary(self) -> dict[str, Any]:
        """Return cost tracking summary including per-model breakdown."""
        return self.cost_tracker.get_summary()

    @property
    def total_cost_usd(self) -> float:
        """Total cost in USD for this session."""
        return self.cost_tracker.total_cost

    @property
    def is_over_budget(self) -> bool:
        """Check if budget has been exceeded."""
        return self.cost_tracker.is_over_budget()

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        channel = msg.channel
        chat_id = msg.chat_id

        if channel == "system":
            channel = msg.origin_channel or ("cli" if ":" not in chat_id else chat_id.split(":", 1)[0])
            chat_id = msg.origin_chat_id or chat_id

        key = session_key or msg.session_key
        if msg.channel == "system":
            key = f"{channel}:{chat_id}"

        pctx = ProcessContext(
            msg=msg,
            session_key=key,
            channel=channel,
            chat_id=chat_id,
        )

        async def _handler(ctx: ProcessContext) -> OutboundMessage | None:
            return await self._handle_message(
                ctx,
                on_progress=on_progress,
                on_stream=on_stream,
                on_stream_end=on_stream_end,
            )

        return await self.pipeline.process(pctx, _handler)

    async def _handle_message(
        self,
        ctx: ProcessContext,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Core message handling logic, executed inside the pipeline."""
        await self._connect_mcp()
        msg = ctx.msg
        channel = ctx.channel
        chat_id = ctx.chat_id
        key = ctx.session_key

        if channel == "system":
            logger.info("Processing system message from {}", msg.sender_id)
            session = self.sessions.get_or_create(key)
            ctx.session = session

            self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"))
            history = session.get_history(max_messages=200)
            current_role = "assistant" if msg.sender_id == "subagent" else "user"
            messages = await self.context.build_messages(
                history=history,
                current_message=msg.content,
                channel=channel,
                chat_id=chat_id,
                current_role=current_role,
                session_key=key,
                session=session,
            )
            try:
                final_content, _, all_msgs, _new_start = await self._run_agent_loop(
                    messages,
                    channel=channel,
                    chat_id=chat_id,
                    message_id=msg.metadata.get("message_id"),
                )
                self.tool_executor.save_turn(session, all_msgs, _new_start)
            except Exception:
                raise

            return OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=final_content or "Background task completed.",
            )

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", channel, msg.sender_id, preview)

        logger.info(
            "[AgentLoop] _process_message started for channel={}, chat_id={}, content={}...",
            channel,
            chat_id,
            msg.content[:50],
        )

        session = self.sessions.get_or_create(key)
        ctx.session = session

        logger.debug("[AgentLoop] Starting memory session for key={}", key)

        # Slash commands
        raw = msg.content.strip()
        cmd_ctx = CommandContext(msg=msg, session=session, key=key, raw=raw, loop=self)
        if result := await self.commands.dispatch(cmd_ctx):
            # Propagate message_id and reaction_id to command responses so channels
            # (e.g. Feishu) can clean up typing indicators / reactions.
            result.metadata.setdefault("message_id", msg.metadata.get("message_id"))
            result.metadata.setdefault("reaction_id", msg.metadata.get("reaction_id"))
            return result

        self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"))
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        history = session.get_history(max_messages=200)
        initial_messages = await self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=channel,
            chat_id=chat_id,
            session_key=key,
            session=session,
        )

        # Bootstrap hook: prepend guidance on first interaction
        bootstrap_guidance = self.bootstrap_hook.check_and_inject(initial_messages)
        if bootstrap_guidance:
            first_user = next((i for i, m in enumerate(initial_messages) if m.get("role") == "user"), None)
            if first_user is not None:
                original = initial_messages[first_user]
                initial_messages[first_user] = {
                    **original,
                    "content": bootstrap_guidance + original.get("content", ""),
                }

        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=channel,
                    chat_id=chat_id,
                    content=content,
                    metadata=meta,
                )
            )

        logger.info("[AgentLoop] Calling _run_agent_loop...")
        try:
            final_content, tools_used, all_msgs, _new_start = await self._run_agent_loop(
                initial_messages,
                on_progress=on_progress or _bus_progress,
                on_stream=on_stream,
                on_stream_end=on_stream_end,
                channel=channel,
                chat_id=chat_id,
                message_id=msg.metadata.get("message_id"),
            )

            logger.info(
                "[AgentLoop] _run_agent_loop returned: final_content_length={}",
                len(final_content or ""),
            )

            if final_content is None:
                final_content = "I've completed processing but have no response to give."
                logger.warning("[AgentLoop] final_content was None, using default message")

            logger.info("[AgentLoop] Saving session with {} messages (new_msg_start={})", len(all_msgs), _new_start)
            self.tool_executor.save_turn(session, all_msgs, _new_start)
        except Exception as e:
            logger.error("[AgentLoop] _run_agent_loop failed with exception: {}", e)
            raise

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            logger.info("[AgentLoop] MessageTool sent in turn, returning None")
            return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        safe_preview = preview.replace("{", "{{").replace("}", "}}")
        logger.info("[AgentLoop] Response to {}:{}: {}", channel, msg.sender_id, safe_preview)

        meta = dict(msg.metadata or {})
        if on_stream is not None:
            meta["_streamed"] = True

        logger.info("[AgentLoop] _process_message completed, returning response")
        return OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=final_content,
            metadata=meta,
        )

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a message directly and return the outbound payload."""
        await self._connect_mcp()
        # Ensure memory manager is started
        if self.memory_manager and not getattr(self.memory_manager, "_started", False):
            try:
                await self.memory_manager.start()
                logger.info("Memory manager started in process_direct")
            except Exception as e:
                logger.warning(f"Memory manager start failed in process_direct: {e}")
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
        return await self._process_message(
            msg,
            session_key=session_key,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
        )
