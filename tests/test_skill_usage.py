"""Tests for skill usage tracking, lifecycle, curator, and improver."""

import asyncio
import time

from markbot.skills.curator import CuratorReport, CuratorService
from markbot.skills.improve import EvalResult, SkillImprover
from markbot.skills.lifecycle import (
    ARCHIVE_THRESHOLD,
    NEW_SKILL_GRACE_PERIOD,
    STALE_THRESHOLD,
    SkillLifecycle,
)
from markbot.skills.usage import SkillUsageStore
from markbot.types.skill import SkillDefinition, SkillState


class TestSkillUsageStore:
    def test_creates_new_entry(self, tmp_path):
        store = SkillUsageStore(tmp_path)
        entry = store.get("test_skill")
        assert entry.view_count == 0
        assert entry.use_count == 0
        assert entry.last_activity_at is None
        assert entry.state == "active"

    def test_bump_view(self, tmp_path):
        store = SkillUsageStore(tmp_path)
        store.bump_view("test_skill")
        entry = store.get("test_skill")
        assert entry.view_count == 1
        assert entry.last_activity_at is not None

    def test_bump_use(self, tmp_path):
        store = SkillUsageStore(tmp_path)
        store.bump_use("test_skill")
        store.bump_use("test_skill")
        entry = store.get("test_skill")
        assert entry.use_count == 2
        assert entry.last_activity_at is not None

    def test_persistence(self, tmp_path):
        store = SkillUsageStore(tmp_path)
        store.bump_view("skill_a")
        store.bump_use("skill_b")

        # Reload from disk
        store2 = SkillUsageStore(tmp_path)
        assert store2.get("skill_a").view_count == 1
        assert store2.get("skill_b").use_count == 1

    def test_set_created_at(self, tmp_path):
        store = SkillUsageStore(tmp_path)
        ts = 1000000.0
        store.set_created_at("test_skill", ts)
        assert store.get("test_skill").created_at == ts

    def test_remove(self, tmp_path):
        store = SkillUsageStore(tmp_path)
        store.bump_view("test_skill")
        store.remove("test_skill")
        # After remove, get should return a fresh entry
        assert store.get("test_skill").view_count == 0

    def test_get_all(self, tmp_path):
        store = SkillUsageStore(tmp_path)
        store.bump_view("a")
        store.bump_use("b")
        all_entries = store.get_all()
        assert "a" in all_entries
        assert "b" in all_entries

    def test_bump_creates_missing_workspace_dir(self, tmp_path):
        ws = tmp_path / "nested" / "ws"
        store = SkillUsageStore(ws)
        store.bump_view("x")
        assert ws.exists()
        assert (ws / ".skill_usage.json").exists()
        assert SkillUsageStore(ws).get("x").view_count == 1


class TestSkillUsageStoreConcurrency:
    def test_two_instances_no_lost_update(self, tmp_path):
        store1 = SkillUsageStore(tmp_path)
        store2 = SkillUsageStore(tmp_path)
        store1.bump_view("x")
        store2.bump_view("x")
        store1.bump_view("x")
        assert SkillUsageStore(tmp_path).get("x").view_count == 3

    def test_remove_sees_other_instance_bumps(self, tmp_path):
        store1 = SkillUsageStore(tmp_path)
        store1.bump_use("a")
        store1.bump_use("b")
        store2 = SkillUsageStore(tmp_path)
        store2.remove("a")
        assert store2.peek("a") is None
        assert store2.peek("b") is not None
        assert store1.get("b").use_count == 1

    def test_threaded_bumps_no_lost_update(self, tmp_path):
        import threading

        per_thread = 15
        errors = []

        def worker():
            try:
                store = SkillUsageStore(tmp_path)
                for _ in range(per_thread):
                    store.bump_view("x")
            except Exception as e:  # pragma: no cover - failure path
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert SkillUsageStore(tmp_path).get("x").view_count == 2 * per_thread


class TestSkillLifecycle:
    def test_builtin_always_active(self, tmp_path):
        store = SkillUsageStore(tmp_path)
        lifecycle = SkillLifecycle(tmp_path, store)
        assert lifecycle.evaluate("builtin_skill", is_builtin=True) == SkillState.ACTIVE

    def test_new_skill_active_in_grace_period(self, tmp_path):
        store = SkillUsageStore(tmp_path)
        lifecycle = SkillLifecycle(tmp_path, store)
        # Recently created, no usage
        assert lifecycle.evaluate("new_skill") == SkillState.ACTIVE

    def test_stale_after_grace_period(self, tmp_path):
        store = SkillUsageStore(tmp_path)
        store.set_created_at("old_skill", time.time() - NEW_SKILL_GRACE_PERIOD - 1)
        lifecycle = SkillLifecycle(tmp_path, store)
        assert lifecycle.evaluate("old_skill") == SkillState.STALE

    def test_stale_after_inactivity(self, tmp_path):
        store = SkillUsageStore(tmp_path)
        store.bump_use("skill")
        # Manually set last_activity_at to old time
        entry = store.get("skill")
        entry.last_activity_at = time.time() - STALE_THRESHOLD - 1
        store._persist()

        lifecycle = SkillLifecycle(tmp_path, store)
        assert lifecycle.evaluate("skill") == SkillState.STALE

    def test_archived_after_long_inactivity(self, tmp_path):
        store = SkillUsageStore(tmp_path)
        store.bump_use("skill")
        entry = store.get("skill")
        entry.last_activity_at = time.time() - ARCHIVE_THRESHOLD - 1
        store._persist()

        lifecycle = SkillLifecycle(tmp_path, store)
        assert lifecycle.evaluate("skill") == SkillState.ARCHIVED

    def test_active_after_recent_use(self, tmp_path):
        store = SkillUsageStore(tmp_path)
        store.bump_use("skill")
        lifecycle = SkillLifecycle(tmp_path, store)
        assert lifecycle.evaluate("skill") == SkillState.ACTIVE

    def test_scan_all_finds_transitions(self, tmp_path):
        store = SkillUsageStore(tmp_path)
        store.set_created_at("old_skill", time.time() - NEW_SKILL_GRACE_PERIOD - 1)
        lifecycle = SkillLifecycle(tmp_path, store)
        reports = lifecycle.scan_all([("old_skill", False)])
        assert len(reports) == 1
        assert reports[0].target_state == SkillState.STALE

    def test_transition_to_stale(self, tmp_path):
        store = SkillUsageStore(tmp_path)
        lifecycle = SkillLifecycle(tmp_path, store)
        result = lifecycle.transition("skill", SkillState.STALE)
        assert result.applied is True
        assert store.get("skill").state == SkillState.STALE

    def test_archive_and_restore(self, tmp_path):
        skills_dir = tmp_path / "skills" / "test_skill"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text("# Test")

        store = SkillUsageStore(tmp_path)
        lifecycle = SkillLifecycle(tmp_path, store)

        # Archive
        result = lifecycle.transition("test_skill", SkillState.ARCHIVED)
        assert result.applied is True
        assert (tmp_path / "skills" / "archived" / "test_skill").exists()
        assert not skills_dir.exists()

        # Restore
        result = lifecycle.transition("test_skill", SkillState.ACTIVE)
        assert result.applied is True
        assert skills_dir.exists()


class TestCuratorService:
    def test_run_maintenance_no_registry(self, tmp_path):
        curator = CuratorService(tmp_path)
        report = curator.run_maintenance()
        assert len(report.errors) > 0

    def test_report_persistence(self, tmp_path):
        curator = CuratorService(tmp_path)
        report = CuratorReport()
        report.skills_scanned = 5
        curator._save_report(report)

        reports = curator.get_recent_reports()
        assert len(reports) == 1
        assert reports[0]["skills_scanned"] == 5

    def test_prunes_orphaned_usage(self, tmp_path):
        skills_dir = tmp_path / "skills" / "my_skill"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text(
            "---\nname: my_skill\ndescription: t\nwhen_to_use: t\n---\nbody"
        )

        from markbot.skills.core.registry import SkillRegistry

        registry = SkillRegistry(tmp_path)
        registry.load_all()

        usage = registry.usage_store
        usage.bump_use("my_skill")
        usage.bump_use("ghost_skill")

        curator = CuratorService(tmp_path, registry)
        report = curator.run_maintenance()

        assert report.orphaned_usage_pruned == 1
        assert usage.peek("ghost_skill") is None
        assert usage.peek("my_skill") is not None

    def test_prune_keeps_archived_skill_usage(self, tmp_path):
        (tmp_path / "skills" / "my_skill").mkdir(parents=True)
        (tmp_path / "skills" / "my_skill" / "SKILL.md").write_text(
            "---\nname: my_skill\ndescription: t\nwhen_to_use: t\n---\nbody"
        )
        (tmp_path / "skills" / "archived" / "old_skill").mkdir(parents=True)
        (tmp_path / "skills" / "archived" / "old_skill" / "SKILL.md").write_text(
            "---\nname: old_skill\ndescription: t\nwhen_to_use: t\n---\nbody"
        )

        from markbot.skills.core.registry import SkillRegistry

        registry = SkillRegistry(tmp_path)
        registry.load_all()

        usage = registry.usage_store
        usage.bump_use("my_skill")
        usage.bump_use("old_skill")
        usage.set_state("old_skill", "archived")

        curator = CuratorService(tmp_path, registry)
        report = curator.run_maintenance()

        assert report.orphaned_usage_pruned == 0
        assert usage.peek("my_skill") is not None
        assert usage.peek("old_skill") is not None

    def test_view_nonexistent_skill_not_counted(self, tmp_path):
        from markbot.skills.core.registry import SkillRegistry
        from markbot.skills.core.tool import SkillViewTool

        registry = SkillRegistry(tmp_path)
        tool = SkillViewTool(registry)

        result = asyncio.run(tool.execute({"name": "ghost_skill"}, None))

        assert "not found" in result
        assert registry.usage_store.peek("ghost_skill") is None


class TestEvalResult:
    def test_default_values(self):
        result = EvalResult(skill_name="test")
        assert result.score == 0.0
        assert result.issues == []
        assert result.suggestions == []


class TestSkillImprover:
    def test_eval_empty_description(self, tmp_path):
        improver = SkillImprover(tmp_path)
        skill = SkillDefinition(name="test", description="", when_to_use="")
        result = improver.run_eval("test", skill)
        assert result.description_clarity < 0.5

    def test_eval_good_description(self, tmp_path):
        improver = SkillImprover(tmp_path)
        skill = SkillDefinition(
            name="test",
            description="A comprehensive skill for managing GitHub repositories",
            when_to_use="When the user needs to interact with GitHub",
            scripts=[],
        )
        result = improver.run_eval("test", skill)
        assert result.description_clarity > 0.5
        assert result.completeness > 0.3

    def test_check_markdown_quality(self):
        assert len(SkillImprover._check_markdown_quality("")) > 0
        assert len(SkillImprover._check_markdown_quality("short")) > 0

    def test_parse_suggestions(self):
        text = "1. Improve description\n2. Add examples\n3. Fix formatting"
        suggestions = SkillImprover._parse_suggestions(text)
        assert len(suggestions) == 3
