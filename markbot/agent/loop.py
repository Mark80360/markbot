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
from markbot.utils.constants import AGENT_IDLE_TIMEOUT_MINUTES
from markbot.agent.container import AgentContext
from markbot.agent.pipeline.engine import ProcessContext
from markbot.agent.stream_scrubber import ScrubberPool
from markbot.bus.events import InboundMessage, OutboundMessage, make_session_key
from markbot.bus.queue import MessageBus
from markbot.cli.slash_commands import CommandContext
from markbot.tools.message import MessageTool
from markbot.types.permission import PermissionMode


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

        # ------------------------------------------------------------------
        # Prefix-cache & response-cache state.  See
        # :mod:`markbot.agent.prefix_cache` and
        # :mod:`markbot.agent.llm_response_cache` for the rationale.
        # ------------------------------------------------------------------
        self.prefix_stability = ctx.prefix_stability
        self.llm_response_cache = ctx.llm_response_cache
        self.token_estimate_cache = ctx.token_estimate_cache
        # Increment on every add/remove/clear of the message list so
        # the token-estimate cache invalidates correctly.
        self._messages_revision: int = 0

        self._running = False
        self.mcp = ctx.mcp
        self._active_tasks: dict[str, list[asyncio.Task]] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        # Serialises mutations to ``_active_tasks`` / ``_session_locks``.
        # In single-threaded asyncio the individual dict ops are atomic,
        # but multi-step sequences (track → dispatch → cleanup) benefit
        # from an explicit guard so future refactors stay safe.
        self._tasks_lock = asyncio.Lock()
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

        self._pending_steer: dict[str, str] = {}
        self._scrubber_pool = ScrubberPool()

        # Per-session last-active timestamps for idle timeout detection.
        # Updated on every inbound message and checked by a background
        # ``_idle_check_loop`` task. A session that has been silent longer
        # than ``AGENT_IDLE_TIMEOUT_MINUTES`` gets a notification + cleanup.
        self._session_last_active: dict[str, float] = {}
        self._idle_task: asyncio.Task | None = None

        # Per-session failure-loop state that survives across loop instances
        # (compact, "继续" 等都会重建 IterationRunner → LoopState)。
        # 关键：反思次数和失败方法黑名单必须跨 loop 边界持久化，
        # 否则每次 compact 后 reflection_injected_count 归零，
        # 强制停止机制在整个任务尺度上完全失效（见 logs/2026-06-27.log
        # 中 4 次强制停止后 LLM 仍能继续撞墙的案例）。
        # 仅在用户**新请求**（非"继续"指令）时重置，避免一次失败任务
        # 的反思次数永远卡住后续新任务。
        self._session_failure_state: dict[str, dict] = {}

        logger.info("Initialization complete, total took {:.3f}s", time.time() - _init_start)

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if not self.mcp.has_servers:
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
                todo_tool.set_session(make_session_key(channel, chat_id) or "")
            # Wire the todo tool into the compactor so active items get
            # re-injected as a standalone user message after auto-compaction.
            # Done here (not at construction) because the compactor is built
            # before the tool registry in the container.
            compactor = getattr(self, "compactor", None)
            if compactor is not None and hasattr(compactor, "set_todo_tool"):
                compactor.set_todo_tool(todo_tool)
        if self._memory_search_tool:
            self._memory_search_tool.set_session_context(channel, chat_id)
        # Propagate channel privacy context to curated memory tools and
        # context-explorer tools so shared channels cannot list/load
        # MEMORY.md / PROFILE.md content.
        for name in (
            "memory_list",
            "memory_forget",
            "memory_save",
            "explore_context_catalog",
            "search_context",
            "load_context",
        ):
            tool = self.tools.get(name) if self.tools else None
            if tool is not None and hasattr(tool, "set_session_context"):
                tool.set_session_context(channel, chat_id)

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
        session=None,
        on_tool_start: Callable[[str, str, str | None], Awaitable[None]] | None = None,
        on_tool_complete: Callable[[str, str, str | None, str | None], Awaitable[None]] | None = None,
        permission_mode_override: PermissionMode | None = None,
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
            session_key=make_session_key(channel, chat_id) or "",
            session=session,
            on_tool_start=on_tool_start,
            on_tool_complete=on_tool_complete,
            permission_mode_override=permission_mode_override,
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
            if self.memory_encoder and hasattr(self.memory_manager, "_memory_store") and self.memory_manager._memory_store:
                self.memory_encoder.set_memory_store(self.memory_manager._memory_store)
        except Exception as e:
            logger.warning("Memory manager start failed: {}", e)
        logger.info("Agent loop started, total startup took {:.3f}s", time.time() - _run_start)

        # Background idle-session checker — runs on a 30s cadence so the
        # main loop can block on ``consume_inbound()`` indefinitely instead
        # of waking every second just to poll idle state.
        self._idle_task = asyncio.create_task(self._idle_check_loop())

        while self._running:
            try:
                msg = await self.bus.consume_inbound()
            except asyncio.CancelledError:
                # Preserve real task cancellation so shutdown can complete cleanly.
                # Only ignore non-task CancelledError signals that may leak from integrations.
                if not self._running or asyncio.current_task().cancelling():
                    raise
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            # Mark the session as active.
            self._session_last_active[msg.session_key] = time.time()

            raw = msg.content.strip()
            if self.commands.is_priority(raw):
                ctx = CommandContext(msg=msg, session=None, key=msg.session_key, raw=raw, loop=self)
                result = await self.commands.dispatch_priority(ctx)
                if result:
                    await self.bus.publish_outbound(result)
                continue

            # Permission / question replies must NOT take the per-session lock.
            # Approval waits inside an in-flight turn that already holds the
            # lock; if the reply also waits on that lock the turn deadlocks
            # until the approval timeout. Resolve these short-circuit replies
            # concurrently via the pipeline middleware only.
            if self._is_pending_question_reply(msg):
                task = asyncio.create_task(self._dispatch_unlocked(msg))
                self._track_task(msg.session_key, task)
                task.add_done_callback(
                    lambda t, k=msg.session_key: self._untrack_task(k, t)
                )
                continue

            task = asyncio.create_task(self._dispatch(msg))
            self._track_task(msg.session_key, task)
            task.add_done_callback(
                lambda t, k=msg.session_key: self._untrack_task(k, t)
            )

    # ------------------------------------------------------------------
    # Task tracking helpers
    # ------------------------------------------------------------------

    def _track_task(self, session_key: str, task: asyncio.Task) -> None:
        """Register an in-flight dispatch task under its session key."""
        self._active_tasks.setdefault(session_key, []).append(task)

    def _untrack_task(self, session_key: str, task: asyncio.Task) -> None:
        """Remove a completed task; clean up empty session entries."""
        tasks = self._active_tasks.get(session_key)
        if tasks is None:
            self._session_locks.pop(session_key, None)
            return
        try:
            tasks.remove(task)
        except ValueError:
            pass
        if not tasks:
            self._active_tasks.pop(session_key, None)
            self._session_locks.pop(session_key, None)

    def _get_session_lock(self, session_key: str) -> asyncio.Lock:
        """Get or create the per-session serial lock."""
        return self._session_locks.setdefault(session_key, asyncio.Lock())

    # ------------------------------------------------------------------
    # Per-session failure-loop state (survives compact / "继续" / handoff)
    # ------------------------------------------------------------------

    def get_failure_state(self, session_key: str) -> dict:
        """Get (or create) the per-session failure-loop state.

        Returns a dict with keys:
        - ``reflection_injected_count``: int — 累计注入的反思次数，跨 loop 不归零
        - ``failed_methods``: list[str] — 已尝试且失败的方法黑名单（去重）
        - ``forced_stop_count``: int — 已触发强制停止的次数

        这个状态在 IterationRunner.run() 启动时被读取并填入 LoopState，
        在 _phase_check_failure_loop / _record_tool_result 中被写回。
        仅在用户**新请求**（非"继续"指令）时由 reset_failure_state 清零。
        """
        return self._session_failure_state.setdefault(
            session_key,
            {
                "reflection_injected_count": 0,
                "failed_methods": [],
                "forced_stop_count": 0,
            },
        )

    def reset_failure_state(self, session_key: str) -> None:
        """重置指定 session 的失败循环状态。

        仅在用户输入**新请求**时调用（非"继续"/compact/handoff 续接）。
        这样一次失败任务的反思次数不会永远卡住后续新任务。
        """
        self._session_failure_state[session_key] = {
            "reflection_injected_count": 0,
            "failed_methods": [],
            "forced_stop_count": 0,
        }

    # ------------------------------------------------------------------
    # Idle session management
    # ------------------------------------------------------------------

    def has_active_conversations(self) -> bool:
        """Return True if any session currently has an in-flight task.

        Used by background services (e.g. DreamService) to avoid running
        while a conversation is in progress.
        """
        for tasks in self._active_tasks.values():
            for t in tasks:
                if not t.done():
                    return True
        return False

    async def _idle_check_loop(self) -> None:
        """Background task that periodically checks for idle sessions.

        Runs on a 30-second cadence so the main loop can block on
        ``consume_inbound()`` indefinitely without waking to poll.
        """
        while self._running:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                return
            self._check_idle_sessions(time.time())

    def _check_idle_sessions(self, now: float) -> None:
        """Check and clean up sessions that have exceeded the idle timeout.

        Called by the background ``_idle_check_loop``. Iterates a snapshot
        of the session keys so we don't mutate the dict while iterating it.
        """
        idle_seconds = AGENT_IDLE_TIMEOUT_MINUTES * 60
        if idle_seconds <= 0:
            return  # disabled

        for session_key in list(self._session_last_active.keys()):
            last_active = self._session_last_active.get(session_key)
            if last_active is None:
                continue
            elapsed = now - last_active
            if elapsed >= idle_seconds:
                logger.info(
                    "Session {} idle for {:.0f}s (>{:.0f}s), timing out",
                    session_key, elapsed, idle_seconds,
                )
                asyncio.ensure_future(self._notify_idle_timeout(session_key))

    async def _notify_idle_timeout(self, session_key: str) -> None:
        """Send an idle-timeout notification for *session_key*.

        The session_key format is ``channel:chat_id``. We parse it to
        route the notification back through the correct channel.

        In-flight tasks are given a grace period to complete before
        cleanup, so we don't cancel active LLM calls or tool executions
        mid-flight (which could leave files half-written or sessions
        unsaved).
        """
        channel = chat_id = session_key
        if ":" in session_key:
            channel, chat_id = session_key.split(":", 1)

        # 暂时取消，不要删除
        # try:
        #     await self.bus.publish_outbound(
        #         OutboundMessage(
        #             channel=channel,
        #             chat_id=chat_id,
        #             content=f"⏰ 会话已闲置 {AGENT_IDLE_TIMEOUT_MINUTES} 分钟，自动关闭。",
        #             metadata={"_idle_timeout": True},
        #         )
        #     )
        # except Exception as e:
        #     logger.warning("Failed to publish idle timeout notification: {}", e)

        # Gracefully wait for in-flight tasks before cleaning up.
        # This prevents cancelling active LLM calls or tool executions
        # that could leave state inconsistent.
        tasks = self._active_tasks.get(session_key, [])
        pending = [t for t in tasks if not t.done()]
        if pending:
            logger.info(
                "Waiting for {} in-flight task(s) to complete for session {} "
                "(up to 30s grace period)",
                len(pending), session_key,
            )
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Tasks for session {} did not complete in 30s, cancelling",
                    session_key,
                )
                for t in pending:
                    if not t.done():
                        t.cancel()
                # Wait briefly for cancellation to propagate.
                await asyncio.gather(*pending, return_exceptions=True)

        self._cleanup_session_state(session_key)

    def _cleanup_session_state(self, session_key: str) -> None:
        """Release resources held by *session_key* after idle timeout.

        Removes session locks, resets the streaming scrubber, evicts
        memory manager session caches, and cleans up steer / last-active
        tracking so the next message from this session starts fresh.

        Note: in-flight task cancellation is handled by
        ``_notify_idle_timeout`` before this method is called.
        """
        # Active tasks are already handled by _notify_idle_timeout.
        # Just pop the list to clear the reference.
        self._active_tasks.pop(session_key, None)

        # Release session-level lock.
        self._session_locks.pop(session_key, None)

        # Reset streaming scrubber state for this session.
        self._scrubber_pool.reset(session_key)

        # Evict memory manager per-session caches (summaries, archived
        # counts).  State is persisted to disk and reloaded on next use.
        memory_manager = getattr(self, "memory_manager", None)
        if memory_manager and hasattr(memory_manager, "evict_session_cache"):
            try:
                memory_manager.evict_session_cache(session_key)
            except Exception as e:
                logger.debug("Failed to evict memory session cache: {}", e)

        # Clear steer and idle tracking.
        self._pending_steer.pop(session_key, None)
        self._session_last_active.pop(session_key, None)

        # Clear per-session failure-loop state to avoid memory leak
        # across many idle sessions. Next message from this session
        # will be treated as a new request and reset_failure_state
        # is called anyway, but clearing here keeps state bounded.
        self._session_failure_state.pop(session_key, None)

        logger.info("Cleaned up state for idle session {}", session_key)

    def _is_pending_question_reply(self, msg: InboundMessage) -> bool:
        """Return True if this inbound is answering a pending ask/approval.

        Used to route the reply outside the per-session lock so the waiting
        tool turn can observe it.
        """
        meta = msg.metadata or {}
        qid = meta.get("question_id")
        if not qid:
            content = (msg.content or "").strip()
            if content.startswith("[Q:") and "]" in content:
                try:
                    qid = content.split("[Q:", 1)[1].split("]", 1)[0].strip()
                except (IndexError, AttributeError):
                    qid = None
        if not qid:
            return False

        question_tool = getattr(self, "question_tool", None)
        pending = getattr(question_tool, "_pending_questions", None) or {}
        if qid in pending:
            return True

        approver = getattr(getattr(self, "ctx", None), "permission_approver", None)
        if approver is not None and qid in getattr(approver, "_pending", {}):
            return True
        return False

    async def _dispatch_unlocked(self, msg: InboundMessage) -> None:
        """Process a message without the per-session lock (question replies)."""
        gate = self._concurrency_gate or nullcontext()
        async with gate:
            try:
                response = await self._process_message(msg)
                if response is not None:
                    await self.bus.publish_outbound(response)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Error processing unlocked reply for session {}", msg.session_key
                )

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message: per-session serial, cross-session concurrent."""
        lock = self._get_session_lock(msg.session_key)
        gate = self._concurrency_gate or nullcontext()
        async with lock, gate:
            try:
                on_stream = on_stream_end = None
                if msg.metadata.get("_wants_stream"):

                    async def on_stream(delta: str) -> None:
                        visible = self._scrubber_pool.feed(msg.session_key, delta)
                        if not visible:
                            return
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content=visible,
                                metadata={"_stream_delta": True},
                            )
                        )

                    async def on_stream_end(*, resuming: bool = False, discard: bool = False) -> None:
                        # Flush any held-back text in the scrubber before signalling end.
                        if not discard:
                            trailing = self._scrubber_pool.flush(msg.session_key)
                            if trailing:
                                await self.bus.publish_outbound(
                                    OutboundMessage(
                                        channel=msg.channel,
                                        chat_id=msg.chat_id,
                                        content=trailing,
                                        metadata={"_stream_delta": True},
                                    )
                                )
                            self._scrubber_pool.reset(msg.session_key)
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
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        self._scrubber_pool.clear()
        try:
            self.sessions.close()
        except Exception as e:
            logger.warning("Failed to close sessions on stop: {}", e)
        try:
            worker = getattr(self.memory_manager, "_worker_task", None)
            if worker and not worker.done():
                worker.cancel()
        except Exception as e:
            logger.warning("Failed to cancel memory worker on stop: {}", e)
        logger.info("Agent loop stopping")

    def get_cost_summary(self) -> dict[str, Any]:
        """Return cost tracking summary including per-model breakdown."""
        return self.cost_tracker.get_summary()

    def steer(self, session_key: str, text: str) -> bool:
        if not text or not text.strip():
            return False
        cleaned = text.strip()
        existing = self._pending_steer.get(session_key, "")
        self._pending_steer[session_key] = (existing + "\n" + cleaned) if existing else cleaned
        logger.info("Steer accepted for session {}: {} chars", session_key, len(cleaned))
        return True

    def drain_steer(self, session_key: str) -> str | None:
        text = self._pending_steer.pop(session_key, None)
        if text:
            logger.debug("Drained steer for session {}: {} chars", session_key, len(text))
        return text

    def clear_steer(self, session_key: str) -> None:
        self._pending_steer.pop(session_key, None)

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
        on_tool_start: Callable[[str, str, str | None], Awaitable[None]] | None = None,
        on_tool_complete: Callable[[str, str, str | None, str | None], Awaitable[None]] | None = None,
        on_outbound_message: Callable[["OutboundMessage"], Awaitable[None]] | None = None,
        permission_mode_override: PermissionMode | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        channel = msg.channel
        chat_id = msg.chat_id

        if channel == "system":
            channel = msg.origin_channel or ("cli" if ":" not in chat_id else chat_id.split(":", 1)[0])
            chat_id = msg.origin_chat_id or chat_id

        key = session_key or msg.session_key
        if msg.channel == "system":
            key = make_session_key(channel, chat_id) or ""

        pctx = ProcessContext(
            msg=msg,
            session_key=key,
            channel=channel,
            chat_id=chat_id,
            permission_mode_override=permission_mode_override,
        )

        async def _handler(ctx: ProcessContext) -> OutboundMessage | None:
            return await self._handle_message(
                ctx,
                on_progress=on_progress,
                on_stream=on_stream,
                on_stream_end=on_stream_end,
                on_tool_start=on_tool_start,
                on_tool_complete=on_tool_complete,
                on_outbound_message=on_outbound_message,
            )

        return await self.pipeline.process(pctx, _handler)

    async def _handle_message(
        self,
        ctx: ProcessContext,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        on_tool_start: Callable[[str, str, str | None], Awaitable[None]] | None = None,
        on_tool_complete: Callable[[str, str, str | None, str | None], Awaitable[None]] | None = None,
        on_outbound_message: Callable[["OutboundMessage"], Awaitable[None]] | None = None,
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
            from markbot.agent.turn_metadata import make_turn_metadata as _make_meta_sys
            _sys_meta = _make_meta_sys(model=self.config.primary_model_ref if self.config else "")
            messages = await self.context.build_messages(
                history=history,
                current_message=msg.content,
                channel=channel,
                chat_id=chat_id,
                current_role=current_role,
                session_key=key,
                session=session,
                turn_meta=_sys_meta,
            )
            try:
                final_content, _, all_msgs, _new_start = await self._run_agent_loop(
                    messages,
                    channel=channel,
                    chat_id=chat_id,
                    message_id=msg.metadata.get("message_id"),
                    session=session,
                    on_tool_start=on_tool_start,
                    on_tool_complete=on_tool_complete,
                    permission_mode_override=ctx.permission_mode_override,
                )
                self.tool_executor.save_turn(session, all_msgs, _new_start)
                self.sessions.save(session)
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

        # 检测"继续"指令：当用户输入"继续"/"continue"等时，注入 resume 引导，
        # 让 LLM 知道要从上次中断处续接，而不是当作全新请求处理。
        # 配合 _finalize_loop 中写入 history 的中断原因，agent 能感知上次进度。
        _resume_triggers = ("继续", "continue", "go on", "resume", "接着", "接着做", "继续做")
        is_resume = raw.lower() in _resume_triggers
        if is_resume:
            resume_hint = (
                "[System] 用户输入了「继续」。请检查对话历史中是否存在因"
                "「达到工具调用迭代上限」或「预算超限」而中断的任务。"
                "如果存在，请从上次中断处续接任务：\n"
                "1. 先回顾历史中已完成的步骤（已创建的文件、已验证的依赖等），"
                "不要重复这些步骤；\n"
                "2. 识别上次中断时正在进行的子任务；\n"
                "3. 从该子任务继续执行，直到任务完成。\n"
                "如果历史中没有中断记录，则按正常方式处理用户请求。"
            )
            msg.content = resume_hint + "\n\n用户输入：" + raw
        else:
            # 新请求：重置该 session 的失败循环状态。
            # 关键：失败反思次数和失败方法黑名单只在同一任务内累积；
            # 用户开新话题时应清零，避免上次任务的反思次数卡住新任务。
            # "继续"指令和 compact 内部触发都不重置（它们是同一任务的延续）。
            self.reset_failure_state(key)

        self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"))
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        history = session.get_history(max_messages=200)
        # Build a fresh turn_meta so the user message can carry the
        # per-turn metadata at its tail.  This keeps the leading
        # user input byte-identical across turns so the server-side
        # KV prefix cache can hit on it.  See
        # markbot.agent.turn_metadata for the full rationale.
        from markbot.agent.turn_metadata import make_turn_metadata as _make_meta
        _meta = _make_meta(model=self.config.primary_model_ref if self.config else "")
        initial_messages = await self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=channel,
            chat_id=chat_id,
            session_key=key,
            session=session,
            turn_meta=_meta,
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
                session=session,
                on_tool_start=on_tool_start,
                on_tool_complete=on_tool_complete,
                permission_mode_override=ctx.permission_mode_override,
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
            self.sessions.save(session)
        except Exception as e:
            logger.error("_run_agent_loop failed with exception: {}", e)
            raise

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            logger.info("MessageTool sent in turn, returning None")
            if on_outbound_message and mt.last_message:
                await on_outbound_message(mt.last_message)
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
        media: list[str] | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        on_tool_start: Callable[[str, str, str | None], Awaitable[None]] | None = None,
        on_tool_complete: Callable[[str, str, str | None, str | None], Awaitable[None]] | None = None,
        on_outbound_message: Callable[["OutboundMessage"], Awaitable[None]] | None = None,
        permission_mode: PermissionMode | None = None,
    ) -> OutboundMessage | None:
        """Process a message directly and return the outbound payload.

        ``permission_mode`` lets unattended callers (cron / autopilot /
        heartbeat) force a mode without relying on the global ``app_state``
        being set by a prior ``/mode`` command — those paths run before any
        interactive user can approve tool calls, so they must opt in here.
        """
        _direct_start = time.time()
        logger.info("process_direct starting...")
        await self._connect_mcp()
        if self.memory_manager and not getattr(self.memory_manager, "_started", False):
            try:
                _t0 = time.time()
                logger.info("Starting memory manager in process_direct...")
                await self.memory_manager.start()
                logger.info("Memory manager started in process_direct, took {:.3f}s", time.time() - _t0)
                if self.memory_encoder and hasattr(self.memory_manager, "_memory_store") and self.memory_manager._memory_store:
                    self.memory_encoder.set_memory_store(self.memory_manager._memory_store)
            except Exception as e:
                logger.warning("Memory manager start failed in process_direct: {}", e)
        logger.info("process_direct startup took {:.3f}s", time.time() - _direct_start)
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content, media=media or [])
        return await self._process_message(
            msg,
            session_key=session_key,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            on_tool_start=on_tool_start,
            on_tool_complete=on_tool_complete,
            on_outbound_message=on_outbound_message,
            permission_mode_override=permission_mode,
        )
