"""Built-in middleware for MessagePipeline."""

from __future__ import annotations

from typing import Any, Awaitable

from loguru import logger

from markbot.agent.services.message_pipeline import Middleware, ProcessContext
from markbot.agent.services.turn_lifecycle import TurnLifecycle
from markbot.bus.events import InboundMessage, OutboundMessage


class TombstoneMiddleware(Middleware):
    """Manages turn lifecycle with tombstone markers.

    Runs before handler to check/cleanup stale markers and set new ones.
    Runs after handler to clear markers on success or mark failure on error.
    """

    def __init__(self, lifecycle: TurnLifecycle):
        self._lifecycle = lifecycle

    async def before(self, ctx: ProcessContext) -> OutboundMessage | None:
        if not ctx.session:
            return None

        self._lifecycle.cleanup_stale(ctx.session)
        context_note = self._lifecycle.check_incomplete(ctx.session, ctx.msg)
        if context_note:
            ctx.extra["context_note"] = context_note
            logger.info(
                f"[TombstoneMW] Detected incomplete turn for session {ctx.session.key}"
            )

        turn_id = self._lifecycle.begin_turn(ctx.session, ctx.msg.content)
        ctx.extra["turn_id"] = turn_id
        ctx.session.save()
        return None

    async def after(
        self, ctx: ProcessContext, response: OutboundMessage | None
    ) -> OutboundMessage | None:
        if not ctx.session:
            return response

        turn_id = ctx.extra.get("turn_id")
        if turn_id:
            self._lifecycle.complete_turn(ctx.session, turn_id)
            ctx.session.save()
        return response

    def mark_failed(self, ctx: ProcessContext, error: str) -> None:
        """Call from exception handler when handler fails."""
        if not ctx.session:
            return
        turn_id = ctx.extra.get("turn_id")
        if turn_id:
            self._lifecycle.fail_turn(ctx.session, turn_id, error)
            ctx.session.save()


class QuestionResponseMiddleware(Middleware):
    """Handles responses to pending user questions.

    Short-circuits processing if message is a question response.
    """

    def __init__(self, get_question_tool: Any = None):
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
                content=None,
            )

        return None

    async def after(
        self, ctx: ProcessContext, response: OutboundMessage | None
    ) -> OutboundMessage | None:
        return response


class MemoryLifecycleMiddleware(Middleware):
    """Bridges TieredMemory operations into the pipeline.

    Handles start_loop/end_loop/save_turn calls around agent execution.
    """

    def __init__(self, tiered_memory: Any = None):
        self._memory = tiered_memory

    async def before(self, ctx: ProcessContext) -> OutboundMessage | None:
        key = ctx.session_key or ctx.msg.session_key
        if self._memory and key:
            try:
                self._memory.create_session(key)
            except Exception as e:
                logger.warning(f"[MemoryMW] Failed to create session: {e}")
        return None

    async def after(
        self,
        ctx: ProcessContext,
        response: OutboundMessage | None,
    ) -> OutboundMessage | None:
        key = ctx.session_key or ctx.msg.session_key
        final_content = response.content if response else None

        if self._memory and key and ctx.session and final_content is not None:
            try:
                self._memory.process_turn(
                    chat_id=key,
                    user_input=ctx.msg.content,
                    assistant_response=final_content,
                    turn_number=0,
                )
                if ctx.session:
                    ctx.session.save()
                self._memory.close_session(key)
            except Exception as e:
                logger.warning(f"[MemoryMW] Process turn failed: {e}")
                try:
                    if self._memory:
                        self._memory.close_session(key)
                except Exception as close_err:
                    logger.warning(f"[MemoryMW] Close session failed: {close_err}")
        elif self._memory and key:
            try:
                self._memory.close_session(key)
            except Exception as e:
                logger.debug(f"[MemoryMW] Close session (no-op): {e}")

        return response

    def handle_failure(self, ctx: ProcessContext) -> None:
        """Call when main handler raises an exception."""
        key = ctx.session_key or ctx.msg.session_key
        if self._memory and key:
            try:
                self._memory.close_session(key)
            except Exception as e:
                logger.warning(f"[MemoryMW] Failure close failed: {e}")
