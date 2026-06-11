"""Message processing pipeline with composable middleware.

Provides a middleware chain around message handling, allowing cross-cutting
concerns (question routing, memory lifecycle, error handling) to be separated
from the core agent logic in AgentLoop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from loguru import logger

from markbot.bus.events import InboundMessage, OutboundMessage


@dataclass
class ProcessContext:
    """Shared context passed through the pipeline."""

    msg: InboundMessage
    session_key: str | None = None
    session: Any = None
    channel: str = ""
    chat_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class Middleware(Protocol):
    """Protocol for pipeline middleware."""

    async def before(self, ctx: ProcessContext) -> OutboundMessage | None:
        """Run before main handler. Return a response to short-circuit."""
        ...

    async def after(
        self, ctx: ProcessContext, response: OutboundMessage | None
    ) -> OutboundMessage | None:
        """Run after main handler. Can modify or replace response."""
        ...

    async def on_error(self, ctx: ProcessContext, error: Exception) -> None:
        """Called when the handler or a later middleware raises an exception."""
        ...


class MessagePipeline:
    """Composable message processing pipeline.

    Middleware are executed in order for `before` hooks (LIFO for `after`).
    Any middleware can short-circuit by returning an OutboundMessage from `before`.
    """

    def __init__(self):
        self._middlewares: list[Middleware] = []

    def use(self, middleware: Middleware) -> "MessagePipeline":
        """Add a middleware to the pipeline. Returns self for chaining."""
        self._middlewares.append(middleware)
        return self

    async def process(
        self,
        ctx: ProcessContext,
        handler: Callable[[ProcessContext], Awaitable[OutboundMessage | None]],
    ) -> OutboundMessage | None:
        """Process message through all middleware and handler.

        Args:
            ctx: Processing context with message and state
            handler: Main handler function

        Returns:
            Final outbound message or None
        """
        for mw in self._middlewares:
            try:
                result = await mw.before(ctx)
                if result is not None:
                    logger.debug("Short-circuited by middleware")
                    return result
            except Exception as e:
                logger.error("Middleware before hook failed: {}", e)
                try:
                    await mw.on_error(ctx, e)
                except Exception as on_err_exc:
                    logger.debug("Middleware on_error hook also failed: {}", on_err_exc)

        try:
            response = await handler(ctx)
        except Exception as e:
            logger.error("Handler failed: {}", e)
            for mw in reversed(self._middlewares):
                try:
                    await mw.on_error(ctx, e)
                except Exception as on_err_exc:
                    logger.debug("Middleware on_error hook also failed: {}", on_err_exc)
            raise

        for mw in reversed(self._middlewares):
            try:
                response = await mw.after(ctx, response)
            except Exception as e:
                logger.warning("Middleware after hook failed: {}", e)

        return response

    @property
    def middleware_count(self) -> int:
        return len(self._middlewares)
