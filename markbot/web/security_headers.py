"""Security response headers for the web UI.

The web UI is a local single-page app that can read config, edit env vars,
run tools and stream agent output — i.e. it is a high-value target for any
XSS that lands in a rendered message. The server previously sent **no**
security headers at all: a `<script>` that got into the transcript (an
uploaded SVG opened in a tab, a model response rendered as HTML, a
`javascript:` link) had a free run of the API, and the page could be framed
by any other site.

This module adds the headers that make those classes of attack expensive:

* **CSP** — nonce-based ``script-src`` (no ``'unsafe-inline'``), so an
  injected inline script does not execute. The one legitimate inline script
  (the session-token bootstrap) carries the per-request nonce.
* **frame-ancestors / X-Frame-Options** — clickjacking.
* **Referrer-Policy: no-referrer** — the page URL carries ``?token=…``, and
  without this header that token leaks to every outbound link and to any
  third-party resource.
* **nosniff / CORP / COOP / Permissions-Policy** — cheap hardening.
* **HSTS** — only when the request actually arrived over HTTPS, so the
  default ``http://127.0.0.1`` deployment is not bricked.

Implemented as pure ASGI middleware rather than ``BaseHTTPMiddleware`` so it
does not interfere with streaming responses (the chat stream is SSE-like
chunked text) and passes WebSocket connections straight through.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

from starlette.datastructures import Headers, MutableHeaders

#: Key under ``scope["state"]`` where the per-request CSP nonce is published
#: so route handlers (which inject the token bootstrap script) can reuse it.
CSP_NONCE_STATE_KEY = "csp_nonce"

# A Host header is attacker-controlled in the abstract. We only ever echo it
# back into a CSP directive, so a bogus value just breaks the page for the
# attacker; still, refuse anything that is not a plain host[:port] so a
# crafted value cannot smuggle extra directives into the policy.
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$|^\[[0-9A-Fa-f:]+\](:\d{1,5})?$")

#: Permissions-Policy: deny the powerful features the UI never uses.
PERMISSIONS_POLICY = ", ".join(
    f"{feature}=()"
    for feature in (
        "accelerometer",
        "autoplay",
        "camera",
        "display-capture",
        "geolocation",
        "gyroscope",
        "magnetometer",
        "microphone",
        "midi",
        "payment",
        "usb",
        "xr-spatial-tracking",
    )
)


def _request_origin(scope: dict[str, Any], scheme: str) -> str | None:
    """Best-effort ``scheme://host`` for the CSP ``connect-src`` directive."""
    host = Headers(scope=scope).get("host", "")
    if not host or not _HOST_RE.match(host):
        return None
    return f"{scheme}://{host}"


def build_csp(nonce: str, ws_origin: str | None) -> str:
    """Build the Content-Security-Policy value for one response.

    ``script-src`` deliberately has no ``'unsafe-inline'``: the only inline
    script in the app is the token bootstrap, and it is given *nonce*. A
    third-party script that lands in the DOM without that nonce is blocked
    even if it is served from our own origin.
    """
    connect_src = ["'self'"]
    if ws_origin:
        # ``'self'`` covers ws:// on modern browsers, but not uniformly across
        # versions; name the exact origin instead of opening ``ws:`` (which
        # would allow any WebSocket host and therefore exfiltration).
        connect_src.append(ws_origin)

    directives: list[tuple[str, list[str]]] = [
        ("default-src", ["'self'"]),
        ("base-uri", ["'self'"]),
        ("object-src", ["'none'"]),
        ("frame-ancestors", ["'none'"]),
        ("form-action", ["'self'"]),
        ("script-src", ["'self'", f"'nonce-{nonce}'"]),
        # Inline *style attributes* are used by the React components and are
        # not covered by a style nonce; scripts remain nonce-only.
        ("style-src", ["'self'", "'unsafe-inline'"]),
        ("img-src", ["'self'", "data:", "blob:"]),
        ("media-src", ["'self'", "blob:"]),
        ("font-src", ["'self'", "data:"]),
        ("connect-src", connect_src),
        ("worker-src", ["'self'", "blob:"]),
    ]
    return "; ".join(f"{name} {' '.join(values)}" for name, values in directives)


def security_headers(nonce: str, scope: dict[str, Any]) -> dict[str, str]:
    """The header set applied to every HTTP response."""
    scheme = scope.get("scheme", "http")
    # Trust a reverse proxy's forwarded proto, but only for the HSTS decision:
    # sending HSTS on plain http://localhost would pin the browser to https
    # for a port that has no TLS listener.
    forwarded = Headers(scope=scope).get("x-forwarded-proto", "").split(",")[0].strip().lower()
    effective_scheme = forwarded if forwarded in ("http", "https") else scheme

    ws_scheme = "wss" if effective_scheme == "https" else "ws"
    headers = {
        "Content-Security-Policy": build_csp(nonce, _request_origin(scope, ws_scheme)),
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Permissions-Policy": PERMISSIONS_POLICY,
    }
    if effective_scheme == "https":
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return headers


class SecurityHeadersMiddleware:
    """Pure-ASGI middleware that stamps security headers on HTTP responses."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            # WebSocket / lifespan: pass through untouched.
            await self.app(scope, receive, send)
            return

        nonce = secrets.token_urlsafe(16)
        scope.setdefault("state", {})[CSP_NONCE_STATE_KEY] = nonce
        headers = security_headers(nonce, scope)

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                # MutableHeaders is case-insensitive, so a handler that already
                # set one of these does not end up with a duplicate pair.
                mutable = MutableHeaders(scope=message)
                for name, value in headers.items():
                    mutable[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)
