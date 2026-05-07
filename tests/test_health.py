"""Tests for markbot.utils.health — health check server."""

import json
import time
from unittest.mock import patch

from markbot.utils.health import HealthServer


class TestHealthServer:
    def test_initial_state(self):
        server = HealthServer(port=0)
        assert not server.is_running

    def test_set_ready(self):
        server = HealthServer(port=0)
        server.set_ready("provider", True)
        assert server._readiness["provider"] is True
        server.set_ready("provider", False)
        assert server._readiness["provider"] is False

    def test_remove_component(self):
        server = HealthServer(port=0)
        server.set_ready("provider", True)
        server.remove_component("provider")
        assert "provider" not in server._readiness

    def test_readiness_all_ok(self):
        server = HealthServer(port=0)
        server.set_ready("provider", True)
        server.set_ready("channels", True)
        assert all(server._readiness.values())

    def test_readiness_degraded(self):
        server = HealthServer(port=0)
        server.set_ready("provider", True)
        server.set_ready("channels", False)
        assert not all(server._readiness.values())
