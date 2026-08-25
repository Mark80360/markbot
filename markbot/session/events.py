"""Typed session events — the append-only log as source of truth.

Contract ("model-visible ⟺ logged"): every message the model sees is
reconstructable from the event log via :func:`derive_messages`. New
model-visible input kinds require a new event type — never a silent
mutation of derived state.

Envelope shape (one JSONL line)::

    {"_type": "event", "seq": N, "ts": "...", "event": "<type>",
     "ignorable": bool, "payload": {...}}

``seq`` is contiguous per session. ``ignorable: true`` marks control
scaffolding (verify nudges, todo reinjection) that old readers may skip
— the analogue of dsh's per-event forward-compat gate, so adding event
types does not bump the session format version.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# Event vocabulary. Model-visible types project into provider messages;
# everything else is metadata/audit or a projection boundary.
EVENT_USER_MESSAGE = "user_message"
EVENT_ASSISTANT_MESSAGE = "assistant_message"
EVENT_TOOL_RESULT = "tool_result"
EVENT_HISTORY_RESET = "history_reset"
EVENT_HISTORY_RETAINED = "history_retained"
EVENT_COMPACTION_APPLIED = "compaction_applied"
EVENT_APPROVAL_ASKED = "approval_asked"
EVENT_APPROVAL_DECIDED = "approval_decided"
EVENT_CONTROL_NOTE = "control_note"

#: Events that project into provider-format messages, mapped to their role.
_MODEL_VISIBLE_ROLES: dict[str, str] = {
    EVENT_USER_MESSAGE: "user",
    EVENT_ASSISTANT_MESSAGE: "assistant",
    EVENT_TOOL_RESULT: "tool",
}

#: Boundary events consumed by the projection itself (never emitted as
#: messages).
_BOUNDARY_EVENTS = frozenset({EVENT_HISTORY_RESET, EVENT_HISTORY_RETAINED})

#: Message dict keys copied verbatim into payloads / projections.
_MESSAGE_KEYS = ("content", "tool_calls", "tool_call_id", "name")


def make_event(
    seq: int,
    event: str,
    payload: dict[str, Any] | None = None,
    *,
    ignorable: bool = False,
    ts: str | None = None,
) -> dict[str, Any]:
    """Build one event envelope."""
    return {
        "_type": "event",
        "seq": seq,
        "ts": ts or datetime.now().isoformat(),
        "event": event,
        "ignorable": ignorable,
        "payload": payload or {},
    }


def event_for_message(message: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Map a provider-format message to ``(event_type, payload)``.

    Unknown roles fall back to ``control_note`` (ignorable) rather than
    being dropped — the log stays complete even for unexpected shapes.
    """
    role = message.get("role", "")
    mapping = {
        "user": EVENT_USER_MESSAGE,
        "assistant": EVENT_ASSISTANT_MESSAGE,
        "tool": EVENT_TOOL_RESULT,
    }
    event_type = mapping.get(role)
    if event_type is None:
        return EVENT_CONTROL_NOTE, {"message_role": role}
    payload = {key: message[key] for key in _MESSAGE_KEYS if key in message}
    if not payload:
        payload = {"content": message.get("content", "")}
    return event_type, payload


def message_from_event(envelope: dict[str, Any]) -> dict[str, Any] | None:
    """Project one model-visible event envelope back to a provider message."""
    event_type = envelope.get("event")
    role = _MODEL_VISIBLE_ROLES.get(event_type)
    if role is None:
        return None
    payload = envelope.get("payload") or {}
    message: dict[str, Any] = {"role": role, "content": payload.get("content", "")}
    for key in _MESSAGE_KEYS[1:]:
        if key in payload:
            message[key] = payload[key]
    if envelope.get("ts"):
        message["timestamp"] = envelope["ts"]
    return message


def derive_messages(
    events: list[dict[str, Any]],
    *,
    upto_seq: int | None = None,
) -> list[dict[str, Any]]:
    """Project model-visible history from the event log.

    Boundary events shape the projection so it stays faithful to what the
    model actually sees:

    * ``history_reset`` truncates the projection to empty at that point;
    * ``history_retained {retained_count}`` keeps only the trailing
      ``retained_count`` projected messages (compaction boundary).

    Raw events remain in the log regardless — the reset/retain envelopes
    bound the *projection*, never erase the audit trail.

    Args:
        events: Event envelopes in seq order (may include non-model events).
        upto_seq: Exclusive upper bound; None means all events.

    Returns provider-format messages equivalent to the live ``messages``
    list — this is the function every consumer (resume, fork, replay,
    transcript export, crash recovery) should read from instead of
    touching derived state.
    """
    out: list[dict[str, Any]] = []
    for envelope in events:
        seq = envelope.get("seq")
        if upto_seq is not None and isinstance(seq, int) and seq >= upto_seq:
            break
        event_type = envelope.get("event")
        if event_type == EVENT_HISTORY_RESET:
            out.clear()
            continue
        if event_type == EVENT_HISTORY_RETAINED:
            payload = envelope.get("payload") or {}
            try:
                retained_count = max(0, int(payload.get("retained_count", 0)))
            except (TypeError, ValueError):
                retained_count = 0
            if retained_count == 0:
                out.clear()
            elif retained_count < len(out):
                del out[:-retained_count]
            continue
        message = message_from_event(envelope)
        if message is not None:
            out.append(message)
    return out


def next_seq(events: list[dict[str, Any]]) -> int:
    """Next contiguous sequence number for *events*."""
    if not events:
        return 0
    last = events[-1].get("seq")
    return (last + 1) if isinstance(last, int) else len(events)


__all__ = [
    "EVENT_APPROVAL_ASKED",
    "EVENT_APPROVAL_DECIDED",
    "EVENT_ASSISTANT_MESSAGE",
    "EVENT_COMPACTION_APPLIED",
    "EVENT_CONTROL_NOTE",
    "EVENT_HISTORY_RESET",
    "EVENT_HISTORY_RETAINED",
    "EVENT_TOOL_RESULT",
    "EVENT_USER_MESSAGE",
    "derive_messages",
    "event_for_message",
    "make_event",
    "message_from_event",
    "next_seq",
]
