"""Task lifecycle tracker — lightweight task state machine for personal assistant.

Provides a general-purpose task tracking primitive that enforces:
- Explicit state transitions (stated → in_progress → verifying → done/failed)
- Verification gate before marking a task as done
- Scope constraint: one active task at a time (configurable)
- Integration with session handoff for cross-session continuity

This is a simplified, session-friendly version of autopilot's TaskCard,
designed for interactive use within the agent loop rather than batch processing.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from markbot.utils.helpers import ensure_dir

TaskStatus = Literal[
    "stated",
    "in_progress",
    "verifying",
    "done",
    "failed",
    "blocked",
    "cancelled",
]

VALID_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    "stated": {"in_progress", "cancelled"},
    "in_progress": {"verifying", "blocked", "failed", "cancelled"},
    "verifying": {"done", "in_progress", "failed"},
    "done": set(),
    "failed": {"in_progress", "cancelled"},
    "blocked": {"in_progress", "cancelled"},
    "cancelled": set(),
}


@dataclass
class Task:
    id: str = ""
    title: str = ""
    description: str = ""
    status: TaskStatus = "stated"
    priority: int = 0
    category: str = ""
    verification_method: str = ""
    verification_result: str = ""
    progress: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    completed_at: float | None = None
    repair_count: int = 0
    max_repairs: int = 2
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid.uuid4().hex[:8]
        now = time.time()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now


@dataclass
class TaskRegistry:
    version: int = 1
    active_task_id: str | None = None
    tasks: list[dict[str, Any]] = field(default_factory=list)
    updated_at: float = 0.0


class TaskTracker:
    """Manages task lifecycle within a workspace.

    Persistence: ``workspace/tasks/registry.json``
    Scope: by default, only one task can be ``in_progress`` at a time.
    """

    _TASKS_DIR = "tasks"
    _REGISTRY_FILE = "registry.json"
    _MAX_ACTIVE_TASKS = 1

    def __init__(self, workspace: Path, max_active_tasks: int = 1) -> None:
        self._workspace = workspace
        self._tasks_dir = workspace / self._TASKS_DIR
        self._max_active_tasks = max(1, max_active_tasks)
        ensure_dir(self._tasks_dir)
        self._registry_path = self._tasks_dir / self._REGISTRY_FILE
        self._cache: dict[str, Task] | None = None

    def _load_registry(self) -> TaskRegistry:
        if not self._registry_path.exists():
            return TaskRegistry()
        try:
            data = json.loads(self._registry_path.read_text(encoding="utf-8"))
            return TaskRegistry(
                version=data.get("version", 1),
                active_task_id=data.get("active_task_id"),
                tasks=data.get("tasks", []),
                updated_at=data.get("updated_at", 0.0),
            )
        except Exception as e:
            logger.warning("Failed to load registry: {}", e)
            return TaskRegistry()

    def _save_registry(self, registry: TaskRegistry) -> None:
        registry.updated_at = time.time()
        data = {
            "version": registry.version,
            "active_task_id": registry.active_task_id,
            "tasks": registry.tasks,
            "updated_at": registry.updated_at,
        }
        self._registry_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self._cache = None

    def _invalidate_cache(self) -> None:
        self._cache = None

    def _get_task(self, task_id: str) -> Task | None:
        if self._cache is not None and task_id in self._cache:
            return self._cache[task_id]
        registry = self._load_registry()
        for t_dict in registry.tasks:
            if t_dict.get("id") == task_id:
                return Task(**{k: v for k, v in t_dict.items() if k in Task.__dataclass_fields__})
        return None

    def _save_task(self, task: Task) -> None:
        registry = self._load_registry()
        task.updated_at = time.time()
        task_dict = asdict(task)

        found = False
        for i, t in enumerate(registry.tasks):
            if t.get("id") == task.id:
                registry.tasks[i] = task_dict
                found = True
                break
        if not found:
            registry.tasks.append(task_dict)

        if task.status == "in_progress":
            registry.active_task_id = task.id
        elif registry.active_task_id == task.id and task.status != "in_progress":
            registry.active_task_id = None

        self._save_registry(registry)
        if self._cache is None:
            self._cache = {}
        self._cache[task.id] = task

    def create_task(
        self,
        title: str,
        *,
        description: str = "",
        priority: int = 0,
        category: str = "",
        verification_method: str = "",
    ) -> Task:
        task = Task(
            title=title,
            description=description,
            priority=priority,
            category=category,
            verification_method=verification_method,
        )
        self._save_task(task)
        logger.info("Created task {}: {}", task.id, title)
        return task

    def transition(self, task_id: str, new_status: TaskStatus, **kwargs: Any) -> Task:
        task = self._get_task(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")

        old_status = task.status
        if new_status not in VALID_TRANSITIONS.get(old_status, set()):
            raise ValueError(
                f"Invalid transition: {old_status} → {new_status} "
                f"(valid: {VALID_TRANSITIONS.get(old_status, set())})"
            )

        if new_status == "in_progress":
            active = self.list_active()
            active_other = [t for t in active if t.id != task_id]
            if len(active_other) >= self._max_active_tasks:
                raise ValueError(
                    f"Cannot start task {task_id}: already have "
                    f"{len(active_other)} active task(s). "
                    f"Complete or block current task first."
                )

        task.status = new_status
        if new_status == "done":
            task.completed_at = time.time()
        if new_status == "in_progress" and old_status == "verifying":
            task.repair_count += 1

        for k, v in kwargs.items():
            if hasattr(task, k):
                setattr(task, k, v)

        self._save_task(task)
        logger.info(
            "Task {} transitioned: {} → {}",
            task_id, old_status, new_status,
        )
        return task

    def start_task(self, task_id: str, **kwargs: Any) -> Task:
        return self.transition(task_id, "in_progress", **kwargs)

    def submit_for_verification(self, task_id: str, *, progress: str = "", **kwargs: Any) -> Task:
        return self.transition(task_id, "verifying", progress=progress, **kwargs)

    def mark_done(self, task_id: str, *, verification_result: str = "", **kwargs: Any) -> Task:
        return self.transition(
            task_id, "done",
            verification_result=verification_result or "passed",
            **kwargs,
        )

    def mark_failed(self, task_id: str, *, reason: str = "", **kwargs: Any) -> Task:
        task = self._get_task(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")

        if task.repair_count < task.max_repairs:
            return self.transition(
                task_id, "in_progress",
                progress=f"Repair attempt {task.repair_count + 1}: {reason}",
                **kwargs,
            )
        return self.transition(task_id, "failed", progress=reason, **kwargs)

    def block_task(self, task_id: str, *, reason: str = "", **kwargs: Any) -> Task:
        return self.transition(task_id, "blocked", progress=reason, **kwargs)

    def cancel_task(self, task_id: str, **kwargs: Any) -> Task:
        task = self._get_task(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        if task.status in ("done", "cancelled"):
            raise ValueError(f"Cannot cancel task in {task.status} state")
        return self.transition(task_id, "cancelled", **kwargs)

    def list_all(self) -> list[Task]:
        registry = self._load_registry()
        tasks: list[Task] = []
        for t_dict in registry.tasks:
            try:
                tasks.append(Task(**{k: v for k, v in t_dict.items() if k in Task.__dataclass_fields__}))
            except Exception:
                pass
        return sorted(tasks, key=lambda t: (-t.priority, -t.updated_at))

    def list_active(self) -> list[Task]:
        return [t for t in self.list_all() if t.status in ("in_progress", "verifying")]

    def list_blocked(self) -> list[Task]:
        return [t for t in self.list_all() if t.status == "blocked"]

    def list_pending(self) -> list[Task]:
        return [t for t in self.list_all() if t.status == "stated"]

    def list_completed(self) -> list[Task]:
        return [t for t in self.list_all() if t.status == "done"]

    def get_active_task(self) -> Task | None:
        active = self.list_active()
        return active[0] if active else None

    def get_next_pending(self) -> Task | None:
        pending = self.list_pending()
        return pending[0] if pending else None

    def get_summary(self) -> dict[str, int]:
        tasks = self.list_all()
        counts: dict[str, int] = {}
        for t in tasks:
            counts[t.status] = counts.get(t.status, 0) + 1
        counts["total"] = len(tasks)
        return counts

    def cleanup_completed(self, max_age_days: int = 30) -> int:
        now = time.time()
        cutoff = now - max_age_days * 86400
        registry = self._load_registry()
        before = len(registry.tasks)
        registry.tasks = [
            t for t in registry.tasks
            if t.get("status") not in ("done", "cancelled")
            or (t.get("completed_at") or t.get("updated_at", 0)) > cutoff
        ]
        removed = before - len(registry.tasks)
        if removed > 0:
            self._save_registry(registry)
            logger.info("Cleaned up {} completed tasks", removed)
        return removed
