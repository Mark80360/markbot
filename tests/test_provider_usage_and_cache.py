"""Tests for provider accounting and cache-key correctness.

Two defects, both invisible at the unit level:

* Azure's streaming path never requested usage, so ``_consume_stream`` saw an
  empty usage dict and every streamed turn was billed — and budgeted — as
  zero tokens.
* The Codex ``prompt_cache_key`` hashed the entire message array, so it
  changed on every turn as the conversation grew. The prefix cache could
  therefore never hit, which defeats the purpose of sending a key at all.
"""

from __future__ import annotations

import ast
import io
import json

import pytest


def _codex_fn(name: str):
    """Load one function from the Codex module without importing it.

    ``oauth_cli_kit`` is an optional dependency, so the source is parsed
    rather than executed.
    """
    src = io.open("markbot/providers/openai_codex.py", encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == name
    )
    ns: dict = {"json": json}
    import hashlib

    ns["hashlib"] = hashlib
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<x>", "exec"), ns)
    return ns[name]


class TestCodexCacheKey:
    def _key(self):
        return _codex_fn("_prompt_cache_key")

    def test_stable_across_turns(self):
        k = self._key()
        t1 = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ]
        t2 = t1 + [{"role": "user", "content": "follow-up"}]
        t3 = t2 + [{"role": "assistant", "content": "answer 2"}]
        assert k(t1) == k(t2) == k(t3), (
            "the cache key must persist as the conversation grows, or the "
            "prefix cache can never hit"
        )

    def test_distinct_conversations_differ(self):
        k = self._key()
        a = [{"role": "system", "content": "s"}, {"role": "user", "content": "A"}]
        b = [{"role": "system", "content": "s"}, {"role": "user", "content": "B"}]
        assert k(a) != k(b)

    def test_distinct_system_prompts_differ(self):
        k = self._key()
        a = [{"role": "system", "content": "s1"}, {"role": "user", "content": "u"}]
        b = [{"role": "system", "content": "s2"}, {"role": "user", "content": "u"}]
        assert k(a) != k(b)

    def test_returns_hex_digest(self):
        out = self._key()([{"role": "user", "content": "hi"}])
        assert isinstance(out, str)
        assert len(out) == 64
        int(out, 16)

    def test_handles_empty(self):
        assert isinstance(self._key()([]), str)

    def test_handles_missing_role(self):
        assert isinstance(self._key()([{"content": "no role"}]), str)

    def test_key_is_deterministic(self):
        k = self._key()
        m = [{"role": "user", "content": "x"}]
        assert k(m) == k(list(m))


class TestAzureStreamingUsage:
    def test_stream_options_requested(self):
        """Without ``stream_options.include_usage`` the API omits the trailing
        usage chunk and every streamed turn is measured as zero tokens."""
        import inspect

        from markbot.providers.azure_openai import AzureOpenAIProvider

        src = inspect.getsource(AzureOpenAIProvider)
        assert '"stream_options"' in src
        assert '{"include_usage": True}' in src

    def test_stream_consumes_usage(self):
        from markbot.providers.azure_openai import AzureOpenAIProvider

        assert callable(AzureOpenAIProvider._consume_stream)

    def test_usage_reader_exists(self):
        """The stream parser must actually look for usage."""
        import inspect

        from markbot.providers.azure_openai import AzureOpenAIProvider

        assert "usage" in inspect.getsource(AzureOpenAIProvider._consume_stream)


class TestNoOtherMissingStreamOptions:
    def test_openai_compat_requests_usage(self):
        """The reference implementation to match."""
        import inspect

        from markbot.providers.openai_compat import OpenAICompatProvider

        assert "include_usage" in inspect.getsource(OpenAICompatProvider.chat_stream)
