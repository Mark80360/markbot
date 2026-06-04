"""Token-based authentication for the web API."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.websockets import WebSocket


_session_token = secrets.token_urlsafe(32)


def get_token() -> str:
    return _session_token


def regenerate_token() -> str:
    global _session_token
    _session_token = secrets.token_urlsafe(32)
    return _session_token


EXEMPT_PREFIXES = {"/api/status"}
EXEMPT_PATHS = {"/api/status"}


class TokenAuthMiddleware(BaseHTTPMiddleware):
    def _is_exempt(self, path: str) -> bool:
        # 非 API 路径（SPA 路由、静态资源）免认证
        if not path.startswith("/api/"):
            return True
        if path in EXEMPT_PATHS:
            return True
        for prefix in EXEMPT_PREFIXES:
            if path.startswith(prefix):
                return True
        return False

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if self._is_exempt(request.url.path):
            return await call_next(request)
        token = request.headers.get("x-markbot-session-token", "")
        if not secrets.compare_digest(token, _session_token):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)


async def verify_ws_token(websocket: WebSocket) -> bool:
    token = websocket.query_params.get("token", "")
    return secrets.compare_digest(token, _session_token)
