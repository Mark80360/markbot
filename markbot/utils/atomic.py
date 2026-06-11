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

    A threading lock is used internally to serialize in-process access,
    since portalocker's Windows backend (msvcrt) does not safely handle
    concurrent lock/unlock from multiple threads on the same file handle.

    Example::

        with FileLock(path, timeout=5.0):
            data = json.loads(path.read_text())
            atomic_write_json(path, mutate(data))
    """

    def __init__(self, path: Path, timeout: float = 5.0) -> None:
        import threading

        self._path = Path(path)
        self._timeout = timeout
        self._lock_path = self._path.with_name(self._path.name + ".lock")
        self._fh: object | None = None
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path.touch(exist_ok=True)
        self._thread_lock = threading.Lock()

    def __enter__(self) -> None:
        self._thread_lock.acquire()
        try:
            lock = portalocker.Lock(
                str(self._lock_path),
                mode="a",
                timeout=self._timeout,
                check_interval=0.1,
                fail_when_locked=False,
                flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
            )
            lock.__enter__()
            self._fh = lock
        except portalocker.exceptions.LockException as exc:
            self._thread_lock.release()
            raise LockAcquisitionError(
                f"Could not acquire lock on {self._lock_path} "
                f"within {self._timeout}s: {exc}"
            ) from exc

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        try:
            if self._fh is not None:
                try:
                    self._fh.__exit__(exc_type, exc, tb)
                finally:
                    self._fh = None
        finally:
            self._thread_lock.release()


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
