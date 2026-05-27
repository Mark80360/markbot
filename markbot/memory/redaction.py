"""Sensitive text redaction for memory operations."""

from __future__ import annotations

import re

_SENSITIVE_PATTERNS: list[tuple[str, str]] = [
    (r'(api[_-]?key\s*[:=]\s*)["\']?[\w\-]{8,}["\']?', r'\1[REDACTED]'),
    (r'(token\s*[:=]\s*)["\']?[\w\-\.]{8,}["\']?', r'\1[REDACTED]'),
    (r'(secret\s*[:=]\s*)["\']?[\w\-]{8,}["\']?', r'\1[REDACTED]'),
    (r'(password\s*[:=]\s*)["\']?[^\s"\']{4,}["\']?', r'\1[REDACTED]'),
    (r'(credential\s*[:=]\s*)["\']?[\w\-]{8,}["\']?', r'\1[REDACTED]'),
    (r'(Bearer\s+)[\w\-\.]{8,}', r'\1[REDACTED]'),
    (r'(Authorization:\s*Bearer\s+)(\S+)', r'\1[REDACTED]'),
    (r'(sk-)[\w\-]{20,}', r'\1[REDACTED]'),
    (r'(sk_live_[\w]{10,})', r'sk_live_[REDACTED]'),
    (r'(sk_test_[\w]{10,})', r'sk_test_[REDACTED]'),
    (r'(ghp_[\w]{30,})', r'ghp_[REDACTED]'),
    (r'(gho_[\w]{30,})', r'gho_[REDACTED]'),
    (r'(github_pat_[\w_]{20,})', r'github_pat_[REDACTED]'),
    (r'(AKIA[\w]{16})', r'AKIA[REDACTED]'),
    (r'(xox[bpas]-[\w\-]{20,})', r'\1[REDACTED]'),
    (r'(AIza[\w_-]{30,})', r'AIza[REDACTED]'),
    (r'(hf_[\w]{10,})', r'hf_[REDACTED]'),
    (r'-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----', r'[REDACTED PRIVATE KEY]'),
    (r'((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:]+:)([^@]+)(@)', r'\1[REDACTED]\3'),
    (r'(eyJ[\w_-]{10,}(?:\.[\w_=-]{4,}){0,2})', r'[REDACTED_JWT]'),
]


def redact_sensitive_text(text: str) -> str:
    """Redact API keys, tokens, passwords, and other secrets from text.

    Applied before sending conversation content to the summary LLM
    to prevent secrets from being baked into compressed summaries.

    Args:
        text: Text that may contain secrets.

    Returns:
        Text with secrets replaced by [REDACTED].
    """
    for pattern, replacement in _SENSITIVE_PATTERNS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


__all__ = ["redact_sensitive_text", "_SENSITIVE_PATTERNS"]