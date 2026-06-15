"""Sanitisation of tool outputs before they enter the LLM context.

Tool execution results (shell stdout, ``run_code`` output, file reads) can
contain byte sequences that, when re-injected into the conversation as a
``tool`` message, break downstream consumers:

1. **Fence injection** — a program that prints four or more backticks
   (```` ```` ````) can prematurely close the markdown code fence that
   *other* layers (logs, compaction summaries, memory writers, channel
   formatters) use to delimit tool output, causing the model to confuse
   "echoed output" with "a code block I should parse". This mirrors the
   attack GenericAgent defends against by inserting a zero-width space
   (U+200B) after the third backtick.

2. **Context-fence spoofing** — markbot uses ``<memory-context>`` tags
   (see :mod:`markbot.memory.fencing`) to wrap recalled memory so the
   streaming scrubber can strip it from user-facing output. A tool whose
   output contains a literal ``</memory-context>`` could trick the
   scrubber into dropping subsequent assistant text, or make the model
   treat tool output as privileged memory context.

This module centralises the single choke-point transform applied at
``ContextBuilder.add_tool_result`` so *every* tool result is normalised
exactly once, regardless of which tool produced it. The transform is
pure, side-effect free and idempotent.

Only string content is touched — multimodal content blocks (image /
text arrays) pass through unchanged because their structure is already
provider-sanitised.
"""

from __future__ import annotations

import re

# Zero-width space inserted into long backtick runs to break the fence
# without changing visible output. One ZWSP after the 3rd backtick is
# enough to stop ```` ```` ```` from being interpreted as a 4-tick fence.
_ZWSP = "\u200b"

# Match 4+ consecutive backticks. We keep the first three intact and
# re-emit the remainder with a ZWSP separator so the run can no longer
# form a single fence token of length >= 4.
_LONG_FENCE_RE = re.compile(r"`{4,}")


def _break_fence(match: re.Match[str]) -> str:
    run = match.group(0)
    return run[:3] + _ZWSP + run[3:]


# Literal fence tags that markbot recognises. We escape them so a tool
# cannot forge a memory-context span. Escaping is reversible-free: the
# leading backslash makes the tag a visible literal rather than a fence,
# and downstream markdown renderers show it as-is. We deliberately do
# NOT strip the tags — that would silently hide content the model may
# need to see (e.g. a program that genuinely prints XML). Escaping
# preserves the information while neutralising the structural threat.
_SPOOF_TAGS = [
    "<memory-context>",
    "</memory-context>",
]


def sanitize_tool_output(text: str) -> str:
    """Return *text* with fence-injection and tag-spoofing neutralised.

    Transforms (in order):
      1. Insert U+200B after the 3rd backtick of any 4+ backtick run.
      2. Prefix literal ``<memory-context>`` / ``</memory-context>`` with
         a backslash so they are no longer parsed as fence tags.

    The function is a no-op for safe text and idempotent: applying it
    twice yields the same result as applying it once (a 3-tick run is
    untouched, so the ZWSP is never doubled; an already-escaped tag has
    no leading ``<`` to match).
    """
    if not isinstance(text, str) or not text:
        return text

    # 1. Break long backtick fences.
    out = _LONG_FENCE_RE.sub(_break_fence, text)

    # 2. Neutralise fence-tag spoofing. Escape the opening angle so the
    #    tag renders as literal text instead of being parsed as a fence.
    for tag in _SPOOF_TAGS:
        # Idempotent guard: don't double-escape an already-escaped tag.
        escaped = "\\" + tag
        out = out.replace(escaped, "\x00__ESC__\x00")
        out = out.replace(tag, escaped)
        out = out.replace("\x00__ESC__\x00", escaped)

    return out


__all__ = ["sanitize_tool_output"]
