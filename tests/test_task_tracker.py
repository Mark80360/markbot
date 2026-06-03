"""Tests for markbot.session.task_tracker — Task lifecycle state machine."""


import pytest

from markbot.session.task_tracker import VALID_TRANSITIONS, Task, TaskRegistry, TaskTracker


class TestTaskDataclass:
    def test_default_id_generation(self):
        task = Task()
        assert task.id != ""
        assert len(task.id) == 8

    def test_custom_id_preserved(self):
        task = Task(id="custom123")
        assert task.id == "custom123"

    def test_default_status(self):
        task = Task()
        assert task.status == "stated"

    def test_timestamps_set_on_creation(self):
        task = Task()
        assert task.created_at > 0
        assert task.updated_at > 0
        assert task.completed_at is None

    def test_custom_fields(self):
        task = Task(
            title="Test Task",
            description="A test task",
            priority=5,
            category="testing",
        )
        assert task.title == "Test Task"
        assert task.description == "A test task"
        assert task.priority == 5
        assert task.category == "testing"

    def test_repair_count_default(self):
        task = Task()
        assert task.repair_count == 0
        assert task.max_repairs == 2


class TestValidTransitions:
    def test_stated_can_go_to_in_progress(self):
        assert "in_progress" in VALID_TRANSITIONS["stated"]

    def test_stated_can_go_to_cancelled(self):
        assert "cancelled" in VALID_TRANSITIONS["stated"]

    def test_stated_cannot_go_to_done(self):
        assert "done" not in VALID_TRANSITIONS["stated"]

    def test_in_progress_can_go_to_verifying(self):
        assert "verifying" in VALID_TRANSITIONS["in_progress"]

    def test_in_progress_can_go_to_blocked(self):
        assert "blocked" in VALID_TRANSITIONS["in_progress"]

    def test_in_progress_can_go_to_failed(self):
        assert "failed" in VALID_TRANSITIONS["in_progress"]

    def test_verifying_can_go_to_done(self):
        assert "done" in VALID_TRANSITIONS["verifying"]

    def test_verifying_can_go_back_to_in_progress(self):
        assert "in_progress" in VALID_TRANSITIONS["verifying"]

    def test_done_is_terminal(self):
        assert VALID_TRANSITIONS["done"] == set()

    def test_cancelled_is_terminal(self):
        assert VALID_TRANSITIONS["cancelled"] == set()

    def test_blocked_can_resume(self):
        assert "in_progress" in VALID_TRANSITIONS["blocked"]

    def test_failed_can_retry(self):
        assert "in_progress" in VALID_TRANSITIONS["failed"]


class TestTaskTracker:
    @pytest.fixture
    def tracker(self, tmp_path):
        return TaskTracker(tmp_path)

    def test_creates_tasks_directory(self, tmp_path):
        tracker = TaskTracker(tmp_path)
        assert (tmp_path / "tasks").exists()

    def test_create_task(self, tracker):
        task = tracker.create_task("Test Task", description="Desc")
        assert task.title == "Test Task"
        assert task.description == "Desc"
        assert task.status == "stated"

    def test_create_task_returns_persisted(self, tracker):
        task = tracker.create_task("Test")
        loaded = tracker._get_task(task.id)
        assert loaded is not None
        assert loaded.title == "Test"

    def test_start_task(self, tracker):
        task = tracker.create_task("Test")
        started = tracker.start_task(task.id)
        assert started.status == "in_progress"

    def test_submit_for_verification(self, tracker):
        task = tracker.create_task("Test")
        tracker.start_task(task.id)
        verifying = tracker.submit_for_verification(task.id, progress="Done")
        assert verifying.status == "verifying"
        assert verifying.progress == "Done"

    def test_mark_done(self, tracker):
        task = tracker.create_task("Test")
        tracker.start_task(task.id)
        tracker.submit_for_verification(task.id)
        done = tracker.mark_done(task.id, verification_result="passed")
        assert done.status == "done"
        assert done.completed_at is not None
        assert done.verification_result == "passed"

    def test_mark_failed(self, tracker):
        task = tracker.create_task("Test")
        tracker.start_task(task.id)
        # Need to go through verifying first, then fail
        tracker.submit_for_verification(task.id)
        failed = tracker.mark_failed(task.id, reason="error")
        # With repair_count=0 < max_repairs=2, it goes back to in_progress
        assert failed.status == "in_progress"
        assert "Repair attempt" in failed.progress

    def test_block_task(self, tracker):
        task = tracker.create_task("Test")
        tracker.start_task(task.id)
        blocked = tracker.block_task(task.id, reason="dependency")
        assert blocked.status == "blocked"

    def test_cancel_task(self, tracker):
        task = tracker.create_task("Test")
        cancelled = tracker.cancel_task(task.id)
        assert cancelled.status == "cancelled"

    def test_invalid_transition_raises(self, tracker):
        task = tracker.create_task("Test")
        with pytest.raises(ValueError, match="Invalid transition"):
            tracker.transition(task.id, "done")

    def test_task_not_found_raises(self, tracker):
        with pytest.raises(ValueError, match="Task not found"):
            tracker.start_task("nonexistent")

    def test_single_active_task_constraint(self, tracker):
        task1 = tracker.create_task("Task 1")
        task2 = tracker.create_task("Task 2")
        tracker.start_task(task1.id)
        with pytest.raises(ValueError, match="already have"):
            tracker.start_task(task2.id)

    def test_list_all(self, tracker):
        tracker.create_task("A")
        tracker.create_task("B")
        tracker.create_task("C")
        tasks = tracker.list_all()
        assert len(tasks) == 3

    def test_list_active(self, tracker):
        task = tracker.create_task("Active")
        tracker.start_task(task.id)
        tracker.create_task("Pending")
        active = tracker.list_active()
        assert len(active) == 1
        assert active[0].id == task.id

    def test_list_pending(self, tracker):
        tracker.create_task("Pending 1")
        tracker.create_task("Pending 2")
        pending = tracker.list_pending()
        assert len(pending) == 2

    def test_list_completed(self, tracker):
        task = tracker.create_task("Done")
        tracker.start_task(task.id)
        tracker.submit_for_verification(task.id)
        tracker.mark_done(task.id)
        completed = tracker.list_completed()
        assert len(completed) == 1

    def test_get_active_task(self, tracker):
        assert tracker.get_active_task() is None
        task = tracker.create_task("Test")
        tracker.start_task(task.id)
        assert tracker.get_active_task() is not None
        assert tracker.get_active_task().id == task.id

    def test_get_next_pending(self, tracker):
        assert tracker.get_next_pending() is None
        task = tracker.create_task("Test")
        next_task = tracker.get_next_pending()
        assert next_task is not None
        assert next_task.id == task.id

    def test_get_summary(self, tracker):
        t1 = tracker.create_task("A")
        t2 = tracker.create_task("B")
        tracker.start_task(t1.id)
        with pytest.raises(ValueError):
            tracker.start_task(t2.id)  # Fails due to constraint
        summary = tracker.get_summary()
        assert summary["total"] == 2
        assert summary.get("in_progress", 0) == 1

    def test_cleanup_completed(self, tracker):
        task = tracker.create_task("Old")
        tracker.start_task(task.id)
        tracker.submit_for_verification(task.id)
        tracker.mark_done(task.id)
        # Set completed_at to old time
        loaded = tracker._get_task(task.id)
        loaded.completed_at = 100000  # very old
        tracker._save_task(loaded)
        removed = tracker.cleanup_completed(max_age_days=1)
        assert removed == 1

    def test_registry_file_created(self, tmp_path):
        tracker = TaskTracker(tmp_path)
        tracker.create_task("Test")
        registry_path = tmp_path / "tasks" / "registry.json"
        assert registry_path.exists()

    def test_load_nonexistent_registry(self, tmp_path):
        tracker = TaskTracker(tmp_path)
        registry = tracker._load_registry()
        assert registry.version == 1
        assert registry.active_task_id is None
        assert registry.tasks == []


class TestTaskRegistry:
    def test_default_values(self):
        registry = TaskRegistry()
        assert registry.version == 1
        assert registry.active_task_id is None
        assert registry.tasks == []
        assert registry.updated_at == 0.0
