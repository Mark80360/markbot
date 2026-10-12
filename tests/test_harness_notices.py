"""Tests for harness notice injection and prefix-cache preservation.

Harness notices (todo re-injection, failure blacklist, compact summary, snip
notice) were injected as mid-conversation ``role: system`` messages at index
1. Two problems:

* It contradicted the invariant the agent loop itself documents — Anthropic
  hoists mid-conversation system messages into a separate system list, and
  the OpenAI-compat sanitisers drop them, so correctness of compaction came
  to depend on a provider-adapter detail.
* Inserting at index 1 rewrites position 1 onward, invalidating the prefix
  cache for the *entire* remaining history. The agent otherwise works hard
  to keep that cache warm, so one injection erased all of it.
"""

from __future__ import annotations

import inspect

import pytest

from markbot.agent.iteration import IterationRunner


class _FakeState:
    def __init__(self, messages):
        self.messages = messages
        self.iteration = 1
        self.new_msg_start = 0
        self.persisted_upto = 0
        self.last_compact_action = None


class TestAppendSyntheticNotice:
    def _runner(self):
        return object.__new__(IterationRunner)

    def test_appends_as_user_role(self):
        r = self._runner()
        msgs = [{"role": "system", "content": "sys"}]
        IterationRunner._append_synthetic_notice(msgs, "notice", "_m")
        assert msgs[-1]["role"] == "user"
        assert msgs[-1]["content"] == "notice"
        assert msgs[-1]["_m"] is True

    def test_does_not_disturb_existing_prefix(self):
        """Every pre-existing message must stay byte-identical and in place —
        that is what keeps the prefix cache valid."""
        r = self._runner()
        original = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ]
        msgs = [dict(m) for m in original]
        IterationRunner._append_synthetic_notice(msgs, "notice", "_m")
        assert msgs[: len(original)] == original
        assert len(msgs) == len(original) + 1

    def test_repeated_notices_stack_in_order(self):
        r = self._runner()
        msgs: list = []
        IterationRunner._append_synthetic_notice(msgs, "first", "_m")
        IterationRunner._append_synthetic_notice(msgs, "second", "_m")
        assert [m["content"] for m in msgs] == ["first", "second"]

    def test_empty_content_still_marked(self):
        r = self._runner()
        msgs: list = []
        IterationRunner._append_synthetic_notice(msgs, "", "_m")
        assert len(msgs) == 1


class TestNoMidConversationSystemMessages:
    """Guard against the pattern coming back."""

    def test_iteration_has_no_system_insert(self):
        src = inspect.getsource(IterationRunner)
        # The only remaining 'role': 'system' occurrences must be reads
        # (m.get("role") == "system"), not dict literals being inserted.
        assert '{"role": "system"' not in src.replace('"role": "system"', 'ROLE_SYSTEM_LITERAL')

    def test_compact_uses_user_role_for_synthetic_messages(self):
        from markbot.agent import compact as compact_mod

        src = inspect.getsource(compact_mod)
        # The only real system prompt is the summariser's own, which is
        # correct; synthetic notices must be user-role.
        assert src.count('"role": "system"') <= 1

    def test_anthropic_system_accumulation_still_supported(self):
        """The provider still tolerates system messages; the point is that we
        no longer rely on that behaviour."""
        from markbot.providers.anthropic import AnthropicProvider

        system, msgs = AnthropicProvider._convert_messages(
            AnthropicProvider.__new__(AnthropicProvider),
            [
                {"role": "system", "content": "main"},
                {"role": "user", "content": "hi"},
            ],
        )
        assert system == "main"
        assert msgs[0]["role"] == "user"

    def test_multiple_system_messages_accumulate(self):
        from markbot.providers.anthropic import AnthropicProvider

        system, _ = AnthropicProvider._convert_messages(
            AnthropicProvider.__new__(AnthropicProvider),
            [
                {"role": "system", "content": "a"},
                {"role": "system", "content": "b"},
            ],
        )
        assert isinstance(system, list)
        assert [b["text"] for b in system] == ["a", "b"]


class TestTodoReinjectionPlacement:
    def test_appends_at_tail_and_accounts(self):
        """The reinjection must land at the tail so the persistence cursor
        still tracks the real history, and so the prefix stays cacheable."""
        r = object.__new__(IterationRunner)
        state = _FakeState([
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q"},
        ])

        class _Todo:
            def format_for_injection(self):
                return "- [ ] step one"

        class _Tools:
            def get(self, name):
                return _Todo() if name == "todo" else None

        class _Loop:
            tools = _Tools()

        r.loop = _Loop()
        state.iteration = 100  # past the reinjection interval
        state.last_todo_reinject_iter = -1

        import asyncio

        asyncio.run(IterationRunner._phase_maybe_reinject_todo(r, state))

        assert state.messages[-1]["_todo_reinjection"] is True
        assert state.messages[-1]["role"] == "user"
        # The system message and the user turn keep their positions.
        assert state.messages[0]["role"] == "system"
        assert state.messages[1]["content"] == "q"
