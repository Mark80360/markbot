"""Tests for markbot.utils.audit — audit logging."""

import json
from pathlib import Path

from markbot.utils.audit import (
    AuditEvent,
    AuditLogger,
    AuditOutcome,
    JsonlSink,
    LogSink,
)


class TestAuditEvent:
    def test_auto_fields(self):
        event = AuditEvent(action="tool.invoke", actor="user:alice", resource="read_file")
        assert event.event_id
        assert event.timestamp
        assert event.outcome == "success"

    def test_to_dict(self):
        event = AuditEvent(
            action="tool.invoke",
            actor="user:alice",
            resource="read_file",
            outcome="success",
            details={"path": "/data/report.csv"},
        )
        d = event.to_dict()
        assert d["action"] == "tool.invoke"
        assert d["actor"] == "user:alice"
        assert d["resource"] == "read_file"
        assert d["details"]["path"] == "/data/report.csv"

    def test_to_json(self):
        event = AuditEvent(action="test", actor="user", resource="res")
        j = event.to_json()
        parsed = json.loads(j)
        assert parsed["action"] == "test"

    def test_to_cef(self):
        event = AuditEvent(
            action="tool.invoke",
            actor="user:alice",
            resource="read_file",
            outcome="denied",
        )
        cef = event.to_cef()
        assert cef.startswith("CEF:0|markbot|markbot|1.0|tool.invoke|")
        assert "denied" in cef
        assert "7" in cef  # severity for denied


class TestJsonlSink:
    def test_write_event(self, tmp_path: Path):
        path = tmp_path / "audit.jsonl"
        sink = JsonlSink(path)
        event = AuditEvent(action="test", actor="user", resource="res")
        sink.write(event)
        sink.flush()
        sink.close()

        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["action"] == "test"


class TestAuditLogger:
    def test_log_action(self):
        sink = LogSink()
        logger = AuditLogger(sink)
        logger.log_action("tool.invoke", "user:alice", "read_file", outcome="success")
        logger.flush()
        logger.close()

    def test_multiple_sinks(self, tmp_path: Path):
        path = tmp_path / "audit.jsonl"
        jsonl_sink = JsonlSink(path)
        log_sink = LogSink()
        audit = AuditLogger(jsonl_sink, log_sink)

        audit.log_action("test.action", "user", "resource")
        audit.flush()
        audit.close()

        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
