"""Turn trace recording.

Why
---
The agent had logs but no answer to the two questions that matter when it
does something wrong: *what did it actually see* and *why did it take that
branch*. There was no way to inspect a finished turn — only the live log
stream, which is gone by the time you look.

This module records a bounded, structured trace per turn:

    trace.record("llm_call", model=..., input_tokens=..., cache_read=...)
    ...
    trace.summary()   # -> dict, safe to log or expose over the web API

Design constraints
------------------
* **Bounded.** A turn is capped at ``max_events``; older events are dropped
  and counted, so a runaway loop cannot exhaust memory. The dropped count is
  itself reported, so a truncated trace is never silently mistaken for a
  complete one.
* **Redacted by construction.** Payloads pass through the log redactor before
  being stored. A trace is retained in memory and surfaced in diagnostics,
  which makes it a *wider* exposure surface than a log line that only ever
  reaches an operator's terminal — so secrets must not survive into it.
* **No silent failures.** Recording must never break a turn, so every entry
  point swallows and counts its own errors instead of propagating.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TraceEvent:
    """One recorded step within a turn."""

    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "at": round(self.at, 3),
            **self.data,
        }


class TurnTrace:
    """A single turn's decision record.

    Lifecycle::

        trace = TurnTrace(session_key="feishu:u1", channel="feishu")
        trace.record("turn_start", user_chars=len(content))
        ...
        summary = trace.summary()
    """

    #: Default cap. A 40-iteration turn with several events per iteration
    #: lands around 150 events, so 512 keeps normal turns complete while
    #: still bounding a pathological one.
    DEFAULT_MAX_EVENTS = 512

    #: Truncation markers for long strings. Enough to identify the value,
    #: short enough not to bloat the trace.
    _MAX_STR = 200

    def __init__(
        self,
        *,
        session_key: str = "",
        channel: str = "",
        chat_id: str = "",
        max_events: int | None = None,
    ) -> None:
        self.session_key = session_key
        self.channel = channel
        self.chat_id = chat_id
        self.max_events = max_events or self.DEFAULT_MAX_EVENTS
        self._events: deque[TraceEvent] = deque(maxlen=self.max_events)
        self._dropped = 0
        self._errors = 0
        self.started_at = time.time()
        self.ended_at: float | None = None

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(self, kind: str, **data: Any) -> None:
        """Record one step. Never raises."""
        try:
            event = TraceEvent(
                kind=kind,
                data=self._clean(data),
                at=time.time(),
            )
            if len(self._events) == self.max_events:
                self._dropped += 1
            self._events.append(event)
        except Exception:
            # A telemetry bug must never take down a turn.
            self._errors += 1

    def record_tool(
        self, name: str, *, ok: bool, duration_s: float | None = None, **extra: Any
    ) -> None:
        """Record a tool outcome in a uniform shape."""
        self.record("tool", tool=name, ok=ok, duration_s=duration_s, **extra)

    def record_llm(
        self,
        *,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read: int = 0,
        cache_creation: int = 0,
        cost_usd: float | None = None,
        finish_reason: str = "",
    ) -> None:
        """Record one model call and its cache economics."""
        self.record(
            "llm",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read=cache_read,
            cache_creation=cache_creation,
            cost_usd=cost_usd,
            finish_reason=finish_reason,
        )

    def finish(self, *, exit_reason: str = "", error: str = "") -> None:
        """Close the turn. Idempotent."""
        if self.ended_at is None:
            self.ended_at = time.time()
        if exit_reason:
            self.record("turn_end", exit_reason=exit_reason, error=error or None)

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    @property
    def events(self) -> list[TraceEvent]:
        return list(self._events)

    @property
    def duration_s(self) -> float:
        return (self.ended_at or time.time()) - self.started_at

    def of_kind(self, kind: str) -> list[TraceEvent]:
        return [e for e in self._events if e.kind == kind]

    def summary(self) -> dict[str, Any]:
        """A compact, log-safe digest of the turn."""
        tools = self.of_kind("tool")
        llms = self.of_kind("llm")
        return {
            "session_key": self.session_key,
            "channel": self.channel,
            "duration_s": round(self.duration_s, 3),
            "events": len(self._events),
            # Non-zero means the trace is NOT the whole story. Callers must
            # surface this rather than presenting a partial trace as complete.
            "dropped_events": self._dropped,
            "record_errors": self._errors,
            "tools": {
                "count": len(tools),
                "failed": sum(1 for e in tools if not e.data.get("ok", True)),
                "names": [e.data.get("tool") for e in tools],
            },
            "llm": {
                "calls": len(llms),
                "input_tokens": sum(e.data.get("input_tokens", 0) or 0 for e in llms),
                "output_tokens": sum(e.data.get("output_tokens", 0) or 0 for e in llms),
                "cache_read": sum(e.data.get("cache_read", 0) or 0 for e in llms),
                "cost_usd": round(
                    sum(e.data.get("cost_usd") or 0.0 for e in llms), 6
                ),
                "models": sorted({e.data.get("model", "") for e in llms if e.data.get("model")}),
            },
            "truncated": self._dropped > 0,
        }

    def to_dict(self, *, include_events: bool = True) -> dict[str, Any]:
        out = self.summary()
        if include_events:
            out["timeline"] = [e.as_dict() for e in self._events]
        return out

    def render(self) -> str:
        """A human-readable one-line-per-step trace for logs and the TUI."""
        lines = [
            f"turn {self.session_key or '-'} "
            f"[{self.channel or '-'}] {len(self._events)} events "
            f"in {self.duration_s:.2f}s"
            + (f" (+{self._dropped} dropped)" if self._dropped else "")
        ]
        for e in self._events:
            detail = " ".join(
                f"{k}={v}" for k, v in e.data.items() if v is not None
            )
            lines.append(f"  {e.at - self.started_at:7.3f}s {e.kind:10} {detail}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Hygiene
    # ------------------------------------------------------------------

    def _clean(self, data: dict[str, Any]) -> dict[str, Any]:
        """Redact secrets and bound value sizes before storage.

        A trace is kept in memory and can be surfaced through diagnostics
        endpoints, so it must not be a place where an API key outlives the
        turn.
        """
        from markbot.log.redact import redact_sensitive

        out: dict[str, Any] = {}
        for key, value in data.items():
            out[key] = self._scrub(key, value, redact_sensitive)
        return out

    def _scrub(self, key: str, value: Any, redact) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            text = redact(value)
            if len(text) > self._MAX_STR:
                return text[: self._MAX_STR] + f"...(+{len(text) - self._MAX_STR})"
            return text
        if isinstance(value, (list, tuple)):
            # Bounded: a tool returning a 10k-element list must not land here.
            items = [self._scrub(key, v, redact) for v in list(value)[:20]]
            if len(value) > 20:
                items.append(f"...(+{len(value) - 20} more)")
            return items
        if isinstance(value, dict):
            return {k: self._scrub(k, v, redact) for k, v in list(value.items())[:20]}
        # Never call repr() on a caller's object: a broken __repr__ would
        # raise inside telemetry and silently drop the whole event. Type name
        # is enough to tell the reader what they are looking at.
        return f"<{type(value).__name__}>"


__all__ = ["TurnTrace", "TraceEvent"]
