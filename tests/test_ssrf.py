"""Tests for markbot.utils.ssrf — SSRF protection module."""

from __future__ import annotations

import socket

import pytest

from markbot.config.schema import Config
from markbot.utils import ssrf
from markbot.utils.ssrf import init_from_config


@pytest.fixture(autouse=True)
def reset_ssrf_state():
    """Reset ssrf module state before each test."""
    init_from_config(Config())
    yield


def test_init_from_config_populates_block_lists():
    config = Config()
    init_from_config(config)
    assert "metadata.google.internal" in ssrf._BLOCKED_HOSTNAMES
    assert "127.0.0.0/8" in [str(n) for n in ssrf._PRIVATE_NETWORKS]


class TestValidateUrlTargetScheme:
    def test_ftp_scheme_rejected(self):
        ok, err = ssrf.validate_url_target("ftp://example.com")
        assert ok is False
        assert "http/https" in err

    def test_file_scheme_rejected(self):
        ok, err = ssrf.validate_url_target("file:///etc/passwd")
        assert ok is False

    def test_missing_hostname_rejected(self):
        ok, err = ssrf.validate_url_target("https:///path")
        assert ok is False
        assert "hostname" in err.lower() or "domain" in err.lower()

    def test_valid_https_accepted(self, monkeypatch):
        monkeypatch.setattr(
            "socket.getaddrinfo",
            lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))],
        )
        ok, err = ssrf.validate_url_target("https://example.com")
        assert ok is True
        assert err == ""
