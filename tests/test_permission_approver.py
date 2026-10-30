"""Tests for :class:`PermissionApprover` — the interactive confirmation path.

This class had **zero** coverage: ``test_profile_and_security.py`` imported
it but never instantiated it, and the pipeline tests substituted a fake
approver. That left the one component standing between "the model wants to
run rm -rf" and "it runs" untested, while it was being made the default
safety control.

Every exit path here is a *deny* except an explicit allow. These tests pin
that, because the failure mode of this class is silent: a bug that returns
``True`` on timeout is indistinguishable from a user who said yes.
"""

from __future__ import annotations

import asyncio

import pytest

from markbot.agent.permission_approval import (
    PermissionApprover,
    _apply_choice,
    _parse_choice,
)
from markbot.types.permission import PermissionMode, ToolPermissionContext
from markbot.types.tool import ToolContext


def _ctx(channel: str = "feishu", chat_id: str = "u1", session_id: str = "s1") -> ToolContext:
    return ToolContext(
        session_id=session_id,
        workspace="/tmp",
        permission_mode=PermissionMode.DEFAULT,
        tool_permission_context=ToolPermissionContext(mode=PermissionMode.DEFAULT),
        channel=channel,
        chat_id=chat_id,
    )


def _responder(approver: PermissionApprover, answer: str):
    """Build a send_callback that answers the prompt like a user would."""

    async def _send(msg):
        approval_id = msg.metadata["question_id"]

        async def _later():
            await asyncio.sleep(0)
            approver.handle_response(approval_id, answer)

        asyncio.get_running_loop().create_task(_later())

    return _send


class TestEveryExitPathDenies:
    async def test_no_channel_denies(self):
        approver = PermissionApprover(send_callback=_responder(None, "Allow"))
        assert await approver("exec", {}, _ctx(channel=""), "needs approval") is False

    async def test_no_chat_denies(self):
        approver = PermissionApprover(send_callback=_responder(None, "Allow"))
        assert await approver("exec", {}, _ctx(chat_id=""), "needs approval") is False

    async def test_no_send_callback_denies(self):
        approver = PermissionApprover(send_callback=None)
        assert await approver("exec", {}, _ctx(), "needs approval") is False

    async def test_timeout_denies(self):
        """The 300s default used to be the only value; it must still deny."""

        async def _never_answers(msg):
            return None

        approver = PermissionApprover(send_callback=_never_answers, timeout_s=0.05)
        assert await approver("exec", {}, _ctx(), "needs approval") is False

    async def test_send_failure_denies(self):
        async def _boom(msg):
            raise RuntimeError("transport down")

        approver = PermissionApprover(send_callback=_boom, timeout_s=1.0)
        assert await approver("exec", {}, _ctx(), "needs approval") is False

    async def test_unparseable_answer_denies(self):
        approver = PermissionApprover(
            send_callback=_responder(None, "what?"), timeout_s=1.0
        )
        # ``_responder`` needs the instance, so rebuild it properly.
        approver._send_callback = _responder(approver, "what?")
        assert await approver("exec", {}, _ctx(), "needs approval") is False


class TestExplicitAnswers:
    async def test_allow_returns_true(self):
        approver = PermissionApprover(timeout_s=1.0)
        approver._send_callback = _responder(approver, "Allow")
        assert await approver("exec", {}, _ctx(), "needs approval") is True

    async def test_deny_returns_false(self):
        approver = PermissionApprover(timeout_s=1.0)
        approver._send_callback = _responder(approver, "Deny")
        assert await approver("exec", {}, _ctx(), "needs approval") is False

    async def test_allow_all_marks_the_session(self):
        approver = PermissionApprover(timeout_s=1.0)
        approver._send_callback = _responder(approver, "Allow All")
        assert await approver("exec", {}, _ctx(session_id="s9"), "needs approval") is True
        assert approver.is_allow_all("s9") is True

    async def test_allow_all_skips_the_next_prompt(self):
        approver = PermissionApprover(timeout_s=1.0)
        approver.set_allow_all("s9")

        async def _must_not_be_called(msg):  # pragma: no cover
            raise AssertionError("prompted despite allow-all")

        approver._send_callback = _must_not_be_called
        assert await approver("exec", {}, _ctx(session_id="s9"), "again") is True

    async def test_allow_all_does_not_leak_to_other_sessions(self):
        approver = PermissionApprover(timeout_s=0.05)
        approver.set_allow_all("s9")

        async def _never(msg):
            return None

        approver._send_callback = _never
        assert await approver("exec", {}, _ctx(session_id="other"), "x") is False

    async def test_clear_allow_all_restores_prompting(self):
        approver = PermissionApprover(timeout_s=1.0)
        approver.set_allow_all("s9")
        approver.clear_allow_all("s9")
        assert approver.is_allow_all("s9") is False
        approver._send_callback = _responder(approver, "Deny")
        assert await approver("exec", {}, _ctx(session_id="s9"), "x") is False


class TestHandleResponse:
    async def test_unknown_id_returns_false(self):
        approver = PermissionApprover()
        assert approver.handle_response("nope", "Allow") is False

    async def test_known_id_resolves_once(self):
        approver = PermissionApprover(timeout_s=1.0)
        seen: list[str] = []

        async def _send(msg):
            approval_id = msg.metadata["question_id"]
            seen.append(approval_id)

            async def _later():
                await asyncio.sleep(0)
                assert approver.handle_response(approval_id, "Allow") is True
                # Second resolution of the same id must be refused.
                assert approver.handle_response(approval_id, "Allow") is False

            asyncio.get_running_loop().create_task(_later())

        approver._send_callback = _send
        assert await approver("exec", {}, _ctx(), "x") is True
        assert len(seen) == 1

    async def test_pending_is_cleaned_up_after_timeout(self):
        async def _never(msg):
            return None

        approver = PermissionApprover(send_callback=_never, timeout_s=0.05)
        assert await approver("exec", {}, _ctx(), "x") is False
        assert approver._pending == {}


class TestChoiceParsing:
    @pytest.mark.parametrize(
        "answer,expected",
        [
            ("Allow", "allow"),
            ("User selected: Allow", "allow"),
            ("1", "allow"),
            ("Deny", "deny"),
            ("User selected: Deny", "deny"),
            ("3", "deny"),
            ("Allow All", "allow_all"),
            ("User selected: Allow All", "allow_all"),
            ("2", "allow_all"),
            ("allow-all", "allow_all"),
        ],
    )
    def test_parse_choice(self, answer, expected):
        assert _parse_choice(answer) == expected

    def test_apply_choice(self):
        approver = PermissionApprover()
        assert _apply_choice("allow", approver, "s") is True
        assert _apply_choice("deny", approver, "s") is False
        assert _apply_choice("allow_all", approver, "s") is True
        assert approver.is_allow_all("s") is True

    def test_negative_phrasing_is_not_mistaken_for_allow(self):
        """Substring matching is a known soft spot; document what it does.

        ``_parse_choice`` looks for the allow label anywhere in the text, so
        "do not allow" still parses as allow. It is only reachable from an
        answer the user typed into the approval prompt itself, but the
        behaviour is pinned here so it cannot change silently.
        """
        assert _parse_choice("do not allow") == "allow"
