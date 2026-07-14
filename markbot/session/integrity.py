"""Session integrity — WAL, checksums, and archiving.

Provides write-ahead logging, SHA-256 checksum verification, and
archive lifecycle management for session files.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

# Archive entries older than this many days are removed by cleanup_archive.
_ARCHIVE_TTL_DAYS = 30


class SessionIntegrity:
    """Manage session file integrity via WAL, checksums, and archiving."""

    def __init__(self, sessions_dir: Path) -> None:
        self.sessions_dir = Path(sessions_dir)
        self.archive_dir = self.sessions_dir / "archive"

    # ------------------------------------------------------------------
    # Write-Ahead Log (WAL)
    # ------------------------------------------------------------------

    def write_wal(self, session_path: Path, data: str) -> Path:
        """Write *data* to a WAL sidecar for *session_path*.

        Returns the path of the WAL file.
        """
        wal_path = Path(session_path).with_suffix(".wal")
        wal_path.parent.mkdir(parents=True, exist_ok=True)
        wal_path.write_text(data, encoding="utf-8")
        return wal_path

    def commit_wal(self, session_path: Path) -> None:
        """Remove the WAL sidecar for *session_path* (no-op if absent)."""
        wal_path = Path(session_path).with_suffix(".wal")
        if wal_path.exists():
            wal_path.unlink()

    def recover_from_wal(self, session_path: Path) -> str | None:
        """Return WAL content for *session_path*, or ``None`` if no WAL."""
        wal_path = Path(session_path).with_suffix(".wal")
        if not wal_path.exists():
            return None
        return wal_path.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # Checksums
    # ------------------------------------------------------------------

    def compute_checksum(self, data: str) -> str:
        """Return the SHA-256 hex digest of *data*."""
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    def write_checksum(self, session_path: Path, data: str) -> None:
        """Write a ``.sha256`` checksum sidecar for *session_path*."""
        session_path = Path(session_path)
        checksum = self.compute_checksum(data)
        cs_path = session_path.with_suffix(".sha256")
        cs_path.write_text(checksum, encoding="utf-8")

    def verify_checksum(self, session_path: Path) -> bool:
        """Verify the session file against its ``.sha256`` sidecar.

        Returns ``True`` if no checksum sidecar exists (nothing to verify)
        or if the checksum matches.
        """
        session_path = Path(session_path)
        cs_path = session_path.with_suffix(".sha256")
        if not cs_path.exists():
            return True
        expected = cs_path.read_text(encoding="utf-8").strip()
        actual = self.compute_checksum(session_path.read_text(encoding="utf-8"))
        return expected == actual

    # ------------------------------------------------------------------
    # Archiving
    # ------------------------------------------------------------------

    def archive_session(self, session_path: Path) -> Path | None:
        """Move *session_path* (and its sidecars) into the archive dir.

        Returns the archived path, or ``None`` if the session file does
        not exist.
        """
        session_path = Path(session_path)
        if not session_path.exists():
            return None

        self.archive_dir.mkdir(parents=True, exist_ok=True)
        dest = self.archive_dir / session_path.name
        shutil.move(str(session_path), str(dest))

        # Move sidecar files (.sha256, .wal) alongside the session.
        for suffix in (".sha256", ".wal"):
            sidecar = session_path.with_suffix(suffix)
            if sidecar.exists():
                shutil.move(str(sidecar), str(dest.with_suffix(suffix)))

        return dest

    def cleanup_archive(self) -> int:
        """Remove archive entries older than :data:`_ARCHIVE_TTL_DAYS`.

        Returns the number of removed files.
        """
        if not self.archive_dir.exists():
            return 0

        import time

        cutoff = time.time() - _ARCHIVE_TTL_DAYS * 86400
        removed = 0
        for entry in self.archive_dir.iterdir():
            if not entry.is_file():
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                entry.unlink()
                removed += 1
        return removed
