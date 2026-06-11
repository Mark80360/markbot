"""Message processing pipeline with composable middleware."""

from markbot.agent.pipeline.engine import MessagePipeline, Middleware, ProcessContext
from markbot.agent.pipeline.middleware import (
    MemoryLifecycleMiddleware,
    QuestionResponseMiddleware,
)

__all__ = [
    "MessagePipeline",
    "Middleware",
    "ProcessContext",
    "QuestionResponseMiddleware",
    "MemoryLifecycleMiddleware",
]
