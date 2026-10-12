"""Tests for transport-level SSRF protection.

Validating ``response.url`` after the fact is not SSRF protection: by then
the request has already been delivered. Against ``169.254.169.254`` the cloud
metadata service has already answered, and the TCP connection to an internal
host has already been made. The guard therefore hooks the transport so every
hop — the original URL and each redirect target — is checked *before* a
socket is opened.
"""

from __future__ import annotations

import pytest

from markbot.utils.ssrf import (
    SSRFRedirectError,
    guarded_client,
    ssrf_guard_transport,
    validate_resolved_url,
)


class TestValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",
            "http://192.168.1.1/admin",
            "http://10.0.0.5/",
            "http://172.16.0.1/",
            "http://[::1]/",
            "http://127.0.0.1:8080/",
        ],
    )
    def test_internal_targets_blocked(self, url):
        ok, err = validate_resolved_url(url)
        assert not ok, url
        assert err

    def test_allow_private_opts_in(self):
        ok, _ = validate_resolved_url("http://192.168.1.1/", allow_private=True)
        assert ok

    def test_public_url_allowed(self):
        ok, _ = validate_resolved_url("https://example.com/")
        assert ok


class TestTransportGuard:
    def test_transport_is_produced(self):
        assert ssrf_guard_transport() is not None

    @pytest.mark.asyncio
    async def test_metadata_ip_refused_before_connect(self):
        async with guarded_client(timeout=2.0) as client:
            with pytest.raises(SSRFRedirectError):
                await client.get("http://169.254.169.254/latest/meta-data/")

    @pytest.mark.asyncio
    async def test_private_lan_refused(self):
        async with guarded_client(timeout=2.0) as client:
            with pytest.raises(SSRFRedirectError):
                await client.get("http://192.168.1.1/")

    @pytest.mark.asyncio
    async def test_loopback_refused(self):
        async with guarded_client(timeout=2.0) as client:
            with pytest.raises(SSRFRedirectError):
                await client.get("http://127.0.0.1:9/")

    @pytest.mark.asyncio
    async def test_allow_private_permits_loopback(self):
        """Explicit opt-out still works, so local development is possible."""
        async with guarded_client(timeout=1.0, allow_private=True) as client:
            # Not asserting a successful request (nothing is listening) —
            # only that the guard no longer refuses it up front.
            with pytest.raises(Exception) as exc:
                await client.get("http://127.0.0.1:9/")
            assert not isinstance(exc.value, SSRFRedirectError)


class TestClientConstruction:
    def test_follows_redirects_by_default(self):
        """Redirects must be followed *with* the guard attached, otherwise
        each hop bypasses it."""
        import httpx

        c = guarded_client(timeout=1.0)
        assert c.follow_redirects is True
        assert isinstance(c, httpx.AsyncClient)

    def test_proxy_supported(self):
        c = guarded_client(proxy="http://127.0.0.1:7890", timeout=1.0)
        assert c is not None

    def test_no_proxy_uses_guarded_transport(self):
        c = guarded_client(timeout=1.0)
        assert c._transport is not None

    def test_allow_private_popped_not_forwarded(self):
        """It is our flag, not httpx's; passing it through would TypeError."""
        guarded_client(timeout=1.0, allow_private=True)


class TestToolIntegration:
    def test_web_tools_use_guarded_client(self):
        """The three model-controlled fetch paths must not use a raw client."""
        import inspect

        from markbot.tools import web

        src = inspect.getsource(web)
        assert "guarded_client" in src
        # Raw clients remain only for the fixed search-API endpoints.
        raw = src.count("httpx.AsyncClient(proxy=self.proxy)")
        assert raw <= 5

    def test_extract_path_guards_redirect(self):
        import inspect

        from markbot.tools.web import WebExtractTool

        src = inspect.getsource(WebExtractTool._fetch_readability)
        assert "guarded_client" in src

    def test_fetch_path_guards_redirect(self):
        import inspect

        from markbot.tools.web import WebFetchTool

        src = inspect.getsource(WebFetchTool._legacy_execute)
        assert "guarded_client" in src
