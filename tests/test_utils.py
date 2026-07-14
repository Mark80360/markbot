"""Tests for markbot.utils module (helpers, constants, network)."""


from markbot.utils.constants import (
    BINARY_EXTENSIONS,
    DANGEROUS_COMMAND_PATTERNS,
    IGNORE_DIRS,
    MAX_SEARCH_RESULTS,
)
from markbot.utils.helpers import (
    build_assistant_message,
    current_time_str,
    detect_image_mime,
    ensure_dir,
    normalize_timezone,
    safe_filename,
    shorten,
    strip_ansi,
    strip_think,
)


class TestShorten:
    def test_short_text_unchanged(self):
        assert shorten("hello") == "hello"

    def test_long_text_truncated(self):
        text = "a" * 200
        result = shorten(text, limit=120)
        assert len(result) == 120
        assert result.endswith("...")

    def test_whitespace_normalized(self):
        assert shorten("  hello   world  ") == "hello world"

    def test_custom_limit(self):
        result = shorten("abcdefghij", limit=5)
        assert result == "ab..."

    def test_exact_limit(self):
        text = "a" * 120
        assert shorten(text, limit=120) == text


class TestStripThink:
    def test_no_think_tag(self):
        assert strip_think("hello world") == "hello world"

    def test_complete_think_tag(self):
        result = strip_think("hello <thinksecret>internal</thinksecret> world")
        assert "hello" in result
        assert "world" in result

    def test_unclosed_think_tag(self):
        result = strip_think("hello <thinksecret>internal only")
        assert "hello" in result

    def test_none_input(self):
        assert strip_think(None) is None

    def test_empty_input(self):
        assert strip_think("") == ""

    def test_multiple_think_blocks(self):
        result = strip_think("a<thinksecret>1</thinksecret>b<thinksecret>2</thinksecret>c")
        assert "a" in result
        assert "b" in result
        assert "c" in result


class TestStripAnsi:
    def test_no_ansi(self):
        assert strip_ansi("hello") == "hello"

    def test_color_codes(self):
        text = "\x1b[31mred\x1b[0m text"
        assert strip_ansi(text) == "red text"

    def test_complex_ansi(self):
        text = "\x1b[1;32;40mbold green\x1b[0m"
        assert strip_ansi(text) == "bold green"


class TestDetectImageMime:
    def test_png(self):
        data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 10
        assert detect_image_mime(data) == "image/png"

    def test_jpeg(self):
        data = b"\xff\xd8\xff\xe0" + b"\x00" * 10
        assert detect_image_mime(data) == "image/jpeg"

    def test_gif87a(self):
        data = b"GIF87a" + b"\x00" * 10
        assert detect_image_mime(data) == "image/gif"

    def test_gif89a(self):
        data = b"GIF89a" + b"\x00" * 10
        assert detect_image_mime(data) == "image/gif"

    def test_webp(self):
        data = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 10
        assert detect_image_mime(data) == "image/webp"

    def test_unknown(self):
        assert detect_image_mime(b"hello world") is None

    def test_empty(self):
        assert detect_image_mime(b"") is None


class TestEnsureDir:
    def test_creates_dir(self, tmp_path):
        target = tmp_path / "new_dir" / "sub"
        result = ensure_dir(target)
        assert result.exists()
        assert result.is_dir()

    def test_existing_dir(self, tmp_path):
        result = ensure_dir(tmp_path)
        assert result == tmp_path


class TestSafeFilename:
    def test_normal_name(self):
        assert safe_filename("hello.txt") == "hello.txt"

    def test_unsafe_chars(self):
        result = safe_filename('file<:>"/\\|?*.txt')
        assert "<" not in result
        assert ">" not in result
        assert ":" not in result
        assert '"' not in result
        assert "\\" not in result
        assert "|" not in result
        assert "?" not in result
        assert "*" not in result

    def test_strips_whitespace(self):
        assert safe_filename("  hello  ") == "hello"


class TestNormalizeTimezone:
    def test_none_returns_utc(self):
        assert normalize_timezone(None) == "UTC"

    def test_empty_returns_utc(self):
        assert normalize_timezone("") == "UTC"

    def test_valid_iana_name(self):
        result = normalize_timezone("Asia/Shanghai")
        assert result == "Asia/Shanghai"

    def test_utc_offset_format(self):
        result = normalize_timezone("UTC+8")
        assert result == "Asia/Shanghai"

    def test_utc_zero(self):
        result = normalize_timezone("UTC+0")
        assert result == "UTC"

    def test_unknown_falls_back(self):
        result = normalize_timezone("Invalid/Zone")
        assert result == "UTC"


class TestCurrentTimeStr:
    def test_returns_string(self):
        result = current_time_str()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_contains_year(self):
        import datetime
        year = str(datetime.datetime.now().year)
        assert year in current_time_str()


class TestConstants:
    def test_ignore_dirs_not_empty(self):
        assert len(IGNORE_DIRS) > 0

    def test_ignore_dirs_contains_git(self):
        assert ".git" in IGNORE_DIRS

    def test_ignore_dirs_contains_node_modules(self):
        assert "node_modules" in IGNORE_DIRS

    def test_ignore_dirs_contains_pycache(self):
        assert "__pycache__" in IGNORE_DIRS

    def test_binary_extensions_not_empty(self):
        assert len(BINARY_EXTENSIONS) > 0

    def test_binary_extensions_contains_common(self):
        assert ".png" in BINARY_EXTENSIONS
        assert ".pdf" in BINARY_EXTENSIONS
        assert ".exe" in BINARY_EXTENSIONS

    def test_max_search_results_positive(self):
        assert MAX_SEARCH_RESULTS > 0

    def test_dangerous_patterns_not_empty(self):
        assert len(DANGEROUS_COMMAND_PATTERNS) > 0


class TestBuildAssistantMessage:
    """Regression tests for build_assistant_message.

    The contract: ``reasoning_content`` is **always** emitted on the message
    (empty string when absent) so thinking-mode providers (DeepSeek V4+,
    Kimi K2) can round-trip the field across turns. The previous
    implementation gated the field on ``is not None or == ""``, which
    evaluated False for ``None`` and produced the HTTP 400
    ``reasoning_content must be passed back`` error.
    """

    def test_reasoning_none_serialises_to_empty_string(self):
        # REGRESSION: was omitted entirely under the old condition.
        msg = build_assistant_message("answer", reasoning_content=None)
        assert msg["reasoning_content"] == ""

    def test_reasoning_empty_string_preserved(self):
        msg = build_assistant_message("answer", reasoning_content="")
        assert msg["reasoning_content"] == ""

    def test_reasoning_non_empty_preserved(self):
        thinking = "step 1: ... step 2: ..."
        msg = build_assistant_message("answer", reasoning_content=thinking)
        assert msg["reasoning_content"] == thinking

    def test_field_is_always_present_even_without_explicit_arg(self):
        msg = build_assistant_message("answer")
        assert "reasoning_content" in msg
        assert msg["reasoning_content"] == ""

    def test_content_normalised_to_empty_string_when_none(self):
        msg = build_assistant_message(None, reasoning_content=None)
        assert msg["content"] == ""
        assert msg["role"] == "assistant"
        assert msg["reasoning_content"] == ""

    def test_tool_calls_attached(self):
        tcs = [{"id": "1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]
        msg = build_assistant_message("answer", tool_calls=tcs, reasoning_content="th")
        assert msg["tool_calls"] == tcs
        assert msg["reasoning_content"] == "th"

    def test_thinking_blocks_attached(self):
        blocks = [{"type": "thinking", "text": "..."}]
        msg = build_assistant_message("answer", thinking_blocks=blocks, reasoning_content="th")
        assert msg["thinking_blocks"] == blocks
        assert msg["reasoning_content"] == "th"

    def test_no_tool_calls_key_when_none(self):
        msg = build_assistant_message("answer", reasoning_content=None)
        assert "tool_calls" not in msg

    def test_no_thinking_blocks_key_when_none(self):
        msg = build_assistant_message("answer", reasoning_content=None)
        assert "thinking_blocks" not in msg

    def test_round_trip_through_messages_list(self):
        # Simulate the agent loop: build, append, build again.
        msgs = []
        msgs.append(build_assistant_message("first", reasoning_content="thinking A"))
        msgs.append(build_assistant_message("second", reasoning_content="thinking B"))
        assert msgs[0]["reasoning_content"] == "thinking A"
        assert msgs[1]["reasoning_content"] == "thinking B"
        # Each message is independently well-formed.
        for m in msgs:
            assert m["role"] == "assistant"
            assert "reasoning_content" in m
