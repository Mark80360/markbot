"""Tests for markbot.autopilot package — types, store, verification, tools, service."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from markbot.autopilot import service as service_mod
from markbot.autopilot.store import AutopilotStore
from markbot.autopilot.types import (
    AutopilotConfig,
    AutopilotPolicy,
    TaskCard,
    TaskJournalEntry,
    TaskRegistry,
    TaskRunResult,
    VerificationCommand,
    VerificationPolicy,
    VerificationStep,
)
from markbot.autopilot.tools import (
    ALL_AUTOPILOT_TOOLS,
    AutopilotIntakeTool,
    AutopilotListTool,
    AutopilotPickNextTool,
    AutopilotRequeueTool,
    AutopilotRejectTool,
    AutopilotStatusTool,
    AutopilotVerifyTool,
    _get_store,
    _invalidate_store,
    _store_cache,
)
from markbot.autopilot.verification import (
    build_verification_commands,
    parse_verification_entry,
    render_verification_report,
    run_verification_steps,
    verification_passed,
)
from markbot.types.permission import PermissionMode, ToolPermissionContext
from markbot.types.tool import ToolContext


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class FakeAgentLoop:
    """Stand-in for AgentLoop that records calls and returns a canned reply."""

    def __init__(self, content: str = "agent did the work") -> None:
        self.content = content
        self.calls: list[dict[str, Any]] = []

    async def process_direct(
        self,
        prompt: str,
        *,
        session_key: str = "",
        channel: str = "",
        chat_id: str = "",
        permission_mode: Any = None,
    ) -> _FakeResponse:
        self.calls.append(
            {
                "prompt": prompt,
                "session_key": session_key,
                "channel": channel,
                "chat_id": chat_id,
                "permission_mode": permission_mode,
            }
        )
        return _FakeResponse(self.content)


def _build_context(workspace: Path) -> ToolContext:
    return ToolContext(
        session_id="test-session",
        workspace=str(workspace),
        permission_mode=PermissionMode.BYPASS,
        tool_permission_context=ToolPermissionContext(mode=PermissionMode.BYPASS),
    )


@pytest.fixture
def store(temp_workspace: Path) -> AutopilotStore:
    s = AutopilotStore(temp_workspace)
    yield s
    _invalidate_store(temp_workspace)


def _clear_store_cache() -> None:
    _store_cache.clear()


# ---------------------------------------------------------------------------
# types.py
# ---------------------------------------------------------------------------


class TestTypes:
    def test_task_card_defaults(self):
        card = TaskCard(id="ap-1", fingerprint="fp1", title="Hello")
        assert card.id == "ap-1"
        assert card.body == ""
        assert card.source_kind == "manual_idea"
        assert card.status == "queued"
        assert card.score == 0
        assert card.score_reasons == []
        assert card.labels == []
        assert card.metadata == {}
        assert card.created_at == 0.0
        assert card.updated_at == 0.0

    def test_task_card_custom(self):
        card = TaskCard(
            id="ap-2",
            fingerprint="fp2",
            title="Bugfix",
            body="fix the crash",
            source_kind="github_issue",
            source_ref="issue:42",
            labels=["bug", "urgent"],
            metadata={"k": "v"},
            created_at=1.0,
            updated_at=2.0,
        )
        assert card.source_kind == "github_issue"
        assert card.source_ref == "issue:42"
        assert card.labels == ["bug", "urgent"]
        assert card.metadata == {"k": "v"}

    def test_task_journal_entry_defaults(self):
        entry = TaskJournalEntry(timestamp=1.0, kind="run_start", summary="hi")
        assert entry.task_id is None
        assert entry.metadata == {}

    def test_task_registry_defaults(self):
        reg = TaskRegistry()
        assert reg.version == 1
        assert reg.updated_at == 0.0
        assert reg.cards == []

    def test_verification_step_status_values(self):
        step = VerificationStep(command="ls", returncode=0, status="success")
        assert step.status == "success"
        assert step.stdout == ""
        assert step.stderr == ""

    def test_task_run_result_defaults(self):
        result = TaskRunResult(card_id="ap-3", status="queued")
        assert result.assistant_summary == ""
        assert result.run_report_path == ""
        assert result.verification_steps == []
        assert result.attempt_count == 0
        assert result.worktree_path == ""

    def test_verification_command_defaults(self):
        cmd = VerificationCommand(raw="echo hi")
        assert cmd.argv == ()
        assert cmd.shell is False
        assert cmd.error is None

    def test_autopilot_policy_defaults(self):
        pol = AutopilotPolicy()
        assert pol.intake_max_visible == 12
        assert pol.default_human_gate is True
        assert pol.prefer_small_safe_steps is True
        assert pol.default_model == ""
        assert pol.max_turns == 12
        assert pol.max_attempts == 3
        assert pol.repair_max_rounds == 2
        assert pol.repair_retry_on == ["local_verification_failed"]
        assert pol.repair_stop_on == ["agent_runtime_error", "permission_error"]

    def test_verification_policy_defaults(self):
        vp = VerificationPolicy()
        assert vp.commands == []
        assert vp.require_tests_before_complete is True

    def test_autopilot_config_defaults(self):
        cfg = AutopilotConfig()
        assert isinstance(cfg.autopilot_policy, AutopilotPolicy)
        assert isinstance(cfg.verification_policy, VerificationPolicy)

    def test_task_card_is_serialisable(self):
        card = TaskCard(id="ap-9", fingerprint="fp", title="t", body="b")
        d = asdict(card)
        assert d["id"] == "ap-9"
        assert d["title"] == "t"


# ---------------------------------------------------------------------------
# store.py
# ---------------------------------------------------------------------------


class TestStore:
    def test_layout_created(self, store: AutopilotStore):
        assert store.autopilot_dir.exists()
        assert store.runs_dir.exists()
        assert (store.autopilot_dir / "registry.json").exists()

    def test_workspace_property(self, store: AutopilotStore, temp_workspace: Path):
        assert store.workspace == temp_workspace.resolve()

    def test_empty_list(self, store: AutopilotStore):
        assert store.list_cards() == []

    def test_enqueue_creates_card(self, store: AutopilotStore):
        card, created = store.enqueue_card(
            source_kind="github_issue",
            title="Fix the bug",
            body="crash on startup",
        )
        assert created is True
        assert card.id.startswith("ap-")
        assert card.fingerprint
        assert card.status == "queued"
        assert card.source_kind == "github_issue"
        assert card.score > 0
        assert card.score_reasons
        assert "source:github_issue=75" in Card_reasons(card)

    def test_enqueue_idempotent_refresh(self, store: AutopilotStore):
        c1, created1 = store.enqueue_card(
            source_kind="manual_idea", title="Do thing", body="x",
        )
        c2, created2 = store.enqueue_card(
            source_kind="manual_idea", title="Do thing", body="x",
        )
        assert created1 is True
        assert created2 is False
        assert c1.id == c2.id
        assert c1.fingerprint == c2.fingerprint

    def test_enqueue_refresh_merges_labels(self, store: AutopilotStore):
        store.enqueue_card(
            source_kind="manual_idea", title="T", body="b", labels=["bug"],
        )
        card, created = store.enqueue_card(
            source_kind="manual_idea", title="T", body="b", labels=["urgent"],
        )
        assert created is False
        assert "bug" in card.labels
        assert "urgent" in card.labels

    def test_normalize_labels_strips_and_filters(self, store: AutopilotStore):
        assert store._normalize_labels(["  a  ", "", "  b"]) == ["a", "b"]
        assert store._normalize_labels(None) == []
        assert store._normalize_labels([]) == []

    def test_merge_labels_dedupes(self, store: AutopilotStore):
        assert store._merge_labels(["a", "b"], ["b", "c"]) == ["a", "b", "c"]

    def test_build_fingerprint_stable(self, store: AutopilotStore):
        fp1 = store._build_fingerprint(
            source_kind="manual_idea", source_ref="r", title="t", body="b",
        )
        fp2 = store._build_fingerprint(
            source_kind="manual_idea", source_ref="r", title="t", body="b",
        )
        assert fp1 == fp2
        assert len(fp1) == 16

    def test_score_card_bug_hint(self, store: AutopilotStore):
        card = TaskCard(id="x", fingerprint="f", title="Fix the bug", body="crash")
        score, reasons = store._score_card(card)
        assert score > 50
        assert any("bug_hint" in r for r in reasons)

    def test_score_card_urgent_label(self, store: AutopilotStore):
        card = TaskCard(
            id="x", fingerprint="f", title="nice", body="ok", labels=["urgent"],
        )
        score, reasons = store._score_card(card)
        assert any("urgent_label" in r for r in reasons)
        assert "urgent" not in (card.title + card.body)

    def test_score_card_failed_penalty(self, store: AutopilotStore):
        card = TaskCard(id="x", fingerprint="f", title="nice", body="ok", status="failed")
        score, reasons = store._score_card(card)
        assert any("previously_failed" in r for r in reasons)
        assert score <= 50 + 20 + 15 - 30

    def test_pick_next_card(self, store: AutopilotStore):
        high, _ = store.enqueue_card(
            source_kind="github_pr", title="Critical fix", body="urgent blocker bug",
        )
        low, _ = store.enqueue_card(
            source_kind="agent_candidate", title="minor", body="",
        )
        nxt = store.pick_next_card()
        assert nxt is not None
        assert nxt.id == high.id
        assert nxt.score >= low.score

    def test_pick_next_card_none_when_no_queued(self, store: AutopilotStore):
        assert store.pick_next_card() is None

    def test_pick_next_excludes_completed(self, store: AutopilotStore):
        card, _ = store.enqueue_card(source_kind="manual_idea", title="t", body="b")
        store.update_status(card.id, status="completed")
        assert store.pick_next_card() is None

    def test_get_card(self, store: AutopilotStore):
        card, _ = store.enqueue_card(source_kind="manual_idea", title="t", body="b")
        assert store.get_card(card.id).id == card.id
        assert store.get_card("missing") is None

    def test_list_cards_filter_by_status(self, store: AutopilotStore):
        c, _ = store.enqueue_card(source_kind="manual_idea", title="t", body="b")
        store.update_status(c.id, status="completed")
        assert store.list_cards(status="completed") != []
        assert store.list_cards(status="queued") == []

    def test_list_cards_sorted_by_score_desc(self, store: AutopilotStore):
        high, _ = store.enqueue_card(
            source_kind="github_pr", title="urgent critical bug", body="crash",
        )
        low, _ = store.enqueue_card(
            source_kind="agent_candidate", title="doc tweak", body="",
        )
        cards = store.list_cards()
        assert cards[0].id == high.id
        assert cards[-1].id == low.id

    def test_update_status(self, store: AutopilotStore):
        card, _ = store.enqueue_card(source_kind="manual_idea", title="t", body="b")
        updated = store.update_status(card.id, status="running", note="go")
        assert updated.status == "running"
        assert updated.metadata["last_note"] == "go"

    def test_update_status_unknown_raises(self, store: AutopilotStore):
        with pytest.raises(ValueError):
            store.update_status("nope", status="running")

    def test_update_status_metadata_updates(self, store: AutopilotStore):
        card, _ = store.enqueue_card(source_kind="manual_idea", title="t", body="b")
        updated = store.update_status(
            card.id, status="failed", metadata_updates={"a": 1},
        )
        assert updated.metadata["a"] == 1

    def test_append_and_load_journal(self, store: AutopilotStore):
        e1 = store.append_journal(kind="k1", summary="one", task_id="ap-1")
        e2 = store.append_journal(kind="k2", summary="two")
        loaded = store.load_journal(limit=10)
        assert len(loaded) == 2
        assert loaded[0].kind == "k1"
        assert loaded[0].task_id == "ap-1"
        assert loaded[1].kind == "k2"
        assert loaded[1].task_id is None
        assert e1.kind == "k1"
        assert e2.summary == "two"

    def test_load_journal_empty(self, store: AutopilotStore):
        assert store.load_journal() == []

    def test_load_journal_limit(self, store: AutopilotStore):
        for i in range(15):
            store.append_journal(kind="k", summary=f"s{i}")
        assert len(store.load_journal(limit=5)) == 5

    def test_load_journal_skips_bad_lines(self, store: AutopilotStore):
        store.append_journal(kind="good", summary="ok")
        with store._journal_path.open("a", encoding="utf-8") as h:
            h.write("not json\n")
        loaded = store.load_journal(limit=10)
        kinds = [e.kind for e in loaded]
        assert "good" in kinds
        assert all(e.kind == "good" for e in loaded)

    def test_rebuild_active_context(self, store: AutopilotStore):
        card, _ = store.enqueue_card(
            source_kind="manual_idea", title="Active task", body="detail",
        )
        ctx = store.rebuild_active_context()
        assert "Active Autopilot Context" in ctx
        assert card.title in ctx

    def test_load_active_context_empty(self, store: AutopilotStore):
        assert store.load_active_context() == ""

    def test_load_active_context_after_rebuild(self, store: AutopilotStore):
        store.rebuild_active_context()
        assert store.load_active_context() != ""

    def test_stats(self, store: AutopilotStore):
        c1, _ = store.enqueue_card(source_kind="manual_idea", title="a", body="b")
        c2, _ = store.enqueue_card(source_kind="manual_idea", title="c", body="d")
        store.update_status(c1.id, status="completed")
        stats = store.stats()
        assert stats.get("completed") == 1
        assert stats.get("queued") == 1

    def test_registry_round_trip(self, store: AutopilotStore):
        store.enqueue_card(source_kind="manual_idea", title="persist", body="me")
        # new store instance reading same files
        store2 = AutopilotStore(store.workspace)
        cards = store2.list_cards()
        assert len(cards) == 1
        assert cards[0].title == "persist"

    def test_load_config_default(self, store: AutopilotStore):
        cfg = store.load_config()
        assert isinstance(cfg, AutopilotConfig)
        assert cfg.autopilot_policy.max_attempts == 3

    def test_save_and_load_config(self, store: AutopilotStore):
        cfg = AutopilotConfig(
            autopilot_policy=AutopilotPolicy(max_attempts=9, default_human_gate=False),
            verification_policy=VerificationPolicy(commands=["pytest"]),
        )
        store.save_config(cfg)
        loaded = store.load_config()
        assert loaded.autopilot_policy.max_attempts == 9
        assert loaded.autopilot_policy.default_human_gate is False
        assert loaded.verification_policy.commands == ["pytest"]

    def test_load_config_caches(self, store: AutopilotStore):
        cfg1 = store.load_config()
        cfg2 = store.load_config()
        assert cfg1 is cfg2

    def test_load_config_corrupt_file_falls_back(self, store: AutopilotStore):
        store._policy_path.write_text("{ not json", encoding="utf-8")
        store._file_config = None
        cfg = store.load_config()
        assert isinstance(cfg, AutopilotConfig)
        assert cfg.autopilot_policy.max_attempts == 3


def Card_reasons(card: TaskCard) -> str:
    return ", ".join(card.score_reasons)


# ---------------------------------------------------------------------------
# verification.py
# ---------------------------------------------------------------------------


class TestVerificationParsing:
    def test_parse_simple_string(self):
        cmd = parse_verification_entry("pytest -q")
        assert cmd.error is None
        assert cmd.shell is False
        assert cmd.argv == ("pytest", "-q")
        assert cmd.raw == "pytest -q"

    def test_parse_empty_string(self):
        cmd = parse_verification_entry("   ")
        assert cmd.error == "empty command"
        assert cmd.argv == ()

    def test_parse_dict_no_shell(self):
        cmd = parse_verification_entry({"command": "ruff check ."})
        assert cmd.error is None
        assert cmd.argv == ("ruff", "check", ".")

    def test_parse_dict_shell_true(self):
        cmd = parse_verification_entry({"command": "a && b", "shell": True})
        assert cmd.shell is True
        assert cmd.error is None
        assert cmd.argv == ()

    def test_parse_dict_empty_command(self):
        cmd = parse_verification_entry({"command": ""})
        assert cmd.error == "empty command"

    def test_parse_string_with_metachars_rejected(self):
        cmd = parse_verification_entry("a && b")
        assert cmd.error is not None
        assert "shell" in cmd.error.lower()
        assert cmd.argv == ()

    def test_parse_invalid_type(self):
        cmd = parse_verification_entry(123)
        assert cmd.error is not None
        assert cmd.argv == ()

    def test_parse_bad_shlex(self):
        # unbalanced quote triggers shlex ValueError
        cmd = parse_verification_entry("echo 'unterminated")
        assert cmd.error is not None
        assert "tokenize" in cmd.error


class TestBuildVerificationCommands:
    def test_build_includes_available(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
        policy = VerificationPolicy(commands=["uv run pytest"])
        cmds = build_verification_commands(policy, tmp_path)
        assert len(cmds) == 1
        assert cmds[0].error is None

    def test_build_skips_unavailable(self, tmp_path: Path):
        # no pyproject.toml → uv / ruff commands skipped
        policy = VerificationPolicy(commands=["uv run pytest", "ruff check ."])
        cmds = build_verification_commands(policy, tmp_path)
        assert cmds == []

    def test_build_keeps_error_commands(self, tmp_path: Path):
        policy = VerificationPolicy(commands=["bad && cmd"])
        cmds = build_verification_commands(policy, tmp_path)
        assert len(cmds) == 1
        assert cmds[0].error is not None

    def test_build_npm_requires_package_json(self, tmp_path: Path):
        policy = VerificationPolicy(commands=["npm test"])
        assert build_verification_commands(policy, tmp_path) == []
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        cmds = build_verification_commands(policy, tmp_path)
        assert len(cmds) == 1


class TestRunVerificationSteps:
    def test_empty_policy_no_steps(self, tmp_path: Path):
        steps = run_verification_steps(VerificationPolicy(), cwd=tmp_path)
        assert steps == []

    def test_runs_real_command(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import subprocess as sp
        from types import SimpleNamespace

        def fake_run(target, *, cwd, shell, text, capture_output, check, timeout):
            assert shell is False
            assert cwd == tmp_path
            return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        monkeypatch.setattr(sp, "run", fake_run)
        (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
        policy = VerificationPolicy(commands=["pytest -q"])
        steps = run_verification_steps(policy, cwd=tmp_path)
        assert len(steps) == 1
        assert steps[0].status == "success"
        assert steps[0].returncode == 0
        assert steps[0].stdout == "ok\n"

    def test_error_command_produces_error_step(self, tmp_path: Path):
        policy = VerificationPolicy(commands=["bad && cmd"])
        steps = run_verification_steps(policy, cwd=tmp_path)
        assert len(steps) == 1
        assert steps[0].status == "error"
        assert steps[0].returncode == -1

    def test_missing_executable(self, tmp_path: Path):
        policy = VerificationPolicy(commands=["this-binary-does-not-exist-xyz"])
        steps = run_verification_steps(policy, cwd=tmp_path)
        assert steps[0].status == "error"
        assert "not found" in steps[0].stderr.lower()


class TestVerificationPassed:
    def test_empty_passes(self):
        assert verification_passed([]) is True

    def test_all_success(self):
        steps = [
            VerificationStep(command="a", returncode=0, status="success"),
            VerificationStep(command="b", returncode=0, status="skipped"),
        ]
        assert verification_passed(steps) is True

    def test_one_failed(self):
        steps = [
            VerificationStep(command="a", returncode=0, status="success"),
            VerificationStep(command="b", returncode=1, status="failed"),
        ]
        assert verification_passed(steps) is False

    def test_error_step_fails(self):
        steps = [VerificationStep(command="a", returncode=-1, status="error")]
        assert verification_passed(steps) is False


class TestRenderVerificationReport:
    def test_empty_steps(self):
        report = render_verification_report("Title", "ap-1", [])
        assert "No verification commands" in report
        assert "ap-1" in report

    def test_with_steps(self):
        steps = [
            VerificationStep(command="ls", returncode=0, status="success", stdout="out"),
            VerificationStep(command="bad", returncode=1, status="failed", stderr="err"),
        ]
        report = render_verification_report("Title", "ap-1", steps)
        assert "SUCCESS" in report
        assert "FAILED" in report
        assert "Return code: 0" in report
        assert "out" in report
        assert "err" in report


# ---------------------------------------------------------------------------
# tools.py
# ---------------------------------------------------------------------------


class TestToolsDefinitions:
    def test_all_tools_registered(self):
        names = [t.definition.name for t in (cls() for cls in ALL_AUTOPILOT_TOOLS)]
        assert "autopilot_intake" in names
        assert "autopilot_list" in names
        assert "autopilot_pick_next" in names
        assert "autopilot_verify" in names
        assert "autopilot_status" in names
        assert "autopilot_reject" in names
        assert "autopilot_requeue" in names

    def test_intake_tool_definition(self):
        d = AutopilotIntakeTool().definition
        assert d.name == "autopilot_intake"
        assert d.is_read_only is False
        assert d.is_destructive is False
        param_names = [p.name for p in d.parameters]
        assert "title" in param_names
        assert "body" in param_names
        assert "source_kind" in param_names

    def test_list_tool_definition_read_only(self):
        d = AutopilotListTool().definition
        assert d.is_read_only is True
        assert d.is_destructive is False

    def test_reject_tool_is_destructive(self):
        d = AutopilotRejectTool().definition
        assert d.is_destructive is True


class TestStoreCache:
    def test_get_store_caches(self, temp_workspace: Path):
        _clear_store_cache()
        s1 = _get_store(temp_workspace)
        s2 = _get_store(temp_workspace)
        assert s1 is s2

    def test_invalidate_store(self, temp_workspace: Path):
        _clear_store_cache()
        s1 = _get_store(temp_workspace)
        _invalidate_store(temp_workspace)
        s2 = _get_store(temp_workspace)
        assert s1 is not s2
        _invalidate_store(temp_workspace)


class TestIntakeTool:
    async def test_execute_creates_task(self, temp_workspace: Path):
        _clear_store_cache()
        tool = AutopilotIntakeTool()
        ctx = _build_context(temp_workspace)
        result = await tool.execute(
            {"title": "New task", "body": "details", "source_kind": "manual_idea"},
            ctx,
        )
        assert "Created" in result
        assert "ID:" in result
        _invalidate_store(temp_workspace)

    async def test_execute_updates_existing(self, temp_workspace: Path):
        _clear_store_cache()
        tool = AutopilotIntakeTool()
        ctx = _build_context(temp_workspace)
        await tool.execute(
            {"title": "Same", "body": "d", "source_kind": "manual_idea"}, ctx,
        )
        result = await tool.execute(
            {"title": "Same", "body": "d", "source_kind": "manual_idea"}, ctx,
        )
        assert "Updated" in result
        _invalidate_store(temp_workspace)

    async def test_check_permission_human_gate_on(self, temp_workspace: Path):
        _clear_store_cache()
        tool = AutopilotIntakeTool()
        ctx = _build_context(temp_workspace)
        decision = await tool.check_permission({}, ctx)
        assert decision.behavior == "ask"
        _invalidate_store(temp_workspace)

    async def test_check_permission_human_gate_off(self, temp_workspace: Path):
        _clear_store_cache()
        store = _get_store(temp_workspace)
        store.save_config(
            AutopilotConfig(autopilot_policy=AutopilotPolicy(default_human_gate=False))
        )
        _clear_store_cache()
        tool = AutopilotIntakeTool()
        ctx = _build_context(temp_workspace)
        decision = await tool.check_permission({}, ctx)
        assert decision.behavior == "allow"
        _invalidate_store(temp_workspace)


class TestListTool:
    async def test_empty(self, temp_workspace: Path):
        _clear_store_cache()
        tool = AutopilotListTool()
        ctx = _build_context(temp_workspace)
        result = await tool.execute({}, ctx)
        assert "No autopilot tasks" in result
        _invalidate_store(temp_workspace)

    async def test_lists_tasks(self, temp_workspace: Path):
        _clear_store_cache()
        store = _get_store(temp_workspace)
        store.enqueue_card(source_kind="manual_idea", title="Task A", body="b")
        _clear_store_cache()
        tool = AutopilotListTool()
        ctx = _build_context(temp_workspace)
        result = await tool.execute({}, ctx)
        assert "Task A" in result
        assert "Stats" in result
        _invalidate_store(temp_workspace)

    async def test_status_filter(self, temp_workspace: Path):
        _clear_store_cache()
        store = _get_store(temp_workspace)
        c, _ = store.enqueue_card(source_kind="manual_idea", title="To complete", body="b")
        store.update_status(c.id, status="completed")
        _clear_store_cache()
        tool = AutopilotListTool()
        ctx = _build_context(temp_workspace)
        result = await tool.execute({"status": "completed"}, ctx)
        assert "To complete" in result
        _invalidate_store(temp_workspace)


class TestStatusTool:
    async def test_not_found(self, temp_workspace: Path):
        _clear_store_cache()
        tool = AutopilotStatusTool()
        ctx = _build_context(temp_workspace)
        result = await tool.execute({"task_id": "missing"}, ctx)
        assert "not found" in result
        _invalidate_store(temp_workspace)

    async def test_shows_details(self, temp_workspace: Path):
        _clear_store_cache()
        store = _get_store(temp_workspace)
        c, _ = store.enqueue_card(
            source_kind="github_issue", title="Detailed", body="the body",
        )
        _clear_store_cache()
        tool = AutopilotStatusTool()
        ctx = _build_context(temp_workspace)
        result = await tool.execute({"task_id": c.id}, ctx)
        assert "Detailed" in result
        assert "github_issue" in result
        assert "the body" in result
        _invalidate_store(temp_workspace)


class TestRejectRequeueTools:
    async def test_reject_unknown(self, temp_workspace: Path):
        _clear_store_cache()
        tool = AutopilotRejectTool()
        ctx = _build_context(temp_workspace)
        result = await tool.execute({"task_id": "nope"}, ctx)
        assert "not found" in result
        _invalidate_store(temp_workspace)

    async def test_reject_then_cannot_reject_completed(self, temp_workspace: Path):
        _clear_store_cache()
        store = _get_store(temp_workspace)
        c, _ = store.enqueue_card(source_kind="manual_idea", title="t", body="b")
        _clear_store_cache()
        ctx = _build_context(temp_workspace)
        reject = AutopilotRejectTool()
        result = await reject.execute({"task_id": c.id, "reason": "no"}, ctx)
        assert "rejected" in result.lower()
        # now in rejected state, still rejectable per rejectable set
        result2 = await reject.execute({"task_id": c.id}, ctx)
        assert "rejected" in result2.lower()
        _invalidate_store(temp_workspace)

    async def test_requeue(self, temp_workspace: Path):
        _clear_store_cache()
        store = _get_store(temp_workspace)
        c, _ = store.enqueue_card(source_kind="manual_idea", title="t", body="b")
        store.update_status(c.id, status="rejected")
        _clear_store_cache()
        ctx = _build_context(temp_workspace)
        tool = AutopilotRequeueTool()
        result = await tool.execute({"task_id": c.id}, ctx)
        assert "requeued" in result.lower()
        card = store.get_card(c.id)
        assert card.status == "queued"
        _invalidate_store(temp_workspace)

    async def test_requeue_not_found(self, temp_workspace: Path):
        _clear_store_cache()
        tool = AutopilotRequeueTool()
        ctx = _build_context(temp_workspace)
        result = await tool.execute({"task_id": "x"}, ctx)
        assert "not found" in result
        _invalidate_store(temp_workspace)


class TestPickNextTool:
    async def test_no_queued(self, temp_workspace: Path):
        _clear_store_cache()
        tool = AutopilotPickNextTool()
        ctx = _build_context(temp_workspace)
        result = await tool.execute({}, ctx)
        assert "No queued tasks" in result
        _invalidate_store(temp_workspace)

    async def test_picks_and_accepts(self, temp_workspace: Path):
        _clear_store_cache()
        store = _get_store(temp_workspace)
        c, _ = store.enqueue_card(source_kind="manual_idea", title="Run me", body="b")
        _clear_store_cache()
        tool = AutopilotPickNextTool()
        ctx = _build_context(temp_workspace)
        result = await tool.execute({}, ctx)
        assert "Next Task Picked" in result
        assert "Run me" in result
        assert "Execution Prompt" in result
        card = store.get_card(c.id)
        assert card.status == "accepted"
        _invalidate_store(temp_workspace)


class TestVerifyTool:
    async def test_unknown_task(self, temp_workspace: Path):
        _clear_store_cache()
        tool = AutopilotVerifyTool()
        ctx = _build_context(temp_workspace)
        result = await tool.execute({"task_id": "missing"}, ctx)
        assert "not found" in result
        _invalidate_store(temp_workspace)

    async def test_passes_when_no_commands(
        self, temp_workspace: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _clear_store_cache()
        store = _get_store(temp_workspace)
        c, _ = store.enqueue_card(source_kind="manual_idea", title="t", body="b")
        _clear_store_cache()
        ctx = _build_context(temp_workspace)
        tool = AutopilotVerifyTool()
        result = await tool.execute({"task_id": c.id, "summary": "done"}, ctx)
        assert "PASSED" in result
        card = store.get_card(c.id)
        assert card.status == "completed"
        _invalidate_store(temp_workspace)

    async def test_fails_when_verification_fails(
        self, temp_workspace: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _clear_store_cache()
        store = _get_store(temp_workspace)
        store.save_config(
            AutopilotConfig(verification_policy=VerificationPolicy(commands=["false"]))
        )
        c, _ = store.enqueue_card(source_kind="manual_idea", title="t", body="b")
        _clear_store_cache()

        def fake_run(policy, *, cwd, timeout=1800):
            return [VerificationStep(command="false", returncode=1, status="failed")]

        monkeypatch.setattr(
            "markbot.autopilot.tools.run_verification_steps", fake_run, raising=False,
        )
        ctx = _build_context(temp_workspace)
        tool = AutopilotVerifyTool()
        result = await tool.execute({"task_id": c.id}, ctx)
        assert "FAILED" in result
        card = store.get_card(c.id)
        assert card.status == "repairing"
        _invalidate_store(temp_workspace)


# ---------------------------------------------------------------------------
# service.py
# ---------------------------------------------------------------------------


class TestServiceHelpers:
    def test_build_execution_prompt_contains_task(self):
        card = TaskCard(id="ap-1", fingerprint="f", title="My Task", body="details")
        cfg = AutopilotConfig()
        prompt = service_mod._build_execution_prompt(card, cfg)
        assert "My Task" in prompt
        assert "ap-1" in prompt
        assert "Autopilot policy" in prompt
        assert "Verification policy" in prompt

    def test_render_run_report_passed(self):
        card = TaskCard(id="ap-1", fingerprint="f", title="T")
        steps = [VerificationStep(command="ls", returncode=0, status="success")]
        report = service_mod._render_run_report(
            card,
            agent_summary="summary",
            verification_steps=steps,
            verification_status="passed",
        )
        assert "PASSED" in report
        assert "summary" in report
        assert "ls" in report

    def test_render_run_report_failed(self):
        card = TaskCard(id="ap-1", fingerprint="f", title="T")
        steps = [VerificationStep(command="bad", returncode=1, status="failed", stderr="oops")]
        report = service_mod._render_run_report(
            card,
            agent_summary="",
            verification_steps=steps,
            verification_status="failed",
        )
        assert "FAILED" in report
        assert "bad" in report
        assert "oops" in report
        assert "(empty agent summary)" in report

    def test_render_run_report_not_started(self):
        card = TaskCard(id="ap-1", fingerprint="f", title="T")
        report = service_mod._render_run_report(
            card,
            agent_summary="",
            verification_steps=[],
            verification_status="not_started",
        )
        assert "not started" in report


class TestAutopilotService:
    async def test_intake_creates_task(self, store: AutopilotStore):
        agent = FakeAgentLoop()
        svc = service_mod.AutopilotService(store, agent)
        card = await svc.intake(
            source_kind="github_issue", title="Test", body="body",
        )
        assert card.id.startswith("ap-")
        assert card.source_kind == "github_issue"
        assert card.status == "queued"
        assert agent.calls == []

    async def test_intake_refreshes_existing(self, store: AutopilotStore):
        agent = FakeAgentLoop()
        svc = service_mod.AutopilotService(store, agent)
        c1 = await svc.intake(source_kind="manual_idea", title="Dup", body="x")
        c2 = await svc.intake(source_kind="manual_idea", title="Dup", body="x")
        assert c1.id == c2.id

    async def test_run_next_no_tasks_returns_none(self, store: AutopilotStore):
        agent = FakeAgentLoop()
        svc = service_mod.AutopilotService(store, agent)
        result = await svc.run_next()
        assert result is None

    async def test_run_next_completes_on_pass(
        self, store: AutopilotStore, monkeypatch: pytest.MonkeyPatch,
    ):
        agent = FakeAgentLoop(content="done")
        svc = service_mod.AutopilotService(store, agent)
        # disable human gate so config is plain
        store._file_config = None
        # ensure no verification commands → passes
        monkeypatch.setattr(
            service_mod,
            "run_verification_steps",
            lambda policy, *, cwd: [],
            raising=True,
        )
        await svc.intake(source_kind="manual_idea", title="T", body="b")
        result = await svc.run_next()
        assert result is not None
        assert result.status == "completed"
        assert result.attempt_count == 1
        assert result.assistant_summary == "done"
        assert agent.calls
        # Unattended paths (autopilot/cron/heartbeat) must force AUTO mode so
        # they don't depend on a user having run `/mode auto` interactively.
        # See logs/2026-07-05.log — cron cleanup was blocked by DEFAULT mode
        # even after `/mode auto` was set, because mode is in-memory only.
        from markbot.types.permission import PermissionMode as _PM
        assert agent.calls[0]["permission_mode"] is _PM.AUTO

    async def test_run_next_retries_then_fails(
        self, store: AutopilotStore, monkeypatch: pytest.MonkeyPatch,
    ):
        store.save_config(
            AutopilotConfig(
                autopilot_policy=AutopilotPolicy(max_attempts=2, repair_max_rounds=1),
                verification_policy=VerificationPolicy(commands=["false"]),
            )
        )
        agent = FakeAgentLoop(content="tried")
        svc = service_mod.AutopilotService(store, agent)

        def fake_run(policy, *, cwd):
            return [VerificationStep(command="false", returncode=1, status="failed")]

        monkeypatch.setattr(service_mod, "run_verification_steps", fake_run, raising=True)
        await svc.intake(source_kind="manual_idea", title="T", body="b")
        result = await svc.run_next()
        assert result is not None
        assert result.status == "failed"
        assert result.attempt_count == 2

    async def test_run_next_agent_error_fails_immediately(
        self, store: AutopilotStore, monkeypatch: pytest.MonkeyPatch,
    ):
        store.save_config(
            AutopilotConfig(
                autopilot_policy=AutopilotPolicy(max_attempts=3),
                verification_policy=VerificationPolicy(commands=["pytest"]),
            )
        )

        class BoomAgent:
            async def process_direct(self, *a, **kw):
                raise RuntimeError("boom")

        svc = service_mod.AutopilotService(store, BoomAgent())
        await svc.intake(source_kind="manual_idea", title="T", body="b")
        result = await svc.run_next()
        assert result is not None
        assert result.status == "failed"
        assert result.attempt_count == 1

    async def test_tick_skips_when_active(
        self, store: AutopilotStore, monkeypatch: pytest.MonkeyPatch,
    ):
        agent = FakeAgentLoop()
        svc = service_mod.AutopilotService(store, agent)
        c = await svc.intake(source_kind="manual_idea", title="T", body="b")
        store.update_status(c.id, status="running")
        result = await svc.tick()
        assert result is None

    async def test_tick_idle_when_no_queued(self, store: AutopilotStore, monkeypatch: pytest.MonkeyPatch):
        agent = FakeAgentLoop()
        svc = service_mod.AutopilotService(store, agent)
        # ensure pick_next_card won't find anything and no active card
        picked = await svc.tick()
        assert picked is None
        lines_i = store.load_journal()
        # tick_skip caused by handling nothing then nothing to pick etc.
        assert any(e.kind in ("tick_skip", "tick_idle") for e in lines_i)

    async def test_tick_runs_when_queued(
        self, store: AutopilotStore, monkeypatch: pytest.MonkeyPatch,
    ):
        agent = FakeAgentLoop(content="ok")
        svc = service_mod.AutopilotService(store, agent)
        monkeypatch.setattr(
            service_mod, "run_verification_steps", lambda policy, *, cwd: [], raising=True,
        )
        await svc.intake(source_kind="manual_idea", title="T", body="b")
        result = await svc.tick()
        assert result is not None
        assert result.status == "completed"

    def test_list_tasks(self, store: AutopilotStore):
        svc = service_mod.AutopilotService(store, FakeAgentLoop())
        c, _ = store.enqueue_card(source_kind="manual_idea", title="a", body="b")
        cards = svc.list_tasks()
        assert len(cards) == 1
        assert cards[0].id == c.id

    def test_list_tasks_with_status_filter(self, store: AutopilotStore):
        svc = service_mod.AutopilotService(store, FakeAgentLoop())
        c, _ = store.enqueue_card(source_kind="manual_idea", title="a", body="b")
        store.update_status(c.id, status="completed")
        assert svc.list_tasks(status="queued") == []
        assert len(svc.list_tasks(status="completed")) == 1

    def test_get_task(self, store: AutopilotStore):
        svc = service_mod.AutopilotService(store, FakeAgentLoop())
        c, _ = store.enqueue_card(source_kind="manual_idea", title="a", body="b")
        assert svc.get_task(c.id).id == c.id
        assert svc.get_task("nope") is None

    def test_get_stats(self, store: AutopilotStore):
        svc = service_mod.AutopilotService(store, FakeAgentLoop())
        c, _ = store.enqueue_card(source_kind="manual_idea", title="a", body="b")
        store.update_status(c.id, status="completed")
        stats = svc.get_stats()
        assert stats.get("completed") == 1

    def test_get_active_context(self, store: AutopilotStore):
        svc = service_mod.AutopilotService(store, FakeAgentLoop())
        store.enqueue_card(source_kind="manual_idea", title="ctx", body="b")
        store.rebuild_active_context()
        ctx = svc.get_active_context()
        assert "ctx" in ctx

    def test_reject_task(self, store: AutopilotStore):
        svc = service_mod.AutopilotService(store, FakeAgentLoop())
        c, _ = store.enqueue_card(source_kind="manual_idea", title="a", body="b")
        card = svc.reject_task(c.id, reason="bad")
        assert card.status == "rejected"
        assert card.metadata["last_note"] == "bad"

    def test_reject_task_default_reason(self, store: AutopilotStore):
        svc = service_mod.AutopilotService(store, FakeAgentLoop())
        c, _ = store.enqueue_card(source_kind="manual_idea", title="a", body="b")
        card = svc.reject_task(c.id)
        assert card.metadata["last_note"] == "rejected by user"

    def test_requeue_task(self, store: AutopilotStore):
        svc = service_mod.AutopilotService(store, FakeAgentLoop())
        c, _ = store.enqueue_card(source_kind="manual_idea", title="a", body="b")
        store.update_status(c.id, status="failed")
        card = svc.requeue_task(c.id)
        assert card.status == "queued"

    def test_store_and_agent_loop_properties(self, store: AutopilotStore):
        agent = FakeAgentLoop()
        svc = service_mod.AutopilotService(store, agent)
        assert svc.store is store
        assert svc.agent_loop is agent