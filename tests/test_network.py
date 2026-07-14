"""Tests for markbot.utils.network — SSRF protection and URL validation."""

import pytest
from unittest.mock import patch, MagicMock
import socket

from markbot.utils.network import (
    validate_url_target,
    validate_resolved_url,
    contains_internal_url,
    _is_private,
    _BLOCKED_NETWORKS,
)


class TestIsPrivate:
    def test_localhost_is_private(self):
        import ipaddress
        addr = ipaddress.ip_address("127.0.0.1")
        assert _is_private(addr) is True

    def test_private_10_is_private(self):
        import ipaddress
        addr = ipaddress.ip_address("10.0.0.1")
        assert _is_private(addr) is True

    def test_private_172_is_private(self):
        import ipaddress
        addr = ipaddress.ip_address("172.16.0.1")
        assert _is_private(addr) is True

    def test_private_192_is_private(self):
        import ipaddress
        addr = ipaddress.ip_address("192.168.1.1")
        assert _is_private(addr) is True

    def test_link_local_is_private(self):
        import ipaddress
        addr = ipaddress.ip_address("169.254.1.1")
        assert _is_private(addr) is True

    def test_cloud_metadata_is_private(self):
        import ipaddress
        addr = ipaddress.ip_address("169.254.169.254")
        assert _is_private(addr) is True

    def test_public_ip_not_private(self):
        import ipaddress
        addr = ipaddress.ip_address("8.8.8.8")
        assert _is_private(addr) is False

    def test_ipv6_loopback_is_private(self):
        import ipaddress
        addr = ipaddress.ip_address("::1")
        assert _is_private(addr) is True

    def test_ipv6_unique_local_is_private(self):
        import ipaddress
        addr = ipaddress.ip_address("fc00::1")
        assert _is_private(addr) is True

    def test_ipv6_link_local_is_private(self):
        import ipaddress
        addr = ipaddress.ip_address("fe80::1")
        assert _is_private(addr) is True


class TestValidateUrlTarget:
    def test_valid_https_url(self):
        with patch("socket.getaddrinfo") as mock:
            mock.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))
            ]
            ok, err = validate_url_target("https://example.com")
            assert ok is True
            assert err == ""

    def test_valid_http_url(self):
        with patch("socket.getaddrinfo") as mock:
            mock.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))
            ]
            ok, err = validate_url_target("http://example.com")
            assert ok is True

    def test_ftp_scheme_rejected(self):
        ok, err = validate_url_target("ftp://example.com")
        assert ok is False
        assert "http/https" in err

    def test_file_scheme_rejected(self):
        ok, err = validate_url_target("file:///etc/passwd")
        assert ok is False

    def test_missing_domain(self):
        ok, err = validate_url_target("https://")
        assert ok is False
        assert "Missing" in err or "domain" in err.lower() or "hostname" in err.lower()

    def test_localhost_blocked(self):
        ok, err = validate_url_target("http://127.0.0.1/admin")
        assert ok is False
        assert "private" in err.lower() or "internal" in err.lower()

    def test_private_ip_blocked(self):
        ok, err = validate_url_target("http://10.0.0.1/api")
        assert ok is False

    def test_metadata_endpoint_blocked(self):
        ok, err = validate_url_target("http://169.254.169.254/latest/meta-data/")
        assert ok is False

    @patch("socket.getaddrinfo")
    def test_dns_resolves_to_private_blocked(self, mock_getaddrinfo):
        mock_getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))
        ]
        ok, err = validate_url_target("http://localtest.me")
        assert ok is False

    @patch("socket.getaddrinfo")
    def test_unresolvable_hostname(self, mock_getaddrinfo):
        mock_getaddrinfo.side_effect = socket.gaierror("Name resolution failed")
        ok, err = validate_url_target("http://nonexistent.invalid")
        assert ok is False
        assert "resolve" in err.lower()


class TestValidateResolvedUrl:
    def test_public_ip_ok(self):
        ok, err = validate_resolved_url("https://93.184.216.34/page")
        assert ok is True

    def test_private_ip_blocked(self):
        ok, err = validate_resolved_url("http://127.0.0.1/page")
        assert ok is False
        assert "private" in err.lower()

    def test_no_hostname_passes(self):
        ok, err = validate_resolved_url("https:///path")
        assert ok is True


class TestContainsInternalUrl:
    def test_no_urls(self):
        assert contains_internal_url("echo hello") is False

    def test_public_url(self):
        with patch("socket.getaddrinfo") as mock:
            mock.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))
            ]
            assert contains_internal_url("curl https://example.com") is False

    def test_internal_url_detected(self):
        assert contains_internal_url("curl http://127.0.0.1/admin") is True

    def test_multiple_urls_one_internal(self):
        with patch("markbot.utils.network.validate_url_target") as mock:
            mock.side_effect = [
                (True, ""),
                (False, "blocked"),
            ]
            result = contains_internal_url("curl https://example.com http://10.0.0.1")
            assert result is True

    def test_allowed_ip_whitelist(self):
        result = contains_internal_url(
            "curl http://10.0.0.1/api",
            allowed_ips=["10.0.0.1"],
        )
        assert result is False

    def test_allowed_network_whitelist(self):
        result = contains_internal_url(
            "curl http://10.0.0.5/api",
            allowed_ips=["10.0.0.0/24"],
        )
        assert result is False

    def test_ip_not_in_whitelist(self):
        result = contains_internal_url(
            "curl http://10.0.0.1/api",
            allowed_ips=["192.168.1.0/24"],
        )
        assert result is True
