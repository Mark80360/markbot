"""Tests for privileged-operation authorization.

``/mode`` writes a process-global tool-permission mode (and persists it to
config.json); ``/restart`` calls ``os.execv``. Before this guard existed,
any participant of any allowlisted group chat could grant the agent
unrestricted host access, or kill the daemon, for every other user.
"""

from __future__ import annotations

import pytest

from markbot.agent.authorization import (
    denial_message,
    is_privileged_sender,
)


class FakeChannelConfig:
    def __init__(self, allow_from=None, admin_from=None):
        if allow_from is not None:
            self.allow_from = allow_from
        if admin_from is not None:
            self.admin_from = admin_from


class TestLocalSurfaces:
    @pytest.mark.parametrize("channel", ["cli", "web", "api", "local", "test"])
    def test_local_surfaces_always_privileged(self, channel):
        d = is_privileged_sender(
            channel=channel, sender_id="anyone", is_group=None, channel_config=None
        )
        assert d.allowed


class TestGroupChats:
    def test_group_is_never_implicitly_privileged(self):
        cfg = FakeChannelConfig(allow_from=["alice", "bob"])
        d = is_privileged_sender(
            channel="feishu", sender_id="alice", is_group=True, channel_config=cfg
        )
        assert not d.allowed
        assert "group" in d.reason

    def test_group_allowed_with_global_admin(self):
        cfg = FakeChannelConfig(allow_from=["alice", "bob"])
        d = is_privileged_sender(
            channel="feishu", sender_id="bob", is_group=True,
            channel_config=cfg, global_admin_ids=["bob"],
        )
        assert d.allowed

    def test_group_denied_when_not_admin(self):
        cfg = FakeChannelConfig(allow_from=["alice", "bob"])
        d = is_privileged_sender(
            channel="feishu", sender_id="alice", is_group=True,
            channel_config=cfg, global_admin_ids=["bob"],
        )
        assert not d.allowed
        assert "adminIds" in d.reason

    def test_group_allowed_with_channel_admin(self):
        cfg = FakeChannelConfig(allow_from=["alice", "bob"], admin_from=["bob"])
        d = is_privileged_sender(
            channel="feishu", sender_id="bob", is_group=True, channel_config=cfg
        )
        assert d.allowed

    def test_unknown_groupness_fails_closed(self):
        """``derive_is_group`` returns None when chat_type is unrecognised."""
        cfg = FakeChannelConfig(allow_from=["alice"])
        d = is_privileged_sender(
            channel="slack", sender_id="alice", is_group=None, channel_config=cfg
        )
        assert not d.allowed


class TestPrivateChats:
    def test_sole_allowlisted_sender_is_the_operator(self):
        """A single-sender deployment has nobody to escalate over."""
        cfg = FakeChannelConfig(allow_from=["alice"])
        d = is_privileged_sender(
            channel="feishu", sender_id="alice", is_group=False, channel_config=cfg
        )
        assert d.allowed
        assert "sole allowlisted" in d.reason

    def test_multi_user_channel_denies_non_admins(self):
        cfg = FakeChannelConfig(allow_from=["alice", "bob"])
        d = is_privileged_sender(
            channel="feishu", sender_id="alice", is_group=False, channel_config=cfg
        )
        assert not d.allowed
        assert "adminIds" in d.reason

    def test_admin_bypasses_single_sender_inference(self):
        cfg = FakeChannelConfig(allow_from=["alice", "bob"])
        d = is_privileged_sender(
            channel="feishu", sender_id="bob", is_group=False,
            channel_config=cfg, global_admin_ids=["bob"],
        )
        assert d.allowed


class TestMatchingForms:
    def test_channel_qualified_id(self):
        cfg = FakeChannelConfig(allow_from=["u1"])
        ok = is_privileged_sender(
            channel="feishu", sender_id="u1", is_group=True,
            channel_config=cfg, global_admin_ids=["feishu:u1"],
        )
        assert ok.allowed

    def test_channel_qualified_id_does_not_leak_across_channels(self):
        """A short ID like '42' can exist on two platforms; the qualifier
        must prevent a match on the wrong one."""
        cfg = FakeChannelConfig(allow_from=["u1"])
        denied = is_privileged_sender(
            channel="slack", sender_id="u1", is_group=True,
            channel_config=cfg, global_admin_ids=["feishu:u1"],
        )
        assert not denied.allowed

    def test_bare_id_matches_any_channel(self):
        cfg = FakeChannelConfig(allow_from=["u1"])
        d = is_privileged_sender(
            channel="slack", sender_id="u1", is_group=True,
            channel_config=cfg, global_admin_ids=["u1"],
        )
        assert d.allowed

    def test_channel_wildcard(self):
        cfg = FakeChannelConfig(allow_from=["u1"])
        ok = is_privileged_sender(
            channel="slack", sender_id="u9", is_group=True,
            channel_config=cfg, global_admin_ids=["slack:*"],
        )
        other = is_privileged_sender(
            channel="discord", sender_id="u9", is_group=True,
            channel_config=cfg, global_admin_ids=["slack:*"],
        )
        assert ok.allowed
        assert not other.allowed

    def test_global_star_opens_everything(self):
        cfg = FakeChannelConfig(allow_from=["a", "b"])
        d = is_privileged_sender(
            channel="qq", sender_id="anyone", is_group=True,
            channel_config=cfg, global_admin_ids=["*"],
        )
        assert d.allowed


class TestNegativeCases:
    def test_empty_sender_denied(self):
        cfg = FakeChannelConfig(allow_from=["alice"])
        d = is_privileged_sender(
            channel="feishu", sender_id="", is_group=False, channel_config=cfg
        )
        assert not d.allowed
        assert "sender" in d.reason

    def test_none_sender_denied(self):
        cfg = FakeChannelConfig(allow_from=["alice"])
        d = is_privileged_sender(
            channel="feishu", sender_id=None, is_group=False, channel_config=cfg
        )
        assert not d.allowed

    def test_dict_channel_config_supported(self):
        d = is_privileged_sender(
            channel="feishu", sender_id="u1", is_group=True,
            channel_config={"allowFrom": ["u1", "u2"]},
            global_admin_ids=["u1"],
        )
        assert d.allowed

    def test_camel_case_admin_from_supported(self):
        d = is_privileged_sender(
            channel="feishu", sender_id="u1", is_group=True,
            channel_config={"allowFrom": ["u1", "u2"], "adminFrom": ["u1"]},
        )
        assert d.allowed

    def test_denial_message_is_actionable(self):
        d = is_privileged_sender(
            channel="feishu", sender_id="x", is_group=True,
            channel_config=FakeChannelConfig(allow_from=["a", "b"]),
        )
        msg = denial_message(d, command="/mode")
        assert "/mode" in msg
        assert "adminIds" in msg

    def test_denial_message_empty_when_allowed(self):
        d = is_privileged_sender(channel="cli", sender_id="u")
        assert denial_message(d, command="/mode") == ""


class TestConfigWiring:
    def test_config_exposes_security_section(self):
        from markbot.config.schema import Config

        cfg = Config()
        assert cfg.security.admin_ids == []
        dumped = cfg.model_dump(by_alias=True)
        assert "security" in dumped
        assert "adminIds" in dumped["security"]

    def test_config_roundtrips_admin_ids(self):
        from markbot.config.schema import Config

        cfg = Config()
        cfg.security.admin_ids = ["feishu:ou_1", "slack:U2"]
        assert cfg.is_privileged_sender(
            channel="feishu", sender_id="ou_1", is_group=True,
            channel_config=FakeChannelConfig(allow_from=["ou_1", "other"]),
        ).allowed

    def test_config_denies_non_admin_in_group(self):
        from markbot.config.schema import Config

        cfg = Config()
        cfg.security.admin_ids = ["feishu:ou_1"]
        assert not cfg.is_privileged_sender(
            channel="feishu", sender_id="ou_9", is_group=True,
            channel_config=FakeChannelConfig(allow_from=["ou_1", "ou_9"]),
        ).allowed
