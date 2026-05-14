"""Session integrity enhancements — WAL, archiving, and checksums.

Layered on top of the existing SessionManager to avoid breaking changes.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger


class SessionIntegrity:
    """Adds WAL, checksum, and archiving to session persistence.

    Designed to wrap ``SessionManager`` without modifying its internals.
    """

    _WAL_SUFFIX = ".wal"
    _ARCHIVE_DIR = "archive"
    _CHECKSUM_SUFFIX = ".sha256"

    def __init__(self, sessions_dir: Path, archive_ttl_days: int = 90) -> None:
        self.sessions_dir = sessions_dir
        self.archive_dir = sessions_dir / self._ARCHIVE_DIR
        self.archive_ttl_days = archive_ttl_days

    def write_wal(self, session_path: Path, data: str) -> Path:
        """Write a WAL entry before the actual session write.

        Returns the WAL file path.
        """
        wal_path = session_path.with_suffix(self._WAL_SUFFIX)
        wal_path.parent.mkdir(parents=True, exist_ok=True)
        wal_path.write_text(data, encoding="utf-8")
        return wal_path

    def commit_wal(self, session_path: Path) -> None:
        """Remove the WAL file after a successful write."""
        wal_path = session_path.with_suffix(self._WAL_SUFFIX)
        try:
            wal_path.unlink(missing_ok=True)
        except Exception as e:
            logger.warning("WAL cleanup failed for {}: {}", session_path, e)

    def recover_from_wal(self, session_path: Path) -> str | None:
        """Attempt to recover data from WAL if the main file is corrupted.

        Returns recovered data string, or None if no WAL exists.
        """
        wal_path = session_path.with_suffix(self._WAL_SUFFIX)
        if not wal_path.exists():
            return None

        try:
            data = wal_path.read_text(encoding="utf-8")
            logger.info("Recovered from WAL: {}", session_path.name)
            return data
        except Exception as e:
            logger.error("WAL recovery failed for {}: {}", session_path, e)
            return None

    def compute_checksum(self, data: str) -> str:
        """Compute SHA-256 checksum for data integrity verification."""
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    def write_checksum(self, session_path: Path, data: str) -> None:
        """Write a checksum file alongside the session file."""
        cs_path = session_path.with_suffix(self._CHECKSUM_SUFFIX)
        checksum = self.compute_checksum(data)
        cs_path.write_text(checksum, encoding="utf-8")

    def verify_checksum(self, session_path: Path) -> bool:
        """Verify the session file's integrity against its checksum.

        Returns True if valid (or no checksum file exists).
        Returns False if checksum mismatch.
        """
        cs_path = session_path.with_suffix(self._CHECKSUM_SUFFIX)
        if not cs_path.exists():
            return True

        try:
            expected = cs_path.read_text(encoding="utf-8").strip()
            actual = self.compute_checksum(session_path.read_text(encoding="utf-8"))
            if expected != actual:
                logger.error(
                    "Checksum mismatch for {}: expected={}, actual={}",
                    session_path.name, expected[:16], actual[:16],
                )
                return False
            return True
        except Exception as e:
            logger.error("Checksum verification failed for {}: {}", session_path, e)
            return False

    def archive_session(self, session_path: Path) -> Path | None:
        """Move an expired session to the archive directory.

        Returns the archive path, or None on failure.
        """
        if not session_path.exists():
            return None

        self.archive_dir.mkdir(parents=True, exist_ok=True)
        archive_name = f"{session_path.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{session_path.suffix}"
        archive_path = self.archive_dir / archive_name

        try:
            shutil.move(str(session_path), str(archive_path))
            for suffix in (self._CHECKSUM_SUFFIX, self._WAL_SUFFIX):
                sidecar = session_path.with_suffix(suffix)
                if sidecar.exists():
                    sidecar_archive = archive_path.with_suffix(suffix)
                    shutil.move(str(sidecar), str(sidecar_archive))
            logger.info("Archived session: {} → {}", session_path.name, archive_path.name)
            return archive_path
        except Exception as e:
            logger.error("Archive failed for {}: {}", session_path, e)
            return None

    def cleanup_archive(self) -> int:
        """Remove archive entries older than ``archive_ttl_days``."""
        if not self.archive_dir.exists():
            return 0

        cutoff = datetime.now().timestamp() - self.archive_ttl_days * 86400
        removed = 0
        for path in self.archive_dir.glob("*.jsonl"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    for suffix in (self._CHECKSUM_SUFFIX, self._WAL_SUFFIX):
                        sidecar = path.with_suffix(suffix)
                        if sidecar.exists():
                            sidecar.unlink()
                    removed += 1
            except Exception:
                continue

        if removed:
            logger.info("Cleaned up {} archived session(s)", removed)
        return removed
