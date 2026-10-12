"""Tests for the token-estimate cache.

The original cache keyed on ``(messages_revision, system_fingerprint)`` and
relied on the loop bumping the revision when messages changed. The loop
bumped it unconditionally at the top of every iteration and then queried
immediately, so the hot path missed ~100% of the time. Worse, one instance
is shared by all concurrent sessions and the key carried no session
identity, so two users with byte-identical system prompts could be served
each other's estimate — and that number gates the compaction threshold.
"""

from __future__ import annotations

from markbot.agent.token_estimate_cache import (
    DEFAULT_AUDIT_RING_CAPACITY,
    TokenEstimateCache,
    _system_fingerprint,
)

SYS = "You are a helpful assistant. " * 40


def _msgs(n: int = 1) -> list[dict]:
    return [{"role": "user", "content": "hello"} for _ in range(n)]


class TestBasicCaching:
    def test_identical_call_hits(self):
        c = TokenEstimateCache()
        m = _msgs()
        a = c.lookup_or_compute(SYS, m, session_key="s1")
        b = c.lookup_or_compute(SYS, m, session_key="s1")
        assert a == b
        assert c.stats["hits"] == 1
        assert c.stats["misses"] == 1

    def test_growth_misses(self):
        c = TokenEstimateCache()
        m = _msgs()
        c.lookup_or_compute(SYS, m, session_key="s1")
        m.append({"role": "assistant", "content": "x" * 500})
        c.lookup_or_compute(SYS, m, session_key="s1")
        assert c.stats["misses"] == 2

    def test_shrinking_misses(self):
        c = TokenEstimateCache()
        m = _msgs(5)
        c.lookup_or_compute(SYS, m, session_key="s1")
        m.pop()
        c.lookup_or_compute(SYS, m, session_key="s1")
        assert c.stats["misses"] == 2

    def test_system_prompt_change_misses(self):
        c = TokenEstimateCache()
        m = _msgs()
        c.lookup_or_compute(SYS, m, session_key="s1")
        c.lookup_or_compute(SYS + " extra", m, session_key="s1")
        assert c.stats["misses"] == 2

    def test_tail_mutation_misses(self):
        """Same length, different tail content (in-place edit of the last
        message) must not serve a stale estimate."""
        c = TokenEstimateCache()
        m = _msgs(2)
        c.lookup_or_compute(SYS, m, session_key="s1")
        m[-1]["content"] = "a much longer replacement " * 50
        c.lookup_or_compute(SYS, m, session_key="s1")
        assert c.stats["misses"] == 2

    def test_wholesale_replacement_misses(self):
        """Compaction swaps in a brand-new list object."""
        c = TokenEstimateCache()
        c.lookup_or_compute(SYS, _msgs(10), session_key="s1")
        c.lookup_or_compute(SYS, _msgs(3), session_key="s1")
        assert c.stats["misses"] == 2


class TestSessionIsolation:
    def test_same_content_different_sessions_are_separate_entries(self):
        c = TokenEstimateCache()
        m = _msgs()
        c.lookup_or_compute(SYS, m, session_key="s1")
        c.lookup_or_compute(SYS, m, session_key="s2")
        assert c.stats["misses"] == 2
        assert c.stats["hits"] == 0

    def test_repeat_lookup_in_own_session_hits(self):
        c = TokenEstimateCache()
        m = _msgs()
        c.lookup_or_compute(SYS, m, session_key="s1")
        c.lookup_or_compute(SYS, m, session_key="s2")
        c.lookup_or_compute(SYS, m, session_key="s1")
        assert c.stats["hits"] == 1

    def test_invalidate_session_drops_only_that_session(self):
        c = TokenEstimateCache()
        c.lookup_or_compute(SYS, _msgs(), session_key="s1")
        c.lookup_or_compute(SYS, _msgs(), session_key="s2")
        c.invalidate_session("s1")
        c.lookup_or_compute(SYS, _msgs(), session_key="s2")
        assert c.stats["hits"] == 1


class TestRealisticHitRate:
    def test_turn_loop_actually_hits(self):
        """Regression for the ~0% hit rate: a turn estimates twice per
        iteration, and the second call must hit."""
        c = TokenEstimateCache()
        msgs: list[dict] = [
            {"role": "system", "content": SYS},
            {"role": "user", "content": "do the thing"},
        ]
        for _ in range(6):
            c.lookup_or_compute(SYS, msgs, session_key="s1")
            c.lookup_or_compute(SYS, msgs, session_key="s1")
            msgs.append({"role": "assistant", "content": "result " * 50})
        assert c.stats["hit_rate"] > 0.3, c.stats

    def test_two_sessions_interleaved_still_hit(self):
        c = TokenEstimateCache()
        a: list[dict] = [{"role": "system", "content": SYS}]
        b: list[dict] = [{"role": "system", "content": SYS}]
        for i in range(5):
            for msgs, key in ((a, "u1"), (b, "u2")):
                c.lookup_or_compute(SYS, msgs, session_key=key)
                c.lookup_or_compute(SYS, msgs, session_key=key)
                msgs.append({"role": "user", "content": f"m{i}"})
        assert c.stats["hit_rate"] > 0.3, c.stats


class TestBounding:
    def test_lru_is_bounded(self):
        c = TokenEstimateCache(max_entries=4)
        for n in range(20):
            c.lookup_or_compute(SYS, _msgs(n + 1), session_key="s1")
        assert c.stats["size"] <= 4

    def test_audit_ring_bounded(self):
        c = TokenEstimateCache()
        for n in range(DEFAULT_AUDIT_RING_CAPACITY * 3):
            c.lookup_or_compute(SYS, _msgs(n + 1), session_key="s1")
        assert len(c.audit_ring) <= DEFAULT_AUDIT_RING_CAPACITY

    def test_invalidate_clears(self):
        c = TokenEstimateCache()
        c.lookup_or_compute(SYS, _msgs(), session_key="s1")
        c.invalidate()
        assert c.stats["size"] == 0
        assert c.stats["hits"] == 0


class TestFingerprint:
    def test_none_is_zero(self):
        assert _system_fingerprint(None) == 0

    def test_str_and_blocks_agree(self):
        text = "hello world"
        blocks = [{"type": "text", "text": "hello world"}]
        assert _system_fingerprint(text) == _system_fingerprint(blocks)

    def test_different_text_differs(self):
        assert _system_fingerprint("a") != _system_fingerprint("b")


class TestLoopIntegration:
    def test_estimate_tokens_passes_session_key(self):
        """The runner must supply the session key, otherwise isolation is
        meaningless."""
        import inspect

        from markbot.agent.iteration import IterationRunner

        src = inspect.getsource(IterationRunner._estimate_tokens)
        assert "session_key" in src
        assert "messages_revision" not in src

    def test_loop_no_longer_bumps_revision_per_iteration(self):
        import inspect

        from markbot.agent.iteration import IterationRunner

        src = inspect.getsource(IterationRunner._run_one_iteration)
        assert "_messages_revision" not in src
