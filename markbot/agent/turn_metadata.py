"""Per-turn metadata that lives at the **tail** of the user message.

The single most important property of a server-side prefix cache is
that it matches the **leading** bytes of a request.  Anything that
goes into the head of the system prompt (date, working-set diff,
auth / rate-limit stats) busts the cache every single time it
changes.

The fix, lifted from CodeWhale's
``runtime_prompt_message()`` /
``user_text_message_with_turn_metadata()``:

- **System prompt**: keep it as static as possible.  Drift-bait goes
  here only if it cannot live anywhere else.
- **User message text**: real user input first.
- **Per-turn metadata** (date, working-set, model route, cache
  counters, /status hint, etc.): appended **after** the user text
  inside the same user message — never at message position 0.

When a new turn starts, the user message is re-emitted with fresh
``turn_meta`` but the prefix (the real user input) is byte-identical
to the previous turn's prefix, so DeepSeek / Anthropic / Codex can
reuse the cached KV states for everything up to and including the
user's actual question.

## Format

The metadata is wrapped in a single block so it can be stripped on
replay / persistence and so the model sees an unambiguous marker::

    <turn_meta>{"date":"2026-06-12", "model":"...", "ts": ...}</turn_meta>

The tag is custom but stable; the model is told (in
:mod:`markbot.agent.context`) that the block is "metadata only, not
instructions".
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


_TURN_META_TAG = "<turn_meta>"
_TURN_META_CLOSE = "</turn_meta>"


@dataclass
class TurnMetadata:
    """Per-turn metadata that the agent injects at the user-message tail.

    Keep this dataclass **small and stable** — every field added here
    either costs us a few bytes of cache drift, or saves us a few
    bytes elsewhere.  When in doubt, do not add a field; the model
    can derive it from the system prompt / tool calls.
    """

    #: ISO-8601 local date (no timezone — keeps the string shorter).
    date: str
    #: Wall-clock unix timestamp (seconds).
    ts: float
    #: The model the LLM call is being routed to (so the model can
    #: see its own routing in a single read).
    model: str = ""
    #: Optional reasoning effort / thinking level.
    reasoning_effort: Optional[str] = None
    #: Optional working-set summary (a short string, NOT a full diff).
    working_set: str = ""
    #: Cache counters from the previous turn, if any.  Used to feed
    #: the model a "you are being cache-friendly" signal.
    prev_cache_hit_rate: Optional[float] = None
    #: Stability of the prefix over the last N turns, 0.0..1.0.
    prev_prefix_stability: float = 1.0
    #: Free-form key/value bag for plugin / hook injection.
    extras: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        d = asdict(self)
        # Strip None / empty values to keep the on-wire string short.
        compact = {k: v for k, v in d.items() if v not in (None, "", 0, 1.0, {})}
        return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


def render_turn_meta_block(meta: TurnMetadata) -> str:
    """Return the ``<turn_meta>...</turn_meta>`` block as a string."""
    return f"{_TURN_META_TAG}{meta.to_json()}{_TURN_META_CLOSE}"


def strip_turn_meta_block(text: str) -> tuple[str, Optional[str]]:
    """Strip the trailing ``<turn_meta>`` block from a user message.

    Returns ``(cleaned_text, meta_json_or_None)``.  The block, if
    present, is removed from the *end* of the string; the leading
    user text is preserved byte-for-byte.  This is what makes the
    prefix cache hit.
    """
    if not text:
        return text, None
    idx = text.rfind(_TURN_META_TAG)
    if idx < 0:
        return text, None
    close = text.find(_TURN_META_CLOSE, idx)
    if close < 0:
        return text, None
    cleaned = text[:idx].rstrip()
    meta_json = text[idx + len(_TURN_META_TAG):close]
    return cleaned, meta_json


def attach_turn_meta(
    user_content: str | list[Any],
    meta: TurnMetadata,
) -> str | list[Any]:
    """Append a ``<turn_meta>`` block to a user message.

    If ``user_content`` is a string, returns a new string with the
    block appended (preserves leading user input exactly).

    If ``user_content`` is a list of content blocks (multimodal),
    appends a final text block carrying the metadata.  This keeps the
    leading user text and image blocks untouched.
    """
    block = render_turn_meta_block(meta)
    if isinstance(user_content, str):
        # If the user content already ends in our block, replace it.
        cleaned, _ = strip_turn_meta_block(user_content)
        return f"{cleaned}\n\n{block}" if cleaned else block
    if isinstance(user_content, list):
        # Strip the trailing meta text block if it's already there.
        new_blocks: list[Any] = []
        for b in user_content:
            if (
                isinstance(b, dict)
                and b.get("type") == "text"
                and isinstance(b.get("text"), str)
                and _TURN_META_TAG in b["text"]
                and _TURN_META_CLOSE in b["text"]
            ):
                continue
            new_blocks.append(b)
        new_blocks.append({"type": "text", "text": block})
        return new_blocks
    return user_content


# ---------------------------------------------------------------------------
# Convenience constructor
# ---------------------------------------------------------------------------

def make_turn_metadata(
    *,
    model: str = "",
    reasoning_effort: Optional[str] = None,
    working_set: str = "",
    prev_cache_hit_rate: Optional[float] = None,
    prev_prefix_stability: float = 1.0,
    now: Optional[float] = None,
    date: Optional[str] = None,
    extras: Optional[dict[str, Any]] = None,
) -> TurnMetadata:
    """Construct a :class:`TurnMetadata` with sensible defaults.

    ``date`` defaults to today's local date; ``ts`` defaults to
    ``time.time()``; both can be overridden (useful for tests).
    """
    moment = now if now is not None else time.time()
    if date is None:
        date = time.strftime("%Y-%m-%d", time.localtime(moment))
    return TurnMetadata(
        date=date,
        ts=moment,
        model=model,
        reasoning_effort=reasoning_effort,
        working_set=working_set,
        prev_cache_hit_rate=prev_cache_hit_rate,
        prev_prefix_stability=prev_prefix_stability,
        extras=extras or {},
    )


__all__ = [
    "TurnMetadata",
    "make_turn_metadata",
    "attach_turn_meta",
    "strip_turn_meta_block",
    "render_turn_meta_block",
]
