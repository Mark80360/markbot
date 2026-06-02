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
