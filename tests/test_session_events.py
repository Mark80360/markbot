"""Tests for the session event-sourcing layer."""

import json
from pathlib import Path

from markbot.session.events import (
    EVENT_APPROVAL_ASKED,
    EVENT_CONTROL_NOTE,
    event_for_message,
    make_event,
    next_seq,
)
from markbot.session.session import Session, SessionManager


class TestEventEnvelope:
    def test_make_event_shape(self):
        envelope = make_event(3, "user_message", {"content": "hi"})
        assert envelope["_type"] == "event"
        assert envelope["seq"] == 3
        assert envelope["event"] == "user_message"
        assert envelope["ignorable"] is False
        assert envelope["payload"] == {"content": "hi"}

    def test_next_seq_contiguous(self):
        events = [make_event(i, "x") for i in range(5)]
        assert next_seq(events) == 5
        assert next_seq([]) == 0

    def test_unknown_role_maps_to_control_note(self):
        event_type, payload = event_for_message({"role": "weird", "content": "?"})
        assert event_type == EVENT_CONTROL_NOTE


class TestSessionEventLog:
    def test_add_message_logs_events(self):
        s = Session(key="t:1")
        s.add_message("user", "hello")
        assert len(s.events) == 1
        assert s.events[0]["event"] == "user_message"
        assert s.events[0]["payload"]["content"] == "hello"

    def test_tool_result_payload_keeps_linkage(self):
        s = Session(key="t:1")
        tool_calls = [{"id": "c1", "function": {"name": "echo", "arguments": "{}"}}]
        s.add_message("assistant", "", tool_calls=tool_calls)
        s.add_message("tool", "out", tool_call_id="c1", name="echo")
        assistant_env = s.events[0]
        assert assistant_env["payload"]["tool_calls"] == tool_calls
        result_env = s.events[1]
        assert result_env["event"] == "tool_result"
        assert result_env["payload"]["tool_call_id"] == "c1"

    def test_append_event_manual_audit(self):
        s = Session(key="t:1")
        s.append_event(EVENT_APPROVAL_ASKED, {"tool": "exec"}, ignorable=False)
        s.append_event("custom_thing", {}, ignorable=True)
        assert s.events[0]["seq"] == 0
        assert s.events[1]["ignorable"] is True

    def test_derive_messages_roundtrip(self):
        s = Session(key="t:1")
        s.add_message("user", "q")
        s.add_message("assistant", "a", tool_calls=[{"id": "c", "function": {}}])
        s.add_message("tool", "r", tool_call_id="c", name="n")
        s.append_event("approval_asked", {})  # non-model event skipped
        derived = s.derive_messages()
        roles = [m["role"] for m in derived]
        assert roles == ["user", "assistant", "tool"]
        assert derived[2]["tool_call_id"] == "c"

    def test_derive_upto_seq_boundary(self):
        s = Session(key="t:1")
        s.add_message("user", "first")
        s.add_message("assistant", "second")
        derived = s.derive_messages(upto_seq=1)
        assert len(derived) == 1
        assert derived[0]["content"] == "first"

    def test_clear_bounds_projection_but_preserves_audit(self):
        s = Session(key="t:1")
        s.add_message("user", "old")
        s.clear()
        # messages gone; the projection honours history_reset as a boundary
        assert s.messages == []
        assert s.derive_messages() == []
        reset_events = [e for e in s.events if e["event"] == "history_reset"]
        assert len(reset_events) == 1
        assert reset_events[0]["payload"]["previous_message_count"] == 1
        # the audit trail still contains the pre-reset message event
        user_events = [e for e in s.events if e["event"] == "user_message"]
        assert len(user_events) == 1

    def test_retain_drops_leading_projection(self):
        s = Session(key="t:1")
        s.add_message("user", "u1")
        s.add_message("assistant", "a1")
        s.add_message("user", "u2")
        s.add_message("assistant", "a2")
        s.retain_recent_legal_suffix(max_messages=2)
        # live messages kept only the legal suffix starting at u2
        assert [m["content"] for m in s.messages] == ["u2", "a2"]
        derived = s.derive_messages()
        assert [m["content"] for m in derived] == ["u2", "a2"]

    def test_reset_then_new_messages(self):
        s = Session(key="t:1")
        s.add_message("user", "old")
        s.add_message("assistant", "old-reply")
        s.clear()
        s.add_message("user", "fresh")
        derived = s.derive_messages()
        assert [m["content"] for m in derived] == ["fresh"]


class TestPersistence:
    def _manager(self, tmp_path):
        return SessionManager(Path(tmp_path))

    def test_roundtrip_with_events(self, tmp_path):
        mgr = self._manager(tmp_path)
        sess = mgr.get_or_create("feishu:chat1")
        sess.add_message("user", "persist me")
        sess.add_message("assistant", "done")
        sess.append_event(EVENT_APPROVAL_ASKED, {"tool": "exec"})
        mgr.save(sess)

        mgr2 = SessionManager(Path(tmp_path))
        reloaded = mgr2.get_or_create("feishu:chat1")
        assert len(reloaded.messages) == 2
        assert len(reloaded.events) == 3
        derived = reloaded.derive_messages()
        assert derived[0]["content"] == "persist me"

    def test_legacy_file_without_events_loads(self, tmp_path):
        sessions_dir = Path(tmp_path) / "sessions"
        sessions_dir.mkdir(parents=True)
        lines = [
            json.dumps(
                {
                    "_type": "metadata",
                    "format_version": 1,
                    "key": "legacy:key",
                    "created_at": "2026-01-01T00:00:00",
                    "updated_at": "2026-01-01T00:00:00",
                    "metadata": {},
                    "last_consolidated": 0,
                }
            ),
            json.dumps({"role": "user", "content": "legacy msg", "timestamp": "2026-01-01T00:00:01"}),
        ]
        (sessions_dir / "legacy_key.jsonl").write_text("\n".join(lines), encoding="utf-8")

        mgr = SessionManager(Path(tmp_path))
        sess = mgr.get_or_create("legacy:key")
        assert len(sess.messages) == 1
        assert sess.events == []

    def test_incremental_save_appends_new_events_only(self, tmp_path):
        mgr = self._manager(tmp_path)
        sess = mgr.get_or_create("inc:key")
        sess.add_message("user", "one")
        mgr.save(sess)
        path = next((Path(tmp_path) / "sessions").glob("inc_key.jsonl"))
        lines_after_first = len(path.read_text(encoding="utf-8").strip().splitlines())

        sess.add_message("assistant", "two")
        mgr.save(sess)
        lines_after_second = len(path.read_text(encoding="utf-8").strip().splitlines())
        # one message line + one event line appended (2 metadata+msg first save)
        assert lines_after_second - lines_after_first == 2

    def test_corrupt_event_line_skipped(self, tmp_path):
        sessions_dir = Path(tmp_path) / "sessions"
        sessions_dir.mkdir(parents=True)
        content = (
            json.dumps(
                {
                    "_type": "metadata",
                    "format_version": 1,
                    "key": "c:k",
                    "created_at": "2026-01-01T00:00:00",
                    "updated_at": "2026-01-01T00:00:00",
                    "metadata": {},
                    "last_consolidated": 0,
                }
            )
            + "\n"
            + '{"_type": "event", broken json\n'
            + json.dumps({"role": "user", "content": "ok"}) + "\n"
        )
        (sessions_dir / "c_k.jsonl").write_text(content, encoding="utf-8")
        mgr = SessionManager(Path(tmp_path))
        sess = mgr.get_or_create("c:k")
        assert len(sess.messages) == 1

    def test_recovery_rebuilds_messages_from_events(self, tmp_path):
        """Message lines corrupted but event lines survive → rebuild."""
        sessions_dir = Path(tmp_path) / "sessions"
        sessions_dir.mkdir(parents=True)
        # Build a healthy session first to obtain real event envelopes.
        src = Session(key="victim:key")
        src.add_message("user", "question")
        src.add_message("assistant", "answer")
        # Write the file with ONLY the metadata line + event envelopes;
        # message lines are "lost" (simulated corruption).
        lines = [
            json.dumps(
                {
                    "_type": "metadata",
                    "format_version": 1,
                    "key": "victim:key",
                    "created_at": "2026-01-01T00:00:00",
                    "updated_at": "2026-01-01T00:00:00",
                    "metadata": {},
                    "last_consolidated": 0,
                }
            )
        ]
        lines.extend(json.dumps(e, ensure_ascii=False) for e in src.events)
        (sessions_dir / "victim_key.jsonl").write_text("\n".join(lines), encoding="utf-8")

        mgr = SessionManager(Path(tmp_path))
        sess = mgr.get_or_create("victim:key")
        assert len(sess.messages) == 2
        assert sess.messages[0]["content"] == "question"
        assert sess._dirty is True
        # Saving persists the repaired state.
        mgr.save(sess)
        mgr2 = SessionManager(Path(tmp_path))
        again = mgr2.get_or_create("victim:key")
        assert len(again.messages) == 2

    def test_recovery_skips_intentionally_cleared_sessions(self, tmp_path):
        sessions_dir = Path(tmp_path) / "sessions"
        sessions_dir.mkdir(parents=True)
        src = Session(key="cleared:key")
        src.add_message("user", "old")
        src.clear()  # boundary event bounds the projection to empty
        lines = [
            json.dumps(
                {
                    "_type": "metadata",
                    "format_version": 1,
                    "key": "cleared:key",
                    "created_at": "2026-01-01T00:00:00",
                    "updated_at": "2026-01-01T00:00:00",
                    "metadata": {},
                    "last_consolidated": 0,
                }
            )
        ]
        lines.extend(json.dumps(e, ensure_ascii=False) for e in src.events)
        (sessions_dir / "cleared_key.jsonl").write_text("\n".join(lines), encoding="utf-8")

        mgr = SessionManager(Path(tmp_path))
        sess = mgr.get_or_create("cleared:key")
        assert sess.messages == []  # nothing resurrected


class TestRebuildDirect:
    def test_rebuild_noop_when_messages_exist(self):
        s = Session(key="t:1")
        s.add_message("user", "x")
        assert s.rebuild_messages_from_events() == 0
        assert len(s.messages) == 1

    def test_rebuild_noop_when_projection_empty(self):
        s = Session(key="t:1")
        assert s.rebuild_messages_from_events() == 0
