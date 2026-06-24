"""Tests for markbot.session.bootstrap — session startup validation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from markbot.session.bootstrap import (
    BootstrapCheckResult,
    BootstrapReport,
    FeatureEntry,
    SessionBootstrap,
)
from markbot.session.handoff import (
    HandoffBlocker,
    HandoffTask,
    SessionHandoff,
)


# ---------------------------------------------------------------------------
# BootstrapCheckResult
# ---------------------------------------------------------------------------


class TestBootstrapCheckResult:
    def test_with_name_and_status(self):
        r = BootstrapCheckResult(name="test", status="ok")
        assert r.name == "test"
        assert r.status == "ok"
        assert r.message == ""
        assert r.details == {}

    def test_with_values(self):
        r = BootstrapCheckResult(
            name="workspace",
            status="ok",
            message="Workspace writable",
            details={"path": "/tmp"},
        )
        assert r.status == "ok"
        assert r.details["path"] == "/tmp"


# ---------------------------------------------------------------------------
# FeatureEntry
# ---------------------------------------------------------------------------


class TestFeatureEntry:
    def test_defaults(self):
        f = FeatureEntry()
        assert f.id == ""
        assert f.status == "not_started"
        assert f.priority == 0
        assert f.verification == []
        assert f.evidence == []

    def test_with_values(self):
        f = FeatureEntry(
            id="F001",
            title="Add login",
            status="in_progress",
            priority=5,
            area="auth",
            verification=["manual"],
            evidence=["screenshot.png"],
        )
        assert f.id == "F001"
        assert f.priority == 5
        assert len(f.verification) == 1


# ---------------------------------------------------------------------------
# BootstrapReport
# ---------------------------------------------------------------------------


class TestBootstrapReport:
    def test_defaults(self):
        r = BootstrapReport()
        assert r.session_key == ""
        assert r.handoff_loaded is False
        assert r.feature_list_loaded is False
        assert r.init_sh_available is False
        assert r.checks == []
        assert r.warnings == []

    def test_has_warnings_false_when_no_warnings(self):
        r = BootstrapReport()
        assert r.has_warnings is False

    def test_has_warnings_true(self):
        r = BootstrapReport()
        r.checks.append(BootstrapCheckResult(name="x", status="warning", message="warn"))
        assert r.has_warnings is True

    def test_has_errors_false(self):
        r = BootstrapReport()
        r.checks.append(BootstrapCheckResult(name="x", status="ok"))
        assert r.has_errors is False

    def test_has_errors_true(self):
        r = BootstrapReport()
        r.checks.append(BootstrapCheckResult(name="x", status="error", message="err"))
        assert r.has_errors is True

    def test_to_context_block_empty(self):
        r = BootstrapReport()
        assert r.to_context_block() == ""

    def test_to_context_block_with_handoff(self):
        r = BootstrapReport(handoff_loaded=True)
        r.handoff = SessionHandoff(
            session_key="cli:test",
            timestamp="2025-01-01 12:00",
            active_tasks=[HandoffTask(id="T1", title="Fix bug", status="in_progress")],
            next_best_step="Run tests",
            blockers=[HandoffBlocker(description="API down")],
        )
        block = r.to_context_block()
        assert "Session Bootstrap Context" in block
        assert "Fix bug" in block
        assert "Run tests" in block
        assert "API down" in block

    def test_to_context_block_with_features(self):
        r = BootstrapReport(feature_list_loaded=True)
        r.feature_list = [
            FeatureEntry(id="F1", title="Feature A", status="not_started", priority=1),
            FeatureEntry(id="F2", title="Feature B", status="done", priority=2),
        ]
        r.next_feature = r.feature_list[0]
        block = r.to_context_block()
        assert "Feature A" in block
        assert "F1" in block

    def test_to_context_block_with_init_sh(self):
        r = BootstrapReport(init_sh_available=True)
        block = r.to_context_block()
        assert "init.sh" in block

    def test_to_context_block_with_warnings(self):
        r = BootstrapReport()
        r.warnings = ["Disk almost full"]
        r.handoff_loaded = True  # force non-empty
        block = r.to_context_block()
        assert "Disk almost full" in block


# ---------------------------------------------------------------------------
# SessionBootstrap
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path):
    return tmp_path


@pytest.fixture
def mock_handoff_manager():
    mgr = MagicMock()
    mgr.load.return_value = None
    return mgr


@pytest.fixture
def bootstrap(workspace, mock_handoff_manager):
    return SessionBootstrap(workspace=workspace, handoff_manager=mock_handoff_manager)


class TestSessionBootstrapRun:
    @pytest.mark.asyncio
    async def test_fresh_session_no_handoff(self, bootstrap, mock_handoff_manager):
        report = await bootstrap.run("cli:test")
        assert report.session_key == "cli:test"
        assert report.handoff_loaded is False
        assert report.handoff is None
        assert report.feature_list_loaded is False
        assert report.init_sh_available is False
        assert len(report.checks) >= 4

    @pytest.mark.asyncio
    async def test_with_handoff(self, bootstrap, mock_handoff_manager):
        handoff = SessionHandoff(
            session_key="cli:test",
            timestamp="2025-01-01 12:00",
            active_tasks=[HandoffTask(id="T1", title="Task", status="in_progress")],
        )
        mock_handoff_manager.load.return_value = handoff
        report = await bootstrap.run("cli:test")
        assert report.handoff_loaded is True
        assert report.handoff is handoff

    @pytest.mark.asyncio
    async def test_init_sh_available(self, bootstrap, workspace):
        (workspace / "init.sh").write_text("#!/bin/bash")
        report = await bootstrap.run("cli:test")
        assert report.init_sh_available is True

    @pytest.mark.asyncio
    async def test_init_sh_not_executable_warning(self, bootstrap, workspace):
        init = workspace / "init.sh"
        init.write_text("#!/bin/bash")
        init.chmod(0o644)  # not executable
        report = await bootstrap.run("cli:test")
        init_checks = [c for c in report.checks if c.name == "init_sh"]
        assert len(init_checks) == 1
        assert init_checks[0].status == "warning"

    @pytest.mark.asyncio
    async def test_feature_list_loaded(self, bootstrap, workspace):
        feature_data = {
            "features": [
                {"id": "F1", "title": "Feature A", "status": "not_started", "priority": 1},
                {"id": "F2", "title": "Feature B", "status": "done", "priority": 2},
            ]
        }
        (workspace / "feature_list.json").write_text(json.dumps(feature_data))
        report = await bootstrap.run("cli:test")
        assert report.feature_list_loaded is True
        assert len(report.feature_list) == 2
        assert report.next_feature is not None
        assert report.next_feature.id == "F1"

    @pytest.mark.asyncio
    async def test_feature_list_next_feature_sorts_by_priority(self, bootstrap, workspace):
        feature_data = {
            "features": [
                {"id": "F2", "title": "Low priority", "status": "not_started", "priority": 10},
                {"id": "F1", "title": "High priority", "status": "in_progress", "priority": 1},
            ]
        }
        (workspace / "feature_list.json").write_text(json.dumps(feature_data))
        report = await bootstrap.run("cli:test")
        assert report.next_feature.id == "F1"

    @pytest.mark.asyncio
    async def test_workspace_writable(self, bootstrap):
        report = await bootstrap.run("cli:test")
        ws_checks = [c for c in report.checks if c.name == "workspace_access"]
        assert len(ws_checks) == 1
        assert ws_checks[0].status == "ok"

    @pytest.mark.asyncio
    async def test_mcp_not_configured(self, bootstrap):
        report = await bootstrap.run("cli:test")
        mcp_checks = [c for c in report.checks if c.name == "mcp_connectivity"]
        assert len(mcp_checks) == 1
        assert mcp_checks[0].status == "ok"

    @pytest.mark.asyncio
    async def test_context_summary_built(self, bootstrap, mock_handoff_manager):
        handoff = SessionHandoff(
            session_key="cli:test",
            timestamp="2025-01-01 12:00",
            active_tasks=[HandoffTask(id="T1", title="Task", status="in_progress")],
            next_best_step="Continue work",
        )
        mock_handoff_manager.load.return_value = handoff
        report = await bootstrap.run("cli:test")
        assert report.context_summary != ""
        assert "Task" in report.context_summary
        assert "Continue work" in report.context_summary

    @pytest.mark.asyncio
    async def test_warnings_collected(self, bootstrap, workspace):
        # No init.sh → should produce a warning
        report = await bootstrap.run("cli:test")
        assert len(report.warnings) > 0
        assert any("init.sh" in w for w in report.warnings)
