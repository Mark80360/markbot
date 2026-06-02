"""Tests for markbot.utils.ssrf — SSRF protection module."""

from __future__ import annotations

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
