"""Concurrency tests for :class:`markbot.session.handoff.HandoffManager`."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from markbot.session.handoff import HandoffManager, SessionHandoff
from markbot.utils.atomic import FileLock, LockAcquisitionError


@pytest.fixture
def manager(tmp_path: Path) -> HandoffManager:
    return HandoffManager(tmp_path)


def _make_handoff(key: str, summary: str = "default") -> SessionHandoff:
    return SessionHandoff(
        session_key=key,
        timestamp="2026-06-02 00:00",
        next_best_step=summary,
    )


def test_concurrent_save_different_keys_no_contention(manager: HandoffManager) -> None:
    """Different session keys acquire independent locks, so parallel
    writes to distinct sessions are non-blocking."""

    def save_one(i: int) -> None:
        manager.save(_make_handoff(f"key-{i}", summary=f"saved-{i}"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(save_one, range(20)))

    for i in range(20):
        h = manager.load(f"key-{i}")
        assert h is not None
        assert h.next_best_step == f"saved-{i}"


def test_concurrent_save_same_key_no_corruption(manager: HandoffManager) -> None:
    """Parallel writes to the same key: file remains valid JSON,
    one of the writes wins (last-write-wins semantics is fine —
    the requirement is no torn writes or corruption)."""
    N = 30

    def save_one(i: int) -> None:
        manager.save(_make_handoff("shared", summary=f"version-{i}"))

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(save_one, range(N)))

    h = manager.load("shared")
    assert h is not None
    assert h.next_best_step.startswith("version-")


def test_load_during_save_returns_consistent(manager: HandoffManager) -> None:
    """A reader that runs while a writer is mid-save must never see
    a half-written file."""
    manager.save(_make_handoff("k", summary="initial"))

    def writer() -> None:
        for i in range(50):
            manager.save(_make_handoff("k", summary=f"v{i}"))

    def reader() -> int:
        ok = 0
        for _ in range(50):
            h = manager.load("k")
            if h is not None and h.next_best_step.startswith("v"):
                ok += 1
        return ok

    with ThreadPoolExecutor(max_workers=2) as pool:
        wf = pool.submit(writer)
        rf = pool.submit(reader)
        wf.result()
        ok_reads = rf.result()

    assert ok_reads > 0


def test_lock_timeout_returns_acquisition_error(manager: HandoffManager) -> None:
    """A read that cannot acquire the lock within the timeout must
    raise ``LockAcquisitionError``, not hang or return stale data."""
    lock_path = manager._handoff_path("k")
    held = FileLock(lock_path, timeout=1.0)
    held.__enter__()
    try:
        contended = FileLock(lock_path, timeout=0.3)
        with pytest.raises(LockAcquisitionError):
            contended.__enter__()
    finally:
        held.__exit__(None, None, None)


def test_delete_during_load_safe(manager: HandoffManager) -> None:
    """A delete that races with a load must not raise, and the load
    returns either the old handoff or None."""
    manager.save(_make_handoff("k", summary="doomed"))

    def reader() -> str:
        for _ in range(50):
            h = manager.load("k")
            if h is not None:
                return h.next_best_step
        return "never"

    def deleter() -> None:
        for _ in range(10):
            manager.delete("k")
            manager.save(_make_handoff("k", summary="resurrected"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        rf = pool.submit(reader)
        df = pool.submit(deleter)
        df.result()
        result = rf.result()

    assert result in ("doomed", "resurrected")
