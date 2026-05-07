"""Health check endpoints — /healthz and /readyz.

Provides a lightweight HTTP server that exposes Kubernetes-style
health probes.  The server uses only the stdlib ``http.server``
module so it adds zero dependencies.

- **/healthz** — liveness probe: is the process alive and the event loop responsive?
- **/readyz**  — readiness probe: are all critical subsystems initialized?

Usage::

    from markbot.utils.health import HealthServer

    server = HealthServer(port=8080)
    server.set_ready("provider", True)
    server.set_ready("channels", True)
    server.start()  # non-blocking, runs in a daemon thread

    # Later, when a subsystem degrades:
    server.set_ready("channels", False)
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from loguru import logger


class _HealthHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler for /healthz and /readyz."""

    server: "HealthServer"

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._respond(200, {"status": "ok", "uptime_s": round(time.monotonic() - self.server._start_time, 1)})
        elif self.path == "/readyz":
            checks = self.server._readiness
            all_ok = all(checks.values()) if checks else False
            status_code = 200 if all_ok else 503
            self._respond(status_code, {"status": "ok" if all_ok else "degraded", "checks": dict(checks)})
        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, code: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("[HealthServer] {}", format % args)


class HealthServer:
    """Lightweight health-probe HTTP server.

    Runs in a daemon thread so it never prevents process exit.
    Thread-safe: ``set_ready()`` can be called from any thread.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        self._host = host
        self._port = port
        self._readiness: dict[str, bool] = {}
        self._start_time = time.monotonic()
        self._httpd: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def set_ready(self, component: str, ready: bool) -> None:
        """Update the readiness status of a component.

        Thread-safe; can be called from the asyncio event loop or
        any other thread.
        """
        with self._lock:
            self._readiness[component] = ready

    def remove_component(self, component: str) -> None:
        """Remove a component from readiness tracking."""
        with self._lock:
            self._readiness.pop(component, None)

    def start(self) -> None:
        """Start the health server in a background daemon thread."""
        if self._httpd is not None:
            return

        try:
            self._httpd = HTTPServer((self._host, self._port), _HealthHandler)
            self._httpd.timeout = 1.0
        except OSError as e:
            logger.warning("[HealthServer] Cannot bind {}:{} — {}", self._host, self._port, e)
            return

        self._thread = threading.Thread(target=self._serve, daemon=True, name="health-server")
        self._thread.start()
        logger.info("[HealthServer] Listening on {}:{}/healthz, /readyz", self._host, self._port)

    def stop(self) -> None:
        """Shut down the health server."""
        if self._httpd:
            self._httpd.shutdown()
            self._httpd = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("[HealthServer] Stopped")

    def _serve(self) -> None:
        assert self._httpd is not None
        try:
            self._httpd.serve_forever()
        except Exception as e:
            logger.error("[HealthServer] Error: {}", e)

    @property
    def is_running(self) -> bool:
        return self._httpd is not None and self._thread is not None and self._thread.is_alive()


_global_server: HealthServer | None = None


def get_health_server(host: str = "0.0.0.0", port: int = 8080) -> HealthServer:
    """Get the global singleton HealthServer (lazy-initialized)."""
    global _global_server
    if _global_server is None:
        _global_server = HealthServer(host=host, port=port)
    return _global_server


def stop_health_server() -> None:
    """Stop the global HealthServer."""
    global _global_server
    if _global_server is not None:
        _global_server.stop()
        _global_server = None
