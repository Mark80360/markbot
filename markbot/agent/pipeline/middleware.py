"""Built-in middleware for MessagePipeline."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from loguru import logger

from markbot.agent.pipeline.engine import Middleware, ProcessContext
from markbot.bus.events import OutboundMessage

if TYPE_CHECKING:
    from markbot.memory.daily_log import DailyLogManager

_AUTO_SUMMARY_INTERVAL = 5


@runtime_checkable
class _HasHandleResponse(Protocol):
    def handle_response(self, question_id: str, response: str) -> None: ...


class QuestionResponseMiddleware(Middleware):
    """Handles responses to pending user questions.

    Short-circuits processing if message is a question response.
    """

    def __init__(self, get_question_tool: object = None):
        self._get_question_tool = get_question_tool

    async def before(self, ctx: ProcessContext) -> OutboundMessage | None:
        question_id = ctx.msg.metadata.get("question_id") if ctx.msg.metadata else None
        if not question_id:
            content = ctx.msg.content.strip()
            if content.startswith("[Q:") and "]" in content:
                try:
                    question_id = content.split("[Q:", 1)[1].split("]", 1)[0].strip()
                except (IndexError, AttributeError):
                    pass

        if not question_id:
            return None

        tool = None
        if self._get_question_tool:
            tool = self._get_question_tool()

        if isinstance(tool, _HasHandleResponse):
            logger.debug("Handling response to question {}", question_id)
            tool.handle_response(question_id, ctx.msg.content)
            return OutboundMessage(
                channel=ctx.channel,
                chat_id=ctx.chat_id,
                content="",
                metadata=dict(ctx.msg.metadata or {}),
            )

        return None

    async def after(
        self, ctx: ProcessContext, response: OutboundMessage | None
    ) -> OutboundMessage | None:
        _ = ctx
        return response

    async def on_error(self, ctx: ProcessContext, error: Exception) -> None:
        _ = ctx, error


class MemoryLifecycleMiddleware(Middleware):
    """Bridges MemoryManager operations into the pipeline.

    - Periodically triggers background summarization every
      ``auto_summary_interval`` user messages.
    - Summary is also triggered during context compaction
      (MemoryCompactionHook) or manual commands (/compact, /new).

    Note: Daily log writing is handled by MemoryManager.sync_turn()
    in IterationRunner._phase_memory_sync(), not here.

    Note: Session persistence is handled by ToolExecutor.save_turn()
    in AgentLoop._handle_message(). This middleware only saves on
    error (to preserve partial progress) and skips the normal-path
    save to avoid redundant disk I/O.
    """

    def __init__(
        self,
        memory_manager: object = None,
        daily_log: DailyLogManager | None = None,
        session_manager: object = None,
        auto_summary_interval: int = _AUTO_SUMMARY_INTERVAL,
    ):
        self._memory = memory_manager
        self._daily_log = daily_log
        self._session_manager = session_manager
        self._auto_summary_interval = max(auto_summary_interval, 1)
        # In-memory cache of per-session message counts. Persisted to
        # ``session.metadata["_auto_summary_count"]`` on each turn so the
        # count survives process restarts instead of resetting to zero.
        self._session_message_counts: dict[str, int] = {}

    def _get_message_count(self, ctx: ProcessContext) -> int:
        """Return the persisted message count for *ctx*'s session.

        Reads from ``session.metadata`` (durable across restarts) and
        caches it in ``_session_message_counts`` for the hot path.
        """
        session_key = ctx.session_key or "_default"
        cached = self._session_message_counts.get(session_key)
        if cached is not None:
            return cached
        persisted = 0
        if ctx.session and isinstance(getattr(ctx.session, "metadata", None), dict):
            persisted = int(ctx.session.metadata.get("_auto_summary_count", 0))
        self._session_message_counts[session_key] = persisted
        return persisted

    def _set_message_count(self, ctx: ProcessContext, count: int) -> None:
        """Update both the in-memory cache and the session metadata."""
        session_key = ctx.session_key or "_default"
        self._session_message_counts[session_key] = count
        if ctx.session and isinstance(getattr(ctx.session, "metadata", None), dict):
            ctx.session.metadata["_auto_summary_count"] = count

    async def before(self, ctx: ProcessContext) -> OutboundMessage | None:
        _ = ctx
        return None

    async def after(
        self,
        ctx: ProcessContext,
        response: OutboundMessage | None,
    ) -> OutboundMessage | None:
        # Session persistence is handled by sessions.save() in
        # AgentLoop._handle_message() after save_turn().

        count = self._get_message_count(ctx) + 1
        self._set_message_count(ctx, count)
        if (
            self._memory
            and count >= self._auto_summary_interval
            and count % self._auto_summary_interval == 0
        ):
            try:
                history = []
                if ctx.session and hasattr(ctx.session, 'get_history'):
                    history = ctx.session.get_history(max_messages=200)
                if history:
                    summary_messages = [
                        m for m in history
                        if isinstance(m, dict) and "role" in m
                    ]
                    if summary_messages:
                        self._memory.add_async_summary_task(messages=summary_messages)
                        logger.info(
                            "Auto summary triggered (session={}, msg_count={})",
                            ctx.session_key or "_default", count,
                        )
            except Exception as e:
                logger.warning("Auto summary trigger failed: {}", e)

        return response

    async def on_error(self, ctx: ProcessContext, error: Exception) -> None:
        _ = error
        if ctx.session:
            try:
                if self._session_manager and hasattr(self._session_manager, 'save'):
                    self._session_manager.save(ctx.session)
            except Exception as e:
                logger.warning("Session save on error failed: {}", e)
