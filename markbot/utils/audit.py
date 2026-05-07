"""Audit logging — AuditLogger and SIEM integration.

Provides structured, append-only audit logging for security-relevant
events (tool invocations, configuration changes, access decisions).
Supports multiple output backends including local JSONL files and
SIEM-compatible HTTP/CEF exporters.

Usage::

    from markbot.utils.audit import get_audit_logger, AuditEvent

    audit = get_audit_logger()

    audit.log(AuditEvent(
        action="tool.invoke",
        actor="user:alice",
        resource="filesystem.read_file",
        outcome="success",
        details={"path": "/data/report.csv"},
    ))
"""

from __future__ import annotations

import json
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from loguru import logger


class AuditOutcome(Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"
    ERROR = "error"


@dataclass
class AuditEvent:
    """Structured audit event record."""

    action: str
    actor: str
    resource: str
    outcome: str = "success"
    details: dict[str, Any] = field(default_factory=dict)
    session_key: str = ""
    correlation_id: str = ""
    timestamp: str = ""
    event_id: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()
        if not self.event_id:
            self.event_id = uuid.uuid4().hex[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "action": self.action,
            "actor": self.actor,
            "resource": self.resource,
            "outcome": self.outcome,
            "session_key": self.session_key,
            "correlation_id": self.correlation_id,
            "details": self.details,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)

    def to_cef(self, vendor: str = "markbot", product: str = "markbot", version: str = "1.0") -> str:
        """Format as Common Event Format (CEF) string for SIEM ingestion."""
        severity_map = {"success": "1", "failure": "5", "denied": "7", "error": "8"}
        sev = severity_map.get(self.outcome, "3")
        name = self.action.replace(".", " ")
        extension = (
            f"act={self.action} "
            f"suser={self.actor} "
            f"outcome={self.outcome} "
            f"cs1={self.resource} "
            f"cs1Label=Resource "
            f"msg={json.dumps(self.details, default=str)[:512]}"
        )
        return f"CEF:0|{vendor}|{product}|{version}|{self.action}|{name}|{sev}|{extension}"


class AuditSink(ABC):
    """Abstract base for audit log output destinations."""

    @abstractmethod
    def write(self, event: AuditEvent) -> None:
        ...

    @abstractmethod
    def flush(self) -> None:
        ...

    @abstractmethod
    def close(self) -> None:
        ...


class JsonlSink(AuditSink):
    """Append-only JSONL file sink."""

    def __init__(self, path: Path, max_size_mb: int = 100) -> None:
        self._path = path
        self._max_size = max_size_mb * 1024 * 1024
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "a", encoding="utf-8")

    def write(self, event: AuditEvent) -> None:
        try:
            self._file.write(event.to_json() + "\n")
            if self._file.tell() > self._max_size:
                self._rotate()
        except Exception as e:
            logger.error("[AuditSink] JSONL write failed: {}", e)

    def flush(self) -> None:
        try:
            self._file.flush()
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._file.close()
        except Exception:
            pass

    def _rotate(self) -> None:
        try:
            self._file.close()
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            rotated = self._path.with_suffix(f".{ts}.jsonl")
            self._path.rename(rotated)
            self._file = open(self._path, "a", encoding="utf-8")
            logger.info("[AuditSink] Rotated audit log to {}", rotated.name)
        except Exception as e:
            logger.error("[AuditSink] Rotation failed: {}", e)


class HttpSink(AuditSink):
    """HTTP POST sink for SIEM / log aggregation services.

    Batches events and flushes periodically or when the batch is full.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        headers: dict[str, str] | None = None,
        batch_size: int = 50,
        flush_interval_s: float = 5.0,
    ) -> None:
        self._endpoint = endpoint
        self._headers = headers or {}
        self._batch_size = batch_size
        self._flush_interval = flush_interval_s
        self._batch: list[AuditEvent] = []
        self._last_flush = time.monotonic()
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            import httpx
            self._client = httpx.Client(timeout=10.0)
        return self._client

    def write(self, event: AuditEvent) -> None:
        self._batch.append(event)
        if len(self._batch) >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        if not self._batch:
            return

        events = list(self._batch)
        self._batch.clear()
        self._last_flush = time.monotonic()

        try:
            payload = json.dumps([e.to_dict() for e in events], ensure_ascii=False, default=str)
            client = self._get_client()
            resp = client.post(self._endpoint, content=payload, headers={**self._headers, "Content-Type": "application/json"})
            if resp.status_code >= 400:
                logger.warning("[AuditSink] HTTP POST returned {}: {}", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.error("[AuditSink] HTTP POST failed: {}", e)

    def close(self) -> None:
        self.flush()
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None


class LogSink(AuditSink):
    """Emit audit events through loguru (for development / testing)."""

    def write(self, event: AuditEvent) -> None:
        logger.info("[Audit] {} {} {} by {} — {}", event.action, event.outcome, event.resource, event.actor, json.dumps(event.details, default=str)[:200])

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class AuditLogger:
    """Central audit logger with multiple output sinks.

    All writes are synchronous and fast (just appending to buffers).
    Call ``flush()`` periodically or on shutdown to ensure delivery.
    """

    def __init__(self, *sinks: AuditSink) -> None:
        self._sinks = list(sinks)

    def add_sink(self, sink: AuditSink) -> None:
        self._sinks.append(sink)

    def log(self, event: AuditEvent) -> None:
        for sink in self._sinks:
            try:
                sink.write(event)
            except Exception as e:
                logger.error("[AuditLogger] Sink write error: {}", e)

    def log_action(
        self,
        action: str,
        actor: str,
        resource: str,
        *,
        outcome: str = "success",
        details: dict[str, Any] | None = None,
        session_key: str = "",
        correlation_id: str = "",
    ) -> None:
        """Convenience method for logging an action."""
        self.log(AuditEvent(
            action=action,
            actor=actor,
            resource=resource,
            outcome=outcome,
            details=details or {},
            session_key=session_key,
            correlation_id=correlation_id,
        ))

    def flush(self) -> None:
        for sink in self._sinks:
            try:
                sink.flush()
            except Exception as e:
                logger.error("[AuditLogger] Sink flush error: {}", e)

    def close(self) -> None:
        self.flush()
        for sink in self._sinks:
            try:
                sink.close()
            except Exception as e:
                logger.error("[AuditLogger] Sink close error: {}", e)


_global_audit: AuditLogger | None = None


def get_audit_logger(*, persist_path: Path | None = None, siem_endpoint: str | None = None) -> AuditLogger:
    """Get the global singleton AuditLogger (lazy-initialized).

    By default creates a LogSink.  Optionally adds a JsonlSink and/or
    an HttpSink for SIEM integration.
    """
    global _global_audit
    if _global_audit is not None:
        return _global_audit

    sinks: list[AuditSink] = [LogSink()]

    if persist_path:
        sinks.append(JsonlSink(persist_path))

    if siem_endpoint:
        sinks.append(HttpSink(siem_endpoint))

    _global_audit = AuditLogger(*sinks)
    return _global_audit


def set_audit_logger(audit: AuditLogger) -> None:
    """Override the global AuditLogger."""
    global _global_audit
    _global_audit = audit


def reset_audit_logger() -> None:
    """Reset the global AuditLogger (useful for testing)."""
    global _global_audit
    if _global_audit is not None:
        _global_audit.close()
    _global_audit = None
