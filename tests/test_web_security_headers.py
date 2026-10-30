"""Tests for web security response headers (CSP, framing, referrer, HSTS).

The web UI had no security headers at all: an XSS in a rendered message could
call the whole API (config, env, logs), the page could be framed by any site,
and because the entry URL carries ``?token=…`` that token leaked to every
outbound link via the ``Referer`` header.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient

from markbot.web.security_headers import (
    CSP_NONCE_STATE_KEY,
    SecurityHeadersMiddleware,
    build_csp,
    security_headers,
)


def _tiny_app() -> Starlette:
    async def ok(request):
        return JSONResponse({"nonce": request.state.csp_nonce})

    async def ws(websocket):
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    app = Starlette(
        routes=[Route("/ok", ok), Route("/plain", lambda r: PlainTextResponse("x")), WebSocketRoute("/ws", ws)]
    )
    app.add_middleware(SecurityHeadersMiddleware)
    return app


@pytest.fixture
def client():
    return TestClient(_tiny_app())


class TestHeadersAreAlwaysPresent:
    def test_html_and_json_get_the_full_set(self, client):
        for path in ("/ok", "/plain"):
            r = client.get(path)
            assert r.status_code == 200
            h = r.headers
            assert "content-security-policy" in h
            assert h["x-content-type-options"] == "nosniff"
            assert h["x-frame-options"] == "DENY"
            assert h["referrer-policy"] == "no-referrer"
            assert h["cross-origin-opener-policy"] == "same-origin"
            assert "camera=()" in h["permissions-policy"]

    def test_frame_ancestors_is_none(self, client):
        csp = client.get("/ok").headers["content-security-policy"]
        assert "frame-ancestors 'none'" in csp
        assert "object-src 'none'" in csp

    def test_no_hsts_over_plain_http(self, client):
        """HSTS on http://127.0.0.1 would pin the browser to a TLS port that
        does not exist and lock the user out of their own UI."""
        assert "strict-transport-security" not in client.get("/ok").headers

    def test_hsts_when_forwarded_proto_is_https(self, client):
        r = client.get("/ok", headers={"x-forwarded-proto": "https"})
        assert "max-age=31536000" in r.headers["strict-transport-security"]


class TestCspIsNonceBased:
    def test_script_src_has_no_unsafe_inline(self, client):
        csp = client.get("/ok").headers["content-security-policy"]
        script_src = next(d for d in csp.split(";") if d.strip().startswith("script-src"))
        assert "'unsafe-inline'" not in script_src
        assert "'nonce-" in script_src

    def test_nonce_is_unique_per_request(self, client):
        a = client.get("/ok").json()["nonce"]
        b = client.get("/ok").json()["nonce"]
        assert a and b and a != b
        assert len(a) >= 16

    def test_header_nonce_matches_request_state(self, client):
        r = client.get("/ok")
        nonce = r.json()["nonce"]
        assert f"'nonce-{nonce}'" in r.headers["content-security-policy"]

    def test_connect_src_names_the_exact_ws_origin(self, client):
        csp = client.get("/ok").headers["content-security-policy"]
        connect_src = next(d for d in csp.split(";") if d.strip().startswith("connect-src"))
        tokens = connect_src.split()[1:]
        # 'self' plus one concrete origin -- never the bare ``ws:`` scheme,
        # which would allow a WebSocket to any host (i.e. exfiltration).
        assert "ws://testserver" in tokens
        assert "ws:" not in tokens
        assert "wss:" not in tokens

    def test_https_request_uses_wss(self, client):
        csp = client.get("/ok", headers={"x-forwarded-proto": "https"}).headers[
            "content-security-policy"
        ]
        assert "wss://testserver" in csp


class TestHostHeaderCannotInjectDirectives:
    def test_bogus_host_is_not_echoed(self):
        scope = {"type": "http", "scheme": "http", "headers": [(b"host", b"evil.example; script-src *")]}
        csp = security_headers("n0nce", scope)["Content-Security-Policy"]
        assert "evil.example" not in csp
        assert csp.count("script-src") == 1

    def test_missing_host_falls_back_to_self_only(self):
        csp = security_headers("n0nce", {"type": "http", "scheme": "http", "headers": []})[
            "Content-Security-Policy"
        ]
        connect_src = next(d for d in csp.split(";") if d.strip().startswith("connect-src"))
        assert connect_src.strip() == "connect-src 'self'"


class TestWebsocketIsNotBroken:
    def test_ws_scope_passes_through_untouched(self, client):
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_text() == "hello"


class TestRealApp:
    """The middleware must be wired into the actual app, not just available."""

    @pytest.fixture
    def real(self):
        from markbot.web.server import _build_app

        return TestClient(_build_app())

    def test_index_has_headers(self, real):
        r = real.get("/")
        assert r.status_code == 200
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert "content-security-policy" in r.headers

    def test_unauthorized_api_response_still_carries_headers(self, real):
        r = real.get("/api/config")
        assert r.status_code == 401, "precondition: this endpoint requires the token"
        assert r.headers["x-frame-options"] == "DENY"
        assert "content-security-policy" in r.headers

    def test_token_bootstrap_script_carries_the_nonce(self, real):
        from markbot.web.auth import get_token

        token = get_token()
        r = real.get(f"/?token={token}")
        assert r.status_code == 200
        body = r.text
        assert "window.__MARKBOT_SESSION_TOKEN__" in body, "token must be injected for an authed caller"

        csp = r.headers["content-security-policy"]
        nonce = next(
            part.split("'nonce-", 1)[1].rstrip("'")
            for part in csp.split(";")
            if "'nonce-" in part
        )
        assert f'nonce="{nonce}"' in body, "inline script without the nonce would be blocked by our own CSP"

    def test_anonymous_page_load_gets_no_token(self, real):
        r = real.get("/")
        assert r.status_code == 200
        assert "window.__MARKBOT_SESSION_TOKEN__" not in r.text

    def test_token_is_emitted_as_a_js_string_literal(self, real):
        """The value must be a quoted JS literal, not raw interpolation.

        ``token_urlsafe`` never produces a quote today, so this is about the
        shape of the emitted script: ``json.dumps`` guarantees the literal is
        well-formed even if the token alphabet ever changes.
        """
        import re

        from markbot.web.auth import get_token

        body = real.get(f"/?token={get_token()}").text
        m = re.search(
            r'<script nonce="[^"]+">window\.__MARKBOT_SESSION_TOKEN__=("(?:[^"\\]|\\.)*");</script>',
            body,
        )
        assert m, f"bootstrap script not in expected shape: {body[-400:]}"
        assert m.group(1).startswith('"') and m.group(1).endswith('"')


class TestCspBuilder:
    def test_directives_are_well_formed(self):
        csp = build_csp("abc", None)
        for directive in csp.split(";"):
            name, _, values = directive.strip().partition(" ")
            assert name and "-" in name, directive
            assert values, directive
        assert "default-src 'self'" in csp

    def test_no_ws_origin_when_host_unknown(self):
        connect_src = next(
            d.strip() for d in build_csp("abc", None).split(";") if d.strip().startswith("connect-src")
        )
        assert connect_src == "connect-src 'self'"

    def test_ws_origin_is_appended_when_known(self):
        connect_src = next(
            d.strip()
            for d in build_csp("abc", "wss://h:1").split(";")
            if d.strip().startswith("connect-src")
        )
        assert connect_src == "connect-src 'self' wss://h:1"

    def test_nonce_state_key_is_published(self):
        assert CSP_NONCE_STATE_KEY == "csp_nonce"
