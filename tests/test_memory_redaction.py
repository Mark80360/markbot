import pytest
from markbot.memory.manager import redact_sensitive_text, _SENSITIVE_PATTERNS


class TestRedactSensitiveText:
    """Test sensitive text redaction."""

    def test_redact_api_key(self):
        text = 'api_key = "sk-abc123def456ghi789"'
        result = redact_sensitive_text(text)
        assert "[REDACTED]" in result
        assert "sk-abc123def456ghi789" not in result

    def test_redact_bearer_token(self):
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        result = redact_sensitive_text(text)
        assert "[REDACTED]" in result
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in result

    def test_redact_github_pat(self):
        text = "github_pat_11ABCDEF1234567890abcdefghijklmnopqrstuvwxABCDEFGHIJ"
        result = redact_sensitive_text(text)
        assert "[REDACTED]" in result

    def test_redact_aws_key(self):
        text = "AKIAIOSFODNN7EXAMPLE"
        result = redact_sensitive_text(text)
        assert "[REDACTED]" in result

    def test_no_redact_normal_text(self):
        text = "This is normal text without secrets"
        result = redact_sensitive_text(text)
        assert result == text

    def test_redact_multiple_secrets(self):
        text = 'api_key="sk-abc123" token="tok-xyz789"'
        result = redact_sensitive_text(text)
        assert result.count("[REDACTED]") == 2

    def test_sensitive_patterns_not_empty(self):
        assert len(_SENSITIVE_PATTERNS) > 0
        for pattern, replacement in _SENSITIVE_PATTERNS:
            assert isinstance(pattern, str)
            assert isinstance(replacement, str)