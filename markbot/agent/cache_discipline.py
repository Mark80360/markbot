"""Cache-discipline rules that are part of the **system prompt**.

Lifted directly from CodeWhale's ``prompts.rs`` cache rules and
adapted to MarkBot's house style.  These rules are deliberately
written to the *model itself* so the agent internalises them
without per-prompt enforcement:

> *"DeepSeek's KV prefix cache matches the leading bytes of a
> request and bills cached tokens at roughly 1/10 the cost of
> miss tokens.  The system prompt above is layered most-static-first
> specifically so the prefix stays stable turn-over-turn."*

## Rules

1. **Append, don't reorder.**  When adding to the message history,
   push the new turn onto the end.  Reordering busts the prefix.
2. **Don't paraphrase quoted content.**  If the user pastes a
   snippet, error message, or stack trace, reproduce it byte-for-byte.
3. **Use ``/compact`` as a hard reset, not a tweak.**  Compaction
   rewrites the prefix to a shorter summary.  It is a forced
   invalidation, not a normal tool.
4. **Read once, refer back.**  Cite content from earlier turns
   verbatim instead of restating it in your own words.
5. **The cache hit chip is the heartbeat.**  If it turns red, the
   operator will see the system prompt churn — investigate before
   the next compact.
"""

from __future__ import annotations


#: Section body.  Keep this **short and stable** — every byte of
#: drift here is a cache miss on the *next* turn.
CACHE_DISCIPLINE_SECTION = """# Cache discipline

This conversation is served from a server-side KV prefix cache: the leading
bytes of each request are matched against the previous one and billed at
roughly a tenth of the miss rate. You keep that cache warm by leaving the
earlier part of the conversation untouched.

1. **Append, don't reorder.** When adding to the message history, push the new
   turn onto the end. Never insert, remove, or re-order earlier turns — every
   token after the change is rebilled at the full rate.
2. **Don't paraphrase quoted content.** If the user pastes an error message, a
   stack trace, or any other verbatim snippet, reproduce it byte-for-byte when
   re-quoting it. A paraphrase is a new string and a miss.
3. **Read once, refer back.** Cite content from earlier turns verbatim instead
   of restating it in your own words.
4. **Do not ask for `/compact` yourself.** Compaction rewrites the prefix down
   to a summary. That is a forced invalidation of the whole cache, not a
   routine tidy-up, and the operator decides when to pay for it. Work within
   the context you already have.

The `Cache: NN%` chip reports to the operator how much of the last request was
served from cache. When it drops, something ahead of this point changed.
"""


#: Comment that gets injected **after** the volatile boundary so an
#: operator reading the source can see the structural intent.
VOLATILE_BOUNDARY_MARKER = (
    "<!-- ====== volatile boundary (below: per-turn, busts cache) "
    "====== above: stable across turns (cache-friendly) ====== -->"
)


__all__ = [
    "CACHE_DISCIPLINE_SECTION",
    "VOLATILE_BOUNDARY_MARKER",
]
