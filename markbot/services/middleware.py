"""Built-in middleware for MessagePipeline."""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from markbot.services.message_pipeline import Middleware, ProcessContext
from markbot.bus.events import OutboundMessage

if TYPE_CHECKING:
    from markbot.memory.daily_log import DailyLogManager


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
    - Summary is only triggered during context compaction (MemoryCompactionHook)
      or manual commands (/compact, /new), matching CoPaw's strategy.
    """

    def __init__(
        self,
        memory_manager: object = None,
        daily_log: "DailyLogManager | None" = None,
        session_manager: object = None,
    ):
        self._memory = memory_manager
        self._daily_log = daily_log
        self._session_manager = session_manager

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
