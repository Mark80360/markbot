"""Tests for :mod:`markbot.utils.atomic`."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from markbot.utils.atomic import (
    FileLock,
    LockAcquisitionError,
    atomic_write_json,
)


def test_atomic_write_creates_file(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    atomic_write_json(path, {"hello": "world"})
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == {"hello": "world"}


def test_atomic_write_overwrites_existing(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    path.write_text('{"old": true}', encoding="utf-8")
    atomic_write_json(path, {"new": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"new": True}


def test_atomic_write_uses_utf8_no_ascii_escape(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    atomic_write_json(path, {"msg": "你好,世界"})
    text = path.read_text(encoding="utf-8")
    assert "你好,世界" in text
    assert r"\u" not in text


def test_atomic_write_serializes_datetime_via_default_str(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    atomic_write_json(path, {"now": datetime(2026, 6, 2, tzinfo=timezone.utc)})
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["now"] == "2026-06-02 00:00:00+00:00"


def test_atomic_write_serializes_uuid_via_default_str(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    u = UUID("12345678-1234-5678-1234-567812345678")
    atomic_write_json(path, {"id": u})
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "id": str(u),
    }


def test_atomic_write_cleans_up_tmp_on_failure(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "data.json"

    def _raise(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr("markbot.utils.atomic.json.dumps", _raise)
    with pytest.raises(RuntimeError, match="boom"):
        atomic_write_json(path, {"x": 1})

    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []
    assert not path.exists()


def test_file_lock_acquires_and_releases(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    lock = FileLock(path, timeout=1.0)
    with lock:
        assert (tmp_path / "data.json.lock").exists()
    with FileLock(path, timeout=1.0):
        pass


def test_file_lock_sidecar_created_on_demand(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    assert not (tmp_path / "data.json.lock").exists()
    with FileLock(path, timeout=1.0):
        assert (tmp_path / "data.json.lock").exists()


def test_lock_acquisition_error_is_timeout_subclass() -> None:
    assert issubclass(LockAcquisitionError, TimeoutError)


def test_file_lock_serializes_concurrent_holders(tmp_path: Path) -> None:
    """Two threads must take turns, not overlap."""
    path = tmp_path / "data.json"
    counter = {"value": 0}
    start = threading.Event()
    finished = []
    lock = FileLock(path, timeout=5.0)

    def worker() -> None:
        start.wait()
        with lock:
            v = counter["value"]
            time.sleep(0.05)
            counter["value"] = v + 1
            finished.append(threading.current_thread().ident)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join(timeout=5.0)

    assert len(finished) == 2
    assert counter["value"] == 2


def test_file_lock_timeout_raises_acquisition_error(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    holder_acquired = threading.Event()
    release_holder = threading.Event()

    def holder() -> None:
        with FileLock(path, timeout=1.0):
            holder_acquired.set()
            release_holder.wait(timeout=5.0)

    t = threading.Thread(target=holder)
    t.start()
    try:
        holder_acquired.wait(timeout=2.0)
        with pytest.raises(LockAcquisitionError):
            with FileLock(path, timeout=0.3):
                pytest.fail("should not have acquired")
    finally:
        release_holder.set()
        t.join(timeout=5.0)
