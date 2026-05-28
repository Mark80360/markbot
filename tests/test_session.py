"""Tests for markbot.session module (session, handoff)."""

import json
import pytest
from pathlib import Path
from datetime import datetime

from markbot.session.session import Session
from markbot.session.handoff import (
    HandoffTask,
    HandoffDecision,
    HandoffBlocker,
    SessionHandoff,
    HandoffManager,
)


class TestSession:
    def test_create_session(self):
        s = Session(key="cli:main")
        assert s.key == "cli:main"
        assert s.messages == []
        assert s.last_consolidated == 0

    def test_add_message(self):
        s = Session(key="cli:main")
        s.add_message("user", "Hello!")
        s.add_message("assistant", "Hi there!")
        assert len(s.messages) == 2
        assert s.messages[0]["role"] == "user"
        assert s.messages[1]["role"] == "assistant"
        assert s.messages[0]["content"] == "Hello!"

    def test_add_message_updates_timestamp(self):
        s = Session(key="cli:main")
        before = s.updated_at
        s.add_message("user", "test")
        assert s.updated_at >= before

    def test_add_message_with_kwargs(self):
        s = Session(key="cli:main")
        s.add_message("tool", "result", tool_call_id="call_1", name="read_file")
        assert s.messages[0]["tool_call_id"] == "call_1"
        assert s.messages[0]["name"] == "read_file"

    def test_get_history(self):
        s = Session(key="cli:main")
        s.add_message("system", "You are helpful")
        s.add_message("user", "Hello")
        s.add_message("assistant", "Hi")
        history = s.get_history()
        assert len(history) >= 2

    def test_get_history_max_messages(self):
        s = Session(key="cli:main")
        for i in range(20):
            s.add_message("user", f"msg {i}")
        history = s.get_history(max_messages=5)
        assert len(history) <= 5

    def test_get_history_no_limit(self):
        s = Session(key="cli:main")
        for i in range(10):
            s.add_message("user", f"msg {i}")
        history = s.get_history(max_messages=0)
        assert len(history) == 10

    def test_get_history_starts_with_user(self):
        s = Session(key="cli:main")
        s.add_message("system", "sys")
        s.add_message("assistant", "hi")
        s.add_message("user", "hello")
        s.add_message("assistant", "response")
        history = s.get_history()
        if history:
            assert history[0]["role"] == "user"

    def test_strip_orphan_tool_results(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "content": "orphan result", "tool_call_id": "missing_id"},
            {"role": "assistant", "content": "response"},
        ]
        result = Session._strip_orphan_tool_results(messages)
        assert len(result) == 2
        assert result[1]["role"] == "assistant"

    def test_strip_orphan_keeps_valid_tool_results(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "function": {"name": "read"}}]},
            {"role": "tool", "content": "result", "tool_call_id": "call_1"},
        ]
        result = Session._strip_orphan_tool_results(messages)
        assert len(result) == 3


class TestHandoffTask:
    def test_defaults(self):
        t = HandoffTask()
        assert t.status == "stated"
        assert t.progress == ""
        assert t.verification == ""

    def test_custom_values(self):
        t = HandoffTask(id="T1", title="Fix bug", status="in_progress", progress="50%")
        assert t.id == "T1"
        assert t.status == "in_progress"


class TestHandoffDecision:
    def test_defaults(self):
        d = HandoffDecision()
        assert d.summary == ""
        assert d.context == ""


class TestHandoffBlocker:
    def test_defaults(self):
        b = HandoffBlocker()
        assert b.description == ""
        assert b.task_id == ""


class TestSessionHandoff:
    def test_defaults(self):
        h = SessionHandoff()
        assert h.active_tasks == []
        assert h.key_decisions == []
        assert h.blockers == []
        assert h.cost_this_session_usd == 0.0

    def test_to_markdown(self):
        h = SessionHandoff(
            session_key="cli:main",
            timestamp="2025-01-01T00:00:00",
            active_tasks=[HandoffTask(id="T1", title="Task 1", status="in_progress")],
            next_best_step="Continue with T1",
        )
        md = h.to_markdown()
        assert "Session Handoff" in md
        assert "T1" in md
        assert "Continue with T1" in md

    def test_to_markdown_with_blockers(self):
        h = SessionHandoff(
            blockers=[HandoffBlocker(description="API down", task_id="T2")],
        )
        md = h.to_markdown()
        assert "API down" in md

    def test_to_markdown_with_stats(self):
        h = SessionHandoff(
            cost_this_session_usd=0.05,
            tool_calls_this_session=10,
        )
        md = h.to_markdown()
        assert "$0.0500" in md
        assert "10" in md

    def test_to_dict(self):
        h = SessionHandoff(session_key="cli:main", next_best_step="test")
        d = h.to_dict()
        assert d["session_key"] == "cli:main"
        assert d["next_best_step"] == "test"

    def test_from_dict(self):
        data = {
            "session_key": "cli:main",
            "timestamp": "2025-01-01",
            "active_tasks": [{"id": "T1", "title": "Task", "status": "stated", "progress": "", "verification": ""}],
            "key_decisions": [{"summary": "Decided X", "context": ""}],
            "blockers": [],
            "next_best_step": "Do X",
            "user_preferences_noted": ["pref1"],
            "cost_this_session_usd": 0.01,
            "tool_calls_this_session": 5,
        }
        h = SessionHandoff.from_dict(data)
        assert h.session_key == "cli:main"
        assert len(h.active_tasks) == 1
        assert h.active_tasks[0].id == "T1"
        assert len(h.key_decisions) == 1
        assert h.user_preferences_noted == ["pref1"]


class TestHandoffManager:
    def test_save_and_load(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        mgr = HandoffManager(workspace)

        h = SessionHandoff(
            session_key="cli:main",
            timestamp="2025-01-01",
            next_best_step="Continue",
        )
        path = mgr.save(h)
        assert path.exists()

        loaded = mgr.load("cli:main")
        assert loaded is not None
        assert loaded.session_key == "cli:main"
        assert loaded.next_best_step == "Continue"

    def test_load_nonexistent(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        mgr = HandoffManager(workspace)
        assert mgr.load("nonexistent") is None

    def test_load_markdown(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        mgr = HandoffManager(workspace)

        h = SessionHandoff(session_key="cli:main", timestamp="2025-01-01")
        mgr.save(h)
        md = mgr.load_markdown("cli:main")
        assert md is not None
        assert "Session Handoff" in md

    def test_delete(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        mgr = HandoffManager(workspace)

        h = SessionHandoff(session_key="cli:main", timestamp="2025-01-01")
        mgr.save(h)
        mgr.delete("cli:main")
        assert mgr.load("cli:main") is None

    def test_cleanup_stale(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        mgr = HandoffManager(workspace)

        h = SessionHandoff(session_key="cli:old", timestamp="2020-01-01")
        path = mgr.save(h)

        import os
        import time
        old_time = os.path.getmtime(path) - 31 * 86400
        os.utime(path, (old_time, old_time))

        removed = mgr.cleanup_stale()
        assert removed == 1
        assert mgr.load("cli:old") is None
