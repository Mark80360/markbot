"""Regression tests for the ``check_privileged`` wiring and its callers.

``is_privileged_sender`` is unit-tested in ``test_authorization.py``, but it is
a pure function: these tests cover the *call site*, which is where the bugs
lived. ``ChannelsConfig`` is a Pydantic model with no ``.get()``, so the
original lookup never resolved a channel section and every ``adminFrom`` /
single-sender ``allowFrom`` rule was silently inert.
"""

import pytest

from markbot.agent.authorization import is_privileged_sender
from markbot.bus.events import InboundMessage
from markbot.cli.slash_commands.builtin import check_privileged
from markbot.cli.slash_commands.router import CommandContext
from markbot.config.schema import ChannelsConfig, Config


def _ctx(sender_id, *, channel="feishu", chat_id="oc_group_1", channels=None,
          config=None, loop=None):
    msg = InboundMessage(
        channel=channel,
        sender_id=sender_id,
        chat_id=chat_id,
        content="/mode auto",
        metadata={"chat_type": "group"},
    )
    if loop is None:
        loop = type("L", (), {})()
        loop.config = config if config is not None else Config()
        loop.channels_config = channels if channels is not None else ChannelsConfig()
    return CommandContext(msg=msg, session=None, key="/mode", raw="/mode auto",
                          args="auto", loop=loop)


def _channels(**sections):
    """Build a ChannelsConfig carrying real per-channel sections."""
    cfg = ChannelsConfig()
    for name, section in sections.items():
        setattr(cfg, name, section)
    return cfg


class TestChannelSectionResolution:
    def test_channels_config_has_no_get(self):
        """Guards the bug's root cause: the old code called .get()."""
        assert not callable(getattr(ChannelsConfig(), "get", None))

    def test_admin_from_is_honoured_in_a_group(self):
        ctx = _ctx("ou_admin",
                   channels=_channels(feishu={"admin_from": ["ou_admin"]}))
        assert check_privileged(ctx, "/mode") is None

    def test_non_admin_denied_in_a_group(self):
        ctx = _ctx("ou_other",
                   channels=_channels(feishu={"admin_from": ["ou_admin"]}))
        assert check_privileged(ctx, "/mode") is not None

    def test_camel_case_admin_from_honoured(self):
        ctx = _ctx("ou_admin",
                   channels=_channels(feishu={"adminFrom": ["ou_admin"]}))
        assert check_privileged(ctx, "/mode") is None

    def test_section_does_not_leak_across_channels(self):
        ctx = _ctx("ou_admin", channel="qq", chat_id="group_1",
                   channels=_channels(feishu={"admin_from": ["ou_admin"]}))
        assert check_privileged(ctx, "/mode") is not None

    def test_sole_allowlisted_sender_is_operator_in_private_chat(self):
        ctx = _ctx("ou_solo", chat_id="dm_1",
                   channels=_channels(feishu={"allow_from": ["ou_solo"]}))
        msg = ctx.msg
        msg.metadata = {"chat_type": "private"}
        assert check_privileged(ctx, "/mode") is None

    def test_global_admin_still_works_without_a_section(self):
        cfg = Config()
        cfg.security.admin_ids = ["ou_admin"]
        ctx = _ctx("ou_admin", config=cfg)
        assert check_privileged(ctx, "/mode") is None

    def test_unknown_channel_is_denied(self):
        ctx = _ctx("ou_x", channel="nosuchchannel")
        assert check_privileged(ctx, "/mode") is not None


class TestProfileIsGatedLikeMode:
    """``/profile`` writes config *and* switches the live permission mode."""

    @pytest.mark.asyncio
    async def test_profile_write_refused_for_non_admin(self, monkeypatch):
        from markbot.cli.slash_commands import builtin

        written = []
        monkeypatch.setattr(
            "markbot.config.loader.update_config_value",
            lambda path, v: written.append((path, v)) or True,
        )
        loop = type("L", (), {})()
        loop.config = Config()
        loop.channels_config = _channels(feishu={"admin_from": ["ou_admin"]})
        msg = InboundMessage(channel="feishu", sender_id="ou_other",
                             chat_id="oc_group_1", content="/profile unattended",
                             metadata={"chat_type": "group"})
        ctx = CommandContext(msg=msg, session=None, key="/profile",
                             raw="/profile unattended", args="unattended",
                             loop=loop)
        out = await builtin.cmd_profile(ctx)
        assert written == [], "config must not be written by a refused sender"
        assert out.content and "ou_other" not in written.__str__()

    @pytest.mark.asyncio
    async def test_profile_write_allowed_for_admin(self, monkeypatch):
        from markbot.cli.slash_commands import builtin

        written = []
        monkeypatch.setattr(
            "markbot.config.loader.update_config_value",
            lambda path, v: written.append((path, v)) or True,
        )
        monkeypatch.setattr(builtin, "list_profiles", lambda: ["unattended"],
                            raising=False)
        loop = type("L", (), {})()
        loop.config = Config()
        loop.channels_config = _channels(feishu={"admin_from": ["ou_admin"]})
        loop.ctx = None
        msg = InboundMessage(channel="feishu", sender_id="ou_admin",
                             chat_id="oc_group_1", content="/profile unattended",
                             metadata={"chat_type": "group"})
        ctx = CommandContext(msg=msg, session=None, key="/profile",
                             raw="/profile unattended", args="unattended",
                             loop=loop)
        await builtin.cmd_profile(ctx)
        assert written, "an authorised sender must still be able to set a profile"


class TestDecisionIsUnchangedByWiring:
    def test_pure_function_agrees_with_wrapper(self):
        """The pure helper and the wired call must not disagree."""
        cc = {"admin_from": ["ou_admin"]}
        pure = is_privileged_sender(
            channel="feishu", sender_id="ou_admin", is_group=True,
            channel_config=cc, global_admin_ids=[],
        )
        wired = check_privileged(_ctx("ou_admin",
                                      channels=_channels(feishu=cc)), "/mode")
        assert pure.allowed is True
        assert wired is None
