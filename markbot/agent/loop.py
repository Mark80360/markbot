"""Agent loop: the core processing engine.

Refactored to use new core types and skill system inspired by MarkBot.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from markbot.agent.compact import CompactionConfig
from markbot.agent.container import AgentContext
from markbot.agent.pipeline.engine import ProcessContext
from markbot.bus.events import InboundMessage, OutboundMessage
from markbot.bus.queue import MessageBus
from markbot.cli.slash_commands import CommandContext
from markbot.tools.message import MessageTool
from markbot.types.permission import PermissionMode, ToolPermissionContext
from markbot.types.tool import ToolContext


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
        ctx_or_bus: AgentContext | MessageBus,
        fallback_manager=None,
        config=None,
        workspace: Path | None = None,
        max_iterations: int = 40,
        context_window_tokens: int = 65_536,
        web_search_config=None,
        web_proxy: str | None = None,
        exec_config=None,
        filesystem_config=None,
        memory_config=None,
        cron_service=None,
        restrict_to_workspace: bool = False,
        session_manager=None,
        mcp_servers: dict | None = None,
        channels_config=None,
        timezone: str | None = None,
        compaction_config: CompactionConfig | None = None,
        max_budget_usd: float | None = None,
        warn_threshold_usd: float = 0.5,
        budget_config=None,
    ):
        if isinstance(ctx_or_bus, AgentContext):
            self._init_from_context(ctx_or_bus)
        else:
            ctx = AgentContext.from_legacy_params(
                bus=ctx_or_bus,
                fallback_manager=fallback_manager,
                config=config,
                workspace=workspace,
                max_iterations=max_iterations,
                context_window_tokens=context_window_tokens,
                web_search_config=web_search_config,
                web_proxy=web_proxy,
                exec_config=exec_config,
                filesystem_config=filesystem_config,
                memory_config=memory_config,
                cron_service=cron_service,
                restrict_to_workspace=restrict_to_workspace,
                session_manager=session_manager,
                mcp_servers=mcp_servers,
                channels_config=channels_config,
                timezone=timezone,
                compaction_config=compaction_config,
                max_budget_usd=max_budget_usd,
                warn_threshold_usd=warn_threshold_usd,
                budget_config=budget_config,
            )
            self._init_from_context(ctx)

    def _init_from_context(self, ctx: AgentContext) -> None:
        _init_start = time.time()
        logger.info("Starting initialization from AgentContext...")

        self.ctx = ctx
        self.bus = ctx.bus
        self.channels_config = ctx.channels_config
        self.fallback_manager = ctx.fallback_manager
        self.config = ctx.config
        self.workspace = ctx.workspace
        self.model = ctx.model
        self.max_iterations = ctx.max_iterations
        self.context_window_tokens = ctx.context_window_tokens
        self.web_search_config = ctx.web_search_config
        self.web_proxy = ctx.web_proxy
        self.exec_config = ctx.exec_config
        self.filesystem_config = ctx.filesystem_config
        self.memory_config = ctx.memory_config
        self.code_execution_config = ctx.code_execution_config
        self.cron_service = ctx.cron_service
        self.restrict_to_workspace = ctx.restrict_to_workspace

        self._start_time = time.time()
        self._last_usage: dict[str, int] = {}
        self._last_context_tokens: int = 0

        self.compactor = ctx.compactor
        self.cost_tracker = ctx.cost_tracker
        self.tools = ctx.tools
        self.skill_registry = ctx.skill_registry
        self.guardrail_manager = ctx.guardrail_manager
        self.sessions = ctx.sessions
        self.subagents = ctx.subagents

        self._running = False
        self.mcp = ctx.mcp
        self._active_tasks: dict[str, list[asyncio.Task]] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        _max = int(os.environ.get("MARKBOT_MAX_CONCURRENT_REQUESTS", "3"))
        self._concurrency_gate: asyncio.Semaphore | None = (
            asyncio.Semaphore(_max) if _max > 0 else None
        )

        self.memory_manager = ctx.memory_manager
        self.bootstrap_hook = ctx.bootstrap_hook
        self.compaction_hook = ctx.compaction_hook
        self.context = ctx.context_builder
        self._memory_search_tool = ctx.memory_search_tool
        self.question_tool = ctx.question_tool

        logger.info("Agent has {} tools available", len(self.tools))
        self.commands = ctx.commands
        self.tool_executor = ctx.tool_executor
        self.pipeline = ctx.pipeline
        self._daily_log = ctx.daily_log
        self._interaction_log = ctx.interaction_log

        self.handoff_manager = ctx.handoff_manager
        self.session_bootstrap = ctx.session_bootstrap
        self.task_tracker = ctx.task_tracker
        self.memory_encoder = ctx.memory_encoder

        logger.info("Initialization complete, total took {:.3f}s", time.time() - _init_start)

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if not self.mcp._mcp_servers:
            logger.debug("No MCP servers configured, skipping connection")
            return
        _t0 = time.time()
        logger.info("Connecting to MCP servers...")
        await self.mcp.connect(self.tools)
        logger.info("MCP connection took {:.3f}s", time.time() - _t0)

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
    ) -> tuple[str | None, list[str], list[dict], int]:
        from markbot.agent.iteration import IterationRunner

        runner = IterationRunner(
            loop=self,
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            session_key=f"{channel}:{chat_id}",
        )
        return await runner.run(initial_messages)

    def _gather_task_context(self) -> str:
        max_task_context_chars = 2000
        parts: list[str] = []

        todo_tool = self.tools.get("todo")
        if todo_tool is not None and hasattr(todo_tool, "_get_store"):
            try:
                store = todo_tool._get_store()
                items = store.list_items()
                active_items = [i for i in items if i.get("status") in ("pending", "in_progress")]
                if active_items:
                    lines = ["[CURRENT TASK STATE — preserve verbatim in summary]"]
                    for item in active_items:
                        status = item.get("status", "pending")
                        priority = item.get("priority", "medium")
                        content = item.get("content", "")
                        item_id = item.get("id", "?")
                        lines.append(f"  - [{item_id}] {content} (status={status}, priority={priority})")
                    text = "\n".join(lines)
                    if len(text) > max_task_context_chars:
                        text = text[:max_task_context_chars] + "\n  ... (truncated)"
                    parts.append(text)
            except Exception as e:
                logger.debug("Failed to gather todo context for compaction: {}", e)

        task_tracker = getattr(self, "task_tracker", None)
        if task_tracker is not None:
            try:
                active = task_tracker.list_active()
                if active:
                    lines = ["[TASK TRACKER STATE — preserve verbatim in summary]"]
                    for t in active:
                        lines.append(
                            f"  - [{t.id}] {t.title} (status={t.status}, progress={t.progress})"
                        )
                    pending = task_tracker.list_pending()
                    for t in pending[:3]:
                        lines.append(
                            f"  - [{t.id}] {t.title} (status=stated, priority={t.priority})"
                        )
                    text = "\n".join(lines)
                    if len(text) > max_task_context_chars:
                        text = text[:max_task_context_chars] + "\n  ... (truncated)"
                    parts.append(text)
            except Exception as e:
                logger.debug("Failed to gather task tracker context for compaction: {}", e)

        return "\n\n".join(parts) if parts else ""

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
        _run_start = time.time()
        logger.info("Starting run()...")
        self._running = True
        await self._connect_mcp()
        try:
            _t0 = time.time()
            logger.info("Starting memory manager...")
            await self.memory_manager.start()
            logger.info("Memory manager started, took {:.3f}s", time.time() - _t0)
        except Exception as e:
            logger.warning("Memory manager start failed: {}", e)
        logger.info("Agent loop started, total startup took {:.3f}s", time.time() - _run_start)

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

                    async def on_stream_end(*, resuming: bool = False, discard: bool = False) -> None:
                        meta: dict[str, Any] = {
                            "_resuming": resuming,
                            "message_id": msg.metadata.get("message_id")
                            if msg.metadata
                            else None,
                            "reaction_id": msg.metadata.get("reaction_id")
                            if msg.metadata
                            else None,
                        }
                        if discard:
                            meta["_stream_discard"] = True
                        else:
                            meta["_stream_end"] = True
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content="",
                                metadata=meta,
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
            "_process_message started for channel={}, chat_id={}, content={}...",
            channel,
            chat_id,
            msg.content[:50],
        )

        session = self.sessions.get_or_create(key)
        ctx.session = session

        logger.debug("Starting memory session for key={}", key)

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

        logger.info("Calling _run_agent_loop...")
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
                "_run_agent_loop returned: final_content_length={}",
                len(final_content or ""),
            )

            if final_content is None:
                final_content = "I've completed processing but have no response to give."
                logger.warning("final_content was None, using default message")

            logger.info("Saving session with {} messages (new_msg_start={})", len(all_msgs), _new_start)
            self.tool_executor.save_turn(session, all_msgs, _new_start)
        except Exception as e:
            logger.error("_run_agent_loop failed with exception: {}", e)
            raise

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            logger.info("MessageTool sent in turn, returning None")
            return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        safe_preview = preview.replace("{", "{{").replace("}", "}}")
        logger.info("Response to {}:{}: {}", channel, msg.sender_id, safe_preview)

        meta = dict(msg.metadata or {})
        if on_stream is not None:
            meta["_streamed"] = True

        logger.info("_process_message completed, returning response")
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
        _direct_start = time.time()
        logger.info("process_direct starting...")
        await self._connect_mcp()
        if self.memory_manager and not getattr(self.memory_manager, "_started", False):
            try:
                _t0 = time.time()
                logger.info("Starting memory manager in process_direct...")
                await self.memory_manager.start()
                logger.info("Memory manager started in process_direct, took {:.3f}s", time.time() - _t0)
            except Exception as e:
                logger.warning("Memory manager start failed in process_direct: {}", e)
        logger.info("process_direct startup took {:.3f}s", time.time() - _direct_start)
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
        return await self._process_message(
            msg,
            session_key=session_key,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
        )
