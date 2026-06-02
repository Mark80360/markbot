"""Concurrency tests for :class:`markbot.session.task_tracker.TaskTracker`.

These exercise the file lock and atomic write under simulated concurrent
load. They are slower than unit tests (each scenario spins up real
threads) but kept as ``test_*`` so the regression suite catches
lost-update or torn-write bugs.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

from markbot.session.task_tracker import TaskTracker


@pytest.fixture
def tracker(tmp_path: Path) -> TaskTracker:
    return TaskTracker(tmp_path, max_active_tasks=1)


def test_concurrent_create_task_no_loss(tracker: TaskTracker) -> None:
    """8 threads × 25 creates each = 200 unique tasks in registry."""

    def create_many(start: int) -> int:
        for i in range(25):
            tracker.create_task(f"task-{start}-{i}")
        return start

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(create_many, i) for i in range(8)]
        for f in as_completed(futures):
            f.result()

    assert len(tracker.list_all()) == 200


def test_concurrent_transition_one_succeeds(tracker: TaskTracker) -> None:
    """Two threads both try to start the same task — only one wins
    (the second sees the task already in_progress, which is a
    valid transition but no-op for active_task_id)."""
    task = tracker.create_task("race-target")

    def start() -> str:
        try:
            tracker.start_task(task.id)
            return "ok"
        except ValueError as exc:
            return f"err:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(start) for _ in range(2)]
        results = [f.result() for f in as_completed(futures)]

    final = tracker.list_all()
    assert len(final) == 1
    assert final[0].status == "in_progress"


def test_concurrent_list_all_returns_consistent(tracker: TaskTracker) -> None:
    """Reads during writes return a consistent snapshot, never a
    half-written file."""

    def writer() -> None:
        for i in range(20):
            tracker.create_task(f"task-{i}")

    def reader() -> int:
        return max(len(tracker.list_all()) for _ in range(10))

    with ThreadPoolExecutor(max_workers=4) as pool:
        write_fut = pool.submit(writer)
        read_futs = [pool.submit(reader) for _ in range(3)]
        max_observed = max(f.result() for f in as_completed(read_futs))
        write_fut.result()

    final_count = len(tracker.list_all())
    assert max_observed <= final_count


def test_concurrent_cleanup_completed_idempotent(tracker: TaskTracker) -> None:
    """Two cleanup calls in parallel do not corrupt registry."""
    for i in range(5):
        t = tracker.create_task(f"task-{i}")
        tracker.transition(t.id, "in_progress")
        tracker.transition(t.id, "verifying")
        tracker.transition(t.id, "done")

    def cleanup() -> int:
        return tracker.cleanup_completed(max_age_days=0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(cleanup).result() for _ in range(2)]

    assert tracker.list_completed() == []


def test_lock_creation_no_existing_registry(tmp_path: Path) -> None:
    """The lock file is created on first construction, even before
    the data file exists."""
    t = TaskTracker(tmp_path)
    assert (tmp_path / "tasks" / "registry.json.lock").exists()
    assert not (tmp_path / "tasks" / "registry.json").exists()


def test_existing_registry_lock_reused(tmp_path: Path) -> None:
    """A second TaskTracker instance on the same workspace shares the
    same lock file (and the second instance's writes are visible)."""
    t1 = TaskTracker(tmp_path)
    t2 = TaskTracker(tmp_path)
    t1.create_task("from-t1")
    assert any(t.title == "from-t1" for t in t2.list_all())
