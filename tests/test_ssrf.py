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
        assert "http/https" in err

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


class TestValidateUrlTargetPrivateNetworks:
    def test_localhost_blocked(self):
        ok, err = ssrf.validate_url_target("http://127.0.0.1/admin")
        assert ok is False
        assert "private" in err.lower() or "internal" in err.lower()

    def test_private_10_blocked(self):
        ok, err = ssrf.validate_url_target("http://10.0.0.1/api")
        assert ok is False

    def test_metadata_endpoint_blocked(self):
        ok, err = ssrf.validate_url_target("http://169.254.169.254/latest/meta-data/")
        assert ok is False

    def test_public_ip_allowed(self, monkeypatch):
        monkeypatch.setattr(
            "socket.getaddrinfo",
            lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))],
        )
        ok, err = ssrf.validate_url_target("https://example.com")
        assert ok is True

    def test_dns_resolves_to_private_blocked(self, monkeypatch):
        monkeypatch.setattr(
            "socket.getaddrinfo",
            lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))],
        )
        ok, err = ssrf.validate_url_target("http://localtest.me")
        assert ok is False

    def test_unresolvable_hostname_blocked(self, monkeypatch):
        def _raise(*a, **kw):
            raise socket.gaierror("Name resolution failed")
        monkeypatch.setattr("socket.getaddrinfo", _raise)
        ok, err = ssrf.validate_url_target("http://nonexistent.invalid")
        assert ok is False
        assert "resolve" in err.lower()


class TestValidateUrlTargetCloudMetadata:
    def test_google_metadata_hostname_blocked(self):
        ok, err = ssrf.validate_url_target("http://metadata.google.internal/computeMetadata/v1/")
        assert ok is False
        assert "metadata" in err.lower() or "blocked" in err.lower()

    def test_metadata_goog_blocked(self):
        ok, err = ssrf.validate_url_target("https://metadata.goog/foo")
        assert ok is False

    def test_alibaba_metadata_ip_blocked(self, monkeypatch):
        monkeypatch.setattr(
            "socket.getaddrinfo",
            lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("100.100.100.200", 0))],
        )
        ok, err = ssrf.validate_url_target("http://100.100.100.200/latest/meta-data/")
        assert ok is False
        assert "metadata" in err.lower() or "blocked" in err.lower()


class TestValidateUrlTargetAllowPrivate:
    def test_allow_private_bypasses_private_network(self):
        ok, err = ssrf.validate_url_target("http://10.0.0.1/api", allow_private=True)
        assert ok is True
        assert err == ""

    def test_allow_private_does_not_bypass_always_blocked(self, monkeypatch):
        monkeypatch.setattr(
            "socket.getaddrinfo",
            lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 0))],
        )
        ok, err = ssrf.validate_url_target("http://169.254.169.254/foo", allow_private=True)
        assert ok is False

    def test_allow_private_does_not_bypass_blocked_hostname(self):
        ok, err = ssrf.validate_url_target(
            "http://metadata.google.internal/foo", allow_private=True
        )
        assert ok is False


class TestValidateResolvedUrl:
    def test_public_ip_ok(self):
        ok, err = ssrf.validate_resolved_url("https://93.184.216.34/page")
        assert ok is True

    def test_private_ip_blocked(self):
        ok, err = ssrf.validate_resolved_url("http://127.0.0.1/page")
        assert ok is False
        assert "private" in err.lower() or "blocked" in err.lower()

    def test_no_hostname_passes(self):
        ok, err = ssrf.validate_resolved_url("https:///path")
        assert ok is True

    def test_metadata_ip_blocked_post_redirect(self, monkeypatch):
        monkeypatch.setattr(
            "socket.getaddrinfo",
            lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 0))],
        )
        ok, err = ssrf.validate_resolved_url("https://example.com/redirect")
        assert ok is False

    def test_allow_private_bypasses_private_network(self):
        ok, err = ssrf.validate_resolved_url("http://10.0.0.1/page", allow_private=True)
        assert ok is True

    def test_always_blocked_not_bypassed_by_allow_private(self, monkeypatch):
        monkeypatch.setattr(
            "socket.getaddrinfo",
            lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("100.100.100.200", 0))],
        )
        ok, err = ssrf.validate_resolved_url("http://100.100.100.200/page", allow_private=True)
        assert ok is False


class TestContainsInternalUrl:
    def test_no_urls_returns_false(self):
        assert ssrf.contains_internal_url("echo hello") is False

    def test_public_url_returns_false(self, monkeypatch):
        monkeypatch.setattr(
            "socket.getaddrinfo",
            lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))],
        )
        assert ssrf.contains_internal_url("curl https://example.com") is False

    def test_internal_url_returns_true(self):
        assert ssrf.contains_internal_url("curl http://127.0.0.1/admin") is True

    def test_multiple_urls_one_internal(self, monkeypatch):
        monkeypatch.setattr(
            "socket.getaddrinfo",
            lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))],
        )
        result = ssrf.contains_internal_url("curl https://example.com http://10.0.0.1")
        assert result is True

    def test_allowed_ip_whitelist(self):
        result = ssrf.contains_internal_url(
            "curl http://10.0.0.1/api",
            allowed_ips=["10.0.0.1"],
        )
        assert result is False

    def test_allowed_network_whitelist(self):
        result = ssrf.contains_internal_url(
            "curl http://10.0.0.5/api",
            allowed_ips=["10.0.0.0/24"],
        )
        assert result is False

    def test_ip_not_in_whitelist(self):
        result = ssrf.contains_internal_url(
            "curl http://10.0.0.1/api",
            allowed_ips=["192.168.1.0/24"],
        )
        assert result is True

    def test_allow_private_bypasses_private_check(self, monkeypatch):
        monkeypatch.setattr(
            "socket.getaddrinfo",
            lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 0))],
        )
        result = ssrf.contains_internal_url("curl http://10.0.0.1/api", allow_private=True)
        assert result is False
