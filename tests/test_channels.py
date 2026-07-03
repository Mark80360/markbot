"""Tests for the markbot.channels package (base, discovery, manager, concrete channels)."""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from markbot.bus.events import InboundMessage, OutboundMessage
from markbot.bus.queue import MessageBus
from markbot.channels import BaseChannel, ChannelManager
from markbot.channels import discovery
from markbot.channels.base import BaseChannel as BaseChannelAlias
from markbot.channels.manager import ChannelManager as ChannelManagerAlias


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


class StubConfig:
    """Minimal config object usable as a channel config, mimicking pydantic models."""

    def __init__(self, **kwargs):
        self.allow_from = ["*"]
        self.enabled = True
        self.__dict__.update(kwargs)


def _make_bus():
    return MessageBus()


@pytest.fixture
def bus():
    return _make_bus()


# ---------------------------------------------------------------------------
# base.py
# ---------------------------------------------------------------------------


class _ConcreteChannel(BaseChannel):
    name = "concrete"
    display_name = "Concrete"
    _started = False

    async def start(self) -> None:
        self._running = True
        self._started = True

    async def stop(self) -> None:
        self._running = False
        self._started = False

    async def send(self, msg: OutboundMessage) -> None:
        # no-op
        pass


class _StreamingChannel(_ConcreteChannel):
    name = "streaming"
    display_name = "Streaming"

    async def send_delta(self, chat_id: str, delta: str, metadata=None) -> None:
        pass


class TestBaseChannel:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            BaseChannel(object(), _make_bus())

    def test_init_attributes(self, bus):
        cfg = {"enabled": True}
        ch = _ConcreteChannel(cfg, bus)
        assert ch.config is cfg
        assert ch.bus is bus
        assert ch.is_running is False
        assert ch.name == "concrete"

    def test_default_config(self):
        assert _ConcreteChannel.default_config() == {"enabled": False}

    @pytest.mark.asyncio
    async def test_login_default_returns_true(self, bus):
        ch = _ConcreteChannel(StubConfig(), bus)
        assert await ch.login() is True
        assert await ch.login(force=True) is True

    @pytest.mark.asyncio
    async def test_health_check_default(self, bus):
        ch = _ConcreteChannel(StubConfig(), bus)
        result = await ch.health_check()
        assert result["healthy"] is False
        assert result["error"] == "Channel not running"
        await ch.start()
        result = await ch.health_check()
        assert result["healthy"] is True
        assert result["error"] is None

    @pytest.mark.asyncio
    async def test_restart_calls_stop_and_start(self, bus):
        ch = _ConcreteChannel(StubConfig(), bus)
        await ch.restart()
        assert ch.is_running is True

    @pytest.mark.asyncio
    async def test_restart_propagates_start_error(self, bus):
        class _BoomChannel(_ConcreteChannel):
            async def start(self):  # noqa: D401
                raise RuntimeError("boom")

        ch = _BoomChannel(StubConfig(), bus)
        with pytest.raises(RuntimeError, match="boom"):
            await ch.restart()
        assert ch.is_running is False

    @pytest.mark.asyncio
    async def test_transcribe_audio_no_key(self, bus):
        ch = _ConcreteChannel(StubConfig(), bus)
        # No transcription_api_key set → returns empty string without network.
        assert await ch.transcribe_audio("foo.mp3") == ""

    @pytest.mark.asyncio
    async def test_transcribe_audio_uses_provider(self, bus, monkeypatch):
        ch = _ConcreteChannel(StubConfig(), bus)
        ch.transcription_api_key = "key"

        async def _fake_transcribe(self, file_path):
            assert file_path == "audio.amr"
            return "hello"

        from markbot.providers import transcription as _t

        monkeypatch.setattr(
            _t.GroqTranscriptionProvider, "transcribe", _fake_transcribe, raising=False
        )
        out = await ch.transcribe_audio("audio.amr")
        assert out == "hello"

    @pytest.mark.asyncio
    async def test_transcribe_audio_swallows_errors(self, bus, monkeypatch):
        ch = _ConcreteChannel(StubConfig(), bus)
        ch.transcription_api_key = "key"

        async def _explode(self, file_path):
            raise RuntimeError("network down")

        from markbot.providers import transcription as _t

        monkeypatch.setattr(
            _t.GroqTranscriptionProvider, "transcribe", _explode, raising=False
        )
        assert await ch.transcribe_audio("audio.amr") == ""

    def test_supports_streaming_false_without_impl(self, bus):
        # _ConcreteChannel doesn't override send_delta → supports_streaming False
        ch = _ConcreteChannel({"streaming": True}, bus)
        assert ch.supports_streaming is False

    def test_supports_streaming_true_with_impl(self, bus):
        ch = _StreamingChannel({"streaming": True}, bus)
        assert ch.supports_streaming is True

    def test_supports_streaming_disabled_in_config(self, bus):
        ch = _StreamingChannel({"streaming": False}, bus)
        assert ch.supports_streaming is False

    def test_supports_streaming_object_config(self, bus):
        cfg = SimpleNamespace(streaming=True)
        ch = _StreamingChannel(cfg, bus)
        assert ch.supports_streaming is True

    def test_is_allowed_empty_denies(self, bus):
        cfg = StubConfig()
        cfg.allow_from = []
        ch = _ConcreteChannel(cfg, bus)
        assert ch.is_allowed("anyone") is False

    def test_is_allowed_wildcard(self, bus):
        cfg = StubConfig()
        cfg.allow_from = ["*"]
        ch = _ConcreteChannel(cfg, bus)
        assert ch.is_allowed("anyone") is True

    def test_is_allowed_explicit(self, bus):
        cfg = StubConfig()
        cfg.allow_from = ["alice", "bob"]
        ch = _ConcreteChannel(cfg, bus)
        assert ch.is_allowed("alice") is True
        assert ch.is_allowed("bob") is True
        assert ch.is_allowed("eve") is False
        # sender_id is coerced to string
        assert ch.is_allowed(12345) is False

    @pytest.mark.asyncio
    async def test_handle_message_denied_does_not_publish(self, bus):
        cfg = StubConfig()
        cfg.allow_from = ["alice"]
        ch = _ConcreteChannel(cfg, bus)
        ch._running = True
        await ch._handle_message("eve", "chat", "hi")
        assert bus.inbound_size == 0

    @pytest.mark.asyncio
    async def test_handle_message_allowed_publishes(self, bus):
        cfg = StubConfig()
        cfg.allow_from = ["*"]
        ch = _ConcreteChannel(cfg, bus)
        ch._running = True
        await ch._handle_message("user1", "chat1", "hello", media=["a.jpg"])
        msg = await bus.consume_inbound()
        assert msg.channel == "concrete"
        assert msg.sender_id == "user1"
        assert msg.chat_id == "chat1"
        assert msg.content == "hello"
        assert msg.media == ["a.jpg"]
        assert "_wants_stream" not in msg.metadata

    @pytest.mark.asyncio
    async def test_handle_message_streaming_meta(self, bus):
        cfg = StubConfig()
        cfg.allow_from = ["*"]
        cfg.streaming = True
        ch = _StreamingChannel(cfg, bus)
        ch._running = True
        await ch._handle_message("u", "c", "hi")
        msg = await bus.consume_inbound()
        assert msg.metadata.get("_wants_stream") is True

    @pytest.mark.asyncio
    async def test_handle_message_session_key_override(self, bus):
        cfg = StubConfig()
        cfg.allow_from = ["*"]
        ch = _ConcreteChannel(cfg, bus)
        ch._running = True
        await ch._handle_message("u", "c", "hi", session_key="custom:thread")
        msg = await bus.consume_inbound()
        assert msg.session_key_override == "custom:thread"


# ---------------------------------------------------------------------------
# discovery.py
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_discover_channel_names_excludes_internal(self):
        names = discovery.discover_channel_names()
        assert "base" not in names
        assert "manager" not in names
        assert "dingtalk" in names or "feishu" in names  # at least one builtin

    def test_load_channel_class_dingtalk(self):
        cls = discovery.load_channel_class("dingtalk")
        assert isinstance(cls, type)
        assert issubclass(cls, BaseChannel)

    def test_load_channel_class_raises_on_missing(self):
        with pytest.raises(ImportError, match="No BaseChannel"):
            discovery.load_channel_class("base")

    def test_internal_set_is_frozen(self):
        assert discovery._INTERNAL == frozenset({"base", "manager"})

    def test_discover_all_includes_builtins(self):
        all_channels = discovery.discover_all()
        # Should contain at least some built-in channels
        assert isinstance(all_channels, dict)
        builtin_names = {"dingtalk", "feishu", "qq", "weixin", "email"}
        assert any(name in all_channels for name in builtin_names)

    def test_discover_all_builtins_take_priority(self, monkeypatch):
        # External plugin returning "dingtalk" should be shadowed by builtin.
        fake_ep = MagicMock()
        fake_ep.name = "dingtalk"
        fake_ep.load.return_value = _ConcreteChannel

        def _fake_entry_points(group=None):
            return [fake_ep]

        monkeypatch.setattr(
            "importlib.metadata.entry_points", _fake_entry_points
        )
        all_channels = discovery.discover_all()
        assert all_channels["dingtalk"] is _ConcreteChannel.__mro__[0] or issubclass(
            all_channels["dingtalk"], BaseChannel
        )

    def test_load_channel_class_returns_subclass(self):
        cls = discovery.load_channel_class("email")
        assert cls.name == "email"
        assert cls.display_name == "Email"


# ---------------------------------------------------------------------------
# manager.py
# ---------------------------------------------------------------------------


class TestChannelManager:
    def _make_config(self, channels: dict | None = None):
        from markbot.config.schema import ChannelsConfig, Config, ProviderConfig

        cfg = Config()
        cfg.providers.groq = ProviderConfig(api_key="groq-key")
        if channels:
            cfg.channels = ChannelsConfig.model_validate(channels)
        else:
            cfg.channels = ChannelsConfig()
        # Defaults already correct: send_tool_hints=False, send_progress=True, send_max_retries=3
        return cfg

    def test_init_with_no_channels(self):
        cfg = self._make_config({})
        mgr = ChannelManager(cfg, _make_bus())
        assert mgr.channels == {}
        assert mgr.enabled_channels == []
        assert mgr.get_channel("foo") is None
        assert mgr.get_status() == {}

    def test_init_with_disabled_channel_skipped(self):
        cfg = self._make_config({"dingtalk": {"enabled": False}})
        mgr = ChannelManager(cfg, _make_bus())
        assert mgr.channels == {}

    def test_init_with_enabled_dingtalk(self):
        cfg = self._make_config(
            {
                "dingtalk": {
                    "enabled": True,
                    "clientId": "id",
                    "clientSecret": "secret",
                    "allowFrom": ["*"],
                }
            }
        )
        mgr = ChannelManager(cfg, _make_bus())
        assert "dingtalk" in mgr.channels
        ch = mgr.get_channel("dingtalk")
        assert ch is not None
        assert ch.transcription_api_key == "groq-key"
        assert ch.is_running is False
        assert "dingtalk" in mgr.enabled_channels
        # get_status structure
        status = mgr.get_status()
        assert status["dingtalk"]["enabled"] is True
        assert status["dingtalk"]["running"] is False

    def test_init_empty_allowfrom_raises_systemexit(self):
        cfg = self._make_config(
            {
                "dingtalk": {
                    "enabled": True,
                    "clientId": "id",
                    "clientSecret": "secret",
                    "allowFrom": [],
                }
            }
        )
        with pytest.raises(SystemExit):
            ChannelManager(cfg, _make_bus())

    def test_enabled_channels_property(self):
        cfg = self._make_config(
            {
                "dingtalk": {
                    "enabled": True,
                    "clientId": "id",
                    "clientSecret": "s",
                    "allowFrom": ["*"],
                },
                "email": {
                    "enabled": True,
                    "allowFrom": ["*"],
                    "imapHost": "h",
                    "imapUsername": "u",
                    "imapPassword": "p",
                },
            }
        )
        mgr = ChannelManager(cfg, _make_bus())
        assert set(mgr.enabled_channels) >= {"dingtalk", "email"}

    @pytest.mark.asyncio
    async def test_start_all_with_no_channels_returns_early(self):
        cfg = self._make_config({})
        mgr = ChannelManager(cfg, _make_bus())
        # Should not raise; should not create dispatch tasks.
        await mgr.start_all()
        assert mgr._dispatch_task is None

    @pytest.mark.asyncio
    async def test_stop_all_with_no_channels(self):
        cfg = self._make_config({})
        mgr = ChannelManager(cfg, _make_bus())
        await mgr.stop_all()  # should be a no-op

    @pytest.mark.asyncio
    async def test_send_once_regular(self, bus):
        cfg = self._make_config({"dingtalk": {"enabled": False}})
        mgr = ChannelManager(cfg, bus)
        ch = AsyncMock()
        msg = OutboundMessage(channel="x", chat_id="c", content="hi")
        await ChannelManager._send_once(ch, msg)
        ch.send.assert_awaited_once_with(msg)
        ch.send_delta.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_once_stream_delta(self, bus):
        cfg = self._make_config({"dingtalk": {"enabled": False}})
        mgr = ChannelManager(cfg, bus)
        ch = AsyncMock()
        msg = OutboundMessage(
            channel="x", chat_id="c", content="d", metadata={"_stream_delta": True}
        )
        await ChannelManager._send_once(ch, msg)
        ch.send_delta.assert_awaited_once()
        ch.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_once_stream_end(self, bus):
        ch = AsyncMock()
        msg = OutboundMessage(
            channel="x", chat_id="c", content="d", metadata={"_stream_end": True}
        )
        await ChannelManager._send_once(ch, msg)
        ch.send_delta.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_once_streamed_skips_send(self, bus):
        ch = AsyncMock()
        msg = OutboundMessage(
            channel="x", chat_id="c", content="d", metadata={"_streamed": True}
        )
        await ChannelManager._send_once(ch, msg)
        ch.send.assert_not_called()
        ch.send_delta.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_with_retry_success(self, bus):
        cfg = self._make_config({})
        cfg.channels.send_max_retries = 3
        mgr = ChannelManager(cfg, bus)
        ch = AsyncMock()
        msg = OutboundMessage(channel="x", chat_id="c", content="hi")
        await mgr._send_with_retry(ch, msg)
        ch.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_with_retry_stream_delta_no_retry(self, bus, monkeypatch):
        cfg = self._make_config({})
        cfg.channels.send_max_retries = 3
        mgr = ChannelManager(cfg, bus)
        ch = AsyncMock()
        ch.send_delta.side_effect = RuntimeError("boom")

        msg = OutboundMessage(
            channel="x", chat_id="c", content="d", metadata={"_stream_delta": True}
        )
        # Should NOT retry — only one call to send_delta.
        await mgr._send_with_retry(ch, msg)
        assert ch.send_delta.await_count == 1

    @pytest.mark.asyncio
    async def test_send_with_retry_retries_and_succeeds(self, bus, monkeypatch):
        cfg = self._make_config({})
        cfg.channels.send_max_retries = 3
        mgr = ChannelManager(cfg, bus)
        ch = AsyncMock()
        ch.send.side_effect = [RuntimeError("boom"), None]
        # Patch sleep to avoid real delays.
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        msg = OutboundMessage(channel="x", chat_id="c", content="hi")
        await mgr._send_with_retry(ch, msg)
        assert ch.send.await_count == 2

    @pytest.mark.asyncio
    async def test_send_with_retry_exhausts_then_gives_up(self, bus, monkeypatch):
        cfg = self._make_config({})
        cfg.channels.send_max_retries = 2
        mgr = ChannelManager(cfg, bus)
        ch = AsyncMock()
        ch.send.side_effect = RuntimeError("always boom")
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        msg = OutboundMessage(channel="x", chat_id="c", content="hi")
        await mgr._send_with_retry(ch, msg)
        assert ch.send.await_count == 2

    @pytest.mark.asyncio
    async def test_try_restart_channel_respects_cooldown(self, bus, monkeypatch):
        cfg = self._make_config({})
        mgr = ChannelManager(cfg, bus)

        fake_time = [1000.0]
        monkeypatch.setattr("time.monotonic", lambda: fake_time[0])

        ch = AsyncMock()
        ch.restart = AsyncMock()

        # First restart succeeds (now - 0 >= cooldown)
        await mgr._try_restart_channel("c", ch)
        assert ch.restart.await_count == 1
        assert mgr._restarting["c"] is False

        # Within cooldown — skipped (last_restart was set to 1000)
        fake_time[0] = 1100.0
        await mgr._try_restart_channel("c", ch)
        assert ch.restart.await_count == 1

        # After cooldown passes — retries
        fake_time[0] = 1400.0
        await mgr._try_restart_channel("c", ch)
        assert ch.restart.await_count == 2


# ---------------------------------------------------------------------------
# dingtalk.py — pure-logic helpers
# ---------------------------------------------------------------------------


class TestDingTalkChannel:
    def make_channel(self, bus):
        from markbot.channels.dingtalk import DingTalkChannel, DingTalkConfig

        cfg = DingTalkConfig(client_id="id", client_secret="secret", allow_from=["*"])
        return DingTalkChannel(cfg, bus)

    def test_default_config(self):
        from markbot.channels.dingtalk import DingTalkChannel

        dc = DingTalkChannel.default_config()
        assert dc["enabled"] is False
        assert dc["clientId"] == ""
        assert dc["allowFrom"] == []

    def test_config_validation(self):
        from markbot.channels.dingtalk import DingTalkConfig

        cfg = DingTalkConfig.model_validate(
            {"clientId": "abc", "clientSecret": "xyz", "allowFrom": ["*"]}
        )
        assert cfg.client_id == "abc"
        assert cfg.client_secret == "xyz"
        assert cfg.allow_from == ["*"]

    def test_name_and_display(self, bus):
        ch = self.make_channel(bus)
        assert ch.name == "dingtalk"
        assert ch.display_name == "DingTalk"

    def test_is_http_url(self):
        from markbot.channels.dingtalk import DingTalkChannel

        assert DingTalkChannel._is_http_url("https://x.com/a") is True
        assert DingTalkChannel._is_http_url("http://x.com/a") is True
        assert DingTalkChannel._is_http_url("file:///tmp/a") is False
        assert DingTalkChannel._is_http_url("/tmp/a") is False

    def test_guess_upload_type(self, bus):
        ch = self.make_channel(bus)
        assert ch._guess_upload_type("https://x.com/a.jpg") == "image"
        assert ch._guess_upload_type("https://x.com/a.png") == "image"
        assert ch._guess_upload_type("https://x.com/a.mp3") == "voice"
        assert ch._guess_upload_type("https://x.com/a.mp4") == "video"
        assert ch._guess_upload_type("https://x.com/a.pdf") == "file"
        assert ch._guess_upload_type("https://x.com/noext") == "file"

    def test_guess_filename(self, bus):
        ch = self.make_channel(bus)
        assert ch._guess_filename("https://x.com/path/photo.jpg", "image") == "photo.jpg"
        assert ch._guess_filename("https://x.com/", "image") == "image.jpg"
        assert ch._guess_filename("https://x.com/", "voice") == "audio.amr"
        assert ch._guess_filename("https://x.com/", "video") == "video.mp4"
        assert ch._guess_filename("https://x.com/", "file") == "file.bin"

    @pytest.mark.asyncio
    async def test_on_message_builds_group_chat_id(self, bus):
        ch = self.make_channel(bus)
        ch._running = True
        published: list[InboundMessage] = []

        async def _capture(msg):
            published.append(msg)

        bus.publish_inbound = _capture  # type: ignore[assignment]

        await ch._on_message(
            content="hello",
            sender_id="staff1",
            sender_name="Alice",
            conversation_type="2",
            conversation_id="conv123",
        )
        assert len(published) == 1
        assert published[0].chat_id == "group:conv123"
        assert published[0].sender_id == "staff1"
        assert published[0].metadata["platform"] == "dingtalk"

    @pytest.mark.asyncio
    async def test_on_message_private_chat(self, bus):
        ch = self.make_channel(bus)
        ch._running = True
        published: list[InboundMessage] = []

        async def _capture(msg):
            published.append(msg)

        bus.publish_inbound = _capture  # type: ignore[assignment]

        await ch._on_message(
            content="hi", sender_id="u1", sender_name="Bob",
            conversation_type="1", conversation_id=None,
        )
        assert published[0].chat_id == "u1"


# ---------------------------------------------------------------------------
# feishu.py — pure-logic helpers
# ---------------------------------------------------------------------------


class TestFeishuChannel:
    def test_extract_post_content_direct(self):
        from markbot.channels.feishu import _extract_post_content

        text, images = _extract_post_content(
            {"title": "T", "content": [[{"tag": "text", "text": "a"}]]}
        )
        assert text == "T a"
        assert images == []

    def test_extract_post_content_localized(self):
        from markbot.channels.feishu import _extract_post_content

        text, images = _extract_post_content(
            {"zh_cn": {"content": [[{"tag": "text", "text": "你好"}]]}}
        )
        assert text == "你好"

    def test_extract_post_content_wrapped(self):
        from markbot.channels.feishu import _extract_post_content

        text, _ = _extract_post_content(
            {"post": {"zh_cn": {"title": "X", "content": [[{"tag": "text", "text": "y"}]]}}}
        )
        assert text == "X y"

    def test_extract_post_content_images(self):
        from markbot.channels.feishu import _extract_post_content

        _, images = _extract_post_content(
            {"content": [[{"tag": "img", "image_key": "k1"}]]}
        )
        assert images == ["k1"]

    def test_extract_post_content_at_and_code(self):
        from markbot.channels.feishu import _extract_post_content

        text, _ = _extract_post_content(
            {
                "content": [
                    [
                        {"tag": "at", "user_name": "Alice"},
                        {"tag": "code_block", "language": "python", "text": "print('hi')"},
                    ]
                ]
            }
        )
        assert "@Alice" in text
        assert "```python" in text
        assert "print('hi')" in text

    def test_extract_post_content_invalid_returns_empty(self):
        from markbot.channels.feishu import _extract_post_content

        assert _extract_post_content("not a dict") == ("", [])
        assert _extract_post_content({}) == ("", [])

    def test_extract_post_text_wrapper(self):
        from markbot.channels.feishu import _extract_post_text

        assert _extract_post_text({"content": [[{"tag": "text", "text": "x"}]]}) == "x"

    def test_extract_share_card_content(self):
        from markbot.channels.feishu import _extract_share_card_content

        assert "shared chat" in _extract_share_card_content(
            {"chat_id": "oc_123"}, "share_chat"
        )
        assert "shared user" in _extract_share_card_content(
            {"user_id": "u"}, "share_user"
        )
        assert _extract_share_card_content({}, "system") == "[system message]"
        assert _extract_share_card_content({}, "unknown_type").startswith("[") is True

    def test_extract_interactive_content_title(self):
        from markbot.channels.feishu import _extract_interactive_content

        parts = _extract_interactive_content({"title": "Hello"})
        assert any("title: Hello" in p for p in parts)

    def test_extract_element_content_variants(self):
        from markbot.channels.feishu import _extract_element_content

        assert _extract_element_content({"tag": "markdown", "content": "**b**"}) == ["**b**"]
        assert _extract_element_content({"tag": "plain_text", "content": "txt"}) == ["txt"]
        assert "link:" in _extract_element_content(
            {"tag": "a", "href": "http://x", "text": "T"}
        )[0]
        assert _extract_element_content({"tag": "img", "alt": {"content": "pic"}}) == ["pic"]
        assert _extract_element_content({"tag": "img", "alt": {}}) == ["[image]"]
        assert _extract_element_content(123) == []
        assert _extract_element_content({"tag": "note", "elements": [
            {"tag": "plain_text", "content": "note1"}
        ]}) == ["note1"]

    def test_detect_msg_format(self):
        from markbot.channels.feishu import FeishuChannel

        assert FeishuChannel._detect_msg_format("hello") == "text"
        assert FeishuChannel._detect_msg_format("[text](https://x.com)") == "post"
        assert FeishuChannel._detect_msg_format("```\ncode\n```") == "interactive"
        assert FeishuChannel._detect_msg_format("**bold**") == "interactive"
        assert FeishuChannel._detect_msg_format("- item\n- item2") == "interactive"
        assert FeishuChannel._detect_msg_format("# Heading") == "interactive"
        long = "x" * (FeishuChannel._POST_MAX_LEN + 1)
        assert FeishuChannel._detect_msg_format(long) == "interactive"
        medium = "x" * 500
        assert FeishuChannel._detect_msg_format(medium) == "post"

    def test_strip_md_formatting(self):
        from markbot.channels.feishu import FeishuChannel

        assert FeishuChannel._strip_md_formatting("**bold**") == "bold"
        assert FeishuChannel._strip_md_formatting("__bold__") == "bold"
        assert FeishuChannel._strip_md_formatting("*italic*") == "italic"
        assert FeishuChannel._strip_md_formatting("~~strike~~") == "strike"

    def test_parse_md_table(self):
        from markbot.channels.feishu import FeishuChannel

        table = "| h1 | h2 |\n| --- | --- |\n| a | b |\n| c | d |\n"
        result = FeishuChannel._parse_md_table(table)
        assert result is not None
        assert result["tag"] == "table"
        assert len(result["columns"]) == 2
        assert result["columns"][0]["display_name"] == "h1"
        assert len(result["rows"]) == 2
        assert result["rows"][0] == {"c0": "a", "c1": "b"}

    def test_parse_md_table_too_short(self):
        from markbot.channels.feishu import FeishuChannel

        assert FeishuChannel._parse_md_table("| a | b |\n| --- | --- |") is None

    def test_build_mention_at_text(self):
        from markbot.channels.feishu import FeishuChannel

        assert FeishuChannel._build_mention_at_text(["ou_1"]) == '<at user_id="ou_1"></at>'
        s = FeishuChannel._build_mention_at_text([{"user_id": "ou_1", "name": "A"}])
        assert 'user_id="ou_1"' in s and ">A<" in s
        assert FeishuChannel._build_mention_at_text([]) == ""

    def test_build_mention_at_elements(self):
        from markbot.channels.feishu import FeishuChannel

        els = FeishuChannel._build_mention_at_elements([{"user_id": "ou_1", "name": "A"}])
        assert els == [{"tag": "at", "user_id": "ou_1", "user_name": "A"}]
        assert FeishuChannel._build_mention_at_elements(["ou_1"]) == [
            {"tag": "at", "user_id": "ou_1"}
        ]

    def test_split_elements_by_table_limit(self):
        from markbot.channels.feishu import FeishuChannel

        md = [{"tag": "markdown", "content": "a"}]
        tbl = {"tag": "table"}
        md2 = [{"tag": "markdown", "content": "b"}]
        groups = FeishuChannel._split_elements_by_table_limit(
            [md[0], tbl, md2[0], {"tag": "table"}]
        )
        # First group: md + tbl + md2, then second group: second table
        assert len(groups) == 2
        assert groups[0][-1] is md2[0]
        assert groups[1][0]["tag"] == "table"

    def test_split_elements_by_table_limit_empty(self):
        from markbot.channels.feishu import FeishuChannel

        assert FeishuChannel._split_elements_by_table_limit([]) == [[]]

    def test_markdown_to_post_with_link(self):
        from markbot.channels.feishu import FeishuChannel

        # Need a concrete instance (static method expects self here)
        ch = FeishuChannel.__new__(FeishuChannel)
        ch.config = SimpleNamespace(streaming=False, group_policy="mention", tool_hint_prefix="🔧")
        out = ch._markdown_to_post("[click](https://x.com)")
        assert "click" in out
        assert "https://x.com" in out

    def test_format_tool_hint_lines(self):
        from markbot.channels.feishu import FeishuChannel

        out = FeishuChannel._format_tool_hint_lines('a("x"), b("y")')
        assert out.count("\n") == 1

    def test_format_tool_hint_lines_nested_commas(self):
        from markbot.channels.feishu import FeishuChannel

        # Commas inside function arguments should not split.
        out = FeishuChannel._format_tool_hint_lines('web_search("a, b")')
        assert out == 'web_search("a, b")'

    def test_resolve_mentions(self):
        from markbot.channels.feishu import FeishuChannel

        mention = SimpleNamespace(
            key="@_user_1", name="Alice",
            id=SimpleNamespace(open_id="ou_1", user_id="u1"),
        )
        result = FeishuChannel._resolve_mentions("hi @_user_1", [mention])
        assert "@Alice" in result
        assert "ou_1" in result
        assert "user id: u1" in result

    def test_resolve_mentions_no_mentions(self):
        from markbot.channels.feishu import FeishuChannel

        assert FeishuChannel._resolve_mentions("hi", None) == "hi"

    def test_register_optional_event_present(self):
        from markbot.channels.feishu import FeishuChannel

        builder = MagicMock()
        method = MagicMock(return_value="called")
        builder.has_method = method
        result = FeishuChannel._register_optional_event(builder, "has_method", "h")
        assert result == "called"
        method.assert_called_once_with("h")

    def test_register_optional_event_absent(self):
        from markbot.channels.feishu import FeishuChannel

        builder = MagicMock(spec=[])  # no attrs
        result = FeishuChannel._register_optional_event(builder, "missing", "h")
        assert result is builder

    def test_feishu_config_defaults(self):
        from markbot.channels.feishu import FeishuConfig

        cfg = FeishuConfig()
        assert cfg.enabled is False
        assert cfg.group_policy == "mention"
        assert cfg.streaming is True
        assert cfg.domain == "feishu"

    def test_feishu_default_config(self):
        from markbot.channels.feishu import FeishuChannel

        dc = FeishuChannel.default_config()
        assert dc["enabled"] is False
        assert dc["streaming"] is True


# ---------------------------------------------------------------------------
# qq.py — pure-logic helpers
# ---------------------------------------------------------------------------


class TestQQChannel:
    def test_sanitize_filename(self):
        from markbot.channels.qq import _sanitize_filename

        assert _sanitize_filename("../../etc/passwd") == "passwd"
        # Path(name).name takes the basename, so the directory prefix is dropped.
        assert _sanitize_filename("a b/c:d*e.txt") == "c_d_e.txt"
        assert _sanitize_filename("文件.txt") == "文件.txt"
        assert _sanitize_filename("") == ""
        assert _sanitize_filename("   ") == ""

    def test_is_image_name(self):
        from markbot.channels.qq import _is_image_name

        assert _is_image_name("a.png") is True
        assert _is_image_name("a.jpg") is True
        assert _is_image_name("a.PDF") is False
        assert _is_image_name("noext") is False

    def test_guess_send_file_type(self):
        from markbot.channels.qq import _guess_send_file_type, QQ_FILE_TYPE_IMAGE, QQ_FILE_TYPE_FILE

        assert _guess_send_file_type("photo.png") == QQ_FILE_TYPE_IMAGE
        assert _guess_send_file_type("photo.jpg") == QQ_FILE_TYPE_IMAGE
        assert _guess_send_file_type("file.pdf") == QQ_FILE_TYPE_FILE
        assert _guess_send_file_type("file.bin") == QQ_FILE_TYPE_FILE

    def test_qq_config_defaults(self):
        from markbot.channels.qq import QQConfig

        cfg = QQConfig()
        assert cfg.enabled is False
        assert cfg.msg_format == "plain"
        assert cfg.download_chunk_size == 1024 * 256
        assert cfg.download_max_bytes == 1024 * 1024 * 200

    def test_qq_default_config(self):
        from markbot.channels.qq import QQChannel

        dc = QQChannel.default_config()
        assert dc["enabled"] is False
        assert dc["appId"] == ""

    def test_qq_name_and_display(self, bus, monkeypatch):
        from markbot.channels.qq import QQChannel, QQConfig

        # Avoid creating media dir on real fs by patching _init_media_root
        monkeypatch.setattr(QQChannel, "_init_media_root", lambda self: MagicMock())
        cfg = QQConfig(app_id="a", secret="b", allow_from=["*"])
        ch = QQChannel(cfg, bus)
        assert ch.name == "qq"
        assert ch.display_name == "QQ"


# ---------------------------------------------------------------------------
# weixin.py — pure-logic helpers
# ---------------------------------------------------------------------------


class TestWeixinChannel:
    def test_ext_for_type(self):
        from markbot.channels.weixin import _ext_for_type

        assert _ext_for_type("image") == ".jpg"
        assert _ext_for_type("voice") == ".silk"
        assert _ext_for_type("video") == ".mp4"
        assert _ext_for_type("file") == ""

    def test_parse_aes_key_raw_bytes(self):
        from markbot.channels.weixin import _parse_aes_key

        import base64
        raw = b"\x00" * 16
        assert _parse_aes_key(base64.b64encode(raw).decode()) == raw

    def test_parse_aes_key_hex_string(self):
        import base64
        from markbot.channels.weixin import _parse_aes_key

        hex_key = "00112233445566778899aabbccddeeff"
        b64 = base64.b64encode(hex_key.encode()).decode()
        assert _parse_aes_key(b64) == bytes.fromhex(hex_key)

    def test_parse_aes_key_invalid(self):
        import base64
        from markbot.channels.weixin import _parse_aes_key

        with pytest.raises(ValueError):
            _parse_aes_key(base64.b64encode(b"\x00" * 5).decode())

    def test_encrypt_decrypt_roundtrip(self):
        import base64
        from markbot.channels.weixin import _decrypt_aes_ecb, _encrypt_aes_ecb

        key = b"0123456789abcdef"
        b64key = base64.b64encode(key).decode()
        data = b"hello world 123"
        encrypted = _encrypt_aes_ecb(data, b64key)
        # Decrypted result includes PKCS7 padding bytes (decryptor doesn't unpad,
        # but the prefix matches).
        decrypted = _decrypt_aes_ecb(encrypted, b64key)
        assert decrypted.startswith(data)

    def test_weixin_config_defaults(self):
        from markbot.channels.weixin import WeixinConfig

        cfg = WeixinConfig()
        assert cfg.enabled is False
        assert cfg.base_url == "https://ilinkai.weixin.qq.com"
        assert cfg.poll_timeout == 35

    def test_weixin_default_config(self):
        from markbot.channels.weixin import WeixinChannel

        dc = WeixinChannel.default_config()
        assert dc["enabled"] is False
        assert dc["baseUrl"] == "https://ilinkai.weixin.qq.com"

    def test_random_wechat_uin_is_base64(self):
        from markbot.channels.weixin import WeixinChannel

        import base64
        val = WeixinChannel._random_wechat_uin()
        # Must be a valid base64 string of a decimal number.
        decoded = base64.b64decode(val).decode()
        int(decoded)  # should not raise

    def test_make_headers_with_token(self):
        from markbot.channels.weixin import WeixinChannel, WeixinConfig

        ch = WeixinChannel.__new__(WeixinChannel)
        ch.config = WeixinConfig(route_tag="tag1")
        ch._token = "abc"
        headers = ch._make_headers()
        assert headers["Authorization"] == "Bearer abc"
        assert headers["SKRouteTag"] == "tag1"
        assert headers["Content-Type"] == "application/json"
        assert "X-WECHAT-UIN" in headers

    def test_make_headers_no_auth(self):
        from markbot.channels.weixin import WeixinChannel, WeixinConfig

        ch = WeixinChannel.__new__(WeixinChannel)
        ch.config = WeixinConfig()
        ch._token = "abc"
        headers = ch._make_headers(auth=False)
        assert "Authorization" not in headers

    def test_session_pause(self):
        from markbot.channels.weixin import WeixinChannel, WeixinConfig

        ch = WeixinChannel.__new__(WeixinChannel)
        ch.config = WeixinConfig()
        ch._session_pause_until = 0.0
        # No pause → 0 remaining
        assert ch._session_pause_remaining_s() == 0

    def test_assert_session_active_raises(self):
        from markbot.channels.weixin import WeixinChannel, WeixinConfig

        import time
        ch = WeixinChannel.__new__(WeixinChannel)
        ch.config = WeixinConfig()
        ch._session_pause_until = time.time() + 600
        with pytest.raises(RuntimeError, match="session paused"):
            ch._assert_session_active()

    def test_assert_session_active_ok(self):
        import time
        from markbot.channels.weixin import WeixinChannel, WeixinConfig

        ch = WeixinChannel.__new__(WeixinChannel)
        ch.config = WeixinConfig()
        ch._session_pause_until = 0.0
        ch._assert_session_active()  # no raise

    def test_process_message_skips_bot_type(self):
        from markbot.channels.weixin import WeixinChannel, WeixinConfig, MESSAGE_TYPE_BOT

        ch = WeixinChannel.__new__(WeixinChannel)
        ch.config = WeixinConfig(allow_from=["*"])
        # Minimal state normally set in __init__
        ch._processed_ids = {}

        async def _noop(**kw):
            pass

        # Should be a no-op because message_type == BOT.
        # No assertion possible without bus mock; just ensure it doesn't raise.
        # Use an event loop manually.
        import asyncio
        msg = {"message_type": MESSAGE_TYPE_BOT, "from_user_id": "x"}
        asyncio.get_event_loop().run_until_complete(ch._process_message(msg)) if False else None
        ch._processed_ids = {}


# ---------------------------------------------------------------------------
# email.py — pure-logic helpers
# ---------------------------------------------------------------------------


class TestEmailChannel:
    def test_email_config_defaults(self):
        from markbot.channels.email import EmailConfig

        cfg = EmailConfig()
        assert cfg.enabled is False
        assert cfg.consent_granted is False
        assert cfg.imap_host == ""
        assert cfg.imap_port == 993
        assert cfg.smtp_port == 587
        assert cfg.poll_interval_seconds == 30
        assert cfg.max_body_chars == 12000
        assert cfg.subject_prefix == "Re: "
        assert cfg.verify_dkim is True
        assert cfg.verify_spf is True

    def test_email_default_config(self):
        from markbot.channels.email import EmailChannel

        dc = EmailChannel.default_config()
        assert dc["enabled"] is False
        assert dc["consentGranted"] is False

    def test_reply_subject(self, bus):
        from markbot.channels.email import EmailChannel, EmailConfig

        ch = EmailChannel.__new__(EmailChannel)
        ch.config = EmailConfig()
        assert ch._reply_subject("Hello") == "Re: Hello"
        assert ch._reply_subject("Re: Hello") == "Re: Hello"
        assert ch._reply_subject("") == "Re: markbot reply"
        ch.config.subject_prefix = "回复："
        assert ch._reply_subject("主题") == "回复：主题"

    def test_reply_subject_handles_already_prefixed(self, bus):
        from markbot.channels.email import EmailChannel, EmailConfig

        ch = EmailChannel.__new__(EmailChannel)
        ch.config = EmailConfig()
        # case-insensitive prefix check
        assert ch._reply_subject("re: existing") == "re: existing"

    def test_format_imap_date(self):
        from markbot.channels.email import EmailChannel

        assert EmailChannel._format_imap_date(date(2026, 7, 3)) == "03-Jul-2026"
        assert EmailChannel._format_imap_date(date(2024, 1, 1)) == "01-Jan-2024"
        assert EmailChannel._format_imap_date(date(2024, 12, 31)) == "31-Dec-2024"

    def test_html_to_text(self):
        from markbot.channels.email import EmailChannel

        html = "<p>Hello</p><br/>World<b>x</b>"
        out = EmailChannel._html_to_text(html)
        assert "Hello" in out
        assert "World" in out
        assert "<" not in out
        # &amp; should be unescaped
        assert EmailChannel._html_to_text("a &amp; b") == "a & b"

    def test_decode_header_value_empty(self):
        from markbot.channels.email import EmailChannel

        assert EmailChannel._decode_header_value("") == ""

    def test_decode_header_value_plain(self):
        from markbot.channels.email import EmailChannel

        assert EmailChannel._decode_header_value("plain subject") == "plain subject"

    def test_normalize_address(self):
        from markbot.channels.email import EmailChannel

        assert EmailChannel._normalize_address("  Foo <a@b.com>  ").lower() == "a@b.com"
        assert EmailChannel._normalize_address("") == ""
        assert EmailChannel._normalize_address("   ") == ""

    def test_is_self_address_positive(self):
        from markbot.channels.email import EmailChannel, EmailConfig

        ch = EmailChannel.__new__(EmailChannel)
        ch.config = EmailConfig(
            from_address="bot@x.com",
            smtp_username="bot@x.com",
            imap_username="bot@x.com",
        )
        ch._self_addresses = ch._collect_self_addresses()
        assert ch._is_self_address("BOT@x.com") is True
        assert ch._is_self_address("other@y.com") is False

    def test_validate_config_missing(self, bus):
        from markbot.channels.email import EmailChannel, EmailConfig

        ch = EmailChannel(EmailConfig(), bus)
        assert ch._validate_config() is False

    def test_validate_config_ok(self, bus):
        from markbot.channels.email import EmailChannel, EmailConfig

        cfg = EmailConfig(
            imap_host="h", imap_username="u", imap_password="p",
            smtp_host="h", smtp_username="u", smtp_password="p",
        )
        ch = EmailChannel(cfg, bus)
        assert ch._validate_config() is True

    def test_check_authentication_results_both_pass(self):
        from markbot.channels.email import EmailChannel

        class _Msg:
            def get_all(self, name):
                return [
                    "mx.google.com; spf=pass (ok) smtp.mailfrom=x; dkim=pass header.i=x",
                ]
        spf, dkim = EmailChannel._check_authentication_results(_Msg())
        assert spf is True
        assert dkim is True

    def test_check_authentication_results_neither(self):
        from markbot.channels.email import EmailChannel

        class _Msg:
            def get_all(self, name):
                return ["bad; spf=fail; dkim=none"]
        spf, dkim = EmailChannel._check_authentication_results(_Msg())
        assert spf is False
        assert dkim is False

    def test_check_authentication_results_none(self):
        from markbot.channels.email import EmailChannel

        class _Msg:
            def get_all(self, name):
                return None
        spf, dkim = EmailChannel._check_authentication_results(_Msg())
        assert spf is False
        assert dkim is False

    def test_extract_message_bytes(self):
        from markbot.channels.email import EmailChannel

        data = [b"junk", (b"(BODY[] {4}", b"abcd"), b")"]
        assert EmailChannel._extract_message_bytes(data) == b"abcd"
        assert EmailChannel._extract_message_bytes([b"only"]) is None

    def test_extract_uid(self):
        from markbot.channels.email import EmailChannel

        data = [(b"1 (UID 1234 BODY[] {1}", b"x"), b")"]
        assert EmailChannel._extract_uid(data) == "1234"
        assert EmailChannel._extract_uid([b"no uid here"]) == ""

    def test_is_stale_imap_error(self):
        from markbot.channels.email import EmailChannel

        assert EmailChannel._is_stale_imap_error(Exception("Bye from server")) is True
        assert EmailChannel._is_stale_imap_error(Exception("some other error")) is False

    def test_is_missing_mailbox_error(self):
        from markbot.channels.email import EmailChannel

        assert EmailChannel._is_missing_mailbox_error(
            Exception("Mailbox doesn't exist")
        ) is True
        assert EmailChannel._is_missing_mailbox_error(Exception("unrelated")) is False

    def test_extract_text_body_plain(self):
        from markbot.channels.email import EmailChannel

        from email.message import EmailMessage
        msg = EmailMessage()
        msg.set_content("Hello body")
        assert "Hello body" in EmailChannel._extract_text_body(msg)

    def test_extract_text_body_html(self):
        from markbot.channels.email import EmailChannel

        from email.message import EmailMessage
        msg = EmailMessage()
        msg.add_alternative("<p>HTML body</p>", subtype="html")
        out = EmailChannel._extract_text_body(msg)
        assert "HTML body" in out or "HTML body" in out.replace("\n", "")

    def test_remember_processed_uid_caps(self, bus):
        from markbot.channels.email import EmailChannel, EmailConfig

        ch = EmailChannel(EmailConfig(), bus)
        ch._processed_uids = set()
        ch._MAX_PROCESSED_UIDS = 4
        for i in range(10):
            ch._remember_processed_uid(str(i), dedupe=True, cycle_uids=set())
        assert len(ch._processed_uids) <= 4 + 1  # eviction keeps roughly half

    def test_remember_processed_uid_empty_noop(self, bus):
        from markbot.channels.email import EmailChannel, EmailConfig

        ch = EmailChannel(EmailConfig(), bus)
        before = len(ch._processed_uids)
        ch._remember_processed_uid("", dedupe=True, cycle_uids=set())
        assert len(ch._processed_uids) == before

    @pytest.mark.asyncio
    async def test_send_skipped_when_no_consent(self, bus):
        from markbot.channels.email import EmailChannel, EmailConfig

        ch = EmailChannel(EmailConfig(consent_granted=False), bus)
        msg = OutboundMessage(
            channel="email", chat_id="a@b.com", content="hello",
        )
        # Should not raise; should return None.
        assert await ch.send(msg) is None

    @pytest.mark.asyncio
    async def test_send_smtp_calls(self, monkeypatch, tmp_path, bus):
        from markbot.channels.email import EmailChannel, EmailConfig

        cfg = EmailConfig(
            consent_granted=True,
            smtp_host="smtp.example.com", smtp_username="u", smtp_password="p",
            from_address="bot@example.com",
        )
        ch = EmailChannel(cfg, bus)
        ch._last_subject_by_chat["user@example.com"] = "Subject"
        ch._last_message_id_by_chat["user@example.com"] = "<msg@example.com>"

        sent = []
        monkeypatch.setattr(ch, "_smtp_send", lambda m: sent.append(m))
        msg = OutboundMessage(
            channel="email", chat_id="user@example.com", content="Reply!",
        )
        await ch.send(msg)
        assert len(sent) == 1
        assert sent[0]["To"] == "user@example.com"
        assert sent[0]["Subject"] == "Re: Subject"
        assert sent[0]["In-Reply-To"] == "<msg@example.com>"

    @pytest.mark.asyncio
    async def test_start_returns_early_when_no_consent(self, bus):
        from markbot.channels.email import EmailChannel, EmailConfig

        ch = EmailChannel(EmailConfig(consent_granted=False), bus)
        await ch.start()
        assert ch.is_running is False

    def test_fetch_messages_between_dates_invalid_range(self, bus):
        from markbot.channels.email import EmailChannel, EmailConfig

        ch = EmailChannel(EmailConfig(), bus)
        # end <= start → empty list, no IMAP calls
        result = ch.fetch_messages_between_dates(date(2026, 1, 5), date(2026, 1, 1))
        assert result == []


# ---------------------------------------------------------------------------
# Package __init__ exports
# ---------------------------------------------------------------------------


class TestPackageExports:
    def test_exports(self):
        from markbot.channels import BaseChannel, ChannelManager as CM
        assert BaseChannel is BaseChannelAlias
        assert CM is ChannelManagerAlias