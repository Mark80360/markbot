"""Abstract base class for markbot memory managers."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from loguru import logger

if TYPE_CHECKING:
    pass


class BaseMemoryManager(ABC):
    """Abstract base class defining the memory manager interface.

    All memory manager backends must implement this interface to be usable
    as a drop-in replacement within markbot.

    Concrete implementations are responsible for managing conversation memory,
    including compaction, summarization, semantic search, and lifecycle management.

    Summary tasks are processed via a **serial FIFO queue** backed by a
    single background worker.  This eliminates race conditions between
    concurrent summary tasks without needing version-number guards.

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

        self._task_counter: int = 0
        self._summary_task_info: dict[str, dict[str, Any]] = {}
        self._task_queue: asyncio.Queue[tuple[str, list, dict]] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None

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
        *,
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> Any:
        """Search stored memories for relevant content.

        Args:
            query: Search query string.
            max_results: Maximum number of results.
            min_score: Minimum relevance score.
            channel: Optional channel filter for session-scoped search.
            chat_id: Optional chat ID filter for session-scoped search.
        """

    def get_in_memory_memory(self, **kwargs) -> Optional["Any"]:
        """Retrieve the in-memory memory object."""
        return None

    def get_compressed_summary(self, *, session_key: str | None = None) -> str:
        """Return the current compressed summary string.

        Args:
            session_key: Optional session key for per-session summary.
                When provided, implementations should return the summary
                specific to that session.  When None, returns the global
                or default summary.
        """
        return getattr(self, "_compressed_summary", "")

    def set_compressed_summary(
        self,
        summary: str,
        *,
        session_key: str | None = None,
    ) -> None:
        """Update the compressed summary string.

        Implementations may override to add persistence or truncation.

        Args:
            summary: The new compressed summary text.
            session_key: Optional session key for per-session summary.
        """
        self._compressed_summary = summary

    # -- Background summary worker (serial FIFO queue) -----------------------

    async def _summarize_worker(self) -> None:
        """Background worker that processes summary tasks serially.

        Processes tasks from the FIFO queue one at a time.  This keeps
        the two stores separate avoids semantic overlap and duplicate
        token consumption.
        """
        while True:
            task_id, messages, kwargs = await self._task_queue.get()
            info = self._summary_task_info.get(task_id)
            if info is None:
                continue

            info["status"] = "running"
            logger.info("Task {} started", task_id)
            try:
                result = await self.summary_memory(messages=messages, **kwargs)
                info["status"] = "completed"
                info["result"] = result
                logger.info("Task {} completed", task_id)
            except asyncio.CancelledError:
                info["status"] = "cancelled"
                logger.info("Task {} cancelled", task_id)
                raise
            except BaseException as e:
                info["status"] = "failed"
                info["error"] = str(e)
                logger.error("Task {} failed: {}", task_id, e)

    def add_async_summary_task(self, messages: list, **kwargs):
        """Schedule a background summarization task without blocking.

        Tasks are executed serially in FIFO order.  If no worker is
        running, one is started immediately; otherwise the task queues.
        """
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._summarize_worker())

        self._task_counter += 1
        task_id = f"summary_{self._task_counter}"

        self._summary_task_info[task_id] = {
            "task_id": task_id,
            "start_time": datetime.now(),
            "status": "pending",
            "result": None,
            "error": None,
        }

        self._task_queue.put_nowait((task_id, messages, kwargs))
        logger.info("Task {} enqueued", task_id)

    def _update_task_statuses(self) -> None:
        """Update status for pending/running tasks if worker was cancelled."""
        if self._worker_task is None or not self._worker_task.done():
            return

        for task_id, info in self._summary_task_info.items():
            if info["status"] == "running":
                if self._worker_task.cancelled():
                    info["status"] = "cancelled"
                    logger.info("Task {} cancelled (worker stopped)", task_id)
                else:
                    exc = self._worker_task.exception()
                    if exc is not None:
                        info["status"] = "failed"
                        info["error"] = str(exc)
                        logger.error("Task {} failed: {}", task_id, exc)

    def list_summarize_status(self) -> list[dict]:
        """Return status of all summary tasks as list of dicts.

        Each dict contains:
            - task_id: Unique identifier
            - start_time: When the task was enqueued
            - status: "pending", "running", "completed", "failed", or "cancelled"
            - result: Summary result (if completed)
            - error: Error message (if failed)
        """
        self._update_task_statuses()
        result = []
        for _task_id, info in self._summary_task_info.items():
            result.append(
                {
                    "task_id": info["task_id"],
                    "start_time": info["start_time"].isoformat(),
                    "status": info["status"],
                    "result": info["result"],
                    "error": info["error"],
                }
            )
        return result

    async def await_summary_tasks(self) -> str:
        """Wait for all queued summary tasks to complete."""
        lines: list[str] = []
        self._update_task_statuses()

        for task_id, info in self._summary_task_info.items():
            status = info["status"]
            if status in ("completed", "failed", "cancelled"):
                if status == "completed":
                    lines.append(f"Summary task {task_id} completed")
                elif status == "failed":
                    lines.append(f"Summary task {task_id} failed: {info['error']}")
                else:
                    lines.append(f"Summary task {task_id} cancelled")
            else:
                lines.append(f"Summary task {task_id} still {status}")

        if self._worker_task and not self._worker_task.done():
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error("Worker failed: {}", e)

        self._summary_task_info.clear()
        return "\n".join(lines)

    @abstractmethod
    async def restart_embedding_model(self) -> None:
        """Restart the embedding model with current config."""

    # -- Lifecycle hooks -----------------------------------------------------

    def prefetch(
        self,
        query: str,
        *,
        session_id: str = "",
    ) -> str:
        """Recall relevant context for the upcoming turn.

        Called before each API call. Return formatted text to inject as
        context, or empty string if nothing relevant. Implementations
        should be fast — use background threads for the actual recall
        and return cached results here.

        Default implementation returns empty string. Override in concrete
        managers to provide prefetch recall.

        Args:
            query: The user message text to use as the search query.
            session_id: Optional session identifier for scoped recall.

        Returns:
            Formatted context string, or empty string if nothing relevant.
        """
        return ""

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
    ) -> None:
        """Persist a completed turn to the backend.

        Called after each turn. Use to store conversation history,
        update embeddings, or trigger async processing.

        Default implementation is no-op. Override in concrete managers
        to persist turn data.

        Args:
            user_content: The user's message content.
            assistant_content: The assistant's response content.
            session_id: Optional session identifier for scoped storage.
        """

    def queue_prefetch(
        self,
        query: str,
        *,
        session_id: str = "",
    ) -> None:
        """Queue a background recall for the NEXT turn.

        Called after each turn completes. The result will be consumed
        by prefetch() on the next turn. Default is no-op — providers
        that do background prefetching should override this.

        Args:
            query: The user message text to use as the search query.
            session_id: Optional session identifier for scoped recall.
        """

    def on_delegation(
        self,
        task: str,
        result: str,
        *,
        child_session_id: str = "",
        **kwargs,
    ) -> None:
        """Called on the PARENT agent when a subagent completes.

        The parent's memory manager gets the task+result pair as an
        observation of what was delegated and what came back.

        Default implementation is no-op. Override to persist subagent
        results as memory entries.

        Args:
            task: The delegation prompt.
            result: The subagent's final response.
            child_session_id: The subagent's session_id.
        """

    def get_memory_context(self, query: str | None = None, *, session_key: str | None = None) -> str:
        """Get formatted memory context for system prompt injection.

        Returns a string containing the compressed summary, recent
        memory entries, and other relevant context. The result is
        suitable for injection into the system prompt.

        Args:
            query: Optional search query to scope the context.
            session_key: Optional session key for session-scoped summary.

        Returns:
            Formatted memory context string, or empty string.
        """
        return ""

