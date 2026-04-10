"""Abstract base class for markbot memory managers."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from reme.memory.file_based.reme_in_memory_memory import ReMeInMemoryMemory

logger = logging.getLogger(__name__)


class BaseMemoryManager(ABC):
    """Abstract base class defining the memory manager interface.

    All memory manager backends must implement this interface to be usable
    as a drop-in replacement within markbot.

    Concrete implementations are responsible for managing conversation memory,
    including compaction, summarization, semantic search, and lifecycle management.

    Attributes:
        working_dir: Working directory path for memory storage.
        agent_id: Unique agent identifier.
        chat_model: Chat model used for compaction and summarization.
        formatter: Formatter paired with the chat model.
    """

    def __init__(
        self,
        working_dir: str,
        agent_id: str = "default",
    ):
        self.working_dir: str = working_dir
        self.agent_id: str = agent_id
        self.chat_model: Optional[Any] = None
        self.formatter: Optional[Any] = None
        self.summary_tasks: list[asyncio.Task] = []

    @abstractmethod
    async def start(self) -> None:
        """Start the memory manager lifecycle."""

    @abstractmethod
    async def close(self) -> bool:
        """Close the memory manager and perform cleanup."""

    @abstractmethod
    async def compact_tool_result(self, **kwargs) -> None:
        """Compact tool results by truncating large outputs."""

    @abstractmethod
    async def check_context(self, **kwargs) -> tuple:
        """Check context size and determine if compaction is needed.

        Returns:
            Tuple of (messages_to_compact, remaining_messages, is_valid).
        """

    @abstractmethod
    async def compact_memory(
        self,
        messages: list,
        previous_summary: str = "",
        extra_instruction: str = "",
        **kwargs,
    ) -> str:
        """Compact messages into a condensed summary.

        Returns:
            Condensed summary string, or empty string on failure.
        """

    @abstractmethod
    async def summary_memory(self, messages: list, **kwargs) -> str:
        """Generate a comprehensive summary of the given messages.

        Returns:
            Comprehensive summary string.
        """

    @abstractmethod
    async def memory_search(
        self,
        query: str,
        max_results: int = 5,
        min_score: float = 0.1,
    ) -> Any:
        """Search stored memories for relevant content."""

    def get_in_memory_memory(self, **kwargs) -> Optional["ReMeInMemoryMemory"]:
        """Retrieve the in-memory memory object."""
        return None

    def add_async_summary_task(self, messages: list, **kwargs):
        """Add an asynchronous summary task for the given messages."""
        remaining_tasks = []
        for task in self.summary_tasks:
            if task.done():
                if task.cancelled():
                    logger.warning("Summary task was cancelled.")
                    continue
                exc = task.exception()
                if exc is not None:
                    logger.error(f"Summary task failed: {exc}")
                else:
                    logger.info(f"Summary task completed: {task.result()}")
            else:
                remaining_tasks.append(task)
        self.summary_tasks = remaining_tasks

        task = asyncio.create_task(
            self.summary_memory(messages=messages, **kwargs),
        )
        task.add_done_callback(self._on_summary_task_done)
        self.summary_tasks.append(task)

    def _on_summary_task_done(self, task: asyncio.Task) -> None:
        """Callback when a summary task completes — update compressed_summary."""
        try:
            result = task.result()
            if result and hasattr(self, "set_compressed_summary"):
                existing = getattr(self, "_compressed_summary", "")
                updated = f"{existing}\n\n{result}" if existing else result
                self.set_compressed_summary(updated)
                logger.info("[MemoryManager] Summary task result appended to compressed_summary")
        except asyncio.CancelledError:
            logger.warning("[MemoryManager] Summary task was cancelled.")
        except Exception as e:
            logger.error(f"[MemoryManager] Summary task failed: {e}")

    async def await_summary_tasks(self) -> str:
        """Wait for all background summary tasks to complete."""
        result = ""
        for task in self.summary_tasks:
            if task.done():
                if task.cancelled():
                    result += "Summary task was cancelled.\n"
                else:
                    exc = task.exception()
                    if exc is not None:
                        logger.error(f"Summary task failed: {exc}")
                        result += f"Summary task failed: {exc}\n"
                    else:
                        task_result = task.result()
                        logger.info(f"Summary task completed: {task_result}")
                        result += f"Summary task completed: {task_result}\n"
            else:
                try:
                    task_result = await task
                    logger.info(f"Summary task completed: {task_result}")
                    result += f"Summary task completed: {task_result}\n"
                except asyncio.CancelledError:
                    result += "Summary task was cancelled.\n"
                except Exception as e:
                    logger.exception(f"Summary task failed: {e}")
                    result += f"Summary task failed: {e}\n"
        self.summary_tasks.clear()
        return result

    @abstractmethod
    async def restart_embedding_model(self) -> None:
        """Restart the embedding model with current config."""
