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
CACHE_DISCIPLINE_SECTION = """# Cache discipline (always enforced)

The system prompt above is layered most-static-first so the
server-side KV prefix cache can hit turn-over-turn.  Cached input
tokens are billed at roughly 1/10 of the miss rate.  The model is
asked to follow these rules so the cache stays warm:

1. **Append, don't reorder.**  When adding to the message history,
   push the new turn onto the end.  Never insert, remove, or
   re-order earlier turns.  Reordering busts the prefix for every
   token that follows.
2. **Don't paraphrase quoted content.**  If the user pastes an
   error message, a stack trace, or any other verbatim snippet,
   reproduce it byte-for-byte when re-quoting it.
3. **Use `/compact` as a hard reset, not a tweak.**  Compaction
   intentionally rewrites the prefix to a shorter summary.  Do
   *not* trigger it for small wins — only when the cache is
   already losing.
4. **Read once, refer back.**  Cite content from earlier turns
   verbatim instead of restating it in your own words.  A new
   paraphrase is a new cache miss.
5. **Heed the `Cache: NN%` chip.**  When the chip turns red, the
   operator will see the system prompt churn — investigate before
   the next compact.

> The system prompt is the longest byte-stable prefix in this
> conversation.  Everything below this line is volatile by design;
> everything above this line is stable by design.  Treat them
> accordingly.
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
