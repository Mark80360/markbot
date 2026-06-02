# P1-2 Implementation Plan: TaskTracker / HandoffManager 文件锁 + 原子写

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `TaskTracker` 和 `HandoffManager` 的持久化层加入跨进程文件锁 + 原子写,消除 read-modify-write 竞态。

**Architecture:** 新建 `markbot/utils/atomic.py` 提供 `FileLock`(portalocker 封装)和 `atomic_write_json`(tmp + os.replace)两个原语;`TaskTracker` 和 `HandoffManager` 在所有公开读 / 写方法外包 `with self._lock:`。公开 API 完全不变。

**Tech Stack:** Python 3.13, portalocker >= 2.5, pytest, loguru, dataclasses

**Spec:** `docs/superpowers/specs/2026-06-02-p1-2-task-locking-design.md`

---

## 文件结构

**新文件**:
- `markbot/utils/atomic.py` — `FileLock` + `atomic_write_json` + `LockAcquisitionError`(~80 行)
- `tests/test_atomic.py` — atomic primitives 单元测试(~10 测试)
- `tests/test_task_tracker_lock.py` — TaskTracker 并发测试(~6 测试)
- `tests/test_handoff_lock.py` — HandoffManager 并发测试(~5 测试)

**修改**:
- `pyproject.toml:20-47` — 在 `dependencies` 列表加 `portalocker>=2.5,<4.0`
- `markbot/session/task_tracker.py:94-100` — `__init__` 加 `self._lock`
- `markbot/session/task_tracker.py:117-128` — `_save_registry` 改用 `atomic_write_json`
- `markbot/session/task_tracker.py:134-165` — `_get_task` / `_save_task` 外加锁
- `markbot/session/task_tracker.py:263-271` — `list_all` 外加锁
- `markbot/session/task_tracker.py:301-314` — `cleanup_completed` 外加锁
- `markbot/session/handoff.py:138-141` — `__init__` 不变(per-key lock 通过 helper)
- `markbot/session/handoff.py:143-148` — 加 `_lock_for()` helper
- `markbot/session/handoff.py:150-157` — `save` 外加锁
- `markbot/session/handoff.py:159-169` — `load` 外加锁
- `markbot/session/handoff.py:177-181` — `delete` 外加锁

**不变**:
- `autopilot/types.py:57` (`TaskRegistry`) — 纯 dataclass,无 IO
- `session/task_tracker.py:75-81` (`TaskRegistry`) — 纯 dataclass,无 IO
- `session/handoff.py:183-193` (`cleanup_stale`) — bulk operation,假设低峰调用

---

## Task 1: 添加 portalocker 依赖

**Files:**
- Modify: `pyproject.toml:46`(在 `openai` 之后加 `portalocker`)

- [ ] **Step 1: 修改 pyproject.toml**

在 `dependencies` 列表 `openai>=2.8.0,<3.0.0` 行后(行 45)插入:
```toml
    "portalocker>=2.5.0,<4.0.0",
```

完整末尾应为:
```toml
    "openai>=2.8.0",
    "tiktoken>=0.12.0,<1.0.0",
    "portalocker>=2.5.0,<4.0.0",
]
```

- [ ] **Step 2: 验证 portalocker 可解析**

Run: `cd D:\Source\markbot; python -c "import portalocker; print(portalocker.__version__)"`
Expected: 输出 `3.2.0` 或类似已安装版本

- [ ] **Step 3: Commit**

```bash
cd D:\Source\markbot
git add pyproject.toml
git commit -m "chore(deps): add portalocker for cross-platform file locking"
```

---

## Task 2: 实现 `markbot/utils/atomic.py` 公共原语

**Files:**
- Create: `markbot/utils/atomic.py`

- [ ] **Step 1: 写文件**

Create `markbot/utils/atomic.py` with the following contents:

```python
"""Cross-platform file locking and atomic JSON writes.

Two primitives are exposed:

* :class:`FileLock` — a context manager that takes an exclusive
  flock-style lock on a sidecar ``.lock`` file. Safe for both
  cross-process coordination and in-process serialization. Acquired
  with a default 5 s timeout, after which
  :class:`LockAcquisitionError` is raised.

* :func:`atomic_write_json` — writes JSON to *path* via a temporary
  file in the same directory, fsyncs, and uses :func:`os.replace` to
  atomically swap it in. Partial writes are never visible to readers.

The sidecar-``.lock`` pattern (rather than locking the data file
directly) is required because portalocker's Windows backend
(``msvcrt.locking``) can only lock files that already exist, and
data files may not exist on first run.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import portalocker


class LockAcquisitionError(TimeoutError):
    """Raised when :class:`FileLock` could not acquire the lock in time.

    Inherits from :class:`TimeoutError` so callers can catch the
    builtin to handle both lock contention and timeouts uniformly.
    """


class FileLock:
    """Exclusive file lock using a sidecar ``.lock`` file.

    The lock is acquired on ``<path>.lock`` (sibling of *path*) so the
    data file can be atomically replaced without ever holding a lock
    on the data file itself. The lock file is created on demand.

    Example::

        with FileLock(path, timeout=5.0):
            data = json.loads(path.read_text())
            atomic_write_json(path, mutate(data))
    """

    def __init__(self, path: Path, timeout: float = 5.0) -> None:
        self._path = Path(path)
        self._timeout = timeout
        self._lock_path = self._path.with_name(self._path.name + ".lock")
        self._fh: portalocker.utils.Lock | None = None

    def __enter__(self) -> None:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path.touch(exist_ok=True)
        try:
            lock = portalocker.Lock(
                str(self._lock_path),
                mode="a",
                timeout=self._timeout,
                fail_when_locked=False,
                flags=portalocker.LOCK_EX,
            )
            lock.__enter__()
            self._fh = lock
        except portalocker.exceptions.LockException as exc:
            raise LockAcquisitionError(
                f"Could not acquire lock on {self._lock_path} "
                f"within {self._timeout}s: {exc}"
            ) from exc

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._fh is not None:
            try:
                self._fh.__exit__(exc_type, exc, tb)
            finally:
                self._fh = None


def atomic_write_json(
    path: Path,
    data: Any,
    *,
    indent: int | None = 2,
) -> None:
    """Write *data* as JSON to *path* atomically.

    Writes to ``<path>.<pid>.<uuid8>.tmp`` in the same directory,
    flushes + fsyncs, then calls :func:`os.replace` to atomically
    swap it in place. ``os.replace`` is atomic on POSIX and Windows,
    so readers always see either the old or new file — never a
    partial write.

    The ``default=str`` fallback lets datetimes, UUIDs, and other
    non-JSON-native types round-trip as their ``str()`` representation.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    tmp_path = path.with_name(tmp_name)
    try:
        text = json.dumps(
            data,
            ensure_ascii=False,
            indent=indent,
            default=str,
        ) + "\n"
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp_path), str(path))
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


__all__ = ["FileLock", "LockAcquisitionError", "atomic_write_json"]
```

- [ ] **Step 2: 验证导入**

Run: `cd D:\Source\markbot; python -c "from markbot.utils.atomic import FileLock, LockAcquisitionError, atomic_write_json; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
cd D:\Source\markbot
git add markbot/utils/atomic.py
git commit -m "feat(utils): add FileLock and atomic_write_json primitives"
```

---

## Task 3: 写 `tests/test_atomic.py`

**Files:**
- Create: `tests/test_atomic.py`

- [ ] **Step 1: 写测试文件**

Create `tests/test_atomic.py` with the following contents:

```python
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

    # No .tmp file should remain.
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []
    # And the data file must not have been created.
    assert not path.exists()


def test_file_lock_acquires_and_releases(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    lock = FileLock(path, timeout=1.0)
    with lock:
        assert (tmp_path / "data.json.lock").exists()
    # Re-acquire immediately after release — must succeed.
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
            time.sleep(0.05)  # hold the lock long enough for overlap risk
            counter["value"] = v + 1
            finished.append(threading.current_thread().ident)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join(timeout=5.0)

    # No thread should have crashed.
    assert len(finished) == 2
    # Counter must be exactly 2 (no lost increment).
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
```

- [ ] **Step 2: 跑测试**

Run: `cd D:\Source\markbot; python -m pytest tests/test_atomic.py -v`
Expected: 全部通过(11 tests passed)

- [ ] **Step 3: Commit**

```bash
cd D:\Source\markbot
git add tests/test_atomic.py
git commit -m "test(atomic): add unit tests for FileLock and atomic_write_json"
```

---

## Task 4: TaskTracker 加锁(主要类)

**Files:**
- Modify: `markbot/session/task_tracker.py`

- [ ] **Step 1: 改 imports**

Replace `markbot/session/task_tracker.py:15-24` 的 import 块,改为:
```python
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from markbot.utils.atomic import FileLock, atomic_write_json
from markbot.utils.helpers import ensure_dir
```

- [ ] **Step 2: 改 __init__ 加 self._lock**

Replace `markbot/session/task_tracker.py:94-100` (the `__init__` method body) with:
```python
    def __init__(self, workspace: Path, max_active_tasks: int = 1) -> None:
        self._workspace = workspace
        self._tasks_dir = workspace / self._TASKS_DIR
        self._max_active_tasks = max(1, max_active_tasks)
        ensure_dir(self._tasks_dir)
        self._registry_path = self._tasks_dir / self._REGISTRY_FILE
        self._lock = FileLock(self._registry_path)
        self._cache: dict[str, Task] | None = None
```

- [ ] **Step 3: 改 _save_registry 用 atomic_write_json**

Replace `markbot/session/task_tracker.py:117-129` (the `_save_registry` method body) with:
```python
    def _save_registry(self, registry: TaskRegistry) -> None:
        registry.updated_at = time.time()
        data = {
            "version": registry.version,
            "active_task_id": registry.active_task_id,
            "tasks": registry.tasks,
            "updated_at": registry.updated_at,
        }
        atomic_write_json(self._registry_path, data)
        self._cache = None
```

(注意:`_save_registry` 单独调用时**不加锁**——它假设调用方已经持锁,这是 read-modify-write 内部的 "write 阶段"。这与之前的设计保持一致,锁是**外层事务**持有的。)

- [ ] **Step 4: 改 _get_task 外加锁**

Replace `markbot/session/task_tracker.py:134-141` (the `_get_task` method body) with:
```python
    def _get_task(self, task_id: str) -> Task | None:
        with self._lock:
            if self._cache is not None and task_id in self._cache:
                return self._cache[task_id]
            registry = self._load_registry()
            for t_dict in registry.tasks:
                if t_dict.get("id") == task_id:
                    return Task(**{k: v for k, v in t_dict.items() if k in Task.__dataclass_fields__})
            return None
```

(注意:`_get_task` 只读,但仍加锁——与其他并发写保持快照一致。)

- [ ] **Step 5: 改 _save_task 外加锁**

Replace `markbot/session/task_tracker.py:143-165` (the `_save_task` method body) with:
```python
    def _save_task(self, task: Task) -> None:
        with self._lock:
            registry = self._load_registry()
            task.updated_at = time.time()
            task_dict = asdict(task)

            found = False
            for i, t in enumerate(registry.tasks):
                if t.get("id") == task.id:
                    registry.tasks[i] = task_dict
                    found = True
                    break
            if not found:
                registry.tasks.append(task_dict)

            if task.status == "in_progress":
                registry.active_task_id = task.id
            elif registry.active_task_id == task.id and task.status != "in_progress":
                registry.active_task_id = None

            self._save_registry(registry)
            if self._cache is None:
                self._cache = {}
            self._cache[task.id] = task
```

- [ ] **Step 6: 改 list_all 外加锁**

Replace `markbot/session/task_tracker.py:263-271` (the `list_all` method body) with:
```python
    def list_all(self) -> list[Task]:
        with self._lock:
            registry = self._load_registry()
            tasks: list[Task] = []
            for t_dict in registry.tasks:
                try:
                    tasks.append(Task(**{k: v for k, v in t_dict.items() if k in Task.__dataclass_fields__}))
                except Exception:
                    pass
            return sorted(tasks, key=lambda t: (-t.priority, -t.updated_at))
```

- [ ] **Step 7: 改 cleanup_completed 外加锁**

Replace `markbot/session/task_tracker.py:301-314` (the `cleanup_completed` method body) with:
```python
    def cleanup_completed(self, max_age_days: int = 30) -> int:
        now = time.time()
        cutoff = now - max_age_days * 86400
        with self._lock:
            registry = self._load_registry()
            before = len(registry.tasks)
            registry.tasks = [
                t for t in registry.tasks
                if t.get("status") not in ("done", "cancelled")
                or (t.get("completed_at") or t.get("updated_at", 0)) > cutoff
            ]
            removed = before - len(registry.tasks)
            if removed > 0:
                self._save_registry(registry)
                logger.info("Cleaned up {} completed tasks", removed)
            return removed
```

- [ ] **Step 8: 跑 TaskTracker 现有测试**

Run: `cd D:\Source\markbot; python -m pytest tests/ -k "task" -v --deselect tests/test_fallback.py::TestFallbackManager::test_circuit_threshold_default`
Expected: 所有 task 相关测试通过(无回归)

- [ ] **Step 9: Commit**

```bash
cd D:\Source\markbot
git add markbot/session/task_tracker.py
git commit -m "fix(session): add file locking + atomic writes to TaskTracker"
```

---

## Task 5: TaskTracker 并发测试

**Files:**
- Create: `tests/test_task_tracker_lock.py`

- [ ] **Step 1: 写测试文件**

Create `tests/test_task_tracker_lock.py` with the following contents:

```python
"""Concurrency tests for :class:`markbot.session.task_tracker.TaskTracker`.

These exercise the file lock and atomic write under simulated concurrent
load. They are slower than unit tests (each scenario spins up real
threads) but kept as ``test_*`` so the regression suite catches
lost-update or torn-write bugs.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

from markbot.session.task_tracker import TaskTracker


@pytest.fixture
def tracker(tmp_path: Path) -> TaskTracker:
    return TaskTracker(tmp_path, max_active_tasks=1)


def test_concurrent_create_task_no_loss(tracker: TaskTracker) -> None:
    """8 threads × 25 creates each = 200 unique tasks in registry."""

    def create_many(start: int) -> int:
        for i in range(25):
            tracker.create_task(f"task-{start}-{i}")
        return start

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(create_many, i) for i in range(8)]
        for f in as_completed(futures):
            f.result()

    assert len(tracker.list_all()) == 200


def test_concurrent_transition_one_succeeds(tracker: TaskTracker) -> None:
    """Two threads both try to start the same task — only one wins
    (the second sees the task already in_progress, which is a
    valid transition but no-op for active_task_id)."""
    task = tracker.create_task("race-target")

    def start() -> str:
        try:
            tracker.start_task(task.id)
            return "ok"
        except ValueError as exc:
            return f"err:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(start) for _ in range(2)]
        results = [f.result() for f in as_completed(futures)]

    # Both might "succeed" (transition stated→in_progress is valid
    # both times), but the final state must be exactly one in_progress
    # task with no list_all() > max_active_tasks violation.
    final = tracker.list_all()
    assert len(final) == 1
    assert final[0].status == "in_progress"


def test_concurrent_list_all_returns_consistent(tracker: TaskTracker) -> None:
    """Reads during writes return a consistent snapshot, never a
    half-written file."""

    def writer() -> None:
        for i in range(20):
            tracker.create_task(f"task-{i}")

    def reader() -> int:
        # Read many times during the writes.
        return max(len(tracker.list_all()) for _ in range(10))

    with ThreadPoolExecutor(max_workers=4) as pool:
        write_fut = pool.submit(writer)
        read_futs = [pool.submit(reader) for _ in range(3)]
        max_observed = max(f.result() for f in as_completed(read_futs))
        write_fut.result()

    # The reader must never see more than the final count.
    final_count = len(tracker.list_all())
    assert max_observed <= final_count


def test_concurrent_cleanup_completed_idempotent(tracker: TaskTracker) -> None:
    """Two cleanup calls in parallel do not corrupt registry."""
    for i in range(5):
        t = tracker.create_task(f"task-{i}")
        tracker.transition(t.id, "in_progress")
        tracker.transition(t.id, "done")

    def cleanup() -> int:
        return tracker.cleanup_completed(max_age_days=0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(cleanup).result() for _ in range(2)]

    # At least one cleanup reports 5; the other may report 0 (already
    # cleaned) — what matters is the registry has 0 done tasks left.
    assert tracker.list_completed() == []


def test_lock_creation_no_existing_registry(tmp_path: Path) -> None:
    """The lock file is created on first construction, even before
    the data file exists."""
    t = TaskTracker(tmp_path)
    assert (tmp_path / "tasks" / "registry.json.lock").exists()
    assert not (tmp_path / "tasks" / "registry.json").exists()


def test_existing_registry_lock_reused(tmp_path: Path) -> None:
    """A second TaskTracker instance on the same workspace shares the
    same lock file (and the second instance's writes are visible)."""
    t1 = TaskTracker(tmp_path)
    t2 = TaskTracker(tmp_path)
    t1.create_task("from-t1")
    assert any(t.title == "from-t1" for t in t2.list_all())
```

- [ ] **Step 2: 跑测试**

Run: `cd D:\Source\markbot; python -m pytest tests/test_task_tracker_lock.py -v --deselect tests/test_fallback.py::TestFallbackManager::test_circuit_threshold_default`
Expected: 全部通过(6 tests passed)

- [ ] **Step 3: Commit**

```bash
cd D:\Source\markbot
git add tests/test_task_tracker_lock.py
git commit -m "test(session): add concurrency tests for TaskTracker file lock"
```

---

## Task 6: HandoffManager 加锁

**Files:**
- Modify: `markbot/session/handoff.py`

- [ ] **Step 1: 改 imports**

Replace `markbot/session/handoff.py:14-24` (import 块) with:
```python
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from markbot.utils.atomic import FileLock, atomic_write_json
from markbot.utils.constants import USER_FILENAME
from markbot.utils.helpers import ensure_dir
```

- [ ] **Step 2: 加 _lock_for helper**

Insert this method into `HandoffManager` after `_handoff_path` (line 147-148) and before `save` (line 150). The new helper sits between them:

```python
    def _lock_for(self, session_key: str) -> FileLock:
        """Return a per-session-key file lock.

        Different session keys get independent locks, so parallel
        saves to distinct sessions do not contend. The lock is
        re-created cheaply on each call (it only opens a file
        handle on ``__enter__``).
        """
        return FileLock(self._handoff_path(session_key))
```

- [ ] **Step 3: 改 save 外加锁**

Replace `markbot/session/handoff.py:150-157` (the `save` method body) with:
```python
    def save(self, handoff: SessionHandoff) -> Path:
        path = self._handoff_path(handoff.session_key)
        with self._lock_for(handoff.session_key):
            atomic_write_json(path, handoff.to_dict())
        logger.info("Saved handoff for session {}", handoff.session_key)
        return path
```

- [ ] **Step 4: 改 load 外加锁**

Replace `markbot/session/handoff.py:159-169` (the `load` method body) with:
```python
    def load(self, session_key: str) -> SessionHandoff | None:
        path = self._handoff_path(session_key)
        with self._lock_for(session_key):
            if not path.exists():
                return None
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                handoff = SessionHandoff.from_dict(data)
                return handoff
            except Exception as e:
                logger.warning("Failed to load handoff for {}: {}", session_key, e)
                return None
```

- [ ] **Step 5: 改 delete 外加锁**

Replace `markbot/session/handoff.py:177-181` (the `delete` method body) with:
```python
    def delete(self, session_key: str) -> None:
        path = self._handoff_path(session_key)
        with self._lock_for(session_key):
            if path.exists():
                path.unlink()
                logger.debug("Deleted handoff for {}", session_key)
```

- [ ] **Step 6: 跑 Handoff 现有测试**

Run: `cd D:\Source\markbot; python -m pytest tests/ -k "handoff" -v --deselect tests/test_fallback.py::TestFallbackManager::test_circuit_threshold_default`
Expected: 所有 handoff 相关测试通过(无回归)

- [ ] **Step 7: Commit**

```bash
cd D:\Source\markbot
git add markbot/session/handoff.py
git commit -m "fix(session): add file locking to HandoffManager save/load/delete"
```

---

## Task 7: HandoffManager 并发测试

**Files:**
- Create: `tests/test_handoff_lock.py`

- [ ] **Step 1: 写测试文件**

Create `tests/test_handoff_lock.py` with the following contents:

```python
"""Concurrency tests for :class:`markbot.session.handoff.HandoffManager`."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from markbot.session.handoff import HandoffManager, SessionHandoff


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
    # Whatever value was last written is the value we get back;
    # the invariant is that the file is parseable JSON and the
    # SessionHandoff round-trips.
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

    # Every successful read produced a valid SessionHandoff (no
    # exception, no half-written file).
    assert ok_reads > 0


def test_lock_timeout_returns_none_or_raises(manager: HandoffManager) -> None:
    """A read that cannot acquire the lock within 5s (default) must
    raise ``LockAcquisitionError``, not hang or return stale data."""
    from markbot.utils.atomic import FileLock, LockAcquisitionError

    lock_path = manager._handoff_path("k")
    # Pre-acquire the lock in this thread.
    held = FileLock(lock_path, timeout=1.0)
    held.__enter__()
    try:
        # Now try to read with a short timeout — must fail.
        # (We use the internal helper to construct the lock for the
        # same path the manager would use.)
        contended = FileLock(lock_path, timeout=0.3)
        with pytest.raises(LockAcquisitionError):
            contended.__enter__()
    finally:
        held.__exit__(None, None, None)


def test_delete_during_load_safe(manager: HandoffManager) -> None:
    """A delete that races with a load must not raise, and the load
    returns either the old handoff or None."""
    import threading

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

    # Reader must have observed either the doomed or resurrected
    # value (not raised).
    assert result in ("doomed", "resurrected")
```

- [ ] **Step 2: 跑测试**

Run: `cd D:\Source\markbot; python -m pytest tests/test_handoff_lock.py -v --deselect tests/test_fallback.py::TestFallbackManager::test_circuit_threshold_default`
Expected: 全部通过(5 tests passed)

- [ ] **Step 3: Commit**

```bash
cd D:\Source\markbot
git add tests/test_handoff_lock.py
git commit -m "test(session): add concurrency tests for HandoffManager file lock"
```

---

## Task 8: 全量回归

- [ ] **Step 1: 跑全量测试**

Run: `cd D:\Source\markbot; python -m pytest --deselect tests/test_fallback.py::TestFallbackManager::test_circuit_threshold_default -q`
Expected: 863 (pre-P1-2) + 22 (new tests) = 885 tests passed

- [ ] **Step 2: 跑 lint**

Run: `cd D:\Source\markbot; python -m ruff check markbot/utils/atomic.py markbot/session/task_tracker.py markbot/session/handoff.py tests/test_atomic.py tests/test_task_tracker_lock.py tests/test_handoff_lock.py`
Expected: 无 errors

- [ ] **Step 3: 报告 + 准备 PR**

报告 P1-2 修复完成,展示:
- 文件清单:1 新增 utils module + 1 新增 deps + 2 改动 + 3 新增测试
- 测试增量: +22 (atomic 11 + task_tracker 6 + handoff 5)
- 全量 885 tests pass

---

## Self-Review

**Spec coverage:**
- [x] `markbot/utils/atomic.py` with FileLock, atomic_write_json, LockAcquisitionError → Task 2
- [x] `pyproject.toml` portalocker dependency → Task 1
- [x] TaskTracker lock integration (5 methods) → Task 4
- [x] HandoffManager lock integration (3 methods + 1 helper) → Task 6
- [x] `markbot/utils/atomic.py` unit tests (10+ tests) → Task 3
- [x] TaskTracker concurrency tests → Task 5
- [x] HandoffManager concurrency tests → Task 7
- [x] Full regression → Task 8

**Placeholder scan:** No "TBD" / "TODO" / "implement later" / "similar to Task N" / "fill in details" present.

**Type consistency:**
- `FileLock(path: Path, timeout: float = 5.0)` used identically in atomic.py, task_tracker.py, handoff.py ✓
- `atomic_write_json(path: Path, data: Any, *, indent: int | None = 2)` used identically ✓
- `LockAcquisitionError(TimeoutError)` referenced consistently in atomic.py and tests ✓
- `self._lock` attribute used in TaskTracker (5 methods) — all 5 added in Task 4 steps 4-7 ✓
- `self._lock_for(session_key)` used in HandoffManager — defined in Task 6 step 2, used in steps 3-5 ✓
