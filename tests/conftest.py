"""Pytest configuration and shared fixtures."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _reset_ssrf_block_lists():
    """Restore the SSRF block lists to schema defaults before every test.

    ``markbot.utils.ssrf`` keeps its rules in module-level globals that are
    populated once from config. Any test that reconfigures them (or, worse,
    leaves them empty) would otherwise leak into every later test in the
    session and make results depend on execution order.
    """
    from markbot.config.schema import Config
    from markbot.utils.ssrf import init_from_config

    init_from_config(Config())
    yield


@pytest.fixture
def temp_workspace(tmp_path: Path) -> Path:
    """Create a temporary workspace for testing."""
    ws = tmp_path / "markbot_test"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


@pytest.fixture
def sample_messages() -> list[dict]:
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello!"},
        {"role": "assistant", "content": "Hi there!"},
    ]
