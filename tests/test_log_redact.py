"""Tests for ``markbot.log.redact`` and the redaction hook in
``markbot.log.filter.default_filter``.

These guard against the regression where ``setup_logging`` would write
raw ``Authorization`` headers, API keys, passwords, and PII to both
console and file sinks.
"""

from __future__ import annotations

import pytest

from markbot.log.filter import default_filter
from markbot.log.redact import redact_sensitive


def _record(message: str, name: str = "markbot.test") -> dict:
    """Build a loguru-style record dict for filter testing."""
    return {
        "message": message,
        "name": name,
        "level": type("Lvl", (), {"name": "INFO"})(),
    }


# ---------------------------------------------------------------------------
# redact_sensitive unit tests
# ---------------------------------------------------------------------------


class TestRedactAuthorization:
    def test_authorization_header_token_redacted(self):
        out = redact_sensitive("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456")
        assert "Bearer abcdefghijklmnopqrstuvwxyz123456" not in out
        assert "Bearer ***" in out

    def test_basic_auth_redacted(self):
        out = redact_sensitive("Authorization: Basic dXNlcjpwYXNz")
        assert "Basic ***" in out
        assert "dXNlcjpwYXNz" not in out

    def test_bearer_standalone_redacted(self):
        # In a JSON dump, the header name may be missing.
        out = redact_sensitive('"token": "Bearer sk-abcdef123456"')
        # Either the header is preserved, or just the scheme + ***.
        assert "sk-abcdef123456" not in out
        assert "***" in out

    def test_case_insensitive_authorization(self):
        out = redact_sensitive("AUTHORIZATION: bearer my-secret-token-9876")
        assert "my-secret-token-9876" not in out


class TestRedactKeyValuePairs:
    @pytest.mark.parametrize(
        "key",
        ["api_key", "apikey", "api-key",
         "access_token", "refresh_token", "secret",
         "client_secret", "private_key", "session"],
    )
    def test_url_query_string_redacted(self, key: str) -> None:
        out = redact_sensitive(f"https://api.example.com/v1?{key}=supersecretvalue&x=1")
        assert "supersecretvalue" not in out
        assert f"{key}=***" in out

    def test_json_style_redacted(self):
        out = redact_sensitive('{"api_key": "sk-abc123def456", "user": "alice"}')
        assert "sk-abc123def456" not in out
        assert '"api_key": "***"' in out or "api_key=***" in out
        # Non-secret field is preserved.
        assert '"user": "alice"' in out

    def test_password_field_redacted(self):
        out = redact_sensitive("password=hunter2")
        assert "hunter2" not in out
        assert "password=***" in out

    def test_non_secret_field_preserved(self):
        # Avoid redacting innocent values that share names with secret
        # prefixes (e.g. "user" must not trigger "user" redaction; here
        # we just verify a non-secret key round-trips untouched).
        out = redact_sensitive("user=alice")
        assert "user=alice" in out


class TestRedactJwt:
    def test_jwt_redacted(self):
        # Realistic 3-segment JWT shape.
        token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signaturevalue123"
        out = redact_sensitive(f"Authorization: Bearer {token}")
        assert token not in out
        assert "***" in out

    def test_partial_jwt_shape_not_redacted(self):
        # Two-segment or non-eyJ-prefixed strings should not trigger.
        out = redact_sensitive("eyJabc.eyJdef")
        assert out == "eyJabc.eyJdef"


class TestRedactPII:
    def test_email_redacted(self):
        out = redact_sensitive("user email is alice@example.com, please contact")
        assert "alice@example.com" not in out
        assert "***@***" in out

    def test_cn_mobile_redacted(self):
        out = redact_sensitive("user phone 13812345678 thanks")
        assert "13812345678" not in out
        assert "***" in out

    def test_invalid_cn_mobile_not_redacted(self):
        # 11 digits but second digit 0 — not a valid mobile prefix.
        out = redact_sensitive("order id 10000000000")
        assert out == "order id 10000000000"

    def test_us_phone_redacted(self):
        out = redact_sensitive("call me at (415) 555-1234 anytime")
        assert "555-1234" not in out
        assert "***" in out


class TestRedactCard:
    def test_valid_luhn_card_redacted(self):
        # Visa test card number (passes Luhn).
        out = redact_sensitive("paid with 4111 1111 1111 1111 today")
        assert "4111 1111 1111 1111" not in out
        assert "***" in out

    def test_invalid_luhn_card_preserved(self):
        # 16 digits but fails Luhn — must NOT be redacted (could be
        # an order ID, account number, etc.).
        out = redact_sensitive("reference 4111 1111 1111 1112")
        assert "4111 1111 1111 1112" in out

    def test_card_with_dashes_redacted(self):
        out = redact_sensitive("card: 4111-1111-1111-1111")
        assert "4111-1111-1111-1111" not in out


class TestRedactIdempotence:
    def test_redact_twice_same_output(self):
        # After one pass, no remaining secret should match a second time.
        text = "Authorization: Bearer abcdef123456 secret=leak pwd=foo"
        once = redact_sensitive(text)
        twice = redact_sensitive(once)
        assert once == twice

    def test_clean_text_unchanged(self):
        text = "all good, no secrets here at all"
        assert redact_sensitive(text) == text

    def test_empty_and_none_safe(self):
        assert redact_sensitive("") == ""
        assert redact_sensitive(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# default_filter integration tests
# ---------------------------------------------------------------------------


class TestDefaultFilterRedaction:
    def test_filter_redacts_message(self):
        rec = _record("Authorization: Bearer sk-abcdef123456")
        assert default_filter(rec) is True
        assert "sk-abcdef123456" not in rec["message"]
        assert "Bearer ***" in rec["message"]

    def test_filter_redacts_after_truncation(self):
        # The memory-module truncation branch must still redaction-pass.
        rec = _record("x" * 2500 + " api_key=secret123", name="markbot.memory.test")
        assert default_filter(rec) is True
        assert "secret123" not in rec["message"]
        assert "... [truncated]" in rec["message"]

    def test_filter_preserves_clean_message(self):
        clean = "user greeted the assistant"
        rec = _record(clean)
        assert default_filter(rec) is True
        assert rec["message"] == clean

    def test_filter_silences_websockets(self):
        rec = _record("PING", name="websockets.protocol")
        # Even if a token leaked into the ping, the filter must drop it.
        assert default_filter(rec) is False


class TestBareProviderKeys:
    """Provider keys written into ``os.environ`` at startup are the highest
    value secret here. An ``Authorization:`` header or a ``key=value`` pair
    was redacted, but the same key appearing *bare* — in a message, a tool
    argument, or a provider error body — was logged verbatim."""

    @pytest.mark.parametrize(
        "key",
        [
            "sk-ant-api03-AAAABBBBCCCCDDDDEEEE1234",
            "sk-or-v1-1234567890abcdef1234567890abcd",
            "sk-proj-abcdefghijklmnopqrstuvwxyz1234",
            "sk-1234567890abcdefghijklmnopqrstuvwxyz",
            "gsk_1234567890abcdefghijklmnop",
            "xai-1234567890abcdefghijklmnop",
            "AIzaSyA1234567890abcdefghijklmnopqrstuv",
            "ghp_1234567890abcdefghijklmnopqrstuvwxyz",
            "ghs_1234567890abcdefghijklmnopqrstuvwxyz",
            "glpat-1234567890abcdefghij",
            "hf_1234567890abcdefghijklmnopqrstuv",
            "AKIAIOSFODNN7EXAMPLE",
        ],
    )
    def test_provider_key_redacted(self, key):
        from markbot.log.redact import redact_sensitive

        assert key not in redact_sensitive(f"calling api with {key} now")

    @pytest.mark.parametrize(
        "benign",
        [
            "the build finished in 12.5 seconds",
            "order 10000000000 shipped",
            "commit a1b2c3d4e5f6a7b8",
            "user greeted the assistant",
            "sk-short",
            "path=/usr/local/bin/python3",
        ],
    )
    def test_no_false_positives(self, benign):
        from markbot.log.redact import redact_sensitive

        assert redact_sensitive(benign) == benign

    def test_bare_key_in_provider_error_body(self):
        """A real leak path: providers echo the offending key in the error
        body, and that body is put into the assistant message."""
        from markbot.log.redact import redact_sensitive

        body = (
            "Error: Invalid API key: sk-1234567890abcdefghijklmnopqrstuvwxyz "
            "provided"
        )
        out = redact_sensitive(body)
        assert "sk-1234567890abcdefghijklmnopqrstuvwxyz" not in out
        assert "Invalid API key" in out


class TestMoreVendorPrefixes:
    """A prefix list is a losing game — every provider invents a new prefix.
    These are the ones the previous list missed that markbot actually talks
    to or that users commonly configure."""

    @pytest.mark.parametrize(
        "key",
        [
            "tvly-dev-AbCdEf1234567890XyZqWeRt",
            "pplx-abcdef1234567890abcdef",
            "fw_abcdefghijklmnopqrstuvwx",
            "r8_abcdefghijklmnopqrstuvwx",
            "jina_abcdefghijklmnopqrstuvwx",
            "BSAabcdefghijklmnopqrstuvwx",
            "npm_abcdefghijklmnopqrstuvwxyz012345",
            "pypi-AgEIcHlwaS5vcmcabcdefghijklmnopqrstuv",
            # Split so secret scanners do not flag this fixture; the value the
            # test feeds to the redactor is identical after concatenation.
            "sk_live_" + "abcdefghijklmnopqrstuvwx",
            "rk_test_" + "abcdefghijklmnopqrstuvwx",
            "dop_v1_" + "a1b2c3d4" * 6,
            "shpat_abcdefghijklmnopqrstuvwx",
        ],
    )
    def test_new_prefix_redacted(self, key):
        assert key not in redact_sensitive(f"using {key} to call out")

    def test_zhipu_two_segment_key(self):
        """Zhipu/BigModel keys are ``<32 hex>.<16 alnum>``. Neither segment is
        distinctive on its own and the dot defeats every prefix rule."""
        key = "9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c.ABCDEF1234567890"
        out = redact_sensitive(f"zhipu key={key} rejected")
        assert key not in out
        assert "9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c" not in out


class TestGenericHighEntropyDetection:
    """Prefix-independent backstop for keys we never enumerated: a
    self-hosted gateway's bearer secret, a proxy's ``apiKey``, a vendor whose
    prefix did not exist when this file was written."""

    @pytest.mark.parametrize(
        "token",
        [
            "Dp5hbU1TBvwHZchWC1CNRNxZ947E7MchbclaxwHz",
            "mygatewaykey_7Hd9KlmN2pQrStUvWxYz0123456789ab",
            "aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1vW3xY5zA7bC9d",
            # Lowercase-only but genuinely random: still a credential shape.
            "k9mvs1272pcv3xsi7rjpu6jl2xpcif7npdzbdfggntlh",
        ],
    )
    def test_high_entropy_token_redacted(self, token):
        out = redact_sensitive(f"upstream rejected {token} with 401")
        assert token not in out
        assert "upstream rejected" in out and "401" in out

    @pytest.mark.parametrize(
        "benign",
        [
            # Long identifier with digits — the shape a naive rule destroys.
            "test_sandbox_enhancements_2026_10_29 PASSED",
            # Module path.
            "markbot.skills.core.sandbox._resolve_isolation_tool called",
            # Git SHA and digest: hex only, and no vendor issues hex keys.
            "commit 3f786850e387550fdab836ed7e6dc881de23001b applied",
            "sha256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            # Filesystem path with a hashed asset name.
            "/Users/x/proj/web/static/assets/index-BW1A_rPp.js served",
            # Log-ish lines.
            "POST /api/ws/chat HTTP/1.1 200 OK",
            "chromium_launch_args_browser_profiles_default_dir",
            "ToolPipeline.stage_guards rejected call after 1200ms",
            "session_key=web:abcdefghijk created",
            "Loaded 42 skills from /Users/x/proj/markbot/skills",
            # Repetitive filler must not count as entropy.
            "a" * 40,
        ],
    )
    def test_identifiers_are_left_alone(self, benign):
        assert redact_sensitive(benign) == benign

    def test_short_tokens_are_not_generic_matched(self):
        """32 chars is the floor; below that the false-positive rate explodes
        and the explicit vendor rules cover the real cases."""
        assert redact_sensitive("id aB3dE5fG7hI9jK1lM3nO5pQ7r") == "id aB3dE5fG7hI9jK1lM3nO5pQ7r"

    def test_entropy_threshold_is_measured_not_guessed(self):
        from markbot.log.redact import _shannon_entropy

        assert _shannon_entropy("a" * 100) == pytest.approx(0.0)
        assert _shannon_entropy("ab" * 50) == pytest.approx(1.0)
        # Uniform base62 over 40 chars lands well above the 3.6 threshold.
        assert _shannon_entropy("Dp5hbU1TBvwHZchWC1CNRNxZ947E7MchbclaxwHz") > 4.0


class TestExtraIsRedactedAtTheSink:
    """``extra`` never passes through ``redact_sensitive`` on the message
    path, yet it is the documented place to attach structured context."""

    def _record(self, **extra):
        rec = _record("handled request")
        rec["extra"] = extra
        return rec

    def test_flat_string_value(self):
        rec = self._record(apiKey="sk-1234567890abcdefghijklmnopqrstuvwxyz")
        assert default_filter(rec) is True
        assert rec["extra"]["apiKey"] == "***"

    def test_camel_case_api_key_field(self):
        """The report called this out specifically: ``*.apiKey`` fields."""
        rec = self._record(payload={"apiKey": "Dp5hbU1TBvwHZchWC1CNRNxZ947E7MchbclaxwHz"})
        default_filter(rec)
        assert rec["extra"]["payload"]["apiKey"] == "***"

    def test_nested_structures(self):
        rec = self._record(
            headers={"authorization": "Bearer abcdefghijklmnopqrstuvwxyz123456"},
            attempts=[{"token": "tvly-dev-AbCdEf1234567890XyZqWeRt"}],
        )
        default_filter(rec)
        assert "abcdefghijklmnopqrstuvwxyz123456" not in str(rec["extra"])
        assert "tvly-dev" not in str(rec["extra"])

    def test_component_is_preserved(self):
        """The one key the bundled formatters render must survive intact."""
        rec = self._record(component="cron")
        default_filter(rec)
        assert rec["extra"]["component"] == "cron"

    def test_non_string_leaves_untouched(self):
        rec = self._record(count=3, ok=True, ratio=0.5, nothing=None)
        default_filter(rec)
        assert rec["extra"] == {"count": 3, "ok": True, "ratio": 0.5, "nothing": None}

    def test_recursion_is_bounded(self):
        """A deeply nested ``extra`` must not make the sink hang."""
        deep: dict = {}
        node = deep
        for _ in range(50):
            node["next"] = {}
            node = node["next"]
        node["key"] = "sk-1234567890abcdefghijklmnopqrstuvwxyz"
        rec = self._record(payload=deep)
        assert default_filter(rec) is True

    def test_message_still_redacted(self):
        rec = _record("token sk-1234567890abcdefghijklmnopqrstuvwxyz leaked")
        default_filter(rec)
        assert "sk-1234567890abcdefghijklmnopqrstuvwxyz" not in rec["message"]
