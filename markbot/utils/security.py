"""Security utilities — PII filtering, secret management, SSRF protection.

Provides pluggable secret backends and automatic PII redaction for logs.
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from typing import Any

from loguru import logger


_PATTERNS: list[tuple[str, str]] = [
    (r'sk-or-[a-zA-Z0-9\-]{20,}', 'sk-or-***REDACTED***'),
    (r'sk-[a-zA-Z0-9]{20,}', 'sk-***REDACTED***'),
    (r'key-[a-zA-Z0-9]{20,}', 'key-***REDACTED***'),
    (r'AKIA[0-9A-Z]{16}', 'AKIA***REDACTED***'),
    (r'(?:api[_-]?key|apikey|token|secret|password|auth)["\s]*[:=]\s*["\']?([a-zA-Z0-9_\-\.]{8,})["\']?', '***REDACTED***'),
    (r'(?:Bearer\s+)([a-zA-Z0-9_\-\.]{20,})', 'Bearer ***REDACTED***'),
    (r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', '***IP***'),
    (r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', '***EMAIL***'),
]

_COMPILED = [(re.compile(p, re.IGNORECASE), r) for p, r in _PATTERNS]


def redact_pii(text: str) -> str:
    """Redact PII patterns from text (API keys, emails, IPs, etc.)."""
    for pattern, replacement in _COMPILED:
        text = pattern.sub(replacement, text)
    return text


def install_loguru_pii_filter() -> None:
    """Install a loguru sink filter that auto-redacts PII from all log output."""
    def _pii_patcher(record: dict) -> None:
        message = record.get("message", "")
        if isinstance(message, str):
            record["message"] = redact_pii(message)

    logger.configure(patcher=_pii_patcher)


class SecretProvider(ABC):
    """Abstract interface for secret retrieval.

    Implementations:
    - EnvSecretProvider: read from environment variables
    - KeyringSecretProvider: read from OS keyring
    - VaultSecretProvider: read from HashiCorp Vault
    """

    @abstractmethod
    def get(self, key: str) -> str | None:
        ...

    @abstractmethod
    def set(self, key: str, value: str) -> None:
        ...

    @abstractmethod
    def delete(self, key: str) -> bool:
        ...

    @abstractmethod
    def list_keys(self) -> list[str]:
        ...


class EnvSecretProvider(SecretProvider):
    """Read secrets from environment variables."""

    def get(self, key: str) -> str | None:
        return os.environ.get(key)

    def set(self, key: str, value: str) -> None:
        os.environ[key] = value

    def delete(self, key: str) -> bool:
        if key in os.environ:
            del os.environ[key]
            return True
        return False

    def list_keys(self) -> list[str]:
        return [k for k in os.environ if any(
            keyword in k.upper() for keyword in ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "AUTH")
        )]


class KeyringSecretProvider(SecretProvider):
    """Read/write secrets from the OS keyring via the ``keyring`` library."""

    SERVICE_NAME = "markbot"

    def __init__(self) -> None:
        try:
            import keyring
            self._kr = keyring
        except ImportError:
            raise ImportError("keyring package required: pip install keyring")

    def get(self, key: str) -> str | None:
        return self._kr.get_password(self.SERVICE_NAME, key)

    def set(self, key: str, value: str) -> None:
        self._kr.set_password(self.SERVICE_NAME, key, value)

    def delete(self, key: str) -> bool:
        try:
            self._kr.delete_password(self.SERVICE_NAME, key)
            return True
        except self._kr.errors.PasswordDeleteError:
            return False

    def list_keys(self) -> list[str]:
        try:
            return self._kr.get_keyring().get_password(self.SERVICE_NAME, None) or []
        except Exception:
            return []


class CompositeSecretProvider(SecretProvider):
    """Chain multiple providers, checking in order until one returns a value."""

    def __init__(self, *providers: SecretProvider) -> None:
        self._providers = list(providers)

    def get(self, key: str) -> str | None:
        for provider in self._providers:
            value = provider.get(key)
            if value is not None:
                return value
        return None

    def set(self, key: str, value: str) -> None:
        if self._providers:
            self._providers[0].set(key, value)

    def delete(self, key: str) -> bool:
        for provider in self._providers:
            if provider.delete(key):
                return True
        return False

    def list_keys(self) -> list[str]:
        keys: set[str] = set()
        for provider in self._providers:
            keys.update(provider.list_keys())
        return sorted(keys)


_default_provider: SecretProvider | None = None


def get_secret_provider() -> SecretProvider:
    """Get the default secret provider (lazy-initialized)."""
    global _default_provider
    if _default_provider is not None:
        return _default_provider
    _default_provider = EnvSecretProvider()
    return _default_provider


def set_secret_provider(provider: SecretProvider) -> None:
    """Override the default secret provider."""
    global _default_provider
    _default_provider = provider


_PRIVATE_IP_RE = re.compile(
    r'^(?:10\.|172\.(?:1[6-9]|2[0-9]|3[01])\.|192\.168\.|127\.|0\.|::1|fe80:|169\.254\.)',
    re.IGNORECASE,
)


def is_private_ip(host: str) -> bool:
    """Check if a hostname/IP is a private/reserved address (SSRF protection)."""
    if host.lower() in ("localhost", "localhost.localdomain"):
        return True
    return bool(_PRIVATE_IP_RE.match(host))


def validate_url_ssrf(url: str, allowed_internal_ips: list[str] | None = None) -> str | None:
    """Validate a URL against SSRF attacks.

    Returns an error message if the URL is dangerous, or None if safe.
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except Exception:
        return f"Invalid URL: {url}"

    host = parsed.hostname or ""
    allowed = set(allowed_internal_ips or [])

    if host in allowed:
        return None

    if is_private_ip(host):
        return f"URL points to private IP: {host}"

    if parsed.scheme not in ("http", "https"):
        return f"Disallowed URL scheme: {parsed.scheme}"

    return None
