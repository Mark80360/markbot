"""Regression tests for the slash-command review fixes.

Each test pins one behaviour that was wrong before: group-only authorisation
for session-scoped commands, the manual compaction window matching the
automatic one, MoA answers landing in history, ``/steer`` refusing while MoA
runs, and typo'd commands being recognised instead of sent to the model.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from markbot.bus.events import InboundMessage
from markbot.cli.slash_commands import builtin
from markbot.cli.slash_commands.builtin import (
    _compact_keep_messages,
    build_builtin_catalog,
    cmd_clear,
    cmd_moa,
    cmd_steer,
    cmd_stop,
)
from markbot.cli.slash_commands.router import CommandContext, looks_like_command
from markbot.config.schema import Config
from markbot.locales import t


class _Session:
    def __init__(self, key: str = "feishu:oc_group_1"):
        self.key = key
        self.messages: list[dict] = []

    def get_history(self, max_messages: int = 0):
        return list(self.messages)

    def add_message(self, role: str, content: str, **kwargs):
        self.messages.append({"role": role, "content": content, **kwargs})

    def clear(self):
        self.messages = []


class _Subagents:
    async def cancel_by_session(self, session_key: str) -> int:
        return 0


class _Sessions:
    def __init__(self, session: _Session):
        self._session = session

    def get_or_create(self, key: str) -> _Session:
        return self._session

    def save(self, session) -> None:
        pass

    def invalidate(self, key: str) -> None:
        pass


class _Loop:
    def __init__(self, *, config=None, session=None, channel_config=None):
        self.config = config if config is not None else Config()
        self.channels_config = channel_config
        self._session = session or _Session()
        self.sessions = _Sessions(self._session)
        self.subagents = _Subagents()
        self._active_tasks: dict[str, list] = {}
        self._moa_sessions: set[str] = set()
        self.steered: list[tuple[str, str]] = []

    def clear_steer(self, session_key: str) -> None:
        self._moa_sessions.discard(session_key)

    def steer(self, session_key: str, text: str) -> bool:
        self.steered.append((session_key, text))
        return True


def _ctx(loop, *, channel="feishu", sender="ou_other", chat_id="oc_group_1",
         chat_type="group", content="/stop", args=""):
    msg = InboundMessage(
        channel=channel,
        sender_id=sender,
        chat_id=chat_id,
        content=content,
        metadata={"chat_type": chat_type},
    )
    return CommandContext(msg=msg, session=loop._session, key=chat_id,
                          raw=content, args=args, loop=loop)


class TestGroupOnlyAuthorisation:
    @pytest.mark.asyncio
    async def test_stop_refused_for_non_admin_in_a_group(self):
        loop = _Loop()
        out = await cmd_stop(_ctx(loop))
        assert "restricted to configured administrators" in out.content

    @pytest.mark.asyncio
    async def test_stop_allowed_in_a_private_chat_without_admins(self):
        loop = _Loop()
        out = await cmd_stop(_ctx(loop, chat_id="dm_1", chat_type="private"))
        assert out.content == t("cmd.stop.none")

    @pytest.mark.asyncio
    async def test_clear_refused_for_non_admin_in_a_group(self):
        loop = _Loop()
        out = await cmd_clear(_ctx(loop, content="/clear"))
        assert "restricted to configured administrators" in out.content
        assert loop._session.messages == []  # untouched


class TestManualCompactionWindow:
    def test_matches_the_automatic_hook(self):
        """Auto-compaction keeps ``keep_recent * 2`` messages; so must /compact."""
        loop = SimpleNamespace(
            config=SimpleNamespace(
                compaction=SimpleNamespace(auto_compact_keep_recent=5)
            )
        )
        assert _compact_keep_messages(loop) == 10

    def test_falls_back_to_the_documented_default(self):
        assert _compact_keep_messages(SimpleNamespace(config=None)) == 10


class TestMoa:
    @pytest.mark.asyncio
    async def test_answer_is_persisted_to_history(self, monkeypatch):
        loop = _Loop()
        loop._session.messages = [{"role": "user", "content": "which db?"}]

        async def fake_run_moa(*, question, history):
            return SimpleNamespace(
                final_answer="Use SQLite.",
                advisors_ok=2,
                advisors_total=3,
                advisor_failures=[],
            )

        monkeypatch.setattr("markbot.agent.moa.run_moa", fake_run_moa)
        out = await cmd_moa(_ctx(loop, content="/moa", args=""))

        assert "Use SQLite." in out.content
        assert loop._session.messages[-1] == {
            "role": "assistant", "content": "Use SQLite.",
        }

    @pytest.mark.asyncio
    async def test_steer_refuses_while_moa_runs(self):
        loop = _Loop()
        key = loop._session.key
        loop._moa_sessions.add(key)
        out = await cmd_steer(_ctx(loop, content="/steer hurry", args="hurry"))
        assert out.content == t("cmd.steer.moa_running")
        assert loop.steered == []


class TestUnknownCommandDetection:
    @pytest.mark.parametrize("text", ["/trace[full]", "/compcat", "/new ", "/Mode"])
    def test_typos_look_like_commands(self, text):
        assert looks_like_command(text) is True

    @pytest.mark.parametrize(
        "text", ["/etc/hosts is broken", "/home/user/a.txt", "/", "//x", "hello /stop"]
    )
    def test_paths_and_prose_do_not(self, text):
        assert looks_like_command(text) is False


class TestCatalogSurface:
    def test_summary_alias_is_registered(self):
        from markbot.cli.slash_commands.router import CommandRouter

        router = CommandRouter()
        builtin.register_builtin_commands(router)
        assert "/summary" in router._priority
        assert router._priority["/summary"] is router._priority["/compact_str"]

    def test_btw_alias_reaches_steer_without_the_session_lock(self):
        """/btw must behave exactly like /steer, including the priority tier."""
        from markbot.cli.slash_commands.router import CommandRouter

        router = CommandRouter()
        builtin.register_builtin_commands(router)
        assert "/btw" in router._priority
        assert router._priority["/btw"] is router._priority["/steer"]
        assert router.is_priority("/btw focus on tests")

    @pytest.mark.asyncio
    async def test_btw_routes_to_steer_and_strips_the_alias(self):
        from markbot.cli.slash_commands.router import CommandRouter

        router = CommandRouter()
        builtin.register_builtin_commands(router)
        loop = _Loop()
        loop._active_tasks[loop._session.key] = [SimpleNamespace(done=lambda: False)]
        ctx = _ctx(loop, content="/btw check the logs", args="")
        out = await router.dispatch_priority(ctx)
        assert out is not None
        assert loop.steered == [(loop._session.key, "check the logs")]

    @pytest.mark.asyncio
    async def test_btw_usage_message_echoes_the_alias(self):
        """A bare /btw should not answer with instructions for /steer."""
        loop = _Loop()
        out = await cmd_steer(_ctx(loop, content="/btw", args=""))
        assert "/btw" in out.content
        assert "/steer" not in out.content

    def test_help_groups_by_category(self):
        text = build_builtin_catalog().help_text()
        # Four categories -> three blank-line separators, plus the header.
        assert text.count("\n\n") >= 3
        for name in ("/stop", "/compact", "/mode", "/moa"):
            assert name in text
        assert "/summary" in text, "aliases should be discoverable from /help"
