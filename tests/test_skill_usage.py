"""Tests for skill usage tracking, lifecycle, curator, and improver."""

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
