"""Abstract base class for markbot memory managers."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from loguru import logger

if TYPE_CHECKING:
    from reme.memory.file_based.reme_in_memory_memory import ReMeInMemoryMemory


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
    ) -> Any:
        """Search stored memories for relevant content."""

    def get_in_memory_memory(self, **kwargs) -> Optional["ReMeInMemoryMemory"]:
        """Retrieve the in-memory memory object."""
        return None

    def get_compressed_summary(self) -> str:
        """Return the current compressed summary string."""
        return getattr(self, "_compressed_summary", "")

    def set_compressed_summary(self, summary: str) -> None:
        """Update the compressed summary string.

        Implementations may override to add persistence or truncation.
        """
        self._compressed_summary = summary

    async def retrieve(
        self,
        messages: list[dict],
        **kwargs,
    ) -> str | None:
        """Retrieve relevant memory based on the given messages.

        Implementations should search for relevant memories and return
        a formatted string for injection into the system prompt.

        Returns:
            Formatted memory context string, or None if no relevant
            memory found.
        """
        return None

    async def dream(self, **kwargs) -> None:
        """Optimize memory files via a background agent pass.

        Runs a lightweight agent with file-editing tools to consolidate
        redundant or outdated memory entries in MEMORY.md.
        Default implementation does nothing.
        """
        return None

    async def _summarize_worker(self) -> None:
        """Background worker that processes summary tasks serially (FIFO)."""
        while True:
            task_id, messages, kwargs = await self._task_queue.get()
            info = self._summary_task_info.get(task_id)
            if info is None:
                continue

            info["status"] = "running"
            logger.info(f"[SummaryWorker] Task {task_id} started")
            try:
                result = await self.summary_memory(messages=messages, **kwargs)
                info["status"] = "completed"
                info["result"] = result
                logger.info(f"[SummaryWorker] Task {task_id} completed")

                if result:
                    existing = self.get_compressed_summary()
                    max_chars = getattr(self, "_MAX_COMPRESSED_SUMMARY_CHARS", 200000)
                    if existing and len(existing) > max_chars * 0.6:
                        updated = result
                        logger.info(
                            "[SummaryWorker] compressed_summary exceeded 60% threshold, "
                            "replacing with latest summary to prevent unbounded growth"
                        )
                    else:
                        updated = f"{existing}\n\n{result}" if existing else result
                    self.set_compressed_summary(updated)
                    logger.info(
                        "[SummaryWorker] Task result appended to compressed_summary"
                    )
            except asyncio.CancelledError:
                info["status"] = "cancelled"
                logger.info(f"[SummaryWorker] Task {task_id} cancelled")
                raise
            except BaseException as e:
                info["status"] = "failed"
                info["error"] = str(e)
                logger.error(f"[SummaryWorker] Task {task_id} failed: {e}")

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
        logger.info(f"[SummaryWorker] Task {task_id} enqueued")

    def _update_task_statuses(self) -> None:
        """Update status for pending/running tasks if worker was cancelled."""
        if self._worker_task is None or not self._worker_task.done():
            return

        for task_id, info in self._summary_task_info.items():
            if info["status"] == "running":
                if self._worker_task.cancelled():
                    info["status"] = "cancelled"
                    logger.info(f"[SummaryWorker] Task {task_id} cancelled (worker stopped)")
                else:
                    exc = self._worker_task.exception()
                    if exc is not None:
                        info["status"] = "failed"
                        info["error"] = str(exc)
                        logger.error(f"[SummaryWorker] Task {task_id} failed: {exc}")

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
                logger.error(f"[SummaryWorker] Worker failed: {e}")

        self._summary_task_info.clear()
        return "\n".join(lines)

    @abstractmethod
    async def restart_embedding_model(self) -> None:
        """Restart the embedding model with current config."""
