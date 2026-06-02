# P1-2: Task / Handoff 文件锁 + 原子写 — 设计

**Date**: 2026-06-02
**Status**: Design
**Related**: P0-3 修复在 P0 阶段(commit `cc9d255`)已经处理了 *log 脱敏*;P1-2 修复 *持久化并发安全*

## 1. 问题陈述

`markbot` 在多进程 / 多线程场景下,`TaskTracker` 和 `HandoffManager` 的持久化层存在 read-modify-write 竞态:

```
Process A: registry = _load_registry()    # 读取 task 列表
           # ... 内存中修改 ...
           _save_registry(registry)       # 写回
Process B: 同样 read-modify-write
           → 后写入者覆盖先写入者的变更
```

虽然 `markbot` CLI 通常是单进程使用,但:
- **同进程多线程**:agent loop 可能在不同 thread 写 task state
- **多进程实例**:多个 markbot gateway / 桌面客户端共享同一 workspace
- **CLI 触发 agent loop**:用户手动触发 `markbot task` 子命令时,后台 agent loop 正在跑

**现状代码漏洞**:
- `TaskTracker._save_task` (line 143-165):read-modify-write,无锁
- `TaskTracker._get_task` (line 134-141):读,无锁
- `TaskTracker.list_all` (line 263-271):读,无锁
- `HandoffManager.save` (line 150-157):写,无锁
- `HandoffManager.load` (line 159-169):读,无锁

**触发示例** (多进程):
```
两个 markbot gateway 实例同时收到 task 完成事件
两个实例都调 markbot.task.mark_done(task_id)
各自执行:read → 找到 task → mark done → write
但 write 阶段:一个实例的 write 完成、另一个的 write 接着覆盖(或反过来)
结果:其中一个 task 状态被覆盖
```

## 2. 解决方案概述

### 2.1 公共原语层(新)

新建 `markbot/utils/atomic.py`:
- `FileLock` 上下文管理器:portalocker 封装的**侧边 `.lock` 文件**锁
- `atomic_write_json` 函数:tmp 文件 + fsync + `os.replace`
- `LockAcquisitionError` 异常:5s 拿不到锁时报

### 2.2 业务类改动(只改两个,不是三个)

| 类 | 文件 | 改动 | 备注 |
|---|---|---|---|
| `TaskTracker` | `markbot/session/task_tracker.py:83` | 加 `self._lock`,所有读 / 写方法外包 `with self._lock:` | **核心改动** |
| `HandoffManager` | `markbot/session/handoff.py:132` | `save/load/delete` 加 per-session-key 锁 | **核心改动** |
| `TaskRegistry` (session) | `markbot/session/task_tracker.py:76` | **不改** | 纯 `@dataclass`,无 IO |
| `TaskRegistry` (autopilot) | `markbot/autopilot/types.py:57` | **不改** | 纯 `@dataclass`,无 IO |

### 2.3 公共 API 完全不变

`TaskTracker` 和 `HandoffManager` 的所有公开方法签名、返回值、异常类型 **保持不变**。锁对调用者透明。

## 3. 设计决策

### 3.1 锁库选型:portalocker

**决定**: 使用 `portalocker >=2.5,<4.0` 作为依赖。

**理由**:
- 跨平台(Windows / macOS / Linux 一致 API)
- API 简洁:portalocker 3.x 提供 `portalocker.Lock(filename, mode, timeout, flags)`
- 自带超时 + 失败检测
- 在 Windows 上用 `msvcrt.locking`,POSIX 上用 `fcntl.flock`/BSD lock
- 进程崩溃时自动释放锁(基于 file handle,kernel 清理)

**对比 stdlib shim**: 0 依赖 vs 简单 API 之间的权衡。考虑到 markbot 已经有 30+ 外部依赖(typer, anthropic, pydantic, 等),加一个 ~50KB 跨平台文件锁库代价小。

### 3.2 锁粒度:文件级

**决定**: 整个 JSON 文件一个锁(读 + 写都拿排他锁)。

**理由**:
- `TaskTracker` 单个文件 (`registry.json`);`HandoffManager` 每个 session_key 一个文件
- 数据量小(几十到几百条任务),全文件读-改-写完全可行
- portalocker 默认就是文件级锁,无需特殊处理
- key 级锁(lock 不同字节范围)需要 fcntl byte-range 锁,Windows 上 `msvcrt.locking` 不支持

### 3.3 锁模式:全排他锁 + 5s 超时

**决定**: 所有 IO 操作(包括读)都拿 `LOCK_EX`,默认超时 5 秒。

**理由**:
- 简化实现:一个锁、一个上下文管理
- task / handoff 不是高频访问(每秒不会读几百次)
- 读 + 写都用排他锁,避免"读卡住时写被卡"的写饥饿
- 5s 超时足够:portalocker 用 LOCK_EX + timeout=5 会在失败时抛 `LockException`,我们包装成 `LockAcquisitionError`

**接受成本**:
- 读操作比读共享锁稍慢(不必要的串行化)
- 5s 内未拿到锁视为失败(更适合自动化场景)

### 3.4 锁文件位置:侧边 `.lock` 文件

**决定**: 在数据文件旁创建 `<data_file_name>.lock` 文件作为锁。

**理由**:
- portalocker 需要文件**已存在**才能加锁(`msvcrt.locking` 在 Windows 上的要求)
- 数据文件可能不存在(首次运行)
- 侧边 `.lock` 文件在 `__init__` 时 `touch(exist_ok=True)`,确保存在
- 锁与数据分离:数据文件可被 `os.replace` 原子替换,锁文件不动

### 3.5 原子写:`atomic_write_json`

**决定**: 实现 `atomic_write_json(path, data, indent=2)`,内部流程:
1. 写 `<path>.<pid>.<uuid8>.tmp`(同目录,确保 `os.replace` 原子)
2. `f.flush(); os.fsync(f.fileno())` 强制落盘
3. `os.replace(tmp, path)` 原子重命名
4. 异常时清理 `tmp.unlink(missing_ok=True)`

**理由**:
- `os.replace` 在 POSIX 和 Windows 上都是原子的(POSIX `rename(2)` atomic since 3.3;Windows `MoveFileEx` with `MOVEFILE_REPLACE_EXISTING`)
- `<pid>.<uuid8>` 后缀防止两个进程意外选同一临时文件名
- `default=str` 防御非 JSON-native 类型(datetime, UUID)
- 写完到 fsync 期间进程崩溃:tmp 文件残留,但**数据文件未损坏**(旧版本完整)

### 3.6 异常处理

- **锁拿不到** → 抛 `LockAcquisitionError(TimeoutError)`,调用方决定重试 / 失败 / 报告用户
- **写失败** → 异常向上抛,`atomic_write_json` 内部已清理 tmp
- **读失败** (JSON parse error) → 现有代码已 `logger.warning` + 回落空数据,不改

### 3.7 锁范围:`read-modify-write` 全程持锁

每个公开方法的整个方法体在 `with self._lock:` 内。**不是**只在 `_save_registry` / `_save_task` 内部持锁,因为 `_save_task` = `_load_registry` + modify + `_save_registry`,锁必须横跨整个事务。

## 4. 接口设计

### 4.1 `markbot/utils/atomic.py`

```python
class LockAcquisitionError(TimeoutError):
    """Could not acquire file lock within timeout."""

class FileLock:
    """Cross-platform file lock using a sidecar .lock file.
    
    Usage:
        with FileLock(path, timeout=5.0):
            data = read(path)
            atomic_write_json(path, data)
    """
    def __init__(self, path: Path, timeout: float = 5.0) -> None: ...
    def __enter__(self) -> None: ...
    def __exit__(self, *exc: object) -> None: ...

def atomic_write_json(
    path: Path,
    data: Any,
    *,
    indent: int | None = 2,
) -> None:
    """Write *data* as JSON atomically. On Windows + POSIX, partial
    writes are never visible to readers (os.replace is atomic)."""
```

### 4.2 `TaskTracker` 改动

```python
class TaskTracker:
    def __init__(self, workspace: Path, max_active_tasks: int = 1) -> None:
        ...
        self._lock = FileLock(self._registry_path)  # 新增
    
    def _save_task(self, task: Task) -> None:
        with self._lock:                              # 新增
            registry = self._load_registry()
            ... (mutate)
            atomic_write_json(self._registry_path, data)  # 替换原 write_text
            self._cache = None
    
    def _get_task(self, task_id: str) -> Task | None:
        with self._lock:                              # 新增
            if self._cache is not None and task_id in self._cache:
                return self._cache[task_id]
            registry = self._load_registry()
            ...
    
    def list_all(self) -> list[Task]:
        with self._lock:                              # 新增
            ...
    
    def cleanup_completed(self, ...) -> int:
        with self._lock:                              # 新增
            ...
```

### 4.3 `HandoffManager` 改动

```python
class HandoffManager:
    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self._handoff_dir = workspace / "sessions" / self._HANDOFF_DIR
        ensure_dir(self._handoff_dir)
    
    def _lock_for(self, session_key: str) -> FileLock:  # 新增 helper
        return FileLock(self._handoff_path(session_key))
    
    def save(self, handoff: SessionHandoff) -> Path:
        path = self._handoff_path(handoff.session_key)
        with self._lock_for(handoff.session_key):      # 新增
            atomic_write_json(path, handoff.to_dict())
        ...
        return path
    
    def load(self, session_key: str) -> SessionHandoff | None:
        path = self._handoff_path(session_key)
        with self._lock_for(session_key):              # 新增
            if not path.exists():
                return None
            try:
                data = json.loads(path.read_text(...))
                ...
```

## 5. 测试设计

### 5.1 `tests/test_atomic.py` (~10 tests)

- `test_atomic_write_creates_file`
- `test_atomic_write_overwrites_existing`
- `test_atomic_write_uses_utf8_no_ascii_escape`
- `test_atomic_write_serializes_dataclass_default_str`
- `test_atomic_write_cleans_up_tmp_on_failure` (mock JSON dump to raise)
- `test_file_lock_acquires_and_releases`
- `test_file_lock_acquisition_timeout_raises`
- `test_file_lock_serializes_concurrent_holders` (2 threads, one waits for other)
- `test_file_lock_sidecar_creates_lock_file`
- `test_lock_acquisition_error_is_timeout_subclass`

### 5.2 `tests/test_task_tracker_lock.py` (~6 tests)

- `test_concurrent_create_task_no_loss` (ThreadPoolExecutor 8 threads × 25 creates = 200 expected)
- `test_concurrent_transition_same_task_one_succeeds` (state machine validation, 2 threads race)
- `test_concurrent_list_all_returns_consistent_snapshot`
- `test_concurrent_cleanup_completed_idempotent`
- `test_lock_creation_no_existing_registry` (lock file is created if missing)
- `test_existing_registry_lock_reused` (subsequent acquisitions use same lock file)

### 5.3 `tests/test_handoff_lock.py` (~5 tests)

- `test_concurrent_save_different_keys_no_contention` (parallel writes to distinct session keys)
- `test_concurrent_save_same_key_one_wins` (last write wins, no data corruption)
- `test_load_during_save_returns_consistent` (atomic read during write)
- `test_lock_timeout_returns_none_or_raises`
- `test_delete_during_load_safe`

## 6. 失败模式分析

| 失败模式 | 后果 | 处理 |
|---|---|---|
| 进程崩溃在 `_load_registry` 后、`atomic_write_json` 前 | 数据未变,锁未持(锁内崩溃→自动释放) | 安全:旧数据完整 |
| 进程崩溃在 `atomic_write_json` 中 (写 tmp 时) | tmp 文件残留,数据文件未动 | 安全:启动时可清理 `*.tmp` |
| 进程崩溃在 `os.replace` 期间 | 操作系统原子性保证(`os.replace` 单系统调用) | 安全:文件要么旧要么新 |
| 进程崩溃持锁中 | portalocker handle 释放,kernel cleanup | 安全:锁自然释放,无 stale |
| 锁 5s 内拿不到 | `LockAcquisitionError` 抛 | 调用方决定 |
| 两个 markbot 实例同 workspace | portalocker 跨进程协调 | 安全:第二个实例等第一个完成 |

## 7. 不在本次范围

- **不**做跨进程 PID 探测(portalocker 已正确处理 release-on-crash)
- **不**改 `TaskTracker` 内部业务逻辑(只加锁,不改 cache 策略、状态机)
- **不**改 `HandoffManager.cleanup_stale`(批量操作,不与其他 save 冲突)
- **不**做 stale `.tmp` 文件后台清理(下次同名操作自然覆盖)
- **不**为 `cleanup_stale` 加锁(它是个 bulk operation,假设在低峰时调用)

## 8. 依赖

新增一个外部依赖:
- `portalocker>=2.5,<4.0` (添加到 `pyproject.toml` 的 `dependencies`)

## 9. 兼容性

- `TaskTracker` 和 `HandoffManager` 的所有公开方法签名不变
- 文件格式不变(JSON 结构相同)
- 已有 .json 文件不需迁移
- 已在 workspace 跑 markbot 的用户:无感升级
