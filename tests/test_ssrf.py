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


# ---------------------------------------------------------------------------
# IPv4-embedded IPv6 regression suite
#
# The predicate used to compare addresses only within their own version:
# ``IPv6Address == IPv4Address`` is always False and
# ``IPv6Address in IPv4Network`` silently returns False. Every address
# below reached the metadata service / loopback / RFC1918 space through
# that gap while the suite stayed green, because no test ever fed an
# encoded address to the predicate.
# ---------------------------------------------------------------------------

EMBEDDED_BLOCKED = [
    # IPv4-mapped (::ffff:0:0/96), dotted, hex and full 8-group spellings
    "http://[::ffff:169.254.169.254]/latest/meta-data/",
    "http://[::ffff:a9fe:a9fe]/latest/meta-data/",
    "http://[0:0:0:0:0:ffff:a9fe:a9fe]/latest/meta-data/",
    "http://[::ffff:127.0.0.1]:9120/",
    "http://[::ffff:10.0.0.5]/",
    "http://[::ffff:a00:5]/",
    "http://[::ffff:192.168.1.1]/",
    "http://[::ffff:100.100.100.200]/",          # Alibaba metadata
    # 6to4 (2002::/16) — IPv4 lives in bytes 2..6
    "http://[2002:a9fe:a9fe::]/",
    "http://[2002:7f00:1::]/",
    # NAT64 well-known prefix (64:ff9b::/96) — IPv4 lives in the last 32 bits
    "http://[64:ff9b::a9fe:a9fe]/",
    "http://[64:ff9b::a00:5]/",
    # Deprecated IPv4-compatible form (::/96)
    "http://[::a9fe:a9fe]/",
    "http://[::a00:5]/",
]

EMBEDDED_ALLOWED = [
    # Negative controls: encoded *public* addresses must still be allowed.
    # A blanket ``is_private`` fallback fails these, because CPython marks
    # the entire ::ffff:0:0/96 block private.
    "http://[::ffff:8.8.8.8]/",
    "http://[2002:808:808::]/",
    "http://[64:ff9b::808:808]/",
    "http://8.8.8.8/",
    "http://93.184.216.34/",
]


class TestIpv4EmbeddedIpv6Blocked:
    @pytest.mark.parametrize("url", EMBEDDED_BLOCKED)
    def test_validate_url_target_blocks(self, url):
        ok, err = ssrf.validate_url_target(url)
        assert ok is False, f"{url} was allowed through the SSRF predicate"
        assert "Blocked" in err

    @pytest.mark.parametrize("url", EMBEDDED_BLOCKED)
    def test_validate_resolved_url_blocks(self, url):
        """The redirect path must not be a second, laxer gate."""
        ok, err = ssrf.validate_resolved_url(url)
        assert ok is False, f"{url} was allowed through the redirect predicate"
        assert "Blocked" in err

    @pytest.mark.parametrize("url", EMBEDDED_ALLOWED)
    def test_public_encoded_addresses_still_allowed(self, url):
        ok, err = ssrf.validate_url_target(url)
        assert ok is True, f"{url} was wrongly blocked: {err}"
        ok, err = ssrf.validate_resolved_url(url)
        assert ok is True, f"{url} was wrongly blocked on redirect: {err}"

    def test_metadata_block_survives_allow_private(self):
        """``allow_private`` lifts the private-network rule, never the metadata one."""
        for url in (
            "http://[::ffff:169.254.169.254]/",
            "http://[2002:a9fe:a9fe::]/",
            "http://[64:ff9b::a9fe:a9fe]/",
        ):
            ok, _ = ssrf.validate_url_target(url, allow_private=True)
            assert ok is False, f"{url} bypassed the metadata block via allow_private"

    def test_reason_names_the_embedded_address(self):
        """A bare 'blocked' would hide which address triggered it."""
        ok, err = ssrf.validate_url_target("http://[::ffff:169.254.169.254]/")
        assert ok is False
        assert "169.254.169.254" in err

    def test_contains_internal_url_sees_encoded_targets(self):
        """exec's gate must flag the encoded form too, not just the dotted one."""
        for url in (
            "http://[::ffff:169.254.169.254]/latest/meta-data/",
            "http://[::ffff:127.0.0.1]:9120/",
            "http://[2002:a9fe:a9fe::]/",
        ):
            assert ssrf.contains_internal_url(f"curl {url}") is True, url

    def test_whitelist_matches_encoded_form(self):
        """An explicit exemption must apply to the encoded spelling as well."""
        assert ssrf.contains_internal_url(
            "curl http://[::ffff:10.0.0.5]/api", allowed_ips=["10.0.0.5"]
        ) is False


class TestBlockListParsingIsLoud:
    """A block list that silently shrinks is a security downgrade."""

    def test_trailing_space_does_not_drop_the_entry(self):
        from markbot.config.schema import Config

        config = Config()
        config.ssrf.always_blocked_ips = ["169.254.169.254 ", " 100.100.100.200"]
        config.ssrf.blocked_networks = ["169.254.0.0/16 ", "10.0.0.0/8"]
        init_from_config(config)

        assert len(ssrf._ALWAYS_BLOCKED_IPS) == 2
        assert len(ssrf._PRIVATE_NETWORKS) == 2
        ok, _ = ssrf.validate_url_target("http://169.254.169.254/latest/meta-data/")
        assert ok is False

    def test_invalid_entries_are_reported_not_swallowed(self):
        """Unparseable entries must produce an ERROR log, not a silent skip."""
        from loguru import logger

        from markbot.config.schema import Config

        captured: list[str] = []
        sink_id = logger.add(lambda m: captured.append(str(m)), level="ERROR")
        try:
            config = Config()
            config.ssrf.always_blocked_ips = ["not-an-ip"]
            config.ssrf.blocked_networks = ["999.999.999.999/8"]
            init_from_config(config)
        finally:
            logger.remove(sink_id)

        assert ssrf._ALWAYS_BLOCKED_IPS == ()
        assert ssrf._PRIVATE_NETWORKS == ()
        joined = "".join(captured)
        assert "not-an-ip" in joined, f"invalid IP entry was not reported: {joined!r}"
        assert "999.999.999.999/8" in joined, f"invalid network was not reported: {joined!r}"
        assert "NOT be blocked" in joined
