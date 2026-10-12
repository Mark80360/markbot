"""Tests for turn admission control.

``_dispatch`` (bus path) took the per-session lock; ``process_direct`` (web
UI, cron, heartbeat, autopilot) did not. Both mutate the same ``Session``
object, whose ``messages`` list is appended without a lock and whose
persisted-message cursor lives outside the file lock — so two concurrent
turns on one session could interleave appends, double-write or drop lines.
"""

from __future__ import annotations

import asyncio

import pytest


class _StubLoop:
    """Minimal stand-in exercising the real admission logic from AgentLoop."""

    # Borrow the implementation under test so the test tracks the code.
    from markbot.agent.loop import AgentLoop as _AL

    _holds_session_lock = _AL._holds_session_lock
    _turn_admission = _AL._turn_admission
    _get_session_lock = _AL._get_session_lock

    def __init__(self, max_concurrent: int = 0) -> None:
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_lock_owners: dict[str, asyncio.Task] = {}
        n = int(max_concurrent)
        self._concurrency_gate = asyncio.Semaphore(n) if n > 0 else None


@pytest.fixture
def loop_obj():
    return _StubLoop()


class TestSessionSerialization:
    async def test_second_turn_waits_for_first(self, loop_obj):
        order: list[str] = []

        async def turn(tag: str, delay: float) -> None:
            async with loop_obj._turn_admission("s1"):
                order.append(f"{tag}-enter")
                await asyncio.sleep(delay)
                order.append(f"{tag}-exit")

        await asyncio.gather(turn("a", 0.05), turn("b", 0.0))
        # No interleaving: a fully finishes before b starts.
        assert order == ["a-enter", "a-exit", "b-enter", "b-exit"]

    async def test_different_sessions_run_concurrently(self, loop_obj):
        active = 0
        peak = 0

        async def turn(key: str) -> None:
            nonlocal active, peak
            async with loop_obj._turn_admission(key):
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.03)
                active -= 1

        await asyncio.gather(turn("s1"), turn("s2"), turn("s3"))
        assert peak == 3

    async def test_lock_released_after_exception(self, loop_obj):
        async def boom() -> None:
            async with loop_obj._turn_admission("s1"):
                raise RuntimeError("turn failed")

        with pytest.raises(RuntimeError):
            await boom()
        # A subsequent turn must not deadlock.
        async with loop_obj._turn_admission("s1"):
            pass


class TestReentrancy:
    async def test_same_task_may_reenter(self, loop_obj):
        """A nested process_direct from inside a turn must not deadlock."""
        async with loop_obj._turn_admission("s1"):
            async with loop_obj._turn_admission("s1"):
                pass

    async def test_owner_released_on_exit(self, loop_obj):
        async with loop_obj._turn_admission("s1"):
            assert loop_obj._session_lock_owners.get("s1") is not None
        assert "s1" not in loop_obj._session_lock_owners

    async def test_other_task_still_blocks_while_owned(self, loop_obj):
        entered_second = asyncio.Event()

        async def second() -> None:
            async with loop_obj._turn_admission("s1"):
                entered_second.set()

        async with loop_obj._turn_admission("s1"):
            task = asyncio.create_task(second())
            await asyncio.sleep(0.02)
            # Must still be waiting: re-entrancy is per-task, not per-session.
            assert not entered_second.is_set()
        await asyncio.wait_for(task, timeout=1.0)
        assert entered_second.is_set()

    def test_holds_check_is_task_scoped(self, loop_obj):
        assert loop_obj._holds_session_lock("s1") is False


class TestConcurrencyGate:
    async def test_gate_limits_process_direct_style_turns(self):
        loop_obj = _StubLoop(max_concurrent=1)
        active = 0
        peak = 0

        async def turn() -> None:
            nonlocal active, peak
            async with loop_obj._turn_admission("s1"):
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.02)
                active -= 1

        await asyncio.gather(turn(), turn())
        assert peak == 1

    async def test_gate_absent_is_fine(self):
        loop_obj = _StubLoop(max_concurrent=0)
        assert loop_obj._concurrency_gate is None
        async with loop_obj._turn_admission("s1"):
            pass


class TestRealLoopWiring:
    """The real AgentLoop must expose the same surface."""

    def test_has_admission_helpers(self):
        from markbot.agent.loop import AgentLoop

        assert callable(AgentLoop._turn_admission)
        assert callable(AgentLoop._holds_session_lock)
        assert callable(AgentLoop._get_session_lock)

    def test_process_direct_uses_admission(self):
        """Guard against the lock being dropped from process_direct again."""
        import inspect

        from markbot.agent.loop import AgentLoop

        src = inspect.getsource(AgentLoop.process_direct)
        assert "_turn_admission" in src

    def test_dispatch_uses_admission(self):
        import inspect

        from markbot.agent.loop import AgentLoop

        assert "_turn_admission" in inspect.getsource(AgentLoop._dispatch)

    def test_unlocked_dispatch_does_not_take_session_lock(self):
        """Approval replies run inside a turn that already holds the lock;
        re-acquiring it would deadlock the daemon."""
        import inspect

        from markbot.agent.loop import AgentLoop

        src = inspect.getsource(AgentLoop._dispatch_unlocked)
        assert "_turn_admission" not in src
        assert "_get_session_lock" not in src
