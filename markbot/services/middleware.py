"""Built-in middleware for MessagePipeline."""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from markbot.bus.events import OutboundMessage
from markbot.services.message_pipeline import Middleware, ProcessContext

if TYPE_CHECKING:
    from markbot.memory.daily_log import DailyLogManager

_AUTO_SUMMARY_INTERVAL = 5


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

        if tool and hasattr(tool, "handle_response"):
            logger.debug(f"[QuestionMW] Handling response to question {question_id}")
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
        return response

    async def on_error(self, ctx: ProcessContext, error: Exception) -> None:
        pass


class MemoryLifecycleMiddleware(Middleware):
    """Bridges MemoryManager operations into the pipeline.

    - Appends raw interaction logs to daily markdown files (no LLM cost).
    - Periodically triggers background summarization every
      ``auto_summary_interval`` user messages.
    - Summary is also triggered during context compaction
      (MemoryCompactionHook) or manual commands (/compact, /new).
    """

    def __init__(
        self,
        memory_manager: object = None,
        daily_log: "DailyLogManager | None" = None,
        session_manager: object = None,
        auto_summary_interval: int = _AUTO_SUMMARY_INTERVAL,
    ):
        self._memory = memory_manager
        self._daily_log = daily_log
        self._session_manager = session_manager
        self._auto_summary_interval = max(auto_summary_interval, 1)
        self._session_message_counts: dict[str, int] = {}

    async def before(self, ctx: ProcessContext) -> OutboundMessage | None:
        return None

    async def after(
        self,
        ctx: ProcessContext,
        response: OutboundMessage | None,
    ) -> OutboundMessage | None:
        final_content = response.content if response else None

        if self._daily_log and final_content is not None:
            try:
                self._daily_log.append_turn(
                    user_content=ctx.msg.content,
                    assistant_content=final_content,
                    channel=ctx.channel,
                    chat_id=ctx.chat_id,
                )
            except Exception as e:
                logger.warning(f"[MemoryMW] Daily log append failed: {e}")

        if ctx.session:
            try:
                if self._session_manager and hasattr(self._session_manager, 'save'):
                    self._session_manager.save(ctx.session)
                elif hasattr(ctx.session, 'save'):
                    ctx.session.save()
            except Exception as e:
                logger.warning(f"[MemoryMW] Session save failed: {e}")

        session_key = ctx.session_key or "_default"
        count = self._session_message_counts.get(session_key, 0) + 1
        self._session_message_counts[session_key] = count
        if (
            self._memory
            and count >= self._auto_summary_interval
            and count % self._auto_summary_interval == 0
        ):
            try:
                history = []
                if ctx.session and hasattr(ctx.session, 'get_history'):
                    history = ctx.session.get_history(max_messages=0)
                if history:
                    summary_messages = [
                        m for m in history
                        if isinstance(m, dict) and "role" in m
                    ]
                    if summary_messages:
                        self._memory.add_async_summary_task(messages=summary_messages)
                        logger.info(
                            f"[MemoryMW] Auto summary triggered "
                            f"(session={session_key}, msg_count={count})"
                        )
            except Exception as e:
                logger.warning(f"[MemoryMW] Auto summary trigger failed: {e}")

        return response

    async def on_error(self, ctx: ProcessContext, error: Exception) -> None:
        if ctx.session:
            try:
                if self._session_manager and hasattr(self._session_manager, 'save'):
                    self._session_manager.save(ctx.session)
                elif hasattr(ctx.session, 'save'):
                    ctx.session.save()
            except Exception as e:
                logger.warning(f"[MemoryMW] Session save on error failed: {e}")
