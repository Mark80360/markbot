"""Agent loop: the core processing engine.

Refactored to use new core types and skill system inspired by MarkBot.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import AsyncExitStack, nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from markbot.agent.context import ContextBuilder
from markbot.agent.tiered_memory import TieredMemoryManager
from markbot.agent.subagent import SubagentManager
from markbot.agent.tokens import TokenTracker
from markbot.agent.compact import MultiLevelCompactor, CompactionConfig, CompactAction
from markbot.agent.cost_tracker import CostTracker, BudgetExceededError
from markbot.agent.tools.cron import CronTool
from markbot.agent.tools.filesystem import DeleteFileTool, EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from markbot.agent.tools.message import MessageTool
from markbot.agent.tools.question import AskUserQuestionTool
from markbot.agent.tools.registry import ToolRegistry
from markbot.agent.tools.search import GlobTool, GrepTool
from markbot.agent.tools.shell import ExecTool
from markbot.agent.tools.spawn import SpawnTool
from markbot.agent.tools.subagent_progress import CheckSubagentTool, ListSubagentsTool
from markbot.agent.tools.web import WebFetchTool, WebSearchTool
from markbot.agent.tools.think import ThinkTool
from markbot.agent.tools.memory import SearchHistoryTool
from markbot.agent.tools.explore import ExploreTool
from markbot.agent.services.turn_lifecycle import TurnLifecycle
from markbot.agent.services.tool_executor import ToolExecutor
from markbot.agent.services.message_pipeline import MessagePipeline, ProcessContext
from markbot.agent.services.middleware import (
    TombstoneMiddleware,
    QuestionResponseMiddleware,
    MemoryLifecycleMiddleware,
)
from markbot.bus.events import InboundMessage, OutboundMessage
from markbot.command import CommandContext, CommandRouter, register_builtin_commands
from markbot.bus.queue import MessageBus
from markbot.providers.base import LLMProvider
from markbot.session.manager import Session, SessionManager
from markbot.core.skills import SkillRegistry
from markbot.core.skills.loader import BUILTIN_SKILLS_DIR

if TYPE_CHECKING:
    from markbot.config.schema import ChannelsConfig, ExecToolConfig, FilesystemToolConfig, WebSearchConfig
    from markbot.cron.service import CronService


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
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
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
        tiered_memory_config=None,
        compaction_config: CompactionConfig | None = None,
        max_budget_usd: float | None = None,
        warn_threshold_usd: float = 0.5,
        budget_config=None,
    ):
        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
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
        
        self.token_tracker = TokenTracker()
        _compaction_cfg = compaction_config or CompactionConfig()
        self.compactor = MultiLevelCompactor(provider=provider, config=_compaction_cfg)
        _pricing: PricingTable | None = None
        if budget_config and getattr(budget_config, 'custom_pricing', None):
            from markbot.agent.cost_tracker import ModelPricing as _MP
            _custom = {
                k: _MP(**v) for k, v in budget_config.custom_pricing.items()
            }
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

        self.sessions = session_manager or SessionManager(workspace)
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            web_search_config=self.web_search_config,
            web_proxy=web_proxy,
            exec_config=self.exec_config,
            filesystem_config=self.filesystem_config,
            restrict_to_workspace=restrict_to_workspace,
        )

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._background_tasks: list[asyncio.Task] = []
        self._session_locks: dict[str, asyncio.Lock] = {}
        # MARKBOT_MAX_CONCURRENT_REQUESTS: <=0 means unlimited; default 3.
        _max = int(os.environ.get("MARKBOT_MAX_CONCURRENT_REQUESTS", "3"))
        self._concurrency_gate: asyncio.Semaphore | None = (
            asyncio.Semaphore(_max) if _max > 0 else None
        )
        # Initialize tiered memory manager (now the only memory system)
        tm_cfg = tiered_memory_config
        self.tiered_memory = TieredMemoryManager(
            str(workspace),
            enable_cold=True if not tm_cfg else tm_cfg.enable_cold,
            session_window=tm_cfg.session_window if tm_cfg else None,
            hot_capacity=tm_cfg.hot_max_entries if tm_cfg else None,
            warm_ttl_days=tm_cfg.warm_ttl_days if tm_cfg else None,
            compact_threshold=tm_cfg.compact_threshold if tm_cfg else None,
        )
        logger.info("Tiered memory system initialized")

        # Initialize context builder with tiered_memory
        self.context = ContextBuilder(
            workspace,
            timezone=timezone,
            tool_registry=self.tools,
            skill_registry=self.skill_registry,
            tiered_memory=self.tiered_memory,
        )
        self._register_default_tools()
        # Skills are now registered via SkillRegistry
        logger.info(f"Agent has {len(self.tools)} tools available")
        self.commands = CommandRouter()
        register_builtin_commands(self.commands)

        # Initialize decoupled services (extracted from monolithic loop)
        self.turn_lifecycle = TurnLifecycle()
        self.tool_executor = ToolExecutor(self.tools)
        self._setup_pipeline()

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        extra_read = [BUILTIN_SKILLS_DIR] if allowed_dir else None
        self._register_base_tools(
            self.tools,
            allowed_dir=allowed_dir,
            extra_allowed_dirs=extra_read,
            workspace=self.workspace,
            web_search_config=self.web_search_config,
            web_proxy=self.web_proxy,
            exec_config=self.exec_config,
            filesystem_config=self.filesystem_config,
        )
        
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(SpawnTool(manager=self.subagents))
        self.tools.register(ThinkTool())
        self.tools.register(SearchHistoryTool(memory_manager=self.tiered_memory))
        self.tools.register(ExploreTool(workspace=self.workspace, allowed_dir=allowed_dir))
        self.tools.register(CheckSubagentTool(subagent_manager=self.subagents))
        self.tools.register(ListSubagentsTool(subagent_manager=self.subagents))

        self.question_tool = AskUserQuestionTool(send_callback=self.bus.publish_outbound)
        self.question_tool.set_context("", "")
        self.tools.register(self.question_tool)
        if self.cron_service:
            self.tools.register(
                CronTool(self.cron_service, default_timezone=self.context.timezone or "UTC")
            )

    @staticmethod
    def _register_base_tools(
        tools: "ToolRegistry",
        allowed_dir: Path | None = None,
        extra_allowed_dirs: list[Path] | None = None,
        workspace: Path | None = None,
        web_search_config: Any = None,
        web_proxy: str | None = None,
        exec_config: Any = None,
        filesystem_config: Any = None,
    ) -> None:
        """Register base filesystem/web/shell tools (shared between AgentLoop and SubagentManager)."""
        from markbot.agent.tools.filesystem import DeleteFileTool, EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
        from markbot.agent.tools.search import GlobTool, GrepTool
        from markbot.agent.tools.shell import ExecTool
        from markbot.agent.tools.web import WebFetchTool, WebSearchTool

        if workspace is None:
            raise ValueError("workspace must be provided")

        fs_backup_dir = getattr(filesystem_config, 'backup_dir', None) if filesystem_config else None
        fs_max_backups = getattr(filesystem_config, 'max_backups', None) if filesystem_config else None
        fs_safe_delete = getattr(filesystem_config, 'safe_delete', True) if filesystem_config else True

        tools.register(ReadFileTool(workspace=workspace, allowed_dir=allowed_dir, extra_allowed_dirs=extra_allowed_dirs))
        for cls in (WriteFileTool, EditFileTool, ListDirTool):
            tools.register(cls(workspace=workspace, allowed_dir=allowed_dir, backup_dir=fs_backup_dir, max_backups=fs_max_backups))
        tools.register(DeleteFileTool(workspace=workspace, allowed_dir=allowed_dir, backup_dir=fs_backup_dir, max_backups=fs_max_backups, safe_delete=fs_safe_delete))
        if exec_config and exec_config.enable:
            tools.register(
                ExecTool(
                    working_dir=str(workspace),
                    timeout=exec_config.timeout,
                    restrict_to_workspace=getattr(exec_config, 'restrict_to_workspace', False),
                    path_append=exec_config.path_append,
                    allowed_internal_ips=exec_config.allowed_internal_ips,
                )
            )
        tools.register(WebSearchTool(config=web_search_config, proxy=web_proxy))
        tools.register(WebFetchTool(proxy=web_proxy))
        tools.register(GlobTool(workspace=workspace, allowed_dir=allowed_dir))
        tools.register(GrepTool(workspace=workspace, allowed_dir=allowed_dir))

    def _setup_pipeline(self) -> None:
        """Initialize the message processing pipeline with middleware."""
        self.pipeline = MessagePipeline()
        self.pipeline.use(TombstoneMiddleware(self.turn_lifecycle))
        self.pipeline.use(
            QuestionResponseMiddleware(get_question_tool=lambda: self.question_tool)
        )
        self.pipeline.use(MemoryLifecycleMiddleware(self.tiered_memory))

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from markbot.agent.tools.mcp import connect_mcp_servers

        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)
            self._mcp_connected = True
        except BaseException as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._mcp_connecting = False

    def _set_tool_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Update context for all tools that need routing info."""
        for name in ("message", "spawn", "cron"):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    tool.set_context(channel, chat_id, *([message_id] if name == "message" else []))
        # Update question tool context
        if hasattr(self, "question_tool") and self.question_tool:
            self.question_tool.set_context(channel, chat_id)

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        from markbot.utils.helpers import strip_think

        return strip_think(text) or None

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
        iteration = 0
        final_content = None
        tools_used: list[str] = []

        logger.info(
            "[AgentLoop] Starting agent loop with {} initial messages", len(initial_messages)
        )

        # Wrap on_stream with stateful think-tag filter so downstream
        # consumers (CLI, channels) never see  <think>  blocks.
        _raw_stream = on_stream
        _stream_buf = ""

        async def _filtered_stream(delta: str) -> None:
            nonlocal _stream_buf
            from markbot.utils.helpers import strip_think

            prev_clean = strip_think(_stream_buf)
            _stream_buf += delta
            new_clean = strip_think(_stream_buf)
            incremental = new_clean[len(prev_clean) :]
            if incremental and _raw_stream:
                await _raw_stream(incremental)

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
            
            from markbot.agent.tokens import token_count_with_estimation
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
                    f", summary={compact_result.summary[:100]}..." if compact_result.summary else "",
                )
                current_tokens = compact_result.tokens_after

            tool_defs = self.tools.get_definitions()
            logger.debug(
                "[AgentLoop] Available tools: {}",
                [t.get("function", {}).get("name") for t in tool_defs[:5]],
            )

            if on_stream:
                logger.info("[AgentLoop] Calling LLM with streaming...")
                response = await self.provider.chat_stream_with_retry(
                    messages=messages,
                    tools=tool_defs,
                    model=self.model,
                    on_content_delta=_filtered_stream,
                )
            else:
                logger.info("[AgentLoop] Calling LLM without streaming...")
                response = await self.provider.chat_with_retry(
                    messages=messages,
                    tools=tool_defs,
                    model=self.model,
                )

            logger.info(
                "[AgentLoop] LLM response received: finish_reason={}, has_tool_calls={}, content_length={}",
                response.finish_reason,
                response.has_tool_calls,
                len(response.content or ""),
            )

            usage = response.usage or {}
            self._last_usage = {
                "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                "total_tokens": int(usage.get("total_tokens", 0) or 0),
                "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens", 0) or 0),
                "cache_read_input_tokens": int(usage.get("cache_read_input_tokens", 0) or 0),
            }
            self._last_context_tokens = current_tokens

            self.token_tracker.update_from_response(response)

            try:
                call_cost = self.cost_tracker.update_from_response(response, model=self.model)
                if call_cost > 0:
                    logger.debug("[AgentLoop] API cost this call: ${:.6f}", call_cost)
                    ct = self.cost_tracker
                    if (ct.warn_threshold_usd > 0
                            and not ct._warn_emitted
                            and ct.total_cost >= ct.warn_threshold_usd):
                        ct._warn_emitted = True
                        logger.warning(
                            "[AgentLoop] Cost warning: ${:.6f} (threshold=${:.2f})",
                            ct.total_cost,
                            ct.warn_threshold_usd,
                        )
            except BudgetExceededError as exc:
                logger.error(
                    "[AgentLoop] Budget exceeded: {}. Stopping loop early.", exc
                )
                break

            if response.has_tool_calls:
                logger.info(
                    "[AgentLoop] LLM requested {} tool call(s): {}",
                    len(response.tool_calls),
                    [tc.name for tc in response.tool_calls],
                )

                if on_stream and on_stream_end:
                    await on_stream_end(resuming=True)
                    _stream_buf = ""

                if on_progress:
                    if not on_stream:
                        thought = self._strip_think(response.content)
                        if thought:
                            await on_progress(thought)
                    tool_hint = self._tool_hint(response.tool_calls)
                    tool_hint = self._strip_think(tool_hint)
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

                # Execute all tool calls concurrently — the LLM batches
                # independent calls in a single response on purpose.
                # return_exceptions=True ensures all results are collected
                # even if one tool is cancelled or raises BaseException.
                logger.info("[AgentLoop] Executing {} tool calls...", len(response.tool_calls))
                results = await asyncio.gather(
                    *(self.tools.execute(tc.name, tc.arguments) for tc in response.tool_calls),
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
                        logger.info(
                            "[AgentLoop] Tool {} result: {}", tool_call.name, safe_preview
                        )
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
                    _stream_buf = ""

                clean = self._strip_think(response.content)
                if response.finish_reason == "error":
                    safe_clean = (clean or "")[:200].replace("{", "{{").replace("}", "}}")
                    logger.error(
                        "[AgentLoop] LLM returned error finish_reason: {}", safe_clean
                    )
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

        if final_content is None and iteration >= self.max_iterations:
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
        
        token_summary = self.token_tracker.get_summary()
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
            f"${cost_summary['budget_limit_usd']}" if cost_summary["budget_limit_usd"] else "unlimited",
            ", OVER BUDGET" if cost_summary["over_budget"] else "",
        )

        return final_content, tools_used, messages

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        await self._connect_mcp()
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
            task.add_done_callback(
                lambda t, k=msg.session_key: self._active_tasks.get(k, [])
                and self._active_tasks[k].remove(t)
                if t in self._active_tasks.get(k, [])
                else None
            )

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message: per-session serial, cross-session concurrent."""
        lock = self._session_locks.setdefault(msg.session_key, asyncio.Lock())
        gate = self._concurrency_gate or nullcontext()
        try:
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
                                    metadata={"_stream_end": True, "_resuming": resuming},
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
                        # _process_message returned None (e.g., MessageTool sent directly).
                        # Publish a minimal outbound to trigger reaction cleanup in the channel.
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
                except Exception:
                    logger.exception("Error processing message for session {}", msg.session_key)
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content="Sorry, I encountered an error.",
                            metadata=dict(msg.metadata or {}),
                        )
                    )
        finally:
            # Clean up lock if no active tasks remain for this session
            if msg.session_key in self._active_tasks:
                active = [t for t in self._active_tasks[msg.session_key] if not t.done()]
                if not active:
                    # No more active tasks for this session, safe to remove lock
                    self._session_locks.pop(msg.session_key, None)
                    self._active_tasks.pop(msg.session_key, None)

    async def close_mcp(self) -> None:
        """Drain pending background archives, then close MCP connections."""
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None

    def _schedule_background(self, coro) -> None:
        """Schedule a coroutine as a tracked background task (drained on shutdown)."""
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)
        task.add_done_callback(self._background_tasks.remove)

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
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
        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (
                msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id)
            )
            logger.info("Processing system message from {}", msg.sender_id)
            key = f"{channel}:{chat_id}"
            session = self.sessions.get_or_create(key)

            # === 墓碑标记：设置墓碑 ===
            turn_id = self.turn_lifecycle.begin_turn(session, msg.content)
            self.sessions.save(session)

            # Start tiered memory session
            self.tiered_memory.create_session(key)
            self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"))
            history = session.get_history(max_messages=0)
            current_role = "assistant" if msg.sender_id == "subagent" else "user"
            messages = self.context.build_messages(
                history=history,
                current_message=msg.content,
                channel=channel,
                chat_id=chat_id,
                current_role=current_role,
                session_key=key,
                session=session,
                memory_config=self.memory_config,
            )
            try:
                final_content, _, all_msgs = await self._run_agent_loop(
                    messages,
                    channel=channel,
                    chat_id=chat_id,
                    message_id=msg.metadata.get("message_id"),
                )
                self.tool_executor.save_turn(session, all_msgs, 1 + len(history))
                # === 墓碑标记：成功完成，清除标记 ===
                self.turn_lifecycle.complete_turn(session, turn_id)
                self.sessions.save(session)
            except Exception as e:
                # === 墓碑标记：失败，更新状态 ===
                self.turn_lifecycle.fail_turn(session, turn_id, str(e))
                self.sessions.save(session)
                # 保存 checkpoint 用于恢复
                try:
                    self.tiered_memory.close_session(key)
                except Exception as checkpoint_err:
                    logger.warning(f"Failed to close session: {checkpoint_err}")
                raise

            # Save to tiered memory (只在成功时保存)
            try:
                self.tiered_memory.process_turn(
                    chat_id=key,
                    user_input=msg.content,
                    assistant_response=final_content or "Background task completed.",
                    turn_number=len(history) + 1,
                )
                # Compaction may have truncated session.messages, re-persist
                self.sessions.save(session)
                self.tiered_memory.close_session(key)
            except Exception as e:
                logger.warning(f"Tiered memory save failed: {e}")
                # 保存 checkpoint 以防数据丢失
                try:
                    self.tiered_memory.close_session(key)
                except Exception as checkpoint_err:
                    logger.warning(f"Failed to close session after memory save error: {checkpoint_err}")
            return OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=final_content or "Background task completed.",
            )

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        logger.info(
            "[AgentLoop] _process_message started for channel={}, chat_id={}, content={}...",
            msg.channel,
            msg.chat_id,
            msg.content[:50],
        )

        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)

        # === 墓碑标记：清理过期的未完成标记 ===
        self.turn_lifecycle.cleanup_stale(session)

        # === 墓碑标记：检查并处理未完成的轮次 ===
        context_note = self.turn_lifecycle.check_incomplete(session, msg)
        if context_note:
            logger.info(
                "[AgentLoop] Detected incomplete turn, adding context note for session {}", key
            )

        # === 墓碑标记：设置新的墓碑，表示本轮次开始处理 ===
        turn_id = self.turn_lifecycle.begin_turn(session, msg.content)
        self.sessions.save(session)  # 立即持久化标记

        # Start tiered memory session
        logger.debug("[AgentLoop] Starting tiered memory session for key={}", key)
        self.tiered_memory.create_session(key)

        # Slash commands
        raw = msg.content.strip()
        ctx = CommandContext(msg=msg, session=session, key=key, raw=raw, loop=self)
        if result := await self.commands.dispatch(ctx):
            return result

        self._set_tool_context(msg.channel, msg.chat_id, msg.metadata.get("message_id"))
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        history = session.get_history(max_messages=0)
        initial_messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
            extra_system_context=context_note,
            session_key=key,
            session=session,
            memory_config=self.memory_config,
        )

        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    metadata=meta,
                )
            )

        logger.info("[AgentLoop] Calling _run_agent_loop...")
        try:
            final_content, _, all_msgs = await self._run_agent_loop(
                initial_messages,
                on_progress=on_progress or _bus_progress,
                on_stream=on_stream,
                on_stream_end=on_stream_end,
                channel=msg.channel,
                chat_id=msg.chat_id,
                message_id=msg.metadata.get("message_id"),
            )

            logger.info(
                "[AgentLoop] _run_agent_loop returned: final_content_length={}",
                len(final_content or ""),
            )

            if final_content is None:
                final_content = "I've completed processing but have no response to give."
                logger.warning("[AgentLoop] final_content was None, using default message")

            logger.info("[AgentLoop] Saving session with {} messages", len(all_msgs))
            self.tool_executor.save_turn(session, all_msgs, 1 + len(history))
            # === 墓碑标记：成功完成，清除标记 ===
            self.turn_lifecycle.complete_turn(session, turn_id)
            self.sessions.save(session)
        except Exception as e:
            # === 墓碑标记：失败，标记状态 ===
            logger.error("[AgentLoop] _run_agent_loop failed with exception: {}", e)
            self.turn_lifecycle.fail_turn(session, turn_id, str(e))
            self.sessions.save(session)
            # 保存 checkpoint 用于恢复
            try:
                self.tiered_memory.close_session(key)
            except Exception as checkpoint_err:
                logger.warning(f"[AgentLoop] Failed to close session: {checkpoint_err}")
            # 重新抛出异常，让上层处理
            raise

        # Save to tiered memory (compact triggered automatically when > 8 turns)
        try:
            logger.debug("[AgentLoop] Saving to tiered memory...")
            self.tiered_memory.process_turn(
                chat_id=key,
                user_input=msg.content,
                assistant_response=final_content,
                turn_number=len(history) + 1,
            )
            # Compaction may have truncated session.messages, re-persist
            self.sessions.save(session)
            self.tiered_memory.close_session(key)
            logger.debug("[AgentLoop] Tiered memory saved successfully")
        except Exception as e:
            logger.warning(f"[AgentLoop] Tiered memory save failed: {e}")
            # 保存 checkpoint 以防数据丢失
            try:
                self.tiered_memory.close_session(key)
            except Exception as checkpoint_err:
                logger.warning(f"[AgentLoop] Failed to save checkpoint after memory save error: {checkpoint_err}")

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            logger.info("[AgentLoop] MessageTool sent in turn, returning None")
            return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        safe_preview = preview.replace("{", "{{").replace("}", "}}")
        logger.info("[AgentLoop] Response to {}:{}: {}", msg.channel, msg.sender_id, safe_preview)

        meta = dict(msg.metadata or {})
        if on_stream is not None:
            meta["_streamed"] = True

        logger.info("[AgentLoop] _process_message completed, returning response")
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=meta,
        )

    @staticmethod
    def _image_placeholder(block: dict[str, Any]) -> dict[str, str]:
        """Convert an inline image block into a compact text placeholder."""
        path = (block.get("_meta") or {}).get("path", "")
        return {"type": "text", "text": f"[image: {path}]" if path else "[image]"}

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
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
        return await self._process_message(
            msg,
            session_key=session_key,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
        )
