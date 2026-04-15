"""Heartbeat service - periodic agent wake-up to check for tasks."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from loguru import logger

if TYPE_CHECKING:
    from markbot.providers.base import LLMProvider
    from typing import Callable, Coroutine

_HEARTBEAT_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "heartbeat",
            "description": "Report heartbeat decision after reviewing tasks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["skip", "run"],
                        "description": "skip = nothing to do, run = has active tasks",
                    },
                    "tasks": {
                        "type": "string",
                        "description": "Natural-language summary of active tasks (required for run)",
                    },
                },
                "required": ["action"],
            },
        },
    }
]


class HeartbeatService:
    """
    Periodic heartbeat service that wakes the agent to check for tasks.

    Phase 1 (decision): reads HEARTBEAT.md and asks the LLM — via a virtual
    tool call — whether there are active tasks.  This avoids free-text parsing
    and the unreliable HEARTBEAT_OK token.

    Phase 2 (execution): only triggered when Phase 1 returns ``run``.  The
    ``on_execute`` callback runs the task through the full agent loop and
    returns the result to deliver.
    """

    def __init__(
        self,
        workspace: Path,
        fallback_manager=None,
        model: str | None = None,
        on_execute: Callable[[str], Coroutine[Any, Any, str]] | None = None,
        on_notify: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        interval_s: int = 30 * 60,
        enabled: bool = True,
        timezone: str | None = None,
        memory_summarizer: "MemorySummarizer | Callable[[], Coroutine[Any, Any, bool]] | None" = None,
    ):
        self.workspace = workspace
        self.fallback_manager = fallback_manager
        self.model = model or "unknown"
        self.on_execute = on_execute
        self.on_notify = on_notify
        self.interval_s = interval_s
        self.enabled = enabled
        self.timezone = timezone
        self.memory_summarizer = memory_summarizer
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_summarization_date: str | None = None

    @property
    def heartbeat_file(self) -> Path:
        return self.workspace / "HEARTBEAT.md"

    def _read_heartbeat_file(self) -> str | None:
        if self.heartbeat_file.exists():
            try:
                text = self.heartbeat_file.read_text(encoding="utf-8")
                if text.strip():
                    return text
            except Exception:
                pass
        return None

    async def _decide(self, content: str) -> tuple[str, str]:
        """Phase 1: ask LLM to decide skip/run via virtual tool call.

        Returns (action, tasks) where action is 'skip' or 'run'.
        """
        from markbot.utils.helpers import current_time_str

        response, _ = await self.fallback_manager.chat_with_fallback(
            messages=[
                {"role": "system", "content": "You are a heartbeat agent. Call the heartbeat tool to report your decision."},
                {"role": "user", "content": (
                    f"Current Time: {current_time_str(self.timezone)}\n\n"
                    "Review the following HEARTBEAT.md and decide whether there are active tasks.\n\n"
                    f"{content}"
                )},
            ],
            tools=_HEARTBEAT_TOOL,
        )

        if not response.has_tool_calls:
            return "skip", ""

        args = response.tool_calls[0].arguments
        return args.get("action", "skip"), args.get("tasks", "")

    async def _maybe_summarize_memory(self) -> None:
        """Check if it's time to summarize memory and trigger if so."""
        if not self.memory_summarizer:
            return

        # Parse memory_summarize_times from HEARTBEAT.md (e.g., "memory_summarize_times: 12:00, 23:59")
        summarize_times = self._parse_memory_summarize_times()
        if not summarize_times:
            return

        now = datetime.now()
        current_time = now.time()
        current_date = now.strftime("%Y-%m-%d")

        # Check if current time matches any configured time (within a 5-minute window)
        matched_time = None
        for t in summarize_times:
            target_hour, target_minute = map(int, t.split(":"))
            if (current_time.hour == target_hour and
                abs(current_time.minute - target_minute) <= 5):
                matched_time = t
                break

        if not matched_time:
            return

        # Check if already ran today at this time
        run_key = f"{current_date}_{matched_time}"
        last_run_key = getattr(self, '_last_memory_summarize_run', None)
        if last_run_key == run_key:
            return

        logger.info(f"Heartbeat: triggering memory summarization (time={matched_time})")
        try:
            import inspect
            _summarizer = self.memory_summarizer
            if inspect.iscoroutinefunction(_summarizer):
                success = await _summarizer()
            elif hasattr(_summarizer, 'summarize_today'):
                success = await _summarizer.summarize_today()
            else:
                success = False
            if success:
                self._last_memory_summarize_run = run_key
                logger.info(f"Heartbeat: memory summarization completed for time={matched_time}")
        except Exception:
            logger.exception("Heartbeat: memory summarization failed")

    def _parse_memory_summarize_times(self) -> list[str] | None:
        """Parse memory_summarize_times from HEARTBEAT.md content.

        Looks for a line like: memory_summarize_times: 12:00, 23:59

        Returns:
            List of time strings like ["12:00", "23:59"], or None if not found.
        """
        content = self._read_heartbeat_file()
        if not content:
            return None

        for line in content.split("\n"):
            line = line.strip()
            if line.startswith("memory_summarize_times:"):
                times_str = line.split(":", 1)[1].strip()
                times = [t.strip() for t in times_str.split(",") if t.strip()]
                if times:
                    logger.debug(f"Parsed memory_summarize_times: {times}")
                    return times
        return None

    async def start(self) -> None:
        """Start the heartbeat service."""
        if not self.enabled:
            logger.info("Heartbeat disabled")
            return
        if self._running:
            logger.warning("Heartbeat already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Heartbeat started (every {}s)", self.interval_s)

    async def stop(self) -> None:
        """Stop the heartbeat service and wait for the task to finish."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run_loop(self) -> None:
        """Main heartbeat loop."""
        while self._running:
            try:
                await asyncio.sleep(self.interval_s)
                if self._running:
                    await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Heartbeat error: {}", e)

    async def _tick(self) -> None:
        """Execute a single heartbeat tick."""
        from markbot.utils.evaluator import evaluate_response

        # Check if it's time to summarize memory (12:00 or 23:59)
        await self._maybe_summarize_memory()

        content = self._read_heartbeat_file()
        if not content:
            logger.debug("Heartbeat: HEARTBEAT.md missing or empty")
            return

        logger.info("Heartbeat: checking for tasks...")

        try:
            action, tasks = await self._decide(content)

            if action != "run":
                logger.info("Heartbeat: OK (nothing to report)")
                return

            logger.info("Heartbeat: tasks found, executing...")
            if self.on_execute:
                response = await self.on_execute(tasks)

                if response:
                    should_notify = await evaluate_response(
                        response, tasks, self.fallback_manager, self.model,
                    )
                    if should_notify and self.on_notify:
                        logger.info("Heartbeat: completed, delivering response")
                        await self.on_notify(response)
                    else:
                        logger.info("Heartbeat: silenced by post-run evaluation")
        except Exception:
            logger.exception("Heartbeat execution failed")

    async def trigger_now(self) -> str | None:
        """Manually trigger a heartbeat (full pipeline including evaluate & notify)."""
        from markbot.utils.evaluator import evaluate_response

        content = self._read_heartbeat_file()
        if not content:
            return None
        action, tasks = await self._decide(content)
        if action != "run" or not self.on_execute:
            return None
        response = await self.on_execute(tasks)
        if response and self.on_notify:
            should_notify = await evaluate_response(
                response, tasks, self.fallback_manager, self.model,
            )
            if should_notify:
                await self.on_notify(response)
        return response
