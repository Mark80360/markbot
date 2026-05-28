"""Pytest configuration and shared fixtures."""
from __future__ import annotations
import pytest
from pathlib import Path


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
