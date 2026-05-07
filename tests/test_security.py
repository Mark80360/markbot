"""Tests for markbot.utils.security — PII filtering and secret management."""

from markbot.utils.security import (
    CompositeSecretProvider,
    EnvSecretProvider,
    SecretProvider,
    is_private_ip,
    redact_pii,
    validate_url_ssrf,
)


class TestRedactPII:
    def test_redacts_openai_key(self):
        text = "Using key sk-abc123def456ghi789jkl012mno345"
        result = redact_pii(text)
        assert "sk-***REDACTED***" in result
        assert "abc123" not in result

    def test_redacts_openrouter_key(self):
        text = "Key: sk-or-v1-abc123def456ghi789jkl012"
        result = redact_pii(text)
        assert "sk-or-***REDACTED***" in result

    def test_redacts_aws_key(self):
        text = "AWS key AKIAIOSFODNN7EXAMPLE"
        result = redact_pii(text)
        assert "AKIA***REDACTED***" in result

    def test_redacts_email(self):
        text = "Contact user@example.com for details"
        result = redact_pii(text)
        assert "***EMAIL***" in result
        assert "user@example.com" not in result

    def test_redacts_ip(self):
        text = "Server at 192.168.1.100 is down"
        result = redact_pii(text)
        assert "***IP***" in result
        assert "192.168.1.100" not in result

    def test_redacts_bearer_token(self):
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.verylongtoken"
        result = redact_pii(text)
        assert "Bearer ***REDACTED***" in result

    def test_preserves_safe_text(self):
        text = "Hello world, this is a normal message"
        assert redact_pii(text) == text


class TestEnvSecretProvider:
    def test_get_and_set(self, monkeypatch):
        provider = EnvSecretProvider()
        monkeypatch.setenv("TEST_SECRET_KEY", "my-secret-value")
        assert provider.get("TEST_SECRET_KEY") == "my-secret-value"

    def test_get_missing(self):
        provider = EnvSecretProvider()
        assert provider.get("NONEXISTENT_KEY_12345") is None

    def test_set_and_delete(self, monkeypatch):
        provider = EnvSecretProvider()
        provider.set("TEST_SET_KEY", "value")
        assert provider.get("TEST_SET_KEY") == "value"
        assert provider.delete("TEST_SET_KEY") is True
        assert provider.get("TEST_SET_KEY") is None

    def test_delete_missing(self):
        provider = EnvSecretProvider()
        assert provider.delete("NONEXISTENT_KEY_12345") is False


class TestCompositeSecretProvider:
    def test_chains_providers(self, monkeypatch):
        p1 = EnvSecretProvider()
        p2 = EnvSecretProvider()
        composite = CompositeSecretProvider(p1, p2)

        monkeypatch.setenv("COMP_KEY_1", "from_p1")
        assert composite.get("COMP_KEY_1") == "from_p1"

    def test_falls_through(self, monkeypatch):
        p1 = EnvSecretProvider()
        p2 = EnvSecretProvider()
        composite = CompositeSecretProvider(p1, p2)

        monkeypatch.delenv("MISSING_COMP_KEY", raising=False)
        assert composite.get("MISSING_COMP_KEY") is None


class TestSSRFProtection:
    def test_private_ips(self):
        assert is_private_ip("10.0.0.1") is True
        assert is_private_ip("172.16.0.1") is True
        assert is_private_ip("192.168.1.1") is True
        assert is_private_ip("127.0.0.1") is True

    def test_public_ips(self):
        assert is_private_ip("8.8.8.8") is False
        assert is_private_ip("1.1.1.1") is False

    def test_validate_safe_url(self):
        assert validate_url_ssrf("https://api.example.com/v1") is None

    def test_validate_private_url(self):
        result = validate_url_ssrf("http://192.168.1.1/admin")
        assert result is not None
        assert "private" in result.lower()

    def test_validate_bad_scheme(self):
        result = validate_url_ssrf("ftp://example.com/file")
        assert result is not None
        assert "scheme" in result.lower()

    def test_allowed_internal_ip(self):
        result = validate_url_ssrf(
            "http://10.0.0.1/health",
            allowed_internal_ips=["10.0.0.1"],
        )
        assert result is None
