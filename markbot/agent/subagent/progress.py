"""Progress tracking system for subagents.

Inspired by MarkBot's AgentTool progress tracking.
Provides real-time progress monitoring, disk output, and activity tracking.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from loguru import logger


@dataclass
class ToolActivity:
    """Record of a tool execution activity."""
    tool_name: str
    input_args: dict[str, Any]
    description: str = ""
    is_search: bool = False
    is_read: bool = False
    timestamp: float = field(default_factory=time.time)


@dataclass
class SubagentProgress:
    """Progress state for a subagent task."""
    task_id: str
    tool_use_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    recent_activities: list[ToolActivity] = field(default_factory=list)
    summary: str = ""
    status: str = "running"  # running, completed, failed, cancelled
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "task_id": self.task_id,
            "tool_use_count": self.tool_use_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "recent_activities": [
                {
                    "tool_name": a.tool_name,
                    "description": a.description,
                    "is_search": a.is_search,
                    "is_read": a.is_read,
                    "timestamp": a.timestamp,
                }
                for a in self.recent_activities
            ],
            "summary": self.summary,
            "status": self.status,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_seconds": self.duration_seconds,
        }
    
    @property
    def duration_seconds(self) -> float:
        """Calculate duration in seconds."""
        end = self.end_time or time.time()
        return end - self.start_time
    
    @property
    def total_tokens(self) -> int:
        """Get total token count."""
        return self.input_tokens + self.output_tokens
    
    @property
    def last_activity(self) -> Optional[ToolActivity]:
        """Get the most recent activity."""
        return self.recent_activities[-1] if self.recent_activities else None


class NullProgressTracker:
    """No-op progress tracker used when SubagentProgressManager is unavailable.

    Implements the same async interface as ProgressTracker so callers
    don't need None checks.
    """

    def __init__(self, task_id: str = "") -> None:
        self.task_id = task_id
        self._progress = SubagentProgress(task_id=task_id or "null")

    async def start(self, description: str = "") -> Optional[Path]:
        return None

    async def record_tool_use(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def record_tokens(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        pass

    async def update_summary(self, summary: str = "") -> None:
        pass

    async def complete(self, result: str = "") -> None:
        self._progress.status = "completed"
        self._progress.end_time = time.time()

    async def fail(self, error: str = "") -> None:
        self._progress.status = "failed"
        self._progress.end_time = time.time()

    async def cancel(self) -> None:
        self._progress.status = "cancelled"
        self._progress.end_time = time.time()

    def get_progress(self) -> SubagentProgress:
        return SubagentProgress(
            task_id=self._progress.task_id,
            tool_use_count=self._progress.tool_use_count,
            input_tokens=self._progress.input_tokens,
            output_tokens=self._progress.output_tokens,
            recent_activities=[],
            summary=self._progress.summary,
            status=self._progress.status,
            start_time=self._progress.start_time,
            end_time=self._progress.end_time,
        )

    @property
    def output_file(self) -> Optional[Path]:
        return None


class ProgressTracker:
    """Tracks progress for a single subagent task."""
    
    MAX_RECENT_ACTIVITIES = 5
    
    def __init__(self, task_id: str, output_dir: Path):
        self.task_id = task_id
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        self._progress = SubagentProgress(task_id=task_id)
        self._output_file: Optional[Path] = None
        self._file_handle: Optional[Any] = None
        self._write_queue: asyncio.Queue[str] = asyncio.Queue()
        self._drain_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        
    async def start(self, description: str) -> Path:
        """Start tracking and create output file."""
        self._output_file = self.output_dir / f"{self.task_id}.output"
        self._file_handle = open(self._output_file, "w", encoding="utf-8")
        
        # Write header
        header = f"""# Subagent Task: {description}
# Task ID: {self.task_id}
# Started: {time.strftime('%Y-%m-%d %H:%M:%S')}
# Status: running

"""
        await self._write(header)
        
        # Start drain task
        self._drain_task = asyncio.create_task(self._drain_loop())
        
        logger.debug(f"Progress tracking started for task {self.task_id}")
        return self._output_file
    
    async def record_tool_use(
        self,
        tool_name: str,
        input_args: dict[str, Any],
        description: str = "",
        is_search: bool = False,
        is_read: bool = False,
    ) -> None:
        """Record a tool usage activity."""
        async with self._lock:
            self._progress.tool_use_count += 1
            
            activity = ToolActivity(
                tool_name=tool_name,
                input_args=input_args,
                description=description or f"Using {tool_name}",
                is_search=is_search,
                is_read=is_read,
            )
            
            self._progress.recent_activities.append(activity)
            
            # Keep only recent activities
            while len(self._progress.recent_activities) > self.MAX_RECENT_ACTIVITIES:
                self._progress.recent_activities.pop(0)
            
            # Log to output file
            log_entry = f"[{time.strftime('%H:%M:%S')}] {activity.description}\n"
            await self._write(log_entry)
            
            logger.debug(f"Task {self.task_id}: {activity.description}")
    
    async def record_tokens(self, input_tokens: int, output_tokens: int) -> None:
        """Record token usage."""
        async with self._lock:
            # Claude API: input_tokens is cumulative, output_tokens is per-turn
            self._progress.input_tokens = input_tokens
            self._progress.output_tokens += output_tokens
    
    async def update_summary(self, summary: str) -> None:
        """Update progress summary."""
        async with self._lock:
            self._progress.summary = summary
            await self._write(f"\n[Progress] {summary}\n")
    
    async def complete(self, result: str) -> None:
        """Mark task as completed."""
        async with self._lock:
            self._progress.status = "completed"
            self._progress.end_time = time.time()
            
            footer = f"""

# Task Completed
# Duration: {self._progress.duration_seconds:.1f}s
# Total Tokens: {self._progress.total_tokens}
# Tool Uses: {self._progress.tool_use_count}

## Result
{result}
"""
            await self._write(footer)
            await self._flush()
            
            logger.info(f"Task {self.task_id} completed in {self._progress.duration_seconds:.1f}s")
    
    async def fail(self, error: str) -> None:
        """Mark task as failed."""
        async with self._lock:
            self._progress.status = "failed"
            self._progress.end_time = time.time()
            
            footer = f"""

# Task Failed
# Duration: {self._progress.duration_seconds:.1f}s
# Error: {error}
"""
            await self._write(footer)
            await self._flush()
            
            logger.error(f"Task {self.task_id} failed: {error}")
    
    async def cancel(self) -> None:
        """Mark task as cancelled."""
        async with self._lock:
            self._progress.status = "cancelled"
            self._progress.end_time = time.time()
            
            footer = f"""

# Task Cancelled
# Duration: {self._progress.duration_seconds:.1f}s
"""
            await self._write(footer)
            await self._flush()
    
    def get_progress(self) -> SubagentProgress:
        """Get current progress snapshot."""
        return SubagentProgress(
            task_id=self._progress.task_id,
            tool_use_count=self._progress.tool_use_count,
            input_tokens=self._progress.input_tokens,
            output_tokens=self._progress.output_tokens,
            recent_activities=list(self._progress.recent_activities),
            summary=self._progress.summary,
            status=self._progress.status,
            start_time=self._progress.start_time,
            end_time=self._progress.end_time,
        )
    
    async def _write(self, content: str) -> None:
        """Queue content for writing."""
        await self._write_queue.put(content)
    
    async def _drain_loop(self) -> None:
        """Background task to drain write queue to disk."""
        try:
            while True:
                try:
                    # Wait for content with timeout
                    content = await asyncio.wait_for(
                        self._write_queue.get(),
                        timeout=1.0
                    )
                    if self._file_handle and not self._file_handle.closed:
                        self._file_handle.write(content)
                        self._file_handle.flush()
                except asyncio.TimeoutError:
                    # No new content, just flush
                    if self._file_handle and not self._file_handle.closed:
                        self._file_handle.flush()
                except asyncio.CancelledError:
                    break
        except Exception as e:
            logger.error(f"Drain loop error for task {self.task_id}: {e}")
    
    async def _flush(self) -> None:
        """Flush remaining content and close file."""
        # Cancel drain task
        if self._drain_task and not self._drain_task.done():
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
        
        # Write remaining queue content
        while not self._write_queue.empty():
            try:
                content = self._write_queue.get_nowait()
                if self._file_handle and not self._file_handle.closed:
                    self._file_handle.write(content)
            except asyncio.QueueEmpty:
                break
        
        # Close file
        if self._file_handle:
            self._file_handle.close()
            self._file_handle = None
    
    @property
    def output_file(self) -> Optional[Path]:
        """Get output file path."""
        return self._output_file


class SubagentProgressManager:
    """Manages progress tracking for all subagents."""
    
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.output_dir = workspace / ".markbot" / "tasks"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        self._trackers: dict[str, ProgressTracker] = {}
        self._lock = asyncio.Lock()
    
    async def create_tracker(self, task_id: str, description: str) -> ProgressTracker:
        """Create a new progress tracker for a task."""
        async with self._lock:
            if task_id in self._trackers:
                logger.warning(f"Tracker already exists for task {task_id}")
                return self._trackers[task_id]
            
            tracker = ProgressTracker(task_id, self.output_dir)
            await tracker.start(description)
            self._trackers[task_id] = tracker
            
            return tracker
    
    def get_tracker(self, task_id: str) -> Optional[ProgressTracker]:
        """Get tracker for a task."""
        return self._trackers.get(task_id)
    
    def get_progress(self, task_id: str) -> Optional[SubagentProgress]:
        """Get progress for a task."""
        tracker = self._trackers.get(task_id)
        return tracker.get_progress() if tracker else None
    
    async def remove_tracker(self, task_id: str) -> None:
        """Remove a tracker."""
        async with self._lock:
            if task_id in self._trackers:
                tracker = self._trackers.pop(task_id)
                # Ensure file is closed
                if tracker._file_handle and not tracker._file_handle.closed:
                    await tracker._flush()
    
    def list_active_tasks(self) -> list[SubagentProgress]:
        """List all active (running) tasks."""
        return [
            tracker.get_progress()
            for tracker in self._trackers.values()
            if tracker.get_progress().status == "running"
        ]
    
    def get_output_file(self, task_id: str) -> Optional[Path]:
        """Get output file path for a task.
        
        Returns the path even if the tracker has been removed,
        allowing access to completed task outputs.
        """
        # First check if tracker exists
        tracker = self._trackers.get(task_id)
        if tracker and tracker.output_file:
            return tracker.output_file
        
        # Fallback: construct path directly (for completed tasks)
        output_file = self.output_dir / f"{task_id}.output"
        if output_file.exists():
            return output_file
        
        return None
    
    async def read_output(self, task_id: str, max_bytes: int = 100_000) -> str:
        """Read output file for a task.
        
        Works even after the tracker has been removed,
        as long as the output file exists on disk.
        """
        output_file = self.get_output_file(task_id)
        if not output_file or not output_file.exists():
            return ""
        
        try:
            content = output_file.read_text(encoding="utf-8")
            if len(content) > max_bytes:
                # Return last max_bytes with note
                truncated = content[-max_bytes:]
                return f"... [truncated, showing last {max_bytes} chars]\n\n{truncated}"
            return content
        except Exception as e:
            logger.error(f"Failed to read output for task {task_id}: {e}")
            return f"[Error reading output: {e}]"
    
    async def tail_output(self, task_id: str, lines: int = 50) -> str:
        """Get last N lines of output.
        
        Works even after the tracker has been removed,
        as long as the output file exists on disk.
        """
        output_file = self.get_output_file(task_id)
        if not output_file or not output_file.exists():
            return ""
        
        try:
            content = output_file.read_text(encoding="utf-8")
            all_lines = content.split("\n")
            return "\n".join(all_lines[-lines:])
        except Exception as e:
            logger.error(f"Failed to tail output for task {task_id}: {e}")
            return f"[Error reading output: {e}]"
    
    def get_task_summary(self, task_id: str) -> Optional[dict[str, Any]]:
        """Get a summary of a task from its output file.
        
        Used when the tracker has been removed but we need basic info.
        """
        output_file = self.get_output_file(task_id)
        if not output_file or not output_file.exists():
            return None
        
        try:
            content = output_file.read_text(encoding="utf-8")
            
            # Parse basic info from the output file
            summary = {
                "task_id": task_id,
                "output_file": str(output_file),
                "file_size": len(content),
            }
            
            # Try to extract status from the file
            if "# Task Completed" in content:
                summary["status"] = "completed"
            elif "# Task Failed" in content:
                summary["status"] = "failed"
            elif "# Task Cancelled" in content:
                summary["status"] = "cancelled"
            else:
                summary["status"] = "unknown"
            
            return summary
        except Exception as e:
            logger.error(f"Failed to get task summary for {task_id}: {e}")
            return None
