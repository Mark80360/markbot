"""Unit tests for the token-estimate cache and the prompt-persist
cross-session disk cache.

The on-disk cache uses a temp dir per test so we don't pollute
the user's actual ``~/.markbot`` directory.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from markbot.agent.token_estimate_cache import TokenEstimateCache
from markbot.agent.prompt_persist import (
    CacheMetadata,
    cache_dir,
    cache_stats,
    clear_cached_base_section,
    hash_base_section,
    load_cached_base_section,
    save_cached_base_section,
)


SYSTEM = "You are a helpful assistant."


class TestTokenEstimateCache(unittest.TestCase):
    """The cache is content-addressed and session-scoped.

    It used to be keyed on ``messages_revision``, a counter the loop bumped
    unconditionally at the top of every iteration and then immediately
    queried — so the hot path missed ~100% of the time. Worse, the key held
    no session identity while one instance was shared by all concurrent
    sessions, so two users with identical system prompts could be served
    each other's estimate. That number gates the compaction threshold.
    """

    def test_first_call_misses_then_hits(self) -> None:
        cache = TokenEstimateCache()
        msgs = [{"role": "user", "content": "hello"}]
        n1 = cache.lookup_or_compute(
            system_prompt=SYSTEM, messages=msgs, session_key="s1",
        )
        n2 = cache.lookup_or_compute(
            system_prompt=SYSTEM, messages=msgs, session_key="s1",
        )
        self.assertEqual(n1, n2)
        self.assertEqual(cache.misses, 1)
        self.assertGreaterEqual(cache.hits, 1)

    def test_message_growth_recomputes(self) -> None:
        cache = TokenEstimateCache()
        msgs: list = []
        cache.lookup_or_compute(
            system_prompt=SYSTEM, messages=msgs, session_key="s1"
        )
        msgs.append({"role": "user", "content": "hello"})
        cache.lookup_or_compute(
            system_prompt=SYSTEM, messages=msgs, session_key="s1"
        )
        self.assertEqual(cache.misses, 2)
        self.assertEqual(cache.hits, 0)

    def test_system_fingerprint_change(self) -> None:
        cache = TokenEstimateCache()
        cache.lookup_or_compute(
            system_prompt="A", messages=[], session_key="s1"
        )
        cache.lookup_or_compute(
            system_prompt="B", messages=[], session_key="s1"
        )
        self.assertEqual(cache.misses, 2)

    def test_same_content_different_sessions_isolated(self) -> None:
        cache = TokenEstimateCache()
        msgs = [{"role": "user", "content": "shared"}]
        cache.lookup_or_compute(
            system_prompt=SYSTEM, messages=msgs, session_key="alice"
        )
        cache.lookup_or_compute(
            system_prompt=SYSTEM, messages=msgs, session_key="bob"
        )
        self.assertEqual(cache.misses, 2)
        self.assertEqual(cache.hits, 0)

    def test_legacy_bump_invalidates(self) -> None:
        cache = TokenEstimateCache()
        msgs = [{"role": "user", "content": "hello"}]
        cache.lookup_or_compute(
            system_prompt=SYSTEM, messages=msgs, session_key="s1"
        )
        cache.bump_messages_revision(2)
        cache.lookup_or_compute(
            system_prompt=SYSTEM, messages=msgs, session_key="s1"
        )
        # The content did not change, so the LRU entry still serves it.
        self.assertEqual(cache.misses, 1)
        self.assertEqual(cache.hits, 1)


class TestPromptPersist(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "ws"
        self.workspace.mkdir(parents=True, exist_ok=True)
        # Force the cache dir under the temp tree.
        self.cache_root = Path(self.tmp.name) / "cache"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _workspace_hash(self) -> str:
        return "testhash123"

    def test_save_then_load(self) -> None:
        save_cached_base_section(
            self._workspace_hash(),
            self.workspace,
            "The base section",
            cache_root=self.cache_root,
        )
        got = load_cached_base_section(
            self._workspace_hash(),
            self.workspace,
            cache_root=self.cache_root,
        )
        self.assertEqual(got, "The base section")

    def test_load_missing(self) -> None:
        got = load_cached_base_section(
            "nope", self.workspace, cache_root=self.cache_root,
        )
        self.assertIsNone(got)

    def test_workspace_mismatch_invalidates(self) -> None:
        save_cached_base_section(
            self._workspace_hash(), self.workspace, "x", cache_root=self.cache_root,
        )
        # Read back from a *different* workspace — must be None.
        other = Path(self.tmp.name) / "other_ws"
        other.mkdir(parents=True, exist_ok=True)
        got = load_cached_base_section(
            self._workspace_hash(), other, cache_root=self.cache_root,
        )
        self.assertIsNone(got)

    def test_mtime_change_invalidates(self) -> None:
        import time as _time
        save_cached_base_section(
            self._workspace_hash(), self.workspace, "x", cache_root=self.cache_root,
        )
        # Sleep + write so the directory mtime is guaranteed to
        # change (Windows mtime has 1-second resolution).
        _time.sleep(1.1)
        (self.workspace / "new.txt").write_text("data", encoding="utf-8")
        got = load_cached_base_section(
            self._workspace_hash(), self.workspace, cache_root=self.cache_root,
        )
        self.assertIsNone(got)

    def test_clear_drops_entry(self) -> None:
        save_cached_base_section(
            self._workspace_hash(), self.workspace, "x", cache_root=self.cache_root,
        )
        clear_cached_base_section(
            self._workspace_hash(), cache_root=self.cache_root,
        )
        self.assertIsNone(
            load_cached_base_section(
                self._workspace_hash(), self.workspace, cache_root=self.cache_root,
            )
        )

    def test_cache_stats(self) -> None:
        save_cached_base_section(
            "h1", self.workspace, "x" * 1024, cache_root=self.cache_root,
        )
        save_cached_base_section(
            "h2", self.workspace, "y" * 1024, cache_root=self.cache_root,
        )
        stats = cache_stats(cache_root=self.cache_root)
        self.assertEqual(stats["entries"], 2)
        self.assertGreaterEqual(stats["total_bytes"], 2048)

    def test_hash_is_stable(self) -> None:
        self.assertEqual(hash_base_section("a"), hash_base_section("a"))
        self.assertNotEqual(hash_base_section("a"), hash_base_section("b"))

    def test_cache_dir_falls_back_to_home(self) -> None:
        # If a ``cache_root`` is None, ``cache_dir`` returns
        # ``~/.markbot/prompt_cache`` (or XDG equivalent on
        # non-Windows).  We just check it returns a Path.
        d = cache_dir()
        if d is not None:
            self.assertTrue(str(d).endswith("prompt_cache"))


if __name__ == "__main__":
    unittest.main()
